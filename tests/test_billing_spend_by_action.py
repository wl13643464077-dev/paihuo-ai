"""账单页「近30天花在哪」聚合:租户隔离、状态过滤、30天窗口."""
import os
import tempfile
import time
import unittest

from app import auth, db


class BillingSpendByActionCase(unittest.TestCase):
    """/api/billing 的 spend_by_action 只统计本租户、近30天、实际收钱的操作."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "spend.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业甲", "balance": 100})
        db.insert("tenants", {"id": 3, "name": "企业乙", "balance": 100})
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

    def _op(self, key, tenant_id, action, points, status, age_seconds=0):
        now = time.time()
        db.insert("billing_operation", {
            "op_key": key,
            "tenant_id": tenant_id,
            "action": action,
            "units": 1,
            "points": points,
            "status": status,
            "created_at": now - age_seconds,
            "updated_at": now - age_seconds,
        })

    def test_aggregates_only_paid_statuses_of_own_tenant_within_30_days(self):
        # 本租户,实际收钱:charged(已扣费在跑)与 succeeded(已交付)都要计入
        self._op("a1", 2, "content_job", 18, "succeeded")
        self._op("a2", 2, "content_job", 18, "succeeded")
        self._op("a3", 2, "learn", 3, "charged")
        # 本租户,但未成交/已退回:pending 未扣费、refunded 已退款,均不计
        self._op("a4", 2, "avatar_video", 12, "refunded")
        self._op("a5", 2, "avatar_video", 12, "pending")
        # 本租户,超出30天窗口:不计
        self._op("a6", 2, "content_job", 18, "succeeded",
                 age_seconds=31 * 86400)
        # 其他租户:绝不能串进来
        self._op("b1", 3, "voice_clone", 9, "succeeded")
        self._op("b2", 3, "content_job", 18, "charged")

        from app import main
        resp = main.billing_get()
        spend = resp["spend_by_action"]

        # 只剩本租户近30天已收钱的两类动作,且按消耗从高到低
        self.assertEqual(
            [{"action": "content_job", "n": 2, "points": 36},
             {"action": "learn", "n": 1, "points": 3}],
            [{"action": r["action"], "n": r["n"], "points": r["points"]}
             for r in spend])
        # 退款/待扣费动作与他租户动作都不应出现
        actions = {r["action"] for r in spend}
        self.assertNotIn("avatar_video", actions)
        self.assertNotIn("voice_clone", actions)

    def test_empty_when_no_paid_operations(self):
        # 只有退款与 pending 记录时,聚合应为空(前端据此不渲染整张卡)
        self._op("c1", 2, "learn", 3, "refunded")
        self._op("c2", 2, "learn", 3, "pending")

        from app import main
        resp = main.billing_get()
        self.assertEqual([], resp["spend_by_action"])

    def test_window_boundary_keeps_recent_and_drops_old(self):
        # 29 天前的计入,31 天前的不计
        self._op("d1", 2, "learn", 3, "succeeded", age_seconds=29 * 86400)
        self._op("d2", 2, "learn", 3, "succeeded", age_seconds=31 * 86400)

        from app import main
        resp = main.billing_get()
        spend = resp["spend_by_action"]
        self.assertEqual(1, len(spend))
        self.assertEqual("learn", spend[0]["action"])
        self.assertEqual(1, spend[0]["n"])
        self.assertEqual(3, spend[0]["points"])


if __name__ == "__main__":
    unittest.main()
