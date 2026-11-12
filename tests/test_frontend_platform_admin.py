"""第五轮 C 组:平台工作台、平台运营页、充点提示、标签标题、看板待接入折叠、店长发给 TA 文案、登录页页脚。"""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from app import auth
from tests import test_frontend_browser_behavior as _browser
from tests import test_frontend_team_create as _team

_launch_chromium = _browser._launch_chromium

ROOT = {"id": 1, "username": "boss", "role": "root", "tenant": "派活平台",
        "modules": ["content", "avatar", "library", "restaurant"], "all_modules": [],
        "industries": ["restaurant"]}
OWNER = _team.OWNER
META = {"app": {"name": "派活", "slogan": "测试"}, "stations": []}
NOW = time.time()
PLATFORM_TEAM = {
    "tenant": {"name": "派活平台", "balance": 0}, "users": [], "all_modules": [],
    "industry_employees": [], "job_titles": [], "my_job_title": "", "is_admin": True,
    "can_allocate": True, "store_branches": {"items": [], "truncated": False},
    "guests": [],
    "applies": [
        {"id": 5, "phone": "13800000001", "name": "张老板", "company": "张记面馆", "note": "",
         "created_at": NOW - 3600, "status": 0, "username": None, "tenant_id": None},
        {"id": 4, "phone": "13800000002", "name": "李老板", "company": "李记", "note": "",
         "created_at": NOW - 7200, "status": 1, "username": "13800000002", "tenant_id": 3},
    ],
    "all_industries": [
        {"key": "restaurant", "name": "餐饮产业部", "short_label": "餐饮", "emoji": "🍜"},
        {"key": "convenience", "name": "便利店行业痛点数字员工", "short_label": "便利店", "emoji": "🏪"},
    ],
    "tenants": [
        {"id": 1, "name": "派活平台", "n_users": 2, "balance": 0, "enabled": 1, "industries": []},
        {"id": 2, "name": "王记火锅", "n_users": 4, "balance": 8, "enabled": 1,
         "industries": ["restaurant"], "plan": "创业版", "plan_expires": NOW + 3 * 86400},
        {"id": 3, "name": "李记便利", "n_users": 1, "balance": 600, "enabled": 1,
         "industries": ["convenience"], "plan": "企业版", "plan_expires": NOW + 90 * 86400},
    ],
}


class PlatformAdminFrontendTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def _open(self, playwright, hash_, payloads=None, *, me=ROOT, post=None, delay=None, width=1100):
        browser = await _launch_chromium(playwright, self.executable)
        context = await browser.new_context(viewport={"width": width, "height": 900})
        page = await context.new_page()
        errors = self._capture_unexpected_browser_errors(page)
        await page.add_init_script("window.EventSource = class { constructor(){} close(){} };")
        api = {"/api/auth/me": me, "/api/meta": META,
               "/api/state": {"inbox": [], "notifications": [], "jobs": [], "balance": 500},
               "/api/employees": [], "/api/depts": [], "/api/team": PLATFORM_TEAM,
               "/api/team/apply-config": {"auto": False, "trial_points": 20, "daily_cap": 20},
               **(payloads or {})}
        self.posts = []
        delay = delay or {}

        async def api_route(route):
            path = urlparse(route.request.url).path
            if path in delay:
                await asyncio.sleep(delay[path])
            if route.request.method in ("POST", "PUT") and post and path in post:
                self.posts.append((path, json.loads(route.request.post_data or "{}")))
                payload = post[path]
            else:
                payload = api.get(path, {})
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        await page.route("**/api/**", api_route)
        await page.goto(f"{self.base}/static/index.html{hash_}")
        return browser, page, errors

    async def test_root_today_is_platform_workbench_and_links_to_platform_page(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/")
            await page.wait_for_selector("[data-platform-today]")
            text = await page.locator("#main").inner_text()
            self.assertIn("平台工作台", text)
            self.assertNotIn("老板好", text)
            self.assertNotIn("门店", text)
            self.assertIn("1 条开通申请待处理", text)
            self.assertIn("1 家企业套餐快到期", text)
            self.assertIn("1 家企业余额不足 20 点", text)
            self.assertIn("王记火锅", text)
            self.assertIn("共 2 家在用", text)
            self.assertEqual("平台工作台 · 派活", await page.title())
            # 开新企业:直接到平台运营页并打开表单,行业用短名
            await page.get_by_role("button", name="➕ 开新企业").click()
            await page.wait_for_selector("#tt-inds")
            self.assertEqual("#/platform", await page.evaluate("location.hash"))
            self.assertEqual("平台运营 · 派活", await page.title())
            chips = [c.strip() for c in await page.locator("#tt-inds .chip").all_inner_texts()]
            self.assertEqual(["🍜 餐饮", "🏪 便利店"], chips)
            main = await page.locator("#main").inner_text()
            self.assertIn("企业管理（仅平台可见）", main)
            self.assertIn("开通申请（1 条待处理）", main)
            self.assertNotIn("租户", main)
            self.assertNotIn("餐饮产业部", main)
            self.assertNotIn("便利店行业痛点数字员工", main)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_grant_points_shows_clear_toast(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/platform",
                post={"/api/team/tenants/2/grant": {"balance": 308}})
            await page.wait_for_selector("#tm-indrow-2")
            await page.locator("#tm-indrow-2").get_by_role("button", name="💎 充点").click()
            await page.wait_for_selector("#ui-dialog")
            await page.fill("#ui-dialog-field", "300")
            await page.locator("#ui-dialog [data-dialog-ok]").click()
            await page.wait_for_function(
                "document.querySelector('#toast-stack')?.textContent.includes('已给 王记火锅 充 300 点，现在余额 308 点')")
            self.assertEqual([("/api/team/tenants/2/grant", {"points": 300, "reason": "平台充值"})], self.posts)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_mine_hub_and_team_page_link_to_platform(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/mine")
            await page.wait_for_selector(".hubcard", state="attached")
            self.assertIn("平台运营", await page.locator("#main").inner_text())
            await page.evaluate("location.hash='#/team'")
            await page.wait_for_selector("[data-team-platform-link]")
            self.assertEqual("团队与权限 · 派活", await page.title())
            self.assertEqual(0, await page.locator("#tm-indrow-2").count())
            await page.locator("[data-team-platform-link] a").click()
            await page.wait_for_selector("#tm-indrow-2")
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_non_root_platform_route_shows_no_data(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/platform", me=OWNER)
            await page.wait_for_selector("text=这一页只有平台方能看")
            self.assertEqual(0, await page.locator("#tm-indrow-2").count())
            # O6-4:看不了的页面,标签不写页面名
            self.assertEqual("无权访问 · 派活", await page.title())
            # 切走再切回来:切换那一刻也是「无权访问」
            await page.evaluate("location.hash='#/'")
            await page.wait_for_function("document.title === '今天 · 派活'")
            title = await page.evaluate("""() => new Promise(resolve => {
                window.addEventListener('hashchange', () => resolve(document.title), {once: true});
                location.hash = '#/platform';
            })""")
            self.assertEqual("无权访问 · 派活", title)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_forbidden_route_title_is_no_access(self):
        """O6-4:服务端 403 的页面(如非平台账号直接打开 #/admin)标签也写「无权访问」。"""
        async with async_playwright() as playwright:
            browser = await _launch_chromium(playwright, self.executable)
            page = await browser.new_page()
            await page.add_init_script("window.EventSource = class { constructor(){} close(){} };")

            async def api_route(route):
                path = urlparse(route.request.url).path
                if path.startswith("/api/admin/"):
                    await route.fulfill(status=403, content_type="application/json",
                                        body=json.dumps({"detail": "需要平台管理员权限"}, ensure_ascii=False))
                    return
                payload = {"/api/auth/me": OWNER, "/api/meta": META,
                           "/api/state": {"inbox": [], "notifications": [], "jobs": [], "balance": 500}}.get(path, {})
                await route.fulfill(status=200, content_type="application/json",
                                    body=json.dumps(payload, ensure_ascii=False))

            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/admin")
            await page.wait_for_selector("text=这个页面需要更高权限")
            self.assertEqual("无权访问 · 派活", await page.title())
            await browser.close()

    async def test_platform_page_order_and_heading(self):
        """P6-1/P6-2:企业管理在最上面,开通申请其次,对客联系方式放最后;有和其他页一样的 H2 大标题。"""
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/platform")
            await page.wait_for_selector("#tm-indrow-2")
            heading = page.locator("#main > .hubhead h2")
            self.assertIn("平台运营", await heading.inner_text())
            self.assertIn("开通申请", await page.locator("#main > .hubhead .sub").inner_text())
            self.assertEqual(0, await page.locator("#main > .notice").count())
            order = await page.evaluate("""() => [...document.querySelectorAll('#main h2, #main h3')]
                .map(h => h.textContent.trim())""")
            idx = lambda word: next(i for i, t in enumerate(order) if word in t)
            self.assertEqual(0, idx("平台运营"))
            self.assertLess(idx("企业管理"), idx("开通申请"))
            self.assertLess(idx("开通申请"), idx("对客联系方式"))
            self.assertEqual(len(order) - 1, idx("对客联系方式"))
            self.assertIn("上面「企业管理」", await page.locator("#pf-applies").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_title_switches_at_start_of_navigation(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/team", delay={"/api/admin/overview": 1.5})
            await page.wait_for_selector("[data-team-platform-link]")
            # O6-3:改 hash 后同一刻(不等渲染、不等请求)标签就换成目标页的名字
            #(测试的监听器注册在 app.js 之后,拿到的就是 hashchange 刚派发完、任何请求返回之前的标题)
            title = await page.evaluate("""() => new Promise(resolve => {
                window.addEventListener('hashchange', () => resolve(document.title), {once: true});
                location.hash = '#/admin';
            })""")
            self.assertEqual("后台 · 派活", title)
            self.assertEqual(0, await page.locator("#adm-features").count())  # 还在加载
            labels = await page.evaluate(
                "[ROUTE_LABELS['staff-tasks'], ROUTE_LABELS.checklists, ROUTE_LABELS['store-rank'], ROUTE_LABELS.team]")
            self.assertEqual(["派给店员", "开闭店清单", "门店排行", "团队与权限"], labels)
            await browser.close()

    async def test_forced_password_page_title(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/", me={**OWNER, "must_change_password": True})
            await page.wait_for_selector("#forced-pw-form")
            self.assertEqual("设置新密码 · 派活", await page.title())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_dispatch_card_does_not_promise_ai_photo_check(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/", me=OWNER)
            desc = await page.evaluate(
                "NAV_GROUPS.find(g=>g.key==='dispatch').cards.find(c=>c.route==='staff-tasks').desc")
            self.assertEqual("把活派给店里的人，店员拍照交差，您在手机或电脑上审核", desc)
            await browser.close()

    async def test_boss_dashboard_folds_all_pending_business_metrics(self):
        metric = lambda i: {"label": f"指标{i}", "availability": False, "source_required": "收银系统"}
        summary = {
            "scope": {"tenant_id": 2, "tenant_name": "王记火锅", "industry_key": "restaurant",
                      "industry_name": "餐饮产业部", "industry_emoji": "🍜"},
            "period": {"days": 7}, "task_metrics": {}, "efficiency_metrics": {}, "risk_metrics": {},
            "inspection_metrics": {"availability": False, "backlog": {}},
            "business_metrics": {"metrics": [metric(i) for i in range(9)]}, "trend": [], "employees": [],
            "recent_activity": [], "can_open_records": True,
        }
        overrides = {
            "/api/boss/dashboard/scopes": {"can_cross_tenant": False, "tenants": [{
                "id": 2, "name": "王记火锅", "industries": [
                    {"key": "restaurant", "name": "餐饮产业部", "short_label": "餐饮", "emoji": "🍜"}]}]},
            "/api/boss/dashboard/summary": summary,
        }
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/boss", overrides, me=OWNER)
            await page.wait_for_selector("[data-boss-metrics-fold]")
            fold = page.locator("[data-boss-metrics-fold]")
            self.assertFalse(await fold.evaluate("e=>e.open"))
            self.assertIn("接上收银数据后显示经营指标", await fold.locator("summary").inner_text())
            self.assertFalse(await page.get_by_text("指标3").is_visible())
            await fold.locator("summary").click()
            self.assertTrue(await page.get_by_text("指标3").is_visible())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_manager_send_text_mentions_dispatch_and_review(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/team", {"/api/team": _team.TEAM}, me=OWNER, width=375,
                post={"/api/team/users": {"id": 32, "username": "dianzhang",
                                          "initial_password": "k7mn2pqr8stv3wxy", "branch_ids": [4]}})
            await page.wait_for_selector("text=新建账号")
            await page.get_by_role("button", name="➕ 新建账号").click()
            await page.wait_for_selector("#tm-create")
            await page.locator('.tm-kind[data-k="manager"]').click()
            await page.fill("#tm-u", "dianzhang")
            await page.locator(".tm-check", has_text="中山路店").click()
            await page.locator("#tm-create-ok").click()
            await page.wait_for_selector("#tm-sendcard")
            text = await page.locator("#tm-send-text").inner_text()
            self.assertTrue(text.startswith("【王记火锅】给您开了派活账号，用来给店员派活、审核交差：\n"), text)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_login_footer_punctuation(self):
        async with async_playwright() as playwright:
            browser = await _launch_chromium(playwright, self.executable)
            page = await browser.new_page(viewport={"width": 375, "height": 812})
            await page.route("**/api/**", lambda route: route.fulfill(
                status=200, content_type="application/json", body="{}"))
            await page.goto(f"{self.base}/static/login.html")
            foot = (await page.locator("#boss-foot").inner_text()).strip()
            self.assertIn("副账号：开通与重置密码都请找您的企业主", foot)
            self.assertNotIn("· ·", foot)
            self.assertNotIn(":", foot)
            self.assertNotIn("?", foot)
            await browser.close()


class AllIndustriesShortLabelTests(unittest.TestCase):
    def test_all_industries_carry_short_label(self):
        rows = auth.all_industries()
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["short_label"])
            self.assertFalse(row["short_label"].endswith("产业部"))
            self.assertFalse(row["short_label"].endswith("行业痛点数字员工"))


if __name__ == "__main__":
    unittest.main()
