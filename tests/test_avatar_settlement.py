import asyncio
import inspect
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from fastapi import HTTPException

from app import auth, avatar, billing, db


class AvatarSettlementCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        self.old_public_dir = avatar.PUBLIC_DIR
        self.old_out_dir = avatar.OUT_DIR
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "avatar-settlement.db")
        avatar.PUBLIC_DIR = os.path.join(self.tmp.name, "public")
        avatar.OUT_DIR = os.path.join(self.tmp.name, "out")
        os.makedirs(avatar.PUBLIC_DIR, exist_ok=True)
        os.makedirs(avatar.OUT_DIR, exist_ok=True)
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 30})
        db.insert("tenants", {"id": 3, "name": "其他企业", "balance": 30})
        self._as_tenant(2)

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        avatar.PUBLIC_DIR = self.old_public_dir
        avatar.OUT_DIR = self.old_out_dir
        self.tmp.cleanup()

    def _as_tenant(self, tenant_id):
        auth.set_current({
            "id": tenant_id * 10,
            "tenant_id": tenant_id,
            "username": f"owner-{tenant_id}",
            "role": "owner",
            "modules": ["avatar"],
        })

    def _asset(self, tenant_id, kind, suffix):
        self._as_tenant(tenant_id)
        name = f"{tenant_id:032x}{suffix}"
        path = os.path.join(avatar.PUBLIC_DIR, name)
        with open(path, "wb") as handle:
            handle.write(b"test")
        avatar.remember_asset(name, kind)
        return name

    @staticmethod
    def _params(**changes):
        value = {
            "photo_name": "photo.jpg",
            "script": "这是一段用于验证数字人任务计费和取消行为的口播稿。",
            "voice_id": "presenter_female",
            "voice_label": "主持女声",
            "own_audio_name": None,
            "engine": "kling",
            "duration": 30,
            "prompt": "",
        }
        value.update(changes)
        return value

    def test_creation_persists_pending_job_before_atomic_charge(self):
        from app import main

        original = billing.charge_if_claimed
        observed = {}

        def inspect_then_charge(action, tid, claim, **kwargs):
            row = db.one(
                "SELECT id,status,billing_status,billing_points "
                "FROM avatar_job ORDER BY id DESC LIMIT 1"
            )
            observed.update(row or {})
            return original(action, tid, claim, **kwargs)

        with mock.patch.object(
                billing, "charge_if_claimed", side_effect=inspect_then_charge):
            job_id = main._create_charged_avatar_job(self._params(), 2)

        self.assertEqual("pending_charge", observed["status"])
        self.assertEqual("pending", observed["billing_status"])
        self.assertEqual(12, observed["billing_points"])
        self.assertEqual(
            {"status": "queued", "billing_status": "charged",
             "billing_points": 12},
            db.one(
                "SELECT status,billing_status,billing_points "
                "FROM avatar_job WHERE id=?",
                (job_id,),
            ),
        )
        self.assertEqual(18, billing.balance(2))
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_log "
                "WHERE tenant_id=2 AND delta=-12"
            )["n"],
        )

    def test_concurrent_heygen_photo_registration_keeps_both_owners(self):
        """平台级照片登记必须是单事务 RMW，并发租户不能互相覆盖。"""
        original_registry = avatar._hg_registry
        old_code_read_barrier = threading.Barrier(2)

        def synchronized_registry_read():
            # 若实现退回“先读后写”，强制两条 worker 都读到同一旧快照，
            # 让丢更新稳定复现；事务实现不会调用这个旧 helper。
            old_code_read_barrier.wait(timeout=2)
            return original_registry()

        async def scenario():
            with mock.patch.object(
                avatar,
                "_hg_registry",
                side_effect=synchronized_registry_read,
            ):
                await asyncio.gather(
                    db.arun(avatar.hg_register_photo, "photo-A", 2, 101),
                    db.arun(avatar.hg_register_photo, "photo-B", 3, 202),
                )

        asyncio.run(scenario())
        entries = avatar._hg_registry()
        self.assertEqual(
            {"photo-A", "photo-B"},
            {entry["id"] for entry in entries},
        )
        self.assertEqual(2, len(entries))

    def test_cleanup_registry_transaction_preserves_concurrent_upload(self):
        """清理旧照片与登记新照片并发时，新归属不能被旧快照覆盖。"""
        avatar.hg_register_photo("photo-old", 2, 101)
        start = threading.Barrier(2)

        def remove_old():
            start.wait(timeout=2)
            avatar._hg_remove_registered_photos({"photo-old"})

        def register_new():
            start.wait(timeout=2)
            avatar.hg_register_photo("photo-new", 3, 202)

        async def scenario():
            await asyncio.gather(
                db.arun(remove_old),
                db.arun(register_new),
            )

        asyncio.run(scenario())
        entries = avatar._hg_registry()
        self.assertEqual(["photo-new"], [entry["id"] for entry in entries])

        cleanup_source = inspect.getsource(avatar.hg_cleanup_photos)
        self.assertIn("_hg_remove_registered_photos", cleanup_source)
        self.assertNotIn("aset_setting", cleanup_source)

    def test_insufficient_balance_does_not_leave_job_or_partial_debit(self):
        from app import main

        db.update("tenants", 2, {"balance": 5})
        with self.assertRaises(HTTPException) as raised:
            main._create_charged_avatar_job(self._params(), 2)
        self.assertEqual(402, raised.exception.status_code)
        self.assertEqual(5, billing.balance(2))
        self.assertEqual(
            0, db.one("SELECT COUNT(*) AS n FROM avatar_job")["n"])
        self.assertEqual(
            0, db.one("SELECT COUNT(*) AS n FROM billing_log")["n"])

    def test_failure_refunds_stored_charge_exactly_once_after_price_change(self):
        from app import main

        job_id = main._create_charged_avatar_job(self._params(), 2)
        saved = billing.prices()
        changed = json.loads(json.dumps(saved))
        changed["avatar_video"]["points"] = 99
        db.set_setting("prices", json.dumps(changed, ensure_ascii=False))

        self.assertTrue(avatar.settle_failure(job_id, "供应商失败"))
        self.assertFalse(avatar.settle_failure(job_id, "重复失败"))
        self.assertEqual(30, billing.balance(2))
        self.assertEqual(
            {"status": "failed", "billing_status": "refunded",
             "billing_points": 12},
            db.one(
                "SELECT status,billing_status,billing_points "
                "FROM avatar_job WHERE id=?",
                (job_id,),
            ),
        )
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_log "
                "WHERE tenant_id=2 AND delta=12"
            )["n"],
        )

    def test_cancelled_queued_job_never_reenters_running(self):
        from app import main

        job_id = main._create_charged_avatar_job(self._params(), 2)
        self.assertEqual({"ok": True}, main.avatar_job_cancel(job_id))
        asyncio.run(avatar.run_job(job_id, lambda _event: None))

        self.assertEqual(
            {"status": "cancelled", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM avatar_job WHERE id=?",
                (job_id,),
            ),
        )
        self.assertEqual(30, billing.balance(2))

    def test_basic_engine_does_not_read_unrelated_heygen_secret(self):
        """Basic/Kling paths must not depend on decrypting HeyGen credentials."""
        from app import main, runninghub

        photo = self._asset(2, "photo", ".jpg")
        voice = self._asset(2, "voice", ".mp3")
        self._as_tenant(2)
        job_id = main._create_charged_avatar_job(
            self._params(
                engine="basic",
                photo_name=photo,
                own_audio_name=voice,
            ),
            2,
        )

        with mock.patch.object(
            avatar.secureconfig,
            "get_secret",
            side_effect=AssertionError("Basic 不应读取 HeyGen 密钥"),
        ) as read_secret, mock.patch.object(
            runninghub,
            "synth",
            new=mock.AsyncMock(side_effect=avatar.Cancelled()),
        ), mock.patch.object(
            avatar,
            "_cap_audio",
            side_effect=lambda path, _seconds: path,
        ):
            asyncio.run(avatar.run_job(job_id, lambda _event: None))

        read_secret.assert_not_called()

    def test_cancel_while_worker_runs_cannot_finish_or_refund_twice(self):
        from app import main

        photo = self._asset(2, "photo", ".jpg")
        voice = self._asset(2, "voice", ".mp3")
        self._as_tenant(2)
        job_id = main._create_charged_avatar_job(
            self._params(photo_name=photo, own_audio_name=voice), 2)

        async def exercise():
            started = asyncio.Event()
            release = asyncio.Event()

            async def blocked_submit(*_args, **_kwargs):
                started.set()
                await release.wait()
                raise avatar.Cancelled()

            with mock.patch.object(
                    avatar, "_cap_audio", side_effect=lambda path, _seconds: path), \
                    mock.patch.object(
                        avatar, "kling_avatar", side_effect=blocked_submit):
                worker = asyncio.create_task(
                    avatar.run_job(job_id, lambda _event: None))
                await asyncio.wait_for(started.wait(), timeout=2)
                self.assertEqual({"ok": True}, main.avatar_job_cancel(job_id))
                release.set()
                await asyncio.wait_for(worker, timeout=2)

        asyncio.run(exercise())
        self.assertEqual(
            {"status": "cancelled", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM avatar_job WHERE id=?",
                (job_id,),
            ),
        )
        self.assertEqual(30, billing.balance(2))
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_log "
                "WHERE tenant_id=2 AND delta=12"
            )["n"],
        )

    def test_restart_refunds_running_job_once(self):
        from app import main

        job_id = main._create_charged_avatar_job(self._params(), 2)
        db.update("avatar_job", job_id, {"status": "running"})
        avatar.resume_pending(lambda _event: None)
        avatar.resume_pending(lambda _event: None)

        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM avatar_job WHERE id=?",
                (job_id,),
            ),
        )
        self.assertEqual(30, billing.balance(2))
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_log "
                "WHERE tenant_id=2 AND delta=12"
            )["n"],
        )

    def test_cross_tenant_asset_is_rejected_before_any_charge(self):
        from app import main

        foreign = self._asset(3, "photo", ".jpg")
        self._as_tenant(2)
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(main.avatar_job_create({
                "photo_name": foreign,
                "script": "跨租户素材不应被使用。",
                "duration": 30,
            }))
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual(30, billing.balance(2))
        self.assertEqual(
            0, db.one("SELECT COUNT(*) AS n FROM billing_log")["n"])
        self.assertEqual(
            0, db.one("SELECT COUNT(*) AS n FROM avatar_job")["n"])

    def test_duration_and_script_limits_reject_cost_bypass_before_charge(self):
        from app import main

        photo = self._asset(2, "photo", ".jpg")
        self._as_tenant(2)
        for duration in (-1, 1, 31, 61, 999999):
            with self.subTest(duration=duration), self.assertRaises(
                HTTPException
            ) as raised:
                asyncio.run(main.avatar_job_create({
                    "photo_name": photo,
                    "script": "正常口播",
                    "duration": duration,
                }))
            self.assertEqual(400, raised.exception.status_code)

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(main.avatar_job_create({
                "photo_name": photo,
                "script": "长" * 241,
                "duration": 30,
            }))
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual(30, billing.balance(2))
        self.assertEqual(
            0, db.one("SELECT COUNT(*) AS n FROM billing_log")["n"])
        self.assertEqual(
            0, db.one("SELECT COUNT(*) AS n FROM avatar_job")["n"])

    def test_legacy_charged_job_without_recorded_points_still_settles_once(self):
        job_id = db.insert("avatar_job", {
            "tenant_id": 2,
            "params_json": json.dumps(self._params(), ensure_ascii=False),
            "status": "running",
        })
        db.update("tenants", 2, {"balance": 18})

        self.assertTrue(avatar.settle_failure(
            job_id, "旧任务中断", terminal_status="failed"))
        self.assertFalse(avatar.settle_failure(
            job_id, "重复恢复", terminal_status="failed"))
        self.assertEqual(30, billing.balance(2))
        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"},
            db.one(
                "SELECT status,billing_status FROM avatar_job WHERE id=?",
                (job_id,),
            ),
        )


if __name__ == "__main__":
    unittest.main()
