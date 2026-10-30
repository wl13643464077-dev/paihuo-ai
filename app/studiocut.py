"""门店数字人「模式 C」自动剪辑（按《内容流水线 v3.1》规范/16-抖音基准-模式C、剪辑升级细则 v4 落地）。

能自动化、在小服务器上稳定跑的部分：
- 版式：状态栏 0–236 不放字；固定标题条 246–462（白字 + 黄底黑字两行）；主画面顶黄色进度条 + 章节刻度；
  字幕底边 1560、78px 白字 7px 黑描边、数字和关键词黄色；底部 1560–1920 与右侧按钮栏不放元素；
- 时间：字幕按人声停顿对齐（douyin_cut.py 的 align 算法）；
- 画面：第 0 帧大字钩子（1.6 秒）、人名条（2–5 秒，可选）、证据卡、结尾 CTA；人物逐句慢推 + 问句/数字处 punch-in；
- 声音：自制合成音效（无版权）挂在钩子/证据卡/章节/CTA 的同一帧；人声链 高通80Hz→3kHz+2.5dB→压缩→-15 LUFS。

整条渲染只有 7 个输入，配合 MALLOC_ARENA_MAX 在 2GB 地址空间上限内稳定运行。
"""
from __future__ import annotations

import math
import os
import re
import struct
import subprocess
import wave

W, H, FPS = 1080, 1920, 30
TITLE_Y, TITLE_H = 246, 216            # 固定标题条
BAR_Y = TITLE_Y + TITLE_H + 8          # 进度条
SUB_BOTTOM, SUB_H = 1560, 280          # 字幕带：底边 1560
MAIN_Y, MAIN_H = 470, 1090            # 主画面 470–1560（人物放这里，标题条不压脸）
CARD_Y, CARD_H = MAIN_Y, 800          # 证据卡轨画布（主画面上部到字幕之上）
CARD_BOX_TOP = 360                     # 卡片放在画布 360–800（屏幕 830–1270）
PIP_SIZE, PIP_X, PIP_Y = 280, W - 330, MAIN_Y + 50   # 证据卡出现时人物缩成右上圆窗（黄圈），脸始终可见
FX_Y = 990                             # 钩子/人名条/CTA 所在带（全帧透明画布）
YELLOW = (255, 212, 0)
PUNCT = "，。！？、；：,.!?;:"

_NUM_RE = re.compile(r"[0-9０-９]+(?:\.[0-9]+)?[%％万亿千百元块家条个倍成号月年天岁分秒]*"
                     r"|[一二三四五六七八九十两百千万亿]+[成倍家条个件招步]")


# ---------- 停顿对齐（移植自 脚本/douyin_cut.py） ----------
def parse_silences(stderr: str) -> list[tuple[float, float]]:
    s = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", stderr or "")]
    e = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", stderr or "")]
    return list(zip(s, e))


def phrases(text: str) -> list[str]:
    out, cur = [], ""
    for ch in str(text or "").replace("\n", ""):
        cur += ch
        if ch in PUNCT:
            out.append(cur)
            cur = ""
    if cur.strip():
        out.append(cur)
    merged = []
    for p in (p for p in out if p.strip()):
        if merged and len(re.sub(r"[\s" + PUNCT + "]", "", p)) < 4:
            merged[-1] += p  # 太短的半句并到上一句，避免字幕一闪而过
        else:
            merged.append(p)
    return merged


def align(text: str, dur: float, sil: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
    ph = phrases(text)
    if not ph:
        return []
    sil = [(a, b) for a, b in sil if b - a > 0.15]
    speech, t = [], 0.0
    for a, b in sil:
        if a > t:
            speech.append((t, a))
        t = max(t, b)
    if t < dur:
        speech.append((t, dur))
    total_speech = sum(b - a for a, b in speech) or dur
    clean = lambda s: len(re.sub(r"[\s" + PUNCT + "\"“”「」]", "", s))
    total = sum(clean(p) for p in ph) or 1

    def at(frac: float) -> float:
        need = frac * total_speech
        for a, b in speech:
            if need <= b - a:
                return a + need
            need -= b - a
        return dur

    mids = [(a + b) / 2 for a, b in sil]
    cum, res, start = 0, [], speech[0][0] if speech else 0.0
    for i, p in enumerate(ph):
        cum += clean(p)
        end = at(cum / total)
        if mids and i < len(ph) - 1:
            near = min(mids, key=lambda m: abs(m - end))
            if abs(near - end) < 0.55:
                end = near
        else:
            end = dur
        res.append((round(start, 2), round(max(end, start + 0.4), 2), p.rstrip("，。、；：,.;:")))
        start = end
    return res


# ---------- 文字渲染 ----------
def _font(path: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(path, size)


def _segments(text: str, keywords: list[str]) -> list[tuple[str, bool]]:
    """把一句话切成 (片段, 是否高亮)：数字、金额、关键词标黄。"""
    marks = [False] * len(text)
    for m in _NUM_RE.finditer(text):
        for i in range(m.start(), m.end()):
            marks[i] = True
    for kw in keywords:
        kw = (kw or "").strip()
        if len(kw) >= 2:
            for m in re.finditer(re.escape(kw), text):
                for i in range(m.start(), m.end()):
                    marks[i] = True
    out = []
    for ch, mk in zip(text, marks):
        if out and out[-1][1] == mk:
            out[-1] = (out[-1][0] + ch, mk)
        else:
            out.append((ch, mk))
    return out


def _balanced_lines(text: str, width: int) -> list[str]:
    """按行均分换行；不把数字/金额（如「1000家」）拆到两行，优先在标点后断。"""
    text = re.sub(r"[，。！？,.!?；;：:]$", "", text)
    if len(text) <= width:
        return [text]
    protected = set()
    for m in _NUM_RE.finditer(text):
        protected.update(range(m.start() + 1, m.end()))
    for m in re.finditer(r"——|……|「[^」]{1,8}」", text):
        protected.update(range(m.start() + 1, m.end()))
    n = -(-len(text) // width)
    lines, rest = [], text
    for k in range(n, 1, -1):
        target = -(-len(rest) // k)
        best = None
        for cut in range(max(1, target - 4), min(len(rest) - 1, target + 4) + 1):
            if cut in protected or (cut + (len(text) - len(rest))) in protected:
                continue
            score = abs(cut - target) - (2 if rest[cut - 1] in PUNCT + "——」" else 0)
            if best is None or score < best[0]:
                best = (score, cut)
        cut = best[1] if best else target
        lines.append(rest[:cut])
        rest = rest[cut:]
    lines.append(rest)
    return [l for l in lines if l]


def subtitle_png(path: str, text: str, font_path: str, keywords: list[str]) -> None:
    """78px 白字 7px 黑描边 + 柔和投影，数字/关键词黄色；底边对齐字幕带底。"""
    from PIL import Image, ImageDraw, ImageFilter
    size, stroke = 78, 7
    font = _font(font_path, size)
    lines = _balanced_lines(text, 12)[:2]
    im = Image.new("RGBA", (W, SUB_H), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (W, SUB_H), (0, 0, 0, 0))
    draw, sdraw = ImageDraw.Draw(im), ImageDraw.Draw(shadow)
    y = SUB_H - 24 - len(lines) * (size + 18)
    for line in lines:
        lw = draw.textlength(line, font=font)
        x = (W - lw) / 2
        sdraw.text((x + 4, y + 6), line, font=font, fill=(0, 0, 0, 150),
                   stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
        for seg, hot in _segments(line, keywords):
            draw.text((x, y), seg, font=font, fill=YELLOW if hot else (255, 255, 255),
                      stroke_width=stroke, stroke_fill=(0, 0, 0))
            x += draw.textlength(seg, font=font)
        y += size + 18
    out = Image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)), im)
    out.save(path, "PNG")


def title_png(path: str, line1: str, line2: str, font_path: str, ai_label: str,
              chapters: list[tuple[float, str]], duration: float) -> None:
    """全帧透明画布：标题条两行（白字 + 黄底黑字）+ 进度条底槽与章节刻度 + AI 声明小字。"""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([(0, TITLE_Y), (W, TITLE_Y + TITLE_H)], fill=(0, 0, 0, 120))
    f1, f2 = _font(font_path, 62), _font(font_path, 62)
    line1, line2 = line1[:14], line2[:14]
    w1 = d.textlength(line1, font=f1)
    d.text(((W - w1) / 2, TITLE_Y + 22), line1, font=f1, fill=(255, 255, 255),
           stroke_width=4, stroke_fill=(0, 0, 0))
    if line2:
        w2 = d.textlength(line2, font=f2)
        bx0, by0 = (W - w2) / 2 - 22, TITLE_Y + 108
        d.rounded_rectangle([(bx0, by0), (bx0 + w2 + 44, by0 + 88)], radius=14, fill=YELLOW)
        d.text(((W - w2) / 2, by0 + 10), line2, font=f2, fill=(0, 0, 0))
    if ai_label:
        small = _font(font_path, 22)
        lw = d.textlength(ai_label[:16], font=small)
        d.text((W - 24 - lw, TITLE_Y + TITLE_H - 30), ai_label[:16], font=small, fill=(230, 230, 230))
    # 进度条底槽 + 章节刻度与胶囊
    d.rectangle([(0, BAR_Y), (W, BAR_Y + 10)], fill=(255, 255, 255, 70))
    cap = _font(font_path, 24)
    last_end = -1
    for t, label in chapters:
        x = int(W * t / max(duration, 0.1))
        d.rectangle([(x - 2, BAR_Y - 4), (x + 2, BAR_Y + 14)], fill=(255, 255, 255, 220))
        if label and x + 6 > last_end + 8:
            last_end = min(max(4, x + 6), W - d.textlength(label, font=cap) - 20) + d.textlength(label, font=cap) + 16
        else:
            continue
        if label:
            tw = d.textlength(label, font=cap)
            x0 = min(max(4, x + 6), W - tw - 20)
            d.rounded_rectangle([(x0, BAR_Y + 18), (x0 + tw + 16, BAR_Y + 50)], radius=14,
                                fill=(0, 0, 0, 140))
            d.text((x0 + 8, BAR_Y + 20), label, font=cap, fill=(255, 255, 255))
    im.save(path, "PNG")


def bar_png(path: str) -> None:
    from PIL import Image
    Image.new("RGBA", (W, 10), YELLOW + (255,)).save(path, "PNG")


def fx_frame(path: str, kind: str, text: str, font_path: str) -> None:
    """钩子大字 / 人名条 / CTA，画在全帧透明画布的 FX 带上（不压字幕、不压脸、不进右侧按钮栏）。"""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if kind == "hook":
        lines = _balanced_lines(text, 8)[:2]
        f = _font(font_path, 112)
        y = MAIN_Y + 40  # 主画面顶部（背景/招牌区），不压脸
        for n, line in enumerate(lines):
            lw = d.textlength(line, font=f)
            d.text(((W - lw) / 2, y), line, font=f, fill=YELLOW if n == 0 else (255, 255, 255),
                   stroke_width=10, stroke_fill=(0, 0, 0))
            y += 136
    elif kind == "name":
        f = _font(font_path, 44)
        tw = d.textlength(text[:20], font=f)
        x0, y0 = 40, FX_Y + 190
        d.rectangle([(x0, y0), (x0 + 12, y0 + 76)], fill=YELLOW)
        d.rectangle([(x0 + 12, y0), (x0 + 12 + tw + 40, y0 + 76)], fill=(0, 0, 0, 170))
        d.text((x0 + 32, y0 + 12), text[:20], font=f, fill=(255, 255, 255))
    elif kind == "cta":
        f = _font(font_path, 60)
        label = (text or "评论区说说")[:10] + " ↓"
        tw = d.textlength(label, font=f)
        x0, y0 = (W - tw) / 2 - 40, FX_Y + 120
        d.rounded_rectangle([(x0, y0), (x0 + tw + 80, y0 + 104)], radius=52, fill=YELLOW,
                            outline=(0, 0, 0), width=5)
        d.text((x0 + 40, y0 + 18), label, font=f, fill=(0, 0, 0))
    im.save(path, "PNG")


def card_frame_png(path: str, inner: str | None) -> None:
    """证据卡帧：卡片放在主画面下部；右上画黄圈，圆窗里是人物（模式 C：任何时候都能看到脸）。"""
    from PIL import Image, ImageDraw
    canvas = Image.new("RGBA", (W, CARD_H), (0, 0, 0, 0))
    if inner:
        with Image.open(inner) as im:
            im = im.convert("RGBA")
            im.thumbnail((W - 100, CARD_H - CARD_BOX_TOP - 10))
            box_h = CARD_H - CARD_BOX_TOP
            canvas.alpha_composite(im, ((W - im.width) // 2, CARD_BOX_TOP + (box_h - im.height) // 2))
        d = ImageDraw.Draw(canvas)
        x, y = PIP_X, PIP_Y - CARD_Y
        d.ellipse([(x - 8, y - 8), (x + PIP_SIZE + 8, y + PIP_SIZE + 8)], outline=YELLOW, width=8)
    canvas.save(path, "PNG")


def circle_mask_png(path: str) -> None:
    from PIL import Image, ImageDraw
    m = Image.new("L", (PIP_SIZE, PIP_SIZE), 0)
    ImageDraw.Draw(m).ellipse([(0, 0), (PIP_SIZE - 1, PIP_SIZE - 1)], fill=255)
    m.save(path, "PNG")


def canvas_png(path: str, h: int, inner: str | None = None) -> None:
    from PIL import Image
    canvas = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    if inner:
        with Image.open(inner) as im:
            im = im.convert("RGBA")
            im.thumbnail((W - 160, h))
            canvas.alpha_composite(im, ((W - im.width) // 2, (h - im.height) // 2))
    canvas.save(path, "PNG")


def concat_list(entries: list[tuple[str, float]], list_path: str) -> None:
    with open(list_path, "w", encoding="utf-8") as fh:
        for path, dur in entries:
            fh.write(f"file '{path}'\nduration {max(0.04, dur):.3f}\n")
        fh.write(f"file '{entries[-1][0]}'\n")


def track(entries: list[tuple[float, float, str]], blank: str, duration: float) -> list[tuple[str, float]]:
    """(start, end, png) 不重叠片段 → concat 清单条目，空隙填透明帧。"""
    out, t = [], 0.0
    for a, b, path in sorted(entries):
        a, b = max(a, t), min(b, duration)
        if b - a < 0.05:
            continue
        if a > t:
            out.append((blank, a - t))
        out.append((path, b - a))
        t = b
    out.append((blank, max(0.04, duration - t)))
    return out


# ---------- 音效（自制合成，无版权） ----------
SR = 44100


def _tone(dur, f0, f1=None, decay=8.0, amp=0.5, noise=0.0, seed=1):
    import random
    rnd = random.Random(seed)
    n, out, phase = int(SR * dur), [], 0.0
    for i in range(n):
        t = i / SR
        f = f0 if f1 is None else f0 + (f1 - f0) * (t / dur)
        phase += 2 * math.pi * f / SR
        env = math.exp(-decay * t) * min(1.0, t * 400)
        v = math.sin(phase) * (1 - noise) + (rnd.random() * 2 - 1) * noise
        out.append(v * env * amp)
    return out


def sfx_samples(kind: str):
    if kind == "pop":
        return _tone(0.12, 900, 500, decay=30, amp=0.55)
    if kind == "whoosh":
        return _tone(0.35, 300, 1600, decay=6, amp=0.35, noise=0.85, seed=3)
    if kind == "ding":
        return [a + b for a, b in zip(_tone(0.6, 1320, decay=6, amp=0.4), _tone(0.6, 1980, decay=9, amp=0.15))]
    if kind == "click":
        return _tone(0.05, 2400, 1800, decay=60, amp=0.4)
    if kind == "riser":
        return _tone(0.6, 200, 900, decay=0.5, amp=0.25, noise=0.3, seed=5)
    return []


def write_sfx(path: str, hits: list[tuple[float, str]], duration: float) -> int:
    """按 (时间, 类型) 把音效混成一条单声道轨（峰值约 -9dBFS，人声链之后再整体响度规整）。"""
    n = int(SR * (duration + 0.5))
    buf = [0.0] * n
    count = 0
    for t, kind in hits:
        s = sfx_samples(kind)
        start = int(max(0.0, t) * SR)
        if start >= n or not s:
            continue
        count += 1
        for i, v in enumerate(s):
            if start + i < n:
                buf[start + i] += v
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v)) * 32767 * 0.7)) for v in buf))
    return count


# ---------- 画面运动：逐句慢推 + punch-in ----------
def zoom_expr(timeline: list[tuple[float, float, str]]) -> str:
    """每句 1.00→1.05 慢推；问句/带数字的句子开头 0.4 秒 punch-in 到 1.10 再回落。"""
    parts = []
    for a, b, text in timeline:
        span = max(0.2, b - a)
        if re.search(r"[？?！!]", text) or _NUM_RE.search(text):
            z = f"(1.10-0.05*min((t-{a})/0.4\\,1)+0.03*(t-{a})/{span:.2f})"
        else:
            z = f"(1.00+0.05*(t-{a})/{span:.2f})"
        parts.append((a, b, z))
    expr = "1.0"
    for a, b, z in reversed(parts):
        expr = f"if(between(t\\,{a}\\,{b})\\,{z}\\,{expr})"
    return expr


def chapters_for(timeline: list[tuple[float, float, str]], source: str) -> list[tuple[float, str]]:
    """章节点：事件（提到出处）/ 判断 / 做法（三条、第一）/ 互动（最后一句）。"""
    out = []
    name = re.split(r"[\s+·,，0-9-]", str(source or "").strip())[0][:2]
    for a, _, text in timeline:
        if name and len(name) == 2 and name in text and a > 1:
            out.append((a, "发生了什么"))
            break
    for a, _, text in timeline:
        if re.search(r"意味着|说明|关键|其实|反倒|我觉得|判断", text) and a > (out[-1][0] + 2 if out else 2):
            out.append((a, "关键判断"))
            break
    for a, _, text in timeline:
        if re.search(r"三条|三件事|三招|第一|做法|我们店|所以我们", text) and a > (out[-1][0] + 2 if out else 2):
            out.append((a, "怎么做"))
            break
    if timeline and timeline[-1][0] > (out[-1][0] + 2 if out else 2):
        out.append((timeline[-1][0], "你怎么看"))
    return out


def build(video: str, work: str, *, script: str, duration: float, silences: list,
          title: str, cover_title: str, cards: list[tuple[str, float, float]],
          keywords: list[str], nameplate: str, ai_label: str, font_path: str,
          source: str, out: str) -> tuple[list[str], dict]:
    """生成全部图层与音效，返回 (ffmpeg 参数, 统计)。"""
    timeline = align(script, duration, silences)
    # 标题条放视频标题（两行：白字 + 黄底黑字）；封面大字留给第 0 帧钩子，避免两处重复
    t = re.sub(r"[｜|]", "", title or cover_title or "")
    halves = re.split(r"[，,？?！!：:]", t, maxsplit=1)
    if len(halves) == 2 and halves[0] and halves[1] and len(halves[0]) <= 14:
        line1, line2 = halves[0], halves[1]
    else:
        cut = (len(t) + 1) // 2
        line1, line2 = t[:cut], t[cut:]
    chapters = chapters_for(timeline, source)
    title_path = os.path.join(work, "title.png")
    title_png(title_path, line1, line2, font_path, ai_label, chapters, duration)
    bar = os.path.join(work, "bar.png")
    bar_png(bar)

    blank_sub = os.path.join(work, "blank-sub.png")
    canvas_png(blank_sub, SUB_H)
    sub_entries = []
    for n, (a, b, text) in enumerate(timeline):
        p = os.path.join(work, f"sub{n}.png")
        subtitle_png(p, text, font_path, keywords)
        sub_entries.append((a, b, p))
    blank_card = os.path.join(work, "blank-card.png")
    card_frame_png(blank_card, None)
    card_entries = []
    for n, (path, a, b) in enumerate(cards):
        p = os.path.join(work, f"cardf{n}.png")
        card_frame_png(p, path)
        card_entries.append((a, b, p))
    mask = os.path.join(work, "pipmask.png")
    circle_mask_png(mask)
    blank_fx = os.path.join(work, "blank-fx.png")
    from PIL import Image
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank_fx, "PNG")
    fx_entries = []
    hook_end = min(1.6, duration * 0.2)
    if cover_title or title:
        p = os.path.join(work, "fx-hook.png")
        fx_frame(p, "hook", (cover_title or title).replace("｜", "").replace("|", ""), font_path)
        fx_entries.append((0.0, hook_end, p))
    if nameplate:
        p = os.path.join(work, "fx-name.png")
        fx_frame(p, "name", nameplate, font_path)
        fx_entries.append((max(hook_end, 2.0), min(5.0, duration - 4.0), p))
    cta_start = max(duration - 3.5, duration * 0.6)
    p = os.path.join(work, "fx-cta.png")
    fx_frame(p, "cta", "评论区说说", font_path)
    fx_entries.append((cta_start, duration, p))

    lists = {}
    for key, entries, blank in (("subs", sub_entries, blank_sub), ("cards", card_entries, blank_card),
                                ("fx", fx_entries, blank_fx)):
        lists[key] = os.path.join(work, f"{key}.txt")
        concat_list(track(entries, blank, duration), lists[key])

    hits = [(0.03, "pop")]
    hits += [(max(0.0, a - 0.12), "whoosh") for _, a, _ in cards]
    hits += [(t, "click") for t, _ in chapters]
    hits += [(max(0.0, cta_start), "ding")]
    for a, _, text in timeline:   # 数字句前 riser，尾巴对准句首
        if _NUM_RE.search(text) and a > 1.0:
            hits.append((a - 0.6, "riser"))
    hits = sorted(hits)
    sfx = os.path.join(work, "sfx.wav")
    n_sfx = write_sfx(sfx, hits, duration)

    concat_in = ["-f", "concat", "-safe", "0", "-protocol_whitelist", "file"]
    args = ["-threads", "2", "-i", video,
            "-loop", "1", "-t", f"{duration:.2f}", "-i", title_path,
            "-loop", "1", "-t", f"{duration:.2f}", "-i", bar,
            *concat_in, "-i", lists["subs"],
            *concat_in, "-i", lists["cards"],
            *concat_in, "-i", lists["fx"],
            "-i", sfx,
            "-loop", "1", "-t", f"{duration:.2f}", "-i", mask]
    z = zoom_expr(timeline)
    pip_enable = "+".join(f"between(t,{a},{b})" for _, a, b in cards) or "0"
    graph = ";".join([
        # 背景：同一画面放大虚化压暗铺满（顶/底不留死黑）；主画面 470–1560 放人物，脸在标题条之下
        f"[0:v]fps={FPS},split=2[src0][src1]",
        f"[src0]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        "boxblur=24:2,eq=brightness=-0.18:saturation=0.8,setsar=1[bg]",
        f"[src1]scale={W}:-2,crop={W}:'min(ih,{MAIN_H})':0:'max(0,(ih-{MAIN_H})*0.22)',"
        f"scale=w='trunc({W}*({z})/2)*2':h='trunc({MAIN_H}*({z})/2)*2':eval=frame,"
        f"crop={W}:{MAIN_H}:(iw-{W})/2:(ih-{MAIN_H})*0.25,setsar=1,split=2[main][pipsrc]",
        f"[bg][main]overlay=x=0:y={MAIN_Y}:shortest=1[base]",
        f"[base][4:v]overlay=x=0:y={CARD_Y}:eof_action=pass[v0c]",
        # 圆窗放整幅主画面（不猜脸在哪），保证脸一定在圈里
        f"[pipsrc]crop='min(iw,ih)':'min(iw,ih)',scale={PIP_SIZE}:{PIP_SIZE},format=rgba[pipr]",
        "[7:v]format=gray[pipm]",
        "[pipr][pipm]alphamerge[pip]",
        f"[v0c][pip]overlay=x={PIP_X}:y={PIP_Y}:shortest=1:enable='{pip_enable}'[v1]",
        "[v1][1:v]overlay=x=0:y=0:shortest=1[v2]",
        f"[v2][2:v]overlay=x='-w+w*t/{duration:.3f}':y={BAR_Y}:shortest=1[v3]",
        f"[v3][5:v]overlay=x=0:y=0:eof_action=pass[v4]",
        f"[v4][3:v]overlay=x=0:y={SUB_BOTTOM - SUB_H}:eof_action=pass[vout]",
        "[0:a]highpass=f=80,equalizer=f=3000:t=q:w=1:g=2.5,"
        "acompressor=threshold=-20dB:ratio=3:attack=5:release=80[voice]",
        "[6:a]volume=0.55[fxa]",
        "[voice][fxa]amix=inputs=2:normalize=0:duration=first,"
        "loudnorm=I=-15:TP=-1.5:LRA=11,aresample=44100[aout]",
    ])
    args += [
        "-filter_complex", graph, "-filter_complex_threads", "1",
        "-map", "[vout]", "-map", "[aout]",
        "-t", f"{duration:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-threads", "2", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out,
    ]
    return args, {"sentences": len(timeline), "cards": len(cards), "sfx": n_sfx,
                  "chapters": [c[1] for c in chapters]}


def silence_args(video: str) -> list[str]:
    return ["-nostats", "-i", video, "-vn", "-af", "silencedetect=n=-35dB:d=0.18", "-f", "null", "-"]


def detect_silences(video: str) -> list[tuple[float, float]]:
    """受限进程里跑 silencedetect；失败时返回空（退回按字数分配时间）。"""
    from . import textvideo
    import shutil
    ffmpeg = os.environ.get("CONTENTCREW_FFMPEG_PATH") or shutil.which("ffmpeg") or ""
    try:
        cmd = textvideo._bounded_media_command("audio", ffmpeg, ["-hide_banner", *silence_args(video)])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return parse_silences(result.stderr)
