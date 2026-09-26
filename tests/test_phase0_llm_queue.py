"""Phase0：联网调用排队有上限、有超时、有进度；微信图片压缩不卡事件循环。"""
import asyncio
import io
import os
import sys
import threading
import unittest
from unittest.mock import AsyncMock, patch

from app import llm, providers, wechat


class LLMQueueCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 每个用例一个绑定到当前事件循环的新闸门。
        self._old_sem = llm._sem
        llm._sem = asyncio.Semaphore(1)
        llm._WAITERS.clear()

    async def asyncTearDown(self):
        llm._sem = self._old_sem
        llm._WAITERS.clear()

    def test_concurrency_is_configurable_with_safe_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONTENTCREW_LLM_CONCURRENCY", None)
            self.assertEqual(6, llm._env_number("CONTENTCREW_LLM_CONCURRENCY", 6, 1, 64))
        with patch.dict(os.environ, {"CONTENTCREW_LLM_CONCURRENCY": "10"}):
            self.assertEqual(10, llm._env_number("CONTENTCREW_LLM_CONCURRENCY", 6, 1, 64))
        for bad in ("0", "-3", "abc", "1000", "nan"):
            with patch.dict(os.environ, {"CONTENTCREW_LLM_CONCURRENCY": bad}):
                self.assertEqual(
                    6, llm._env_number("CONTENTCREW_LLM_CONCURRENCY", 6, 1, 64)
                )

    async def test_queue_wait_times_out_with_recognisable_error(self):
        holder = await llm._acquire_slot(lambda *_a: None, 1)
        steps = []
        with self.assertRaises(llm.LLMQueueTimeout) as caught:
            await llm._acquire_slot(lambda k, l: steps.append((k, l)), 0.05)
        self.assertIsInstance(caught.exception, llm.LLMError)
        self.assertEqual("queue", steps[0][0])
        self.assertIn("排队中", steps[0][1])
        self.assertEqual(0, llm.queue_depth())
        # 超时的等待者不能吞掉名额：放掉后下一位立刻拿到。
        holder.release()
        again = await asyncio.wait_for(llm._acquire_slot(lambda *_a: None, 1), 1)
        again.release()
        self.assertFalse(llm._sem.locked())

    async def test_progress_reports_how_many_are_ahead_and_order_is_fifo(self):
        holder = await llm._acquire_slot(lambda *_a: None, 1)
        order, first_steps, second_steps = [], [], []

        async def waiter(name, steps):
            slot = await llm._acquire_slot(lambda k, l: steps.append(l), 5)
            order.append(name)
            await asyncio.sleep(0.01)
            slot.release()

        first = asyncio.create_task(waiter("first", first_steps))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(waiter("second", second_steps))
        await asyncio.sleep(0.01)
        self.assertEqual(2, llm.queue_depth())
        self.assertIn("马上轮到你", first_steps[0])
        self.assertIn("前面还有 1 个任务", second_steps[0])
        holder.release()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(["first", "second"], order)
        self.assertEqual(0, llm.queue_depth())
        self.assertFalse(llm._sem.locked())

    async def test_cancelled_waiter_releases_its_place(self):
        holder = await llm._acquire_slot(lambda *_a: None, 1)
        pending = asyncio.create_task(llm._acquire_slot(lambda *_a: None, 5))
        await asyncio.sleep(0.01)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(0, llm.queue_depth())
        holder.release()
        slot = await asyncio.wait_for(llm._acquire_slot(lambda *_a: None, 1), 1)
        slot.release()

    async def test_call_queue_timeout_happens_before_spawning_and_maps_to_refund_text(self):
        holder = await llm._acquire_slot(lambda *_a: None, 1)
        spawn = AsyncMock()
        try:
            with patch.object(llm, "CLAUDE", sys.executable), \
                    patch.object(llm.asyncio, "create_subprocess_exec", new=spawn):
                with self.assertRaises(llm.LLMQueueTimeout) as caught:
                    await llm.call(
                        "hi",
                        provider_env={
                            "ANTHROPIC_BASE_URL": "https://example.invalid",
                            "ANTHROPIC_AUTH_TOKEN": "t",
                        },
                        queue_timeout=0.05,
                    )
        finally:
            holder.release()
        spawn.assert_not_awaited()
        public = providers.public_failure_message(caught.exception)
        self.assertIn("自动退回", public)
        self.assertFalse(llm._sem.locked())


class WechatImageThreadCase(unittest.IsolatedAsyncioTestCase):
    async def test_image_conversion_runs_off_event_loop_thread(self):
        loop_thread = threading.get_ident()
        seen = []

        def fake_convert(data):
            seen.append(threading.get_ident())
            return b"jpg"

        class _Resp:
            def json(self):
                return {"media_id": "m1", "url": "https://mmbiz.qpic.cn/x"}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        with patch.object(wechat, "token", new=AsyncMock(return_value="tok")), \
                patch.object(wechat, "_to_jpg_under_1m", side_effect=fake_convert), \
                patch.object(wechat.httpx, "AsyncClient", _Client):
            self.assertEqual("m1", await wechat.upload_thumb(2, b"raw"))
            self.assertEqual(
                "https://mmbiz.qpic.cn/x",
                await wechat.upload_content_img(2, b"raw"),
            )
        self.assertEqual(2, len(seen))
        self.assertTrue(all(ident != loop_thread for ident in seen))

    def test_real_conversion_still_produces_small_jpeg(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (1600, 900), (200, 10, 10, 255)).save(buf, "PNG")
        out = wechat._to_jpg_under_1m(buf.getvalue())
        self.assertTrue(out.startswith(b"\xff\xd8\xff"))
        self.assertLessEqual(len(out), 990 * 1024)


if __name__ == "__main__":
    unittest.main()
