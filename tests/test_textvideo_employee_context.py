"""视频混剪工具必须真实调用撰稿人/多媒体师的私有能力与技能。"""
import inspect
import unittest
from unittest.mock import AsyncMock, patch

from app import providers, textvideo


class TextVideoEmployeeContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_wrapper_keeps_employee_context_private(self):
        bundle = providers.PromptBundle(
            system="SECRET-WORKFLOW\nSECRET-CAPS\nSECRET-SKILLS",
            user="PUBLIC-VIDEO-TASK",
            sensitive=("SECRET-WORKFLOW", "SECRET-CAPS", "SECRET-SKILLS"),
        )
        gateway = AsyncMock(return_value={
            "text": "PUBLIC-DELIVERY", "cost_usd": 0, "tokens": 1,
        })
        with patch.object(
            textvideo, "_textvideo_employee_bundle", return_value=bundle,
        ), patch.object(textvideo.providers, "call_text", gateway):
            result = await textvideo._call_textvideo_employee(3, "ignored")

        call = gateway.await_args
        self.assertEqual(3, call.args[0])
        self.assertEqual("PUBLIC-VIDEO-TASK", call.args[1])
        self.assertEqual(bundle.system, call.kwargs["system_prompt"])
        self.assertEqual(bundle.sensitive, call.kwargs["sensitive_texts"])
        self.assertNotIn("SECRET", str(result))

    async def test_vision_wrapper_uses_private_system_and_checks_leak(self):
        secret = "【你的进修技能库】SECRET-MEDIA-WORKFLOW"
        bundle = providers.PromptBundle(
            system=secret,
            user="PUBLIC-CLIP-TASK",
            sensitive=(secret,),
        )
        gateway = AsyncMock(return_value={
            "text": secret, "cost_usd": 0, "tokens": 1,
        })
        with patch.object(
            textvideo, "_textvideo_employee_bundle", return_value=bundle,
        ), patch.object(textvideo.providers, "call_vision", gateway):
            with self.assertRaises(providers.PrivatePromptLeak):
                await textvideo._call_textvideo_employee_vision(
                    5, "ignored", [("image/png", "YWJj")]
                )

        call = gateway.await_args
        self.assertEqual(5, call.args[0])
        self.assertEqual("PUBLIC-CLIP-TASK", call.args[1])
        self.assertEqual(bundle.system, call.kwargs["system_prompt"])
        self.assertNotIn("SECRET", call.args[1])

    def test_all_employee_attributed_video_calls_use_private_wrappers(self):
        for func, wrapper in (
            (textvideo.make_script, "_call_textvideo_employee("),
            (textvideo._describe_clips, "_call_textvideo_employee_vision("),
            (textvideo._assign_clips, "_call_textvideo_employee("),
            (textvideo._run_job_inner, "_call_textvideo_employee("),
        ):
            with self.subTest(function=func.__name__):
                source = inspect.getsource(func)
                self.assertIn(wrapper, source)


if __name__ == "__main__":
    unittest.main()
