"""进程内可观测性:请求延迟/错误率、业务计数器与组件量规。

审核报告把可观测性列为全项目最弱项(2.5/10):除散落日志外,没有任何指标——
错误率涨了、某条路由变慢了、引擎队列堆积了,都要等老板投诉才知道。

设计取向:
- 纯进程内、纯标准库。单机 SQLite 架构下先要"看得见",不引入外部依赖;
  数据结构与 Prometheus 语义兼容(counter/gauge/histogram),将来要接采集器
  只需加一个文本导出端点。
- 双窗口:累计(进程启动以来) + 最近窗口(60 个 1 分钟桶的环)。运维看的是
  "最近 5 分钟错误率",不是历史平均。
- 锁保护:请求记录来自 uvicorn 线程池与事件循环两侧,聚合必须线程安全;
  每次记录只做 O(1) 加法,不在热路径上分配大对象。
- 绝不记请求参数/正文/租户业务数据——只有路由模板、状态码分类与耗时。
"""
from __future__ import annotations

import threading
import time

# 延迟直方图桶(毫秒);最后一桶是 +Inf。选点覆盖"快接口/慢接口/LLM 长请求"。
LATENCY_BUCKETS_MS = (50, 100, 250, 500, 1000, 2500, 5000, 15000, 60000)
WINDOW_SLOTS = 60          # 1 分钟一桶,滚动一小时
_MAX_ROUTES = 200          # 路由模板聚合上限,防路径爆炸打爆内存
_MAX_COUNTERS = 300

_lock = threading.Lock()
_started_at = time.time()

# 路由聚合: route -> {n, err5, err4, ms_sum, ms_max, hist[list]}
_routes: dict = {}
# 分钟环: slot -> {minute, n, err5, ms_sum}
_ring: list = [{"minute": -1, "n": 0, "err5": 0, "ms_sum": 0.0}
               for _ in range(WINDOW_SLOTS)]
# 业务计数器: name -> int
_counters: dict = {}
# 量规回调: name -> fn() -> number(读取时惰性求值,注册方无需推数据)
_gauges: dict = {}


def _hist_index(ms: float) -> int:
    for index, bound in enumerate(LATENCY_BUCKETS_MS):
        if ms <= bound:
            return index
    return len(LATENCY_BUCKETS_MS)


def observe_request(route: str, status: int, elapsed_ms: float) -> None:
    """记录一次 HTTP 请求。route 必须是路由模板(如 /api/jobs/{job_id}),不是实路径。"""
    route = str(route or "unknown")[:120]
    now_minute = int(time.time() // 60)
    with _lock:
        agg = _routes.get(route)
        if agg is None:
            if len(_routes) >= _MAX_ROUTES:
                route = "_overflow"
                agg = _routes.get(route)
            if agg is None:
                agg = {"n": 0, "err5": 0, "err4": 0, "ms_sum": 0.0,
                       "ms_max": 0.0,
                       "hist": [0] * (len(LATENCY_BUCKETS_MS) + 1)}
                _routes[route] = agg
        agg["n"] += 1
        agg["ms_sum"] += elapsed_ms
        if elapsed_ms > agg["ms_max"]:
            agg["ms_max"] = elapsed_ms
        agg["hist"][_hist_index(elapsed_ms)] += 1
        if status >= 500:
            agg["err5"] += 1
        elif status >= 400:
            agg["err4"] += 1

        slot = _ring[now_minute % WINDOW_SLOTS]
        if slot["minute"] != now_minute:
            slot["minute"] = now_minute
            slot["n"] = 0
            slot["err5"] = 0
            slot["ms_sum"] = 0.0
        slot["n"] += 1
        slot["ms_sum"] += elapsed_ms
        if status >= 500:
            slot["err5"] += 1


def count(name: str, delta: int = 1) -> None:
    """业务事件计数(工位失败、供应商重试、审查拦截等)。"""
    name = str(name)[:80]
    with _lock:
        if name not in _counters and len(_counters) >= _MAX_COUNTERS:
            name = "_overflow"
        _counters[name] = _counters.get(name, 0) + delta


def register_gauge(name: str, reader) -> None:
    """注册量规读取器(队列深度、订阅数等);读取时求值,异常按 None 呈现。"""
    with _lock:
        _gauges[str(name)[:80]] = reader


def recent_window(minutes: int = 5, now: float = None) -> dict:
    """最近 N 分钟的请求量、5xx 错误率与平均延迟(运维判断"现在有没有事")。"""
    minutes = max(1, min(int(minutes), WINDOW_SLOTS))
    now_minute = int((now if now is not None else time.time()) // 60)
    n = err5 = 0
    ms_sum = 0.0
    with _lock:
        for back in range(minutes):
            slot = _ring[(now_minute - back) % WINDOW_SLOTS]
            if slot["minute"] == now_minute - back:
                n += slot["n"]
                err5 += slot["err5"]
                ms_sum += slot["ms_sum"]
    return {
        "minutes": minutes,
        "requests": n,
        "errors_5xx": err5,
        "error_rate": (err5 / n) if n else 0.0,
        "avg_ms": (ms_sum / n) if n else 0.0,
    }


def snapshot(top: int = 30) -> dict:
    """完整指标快照(root 运维端点用)。"""
    with _lock:
        routes = []
        for route, agg in _routes.items():
            routes.append({
                "route": route,
                "requests": agg["n"],
                "errors_5xx": agg["err5"],
                "errors_4xx": agg["err4"],
                "avg_ms": (agg["ms_sum"] / agg["n"]) if agg["n"] else 0.0,
                "max_ms": agg["ms_max"],
                "hist_le_ms": dict(zip(
                    [str(b) for b in LATENCY_BUCKETS_MS] + ["inf"],
                    agg["hist"],
                )),
            })
        counters = dict(_counters)
        gauge_readers = dict(_gauges)
    routes.sort(key=lambda item: item["requests"], reverse=True)
    gauges = {}
    for name, reader in gauge_readers.items():
        try:
            gauges[name] = reader()
        except Exception:
            gauges[name] = None
    return {
        "uptime_seconds": time.time() - _started_at,
        "recent_5m": recent_window(5),
        "recent_60m": recent_window(60),
        "routes": routes[:top],
        "counters": counters,
        "gauges": gauges,
    }


def reset_for_tests() -> None:
    global _started_at
    with _lock:
        _routes.clear()
        _counters.clear()
        _gauges.clear()
        for slot in _ring:
            slot["minute"] = -1
            slot["n"] = 0
            slot["err5"] = 0
            slot["ms_sum"] = 0.0
        _started_at = time.time()
