"""第 1 期首日体验:5 个一级入口的导航、「今天」页与手机端细节(真实浏览器渲染)。"""
from __future__ import annotations

import json
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright

# 只导入模块,不把对方的 TestCase 类放进本模块命名空间(否则会被重复发现执行)。
from tests import test_frontend_browser_behavior as _browser

_launch_chromium = _browser._launch_chromium

OWNER = {
    "id": 21,
    "username": "tea-owner",
    "role": "owner",
    "tenant": "阿花奶茶",
    "modules": ["content", "avatar", "library"],
    "all_modules": [],
}
MEMBER_NO_CONTENT = {
    "id": 22,
    "username": "clerk",
    "role": "member",
    "tenant": "阿花奶茶",
    "modules": ["library"],
    "all_modules": [],
}
STATE = {
    "inbox": [
        {"id": 12, "status": "awaiting_review", "title": "国庆新品推文",
         "brief": {"direction": "国庆"}},
        {"id": 13, "status": "failed", "title": "(未产出标题)",
         "brief": {"direction": "秋季上新"}},
    ],
    "notifications": [
        {"id": 1, "title": "巡店报告出来了", "body": "三里屯店 86 分",
         "link": "#/inspections", "created_at": 1700000000},
    ],
    "jobs": [], "items": [], "total": 0, "balance": 120, "plan": "标准版",
    "setup": {}, "trio": {},
}
INSPECTIONS = {
    "items": [],
    "summary": {"open_issues": 4, "overdue_actions": 1,
                "pending_rechecks": 2, "total_branches": 6},
}
TABS = ["今天", "派活", "门店", "获客", "我的"]


class FrontendNavMobileTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def _open(self, playwright, *, me=OWNER, width=390, height=844,
                    hash_="#/", init_script=""):
        api = {
            "/api/auth/me": me,
            "/api/meta": {"app": {"name": "派活", "slogan": "测试"}, "stations": []},
            "/api/state": STATE,
            "/api/employees": [],
            "/api/depts": [],
            "/api/inspections": INSPECTIONS,
            "/api/notifications": {"items": [], "total": 0},
        }
        browser = await _launch_chromium(playwright, self.executable)
        page = await browser.new_page(viewport={"width": width, "height": height})
        errors = self._capture_unexpected_browser_errors(page)
        await page.add_init_script(
            "window.EventSource = class { constructor(u){this.url=u;} close(){} };"
            + init_script
        )

        async def api_route(route):
            payload = api.get(urlparse(route.request.url).path, {})
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        await page.route("**/api/**", api_route)
        await page.goto(f"{self.base}/static/index.html{hash_}")
        await page.wait_for_function(
            "document.querySelectorAll('#tabbar a').length>0"
        )
        return browser, page, errors

    async def _visible_tabs(self, page, selector):
        return await page.evaluate(
            """(sel) => [...document.querySelectorAll(sel)]
                .filter(a => a.offsetParent !== null)
                .map(a => { const r = a.getBoundingClientRect();
                  return {label: a.querySelector('.nav-tx').textContent,
                          href: a.getAttribute('href'), on: a.classList.contains('on'),
                          left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                          height: r.height}; })""",
            selector,
        )

    async def test_phone_width_shows_five_tabs_on_one_screen_without_horizontal_scroll(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright)
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('等我处理的')"
            )
            tabs = await self._visible_tabs(page, "#tabbar a")
            self.assertEqual(TABS, [t["label"] for t in tabs])
            for tab in tabs:
                self.assertGreaterEqual(tab["left"], 0)
                self.assertLessEqual(tab["right"], 390)
                self.assertGreaterEqual(tab["height"], 44)
                self.assertLessEqual(tab["bottom"], 844)
            self.assertEqual([True, False, False, False, False], [t["on"] for t in tabs])
            # 顶部导航在手机上不再重复显示 5 个入口,只留退出
            self.assertEqual([], await self._visible_tabs(page, "#nav a"))
            self.assertTrue(await page.locator("#nav .navlogout").is_visible())
            widths = await page.evaluate(
                "({doc: document.documentElement.scrollWidth, nav: document.querySelector('#tabbar').scrollWidth,"
                " navClient: document.querySelector('#tabbar').clientWidth})"
            )
            self.assertLessEqual(widths["doc"], 390)
            self.assertLessEqual(widths["nav"], widths["navClient"])
            # 反馈浮钮必须在 Tab 栏上方,不能盖住入口
            overlap = await page.evaluate(
                """() => { const fb = document.querySelector('#fb-btn').getBoundingClientRect();
                  const bar = document.querySelector('#tabbar').getBoundingClientRect();
                  return fb.bottom <= bar.top; }"""
            )
            self.assertTrue(overlap)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_sub_pages_highlight_their_parent_entry_and_every_route_is_kept(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, hash_="#/notifications")
            await page.wait_for_function(
                "document.querySelector('#tabbar a.on')?.dataset.nav==='mine'"
            )
            mapping = await page.evaluate(
                """() => Object.fromEntries(
                    ["", "new", "job", "delivery", "tasks", "meetings", "experts",
                     "inspections", "boss", "production", "tools", "censor", "avatar",
                     "schedules", "channels", "billing", "team", "company", "profiles",
                     "assets", "knowledge", "notifications", "trash", "guide", "admin",
                     "settings"].map(k => [k, navGroupOf(k)]))"""
            )
            expected = {
                "": "today",
                "new": "dispatch", "job": "dispatch", "delivery": "dispatch",
                "tasks": "dispatch", "meetings": "dispatch", "experts": "dispatch",
                "inspections": "store", "boss": "store", "production": "store",
                "tools": "growth", "censor": "growth", "avatar": "growth",
                "schedules": "growth", "channels": "growth",
                "billing": "mine", "team": "mine", "company": "mine", "profiles": "mine",
                "assets": "mine", "knowledge": "mine", "notifications": "mine",
                "trash": "mine", "guide": "mine", "admin": "mine", "settings": "mine",
            }
            self.assertEqual(expected, mapping)
            # 中转页上的每张卡片都指向一个真实存在的路由(链接/深链不断)
            missing = await page.evaluate(
                """() => NAV_GROUPS.flatMap(g => g.cards.flatMap(c => [c, ...(c.subs||[])]))
                    .filter(c => c.route !== undefined && !(c.route in routes))
                    .map(c => c.route)"""
            )
            self.assertEqual([], missing)
            # 子页面切换后高亮跟着走
            await page.evaluate("location.hash='#/tools/hot'")
            await page.wait_for_function(
                "document.querySelector('#tabbar a.on')?.dataset.nav==='growth'"
            )
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_hub_cards_follow_existing_permissions(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=MEMBER_NO_CONTENT, hash_="#/mine"
            )
            await page.wait_for_function(
                "document.querySelector('#main .hubgrid')"
            )
            tabs = await self._visible_tabs(page, "#tabbar a")
            labels = [t["label"] for t in tabs]
            # 没有内容/数字人权限的店员:获客入口整个不出现;门店只有巡店一张卡,直接进巡店
            self.assertNotIn("获客", labels)
            store = next(t for t in tabs if t["label"] == "门店")
            self.assertEqual("#/inspections", store["href"])
            cards = await page.evaluate(
                "[...document.querySelectorAll('#main .hubcard .hc-t')].map(x=>x.textContent)"
            )
            self.assertIn("我的资料库", cards)
            self.assertIn("套餐", cards)
            for owner_only in ("团队与权限", "企业档案", "回收站", "员工进修管理", "后台"):
                self.assertNotIn(owner_only, cards)
            self.assertTrue(await page.locator("#main button", has_text="退出登录").is_visible())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_today_page_puts_daily_items_first_and_folds_the_guides(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright,
                width=1280, height=900,
                init_script="window.PH_ONBOARDING={card:()=>'<div id=\"ob-hook\">新手引导卡</div>'};",
            )
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('今天的门店')"
            )
            info = await page.evaluate(
                """() => ({
                  first: document.querySelector('#main').firstElementChild?.id,
                  rows: [...document.querySelectorAll('.today-todo .todo-row b')].map(x=>x.textContent),
                  stats: [...document.querySelectorAll('.today-stats b')].map(x=>x.textContent),
                  post: document.querySelector('.today-growth a.today-post')?.getAttribute('href'),
                  fold: !!document.querySelector('details.guide-fold'),
                  foldOpen: document.querySelector('details.guide-fold')?.open,
                  floors: document.querySelectorAll('#main .deptsec,#main .room').length,
                  money: document.querySelector('.today-money')?.textContent,
                  topNav: [...document.querySelectorAll('#nav a.nav-top')].filter(a=>a.offsetParent!==null).length,
                  tabbarShown: getComputedStyle(document.querySelector('#tabbar')).display,
                })"""
            )
            self.assertEqual("ob-hook", info["first"])
            self.assertEqual(
                ["等您拍板:国庆新品推文", "失败了:秋季上新", "2 条门店整改等您审核", "巡店报告出来了"],
                info["rows"],
            )
            self.assertEqual(["0", "4", "1"], info["stats"])
            self.assertEqual("#/tools/hot", info["post"])
            self.assertTrue(info["fold"])
            self.assertFalse(info["foldOpen"])
            self.assertEqual(0, info["floors"])  # 行业专家楼层已挪去「派活 → 找行业专家」
            self.assertIn("余额 120 点", info["money"])
            self.assertEqual(5, info["topNav"])  # 桌面仍是顶部导航
            self.assertEqual("none", info["tabbarShown"])
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_phone_inputs_are_16px_and_dialog_locks_background_scroll(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright)
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('等我处理的')"
            )
            sizes = await page.evaluate(
                """() => { const box = document.createElement('div');
                  box.innerHTML = '<input id="t1" style="font-size:13px"><select id="t2"></select><textarea id="t3"></textarea>';
                  document.querySelector('#main').appendChild(box);
                  return ['t1','t2','t3'].map(id => getComputedStyle(document.getElementById(id)).fontSize); }"""
            )
            self.assertEqual(["16px", "16px", "16px"], sizes)
            before = await page.evaluate("getComputedStyle(document.documentElement).overflowY")
            self.assertNotEqual("hidden", before)
            # 不能直接返回 Promise,否则 evaluate 会一直等对话框被关掉
            await page.evaluate("() => { window.__dlg = uiConfirm('确认一下?'); return true; }")
            await page.wait_for_selector("#ui-dialog")
            locked = await page.evaluate("getComputedStyle(document.documentElement).overflowY")
            self.assertEqual("hidden", locked)
            await page.locator("#ui-dialog [data-dialog-cancel]").click()
            await page.wait_for_function("!document.querySelector('#ui-dialog')")
            after = await page.evaluate("getComputedStyle(document.documentElement).overflowY")
            self.assertNotEqual("hidden", after)
            # 「新手上路」里的「不再提示」点击区至少 44px
            await page.evaluate("document.querySelector('details.guide-fold').open=true")
            dismiss = await page.evaluate(
                """() => [...document.querySelectorAll('details.guide-fold .dismiss')]
                    .map(b => { const r = b.getBoundingClientRect(); return Math.min(r.width, r.height); })"""
            )
            self.assertTrue(dismiss)
            self.assertTrue(all(side >= 44 for side in dismiss), dismiss)
            self._assert_no_browser_errors(errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
