"""AI 会议成员必须实际运用岗位工作方式、能力与技能库。"""
import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app import db, departments, employeeidentity, employees, meeting


def _frozen_member(idx: int) -> dict:
    employee = employeeidentity.active_employee(idx)
    config = employees.get_config(idx)
    member = meeting.emp_brief(idx, employee=employee, config=config)
    if not employee or not member:
        raise AssertionError(f"测试员工 {idx} 不可用")
    return {
        **member,
        "_employee": employee,
        "_config": config,
        "_role_bundle": config["role_bundle"],
    }


def _set_effective_config(member: dict, **changes) -> None:
    """Build a valid in-memory approved bundle revision for context tests."""
    role_bundle = copy.deepcopy(member["_role_bundle"])
    effective = copy.deepcopy(role_bundle["effective"])
    effective["config"] = {
        **db.normalize_employee_config(effective.get("config")),
        **changes,
    }
    for field in ("skills", "settings", "caps_off", "professional_profile"):
        effective["config"][f"{field}_json"] = json.dumps(
            effective["config"].get(field) or ([] if field != "settings" and field != "professional_profile" else {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    role_bundle["effective"] = effective
    role_bundle["effective_json"] = json.dumps(
        effective, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    role_bundle["bundle_sha256"] = db.employee_role_bundle_sha256(role_bundle)
    member["_role_bundle"] = role_bundle


class MeetingEmployeeContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "meeting-context.db")
        db.conn()

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_content_employee_private_context_contains_workflow_caps_and_skills(self):
        member = _frozen_member(0)
        _set_effective_config(
            member, prompt_template="SECRET-WORKFLOW-{topic}",
        )
        with patch.object(
            meeting.employees, "skills_block", return_value="SECRET-SKILL",
        ), patch.object(
            meeting.registry, "capabilities_for",
            return_value=[
                {"name": "趋势扫描", "desc": "SECRET-CAP", "enabled": True},
                {"name": "关闭能力", "desc": "DISABLED-CAP", "enabled": False},
            ],
        ):
            system, sensitive = meeting._meeting_member_private_context(member)

        self.assertIn("SECRET-WORKFLOW", system)
        self.assertIn("SECRET-CAP", system)
        self.assertIn("SECRET-SKILL", system)
        self.assertNotIn("DISABLED-CAP", system)
        self.assertTrue(any("SECRET-WORKFLOW" in item for item in sensitive))
        self.assertTrue(any("SECRET-CAP" in item for item in sensitive))
        self.assertTrue(any("SECRET-SKILL" in item for item in sensitive))

    def test_industry_employee_respects_custom_workflow_and_disabled_caps(self):
        expert = next(
            employee for employee in departments.specialists().values()
            if employee.get("catalog_version") == "2026.08.v4"
        )
        member = _frozen_member(expert["idx"])
        _set_effective_config(
            member,
            prompt_template="SECRET-INDUSTRY-WORKFLOW-{direction}",
            caps_off=["停用"],
        )
        caps = [
            {"name": "诊断", "desc": "SECRET-INDUSTRY-CAP", "enabled": True},
            {"name": "停用", "desc": "DISABLED-INDUSTRY-CAP", "enabled": False},
        ]
        with patch.object(
            meeting.employees, "skills_block",
            return_value="SECRET-INDUSTRY-SKILL",
        ), patch.object(
            meeting.departments, "capabilities_for", return_value=caps,
        ) as capabilities:
            system, sensitive = meeting._meeting_member_private_context(member)

        capabilities.assert_called_once_with(
            expert["idx"], ["停用"], employee=expert,
        )
        self.assertIn("SECRET-INDUSTRY-WORKFLOW", system)
        self.assertNotIn("DEFAULT-HANDBOOK", system)
        self.assertIn("SECRET-INDUSTRY-CAP", system)
        self.assertNotIn("DISABLED-INDUSTRY-CAP", system)
        self.assertIn("SECRET-INDUSTRY-SKILL", system)
        self.assertTrue(any("SECRET-INDUSTRY-WORKFLOW" in item for item in sensitive))


if __name__ == "__main__":
    unittest.main()
