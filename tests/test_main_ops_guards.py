"""main.py 侧的运维护栏:北京时间日切的免费额度、/healthz?deep=1。

依赖 fastapi;本地没装时整体跳过(CI 上会跑)。
"""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from app import obs

HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None


def _utc_ts(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


@unittest.skipUnless(HAS_FASTAPI, "fastapi is not installed")
class MainOpsGuardTests(unittest.TestCase):
    def setUp(self):
        from app import main
        self.main = main
        obs.reset_for_tests()
        self.addCleanup(obs.reset_for_tests)

    def test_expert_match_daily_limit_resets_at_beijing_midnight(self):
        main = self.main
        main._match_uses.clear()
        self.addCleanup(main._match_uses.clear)
        from app import timeutil
        clock = SimpleNamespace(now=_utc_ts(2026, 3, 1, 15, 0))   # 北京 23:00
        fake_time = SimpleNamespace(time=lambda: clock.now)
        with mock.patch.object(timeutil, "time", fake_time), \
                mock.patch.object(main, "_MATCH_DAILY", 2):
            self.assertEqual([False, False, True],
                             [main._match_over_limit(7) for _ in range(3)])
            clock.now = _utc_ts(2026, 3, 1, 16, 0, 1)           # 北京次日 00:00:01
            # UTC 还是同一天,但北京时间已换日,额度重新开始
            self.assertFalse(main._match_over_limit(7))

    def _request(self, host="127.0.0.1", headers=None):
        return SimpleNamespace(client=SimpleNamespace(host=host),
                               headers=headers or {})

    def test_healthz_default_stays_shallow(self):
        self.assertEqual({"status": "ok"}, self.main.healthz())
        self.assertEqual({"status": "ok"}, self.main.healthz(self._request()))

    def test_healthz_deep_requires_local_or_token_and_reports_loops(self):
        main = self.main
        forbidden = main.healthz(self._request("203.0.113.5"), deep="1")
        self.assertEqual(403, forbidden.status_code)
        obs.register_loop("retention", 60)
        obs.beat("retention")
        ok = main.healthz(self._request(), deep="1")
        self.assertEqual(200, ok.status_code)
        self.assertEqual("ok", json.loads(ok.body)["loops"]["retention"]["status"])
        obs.beat("retention", now=0)
        token = "x" * 32
        with mock.patch.dict(os.environ, {"CONTENTCREW_HEALTH_TOKEN": token}):
            stale = main.healthz(
                self._request("203.0.113.5", {"x-health-token": token}), deep="1")
        self.assertEqual(503, stale.status_code)
        body = json.loads(stale.body)
        self.assertEqual("degraded", body["status"])
        self.assertEqual("stale", body["loops"]["retention"]["status"])


if __name__ == "__main__":
    unittest.main()
