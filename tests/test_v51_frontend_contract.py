"""V51 连续协作、巡店与老板看板的前端合同。"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    candidates = [
        position
        for position in (
            source.find("\nfunction ", start + 1),
            source.find("\nasync function ", start + 1),
        )
        if position >= 0
    ]
    return source[start : min(candidates) if candidates else len(source)]


class V51FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_followup_request_key_survives_uncertain_retry_and_rotates_with_input(self):
        self.assertIn("function persistentMutationRequestKey(", self.source)
        self.assertIn("function clearPersistentMutationRequestKey(", self.source)
        helper = function(self.source, "persistentMutationRequestKey")
        self.assertIn("fingerprint", helper)
        self.assertIn("localStorage", helper)
        submit = function(self.source, "taskFollowup")
        self.assertIn('persistentMutationRequestKey("followup"', submit)
        self.assertIn("{feedback,material,...binding}", submit)
        self.assertIn("identity_ref", submit)
        self.assertIn("config_revision", submit)
        self.assertIn("config_sha256", submit)
        self.assertIn("岗位版本信息不完整", submit)
        self.assertIn("longRunning:true", submit)
        self.assertIn("clearPersistentMutationRequestKey", submit)

    def test_v2_decision_evidence_is_server_guided_compact_and_accessible(self):
        guide = function(self.source, "taskGuideFor")
        checklist = function(self.source, "decisionEvidenceChecklist")
        normalizer = function(
            self.source, "normalizeDecisionEvidenceRequirements"
        )
        collector = function(self.source, "collectDecisionEvidenceItems")

        self.assertIn(
            "raw.evidence_requirements", guide,
            "V2 证据要求必须来自服务端 task_guide",
        )
        self.assertIn("if(result.length===8) break", normalizer)
        self.assertIn('if(!requirements.length||!panelId) return ""', checklist)
        self.assertIn("决策证据清单", checklist)
        self.assertIn("结果只能是 HOLD，不会编造数据", checklist)
        self.assertIn('max-height:min(42vh,330px)', checklist)
        self.assertIn('maxlength="${DECISION_EVIDENCE_ITEM_MAX_CHARS}"', checklist)
        self.assertIn("每项最多 ${DECISION_EVIDENCE_ITEM_MAX_CHARS} 字", checklist)
        self.assertIn("合计最多 ${DECISION_EVIDENCE_TOTAL_MAX_CHARS} 字（含来源名称）", checklist)
        self.assertIn('label for="${esc(fieldId)}"', checklist)
        self.assertIn('aria-describedby="${esc(statusId)} ${esc(policyId)}"', checklist)
        self.assertIn('aria-live="polite"', checklist)
        self.assertIn("decisionEvidencePanelKeydown", checklist)
        self.assertIn("esc(requirement.input_id)", checklist)
        self.assertIn("esc(requirement.label)", checklist)
        self.assertIn("esc(frozen.source_name)", checklist)
        self.assertIn("items.push({input_id,content})", collector)
        self.assertNotIn("evidence_id", collector)
        self.assertNotIn("manifest", collector)
        counter = function(self.source, "decisionEvidenceCountUpdate")
        self.assertIn('covered?"本轮已更新":"本轮已填写"', counter)
        self.assertIn("count.textContent!==nextCount", counter)
        limit = function(self.source, "decisionEvidenceItemsTooLong")
        self.assertIn("content.length>DECISION_EVIDENCE_ITEM_MAX_CHARS", limit)
        self.assertIn("total+=content.length+source_name.length", limit)
        self.assertIn("total>DECISION_EVIDENCE_TOTAL_MAX_CHARS", limit)
        self.assertIn('const source_name=String(item?.source_name||"")', limit)
        self.assertIn("const DECISION_EVIDENCE_ITEM_MAX_CHARS=4000", self.source)
        self.assertIn("const DECISION_EVIDENCE_TOTAL_MAX_CHARS=20000", self.source)
        restore = function(self.source, "restoreFormState")
        self.assertIn("decisionEvidenceCountUpdate(panel.id)", restore)

    def test_real_browser_suite_uses_bundled_playwright_when_system_chrome_is_absent(self):
        browser = (
            ROOT / "tests" / "test_frontend_browser_behavior.py"
        ).read_text(encoding="utf-8")
        self.assertIn("async def _launch_chromium(", browser)
        self.assertIn('options = {"headless": True}', browser)
        self.assertIn('options["executable_path"] = executable', browser)
        self.assertIn("playwright.chromium.launch(**options)", browser)
        self.assertNotIn(
            'raise unittest.SkipTest("no system Chromium executable available")',
            browser,
        )

    def test_v2_initial_task_collects_evidence_into_brief_and_idempotency_input(self):
        form = function(self.source, "specTaskTab")
        submit = function(self.source, "specSubmit")
        self.assertIn("decisionEvidenceChecklist(guide.evidence_requirements", form)
        self.assertIn("collectDecisionEvidenceItems(`spec-evidence-${Number(idx)}`)", submit)
        self.assertIn("brief.evidence_items=evidence_items", submit)
        self.assertIn("decisionEvidenceItemsTooLong(evidence_items)", submit)
        self.assertLess(
            submit.index("brief.evidence_items=evidence_items"),
            submit.index('persistentMutationRequestKey("initialtask"'),
        )

    def test_v2_followup_only_sends_current_round_evidence_updates(self):
        panel = function(self.source, "taskRevisionPanel")
        submit = function(self.source, "taskFollowup")
        self.assertIn("t.task_guide?.evidence_requirements", panel)
        self.assertIn("brief:t.brief", panel)
        self.assertIn("已冻结", self.source)
        self.assertIn("本轮可更新", self.source)
        self.assertIn("collectDecisionEvidenceItems(`follow-evidence-${Number(tid)}`)", submit)
        self.assertIn("requestInput.evidence_items=evidence_items", submit)
        self.assertIn("body.evidence_items=evidence_items", submit)
        self.assertIn("config_sha256", submit)
        self.assertIn("decisionEvidenceItemsTooLong(evidence_items)", submit)
        self.assertNotIn("decision_evidence", submit)
        self.assertNotIn("evidence_id", submit)

    def test_initial_employee_task_is_idempotent_and_thread_versions_hide_inline_edit(self):
        solo = function(self.source, "soloSubmit")
        specialist = function(self.source, "specSubmit")
        for submit in (solo, specialist):
            self.assertIn('persistentMutationRequestKey("initialtask"', submit)
            self.assertIn("request_key", submit)
            self.assertIn("clearPersistentMutationRequestKey", submit)
            self.assertIn("e.uncertain", submit)
        self.assertGreaterEqual(
            self.source.count('thread?.status==="standalone"'), 2
        )

    def test_inspection_request_key_includes_file_identity_and_selected_industry(self):
        submit = function(self.source, "inspectionSubmit")
        self.assertIn('persistentMutationRequestKey("inspection"', submit)
        self.assertIn("lastModified", submit)
        self.assertIn('form.append("industry_key"', submit)
        self.assertIn('form.append("request_key",requestKey)', submit)
        self.assertIn("clearPersistentMutationRequestKey", submit)

    def test_inspection_uses_canonical_shapes_statuses_and_cursor(self):
        view = function(self.source, "inspectionView")
        draw = function(self.source, "inspectionDraw")
        status = function(self.source, "inspectionStatusLabel")
        self.assertIn("industry_key", view)
        self.assertIn("before_id", view)
        self.assertIn("next_before_id", draw)
        self.assertIn("v.branch?.name", draw)
        self.assertIn("summary.branches", draw)
        for value in ("preparing", "analyzing", "completed", "failed"):
            self.assertIn(f'{value}:', status)
        self.assertIn("function inspectionSelectIndustry(", self.source)
        self.assertIn("function inspectionPage(", self.source)

    def test_inspection_cas_and_human_review_close_loop(self):
        action = function(self.source, "inspectionAction")
        assignment = function(self.source, "inspectionAssign")
        recheck = function(self.source, "inspectionRecheck")
        review = function(self.source, "inspectionReview")
        self.assertIn("expected_version", action)
        self.assertIn("expected_version", assignment)
        self.assertIn("/assignment", assignment)
        self.assertIn('form.append("expected_version"', recheck)
        self.assertIn("/review", review)
        self.assertIn("expected_action_version", review)
        self.assertIn('decision:"close"', review)
        self.assertIn('decision:"reject"', review)
        self.assertIn("isAdmin()", function(self.source, "inspectionIssueHtml"))
        self.assertIn('action.status==="awaiting_recheck"&&!pending', function(self.source, "inspectionIssueHtml"))

    def test_inspection_home_ranks_regions_and_stores_and_filters_history(self):
        view = function(self.source, "inspectionView")
        draw = function(self.source, "inspectionDraw")
        branch_filter = function(self.source, "inspectionFilterBranch")
        self.assertIn('listQuery.set("branch_id"', view)
        self.assertIn("summary.regions", draw)
        self.assertIn("风险优先门店", draw)
        self.assertIn("区域汇总", draw)
        self.assertIn("inspectionFilterBranch", draw)
        self.assertIn("INSPECTION_BRANCH_ID", branch_filter)
        self.assertIn("inspectionView", branch_filter)

    def test_inspection_deep_link_carries_industry_and_coverage_counts_only_visited_stores(self):
        render = function(self.source, "render")
        view = function(self.source, "inspectionView")
        draw = function(self.source, "inspectionDraw")
        self.assertIn("scopeArg", render)
        self.assertIn("rawIndustry", view)
        self.assertIn("decodeURIComponent", view)
        self.assertIn("visitedBranches", draw)
        self.assertIn("Number(b.visits||0)>0", draw)
        self.assertIn("function inspectionRecordHash(", self.source)

    def test_industry_task_post_success_cannot_be_reissued_when_detail_get_fails(self):
        submit = function(self.source, "specSubmit")
        self.assertIn("createdTaskId", submit)
        self.assertIn("openError", submit)
        self.assertIn("location.hash=`#/tasks/${createdTaskId}`", submit)
        self.assertIn("任务已创建", submit)

    def test_inspection_detail_compares_phases_and_uses_local_display_number(self):
        detail = function(self.source, "inspectionDetailHtml")
        issue = function(self.source, "inspectionIssueHtml")
        self.assertIn("p.display_no", detail)
        self.assertIn("整改前现场", detail)
        self.assertIn("整改后复查", detail)
        self.assertIn("操作时间线", detail)
        self.assertIn("e.display_no", issue)
        self.assertNotIn("e.photo_id", issue)
        self.assertIn("r.photos", issue)

    def test_inspection_employee_card_opens_photo_workbench_not_text_task(self):
        opener = function(self.source, "openEmp")
        modal = function(self.source, "drawModal")
        workbench = function(self.source, "inspectionEmployeeTab")
        self.assertIn('station?.key==="inspection"?"inspection"', opener)
        self.assertIn('isInspection?[["inspection"', modal)
        self.assertIn("#/inspections", workbench)
        self.assertIn("必须有当次门店照片", workbench)

    def test_new_store_uses_product_dialog_not_native_prompt(self):
        handler = function(self.source, "inspectionNewBranch")
        self.assertIn("uiPrompt(", handler)
        self.assertNotIn("isAdmin()", handler)
        self.assertNotRegex(self.source, r"(?<!ui)\bprompt\s*\(")

    def test_boss_metrics_keep_null_unavailable_and_employee_detail_pages(self):
        number = function(self.source, "bossDashNumber")
        percent = function(self.source, "bossDashPct")
        detail = function(self.source, "bossEmployeeDetail")
        detail_draw = function(self.source, "bossEmployeeDetailDraw")
        self.assertIn("value===null", number)
        self.assertIn("value===null", percent)
        self.assertIn("inspection_schema_unavailable", self.source)
        self.assertIn("offset", detail)
        self.assertIn("tasks.total", detail_draw)
        self.assertIn("visits.total", detail_draw)
        self.assertIn("bossEmployeeDetailPage", self.source)
        self.assertIn("最近交付", detail)
        self.assertIn("v.period_event_at||v.created_at", detail_draw)
        self.assertNotIn("v.updated_at?new Date", detail_draw)
        self.assertIn("function bossStatusClass(", self.source)

    def test_boss_history_rows_keep_frozen_identity_across_list_and_detail(self):
        dashboard = function(self.source, "bossDashboardDraw")
        detail = function(self.source, "bossEmployeeDetail")
        detail_page = function(self.source, "bossEmployeeDetailPage")
        production = function(self.source, "productionView")
        production_detail = function(self.source, "prodDetail")

        self.assertIn("employeeIdentityLabel(e)", dashboard)
        self.assertIn("e.config_revision", dashboard)
        self.assertIn("e.identity_ref", dashboard)
        self.assertIn('qs.set("identity_ref",identityRef)', detail)
        self.assertIn("BOSS_EMPLOYEE_REQUEST_SEQ", detail)
        self.assertIn("scopeToken", detail)
        self.assertIn("requestSeq!==BOSS_EMPLOYEE_REQUEST_SEQ", detail)
        self.assertIn("identityRef", detail_page)
        self.assertIn("employeeIdentityLabel(e)", production)
        self.assertIn("e.identity_ref", production)
        self.assertIn('qs.set("identity_ref",identityRef)', production_detail)

    def test_boss_summary_discards_late_responses_from_an_old_scope(self):
        view = function(self.source, "bossDashboardView")
        snapshot = function(self.source, "bossDashboardScopeSnapshot")
        guard = function(self.source, "bossDashboardRequestIsCurrent")

        self.assertIn("BOSS_DASH_REQUEST_SEQ", self.source)
        self.assertIn("tenantId", snapshot)
        self.assertIn("industryKey", snapshot)
        self.assertIn("days", snapshot)
        self.assertIn("request?.seq===BOSS_DASH_REQUEST_SEQ", guard)
        self.assertGreaterEqual(view.count("bossDashboardRequestIsCurrent(request)"), 2)
        self.assertLess(
            view.index("bossDashboardRequestIsCurrent(request)", view.index('await api("/boss/dashboard/scopes")')),
            view.index("BOSS_DASH_SCOPES=scopes"),
        )
        self.assertLess(
            view.rindex("bossDashboardRequestIsCurrent(request)"),
            view.index("BOSS_DASH_DATA=summary"),
        )

    def test_production_task_output_opens_the_canonical_task_detail(self):
        detail = function(self.source, "prodDetail")
        self.assertIn('href="#/tasks/${it.id}"', detail)
        self.assertNotIn('href="#/">🎯专家任务', detail)

    def test_task_center_and_task_detail_label_frozen_assignment_state(self):
        state = function(self.source, "employeeAssignmentState")
        row = function(self.source, "tcRow")
        detail = function(self.source, "taskDetailView")

        self.assertIn("employeeIdentityState(employee)", state)
        self.assertIn("employeeCanAssignNew(employee)", state)
        self.assertIn("employeeIdentityLabel(employee)", state)
        self.assertIn("员工仍在岗 · 此任务使用历史岗位版本", self.source)
        self.assertIn("employeeAssignmentState(x)", row)
        self.assertIn("employeeAssignmentState(t)", detail)

    def test_legacy_employee_modal_is_history_only(self):
        opener = function(self.source, "openSpec")
        draw = function(self.source, "drawSpec")
        caps = function(self.source, "specCapsTab")
        skills = function(self.source, "specSkillsTab")
        learning = function(self.source, "employeeLearningPanel")
        self.assertIn("employeeCanAssignNew(SPEC)", opener)
        self.assertIn("employeeCanAssignNew(e)", draw)
        self.assertIn("!employeeCanAssignNew(e)", caps)
        self.assertIn("!employeeCanAssignNew(e)", skills)
        self.assertIn("员工仍在岗 · 此任务使用历史岗位版本", self.source)
        self.assertIn('canAssign?[["task","📋 派活"]]:[]', draw)
        self.assertIn('<span class="tag readonly">', caps)
        self.assertIn('<span class="tag readonly">', learning)
        self.assertIn('readonly?`<span class="tag readonly">', caps)
        self.assertIn('readonly?`<span class="tag readonly">', learning)

    def test_specialist_skills_tab_shows_learned_skills_and_factory_profile(self):
        skills = function(self.source, "specSkillsTab")
        # 餐饮等 V1 专家的已掌握技能卡必须留在「技能库」tab，可启用/停用/删除
        self.assertIn("e.skills", skills)
        self.assertIn("已掌握技能", skills)
        self.assertIn("specToggleSkill", skills)
        self.assertIn("specDelSkill", skills)
        # V4 行业专家的岗位出厂能力（技能树/能力/知识域/工作方式）必须在「技能库」tab 完整可见，
        # 且明确标注为出厂基线，不冒充网学技能。
        self.assertIn("professional_profile", skills)
        self.assertIn("岗位出厂能力", skills)
        self.assertIn("employeeProfessionalProfile(e,{readonly})", skills)
        # 老板明确要求去掉证据研究面板（evidence_insufficient / 发起证据研究 /
        # 真实来源 / 待审核变化），技能库 tab 不得再渲染它，并保留空态兜底。
        self.assertNotIn("employeeLearningPanel", skills)
        self.assertIn("该岗位暂无技能档案", skills)
        # 出厂档案渲染函数必须覆盖技能树与核心能力两组，360 的真实技能才不会只剩名字
        profile = function(self.source, "employeeProfessionalProfile")
        self.assertIn('"skill_tree"', profile)
        self.assertIn('"capabilities"', profile)
        self.assertIn("capability_details", profile)

    def test_skill_tree_and_capability_rows_expand_with_detail_text(self):
        rows = function(self.source, "employeeProfileRows")
        # 技能树/核心能力条目必须可点开并展示详细介绍（capability_details），
        # 没有详情时退回纯文本条目，绝不渲染空壳。
        for marker in (
            '"skill_tree"', '"capabilities"', "detailMaps",
            "<details>", "点开看介绍",
        ):
            self.assertIn(marker, rows)
        self.assertIn("return `<li>${esc(item)}</li>`", rows)
        profile = function(self.source, "employeeProfessionalProfile")
        self.assertIn("employee.capability_details", profile)
        # 技能树/核心能力两组必须排最前并默认展开，老板一眼看到详情入口
        self.assertLess(
            profile.index('["skill_tree"'), profile.index('["knowledge_domains"'),
        )
        # 「能力」tab 的能力卡优先展示 detail 详细介绍；desc 与名字相同的
        # 裸标题不再重复渲染
        caps = function(self.source, "specCapsTab")
        self.assertIn("c.detail", caps)
        self.assertIn("c.desc&&c.desc!==c.name", caps)

    def test_disabled_employee_cards_and_panel_are_explicitly_read_only(self):
        card = function(self.source, "specRoomCard")
        opener = function(self.source, "openSpec")
        draw = function(self.source, "drawSpec")

        self.assertIn("employeeCanAssignNew(e)", card)
        self.assertIn("员工暂未在岗 · 当前岗位", self.source)
        self.assertIn("e.enabled===false", card)
        self.assertIn("enabled", opener)
        self.assertIn("employeeCanAssignNew(e)", draw)

    def test_meeting_task_link_is_employee_independent_and_members_show_roster(self):
        opener = function(self.source, "mtOpenTask")
        meetings = function(self.source, "meetingsView")
        member = function(self.source, "meetingMemberLabel")

        self.assertIn("location.hash=`#/tasks/${taskId}`", opener)
        self.assertNotIn("openSpec", opener)
        self.assertNotIn("MODAL_IDX", opener)
        self.assertNotIn("api(", opener)
        self.assertIn("meetingMemberLabel(b)", meetings)
        self.assertIn("employeeIdentityState(member)", member)
        self.assertIn("employeeIdentityLabel(member)", member)
        self.assertIn("员工仍在岗 · 此任务使用历史岗位版本", self.source)
        self.assertIn("employeeCanAssignNew(e)", meetings)

    def test_old_production_dashboard_labels_frozen_identity_count(self):
        production = function(self.source, "productionView")
        self.assertIn('"产出身份",t.employees', production)
        self.assertNotIn('"在产员工",t.employees', production)

    def test_boss_recent_activity_is_structural_safe_and_status_mapped(self):
        draw = function(self.source, "bossDashboardDraw")
        self.assertIn("recent_activity", draw)
        self.assertIn("最近交付 / 动态", draw)
        self.assertIn("safeRouteUrl(item.target_route)", draw)
        self.assertIn("bossStatusClass(item.status_group)", draw)
        self.assertIn("item.revision_no", draw)
        self.assertIn("d.can_open_records", draw)
        self.assertIn("跨企业看板仅供查看", draw)
        self.assertIn("inspectionRecordHash(0,scope.industry_key)", draw)
        self.assertNotIn("output_md", draw)

    def test_all_three_employee_entry_points_keep_the_multi_round_panel(self):
        # 任务详情、单员工弹层、行业员工工位都能进入下一轮并最终确认满意。
        self.assertGreaterEqual(self.source.count("taskRevisionPanel("), 4)
        panel = function(self.source, "taskRevisionPanel")
        self.assertIn("生成第 ${revision+1} 轮", panel)
        self.assertIn("满意，结束", panel)
        opener = function(self.source, "taskOpenRevision")
        self.assertIn('mode==="spec"', opener)
        self.assertIn('mode==="solo"', opener)

    def test_revision_panel_keeps_accept_when_employee_is_disabled_and_recovers_failed_leaf(self):
        panel = function(self.source, "taskRevisionPanel")
        self.assertIn("thread.can_accept", panel)
        self.assertIn("employee_disabled", panel)
        self.assertIn("resume_task_id", panel)
        self.assertIn("failed_current_task_id", panel)
        self.assertNotIn(
            'if(!thread.can_continue) return',
            panel,
        )
        # 任务详情、内容员工和行业员工的失败卡都要挂载恢复面板。
        self.assertGreaterEqual(
            self.source.count('taskRevisionPanel(t,"'),
            3,
        )


if __name__ == "__main__":
    unittest.main()
