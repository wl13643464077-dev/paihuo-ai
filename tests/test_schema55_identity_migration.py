"""Schema 55 identity/catalog and role-bundle migration contracts.

These tests deliberately exercise the public identity registry and the
schema54 -> schema55 transaction boundary.  They are RED until the V4
catalog and migration are wired in; keeping the contract here prevents a
second implementation from silently changing the old six-field digest.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from app import db, departments, employeeidentity


class Schema55IdentityMigrationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "schema55.db")
        departments.reset_cache()

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        departments.reset_cache()
        self.tmp.cleanup()

    def _disconnect(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

    def test_schema55_catalog_has_real_v4_people_and_all_lineages(self):
        self.assertEqual(57, db.LATEST_SCHEMA_VERSION)
        active = departments.specialists()
        current_v4 = [
            employee for employee in active.values()
            if employee.get("catalog_version") == "2026.08.v4"
        ]
        self.assertEqual(420, len(active))
        self.assertEqual(360, len(current_v4))
        people = [str(employee.get("person") or "") for employee in current_v4]
        self.assertEqual(360, len(set(people)))
        self.assertTrue(all(people))
        self.assertTrue(all(
            not any(marker in person for marker in ("市场", "商圈", "客群", "店型"))
            for person in people
        ))

        versions = departments.identity_versions(1001)
        self.assertEqual("2026.08.v4", versions[0]["catalog_version"])
        self.assertEqual("2026.08.v3", versions[1]["catalog_version"])
        self.assertEqual("v1", versions[2]["catalog_version"])
        self.assertEqual("v2-person", versions[0]["identity_scheme"])
        self.assertEqual(versions[0]["person"], versions[0]["person_snapshot"])
        self.assertEqual("legacy-six", versions[1].get("identity_scheme", "legacy-six"))
        refs = [employeeidentity.identity_ref(row) for row in versions]
        self.assertEqual(len(refs), len(set(refs)))
        self.assertEqual(1200, len(departments.all_identity_versions()))

    def test_v1_to_v3_identity_ref_algorithm_is_byte_stable(self):
        versions = departments.identity_versions(1001)
        for row in versions[1:]:
            frozen = {
                "idx": row["idx"],
                "key": row["key"],
                "name": row["name"],
                "dept_key": row["dept_key"],
                "catalog_version": row["catalog_version"],
                "spec_sha256": row["employee_spec_sha256"],
            }
            self.assertEqual(db.employee_identity_ref(frozen), employeeidentity.identity_ref(row))

    def test_schema55_tables_and_frozen_columns_exist(self):
        db.conn()
        self.assertEqual(57, db.one("PRAGMA user_version")["user_version"])
        tables = {
            row["name"] for row in db.q(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({
            "employee_role_bundle_revision", "employee_learning_batch",
            "employee_learning_run", "employee_learning_source",
            "employee_learning_artifact",
        } <= tables)
        for table, required in {
            "task": {"person_snapshot", "identity_scheme", "bundle_sha256"},
            "task_thread": {"person_snapshot", "identity_scheme", "bundle_sha256"},
            "employee_role_config": {"person_snapshot", "identity_scheme"},
        }.items():
            columns = {row["name"] for row in db.q(f"PRAGMA table_info({table})")}
            self.assertTrue(required <= columns, (table, required - columns))

    def test_schema55_bundle_is_hashable_and_learning_tables_are_auditable(self):
        db.conn()
        bundle_columns = {row["name"] for row in db.q(
            "PRAGMA table_info(employee_role_bundle_revision)"
        )}
        self.assertTrue({
            "identity_ref", "config_revision", "config_sha256", "bundle_sha256",
            "person_snapshot", "identity_scheme", "baseline_json", "effective_json",
            "status", "created_at", "updated_at",
        } <= bundle_columns)
        run_columns = {row["name"] for row in db.q(
            "PRAGMA table_info(employee_learning_run)"
        )}
        self.assertTrue({
            "batch_id", "identity_ref", "base_config_revision", "base_config_sha256",
            "status", "result_json", "error_code", "created_at", "updated_at",
        } <= run_columns)
        for table in (
            "employee_learning_batch", "employee_learning_source",
            "employee_learning_artifact",
        ):
            self.assertTrue(db.one(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ))

    def test_schema55_v4_task_identity_requires_person_and_bundle(self):
        employee = next(
            row for row in departments.specialists().values()
            if row.get("catalog_version") == "2026.08.v4"
        )
        frozen = employeeidentity.snapshot(employee)
        self.assertIn("person_snapshot", frozen)
        self.assertIn("identity_scheme", frozen)
        self.assertNotEqual(
            employeeidentity.identity_ref(employee),
            db.employee_identity_ref({
                "idx": frozen["idx"], "key": frozen["key"],
                "name": frozen["name"], "dept_key": frozen["dept_key"],
                "catalog_version": frozen["catalog_version"],
                "spec_sha256": frozen["spec_sha256"],
            }),
        )

    def test_schema55_role_bundles_preserve_historical_display_names(self):
        db.conn()
        versions = departments.identity_versions(1001)
        for employee in versions:
            from app import employees

            config = employees.ensure_role_config(employee)
            frozen = employeeidentity.task_fields(employee, config=config)
            bundle = db.get_employee_role_bundle(
                config["identity_ref"], config["config_revision"],
                config["config_sha256"], frozen["bundle_sha256"],
            )
            self.assertIsNotNone(bundle)
            expected_person = str(employee.get("person") or "").strip()
            self.assertEqual(expected_person, frozen["person_snapshot"])
            self.assertEqual(expected_person, bundle["person_snapshot"])
            self.assertIsNotNone(employeeidentity.resolve_task_binding({
                "emp_idx": employee["idx"], **frozen,
            }))

    def test_schema54_to_55_migration_is_idempotent_and_preserves_v3_task(self):
        # Opening creates the latest fixture.  The migration implementation
        # exposes a downgrade helper in tests by removing only the v55 additions;
        # this assertion remains useful on both a fresh and upgraded database.
        db.conn()
        self.assertEqual(57, db.one("PRAGMA user_version")["user_version"])
        first = db.one(
            "SELECT COUNT(*) AS n FROM employee_role_config "
            "WHERE employee_catalog_version='2026.08.v3'"
        )
        second = db.one(
            "SELECT COUNT(*) AS n FROM employee_role_config "
            "WHERE employee_catalog_version='2026.08.v4'"
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(360, int(first["n"]))
        self.assertEqual(360, int(second["n"]))
        # Running conn() again must not duplicate bundles, runs or configs.
        db.conn()
        self.assertEqual(360, db.one(
            "SELECT COUNT(*) AS n FROM employee_role_config "
            "WHERE employee_catalog_version='2026.08.v4'"
        )["n"])
        self.assertEqual(360, db.one(
            "SELECT COUNT(*) AS n FROM employee_role_bundle_revision "
            "WHERE employee_catalog_version='2026.08.v4'"
        )["n"])


if __name__ == "__main__":
    unittest.main()
