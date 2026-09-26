"""结论卡真浏览器回归：手机首屏看到结论与 3 条行动，全文默认折叠，派给店员打开派活表单。"""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import unittest
from urllib.parse import urlparse

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - 没装 playwright 的环境直接跳过
    async_playwright = None


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = (
    "**一句话结论**：能租，但先谈 2 个月免租期\n\n"
    "**今天/本周就做这 3 件事**\n"
    "1. 店长：周五前约房东谈免租期\n"
    "2. 老板：周六晚上实地数一次客流\n"
    "3. 财务：租金占比超 15% 就不签\n\n"
    "**⚠️ 要留意**：对面商场明年开业会分流"
)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return


def _browser_executable() -> str:
    candidates = (
        os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    )
    return next((path for path in candidates if path and os.path.isfile(path)), "")


@unittest.skipIf(async_playwright is None, "playwright 不可用")
class VerdictCardBrowserTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        handler = partial(_QuietHandler, directory=str(ROOT))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _payloads(self) -> dict:
        long_report = "# 铺面评估\n" + "\n".join(f"第 {i} 段分析正文。" for i in range(80))
        return {
            "/api/auth/me": {
                "id": 7, "username": "boss", "role": "owner", "tenant": "测试小店",
                "modules": ["content", "library", "meeting"], "all_modules": [],
            },
            "/api/meta": {"stations": [], "industries": [], "platforms": [],
                          "brief_templates": [], "platform_specs": {}},
            "/api/state": {"jobs": [], "inbox": [], "notifications": []},
            "/api/employees": [],
            "/api/depts": [],
            "/api/meetings": {"items": [], "total": 0, "offset": 0, "limit": 20},
            "/api/tasks/5": {
                "id": 5, "status": "done", "brief": {"direction": "看看这个铺面能不能租"},
                "emp_name": "王姐·选址顾问", "dept_name": "零售", "created_at": 1,
                "updated_at": 2, "terminal_at": 2, "output_md": long_report,
                "summary_md": SUMMARY, "steps": [], "source": {"label": "直接派活"},
            },
            "/api/tasks/6": {
                "id": 6, "status": "running", "brief": {"direction": "做一份周末引流方案"},
                "emp_name": "王姐·选址顾问", "created_at": 1, "steps": [
                    {"k": "working", "l": "员工正在处理任务", "ts": 1},
                ],
                "boss_progress": {
                    "stages": ["正在查资料", "正在写方案", "正在检查", "马上好"],
                    "current": 1, "label": "正在写方案", "elapsed_seconds": 240,
                    "eta_seconds": 360, "hint": "预计还要约 6 分钟",
                },
                "source": {"label": "直接派活"},
            },
            "/api/meetings/9": {
                "id": 9, "status": "done", "phase": "completed", "decision": "GO",
                "question": "要不要在社区开团购", "members": [],
                "summary_md": "**一句话结论**：可以干：毛利够\n\n**今天/本周就做这 3 件事**\n"
                              "1. 小王·运营：做团购试点方案\n2. 启动 20 户试点\n\n"
                              "**⚠️ 要留意**：团长流失快",
                "consensus_md": "# 会议共识\n很长的共识正文", "messages": [
                    {"who": "会议主持人", "text": "讨论内容", "color": "#000", "emoji": "🎙️"},
                ],
                "actions": [], "execution_tasks": [],
            },
        }

    async def _open(self, playwright, hash_route: str):
        executable = _browser_executable()
        options = {"headless": True}
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script(
            "window.EventSource = class { constructor(u){this.url=u;} close(){} };"
        )
        payloads = self._payloads()

        async def api_route(route):
            path = urlparse(route.request.url).path
            await route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps(payloads.get(path, {}), ensure_ascii=False),
            )

        await page.route("**/api/**", api_route)
        await page.goto(f"{self.base}/static/index.html{hash_route}")
        return browser, page, errors

    async def test_task_detail_shows_verdict_card_on_first_mobile_screen(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/tasks/5")
            card = page.locator("[data-verdict-card]")
            await card.wait_for(timeout=5000)
            self.assertIn("能租，但先谈 2 个月免租期", await card.inner_text())
            self.assertEqual(3, await card.locator("button", has_text="派给店员").count())
            # 手机首屏(844px 高)能看到结论和第一条行动。
            first_action = card.locator(".vc-actions li").first
            box = await first_action.bounding_box()
            self.assertLess(box["y"] + box["height"], 844)
            # 全文默认折叠。
            self.assertFalse(await page.locator("details.vc-full").evaluate("el => el.open"))
            self.assertFalse(await page.get_by_text("第 40 段分析正文。").is_visible())
            await page.locator("details.vc-full > summary").click()
            self.assertTrue(await page.get_by_text("第 40 段分析正文。").is_visible())
            # 第 2 期:老板点「派给店员」直接打开派活表单,预填这条行动(去掉「店长：」称呼)
            await card.locator("button", has_text="派给店员").first.click()
            await page.wait_for_function(
                "location.hash==='#/staff-tasks'"
                " && document.querySelector('[data-sa-draft] input')?.value==='周五前约房东谈免租期'",
                timeout=5000,
            )
            self.assertEqual([], errors)
            await browser.close()

    async def test_running_task_shows_plain_stage_progress(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/tasks/6")
            stage = page.locator(".stage-progress")
            await stage.wait_for(timeout=5000)
            text = await stage.inner_text()
            for expected in ("正在写方案", "正在查资料", "马上好", "已用 4 分钟", "预计还要约 6 分钟"):
                self.assertIn(expected, text)
            self.assertTrue(await stage.is_visible())
            self.assertEqual([], errors)
            await browser.close()

    async def test_meeting_detail_puts_verdict_card_above_the_new_meeting_form(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/meetings/9")
            card = page.locator("[data-verdict-card]")
            await card.wait_for(timeout=5000)
            self.assertIn("可以干：毛利够", await card.inner_text())
            self.assertEqual(2, await card.locator("button", has_text="派给店员").count())
            box = await card.bounding_box()
            self.assertLess(box["y"], 844)
            form_box = await page.locator("#mt-q").bounding_box()
            self.assertGreater(form_box["y"], box["y"])
            self.assertFalse(await page.locator("#mt-full").evaluate("el => el.open"))
            self.assertEqual([], errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
