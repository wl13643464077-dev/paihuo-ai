"""营销工具后台任务必须在成功、失败、超时和重启后都能可靠收口。"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import auth, billing, db, llm, main


class ToolWorkerReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "tool-worker.db")
        db.conn()
        main._ensure_tool_running_index()
        db.insert("tenants", {"id": 2, "name": "付费租户", "balance": 10})

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _job(self, *, tid=2, kind="leads", at=None, params=None):
        at = time.time() if at is None else at
        points = float((billing.prices().get(
            main.TOOL_REFUND.get(kind, "expert_task")
        ) or {"points": 1})["points"])
        return db.insert("tool_job", {
            "tenant_id": tid, "kind": kind,
            "params_json": json.dumps(params or {"industry": "企业服务"}),
            "status": "running", "billing_status": "charged",
            "billing_points": points, "created_at": at, "updated_at": at,
        })

    async def test_failure_settles_before_broken_logger_and_refunds_once(self):
        jid = self._job()
        secret = "INTERNAL-MANUAL-SECRET echoed by provider"
        with patch.object(main, "_run_tool",
                          AsyncMock(side_effect=llm.LLMError(secret))), \
                patch.object(main.log, "exception", side_effect=RuntimeError("日志也坏了")), \
                patch.object(main.engine, "broadcast") as broadcast:
            await main._tool_worker(jid)

        row = db.one("SELECT * FROM tool_job WHERE id=?", (jid,))
        self.assertEqual(row["status"], "failed")
        self.assertNotIn(secret, row["error"])
        self.assertIn("免费重试", row["error"])
        self.assertEqual(db.one("SELECT balance FROM tenants WHERE id=2")["balance"], 13)
        self.assertEqual(
            db.one("SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2")["n"], 1
        )
        broadcast.assert_called()

        # worker、看门狗或重启恢复再次撞上同一条记录时，不得重复退款。
        self.assertFalse(main._fail_tool_job(row, "重复失败"))
        self.assertEqual(db.one("SELECT balance FROM tenants WHERE id=2")["balance"], 13)

    async def test_notification_and_broadcast_failure_do_not_revert_done(self):
        jid = self._job()
        with patch.object(main, "_run_tool",
                          AsyncMock(return_value={"leads": [{"source_url": "https://a.example/p/1"}]})), \
                patch.object(main.notify, "push", side_effect=RuntimeError("通知不可用")), \
                patch.object(main.engine, "broadcast", side_effect=RuntimeError("SSE不可用")):
            await main._tool_worker(jid)

        row = db.one("SELECT * FROM tool_job WHERE id=?", (jid,))
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["billing_status"], "succeeded")
        self.assertEqual(row["progress"], "任务已完成")
        self.assertEqual(db.one("SELECT balance FROM tenants WHERE id=2")["balance"], 10)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM billing_log")["n"], 0)

    async def test_verified_leads_degraded_analysis_still_finishes_done(self):
        """分析降级不应丢掉已核验原帖或误触发退款。"""
        jid = self._job()
        result = {
            "analysis_status": "degraded",
            "leads": [{
                "source_id": "S1",
                "source_url": "https://www.zhihu.com/question/123",
                "intent": "低",
            }],
        }
        with patch.object(main, "_run_tool", AsyncMock(return_value=result)), \
                patch.object(main.notify, "push"), \
                patch.object(main.engine, "broadcast"):
            await main._tool_worker(jid)

        row = db.one("SELECT * FROM tool_job WHERE id=?", (jid,))
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["billing_status"], "succeeded")
        self.assertEqual(json.loads(row["result_json"])["analysis_status"], "degraded")
        self.assertEqual(db.one("SELECT balance FROM tenants WHERE id=2")["balance"], 10)

    async def test_timeout_cancels_work_and_finishes_as_failed(self):
        jid = self._job()
        cancelled = asyncio.Event()

        async def never_finishes(*_args):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch.object(main, "_run_tool", side_effect=never_finishes), \
                patch.dict(main.TOOL_TIMEOUTS, {"leads": 0.01}):
            await main._tool_worker(jid)

        self.assertTrue(cancelled.is_set())
        row = db.one("SELECT * FROM tool_job WHERE id=?", (jid,))
        self.assertEqual(row["status"], "failed")
        self.assertIn("运行超过", row["error"])
        self.assertEqual(db.one("SELECT balance FROM tenants WHERE id=2")["balance"], 13)

    async def test_refund_error_rolls_back_status_for_watchdog_retry(self):
        jid = self._job(tid=999)
        row = db.one("SELECT * FROM tool_job WHERE id=?", (jid,))
        with self.assertRaisesRegex(RuntimeError, "退款租户不存在"):
            main._fail_tool_job(row, "模型失败")
        self.assertEqual(
            db.one("SELECT status FROM tool_job WHERE id=?", (jid,))["status"], "running"
        )

    async def test_watchdog_only_recovers_stale_running_jobs(self):
        db.insert("tenants", {"id": 3, "name": "另一个租户", "balance": 20})
        stale = self._job(tid=2, at=1)
        fresh = self._job(tid=3, at=900)
        recovered = main._recover_stale_tool_jobs(now=1000)
        self.assertEqual(recovered, 1)
        self.assertEqual(db.one("SELECT status FROM tool_job WHERE id=?", (stale,))["status"],
                         "failed")
        self.assertEqual(db.one("SELECT status FROM tool_job WHERE id=?", (fresh,))["status"],
                         "running")

    async def test_concurrent_loser_is_never_charged_when_active_index_rejects_insert(self):
        auth.set_current({"id": 22, "tenant_id": 2, "role": "owner",
                          "modules": ["content"]})
        with patch.object(main, "_spawn_tool_worker"):
            first = main._tool_enqueue(
                "leads", {"industry": "企业服务"}, note="第一次请求"
            )
        self.assertEqual(db.one("SELECT balance FROM tenants WHERE id=2")["balance"], 7)

        with patch.object(main, "_spawn_tool_worker"):
            with self.assertRaises(HTTPException) as denied:
                main._tool_enqueue(
                    "leads", {"industry": "企业服务"}, note="并发重复请求"
                )

        self.assertEqual(denied.exception.status_code, 429)
        self.assertGreater(first["job_id"], 0)
        self.assertEqual(db.one("SELECT balance FROM tenants WHERE id=2")["balance"], 7)
        self.assertEqual(
            db.one("SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2")["n"], 1
        )

    async def test_insert_failure_never_charges_points(self):
        auth.set_current({"id": 22, "tenant_id": 2, "role": "owner",
                          "modules": ["content"]})
        with patch.object(main.db, "insert", side_effect=RuntimeError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                main._tool_enqueue("leads", {"industry": "企业服务"})
        self.assertEqual(10, billing.balance(2))
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2")["n"],
        )

    async def test_failure_refunds_frozen_amount_after_admin_reprices_tool(self):
        auth.set_current({"id": 22, "tenant_id": 2, "role": "owner",
                          "modules": ["content"]})
        with patch.object(main, "_spawn_tool_worker"):
            created = main._tool_enqueue("leads", {"industry": "企业服务"})
        self.assertEqual(7, billing.balance(2))
        prices = dict(billing.DEFAULT_PRICES)
        prices["leads"] = {"points": 99, "label": "临时新价格"}
        db.set_setting("prices", json.dumps(prices, ensure_ascii=False))

        row = db.one("SELECT * FROM tool_job WHERE id=?", (created["job_id"],))
        self.assertTrue(main._fail_tool_job(row, "模型失败"))
        self.assertEqual(10, billing.balance(2))
        self.assertEqual(
            3,
            db.one(
                "SELECT delta FROM billing_log WHERE tenant_id=2 "
                "AND delta>0 ORDER BY id DESC LIMIT 1"
            )["delta"],
        )

    async def test_calendar_cache_and_done_status_commit_together(self):
        billing.charge("pcal", tid=2)
        jid = self._job(
            kind="pcal",
            params={"ym": "2026-07", "industry": "企业服务", "focus": ""},
        )
        result = {"days": [{"d": 1, "moment": "内容"}], "tips": "建议"}
        with patch.object(main, "_run_tool", AsyncMock(return_value=result)), \
                patch.object(main.notify, "push"), \
                patch.object(main.engine, "broadcast"):
            await main._tool_worker(jid)

        self.assertEqual(
            {"status": "done", "billing_status": "succeeded"},
            db.one(
                "SELECT status,billing_status FROM tool_job WHERE id=?", (jid,)
            ),
        )
        self.assertEqual(
            result,
            json.loads(db.get_setting("pcal:2:2026-07")),
        )
        self.assertEqual(8, billing.balance(2))

    async def test_calendar_cache_write_failure_rolls_back_done_and_refunds(self):
        billing.charge("pcal", tid=2)
        jid = self._job(
            kind="pcal",
            params={"ym": "2026-07", "industry": "企业服务", "focus": ""},
        )
        db.execute(
            "CREATE TRIGGER reject_pcal_cache BEFORE INSERT ON app_setting "
            "BEGIN SELECT RAISE(ABORT,'cache disk full'); END"
        )
        with patch.object(
                main, "_run_tool",
                AsyncMock(return_value={"days": [{"d": 1}], "tips": "建议"})):
            await main._tool_worker(jid)

        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM tool_job WHERE id=?", (jid,)
            ),
        )
        self.assertIsNone(db.get_setting("pcal:2:2026-07"))
        self.assertEqual(10, billing.balance(2))

    async def test_manual_benchmark_updates_last_run_only_after_success(self):
        db.set_setting(
            "bench_watch:2",
            json.dumps({"targets": [{"name": "对标A"}], "enabled": True}),
        )
        billing.charge("bench_watch", tid=2)
        failed = self._job(kind="bench", params={})
        db.execute(
            "CREATE TRIGGER reject_bench_cache BEFORE UPDATE ON app_setting "
            "BEGIN SELECT RAISE(ABORT,'cache disk full'); END"
        )
        with patch.object(
                main, "_run_tool",
                AsyncMock(return_value={"summary": "周报", "items": []})):
            await main._tool_worker(failed)
        self.assertNotIn("last_run", json.loads(db.get_setting("bench_watch:2")))
        self.assertEqual(10, billing.balance(2))

        db.execute("DROP TRIGGER reject_bench_cache")
        billing.charge("bench_watch", tid=2)
        succeeded = self._job(kind="bench", params={})
        with patch.object(
                main, "_run_tool",
                AsyncMock(return_value={"summary": "周报", "items": []})), \
                patch.object(main.notify, "push"):
            await main._tool_worker(succeeded)
        self.assertGreater(
            json.loads(db.get_setting("bench_watch:2"))["last_run"],
            0,
        )
        self.assertEqual(7, billing.balance(2))

    async def test_startup_recovers_legacy_duplicate_running_before_unique_index(self):
        db.execute("DROP INDEX idx_tool_job_one_active")
        billing.charge("leads", tid=2, note="旧并发任务一")
        billing.charge("leads", tid=2, note="旧并发任务二")
        first = self._job(tid=2)
        second = self._job(tid=2)

        main._recover_interrupted_tool_jobs()
        main._ensure_tool_running_index()

        self.assertEqual(
            [db.one("SELECT status FROM tool_job WHERE id=?", (jid,))["status"]
             for jid in (first, second)],
            ["failed", "failed"],
        )
        self.assertEqual(db.one("SELECT balance FROM tenants WHERE id=2")["balance"], 10)
        self.assertTrue(db.one(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_tool_job_one_active'"
        ))


class LlmCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_outer_cancellation_kills_cli_and_clears_registry(self):
        class Input:
            def write(self, _data):
                return None

            async def drain(self):
                return None

            def close(self):
                return None

        class Output:
            async def readline(self):
                await asyncio.Event().wait()

            async def read(self, _size):
                await asyncio.Event().wait()

        class Proc:
            def __init__(self):
                self.stdin, self.stdout, self.stderr = Input(), Output(), Output()
                self.returncode = None
                self.killed = False

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                return self.returncode

        proc = Proc()
        with patch.object(llm, "CLAUDE", sys.executable), \
                patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            task = asyncio.create_task(llm.call(
                "检索", web=True, token="tool-cancel",
                provider_env={"ANTHROPIC_BASE_URL": "https://proxy.example",
                              "ANTHROPIC_AUTH_TOKEN": "key"},
            ))
            for _ in range(10):
                if "tool-cancel" in llm.RUNNING:
                    break
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(proc.killed)
        self.assertNotIn("tool-cancel", llm.RUNNING)


if __name__ == "__main__":
    unittest.main()
