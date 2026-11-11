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

    async def _open(self, playwright, todo=TODO, cards=3, extra=None):
        self.submitted = []
        self.ticks = []
        browser = await _launch_chromium(playwright, self.executable)
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        errors = self._capture_unexpected_browser_errors(page)

        async def api_route(route):
            path = urlparse(route.request.url).path
            if extra and path in extra:
                payload = extra[path]
            elif path == "/api/staff/tasks/41/submit":
                body = route.request.post_data_buffer or b""
                self.submitted.append(body)
                payload = {**TASK, "status": "submitted"}
            elif path == "/api/staff/tasks/41":
                payload = TASK
            elif path.startswith("/api/checklist/runs/"):
                self.ticks.append((path, route.request.post_data_buffer or b""))
                payload = {}
            else:
                payload = {"/api/staff/todo": todo}.get(path, {})
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        await page.route("**/api/**", api_route)
        await page.goto(f"{self.base}/static/staff.html")
        if todo["items"]:
            await page.wait_for_function(f"document.querySelectorAll('.card').length==={cards}")
        else:
            await page.wait_for_selector(".empty")
        return browser, context, page, errors

    async def test_empty_home_tells_never_assigned_from_all_done(self):
        empty = {**TODO, "items": [], "reviews": [], "user": {**TODO["user"], "can_dispatch": False}}
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright, {**empty, "has_any_task": False})
            self.assertEqual("还没有人给您派活", (await page.locator(".empty").inner_text()).strip())
            await browser.close()
            browser, context, page, errors = await self._open(playwright, {**empty, "has_any_task": True})
            self.assertIn("今天的活都做完了", await page.locator(".empty").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

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

    async def test_home_footer_and_photo_error_clears_after_picking(self):
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright)
            # 店长/店员看不到「完整版」(会走进老板工作台);退出是底部的小文字链。
            foot = await page.locator("#main").inner_text()
            self.assertNotIn("完整版", foot)
            self.assertEqual(1, await page.locator(".foot-links [data-act=logout]").count())
            self.assertEqual(0, await page.locator(".btn[data-act=logout]").count())
            await page.locator(".card").first.click()
            await page.wait_for_selector("[data-act=shoot]")
            await page.locator("[data-act=submit]").click()
            self.assertIn("请先点「拍照」", await page.locator("#err").inner_text())
            async with page.expect_file_chooser() as chooser:
                await page.locator("[data-act=shoot]").click()
            fc = await chooser.value
            await fc.set_files({"name": "big.jpg", "mimeType": "image/jpeg", "buffer": _jpeg()})
            await page.wait_for_selector(".thumb img")
            self.assertEqual("", (await page.locator("#err").inner_text()).strip())
            self._assert_no_browser_errors(errors)
            await browser.close()
        owner_todo = {**TODO, "user": {**TODO["user"], "can_open_full": True}}
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright, owner_todo)
            self.assertIn("完整版", await page.locator("#main").inner_text())
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
            self.assertIn("拍照，标记做完", await page.locator("[data-item=b]").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_approved_task_shows_positive_feedback_on_home(self):
        approved = {**TASK, "id": 42, "title": "擦玻璃", "status": "approved", "status_label": "已通过",
                    "urgency": "approved", "require_photo": True}
        todo = {**TODO, "items": TODO["items"] + [approved]}
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright, todo, cards=4)
            main = await page.locator("#main").inner_text()
            self.assertIn("已交 / 已做完", main)
            card = page.locator(".card", has_text="擦玻璃")
            self.assertIn("✅ 已通过", await card.inner_text())
            self.assertNotIn("要拍照", await card.inner_text())
            # 「我的待办」只数还要做的 3 件
            self.assertIn("我的待办（3）", main)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_checklist_done_and_undone_read_differently_and_can_undo(self):
        run = {"kind": "checklist", "id": 7, "title": "闭店清单", "branch_name": "人民路店",
               "status": "open", "due_text": "22:00", "urgency": "today",
               "progress": {"done": 1, "total": 2},
               "items": [{"key": "a", "text": "关灯", "done": True, "done_at": 1790000520},
                         {"key": "b", "text": "锁门", "done": False}]}
        todo = {**TODO, "items": [run], "reviews": []}
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright, todo, cards=1)
            await page.locator(".card").first.click()
            await page.wait_for_selector("[data-item=b]")
            # 没做的：按钮写「点一下，标记做完」；做完的：绿色「✅ 已完成 · 时间」+ 撤回
            self.assertEqual("点一下，标记做完", (await page.locator("[data-item=b]").inner_text()).strip())
            done = await page.locator(".item", has_text="关灯").inner_text()
            self.assertIn("✅ 已完成 · 22:22", done)
            self.assertEqual(0, await page.locator(".item", has_text="关灯").locator("[data-item]").count())
            await page.locator("[data-undo=a]").click()
            for _ in range(50):
                if self.ticks:
                    break
                await page.wait_for_timeout(50)
            path, body = self.ticks[0]
            self.assertEqual("/api/checklist/runs/7/items/a", path)
            self.assertIn(b'name="done"\r\n\r\n0', body)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_resubmitted_review_shows_last_reason_and_photo_opens_in_page(self):
        review = {"id": 50, "title": "补货", "branch_name": "人民路店", "assignee_name": "xiaowang",
                  "status": "submitted", "review_note": "货架还空着", "reviewed_at": 1790000000,
                  "submit_count": 2, "reviewer_label": "店长 laowang",
                  "photos": [{"id": 1, "url": "/static/img/avatar.png"}, {"id": 2, "url": "/static/favicon.svg"}]}
        todo = {**TODO, "reviews": [review]}
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright, todo)
            await page.locator("[data-go=reviews]").click()
            await page.wait_for_selector("[data-review-act=approve]")
            self.assertIn("店长 laowang 打回：货架还空着（第 2 次交）", await page.locator("#main").inner_text())
            pages = len(context.pages)
            await page.locator(".photos [data-view='0']").click()
            await page.wait_for_selector("#viewer img")
            self.assertEqual(pages, len(context.pages))            # 没开新标签页
            self.assertIn("1 / 2", await page.locator("#viewer").inner_text())
            await page.locator("#viewer [data-v=n]").click()
            self.assertIn("2 / 2", await page.locator("#viewer").inner_text())
            await page.locator("#viewer [data-v=x]").click()
            self.assertEqual(0, await page.locator("#viewer").count())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_staff_sees_no_ai_note_on_own_submitted_task(self):
        mine = {**TASK, "status": "submitted", "status_label": "已交，等审核", "urgency": "waiting",
                "can_manage": False, "can_submit": False, "photos": [{"id": 1, "url": "/static/img/avatar.png"}],
                "ai_check": {"error": True, "reason": "AI 这次没看成，请直接人工看照片"}}
        todo = {**TODO, "items": [mine], "reviews": [],
                "user": {**TODO["user"], "can_dispatch": False}}
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright, todo, cards=1,
                                                             extra={"/api/staff/tasks/41": mine})
            self.assertIn("已交，等店长或老板看", await page.locator(".card").inner_text())
            await page.locator(".card").first.click()
            await page.wait_for_selector(".photos")
            main = await page.locator("#main").inner_text()
            self.assertIn("等店长或老板看", main)
            self.assertNotIn("AI", main)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_manager_home_shows_store_tasks_and_opens_review_or_readonly(self):
        waiting = {"kind": "task", "id": 60, "title": "补货", "branch_name": "人民路店",
                   "assignee_user_id": 23, "assignee_name": "xiaowang", "status": "submitted",
                   "store_state": "submitted", "due_text": "09-25 18:00", "photos": []}
        doing = {"kind": "task", "id": 61, "title": "冷柜除霜", "branch_name": "人民路店",
                 "assignee_user_id": 24, "assignee_name": "xiaoli", "status": "todo",
                 "store_state": "overdue", "urgency": "overdue", "overdue": True,
                 "due_text": "09-24 18:00", "photos": []}
        todo = {**TODO, "store_tasks": [waiting, doing]}
        posts = []
        detail = {
            "/api/staff/tasks/60": {**waiting, "can_manage": True, "dispatcher_label": "老板 boss",
                                    "photos": [{"id": 1, "url": "/static/img/avatar.png"}]},
            "/api/staff/tasks/61": {**doing, "can_manage": True, "can_submit": False,
                                    "dispatcher_label": "店长 renmin-dz"},
        }
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright, todo, cards=5, extra=detail)
            self.assertEqual("派活 · 店长", await page.title())
            self.assertIn("本店在做的活（2）", await page.locator("#main").inner_text())
            first = page.locator(".card.store").first
            self.assertIn("等您审核", await first.inner_text())
            # 后半句跟状态走：等审核的写「已交」，逾期的写「已逾期」
            self.assertIn("xiaowang 已交", await first.inner_text())
            self.assertNotIn("在做", await first.inner_text())
            second = await page.locator(".card.store").nth(1).inner_text()
            self.assertIn("xiaoli 已逾期", second)
            self.assertIn("09-24 18:00", second)

            # 在做的活：只读，不给提交按钮
            await page.locator(".card.store", has_text="冷柜除霜").click()
            await page.wait_for_function("location.hash==='#store-61'")
            await page.wait_for_selector("text=只看进度")
            main = await page.locator("#main").inner_text()
            self.assertIn("交给 xiaoli", main)
            self.assertIn("店长 renmin-dz 派的", main)
            self.assertEqual(0, await page.locator("[data-act=submit],[data-act=shoot]").count())
            await page.locator(".back").click()

            # 等审核的活：直接给审核卡，通过后回首页
            async def review_route(route):
                posts.append(json.loads(route.request.post_data or "{}"))
                await route.fulfill(status=200, content_type="application/json", body="{}")
            await page.route("**/api/staff/tasks/60/review", review_route)
            await page.locator(".card.store", has_text="补货").click()
            await page.wait_for_selector("[data-review-act=approve]")
            self.assertIn("等您审核", await page.locator("#main").inner_text())
            await page.locator("[data-review-act=approve]").click()
            await page.wait_for_function("document.querySelector('.msg.ok')?.textContent.includes('已通过')")
            self.assertEqual([{"action": "approve", "note": ""}], posts)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_store_card_follows_state_and_manager_can_nudge(self):
        redo = {"kind": "task", "id": 62, "title": "擦玻璃", "branch_name": "人民路店",
                "assignee_user_id": 24, "assignee_name": "xiaoli", "status": "todo",
                "store_state": "rejected", "urgency": "today", "review_note": "没擦干净",
                "reviewed_at": 1790000000, "reviewer_label": "店长 laowang",
                "due_text": "09-25 18:00", "photos": []}
        doing = {"kind": "task", "id": 63, "title": "把冷柜清洗一遍", "branch_name": "人民路店",
                 "assignee_user_id": 23, "assignee_name": "xiaowang", "status": "todo",
                 "store_state": "later", "urgency": "later", "due_text": "09-28 18:00", "photos": []}
        todo = {**TODO, "store_tasks": [redo, doing]}
        nudges = []
        detail = {
            "/api/staff/tasks/62": {**redo, "can_manage": True, "can_submit": False},
            "/api/staff/tasks/63": {**doing, "can_manage": True, "can_submit": False},
        }
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright, todo, cards=5, extra=detail)
            first = await page.locator(".card.store").first.inner_text()
            self.assertIn("被打回", first)
            self.assertIn("xiaoli 在重做", first)
            self.assertNotIn("在做", first.replace("在重做", ""))
            self.assertIn("xiaowang 在做", await page.locator(".card.store").nth(1).inner_text())

            async def nudge_route(route):
                nudges.append(urlparse(route.request.url).path)
                if len(nudges) == 1:
                    await route.fulfill(status=200, content_type="application/json",
                                        body=json.dumps({"ok": True, "nudged_at": 1790000000,
                                                         "nudged_text": "22:13"}))
                else:
                    await route.fulfill(status=429, content_type="application/json",
                                        body=json.dumps({"detail": "刚催过，30 分钟后再催"},
                                                        ensure_ascii=False))
            await page.route("**/api/staff/tasks/*/nudge", nudge_route)

            # 被打回的活：写清是谁打回的，能催一下
            await page.locator(".card.store", has_text="擦玻璃").click()
            await page.wait_for_selector("[data-act=nudge]")
            main = await page.locator("#main").inner_text()
            self.assertIn("店长 laowang 打回，在重做：没擦干净", main)
            self.assertIn("催一下 xiaoli", await page.locator("[data-act=nudge]").inner_text())
            await page.locator("[data-act=nudge]").click()
            await page.wait_for_selector("#nudge .tag.checked")
            self.assertIn("已催 · 22:13", await page.locator("#nudge").inner_text())
            self.assertEqual(0, await page.locator("[data-act=nudge]").count())
            self.assertEqual(["/api/staff/tasks/62/nudge"], nudges)
            await page.locator(".back").click()

            # 服务端说刚催过：把原因讲给店长听，按钮还在
            await page.locator(".card.store", has_text="把冷柜清洗一遍").click()
            await page.wait_for_selector("[data-act=nudge]")
            await page.locator("[data-act=nudge]").click()
            await page.wait_for_selector("#nudge .msg.err")
            self.assertIn("刚催过，30 分钟后再催", await page.locator("#nudge").inner_text())
            self.assertEqual(1, await page.locator("[data-act=nudge]").count())
            # 浏览器会把这次故意返回的 429 记成资源加载失败，不算页面错误
            errors[:] = [e for e in errors if "status of 429" not in e]
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_staff_sees_who_rejected_and_rejected_task_on_top(self):
        rejected = {**TASK, "id": 42, "title": "擦玻璃", "urgency": "today", "review_note": "没擦干净",
                    "reviewed_at": 1790000000, "reviewer_label": "店长 laowang",
                    "dispatcher_label": "老板 wangji"}
        staff = {**TODO, "reviews": [], "items": [rejected, *TODO["items"]],
                 "user": {**TODO["user"], "role_label": "店员", "can_dispatch": False}}
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(
                playwright, staff, cards=4, extra={"/api/staff/tasks/42": rejected})
            first = await page.locator(".card").first.inner_text()
            self.assertIn("擦玻璃", first)
            self.assertIn("被打回，要重做", first)
            await page.locator(".card").first.click()
            await page.wait_for_selector("[data-act=shoot]")
            main = await page.locator("#main").inner_text()
            self.assertIn("店长 laowang 打回：没擦干净", main)
            self.assertNotIn("上次被打回", main)
            self.assertEqual(0, await page.locator("[data-act=nudge]").count())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_staff_title_and_dispatcher_role_on_task_detail(self):
        staff = {**TODO, "reviews": [], "user": {**TODO["user"], "role_label": "店员",
                                                  "can_dispatch": False}}
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(
                playwright, staff,
                extra={"/api/staff/tasks/41": {**TASK, "dispatcher_label": "老板 wangji"}})
            self.assertEqual("派活 · 店员", await page.title())
            self.assertEqual(0, await page.locator("#store-head").count())
            await page.locator(".card").first.click()
            await page.wait_for_selector("[data-act=shoot]")
            self.assertIn("老板 wangji 派的", await page.locator(".panel .sub").first.inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_checklist_item_text_gets_full_width_on_375_phone(self):
        long_text = "剩余食材分类封好、贴上日期标签放进冷藏，拍一张冷藏柜里面"
        run = {"kind": "checklist", "id": 7, "title": "闭店清单", "branch_name": "人民路店",
               "status": "open", "due_text": "22:00", "urgency": "today",
               "progress": {"done": 1, "total": 2},
               "items": [{"key": "a", "text": "关灯", "done": True, "done_at": 1790000520},
                         {"key": "b", "text": long_text, "done": False, "require_photo": True}]}
        todo = {**TODO, "items": [run], "reviews": []}
        async with async_playwright() as playwright:
            browser, context, page, errors = await self._open(playwright, todo, cards=1)
            await page.set_viewport_size({"width": 375, "height": 812})
            await page.locator(".card").first.click()
            await page.wait_for_selector("[data-item=b]")
            box = await page.evaluate("""() => {
              const item = document.querySelectorAll('.item')[1];
              const tx = item.querySelector('.tx').getBoundingClientRect();
              const btn = item.querySelector('[data-item]').getBoundingClientRect();
              const undo = document.querySelector('[data-undo=a]').getBoundingClientRect();
              const lh = parseFloat(getComputedStyle(item.querySelector('.tx')).lineHeight);
              return {txW: tx.width, itemW: item.getBoundingClientRect().width, lines: Math.round(tx.height / lh),
                      below: btn.top >= tx.bottom - 1, btnW: btn.width, btnH: btn.height, undoH: undo.height,
                      doc: document.documentElement.scrollWidth};
            }""")
            self.assertTrue(box["below"])                         # 按钮在文字下面
            self.assertGreater(box["txW"], box["itemW"] - 4)      # 文字占满整行
            self.assertLessEqual(box["lines"], 3)
            self.assertGreater(box["btnW"], box["itemW"] - 4)
            self.assertGreaterEqual(box["btnH"], 44)
            self.assertGreaterEqual(box["undoH"], 44)
            self.assertLessEqual(box["doc"], 375)
            self.assertIn("✅ 已完成 · 22:22", await page.locator(".item", has_text="关灯").inner_text())
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
            self.assertIn("等您审核（1）", main)
            self.assertIn("货架还是空的", main)
            await page.fill("#sa-one", "人民路店明天中午前把冷柜清一遍拍照给我，火星店盘点饮料")
            await page.get_by_role("button", name="✨ 拆成任务").click()
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

    async def test_review_flow_keeps_rejected_task_visible_and_clears_badge(self):
        tasks = {
            50: {"id": 50, "title": "补货", "branch_id": 3, "branch_name": "人民路店", "assignee_user_id": 23,
                 "assignee_name": "xiaowang", "status": "submitted", "require_photo": True,
                 "review_note": "货架还空着", "reviewed_at": 1790000000, "submit_count": 2,
                 "reviewer_label": "店长 laowang",
                 "photos": [{"id": 1, "url": "/static/img/avatar.png"}, {"id": 2, "url": "/static/favicon.svg"}]},
            51: {"id": 51, "title": "擦玻璃", "branch_id": 3, "branch_name": "人民路店", "assignee_user_id": None,
                 "assignee_name": "", "status": "todo", "overdue": False, "photos": []},
        }
        meta = {**META, "ai_check_enabled": False, "ai_check_asked": False}
        state = {"inbox": [], "jobs": [], "balance": 10,
                 "notifications": [{"id": 1, "kind": "staff_task_submitted", "link": "#/staff-tasks/50",
                                    "title": "店员交差了，等您审核", "summary": "xiaowang 交了「补货」"}]}
        posts = []
        api = {
            "/api/auth/me": OWNER,
            "/api/meta": {"app": {"name": "派活", "slogan": "测试"}, "stations": []},
            "/api/state": state, "/api/employees": [], "/api/depts": [], "/api/staff/meta": meta,
        }
        async with async_playwright() as playwright:
            browser = await _launch_chromium(playwright, self.executable)
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            errors = self._capture_unexpected_browser_errors(page)
            await page.add_init_script("window.EventSource = class { constructor(u){this.url=u;} close(){} };")

            async def api_route(route):
                url = urlparse(route.request.url)
                path, method = url.path, route.request.method
                if method in ("POST", "PUT"):
                    body = json.loads(route.request.post_data or "{}")
                    posts.append((path, body))
                    if path == "/api/staff/tasks/50/review":
                        tasks[50].update(status="todo", review_note=body["note"], photos=[],
                                         reviewer_label="老板 boss")
                    payload = {"ok": True}
                    if path == "/api/staff/tasks/50/nudge":
                        nudged_at = __import__("time").time()
                        payload = {"ok": True, "nudged_at": nudged_at, "nudged_text": "10:05"}
                elif path == "/api/staff/tasks":
                    want = dict(p.split("=", 1) for p in url.query.split("&") if "=" in p).get("status")
                    payload = {"items": [t for t in sorted(tasks.values(), key=lambda t: -t["id"])
                                         if not want or t["status"] == want], "next_before_id": None}
                else:
                    payload = api.get(path, {})
                await route.fulfill(status=200, content_type="application/json",
                                    body=json.dumps(payload, ensure_ascii=False))

            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/dispatch")
            await page.wait_for_selector(".hubcard", state="attached")
            self.assertEqual("1", (await page.locator("[data-nav=today] .badge").first.inner_text()).strip())
            # 今天页「去审核」：审核区在最上面，派活区收成一行
            await page.evaluate("PH_STAFF_ADMIN.openReview()")
            await page.wait_for_selector("#sa-review")
            order = await page.evaluate(
                "[...document.querySelectorAll('#main .card')].map(c=>c.id).filter(Boolean)")
            self.assertEqual(["sa-review", "sa-dispatch"], order[:2])
            self.assertEqual(0, await page.locator("#sa-one").count())
            self.assertIn("＋ 派一件新活", await page.locator("#sa-dispatch").inner_text())
            review = await page.locator("#sa-review").inner_text()
            # 写上是谁打回的
            self.assertIn("店长 laowang 打回：货架还空着（第 2 次交）", review)
            # 等审核的活没有「催一下」
            self.assertEqual(0, await page.locator("#sa-task-50 [onclick*='_nudge']").count())
            # 老板没表过态：问一句要不要 AI 先看照片(默认不开)
            self.assertIn("要不要让 AI 先帮您看照片", review)
            await page.locator("#sa-ai-ask").get_by_role("button", name="先不用").click()
            await page.wait_for_function("!document.querySelector('#sa-ai-ask')")
            self.assertIn(("/api/staff/settings", {"ai_check_enabled": False}), posts)
            self.assertIn("AI 先看照片", await page.locator("#sa-review h3").inner_text())
            # 照片 200px 起，点开是页内大图
            width = await page.locator(".sa-photos img").first.evaluate("e=>e.getBoundingClientRect().width")
            self.assertGreaterEqual(width, 200)
            pages = len(context.pages)
            await page.locator(".sa-photos a").first.click()
            await page.wait_for_selector("#sa-viewer img")
            self.assertEqual(pages, len(context.pages))
            await page.keyboard.press("Escape")
            self.assertEqual(0, await page.locator("#sa-viewer").count())
            # 只看「等审核」时打回：筛选自动清掉，打回的活留在「店员在做」里看得到
            await page.locator(".sa-bar select[aria-label='按状态看']").select_option("submitted")
            await page.wait_for_function("document.querySelector('#main').innerText.includes('已按「等审核」筛选')")
            await page.get_by_role("button", name="↩️ 打回重做").click()
            await page.wait_for_selector("#ui-dialog")
            await page.fill("#ui-dialog-field", "还是没补齐")
            await page.locator("#ui-dialog [data-dialog-ok]").click()
            await page.wait_for_function(
                "document.querySelector('#sa-task-50')?.innerText.includes('老板 boss 打回：还是没补齐')")
            self.assertEqual("", await page.locator(".sa-bar select[aria-label='按状态看']").input_value())
            self.assertIn("店员在做（2）", await page.locator("#main").inner_text())
            self.assertIn(("/api/staff/tasks/50/review", {"action": "reject", "note": "还是没补齐"}), posts)
            # 顶部「今天」的待处理角标马上消失
            self.assertEqual(0, await page.locator("[data-nav=today] .badge").count())
            # 打回后在做的活能「催一下」；没指定人的活没有
            self.assertEqual(0, await page.locator("#sa-task-51 [onclick*='_nudge']").count())
            await page.locator("#sa-task-50").get_by_role("button", name="🔔 催一下").click()
            await page.wait_for_function(
                "document.querySelector('#sa-task-50')?.innerText.includes('已催 · ')")
            self.assertIn(("/api/staff/tasks/50/nudge", {}), posts)
            self.assertEqual(0, await page.locator("#sa-task-50 [onclick*='_nudge']").count())
            self._assert_no_browser_errors(errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
