"""Phase0：专家任务异常路径兜底、失败步骤落库、速览要点与完成/失败通知。"""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import billing, db, employeeidentity, llm, taskrunner, watchdog


class TaskRunnerPhase0Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "phase0-task.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 10})
        taskrunner.RUNNING.clear()

    def tearDown(self):
        taskrunner.RUNNING.clear()
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _task(self, *, status="queued", billing_status="charged", idx=0, **extra):
        employee = employeeidentity.any_employee(idx)
        self.assertIsNotNone(employee)
        row = {
            "tenant_id": 2,
            "emp_idx": idx,
            "brief_json": json.dumps({"direction": "做一份门店选址报告"}),
            "status": status,
            "billing_status": billing_status,
            "billing_points": 3,
            **employeeidentity.task_fields(employee),
        }
        row.update(extra)
        return db.insert("task", row)

    def _row(self, task_id):
        return db.one("SELECT * FROM task WHERE id=?", (task_id,))

    def _notices(self):
        return db.q("SELECT title,body,link FROM notification WHERE tenant_id=2")

    async def test_binding_resolution_error_settles_and_refunds(self):
        task_id = self._task()
        with patch.object(
            taskrunner.employeeidentity,
            "resolve_task_binding",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            await taskrunner.run_task(task_id, lambda _p: None)
        row = self._row(task_id)
        self.assertEqual("failed", row["status"])
        self.assertEqual("refunded", row["billing_status"])
        self.assertEqual(13, billing.balance(2))
        self.assertNotIn(task_id, taskrunner.RUNNING)
        notices = self._notices()
        self.assertEqual(1, len(notices))
        self.assertEqual("专家任务没做完", notices[0]["title"])
        self.assertEqual(f"#/tasks/{task_id}", notices[0]["link"])

    async def test_claim_error_does_not_leave_charged_task_queued(self):
        task_id = self._task()
        with patch.object(
            taskrunner, "_claim_task",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            await taskrunner.run_task(task_id, lambda _p: None)
        row = self._row(task_id)
        self.assertEqual("failed", row["status"])
        self.assertEqual("refunded", row["billing_status"])
        self.assertEqual(13, billing.balance(2))
        self.assertNotIn(task_id, taskrunner.RUNNING)

    async def test_settlement_error_falls_back_to_failed_and_watchdog_refunds_later(self):
        task_id = self._task()
        with patch.object(
            taskrunner.employeeidentity, "resolve_task_binding", return_value=None,
        ), patch.object(
            taskrunner, "settle_failure", side_effect=RuntimeError("billing down"),
        ):
            await taskrunner.run_task(task_id, lambda _p: None)
        row = self._row(task_id)
        # 结算失败也不许停在排队中；扣费标记保留，等对账补退。
        self.assertEqual("failed", row["status"])
        self.assertEqual("charged", row["billing_status"])
        self.assertEqual(10, billing.balance(2))
        self.assertEqual([], self._notices())

        # 看门狗对账：failed+charged 补一次幂等退款，并沿用原失败说明。
        result = await watchdog.sweep(None, now=row["updated_at"] + 3600)
        self.assertEqual(1, result["refund"])
        row = self._row(task_id)
        self.assertEqual("refunded", row["billing_status"])
        self.assertIn("能力包版本不匹配", row["output_md"])
        self.assertEqual(13, billing.balance(2))
        again = await watchdog.sweep(None, now=row["updated_at"] + 3600)
        self.assertEqual(0, again["refund"])
        self.assertEqual(13, billing.balance(2))

    async def test_llm_failure_persists_error_step_before_settlement(self):
        task_id = self._task()
        with patch.object(
            taskrunner.providers, "call_text",
            new=AsyncMock(side_effect=llm.LLMError("upstream")),
        ):
            await taskrunner.run_task(task_id, lambda _p: None)
        await db.adrain()
        row = self._row(task_id)
        self.assertEqual("failed", row["status"])
        self.assertEqual("refunded", row["billing_status"])
        steps = json.loads(row["steps_json"])
        self.assertEqual("error", steps[-1]["k"])
        self.assertEqual(row["output_md"], steps[-1]["l"])
        self.assertNotIn(task_id, taskrunner.RUNNING)
        self.assertEqual(1, len(self._notices()))

    async def test_unexpected_failure_also_persists_error_step(self):
        task_id = self._task()
        with patch.object(
            taskrunner.providers, "call_text",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            await taskrunner.run_task(task_id, lambda _p: None)
        await db.adrain()
        steps = json.loads(self._row(task_id)["steps_json"])
        self.assertEqual("error", steps[-1]["k"])

    async def test_success_notifies_boss_once_and_meeting_tasks_stay_quiet(self):
        task_id = self._task(idx=101)
        with patch.object(
            taskrunner.providers, "call_text",
            new=AsyncMock(return_value={
                "text": "# 选址结论\n先看人流。", "cost_usd": 0.0, "tokens": 1,
            }),
        ), patch.object(
            taskrunner.departments, "enforce_decision_output",
            return_value={"is_decision": False},
        ):
            await taskrunner.run_task(task_id, lambda _p: None)
        self.assertEqual("done", self._row(task_id)["status"])
        notices = self._notices()
        self.assertEqual(1, len(notices))
        self.assertEqual("专家任务已完成", notices[0]["title"])
        self.assertIn("帮你做的《做一份门店选址报告》已完成，点开看结论", notices[0]["body"])
        self.assertTrue(notices[0]["body"].startswith("【"))

        meeting_task = self._task(source_meeting_id=99, source_action_key="k1",
                                  billing_status="included")
        self.assertFalse(taskrunner.notify_task_outcome(meeting_task, True))
        self.assertEqual(1, len(self._notices()))

    async def test_notification_failure_never_changes_task_state(self):
        task_id = self._task()
        with patch("app.notify.push", side_effect=RuntimeError("wechat down")):
            with patch.object(
                taskrunner.providers, "call_text",
                new=AsyncMock(side_effect=llm.LLMError("x")),
            ):
                await taskrunner.run_task(task_id, lambda _p: None)
        row = self._row(task_id)
        self.assertEqual("failed", row["status"])
        self.assertEqual("refunded", row["billing_status"])

    def test_summary_points_string_is_not_split_into_characters(self):
        self.assertEqual(
            ["客流周末高 30%", "租金偏高", "建议先谈免租期"],
            taskrunner._summary_points("1. 客流周末高 30%\n- 租金偏高；建议先谈免租期"),
        )
        self.assertEqual(["一条"], taskrunner._summary_points(["一条", "", None]))
        self.assertEqual([], taskrunner._summary_points(None))
        self.assertEqual(5, len(taskrunner._summary_points([str(i) for i in range(9)])))

    async def test_gen_summary_with_string_points_writes_readable_card(self):
        md = "# 报告\n" + "正文。" * 800
        task_id = self._task(status="done", billing_status="succeeded", output_md=md)
        with patch.object(
            taskrunner.providers, "call_text_json",
            new=AsyncMock(return_value={
                "data": {"points": "客流稳定\n租金可谈", "action": "本周约房东"},
                "cost_usd": 0.0,
            }),
        ):
            await taskrunner._gen_summary(task_id, md, 0, 2, lambda _p: None)
        summary = self._row(task_id)["summary_md"]
        self.assertEqual(
            ["- 客流稳定", "- 租金可谈"], summary.splitlines()[:2]
        )

    async def test_gen_summary_failure_is_logged_as_warning(self):
        md = "# 报告\n" + "正文。" * 800
        task_id = self._task(status="done", billing_status="succeeded", output_md=md)
        with patch.object(
            taskrunner.providers, "call_text_json",
            new=AsyncMock(side_effect=RuntimeError("model down")),
        ), self.assertLogs("taskrunner", level="WARNING") as logs:
            await taskrunner._gen_summary(task_id, md, 0, 2, lambda _p: None)
        self.assertTrue(any("RuntimeError" in line for line in logs.output))
        self.assertIsNone(self._row(task_id)["summary_md"])


if __name__ == "__main__":
    unittest.main()
