"""Headless-browser regression for the navigation race reported in production."""
from __future__ import annotations

import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import unittest
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
SERVE_ROOT = ROOT


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return


def _browser_executable() -> str:
    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or ""
    candidates = (
        configured,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    )
    return next((path for path in candidates if path and os.path.isfile(path)), "")


class FrontendBrowserBehaviorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.executable = _browser_executable()
        if not cls.executable:
            raise unittest.SkipTest("no system Chromium executable available")
        handler = partial(_QuietHandler, directory=str(SERVE_ROOT))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None):
            cls.server.shutdown()
            cls.server.server_close()
        if getattr(cls, "thread", None):
            cls.thread.join(timeout=2)

    def _capture_unexpected_browser_errors(self, page) -> list[str]:
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))

        def record_console_error(message):
            if message.type == "error":
                errors.append(f"console.error: {message.text}")

        page.on("console", record_console_error)
        return errors

    def _assert_no_browser_errors(self, errors: list[str]):
        self.assertEqual([], errors, "\n".join(errors))

    async def test_late_old_route_cannot_cover_the_new_page(self):
        slow_task_requested = asyncio.Event()
        payloads = {
            "/api/auth/me": {
                "id": 7,
                "username": "browser-test",
                "role": "owner",
                "tenant": "浏览器测试企业",
                "modules": ["content", "avatar", "library"],
                "all_modules": [],
            },
            "/api/meta": {},
            "/api/state": {"jobs": [], "inbox": [], "notifications": []},
            "/api/employees": [],
            "/api/billing": {
                "balance": 20,
                "is_platform": False,
                "recharged": 20,
                "spent": 0,
                "txn_n": 0,
                "log": [],
                "prices": {},
                "plans": [],
                "plan": "",
                "plan_expires": None,
            },
        }
        task_page = {
            "items": [{
                "key": "expert:999",
                "record_id": 999,
                "kind": "expert",
                "kind_label": "数字员工任务",
                "title": "SLOW OLD TASK MUST NEVER WIN",
                "assignee": "测试员工",
                "status": "running",
                "status_group": "active",
                "status_label": "进行中",
                "source_label": "测试来源",
                "target_route": "#/tasks/999",
                "source_route": "",
                "created_at": 1,
                "updated_at": 1,
            }],
            "counts": {
                "all": 1, "open": 1, "active": 1, "waiting": 0,
                "done": 0, "failed": 0, "cancelled": 0,
            },
            "kind_counts": {"expert": 1},
            "has_more": False,
            "next_offset": None,
        }

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=self.executable,
                headless=True,
            )
            page = await browser.new_page()
            browser_errors = self._capture_unexpected_browser_errors(page)
            await page.add_init_script(
                """
                window.__eventSources = [];
                window.EventSource = class {
                  constructor(url) { this.url=url; window.__eventSources.push(this); }
                  close() { this.closed=true; }
                };
                """
            )

            async def api_route(route):
                path = urlparse(route.request.url).path
                if path == "/api/task-center":
                    slow_task_requested.set()
                    await asyncio.sleep(0.7)
                    payload = task_page
                else:
                    payload = payloads.get(path, {})
                try:
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(payload, ensure_ascii=False),
                    )
                except PlaywrightError:
                    # The expected path: changing routes aborts the slow request.
                    pass

            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/tasks")
            await asyncio.wait_for(slow_task_requested.wait(), timeout=5)
            await page.evaluate("location.hash='#/billing'")
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('我的积分账户')"
            )
            await asyncio.sleep(0.9)

            text = await page.locator("#main").inner_text()
            self.assertIn("我的积分账户", text)
            self.assertNotIn("SLOW OLD TASK MUST NEVER WIN", text)
            self.assertEqual("#/billing", await page.evaluate("location.hash"))
            self._assert_no_browser_errors(browser_errors)
            await browser.close()

    async def test_sse_reconnect_is_singleton_and_form_state_restores(self):
        payloads = {
            "/api/auth/me": {
                "id": 8,
                "username": "browser-test",
                "role": "owner",
                "tenant": "浏览器测试企业",
                "modules": ["content", "avatar", "library"],
                "all_modules": [],
            },
            "/api/meta": {},
            "/api/state": {"jobs": [], "inbox": [], "notifications": []},
            "/api/employees": [],
            "/api/billing": {
                "balance": 20,
                "is_platform": False,
                "recharged": 20,
                "spent": 0,
                "txn_n": 0,
                "log": [],
                "prices": {},
                "plans": [],
                "plan": "",
                "plan_expires": None,
            },
        }

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=self.executable,
                headless=True,
            )
            page = await browser.new_page()
            browser_errors = self._capture_unexpected_browser_errors(page)
            await page.add_init_script(
                """
                window.__eventSources = [];
                window.EventSource = class {
                  constructor(url) { this.url=url; this.closed=false;
                    window.__eventSources.push(this); }
                  close() { this.closed=true; }
                };
                """
            )

            async def api_route(route):
                path = urlparse(route.request.url).path
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payloads.get(path, {}), ensure_ascii=False),
                )

            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/billing")
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('我的积分账户')"
            )
            await page.wait_for_function("window.__eventSources.length === 1")

            await page.evaluate("window.__eventSources[0].onerror()")
            await page.wait_for_function(
                "window.__eventSources.length >= 2",
                timeout=4000,
            )
            singleton = await page.evaluate(
                """() => ({
                  firstClosed: window.__eventSources[0].closed,
                  currentIsLast: SSE === window.__eventSources.at(-1),
                  liveCount: window.__eventSources.filter(item=>!item.closed).length
                })"""
            )
            self.assertTrue(singleton["firstClosed"])
            self.assertTrue(singleton["currentIsLast"])
            self.assertEqual(1, singleton["liveCount"])

            restored = await page.evaluate(
                """() => {
                  const main=document.querySelector("#main");
                  main.innerHTML='<details id="draft-details" open><summary>草稿</summary>'
                    +'<textarea id="draft-input">一份未提交的任务草稿</textarea></details>';
                  const input=document.querySelector("#draft-input");
                  input.focus(); input.setSelectionRange(3,8);
                  const snapshot=captureFormState();
                  main.innerHTML='<details id="draft-details"><summary>草稿</summary>'
                    +'<textarea id="draft-input"></textarea></details>';
                  restoreFormState(snapshot);
                  const next=document.querySelector("#draft-input");
                  return {
                    value:next.value,
                    active:document.activeElement===next,
                    start:next.selectionStart,
                    end:next.selectionEnd,
                    open:document.querySelector("#draft-details").open
                  };
                }"""
            )
            self.assertEqual("一份未提交的任务草稿", restored["value"])
            self.assertTrue(restored["active"])
            self.assertEqual((3, 8), (restored["start"], restored["end"]))
            self.assertTrue(restored["open"])
            self._assert_no_browser_errors(browser_errors)
            await browser.close()

    async def test_must_change_password_blocks_shell_data_and_returns_to_login(self):
        requested_api_paths: list[str] = []
        password_payloads: list[dict] = []
        auth_payload = {
            "id": 9,
            "username": "password-migration-test",
            "role": "owner",
            "tenant": "浏览器测试企业",
            "modules": ["content", "avatar", "library"],
            "all_modules": [],
            "must_change_password": True,
        }

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=self.executable,
                headless=True,
            )
            page = await browser.new_page()
            browser_errors = self._capture_unexpected_browser_errors(page)
            await page.add_init_script(
                """
                window.__eventSources = [];
                window.EventSource = class {
                  constructor(url) { this.url=url; window.__eventSources.push(this); }
                  close() { this.closed=true; }
                };
                """
            )

            async def api_route(route):
                path = urlparse(route.request.url).path
                requested_api_paths.append(path)
                if path == "/api/auth/me":
                    payload = auth_payload
                elif path == "/api/auth/password":
                    password_payloads.append(
                        json.loads(route.request.post_data or "{}")
                    )
                    payload = {"ok": True}
                else:
                    payload = {}
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
            await page.goto(f"{self.base}/static/index.html#/tasks")
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('请先设置您自己的密码')"
            )

            self.assertIn("账号安全升级", await page.locator("#nav").inner_text())
            self.assertNotIn("任务中心", await page.locator("#nav").inner_text())
            main_text = await page.locator("#main").inner_text()
            self.assertIn("请先设置您自己的密码", main_text)
            self.assertIn("之后才能继续查看任务和使用数字员工", main_text)
            self.assertEqual(1, await page.locator("#main .card").count())
            self.assertNotIn("/api/meta", requested_api_paths)
            self.assertNotIn("/api/state", requested_api_paths)
            self.assertNotIn("/api/employees", requested_api_paths)

            await page.locator("#forced-pw-old").fill("LegacyPass123!")
            await page.locator("#forced-pw-new").fill("FreshPassword456!")
            await page.locator("#forced-pw-confirm").fill("FreshPassword456!")
            await page.get_by_role("button", name="保存新密码").click()
            await page.wait_for_url("**/login", timeout=3000)

            self.assertEqual(
                [{"old": "LegacyPass123!", "new": "FreshPassword456!"}],
                password_payloads,
            )
            self.assertNotIn("/api/meta", requested_api_paths)
            self.assertEqual("/login", urlparse(page.url).path)
            self._assert_no_browser_errors(browser_errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
