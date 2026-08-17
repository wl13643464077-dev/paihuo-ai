"""Schema 52 巡店前端合同。"""

from pathlib import Path
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


class V52FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def test_branch_import_is_a_collapsible_top_level_action(self):
        action = function(self.source, "inspectionImportActionHtml")
        toggle = function(self.source, "inspectionImportToggle")
        draw = function(self.source, "inspectionImportRender")
        workbench = function(self.source, "inspectionDraw")

        self.assertEqual(1, self.source.count("function inspectionImportRender("))
        self.assertIn("let INSPECTION_IMPORT_OPEN=false", self.source)
        self.assertIn('aria-controls="inspection-import-workbench"', action)
        self.assertIn('aria-expanded="${INSPECTION_IMPORT_OPEN?"true":"false"}"', action)
        self.assertIn("批量导入门店", action)
        self.assertIn("INSPECTION_IMPORT_OPEN=", toggle)
        self.assertIn("requestAnimationFrame", toggle)
        self.assertIn("scrollIntoView", toggle)
        self.assertIn("focus", toggle)
        self.assertNotIn("inspectionDraw()", toggle)
        self.assertIn("panel.hidden=!next", toggle)
        self.assertIn('classList.toggle("is-open",next)', toggle)
        self.assertIn('setAttribute("aria-expanded"', toggle)
        self.assertIn("label.textContent=", toggle)
        self.assertNotIn('if(!INSPECTION_IMPORT_OPEN)return ""', draw)
        self.assertIn('${INSPECTION_IMPORT_OPEN?"":"hidden"}', draw)
        self.assertIn('id="inspection-import-workbench"', draw)
        self.assertIn("inspectionImportActionHtml()", workbench)
        self.assertLess(
            workbench.index("inspectionImportActionHtml()"),
            workbench.index("inspectionImportRender()"),
        )

    def test_branch_import_uses_compact_responsive_classes_and_native_file_control(self):
        draw = function(self.source, "inspectionImportRender")
        file_changed = function(self.source, "inspectionImportFileChanged")

        for class_name in (
            "inspection-import-workbench",
            "inspection-import-context-row",
            "inspection-import-context-title",
            "inspection-import-industry",
            "inspection-import-layout",
            "inspection-import-template",
            "inspection-import-upload",
            "inspection-import-file",
            "inspection-import-result",
        ):
            self.assertIn(class_name, draw)
            self.assertIn(f".{class_name}", self.index_html)
        self.assertIn("::file-selector-button", self.index_html)
        self.assertIn("@media (prefers-reduced-motion:reduce)", self.index_html)
        self.assertIn("focus-visible", self.index_html)
        self.assertIn('aria-live="polite"', draw)
        self.assertIn('aria-describedby="inspection-import-file-help inspection-import-file-name"', draw)
        self.assertIn("inspectionImportFileChanged(this)", draw)
        self.assertIn("textContent", file_changed)
        self.assertIn("disabled", file_changed)
        self.assertNotIn('style="', draw)
        self.assertNotIn('<header class="inspection-import-head"', draw)
        self.assertNotIn("inspection-import-steps", draw)
        self.assertNotIn("inspection-import-close", draw)
        self.assertIn('${canImport?`<div class="inspection-import-layout">', draw)
        self.assertIn('aria-current="step"', draw)
        self.assertIn('aria-pressed="', draw)
        self.assertIn('role="alert"', draw)

        layout_rule = self.index_html.split(
            ".inspection-import-layout{", 1
        )[1].split("}", 1)[0]
        self.assertIn("35fr", layout_rule)
        self.assertIn("65fr", layout_rule)
        template_rule = self.index_html.split(
            ".inspection-import-template{", 1
        )[1].split("}", 1)[0]
        upload_rule = self.index_html.split(
            ".inspection-import-upload{", 1
        )[1].split("}", 1)[0]
        self.assertIn("min-height:178px", template_rule)
        self.assertIn("min-height:178px", upload_rule)
        upload_button_rule = self.index_html.split(
            ".inspection-import-upload-button{", 1
        )[1].split("}", 1)[0]
        self.assertIn("align-self:end", upload_button_rule)
        self.assertIn("height:46px", upload_button_rule)
        self.assertNotIn("align-self:stretch", upload_button_rule)

    def test_branch_import_is_a_three_step_preview_commit_workflow(self):
        draw = function(self.source, "inspectionImportRender")
        upload = function(self.source, "inspectionImportUpload")
        commit = function(self.source, "inspectionImportCommit")
        self.assertIn("下载Excel模板", draw)
        self.assertIn("批量导入门店", draw)
        for step in ("①", "②", "③"):
            self.assertIn(step, draw)
        self.assertNotIn("④", draw)
        self.assertIn("industry.name", draw)
        self.assertIn("industry.emoji", draw)
        self.assertIn("/inspections/branches/import-template", draw)
        self.assertIn("/inspections/branches/imports", upload)
        self.assertIn('form.append("industry_key"', upload)
        self.assertIn('form.append("request_key"', upload)
        self.assertIn('form.append("file"', upload)
        self.assertIn('counts.error', draw)
        self.assertIn('row.error_message||row.error_code', draw)
        self.assertIn('counts.error>0', draw)
        self.assertIn('/commit', commit)
        self.assertIn('expected_version', commit)
        self.assertIn('await inspectionView()', commit)
        self.assertIn("meta.permissions?.can_import_branches", draw)
        self.assertIn("isAdmin()", draw)

    def test_branch_import_preview_shows_business_counts_and_safe_rows(self):
        draw = function(self.source, "inspectionImportRender")
        load = function(self.source, "inspectionImportLoadPage")
        page = function(self.source, "inspectionImportPage")
        upload = function(self.source, "inspectionImportUpload")
        self.assertIn("preview.business_counts", draw)
        self.assertIn('row.row_kind==="business"', draw)
        for field in (
            "data.metric_key",
            "data.period_start",
            "data.period_end",
            "data.value",
            "data.unit",
            "data.source_ref",
        ):
            self.assertIn(f"esc({field}", draw)
        self.assertIn("经营数据预检", draw)
        self.assertIn("businessCounts.error", draw)
        self.assertIn("totalErrors", draw)
        for label in ("全部行", "仅门店", "仅经营数据", "仅错误"):
            self.assertIn(label, draw)
        self.assertIn("上一页", draw)
        self.assertIn("下一页", draw)
        self.assertIn(".slice(0,50)", draw)
        self.assertIn("preview.filtered_total_rows", draw)
        self.assertIn("preview.limit", draw)
        self.assertIn('limit:"50"', load)
        self.assertIn("row_kind", load)
        self.assertIn("errors_only", load)
        self.assertIn('query.set("cursor",cursor)', load)
        self.assertNotIn("Number(cursor)", load)
        self.assertIn("result.next_cursor", load)
        self.assertIn("nextCursor", page)
        self.assertIn("cursorStack.push(nextCursor)", page)
        self.assertNotIn("Number(page.nextCursor)", page)
        self.assertNotIn("inspectionImportLoadRows", self.source)
        self.assertIn("INSPECTION_IMPORT_PAGE", upload)

    def test_branch_import_upload_declares_and_enforces_capacity(self):
        draw = function(self.source, "inspectionImportRender")
        upload = function(self.source, "inspectionImportUpload")
        self.assertIn("16MB", draw)
        self.assertIn("2万家门店", draw)
        self.assertIn("4万行经营数据", draw)
        self.assertIn("16*1024*1024", upload)
        self.assertIn("文件不能超过 16MB", upload)

    def test_import_conflict_or_expiry_rotates_key_only_for_confirmed_409(self):
        request = function(self.source, "apiRequest")
        upload_transport = function(self.source, "apiUpload")
        import_upload = function(self.source, "inspectionImportUpload")
        commit = function(self.source, "inspectionImportCommit")
        for body in (request, upload_transport):
            self.assertIn('headers.get("X-Paihuo-Error-Code")', body)
            self.assertIn("err.code", body)
        self.assertIn(
            'e?.status===409&&e?.code==="IMPORT_PREVIEW_EXPIRED"',
            import_upload,
        )
        self.assertIn(
            '["IMPORT_STATE_CONFLICT","IMPORT_PREVIEW_EXPIRED"].includes(e?.code)',
            commit,
        )
        self.assertIn("clearPersistentMutationRequestKey", commit)
        self.assertIn("preview:null", commit)
        self.assertIn("重新上传", commit)
        conflict = commit.index('["IMPORT_STATE_CONFLICT","IMPORT_PREVIEW_EXPIRED"]')
        self.assertIn("clearPersistentMutationRequestKey", commit[conflict:])

    def test_import_async_results_are_guarded_by_captured_scope_generation(self):
        guard = function(self.source, "inspectionImportScopeIsCurrent")
        load = function(self.source, "inspectionImportLoadPage")
        poll = function(self.source, "inspectionImportPoll")
        upload = function(self.source, "inspectionImportUpload")
        commit = function(self.source, "inspectionImportCommit")

        for token in ("INSPECTION_INDUSTRY", "requestKey", "importId"):
            self.assertIn(token, guard)
        self.assertIn("inspectionImportScopeIsCurrent", load)
        self.assertIn("if(!isCurrent())return", load)
        self.assertIn("if(isCurrent()){page.loading=false;inspectionDraw();}", load)
        self.assertIn("industryKey=INSPECTION_INDUSTRY", poll)
        self.assertIn("capturedImportId", poll)
        self.assertGreaterEqual(poll.count("if(!isCurrent())return null"), 3)
        self.assertLess(
            poll.index("if(!isCurrent()", poll.index("await api(")),
            poll.index("INSPECTION_IMPORT.preview=result"),
        )
        self.assertIn("await inspectionFileFingerprint(file)", upload)
        self.assertIn("if(String(INSPECTION_INDUSTRY)!==industry_key)return", upload)
        self.assertIn(
            "inspectionImportPoll(importId,{industryKey:industry_key,requestKey})",
            upload,
        )
        self.assertGreaterEqual(
            upload.count("inspectionImportScopeIsCurrent(industry_key"), 3
        )
        for token in (
            "importId=Number(preview.import_id)",
            "industry_key=String(INSPECTION_INDUSTRY",
            "requestKey=String(INSPECTION_IMPORT.requestKey",
        ):
            self.assertIn(token, commit)
        self.assertIn("if(!isCurrent())return", commit)
        self.assertIn(
            'clearPersistentMutationRequestKey("inspectionbranchimport",industry_key,requestKey)',
            commit,
        )
        self.assertNotIn(
            'clearPersistentMutationRequestKey("inspectionbranchimport",INSPECTION_INDUSTRY',
            commit,
        )

    def test_branch_import_key_uses_account_industry_and_file_digest(self):
        digest = function(self.source, "inspectionFileFingerprint")
        upload = function(self.source, "inspectionImportUpload")
        self.assertIn("crypto.subtle.digest", digest)
        self.assertIn("file.arrayBuffer", digest)
        self.assertIn('persistentMutationRequestKey("inspectionbranchimport"', upload)
        self.assertIn('persistentMutationStorageKey("inspectionbranchimport"', upload)
        self.assertIn("localStorage", upload)
        self.assertIn("file_sha256", upload)
        self.assertIn("industry_key", upload)
        self.assertIn("fingerprint", upload)
        self.assertIn("e.uncertain", upload)
        expired = upload.index('e?.code==="IMPORT_PREVIEW_EXPIRED"')
        uncertain = upload.index("e.uncertain")
        self.assertIn("clearPersistentMutationRequestKey", upload[expired:uncertain])
        self.assertNotIn(
            "clearPersistentMutationRequestKey",
            upload[uncertain:upload.index("inspectionDraw()", uncertain)],
        )

    def test_branch_picker_uses_bounded_server_search_with_legacy_fallback(self):
        search = function(self.source, "inspectionBranchSearch")
        picker = function(self.source, "inspectionBranchPickerHtml")
        self.assertIn("/inspections/branches/search", search)
        for field in ('query.set("q"', 'query.set("region"', 'query.set("limit"', 'query.set("before_id"'):
            self.assertIn(field, search)
        self.assertIn("meta.branches", search)
        self.assertIn("门店编号 / 名称", picker)
        self.assertIn("区域筛选", picker)
        self.assertIn("inspectionBranchSearchPage", picker)

    def test_checklist_is_versioned_layered_and_has_ordered_capture_slots(self):
        load = function(self.source, "inspectionChecklistLoad")
        draw = function(self.source, "inspectionChecklistHtml")
        submit = function(self.source, "inspectionSubmit")
        self.assertIn("/inspections/checklist", load)
        self.assertIn("branch_id", load)
        self.assertIn("catalog_version", draw)
        for tier in ("mandatory", "recommended", "operations"):
            self.assertIn(tier, draw)
        self.assertIn("capture_slots", draw)
        self.assertIn("仅在所列适用条件与管辖范围内必查", draw)
        self.assertIn("不是法定阈值", draw)
        self.assertIn("inspectionStandardMetaHtml(item)", draw)
        self.assertIn("inspectionObservationControlHtml(item)", draw)
        self.assertIn("condition", self.source)
        self.assertIn("jurisdiction", self.source)
        self.assertIn("source_no", self.source)
        self.assertIn("safeExternalUrl(item?.source_url)", self.source)
        self.assertIn("shot_guide", draw)
        self.assertIn("必拍覆盖", draw)
        self.assertIn('form.append("files"', submit)
        self.assertIn('form.append("file_slots"', submit)
        self.assertIn('form.append("template_version"', submit)
        self.assertIn('form.append("observations_json"', submit)
        self.assertNotIn('scope=JSON.stringify', submit)
        observations = function(self.source, "inspectionObservations")
        self.assertIn("{metric_code:input.dataset.observationCode,value:Number(input.value)", observations)
        self.assertIn('input.dataset.observationType==="boolean"', observations)
        controls = function(self.source, "inspectionObservationControlHtml")
        self.assertIn('type==="boolean"', controls)
        self.assertIn('<option value="true">', controls)
        self.assertIn('<option value="false">', controls)
        self.assertIn('type==="document"', controls)
        self.assertIn("{metrics,checklist}", observations)

    def test_operating_metrics_keep_null_explicit_and_all_comparisons_visible(self):
        metrics = function(self.source, "inspectionMetricsHtml")
        value = function(self.source, "inspectionMetricValue")
        self.assertIn("实际", metrics)
        self.assertIn("目标", metrics)
        self.assertIn("上期", metrics)
        self.assertIn("同期", metrics)
        self.assertIn("基准", metrics)
        self.assertIn("待接入", value)
        for field in ("actual", "target", "previous", "same_year", "benchmark"):
            self.assertIn(field, metrics)
        self.assertIn("previous_period", metrics)
        self.assertIn("same_period_last_year", metrics)

    def test_inspection_detail_shows_recorded_data_and_capture_slot_labels(self):
        detail = function(self.source, "inspectionDetailHtml")
        recorded = function(self.source, "inspectionRecordedDataHtml")
        present = function(self.source, "inspectionHasRecordedValue")
        display = function(self.source, "inspectionObservationValue")

        self.assertIn("detail.standard_snapshot", recorded)
        self.assertIn("detail.observations", recorded)
        self.assertIn("snapshot.metrics", recorded)
        self.assertIn("snapshot.items", recorded)
        self.assertIn("本次经营与人员数据", recorded)
        self.assertIn("人工/系统观察记录", recorded)
        self.assertIn("metric.label", recorded)
        self.assertIn("row.value", recorded)
        self.assertIn("row.unit", recorded)
        self.assertIn("item.label", recorded)
        self.assertGreaterEqual(recorded.count("esc("), 4)
        self.assertIn("value!==null", present)
        self.assertIn("value!==undefined", present)
        self.assertIn('value!==""', present)
        self.assertNotIn("||0", recorded)
        self.assertIn("typeof value===\"boolean\"", display)

        self.assertIn("detail.template_version", recorded)
        self.assertIn("snapshot.as_of", recorded)
        self.assertIn("inspectionRecordedDataHtml(v)", detail)
        self.assertIn("snapshot.capture_slots", detail)
        self.assertIn("p.capture_slot", detail)
        self.assertIn("slotLabels", detail)
        self.assertIn("slot.label||slot.slot_code", detail)
        self.assertIn("采集位：", detail)

    def test_workbench_coverage_prefers_server_total_with_legacy_fallback(self):
        draw = function(self.source, "inspectionDraw")
        self.assertIn("summary.visited_branches", draw)
        self.assertIn("summary.visited_branches!==null", draw)
        self.assertIn("summary.visited_branches!==undefined", draw)
        self.assertIn("branchMetrics.filter", draw)
        self.assertNotIn("summary.visited_branches||", draw)
        self.assertIn('["覆盖门店",bossDashNumber(visitedBranches)]', draw)

    def test_workbench_has_complete_async_states_and_mobile_safe_layout(self):
        picker = function(self.source, "inspectionBranchPickerHtml")
        checklist = function(self.source, "inspectionChecklistHtml")
        importer = function(self.source, "inspectionImportRender")
        combined = picker + checklist + importer
        for state in ("正在加载", "暂无", "加载失败", "无权限"):
            self.assertIn(state, combined)
        self.assertIn("minmax(0,1fr)", self.source)
        self.assertIn("overflow-wrap:anywhere", self.source)

    def test_industry_deep_link_resets_every_history_scope(self):
        reset = function(self.source, "inspectionResetScope")
        view = function(self.source, "inspectionView")
        select = function(self.source, "inspectionSelectIndustry")
        carry = function(self.source, "bossCarryInspectionIndustry")

        for token in (
            "INSPECTION_BRANCH_ID=0",
            "INSPECTION_REGION=null",
            "INSPECTION_CURSOR_STACK=[null]",
            "INSPECTION_CURSOR_PAGE=0",
            'region:""',
            "cursorStack:[null]",
        ):
            self.assertIn(token, reset)
        self.assertIn("routeIndustry!==INSPECTION_INDUSTRY", view)
        self.assertIn("inspectionResetScope(routeIndustry)", view)
        self.assertIn("inspectionResetScope(selected)", select)
        self.assertIn("inspectionResetScope(selected)", carry)

    def test_detail_loading_is_not_coupled_to_history_list(self):
        view = function(self.source, "inspectionView")
        self.assertIn("if(id)", view)
        self.assertIn("await api(`/inspections/${id}?${detailQuery}`)", view)
        self.assertNotIn("Promise.all", view)
        self.assertIn("e?.status===404", view)
        self.assertIn("INSPECTION_BRANCH_ID=0", view)

    def test_branch_search_results_offer_capture_and_history_actions(self):
        picker = function(self.source, "inspectionBranchPickerHtml")
        self.assertIn("搜索结果", picker)
        self.assertIn("inspectionBranchSelect", picker)
        self.assertIn("选择巡店", picker)
        self.assertIn("inspectionFilterBranch", picker)
        self.assertIn("看历史", picker)

    def test_region_summary_drills_into_server_filtered_history(self):
        view = function(self.source, "inspectionView")
        draw = function(self.source, "inspectionDraw")
        filter_region = function(self.source, "inspectionFilterRegion")
        self.assertIn('listQuery.set("region"', view)
        self.assertIn("inspectionFilterRegion", draw)
        self.assertIn("看记录", draw)
        self.assertIn("INSPECTION_BRANCH_ID=0", filter_region)
        self.assertIn("INSPECTION_REGION=", filter_region)
        self.assertIn("INSPECTION_CURSOR_STACK=[null]", filter_region)

    def test_boss_inspection_links_keep_industry_in_the_hash(self):
        scoped = function(self.source, "inspectionScopedRoute")
        draw = function(self.source, "bossDashboardDraw")
        detail = function(self.source, "bossEmployeeDetailDraw")
        self.assertIn("inspectionRecordHash", scoped)
        self.assertIn("industry", scoped)
        self.assertIn("inspectionScopedRoute", draw)
        self.assertIn("inspectionScopedRoute", detail)


if __name__ == "__main__":
    unittest.main()
