"""浏览器行为:存储被禁用不崩首页、退出登录清业务缓存、公众号复制降级。"""
from __future__ import annotations

import json
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright

# 只导入模块,不把对方的 TestCase 类放进本模块命名空间(否则会被重复发现执行)。
from tests import test_frontend_browser_behavior as _browser

_launch_chromium = _browser._launch_chromium


OWNER = {
    "id": 9,
    "username": "shop-owner",
    "role": "owner",
    "tenant": "浏览器测试门店",
    "modules": ["content", "avatar", "library"],
    "all_modules": [],
}

# 首页(办公室)渲染所需的最小接口数据
HOME_API = {
    "/api/auth/me": OWNER,
    "/api/meta": {"app": {"name": "派活", "slogan": "测试"}, "stations": []},
    "/api/state": {"inbox": [], "notifications": [], "jobs": [], "items": [],
                   "total": 0, "balance": 100, "setup": {}, "trio": {}},
    "/api/employees": [],
    "/api/depts": [],
}

BUSINESS_KEYS = {
    "tools_cache_user_9": json.dumps({"leads": [{"phone": "13800000000"}], "t": 1}),
    "paihuo:purchase-contact": "13900000000",
    "paihuo:pending-purchase": json.dumps({"plan": "biz", "period": "year"}),
    "brief_draft_user_9": json.dumps({"topic": "秋季上新"}),
    "prefill_brief": json.dumps({"topic": "爆款"}),
    "paihuo.agentTeam.v1": json.dumps({"query": "帮我写活动文案"}),
    "paihuo:pending:inspectionbranchimport:user:9:x": "{}",
    "tools_cache": "{}",
}
UI_PREFS = {
    "deptopen_浏览器测试门店_content": "0",
    "trio_hide_浏览器测试门店": "1",
    "howto_hide_浏览器测试门店": "1",
    "ob_hide_浏览器测试门店": "1",
    "mp_theme": "green",
}


class FrontendStorageAndCopyTests(unittest.IsolatedAsyncioTestCase):
    # 复用现有浏览器用例的静态文件服务与报错收集
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def _open_app(self, playwright, init_script: str = ""):
        browser = await _launch_chromium(playwright, self.executable)
        page = await browser.new_page()
        errors = self._capture_unexpected_browser_errors(page)
        await page.add_init_script(
            """
            window.EventSource = class {
              constructor(url) { this.url=url; }
              close() { this.closed=true; }
            };
            """ + init_script
        )

        async def api_route(route):
            path = urlparse(route.request.url).path
            payload = HOME_API.get(path, {})
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload, ensure_ascii=False),
            )

        async def login_route(route):
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body="<!doctype html><title>登录</title><main>登录</main>",
            )

        await page.route("**/api/**", api_route)
        await page.route("**/login", login_route)
        return browser, page, errors

    async def test_home_renders_when_storage_access_throws(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open_app(
                playwright,
                """
                Object.defineProperty(window, 'localStorage', {
                  configurable: true,
                  get() { throw new DOMException('storage disabled', 'SecurityError'); }
                });
                """,
            )
            await page.goto(f"{self.base}/static/index.html#/")
            await page.wait_for_function(
                "document.querySelector('#nav')?.textContent.includes('退出')"
            )
            await page.wait_for_timeout(300)
            # 各处读写走安全封装:读返回 null、写返回 false,都不抛错
            result = await page.evaluate(
                """() => {
                  const wrote = lsSet('deptopen_x_content', '1');
                  lsDel('deptopen_x_content');
                  toggleDept('content');
                  return {read: lsGet('mp_theme'), wrote,
                          open: deptIsOpen('content'),
                          cards: [trioCard(), howtoCard(), obCard()].map(x => typeof x)};
                }"""
            )
            self.assertEqual(
                {"read": None, "wrote": False, "open": True,
                 "cards": ["string", "string", "string"]},
                result,
            )
            await page.evaluate("guideReset()")
            await page.evaluate("clearBusinessStorage()")
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_logout_clears_business_cache_but_keeps_ui_preferences(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open_app(playwright)
            logout_calls: list[str] = []
            page.on(
                "request",
                lambda request: logout_calls.append(request.url)
                if urlparse(request.url).path == "/api/auth/logout" else None,
            )
            await page.goto(f"{self.base}/static/index.html#/")
            await page.wait_for_function(
                "document.querySelector('#nav')?.textContent.includes('退出')"
            )
            await page.evaluate(
                """(items) => { for (const [k, v] of Object.entries(items))
                    localStorage.setItem(k, v); }""",
                {**BUSINESS_KEYS, **UI_PREFS},
            )
            await page.locator("#nav .navlogout").click()
            await page.wait_for_url("**/login", timeout=3000)
            self.assertEqual(1, len(logout_calls))
            remaining = await page.evaluate(
                """() => Object.fromEntries(Object.keys(localStorage)
                    .map(k => [k, localStorage.getItem(k)]))"""
            )
            self.assertEqual(UI_PREFS, remaining)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def _mp_copy(self, execcommand_result: str):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open_app(
                playwright,
                """
                delete window.ClipboardItem;
                window.__writeText = [];
                Object.defineProperty(navigator, 'clipboard', {
                  configurable: true,
                  value: { writeText: (s) => { window.__writeText.push(s);
                                               return Promise.resolve(); } },
                });
                document.execCommand = function(cmd) {
                  if (cmd !== 'copy') return false;
                  const sel = getSelection();
                  const holder = document.createElement('div');
                  if (sel.rangeCount) holder.appendChild(sel.getRangeAt(0).cloneContents());
                  window.__copied = holder.innerHTML;
                  return %s;
                };
                """ % execcommand_result,
            )
            await page.goto(f"{self.base}/static/index.html#/")
            await page.wait_for_function(
                "document.querySelector('#nav')?.textContent.includes('退出')"
            )
            outcome = await page.evaluate(
                """async () => {
                  document.body.insertAdjacentHTML('beforeend', '<div id="mp-report"></div>');
                  MP_CUR = {job: 1, theme: 'orange', html:
                    "<section style='color:rgb(255, 0, 0)'><b>秋季上新</b>" +
                    "<img src='/static/img/avatar-1152.webp' onload='window.__pwned=1'>" +
                    "<a href='javascript:window.__pwned=3'>链接</a>" +
                    "<script>window.__pwned=2<\\/script></section>"};
                  await mpCopy();
                  await new Promise(r => setTimeout(r, 200));
                  const manual = document.querySelector('#mp-manual-copy');
                  return {copied: window.__copied || '', pwned: window.__pwned || 0,
                          writeText: window.__writeText,
                          leftovers: document.querySelectorAll('[aria-hidden=true][contenteditable]').length,
                          manual: manual ? manual.innerHTML : null,
                          report: document.querySelector('#mp-report').textContent};
                }"""
            )
            self._assert_no_browser_errors(errors)
            await browser.close()
            return outcome

    async def test_mp_copy_without_clipboard_item_copies_rich_text_via_selection(self):
        outcome = await self._mp_copy("true")
        self.assertIn("秋季上新", outcome["copied"])
        self.assertIn("color:rgb(255, 0, 0)", outcome["copied"])
        for dangerous in ("<script", "onload", "javascript:"):
            self.assertNotIn(dangerous, outcome["copied"])
        self.assertEqual(0, outcome["pwned"])
        self.assertEqual([], outcome["writeText"])   # 不再退回复制 HTML 源码
        self.assertEqual(0, outcome["leftovers"])     # 隐藏容器用完即删
        self.assertIsNone(outcome["manual"])

    async def test_mp_copy_falls_back_to_long_press_hint(self):
        outcome = await self._mp_copy("false")
        self.assertIn("长按下面的文章", outcome["report"])
        self.assertIn("秋季上新", outcome["manual"])
        self.assertNotIn("onload", outcome["manual"])
        self.assertNotIn("<script", outcome["manual"])
        self.assertEqual(0, outcome["pwned"])
        self.assertEqual([], outcome["writeText"])


if __name__ == "__main__":
    unittest.main()
