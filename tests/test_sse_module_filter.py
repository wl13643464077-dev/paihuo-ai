"""SSE 按板块过滤:content-only 成员不再收数字人事件白刷新。"""
import asyncio
import unittest
from unittest.mock import patch

from app.engine import engine


class SseModuleFilterCase(unittest.TestCase):
    def _delivered(self, subscriber, ev):
        q = asyncio.Queue()
        hits = []
        with patch.object(engine, "_tid_of", return_value=2), \
             patch.object(engine, "public_event", side_effect=lambda e: e), \
             patch.object(engine, "internal_event", side_effect=lambda e: e), \
             patch.object(engine, "_dispatch",
                          side_effect=lambda fn, *a: hits.append(a[1])), \
             patch.dict(engine.subscribers, {q: subscriber}, clear=True):
            engine.broadcast(ev)
        return hits

    def test_content_member_skips_avatar_events(self):
        member = (2, False, frozenset({"content"}))
        self.assertEqual(
            [], self._delivered(member, {"type": "avatar_update", "job_id": 1}),
            "content-only 成员不该收到数字人事件")
        self.assertEqual(
            1, len(self._delivered(member, {"type": "job_update", "job_id": 1})),
            "自己板块的事件照常投递")

    def test_owner_receives_everything(self):
        owner = (2, False, None)
        self.assertEqual(
            1, len(self._delivered(owner, {"type": "avatar_update", "job_id": 1})))

    def test_legacy_two_tuple_subscriber_still_works(self):
        legacy = (2, False)
        self.assertEqual(
            1, len(self._delivered(legacy, {"type": "avatar_update", "job_id": 1})),
            "旧格式订阅者(无板块信息)不受过滤影响")

    def test_unmapped_event_types_broadcast_to_members(self):
        member = (2, False, frozenset({"library"}))
        self.assertEqual(
            1, len(self._delivered(member, {"type": "meeting_update", "id": 3})),
            "未映射类型保持原行为,不误伤")


if __name__ == "__main__":
    unittest.main()
