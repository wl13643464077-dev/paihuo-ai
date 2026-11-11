"""老板数据驾驶舱：一屏看清「员工用派活做了什么、门店执行得怎样、哪些事等老板拍板」。

口径原则（给老板讲得清楚）：
- 只读派活自己的业务表（任务、内容工单、数字人/成片、工具、会议、店员任务、清单、
  巡店、点数流水、发布台账），不接任何外部数据，也不估算“省了多少钱/工时”。
- 时间窗 = 截至现在的近 N 天；「较上期」对比紧挨着的前一个同样长的时间窗。
- 只看当前企业（租户），只给老板（owner/root）看。
- 下发：用派活已有的「派给店员」任务（staff_task），来源记为 cockpit:…，
  所以下发出去的事在店员端照常拍照交差，驾驶舱里能追踪状态。
"""
from __future__ import annotations

import io
import json
import time
from typing import Any, Callable, Mapping

from . import db, departments, storerank, timeutil

PERIODS = (7, 30, 90)
DEFAULT_DAYS = 7
AI_ACTION = "cockpit_ai"
AI_TIMEOUT_S = 90
SOURCE_PREFIX = "cockpit:"
IDLE_DAYS = 7

DONE = ("done", "completed", "success", "succeed")
WAITING = ("awaiting_review", "gate_blocked", "awaiting_execution", "paused", "blocked")
FAILED = ("failed", "error")
ROLE_LABELS = {"owner": "老板", "root": "老板", "director": "总监",
               "manager": "店长", "staff": "店员"}
SEVERITY_LABELS = {"critical": "严重", "high": "高", "medium": "中", "low": "低"}


class CockpitError(ValueError):
    status = 400


def _in(values) -> str:
    return ",".join("'" + v + "'" for v in values)


def _days(days: Any) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        raise CockpitError("周期只能是 7、30 或 90 天") from None
    if value not in PERIODS:
        raise CockpitError("周期只能是 7、30 或 90 天")
    return value


def _n(sql: str, args: tuple) -> int:
    row = db.one(sql, args) or {}
    return int(next(iter(row.values()), 0) or 0) if row else 0


def _f(sql: str, args: tuple) -> float:
    row = db.one(sql, args) or {}
    return float(next(iter(row.values()), 0) or 0) if row else 0.0


def _rate(ok: int, total: int) -> float | None:
    return round(ok * 100.0 / total, 1) if total else None


def _title(raw: Any, limit: int = 40) -> str:
    text = " ".join(str(raw or "").split())
    return (text[:limit] + "…") if len(text) > limit else text


def _brief_title(brief_json: Any) -> str:
    brief = db.jloads(brief_json, {}) or {}
    if not isinstance(brief, dict):
        return ""
    for key in ("direction", "title", "topic", "goal", "question"):
        if brief.get(key):
            return _title(brief.get(key))
    return ""


# ---------------- 各块原始计数（窗口 [s, e)） ----------------
def _ai_done(tid: int, s: float, e: float) -> dict[str, int]:
    """AI 交付件数，按业务类型拆开。"""
    done = _in(DONE)
    return {
        "expert": _n(
            "SELECT COUNT(*) FROM task WHERE tenant_id=? AND deleted_at IS NULL "
            f"AND LOWER(status) IN ({done}) "
            "AND COALESCE(terminal_at,updated_at)>=? AND COALESCE(terminal_at,updated_at)<?",
            (tid, s, e)),
        "content": _n(
            "SELECT COUNT(*) FROM job WHERE tenant_id=? AND deleted_at IS NULL "
            f"AND LOWER(status) IN ({done}) AND updated_at>=? AND updated_at<?",
            (tid, s, e)),
        "video": _n(
            "SELECT COUNT(*) FROM avatar_job WHERE tenant_id=? AND deleted_at IS NULL "
            f"AND LOWER(status) IN ({done}) AND updated_at>=? AND updated_at<?",
            (tid, s, e)) + _n(
            "SELECT COUNT(*) FROM tv_job WHERE tenant_id=? "
            f"AND LOWER(status) IN ({done}) AND updated_at>=? AND updated_at<?",
            (tid, s, e)),
        "tool": _n(
            "SELECT COUNT(*) FROM tool_job WHERE tenant_id=? "
            f"AND LOWER(status) IN ({done}) AND updated_at>=? AND updated_at<?",
            (tid, s, e)),
        "meeting": _n(
            "SELECT COUNT(*) FROM meeting WHERE tenant_id=? "
            f"AND LOWER(status) IN ({done}) AND updated_at>=? AND updated_at<?",
            (tid, s, e)),
    }


def _created(tid: int, s: float, e: float) -> dict[str, int]:
    """派出去的活（按创建时间）。"""
    return {
        "expert": _n("SELECT COUNT(*) FROM task WHERE tenant_id=? AND deleted_at IS NULL "
                     "AND created_at>=? AND created_at<?", (tid, s, e)),
        "content": _n("SELECT COUNT(*) FROM job WHERE tenant_id=? AND deleted_at IS NULL "
                      "AND created_at>=? AND created_at<?", (tid, s, e)),
        "video": _n("SELECT COUNT(*) FROM avatar_job WHERE tenant_id=? AND deleted_at IS NULL "
                    "AND created_at>=? AND created_at<?", (tid, s, e))
        + _n("SELECT COUNT(*) FROM tv_job WHERE tenant_id=? AND created_at>=? AND created_at<?",
             (tid, s, e)),
        "tool": _n("SELECT COUNT(*) FROM tool_job WHERE tenant_id=? AND created_at>=? "
                   "AND created_at<?", (tid, s, e)),
        "meeting": _n("SELECT COUNT(*) FROM meeting WHERE tenant_id=? AND created_at>=? "
                      "AND created_at<?", (tid, s, e)),
        "staff": _n("SELECT COUNT(*) FROM staff_task WHERE tenant_id=? AND deleted_at IS NULL "
                    "AND created_at>=? AND created_at<?", (tid, s, e)),
    }


def _staff_stats(tid: int, s: float, e: float) -> dict[str, Any]:
    """店员任务：到期的里按时交差的比例（取消的不算）。"""
    row = db.one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN submitted_at IS NOT NULL AND submitted_at<=due_at THEN 1 ELSE 0 END) AS ok "
        "FROM staff_task WHERE tenant_id=? AND deleted_at IS NULL AND status!='cancelled' "
        "AND due_at IS NOT NULL AND due_at>=? AND due_at<?", (tid, s, e)) or {}
    total, ok = int(row.get("total") or 0), int(row.get("ok") or 0)
    approved = _n("SELECT COUNT(*) FROM staff_task WHERE tenant_id=? AND deleted_at IS NULL "
                  "AND status='approved' AND reviewed_at>=? AND reviewed_at<?", (tid, s, e))
    return {"due": total, "on_time": ok, "rate": _rate(ok, total), "approved": approved}


def _checklist_stats(tid: int, s: float, e: float) -> dict[str, Any]:
    d1 = timeutil.today_cn(s)
    d2 = timeutil.today_cn(e - 1)
    row = db.one(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS ok "
        "FROM checklist_run WHERE tenant_id=? AND run_date>=? AND run_date<=? "
        "AND (status!='open' OR run_date<?)", (tid, d1, d2, timeutil.today_cn(e))) or {}
    total, ok = int(row.get("total") or 0), int(row.get("ok") or 0)
    return {"total": total, "done": ok, "rate": _rate(ok, total)}


def _inspection_stats(tid: int, s: float, e: float) -> dict[str, Any]:
    row = db.one(
        "SELECT COUNT(*) AS n, AVG(score) AS avg FROM inspection_visit WHERE tenant_id=? "
        "AND deleted_at IS NULL AND completed_at>=? AND completed_at<?", (tid, s, e)) or {}
    n = int(row.get("n") or 0)
    avg = row.get("avg")
    return {"visits": n, "avg_score": round(float(avg), 1) if avg is not None and n else None}


def _points_spent(tid: int, s: float, e: float) -> float:
    spent = _f("SELECT COALESCE(-SUM(delta),0) FROM billing_log WHERE tenant_id=? AND delta<0 "
               "AND created_at>=? AND created_at<?", (tid, s, e))
    refunded = _f("SELECT COALESCE(SUM(delta),0) FROM billing_log WHERE tenant_id=? AND delta>0 "
                  "AND reason LIKE '退回%' AND created_at>=? AND created_at<?", (tid, s, e))
    return round(max(0.0, spent - refunded), 1)


def _active_people(tid: int, s: float, e: float) -> set[int]:
    ids: set[int] = set()
    for table, col, ts_col in (
            ("task", "created_by", "created_at"), ("job", "created_by", "created_at"),
            ("avatar_job", "created_by", "created_at"), ("tv_job", "created_by", "created_at"),
            ("tool_job", "created_by", "created_at"), ("meeting", "created_by", "created_at"),
            ("staff_task", "created_by", "created_at"),
            ("staff_task", "assignee_user_id", "submitted_at"),
            ("checklist_run", "completed_by", "completed_at"),
            ("inspection_visit", "created_by", "created_at")):
        for row in db.q(f"SELECT DISTINCT {col} AS u FROM {table} WHERE tenant_id=? "
                        f"AND {col} IS NOT NULL AND {ts_col}>=? AND {ts_col}<?", (tid, s, e)):
            ids.add(int(row["u"]))
    return ids


# ---------------- 待老板拍板 ----------------
def _todo(tid: int, now: float, branches: dict[int, str], users: dict[int, dict]) -> list[dict]:
    items: list[dict] = []

    def who(uid):
        return (users.get(int(uid)) or {}).get("name", "") if uid else ""

    for r in db.q("SELECT id,branch_id,assignee_user_id,title,submitted_at FROM staff_task "
                  "WHERE tenant_id=? AND deleted_at IS NULL AND status='submitted' "
                  "ORDER BY submitted_at LIMIT 30", (tid,)):
        items.append({"key": f"staff_review:{r['id']}", "type": "review", "level": "normal",
                      "label": "店员已交差，等您验收", "title": _title(r["title"]),
                      "branch_id": r["branch_id"], "branch": branches.get(r["branch_id"] or 0, ""),
                      "who": who(r["assignee_user_id"]), "since": r["submitted_at"],
                      "route": "#/staff-tasks", "dispatchable": False})
    for r in db.q("SELECT id,branch_id,assignee_user_id,title,due_at FROM staff_task "
                  "WHERE tenant_id=? AND deleted_at IS NULL AND status IN ('todo','rejected') "
                  "AND due_at IS NOT NULL AND due_at<? ORDER BY due_at LIMIT 30", (tid, now)):
        items.append({"key": f"staff_overdue:{r['id']}", "type": "overdue", "level": "high",
                      "label": "店员任务逾期未交", "title": _title(r["title"]),
                      "branch_id": r["branch_id"], "branch": branches.get(r["branch_id"] or 0, ""),
                      "who": who(r["assignee_user_id"]), "since": r["due_at"],
                      "route": "#/staff-tasks", "dispatchable": True})
    for r in db.q(
            "SELECT a.id,a.plan,a.due_at,a.status,a.assignee_user_id,v.branch_id,i.title,i.severity "
            "FROM inspection_action a JOIN inspection_visit v ON v.id=a.visit_id "
            "JOIN inspection_issue i ON i.id=a.issue_id "
            "WHERE a.tenant_id=? AND v.deleted_at IS NULL AND a.status IN "
            "('open','in_progress','reopened','awaiting_recheck') ORDER BY a.due_at LIMIT 60",
            (tid,)):
        if r["status"] == "awaiting_recheck":
            label, level, typ = "整改已提交，等您复查", "normal", "recheck"
        elif r["due_at"] and float(r["due_at"]) < now:
            label, level, typ = "巡店整改逾期", "high", "overdue"
        elif str(r["severity"]) in ("critical", "high"):
            label, level, typ = f"{SEVERITY_LABELS.get(r['severity'], '')}风险整改中", "mid", "risk"
        else:
            continue
        items.append({"key": f"action:{r['id']}", "type": typ, "level": level, "label": label,
                      "title": _title(r["title"] or r["plan"]), "branch_id": r["branch_id"],
                      "branch": branches.get(r["branch_id"] or 0, ""),
                      "who": who(r["assignee_user_id"]), "since": r["due_at"],
                      "route": "#/inspections", "dispatchable": typ != "recheck"})
    for r in db.q(f"SELECT id,brief_json,status,updated_at FROM job WHERE tenant_id=? "
                  f"AND deleted_at IS NULL AND LOWER(status) IN ({_in(WAITING)}) "
                  "ORDER BY updated_at LIMIT 20", (tid,)):
        items.append({"key": f"job:{r['id']}", "type": "content", "level": "normal",
                      "label": "内容等您审稿", "title": _brief_title(r["brief_json"]) or f"内容工单 #{r['id']}",
                      "branch_id": None, "branch": "", "who": "AI 内容团队",
                      "since": r["updated_at"], "route": f"#/job/{r['id']}", "dispatchable": False})
    for r in db.q(f"SELECT id,brief_json,employee_name_snapshot,updated_at FROM task "
                  f"WHERE tenant_id=? AND deleted_at IS NULL AND LOWER(status) IN ({_in(WAITING)}) "
                  "ORDER BY updated_at LIMIT 20", (tid,)):
        items.append({"key": f"task:{r['id']}", "type": "content", "level": "normal",
                      "label": "AI 员工等您确认", "title": _brief_title(r["brief_json"]) or f"任务 #{r['id']}",
                      "branch_id": None, "branch": "", "who": r["employee_name_snapshot"] or "AI 员工",
                      "since": r["updated_at"], "route": f"#/tasks/{r['id']}", "dispatchable": False})
    rank = {"high": 0, "mid": 1, "normal": 2}
    items.sort(key=lambda x: (rank.get(x["level"], 3), float(x["since"] or 0)))
    return items


# ---------------- 人 ----------------
def _users(tid: int) -> dict[int, dict]:
    out: dict[int, dict] = {}
    branch_names = {int(r["id"]): r["name"] for r in db.q(
        "SELECT id,name FROM store_branch WHERE tenant_id=?", (tid,))}
    bind: dict[int, list[str]] = {}
    for r in db.q("SELECT user_id,branch_id FROM user_branch WHERE tenant_id=?", (tid,)):
        if int(r["branch_id"]) in branch_names:
            bind.setdefault(int(r["user_id"]), []).append(branch_names[int(r["branch_id"])])
    for r in db.q("SELECT id,username,role,job_title,enabled,created_at FROM users "
                  "WHERE tenant_id=? ORDER BY id", (tid,)):
        role = str(r.get("role") or "")
        level = role if role in ("owner", "root") else str(r.get("job_title") or "staff")
        out[int(r["id"])] = {
            "id": int(r["id"]), "name": str(r.get("username") or ""),
            "role": level, "role_label": ROLE_LABELS.get(level, "店员"),
            "enabled": int(r.get("enabled") or 0) == 1,
            "branches": bind.get(int(r["id"]), []),
            "created_at": r.get("created_at"),
        }
    return out


def _people(tid: int, s: float, e: float, users: dict[int, dict], now: float) -> list[dict]:
    def by_user(sql: str, args: tuple) -> dict[int, int]:
        return {int(r["u"]): int(r["n"] or 0) for r in db.q(sql, args) if r.get("u") is not None}

    ai = {}
    for table in ("task", "job", "avatar_job", "tv_job", "tool_job", "meeting"):
        extra = " AND deleted_at IS NULL" if table in ("task", "job", "avatar_job") else ""
        for uid, n in by_user(f"SELECT created_by AS u, COUNT(*) AS n FROM {table} "
                              f"WHERE tenant_id=?{extra} AND created_at>=? AND created_at<? "
                              "GROUP BY created_by", (tid, s, e)).items():
            ai[uid] = ai.get(uid, 0) + n
    dispatched = by_user("SELECT created_by AS u, COUNT(*) AS n FROM staff_task WHERE tenant_id=? "
                         "AND deleted_at IS NULL AND created_at>=? AND created_at<? GROUP BY created_by",
                         (tid, s, e))
    done = by_user("SELECT assignee_user_id AS u, COUNT(*) AS n FROM staff_task WHERE tenant_id=? "
                   "AND deleted_at IS NULL AND submitted_at>=? AND submitted_at<? "
                   "GROUP BY assignee_user_id", (tid, s, e))
    overdue = by_user("SELECT assignee_user_id AS u, COUNT(*) AS n FROM staff_task WHERE tenant_id=? "
                      "AND deleted_at IS NULL AND status IN ('todo','rejected') AND due_at IS NOT NULL "
                      "AND due_at<? GROUP BY assignee_user_id", (tid, now))
    checklists = by_user("SELECT completed_by AS u, COUNT(*) AS n FROM checklist_run WHERE tenant_id=? "
                         "AND completed_at>=? AND completed_at<? GROUP BY completed_by", (tid, s, e))
    last: dict[int, float] = {}
    for table, col, ts_col in (
            ("task", "created_by", "created_at"), ("job", "created_by", "created_at"),
            ("avatar_job", "created_by", "created_at"), ("tv_job", "created_by", "created_at"),
            ("tool_job", "created_by", "created_at"), ("meeting", "created_by", "created_at"),
            ("staff_task", "created_by", "created_at"),
            ("staff_task", "assignee_user_id", "submitted_at"),
            ("checklist_run", "completed_by", "completed_at")):
        for r in db.q(f"SELECT {col} AS u, MAX({ts_col}) AS t FROM {table} WHERE tenant_id=? "
                      f"AND {col} IS NOT NULL GROUP BY {col}", (tid,)):
            if r.get("t"):
                uid = int(r["u"])
                last[uid] = max(last.get(uid, 0.0), float(r["t"]))
    rows = []
    for uid, u in users.items():
        if not u["enabled"]:
            continue
        t = last.get(uid)
        idle = int((now - t) // 86400) if t else None
        row = {**u, "ai_tasks": ai.get(uid, 0), "dispatched": dispatched.get(uid, 0),
               "staff_done": done.get(uid, 0), "overdue": overdue.get(uid, 0),
               "checklists": checklists.get(uid, 0), "last_active": t, "idle_days": idle}
        row["total"] = row["ai_tasks"] + row["dispatched"] + row["staff_done"] + row["checklists"]
        row["flag"] = ("never" if t is None else "idle" if idle is not None and idle >= IDLE_DAYS
                       else "overdue" if row["overdue"] else "")
        rows.append(row)
    rows.sort(key=lambda r: (-r["total"], r["id"]))
    return rows


# ---------------- 其他块 ----------------
def _trend(tid: int, days: int, now: float) -> list[dict]:
    """按自然日（北京时间）：AI 交付、店员交差、清单完成。"""
    day_end = timeutil.cn_day_start_ts(now) + 86400
    start = day_end - days * 86400
    buckets = []
    for i in range(days):
        s = start + i * 86400
        buckets.append({"day": timeutil.today_cn(s), "s": s, "ai": 0, "staff": 0, "checklist": 0})

    def put(ts_list, key):
        for t in ts_list:
            idx = int((float(t) - start) // 86400)
            if 0 <= idx < days:
                buckets[idx][key] += 1

    done = _in(DONE)
    put([r["t"] for r in db.q(
        "SELECT COALESCE(terminal_at,updated_at) AS t FROM task WHERE tenant_id=? AND deleted_at IS NULL "
        f"AND LOWER(status) IN ({done}) AND COALESCE(terminal_at,updated_at)>=?", (tid, start))], "ai")
    for table, extra in (("job", " AND deleted_at IS NULL"), ("avatar_job", " AND deleted_at IS NULL"),
                         ("tv_job", ""), ("tool_job", ""), ("meeting", "")):
        put([r["t"] for r in db.q(f"SELECT updated_at AS t FROM {table} WHERE tenant_id=?{extra} "
                                  f"AND LOWER(status) IN ({done}) AND updated_at>=?", (tid, start))], "ai")
    put([r["t"] for r in db.q("SELECT submitted_at AS t FROM staff_task WHERE tenant_id=? "
                              "AND deleted_at IS NULL AND submitted_at>=?", (tid, start))], "staff")
    put([r["t"] for r in db.q("SELECT completed_at AS t FROM checklist_run WHERE tenant_id=? "
                              "AND status='done' AND completed_at>=?", (tid, start))], "checklist")
    for b in buckets:
        b.pop("s", None)
    return buckets


def _ai_employees(tid: int, s: float, e: float) -> list[dict]:
    depts = {str(d.get("key")): str(d.get("name") or d.get("key")) for d in departments.list_depts()}
    rows = db.q(
        "SELECT employee_name_snapshot AS name, employee_dept_key AS dept, COUNT(*) AS n "
        "FROM task WHERE tenant_id=? AND deleted_at IS NULL "
        f"AND LOWER(status) IN ({_in(DONE)}) "
        "AND COALESCE(terminal_at,updated_at)>=? AND COALESCE(terminal_at,updated_at)<? "
        "AND employee_name_snapshot!='' GROUP BY employee_name_snapshot, employee_dept_key "
        "ORDER BY n DESC LIMIT 8", (tid, s, e))
    return [{"name": r["name"], "dept": depts.get(str(r["dept"] or ""), ""), "count": int(r["n"])}
            for r in rows]


def _content(tid: int, s: float, e: float) -> dict[str, Any]:
    platforms = {str(r["p"] or "其他"): int(r["n"]) for r in db.q(
        "SELECT platform AS p, COUNT(*) AS n FROM publish_log WHERE tenant_id=? "
        "AND published_at>=? AND published_at<? GROUP BY platform ORDER BY n DESC", (tid, s, e))}
    return {"published": sum(platforms.values()), "platforms": platforms}


def _stores(tid: int, days: int, now: float) -> list[dict]:
    period = "week" if days <= 7 else "month"
    try:
        data = storerank.compute(tid, period, now=now)
    except Exception:
        return []
    open_issues = {int(r["b"]): int(r["n"]) for r in db.q(
        "SELECT v.branch_id AS b, COUNT(*) AS n FROM inspection_issue i "
        "JOIN inspection_visit v ON v.id=i.visit_id WHERE i.tenant_id=? AND v.deleted_at IS NULL "
        "AND i.status NOT IN ('closed') GROUP BY v.branch_id", (tid,))}
    out = []
    for r in data.get("stores", []):
        comp = r.get("components") or {}
        out.append({
            "branch_id": r["branch_id"], "name": r["branch_name"], "region": r.get("region", ""),
            "score": r.get("score"), "delta": r.get("delta"), "rank": r.get("rank"),
            "needs_attention": r.get("needs_attention"), "reasons": r.get("reasons", [])[:3],
            "checklist": (comp.get("checklist") or {}).get("score"),
            "action": (comp.get("action") or {}).get("score"),
            "task": (comp.get("task") or {}).get("score"),
            "issue": (comp.get("issue") or {}).get("score"),
            "open_issues": open_issues.get(int(r["branch_id"]), 0),
        })
    return out


def _tracking(tid: int, now: float, branches: dict[int, str], users: dict[int, dict]) -> list[dict]:
    rows = db.q("SELECT id,branch_id,assignee_user_id,title,status,due_at,created_at,submitted_at,"
                "source_ref,priority FROM staff_task WHERE tenant_id=? AND deleted_at IS NULL "
                "AND source_ref LIKE ? ORDER BY id DESC LIMIT 40", (tid, SOURCE_PREFIX + "%"))
    labels = {"todo": "待执行", "submitted": "已交差待验收", "approved": "已完成",
              "rejected": "被打回重做", "cancelled": "已取消"}
    out = []
    for r in rows:
        status = str(r["status"])
        overdue = status in ("todo", "rejected") and r["due_at"] and float(r["due_at"]) < now
        out.append({
            "id": r["id"], "title": r["title"], "status": status,
            "status_label": "已逾期" if overdue else labels.get(status, status),
            "overdue": bool(overdue), "due_at": r["due_at"], "created_at": r["created_at"],
            "submitted_at": r["submitted_at"], "priority": r["priority"],
            "branch": branches.get(r["branch_id"] or 0, ""),
            "who": (users.get(int(r["assignee_user_id"])) or {}).get("name", "") if r["assignee_user_id"] else "全店",
            "source": str(r["source_ref"] or "")[len(SOURCE_PREFIX):],
        })
    return out


def _headline(k: dict, todo: list, people: list, stores: list) -> list[str]:
    """一句话结论：纯规则拼出来，每个数都能在下面找到出处。"""
    lines = []
    lines.append(f"这段时间派出 {k['dispatched']['value']} 件活，AI 交付 {k['ai_done']['value']} 件，"
                 f"店员交差 {k['staff_done']['value']} 件。")
    high = [t for t in todo if t["level"] == "high"]
    if high:
        lines.append(f"有 {len(high)} 件事已经逾期，建议先处理。")
    elif todo:
        lines.append(f"有 {len(todo)} 件事等您拍板。")
    idle = [p for p in people if p["flag"] in ("idle", "never") and p["role"] not in ("owner", "root")]
    if idle:
        names = "、".join(p["name"] for p in idle[:3])
        lines.append(f"{names}{'等' if len(idle) > 3 else ''} {len(idle)} 个账号超过 {IDLE_DAYS} 天没用派活。")
    weak = [s for s in stores if s.get("needs_attention")]
    if weak:
        lines.append(f"{'、'.join(s['name'] for s in weak[:3])} 需要盯一下。")
    return lines


def _kpi(label: str, cur: float | None, prev: float | None, unit: str = "", hint: str = "",
         good: str = "up") -> dict:
    delta = None
    if cur is not None and prev is not None:
        delta = round(cur - prev, 1)
    return {"label": label, "value": cur, "prev": prev, "delta": delta, "unit": unit,
            "hint": hint, "good": good}


def summary(tid: int, days: Any = DEFAULT_DAYS, *, now: float | None = None) -> dict:
    days = _days(days)
    tid = int(tid)
    ts = time.time() if now is None else float(now)
    span = days * 86400
    s, ps = ts - span, ts - 2 * span
    tenant = db.one("SELECT id,name,balance,plan,plan_expires FROM tenants WHERE id=?", (tid,)) or {}
    users = _users(tid)
    branches = {int(r["id"]): str(r["name"]) for r in db.q(
        "SELECT id,name FROM store_branch WHERE tenant_id=?", (tid,))}

    ai_c, ai_p = _ai_done(tid, s, ts), _ai_done(tid, ps, s)
    cr_c, cr_p = _created(tid, s, ts), _created(tid, ps, s)
    st_c, st_p = _staff_stats(tid, s, ts), _staff_stats(tid, ps, s)
    ck_c, ck_p = _checklist_stats(tid, s, ts), _checklist_stats(tid, ps, s)
    in_c, in_p = _inspection_stats(tid, s, ts), _inspection_stats(tid, ps, s)
    pt_c, pt_p = _points_spent(tid, s, ts), _points_spent(tid, ps, s)
    act_c, act_p = _active_people(tid, s, ts), _active_people(tid, ps, s)
    enabled = [u for u in users.values() if u["enabled"]]
    staff_done_c = _n("SELECT COUNT(*) FROM staff_task WHERE tenant_id=? AND deleted_at IS NULL "
                      "AND submitted_at>=? AND submitted_at<?", (tid, s, ts))
    staff_done_p = _n("SELECT COUNT(*) FROM staff_task WHERE tenant_id=? AND deleted_at IS NULL "
                      "AND submitted_at>=? AND submitted_at<?", (tid, ps, s))
    ai_total_c, ai_total_p = sum(ai_c.values()), sum(ai_p.values())
    todo = _todo(tid, ts, branches, users)
    kpis = {
        "dispatched": _kpi("派出的活", sum(cr_c.values()), sum(cr_p.values()), "件",
                           "AI 任务、内容、视频、工具、会议和店员任务的新建数"),
        "ai_done": _kpi("AI 交付", ai_total_c, ai_total_p, "件", "AI 数字员工做完交付的活"),
        "staff_done": _kpi("店员交差", staff_done_c, staff_done_p, "件", "店员拍照交差的任务"),
        "staff_rate": _kpi("店员按时率", st_c["rate"], st_p["rate"], "%",
                           f"到期 {st_c['due']} 件，按时交差 {st_c['on_time']} 件"),
        "checklist_rate": _kpi("清单完成率", ck_c["rate"], ck_p["rate"], "%",
                               f"开闭店/交班清单 {ck_c['total']} 份，完成 {ck_c['done']} 份"),
        "inspection": _kpi("巡店均分", in_c["avg_score"], in_p["avg_score"], "分",
                           f"完成巡店 {in_c['visits']} 次"),
        "active_people": _kpi("活跃账号", len(act_c & set(users)), len(act_p & set(users)), "人",
                              f"共 {len(enabled)} 个启用账号"),
        "points": _kpi("点数消耗", pt_c, pt_p, "点",
                       (f"平均每件 AI 交付 {round(pt_c / ai_total_c, 1)} 点" if ai_total_c else "本期还没有 AI 交付"),
                       good="down"),
    }
    people = _people(tid, s, ts, users, ts)
    stores = _stores(tid, days, ts)
    mix = [
        {"key": "expert", "label": "经营分析 / 专家任务", "count": ai_c["expert"]},
        {"key": "content", "label": "内容图文", "count": ai_c["content"]},
        {"key": "video", "label": "数字人 / 视频成片", "count": ai_c["video"]},
        {"key": "tool", "label": "营销工具", "count": ai_c["tool"]},
        {"key": "meeting", "label": "AI 会议决策", "count": ai_c["meeting"]},
        {"key": "staff", "label": "店员执行", "count": staff_done_c},
        {"key": "checklist", "label": "开闭店清单", "count": ck_c["done"]},
        {"key": "inspection", "label": "巡店", "count": in_c["visits"]},
    ]
    return {
        "tenant": {"id": tid, "name": tenant.get("name") or "", "plan": tenant.get("plan") or "",
                   "balance": round(float(tenant.get("balance") or 0), 1)},
        "period": {"days": days, "start": s, "end": ts, "options": list(PERIODS)},
        "generated_at": ts,
        "headline": _headline(kpis, todo, people, stores),
        "kpis": kpis,
        "todo": todo[:40],
        "todo_counts": {"total": len(todo), "high": sum(1 for t in todo if t["level"] == "high")},
        "people": people,
        "stores": stores,
        "trend": _trend(tid, days, ts),
        "mix": mix,
        "ai_employees": _ai_employees(tid, s, ts),
        "content": _content(tid, s, ts),
        "tracking": _tracking(tid, ts, branches, users),
        "branches": [{"id": k, "name": v} for k, v in branches.items()],
        "ai_analysis": latest_analysis(tid),
        "notes": "全部数据来自派活系统内的真实记录；没有数据的项显示“—”，不做估算。",
    }


# ---------------- 导出 ----------------
def _fmt_ts(t: Any) -> str:
    if not t:
        return ""
    return timeutil.now_cn(float(t)).strftime("%Y-%m-%d %H:%M")


def export_xlsx(data: Mapping[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from .export import _xlsx_safe

    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="1F2A44")
    head_font = Font(bold=True, color="FFFFFF")

    def sheet(title: str, headers: list[str], rows: list[list[Any]], first: bool = False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        ws.append(headers)
        for c in ws[1]:
            c.fill, c.font = head_fill, head_font
            c.alignment = Alignment(vertical="center")
        for r in rows:
            ws.append([_xlsx_safe("" if v is None else v) for v in r])
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(width * 1.6 + 2, 10), 60)
        ws.freeze_panes = "A2"

    p = data["period"]
    overview = [["企业", data["tenant"]["name"]], ["周期", f"近 {p['days']} 天"],
                ["统计时间", f"{_fmt_ts(p['start'])} ~ {_fmt_ts(p['end'])}"],
                ["点数余额", data["tenant"]["balance"]], ["", ""]]
    overview += [["结论", line] for line in data["headline"]]
    overview.append(["", ""])
    for k in data["kpis"].values():
        overview.append([k["label"], "—" if k["value"] is None else f"{k['value']}{k['unit']}",
                         "—" if k["prev"] is None else f"上期 {k['prev']}{k['unit']}", k["hint"]])
    sheet("概览", ["指标", "本期", "上期", "说明"], overview, first=True)
    sheet("员工使用", ["账号", "角色", "门店", "派给AI的活", "派给店员的活", "交差", "清单", "逾期", "最近使用", "未用天数"],
          [[u["name"], u["role_label"], "、".join(u["branches"]), u["ai_tasks"], u["dispatched"],
            u["staff_done"], u["checklists"], u["overdue"], _fmt_ts(u["last_active"]),
            "" if u["idle_days"] is None else u["idle_days"]] for u in data["people"]])
    sheet("门店", ["排名", "门店", "综合分", "较上期", "清单按时%", "整改按时%", "派活按时%", "巡店问题分", "未闭环问题", "要盯"],
          [[s["rank"], s["name"], s["score"], s["delta"], s["checklist"], s["action"], s["task"],
            s["issue"], s["open_issues"], "是" if s["needs_attention"] else ""] for s in data["stores"]])
    sheet("待拍板", ["类型", "事项", "门店", "负责人", "时间"],
          [[t["label"], t["title"], t["branch"], t["who"], _fmt_ts(t["since"])] for t in data["todo"]])
    sheet("下发追踪", ["事项", "门店", "负责人", "状态", "截止", "下发时间"],
          [[t["title"], t["branch"], t["who"], t["status_label"], _fmt_ts(t["due_at"]),
            _fmt_ts(t["created_at"])] for t in data["tracking"]])
    sheet("每日趋势", ["日期", "AI交付", "店员交差", "清单完成"],
          [[d["day"], d["ai"], d["staff"], d["checklist"]] for d in data["trend"]])
    sheet("AI员工", ["AI 员工", "部门", "交付件数"],
          [[a["name"], a["dept"], a["count"]] for a in data["ai_employees"]])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------- AI 分析 ----------------
def _ai_key(tid: int) -> str:
    return f"cockpit_ai:{int(tid)}"


def latest_analysis(tid: int) -> dict | None:
    raw = db.get_setting(_ai_key(tid))
    data = db.jloads(raw, None) if raw else None
    return data if isinstance(data, dict) else None


def build_ai_prompt(data: Mapping[str, Any]) -> str:
    """只把汇总数和名字交给模型，不给任务正文；要求只基于这些数说话。"""
    k = data["kpis"]
    facts = {
        "周期天数": data["period"]["days"],
        "指标": {v["label"]: {"本期": v["value"], "上期": v["prev"], "单位": v["unit"], "说明": v["hint"]}
               for v in k.values()},
        "待老板处理": [{"类型": t["label"], "事项": t["title"], "门店": t["branch"], "负责人": t["who"]}
                  for t in data["todo"][:15]],
        "员工使用": [{"账号": p["name"], "角色": p["role_label"], "门店": p["branches"],
                  "派给AI": p["ai_tasks"], "派给店员": p["dispatched"], "交差": p["staff_done"],
                  "清单": p["checklists"], "逾期": p["overdue"], "未用天数": p["idle_days"]}
                 for p in data["people"][:30]],
        "门店": [{"门店": s["name"], "综合分": s["score"], "较上期": s["delta"], "原因": s["reasons"],
                "未闭环问题": s["open_issues"]} for s in data["stores"][:20]],
        "AI员工交付": data["ai_employees"],
        "工作构成": {m["label"]: m["count"] for m in data["mix"]},
        "已发布内容": data["content"],
    }
    method = _industry_method(int(data["tenant"]["id"]))
    return (
        "你是连锁门店老板的经营助理。下面是派活系统里这段时间的真实统计（JSON）。\n"
        + method + (
        "只根据这些数据说话，不要编造任何数字、门店或人名，没有数据就说没有数据；"
        "不要估算营业额、利润或节省的钱。用老板听得懂的大白话。\n"
        "输出 JSON：{\"verdict\":\"一句话总评(40字内)\","
        "\"highlights\":[\"做得好的地方，最多3条，每条带数字\"],"
        "\"risks\":[\"要警惕的问题，最多4条，每条带数字和出处\"],"
        "\"actions\":[{\"title\":\"要下发的具体动作(25字内)\",\"detail\":\"怎么做、做到什么程度(80字内)\","
        "\"branch\":\"涉及的门店名，没有就空\",\"owner\":\"建议负责的账号名，没有就空\","
        "\"priority\":\"high|normal|low\",\"due_days\":完成期限天数}]} actions 最多5条。\n"
        "数据：") + json.dumps(facts, ensure_ascii=False, default=str)
    )


def _industry_method(tid: int) -> str:
    """企业开通了餐饮行业时，按餐饮实战打法（周会追差、一店三效）组织分析。"""
    try:
        keys = {str(r["industry_key"]) for r in db.q(
            "SELECT industry_key FROM tenant_industry WHERE tenant_id=?", (tid,))}
    except Exception:
        return ""
    if "restaurant" not in keys:
        return ""
    from . import industryknowledge
    books = {b.get("name"): b for b in (industryknowledge.get("restaurant").get("playbooks") or [])}
    lines = []
    for name in ("周会月会复盘与执行追踪", "经营落地四步闭环"):
        book = books.get(name)
        if book:
            lines.append(f"【{name}】" + "；".join(book.get("steps") or []))
    if not lines:
        return ""
    return ("按下面的餐饮实战打法组织分析：先找本期与上期差异最大的点并归因，"
            "每个建议动作都要落到门店、负责人、期限和验收指标，便于老板一键下发和下周追踪。\n"
            + "\n".join(lines) + "\n")


def _clean_list(raw: Any, limit: int, size: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [_title(x, size) for x in raw if str(x or "").strip()][:limit]


def normalize_ai(data: Any, branches: list[dict], people: list[dict]) -> dict:
    if not isinstance(data, dict) or not str(data.get("verdict") or "").strip():
        raise CockpitError("AI 没给出分析结果")
    bmap = {str(b["name"]): int(b["id"]) for b in branches}
    pmap = {str(p["name"]): int(p["id"]) for p in people}
    actions = []
    for a in (data.get("actions") or [])[:5]:
        if not isinstance(a, dict) or not str(a.get("title") or "").strip():
            continue
        branch = str(a.get("branch") or "").strip()
        owner = str(a.get("owner") or "").strip()
        try:
            due = max(1, min(30, int(a.get("due_days") or 3)))
        except (TypeError, ValueError):
            due = 3
        pri = str(a.get("priority") or "normal")
        actions.append({
            "title": _title(a.get("title"), 40), "detail": _title(a.get("detail"), 200),
            "branch": branch if branch in bmap else "", "branch_id": bmap.get(branch),
            "owner": owner if owner in pmap else "", "owner_id": pmap.get(owner),
            "priority": pri if pri in ("high", "normal", "low") else "normal", "due_days": due,
        })
    return {"verdict": _title(data.get("verdict"), 80),
            "highlights": _clean_list(data.get("highlights"), 3, 120),
            "risks": _clean_list(data.get("risks"), 4, 140), "actions": actions}


async def _default_ai_call(prompt: str, tid: int) -> dict:
    from . import providers
    return await providers.call_text_json(None, prompt, timeout=AI_TIMEOUT_S, retries=1,
                                          token=f"cockpit-ai:{tid}")


async def analyze(tid: int, days: Any = DEFAULT_DAYS, *, call: Callable | None = None,
                  now: float | None = None) -> dict:
    """先扣点再调模型；失败自动退点。结果存一份，下次打开驾驶舱直接看到。"""
    from . import stafftask
    tid = int(tid)
    data = await db.arun(summary, tid, days, now=now)
    try:
        op_key = await db.arun(stafftask._start_charge, AI_ACTION, tid, "数据驾驶舱·AI 经营分析")
    except stafftask.StaffTaskError as exc:
        err = CockpitError(str(exc))
        err.status = getattr(exc, "status", 400)
        raise err from None
    try:
        prompt = await db.arun(build_ai_prompt, data)
        result = await (call or _default_ai_call)(prompt, tid)
        analysis = normalize_ai((result or {}).get("data"), data["branches"], data["people"])
    except Exception as exc:
        await db.arun(stafftask._finish_charge, op_key, False, "AI 分析没做成，自动退回")
        err = CockpitError("这次分析没做成，点数已退回，请稍后再试")
        err.status = 502
        raise err from exc
    await db.arun(stafftask._finish_charge, op_key, True)
    analysis.update({"days": data["period"]["days"], "created_at": data["generated_at"],
                     "points": await db.arun(stafftask._price_points, AI_ACTION) if op_key else 0})
    await db.arun(db.set_setting, _ai_key(tid), json.dumps(analysis, ensure_ascii=False))
    return analysis
