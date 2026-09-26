"""Phase0：质检关卡对模型 JSON 的容错 + 北京时间日期(真实调用 gate.check)。"""
import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import gate, providers


class _FakeDatetime(datetime):
    """固定在 UTC 2026-09-25 17:30,也就是北京时间 9 月 26 日凌晨。"""

    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 9, 25, 17, 30, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


class GateJsonToleranceCase(unittest.TestCase):
    def setUp(self):
        self.prompts = []
        self.data = {"issues": []}
        rules = patch.object(
            gate, "_rules", lambda: {"sensitive_words": [], "notes": ""})
        rules.start()
        self.addCleanup(rules.stop)

        async def fake_call(_idx, prompt, **_kwargs):
            self.prompts.append(prompt)
            return {"data": self.data, "cost_usd": 0.01, "tokens": 7}

        call = patch.object(providers, "call_text_json", fake_call)
        call.start()
        self.addCleanup(call.stop)

    def _check(self, research=None):
        return asyncio.run(gate.check(
            "周末到店福利", "本周末到店消费可领取小礼品,欢迎大家来玩。",
            ["小红书"], research=research,
        ))

    def test_null_issues_passes_without_type_error(self):
        self.data = {"issues": None}
        result = self._check()
        self.assertTrue(result["passed"])
        self.assertEqual([], result["issues"])
        self.assertEqual(7, result["tokens"])

    def test_string_and_mixed_issues_are_normalized(self):
        self.data = {"issues": [
            "第二段有夸大用语",
            {"type": "广告法", "severity": "高", "detail": "出现“最”字"},
            {"type": "平台", "severity": "high", "detail": "导流"},
            None, 42, ["nested"], "  ",
        ]}
        result = self._check()
        self.assertFalse(result["passed"])   # 有一条"高"风险
        self.assertEqual(3, len(result["issues"]))
        for issue in result["issues"]:
            self.assertIsInstance(issue, dict)
            self.assertLessEqual({"type", "severity", "detail"}, set(issue))
        self.assertEqual("第二段有夸大用语", result["issues"][0]["detail"])
        self.assertEqual("中", result["issues"][2]["severity"])

    def test_single_issue_object_and_non_dict_data(self):
        self.data = {"issues": {"type": "事实", "severity": "高风险",
                                "detail": "日期写错"}}
        result = self._check()
        self.assertFalse(result["passed"])
        self.assertEqual("高", result["issues"][0]["severity"])

        self.data = ["直接返回了列表形式的提示"]
        result = self._check()
        self.assertTrue(result["passed"])
        self.assertEqual("直接返回了列表形式的提示", result["issues"][0]["detail"])

    def test_research_sources_as_url_strings_and_messy_facts(self):
        research = {
            "facts": "门店 2020 年开业",
            "data_points": [None, 3.5, {"label": "客单价", "value": "68 元"},
                            ["x"], "复购率 40%"],
            "sources": [
                "https://example.com/a",
                {"title": "行业报告", "url": "https://example.com/b"},
                {"url": "https://example.com/c"},
                None, 7,
            ],
        }
        result = self._check(research=research)
        self.assertTrue(result["passed"])
        prompt = self.prompts[-1]
        self.assertIn("- 门店 2020 年开业", prompt)
        self.assertIn("- 3.5", prompt)
        self.assertIn("客单价 68 元", prompt)
        self.assertIn("- 复购率 40%", prompt)
        self.assertIn("https://example.com/a;行业报告;https://example.com/c",
                      prompt)

    def test_research_with_null_fields_or_wrong_type(self):
        for research in ({"facts": None, "data_points": None, "sources": None},
                         {"sources": "https://example.com"},
                         ["not", "a", "dict"]):
            with self.subTest(research=research):
                result = self._check(research=research)
                self.assertTrue(result["passed"])

    def test_prompt_uses_beijing_date(self):
        with patch.object(gate, "datetime", _FakeDatetime):
            self.assertEqual("2026-09-26", gate.beijing_today())
            self._check()
        self.assertIn("今天是 2026-09-26", self.prompts[-1])


if __name__ == "__main__":
    unittest.main()
