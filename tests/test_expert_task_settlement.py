import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import auth, billing, db, taskrunner


class ExpertTaskSettlementCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "expert-task.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 5})
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner",
            "role": "owner",
            "modules": ["content"],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_direct_task_is_created_then_charged_and_failure_refunds_once(self):
        from app import main

        task_id = main._create_charged_expert_task({
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "写一份方案"}),
        })
        row = db.one(
            "SELECT status,billing_status,billing_points FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual("queued", row["status"])
        self.assertEqual("charged", row["billing_status"])
        self.assertEqual(1, row["billing_points"])
        self.assertEqual(4, billing.balance(2))

        self.assertTrue(taskrunner.settle_failure(task_id, "模型失败"))
        self.assertFalse(taskrunner.settle_failure(task_id, "重复结算"))
        self.assertEqual(5, billing.balance(2))
        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM task WHERE id=?",
                (task_id,),
            ),
        )
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_log "
                "WHERE tenant_id=2 AND delta=1"
            )["n"],
        )

    def test_direct_task_rejects_malformed_brief_before_charge_or_create(self):
        from app import main, providers

        cases = (
            {"direction": "正常任务", "material": 1},
            {"direction": "任" * 2001},
            {"direction": "正常任务", "industry": ["不应是数组"]},
            {"direction": "正常任务", "length": "unbounded"},
        )
        for brief in cases:
            with self.subTest(brief=brief), patch.object(
                providers, "call_text", AsyncMock()
            ) as gateway:
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(main.task_create({
                        "emp_idx": 0,
                        "brief": brief,
                    }))
                self.assertEqual(400, caught.exception.status_code)
                gateway.assert_not_awaited()
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) AS n FROM task")["n"],
        )
        self.assertEqual(5, billing.balance(2))

    def test_post_claim_prompt_failure_refunds_and_clears_running(self):
        from app import main, providers

        task_id = main._create_charged_expert_task({
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({
                "direction": "正常任务",
                "material": 1,
            }),
        })
        self.assertEqual(4, billing.balance(2))
        with patch.object(providers, "call_text", AsyncMock()) as gateway:
            asyncio.run(taskrunner.run_task(task_id, lambda _payload: None))

        gateway.assert_not_awaited()
        self.assertNotIn(task_id, taskrunner.RUNNING)
        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM task WHERE id=?",
                (task_id,),
            ),
        )
        self.assertEqual(5, billing.balance(2))

    def test_redo_rejects_oversized_feedback_before_charge_or_task_creation(self):
        from app import main

        task_id = db.insert("task", {
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "原任务"}),
            "status": "done",
            "billing_status": "succeeded",
            "billing_points": 1,
            "output_md": "# 原交付",
        })
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.task_redo(
                task_id,
                {"feedback": "改" * 2001},
            ))
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual(
            1,
            db.one("SELECT COUNT(*) AS n FROM task")["n"],
        )
        self.assertEqual(5, billing.balance(2))

    def test_meeting_included_task_fails_without_creating_a_refund(self):
        task_id = db.insert("task", {
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": "{}",
            "status": "running",
        })
        self.assertTrue(taskrunner.settle_failure(task_id, "会议内任务失败"))
        row = db.one(
            "SELECT status,billing_status FROM task WHERE id=?", (task_id,))
        self.assertEqual(
            {"status": "failed", "billing_status": "included"},
            row,
        )
        self.assertEqual(5, billing.balance(2))
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) AS n FROM billing_log WHERE tenant_id=2")["n"],
        )

    def test_database_claim_allows_only_one_worker(self):
        task_id = db.insert("task", {
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": "{}",
            "status": "queued",
            "billing_status": "charged",
            "billing_points": 1,
        })
        self.assertIsNotNone(taskrunner._claim_task(task_id))
        self.assertIsNone(taskrunner._claim_task(task_id))
        self.assertEqual(
            "running",
            db.one("SELECT status FROM task WHERE id=?", (task_id,))["status"],
        )

    def test_late_provider_result_after_delete_cannot_create_orphan_asset(self):
        from app import main, providers

        task_id = main._create_charged_expert_task({
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "写一份方案"}),
        })
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_result(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"text": "# 晚到结果\n正文", "cost_usd": 0.1, "tokens": 12}

        async def scenario():
            with patch.object(providers, "call_text", side_effect=delayed_result), \
                    patch.object(taskrunner, "_enforce_length",
                                 AsyncMock(side_effect=lambda _i, md, _b, _p: (md, 0))):
                worker = asyncio.create_task(taskrunner.run_task(task_id, lambda _x: None))
                await started.wait()
                main.task_delete(task_id)
                release.set()
                await worker

        asyncio.run(scenario())
        hidden = db.one(
            "SELECT id,deleted_at FROM task WHERE id=?", (task_id,)
        )
        self.assertEqual(task_id, hidden["id"])
        self.assertIsNotNone(hidden["deleted_at"])
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) AS n FROM asset "
                "WHERE payload_json LIKE ?",
                (f'%\"task_id\": {task_id}%',),
            )["n"],
        )
        self.assertEqual(5, billing.balance(2))
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_log "
                "WHERE tenant_id=2 AND delta=1"
            )["n"],
        )

    def test_startup_refunds_legacy_failed_charge_and_cleans_uncharged_shell(self):
        failed = db.insert("task", {
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": "{}",
            "status": "failed",
            "billing_status": "charged",
            "billing_points": None,
        })
        shell = db.insert("task", {
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": "{}",
            "status": "pending_charge",
            "billing_status": "pending",
            "billing_points": 1,
        })
        billing.charge("expert_task", tid=2, note="遗留失败任务")
        taskrunner.resume_pending(lambda _payload: None)

        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM task WHERE id=?", (failed,)
            ),
        )
        self.assertIsNone(db.one("SELECT id FROM task WHERE id=?", (shell,)))
        self.assertEqual(5, billing.balance(2))

    def test_delete_refunds_legacy_failed_charge_before_removing_anchor(self):
        from app import main

        task_id = db.insert("task", {
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": "{}",
            "status": "failed",
            "billing_status": "charged",
            "billing_points": 1,
        })
        billing.charge("expert_task", tid=2, note="遗留失败任务")
        main.task_delete(task_id)
        hidden = db.one(
            "SELECT id,deleted_at,billing_status FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual(task_id, hidden["id"])
        self.assertIsNotNone(hidden["deleted_at"])
        self.assertEqual("refunded", hidden["billing_status"])
        self.assertEqual(5, billing.balance(2))


if __name__ == "__main__":
    unittest.main()
