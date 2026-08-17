"""行业专家的 steps/deliverables 必须真实反映岗位手册。

steps 是 departments.capabilities_for() 的唯一数据源:它决定老板在员工面板上
能开关哪些能力,也决定注入提示词的「本次启用的工作流步骤」。历史上这个字段
被写成了交付物清单(360 人)或压根没解析出来(餐饮 46 人),等于能力开关对
400+ 名员工是错的或失效的。这些断言把修复钉住。
"""
import glob
import json
import os
import re
import unittest

from app import departments

DEPT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "departments",
)


def _load_all():
    employees = []
    for path in sorted(glob.glob(os.path.join(DEPT_DIR, "*.json"))):
        with open(path, encoding="utf-8") as handle:
            dept = json.load(handle)
        for employee in dept.get("employees") or []:
            employees.append((os.path.basename(path), dept.get("key"), employee))
    return employees


class DepartmentStepDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.employees = _load_all()

    def test_corpus_is_present(self):
        self.assertGreaterEqual(len(self.employees), 400)

    def test_every_employee_has_workflow_steps(self):
        """一个都不能少:steps 为空的员工在前端没有任何能力项可开关。"""
        missing = [
            f"{f}#{e.get('num')} {e.get('name')}"
            for f, _key, e in self.employees
            if not (e.get("steps") or [])
        ]
        self.assertEqual([], missing, f"{len(missing)} 名员工缺少工作流步骤")

    def test_steps_are_not_a_copy_of_deliverables(self):
        """steps 抄 deliverables 是这次修复的原始缺陷,不许回潮。"""
        offenders = []
        for filename, _key, employee in self.employees:
            steps = set(employee.get("steps") or [])
            delivs = set(employee.get("deliverables") or [])
            if steps and delivs and len(steps & delivs) >= 3:
                offenders.append(f"{filename}#{employee.get('num')}")
        self.assertEqual([], offenders, f"{len(offenders)} 名员工的步骤仍是交付物清单")

    def test_deliverables_carry_no_parser_residue(self):
        """按顿号硬切句子会留下「…。输出顺序固定为：结论摘要」这类残片。"""
        offenders = []
        for filename, _key, employee in self.employees:
            for item in employee.get("deliverables") or []:
                if "输出顺序固定为" in item or item.endswith("。"):
                    offenders.append(f"{filename}#{employee.get('num')}: {item[:40]}")
        self.assertEqual([], offenders, f"{len(offenders)} 条交付物含解析残片")

    def test_no_markdown_emphasis_leaks_into_labels(self):
        """步骤文本会直接当能力名显示并注入提示词,不该带 ** 标记。"""
        offenders = [
            f"{f}#{e.get('num')}"
            for f, _key, e in self.employees
            for item in (e.get("steps") or []) + (e.get("deliverables") or [])
            if "**" in item
        ]
        self.assertEqual([], offenders)

    def test_idx_values_are_unique(self):
        seen = {}
        for filename, _key, employee in self.employees:
            idx = employee.get("idx")
            self.assertNotIn(idx, seen, f"idx {idx} 在 {filename} 与 {seen.get(idx)} 重复")
            seen[idx] = filename


class CapabilityRenderingTests(unittest.TestCase):
    """capabilities_for 是前端能力面板与提示词注入的出口。"""

    def test_every_specialist_exposes_capabilities(self):
        specialists = departments.specialists()
        # Schema 55 keeps 360 V4 industry people active, plus restaurant's 60
        # unchanged specialists.
        self.assertEqual(420, len(specialists))
        # The legacy idx-keyed compatibility map still collapses same-slot
        # versions, while the lossless registry retains V1 + V2 + V3.
        self.assertEqual(420, len(departments.legacy_specialists()))
        self.assertEqual(780, len(departments.historical_specialists()))
        # The active-first idx compatibility view keeps the 360 shared slots
        # once and also retains the 60 independent 20xxx historical ids.
        self.assertEqual(480, len(departments.all_specialists()))
        self.assertEqual(1200, len(departments.all_identity_versions()))
        empty = [
            idx for idx in specialists
            if not departments.capabilities_for(idx, [])
        ]
        self.assertEqual([], empty, f"{len(empty)} 名专家没有可开关的能力项")

    def test_capability_names_are_unique_within_an_employee(self):
        """名字即开关键:同名会让关一个连带关掉另一个。"""
        for idx in departments.specialists():
            caps = departments.capabilities_for(idx, [])
            names = [c["name"] for c in caps]
            self.assertEqual(
                len(names), len(set(names)),
                f"专家 {idx} 的能力项名字重复: {names}",
            )

    def test_capability_names_are_label_sized_and_clean(self):
        pattern = re.compile(r"^第\d+步$")
        for idx in departments.specialists():
            for cap in departments.capabilities_for(idx, []):
                name = cap["name"]
                self.assertTrue(name, f"专家 {idx} 有空能力名")
                self.assertNotIn("**", name)
                if not pattern.match(name):
                    self.assertLessEqual(
                        len(name), 24,
                        f"专家 {idx} 的能力名过长: {name}",
                    )

    def test_disabling_a_capability_is_honoured(self):
        idx = sorted(departments.specialists())[0]
        caps = departments.capabilities_for(idx, [])
        self.assertTrue(caps)
        target = caps[0]["name"]
        toggled = departments.capabilities_for(idx, [target])
        by_name = {c["name"]: c["enabled"] for c in toggled}
        self.assertFalse(by_name[target])
        self.assertTrue(all(v for k, v in by_name.items() if k != target))

    def test_capability_desc_keeps_the_full_step_text(self):
        """名字可以被截短,但注入提示词的 desc 必须是完整步骤。"""
        for idx in sorted(departments.specialists())[:20]:
            employee = departments.get(idx)
            caps = departments.capabilities_for(idx, [])
            self.assertEqual(
                [c["desc"] for c in caps],
                list(employee.get("steps") or []),
            )

    def test_v4_capabilities_carry_display_detail_without_touching_desc(self):
        """detail 只服务界面展示；desc（提示词载荷）必须保持档案原文不变。"""
        idx = 1001  # V4 行业员工：能力行来自专业岗位档案而非 steps
        employee = departments.get(idx)
        profile_caps = list(
            (employee.get("professional_profile") or {}).get("capabilities")
            or []
        )
        self.assertTrue(profile_caps, "1001 应有 V4 岗位档案能力")
        caps = departments.capabilities_for(idx, [])
        self.assertEqual([c["desc"] for c in caps], profile_caps)
        detailed = [c for c in caps if c.get("detail")]
        self.assertEqual(
            len(profile_caps), len(detailed),
            "V4 员工每条能力都应有 detail 详细介绍（capability_details_v1.json）",
        )
        for cap in detailed:
            self.assertGreaterEqual(len(cap["detail"]), 20)
            self.assertNotEqual(cap["detail"], cap["desc"])

    def test_restaurant_and_content_roles_have_factory_profiles(self):
        """餐饮/内容部老岗位升级到 V4 展示标准：出厂档案 + 每项详细介绍。

        sidecar 只服务展示层：冻结身份、配置包与任务提示词永远不读它，
        技能卡（skills_json）也一个字不动。
        """
        from app.skills import registry as skills_registry

        with open(
            os.path.join(DEPT_DIR, "restaurant.json"), encoding="utf-8",
        ) as handle:
            restaurant = json.load(handle)
        keys = [s["key"] for s in skills_registry.STATIONS]
        keys += [e["key"] for e in restaurant.get("employees") or []]
        self.assertGreaterEqual(len(keys), 70)
        for key in keys:
            row = departments.factory_profile_for(key)
            self.assertTrue(row, f"{key} 缺出厂能力档案")
            profile = row["professional_profile"]
            self.assertTrue(profile.get("scope"), f"{key} 缺职责域")
            details = row["capability_details"]
            for group in ("skill_tree", "capabilities"):
                items = profile.get(group) or []
                self.assertGreaterEqual(len(items), 3, f"{key} {group} 过薄")
                for item in items:
                    self.assertGreaterEqual(
                        len((details.get(group) or {}).get(item, "")), 20,
                        f"{key} {group} «{item}» 缺详细介绍",
                    )
        self.assertEqual({}, departments.factory_profile_for("no_such_role"))


if __name__ == "__main__":
    unittest.main()
