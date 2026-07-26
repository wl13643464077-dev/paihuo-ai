import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import assetfiles, auth, db


class AssetFileIsolationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        self.old_asset_root = assetfiles.ASSET_ROOT
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "assets.db")
        assetfiles.ASSET_ROOT = os.path.join(self.tmp.name, "assets")
        os.makedirs(assetfiles.ASSET_ROOT)
        db.conn()
        db.insert("tenants", {"id": 2, "name": "租户甲"})
        db.insert("tenants", {"id": 3, "name": "租户乙"})
        self.own_job = db.insert(
            "job", {"tenant_id": 2, "brief_json": json.dumps({"direction": "甲"})}
        )
        self.own_other_job = db.insert(
            "job", {"tenant_id": 2, "brief_json": json.dumps({"direction": "甲二"})}
        )
        self.foreign_job = db.insert(
            "job", {"tenant_id": 3, "brief_json": json.dumps({"direction": "乙"})}
        )
        self.own_file = self._write(self.own_job, "own.png")
        self.own_other_file = self._write(self.own_other_job, "other.png")
        self.foreign_file = self._write(self.foreign_job, "foreign.png")
        auth.set_current(
            {
                "id": 20,
                "tenant_id": 2,
                "username": "owner-a",
                "role": "owner",
                "modules": ["content"],
            }
        )

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        assetfiles.ASSET_ROOT = self.old_asset_root
        self.tmp.cleanup()

    def _write(self, job_id: int, name: str) -> str:
        folder = os.path.join(assetfiles.ASSET_ROOT, f"job{job_id}")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(b"fixture")
        return f"/files/job{job_id}/{name}"

    def test_resolver_requires_tenant_exact_job_real_file_and_extension(self):
        self.assertTrue(
            assetfiles.resolve_tenant_asset(
                self.own_file,
                2,
                expected_job_id=self.own_job,
                allowed_extensions=(".png",),
            ).endswith("own.png")
        )
        for path in (self.own_other_file, self.foreign_file):
            with self.subTest(path=path), self.assertRaises(
                assetfiles.AssetAccessError
            ):
                assetfiles.resolve_tenant_asset(
                    path, 2, expected_job_id=self.own_job
                )
        with self.assertRaises(assetfiles.AssetAccessError):
            assetfiles.resolve_tenant_asset(
                self.own_file, 2, allowed_extensions=(".mp4",)
            )

        outside = os.path.join(self.tmp.name, "outside.png")
        with open(outside, "wb") as handle:
            handle.write(b"secret")
        link = os.path.join(
            assetfiles.ASSET_ROOT, f"job{self.own_job}", "link.png"
        )
        os.symlink(outside, link)
        with self.assertRaises(assetfiles.AssetAccessError):
            assetfiles.resolve_tenant_asset(
                f"/files/job{self.own_job}/link.png", 2
            )

    def test_job_video_relation_is_checked_through_database(self):
        tv_id = db.insert(
            "tv_job",
            {
                "tenant_id": 2,
                "job_id": self.own_job,
                "params_json": "{}",
                "video_file": "",
            },
        )
        folder = os.path.join(assetfiles.ASSET_ROOT, "tv")
        os.makedirs(folder)
        path = os.path.join(folder, f"tv_{tv_id}.mp4")
        with open(path, "wb") as handle:
            handle.write(b"video")
        url = f"/files/tv/tv_{tv_id}.mp4"
        self.assertEqual(
            os.path.realpath(path),
            assetfiles.resolve_tenant_asset(
                url, 2, expected_job_id=self.own_job
            ),
        )
        with self.assertRaises(assetfiles.AssetAccessError):
            assetfiles.resolve_tenant_asset(
                url, 2, expected_job_id=self.own_other_job
            )

    def test_station_edits_cannot_persist_foreign_asset_reference(self):
        from app import main

        with self.assertRaises(HTTPException) as caught:
            main.station_action(
                self.own_job,
                5,
                {
                    "action": "edit",
                    "payload": {
                        "edits": {"images": [{"file": self.foreign_file}]}
                    },
                },
            )
        self.assertEqual(400, caught.exception.status_code)

        for edits in (
            {"sources": [{"url": "javascript:alert(document.cookie)"}]},
            {"file": "data:text/html,<script>alert(1)</script>"},
            {"deck": "https://attacker.example/payload.html"},
        ):
            with self.subTest(edits=edits), self.assertRaises(
                HTTPException
            ) as caught:
                main.station_action(
                    self.own_job,
                    5,
                    {"action": "edit", "payload": {"edits": edits}},
                )
            self.assertEqual(400, caught.exception.status_code)

    def test_zip_and_matrix_publish_cannot_open_foreign_asset(self):
        from app import main, matrixpub

        delivery = {
            "title": "测试",
            "body": "正文",
            "tags": [],
            "images": [],
            "packs": [
                {
                    "platform": "小红书",
                    "title": "测试",
                    "body": "正文",
                    "tags": [],
                    "cover": {"file": self.foreign_file},
                    "images": [],
                }
            ],
            "publish_plan": "",
        }
        with patch.object(main, "build_delivery", return_value=delivery):
            with self.assertRaises(HTTPException) as caught:
                main.pack_zip(self.own_job)
        self.assertEqual(400, caught.exception.status_code)

        self.assertEqual("evil", main._safe_zip_segment("  evil  "))
        self.assertNotIn("..", main._safe_zip_segment("../../evil"))
        self.assertNotIn("/", main._safe_zip_segment("../../evil"))
        self.assertNotIn("\\", main._safe_zip_segment(r"..\\..\\evil"))

        platform = next(iter(matrixpub.PLATFORMS))
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                main.matrix_publish(
                    {
                        "platform": platform,
                        "images": [self.foreign_file],
                        "title": "测试",
                    }
                )
            )
        self.assertEqual(400, caught.exception.status_code)

    def test_video_delete_requires_content_module_before_lookup(self):
        from app import main

        auth.set_current(
            {
                "id": 21,
                "tenant_id": 2,
                "username": "member",
                "role": "member",
                "modules": [],
            }
        )
        with self.assertRaises(HTTPException) as caught:
            main.text_video_delete(999)
        self.assertEqual(403, caught.exception.status_code)


if __name__ == "__main__":
    unittest.main()
