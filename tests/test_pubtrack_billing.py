import asyncio
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import auth, billing, db, pubtrack


class PublicationRetroBillingCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "pubtrack.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 10})
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner",
            "role": "owner",
            "modules": ["content"],
        })
        self.pid = pubtrack.add_entry(
            2,
            "公众号",
            "测试文章",
            published_at=time.time() - 86400,
        )

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def row(self):
        return db.one("SELECT * FROM publish_log WHERE id=?", (self.pid,))

    def make_due(self):
        row = self.row()
        retro = db.jloads(row["retro_json"], {})
        retro["1"]["due"] = time.time() - 1
        db.update(
            "publish_log",
            self.pid,
            {"retro_json": json.dumps(retro, ensure_ascii=False)},
        )

    async def test_failure_refunds_and_restores_retryable_state(self):
        with patch.object(
            pubtrack.wechat,
            "get_conf",
            return_value={"appid": "app", "secret": "secret"},
        ), patch.object(
            pubtrack.wechat,
            "article_stats",
            new=AsyncMock(return_value={"read_user": 10}),
        ), patch.object(
            pubtrack.censor,
            "retro",
            new=AsyncMock(side_effect=RuntimeError("model down")),
        ):
            self.assertFalse(await pubtrack.auto_retro(2, self.row(), 1))

        self.assertEqual(10, billing.balance(2))
        operation = db.one(
            "SELECT status FROM billing_operation "
            "WHERE action='censor_retro' ORDER BY created_at DESC LIMIT 1"
        )
        self.assertEqual("refunded", operation["status"])
        state = db.jloads(self.row()["retro_json"], {})["1"]
        self.assertEqual("pending", state["state"])
        self.assertNotIn("op_key", state)
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) n FROM censor_log")["n"],
        )

    async def test_same_publication_day_is_delivered_and_charged_once(self):
        result = {
            "report": "# 复盘报告",
            "grade": "良好",
            "signals": [],
            "actions": ["继续测试"],
            "cost_usd": 0.1,
            "tokens": 20,
        }
        retro = AsyncMock(return_value=result)
        with patch.object(
            pubtrack.wechat,
            "get_conf",
            return_value={"appid": "app", "secret": "secret"},
        ), patch.object(
            pubtrack.wechat,
            "article_stats",
            new=AsyncMock(return_value={"read_user": 10}),
        ), patch.object(
            pubtrack.censor,
            "retro",
            retro,
        ), patch.object(pubtrack.notify, "push"):
            self.assertTrue(await pubtrack.auto_retro(2, self.row(), 1))
            self.assertTrue(await pubtrack.auto_retro(2, self.row(), 1))

        self.assertEqual(9, billing.balance(2))
        self.assertEqual(1, retro.await_count)
        self.assertEqual(
            [-1],
            [r["delta"] for r in db.q(
                "SELECT delta FROM billing_log WHERE tenant_id=2 ORDER BY id"
            )],
        )
        self.assertEqual(
            1,
            db.one("SELECT COUNT(*) n FROM censor_log WHERE tenant_id=2")["n"],
        )
        self.assertEqual(
            "done",
            db.jloads(self.row()["retro_json"], {})["1"]["state"],
        )

    async def test_startup_recovery_refunds_processing_anchor(self):
        op_key = billing.start_operation(
            "censor_retro", tid=2, op_key="pubretro:recovery"
        )
        row = self.row()
        retro = db.jloads(row["retro_json"], {})
        retro["1"].update({
            "state": "processing",
            "op_key": op_key,
            "started_at": time.time(),
        })
        db.update(
            "publish_log",
            self.pid,
            {"retro_json": json.dumps(retro, ensure_ascii=False)},
        )
        self.assertEqual(9, billing.balance(2))

        self.assertEqual(1, pubtrack.recover_interrupted())
        self.assertEqual(10, billing.balance(2))
        self.assertEqual(
            "refunded",
            db.one(
                "SELECT status FROM billing_operation WHERE op_key=?", (op_key,)
            )["status"],
        )
        self.assertEqual(
            "pending",
            db.jloads(self.row()["retro_json"], {})["1"]["state"],
        )

    async def test_reset_and_delete_cannot_destroy_live_billing_anchor(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_retro(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return {
                "report": "# 复盘",
                "grade": "良好",
                "signals": [],
                "actions": [],
                "cost_usd": 0.1,
                "tokens": 10,
            }

        with patch.object(
            pubtrack.wechat,
            "get_conf",
            return_value={"appid": "app", "secret": "secret"},
        ), patch.object(
            pubtrack.wechat,
            "article_stats",
            new=AsyncMock(return_value={"read_user": 10}),
        ), patch.object(
            pubtrack.censor,
            "retro",
            side_effect=delayed_retro,
        ), patch.object(pubtrack.notify, "push"):
            worker = asyncio.create_task(
                pubtrack.auto_retro(2, self.row(), 1)
            )
            await entered.wait()
            with self.assertRaises(pubtrack.RetroBusy):
                pubtrack.mark_published(2, self.pid)
            with self.assertRaises(pubtrack.RetroBusy):
                pubtrack.delete_entry(2, self.pid)
            release.set()
            self.assertTrue(await worker)

        self.assertEqual(9, billing.balance(2))
        self.assertEqual(
            "succeeded",
            db.one(
                "SELECT status FROM billing_operation "
                "WHERE action='censor_retro' ORDER BY created_at DESC LIMIT 1"
            )["status"],
        )
        self.assertEqual(
            "done",
            db.jloads(self.row()["retro_json"], {})["1"]["state"],
        )

    async def test_reset_during_stats_fetch_invalidates_stale_worker(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_stats(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return {"read_user": 10}

        retro = AsyncMock(return_value={
            "report": "# 不应生成",
            "grade": "良好",
            "signals": [],
            "actions": [],
        })
        original_published_at = self.row()["published_at"]
        with patch.object(
            pubtrack.wechat,
            "get_conf",
            return_value={"appid": "app", "secret": "secret"},
        ), patch.object(
            pubtrack.wechat,
            "article_stats",
            side_effect=delayed_stats,
        ), patch.object(pubtrack.censor, "retro", retro):
            worker = asyncio.create_task(
                pubtrack.auto_retro(2, self.row(), 1)
            )
            await entered.wait()
            pubtrack.mark_published(2, self.pid)
            release.set()
            self.assertFalse(await worker)

        self.assertGreater(self.row()["published_at"], original_published_at)
        self.assertEqual(10, billing.balance(2))
        retro.assert_not_awaited()
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) n FROM billing_operation")["n"],
        )

    async def test_tick_keeps_transient_api_failure_pending_with_backoff(self):
        self.make_due()
        stats = AsyncMock(side_effect=RuntimeError("wechat timeout"))
        fake_datetime = type(
            "NoonDateTime",
            (),
            {"now": staticmethod(lambda _tz: SimpleNamespace(hour=12))},
        )
        with patch.object(
            pubtrack, "datetime", fake_datetime
        ), patch.object(
            pubtrack.wechat,
            "get_conf",
            return_value={"appid": "app", "secret": "secret"},
        ), patch.object(
            pubtrack.wechat, "article_stats", stats
        ), patch.object(pubtrack.notify, "push") as pushed:
            await pubtrack.tick()
            await pubtrack.tick()

        state = db.jloads(self.row()["retro_json"], {})["1"]
        self.assertEqual("pending", state["state"])
        self.assertGreater(state["retry_after"], time.time())
        self.assertEqual(1, state["retry_attempts"])
        self.assertIn("临时失败", state["last_error"])
        self.assertEqual(1, stats.await_count)
        pushed.assert_not_called()
        self.assertEqual(10, billing.balance(2))

    async def test_tick_marks_no_data_notified_and_sends_human_fallback(self):
        self.make_due()
        fake_datetime = type(
            "NoonDateTime",
            (),
            {"now": staticmethod(lambda _tz: SimpleNamespace(hour=12))},
        )
        with patch.object(
            pubtrack, "datetime", fake_datetime
        ), patch.object(
            pubtrack.wechat, "get_conf", return_value={}
        ), patch.object(pubtrack.notify, "push") as pushed:
            await pubtrack.tick()
            await pubtrack.tick()

        state = db.jloads(self.row()["retro_json"], {})["1"]
        self.assertEqual("notified", state["state"])
        pushed.assert_called_once()
        self.assertEqual("retro_due", pushed.call_args.args[1])
        self.assertEqual(10, billing.balance(2))

    async def test_tenant_auto_retro_switch_pauses_tick_and_reminders(self):
        self.make_due()
        pubtrack.set_auto_enabled(2, False)
        self.assertFalse(pubtrack.auto_enabled(2))
        fake_datetime = type(
            "NoonDateTime",
            (),
            {"now": staticmethod(lambda _tz: SimpleNamespace(hour=12))},
        )
        worker = AsyncMock(return_value=pubtrack.RETRO_DONE)
        with patch.object(
            pubtrack, "datetime", fake_datetime
        ), patch.object(
            pubtrack, "_auto_retro_result", worker
        ), patch.object(pubtrack.notify, "push") as pushed:
            await pubtrack.tick()

        worker.assert_not_awaited()
        pushed.assert_not_called()
        self.assertEqual(
            "pending", db.jloads(self.row()["retro_json"], {})["1"]["state"]
        )
        self.assertEqual(10, billing.balance(2))

        # 重新打开后同一到期项立即恢复自动处理。
        pubtrack.set_auto_enabled(2, True)
        self.assertTrue(pubtrack.auto_enabled(2))
        with patch.object(
            pubtrack, "datetime", fake_datetime
        ), patch.object(
            pubtrack, "_auto_retro_result", worker
        ), patch.object(pubtrack.notify, "push"):
            await pubtrack.tick()
        worker.assert_awaited_once()

    async def test_stale_no_data_worker_cannot_notify_new_publication_cycle(self):
        self.make_due()
        original_published_at = self.row()["published_at"]
        fake_datetime = type(
            "NoonDateTime",
            (),
            {"now": staticmethod(lambda _tz: SimpleNamespace(hour=12))},
        )

        async def reset_then_report_no_data(_tid, _row, _day):
            pubtrack.mark_published(2, self.pid)
            return pubtrack.RETRO_NO_DATA

        with patch.object(
            pubtrack, "datetime", fake_datetime
        ), patch.object(
            pubtrack,
            "_auto_retro_result",
            side_effect=reset_then_report_no_data,
        ), patch.object(pubtrack.notify, "push") as pushed:
            await pubtrack.tick()

        current = self.row()
        self.assertGreater(current["published_at"], original_published_at)
        state = db.jloads(current["retro_json"], {})["1"]
        self.assertEqual("pending", state["state"])
        self.assertGreater(state["due"], time.time())
        pushed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
