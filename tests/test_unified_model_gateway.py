"""业务模型调用必须服从员工级或全局配置，且只能经过 providers 网关。"""
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from app import growth, imagehunt, providers, textvideo


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_MODULE = ROOT / "app" / "providers.py"


class GatewayBoundaryAstTests(unittest.TestCase):
    def test_only_provider_module_may_assemble_model_http_endpoints(self):
        offenders = []
        endpoint_parts = (
            "/v1/chat/completions",
            "/v1/images/generations",
            "/v1/images/edits",
        )
        for path in (ROOT / "app").rglob("*.py"):
            if path == PROVIDER_MODULE:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if any(part in node.value for part in endpoint_parts):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "业务模块绕过 providers 裸调供应商 HTTP:\n" + "\n".join(offenders),
        )

    def test_business_runtime_cannot_pin_models_or_provider_defaults(self):
        offenders = []
        runtime_calls = {"chat", "image", "call_text", "call_text_json"}
        for path in (ROOT / "app").rglob("*.py"):
            if path == PROVIDER_MODULE:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and node.value in {"gpt-4o", "gpt-image-2"}
                ):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}={node.value}"
                    )
                    continue
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in {"DEFAULT_TEXT", "DEFAULT_IMAGE"}
                ):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}=provider-default"
                    )
                    continue
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr not in runtime_calls:
                    continue
                if func.attr in {"chat", "image"}:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}=low-level-{func.attr}"
                    )
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "model":
                        continue
                    value = keyword.value
                    if isinstance(value, ast.Constant):
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}={value.value!r}"
                        )
                    elif (
                        isinstance(value, ast.Attribute)
                        and value.attr in {"DEFAULT_TEXT", "DEFAULT_IMAGE"}
                    ):
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}=DEFAULT"
                        )
        self.assertEqual(
            offenders,
            [],
            "业务运行时固定模型，绕过员工/全局选择:\n" + "\n".join(offenders),
        )


class ProviderGatewayBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_rejects_unknown_model_before_credentials_or_network(self):
        with patch.object(
            providers,
            "yunwu_conf",
            side_effect=AssertionError("未知模型不应进入供应商配置"),
        ):
            with self.assertRaisesRegex(providers.ProviderError, "文本模型不可用"):
                await providers.chat("测试", model="invented-model")

    async def test_vision_resolves_employee_text_model(self):
        with patch.object(
            providers, "text_model_for", return_value="gpt-5.5"
        ) as route, patch.object(
            providers,
            "_chat_content",
            AsyncMock(return_value={"text": "识别结果", "cost_usd": 0.0, "tokens": 7}),
            create=True,
        ) as request:
            got = await providers.call_vision(
                5,
                "识别图片",
                [("image/png", "YWJj")],
                timeout=12,
                token="vision:test",
            )

        route.assert_called_once_with(5)
        self.assertEqual(request.await_args.kwargs["model"], "gpt-5.5")
        self.assertEqual(got["text"], "识别结果")

    async def test_web_json_repair_keeps_configured_cloud_tool_model(self):
        research = {
            "text": "原帖：https://example.com/post/1",
            "cost_usd": 0,
            "tokens": 1,
        }
        repaired = {
            "text": '{"source_url":"https://example.com/post/1"}',
            "cost_usd": 0,
            "tokens": 1,
        }
        with patch.object(
            providers, "yunwu_conf", return_value=("https://proxy.example", "key")
        ), patch.object(
            providers, "text_model_for", return_value=providers.CLAUDE_LOCAL
        ), patch(
            "app.llm.call", AsyncMock(return_value=research)
        ), patch.object(
            providers, "chat", AsyncMock(return_value=repaired)
        ) as gateway:
            await providers.call_web_json(
                "返回 JSON", retries=0, repair_invalid=True
            )

        self.assertEqual(
            gateway.await_args.kwargs["model"], providers.CLAUDE_LOCAL
        )

    async def test_image_edit_resolves_employee_image_model(self):
        with patch.object(
            providers, "image_model_for", return_value="doubao-seedream-5-0-260128"
        ) as route, patch.object(
            providers,
            "_image_edit",
            AsyncMock(return_value=b"safe-image"),
            create=True,
        ) as request:
            got = await providers.edit_image(
                5, "美化产品图", b"source", size="1024x1024"
            )

        route.assert_called_once_with(5)
        self.assertEqual(
            request.await_args.kwargs["model"], "doubao-seedream-5-0-260128"
        )
        self.assertEqual(got, b"safe-image")

    async def test_image_generation_resolves_employee_image_model(self):
        with patch.object(
            providers, "image_model_for", return_value="doubao-seedream-5-0-260128"
        ) as route, patch.object(
            providers,
            "image",
            AsyncMock(return_value=b"generated"),
        ) as request:
            got = await providers.call_image(
                6, "生成封面", size="1024x1536"
            )

        route.assert_called_once_with(6)
        self.assertEqual(
            request.await_args.kwargs["model"], "doubao-seedream-5-0-260128"
        )
        self.assertEqual(got, b"generated")


class BusinessGatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_competitor_watch_uses_benchmark_employee_route(self):
        response = {
            "data": {"items": [], "summary": "平稳", "actions": []},
            "cost_usd": 0,
            "tokens": 1,
        }
        with patch.object(
            growth,
            "watch_conf",
            return_value={
                "targets": [{"name": "对标品牌", "platform": "小红书", "note": ""}]
            },
        ), patch.object(
            providers, "call_text_json", AsyncMock(return_value=response)
        ) as gateway:
            await growth.bench_report(2, save=False)

        self.assertEqual(gateway.await_args.args[0], 2)

    async def test_boss_metadata_displays_effective_employee_model(self):
        from app import main

        station = {
            "idx": 0,
            "key": "trend",
            "name": "趋势官",
            "dept": "热点雷达部",
            "emoji": "📡",
            "color": "#000",
            "intro": "介绍",
            "duty": "趋势",
            "approval": "pick",
            "skill": "Horizon",
            "model": providers.DEFAULT_TEXT,
            "run": object(),
        }
        with patch.object(main, "_is_boss", return_value=True), patch.object(
            main.registry, "STATIONS", [station]
        ), patch.object(
            main.departments, "list_depts", return_value=[]
        ), patch.object(
            providers, "text_model_for", return_value="gpt-5.5"
        ):
            payload = main.meta()

        self.assertEqual(payload["stations"][0]["model"], "gpt-5.5")

    async def test_uploaded_image_ocr_uses_global_text_model_route(self):
        from app import main

        class Upload:
            filename = "receipt.png"

        with patch.object(main, "_need_any_work_module"), patch.object(
            main, "_read_limited", AsyncMock(return_value=b"image")
        ), patch.object(
            providers,
            "call_vision",
            AsyncMock(
                return_value={"text": "【文字】收据【画面】纸张", "cost_usd": 0, "tokens": 1}
            ),
        ) as gateway:
            got = await main.parse_file(Upload())

        self.assertIsNone(gateway.await_args.args[0])
        self.assertEqual(got["text"], "【文字】收据【画面】纸张")

    async def test_avatar_link_rewrite_uses_writer_employee_route(self):
        from app import linkgrab, main

        result = {
            "text": "这是一段足够长并且可以直接使用的安全中文口播稿，包含钩子与结尾互动。",
            "cost_usd": 0,
            "tokens": 1,
        }
        with patch.object(main, "TEN", return_value=2), patch.object(
            linkgrab, "is_video_link", return_value=False
        ), patch.object(
            linkgrab, "fetch_page_text", AsyncMock(return_value="公开页面正文")
        ), patch.object(
            providers, "call_text", AsyncMock(return_value=result)
        ) as gateway:
            await main._avatar_script_from_link_work(
                {}, "https://example.com/post", 30
            )

        self.assertEqual(gateway.await_args.args[0], 3)

    async def test_menu_copy_uses_writer_employee_vision_route(self):
        result = {
            "text": json.dumps({"item": "咖啡"}, ensure_ascii=False),
            "cost_usd": 0.0,
            "tokens": 3,
        }
        with patch.object(
            providers, "call_vision", AsyncMock(return_value=result), create=True
        ) as gateway:
            got = await growth.menu_copy(1, "YWJj", "image/png", "写菜单")

        self.assertEqual(got["item"], "咖啡")
        self.assertEqual(gateway.await_args.args[0], 3)

    async def test_product_shot_uses_media_employee_image_route(self):
        with patch.object(
            providers, "edit_image", AsyncMock(return_value=b"edited"), create=True
        ) as gateway:
            got = await growth.product_shot(1, b"source", "桌面场景")

        self.assertEqual(got, b"edited")
        self.assertEqual(gateway.await_args.args[0], 5)

    async def test_video_script_uses_writer_employee_text_route(self):
        with patch.object(
            providers,
            "call_text",
            AsyncMock(return_value={"text": "一" * 200, "cost_usd": 0, "tokens": 1}),
        ) as gateway:
            got = await textvideo.make_script("标题", "原文" * 300)

        self.assertEqual(gateway.await_args.args[0], 3)
        self.assertEqual(got, "一" * 200)

    async def test_image_search_queries_use_media_employee_text_route(self):
        with patch.object(
            providers,
            "call_text",
            AsyncMock(
                return_value={
                    "text": '{"queries":["咖啡产品图"]}',
                    "cost_usd": 0,
                    "tokens": 1,
                }
            ),
        ) as gateway:
            got = await imagehunt._gen_queries(
                "咖啡新品", [{"slot": "首图", "desc": "咖啡"}]
            )

        self.assertEqual(gateway.await_args.args[0], 5)
        self.assertEqual(got, ["咖啡产品图"])


if __name__ == "__main__":
    unittest.main()
