"""Independent acceptance tests for the 360-role V3 auditor."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
AUDITOR_PATH = ROOT / "tools" / "audit_industry_decisions_v3.py"


def _auditor():
    spec = importlib.util.spec_from_file_location("industry_decision_v3_auditor", AUDITOR_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("V3 auditor cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IndustryDecisionV3AuditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auditor = _auditor()
        cls.tea = json.loads(
            (ROOT / "data" / "industry_decisions_v3" / "tea_coffee.json")
            .read_text(encoding="utf-8")
        )

    def test_all_360_roles_pass_the_generator_independent_audit(self):
        report, errors = self.auditor.run()
        self.assertEqual([], errors)
        self.assertTrue(report["passed"])
        self.assertEqual(10, report["catalogs"])
        self.assertEqual(360, report["employees"])
        self.assertEqual(420, report["expected_active_industry_employees"])
        self.assertEqual(431, report["expected_active_total_with_core"])

    def test_runtime_loads_v4_current_and_preserves_v3_history(self):
        from app import departments

        departments.reset_cache()
        specialists = departments.specialists()
        self.assertEqual(420, len(specialists))
        self.assertEqual(360, sum(
            employee.get("catalog_version") == "2026.08.v4"
            for employee in specialists.values()
        ))
        identities = departments.all_identity_versions()
        self.assertEqual(1200, len(identities))
        self.assertEqual(360, sum(
            employee.get("catalog_version") == "2026.08.v3"
            for employee in identities
        ))

    def test_deprecated_b_generator_cannot_overwrite_catalogs(self):
        paths = sorted((ROOT / "data" / "industry_decisions_v3").glob("*.json"))
        before = {path: path.read_bytes() for path in paths}
        result = subprocess.run(
            [sys.executable, "-B", str(
                ROOT / "tools" / "generate_industry_decisions_v3_b.py"
            )],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("DEPRECATED", result.stderr + result.stdout)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_beauty_sensitive_image_role_is_backed_by_official_pipl(self):
        catalog = json.loads(
            (ROOT / "data" / "industry_decisions_v3" / "beauty.json")
            .read_text(encoding="utf-8")
        )
        sources = {source["id"]: source for source in catalog["sources"]}
        pipl = sources["beauty-pipl-2021"]
        self.assertEqual("official", pipl["source_type"])
        self.assertEqual(
            "https://www.npc.gov.cn/npc/c2/c30834/202108/t20210820_313088.html",
            pipl["url"],
        )
        pain = next(
            pain for pain in catalog["pain_points"]
            if pain["code"] == "BEAUTYV3-PRIVACY-FINANCE-CONTINUITY"
        )
        self.assertIn("beauty-pipl-2021", pain["source_ids"])

    def test_pet_welfare_role_has_direct_welfare_and_premises_sources(self):
        catalog = json.loads(
            (ROOT / "data" / "industry_decisions_v3" / "pet.json")
            .read_text(encoding="utf-8")
        )
        sources = {source["id"]: source for source in catalog["sources"]}
        self.assertIn("现行推荐性国家标准", sources["pet-premises-cleaning-2025"]["scope"])
        self.assertIn("不冒充中国法定阈值", sources["pet-welfare-woah-2021"]["scope"])
        pain = next(
            pain for pain in catalog["pain_points"]
            if pain["code"] == "PETV3-WELFARE-DATA-CONTINUITY"
        )
        self.assertLessEqual(
            {"pet-welfare-woah-2021", "pet-premises-cleaning-2025"},
            set(pain["source_ids"]),
        )

    def test_generic_role_skeleton_is_rejected_even_when_business_nouns_change(self):
        employee = copy.deepcopy(self.tea["employees"][0])
        contract = employee["decision_contract"]
        profile = employee["professional_profile"]
        contract["workflow"] = [
            "冻结判断对象、门店范围、数据时点、批准责任与业务规则",
            "校验输入", "计算结果", "形成建议",
            "标明GO/HOLD/ESCALATE/ADVISE依据",
        ]
        contract["success_metrics"][1]["name"] = "人工复核可解释率"
        contract["success_metrics"][1]["formula"] = "能回链反证及人工结论的数量÷总数×100%"
        contract["success_metrics"][2]["name"] = "到期关闭率"
        contract["success_metrics"][2]["formula"] = "批准截止前补齐证据并关闭的数量÷总数×100%"
        profile["tool_permissions"][0]["tool"] = "企业事实查询器"
        profile["learning_tracks"] = [
            "方法进修：业务方法", "案例进修：业务案例", "结果校准：持续复盘",
        ]
        profile["escalation_matrix"][0]["condition"] = (
            "工具仍无法对齐同一对象与时点，则停止当前判断"
        )
        audit = self.auditor.Audit()
        self.auditor._audit_role(audit, "tea_coffee", employee)
        joined = "\n".join(audit.errors)
        self.assertIn("real professional workflow", joined)
        self.assertIn("all three metrics must be native business metrics", joined)
        self.assertIn("role-specific read-only systems", joined)
        self.assertIn("learning tracks", joined)
        self.assertIn("escalation condition", joined)

    def test_skill_and_object_substitution_metric_is_rejected(self):
        employee = copy.deepcopy(self.tea["employees"][0])
        contract = employee["decision_contract"]
        contract["success_metrics"][1]["name"] = "批次复核一次通过率"
        contract["success_metrics"][1]["formula"] = (
            "无需返工即通过批次复核的冷链批次记录数 ÷ "
            "同期进入批次复核的冷链批次记录总数 × 100%；分母为0时记N/A"
        )
        contract["success_metrics"][2]["name"] = "异常升级正常完成率"
        contract["success_metrics"][2]["formula"] = (
            "在温控报警后完成异常升级且隔离台账无未决冲突的事项数 ÷ "
            "同期应完成异常升级的事项总数 × 100%；分母为0时记N/A"
        )
        audit = self.auditor.Audit()
        self.auditor._audit_role(audit, "attack", employee)
        matches = [
            row for row in audit.errors
            if "metric formula is a skill/object substitution template" in row
        ]
        self.assertEqual(2, len(matches), audit.errors)

    def test_similarity_check_detects_a_role_profile_clone(self):
        catalog = copy.deepcopy(self.tea)
        first, second = catalog["employees"][:2]
        second["professional_profile"]["capabilities"] = copy.deepcopy(
            first["professional_profile"]["capabilities"]
        )
        audit = self.auditor.Audit()
        result = self.auditor._audit_similarity(
            audit,
            "tea_coffee",
            catalog,
            "capabilities",
            lambda employee: employee["professional_profile"]["capabilities"],
        )
        self.assertLess(result["unique"], 36)
        self.assertTrue(any("exact role skeleton duplicated" in row for row in audit.errors))

    def test_cross_industry_check_detects_a_shared_workflow_skeleton(self):
        left = copy.deepcopy(self.tea["employees"][0])
        right = copy.deepcopy(self.tea["employees"][1])
        right["decision_contract"]["workflow"] = copy.deepcopy(
            left["decision_contract"]["workflow"]
        )
        other_catalog = copy.deepcopy(self.tea)
        other_catalog["key"] = "fake_other_industry"
        audit = self.auditor.Audit()
        result = self.auditor._audit_cross_industry_similarity(
            audit,
            [
                ("tea_coffee", self.tea, left),
                ("fake_other_industry", other_catalog, right),
            ],
            "workflow",
            lambda employee: employee["decision_contract"]["workflow"],
        )
        self.assertLess(result["unique"], 2)
        self.assertTrue(
            any("duplicated across industries" in row for row in audit.errors)
        )


if __name__ == "__main__":
    unittest.main()
