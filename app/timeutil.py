"""北京时间工具:老板们按北京时间过日子,"今天/每日额度/日期显示"都以此为准。

服务器进程可能跑在 UTC(容器、云主机默认),直接用 time.localtime 或
``time.time() // 86400`` 会在北京时间早上 8 点才"换日"。这里统一提供按
Asia/Shanghai 计算的当前时间、日期字符串与日切时间戳。

中国自 1991 年起不再实行夏令时,固定 UTC+8 与 tzdata 的 Asia/Shanghai 在
此后任何时刻都一致;用固定偏移可避免精简系统缺 tzdata 时启动失败。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

CN_OFFSET_SECONDS = 8 * 3600
CN_TZ = timezone(timedelta(seconds=CN_OFFSET_SECONDS), "Asia/Shanghai")
_DAY = 86400


def _ts(ts: float | None) -> float:
    return time.time() if ts is None else float(ts)


def now_cn(ts: float | None = None) -> datetime:
    """当前(或给定时间戳)的北京时间,带时区信息。"""
    return datetime.fromtimestamp(_ts(ts), CN_TZ)


def today_cn(ts: float | None = None) -> str:
    """北京时间的日期字符串 YYYY-MM-DD。"""
    return now_cn(ts).strftime("%Y-%m-%d")


def cn_day_index(ts: float | None = None) -> int:
    """北京时间的"第几天"整数序号,用作按日计数的键(同一天内恒定)。"""
    return int((_ts(ts) + CN_OFFSET_SECONDS) // _DAY)


def cn_day_start_ts(ts: float | None = None) -> float:
    """给定时刻所在北京时间自然日 00:00 的 Unix 时间戳。"""
    return float(cn_day_index(ts) * _DAY - CN_OFFSET_SECONDS)


def format_cn(ts: float | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """把 Unix 时间戳格式化成北京时间文本;空值按 0 处理,与旧写法兼容。"""
    return now_cn(float(ts or 0)).strftime(fmt)


def next_cn_clock_ts(hour: int, minute: int = 0, ts: float | None = None) -> float:
    """下一次北京时间 hour:minute 的时间戳(严格晚于给定时刻)。"""
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise ValueError("hour/minute out of range")
    current = _ts(ts)
    target = cn_day_start_ts(current) + int(hour) * 3600 + int(minute) * 60
    if target <= current:
        target += _DAY
    return target
