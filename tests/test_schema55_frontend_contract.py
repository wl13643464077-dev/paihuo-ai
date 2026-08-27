"""Schema 55 UI contract: real V4 person and auditable learning state."""

from pathlib import Path
import json
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


def function(source: str, name: str) -> str:
    markers = (f"function {name}(", f"async function {name}(")
    starts = [source.find(marker) for marker in markers]
    starts = [position for position in starts if position >= 0]
    if not starts:
        raise AssertionError(f"missing JavaScript function: {name}")
    start = min(starts)
    candidates = [
        position for position in (
            source.find("\nfunction ", start + 1),
            source.find("\nasync function ", start + 1),
        ) if position >= 0
    ]
    return source[start:min(candidates) if candidates else len(source)]


def optional_function(source: str, name: str) -> str:
    try:
        return function(source, name)
    except AssertionError:
        return ""


class Schema55FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_employee_cards_treat_person_as_name_and_job_as_role(self):
        display = function(self.source, "employeeDisplayIdentity")
        card = function(self.source, "specRoomCard")
        detail = function(self.source, "drawSpec")
        self.assertIn("person", display)
        self.assertIn("name", display)
        self.assertIn("employeeDisplayIdentity", card)
        self.assertIn("employeeDisplayIdentity", detail)
        self.assertNotIn("person||e.name", card.replace(" ", ""))

    def test_ui_confirmation_stays_above_redrawn_batch_dialog(self):
        dialog = function(self.source, "openUiDialog")
        manager = function(self.source, "drawEmployeeLearningBatchManager")
        self.assertIn('class="overlay ui-dialog-overlay"', dialog)
        self.assertIn('style="z-index:1000"', dialog)
        self.assertIn('id="employee-learning-batch-dialog"', manager)

    def test_learning_ui_distinguishes_baseline_proposal_and_activated(self):
        panel = function(self.source, "employeeLearningPanel")
        for text in (
            "尚未完成全网进修",
            "真实来源",
            "待审核",
            "已激活",
            "知识",
            "技能",
            "能力",
            "工作流",
        ):
            self.assertIn(text, panel)
        self.assertIn("source_url", panel)
        self.assertIn("source.url", panel)
        self.assertIn("run_id", panel)
        self.assertIn("employeeRejectLearning", panel)
        self.assertIn("拒绝提案", panel)
        self.assertIn("hasReviewBinding", panel)
        self.assertIn('hasReviewBinding?"":"disabled"', panel)

    def test_learning_mutations_keep_identity_config_and_bundle_cas(self):
        start = function(self.source, "employeeStartLearning")
        approve = function(self.source, "employeeApproveLearning")
        reject = function(self.source, "employeeRejectLearning")
        refresh = function(self.source, "employeeLearningRefreshReviewViews")
        for block in (start, approve, reject):
            self.assertIn("identity_ref", block)
            self.assertIn("config_revision", block)
            self.assertIn("config_sha256", block)
            self.assertIn("bundle_sha256", block)
        self.assertIn("request_key", start)
        self.assertIn("persistentMutationRequestKey", start)
        self.assertIn("result?.run", start)
        self.assertIn("/employee-learning/runs/${Number(runId)}/reject", reject)
        self.assertIn("reason", reject)
        self.assertIn("employeeLearningRefreshReviewViews", approve)
        self.assertIn("employeeLearningRefreshReviewViews", reject)
        self.assertIn("refreshEmployeeLearningBatch", refresh)
        self.assertIn('api(`/depts/emp/${resolvedEmployeeIdx}`)', refresh)

    def test_admin_uses_v4_person_and_catalog_to_route_learning(self):
        detail = function(self.source, "admDetail")
        learn = function(self.source, "admLearn")
        panel = function(self.source, "employeeLearningPanel")
        self.assertIn("d.person||d.name", detail)
        self.assertIn('d.catalog_version==="2026.08.v4"', detail)
        self.assertIn('bindingScope:"admin"', detail)
        self.assertIn('bindingScope==="admin"?"window.__ADM_DETAIL":"SPEC"', panel)
        self.assertIn("employeeStartLearning", panel)
        self.assertIn("employeeApproveLearning", panel)
        self.assertIn("修理厂", panel)
        self.assertIn('employee?.catalog_version==="2026.08.v4"', learn)
        self.assertIn("employeeStartLearning", learn)

    def test_batch_manager_lists_frozen_pending_runs_and_opens_review_detail(self):
        review_list = function(self.source, "employeeLearningBatchReviewList")
        manager = function(self.source, "drawEmployeeLearningBatchManager")
        load = function(self.source, "loadEmployeeLearningBatches")
        open_review = function(self.source, "openEmployeeLearningBatchReview")
        open_spec = function(self.source, "openSpec")
        for value in (
            "awaiting_approval", "employee_idx", "run.person", "run.name",
            "config_revision", "打开详情审核", "拒绝提案",
        ):
            self.assertIn(value, review_list)
        for block in (review_list, manager):
            self.assertIn("bossDashNumber(", block)
            self.assertNotIn("fmtN(", block)
        self.assertIn("toggleEmployeeLearningBatchReviews", manager)
        self.assertIn("employeeLearningBatchReviewList", manager)
        self.assertIn("修理厂", manager)
        dashboard = function(self.source, "dashboard")
        self.assertIn("修理厂", dashboard)
        self.assertIn("openEmployeeLearningBatchManager", dashboard)
        self.assertIn("employeeRejectLearning", review_list)
        self.assertIn("'batch'", review_list)
        self.assertIn("/employee-learning/batches/${Number(batch.id)}", load)
        self.assertIn("closeEmployeeLearningBatchManager", open_review)
        self.assertIn('openSpec(Number(idx),"skills")', open_review)
        self.assertIn('initialTab==="skills"?"skills"', open_spec)

    def test_batch_execute_has_an_independent_complete_ui_snapshot_contract(self):
        execute = function(self.source, "executeEmployeeLearningBatch")
        snapshot = function(self.source, "employeeLearningBatchExecuteSnapshot")
        current = function(self.source, "employeeLearningBatchExecuteIsCurrent")
        for value in (
            "scope", "maxConcurrency", "requestKey", "previewToken",
            "targetDigest",
        ):
            self.assertIn(value, snapshot)
            self.assertIn(value, current)
        self.assertIn("EMPLOYEE_LEARNING_BATCH_EXECUTE_SEQ", execute)
        self.assertIn("employeeLearningBatchExecuteIsCurrent", execute)
        self.assertIn("snapshot.scope,snapshot.requestKey", execute)

    def test_batch_preview_renders_frozen_targets_then_executes_exact_confirmation(self):
        preview = {
            "target_count": 360,
            "budget_cap_points": 1080,
            "max_concurrency": 2,
            "billing_mode": "platform_included",
            "wallet_charge_points": 0,
            "points_per_employee": 3,
            "preview_token": "e" * 64,
            "target_digest": "d" * 64,
            "industry_counts": {
                "auto": 36, "beauty": 36, "convenience": 36,
                "fitness": 36, "grocery": 36, "hotel": 36,
                "pet": 36, "pharmacy": 36, "snack": 36,
                "tea_coffee": 36,
            },
            "target_sample": [{
                "idx": 1001,
                "person": "林清越",
                "name": "茶咖门店增长策略师",
                "industry_key": "tea_coffee",
                "identity_ref": "a" * 64,
                "config_revision": 7,
                "config_sha256": "b" * 64,
                "bundle_sha256": "c" * 64,
            }],
        }
        script = (
            '"use strict";\n'
            'const $=()=>null;\n'
            'const esc=value=>String(value??"");\n'
            'let rendered="";\n'
            'const document={body:{insertAdjacentHTML:(_position,html)=>{rendered=html;}}};\n'
            'const EMPLOYEE_LEARNING_INDUSTRIES=[["all_v4","全部 V4 行业专属员工（360 人）"],["auto","汽车后市场"],["beauty","美容美业"],["convenience","便利店"],["fitness","健身瑜伽"],["grocery","商超零售"],["hotel","酒店住宿"],["pet","宠物服务"],["pharmacy","零售药房"],["snack","量贩零食"],["tea_coffee","茶咖现制"]];\n'
            'let EMPLOYEE_LEARNING_BATCH={scope:"all_v4",maxConcurrency:2,requestKey:"",loading:false,error:"",batches:[],preview:null};\n'
            'let EMPLOYEE_LEARNING_BATCH_PREVIEW_SEQ=0,EMPLOYEE_LEARNING_BATCH_EXECUTE_SEQ=0;\n'
            'const persistentMutationRequestKey=()=>"schema55-ui-preview-001";\n'
            'const clearPersistentMutationRequestKey=()=>{};\n'
            'const toast=()=>{};\n'
            'const loadEmployeeLearningBatches=async()=>{};\n'
            'let createCall=null,confirmSawPreview=false;\n'
            'const uiConfirm=async()=>{confirmSawPreview=rendered.includes("冻结目标样本")&&rendered.includes("林清越")&&rendered.includes("茶咖门店增长策略师")&&rendered.includes("配置 r7");return true;};\n'
            f'const previewFixture={json.dumps(preview, ensure_ascii=False)};\n'
            'const api=async(path,options={})=>{if(path.endsWith("/dry-run"))return {preview:previewFixture};if(path==="/employee-learning/batches"&&options.method==="POST"){createCall=options;return {batch:{id:91}};}throw new Error(`unexpected API ${path}`);};\n'
            + function(self.source, "bossDashNumber")
            + "\n"
            + function(self.source, "employeeLearningBatchHashSummary")
            + "\n"
            + function(self.source, "employeeLearningBatchPreviewReady")
            + "\n"
            + function(self.source, "employeeLearningBatchIndustryLabel")
            + "\n"
            + function(self.source, "employeeLearningBatchRequestBody")
            + "\n"
            + function(self.source, "employeeLearningBatchExecuteSnapshot")
            + "\n"
            + function(self.source, "employeeLearningBatchExecuteIsCurrent")
            + "\n"
            + function(self.source, "employeeLearningBatchBillingProof")
            + "\n"
            + function(self.source, "employeeLearningBatchStatusLabel")
            + "\n"
            + function(self.source, "employeeLearningBatchReviewList")
            + "\n"
            + function(self.source, "drawEmployeeLearningBatchManager")
            + "\n"
            + function(self.source, "previewEmployeeLearningBatch")
            + "\n"
            + function(self.source, "executeEmployeeLearningBatch")
            + "\n(async()=>{await previewEmployeeLearningBatch();"
            + 'if(!rendered.includes("预览已锁定"))throw new Error("preview was not rendered");'
            + 'if(!rendered.includes("预计钱包扣点上限 0 点")||rendered.includes("钱包实扣"))throw new Error("dry-run plan was mislabeled as an actual debit");'
            + 'if(!rendered.includes(previewFixture.target_digest))throw new Error("target digest missing");'
            + 'if((rendered.match(/data-learning-industry=/g)||[]).length!==10)throw new Error("ten-industry summary missing");'
            + 'if(rendered.includes("a".repeat(64))||rendered.includes("b".repeat(64))||rendered.includes("c".repeat(64)))throw new Error("full frozen tuple leaked");'
            + 'await executeEmployeeLearningBatch();'
            + 'if(!confirmSawPreview)throw new Error("confirmation did not follow the rendered frozen preview");'
            + 'const body=createCall?.body||{};if(body.preview_token!==previewFixture.preview_token||body.budget_cap_points!==1080||body.confirm_execute!==true||body.auto_approve!==false)throw new Error("execute body drifted from preview");'
            + '})().catch(error=>{console.error(error);process.exit(1);});\n'
        )
        self._assert_node_ok(script)

    def test_reject_posts_frozen_tuple_and_refreshes_batch_and_employee(self):
        run = {
            "id": 73, "batch_id": 9, "employee_idx": 1001,
            "status": "awaiting_approval", "identity_ref": "a" * 64,
            "config_revision": 7, "config_sha256": "b" * 64,
            "bundle_sha256": "c" * 64,
        }
        script = (
            '"use strict";\n'
            'const $=()=>null;const toast=()=>{};let drawCount=0;const drawEmployeeLearningBatchManager=()=>{drawCount++;};\n'
            'globalThis.window={__ADM_DETAIL:null};let SPEC={idx:1001};let employeeDraws=0;const drawSpec=()=>{employeeDraws++;};const admDetail=async()=>{};\n'
            f'const frozenRun={json.dumps(run)};\n'
            'let EMPLOYEE_LEARNING_BATCH={batches:[{id:9,runs:[frozenRun]}]};let calls=[];\n'
            'const uiPrompt=async options=>{if(!options.validate("x".repeat(201)))throw new Error("reason bound missing");return "证据与岗位目标不匹配";};\n'
            'const api=async(path,options={})=>{calls.push({path,options});if(path.endsWith("/reject"))return {run:{...frozenRun,status:"rejected"}};if(path==="/employee-learning/batches/9")return {batch:{id:9,runs:[{...frozenRun,status:"rejected"}]}};if(path==="/depts/emp/1001")return {idx:1001,learning_run:{...frozenRun,status:"rejected"}};throw new Error(`unexpected API ${path}`);};\n'
            + function(self.source, "employeeLearningRunMutationFields")
            + "\n"
            + function(self.source, "employeeLearningRunForReview")
            + "\n"
            + function(self.source, "refreshEmployeeLearningBatch")
            + "\n"
            + function(self.source, "employeeLearningRefreshReviewViews")
            + "\n"
            + function(self.source, "employeeRejectLearning")
            + '\n(async()=>{const result=await employeeRejectLearning(73,"batch",9,1001);if(result?.run?.status!=="rejected")throw new Error("reject failed");'
            + 'const mutation=calls.find(call=>call.path.endsWith("/reject"));const expected={identity_ref:"a".repeat(64),config_revision:7,config_sha256:"b".repeat(64),bundle_sha256:"c".repeat(64),reason:"证据与岗位目标不匹配"};if(JSON.stringify(mutation?.options?.body)!==JSON.stringify(expected))throw new Error(`bad reject shape ${JSON.stringify(mutation)}`);'
            + 'if(!calls.some(call=>call.path==="/employee-learning/batches/9")||!calls.some(call=>call.path==="/depts/emp/1001")||employeeDraws!==1)throw new Error("success did not refresh batch and employee");'
            + '})().catch(error=>{console.error(error);process.exit(1);});\n'
        )
        self._assert_node_ok(script)

    def test_approve_posts_run_r1_not_current_r2_and_refreshes_after_cas_stale(self):
        pending_run = {
            "id": 73, "batch_id": 9, "employee_idx": 1001,
            "status": "awaiting_approval", "identity_ref": "a" * 64,
            "base_config_revision": 1, "base_config_sha256": "b" * 64,
            "bundle_sha256": "c" * 64,
        }
        current_employee = {
            "idx": 1001, "identity_ref": "a" * 64,
            "config_revision": 2, "config_sha256": "d" * 64,
            "bundle_sha256": "e" * 64, "learning_run": pending_run,
        }
        script = (
            '"use strict";const $=()=>null;const toast=()=>{};const uiConfirm=async()=>true;let employeeDraws=0;const drawSpec=()=>{employeeDraws++;};const drawEmployeeLearningBatchManager=()=>{};const admDetail=async()=>{};globalThis.window={__ADM_DETAIL:null};\n'
            f'let SPEC={json.dumps(current_employee)},EMPLOYEE_LEARNING_BATCH={{batches:[]}};let calls=[];\n'
            'const api=async(path,options={})=>{calls.push({path,options});if(path.endsWith("/approve")){const error=new Error("员工岗位或配置已变更");error.status=409;throw error;}if(path==="/employee-learning/batches/9")return {batch:{id:9,runs:[]}};if(path==="/depts/emp/1001")return {idx:1001,learning_run:{id:73,status:"stale"}};throw new Error(`unexpected API ${path}`);};\n'
            + function(self.source, "employeeIdentityMutationFields")
            + "\n"
            + function(self.source, "employeeLearningRunMutationFields")
            + "\n"
            + function(self.source, "employeeLearningRunForReview")
            + "\n"
            + function(self.source, "refreshEmployeeLearningBatch")
            + "\n"
            + function(self.source, "employeeLearningRefreshReviewViews")
            + "\n"
            + function(self.source, "employeeApproveLearning")
            + '\n(async()=>{await employeeApproveLearning(73,"spec",9,1001);const mutation=calls.find(call=>call.path.endsWith("/approve"));const expected={identity_ref:"a".repeat(64),config_revision:1,config_sha256:"b".repeat(64),bundle_sha256:"c".repeat(64)};if(JSON.stringify(mutation?.options?.body)!==JSON.stringify(expected))throw new Error(`approve used current config instead of frozen run ${JSON.stringify(mutation)}`);if(!calls.some(call=>call.path==="/employee-learning/batches/9")||!calls.some(call=>call.path==="/depts/emp/1001")||employeeDraws!==1||SPEC.learning_run?.status!=="stale")throw new Error("CAS stale did not refresh batch and employee UI");})().catch(error=>{console.error(error);process.exit(1);});\n'
        )
        self._assert_node_ok(script)

    def test_slow_stale_preview_cannot_overwrite_new_scope_or_concurrency(self):
        script = (
            '"use strict";const $=()=>null;const drawEmployeeLearningBatchManager=()=>{};const persistentMutationRequestKey=(_kind,scope,body)=>`${scope}-${body.max_concurrency}`;\n'
            'let EMPLOYEE_LEARNING_BATCH={scope:"all_v4",maxConcurrency:2,requestKey:"",loading:false,error:"",preview:null,batches:[]};let EMPLOYEE_LEARNING_BATCH_PREVIEW_SEQ=0,EMPLOYEE_LEARNING_BATCH_EXECUTE_SEQ=0;let resolvers=[];const api=()=>new Promise(resolve=>resolvers.push(resolve));\n'
            + function(self.source, "employeeLearningBatchRequestBody")
            + "\n"
            + function(self.source, "employeeLearningBatchScopeChanged")
            + "\n"
            + function(self.source, "employeeLearningBatchConcurrencyChanged")
            + "\n"
            + function(self.source, "previewEmployeeLearningBatch")
            + '\n(async()=>{const oldRequest=previewEmployeeLearningBatch();employeeLearningBatchScopeChanged("tea_coffee");employeeLearningBatchConcurrencyChanged(4);const newRequest=previewEmployeeLearningBatch();resolvers[1]({preview:{target_digest:"new",max_concurrency:4}});await newRequest;resolvers[0]({preview:{target_digest:"old",max_concurrency:2}});await oldRequest;if(EMPLOYEE_LEARNING_BATCH.preview?.target_digest!=="new"||EMPLOYEE_LEARNING_BATCH.scope!=="tea_coffee"||EMPLOYEE_LEARNING_BATCH.maxConcurrency!==4)throw new Error("stale preview overwrote current selection");})().catch(error=>{console.error(error);process.exit(1);});\n'
        )
        self._assert_node_ok(script)

    def test_stale_execute_cannot_clear_a_new_preview_or_its_request_key(self):
        old_preview = {
            "target_count": 360, "budget_cap_points": 1080,
            "max_concurrency": 2, "preview_token": "e" * 64,
            "target_digest": "d" * 64,
            "industry_counts": {"auto": 36},
            "target_sample": [{
                "config_revision": 7, "identity_ref": "a" * 64,
                "config_sha256": "b" * 64, "bundle_sha256": "c" * 64,
            }],
        }
        new_preview = {
            **old_preview, "target_count": 36, "budget_cap_points": 108,
            "max_concurrency": 4, "preview_token": "f" * 64,
            "target_digest": "9" * 64,
        }
        script = (
            '"use strict";const $=()=>null;const drawEmployeeLearningBatchManager=()=>{};const toast=()=>{};const uiConfirm=async()=>true;\n'
            f'const oldPreview={json.dumps(old_preview)},newPreview={json.dumps(new_preview)};\n'
            'let EMPLOYEE_LEARNING_BATCH={scope:"all_v4",maxConcurrency:2,requestKey:"all_v4-2",loading:false,error:"",preview:oldPreview,batches:[]};let EMPLOYEE_LEARNING_BATCH_PREVIEW_SEQ=0,EMPLOYEE_LEARNING_BATCH_EXECUTE_SEQ=0;\n'
            'const persistentMutationRequestKey=(_kind,scope,body)=>`${scope}-${body.max_concurrency}`;let clearCalls=[],executeResolve,loads=0;const clearPersistentMutationRequestKey=(...args)=>clearCalls.push(args);const loadEmployeeLearningBatches=async()=>{loads++;};\n'
            'const api=async(path,options={})=>{if(path==="/employee-learning/batches"&&options.method==="POST")return new Promise(resolve=>{executeResolve=resolve;});if(path.endsWith("/dry-run"))return {preview:newPreview};throw new Error(`unexpected API ${path}`);};\n'
            + function(self.source, "employeeLearningBatchPreviewReady")
            + "\n"
            + function(self.source, "employeeLearningBatchRequestBody")
            + "\n"
            + function(self.source, "employeeLearningBatchScopeChanged")
            + "\n"
            + function(self.source, "employeeLearningBatchConcurrencyChanged")
            + "\n"
            + optional_function(self.source, "employeeLearningBatchExecuteSnapshot")
            + "\n"
            + optional_function(self.source, "employeeLearningBatchExecuteIsCurrent")
            + "\n"
            + function(self.source, "previewEmployeeLearningBatch")
            + "\n"
            + function(self.source, "executeEmployeeLearningBatch")
            + '\n(async()=>{const oldExecute=executeEmployeeLearningBatch();await Promise.resolve();employeeLearningBatchScopeChanged("tea_coffee");employeeLearningBatchConcurrencyChanged(4);await previewEmployeeLearningBatch();if(EMPLOYEE_LEARNING_BATCH.preview!==newPreview)throw new Error("new preview setup failed");executeResolve({batch:{id:51,status:"queued"}});await oldExecute;if(EMPLOYEE_LEARNING_BATCH.scope!=="tea_coffee"||EMPLOYEE_LEARNING_BATCH.maxConcurrency!==4||EMPLOYEE_LEARNING_BATCH.requestKey!=="tea_coffee-4"||EMPLOYEE_LEARNING_BATCH.preview!==newPreview)throw new Error("stale execute overwrote new preview state");if(clearCalls.length)throw new Error(`stale execute cleared a persistent key ${JSON.stringify(clearCalls)}`);if(!EMPLOYEE_LEARNING_BATCH.batches.some(batch=>batch.id===51))throw new Error("successful stale batch was not retained for polling");})().catch(error=>{console.error(error);process.exit(1);});\n'
        )
        self._assert_node_ok(script)

    def test_batch_history_renders_billing_proof_and_never_assumes_missing_charge(self):
        script = (
            '"use strict";const $=()=>null;const esc=value=>String(value??"");let rendered="";const document={body:{insertAdjacentHTML:(_position,html)=>{rendered=html;}}};\n'
            'const EMPLOYEE_LEARNING_INDUSTRIES=[["all_v4","全部"],["auto","汽车后市场"],["beauty","美容美业"],["convenience","便利店"],["fitness","健身瑜伽"],["grocery","商超零售"],["hotel","酒店住宿"],["pet","宠物服务"],["pharmacy","零售药房"],["snack","量贩零食"],["tea_coffee","茶咖现制"]];\n'
            'let EMPLOYEE_LEARNING_BATCH={scope:"all_v4",maxConcurrency:2,loading:false,error:"",preview:null,batches:[{id:13,status:"completed",target_count:1,budget_cap_points:3,billing_proof_status:"verified",billing_mode:"tenant_points",wallet_charge_points:3,planned_wallet_charge_points:3,actual_wallet_debit_proof_status:"proof_missing",actual_wallet_debit_points:null,points_per_employee:3,target_digest:"e".repeat(64),counts:{}},{id:12,status:"completed",target_count:360,budget_cap_points:1080,billing_proof_status:"verified",billing_mode:"platform_included",wallet_charge_points:0,planned_wallet_charge_points:0,actual_wallet_debit_proof_status:"verified",actual_wallet_debit_points:0,points_per_employee:3,target_digest:"d".repeat(64),counts:{}},{id:11,status:"completed",target_count:1,budget_cap_points:3,counts:{}}]};\n'
            + function(self.source, "bossDashNumber")
            + "\n"
            + function(self.source, "employeeLearningBatchHashSummary")
            + "\n"
            + optional_function(self.source, "employeeLearningBatchBillingProof")
            + "\n"
            + function(self.source, "employeeLearningBatchPreviewReady")
            + "\n"
            + function(self.source, "employeeLearningBatchIndustryLabel")
            + "\n"
            + function(self.source, "employeeLearningBatchStatusLabel")
            + "\n"
            + function(self.source, "employeeLearningBatchReviewList")
            + "\n"
            + function(self.source, "drawEmployeeLearningBatchManager")
            + '\ndrawEmployeeLearningBatchManager();if(!rendered.includes("billing_mode platform_included")||!rendered.includes("总部平台套餐内")||!rendered.includes("预计钱包扣点上限 0 点")||!rendered.includes("实际净扣 0 点")||!rendered.includes("每人 3 点")||!rendered.includes("dddddddd…dddddd"))throw new Error("complete billing proof was not rendered");if(rendered.includes("钱包实扣"))throw new Error("planned amount was presented as an actual debit");if(!rendered.includes("实际净扣证明缺失"))throw new Error("missing actual debit proof was not explicit");if(!rendered.includes("计费证明缺失"))throw new Error("missing billing proof was presented as a zero charge");\n'
        )
        self._assert_node_ok(script)

    def test_queued_tenant_batch_never_labels_the_plan_cap_as_actual_debit(self):
        script = (
            '"use strict";const $=()=>null;const esc=value=>String(value??"");let rendered="";const document={body:{insertAdjacentHTML:(_position,html)=>{rendered=html;}}};\n'
            'const EMPLOYEE_LEARNING_INDUSTRIES=[["all_v4","全部"],["auto","汽车后市场"],["beauty","美容美业"],["convenience","便利店"],["fitness","健身瑜伽"],["grocery","商超零售"],["hotel","酒店住宿"],["pet","宠物服务"],["pharmacy","零售药房"],["snack","量贩零食"],["tea_coffee","茶咖现制"]];\n'
            'let EMPLOYEE_LEARNING_BATCH={scope:"all_v4",maxConcurrency:2,loading:false,error:"",preview:null,batches:[{id:21,status:"queued",target_count:36,budget_cap_points:108,billing_proof_status:"verified",billing_mode:"tenant_points",wallet_charge_points:108,planned_wallet_charge_points:108,actual_wallet_debit_proof_status:"verified",actual_wallet_debit_points:0,points_per_employee:3,target_digest:"a".repeat(64),counts:{}},{id:20,status:"queued",target_count:360,budget_cap_points:1080,billing_proof_status:"verified",billing_mode:"platform_included",wallet_charge_points:0,planned_wallet_charge_points:0,actual_wallet_debit_proof_status:"verified",actual_wallet_debit_points:0,points_per_employee:3,target_digest:"b".repeat(64),counts:{}}]};\n'
            + function(self.source, "bossDashNumber")
            + "\n"
            + function(self.source, "employeeLearningBatchHashSummary")
            + "\n"
            + function(self.source, "employeeLearningBatchBillingProof")
            + "\n"
            + function(self.source, "employeeLearningBatchPreviewReady")
            + "\n"
            + function(self.source, "employeeLearningBatchIndustryLabel")
            + "\n"
            + function(self.source, "employeeLearningBatchStatusLabel")
            + "\n"
            + function(self.source, "employeeLearningBatchReviewList")
            + "\n"
            + function(self.source, "drawEmployeeLearningBatchManager")
            + '\ndrawEmployeeLearningBatchManager();if(!rendered.includes("预计钱包扣点上限 108 点"))throw new Error("tenant plan cap was not labelled as an estimate");if(!rendered.includes("实际净扣 0 点"))throw new Error("verified actual debit was not rendered");if(rendered.includes("钱包实扣 108 点")||rendered.includes("实际净扣 108 点"))throw new Error("queued plan cap was mislabeled as an actual debit");\n'
        )
        self._assert_node_ok(script)

    def _assert_node_ok(self, script):
        result = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
