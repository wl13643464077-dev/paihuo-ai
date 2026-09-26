"""Exercise the normal startup path, including workers skipped by validation mode."""

import asyncio
from contextlib import ExitStack
import os
import tempfile
import unittest
from unittest import mock

from app import db, main, watchdog


class ProductionLifespanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.temp.name, "startup.db")
        self.old_state = dict(main.app.state._state)

    def tearDown(self):
        main.app.state._state.clear()
        main.app.state._state.update(self.old_state)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()

    async def _normal_startup(self, *, sweep_error=False):
        # Keep real lifespan registration and temporary-database initialization.
        # Mock outbound/recovery work and each worker body, not the startup code.
        with ExitStack() as patches:
            patches.enter_context(mock.patch.object(main, "VALIDATION", False))
            patches.enter_context(mock.patch.object(main.instancelock, "acquire", return_value="test-lock"))
            patches.enter_context(mock.patch.object(main.obs, "register_gauge"))
            patches.enter_context(mock.patch.object(main.obs, "watch_task"))
            sync_stubs = [
                (main.auth, "bootstrap", None),
                (main.secureconfig, "migrate_legacy_secrets", {}),
                (main, "_sync_platform_industry_scope", 0),
                (main.avatar, "recover_asset_transactions", {"recovered": 0}),
                (main.meeting, "recover_interventions", 0),
                (main.pubtrack, "recover_interrupted", 0),
                (main, "_recover_wechat_deliveries", (0, set())),
                (main.billing, "settle_legacy_subscriptions", 0),
                (main.employeelearning, "recover_interrupted_runs", 0),
                (main, "_recover_employee_learning_billing", (0, set())),
                (main.billing, "recover_interrupted_operations", 0),
                (main, "_recover_unpaid_activity_artwork", {"scanned": 0}),
                (main, "_detect_orphaned_learning_batches_for_restart", 0),
                (main.taskrunner, "resume_pending", None),
                (main.avatar, "resume_pending", None),
                (main.features, "legacy_since", 0),
                (main.meeting, "resume_pending", None),
                (main.textvideo, "resume_pending", None),
                (main.matrixpub, "resume_pending", None),
                (main, "_recover_interrupted_tool_jobs", None),
                (main, "_ensure_tool_running_index", None),
                (main, "_start_tool_watchdog", None),
            ]
            for target, name, result in sync_stubs:
                patches.enter_context(mock.patch.object(target, name, return_value=result))
            sweep = patches.enter_context(mock.patch.object(
                main.inspectionimport, "cleanup_expired_previews",
                return_value={"scanned": 1, "expired": 0, "compacted": 0},
                side_effect=RuntimeError("test cleanup unavailable") if sweep_error else None,
            ))
            async_stubs = [
                (main.engine, "start"), (main.scheduler, "loop"),
                (watchdog, "loop"), (main.analyzer, "loop"),
                (main.purchases, "pay_order_loop"), (main.retention, "loop"),
                (main.reminders, "loop"), (main, "_resume_inspection_tasks"),
                (main, "_backfill_inspection_scores"),
                (main.avatar, "public_cleanup_loop"),
                (main, "_team_run_recovery_loop"),
            ]
            workers = {
                (id(target), name): patches.enter_context(mock.patch.object(
                    target, name, new_callable=mock.AsyncMock,
                ))
                for target, name in async_stubs
            }
            async with main.app.router.lifespan_context(main.app):
                await asyncio.sleep(0)
                sweep.assert_called_once_with()
                workers[(id(main.retention), "loop")].assert_awaited_once_with()
                workers[(id(main.reminders), "loop")].assert_awaited_once_with()
                workers[(id(main.purchases), "pay_order_loop")].assert_awaited_once_with()
                workers[(id(main), "_team_run_recovery_loop")].assert_awaited_once_with()
                self.assertFalse(main.app.state.learning_batch_shutting_down)
            self.assertTrue(main.app.state.learning_batch_shutting_down)

    async def test_normal_lifespan_starts_post_validation_workers(self):
        await self._normal_startup()

    async def test_cleanup_failure_does_not_block_normal_lifespan(self):
        await self._normal_startup(sweep_error=True)


if __name__ == "__main__":
    unittest.main()
