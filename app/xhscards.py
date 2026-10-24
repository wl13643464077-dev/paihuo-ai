"""小红书 3:4 图文卡组本地出图（移植自「内容流水线」v3 的 xhs_carousel.py）。

为什么做：小红书用户是来「搜和存」的。流水线实测里口播视频中位 48 观看，
图文「保姆级」清单拿到 7381 观看、143 收藏。所以小红书版默认出 6–9 张卡：
封面（大字结论）→ 事实卡 → 步骤/清单卡 → 避坑卡 → 总结收藏卡。

纯 PIL 本地渲染，零生图费用；字体找不到时抛 CardRenderError，由调用方降级。
卡片里只放文字，不放二维码、网址、联系方式、其他平台 logo。
"""
from __future__ import annotations

import io
import os
import re

W, H = 1080, 1440
MARGIN = 84
MAX_CARDS = 9
MIN_CARDS = 3

THEMES = {
    "中国红": dict(bg="#FFF6EA", ink="#141414", accent="#D7261E", hl="#FFE100", sub="#6B6B6B"),
    "蓝白": dict(bg="#F3F7FF", ink="#0E1A33", accent="#1E5BFF", hl="#FFD23F", sub="#5B667A"),
    "黑金": dict(bg="#F7F2E8", ink="#111111", accent="#B8862B", hl="#F2C14E", sub="#6E6656"),
    "墨绿": dict(bg="#F1F4EC", ink="#16231A", accent="#1F6B45", hl="#F4D35E", sub="#5E6B60"),
    "粉红": dict(bg="#FFF1F3", ink="#1D1416", accent="#E8336D", hl="#FFE066", sub="#7A6468"),
}
DEFAULT_THEME = "中国红"
CARD_TYPES = ("cover", "fact", "step", "pitfall", "summary")

_FONT_CANDIDATES = (
    os.environ.get("PAIHUO_CARD_FONT") or "",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
)
_URLISH = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


class CardRenderError(RuntimeError):
    """本机没有中文字体或卡片规格无效。"""


def _font_path() -> str:
    for path in _FONT_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    raise CardRenderError("服务器缺少中文字体，暂时无法出图文卡片")


def _font(size: int, bold: bool = True):
    from PIL import ImageFont
    path = _font_path()
    # Hiragino/Noto 的 ttc 里粗体通常在靠后的索引；取不到就用默认索引。
    for index in ((2, 1, 0) if bold else (0,)):
        try:
            return ImageFont.truetype(path, size, index=index)
        except (OSError, ValueError):
            continue
    raise CardRenderError("中文字体无法加载")


def _clean(value, limit: int) -> str:
    text = _URLISH.sub("", str(value or ""))
    return re.sub(r"[ \t]+", " ", text).strip()[:limit]


def _lines(value, limit_each: int, max_items: int) -> list[str]:
    if isinstance(value, str):
        value = [part for part in re.split(r"[\n；;]", value) if part.strip()]
    if not isinstance(value, (list, tuple)):
        return []
    return [_clean(item, limit_each) for item in value if _clean(item, limit_each)][:max_items]


def normalize_spec(raw) -> dict:
    """把 LLM 给的卡片规格收敛成安全、可渲染的结构；无效卡片直接丢弃。"""
    if isinstance(raw, list):
        raw = {"cards": raw}
    if not isinstance(raw, dict):
        return {"theme": DEFAULT_THEME, "cards": []}
    theme = str(raw.get("theme") or "").strip()
    cards = []
    for item in raw.get("cards") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        if kind not in CARD_TYPES:
            kind = "step" if item.get("no") else "fact"
        title = item.get("title")
        if kind == "cover":
            title_lines = _lines(title, 12, 3) or [_clean(title, 12)]
            card = {"type": "cover", "title": [t for t in title_lines if t],
                    "kicker": _clean(item.get("kicker"), 14),
                    "sub": _clean(item.get("sub"), 18),
                    "stamp": _clean(item.get("stamp"), 10)}
            if not card["title"]:
                continue
        elif kind in ("pitfall", "summary"):
            card = {"type": kind, "title": _clean(title, 16) or ("最容易踩的坑" if kind == "pitfall" else "划重点"),
                    "items": _lines(item.get("items") or item.get("body"), 34, 5),
                    "ending": _clean(item.get("ending"), 24)}
            if not card["items"]:
                continue
        else:
            card = {"type": kind, "no": _clean(item.get("no"), 3),
                    "title": _clean(title, 18), "body": _clean(item.get("body"), 90),
                    "todo": _clean(item.get("todo"), 40)}
            if not card["title"] and not card["body"]:
                continue
        cards.append(card)
    # 封面必须在第一张；没有封面就用第一张卡的标题补一张。
    covers = [card for card in cards if card["type"] == "cover"]
    others = [card for card in cards if card["type"] != "cover"]
    if not covers and others:
        covers = [{"type": "cover", "title": [others[0].get("title") or "划重点"],
                   "kicker": "", "sub": "", "stamp": ""}]
    cards = (covers[:1] + others)[:MAX_CARDS]
    return {"theme": theme if theme in THEMES else DEFAULT_THEME, "cards": cards}


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    out = []
    for para in str(text or "").split("\n"):
        current = ""
        for ch in para:
            if draw.textlength(current + ch, font=font) > max_width and current:
                if ch in "，。、；：！？”）》,.!?;:":
                    out.append(current + ch)
                    current = ""
                    continue
                out.append(current)
                current = ch
            else:
                current += ch
        if current:
            out.append(current)
    return out


def _paragraph(draw, xy, text, font, fill, max_width, line_height=1.5, max_lines=None) -> int:
    x, y = xy
    lines = _wrap(draw, text, font, max_width)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += int(font.size * line_height)
    return y


def _fit(draw, text: str, max_width: int, start: int, minimum: int = 56):
    size = start
    while size > minimum and draw.textlength(text, font=_font(size)) > max_width:
        size -= 4
    return _font(size)


def _footer(draw, theme: dict, page: int, total: int, signature: str):
    small = _font(28, bold=False)
    if signature:
        draw.text((MARGIN, H - 92), signature, font=small, fill=theme["sub"])
    label = f"{page}/{total}"
    draw.text((W - MARGIN - draw.textlength(label, font=small), H - 92), label,
              font=small, fill=theme["sub"])


def _render_cover(draw, card, theme):
    draw.rectangle([0, 0, W, 18], fill=theme["accent"])
    y = 210
    if card.get("kicker"):
        kicker_font = _font(40)
        width = draw.textlength(card["kicker"], font=kicker_font)
        draw.rounded_rectangle([MARGIN, y, MARGIN + width + 48, y + 72], radius=14,
                               fill=theme["ink"])
        draw.text((MARGIN + 24, y + 12), card["kicker"], font=kicker_font, fill=theme["bg"])
        y += 130
    for index, line in enumerate(card["title"]):
        font = _fit(draw, line, W - 2 * MARGIN, 150, 72)
        if index == len(card["title"]) - 1:
            # 最后一行打荧光笔高亮，制造「结论」的视觉重心。
            width = draw.textlength(line, font=font)
            draw.rectangle([MARGIN - 8, y + font.size * 0.55, MARGIN + width + 8,
                            y + font.size * 1.12], fill=theme["hl"])
        draw.text((MARGIN, y), line, font=font, fill=theme["ink"])
        y += int(font.size * 1.25)
    if card.get("sub"):
        y += 30
        draw.line([MARGIN, y, MARGIN + 120, y], fill=theme["accent"], width=8)
        _paragraph(draw, (MARGIN, y + 34), card["sub"], _font(52), theme["accent"],
                   W - 2 * MARGIN, max_lines=2)
    if card.get("stamp"):
        stamp_font = _font(54)
        lines = re.split(r"\\n|\n", card["stamp"])[:2]
        cx, cy, radius = W - MARGIN - 130, H - 380, 130
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                     outline=theme["accent"], width=10)
        for i, line in enumerate(lines):
            width = draw.textlength(line, font=stamp_font)
            draw.text((cx - width / 2, cy - 34 * len(lines) + i * 68), line,
                      font=stamp_font, fill=theme["accent"])


def _photo_background(canvas, image_bytes: bytes):
    """真实照片铺满封面（居中裁切到 3:4）并压暗，保证白色大字可读。"""
    from PIL import Image, ImageEnhance, ImageOps
    with Image.open(io.BytesIO(image_bytes)) as source:
        source.draft("RGB", (W * 2, H * 2))
        photo = ImageOps.exif_transpose(source).convert("RGB")
    photo = ImageOps.fit(photo, (W, H), method=Image.LANCZOS)
    photo = ImageEnhance.Brightness(photo).enhance(0.55)
    canvas.paste(photo, (0, 0))
    # 底部再加一层渐变黑，标题落在下半部分。
    shade = Image.new("L", (1, H))
    for y in range(H):
        shade.putpixel((0, y), int(max(0, (y - H * 0.35) / (H * 0.65)) * 170))
    black = Image.new("RGB", (W, H), "#000000")
    canvas.paste(black, (0, 0), shade.resize((W, H)))


def _render_photo_cover(draw, card, theme):
    y = H - 160 - sum(int(_fit(draw, line, W - 2 * MARGIN, 150, 72).size * 1.25)
                      for line in card["title"])
    if card.get("sub"):
        y -= 110
    if card.get("kicker"):
        kicker_font = _font(40)
        width = draw.textlength(card["kicker"], font=kicker_font)
        draw.rounded_rectangle([MARGIN, y - 110, MARGIN + width + 48, y - 38], radius=14,
                               fill=theme["accent"])
        draw.text((MARGIN + 24, y - 98), card["kicker"], font=kicker_font, fill="#FFFFFF")
    for index, line in enumerate(card["title"]):
        font = _fit(draw, line, W - 2 * MARGIN, 150, 72)
        fill = theme["hl"] if index == len(card["title"]) - 1 else "#FFFFFF"
        draw.text((MARGIN, y), line, font=font, fill=fill,
                  stroke_width=6, stroke_fill="#000000")
        y += int(font.size * 1.25)
    if card.get("sub"):
        _paragraph(draw, (MARGIN, y + 20), card["sub"], _font(52), "#FFFFFF",
                   W - 2 * MARGIN, max_lines=2)
    draw.text((W - MARGIN - 200, 60), "图片仅作配图", font=_font(26, bold=False), fill="#DDDDDD")


def _render_fact_or_step(draw, card, theme):
    y = 150
    if card.get("no"):
        badge = _font(96)
        draw.rounded_rectangle([MARGIN, y, MARGIN + 150, y + 150], radius=28, fill=theme["accent"])
        width = draw.textlength(card["no"], font=badge)
        draw.text((MARGIN + 75 - width / 2, y + 18), card["no"], font=badge, fill="#FFFFFF")
        y += 200
    elif card["type"] == "fact":
        draw.text((MARGIN, y), "发生了什么", font=_font(40), fill=theme["accent"])
        y += 80
    if card.get("title"):
        title_font = _font(76)
        y = _paragraph(draw, (MARGIN, y), card["title"], title_font, theme["ink"],
                       W - 2 * MARGIN, line_height=1.3, max_lines=3) + 30
    if card.get("body"):
        y = _paragraph(draw, (MARGIN, y), card["body"], _font(52, bold=False), theme["ink"],
                       W - 2 * MARGIN, line_height=1.6, max_lines=7) + 30
    if card.get("todo"):
        box_font = _font(44)
        lines = _wrap(draw, "怎么做：" + card["todo"], box_font, W - 2 * MARGIN - 60)[:3]
        box_h = 50 + len(lines) * 66
        top = min(y + 20, H - 200 - box_h)
        draw.rounded_rectangle([MARGIN, top, W - MARGIN, top + box_h], radius=22, fill=theme["hl"])
        for i, line in enumerate(lines):
            draw.text((MARGIN + 30, top + 26 + i * 66), line, font=box_font, fill=theme["ink"])


def _render_list(draw, card, theme):
    pitfall = card["type"] == "pitfall"
    draw.rectangle([0, 0, W, 240], fill=theme["ink"] if pitfall else theme["accent"])
    title_font = _fit(draw, card["title"], W - 2 * MARGIN, 88, 60)
    draw.text((MARGIN, 80), card["title"], font=title_font, fill="#FFFFFF")
    y = 320
    item_font = _font(54, bold=False)
    mark_font = _font(54)
    for index, item in enumerate(card["items"], start=1):
        mark = "×" if pitfall else f"{index}"
        draw.text((MARGIN, y), mark, font=mark_font, fill=theme["accent"])
        y = _paragraph(draw, (MARGIN + 80, y), item, item_font, theme["ink"],
                       W - 2 * MARGIN - 80, line_height=1.5, max_lines=3) + 54
        if y > H - 300:
            break
    if card.get("ending"):
        ending_font = _font(48)
        width = draw.textlength(card["ending"], font=ending_font)
        top = H - 250
        draw.rounded_rectangle([MARGIN, top, min(W - MARGIN, MARGIN + width + 60), top + 96],
                               radius=48, fill=theme["hl"])
        draw.text((MARGIN + 30, top + 20), card["ending"], font=ending_font, fill=theme["ink"])


def render_cards(spec: dict, *, signature: str = "",
                 cover_image: bytes | None = None) -> list[bytes]:
    """渲染整组卡片，返回每张 PNG 的字节（1080×1440）。

    ``cover_image`` 给了真实照片时，封面改成「照片铺满 + 描边大字」样式。"""
    from PIL import Image, ImageDraw
    normalized = normalize_spec(spec)
    cards = normalized["cards"]
    if len(cards) < MIN_CARDS:
        raise CardRenderError(f"图文卡片至少 {MIN_CARDS} 张，当前有效 {len(cards)} 张")
    _font_path()
    theme = THEMES[normalized["theme"]]
    signature = _clean(signature, 16)
    images = []
    for page, card in enumerate(cards, start=1):
        canvas = Image.new("RGB", (W, H), theme["bg"])
        draw = ImageDraw.Draw(canvas)
        if card["type"] == "cover" and cover_image:
            try:
                _photo_background(canvas, cover_image)
                draw = ImageDraw.Draw(canvas)
                _render_photo_cover(draw, card, theme)
            except (OSError, ValueError):
                canvas.paste(Image.new("RGB", (W, H), theme["bg"]))
                draw = ImageDraw.Draw(canvas)
                _render_cover(draw, card, theme)
        elif card["type"] == "cover":
            _render_cover(draw, card, theme)
        elif card["type"] in ("pitfall", "summary"):
            _render_list(draw, card, theme)
        else:
            _render_fact_or_step(draw, card, theme)
        _footer(draw, theme, page, len(cards), signature)
        buffer = io.BytesIO()
        canvas.save(buffer, "PNG", optimize=True)
        images.append(buffer.getvalue())
    return images
