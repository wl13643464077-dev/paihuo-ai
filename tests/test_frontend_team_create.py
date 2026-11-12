"""团队页建店员账号(375px 手机宽)、首登改密页、点数明细合并退回、派给店员工具条的浏览器行为。"""
from __future__ import annotations

import json
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from tests import test_frontend_browser_behavior as _browser

_launch_chromium = _browser._launch_chromium

OWNER = {"id": 9, "username": "shop-owner", "role": "owner", "tenant": "王记火锅",
         "modules": ["content", "avatar", "library", "restaurant"], "all_modules": [],
         "industries": ["restaurant"]}
META = {"app": {"name": "派活", "slogan": "测试"}, "stations": []}
TEAM = {
    "tenant": {"name": "王记火锅", "balance": 500},
    "users": [
        {"id": 9, "username": "shop-owner", "role": "owner", "modules": [], "job_title": "",
         "allowed_emp_idxs": None, "enabled": 1, "branch_ids": []},
        {"id": 30, "username": "xiaoli", "role": "member", "modules": ["restaurant"],
         "job_title": "staff", "allowed_emp_idxs": [], "enabled": 1, "branch_ids": [3]},
    ],
    "all_modules": [{"key": "content", "label": "🎬 内容生产部"},
                    {"key": "restaurant", "label": "🍜 餐饮"}],
    "industry_employees": [],
    "job_titles": [{"key": "director", "label": "总监"}, {"key": "manager", "label": "经理"},
                   {"key": "staff", "label": "员工"}],
    "my_job_title": "", "is_admin": True, "can_allocate": True,
    "store_branches": {"items": [
        {"id": 3, "industry_key": "restaurant", "store_code": "", "name": "人民路店", "region": "上海"},
        {"id": 4, "industry_key": "restaurant", "store_code": "", "name": "中山路店", "region": "上海"},
    ], "truncated": False},
}


class TeamCreateMobileTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def _open(self, playwright, hash_, payloads, *, width=375, me=OWNER, post=None):
        browser = await _launch_chromium(playwright, self.executable)
        context = await browser.new_context(viewport={"width": width, "height": 812})
        page = await context.new_page()
        errors = self._capture_unexpected_browser_errors(page)
        await page.add_init_script("window.EventSource = class { constructor(){} close(){} };")
        api = {"/api/auth/me": me, "/api/meta": META,
               "/api/state": {"inbox": [], "notifications": [], "jobs": [], "balance": 500},
               "/api/employees": [], "/api/depts": [], **payloads}
        self.posts = []

        async def api_route(route):
            path = urlparse(route.request.url).path
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

    async def test_create_store_staff_on_phone_and_send_card(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/team", {"/api/team": TEAM},
                post={"/api/team/users": {"id": 31, "username": "xiaowang",
                                          "initial_password": "k7mn2pqr8stv3wxy", "branch_ids": [4]}})
            await page.wait_for_selector("text=新建账号")
            # 已分门店的员工在团队页也叫「店员」,和派活页一致
            row = page.locator(".topic", has_text="xiaoli")
            self.assertIn("店员", await row.inner_text())
            # 店员行只露三个常用按钮，直接写出负责哪家店；其余收进「⋯ 更多」
            self.assertIn("负责门店：人民路店", await row.inner_text())
            visible = [b.strip() for b in await row.locator("button:visible").all_inner_texts()]
            self.assertEqual(["🏪 负责门店", "🔑 重置密码", "⏸ 停用"], visible)
            self.assertTrue(await row.locator(".tm-row-more summary", has_text="⋯ 更多").is_visible())
            self.assertFalse(await row.get_by_role("button", name="🧩 行业/板块").is_visible())
            await row.locator(".tm-row-more summary").click()
            self.assertTrue(await row.get_by_role("button", name="🧩 行业/板块").is_visible())
            self.assertTrue(await row.get_by_role("button", name="🤝 分配 AI 专家").is_visible())
            await row.locator(".tm-row-more summary").click()
            row_height = await row.evaluate("e=>e.getBoundingClientRect().height")
            self.assertLess(row_height, 160)   # 以前 7 个按钮要折 3 行以上
            await page.get_by_role("button", name="➕ 新建账号").click()
            await page.wait_for_selector("#tm-create")
            kinds = await page.locator("#tm-create .tm-kind b").all_inner_texts()
            self.assertEqual(["🧑‍🍳 店员", "🏪 店长", "💼 办公室同事"], kinds)
            # 店员:门店是复选框,不是原生多选;职级和 AI 板块藏起来
            self.assertEqual(0, await page.locator("#tm-create select[multiple]").count())
            self.assertEqual(2, await page.locator('input[name="tm-store"]').count())
            self.assertFalse(await page.locator("#tm-office-part").is_visible())
            self.assertFalse(await page.locator("#tm-p").is_visible())
            # 办公室同事才看到职级
            await page.locator('.tm-kind[data-k="office"]').click()
            self.assertTrue(await page.locator("#tm-title").is_visible())
            self.assertFalse(await page.locator("#tm-store-part").is_visible())
            await page.locator('.tm-kind[data-k="staff"]').click()
            # 手机上不横向滚动,门店勾选框够大
            layout = await page.evaluate("""() => ({
              doc: document.documentElement.scrollWidth,
              box: Math.min(...[...document.querySelectorAll('.tm-check')].map(e => e.getBoundingClientRect().height)),
              right: Math.max(...[...document.querySelectorAll('#tm-create *')].map(e => e.getBoundingClientRect().right)),
            })""")
            self.assertLessEqual(layout["doc"], 375)
            self.assertLessEqual(layout["right"], 375)
            self.assertGreaterEqual(layout["box"], 44)
            # 空提交:一次标出账号没填
            await page.locator("#tm-create-ok").click()
            self.assertEqual("请填写登录账号", await page.locator("#tm-u-err").inner_text())
            self.assertEqual([], self.posts)
            await page.fill("#tm-u", "xiaowang")
            await page.locator(".tm-check", has_text="中山路店").click()
            await page.locator("#tm-create-ok").click()
            await page.wait_for_selector("#tm-sendcard")
            path, body = self.posts[0]
            self.assertEqual("/api/team/users", path)
            self.assertNotIn("password", body)  # 交给服务端生成
            self.assertEqual({"username": "xiaowang", "job_title": "staff", "modules": [],
                              "allowed_emp_idxs": [], "branch_ids": [4]}, body)
            text = await page.locator("#tm-send-text").inner_text()
            origin = await page.evaluate("location.origin")
            # 有品牌、有用途、有首登提醒，店员收到不会当成陌生链接
            self.assertEqual("【王记火锅】给您开了派活账号，用来看每天的活、拍照交差：\n"
                             f"账号：xiaowang\n初始密码：k7mn2pqr8stv3wxy\n手机打开：{origin}/staff\n"
                             "第一次登录会让您改一个自己好记的密码。", text.strip())
            self.assertIn("只显示这一次", await page.locator("#tm-sendcard").inner_text())
            await page.locator("#tm-send-copy").click()
            await page.wait_for_function("document.querySelector('#tm-send-copy').textContent.includes('已复制')")
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_create_form_resets_to_staff_each_time(self):
        """O6-1:刚建完店长,再点「新建账号」回到默认「店员」,账号/密码空着;只有一家店才默认勾上。"""
        one_store = {**TEAM, "store_branches": {"items": TEAM["store_branches"]["items"][:1], "truncated": False}}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/team", {"/api/team": one_store},
                post={"/api/team/users": {"id": 32, "username": "laozhang",
                                          "initial_password": "k7mn2pqr8stv3wxy", "branch_ids": [3]}})
            await page.get_by_role("button", name="➕ 新建账号").click()
            await page.wait_for_selector("#tm-create")
            self.assertTrue(await page.locator('input[name="tm-store"]').is_checked())
            await page.locator('.tm-kind[data-k="manager"]').click()
            await page.fill("#tm-u", "laozhang")
            await page.locator("#tm-p-auto").uncheck()
            await page.fill("#tm-p", "abc")
            await page.locator("#tm-p-auto").check()
            await page.locator("#tm-create-ok").click()
            await page.wait_for_selector("#tm-sendcard")
            self.assertEqual("manager", self.posts[0][1]["job_title"])
            await page.get_by_role("button", name="我发好了").click()
            await page.get_by_role("button", name="➕ 新建账号").click()
            await page.wait_for_selector("#tm-create")
            checked = await page.locator("#tm-create .tm-kind[aria-checked='true']").get_attribute("data-k")
            self.assertEqual("staff", checked)
            self.assertEqual("", await page.input_value("#tm-u"))
            self.assertEqual("", await page.input_value("#tm-p"))
            self.assertTrue(await page.locator("#tm-p-auto").is_checked())
            self.assertTrue(await page.locator('input[name="tm-store"]').is_checked())
            # 取消后再开也一样回到店员
            await page.locator('.tm-kind[data-k="office"]').click()
            await page.locator("#tm-create").get_by_role("button", name="取消", exact=True).click()
            await page.get_by_role("button", name="➕ 新建账号").click()
            self.assertEqual("staff", await page.locator(
                "#tm-create .tm-kind[aria-checked='true']").get_attribute("data-k"))
            self.assertFalse(await page.locator("#tm-office-part").is_visible())
            self._assert_no_browser_errors(errors)
            await browser.close()
        # 两家店:不替老板默认勾任何一家
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/team", {"/api/team": TEAM})
            await page.get_by_role("button", name="➕ 新建账号").click()
            await page.wait_for_selector("#tm-create")
            self.assertEqual(0, await page.locator('input[name="tm-store"]:checked').count())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_branch_form_uses_checkboxes_and_opens_industry(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/team", {"/api/team": TEAM},
                post={"/api/team/users/30/branches": {"user_id": 30, "branch_ids": [3, 4]}})
            await page.wait_for_selector("text=新建账号")
            await page.locator(".topic", has_text="xiaoli").get_by_role("button", name="🏪 负责门店").click()
            await page.wait_for_selector('input[name="tm-branch"]')
            self.assertEqual(0, await page.locator("select[multiple]").count())
            self.assertTrue(await page.locator('input[name="tm-branch"][value="3"]').is_checked())
            await page.locator(".tm-check", has_text="中山路店").click()
            await page.get_by_role("button", name="💾 保存门店").click()
            for _ in range(50):
                if self.posts:
                    break
                await page.wait_for_timeout(50)
            self.assertEqual(("/api/team/users/30/branches", {"branch_ids": [3, 4], "open_industries": True}),
                             self.posts[0])
            self.assertIn("🔑 重置密码", await page.locator(".topic", has_text="xiaoli").inner_text())
            self.assertIn("了解更多", await page.locator("#main").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_forced_password_page_has_no_reconnect_dot_and_inline_error(self):
        member = {"id": 30, "username": "xiaoli", "role": "member", "job_title": "staff",
                  "tenant": "王记火锅", "modules": ["restaurant"], "all_modules": [],
                  "must_change_password": True, "password_change_needs_old": False}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/", {}, me=member)
            await page.wait_for_selector("#forced-pw-new")
            self.assertFalse(await page.locator("#fb-btn").is_visible())
            self.assertFalse(await page.locator("#conn").is_visible())
            notice = await page.locator("#forced-pw-form .notice").inner_text()
            self.assertNotIn(",", notice)
            await page.fill("#forced-pw-new", "short")
            await page.fill("#forced-pw-confirm", "short")
            await page.locator("#forced-pw-confirm").press("Enter")
            await page.wait_for_function("document.querySelector('#forced-pw-err').textContent.length>0")
            self.assertIn("12", await page.locator("#forced-pw-err").inner_text())
            self.assertEqual("true", await page.locator("#forced-pw-new").get_attribute("aria-invalid"))
            await page.wait_for_timeout(3500)  # 不会像提示条那样自己消失
            self.assertTrue(await page.locator("#forced-pw-err").is_visible())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_billing_ledger_merges_full_refund_pair(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/", {}, width=1280)
            await page.wait_for_function("typeof billMergeRefundPairs==='function'")
            rows = await page.evaluate("""() => billMergeRefundPairs([
              {delta: 0.2, balance: 500, reason: '退回:店员交差·AI 验照片 · 模型超时', created_at: 3},
              {delta: -0.2, balance: 499.8, reason: '店员交差·AI 验照片 · 任务#5', created_at: 2},
              {delta: -1, balance: 500, reason: '扫描今日热点', created_at: 1},
              {delta: 2, balance: 501, reason: '退回:数字人视频 · 失败', created_at: 0},
            ])""")
            self.assertEqual(3, len(rows))
            self.assertEqual("AI 没看成，未扣点", rows[0]["merged"])
            self.assertEqual(0, rows[0]["delta"])
            self.assertEqual(500, rows[0]["balance"])
            self.assertEqual(-1, rows[1]["delta"])
            self.assertNotIn("merged", rows[2])  # 找不到同额扣点的退回保持原样
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_staff_tasks_filters_in_one_row_and_ai_toggle_by_review_heading(self):
        meta = {"can_dispatch": True, "can_review": True, "sees_all": True, "role_label": "老板",
                "ai_check_enabled": True, "prices": {"staff_parse": 0.2, "staff_ai_check": 0.2},
                "branches": [{"id": 3, "name": "人民路店", "members": []},
                             {"id": 4, "name": "中山路店", "members": []}]}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/staff-tasks", {"/api/staff/meta": meta,
                                              "/api/staff/tasks": {"items": [], "next_before_id": None}})
            await page.wait_for_selector(".sa-bar")
            tops = await page.evaluate(
                "[...document.querySelectorAll('.sa-bar select')].map(e=>Math.round(e.getBoundingClientRect().top))")
            self.assertEqual(2, len(tops))
            self.assertEqual(tops[0], tops[1])
            self.assertEqual(0, await page.locator(".sa-bar input[type=checkbox]").count())
            self.assertIn("AI 先看照片", await page.locator("h3", has_text="等您审核").inner_text())
            await page.get_by_role("button", name="✍️ 手动派一件").click()
            await page.get_by_role("button", name="📤 确认派出去(1 件)").click()
            draft = page.locator("[data-sa-draft='0']")
            await page.wait_for_function("document.querySelectorAll('[data-sa-draft] .field-err').length===2")
            text = await draft.inner_text()
            self.assertIn("请写要做什么", text)
            self.assertIn("请选择门店", text)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_single_store_is_preselected_and_no_staff_is_said_plainly(self):
        # 只有一家店：「哪家店」自动选上；还没有店员账号：页面和派出后的提示都如实说
        meta = {"can_dispatch": True, "can_review": True, "sees_all": True, "role_label": "老板",
                "ai_check_enabled": False, "prices": {"staff_parse": 0.2, "staff_ai_check": 0.2},
                "branches": [{"id": 6, "name": "忘记火锅·春熙路店", "members": []}]}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/staff-tasks", {"/api/staff/meta": meta,
                                              "/api/staff/tasks": {"items": [], "next_before_id": None}})
            await page.wait_for_selector("#sa-nostaff")
            hint = await page.locator("#sa-nostaff").inner_text()
            self.assertIn("还没有店长或店员账号", hint)
            self.assertEqual("#/team", await page.locator("#sa-nostaff a").get_attribute("href"))
            await page.get_by_role("button", name="✍️ 手动派一件").click()
            self.assertEqual("6", await page.locator("[data-sa-draft='0'] select").first.input_value())
            # 门店信息晚到的草稿(branch_id 为空)在重画时也会补上
            await page.evaluate("PH_STAFF_ADMIN._set(0,'branch_id','',true)")
            self.assertEqual("6", await page.locator("[data-sa-draft='0'] select").first.input_value())
            self._assert_no_browser_errors(errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
