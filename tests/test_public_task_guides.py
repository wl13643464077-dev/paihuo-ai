"""公开派活指引必须按行业和岗位变化，同时守住岗位档案边界。"""
import json
import unittest
from unittest.mock import patch

from app import departments, main
from app.skills import registry


GUIDE_KEYS = {
    "task_placeholder",
    "industry_placeholder",
    "material_placeholder",
    "input_tips",
    "output_hint",
}
FORBIDDEN_PUBLIC_FIELDS = {
    "capabilities",
    "workflow",
    "skills",
    "md",
    "default_template",
    "steps",
    "inputs",
    "deliverables",
    "duty",
    "desc",
    "prompt_template",
}


def _guide_text(guide: dict) -> str:
    return json.dumps(guide, ensure_ascii=False, sort_keys=True)


class PublicExpertTaskGuideTests(unittest.TestCase):
    def test_public_role_catalog_exactly_covers_all_97_published_roles(self):
        published_names = {
            employee["name"]
            for employee in departments.specialists().values()
        }
        configured_names = set(departments._PUBLIC_ROLE_GUIDES)
        self.assertEqual(97, len(published_names))
        self.assertEqual(published_names, configured_names)
        for name, role in departments._PUBLIC_ROLE_GUIDES.items():
            self.assertEqual(
                {"focus", "materials", "input_tips", "output_hint"},
                set(role),
                name,
            )
            self.assertEqual(3, len(role["input_tips"]), name)
            self.assertTrue(
                all(
                    role[key]
                    for key in ("focus", "materials", "output_hint")
                )
            )
            self.assertTrue(all(role["input_tips"]))

    def test_unknown_role_is_rejected_instead_of_using_generic_fallback(self):
        with self.assertRaisesRegex(
            departments.DepartmentConfigError,
            "尚未配置专属派活指引",
        ):
            departments.public_task_guide(
                {
                    "name": "尚未登记的新岗位",
                    "dept_key": "auto",
                }
            )

    def test_all_specialists_have_unique_industry_and_role_guides(self):
        specialists = departments.specialists()
        self.assertGreaterEqual(len(specialists), 400)
        rendered = {}
        for idx, employee in specialists.items():
            guide = departments.public_task_guide(employee)
            self.assertEqual(GUIDE_KEYS, set(guide), idx)
            self.assertGreaterEqual(len(guide["input_tips"]), 3, idx)
            self.assertIn(employee["name"], guide["task_placeholder"], idx)
            self.assertTrue(guide["industry_placeholder"].startswith("例如："), idx)
            self.assertTrue(guide["material_placeholder"], idx)
            self.assertTrue(guide["output_hint"], idx)
            text = _guide_text(guide)
            self.assertNotIn(
                text,
                rendered,
                f"{idx} 与 {rendered.get(text)} 错用了同一份行业岗位指引",
            )
            rendered[text] = idx

    def test_36_standard_roles_remain_substantively_distinct_without_role_name(self):
        specialists = list(departments.specialists().values())
        one_industry = {
            employee["name"]: employee
            for employee in specialists
            if employee["dept_key"] == "auto"
        }
        self.assertEqual(36, len(one_industry))

        without_role_name = {}
        for name, employee in one_industry.items():
            text = _guide_text(departments.public_task_guide(employee))
            text = text.replace(name, "")
            self.assertNotIn(
                text,
                without_role_name,
                f"{name} 与 {without_role_name.get(text)} 只有岗位名不同",
            )
            without_role_name[text] = name

    def test_semantic_sentinels_do_not_regress_to_wrong_role_families(self):
        def guide_text(name):
            return _guide_text(
                departments.public_task_guide(
                    {"name": name, "dept_key": "hotel"}
                )
            )

        trace = guide_text("追溯、撤回与召回")
        for marker in ("问题批次", "上游来源", "隔离通知"):
            self.assertIn(marker, trace)
        for wrong in ("目标客群与触达渠道", "面向增长落地", "召回激励"):
            self.assertNotIn(wrong, trace)

        continuity = guide_text("业务连续性与数据治理")
        for marker in ("关键数据", "备份恢复", "权限"):
            self.assertIn(marker, continuity)
        for wrong in ("损益桥", "预算差异", "经营财务"):
            self.assertNotIn(wrong, continuity)

        incident = guide_text("投诉、事故与服务恢复")
        for marker in ("当前安全状态", "即时控制", "恢复条件"):
            self.assertIn(marker, incident)
        for wrong in ("在售项目", "组合架构", "商品生命周期"):
            self.assertNotIn(wrong, incident)

        crm = guide_text("CRM、忠诚度与生命周期")
        for marker in ("客户关系阶段", "会员权益", "留存指标"):
            self.assertIn(marker, crm)
        for wrong in ("保留新增方向", "组合成员", "库存处理"):
            self.assertNotIn(wrong, crm)

    def test_non_restaurant_guides_never_contain_restaurant_template_words(self):
        bad_words = ("菜单", "菜品", "餐饮")
        offenders = []
        for idx, employee in departments.specialists().items():
            if employee.get("dept_key") == "restaurant":
                continue
            text = _guide_text(departments.public_task_guide(employee))
            hit = [word for word in bad_words if word in text]
            if hit:
                offenders.append((idx, employee.get("dept_key"), hit))
        self.assertEqual([], offenders)

    def test_guide_does_not_copy_internal_profile_fields(self):
        secrets = {
            "inputs": ["INTERNAL_INPUT_SENTINEL"],
            "deliverables": ["INTERNAL_DELIVERY_SENTINEL"],
            "steps": ["INTERNAL_WORKFLOW_SENTINEL"],
            "md": "INTERNAL_HANDBOOK_SENTINEL",
            "capabilities": ["INTERNAL_CAPABILITY_SENTINEL"],
            "default_template": "INTERNAL_TEMPLATE_SENTINEL",
        }
        employee = {
            "idx": 999999,
            "name": "市场容量与趋势雷达",
            "dept_key": "auto",
            "dept_name": "汽车后市场产业部",
            "intro": "公开介绍",
            **secrets,
        }
        public = main._public_expert(employee, include_task_guide=True)
        text = json.dumps(public, ensure_ascii=False)
        for values in secrets.values():
            for secret in values if isinstance(values, list) else [values]:
                self.assertNotIn(secret, text)
        self.assertTrue(FORBIDDEN_PUBLIC_FIELDS.isdisjoint(public))
        self.assertEqual(GUIDE_KEYS, set(public["task_guide"]))

    def test_each_industry_uses_its_own_business_context(self):
        expected = {
            "auto": "工单",
            "beauty": "预约到店",
            "convenience": "来客",
            "fitness": "课耗",
            "grocery": "生鲜损耗",
            "hotel": "入住率",
            "pet": "宠物",
            "pharmacy": "处方",
            "restaurant": "菜单",
            "snack": "零食",
            "tea_coffee": "杯量",
        }
        first_by_dept = {}
        for employee in departments.specialists().values():
            first_by_dept.setdefault(employee["dept_key"], employee)
        self.assertEqual(set(expected), set(first_by_dept))
        for dept_key, marker in expected.items():
            guide = departments.public_task_guide(first_by_dept[dept_key])
            self.assertIn(marker, _guide_text(guide), dept_key)


class PublicContentTaskGuideTests(unittest.TestCase):
    def test_all_content_employees_have_distinct_safe_guides(self):
        rendered = set()
        material_prompts = set()
        for station in registry.STATIONS:
            public = main._public_station(station, include_task_guide=True)
            guide = public["task_guide"]
            self.assertEqual(GUIDE_KEYS, set(guide), station["key"])
            text = _guide_text(guide)
            self.assertNotIn(text, rendered)
            rendered.add(text)
            self.assertNotIn(
                guide["material_placeholder"],
                material_prompts,
                f"{station['key']} 仍在复用别的岗位材料提示",
            )
            material_prompts.add(guide["material_placeholder"])
            for word in ("菜单", "菜品", "餐饮"):
                self.assertNotIn(word, text)
            self.assertTrue(FORBIDDEN_PUBLIC_FIELDS.isdisjoint(public))
        self.assertIn(
            "品牌定位",
            main._PUBLIC_STATION_TASK_GUIDES["trend"][
                "material_placeholder"
            ],
        )
        self.assertIn(
            "待核实",
            main._PUBLIC_STATION_TASK_GUIDES["research"][
                "material_placeholder"
            ],
        )
        self.assertIn(
            "对标账号",
            main._PUBLIC_STATION_TASK_GUIDES["benchmark"][
                "material_placeholder"
            ],
        )

    def test_tour_gets_intro_only_but_member_gets_task_guide(self):
        config = {
            "prompt_template": "",
            "skills": [],
            "learned_at": None,
            "settings": {},
            "caps_off": [],
            "enabled": True,
        }
        with patch.object(main.employees, "get_config", return_value=config):
            with patch.object(
                main.auth, "current", return_value={"role": "tour"}
            ):
                tour_rows = main.employees_list()
            with patch.object(
                main.auth, "current", return_value={"role": "member"}
            ):
                member_rows = main.employees_list()
        self.assertTrue(tour_rows)
        self.assertTrue(member_rows)
        self.assertTrue(all("task_guide" not in row for row in tour_rows))
        self.assertTrue(all("task_guide" in row for row in member_rows))

    def test_expert_list_omits_bulk_guides_and_detail_follows_tour_boundary(self):
        config = {
            "prompt_template": "",
            "skills": [],
            "learned_at": None,
            "settings": {},
            "caps_off": [],
            "enabled": True,
        }
        depts = departments.list_depts()
        sample = {
            **depts[0]["employees"][0],
            "dept_key": depts[0]["key"],
            "dept_name": depts[0]["name"],
        }
        cfgs = {
            e["idx"]: config
            for dept in depts
            for e in dept["employees"]
        }

        def list_for(role):
            with (
                patch.object(main.auth, "current", return_value={"role": role}),
                patch.object(main.auth, "tenant_id", return_value=-1 if role == "tour" else 2),
                patch.object(main.auth, "dept_visible", return_value=True),
                patch.object(main.db, "q", return_value=[]),
                patch.object(main.employees, "get_configs", return_value=cfgs),
            ):
                return main.depts_list()

        tour_list = list_for("tour")
        member_list = list_for("member")
        self.assertNotIn("task_guide", tour_list[0]["employees"][0])
        self.assertNotIn("task_guide", member_list[0]["employees"][0])

        def detail_for(role):
            with (
                patch.object(
                    main.auth,
                    "current",
                    return_value={
                        "role": role,
                        "tenant_id": -1 if role == "tour" else 2,
                    },
                ),
                patch.object(main, "_need_module"),
                patch.object(main.departments, "get", return_value=sample),
                patch.object(main.employees, "get_config", return_value=config),
                patch.object(main.db, "q", return_value=[]),
                patch.object(main.db, "one", return_value={}),
            ):
                return main.dept_emp(sample["idx"])

        self.assertNotIn("task_guide", detail_for("tour"))
        self.assertIn("task_guide", detail_for("member"))


if __name__ == "__main__":
    unittest.main()
