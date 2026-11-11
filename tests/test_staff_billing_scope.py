"""成员账号(店员/店长/总监)不看企业的账;/api/auth/me 告诉前端该不该回手机版。"""
from __future__ import annotations

import time
import unittest

from fastapi import HTTPException
from starlette.requests import Request

from app import auth, db
from tests.test_phase2_stafftask import StaffTaskBase

FULL_ONLY_KEYS = ("log", "monthly", "plans", "periods", "recharged", "spent",
                  "refunded_total", "txn_n", "spend_by_action", "point_examples",
                  "plan", "plan_expires")


def _page_request(path: str, query: bytes = b"") -> Request:
    return Request({"type": "http", "method": "GET", "path": path,
                    "query_string": query, "headers": [],
                    "client": ("198.51.100.7", 1234)})


class StaffBillingScopeTests(StaffTaskBase):
    def setUp(self):
        super().setUp()
        now = time.time()
        for delta, balance, reason in ((500, 500, "套餐入账"), (-18, 482, "内容工单")):
            db.execute(
                "INSERT INTO billing_log(tenant_id,delta,balance,reason,created_at) "
                "VALUES(2,?,?,?,?)", (delta, balance, reason, now))
        db.execute("UPDATE tenants SET balance=482 WHERE id=2")

    def tearDown(self):
        auth.set_current(None)
        super().tearDown()

    def _as(self, uid):
        auth.set_current(auth.get_user(uid))

    def test_owner_still_gets_ledger_and_plans(self):
        from app import main

        self._as(20)
        data = main.billing_get()
        self.assertEqual(482, data["balance"])
        self.assertEqual(2, len(data["log"]))
        self.assertEqual(500, data["recharged"])
        self.assertEqual(18, data["spent"])
        self.assertTrue(data["monthly"])
        self.assertTrue(data["plans"])
        self.assertTrue(data["periods"])
        self.assertNotIn("restricted", data)

    def test_members_get_balance_and_action_cost_only(self):
        from app import main

        for uid in (21, 22, 23, 27):   # 总监 / 店长 / 店员 / 内容运营副账号
            with self.subTest(uid=uid):
                self._as(uid)
                data = main.billing_get()
                self.assertEqual(482, data["balance"])
                self.assertTrue(data["restricted"])
                for key in FULL_ONLY_KEYS:
                    self.assertNotIn(key, data)
                self.assertTrue(data["prices"])
                for row in data["prices"].values():
                    self.assertLessEqual(set(row), {"points", "label"})
                self.assertNotIn("套餐入账", repr(data))

    def test_members_cannot_export_ledger_or_list_purchases(self):
        from app import main

        self._as(23)
        with self.assertRaises(HTTPException) as caught:
            main.records_export("billing")
        self.assertEqual(403, caught.exception.status_code)
        with self.assertRaises(HTTPException) as caught:
            main.purchase_list()
        self.assertEqual(403, caught.exception.status_code)

    def test_platform_root_keeps_everything(self):
        from app import main

        db.insert("tenants", {"id": 1, "name": "平台", "industries_json": "[]"})
        db.insert("users", {"id": 1, "tenant_id": 1, "username": "platform-root",
                            "password_hash": "x", "role": "root",
                            "modules_json": "[]", "enabled": 1})
        self._as(1)
        data = main.billing_get()
        for key in ("log", "monthly", "plans", "periods", "prices"):
            self.assertIn(key, data)

    def test_auth_me_reports_prefers_staff_home(self):
        from app import main

        expected = {20: False, 21: False, 22: True, 23: True, 27: False}
        for uid, want in expected.items():
            with self.subTest(uid=uid):
                self._as(uid)
                self.assertIs(want, main.auth_me()["prefers_staff_home"])

    def test_staff_lands_on_phone_app_and_full_version_stays_reachable(self):
        from app import main

        self._as(23)
        plain = main.index(_page_request("/"))
        self.assertEqual(302, plain.status_code)
        self.assertEqual("/staff", plain.headers["location"])
        self.assertEqual(200, main.index(_page_request("/", b"full=1")).status_code)
        # /staff 自己不再跳转,改完密码回来不会转圈
        self.assertEqual(200, main.staff_page().status_code)
        self._as(20)
        self.assertEqual(200, main.index(_page_request("/")).status_code)


if __name__ == "__main__":
    unittest.main()
