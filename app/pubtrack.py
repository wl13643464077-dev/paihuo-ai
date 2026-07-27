"""发布台账 + 数据自动回流(V25):复盘官自动上岗.

- 发草稿箱成功自动登记;其他平台老板一键手动登记(填标题+平台+日期,10秒);
- T+1/3/7 到点(北京时间上午9-22点)自动处理:
  ① 配了公众号 API 且是公众号内容 → 拉取已群发文章的真实数据(阅读/在看/分享,
     需认证号的数据分析权限)→ 审查官自动出复盘报告 → 微信通知;
  ② 拉不到数据(没配API/其他平台) → 微信+站内提醒"该复盘了",一键贴数据复盘。
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import billing, censor, db, notify, wechat

log = logging.getLogger("pubtrack")
TZ = ZoneInfo("Asia/Shanghai")
RETRO_DAYS = (1, 3, 7)
RETRO_DONE = "done"
RETRO_NO_DATA = "no_data"
RETRO_RETRYABLE = "retryable"


class RetroBusy(ValueError):
    pass


def _has_processing(retro: dict) -> bool:
    return any(
        isinstance(state, dict) and state.get("state") == "processing"
        for state in (retro or {}).values()
    )


def add_entry(tid: int, platform: str, title: str, job_id=None, url: str = "",
              source: str = "manual", published_at: float = None) -> int:
    published_at = published_at or time.time()
    retro = {str(d): {"due": published_at + d * 86400, "state": "pending"} for d in RETRO_DAYS}
    return db.insert("publish_log", {
        "tenant_id": tid, "platform": platform, "title": (title or "")[:80],
        "job_id": job_id, "url": url, "source": source, "published_at": published_at,
        "retro_json": json.dumps(retro)})


def entries(tid: int, limit: int = 60, offset: int = 0) -> list:
    rows = db.q(
        "SELECT * FROM publish_log WHERE tenant_id=? "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (tid, limit, offset),
    )
    for r in rows:
        r["retro"] = db.jloads(r.pop("retro_json"), {})
    return rows


def entries_total(tid: int) -> int:
    return int(db.one(
        "SELECT COUNT(*) n FROM publish_log WHERE tenant_id=?", (tid,)
    )["n"] or 0)


def mark_published(tid: int, pid: int):
    """老板点「我已群发」:把复盘钟表从现在重新起算."""
    now = time.time()
    retro = {str(d): {"due": now + d * 86400, "state": "pending"} for d in RETRO_DAYS}
    with db.atomic() as connection:
        row = connection.execute(
            "SELECT retro_json FROM publish_log WHERE id=? AND tenant_id=?",
            (pid, tid),
        ).fetchone()
        if not row:
            raise ValueError("台账记录不存在")
        if _has_processing(db.jloads(row["retro_json"], {})):
            raise RetroBusy("自动复盘正在结算，请稍后再重置发布时间")
        connection.execute(
            "UPDATE publish_log SET published_at=?,retro_json=?,updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (now, json.dumps(retro), now, pid, tid),
        )


def delete_entry(tid: int, pid: int) -> bool:
    """处理中记录保留计费锚点；其余记录在同一事务安全删除。"""
    with db.atomic() as connection:
        row = connection.execute(
            "SELECT retro_json FROM publish_log WHERE id=? AND tenant_id=?",
            (pid, tid),
        ).fetchone()
        if not row:
            return False
        if _has_processing(db.jloads(row["retro_json"], {})):
            raise RetroBusy("自动复盘正在结算，请稍后再删除")
        changed = connection.execute(
            "DELETE FROM publish_log WHERE id=? AND tenant_id=?", (pid, tid)
        )
        return changed.rowcount == 1


def _mark_retryable_pending(
    tid: int,
    row_id: int,
    day: int,
    expected_published_at: float,
    reason: str,
) -> bool:
    """保留 pending 并指数退避；临时故障不能被误标成已通知后永久停跑。"""
    now = time.time()
    with db.atomic() as connection:
        current = connection.execute(
            "SELECT retro_json,published_at FROM publish_log "
            "WHERE id=? AND tenant_id=?",
            (row_id, tid),
        ).fetchone()
        if not current:
            return False
        if float(current["published_at"] or 0) != float(expected_published_at or 0):
            return False
        retro = db.jloads(current["retro_json"], {})
        state = retro.get(str(day)) or {}
        if state.get("state") not in ("pending", None):
            return False
        attempts = max(0, int(state.get("retry_attempts") or 0)) + 1
        delay = min(6 * 3600, 15 * 60 * (2 ** min(attempts - 1, 5)))
        state.update({
            "state": "pending",
            "retry_attempts": attempts,
            "retry_after": now + delay,
            "last_error": (reason or "临时失败")[:160],
        })
        retro[str(day)] = state
        changed = connection.execute(
            "UPDATE publish_log SET retro_json=?,updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (json.dumps(retro, ensure_ascii=False), now, row_id, tid),
        )
        return changed.rowcount == 1


async def _auto_retro_result(tid: int, row: dict, day: int) -> str:
    """返回 done/no_data/retryable，让调度器区分缺数据与临时故障。"""
    if row["platform"] != "公众号":
        return RETRO_NO_DATA
    conf = await db.arun(wechat.get_conf, tid)
    if not (conf.get("appid") and conf.get("secret")):
        return RETRO_NO_DATA
    try:
        stats = await wechat.article_stats(tid, row["title"], row["published_at"])
    except Exception as exc:
        log.info(
            "拉公众号数据失败 tid=%s error_type=%s",
            tid,
            type(exc).__name__,
        )
        await db.arun(
            _mark_retryable_pending,
            tid,
            int(row["id"]),
            day,
            row.get("published_at"),
            "公众号数据接口临时失败",
        )
        return RETRO_RETRYABLE
    if not stats:
        return RETRO_NO_DATA
    row_id = int(row["id"])
    op_key = f"pubretro:{row_id}:{int(day)}:{uuid.uuid4().hex}"
    previous_state = {"value": "pending"}

    def claim_start(connection):
        current = connection.execute(
            "SELECT retro_json,published_at FROM publish_log "
            "WHERE id=? AND tenant_id=?",
            (row_id, tid),
        ).fetchone()
        if not current:
            return False
        if float(current["published_at"] or 0) != float(row.get("published_at") or 0):
            return False
        retro = db.jloads(current["retro_json"], {})
        state = retro.get(str(day)) or {}
        if state.get("state") == "done":
            return False
        if state.get("state") not in ("pending", "notified", None):
            return False
        previous_state["value"] = state.get("state") or "pending"
        state.update({
            "state": "processing",
            "op_key": op_key,
            "started_at": time.time(),
        })
        retro[str(day)] = state
        changed = connection.execute(
            "UPDATE publish_log SET retro_json=?,updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (json.dumps(retro, ensure_ascii=False), time.time(), row_id, tid),
        )
        return changed.rowcount == 1

    try:
        started = await db.arun(
            billing.start_operation_if_claimed,
            "censor_retro",
            tid,
            claim_start,
            note=f"自动复盘T+{day}·《{(row.get('title') or '')[:14]}》",
            op_key=op_key,
        )
    except billing.InsufficientPoints:
        await db.arun(
            _mark_retryable_pending,
            tid,
            row_id,
            day,
            row.get("published_at"),
            "点数不足，等待充值后重试",
        )
        return RETRO_RETRYABLE
    if not started:
        current = await db.aone(
            "SELECT retro_json FROM publish_log WHERE id=? AND tenant_id=?",
            (row_id, tid),
        )
        state = (db.jloads((current or {}).get("retro_json"), {}).get(str(day)) or {})
        if state.get("state") == "done":
            return RETRO_DONE
        await db.arun(
            _mark_retryable_pending,
            tid,
            row_id,
            day,
            row.get("published_at"),
            "复盘状态冲突，稍后重试",
        )
        return RETRO_RETRYABLE
    data_text = (f"发布 {day} 天(数据来自公众号后台 API):阅读 {stats.get('read_user', '?')} 人 / "
                 f"{stats.get('read_count', '?')} 次,分享 {stats.get('share_user', '?')} 人,"
                 f"在看/收藏 {stats.get('fav_user', '?')},原文点击 {stats.get('ori_read', '—')}")

    def restore_claim(connection):
        current = connection.execute(
            "SELECT retro_json FROM publish_log WHERE id=? AND tenant_id=?",
            (row_id, tid),
        ).fetchone()
        if not current:
            return False
        retro = db.jloads(current["retro_json"], {})
        state = retro.get(str(day)) or {}
        if state.get("state") != "processing" or state.get("op_key") != op_key:
            return False
        attempts = max(0, int(state.get("retry_attempts") or 0)) + 1
        delay = min(6 * 3600, 15 * 60 * (2 ** min(attempts - 1, 5)))
        state.update({
            "state": "pending",
            "retry_attempts": attempts,
            "retry_after": time.time() + delay,
            "last_error": "自动复盘模型临时失败",
        })
        state.pop("op_key", None)
        state.pop("started_at", None)
        retro[str(day)] = state
        changed = connection.execute(
            "UPDATE publish_log SET retro_json=?,updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (json.dumps(retro, ensure_ascii=False), time.time(), row_id, tid),
        )
        return changed.rowcount == 1

    try:
        result = await censor.retro(
            tid, "公众号", row["title"], data_text, save=False
        )

        def complete_claim(connection):
            current = connection.execute(
                "SELECT retro_json FROM publish_log WHERE id=? AND tenant_id=?",
                (row_id, tid),
            ).fetchone()
            if not current:
                return False
            retro = db.jloads(current["retro_json"], {})
            state = retro.get(str(day)) or {}
            if state.get("state") != "processing" or state.get("op_key") != op_key:
                return False
            state.update({"state": "done", "completed_at": time.time()})
            state.pop("op_key", None)
            state.pop("started_at", None)
            state.pop("retry_after", None)
            state.pop("retry_attempts", None)
            state.pop("last_error", None)
            retro[str(day)] = state
            values = censor.retro_log_values(
                tid, "公众号", row["title"], result
            )
            columns = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO censor_log({columns},created_at,updated_at) "
                f"VALUES({placeholders},?,?)",
                [*values.values(), time.time(), time.time()],
            )
            changed = connection.execute(
                "UPDATE publish_log SET retro_json=?,updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (json.dumps(retro, ensure_ascii=False), time.time(), row_id, tid),
            )
            return changed.rowcount == 1

        if not await db.arun(
                billing.complete_operation_if_claimed, op_key, complete_claim):
            raise RuntimeError("自动复盘结算状态冲突")
        try:
            await asyncio.to_thread(
                notify.push,
                tid,
                "report",
                {
                    "report_name": f"《{row['title'][:20]}》T+{day} 自动复盘",
                    "summary": (
                        f"{data_text};评级:{result.get('grade', '—')}"
                    ),
                    "link": "#/censor",
                },
            )
        except Exception as exc:
            log.info(
                "自动复盘通知失败 tid=%s error_type=%s",
                tid,
                type(exc).__name__,
            )
        return RETRO_DONE
    except Exception as exc:
        try:
            await db.arun(
                billing.fail_operation_if_claimed,
                op_key,
                "自动复盘失败",
                restore_claim,
            )
        except Exception as settlement_exc:
            log.error(
                "自动复盘失败结算异常 tid=%s error_type=%s",
                tid,
                type(settlement_exc).__name__,
            )
        log.warning(
            "自动复盘失败 tid=%s error_type=%s",
            tid,
            type(exc).__name__,
        )
        return RETRO_RETRYABLE


async def auto_retro(tid: int, row: dict, day: int) -> bool:
    """兼容手动拉取入口；只有真正完成才返回 True。"""
    return await _auto_retro_result(tid, row, day) == RETRO_DONE


def _mark_notified_if_pending(
    tid: int,
    row_id: int,
    day: int,
    expected_published_at: float,
) -> bool:
    with db.atomic() as connection:
        current = connection.execute(
            "SELECT retro_json,published_at FROM publish_log "
            "WHERE id=? AND tenant_id=?",
            (row_id, tid),
        ).fetchone()
        if not current:
            return False
        if float(current["published_at"] or 0) != float(expected_published_at or 0):
            return False
        retro = db.jloads(current["retro_json"], {})
        state = retro.get(str(day)) or {}
        if state.get("state") not in ("pending", None):
            return False
        state["state"] = "notified"
        state.pop("retry_after", None)
        state.pop("retry_attempts", None)
        state.pop("last_error", None)
        retro[str(day)] = state
        changed = connection.execute(
            "UPDATE publish_log SET retro_json=?,updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (json.dumps(retro, ensure_ascii=False), time.time(), row_id, tid),
        )
        return changed.rowcount == 1


def recover_interrupted() -> int:
    """在通用计费恢复前，把自动复盘 processing 锚点和退款一起复原。"""
    recovered = 0
    for row in db.q("SELECT id,tenant_id,retro_json FROM publish_log"):
        retro = db.jloads(row.get("retro_json"), {})
        for day, state in list(retro.items()):
            if not isinstance(state, dict) or state.get("state") != "processing":
                continue
            op_key = state.get("op_key") or ""

            def restore(connection, *, expected=op_key, day_key=str(day), item=row):
                current = connection.execute(
                    "SELECT retro_json FROM publish_log WHERE id=? AND tenant_id=?",
                    (item["id"], item["tenant_id"]),
                ).fetchone()
                if not current:
                    return False
                payload = db.jloads(current["retro_json"], {})
                current_state = payload.get(day_key) or {}
                if (current_state.get("state") != "processing"
                        or current_state.get("op_key") != expected):
                    return False
                current_state["state"] = "pending"
                current_state.pop("op_key", None)
                current_state.pop("started_at", None)
                attempts = max(
                    0, int(current_state.get("retry_attempts") or 0)
                ) + 1
                current_state.update({
                    "retry_attempts": attempts,
                    "retry_after": time.time() + 15 * 60,
                    "last_error": "服务重启中断，稍后自动重试",
                })
                payload[day_key] = current_state
                changed = connection.execute(
                    "UPDATE publish_log SET retro_json=?,updated_at=? "
                    "WHERE id=? AND tenant_id=?",
                    (
                        json.dumps(payload, ensure_ascii=False),
                        time.time(),
                        item["id"],
                        item["tenant_id"],
                    ),
                )
                return changed.rowcount == 1

            operation = db.one(
                "SELECT status FROM billing_operation WHERE op_key=?", (op_key,)
            ) if op_key else None
            if operation and operation["status"] == "charged":
                if billing.fail_operation_if_claimed(
                        op_key, "服务重启中断自动复盘", restore):
                    recovered += 1
            else:
                with db.atomic() as connection:
                    if restore(connection):
                        recovered += 1
    return recovered


def auto_enabled(tid: int) -> bool:
    """租户级自动复盘总开关;默认开启,关掉后既不扣点跑复盘也不发到期提醒。"""
    return not db.get_setting(f"retro_auto_off:{tid}")


def set_auto_enabled(tid: int, enabled: bool):
    db.set_setting(f"retro_auto_off:{tid}", None if enabled else "1")


async def tick():
    """调度器定期调用:处理到期的 T+1/3/7."""
    hour = datetime.now(TZ).hour
    if not (9 <= hour <= 22):     # 别半夜打扰老板
        return
    now = time.time()
    paused_tenants: dict = {}
    rows = await db.aq(
        "SELECT * FROM publish_log WHERE created_at > ?",
        (now - 40 * 86400,),
    )
    for row in rows:
        tenant = row["tenant_id"]
        if tenant not in paused_tenants:
            paused_tenants[tenant] = not await db.arun(auto_enabled, tenant)
        if paused_tenants[tenant]:
            continue
        retro = db.jloads(row["retro_json"], {})
        changed = False
        for d in RETRO_DAYS:
            st = retro.get(str(d)) or {}
            if (
                st.get("state") == "pending"
                and st.get("due", 1e18) <= now
                and st.get("retry_after", 0) <= now
            ):
                tid = row["tenant_id"]
                outcome = await _auto_retro_result(tid, row, d)
                if (
                    outcome == RETRO_NO_DATA
                    and await db.arun(
                        _mark_notified_if_pending,
                        tid,
                        row["id"],
                        d,
                        row.get("published_at"),
                    )
                ):
                    try:
                        await asyncio.to_thread(
                            notify.push,
                            tid,
                            "retro_due",
                            {
                                "title": row["title"],
                                "platform": row["platform"],
                                "day": f"T+{d}",
                            },
                        )
                    except Exception as exc:
                        log.info(
                            "复盘到期通知失败 tid=%s error_type=%s",
                            tid,
                            type(exc).__name__,
                        )
