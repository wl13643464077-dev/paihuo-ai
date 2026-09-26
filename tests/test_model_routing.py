"""员工级模型自由切换的聚焦回归测试。"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import db, main, providers


UNAVAILABLE_IMAGE_MODEL = "nano-banana-2-pro"
TEST_IDENTITY_REF = "a" * 64
TEST_BINDING = {
    "employee": {"idx": 0},
    "identity": {"identity_ref": TEST_IDENTITY_REF},
    "config": {"config_revision": 7},
}
TEST_PUBLIC_IDENTITY = {
    "person_status": "active",
    "identity_status": "current",
    "can_assign_new": True,
    "can_continue": True,
    "can_learn": True,
}


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
                patch.object(main.employeeidentity, "active_employee", return_value=station), \
                patch.object(main, "_employee_public_contract",
                             return_value=TEST_PUBLIC_IDENTITY), \
                patch.object(main.employees, "get_config",
                             return_value={"prompt_template": None, "skills": [],
                                           "model_text": "removed-employee-model",
                                           "model_image": None}), \
                patch.object(main.employees, "is_enabled", return_value=True), \
                patch.object(main.avatar, "engine_name", return_value="kling"), \
                patch.object(main.db, "get_setting",
                             side_effect=lambda key: saved.get(key)):
            payload = main.admin_overview()

        self.assertEqual(
            providers.DEFAULT_TEXT,
            payload["routing"]["default_text_model"],
        )
        self.assertEqual("", payload["employees"][0]["model_text"])

    def test_employee_model_save_rejects_unknown_or_invalid_text_models(self):
        with patch.object(main, "_need_boss"), \
                patch.object(main, "_employee_current_write_binding",
                             return_value=TEST_BINDING), \
                patch.object(main.employees, "set_models_for_identity") as save_mock:
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
                patch.object(main, "_employee_current_write_binding",
                             return_value=TEST_BINDING), \
                patch.object(main.employees, "set_models_for_identity") as employee_save, \
                patch.object(main.employees, "get_config", return_value={}), \
                patch.object(main, "_employee_public_contract", return_value={}):
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
                ((TEST_IDENTITY_REF, "claude-opus-4-8", None),
                 {"expected_revision": 7}),
                ((TEST_IDENTITY_REF, "", None),
                 {"expected_revision": 7}),
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
                patch.object(main.employeeidentity, "active_employee", return_value=station), \
                patch.object(main, "_employee_public_contract",
                             return_value=TEST_PUBLIC_IDENTITY), \
                patch.object(main.employees, "get_config",
                             return_value={"prompt_template": None, "skills": [],
                                           "model_text": None,
                                           "model_image": UNAVAILABLE_IMAGE_MODEL}), \
                patch.object(main.employees, "is_enabled", return_value=True), \
                patch.object(main.avatar, "engine_name", return_value="kling"), \
                patch.object(main.db, "get_setting",
                             side_effect=lambda key: saved.get(key)):
            payload = main.admin_overview()

        selectable_ids = {model["id"] for model in payload["image_models"]}
        self.assertNotIn(UNAVAILABLE_IMAGE_MODEL, selectable_ids)
        self.assertEqual(providers.DEFAULT_IMAGE,
                         payload["routing"]["default_image_model"])
        self.assertEqual("", payload["employees"][0]["model_image"])

    def test_employee_model_save_rejects_unavailable_and_unknown_image_models(self):
        with patch.object(main, "_need_boss"), \
                patch.object(main, "_employee_current_write_binding",
                             return_value=TEST_BINDING), \
                patch.object(main.employees, "set_models_for_identity") as save_mock:
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
                patch.object(main, "_employee_current_write_binding",
                             return_value=TEST_BINDING), \
                patch.object(main.employees, "set_models_for_identity") as save_mock, \
                patch.object(main.employees, "get_config", return_value={}), \
                patch.object(main, "_employee_public_contract", return_value={}):
            self.assertEqual({"ok": True},
                             main.admin_emp_models(5, {"model_image": model}))
        save_mock.assert_called_once_with(
            TEST_IDENTITY_REF, None, model, expected_revision=7,
        )

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
    """网关测试使用独立数据库，避免异步 worker 触碰仓库默认 DB 锁。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "model-routing.db")
        db.conn()

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

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

    async def test_web_task_retries_once_when_gateway_returns_no_verified_search(self):
        research = {
            "text": "有效证据包:https://example.com",
            "cost_usd": 0.2,
            "tokens": 30,
        }
        final = {"text": "最终交付", "cost_usd": 0.3, "tokens": 40}
        progress = []
        with patch.object(providers, "text_model_for", return_value="gpt-5.5"), \
                patch.object(
                    providers, "yunwu_conf",
                    return_value=("https://proxy.example", "key"),
                ), \
                patch.object(
                    providers, "_controlled_webfetch_evidence",
                    AsyncMock(return_value=""),
                ), \
                patch.object(
                    providers, "chat", AsyncMock(return_value=final),
                ) as chat_mock, \
                patch(
                    "app.llm.call",
                    AsyncMock(side_effect=[
                        providers.llm.WebSearchRequiredError(
                            "first response skipped search",
                            cost_usd=0.1,
                            tokens=10,
                        ),
                        research,
                    ]),
                ) as agent_mock:
            got = await providers.call_text(
                0,
                "查今天热点",
                web=True,
                token="task70:",
                progress=lambda kind, label: progress.append((kind, label)),
            )

        self.assertEqual(2, agent_mock.await_count)
        first = agent_mock.await_args_list[0]
        second = agent_mock.await_args_list[1]
        self.assertEqual(
            first.kwargs["token"] + ":retry1",
            second.kwargs["token"],
        )
        self.assertIn("必须先实际调用 WebSearch", second.args[0])
        self.assertTrue(any(kind == "retry" for kind, _label in progress))
        chat_mock.assert_awaited_once()
        self.assertEqual("最终交付", got["text"])
        self.assertAlmostEqual(0.6, got["cost_usd"])
        self.assertEqual(80, got["tokens"])

    async def test_web_task_stops_after_second_no_search_and_does_not_write(self):
        no_search = providers.llm.WebSearchRequiredError(
            "must never reach a public response"
        )
        with patch.object(providers, "text_model_for", return_value="gpt-5.5"), \
                patch.object(
                    providers, "yunwu_conf",
                    return_value=("https://proxy.example", "key"),
                ), \
                patch.object(providers, "chat", AsyncMock()) as chat_mock, \
                patch(
                    "app.llm.call",
                    AsyncMock(side_effect=[no_search, no_search]),
                ) as agent_mock:
            with self.assertRaises(providers.llm.WebSearchRequiredError):
                await providers.call_text(0, "查今天热点", web=True)

        self.assertEqual(2, agent_mock.await_count)
        chat_mock.assert_not_awaited()

    async def test_first_no_search_usage_survives_a_generic_second_failure(self):
        first = providers.llm.WebSearchRequiredError(
            "no verified search", cost_usd=0.125, tokens=9
        )
        second = providers.llm.LLMError("runner failed")
        with patch(
            "app.llm.call", AsyncMock(side_effect=[first, second])
        ) as agent:
            with self.assertRaises(providers.llm.LLMError) as caught:
                await providers._call_websearch_gateway(
                    "search now",
                    base="https://proxy.example",
                    key="key",
                    timeout=30,
                )

        self.assertNotIsInstance(
            caught.exception, providers.llm.WebSearchRequiredError
        )
        self.assertAlmostEqual(0.125, caught.exception.cost_usd)
        self.assertEqual(9, caught.exception.tokens)
        self.assertEqual(2, agent.await_count)

    async def test_web_task_does_not_blind_retry_other_runner_errors(self):
        with patch.object(providers, "text_model_for", return_value="gpt-5.5"), \
                patch.object(
                    providers, "yunwu_conf",
                    return_value=("https://proxy.example", "key"),
                ), \
                patch(
                    "app.llm.call",
                    AsyncMock(side_effect=providers.llm.LLMError("runner failed")),
                ) as agent_mock:
            with self.assertRaises(providers.llm.LLMError):
                await providers.call_text(0, "查今天热点", web=True)

        self.assertEqual(1, agent_mock.await_count)

    async def test_websearch_retry_shares_one_wall_clock_deadline(self):
        no_search = providers.llm.WebSearchRequiredError("no verified search")
        recovered = {
            "text": "verified",
            "cost_usd": 0.2,
            "tokens": 20,
            "tool_usage": {
                "WebSearch": {"attempts": 1, "success": 1, "errors": 0}
            },
        }
        with patch.object(
                providers, "_monotonic", side_effect=[100.0, 100.0, 160.0]
            ), patch(
                "app.llm.call", AsyncMock(side_effect=[no_search, recovered])
            ) as agent_mock:
            got = await providers._call_websearch_gateway(
                "search now",
                base="https://proxy.example",
                key="key",
                timeout=100,
                token="task70::research",
            )

        self.assertEqual("verified", got["text"])
        self.assertEqual(100, agent_mock.await_args_list[0].kwargs["timeout"])
        self.assertEqual(40, agent_mock.await_args_list[1].kwargs["timeout"])

    async def test_websearch_deadline_bounds_gateway_queue_and_cleanup_wait(self):
        async def delayed_gateway(*_args, **_kwargs):
            await asyncio.sleep(0.08)
            return {"text": "too late", "cost_usd": 0, "tokens": 0}

        started = asyncio.get_running_loop().time()
        with patch("app.llm.call", AsyncMock(side_effect=delayed_gateway)) as agent:
            with self.assertRaises(providers.llm.LLMError) as caught:
                await providers._call_websearch_gateway(
                    "search now",
                    base="https://proxy.example",
                    key="key",
                    timeout=0.02,
                )
        elapsed = asyncio.get_running_loop().time() - started

        self.assertNotIsInstance(
            caught.exception, providers.llm.WebSearchRequiredError
        )
        self.assertLess(elapsed, 0.07)
        self.assertEqual(1, agent.await_count)

    async def test_web_json_shares_one_no_search_retry_across_format_retries(self):
        no_search = providers.llm.WebSearchRequiredError("no verified search")
        malformed = {
            "text": "not-json",
            "cost_usd": 0.2,
            "tokens": 20,
            "web_sources": [],
            "tool_usage": {
                "WebSearch": {"attempts": 1, "success": 1, "errors": 0}
            },
        }
        agent = AsyncMock(side_effect=[
            no_search,
            malformed,
            no_search,
            AssertionError("zero-search retry budget was reset"),
        ])
        with patch.object(
                providers, "yunwu_conf",
                return_value=("https://proxy.example", "key"),
            ), patch("app.llm.call", agent):
            with self.assertRaises(providers.llm.WebSearchRequiredError):
                await providers.call_web_json(
                    "return json", retries=1, repair_invalid=False
                )

        self.assertEqual(3, agent.await_count)

    async def test_text_json_shares_one_no_search_retry_across_format_retries(self):
        no_search = providers.llm.WebSearchRequiredError("no verified search")
        research = {
            "text": "verified evidence",
            "cost_usd": 0.2,
            "tokens": 20,
        }
        agent = AsyncMock(side_effect=[
            no_search,
            research,
            no_search,
            AssertionError("call_text_json reset zero-search retry budget"),
        ])
        writer = AsyncMock(return_value={
            "text": "not-json",
            "cost_usd": 0.3,
            "tokens": 30,
        })
        with patch.object(providers, "text_model_for", return_value="gpt-5.5"), \
                patch.object(
                    providers, "yunwu_conf",
                    return_value=("https://proxy.example", "key"),
                ), \
                patch.object(
                    providers, "_controlled_webfetch_evidence",
                    AsyncMock(return_value=""),
                ), \
                patch.object(providers, "chat", writer), \
                patch("app.llm.call", agent):
            with self.assertRaises(providers.llm.WebSearchRequiredError):
                await providers.call_text_json(
                    0, "return json", web=True, retries=1
                )

        self.assertEqual(3, agent.await_count)
        self.assertEqual(1, writer.await_count)

    def test_no_search_failure_has_an_accurate_non_reflective_public_message(self):
        marker = "PRIVATE-UPSTREAM-DETAIL"
        message = providers.public_failure_message(
            providers.llm.WebSearchRequiredError(marker)
        )

        self.assertIn("联网检索未返回有效结果", message)
        self.assertIn("免费重试", message)
        self.assertNotIn("超时或繁忙", message)
        self.assertNotIn(marker, message)

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
            "web_sources": [{
                "source_title": "Original post",
                "source_url": "https://example.com/posts/1",
            }],
        }
        with patch.object(providers, "yunwu_conf",
                          return_value=("https://proxy.example", "key")), \
                patch("app.llm.call", AsyncMock(return_value=agent_result)) as agent_mock:
            got = await providers.call_web_json("只找真实原帖", token="lead:1")

        self.assertEqual(got["data"]["sources"][0]["source_url"],
                         "https://example.com/posts/1")
        self.assertEqual(got["tokens"], 30)
        self.assertEqual(agent_result["web_sources"], got["web_sources"])
        self.assertTrue(agent_mock.await_args.kwargs["web"])
        self.assertTrue(agent_mock.await_args.kwargs["capture_web_sources"])
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
            {
                "text": "不是 JSON", "cost_usd": 0.2, "tokens": 30,
                "web_sources": [{
                    "source_title": "First source",
                    "source_url": "https://example.com/posts/1",
                }],
            },
            {
                "text": '{"sources":[]}', "cost_usd": 0.3, "tokens": 40,
                "web_sources": [
                    {
                        "source_title": "Duplicate title ignored",
                        "source_url": "https://example.com/posts/1",
                    },
                    {
                        "source_title": "Second source",
                        "source_url": "https://example.com/posts/2",
                    },
                ],
            },
        ]
        with patch.object(providers, "yunwu_conf",
                          return_value=("https://proxy.example", "key")), \
                patch("app.llm.call", AsyncMock(side_effect=attempts)) as agent_mock:
            got = await providers.call_web_json("只找真实原帖", retries=1)

        self.assertEqual(agent_mock.await_count, 2)
        self.assertAlmostEqual(got["cost_usd"], 0.5)
        self.assertEqual(got["tokens"], 70)
        self.assertEqual(
            [
                {
                    "source_title": "First source",
                    "source_url": "https://example.com/posts/1",
                },
                {
                    "source_title": "Second source",
                    "source_url": "https://example.com/posts/2",
                },
            ],
            got["web_sources"],
        )

    async def test_structured_web_model_text_cannot_create_captured_sources(self):
        agent_result = {
            "text": '{"sources":[{"source_url":"https://invented.invalid/1"}]}',
            "cost_usd": 0.2,
            "tokens": 30,
        }
        with patch.object(providers, "yunwu_conf",
                          return_value=("https://proxy.example", "key")), \
                patch("app.llm.call", AsyncMock(return_value=agent_result)):
            got = await providers.call_web_json("只找真实原帖")

        self.assertEqual([], got["web_sources"])

    async def test_structured_web_result_can_be_repaired_without_searching_twice(self):
        research = {
            "text": "找到原帖：https://example.com/posts/1，用户正在比价。",
            "cost_usd": 0.2,
            "tokens": 30,
            "web_sources": [{
                "source_title": "Trusted tool source",
                "source_url": "https://example.com/posts/1",
            }],
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
        self.assertEqual(research["web_sources"], got["web_sources"])

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
