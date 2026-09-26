"""第 2 期门店排行与老板早报：公式、边界、较上期、按门店绑定过滤、早报内容。"""
from __future__ import annotations

import unittest
from datetime import datetime

from app import db, notify, scheduler, storerank

from tests.test_phase2_checklist import D0, H, Phase2Base

DAY_S = 86400
NOW = D0 + 7 * DAY_S + 12 * H          # 2026-09-28 12:00，周窗 = 09-21 12:00 ~ 09-28 12:00


class FormulaTests(unittest.TestCase):
    def test_issue_score_normalizes_by_area_or_visits(self):
        self.assertIsNone(storerank.issue_score(5, 0, 200))
        self.assertEqual(50.0, storerank.issue_score(5, 1, None))      # 没面积按 100㎡
        self.assertEqual(75.0, storerank.issue_score(5, 1, 200))       # 大店问题折半
        self.assertEqual(75.0, storerank.issue_score(10, 2, 200))      # 按巡店次数平均
        self.assertEqual(0.0, storerank.issue_score(30, 1, 100))       # 最低 0 分
        self.assertEqual(100.0, storerank.issue_score(0, 3, None))
        # 面积异常值夹在 30~5000㎡ 之间，不会一个问题扣穿
        self.assertEqual(storerank.issue_score(1, 1, 30), storerank.issue_score(1, 1, 1))
        self.assertEqual(50.0, storerank.issue_score(5, 1, "坏数据"))

    def test_total_reweights_missing_components(self):
        parts = {"checklist": {"score": 100.0}, "action": {"score": 0.0},
                 "task": {"score": None}, "issue": {"score": 80.0}}
        # (100×0.4 + 0×0.3 + 80×0.1) ÷ 0.8 = 60
        self.assertEqual(60.0, storerank.total_score(parts))
        full = {"checklist": {"score": 90.0}, "action": {"score": 80.0},
                "task": {"score": 70.0}, "issue": {"score": 60.0}}
        self.assertEqual(80.0, storerank.total_score(full))
        self.assertIsNone(storerank.total_score(
            {k: {"score": None} for k in storerank.WEIGHTS}))
        self.assertAlmostEqual(1.0, sum(storerank.WEIGHTS.values()))

    def test_period_validation(self):
        self.assertEqual(7, storerank.period_days("week"))
        self.assertEqual(30, storerank.period_days("month"))
        with self.assertRaises(storerank.RankError):
            storerank.period_days("year")


class RankBase(Phase2Base):
    def setUp(self):
        super().setUp()
        self.c = db.insert("store_branch", {"tenant_id": 2, "industry_key": "tea_coffee",
                                            "name": "新开店", "active": 1, "created_at": 0})
        self.tpl = 0

    def run_row(self, branch, due, status, kind="close"):
        self.tpl += 1
        db.insert("checklist_run", {
            "tenant_id": 2, "branch_id": branch, "template_id": self.tpl,
            "run_date": "2026-09-2x", "kind": kind, "status": status, "due_at": due,
            "items_json": "[]"})

    def visit(self, branch, at, issues, *, false_positive=0):
        visit = db.insert("inspection_visit", {"tenant_id": 2, "industry_key": "tea_coffee",
                                               "branch_id": branch, "status": "completed",
                                               "visit_at": at})
        ids = []
        for n in range(issues):
            ids.append(db.insert("inspection_issue", {"tenant_id": 2, "visit_id": visit,
                                                      "title": f"问题{n}", "severity": "low"}))
        for issue in ids[:false_positive]:
            db.insert("inspection_action", {"tenant_id": 2, "visit_id": visit,
                                            "issue_id": issue, "plan": "误报",
                                            "status": "closed", "close_reason": "false_positive",
                                            "due_at": at + DAY_S, "closed_at": at})
        return visit, ids


class RankingTests(RankBase):
    def seed(self):
        start = NOW - 7 * DAY_S
        # A 店：清单全按时、整改按时关、派活按时交、巡店 4 个问题(200㎡)
        for i in range(4):
            self.run_row(self.a, start + (i + 1) * DAY_S, "done", "open")
        visit, issues = self.visit(self.a, start + DAY_S, 4)
        db.insert("inspection_action", {"tenant_id": 2, "visit_id": visit, "issue_id": issues[0],
                                        "plan": "擦干净", "status": "closed",
                                        "due_at": start + 3 * DAY_S, "closed_at": start + 2 * DAY_S})
        for i in range(2):
            db.insert("staff_task", {"tenant_id": 2, "branch_id": self.a, "title": f"活{i}",
                                     "status": "approved", "due_at": start + 2 * DAY_S,
                                     "submitted_at": start + DAY_S})
        # 被取消的、别家门店的不算
        db.insert("staff_task", {"tenant_id": 2, "branch_id": self.a, "title": "取消",
                                 "status": "cancelled", "due_at": start + 2 * DAY_S})
        # B 店：1 次开店按时、3 次闭店没做；1 条整改超期没关；巡店 3 个问题其中 1 个误报
        self.run_row(self.b, start + DAY_S, "done", "open")
        for i in range(3):
            self.run_row(self.b, start + (i + 2) * DAY_S, "missed", "close")
        visit, issues = self.visit(self.b, start + DAY_S, 3, false_positive=1)
        db.insert("inspection_action", {"tenant_id": 2, "visit_id": visit, "issue_id": issues[1],
                                        "plan": "修灯", "status": "open",
                                        "due_at": start + 2 * DAY_S})
        # B 店上期全按时
        for i in range(2):
            self.run_row(self.b, start - (i + 1) * DAY_S, "done", "open")

    def test_scores_components_ranks_and_reasons(self):
        self.seed()
        out = storerank.compute(2, "week", now=NOW)
        self.assertEqual(storerank.FORMULA, out["formula"])
        self.assertEqual({"checklist": 40, "action": 30, "task": 20, "issue": 10},
                         out["weights"])
        by = {row["branch_name"]: row for row in out["stores"]}
        a, b, c = by["朝阳店"], by["静安店"], by["新开店"]
        # A = 100×.4 + 100×.3 + 100×.2 + (100−10×4×100/200)×.1 = 98
        self.assertEqual(98.0, a["score"])
        self.assertEqual(80.0, a["components"]["issue"]["score"])
        self.assertEqual({"score": 100.0, "done": 2, "total": 2, "label": "派活按时完成率",
                          "weight": 20}, a["components"]["task"])
        # B = (25×.4 + 0×.3 + (100−10×2)×.1) ÷ .8 = 22.5，派活没数据不计入
        self.assertEqual(25.0, b["components"]["checklist"]["score"])
        self.assertEqual(0.0, b["components"]["action"]["score"])
        self.assertIsNone(b["components"]["task"]["score"])
        self.assertEqual(80.0, b["components"]["issue"]["score"])
        self.assertEqual(22.5, b["score"])
        self.assertEqual(100.0, b["prev_score"])
        self.assertEqual(-77.5, b["delta"])
        self.assertIn("近 7 天 3 次闭店清单没按时做完", b["reasons"])
        self.assertIn("1 条巡店整改已超期还没关", b["reasons"])
        self.assertIn("比上期掉了 77.5 分", b["reasons"])
        self.assertTrue(b["needs_attention"])
        self.assertFalse(a["needs_attention"])
        self.assertEqual([], a["reasons"])
        # 没数据的店不排名，排最后
        self.assertIsNone(c["score"])
        self.assertIsNone(c["rank"])
        self.assertIn("还没有", c["reasons"][0])
        self.assertEqual(["朝阳店", "静安店", "新开店"],
                         [row["branch_name"] for row in out["stores"]])
        self.assertEqual([1, 2, None], [row["rank"] for row in out["stores"]])
        # 同样的数据再算一遍，结果一字不差
        self.assertEqual(out, storerank.compute(2, "week", now=NOW))

    def test_month_window_includes_older_data(self):
        self.seed()
        week = {r["branch_name"]: r for r in storerank.compute(2, "week", now=NOW)["stores"]}
        month = {r["branch_name"]: r for r in storerank.compute(2, "month", now=NOW)["stores"]}
        self.assertEqual(4, week["静安店"]["components"]["checklist"]["total"])
        self.assertEqual(6, month["静安店"]["components"]["checklist"]["total"])
        self.assertEqual("近 30 天", storerank.compute(2, "month", now=NOW)["period_label"])

    def test_ranking_follows_branch_binding(self):
        self.seed()
        owner = storerank.ranking(2, 20, "week", now=NOW)
        self.assertEqual(3, len(owner["stores"]))
        self.assertEqual(3, len(storerank.ranking(2, 21, "week", now=NOW)["stores"]))  # 总监
        staff_b = storerank.ranking(2, 24, "week", now=NOW)
        self.assertEqual(["静安店"], [r["branch_name"] for r in staff_b["stores"]])
        self.assertEqual(1, staff_b["stores"][0]["rank"])
        manager_a = storerank.ranking(2, 22, "week", now=NOW)
        self.assertEqual(["朝阳店"], [r["branch_name"] for r in manager_a["stores"]])
        other = storerank.ranking(3, 30, "week", now=NOW)
        self.assertEqual([], other["stores"])
        with self.assertRaises(ValueError):
            storerank.ranking(3, 20, "week", now=NOW)                # 跨企业

    def test_rank_change_against_previous_period(self):
        start = NOW - 7 * DAY_S
        # 上期 A 差 B 好，本期反过来
        self.run_row(self.a, start - DAY_S, "missed")
        self.run_row(self.b, start - DAY_S, "done")
        self.run_row(self.a, start + DAY_S, "done")
        self.run_row(self.b, start + DAY_S, "missed")
        by = {r["branch_name"]: r for r in storerank.compute(2, "week", now=NOW)["stores"]}
        self.assertEqual((1, 2, 1), (by["朝阳店"]["rank"], by["朝阳店"]["prev_rank"],
                                     by["朝阳店"]["rank_change"]))
        self.assertEqual(-1, by["静安店"]["rank_change"])
        self.assertEqual(100.0, by["朝阳店"]["delta"])


class MorningBriefTests(RankBase):
    BRIEF_NOW = D0 + DAY_S + 8 * H                   # 2026-09-22 08:00，昨天 = 09-21

    def seed_yesterday(self):
        for tpl, (kind, status) in enumerate((("open", "done"), ("handover", "done"),
                                              ("close", "done")), 1):
            db.insert("checklist_run", {"tenant_id": 2, "branch_id": self.a, "template_id": tpl,
                                        "run_date": "2026-09-21", "kind": kind,
                                        "status": status, "due_at": D0 + 10 * H,
                                        "items_json": "[]", "created_at": D0 + len(kind)})
        for tpl, (kind, status) in enumerate((("open", "done"), ("handover", "missed"),
                                              ("close", "missed")), 1):
            db.insert("checklist_run", {"tenant_id": 2, "branch_id": self.b, "template_id": tpl,
                                        "run_date": "2026-09-21", "kind": kind,
                                        "status": status, "due_at": D0 + 10 * H,
                                        "items_json": "[]", "created_at": D0 + len(kind)})
        db.insert("staff_task", {"tenant_id": 2, "branch_id": self.b, "title": "补货",
                                 "status": "todo", "due_at": D0 + 12 * H})
        db.insert("staff_task", {"tenant_id": 2, "branch_id": self.a, "title": "拍堆头",
                                 "status": "submitted", "due_at": D0 + 20 * H,
                                 "submitted_at": D0 + 19 * H})
        visit = db.insert("inspection_visit", {"tenant_id": 2, "industry_key": "tea_coffee",
                                               "branch_id": self.b, "status": "completed",
                                               "visit_at": D0})
        issue = db.insert("inspection_issue", {"tenant_id": 2, "visit_id": visit,
                                               "title": "灯坏了", "severity": "low"})
        db.insert("inspection_action", {"tenant_id": 2, "visit_id": visit, "issue_id": issue,
                                        "plan": "换灯", "status": "open", "due_at": D0 + H})
        db.insert("inspection_action", {"tenant_id": 2, "visit_id": visit, "issue_id": issue,
                                        "plan": "复查", "status": "awaiting_recheck",
                                        "due_at": D0 + 30 * H})

    def test_brief_has_rate_overdue_ranking_and_waiting(self):
        self.seed_yesterday()
        brief = storerank.morning_brief(2, self.BRIEF_NOW)
        self.assertTrue(brief["has_content"])
        self.assertEqual("2026-09-21", brief["date"])
        self.assertEqual(67, brief["checklist_rate"])
        text = "\n".join(brief["lines"])
        self.assertIn("昨天清单按时完成 67%（4/6）", text)
        self.assertIn("静安店 1/3", text)
        self.assertIn("逾期没做完：静安店 1 件派活、1 条整改", text)
        self.assertIn("前 3：朝阳店", text)
        self.assertNotIn("后 3", text)                  # 只有两家有分，不重复列后 3
        self.assertIn("1 件店员交的活等您验收", text)
        self.assertIn("1 条整改等您复查", text)
        self.assertEqual({"tasks_to_review": 1, "rechecks_to_review": 1}, brief["waiting"])
        self.assertTrue(brief["short"].startswith("门店："))

    def test_bottom_three_listed_for_bigger_chains(self):
        start = self.BRIEF_NOW - 7 * DAY_S
        for n in range(6):
            bid = db.insert("store_branch", {"tenant_id": 2, "industry_key": "tea_coffee",
                                             "name": f"分店{n}", "active": 1, "created_at": 0})
            for i in range(4):
                self.run_row(bid, start + (i + 1) * DAY_S, "done" if i < 4 - n % 4 else "missed")
        brief = storerank.morning_brief(2, self.BRIEF_NOW)
        bottom_line = next(line for line in brief["lines"] if line.startswith("⚠️ 后 3"))
        self.assertEqual(3, len(brief["bottom"]))
        self.assertIn("闭店清单没按时做完", bottom_line)

    def test_no_branches_means_no_store_section(self):
        self.assertEqual({"has_content": False, "lines": [], "short": ""},
                         storerank.morning_brief(3, self.BRIEF_NOW))

    def test_daily_digest_pushes_store_section_once(self):
        self.seed_yesterday()
        now = datetime.fromtimestamp(self.BRIEF_NOW + 60, scheduler.TZ)
        scheduler._run_daily_digest(now)
        notes = db.q("SELECT * FROM notification WHERE kind='daily_digest' ORDER BY id")
        self.assertEqual([(2, 20)], [(n["tenant_id"], n["user_id"]) for n in notes])
        self.assertTrue(notes[0]["body"].startswith("门店：昨天清单按时完成 67%"))
        self.assertEqual("#/checklists", notes[0]["link"])
        scheduler._run_daily_digest(now)                 # 同一天第二次不重发
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM notification "
                                   "WHERE kind='daily_digest'")["n"])
        self.assertFalse(scheduler._claim_daily_digest(2, now.strftime("%Y-%m-%d")))
        self.assertTrue(scheduler._claim_daily_digest(2, "2099-01-01"))

    def test_digest_markdown_has_store_lines_and_today_post_link(self):
        msg = notify.build_msg("daily_digest", {
            "date": "09-21", "store_lines": ["🏪 昨天清单按时完成 67%（4/6）"]})
        self.assertIn("🏪 昨天清单按时完成 67%（4/6）", msg)
        self.assertIn("/#/tools/hot)", msg)
        self.assertIn("/#/checklists)", msg)
        plain = notify.build_msg("daily_digest", {"date": "09-21"})
        self.assertIn("今天发什么", plain)
        self.assertNotIn("#/checklists", plain)


if __name__ == "__main__":
    unittest.main()
