"""Provider diagnostics must not cross exception, HTTP, or journal boundaries."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app import (
    db,
    expertmatch,
    feishu,
    mailer,
    main,
    matrixpub,
    meeting,
    taskrunner,
    wechat,
)


SECRET_SENTINEL = (
    "UPSTREAM_SECRET_SENTINEL_"
    "52d2657f474548c991e7d9e7ca9d1441"
)


class _FeishuResponse:
    status_code = 200

    def json(self):
        return {
            "code": 999,
            "msg": f"{SECRET_SENTINEL} Authorization: Bearer raw-token",
        }


class _FeishuClient:
    async def post(self, *_args, **_kwargs):
        return _FeishuResponse()


class FeishuFailureBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_response_message_is_not_reflected_in_exception(self):
        with patch.object(
            feishu,
            "conf",
            return_value=("cli-test-app", "cli-test-secret"),
        ):
            with self.assertRaises(feishu.FeishuProviderError) as caught:
                await feishu._token(_FeishuClient())

        self.assertNotIn(SECRET_SENTINEL, str(caught.exception))

    async def test_provider_exception_is_not_reflected_by_http_boundary(self):
        with (
            patch.object(main, "_need_module"),
            patch.object(
                feishu,
                "sync",
                new=AsyncMock(
                    side_effect=feishu.FeishuProviderError(SECRET_SENTINEL)
                ),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await main.feishu_sync({"kind": "knowledge"})

        self.assertEqual(502, caught.exception.status_code)
        self.assertNotIn(SECRET_SENTINEL, str(caught.exception.detail))

    async def test_safe_local_validation_message_remains_actionable(self):
        expected = "先保存有效的飞书多维表格链接"
        with (
            patch.object(main, "_need_module"),
            patch.object(
                feishu,
                "sync",
                new=AsyncMock(side_effect=ValueError(expected)),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await main.feishu_sync({"kind": "knowledge"})

        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual(expected, caught.exception.detail)


class ProviderLogBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expert_router_logs_only_failure_class(self):
        employee = {
            "idx": 201,
            "name": "经营顾问",
            "person": "",
            "dept_name": "窗帘产业部",
            "dept_key": "curtain",
            "group": "经营组",
            "desc": "经营诊断",
            "emoji": "📊",
        }
        with (
            patch.object(
                expertmatch,
                "_visible_specialists",
                return_value=[employee],
            ),
            patch.object(
                expertmatch.providers,
                "call_text_json",
                new=AsyncMock(side_effect=RuntimeError(SECRET_SENTINEL)),
            ),
            self.assertLogs("expertmatch", level="WARNING") as captured,
        ):
            result = await expertmatch.match_experts("诊断经营问题", tid=2)

        output = "\n".join(captured.output)
        self.assertEqual([], result)
        self.assertNotIn(SECRET_SENTINEL, output)
        self.assertIn("RuntimeError", output)

    async def test_preflight_logs_only_failure_class(self):
        employee = {
            "idx": 201,
            "name": "经营顾问",
            "dept_key": "curtain",
            "group": "经营组",
            "desc": "经营诊断",
        }
        with (
            patch.object(expertmatch.departments, "get", return_value=employee),
            patch.object(expertmatch, "_dept_peers", return_value=[]),
            patch.object(
                expertmatch.providers,
                "call_text_json",
                new=AsyncMock(side_effect=RuntimeError(SECRET_SENTINEL)),
            ),
            self.assertLogs("expertmatch", level="WARNING") as captured,
        ):
            result = await expertmatch.preflight_fit(201, "诊断经营问题")

        output = "\n".join(captured.output)
        self.assertTrue(result["fit"])
        self.assertNotIn(SECRET_SENTINEL, output)
        self.assertIn("RuntimeError", output)

    async def test_task_postprocessing_never_logs_provider_diagnostics(self):
        long_markdown = "# 标题\n" + ("交付内容。" * 1000)
        with (
            patch.object(
                taskrunner.providers,
                "call_text",
                new=AsyncMock(side_effect=RuntimeError(SECRET_SENTINEL)),
            ),
            self.assertLogs("taskrunner", level="DEBUG") as captured,
        ):
            result, cost = await taskrunner._enforce_length(
                1,
                long_markdown,
                {"length": "lite"},
                lambda *_args: None,
            )

        output = "\n".join(captured.output)
        self.assertEqual(long_markdown, result)
        self.assertEqual(0.0, cost)
        self.assertNotIn(SECRET_SENTINEL, output)
        self.assertIn("RuntimeError", output)

    async def test_task_summary_never_logs_provider_diagnostics(self):
        with (
            patch.object(
                taskrunner.providers,
                "call_text_json",
                new=AsyncMock(side_effect=RuntimeError(SECRET_SENTINEL)),
            ),
            self.assertLogs("taskrunner", level="DEBUG") as captured,
        ):
            await taskrunner._gen_summary(
                1,
                "# 已交付正文",
                1,
                2,
                lambda *_args: None,
            )

        output = "\n".join(captured.output)
        self.assertNotIn(SECRET_SENTINEL, output)
        self.assertIn("RuntimeError", output)

    def test_broadcast_failure_never_logs_callback_diagnostics(self):
        def broken_broadcast(_payload):
            raise RuntimeError(SECRET_SENTINEL)

        with self.assertLogs("taskrunner", level="DEBUG") as captured:
            taskrunner._broadcast_safely(broken_broadcast, {"type": "update"})

        output = "\n".join(captured.output)
        self.assertNotIn(SECRET_SENTINEL, output)
        self.assertIn("RuntimeError", output)


class MeetingLogBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "meeting-log-boundary.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})

    def tearDown(self):
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def test_ranking_fallback_never_logs_provider_diagnostics(self):
        members = {
            0: {
                "idx": 0,
                "name": "趋势官",
                "duty": "趋势研究",
                "md": "内部岗位资料",
                "color": "#111111",
                "emoji": "📡",
            },
            1: {
                "idx": 1,
                "name": "情报官",
                "duty": "证据核验",
                "md": "内部技能资料",
                "color": "#222222",
                "emoji": "🔎",
            },
        }
        meeting_id = db.insert("meeting", {
            "tenant_id": 1,
            "question": "是否发布新品",
            "emp_idxs_json": "[0,1]",
            "status": "queued",
            "auto_execute": 0,
            "billing_status": "included",
            "billing_points": 0,
        })

        async def fake_model(idx, _prompt, **kwargs):
            token = kwargs.get("token", "")
            if ":proposal:" in token:
                return {"data": {
                    "title": f"方案{idx}",
                    "position": "先做最小验证",
                    "plan": ["制作样品", "核对需求"],
                    "deliverable": "样品和验证记录",
                    "success_metric": "形成可核验结论",
                    "risk": "证据不足",
                }}
            if token.endswith(":rank"):
                raise RuntimeError(SECRET_SENTINEL)
            if ":validate:" in token:
                return {"data": {
                    "verdict": "PASS",
                    "summary": "验证路径成立",
                    "evidence": ["已有可核验材料"],
                    "condition": "先完成小样",
                }}
            if token.endswith(":decision"):
                return {"data": {
                    "decision": "GO",
                    "summary": "可以进入最小执行",
                    "next_action": "完成首版样品",
                    "actions": [{
                        "idx": 0,
                        "task": "完成首版样品",
                        "acceptance": "样品可评审",
                    }],
                }}
            raise AssertionError(token)

        with (
            patch.object(meeting, "emp_brief", side_effect=members.get),
            patch.object(meeting.registry, "company_block", return_value=""),
            patch.object(
                meeting.providers,
                "call_text_json",
                side_effect=fake_model,
            ),
            self.assertLogs("meeting", level="WARNING") as captured,
        ):
            await meeting.run(meeting_id, lambda _event: None)

        output = "\n".join(captured.output)
        self.assertEqual(
            "done",
            db.one("SELECT status FROM meeting WHERE id=?", (meeting_id,))[
                "status"
            ],
        )
        self.assertNotIn(SECRET_SENTINEL, output)
        self.assertIn("RuntimeError", output)


class CredentialMinimizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wechat_connectivity_result_contains_no_token_fragment(self):
        token_value = f"token-prefix-{SECRET_SENTINEL}"
        with patch.object(
            wechat,
            "token",
            new=AsyncMock(return_value=token_value),
        ):
            result = await wechat.test_conn(2)

        self.assertEqual(
            {"ok": True, "note": "连接成功,可以一键发草稿箱了"},
            result,
        )
        self.assertNotIn(token_value[-6:], str(result))


class JournalPrivacyTests(unittest.TestCase):
    def test_mail_success_log_does_not_emit_recipient_address(self):
        recipient = f"{SECRET_SENTINEL}@example.com"
        settings = {
            "smtp_user": "sender@example.com",
            "smtp_authcode": "configured-secret",
            "lead_email": recipient,
        }
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        with (
            patch.object(
                mailer.db,
                "get_setting",
                side_effect=settings.get,
            ),
            patch.object(mailer.smtplib, "SMTP_SSL", return_value=smtp),
            self.assertLogs("mailer", level="INFO") as captured,
        ):
            mailer._send("通知", "正文")

        output = "\n".join(captured.output)
        self.assertNotIn(recipient, output)
        self.assertNotIn(SECRET_SENTINEL, output)
        smtp.sendmail.assert_called_once()

    def test_publish_progress_journal_does_not_emit_user_facing_message(self):
        # _log 现在把「读-改-写」整事务投给 db 线程池(防止事件循环被写锁冻结)。
        # 这里把提交的事务同步执行在假连接上:约束不变——消息只进库,不进日志。
        executed = {}

        class _FakeCursor:
            @staticmethod
            def fetchone():
                return {"log": ""}

        class _FakeConn:
            def execute(self, sql, args=()):
                if sql.strip().upper().startswith("UPDATE"):
                    executed["sql"], executed["args"] = sql, args
                return _FakeCursor()

        class _FakeAtomic:
            def __enter__(self):
                return _FakeConn()

            def __exit__(self, *exc):
                return False

        with (
            patch.object(matrixpub.db, "atomic", _FakeAtomic),
            patch.object(matrixpub.db, "submit_write",
                         side_effect=lambda fn, *a, **k: fn(*a, **k)),
            self.assertLogs("matrixpub", level="INFO") as captured,
        ):
            matrixpub._log(17, SECRET_SENTINEL)

        output = "\n".join(captured.output)
        self.assertNotIn(SECRET_SENTINEL, output)
        self.assertIn(SECRET_SENTINEL, executed["args"][0])


class RuntimeLoggingStaticTests(unittest.TestCase):
    def test_runtime_never_emits_raw_tracebacks_to_journal(self):
        app_root = Path(__file__).resolve().parents[1] / "app"
        offenders = []
        for source_path in sorted(app_root.rglob("*.py")):
            text = source_path.read_text(encoding="utf-8")
            if (
                re.search(
                    r"(?:\blog|logging\.getLogger\([^)]*\))\.exception\(",
                    text,
                )
                or "exc_info=True" in text
            ):
                offenders.append(source_path.relative_to(app_root).as_posix())
        self.assertEqual(
            [],
            offenders,
            "runtime logging must use stable context plus error_type only",
        )


if __name__ == "__main__":
    unittest.main()
