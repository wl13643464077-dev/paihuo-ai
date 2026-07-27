"""回收站「彻底删除」(合规硬删)契约测试。"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import assetfiles, auth, db, main


class TrashPurgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "purge.db")
        db.conn()
        db.insert(
            "tenants",
            {"id": 2, "name": "测试企业", "balance": 20, "enabled": 1},
        )
        auth.set_current(
            {
                "id": 20,
                "tenant_id": 2,
                "username": "owner",
                "role": "owner",
                "modules": [],
            }
        )
        # 交付文件根目录指向临时目录,绝不碰仓库真实 data/assets
        self.asset_root = os.path.join(self.tmp.name, "assets")
        os.makedirs(self.asset_root, exist_ok=True)
        self._asset_patch = patch.object(
            assetfiles, "ASSET_ROOT", self.asset_root
        )
        self._asset_patch.start()

    def tearDown(self):
        self._asset_patch.stop()
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _deleted_job(self, **extra):
        return db.insert(
            "job",
            {
                "tenant_id": 2,
                "brief_json": json.dumps({"direction": "已删内容"}),
                "status": "done",
                "billing_status": "succeeded",
                "deleted_at": 100,
                **extra,
            },
        )

    def _deleted_avatar(self, **extra):
        return db.insert(
            "avatar_job",
            {
                "tenant_id": 2,
                "params_json": json.dumps({"script": "已删口播"}),
                "status": "failed",
                "billing_status": "refunded",
                "deleted_at": 100,
                **extra,
            },
        )

    def test_purge_job_removes_row_station_runs_and_files(self):
        job_id = self._deleted_job()
        db.insert(
            "station_run",
            {"job_id": job_id, "station_idx": 1, "status": "done",
             "output_json": "{}"},
        )
        db.insert(
            "station_run",
            {"job_id": job_id, "station_idx": 2, "status": "done",
             "output_json": "{}"},
        )
        # 账目锚点必须在硬删后原样留存
        db.insert(
            "billing_log",
            {"tenant_id": 2, "delta": -3, "balance": 17,
             "reason": f"内容工单#{job_id}"},
        )
        job_dir = os.path.join(self.asset_root, f"job{job_id}")
        os.makedirs(os.path.join(job_dir, "imgs"), exist_ok=True)
        for name in ("final.md", os.path.join("imgs", "cover.png")):
            with open(os.path.join(job_dir, name), "w") as fh:
                fh.write("payload")

        result = main.trash_purge("job", job_id)

        self.assertEqual(
            {"ok": True, "purged": True, "kind": "job", "id": job_id,
             "files_removed": 2, "files_failed": 0},
            result,
        )
        self.assertIsNone(
            db.one("SELECT id FROM job WHERE id=?", (job_id,)))
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) n FROM station_run WHERE job_id=?",
                (job_id,),
            )["n"],
        )
        self.assertFalse(os.path.exists(job_dir))
        self.assertFalse(
            any(
                item["kind"] == "job" and item["id"] == job_id
                for item in main.trash_list()["items"]
            )
        )
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2"
            )["n"],
        )

    def test_purge_avatar_removes_final_clip(self):
        avatar_id = self._deleted_avatar()
        clip_dir = os.path.join(self.asset_root, "avatar")
        os.makedirs(clip_dir, exist_ok=True)
        clip = os.path.join(clip_dir, f"avatar_{avatar_id}.mp4")
        with open(clip, "wb") as fh:
            fh.write(b"mp4")

        result = main.trash_purge("avatar", avatar_id)

        self.assertTrue(result["purged"])
        self.assertEqual(1, result["files_removed"])
        self.assertEqual(0, result["files_failed"])
        self.assertFalse(os.path.exists(clip))
        self.assertIsNone(
            db.one("SELECT id FROM avatar_job WHERE id=?", (avatar_id,)))

    def test_avatar_symlink_is_not_reported_as_destroyed(self):
        avatar_id = self._deleted_avatar()
        clip_dir = os.path.join(self.asset_root, "avatar")
        os.makedirs(clip_dir, exist_ok=True)
        target = os.path.join(self.tmp.name, "sensitive.mp4")
        with open(target, "wb") as fh:
            fh.write(b"mp4")
        clip = os.path.join(clip_dir, f"avatar_{avatar_id}.mp4")
        os.symlink(target, clip)

        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("avatar", avatar_id)

        self.assertEqual(409, ctx.exception.status_code)
        self.assertTrue(os.path.isfile(target))
        self.assertTrue(os.path.islink(clip))
        self.assertIsNotNone(
            db.one("SELECT id FROM avatar_job WHERE id=?", (avatar_id,))
        )

    def test_purge_without_files_reports_zero_and_still_deletes(self):
        knowledge_id = db.insert(
            "knowledge",
            {"tenant_id": 2, "title": "敏感客户名单", "content": "正文",
             "deleted_at": 100},
        )
        result = main.trash_purge("knowledge", knowledge_id)
        self.assertEqual(0, result["files_removed"])
        self.assertEqual(0, result["files_failed"])
        self.assertIsNone(
            db.one("SELECT id FROM knowledge WHERE id=?", (knowledge_id,)))

    def test_not_soft_deleted_record_returns_404(self):
        alive = db.insert(
            "job",
            {
                "tenant_id": 2,
                "brief_json": json.dumps({"direction": "仍在使用"}),
                "status": "done",
            },
        )
        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("job", alive)
        self.assertEqual(404, ctx.exception.status_code)
        self.assertIsNotNone(db.one("SELECT id FROM job WHERE id=?", (alive,)))

    def test_unknown_kind_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("billing_log", 1)
        self.assertEqual(404, ctx.exception.status_code)

    def test_cross_tenant_record_returns_404(self):
        foreign = db.insert(
            "knowledge",
            {"tenant_id": 1, "title": "他租户知识", "content": "不可见",
             "deleted_at": 100},
        )
        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("knowledge", foreign)
        self.assertEqual(404, ctx.exception.status_code)
        self.assertIsNotNone(
            db.one("SELECT id FROM knowledge WHERE id=?", (foreign,)))

    def test_member_cannot_purge(self):
        own = self._deleted_avatar()
        auth.set_current(
            {
                "id": 21,
                "tenant_id": 2,
                "username": "member",
                "role": "member",
                "modules": ["avatar"],
            }
        )
        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("avatar", own)
        self.assertEqual(403, ctx.exception.status_code)
        self.assertIsNotNone(
            db.one("SELECT id FROM avatar_job WHERE id=?", (own,)))

    def test_active_status_record_is_rejected_409(self):
        # 理论上进行中的记录进不了回收站,这里防御脏数据
        stuck = self._deleted_job(status="running")
        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("job", stuck)
        self.assertEqual(409, ctx.exception.status_code)
        self.assertIsNotNone(db.one("SELECT id FROM job WHERE id=?", (stuck,)))

    def test_file_removal_failure_retains_db_anchor_and_is_retryable(self):
        job_id = self._deleted_job()
        job_dir = os.path.join(self.asset_root, f"job{job_id}")
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, "final.md"), "w") as fh:
            fh.write("payload")
        with patch.object(
            main.shutil, "rmtree", side_effect=OSError("permission denied")
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.trash_purge("job", job_id)
        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("记录已锁定并保留", str(ctx.exception.detail))
        retained = db.one(
            "SELECT id,delete_reason FROM job WHERE id=?", (job_id,)
        )
        self.assertIsNotNone(retained)
        self.assertTrue(
            retained["delete_reason"].startswith(main._PURGE_MARKER_PREFIX)
        )
        self.assertTrue(os.path.isfile(os.path.join(job_dir, "final.md")))
        with self.assertRaises(HTTPException) as restore_ctx:
            main.trash_restore("job", job_id)
        self.assertEqual(409, restore_ctx.exception.status_code)
        self.assertIn("不能恢复", str(restore_ctx.exception.detail))
        listed = next(
            item for item in main.trash_list()["items"]
            if item["kind"] == "job" and item["id"] == job_id
        )
        self.assertNotIn(main._PURGE_MARKER_PREFIX, listed["reason"])

        result = main.trash_purge("job", job_id)
        self.assertTrue(result["purged"])
        self.assertEqual(0, result["files_failed"])
        self.assertIsNone(db.one("SELECT id FROM job WHERE id=?", (job_id,)))

    def test_linked_video_is_removed_before_job_database_anchor(self):
        job_id = self._deleted_job()
        tv_id = db.insert(
            "tv_job",
            {
                "tenant_id": 2,
                "job_id": job_id,
                "params_json": "{}",
                "status": "done",
                "billing_status": "succeeded",
            },
        )
        clip_dir = os.path.join(self.asset_root, "tv")
        os.makedirs(clip_dir, exist_ok=True)
        clip = os.path.join(clip_dir, f"tv_{tv_id}.mp4")
        with open(clip, "wb") as fh:
            fh.write(b"video")
        db.update("tv_job", tv_id, {"video_file": f"/files/tv/tv_{tv_id}.mp4"})

        result = main.trash_purge("job", job_id)

        self.assertTrue(result["purged"])
        self.assertEqual(1, result["files_removed"])
        self.assertFalse(os.path.exists(clip))
        self.assertIsNone(db.one("SELECT id FROM job WHERE id=?", (job_id,)))
        self.assertIsNone(db.one("SELECT id FROM tv_job WHERE id=?", (tv_id,)))

    def test_unsafe_linked_video_path_retains_job_and_video_rows(self):
        job_id = self._deleted_job()
        tv_id = db.insert(
            "tv_job",
            {
                "tenant_id": 2,
                "job_id": job_id,
                "params_json": "{}",
                "status": "done",
                "billing_status": "succeeded",
                "video_file": "/files/tv/../foreign.mp4",
            },
        )

        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("job", job_id)

        self.assertEqual(409, ctx.exception.status_code)
        self.assertIsNotNone(db.one("SELECT id FROM job WHERE id=?", (job_id,)))
        self.assertIsNotNone(db.one("SELECT id FROM tv_job WHERE id=?", (tv_id,)))

    def test_job_directory_regular_file_or_symlink_blocks_purge(self):
        for shape in ("file", "symlink"):
            with self.subTest(shape=shape):
                job_id = self._deleted_job()
                job_path = os.path.join(self.asset_root, f"job{job_id}")
                if shape == "file":
                    with open(job_path, "wb") as fh:
                        fh.write(b"not-a-directory")
                else:
                    os.symlink(
                        os.path.join(self.tmp.name, "missing-target"),
                        job_path,
                    )

                with self.assertRaises(HTTPException) as ctx:
                    main.trash_purge("job", job_id)

                self.assertEqual(409, ctx.exception.status_code)
                self.assertTrue(os.path.lexists(job_path))
                row = db.one(
                    "SELECT delete_reason FROM job WHERE id=?", (job_id,)
                )
                self.assertTrue(
                    row["delete_reason"].startswith(main._PURGE_MARKER_PREFIX)
                )
                os.remove(job_path)

    def test_file_cleanup_runs_outside_sqlite_write_transaction(self):
        job_id = self._deleted_job()
        depths = []
        original_local = main._purge_local_files
        original_tv = main._purge_tv_files

        def local(kind, rid):
            depths.append(getattr(db._thread, "atomic_depth", 0))
            return original_local(kind, rid)

        def videos(paths, tid, rid):
            depths.append(getattr(db._thread, "atomic_depth", 0))
            return original_tv(paths, tid, rid)

        with patch.object(main, "_purge_local_files", side_effect=local), \
                patch.object(main, "_purge_tv_files", side_effect=videos):
            result = main.trash_purge("job", job_id)

        self.assertTrue(result["purged"])
        self.assertEqual([0, 0], depths)

    def test_new_linked_video_during_file_cleanup_keeps_retry_anchor(self):
        job_id = self._deleted_job()
        inserted = {}

        def add_concurrent_relation(_kind, _rid):
            inserted["id"] = db.insert(
                "tv_job",
                {
                    "tenant_id": 2,
                    "job_id": job_id,
                    "params_json": "{}",
                    "status": "done",
                    "billing_status": "succeeded",
                },
            )
            return 0, 0

        with patch.object(
            main, "_purge_local_files", side_effect=add_concurrent_relation
        ), self.assertRaises(HTTPException) as ctx:
            main.trash_purge("job", job_id)

        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("关联交付文件", str(ctx.exception.detail))
        row = db.one("SELECT delete_reason FROM job WHERE id=?", (job_id,))
        self.assertTrue(
            row["delete_reason"].startswith(main._PURGE_MARKER_PREFIX)
        )
        self.assertIsNotNone(
            db.one("SELECT id FROM tv_job WHERE id=?", (inserted["id"],))
        )

        result = main.trash_purge("job", job_id)
        self.assertTrue(result["purged"])
        self.assertIsNone(db.one("SELECT id FROM job WHERE id=?", (job_id,)))
        self.assertIsNone(
            db.one("SELECT id FROM tv_job WHERE id=?", (inserted["id"],))
        )


if __name__ == "__main__":
    unittest.main()
