"""微信公众号开放平台对接(V24):一键把排版好的文章发进草稿箱.

- 租户级配置:AppID/AppSecret 存 app_setting `wechat_mp:{tid}`(各企业连各自的公众号);
- 令牌:stable_token 接口,缓存到 `wechat_token:{tid}`;
- 发草稿流程:正文图片逐张传 uploadimg(换成微信 CDN 链接,站外图会被公众号剥掉)
  → 封面传永久素材拿 thumb_media_id → draft/add;
- 错误码全部翻译成老板能看懂的人话(最常见:IP 白名单没加服务器 IP)。

配置要求(前端配置向导同步展示):
1) mp.weixin.qq.com → 设置与开发 → 开发接口管理/基本配置,拿 AppID、生成 AppSecret;
2) 同页「IP 白名单」加上服务器公网 IP;
3) 未认证的个人订阅号没有草稿箱接口权限(errcode 48001),需要微信认证。
"""
import io
import json
import logging
import os
import time

import httpx

from . import db, secureconfig

log = logging.getLogger("wechat")

API = "https://api.weixin.qq.com/cgi-bin"
SERVER_IP = os.environ.get("PAIHUO_PUBLIC_EGRESS_IP", "").strip()

ERR_HINTS = {
    40013: "AppID 不对,回公众号后台「设置与开发→基本配置」重新复制",
    40125: "AppSecret 不对或已重置,重新生成后粘贴进来",
    41004: "AppSecret 缺失,请在配置里填上",
    40001: "AppSecret 错误或已失效,重新生成后再试",
    40164: (
        "服务器 IP 不在公众号白名单：去「基本配置→IP白名单」添加部署服务器的"
        + (f"出口 IP {SERVER_IP}" if SERVER_IP
           else "出口 IP(平台未配置该 IP 的展示,请点右下角 💬 联系平台顾问索取)")
        + " 再试"
    ),
    48001: "该公众号没有草稿箱接口权限:未认证的个人订阅号不支持,需要完成微信认证",
    45009: "接口调用次数超限,明天再试或去后台清空配额",
    45110: "草稿箱已满(上限比较高,一般是没清理),去公众号后台删些旧草稿",
    40007: "封面素材无效,换一张 PNG/JPG 封面再试",
}


class WeChatError(Exception):
    def __init__(self, code, msg="", *, public: bool = False):
        self.code = code
        hint = ERR_HINTS.get(code)
        detail = hint or (
            str(msg) if public else "微信接口暂时不可用，请稍后重试"
        )
        super().__init__(f"{detail}(errcode {code})")


SECRET_FIELD = "wechat_mp_secret"


def get_conf(tid: int) -> dict:
    """返回可直接使用的配置;AppSecret 在库里是密文,这里解密后只存在于内存。"""
    conf = db.jloads(db.get_setting(f"wechat_mp:{tid}"), {}) or {}
    if conf.get("secret"):
        conf["secret"] = secureconfig.decrypt_field(SECRET_FIELD, conf["secret"])
    return conf


def set_conf(tid: int, appid: str, secret: str):
    cur = get_conf(tid)
    if appid:
        cur["appid"] = appid.strip()
    if secret:                      # 前端回存打码值时不覆盖
        cur["secret"] = secret.strip()
    stored = dict(cur)
    # AppSecret 能代表公众号做任何事,不能与备份一起明文流出。
    if stored.get("secret"):
        stored["secret"] = secureconfig.encrypt_field(SECRET_FIELD, stored["secret"])
    db.set_setting(f"wechat_mp:{tid}", json.dumps(stored))
    db.set_setting(f"wechat_token:{tid}", None)   # 换号后旧 token 作废


def _raise_if_err(d: dict):
    if isinstance(d, dict) and d.get("errcode"):
        raise WeChatError(d["errcode"], d.get("errmsg", ""))


async def token(tid: int, force: bool = False) -> str:
    conf = get_conf(tid)
    if not (conf.get("appid") and conf.get("secret")):
        raise WeChatError(
            0,
            "还没配置公众号 AppID/AppSecret(发布渠道页配置)",
            public=True,
        )
    cache = db.jloads(db.get_setting(f"wechat_token:{tid}"), {}) or {}
    if not force and cache.get("token") and cache.get("exp", 0) > time.time() + 120:
        return cache["token"]
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(f"{API}/stable_token",
                           json={"grant_type": "client_credential",
                                 "appid": conf["appid"], "secret": conf["secret"],
                                 "force_refresh": False})
        d = r.json()
    _raise_if_err(d)
    tok = d.get("access_token")
    if not tok:
        raise WeChatError(0)
    db.set_setting(f"wechat_token:{tid}",
                   json.dumps({"token": tok, "exp": time.time() + int(d.get("expires_in", 7200))}))
    return tok


async def test_conn(tid: int) -> dict:
    """连通性测试:强制取一次新 token."""
    await token(tid, force=True)
    return {"ok": True, "note": "连接成功,可以一键发草稿箱了"}


# ---------------- 图片上传 ----------------
def _to_jpg_under_1m(data: bytes) -> bytes:
    """uploadimg 限 1MB 且只收 jpg/png:超限就 PIL 压缩."""
    if len(data) <= 990 * 1024 and data[:3] == b"\xff\xd8\xff":
        return data
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    for q in (88, 78, 66, 55):
        if im.width > 1200:
            im = im.resize((1200, round(im.height * 1200 / im.width)))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q)
        if buf.tell() <= 990 * 1024:
            return buf.getvalue()
    return buf.getvalue()


async def upload_content_img(tid: int, data: bytes, name: str = "img.jpg") -> str:
    """正文图 → 微信 CDN 链接(不占素材库额度)."""
    tok = await token(tid)
    data = _to_jpg_under_1m(data)
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.post(f"{API}/media/uploadimg?access_token={tok}",
                           files={"media": (name, data, "image/jpeg")})
        d = r.json()
    _raise_if_err(d)
    return d["url"]


async def upload_thumb(tid: int, data: bytes, name: str = "cover.jpg") -> str:
    """封面 → 永久素材,返回 thumb_media_id(draft 必填)."""
    tok = await token(tid)
    data = _to_jpg_under_1m(data)
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.post(f"{API}/material/add_material?access_token={tok}&type=image",
                           files={"media": (name, data, "image/jpeg")})
        d = r.json()
    _raise_if_err(d)
    return d["media_id"]


async def add_draft(tid: int, title: str, digest: str, content_html: str,
                    thumb_media_id: str, author: str = "") -> str:
    tok = await token(tid)
    article = {"title": (title or "未命名")[:60], "author": (author or "")[:8],
               "digest": (digest or "")[:110], "content": content_html,
               "thumb_media_id": thumb_media_id, "need_open_comment": 1}
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.post(f"{API}/draft/add?access_token={tok}",
                           content=json.dumps({"articles": [article]}, ensure_ascii=False)
                           .encode("utf-8"),
                           headers={"Content-Type": "application/json; charset=utf-8"})
        d = r.json()
    _raise_if_err(d)
    return d.get("media_id", "")


async def find_draft_by_marker(tid: int, request_key: str) -> str:
    """对超时/本地落库失败的投递做只读对账，避免盲目再建一份草稿。"""
    marker = f"paihuo-draft:{request_key}"
    tok = await token(tid)
    offset = 0
    async with httpx.AsyncClient(timeout=60) as cli:
        for _ in range(4):  # 最多核对最近 80 份，避免大草稿箱拖垮请求。
            response = await cli.post(
                f"{API}/draft/batchget?access_token={tok}",
                json={"offset": offset, "count": 20, "no_content": 0},
            )
            payload = response.json()
            _raise_if_err(payload)
            items = payload.get("item") or []
            for item in items:
                for article in ((item.get("content") or {}).get("news_item")) or []:
                    if marker in str(article.get("content") or ""):
                        return str(item.get("media_id") or "")
            offset += len(items)
            if not items or offset >= int(payload.get("total_count") or 0):
                break
    return ""


# ---------------- 数据回流(V25:已群发文章的真实数据,需认证号数据分析权限) ----------------
async def article_stats(tid: int, title: str, published_at: float) -> dict:
    """按标题在已发布列表里找文章,再拉 datacube 阅读数据.找不到/无权限返回 {}."""
    tok = await token(tid)
    target = (title or "").strip()[:30]
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(f"{API}/freepublish/batchget?access_token={tok}",
                           json={"offset": 0, "count": 20, "no_content": 1})
        d = r.json()
        _raise_if_err(d)
        found = None
        for it in d.get("item") or []:
            for art in ((it.get("content") or {}).get("news_item")) or []:
                if target and target in (art.get("title") or ""):
                    found = art
                    break
        if not found:
            return {}
        # datacube 按发文日期取该日所有图文的数据,再按标题聚合当天合计
        day = time.strftime("%Y-%m-%d", time.localtime(published_at))
        r = await cli.post(f"{API}/datacube/getarticletotal?access_token={tok}",
                           json={"begin_date": day, "end_date": day})
        d = r.json()
        _raise_if_err(d)
        for item in d.get("list") or []:
            if target in (item.get("title") or ""):
                det = (item.get("details") or [{}])[-1]
                return {"read_user": det.get("int_page_read_user"),
                        "read_count": det.get("int_page_read_count"),
                        "share_user": det.get("share_user"),
                        "fav_user": det.get("add_to_fav_user"),
                        "ori_read": det.get("ori_page_read_user"),
                        "url": found.get("url", "")}
    return {}


# ---------------- 兜底封面(工单没有 PNG/JPG 封面时现画一张) ----------------
_CJK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"


def make_cover_png(title: str, color: str = "#ff7f2a") -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    w, h = 900, 383
    base = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    im = Image.new("RGB", (w, h), base)
    dr = ImageDraw.Draw(im)
    for y in range(h):  # 简单纵向渐变
        f = y / h * 0.35
        dr.line([(0, y), (w, y)],
                fill=tuple(min(255, round(c + (255 - c) * f)) for c in base))
    try:
        font = ImageFont.truetype(_CJK_FONT, 56)
    except OSError:
        font = ImageFont.load_default()
    text = (title or "").strip()[:24]
    lines = [text[i:i + 12] for i in range(0, len(text), 12)][:2]
    y0 = h / 2 - len(lines) * 36
    for i, ln in enumerate(lines):
        tw = dr.textlength(ln, font=font)
        dr.text(((w - tw) / 2, y0 + i * 74), ln, fill="#ffffff", font=font)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def local_asset(url_path: str, tenant_id: int = None, expected_job_id: int = None):
    """Compatibility wrapper; tenant context is mandatory for local file access."""
    if tenant_id is None:
        return None
    from . import assetfiles
    try:
        return assetfiles.resolve_tenant_asset(
            url_path, tenant_id, expected_job_id=expected_job_id
        )
    except assetfiles.AssetAccessError:
        return None
