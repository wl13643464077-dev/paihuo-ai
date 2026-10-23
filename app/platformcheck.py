"""三平台发布前体检（移植自「内容流水线」v3 的 platform_check.py）。

一核三版：抖音、视频号、小红书各有自己的字数、开头、标签和审核红线。
分发官产出每个平台版本后，这里逐条体检：
- red    必须改，否则容易被限流/判违规（网址、联系方式、私信导流、其他平台名、禁用开头）
- yellow 建议改（字数、标签数、来源、收藏/转发理由、绝对化用语）

纯规则、零调用成本；只读版本内容，不修改任何字段。
"""
from __future__ import annotations

import re

URL = re.compile(r"https?://|www\.|\.com\b|\.cn\b|\.htm", re.IGNORECASE)
CONTACT = re.compile(
    r"微信号|加微信|vx|VX|v信|wx[:：]|QQ|qq号|手机号|电话[:：]?\s*\d|邮箱|二维码|加我|"
    r"私信我?领|私信.{0,4}(发|送|领)|扣\s*1|评论区.{0,4}(领|发你)"
)
OTHER_PLATFORM = re.compile(r"抖音号|视频号ID|快手号|bilibili|公众号[:：]", re.IGNORECASE)
# 小红书最严：事件本身涉及平台时改写成「某短视频平台」「某二手平台」。
OTHER_PLATFORM_XHS = re.compile(r"抖音|视频号|快手|B站|bilibili|淘宝|拼多多|闲鱼", re.IGNORECASE)
BAD_OPENING = re.compile(r"^(据|今天|大家好|最近|20\d\d年|\d{1,2}月\d{1,2}日)")
ABSOLUTE_WORDS = ("唯一", "首个", "顶级", "100%", "绝对", "保证", "永久", "全网最低",
                  "稳赚", "第一", "最强", "最好", "最佳", "最权威")
INFLAMMATORY = ("不配", "全网都在骂", "赋能")

# 平台: (标题下限, 标题上限, 正文建议上限, 正文建议下限, 标签下限, 标签上限, 口播字数区间)
LIMITS = {
    "抖音": (6, 30, 160, 0, 4, 6, (180, 320)),
    "视频号": (6, 16, 400, 0, 3, 5, (300, 480)),
    "小红书": (6, 20, 700, 250, 8, 10, None),
}
XHS_CARDS_RANGE = (6, 9)


def _tags(version: dict) -> list[str]:
    raw = version.get("tags") or []
    if isinstance(raw, str):
        raw = re.findall(r"#?([^\s#]+)", raw)
    tags = [str(tag).strip().lstrip("#") for tag in raw if str(tag).strip()]
    body_tags = re.findall(r"#([^\s#]+)", str(version.get("body") or ""))
    return list(dict.fromkeys(tags + body_tags))


def _plain_len(text: str) -> int:
    return len(re.sub(r"\s", "", str(text or "")))


def _card_text(cards) -> str:
    parts = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        for value in card.values():
            if isinstance(value, (list, tuple)):
                parts.extend(str(item) for item in value)
            elif isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def check_version(version: dict) -> list[dict]:
    """体检一个平台版本，返回 ``[{"level": "red"|"yellow", "msg": str}]``。"""
    platform = str(version.get("platform") or "").strip()
    issues: list[dict] = []

    def red(msg):
        issues.append({"level": "red", "msg": msg})

    def yellow(msg):
        issues.append({"level": "yellow", "msg": msg})

    title = str(version.get("title") or "").strip().splitlines()[0:1]
    title = title[0] if title else ""
    body = str(version.get("body") or "")
    body_lines = [line for line in body.splitlines()
                  if not line.strip().startswith(("来源", "信息来源", "本视频", "AI", "#"))]
    body_main = "\n".join(body_lines).strip()
    tags = _tags(version)
    first_comment = str(version.get("first_comment") or "")
    script = str(version.get("script") or "")
    cards = version.get("cards") if isinstance(version.get("cards"), list) else []
    public_text = "\n".join([title, body, first_comment, " ".join(tags), _card_text(cards)])

    # 通用红线：对外公开文案一律不贴链接、不留联系方式、不做私信导流。
    if platform in LIMITS or platform == "微博":
        if URL.search(public_text):
            red("出现网址：公开文案不贴链接，来源写「媒体名 + 日期」")
        contact = CONTACT.search(public_text)
        if contact:
            red(f"疑似联系方式/私信导流：「{contact.group(0)}」，改成评论区提问")
    for word in INFLAMMATORY:
        if word in public_text:
            red(f"引战/禁用词：「{word}」")
    if platform not in LIMITS:
        for word in ABSOLUTE_WORDS:
            if word in title + body_main:
                yellow(f"绝对化用语：「{word}」（确认不是广告语境）")
                break
        return issues

    tmin, tmax, bmax, bmin, gmin, gmax, script_range = LIMITS[platform]
    other = (OTHER_PLATFORM_XHS if platform == "小红书" else OTHER_PLATFORM).search(public_text)
    if other:
        red(f"出现其他平台信息：「{other.group(0)}」")
    if not title:
        red("缺标题")
    elif not tmin <= len(title) <= tmax:
        red(f"标题 {len(title)} 字，{platform}要 {tmin}–{tmax} 字")
    length = _plain_len(body_main)
    if bmax and length > bmax:
        yellow(f"正文 {length} 字，{platform}建议 ≤{bmax} 字")
    if bmin and length < bmin:
        yellow(f"正文只有 {length} 字，{platform}建议 {bmin}–{bmax} 字")
    if not gmin <= len(tags) <= gmax:
        yellow(f"标签 {len(tags)} 个，{platform}建议 {gmin}–{gmax} 个")
    for word in ABSOLUTE_WORDS:
        if word in title + body_main:
            yellow(f"绝对化用语：「{word}」（确认不是广告语境）")
            break
    if not re.search(r"(来源|据).{2,}", body):
        yellow("没写信息来源：涉及新闻/政策/数据时写「媒体名 + 日期」，放在末尾")

    if platform in ("抖音", "视频号"):
        first = body_main.splitlines()[0].strip() if body_main else ""
        opening = BAD_OPENING.match(first)
        if opening:
            red(f"描述第一句以「{opening.group(0)}」开头：第一句要是判断/冲突，来源放最后")
        if not first_comment:
            yellow("缺首评：准备一个让人想回答的问题")
        if script:
            count = _plain_len(script)
            low, high = script_range
            if not low <= count <= high:
                yellow(f"口播稿 {count} 字（约 {count / 6:.0f} 秒），{platform}建议 {low}–{high} 字")
            opening = BAD_OPENING.match(script.strip())
            if opening:
                red(f"口播第一句以「{opening.group(0)}」开头：前 3 秒要是结论/冲突")
        else:
            yellow(f"缺口播稿：{platform}是视频平台，需要单独的口播稿")
    if platform == "视频号":
        reason = str(version.get("forward_reason") or "")
        if not re.search(r"转给|发给|转发", public_text + reason):
            yellow("视频号没写转发理由（例：转给你那个正在谈加盟的合伙人）")
    if platform == "小红书":
        if "收藏" not in public_text:
            yellow("小红书没给收藏理由（例：收藏起来，下次对一遍）")
        if cards:
            low, high = XHS_CARDS_RANGE
            if not low <= len(cards) <= high:
                yellow(f"图文卡片 {len(cards)} 张，建议 {low}–{high} 张")
        else:
            yellow("小红书没有图文卡片：小红书用户是来搜和存的，图文比口播视频更容易被收藏")
    return issues


def summarize(issues: list[dict]) -> dict:
    reds = sum(1 for item in issues if item.get("level") == "red")
    yellows = sum(1 for item in issues if item.get("level") == "yellow")
    return {"red": reds, "yellow": yellows, "ok": reds == 0}


def check_versions(versions: list) -> list:
    """给每个平台版本附上 ``checks`` 与 ``check_summary``；返回新列表，不改原对象。"""
    out = []
    for version in versions or []:
        if not isinstance(version, dict):
            continue
        issues = check_version(version)
        out.append({**version, "checks": issues, "check_summary": summarize(issues)})
    return out
