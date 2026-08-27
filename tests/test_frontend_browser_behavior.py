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


async def _launch_chromium(playwright, executable: str):
    options = {"headless": True}
    if executable:
        options["executable_path"] = executable
    return await playwright.chromium.launch(**options)


class FrontendBrowserBehaviorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.executable = _browser_executable()
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
            browser = await _launch_chromium(playwright, self.executable)
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
            browser = await _launch_chromium(playwright, self.executable)
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
            browser = await _launch_chromium(playwright, self.executable)
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
            browser = await _launch_chromium(playwright, self.executable)
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

    async def test_v2_decision_evidence_forms_count_submit_and_escape_safely(self):
        payloads = {
            "/api/auth/me": {
                "id": 53,
                "username": "decision-evidence-browser-test",
                "role": "owner",
                "tenant": "决策证据测试企业",
                "modules": ["content", "retail", "restaurant"],
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
        payloads["/api/tasks/77"] = {
            "id": 77,
            "status": "done",
            "revision_no": 1,
            "person_status": "active",
            "identity_status": "current",
            "identity_ref": "7" * 64,
            "config_revision": 1,
            "config_sha256": "8" * 64,
            "bundle_sha256": "9" * 64,
            "can_assign_new": True,
            "can_continue": True,
            "can_learn": True,
            "output_md": "## HOLD\n待补齐证据后决策",
            "task_guide": {"evidence_requirements": [
                {"input_id": "RI-01", "label": "当前客流基线"},
                {"input_id": "RI-02", "label": "客群和价格带"},
                {"input_id": "RI-03", "label": "竞品门店现状"},
                {"input_id": "RI-04", "label": "投入与回收红线"},
                {
                    "input_id": "RI-05",
                    "label": (
                        '<img src=x onerror="window.__evidenceXss=1">'
                        "决策日"
                    ),
                },
            ]},
            "brief": {"decision_evidence": {"items": [{
                "input_id": "RI-01",
                "content": "服务端冻结内容",
                "source_name": (
                    '</span><img src=x onerror="window.__evidenceXss=2">'
                ),
            }]}},
            "thread": {
                "status": "active",
                "revision_count": 1,
                "current_task_id": 77,
                "resume_task_id": 77,
                "can_continue": True,
                "can_accept": True,
                "thread_id": 9,
                "revisions": [],
            },
        }

        async with async_playwright() as playwright:
            browser = await _launch_chromium(playwright, self.executable)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
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

            result = await page.evaluate(
                """async () => {
                  window.__evidenceXss=0;
                  const apiRequest=api;
                  const host=document.createElement("section");
                  host.id="evidence-test-host";
                  document.querySelector("#main").append(host);
                  const requirements=[
                    {input_id:"RI-01",label:"当前客流基线"},
                    {input_id:"RI-02",label:"客群和价格带"},
                    {input_id:"RI-03",label:"竞品门店现状"},
                    {input_id:"RI-04",label:"投入与回收红线"},
                    {input_id:"RI-05",label:'<img src=x onerror="window.__evidenceXss=1">决策日'}
                  ];
                  const guide={
                    task_placeholder:"请给出进入决策",
                    industry_placeholder:"零售门店",
                    material_placeholder:"通用补充材料",
                    input_tips:["目标区域"],
                    output_hint:"可审计的决策建议",
                    evidence_requirements:requirements
                  };
                  const expert={idx:5301,name:"零售决策员",dept_name:"零售产业部",tasks:[],task_guide:guide,
                    identity_ref:"1".repeat(64),config_revision:3,
                    config_sha256:"2".repeat(64),bundle_sha256:"3".repeat(64)};
                  SPEC=expert;
                  host.innerHTML=specTaskTab(expert);
                  const v2Panel=host.querySelector("[data-decision-evidence-panel]");
                  const v2Fields=[...v2Panel.querySelectorAll("textarea[data-evidence-input-id]")];
                  const labelsLinked=v2Fields.every(field=>host.querySelector(`label[for="${field.id}"]`));
                  v2Fields[0].value="过去 28 天日均客流 127，来自 POS 日报";
                  v2Fields[0].dispatchEvent(new Event("input",{bubbles:true}));
                  v2Fields[2].value="三家竞品的现场记录和公开价盘";
                  v2Fields[2].dispatchEvent(new Event("input",{bubbles:true}));
                  const initialCount=v2Panel.querySelector('[role="status"]').textContent.trim();
                  const initialRowStatuses=v2Fields.map(field=>
                    document.getElementById(`${field.id}-status`).textContent.trim());
                  const formSnapshot=captureFormState();
                  v2Fields[0].value="";v2Fields[2].value="";
                  decisionEvidenceCountUpdate(v2Panel.id);
                  restoreFormState(formSnapshot);
                  const restoredCount=v2Panel.querySelector('[role="status"]').textContent.trim();
                  const initialCalls=[],initialKeys=[];
                  api=async(path,opts)=>{
                    initialCalls.push({path,body:structuredClone(opts.body)});
                    return {task_id:901};
                  };
                  persistentMutationRequestKey=(kind,scope,payload)=>{
                    initialKeys.push(structuredClone(payload));
                    return "initialtask-evidence-browser-0001";
                  };
                  specOpenTask=async()=>{};
                  host.querySelector("#spec-dir").value="判断新商圈是否值得进入";
                  await specSubmit(5301,host.querySelector('button[onclick^="specSubmit"]'));

                  const v2Snapshot={
                    panelCount:host.querySelectorAll("[data-decision-evidence-panel]").length,
                    rowCount:v2Fields.length,
                    labelsLinked,
                    count:initialCount,
                    restoredCount,
                    rowStatuses:initialRowStatuses,
                    itemMax:v2Fields[0].maxLength,
                    boundedRequirementCount:(()=>{
                      const boundedHost=document.createElement("div");
                      boundedHost.innerHTML=decisionEvidenceChecklist(
                        Array.from({length:9},(_,index)=>({
                          input_id:`RI-${String(index+1).padStart(2,"0")}`,
                          label:`证据 ${index+1}`
                        })),{panelId:"bounded-evidence"}
                      );
                      return boundedHost.querySelectorAll(
                        "textarea[data-evidence-input-id]"
                      ).length;
                    })(),
                    itemAtLimitAccepted:!decisionEvidenceItemsTooLong([
                      {content:"x".repeat(4000),source_name:""}
                    ]),
                    itemOverLimitRejected:decisionEvidenceItemsTooLong([
                      {content:"x".repeat(4001),source_name:""}
                    ]),
                    sourceInclusiveTotalRejected:decisionEvidenceItemsTooLong(
                      Array.from({length:5},()=>({
                        content:"x".repeat(4000),source_name:"来源"
                      }))
                    ),
                    fitsMobile:v2Panel.getBoundingClientRect().right<=window.innerWidth
                      && document.documentElement.scrollWidth<=window.innerWidth,
                    escapedLabelText:v2Panel.textContent.includes('<img src=x onerror="window.__evidenceXss=1">'),
                    renderedImages:v2Panel.querySelectorAll("img").length,
                    body:initialCalls[0].body,
                    keyInput:initialKeys[0]
                  };

                  const restaurant={idx:5302,name:"餐厅运营员",dept_name:"餐饮产业部",tasks:[],
                    identity_ref:"4".repeat(64),config_revision:4,
                    config_sha256:"5".repeat(64),bundle_sha256:"6".repeat(64),task_guide:{
                    task_placeholder:"复盘餐厅经营",
                    industry_placeholder:"餐饮门店",
                    material_placeholder:"粘贴经营数据",
                    input_tips:["门店与期间"],output_hint:"餐厅经营复盘"
                  }};
                  SPEC=restaurant;
                  host.innerHTML=specTaskTab(restaurant);
                  host.querySelector("#spec-dir").value="复盘本月餐厅经营";
                  await specSubmit(5302,host.querySelector('button[onclick^="specSubmit"]'));
                  const v1Snapshot={
                    panelCount:host.querySelectorAll("[data-decision-evidence-panel]").length,
                    body:initialCalls[1].body,
                    keyInput:initialKeys[1]
                  };

                  const task=await apiRequest("/tasks/77",{routeScoped:false});
                  host.innerHTML=taskRevisionPanel(task,"spec");
                  const followPanel=host.querySelector("[data-decision-evidence-panel]");
                  const followFields=[...followPanel.querySelectorAll("textarea[data-evidence-input-id]")];
                  const frozenPlaceholder=followFields[0].placeholder;
                  const followInitialCount=followPanel.querySelector('[role="status"]').textContent.trim();
                  followFields[0].value="更新后的客流证据";
                  followFields[0].dispatchEvent(new Event("input",{bubbles:true}));
                  const frozenUpdateStatus=document.getElementById(`${followFields[0].id}-status`).textContent.trim();
                  followFields[0].value="";
                  followFields[0].dispatchEvent(new Event("input",{bubbles:true}));
                  const frozenRestoredStatus=document.getElementById(`${followFields[0].id}-status`).textContent.trim();
                  followFields[1].value="25-34 岁客群占比 41%，来自会员脱敏汇总";
                  followFields[1].dispatchEvent(new Event("input",{bubbles:true}));
                  const followUpdatedCount=followPanel.querySelector('[role="status"]').textContent.trim();
                  const supplementalStatus=document.getElementById(`${followFields[1].id}-status`).textContent.trim();
                  followFields[1].focus();
                  followFields[1].dispatchEvent(new KeyboardEvent("keydown",{
                    key:"Escape",bubbles:true,cancelable:true
                  }));
                  const escapeClosed=!followPanel.open&&document.activeElement===followPanel.querySelector("summary");
                  followPanel.open=true;
                  host.querySelector("#follow-feedback-77").value="补充证据后重新判断";
                  const followCalls=[],followKeys=[];
                  api=async(path,opts)=>{
                    followCalls.push({path,body:structuredClone(opts.body)});
                    return {task_id:78,thread:{revision_count:2}};
                  };
                  persistentMutationRequestKey=(kind,scope,payload)=>{
                    followKeys.push(structuredClone(payload));
                    return "followup-evidence-browser-0001";
                  };
                  await taskFollowup(77,"spec",host.querySelector('button[onclick^="taskFollowup"]'),
                    task.identity_ref,task.config_revision,task.config_sha256,task.bundle_sha256);
                  return {
                    v2:v2Snapshot,v1:v1Snapshot,xss:window.__evidenceXss,
                    follow:{
                      initialCount:followInitialCount,updatedCount:followUpdatedCount,
                      frozenPlaceholder,frozenUpdateStatus,frozenRestoredStatus,
                      supplementalStatus,escapeClosed,
                      frozenText:followPanel.textContent,
                      renderedImages:followPanel.querySelectorAll("img").length,
                      body:followCalls[0].body,keyInput:followKeys[0]
                    }
                  };
                }"""
            )

            self.assertEqual(1, result["v2"]["panelCount"])
            self.assertEqual(5, result["v2"]["rowCount"])
            self.assertTrue(result["v2"]["labelsLinked"])
            self.assertEqual("已提供 2/5", result["v2"]["count"])
            self.assertEqual("已提供 2/5", result["v2"]["restoredCount"])
            self.assertEqual(
                ["本轮已填写", "待补", "本轮已填写", "待补", "待补"],
                result["v2"]["rowStatuses"],
            )
            self.assertEqual(4000, result["v2"]["itemMax"])
            self.assertEqual(8, result["v2"]["boundedRequirementCount"])
            self.assertTrue(result["v2"]["itemAtLimitAccepted"])
            self.assertTrue(result["v2"]["itemOverLimitRejected"])
            self.assertTrue(result["v2"]["sourceInclusiveTotalRejected"])
            self.assertTrue(result["v2"]["fitsMobile"])
            self.assertTrue(result["v2"]["escapedLabelText"])
            self.assertEqual(0, result["v2"]["renderedImages"])
            expected_initial = [
                {
                    "input_id": "RI-01",
                    "content": "过去 28 天日均客流 127，来自 POS 日报",
                },
                {
                    "input_id": "RI-03",
                    "content": "三家竞品的现场记录和公开价盘",
                },
            ]
            self.assertEqual(
                expected_initial,
                result["v2"]["body"]["brief"]["evidence_items"],
            )
            self.assertEqual(
                expected_initial,
                result["v2"]["keyInput"]["brief"]["evidence_items"],
            )
            expected_v2_binding = {
                "identity_ref": "1" * 64,
                "config_revision": 3,
                "config_sha256": "2" * 64,
                "bundle_sha256": "3" * 64,
            }
            for payload in (result["v2"]["body"], result["v2"]["keyInput"]):
                self.assertEqual(
                    expected_v2_binding,
                    {key: payload[key] for key in expected_v2_binding},
                )
            self.assertEqual(0, result["v1"]["panelCount"])
            self.assertNotIn("evidence_items", result["v1"]["body"]["brief"])
            self.assertNotIn("evidence_items", result["v1"]["keyInput"]["brief"])
            expected_v1_binding = {
                "identity_ref": "4" * 64,
                "config_revision": 4,
                "config_sha256": "5" * 64,
                "bundle_sha256": "6" * 64,
            }
            for payload in (result["v1"]["body"], result["v1"]["keyInput"]):
                self.assertEqual(
                    expected_v1_binding,
                    {key: payload[key] for key in expected_v1_binding},
                )
            self.assertEqual("已提供 1/5", result["follow"]["initialCount"])
            self.assertEqual("已提供 2/5", result["follow"]["updatedCount"])
            self.assertIn("沿用已冻结证据", result["follow"]["frozenPlaceholder"])
            self.assertEqual("本轮已更新", result["follow"]["frozenUpdateStatus"])
            self.assertIn("已冻结", result["follow"]["frozenRestoredStatus"])
            self.assertEqual("本轮已填写", result["follow"]["supplementalStatus"])
            self.assertIn("已冻结", result["follow"]["frozenText"])
            self.assertIn("本轮可更新", result["follow"]["frozenText"])
            self.assertTrue(result["follow"]["escapeClosed"])
            self.assertEqual(0, result["follow"]["renderedImages"])
            self.assertEqual(0, result["xss"])
            expected_followup = [{
                "input_id": "RI-02",
                "content": "25-34 岁客群占比 41%，来自会员脱敏汇总",
            }]
            self.assertEqual(
                expected_followup, result["follow"]["body"]["evidence_items"]
            )
            self.assertEqual(
                expected_followup, result["follow"]["keyInput"]["evidence_items"]
            )
            expected_followup_binding = {
                "identity_ref": "7" * 64,
                "config_revision": 1,
                "config_sha256": "8" * 64,
                "bundle_sha256": "9" * 64,
            }
            for payload in (
                result["follow"]["body"], result["follow"]["keyInput"]
            ):
                self.assertEqual(
                    expected_followup_binding,
                    {key: payload[key] for key in expected_followup_binding},
                )
            payload_text = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("evidence_id", payload_text)
            self.assertNotIn("manifest", payload_text)
            self._assert_no_browser_errors(browser_errors)
            await browser.close()

    async def test_schema54_current_and_frozen_role_identity_ui_and_payloads(self):
        payloads = {
            "/api/auth/me": {
                "id": 54,
                "username": "boss",
                "role": "root",
                "tenant": "岗位身份测试企业",
                "modules": ["content", "retail"],
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
            browser = await _launch_chromium(playwright, self.executable)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
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

            result = await page.evaluate(
                """async () => {
                  window.__schema54Xss=0;
                  const identityRef="a".repeat(64),configHash="b".repeat(64),
                    currentBundle="e".repeat(64),historicalBundle="f".repeat(64);
                  const profile={
                    scope:'只处理 <img src=x onerror="window.__schema54Xss=1"> 可审计选址判断',
                    decisions:["是否进入目标商圈","缺证时 HOLD 还是升级"],
                    knowledge_domains:["客流口径","租金结构","竞品供给"],
                    data_objects:["POS 日报","租赁报价","竞品踏勘表"],
                    skill_tree:["核验来源","计算区间","寻找反证","比较方案","生成门禁"],
                    capabilities:["事实核验","情景比较","指标复算","行动编排"],
                    operating_rhythm:{daily:"每日核对新鲜度",event_driven:"数据变化立即重算",review:"到期复盘偏差"},
                    tool_permissions:[
                      {tool:"企业事实查询器",access:"read_only",scope:"POS 日报"},
                      {tool:"证据追溯器",access:"read_only",scope:"租赁报价"}
                    ],
                    escalation_matrix:[
                      {level:"HOLD",condition:"证据缺失",owner:"数据负责人",action:"补证"},
                      {level:"ESCALATE",condition:"越过红线",owner:"有权审批人",action:"人工决定"}
                    ],
                    learning_tracks:["学习口径变化","沉淀反证案例","用结果校准边界"]
                  };
                  const current={
                    idx:5401,name:"商圈进入决策员",person:"林策",dept_name:"零售产业部",group:"增长决策组",
                    color:"#2f6b5f",emoji:"🧭",duty:"判断新商圈是否值得进入",desc:"形成可审计进入判断",
                    intro:"围绕真实客流、租金和竞品证据给出判断",enabled:true,tasks:[],stats:{runs:2,cost_usd:1},
                    skills:[],capabilities:[],inputs:[],steps:[],deliverables:[],prompt_template:"",is_custom:false,
                    task_guide:{task_placeholder:"判断是否进入",industry_placeholder:"目标商圈",material_placeholder:"业务资料",input_tips:[],output_hint:"判断"},
                    person_status:"active",identity_status:"current",identity_ref:identityRef,
                    config_revision:7,config_sha256:configHash,bundle_sha256:currentBundle,
                    can_assign_new:true,can_continue:true,can_learn:true,
                    role_profile_summary:{has_profile:true,capability_count:4,skill_count:5},professional_profile:profile
                  };
                  const historical={...current,identity_status:"historical",identity_ref:"c".repeat(64),
                    config_revision:2,config_sha256:"d".repeat(64),bundle_sha256:historicalBundle,
                    can_assign_new:false,can_continue:true,can_learn:false};

                  const axes={
                    current:employeeIdentityState(current),historical:employeeIdentityState(historical),
                    currentAssign:employeeCanAssignNew(current),historicalAssign:employeeCanAssignNew(historical),
                    currentLearn:employeeCanLearn(current),historicalLearn:employeeCanLearn(historical),
                    historicalContinue:employeeCanContinue(historical),historicalLabel:employeeIdentityLabel(historical)
                  };

                  SPEC=current;SPEC_TAB="info";SPEC_TASK=null;drawSpec();
                  const currentPanel=document.querySelector("#modal .panel");
                  const currentText=currentPanel.textContent;
                  const currentSnapshot={
                    profileGroups:currentPanel.querySelectorAll('section[aria-label="专业岗位档案"] details').length,
                    taskTab:[...currentPanel.querySelectorAll(".tb")].some(el=>el.textContent.includes("派活")),
                    promptTab:[...currentPanel.querySelectorAll(".tb")].some(el=>el.textContent.includes("自定义配置")),
                    text:currentText,renderedImages:currentPanel.querySelectorAll("img").length,
                    escapedAttack:currentText.includes('<img src=x onerror="window.__schema54Xss=1">'),
                    fitsMobile:currentPanel.getBoundingClientRect().right<=window.innerWidth
                      && document.documentElement.scrollWidth<=window.innerWidth
                  };

                  SPEC_TAB="task";drawSpec();
                  document.querySelector("#spec-dir").value="判断新商圈是否值得进入";
                  const createCalls=[],createKeys=[];
                  api=async(path,opts)=>{createCalls.push({path,body:structuredClone(opts.body)});return {task_id:9541};};
                  persistentMutationRequestKey=(kind,scope,payload)=>{createKeys.push(structuredClone(payload));return "schema54-current-create-0001";};
                  specOpenTask=async()=>{};
                  await specSubmit(current.idx,document.querySelector('button[onclick^="specSubmit"]'));

                  const learnCalls=[];
                  api=async(path,opts)=>{learnCalls.push({path,body:structuredClone(opts?.body||{})});return current;};
                  await specLearn(current.idx);

                  SPEC=historical;SPEC_TAB="info";drawSpec();
                  const historicalPanel=document.querySelector("#modal .panel");
                  const historicalSnapshot={
                    taskTab:[...historicalPanel.querySelectorAll(".tb")].some(el=>el.textContent.includes("派活")),
                    promptTab:[...historicalPanel.querySelectorAll(".tb")].some(el=>el.textContent.includes("自定义配置")),
                    text:historicalPanel.textContent
                  };

                  const followHost=document.createElement("div");document.body.append(followHost);
                  const task={id:77,status:"done",revision_no:1,brief:{},identity_ref:historical.identity_ref,
                    config_revision:historical.config_revision,config_sha256:historical.config_sha256,
                    bundle_sha256:historical.bundle_sha256,
                    person_status:"active",identity_status:"historical",
                    can_continue:true,thread:{status:"active",revision_count:1,current_task_id:77,resume_task_id:77,
                      can_continue:true,can_accept:false,thread_id:4,revisions:[]}};
                  followHost.innerHTML=taskRevisionPanel(task,"spec");
                  followHost.querySelector("#follow-feedback-77").value="沿原线程补充租金证据";
                  const followCalls=[],followKeys=[];
                  api=async(path,opts)=>{followCalls.push({path,body:structuredClone(opts.body)});return {task_id:78,thread:{revision_count:2}};};
                  persistentMutationRequestKey=(kind,scope,payload)=>{followKeys.push(structuredClone(payload));return "schema54-followup-0001";};
                  specOpenTask=async()=>{};
                  await taskFollowup(77,"spec",followHost.querySelector("button"),task.identity_ref,
                    task.config_revision,task.config_sha256,task.bundle_sha256);

                  return {axes,current:currentSnapshot,historical:historicalSnapshot,
                    createBody:createCalls[0].body,createKey:createKeys[0],learnBody:learnCalls[0].body,
                    followBody:followCalls[0].body,followKey:followKeys[0],xss:window.__schema54Xss};
                }"""
            )

            self.assertEqual(
                {"personStatus": "active", "identityStatus": "current"},
                result["axes"]["current"],
            )
            self.assertEqual(
                {"personStatus": "active", "identityStatus": "historical"},
                result["axes"]["historical"],
            )
            self.assertTrue(result["axes"]["currentAssign"])
            self.assertFalse(result["axes"]["historicalAssign"])
            self.assertTrue(result["axes"]["currentLearn"])
            self.assertFalse(result["axes"]["historicalLearn"])
            self.assertTrue(result["axes"]["historicalContinue"])
            self.assertEqual(
                "员工仍在岗 · 此任务使用历史岗位版本",
                result["axes"]["historicalLabel"],
            )
            self.assertEqual(8, result["current"]["profileGroups"])
            self.assertTrue(result["current"]["taskTab"])
            self.assertTrue(result["current"]["promptTab"])
            self.assertTrue(result["current"]["escapedAttack"])
            self.assertEqual(0, result["current"]["renderedImages"])
            self.assertTrue(result["current"]["fitsMobile"])
            for label in (
                "专业知识域", "数据对象", "技能树", "核心能力", "工作流程",
                "工具权限", "升级路径", "学习路径",
            ):
                self.assertIn(label, result["current"]["text"])
            self.assertFalse(result["historical"]["taskTab"])
            self.assertFalse(result["historical"]["promptTab"])
            self.assertIn(
                "员工仍在岗 · 此任务使用历史岗位版本",
                result["historical"]["text"],
            )
            for payload in (result["createBody"], result["createKey"]):
                self.assertEqual("a" * 64, payload["identity_ref"])
                self.assertEqual(7, payload["config_revision"])
                self.assertEqual("b" * 64, payload["config_sha256"])
                self.assertEqual("e" * 64, payload["bundle_sha256"])
            self.assertEqual("a" * 64, result["learnBody"]["identity_ref"])
            self.assertEqual(7, result["learnBody"]["config_revision"])
            self.assertEqual("b" * 64, result["learnBody"]["config_sha256"])
            self.assertEqual("e" * 64, result["learnBody"]["bundle_sha256"])
            for payload in (result["followBody"], result["followKey"]):
                self.assertEqual("c" * 64, payload["identity_ref"])
                self.assertEqual(2, payload["config_revision"])
                self.assertEqual("d" * 64, payload["config_sha256"])
                self.assertEqual("f" * 64, payload["bundle_sha256"])
            self.assertEqual(0, result["xss"])
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
            browser = await _launch_chromium(playwright, self.executable)
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


    async def test_inspection_import_is_collapsed_and_expands_from_top_action(self):
        payloads = {
            "/api/auth/me": {
                "id": 52,
                "username": "inspection-owner",
                "role": "owner",
                "tenant": "巡店浏览器测试企业",
                "modules": ["content", "avatar", "library"],
                "all_modules": [],
            },
            "/api/meta": {},
            "/api/state": {"jobs": [], "inbox": [], "notifications": []},
            "/api/employees": [],
            "/api/inspections/meta": {
                "industry_key": "restaurant",
                "industries": [{
                    "key": "restaurant",
                    "name": "餐饮产业部",
                    "emoji": "🍜",
                }],
                "permissions": {"can_import_branches": True},
                "branches": [],
            },
            "/api/inspections": {
                "items": [],
                "summary": {"branches": [], "regions": []},
            },
            "/api/inspections/branches/search": {
                "items": [],
                "next_before_id": None,
            },
        }

        async with async_playwright() as playwright:
            browser = await _launch_chromium(playwright, self.executable)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            await page.emulate_media(reduced_motion="reduce")
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
                payload = payloads.get(path, {})
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            await page.route("**/api/**", api_route)
            await page.goto(
                f"{self.base}/static/index.html#/inspections/0/restaurant"
            )
            toggle = page.locator("#inspection-import-toggle")
            await toggle.wait_for()

            self.assertEqual("false", await toggle.get_attribute("aria-expanded"))
            workbench = page.locator("#inspection-import-workbench")
            self.assertEqual(1, await workbench.count())
            self.assertTrue(await workbench.is_hidden())
            self.assertIn("批量导入门店", await toggle.inner_text())
            await page.locator("#inspection-scope").fill("保留这段巡店重点")
            await page.locator("#inspection-branch-q").fill("S-keep-search")

            await toggle.click()
            await workbench.wait_for(state="visible")
            self.assertEqual("true", await toggle.get_attribute("aria-expanded"))
            await page.wait_for_function(
                "document.activeElement?.id==='inspection-import-workbench'"
            )
            self.assertEqual(
                "inspection-import-workbench",
                await page.evaluate("document.activeElement?.id"),
            )
            workbench_text = await workbench.inner_text()
            self.assertIn("🍜 餐饮产业部", workbench_text)
            self.assertIn("门店批量导入模板", workbench_text)
            self.assertIn("选择填好的 Excel", workbench_text)
            self.assertEqual(
                0, await workbench.locator("header, .inspection-import-steps").count()
            )
            context_box = await page.locator(
                ".inspection-import-context-row"
            ).bounding_box()
            layout_box = await page.locator(
                ".inspection-import-layout"
            ).bounding_box()
            template_box = await page.locator(
                ".inspection-import-template"
            ).bounding_box()
            upload_box = await page.locator(
                ".inspection-import-upload"
            ).bounding_box()
            self.assertIsNotNone(context_box)
            self.assertIsNotNone(layout_box)
            self.assertIsNotNone(template_box)
            self.assertIsNotNone(upload_box)
            self.assertLessEqual(
                context_box["y"] + context_box["height"],
                layout_box["y"] + 1,
            )
            self.assertGreaterEqual(template_box["height"], 170)
            self.assertLessEqual(template_box["height"], 210)
            self.assertGreaterEqual(upload_box["height"], 170)
            self.assertLessEqual(upload_box["height"], 210)
            self.assertLessEqual(abs(template_box["height"] - upload_box["height"]), 1)
            column_ratio = template_box["width"] / (
                template_box["width"] + upload_box["width"]
            )
            self.assertGreaterEqual(column_ratio, 0.32)
            self.assertLessEqual(column_ratio, 0.40)
            self.assertEqual(
                0,
                await page.get_by_role("button", name="确认提交导入").count(),
            )
            upload_button_box = await page.locator(
                "#inspection-import-upload-button"
            ).bounding_box()
            self.assertIsNotNone(upload_button_box)
            self.assertGreaterEqual(upload_button_box["height"], 44)
            self.assertLessEqual(upload_button_box["height"], 48)
            self.assertTrue(
                await page.locator("#inspection-import-upload-button").is_disabled()
            )
            await page.locator("#inspection-import-file").set_input_files({
                "name": "全公司门店.xlsx",
                "mimeType": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                "buffer": b"PK-browser-contract",
            })
            self.assertFalse(
                await page.locator("#inspection-import-upload-button").is_disabled()
            )
            self.assertIn(
                "全公司门店.xlsx",
                await page.locator("#inspection-import-file-name").inner_text(),
            )

            for _ in range(2):
                await toggle.focus()
                await toggle.press("Enter")
                await workbench.wait_for(state="hidden")
                self.assertEqual(
                    "false", await toggle.get_attribute("aria-expanded")
                )
                self.assertEqual(
                    "保留这段巡店重点",
                    await page.locator("#inspection-scope").input_value(),
                )
                self.assertEqual(
                    "S-keep-search",
                    await page.locator("#inspection-branch-q").input_value(),
                )
                self.assertEqual(
                    "全公司门店.xlsx",
                    await page.locator("#inspection-import-file").evaluate(
                        "input => input.files[0]?.name"
                    ),
                )
                await toggle.press("Enter")
                await workbench.wait_for(state="visible")
                self.assertEqual(
                    "true", await toggle.get_attribute("aria-expanded")
                )
                self.assertEqual(
                    "全公司门店.xlsx",
                    await page.locator("#inspection-import-file").evaluate(
                        "input => input.files[0]?.name"
                    ),
                )
            self._assert_no_browser_errors(browser_errors)
            await browser.close()

    async def test_late_boss_summary_cannot_replace_a_newer_scope(self):
        stale_started = asyncio.Event()
        release_stale = asyncio.Event()
        common = {
            "/api/auth/me": {
                "id": 53,
                "username": "boss-race-owner",
                "role": "owner",
                "tenant": "老板看板代次测试企业",
                "modules": ["content", "restaurant"],
                "all_modules": [],
            },
            "/api/meta": {"stations": []},
            "/api/state": {"jobs": [], "inbox": [], "notifications": []},
            "/api/employees": [],
            "/api/boss/dashboard/scopes": {
                "can_cross_tenant": False,
                "tenants": [{
                    "id": 1,
                    "name": "老板看板代次测试企业",
                    "industries": [{
                        "key": "restaurant", "name": "餐饮产业部", "emoji": "🍜"
                    }],
                }],
            },
        }

        def summary(days: int, marker: str):
            return {
                "scope": {
                    "tenant_id": 1,
                    "tenant_name": "老板看板代次测试企业",
                    "industry_key": "restaurant",
                    "industry_name": marker,
                    "industry_emoji": "🍜",
                },
                "period": {"days": days},
                "task_metrics": {},
                "efficiency_metrics": {},
                "risk_metrics": {},
                "inspection_metrics": {
                    "availability": False,
                    "reason_code": "inspection_schema_unavailable",
                    "backlog": {},
                },
                "business_metrics": {"metrics": []},
                "trend": [],
                "employees": [],
                "recent_activity": [],
                "can_open_records": True,
            }

        async with async_playwright() as playwright:
            browser = await _launch_chromium(playwright, self.executable)
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
                parsed = urlparse(route.request.url)
                if parsed.path == "/api/boss/dashboard/summary":
                    days = int(parse_qs(parsed.query).get("days", ["30"])[0])
                    if days == 7:
                        stale_started.set()
                        await release_stale.wait()
                        payload = summary(7, "STALE-7")
                    elif days == 60:
                        payload = summary(60, "CURRENT-60")
                    else:
                        payload = summary(30, "INITIAL-30")
                else:
                    payload = common.get(parsed.path, {})
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/boss")
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('INITIAL-30')"
            )

            await page.evaluate("void bossDashDays(7)")
            await asyncio.wait_for(stale_started.wait(), timeout=5)
            await page.evaluate("void bossDashDays(60)")
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('CURRENT-60')"
            )
            release_stale.set()
            await asyncio.sleep(0.2)

            main_text = await page.locator("#main").inner_text()
            self.assertIn("CURRENT-60", main_text)
            self.assertNotIn("STALE-7", main_text)
            self.assertEqual(
                {"days": 60, "industry": "CURRENT-60"},
                await page.evaluate(
                    "({days:BOSS_DASH_DATA.period.days,"
                    "industry:BOSS_DASH_DATA.scope.industry_name})"
                ),
            )
            self._assert_no_browser_errors(browser_errors)
            await browser.close()

    async def test_stale_inspection_import_upload_cannot_cross_industry(self):
        upload_started = asyncio.Event()
        release_upload = asyncio.Event()
        industries = [
            {"key": "restaurant", "name": "餐饮产业部", "emoji": "🍜"},
            {"key": "retail", "name": "零售产业部", "emoji": "🛍️"},
        ]
        common = {
            "/api/auth/me": {
                "id": 53,
                "username": "inspection-scope-owner",
                "role": "owner",
                "tenant": "巡店代次测试企业",
                "modules": ["content", "avatar", "library"],
                "all_modules": [],
            },
            "/api/meta": {},
            "/api/state": {"jobs": [], "inbox": [], "notifications": []},
            "/api/employees": [],
            "/api/inspections": {
                "items": [],
                "summary": {"branches": [], "regions": []},
            },
            "/api/inspections/branches/search": {
                "items": [],
                "next_before_id": None,
            },
        }

        async with async_playwright() as playwright:
            browser = await _launch_chromium(playwright, self.executable)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
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
                parsed = urlparse(route.request.url)
                path = parsed.path
                if (
                    path == "/api/inspections/branches/imports"
                    and route.request.method == "POST"
                ):
                    upload_started.set()
                    await release_upload.wait()
                    payload = {
                        "import_id": 5301,
                        "status": "ready",
                        "counts": {
                            "create": 1, "update": 0, "skip": 0, "error": 0
                        },
                        "business_counts": {
                            "create": 0, "update": 0, "skip": 0, "error": 0
                        },
                        "rows": [],
                    }
                elif path == "/api/inspections/meta":
                    query = parse_qs(parsed.query)
                    industry_key = query.get(
                        "industry_key", ["restaurant"]
                    )[0]
                    payload = {
                        "industry_key": industry_key,
                        "industries": industries,
                        "permissions": {"can_import_branches": True},
                        "branches": [],
                    }
                else:
                    payload = common.get(path, {})
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            await page.route("**/api/**", api_route)
            await page.goto(
                f"{self.base}/static/index.html#/inspections/0/restaurant"
            )
            await page.locator("#inspection-import-toggle").click()
            await page.locator("#inspection-import-file").set_input_files({
                "name": "餐饮门店.xlsx",
                "mimeType": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                "buffer": b"PK-stale-industry-contract",
            })
            await page.locator("#inspection-import-upload-button").click()
            await asyncio.wait_for(upload_started.wait(), timeout=5)

            await page.locator(
                'select[onchange*="inspectionSelectIndustry"]'
            ).select_option("retail")
            await page.wait_for_url("**#/inspections/0/retail")
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('零售产业部')"
            )
            release_upload.set()
            await page.wait_for_function(
                """() => {
                  try {
                    return INSPECTION_INDUSTRY === "retail"
                      && INSPECTION_IMPORT.status === "idle"
                      && INSPECTION_IMPORT.preview === null;
                  } catch (_) { return false; }
                }"""
            )

            main_text = await page.locator("#main").inner_text()
            self.assertIn("零售产业部", main_text)
            self.assertNotIn("数据已通过校验", main_text)
            self.assertNotIn("待确认", await page.locator(
                "#inspection-import-toggle"
            ).inner_text())
            self._assert_no_browser_errors(browser_errors)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
