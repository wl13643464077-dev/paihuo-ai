"""P1 async route boundaries reviewed after the PR #2 latency sweep."""
import ast
import asyncio
import os
import unittest
from unittest import mock

from app import main


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


class CancellationLinearizationTests(unittest.IsolatedAsyncioTestCase):
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
