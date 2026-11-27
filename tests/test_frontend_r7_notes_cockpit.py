"""第七轮收尾（真实浏览器渲染）：今天页「新进展」同类通知合并；老板看板剩余内容并进数据驾驶舱。

驾驶舱「有巡店、有 AI 交付」的样子线上测试库里没有，这里用假数据渲染来验证。
设置环境变量 R7_SHOT_DIR 时顺手把页面截图存进去，方便人工看效果。
"""
from __future__ import annotations

import copy
import json
import os
import time
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from tests import test_frontend_nav_mobile as _nav
from tests import test_frontend_r7_ia as _ia

NOW = time.time()
TODAY = time.strftime("%Y-%m-%d", time.gmtime(NOW + 8 * 3600))
DAY2 = time.strftime("%Y-%m-%d", time.gmtime(NOW + 8 * 3600 - 2 * 86400))
SHOT_DIR = os.environ.get("R7_SHOT_DIR", "")


def _esc(i, branch, remaining, assignee=""):
    """后端提醒循环现在写的清单超时升级通知（带 meta）。"""
    return {"id": 100 + i, "kind": "staff_escalate", "title": "闭店清单超时一天多，请过问",
            "body": f"{branch} · 还差 {remaining} 项 · 超时 43 小时。店长已经提醒过，请您过问。",
            "link": "#/checklists", "created_at": NOW - 600,
            "meta": {"obj": "checklist", "stage": "esc2", "name": "闭店清单", "date": DAY2,
                     "branch_id": i, "branch": branch, "remaining": remaining,
                     "due_at": NOW - 43 * 3600 - 120, "assignee": assignee}}


def _notifications():
    return [
        _esc(1, "天府三街店", 8), _esc(2, "建设路店", 8), _esc(3, "春熙路店", 7, "王店长"),
        # 不同类的夹在中间：不影响上面那一组，自己单条显示
        {"id": 201, "kind": "task_outcome", "title": "选址分析做完了", "body": "店长助理交付了一份方案",
         "link": "#/tasks/9", "created_at": NOW - 500},
        _esc(4, "高新店", 5), _esc(5, "金牛万达店", 3),
        # 另一类清单：只有一家，不成组
        {**_esc(6, "天府三街店", 6), "id": 301, "title": "交班清单超时一天多，请过问",
         "meta": {**_esc(6, "天府三街店", 6)["meta"], "name": "交班清单"}},
        # 历史通知：没有 meta，按原样单条显示
        {"id": 401, "kind": "staff_escalate", "link": "#/checklists", "created_at": NOW - 9000,
         "title": "超时一天多，请过问：王记火锅·天府三街店·2026-11-20 闭店清单（还差 8 项）",
         "body": "王记火锅·天府三街店·2026-11-20 闭店清单（还差 8 项）已超时 43 小时（负责人：还没指派），店长已经提醒过，请老板过问。"},
    ]


def _trend():
    rows = []
    for i in range(7):
        day = time.strftime("%Y-%m-%d", time.gmtime(NOW + 8 * 3600 - (6 - i) * 86400))
        rows.append({"day": day, "ai": 0, "staff": 0, "checklist": 0, "inspection": 0})
    rows[2].update(ai=2, staff=1)
    rows[5].update(checklist=3, inspection=1)
    return rows


COCKPIT_FULL = {
    **_ia.COCKPIT,
    "trend": _trend(),
    "inspection": {"has_records": True, "visits": 4, "avg_score": 86.5, "open_issues": 7,
                   "critical_issues": 2, "open_actions": 5, "overdue_actions": 1},
    "recent_deliveries": [
        {"id": 31, "name": "店长助理", "status": "done", "status_label": "已交付",
         "at": NOW - 1800, "route": "#/tasks/31"},
        {"id": 30, "name": "选品顾问", "status": "failed", "status_label": "没做成",
         "at": NOW - 7200, "route": "#/tasks/30"},
        {"id": 29, "name": "店长助理", "status": "waiting", "status_label": "等您确认",
         "at": NOW - 90000, "route": "#/tasks/29"},
    ],
    "stores": [
        {"branch_id": 1, "name": "天府三街店", "region": "", "score": 33, "delta": None, "rank": 1,
         "needs_attention": True, "reasons": ["近 7 天 2 次闭店清单没按时做完"],
         "checklist": 0, "action": None, "task": 100, "issue": None, "open_issues": 0},
        {"branch_id": 2, "name": "建设路店", "region": "", "score": None, "delta": None, "rank": None,
         "needs_attention": False, "reasons": [],
         "checklist": None, "action": None, "task": None, "issue": None, "open_issues": 0},
    ],
    "notes": "全部数据来自派活系统内的真实记录。",
}
META = {"can_dispatch": True,
        "branches": [{"id": 1, "name": "天府三街店", "members": [{"id": 5, "name": "小李", "title_label": "店员"}]}]}


class NotesAndCockpitTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(_nav.FrontendNavMobileTests.setUpClass.__func__)
    tearDownClass = classmethod(_nav.FrontendNavMobileTests.tearDownClass.__func__)
    _capture_unexpected_browser_errors = _nav.FrontendNavMobileTests._capture_unexpected_browser_errors
    _assert_no_browser_errors = _nav.FrontendNavMobileTests._assert_no_browser_errors
    _open = _nav.FrontendNavMobileTests._open

    async def _shot(self, page, name):
        if SHOT_DIR:
            os.makedirs(SHOT_DIR, exist_ok=True)
            await page.screenshot(path=os.path.join(SHOT_DIR, name + ".jpg"), type="jpeg",
                                  quality=60, full_page=True)

    async def _today(self, playwright, width=390):
        state = copy.deepcopy(_nav.STATE)
        state.update({"inbox": [], "notifications": _notifications(), "today": TODAY})
        reads = []
        browser, page, errors = await self._open(
            playwright, me=_nav.OWNER, width=width, height=844, hash_="#/",
            api_overrides={"/api/state": state})

        async def read_route(route):
            ids = (route.request.post_data_json or {}).get("ids")
            reads.append(ids)
            gone = set(ids or [n["id"] for n in state["notifications"]])
            state["notifications"][:] = [n for n in state["notifications"] if n["id"] not in gone]
            await route.fulfill(status=200, content_type="application/json", body='{"ok":true}')

        await page.route("**/api/notifications/read", read_route)
        await page.wait_for_selector(".note-group")
        return browser, page, errors, reads

    async def test_same_kind_same_day_notifications_fold_into_one_group_on_phone(self):
        async with async_playwright() as playwright:
            browser, page, errors, reads = await self._today(playwright)
            await self._shot(page, "today-390-groups")
            todo = await page.locator(".today-todo").inner_text()
            self.assertIn("新进展（8）", todo)                    # 计数还是按条数，合并只是展示
            groups = page.locator(".note-group")
            self.assertEqual(1, await groups.count())
            group = groups.first
            self.assertEqual("5 家店的闭店清单超时没做完前天",
                             (await group.locator(".ng-title").inner_text()).replace(" ", "").replace("\n", "")
                             .replace("5家店", "5 家店"))
            lines = await group.locator(".ng-line:visible").all_inner_texts()
            self.assertEqual(["天府三街店 · 还差 8 项 · 超时 43 小时 · 还没指派负责人",
                              "建设路店 · 还差 8 项 · 超时 43 小时 · 还没指派负责人",
                              "春熙路店 · 还差 7 项 · 超时 43 小时 · 负责人 王店长"], lines)
            # 手机 390 宽：标题 1 行 + 3 家店各 1 行 + 按钮 1 行，一共不超过 5 行
            box = await group.evaluate(
                """el => ({h: el.getBoundingClientRect().height,
                           wraps: [...el.querySelectorAll('.ng-title,.ng-line')].filter(x => x.offsetParent)
                             .filter(x => x.getBoundingClientRect().height > 26).length,
                           cut: [...el.querySelectorAll('.ng-line')].filter(x => x.offsetParent)
                             .filter(x => x.scrollWidth > x.clientWidth + 1).length})""")
            self.assertEqual(0, box["wraps"])
            self.assertEqual(0, box["cut"])                       # 也没有被省略号截掉
            self.assertLess(box["h"], 170)
            self.assertFalse(await page.evaluate(
                "document.documentElement.scrollWidth>document.documentElement.clientWidth"))
            # 超过 3 条的折叠成「还有 N 条」，点开能看全、能收起
            toggle = group.locator(".ng-toggle")
            self.assertEqual("还有 2 条", await toggle.inner_text())
            await toggle.click()
            self.assertEqual(5, await group.locator(".ng-line:visible").count())
            self.assertEqual("收起", await toggle.inner_text())
            await toggle.click()
            self.assertEqual(3, await group.locator(".ng-line:visible").count())
            # 不同类的、只有一条的、没有结构化信息的老通知：保持单条
            singles = await page.locator(".note-single .todo-tx b").all_inner_texts()
            self.assertEqual(["选址分析做完了", "交班清单超时一天多，请过问",
                              "超时一天多，请过问：王记火锅·天府三街店·2026-11-20 闭店清单（还差 8 项）"], singles)
            self.assertEqual(["这组已读", "去看看"], await group.locator(".ng-foot .btn").all_inner_texts())

            # 整组已读 = 组里 5 条一起标已读，别的通知不动
            await group.get_by_role("button", name="这组已读").click()
            await page.wait_for_function("document.querySelectorAll('.note-group').length===0")
            self.assertEqual([[101, 102, 103, 104, 105]], reads)
            self.assertIn("新进展（3）", await page.locator(".today-todo").inner_text())
            self.assertEqual(3, await page.locator(".note-single").count())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_group_go_button_marks_group_read_and_opens_the_checklist_page(self):
        async with async_playwright() as playwright:
            browser, page, errors, reads = await self._today(playwright, width=1280)
            await self._shot(page, "today-1280-groups")
            await page.locator(".note-group").get_by_role("button", name="去看看").click()
            await page.wait_for_function("location.hash==='#/checklists'")
            self.assertEqual([[101, 102, 103, 104, 105]], reads)
            # 清单页的接口这里没有造数据，不检查它自己的渲染报错
            await browser.close()

    async def test_cockpit_shows_inspection_board_recent_deliveries_and_daily_detail(self):
        overrides = {"/api/cockpit/summary": COCKPIT_FULL, "/api/staff/meta": META}
        for width in (1280, 390):
            async with async_playwright() as playwright:
                browser, page, errors = await self._open(
                    playwright, me=_ia.OWNER_WITH_EXPERTS, width=width, height=900,
                    hash_="#/cockpit", api_overrides=overrides)
                await page.wait_for_selector("[data-cp-inspection]")
                # 巡店与整改六格
                cells = await page.evaluate(
                    "[...document.querySelectorAll('[data-cp-inspection] .cp-six>div')].map(d=>"
                    "[d.querySelector('span').textContent,d.querySelector('b').textContent,"
                    "d.querySelector('b').classList.contains('hot')])")
                self.assertEqual([["本期巡店", "4", False], ["本期平均分", "86.5", False],
                                  ["还没解决的问题", "7", False], ["还没解决的高风险问题", "2", True],
                                  ["正在整改", "5", False], ["整改已逾期", "1", True]], cells)
                self.assertEqual(0, await page.locator("[data-cp-inspection-empty]").count())
                # 最近交付：专家、状态、时间
                recent = await page.evaluate(
                    "[...document.querySelectorAll('[data-cp-recent] .cp-recent')].map(r=>"
                    "[r.querySelector('.cp-tag').textContent,r.querySelector('b').textContent,"
                    "r.querySelector('a').getAttribute('href')])")
                self.assertEqual([["已交付", "店长助理", "#/tasks/31"], ["没做成", "选品顾问", "#/tasks/30"],
                                  ["等您确认", "店长助理", "#/tasks/29"]], recent)
                self.assertIn("分钟前", await page.locator("[data-cp-recent] .cp-recent .w").first.inner_text())
                # 每天明细：默认收起；展开后只列有记录的日子，全 0 的合成一句
                self.assertEqual(0, await page.locator("[data-cp-day-table]").count())
                await page.locator("[data-cp-day-all]").click()
                rows = await page.evaluate(
                    "[...document.querySelectorAll('[data-cp-day-table] tbody tr')].map(tr=>"
                    "[...tr.children].slice(1).map(td=>td.textContent))")
                self.assertEqual([["0", "0", "3", "1"], ["2", "1", "0", "0"]], rows)
                self.assertEqual(["日期", "AI 交付", "店员交差", "清单完成", "巡店次数"],
                                 await page.locator("[data-cp-day-table] th").all_inner_texts())
                self.assertEqual("其余 5 天没有记录", await page.locator("[data-cp-day-idle]").inner_text())
                self.assertEqual("收起每天明细", await page.locator("[data-cp-day-all]").inner_text())
                # 经营指标：只留一行说明，不搬「待接入」卡
                self.assertEqual("营业额、客单价这类经营指标需要接上收银数据后才有。",
                                 await page.locator("[data-cp-biz-note]").inner_text())
                self.assertNotIn("待接入", await page.locator("#main").inner_text())
                # 门店表现：没数据的维度合成一句，不再一行行写「—」
                stores = page.locator(".cp-store")
                self.assertEqual("整改、巡店还没有记录",
                                 await stores.nth(0).locator("[data-cp-comp-none]").inner_text())
                self.assertEqual(2, await stores.nth(0).locator(".cp-comp").count())
                self.assertEqual("清单、整改、派活、巡店还没有记录",
                                 await stores.nth(1).locator("[data-cp-comp-none]").inner_text())
                self.assertEqual(0, await stores.nth(1).locator(".cp-comp").count())
                # 按钮上的 emoji 换成了线性图标
                acts = await page.locator(".cp-acts").inner_text()
                for mark in ("⬇", "🖨", "📤", "↻"):
                    self.assertNotIn(mark, acts)
                for name in ("导出 Excel", "打印 / 存 PDF", "下发任务", "刷新"):
                    btn = page.locator(".cp-acts .cp-btn", has_text=name)
                    self.assertEqual(1, await btn.locator("svg.ic").count(), name)
                self.assertFalse(await page.evaluate(
                    "document.documentElement.scrollWidth>document.documentElement.clientWidth"))
                await self._shot(page, f"cockpit-{width}-full")
                self._assert_no_browser_errors(errors)
                await browser.close()

    async def test_cockpit_collapses_inspection_card_when_no_visit_ever_and_hides_empty_recent(self):
        empty = {**COCKPIT_FULL, "recent_deliveries": [],
                 "inspection": {"has_records": False, "visits": 0, "avg_score": None, "open_issues": 0,
                                "critical_issues": 0, "open_actions": 0, "overdue_actions": 0}}
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=_ia.OWNER_WITH_EXPERTS, width=1280, height=900, hash_="#/cockpit",
                api_overrides={"/api/cockpit/summary": empty, "/api/staff/meta": META})
            await page.wait_for_selector("[data-cp-inspection]")
            card = page.locator("[data-cp-inspection]")
            self.assertIn("还没有巡店记录", await card.locator("[data-cp-inspection-empty]").inner_text())
            self.assertEqual(0, await card.locator(".cp-six").count())
            link = card.locator("h3 a")
            self.assertEqual(["去巡店", "#/inspections"],
                             [await link.inner_text(), await link.get_attribute("href")])
            self.assertEqual(0, await page.locator("[data-cp-recent]").count())
            self.assertNotIn("最近交付", await page.locator("#main").inner_text())
            self._assert_no_browser_errors(errors)
            await browser.close()

    async def test_cockpit_dispatch_dialog_closes_when_the_route_changes(self):
        async with async_playwright() as playwright:
            browser, page, errors = await self._open(
                playwright, me=_ia.OWNER_WITH_EXPERTS, width=1280, height=900, hash_="#/cockpit",
                api_overrides={"/api/cockpit/summary": COCKPIT_FULL, "/api/staff/meta": META})
            await page.wait_for_selector("[data-cp-inspection]")
            await page.locator(".cp-acts .cp-btn", has_text="下发任务").click()
            await page.wait_for_selector("#cp-modal .cp-dialog")
            self.assertEqual("下发任务", (await page.locator("#cp-modal h3").inner_text()).strip())
            await page.evaluate("location.hash='#/store'")
            await page.wait_for_function("!document.getElementById('cp-modal')")
            self._assert_no_browser_errors(errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
