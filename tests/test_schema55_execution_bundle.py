"""Schema 55 execution contract for exact, approved role bundles.

These tests intentionally describe the integration boundary before the runtime
implementation is wired.  A static V4 profile is only the baseline; a task or
meeting must execute the exact approved bundle revision frozen at creation.
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import tempfile
import unittest

from app import db, departments, employeeidentity, employees, meeting, taskrunner


class Schema55ExecutionBundleContractTests(unittest.TestCase):
    def test_task_snapshot_freezes_person_and_role_bundle(self):
        source = inspect.getsource(employeeidentity.task_fields)
        self.assertIn('"person_snapshot"', source)
        self.assertIn('"bundle_sha256"', source)

    def test_task_binding_requires_exact_bundle_without_current_idx_fallback(self):
        source = inspect.getsource(employeeidentity.resolve_task_binding)
        self.assertIn("bundle_sha256", source)
        self.assertIn("role_bundle", source)
        self.assertNotIn("get_config(int(task", source)
        self.assertNotIn("get_active(int(task", source)

    def test_taskrunner_injects_effective_approved_bundle(self):
        source = inspect.getsource(taskrunner.run_task)
        for token in (
            "role_bundle",
            "effective_profile",
            "workflow",
            "capabilities",
            "skills",
        ):
            self.assertIn(token, source)
        self.assertNotIn("unapproved_artifacts", source)

    def test_meeting_uses_frozen_person_and_bundle(self):
        briefs = inspect.getsource(meeting._meeting_member_briefs)
        private = inspect.getsource(meeting._meeting_member_private_context)
        self.assertIn("person_snapshot", briefs)
        self.assertIn("bundle_sha256", briefs)
        self.assertIn("role_bundle", private)
        self.assertIn("effective_profile", private)


class Schema55EffectiveExecutionBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "schema55-execution.db")
        departments.reset_cache()
        db.conn()

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        departments.reset_cache()
        self.tmp.cleanup()

    @staticmethod
    def _approved_overlay(config: dict) -> dict:
        role_bundle = copy.deepcopy(config["role_bundle"])
        effective = copy.deepcopy(role_bundle["effective"])
        effective_profile = copy.deepcopy(effective["professional_profile"])
        effective_profile.setdefault("capabilities", []).append(
            "SCHEMA55-APPROVED-CAPABILITY"
        )
        effective["professional_profile"] = effective_profile
        effective["workflow"] = [
            *list(effective.get("workflow") or []),
            "SCHEMA55-APPROVED-WORKFLOW",
        ]
        effective_config = db.normalize_employee_config(effective["config"])
        effective_config["skills"] = [{
            "title": "SCHEMA55-APPROVED-SKILL",
            "detail": "只来自已审批能力包",
            "enabled": True,
        }]
        effective_config["skills_json"] = json.dumps(
            effective_config["skills"], ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        effective["config"] = effective_config
        role_bundle["effective"] = effective
        role_bundle["effective_json"] = json.dumps(
            effective, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        role_bundle["bundle_sha256"] = db.employee_role_bundle_sha256(
            role_bundle
        )
        return role_bundle

    def test_taskrunner_consumes_only_approved_effective_overlay(self):
        employee = employeeidentity.active_employee(1001)
        config = employees.ensure_role_config(employee)
        role_bundle = self._approved_overlay(config)
        context = taskrunner._approved_effective_role_context({
            "employee": employee,
            "config": config,
            "role_bundle": role_bundle,
        })
        self.assertIn(
            "SCHEMA55-APPROVED-CAPABILITY", context["capabilities"]
        )
        self.assertIn(
            "SCHEMA55-APPROVED-WORKFLOW", context["workflow"]
        )
        self.assertEqual(
            "SCHEMA55-APPROVED-SKILL", context["skills"][0]["title"]
        )
        self.assertIn("SCHEMA55-APPROVED-SKILL", context["approved_context"])

    def test_approved_context_is_deduplicated_and_fingerprinted(self):
        """能力包注入必须是去重可读文本，不再重复整包压缩 JSON。

        技能明细/能力项/一致的决策合同已由同一批准配置在 system 其他块全文
        渲染；重复注入实测每单多花 1k~6k 字符且原始 JSON 可读性差。
        """
        employee = employeeidentity.active_employee(1001)
        config = employees.ensure_role_config(employee)
        role_bundle = self._approved_overlay(config)
        context = taskrunner._approved_effective_role_context({
            "employee": employee,
            "config": config,
            "role_bundle": role_bundle,
        })
        approved = context["approved_context"]
        self.assertIn("版本指纹", approved)
        self.assertIn(str(role_bundle["bundle_sha256"])[:16], approved)
        # 技能标题保留（批准语义可核对），明细不再第二次注入：
        # 上文技能库块已按同一 effective 配置全文渲染。
        self.assertIn("SCHEMA55-APPROVED-SKILL", approved)
        self.assertNotIn("只来自已审批能力包", approved)
        # 不再是原始压缩 JSON 整包。
        self.assertNotIn('"professional_profile"', approved)
        # 与目录版本一致的决策合同只引用、不重复全文。
        if role_bundle["effective"].get("decision_contract"):
            self.assertIn("同一批准版本", approved)
        # 泄露检测指纹源必须保持旧实现同构的 JSON 形态：可读渲染逐行是
        # 自然中文，按 32 字逐行指纹会把交付中正常运用岗位口径误杀。
        leak_source = context["approved_sensitive"]
        self.assertIn('"professional_profile"', leak_source)
        self.assertIn("SCHEMA55-APPROVED-SKILL", leak_source)

    def test_proposed_bundle_is_inert_and_cannot_execute(self):
        employee = employeeidentity.active_employee(1001)
        config = employees.ensure_role_config(employee)
        role_bundle = self._approved_overlay(config)
        role_bundle["status"] = "proposed"
        with self.assertRaisesRegex(ValueError, "未批准"):
            taskrunner._approved_effective_role_context({
                "employee": employee,
                "config": config,
                "role_bundle": role_bundle,
            })


if __name__ == "__main__":
    unittest.main()
