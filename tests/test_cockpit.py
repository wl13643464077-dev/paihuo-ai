"""老板数据驾驶舱：口径、租户隔离、待拍板、员工使用、下发追踪、导出、AI 分析扣退点。"""
from __future__ import annotations

import asyncio
import io
import json
import unittest

from app import billing, cockpit, db

from tests.test_phase2_checklist import D0, Phase2Base

DAY_S = 86400
NOW = D0 + 7 * DAY_S + 12 * 3600       # 2026-09-28 12:00
IN = NOW - 2 * DAY_S                   # 本期内
PREV = NOW - 10 * DAY_S                # 上期内


class CockpitBase(Phase2Base):
    def task(self, tid, status, at, uid=20, name="店长助理", dept="tea_coffee"):
        return db.insert("task", {
            "tenant_id": tid, "emp_idx": 101, "brief_json": json.dumps({"direction": "做个周报"}),
            "status": status, "employee_name_snapshot": name, "employee_dept_key": dept,
            "created_by": uid, "created_at": at, "updated_at": at, "terminal_at": at})

    def seed(self):
        self.task(2, "done", IN)
        self.task(2, "done", IN, uid=22)
        self.task(2, "done", PREV)
        self.task(2, "awaiting_review", IN)
        self.task(3, "done", IN, uid=30)          # 别家企业，不能算进来
        db.insert("job", {"tenant_id": 2, "brief_json": json.dumps({"direction": "国庆海报"}),
                          "status": "done", "created_by": 22, "created_at": IN, "updated_at": IN})
        db.insert("avatar_job", {"tenant_id": 2, "params_json": "{}", "status": "done",
                                 "created_by": 20, "created_at": IN, "updated_at": IN})
        # 店员任务：一件按时交、一件逾期没交、一件已交待验收
        db.insert("staff_task", {"tenant_id": 2, "branch_id": self.a, "assignee_user_id": 23,
                                 "title": "擦玻璃", "status": "approved", "created_by": 22,
                                 "due_at": IN + 3600, "submitted_at": IN, "reviewed_at": IN,
                                 "created_at": IN - 3600})
        db.insert("staff_task", {"tenant_id": 2, "branch_id": self.b, "assignee_user_id": 24,
                                 "title": "补货", "status": "todo", "created_by": 20,
                                 "due_at": IN, "created_at": IN - 3600})
        db.insert("staff_task", {"tenant_id": 2, "branch_id": self.a, "assignee_user_id": 23,
                                 "title": "清洗制冰机", "status": "submitted", "created_by": 20,
                                 "due_at": NOW + DAY_S, "submitted_at": NOW - 3600,
                                 "created_at": IN})
        db.insert("billing_log", {"tenant_id": 2, "delta": -10, "balance": 90, "reason": "专家任务",
                                  "created_at": IN})
        db.insert("billing_log", {"tenant_id": 2, "delta": 2, "balance": 92,
                                  "reason": "退回:专家任务", "created_at": IN})
        db.insert("billing_log", {"tenant_id": 3, "delta": -50, "balance": 0, "reason": "x",
                                  "created_at": IN})


class SummaryTests(CockpitBase):
    def test_counts_are_tenant_scoped_and_compared_with_previous_period(self):
        self.seed()
        d = cockpit.summary(2, 7, now=NOW)
        k = d["kpis"]
        self.assertEqual(4, k["ai_done"]["value"])            # 2 专家 + 1 内容 + 1 数字人
        self.assertEqual(1, k["ai_done"]["prev"])
        self.assertEqual(8.0, k["points"]["value"])           # 扣 10 退 2，别家 50 不算
        self.assertEqual(2, k["staff_done"]["value"])
        self.assertEqual(50.0, k["staff_rate"]["value"])     # 到期 2 件按时 1 件
        self.assertIsNone(k["inspection"]["value"])           # 没巡店就显示 —，不造 0
        mix = {m["key"]: m["count"] for m in d["mix"]}
        self.assertEqual(2, mix["expert"])
        self.assertEqual(1, mix["video"])
        self.assertEqual("店长助理", d["ai_employees"][0]["name"])
        self.assertEqual(7, len(d["trend"]))
        self.assertEqual(4, sum(x["ai"] for x in d["trend"]))
        self.assertTrue(d["headline"])

    def test_todo_lists_overdue_first_then_reviews(self):
        self.seed()
        todo = cockpit.summary(2, 7, now=NOW)["todo"]
        self.assertEqual("staff_overdue", todo[0]["key"].split(":")[0])
        self.assertEqual("静安店", todo[0]["branch"])
        self.assertTrue(todo[0]["dispatchable"])
        keys = {t["key"].split(":")[0] for t in todo}
        self.assertIn("staff_review", keys)
        self.assertIn("task", keys)                           # AI 员工等确认

    def test_people_usage_and_idle_flags(self):
        self.seed()
        people = {p["id"]: p for p in cockpit.summary(2, 7, now=NOW)["people"]}
        self.assertNotIn(30, people)                          # 别家老板
        self.assertEqual(2, people[22]["ai_tasks"])           # 1 任务 + 1 内容
        self.assertEqual(1, people[22]["dispatched"])
        self.assertEqual(2, people[23]["staff_done"])
        self.assertEqual(1, people[24]["overdue"])
        self.assertEqual("never", people[21]["flag"])         # 总监从没用过
        self.assertEqual(["朝阳店"], people[23]["branches"])

    def test_tracking_only_shows_cockpit_dispatches(self):
        self.seed()
        db.insert("staff_task", {"tenant_id": 2, "branch_id": self.b, "assignee_user_id": 24,
                                 "title": "整改冷柜温度", "status": "todo", "source": "boss",
                                 "source_ref": "cockpit:ai", "due_at": NOW - 3600,
                                 "created_at": IN})
        track = cockpit.summary(2, 7, now=NOW)["tracking"]
        self.assertEqual(1, len(track))
        self.assertEqual("已逾期", track[0]["status_label"])
        self.assertEqual("u24", track[0]["who"])

    def test_bad_period_rejected(self):
        with self.assertRaises(cockpit.CockpitError):
            cockpit.summary(2, 14, now=NOW)

    def test_export_has_all_sheets(self):
        from openpyxl import load_workbook
        self.seed()
        wb = load_workbook(io.BytesIO(cockpit.export_xlsx(cockpit.summary(2, 30, now=NOW))))
        self.assertEqual(["概览", "员工使用", "门店", "待拍板", "下发追踪", "每日趋势", "AI员工"],
                         wb.sheetnames)
        self.assertEqual("u20", wb["员工使用"]["A2"].value or wb["员工使用"]["A3"].value)


class AiTests(CockpitBase):
    def setUp(self):
        super().setUp()
        db.execute("UPDATE tenants SET balance=10 WHERE id=2")

    def test_analysis_charges_maps_actions_and_is_saved(self):
        self.seed()
        seen = {}

        async def fake(prompt, tid):
            seen["prompt"] = prompt
            return {"data": {"verdict": "执行还行，静安店补货拖了", "highlights": ["AI 交付 4 件"],
                             "risks": ["静安店补货逾期"],
                             "actions": [{"title": "静安店今天补齐货", "detail": "拍照交差",
                                          "branch": "静安店", "owner": "u24", "priority": "high",
                                          "due_days": 1},
                                         {"title": "瞎编门店", "branch": "不存在店", "owner": "谁"}]}}

        out = asyncio.run(cockpit.analyze(2, 7, call=fake, now=NOW))
        self.assertIn("静安店", seen["prompt"])
        self.assertNotIn("output_md", seen["prompt"])        # 只给汇总，不给交付正文
        self.assertEqual(self.b, out["actions"][0]["branch_id"])
        self.assertEqual(24, out["actions"][0]["owner_id"])
        self.assertIsNone(out["actions"][1]["branch_id"])     # 编出来的门店不认
        self.assertEqual(1, out["points"])
        self.assertEqual(9, billing.balance(2))
        self.assertEqual(out["verdict"], cockpit.summary(2, 7, now=NOW)["ai_analysis"]["verdict"])

    def test_restaurant_tenant_analysis_follows_weekly_review_playbook(self):
        db.execute("INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                   "VALUES(2,'restaurant',0,0)")
        prompt = cockpit.build_ai_prompt(cockpit.summary(2, 7, now=NOW))
        self.assertIn("周会月会复盘与执行追踪", prompt)
        db.execute("DELETE FROM tenant_industry WHERE tenant_id=2 AND industry_key='restaurant'")
        self.assertNotIn("周会月会复盘", cockpit.build_ai_prompt(cockpit.summary(2, 7, now=NOW)))

    def test_failed_analysis_refunds(self):
        async def broken(prompt, tid):
            return {"data": {"oops": 1}}

        with self.assertRaises(cockpit.CockpitError):
            asyncio.run(cockpit.analyze(2, 7, call=broken, now=NOW))
        self.assertEqual(10, billing.balance(2))

    def test_no_points_is_402(self):
        db.execute("UPDATE tenants SET balance=0 WHERE id=2")
        with self.assertRaises(cockpit.CockpitError) as ctx:
            asyncio.run(cockpit.analyze(2, 7, call=None, now=NOW))
        self.assertEqual(402, ctx.exception.status)


if __name__ == "__main__":
    unittest.main()
