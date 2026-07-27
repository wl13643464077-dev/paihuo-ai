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

    def test_job_purge_applies_documented_retention_matrix(self):
        self.assertEqual(
            {
                "job", "station_run", "asset", "knowledge", "tv_job",
                "notification", "managed_files",
            },
            set(main._JOB_PURGE_RETENTION_MATRIX["delete"]),
        )
        self.assertEqual(
            {"censor_log", "publish_log", "pub_task"},
            set(main._JOB_PURGE_RETENTION_MATRIX["redact_keep"]),
        )
        self.assertEqual(
            {
                "billing_log", "billing_operation",
                "wechat_draft_delivery",
            },
            set(main._JOB_PURGE_RETENTION_MATRIX["opaque_keep"]),
        )
        secret = "客户新品绝密标题"
        secret_body = "未公开正文、客户名单与投放素材路径"
        job_id = self._deleted_job(
            brief_json=json.dumps({
                "direction": secret,
                "material": secret_body,
                "ref_link": "https://private.example/material",
            }, ensure_ascii=False),
        )
        db.insert("station_run", {
            "job_id": job_id,
            "station_idx": 4,
            "status": "done",
            "output_json": json.dumps({
                "title_candidates": [secret],
                "body": secret_body,
                "images": [f"/files/job{job_id}/secret.png"],
            }, ensure_ascii=False),
            "review_comment": "内部审核意见",
            "steps_json": json.dumps([{"label": "内部步骤"}], ensure_ascii=False),
        })
        asset_id = db.insert("asset", {
            "tenant_id": 2,
            "job_id": job_id,
            "type": "final",
            "payload_json": json.dumps({
                "title": secret, "body": secret_body,
                "file": f"/files/job{job_id}/secret.png",
            }, ensure_ascii=False),
        })
        knowledge_id = db.insert("knowledge", {
            "tenant_id": 2,
            "job_id": job_id,
            "title": f"《{secret}》交付复盘",
            "content": secret_body,
            "tags_json": '["自动沉淀"]',
        })

        tv_id = db.insert("tv_job", {
            "tenant_id": 2,
            "job_id": job_id,
            "params_json": json.dumps({
                "title": secret,
                "body": secret_body,
                "images": [f"/files/job{job_id}/secret.png"],
            }, ensure_ascii=False),
            "script": secret_body,
            "status": "done",
            "billing_status": "succeeded",
        })
        tv_name = f"tv_{tv_id}.mp4"
        db.update("tv_job", tv_id, {"video_file": f"/files/tv/{tv_name}"})
        publish_id = db.insert("publish_log", {
            "tenant_id": 2,
            "job_id": job_id,
            "platform": "公众号",
            "title": secret,
            "url": "https://private.example/published",
            "source": "draft",
            "retro_json": '{"1":{"state":"done"}}',
        })
        censor_id = db.insert("censor_log", {
            "tenant_id": 2,
            "job_id": job_id,
            "kind": "pre",
            "platform": "公众号",
            "title": secret,
            "verdict": "pass",
            "score": 98,
            "issues_json": json.dumps([{"quote": secret_body}],
                                      ensure_ascii=False),
            "report": secret_body,
        })
        pub_id = db.insert("pub_task", {
            "tenant_id": 2,
            "platform": "xhs",
            "account": "private-account",
            "payload_json": json.dumps({
                "job_id": job_id,
                "title": secret,
                "body": secret_body,
                "images": [f"/files/job{job_id}/secret.png"],
            }, ensure_ascii=False),
            "status": "done",
            "submission_state": "submitted",
            "log": f"已发布:{secret}",
        })
        pub_shot = f"/files/pub/fail_{pub_id}.png"
        db.update("pub_task", pub_id, {
            "fail_json": json.dumps({
                "why": secret_body, "shot": pub_shot,
            }, ensure_ascii=False),
        })
        op_key = "wechat-draft:compliance-anchor"
        delivery_id = db.insert("wechat_draft_delivery", {
            "tenant_id": 2,
            "job_id": job_id,
            "request_hash": "a" * 64,
            "request_key": "a" * 20,
            "title": secret,
            "status": "done",
            "billing_status": "succeeded",
            "billing_points": 1,
            "op_key": op_key,
            "media_id": "external-media-anchor",
            "publish_log_id": publish_id,
            "report_json": json.dumps({"summary": secret_body},
                                      ensure_ascii=False),
            "error": secret_body,
        })
        db.insert("billing_operation", {
            "op_key": op_key,
            "tenant_id": 2,
            "job_id": job_id,
            "action": "wechat_draft",
            "units": 1,
            "points": 1,
            "note": secret,
            "status": "succeeded",
            "error": secret_body,
        })
        retro_op_key = "retro:compliance-anchor"
        db.insert("billing_operation", {
            "op_key": retro_op_key,
            "tenant_id": 2,
            "job_id": job_id,
            "action": "censor_retro",
            "units": 1,
            "points": 1,
            "note": f"自动复盘T+1·《{secret[:14]}》",
            "status": "succeeded",
            "error": secret_body,
        })
        billing_ids = [
            db.insert("billing_log", {
                "tenant_id": 2, "job_id": job_id,
                "delta": -18, "balance": 82,
                "reason": f"内容流水线整单 · 工单#{job_id}·{secret}",
            }),
            db.insert("billing_log", {
                "tenant_id": 2, "job_id": job_id,
                "delta": -1, "balance": 81,
                "reason": f"一键发公众号草稿箱(含终审) · {secret[:20]}",
            }),
            db.insert("billing_log", {
                "tenant_id": 2, "job_id": job_id,
                "delta": -1, "balance": 80,
                "reason": (
                    f"深度复盘审查 · 自动复盘T+1·《{secret[:14]}》"
                ),
            }),
        ]
        notice_id = db.insert("notification", {
            "tenant_id": 2,
            "job_id": job_id,
            "kind": "pub",
            "title": "矩阵发布成功",
            "body": secret,
            "link": "#/channels",
        })
        retro_notice_id = db.insert("notification", {
            "tenant_id": 2,
            "job_id": job_id,
            "kind": "report",
            "title": f"《{secret[:20]}》T+1 自动复盘",
            "body": "复盘指标摘要",
            "link": "#/censor",
        })
        unrelated_notice_id = db.insert("notification", {
            "tenant_id": 2,
            "kind": "report",
            "title": "同租户的无关经营提醒",
            "body": f"这是一条引用标题“{secret}”的无关提醒",
            "link": "#/knowledge",
        })

        # 同标题的别家租户记录必须原样保留。
        db.insert("tenants", {"id": 3, "name": "别家", "balance": 20})
        foreign_publish = db.insert("publish_log", {
            "tenant_id": 3,
            "job_id": job_id,
            "platform": "公众号",
            "title": secret,
            "url": "https://foreign.example/post",
            "retro_json": "{}",
        })
        foreign_censor = db.insert("censor_log", {
            "tenant_id": 3,
            "kind": "pre",
            "platform": "公众号",
            "title": secret,
            "issues_json": '[{"foreign":true}]',
            "report": "别家正文",
        })

        job_dir = os.path.join(self.asset_root, f"job{job_id}")
        tv_dir = os.path.join(self.asset_root, "tv")
        pub_dir = os.path.join(self.asset_root, "pub")
        os.makedirs(job_dir, exist_ok=True)
        os.makedirs(tv_dir, exist_ok=True)
        os.makedirs(pub_dir, exist_ok=True)
        with open(os.path.join(job_dir, "secret.png"), "wb") as fh:
            fh.write(b"job")
        with open(os.path.join(tv_dir, tv_name), "wb") as fh:
            fh.write(b"tv")
        with open(os.path.join(pub_dir, f"fail_{pub_id}.png"), "wb") as fh:
            fh.write(b"shot")

        result = main.trash_purge("job", job_id)

        self.assertTrue(result["purged"])
        self.assertEqual(3, result["files_removed"])
        self.assertEqual(0, result["files_failed"])
        for table, column, row_id in (
            ("job", "id", job_id),
            ("asset", "id", asset_id),
            ("knowledge", "id", knowledge_id),
            ("tv_job", "id", tv_id),
            ("notification", "id", notice_id),
            ("notification", "id", retro_notice_id),
        ):
            self.assertIsNone(
                db.one(f"SELECT {column} FROM {table} WHERE id=?", (row_id,)),
                table,
            )
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) n FROM station_run WHERE job_id=?",
                   (job_id,))["n"],
        )

        censor_row = db.one("SELECT * FROM censor_log WHERE id=?", (censor_id,))
        self.assertEqual(main._PURGED_CONTENT, censor_row["title"])
        self.assertEqual("[]", censor_row["issues_json"])
        self.assertEqual("", censor_row["report"])
        publish_row = db.one(
            "SELECT * FROM publish_log WHERE id=?", (publish_id,))
        self.assertEqual(main._PURGED_CONTENT, publish_row["title"])
        self.assertEqual("", publish_row["url"])
        self.assertEqual("{}", publish_row["retro_json"])
        pub_row = db.one("SELECT * FROM pub_task WHERE id=?", (pub_id,))
        self.assertEqual(
            {"job_id": job_id, "purged": True},
            json.loads(pub_row["payload_json"]),
        )
        self.assertEqual("submitted", pub_row["submission_state"])
        self.assertEqual("done", pub_row["status"])
        self.assertIsNone(pub_row["account"])
        self.assertEqual("", pub_row["log"])
        self.assertEqual("{}", pub_row["fail_json"])

        delivery = db.one(
            "SELECT * FROM wechat_draft_delivery WHERE id=?", (delivery_id,))
        self.assertEqual("a" * 64, delivery["request_hash"])
        self.assertEqual(op_key, delivery["op_key"])
        self.assertEqual("external-media-anchor", delivery["media_id"])
        self.assertEqual("done", delivery["status"])
        self.assertEqual(main._PURGED_CONTENT, delivery["title"])
        self.assertIsNone(delivery["report_json"])
        self.assertIsNone(delivery["error"])
        operation = db.one(
            "SELECT * FROM billing_operation WHERE op_key=?", (op_key,))
        self.assertEqual("succeeded", operation["status"])
        self.assertEqual(1, operation["points"])
        self.assertEqual(main._PURGED_CONTENT, operation["note"])
        self.assertIsNone(operation["error"])
        retro_operation = db.one(
            "SELECT * FROM billing_operation WHERE op_key=?",
            (retro_op_key,),
        )
        self.assertEqual("censor_retro", retro_operation["action"])
        self.assertEqual("succeeded", retro_operation["status"])
        self.assertEqual(1, retro_operation["points"])
        self.assertEqual(main._PURGED_CONTENT, retro_operation["note"])
        self.assertIsNone(retro_operation["error"])
        billing_rows = db.q(
            "SELECT id,delta,balance,reason FROM billing_log "
            "WHERE id IN (?,?,?) ORDER BY id",
            tuple(billing_ids),
        )
        self.assertEqual([-18, -1, -1],
                         [row["delta"] for row in billing_rows])
        self.assertEqual([82, 81, 80],
                         [row["balance"] for row in billing_rows])
        self.assertTrue(all(
            main._PURGED_CONTENT in row["reason"] for row in billing_rows))
        self.assertNotIn(
            secret,
            json.dumps(
                [
                    censor_row, publish_row, pub_row, delivery,
                    operation, retro_operation, billing_rows,
                ],
                ensure_ascii=False,
            ),
        )
        self.assertIsNotNone(
            db.one(
                "SELECT id FROM notification WHERE id=?",
                (unrelated_notice_id,),
            )
        )
        self.assertEqual(
            secret,
            db.one("SELECT title FROM publish_log WHERE id=?",
                   (foreign_publish,))["title"],
        )
        self.assertEqual(
            secret,
            db.one("SELECT title FROM censor_log WHERE id=?",
                   (foreign_censor,))["title"],
        )
        self.assertFalse(os.path.exists(job_dir))
        self.assertFalse(os.path.exists(
            os.path.join(tv_dir, tv_name)))
        self.assertFalse(os.path.exists(
            os.path.join(pub_dir, f"fail_{pub_id}.png")))

    def test_same_tenant_same_title_relations_are_never_cross_purged(self):
        title = "同一个活动标题"
        job_a = self._deleted_job(
            brief_json=json.dumps({"direction": title}, ensure_ascii=False)
        )
        job_b = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": title}, ensure_ascii=False),
            "status": "done",
            "billing_status": "succeeded",
        })
        censor_a = db.insert("censor_log", {
            "tenant_id": 2, "job_id": job_a, "kind": "pre",
            "platform": "公众号", "title": title, "report": "A审查正文",
            "issues_json": "[]",
        })
        censor_b = db.insert("censor_log", {
            "tenant_id": 2, "job_id": job_b, "kind": "pre",
            "platform": "公众号", "title": title, "report": "B审查正文",
            "issues_json": "[]",
        })
        notice_a = db.insert("notification", {
            "tenant_id": 2, "job_id": job_a, "kind": "report",
            "title": title, "body": "A通知", "link": "#/censor",
        })
        notice_b = db.insert("notification", {
            "tenant_id": 2, "job_id": job_b, "kind": "report",
            "title": title, "body": "B通知", "link": "#/censor",
        })
        bill_a = db.insert("billing_log", {
            "tenant_id": 2, "job_id": job_a, "delta": -1, "balance": 19,
            "reason": f"一键发公众号草稿箱(含终审) · {title}",
        })
        bill_b = db.insert("billing_log", {
            "tenant_id": 2, "job_id": job_b, "delta": -1, "balance": 18,
            "reason": f"一键发公众号草稿箱(含终审) · {title}",
        })
        op_a = "same-title-a"
        op_b = "same-title-b"
        db.insert("billing_operation", {
            "op_key": op_a, "tenant_id": 2, "job_id": job_a,
            "action": "censor_retro", "points": 1, "note": title,
            "status": "succeeded", "error": "A错误正文",
        })
        db.insert("billing_operation", {
            "op_key": op_b, "tenant_id": 2, "job_id": job_b,
            "action": "censor_retro", "points": 1, "note": title,
            "status": "succeeded", "error": "B错误正文",
        })

        result = main.trash_purge("job", job_a)

        self.assertTrue(result["purged"])
        self.assertIsNone(db.one(
            "SELECT id FROM notification WHERE id=?", (notice_a,)))
        self.assertEqual("B通知", db.one(
            "SELECT body FROM notification WHERE id=?", (notice_b,))["body"])
        self.assertEqual(main._PURGED_CONTENT, db.one(
            "SELECT title FROM censor_log WHERE id=?", (censor_a,))["title"])
        self.assertEqual("B审查正文", db.one(
            "SELECT report FROM censor_log WHERE id=?", (censor_b,))["report"])
        self.assertIn(main._PURGED_CONTENT, db.one(
            "SELECT reason FROM billing_log WHERE id=?", (bill_a,))["reason"])
        self.assertEqual(
            f"一键发公众号草稿箱(含终审) · {title}",
            db.one("SELECT reason FROM billing_log WHERE id=?", (bill_b,))[
                "reason"
            ],
        )
        self.assertEqual(main._PURGED_CONTENT, db.one(
            "SELECT note FROM billing_operation WHERE op_key=?", (op_a,))[
                "note"
            ])
        self.assertEqual(title, db.one(
            "SELECT note FROM billing_operation WHERE op_key=?", (op_b,))[
                "note"
            ])
        self.assertIsNotNone(db.one(
            "SELECT id FROM job WHERE id=?", (job_b,)))

    def test_unattributed_legacy_title_record_blocks_without_mutation(self):
        title = "历史同标题"
        job_id = self._deleted_job(
            brief_json=json.dumps({"direction": title}, ensure_ascii=False)
        )
        legacy = db.insert("censor_log", {
            "tenant_id": 2,
            "kind": "pre",
            "platform": "公众号",
            "title": title,
            "issues_json": '[{"legacy":true}]',
            "report": "无法证明属于哪一单",
        })

        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("job", job_id)

        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("无法安全归属", str(ctx.exception.detail))
        self.assertEqual(title, db.one(
            "SELECT title FROM censor_log WHERE id=?", (legacy,))["title"])
        retained = db.one(
            "SELECT delete_reason FROM job WHERE id=?", (job_id,))
        self.assertFalse(main._is_purge_marker(retained["delete_reason"]))

    def test_unattributed_legacy_notification_blocks_without_mutation(self):
        title = "历史通知标题"
        job_id = self._deleted_job(
            brief_json=json.dumps({"direction": title}, ensure_ascii=False)
        )
        legacy = db.insert("notification", {
            "tenant_id": 2,
            "kind": "report",
            "title": f"《{title}》T+1 自动复盘",
            "body": "无法证明属于哪一单的历史通知",
            "link": "#/censor",
        })

        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("job", job_id)

        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("无法安全归属", str(ctx.exception.detail))
        self.assertEqual(
            f"《{title}》T+1 自动复盘",
            db.one(
                "SELECT title FROM notification WHERE id=?", (legacy,)
            )["title"],
        )
        retained = db.one(
            "SELECT delete_reason FROM job WHERE id=?", (job_id,))
        self.assertFalse(main._is_purge_marker(retained["delete_reason"]))

    def test_legacy_spaced_job_refund_is_redacted_without_cross_purge(self):
        job_id = self._deleted_job()
        linked = db.insert("billing_log", {
            "tenant_id": 2,
            "delta": 18,
            "balance": 38,
            "reason": (
                f"退回:内容流水线整单 · 老板取消工单 #{job_id}"
            ),
        })
        other_reason = (
            f"退回:内容流水线整单 · 老板取消工单 #{job_id}0"
        )
        unrelated = db.insert("billing_log", {
            "tenant_id": 2,
            "delta": 18,
            "balance": 56,
            "reason": other_reason,
        })

        result = main.trash_purge("job", job_id)

        self.assertTrue(result["purged"])
        self.assertEqual(
            f"退回:内容流水线整单 · {main._PURGED_CONTENT}",
            db.one(
                "SELECT reason FROM billing_log WHERE id=?", (linked,)
            )["reason"],
        )
        self.assertEqual(
            other_reason,
            db.one(
                "SELECT reason FROM billing_log WHERE id=?", (unrelated,)
            )["reason"],
        )

    def test_repeated_purge_is_idempotent_and_tenant_scoped(self):
        job_id = self._deleted_job()
        first = main.trash_purge("job", job_id)
        second = main.trash_purge("job", job_id)
        self.assertTrue(first["purged"])
        self.assertEqual(
            {"ok": True, "purged": True, "kind": "job", "id": job_id,
             "files_removed": 0, "files_failed": 0},
            second,
        )
        self.assertEqual(
            "1",
            db.get_setting(main._purge_tombstone_key("job", 2, job_id)),
        )

        db.insert("tenants", {"id": 3, "name": "别家", "balance": 20})
        auth.set_current({
            "id": 30, "tenant_id": 3, "username": "foreign-owner",
            "role": "owner", "modules": [],
        })
        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("job", job_id)
        self.assertEqual(404, ctx.exception.status_code)

    def test_unsafe_publish_screenshot_blocks_purge_at_file_boundary(self):
        job_id = self._deleted_job(
            brief_json=json.dumps({"direction": "边界测试"}),
        )
        pub_id = db.insert("pub_task", {
            "tenant_id": 2,
            "platform": "xhs",
            "payload_json": json.dumps({
                "job_id": job_id, "title": "边界测试",
            }),
            "status": "failed",
            "submission_state": "not_submitted",
        })
        target = os.path.join(self.tmp.name, "foreign-sensitive.png")
        with open(target, "wb") as fh:
            fh.write(b"foreign")
        pub_dir = os.path.join(self.asset_root, "pub")
        os.makedirs(pub_dir, exist_ok=True)
        shot = os.path.join(pub_dir, f"fail_{pub_id}.png")
        os.symlink(target, shot)
        db.update("pub_task", pub_id, {
            "fail_json": json.dumps({
                "shot": f"/files/pub/fail_{pub_id}.png",
            }),
        })

        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("job", job_id)

        self.assertEqual(409, ctx.exception.status_code)
        self.assertTrue(os.path.isfile(target))
        self.assertTrue(os.path.islink(shot))
        self.assertIsNotNone(db.one("SELECT id FROM job WHERE id=?", (job_id,)))
        self.assertIsNotNone(
            db.one("SELECT id FROM pub_task WHERE id=?", (pub_id,)))

    def test_active_delivery_anchor_blocks_destructive_purge(self):
        job_id = self._deleted_job()
        delivery_id = db.insert("wechat_draft_delivery", {
            "tenant_id": 2,
            "job_id": job_id,
            "request_hash": "b" * 64,
            "request_key": "b" * 20,
            "title": "正在投递",
            "status": "submitting",
            "billing_status": "charged",
            "op_key": "wechat-active-anchor",
        })

        with self.assertRaises(HTTPException) as ctx:
            main.trash_purge("job", job_id)

        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("对账", str(ctx.exception.detail))
        self.assertEqual(
            "正在投递",
            db.one(
                "SELECT title FROM wechat_draft_delivery WHERE id=?",
                (delivery_id,),
            )["title"],
        )
        self.assertIsNotNone(db.one("SELECT id FROM job WHERE id=?", (job_id,)))

    def test_irreversible_delete_copy_discloses_retained_anchors(self):
        app_js = os.path.join(
            os.path.dirname(__file__), "..", "static", "app.js"
        )
        with open(app_js, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("去标识化账务与审计锚点", source)
        self.assertIn("业务正文、客户素材和交付文件将永久删除", source)
        self.assertIn('toast("业务内容已彻底删除")', source)


if __name__ == "__main__":
    unittest.main()
