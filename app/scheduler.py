"""定时任务调度(V4):老板设好节奏,内容部到点自动开工.

- 时间一律按北京时间(Asia/Shanghai)理解;
- kind: daily(每天 HH:MM) / weekly(每周X HH:MM) / interval(每 N 小时);
- 到点时若 3 单并行已满则顺延 10 分钟重试,不硬塞;
- 建出的工单与手工下达完全同构,走同一条流水线。
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db

log = logging.getLogger("scheduler")
TZ = ZoneInfo("Asia/Shanghai")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


async def _run_billed(
    action: str,
    tid: int,
    note: str,
    runner,
    commit=None,
    after_success=None,
):
    """周期能力统一结算。

    runner 必须只计算；可见产物由 commit 与计费成功同事务提交。通知等非关键
    副作用放在 after_success，失败只记日志，不能把已经交付的结果再退款。
    """
    from . import billing
    op_key = billing.start_operation(action, tid=tid, note=note)
    try:
        result = await runner()
        if commit is None:
            completed = billing.complete_operation(op_key)
        else:
            completed = billing.complete_operation_if_claimed(
                op_key, lambda connection: bool(commit(connection, result))
            )
        if not completed:
            raise RuntimeError("周期任务结算状态冲突")
    except BaseException as exc:
        try:
            billing.fail_operation(op_key, "周期任务失败自动退回")
        except Exception as settlement_exc:
            log.error(
                "periodic billing settlement failed op=%s error_type=%s",
                op_key,
                type(settlement_exc).__name__,
            )
        raise
    if after_success is not None:
        try:
            after_success(result)
        except Exception as exc:
            log.error(
                "periodic post-commit notification failed action=%s tid=%s "
                "error_type=%s",
                action,
                tid,
                type(exc).__name__,
            )
    return result


def _upsert_setting_tx(connection, key: str, value) -> None:
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    connection.execute(
        "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        "updated_at=excluded.updated_at",
        (key, payload, time.time()),
    )


async def _run_hot_daily(tid: int, today: str):
    from . import growth, notify

    def commit(connection, data):
        row = connection.execute(
            "SELECT value FROM app_setting WHERE key=?", (f"hot_daily:{tid}",)
        ).fetchone()
        conf = db.jloads(row["value"] if row else None, {"enabled": False}) or {}
        if conf.get("last_ymd") == today:
            return False
        conf["last_ymd"] = today
        channels = data.get("channels") or conf.get("channels") or []
        industry = data.get("industry") or conf.get("industry") or "通用"
        _upsert_setting_tx(connection, f"hotpick_channels:{tid}", channels)
        _upsert_setting_tx(
            connection,
            f"hotpick:{tid}:{today}:{industry}",
            data,
        )
        _upsert_setting_tx(connection, f"hot_daily:{tid}", conf)
        return True

    def after(data):
        picks = data.get("picks") or []
        notify.push(tid, "report", {
            "report_name": f"今日必发({data.get('industry') or '通用'})",
            "summary": " / ".join(
                f"{index + 1}.{(pick.get('title') or '')[:20]}"
                for index, pick in enumerate(picks)
            ) + " —— 进工具箱看理由,看中一键开工",
            "link": "#/tools",
        })

    return await _run_billed(
        "hot_pick",
        tid,
        "每日自动",
        lambda: growth.hot_daily_run(tid, save=False),
        commit=commit,
        after_success=after,
    )


async def _run_bench_weekly(tid: int):
    from . import growth, notify

    def commit(connection, data):
        now = time.time()
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        connection.execute(
            "INSERT INTO knowledge"
            "(title,content,tags_json,source,tenant_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                f"竞品盯梢周报 {today}",
                data.get("md") or "",
                json.dumps(["竞品盯梢"], ensure_ascii=False),
                "auto",
                tid,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT value FROM app_setting WHERE key=?", (f"bench_watch:{tid}",)
        ).fetchone()
        conf = db.jloads(row["value"] if row else None, {}) or {}
        try:
            recently_completed = (
                conf.get("last_run")
                and now - float(conf["last_run"]) < 3 * 86400
            )
        except (TypeError, ValueError):
            recently_completed = False
        if recently_completed:
            return False
        conf["last_run"] = now
        _upsert_setting_tx(connection, f"bench_watch:{tid}", conf)
        return True

    def after(data):
        notify.push(tid, "report", {
            "report_name": "竞品盯梢周报",
            "summary": data.get("summary", ""),
            "link": "#/knowledge",
        })

    return await _run_billed(
        "bench_watch",
        tid,
        "每周自动",
        lambda: growth.bench_report(tid, save=False),
        commit=commit,
        after_success=after,
    )


def _digest_stats(tid: int, balance: float, day_start: datetime) -> dict:
    """汇总某租户「昨日」(北京时区自然日)的经营数字,纯查询不写库."""
    y_start = (day_start - timedelta(days=1)).timestamp()
    y_end = day_start.timestamp()
    week_start = (day_start - timedelta(days=7)).timestamp()
    jobs_done = db.one(
        "SELECT COUNT(*) n FROM job WHERE tenant_id=? AND status='done' "
        "AND updated_at>=? AND updated_at<?",
        (tid, y_start, y_end),
    )["n"]
    tasks_done = db.one(
        "SELECT COUNT(*) n FROM task WHERE tenant_id=? AND status='done' "
        "AND deleted_at IS NULL AND updated_at>=? AND updated_at<?",
        (tid, y_start, y_end),
    )["n"]
    spent = float(db.one(
        "SELECT COALESCE(SUM(-delta),0) s FROM billing_log "
        "WHERE tenant_id=? AND delta<0 AND created_at>=? AND created_at<?",
        (tid, y_start, y_end),
    )["s"] or 0)
    # 退点口径:所有退款路径(操作退款/工单失败退款)都会写一条
    # reason 以「退回」开头的正向流水;billing_operation 只覆盖走操作记录的
    # 退款,漏掉 refund_if_claimed 等直退路径,故以流水为准。
    refunds = db.one(
        "SELECT COUNT(*) n FROM billing_log WHERE tenant_id=? AND delta>0 "
        "AND reason LIKE '退回%' AND created_at>=? AND created_at<?",
        (tid, y_start, y_end),
    )["n"]
    paused = db.one(
        "SELECT COUNT(*) n FROM schedule WHERE tenant_id=? AND enabled=0 "
        "AND last_note LIKE '已暂停%'",
        (tid,),
    )["n"]
    week_spent = float(db.one(
        "SELECT COALESCE(SUM(-delta),0) s FROM billing_log "
        "WHERE tenant_id=? AND delta<0 AND created_at>=? AND created_at<?",
        (tid, week_start, y_end),
    )["s"] or 0)
    daily_avg = week_spent / 7
    days_left = int(balance // daily_avg) if daily_avg > 0 and balance > 0 else None
    return {
        "date": (day_start - timedelta(days=1)).strftime("%m-%d"),
        "jobs_done": int(jobs_done or 0),
        "tasks_done": int(tasks_done or 0),
        "spent": spent,
        "refunds": int(refunds or 0),
        "paused": int(paused or 0),
        "balance": float(balance or 0),
        "days_left": days_left,
    }


def _run_daily_digest(now: datetime):
    """老板「昨日经营简报」:每天早上一次,被动推给不天天登录的老板。

    幂等靠 app_setting ``daily_digest_sent:{tid}`` 记最近已发日期(北京时区),
    同日再 tick 直接跳过,重启后也不重发;昨日完全无活动且无暂停风险则不打扰。
    """
    from . import notify
    today = now.strftime("%Y-%m-%d")
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for t in db.q("SELECT id,balance,plan,plan_expires FROM tenants "
                  "WHERE enabled=1 AND id!=1"):
        tid = int(t["id"])
        try:
            if db.get_setting(f"daily_digest_off:{tid}"):
                continue                              # 老板关了开关,不发
            if db.get_setting(f"daily_digest_sent:{tid}") == today:
                continue                              # 今天已发过,不重发
            stats = _digest_stats(tid, float(t["balance"] or 0), day_start)
            # 套餐临期是必须叫醒老板的风险:即使昨日无活动也要提醒
            plan_days_left = None
            if t.get("plan_expires"):
                plan_days_left = int(
                    (float(t["plan_expires"]) - now.timestamp()) // 86400)
            expiring = plan_days_left is not None and plan_days_left <= 7
            had_activity = (stats["jobs_done"] or stats["tasks_done"]
                            or stats["spent"] > 0 or stats["refunds"])
            if not had_activity and not stats["paused"] and not expiring:
                continue                              # 昨日没动静也没风险,别骚扰
            spend_part = f"消耗 {stats['spent']:.0f} 点"
            if stats["refunds"]:
                spend_part += f"、退回 {stats['refunds']} 笔"
            summary = (f"昨日完成:内容工单 {stats['jobs_done']} 单、"
                       f"专家任务 {stats['tasks_done']} 件;{spend_part};"
                       + (f"{stats['paused']} 个定时任务已暂停;" if stats["paused"] else "")
                       + f"余额 {stats['balance']:.0f} 点"
                       + (f"(约可再跑 {stats['days_left']} 天)"
                          if stats["days_left"] is not None else "")
                       + (("" if not expiring else
                           ";套餐已到期,请尽快续费" if plan_days_left < 0
                           else f";套餐还有 {plan_days_left} 天到期")))
            stats["plan_days_left"] = plan_days_left
            # 先落幂等标记再推送:宁可极端情况漏一天,也不给老板发两条
            db.set_setting(f"daily_digest_sent:{tid}", today)
            notify.push(tid, "daily_digest", {**stats, "summary": summary})
            log.info("daily digest sent tid=%s", tid)
        except Exception as exc:
            log.error(
                "daily digest tid=%s failed error_type=%s",
                tid,
                type(exc).__name__,
            )


def compute_next(s: dict, now: float = None) -> float:
    now = now or time.time()
    if s["kind"] == "interval":
        return now + max(1, int(s["every_hours"] or 24)) * 3600
    try:
        hh, mm = map(int, (s["at_time"] or "09:00").split(":"))
    except ValueError:
        hh, mm = 9, 0
    dt = datetime.fromtimestamp(now, TZ)
    cand = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if s["kind"] == "weekly":
        cand += timedelta(days=(int(s["weekday"] or 0) - cand.weekday()) % 7)
        if cand <= dt:
            cand += timedelta(days=7)
    else:  # daily
        if cand <= dt:
            cand += timedelta(days=1)
    return cand.timestamp()


def describe(s: dict) -> str:
    if s["kind"] == "interval":
        return f"每 {s['every_hours'] or 24} 小时"
    if s["kind"] == "weekly":
        return f"每{WEEKDAYS[int(s['weekday'] or 0)]} {s['at_time'] or '09:00'}"
    return f"每天 {s['at_time'] or '09:00'}"


def occurrence_key(next_run_at: float) -> str:
    """把计划表里的原始触发时刻固定成可建唯一索引的键。"""
    if next_run_at is None:
        raise ValueError("定时任务缺少触发时刻")
    return str(round(float(next_run_at) * 1_000_000))


def fire(s: dict, engine, occurrence: str | None = None) -> int:
    """按计划开单；同一 occurrence 重试只复用原单、绝不重复扣费。"""
    tid = s.get("tenant_id") or 1
    from . import billing

    points = 0.0 if tid == 1 else float(
        (billing.prices().get("content_job") or {"points": 18})["points"])
    existing = None
    if occurrence is not None:
        existing = db.one(
            "SELECT * FROM job WHERE source_schedule_id=? "
            "AND source_schedule_occurrence=?",
            (s.get("id"), occurrence),
        )

    if existing:
        job_id = existing["id"]
    else:
        running = db.q(
            "SELECT id FROM job WHERE tenant_id=? AND "
            "status NOT IN ('done','cancelled','failed')",
            (tid,),
        )
        if len(running) >= 3:
            raise ValueError("并行工单已满(3)")
        profile_id = s.get("profile_id")
        if profile_id and not db.one(
                "SELECT id FROM account_profile WHERE id=? AND tenant_id=? "
                "AND deleted_at IS NULL",
                (profile_id, tid)):
            # 兼容旧版本可能留下的越租户引用：不读取、不传播，按未选人设安全执行。
            profile_id = None
        now = time.time()
        with db.atomic() as connection:
            if occurrence is not None:
                row = connection.execute(
                    "SELECT id FROM job WHERE source_schedule_id=? "
                    "AND source_schedule_occurrence=?",
                    (s.get("id"), occurrence),
                ).fetchone()
                if row:
                    job_id = row["id"]
                else:
                    cursor = connection.execute(
                        "INSERT INTO job"
                        "(brief_json,profile_id,tenant_id,source_schedule_id,"
                        "source_schedule_occurrence,mode,status,billing_status,"
                        "billing_points,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,'pending_charge','pending',?,?,?)",
                        (
                            s["brief_json"],
                            profile_id,
                            tid,
                            s.get("id"),
                            occurrence,
                            s["mode"] or "copilot",
                            points,
                            now,
                            now,
                        ),
                    )
                    job_id = cursor.lastrowid
            else:
                cursor = connection.execute(
                    "INSERT INTO job"
                    "(brief_json,profile_id,tenant_id,source_schedule_id,mode,"
                    "status,billing_status,billing_points,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'pending_charge','pending',?,?,?)",
                    (
                        s["brief_json"],
                        profile_id,
                        tid,
                        s.get("id"),
                        s["mode"] or "copilot",
                        points,
                        now,
                        now,
                    ),
                )
                job_id = cursor.lastrowid

    job = db.one(
        "SELECT id,status,billing_status FROM job WHERE id=?", (job_id,)
    )
    if not job:
        raise RuntimeError("定时工单创建失败")

    def claim(c):
        cur = c.execute(
            "UPDATE job SET billing_status='charged',status='running',updated_at=? "
            "WHERE id=? AND billing_status='pending' AND status='pending_charge'",
            (time.time(), job_id),
        )
        return cur.rowcount == 1

    if job["billing_status"] == "pending" and job["status"] == "pending_charge":
        try:
            charged = billing.charge_if_claimed(
                "content_job", tid, claim,
                note=f"定时·{s.get('name', '')[:12]}", points=points)
        except Exception:
            db.q(
                "DELETE FROM job WHERE id=? AND billing_status='pending'",
                (job_id,),
            )
            raise
        if not charged:
            current = db.one(
                "SELECT status,billing_status FROM job WHERE id=?", (job_id,)
            )
            if not current or current.get("billing_status") != "charged":
                raise RuntimeError("定时工单计费状态冲突")
    elif job["billing_status"] == "pending":
        raise RuntimeError("定时工单待扣费状态异常")

    current = db.one("SELECT status FROM job WHERE id=?", (job_id,))
    if current and current["status"] not in {"done", "cancelled", "failed"}:
        engine.notify(job_id)
        engine.touch(job_id)
    return job_id


def _claim_due(schedule_id: int, now: float) -> str | None:
    """CAS 抢占到期计划；多进程/重复 tick 只有一个执行者能开工。"""
    token = uuid.uuid4().hex
    changed = db.execute(
        "UPDATE schedule SET claim_token=?,claim_until=?,updated_at=? "
        "WHERE id=? AND enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=? "
        "AND (claim_until IS NULL OR claim_until<?)",
        (token, now + 900, now, schedule_id, now, now),
    )
    return token if changed == 1 else None


def _finish_claim(schedule_id: int, token: str, **fields) -> bool:
    allowed = {"last_run_at", "next_run_at", "last_note", "enabled",
               "fail_streak"}
    data = {key: value for key, value in fields.items() if key in allowed}
    data.update({"claim_token": None, "claim_until": None, "updated_at": time.time()})
    sets = ",".join(f"{key}=?" for key in data)
    return db.execute(
        f"UPDATE schedule SET {sets} WHERE id=? AND claim_token=?",
        (*data.values(), schedule_id, token),
    ) == 1


def _tick(engine):
    now = time.time()
    for s in db.q("SELECT * FROM schedule WHERE enabled=1"):
        if not s["next_run_at"]:
            db.update("schedule", s["id"], {"next_run_at": compute_next(s, now)})
            continue
        if s["next_run_at"] > now:
            continue
        claim_token = _claim_due(s["id"], now)
        if not claim_token:
            continue
        try:
            job_id = fire(s, engine, occurrence_key(s["next_run_at"]))
            _finish_claim(
                s["id"], claim_token,
                last_run_at=now,
                next_run_at=compute_next(s, now),
                last_note=f"已按时开工 → 工单 #{job_id}",
                fail_streak=0,
            )
            log.info("schedule %s fired -> job %s", s["id"], job_id)
        except Exception as e:
            from . import billing
            if isinstance(e, billing.InsufficientPoints):   # 按类型判断,不再靠文案子串
                _finish_claim(
                    s["id"], claim_token,
                    enabled=0,
                    last_note=f"已暂停:{str(e)[:60]}",
                )
                # 自动日更是老板买这套系统的核心理由;静默停更等于让他两周后
                # 才发现断更。站内必达,配了企微立刻推手机。
                try:
                    from . import notify
                    notify.push(s.get("tenant_id") or 1, "schedule_paused",
                                {"title": s.get("name") or "定时任务"})
                except Exception:
                    log.warning("schedule pause notify failed id=%s", s["id"])
            elif isinstance(e, ValueError):
                _finish_claim(
                    s["id"], claim_token,
                    next_run_at=now + 600,
                    last_note="到点时 3 单并行已满,10 分钟后重试",
                )
            else:
                log.error(
                    "schedule %s fire failed error_type=%s",
                    s["id"],
                    type(e).__name__,
                )
                streak = int(s.get("fail_streak") or 0) + 1
                _finish_claim(
                    s["id"], claim_token,
                    next_run_at=now + 600,
                    last_note=f"开工出错,10分钟后重试(已连续失败 {streak} 次)",
                    fail_streak=streak,
                )
                # 静默重试一整天老板毫不知情是最伤的:连续 3 次就告警一次,
                # 之后每 12 次(约 2 小时)再提醒,直到某次成功清零。
                if streak == 3 or (streak > 3 and streak % 12 == 0):
                    try:
                        from . import notify
                        notify.push(s.get("tenant_id") or 1, "schedule_failed",
                                    {"title": s.get("name") or "定时任务",
                                     "streak": streak})
                    except Exception:
                        log.warning(
                            "schedule fail notify failed id=%s", s["id"])


async def _periodic():
    """V25:低频周期任务——台账自动复盘 + 竞品周报(周一上午)+ 每日必发推送(早7-9点)."""
    from . import billing, growth, pubtrack
    await pubtrack.tick()
    now = datetime.now(TZ)
    if 7 <= now.hour <= 9:                                # 每日必发:一天一次
        today = now.strftime("%Y-%m-%d")
        for r in db.q("SELECT key FROM app_setting WHERE key LIKE 'hot_daily:%'"):
            tid = int(r["key"].split(":")[1])
            conf = growth.hot_daily_conf(tid)
            if not conf.get("enabled") or conf.get("last_ymd") == today:
                continue
            try:
                await _run_hot_daily(tid, today)
                log.info("hot daily done tid=%s", tid)
            except billing.InsufficientPoints:
                conf["enabled"] = False
                conf["note"] = "点数不足已暂停"
                db.set_setting(f"hot_daily:{tid}", __import__("json").dumps(conf, ensure_ascii=False))
            except Exception as exc:
                log.error(
                    "hot daily tid=%s failed error_type=%s",
                    tid,
                    type(exc).__name__,
                )
    if 8 <= now.hour <= 10:                              # 昨日经营简报:一天一次
        try:
            _run_daily_digest(now)
        except Exception as exc:
            log.error(
                "daily digest tick failed error_type=%s",
                type(exc).__name__,
            )
    if now.weekday() == 0 and 9 <= now.hour <= 11:      # 周一上午出周报
        for r in db.q("SELECT key FROM app_setting WHERE key LIKE 'bench_watch:%'"):
            tid = int(r["key"].split(":")[1])
            conf = growth.watch_conf(tid)
            if not (conf.get("enabled") and conf.get("targets")):
                continue
            if conf.get("last_run") and time.time() - conf["last_run"] < 3 * 86400:
                continue
            try:
                await _run_bench_weekly(tid)
                log.info("bench weekly report done tid=%s", tid)
            except billing.InsufficientPoints:
                conf["enabled"] = False
                db.set_setting(f"bench_watch:{tid}", __import__("json").dumps(conf, ensure_ascii=False))
            except Exception as exc:
                log.error(
                    "bench weekly tid=%s failed error_type=%s",
                    tid,
                    type(exc).__name__,
                )


async def loop(engine):
    log.info("scheduler started (TZ=Asia/Shanghai)")
    last_periodic = 0.0
    while True:
        try:
            _tick(engine)
        except Exception as exc:
            log.error(
                "scheduler tick failed error_type=%s",
                type(exc).__name__,
            )
        if time.time() - last_periodic > 1800:           # 每30分钟跑一次低频任务
            last_periodic = time.time()
            try:
                await _periodic()
            except Exception as exc:
                log.error(
                    "periodic tick failed error_type=%s",
                    type(exc).__name__,
                )
        await asyncio.sleep(30)
