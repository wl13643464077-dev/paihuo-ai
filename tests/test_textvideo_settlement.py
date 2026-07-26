import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException

from app import auth, billing, db, textvideo


class TextVideoSettlementCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        self.old_asset_dir = textvideo.ASSET_DIR
        self.old_clip_root = textvideo.CLIP_ROOT
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "textvideo.db")
        textvideo.ASSET_DIR = os.path.join(self.tmp.name, "assets")
        textvideo.CLIP_ROOT = os.path.join(textvideo.ASSET_DIR, "tvclips")
        os.makedirs(textvideo.ASSET_DIR, exist_ok=True)
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 30})
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner-2",
            "role": "owner",
            "modules": ["content"],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        textvideo.ASSET_DIR = self.old_asset_dir
        textvideo.CLIP_ROOT = self.old_clip_root
        self.tmp.cleanup()

    @staticmethod
    def _params():
        return {
            "title": "专项验收",
            "script": "这是一段足够长的测试口播稿，用来验证文字转视频任务的计费一致性。",
            "voice_id": "presenter_female",
            "image_query": "",
            "bgm": "none",
        }

    def _create(self):
        from app import main

        return main._create_charged_tv_job(
            self._params(), tenant_id=2, note="专项验收")

    def _write_video(self, tvid, folder="tv"):
        directory = os.path.join(textvideo.ASSET_DIR, folder)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"tv_{tvid}.mp4")
        with open(path, "wb") as handle:
            handle.write(b"video")
        return path, f"/files/{folder}/tv_{tvid}.mp4"

    def test_schema_has_explicit_billing_snapshot_columns(self):
        columns = {
            row["name"]: row for row in db.q("PRAGMA table_info(tv_job)")
        }
        self.assertIn("billing_status", columns)
        self.assertIn("billing_points", columns)

    def test_creation_persists_pending_job_before_atomic_charge(self):
        original = billing.charge_if_claimed
        observed = {}

        def inspect_then_charge(action, tid, claim, **kwargs):
            row = db.one(
                "SELECT status,billing_status,billing_points "
                "FROM tv_job ORDER BY id DESC LIMIT 1"
            )
            observed.update(row or {})
            return original(action, tid, claim, **kwargs)

        with mock.patch.object(
                billing, "charge_if_claimed", side_effect=inspect_then_charge):
            tvid = self._create()

        self.assertEqual({
            "status": "pending_charge",
            "billing_status": "pending",
            "billing_points": 3,
        }, observed)
        self.assertEqual({
            "status": "queued",
            "billing_status": "charged",
            "billing_points": 3,
        }, db.one(
            "SELECT status,billing_status,billing_points "
            "FROM tv_job WHERE id=?", (tvid,)))
        self.assertEqual(27, billing.balance(2))
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_log "
            "WHERE tenant_id=2 AND delta=-3")["n"])

    def test_insufficient_points_leaves_no_job_or_partial_debit(self):
        db.update("tenants", 2, {"balance": 2})
        with self.assertRaises(HTTPException) as raised:
            self._create()
        self.assertEqual(402, raised.exception.status_code)
        self.assertEqual(2, billing.balance(2))
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM tv_job")["n"])
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_log")["n"])

    def test_billing_log_failure_rolls_back_balance_and_pending_job(self):
        db.q(
            "CREATE TRIGGER fail_tv_charge BEFORE INSERT ON billing_log "
            "WHEN NEW.delta < 0 BEGIN "
            "SELECT RAISE(ABORT, 'billing log unavailable'); END"
        )
        with self.assertRaises(Exception):
            self._create()
        self.assertEqual(30, billing.balance(2))
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM tv_job")["n"])
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_log")["n"])

    def test_two_workers_claim_queued_job_only_once(self):
        tvid = self._create()
        calls = 0

        async def fake_build(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            _path, url = self._write_video(tvid)
            return url

        async def exercise():
            with mock.patch.object(textvideo, "build", side_effect=fake_build), \
                    mock.patch("app.notify.push"):
                await asyncio.gather(
                    textvideo.run_job(tvid, lambda _event: None),
                    textvideo.run_job(tvid, lambda _event: None),
                )

        asyncio.run(exercise())
        self.assertEqual(1, calls)
        self.assertEqual({
            "status": "done",
            "billing_status": "succeeded",
        }, db.one(
            "SELECT status,billing_status FROM tv_job WHERE id=?", (tvid,)))
        self.assertEqual(27, billing.balance(2))

    def test_notification_and_broadcast_failure_cannot_refund_delivery(self):
        tvid = self._create()

        async def fake_build(*_args, **_kwargs):
            _path, url = self._write_video(tvid)
            return url

        def broken(_payload):
            raise RuntimeError("旁路通知不可用")

        async def exercise():
            with mock.patch.object(textvideo, "build", side_effect=fake_build), \
                    mock.patch("app.notify.push", side_effect=RuntimeError("通知故障")):
                await textvideo.run_job(tvid, broken)

        asyncio.run(exercise())
        self.assertEqual({
            "status": "done",
            "billing_status": "succeeded",
        }, db.one(
            "SELECT status,billing_status FROM tv_job WHERE id=?", (tvid,)))
        self.assertEqual(27, billing.balance(2))
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_log "
            "WHERE tenant_id=2 AND delta>0")["n"])

    def test_failure_refunds_stored_points_exactly_once_after_price_change(self):
        tvid = self._create()
        prices = json.loads(json.dumps(billing.prices()))
        prices["text_video"]["points"] = 99
        db.set_setting("prices", json.dumps(prices, ensure_ascii=False))

        async def broken_build(*_args, **_kwargs):
            raise RuntimeError("合成器故障")

        async def exercise():
            with mock.patch.object(
                    textvideo, "build", side_effect=broken_build):
                await asyncio.gather(
                    textvideo.run_job(tvid, lambda _event: None),
                    textvideo.run_job(tvid, lambda _event: None),
                )

        asyncio.run(exercise())
        self.assertEqual({
            "status": "failed",
            "billing_status": "refunded",
            "billing_points": 3,
        }, db.one(
            "SELECT status,billing_status,billing_points "
            "FROM tv_job WHERE id=?", (tvid,)))
        self.assertEqual(30, billing.balance(2))
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_log "
            "WHERE tenant_id=2 AND delta=3")["n"])

    def test_restart_refunds_running_job_once_and_removes_orphan_file(self):
        tvid = self._create()
        path, _url = self._write_video(tvid)
        db.update("tv_job", tvid, {"status": "running"})

        with mock.patch("asyncio.create_task"):
            textvideo.resume_pending(lambda _event: None)
            textvideo.resume_pending(lambda _event: None)

        self.assertEqual({
            "status": "failed",
            "billing_status": "refunded",
        }, db.one(
            "SELECT status,billing_status FROM tv_job WHERE id=?", (tvid,)))
        self.assertEqual(30, billing.balance(2))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_log "
            "WHERE tenant_id=2 AND delta=3")["n"])

    def test_delete_during_build_refunds_once_and_result_cannot_resurrect(self):
        from app import main

        tvid = self._create()
        started = asyncio.Event()
        release = asyncio.Event()
        output = {}

        async def blocked_build(*_args, **_kwargs):
            started.set()
            await release.wait()
            path, url = self._write_video(tvid)
            output["path"] = path
            return url

        async def exercise():
            with mock.patch.object(
                    textvideo, "build", side_effect=blocked_build), \
                    mock.patch("app.notify.push"):
                worker = asyncio.create_task(
                    textvideo.run_job(tvid, lambda _event: None))
                await asyncio.wait_for(started.wait(), timeout=2)
                self.assertEqual(
                    {"ok": True}, main.text_video_delete(tvid))
                release.set()
                await asyncio.wait_for(worker, timeout=2)

        asyncio.run(exercise())
        self.assertIsNone(db.one(
            "SELECT id FROM tv_job WHERE id=?", (tvid,)))
        self.assertEqual(30, billing.balance(2))
        self.assertFalse(os.path.exists(output["path"]))
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_log "
            "WHERE tenant_id=2 AND delta=3")["n"])

    def test_delete_before_worker_start_refunds_then_stale_worker_is_noop(self):
        from app import main

        tvid = self._create()
        self.assertEqual({"ok": True}, main.text_video_delete(tvid))
        asyncio.run(textvideo.run_job(tvid, lambda _event: None))

        self.assertIsNone(db.one(
            "SELECT id FROM tv_job WHERE id=?", (tvid,)))
        self.assertEqual(30, billing.balance(2))
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_log "
            "WHERE tenant_id=2 AND delta=3")["n"])

    def test_delete_delivered_video_removes_file_without_refund(self):
        from app import main

        tvid = self._create()

        async def fake_build(*_args, **_kwargs):
            _path, url = self._write_video(tvid)
            return url

        async def exercise():
            with mock.patch.object(textvideo, "build", side_effect=fake_build), \
                    mock.patch("app.notify.push"):
                await textvideo.run_job(tvid, lambda _event: None)

        asyncio.run(exercise())
        path = os.path.join(textvideo.ASSET_DIR, "tv", f"tv_{tvid}.mp4")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual({"ok": True}, main.text_video_delete(tvid))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(27, billing.balance(2))
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_log "
            "WHERE tenant_id=2 AND delta>0")["n"])

    def test_pending_charge_orphan_is_removed_without_refund(self):
        tvid = db.insert("tv_job", {
            "tenant_id": 2,
            "params_json": json.dumps(self._params(), ensure_ascii=False),
            "status": "pending_charge",
            "billing_status": "pending",
            "billing_points": 3,
        })
        path, _url = self._write_video(tvid)

        with mock.patch("asyncio.create_task"):
            textvideo.resume_pending(lambda _event: None)

        self.assertIsNone(db.one(
            "SELECT id FROM tv_job WHERE id=?", (tvid,)))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(30, billing.balance(2))
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_log")["n"])


if __name__ == "__main__":
    unittest.main()
