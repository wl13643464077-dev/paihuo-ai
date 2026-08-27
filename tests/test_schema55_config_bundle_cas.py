"""Schema55 config and effective role-bundle revisions advance atomically."""

from __future__ import annotations

import os
import tempfile
import unittest

import hashlib

from app import db, departments, employeeidentity, employeelearning, employees


class Schema55ConfigBundleCasCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "schema55-config-bundle.db")
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

    def test_prompt_revision_creates_matching_bundle_in_same_transaction(self):
        employee = employeeidentity.active_employee(1001)
        before = employees.ensure_role_config(employee)
        before_bundle = before["role_bundle"]

        employees.set_prompt_for_identity(
            before["identity_ref"],
            "只使用已批准证据的工作法",
            expected_revision=before["config_revision"],
        )

        after = employees.get_config_by_identity(before["identity_ref"])
        self.assertIsNotNone(after)
        self.assertEqual(before["config_revision"] + 1, after["config_revision"])
        self.assertEqual("只使用已批准证据的工作法", after["prompt_template"])
        self.assertRegex(after["bundle_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(before["bundle_sha256"], after["bundle_sha256"])
        self.assertEqual(
            after["config_sha256"], after["role_bundle"]["config_sha256"]
        )
        self.assertEqual(
            after["prompt_template"],
            after["role_bundle"]["effective"]["config"]["prompt_template"],
        )

        historical = db.one(
            "SELECT * FROM employee_role_bundle_revision "
            "WHERE identity_ref=? AND config_revision=?",
            (before["identity_ref"], before["config_revision"]),
        )
        self.assertEqual("historical", historical["status"])
        self.assertEqual(before_bundle["bundle_sha256"], historical["bundle_sha256"])
        self.assertTrue(db.employee_role_bundle_row_valid(historical))

    def test_stale_config_cas_cannot_create_orphan_bundle(self):
        before = employees.get_config(1001)
        employees.set_settings_for_identity(
            before["identity_ref"], {"mode": "evidence"},
            expected_revision=before["config_revision"],
        )
        with self.assertRaisesRegex(RuntimeError, "已更新"):
            employees.set_caps_off_for_identity(
                before["identity_ref"], ["web"],
                expected_revision=before["config_revision"],
            )
        rows = db.q(
            "SELECT config_revision FROM employee_role_bundle_revision "
            "WHERE identity_ref=? ORDER BY config_revision",
            (before["identity_ref"],),
        )
        self.assertEqual(
            [before["config_revision"], before["config_revision"] + 1],
            [int(row["config_revision"]) for row in rows],
        )

    def test_approved_evidence_updates_knowledge_skill_capability_and_workflow(self):
        employee = employeeidentity.active_employee(1001)
        before = employees.get_config(1001)
        batch = employeelearning.create_batch(
            "v4-learning-batch-1001", budget_cap_points=3
        )
        run = employeelearning.create_run(
            batch["id"], "v4-learning-run-1001",
            employee_idx=1001, identity_ref=before["identity_ref"],
            base_config_revision=before["config_revision"],
            base_config_sha256=before["config_sha256"],
            industry_key="tea_coffee", budget_points=3,
        )
        employeelearning.start_run(run["id"])
        sources = []
        for index in range(5):
            sources.append({
                "url": f"https://evidence{index % 3 + 1}.example/rule/{index}",
                "title": f"岗位证据 {index}",
                "publisher": "行业权威机构" if index == 0 else "行业研究机构",
                "authority_level": "official" if index == 0 else "research",
                "published_at": "2026-07-01", "fetched_at": 1720000000 + index,
                "http_status": 200, "tls_valid": True,
                "content_sha256": hashlib.sha256(
                    f"evidence-content-{index}".encode()
                ).hexdigest(),
                "excerpt": f"与十五分钟需求决策相关的可核查证据 {index}",
                "capture_event_id": f"websearch-capture-{index}",
                "capture_provider": "websearch",
            })
        artifacts = [
            {"kind": "knowledge", "title": "事件日需求知识",
             "statement": "识别会展与天气对十五分钟需求的分层影响",
             "payload": {}, "source_indexes": [1, 2]},
            {"kind": "skill", "title": "拾取曲线断点校验",
             "statement": "用滚动原点识别需求拾取断点并标注置信区间",
             "payload": {}, "source_indexes": [2, 3]},
            {"kind": "capability", "title": "事件脉冲评估能力",
             "statement": "区分基线需求与外部事件脉冲并给出人工复核阈值",
             "payload": {}, "source_indexes": [3, 4]},
            {"kind": "workflow", "title": "预测发布前证据门禁",
             "statement": "发布前核对事件日历、数据截止点与滚动验证误差",
             "payload": {"step": "核对事件日历与滚动验证误差后再提交预测"},
             "source_indexes": [4, 5]},
        ]
        employeelearning.build_learning_proposal(run["id"], sources, artifacts)
        approved = employeelearning.approve_run(
            run["id"],
            lambda **kwargs: employees.activate_learning_bundle(
                **kwargs, expected_bundle_sha256=before["bundle_sha256"]
            ),
            reviewer_id=1,
        )
        self.assertEqual("activated", approved["status"])
        after = employees.get_config(1001)
        self.assertEqual(before["config_revision"] + 1, after["config_revision"])
        effective = after["role_bundle"]["effective"]
        self.assertIn("外部事件脉冲", " ".join(
            effective["professional_profile"]["capabilities"]
        ))
        self.assertIn("核对事件日历", " ".join(effective["workflow"]))
        self.assertTrue(effective["learning_evidence"])
        self.assertTrue(after["skills"])


if __name__ == "__main__":
    unittest.main()
