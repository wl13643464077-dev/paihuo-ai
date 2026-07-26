import asyncio
import json
import os
import tempfile
import unittest

from app import auth, billing, db, providers
from app.engine import MAX_RETRY, engine


class ContentJobSettlementCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "content-settlement.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "付费租户", "balance": 20})
        auth.set_current({
            "id": 2, "tenant_id": 2, "username": "owner",
            "role": "owner", "modules": ["content"],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _pending_job(self):
        return db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "测试内容"}),
            "status": "pending_charge",
            "billing_status": "pending",
            "billing_points": 18,
        })

    def _charged_job(self, usable=False):
        jid = self._pending_job()

        def claim(c):
            cur = c.execute(
                "UPDATE job SET billing_status='charged', status='running' "
                "WHERE id=? AND billing_status='pending'",
                (jid,),
            )
            return cur.rowcount == 1

        self.assertTrue(
            billing.charge_if_claimed(
                "content_job", 2, claim, note="测试", points=18,
            )
        )
        if usable:
            db.insert("station_run", {
                "job_id": jid,
                "station_idx": 3,
                "status": "done",
                "output_json": json.dumps({"body": "已经形成可用正文"}),
            })
        else:
            db.insert("station_run", {
                "job_id": jid,
                "station_idx": 1,
                "status": "running",
            })
        return jid

    def test_pending_job_activation_debits_and_logs_in_one_transaction(self):
        jid = self._pending_job()

        def claim(c):
            cur = c.execute(
                "UPDATE job SET billing_status='charged', status='running' "
                "WHERE id=? AND billing_status='pending'",
                (jid,),
            )
            return cur.rowcount == 1

        self.assertTrue(
            billing.charge_if_claimed(
                "content_job", 2, claim, note="原子开工", points=18,
            )
        )
        self.assertEqual(2, billing.balance(2))
        self.assertEqual("charged", db.one("SELECT billing_status FROM job WHERE id=?", (jid,))[
            "billing_status"])
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_log WHERE tenant_id=2 AND delta=-18")["n"])

        self.assertFalse(
            billing.charge_if_claimed(
                "content_job", 2, claim, note="重复请求", points=18,
            )
        )
        self.assertEqual(2, billing.balance(2))

    def test_unusable_failure_refunds_exactly_once_and_closes_running_station(self):
        jid = self._charged_job(usable=False)
        self.assertTrue(engine.settle_failure(jid, "供应商失败"))
        self.assertFalse(engine.settle_failure(jid, "看门狗重复处理"))

        self.assertEqual(20, billing.balance(2))
        job = db.one("SELECT status,billing_status FROM job WHERE id=?", (jid,))
        self.assertEqual({"status": "failed", "billing_status": "refunded"}, job)
        self.assertEqual("failed", db.one(
            "SELECT status FROM station_run WHERE job_id=?", (jid,))["status"])
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_log WHERE tenant_id=2 AND delta=18")["n"])

    def test_failure_after_usable_draft_closes_without_full_refund(self):
        jid = self._charged_job(usable=True)
        self.assertFalse(engine.settle_failure(jid, "发布辅助失败"))

        self.assertEqual(2, billing.balance(2))
        job = db.one("SELECT status,billing_status FROM job WHERE id=?", (jid,))
        self.assertEqual({"status": "failed", "billing_status": "charged"}, job)
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_log WHERE tenant_id=2 AND delta=18")["n"])

    def test_provider_error_is_retried_then_station_and_job_reach_terminal_state(self):
        jid = self._charged_job(usable=False)
        db.q("DELETE FROM station_run WHERE job_id=?", (jid,))
        attempts = []

        async def fail(_ctx):
            attempts.append(1)
            raise providers.ProviderError("gateway unavailable")

        result = asyncio.run(engine._execute(
            jid,
            0,
            {"skill": "trend", "model": "api", "name": "趋势官", "run": fail},
            {"direction": "测试"},
            {},
            1,
            None,
            None,
        ))
        self.assertFalse(result)
        self.assertEqual(MAX_RETRY + 1, len(attempts))
        self.assertEqual(
            "failed",
            db.one("SELECT status FROM station_run WHERE job_id=?", (jid,))["status"],
        )

        engine.settle_failure(jid, "供应商重试后仍失败")
        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one("SELECT status,billing_status FROM job WHERE id=?", (jid,)),
        )


if __name__ == "__main__":
    unittest.main()
