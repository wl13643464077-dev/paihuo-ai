"""Keep the production 58–61 and offline v2 58–60 lineages losslessly merged.

Fixtures remove the other lineage's physical additions, not just its version
stamp. Production data must survive 61 -> 64; an already-used offline bundle
must also keep its payment orders, staff work and historical ledger names.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from app import db


PRODUCTION_TABLES = (
    "brand_package_fact", "brand_package", "team_run_member", "team_run",
    "task_activity_image_review", "task_activity_image",
)
V2_TABLES = (
    "user_branch", "pay_order", "staff_task_photo", "staff_task_event",
    "staff_task", "checklist_run", "checklist_template",
)
MERGED_STAMPS = {
    62: "member-branch-scope",
    63: "wxpay-native-pay-order",
    64: "staff-task-loop",
}


class V2ProductionSchemaMergeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        self._disconnect()
        db.DB_PATH = os.path.join(self.tmp.name, "merge.db")

    def tearDown(self):
        self._disconnect()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    @staticmethod
    def _disconnect():
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

    @staticmethod
    def _data_snapshot(connection):
        snapshot = {}
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name<>'schema_version' ORDER BY name"
        ):
            columns = tuple(row[1] for row in connection.execute(
                f'PRAGMA table_info("{table}")'
            ))
            projection = ",".join(f'"{column}"' for column in columns)
            rows = tuple(tuple(row) for row in connection.execute(
                f'SELECT {projection} FROM "{table}" ORDER BY rowid'
            ))
            snapshot[table] = (columns, rows)
        return snapshot

    def _assert_data_preserved(self, snapshot):
        connection = db.conn()
        for table, (columns, rows) in snapshot.items():
            projection = ",".join(f'"{column}"' for column in columns)
            actual = tuple(tuple(row) for row in connection.execute(
                f'SELECT {projection} FROM "{table}" ORDER BY rowid'
            ))
            self.assertEqual(rows, actual, table)

    def _assert_merged(self):
        connection = db.conn()
        self.assertEqual(64, db.LATEST_SCHEMA_VERSION)
        self.assertEqual(64, connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])
        self.assertEqual(MERGED_STAMPS, dict(connection.execute(
            "SELECT version,name FROM schema_version WHERE version>=62"
        )))
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertTrue(set(PRODUCTION_TABLES + V2_TABLES) <= tables)
        db._validate_migrated_database(connection)

    def _seed_content(self):
        def insert(table, values):
            # Some append-only audit tables intentionally have no updated_at;
            # db.insert adds it automatically for normal application tables.
            columns = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            db.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders})", tuple(values.values()))

        db.conn()
        insert("tenants", {"id": 9101, "name": "迁移测试商户", "balance": 4321.5})
        insert("users", {
            "id": 9101, "tenant_id": 9101, "username": "migration-owner",
            "password_hash": "fixture-not-a-real-password", "role": "owner",
            "phone": "13000000000",
        })
        insert("store_branch", {
            "id": 9101, "tenant_id": 9101, "industry_key": "coffee", "name": "历史门店",
        })
        insert("knowledge", {"tenant_id": 9101, "title": "历史资料", "content": "原文保持不变"})
        config = dict(db.one(
            "SELECT * FROM employee_role_config WHERE employee_catalog_version='2026.08.v4' LIMIT 1"
        ))
        bundle = db.one(
            "SELECT bundle_sha256 FROM employee_role_bundle_revision WHERE identity_ref=? "
            "AND config_revision=? AND config_sha256=?",
            (config["identity_ref"], config["config_revision"], config["config_sha256"]),
        )
        identity = {key: config[key] for key in (
            "employee_key", "employee_catalog_version", "employee_name_snapshot",
            "employee_dept_key", "employee_spec_sha256", "person_snapshot", "identity_scheme",
        )}
        identity.update({
            "employee_identity_ref": config["identity_ref"],
            "employee_config_revision": config["config_revision"],
            "employee_config_sha256": config["config_sha256"],
            "bundle_sha256": bundle["bundle_sha256"],
        })
        insert("task", {
            "id": 9101, "tenant_id": 9101, "emp_idx": config["idx"], **identity,
            "brief_json": '{"goal":"保持历史输入"}', "output_md": "保持历史输出",
            "status": "done", "thread_id": 9101, "request_key": "history-task",
        })
        thread_identity = {key: value for key, value in identity.items() if key not in (
            "employee_name_snapshot", "employee_dept_key", "employee_spec_sha256",
        )}
        insert("task_thread", {
            "id": 9101, "tenant_id": 9101, "emp_idx": config["idx"], **thread_identity,
            "root_task_id": 9101, "current_task_id": 9101, "accepted_task_id": 9101,
        })
        insert("brand_package", {
            "id": 9101, "tenant_id": 9101, "version": 1, "brand_name": "历史品牌",
            "status": "confirmed", "created_at": 1, "updated_at": 2,
        })
        insert("brand_package_fact", {
            "package_id": 9101, "fact_key": "slogan", "value": "历史口号",
            "source_kind": "manual", "source_captured_at": 1, "created_at": 1, "updated_at": 2,
        })
        insert("team_run", {
            "id": 9101, "tenant_id": 9101, "actor_id": 9101, "request_key": "history-team",
            "payload_sha256": "a" * 64, "query": "历史团队需求", "team_name": "历史小队",
            "mode": "auto", "depth": "professional", "leader_emp_idx": config["idx"],
            "status": "done", "summary_task_id": 9101, "created_at": 1, "updated_at": 2,
        })
        insert("team_run_member", {
            "team_run_id": 9101, "tenant_id": 9101, "position": 0, "emp_idx": config["idx"],
            "task_id": 9101, "created_at": 1, "updated_at": 2,
        })
        insert("task_activity_image", {
            "id": 9101, "tenant_id": 9101, "task_id": 9101, "group_key": "history-group",
            "file_path": "fixture/history.png", "status": "passed",
            "billing_op_key": "history-image-charge", "brand_package_id": 9101,
            "brand_version": 1, "created_at": 1,
        })
        insert("task_activity_image_review", {
            "tenant_id": 9101, "task_id": 9101, "image_id": 9101, "reviewer_id": 9101,
            "decision": "approve", "result_status": "passed", "logo_match": 1,
            "no_extra_claims": 1, "created_at": 2,
        })
        insert("user_branch", {"tenant_id": 9101, "user_id": 9101, "branch_id": 9101, "created_at": 1})
        insert("pay_order", {
            "tenant_id": 9101, "created_by": 9101, "plan_key": "fixture", "period_key": "year",
            "plan_name": "历史套餐", "period_label": "一年", "quoted_points": 100,
            "amount_fen": 20000, "out_trade_no": "historical-order", "transaction_id": "historical-transaction",
            "status": "paid", "expires_at": 100, "paid_at": 2,
        })
        insert("staff_task", {
            "id": 9101, "tenant_id": 9101, "branch_id": 9101, "assignee_user_id": 9101,
            "title": "历史店员任务", "status": "approved", "request_key": "history-staff",
        })
        insert("staff_task_event", {"tenant_id": 9101, "task_id": 9101, "kind": "approved", "note": "历史审核"})
        insert("staff_task_photo", {
            "tenant_id": 9101, "task_id": 9101, "storage_key": "fixture/staff.png", "sha256": "b" * 64,
            "mime_type": "image/png", "byte_size": 123, "received_at": 2,
        })
        insert("checklist_template", {"id": 9101, "tenant_id": 9101, "kind": "open", "name": "历史开店清单"})
        insert("checklist_run", {
            "tenant_id": 9101, "branch_id": 9101, "template_id": 9101,
            "run_date": "2026-09-25", "kind": "open", "status": "done",
        })

    def _prepare_lineage(self, version):
        self._seed_content()
        self._disconnect()
        with closing(sqlite3.connect(db.DB_PATH)) as connection:
            if version in (57, 61):
                for table in V2_TABLES:
                    connection.execute(f'DROP TABLE "{table}"')
                connection.execute("DROP INDEX idx_users_tenant_phone")
                connection.execute("ALTER TABLE users DROP COLUMN phone")
                connection.execute("ALTER TABLE inspection_action DROP COLUMN assignee_user_id")
                connection.execute("ALTER TABLE inspection_action DROP COLUMN close_reason")
            if version in (57, 60):
                for table in PRODUCTION_TABLES:
                    connection.execute(f'DROP TABLE "{table}"')
            connection.execute("DELETE FROM schema_version WHERE version>?", (version,))
            if version == 60:
                for old_version, name in zip((58, 59, 60), MERGED_STAMPS.values()):
                    connection.execute("UPDATE schema_version SET name=? WHERE version=?", (name, old_version))
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
            return self._data_snapshot(connection), tuple(connection.execute(
                "SELECT version,name,applied_at FROM schema_version ORDER BY version"
            ))

    def test_fresh_database_installs_both_feature_sets(self):
        self._assert_merged()
        self.assertEqual("reviewed-brand-knowledge-packages", db.one(
            "SELECT name FROM schema_version WHERE version=58"
        )["name"])

    def _assert_upgrade_and_restart(self, version):
        snapshot, ledger = self._prepare_lineage(version)
        self._assert_merged()
        self._assert_data_preserved(snapshot)
        self.assertEqual(ledger, tuple(tuple(row) for row in db.conn().execute(
            "SELECT version,name,applied_at FROM schema_version WHERE version<=? ORDER BY version", (version,)
        )))
        first_ledger = tuple(tuple(row) for row in db.conn().execute("SELECT * FROM schema_version ORDER BY version"))
        self._disconnect()
        self._assert_merged()
        self._assert_data_preserved(snapshot)
        self.assertEqual(first_ledger, tuple(tuple(row) for row in db.conn().execute("SELECT * FROM schema_version ORDER BY version")))

    def test_schema57_upgrade_keeps_existing_accounts_and_task_history(self):
        self._assert_upgrade_and_restart(57)

    def test_production61_upgrade_keeps_brand_team_artwork_and_task_history(self):
        self._assert_upgrade_and_restart(61)

    def test_offline60_upgrade_keeps_paid_orders_staff_data_and_original_ledger(self):
        self._assert_upgrade_and_restart(60)

    def test_failed_validation_rolls_back_all_new_tables_and_version(self):
        snapshot, ledger = self._prepare_lineage(61)
        with patch.object(db, "_validate_migrated_database", side_effect=RuntimeError("validation-test")):
            with self.assertRaisesRegex(RuntimeError, "validation-test"):
                db.conn()
        with closing(sqlite3.connect(db.DB_PATH)) as connection:
            self.assertEqual(61, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(ledger, tuple(connection.execute("SELECT * FROM schema_version ORDER BY version")))
            self.assertEqual(snapshot, self._data_snapshot(connection))
        self._assert_merged()
        self._assert_data_preserved(snapshot)

    def test_future_schema_is_rejected_without_database_mutation(self):
        self._assert_merged()
        self._disconnect()
        with closing(sqlite3.connect(db.DB_PATH)) as connection:
            connection.execute("PRAGMA user_version=65")
            connection.commit()
        with open(db.DB_PATH, "rb") as handle:
            before = handle.read()
        with self.assertRaisesRegex(RuntimeError, "拒绝降级启动"):
            db.conn()
        with open(db.DB_PATH, "rb") as handle:
            self.assertEqual(before, handle.read())


if __name__ == "__main__":
    unittest.main()
