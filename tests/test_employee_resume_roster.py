"""AI 场景专岗附加花名册 + 员工人设履历。"""
import json
import os
import re
import unittest

from app import departments, resume

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class SceneRosterTests(unittest.TestCase):
    def test_27_scene_roles_live_in_their_own_range_and_are_assignable(self):
        scene = [e for e in departments.specialists().values() if e.get("scene_role")]
        self.assertEqual(27, len(scene))
        for employee in scene:
            self.assertIn(employee["idx"], departments.SCENE_IDX_RANGE)
            self.assertEqual("AI场景专岗", employee["group"])
            self.assertTrue(employee["can_assign"])
            self.assertEqual("v1", employee["catalog_version"])
            self.assertTrue(employee.get("md"))
            guide = departments.public_task_guide(employee)
            self.assertEqual(3, len(guide["input_tips"]))
            self.assertTrue(departments.capabilities_for(employee["idx"], []))
        per_dept = {}
        for employee in scene:
            per_dept[employee["dept_key"]] = per_dept.get(employee["dept_key"], 0) + 1
        self.assertEqual(
            {"convenience", "snack", "grocery", "pharmacy", "hotel", "auto",
             "fitness", "beauty", "pet"},
            set(per_dept),
        )
        self.assertTrue(all(n == 3 for n in per_dept.values()))

    def test_scene_people_and_names_never_collide_with_frozen_roster(self):
        active = list(departments.specialists().values())
        for field in ("idx", "key", "name"):
            values = [e.get(field) for e in active]
            self.assertEqual(len(values), len(set(values)), field)
        people = [e.get("person") for e in active if e.get("person")]
        self.assertEqual(len(people), len(set(people)))

    def test_generated_sidecar_matches_spec(self):
        with open(os.path.join(ROOT, "app", "scene_roles_v1.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        with open(os.path.join(ROOT, "app", "factory_profiles_v1.json"), encoding="utf-8") as handle:
            profiles = json.load(handle)
        for dept in data["departments"].values():
            for employee in dept["employees"]:
                self.assertIn(employee["key"], profiles)
                # 没有硬件依赖的场景：不出现设备/机器人类岗位
                self.assertNotRegex(employee["name"], r"机器人|设备|传感|智能秤|货架")


class ResumeTests(unittest.TestCase):
    def _all(self):
        for dept in departments.list_depts():
            for employee in dept["employees"]:
                yield {**employee, "dept_key": dept["key"], "dept_name": dept["name"]}

    def test_every_active_employee_gets_a_complete_resume(self):
        count = 0
        for employee in self._all():
            r = resume.for_employee(employee)
            count += 1
            self.assertTrue(r["headline"].startswith(employee["name"]) or r["source"] == "override", employee["name"])
            self.assertEqual(3, len(r["experiences"]), employee["name"])
            self.assertTrue(r["projects"], employee["name"])
            self.assertTrue(r["skill_bars"], employee["name"])
            self.assertTrue(r["prepare"], employee["name"])
            self.assertIn("人设履历", r["footnote"])
            for job in r["experiences"]:
                self.assertTrue(job["company"].startswith("某"), job["company"])
            card = resume.card(r)
            self.assertLessEqual(len(card["tags"]), 3)
        self.assertEqual(447, count)

    def test_generated_resumes_never_invent_numbers_in_wins(self):
        for employee in self._all():
            r = resume.for_employee(employee)
            if r["source"] != "generated":
                continue
            for job in r["experiences"]:
                for win in job["wins"]:
                    self.assertIsNone(re.search(r"\d+\s*%|提升\s*\d", win), win)

    def test_hand_written_sample_overrides_generated(self):
        employee = departments.get_active(111)
        r = resume.for_employee({**employee, "dept_key": "restaurant", "dept_name": "餐饮产业部"})
        self.assertEqual("override", r["source"])
        self.assertEqual("某连锁川菜品牌", r["experiences"][0]["company"])


if __name__ == "__main__":
    unittest.main()
