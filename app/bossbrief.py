"""老板一眼看懂层：所有 AI 交付统一成「一句话结论 + 3 条行动 + 风险提醒」。

实体店老板不读长报告。专家任务、会议结论都落成同一种速览结构，
再序列化成一段固定格式的 Markdown 存进原有 ``summary_md`` 字段
（不改表结构；旧页面按 Markdown 渲染也能看，新页面按标签解析成结论卡）：

    **一句话结论**：……

    **今天/本周就做这 3 件事**
    1. ……
    2. ……
    3. ……

    **⚠️ 要留意**：……

    **补充说明**
    - ……

另外提供专家任务等待期间的「大白话阶段进度」：只输出阶段名、已用时和
预计剩余时间，不透出内部步骤文字、模型名或提示词。

本模块只依赖 db（仅 ``typical_task_seconds`` 用），其余全是纯函数，便于测试。
"""
from __future__ import annotations

import re
import statistics
import time

VERDICT_LABEL = "一句话结论"
ACTIONS_LABEL = "今天/本周就做这 3 件事"
WATCH_LABEL = "要留意"
EXTRA_LABEL = "补充说明"
ACTION_MAX = 40
VERDICT_MAX = 80
WATCH_MAX = 80
# 兜底实在抽不出行动时的最后一条：至少告诉老板下一步该干什么。
DEFAULT_ACTION = "老板：花 3 分钟看完整报告，挑一件今天就能做的事交给店员"

_LIST_RE = re.compile(r"^\s{0,3}(?:[-*+•·]|\d{1,2}[.、)）]|[（(]\d{1,2}[)）])\s*(.+)$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")
_BOLD_HEADING_RE = re.compile(r"^\s*\*\*([^*]{1,30})\*\*\s*[：:]?\s*$")
_ACTION_HEADING = re.compile(r"下一步|行动|怎么做|执行|落地|建议|待办|清单|步骤|动作|计划|安排")
_NEXT_HEADING = re.compile(r"下一步")
_VERDICT_HEADING = re.compile(r"结论|总结|摘要|一句话|核心|判断|概要|要点|概览")
_WATCH_HEADING = re.compile(r"风险|注意|提醒|避坑|警惕|隐患|雷区|留意")


# ---------------------------------------------------------------- 文本清洗
def clean_text(value) -> str:
    """去掉 Markdown 标记、多余空白和列表前缀，只留一行干净的中文。"""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    text = str(value)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]{1,80}>", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = " ".join(text.split())
    text = re.sub(r"^(?:[-*+•·]|\d{1,2}[.、)）]|[（(]\d{1,2}[)）])\s*", "", text)
    text = re.sub(r"^[👉✅⚠️❗️🔥📌💡]+\s*", "", text)
    return text.strip(" ：:;；")


def shorten(text: str, limit: int) -> str:
    """超长时优先在句读处截断，实在不行硬截并加省略号。"""
    text = clean_text(text)
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(mark) for mark in "。；;！!？?")
    if cut >= limit * 0.5:
        return head[:cut + 1].rstrip("；;")
    return text[:limit - 1].rstrip("，,、 ") + "…"


def _first_sentence(text: str) -> str:
    text = clean_text(text)
    match = re.match(r"(.+?[。！!？?])", text)
    return match.group(1) if match else text


def as_items(raw, limit: int, item_limit: int) -> list[str]:
    """把模型给的列表字段规整成若干条短句。

    模型偶尔把列表返回成一整段字符串；直接迭代会被逐字拆成单字条目。
    字符串按换行/分号切条，并去掉常见的列表前缀。
    """
    if isinstance(raw, str):
        items = re.split(r"[\n；;]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = []
    out: list[str] = []
    for item in items:
        if isinstance(item, (dict, list, tuple)) or item is None:
            continue
        text = shorten(item, item_limit)
        if text and text not in out:
            out.append(text)
    return out[:limit]


# ---------------------------------------------------------------- 序列化 / 解析
def format_brief(verdict: str, actions, watch: str = "", extra=()) -> str:
    """把速览结构写成固定格式 Markdown（存 summary_md）。"""
    verdict = shorten(verdict, VERDICT_MAX)
    actions = [a for a in (shorten(x, ACTION_MAX) for x in actions or ()) if a][:3]
    watch = shorten(watch, WATCH_MAX)
    parts = []
    if verdict:
        parts.append(f"**{VERDICT_LABEL}**：{verdict}")
    if actions:
        parts.append(
            f"**{ACTIONS_LABEL}**\n"
            + "\n".join(f"{i}. {a}" for i, a in enumerate(actions, 1))
        )
    if watch:
        parts.append(f"**⚠️ {WATCH_LABEL}**：{watch}")
    extra_lines = [" ".join(str(x).split()) for x in extra or () if str(x).strip()]
    if extra_lines:
        parts.append(f"**{EXTRA_LABEL}**\n" + "\n".join(f"- {x}" for x in extra_lines))
    return "\n\n".join(parts)


def parse_brief(summary_md: str) -> dict:
    """``format_brief`` 的逆过程；兼容旧版「要点 + 👉 行动建议」速览。"""
    result = {"verdict": "", "actions": [], "watch": "", "extra": []}
    section = ""
    legacy_points: list[str] = []
    for raw in str(summary_md or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        bare = line.replace("**", "")
        if bare.startswith(VERDICT_LABEL):
            result["verdict"] = clean_text(bare[len(VERDICT_LABEL):])
            section = ""
            continue
        if bare.startswith(ACTIONS_LABEL):
            section = "actions"
            continue
        if WATCH_LABEL in bare[:8]:
            result["watch"] = clean_text(bare.split(WATCH_LABEL, 1)[1])
            section = ""
            continue
        if bare.startswith(EXTRA_LABEL):
            section = "extra"
            continue
        legacy_action = re.search(r"(?:一句话行动建议|下一步)\s*[：:]\s*(.+)$", bare)
        if legacy_action:
            result["actions"].append(clean_text(legacy_action.group(1)))
            continue
        item = clean_text(line)
        if not item:
            continue
        if section == "actions" and _LIST_RE.match(line):
            result["actions"].append(item)
        elif section == "extra":
            result["extra"].append(item)
        else:
            legacy_points.append(item)
    if not result["verdict"] and legacy_points:
        result["verdict"] = legacy_points.pop(0)
    result["extra"] = legacy_points + result["extra"]
    return result


def one_line(summary_md: str, limit: int = 300) -> str:
    """速览压成一行（会议共识里引用派生任务结果时用）。"""
    brief = parse_brief(summary_md)
    parts = []
    if brief["verdict"]:
        parts.append(brief["verdict"])
    if brief["actions"]:
        parts.append("要做：" + "；".join(brief["actions"]))
    if brief["watch"]:
        parts.append("留意：" + brief["watch"])
    text = "｜".join(parts) or " ".join(str(summary_md or "").split())
    return text[:limit]


# ---------------------------------------------------------------- 正文规则兜底
def _sections(md: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_code = False
    for raw in str(md or "").replace("\r", "").split("\n"):
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        heading = _HEADING_RE.match(raw) or _BOLD_HEADING_RE.match(raw)
        if heading:
            sections.append((clean_text(heading.group(1)), []))
            continue
        sections[-1][1].append(raw)
    return sections


def _list_items(lines: list[str]) -> list[str]:
    items = []
    for line in lines:
        if line.lstrip().startswith("|"):
            continue
        match = _LIST_RE.match(line)
        if match:
            text = clean_text(match.group(1))
            if len(text) >= 4:
                items.append(text)
    return items


def _paragraphs(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith(("|", ">", "---", "<!--"))
            or _LIST_RE.match(line)
        ):
            continue
        text = clean_text(stripped)
        if len(text) >= 6:
            out.append(text)
    return out


def fallback_brief(md: str, title: str = "") -> dict:
    """模型速览失败时，从正文按规则抽「结论 + 行动 + 风险」，保证总有行动。"""
    sections = _sections(md)
    headings = [h for h, _ in sections]
    doc_title = next((h for h in headings if h), "") or clean_text(title)

    verdict = ""
    for heading, lines in sections:
        if heading and _VERDICT_HEADING.search(heading) and not _ACTION_HEADING.search(heading):
            candidates = _paragraphs(lines) or _list_items(lines)
            if candidates:
                verdict = _first_sentence(candidates[0])
                break
    if not verdict:
        for heading, lines in sections:
            candidates = _paragraphs(lines)
            if candidates:
                verdict = _first_sentence(candidates[0])
                break
    if not verdict:
        verdict = doc_title or "报告已完成，重点见下方行动"

    # 行动优先级：「下一步」章节 → 其他行动/建议类章节 → 全文列表项。
    ordered = (
        [lines for h, lines in sections if h and _NEXT_HEADING.search(h)]
        + [lines for h, lines in sections
           if h and _ACTION_HEADING.search(h) and not _NEXT_HEADING.search(h)
           and not _WATCH_HEADING.search(h)]
    )
    actions: list[str] = []
    for lines in ordered:
        for item in _list_items(lines) or _paragraphs(lines):
            text = shorten(item, ACTION_MAX)
            if text and text not in actions:
                actions.append(text)
            if len(actions) >= 3:
                break
        if len(actions) >= 3:
            break
    if len(actions) < 3:
        for heading, lines in reversed(sections):
            if heading and (_WATCH_HEADING.search(heading) or _VERDICT_HEADING.search(heading)):
                continue
            for item in _list_items(lines):
                text = shorten(item, ACTION_MAX)
                if text and text not in actions:
                    actions.append(text)
                if len(actions) >= 3:
                    break
            if len(actions) >= 3:
                break
    if not actions:
        actions = [DEFAULT_ACTION]

    watch = ""
    for heading, lines in sections:
        if heading and _WATCH_HEADING.search(heading):
            candidates = _list_items(lines) or _paragraphs(lines)
            if candidates:
                watch = candidates[0]
                break
    return {
        "verdict": shorten(verdict, VERDICT_MAX),
        "actions": actions[:3],
        "watch": shorten(watch, WATCH_MAX),
    }


def merge_model_brief(data, md: str, title: str = "") -> tuple[dict, bool]:
    """模型给的速览优先；缺结论或行动不足 3 条时用正文规则补齐。

    返回 (速览, 是否用到了模型结果)。兼容旧提示词的 points/action 字段。
    """
    data = data if isinstance(data, dict) else {}
    verdict = shorten(data.get("verdict") or data.get("action") or "", VERDICT_MAX)
    actions = as_items(data.get("actions"), 3, ACTION_MAX)
    watch = shorten(data.get("watch") or data.get("risk") or "", WATCH_MAX)
    used_model = bool(verdict or actions)
    fallback = fallback_brief(md, title)
    if not verdict:
        verdict = fallback["verdict"]
    for item in fallback["actions"]:
        if len(actions) >= 3:
            break
        if item not in actions and (item != DEFAULT_ACTION or not actions):
            actions.append(item)
    if not watch:
        watch = fallback["watch"]
    return {"verdict": verdict, "actions": actions[:3], "watch": watch}, used_model


# ---------------------------------------------------------------- V2 决策员工
_DECISION_VERDICT = {
    "GO": "可以做（GO）：专家建议可行，但要您亲自审批后才执行",
    "HOLD": "先别动（HOLD）：资料还不够，专家暂不下结论",
    "ESCALATE": "要请懂行的人拍板（ESCALATE）：涉及资质、法规或重大风险",
    "ADVISE": "仅供参考（ADVISE）：专家给的是分析建议，不是执行指令",
}
_DECISION_ACTIONS = {
    "GO": (
        "老板：看完整报告里的证据，确认没问题再审批",
        "店长：审批通过后按报告步骤手动执行",
        "店员：执行后把结果数据记下来，方便复盘",
    ),
    "HOLD": (
        "老板：先补齐缺的资料{gap}",
        "老板：资料补齐后重新派给专家复核",
        "复核前不要按报告改价、下单或调整人手",
    ),
    "ESCALATE": (
        "老板：把报告转给有资质的负责人或专业顾问",
        "负责人：看完风险点后再决定做不做",
        "决定前不要按报告执行任何操作",
    ),
    "ADVISE": (
        "老板：看报告里的分析，挑有用的记下来",
        "要落地的事，先和店长商量再定",
        "需要执行时，再派专家出正式方案",
    ),
}


def decision_brief(status: str, gap_detail: str = "") -> dict:
    """V2 决策员工的速览由门禁状态决定，不交给二次模型改写。"""
    status = status if status in _DECISION_VERDICT else "HOLD"
    gap = shorten(gap_detail, 16)
    actions = [
        item.format(gap=f"：{gap}" if gap else "")
        for item in _DECISION_ACTIONS[status]
    ]
    return {
        "verdict": _DECISION_VERDICT[status],
        "actions": [shorten(a, ACTION_MAX) for a in actions],
        "watch": "系统不会自动执行任何操作；GO 也只代表可以进入您审批",
    }


# ---------------------------------------------------------------- 会议
MEETING_DECISION_WORD = {
    "GO": "可以干",
    "NO_GO": "不建议干",
    "NEED_INFO": "还差关键信息，先补齐再定",
}


def meeting_brief(
    decision: str,
    selected: dict | None,
    summary: str,
    next_action: str,
    actions=(),
    validations=(),
) -> dict:
    """会议结论映射成同一结构：结论 + 最多 3 条行动 + 风险。"""
    word = MEETING_DECISION_WORD.get(str(decision or ""), "待定")
    summary_text = clean_text(summary) or "关键证据尚未补齐"
    verdict = f"{word}：{summary_text}"
    items: list[str] = []
    for action in actions or ():
        if not isinstance(action, dict):
            continue
        task = clean_text(action.get("task"))
        who = clean_text(action.get("who"))
        if not task:
            continue
        text = shorten(f"{who}：{task}" if who else task, ACTION_MAX)
        if text not in items:
            items.append(text)
        if len(items) >= 3:
            break
    next_text = shorten(next_action, ACTION_MAX)
    if next_text and len(items) < 3 and not any(
        clean_text(next_action)[:12] in clean_text(a.get("task"))
        for a in actions or () if isinstance(a, dict)
    ):
        items.append(next_text)
    if not items:
        items = [next_text or "先停一停，换一个更小、更好验证的问题再开会"]
    watch = ""
    if isinstance(selected, dict):
        watch = clean_text(selected.get("risk"))
    if not watch:
        ordered = sorted(
            (v for v in validations or () if isinstance(v, dict)),
            key=lambda v: str(v.get("verdict") or "").upper() != "FAIL",
        )
        for validation in ordered:
            watch = clean_text(validation.get("fatal_risk"))
            if watch:
                break
    extra = []
    if isinstance(selected, dict) and clean_text(selected.get("title")):
        extra.append(f"选定方案：{clean_text(selected.get('title'))}")
    return {
        "verdict": shorten(verdict, VERDICT_MAX),
        "actions": items[:3],
        "watch": shorten(watch, WATCH_MAX),
        "extra": extra,
    }


# ---------------------------------------------------------------- 等待期阶段进度
STAGES = ("正在查资料", "正在写方案", "正在检查", "马上好")
_DEFAULT_SECONDS = {"lite": 300, "std": 600, "full": 900}
_TARGET_CHARS = {"lite": 800, "std": 2000, "full": 4000}


def _step_stage(step: dict, length: str) -> int:
    kind = str(step.get("k") or "").lower()
    label = str(step.get("l") or "")
    if kind == "done":
        return 3
    if kind in {"run", "review", "gate"}:
        return 2
    if kind == "typing":
        match = re.search(r"已写\s*(\d+)\s*字", label)
        target = _TARGET_CHARS.get(length or "", 2000)
        if match and int(match.group(1)) >= target * 0.9:
            return 2
        return 1
    if kind == "tool" and re.search(r"交给|撰写|思考|推理", label):
        return 1
    if kind == "retry" and re.search(r"重写|修复|格式", label):
        return 2
    return 0


def task_stage_progress(
    steps,
    status: str,
    created_at: float | None,
    *,
    now: float | None = None,
    typical_seconds: float | None = None,
    length: str = "",
) -> dict | None:
    """把内部步骤映射成老板看得懂的 4 段进度（只在排队/进行中返回）。

    只输出阶段序号、固定阶段名与时间估计；步骤原文（可能含模型名、
    检索词、提示词片段）一律不出本函数。
    """
    status = str(status or "")
    if status not in {"queued", "running"}:
        return None
    now = time.time() if now is None else float(now)
    start = now if created_at is None else float(created_at)
    elapsed = max(0, int(now - start))
    typical = int(typical_seconds or _DEFAULT_SECONDS.get(length or "", 600))
    typical = max(120, typical)
    stage = -1
    if status == "running":
        stage = 0
        for step in steps or ():
            if isinstance(step, dict):
                stage = max(stage, _step_stage(step, length))
    if status == "queued":
        label = "排队中，马上开工"
    else:
        label = STAGES[stage]
    remaining = typical - elapsed
    if stage == 3:
        remaining = min(max(remaining, 20), 60)
    elif stage == 2:
        remaining = max(remaining, 60)
    if remaining >= 30:
        minutes = max(1, round(remaining / 60))
        eta = int(remaining)
        hint = f"预计还要约 {minutes} 分钟"
    else:
        eta = None
        hint = "比平时多花了点时间，还在认真做，做完会通知您"
    return {
        "stages": list(STAGES),
        "current": stage,
        "label": label,
        "elapsed_seconds": elapsed,
        "eta_seconds": eta,
        "hint": hint,
    }


def typical_task_seconds(tenant_id: int, emp_idx: int, length: str = "") -> int:
    """同一员工最近已交付任务的耗时中位数；样本不足时按篇幅给默认值。"""
    from . import db

    default = _DEFAULT_SECONDS.get(length or "", 600)
    try:
        rows = db.q(
            "SELECT created_at,terminal_at FROM task WHERE tenant_id=? AND emp_idx=? "
            "AND status='done' AND terminal_at IS NOT NULL AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 20",
            (int(tenant_id), int(emp_idx)),
        )
    except Exception:
        return default
    spans = [
        float(r["terminal_at"]) - float(r["created_at"])
        for r in rows
        if r.get("terminal_at") and r.get("created_at")
        and 30 <= float(r["terminal_at"]) - float(r["created_at"]) <= 3 * 3600
    ]
    if len(spans) < 3:
        return default
    return int(statistics.median(spans))
