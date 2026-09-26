"""统一看门狗:周期扫描卡住的专家任务 / 会议 / 内容工单并安全收口.

卡住 = 数据库里还是「排队中/执行中」,但当前进程里没有任何协程在推进它,
且最近一次动静(updated_at、步骤时间戳、工位运行记录)已超过阈值。
这种任务老板看到的是永远转圈、点数却已扣掉;看门狗把它按失败收口并
走各模块已有的失败结算(幂等退款),再通知老板可以重新派一次。

安全边界:
- 只处理本进程内确实没在跑的记录(taskrunner.RUNNING / meeting.ACTIVE /
  engine.locks + 引擎队列),绝不误杀长任务;
- 结算前用 CAS 条件更新「认领」这条记录(状态与 updated_at 都没变才算数),
  与正常完成竞争时只有一方生效;
- 循环自身兜底所有异常,任何一次扫描失败都不会让循环退出。
"""
import asyncio
import json
import logging
import os
import time

from . import db

log = logging.getLogger("watchdog")

TIMEOUT_MESSAGE = "任务超时没有完成，点数已自动退回，可以重新派一次"
_KEEP_DELIVERY_MESSAGE = "任务超时没有完成，已写好的正文保留，可以重新派一次"
_MEETING_TIMEOUT_MESSAGE = "会议超时没有开完，点数已自动退回，可以重新开一次"
_EXECUTION_TIMEOUT_NOTE = "会议结论已保存，但自动派活超时没完成；请点击“执行决定”重试"
_INTERVENTION_TIMEOUT_REASON = "会议追问超时没有完成，点数已自动退回，可以再问一次"
_ERROR_BACKOFF_SECONDS = 5


def _env_minutes(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value or value < 1 or value > 7 * 24 * 60:
        return default
    return value


def thresholds() -> dict:
    """各类记录的卡死阈值(秒),可用环境变量按分钟覆盖。"""
    return {
        "task": _env_minutes("CONTENTCREW_WATCHDOG_TASK_MINUTES", 60) * 60,
        "meeting": _env_minutes("CONTENTCREW_WATCHDOG_MEETING_MINUTES", 180) * 60,
        "job": _env_minutes("CONTENTCREW_WATCHDOG_JOB_MINUTES", 120) * 60,
    }


def interval_seconds() -> float:
    return _env_minutes("CONTENTCREW_WATCHDOG_INTERVAL_MINUTES", 2) * 60


def _last_step_ts(steps_json) -> float:
    steps = db.jloads(steps_json, []) or []
    latest = 0.0
    if isinstance(steps, list):
        for step in steps[-5:]:
            if isinstance(step, dict):
                try:
                    latest = max(latest, float(step.get("ts") or 0))
                except (TypeError, ValueError):
                    continue
    return latest


def _inspection_idx() -> int:
    # 巡店任务(同在 task 表)由 main 的独立执行器负责,看门狗不接管。
    try:
        from . import inspection
        return int(getattr(inspection, "EMPLOYEE_IDX", 10))
    except Exception:
        return 10


# ---------------- 扫描(线程池里跑,纯读) ----------------
def stale_tasks(now: float, threshold: float) -> list[dict]:
    rows = db.q(
        "SELECT id,status,updated_at,steps_json FROM task "
        "WHERE status IN ('queued','running') "
        "AND billing_status IN ('charged','included') "
        "AND deleted_at IS NULL AND emp_idx!=? AND COALESCE(updated_at,0)<?",
        (_inspection_idx(), now - threshold),
    )
    stale = []
    for row in rows:
        last = max(float(row.get("updated_at") or 0), _last_step_ts(row.get("steps_json")))
        if now - last >= threshold:
            stale.append({
                "id": int(row["id"]),
                "status": row["status"],
                "updated_at": row.get("updated_at"),
            })
    return stale


def stale_meetings(now: float, threshold: float) -> list[dict]:
    return [
        {
            "id": int(row["id"]),
            "status": row["status"],
            "updated_at": row.get("updated_at"),
        }
        for row in db.q(
            "SELECT id,status,updated_at FROM meeting "
            "WHERE status IN ('queued','running') AND COALESCE(updated_at,0)<?",
            (now - threshold,),
        )
    ]


def stale_jobs(now: float, threshold: float) -> list[dict]:
    # 工位执行期间工单行本身不一定刷新 updated_at,进度写在 station_run 上,
    # 所以取两者里最新的一次动静。
    rows = db.q(
        "SELECT j.id,j.status,j.updated_at,"
        "MAX(COALESCE(j.updated_at,0),COALESCE(("
        "SELECT MAX(r.updated_at) FROM station_run r WHERE r.job_id=j.id),0)) "
        "AS last_activity "
        "FROM job j WHERE j.status='running'",
    )
    return [
        {
            "id": int(row["id"]),
            "status": row["status"],
            "updated_at": row.get("updated_at"),
        }
        for row in rows
        if now - float(row.get("last_activity") or 0) >= threshold
    ]


def failed_charged_tasks(now: float, grace: float = 300) -> list[int]:
    """失败但还没退款的独立任务(结算中途出错留下的),补做幂等退款。"""
    return [
        int(row["id"])
        for row in db.q(
            "SELECT id FROM task WHERE status='failed' AND billing_status='charged' "
            "AND deleted_at IS NULL AND emp_idx!=? AND COALESCE(updated_at,0)<?",
            (_inspection_idx(), now - grace),
        )
    ]


def failed_charged_meetings(now: float, grace: float = 300) -> list[int]:
    return [
        int(row["id"])
        for row in db.q(
            "SELECT id FROM meeting WHERE status='failed' AND billing_status='charged' "
            "AND COALESCE(updated_at,0)<?",
            (now - grace,),
        )
    ]


# ---------------- 认领 + 结算(线程池里跑) ----------------
def _claim_stale(table: str, row: dict) -> bool:
    """CAS 认领:状态和 updated_at 都与扫描时一致才算数。

    正常流程任何一次推进都会改 updated_at,认领就会失败;认领成功后
    排队中的专家任务同时转成执行中,taskrunner 的 queued→running 抢占随之落空,
    不会出现「看门狗在收口、执行器又开跑」。
    """
    assert table in {"task", "meeting", "job"}
    now = time.time()
    status_sql = ""
    if table == "task" and row["status"] == "queued":
        status_sql = "status='running',"
    if row.get("updated_at") is None:
        changed = db.execute(
            f"UPDATE {table} SET {status_sql}updated_at=? "
            "WHERE id=? AND status=? AND updated_at IS NULL",
            (now, row["id"], row["status"]),
        )
    else:
        changed = db.execute(
            f"UPDATE {table} SET {status_sql}updated_at=? "
            "WHERE id=? AND status=? AND updated_at=?",
            (now, row["id"], row["status"], row["updated_at"]),
        )
    return changed == 1


def settle_stale_task(row: dict) -> bool:
    from . import taskrunner
    if not _claim_stale("task", row):
        return False
    settled = taskrunner.settle_failure(row["id"], TIMEOUT_MESSAGE)
    if settled:
        taskrunner.notify_task_outcome(row["id"], False, "超时没有完成")
        log.warning("watchdog settled stale task %s", row["id"])
    return bool(settled)


def settle_stale_meeting(row: dict) -> bool:
    from . import meeting
    current = db.one(
        "SELECT id,status,phase,decision,intervention_state,intervention_op_key "
        "FROM meeting WHERE id=?",
        (row["id"],),
    )
    if not current or not _claim_stale("meeting", row):
        return False
    if current.get("intervention_state") == "running" and current.get(
        "intervention_op_key"
    ):
        # 追问:恢复上一次有效结论并退回这次追问的点数。
        restored = meeting.abort_intervention(
            row["id"], current["intervention_op_key"], _INTERVENTION_TIMEOUT_REASON,
        )
        if restored:
            meeting.notify_outcome(row["id"], False, "追问超时没有完成，上次结论保留")
            log.warning("watchdog aborted stale meeting intervention %s", row["id"])
        return bool(restored)
    if current.get("phase") in {"execute", "executing"} and str(
        current.get("decision") or ""
    ).upper() in {"GO", "NEED_INFO"}:
        # 结论已经交付,只是自动派活卡住:退回「等你执行」,老板点一下即可续上;
        # 已派出的任务按唯一键复用,不会重复派活,也不退结论的钱。
        changed = db.execute(
            "UPDATE meeting SET status='done',phase='awaiting_execution',"
            "next_action=?,updated_at=? "
            "WHERE id=? AND status='running' AND phase IN ('execute','executing')",
            (_EXECUTION_TIMEOUT_NOTE, time.time(), row["id"]),
        )
        if changed == 1:
            meeting.notify_outcome(
                row["id"], True, "但自动派活超时没完成，要再点一下“执行决定”续上"
            )
            log.warning("watchdog released stale meeting execution %s", row["id"])
        return changed == 1
    settled = meeting.settle_failure(row["id"], _MEETING_TIMEOUT_MESSAGE)
    if settled:
        meeting.notify_outcome(row["id"], False, "超时没有开完")
        log.warning("watchdog settled stale meeting %s", row["id"])
    return bool(settled)


def settle_stale_job(engine, row: dict) -> bool:
    if not _claim_stale("job", row):
        return False
    keep_delivery = bool(engine._has_usable_delivery(row["id"]))
    message = _KEEP_DELIVERY_MESSAGE if keep_delivery else TIMEOUT_MESSAGE
    engine.settle_failure(row["id"], message)
    current = db.one("SELECT status FROM job WHERE id=?", (row["id"],))
    if not current or current["status"] != "failed":
        return False
    log.warning("watchdog settled stale job %s", row["id"])
    # 老板通知由 engine.settle_failure 统一写(job_failed)，这里不再重复推。
    return True


def reconcile_task_refund(task_id: int) -> bool:
    """failed+charged 专家任务补退款;沿用原失败说明,不覆盖成超时文案。"""
    from . import taskrunner
    row = db.one("SELECT output_md FROM task WHERE id=?", (task_id,))
    message = str((row or {}).get("output_md") or "").strip() or TIMEOUT_MESSAGE
    return bool(taskrunner.settle_failure(task_id, message))


def reconcile_meeting_refund(meeting_id: int) -> bool:
    from . import meeting
    row = db.one("SELECT next_action FROM meeting WHERE id=?", (meeting_id,))
    message = (
        str((row or {}).get("next_action") or "").strip() or _MEETING_TIMEOUT_MESSAGE
    )
    return bool(meeting.settle_failure(meeting_id, message))


# ---------------- 进程内在跑判定(事件循环上执行) ----------------
def _job_busy(engine, job_id: int) -> bool:
    if engine is None:
        return True
    lock = getattr(engine, "locks", {}).get(job_id)
    if lock is not None and lock.locked():
        return True
    queue = getattr(engine, "queue", None)
    pending = getattr(queue, "_queue", None)
    try:
        return pending is not None and job_id in list(pending)
    except Exception:
        return True


def _task_busy(task_id: int) -> bool:
    from . import taskrunner
    return task_id in taskrunner.RUNNING


def _meeting_busy(meeting_id: int) -> bool:
    from . import meeting
    return meeting.is_active(meeting_id)


async def _settle_each(kind: str, rows: list[dict], busy, settle) -> int:
    settled = 0
    for row in rows:
        try:
            # 在跑判定放在事件循环上读(运行集合只在循环线程里改),
            # 结算进线程池;两步之间若执行器抢先开跑,CAS 认领会落空。
            if busy(row["id"]):
                continue
            if await asyncio.to_thread(settle, row):
                settled += 1
        except Exception as exc:
            log.error(
                "watchdog %s %s settle failed error_type=%s",
                kind,
                row.get("id"),
                type(exc).__name__,
            )
    return settled


async def sweep(engine=None, now: float | None = None) -> dict:
    """扫描一遍并收口;返回各类收口数量(测试/诊断用)。"""
    now = time.time() if now is None else now
    limits = thresholds()
    result = {"task": 0, "meeting": 0, "job": 0, "refund": 0}

    try:
        rows = await asyncio.to_thread(stale_tasks, now, limits["task"])
        result["task"] = await _settle_each("task", rows, _task_busy, settle_stale_task)
    except Exception as exc:
        log.error("watchdog task scan failed error_type=%s", type(exc).__name__)

    try:
        rows = await asyncio.to_thread(stale_meetings, now, limits["meeting"])
        result["meeting"] = await _settle_each(
            "meeting", rows, _meeting_busy, settle_stale_meeting
        )
    except Exception as exc:
        log.error("watchdog meeting scan failed error_type=%s", type(exc).__name__)

    if engine is not None:
        try:
            rows = await asyncio.to_thread(stale_jobs, now, limits["job"])
            result["job"] = await _settle_each(
                "job",
                rows,
                lambda job_id: _job_busy(engine, job_id),
                lambda row: settle_stale_job(engine, row),
            )
        except Exception as exc:
            log.error("watchdog job scan failed error_type=%s", type(exc).__name__)

    # 结算中途出错留下的 failed+charged:补一次幂等退款(CAS 保证只退一次)。
    try:
        for task_id in await asyncio.to_thread(failed_charged_tasks, now):
            if _task_busy(task_id):
                continue
            if await asyncio.to_thread(reconcile_task_refund, task_id):
                result["refund"] += 1
        for meeting_id in await asyncio.to_thread(failed_charged_meetings, now):
            if _meeting_busy(meeting_id):
                continue
            if await asyncio.to_thread(reconcile_meeting_refund, meeting_id):
                result["refund"] += 1
    except Exception as exc:
        log.error("watchdog refund reconcile failed error_type=%s", type(exc).__name__)

    if any(result.values()):
        log.warning("watchdog sweep %s", json.dumps(result, sort_keys=True))
    return result


async def loop(engine):
    """常驻循环:每 interval 扫一次;任何异常都吞掉记日志,循环本身不退出。"""
    log.info("watchdog started")
    while True:
        try:
            await asyncio.sleep(interval_seconds())
            await sweep(engine)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("watchdog loop failed error_type=%s", type(exc).__name__)
            try:
                await asyncio.sleep(_ERROR_BACKOFF_SECONDS)
            except asyncio.CancelledError:
                raise
