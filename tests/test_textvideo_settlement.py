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

    def test_job_linked_charge_and_refund_keep_explicit_attribution(self):
        from app import main

        job_id = db.insert("job", {
            "tenant_id": 2,
            "brief_json": '{"direction":"关联成片"}',
            "status": "done",
            "billing_status": "succeeded",
        })
        tvid = main._create_charged_tv_job(
            self._params(),
            tenant_id=2,
            job_id=job_id,
            note="关联成片",
        )
        self.assertTrue(textvideo.settle_failure(tvid, "渲染失败"))
        rows = db.q(
            "SELECT job_id,delta FROM billing_log "
            "WHERE tenant_id=2 ORDER BY id"
        )
        self.assertEqual(
            [(job_id, -3), (job_id, 3)],
            [(row["job_id"], row["delta"]) for row in rows],
        )

    def test_job_linked_delivery_notification_keeps_explicit_attribution(self):
        from app import main

        job_id = db.insert("job", {
            "tenant_id": 2,
            "brief_json": '{"direction":"关联通知成片"}',
            "status": "done",
            "billing_status": "succeeded",
        })
        tvid = main._create_charged_tv_job(
            self._params(),
            tenant_id=2,
            job_id=job_id,
            note="关联通知成片",
        )

        async def fake_build(*_args, **_kwargs):
            _path, url = self._write_video(tvid)
            return url

        async def exercise():
            with mock.patch.object(
                textvideo, "build", side_effect=fake_build
            ), mock.patch("app.notify.push") as pushed:
                await textvideo.run_job(tvid, lambda _event: None)
                return pushed

        pushed = asyncio.run(exercise())
        pushed.assert_called_once()
        self.assertEqual(job_id, pushed.call_args.args[2]["job_id"])

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

    def test_final_step_is_committed_before_done_state(self):
        """最后步骤不能还在后台队列里，任务状态就先变成 done。"""
        tvid = self._create()
        observations = []

        async def fake_build(*args, **_kwargs):
            progress = args[7]
            progress("渲染器最后一步")
            _path, url = self._write_video(tvid)
            return url

        original_arun = db.arun

        async def observe_delivery(fn, *args, **kwargs):
            if getattr(fn, "__name__", "") == "_deliver_job":
                before = db.one(
                    "SELECT status,steps_json FROM tv_job WHERE id=?", (tvid,)
                )
                observations.append(before)
            return await original_arun(fn, *args, **kwargs)

        async def exercise():
            with mock.patch.object(textvideo, "build", side_effect=fake_build), \
                    mock.patch("app.notify.push"), \
                    mock.patch.object(db, "arun", side_effect=observe_delivery):
                await textvideo.run_job(tvid, lambda _event: None)

        asyncio.run(exercise())
        self.assertEqual(1, len(observations))
        self.assertEqual("running", observations[0]["status"])
        self.assertEqual(
            "渲染器最后一步",
            json.loads(observations[0]["steps_json"])[-1]["msg"],
        )
        row = db.one(
            "SELECT status,steps_json FROM tv_job WHERE id=?", (tvid,)
        )
        self.assertEqual("done", row["status"])
        self.assertEqual(
            "交付完成",
            json.loads(row["steps_json"])[-1]["msg"],
        )

    def test_cancellation_is_seen_by_progress_without_sync_db_on_loop(self):
        """长合成期间取消后，下一次进度回调应中止工作而不是继续产出。"""
        from app import main

        tvid = self._create()
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled_at_progress = asyncio.Event()

        async def cancellable_build(*args, **_kwargs):
            progress = args[7]
            started.set()
            await release.wait()
            try:
                progress("取消后的下一步")
            except textvideo._Cancelled:
                cancelled_at_progress.set()
                raise
            self.fail("取消后的进度回调没有终止合成")

        async def exercise():
            with mock.patch.object(
                    textvideo, "build", side_effect=cancellable_build), \
                    mock.patch("app.notify.push"):
                worker = asyncio.create_task(
                    textvideo.run_job(tvid, lambda _event: None)
                )
                await asyncio.wait_for(started.wait(), timeout=2)
                self.assertEqual({"ok": True}, main.text_video_delete(tvid))
                # 给异步状态监测一次轮询机会；事件循环不能用同步 SELECT。
                await asyncio.sleep(0.65)
                release.set()
                await asyncio.wait_for(worker, timeout=2)

        asyncio.run(exercise())
        self.assertTrue(cancelled_at_progress.is_set())
        self.assertIsNone(db.one(
            "SELECT id FROM tv_job WHERE id=?", (tvid,)
        ))

    def test_build_cancellation_does_not_enter_concat_fallback(self):
        """叠化失败提示发现取消后，不能吞掉取消再跑 900 秒回退。"""
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            # 两个分段成功，叠化失败；若取消被吞还会出现第 4 次 concat。
            return mock.Mock(returncode=0 if len(calls) <= 2 else 1)

        def progress(message):
            if "回退简单拼接" in message:
                raise textvideo._Cancelled()

        async def exercise():
            with mock.patch.object(
                    textvideo, "split_sentences", return_value=["甲", "乙"]), \
                    mock.patch.object(
                        textvideo.avatar,
                        "tts",
                        new=mock.AsyncMock(return_value="https://media.test/a.mp3"),
                    ), \
                    mock.patch.object(
                        textvideo.netfetch,
                        "download_public_media",
                        new=mock.AsyncMock(),
                    ), \
                    mock.patch.object(textvideo, "_probe_dur", return_value=1.0), \
                    mock.patch.object(textvideo, "_seg_cmd", return_value=["segment"]), \
                    mock.patch.object(
                        textvideo, "_append_ending", new=mock.AsyncMock()
                    ), \
                    mock.patch.object(
                        textvideo,
                        "_xfade_cmd",
                        return_value=(
                            ["xfade"], "graph", "video", "unused", 2.0
                        ),
                    ), \
                    mock.patch.object(textvideo, "_render_output_ok", return_value=True), \
                    mock.patch.object(
                        textvideo.subprocess, "run", side_effect=fake_run
                    ):
                with self.assertRaises(textvideo._Cancelled):
                    await textvideo.build(
                        1,
                        2,
                        "标题",
                        "两句口播",
                        [],
                        "",
                        "tv",
                        progress,
                        bgm="none",
                    )

        asyncio.run(exercise())
        self.assertEqual(3, len(calls), "取消后仍启动了 concat 回退")

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
