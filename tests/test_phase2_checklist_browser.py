"""第 2 期老板端清单/排行页面：真实浏览器渲染（手机宽度、无报错、模板保存请求）。"""
from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

# 只导入模块,不把对方的 TestCase 类放进本模块命名空间(否则会被重复发现执行)。
from tests import test_frontend_browser_behavior as _browser
from tests import test_frontend_nav_mobile as _nav

_launch_chromium = _browser._launch_chromium


def _item(key, text, done, photo=False, url=""):
    return {"key": key, "text": text, "require_photo": photo, "done": done,
            "done_at": 1790000000 if done else None, "done_by": 23 if done else None,
            "done_by_name": "小王" if done else "", "note": "",
            "photo_url": url, "photo_taken_at": None}


RUN_A = {"id": 1, "branch_id": 1, "branch_name": "朝阳店", "template_id": 1,
         "name": "开店清单", "kind": "open", "kind_label": "开店清单",
         "run_date": "2026-09-21", "status": "done", "status_label": "已完成",
         "due_at": 1790042400, "due_text": "10:00 前", "overdue": False,
         "assignee_user_id": 22, "assignee_name": "店长老李", "assigned_to_me": False,
         "completed_at": 1790040000, "done_count": 2, "total": 2,
         "items": [_item("o01", "制冰机出冰正常，拍一张冰桶", True, True,
                         "/files/staff/2/1/" + "a" * 32 + ".jpg"),
                   _item("o07", "收银机开机、零钱备足", True)]}
RUN_B = {**RUN_A, "id": 2, "branch_id": 2, "branch_name": "静安店", "status": "missed",
         "status_label": "超时没做完", "assignee_user_id": None, "assignee_name": "",
         "completed_at": None, "done_count": 0,
         "items": [_item("o01", "制冰机出冰正常，拍一张冰桶", False, True),
                   _item("o07", "收银机开机、零钱备足", False)]}
OVERVIEW = {"date": "2026-09-21", "summary": {"total": 2, "done": 1, "missed": 1,
                                             "open": 0, "rate": 50.0},
            "stores": [
                {"branch_id": 1, "branch_name": "朝阳店", "region": "", "total": 1,
                 "done": 1, "missed": 0, "open": 0, "runs": [RUN_A]},
                {"branch_id": 2, "branch_name": "静安店", "region": "", "total": 1,
                 "done": 0, "missed": 1, "open": 0, "runs": [RUN_B]}]}
TEMPLATES = {"items": [{"id": 7, "industry_key": "tea_coffee", "kind": "open",
                        "kind_label": "开店清单", "name": "开店清单", "due_time": "10:00",
                        "active": True, "updated_at": 0,
                        "items": [{"key": "o01", "text": "制冰机出冰正常，拍一张冰桶",
                                   "require_photo": True},
                                  {"key": "o07", "text": "收银机开机、零钱备足",
                                   "require_photo": False}]}]}


def _part(label, weight, score, done=None, total=None):
    return {"label": label, "weight": weight, "score": score, "done": done, "total": total}


RANK = {"period": "week", "period_label": "近 7 天", "formula": "综合分 = 清单按时完成率×40% …",
        "weights": {}, "stores": [
            {"branch_id": 1, "branch_name": "朝阳店", "region": "", "score": 98.0,
             "prev_score": 90.0, "delta": 8.0, "rank": 1, "prev_rank": 2, "rank_change": 1,
             "needs_attention": False, "reasons": [],
             "components": {"checklist": _part("清单按时完成率", 40, 100.0, 4, 4),
                            "action": _part("整改按时关闭率", 30, 100.0, 1, 1),
                            "task": _part("派活按时完成率", 20, None, 0, 0),
                            "issue": {"label": "巡店问题分", "weight": 10, "score": 80.0,
                                      "issues": 4, "visits": 1}}},
            {"branch_id": 2, "branch_name": "静安店", "region": "", "score": 22.5,
             "prev_score": 100.0, "delta": -77.5, "rank": 2, "prev_rank": 1,
             "rank_change": -1, "needs_attention": True,
             "reasons": ["近 7 天 3 次闭店清单没按时做完", "1 条巡店整改已超期还没关"],
             "components": {"checklist": _part("清单按时完成率", 40, 25.0, 1, 4)}}]}


class ChecklistBrowserTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_browser.FrontendBrowserBehaviorTests.setUpClass.__func__)
    tearDownClass = classmethod(_browser.FrontendBrowserBehaviorTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _browser.FrontendBrowserBehaviorTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _browser.FrontendBrowserBehaviorTests._assert_no_browser_errors

    async def _open(self, playwright, hash_, *, me=None):
        api = {
            "/api/auth/me": me or {**_nav.OWNER, "job_title": ""},
            "/api/meta": {"app": {"name": "派活", "slogan": "测试"}, "stations": []},
            "/api/state": _nav.STATE,
            "/api/employees": [],
            "/api/depts": [],
            "/api/checklist/runs": OVERVIEW,
            "/api/checklist/templates": TEMPLATES,
            "/api/stores/ranking": RANK,
        }
        self.calls = []
        browser = await _launch_chromium(playwright, self.executable)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        errors = self._capture_unexpected_browser_errors(page)
        await page.add_init_script(
            "window.EventSource = class { constructor(u){this.url=u;} close(){} };")

        async def api_route(route):
            parsed = urlparse(route.request.url)
            body = route.request.post_data
            self.calls.append((route.request.method, parsed.path, parse_qs(parsed.query),
                               json.loads(body) if body else None))
            payload = api.get(parsed.path, {})
            if route.request.method == "PUT":
                payload = TEMPLATES["items"][0]
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        await page.route("**/api/**", api_route)
        await page.goto(f"{self.base}/static/index.html{hash_}")
        await page.wait_for_function("document.querySelectorAll('#tabbar a').length>0")
        return browser, page, errors

    async def test_store_hub_lists_checklist_and_rank_cards(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/store")
            await page.wait_for_function("document.querySelector('#main .hubgrid')")
            cards = await page.evaluate(
                "[...document.querySelectorAll('#main .hubcard .hc-t')].map(x=>x.textContent)")
            self.assertIn("开闭店清单", cards)
            self.assertIn("门店排行", cards)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_today_overview_puts_missed_store_first_and_expands(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/checklists")
            await page.wait_for_function("document.querySelectorAll('.ck-store').length===2")
            names = await page.evaluate(
                "[...document.querySelectorAll('.ck-store-top b')].map(x=>x.textContent)")
            self.assertEqual(["🏪 静安店", "🏪 朝阳店"], names)
            self.assertIn("50%", await page.inner_text(".ck-sum"))
            await page.click("text=🏪 朝阳店")
            await page.wait_for_function("document.querySelector('.ck-items')")
            link = await page.get_attribute(".ck-items a", "href")
            self.assertEqual("/files/staff/2/1/" + "a" * 32 + ".jpg", link)
            self.assertIn("小王", await page.inner_text(".ck-items"))
            width = await page.evaluate("document.documentElement.scrollWidth")
            self.assertLessEqual(width, 390)
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_owner_edits_template_and_saves_full_item_list(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/checklists")
            await page.click("text=清单模板")
            await page.wait_for_function("document.querySelector('.ck-tpl')")
            await page.click("text=✏️ 改清单")
            await page.click("text=＋ 加一项")
            await page.fill(".ck-edit-row:last-child input[type=text]", "门口地垫摆正")
            await page.fill("input[type=time]", "09:30")
            await page.dispatch_event("input[type=time]", "change")
            await page.click("text=保存")
            await page.wait_for_function("!document.querySelector('.ck-edit-row')")
            put = [c for c in self.calls if c[0] == "PUT"]
            self.assertEqual("/api/checklist/templates/7", put[0][1])
            body = put[0][3]
            self.assertEqual("09:30", body["due_time"])
            self.assertEqual(["o01", "o07", None], [i.get("key") for i in body["items"]])
            self.assertEqual("门口地垫摆正", body["items"][2]["text"])
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_staff_member_does_not_see_template_tab(self):
        staff = {**_nav.MEMBER_NO_CONTENT, "job_title": "manager"}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/checklists", me=staff)
            await page.wait_for_function("document.querySelectorAll('.ck-store').length===2")
            self.assertEqual(0, await page.locator("text=清单模板").count())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_store_rank_cards_show_score_reasons_and_switch_period(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(playwright, "#/store-rank")
            await page.wait_for_function("document.querySelectorAll('.ck-rank').length===2")
            text = await page.inner_text("#ck-rank")
            self.assertIn("近 7 天 3 次闭店清单没按时做完", text)
            self.assertIn("要盯一下", text)
            self.assertIn("↓77.5", text)
            self.assertEqual(1, await page.locator(".ck-rank.warn").count())
            await page.click("text=近 30 天")
            await page.wait_for_function("document.querySelectorAll('.ck-rank').length===2")
            periods = [c[2].get("period") for c in self.calls if c[1] == "/api/stores/ranking"]
            self.assertEqual([["week"], ["month"]], periods)
            width = await page.evaluate("document.documentElement.scrollWidth")
            self.assertLessEqual(width, 390)
            self._assert_no_browser_errors(errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
