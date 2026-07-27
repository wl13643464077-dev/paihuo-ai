"""前端页面切换不得因重复加载全局数据而看起来“点了没反应”."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendNavigationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.promo_html = (ROOT / "static" / "promo.html").read_text(encoding="utf-8")
        cls.main_py = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        cls.funnel_py = (ROOT / "app" / "funnel.py").read_text(encoding="utf-8")

    def test_route_click_shows_loading_and_reuses_shell_data(self):
        self.assertIn("function routeLoading(page)", self.app_js)
        self.assertIn('window.addEventListener("hashchange"', self.app_js)
        self.assertIn("render(true);", self.app_js)
        self.assertIn("if(!reuseShell||!STATE||!EMP||SHELL_DIRTY)", self.app_js)
        self.assertIn("页面已经收到您的点击", self.app_js)

    def test_slow_old_route_is_cancelled_before_it_can_cover_the_new_page(self):
        self.assertIn("if(ROUTE_CONTROLLER) ROUTE_CONTROLLER.abort()", self.app_js)
        self.assertIn("const renderSeq=++NAV_SEQ", self.app_js)
        self.assertIn('stale.name="NavigationAbort"', self.app_js)
        self.assertIn("renderSeq!==NAV_SEQ", self.app_js)

    def test_station_panel_uses_backend_review_contract(self):
        self.assertNotIn("needsWait(", self.app_js)
        self.assertIn("r.needs_review", self.app_js)

    def test_solo_polling_has_a_bounded_lifecycle(self):
        self.assertIn("function stopSoloWatch()", self.app_js)
        self.assertRegex(
            self.app_js,
            r"function closeModal\(\)\{\s*stopSoloWatch\(\);",
        )
        self.assertIn("SOLO_TIMER=null", self.app_js)
        # 12s:轮询兜底 SSE(实时步骤流已挂 data-tasksteps),又不至于打爆后端。
        self.assertIn("setTimeout(tick, 12000)", self.app_js)
        # 单人任务的实时步骤流挂载点:没有它,老板派完活要干等一个轮询周期。
        self.assertIn('data-tasksteps="${t.id}"', self.app_js)
        self.assertIn('window.addEventListener("pagehide"', self.app_js)

    def test_optional_route_data_does_not_swallow_abort_or_permission(self):
        self.assertIn(
            'if(e?.name==="NavigationAbort"||e?.status===401||e?.status===403) throw e;',
            self.app_js,
        )
        self.assertIn("err.status=401", self.app_js)
        self.assertEqual(
            self.app_js.count('api("/channels/wechat").catch(optionalResult(null))'),
            1,
        )
        self.assertIn('api("/pstyles").catch(optionalResult({}))', self.app_js)
        self.assertIn('api("/channels/webhook").catch(optionalResult(null))', self.app_js)
        self.assertIn(
            'api("/matrix/accounts").catch(optionalResult({platforms:[],accounts:[]}))',
            self.app_js,
        )
        self.assertIn(
            'api(listPath("/matrix/tasks","publish",MATRIX_FILTER)).catch(optionalResult([]))',
            self.app_js,
        )
        self.assertRegex(
            self.app_js,
            r'(?s)async function taskDetailView\(tid\).*?catch\(e\)\{\s*'
            r'if\(e\?\.name==="NavigationAbort"\|\|e\?\.status===403\) throw e;',
        )

    def test_api_scopes_only_reads_to_the_active_route(self):
        self.assertIn("longRunning=false, routeScoped", self.app_js)
        self.assertIn('const method=String(fetchOpts.method||"GET").toUpperCase()', self.app_js)
        self.assertIn('method==="GET"', self.app_js)
        self.assertIn("err.uncertain=!!longRunning", self.app_js)
        self.assertIn("服务器可能仍在处理", self.app_js)

    def test_known_long_operations_have_an_uncertain_timeout_contract(self):
        self.assertGreaterEqual(self.app_js.count("timeout:330000,longRunning:true"), 4)
        for fn in ("distill", "companyDistill", "censorQuick", "cenRetro"):
            start = self.app_js.index(f"function {fn}(")
            next_fn = self.app_js.find("\nfunction ", start + 1)
            if next_fn < 0:
                next_fn = len(self.app_js)
            self.assertIn(
                "timeout:330000,longRunning:true",
                self.app_js[start:next_fn],
                f"{fn} 仍使用默认短超时",
            )

    def test_sse_is_singleton_and_recovers_authoritative_state(self):
        self.assertIn("let SSE = null", self.app_js)
        self.assertIn("function stopSse(", self.app_js)
        self.assertIn("function scheduleSseReconnect(", self.app_js)
        self.assertIn("function refreshAuthoritativeState()", self.app_js)
        self.assertIn('document.addEventListener("visibilitychange"', self.app_js)
        self.assertIn("refreshAuthoritativeState();", self.app_js)

    def test_sse_has_a_40_second_heartbeat_watchdog(self):
        self.assertIn("const SSE_STALE_MS = 40000", self.app_js)
        self.assertIn("function startSseWatchdog()", self.app_js)
        self.assertIn("SSE_LAST_EVENT_AT=Date.now()", self.app_js)
        self.assertIn("scheduleSseReconnect(0)", self.app_js)
        self.assertIn(
            'yield "data: {\\"type\\":\\"ping\\"}\\n\\n"',
            self.main_py,
        )

    def test_sse_refresh_preserves_safe_form_drafts(self):
        self.assertIn("function captureFormState()", self.app_js)
        self.assertIn("function restoreFormState(snapshot)", self.app_js)
        self.assertIn('new Set(["password","file"', self.app_js)
        self.assertIn("TYPING_UNTIL", self.app_js)
        self.assertIn("captureFormState();", self.app_js)
        self.assertIn("restoreFormState(snapshot);", self.app_js)

    def test_refresh_preserves_details_and_routes_reset_scroll(self):
        self.assertIn('"#main details,#modal details"', self.app_js)
        self.assertIn("details:details.map", self.app_js)
        self.assertIn("el.open=!!saved.open", self.app_js)
        self.assertIn('window.scrollTo({top:0,left:0,behavior:"auto"})', self.app_js)

    def test_global_error_reporting_is_sanitized_and_non_blocking(self):
        self.assertIn("function reportClientError(", self.app_js)
        self.assertIn("function clientErrorFingerprint(", self.app_js)
        self.assertIn('"/api/clientlog"', self.app_js)
        self.assertIn('window.addEventListener("error"', self.app_js)
        self.assertIn('window.addEventListener("unhandledrejection"', self.app_js)
        reporter = self.app_js.split(
            "function reportClientError(", 1
        )[1].split("function busy(", 1)[0]
        self.assertIn("error_name:", reporter)
        self.assertIn("fingerprint,", reporter)
        self.assertNotIn("\n    message,", reporter)
        self.assertNotIn("\n    stack:", reporter)

    def test_product_funnel_is_server_observed_and_content_free(self):
        self.assertIn("const FUNNEL_EVENTS = new Set", self.app_js)
        self.assertIn("function trackFunnel(", self.app_js)
        self.assertIn('fetch("/api/funnel/events"', self.app_js)
        self.assertNotIn("paihuo_funnel_user_", self.app_js)
        self.assertIn('publicFunnel("landing_view","promo")', self.promo_html)
        self.assertIn('publicFunnel("pricing_view","promo")', self.promo_html)
        self.assertIn('@app.get("/api/admin/funnel")', self.main_py)
        for event in (
            "login_success",
            "lead_submitted",
            "registration_complete",
            "first_job_submitted",
            "pricing_view",
            "landing_view",
            "page_view",
        ):
            self.assertIn(f'"{event}"', self.funnel_py)
        start = self.app_js.index("function trackFunnel(")
        end = self.app_js.index("\n}", start) + 2
        source = self.app_js[start:end]
        self.assertIn('fetch("/api/funnel/events"', source)
        self.assertNotIn("sendBeacon", source)
        self.assertNotIn("message", source)
        self.assertNotIn("brief", source)
        self.assertNotIn("password", source)

    def test_mutations_are_deduplicated_and_long_brief_is_account_scoped(self):
        self.assertIn("const INFLIGHT_MUTATIONS = new Map()", self.app_js)
        self.assertIn("function mutationRequestKey(", self.app_js)
        self.assertIn("INFLIGHT_MUTATIONS.get(key)", self.app_js)
        self.assertIn("function briefDraftKey()", self.app_js)
        self.assertIn("brief_draft_user_", self.app_js)
        self.assertIn("function restoreBriefDraft(", self.app_js)
        self.assertIn("function clearBriefDraft(", self.app_js)
        self.assertIn("clearBriefDraft();", self.app_js)

    def test_product_dialogs_replace_native_blocking_dialogs(self):
        self.assertIn("function uiConfirm(", self.app_js)
        self.assertIn("function uiPrompt(", self.app_js)
        self.assertIn("function uiAlert(", self.app_js)
        self.assertNotRegex(self.app_js, r"(?<!ui)\bprompt\s*\(")
        self.assertNotRegex(self.app_js, r"(?<!ui)\balert\s*\(")
        self.assertNotRegex(self.app_js, r"(?<!ui)\bconfirm\s*\(")

    def test_lists_have_a_consumable_truncation_contract_and_bounded_state(self):
        self.assertIn("function normalizeListContract(", self.app_js)
        self.assertIn("function listContractNotice(", self.app_js)
        self.assertIn("function setBoundedState(", self.app_js)
        self.assertIn("function pushBoundedState(", self.app_js)
        self.assertGreaterEqual(self.app_js.count("listContractNotice("), 4)

    def test_bulk_assignment_reports_each_failure(self):
        start = self.app_js.index("async function mtAssignAll(")
        end = self.app_js.index("\n}", start) + 2
        source = self.app_js[start:end]
        self.assertIn("const failures=[]", source)
        self.assertIn("failures.push(", source)
        self.assertIn("uiAlert(", source)
        self.assertNotIn("catch(e){}", source)

    def test_job_detail_explains_whole_job_progress_and_safe_navigation(self):
        self.assertIn("function jobProgress(j)", self.app_js)
        self.assertIn("整单进度", self.app_js)
        self.assertIn("后台继续执行", self.app_js)
        self.assertIn('role="progressbar"', self.app_js)

    def test_knowledge_list_is_light_and_detail_is_loaded_on_demand(self):
        view_start = self.app_js.index("async function knowledgeView(")
        open_start = self.app_js.index("async function kbOpen(", view_start)
        form_start = self.app_js.index("async function knowForm(", open_start)
        save_start = self.app_js.index("async function knowSave(", form_start)
        view_source = self.app_js[view_start:open_start]
        open_source = self.app_js[open_start:form_start]
        form_source = self.app_js[form_start:save_start]

        self.assertIn(
            'normalizeListContract(await api(listPath("/knowledge","knowledge",{',
            view_source,
        )
        self.assertIn("const rows=knowledgeContract.items", view_source)
        self.assertIn("KNOW_CACHE = rows", view_source)
        self.assertIn('await api("/knowledge/"+id)', open_source)
        self.assertIn('await api("/knowledge/"+id)', form_source)
        self.assertNotIn('await api("/knowledge")', open_source)
        self.assertNotIn('await api("/knowledge")', form_source)

    def test_tool_cache_is_account_scoped_and_tour_cannot_read_it(self):
        self.assertIn('`tools_cache_user_${ME?.id??"none"}`', self.app_js)
        self.assertIn('localStorage.removeItem("tools_cache")', self.app_js)
        self.assertIn('ME?.role==="tour"||!can("content")', self.app_js)
        self.assertIn("TS.busy.leads||TS.running?.leads", self.app_js)

    def test_route_failure_replaces_spinner_with_retry(self):
        self.assertIn("页面暂时没加载出来", self.app_js)
        self.assertIn('onclick="render(true)"', self.app_js)

    def test_running_tools_refresh_without_reloading_the_whole_app_shell(self):
        self.assertIn("function invalidateToolJobs()", self.app_js)
        self.assertIn("function scheduleToolPoll()", self.app_js)
        self.assertIn(
            'await api("/tools/jobs?limit=40&offset=0")',
            self.app_js,
        )
        self.assertIn("},15000);", self.app_js)
        self.assertIn("最迟6分钟自动结束并退点", self.app_js)

    def test_large_lists_use_server_pagination_filters_and_navigation(self):
        for helper in (
            "function listPath(",
            "function listPager(",
            "function listGo(",
            "function toolHistory(",
        ):
            self.assertIn(helper, self.app_js)
        for key in ("assets", "knowledge", "censor", "video", "publish"):
            self.assertIn(f'listPager(', self.app_js)
            self.assertIn(f'"{key}"', self.app_js)
        self.assertIn("筛选和分页均由服务端处理", self.app_js)
        self.assertIn('platform:KFILTER.platform', self.app_js)
        self.assertIn('platform:AFILTER.platform', self.app_js)
        self.assertIn('listPath("/censor/logs","censor",CEN_LOG_FILTER)', self.app_js)
        self.assertIn('listPath("/text-video","video",{standalone:1})', self.app_js)
        self.assertIn('listPath("/matrix/tasks","publish",MATRIX_FILTER)', self.app_js)

    def test_frontend_change_has_a_new_script_version(self):
        self.assertIn("/static/app.js?v=45", self.index_html)
        self.assertIn('release:"web-v40"', self.app_js)

    def test_employee_task_forms_use_role_and_industry_specific_guidance(self):
        self.assertIn("const CONTENT_TASK_GUIDE_FALLBACK = {", self.app_js)
        self.assertIn("function taskGuideFor(", self.app_js)
        self.assertIn("function taskGuideCard(", self.app_js)
        for station in (
            "trend", "research", "benchmark", "draft", "style",
            "media", "cover", "deck", "publish", "retro",
        ):
            self.assertIn(f"  {station}:{{", self.app_js)
        self.assertGreaterEqual(
            self.app_js.count("taskGuideFor("),
            3,  # helper + 内容部派活 + 产业专家派活
        )
        self.assertIn(
            'placeholder="${esc(guide.task_placeholder)}"',
            self.app_js,
        )
        self.assertIn(
            'placeholder="${esc(guide.industry_placeholder)}"',
            self.app_js,
        )
        self.assertIn(
            'placeholder="${esc(guide.material_placeholder)}"',
            self.app_js,
        )
        self.assertIn("const TASK_DATA_PRIVACY_NOTE =", self.app_js)
        self.assertIn("姓名、手机号、证件号等个人标识", self.app_js)
        guide_card = self.app_js.split(
            "function taskGuideCard(", 1
        )[1].split(
            "const TASK_DATA_PRIVACY_NOTE", 1
        )[0]
        self.assertIn('class="tag"', guide_card)
        self.assertNotIn('class="chip on"', guide_card)
        self.assertGreaterEqual(
            self.app_js.count("${esc(TASK_DATA_PRIVACY_NOTE)}"),
            2,
        )
        # 这些曾经写死在所有 400+ 员工的共用表单里，绝不能回归。
        for restaurant_only_template in (
            'placeholder="如:餐饮"',
            "如:川菜正餐 / 咖啡快闪 / 茶饮连锁",
            "粘贴数据、地址、菜单;或直接传文件",
        ):
            self.assertNotIn(restaurant_only_template, self.app_js)

    def test_employee_dispatch_buttons_open_the_guided_task_form(self):
        self.assertIn("async function openSpec(idx,initialTab)", self.app_js)
        self.assertIn(
            '(initialTab==="task"?"task":"intro")',
            self.app_js,
        )
        self.assertIn(
            "event.stopPropagation();openSpec(${e.idx},'task')",
            self.app_js,
        )
        self.assertIn(
            'function pickExpert(idx){ EXP_PREFILL = EXP_LAST_QUERY; openSpec(idx,"task"); }',
            self.app_js,
        )
        self.assertIn(
            'ME&&ME.role==="tour" ? "intro"',
            self.app_js,
        )

    def test_dynamic_urls_and_inline_arguments_are_closed_at_the_sink(self):
        for helper in ("safeExternalUrl", "safeAssetUrl", "safeRouteUrl"):
            self.assertIn(f"function {helper}(", self.app_js)
        self.assertNotIn('href="javascript:', self.app_js)
        self.assertNotIn('src="${esc(c.thumb)}"', self.app_js)
        self.assertNotIn("clipToggle('${c.name}')", self.app_js)
        self.assertNotIn("clipDel('${c.name}')", self.app_js)
        self.assertNotIn("(j.brief.platforms||[]).join", self.app_js)
        self.assertNotIn("(s.brief.platforms||[]).join", self.app_js)
        self.assertIn('<iframe id="mp-frame" sandbox', self.app_js)
        self.assertIn("${esc(MP_CUR.title||\"\")}", self.app_js)
        self.assertIn('sandbox referrerpolicy="no-referrer"', self.app_js)
        self.assertIn('rel="noopener noreferrer"', self.app_js)


if __name__ == "__main__":
    unittest.main()
