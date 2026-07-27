"""P1 async route boundaries reviewed after the PR #2 latency sweep."""
import ast
import asyncio
import os
import tempfile
import threading
import unittest
from unittest import mock

from app import auth, billing, db, main


class ReviewedP1RouteCallGraphTests(unittest.TestCase):
    TARGETS = {
        "avatar_script_from_link": {
            "_need_module",
            "_profile_id_for_tenant",
            "billing.start_operation",
            "billing.fail_operation",
            "billing.complete_operation",
        },
        "avatar_clone": {
            "_need_module",
            "_prepare_avatar_clone_sample",
            "_start_billed_operation",
            "billing.complete_operation_if_claimed",
            "billing.fail_operation",
        },
        "reconcile_wechat_delivery": {
            "_need_module",
            "_wechat_delivery_or_404",
            "_mark_wechat_submitted",
            "_finalize_wechat_delivery",
        },
        "confirm_wechat_delivery_not_delivered": {
            "_need_module",
            "_wechat_delivery_or_404",
            "_mark_wechat_submitted",
            "_finalize_wechat_delivery",
            "_fail_wechat_delivery",
        },
        "censor_check_api": {
            "_need_module",
            "_start_billed_operation",
            "billing.complete_operation_if_claimed",
            "billing.fail_operation",
        },
        "censor_retro_api": {
            "_need_module",
            "_start_billed_operation",
            "billing.complete_operation_if_claimed",
            "billing.fail_operation",
        },
        "menu_copy_api": {
            "_need_module",
            "_start_billed_operation",
            "billing.complete_operation",
            "billing.fail_operation",
        },
        "product_shot_api": {
            "_need_module",
            "_start_billed_operation",
            "billing.complete_operation",
            "billing.fail_operation",
            "open",
            "os.makedirs",
            "os.remove",
            "os.fsync",
        },
        "photo_factory_api": {
            "_need_module",
            "_start_billed_operation",
            "billing.fail_operation",
            "open",
            "os.makedirs",
            "os.remove",
            "os.fsync",
        },
        "_shot_leg": {
            "billing.complete_operation",
            "billing.fail_operation",
            "open",
            "os.makedirs",
            "os.remove",
            "os.fsync",
        },
        "_copy_leg": {
            "billing.complete_operation",
            "billing.fail_operation",
        },
        "variants_api": {
            "_need_module",
            "_start_billed_operation",
            "billing.complete_operation",
            "billing.fail_operation",
        },
        "matrix_publish": {
            "_need_module",
            "_job_or_404",
            "assetfiles.resolve_tenant_asset",
            "matrixpub.enqueue",
        },
        "meeting_suggest": {
            "auth.allowed",
            "departments.list_depts",
            "employees.is_enabled",
            "_meeting_member_view",
        },
    }
    LINEARIZED_QUEUE_ROUTES = {
        "create_job": 1,
        "task_create": 1,
        "task_redo": 1,
        "task_retry": 1,
        "task_center_retry": 5,
        "avatar_job_create": 1,
        "avatar_job_retry": 1,
        "meeting_create": 1,
        "meeting_ask": 1,
        "job_text_video": 1,
        "text_video_create": 2,
        "_tool_enqueue_async": 1,
    }

    @staticmethod
    def _call_name(call):
        parts = []
        node = call.func
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return ""

    def test_reviewed_p1_routes_have_no_inline_blocking_edges(self):
        path = os.path.join(os.path.dirname(main.__file__), "main.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        offenders = []
        for function_name, forbidden in self.TARGETS.items():
            matches = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == function_name
            ]
            self.assertEqual(
                1,
                len(matches),
                f"main.py:{function_name} 不再是唯一 async 入口",
            )
            target = matches[0]

            class DirectEdgeVisitor(ast.NodeVisitor):
                def visit_AsyncFunctionDef(self, node):
                    if node is target:
                        self.generic_visit(node)

                def visit_FunctionDef(self, node):
                    return

                def visit_Lambda(self, node):
                    return

                def visit_Call(self, node):
                    name = ReviewedP1RouteCallGraphTests._call_name(node)
                    if name in forbidden:
                        offenders.append(
                            f"main.py:{node.lineno} {function_name}->{name}"
                        )
                    self.generic_visit(node)

            DirectEdgeVisitor().visit(target)
        self.assertEqual(
            [],
            offenders,
            "P1 async 路由重新出现阻塞调用边:\n" + "\n".join(offenders),
        )

    def test_durable_queue_routes_linearize_commit_and_worker_start(self):
        path = os.path.join(os.path.dirname(main.__file__), "main.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }
        actual = {}
        for name, expected in self.LINEARIZED_QUEUE_ROUTES.items():
            self.assertIn(name, functions)
            actual[name] = sum(
                1
                for node in ast.walk(functions[name])
                if isinstance(node, ast.Call)
                and self._call_name(node)
                == "_run_db_then_start_worker_safely"
            )
            self.assertEqual(
                expected,
                actual[name],
                f"main.py:{name} 的持久队列提交与 worker 启动边界发生变化",
            )


class CancellationLinearizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_expert_create_starts_worker_after_late_commit(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        worker_started = asyncio.Event()
        committed = []
        worker_ids = []

        async def fake_arun(fn, *args, **kwargs):
            if fn is main._need_module:
                return None
            if fn is main.employees.is_enabled:
                return True
            if fn is main._create_charged_expert_task:
                entered.set()
                await release.wait()
                committed.append(42)
                return 42
            self.fail(f"unexpected DB operation: {fn}")

        async def fake_worker(task_id, _broadcast):
            worker_ids.append(task_id)
            worker_started.set()

        with mock.patch.object(main.departments, "get", return_value=None), \
                mock.patch.object(main.registry, "BY_IDX", {5: {}}), \
                mock.patch.object(
                    main.taskrunner,
                    "normalize_brief",
                    return_value={"direction": "cancel test"},
                ), \
                mock.patch.object(main.db, "arun", side_effect=fake_arun), \
                mock.patch.object(
                    main.taskrunner,
                    "run_task",
                    new=fake_worker,
                ):
            request = asyncio.create_task(
                main.task_create({"emp_idx": 5, "brief": {}})
            )
            await entered.wait()
            request.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await request
            await asyncio.wait_for(worker_started.wait(), timeout=1)

        self.assertEqual([42], committed)
        self.assertEqual([42], worker_ids)

    async def test_expert_create_does_not_wait_for_funnel_observability(self):
        funnel_entered = asyncio.Event()
        funnel_release = asyncio.Event()
        worker_started = asyncio.Event()

        async def fake_arun(fn, *args, **kwargs):
            if fn is main._need_module:
                return None
            if fn is main.employees.is_enabled:
                return True
            if fn is main._create_charged_expert_task:
                return 43
            if fn is main.funnel.record_first_work:
                funnel_entered.set()
                await funnel_release.wait()
                return None
            self.fail(f"unexpected DB operation: {fn}")

        async def fake_worker(_task_id, _broadcast):
            worker_started.set()

        with mock.patch.object(main.departments, "get", return_value=None), \
                mock.patch.object(main.registry, "BY_IDX", {5: {}}), \
                mock.patch.object(
                    main.taskrunner,
                    "normalize_brief",
                    return_value={"direction": "funnel test"},
                ), \
                mock.patch.object(main.db, "arun", side_effect=fake_arun), \
                mock.patch.object(
                    main.taskrunner,
                    "run_task",
                    new=fake_worker,
                ):
            result = await asyncio.wait_for(
                main.task_create({"emp_idx": 5, "brief": {}}),
                timeout=1,
            )
            await asyncio.wait_for(worker_started.wait(), timeout=1)
            await asyncio.wait_for(funnel_entered.wait(), timeout=1)
            self.assertEqual({"task_id": 43}, result)
            funnel_release.set()
            await asyncio.sleep(0)

    async def test_worker_schedule_failure_settles_committed_record(self):
        settled = []

        def commit():
            return 44

        def settle(record_id):
            settled.append(record_id)
            return True

        def fail_to_start(_record_id):
            raise RuntimeError("worker scheduler failed")

        async def fake_arun(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with mock.patch.object(main.db, "arun", side_effect=fake_arun):
            with self.assertRaisesRegex(
                RuntimeError,
                "worker scheduler failed",
            ):
                await main._run_db_then_start_worker_safely(
                    commit,
                    start_worker=fail_to_start,
                    settle_unstarted=settle,
                )

        self.assertEqual([44], settled)

    async def test_real_sqlite_late_commit_still_starts_worker(self):
        old_path = db.DB_PATH
        tmp = tempfile.TemporaryDirectory()
        entered = threading.Event()
        release = threading.Event()
        worker_started = asyncio.Event()
        worker_ids = []
        db._shutdown_async_pool(wait=True)
        try:
            db.DB_PATH = os.path.join(tmp.name, "queue-linearization.db")
            db.conn()

            def delayed_commit():
                entered.set()
                release.wait(timeout=5)
                return db.insert(
                    "tenants",
                    {"name": "late-commit-tenant", "balance": 0},
                )

            def start_worker(record_id):
                worker_ids.append(record_id)
                worker_started.set()

            request = asyncio.create_task(
                main._run_db_then_start_worker_safely(
                    delayed_commit,
                    start_worker=start_worker,
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            request.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await request
            await asyncio.wait_for(worker_started.wait(), timeout=1)

            row = await db.aone(
                "SELECT id FROM tenants WHERE name=?",
                ("late-commit-tenant",),
            )
            self.assertEqual([row["id"]], worker_ids)
        finally:
            release.set()
            db._shutdown_async_pool(wait=True)
            if db._conn is not None:
                db._conn.close()
            db._conn = None
            db.DB_PATH = old_path
            tmp.cleanup()

    async def test_cancelled_billing_start_refunds_late_charge(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        refunds = []

        def start_operation():
            return "late-op"

        async def fake_arun(fn, *args, **kwargs):
            if fn is start_operation:
                entered.set()
                await release.wait()
                return "late-op"
            if fn is main.billing.fail_operation:
                refunds.append(args)
                return True
            self.fail(f"unexpected DB operation: {fn}")

        with mock.patch.object(main.db, "arun", side_effect=fake_arun):
            request = asyncio.create_task(
                main._start_billing_operation_safely(
                    start_operation,
                    cancel_reason="cancelled",
                )
            )
            await entered.wait()
            request.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await request

        self.assertEqual([("late-op", "cancelled")], refunds)

    async def test_committing_settlement_wins_over_request_cancellation(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        def complete_operation():
            return True

        async def fake_arun(fn, *args, **kwargs):
            self.assertIs(fn, complete_operation)
            entered.set()
            await release.wait()
            return True

        with mock.patch.object(main.db, "arun", side_effect=fake_arun):
            request = asyncio.create_task(
                main._run_db_safely(complete_operation)
            )
            await entered.wait()
            request.cancel()
            release.set()
            self.assertTrue(await request)

    async def test_repeated_cancellation_cannot_interrupt_billing_start_refund(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        refunds = []

        def start_operation():
            return "late-op"

        async def fake_arun(fn, *args, **kwargs):
            if fn is start_operation:
                entered.set()
                await release.wait()
                return "late-op"
            if fn is main.billing.fail_operation:
                refunds.append(args)
                return True
            self.fail(f"unexpected DB operation: {fn}")

        with mock.patch.object(main.db, "arun", side_effect=fake_arun):
            request = asyncio.create_task(
                main._start_billing_operation_safely(
                    start_operation,
                    cancel_reason="cancelled",
                )
            )
            await entered.wait()
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await request

        self.assertEqual([("late-op", "cancelled")], refunds)

    async def test_repeated_cancellation_cannot_interrupt_refund(self):
        start_entered = asyncio.Event()
        start_release = asyncio.Event()
        refund_entered = asyncio.Event()
        refund_release = asyncio.Event()
        refunds = []

        def start_operation():
            return "late-op"

        async def fake_arun(fn, *args, **kwargs):
            if fn is start_operation:
                start_entered.set()
                await start_release.wait()
                return "late-op"
            if fn is main.billing.fail_operation:
                refund_entered.set()
                await refund_release.wait()
                refunds.append(args)
                return True
            self.fail(f"unexpected DB operation: {fn}")

        with mock.patch.object(main.db, "arun", side_effect=fake_arun):
            request = asyncio.create_task(
                main._start_billing_operation_safely(
                    start_operation,
                    cancel_reason="cancelled",
                )
            )
            await start_entered.wait()
            request.cancel()
            start_release.set()
            await refund_entered.wait()
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            refund_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await request

        self.assertEqual([("late-op", "cancelled")], refunds)

    async def test_repeated_cancellation_cannot_interrupt_settlement(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        effects = []

        def complete_operation():
            return "committed"

        async def fake_arun(fn, *args, **kwargs):
            self.assertIs(fn, complete_operation)
            entered.set()
            await release.wait()
            effects.append("committed")
            return "committed"

        with mock.patch.object(main.db, "arun", side_effect=fake_arun):
            request = asyncio.create_task(
                main._run_db_safely(complete_operation)
            )
            await entered.wait()
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            release.set()
            self.assertEqual("committed", await request)

        self.assertEqual(["committed"], effects)

    async def test_cancelled_billing_start_propagates_child_error(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        def start_operation():
            raise RuntimeError("start failed")

        async def fake_arun(fn, *args, **kwargs):
            self.assertIs(fn, start_operation)
            entered.set()
            await release.wait()
            raise RuntimeError("start failed")

        with mock.patch.object(main.db, "arun", side_effect=fake_arun):
            request = asyncio.create_task(
                main._start_billing_operation_safely(
                    start_operation,
                    cancel_reason="cancelled",
                )
            )
            await entered.wait()
            request.cancel()
            release.set()
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                await request

    async def test_cancelled_billing_refund_propagates_child_error(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        def start_operation():
            return "late-op"

        async def fake_arun(fn, *args, **kwargs):
            if fn is start_operation:
                entered.set()
                await release.wait()
                return "late-op"
            if fn is main.billing.fail_operation:
                raise RuntimeError("refund failed")
            self.fail(f"unexpected DB operation: {fn}")

        with mock.patch.object(main.db, "arun", side_effect=fake_arun):
            request = asyncio.create_task(
                main._start_billing_operation_safely(
                    start_operation,
                    cancel_reason="cancelled",
                )
            )
            await entered.wait()
            request.cancel()
            release.set()
            with self.assertRaisesRegex(RuntimeError, "refund failed"):
                await request

    async def test_repeated_cancellation_keeps_real_sqlite_ledger_consistent(self):
        old_path = db.DB_PATH
        tmp = tempfile.TemporaryDirectory()
        entered = threading.Event()
        release = threading.Event()
        db._shutdown_async_pool(wait=True)
        try:
            db.DB_PATH = os.path.join(tmp.name, "cancel-ledger.db")
            db.conn()
            db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
            db.insert("tenants", {"id": 2, "name": "企业", "balance": 20})
            auth.set_current({
                "id": 20,
                "tenant_id": 2,
                "username": "owner",
                "role": "owner",
                "modules": ["content"],
            })

            def delayed_start():
                entered.set()
                release.wait(timeout=5)
                return billing.start_operation(
                    "learn",
                    tid=2,
                    op_key="cancel:double:sqlite",
                )

            request = asyncio.create_task(
                main._start_billing_operation_safely(
                    delayed_start,
                    cancel_reason="cancelled",
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await request

            operation = await db.aone(
                "SELECT status FROM billing_operation WHERE op_key=?",
                ("cancel:double:sqlite",),
            )
            ledger = await db.aq(
                "SELECT delta,balance FROM billing_log "
                "WHERE tenant_id=2 ORDER BY id"
            )
            tenant = await db.aone(
                "SELECT balance FROM tenants WHERE id=2"
            )
            pending = await db.aone(
                "SELECT COUNT(*) n FROM billing_operation "
                "WHERE status='pending'"
            )

            self.assertEqual("refunded", operation["status"])
            self.assertEqual([-3, 3], [row["delta"] for row in ledger])
            self.assertEqual(20, tenant["balance"])
            self.assertEqual(tenant["balance"], ledger[-1]["balance"])
            self.assertEqual(0, pending["n"])
        finally:
            release.set()
            auth.set_current(None)
            db._shutdown_async_pool(wait=True)
            if db._conn is not None:
                db._conn.close()
            db._conn = None
            db.DB_PATH = old_path
            tmp.cleanup()

    async def test_cancelled_matrix_enqueue_still_starts_its_worker(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        worker_started = asyncio.Event()
        worker_ids = []

        def enqueue(*args):
            return 42

        async def run_task(task_id, _broadcast):
            worker_ids.append(task_id)
            worker_started.set()

        async def fake_arun(fn, *args, **kwargs):
            if fn is main._need_module:
                return None
            if fn is enqueue:
                entered.set()
                await release.wait()
                return 42
            return fn(*args, **kwargs)

        with mock.patch.object(main.db, "arun", side_effect=fake_arun), \
                mock.patch.object(main.matrixpub, "enqueue", new=enqueue), \
                mock.patch.object(main.matrixpub, "run_task", new=run_task):
            request = asyncio.create_task(
                main.matrix_publish(
                    {
                        "platform": "xhs",
                        "account": "account-1",
                        "title": "cancel test",
                        "body": "cancel test",
                    }
                )
            )
            await entered.wait()
            request.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await request
            await asyncio.wait_for(worker_started.wait(), timeout=1)

        self.assertEqual([42], worker_ids)


if __name__ == "__main__":
    unittest.main()
