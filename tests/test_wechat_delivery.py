import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import auth, billing, db, main


class WeChatDeliveryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "wechat-delivery.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 10})
        db.insert("job", {
            "id": 7,
            "tenant_id": 2,
            "brief_json": "{}",
            "status": "done",
            "billing_status": "succeeded",
        })
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner",
            "role": "owner",
            "modules": ["content"],
        })
        self.payload = {
            "title": "测试文章",
            "body": "这是已经定稿的文章正文。",
            "html": "<p>这是已经定稿的文章正文。</p>",
            "images": [],
            "cover_rel": None,
            "digest": "文章摘要",
        }
        self.report = {
            "verdict": "pass",
            "score": 95,
            "issues": [],
            "summary": "通过",
            "label": "审查通过",
        }

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def common(self):
        return (
            patch.object(main, "_job_or_404", return_value={"id": 7}),
            patch.object(main, "_mp_payload", return_value=self.payload),
            patch.object(
                main.wechat,
                "get_conf",
                return_value={"appid": "app", "secret": "secret"},
            ),
            patch.object(main.wechat, "make_cover_png", return_value=b"cover"),
            patch.object(
                main.wechat, "upload_thumb", new=AsyncMock(return_value="thumb")
            ),
        )

    def test_unknown_upstream_error_message_is_not_reflected(self):
        from app import wechat

        secret = "INTERNAL-MANUAL-SECRET echoed by WeChat"
        upstream = wechat.WeChatError(99999, secret)
        self.assertNotIn(secret, str(upstream))
        self.assertIn("暂时不可用", str(upstream))

        local = wechat.WeChatError(
            0,
            "还没配置公众号 AppID/AppSecret",
            public=True,
        )
        self.assertIn("还没配置公众号", str(local))

    async def test_ordinary_preflight_exception_refunds_once(self):
        secret = "WECHAT_PREFLIGHT_SECRET_SENTINEL_9b3a46695e52"
        add_draft = AsyncMock(return_value="media")
        patches = self.common()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(
                    main.censor,
                    "check",
                    new=AsyncMock(side_effect=OSError(secret)),
                ), patch.object(main.wechat, "add_draft", add_draft):
            with self.assertRaises(HTTPException) as failed:
                await main.job_wechat_draft(7, {"theme": "default"})

        self.assertEqual(500, failed.exception.status_code)
        self.assertEqual(10, billing.balance(2))
        add_draft.assert_not_awaited()
        delivery = db.one("SELECT * FROM wechat_draft_delivery")
        self.assertEqual(
            ("failed", "refunded"),
            (delivery["status"], delivery["billing_status"]),
        )
        self.assertEqual(
            "refunded",
            db.one(
                "SELECT status FROM billing_operation WHERE op_key=?",
                (delivery["op_key"],),
            )["status"],
        )
        self.assertNotIn(secret, delivery["error"] or "")
        self.assertNotIn(
            secret,
            "\n".join(
                str(row)
                for row in db.q(
                    "SELECT reason FROM billing_log WHERE tenant_id=2 ORDER BY id"
                )
            ),
        )
        self.assertNotIn(
            secret,
            "\n".join(
                str(row)
                for row in db.q(
                    "SELECT note,error FROM billing_operation "
                    "WHERE tenant_id=2 ORDER BY created_at"
                )
            ),
        )

    async def test_external_success_is_replayed_without_duplicate_after_log_failure(self):
        db.execute(
            "CREATE TRIGGER reject_publish BEFORE INSERT ON publish_log "
            "BEGIN SELECT RAISE(ABORT,'disk full'); END"
        )
        add_draft = AsyncMock(return_value="media-1")
        censor = AsyncMock(return_value=self.report)
        patches = self.common()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(main.censor, "check", censor), \
                patch.object(main.wechat, "add_draft", add_draft):
            with self.assertRaises(HTTPException) as pending:
                await main.job_wechat_draft(7, {"theme": "default"})
            self.assertEqual(503, pending.exception.status_code)
            self.assertEqual(9, billing.balance(2))
            delivery = db.one("SELECT * FROM wechat_draft_delivery")
            self.assertEqual(
                ("submitted", "charged", "media-1"),
                (
                    delivery["status"],
                    delivery["billing_status"],
                    delivery["media_id"],
                ),
            )

            db.execute("DROP TRIGGER reject_publish")
            result = await main.job_wechat_draft(7, {"theme": "default"})

        self.assertTrue(result["ok"])
        self.assertEqual("media-1", result["media_id"])
        self.assertEqual(1, add_draft.await_count)
        self.assertEqual(1, censor.await_count)
        self.assertEqual(9, billing.balance(2))
        delivery = db.one("SELECT * FROM wechat_draft_delivery")
        self.assertEqual(
            ("done", "succeeded"),
            (delivery["status"], delivery["billing_status"]),
        )
        self.assertEqual(
            1,
            db.one("SELECT COUNT(*) n FROM publish_log")["n"],
        )

    async def test_ambiguous_wechat_response_is_reconciled_not_resent(self):
        add_draft = AsyncMock(side_effect=OSError("connection reset"))
        find = AsyncMock(side_effect=["", "media-reconciled"])
        patches = self.common()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(
                    main.censor, "check", new=AsyncMock(return_value=self.report)
                ), patch.object(main.wechat, "add_draft", add_draft), \
                patch.object(main.wechat, "find_draft_by_marker", find):
            with self.assertRaises(HTTPException) as uncertain:
                await main.job_wechat_draft(7, {"theme": "default"})
            self.assertEqual(503, uncertain.exception.status_code)
            self.assertEqual(9, billing.balance(2))

            result = await main.job_wechat_draft(7, {"theme": "default"})

        self.assertTrue(result["ok"])
        self.assertEqual("media-reconciled", result["media_id"])
        self.assertEqual(1, add_draft.await_count)
        self.assertEqual(2, find.await_count)
        self.assertEqual(9, billing.balance(2))
        self.assertEqual(
            "done",
            db.one("SELECT status FROM wechat_draft_delivery")["status"],
        )

    async def test_job_delete_is_blocked_while_delivery_needs_reconciliation(self):
        job_id = db.insert("job", {
            "tenant_id": 2,
            "brief_json": "{}",
            "status": "done",
            "billing_status": "succeeded",
        })
        db.insert("wechat_draft_delivery", {
            "tenant_id": 2,
            "job_id": job_id,
            "request_hash": "a" * 64,
            "request_key": "a" * 20,
            "title": "测试文章",
            "status": "submitting",
            "billing_status": "charged",
            "billing_points": 1,
            "op_key": "wechat:uncertain",
        })

        with self.assertRaises(HTTPException) as blocked:
            main.delete_job(job_id)

        self.assertEqual(409, blocked.exception.status_code)
        self.assertIsNotNone(db.one("SELECT id FROM job WHERE id=?", (job_id,)))
        self.assertIsNotNone(
            db.one(
                "SELECT id FROM wechat_draft_delivery WHERE job_id=?", (job_id,)
            )
        )

    async def test_pending_delivery_blocks_delete_and_missing_job_prevents_charge(self):
        job_id = db.insert("job", {
            "tenant_id": 2,
            "brief_json": "{}",
            "status": "done",
            "billing_status": "succeeded",
        })
        pending_id = db.insert("wechat_draft_delivery", {
            "tenant_id": 2,
            "job_id": job_id,
            "request_hash": "b" * 64,
            "request_key": "b" * 20,
            "title": "待扣费",
            "status": "pending_charge",
            "billing_status": "pending",
        })
        with self.assertRaises(HTTPException) as blocked:
            main.delete_job(job_id)
        self.assertEqual(409, blocked.exception.status_code)
        db.q("DELETE FROM wechat_draft_delivery WHERE id=?", (pending_id,))

        db.execute(
            "CREATE TRIGGER delete_job_after_delivery "
            "AFTER INSERT ON wechat_draft_delivery "
            "BEGIN DELETE FROM job WHERE id=NEW.job_id; END"
        )
        with self.assertRaisesRegex(RuntimeError, "计费状态冲突"):
            main._create_wechat_delivery(
                2, job_id, "c" * 64, "并发删除测试"
            )
        self.assertEqual(10, billing.balance(2))
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) n FROM billing_operation")["n"],
        )
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) n FROM wechat_draft_delivery "
                "WHERE request_hash=?",
                ("c" * 64,),
            )["n"],
        )

    async def test_same_job_cannot_start_second_payload_while_first_is_uncertain(self):
        db.insert("wechat_draft_delivery", {
            "tenant_id": 2,
            "job_id": 7,
            "request_hash": "old-" + "a" * 60,
            "request_key": "old-request",
            "title": "旧正文",
            "status": "submitting",
            "billing_status": "charged",
            "billing_points": 1,
            "op_key": "wechat:old",
        })
        row, created = main._create_wechat_delivery(
            2, 7, "new-" + "b" * 60, "修改后的正文"
        )
        self.assertFalse(created)
        self.assertEqual("submitting", row["status"])
        self.assertEqual(10, billing.balance(2))
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM wechat_draft_delivery WHERE job_id=7"
            )["n"],
        )

    async def test_empty_media_id_stays_uncertain_and_never_claims_success(self):
        patches = self.common()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(
                    main.censor,
                    "check",
                    new=AsyncMock(return_value=self.report),
                ), patch.object(
                    main.wechat,
                    "add_draft",
                    new=AsyncMock(return_value=""),
                ), patch.object(
                    main.wechat,
                    "find_draft_by_marker",
                    new=AsyncMock(return_value=""),
                ):
            with self.assertRaises(HTTPException) as uncertain:
                await main.job_wechat_draft(7, {"theme": "default"})

        self.assertEqual(503, uncertain.exception.status_code)
        delivery = db.one("SELECT * FROM wechat_draft_delivery")
        self.assertEqual(
            ("submitting", "charged", None),
            (
                delivery["status"],
                delivery["billing_status"],
                delivery["media_id"],
            ),
        )
        self.assertEqual(9, billing.balance(2))
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM publish_log")["n"])

    async def test_final_delete_transaction_rechecks_delivery_created_midway(self):
        job_id = db.insert("job", {
            "tenant_id": 2,
            "brief_json": "{}",
            "status": "done",
            "billing_status": "succeeded",
        })

        def create_pending(_prefix):
            db.insert("wechat_draft_delivery", {
                "tenant_id": 2,
                "job_id": job_id,
                "request_hash": "late-" + "d" * 59,
                "request_key": "late-request",
                "title": "刚开始投递",
                "status": "pending_charge",
                "billing_status": "pending",
            })
            return 0

        with patch.object(main.llm, "kill", side_effect=create_pending):
            with self.assertRaises(HTTPException) as blocked:
                main.delete_job(job_id)

        self.assertEqual(409, blocked.exception.status_code)
        self.assertIsNotNone(db.one("SELECT id FROM job WHERE id=?", (job_id,)))
        self.assertIsNotNone(
            db.one(
                "SELECT id FROM wechat_draft_delivery WHERE job_id=?", (job_id,)
            )
        )

    async def test_deep_review_provider_failure_refunds_without_free_log_or_send(self):
        add_draft = AsyncMock(return_value="media")
        patches = self.common()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(
                    main.censor.providers,
                    "call_text_json",
                    new=AsyncMock(side_effect=RuntimeError("provider down")),
                ), patch.object(main.wechat, "add_draft", add_draft):
            with self.assertRaises(HTTPException) as failed:
                await main.job_wechat_draft(7, {"theme": "default"})

        self.assertEqual(500, failed.exception.status_code)
        self.assertEqual(10, billing.balance(2))
        add_draft.assert_not_awaited()
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM censor_log")["n"])
        delivery = db.one("SELECT * FROM wechat_draft_delivery")
        self.assertEqual(
            ("failed", "refunded"),
            (delivery["status"], delivery["billing_status"]),
        )

    async def test_blocked_and_delivered_reviews_log_once_with_settlement(self):
        blocked = dict(self.report)
        blocked.update({
            "verdict": "block",
            "score": 20,
            "issues": [{"severity": "高", "type": "硬红线"}],
            "summary": "存在高危内容",
        })
        patches = self.common()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(
                    main.censor, "check", new=AsyncMock(return_value=blocked)
                ):
            result = await main.job_wechat_draft(7, {"theme": "default"})

        self.assertTrue(result["blocked"])
        self.assertEqual(9, billing.balance(2))
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM censor_log")["n"])
        self.assertEqual(
            ("blocked", "succeeded"),
            tuple(
                db.one(
                    "SELECT status,billing_status FROM wechat_draft_delivery"
                )[key]
                for key in ("status", "billing_status")
            ),
        )

        # 新正文可再发；成功送达时也应只产生该投递自己的单条终审记录。
        self.payload = {
            **self.payload,
            "body": "整改后的正文。",
            "html": "<p>整改后的正文。</p>",
        }
        delivered_patches = self.common()
        with delivered_patches[0], delivered_patches[1], delivered_patches[2], \
                delivered_patches[3], delivered_patches[4], \
                patch.object(
                    main.censor, "check", new=AsyncMock(return_value=self.report)
                ), patch.object(
                    main.wechat, "add_draft", new=AsyncMock(return_value="media-2")
                ):
            delivered = await main.job_wechat_draft(7, {"theme": "default"})

        self.assertTrue(delivered["ok"])
        self.assertEqual(8, billing.balance(2))
        self.assertEqual(2, db.one("SELECT COUNT(*) n FROM censor_log")["n"])
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM publish_log")["n"])
        statuses = db.q(
            "SELECT status,billing_status FROM wechat_draft_delivery ORDER BY id"
        )
        self.assertEqual(
            [("blocked", "succeeded"), ("done", "succeeded")],
            [(row["status"], row["billing_status"]) for row in statuses],
        )

    def _uncertain_delivery(self, request_hash: str = None):
        delivery, created = main._create_wechat_delivery(
            2, 7, request_hash or ("u" * 64), "测试文章"
        )
        self.assertTrue(created)
        db.execute(
            "UPDATE wechat_draft_delivery SET status='submitting',"
            "report_json=?,updated_at=? WHERE id=?",
            (
                '{"verdict":"pass","score":95,"issues":[],"summary":"通过"}',
                time.time() - 360,
                delivery["id"],
            ),
        )
        return db.one(
            "SELECT * FROM wechat_draft_delivery WHERE id=?", (delivery["id"],)
        )

    async def test_manual_reconcile_finds_draft_and_never_resends(self):
        delivery = self._uncertain_delivery()
        find = AsyncMock(return_value="media-found")
        with patch.object(main.wechat, "find_draft_by_marker", find):
            result = await main.reconcile_wechat_delivery(delivery["id"])

        self.assertTrue(result["ok"])
        self.assertEqual("media-found", result["media_id"])
        self.assertEqual(9, billing.balance(2))
        self.assertEqual(
            ("done", "succeeded"),
            tuple(
                db.one(
                    "SELECT status,billing_status FROM wechat_draft_delivery "
                    "WHERE id=?",
                    (delivery["id"],),
                )[key]
                for key in ("status", "billing_status")
            ),
        )
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM publish_log")["n"])
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM censor_log")["n"])
        find.assert_awaited_once()

    async def test_manual_confirm_absent_refunds_once_but_found_draft_never_refunds(self):
        delivery = self._uncertain_delivery("a" * 64)
        with patch.object(
            main.wechat,
            "find_draft_by_marker",
            new=AsyncMock(return_value=""),
        ):
            result = await main.confirm_wechat_delivery_not_delivered(
                delivery["id"],
                {
                    "confirmed_no_draft": True,
                    "title_confirmation": "测试文章",
                },
            )
        self.assertTrue(result["refunded"])
        self.assertEqual(10, billing.balance(2))
        self.assertEqual(
            ("failed", "refunded"),
            tuple(
                db.one(
                    "SELECT status,billing_status FROM wechat_draft_delivery "
                    "WHERE id=?",
                    (delivery["id"],),
                )[key]
                for key in ("status", "billing_status")
            ),
        )
        with self.assertRaises(HTTPException) as duplicate:
            await main.confirm_wechat_delivery_not_delivered(
                delivery["id"],
                {
                    "confirmed_no_draft": True,
                    "title_confirmation": "测试文章",
                },
            )
        self.assertEqual(409, duplicate.exception.status_code)
        self.assertEqual(10, billing.balance(2))

        found = self._uncertain_delivery("b" * 64)
        with patch.object(
            main.wechat,
            "find_draft_by_marker",
            new=AsyncMock(return_value="media-present"),
        ):
            result = await main.confirm_wechat_delivery_not_delivered(
                found["id"],
                {
                    "confirmed_no_draft": True,
                    "title_confirmation": "测试文章",
                },
            )
        self.assertTrue(result["ok"])
        self.assertEqual(9, billing.balance(2))
        self.assertEqual(
            ("done", "succeeded"),
            tuple(
                db.one(
                    "SELECT status,billing_status FROM wechat_draft_delivery "
                    "WHERE id=?",
                    (found["id"],),
                )[key]
                for key in ("status", "billing_status")
            ),
        )

    async def test_changed_content_hash_still_reconciles_old_active_first(self):
        old = self._uncertain_delivery("old-" + "x" * 60)
        current_hash = main._wechat_delivery_hash(7, "default", self.payload)
        self.assertNotEqual(old["request_hash"], current_hash)
        censor = AsyncMock(return_value=self.report)
        add_draft = AsyncMock(return_value="new-media")
        patches = self.common()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(
                    main.wechat,
                    "find_draft_by_marker",
                    new=AsyncMock(return_value="old-media"),
                ), patch.object(main.censor, "check", censor), \
                patch.object(main.wechat, "add_draft", add_draft):
            result = await main.job_wechat_draft(7, {"theme": "default"})

        self.assertTrue(result["ok"])
        self.assertEqual("old-media", result["media_id"])
        censor.assert_not_awaited()
        add_draft.assert_not_awaited()
        self.assertEqual(9, billing.balance(2))
        self.assertEqual(
            1,
            db.one("SELECT COUNT(*) n FROM wechat_draft_delivery")["n"],
        )


if __name__ == "__main__":
    unittest.main()
