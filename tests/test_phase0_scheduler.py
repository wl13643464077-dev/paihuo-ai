"""Phase0：定时任务不再因「等老板拍板」的工单静默断更。"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app import db, scheduler


class _EngineProbe:
    def __init__(self):
        self.notified = []

    def notify(self, job_id):
        self.notified.append(job_id)

    def touch(self, job_id, tenant_id=None):
        pass


class SchedulerBacklogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "phase0-scheduler.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 500})
        self.sid = db.insert("schedule", {
            "tenant_id": 2,
            "name": "日更",
            "brief_json": json.dumps({"direction": "新品"}),
            "mode": "copilot",
            "kind": "daily",
            "enabled": 1,
            "next_run_at": 100,
        })
        self.engine = _EngineProbe()

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _jobs(self, status, n):
        for _ in range(n):
            db.insert("job", {
                "tenant_id": 2,
                "brief_json": "{}",
                "mode": "copilot",
                "status": status,
                "billing_status": "charged",
                "billing_points": 18,
            })

    def _schedule(self):
        return db.one("SELECT * FROM schedule WHERE id=?", (self.sid,))

    def _make_due(self):
        # 每次换一个到点时刻：同一 occurrence 会复用已开的工单。
        self.due = getattr(self, "due", 100) + 1
        db.execute(
            "UPDATE schedule SET next_run_at=?,claim_token=NULL,claim_until=NULL "
            "WHERE id=?",
            (self.due, self.sid),
        )

    def _notices(self):
        return db.q("SELECT title,body,link FROM notification WHERE tenant_id=2")

    def test_jobs_waiting_for_boss_do_not_consume_parallel_slots(self):
        self._jobs("awaiting_review", 5)
        self._jobs("gate_blocked", 1)
        self._jobs("running", 2)
        job_id = scheduler.fire(self._schedule(), self.engine)
        self.assertEqual([job_id], self.engine.notified)

    def test_parallel_full_is_dedicated_exception(self):
        self._jobs("running", 3)
        with self.assertRaises(scheduler.ScheduleBlocked) as caught:
            scheduler.fire(self._schedule(), self.engine)
        self.assertEqual("parallel_full", caught.exception.reason)
        # 兼容「立即执行」接口：仍可按 ValueError 映射成 429。
        self.assertIsInstance(caught.exception, ValueError)

    def test_review_backlog_pauses_once_and_resumes_automatically(self):
        self._jobs("awaiting_review", scheduler.REVIEW_BACKLOG_LIMIT)
        with self.assertRaises(scheduler.ScheduleBlocked) as caught:
            scheduler.fire(self._schedule(), self.engine)
        self.assertEqual("review_backlog", caught.exception.reason)

        scheduler._tick(self.engine)
        row = self._schedule()
        self.assertTrue(row["last_note"].startswith("暂停开工:"))
        self.assertIn(f"{scheduler.REVIEW_BACKLOG_LIMIT} 单等你拍板", row["last_note"])
        self.assertEqual(1, row["enabled"])
        self.assertEqual(0, int(row["fail_streak"] or 0))
        self.assertGreater(row["next_run_at"], 100)
        notices = self._notices()
        self.assertEqual(1, len(notices))
        self.assertEqual("定时任务先暂停了", notices[0]["title"])
        self.assertIn("等你拍板", notices[0]["body"])
        self.assertEqual("#/schedules", notices[0]["link"])

        # 老板几天不拍板：每 10 分钟重试，但只提醒一次。
        for _ in range(3):
            self._make_due()
            scheduler._tick(self.engine)
        self.assertEqual(1, len(self._notices()))
        self.assertEqual(0, int(self._schedule()["fail_streak"] or 0))

        # 老板拍板后自动恢复开工，提醒标记清零。
        db.execute("UPDATE job SET status='done' WHERE status='awaiting_review'")
        self._make_due()
        scheduler._tick(self.engine)
        self.assertTrue(self._schedule()["last_note"].startswith("已按时开工"))
        self.assertIsNone(db.get_setting(scheduler._block_key(self.sid)))

        # 新一段积压会重新提醒一次。
        self._jobs("awaiting_review", scheduler.REVIEW_BACKLOG_LIMIT)
        self._make_due()
        scheduler._tick(self.engine)
        self.assertEqual(2, len(self._notices()))

    def test_parallel_full_notifies_only_after_long_block(self):
        self._jobs("running", 3)
        with patch.object(scheduler.time, "time", return_value=1000.0):
            scheduler._tick(self.engine)
        self.assertEqual([], self._notices())
        self.assertIn("并行已满", self._schedule()["last_note"])

        self._make_due()
        with patch.object(
            scheduler.time, "time",
            return_value=1000.0 + scheduler.PARALLEL_FULL_NOTIFY_AFTER,
        ):
            scheduler._tick(self.engine)
        self.assertEqual(1, len(self._notices()))

        self._make_due()
        with patch.object(
            scheduler.time, "time",
            return_value=1000.0 + 2 * scheduler.PARALLEL_FULL_NOTIFY_AFTER,
        ):
            scheduler._tick(self.engine)
        self.assertEqual(1, len(self._notices()))
        self.assertEqual(0, int(self._schedule()["fail_streak"] or 0))

    def test_other_value_errors_count_as_real_failures(self):
        with patch.object(scheduler, "fire", side_effect=ValueError("坏数据")), \
                patch("app.notify.push") as push:
            for _ in range(3):
                self._make_due()
                scheduler._tick(self.engine)
        row = self._schedule()
        self.assertEqual(3, row["fail_streak"])
        self.assertIn("开工出错", row["last_note"])
        self.assertEqual(1, push.call_count)
        self.assertEqual("schedule_failed", push.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
