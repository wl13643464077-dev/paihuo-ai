"""账单页「近30天花在哪」聚合:必须以 billing_log 流水为准。

对抗性审查发现:核心扣点路径(内容工单/专家任务/会议/成片/工具/定时)
走 charge_if_claimed,只写流水不写 billing_operation;从后者聚合会把
老板最大头的花销全部漏掉。本组测试用【真实扣款路径】产生数据自证。
"""
import os
import tempfile
import time
import unittest

from app import auth, billing, db


class BillingSpendByActionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "spend.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业甲", "balance": 500})
        db.insert("tenants", {"id": 3, "name": "企业乙", "balance": 500})
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

    def _charge(self, tid, action, note=""):
        self.assertTrue(billing.charge_if_claimed(
            action, tid, lambda c: True, note=note))

    def test_charge_if_claimed_spend_is_visible_and_refund_offsets(self):
        # 真实主路径:charge_if_claimed 两笔内容工单 + 一笔专家任务
        self._charge(2, "content_job", note="工单#5·门店开业")
        self._charge(2, "content_job", note="工单#6·周年庆")
        self._charge(2, "expert_task", note="任务#9·菜单定价")
        # 其中一单失败退回:必须按 label 冲抵
        self.assertTrue(billing.refund_if_claimed(
            "content_job", 2, lambda c: True, note="工单#6·周年庆"))
        # 其他租户的消耗绝不能串进来
        self._charge(3, "content_job", note="别家的单")

        from app import main
        spend = main.billing_get()["spend_by_action"]
        as_map = {r["action"]: r for r in spend}
        self.assertIn("content_job", as_map, "主路径消耗必须出现在聚合里")
        self.assertEqual(18, as_map["content_job"]["points"],
                         "两笔 36 点冲抵一笔退款 18 点后净 18")
        self.assertEqual(1, as_map["content_job"]["n"])
        self.assertEqual(1, as_map["expert_task"]["points"])
        # 排序按净消耗从高到低
        self.assertEqual("content_job", spend[0]["action"])

    def test_fully_refunded_action_disappears(self):
        self._charge(2, "expert_task")
        self.assertTrue(billing.refund_if_claimed(
            "expert_task", 2, lambda c: True))
        from app import main
        self.assertEqual([], main.billing_get()["spend_by_action"])

    def test_window_boundary_keeps_recent_and_drops_old(self):
        now = time.time()
        label = billing.prices()["expert_task"]["label"]
        for age, note in ((29 * 86400, "计入"), (31 * 86400, "剔除")):
            db.insert("billing_log", {
                "tenant_id": 2, "delta": -1, "balance": 499,
                "reason": f"{label} · {note}",
                "created_at": now - age, "updated_at": now - age,
            })
        from app import main
        spend = main.billing_get()["spend_by_action"]
        self.assertEqual(1, len(spend))
        self.assertEqual("expert_task", spend[0]["action"])
        self.assertEqual(1, spend[0]["points"])


if __name__ == "__main__":
    unittest.main()
