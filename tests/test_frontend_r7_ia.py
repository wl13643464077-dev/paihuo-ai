"""第七轮 A 组：信息架构与入口(真实浏览器渲染)。

派活中转页分两段、中转页卡片带实时数字、老板看板并进数据驾驶舱、平台管理员导航、
套餐页顺序、改密码移到「我的」。
"""
from __future__ import annotations

import json
import time
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright

# 只导入模块，不把对方的 TestCase 类放进本模块命名空间(否则会被重复发现执行)。
from tests import test_frontend_nav_mobile as _nav

STATE = _nav.STATE
OWNER = _nav.OWNER
# 店长能派活给店员：PH_STAFF_ADMIN.canDispatch() 看 can_dispatch_staff
OWNER_WITH_EXPERTS = {**OWNER, "modules": ["content", "avatar", "library", "restaurant"],
                      "industries": ["restaurant"], "can_dispatch_staff": True}
ROOT = {"id": 1, "username": "boss", "role": "root", "tenant": "派活平台",
        "modules": ["content", "avatar", "library", "restaurant"], "all_modules": [],
        "industries": ["restaurant"]}
STAFF = {"id": 23, "username": "xiaoli", "role": "member", "job_title": "staff",
         "tenant": "阿花奶茶", "modules": [], "all_modules": []}
NOW = time.time()


def _kpi(label, value=0, unit="件"):
    return {"label": label, "value": value, "prev": 0, "delta": 0, "unit": unit, "hint": "", "good": "up"}


COCKPIT = {
    "tenant": {"id": 1, "name": "阿花奶茶", "plan": "标准版", "balance": 120},
    "period": {"days": 7, "start": NOW - 7 * 86400, "end": NOW, "options": [7, 30, 90]},
    "generated_at": NOW, "headline": ["近 7 天派出 3 件活"],
    "kpis": {key: _kpi(key) for key in (
        "dispatched", "ai_done", "staff_done", "staff_rate", "checklist_rate",
        "inspection", "active_people", "points")},
    "todo": [{"key": "task_stale:9", "type": "stale", "level": "mid", "label": "AI 任务超过 24 小时还在跑",
              "title": "做个周报", "branch_id": None, "branch": "", "who": "店长助理",
              "since": NOW - 2 * 86400, "route": "#/tasks/9", "dispatchable": False}],
    "todo_counts": {"total": 1, "high": 0},
    "people": [], "stores": [], "trend": [], "mix": [],
    "ai_employees": [
        {"name": "店长助理", "dept": "餐饮", "count": 3, "created": 4, "waiting": 1,
         "failed": 0, "refunded": 0, "avg_cycle_seconds": 1500},
        {"name": "选品顾问", "dept": "餐饮", "count": 0, "created": 1, "waiting": 0,
         "failed": 1, "refunded": 1, "avg_cycle_seconds": None},
    ],
    "content": {"platforms": {}}, "tracking": [], "branches": [], "ai_analysis": None, "notes": "",
}
BILLING = {
    "balance": 88, "plan": "创业版", "plan_expires": NOW + 40 * 86400, "is_platform": False,
    "recharged": 100, "spent": 12, "refunded_total": 0, "txn_n": 2,
    "log": [{"id": 2, "delta": -12, "balance": 88, "reason": "专家任务", "created_at": NOW - 3600},
            {"id": 1, "delta": 100, "balance": 100, "reason": "开通套餐", "created_at": NOW - 7200}],
    "monthly": [{"ym": "2026-11", "recharged": 100, "spent": 12, "refunded": 0}],
    "prices": {"expert_task": {"label": "找专家出方案", "points": 3, "group": "expert",
                               "group_label": "找专家", "group_order": 2}},
    "spend_by_action": [],
}


class FrontendInfoArchitectureTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_nav.FrontendNavMobileTests.setUpClass.__func__)
    tearDownClass = classmethod(_nav.FrontendNavMobileTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _nav.FrontendNavMobileTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _nav.FrontendNavMobileTests._assert_no_browser_errors
    _open = _nav.FrontendNavMobileTests._open
    _visible_tabs = _nav.FrontendNavMobileTests._visible_tabs

    async def _sections(self, page):
        return await page.evaluate(
            """() => [...document.querySelectorAll('#main [data-hub-section]')].map(h => [
                 h.textContent.trim(),
                 [...h.nextElementSibling.querySelectorAll('.hc-t')].map(x => x.textContent.trim())])""")

    # ---------- 1. 派活中转页分两段 ----------
    async def test_dispatch_hub_splits_staff_and_ai_sections_and_renames_ai_tasks(self):
        state = {**STATE, "setup": {"store": True}}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=OWNER_WITH_EXPERTS, width=1280, height=900, hash_="#/dispatch",
                api_overrides={"/api/state": state,
                               "/api/task-center": {"items": [], "total": 0, "counts": {}}})
            await page.wait_for_selector("[data-hub-section]")
            self.assertEqual([
                ["派给店里的人", ["派给店员"]],
                ["交给 AI 专家", ["下达任务（内容）", "找行业专家", "多位专家一起商量", "协同小队",
                             "交给 AI 的活", "AI 专家产出"]],
            ], await self._sections(page))
            self.assertNotIn("派出去的活", await page.locator("#main").inner_text())
            # 路由 key 不变，只换显示名：标签标题、页面标题一起换
            await page.locator(".hubcard", has_text="交给 AI 的活").click()
            await page.wait_for_function("location.hash==='#/tasks'")
            await page.wait_for_function(
                "document.querySelector('#main h2')?.textContent.includes('交给 AI 的活')")
            self.assertEqual("交给 AI 的活 · 派活", await page.title())
            self.assertEqual("交给 AI 的活", await page.evaluate("ROUTE_LABELS.tasks"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_dispatch_hub_puts_ai_first_without_store_and_hides_empty_section(self):
        async with async_playwright() as playwright:
            # 还没有门店：先用 AI 专家，「派给店里的人」整段放最后
            browser, page, errors = await self._open(
                playwright, me=OWNER_WITH_EXPERTS, width=1280, height=900, hash_="#/dispatch")
            await page.wait_for_selector("[data-hub-section]")
            self.assertEqual(["交给 AI 专家", "派给店里的人"], [s[0] for s in await self._sections(page)])
            await browser.close()
            # 没有派活权限的成员：看不到的那一段不出现，权限逻辑不变
            member = {**_nav.MEMBER_NO_CONTENT, "modules": ["content", "library"]}
            browser, page, errors = await self._open(
                playwright, me=member, width=1280, height=900, hash_="#/dispatch")
            await page.wait_for_function("document.querySelector('#main .hubhead')")
            sections = await self._sections(page)
            self.assertNotIn("派给店里的人", [s[0] for s in sections])
            for _title, cards in sections:
                self.assertNotIn("派给店员", cards)
                self.assertNotIn("AI 专家产出", cards)
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 2. 中转页卡片带实时数字 ----------
    async def test_hub_cards_show_live_numbers_and_hide_zeros(self):
        state = {**STATE, "setup": {"store": True},
                 "staff_task_counts": {"available": True, "doing": 3, "overdue": 2, "review_pending": 1}}
        overrides = {
            "/api/state": state,
            "/api/task-center": {"items": [], "total": 0, "counts": {"waiting": 0, "failed": 0}},
            "/api/checklist/runs": {"stores": [
                {"branch_id": 1, "items_total": 5, "items_done": 3},
                {"branch_id": 2, "items_total": 3, "items_done": 0}]},
            "/api/schedules": [
                {"id": 1, "enabled": 1, "next_run_at": NOW + 60, "last_run_at": None},
                {"id": 2, "enabled": 0, "next_run_at": NOW + 60, "last_run_at": None},
                {"id": 3, "enabled": 1, "next_run_at": NOW + 9 * 86400, "last_run_at": None}],
        }
        stat = ("(route) => { const el=document.querySelector(`[data-hub-stat=\"${route}\"]`);"
                " return el ? [el.hidden, el.textContent, el.classList.contains('hot')] : null; }")
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=OWNER_WITH_EXPERTS, width=1280, height=900, hash_="#/dispatch",
                api_overrides=overrides)
            await page.wait_for_function(
                "document.querySelector('[data-hub-stat=\"staff-tasks\"]')?.hidden===false")
            self.assertEqual([False, "1 件等审核 · 2 件逾期", True],
                             await page.evaluate(stat, "staff-tasks"))
            # 内容任务用今天页同一份待处理(inbox)
            self.assertEqual([False, "2 篇等您处理", False], await page.evaluate(stat, "new"))
            # 数字为 0：不显示
            await page.wait_for_timeout(300)
            self.assertEqual([True, "", False], await page.evaluate(stat, "tasks"))
            # 没有数可取的卡片不留占位
            self.assertIsNone(await page.evaluate(stat, "experts"))

            await page.evaluate("location.hash='#/store'")
            await page.wait_for_function(
                "document.querySelector('[data-hub-stat=\"checklists\"]')?.hidden===false"
                " && document.querySelector('[data-hub-stat=\"inspections\"]')?.hidden===false")
            self.assertEqual([False, "今天 3/8 项", False], await page.evaluate(stat, "checklists"))
            self.assertEqual([False, "4 个问题没整改完 · 1 条整改逾期 · 2 条等您复查", True],
                             await page.evaluate(stat, "inspections"))

            await page.evaluate("location.hash='#/growth'")
            await page.wait_for_function(
                "document.querySelector('[data-hub-stat=\"schedules\"]')?.hidden===false")
            self.assertEqual([False, "今天 1 条", False], await page.evaluate(stat, "schedules"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_hub_cards_render_first_and_stay_quiet_when_numbers_fail(self):
        state = {**STATE, "setup": {"store": True}, "inbox": []}
        hits = []
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=OWNER_WITH_EXPERTS, width=1280, height=900, hash_="#/",
                api_overrides={"/api/state": state})

            async def hang(route):
                # 接口一直不回：卡片必须已经画出来，不能等它
                hits.append(urlparse(route.request.url).path)

            async def broken(route):
                hits.append(urlparse(route.request.url).path)
                await route.fulfill(status=200, content_type="application/json", body=json.dumps("oops"))

            await page.route("**/api/task-center**", hang)
            await page.route("**/api/checklist/runs**", broken)
            await page.evaluate("location.hash='#/dispatch'")
            await page.wait_for_selector(".hubcard")
            self.assertEqual(7, await page.locator(".hubcard").count())
            await page.wait_for_function(
                "document.querySelector('[data-hub-stat=\"tasks\"]')!==null")
            self.assertTrue(await page.evaluate("document.querySelector('[data-hub-stat=\"tasks\"]').hidden"))
            await page.evaluate("location.hash='#/store'")
            await page.wait_for_selector("[data-hub-stat=\"checklists\"]", state="attached")
            await page.wait_for_timeout(300)
            self.assertTrue(await page.evaluate(
                "document.querySelector('[data-hub-stat=\"checklists\"]').hidden"))
            self.assertIn("/api/task-center", hits)
            self.assertIn("/api/checklist/runs", hits)
            self.assertEqual("", await page.evaluate(
                "(document.querySelector('#toast-stack')?.textContent||'')"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 3. 看板收成一块 ----------
    async def test_store_hub_drops_boss_board_and_old_route_lands_on_cockpit(self):
        overrides = {"/api/cockpit/summary": COCKPIT, "/api/staff/meta": {"branches": [], "can_dispatch": True}}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=OWNER_WITH_EXPERTS, width=1280, height=900, hash_="#/store",
                api_overrides=overrides)
            await page.wait_for_selector(".hubcard")
            titles = await page.locator(".hubcard .hc-t").all_inner_texts()
            self.assertEqual(["数据驾驶舱", "巡店", "开闭店清单", "门店排行"], titles)
            self.assertFalse(await page.evaluate(
                "NAV_GROUPS.some(g=>g.cards.some(c=>c.route==='boss'))"))
            self.assertEqual(0, await page.locator('#main a[href="#/boss"]').count())

            # 旧地址进来直接换成驾驶舱，而且不留历史(后退回到门店页，不会弹回 #/boss)
            await page.evaluate("location.hash='#/boss'")
            await page.wait_for_function("location.hash==='#/cockpit'")
            await page.wait_for_selector("[data-cp-ai-employees]")
            self.assertEqual("数据驾驶舱 · 派活", await page.title())
            main = await page.locator("#main").inner_text()
            self.assertIn("AI 任务超过 24 小时还在跑", main)
            self.assertNotIn("老板看板", main)
            await page.go_back()
            await page.wait_for_function("location.hash==='#/store'")

            # 原老板看板的「AI 专家干了什么、效率怎么样」并成驾驶舱里的明细
            await page.evaluate("location.hash='#/cockpit'")
            await page.wait_for_selector("[data-cp-ai-all]")
            self.assertEqual(0, await page.locator("[data-cp-ai-table]").count())
            self.assertIn("看明细（2 位专家）", await page.locator("[data-cp-ai-all]").inner_text())
            await page.locator("[data-cp-ai-all]").click()
            rows = await page.evaluate(
                "[...document.querySelectorAll('[data-cp-ai-table] tbody tr')].map(tr=>"
                "[...tr.children].map(td=>td.innerText.replace(/\\s+/g,' ').trim()))")
            self.assertEqual(["店长助理 餐饮", "4", "3", "25 分钟", "1", "0", "0"], rows[0])
            self.assertEqual(["选品顾问 餐饮", "1", "0", "—", "0", "1", "1"], rows[1])
            self.assertEqual("#/production", await page.locator(
                "[data-cp-ai-employees] h3 a").first.get_attribute("href"))
            self.assertEqual("#/store-rank", await page.locator(
                'a.cp-mini[href="#/store-rank"]').get_attribute("href"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 4. 平台管理员导航 ----------
    async def test_root_nav_is_workbench_tenants_admin_mine_on_desktop_and_phone(self):
        team = {"tenant": {"name": "派活平台", "balance": 0}, "users": [], "all_modules": [],
                "industry_employees": [], "job_titles": [], "my_job_title": "", "is_admin": True,
                "can_allocate": True, "store_branches": {"items": [], "truncated": False},
                "guests": [], "applies": [], "all_industries": [], "tenants": []}
        overrides = {"/api/team": team,
                     "/api/team/apply-config": {"auto": False, "trial_points": 20, "daily_cap": 20}}
        expected = [("工作台", "#/"), ("企业", "#/platform"), ("后台", "#/admin"), ("我的", "#/mine")]
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=ROOT, width=1280, height=900, api_overrides=overrides)
            await page.wait_for_selector("[data-platform-today]")
            tabs = await self._visible_tabs(page, "#nav a.nav-top")
            self.assertEqual(expected, [(t["label"], t["href"]) for t in tabs])
            self.assertEqual([True, False, False, False], [t["on"] for t in tabs])
            await page.locator('#nav a[data-nav="platform"]').click()
            await page.wait_for_function("location.hash==='#/platform'")
            await page.wait_for_function(
                "document.querySelector('#nav a.nav-top.on')?.dataset.nav==='platform'")
            await page.evaluate("location.hash='#/mine'")
            await page.wait_for_function(
                "document.querySelector('#nav a.nav-top.on')?.dataset.nav==='mine'")
            # 不拦路由：直接输地址仍能打开其它页面，只是导航上不高亮
            await page.evaluate("location.hash='#/dispatch'")
            await page.wait_for_function("document.querySelector('#main .hubhead')")
            self.assertEqual("#/dispatch", await page.evaluate("location.hash"))
            self.assertEqual(0, await page.locator("#nav a.nav-top.on").count())
            self._assert_no_browser_errors(errors)
            await browser.close()

            browser, page, errors = await self._open(playwright, me=ROOT, api_overrides=overrides)
            await page.wait_for_selector("[data-platform-today]")
            tabs = await self._visible_tabs(page, "#tabbar a")
            self.assertEqual(expected, [(t["label"], t["href"]) for t in tabs])
            for tab in tabs:
                self.assertGreaterEqual(tab["left"], 0)
                self.assertLessEqual(tab["right"], 390)
            self._assert_no_browser_errors(errors)
            await browser.close()

            # 老板的导航不变
            browser, page, errors = await self._open(playwright, me=OWNER)
            self.assertEqual(_nav.TABS, [t["label"] for t in await self._visible_tabs(page, "#tabbar a")])
            await browser.close()

    # ---------- 5. 套餐页顺序 ----------
    async def test_billing_page_puts_plans_right_after_balance_and_folds_the_ledger(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, width=1280, height=900, hash_="#/billing",
                api_overrides={"/api/billing": BILLING})
            await page.wait_for_selector("#billing-ledger")
            order = await page.evaluate(
                """() => { const ids=['billing-plans','billing-points','billing-prices','purchase-own','billing-ledger'];
                     const top=id=>document.getElementById(id).getBoundingClientRect().top+scrollY;
                     return {tops: ids.map(top), balance: document.querySelector('[data-billing-go-plans]')
                       .getBoundingClientRect().top+scrollY}; }""")
            self.assertEqual(sorted(order["tops"]), order["tops"])
            self.assertLess(order["balance"], order["tops"][0])
            # 没有申请时整块收成一行
            own = page.locator("#purchase-own")
            self.assertIsNotNone(await own.get_attribute("data-purchase-own-empty"))
            self.assertEqual(0, await own.locator("h2").count())
            self.assertIn("还没有购买申请", await own.inner_text())
            self.assertLess((await own.bounding_box())["height"], 90)
            # 账单明细默认收起，点开后有按月对账和点数明细；切页签不会又收起来
            ledger = page.locator("#billing-ledger")
            self.assertFalse(await ledger.evaluate("e=>e.open"))
            self.assertIn("共 2 笔", await ledger.locator("summary").inner_text())
            self.assertFalse(await page.get_by_text("按月对账（近半年）").is_visible())
            await ledger.locator("summary").click()
            self.assertTrue(await page.get_by_text("按月对账（近半年）").is_visible())
            self.assertTrue(await page.get_by_text("点数明细（充值 & 消耗记录）").is_visible())
            await page.locator("#billing-ledger .tb", has_text="消耗记录").click()
            await page.wait_for_function(
                "document.querySelector('#billing-ledger .tb.on')?.textContent.includes('消耗记录')")
            self.assertTrue(await page.locator("#billing-ledger").evaluate("e=>e.open"))
            # 主按钮：平滑滚到套餐区
            await page.evaluate("scrollTo(0,0); Element.prototype.scrollIntoView=function(o){window.__scrolled=[this.id,o&&o.behavior];}")
            button = page.locator("[data-billing-go-plans]")
            self.assertEqual("充点 / 升级套餐", (await button.inner_text()).strip())
            await button.click()
            self.assertEqual(["billing-plans", "smooth"], await page.evaluate("window.__scrolled"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_billing_page_lists_own_requests_in_full_when_there_are_some(self):
        request = {"id": 7, "plan_name": "创业版", "period_label": "月付", "status": "requested",
                   "price": 299, "points": 300, "created_at": NOW - 600}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, width=1280, height=900, hash_="#/billing",
                api_overrides={"/api/billing": BILLING, "/api/purchases": {"items": [request]}})
            await page.wait_for_selector("#billing-ledger")
            await page.evaluate("PURCHASE_CONTEXT.own={items:[" + json.dumps(request, ensure_ascii=False) + "]};billingView()")
            await page.wait_for_function("document.querySelector('#purchase-own h2')")
            own = page.locator("#purchase-own")
            self.assertIsNone(await own.get_attribute("data-purchase-own-empty"))
            self.assertIn("创业版 · 月付", await own.inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 6. 改密码移到「我的」 ----------
    async def test_password_form_lives_on_mine_page_for_every_role(self):
        puts = []
        async with async_playwright() as playwright:
            for me in (OWNER, STAFF, ROOT):
                browser, page, errors = await self._open(
                    playwright, me=me, width=1280, height=900, hash_="#/mine")
                page.on("request", lambda r: puts.append((urlparse(r.url).path, r.post_data_json))
                        if r.method == "PUT" else None)
                await page.wait_for_selector(".mine-who [data-mine-pw-toggle]")
                form = page.locator("#mine-pw-form")
                self.assertFalse(await form.is_visible())
                toggle = page.locator("[data-mine-pw-toggle]")
                self.assertEqual("🔑 修改密码", (await toggle.inner_text()).strip())
                await toggle.click()
                self.assertTrue(await form.is_visible())
                self.assertEqual("true", await toggle.get_attribute("aria-expanded"))
                # 账号卡片在最上面：表单排在所有功能卡片之前
                self.assertLess((await form.bounding_box())["y"],
                                (await page.locator(".hubcard").first.bounding_box())["y"])
                # 现有校验文案不变
                await page.fill("#tm-old", "old-password-1")
                await page.fill("#tm-new", "short")
                await page.get_by_role("button", name="💾 保存新密码").click()
                await page.wait_for_function(
                    "document.querySelector('#toast-stack')?.textContent.includes('密码至少需要 12 位')")
                self.assertEqual([], puts)
                await page.fill("#tm-new", "brand-new-pass-2026")
                await page.get_by_role("button", name="💾 保存新密码").click()
                await page.wait_for_function(
                    "document.querySelector('#toast-stack')?.textContent.includes('密码已修改')")
                self.assertEqual([("/api/auth/password",
                                   {"old": "old-password-1", "new": "brand-new-pass-2026"})], puts)
                self.assertFalse(await form.is_visible())
                self.assertEqual("", await page.input_value("#tm-new"))
                puts.clear()
                self._assert_no_browser_errors(errors)
                await browser.close()

    async def test_team_page_no_longer_shows_password_form(self):
        from tests import test_frontend_team_create as _team
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=_team.OWNER, width=1280, height=900, hash_="#/team",
                api_overrides={"/api/team": _team.TEAM})
            await page.wait_for_selector("[data-team-pw-hint]")
            self.assertEqual(0, await page.locator("#tm-old").count())
            self.assertEqual(0, await page.locator("#main h2", has_text="修改密码").count())
            hint = page.locator("[data-team-pw-hint] a")
            self.assertEqual("#/mine", await hint.get_attribute("href"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 源码合同 ----------
    def test_source_contract(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "static", "app.js"), encoding="utf-8") as handle:
            app_js = handle.read()
        self.assertNotIn("派出去的活", app_js.replace("派出去的活暂时没人", ""))
        self.assertIn('"boss":bossRedirectView', app_js)
        self.assertIn('location.replace("#/cockpit")', app_js)
        self.assertNotIn('route:"boss"', app_js)
        # 唯一保留的入口：平台管理员工作台的「各企业看板」(跨企业切换只有老板看板有)
        self.assertIn('if(ME?.role==="root") return bossDashboardView();', app_js)
        self.assertEqual(1, app_js.count('href="#/boss"'))
        self.assertIn('<a class="btn" href="#/boss">📈 各企业看板</a>', app_js)
        # 后端接口和看板服务层保留
        self.assertTrue(os.path.exists(os.path.join(root, "app", "bossdashboard.py")))
        self.assertIn('api("/auth/password",{method:"PUT"', app_js)


if __name__ == "__main__":
    unittest.main()
