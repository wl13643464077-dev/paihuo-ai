"""An interrupted image request cannot expose an unpaid candidate."""

from io import BytesIO
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

from app import assetfiles, brand_media, db, main


class ActivityBillingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.temp.name, "billing-recovery.db")
        self.asset_patch = mock.patch.object(
            assetfiles, "ASSET_ROOT", os.path.join(self.temp.name, "assets"),
        )
        self.asset_patch.start()
        db.conn()
        db.insert("tenants", {"id": 2, "name": "测试餐饮", "balance": 100})
        self.task_id = db.insert("task", {
            "tenant_id": 2, "emp_idx": 160, "brief_json": "{}", "status": "done",
        })

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_db_path
        self.asset_patch.stop()
        self.temp.cleanup()

    def _save(self, op_key: str):
        output = BytesIO()
        Image.new("RGB", (48, 48), (40, 90, 130)).save(output, "PNG")
        return brand_media.save_task_artwork(
            2, self.task_id, "主视觉", {
                "image_bytes": output.getvalue(),
                "status": "needs_manual_review",
                "quality": {"status": "needs_manual_review"},
                "required_text": {
                    "store_name": "青禾小馆", "activity_title": "周年庆",
                    "activity_content": "新品体验", "authorized_texts": [],
                },
                "brand_package_id": 1, "brand_version": 1,
            }, billing_op_key=op_key,
        )

    def test_candidate_visible_only_after_success_and_refund_cleanup_is_bounded(self):
        op_key = "a" * 32
        now = 1_800_000_000.0
        db.insert("billing_operation", {
            "op_key": op_key, "tenant_id": 2, "action": "product_shot",
            "units": 1, "points": 2, "status": "charged",
            "created_at": now, "updated_at": now,
        })
        image = self._save(op_key)
        path = os.path.join(assetfiles.ASSET_ROOT, image["stored_path"])
        self.assertTrue(os.path.isfile(path))
        self.assertEqual([], brand_media.list_task_artwork(2, self.task_id))
        with self.assertRaises(brand_media.BrandMediaError):
            brand_media.get_task_artwork_file(2, self.task_id, image["id"])

        db.execute(
            "UPDATE billing_operation SET status='succeeded' WHERE op_key=?",
            (op_key,),
        )
        self.assertEqual(1, len(brand_media.list_task_artwork(2, self.task_id)))
        self.assertEqual(os.path.realpath(path), brand_media.get_task_artwork_file(
            2, self.task_id, image["id"],
        ))
        self.assertEqual(0, main._recover_unpaid_activity_artwork()["removed"])

        db.execute(
            "UPDATE billing_operation SET status='refunded' WHERE op_key=?",
            (op_key,),
        )
        self.assertEqual([], brand_media.list_task_artwork(2, self.task_id))
        outcome = main._recover_unpaid_activity_artwork()
        self.assertEqual({"scanned": 1, "removed": 1, "errors": 0}, outcome)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM task_activity_image WHERE id=?",
            (image["id"],),
        )["n"])


if __name__ == "__main__":
    unittest.main()
