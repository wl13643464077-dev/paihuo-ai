"""可观测性:请求指标、业务计数器、深度健康检查与权限边界。

审核报告把可观测性列为全项目最弱项(2.5/10)。这组测试钉住新增的观测层:
- 指标聚合语义正确(计数/错误分类/直方图/滚动窗口);
- 中间件按路由模板聚合,不因路径参数爆炸;
- 深检端点逐组件报告且仅平台管理员可见;
- /healthz 保持浅探活契约不变(毫秒级,不做深检)。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app import obs


class MetricsAggregationTests(unittest.TestCase):
    def setUp(self):
        obs.reset_for_tests()
        self.addCleanup(obs.reset_for_tests)

    def test_counts_errors_and_latency(self):
        obs.observe_request("/api/jobs", 200, 40.0)
        obs.observe_request("/api/jobs", 200, 60.0)
        obs.observe_request("/api/jobs", 502, 700.0)
        obs.observe_request("/api/jobs", 404, 5.0)

        snap = obs.snapshot()
        route = next(r for r in snap["routes"] if r["route"] == "/api/jobs")
        self.assertEqual(4, route["requests"])
        self.assertEqual(1, route["errors_5xx"])
        self.assertEqual(1, route["errors_4xx"])
        self.assertAlmostEqual((40 + 60 + 700 + 5) / 4, route["avg_ms"], places=3)
        self.assertEqual(700.0, route["max_ms"])

    def test_histogram_buckets(self):
        for ms in (10, 90, 400, 70000):
            obs.observe_request("/x", 200, ms)
        snap = obs.snapshot()
        hist = next(r for r in snap["routes"] if r["route"] == "/x")["hist_le_ms"]
        self.assertEqual(1, hist["50"])       # 10ms
        self.assertEqual(1, hist["100"])      # 90ms
        self.assertEqual(1, hist["500"])      # 400ms
        self.assertEqual(1, hist["inf"])      # 70s

    def test_recent_window_tracks_error_rate(self):
        now = time.time()
        for _ in range(8):
            obs.observe_request("/y", 200, 10.0)
        for _ in range(2):
            obs.observe_request("/y", 500, 10.0)
        recent = obs.recent_window(5, now=now)
        self.assertEqual(10, recent["requests"])
        self.assertEqual(2, recent["errors_5xx"])
        self.assertAlmostEqual(0.2, recent["error_rate"], places=3)

    def test_old_minutes_age_out_of_window(self):
        base = 1_800_000_000.0
        with patch.object(time, "time", return_value=base):
            obs.observe_request("/z", 500, 10.0)
        # 70 分钟后:一小时环已翻篇,旧错误不再计入任何窗口
        recent = obs.recent_window(60, now=base + 70 * 60)
        self.assertEqual(0, recent["requests"])

    def test_route_cardinality_is_bounded(self):
        for index in range(obs._MAX_ROUTES + 50):
            obs.observe_request(f"/spam/{index}", 200, 1.0)
        snap = obs.snapshot(top=1000)
        self.assertLessEqual(len(snap["routes"]), obs._MAX_ROUTES + 1)
        overflow = next(
            (r for r in snap["routes"] if r["route"] == "_overflow"), None)
        self.assertIsNotNone(overflow, "超限路由应归入 _overflow 而不是丢失")

    def test_counters_and_gauges(self):
        obs.count("provider_retry")
        obs.count("provider_retry")
        obs.register_gauge("queue_depth", lambda: 7)
        obs.register_gauge("broken", lambda: 1 / 0)
        snap = obs.snapshot()
        self.assertEqual(2, snap["counters"]["provider_retry"])
        self.assertEqual(7, snap["gauges"]["queue_depth"])
        self.assertIsNone(snap["gauges"]["broken"], "坏量规应呈现 None 而非抛错")


class BusinessCounterWiringTests(unittest.TestCase):
    """关键业务事件必须真的在计数(源码级断言,防止重构时静默丢埋点)。"""

    def _source(self, name):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app", name,
        )
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_engine_counts_job_failures(self):
        self.assertIn('obs.count("job_settle_failure")', self._source("engine.py"))

    def test_providers_count_retries_and_exhaustion(self):
        source = self._source("providers.py")
        self.assertIn('obs.count("provider_retry")', source)
        self.assertIn('obs.count("provider_exhausted")', source)

    def test_censor_counts_blocks(self):
        self.assertIn('obs.count("censor_block")', self._source("censor.py"))


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from app import auth, db, main

        cls.main = main
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(cls.tmp.name, "obs.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台"})
        db.insert("tenants", {"id": 2, "name": "租户甲"})
        cls.member_uid = db.insert("users", {
            "tenant_id": 2, "username": "obs-owner",
            "password_hash": "x", "role": "owner",
        })
        cls.root_uid = db.insert("users", {
            "tenant_id": 1, "username": "obs-root",
            "password_hash": "x", "role": "root",
        })
        cls.client = TestClient(main.app)
        cls.auth = auth
        cls.db = db

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        if cls.db._conn is not None:
            cls.db._conn.close()
        cls.db._conn = None
        cls.db.DB_PATH = cls.old_path
        cls.tmp.cleanup()

    def setUp(self):
        obs.reset_for_tests()
        self.addCleanup(obs.reset_for_tests)

    def _login(self, uid):
        self.client.cookies.set("cc_sess", self.auth.make_session(uid))

    def test_healthz_stays_shallow_and_anonymous(self):
        self.client.cookies.clear()
        response = self.client.get("/healthz")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())

    def test_middleware_records_by_route_template(self):
        self.client.cookies.clear()
        self.client.get("/healthz")
        self.client.get("/healthz")
        snap = obs.snapshot()
        route = next(
            (r for r in snap["routes"] if r["route"] == "/healthz"), None)
        self.assertIsNotNone(route, "中间件未按模板记录请求")
        self.assertGreaterEqual(route["requests"], 2)

    def test_unmatched_paths_are_bucketed_not_exploded(self):
        self.client.cookies.clear()
        for index in range(5):
            self.client.get(f"/no-such-prefix-{index}/x")
        snap = obs.snapshot(top=1000)
        exploded = [
            r["route"] for r in snap["routes"]
            if r["route"].startswith("/no-such-prefix")
        ]
        self.assertEqual([], exploded, f"未匹配路径按实路径爆炸: {exploded}")

    def test_metrics_endpoint_requires_root(self):
        self._login(self.member_uid)
        self.assertEqual(403, self.client.get("/api/ops/metrics").status_code)
        self._login(self.root_uid)
        response = self.client.get("/api/ops/metrics")
        self.assertEqual(200, response.status_code)
        body = response.json()
        for key in ("uptime_seconds", "recent_5m", "routes",
                    "counters", "gauges"):
            self.assertIn(key, body)

    def test_deep_health_requires_root_and_reports_components(self):
        self._login(self.member_uid)
        self.assertEqual(403, self.client.get("/api/ops/health").status_code)

        self._login(self.root_uid)
        response = self.client.get("/api/ops/health")
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertIn(body["status"], ("ok", "degraded", "down"))
        for component in ("database", "engine", "disk", "traffic", "providers"):
            self.assertIn(component, body["checks"])
        self.assertEqual("ok", body["checks"]["database"]["status"])
        self.assertIn("roundtrip_ms", body["checks"]["database"])

    def test_deep_health_reports_database_down(self):
        self._login(self.root_uid)

        async def boom(*args, **kwargs):
            raise RuntimeError("db exploded")

        with patch.object(self.main.db, "aget_setting", boom):
            body = self.client.get("/api/ops/health").json()
        self.assertEqual("down", body["status"])
        self.assertEqual("down", body["checks"]["database"]["status"])
        # 深检响应不得回显异常文本,只给类型。
        self.assertNotIn("exploded", str(body))

    def test_traffic_check_degrades_on_error_spike(self):
        for _ in range(30):
            obs.observe_request("/api/x", 500, 5.0)
        self._login(self.root_uid)
        body = self.client.get("/api/ops/health").json()
        self.assertIn(body["checks"]["traffic"]["status"], ("degraded", "down"))


if __name__ == "__main__":
    unittest.main()
