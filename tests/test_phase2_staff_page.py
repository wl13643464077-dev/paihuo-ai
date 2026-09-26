"""第 2 期店员手机版 /staff(static/staff.html)在 390px 宽手机上的真实渲染(mock 接口)。"""
from __future__ import annotations

import io
import json
import unittest
from urllib.parse import urlparse

from PIL import Image
from playwright.async_api import async_playwright

from tests import test_frontend_browser_behavior as _browser

_launch_chromium = _browser._launch_chromium

TASK = {
    "kind": "task", "id": 41, "title": "把冷柜清洗一遍", "detail": "里外都擦，拍一张全景",
    "branch_id": 3, "branch_name": "人民路店", "status": "todo", "status_label": "待做",
    "require_photo": True, "due_at": 1790000000, "due_text": "09-24 18:00",
    "urgency": "overdue", "created_by_name": "boss", "review_note": "", "reviewed_at": None,
    "photos": [], "can_submit": True,
}
TODO = {
    "date": "2026-09-25", "date_text": "9 月 25 日",
    "user": {"id": 22, "name": "renmin-dz", "role_label": "店长", "can_dispatch": True},
    "branches": [{"id": 3, "name": "人民路店"}],
    "items": [
        TASK,
        {"kind": "checklist", "id": 7, "title": "开店清单", "branch_name": "人民路店",
         "status": "open", "due_text": "10:00", "urgency": "today",
         "progress": {"done": 1, "total": 2},
         "items": [{"key": "a", "text": "开灯", "done": True},
                   {"key": "b", "text": "拍冷柜", "done": False, "require_photo": True}]},
        {"kind": "inspection_action", "id": 5, "title": "地面有水渍", "branch_name": "人民路店",
         "status": "open", "due_text": "09-30 12:00", "urgency": "later", "plan": "拖干"},
    ],
    "tasks": [], "checklists": [], "actions": [],
    "reviews": [{"id": 50, "title": "补货", "branch_name": "人民路店", "assignee_name": "xiaowang",
                 "status": "submitted", "photos": [],
                 "ai_check": {"verdict": "doubt", "reason": "照片有点糊", "confidence": 0.5}}],
    "counts": {"overdue": 1, "today": 1, "todo": 3, "reviews": 1},
}


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2400, 1800), (80, 140, 200)).save(buf, "JPEG")
    return buf.getvalue()


class StaffPageTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def _open(self, playwright):
        self.submitted = []
        browser = await _launch_chromium(playwright, self.executable)
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        errors = self._capture_unexpected_browser_errors(page)

        async def api_route(route):
            path = urlparse(route.request.url).path
            if path == "/api/staff/tasks/41/submit":
                body = route.request.post_data_buffer or b""
                self.submitted.append(body)
                payload = {**TASK, "status": "submitted"}
            elif path == "/api/staff/tasks/41":
                payload = TASK
            else:
                payload = {"/api/staff/todo": TODO}.get(path, {})
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        await page.route("**/api/**", api_route)
        await page.goto(f"{self.base}/static/staff.html")
        await page.wait_for_function("document.querySelectorAll('.card').length===3")
        return browser, context, page, errors

    async def test_phone_home_task_submit_and_offline_notice(self):
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright)
            self.assertEqual("今天 9 月 25 日 · 人民路店", await page.locator("#today").inner_text())
            titles = await page.locator(".card .t").all_inner_texts()
            self.assertEqual(["📌 把冷柜清洗一遍", "✅ 开店清单", "🔧 地面有水渍"], titles)
            self.assertIn("已逾期", await page.locator(".card").first.inner_text())
            self.assertIn("待我审核（1）", await page.locator("[data-go=reviews]").inner_text())
            layout = await page.evaluate("""() => ({
              doc: document.documentElement.scrollWidth,
              small: [...document.querySelectorAll('.btn,.card,.back')]
                .filter(el => el.offsetParent !== null)
                .map(el => el.getBoundingClientRect().height).filter(h => h < 48).length,
              font: Math.min(...[...document.querySelectorAll('.card .t,.card .m,.btn')]
                .map(el => parseFloat(getComputedStyle(el).fontSize))),
            })""")
            self.assertLessEqual(layout["doc"], 390)
            self.assertEqual(0, layout["small"])
            self.assertGreaterEqual(layout["font"], 16)

            # 点开任务 → 拍照(压缩) → 提交
            await page.locator(".card").first.click()
            await page.wait_for_selector("[data-act=shoot]")
            self.assertIn("里外都擦", await page.locator("#main").inner_text())
            async with page.expect_file_chooser() as chooser:
                await page.locator("[data-act=shoot]").click()
            fc = await chooser.value
            self.assertEqual("environment", await fc.element.get_attribute("capture"))
            await fc.set_files({"name": "big.jpg", "mimeType": "image/jpeg", "buffer": _jpeg()})
            await page.wait_for_selector(".thumb img")
            await page.locator("[data-act=submit]").click()
            await page.wait_for_function("document.querySelector('.msg.ok')?.textContent.includes('交上去了')")
            self.assertEqual(1, len(self.submitted))
            self.assertIn(b'name="photos"', self.submitted[0])
            self.assertIn(b"image/jpeg", self.submitted[0])
            # 压缩后远小于原图
            self.assertLess(len(self.submitted[0]), len(_jpeg()))

            # 网络断了给明确提示
            await context.set_offline(True)
            await page.evaluate("window.dispatchEvent(new Event('offline'))")
            self.assertTrue(await page.locator("#net").is_visible())
            await context.set_offline(False)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_manager_reviews_and_checklist_views(self):
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright)
            await page.locator("[data-go=reviews]").click()
            await page.wait_for_selector("[data-review-act=approve]")
            self.assertIn("照片有点糊", await page.locator("#main").inner_text())
            await page.locator("[data-review-act=reject]").click()
            self.assertTrue(await page.locator(".reject-box textarea").is_visible())
            await page.locator(".back").click()
            await page.locator(".card", has_text="开店清单").click()
            await page.wait_for_selector("[data-item=b]")
            self.assertIn("拍照打勾", await page.locator("[data-item=b]").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()


OWNER = {"id": 20, "username": "boss", "role": "owner", "tenant": "连锁",
         "modules": ["content", "library"], "all_modules": [], "job_title": ""}
META = {"can_dispatch": True, "can_review": True, "sees_all": True, "role_label": "老板",
        "ai_check_enabled": True, "prices": {"staff_parse": 0.2, "staff_ai_check": 0.2},
        "branches": [{"id": 3, "name": "人民路店", "members": [
            {"id": 22, "name": "renmin-dz", "job_title": "manager", "title_label": "店长"},
            {"id": 23, "name": "xiaowang", "job_title": "staff", "title_label": "店员"}]},
            {"id": 4, "name": "中山路店", "members": []}]}
LIST = {"items": [
    {"id": 50, "title": "补货", "branch_id": 3, "branch_name": "人民路店", "assignee_user_id": 23,
     "assignee_name": "xiaowang", "status": "submitted", "status_label": "已交，等审核",
     "require_photo": True, "due_text": "09-25 18:00", "photos": [{"id": 1, "url": "/static/img/avatar.png"}],
     "ai_check": {"verdict": "fail", "reason": "货架还是空的", "confidence": 0.8}},
    {"id": 51, "title": "擦玻璃", "branch_id": 3, "branch_name": "人民路店", "assignee_user_id": None,
     "assignee_name": "", "status": "todo", "overdue": True, "due_text": "09-24 18:00", "photos": []},
], "next_before_id": None}


class StaffAdminPageTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def test_one_liner_drafts_then_batch_create_and_review_list(self):
        created = []
        api = {
            "/api/auth/me": OWNER,
            "/api/meta": {"app": {"name": "派活", "slogan": "测试"}, "stations": []},
            "/api/state": {"inbox": [], "notifications": [], "jobs": [], "balance": 10},
            "/api/employees": [], "/api/depts": [],
            "/api/staff/meta": META, "/api/staff/tasks": LIST,
            "/api/staff/tasks/parse": {"points": 0.2, "drafts": [
                {"title": "把冷柜清一遍", "detail": "", "branch_id": 3, "assignee_user_id": 22,
                 "due_at": 1790060400, "require_photo": True, "hints": []},
                {"title": "盘点饮料", "detail": "", "branch_id": None, "assignee_user_id": None,
                 "due_at": None, "require_photo": True, "hints": ["没找到「火星店」这家店，请选一下"]}]},
        }
        async with async_playwright() as playwright:
            browser = await _launch_chromium(playwright, self.executable)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            errors = self._capture_unexpected_browser_errors(page)
            await page.add_init_script(
                "window.EventSource = class { constructor(u){this.url=u;} close(){} };")

            async def api_route(route):
                path = urlparse(route.request.url).path
                if path == "/api/staff/tasks" and route.request.method == "POST":
                    created.append(json.loads(route.request.post_data or "{}"))
                    payload = {"id": 60 + len(created)}
                else:
                    payload = api.get(path, {})
                await route.fulfill(status=200, content_type="application/json",
                                    body=json.dumps(payload, ensure_ascii=False))

            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/dispatch")
            await page.wait_for_selector(".hubcard", state="attached")
            self.assertIn("派给店员", await page.locator("#main").inner_text())
            await page.goto(f"{self.base}/static/index.html#/staff-tasks")
            await page.wait_for_selector("#sa-one")
            main = await page.locator("#main").inner_text()
            self.assertIn("等您审核(1)", main)
            self.assertIn("货架还是空的", main)
            await page.fill("#sa-one", "人民路店明天中午前把冷柜清一遍拍照给我，火星店盘点饮料")
            await page.get_by_role("button", name="✨ 拆成任务(0.2 点)").click()
            await page.wait_for_selector("[data-sa-draft='1']")
            self.assertIn("没找到「火星店」", await page.locator("[data-sa-draft='1']").inner_text())
            # 第二件没选门店，不能派
            await page.get_by_role("button", name="📤 确认派出去(2 件)").click()
            await page.wait_for_function("document.querySelector(\"[data-sa-draft='1']\").innerText.includes('请选择门店')")
            self.assertEqual([], created)
            await page.locator("[data-sa-draft='1'] select").first.select_option("4")
            await page.get_by_role("button", name="📤 确认派出去(2 件)").click()
            await page.wait_for_function("document.querySelectorAll('[data-sa-draft]').length===0")
            self.assertEqual(2, len(created))
            self.assertEqual({"branch_id": 3, "assignee_user_id": 22, "title": "把冷柜清一遍"},
                             {k: created[0][k] for k in ("branch_id", "assignee_user_id", "title")})
            self.assertEqual(4, created[1]["branch_id"])
            self.assertNotEqual(created[0]["request_key"], created[1]["request_key"])
            self.assertTrue(created[0]["request_key"].startswith("sa-"))
            self._assert_no_browser_errors(errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
