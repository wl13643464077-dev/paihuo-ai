"""Phase0：统一看门狗——只收口进程内确实没在跑、且长时间无动静的记录。"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import billing, db, employeeidentity, meeting, taskrunner, watchdog
from app.engine import Engine


HOUR = 3600


class WatchdogCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "phase0-watchdog.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 50})
        db.insert("users", {"tenant_id": 2, "username": "boss2", "password_hash": "x", "role": "owner", "enabled": 1})
        self.now = time.time()
        self.old = self.now - 5 * HOUR
        taskrunner.RUNNING.clear()
        meeting.ACTIVE.clear()

    def tearDown(self):
        taskrunner.RUNNING.clear()
        meeting.ACTIVE.clear()
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    # ---------- fixtures ----------
    def _task(self, *, status="running", updated_at=None, steps=None, **extra):
        employee = employeeidentity.any_employee(101)
        row = {
            "tenant_id": 2,
            "emp_idx": 101,
            "brief_json": json.dumps({"direction": "评估新店选址"}),
            "status": status,
            "billing_status": "charged",
            "billing_points": 3,
            "steps_json": json.dumps(steps or []),
            "updated_at": self.old if updated_at is None else updated_at,
            **employeeidentity.task_fields(employee),
        }
        row.update(extra)
        return db.insert("task", row)

    def _meeting(self, **extra):
        row = {
            "tenant_id": 2,
            "question": "周末要不要加开夜宵档？",
            "emp_idxs_json": "[0,1]",
            "status": "running",
            "phase": "validate",
            "billing_status": "charged",
            "billing_points": 4,
            "updated_at": self.old,
        }
        row.update(extra)
        return db.insert("meeting", row)

    def _job(self, *, status="running", updated_at=None, run_updated_at=None):
        job_id = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "新品上市推文"}),
            "mode": "copilot",
            "status": status,
            "billing_status": "charged",
            "billing_points": 18,
            "updated_at": self.old if updated_at is None else updated_at,
        })
        db.insert("station_run", {
            "job_id": job_id,
            "station_idx": 1,
            "skill_id": "x",
            "version": 1,
            "status": "running",
            "updated_at": self.old if run_updated_at is None else run_updated_at,
        })
        return job_id

    def _notices(self):
        return db.q("SELECT title,body,link FROM notification WHERE tenant_id=2")

    # ---------- tasks ----------
    async def test_stale_task_is_failed_refunded_and_boss_notified(self):
        task_id = self._task()
        queued_id = self._task(status="queued")
        result = await watchdog.sweep(None, now=self.now)
        self.assertEqual(2, result["task"])
        for tid in (task_id, queued_id):
            row = db.one("SELECT * FROM task WHERE id=?", (tid,))
            self.assertEqual("failed", row["status"])
            self.assertEqual("refunded", row["billing_status"])
            self.assertEqual(watchdog.TIMEOUT_MESSAGE, row["output_md"])
        self.assertEqual(56, billing.balance(2))
        notices = self._notices()
        self.assertEqual(2, len(notices))
        self.assertIn("超时没有完成，点数已自动退回", notices[0]["body"])
        # 幂等：再扫一遍不会重复退款/通知。
        await watchdog.sweep(None, now=self.now + HOUR)
        self.assertEqual(56, billing.balance(2))
        self.assertEqual(2, len(self._notices()))

    async def test_task_running_in_this_process_is_never_touched(self):
        task_id = self._task()
        taskrunner.RUNNING.add(task_id)
        result = await watchdog.sweep(None, now=self.now)
        self.assertEqual(0, result["task"])
        self.assertEqual("running", db.one(
            "SELECT status FROM task WHERE id=?", (task_id,))["status"])
        self.assertEqual(50, billing.balance(2))

    async def test_recent_step_or_update_keeps_long_task_alive(self):
        recent_step = self._task(steps=[{"k": "search", "l": "检索", "ts": self.now - 60}])
        fresh = self._task(updated_at=self.now - 600)
        inspection = self._task(emp_idx=watchdog._inspection_idx())
        result = await watchdog.sweep(None, now=self.now)
        self.assertEqual(0, result["task"])
        for tid in (recent_step, fresh, inspection):
            self.assertEqual("running", db.one(
                "SELECT status FROM task WHERE id=?", (tid,))["status"])

    async def test_threshold_is_configurable(self):
        task_id = self._task(updated_at=self.now - 20 * 60)
        with patch.dict(os.environ, {"CONTENTCREW_WATCHDOG_TASK_MINUTES": "10"}):
            result = await watchdog.sweep(None, now=self.now)
        self.assertEqual(1, result["task"])
        self.assertEqual("failed", db.one(
            "SELECT status FROM task WHERE id=?", (task_id,))["status"])
        with patch.dict(os.environ, {"CONTENTCREW_WATCHDOG_TASK_MINUTES": "abc"}):
            self.assertEqual(3600, watchdog.thresholds()["task"])

    async def test_cas_claim_loses_to_concurrent_progress(self):
        task_id = self._task()
        [row] = watchdog.stale_tasks(self.now, 3600)
        # 扫描之后、结算之前，正常执行器刚好写了一次进度。
        db.update("task", task_id, {"steps_json": "[]"})
        self.assertFalse(watchdog.settle_stale_task(row))
        current = db.one("SELECT status,billing_status FROM task WHERE id=?", (task_id,))
        self.assertEqual({"status": "running", "billing_status": "charged"}, current)

    async def test_claimed_queued_task_cannot_be_started_by_runner(self):
        task_id = self._task(status="queued")
        [row] = watchdog.stale_tasks(self.now, 3600)
        self.assertTrue(watchdog._claim_stale("task", row))
        # 认领后执行器的 queued→running 抢占必然落空。
        self.assertIsNone(taskrunner._claim_task(task_id))

    # ---------- meetings ----------
    async def test_stale_meeting_is_refunded_and_notified(self):
        mid = self._meeting()
        result = await watchdog.sweep(None, now=self.now)
        self.assertEqual(1, result["meeting"])
        row = db.one("SELECT * FROM meeting WHERE id=?", (mid,))
        self.assertEqual("failed", row["status"])
        self.assertEqual("refunded", row["billing_status"])
        self.assertEqual(54, billing.balance(2))
        [notice] = self._notices()
        self.assertEqual("会议没开完", notice["title"])
        self.assertEqual(f"#/meetings/{mid}", notice["link"])

    async def test_active_meeting_is_skipped(self):
        mid = self._meeting()
        with meeting._ActiveMeeting(mid):
            result = await watchdog.sweep(None, now=self.now)
        self.assertEqual(0, result["meeting"])
        self.assertEqual("running", db.one(
            "SELECT status FROM meeting WHERE id=?", (mid,))["status"])
        self.assertFalse(meeting.is_active(mid))

    async def test_stuck_execution_returns_to_manual_execute_without_refund(self):
        mid = self._meeting(phase="executing", decision="GO")
        result = await watchdog.sweep(None, now=self.now)
        self.assertEqual(1, result["meeting"])
        row = db.one("SELECT * FROM meeting WHERE id=?", (mid,))
        self.assertEqual("done", row["status"])
        self.assertEqual("awaiting_execution", row["phase"])
        self.assertEqual("charged", row["billing_status"])
        self.assertEqual(50, billing.balance(2))

    async def test_meeting_under_threshold_is_left_alone(self):
        mid = self._meeting(updated_at=self.now - HOUR)
        result = await watchdog.sweep(None, now=self.now)
        self.assertEqual(0, result["meeting"])
        self.assertEqual("running", db.one(
            "SELECT status FROM meeting WHERE id=?", (mid,))["status"])

    # ---------- content jobs ----------
    async def test_stale_job_is_settled_via_engine(self):
        engine = Engine()
        job_id = self._job()
        result = await watchdog.sweep(engine, now=self.now)
        self.assertEqual(1, result["job"])
        row = db.one("SELECT status,billing_status FROM job WHERE id=?", (job_id,))
        self.assertEqual({"status": "failed", "billing_status": "refunded"}, row)
        self.assertEqual(68, billing.balance(2))
        run = db.one("SELECT status,review_comment FROM station_run WHERE job_id=?", (job_id,))
        self.assertEqual("failed", run["status"])
        self.assertEqual(watchdog.TIMEOUT_MESSAGE, run["review_comment"])
        [notice] = self._notices()
        self.assertEqual(f"#/job/{job_id}", notice["link"])

    async def test_job_with_recent_station_activity_or_lock_is_skipped(self):
        engine = Engine()
        busy_station = self._job(run_updated_at=self.now - 60)
        locked = self._job()
        queued = self._job()
        lock = asyncio.Lock()
        await lock.acquire()
        engine.locks[locked] = lock
        engine.queue.put_nowait(queued)
        result = await watchdog.sweep(engine, now=self.now)
        self.assertEqual(0, result["job"])
        for job_id in (busy_station, locked, queued):
            self.assertEqual("running", db.one(
                "SELECT status FROM job WHERE id=?", (job_id,))["status"])
        lock.release()

    # ---------- loop resilience ----------
    async def test_loop_survives_sweep_errors(self):
        calls = []

        async def flaky(_engine):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("scan crashed")
            return {}

        with patch.object(watchdog, "interval_seconds", return_value=0.001), \
                patch.object(watchdog, "_ERROR_BACKOFF_SECONDS", 0.001), \
                patch.object(watchdog, "sweep", new=AsyncMock(side_effect=flaky)):
            runner = asyncio.ensure_future(watchdog.loop(None))
            for _ in range(500):
                await asyncio.sleep(0.005)
                if len(calls) >= 3:
                    break
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner
        self.assertGreaterEqual(len(calls), 3)

    async def test_one_bad_row_does_not_block_others(self):
        first = self._task()
        second = self._task()
        original = taskrunner.settle_failure

        def flaky(task_id, message):
            if task_id == first:
                raise RuntimeError("row broken")
            return original(task_id, message)

        with patch.object(taskrunner, "settle_failure", side_effect=flaky):
            result = await watchdog.sweep(None, now=self.now)
        self.assertEqual(1, result["task"])
        self.assertEqual("failed", db.one(
            "SELECT status FROM task WHERE id=?", (second,))["status"])


class MeetingOutcomeNotifyCase(unittest.IsolatedAsyncioTestCase):
    """会议失败走结算并通知老板；通知故障不影响会议终态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "phase0-meeting.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 8})
        db.insert("users", {"tenant_id": 2, "username": "boss2", "password_hash": "x", "role": "owner", "enabled": 1})
        meeting.ACTIVE.clear()

    def tearDown(self):
        meeting.ACTIVE.clear()
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _queued_meeting(self):
        return db.insert("meeting", {
            "tenant_id": 2,
            "question": "新品要不要本周上线？",
            "emp_idxs_json": "[0,1]",
            "member_snapshot_json": json.dumps(
                employeeidentity.member_snapshots([0, 1], active_only=True),
                ensure_ascii=False, separators=(",", ":"),
            ),
            "auto_execute": 0,
            "billing_status": "charged",
            "billing_points": 2,
        })

    async def test_failed_meeting_refunds_and_notifies_once(self):
        mid = self._queued_meeting()
        with patch.object(
            meeting, "_meeting_member_briefs",
            side_effect=RuntimeError("有效参会成员不足 2 人"),
        ), patch.object(meeting.registry, "company_block", return_value=""):
            await meeting.run(mid, lambda _e: None)
        row = db.one("SELECT status,billing_status FROM meeting WHERE id=?", (mid,))
        self.assertEqual({"status": "failed", "billing_status": "refunded"}, row)
        self.assertEqual(10, billing.balance(2))
        notices = db.q("SELECT title,body,link FROM notification WHERE tenant_id=2")
        self.assertEqual(1, len(notices))
        self.assertEqual("会议没开完", notices[0]["title"])
        self.assertIn("点数已自动退回", notices[0]["body"])
        self.assertFalse(meeting.is_active(mid))

    async def test_notify_failure_keeps_meeting_state(self):
        mid = self._queued_meeting()
        with patch("app.notify.push", side_effect=RuntimeError("down")), \
                patch.object(meeting, "_meeting_member_briefs",
                             side_effect=RuntimeError("x")), \
                patch.object(meeting.registry, "company_block", return_value=""):
            await meeting.run(mid, lambda _e: None)
        row = db.one("SELECT status,billing_status FROM meeting WHERE id=?", (mid,))
        self.assertEqual({"status": "failed", "billing_status": "refunded"}, row)

    def test_done_notification_is_plain_language(self):
        mid = self._queued_meeting()
        db.update("meeting", mid, {"status": "done", "decision": "GO"})
        self.assertTrue(meeting.notify_outcome(mid, True, "等你决定要不要执行"))
        [notice] = db.q("SELECT title,body,link FROM notification WHERE tenant_id=2")
        self.assertEqual("会议出结论了", notice["title"])
        self.assertIn("结论：建议干", notice["body"])
        self.assertIn("点开看结论", notice["body"])
        self.assertEqual(f"#/meetings/{mid}", notice["link"])

    def test_non_charged_settle_is_conditional(self):
        mid = self._queued_meeting()
        db.update("meeting", mid, {"billing_status": "included", "status": "done"})
        self.assertFalse(meeting.settle_failure(mid, "x"))
        self.assertEqual("done", db.one(
            "SELECT status FROM meeting WHERE id=?", (mid,))["status"])


if __name__ == "__main__":
    unittest.main()
