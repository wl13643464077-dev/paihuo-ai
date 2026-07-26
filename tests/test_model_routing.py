"""员工级模型自由切换的聚焦回归测试。"""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import main, providers


UNAVAILABLE_IMAGE_MODEL = "nano-banana-2-pro"


class TextModelRoutingTests(unittest.TestCase):
    def test_web_task_honors_employee_model(self):
        """检索任务不应再把老板的员工级选择强制改成工具模型。"""
        with patch.object(providers.db, "one", return_value={"model_text": "gpt-5.5"}), \
                patch.object(providers.db, "get_setting", return_value="deepseek-v4-flash"):
            self.assertEqual(providers.text_model_for(0, web_required=True), "gpt-5.5")

    def test_web_task_can_explicitly_choose_cloud_tool_model(self):
        with patch.object(providers.db, "one", return_value={"model_text": providers.CLAUDE_LOCAL}), \
                patch.object(providers.db, "get_setting", return_value="deepseek-v4-flash"):
            self.assertEqual(providers.text_model_for(1, web_required=True), providers.CLAUDE_LOCAL)

    def test_empty_employee_choice_follows_global_default(self):
        with patch.object(providers.db, "one", return_value={"model_text": None}), \
                patch.object(providers.db, "get_setting", return_value="claude-opus-4-8"):
            self.assertEqual(providers.text_model_for(2, web_required=True), "claude-opus-4-8")

    def test_invalid_saved_values_fall_back_safely(self):
        with patch.object(providers.db, "one", return_value={"model_text": "removed-model"}), \
                patch.object(providers.db, "get_setting", return_value="also-invalid"):
            self.assertEqual(providers.text_model_for(0, web_required=True), providers.DEFAULT_TEXT)

    def test_admin_selector_sanitizes_legacy_invalid_text_models(self):
        station = {"idx": 0, "key": "trend", "name": "趋势官"}
        saved = {"default_text_model": "removed-global-model"}

        with patch.object(main, "_need_boss"), \
                patch.object(main.registry, "STATIONS", [station]), \
                patch.object(main.departments, "list_depts", return_value=[]), \
                patch.object(main.employees, "get_config",
                             return_value={"prompt_template": None, "skills": []}), \
                patch.object(main.employees, "is_enabled", return_value=True), \
                patch.object(main.avatar, "engine_name", return_value="kling"), \
                patch.object(main.db, "get_setting",
                             side_effect=lambda key: saved.get(key)), \
                patch.object(main.db, "one",
                             return_value={"model_text": "removed-employee-model",
                                           "model_image": None}):
            payload = main.admin_overview()

        self.assertEqual(
            providers.DEFAULT_TEXT,
            payload["routing"]["default_text_model"],
        )
        self.assertEqual("", payload["employees"][0]["model_text"])

    def test_employee_model_save_rejects_unknown_or_invalid_text_models(self):
        with patch.object(main, "_need_boss"), \
                patch.object(main, "_is_emp", return_value=True), \
                patch.object(main.employees, "set_models") as save_mock:
            for model in ("removed-text-model", 123, {"id": "gpt-5.5"}):
                with self.subTest(model=model):
                    with self.assertRaises(HTTPException) as caught:
                        main.admin_emp_models(0, {"model_text": model})
                    self.assertEqual(400, caught.exception.status_code)
            save_mock.assert_not_called()

    def test_global_model_save_rejects_unknown_or_invalid_text_before_any_write(self):
        with patch.object(main, "_need_root"), \
                patch.object(main.db, "set_setting") as save_mock:
            for model in ("removed-text-model", 123, {"id": "gpt-5.5"}):
                with self.subTest(model=model):
                    with self.assertRaises(HTTPException) as caught:
                        main.settings_put({
                            "yunwu_base": "https://proxy.example",
                            "default_text_model": model,
                        })
                    self.assertEqual(400, caught.exception.status_code)
                    save_mock.assert_not_called()

    def test_available_text_models_can_still_be_saved_and_cleared(self):
        with patch.object(main, "_need_root"), \
                patch.object(main.db, "set_setting") as global_save:
            self.assertEqual(
                {"ok": True},
                main.settings_put({"default_text_model": "gpt-5.5"}),
            )
            self.assertEqual(
                {"ok": True},
                main.settings_put({"default_text_model": ""}),
            )
        self.assertEqual(
            [
                (("default_text_model", "gpt-5.5"), {}),
                (("default_text_model", None), {}),
            ],
            global_save.call_args_list,
        )

        with patch.object(main, "_need_boss"), \
                patch.object(main, "_is_emp", return_value=True), \
                patch.object(main.employees, "set_models") as employee_save:
            self.assertEqual(
                {"ok": True},
                main.admin_emp_models(0, {"model_text": "claude-opus-4-8"}),
            )
            self.assertEqual(
                {"ok": True},
                main.admin_emp_models(0, {"model_text": ""}),
            )
        self.assertEqual(
            [
                ((0, "claude-opus-4-8", None), {}),
                ((0, "", None), {}),
            ],
            employee_save.call_args_list,
        )


class ImageModelRoutingTests(unittest.TestCase):
    def test_admin_selector_hides_unavailable_models_and_sanitizes_legacy_values(self):
        station = {"idx": 5, "key": "media", "name": "多媒体师"}
        saved = {
            "default_image_model": UNAVAILABLE_IMAGE_MODEL,
        }

        with patch.object(main, "_need_boss"), \
                patch.object(main.registry, "STATIONS", [station]), \
                patch.object(main.departments, "list_depts", return_value=[]), \
                patch.object(main.employees, "get_config",
                             return_value={"prompt_template": None, "skills": []}), \
                patch.object(main.employees, "is_enabled", return_value=True), \
                patch.object(main.avatar, "engine_name", return_value="kling"), \
                patch.object(main.db, "get_setting",
                             side_effect=lambda key: saved.get(key)), \
                patch.object(main.db, "one",
                             return_value={"model_text": None,
                                           "model_image": UNAVAILABLE_IMAGE_MODEL}):
            payload = main.admin_overview()

        selectable_ids = {model["id"] for model in payload["image_models"]}
        self.assertNotIn(UNAVAILABLE_IMAGE_MODEL, selectable_ids)
        self.assertEqual(providers.DEFAULT_IMAGE,
                         payload["routing"]["default_image_model"])
        self.assertEqual("", payload["employees"][0]["model_image"])

    def test_employee_model_save_rejects_unavailable_and_unknown_image_models(self):
        with patch.object(main, "_need_boss"), \
                patch.object(main, "_is_emp", return_value=True), \
                patch.object(main.employees, "set_models") as save_mock:
            for model in (UNAVAILABLE_IMAGE_MODEL, "invented-image-model", 123):
                with self.subTest(model=model):
                    with self.assertRaises(HTTPException) as caught:
                        main.admin_emp_models(5, {"model_image": model})
                    self.assertEqual(400, caught.exception.status_code)
            save_mock.assert_not_called()

    def test_global_model_save_rejects_unavailable_or_invalid_before_any_write(self):
        with patch.object(main, "_need_root"), \
                patch.object(main.db, "set_setting") as save_mock:
            for model in (UNAVAILABLE_IMAGE_MODEL, "invented-image-model", 123):
                with self.subTest(model=model):
                    with self.assertRaises(HTTPException) as caught:
                        main.settings_put({
                            "yunwu_base": "https://proxy.example",
                            "default_image_model": model,
                        })
                    self.assertEqual(400, caught.exception.status_code)
                    save_mock.assert_not_called()

    def test_available_image_model_can_still_be_saved(self):
        model = "doubao-seedream-5-0-260128"
        with patch.object(main, "_need_boss"), \
                patch.object(main, "_is_emp", return_value=True), \
                patch.object(main.employees, "set_models") as save_mock:
            self.assertEqual({"ok": True},
                             main.admin_emp_models(5, {"model_image": model}))
        save_mock.assert_called_once_with(5, None, model)

    def test_available_global_image_model_can_still_be_saved(self):
        model = "doubao-seedream-5-0-260128"
        with patch.object(main, "_need_root"), \
                patch.object(main.db, "set_setting") as save_mock:
            self.assertEqual({"ok": True},
                             main.settings_put({"default_image_model": model}))
        save_mock.assert_called_once_with("default_image_model", model)

    def test_legacy_employee_image_model_falls_back_to_available_global_default(self):
        global_default = "doubao-seedream-5-0-260128"
        with patch.object(providers.db, "one",
                          return_value={"model_image": UNAVAILABLE_IMAGE_MODEL}), \
                patch.object(providers.db, "get_setting",
                             return_value=global_default):
            self.assertEqual(global_default, providers.image_model_for(5))

    def test_legacy_employee_and_global_image_models_fall_back_to_builtin_default(self):
        with patch.object(providers.db, "one",
                          return_value={"model_image": UNAVAILABLE_IMAGE_MODEL}), \
                patch.object(providers.db, "get_setting",
                             return_value=UNAVAILABLE_IMAGE_MODEL):
            self.assertEqual(providers.DEFAULT_IMAGE,
                             providers.image_model_for(5))


class ImageRuntimeAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_rejects_unavailable_model_before_credentials_or_network(self):
        with patch.object(
                providers, "yunwu_conf",
                side_effect=AssertionError("unavailable model reached provider setup")):
            with self.assertRaisesRegex(providers.ProviderError, "不可用"):
                await providers.image(
                    "生成一张安全测试图",
                    model=UNAVAILABLE_IMAGE_MODEL,
                )


class ToolGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_json_retry_accumulates_every_model_call(self):
        attempts = [
            {"text": "不是 JSON", "cost_usd": 0.2, "tokens": 30},
            {"text": '{"ok":true}', "cost_usd": 0.3, "tokens": 40},
        ]
        with patch.object(
                providers, "call_text", new=AsyncMock(side_effect=attempts)) as call:
            got = await providers.call_text_json(
                1, "只输出 JSON", retries=1
            )

        self.assertEqual(call.await_count, 2)
        self.assertEqual(got["data"], {"ok": True})
        self.assertAlmostEqual(got["cost_usd"], 0.5)
        self.assertEqual(got["tokens"], 70)

    async def test_gpt_web_task_researches_then_uses_selected_model(self):
        research = {"text": "证据包:https://example.com", "cost_usd": 0.2, "tokens": 30}
        final = {"text": "最终交付", "cost_usd": 0.3, "tokens": 40}
        with patch.object(providers, "text_model_for", return_value="gpt-5.5"), \
                patch.object(providers, "yunwu_conf", return_value=("https://proxy.example", "key")), \
                patch.object(providers, "chat", AsyncMock(return_value=final)) as chat_mock, \
                patch("app.llm.call", AsyncMock(return_value=research)) as agent_mock:
            got = await providers.call_text(0, "查今天热点", web=True)
        self.assertEqual(got["text"], "最终交付")
        self.assertEqual(got["cost_usd"], 0.5)
        self.assertEqual(got["tokens"], 70)
        agent_mock.assert_awaited_once()
        self.assertEqual(chat_mock.await_args.kwargs["model"], "gpt-5.5")
        self.assertIn("证据包", chat_mock.await_args.args[0])

    async def test_claude_web_task_isolates_research_then_uses_api_for_final(self):
        research = {"text": "证据包", "cost_usd": 0.2, "tokens": 30}
        final = {"text": "带来源的交付", "cost_usd": 0.1, "tokens": 20}
        with patch.object(providers, "text_model_for", return_value=providers.AGENT_MODEL), \
                patch.object(providers, "yunwu_conf", return_value=("https://proxy.example", "key")), \
                patch.object(providers, "chat", AsyncMock(return_value=final)) as chat_mock, \
                patch("app.llm.call", AsyncMock(return_value=research)) as agent_mock:
            got = await providers.call_text(0, "查今天热点", web=True)
        self.assertEqual(got["text"], final["text"])
        self.assertAlmostEqual(got["cost_usd"], 0.3)
        self.assertEqual(got["tokens"], 50)
        agent_mock.assert_awaited_once()
        chat_mock.assert_awaited_once()
        self.assertEqual(chat_mock.await_args.kwargs["model"], providers.AGENT_MODEL)

    async def test_web_gateway_requires_provider_key(self):
        with patch.object(providers, "text_model_for", return_value="gpt-5.5"), \
                patch.object(providers, "yunwu_conf", return_value=("https://proxy.example", "")):
            with self.assertRaisesRegex(providers.ProviderError, "云雾API key"):
                await providers.call_text(0, "查今天热点", web=True)

    async def test_plain_generation_never_falls_back_to_local_login(self):
        with patch.object(providers, "text_model_for", return_value="gpt-5.5"), \
                patch.object(providers, "yunwu_conf", return_value=("https://proxy.example", "")):
            with self.assertRaisesRegex(providers.ProviderError, "禁止回退本地 Claude"):
                await providers.call_text(3, "写一份初稿")

    async def test_structured_web_evidence_is_parsed_before_downstream_writing(self):
        agent_result = {
            "text": '{"sources":[{"source_url":"https://example.com/posts/1"}]}',
            "cost_usd": 0.2,
            "tokens": 30,
        }
        with patch.object(providers, "yunwu_conf",
                          return_value=("https://proxy.example", "key")), \
                patch("app.llm.call", AsyncMock(return_value=agent_result)) as agent_mock:
            got = await providers.call_web_json("只找真实原帖", token="lead:1")

        self.assertEqual(got["data"]["sources"][0]["source_url"],
                         "https://example.com/posts/1")
        self.assertEqual(got["tokens"], 30)
        self.assertTrue(agent_mock.await_args.kwargs["web"])
        self.assertEqual(agent_mock.await_args.kwargs["model"], providers.AGENT_MODEL)
        self.assertEqual(
            agent_mock.await_args.kwargs["provider_env"]["ANTHROPIC_AUTH_TOKEN"], "key")

    async def test_structured_web_evidence_requires_cloud_key(self):
        with patch.object(providers, "yunwu_conf",
                          return_value=("https://proxy.example", "")):
            with self.assertRaisesRegex(providers.ProviderError, "云雾API key"):
                await providers.call_web_json("只找真实原帖")

    async def test_structured_web_retry_accumulates_every_gateway_call(self):
        attempts = [
            {"text": "不是 JSON", "cost_usd": 0.2, "tokens": 30},
            {"text": '{"sources":[]}', "cost_usd": 0.3, "tokens": 40},
        ]
        with patch.object(providers, "yunwu_conf",
                          return_value=("https://proxy.example", "key")), \
                patch("app.llm.call", AsyncMock(side_effect=attempts)) as agent_mock:
            got = await providers.call_web_json("只找真实原帖", retries=1)

        self.assertEqual(agent_mock.await_count, 2)
        self.assertAlmostEqual(got["cost_usd"], 0.5)
        self.assertEqual(got["tokens"], 70)

    async def test_structured_web_result_can_be_repaired_without_searching_twice(self):
        research = {
            "text": "找到原帖：https://example.com/posts/1，用户正在比价。",
            "cost_usd": 0.2,
            "tokens": 30,
        }
        repaired = {
            "text": '{"sources":[{"source_url":"https://example.com/posts/1"}]}',
            "cost_usd": 0.1,
            "tokens": 10,
        }
        with patch.object(providers, "yunwu_conf",
                          return_value=("https://proxy.example", "key")), \
                patch.object(providers, "text_model_for", return_value="deepseek-v4-flash"), \
                patch("app.llm.call", AsyncMock(return_value=research)) as agent_mock, \
                patch.object(providers, "chat",
                             AsyncMock(return_value=repaired)) as repair_mock:
            got = await providers.call_web_json(
                "只找真实原帖", retries=0, repair_invalid=True
            )

        agent_mock.assert_awaited_once()
        repair_mock.assert_awaited_once()
        self.assertEqual(got["data"]["sources"][0]["source_url"],
                         "https://example.com/posts/1")
        self.assertAlmostEqual(got["cost_usd"], 0.3)
        self.assertEqual(got["tokens"], 40)

    async def test_structured_web_repair_cannot_invent_or_change_source_url(self):
        research = {
            "text": "找到原帖：https://example.com/posts/real",
            "cost_usd": 0.2,
            "tokens": 30,
        }
        dishonest_repair = {
            "text": '{"sources":[{"source_url":"https://example.com/posts/fake"}]}',
            "cost_usd": 0.1,
            "tokens": 10,
        }
        with patch.object(providers, "yunwu_conf",
                          return_value=("https://proxy.example", "key")), \
                patch.object(providers, "text_model_for", return_value="deepseek-v4-flash"), \
                patch("app.llm.call", AsyncMock(return_value=research)), \
                patch.object(providers, "chat", AsyncMock(return_value=dishonest_repair)):
            with self.assertRaisesRegex(providers.ProviderError, "改写了来源 URL"):
                await providers.call_web_json(
                    "只找真实原帖", retries=0, repair_invalid=True
                )

    async def test_frozen_web_urls_cover_where_and_leading_space_bypasses(self):
        allowed = {"https://example.com/posts/real"}
        dishonest = [
            {"sources": [{"where": "来自 https://example.com/posts/fake"}]},
            {"sources": [{"source_url": " https://example.com/posts/fake"}]},
            {"sources": [{"note": "[原帖](https://example.com/posts/fake)"}]},
        ]
        for data in dishonest:
            with self.subTest(data=data), self.assertRaisesRegex(
                    providers.ProviderError, "改写了来源 URL"):
                providers._assert_repaired_urls_frozen(data, allowed)


if __name__ == "__main__":
    unittest.main()
