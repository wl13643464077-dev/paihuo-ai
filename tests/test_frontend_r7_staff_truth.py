"""第七轮：老板端各页如实反映「派给店员的活」(真实浏览器渲染)。

今天页逾期、老板看板先处理清单、派出去的活入口、交付中心空编号、门店排行名次牌、
派给店员页的状态徽标颜色。
"""
from __future__ import annotations

import unittest

from playwright.async_api import async_playwright

# 只导入模块，不把对方的 TestCase 类放进本模块命名空间(否则会被重复发现执行)。
from tests import test_frontend_nav_mobile as _nav

STATE = _nav.STATE
INSPECTIONS = _nav.INSPECTIONS


class FrontendStaffTruthTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_nav.FrontendNavMobileTests.setUpClass.__func__)
    tearDownClass = classmethod(_nav.FrontendNavMobileTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _nav.FrontendNavMobileTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _nav.FrontendNavMobileTests._assert_no_browser_errors
    _open = _nav.FrontendNavMobileTests._open

    async def test_today_page_counts_overdue_staff_tasks(self):
        state = {**STATE, "inbox": [], "notifications": [],
                 "staff_task_counts": {"available": True, "doing": 3, "overdue": 2, "review_pending": 0}}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, width=1280, height=900, api_overrides={"/api/state": state})
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('今天的门店')")
            todo = await page.locator(".today-todo").inner_text()
            # 2 条整改等审核 + 2 件店员的活逾期
            self.assertIn("等我处理的（4）", todo)
            self.assertIn("2 件派给店员的活已逾期", todo)
            stats = await page.evaluate(
                "[...document.querySelectorAll('.today-stats > div')].map(x=>x.innerText.replace(/\\s+/g,' ').trim())")
            self.assertEqual("0 今日巡店", stats[0])
            self.assertIn("另有店员在做 3 件", stats[1])
            # 逾期 = 整改 1 + 店员的活 2，口径写在格子里
            self.assertTrue(stats[2].startswith("3 逾期"), stats[2])
            self.assertIn("整改 1 · 店员的活 2", stats[2])
            cell = page.locator(".today-stats > div.bad")
            self.assertEqual(1, await cell.count())
            await cell.click()
            await page.wait_for_function("location.hash==='#/staff-tasks'")
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_today_page_overdue_stays_plain_when_nothing_is_overdue(self):
        state = {**STATE, "inbox": [], "notifications": []}
        inspections = {"items": [], "summary": {"open_issues": 0, "overdue_actions": 0,
                                                "pending_rechecks": 0, "total_branches": 3}}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, width=1280, height=900,
                api_overrides={"/api/state": state, "/api/inspections": inspections})
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('今天的门店')")
            self.assertEqual(0, await page.locator(".today-stats > div.bad").count())
            self.assertNotIn("已逾期", await page.locator(".today-todo").inner_text())
            self.assertIn("整改和店员的活都算", await page.locator(".today-stats").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_boss_dashboard_lists_staff_tasks_first_and_labels_ai_kpis(self):
        summary = {
            "scope": {"tenant_id": 1, "tenant_name": "阿花奶茶", "industry_key": "restaurant",
                      "industry_name": "餐饮产业部", "industry_emoji": "🍜"},
            "period": {"days": 7}, "task_metrics": {}, "efficiency_metrics": {},
            "risk_metrics": {"staff_review_pending": 1, "staff_overdue_tasks": 2,
                             "stale_active_tasks": 0, "waiting_for_decision": 0,
                             "failed_tasks": 0, "refunded_tasks": 0},
            "inspection_metrics": {"availability": False, "backlog": {}},
            "business_metrics": {"metrics": []}, "trend": [], "employees": [],
            "recent_activity": [], "can_open_records": True,
        }
        overrides = {
            "/api/boss/dashboard/scopes": {"can_cross_tenant": False, "tenants": [{
                "id": 1, "name": "阿花奶茶", "industries": [
                    {"key": "restaurant", "name": "餐饮产业部", "short_label": "餐饮", "emoji": "🍜"}]}]},
            "/api/boss/dashboard/summary": summary,
        }
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, width=1280, height=900, hash_="#/mine", api_overrides=overrides)
            # 老板看板已并进数据驾驶舱、不再有入口(#/boss 会跳 #/cockpit)；视图函数还保留，这里直接调用它
            await page.wait_for_selector("#main .hubhead")
            await page.evaluate("void bossDashboardView()")
            await page.wait_for_selector("[data-boss-staff-risk]")
            rows = await page.evaluate(
                "[...document.querySelectorAll('[data-boss-staff-risk]')].map(a=>"
                "[a.getAttribute('href'), a.innerText.replace(/\\s+/g,' ').trim(), getComputedStyle(a.querySelector('b')).color])")
            self.assertEqual(2, len(rows))
            self.assertEqual(["#/staff-tasks", "#/staff-tasks"], [r[0] for r in rows])
            self.assertTrue(rows[0][1].startswith("店员交差等您审核"), rows[0][1])
            self.assertTrue(rows[1][1].startswith("派给店员的活已逾期"), rows[1][1])
            self.assertEqual(["rgb(180, 41, 47)"] * 2, [r[2] for r in rows])
            text = await page.locator("#main").inner_text()
            self.assertIn("交给 AI 专家的任务", text)
            self.assertIn("派给店员的活看「先处理清单」", text)
            # 管不着店员的活时(接口给 None)不显示这两行，也不伪装成 0
            summary["risk_metrics"].update(staff_review_pending=None, staff_overdue_tasks=None)
            await page.evaluate("void bossDashboardView()")
            await page.wait_for_function(
                "document.querySelector('#main')?.innerText.includes('先处理清单')"
                " && !document.querySelector('[data-boss-staff-risk]')")
            self.assertNotIn("店员交差等您审核", await page.locator("#main").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_task_center_points_to_staff_tasks_and_delivery_without_id_goes_back(self):
        state = {**STATE,
                 "staff_task_counts": {"available": True, "doing": 3, "overdue": 1, "review_pending": 1}}
        overrides = {"/api/state": state,
                     "/api/task-center": {"items": [], "total": 0, "counts": {}}}
        async with async_playwright() as playwright:
            # 不带任务编号打开交付中心：不报原始错误，回到「派出去的活」
            browser, page, errors = await self._open(
                playwright, width=1280, height=900, hash_="#/delivery", api_overrides=overrides)
            await page.wait_for_function("location.hash==='#/tasks'")
            await page.wait_for_selector("[data-tc-staff-entry]")
            main = await page.locator("#main").inner_text()
            self.assertNotIn("path.job_id", main)
            self.assertIn("这里是您交给 AI 专家的活", main)
            self.assertNotIn("您派出去的活都在这里", main)
            entry = page.locator("[data-tc-staff-entry]")
            self.assertEqual("#/staff-tasks", await entry.get_attribute("href"))
            self.assertIn("派给店员的活在「派给店员」里看 →（在做 3 件 / 等审核 1 件，其中逾期 1 件）",
                          await entry.inner_text())
            self.assertIn("还没有交给 AI 专家的任务。", main)
            self.assertEqual("#/staff-tasks", await page.locator(
                "#tc-list .empty a", has_text="派给店员").get_attribute("href"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_store_rank_gives_no_medal_to_a_lone_or_flagged_store(self):
        def store(bid, name, score, rank, flagged=False):
            return {"branch_id": bid, "branch_name": name, "score": score, "rank": rank,
                    "delta": None, "rank_change": None, "needs_attention": flagged,
                    "reasons": [], "components": {}}
        lone = {"formula": "算法说明", "stores": [
            store(1, "春熙路店", 0, 1, True), store(2, "人民路店", None, None), store(3, "中山路店", None, None)]}
        many = {"formula": "算法说明", "stores": [
            store(1, "人民路店", 92, 1), store(2, "中山路店", 71, 2), store(3, "春熙路店", 20, 3, True)]}
        ranking = {"/api/stores/ranking": lone}
        badges = ("[...document.querySelectorAll('.ck-rank-no')].map(x=>"
                  "[x.textContent.trim(), ['top','warn','none'].find(c=>x.classList.contains(c))||''])")
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, width=1280, height=900, hash_="#/store-rank", api_overrides=ranking)
            await page.wait_for_selector("[data-ck-norank]")
            self.assertEqual([["—", "none"]] * 3, await page.evaluate(badges))
            self.assertIn("只有 1 家店有记录，暂不排名。", await page.locator("[data-ck-norank]").inner_text())
            self.assertIn("要盯一下", await page.locator("#ck-rank").inner_text())
            lone["stores"] = many["stores"]   # _open 拷贝了路由表，只能原地改这份数据
            await page.evaluate("ckPeriod('month')")
            await page.wait_for_function("document.querySelectorAll('.ck-rank-no.top').length===2")
            self.assertEqual([["1", "top"], ["2", "top"], ["3", "warn"]], await page.evaluate(badges))
            self.assertEqual(0, await page.locator("[data-ck-norank]").count())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_staff_task_badges_use_warning_color_for_overdue_and_rejected(self):
        def task(tid, **kw):
            return {"id": tid, "title": f"活{tid}", "status": "todo", "photos": [], "branch_id": 3,
                    "branch_name": "人民路店", "assignee_user_id": 23, "assignee_name": "xiaoli",
                    "created_by_name": "tea-owner", "overdue": False, **kw}
        tasks = {"items": [
            task(1, overdue=True), task(2, review_note="重拍", reviewed_at=1700000000),
            task(3), task(4, status="submitted"), task(5, status="approved")], "next_before_id": None}
        overrides = {"/api/staff/tasks": tasks,
                     "/api/staff/meta": {"branches": [{"id": 3, "name": "人民路店", "members": []}]}}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, width=1280, height=900, hash_="#/staff-tasks", api_overrides=overrides)
            await page.wait_for_selector("#sa-task-1")
            pills = await page.evaluate(
                "Object.fromEntries([1,2,3,4,5].map(i=>{const p=document.querySelector(`#sa-task-${i} .pill`);"
                "return [i,[p.textContent.trim(), getComputedStyle(p).backgroundColor]];}))")
            red, blue, yellow, green = ("rgb(255, 194, 197)", "rgb(201, 226, 255)",
                                        "rgb(255, 233, 173)", "rgb(167, 236, 201)")
            self.assertEqual(["已逾期", red], pills["1"])
            self.assertEqual(["被打回", red], pills["2"])
            self.assertEqual(["待做", blue], pills["3"])
            self.assertEqual(["等审核", yellow], pills["4"])
            self.assertEqual(["已通过", green], pills["5"])
            self._assert_no_browser_errors(errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
