"""数据保留期:定期清理只会越积越多、过期后没有业务价值的日志类数据。

哪些清、哪些不清(读表结构后的判断):
- censor_log 审查记录:老板在"审查记录"页回看,半年以前的报告基本不再看,默认 180 天。
- notification 已读通知:定向通知已读(read_at 有值)满 180 天清掉;全员广播通知的
  已读状态记在 read_by 里、read_at 永远为空,只能按创建时间兜底,满 365 天清掉。
  未读的定向通知不清。
- client_error 前端报错:只给运维排障用,默认 30 天(写入时另有每租户 1000 条上限)。

刻意**不清**的表:
- publish_log 发布台账:老板可见的发布历史,wechat_draft_delivery.publish_log_id
  引用它,还驱动发后 1/3/7 天复盘,删了会让投递记录悬空。
- inspection_event 巡店事件流:是巡店单的审计轨迹,随巡店单一起删除,不能单独过期。
- funnel_event 已由 funnel.py 自带 400 天保留;billing_log 等账务表属于审计,不碰。

执行方式:每天北京时间凌晨低峰跑一次,按主键分批删除(每批一个短事务,批间让出
写锁),全部完成后 ``PRAGMA wal_checkpoint(TRUNCATE)`` 把 WAL 收回。
每条规则的天数可用环境变量覆盖,例如 ``CONTENTCREW_RETENTION_CENSOR_LOG_DAYS=365``;
设为 0 表示停用该规则。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass

from . import db, obs, timeutil

log = logging.getLogger("retention")

LOOP_NAME = "retention"
RUN_HOUR_CN = 4            # 北京时间凌晨 4 点多,店铺业务最闲
RUN_MINUTE_CN = 17
BATCH_SIZE = 500
MAX_BATCHES_PER_RULE = 2000
BATCH_PAUSE_SECONDS = 0.05
_DAY = 86400


@dataclass(frozen=True)
class Rule:
    name: str          # 规则名,也用于环境变量覆盖
    table: str
    where: str         # 过期条件,唯一的参数是截止时间戳
    days: int
    label: str


DEFAULT_RULES = (
    Rule("censor_log", "censor_log", "created_at < ?", 180, "审查记录"),
    Rule("notification_read", "notification",
         "read_at IS NOT NULL AND read_at < ?", 180, "已读通知"),
    Rule("notification_broadcast", "notification",
         "user_id IS NULL AND created_at < ?", 365, "全员广播通知"),
    Rule("client_error", "client_error", "created_at < ?", 30, "前端报错记录"),
)


def rule_days(rule: Rule) -> int:
    """读取环境变量覆盖的保留天数;非法值回落到默认,负数按 0(停用)。"""
    raw = os.environ.get(f"CONTENTCREW_RETENTION_{rule.name.upper()}_DAYS")
    if raw is None or not raw.strip():
        return rule.days
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("invalid retention override rule=%s, using default", rule.name)
        return rule.days


def purge_batch(rule: Rule, cutoff: float, after_id: int,
                batch_size: int = BATCH_SIZE) -> tuple[int, int]:
    """删一批过期行。按主键向后推进,整轮只扫表一次。

    返回 (本批删除行数, 本批最大 id);最大 id 为 0 表示已经扫到尽头。
    """
    with db.atomic() as connection:
        rows = connection.execute(
            f"SELECT id FROM {rule.table} WHERE id > ? AND ({rule.where}) "
            "ORDER BY id LIMIT ?",
            (int(after_id), float(cutoff), int(batch_size)),
        ).fetchall()
        if not rows:
            return 0, 0
        ids = [int(row[0]) for row in rows]
        marks = ",".join("?" for _ in ids)
        # 删除时再核一次过期条件:两步之间行可能被改(如通知刚被标已读)。
        deleted = connection.execute(
            f"DELETE FROM {rule.table} WHERE id IN ({marks}) AND ({rule.where})",
            (*ids, float(cutoff)),
        ).rowcount
    return int(deleted), ids[-1]


def checkpoint() -> int:
    """把 WAL 合回主库并截断;返回 busy 标志(0=完全完成,-1=失败)。"""
    try:
        row = db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return int(row[0]) if row is not None else -1
    except (sqlite3.Error, TypeError, ValueError):
        return -1


def _plan(now: float | None, rules) -> list[tuple[Rule, float]]:
    current = time.time() if now is None else float(now)
    plan = []
    for rule in rules:
        days = rule_days(rule)
        if days <= 0:
            continue
        plan.append((rule, current - days * _DAY))
    return plan


def run_once(now: float | None = None, *, rules=DEFAULT_RULES,
             batch_size: int = BATCH_SIZE,
             max_batches: int = MAX_BATCHES_PER_RULE) -> dict:
    """同步执行一轮(测试与维护脚本用)。"""
    deleted: dict[str, int] = {}
    for rule, cutoff in _plan(now, rules):
        total, after = 0, 0
        for _ in range(max(1, int(max_batches))):
            count, after = purge_batch(rule, cutoff, after, batch_size)
            total += count
            if after == 0:
                break
        deleted[rule.name] = total
    return {"deleted": deleted, "wal_busy": checkpoint()}


async def run_once_async(now: float | None = None, *, rules=DEFAULT_RULES,
                         batch_size: int = BATCH_SIZE,
                         max_batches: int = MAX_BATCHES_PER_RULE,
                         pause: float = BATCH_PAUSE_SECONDS) -> dict:
    """异步执行一轮:每批单独进 db 线程池,批间让出,不长期占用写锁。"""
    deleted: dict[str, int] = {}
    for rule, cutoff in _plan(now, rules):
        total, after = 0, 0
        for _ in range(max(1, int(max_batches))):
            count, after = await db.arun(purge_batch, rule, cutoff, after, batch_size)
            total += count
            obs.beat(LOOP_NAME)
            if after == 0:
                break
            await asyncio.sleep(pause)
        deleted[rule.name] = total
    wal_busy = await db.arun(checkpoint)
    return {"deleted": deleted, "wal_busy": wal_busy}


async def loop(*, hour: int = RUN_HOUR_CN, minute: int = RUN_MINUTE_CN,
               beat_every: float = 600.0) -> None:
    """每天北京时间 hour:minute 跑一轮;任何异常都兜住,等下一轮/稍后重试。"""
    obs.register_loop(LOOP_NAME, beat_every * 3)
    log.info("retention loop started (daily at %02d:%02d Asia/Shanghai)", hour, minute)
    while True:
        obs.beat(LOOP_NAME)
        try:
            target = timeutil.next_cn_clock_ts(hour, minute)
            while True:
                remaining = target - time.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, beat_every))
                obs.beat(LOOP_NAME)
            started = time.time()
            report = await run_once_async()
            log.info(
                "retention sweep done deleted=%s wal_busy=%s seconds=%.1f",
                ",".join(f"{k}:{v}" for k, v in sorted(report["deleted"].items())),
                report["wal_busy"], time.time() - started,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("retention sweep failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(min(300.0, beat_every))
