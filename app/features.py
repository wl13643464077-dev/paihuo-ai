"""平台级功能开关 + 合规配置(第 3 期).

- 高风险功能(代管 Cookie 自动发帖 / 全网抓图 / 视频链接下载转文字 / 搜索引擎网页抓取)
  默认关闭；平台管理员(root)可在后台按平台整体打开，也可只给某个企业单独开/关。
- 存储全部走 app_setting 键值，不改表结构：
    feature:<key>            平台级 "1"/"0"，没有记录 = 用默认值
    feature:<key>:<tenant>   企业级覆盖 "1"/"0"，没有记录 = 跟随平台
    compliance:<name>        公开链接有效期等可调参数(整数)
    ai_label:<tenant>        企业的「AI 生成内容标识」设置(JSON)
- 关闭时统一抛 FeatureDisabled(带给老板看的大白话原因)，接口层转成 403。
"""
import json
import time

from . import auth, db


class FeatureDisabled(PermissionError):
    """功能已被平台关闭；str(exc) 就是给老板看的原因。"""

    status_code = 403

    def __init__(self, key: str, message: str = ""):
        self.key = key
        super().__init__(message or off_hint(key))


# 每个开关:名称、默认值、风险说明(后台显示)、关闭时给老板的说明
FEATURES = {
    "matrix_autopub": {
        "name": "矩阵自动发布(代管小红书/抖音登录态)",
        "default": False,
        "risk": ("需要老板把小红书/抖音的登录 Cookie 交给平台代管，服务器用无头浏览器替他发帖，"
                 "还会调用平台未公开的接口查账号。这违反平台用户协议，账号可能被限流或封禁；"
                 "Cookie 一旦泄露等于账号被人接管，平台要承担保管责任。"),
        "off_hint": ("平台已关闭「自动代发」(需要把您的平台登录态交给我们保管，有封号和泄露风险)。"
                     "请用「🪄 半自动发布」:一键复制标题正文、下载素材包、打开平台发布页，自己点发布就行。"),
    },
    "imagehunt": {
        "name": "全网抓图配图(百度/360/必应图片)",
        "default": False,
        "risk": ("从图片搜索引擎抓取别人的图片直接用在商用内容里。网上的图片绝大多数有版权，"
                 "被原作者或图库公司发现可能要求赔偿，平台和商家都有连带风险。"),
        "off_hint": ("平台已关闭「全网抓图」(网上的图片大多有版权，直接商用可能被索赔)。"
                     "请用 AI 生成配图，或上传您自己拍的照片。"),
    },
    "linkgrab_video": {
        "name": "视频链接转文字(下载抖音/快手/B站视频)",
        "default": False,
        "risk": ("用下载工具抓取抖音/快手/B站等平台上别人的视频音频再转成文字，"
                 "违反平台协议，也可能侵犯原作者的著作权。"),
        "off_hint": ("平台已关闭「视频链接转文字」(下载别人平台上的视频有侵权和违反平台规则的风险)。"
                     "请上传你自己的视频/音频文件，或把视频文案直接复制粘贴进来改写。"),
    },
    "lead_search_scrape": {
        "name": "线索雷达·搜索引擎网页抓取(DuckDuckGo/必应)",
        "default": False,
        "risk": ("线索雷达直接抓取 DuckDuckGo/必应的搜索结果网页，这违反搜索引擎的使用条款，"
                 "服务器 IP 可能被封，结果也不稳定。关闭后改走正规的联网检索服务。"),
        "off_hint": ("平台已关闭「搜索引擎网页抓取」(直接抓搜索结果页违反搜索引擎的使用条款)，"
                     "线索雷达改用正规联网检索服务查找公开帖子，结果可能少一些。"),
    },
}

# 可调参数:名称 → (默认, 最小, 最大, 说明)
CONFIG = {
    "pubfile_ttl_days": (7, 1, 90, "公众号素材图公开链接有效天数"),
    "pubfile_legacy_days": (7, 0, 90, "旧版永久图片链接的过渡期(天)，过了就失效"),
    "pub_link_ttl_hours": (6, 1, 72, "数字人照片/声音给视频厂商拉取的临时链接有效小时数"),
    "pub_cleanup_days": (30, 1, 365, "数字人公开素材目录里超过多少天的临时文件自动删除"),
}


def _flag(value):
    if value in ("1", 1, True):
        return True
    if value in ("0", 0, False):
        return False
    return None


def _current_tenant():
    """只有真实登录上下文才有企业；后台任务没有上下文时只看平台开关。"""
    user = auth.current() or {}
    try:
        tid = int(user.get("tenant_id") or 0)
    except (TypeError, ValueError):
        tid = 0
    return tid if tid > 0 else None


def platform_state(key: str) -> bool:
    spec = FEATURES[key]
    value = _flag(db.get_setting(f"feature:{key}"))
    return spec["default"] if value is None else value


def tenant_override(key: str, tenant_id: int):
    if not tenant_id:
        return None
    return _flag(db.get_setting(f"feature:{key}:{int(tenant_id)}"))


def is_enabled(key: str, tenant_id: int = None) -> bool:
    if key not in FEATURES:
        raise KeyError(key)
    tid = tenant_id if tenant_id else _current_tenant()
    override = tenant_override(key, tid) if tid else None
    return platform_state(key) if override is None else override


def off_hint(key: str) -> str:
    return (FEATURES.get(key) or {}).get("off_hint") or "该功能已被平台关闭"


def require(key: str, tenant_id: int = None) -> None:
    if not is_enabled(key, tenant_id):
        raise FeatureDisabled(key)


def set_platform(key: str, enabled) -> None:
    if key not in FEATURES:
        raise KeyError(key)
    value = None if enabled is None else ("1" if enabled else "0")
    db.set_setting(f"feature:{key}", value)


def set_tenant(key: str, tenant_id: int, enabled) -> None:
    """企业级覆盖；enabled=None 表示取消覆盖、跟随平台。只允许 root 调用(接口层把关)。"""
    if key not in FEATURES:
        raise KeyError(key)
    tid = int(tenant_id)
    if tid < 1:
        raise ValueError("企业编号无效")
    value = None if enabled is None else ("1" if enabled else "0")
    db.set_setting(f"feature:{key}:{tid}", value)


def public_flags(tenant_id: int = None) -> dict:
    """给前端:当前企业每个开关是否可用 + 关闭时的说明。"""
    return {
        key: {"enabled": is_enabled(key, tenant_id), "hint": spec["off_hint"],
              "name": spec["name"]}
        for key, spec in FEATURES.items()
    }


def admin_overview() -> dict:
    """后台用:每个开关的平台状态、默认值、风险说明和企业级覆盖。"""
    overrides = {key: [] for key in FEATURES}
    for row in db.q("SELECT key,value FROM app_setting WHERE key LIKE 'feature:%:%'"):
        parts = str(row.get("key") or "").split(":")
        if len(parts) != 3 or parts[1] not in FEATURES:
            continue
        flag = _flag(row.get("value"))
        if flag is None:
            continue
        try:
            tid = int(parts[2])
        except ValueError:
            continue
        overrides[parts[1]].append({"tenant_id": tid, "enabled": flag})
    return {
        "features": [
            {"key": key, "name": spec["name"], "default": spec["default"],
             "enabled": platform_state(key), "risk": spec["risk"],
             "off_hint": spec["off_hint"],
             "overrides": sorted(overrides[key], key=lambda item: item["tenant_id"])}
            for key, spec in FEATURES.items()
        ],
        "config": [
            {"key": name, "value": config_int(name), "default": spec[0],
             "min": spec[1], "max": spec[2], "label": spec[3]}
            for name, spec in CONFIG.items()
        ],
    }


# ---------------- 可调参数 ----------------
def config_int(name: str) -> int:
    default, low, high, _label = CONFIG[name]
    raw = db.get_setting(f"compliance:{name}")
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def set_config(values: dict) -> dict:
    saved = {}
    for name, raw in (values or {}).items():
        if name not in CONFIG:
            continue
        default, low, high, label = CONFIG[name]
        if raw in (None, ""):
            db.set_setting(f"compliance:{name}", None)
            saved[name] = default
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"「{label}」要填整数") from None
        if value < low or value > high:
            raise ValueError(f"「{label}」只能在 {low}-{high} 之间")
        db.set_setting(f"compliance:{name}", str(value))
        saved[name] = value
    return saved


def legacy_since(now: float = None) -> float:
    """旧版永久签名链接的过渡期起点:第一次启动新版本时记下，之后不再变。"""
    raw = db.get_setting("compliance:pubfile_legacy_since")
    try:
        if raw is not None:
            return float(raw)
    except (TypeError, ValueError):
        pass
    stamp = float(now if now is not None else time.time())
    db.execute(
        "INSERT OR IGNORE INTO app_setting(key,value,updated_at) VALUES(?,?,?)",
        ("compliance:pubfile_legacy_since", str(stamp), time.time()),
    )
    raw = db.get_setting("compliance:pubfile_legacy_since")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return stamp


# ---------------- AI 生成内容标识 ----------------
DEFAULT_AI_LABEL = "本内容由 AI 辅助生成"
AI_LABEL_PLATFORMS = ("小红书", "抖音", "视频号", "公众号", "微博", "知乎", "B站", "快手")


def ai_label_conf(tenant_id: int) -> dict:
    raw = db.jloads(db.get_setting(f"ai_label:{int(tenant_id)}"), {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    text = str(raw.get("text") or "").strip()[:30] or DEFAULT_AI_LABEL
    off = raw.get("off_platforms") if isinstance(raw.get("off_platforms"), list) else []
    return {
        "enabled": raw.get("enabled", True) is not False,
        "text": text,
        "off_platforms": [str(p)[:10] for p in off if str(p).strip()][:20],
        "default_text": DEFAULT_AI_LABEL,
        "platforms": list(AI_LABEL_PLATFORMS),
    }


def save_ai_label_conf(tenant_id: int, body: dict) -> dict:
    body = body if isinstance(body, dict) else {}
    current = ai_label_conf(tenant_id)
    text = body.get("text", current["text"])
    if not isinstance(text, str) or len(text.strip()) > 30:
        raise ValueError("标识文案最多 30 个字")
    off = body.get("off_platforms", current["off_platforms"])
    if not isinstance(off, list):
        raise ValueError("关闭标识的平台格式无效")
    stored = {
        "enabled": bool(body.get("enabled", current["enabled"])),
        "text": text.strip() or DEFAULT_AI_LABEL,
        "off_platforms": sorted({str(p).strip()[:10] for p in off if str(p).strip()})[:20],
    }
    db.set_setting(f"ai_label:{int(tenant_id)}",
                   json.dumps(stored, ensure_ascii=False))
    return ai_label_conf(tenant_id)


def ai_label_for(tenant_id: int, platform: str = None) -> str:
    """该企业(及平台)要不要加标识；返回标识文案，空串表示不加。"""
    conf = ai_label_conf(tenant_id)
    if not conf["enabled"]:
        return ""
    if platform and platform in conf["off_platforms"]:
        return ""
    return conf["text"]


def append_label_text(text: str, label: str) -> str:
    """文案末尾追加标识；已带同样标识不重复加。"""
    text = text or ""
    if not label or text.rstrip().endswith(label):
        return text
    return f"{text.rstrip()}\n\n{label}" if text.strip() else label


def label_packs(packs: list, tenant_id: int) -> list:
    """发布包(各平台拿来即发的文案)末尾加标识；按平台可关。"""
    conf = ai_label_conf(tenant_id)
    out = []
    for pack in packs or []:
        platform = (pack or {}).get("platform") if isinstance(pack, dict) else None
        label = (conf["text"] if conf["enabled"] and platform not in conf["off_platforms"]
                 else "")
        if label and isinstance(pack, dict):
            pack = {**pack, "body": append_label_text(pack.get("body") or "", label),
                    "ai_label": label}
        out.append(pack)
    return out


def label_markdown(md: str, tenant_id: int) -> str:
    """导出文件(docx/pdf/md)末尾加标识。"""
    label = ai_label_for(tenant_id)
    if not label or (md or "").rstrip().endswith(f"*{label}*"):
        return md or ""
    return f"{(md or '').rstrip()}\n\n---\n\n*{label}*\n"


VIDEO_LINE = 12  # 成片结尾卡每行 12 个字(与 textvideo 结尾卡的换行规则一致)


def label_end_text(tenant_id: int, end_text: str = "") -> str:
    """成片结尾卡文字:原有引导语(最多两行)+ 最后一行 AI 标识。"""
    label = ai_label_for(tenant_id, None)
    original = (end_text or "")[:40]
    if not label:
        return original
    label = label.replace(" ", "")[:VIDEO_LINE]
    source = original or "喜欢这条就点赞关注\n下条更精彩"
    if label in source.replace(" ", "").replace("　", "").replace("\n", ""):
        return original
    lines = []
    for segment in source.split("\n"):
        segment = "".join(segment.split())
        lines += [segment[i:i + VIDEO_LINE] for i in range(0, len(segment), VIDEO_LINE)]
    lines = [line for line in lines if line][:2]
    # 结尾卡按固定字数切行，用全角空格补齐，保证标识单独占最后一行
    return ("".join(line.ljust(VIDEO_LINE, "　") for line in lines) + label)[:40]
