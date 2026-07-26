import asyncio
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import HTTPException

from app import auth, billing, db
from app.engine import engine
from app.skills import registry


class ContentJobCancellationCase(unittest.TestCase):
    def setUp(self):
        from app import main

        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        self.old_root = main.ROOT
        main.ROOT = self.tmp.name
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "content-cancellation.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "付费租户", "balance": 20})
        auth.set_current({
            "id": 2,
            "tenant_id": 2,
            "username": "owner",
            "role": "owner",
            "modules": ["content"],
        })

    def tearDown(self):
        from app import main

        auth.set_current(None)
        engine.locks.clear()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        main.ROOT = self.old_root
        self.tmp.cleanup()

    def _charged_job(self, *, status="running", run_status=None, station_idx=0,
                     output=None):
        jid = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "取消竞态测试"}),
            "mode": "fullauto",
            "status": "pending_charge",
            "billing_status": "pending",
            "billing_points": 18,
        })

        def claim(c):
            cur = c.execute(
                "UPDATE job SET status=?,billing_status='charged' "
                "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
                (status, jid),
            )
            return cur.rowcount == 1

        self.assertTrue(billing.charge_if_claimed(
            "content_job", 2, claim, note="取消竞态测试", points=18))
        if run_status:
            db.insert("station_run", {
                "job_id": jid,
                "station_idx": station_idx,
                "status": run_status,
                "output_json": json.dumps(output, ensure_ascii=False)
                if output is not None else None,
            })
        return jid

    def _refund_count(self):
        return db.one(
            "SELECT COUNT(*) AS n FROM billing_log "
            "WHERE tenant_id=2 AND delta=18",
        )["n"]

    def _run_while(self, jid, callback):
        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = []

            async def delayed(_ctx):
                calls.append(1)
                entered.set()
                await release.wait()
                return {
                    "data": {"topics": [{"title": "不应落库的在途产出"}]},
                    "tokens": 99,
                    "cost_usd": 0.12,
                }

            cfg = registry.BY_IDX[0]
            original = cfg["run"]
            cfg["run"] = delayed
            task = asyncio.create_task(engine._advance_once(jid))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                callback()
                release.set()
                await asyncio.wait_for(task, timeout=2)
            finally:
                cfg["run"] = original
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            return len(calls)

        return asyncio.run(scenario())

    def test_provider_return_cannot_revive_cancelled_job_and_refund_is_idempotent(self):
        from app import main

        jid = self._charged_job()
        calls = self._run_while(
            jid, lambda: self.assertEqual({"ok": True}, main.cancel(jid)))

        self.assertEqual(1, calls)
        self.assertEqual(
            {"status": "cancelled", "billing_status": "refunded",
             "cost_usd": 0.0, "tokens": 0},
            db.one(
                "SELECT status,billing_status,cost_usd,tokens "
                "FROM job WHERE id=?",
                (jid,),
            ),
        )
        run = db.one(
            "SELECT status,output_json FROM station_run WHERE job_id=?",
            (jid,),
        )
        self.assertEqual("cancelled", run["status"])
        self.assertIsNone(run["output_json"])
        self.assertEqual(20, billing.balance(2))
        self.assertEqual(1, self._refund_count())

        self.assertEqual({"ok": True}, main.cancel(jid))
        self.assertEqual(20, billing.balance(2))
        self.assertEqual(1, self._refund_count())

    def test_concurrent_cancel_requests_refund_exactly_once(self):
        jid = self._charged_job(run_status="running")
        barrier = threading.Barrier(2)

        def settle():
            barrier.wait(timeout=2)
            return engine.settle_cancel(jid, "并发取消测试")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: settle(), range(2)))

        self.assertEqual([False, True], sorted(results))
        self.assertEqual(
            {"status": "cancelled", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM job WHERE id=?", (jid,)),
        )
        self.assertEqual(20, billing.balance(2))
        self.assertEqual(1, self._refund_count())

    def test_delete_settles_inflight_job_before_removal_and_worker_stops(self):
        from app import main

        jid = self._charged_job()
        calls = self._run_while(
            jid,
            lambda: self.assertTrue(
                main.delete_job(jid).get("soft_deleted")
            ),
        )

        self.assertEqual(1, calls)
        hidden = db.one(
            "SELECT id,deleted_at FROM job WHERE id=?", (jid,)
        )
        self.assertEqual(jid, hidden["id"])
        self.assertIsNotNone(hidden["deleted_at"])
        self.assertIsNotNone(
            db.one("SELECT id FROM station_run WHERE job_id=?", (jid,)))
        self.assertEqual(20, billing.balance(2))
        self.assertEqual(1, self._refund_count())

    def test_cancel_after_usable_body_keeps_charge_and_is_still_idempotent(self):
        from app import main

        jid = self._charged_job(
            run_status="done",
            station_idx=3,
            output={"body": "已经形成可用正文"},
        )
        self.assertEqual({"ok": True}, main.cancel(jid))
        self.assertEqual({"ok": True}, main.cancel(jid))

        self.assertEqual(
            {"status": "cancelled", "billing_status": "charged"},
            db.one(
                "SELECT status,billing_status FROM job WHERE id=?",
                (jid,),
            ),
        )
        self.assertEqual(2, billing.balance(2))
        self.assertEqual(0, self._refund_count())

    def test_unreviewed_provider_body_is_discarded_when_full_refund_wins(self):
        from app import main

        jid = self._charged_job()

        async def produce(_ctx):
            return {
                "data": {"body": "尚未进入交付态的正文"},
                "tokens": 88,
                "cost_usd": 0.08,
            }

        self.assertTrue(asyncio.run(engine._execute(
            jid,
            3,
            {
                "skill": "writer",
                "model": "api",
                "name": "撰稿师",
                "run": produce,
            },
            {"direction": "测试"},
            {},
            1,
            None,
            None,
        )))
        self.assertIsNotNone(db.one(
            "SELECT output_json FROM station_run WHERE job_id=?", (jid,)
        )["output_json"])

        self.assertEqual({"ok": True}, main.cancel(jid))
        self.assertEqual(
            {"status": "cancelled", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM job WHERE id=?", (jid,)),
        )
        run = db.one(
            "SELECT status,output_json FROM station_run WHERE job_id=?", (jid,))
        self.assertEqual("cancelled", run["status"])
        self.assertIsNone(run["output_json"])
        self.assertEqual(20, billing.balance(2))
        self.assertEqual(1, self._refund_count())

    def test_done_job_cannot_be_cancelled_or_refunded(self):
        from app import main

        jid = self._charged_job(
            status="done",
            run_status="done",
            station_idx=3,
            output={"body": "成功交付"},
        )
        with self.assertRaises(HTTPException) as caught:
            main.cancel(jid)
        self.assertEqual(400, caught.exception.status_code)
        self.assertIn("已完成", str(caught.exception.detail))
        self.assertEqual(
            {"status": "done", "billing_status": "charged"},
            db.one(
                "SELECT status,billing_status FROM job WHERE id=?",
                (jid,),
            ),
        )
        self.assertEqual(2, billing.balance(2))
        self.assertEqual(0, self._refund_count())

    def test_done_job_delete_does_not_refund_successful_delivery(self):
        from app import main

        jid = self._charged_job(
            status="done",
            run_status="done",
            station_idx=3,
            output={"body": "成功交付"},
        )
        self.assertTrue(main.delete_job(jid).get("soft_deleted"))
        hidden = db.one(
            "SELECT id,deleted_at FROM job WHERE id=?", (jid,)
        )
        self.assertEqual(jid, hidden["id"])
        self.assertIsNotNone(hidden["deleted_at"])
        self.assertEqual(2, billing.balance(2))
        self.assertEqual(0, self._refund_count())

    def test_delete_keeps_recovery_anchor_when_refund_transaction_fails(self):
        from app import main

        jid = self._charged_job(run_status="running")
        with patch(
                "app.engine.billing.refund_amount_if_claimed",
                side_effect=RuntimeError("wallet unavailable")):
            with self.assertRaises(RuntimeError):
                main.delete_job(jid)

        self.assertEqual(
            {"status": "running", "billing_status": "charged"},
            db.one(
                "SELECT status,billing_status FROM job WHERE id=?", (jid,)),
        )
        self.assertIsNotNone(
            db.one("SELECT id FROM station_run WHERE job_id=?", (jid,)))
        self.assertEqual(2, billing.balance(2))
        self.assertEqual(0, self._refund_count())

    def test_station_actions_reject_terminal_or_refunded_jobs(self):
        from app import main

        cases = (
            ("cancelled", "refunded"),
            ("failed", "refunded"),
            ("failed", "charged"),
            ("done", "charged"),
        )
        for job_status, billing_status in cases:
            for action in ("approve", "reject", "rerun"):
                with self.subTest(
                        job_status=job_status,
                        billing_status=billing_status,
                        action=action):
                    db.q("DELETE FROM station_run")
                    db.q("DELETE FROM job")
                    db.q("DELETE FROM billing_log")
                    db.q("UPDATE tenants SET balance=20 WHERE id=2")
                    jid = self._charged_job(
                        status=job_status,
                        run_status="awaiting_review",
                        output={"body": "旧产出"},
                    )
                    db.q(
                        "UPDATE job SET billing_status=? WHERE id=?",
                        (billing_status, jid),
                    )
                    with self.assertRaises(HTTPException) as caught:
                        main.station_action(jid, 0, {
                            "action": action,
                            "payload": {"comment": "请修改"},
                        })
                    self.assertEqual(400, caught.exception.status_code)
                    self.assertIn("新建工单", str(caught.exception.detail))
                    self.assertEqual(
                        job_status,
                        db.one("SELECT status FROM job WHERE id=?", (jid,))[
                            "status"],
                    )
                    self.assertEqual(
                        "awaiting_review",
                        db.one(
                            "SELECT status FROM station_run WHERE job_id=?",
                            (jid,),
                        )["status"],
                    )

    def test_active_charged_job_can_still_approve_and_rerun_without_rebilling(self):
        from app import main

        jid = self._charged_job(
            status="awaiting_review",
            run_status="awaiting_review",
            output={"topics": [{"title": "保留的选题"}]},
        )
        self.assertEqual(
            {"ok": True},
            main.station_action(jid, 0, {
                "action": "approve",
                "payload": {"selected": 0},
            }),
        )
        self.assertEqual(
            {"status": "running", "billing_status": "charged"},
            db.one(
                "SELECT status,billing_status FROM job WHERE id=?", (jid,)),
        )
        self.assertEqual(
            "done",
            db.one(
                "SELECT status FROM station_run WHERE job_id=?", (jid,))[
                "status"],
        )

        self.assertEqual(
            {"ok": True},
            main.station_action(jid, 0, {
                "action": "rerun",
                "payload": {"comment": "换一个角度"},
            }),
        )
        self.assertEqual(
            "rejected",
            db.one(
                "SELECT status FROM station_run WHERE job_id=?", (jid,))[
                "status"],
        )
        self.assertEqual(2, billing.balance(2))
        self.assertEqual(0, self._refund_count())

    def test_guarded_completion_still_creates_tenant_owned_delivery_records(self):
        jid = self._charged_job()
        for idx in range(10):
            db.insert("station_run", {
                "job_id": jid,
                "station_idx": idx,
                "status": "done",
                "output_json": json.dumps(
                    {"report": "复盘完成"} if idx == 9 else {},
                    ensure_ascii=False,
                ),
            })

        self.assertTrue(asyncio.run(engine._advance_once(jid)))
        self.assertEqual(
            {"status": "done", "billing_status": "charged"},
            db.one(
                "SELECT status,billing_status FROM job WHERE id=?", (jid,)),
        )
        self.assertEqual(
            {"tenant_id": 2, "type": "final"},
            db.one(
                "SELECT tenant_id,type FROM asset "
                "WHERE job_id=? AND type='final'",
                (jid,),
            ),
        )
        self.assertEqual(
            {"tenant_id": 2, "source": "auto"},
            db.one(
                "SELECT tenant_id,source FROM knowledge WHERE job_id=?",
                (jid,),
            ),
        )

    def test_guarded_provider_transition_still_reaches_awaiting_review(self):
        jid = self._charged_job()
        db.q("UPDATE job SET mode='manual' WHERE id=?", (jid,))

        async def produce(_ctx):
            return {
                "data": {"topics": [{"title": "待审批选题"}]},
                "tokens": 12,
                "cost_usd": 0.01,
            }

        cfg = registry.BY_IDX[0]
        original = cfg["run"]
        cfg["run"] = produce
        try:
            self.assertTrue(asyncio.run(engine._advance_once(jid)))
        finally:
            cfg["run"] = original

        self.assertEqual(
            {"status": "awaiting_review", "billing_status": "charged"},
            db.one(
                "SELECT status,billing_status FROM job WHERE id=?", (jid,)),
        )
        run = db.one(
            "SELECT status,output_json FROM station_run WHERE job_id=?", (jid,))
        self.assertEqual("awaiting_review", run["status"])
        self.assertIn("待审批选题", run["output_json"])


if __name__ == "__main__":
    unittest.main()
