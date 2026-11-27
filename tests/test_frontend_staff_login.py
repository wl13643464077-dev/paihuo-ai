"""登录页的店员版(从 /staff 跳过来带 ?next=/staff)和 next 跳转只认站内路径。"""
from __future__ import annotations

import json
import unittest
from urllib.parse import quote, urlparse

from playwright.async_api import async_playwright

from tests import test_frontend_browser_behavior as _browser

_launch_chromium = _browser._launch_chromium


class StaffLoginPageTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def _page(self, playwright):
        browser = await _launch_chromium(playwright, self.executable)
        page = await browser.new_page(viewport={"width": 375, "height": 812})
        errors = self._capture_unexpected_browser_errors(page)
        self.logins = []

        async def api_route(route):
            path = urlparse(route.request.url).path
            if path == "/api/auth/login":
                self.logins.append(json.loads(route.request.post_data or "{}"))
            await route.fulfill(status=200, content_type="application/json", body="{}")

        async def staff_route(route):
            await route.fulfill(status=200, content_type="text/html",
                                body="<!doctype html><div id='staff-home'>店员手机版</div>")

        await page.route("**/api/**", api_route)
        # 不能用 "**/staff"：它也会匹配 login.html?next=/staff
        await page.route(lambda url: urlparse(url).path == "/staff", staff_route)
        return browser, page, errors

    async def test_staff_version_hides_boss_entries_and_returns_to_staff(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._page(playwright)
            await page.goto(f"{self.base}/static/login.html?next=/staff")
            self.assertEqual("门店登录 · 派活", await page.title())
            self.assertIn("门店登录", await page.locator("#staff-head").inner_text())
            self.assertIn("店长、店员看今天的活", await page.locator("#staff-head").inner_text())
            self.assertFalse(await page.locator("#slogan").is_visible())
            self.assertFalse(await page.get_by_text("申请开通账号").first.is_visible())
            self.assertFalse(await page.get_by_text("游客参观").is_visible())
            self.assertFalse(await page.get_by_text("副账号").is_visible())
            self.assertEqual("忘了密码？找老板或店长重置",
                             (await page.locator("#staff-foot").inner_text()).strip())
            await page.fill("#u", "xiaoli")
            await page.fill("#p", "secret-password-1")
            await page.locator("#login-btn").click()
            await page.wait_for_selector("#staff-home")
            self.assertEqual("/staff", urlparse(page.url).path)
            self.assertEqual([{"username": "xiaoli", "password": "secret-password-1"}], self.logins)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_plain_login_keeps_boss_version(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._page(playwright)
            await page.goto(f"{self.base}/static/login.html")
            self.assertTrue(await page.locator("#slogan").is_visible())
            self.assertTrue(await page.get_by_text("游客参观").is_visible())
            self.assertFalse(await page.locator("#staff-head").is_visible())
            self.assertEqual("", await page.evaluate("NEXT"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_next_only_accepts_same_site_relative_paths(self):
        cases = {
            "/staff": "/staff",
            "/staff#task-12": "/staff#task-12",
            "//evil.example/staff": "",
            "/\\evil.example": "",
            "https://evil.example/staff": "",
            "javascript:alert(1)": "",
            "/staff?x=//evil.example": "",
            "staff": "",
            "/%2F%2Fevil.example": "",
        }
        async with async_playwright() as playwright:
            browser, page, errors = await self._page(playwright)
            for raw, want in cases.items():
                with self.subTest(next=raw):
                    await page.goto(f"{self.base}/static/login.html?next={quote(raw, safe='')}")
                    self.assertEqual(want, await page.evaluate("NEXT"))
            # 被拒的 next 不会把人带走：登录后照常回首页
            await page.goto(f"{self.base}/static/login.html?next={quote('//evil.example/x', safe='')}")
            self.assertFalse(await page.locator("#staff-head").is_visible())
            self.assertEqual("/", await page.evaluate("afterLogin()"))
            self._assert_no_browser_errors(errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
