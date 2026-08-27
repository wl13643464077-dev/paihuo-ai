"""巡店 HTTP/任务/文件集成的硬边界。

不调用真实模型或网络；只验证权限、幂等、文件收口、专用重试与
人工关单入口不会被通用任务链路绕过。
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request

from app import assetfiles, auth, db, inspection, inspectionstandards, main, taskrunner
from app.skills import registry


def _photo(storage_key: str, digest: str = "a" * 64) -> dict:
    return {
        "storage_key": storage_key,
        "mime_type": "image/jpeg",
        "byte_size": 4,
        "sha256": digest,
        "width": 2,
        "height": 2,
    }


def _analysis_result(photos: list[dict], payload: dict) -> dict:
    payload = copy.deepcopy(payload)
    defaults = {"critical": 0, "high": 1, "medium": 3, "low": 7}
    for issue in payload.get("issues") or []:
        action = issue.get("action") if isinstance(issue, dict) else None
        if isinstance(action, dict):
            action.setdefault("owner", "测试店长")
            action.setdefault("due_days", defaults.get(issue.get("severity"), 3))
    issue_photo_ids = {
        int(evidence["photo_id"])
        for issue in payload.get("issues") or []
        for evidence in issue.get("evidence") or []
    }
    result = {
        **payload,
        "photo_reviews": [{
            "photo_id": int(item["id"]),
            "analyzable": True,
            "verdict": "issue" if int(item["id"]) in issue_photo_ids else "clean",
            "confidence": .95,
            "visible_facts": ["画面主体、通道与物品状态清晰可见"],
        } for item in photos if item.get("phase", "before") == "before"],
        "analysis_status": "issues_found" if payload.get("issues") else "clean_verified",
    }
    if not payload.get("issues"):
        result["verification"] = {
            "primary_model": "gpt-5.5",
            "review_model": "claude-opus-4-8",
            "both_clean": True,
        }
    return result


class InspectionBackendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "inspection-integration.db")
        db.conn()
        db.insert("tenants", {
            "id": 2,
            "name": "巡店测试企业",
            "balance": 20,
            "enabled": 1,
            "industries_json": '[]',
        })
        db.execute(
            "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
            "VALUES(2,'restaurant',1,0)"
        )
        db.execute(
            "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
            "VALUES(2,'auto',0,0)"
        )
        for user in (
            (20, "owner", "[]"),
            (21, "member", '["restaurant"]'),
            (22, "member", "[]"),
            (23, "tour", '["restaurant"]'),
            (24, "auditor", '["restaurant"]'),
            (25, "member", '["auto"]'),
        ):
            db.insert("users", {
                "id": user[0],
                "tenant_id": 2,
                "username": f"u{user[0]}",
                "password_hash": "x",
                "role": user[1],
                "modules_json": user[2],
                "enabled": 1,
            })
        self.owner = {
            "id": 20,
            "tenant_id": 2,
            "username": "u20",
            "role": "owner",
            "modules": [],
        }
        auth.set_current(self.owner)
        self.branch = inspection.create_branch(
            2, 20, "restaurant", {"name": "测试一店", "region": "华北"}
        )
        self.auto_branch = inspection.create_branch(
            2, 20, "auto", {"name": "测试车行", "region": "华北"}
        )
        main._persistent_upload_hits.clear()
        main._persistent_upload_active_tenants.clear()

    def tearDown(self):
        auth.set_current(None)
        main._persistent_upload_hits.clear()
        main._persistent_upload_active_tenants.clear()
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _linked_task(self, *, status="done", billing_status="included"):
        task_id = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": inspection.EMPLOYEE_IDX,
            "brief_json": json.dumps({"direction": "巡店"}),
            "status": status,
            "billing_status": billing_status,
            "billing_points": 1,
            "created_by": 20,
        })
        visit = inspection.create_visit_draft(
            2,
            20,
            "restaurant",
            self.branch["id"],
            {"request_key": f"linked-visit-{task_id:08d}"},
            [_photo(f"inspections/2/{task_id}/{'a' * 32}.jpg")],
            task_id=task_id,
        )
        return task_id, visit

    def test_tour_unknown_role_and_unmapped_member_fail_closed(self):
        # 门店名称是展示字段，不再承担自然键职责；不同区域可以同名。
        duplicate_name = main.inspection_branch_create({
            "name": "测试一店", "region": "另一区域",
            "industry_key": "restaurant",
        })
        self.assertNotEqual(self.branch["id"], duplicate_name["id"])
        self.assertEqual("测试一店", duplicate_name["name"])
        for uid, role, modules in (
            (22, "member", []),
            (23, "tour", ["restaurant"]),
            (24, "auditor", ["restaurant"]),
        ):
            auth.set_current({
                "id": uid, "tenant_id": 2, "username": f"u{uid}",
                "role": role, "modules": modules,
            })
            with self.assertRaises(HTTPException) as caught:
                main.inspection_meta("restaurant")
            self.assertEqual(403, caught.exception.status_code)

    def test_authorized_member_can_create_only_own_industry_branch(self):
        auth.set_current({
            "id": 21, "tenant_id": 2, "username": "u21",
            "role": "member", "modules": ["restaurant"],
        })
        created = main.inspection_branch_create({
            "name": "成员新建店", "region": "华东",
            "industry_key": "restaurant",
        })
        self.assertEqual(21, created["created_by"])
        self.assertEqual("restaurant", created["industry_key"])
        with self.assertRaises(HTTPException) as cross_industry:
            main.inspection_branch_create({
                "name": "跨行业门店", "industry_key": "auto",
            })
        self.assertEqual(403, cross_industry.exception.status_code)
        db.execute("UPDATE users SET enabled=0 WHERE id=21")
        with self.assertRaises(HTTPException) as disabled:
            main.inspection_branch_create({
                "name": "停用成员门店", "industry_key": "restaurant",
            })
        self.assertEqual(403, disabled.exception.status_code)

    def test_branch_filter_limits_history_but_keeps_global_risk_summary(self):
        second_branch = inspection.create_branch(
            2, 20, "restaurant", {"name": "测试二店", "region": "华东"}
        )
        for branch, key, score in (
            (self.branch, "filter-first-0001", 70),
            (second_branch, "filter-second-0001", 90),
        ):
            visit = inspection.create_visit_draft(
                2, 20, "restaurant", branch["id"],
                {"request_key": key},
                [_photo(f"inspections/2/{key}/{'7' * 32}.jpg", key[0] * 64)],
            )
            inspection.complete_visit(2, 20, "restaurant", visit["id"], _analysis_result(visit["photos"], {
                "summary": "完成", "score": score, "issues": [],
            }))
        result = main.inspection_list(
            industry_key="restaurant", branch_id=self.branch["id"], limit=40,
        )
        self.assertTrue(result["items"])
        self.assertEqual(
            {self.branch["id"]},
            {item["branch"]["id"] for item in result["items"]},
        )
        self.assertEqual(
            {self.branch["id"], second_branch["id"]},
            {item["id"] for item in result["summary"]["branches"]},
        )

    def test_member_default_industry_is_filtered_to_own_modules(self):
        # restaurant 是企业主行业，但该成员只被授权 auto。
        # 首次打开巡店页应直接落在 auto，而不是误选 restaurant 后 403。
        auth.set_current({
            "id": 25, "tenant_id": 2, "username": "u25",
            "role": "member", "modules": ["auto"],
        })
        result = main.inspection_meta(None)
        self.assertEqual("auto", result["industry_key"])
        self.assertEqual(["auto"], [item["key"] for item in result["industries"]])

    def test_inspection_task_uses_industry_scope_and_disables_text_thread(self):
        task_id, visit = self._linked_task()
        detail = main.task_get(task_id)
        self.assertEqual("inspection", detail["source"]["type"])
        self.assertEqual(
            f"#/inspections/{visit['id']}/restaurant",
            detail["source"]["route"],
        )
        self.assertEqual("unsupported", detail["thread"]["status"])
        self.assertFalse(detail["thread"]["can_continue"])

        async def rejected_calls():
            with self.assertRaises(HTTPException) as followup:
                await main.task_followup(task_id, {
                    "feedback": "换一版", "request_key": "followup-reject-0001",
                })
            self.assertEqual(409, followup.exception.status_code)
            with self.assertRaises(HTTPException) as accepted:
                await main.task_thread_accept(task_id, {"task_id": task_id})
            self.assertEqual(409, accepted.exception.status_code)

        asyncio.run(rejected_calls())
        auth.set_current({
            "id": 22, "tenant_id": 2, "username": "u22",
            "role": "member", "modules": [],
        })
        with self.assertRaises(HTTPException) as hidden:
            main.task_get(task_id)
        self.assertEqual(404, hidden.exception.status_code)

    def test_legacy_soft_deleted_inspection_task_cannot_be_hard_purged(self):
        task_id, _visit = self._linked_task()
        db.execute(
            "UPDATE task SET deleted_at=1,delete_reason='legacy' WHERE id=?",
            (task_id,),
        )
        with self.assertRaises(HTTPException) as blocked:
            main.trash_purge("task", task_id)
        self.assertEqual(409, blocked.exception.status_code)
        self.assertIsNotNone(db.one("SELECT id FROM task WHERE id=?", (task_id,)))

    def test_failed_inspection_retry_uses_only_inspection_worker(self):
        task_id, visit = self._linked_task(
            status="failed", billing_status="refunded"
        )
        inspection._mark_visit_failed(
            2, 20, visit["id"], RuntimeError("expected-test-failure")
        )
        with mock.patch.object(
            main, "_run_inspection_task", new=mock.AsyncMock()
        ) as inspection_worker, mock.patch.object(
            taskrunner, "run_task", new=mock.AsyncMock()
        ) as generic_worker:
            result = asyncio.run(main.task_retry(task_id))
        self.assertTrue(result["free_retry"])
        inspection_worker.assert_awaited_once_with(task_id)
        generic_worker.assert_not_awaited()
        self.assertEqual(
            {"status": "queued", "billing_status": "included"},
            db.one(
                "SELECT status,billing_status FROM task WHERE id=?", (task_id,)
            ),
        )
        self.assertEqual(
            "analyzing",
            db.one(
                "SELECT status FROM inspection_visit WHERE id=?", (visit["id"],)
            )["status"],
        )

    def test_restart_recovery_requeues_valid_and_settles_orphan(self):
        task_id, _visit = self._linked_task(
            status="running", billing_status="included"
        )
        orphan_id = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": inspection.EMPLOYEE_IDX,
            "brief_json": json.dumps({"direction": "孤儿"}),
            "status": "queued",
            "billing_status": "included",
        })
        recovered = main._recover_inspection_tasks()
        self.assertEqual([task_id], recovered["task_ids"])
        self.assertEqual(1, recovered["invalid"])
        self.assertEqual(
            "queued", db.one("SELECT status FROM task WHERE id=?", (task_id,))["status"]
        )
        self.assertEqual(
            "failed", db.one("SELECT status FROM task WHERE id=?", (orphan_id,))["status"]
        )

    def test_zero_issue_primary_uses_different_model_and_audit_issue_wins(self):
        task_id, visit = self._linked_task(status="queued", billing_status="included")
        photo_id = visit["photos"][0]["id"]
        primary = _analysis_result(
            visit["photos"],
            {"summary": "未发现问题", "score": 100, "issues": []},
        )
        primary.pop("verification")
        primary["analysis_status"] = "clean_candidate"
        audit = _analysis_result(visit["photos"], {
            "summary": "复核发现通道堆箱", "score": 72,
            "issues": [{
                "title": "通道堆箱", "description": "通道右侧纸箱形成遮挡",
                "severity": "high", "category": "safety", "confidence": .96,
                "evidence": [{"photo_id": photo_id, "note": "右侧堆箱"}],
                "action": {"plan": "立即清理并拍照复核", "due_days": 0},
            }],
        })
        gateway = mock.AsyncMock(side_effect=[
            {"text": json.dumps(primary, ensure_ascii=False), "tokens": 10},
            {"text": json.dumps(audit, ensure_ascii=False), "tokens": 11},
        ])
        with mock.patch.object(
            main, "_inspection_prompt_bundle",
            return_value=main.providers.PromptBundle(system="system", user="user"),
        ), mock.patch.object(
            main, "_load_inspection_images",
            return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
        ), mock.patch.object(
            main.providers, "vision_model_for", return_value="gpt-5.5",
        ), mock.patch.object(main.providers, "call_vision", gateway):
            asyncio.run(main._run_inspection_task(task_id))

        self.assertEqual(2, gateway.await_count)
        self.assertEqual("gpt-5.5", gateway.await_args_list[0].kwargs["model_override"])
        self.assertEqual(
            "claude-opus-4-8",
            gateway.await_args_list[1].kwargs["model_override"],
        )
        row = db.one(
            "SELECT status,billing_status FROM task WHERE id=?", (task_id,)
        )
        self.assertEqual({"status": "done", "billing_status": "included"}, row)
        stored = db.jloads(db.one(
            "SELECT model_json FROM inspection_visit WHERE id=?", (visit["id"],)
        )["model_json"], {})
        self.assertEqual("issues_found", stored["analysis_status"])
        self.assertEqual(1, len(stored["issues"]))

    def test_runtime_contract_replaces_editable_examples_with_exact_photo_ids(self):
        self.assertNotIn('"photo_id":1', registry.DEFAULT_PROMPTS["inspection"])
        visit = {
            "industry_key": "restaurant",
            "branch": {"name": "测试店"},
            "request_key": "runtime-contract-test",
            "visit_at": 1,
            "scope": "通道与卫生",
            "photos": [
                {"id": 73, "display_no": 1, "phase": "before", "caption": ""},
                {"id": 88, "display_no": 2, "phase": "before", "caption": ""},
            ],
        }
        legacy = '旧可编辑样例 {"photo_id":1,"issues":[]}'
        with mock.patch.object(
            main.employees,
            "get_config",
            return_value={"prompt_template": legacy},
        ), mock.patch.object(
            main.employees,
            "skills_block",
            return_value="自定义工作方式",
        ), mock.patch.object(
            registry,
            "context_block",
            return_value="已校验企业上下文",
        ):
            bundle = main._inspection_prompt_bundle(2, visit)

        marker = main._INSPECTION_CONTRACT_MARKER
        self.assertEqual(1, bundle.system.count(marker))
        self.assertGreater(bundle.system.rfind(marker), bundle.system.rfind(legacy))
        self.assertIn("allowed_photo_ids=[73, 88]", bundle.system)
        self.assertIn("expected_photo_review_count=2", bundle.system)
        self.assertIn('"enum":[73,88]', bundle.system)
        self.assertIn(
            '"confidence":{"type":"number","minimum":0.8,"maximum":1}',
            bundle.system,
        )
        packet = json.loads(bundle.user.split("\n", 1)[1])
        self.assertEqual([73, 88], packet["allowed_photo_ids"])
        self.assertEqual(2, packet["expected_photo_review_count"])

    def test_primary_contract_retry_uses_same_model_and_accumulates_usage(self):
        task_id, visit = self._linked_task(status="queued", billing_status="included")
        photo_id = visit["photos"][0]["id"]
        valid = _analysis_result(visit["photos"], {
            "summary": "复做后发现通道风险",
            "score": 76,
            "issues": [{
                "title": "通道堆放物",
                "description": "通道内可见纸箱",
                "severity": "high",
                "category": "safety",
                "confidence": .96,
                "root_cause": "堆放原因待核查",
                "evidence": [{"photo_id": photo_id, "note": "通道纸箱"}],
                "action": {
                    "plan": "清理通道并拍照复查",
                    "owner": "店长",
                    "due_days": 0,
                },
            }],
        })
        invalid = json.loads(json.dumps(valid, ensure_ascii=False))
        invalid["summary"] = "FIRST_RAW_MUST_NOT_BE_ECHOED"
        invalid["photo_reviews"][0]["photo_id"] = 999999
        gateway = mock.AsyncMock(side_effect=[
            {"text": json.dumps(invalid, ensure_ascii=False), "tokens": 7, "cost_usd": .1},
            {"text": json.dumps(valid, ensure_ascii=False), "tokens": 11, "cost_usd": .2},
        ])
        with mock.patch.object(
            main, "_inspection_prompt_bundle",
            return_value=main.providers.PromptBundle(system="system", user="user"),
        ), mock.patch.object(
            main, "_load_inspection_images",
            return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
        ), mock.patch.object(
            main.providers, "vision_model_for", return_value="gpt-5.5",
        ), mock.patch.object(
            main.providers, "call_vision", gateway,
        ), mock.patch.object(main.obs, "count") as counter, self.assertLogs(
            "main", level="WARNING"
        ) as logs:
            asyncio.run(main._run_inspection_task(task_id))

        self.assertEqual(2, gateway.await_count)
        self.assertEqual(
            ["gpt-5.5", "gpt-5.5"],
            [call.kwargs["model_override"] for call in gateway.await_args_list],
        )
        self.assertEqual(
            gateway.await_args_list[0].args[2],
            gateway.await_args_list[1].args[2],
            "格式重做必须重新发送同一批带权威标签的图片",
        )
        retry_system = gateway.await_args_list[1].kwargs["system_prompt"]
        self.assertIn("validation_code=IC_REVIEW_FOREIGN_ID", retry_system)
        self.assertEqual(1, retry_system.count(main._INSPECTION_CONTRACT_MARKER))
        self.assertNotIn("FIRST_RAW_MUST_NOT_BE_ECHOED", retry_system)
        counter.assert_any_call("inspection.validation.IC_REVIEW_FOREIGN_ID")
        self.assertIn("validation_code=IC_REVIEW_FOREIGN_ID", "\n".join(logs.output))
        self.assertNotIn("FIRST_RAW_MUST_NOT_BE_ECHOED", "\n".join(logs.output))
        row = db.one(
            "SELECT status,billing_status,tokens,cost_usd FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual("done", row["status"])
        self.assertEqual("included", row["billing_status"])
        self.assertEqual(18, row["tokens"])
        self.assertAlmostEqual(.3, row["cost_usd"])

    def test_missing_action_owner_or_due_retries_instead_of_inventing_defaults(self):
        for missing_field in ("owner", "due_days"):
            task_id, visit = self._linked_task(
                status="queued", billing_status="included"
            )
            photo_id = visit["photos"][0]["id"]
            valid = _analysis_result(visit["photos"], {
                "summary": "发现通道风险",
                "score": 78,
                "issues": [{
                    "title": "通道堆放物",
                    "description": "通道内可见纸箱",
                    "severity": "high",
                    "category": "safety",
                    "confidence": .95,
                    "root_cause": "原因待核查",
                    "evidence": [{"photo_id": photo_id, "note": "可见纸箱"}],
                    "action": {
                        "plan": "清理并拍照复查",
                        "owner": "值班店长",
                        "due_days": 1,
                    },
                }],
            })
            invalid = copy.deepcopy(valid)
            invalid["issues"][0]["action"].pop(missing_field)
            gateway = mock.AsyncMock(side_effect=[
                {"text": json.dumps(invalid, ensure_ascii=False), "tokens": 2},
                {"text": json.dumps(valid, ensure_ascii=False), "tokens": 3},
            ])
            with self.subTest(missing_field=missing_field), mock.patch.object(
                main, "_inspection_prompt_bundle",
                return_value=main.providers.PromptBundle(system="system", user="user"),
            ), mock.patch.object(
                main, "_load_inspection_images",
                return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
            ), mock.patch.object(
                main.providers, "vision_model_for", return_value="gpt-5.5",
            ), mock.patch.object(main.providers, "call_vision", gateway):
                asyncio.run(main._run_inspection_task(task_id))

            self.assertEqual(2, gateway.await_count)
            self.assertIn(
                "validation_code=IC_ACTION_REQUIRED",
                gateway.await_args_list[1].kwargs["system_prompt"],
            )
            self.assertEqual(
                {"status": "done", "tokens": 5},
                db.one("SELECT status,tokens FROM task WHERE id=?", (task_id,)),
            )
            action = db.one(
                "SELECT owner,due_at FROM inspection_action WHERE visit_id=?",
                (visit["id"],),
            )
            self.assertEqual("值班店长", action["owner"])
            self.assertIsNotNone(action["due_at"])

    def test_zero_issue_audit_has_its_own_contract_retry_and_usage(self):
        task_id, visit = self._linked_task(status="queued", billing_status="included")
        photo_id = visit["photos"][0]["id"]
        primary = _analysis_result(
            visit["photos"],
            {"summary": "主模型未见问题", "score": 100, "issues": []},
        )
        primary.pop("verification")
        primary["analysis_status"] = "clean_candidate"
        invalid_audit = {
            "analysis_status": "clean_candidate",
            "summary": "复核格式漂移",
            "score": 100,
            "issues": [],
        }
        valid_audit = _analysis_result(visit["photos"], {
            "summary": "复做后发现积水",
            "score": 70,
            "issues": [{
                "title": "地面积水",
                "description": "通道地面可见水渍",
                "severity": "high",
                "category": "safety",
                "confidence": .97,
                "root_cause": "来源待核查",
                "evidence": [{"photo_id": photo_id, "note": "地面水渍"}],
                "action": {
                    "plan": "立即设置警示并清理",
                    "owner": "值班店长",
                    "due_days": 0,
                },
            }],
        })
        gateway = mock.AsyncMock(side_effect=[
            {"text": json.dumps(primary, ensure_ascii=False), "tokens": 5, "cost_usd": .1},
            {"text": json.dumps(invalid_audit, ensure_ascii=False), "tokens": 7, "cost_usd": .2},
            {"text": json.dumps(valid_audit, ensure_ascii=False), "tokens": 11, "cost_usd": .3},
        ])
        with mock.patch.object(
            main, "_inspection_prompt_bundle",
            return_value=main.providers.PromptBundle(system="system", user="user"),
        ), mock.patch.object(
            main, "_load_inspection_images",
            return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
        ), mock.patch.object(
            main.providers, "vision_model_for", return_value="gpt-5.5",
        ), mock.patch.object(
            main.providers, "vision_review_model_for", return_value="claude-opus-4-8",
        ), mock.patch.object(main.providers, "call_vision", gateway):
            asyncio.run(main._run_inspection_task(task_id))

        self.assertEqual(3, gateway.await_count)
        self.assertEqual(
            ["gpt-5.5", "claude-opus-4-8", "claude-opus-4-8"],
            [call.kwargs["model_override"] for call in gateway.await_args_list],
        )
        self.assertIn(
            "validation_code=IC_REVIEW_SHAPE",
            gateway.await_args_list[2].kwargs["system_prompt"],
        )
        row = db.one("SELECT status,tokens,cost_usd FROM task WHERE id=?", (task_id,))
        self.assertEqual("done", row["status"])
        self.assertEqual(23, row["tokens"])
        self.assertAlmostEqual(.6, row["cost_usd"])

    def test_second_contract_failure_refunds_without_partial_model_write(self):
        task_id, visit = self._linked_task(status="queued", billing_status="charged")
        photo_id = visit["photos"][0]["id"]
        gateway = mock.AsyncMock(side_effect=[
            {"text": "not-json", "tokens": 3, "cost_usd": .1},
            {"text": "{}", "tokens": 4, "cost_usd": .2},
        ])
        with mock.patch.object(
            main, "_inspection_prompt_bundle",
            return_value=main.providers.PromptBundle(system="system", user="user"),
        ), mock.patch.object(
            main, "_load_inspection_images",
            return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
        ), mock.patch.object(
            main.providers, "vision_model_for", return_value="gpt-5.5",
        ), mock.patch.object(main.providers, "call_vision", gateway):
            asyncio.run(main._run_inspection_task(task_id))

        self.assertEqual(2, gateway.await_count)
        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one("SELECT status,billing_status FROM task WHERE id=?", (task_id,)),
        )
        self.assertEqual(
            {"status": "failed", "model_json": None},
            db.one(
                "SELECT status,model_json FROM inspection_visit WHERE id=?",
                (visit["id"],),
            ),
        )
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) n FROM inspection_issue WHERE visit_id=?",
                (visit["id"],),
            )["n"],
        )

    def test_semantic_hard_gates_do_not_format_retry(self):
        for field, value in (("analyzable", False), ("confidence", .79)):
            task_id, visit = self._linked_task(
                status="queued", billing_status="charged"
            )
            photo_id = visit["photos"][0]["id"]
            candidate = _analysis_result(
                visit["photos"],
                {"summary": "质量门未通过", "score": 80, "issues": []},
            )
            candidate.pop("verification")
            candidate["analysis_status"] = "clean_candidate"
            candidate["photo_reviews"][0][field] = value
            gateway = mock.AsyncMock(return_value={
                "text": json.dumps(candidate, ensure_ascii=False), "tokens": 5,
            })
            with self.subTest(field=field), mock.patch.object(
                main, "_inspection_prompt_bundle",
                return_value=main.providers.PromptBundle(system="system", user="user"),
            ), mock.patch.object(
                main, "_load_inspection_images",
                return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
            ), mock.patch.object(
                main.providers, "vision_model_for", return_value="gpt-5.5",
            ), mock.patch.object(main.providers, "call_vision", gateway):
                asyncio.run(main._run_inspection_task(task_id))
            gateway.assert_awaited_once()
            self.assertEqual(
                {"status": "failed", "billing_status": "refunded"},
                db.one(
                    "SELECT status,billing_status FROM task WHERE id=?",
                    (task_id,),
                ),
            )

    def test_leak_provider_error_and_cancellation_never_format_retry(self):
        cases = (
            ("leak", {"text": '【你的岗位工作手册】'}),
            ("provider", main.providers.ProviderError("上游失败")),
            ("cancel", asyncio.CancelledError()),
        )
        for name, outcome in cases:
            task_id, visit = self._linked_task(
                status="queued", billing_status="charged"
            )
            photo_id = visit["photos"][0]["id"]
            if isinstance(outcome, dict):
                gateway = mock.AsyncMock(return_value=outcome)
            else:
                gateway = mock.AsyncMock(side_effect=outcome)

            async def run():
                await main._run_inspection_task(task_id)

            with self.subTest(name=name), mock.patch.object(
                main, "_inspection_prompt_bundle",
                return_value=main.providers.PromptBundle(system="system", user="user"),
            ), mock.patch.object(
                main, "_load_inspection_images",
                return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
            ), mock.patch.object(
                main.providers, "vision_model_for", return_value="gpt-5.5",
            ), mock.patch.object(main.providers, "call_vision", gateway):
                if name == "cancel":
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(run())
                else:
                    asyncio.run(run())
            gateway.assert_awaited_once()
            self.assertEqual(
                {"status": "failed", "billing_status": "refunded"},
                db.one(
                    "SELECT status,billing_status FROM task WHERE id=?",
                    (task_id,),
                ),
            )

    def test_contract_retry_and_slot_wait_share_one_absolute_deadline(self):
        task_id, visit = self._linked_task(status="queued", billing_status="charged")
        photo_id = visit["photos"][0]["id"]
        timeouts = []

        async def drifting_gateway(*_args, **kwargs):
            timeouts.append(float(kwargs["timeout"]))
            if len(timeouts) == 1:
                await asyncio.sleep(.03)
                return {"text": "not-json", "tokens": 1}
            await asyncio.sleep(.2)
            return {"text": "{}", "tokens": 1}

        started = time.monotonic()
        with mock.patch.object(
            main, "_inspection_prompt_bundle",
            return_value=main.providers.PromptBundle(system="system", user="user"),
        ), mock.patch.object(
            main, "_load_inspection_images",
            return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
        ), mock.patch.object(
            main.providers, "vision_model_for", return_value="gpt-5.5",
        ), mock.patch.object(
            main, "_INSPECTION_ANALYSIS_MODEL_TIMEOUT_SECONDS", .12,
        ), mock.patch.object(main.providers, "call_vision", side_effect=drifting_gateway):
            asyncio.run(main._run_inspection_task(task_id))
        elapsed = time.monotonic() - started

        self.assertEqual(2, len(timeouts))
        self.assertLess(timeouts[1], timeouts[0])
        self.assertLess(elapsed, .19, "格式重做不得重置为新的 300s 窗口")
        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one("SELECT status,billing_status FROM task WHERE id=?", (task_id,)),
        )

    def test_high_score_primary_issue_skips_review_and_keeps_unique_evidence(self):
        task_id, visit = self._linked_task(status="queued", billing_status="included")
        photo_id = visit["photos"][0]["id"]
        primary = _analysis_result(visit["photos"], {
            "summary": "主模型发现独有的严重消防风险",
            "score": 100,
            "issues": [{
                "title": "唯一安全出口被完全封堵",
                "description": "照片中可见唯一安全出口前堆放多层纸箱",
                "severity": "critical",
                "category": "safety",
                "confidence": .98,
                "evidence": [{
                    "photo_id": photo_id,
                    "note": "安全出口门前纸箱完全遮挡通行区域",
                }],
                "action": {
                    "plan": "立即清空出口并上传无遮挡复查照片",
                    "due_days": 0,
                },
            }],
        })
        gateway = mock.AsyncMock(return_value={
            "text": json.dumps(primary, ensure_ascii=False),
            "tokens": 9,
        })
        with mock.patch.object(
            main, "_inspection_prompt_bundle",
            return_value=main.providers.PromptBundle(system="system", user="user"),
        ), mock.patch.object(
            main, "_load_inspection_images",
            return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
        ), mock.patch.object(
            main.providers, "vision_model_for", return_value="gpt-5.5",
        ), mock.patch.object(main.providers, "call_vision", gateway):
            asyncio.run(main._run_inspection_task(task_id))

        gateway.assert_awaited_once()
        self.assertEqual(
            {"status": "done", "billing_status": "included"},
            db.one(
                "SELECT status,billing_status FROM task WHERE id=?", (task_id,)
            ),
        )
        issue = db.one(
            "SELECT id,title,severity FROM inspection_issue WHERE visit_id=?",
            (visit["id"],),
        )
        self.assertEqual("critical", issue["severity"])
        self.assertEqual("唯一安全出口被完全封堵", issue["title"])
        evidence = db.one(
            "SELECT photo_id,note FROM inspection_evidence WHERE issue_id=?",
            (issue["id"],),
        )
        self.assertEqual(photo_id, evidence["photo_id"])
        self.assertIn("完全遮挡", evidence["note"])
        stored = db.jloads(db.one(
            "SELECT model_json FROM inspection_visit WHERE id=?", (visit["id"],)
        )["model_json"], {})
        self.assertEqual("issues_found", stored["analysis_status"])
        self.assertEqual(photo_id, stored["issues"][0]["evidence"][0]["photo_id"])

    def test_two_clean_models_complete_with_conservative_score(self):
        task_id, visit = self._linked_task(status="queued", billing_status="included")
        photo_id = visit["photos"][0]["id"]
        candidates = []
        for score, summary in ((100, "主模型逐图清洁"), (96, "复核逐图清洁")):
            value = _analysis_result(
                visit["photos"], {"summary": summary, "score": score, "issues": []}
            )
            value.pop("verification")
            value["analysis_status"] = "clean_candidate"
            candidates.append({
                "text": json.dumps(value, ensure_ascii=False), "tokens": 5,
            })
        with mock.patch.object(
            main, "_inspection_prompt_bundle",
            return_value=main.providers.PromptBundle(system="system", user="user"),
        ), mock.patch.object(
            main, "_load_inspection_images",
            return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
        ), mock.patch.object(
            main.providers, "vision_model_for", return_value="gpt-5.5",
        ), mock.patch.object(
            main.providers, "call_vision", new=mock.AsyncMock(side_effect=candidates),
        ):
            asyncio.run(main._run_inspection_task(task_id))

        stored = db.jloads(db.one(
            "SELECT score,model_json FROM inspection_visit WHERE id=?", (visit["id"],)
        )["model_json"], {})
        self.assertEqual("clean_verified", stored["analysis_status"])
        self.assertEqual(96, stored["score"])
        self.assertTrue(stored["verification"]["both_clean"])

    def test_audit_failure_fails_visit_and_refunds_charged_task(self):
        task_id, visit = self._linked_task(status="queued", billing_status="charged")
        photo_id = visit["photos"][0]["id"]
        primary = _analysis_result(
            visit["photos"],
            {"summary": "未发现问题", "score": 100, "issues": []},
        )
        primary.pop("verification")
        primary["analysis_status"] = "clean_candidate"
        gateway = mock.AsyncMock(side_effect=[
            {"text": json.dumps(primary, ensure_ascii=False), "tokens": 5},
            main.providers.ProviderError("视觉复核失败"),
        ])
        with mock.patch.object(
            main, "_inspection_prompt_bundle",
            return_value=main.providers.PromptBundle(system="system", user="user"),
        ), mock.patch.object(
            main, "_load_inspection_images",
            return_value=[({"photo_id": photo_id, "display_no": 1}, "image/jpeg", "eA==")],
        ), mock.patch.object(
            main.providers, "vision_model_for", return_value="gpt-5.5",
        ), mock.patch.object(main.providers, "call_vision", gateway):
            asyncio.run(main._run_inspection_task(task_id))

        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM task WHERE id=?", (task_id,)
            ),
        )
        self.assertEqual(
            {"status": "failed", "model_json": None},
            db.one(
                "SELECT status,model_json FROM inspection_visit WHERE id=?",
                (visit["id"],),
            ),
        )

    def test_recheck_route_requires_owner_before_it_can_close_issue(self):
        _task_id, visit = self._linked_task()
        photo_id = visit["photos"][0]["id"]
        completed = inspection.complete_visit(
            2,
            20,
            "restaurant",
            visit["id"],
            _analysis_result(visit["photos"], {
                "summary": "待整改",
                "score": 80,
                "issues": [{
                    "title": "灯箱不亮",
                    "description": "右侧灯箱不亮",
                    "severity": "medium",
                    "category": "brand",
                    "confidence": .95,
                    "evidence": [{"photo_id": photo_id}],
                    "action": {"plan": "修复并上传复查照片", "due_days": 1},
                }],
            }),
        )
        action = completed["issues"][0]["action"]
        action = inspection.transition_action(
            2,
            21,
            "restaurant",
            action["id"],
            expected_version=action["version"],
            target_status="awaiting_recheck",
        )
        photos = inspection.add_recheck_photos(
            2,
            21,
            "restaurant",
            action["id"],
            [_photo(
                f"inspections/2/{visit['id']}/{'d' * 32}.jpg",
                "d" * 64,
            )],
        )
        recheck = inspection.record_recheck(
            2,
            21,
            "restaurant",
            action["id"],
            {
                "recommendation": "close",
                "confidence": .95,
                "note": "画面显示灯箱已亮",
                "evidence_photo_ids": [photos[0]["id"]],
            },
        )
        auth.set_current({
            "id": 21, "tenant_id": 2, "username": "u21",
            "role": "member", "modules": ["restaurant"],
        })
        with self.assertRaises(HTTPException) as member_denied:
            main.inspection_recheck_review(recheck["id"], {
                "industry_key": "restaurant",
                "decision": "close",
                "expected_action_version": action["version"],
                "note": "成员不能关单",
            })
        self.assertEqual(403, member_denied.exception.status_code)
        auth.set_current(self.owner)
        reviewed = main.inspection_recheck_review(recheck["id"], {
            "industry_key": "restaurant",
            "decision": "close",
            "expected_action_version": action["version"],
            "note": "企业主人工复核通过",
        })
        self.assertEqual("approved", reviewed["status"])
        self.assertEqual("closed", reviewed["action"]["status"])
        self.assertEqual(20, reviewed["action"]["closed_by"])

    def test_assignment_route_is_manager_only_and_uses_cas(self):
        _task_id, visit = self._linked_task()
        completed = inspection.complete_visit(
            2, 20, "restaurant", visit["id"], _analysis_result(visit["photos"], {
                "summary": "待确认责任", "score": 82,
                "issues": [{
                    "title": "货架杂乱", "description": "货架可见杂乱",
                    "severity": "low", "category": "display", "confidence": .96,
                    "evidence": [{"photo_id": visit["photos"][0]["id"]}],
                    "action": {"plan": "重新陈列", "due_days": 2},
                }],
            }),
        )
        issue = completed["issues"][0]
        action = issue["action"]
        body = {
            "industry_key": "restaurant",
            "action_id": action["id"],
            "expected_version": action["version"],
            "owner": "陈店长",
            "due_at": 2_000_000_000,
            "plan": "重新陈列并按标准拍照",
        }
        auth.set_current({
            "id": 21, "tenant_id": 2, "username": "u21",
            "role": "member", "modules": ["restaurant"],
        })
        with self.assertRaises(HTTPException) as denied:
            main.inspection_action_assignment(visit["id"], issue["id"], body)
        self.assertEqual(403, denied.exception.status_code)

        auth.set_current(self.owner)
        assigned = main.inspection_action_assignment(visit["id"], issue["id"], body)
        self.assertEqual("陈店长", assigned["owner"])
        self.assertEqual(action["version"] + 1, assigned["version"])
        with self.assertRaises(HTTPException) as stale:
            main.inspection_action_assignment(visit["id"], issue["id"], body)
        self.assertEqual(409, stale.exception.status_code)

    def test_cancelled_recheck_still_records_manual_review_before_exit(self):
        _task_id, visit = self._linked_task()
        completed = inspection.complete_visit(
            2, 20, "restaurant", visit["id"], _analysis_result(visit["photos"], {
                "summary": "待复查", "score": 70,
                "issues": [{
                    "title": "陈列歪斜", "description": "货架陈列歪斜",
                    "severity": "low", "category": "display", "confidence": .9,
                    "evidence": [{"photo_id": visit["photos"][0]["id"]}],
                    "action": {"plan": "扶正后上传照片", "due_days": 1},
                }],
            }),
        )
        issue = completed["issues"][0]
        action = issue["action"]
        prepared = [{"data": b"jpeg", **_photo("unused", "e" * 64)}]

        def stored(tid, visit_id, _items):
            return [_photo(
                f"inspections/{tid}/{visit_id}/{'e' * 32}.jpg", "e" * 64
            )]

        async def submit():
            return await main.inspection_recheck_create(
                visit_id=visit["id"],
                issue_id=issue["id"],
                action_id=action["id"],
                expected_version=action["version"],
                industry_key="restaurant",
                file=object(),
            )

        with mock.patch.object(
            main, "_prepare_inspection_uploads",
            new=mock.AsyncMock(return_value=prepared),
        ), mock.patch.object(
            main, "_store_inspection_images", side_effect=stored
        ), mock.patch.object(
            main, "_load_inspection_images", return_value=[("image/jpeg", "eA==")]
        ), mock.patch.object(
            main.providers,
            "call_vision",
            new=mock.AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(submit())
        row = db.one(
            "SELECT status,model_recommendation FROM inspection_recheck "
            "WHERE action_id=?",
            (action["id"],),
        )
        self.assertEqual("pending", row["status"])
        self.assertEqual("manual_review", row["model_recommendation"])
        self.assertEqual(
            "awaiting_recheck",
            db.one(
                "SELECT status FROM inspection_action WHERE id=?", (action["id"],)
            )["status"],
        )

    def test_bundle_cancellation_anchors_photos_and_retry_does_not_duplicate(self):
        _task_id, visit = self._linked_task()
        completed = inspection.complete_visit(
            2, 20, "restaurant", visit["id"], _analysis_result(visit["photos"], {
                "summary": "待复查", "score": 72,
                "issues": [{
                    "title": "冰箱温度异常", "description": "温度表超标",
                    "severity": "high", "category": "safety", "confidence": .96,
                    "evidence": [{"photo_id": visit["photos"][0]["id"]}],
                    "action": {"plan": "调整温控后上传照片", "due_days": 1},
                }],
            }),
        )
        issue = completed["issues"][0]
        action = issue["action"]
        prepared = [{"data": b"jpeg", **_photo("unused", "5" * 64)}]

        def stored(tid, visit_id, _items):
            return [_photo(
                f"inspections/{tid}/{visit_id}/{'5' * 32}.jpg", "5" * 64
            )]

        async def submit():
            return await main.inspection_recheck_create(
                visit_id=visit["id"],
                issue_id=issue["id"],
                action_id=action["id"],
                expected_version=action["version"],
                industry_key="restaurant",
                file=object(),
            )

        with mock.patch.object(
            main,
            "_prepare_inspection_uploads",
            new=mock.AsyncMock(return_value=prepared),
        ), mock.patch.object(
            main, "_store_inspection_images", side_effect=stored
        ) as store, mock.patch.object(
            main,
            "_inspection_recheck_bundle",
            side_effect=asyncio.CancelledError(),
        ) as bundle:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(submit())
            replay = asyncio.run(submit())

        self.assertTrue(replay["replayed"])
        self.assertEqual(1, store.call_count)
        self.assertEqual(1, bundle.call_count)
        self.assertEqual(
            {"photos": 1, "unattached": 0, "pending": 1},
            db.one(
                "SELECT "
                "(SELECT COUNT(*) FROM inspection_photo WHERE tenant_id=2 "
                "AND visit_id=? AND phase='recheck') photos,"
                "(SELECT COUNT(*) FROM inspection_photo WHERE tenant_id=2 "
                "AND visit_id=? AND phase='recheck' AND recheck_id IS NULL) unattached,"
                "(SELECT COUNT(*) FROM inspection_recheck WHERE tenant_id=2 "
                "AND action_id=? AND status='pending') pending",
                (visit["id"], visit["id"], action["id"]),
            ),
        )

    def test_record_cancellation_commits_anchor_before_propagating(self):
        _task_id, visit = self._linked_task()
        completed = inspection.complete_visit(
            2, 20, "restaurant", visit["id"], _analysis_result(visit["photos"], {
                "summary": "待复查", "score": 75,
                "issues": [{
                    "title": "灭火器遮挡", "description": "灭火器前有纸箱",
                    "severity": "high", "category": "safety", "confidence": .97,
                    "evidence": [{"photo_id": visit["photos"][0]["id"]}],
                    "action": {"plan": "清理遮挡物后上传照片", "due_days": 1},
                }],
            }),
        )
        issue = completed["issues"][0]
        action = issue["action"]
        prepared = [{"data": b"jpeg", **_photo("unused", "4" * 64)}]
        record_started = threading.Event()
        real_record = inspection.record_recheck

        def stored(tid, visit_id, _items):
            return [_photo(
                f"inspections/{tid}/{visit_id}/{'4' * 32}.jpg", "4" * 64
            )]

        def slow_record(*args, **kwargs):
            record_started.set()
            time.sleep(.03)
            return real_record(*args, **kwargs)

        async def submit():
            return await main.inspection_recheck_create(
                visit_id=visit["id"],
                issue_id=issue["id"],
                action_id=action["id"],
                expected_version=action["version"],
                industry_key="restaurant",
                file=object(),
            )

        async def cancel_during_record():
            task = asyncio.create_task(submit())
            while not record_started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        response = {
            "text": json.dumps({
                "recommendation": "manual_review",
                "confidence": 0,
                "note": "请人工核对",
                "evidence_photo_ids": [],
            }, ensure_ascii=False),
        }
        with mock.patch.object(
            main,
            "_prepare_inspection_uploads",
            new=mock.AsyncMock(return_value=prepared),
        ), mock.patch.object(
            main, "_store_inspection_images", side_effect=stored
        ) as store, mock.patch.object(
            main, "_load_inspection_images", return_value=[("image/jpeg", "eA==")]
        ), mock.patch.object(
            main.providers,
            "call_vision",
            new=mock.AsyncMock(return_value=response),
        ), mock.patch.object(
            inspection, "record_recheck", side_effect=slow_record
        ) as record:
            asyncio.run(cancel_during_record())
            replay = asyncio.run(submit())

        self.assertTrue(replay["replayed"])
        self.assertEqual(1, store.call_count)
        self.assertEqual(1, record.call_count)
        self.assertEqual(
            {"photos": 1, "unattached": 0, "pending": 1},
            db.one(
                "SELECT "
                "(SELECT COUNT(*) FROM inspection_photo WHERE tenant_id=2 "
                "AND visit_id=? AND phase='recheck') photos,"
                "(SELECT COUNT(*) FROM inspection_photo WHERE tenant_id=2 "
                "AND visit_id=? AND phase='recheck' AND recheck_id IS NULL) unattached,"
                "(SELECT COUNT(*) FROM inspection_recheck WHERE tenant_id=2 "
                "AND action_id=? AND status='pending') pending",
                (visit["id"], visit["id"], action["id"]),
            ),
        )

    def test_recheck_whole_model_operation_times_out_to_manual_review(self):
        _task_id, visit = self._linked_task()
        completed = inspection.complete_visit(
            2, 20, "restaurant", visit["id"], _analysis_result(visit["photos"], {
                "summary": "待复查", "score": 70,
                "issues": [{
                    "title": "通道有杂物", "description": "通道内可见杂物",
                    "severity": "medium", "category": "safety", "confidence": .9,
                    "evidence": [{"photo_id": visit["photos"][0]["id"]}],
                    "action": {"plan": "清理后上传照片", "due_days": 1},
                }],
            }),
        )
        issue = completed["issues"][0]
        action = issue["action"]
        prepared = [{"data": b"jpeg", **_photo("unused", "6" * 64)}]

        def stored(tid, visit_id, _items):
            return [_photo(
                f"inspections/{tid}/{visit_id}/{'6' * 32}.jpg", "6" * 64
            )]

        async def slow_vision(*_args, **_kwargs):
            await asyncio.sleep(.05)
            return {"text": "{}"}

        async def submit():
            return await main.inspection_recheck_create(
                visit_id=visit["id"],
                issue_id=issue["id"],
                action_id=action["id"],
                expected_version=action["version"],
                industry_key="restaurant",
                file=object(),
            )

        with mock.patch.object(
            main,
            "_prepare_inspection_uploads",
            new=mock.AsyncMock(return_value=prepared),
        ), mock.patch.object(
            main, "_store_inspection_images", side_effect=stored
        ), mock.patch.object(
            main, "_load_inspection_images", return_value=[("image/jpeg", "eA==")]
        ), mock.patch.object(
            main, "_INSPECTION_RECHECK_MODEL_TIMEOUT_SECONDS", .001
        ), mock.patch.object(
            main.providers, "call_vision", side_effect=slow_vision
        ):
            result = asyncio.run(submit())
        self.assertEqual(
            "manual_review",
            result["recheck"]["model_recommendation"],
        )
        self.assertEqual("pending", result["recheck"]["status"])

    def test_recheck_storage_failure_keeps_action_retryable(self):
        _task_id, visit = self._linked_task()
        completed = inspection.complete_visit(
            2, 20, "restaurant", visit["id"], _analysis_result(visit["photos"], {
                "summary": "待整改", "score": 70,
                "issues": [{
                    "title": "通道有杂物", "description": "通道内可见杂物",
                    "severity": "medium", "category": "safety", "confidence": .9,
                    "evidence": [{"photo_id": visit["photos"][0]["id"]}],
                    "action": {"plan": "清理后上传照片", "due_days": 1},
                }],
            }),
        )
        issue = completed["issues"][0]
        action = inspection.transition_action(
            2, 20, "restaurant", issue["action"]["id"],
            expected_version=issue["action"]["version"],
            target_status="in_progress", note="开始整改",
        )

        async def submit():
            return await main.inspection_recheck_create(
                visit_id=visit["id"], issue_id=issue["id"],
                action_id=action["id"], expected_version=action["version"],
                industry_key="restaurant", file=object(),
            )

        with mock.patch.object(
            main, "_prepare_inspection_uploads",
            new=mock.AsyncMock(return_value=[{"data": b"jpeg", **_photo("unused")}]),
        ), mock.patch.object(
            main, "_store_inspection_images", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                asyncio.run(submit())
        row = db.one("SELECT status,version FROM inspection_action WHERE id=?", (action["id"],))
        self.assertEqual("in_progress", row["status"])
        self.assertEqual(action["version"], row["version"])
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) n FROM inspection_photo WHERE visit_id=? AND phase='recheck'",
            (visit["id"],),
        )["n"])

    def test_image_loader_uses_explicit_recheck_phase(self):
        self.assertLess(
            main._INSPECTION_RECHECK_MODEL_TIMEOUT_SECONDS,
            120,
            "后端复查必须在前端 120s 超时前降级到人工复核",
        )
        visit = {
            "photos": [
                {"phase": "before", "storage_key": "before.jpg"},
                {"phase": "recheck", "storage_key": "after.jpg"},
            ]
        }
        with mock.patch.object(
            assetfiles, "resolve_tenant_asset", return_value="/tmp/after.jpg"
        ) as resolve, mock.patch.object(
            main, "_read_file_bytes", return_value=b"after"
        ):
            images = main._load_inspection_images(2, visit, phase="recheck")
        self.assertEqual(1, len(images))
        resolve.assert_called_once_with(
            "/files/after.jpg", 2, allowed_extensions=(".jpg",)
        )

    def test_store_batch_failure_removes_all_prior_files_and_rejects_symlink(self):
        asset_root = tempfile.mkdtemp(dir=self.tmp.name)
        items = [
            {"data": b"one", **_photo("unused-1")},
            {"data": b"two", **_photo("unused-2", "b" * 64)},
        ]
        real_open = os.open
        calls = 0

        def fail_second(path, flags, mode=0o777):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated disk failure")
            return real_open(path, flags, mode)

        with mock.patch.object(assetfiles, "ASSET_ROOT", asset_root), \
                mock.patch.object(main.os, "open", side_effect=fail_second):
            with self.assertRaises(OSError):
                main._store_inspection_images(2, 1, items)
        stored_dir = os.path.join(asset_root, "inspections", "2", "1")
        self.assertEqual([], os.listdir(stored_dir))

        outside = tempfile.mkdtemp(dir=self.tmp.name)
        tenant_parent = os.path.join(asset_root, "inspections")
        os.symlink(outside, os.path.join(tenant_parent, "3"))
        with mock.patch.object(assetfiles, "ASSET_ROOT", asset_root):
            with self.assertRaises(ValueError):
                main._store_inspection_images(3, 9, items[:1])
        self.assertFalse(os.path.exists(os.path.join(outside, "9")))

    def test_replayed_empty_shell_cleans_crash_orphan_before_new_write(self):
        shell = inspection.create_visit_shell(
            2,
            20,
            "restaurant",
            self.branch["id"],
            {"request_key": "empty-shell-orphan-0001"},
        )
        asset_root = tempfile.mkdtemp(dir=self.tmp.name)
        directory = os.path.join(
            asset_root, "inspections", "2", str(shell["id"])
        )
        os.makedirs(directory)
        orphan = os.path.join(directory, "f" * 32 + ".jpg")
        with open(orphan, "wb") as handle:
            handle.write(b"orphan")
        with mock.patch.object(assetfiles, "ASSET_ROOT", asset_root):
            removed = main._cleanup_empty_shell_inspection_files(2, shell["id"])
        self.assertEqual(1, removed)
        self.assertFalse(os.path.exists(orphan))

    def test_inspection_files_count_toward_shared_persistent_quota(self):
        asset_root = tempfile.mkdtemp(dir=self.tmp.name)
        inspection_dir = os.path.join(asset_root, "inspections", "2", "7")
        os.makedirs(inspection_dir)
        with open(os.path.join(inspection_dir, "a.jpg"), "wb") as handle:
            handle.write(b"12345")
        clip_root = tempfile.mkdtemp(dir=self.tmp.name)
        with mock.patch.object(assetfiles, "ASSET_ROOT", asset_root), \
                mock.patch.object(main.textvideo, "CLIP_ROOT", clip_root), \
                mock.patch.object(
                    main.avatar, "tenant_asset_usage",
                    return_value={"files": 0, "bytes": 0},
                ):
            usage = main._persistent_upload_usage(2)
        self.assertEqual({"files": 1, "bytes": 5}, usage)

    def test_inspection_file_resolution_requires_exact_tenant_visit_and_db_row(self):
        _task_id, visit = self._linked_task()
        filename = "9" * 32 + ".jpg"
        storage_key = f"inspections/2/{visit['id']}/{filename}"
        db.execute(
            "UPDATE inspection_photo SET storage_key=? WHERE id=?",
            (storage_key, visit["photos"][0]["id"]),
        )
        asset_root = tempfile.mkdtemp(dir=self.tmp.name)
        path = os.path.join(asset_root, storage_key)
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as handle:
            handle.write(b"jpeg")
        url = "/files/" + storage_key
        with mock.patch.object(assetfiles, "ASSET_ROOT", asset_root):
            self.assertEqual(os.path.realpath(path), assetfiles.resolve_tenant_asset(url, 2))
            with self.assertRaises(assetfiles.AssetAccessError):
                assetfiles.resolve_tenant_asset(url, 3)
            wrong_visit = url.replace(
                f"/{visit['id']}/", f"/{visit['id'] + 1}/"
            )
            with self.assertRaises(assetfiles.AssetAccessError):
                assetfiles.resolve_tenant_asset(wrong_visit, 2)

    def test_inspection_file_middleware_enforces_member_industry_scope(self):
        task_id = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": inspection.EMPLOYEE_IDX,
            "brief_json": "{}",
            "status": "done",
            "billing_status": "included",
            "billing_points": 1,
            "created_by": 20,
        })
        visit = inspection.create_visit_draft(
            2,
            20,
            "auto",
            self.auto_branch["id"],
            {"request_key": "auto-file-scope-0001"},
            [_photo(f"inspections/2/{task_id}/{'8' * 32}.jpg", "8" * 64)],
            task_id=task_id,
        )
        storage_key = visit["photos"][0]["storage_key"]
        url = "/files/" + storage_key
        self.assertEqual(
            {
                "tenant_id": 2,
                "industry_key": "auto",
                "required_module": "auto",
            },
            assetfiles.file_access_scope(url),
        )

        async def probe(uid):
            request = Request({
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": url,
                "raw_path": url.encode("ascii"),
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("testserver", 80),
            })

            async def call_next(_request):
                return Response(status_code=200)

            user = auth.get_user(uid)
            with mock.patch.object(
                main.auth, "parse_session", return_value=uid
            ), mock.patch.object(
                main.auth, "get_user", return_value=user
            ):
                return await main._auth_mw(request, call_next)

        # 同企业不等于同行业：member 必须再过 modules 授权。
        self.assertEqual(403, asyncio.run(probe(21)).status_code)
        self.assertEqual(200, asyncio.run(probe(25)).status_code)
        self.assertEqual(200, asyncio.run(probe(20)).status_code)

        db.execute(
            "UPDATE inspection_visit SET deleted_at=1 WHERE id=?",
            (visit["id"],),
        )
        self.assertEqual(0, assetfiles.file_owner_tid(url))
        self.assertEqual(403, asyncio.run(probe(20)).status_code)

    def test_initial_route_replay_does_not_store_or_charge_twice(self):
        slots = [
            item["slot_code"]
            for item in inspectionstandards.capture_slots("restaurant")
            if item["required"]
        ]
        prepared = [
            {"data": b"jpeg", **_photo("unused", f"{index:x}" * 64),
             "capture_slot": slot}
            for index, slot in enumerate(slots, start=1)
        ]

        def stored(tid, visit_id, items):
            return [{
                **_photo(
                    f"inspections/{tid}/{visit_id}/{index:032x}.jpg",
                    f"{index:x}" * 64,
                ),
                "capture_slot": item["capture_slot"],
            } for index, item in enumerate(items, start=1)]

        async def create_once():
            return await main.inspection_create(
                branch_id=self.branch["id"],
                visit_at="",
                scope="消防与陈列",
                request_key="route-idempotency-0001",
                industry_key="restaurant",
                files=[object() for _ in slots],
                file_slots=slots,
                template_version=inspectionstandards.CATALOG_VERSION,
                observations_json='{"metrics":[],"checklist":[]}',
            )

        before = db.one("SELECT balance FROM tenants WHERE id=2")["balance"]
        with mock.patch.object(
            main, "_prepare_inspection_uploads", new=mock.AsyncMock(return_value=prepared)
        ), mock.patch.object(
            main, "_store_inspection_images", side_effect=stored
        ) as store, mock.patch.object(main, "_start_inspection_task"):
            first = asyncio.run(create_once())
            replay = asyncio.run(create_once())
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["task_id"], replay["task_id"])
        self.assertEqual(1, store.call_count)
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM task WHERE emp_idx=?",
                (inspection.EMPLOYEE_IDX,),
            )["n"],
        )
        after = db.one("SELECT balance FROM tenants WHERE id=2")["balance"]
        self.assertEqual(1, float(before) - float(after))
        self.assertTrue(
            main._settle_inspection_task_by_id(
                first["task_id"], "模拟巡店分析失败"
            )
        )
        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM task WHERE id=?",
                (first["task_id"],),
            ),
        )
        self.assertEqual(
            "failed",
            db.one(
                "SELECT status FROM inspection_visit WHERE id=?",
                (first["inspection_id"],),
            )["status"],
        )
        self.assertEqual(
            float(before),
            float(db.one("SELECT balance FROM tenants WHERE id=2")["balance"]),
        )

    def test_insufficient_points_removes_files_task_and_empty_shell(self):
        db.execute("UPDATE tenants SET balance=0 WHERE id=2")
        asset_root = tempfile.mkdtemp(dir=self.tmp.name)
        slots = [
            item["slot_code"]
            for item in inspectionstandards.capture_slots("restaurant")
            if item["required"]
        ]
        prepared = [
            {"data": b"jpeg", **_photo("unused", f"{index:x}" * 64),
             "capture_slot": slot}
            for index, slot in enumerate(slots, start=1)
        ]

        async def submit():
            return await main.inspection_create(
                branch_id=self.branch["id"],
                visit_at="",
                scope="失败清理",
                request_key="route-no-balance-0001",
                industry_key="restaurant",
                files=[object() for _ in slots],
                file_slots=slots,
                template_version=inspectionstandards.CATALOG_VERSION,
                observations_json='{"metrics":[],"checklist":[]}',
            )

        with mock.patch.object(assetfiles, "ASSET_ROOT", asset_root), \
                mock.patch.object(
                    main,
                    "_prepare_inspection_uploads",
                    new=mock.AsyncMock(return_value=prepared),
                ):
            with self.assertRaises(HTTPException) as insufficient:
                asyncio.run(submit())
        self.assertEqual(402, insufficient.exception.status_code)
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) n FROM inspection_visit WHERE request_key=?",
                ("route-no-balance-0001",),
            )["n"],
        )
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) n FROM task WHERE emp_idx=?",
                (inspection.EMPLOYEE_IDX,),
            )["n"],
        )
        remaining = [
            name
            for _root, _dirs, names in os.walk(asset_root)
            for name in names
        ]
        self.assertEqual([], remaining)

    def test_disk_failure_removes_empty_shell_and_same_key_can_retry(self):
        slots = [
            item["slot_code"]
            for item in inspectionstandards.capture_slots("restaurant")
            if item["required"]
        ]
        prepared = [
            {"data": b"jpeg", **_photo("unused", f"{index:x}" * 64),
             "capture_slot": slot}
            for index, slot in enumerate(slots, start=1)
        ]
        request_key = "route-disk-retry-0001"

        async def create_once():
            return await main.inspection_create(
                branch_id=self.branch["id"],
                visit_at="",
                scope="消防与陈列",
                request_key=request_key,
                industry_key="restaurant",
                files=[object() for _ in slots],
                file_slots=slots,
                template_version=inspectionstandards.CATALOG_VERSION,
                observations_json='{"metrics":[],"checklist":[]}',
            )

        with mock.patch.object(
            main,
            "_prepare_inspection_uploads",
            new=mock.AsyncMock(return_value=prepared),
        ), mock.patch.object(
            main,
            "_store_inspection_images",
            side_effect=OSError("simulated disk failure"),
        ):
            with self.assertRaises(OSError):
                asyncio.run(create_once())
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) n FROM inspection_visit WHERE request_key=?",
                (request_key,),
            )["n"],
        )

        def stored(tid, visit_id, items):
            return [{
                **_photo(
                    f"inspections/{tid}/{visit_id}/{index:032x}.jpg",
                    f"{index:x}" * 64,
                ),
                "capture_slot": item["capture_slot"],
            } for index, item in enumerate(items, start=1)]

        with mock.patch.object(
            main,
            "_prepare_inspection_uploads",
            new=mock.AsyncMock(return_value=prepared),
        ), mock.patch.object(
            main,
            "_store_inspection_images",
            side_effect=stored,
        ), mock.patch.object(main, "_start_inspection_task"):
            retried = asyncio.run(create_once())
        self.assertTrue(retried["created"])
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM inspection_visit WHERE request_key=?",
                (request_key,),
            )["n"],
        )


if __name__ == "__main__":
    unittest.main()
