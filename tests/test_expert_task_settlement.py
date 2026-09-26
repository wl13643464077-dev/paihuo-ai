import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import (
    auth,
    billing,
    db,
    employeeidentity,
    employees,
    llm,
    taskrunner,
    taskthreads,
)


def _core_task_binding(idx: int = 0) -> dict:
    """Exact current identity and immutable config revision for a core task."""
    employee = employeeidentity.active_employee(idx)
    return employeeidentity.task_fields(
        employee, config=employees.get_config(idx),
    )


class ExpertTaskSettlementCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
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
        # 这些用例只验证内容部核心员工的扣费/退款/幂等。并行构建中的
        # 行业V3目录可以主动fail-closed，但不能掩盖独立的核心结算合同。
        self._industry_lookup = patch.object(
            employeeidentity.departments, "get_active", return_value=None,
        )
        self._industry_lookup.start()
        self._industry_versions = patch.object(
            employeeidentity.departments, "identity_versions", return_value=[],
        )
        self._industry_versions.start()

    def _binding(self, idx: int = 0):
        config = employees.get_config(idx)
        return {
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }

    def tearDown(self):
        self._industry_versions.stop()
        self._industry_lookup.stop()
        auth.set_current(None)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
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
                        **self._binding(),
                        "brief": brief,
                    }))
                self.assertEqual(400, caught.exception.status_code)
                gateway.assert_not_awaited()
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) AS n FROM task")["n"],
        )
        self.assertEqual(5, billing.balance(2))

    def test_initial_task_request_key_replays_without_duplicate_charge_or_worker(self):
        from app import main

        body = {
            "emp_idx": 0,
            **self._binding(),
            "brief": {"direction": "写一份门店方案", "material": "现场数据"},
            "request_key": "initial-task-idempotent-0001",
        }
        with patch.object(main, "_start_expert_task_worker", return_value=None) as start:
            first = asyncio.run(main.task_create(body))
            replay = asyncio.run(main.task_create(body))

        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["task_id"], replay["task_id"])
        self.assertEqual(1, start.call_count)
        self.assertEqual(1, db.one("SELECT COUNT(*) AS n FROM task")["n"])
        self.assertEqual(4, billing.balance(2))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.task_create({
                **body,
                "brief": {"direction": "换成一个不同任务"},
            }))
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(1, db.one("SELECT COUNT(*) AS n FROM task")["n"])
        self.assertEqual(4, billing.balance(2))

    def test_concurrent_initial_task_same_key_is_one_charge_and_one_replay(self):
        from app import main

        task_data = {
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "并发派活"}, ensure_ascii=False),
            "created_by": 20,
        }
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def submit():
            try:
                barrier.wait()
                results.append(main._create_idempotent_expert_task(
                    task_data,
                    "initial-task-concurrent-0001",
                    20,
                    note="并发派活",
                ))
            except Exception as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertEqual([False, True], sorted(item["created"] for item in results))
        self.assertEqual(1, len({item["task_id"] for item in results}))
        self.assertEqual(1, db.one("SELECT COUNT(*) AS n FROM task")["n"])
        self.assertEqual(4, billing.balance(2))

    def test_thread_delivery_is_immutable_but_legacy_standalone_can_still_be_edited(self):
        from app import main

        standalone = db.insert("task", {
            "emp_idx": 0,
            **_core_task_binding(),
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "单版任务"}),
            "status": "done",
            "output_md": "# 旧内容",
        })
        self.assertEqual({"ok": True}, main.task_output_edit(
            standalone, {"md": "# 手工校正"},
        ))
        self.assertEqual(
            "# 手工校正",
            db.one("SELECT output_md FROM task WHERE id=?", (standalone,))["output_md"],
        )

        taskthreads.ensure_thread(standalone, 2, actor_id=20)
        with self.assertRaises(HTTPException) as caught:
            main.task_output_edit(standalone, {"md": "# 篡改历史"})
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(
            "# 手工校正",
            db.one("SELECT output_md FROM task WHERE id=?", (standalone,))["output_md"],
        )

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

    def test_no_search_failure_refunds_once_and_persists_accurate_safe_message(self):
        from app import main, providers

        task_id = main._create_charged_expert_task({
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "联网核验一份经营方案"}),
        })
        marker = "PRIVATE-UPSTREAM-DETAIL"
        gateway_error = llm.WebSearchRequiredError(
            marker, cost_usd=0.3, tokens=30
        )
        with patch.object(
            providers,
            "call_text",
            AsyncMock(side_effect=gateway_error),
        ) as gateway:
            asyncio.run(taskrunner.run_task(task_id, lambda _payload: None))

        gateway.assert_awaited_once()
        row = db.one(
            "SELECT status,billing_status,output_md,cost_usd,tokens "
            "FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual("failed", row["status"])
        self.assertEqual("refunded", row["billing_status"])
        self.assertIn("联网检索未返回有效结果", row["output_md"])
        self.assertIn("免费重试", row["output_md"])
        self.assertNotIn("超时或繁忙", row["output_md"])
        self.assertNotIn(marker, row["output_md"])
        self.assertAlmostEqual(0.3, row["cost_usd"])
        self.assertEqual(30, row["tokens"])
        self.assertEqual(5, billing.balance(2))
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_log "
                "WHERE tenant_id=2 AND delta=1"
            )["n"],
        )

    def test_oversized_failure_usage_cannot_block_refund_settlement(self):
        from app import main, providers

        task_id = main._create_charged_expert_task({
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "联网核验异常用量边界"}),
        })
        gateway_error = llm.WebSearchRequiredError(
            cost_usd=0.3,
            tokens=10 ** 30,
        )
        with patch.object(
            providers,
            "call_text",
            AsyncMock(side_effect=gateway_error),
        ):
            asyncio.run(taskrunner.run_task(task_id, lambda _payload: None))

        row = db.one(
            "SELECT status,billing_status,tokens FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual("failed", row["status"])
        self.assertEqual("refunded", row["billing_status"])
        self.assertLessEqual(row["tokens"], llm.MAX_RECORDED_TOKENS)
        self.assertEqual(5, billing.balance(2))

    def test_immediate_free_retry_gets_a_follow_on_worker(self):
        from app import main, providers

        task_id = main._create_charged_expert_task({
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "失败后立即免费重试"}),
        })
        calls = AsyncMock(side_effect=[
            llm.WebSearchRequiredError(cost_usd=0.1, tokens=10),
            {"text": "# 重试成功\n正文", "cost_usd": 0.2, "tokens": 20},
        ])
        retry_results = []

        def broadcast(payload):
            step = payload.get("step") if isinstance(payload, dict) else None
            if (
                isinstance(step, dict)
                and step.get("k") == "error"
                and not retry_results
            ):
                retry_results.append(taskrunner.prepare_retry(task_id, 2))
                taskrunner.start_worker(task_id, broadcast)

        async def scenario():
            # v2 also summarizes short deliveries; this test counts execution
            # attempts only, so keep the post-delivery summary independent.
            with patch.object(providers, "call_text", calls), \
                    patch.object(taskrunner, "_gen_summary", AsyncMock()):
                first = taskrunner.start_worker(task_id, broadcast)
                await first
                for _ in range(200):
                    row = db.one(
                        "SELECT status FROM task WHERE id=?", (task_id,)
                    )
                    if row and row["status"] == "done":
                        break
                    await asyncio.sleep(0.01)
                current = taskrunner.WORKER_TASKS.get(task_id)
                if current is not None:
                    await asyncio.gather(current, return_exceptions=True)

        asyncio.run(scenario())
        row = db.one(
            "SELECT status,billing_status,cost_usd,tokens FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual([True], retry_results)
        self.assertEqual(2, calls.await_count)
        self.assertEqual("done", row["status"])
        self.assertEqual("included", row["billing_status"])
        self.assertAlmostEqual(0.3, row["cost_usd"])
        self.assertEqual(30, row["tokens"])
        self.assertNotIn(task_id, taskrunner.WORKER_TASKS)
        self.assertNotIn(task_id, taskrunner.RUNNING)

    def test_free_retries_preserve_and_accumulate_real_provider_usage(self):
        from app import main, providers

        task_id = main._create_charged_expert_task({
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "连续联网核验"}),
        })
        self.assertTrue(taskrunner.settle_failure(
            task_id,
            "第一次失败",
            cost_usd=0.3,
            tokens=30,
        ))
        self.assertTrue(taskrunner.prepare_retry(task_id, 2))
        queued = db.one(
            "SELECT cost_usd,tokens FROM task WHERE id=?", (task_id,)
        )
        self.assertAlmostEqual(0.3, queued["cost_usd"])
        self.assertEqual(30, queued["tokens"])

        self.assertTrue(taskrunner.settle_failure(
            task_id,
            "第二次失败",
            cost_usd=0.2,
            tokens=20,
        ))
        failed_again = db.one(
            "SELECT cost_usd,tokens FROM task WHERE id=?", (task_id,)
        )
        self.assertAlmostEqual(0.5, failed_again["cost_usd"])
        self.assertEqual(50, failed_again["tokens"])

        self.assertTrue(taskrunner.prepare_retry(task_id, 2))
        with patch.object(
            providers,
            "call_text",
            AsyncMock(return_value={
                "text": "# 最终交付\n正文",
                "cost_usd": 0.4,
                "tokens": 40,
            }),
        ):
            asyncio.run(taskrunner.run_task(task_id, lambda _payload: None))

        delivered = db.one(
            "SELECT status,billing_status,cost_usd,tokens FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual("done", delivered["status"])
        self.assertEqual("included", delivered["billing_status"])
        self.assertAlmostEqual(0.9, delivered["cost_usd"])
        self.assertEqual(90, delivered["tokens"])
        self.assertEqual(5, billing.balance(2))
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_log "
                "WHERE tenant_id=2 AND delta=1"
            )["n"],
        )

    def test_redo_rejects_oversized_feedback_before_charge_or_task_creation(self):
        from app import main

        task_id = db.insert("task", {
            "emp_idx": 0,
            **_core_task_binding(),
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
                {
                    "feedback": "改" * 2001,
                    "request_key": "redo-oversized-feedback-0001",
                },
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
            **_core_task_binding(),
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

    def test_delete_cancels_a_worker_waiting_before_provider_execution(self):
        from app import main, providers

        task_id = main._create_charged_expert_task({
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "等待联网槽位"}),
        })
        entered = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked_provider(*_args, **_kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {"text": "# 不应交付", "cost_usd": 1, "tokens": 100}

        async def scenario():
            with patch.object(providers, "call_text", side_effect=blocked_provider):
                worker = taskrunner.start_worker(task_id, lambda _payload: None)
                await asyncio.wait_for(entered.wait(), 5)
                deleted = main.task_delete(task_id)
                try:
                    await asyncio.wait_for(cancelled.wait(), 0.2)
                finally:
                    release.set()
                    await asyncio.gather(worker, return_exceptions=True)
                return deleted

        deleted = asyncio.run(scenario())
        self.assertTrue(deleted.get("soft_deleted"))
        self.assertNotIn(task_id, taskrunner.WORKER_TASKS)
        self.assertNotIn(task_id, taskrunner.RUNNING)
        self.assertEqual(5, billing.balance(2))

    def test_delete_cancels_a_retry_worker_started_during_settlement(self):
        from app import main, providers

        task_id = main._create_charged_expert_task({
            "emp_idx": 0,
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "删除与免费重试并发"}),
        })
        self.assertTrue(taskrunner.settle_failure(task_id, "第一次失败"))
        original_settle = taskrunner.settle_failure
        delete_in_settlement = threading.Event()
        release_delete = threading.Event()
        provider_entered = asyncio.Event()
        provider_cancelled = asyncio.Event()
        provider_release = asyncio.Event()

        def gated_settle(*args, **kwargs):
            if len(args) > 1 and args[1] == "老板删除未交付任务":
                delete_in_settlement.set()
                release_delete.wait(timeout=5)
            return original_settle(*args, **kwargs)

        async def blocked_provider(*_args, **_kwargs):
            provider_entered.set()
            try:
                await provider_release.wait()
            except asyncio.CancelledError:
                provider_cancelled.set()
                raise
            return {"text": "# 不应交付", "cost_usd": 1, "tokens": 100}

        async def scenario():
            worker = None
            was_cancelled = False
            with patch.object(taskrunner, "settle_failure", side_effect=gated_settle), \
                    patch.object(providers, "call_text", side_effect=blocked_provider):
                deleting = asyncio.create_task(asyncio.to_thread(main.task_delete, task_id))
                entered = await asyncio.to_thread(delete_in_settlement.wait, 5)
                self.assertTrue(entered)
                self.assertTrue(taskrunner.prepare_retry(task_id, 2))
                worker = taskrunner.start_worker(task_id, lambda _payload: None)
                await asyncio.wait_for(provider_entered.wait(), 5)
                release_delete.set()
                deleted = await deleting
                try:
                    await asyncio.wait_for(provider_cancelled.wait(), 0.5)
                    was_cancelled = True
                except asyncio.TimeoutError:
                    pass
                finally:
                    provider_release.set()
                    taskrunner.cancel_worker(task_id)
                    await asyncio.gather(worker, return_exceptions=True)
                return deleted, was_cancelled

        deleted, was_cancelled = asyncio.run(scenario())
        self.assertTrue(deleted.get("soft_deleted"))
        self.assertTrue(was_cancelled)
        self.assertNotIn(task_id, taskrunner.WORKER_TASKS)
        self.assertNotIn(task_id, taskrunner.RUNNING)
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) AS n FROM asset WHERE payload_json LIKE ?",
                (f'%"task_id": {task_id}%',),
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
            **_core_task_binding(),
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
