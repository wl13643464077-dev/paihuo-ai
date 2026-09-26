"""店员现场照片：服务端压缩、水印、落盘与访问归属。"""
from __future__ import annotations

import io
import os
import tempfile
import unittest

from PIL import Image

from app import assetfiles, db, inspection, photoproof


def _jpeg(w=3000, h=2000, color=(200, 120, 40)) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (w, h), color)
    exif = img.getexif()
    exif[0x0132] = "2001:01:01 00:00:00"   # 伪造的拍摄时间，必须被丢弃
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


class PhotoProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "photo.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "连锁", "industries_json": "[]"})
        db.execute("INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                   "VALUES(2,'restaurant',1,0)")
        for uid, role, title in ((20, "owner", "staff"), (22, "member", "manager"),
                                 (23, "member", "staff")):
            db.insert("users", {"id": uid, "tenant_id": 2, "username": f"u{uid}",
                                "password_hash": "x", "role": role, "job_title": title,
                                "modules_json": '["restaurant"]', "enabled": 1})
        self.branch = inspection.create_branch(2, 20, "restaurant", {"name": "朝阳店"})
        self.other = inspection.create_branch(2, 20, "restaurant", {"name": "静安店"})
        inspection.set_member_branches(20, 22, [self.branch["id"]])
        self.root = os.path.join(self.tmp.name, "assets")

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_store_resizes_strips_exif_and_watermarks_with_server_time(self):
        ts = 1790000000.0   # 2026-09-21 北京时间
        meta = photoproof.store_photo(2, self.branch["id"], _jpeg(),
                                      branch_name="朝阳店", person_name="小王",
                                      now=ts, asset_root=self.root)
        self.assertEqual(ts, meta["received_at"])
        self.assertIn("朝阳店", meta["watermark_text"])
        self.assertIn("小王", meta["watermark_text"])
        self.assertIn("2026-09-2", meta["watermark_text"])
        self.assertLessEqual(max(meta["width"], meta["height"]), photoproof.MAX_EDGE)
        path = os.path.join(self.root, meta["storage_key"])
        with Image.open(path) as img:
            self.assertEqual("JPEG", img.format)
            self.assertFalse(dict(img.getexif()))
        self.assertTrue(photoproof.STAFF_FILE_RE.match(meta["url"]))

    def test_rejects_non_images_and_oversize(self):
        with self.assertRaises(photoproof.PhotoError):
            photoproof.store_photo(2, self.branch["id"], b"not an image",
                                   asset_root=self.root)
        with self.assertRaises(photoproof.PhotoError):
            photoproof.store_photo(2, self.branch["id"], b"",
                                   asset_root=self.root)
        with self.assertRaises(photoproof.PhotoError):
            photoproof.store_photo(2, 0, _jpeg(10, 10), asset_root=self.root)

    def test_file_scope_uses_branch_industry_and_fails_closed(self):
        meta = photoproof.store_photo(2, self.branch["id"], _jpeg(40, 30),
                                      asset_root=self.root)
        scope = assetfiles.file_access_scope(meta["url"])
        self.assertEqual(2, scope["tenant_id"])
        self.assertEqual("restaurant", scope["required_module"])
        # 伪造成别家租户的路径 → 门店不存在 → 租户 0
        forged = meta["url"].replace("/staff/2/", "/staff/3/")
        self.assertEqual(0, assetfiles.file_access_scope(forged)["tenant_id"])

    def test_branch_visibility_follows_store_binding(self):
        self.assertTrue(photoproof.branch_visible(2, 20, self.other["id"]))   # 老板
        self.assertTrue(photoproof.branch_visible(2, 22, self.branch["id"]))  # 负责的店
        self.assertFalse(photoproof.branch_visible(2, 22, self.other["id"]))  # 别的店
        self.assertFalse(photoproof.branch_visible(2, 23, self.branch["id"])) # 未分配
        self.assertFalse(photoproof.branch_visible(3, 20, self.branch["id"])) # 租户不符


if __name__ == "__main__":
    unittest.main()
