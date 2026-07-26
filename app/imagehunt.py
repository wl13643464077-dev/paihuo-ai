"""全网真实素材图抓取(V24).

三路图片搜索引擎(必应/百度/360)并行兜底,不依赖任何付费 API key:
- search():只搜不下载,返回候选(交付页「真实图库」让老板挑);
- fetch_image():下载单张 + 安全校验(SSRF守卫/图片魔数/尺寸)+ PIL 统一转 JPG;
- hunt_for_job():流水线多媒体师用——按配图点位生成检索词,抓回真实图存进工单素材。

版权提示:抓取图仅作素材参考,商用请自行确认版权(前端有提示)。
"""
import asyncio
import html as _htmlmod
import io
import json
import logging
import os
import re
import warnings
from urllib.parse import quote, urljoin

import httpx

from . import providers
from .linkgrab import _guard_url, _pinned_request

log = logging.getLogger("imagehunt")

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ASSET_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")

MIN_BYTES = 8 * 1024
MAX_BYTES = 12 * 1024 * 1024
MIN_W, MIN_H = 360, 240
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_DIMENSION = 8192


async def _bing(cli, q, n):
    r = await cli.get(f"https://www.bing.com/images/async?q={quote(q)}&first=0&count={n}&mmasync=1")
    out = []
    for m in re.findall(r'class="iusc"[^>]*\sm="([^"]+)"', r.text):
        try:
            d = json.loads(_htmlmod.unescape(m))
        except ValueError:
            continue
        if d.get("murl"):
            out.append({"img": d["murl"], "thumb": d.get("turl") or d["murl"],
                        "page": d.get("purl") or "", "from": "bing"})
    return out


async def _baidu(cli, q, n):
    r = await cli.get("https://image.baidu.com/search/acjson?tn=resultjson_com&ipn=rj"
                      f"&word={quote(q)}&ie=utf-8&oe=utf-8&pn=0&rn={n}",
                      headers={"Referer": "https://image.baidu.com/"})
    try:  # 百度返回的 JSON 常带非法转义
        data = json.loads(r.text.replace("\\'", "'"))
    except ValueError:
        return []
    out = []
    for it in data.get("data") or []:
        u = it.get("middleURL") or it.get("thumbURL") or it.get("hoverURL")
        if u:
            out.append({"img": u, "thumb": it.get("thumbURL") or u,
                        "page": it.get("fromURLHost") or "", "from": "baidu"})
    return out


async def _so360(cli, q, n):
    r = await cli.get(f"https://image.so.com/j?q={quote(q)}&src=srp&pn={n}&sn=0")
    try:
        data = r.json()
    except ValueError:
        return []
    out = []
    for it in data.get("list") or []:
        if it.get("img"):
            out.append({"img": it["img"], "thumb": it.get("thumb") or it["img"],
                        "page": it.get("link") or "", "from": "360"})
    return out


async def search(query: str, n: int = 24) -> list:
    """三路并行搜图,去重合并.每项 {img, thumb, page, from}."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": UA_DESKTOP,
                                          "Accept-Language": "zh-CN,zh;q=0.9"}) as cli:
        results = await asyncio.gather(_bing(cli, query, n), _baidu(cli, query, n),
                                       _so360(cli, query, n), return_exceptions=True)
    merged, seen = [], set()
    # 轮转合并:三个引擎的结果交错排,单引擎挂了不影响
    pools = [r for r in results if isinstance(r, list)]
    for i in range(max((len(p) for p in pools), default=0)):
        for p in pools:
            if i < len(p):
                u = p[i]["img"]
                if u not in seen and u.startswith("http"):
                    seen.add(u)
                    merged.append(p[i])
    return merged[:n]


def _looks_image(data: bytes) -> bool:
    return (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
            or data[:6] in (b"GIF87a", b"GIF89a")
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _decode_image(data: bytes, max_pixels: int = MAX_IMAGE_PIXELS):
    """先读图片头并限制像素/帧，再允许 PIL 解压像素数据。"""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                width, height = source.size
                frames = int(getattr(source, "n_frames", 1) or 1)
                if (
                    width <= 0
                    or height <= 0
                    or width > MAX_IMAGE_DIMENSION
                    or height > MAX_IMAGE_DIMENSION
                    or width * height > max_pixels
                    or frames != 1
                ):
                    raise ValueError("图片尺寸、像素量或帧数超过安全限制")
                if (source.format or "").upper() not in {
                    "JPEG", "PNG", "GIF", "WEBP"
                }:
                    raise ValueError("图片格式不受支持")
                source.load()
                return source.convert("RGB").copy()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("图片像素量超过安全限制") from exc


def safe_thumbnail_bytes(data: bytes) -> tuple[bytes, str]:
    """校验并重编码远端缩略图，绝不反射上游 Content-Type。"""
    image = _decode_image(data)
    image.thumbnail((1600, 1600))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=85, optimize=True)
    return output.getvalue(), "image/jpeg"


def _process(data: bytes) -> bytes:
    """PIL 校验尺寸 + 占位图检测 + 统一转 JPG(webp 也转成公众号能收的格式),超宽压到 1280."""
    im = _decode_image(data)
    if im.width < MIN_W or im.height < MIN_H:
        raise ValueError(f"图太小({im.width}x{im.height})")
    # 防盗链占位图/纯图标检测:真实照片色彩丰富(64×64 采样通常 >3000 色,灰度方差 >30),
    # 占位图是"白底+图标+一行字"(实测 ~870 色/方差 17),双指标其一不达标就拒收
    small = im.convert("RGB").resize((64, 64))
    colors = small.getcolors(64 * 64)
    ncolors = len(colors) if colors else 4096
    g = list(small.convert("L").tobytes())
    mean = sum(g) / len(g)
    std = (sum((x - mean) ** 2 for x in g) / len(g)) ** 0.5
    if ncolors < 1000 or std < 22:
        raise ValueError(f"疑似防盗链占位图/图标(色彩{ncolors}/方差{std:.0f}),弃用")
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if im.width > 1280:
        im = im.resize((1280, round(im.height * 1280 / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


async def fetch_public_bytes(url: str, headers: dict = None, max_bytes: int = MAX_BYTES,
                             timeout: float = 25, client: httpx.AsyncClient = None):
    """按跳复验公网目标并限量读取，返回 ``(body, response_headers)``。

    关闭 httpx 自动跳转，避免攻击者用公网 302 把服务端引向云元数据或内网；
    同时不把未知 Content-Length 的响应一次性读进内存。
    """
    own_client = client is None
    cli = client or httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, headers=headers or {},
        limits=httpx.Limits(max_keepalive_connections=0))
    cur = url
    try:
        for _ in range(6):
            addresses = await _guard_url(cur)
            target, pinned_headers, extensions = _pinned_request(cur, addresses)
            request_headers = dict(headers or {})
            request_headers.update(pinned_headers)
            async with cli.stream(
                    "GET", target, headers=request_headers,
                    extensions=extensions) as response:
                location = response.headers.get("location")
                if response.is_redirect and location:
                    cur = urljoin(cur, location)
                    continue
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length:
                    try:
                        declared = int(length)
                    except ValueError:
                        declared = 0
                    if declared > max_bytes:
                        raise ValueError("远程文件超过大小限制")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError("远程文件超过大小限制")
                return bytes(body), dict(response.headers)
        raise ValueError("远程地址重定向次数过多")
    finally:
        if own_client:
            await cli.aclose()


async def fetch_image(url: str, referer: str = None) -> bytes:
    """下载并校验一张图,返回统一处理后的 JPG 二进制;不合格抛 ValueError."""
    headers = {"User-Agent": UA_DESKTOP}
    if referer:
        headers["Referer"] = referer
    data, _ = await fetch_public_bytes(
        url, headers=headers, max_bytes=MAX_BYTES, timeout=25)
    if len(data) < MIN_BYTES or len(data) > MAX_BYTES or not _looks_image(data):
        raise ValueError("不是有效图片或大小不合适")
    return await asyncio.to_thread(_process, data)


_REFERER = {"baidu": "https://image.baidu.com/", "360": "https://image.so.com/",
            "bing": "https://www.bing.com/"}


def _origin(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    return f"{p.scheme}://{p.hostname}/" if p.hostname else ""


def _ref_for(c: dict) -> str:
    """必应给的是原站直链→Referer 用图片自己的站(过原站防盗链);百度/360缩略图用引擎站."""
    if c.get("from") in ("baidu", "360"):
        return _REFERER[c["from"]]
    return _origin(c.get("img") or "")


async def _grab_first_ok(cands: list, want: int) -> list:
    """按候选顺序逐个试下载,拿满 want 张为止.返回 [(jpg_bytes, cand)]."""
    got = []
    for c in cands:
        if len(got) >= want:
            break
        try:
            data = await fetch_image(c["img"], referer=_ref_for(c))
            got.append((data, c))
        except Exception:
            continue
    return got


def _save_job_file(job_id: int, name: str, data: bytes) -> str:
    d = os.path.join(ASSET_DIR, f"job{job_id}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "wb") as f:
        f.write(data)
    return f"/files/job{job_id}/{name}"


async def _gen_queries(title: str, plan: list, industry: str = "") -> list:
    """让文本模型把配图点位翻成图片搜索关键词(名词化短语,不要句子)."""
    slots = "\n".join(f"- {im.get('slot', '')}:{im.get('desc', '')}" for im in plan)
    try:
        r = await providers.call_text(
            5,
            f"内容标题:《{title}》 行业:{industry or '通用'}\n配图点位:\n{slots}\n"
            f"为每个点位写一个中文图片搜索关键词(2-6个词的名词短语,像在百度图片里搜真实照片,"
            f"避免抽象词)。只输出 JSON:{{\"queries\":[\"关键词1\",...]}},数量与点位一致。",
            timeout=120)
        from . import llm
        qs = llm.extract_json(r["text"]).get("queries") or []
        qs = [str(q).strip()[:30] for q in qs if str(q).strip()]
        if qs:
            return (qs + [qs[-1]] * len(plan))[:len(plan)]
    except Exception as exc:
        log.warning(
            "关键词生成失败，退回标题检索 error_type=%s",
            type(exc).__name__,
        )
    base = re.sub(r"[《》【】!?,。:;、\s]+", " ", title).strip()[:20] or "配图"
    return [base] * len(plan)


async def hunt_for_job(job_id: int, version: int, title: str, plan: list,
                       want: int, industry: str = "", progress=None) -> list:
    """流水线用:按配图点位全网抓真实图,存进工单素材,返回 images 列表."""
    progress = progress or (lambda *a: None)
    plan = (plan or [])[:max(1, want)]
    while len(plan) < want:
        plan = plan + [{"slot": f"配图{len(plan) + 1}", "desc": title}]
    progress("tool", f"🔎 真实图模式:为 {len(plan)} 个配图点位生成检索词…")
    queries = await _gen_queries(title, plan, industry)
    images = []
    for i, (im, q) in enumerate(zip(plan, queries)):
        progress("tool", f"全网搜图 {i + 1}/{len(plan)}:「{q}」…")
        try:
            cands = await search(q, 15)
            got = await _grab_first_ok(cands, 1)
        except Exception:
            progress("error", f"「{q}」搜图失败，请稍后重试")
            continue
        if not got:
            progress("error", f"「{q}」没抓到能用的图")
            continue
        data, c = got[0]
        f = _save_job_file(job_id, f"media_v{version}_real_{i}.jpg", data)
        images.append({"slot": im.get("slot", f"配图{i + 1}"), "desc": im.get("desc", ""),
                       "platform": "通用", "file": f, "engine": "真实图·全网抓取",
                       "query": q, "source": c.get("page") or c.get("img", "")})
        progress("done", f"第 {i + 1} 张真实图入库(来自{c['from']})")
    return images
