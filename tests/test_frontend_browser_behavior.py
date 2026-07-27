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
from urllib.parse import parse_qs, urlparse

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

    async def test_task_center_discards_stale_filter_search_and_load_more_responses(self):
        payloads = {
            "/api/auth/me": {
                "id": 10,
                "username": "task-race-test",
                "role": "owner",
                "tenant": "浏览器测试企业",
                "modules": ["content", "avatar", "library"],
                "all_modules": [],
            },
            "/api/meta": {},
            "/api/state": {"jobs": [], "inbox": [], "notifications": []},
            "/api/employees": [],
        }
        done_requested = asyncio.Event()
        failed_requested = asyncio.Event()
        load_more_requested = asyncio.Event()
        old_search_requested = asyncio.Event()
        new_search_requested = asyncio.Event()

        def task_page(
            title: str, *, group: str = "active",
            has_more: bool = False, offset: int = 0,
        ):
            return {
                "items": [{
                    "key": f"content:{title}",
                    "record_id": 901 + offset,
                    "kind": "content",
                    "kind_label": "内容工单",
                    "title": title,
                    "assignee": "测试员工",
                    "status": group,
                    "status_group": group,
                    "status_label": {
                        "active": "进行中", "done": "已完成", "failed": "失败"
                    }.get(group, group),
                    "source_label": "测试来源",
                    "target_route": "#/tasks/901",
                    "source_route": "",
                    "created_at": 1,
                    "updated_at": 1,
                }],
                "counts": {
                    "all": 5, "open": 2, "active": 2, "waiting": 0,
                    "done": 1, "failed": 1, "cancelled": 1,
                },
                "kind_counts": {"content": 5},
                "filtered_total": 2 if has_more else 1,
                "has_more": has_more,
                "next_offset": 100 if has_more else None,
                "offset": offset,
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
                parsed = urlparse(route.request.url)
                if parsed.path != "/api/task-center":
                    payload = payloads.get(parsed.path, {})
                else:
                    query = parse_qs(parsed.query)
                    status = query.get("status", ["open"])[0]
                    search = query.get("q", [""])[0]
                    offset = int(query.get("offset", ["0"])[0])
                    if search == "old":
                        old_search_requested.set()
                        await asyncio.sleep(0.7)
                        payload = task_page("STALE OLD SEARCH")
                    elif search == "new":
                        new_search_requested.set()
                        await asyncio.sleep(0.05)
                        payload = task_page("CURRENT NEW SEARCH")
                    elif status == "done":
                        done_requested.set()
                        await asyncio.sleep(0.7)
                        payload = task_page("STALE DONE FILTER", group="done")
                    elif status == "failed" and offset:
                        load_more_requested.set()
                        await asyncio.sleep(0.7)
                        payload = task_page(
                            "STALE LOAD MORE", group="failed", offset=offset
                        )
                    elif status == "failed":
                        failed_requested.set()
                        await asyncio.sleep(0.05)
                        payload = task_page(
                            "CURRENT FAILED FILTER", group="failed", has_more=True
                        )
                    else:
                        payload = task_page("CURRENT OPEN FILTER")
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/tasks")
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('CURRENT OPEN FILTER')"
            )

            await page.evaluate("tcSetStatus('done')")
            await asyncio.wait_for(done_requested.wait(), timeout=3)
            await page.evaluate("tcSetStatus('failed')")
            await asyncio.wait_for(failed_requested.wait(), timeout=3)
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('CURRENT FAILED FILTER')"
            )
            await asyncio.sleep(0.8)
            filter_text = await page.locator("#main").inner_text()
            self.assertIn("CURRENT FAILED FILTER", filter_text)
            self.assertNotIn("STALE DONE FILTER", filter_text)

            await page.evaluate(
                "tcLoadMore(document.querySelector('#tc-load-more'))"
            )
            await asyncio.wait_for(load_more_requested.wait(), timeout=3)
            await page.evaluate("tcSetStatus('open')")
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('CURRENT OPEN FILTER')"
            )
            await asyncio.sleep(0.8)
            load_more_text = await page.locator("#main").inner_text()
            self.assertIn("CURRENT OPEN FILTER", load_more_text)
            self.assertNotIn("STALE LOAD MORE", load_more_text)
            self.assertNotIn("CURRENT FAILED FILTER", load_more_text)

            await page.evaluate("tcSearch('old')")
            await asyncio.wait_for(old_search_requested.wait(), timeout=3)
            await page.evaluate("tcSearch('new')")
            await asyncio.wait_for(new_search_requested.wait(), timeout=3)
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('CURRENT NEW SEARCH')"
            )
            await asyncio.sleep(0.8)
            search_text = await page.locator("#main").inner_text()
            self.assertIn("CURRENT NEW SEARCH", search_text)
            self.assertNotIn("STALE OLD SEARCH", search_text)
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

    async def test_employee_task_forms_render_server_specific_guidance(self):
        payloads = {
            "/api/auth/me": {
                "id": 18,
                "username": "guide-browser-test",
                "role": "owner",
                "tenant": "浏览器测试企业",
                "modules": ["content", "auto", "beauty"],
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
                window.EventSource = class {
                  constructor(url) { this.url=url; }
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

            rendered = await page.evaluate(
                """() => {
                  const host=document.createElement("div");
                  const renderExpert=(employee)=>{
                    host.innerHTML=specTaskTab({...employee,tasks:[]});
                    return {
                      task:host.querySelector("#spec-dir").placeholder,
                      industry:host.querySelector("#spec-industry").placeholder,
                      material:host.querySelector("#spec-material").placeholder,
                      text:host.innerText
                    };
                  };
                  const auto=renderExpert({
                    idx:1601,name:"市场容量与趋势雷达",
                    dept_name:"汽车后市场产业部",
                    task_guide:{
                      task_placeholder:"请判断三公里汽车维修市场机会",
                      industry_placeholder:"汽车维修连锁、轮胎服务门店",
                      material_placeholder:"上传工单、项目产值和返工数据",
                      input_tips:["目标区域和时间","工单与产值现状","预算和店型限制"],
                      output_hint:"机会判断、风险和下一步验证动作"
                    }
                  });
                  const beauty=renderExpert({
                    idx:1801,name:"市场容量与趋势雷达",
                    dept_name:"美容美业产业部",
                    task_guide:{
                      task_placeholder:"请判断商圈美容项目增长机会",
                      industry_placeholder:"美容连锁、美发工作室、皮肤管理中心",
                      material_placeholder:"上传预约到店、项目客单和复购数据",
                      input_tips:["目标商圈和时间","预约到店现状","项目和技师限制"],
                      output_hint:"增长判断、风险和验证动作"
                    }
                  });
                  host.innerHTML=soloTab(
                    {idx:0,key:"trend",name:"趋势官"},
                    {task_guide:{
                      task_placeholder:"追踪本周汽车后市场内容机会",
                      industry_placeholder:"汽车后市场",
                      material_placeholder:"上传品牌定位和目标客群",
                      input_tips:["目标行业","关注平台","业务目标"],
                      output_hint:"趋势信号和五个候选选题"
                    }}
                  );
                  const content={
                    task:host.querySelector("#solo-dir").placeholder,
                    industry:host.querySelector("#solo-ind").placeholder,
                    material:host.querySelector("#solo-mat").placeholder,
                    text:host.innerText
                  };
                  return {auto,beauty,content};
                }"""
            )

            self.assertEqual("请判断三公里汽车维修市场机会", rendered["auto"]["task"])
            self.assertIn("工单", rendered["auto"]["material"])
            self.assertIn("机会判断", rendered["auto"]["text"])
            self.assertIn("个人标识", rendered["auto"]["text"])
            self.assertEqual("请判断商圈美容项目增长机会", rendered["beauty"]["task"])
            self.assertIn("预约到店", rendered["beauty"]["material"])
            self.assertIn("美容连锁", rendered["beauty"]["industry"])
            self.assertNotEqual(rendered["auto"], rendered["beauty"])
            self.assertIn("五个候选选题", rendered["content"]["text"])
            self.assertIn("个人标识", rendered["content"]["text"])
            self.assertEqual(
                "追踪本周汽车后市场内容机会",
                rendered["content"]["task"],
            )
            non_restaurant_text = json.dumps(rendered, ensure_ascii=False)
            for leaked_word in ("菜单", "菜品", "川菜正餐"):
                self.assertNotIn(leaked_word, non_restaurant_text)
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
            self.assertIn("之后就能正常查看任务和使用数字员工", main_text)
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
