"""员工自动进化(实战心得)与 TinyFish 情报通道的降级契约."""
import unittest
from unittest.mock import patch

from app import departments, employees, tinyfish


class InsightStoreTests(unittest.TestCase):
    TENANT = 987654
    IDX = 1701

    def _cleanup(self):
        from app import db
        for kind in ("pending", "adopted"):
            db.set_setting(
                employees._insight_setting_key(kind, self.TENANT, self.IDX), None,
            )

    def test_insight_roundtrip_caps_and_prompt_block(self):
        self._cleanup()
        try:
            rows = [{"insight": f"心得{i}", "task_id": i, "verdict": "adopt", "at": i}
                    for i in range(30)]
            employees.save_insights("adopted", self.TENANT, self.IDX, rows)
            adopted = employees.insight_lists(self.TENANT, self.IDX)["adopted"]
            self.assertEqual(len(adopted), employees.INSIGHT_ADOPTED_MAX)
            self.assertEqual(adopted[-1]["insight"], "心得29")
            text = employees.adopted_insights_text(self.TENANT, self.IDX)
            self.assertIn("- 心得29", text)
            self.assertEqual(
                text.count("\n") + 1, employees.INSIGHT_ADOPTED_MAX,
            )
        finally:
            self._cleanup()

    def test_corrupt_setting_treated_as_empty(self):
        from app import db
        key = employees._insight_setting_key("pending", self.TENANT, self.IDX)
        db.set_setting(key, "{not json")
        try:
            self.assertEqual(
                employees.insight_lists(self.TENANT, self.IDX)["pending"], [],
            )
        finally:
            db.set_setting(key, None)

    def test_insights_injected_into_system_prompt_only(self):
        e = {"idx": 1701, "name": "测试专员", "dept_name": "餐饮产业部",
             "group": "测试组", "desc": "测试职责", "duty": "测试",
             "dept_key": "restaurant", "md": "手册内容"}
        bundle = departments.build_task_prompt(
            e, {"direction": "老板任务"}, "", "", [],
            insights_text="- 报告先给结论再给数据",
        )
        self.assertIn("实战心得", bundle.system)
        self.assertIn("报告先给结论再给数据", bundle.system)
        self.assertNotIn("实战心得", bundle.user)


class TinyFishDegradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_without_key_returns_empty(self):
        with patch.object(tinyfish, "api_key", return_value=""):
            self.assertEqual(await tinyfish.search("餐饮"), [])
            self.assertEqual(await tinyfish.fetch(["https://example.com"]), [])
            bundle = await tinyfish.research_bundle(["餐饮"])
            self.assertEqual(bundle["material"], "")

    async def test_call_web_json_skips_tinyfish_without_key(self):
        from app import providers
        with patch.object(tinyfish, "available", return_value=False), \
                patch.object(providers, "_tinyfish_web_json") as tf_path, \
                patch.object(providers, "yunwu_conf", return_value=("", "")):
            with self.assertRaises(providers.ProviderError):
                await providers.call_web_json("任务", timeout=5, retries=0)
            tf_path.assert_not_called()


if __name__ == "__main__":
    unittest.main()
