"""Cross-process safety contracts for SQLite schema initialization."""

import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from app import db


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SCHEMA_VERSION = 56
EXPECTED_SCHEMA_LEDGER_NAME = "meeting-agent-team-relay"

INITIALIZE_WORKER = textwrap.dedent(
    """
    import os
    import sys
    import time
    from app import db

    db.DB_PATH = sys.argv[1]
    db._conn = None
    db._conn_path = None
    gate = sys.argv[2]
    deadline = time.monotonic() + 30
    while not os.path.exists(gate):
        if time.monotonic() >= deadline:
            raise RuntimeError("initializer gate timed out")
        time.sleep(0.005)
    connection = db.conn()
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    ledger = connection.execute(
        "SELECT COALESCE(MAX(version),0) FROM schema_version"
    ).fetchone()[0]
    check = connection.execute("PRAGMA quick_check").fetchone()[0]
    if (version, ledger, check) != (db.LATEST_SCHEMA_VERSION,
                                    db.LATEST_SCHEMA_VERSION, "ok"):
        raise RuntimeError((version, ledger, check))
    """
)

CRASH_DURING_MIGRATION_WORKER = textwrap.dedent(
    """
    import os
    import sys
    from app import db

    db.DB_PATH = sys.argv[1]
    db._conn = None
    db._conn_path = None

    # The first post-DDL validation is deliberately after every schema-52
    # table/index has been prepared.  A hard exit here proves that the next
    # process sees either v51 or v52, never the formerly committed half-state.
    def crash_after_ddl(_connection):
        os._exit(91)

    db._validate_migrated_database = crash_after_ddl
    db.conn()
    raise RuntimeError("migration crash hook was not reached")
    """
)


class SchemaMigrationConcurrencyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "shared.db")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _worker_env():
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(REPO_ROOT) if not existing
            else str(REPO_ROOT) + os.pathsep + existing
        )
        return env

    def _run_initializers(self, count=4):
        gate = os.path.join(self.tmp.name, f"gate-{time.time_ns()}")
        processes = [
            subprocess.Popen(
                [sys.executable, "-B", "-c", INITIALIZE_WORKER,
                 self.db_path, gate],
                cwd=REPO_ROOT,
                env=self._worker_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(count)
        ]
        Path(gate).write_text("go", encoding="ascii")
        failures = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=90)
            if process.returncode != 0:
                failures.append((process.returncode, stdout, stderr))
        self.assertEqual([], failures)

    def _assert_latest_and_healthy(self):
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(EXPECTED_SCHEMA_VERSION, db.LATEST_SCHEMA_VERSION)
            self.assertEqual(
                EXPECTED_SCHEMA_VERSION,
                connection.execute("PRAGMA user_version").fetchone()[0],
            )
            self.assertEqual(
                (EXPECTED_SCHEMA_VERSION, EXPECTED_SCHEMA_LEDGER_NAME, 1),
                connection.execute(
                    "SELECT version,name,COUNT(*) FROM schema_version "
                    "WHERE version=? GROUP BY version,name",
                    (EXPECTED_SCHEMA_VERSION,),
                ).fetchone(),
            )
            self.assertEqual(
                "ok", connection.execute("PRAGMA quick_check").fetchone()[0]
            )
            override_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(inspection_standard_override)"
                )
            }
            self.assertTrue({
                "id", "tenant_id", "industry_key", "scope_kind",
                "scope_key", "item_code", "patch_json", "row_version",
                "active", "created_by", "created_at", "updated_at",
            } <= override_columns)
            self.assertIn(
                "idx_inspection_standard_override_scope",
                {row[1] for row in connection.execute(
                    "SELECT type,name FROM sqlite_master WHERE type='index'"
                )},
            )
        finally:
            connection.close()

    def _prepare_real_schema51(self):
        self._run_initializers(count=1)
        connection = sqlite3.connect(self.db_path)
        try:
            for index in (
                "idx_store_branch_code",
                "idx_inspection_branch_import_request",
                "idx_inspection_branch_import_source",
                "idx_inspection_import_row",
                "idx_inspection_business_value_natural",
                "idx_inspection_business_value_period",
            ):
                connection.execute(f"DROP INDEX IF EXISTS {index}")
            for table in (
                "inspection_business_value",
                "inspection_branch_import_row",
                "inspection_branch_import",
                "inspection_standard_override",
            ):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            for table, columns in {
                "inspection_visit": (
                    "template_key", "template_version",
                    "template_snapshot_json", "observations_json",
                ),
                "inspection_photo": ("capture_slot", "item_code"),
                "store_branch": (
                    "store_code", "province", "city", "district",
                    "manager_name", "manager_employee_no", "manager_phone",
                    "store_type", "opened_on", "area_sqm", "seat_count",
                    "longitude", "latitude", "remark", "row_version",
                ),
            }.items():
                present = {
                    row[1] for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    )
                }
                for column in columns:
                    if column in present:
                        connection.execute(
                            f"ALTER TABLE {table} DROP COLUMN {column}"
                        )
            connection.execute("DELETE FROM schema_version WHERE version>=52")
            connection.execute("PRAGMA user_version=51")
            connection.commit()
        finally:
            connection.close()

    def test_independent_processes_serialize_empty_database_initialization(self):
        self._run_initializers()
        self._assert_latest_and_healthy()

        lock_path = db._migration_lock_path(self.db_path)
        lock_stat = os.lstat(lock_path)
        self.assertTrue(stat.S_ISREG(lock_stat.st_mode))
        self.assertEqual(1, lock_stat.st_nlink)
        self.assertEqual(0, stat.S_IMODE(lock_stat.st_mode) & 0o077)

    def test_independent_processes_serialize_real_51_to_52_upgrade(self):
        self._prepare_real_schema51()
        self._run_initializers()
        self._assert_latest_and_healthy()

        connection = sqlite3.connect(self.db_path)
        try:
            self.assertIn(
                "row_version",
                {row[1] for row in connection.execute(
                    "PRAGMA table_info(store_branch)"
                )},
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name='inspection_branch_import'"
                ).fetchone()[0],
            )
        finally:
            connection.close()

    def test_hard_exit_rolls_back_whole_migration_and_next_process_recovers(self):
        self._prepare_real_schema51()
        crashed = subprocess.run(
            [sys.executable, "-B", "-c", CRASH_DURING_MIGRATION_WORKER,
             self.db_path],
            cwd=REPO_ROOT,
            env=self._worker_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(91, crashed.returncode, crashed.stderr)

        # Opening the file performs SQLite's own crash recovery.  The schema
        # marker and the structural changes must still describe the same v51.
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(51, connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0])
            self.assertNotIn(
                "store_code",
                {row[1] for row in connection.execute(
                    "PRAGMA table_info(store_branch)"
                )},
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name='inspection_branch_import'"
                ).fetchone()[0],
            )
            self.assertEqual(
                "ok", connection.execute("PRAGMA quick_check").fetchone()[0]
            )
        finally:
            connection.close()

        self._run_initializers(count=1)
        self._assert_latest_and_healthy()

    def test_symlink_migration_lock_is_rejected_before_database_creation(self):
        lock_path = db._migration_lock_path(self.db_path)
        target = os.path.join(self.tmp.name, "attacker-controlled")
        Path(target).write_text("not a lock", encoding="ascii")
        os.symlink(target, lock_path)

        with self.assertRaisesRegex(RuntimeError, "migration lock"):
            with db._migration_process_lock(self.db_path):
                pass
        self.assertFalse(os.path.exists(self.db_path))

    def test_hardlinked_migration_lock_is_rejected_before_database_creation(self):
        lock_path = db._migration_lock_path(self.db_path)
        Path(lock_path).write_bytes(b"")
        os.chmod(lock_path, 0o600)
        os.link(lock_path, os.path.join(self.tmp.name, "second-lock-name"))

        with self.assertRaisesRegex(RuntimeError, "migration lock"):
            with db._migration_process_lock(self.db_path):
                pass
        self.assertFalse(os.path.exists(self.db_path))

    def test_override_schema_enforces_scope_and_natural_key_contracts(self):
        self._run_initializers(count=1)
        connection = sqlite3.connect(self.db_path)
        try:
            index = next(
                row for row in connection.execute(
                    "PRAGMA index_list(inspection_standard_override)"
                )
                if row[1] == "idx_inspection_standard_override_scope"
            )
            self.assertEqual(0, index[2])
            self.assertEqual(
                ["tenant_id", "industry_key", "active", "scope_kind",
                 "scope_key"],
                [row[2] for row in connection.execute(
                    "PRAGMA index_info(idx_inspection_standard_override_scope)"
                )],
            )
            values = (
                2, "restaurant", "tenant", "", "FOOD-001", "{}", 20, 1.0,
                1.0,
            )
            connection.execute(
                "INSERT INTO inspection_standard_override(tenant_id,industry_key,"
                "scope_kind,scope_key,item_code,patch_json,created_by,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO inspection_standard_override(tenant_id,"
                    "industry_key,scope_kind,scope_key,item_code,patch_json,"
                    "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    values,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO inspection_standard_override(tenant_id,"
                    "industry_key,scope_kind,scope_key,item_code,patch_json,"
                    "created_by,created_at,updated_at) VALUES(2,'restaurant',"
                    "'country','', 'FOOD-002','{}',20,1,1)"
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
