"""Confirmed-brand grounding for matrix copy and direct video scripts."""

import unittest
from unittest.mock import AsyncMock, patch

from app import brand_package, growth, textvideo
from app.skills import registry


BRAND = {
    "version": 7,
    "brand_name": "山海面馆",
    "fields": {
        "brand_name": "山海面馆",
        "store_name": "山海面馆望京店",
        "slogan": "一碗见山海",
    },
}


class DirectVideoBrandReviewTests(unittest.TestCase):
    def test_no_confirmed_package_keeps_existing_user_script_behavior(self):
        with patch.object(brand_package, "get_active", return_value=None):
            review = textvideo.review_user_script_brand(
                2, "教学视频", "今天分享三种煮面的技巧。"
            )
        self.assertEqual({
            "brand_version": None, "warnings": [], "blocking": [],
        }, review)

    def test_missing_brand_is_warning_not_auto_insert_or_block(self):
        script = "今天教你三种煮面的技巧，第一步先把水烧开。"
        with patch.object(brand_package, "get_active", return_value=BRAND):
            review = textvideo.review_user_script_brand(2, "厨房技巧", script)
        self.assertEqual(7, review["brand_version"])
        self.assertFalse(review["blocking"])
        self.assertIn("不会自动补写品牌", review["warnings"][0])
        self.assertNotIn("山海面馆", script)

    def test_matching_brand_or_store_in_title_needs_no_warning(self):
        review = textvideo.review_user_script_brand(
            2, "山海面馆望京店探店", "一碗好面，现煮现上。", active_brand=BRAND,
        )
        self.assertEqual([], review["warnings"])
        self.assertEqual([], review["blocking"])

    def test_explicit_wrong_brand_or_platform_placeholder_is_blocked(self):
        wrong_name = textvideo.review_user_script_brand(
            2, "今日带货", "品牌：别家面馆\n今天新品上市。", active_brand=BRAND,
        )
        platform_as_store = textvideo.review_user_script_brand(
            2, "今日带货", "这款面由派活出品，欢迎到店。", active_brand=BRAND,
        )
        self.assertTrue(wrong_name["blocking"])
        self.assertTrue(platform_as_store["blocking"])

    def test_unverified_store_name_is_not_mistaken_for_known_brand(self):
        brand_without_store = {**BRAND, "fields": {"brand_name": "山海面馆"}}
        review = textvideo.review_user_script_brand(
            2, "教程", "店名：山海面馆望京店\n今天教你做面。",
            active_brand=brand_without_store,
        )
        self.assertEqual([], review["blocking"])


class GeneratedBrandContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_condensed_script_receives_confirmed_facts(self):
        gateway = AsyncMock(return_value={"text": "山海面馆的招牌面，值得一试。" * 5})
        with patch.object(textvideo, "_call_textvideo_employee", gateway):
            output = await textvideo.make_script(
                "新品", "煮面教学。" * 80,
                brand_context="【已确认品牌知识包 v7】门店名：山海面馆望京店",
            )
        self.assertIn("山海面馆", output)
        prompt = gateway.await_args.args[1]
        self.assertIn("已确认品牌知识包 v7", prompt)
        self.assertIn("不得虚构新事实", prompt)

    async def test_short_user_body_is_not_silently_rewritten(self):
        gateway = AsyncMock()
        with patch.object(textvideo, "_call_textvideo_employee", gateway):
            output = await textvideo.make_script(
                "标题", "今天分享三种煮面技巧。", brand_context="山海面馆",
            )
        self.assertEqual("今天分享三种煮面技巧。", output)
        gateway.assert_not_awaited()

    async def test_variants_get_confirmed_package_and_surface_missing_brand(self):
        generated = {
            "data": {"variants": [{
                "style": "故事", "hook": "你猜这碗面有什么不同？",
                "script": "山海面馆望京店今天讲讲一碗面的故事。",
            }]},
            "cost_usd": 0.2, "tokens": 12,
        }

        async def arun(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        gateway = AsyncMock(return_value=generated)
        with patch.object(brand_package, "get_active", return_value=BRAND), \
                patch.object(registry, "company_block", return_value=
                             "【已确认品牌知识包 v7】门店名：山海面馆望京店"), \
                patch.object(growth.db, "arun", side_effect=arun), \
                patch.object(growth, "_call_toolbox_employee_json", gateway):
            result = await growth.script_variants(
                2, "这是一份关于现煮面条的通用口播稿。", 3, "温和科普"
            )
        prompt = gateway.await_args.args[1]
        self.assertIn("已确认品牌知识包 v7", prompt)
        self.assertIn("山海面馆望京店", prompt)
        self.assertEqual(7, result["brand_version"])
        self.assertTrue(result["brand_warnings"])
        self.assertEqual(generated["data"]["variants"], result["variants"])

    async def test_variants_reject_explicit_wrong_brand_before_model_call(self):
        async def arun(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        gateway = AsyncMock()
        with patch.object(brand_package, "get_active", return_value=BRAND), \
                patch.object(growth.db, "arun", side_effect=arun), \
                patch.object(growth, "_call_toolbox_employee_json", gateway):
            with self.assertRaises(textvideo.BrandCopyMismatch):
                await growth.script_variants(
                    2, "品牌：别家面馆\n这是一篇带货口播稿。", 3, ""
                )
        gateway.assert_not_awaited()

    async def test_variants_reject_generated_wrong_brand(self):
        async def arun(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        generated = AsyncMock(return_value={
            "data": {"variants": [{"script": "品牌：别家面馆\n欢迎光临。"}]},
            "cost_usd": 0, "tokens": 1,
        })
        with patch.object(brand_package, "get_active", return_value=BRAND), \
                patch.object(registry, "company_block", return_value="确认版资料"), \
                patch.object(growth.db, "arun", side_effect=arun), \
                patch.object(growth, "_call_toolbox_employee_json", generated):
            with self.assertRaises(textvideo.BrandCopyMismatch):
                await growth.script_variants(
                    2, "山海面馆推出了新的招牌面，欢迎大家品尝。", 3, ""
                )


class VideoWorkerBrandTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_renders_direct_user_script_unchanged_and_surfaces_warning(self):
        original = "今天分享三种煮面技巧，第一步先把水烧开。"
        steps = []

        async def arun(fn, *args, **kwargs):
            if fn is textvideo._steps_append:
                steps.append(args[1])
                return True
            if fn is textvideo._deliver_job:
                return True
            return fn(*args, **kwargs)

        build = AsyncMock(return_value="/files/tv/test.mp4")
        with patch.object(brand_package, "get_active", return_value=BRAND), \
                patch.object(registry, "company_block", return_value="确认版 v7"), \
                patch.object(textvideo.db, "arun", side_effect=arun), \
                patch.object(textvideo.db, "aone", AsyncMock(return_value={
                    "status": "running", "billing_status": "charged",
                })), \
                patch.object(textvideo, "build", build), \
                patch.object(textvideo, "_cleanup_hunt_assets"), \
                patch("app.notify.push"):
            await textvideo._run_job_inner(
                21, {"job_id": None},
                {"title": "厨房技巧", "script": original, "images": [], "bgm": "none"},
                2, None,
            )

        self.assertEqual(original, build.await_args.args[3])
        self.assertTrue(any("已确认品牌知识包 v7" in step for step in steps))
        self.assertTrue(any("不会自动补写品牌" in step for step in steps))

    async def test_worker_refunds_instead_of_rendering_conflicting_copy(self):
        settlements = []

        async def arun(fn, *args, **kwargs):
            if fn is textvideo._steps_append:
                return True
            if fn is textvideo.settle_failure:
                settlements.append((args, kwargs))
                return True
            return fn(*args, **kwargs)

        build = AsyncMock()
        with patch.object(brand_package, "get_active", return_value=BRAND), \
                patch.object(registry, "company_block", return_value="确认版 v7"), \
                patch.object(textvideo.db, "arun", side_effect=arun), \
                patch.object(textvideo.db, "aone", AsyncMock(return_value={
                    "status": "running", "billing_status": "charged",
                })), \
                patch.object(textvideo, "build", build), \
                patch.object(textvideo, "cleanup_job_assets"), \
                patch.object(textvideo, "_cleanup_hunt_assets"):
            await textvideo._run_job_inner(
                22, {"job_id": None},
                {"title": "厨房技巧", "script": "品牌：别家面馆\n新品上市", "bgm": "none"},
                2, None,
            )

        build.assert_not_awaited()
        self.assertEqual(1, len(settlements))
        self.assertEqual("failed", settlements[0][1]["terminal_status"])


if __name__ == "__main__":
    unittest.main()
