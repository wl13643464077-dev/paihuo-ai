"""运行期护栏:北京时间日切、单进程实例锁、后台循环心跳、数据保留期。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from app import db, instancelock, obs, retention, timeutil

ROOT = Path(__file__).resolve().parents[1]


def _utc_ts(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class BeijingTimeTests(unittest.TestCase):
    def test_day_rolls_over_at_beijing_midnight_not_utc_midnight(self):
        # UTC 15:59:59 = 北京 23:59:59;UTC 16:00:00 = 北京次日 00:00:00
        before = _utc_ts(2026, 3, 1, 15, 59, 59)
        after = _utc_ts(2026, 3, 1, 16, 0, 0)
        self.assertEqual("2026-03-01", timeutil.today_cn(before))
        self.assertEqual("2026-03-02", timeutil.today_cn(after))
        self.assertEqual(timeutil.cn_day_index(before) + 1,
                         timeutil.cn_day_index(after))
        # UTC 日序号在北京时间早 8 点才变,这正是要修的问题
        morning_cn = _utc_ts(2026, 3, 1, 23, 30)   # 北京 3/2 07:30
        self.assertEqual(timeutil.cn_day_index(after),
                         timeutil.cn_day_index(morning_cn))
        self.assertNotEqual(int(after // 86400), int(morning_cn // 86400) + 1)

    def test_day_start_and_next_clock(self):
        ts = _utc_ts(2026, 3, 1, 23, 30)            # 北京 3/2 07:30
        start = timeutil.cn_day_start_ts(ts)
        self.assertEqual(_utc_ts(2026, 3, 1, 16, 0), start)
        self.assertEqual("2026-03-02 00:00", timeutil.format_cn(start))
        self.assertEqual("2026-03-02 07:30", timeutil.format_cn(ts))
        self.assertEqual(8 * 3600, timeutil.now_cn(ts).utcoffset().total_seconds())
        # 07:30 之后的下一个 04:17 是次日;之前的下一个 09:00 是当天
        self.assertEqual(start + 86400 + 4 * 3600 + 17 * 60,
                         timeutil.next_cn_clock_ts(4, 17, ts))
        self.assertEqual(start + 9 * 3600, timeutil.next_cn_clock_ts(9, 0, ts))
        # 恰好到点时取下一天,避免同一秒内重复执行
        self.assertEqual(start + 86400, timeutil.next_cn_clock_ts(0, 0, start))
        with self.assertRaises(ValueError):
            timeutil.next_cn_clock_ts(24, 0, ts)

    def test_format_is_independent_of_process_timezone(self):
        ts = _utc_ts(2026, 7, 1, 18, 5)
        old = os.environ.get("TZ")
        try:
            for zone in ("UTC", "America/New_York"):
                os.environ["TZ"] = zone
                time.tzset()
                self.assertEqual("2026-07-02 02:05", timeutil.format_cn(ts))
                self.assertEqual("2026-07-02", timeutil.today_cn(ts))
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.assertEqual("1970-01-01 08:00", timeutil.format_cn(None))

    def test_feishu_records_use_beijing_time(self):
        from app import feishu
        ts = _utc_ts(2026, 7, 1, 18, 5)
        know = feishu._know_record({"id": 1, "created_at": ts, "meta": {}})
        asset = feishu._asset_record({"id": 2, "created_at": ts, "meta": {},
                                      "payload": {}})
        self.assertEqual("2026-07-02 02:05", know["创建时间"])
        self.assertEqual("2026-07-02 02:05", asset["创建时间"])


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "contentcrew.db")
        Path(self.db_path).touch()

    def tearDown(self):
        instancelock.release_all_for_tests()
        self.tmp.cleanup()

    def _other_process(self, db_path: str) -> subprocess.CompletedProcess:
        code = textwrap.dedent(f"""
            import sys
            from app import instancelock
            try:
                instancelock.acquire({db_path!r})
            except instancelock.InstanceLockError as exc:
                print(exc)
                sys.exit(7)
            print("acquired")
        """)
        return subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True,
            text=True, timeout=30,
        )

    def test_second_process_is_refused_while_first_holds_lock(self):
        path = instancelock.acquire(self.db_path)
        self.assertEqual(self.db_path + ".instance.lock", path)
        self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
        self.assertEqual(str(os.getpid()), Path(path).read_text().strip())
        # 同一进程重复启动(测试里常见)是幂等的
        self.assertEqual(path, instancelock.acquire(self.db_path))

        result = self._other_process(self.db_path)
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertIn("只能运行 1 个 worker", result.stdout)
        self.assertIn(f"pid={os.getpid()}", result.stdout)

        # 符号链接别名指向同一个库,也必须命中同一把锁
        alias = os.path.join(self.tmp.name, "alias.db")
        os.symlink(self.db_path, alias)
        self.assertEqual(7, self._other_process(alias).returncode)

        instancelock.release_all_for_tests()
        result = self._other_process(self.db_path)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("acquired", result.stdout)

    def test_lock_released_when_holder_process_dies(self):
        result = self._other_process(self.db_path)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        # 子进程已退出,内核自动释放 flock,本进程可以拿到
        instancelock.acquire(self.db_path)

    def test_symlinked_lock_file_is_rejected(self):
        target = os.path.join(self.tmp.name, "elsewhere")
        Path(target).write_text("")
        os.symlink(target, self.db_path + ".instance.lock")
        with self.assertRaises(instancelock.InstanceLockError):
            instancelock.acquire(self.db_path)
        self.assertEqual([], instancelock.held_paths())

    def test_hardlinked_lock_file_is_rejected_and_loose_mode_is_tightened(self):
        lock = self.db_path + ".instance.lock"
        Path(lock).write_text("")
        os.link(lock, os.path.join(self.tmp.name, "second-name"))
        with self.assertRaises(instancelock.InstanceLockError):
            instancelock.acquire(self.db_path)
        os.unlink(os.path.join(self.tmp.name, "second-name"))
        os.chmod(lock, 0o644)
        instancelock.acquire(self.db_path)
        self.assertEqual(0o600, os.stat(lock).st_mode & 0o777)

    def test_memory_database_cannot_be_locked(self):
        with self.assertRaises(instancelock.InstanceLockError):
            instancelock.lock_path_for(":memory:")


class LoopHeartbeatTests(unittest.TestCase):
    def setUp(self):
        obs.reset_for_tests()

    def tearDown(self):
        obs.reset_for_tests()

    def test_stale_never_and_fresh_loops(self):
        obs.register_loop("retention", 60)
        obs.register_loop("idle", 60)
        self.assertEqual("never", obs.loop_status(now=1000)["loops"]["retention"]["status"])
        obs.beat("retention", now=1000)
        status = obs.loop_status(now=1030)
        self.assertEqual("ok", status["loops"]["retention"]["status"])
        self.assertEqual(30.0, status["loops"]["retention"]["seconds_since_beat"])
        self.assertFalse(status["ok"])       # idle 从未心跳
        obs.beat("idle", now=1030)
        self.assertTrue(obs.loop_status(now=1030)["ok"])
        stale = obs.loop_status(now=1100)
        self.assertEqual("stale", stale["loops"]["retention"]["status"])
        self.assertFalse(stale["ok"])

    def test_watched_task_reports_stopped_after_crash(self):
        async def scenario():
            async def crashes():
                raise RuntimeError("boom")

            async def runs_forever():
                await asyncio.sleep(3600)

            crashed = asyncio.create_task(crashes())
            alive = asyncio.create_task(runs_forever())
            obs.watch_task("scheduler", alive)
            obs.watch_task("analyzer", crashed)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # 被外部观察的任务不需要心跳,活着就算正常(即使很久以后)
            status = obs.loop_status(now=time.time() + 86400)
            self.assertIsInstance(crashed.exception(), RuntimeError)
            alive.cancel()
            try:
                await alive
            except asyncio.CancelledError:
                pass
            return status

        status = asyncio.run(scenario())
        self.assertEqual("ok", status["loops"]["scheduler"]["status"])
        self.assertEqual("stopped", status["loops"]["analyzer"]["status"])
        self.assertFalse(status["ok"])

    def test_deep_health_only_for_loopback_or_valid_token(self):
        token = "t" * 32
        self.assertTrue(obs.deep_health_allowed("127.0.0.1"))
        self.assertTrue(obs.deep_health_allowed("::1"))
        self.assertFalse(obs.deep_health_allowed("203.0.113.9"))
        self.assertFalse(obs.deep_health_allowed("?"))
        self.assertTrue(obs.deep_health_allowed("203.0.113.9", token, token))
        self.assertFalse(obs.deep_health_allowed("203.0.113.9", "wrong", token))
        # 空令牌或过短的令牌配置不生效,不能被空字符串"匹配"
        self.assertFalse(obs.deep_health_allowed("203.0.113.9", "", ""))
        self.assertFalse(obs.deep_health_allowed("203.0.113.9", "short", "short"))


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._close_all_connections()
        db._conn = db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "retention.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "租户甲"})
        self.now = time.time()

    def tearDown(self):
        db._close_all_connections()
        db._conn = db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _days_ago(self, days: float) -> float:
        return self.now - days * 86400

    def _seed(self):
        for age in (10, 179, 181, 400):
            db.insert("censor_log", {"tenant_id": 2, "title": f"censor-{age}",
                                     "created_at": self._days_ago(age)})
        # 定向通知:已读久远 / 已读近期 / 未读久远
        db.insert("notification", {"tenant_id": 2, "kind": "job_done",
                                   "title": "read-old", "user_id": 20,
                                   "read_at": self._days_ago(200),
                                   "created_at": self._days_ago(210)})
        db.insert("notification", {"tenant_id": 2, "kind": "job_done",
                                   "title": "read-recent", "user_id": 20,
                                   "read_at": self._days_ago(5),
                                   "created_at": self._days_ago(300)})
        db.insert("notification", {"tenant_id": 2, "kind": "job_done",
                                   "title": "unread-old", "user_id": 20,
                                   "created_at": self._days_ago(500)})
        # 广播通知:read_at 永远为空,按创建时间 365 天兜底
        db.insert("notification", {"tenant_id": 2, "kind": "daily_digest",
                                   "title": "broadcast-old",
                                   "created_at": self._days_ago(400)})
        db.insert("notification", {"tenant_id": 2, "kind": "daily_digest",
                                   "title": "broadcast-recent",
                                   "created_at": self._days_ago(100)})
        for age in (1, 29, 31, 90):
            db.insert("client_error", {"tenant_id": 2, "kind": "error",
                                       "message": f"err-{age}",
                                       "created_at": self._days_ago(age)})
        # 台账/审计类:再老也不能被清
        db.insert("publish_log", {"tenant_id": 2, "title": "ancient-publish",
                                  "created_at": self._days_ago(2000)})
        db.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,kind,created_at) "
            "VALUES(2,1,'created',?)", (self._days_ago(2000),))

    def _titles(self, table, column="title"):
        return sorted(r[column] for r in db.q(f"SELECT {column} FROM {table}"))

    def test_run_once_deletes_only_expired_rows_in_small_batches(self):
        self._seed()
        # 批大小 1,验证分批推进能扫完而不是只删一行
        report = retention.run_once(self.now, batch_size=1)
        self.assertEqual({"censor_log": 2, "notification_read": 1,
                          "notification_broadcast": 1, "client_error": 2},
                         report["deleted"])
        self.assertEqual(["censor-10", "censor-179"], self._titles("censor_log"))
        self.assertEqual(["broadcast-recent", "read-recent", "unread-old"],
                         self._titles("notification"))
        self.assertEqual(["err-1", "err-29"], self._titles("client_error", "message"))
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM publish_log")["n"])
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM inspection_event")["n"])
        self.assertEqual(0, report["wal_busy"])
        wal = Path(db.DB_PATH + "-wal")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)
        # 再跑一遍是空操作
        again = retention.run_once(self.now)
        self.assertEqual(0, sum(again["deleted"].values()))

    def test_env_override_changes_days_and_zero_disables_rule(self):
        self._seed()
        with mock.patch.dict(os.environ, {
            "CONTENTCREW_RETENTION_CENSOR_LOG_DAYS": "0",
            "CONTENTCREW_RETENTION_CLIENT_ERROR_DAYS": "60",
            "CONTENTCREW_RETENTION_NOTIFICATION_READ_DAYS": "bogus",
        }), self.assertLogs("retention", level="WARNING") as logs:
            report = retention.run_once(self.now)
        self.assertIn("notification_read", "\n".join(logs.output))
        self.assertNotIn("censor_log", report["deleted"])
        self.assertEqual(4, db.one("SELECT COUNT(*) n FROM censor_log")["n"])
        self.assertEqual(["err-1", "err-29", "err-31"],
                         self._titles("client_error", "message"))
        self.assertEqual(1, report["deleted"]["notification_read"])

    def test_async_run_matches_sync_and_beats(self):
        self._seed()
        obs.reset_for_tests()
        try:
            report = asyncio.run(retention.run_once_async(
                self.now, batch_size=2, pause=0))
            self.assertEqual(6, sum(report["deleted"].values()))
            self.assertEqual("ok", obs.loop_status()["loops"]["retention"]["status"])
        finally:
            obs.reset_for_tests()

    def test_loop_survives_sweep_failure_and_keeps_beating(self):
        calls = []

        async def failing_sweep(*_args, **_kwargs):
            calls.append(time.time())
            if len(calls) >= 2:
                raise asyncio.CancelledError
            raise sqlite3_error()

        def sqlite3_error():
            import sqlite3
            return sqlite3.OperationalError("database is locked")

        obs.reset_for_tests()
        try:
            with mock.patch.object(retention, "run_once_async", failing_sweep), \
                    mock.patch.object(retention.timeutil, "next_cn_clock_ts",
                                      side_effect=lambda *a, **k: time.time() - 1):
                with self.assertLogs("retention", level="ERROR") as logs, \
                        self.assertRaises(asyncio.CancelledError):
                    asyncio.run(retention.loop(beat_every=0.01))
            self.assertEqual(2, len(calls))
            self.assertIn("OperationalError", "\n".join(logs.output))
            self.assertEqual("ok", obs.loop_status()["loops"]["retention"]["status"])
        finally:
            obs.reset_for_tests()


if __name__ == "__main__":
    unittest.main()
