"""Schema-52 import retention and audit-archive regressions."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest

from app import db, inspection, inspectionimport
from tests.test_inspection_branch_import import branch_row, xlsx


class Schema52RetentionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._close_all_connections()
        db._conn = db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "retention.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "租户甲"})
        db.insert("tenants", {"id": 3, "name": "租户乙"})
        for tid in (2, 3):
            db.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                "VALUES(?,?,1,0)",
                (tid, "restaurant"),
            )
            db.insert("users", {
                "id": tid * 10, "tenant_id": tid,
                "username": f"owner-{tid}", "password_hash": "test",
                "role": "owner", "modules_json": "[]", "enabled": 1,
            })

    def tearDown(self):
        db._close_all_connections()
        db._conn = db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_startup_style_sweep_is_cross_tenant_bounded_and_preserves_authority(self):
        old = time.time() - inspectionimport.PREVIEW_TTL_SECONDS - 5
        previews = []
        retained_ciphertext = None
        for tid in (2, 3):
            previews.append(inspectionimport.preview_import(
                tid, tid * 10, "restaurant", f"retention-{tid:02d}-0001",
                f"tenant-{tid}.xlsx", xlsx([branch_row(f"R{tid:03d}")]),
            ))
            db.execute(
                "UPDATE inspection_branch_import SET updated_at=? WHERE id=?",
                (old, previews[-1]["import_id"]),
            )
            if retained_ciphertext is None:
                retained_ciphertext = str(db.one(
                    "SELECT payload_json FROM inspection_branch_import_row "
                    "WHERE import_id=? ORDER BY row_number LIMIT 1",
                    (previews[-1]["import_id"],),
                )["payload_json"])
        # Authoritative records must not be in the retention deletion set.
        branch_id = db.insert("store_branch", {
            "tenant_id": 2, "industry_key": "restaurant",
            "store_code": "AUTH001", "name": "权威门店",
        })
        visit_id = db.insert("inspection_visit", {
            "tenant_id": 2, "industry_key": "restaurant",
            "branch_id": branch_id, "employee_idx": inspection.EMPLOYEE_IDX,
            "request_key": "retention-visit-0001", "status": "draft",
            "created_by": 20, "created_at": 0, "updated_at": 0,
        })
        self.assertEqual(1, db.one("PRAGMA secure_delete")["secure_delete"])
        self.assertEqual(1, asyncio.run(db.arun(
            lambda: db.one("PRAGMA secure_delete")["secure_delete"],
        )))
        result = asyncio.run(db.arun(
            inspectionimport.cleanup_expired_previews,
            now=time.time(), batch_size=1,
        ))
        self.assertEqual(1, result["expired"])
        self.assertEqual(1, result["remaining_bounded"])
        self.assertEqual(
            1,
            db.one("SELECT COUNT(*) n FROM inspection_branch_import "
                   "WHERE status='previewed' AND staging_purged_at IS NULL")["n"],
        )
        asyncio.run(db.arun(
            inspectionimport.cleanup_expired_previews,
            now=time.time(), batch_size=8,
        ))
        self.assertEqual(
            2,
            db.one("SELECT COUNT(*) n FROM inspection_branch_import "
                   "WHERE status='expired' AND staging_purged_at IS NOT NULL")["n"],
        )
        self.assertEqual(1, result["wal_checkpointed"])
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM store_branch WHERE id=?", (branch_id,))["n"])
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM inspection_visit WHERE id=?", (visit_id,))["n"])
        backup_path = os.path.join(self.tmp.name, "retention-backup.db")
        backup = sqlite3.connect(backup_path)
        try:
            db.conn().backup(backup)
        finally:
            backup.close()
        with open(backup_path, "rb") as handle:
            backup_bytes = handle.read()
        self.assertNotIn(retained_ciphertext.encode("utf-8"), backup_bytes)
        wal_path = db.DB_PATH + "-wal"
        wal_bytes = b""
        if os.path.exists(wal_path):
            with open(wal_path, "rb") as handle:
                wal_bytes = handle.read()
        self.assertNotIn(retained_ciphertext.encode("utf-8"), wal_bytes)
        plan = db.q(
            "EXPLAIN QUERY PLAN SELECT id,tenant_id,status "
            "FROM inspection_branch_import "
            "WHERE status IN ('previewed','expired') "
            "AND staging_purged_at IS NULL AND updated_at<? "
            "ORDER BY updated_at,tenant_id,id LIMIT ?",
            (time.time(), inspectionimport.RETENTION_CLEANUP_BATCH),
        )
        details = " ".join(str(row["detail"]) for row in plan)
        self.assertIn("idx_inspection_branch_import_retention", details)
        self.assertNotIn("USE TEMP B-TREE", details)

    def test_committed_rows_are_deduplicated_into_verified_archive(self):
        data = xlsx([branch_row("ARCH001", phone="13712345678")])
        first = inspectionimport.preview_import(
            2, 20, "restaurant", "archive-first-0001", "first.xlsx", data,
        )
        inspectionimport.commit_import(2, 20, first["import_id"], "restaurant")
        second = inspectionimport.preview_import(
            2, 20, "restaurant", "archive-second-0001", "second.xlsx", data,
        )
        inspectionimport.commit_import(2, 20, second["import_id"], "restaurant")
        third = inspectionimport.preview_import(
            2, 20, "restaurant", "archive-third-0001", "third.xlsx", data,
        )
        inspectionimport.commit_import(2, 20, third["import_id"], "restaurant")
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) n FROM inspection_branch_import_row")["n"]
        )
        # First commit records create; later no-op imports record skip and
        # share one second authenticated archive.  The per-ledger action list
        # stays empty instead of growing by one JSON object per workbook row.
        self.assertEqual(2, db.one(
            "SELECT COUNT(*) n FROM inspection_branch_import_archive")["n"]
        )
        self.assertEqual(3, db.one(
            "SELECT COUNT(*) n FROM inspection_branch_import")["n"]
        )
        self.assertEqual(2, db.one(
            "SELECT MAX(length(audit_actions_json)) n "
            "FROM inspection_branch_import")["n"]
        )
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) n FROM store_branch WHERE store_code='ARCH001'")["n"]
        )
        page = inspectionimport.get_import(
            2, 20, third["import_id"], "restaurant", limit=10,
        )
        self.assertEqual(1, len(page["rows"]))
        self.assertEqual("skip", page["rows"][0]["action"])
        self.assertNotIn("13712345678", str(page))
        archive = db.one(
            "SELECT archive_sha256,payload_zlib "
            "FROM inspection_branch_import_archive ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(
            archive["archive_sha256"],
            __import__("hashlib").sha256(
                __import__("zlib").decompress(bytes(archive["payload_zlib"]))
            ).hexdigest(),
        )

    def test_archive_tamper_fails_closed_without_touching_authority(self):
        data = xlsx([branch_row("ARCH002")])
        preview = inspectionimport.preview_import(
            2, 20, "restaurant", "archive-tamper-0001", "tamper.xlsx", data,
        )
        inspectionimport.commit_import(
            2, 20, preview["import_id"], "restaurant",
        )
        archive = db.one(
            "SELECT id,payload_zlib FROM inspection_branch_import_archive"
        )
        damaged = bytearray(bytes(archive["payload_zlib"]))
        damaged[len(damaged) // 2] ^= 0x01
        db.execute(
            "UPDATE inspection_branch_import_archive SET payload_zlib=? "
            "WHERE id=?", (bytes(damaged), int(archive["id"])),
        )
        with self.assertRaises(inspectionimport.ImportContractError) as caught:
            inspectionimport.get_import(
                2, 20, preview["import_id"], "restaurant", limit=10,
            )
        self.assertEqual("IMPORT_ARCHIVE_INVALID", caught.exception.code)
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) n FROM store_branch WHERE store_code='ARCH002'"
        )["n"])


if __name__ == "__main__":
    unittest.main()
