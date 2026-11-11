"""第四轮 N-01/N-02/N-03/N-10:成员看不到企业余额;只看到、只能选自己真能用的 AI 专家。"""
from __future__ import annotations

import asyncio
import json
import unittest

from fastapi import HTTPException

from app import auth, billing, db, departments
from tests.test_phase2_stafftask import StaffTaskBase


class MemberBalanceTests(StaffTaskBase):
    def setUp(self):
        super().setUp()
        db.execute("UPDATE tenants SET balance=482 WHERE id=2")

    def tearDown(self):
        auth.set_current(None)
        super().tearDown()

    def _as(self, uid):
        auth.set_current(auth.get_user(uid))

    def test_state_hides_balance_number_from_members(self):
        from app import main

        for uid in (22, 23, 27):   # 店长 / 店员 / 办公室同事
            with self.subTest(uid=uid):
                self._as(uid)
                state = main.state()
                self.assertIsNone(state["balance"])
                self.assertEqual("", state["plan"])
                self.assertIs(True, state["points_ok"])
                self.assertNotIn("482", json.dumps(state, default=str))

    def test_state_keeps_balance_for_owner_and_director(self):
        from app import main

        for uid in (20, 21):
            with self.subTest(uid=uid):
                self._as(uid)
                state = main.state()
                self.assertEqual(482, state["balance"])
                self.assertNotIn("points_ok", state)

    def test_points_flags_follow_each_cost(self):
        flags = billing.points_flags(2, 1)
        prices = sorted({float(r["points"]) for r in billing.prices().values()
                         if float(r.get("points") or 0) > 0})
        for pts in prices:
            key = str(int(pts)) if pts.is_integer() else f"{pts:g}"
            self.assertEqual(pts <= 1, flags["points_ok_by_cost"][key])
        self.assertEqual(prices[0] <= 1, flags["points_ok"])
        self.assertIs(False, billing.points_flags(2, 0)["points_ok"])
        # 平台自有租户免扣,恒为够
        self.assertIs(True, billing.points_flags(1, 0)["points_ok"])

    def test_insufficient_points_message_hides_balance_for_members(self):
        from app import main

        exc = HTTPException(402, "点数不足:本次需 18 点,余额 5 点。请充值或升级套餐")
        self._as(23)
        body = json.loads(asyncio.run(main._friendly_http_exception(None, exc)).body)
        self.assertNotIn("余额", body["detail"])
        self.assertIn("本次需 18 点", body["detail"])
        self.assertIn("请联系老板充值", body["detail"])
        self._as(20)
        body = json.loads(asyncio.run(main._friendly_http_exception(None, exc)).body)
        self.assertIn("余额 5 点", body["detail"])


class MemberExpertScopeTests(StaffTaskBase):
    def tearDown(self):
        auth.set_current(None)
        super().tearDown()

    def _as(self, uid):
        auth.set_current(auth.get_user(uid))

    def _restaurant(self):
        return next(d for d in departments.list_depts() if d["key"] == "restaurant")

    def test_auth_me_counts_only_usable_experts(self):
        from app import main

        rest = self._restaurant()
        self._as(20)
        self.assertIsNone(main.auth_me()["expert_count"])
        # 店长:行业已开通但白名单为空 → 0 位
        db.update("users", 22, {"allowed_emp_idxs_json": "[]"})
        self._as(22)
        me = main.auth_me()
        self.assertEqual(0, me["expert_count"])
        self.assertEqual(0, me["meeting_expert_count"])
        # 白名单给 2 位 → 2 位
        two = [int(e["idx"]) for e in rest["employees"][:2]]
        db.update("users", 22, {"allowed_emp_idxs_json": json.dumps(two)})
        self._as(22)
        self.assertEqual(2, main.auth_me()["expert_count"])
        # 没开通任何行业板块 → 0 位
        db.update("users", 23, {"modules_json": "[]"})
        self._as(23)
        self.assertEqual(0, main.auth_me()["expert_count"])

    def test_depts_list_hides_experts_of_unallowed_modules_for_members(self):
        from app import main

        db.update("users", 23, {"modules_json": "[]"})
        self._as(23)
        for dept in main.depts_list():
            self.assertEqual([], dept["employees"], dept["key"])
        self._as(24)   # 有餐饮板块、没设白名单 → 餐饮的人都在
        rest = next(d for d in main.depts_list() if d["key"] == "restaurant")
        self.assertEqual(len(self._restaurant()["employees"]), len(rest["employees"]))
        self._as(20)
        rest = next(d for d in main.depts_list() if d["key"] == "restaurant")
        self.assertTrue(rest["employees"])

    def test_expert_match_roster_respects_member_whitelist(self):
        from app import expertmatch

        rest = self._restaurant()
        one = int(rest["employees"][0]["idx"])
        db.update("users", 22, {"allowed_emp_idxs_json": json.dumps([one])})
        self._as(22)
        self.assertEqual([one], [int(e["idx"]) for e in expertmatch._visible_specialists("restaurant")])
        self._as(20)
        self.assertGreater(len(expertmatch._visible_specialists("restaurant")), 1)

    def test_restricted_team_view_carries_store_names(self):
        from app import main

        db.update("users", 22, {"allowed_emp_idxs_json": "[]"})
        self._as(22)
        team = main.team_get()
        self.assertFalse(team["is_admin"])
        by_id = {u["id"]: u for u in team["users"]}
        self.assertEqual(["人民路店"], by_id[22]["branch_names"])
        self.assertEqual(["人民路店"], by_id[23]["branch_names"])
        self.assertEqual(["中山路店"], by_id[24]["branch_names"])
        self.assertNotIn("store_branches", team)
        self.assertNotIn("balance", team["tenant"])


if __name__ == "__main__":
    unittest.main()
