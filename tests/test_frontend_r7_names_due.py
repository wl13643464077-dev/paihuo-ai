"""第七轮 B 组的浏览器行为：账号姓名、截止时间相对写法、任务卡按钮收拢、
清单统计口径、套餐页零值写法、今天页日期来源。"""
from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timedelta
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from app import timeutil
from tests import test_frontend_browser_behavior as _browser

_launch_chromium = _browser._launch_chromium


def _cn(y, m, d, hh, mm=0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=timeutil.CN_TZ).timestamp()


OWNER = {"id": 9, "username": "wangji", "name": "王总", "display_name": "王总",
         "role": "owner", "tenant": "王记火锅", "job_title": "",
         "modules": ["content", "avatar", "library", "restaurant"], "all_modules": [],
         "industries": ["restaurant"]}
META = {"app": {"name": "派活", "slogan": "测试"}, "stations": []}
STATE = {"inbox": [], "notifications": [], "jobs": [], "balance": 500,
         "today": "2026-11-24", "today_text": "11 月 24 日 周二"}
TEAM = {
    "tenant": {"name": "王记火锅", "balance": 500},
    "users": [
        {"id": 9, "username": "wangji", "display_name": "王总", "name": "王总", "role": "owner",
         "modules": [], "job_title": "", "allowed_emp_idxs": None, "enabled": 1, "branch_ids": []},
        {"id": 30, "username": "xiaoli", "display_name": "小李", "name": "小李", "role": "member",
         "modules": ["restaurant"], "job_title": "staff", "allowed_emp_idxs": [], "enabled": 1,
         "branch_ids": [3]},
        {"id": 31, "username": "laowang", "display_name": "", "name": "laowang", "role": "member",
         "modules": ["restaurant"], "job_title": "manager", "allowed_emp_idxs": [], "enabled": 1,
         "branch_ids": [3]},
    ],
    "all_modules": [{"key": "content", "label": "🎬 内容生产部"},
                    {"key": "restaurant", "label": "🍜 餐饮"}],
    "industry_employees": [],
    "job_titles": [{"key": "director", "label": "总监"}, {"key": "manager", "label": "经理"},
                   {"key": "staff", "label": "员工"}],
    "my_job_title": "", "is_admin": True, "can_allocate": True,
    "store_branches": {"items": [
        {"id": 3, "industry_key": "restaurant", "store_code": "", "name": "人民路店", "region": "上海"},
    ], "truncated": False},
}
STAFF_META = {"can_dispatch": True, "can_review": True, "sees_all": True, "role_label": "老板",
              "ai_check_enabled": True, "ai_check_asked": True,
              "prices": {"staff_parse": 0.2, "staff_ai_check": 0.2},
              "branches": [{"id": 3, "name": "人民路店", "members": [
                  {"id": 22, "name": "王店长", "username": "laowang", "job_title": "manager",
                   "title_label": "店长"},
                  {"id": 23, "name": "小李", "username": "xiaoli", "job_title": "staff",
                   "title_label": "店员"},
                  {"id": 24, "name": "xiaozhang", "username": "xiaozhang", "job_title": "staff",
                   "title_label": "店员"}]}]}


def _tomorrow_1644() -> float:
    day = timeutil.now_cn() + timedelta(days=1)
    return _cn(day.year, day.month, day.day, 16, 44)


def _staff_tasks() -> dict:
    now = time.time()
    base = {"branch_id": 3, "branch_name": "人民路店", "assignee_user_id": 23,
            "assignee_name": "小李", "assignee_username": "xiaoli", "require_photo": True,
            "dispatcher_label": "王总（老板）", "created_by_name": "王总", "photos": [],
            "review_note": "", "reviewed_at": None, "remind_count": 0}
    return {"items": [
        {**base, "id": 60, "title": "冷柜除霜", "status": "todo", "overdue": True,
         "due_at": now - 3 * 3600 - 120, "due_text": "11-24 20:44"},
        {**base, "id": 61, "title": "擦玻璃", "status": "todo", "overdue": False,
         "due_at": _tomorrow_1644(), "due_text": "11-25 16:44"},
        {**base, "id": 62, "title": "补货", "status": "submitted", "overdue": False,
         "due_at": now - 86400, "due_text": "11-23 18:00",
         "photos": [{"id": 1, "url": "/static/img/avatar.png"}]},
        {**base, "id": 63, "title": "换灯泡", "status": "approved", "overdue": False,
         "due_at": now - 2 * 86400, "due_text": "11-22 18:00"},
    ], "next_before_id": None}


class R7NamesDueBrowserTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def _open(self, playwright, hash_, payloads=None, *, width=1280, writes=None,
                    timezone_id=None, me=OWNER, state=STATE, page_path="/static/index.html"):
        browser = await _launch_chromium(playwright, self.executable)
        options = {"viewport": {"width": width, "height": 900}}
        if timezone_id:
            options["timezone_id"] = timezone_id
        context = await browser.new_context(**options)
        page = await context.new_page()
        errors = self._capture_unexpected_browser_errors(page)
        await page.add_init_script("window.EventSource = class { constructor(){} close(){} };")
        api = {"/api/auth/me": me, "/api/meta": META, "/api/state": state,
               "/api/employees": [], "/api/depts": [], **(payloads or {})}
        self.writes = []

        async def api_route(route):
            path = urlparse(route.request.url).path
            if path.startswith("/api/funnel/"):     # 页面浏览埋点，和本测试无关
                payload = {"ok": True}
            elif route.request.method in ("POST", "PUT", "DELETE"):
                self.writes.append((route.request.method, path,
                                    json.loads(route.request.post_data or "{}")))
                payload = (writes or {}).get(path, {"ok": True})
            else:
                payload = api.get(path, {})
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        await page.route("**/api/**", api_route)
        await page.goto(f"{self.base}{page_path}{hash_}")
        return browser, page, errors

    # ---------- 2. 截止时间相对写法 ----------
    async def test_due_info_is_relative_in_beijing_time_whatever_the_browser_timezone(self):
        now = _cn(2026, 11, 24, 12, 0)
        cases = [
            (_cn(2026, 11, 24, 20, 44), True, "今天 20:44", False),
            (_cn(2026, 11, 25, 16, 44), True, "明天 16:44", False),
            (_cn(2026, 11, 26, 9, 0), True, "后天 09:00", False),
            (_cn(2026, 11, 28, 16, 44), True, "11-28 16:44", False),
            (_cn(2027, 1, 2, 8, 5), True, "2027-01-02 08:05", False),
            (_cn(2026, 11, 24, 8, 30), True, "已逾期 3 小时", True),
            (_cn(2026, 11, 24, 11, 40), True, "已逾期 20 分钟", True),
            (_cn(2026, 11, 22, 11, 0), True, "已逾期 2 天", True),
            # 已经做完的事不说逾期，只写原定时间
            (_cn(2026, 11, 22, 11, 0), False, "11-22 11:00", False),
            (_cn(2026, 11, 24, 8, 30), False, "今天 08:30", False),
        ]
        async with async_playwright() as playwright:
            # 浏览器在纽约：北京 11-24 00:30 在当地还是 11-23，仍要按北京时间写「今天」
            browser, page, errors = await self._open(
                playwright, "#/", timezone_id="America/New_York")
            await page.wait_for_function("typeof dueInfo==='function'")
            for ts, is_open, text, overdue in cases:
                got = await page.evaluate("([ts,open,now])=>dueInfo(ts,open,now)", [ts, is_open, now])
                self.assertEqual((text, overdue), (got["text"], got["overdue"]), text)
            midnight = await page.evaluate(
                "([ts,now])=>dueInfo(ts,true,now)",
                [_cn(2026, 11, 24, 0, 30), _cn(2026, 11, 23, 23, 50)])
            self.assertEqual("明天 00:30", midnight["text"])
            self.assertEqual("2026-11-24 00:30", midnight["full"])
            self.assertIsNone(await page.evaluate("dueInfo(null,true)"))
            html = await page.evaluate(
                "([ts,now])=>dueHtml(ts,true,now)", [_cn(2026, 11, 24, 8, 30), now])
            self.assertIn("已逾期 3 小时", html)
            self.assertIn('title="原定 2026-11-24 08:30 截止"', html)
            html = await page.evaluate(
                "([ts,now])=>dueHtml(ts,true,now)", [_cn(2026, 11, 25, 16, 44), now])
            self.assertIn("截止 明天 16:44", html)
            self.assertIn('title="2026-11-25 16:44 截止"', html)
            self.assertEqual("小李（店员）", await page.evaluate("personWithRole('小李','店员')"))
            self.assertEqual("王店长", await page.evaluate("personWithRole('王店长','店长')"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 3. 老板端任务卡：只露「催一下」，其余收进「⋯ 更多」 ----------
    async def test_boss_task_card_shows_names_relative_due_and_collapsed_actions(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/staff-tasks",
                {"/api/staff/meta": STAFF_META, "/api/staff/tasks": _staff_tasks(),
                 "/api/staff/tasks/60": {"events": [
                     {"id": 1, "kind": "created", "note": "派给「人民路店」", "actor_name": "王总",
                      "created_at": 1790000000}], "earlier_photos": []}})
            await page.wait_for_selector("#sa-task-60")
            overdue = page.locator("#sa-task-60")
            text = await overdue.inner_text()
            self.assertIn("👤 小李", text)
            self.assertNotIn("xiaoli", text)
            self.assertIn("王总（老板） 派的", text)
            self.assertIn("已逾期 3 小时", text)
            self.assertNotIn("11-24 20:44", text)
            self.assertRegex(await overdue.locator(".due-rel").get_attribute("title"),
                             r"^原定 \d{4}-\d{2}-\d{2} \d{2}:\d{2} 截止$")
            # 平铺的只剩「催一下」；逾期的用主按钮
            visible = [b.strip() for b in await overdue.locator("button:visible").all_inner_texts()]
            self.assertEqual(["🔔 催一下"], visible)
            self.assertEqual(0, await overdue.locator("select:visible").count())
            nudge = overdue.get_by_role("button", name="🔔 催一下")
            self.assertIn("pri", (await nudge.get_attribute("class")).split())
            later = page.locator("#sa-task-61")
            self.assertIn("截止 明天 16:44", await later.inner_text())
            self.assertNotIn("pri", (await later.get_by_role("button", name="🔔 催一下")
                                     .get_attribute("class")).split())
            # 键盘可达：Tab 到「⋯ 更多」，回车展开，里面是改派给谁 / 取消这件 / 经过
            summary = overdue.locator("details.sa-more > summary")
            self.assertEqual("⋯ 更多", (await summary.inner_text()).strip())
            await summary.focus()
            await page.keyboard.press("Enter")
            box = overdue.locator(".tm-row-more-box")
            await box.wait_for(state="visible")
            self.assertEqual(["取消这件", "🕘 经过"],
                             [b.strip() for b in await box.locator("button").all_inner_texts()])
            self.assertEqual("改派给谁", (await box.locator("label").inner_text()).strip())
            options = [o.strip() for o in await box.locator("select option").all_inner_texts()]
            # 姓名里带了身份的不重复；没填姓名的显示登录名；自己不在改派名单里
            self.assertEqual(["请选择", "王店长", "xiaozhang（店员）"], options)
            # Esc 收起，焦点回到「更多」
            await box.locator("select").focus()
            await page.keyboard.press("Escape")
            self.assertFalse(await box.is_visible())
            self.assertTrue(await summary.evaluate("e=>document.activeElement===e"))
            # 再展开：点「经过」后菜单收起，经过列在卡片下面，人名是姓名
            await summary.click()
            await box.get_by_role("button", name="🕘 经过").click()
            await page.wait_for_selector("#sa-ev-60 .sa-events li")
            self.assertFalse(await box.is_visible())
            self.assertIn("王总", await page.locator("#sa-ev-60").inner_text())
            # 点到别处也会收起
            await summary.click()
            await box.wait_for(state="visible")
            await page.locator("#sa-task-61 b").first.click()
            self.assertFalse(await box.is_visible())
            # 改派：在菜单里选人
            await summary.click()
            await box.locator("select").select_option("22")
            await page.wait_for_function("!document.querySelector('details.sa-more[open]')")
            self.assertIn(("POST", "/api/staff/tasks/60/assign", {"assignee_user_id": 22}),
                          self.writes)
            # 等审核卡：通过 / 打回是主次按钮，取消和经过同样收进更多
            review = page.locator("#sa-task-62")
            visible = [b.strip() for b in await review.locator("button:visible").all_inner_texts()]
            self.assertEqual(["👍 通过", "↩️ 打回重做"], visible)
            self.assertNotIn("已逾期", await review.locator(".sub").first.inner_text())
            await review.locator("details.sa-more > summary").click()
            review_box = review.locator(".tm-row-more-box")
            self.assertEqual(["取消这件", "🕘 经过"],
                             [b.strip() for b in await review_box.locator("button").all_inner_texts()])
            self.assertEqual(0, await review_box.locator("select").count())
            # 已结束的活只有「经过」，不用再套一层更多；过了点也不说逾期
            await page.locator("summary", has_text="已结束（1）").click()
            ended = page.locator("#sa-task-63")
            self.assertEqual(0, await ended.locator("details.sa-more").count())
            self.assertEqual(1, await ended.get_by_role("button", name="🕘 经过").count())
            self.assertNotIn("已逾期", await ended.inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_more_menu_fits_on_375_phone(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/staff-tasks",
                {"/api/staff/meta": STAFF_META, "/api/staff/tasks": _staff_tasks()}, width=375)
            await page.wait_for_selector("#sa-task-60")
            await page.locator("#sa-task-60 details.sa-more > summary").click()
            box = page.locator("#sa-task-60 .tm-row-more-box")
            await box.wait_for(state="visible")
            rect = await box.evaluate("e=>{const r=e.getBoundingClientRect();return [r.left,r.right]}")
            self.assertGreaterEqual(rect[0], 0)
            self.assertLessEqual(rect[1], 375)
            heights = await box.evaluate(
                "e=>[...e.querySelectorAll('button,select')].map(x=>x.getBoundingClientRect().height)")
            self.assertGreaterEqual(min(heights), 44)       # 手机上好点
            self.assertLessEqual(await page.evaluate("document.documentElement.scrollWidth"), 375)
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 1. 团队页：姓名 ----------
    async def test_team_rows_show_name_with_login_and_fill_in_entry(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/team", {"/api/team": TEAM})
            await page.wait_for_selector("[data-member-row='30']")
            named = page.locator("[data-member-row='30']")
            self.assertIn("小李（xiaoli）", (await named.inner_text()).replace("\n", ""))
            self.assertEqual(0, await named.locator("[data-name-missing]").count())
            # 顶栏和「我」这一行也用姓名
            self.assertIn("王总", await page.locator(".nav-who").inner_text())
            self.assertIn("王总（wangji）（我）",
                          (await page.locator("[data-member-row='9']").inner_text()).replace("\n", ""))
            # 没填姓名：显示登录名，旁边有「补上姓名」
            unnamed = page.locator("[data-member-row='31']")
            self.assertIn("laowang", await unnamed.inner_text())
            await unnamed.locator("[data-name-missing]", has_text="补上姓名").click()
            await page.wait_for_selector("#ui-dialog")
            self.assertIn("登录账号「laowang」不变", await page.locator("#ui-dialog").inner_text())
            await page.fill("#ui-dialog-field", "王" * 21)
            await page.locator("#ui-dialog [data-dialog-ok]").click()
            self.assertEqual("姓名最多 20 个字", await page.locator("#ui-dialog-error").inner_text())
            self.assertEqual([], self.writes)
            await page.fill("#ui-dialog-field", "  王店长 ")
            await page.locator("#ui-dialog [data-dialog-ok]").click()
            await page.wait_for_function("!document.querySelector('#ui-dialog')")
            self.assertEqual([("PUT", "/api/team/users/31", {"display_name": "王店长"})], self.writes)
            # 已有姓名的在「⋯ 更多」里改
            await named.locator(".tm-row-more summary").click()
            await named.get_by_role("button", name="✏️ 改姓名").click()
            await page.wait_for_selector("#ui-dialog")
            self.assertEqual("小李", await page.locator("#ui-dialog-field").input_value())
            await page.keyboard.press("Escape")
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_create_form_has_name_before_login_and_send_card_greets_by_name(self):
        created = {"id": 32, "username": "xiaowang", "display_name": "小王", "name": "小王",
                   "initial_password": "k7mn2pqr8stv3wxy", "branch_ids": [3]}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/team", {"/api/team": TEAM}, width=375,
                writes={"/api/team/users": created})
            await page.wait_for_selector("text=新建账号")
            await page.get_by_role("button", name="➕ 新建账号").click()
            await page.wait_for_selector("#tm-create")
            # 姓名在登录账号前面，占位写着例子，打开就聚焦在姓名上
            order = await page.evaluate(
                "[...document.querySelectorAll('#tm-create input[id]')].map(e=>e.id)")
            self.assertLess(order.index("tm-dn"), order.index("tm-u"))
            self.assertEqual("如：小李、王店长", await page.get_attribute("#tm-dn", "placeholder"))
            self.assertEqual("20", await page.get_attribute("#tm-dn", "maxlength"))
            self.assertIn("选填", await page.locator("label[for='tm-dn']").inner_text())
            await page.wait_for_function("document.activeElement&&document.activeElement.id==='tm-dn'")
            # 填了姓名不会自动生成登录账号，登录账号仍要手填
            await page.fill("#tm-dn", " 小王 ")
            self.assertEqual("", await page.input_value("#tm-u"))
            await page.locator("#tm-create-ok").click()
            self.assertEqual("请填写登录账号", await page.locator("#tm-u-err").inner_text())
            self.assertEqual([], self.writes)
            # 姓名里有尖括号：标在姓名这一格
            await page.fill("#tm-dn", "<小王>")
            await page.fill("#tm-u", "xiaowang")
            await page.locator("#tm-create-ok").click()
            self.assertEqual("姓名不能有引号、尖括号等符号",
                             await page.locator("#tm-dn-err").inner_text())
            self.assertEqual([], self.writes)
            await page.fill("#tm-dn", " 小王 ")
            await page.locator("#tm-create-ok").click()
            await page.wait_for_selector("#tm-sendcard")
            method, path, body = self.writes[0]
            self.assertEqual(("POST", "/api/team/users"), (method, path))
            self.assertEqual("小王", body["display_name"])
            self.assertEqual("xiaowang", body["username"])
            text = (await page.locator("#tm-send-text").inner_text()).strip()
            self.assertTrue(text.startswith("小王，【王记火锅】给您开了派活账号，"), text)
            self.assertIn("账号：xiaowang", text)
            self.assertIn("发给小王", await page.locator("#tm-sendcard h3").inner_text())
            self.assertLessEqual(await page.evaluate("document.documentElement.scrollWidth"), 375)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_create_without_name_sends_no_display_name_and_plain_send_card(self):
        created = {"id": 33, "username": "noname", "display_name": "", "name": "noname",
                   "initial_password": "k7mn2pqr8stv3wxy", "branch_ids": [3]}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/team", {"/api/team": TEAM}, writes={"/api/team/users": created})
            await page.wait_for_selector("text=新建账号")
            await page.get_by_role("button", name="➕ 新建账号").click()
            await page.fill("#tm-u", "noname")
            await page.locator("#tm-create-ok").click()
            await page.wait_for_selector("#tm-sendcard")
            self.assertNotIn("display_name", self.writes[0][2])
            text = (await page.locator("#tm-send-text").inner_text()).strip()
            self.assertTrue(text.startswith("【王记火锅】给您开了派活账号，"), text)
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 5. 套餐页零值 ----------
    async def test_billing_zero_values_have_no_sign_and_empty_cells_are_dashes(self):
        billing = {"balance": 20, "is_platform": False, "recharged": 0, "spent": 0, "txn_n": 0,
                   "log": [], "prices": {}, "plans": [], "plan": "", "plan_expires": None,
                   "monthly": [{"ym": "2026-11", "recharged": 0, "spent": 0, "refunded": 0},
                               {"ym": "2026-10", "recharged": 100, "spent": 30.4, "refunded": 0}]}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/billing", {"/api/billing": billing})
            await page.wait_for_function("document.querySelector('#main .kv')")
            kv = (await page.locator("#main .kv").first.inner_text()).replace("\n", " ")
            self.assertIn("累计充值 0", kv)
            self.assertIn("累计消耗 0", kv)
            for bad in ("-0", "−0", "+0"):
                self.assertNotIn(bad, kv)
            rows = await page.evaluate(
                "[...document.querySelectorAll('#main .dimtable')]"
                ".find(t=>t.innerText.includes('月份'))"
                ".querySelectorAll('tbody tr').length")
            self.assertEqual(2, rows)
            cells = await page.evaluate(
                "[...[...document.querySelectorAll('#main .dimtable')]"
                ".find(t=>t.innerText.includes('月份')).querySelectorAll('tbody tr')]"
                ".map(r=>[...r.children].map(c=>c.innerText.trim()))")
            self.assertEqual(["2026-11", "—", "—", "—"], cells[0])
            self.assertEqual(["2026-10", "+100", "-30", "—"], cells[1])
            self.assertEqual(["0", "0", "-12", "+7", "0"], await page.evaluate(
                "[ptsSigned(0,'-'),ptsSigned(-0.3,'-'),ptsSigned(12.2,'-'),ptsSigned(7,'+'),ptsSigned(null,'+')]"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 6. 今天页日期来自服务器 ----------
    async def test_today_page_date_comes_from_server_not_browser_clock(self):
        async with async_playwright() as playwright:
            # 浏览器时区在檀香山(比北京晚 18 小时)，日期仍按服务器给的北京日期写
            browser, page, errors = await self._open(
                playwright, "#/", timezone_id="Pacific/Honolulu")
            await page.wait_for_selector(".today-hello")
            self.assertEqual("11 月 24 日 周二",
                             (await page.locator(".today-hello h2 .sub").inner_text()).strip())
            # 老版本后端没给日期时的兜底：也按北京时间算，写法一致
            fallback = await page.evaluate("(()=>{STATE.today_text='';return todayLabelCn();})()")
            self.assertEqual(timeutil.today_text_cn(), fallback)
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 4. 清单页统计口径 ----------
    async def test_checklist_summary_counts_items_like_the_store_rows(self):
        def run(run_id, branch_id, status, done, total):
            return {"id": run_id, "branch_id": branch_id, "branch_name": f"店{branch_id}",
                    "template_id": 1, "name": "开店清单", "kind": "open", "run_date": "2026-09-21",
                    "status": status, "status_label": status, "due_text": "10:00 前",
                    "assignee_name": "小李", "done_count": done, "total": total, "items": []}
        overview = {"date": "2026-09-21",
                    "summary": {"total": 3, "done": 0, "missed": 1, "open": 2, "rate": 0.0,
                                "items_total": 42, "items_done": 5, "items_missed": 14},
                    "stores": [
                        {"branch_id": 1, "branch_name": "店1", "region": "", "total": 1, "done": 0,
                         "missed": 0, "open": 1, "items_total": 14, "items_done": 5,
                         "items_missed": 0, "runs": [run(1, 1, "open", 5, 14)]},
                        {"branch_id": 2, "branch_name": "店2", "region": "", "total": 1, "done": 0,
                         "missed": 0, "open": 1, "items_total": 14, "items_done": 0,
                         "items_missed": 0, "runs": [run(2, 2, "open", 0, 14)]},
                        {"branch_id": 3, "branch_name": "店3", "region": "", "total": 1, "done": 0,
                         "missed": 1, "open": 0, "items_total": 14, "items_done": 0,
                         "items_missed": 14, "runs": [run(3, 3, "missed", 0, 14)]}]}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "#/checklists",
                {"/api/checklist/runs": overview, "/api/checklist/templates": {"items": []}},
                width=390)
            await page.wait_for_function("document.querySelectorAll('.ck-store').length===3")
            cell = lambda key: page.locator(f".ck-sum [data-ck-sum='{key}']")  # noqa: E731
            self.assertEqual("5 / 42", (await cell("done").locator("b").inner_text()).strip())
            self.assertEqual("已做（项）", (await cell("done").locator("span").inner_text()).strip())
            self.assertEqual("37", (await cell("left").locator("b").inner_text()).strip())
            self.assertEqual("还没做（项）", (await cell("left").locator("span").inner_text()).strip())
            self.assertEqual("14", (await cell("missed").locator("b").inner_text()).strip())
            self.assertEqual("超时没做（项） · 1 份清单",
                             (await cell("missed").locator("span").inner_text()).strip())
            # 上下对得上：顶部的总项数 = 各店「x/y 项」的 y 加起来
            pills = await page.evaluate(
                "[...document.querySelectorAll('.ck-store .pill.running')].map(p=>p.textContent.trim())")
            self.assertEqual(["0/14 项", "5/14 项"], sorted(pills))
            self.assertLessEqual(await page.evaluate("document.documentElement.scrollWidth"), 390)
            self._assert_no_browser_errors(errors)
            await browser.close()

    # ---------- 店员手机版 ----------
    async def test_staff_phone_shows_name_with_role_and_relative_due(self):
        now = time.time()
        task = {"kind": "task", "id": 41, "title": "把冷柜清洗一遍", "detail": "",
                "branch_id": 3, "branch_name": "人民路店", "status": "todo", "require_photo": True,
                "due_at": now - 2 * 86400 - 600, "due_text": "11-22 18:00", "urgency": "overdue",
                "dispatcher_label": "王总（老板）", "created_by_name": "王总",
                "review_note": "", "reviewed_at": None, "photos": [], "can_submit": True}
        later = {**task, "id": 42, "title": "擦玻璃", "urgency": "later",
                 "due_at": _tomorrow_1644(), "due_text": "11-25 16:44"}
        done = {**task, "id": 43, "title": "换灯泡", "status": "approved", "urgency": "approved"}
        todo = {"date": "2026-11-24", "date_text": "11 月 24 日 周二",
                "user": {"id": 23, "name": "小李", "username": "xiaoli", "role_label": "店员",
                         "can_dispatch": False},
                "branches": [{"id": 3, "name": "人民路店"}],
                "items": [task, later, done], "tasks": [], "checklists": [], "actions": [],
                "reviews": [], "counts": {}}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "", {"/api/staff/todo": todo, "/api/staff/tasks/41": task},
                width=390, page_path="/static/staff.html", timezone_id="America/New_York")
            await page.wait_for_function("document.querySelectorAll('.card').length===3")
            self.assertEqual("小李（店员）", (await page.locator("#who").inner_text()).strip())
            # 和老板今天页同一个写法：「11 月 24 日 周二」
            self.assertEqual("今天 11 月 24 日 周二 · 人民路店",
                             (await page.locator("#today").inner_text()).strip())
            cards = await page.locator(".card").all_inner_texts()
            overdue_card = next(c for c in cards if "把冷柜清洗一遍" in c)
            self.assertIn("已逾期 2 天", overdue_card)
            self.assertNotIn("11-22 18:00", overdue_card)
            self.assertIn("截止 明天 16:44", next(c for c in cards if "擦玻璃" in c))
            self.assertNotIn("截止", next(c for c in cards if "换灯泡" in c))
            self.assertRegex(
                await page.locator(".card", has_text="把冷柜清洗一遍").locator(".due-rel")
                .get_attribute("title"), r"^原定 \d{4}-\d{2}-\d{2} \d{2}:\d{2} 截止$")
            await page.locator(".card", has_text="把冷柜清洗一遍").click()
            await page.wait_for_selector("#picker")
            detail = await page.locator(".panel").first.inner_text()
            self.assertIn("王总（老板） 派的", detail)
            self.assertIn("已逾期 2 天", detail)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_staff_phone_does_not_repeat_role_already_in_name(self):
        todo = {"date": "2026-11-24", "date_text": "11 月 24 日 周二",
                "user": {"id": 22, "name": "王店长", "role_label": "店长", "can_dispatch": True},
                "branches": [], "items": [], "tasks": [], "checklists": [], "actions": [],
                "reviews": [], "store_tasks": [], "counts": {}, "has_any_task": True}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, "", {"/api/staff/todo": todo}, width=390,
                page_path="/static/staff.html")
            await page.wait_for_selector(".empty")
            self.assertEqual("王店长", (await page.locator("#who").inner_text()).strip())
            self._assert_no_browser_errors(errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
