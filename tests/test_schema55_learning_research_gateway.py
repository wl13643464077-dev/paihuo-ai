"""Schema 55 web-learning gateway provenance and page-verification contract."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app import providers


class Schema55LearningResearchGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.authority_patch = patch.object(
            providers.learningevidence,
            "authority_for_url",
            side_effect=lambda url: (
                "regulator" if "source1.example" in str(url) else "industry"
            ),
        )
        self.authority_patch.start()

    def tearDown(self):
        self.authority_patch.stop()

    @staticmethod
    def _gateway_result(count=6, *, attempts=3, success=3):
        return {
            "data": {"research_summary": "只作检索摘要，不直接激活能力"},
            "cost_usd": 0.3,
            "tokens": 600,
            "tool_usage": {"WebSearch": {
                "attempts": attempts, "success": success, "errors": 0,
            }},
            "web_sources": [
                {
                    "source_title": f"证据 {i}",
                    "source_url": f"https://source{i}.example/article?utm_source=search",
                }
                for i in range(1, count + 1)
            ],
        }

    async def test_verified_learning_sources_come_only_from_captured_and_fetched_urls(self):
        async def fetched(url, **_kwargs):
            return {
                "source_url": url.split("?", 1)[0],
                "source_title": "受控抓取标题",
                "text": "这是一段由应用安全抓取并用于岗位研究的公开证据正文。" * 5,
            }

        with patch.object(
            providers, "call_web_json", AsyncMock(return_value=self._gateway_result())
        ) as search, patch(
            "app.linkgrab.fetch_page_evidence", AsyncMock(side_effect=fetched)
        ) as fetch:
            result = await providers.call_verified_learning_research(
                "只检索脱敏后的岗位主题", min_queries=3, max_sources=6,
            )

        search.assert_awaited_once()
        self.assertEqual(6, fetch.await_count)
        self.assertEqual(6, len(result["sources"]))
        self.assertEqual(3, result["query_count"])
        for source in result["sources"]:
            self.assertEqual("websearch", source["capture_provider"])
            self.assertTrue(source["capture_event_id"])
            self.assertEqual(200, source["http_status"])
            self.assertTrue(source["tls_valid"])
            self.assertRegex(source["content_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("utm_source", source["url"])

    async def test_less_than_three_successful_queries_fails_closed(self):
        with patch.object(
            providers, "call_web_json",
            AsyncMock(return_value=self._gateway_result(attempts=3, success=2)),
        ), self.assertRaisesRegex(providers.ProviderError, "至少完成 3 次"):
            await providers.call_verified_learning_research(
                "岗位主题", min_queries=3,
            )

    async def test_unreachable_and_uncaptured_model_urls_do_not_count(self):
        result = self._gateway_result(count=5)
        result["data"]["invented_url"] = "https://invented.invalid/fake"

        async def fetched(url, **_kwargs):
            if "source5" in url:
                raise ValueError("blocked")
            return {"source_url": url, "source_title": "标题", "text": "公开证据" * 40}

        with patch.object(
            providers, "call_web_json", AsyncMock(return_value=result)
        ), patch(
            "app.linkgrab.fetch_page_evidence", AsyncMock(side_effect=fetched)
        ):
            verified = await providers.call_verified_learning_research("岗位主题")

        self.assertEqual(4, len(verified["sources"]))
        serialized = repr(verified)
        self.assertNotIn("invented.invalid", serialized)

    async def test_authority_is_derived_from_redirect_verified_final_url(self):
        result = self._gateway_result(count=1)
        result["web_sources"][0]["authority_level"] = "regulator"

        async def fetched(_url, **_kwargs):
            return {
                "source_url": "https://final-authority.example/rule",
                "source_title": "最终页",
                "text": "可核验公开规则与专业方法。" * 20,
            }

        with (
            patch.object(
                providers, "call_web_json", AsyncMock(return_value=result),
            ),
            patch(
                "app.linkgrab.fetch_page_evidence", AsyncMock(side_effect=fetched),
            ),
            patch.object(
                providers.learningevidence,
                "authority_for_url",
                side_effect=lambda url: (
                    "official"
                    if str(url) == "https://final-authority.example/rule"
                    else "industry"
                ),
            ) as authority,
        ):
            verified = await providers.call_verified_learning_research(
                "岗位主题", min_queries=3, max_sources=1,
            )
        self.assertEqual("official", verified["sources"][0]["authority_level"])
        self.assertEqual(
            "https://final-authority.example/rule",
            verified["sources"][0]["final_url"],
        )
        authority.assert_called_once_with("https://final-authority.example/rule")


if __name__ == "__main__":
    unittest.main()
