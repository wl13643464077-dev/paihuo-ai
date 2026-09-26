"""门店排行（周 / 月）与老板早报里的门店部分（第 2 期）。

打分是确定性的，同样的数据永远得同样的分，公式可以原样讲给老板听：

    综合分 = 清单按时完成率 × 40% + 整改按时关闭率 × 30%
           + 派活按时完成率 × 20% + 巡店问题分 × 10%

- 清单按时完成率：这段时间截止的开店/闭店/交班清单里，在截止时间前全部打完勾的比例。
- 整改按时关闭率：这段时间到期的巡店整改（误报作废的不算）里，在期限内关闭、
  或在期限内提交了复查的比例。
- 派活按时完成率：这段时间到期的派给店员的活（取消的不算）里，在截止前交差的比例。
- 巡店问题分 = 100 − 10 × 平均每次巡店查出的问题数 × (100 ÷ 门店面积㎡)，
  没填面积的按 100㎡ 算（也就是按巡店次数归一），最低 0 分；误报的问题不算。
- 某一项这段时间没有数据（比如没巡过店），这一项不计入，其余几项按权重重新折算；
  四项都没有数据的门店不排名。
- 时间窗：周 = 截至现在的近 7 天，月 = 近 30 天；「较上期」与紧挨着的前一个同样长的
  时间窗比较。
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from . import checklist, db, timeutil

PERIODS = {"week": 7, "month": 30}
PERIOD_LABELS = {"week": "近 7 天", "month": "近 30 天"}
WEIGHTS = {"checklist": 0.4, "action": 0.3, "task": 0.2, "issue": 0.1}
COMPONENT_LABELS = {
    "checklist": "清单按时完成率",
    "action": "整改按时关闭率",
    "task": "派活按时完成率",
    "issue": "巡店问题分",
}
ISSUE_PENALTY = 10.0
REFERENCE_AREA = 100.0
AREA_MIN, AREA_MAX = 30.0, 5000.0
ATTENTION_SCORE = 60.0
DROP_ALERT = 5.0
FORMULA = (
    "综合分 = 清单按时完成率×40% + 整改按时关闭率×30% + 派活按时完成率×20% "
    "+ 巡店问题分×10%。巡店问题分 = 100 − 10 × 平均每次巡店查出的问题数 × "
    "(100 ÷ 门店面积㎡)，没填面积按 100㎡ 算，最低 0 分。某一项这段时间没有数据就不计入，"
    "其余几项按权重重新折算。"
)


class RankError(ValueError):
    status = 400


def period_days(period: str) -> int:
    if period not in PERIODS:
        raise RankError("排行周期只能是 week 或 month")
    return PERIODS[period]


# ---------------- 原始计数 ----------------
def _empty() -> dict:
    return {
        "cl_total": 0, "cl_on_time": 0, "cl_missed_by_kind": {},
        "ac_total": 0, "ac_on_time": 0,
        "tk_total": 0, "tk_on_time": 0,
        "visits": 0, "issues": 0,
    }


def collect(tid: int, start: float, end: float) -> dict[int, dict]:
    """[start, end) 内按门店汇总的原始计数（整租户一次查完，再按门店分）。"""
    out: dict[int, dict] = {}

    def slot(branch_id) -> dict:
        return out.setdefault(int(branch_id), _empty())

    for row in db.q(
        "SELECT branch_id,kind,COUNT(*) AS n,"
        "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS ok "
        "FROM checklist_run WHERE tenant_id=? AND due_at>=? AND due_at<? "
        "GROUP BY branch_id,kind",
        (int(tid), start, end),
    ):
        entry = slot(row["branch_id"])
        total, ok = int(row["n"] or 0), int(row["ok"] or 0)
        entry["cl_total"] += total
        entry["cl_on_time"] += ok
        if total > ok:
            kind = str(row.get("kind") or "custom")
            entry["cl_missed_by_kind"][kind] = entry["cl_missed_by_kind"].get(kind, 0) + total - ok
    for row in db.q(
        "SELECT v.branch_id,COUNT(*) AS n,SUM(CASE WHEN "
        "(a.status='closed' AND a.closed_at IS NOT NULL AND a.closed_at<=a.due_at) "
        "OR (a.status IN ('closed','awaiting_recheck') AND EXISTS("
        "SELECT 1 FROM inspection_recheck rc WHERE rc.tenant_id=a.tenant_id "
        "AND rc.action_id=a.id AND rc.created_at<=a.due_at)) THEN 1 ELSE 0 END) AS ok "
        "FROM inspection_action a JOIN inspection_visit v ON v.id=a.visit_id "
        "AND v.tenant_id=a.tenant_id AND v.deleted_at IS NULL "
        "WHERE a.tenant_id=? AND a.due_at>=? AND a.due_at<? "
        "AND COALESCE(a.close_reason,'')<>'false_positive' GROUP BY v.branch_id",
        (int(tid), start, end),
    ):
        entry = slot(row["branch_id"])
        entry["ac_total"] += int(row["n"] or 0)
        entry["ac_on_time"] += int(row["ok"] or 0)
    for row in db.q(
        "SELECT branch_id,COUNT(*) AS n,SUM(CASE WHEN status IN ('submitted','approved') "
        "AND submitted_at IS NOT NULL AND submitted_at<=due_at THEN 1 ELSE 0 END) AS ok "
        "FROM staff_task WHERE tenant_id=? AND branch_id IS NOT NULL "
        "AND deleted_at IS NULL AND status<>'cancelled' AND due_at>=? AND due_at<? "
        "GROUP BY branch_id",
        (int(tid), start, end),
    ):
        entry = slot(row["branch_id"])
        entry["tk_total"] += int(row["n"] or 0)
        entry["tk_on_time"] += int(row["ok"] or 0)
    for row in db.q(
        "SELECT v.branch_id,COUNT(*) AS visits,COALESCE(SUM(("
        "SELECT COUNT(*) FROM inspection_issue i WHERE i.tenant_id=v.tenant_id "
        "AND i.visit_id=v.id AND NOT EXISTS(SELECT 1 FROM inspection_action fa "
        "WHERE fa.tenant_id=i.tenant_id AND fa.issue_id=i.id "
        "AND fa.close_reason='false_positive'))),0) AS issues "
        "FROM inspection_visit v WHERE v.tenant_id=? AND v.status='completed' "
        "AND v.deleted_at IS NULL "
        "AND COALESCE(v.visit_at,v.completed_at,v.created_at)>=? "
        "AND COALESCE(v.visit_at,v.completed_at,v.created_at)<? GROUP BY v.branch_id",
        (int(tid), start, end),
    ):
        entry = slot(row["branch_id"])
        entry["visits"] += int(row["visits"] or 0)
        entry["issues"] += int(row["issues"] or 0)
    return out


# ---------------- 打分 ----------------
def _rate(ok: int, total: int) -> float | None:
    return round(ok * 100.0 / total, 1) if total else None


def issue_score(issues: int, visits: int, area_sqm: float | None) -> float | None:
    """巡店问题分；没巡过店返回 None。"""
    if not visits:
        return None
    area = REFERENCE_AREA
    try:
        if area_sqm is not None and float(area_sqm) > 0:
            area = min(AREA_MAX, max(AREA_MIN, float(area_sqm)))
    except (TypeError, ValueError):
        area = REFERENCE_AREA
    density = (issues / visits) * (REFERENCE_AREA / area)
    return round(max(0.0, min(100.0, 100.0 - ISSUE_PENALTY * density)), 1)


def components(raw: Mapping[str, Any], area_sqm: float | None) -> dict[str, dict]:
    return {
        "checklist": {"score": _rate(raw["cl_on_time"], raw["cl_total"]),
                      "done": raw["cl_on_time"], "total": raw["cl_total"]},
        "action": {"score": _rate(raw["ac_on_time"], raw["ac_total"]),
                   "done": raw["ac_on_time"], "total": raw["ac_total"]},
        "task": {"score": _rate(raw["tk_on_time"], raw["tk_total"]),
                 "done": raw["tk_on_time"], "total": raw["tk_total"]},
        "issue": {"score": issue_score(raw["issues"], raw["visits"], area_sqm),
                  "issues": raw["issues"], "visits": raw["visits"],
                  "area_sqm": area_sqm},
    }


def total_score(parts: Mapping[str, Mapping[str, Any]]) -> float | None:
    """有数据的分项按权重加权平均；全都没数据返回 None。"""
    weight = 0.0
    acc = 0.0
    for key, w in WEIGHTS.items():
        score = parts.get(key, {}).get("score")
        if score is None:
            continue
        weight += w
        acc += w * float(score)
    if weight <= 0:
        return None
    return round(acc / weight, 1)


def reasons(raw: Mapping[str, Any], parts: Mapping[str, Mapping[str, Any]],
            *, days: int, delta: float | None, overdue_actions: int = 0) -> list[str]:
    """需要关注的原因，大白话，最要紧的排前面。"""
    found: list[tuple[int, str]] = []
    span = f"近 {days} 天"
    for kind in ("close", "open", "handover", "custom"):
        n = int(raw["cl_missed_by_kind"].get(kind, 0))
        if n:
            found.append((n * 3, f"{span} {n} 次{checklist.KIND_LABELS[kind]}没按时做完"))
    if overdue_actions:
        found.append((overdue_actions * 3, f"{overdue_actions} 条巡店整改已超期还没关"))
    late_actions = raw["ac_total"] - raw["ac_on_time"]
    if late_actions and not overdue_actions:
        found.append((late_actions * 2, f"{span} {late_actions} 条巡店整改没按期关"))
    late_tasks = raw["tk_total"] - raw["tk_on_time"]
    if late_tasks:
        found.append((late_tasks * 2, f"{span} {late_tasks} 件派的活没按时交"))
    issue = parts["issue"]["score"]
    if issue is not None and issue < 80 and raw["visits"]:
        avg = raw["issues"] / raw["visits"]
        found.append((int(100 - issue) // 10, f"巡店平均每次查出 {avg:.1f} 个问题"))
    if delta is not None and delta <= -DROP_ALERT:
        found.append((int(-delta), f"比上期掉了 {abs(delta):.1f} 分"))
    found.sort(key=lambda item: -item[0])
    return [text for _, text in found]


def _branches(tid: int, branch_ids: list[int] | None) -> list[dict]:
    rows = db.q(
        "SELECT id,name,region,area_sqm FROM store_branch WHERE tenant_id=? "
        "AND active=1 ORDER BY id",
        (int(tid),),
    )
    if branch_ids is None:
        return rows
    allowed = {int(b) for b in branch_ids}
    return [row for row in rows if int(row["id"]) in allowed]


def _overdue_actions(tid: int, now: float) -> dict[int, int]:
    return {int(row["branch_id"]): int(row["n"] or 0) for row in db.q(
        "SELECT v.branch_id,COUNT(*) AS n FROM inspection_action a "
        "JOIN inspection_visit v ON v.id=a.visit_id AND v.tenant_id=a.tenant_id "
        "AND v.deleted_at IS NULL WHERE a.tenant_id=? "
        "AND a.status NOT IN ('closed','awaiting_recheck') AND a.due_at IS NOT NULL "
        "AND a.due_at<? GROUP BY v.branch_id",
        (int(tid), now),
    )}


def _rank(rows: list[dict]) -> None:
    """按分数从高到低排名；没分数的排最后不给名次；同分按门店名、id。"""
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0),
                             r["branch_name"], r["branch_id"]))
    rank = 0
    for row in rows:
        if row["score"] is None:
            row["rank"] = None
            continue
        rank += 1
        row["rank"] = rank


def compute(tid: int, period: str = "week", *, now: float | None = None,
            branch_ids: list[int] | None = None) -> dict:
    """算排行（不做权限判断）。branch_ids=None 表示全部启用门店。"""
    days = period_days(period)
    ts = time.time() if now is None else float(now)
    span = days * 86400
    start, prev_start = ts - span, ts - 2 * span
    branches = _branches(tid, branch_ids)
    current = collect(tid, start, ts)
    previous = collect(tid, prev_start, start)
    overdue = _overdue_actions(tid, ts)
    rows = []
    prev_rows = []
    for branch in branches:
        bid = int(branch["id"])
        area = branch.get("area_sqm")
        raw = current.get(bid) or _empty()
        parts = components(raw, area)
        score = total_score(parts)
        prev_parts = components(previous.get(bid) or _empty(), area)
        prev_score = total_score(prev_parts)
        delta = (round(score - prev_score, 1)
                 if score is not None and prev_score is not None else None)
        why = reasons(raw, parts, days=days, delta=delta,
                      overdue_actions=overdue.get(bid, 0))
        if score is None:
            why = ["这段时间还没有清单、整改、派活或巡店记录"]
        rows.append({
            "branch_id": bid,
            "branch_name": str(branch.get("name") or ""),
            "region": str(branch.get("region") or ""),
            "score": score,
            "prev_score": prev_score,
            "delta": delta,
            "components": {
                key: {**value, "label": COMPONENT_LABELS[key],
                      "weight": int(WEIGHTS[key] * 100)}
                for key, value in parts.items()
            },
            "reasons": why,
            "needs_attention": bool(score is not None and (
                score < ATTENTION_SCORE or (delta is not None and delta <= -DROP_ALERT)
                or raw["cl_missed_by_kind"] or overdue.get(bid, 0))),
        })
        prev_rows.append({"branch_id": bid, "branch_name": rows[-1]["branch_name"],
                          "score": prev_score})
    _rank(rows)
    _rank(prev_rows)
    prev_rank = {row["branch_id"]: row["rank"] for row in prev_rows}
    for row in rows:
        row["prev_rank"] = prev_rank.get(row["branch_id"])
        row["rank_change"] = (row["prev_rank"] - row["rank"]
                              if row["rank"] and row["prev_rank"] else None)
    return {
        "period": period,
        "period_label": PERIOD_LABELS[period],
        "start": start,
        "end": ts,
        "formula": FORMULA,
        "weights": {key: int(w * 100) for key, w in WEIGHTS.items()},
        "stores": rows,
    }


def ranking(tid: int, uid: int, period: str = "week", *,
            now: float | None = None) -> dict:
    """门店排行接口：老板/总监看全部门店，经理/员工只看绑定门店。"""
    user = checklist._user(tid, uid)
    visible = checklist.visible_branch_ids(tid, user)
    return compute(tid, period, now=now, branch_ids=visible)


# ---------------- 老板早报里的门店部分 ----------------
def morning_brief(tid: int, now: float | None = None) -> dict:
    """昨天各店清单完成率、逾期点名、排行前 3/后 3、今天等老板处理的事。"""
    ts = time.time() if now is None else float(now)
    branches = {int(b["id"]): str(b.get("name") or "") for b in _branches(tid, None)}
    empty = {"has_content": False, "lines": [], "short": ""}
    if not branches:
        return empty
    yesterday = timeutil.today_cn(timeutil.cn_day_start_ts(ts) - 1)
    runs: dict[int, list[int]] = {}
    for row in db.q(
        "SELECT branch_id,COUNT(*) AS n,SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS ok "
        "FROM checklist_run WHERE tenant_id=? AND run_date=? GROUP BY branch_id",
        (int(tid), yesterday),
    ):
        if int(row["branch_id"]) in branches:
            runs[int(row["branch_id"])] = [int(row["ok"] or 0), int(row["n"] or 0)]
    total = sum(v[1] for v in runs.values())
    done = sum(v[0] for v in runs.values())
    lines: list[str] = []
    checklist_rate = round(done * 100.0 / total) if total else None
    if total:
        weak = sorted(
            ((bid, ok, n) for bid, (ok, n) in runs.items() if ok < n),
            key=lambda item: (item[1] / item[2], branches[item[0]]),
        )
        tail = "、".join(f"{branches[bid]} {ok}/{n}" for bid, ok, n in weak[:3])
        lines.append(f"🏪 昨天清单按时完成 {checklist_rate}%（{done}/{total}）"
                     + (f"，没做完的：{tail}" if tail else "，全部按时做完"))
    tasks_over: dict[int, int] = {}
    for row in db.q(
        "SELECT branch_id,COUNT(*) AS n FROM staff_task WHERE tenant_id=? "
        "AND status='todo' AND deleted_at IS NULL AND due_at IS NOT NULL AND due_at<? "
        "GROUP BY branch_id",
        (int(tid), ts),
    ):
        key = int(row["branch_id"]) if row.get("branch_id") else 0
        if key and key not in branches:
            continue                      # 已停用门店的不点名
        tasks_over[key] = tasks_over.get(key, 0) + int(row["n"] or 0)
    actions_over = {bid: n for bid, n in _overdue_actions(tid, ts).items() if bid in branches}
    overdue_parts = []
    for bid in sorted(set(tasks_over) | set(actions_over),
                      key=lambda b: (-(tasks_over.get(b, 0) + actions_over.get(b, 0)), b)):
        bits = []
        if tasks_over.get(bid):
            bits.append(f"{tasks_over[bid]} 件派活")
        if actions_over.get(bid):
            bits.append(f"{actions_over[bid]} 条整改")
        overdue_parts.append(f"{branches.get(bid, '没指定门店')} " + "、".join(bits))
    overdue_total = sum(tasks_over.values()) + sum(actions_over.values())
    if overdue_parts:
        lines.append("⏰ 逾期没做完：" + "；".join(overdue_parts[:5])
                     + (f" 等 {len(overdue_parts)} 家" if len(overdue_parts) > 5 else ""))
    rank = compute(tid, "week", now=ts)
    scored = [row for row in rank["stores"] if row["score"] is not None]
    top = scored[:3]
    bottom = scored[3:][-3:] if len(scored) > 3 else []
    if top:
        lines.append("🏆 门店排行（近 7 天）前 3：" + "、".join(
            f"{row['branch_name']} {row['score']:.0f} 分" for row in top))
    if bottom:
        lines.append("⚠️ 后 3：" + "、".join(
            f"{row['branch_name']} {row['score']:.0f} 分"
            + (f"（{row['reasons'][0]}）" if row["reasons"] else "")
            for row in reversed(bottom)))
    submitted = int((db.one(
        "SELECT COUNT(*) AS n FROM staff_task WHERE tenant_id=? AND status='submitted' "
        "AND deleted_at IS NULL",
        (int(tid),),
    ) or {}).get("n") or 0)
    rechecks = int((db.one(
        "SELECT COUNT(*) AS n FROM inspection_action a JOIN inspection_visit v "
        "ON v.id=a.visit_id AND v.tenant_id=a.tenant_id AND v.deleted_at IS NULL "
        "WHERE a.tenant_id=? AND a.status='awaiting_recheck'",
        (int(tid),),
    ) or {}).get("n") or 0)
    waiting = []
    if submitted:
        waiting.append(f"{submitted} 件店员交的活等您验收")
    if rechecks:
        waiting.append(f"{rechecks} 条整改等您复查")
    if waiting:
        lines.append("📌 今天等您处理：" + "、".join(waiting))
    short_bits = []
    if checklist_rate is not None:
        short_bits.append(f"昨天清单按时完成 {checklist_rate}%")
    if overdue_total:
        short_bits.append(f"逾期 {overdue_total} 件")
    if waiting:
        short_bits.append("等您处理 " + str(submitted + rechecks) + " 件")
    return {
        "has_content": bool(total or overdue_total or submitted or rechecks),
        "date": yesterday,
        "checklist_rate": checklist_rate,
        "checklist": {"done": done, "total": total},
        "overdue_total": overdue_total,
        "top": [{"branch_name": r["branch_name"], "score": r["score"]} for r in top],
        "bottom": [{"branch_name": r["branch_name"], "score": r["score"],
                    "reasons": r["reasons"]} for r in bottom],
        "waiting": {"tasks_to_review": submitted, "rechecks_to_review": rechecks},
        "lines": lines,
        "short": "门店：" + "，".join(short_bits) if short_bits else "",
    }
