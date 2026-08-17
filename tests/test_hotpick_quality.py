"""热点必发结果质量门回归测试。"""
import unittest
from unittest.mock import AsyncMock, patch

from app import growth, providers


class HotPickQualityGateTests(unittest.IsolatedAsyncioTestCase):
    def _valid_pick(self, title="可交付选题"):
        return {
            "title": title,
            "why": "今天有明确热度证据",
            "angle": "从老板关心的结果切入",
            "direction": "直接给内容流水线开工",
        }

    async def _run(self, data):
        response = {"data": data, "cost_usd": 0.1, "tokens": 10}
        save = AsyncMock()
        self.last_save = save
        with patch.object(
            growth.providers,
            "call_text_json",
            new=AsyncMock(return_value=response),
        ), patch.object(
            growth.db,
            "aset_setting",
            new=save,
        ):
            result = await growth.hot_pick(
                2, "家居", channels=["微博热搜"], save=True
            )
        return result, save

    async def test_missing_empty_or_non_list_picks_raise_stable_provider_error(self):
        for data in ({}, {"picks": []}, {"picks": {"title": "错误类型"}}):
            with self.subTest(data=data):
                with self.assertRaises(providers.ProviderError) as caught:
                    _result, save = await self._run(data)
                self.assertEqual("热点扫描未获得有效联网结果，本次结果不交付", str(caught.exception))
                self.last_save.assert_not_awaited()

    async def test_permission_restricted_or_unavailable_network_declaration_is_rejected(self):
        for field, declaration in (
            ("scan_note", "WebSearch权限不足"),
            ("scan_note", "访问受限，无法使用联网能力"),
            ("scan_note", "当前环境无法联网"),
            ("status", "受限"),
            ("error", "permission denied"),
            ("status", "restricted"),
            ("message", "network unavailable"),
        ):
            with self.subTest(field=field, declaration=declaration):
                with self.assertRaises(providers.ProviderError) as caught:
                    await self._run({
                        "picks": [self._valid_pick()],
                        field: declaration,
                    })
                self.assertEqual("热点扫描未获得有效联网结果，本次结果不交付", str(caught.exception))

    async def test_all_picks_missing_a_required_field_are_rejected(self):
        for missing in ("title", "why", "angle", "direction"):
            pick = self._valid_pick()
            pick[missing] = ""
            with self.subTest(missing=missing):
                with self.assertRaises(providers.ProviderError) as caught:
                    await self._run({"picks": [pick]})
                self.assertEqual("热点扫描未获得有效联网结果，本次结果不交付", str(caught.exception))

    async def test_boolean_or_numeric_pick_fields_are_not_coerced_to_text(self):
        for field, value in (("title", True), ("why", 1), ("angle", False),
                             ("direction", 3.14)):
            pick = self._valid_pick()
            pick[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(providers.ProviderError) as caught:
                    await self._run({"picks": [pick]})
                self.assertEqual("热点扫描未获得有效联网结果，本次结果不交付", str(caught.exception))

    async def test_structured_false_status_is_fail_closed(self):
        for field in ("status", "ok", "success", "available", "enabled", "reachable"):
            with self.subTest(field=field):
                with self.assertRaises(providers.ProviderError) as caught:
                    await self._run({
                        "picks": [self._valid_pick()],
                        field: False,
                    })
                self.assertEqual("热点扫描未获得有效联网结果，本次结果不交付", str(caught.exception))

    async def test_failure_declaration_inside_pick_fields_is_rejected(self):
        for field in ("title", "why", "angle", "direction"):
            pick = self._valid_pick()
            pick[field] = "WebSearch 权限不足，无法联网检索"
            with self.subTest(field=field):
                with self.assertRaises(providers.ProviderError) as caught:
                    await self._run({"picks": [pick]})
                self.assertEqual("热点扫描未获得有效联网结果，本次结果不交付", str(caught.exception))

    async def test_valid_picks_are_cleaned_filtered_and_capped_at_three(self):
        first = self._valid_pick("  第一条  ")
        first["unexpected"] = "不应进入持久结果"
        second = self._valid_pick("第二条")
        third = self._valid_pick("第三条")
        fourth = self._valid_pick("第四条")
        invalid = {"title": "缺字段", "why": "只有两项"}

        result, save = await self._run({
            "picks": [first, invalid, second, third, fourth],
            "scan_note": "已完成联网扫描",
            "unexpected": "不应进入持久结果",
        })

        self.assertEqual(["第一条", "第二条", "第三条"],
                         [pick["title"] for pick in result["picks"]])
        self.assertEqual(3, len(result["picks"]))
        for pick in result["picks"]:
            self.assertTrue(all(pick.get(field) for field in (
                "title", "why", "angle", "direction"
            )))
            self.assertEqual(set(growth._HOT_PICK_REQUIRED_FIELDS), set(pick))
        self.assertNotIn("unexpected", result)
        self.assertEqual(2, save.await_count)


if __name__ == "__main__":
    unittest.main()
