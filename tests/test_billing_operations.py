import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import auth, billing, db, meeting, scheduler


class BillingOperationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "billing.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 20})
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner",
            "role": "owner",
            "modules": ["content"],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_failure_refunds_actual_charge_exactly_once(self):
        key = billing.start_operation("learn", note="测试", op_key="learn:1")
        self.assertEqual("learn:1", key)
        self.assertEqual(17, billing.balance(2))

        self.assertTrue(billing.fail_operation(key, "模型不可用"))
        self.assertFalse(billing.fail_operation(key, "重复失败"))
        self.assertEqual(20, billing.balance(2))
        logs = db.q("SELECT delta FROM billing_log WHERE tenant_id=2 ORDER BY id")
        self.assertEqual([-3, 3], [row["delta"] for row in logs])

    def test_success_is_not_refunded_and_duplicate_key_is_rejected(self):
        key = billing.start_operation("link_extract", op_key="link:1")
        self.assertTrue(billing.complete_operation(key))
        self.assertTrue(billing.complete_operation(key))
        self.assertFalse(billing.fail_operation(key))
        self.assertEqual(19, billing.balance(2))
        with self.assertRaises(ValueError):
            billing.start_operation("link_extract", op_key="link:1")

    def test_startup_recovery_refunds_charged_and_drops_uncharged_pending(self):
        billing.start_operation("bench_watch", op_key="weekly:1")
        db.insert("billing_operation", {
            "op_key": "orphan-pending",
            "tenant_id": 2,
            "action": "learn",
            "points": 3,
            "status": "pending",
        })

        self.assertEqual(1, billing.recover_interrupted_operations())
        self.assertEqual(20, billing.balance(2))
        self.assertIsNone(db.one(
            "SELECT op_key FROM billing_operation WHERE op_key='orphan-pending'"))
        self.assertEqual(
            "refunded",
            db.one("SELECT status FROM billing_operation WHERE op_key='weekly:1'")["status"],
        )

    def test_legacy_successful_subscription_is_settled_without_balance_change(self):
        now = 123456.0
        with db.atomic() as connection:
            connection.execute(
                "UPDATE tenants SET balance=170,plan='体验版·月付',"
                "plan_expires=?,updated_at=? WHERE id=2",
                (now + 31 * 86400, now),
            )
            connection.execute(
                "INSERT INTO billing_log"
                "(tenant_id,delta,balance,reason,created_at,updated_at) "
                "VALUES(2,150,170,'旧版套餐入账',?,?)",
                (now, now),
            )
            connection.execute(
                "INSERT INTO billing_operation"
                "(op_key,tenant_id,action,units,points,note,status,"
                "created_at,updated_at) "
                "VALUES('legacy-subscribe',2,'subscribe',1,150,'旧版回执',"
                "'charged',?,?)",
                (now, now),
            )
        before_balance = billing.balance(2)
        before_logs = db.one(
            "SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2"
        )["n"]

        self.assertEqual(1, billing.settle_legacy_subscriptions())
        self.assertEqual(0, billing.recover_interrupted_operations())

        self.assertEqual(before_balance, billing.balance(2))
        self.assertEqual(
            before_logs,
            db.one(
                "SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2"
            )["n"],
        )
        self.assertEqual(
            "succeeded",
            db.one(
                "SELECT status FROM billing_operation "
                "WHERE op_key='legacy-subscribe'"
            )["status"],
        )


class ScheduledBillingOperationCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "scheduled-billing.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 20})
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner",
            "role": "owner",
            "modules": [],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def test_periodic_failure_refunds_and_success_settles(self):
        async def fail():
            raise RuntimeError("upstream failed")

        with self.assertRaises(RuntimeError):
            await scheduler._run_billed("hot_pick", 2, "每日自动", fail)
        self.assertEqual(20, billing.balance(2))
        self.assertEqual(
            "refunded",
            db.one("SELECT status FROM billing_operation ORDER BY created_at DESC LIMIT 1")[
                "status"
            ],
        )

        async def succeed():
            return "ok"

        self.assertEqual(
            "ok",
            await scheduler._run_billed("bench_watch", 2, "每周自动", succeed),
        )
        self.assertEqual(17, billing.balance(2))
        self.assertEqual(
            "succeeded",
            db.one("SELECT status FROM billing_operation ORDER BY created_at DESC LIMIT 1")[
                "status"
            ],
        )

    async def test_failed_meeting_intervention_uses_idempotent_operation_refund(self):
        mid = db.insert("meeting", {
            "tenant_id": 2,
            "question": "是否上线",
            "emp_idxs_json": "[0,1]",
            "status": "running",
            "phase": "completed",
            "consensus_md": "已有结论",
        })
        op_key = billing.start_operation(
            "expert_task", tid=2, n=2, note="会议追问")
        self.assertEqual(18, billing.balance(2))

        with patch(
            "app.meeting.providers.call_text",
            new=AsyncMock(side_effect=RuntimeError("provider down")),
        ), patch(
            "app.meeting.providers.call_text_json",
            new=AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            await meeting.ask(mid, "请补证据", lambda _event: None, op_key)

        self.assertEqual(20, billing.balance(2))
        self.assertEqual(
            "refunded",
            db.one("SELECT status FROM billing_operation WHERE op_key=?", (op_key,))[
                "status"
            ],
        )
        self.assertFalse(billing.fail_operation(op_key, "重复结算"))

    async def test_failed_link_extraction_refunds_the_billed_operation(self):
        from app import main

        with patch(
            "app.linkgrab._guard_url",
            new=AsyncMock(return_value=None),
        ), patch(
            "app.main._avatar_script_from_link_work",
            new=AsyncMock(side_effect=RuntimeError("gateway unavailable")),
        ):
            with self.assertRaises(RuntimeError):
                await main.avatar_script_from_link({
                    "url": "https://example.com/post",
                    "duration": 30,
                })

        self.assertEqual(20, billing.balance(2))
        row = db.one(
            "SELECT status FROM billing_operation WHERE action='link_extract' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        self.assertEqual("refunded", row["status"])

    async def test_link_extraction_rejects_oversized_input_before_guard_or_billing(self):
        from app import main

        cases = (
            {
                "url": "https://example.com/post " + ("说明" * 2000),
                "duration": 30,
            },
            {
                "url": "https://example.com/post",
                "style": "风" * 201,
                "duration": 30,
            },
            {
                "url": ["https://example.com/post"],
                "duration": 30,
            },
        )
        for body in cases:
            with self.subTest(body_type=type(body["url"]).__name__), patch(
                "app.linkgrab._guard_url",
                new=AsyncMock(),
            ) as guard, patch(
                "app.main._avatar_script_from_link_work",
                new=AsyncMock(),
            ) as worker:
                with self.assertRaises(HTTPException) as caught:
                    await main.avatar_script_from_link(body)
                self.assertEqual(400, caught.exception.status_code)
                guard.assert_not_awaited()
                worker.assert_not_awaited()

        self.assertEqual(20, billing.balance(2))
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_operation "
                "WHERE action='link_extract'"
            )["n"],
        )

    async def test_product_shot_disk_failure_refunds_operation(self):
        from app import main

        class Upload:
            filename = "product.png"
            content_type = "image/png"

            def __init__(self):
                self.sent = False

            async def read(self, _size):
                if self.sent:
                    return b""
                self.sent = True
                return b"source-image"

        with patch.object(
            main.growth, "product_shot", new=AsyncMock(return_value=b"png-result")
        ), patch.object(main, "ROOT", self.tmp.name), \
                patch("builtins.open", side_effect=OSError("disk full")):
            with self.assertRaises(HTTPException) as failed:
                await main.product_shot_api(Upload(), "桌面")

        self.assertEqual(500, failed.exception.status_code)
        self.assertEqual(20, billing.balance(2))
        operation = db.one(
            "SELECT status FROM billing_operation WHERE action='product_shot' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        self.assertEqual("refunded", operation["status"])

    async def test_censor_check_success_settles_with_log_and_strict_mode(self):
        from app import main

        report = {
            "verdict": "pass",
            "score": 98,
            "issues": [],
            "summary": "通过",
            "label": "可以发布",
            "cost_usd": 0.1,
            "tokens": 10,
        }
        check = AsyncMock(return_value=report)
        with patch.object(main.censor, "check", check):
            result = await main.censor_check_api({
                "title": "新品说明",
                "body": "这是准备发布的正文。",
                "platform": "公众号",
            })

        self.assertEqual(report, result)
        self.assertEqual(19, billing.balance(2))
        operation = db.one(
            "SELECT status FROM billing_operation WHERE action='censor_check'"
        )
        self.assertEqual("succeeded", operation["status"])
        log = db.one("SELECT * FROM censor_log WHERE tenant_id=2")
        self.assertEqual(("pre", "pass"), (log["kind"], log["verdict"]))
        self.assertTrue(check.await_args.kwargs["strict"])
        self.assertFalse(check.await_args.kwargs["save"])

    async def test_censor_provider_failure_and_cancellation_refund_without_log(self):
        from app import main

        with patch.object(
            main.censor.providers,
            "call_text_json",
            new=AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            # 深审失败退点后降级为免费规则扫描结果,而不是把整个响应变成报错。
            report = await main.censor_check_api({
                "title": "待审内容",
                "body": "这是一段需要深度审查的正文。",
                "platform": "公众号",
            })
        self.assertTrue(report["degraded"])
        self.assertIn("退回", report["summary"])
        self.assertEqual(20, billing.balance(2))
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM censor_log")["n"])
        self.assertEqual(
            "refunded",
            db.one(
                "SELECT status FROM billing_operation "
                "WHERE action='censor_check' ORDER BY created_at DESC LIMIT 1"
            )["status"],
        )

        with patch.object(
            main.censor,
            "check",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main.censor_check_api({
                    "title": "被取消",
                    "body": "请求被客户端中断。",
                })
        self.assertEqual(20, billing.balance(2))
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM censor_log")["n"])
        statuses = db.q(
            "SELECT status FROM billing_operation "
            "WHERE action='censor_check' ORDER BY rowid"
        )
        self.assertEqual(["refunded", "refunded"], [row["status"] for row in statuses])

    async def test_censor_retro_success_and_failure_are_durable(self):
        from app import main

        result = {
            "report": "复盘报告",
            "grade": "良好",
            "signals": [],
            "actions": ["继续优化"],
            "cost_usd": 0.1,
            "tokens": 8,
        }
        retro = AsyncMock(return_value=result)
        with patch.object(main.censor, "retro", retro):
            got = await main.censor_retro_api({
                "title": "文章",
                "platform": "公众号",
                "data_text": "阅读100，点赞10",
            })
        self.assertEqual(result, got)
        self.assertEqual(19, billing.balance(2))
        self.assertEqual(
            ("retro", "良好"),
            tuple(
                db.one("SELECT kind,verdict FROM censor_log WHERE tenant_id=2")[
                    key
                ]
                for key in ("kind", "verdict")
            ),
        )
        self.assertFalse(retro.await_args.kwargs["save"])

        with patch.object(
            main.censor,
            "retro",
            new=AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            with self.assertRaises(HTTPException) as failed:
                await main.censor_retro_api({
                    "title": "失败文章",
                    "platform": "公众号",
                    "data_text": "阅读数据",
                })
        self.assertEqual(503, failed.exception.status_code)
        self.assertEqual(19, billing.balance(2))
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM censor_log")["n"])
        operations = db.q(
            "SELECT status FROM billing_operation "
            "WHERE action='censor_retro' ORDER BY rowid"
        )
        self.assertEqual(["succeeded", "refunded"], [row["status"] for row in operations])

    async def test_voice_clone_result_and_billing_commit_together(self):
        from app import main

        sample = os.path.join(self.tmp.name, "sample.mp3")
        with open(sample, "wb") as handle:
            handle.write(b"voice-sample")
        voice = {
            "id": "voice-1",
            "label": "🧬 老板",
            "cloned": True,
            "created_at": 123.0,
        }
        work_samples = []

        async def clone_private_copy(path, _label, *, save):
            work_samples.append(path)
            self.assertNotEqual(os.path.realpath(sample), os.path.realpath(path))
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as handle:
                self.assertEqual(b"voice-sample", handle.read())
            self.assertFalse(save)
            return voice

        clone = AsyncMock(side_effect=clone_private_copy)
        with patch.object(main, "_avatar_asset_name", return_value="sample.mp3"), \
                patch.object(main.avatar, "asset_path", return_value=sample), \
                patch.object(main.avatar, "clone_voice", clone):
            got = await main.avatar_clone({
                "audio_name": "sample.mp3",
                "label": "老板",
            })

        self.assertEqual(voice, got)
        self.assertEqual(11, billing.balance(2))
        self.assertEqual(
            "succeeded",
            db.one(
                "SELECT status FROM billing_operation WHERE action='voice_clone'"
            )["status"],
        )
        saved = json.loads(db.get_setting("cloned_voices:2"))
        self.assertEqual(["voice-1"], [item["id"] for item in saved])
        self.assertFalse(clone.await_args.kwargs["save"])
        self.assertEqual(1, len(work_samples))
        self.assertFalse(os.path.exists(work_samples[0]))
        self.assertTrue(os.path.isfile(sample))

    async def test_concurrent_voice_list_updates_do_not_lose_entries(self):
        """声音列表的读改写必须在一个原子事务中完成。"""
        from app import avatar

        voices = [
            {
                "id": f"voice-{index}",
                "label": f"声音{index}",
                "cloned": True,
                "created_at": float(index),
            }
            for index in range(8)
        ]
        await asyncio.gather(*(
            db.arun(avatar._prepend_cloned_voice, 2, voice)
            for voice in voices
        ))
        saved = json.loads(db.get_setting("cloned_voices:2"))
        self.assertEqual(
            {voice["id"] for voice in voices},
            {voice["id"] for voice in saved},
        )

    async def test_voice_clone_failure_and_cancellation_refund_without_visibility(self):
        from app import main

        sample = os.path.join(self.tmp.name, "sample.mp3")
        with open(sample, "wb") as handle:
            handle.write(b"voice-sample")
        work_samples = []

        async def fail_from_private_copy(path, _label, *, save):
            work_samples.append(path)
            self.assertNotEqual(os.path.realpath(sample), os.path.realpath(path))
            self.assertTrue(os.path.isfile(path))
            raise RuntimeError("clone failed")

        async def cancel_from_private_copy(path, _label, *, save):
            work_samples.append(path)
            self.assertNotEqual(os.path.realpath(sample), os.path.realpath(path))
            self.assertTrue(os.path.isfile(path))
            raise asyncio.CancelledError()

        common = (
            patch.object(main, "_avatar_asset_name", return_value="sample.mp3"),
            patch.object(main.avatar, "asset_path", return_value=sample),
        )
        with common[0], common[1], patch.object(
            main.avatar,
            "clone_voice",
            new=AsyncMock(side_effect=fail_from_private_copy),
        ):
            with self.assertRaises(HTTPException) as failed:
                await main.avatar_clone({"audio_name": "sample.mp3"})
        self.assertEqual(500, failed.exception.status_code)
        self.assertEqual(20, billing.balance(2))
        self.assertIsNone(db.get_setting("cloned_voices:2"))

        with patch.object(
            main, "_avatar_asset_name", return_value="sample.mp3"
        ), patch.object(
            main.avatar, "asset_path", return_value=sample
        ), patch.object(
            main.avatar,
            "clone_voice",
            new=AsyncMock(side_effect=cancel_from_private_copy),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main.avatar_clone({"audio_name": "sample.mp3"})
        self.assertEqual(20, billing.balance(2))
        self.assertIsNone(db.get_setting("cloned_voices:2"))
        self.assertEqual(
            ["refunded", "refunded"],
            [
                row["status"]
                for row in db.q(
                    "SELECT status FROM billing_operation "
                    "WHERE action='voice_clone' ORDER BY rowid"
                )
            ],
        )
        self.assertTrue(work_samples)
        self.assertTrue(all(not os.path.exists(path) for path in work_samples))
        self.assertTrue(os.path.isfile(sample))

    async def test_periodic_hot_result_commits_before_best_effort_notification(self):
        from app import growth, notify

        db.set_setting(
            "hot_daily:2",
            json.dumps({
                "enabled": True,
                "industry": "家居",
                "channels": ["微博热搜"],
            }, ensure_ascii=False),
        )
        data = {
            "date": "2026-07-26",
            "industry": "家居",
            "channels": ["微博热搜"],
            "picks": [{"title": "今日选题"}],
            "scan_note": "摘要",
        }
        runner = AsyncMock(return_value=data)
        with patch.object(growth, "hot_daily_run", runner), patch.object(
            notify, "push", side_effect=RuntimeError("webhook down")
        ):
            got = await scheduler._run_hot_daily(2, "2026-07-26")

        self.assertEqual(data, got)
        self.assertEqual(19, billing.balance(2))
        self.assertEqual(
            "succeeded",
            db.one(
                "SELECT status FROM billing_operation WHERE action='hot_pick'"
            )["status"],
        )
        conf = json.loads(db.get_setting("hot_daily:2"))
        self.assertEqual("2026-07-26", conf["last_ymd"])
        self.assertEqual(
            data,
            json.loads(db.get_setting("hotpick:2:2026-07-26:家居")),
        )
        self.assertFalse(runner.await_args.kwargs["save"])

    async def test_periodic_benchmark_notification_failure_never_refunds_or_duplicates(self):
        from app import growth, notify

        db.set_setting(
            "bench_watch:2",
            json.dumps({
                "enabled": True,
                "targets": [{"name": "对标品牌", "platform": "小红书"}],
            }, ensure_ascii=False),
        )
        report = {
            "summary": "竞争态势",
            "md": "# 周报",
            "items": [],
            "actions": [],
        }
        runner = AsyncMock(return_value=report)
        with patch.object(growth, "bench_report", runner), patch.object(
            notify, "push", side_effect=RuntimeError("webhook down")
        ):
            self.assertEqual(report, await scheduler._run_bench_weekly(2))
            with self.assertRaises(RuntimeError):
                await scheduler._run_bench_weekly(2)

        self.assertEqual(17, billing.balance(2))
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM knowledge "
                "WHERE tenant_id=2 AND source='auto'"
            )["n"],
        )
        self.assertIsInstance(
            json.loads(db.get_setting("bench_watch:2"))["last_run"],
            (int, float),
        )
        self.assertEqual(2, runner.await_count)
        self.assertFalse(runner.await_args.kwargs["save"])
        statuses = db.q(
            "SELECT status FROM billing_operation "
            "WHERE action='bench_watch' ORDER BY rowid"
        )
        self.assertEqual(
            ["succeeded", "refunded"], [row["status"] for row in statuses]
        )

    async def test_periodic_commit_failure_refunds_without_visible_result(self):
        from app import growth, notify

        db.set_setting(
            "bench_watch:2",
            json.dumps({
                "enabled": True,
                "targets": [{"name": "对标品牌", "platform": "小红书"}],
            }, ensure_ascii=False),
        )
        db.execute(
            "CREATE TRIGGER reject_periodic_knowledge "
            "BEFORE INSERT ON knowledge "
            "BEGIN SELECT RAISE(ABORT,'disk full'); END"
        )
        with patch.object(
            growth,
            "bench_report",
            new=AsyncMock(return_value={
                "summary": "竞争态势",
                "md": "# 周报",
                "items": [],
                "actions": [],
            }),
        ), patch.object(notify, "push") as pushed:
            with self.assertRaises(Exception):
                await scheduler._run_bench_weekly(2)

        self.assertEqual(20, billing.balance(2))
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM knowledge")["n"])
        self.assertNotIn(
            "last_run", json.loads(db.get_setting("bench_watch:2"))
        )
        self.assertEqual(
            "refunded",
            db.one(
                "SELECT status FROM billing_operation WHERE action='bench_watch'"
            )["status"],
        )
        pushed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
