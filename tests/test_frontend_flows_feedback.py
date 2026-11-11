"""浏览器行为:花点数按钮、余额不够去充值、中转页「最近用过」、筛选记忆、顶栏账号区、
下达任务三步 + 更多设置、专家商量室按本行业分组、回车提交。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

# 只导入模块,不把对方的 TestCase 类放进本模块命名空间(否则会被重复发现执行)。
from tests import test_frontend_browser_behavior as _browser

_launch_chromium = _browser._launch_chromium
APP_JS = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")

OWNER = {
    "id": 9,
    "username": "shop-owner",
    "role": "owner",
    "tenant": "浏览器测试门店",
    "modules": ["content", "avatar", "library", "restaurant", "pet"],
    "all_modules": [],
    "industries": ["restaurant"],
}

MEMBER = {
    "id": 24,
    "username": "xiaoli",
    "role": "member",
    "job_title": "staff",
    "tenant": "浏览器测试门店",
    "modules": ["restaurant"],
    "all_modules": [],
    "industries": ["restaurant"],
}

META = {
    "app": {"name": "派活", "slogan": "测试"},
    "stations": [],
    "industries": ["通用", "餐饮", "宠物"],
    "brief_templates": ["蹭热点", "日更选题", "产品软文"],
    "platforms": ["小红书", "抖音", "公众号"],
    "platform_specs": {},
    "image_modes": [{"key": "ai", "label": "AI生成"}, {"key": "real", "label": "真实图"}],
    "job_points": 18,
    "modes": {"auto": "全自动"},
    "default_mode": "auto",
}


def _employee(idx, name, group):
    return {"idx": idx, "name": name, "person": "", "emoji": "🧑", "color": "#fff",
            "group": group, "person_status": "active", "identity_status": "current",
            "can_assign_new": True, "identity_ref": "a" * 64,
            "config_revision": 1, "config_sha256": "b" * 64, "bundle_sha256": "c" * 64}


DEPTS = [
    {"key": "restaurant", "name": "餐饮", "emoji": "🍜",
     "employees": [_employee(2001, "店长参谋", "餐饮·经营"), _employee(2002, "后厨顾问", "餐饮·后厨")]},
    {"key": "pet", "name": "宠物", "emoji": "🐶",
     "employees": [_employee(3001, "宠物店顾问", "宠物·门店")]},
]


def _api(balance):
    return {
        "/api/auth/me": OWNER,
        "/api/meta": META,
        "/api/state": {"inbox": [], "notifications": [], "jobs": [], "items": [],
                       "total": 0, "balance": balance, "setup": {}, "trio": {},
                       "profiles": []},
        "/api/employees": [],
        "/api/depts": DEPTS,
        "/api/meetings": {"items": [], "total": 0, "offset": 0, "limit": 20},
        "/api/task-center": {"items": [], "total": 0, "counts": {}},
    }


class FrontendFlowsFeedbackTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def _open(self, playwright, *, balance=100, init_script="", width=1280, hash_="#/", me=None, extra=None):
        browser = await _launch_chromium(playwright, self.executable)
        page = await browser.new_page(viewport={"width": width, "height": 900})
        errors = self._capture_unexpected_browser_errors(page)
        await page.add_init_script(
            "window.EventSource = class { constructor(){} close(){} };" + init_script)
        payloads = _api(balance)
        if me is not None:
            payloads["/api/auth/me"] = me
        payloads.update(extra or {})
        self.api_hits = []

        async def api_route(route):
            path = urlparse(route.request.url).path
            self.api_hits.append(path)
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payloads.get(path, {}), ensure_ascii=False))

        await page.route("**/api/**", api_route)
        await page.goto(f"{self.base}/static/index.html{hash_}")
        await page.wait_for_function("document.querySelector('#nav')?.textContent.includes('退出')")
        return browser, page, errors

    async def test_points_action_shows_cost_under_button_and_recharges_when_short(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, balance=0)
            result = await page.evaluate(
                """() => {
                  window.__ran = 0;
                  const box = document.createElement('div');
                  box.innerHTML = pointsAction({label:'🚀 派活', onclick:'window.__ran=1', cost:12, id:'t-go'});
                  document.querySelector('#main').appendChild(box);
                  const btn = document.querySelector('#t-go');
                  btn.click();
                  return {text: btn.textContent, note: document.querySelector('#t-go-cost').textContent,
                          ran: window.__ran, hash: location.hash};
                }"""
            )
            self.assertEqual("点数不够，去充值", result["text"])
            self.assertEqual("本次 12 点 · 余额 0 点", result["note"])
            self.assertEqual(0, result["ran"])  # 原动作不会执行
            self.assertEqual("#/billing", result["hash"])
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_points_action_keeps_action_text_when_balance_is_enough(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, balance=100)
            result = await page.evaluate(
                """() => {
                  const box = document.createElement('div');
                  box.innerHTML = pointsAction({label:'🚀 派活', onclick:'', cost:0.2, id:'t-go'});
                  document.querySelector('#main').appendChild(box);
                  const btn = document.querySelector('#t-go');
                  const before = {text: btn.textContent, note: document.querySelector('#t-go-cost').textContent};
                  syncPointsAction(btn, 120);
                  return {before, after: btn.textContent, recharge: btn.dataset.recharge || ''};
                }"""
            )
            self.assertEqual("🚀 派活", result["before"]["text"])
            self.assertEqual("本次 0.2 点 · 余额 100 点", result["before"]["note"])
            self.assertEqual("点数不够，去充值", result["after"])
            self.assertEqual("1", result["recharge"])
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_new_brief_has_three_steps_and_remembers_more_settings(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, balance=5, hash_="#/new")
            await page.wait_for_selector("#brief-form")
            info = await page.evaluate(
                """() => ({
                  industry: document.querySelector('#b-ind .chip.on')?.textContent,
                  moreOpen: document.querySelector('#b-more').open,
                  tplInMore: !!document.querySelector('#b-more #b-tpl'),
                  modeInMore: !!document.querySelector('#b-more #b-mode'),
                  shortAboveSubmit: !!(document.querySelector('#b-short')?.compareDocumentPosition(
                    document.querySelector('#b-submit')) & Node.DOCUMENT_POSITION_FOLLOWING),
                  submit: document.querySelector('#b-submit').textContent,
                  note: document.querySelector('#b-submit-cost').textContent,
                })"""
            )
            self.assertEqual("餐饮", info["industry"])  # 按企业自己的行业预选
            self.assertFalse(info["moreOpen"])
            self.assertTrue(info["tplInMore"])
            self.assertTrue(info["modeInMore"])
            self.assertTrue(info["shortAboveSubmit"])
            self.assertEqual("点数不够，去充值", info["submit"])
            self.assertEqual("本次 18 点 · 余额 5 点", info["note"])
            # 记住上次的「更多设置」(按账号)
            await page.evaluate(
                """() => { selectBriefChips('b-tpl','产品软文'); selectBriefChips('b-pf',['抖音'],'data-p');
                           saveBriefPrefs(); }""")
            stored = await page.evaluate("localStorage.getItem('ui_pref_shop-owner_brief')")
            self.assertIn("产品软文", stored)
            await page.evaluate("render()")
            await page.wait_for_selector("#brief-form")
            await page.wait_for_timeout(100)
            restored = await page.evaluate(
                """() => ({tpl: document.querySelector('#b-tpl .chip.on')?.textContent,
                          pf: [...document.querySelectorAll('#b-pf .chip.on')].map(x => x.dataset.p)})""")
            self.assertEqual({"tpl": "产品软文", "pf": ["抖音"]}, restored)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_meetings_expand_own_industry_and_fold_the_rest(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, hash_="#/meetings")
            await page.wait_for_selector("#mt-suggest")
            info = await page.evaluate(
                """() => ({
                  suggestPrimary: document.querySelector('#mt-suggest').classList.contains('pri'),
                  industries: [...document.querySelectorAll('#main details.mt-ind > summary')].map(x => x.textContent.trim()),
                  openIndustries: [...document.querySelectorAll('#main details.mt-ind[open]')].map(x => x.dataset.ind),
                  openGroups: [...document.querySelectorAll('#main details.mt-ind[open] details.mt-group[open] > summary')].map(x => x.textContent.trim()),
                  petHas: document.querySelector('details.mt-ind[data-ind="pet"]')?.textContent.includes('宠物店顾问'),
                  intro: document.querySelector('#main .notice')?.textContent,
                  auto: document.querySelector('#mt-auto')?.closest('label')?.textContent,
                })"""
            )
            self.assertTrue(info["suggestPrimary"])
            # 每个行业一个总折叠并标上行业名;本行业展开,别的行业收起
            self.assertEqual(["🍜 餐饮（2 组 · 2 人）", "🐶 宠物（1 组 · 1 人）"], info["industries"])
            self.assertEqual(["restaurant"], info["openIndustries"])
            self.assertEqual(["餐饮·经营（1）", "餐饮·后厨（1）"], info["openGroups"])
            self.assertTrue(info["petHas"])
            self.assertIn("得出结论（做 / 先补信息）后", info["auto"])
            self.assertNotIn("GO 或 NEED INFO", info["auto"])
            self.assertIn("每位专家各出一个方案，互相挑毛病，最后给您一个做或不做的结论", info["intro"])
            for jargon in ("Top N", "GO / NO-GO", "单位经济反向验证"):
                self.assertNotIn(jargon, info["intro"])
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_meetings_only_open_two_most_used_groups(self):
        busy = [
            {"key": "restaurant", "name": "餐饮产业部", "short_label": "餐饮", "emoji": "🍜",
             "employees": [
                 {**_employee(2101, "选址顾问", "餐饮·选址"), "tasks_n": 0},
                 {**_employee(2102, "后厨顾问", "餐饮·后厨"), "tasks_n": 9},
                 {**_employee(2103, "会员顾问", "餐饮·会员"), "tasks_n": 4},
             ]},
        ]
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, hash_="#/meetings", extra={"/api/depts": busy})
            await page.wait_for_selector("#mt-suggest")
            info = await page.evaluate(
                """() => ({
                  label: document.querySelector('details.mt-ind > summary').textContent.trim(),
                  open: [...document.querySelectorAll('details.mt-group[open] > summary')].map(x => x.textContent.trim()),
                })"""
            )
            self.assertEqual("🍜 餐饮（3 组 · 3 人）", info["label"])  # 用短名,不用「餐饮产业部」
            self.assertEqual(["餐饮·后厨（1）", "餐饮·会员（1）"], info["open"])
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_meetings_start_button_waits_for_two_experts(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, hash_="#/meetings")
            await page.wait_for_selector("#mt-go")
            state = lambda: page.evaluate(
                """() => ({disabled: document.querySelector('#mt-go').disabled,
                           text: document.querySelector('#mt-go').textContent.trim(),
                           note: document.querySelector('#mt-go-cost').textContent.trim()})""")
            self.assertEqual({"disabled": True, "text": "先选 2 位专家", "note": "本次 0 点 · 余额 100 点"},
                             await state())
            await page.locator('.mt-chip[data-i="2001"]').click()
            self.assertEqual({"disabled": True, "text": "再选 1 位专家", "note": "本次 1 点 · 余额 100 点"},
                             await state())
            await page.locator('.mt-chip[data-i="2002"]').click()
            self.assertEqual({"disabled": False, "text": "🎯 开会并收敛", "note": "本次 2 点 · 余额 100 点"},
                             await state())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_cost_button_disabled_label_and_recharge(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, balance=1)
            result = await page.evaluate(
                """() => {
                  const box = document.createElement('div');
                  box.innerHTML = costButton({label:'🔥 扫描', onclick:'', cost:1, id:'c1'})
                    + costButton({label:'🎯 开会', onclick:'', cost:0, id:'c2', disabled:true, disabledLabel:'先选 2 位专家'})
                    + costButton({label:'🎯 开会', onclick:'', cost:3, id:'c3', btnClass:'btn sm pri'});
                  document.querySelector('#main').appendChild(box);
                  const c2 = document.querySelector('#c2');
                  const before = c2.textContent;
                  setCostButtonDisabled(c2, false);
                  return {
                    c1: [document.querySelector('#c1').textContent, document.querySelector('#c1-cost').textContent],
                    c2: [before, c2.textContent, c2.disabled],
                    c3: [document.querySelector('#c3').textContent, document.querySelector('#c3').className,
                         document.querySelector('#c3').dataset.recharge || ''],
                  };
                }"""
            )
            self.assertEqual(["🔥 扫描", "本次 1 点 · 余额 1 点"], result["c1"])
            self.assertEqual(["先选 2 位专家", "🎯 开会", False], result["c2"])
            self.assertEqual(["点数不够，去充值", "btn sm pri", "1"], result["c3"])
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_tools_buttons_keep_points_out_of_label(self):
        tools_meta = {"industries": ["餐饮"], "hot_channels": ["抖音"], "hot_channels_saved": ["抖音"],
                      "hot_daily": {}, "festivals": []}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, balance=0, hash_="#/tools/hot",
                extra={"/api/tools/meta": tools_meta, "/api/tools/jobs": {"items": [], "total": 0}})
            await page.wait_for_selector("#hot-ch")
            info = await page.evaluate(
                """() => { const b = document.querySelector('[data-label="🔥 扫描今日热点"]');
                  return {text: b.textContent, note: b.parentElement.querySelector('.cost-note').textContent}; }""")
            self.assertEqual("点数不够，去充值", info["text"])
            self.assertEqual("本次 1 点 · 余额 0 点", info["note"])
            self.assertNotIn("(1点)", await page.locator("#main").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_pay_bar_is_removed_when_page_changes(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright)
            await page.evaluate("pay402('点数不足')")
            self.assertEqual(1, await page.locator("#paybar").count())
            await page.evaluate("location.hash = '#/meetings'")
            await page.wait_for_function("!document.querySelector('#paybar')")
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_experts_open_own_industry_first_with_finder_on_top(self):
        def emp(idx, name, group):
            return {**_employee(idx, name, group), "role": name, "resume_card": {}, "enabled": True}
        depts = [
            {"key": "pet", "name": "宠物行业", "emoji": "🐶", "tagline": "宠物", "groups": [{"name": "宠物·门店"}],
             "employees": [emp(3001, "宠物店顾问", "宠物·门店")]},
            {"key": "restaurant", "name": "餐饮产业部", "emoji": "🍜", "tagline": "餐饮",
             "groups": [{"name": "餐饮·经营"}], "employees": [emp(2001, "店长参谋", "餐饮·经营")]},
        ]
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, hash_="#/experts", extra={"/api/depts": depts})
            await page.wait_for_selector(".deptsec")
            info = await page.evaluate(
                """() => ({
                  floors: [...document.querySelectorAll('.deptsec[data-deptsec]')].map(x => [x.dataset.deptsec, !!x.querySelector('.deptbody')]),
                  finderDept: document.querySelector('.exp-finder-top')?.dataset.finderDept,
                  finderFirst: !!(document.querySelector('.exp-finder-top')?.compareDocumentPosition(
                    document.querySelector('.deptsec')) & Node.DOCUMENT_POSITION_FOLLOWING),
                  finders: document.querySelectorAll('#ef-text-restaurant').length,
                  contentOpen: deptIsOpen('content'),
                })"""
            )
            self.assertEqual([["restaurant", True], ["pet", False], ["content", False]], info["floors"])
            self.assertEqual("restaurant", info["finderDept"])
            self.assertTrue(info["finderFirst"])
            self.assertEqual(1, info["finders"])
            self.assertFalse(info["contentOpen"])
            # 用户自己展开/收起过的,以用户的选择为准
            await page.evaluate("toggleDept('content')")
            await page.wait_for_function("!!document.querySelector('.deptsec[data-deptsec=\"content\"] .deptbody')")
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_hub_shows_recently_used_pages_per_account(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright)
            await page.evaluate("hubRecordVisit('tasks'); hubRecordVisit('meetings'); hubRecordVisit('tasks');")
            stored = await page.evaluate("localStorage.getItem('ui_pref_shop-owner_hub_recent_dispatch')")
            self.assertEqual(["tasks", "meetings"], json.loads(stored))
            await page.evaluate("location.hash = '#/dispatch'")
            await page.wait_for_selector(".hub-recent")
            links = await page.evaluate(
                "[...document.querySelectorAll('.hub-recent a')].map(a => a.getAttribute('href'))")
            self.assertEqual(["#/tasks", "#/meetings"], links)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_filters_are_restored_per_account(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright,
                init_script="""
                  localStorage.setItem('ui_pref_shop-owner_tc_status', JSON.stringify('done'));
                  localStorage.setItem('ui_pref_shop-owner_tc_kind', JSON.stringify('job'));
                  localStorage.setItem('ui_pref_shop-owner_boss_days', JSON.stringify(90));
                  localStorage.setItem('ui_pref_shop-owner_inspection_industry', JSON.stringify('restaurant'));
                  localStorage.setItem('ui_pref_someone-else_tc_status', JSON.stringify('failed'));
                """,
            )
            result = await page.evaluate(
                "({status: TC_STATUS, kind: TC_KIND, days: BOSS_DASH_FILTER.days, industry: INSPECTION_INDUSTRY})")
            self.assertEqual(
                {"status": "done", "kind": "job", "days": 90, "industry": "restaurant"}, result)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_ui_prefs_survive_storage_that_throws(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright,
                init_script="""
                  Object.defineProperty(window, 'localStorage', {
                    configurable: true,
                    get() { throw new DOMException('storage disabled', 'SecurityError'); }
                  });
                """,
            )
            result = await page.evaluate(
                """() => { hubRecordVisit('tasks'); saveBriefPrefs();
                  return {wrote: uiPrefSet('tc_status','done'), read: uiPrefGet('tc_status','open'),
                          row: hubRecentRow(NAV_GROUPS[1], navVisibleCards(NAV_GROUPS[1]))}; }""")
            self.assertEqual({"wrote": False, "read": "open", "row": ""}, result)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_desktop_account_area_sits_next_to_online_status(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright)
            info = await page.evaluate(
                """() => { const out = document.querySelector('#nav .navlogout').getBoundingClientRect();
                  const conn = document.querySelector('#conn').getBoundingClientRect();
                  return {who: document.querySelector('#nav .nav-who')?.textContent,
                          inAcct: !!document.querySelector('#nav .nav-acct .navlogout'),
                          gap: conn.left - out.right}; }""")
            self.assertEqual("浏览器测试门店 · shop-owner · 余额 100 点", info["who"])
            self.assertTrue(info["inAcct"])
            self.assertGreaterEqual(info["gap"], 0)
            self.assertLess(info["gap"], 60)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_member_sees_no_company_balance_and_no_billing_page(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, me=MEMBER, hash_="#/mine")
            await page.wait_for_function("document.querySelector('#main .hubgrid')")
            mine = await page.evaluate(
                """() => ({who: document.querySelector('#nav .nav-who')?.textContent,
                  card: document.querySelector('#main .mine-who')?.textContent,
                  cards: [...document.querySelectorAll('#main .hubcard .hc-t')].map(x=>x.textContent)})""")
            self.assertEqual("浏览器测试门店 · xiaoli", mine["who"])
            self.assertNotIn("余额", mine["card"])
            self.assertNotIn("套餐", mine["cards"])
            await page.evaluate("location.hash='#/'")
            await page.wait_for_function("document.querySelector('.today-hello')")
            self.assertEqual(0, await page.locator(".today-money").count())
            await page.evaluate("location.hash='#/billing'")
            await page.wait_for_function("document.querySelector('#billing-owner-only')")
            text = await page.locator("#main").inner_text()
            self.assertIn("套餐和点数由老板管理。", text)
            self.assertNotIn("/api/billing", self.api_hits)
            self.assertNotIn("/api/purchases", self.api_hits)
            # 「点数不够」的按钮逻辑对成员照常可用
            note = await page.evaluate("costNoteText(12)")
            self.assertEqual("本次 12 点 · 余额 100 点", note)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_owner_still_sees_balance_and_billing_card(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, hash_="#/mine")
            await page.wait_for_function("document.querySelector('#main .hubgrid')")
            mine = await page.evaluate(
                """() => ({card: document.querySelector('#main .mine-who')?.textContent,
                  cards: [...document.querySelectorAll('#main .hubcard .hc-t')].map(x=>x.textContent)})""")
            self.assertIn("余额 100 点", mine["card"])
            self.assertIn("套餐", mine["cards"])
            await page.evaluate("location.hash='#/'")
            await page.wait_for_function("document.querySelector('.today-money')")
            self.assertIn("余额 100 点", await page.locator(".today-money").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_director_sees_balance_and_billing_card(self):
        director = {**MEMBER, "id": 25, "username": "zongjian", "job_title": "director"}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, me=director, hash_="#/mine")
            await page.wait_for_function("document.querySelector('#main .hubgrid')")
            cards = await page.evaluate(
                "[...document.querySelectorAll('#main .hubcard .hc-t')].map(x=>x.textContent)")
            self.assertIn("套餐", cards)
            await page.evaluate("location.hash='#/'")
            await page.wait_for_function("document.querySelector('.today-money')")
            self.assertIn("余额 100 点", await page.locator(".today-money").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def _forced_change(self, playwright, me):
        browser = await _launch_chromium(playwright, self.executable)
        page = await browser.new_page(viewport={"width": 390, "height": 800})
        errors = self._capture_unexpected_browser_errors(page)
        await page.add_init_script("window.EventSource = class { constructor(){} close(){} };")
        payloads = _api(100)
        current = dict(me, must_change_password=True, password_change_needs_old=False)

        async def api_route(route):
            path = urlparse(route.request.url).path
            if path == "/api/auth/me":
                payload = current
            elif path == "/api/auth/password":
                current["must_change_password"] = False
                payload = {"ok": True}
            else:
                payload = payloads.get(path, {})
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        async def staff_route(route):
            await route.fulfill(status=200, content_type="text/html",
                                body="<!doctype html><div id='staff-home'>店员手机版</div>")

        await page.route("**/api/**", api_route)
        await page.route("**/staff", staff_route)
        await page.goto(f"{self.base}/static/index.html?full=1")
        await page.wait_for_function(
            "document.querySelector('#main')?.textContent.includes('请先设置您自己的密码')")
        await page.locator("#forced-pw-new").fill("FreshPassword456!")
        await page.locator("#forced-pw-confirm").fill("FreshPassword456!")
        await page.locator("#forced-pw-confirm").press("Enter")
        return browser, page, errors

    async def test_forced_password_change_sends_store_staff_back_to_phone_app(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._forced_change(
                playwright, dict(MEMBER, prefers_staff_home=True))
            await page.wait_for_selector("#staff-home", timeout=5000)
            self.assertEqual("/staff", urlparse(page.url).path)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_forced_password_change_keeps_owner_in_workspace(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._forced_change(
                playwright, dict(OWNER, prefers_staff_home=False))
            await page.wait_for_function("document.querySelector('.today-hello')", timeout=5000)
            self.assertEqual("/static/index.html", urlparse(page.url).path)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_enter_submits_single_action_forms(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright)
            await page.evaluate(
                """() => { window.__hit = 0;
                  document.body.insertAdjacentHTML('beforeend',
                    `<input id="enter-probe" ${enterSubmit('window.__hit++')}>`); }""")
            await page.locator("#enter-probe").press("Enter")
            self.assertEqual(1, await page.evaluate("window.__hit"))
            self._assert_no_browser_errors(errors)
            await browser.close()

    def test_team_and_tenant_forms_submit_on_enter(self):
        for call, count in (('enterSubmit("tmMyPw()")', 4), ('enterSubmit("tmUserCreate()")', 2),
                            ('enterSubmit("tmTenantCreate()")', 3)):
            self.assertEqual(count, APP_JS.count(call), call)


if __name__ == "__main__":
    unittest.main()
