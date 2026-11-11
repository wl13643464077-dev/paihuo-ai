"""第四轮 N-01/N-02/N-03/N-10/N-16/N-25 浏览器行为:成员看不到企业余额,
用不了的入口不出现、直接打开也有一句话说清楚;商量室不列巡店经理;店长的团队页叫法一致。"""
from __future__ import annotations

import json
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from tests import test_frontend_browser_behavior as _browser
from tests import test_frontend_flows_feedback as _flows

_launch_chromium = _browser._launch_chromium

MANAGER = {**_flows.MEMBER, "id": 22, "username": "laowang", "job_title": "manager",
           "modules": ["restaurant"], "can_allocate": True,
           "expert_count": 0, "meeting_expert_count": 0}

NO_EXPERT = "老板还没给您开通 AI 专家"


class FrontendMemberScopeTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def _open(self, playwright, *, me, hash_="#/", state=None, extra=None, width=390):
        browser = await _launch_chromium(playwright, self.executable)
        page = await browser.new_page(viewport={"width": width, "height": 812})
        errors = self._capture_unexpected_browser_errors(page)
        await page.add_init_script("window.EventSource = class { constructor(){} close(){} };")
        payloads = _flows._api(100)
        payloads["/api/auth/me"] = me
        if state is not None:
            payloads["/api/state"] = {**payloads["/api/state"], **state}
        payloads.update(extra or {})

        async def api_route(route):
            path = urlparse(route.request.url).path
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payloads.get(path, {}), ensure_ascii=False))

        await page.route("**/api/**", api_route)
        await page.goto(f"{self.base}/static/index.html?full=1{hash_}")
        await page.wait_for_function("document.querySelector('#nav')?.textContent.includes('退出')")
        return browser, page, errors

    async def test_member_cost_button_has_no_balance_and_points_short_asks_boss(self):
        # 服务端给成员的 state:没有余额数字,只有「够不够」
        state = {"balance": None, "points_ok": False,
                 "points_ok_by_cost": {"0.2": True, "12": False}}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, me=_flows.MEMBER, state=state)
            result = await page.evaluate(
                """() => {
                  window.__ran = 0;
                  const box = document.createElement('div');
                  box.innerHTML = pointsAction({label:'🚀 派活', onclick:'window.__ran=1', cost:12, id:'t-go'})
                    + pointsAction({label:'拆成任务', onclick:'', cost:0.2, id:'t-cheap'});
                  document.querySelector('#main').appendChild(box);
                  const before = location.hash;
                  document.querySelector('#t-go').click();
                  return {text: document.querySelector('#t-go').textContent,
                          note: document.querySelector('#t-go-cost').textContent,
                          cheap: document.querySelector('#t-cheap').textContent,
                          cheapNote: document.querySelector('#t-cheap-cost').textContent,
                          ran: window.__ran, moved: location.hash !== before,
                          toast: document.querySelector('#toast-stack')?.textContent || ''};
                }""")
            self.assertEqual("点数不够，请找老板", result["text"])
            self.assertEqual("本次 12 点", result["note"])
            self.assertEqual("拆成任务", result["cheap"])
            self.assertEqual("本次 0.2 点", result["cheapNote"])
            self.assertEqual(0, result["ran"])
            self.assertFalse(result["moved"])   # 不去套餐页
            self.assertIn("点数由老板管理，请联系老板充值", result["toast"])
            self.assertNotIn("余额", await page.locator("body").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_manager_without_experts_sees_no_dead_entries(self):
        async with async_playwright() as playwright:
            # 服务端已按板块和白名单过滤,这个店长一位专家都拿不到
            depts = [{**d, "employees": []} for d in _flows.DEPTS]
            browser, page, errors = await self._open(
                playwright, me=MANAGER, hash_="#/dispatch", extra={"/api/depts": depts})
            await page.wait_for_function("document.querySelector('#main')?.textContent.trim().length > 0")
            cards = await page.evaluate(
                "[...document.querySelectorAll('#main .hubcard .hc-t')].map(x=>x.textContent)")
            for hidden in ("找行业专家", "多位专家一起商量", "协同小队", "派出去的活", "下达任务(内容)"):
                self.assertNotIn(hidden, cards)
            # 直接打开这些页面:一打开就说清楚,不给整张表
            for hash_, title in (("#/new", "下达任务"), ("#/meetings", "多位专家一起商量"),
                                 ("#/experts", "找行业专家"), ("#/teamruns", "协同小队"),
                                 ("#/tools", "获客工具")):
                with self.subTest(hash_=hash_):
                    await page.evaluate(f"location.hash={json.dumps(hash_)}")
                    await page.wait_for_function(
                        "document.querySelector('#main [data-no-expert]')"
                        f"?.textContent.includes({json.dumps(title)})")
                    text = await page.locator("#main").inner_text()
                    self.assertIn("老板还没给您开通", text)
                    self.assertEqual(0, await page.locator("#main textarea").count())
                    self.assertEqual(0, await page.locator("#mt-go, .mt-chip").count())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_member_experts_page_counts_follow_usable_experts(self):
        # 服务端已按白名单过滤:餐饮只给了 1 位,宠物板块没开通
        depts = [{**_flows.DEPTS[0], "tagline": "60 位餐饮行业专家，一人一岗",
                  "groups": [{"name": "餐饮·经营", "emoji": "📂"}],
                  "employees": _flows.DEPTS[0]["employees"][:1]},
                 {**_flows.DEPTS[1], "employees": []}]
        me = {**_flows.MEMBER, "expert_count": 1, "meeting_expert_count": 1}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=me, hash_="#/experts", extra={"/api/depts": depts})
            await page.wait_for_selector("#main .deptsec")
            heads = await page.evaluate(
                "[...document.querySelectorAll('#main .depthead')].map(x=>x.textContent.replace(/\\s+/g,' ').trim())")
            self.assertEqual(1, len(heads))
            self.assertIn("1人", heads[0])
            self.assertIn("1 位餐饮行业专家", heads[0])
            self.assertNotIn("60 位", heads[0])
            # 只有 1 位专家:商量室说清楚还差人,不列选人
            await page.evaluate("location.hash='#/meetings'")
            await page.wait_for_selector("#main [data-no-expert]")
            self.assertIn("至少要 2 位", await page.locator("#main").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_meetings_picker_skips_inspection_manager(self):
        meta = {**_flows.META, "stations": [
            {**_flows._employee(1, "选题策划", "内容"), "color": "#fff"},
            {**_flows._employee(10, "巡店经理", "内容"), "color": "#fff"},
        ]}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=_flows.OWNER, hash_="#/meetings", extra={"/api/meta": meta}, width=1280)
            await page.wait_for_selector("#mt-go")
            self.assertEqual(1, await page.locator('.mt-chip[data-i="1"]').count())
            self.assertEqual(0, await page.locator('.mt-chip[data-i="10"]').count())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_store_manager_team_page_uses_store_words(self):
        team = {"is_admin": False, "my_job_title": "manager", "can_allocate": True,
                "tenant": {"name": "王记火锅"}, "all_modules": [], "industry_employees": [],
                "job_titles": [],
                "users": [
                    {"id": 22, "username": "laowang", "role": "member", "job_title": "manager",
                     "modules": ["restaurant"], "enabled": 1, "branch_names": ["春熙路店"]},
                    {"id": 24, "username": "xiaoli", "role": "member", "job_title": "staff",
                     "modules": ["restaurant"], "enabled": 1, "branch_names": ["春熙路店"]},
                    {"id": 25, "username": "xiaozhang", "role": "member", "job_title": "staff",
                     "modules": ["restaurant"], "enabled": 1, "branch_names": ["建设路店"]},
                ]}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=MANAGER, hash_="#/team", extra={"/api/team": team})
            await page.wait_for_selector("#tm-crew")
            text = await page.locator("#main").inner_text()
            self.assertIn("您是店长，负责 春熙路店", text)
            self.assertNotIn("经理", text)
            self.assertNotIn("数字员工", text)
            self.assertNotIn("分配", text)
            rows = await page.locator("#tm-crew [data-crew-row]").all_inner_texts()
            self.assertEqual(1, len(rows))   # 只列同一家店的店员
            self.assertIn("xiaoli", rows[0])
            self.assertIn("店员", rows[0])
            self.assertIn("春熙路店", rows[0])
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_member_forced_password_copy_matches_role(self):
        staff = {**_flows.MEMBER, "must_change_password": True, "prefers_staff_home": True,
                 "password_change_needs_old": False}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, me=staff)
            await page.wait_for_selector("#forced-pw-why")
            why = await page.locator("#forced-pw-why").inner_text()
            self.assertIn("之后就能看今天的活、拍照交差", why)
            self.assertNotIn("数字员工", why)
            self._assert_no_browser_errors(errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
