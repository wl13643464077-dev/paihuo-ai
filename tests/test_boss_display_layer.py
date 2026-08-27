"""老板视角展示层：行业市场命名 + 员工介绍一眼懂（纯展示、不动内箱）。"""
import unittest
from pathlib import Path

from app import departments
from app.main import _boss_glance_intro, _display_dept_name

APP_JS = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(
    encoding="utf-8"
)


class DisplayDeptNameTests(unittest.TestCase):
    def test_all_industry_depts_map_to_plain_industry_names(self):
        expected = {
            "auto": "汽车行业",
            "beauty": "美容美业行业",
            "convenience": "便利店行业",
            "fitness": "健身瑜伽行业",
            "grocery": "商超零售行业",
            "hotel": "酒店住宿行业",
            "pet": "宠物服务行业",
            "pharmacy": "零售药房行业",
            "restaurant": "餐饮行业",
            "snack": "量贩零食行业",
            "tea_coffee": "茶咖现制行业",
        }
        for dept in departments.list_depts():
            self.assertEqual(
                expected[dept["key"]],
                _display_dept_name(dept["key"], dept["name"]),
            )

    def test_unknown_key_falls_back_and_catalog_names_stay_frozen(self):
        self.assertEqual("内容生产部", _display_dept_name("content", "内容生产部"))
        # 内箱不动：目录原始部门名保持发布时的冻结值，不被展示映射改写。
        raw_names = {d["key"]: d["name"] for d in departments.list_depts()}
        self.assertEqual("汽车后市场产业部", raw_names["auto"])
        self.assertEqual("餐饮产业部", raw_names["restaurant"])


class BossGlanceIntroTests(unittest.TestCase):
    def _employee(self, dept_index=0, emp_index=0):
        dept = departments.list_depts()[dept_index]
        raw = dept["employees"][emp_index]
        return {**raw, "dept_key": dept["key"], "dept_name": dept["name"]}

    def test_internal_intro_quotes_duty_core_and_reads_plainly(self):
        e = self._employee()  # auto 1601: duty 含引号内核心问题
        intro = _boss_glance_intro(e, internal=True)
        self.assertIn("TA干的活：帮您把关「", intro)
        self.assertIn("啥时候找TA：", intro)
        self.assertIn("怎么用：", intro)
        self.assertIn("最终拍板永远在您", intro)
        # 「官」后缀转成事：介绍里不该出现「审查官」相关的事」这种职务叫法
        self.assertNotIn("官」相关的事", intro)

    def test_tour_intro_never_leaks_duty_and_uses_display_dept(self):
        e = self._employee()
        duty = str(e.get("duty") or "")
        intro = _boss_glance_intro(e, internal=False)
        self.assertNotIn("帮您把关「", intro)
        # duty 合同原文的核心问题句不得出现在游客介绍里
        if "“" in duty and "”" in duty:
            core = duty.split("“", 1)[1].split("”", 1)[0]
            self.assertNotIn(core, intro)
        self.assertIn("汽车行业", intro)
        self.assertNotIn("产业部", intro)

    def test_short_plain_duty_is_used_directly(self):
        e = self._employee(dept_index=2)  # convenience 1101: duty 是短白话
        intro = _boss_glance_intro(e, internal=True)
        self.assertIn("帮您把关「", intro)


class HomeGuideCopyTests(unittest.TestCase):
    def test_industry_market_replaces_group_floors(self):
        self.assertNotIn("集团楼层", APP_JS)
        self.assertIn("行业市场", APP_JS)
        self.assertIn("行业精品员工", APP_JS)

    def test_onboarding_steps_carry_why_and_how(self):
        self.assertIn("开工四步", APP_JS)
        self.assertIn("为什么:有了它,产出才像您本人写的", APP_JS)
        self.assertIn("怎么做:选方向和模板→提交→8-15分钟收成品", APP_JS)

    def test_release_trio_carries_how(self):
        self.assertIn("发布三件套", APP_JS)
        self.assertIn("怎么做:任务交付页→「一键成片」", APP_JS)
        self.assertIn("一条内容的完整出路", APP_JS)

    def test_howto_card_teaches_four_step_usage_and_resets_with_guide(self):
        self.assertIn("数字员工怎么用 · 四步用人法", APP_JS)
        for kw in ("① 挑人", "② 派活", "③ 收活", "④ 沉淀"):
            self.assertIn(kw, APP_JS)
        self.assertIn("Agent 团队协作执行", APP_JS)
        # 「重看新手引导」必须能恢复教程卡
        self.assertIn('localStorage.removeItem("howto_hide_"', APP_JS)
        self.assertIn("howtoCard()", APP_JS)


if __name__ == "__main__":
    unittest.main()
