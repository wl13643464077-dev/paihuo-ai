"""提醒与升级（第 2 期）：一个循环，每 5 分钟看一遍「快到期 / 到期 / 逾期」的事。

盯三类对象：
- ``staff_task``：status='todo'、有 due_at（A 的派活任务）；
- ``checklist_run``：还没做完（open，或已被标 missed 但没补完）、有 due_at；
- ``inspection_action``：指派到具体账号、未关闭（已提交复查等老板审核的不算）、有 due_at。

规则（同一对象同一级别只发一次）：
1. 截止前 2 小时：提醒被指派人（没指派就提醒绑定该门店的所有人）；
2. 到截止时间还没做完：再提醒一次；
3. 超过截止时间 30 分钟（ESCALATE_GRACE）：升级 1 级，通知绑定该门店的店长；
4. 超过截止时间 24 小时：升级 2 级，通知老板。
晚上 22:00 到次日 7:30（北京时间）不打扰任何人（店员、店长、老板都一样），
这段时间到点的提醒不记「已发」，顺延到 7:30 之后的第一轮再发；错过的「截止前」
提醒如果到早上已经过了截止时间就不再补，只发「到点」那一条。

去重记录：
- staff_task 用表里自带的 remind_count / last_remind_at / escalated_level：
  last_remind_at ≥ 截止前 2 小时 ⇒ 「截止前」已发；last_remind_at ≥ 截止时间 ⇒ 「到点」已发；
  escalated_level ≥ 1 / ≥ 2 ⇒ 对应升级已发。截止时间被改晚后会自然重新提醒。
  每次提醒/升级另记一条 staff_task_event（kind=reminded/escalated）作审计轨迹。
- checklist_run / inspection_action 不改表：每个租户一条 app_setting
  ``reminder_log:{tid}``，JSON {对象键: 已发级别位图}。对象键里带上指派人和截止时间
  （如 ``ia:12:34:1790000000``），改派或改期后重新提醒；每轮只保留仍在扫描范围内的
  对象，已完成/过期很久的自动清掉，不会无限增长。

渠道：每人一条站内通知（notify.record 按人落库）+ 每个租户每轮最多一条企业微信群
text 消息，汇总本轮所有提醒并 @ 相关人的手机号（users.phone）。合并发送是为了
不触发群机器人每分钟 20 条的限流。

这个循环顺带：生成当天清单（幂等）、把过了截止时间的清单标 missed、
北京时间 8 点后触发老板早报（幂等，一天一次）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from . import checklist, db, notify, obs, timeutil

log = logging.getLogger("reminders")

LOOP_NAME = "reminders"
TICK_SECONDS = 300
PRE_WINDOW = 2 * 3600
ESCALATE_GRACE = 30 * 60
BOSS_AFTER = 24 * 3600
LOOKBACK = 7 * 86400
QUIET_START_MIN = 22 * 60          # 22:00
QUIET_END_MIN = 7 * 60 + 30        # 07:30
DIGEST_HOURS = (8, 10)             # 老板早报:北京时间 8:00 起的第一轮

STAGE_PRE = 1
STAGE_DUE = 2
STAGE_ESC1 = 4
STAGE_ESC2 = 8
STAGE_ORDER = (STAGE_PRE, STAGE_DUE, STAGE_ESC1, STAGE_ESC2)
_LOG_KEY = "reminder_log:{tid}"
_WEBHOOK_SECTIONS = (
    (STAGE_PRE, "⏰ 快到截止时间了"),
    (STAGE_DUE, "⌛ 到点还没做完"),
    (STAGE_ESC1, "🔺 超时没做完，请店长跟进"),
    (STAGE_ESC2, "🔴 超时一天多，请老板过问"),
)


# ---------------- 纯函数:时间窗 ----------------
def _minute_of_day(now: float) -> int:
    dt = timeutil.now_cn(now)
    return dt.hour * 60 + dt.minute


def in_quiet_hours(now: float) -> bool:
    """北京时间 22:00 到次日 07:30 不打扰。"""
    minute = _minute_of_day(now)
    return minute >= QUIET_START_MIN or minute < QUIET_END_MIN


def quiet_resume_ts(now: float) -> float:
    """免打扰时段里的提醒顺延到的时刻（下一个 07:30）；不在免打扰时段返回 now。"""
    if not in_quiet_hours(now):
        return float(now)
    return timeutil.next_cn_clock_ts(QUIET_END_MIN // 60, QUIET_END_MIN % 60, now)


def pending_stages(due_at: float, now: float, sent: int) -> list[int]:
    """这一刻该发、但还没发过的级别（按先后顺序）。"""
    due = float(due_at)
    out: list[int] = []
    if now < due:
        if now >= due - PRE_WINDOW and not sent & (STAGE_PRE | STAGE_DUE):
            out.append(STAGE_PRE)
        return out
    if not sent & STAGE_DUE:
        out.append(STAGE_DUE)
    if now >= due + ESCALATE_GRACE and not sent & STAGE_ESC1:
        out.append(STAGE_ESC1)
    if now >= due + BOSS_AFTER and not sent & STAGE_ESC2:
        out.append(STAGE_ESC2)
    return out


def task_sent_mask(row: dict) -> int:
    """staff_task 的已发级别：由 last_remind_at / escalated_level 推出来。"""
    due = float(row["due_at"])
    last = row.get("last_remind_at")
    mask = 0
    if last is not None:
        if float(last) >= due - PRE_WINDOW:
            mask |= STAGE_PRE
        if float(last) >= due:
            mask |= STAGE_DUE
    level = int(row.get("escalated_level") or 0)
    if level >= 1:
        mask |= STAGE_ESC1
    if level >= 2:
        mask |= STAGE_ESC2
    return mask


def _duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} 分钟"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)} 小时"
    return f"{int(seconds // 86400)} 天"


# ---------------- 扫描对象 ----------------
def _task_objects(tid: int, now: float) -> list[dict]:
    rows = db.q(
        "SELECT t.id,t.branch_id,t.assignee_user_id,t.title,t.due_at,"
        "t.require_photo,t.last_remind_at,t.escalated_level,b.name AS branch_name "
        "FROM staff_task t LEFT JOIN store_branch b ON b.id=t.branch_id "
        "AND b.tenant_id=t.tenant_id WHERE t.tenant_id=? AND t.status='todo' "
        "AND t.deleted_at IS NULL AND t.due_at IS NOT NULL AND t.due_at<=? "
        "AND t.due_at>=? ORDER BY t.due_at,t.id",
        (int(tid), now + PRE_WINDOW, now - LOOKBACK),
    )
    out = []
    for row in rows:
        title = str(row.get("title") or "").strip()[:30] or "派的活"
        out.append({
            "type": "task", "id": int(row["id"]),
            "branch_id": int(row["branch_id"]) if row.get("branch_id") else None,
            "branch_name": str(row.get("branch_name") or ""),
            "assignee": int(row["assignee_user_id"]) if row.get("assignee_user_id") else None,
            "due_at": float(row["due_at"]),
            "label": f"派的活「{title}」",
            "photo": bool(row.get("require_photo")),
            "sent": task_sent_mask(row),
            "boss_link": "#/staff-tasks",
        })
    return out


def _checklist_objects(tid: int, now: float, sent_log: dict) -> list[dict]:
    rows = db.q(
        "SELECT r.id,r.branch_id,r.kind,r.assignee_user_id,r.due_at,r.items_json,"
        "r.run_date,b.name AS branch_name,t.name AS template_name "
        "FROM checklist_run r JOIN store_branch b ON b.id=r.branch_id "
        "AND b.tenant_id=r.tenant_id AND b.active=1 "
        "LEFT JOIN checklist_template t ON t.id=r.template_id AND t.tenant_id=r.tenant_id "
        "WHERE r.tenant_id=? AND r.status IN ('open','missed') AND r.completed_at IS NULL "
        "AND r.due_at IS NOT NULL AND r.due_at<=? AND r.due_at>=? "
        "ORDER BY r.due_at,r.id",
        (int(tid), now + PRE_WINDOW, now - LOOKBACK),
    )
    out = []
    for row in rows:
        items = db.jloads(row.get("items_json"), []) or []
        left = sum(1 for item in items if isinstance(item, dict) and not item.get("done"))
        name = str(row.get("template_name") or "") or checklist.KIND_LABELS.get(
            str(row.get("kind") or ""), "清单")
        assignee = int(row["assignee_user_id"]) if row.get("assignee_user_id") else None
        key = f"cr:{int(row['id'])}:{assignee or 0}:{int(float(row['due_at']))}"
        out.append({
            "type": "checklist", "id": int(row["id"]),
            "branch_id": int(row["branch_id"]),
            "branch_name": str(row.get("branch_name") or ""),
            "assignee": assignee,
            "due_at": float(row["due_at"]),
            "label": f"{row.get('run_date') or ''} {name}（还差 {left} 项）".strip(),
            "photo": False,
            "log_key": key,
            "sent": int(sent_log.get(key) or 0),
            "boss_link": "#/checklists",
        })
    return out


def _action_objects(tid: int, now: float, sent_log: dict) -> list[dict]:
    rows = db.q(
        "SELECT a.id,a.assignee_user_id,a.due_at,a.plan,i.title AS issue_title,"
        "v.branch_id,b.name AS branch_name FROM inspection_action a "
        "JOIN inspection_visit v ON v.id=a.visit_id AND v.tenant_id=a.tenant_id "
        "AND v.deleted_at IS NULL "
        "JOIN store_branch b ON b.id=v.branch_id AND b.tenant_id=a.tenant_id AND b.active=1 "
        "LEFT JOIN inspection_issue i ON i.id=a.issue_id AND i.tenant_id=a.tenant_id "
        "WHERE a.tenant_id=? AND a.assignee_user_id IS NOT NULL "
        "AND a.status NOT IN ('closed','awaiting_recheck') AND a.due_at IS NOT NULL "
        "AND a.due_at<=? AND a.due_at>=? ORDER BY a.due_at,a.id",
        (int(tid), now + PRE_WINDOW, now - LOOKBACK),
    )
    out = []
    for row in rows:
        title = str(row.get("issue_title") or row.get("plan") or "").strip()[:30] or "整改"
        assignee = int(row["assignee_user_id"])
        key = f"ia:{int(row['id'])}:{assignee}:{int(float(row['due_at']))}"
        out.append({
            "type": "action", "id": int(row["id"]),
            "branch_id": int(row["branch_id"]),
            "branch_name": str(row.get("branch_name") or ""),
            "assignee": assignee,
            "due_at": float(row["due_at"]),
            "label": f"巡店整改「{title}」",
            "photo": True,
            "log_key": key,
            "sent": int(sent_log.get(key) or 0),
            "boss_link": "#/inspections",
        })
    return out


# ---------------- 收件人与文案 ----------------
def _recipients(tid: int, obj: dict, stage: int, cache: dict) -> list[int]:
    branch_id = obj.get("branch_id")
    if stage in (STAGE_PRE, STAGE_DUE):
        if obj.get("assignee"):
            return [int(obj["assignee"])]
        if not branch_id:
            return []
        key = ("members", branch_id)
        if key not in cache:
            cache[key] = checklist.branch_members(tid, branch_id)
        return list(cache[key])
    if stage == STAGE_ESC1:
        if not branch_id:
            return []
        key = ("managers", branch_id)
        if key not in cache:
            cache[key] = checklist.branch_managers(tid, branch_id)
        return [uid for uid in cache[key] if uid != obj.get("assignee")]
    key = ("bosses",)
    if key not in cache:
        cache[key] = notify._boss_uids(tid)
    return list(cache[key])


def _user_name(tid: int, uid: int | None, cache: dict) -> str:
    if not uid:
        return "还没指派"
    key = ("name", uid)
    if key not in cache:
        row = db.one("SELECT username FROM users WHERE id=? AND tenant_id=?",
                     (int(uid), int(tid)))
        cache[key] = str((row or {}).get("username") or f"账号{uid}")
    return cache[key]


def build_message(obj: dict, stage: int, now: float, assignee_name: str) -> dict:
    """一条提醒的站内标题/正文/链接 + 企微群里的一行。"""
    where = obj.get("branch_name") or ""
    subject = f"{where}·{obj['label']}" if where else obj["label"]
    due_text = timeutil.format_cn(obj["due_at"], "%m-%d %H:%M")
    if stage == STAGE_PRE:
        left = _duration(obj["due_at"] - now)
        hint = "，记得拍照" if obj.get("photo") else ""
        text = f"{subject}还有 {left} 到截止时间（{due_text}）{hint}。"
        headline = "快到截止时间了"
    elif stage == STAGE_DUE:
        text = f"{subject}已到截止时间（{due_text}），还没做完，请尽快处理。"
        headline = "到点还没做完"
    elif stage == STAGE_ESC1:
        text = (f"{subject}已超时 {_duration(now - obj['due_at'])} 还没做完"
                f"（负责人：{assignee_name}），请店长跟进。")
        headline = "超时没做完，请跟进"
    else:
        text = (f"{subject}已超时 {_duration(now - obj['due_at'])}"
                f"（负责人：{assignee_name}），店长已经提醒过，请老板过问。")
        headline = "超时一天多，请过问"
    kind = "staff_remind" if stage in (STAGE_PRE, STAGE_DUE) else "staff_escalate"
    link = "#/" if kind == "staff_remind" else obj.get("boss_link") or "#/"
    return {
        "kind": kind,
        "payload": {
            "headline": f"{headline}：{subject}"[:80],
            "text": text,
            "summary": text,
            "link": link,
        },
        "line": text,
    }


def webhook_text(lines_by_stage: dict[int, list[str]], *, max_lines: int = 12) -> str:
    """把本轮的提醒汇成一条群消息（分段、超出条数只写「另有 N 条」）。"""
    parts = ["【派活提醒】"]
    shown = 0
    hidden = 0
    for stage, title in _WEBHOOK_SECTIONS:
        lines = lines_by_stage.get(stage) or []
        if not lines:
            continue
        parts.append(title + "：")
        for line in lines:
            if shown >= max_lines:
                hidden += 1
                continue
            parts.append("· " + line)
            shown += 1
    if hidden:
        parts.append(f"另有 {hidden} 条，打开派活看全部。")
    return "\n".join(parts)


# ---------------- 一轮 ----------------
def _load_log(tid: int) -> dict:
    raw = db.jloads(db.get_setting(_LOG_KEY.format(tid=int(tid))), {}) or {}
    return raw if isinstance(raw, dict) else {}


def _save_log(tid: int, data: dict) -> None:
    db.set_setting(
        _LOG_KEY.format(tid=int(tid)),
        json.dumps(data, sort_keys=True) if data else None,
    )


def _mark_task(tid: int, obj: dict, stage: int, now: float, sent_ok: bool,
               text: str) -> None:
    with db.atomic() as connection:
        if stage in (STAGE_PRE, STAGE_DUE):
            connection.execute(
                "UPDATE staff_task SET remind_count=remind_count+?,last_remind_at=?,"
                "updated_at=? WHERE id=? AND tenant_id=? AND status='todo'",
                (1 if sent_ok else 0, now, now, obj["id"], int(tid)),
            )
            kind = "reminded"
        else:
            level = 1 if stage == STAGE_ESC1 else 2
            connection.execute(
                "UPDATE staff_task SET escalated_level=MAX(escalated_level,?),"
                "updated_at=? WHERE id=? AND tenant_id=? AND status='todo'",
                (level, now, obj["id"], int(tid)),
            )
            kind = "escalated"
        connection.execute(
            "INSERT INTO staff_task_event(tenant_id,task_id,actor_user_id,kind,note,"
            "created_at) VALUES(?,?,NULL,?,?,?)",
            (int(tid), obj["id"], kind, text[:200] if sent_ok else "没有可提醒的人", now),
        )


def process_tenant(tid: int, now: float, *, quiet: bool | None = None,
                   stats: dict | None = None) -> dict:
    """处理一个租户的提醒；返回统计。quiet 为 None 时按 now 判断免打扰。"""
    stats = stats if stats is not None else _new_stats()
    quiet = in_quiet_hours(now) if quiet is None else quiet
    sent_log = _load_log(tid)
    objects = (_task_objects(tid, now) + _checklist_objects(tid, now, sent_log)
               + _action_objects(tid, now, sent_log))
    new_log: dict[str, int] = {}
    cache: dict = {}
    lines_by_stage: dict[int, list[str]] = {}
    mention_uids: list[int] = []
    for obj in objects:
        sent = int(obj["sent"])
        stages = pending_stages(obj["due_at"], now, sent)
        if stages and quiet:
            stats["deferred"] += len(stages)
            stages = []
        for stage in stages:
            recipients = _recipients(tid, obj, stage, cache)
            message = build_message(obj, stage, now,
                                    _user_name(tid, obj.get("assignee"), cache))
            delivered = 0
            for uid in recipients:
                if notify.record(tid, message["kind"], message["payload"],
                                 target_user_id=uid) is not None:
                    delivered += 1
                    if uid not in mention_uids:
                        mention_uids.append(uid)
            if delivered:
                lines_by_stage.setdefault(stage, []).append(message["line"])
                stats["sent"] += 1
            else:
                stats["no_recipient"] += 1
            if obj["type"] == "task":
                _mark_task(tid, obj, stage, now, bool(delivered), message["line"])
            sent |= stage
        if obj.get("log_key") and sent:
            new_log[obj["log_key"]] = sent
    if new_log != sent_log:
        _save_log(tid, new_log)
    if lines_by_stage and notify.get_webhook(tid):
        content = webhook_text(lines_by_stage)
        mobiles = notify.mobiles_for_users(tid, mention_uids)
        if notify.send_text_sync(tid, content, mobiles):
            stats["webhook"] += 1
    return stats


def _new_stats() -> dict:
    return {"sent": 0, "deferred": 0, "no_recipient": 0, "webhook": 0,
            "generated": 0, "missed": 0, "tenants": 0}


def _maybe_daily_digest(now: float) -> bool:
    """北京时间 8 点后的第一轮触发老板早报（scheduler 里的函数自带一天一次的幂等）。"""
    hour = timeutil.now_cn(now).hour
    if not DIGEST_HOURS[0] <= hour <= DIGEST_HOURS[1]:
        return False
    from . import scheduler
    scheduler._run_daily_digest(datetime.fromtimestamp(now, scheduler.TZ))
    return True


def run_once(now: float | None = None) -> dict:
    """跑一轮：生成当天清单 → 标 missed → 各租户提醒/升级 → 早报。每步单独兜底。"""
    ts = time.time() if now is None else float(now)
    stats = _new_stats()
    try:
        stats["generated"] = checklist.generate_runs(now=ts)
    except Exception as exc:
        log.error("checklist generate failed error_type=%s", type(exc).__name__)
    try:
        stats["missed"] = len(checklist.mark_missed(now=ts))
    except Exception as exc:
        log.error("checklist mark missed failed error_type=%s", type(exc).__name__)
    quiet = in_quiet_hours(ts)
    for row in db.q("SELECT id FROM tenants WHERE enabled=1 ORDER BY id"):
        try:
            process_tenant(int(row["id"]), ts, quiet=quiet, stats=stats)
            stats["tenants"] += 1
        except Exception as exc:
            log.error("reminders tenant=%s failed error_type=%s",
                      row["id"], type(exc).__name__)
    try:
        _maybe_daily_digest(ts)
    except Exception as exc:
        log.error("daily digest from reminders failed error_type=%s",
                  type(exc).__name__)
    return stats


async def loop(*, interval: float = TICK_SECONDS) -> None:
    """后台循环：每 5 分钟一轮；任何异常都兜住，登记 obs 心跳供 /healthz?deep=1。"""
    obs.register_loop(LOOP_NAME, interval * 3)
    log.info("reminders loop started (every %ss)", int(interval))
    while True:
        obs.beat(LOOP_NAME)
        try:
            stats = await asyncio.to_thread(run_once)
            if stats.get("sent") or stats.get("generated") or stats.get("missed"):
                log.info(
                    "reminders tick generated=%d missed=%d sent=%d deferred=%d",
                    stats["generated"], stats["missed"], stats["sent"],
                    stats["deferred"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("reminders tick failed error_type=%s", type(exc).__name__)
        obs.beat(LOOP_NAME)
        await asyncio.sleep(interval)

