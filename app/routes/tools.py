"""营销工具箱的 HTTP 路由 + 工具作业 worker/看门狗(第 3 期从 main.py 机械拆分，函数体未改)。

main.py 启动段仍调用这里的 _recover_interrupted_tool_jobs / _ensure_tool_running_index /
_start_tool_watchdog(经 main 重新导出)。不 import main.py。
"""


import asyncio
import json
import logging
import os
import sqlite3
import time

from fastapi import APIRouter, File as _File, Form, HTTPException, UploadFile as _UploadFile

from .. import auth, avatar, billing, db, growth, notify, providers
from ..engine import engine
from ..skills import registry
from ..web_common import (
    INDUSTRIES, ROOT, TEN, _is_boss, _need_module, _page_result, _pagination,
    _public_failure_for_view, _public_progress_for_view, _read_limited, _run_db_safely,
    _preflight_user_video_brand,
    _run_db_then_start_worker_safely, _start_billed_operation, _start_billing_operation_safely,
)


log = logging.getLogger("main")  # 与拆分前同名，日志检索口径不变
router = APIRouter()


# ---------------- ③ 营销工具箱(长任务=后台作业:挂起可回看,关页面不丢) ----------------
TOOL_KINDS = {"hot": "今日必发", "pcal": "私域日历", "warm": "起号军师",
              "leads": "线索雷达", "bench": "竞品盯梢"}
TOOL_REFUND = {"hot": "hot_pick", "pcal": "pcal", "warm": "warmup",
               "leads": "leads", "bench": "bench_watch"}
TOOL_TIMEOUTS = {"hot": 300, "pcal": 300, "warm": 360, "leads": 360, "bench": 360}
TOOL_STALE_GRACE = 60
_TOOL_TASKS = set()
_TOOL_WATCHDOG_TASK = None


def _broadcast_tool(tid: int, kind: str):
    try:
        engine.broadcast({"type": "tool_update", "tenant_id": tid, "kind": kind})
    except Exception as exc:
        try:
            log.error(
                "tool_job broadcast failed tenant=%s kind=%s error_type=%s",
                tid,
                kind,
                type(exc).__name__,
            )
        except Exception:
            pass


def _fail_tool_job(row: dict, error: str, refund_note: str = "后台任务失败退回") -> bool:
    """CAS 抢占失败状态，并在同一个 SQLite 事务里完成退款，重复调用安全。"""
    jid, tid, kind = row["id"], row["tenant_id"], row["kind"]
    message = (str(error or "后台任务失败").strip() or "后台任务失败")[:200]
    now = time.time()

    def claim(c):
        cur = c.execute(
            "UPDATE tool_job SET status='failed',billing_status='refunded',"
            "error=?,progress=?,updated_at=? "
            "WHERE id=? AND status='running' AND billing_status='charged'",
            (message, "任务已结束，可重新发起", now, jid),
        )
        return cur.rowcount == 1

    points = row.get("billing_points")
    if points is None:  # 仅兼容升级前仍在 running 的旧记录。
        action = TOOL_REFUND.get(kind, "expert_task")
        points = float((billing.prices().get(action) or {"points": 1})["points"])
    return billing.refund_amount_if_claimed(
        tid, points, claim, f"退回:{refund_note}"
    )


def _settle_tool_failure(row: dict, error: str, refund_note: str) -> bool:
    """worker 的防火墙：结算暂时失败时保留 running，交给看门狗稍后重试。"""
    try:
        return _fail_tool_job(row, error, refund_note)
    except Exception as exc:
        try:
            log.error(
                "settle tool_job %s failure failed error_type=%s",
                row.get("id"),
                type(exc).__name__,
            )
        except Exception:
            pass
        return False


def _settle_unstarted_tool_result(result: dict) -> bool:
    job_id = int((result or {}).get("job_id") or 0)
    row = db.one("SELECT * FROM tool_job WHERE id=?", (job_id,))
    if not row:
        return False
    return _settle_tool_failure(
        row,
        "工具任务启动失败，系统已安全终止并退回本次点数",
        "启动失败退回",
    )


def _recover_interrupted_tool_jobs():
    """服务重启时，旧进程留下的 running 已不可能继续，立即收口并退点。"""
    # pending_charge 从未扣款，直接清理；不能把它误当成已付费任务退款。
    db.q(
        "DELETE FROM tool_job "
        "WHERE status='pending_charge' AND billing_status='pending'"
    )
    for row in db.q("SELECT * FROM tool_job WHERE status='running'"):
        try:
            if _fail_tool_job(row, "服务重启中断，已自动结束，请重新发起", "重启中断退回"):
                _broadcast_tool(row["tenant_id"], row["kind"])
        except Exception as exc:
            try:
                log.error(
                    "recover interrupted tool_job %s failed error_type=%s",
                    row["id"],
                    type(exc).__name__,
                )
            except Exception:
                pass


def _ensure_tool_running_index():
    """同租户同工具只允许一条待扣款或运行记录，堵住并发双击窗口。"""
    db.execute("DROP INDEX IF EXISTS idx_tool_job_one_running")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_job_one_active "
        "ON tool_job(tenant_id, kind) "
        "WHERE status IN ('pending_charge','running')"
    )


def _recover_stale_tool_jobs(
    now: float = None, defer_broadcast: bool = False
) -> int | tuple[int, list[tuple[int, str]]]:
    """按创建时间执行绝对总时限；心跳只供展示，不能把截止时间越续越长。"""
    now = now or time.time()
    recovered = 0
    events = []
    for row in db.q("SELECT * FROM tool_job WHERE status='running'"):
        timeout = TOOL_TIMEOUTS.get(row["kind"], 360)
        # 免费重试沿用原记录，必须从本次 retry_started_at 重新计时；否则历史
        # created_at 会让刚重排的任务被看门狗立即判为超时。
        started_at = (
            row.get("retry_started_at")
            or row.get("created_at")
            or row.get("updated_at")
            or now
        )
        if now - started_at <= timeout + TOOL_STALE_GRACE:
            continue
        minutes = max(1, round(timeout / 60))
        try:
            if _fail_tool_job(
                    row, f"运行超过{minutes}分钟仍未完成，已自动结束并退回点数，请重试",
                    "超时自动退回"):
                recovered += 1
                if defer_broadcast:
                    events.append((row["tenant_id"], row["kind"]))
                else:
                    _broadcast_tool(row["tenant_id"], row["kind"])
        except Exception as exc:
            try:
                log.error(
                    "recover stale tool_job %s failed error_type=%s",
                    row["id"],
                    type(exc).__name__,
                )
            except Exception:
                pass
    return (recovered, events) if defer_broadcast else recovered


async def _tool_watchdog_loop():
    while True:
        try:
            await asyncio.sleep(60)
            _recovered, events = await db.arun(
                _recover_stale_tool_jobs, None, True
            )
            for tenant_id, kind in events:
                _broadcast_tool(tenant_id, kind)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            try:
                log.error(
                    "tool_job watchdog failed error_type=%s",
                    type(exc).__name__,
                )
            except Exception:
                pass


def _start_tool_watchdog():
    global _TOOL_WATCHDOG_TASK
    if _TOOL_WATCHDOG_TASK is None or _TOOL_WATCHDOG_TASK.done():
        _TOOL_WATCHDOG_TASK = asyncio.create_task(_tool_watchdog_loop())


def _spawn_tool_worker(jid: int):
    """保留后台 task 的强引用，直到它真正收口，避免被事件循环提前回收。"""
    task = asyncio.create_task(_tool_worker(jid))
    _TOOL_TASKS.add(task)

    def finished(done):
        _TOOL_TASKS.discard(done)
        if done.cancelled():
            return
        try:
            error = done.exception()
        except (asyncio.CancelledError, Exception):
            return
        if error:
            try:
                log.error(
                    "tool_job %s worker escaped error_type=%s",
                    jid,
                    type(error).__name__,
                )
            except Exception:
                pass

    task.add_done_callback(finished)
    return task


async def _run_tool(row: dict, progress) -> dict:
    tid, kind = row["tenant_id"], row["kind"]
    p = db.jloads(row["params_json"], {})
    if kind == "hot":
        return await growth.hot_pick(tid, p.get("industry") or "通用",
                                     p.get("channels") or [], save=False)
    if kind == "pcal":
        return await growth.private_calendar(tid, p.get("industry") or "通用",
                                             p.get("focus") or "", p["ym"],
                                             save=False)
    if kind == "warm":
        return await growth.warmup_plan(
            tid, p.get("platform") or "小红书", p.get("industry") or "通用",
            p.get("positioning") or "", "", persona_text=p.get("persona_text") or ""
        )
    if kind == "leads":
        return await growth.leads_radar(
            tid, p.get("industry") or "通用", p.get("city") or "",
            p.get("product") or "", progress=progress
        )
    if kind == "bench":
        return await growth.bench_report(tid, save=False)
    raise ValueError("未知工具")


def _persist_tool_result(connection, row: dict, result: dict, now: float) -> bool:
    """业务结果、可读缓存与计费成功在同一事务里出现。"""
    jid, tid, kind = row["id"], row["tenant_id"], row["kind"]
    changed = connection.execute(
        "UPDATE tool_job SET status='done',result_json=?,error=NULL,progress=?,"
        "billing_status='succeeded',updated_at=? "
        "WHERE id=? AND status='running' AND billing_status='charged'",
        (
            json.dumps(result, ensure_ascii=False),
            "任务已完成",
            now,
            jid,
        ),
    )
    if changed.rowcount != 1:
        return False
    params = db.jloads(row.get("params_json"), {})
    settings = []
    if kind == "pcal":
        ym = str(params.get("ym") or "")[:7]
        settings.append((
            f"pcal:{tid}:{ym}",
            json.dumps(result, ensure_ascii=False),
        ))
    elif kind == "hot":
        date = str(result.get("date") or "")[:10]
        industry = str(result.get("industry") or params.get("industry") or "通用")[:20]
        channels = result.get("channels") if isinstance(result.get("channels"), list) else []
        settings.extend((
            (f"hotpick_channels:{tid}", json.dumps(channels, ensure_ascii=False)),
            (
                f"hotpick:{tid}:{date}:{industry}",
                json.dumps(result, ensure_ascii=False),
            ),
        ))
    elif kind == "bench":
        key = f"bench_watch:{tid}"
        current = connection.execute(
            "SELECT value FROM app_setting WHERE key=?", (key,)
        ).fetchone()
        conf = db.jloads(current["value"], {}) if current else {}
        conf["last_run"] = now
        settings.append((key, json.dumps(conf, ensure_ascii=False)))
    for key, value in settings:
        connection.execute(
            "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            "updated_at=excluded.updated_at",
            (key, value, now),
        )
    return True


async def _tool_worker(jid: int):
    try:
        row = await db.aone("SELECT * FROM tool_job WHERE id=?", (jid,))
    except Exception as exc:
        try:
            log.error(
                "tool_job %s initial read failed; watchdog will retry "
                "error_type=%s",
                jid,
                type(exc).__name__,
            )
        except Exception:
            pass
        return
    if not row or row["status"] != "running":
        return
    tid, kind = row["tenant_id"], row["kind"]
    progress_last = {"at": 0.0, "label": ""}

    def progress(_step: str, label: str):
        # 步骤上报是旁路能力：限频写心跳，任何异常都不能打断真实任务。
        now = time.time()
        label = (str(label or "正在处理").strip() or "正在处理")[:160]
        if label == progress_last["label"] and now - progress_last["at"] < 15:
            return
        if now - progress_last["at"] < 8:
            return
        try:
            db.submit_write(
                db.execute,
                "UPDATE tool_job SET progress=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (label, now, jid),
            )
            progress_last.update({"at": now, "label": label})
        except Exception:
            pass

    try:
        progress("boot", f"{TOOL_KINDS.get(kind, kind)}已接单，正在启动…")
        r = await asyncio.wait_for(
            _run_tool(row, progress), timeout=TOOL_TIMEOUTS.get(kind, 360)
        )
        if not isinstance(r, dict):
            raise ValueError("工具没有返回有效结果")
        r = dict(r)
        r.pop("cost_usd", None)
        r.pop("tokens", None)
        def _commit_result():
            with db.atomic() as connection:
                return _persist_tool_result(
                    connection, row, r, time.time()
                )

        changed = await db.arun(_commit_result)
        if changed:
            try:
                await asyncio.to_thread(
                    notify.push,
                    tid,
                    "report",
                    {
                        "report_name": (
                            f"{TOOL_KINDS.get(kind, kind)}跑完了"
                        ),
                        "summary": "结果已经摆在工具箱里,回来就能看",
                        "link": "#/tools",
                    },
                )
            except Exception as exc:
                try:
                    log.error(
                        "tool_job %s notification failed error_type=%s",
                        jid,
                        type(exc).__name__,
                    )
                except Exception:
                    pass
    except asyncio.TimeoutError:
        minutes = max(1, round(TOOL_TIMEOUTS.get(kind, 360) / 60))
        await db.arun(
            _settle_tool_failure,
            row,
            f"运行超过{minutes}分钟仍未完成，已自动结束并退回点数，请重试",
            "超时自动退回",
        )
        try:
            log.warning("tool_job %s(%s) timed out", jid, kind)
        except Exception:
            pass
    except asyncio.CancelledError:
        await db.arun(
            _settle_tool_failure,
            row,
            "任务被服务中断，已自动结束，请重新发起",
            "服务中断退回",
        )
        raise
    except Exception as e:
        # 先收口状态和退款，再记日志；即使日志组件自身出错，用户也不会再看到永久 running。
        public_error = providers.public_failure_message(e)
        await db.arun(
            _settle_tool_failure,
            row,
            public_error,
            "后台任务失败退回",
        )
        try:
            log.error(
                "tool_job %s(%s) failed error_type=%s",
                jid,
                kind,
                type(e).__name__,
            )
        except Exception:
            pass
    finally:
        _broadcast_tool(tid, kind)


def _tool_require_idle(kind: str):
    if db.one("SELECT id FROM tool_job WHERE tenant_id=? AND kind=? "
              "AND status IN ('pending_charge','running')",
              (TEN(), kind)):
        raise HTTPException(429, "这个工具已有一个任务在后台跑,等它完事再派新的")


def _tool_enqueue_record(kind: str, params: dict, note: str = "") -> dict:
    """先落任务再原子扣点；任何插入/并发失败都不会碰用户余额。"""
    tid = TEN()
    action = TOOL_REFUND.get(kind, "expert_task")
    points = 0.0 if tid == 1 else float(
        (billing.prices().get(action) or {"points": 1})["points"]
    )
    try:
        jid = db.insert("tool_job", {
            "tenant_id": tid, "kind": kind,
            "params_json": json.dumps(params, ensure_ascii=False),
            "created_by": int((auth.current() or {}).get("id") or 0) or None,
            "status": "pending_charge",
            "billing_status": "pending",
            "billing_points": points,
            "progress": "任务已进入后台队列",
        })
    except sqlite3.IntegrityError:
        raise HTTPException(429, "这个工具已有一个任务在后台跑，等它完成后再试")

    def claim(connection):
        changed = connection.execute(
            "UPDATE tool_job SET status='running',billing_status='charged',updated_at=? "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (time.time(), jid),
        )
        return changed.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            action, tid, claim,
            note=(f"工具单#{jid}·{note}" if note else f"工具单#{jid}")[:160],
            points=points
        )
    except billing.InsufficientPoints as exc:
        db.q(
            "DELETE FROM tool_job WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (jid,),
        )
        raise HTTPException(402, str(exc)) from exc
    except Exception:
        db.q(
            "DELETE FROM tool_job WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (jid,),
        )
        raise
    if not charged:
        raise RuntimeError("工具任务计费状态冲突")
    return {"job_id": jid, "note": "已挂到后台跑:您随便去忙别的,回工具箱就能看到;跑完还会推微信"}


def _tool_enqueue(kind: str, params: dict, note: str = "") -> dict:
    """同步兼容入口；HTTP 协程使用 ``_tool_enqueue_async``。"""
    result = _tool_enqueue_record(kind, params, note)
    _spawn_tool_worker(result["job_id"])
    return result


async def _tool_enqueue_async(
    kind: str, params: dict, note: str = ""
) -> dict:
    """完整扣费事务进 DB 池，回到事件循环后再创建 asyncio worker。"""
    await db.arun(_tool_require_idle, kind)
    result = await _run_db_then_start_worker_safely(
        _tool_enqueue_record,
        kind,
        params,
        note,
        start_worker=lambda queued: _spawn_tool_worker(queued["job_id"]),
        settle_unstarted=_settle_unstarted_tool_result,
    )
    return result


@router.get("/api/tools/jobs")
def tool_jobs(
    limit: int = None,
    offset: int = 0,
    kind: str = "",
    status: str = "",
):
    _need_module("content")
    page_limit, page_offset, paged = _pagination(limit, offset, 15)
    where = ["tenant_id=?"]
    params = [TEN()]
    kind = (kind or "").strip()[:30]
    status = (status or "").strip()[:30]
    if kind:
        where.append("kind=?")
        params.append(kind)
    if status:
        where.append("status=?")
        params.append(status)
    where_sql = " AND ".join(where)
    rows = db.q(
        f"SELECT * FROM tool_job WHERE {where_sql} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_limit, page_offset),
    )
    items = []
    for r in rows:
        k = r["kind"]
        items.append({
            "id": r["id"], "kind": k, "status": r["status"],
            "error": _public_failure_for_view(
                r.get("status"), r.get("error"), _is_boss()),
            "params": db.jloads(r["params_json"], {}),
            "result": db.jloads(r["result_json"], None)
            if r["status"] == "done" else None,
            "progress": _public_progress_for_view(
                r.get("status"), r.get("progress"), _is_boss()),
            "created_at": r["created_at"], "updated_at": r.get("updated_at"),
            "timeout_seconds": TOOL_TIMEOUTS.get(k, 360),
        })
    if not paged and not any((kind, status)):
        latest = {}
        for item in items:
            latest.setdefault(item["kind"], item)
        return list(latest.values())
    total = db.one(
        f"SELECT COUNT(*) AS n FROM tool_job WHERE {where_sql}", tuple(params)
    )["n"]
    return _page_result(items, total, page_limit, page_offset)
@router.get("/api/tools/meta")
def tools_meta():
    _need_module("content")
    return {"festivals": growth.upcoming_festivals(30), "voices": avatar.VOICES,
            "cloned": avatar.cloned_voices(),
            "bench": growth.watch_conf(TEN()),
            "hot_channels": growth.HOT_CHANNELS,
            "hot_channels_saved": growth.hot_channels_saved(TEN()),
            "hot_daily": growth.hot_daily_conf(TEN()),
            "bgm_moods": [{"key": k, "label": v["label"]} for k, v in
                          __import__("app.textvideo", fromlist=["x"]).BGM_MOODS.items()]
                         + [{"key": "none", "label": "不配乐"}],
            "industries": INDUSTRIES}


@router.get("/api/tools/pcal")
def pcal_get(ym: str):
    _need_module("content")
    return growth.get_calendar(TEN(), ym) or {}


@router.post("/api/tools/pcal")
async def pcal_gen(body: dict):
    await db.arun(_need_module, "content")
    ym = body.get("ym") or time.strftime("%Y-%m")
    return await _tool_enqueue_async(
        "pcal",
        {
            "ym": ym,
            "industry": (body.get("industry") or "通用")[:20],
            "focus": (body.get("focus") or "")[:200],
        },
        note=ym,
    )


@router.put("/api/tools/pcal")
def pcal_edit(body: dict):
    _need_module("content")
    try:
        growth.save_calendar_edits(TEN(), body.get("ym") or "", body.get("days") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/api/tools/pcal/feishu")
async def pcal_feishu(body: dict):
    _need_module("content")
    try:
        return await growth.calendar_to_feishu(TEN(), body.get("ym") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/api/tools/hot-daily")
def hot_daily_put(body: dict):
    _need_module("content")
    conf = growth.save_hot_daily(TEN(), body.get("enabled"),
                                 body.get("industry") or "通用", body.get("channels") or [])
    return {"ok": True, "enabled": conf["enabled"]}


@router.get("/api/tools/hotpick")
def hotpick_get(industry: str = "通用"):
    _need_module("content")
    return growth.get_hot_pick(TEN(), industry) or {"festivals": growth.upcoming_festivals(7)}


@router.post("/api/tools/hotpick")
async def hotpick_gen(body: dict):
    await db.arun(_need_module, "content")
    industry = (body.get("industry") or "通用")[:20]
    return await _tool_enqueue_async(
        "hot",
        {
            "industry": industry,
            "channels": (body.get("channels") or [])[:10],
        },
        note=industry,
    )


@router.post("/api/tools/warmup")
async def warmup_gen(body: dict):
    await db.arun(_need_module, "content")
    persona_text = ""
    if body.get("profile_id"):
        pr = await db.aone(
            "SELECT * FROM account_profile WHERE id=? AND tenant_id=? "
            "AND deleted_at IS NULL",
            (body["profile_id"], TEN()),
        )
        if pr:
            persona_text = registry._persona_text({"persona": db.jloads(pr["persona_json"], {})})
    return await _tool_enqueue_async(
        "warm",
        {
            "platform": body.get("platform") or "小红书",
            "industry": (body.get("industry") or "通用")[:20],
            "positioning": (body.get("positioning") or "")[:200],
            "persona_text": persona_text[:1500],
        },
        note=(body.get("platform") or "") + (body.get("industry") or ""),
    )


@router.post("/api/tools/leads")
async def leads_gen(body: dict):
    await db.arun(_need_module, "content")
    return await _tool_enqueue_async(
        "leads",
        {
            "industry": (body.get("industry") or "通用")[:20],
            "city": (body.get("city") or "")[:20],
            "product": (body.get("product") or "")[:60],
        },
        note=(body.get("city") or "") + (body.get("industry") or ""),
    )


@router.get("/api/tools/bench")
def bench_get():
    _need_module("content")
    conf = growth.watch_conf(TEN())
    return conf


@router.put("/api/tools/bench")
def bench_put(body: dict):
    _need_module("content")
    targets = growth.save_watch(TEN(), body.get("targets") or [], body.get("enabled"))
    return {"ok": True, "n": len(targets)}


@router.post("/api/tools/bench/run-now")
async def bench_run(body: dict = None):
    await db.arun(_need_module, "content")
    if not (await db.arun(growth.watch_conf, TEN())).get("targets"):
        raise HTTPException(400, "先在上面添加要盯的对标账号并保存")
    return await _tool_enqueue_async("bench", {}, note="手动")


def _tool_image_base64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode()


def _store_tool_image(data: bytes, tid: int) -> tuple[str, str]:
    """Durably store one generated image and remove partial writes on failure."""
    import uuid
    directory = os.path.join(ROOT, "data", "assets", "tools", str(tid))
    os.makedirs(directory, exist_ok=True)
    name = f"shot_{uuid.uuid4().hex[:10]}.png"
    path = os.path.join(directory, name)
    try:
        with open(path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path, name


def _remove_tool_image(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


async def _store_tool_image_safely(data: bytes, tid: int) -> tuple[str, str]:
    """Keep blocking writes off-loop and avoid an orphan on cancellation."""
    write_task = asyncio.create_task(
        asyncio.to_thread(_store_tool_image, data, tid)
    )
    try:
        return await asyncio.shield(write_task)
    except asyncio.CancelledError:
        stored = None
        try:
            stored = await write_task
        except BaseException:
            pass
        if stored:
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(_remove_tool_image, stored[0])
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
        raise


@router.post("/api/tools/menu-copy")
async def menu_copy_api(file: _UploadFile = _File(...), want: str = Form("")):
    await db.arun(_need_module, "content")
    raw = await _read_limited(file, 8 * 1024 * 1024, "图片太大(≤8MB)")
    op_key = await _start_billing_operation_safely(
        _start_billed_operation,
        "menu_copy",
        cancel_reason="识图请求中断自动退回",
    )
    mime = file.content_type if (file.content_type or "").startswith("image/") else "image/jpeg"
    try:
        result = await growth.menu_copy(
            TEN(),
            await asyncio.to_thread(_tool_image_base64, raw),
            mime,
            want,
        )
        if not await _run_db_safely(billing.complete_operation, op_key):
            raise RuntimeError("计费操作状态冲突")
        return result
    except asyncio.CancelledError:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "请求中断自动退回",
        )
        raise
    except Exception:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "识图失败自动退回",
        )
        raise HTTPException(500, "识图失败,点数已退回,换张清晰的图试试")


@router.post("/api/tools/product-shot")
async def product_shot_api(file: _UploadFile = _File(...), scene: str = Form("")):
    await db.arun(_need_module, "content")
    raw = await _read_limited(file, 8 * 1024 * 1024, "图片太大(≤8MB)")
    op_key = await _start_billing_operation_safely(
        _start_billed_operation,
        "product_shot",
        cancel_reason="商品图请求中断自动退回",
    )
    path = ""
    try:
        data = await growth.product_shot(TEN(), raw, scene)
        path, name = await _store_tool_image_safely(data, TEN())
        if not await _run_db_safely(billing.complete_operation, op_key):
            raise RuntimeError("计费操作状态冲突")
        return {"file": f"/files/tools/{TEN()}/{name}"}
    except asyncio.CancelledError:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "请求中断自动退回",
        )
        await asyncio.to_thread(_remove_tool_image, path)
        raise
    except Exception:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "商品图生成或保存失败自动退回",
        )
        await asyncio.to_thread(_remove_tool_image, path)
        raise HTTPException(500, "美化失败，点数已退回，请稍后重试")


@router.post("/api/tools/photo-factory")
async def photo_factory_api(file: _UploadFile = _File(...), scene: str = Form(""),
                            want: str = Form("")):
    """拍照工厂:一次上传同时出「商业海报图 + 全套文案」。

    此前前端并行调 product-shot 与 menu-copy 两个接口,同一张 8MB 照片要上传
    两遍(手机 4G 下时间翻倍)。合并为一次上传、服务器侧并发跑两条腿;
    两条腿各自独立计费与退款,哪条失败退哪条的点,响应里把每条腿的结果
    与失败原因分开说清,老板不用猜"钱花在哪了"。
    """
    await db.arun(_need_module, "content")
    raw = await _read_limited(file, 8 * 1024 * 1024, "图片太大(≤8MB)")
    mime = file.content_type if (file.content_type or "").startswith("image/") else "image/jpeg"
    b64 = await asyncio.to_thread(_tool_image_base64, raw)

    # 两条腿的计费操作先后开好:第二条点数不足时退掉第一条,给合并后的
    # 提示,而不是让老板看到"本次需 1 点"这种只说半截的话。
    shot_op = await _start_billing_operation_safely(
        _start_billed_operation,
        "product_shot",
        cancel_reason="拍照工厂商品图请求中断自动退回",
    )
    try:
        copy_op = await _start_billing_operation_safely(
            _start_billed_operation,
            "menu_copy",
            cancel_reason="拍照工厂文案请求中断自动退回",
        )
    except BaseException as exc:
        await _run_db_safely(
            billing.fail_operation,
            shot_op,
            "拍照工厂另一半未启动,整体退回",
        )
        if isinstance(exc, HTTPException) and exc.status_code == 402:
            raise HTTPException(
                402, "拍照工厂一次需 3 点(出图2+文案1),当前余额不足。请充值后再试"
            ) from exc
        raise

    async def _shot_leg(op_key):
        path = ""
        try:
            data = await growth.product_shot(TEN(), raw, scene)
            path, name = await _store_tool_image_safely(data, TEN())
            if not await _run_db_safely(
                billing.complete_operation,
                op_key,
            ):
                raise RuntimeError("计费操作状态冲突")
            return {"file": f"/files/tools/{TEN()}/{name}"}
        except asyncio.CancelledError:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "请求中断自动退回",
            )
            await asyncio.to_thread(_remove_tool_image, path)
            raise
        except Exception:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "商品图生成或保存失败自动退回",
            )
            await asyncio.to_thread(_remove_tool_image, path)
            return {"error": "美化没成功,这条腿的 2 点已退回;可换张更清晰的图重试"}

    async def _copy_leg(op_key):
        try:
            result = await growth.menu_copy(TEN(), b64, mime, want)
            if not await _run_db_safely(
                billing.complete_operation,
                op_key,
            ):
                raise RuntimeError("计费操作状态冲突")
            return {"menu": result}
        except asyncio.CancelledError:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "请求中断自动退回",
            )
            raise
        except Exception:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "识图失败自动退回",
            )
            return {"error": "文案没写成,这条腿的 1 点已退回;可换张更清晰的图重试"}

    shot_result, copy_result = await asyncio.gather(
        _shot_leg(shot_op), _copy_leg(copy_op))
    if shot_result.get("error") and copy_result.get("error"):
        raise HTTPException(500, "图和文案都没成功,3 点已全部退回;换张清晰的图再试")
    return {
        "file": shot_result.get("file") or "",
        "image_error": shot_result.get("error") or "",
        "menu": copy_result.get("menu"),
        "copy_error": copy_result.get("error") or "",
    }


@router.post("/api/tools/variants")
async def variants_api(body: dict):
    await db.arun(_need_module, "content")
    script = (body.get("script") or "").strip()
    if len(script) < 30:
        raise HTTPException(400, "先贴一篇口播稿(至少30字)")
    await _preflight_user_video_brand("", script[:2500])
    op_key = await _start_billing_operation_safely(
        _start_billed_operation,
        "matrix_variants",
        cancel_reason="裂变请求中断自动退回",
    )
    try:
        result = await growth.script_variants(
            TEN(), script, body.get("n") or 3, body.get("styles") or ""
        )
        if not await _run_db_safely(billing.complete_operation, op_key):
            raise RuntimeError("计费操作状态冲突")
        return result
    except asyncio.CancelledError:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "请求中断自动退回",
        )
        raise
    except Exception:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "裂变失败自动退回",
        )
        raise HTTPException(500, "裂变失败,点数已退回,请重试")
