"""门店数字人一条龙：门店场景照 → 72 小时行业热点选题 → 口播稿 → 数字人出片 → 自动剪辑 + 三平台发布包。

方法来自《内容流水线 v3.1》：
- 封面人物场景生图（gen_person_scene）：用老板真实照片做参考，保住脸和身形，换到自家门头/店里；
- 选题雷达（01-topic-radar）：严格 72 小时、必须有可核验热度证据，来源写「媒体名+日期」；
- 抖音基准模式 C：真人常在，证据包装成卡片，单张证据卡 ≤3 秒；
- 发布包体检（platform_check）：公开文案零网址、零联系方式、零「私信领取/扣1/加V」，AI 只出草稿不代发。

所有结论与成片都交老板确认后由老板本人发布。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone

from . import avatar, billing, db, providers

log = logging.getLogger("storestudio")
TZ = timezone(timedelta(hours=8))

W, H = 1080, 1920
MAX_SCRIPT_CHARS = {15: 120, 30: 240, 60: 480}

SCENES = {
    "door": {
        "label": "站在门头前",
        "prompt": "He stands in front of his own shop entrance shown in the second reference photo, "
                  "the real storefront and signboard behind him exactly as in that photo, daytime, "
                  "welcoming customers with a confident smile, waist-up.",
    },
    "inside": {
        "label": "站在店里",
        "prompt": "He stands inside his own shop shown in the second reference photo, keeping that "
                  "interior's real layout, colours and decoration behind him, warm business-hours "
                  "lighting, talking to camera like a shop owner, waist-up.",
    },
    "counter": {
        "label": "站在收银台/出餐口",
        "prompt": "He stands behind the counter of his own shop shown in the second reference photo "
                  "(cashier, bar or serving window, matching that shop's style), leaning slightly "
                  "forward and talking to camera, waist-up.",
    },
}

IDENTITY = (
    "The first reference photo is the shop owner. Keep his or her face, facial features, hairstyle, "
    "skin tone and body shape exactly the same; do not beautify into a different person. "
    "The second reference photo is the owner's real shop. New photo: "
)
TAIL = (
    " The owner is the only person in focus; no other recognisable faces. Vertical 2:3 portrait, "
    "the owner in the upper-middle of the frame, face fully visible and facing the camera, mouth "
    "closed and natural (this photo will drive a talking digital human). Photorealistic, natural "
    "skin, commercial photography, no added text, no watermark, do not invent extra brand logos."
)

# 审核红线（公开文案）：网址、联系方式、导流话术、广告法绝对化用语
_REDLINES = (
    (re.compile(r"https?://|www\.|\.com\b|\.cn\b", re.I), "含网址"),
    (re.compile(r"1[3-9]\d{9}|\d{3,4}-\d{7,8}"), "含电话号码"),
    (re.compile(r"微信|vx|v信|加v|加V|weixin|qq群|QQ群|二维码", re.I), "含联系方式/导流"),
    (re.compile(r"私信领取|私信我领|扣1|扣个1|评论区领取|主页领取"), "含导流话术"),
    (re.compile(r"最好|最佳|(?:全国|全网|行业|销量|排名|全市|全城)第一|第一名|第一品牌|顶级|国家级|全网最|100%|百分百"),
     "含广告法绝对化用语"),
)


class StudioError(ValueError):
    """面向老板的可读错误（不含供应商细节）。"""


def redline_issues(text: str) -> list[str]:
    text = str(text or "")
    return [label for pattern, label in _REDLINES if pattern.search(text)]


def _clean_public(text: str, limit: int) -> str:
    text = re.sub(r"https?://\S+", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def price_points(action: str, units: int = 1) -> int:
    price = (billing.prices() or {}).get(action) or billing.DEFAULT_PRICES.get(action) or {"points": 1}
    return int(float(price.get("points") or 0) * max(1, units))


# ---------- ① 门店场景照 ----------
def _png_ref(path: str, side: int = 1024) -> bytes:
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((side, side))
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()


def scene_prompt(scene: str, industry: str = "", note: str = "") -> str:
    spec = SCENES.get(scene) or SCENES["door"]
    extra = []
    if industry:
        extra.append(f"The shop is a {industry} business in China.")
    note = _clean_public(note, 80)
    if note:
        extra.append(f"Owner's wish (Chinese): {note}.")
    return IDENTITY + spec["prompt"] + (" " + " ".join(extra) if extra else "") + TAIL


def _to_jpeg(data: bytes) -> bytes:
    """生图返回的 PNG 约 2.5MB，转成 JPEG（约十分之一），老板手机上秒开。"""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
        return out.getvalue()


async def scene_photos(tid: int, portrait_name: str, store_name: str, scene: str,
                       industry: str = "", note: str = "", n: int = 2,
                       user: dict | None = None) -> list[dict]:
    """老板人像 + 门店照 → n 张「老板在自家店」场景照，存进数字人照片库。"""
    if scene not in SCENES:
        raise StudioError("场景只能选：门头前 / 店里 / 收银台")
    portrait = await db.arun(avatar.asset_path, portrait_name, {"photo"}, tid)
    store = await db.arun(avatar.asset_path, store_name, {"store"}, tid)
    portrait_png, store_png = await asyncio.gather(
        asyncio.to_thread(_png_ref, portrait), asyncio.to_thread(_png_ref, store))
    prompt = scene_prompt(scene, industry, note)

    async def one() -> bytes:
        return await providers.edit_image(
            5, prompt, portrait_png, size="1024x1536", timeout=300,
            extra_images=[store_png])

    results = await asyncio.gather(*(one() for _ in range(max(1, min(n, 3)))),
                                   return_exceptions=True)
    images = [r for r in results if isinstance(r, (bytes, bytearray))]
    if not images:
        first = next((r for r in results if isinstance(r, BaseException)), None)
        raise providers.ProviderError("场景照生成失败，点数已退回，请换张更清晰的门店照再试") from first
    saved = []
    for data in images:
        jpeg = await asyncio.to_thread(_to_jpeg, bytes(data))
        await asyncio.to_thread(avatar.validate_upload_media, jpeg, ".jpg", "photo")
        pub = await asyncio.to_thread(avatar.store_uploaded_asset, jpeg, ".jpg", "photo", tid)
        # 场景照由已授权的本人肖像合成，沿用并补记授权留痕（记录操作人）
        await db.arun(avatar.record_consent, tid, pub["name"], "photo", "scene_compose", user)
        saved.append({"name": pub["name"], "preview": f"/files/avatar-public/{pub['name']}",
                      "scene": scene})
    return saved


# ---------- ② 72 小时行业热点选题 ----------
def _hours_ago(published: str, now: datetime) -> float | None:
    """解析模型给的各种日期写法，返回距今小时数；只有日期时按当天 23:59 算（对时效宽一档）。

    支持：2026-10-28[ 18:01] / 2026/10/28 / 2026年10月28日 / 10月28日 / 10-28 /
    今天 / 昨天 / 前天 / N小时前 / N天前。"""
    text = str(published or "").strip()
    if not text:
        return None
    m = re.search(r"(\d+)\s*(?:个)?小时前", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)\s*天前", text)
    if m:
        return float(m.group(1)) * 24
    for word, days in (("今天", 0), ("今日", 0), ("昨天", 1), ("昨日", 1), ("前天", 2)):
        if word in text:
            return float(days * 24)
    year = month = day = None
    hour = minute = None
    m = re.search(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text)
    if m:
        year, month, day = (int(x) for x in m.groups())
    else:
        m = re.search(r"(?<!\d)(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?!\d)", text)
        if m:
            year, month, day = now.year, int(m.group(1)), int(m.group(2))
    if year is None:
        return None
    t = re.search(r"(\d{1,2}):(\d{2})", text[m.end():] if m else "")
    if t:
        hour, minute = int(t.group(1)), int(t.group(2))
    try:
        when = datetime(year, month, day, hour if hour is not None else 23,
                        minute if minute is not None else 59, tzinfo=TZ)
    except ValueError:
        return None
    if when - now > timedelta(days=2) and year == now.year and not re.search(r"20\d{2}", text):
        when = when.replace(year=year - 1)  # 「12月31日」在 1 月看是去年
    return max(0.0, (now - when).total_seconds() / 3600)


def _topic_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("topics", "picks", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _topic_items(value)
                if nested:
                    return nested
    return []


def normalize_topics(data, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(TZ)
    out, rejected = [], {"no_source": 0, "no_date": 0, "too_old": 0}
    for raw in _topic_items(data):
        if not isinstance(raw, dict):
            continue
        title = _clean_public(raw.get("title"), 60)
        heat = _clean_public(raw.get("heat") or raw.get("why") or raw.get("evidence"), 120)
        source = _clean_public(raw.get("source") or raw.get("channel"), 60)
        published = _clean_public(raw.get("published") or raw.get("date") or raw.get("time")
                                  or raw.get("publish_time"), 30)
        if not (title and heat and source):
            rejected["no_source"] += 1
            continue  # 没有可核验热度证据和出处的不收
        hours = None
        for text in (published, source, heat):
            hours = _hours_ago(text, now)
            if hours is not None:
                break
        if hours is None:
            rejected["no_date"] += 1
            continue
        if hours > 72:
            rejected["too_old"] += 1
            continue
        try:
            score = max(0, min(100, int(raw.get("score") or 0)))
        except (TypeError, ValueError):
            score = 0
        keywords = [_clean_public(k, 16) for k in (raw.get("keywords") or []) if isinstance(k, str)]
        out.append({
            "title": title, "heat": heat, "source": source, "published": published,
            "hours_ago": round(hours), "angle": _clean_public(raw.get("angle"), 120),
            "hook": _clean_public(raw.get("hook"), 60), "score": score,
            "keywords": [k for k in keywords if k][:3] or [title[:12]],
        })
    if any(rejected.values()):
        log.info("studio topics kept=%s rejected=%s", len(out), rejected)
    out.sort(key=lambda t: (-t["score"], t["hours_ago"]))
    return out[:6]


async def hot_topics(tid: int, industry: str, store_note: str = "") -> dict:
    from . import growth
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    since = (now - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M")
    industry = _clean_public(industry, 20) or "实体门店"
    store_note = _clean_public(store_note, 120)
    prompt = f"""现在是 {now.strftime('%Y-%m-%d %H:%M')}（北京时间）。你给一位「{industry}」门店老板找短视频选题。
【门店情况】{store_note or '(老板没写，按该行业普通门店理解)'}
用 WebSearch 检索 {since} 之后（严格 72 小时内）发生或升温的、和「{industry}」老板相关的热点：
行业新闻、监管新规、平台政策（美团/抖音/小红书等）、消费趋势、明星/网红同款、节日节点、社会热议事件。
【硬门槛】
- 只收 72 小时内的：published 写原始首发日期（YYYY-MM-DD），72 小时前的旧闻一律不要；
- heat 必须写真实检索到的热度证据（哪个平台/媒体 + 现象或数据），不许写「近期很火」；
- source 写「媒体名+日期」，不写网址；单方说法写「某某称」；
- angle 要落到这家门店：老板拿这件事能讲出什么判断、给顾客什么好处或提醒；
- hook 是口播第一句：冲突或结论开头，禁止「据××报道」开头；
- keywords 给 3 个适合搜新闻配图的短词；
- 不碰政治、灾难消费、未经证实的食品安全传言、点名攻击同行。
挑 6 个，按「门店能接得住 + 顾客爱看 + 时效」打分 score（0-100）。
只输出 JSON：{{"topics":[{{"title":"选题标题","heat":"热度证据","source":"媒体名+日期","published":"YYYY-MM-DD","angle":"门店切入角度","hook":"第一句","keywords":["词1","词2","词3"],"score":80}}],"scan_note":"本次扫到的热点面，60字"}}"""
    brief = (f"日期：{today}；行业：{industry}。检索最近 72 小时与该行业门店经营相关的公开热点、"
             "行业新闻、平台政策和消费趋势，记录媒体名与首发日期。")
    result = await growth._call_toolbox_employee_json(
        0, prompt, web=True, timeout=600, research_brief=brief)
    data = result.get("data") if isinstance(result, dict) else None
    topics = normalize_topics(data, now)
    if not topics:
        if not _topic_items(data):
            log.warning("studio topics empty payload type=%s", type(data).__name__)
            raise providers.ProviderError("热点扫描这次没返回结果，点数已退回，请再点一次")
        raise providers.ProviderError("72 小时内没找到能核实出处的行业热点，点数已退回；可稍后再扫或自己写选题")
    return {"industry": industry, "date": today, "topics": topics,
            "scan_note": _clean_public((data or {}).get("scan_note"), 120)}


# ---------- ③ 口播稿 ----------
async def write_script(tid: int, topic: dict, industry: str, store_note: str,
                       duration: int) -> dict:
    from . import growth
    duration = duration if duration in MAX_SCRIPT_CHARS else 30
    limit = MAX_SCRIPT_CHARS[duration]
    target = {15: "70-100", 30: "150-210", 60: "300-420"}[duration]
    topic = {k: _clean_public(v, 160) for k, v in (topic or {}).items() if isinstance(v, str)}
    if not topic.get("title"):
        raise StudioError("先选一个选题")
    prompt = f"""你给「{_clean_public(industry, 20) or '实体门店'}」老板写一条 {duration} 秒抖音口播稿，老板本人出镜，站在自家店里讲。
【门店】{_clean_public(store_note, 120) or '(未填写)'}
【选题】{topic.get('title')}
【热度证据】{topic.get('heat', '')}（出处：{topic.get('source', '')}）
【切入角度】{topic.get('angle', '')}
【建议第一句】{topic.get('hook', '')}
【结构】冲突/结论开头 → 这件事是什么（一句，带出处「媒体名+日期」）→ 对顾客/同行意味着什么 → 我们店怎么做（2-3 条具体做法）→ 一个互动问题收尾。
【要求】{target} 字，口语短句，像老板当面聊天；不写网址、电话、微信，不说「私信领取/扣1/加V」，不用「最/第一/顶级」等绝对化词；
单方说法写「某某称」；不编造数据和案例；不贬低同行。
只输出 JSON：{{"script":"口播全文","title":"视频标题（≤22字）","cover_title":"封面大字（≤14字，可用｜分两行）"}}"""
    result = await growth._call_toolbox_employee_json(3, prompt, timeout=300)
    data = result.get("data") if isinstance(result, dict) else {}
    script = re.sub(r"\s+", "", str((data or {}).get("script") or ""))
    script = re.sub(r"https?://\S+", "", script)[:limit]
    if len(script) < 30:
        raise providers.ProviderError("口播稿没写出来，点数已退回，请重试")
    return {
        "script": script,
        "title": _clean_public((data or {}).get("title"), 30) or topic["title"][:22],
        "cover_title": _clean_public((data or {}).get("cover_title"), 20) or topic["title"][:14],
        "issues": redline_issues(script),
    }


# ---------- ④ 剪辑 + 发布包 ----------
def studio_key(job_id: int) -> str:
    return f"studio:{int(job_id)}"


def studio_state(job_id: int) -> dict:
    return db.jloads(db.get_setting(studio_key(job_id)), {}) or {}


def _save_state(job_id: int, state: dict) -> None:
    db.set_setting(studio_key(job_id), json.dumps(state, ensure_ascii=False))


def font_path() -> str:
    for path in (
        os.environ.get("CONTENTCREW_CJK_FONT") or "",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ):
        if path and os.path.isfile(path):
            return path
    raise StudioError("服务器缺少中文字体，无法剪辑")


def split_sentences(script: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])|(?<=[，,])(?=.{14,})", str(script or ""))
    out, buf = [], ""
    for part in parts:
        buf += part.strip()
        if len(buf) >= 6:
            out.append(buf)
            buf = ""
    if buf:
        if out:
            out[-1] += buf
        else:
            out.append(buf)
    return [s for s in out if s]


def _wrap_line(text: str, width: int) -> str:
    """按行数均分换行，避免最后一行只剩一两个字。"""
    text = re.sub(r"[，。！？,.!?；;：:]$", "", text)
    if len(text) <= width:
        return text
    lines = -(-len(text) // width)
    step = -(-len(text) // lines)
    return "\n".join(text[i:i + step] for i in range(0, len(text), step))


def subtitle_timeline(script: str, duration: float) -> list[tuple[float, float, str]]:
    sentences = split_sentences(script)
    total = sum(len(s) for s in sentences) or 1
    t, out = 0.0, []
    for s in sentences:
        d = duration * len(s) / total
        out.append((round(t, 2), round(min(duration, t + d), 2), s))
        t += d
    return out


def broll_windows(timeline: list, count: int, duration: float) -> list[tuple[float, float]]:
    """证据卡出现时段：避开开头 2 秒钩子和结尾 2 秒，每张 ≤2.8 秒，均匀落在句首。"""
    starts = [a for a, _, _ in timeline if 2.0 <= a <= duration - 4.0]
    if not starts or count <= 0:
        return []
    step = max(1, len(starts) // count)
    picked = starts[::step][:count]
    return [(a, round(min(a + 2.8, duration - 1.5), 2)) for a in picked]


def _cite_window(timeline: list, source: str, duration: float):
    """口播里提到出处（媒体名）的那一句，正好亮出原文引用卡。"""
    name = re.split(r"[\s+·,，0-9-]", str(source or "").strip())[0][:4]
    if len(name) < 2:
        return None
    for a, b, text in timeline:
        if name[:2] in text and a >= 1.0:
            return (a, round(min(a + max(2.8, b - a), duration - 1.0), 2))
    return None


def _place_cards(broll: list, timeline: list, windows: list, studio: dict,
                 duration: float) -> list:
    """引用卡跟着「提到出处」那句；门店实拍跟着「我们店/门口/明档」那句；其余按均匀时段。"""
    fixed = {}
    for i, card in enumerate(broll):
        if card["path"].endswith("quote.png"):
            w = _cite_window(timeline, (studio.get("topic") or {}).get("source", ""), duration)
        elif card["path"].endswith("store.png"):
            w = next(((a, round(min(a + 2.8, duration - 1.0), 2)) for a, b, t in timeline
                      if a >= 3.0 and re.search(r"我们店|店里|门口|明档|进门|本店|咱店", t)), None)
        else:
            w = None
        if w and all(w[1] <= o[0] or w[0] >= o[1] for o in fixed.values()):
            fixed[i] = w
    free = [w for w in windows if all(w[1] <= o[0] or w[0] >= o[1] for o in fixed.values())]
    out = []
    for i in range(len(broll)):
        out.append(fixed.get(i) or (free.pop(0) if free else None))
    return [w for w in out if w]


def _probe_duration(path: str) -> float:
    ffprobe = os.environ.get("CONTENTCREW_FFPROBE_PATH") or shutil.which("ffprobe")
    if not ffprobe:
        raise StudioError("服务器缺少 ffprobe")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=30)
    try:
        return max(1.0, float((out.stdout or "").strip()))
    except ValueError as exc:
        raise StudioError("成片时长读取失败") from exc


def _card_image(src: bytes, dst: str, label: str) -> None:
    """新闻配图做成白边证据卡（含图源小字），尺寸 960 宽。"""
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    with Image.open(io.BytesIO(src)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((920, 620))
        card = Image.new("RGB", (im.width + 40, im.height + 84), "white")
        card.paste(im, (20, 20))
        draw = ImageDraw.Draw(card)
        font = ImageFont.truetype(font_path(), 26)
        draw.text((22, im.height + 32), f"图源：{label}"[:40], fill=(90, 90, 90), font=font)
        card.save(dst, "PNG")


def text_strip(text: str, path: str, *, size: int, color=(255, 255, 255),
               width: int = 14, box: bool = False) -> None:
    """字幕/标题渲染成透明 PNG（不依赖 ffmpeg drawtext，与封面同一字体）。"""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(font_path(), size)
    lines = _wrap_line(text, width).split("\n")[:3]
    pad, gap = 22, int(size * 0.25)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    widths = [probe.textlength(line, font=font) for line in lines]
    h = len(lines) * size + (len(lines) - 1) * gap + pad * 2
    im = Image.new("RGBA", (W, int(h)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    if box:
        bw = int(max(widths) + pad * 2)
        draw.rounded_rectangle([((W - bw) // 2, 0), ((W + bw) // 2, int(h))], radius=18,
                               fill=(0, 0, 0, 110))
    y = pad
    for line, lw in zip(lines, widths):
        draw.text(((W - lw) / 2, y - size * 0.1), line, font=font, fill=color,
                  stroke_width=max(3, size // 12), stroke_fill=(0, 0, 0))
        y += size + gap
    im.save(path, "PNG")


SUB_H, CARD_H = 380, 640  # 字幕轨 / 证据卡轨的固定画布高度（透明底）


def _canvas(path: str, h: int, inner: str | None = None) -> None:
    """把一张素材居中放进 W×h 透明画布；inner 为空则是全透明占位帧。"""
    from PIL import Image
    canvas = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    if inner:
        with Image.open(inner) as im:
            im = im.convert("RGBA")
            im.thumbnail((W - 80, h))
            canvas.alpha_composite(im, ((W - im.width) // 2, (h - im.height) // 2))
    canvas.save(path, "PNG")


def _concat_track(entries: list[tuple[str, float]], list_path: str) -> None:
    """ffmpeg concat 清单：一条轨道 = 一个输入，避免几十个输入拖垮受限进程。"""
    with open(list_path, "w", encoding="utf-8") as fh:
        for path, dur in entries:
            fh.write(f"file '{path}'\nduration {max(0.04, dur):.3f}\n")
        fh.write(f"file '{entries[-1][0]}'\n")


def render_cmd(video: str, cards: list[tuple[str, float, float]], text_dir: str,
               timeline: list, title: str, out: str, duration: float) -> list[str]:
    """模式 C：真人全程在画面，证据卡压在下半屏 ≤2.8 秒；顶部标题常驻，底部逐句字幕。

    只用 4 个输入（原片 / 标题 / 字幕轨 / 证据卡轨），线程数可控，适配服务器的资源上限。"""
    blank_sub = os.path.join(text_dir, "blank-sub.png")
    blank_card = os.path.join(text_dir, "blank-card.png")
    _canvas(blank_sub, SUB_H)
    _canvas(blank_card, CARD_H)
    subs, t = [], 0.0
    for n, (a, b, text) in enumerate(timeline):
        strip = os.path.join(text_dir, f"s{n}-strip.png")
        frame = os.path.join(text_dir, f"s{n}.png")
        text_strip(text, strip, size=60, width=14)
        _canvas(frame, SUB_H, strip)
        if a > t:
            subs.append((blank_sub, a - t))
        subs.append((frame, b - a))
        t = b
    if t < duration:
        subs.append((blank_sub, duration - t))
    track, t = [], 0.0
    for n, (path, a, b) in enumerate(sorted(cards, key=lambda c: c[1])):
        frame = os.path.join(text_dir, f"card{n}-frame.png")
        _canvas(frame, CARD_H, path)
        if a > t:
            track.append((blank_card, a - t))
        track.append((frame, b - a))
        t = b
    track.append((blank_card, max(0.04, duration - t)))
    sub_list = os.path.join(text_dir, "subs.txt")
    card_list = os.path.join(text_dir, "cards.txt")
    _concat_track(subs or [(blank_sub, duration)], sub_list)
    _concat_track(track, card_list)
    title_png = os.path.join(text_dir, "title.png")
    if title:
        text_strip(title, title_png, size=66, color=(255, 209, 102), width=12, box=True)
    else:
        _canvas(title_png, 40)
    concat_in = ["-f", "concat", "-safe", "0", "-protocol_whitelist", "file"]
    args = ["-threads", "2", "-i", video,
            "-loop", "1", "-t", f"{duration:.2f}", "-i", title_png,
            *concat_in, "-i", sub_list,
            *concat_in, "-i", card_list]
    chain = ";".join([
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30[base]",
        "[3:v]fps=30,format=rgba[cardt]",
        "[2:v]fps=30,format=rgba[subt]",
        f"[base][cardt]overlay=x=0:y={int(H * 0.40)}:eof_action=pass[v1]",
        "[v1][1:v]overlay=x=0:y=150:shortest=1[v2]",
        f"[v2][subt]overlay=x=0:y={int(H * 0.76)}:eof_action=pass[vout]",
    ])
    return args + [
        "-filter_complex", chain, "-filter_complex_threads", "1",
        "-map", "[vout]", "-map", "0:a?",
        "-t", f"{duration:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-threads", "2", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out,
    ]


def _run_render(arguments: list[str]) -> None:
    from . import textvideo
    command = textvideo._render_command(arguments)
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            timeout=900)
    if result.returncode != 0:
        log.warning("studio render failed rc=%s %s", result.returncode,
                    (result.stderr or b"")[-400:].decode("utf-8", "ignore"))
        raise StudioError("自动剪辑失败")


def make_covers(photo: str, title: str, out_34: str, out_916: str, ai_label: str = "") -> None:
    """封面 = 真人场景 + 大字报（3:4 与 9:16），字压在下方 40%，不挡脸。"""
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    lines = [x for x in re.split(r"[｜|\n]", title or "") if x.strip()] or [title or ""]
    if len(lines) == 1 and len(lines[0]) > 8:
        t = lines[0]
        lines = [t[: (len(t) + 1) // 2], t[(len(t) + 1) // 2:]]
    with Image.open(photo) as src:
        src = ImageOps.exif_transpose(src).convert("RGB")
        for size, out in (((1080, 1440), out_34), ((1080, 1920), out_916)):
            im = ImageOps.fit(src, size, centering=(0.5, 0.3))
            shade = Image.new("L", size, 0)
            sd = ImageDraw.Draw(shade)
            for y in range(int(size[1] * 0.55), size[1]):
                sd.line([(0, y), (size[0], y)], fill=int(190 * (y - size[1] * 0.55) / (size[1] * 0.45)))
            im = Image.composite(Image.new("RGB", size, (0, 0, 0)), im, shade)
            draw = ImageDraw.Draw(im)
            fsize = 132 if size[1] == 1440 else 140
            font = ImageFont.truetype(font_path(), fsize)
            y = size[1] - 80 - len(lines[:2]) * (fsize + 26)
            for n, line in enumerate(lines[:2]):
                w = draw.textlength(line, font=font)
                color = (255, 209, 102) if n == 0 else (255, 255, 255)
                draw.text(((size[0] - w) / 2, y), line, font=font, fill=color,
                          stroke_width=8, stroke_fill=(0, 0, 0))
                y += fsize + 26
            if ai_label:
                small = ImageFont.truetype(font_path(), 30)
                draw.text((30, 30), ai_label[:16], font=small, fill=(255, 255, 255),
                          stroke_width=3, stroke_fill=(0, 0, 0))
            im.save(out, "JPEG", quality=90)


async def publish_copy(tid: int, studio: dict, script: str) -> dict:
    from . import growth
    prompt = f"""给这条门店老板口播视频写三个平台的发布文案（只存草稿，老板自己点发布）。
【视频标题】{studio.get('title', '')}
【选题】{studio.get('topic', {}).get('title', '')}（出处：{studio.get('topic', {}).get('source', '')}）
【口播稿】{script[:600]}
【平台规则】抖音：标题 ≤30 字，正文 ≤120 字，5 个话题标签；视频号：标题 ≤30 字，正文第一人称 ≤150 字，3 个标签；
小红书：标题 ≤20 字含搜索词，正文 300-500 字分段带 emoji，8 个标签。
【红线】零网址、零电话微信、零「私信领取/扣1/加V/评论区领」，不用「最/第一/顶级」；来源写「媒体名+日期」。
只输出 JSON：{{"douyin":{{"title":"","body":"","tags":[""]}},"shipinhao":{{"title":"","body":"","tags":[""]}},"xiaohongshu":{{"title":"","body":"","tags":[""]}}}}"""
    result = await growth._call_toolbox_employee_json(8, prompt, timeout=300)
    data = result.get("data") if isinstance(result, dict) else {}
    out = {}
    limits = {"douyin": (30, 200, 5), "shipinhao": (30, 220, 3), "xiaohongshu": (20, 800, 10)}
    for key, (tl, bl, tn) in limits.items():
        raw = (data or {}).get(key) if isinstance(data, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        tags = [re.sub(r"[#\s]", "", str(t))[:16] for t in (raw.get("tags") or []) if str(t).strip()]
        out[key] = {
            "title": _clean_public(raw.get("title"), tl) or studio.get("title", "")[:tl],
            "body": _clean_public(raw.get("body"), bl),
            "tags": [t for t in tags if t][:tn],
        }
    return out


def checklist(copy: dict, script: str, has_broll: bool) -> list[dict]:
    names = {"douyin": "抖音", "shipinhao": "视频号", "xiaohongshu": "小红书"}
    items = [{"ok": not redline_issues(script), "text": "口播稿过红线（网址/联系方式/导流/绝对化用语）"
              + ("" if not redline_issues(script) else "：" + "、".join(redline_issues(script)))}]
    for key, label in names.items():
        c = copy.get(key) or {}
        issues = redline_issues(" ".join([c.get("title", ""), c.get("body", "")]))
        items.append({"ok": not issues and bool(c.get("title")),
                      "text": f"{label}文案过红线" + ("：" + "、".join(issues) if issues else "")})
    items.append({"ok": len((copy.get("xiaohongshu") or {}).get("title", "")) <= 20,
                  "text": "小红书标题 ≤20 字"})
    items.append({"ok": has_broll, "text": "成片含新闻证据卡（模式 C 真人+证据交替）"})
    items.append({"ok": True, "text": "成片与封面已加「AI 生成」标识，发布时请同时勾选平台的 AI 声明"})
    items.append({"ok": True, "text": "AI 只出草稿，发布请老板本人确认后点击"})
    return items


def _copy_md(studio: dict, copy: dict, checks: list[dict], sources: list[dict]) -> str:
    lines = [f"# 发布包 · {studio.get('title', '')}", ""]
    topic = studio.get("topic") or {}
    lines += [f"- 选题：{topic.get('title', '')}", f"- 出处：{topic.get('source', '')}", ""]
    for key, label in (("douyin", "抖音"), ("shipinhao", "视频号"), ("xiaohongshu", "小红书")):
        c = copy.get(key) or {}
        lines += [f"## {label}", f"**标题**：{c.get('title', '')}", "", c.get("body", ""), "",
                  " ".join("#" + t for t in c.get("tags", [])), ""]
    lines += ["## 发布前体检", *[("✅ " if c["ok"] else "🔴 ") + c["text"] for c in checks], ""]
    if sources:
        lines += ["## 证据卡图源", *[f"- {s['label']}：{s['page']}" for s in sources], ""]
    lines += ["---", "AI 只出草稿；请老板本人核对事实与画面后在各平台手动发布。"]
    return "\n".join(lines)


def quote_card(dst: str, source: str, text: str, kicker: str = "原文出处") -> None:
    """「原文引用卡」：不用别人的图，用出处 + 关键事实做证据（模式 C 的纸卡/荧光笔）。"""
    from PIL import Image, ImageDraw, ImageFont
    head = ImageFont.truetype(font_path(), 40)
    body = ImageFont.truetype(font_path(), 44)
    small = ImageFont.truetype(font_path(), 26)
    lines = _wrap_line(_clean_public(text, 80), 18).split("\n")[:4]
    h = 60 + 56 + len(lines) * 64 + 80
    card = Image.new("RGB", (960, h), (255, 252, 240))
    draw = ImageDraw.Draw(card)
    draw.rectangle([(0, 0), (14, h)], fill=(242, 183, 5))
    draw.text((44, 36), _clean_public(source, 30), font=head, fill=(40, 40, 40))
    y = 36 + 72
    for line in lines:
        draw.rectangle([(40, y + 30), (40 + draw.textlength(line, font=body), y + 52)],
                       fill=(255, 224, 120))
        draw.text((44, y), line, font=body, fill=(20, 20, 20))
        y += 64
    draw.text((44, h - 50), f"{kicker} · 以原报道为准", font=small, fill=(120, 120, 120))
    card.save(dst, "PNG")


def photo_card(src: str, dst: str, label: str) -> None:
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
    _card_image(buf.getvalue(), dst, label)


async def _evidence_cards(tid: int, studio: dict, store_path: str | None, work: str) -> list[dict]:
    """证据卡：①原文引用卡（出处+热度事实）②老板自家门店照 ③（平台开了全网抓图时）新闻配图。"""
    topic = studio.get("topic") or {}
    cards = []
    if topic.get("source") and topic.get("heat"):
        path = os.path.join(work, "quote.png")
        await asyncio.to_thread(quote_card, path, topic["source"], topic["heat"])
        cards.append({"path": path, "label": topic["source"], "page": topic["source"]})
    if store_path:
        path = os.path.join(work, "store.png")
        await asyncio.to_thread(photo_card, store_path, path, "我们店")
        cards.append({"path": path, "label": "我们店（老板自拍）", "page": "门店实拍"})
    news = await _grab_broll(tid, topic.get("keywords") or [], 2, work)
    return cards[:1] + news + cards[1:]


async def _grab_broll(tid: int, keywords: list[str], want: int, work: str) -> list[dict]:
    from . import imagehunt
    from urllib.parse import urlparse
    got, seen = [], set()
    for kw in keywords:
        if len(got) >= want:
            break
        try:
            cands = await imagehunt.search(kw, n=10, tenant_id=tid)
        except Exception as exc:  # 抓图是加分项，失败不影响成片
            log.info("studio broll search skipped error_type=%s", type(exc).__name__)
            continue
        for c in cands:
            if len(got) >= want:
                break
            page = str(c.get("page") or "")
            host = urlparse(page).hostname or str(c.get("from") or "网络")
            if c.get("img") in seen:
                continue
            seen.add(c.get("img"))
            try:
                data = await imagehunt.fetch_image(c["img"], imagehunt._ref_for(c), tenant_id=tid)
            except Exception:
                continue
            path = os.path.join(work, f"card{len(got)}.png")
            await asyncio.to_thread(_card_image, data, path, host)
            got.append({"path": path, "label": host, "page": page or host})
    return got


async def compose(job_id: int, tid: int, params: dict, video_path: str, photo_path: str,
                  progress) -> dict:
    """数字人原片 → 证据卡交替 + 字幕 + 标题 → 成片；再出封面、三平台文案、体检与发布包 zip。"""
    from . import features
    studio = dict(params.get("studio") or {})
    script = params.get("script") or ""
    state = {"status": "running", "started_at": time.time()}
    await db.arun(_save_state, job_id, state)
    out_dir = os.path.dirname(video_path)
    base = os.path.join(out_dir, f"avatar_{job_id}_studio")
    with tempfile.TemporaryDirectory(prefix=f"studio{job_id}-") as work:
        progress("start", "✂️ 做证据卡（原文引用卡 + 门店实拍 + 新闻配图）…")
        store_path = None
        if studio.get("store_name"):
            try:
                store_path = await db.arun(avatar.asset_path, studio["store_name"], {"store"}, tid)
            except Exception:
                store_path = None
        broll = (await _evidence_cards(tid, studio, store_path, work))[:4]
        duration = await asyncio.to_thread(_probe_duration, video_path)
        timeline = subtitle_timeline(script, duration)
        windows = broll_windows(timeline, len(broll), duration)
        windows = _place_cards(broll, timeline, windows, studio, duration)
        cards = [(b["path"], a, e) for b, (a, e) in zip(broll, windows)]
        progress("start", f"✂️ 剪辑中：真人 + {len(cards)} 张证据卡 + 字幕 + 标题…")
        final = base + ".mp4"
        await asyncio.to_thread(
            _run_render, render_cmd(video_path, cards, work, timeline,
                                    studio.get("title", ""), final, duration))
        ai_label = await db.arun(features.ai_label_for, tid, None)
        if ai_label:
            await asyncio.to_thread(avatar.stamp_video_label, final, ai_label)
        progress("start", "🖼 做封面（3:4 + 9:16）…")
        await asyncio.to_thread(make_covers, photo_path, studio.get("cover_title") or studio.get("title", ""),
                                base + "-cover34.jpg", base + "-cover916.jpg", ai_label or "AI生成")
        progress("start", "📝 写三平台发布文案 + 体检…")
        copy = await publish_copy(tid, studio, script)
        checks = checklist(copy, script, bool(cards))
        sources = [{"label": b["label"], "page": b["page"]} for b in broll[:len(cards)]]
        md = _copy_md(studio, copy, checks, sources)
        pack = base + "-pack.zip"
        with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(final, "成片-剪辑版.mp4")
            z.write(video_path, "数字人原片.mp4")
            z.write(base + "-cover34.jpg", "封面-3比4.jpg")
            z.write(base + "-cover916.jpg", "封面-9比16.jpg")
            z.writestr("发布文案与体检.md", md)
            z.writestr("口播稿.txt", script)
    rel = f"/files/avatar/avatar_{job_id}_studio"
    state = {
        "status": "done", "finished_at": time.time(),
        "video": rel + ".mp4", "cover34": rel + "-cover34.jpg", "cover916": rel + "-cover916.jpg",
        "pack": rel + "-pack.zip", "copy": copy, "checks": checks,
        "broll": len(cards), "title": studio.get("title", ""),
    }
    await db.arun(_save_state, job_id, state)
    return state


def refund_pack(job_id: int, params: dict, reason: str) -> None:
    op = ((params or {}).get("studio") or {}).get("billing_op")
    if op:
        try:
            billing.fail_operation(op, reason)
        except Exception as exc:
            log.error("studio pack refund failed job=%s error_type=%s", job_id, type(exc).__name__)


def recover_interrupted() -> int:
    """服务重启时仍在剪辑的工单：标失败并退剪辑包点数（原片已交付不受影响）。"""
    count = 0
    for row in db.q("SELECT key,value FROM app_setting WHERE key LIKE 'studio:%'"):
        state = db.jloads(row.get("value"), {}) or {}
        if state.get("status") != "running":
            continue
        try:
            job_id = int(str(row["key"]).split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        job = db.one("SELECT params_json FROM avatar_job WHERE id=?", (job_id,)) or {}
        _save_state(job_id, {"status": "failed",
                             "error": "服务重启打断了自动剪辑，剪辑包点数已退回；原片可正常下载"})
        refund_pack(job_id, db.jloads(job.get("params_json"), {}) or {}, "服务重启中断自动退回")
        count += 1
    return count


async def run_after_delivery(job_id: int, tid: int, params: dict, video_path: str,
                             photo_path: str, progress) -> None:
    """数字人原片交付后自动剪辑；失败只退剪辑包点数，不影响原片。"""
    try:
        await compose(job_id, tid, params, video_path, photo_path, progress)
        op = (params.get("studio") or {}).get("billing_op")
        if op:
            await db.arun(billing.complete_operation, op)
        progress("done", "📦 剪辑版成片 + 封面 + 三平台发布包已就绪")
    except Exception as exc:
        log.error("studio compose failed job=%s error_type=%s", job_id, type(exc).__name__)
        await db.arun(_save_state, job_id, {"status": "failed", "error": "自动剪辑失败，剪辑包点数已退回；原片可正常下载"})
        await db.arun(refund_pack, job_id, params, "自动剪辑失败自动退回")
        progress("error", "自动剪辑失败，剪辑包点数已退回；数字人原片可正常下载")
