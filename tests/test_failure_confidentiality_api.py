"""Public failure views must never reflect raw provider diagnostics.

These tests deliberately seed legacy rows that predate provider-error
normalisation.  The read boundary must remain safe even when old database
values contain credentials, prompts, upstream response bodies, or stack traces.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import auth, db, employeeidentity, llm, main, providers
from app.engine import engine


SECRET_SENTINEL = (
    "FAILURE_DIAGNOSTIC_SECRET_"
    "d76f5f2d7fca4e8b9cab0db1c3605c59"
)
HUMAN_REJECTION = "人工审核意见：标题请改为客户能立即理解的结果表达"


class FailureConfidentialityApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "failure-confidentiality.db")
        db.conn()

        db.insert("tenants", {
            "id": 1, "name": "平台", "balance": 50,
        })
        db.insert("tenants", {
            "id": 2, "name": "测试租户", "balance": 50,
        })
        self.boss_id = db.insert("users", {
            "id": 101,
            "tenant_id": 1,
            "username": "boss",
            "password_hash": "fixture-boss-hash",
            "role": "root",
            "modules_json": "[]",
            "enabled": 1,
            "must_change_password": 0,
        })
        self.member_id = db.insert("users", {
            "id": 202,
            "tenant_id": 2,
            "username": "failure-boundary-member",
            "password_hash": "fixture-member-hash",
            "role": "member",
            "modules_json": json.dumps(
                ["content", "avatar", "library"], ensure_ascii=False
            ),
            "enabled": 1,
            "must_change_password": 0,
        })
        self.records = {
            1: self._seed_legacy_failure_records(1),
            2: self._seed_legacy_failure_records(2),
        }
        engine.subscribers.clear()
        engine._tid_cache.clear()
        auth.set_current(None)

    def tearDown(self):
        auth.set_current(None)
        engine.subscribers.clear()
        engine._tid_cache.clear()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    @staticmethod
    def _seed_legacy_failure_records(tenant_id: int) -> dict[str, int]:
        raw_error = (
            f"provider response body={SECRET_SENTINEL}; "
            "Authorization: Bearer raw-upstream-token; stack=/srv/private.py:91"
        )
        raw_steps = json.dumps(
            [{
                "k": "error",
                "l": f"供应商异常详情 {raw_error}",
                "msg": f"raw progress {raw_error}",
                "ts": 123.0,
                "t": 123.0,
            }],
            ensure_ascii=False,
        )
        tool_id = db.insert("tool_job", {
            "tenant_id": tenant_id,
            "kind": "leads",
            "params_json": json.dumps({"industry": "企业服务"}),
            "status": "failed",
            "billing_status": "refunded",
            "billing_points": 3,
            "error": raw_error,
            "progress": f"raw progress {raw_error}",
        })
        avatar_id = db.insert("avatar_job", {
            "tenant_id": tenant_id,
            "params_json": json.dumps({
                "prompt": "生成一段公开产品介绍",
                "script": "这是一段公开口播稿",
            }, ensure_ascii=False),
            "status": "failed",
            "billing_status": "refunded",
            "billing_points": 5,
            "steps_json": raw_steps,
            "error": raw_error,
        })
        video_id = db.insert("tv_job", {
            "tenant_id": tenant_id,
            "params_json": json.dumps({
                "title": "公开成片标题",
                "script": "这是一段公开成片口播稿",
            }, ensure_ascii=False),
            "status": "failed",
            "billing_status": "refunded",
            "billing_points": 3,
            "steps_json": raw_steps,
            "error": raw_error,
        })
        publish_id = db.insert("pub_task", {
            "tenant_id": tenant_id,
            "platform": "xhs",
            "payload_json": json.dumps({
                "title": "公开发布标题",
            }, ensure_ascii=False),
            "status": "failed",
            "submission_state": "not_submitted",
            "log": f"legacy browser log {raw_error}",
            "fail_json": json.dumps({
                "kind": "unknown",
                "why": raw_error,
                "fix": raw_error,
                "err": raw_error,
            }, ensure_ascii=False),
        })
        meeting_id = db.insert("meeting", {
            "tenant_id": tenant_id,
            "question": "如何完成本周产品发布",
            "emp_idxs_json": "[0,1]",
            "member_snapshot_json": json.dumps(
                employeeidentity.member_snapshots([0, 1], active_only=True),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "messages_json": json.dumps([
                {
                    "who": "系统",
                    "kind": "error",
                    "text": raw_error,
                    "ts": 123.0,
                },
                {
                    "who": "会议主持人",
                    "kind": "speech",
                    "text": "公开说明：会议已停止。",
                    "ts": 124.0,
                },
            ], ensure_ascii=False),
            "status": "failed",
            "phase": "failed",
            "next_action": raw_error,
            "billing_status": "refunded",
            "billing_points": 2,
        })
        job_id = db.insert("job", {
            "tenant_id": tenant_id,
            "brief_json": json.dumps(
                {"direction": "生成公开的产品发布稿"}, ensure_ascii=False
            ),
            "mode": "copilot",
            "status": "failed",
            "billing_status": "refunded",
            "billing_points": 10,
        })
        db.insert("station_run", {
            "job_id": job_id,
            "station_idx": 0,
            "version": 1,
            "status": "failed",
            "review_comment": raw_error,
            "steps_json": raw_steps,
        })
        db.insert("station_run", {
            "job_id": job_id,
            "station_idx": 1,
            "version": 1,
            "status": "rejected",
            "review_comment": HUMAN_REJECTION,
            "steps_json": json.dumps(
                [{"k": "review", "l": HUMAN_REJECTION, "ts": 125.0}],
                ensure_ascii=False,
            ),
        })
        return {
            "tool": tool_id,
            "avatar": avatar_id,
            "video": video_id,
            "publish": publish_id,
            "meeting": meeting_id,
            "job": job_id,
        }

    async def _get_json(self, user_id: int, path: str):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://paihuo.test",
            cookies={"cc_sess": auth.make_session(user_id)},
        ) as client:
            response = await client.get(path)
        self.assertEqual(200, response.status_code, response.text)
        return response

    def _assert_failure_is_public(self, response):
        self.assertNotIn(SECRET_SENTINEL, response.text)
        self.assertNotIn("raw-upstream-token", response.text)
        self.assertNotIn("/srv/private.py", response.text)
        self.assertIn(providers.PUBLIC_TASK_FAILURE, response.text)

    async def _assert_task_product_http_boundaries(
        self,
        user_id: int,
        tenant_id: int,
    ):
        records = self.records[tenant_id]
        paths = (
            "/api/tools/jobs",
            f"/api/task-center/tool/{records['tool']}",
            "/api/avatar/jobs",
            f"/api/task-center/avatar/{records['avatar']}",
            "/api/text-video",
            f"/api/task-center/video/{records['video']}",
            "/api/matrix/tasks",
            f"/api/task-center/publish/{records['publish']}",
        )
        for path in paths:
            with self.subTest(user_id=user_id, path=path):
                response = await self._get_json(user_id, path)
                self._assert_failure_is_public(response)

    async def test_member_http_views_hide_legacy_tool_avatar_and_video_failures(self):
        await self._assert_task_product_http_boundaries(
            self.member_id,
            tenant_id=2,
        )

    async def test_boss_http_views_hide_legacy_tool_avatar_and_video_failures(self):
        await self._assert_task_product_http_boundaries(
            self.boss_id,
            tenant_id=1,
        )

    async def test_member_and_boss_meeting_http_views_hide_error_messages(self):
        for user_id, tenant_id in (
            (self.member_id, 2),
            (self.boss_id, 1),
        ):
            mid = self.records[tenant_id]["meeting"]
            for path in ("/api/meetings", f"/api/meetings/{mid}"):
                with self.subTest(user_id=user_id, path=path):
                    response = await self._get_json(user_id, path)
                    self._assert_failure_is_public(response)
                    if path.endswith(f"/{mid}"):
                        self.assertIn(
                            "公开说明：会议已停止。",
                            response.text,
                        )

    async def test_failed_station_diagnostics_are_hidden_but_human_rejection_remains(self):
        for user_id, tenant_id in (
            (self.member_id, 2),
            (self.boss_id, 1),
        ):
            jid = self.records[tenant_id]["job"]
            response = await self._get_json(user_id, f"/api/jobs/{jid}")
            self._assert_failure_is_public(response)
            payload = response.json()
            self.assertEqual(
                HUMAN_REJECTION,
                payload["runs"]["1"]["review_comment"],
            )

    async def test_member_and_boss_sse_error_steps_never_emit_raw_diagnostics(self):
        for user_id, tenant_id in (
            (self.member_id, 2),
            (self.boss_id, 1),
        ):
            auth.set_current(auth.get_user(user_id))
            response = await main.events()
            iterator = response.body_iterator
            try:
                hello = await asyncio.wait_for(anext(iterator), timeout=1)
                self.assertIn('"type":"hello"', hello)
                engine.broadcast({
                    "type": "task_step",
                    "tenant_id": tenant_id,
                    "task_id": 999,
                    "idx": 0,
                    "n": 1,
                    "step": {
                        "k": "error",
                        "l": f"raw SSE failure {SECRET_SENTINEL}",
                        "ts": 126.0,
                    },
                })
                chunk = await asyncio.wait_for(anext(iterator), timeout=1)
                self.assertNotIn(SECRET_SENTINEL, chunk)
                event = json.loads(chunk.removeprefix("data: ").strip())
                self.assertEqual("error", event["step"]["k"])
                self.assertTrue(event["step"]["l"])
            finally:
                await iterator.aclose()
                auth.set_current(None)

    async def test_provider_exception_is_normalized_before_storage_and_http_json(self):
        job_id = db.insert("tool_job", {
            "tenant_id": 2,
            "kind": "bench",
            "params_json": "{}",
            "status": "running",
            "billing_status": "charged",
            "billing_points": 1,
        })
        with patch.object(
            main,
            "_run_tool",
            AsyncMock(side_effect=llm.LLMError(SECRET_SENTINEL)),
        ), patch.object(main.log, "exception"):
            await main._tool_worker(job_id)

        row = db.one("SELECT status,error FROM tool_job WHERE id=?", (job_id,))
        self.assertEqual("failed", row["status"])
        self.assertNotIn(SECRET_SENTINEL, row["error"])
        # 失败文案现按异常类型分类,但仍必须是与异常内容无关的固定安全文案:
        # 换一个哨兵,产出的文案必须一字不差。
        self.assertEqual(
            providers.public_failure_message(llm.LLMError("另一个哨兵")),
            row["error"],
        )
        response = await self._get_json(self.member_id, "/api/tools/jobs")
        self.assertNotIn(SECRET_SENTINEL, response.text)


if __name__ == "__main__":
    unittest.main()
