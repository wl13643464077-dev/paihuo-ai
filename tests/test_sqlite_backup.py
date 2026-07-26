"""SQLite 在线一致备份、校验、恢复演练与保留策略测试。"""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from deploy.backup_db import BackupError, backup_database, main as backup_main
from deploy.verify_backup import (
    VerificationError,
    main as verify_main,
    restore_drill,
    verify_database,
)


UTC = timezone.utc


class SqliteBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "contentcrew.db"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        self.writer = sqlite3.connect(self.source)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.writer.execute(
            "CREATE TABLE jobs("
            "id INTEGER PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.writer.execute(
            "INSERT INTO jobs(status, payload) VALUES ('done', 'baseline')"
        )
        self.writer.commit()
        self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.reader: sqlite3.Connection | None = None

    def tearDown(self) -> None:
        if self.reader is not None:
            self.reader.close()
        self.writer.close()
        self.temp_dir.cleanup()

    def _leave_committed_rows_in_wal(self) -> None:
        # 固定一个旧快照，确保新提交仍留在 WAL，模拟线上持续写入。
        self.reader = sqlite3.connect(self.source)
        self.reader.execute("BEGIN")
        self.reader.execute("SELECT COUNT(*) FROM jobs").fetchone()
        self.writer.executemany(
            "INSERT INTO jobs(status, payload) VALUES (?, ?)",
            [
                ("running", "wal-row-1"),
                ("done", "wal-row-2"),
                ("failed", "wal-row-3"),
            ],
        )
        self.writer.commit()
        wal_path = Path(f"{self.source}-wal")
        self.assertTrue(wal_path.exists())
        self.assertGreater(wal_path.stat().st_size, 0)

    def test_live_wal_rows_are_in_consistent_backup_and_restore_drill(self) -> None:
        self._leave_committed_rows_in_wal()

        report = backup_database(
            self.source,
            self.backup_dir,
            keep_days=14,
            keep_minimum=3,
            run_restore_drill=True,
            now=datetime(2026, 7, 25, 12, 30, 45, tzinfo=UTC),
        )

        backup_path = Path(report["backup_path"])
        self.assertEqual(backup_path.name, "db-2026-07-25T123045Z.db")
        self.assertTrue(backup_path.is_file())
        self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
        with contextlib.closing(sqlite3.connect(backup_path)) as restored:
            rows = restored.execute(
                "SELECT status, payload FROM jobs ORDER BY id"
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("done", "baseline"),
                ("running", "wal-row-1"),
                ("done", "wal-row-2"),
                ("failed", "wal-row-3"),
            ],
        )
        self.assertEqual(report["integrity_check"], "ok")
        self.assertTrue(report["restore_drill"]["ok"])
        self.assertFalse(list(self.backup_dir.glob("*.partial-*")))

    def test_verification_failure_never_replaces_or_prunes_old_backups(self) -> None:
        old_backups = [
            self.backup_dir / "db-2026-05-01T000000Z.db",
            self.backup_dir / "db-2026-06-01T000000Z.db",
            self.backup_dir / "db-2026-07-01T000000Z.db",
        ]
        for index, old_backup in enumerate(old_backups):
            old_backup.write_bytes(f"known-old-backup-{index}".encode())

        with patch(
            "deploy.backup_db.verify_database",
            side_effect=VerificationError("injected integrity failure"),
        ):
            with self.assertRaisesRegex(BackupError, "integrity failure"):
                backup_database(
                    self.source,
                    self.backup_dir,
                    keep_days=0,
                    keep_minimum=1,
                    run_restore_drill=False,
                    now=datetime(2026, 7, 25, 12, 31, tzinfo=UTC),
                )

        for index, old_backup in enumerate(old_backups):
            self.assertEqual(
                old_backup.read_bytes(),
                f"known-old-backup-{index}".encode(),
            )
        self.assertFalse(
            (self.backup_dir / "db-2026-07-25T123100Z.db").exists()
        )
        self.assertFalse(list(self.backup_dir.glob("*.partial-*")))

    def test_existing_destination_is_never_overwritten(self) -> None:
        destination = self.backup_dir / "manual.db"
        destination.write_bytes(b"do-not-overwrite")

        with self.assertRaisesRegex(BackupError, "already exists|已存在"):
            backup_database(
                self.source,
                self.backup_dir,
                output_path=destination,
                run_restore_drill=False,
            )

        self.assertEqual(destination.read_bytes(), b"do-not-overwrite")

    def test_lock_conflict_fails_without_creating_partial_backup(self) -> None:
        lock_path = self.backup_dir / ".backup.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(BackupError, "already running"):
                backup_database(
                    self.source,
                    self.backup_dir,
                    run_restore_drill=False,
                )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.assertFalse(list(self.backup_dir.glob("*.partial-*")))
        self.assertFalse(list(self.backup_dir.glob("db-*.db")))

    def test_backup_cli_returns_nonzero_for_missing_source(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = backup_main(
                [
                    "--database",
                    str(self.root / "missing.db"),
                    "--backup-dir",
                    str(self.backup_dir),
                    "--restore-drill",
                ]
            )
        self.assertNotEqual(exit_code, 0)
        self.assertIn("backup failed", stderr.getvalue().lower())

    def test_next_locked_run_removes_only_managed_stale_partials(self) -> None:
        stale = self.backup_dir / ".paihuo-backup.partial-deadbeef.db"
        stale_sidecars = [
            Path(f"{stale}-journal"),
            Path(f"{stale}-wal"),
            Path(f"{stale}-shm"),
        ]
        stale.write_bytes(b"partial database")
        for sidecar in stale_sidecars:
            sidecar.write_bytes(b"partial sidecar")
        unrelated = self.backup_dir / ".manual.db.partial-deadbeef.db"
        unrelated.write_bytes(b"must stay")

        report = backup_database(
            self.source,
            self.backup_dir,
            output_path=self.backup_dir / "manual-new.db",
            run_restore_drill=False,
            now=datetime(2026, 7, 25, 12, 33, tzinfo=UTC),
        )

        self.assertFalse(stale.exists())
        self.assertTrue(all(not sidecar.exists() for sidecar in stale_sidecars))
        self.assertEqual(unrelated.read_bytes(), b"must stay")
        self.assertEqual(len(report["removed_stale_partials"]), 4)

    def test_retention_only_deletes_managed_old_files_and_keeps_minimum(self) -> None:
        managed = [
            self.backup_dir / "db-2026-01-01T000000Z.db",
            self.backup_dir / "db-2026-02-01T000000Z.db",
            self.backup_dir / "db-2026-03-01T000000Z.db",
            self.backup_dir / "db-2026-04-01T000000Z.db",
        ]
        for index, path in enumerate(managed):
            path.write_bytes(f"old-{index}".encode())
        manual = self.backup_dir / "contentcrew-pre-release-20260724.db"
        manual.write_bytes(b"manual")
        unrelated = self.backup_dir / "notes.txt"
        unrelated.write_text("keep me", encoding="utf-8")

        report = backup_database(
            self.source,
            self.backup_dir,
            keep_days=0,
            keep_minimum=2,
            run_restore_drill=False,
            now=datetime(2026, 7, 25, 12, 32, tzinfo=UTC),
        )

        remaining_managed = sorted(
            path.name
            for path in self.backup_dir.iterdir()
            if path.name.startswith("db-") and path.suffix == ".db"
        )
        self.assertEqual(
            remaining_managed,
            ["db-2026-04-01T000000Z.db", "db-2026-07-25T123200Z.db"],
        )
        self.assertTrue(manual.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(len(report["pruned_backups"]), 3)


class SqliteVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.backup = self.root / "known-good.db"
        with contextlib.closing(sqlite3.connect(self.backup)) as connection:
            connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT)")
            connection.executemany(
                "INSERT INTO items(name) VALUES (?)", [("一",), ("二",), ("三",)]
            )
            connection.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_read_only_verification_creates_no_wal_or_shm_sidecars(self) -> None:
        report = verify_database(self.backup)

        self.assertEqual(report["integrity_check"], "ok")
        self.assertEqual(report["table_counts"], {"items": 3})
        self.assertFalse(Path(f"{self.backup}-wal").exists())
        self.assertFalse(Path(f"{self.backup}-shm").exists())

    def test_nonempty_wal_is_rejected_as_not_a_standalone_backup(self) -> None:
        wal_path = Path(f"{self.backup}-wal")
        wal_path.write_bytes(b"committed data may live here")

        with self.assertRaisesRegex(VerificationError, "not standalone"):
            verify_database(self.backup)

    def test_nonempty_rollback_journal_is_rejected_even_if_database_is_valid(self) -> None:
        journal_path = Path(f"{self.backup}-journal")
        journal_path.write_bytes(b"uncommitted pages may live here")

        with self.assertRaisesRegex(VerificationError, "rollback journal"):
            verify_database(self.backup)

    def test_symlink_cannot_bypass_actual_database_sidecar_checks(self) -> None:
        alias = self.root / "alias.db"
        alias.symlink_to(self.backup)
        Path(f"{self.backup}-journal").write_bytes(b"hot journal")

        with self.assertRaisesRegex(VerificationError, "symlink"):
            verify_database(alias)

    def test_restore_drill_uses_temporary_path_and_cleans_it(self) -> None:
        drill_parent = self.root / "drills"
        drill_parent.mkdir()

        report = restore_drill(self.backup, restore_dir=drill_parent)

        self.assertTrue(report["ok"])
        self.assertEqual(report["source_table_counts"], {"items": 3})
        self.assertEqual(report["restored_table_counts"], {"items": 3})
        self.assertEqual(list(drill_parent.iterdir()), [])

    def test_explicit_restore_target_is_verified_and_never_overwritten(self) -> None:
        restore_target = self.root / "restore-stage" / "contentcrew.db"
        report = restore_drill(self.backup, restore_to=restore_target)
        self.assertTrue(report["ok"])
        self.assertTrue(restore_target.exists())
        with contextlib.closing(sqlite3.connect(restore_target)) as restored:
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM items").fetchone()[0], 3)

        original_bytes = restore_target.read_bytes()
        with self.assertRaisesRegex(VerificationError, "already exists|已存在"):
            restore_drill(self.backup, restore_to=restore_target)
        self.assertEqual(restore_target.read_bytes(), original_bytes)

    def test_corrupt_backup_cli_fails_without_sidecars_or_restore_artifact(self) -> None:
        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(os.urandom(256))
        restore_parent = self.root / "failed-drill"
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = verify_main(
                [str(corrupt), "--restore-drill", "--restore-dir", str(restore_parent)]
            )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("failed", stderr.getvalue().lower())
        self.assertFalse(Path(f"{corrupt}-wal").exists())
        self.assertFalse(Path(f"{corrupt}-shm").exists())
        self.assertFalse(restore_parent.exists())


if __name__ == "__main__":
    unittest.main()
