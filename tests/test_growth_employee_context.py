"""工具箱数字员工私有上下文与联网隔离边界回归测试。"""
import inspect
import unittest
from unittest.mock import AsyncMock, patch

from app import employees, growth


class ToolboxEmployeeContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_toolbox_employee_route_injects_private_context_only_in_system(self):
        response = {"data": {"safe": True}, "cost_usd": 0, "tokens": 1}
        gateway = AsyncMock(return_value=response)
        private_workflow = "SECRET-WORKFLOW-{topic}"
        private_skill = "\n【你的进修技能库】\n- 【SECRET-SKILL】私有打法\n"
        private_cap = "SECRET-CAPABILITY"
        routes = (
            (8, False, ""),                 # 私域日历
            (0, True, "热点公开检索"),       # 今日必发
            (0, True, "起号公开检索"),       # 起号军师
            (1, False, ""),                 # 线索最终分析
            (2, True, "竞品公开检索"),       # 竞品盯梢
            (8, False, ""),                 # 口播矩阵
        )

        with patch.object(
            employees,
            "get_config",
            return_value={"prompt_template": private_workflow},
        ), patch.object(
            employees,
            "skills_block",
            return_value=private_skill,
        ), patch(
            "app.skills.registry.capabilities_for",
            return_value=[{
                "name": "私有能力",
                "desc": private_cap,
                "enabled": True,
            }],
        ), patch.object(growth.providers, "call_text_json", gateway):
            for idx, web, public_research in routes:
                with self.subTest(idx=idx, web=web, research=public_research):
                    result = await growth._call_toolbox_employee_json(
                        idx,
                        "PUBLIC-USER-PROMPT",
                        web=web,
                        research_brief=public_research,
                    )
                    call = gateway.await_args
                    system = call.kwargs["system_prompt"]
                    research = call.kwargs.get("research_brief") or ""
                    sensitive = call.kwargs["sensitive_texts"]

                    self.assertEqual(idx, call.args[0])
                    self.assertEqual("PUBLIC-USER-PROMPT", call.args[1])
                    self.assertEqual(web, call.kwargs["web"])
                    self.assertIn("SECRET-WORKFLOW", system)
                    self.assertIn(private_skill.strip(), system)
                    self.assertIn(private_cap, system)
                    self.assertTrue(any("SECRET-WORKFLOW" in item for item in sensitive))
                    self.assertTrue(any("SECRET-SKILL" in item for item in sensitive))
                    self.assertTrue(any(private_cap in item for item in sensitive))
                    for private in ("SECRET-WORKFLOW", "SECRET-SKILL", private_cap):
                        self.assertNotIn(private, call.args[1])
                        self.assertNotIn(private, research)
                        self.assertNotIn(private, str(result))
                    if web:
                        self.assertTrue(research.strip())
                        self.assertIn(public_research, research)

    def test_all_six_text_tool_entrypoints_use_the_private_context_wrapper(self):
        for func in (
            growth.private_calendar,
            growth.hot_pick,
            growth.warmup_plan,
            growth.leads_radar,
            growth.bench_report,
            growth.script_variants,
        ):
            with self.subTest(entrypoint=func.__name__):
                source = inspect.getsource(func)
                self.assertIn("_call_toolbox_employee_json(", source)
                self.assertNotIn("providers.call_text_json(", source)

    async def test_menu_vision_route_uses_writer_private_context_without_user_leak(self):
        vision = AsyncMock(return_value={
            "text": '{"item":"产品","selling_point":"卖点"}',
            "cost_usd": 0,
            "tokens": 1,
        })
        with patch.object(
            employees,
            "get_config",
            return_value={"prompt_template": "SECRET-WRITER-WORKFLOW"},
        ), patch.object(
            employees,
            "skills_block",
            return_value="SECRET-WRITER-SKILL",
        ), patch(
            "app.skills.registry.capabilities_for",
            return_value=[{
                "name": "撰写能力", "desc": "SECRET-WRITER-CAP", "enabled": True,
            }],
        ), patch.object(growth.providers, "call_vision", vision):
            result = await growth.menu_copy(
                9, "YWJj", "image/png", "PUBLIC-MENU-REQUEST"
            )

        call = vision.await_args
        self.assertEqual(3, call.args[0])
        self.assertIn("PUBLIC-MENU-REQUEST", call.args[1])
        self.assertIn("SECRET-WRITER-WORKFLOW", call.kwargs["system_prompt"])
        self.assertIn("SECRET-WRITER-SKILL", call.kwargs["system_prompt"])
        self.assertIn("SECRET-WRITER-CAP", call.kwargs["system_prompt"])
        for private in ("SECRET-WRITER-WORKFLOW", "SECRET-WRITER-SKILL",
                        "SECRET-WRITER-CAP"):
            self.assertNotIn(private, call.args[1])
            self.assertNotIn(private, str(result))

    async def test_product_shot_uses_private_media_planner_then_only_public_edit_prompt(self):
        vision = AsyncMock(return_value={
            "text": '{"edit_prompt":"PUBLIC-SAFE-EDIT-PROMPT"}',
            "cost_usd": 0,
            "tokens": 1,
        })
        image = AsyncMock(return_value=b"safe-image")
        with patch.object(
            employees,
            "get_config",
            return_value={"prompt_template": "SECRET-MEDIA-WORKFLOW"},
        ), patch.object(
            employees,
            "skills_block",
            return_value="SECRET-MEDIA-SKILL",
        ), patch(
            "app.skills.registry.capabilities_for",
            return_value=[{
                "name": "视觉能力", "desc": "SECRET-MEDIA-CAP", "enabled": True,
            }],
        ), patch.object(
            growth.providers, "call_vision", vision,
        ), patch.object(growth.providers, "edit_image", image):
            result = await growth.product_shot(
                9, b"source", "PUBLIC-PRODUCT-SCENE"
            )

        self.assertEqual(b"safe-image", result)
        self.assertEqual(5, vision.await_args.args[0])
        self.assertIn("PUBLIC-PRODUCT-SCENE", vision.await_args.args[1])
        self.assertIn("SECRET-MEDIA-WORKFLOW", vision.await_args.kwargs["system_prompt"])
        self.assertIn("SECRET-MEDIA-SKILL", vision.await_args.kwargs["system_prompt"])
        self.assertIn("SECRET-MEDIA-CAP", vision.await_args.kwargs["system_prompt"])
        self.assertEqual(5, image.await_args.args[0])
        self.assertEqual("PUBLIC-SAFE-EDIT-PROMPT", image.await_args.args[1])
        for private in ("SECRET-MEDIA-WORKFLOW", "SECRET-MEDIA-SKILL",
                        "SECRET-MEDIA-CAP"):
            self.assertNotIn(private, vision.await_args.args[1])
            self.assertNotIn(private, image.await_args.args[1])


class SkillsBlockHardeningTests(unittest.TestCase):
    def test_malformed_and_oversized_skills_are_skipped_without_hiding_later_valid_skill(self):
        oversized = {
            "title": "X" * (employees.MAX_SKILLS_CHARS + 100),
            "detail": "不应注入",
            "enabled": True,
        }
        skills = [
            None,
            "not-a-dict",
            oversized,
            {"title": "有效技能", "detail": "应继续被注入", "enabled": True},
            {"title": "已停用", "detail": "不应注入", "enabled": False},
        ]
        with patch.object(employees, "get_config", return_value={"skills": skills}):
            block = employees.skills_block(0)

        self.assertIn("有效技能", block)
        self.assertIn("应继续被注入", block)
        self.assertNotIn("X" * 100, block)
        self.assertNotIn("已停用", block)


if __name__ == "__main__":
    unittest.main()
