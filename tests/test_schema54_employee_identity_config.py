"""Schema 55: person slots, immutable role bundles, and historical replay."""

import asyncio
import json
import os
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import (
    auth, billing, db, departments, employeeidentity, employees, main, meeting,
    providers, taskrunner,
)
from app.skills import registry


class Schema54DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "schema54.db")
        db.conn()

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _disconnect(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

    def _downgrade_fixture_to_schema53(self):
        """Turn the fresh fixture into the exact pre-v54 physical shape."""
        path = db.DB_PATH
        self._disconnect()
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP INDEX IF EXISTS idx_task_employee_identity")
            connection.execute("DROP TABLE employee_role_config_history")
            connection.execute("DROP TABLE employee_role_config")
            connection.execute("DROP TABLE employee_slot")
            # The fresh Schema55 fixture has already materialized current V4
            # bundles/learning tables.  Remove those post-v53 artifacts too;
            # otherwise the migration under test encounters pre-existing
            # bundle rows and cannot exercise a clean 53 -> 55 upgrade.
            for table in (
                "employee_role_bundle_revision",
                "employee_learning_artifact",
                "employee_learning_source",
                "employee_learning_run",
                "employee_learning_batch",
            ):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            for table in ("task", "task_thread"):
                for column in (
                    "employee_identity_ref", "employee_config_revision",
                    "employee_config_sha256",
                    "person_snapshot", "identity_scheme", "bundle_sha256",
                ):
                    try:
                        connection.execute(
                            f"ALTER TABLE {table} DROP COLUMN {column}"
                        )
                    except sqlite3.OperationalError as exc:
                        if "no such column" not in str(exc).lower():
                            raise
            # A Schema55 fixture carries both v54 and v55 ledger stamps.  To
            # exercise the complete 53 -> 55 upgrade, remove every post-v53
            # stamp rather than leaving v55 to short-circuit the migration.
            connection.execute("DELETE FROM schema_version WHERE version>=54")
            connection.execute("PRAGMA user_version=53")
            connection.commit()
        finally:
            connection.close()

    def test_schema54_tables_and_frozen_columns_exist(self):
        self.assertEqual(55, db.LATEST_SCHEMA_VERSION)
        self.assertEqual(55, db.one("PRAGMA user_version")["user_version"])
        tables = {
            row["name"] for row in db.q(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({
            "employee_slot", "employee_role_config",
            "employee_role_config_history",
        } <= tables)
        for table, required in {
            "task": {
                "employee_identity_ref", "employee_config_revision",
                "employee_config_sha256",
            },
            "task_thread": {
                "employee_identity_ref", "employee_config_revision",
                "employee_config_sha256",
            },
        }.items():
            columns = {row["name"] for row in db.q(f"PRAGMA table_info({table})")}
            self.assertTrue(required <= columns)

    def test_schema53_migration_freezes_v1_v2_tasks_threads_and_meetings(self):
        v1 = db._frozen_v1_employee_identities()[1001]
        v2 = db._frozen_v2_employee_identities()[20001]
        for idx, prompt in ((1001, "V1历史工作法"), (20001, "V2历史工作法")):
            db.execute(
                "INSERT INTO employee_config("
                "idx,prompt_template,skills_json,settings_json,caps_off_json,"
                "model_text,enabled,created_at,updated_at) "
                "VALUES(?,?,?, '{}','[]',?,1,1,1)",
                (idx, prompt, '[{"title":"历史技能"}]', f"model-{idx}"),
            )
        task_ids = []
        for frozen in (v1, v2):
            task_ids.append(db.insert("task", {
                "tenant_id": 1,
                "emp_idx": frozen["idx"],
                "employee_key": frozen["key"],
                "employee_catalog_version": frozen["catalog_version"],
                "employee_name_snapshot": frozen["name"],
                "employee_dept_key": frozen["dept_key"],
                "employee_spec_sha256": frozen["spec_sha256"],
                "brief_json": "{}",
            }))
        thread_id = db.insert("task_thread", {
            "tenant_id": 1,
            "emp_idx": v1["idx"],
            "employee_key": v1["key"],
            "employee_catalog_version": v1["catalog_version"],
            "root_task_id": task_ids[0],
            "current_task_id": task_ids[0],
        })
        meeting_id = db.insert("meeting", {
            "tenant_id": 1,
            "question": "复核历史岗位",
            "emp_idxs_json": json.dumps([v1["idx"]]),
            "member_snapshot_json": json.dumps([v1], ensure_ascii=False),
        })

        self._downgrade_fixture_to_schema53()
        db.conn()

        self.assertEqual(55, db.one("PRAGMA user_version")["user_version"])
        for frozen, prompt, task_id in (
            (v1, "V1历史工作法", task_ids[0]),
            (v2, "V2历史工作法", task_ids[1]),
        ):
            expected_ref = db.employee_identity_ref(frozen)
            role = db.one(
                "SELECT * FROM employee_role_config WHERE identity_ref=?",
                (expected_ref,),
            )
            self.assertIsNotNone(role)
            self.assertEqual(prompt, role["prompt_template"])
            self.assertIsNotNone(role["archived_at"])
            task = db.one("SELECT * FROM task WHERE id=?", (task_id,))
            self.assertEqual(expected_ref, task["employee_identity_ref"])
            self.assertEqual(role["config_revision"], task["employee_config_revision"])
            self.assertEqual(role["config_sha256"], task["employee_config_sha256"])
        # Reusing slot 1001 across V1/V3/V4 must retain three independent role
        # identities; the V1 custom prompt cannot leak into either current
        # generation.
        rows_1001 = db.q(
            "SELECT employee_catalog_version,prompt_template "
            "FROM employee_role_config WHERE idx=1001"
        )
        self.assertEqual(
            {"v1", "2026.08.v3", "2026.08.v4"},
            {row["employee_catalog_version"] for row in rows_1001},
        )
        self.assertEqual(
            "V1历史工作法",
            next(row["prompt_template"] for row in rows_1001
                 if row["employee_catalog_version"] == "v1"),
        )
        self.assertEqual(
            {None},
            {row["prompt_template"] for row in rows_1001
             if row["employee_catalog_version"] != "v1"},
        )
        thread = db.one("SELECT * FROM task_thread WHERE id=?", (thread_id,))
        root = db.one("SELECT * FROM task WHERE id=?", (task_ids[0],))
        self.assertEqual(root["employee_identity_ref"], thread["employee_identity_ref"])
        self.assertEqual(root["employee_config_sha256"], thread["employee_config_sha256"])
        meeting_row = db.one("SELECT * FROM meeting WHERE id=?", (meeting_id,))
        member = db.jloads(meeting_row["member_snapshot_json"], [])[0]
        self.assertEqual(root["employee_identity_ref"], member["identity_ref"])
        self.assertEqual(root["employee_config_sha256"], member["config_sha256"])

    def test_schema53_full_360_v1_and_60_v2_archive_migrates_exactly(self):
        v1 = {
            idx: frozen
            for idx, frozen in db._frozen_v1_employee_identities().items()
            if idx in set().union(*db._SCHEMA54_V1_DECISION_RANGES.values())
        }
        v2 = db._frozen_v2_employee_identities()
        self.assertEqual((360, 60), (len(v1), len(v2)))
        for frozen in (*v1.values(), *v2.values()):
            db.insert("task", {
                "tenant_id": 1,
                "emp_idx": frozen["idx"],
                "employee_key": frozen["key"],
                "employee_catalog_version": frozen["catalog_version"],
                "employee_name_snapshot": frozen["name"],
                "employee_dept_key": frozen["dept_key"],
                "employee_spec_sha256": frozen["spec_sha256"],
                "brief_json": "{}",
            })
        self._downgrade_fixture_to_schema53()
        db.conn()
        self.assertEqual(55, db.one("PRAGMA user_version")["user_version"])
        self.assertEqual(420, db.one(
            "SELECT COUNT(*) AS n FROM task WHERE length(employee_identity_ref)=64 "
            "AND employee_config_revision=1 AND length(employee_config_sha256)=64"
        )["n"])
        self.assertEqual(420, db.one(
            "SELECT COUNT(*) AS n FROM employee_role_config "
            "WHERE archived_at IS NOT NULL"
        )["n"])

    def test_schema53_unknown_config_idx_rolls_back_schema54_transaction(self):
        db.execute(
            "INSERT INTO employee_config(idx,prompt_template,created_at,updated_at) "
            "VALUES(20007,'歧义且不属于V2的配置',1,1)"
        )
        path = db.DB_PATH
        self._downgrade_fixture_to_schema53()
        with self.assertRaisesRegex(RuntimeError, "无法判定旧员工配置归属"):
            db.conn()
        self._disconnect()
        connection = sqlite3.connect(path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            task_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(task)")
            }
        finally:
            connection.close()
        self.assertEqual(53, version)
        self.assertNotIn("employee_role_config", tables)
        self.assertNotIn("employee_identity_ref", task_columns)

    def _assert_schema53_migration_rolled_back(self):
        path = db.DB_PATH
        self._disconnect()
        connection = sqlite3.connect(path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            task_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(task)")
            }
        finally:
            connection.close()
        self.assertEqual(53, version)
        self.assertNotIn("employee_role_config", tables)
        self.assertNotIn("employee_identity_ref", task_columns)

    def test_schema53_migration_rejects_missing_or_bad_v1_seed_transactionally(self):
        for mode in ("missing", "bad-json", "duplicate-idx"):
            with self.subTest(mode=mode):
                self.tearDown()
                self.setUp()
                seed_root = os.path.join(self.tmp.name, "v1-seed")
                if mode != "missing":
                    os.makedirs(seed_root)
                if mode == "bad-json":
                    with open(
                        os.path.join(seed_root, "bad.json"), "w", encoding="utf-8"
                    ) as handle:
                        handle.write("{")
                elif mode == "duplicate-idx":
                    for name in ("a", "b"):
                        with open(
                            os.path.join(seed_root, f"{name}.json"),
                            "w",
                            encoding="utf-8",
                        ) as handle:
                            json.dump({
                                "key": name,
                                "employees": [{
                                    "idx": 1001,
                                    "key": f"{name}.role",
                                    "name": f"{name}岗位",
                                }],
                            }, handle, ensure_ascii=False)
                self._downgrade_fixture_to_schema53()
                with patch.object(db, "_v1_seed_directory", return_value=seed_root):
                    with self.assertRaisesRegex(RuntimeError, "V1.*(seed|种子|目录|重复|解析)"):
                        db.conn()
                self._assert_schema53_migration_rolled_back()

    def test_schema53_known_v1_rows_require_an_exact_seed(self):
        v1 = db._frozen_v1_employee_identities()[1001]
        db.execute(
            "INSERT INTO employee_config(idx,prompt_template,created_at,updated_at) "
            "VALUES(1001,'必须精确归属',1,1)"
        )
        task_frozen = {**v1, "key": v1["key"] + ".tampered"}
        db.insert("task", {
            "tenant_id": 1,
            "emp_idx": 1001,
            "employee_key": task_frozen["key"],
            "employee_catalog_version": task_frozen["catalog_version"],
            "employee_name_snapshot": task_frozen["name"],
            "employee_dept_key": task_frozen["dept_key"],
            "employee_spec_sha256": task_frozen["spec_sha256"],
            "brief_json": "{}",
        })
        db.insert("meeting", {
            "tenant_id": 1,
            "question": "精确身份审计",
            "emp_idxs_json": "[1001]",
            "member_snapshot_json": json.dumps([v1], ensure_ascii=False),
        })
        self._downgrade_fixture_to_schema53()
        with self.assertRaisesRegex(RuntimeError, "V1.*精确"):
            db.conn()
        self._assert_schema53_migration_rolled_back()

    def test_schema53_known_v1_config_without_its_seed_rolls_back(self):
        db.execute(
            "INSERT INTO employee_config(idx,prompt_template,created_at,updated_at) "
            "VALUES(1001,'不得猜测归属',1,1)"
        )
        seed_root = os.path.join(self.tmp.name, "partial-v1-seed")
        os.makedirs(seed_root)
        with open(
            os.path.join(seed_root, "partial.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump({
                "key": "partial",
                "employees": [{
                    "idx": 1002, "key": "partial.role", "name": "其他岗位",
                }],
            }, handle, ensure_ascii=False)
        self._downgrade_fixture_to_schema53()
        with patch.object(db, "_v1_seed_directory", return_value=seed_root):
            with self.assertRaisesRegex(RuntimeError, "V1.*精确"):
                db.conn()
        self._assert_schema53_migration_rolled_back()

    def test_schema53_known_v1_meeting_requires_an_exact_seed(self):
        v1 = db._frozen_v1_employee_identities()[1001]
        drifted = {**v1, "name": v1["name"] + "被篡改"}
        db.insert("meeting", {
            "tenant_id": 1,
            "question": "会议身份必须精确",
            "emp_idxs_json": "[1001]",
            "member_snapshot_json": json.dumps([drifted], ensure_ascii=False),
        })
        self._downgrade_fixture_to_schema53()
        with self.assertRaisesRegex(RuntimeError, "V1.*精确"):
            db.conn()
        self._assert_schema53_migration_rolled_back()

    def test_schema53_complete_archives_are_required_without_any_reference(self):
        """A one-row V1/V2 seed cannot bless a schema53 database by disuse."""
        for catalog in ("v1", "v2"):
            with self.subTest(catalog=catalog):
                self.tearDown()
                self.setUp()
                seed_root = os.path.join(self.tmp.name, f"one-row-{catalog}")
                os.makedirs(seed_root)
                if catalog == "v1":
                    payload = {
                        "key": "tea_coffee",
                        "employees": [{
                            "idx": 1001, "key": "only.one", "name": "仅一人",
                        }],
                    }
                    patcher = patch.object(
                        db, "_v1_seed_directory", return_value=seed_root,
                    )
                else:
                    payload = {
                        "key": "tea_coffee",
                        "catalog_version": "2026.08.v2",
                        "employees": [{
                            "idx": 20001, "key": "only.v2", "name": "仅一人",
                        }],
                    }
                    patcher = patch.object(
                        db, "_v2_seed_directory", return_value=seed_root,
                    )
                with open(
                    os.path.join(seed_root, "tea_coffee.json"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                self._downgrade_fixture_to_schema53()
                with patcher:
                    with self.assertRaisesRegex(
                        RuntimeError, f"{catalog.upper()}.*(精确|完整|覆盖)"
                    ):
                        db.conn()
                self._assert_schema53_migration_rolled_back()

    def test_schema53_tampered_v2_and_unknown_task_fail_closed(self):
        attacks = []
        v2 = db._frozen_v2_employee_identities()[20001]
        attacks.append({**v2, "name": v2["name"] + "-篡改"})
        attacks.append({
            "idx": 55555,
            "key": "unknown.55555",
            "name": "未知员工",
            "dept_key": "unknown",
            "catalog_version": "unknown",
            "spec_sha256": "5" * 64,
        })
        for frozen in attacks:
            with self.subTest(idx=frozen["idx"]):
                self.tearDown()
                self.setUp()
                db.insert("task", {
                    "tenant_id": 1,
                    "emp_idx": frozen["idx"],
                    "employee_key": frozen["key"],
                    "employee_catalog_version": frozen["catalog_version"],
                    "employee_name_snapshot": frozen["name"],
                    "employee_dept_key": frozen["dept_key"],
                    "employee_spec_sha256": frozen["spec_sha256"],
                    "brief_json": "{}",
                })
                self._downgrade_fixture_to_schema53()
                with self.assertRaisesRegex(RuntimeError, "完整历史目录精确解析"):
                    db.conn()
                self._assert_schema53_migration_rolled_back()

    def test_schema53_thread_identity_must_exactly_match_its_root_task(self):
        frozen = db._frozen_v1_employee_identities()[1001]
        task_id = db.insert("task", {
            "tenant_id": 1,
            "emp_idx": frozen["idx"],
            "employee_key": frozen["key"],
            "employee_catalog_version": frozen["catalog_version"],
            "employee_name_snapshot": frozen["name"],
            "employee_dept_key": frozen["dept_key"],
            "employee_spec_sha256": frozen["spec_sha256"],
            "brief_json": "{}",
        })
        db.insert("task_thread", {
            "tenant_id": 1,
            "emp_idx": frozen["idx"],
            "employee_key": frozen["key"] + ".tampered",
            "employee_catalog_version": frozen["catalog_version"],
            "root_task_id": task_id,
            "current_task_id": task_id,
        })
        self._downgrade_fixture_to_schema53()
        with self.assertRaisesRegex(RuntimeError, "task_thread.*与根任务不一致"):
            db.conn()
        self._assert_schema53_migration_rolled_back()

    def test_identity_ref_is_full_deterministic_sha256(self):
        current = employeeidentity.active_employee(1001)
        self.assertIsNotNone(current)
        frozen = employeeidentity.snapshot(current)
        first = employeeidentity.identity_ref(current)
        second = employeeidentity.identity_ref(frozen)
        self.assertEqual(first, second)
        self.assertRegex(first, re.compile(r"^[0-9a-f]{64}$"))
        changed = {**frozen, "name": frozen["name"] + "-changed"}
        self.assertNotEqual(first, employeeidentity.identity_ref(changed))

    def test_same_idx_current_and_historical_configs_never_alias(self):
        versions = departments.identity_versions(1001)
        self.assertGreaterEqual(len(versions), 2)
        current = versions[0]
        historical = next(
            row for row in versions
            if row.get("catalog_version") == "v1"
        )
        current_ref = employeeidentity.identity_ref(current)
        historical_ref = employeeidentity.identity_ref(historical)
        self.assertNotEqual(current_ref, historical_ref)

        historical_config = employees.ensure_role_config(historical)
        # Historical V1 rows remain readable for replay but are immutable;
        # only the active V4 identity may receive a new role revision.
        with self.assertRaisesRegex(ValueError, "历史员工岗位配置只读"):
            employees.set_prompt_for_identity(
                historical_ref,
                "V1 冻结工作法",
                expected_revision=historical_config["config_revision"],
            )
        historical_after = employees.get_config_by_identity(historical_ref)
        self.assertEqual(
            historical_config["config_sha256"], historical_after["config_sha256"]
        )
        employees.set_prompt(1001, "V3 当前工作法")

        self.assertEqual(
            historical_config["prompt_template"],
            employees.get_config_by_identity(historical_ref)["prompt_template"],
        )
        current_config = employees.get_config(1001)
        self.assertEqual(current_ref, current_config["identity_ref"])
        self.assertEqual("V3 当前工作法", current_config["prompt_template"])

    def test_person_and_role_status_are_independent_axes(self):
        versions = departments.identity_versions(1001)
        current = versions[0]
        historical = next(
            row for row in versions if row.get("catalog_version") == "v1"
        )
        current_view = employeeidentity.identity_view(current)
        historical_view = employeeidentity.identity_view(historical)
        self.assertEqual(("active", "current"), (
            current_view["person_status"], current_view["identity_status"],
        ))
        self.assertTrue(current_view["can_assign_new"])
        self.assertTrue(current_view["can_continue"])
        self.assertEqual(("active", "historical"), (
            historical_view["person_status"], historical_view["identity_status"],
        ))
        self.assertFalse(historical_view["can_assign_new"])
        self.assertTrue(historical_view["can_continue"])
        self.assertFalse(historical_view["can_learn"])

    def test_professional_profile_participates_in_config_hash(self):
        identity = "a" * 64
        empty = db.normalize_employee_config()
        profiled = db.normalize_employee_config({
            "professional_profile": {
                "capabilities": [{"key": "evidence", "name": "证据核验"}],
                "skill_tree": {"root": ["来源校验", "决策门禁"]},
            },
        })
        self.assertNotEqual(
            db.employee_config_sha256(identity, 1, empty),
            db.employee_config_sha256(identity, 1, profiled),
        )

    def test_config_reads_recompute_identity_and_canonical_hash(self):
        employee = employeeidentity._core_employee(0)
        with patch.object(departments, "get_active", return_value=None):
            config = employees.ensure_role_config(employee)
        identity_ref = config["identity_ref"]
        original = db.one(
            "SELECT * FROM employee_role_config WHERE identity_ref=?",
            (identity_ref,),
        )
        attacks = {
            "idx": 1,
            "employee_key": "tampered-key",
            "employee_catalog_version": "tampered-version",
            "employee_name_snapshot": "tampered-name",
            "employee_dept_key": "tampered-dept",
            "employee_spec_sha256": "f" * 64,
            "prompt_template": "tampered-prompt",
            "skills_json": '[{"title":"tampered"}]',
            "learned_at": 12345.0,
            "settings_json": '{"tampered":true}',
            "caps_off_json": '["tampered"]',
            "model_text": "gpt-5.5",
            "model_image": "tampered-image",
            "professional_profile_json": '{"tampered":true}',
            "config_revision": int(original["config_revision"]) + 1,
            "config_sha256": "0" * 64,
        }
        for column, value in attacks.items():
            with self.subTest(column=column):
                db.execute(
                    f"UPDATE employee_role_config SET {column}=? "
                    "WHERE identity_ref=?",
                    (value, identity_ref),
                )
                self.assertIsNone(
                    employees.get_config_by_identity(identity_ref),
                    f"tampered {column} was accepted",
                )
                db.execute(
                    f"UPDATE employee_role_config SET {column}=? "
                    "WHERE identity_ref=?",
                    (original[column], identity_ref),
                )
        self.assertIsNotNone(employees.get_config_by_identity(identity_ref))
        db.execute(
            "UPDATE employee_role_config SET prompt_template=? WHERE identity_ref=?",
            ("tampered-ensure", identity_ref),
        )
        with patch.object(departments, "get_active", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "完整性"):
                employees.ensure_role_config(employee)

    def test_startup_rejects_role_config_content_tamper(self):
        employee = employeeidentity._core_employee(0)
        with patch.object(departments, "get_active", return_value=None):
            config = employees.ensure_role_config(employee)
        db.execute(
            "UPDATE employee_role_config SET prompt_template=? "
            "WHERE identity_ref=?",
            ("tampered-without-rehash", config["identity_ref"]),
        )
        self._disconnect()
        with self.assertRaisesRegex(RuntimeError, "岗位配置完整性"):
            db.conn()

    def test_core_solo_and_meeting_prompts_use_explicit_frozen_config(self):
        employee = employeeidentity._core_employee(0)
        with patch.object(departments, "get_active", return_value=None):
            first = employees.get_config(0)
            employees.set_prompt_for_identity(
                first["identity_ref"], "冻结工作法",
                expected_revision=first["config_revision"],
            )
            frozen = employees.get_config(0)
            cap_name = registry.CAPABILITIES[registry.BY_IDX[0]["key"]][0]["name"]
            employees.set_caps_off_for_identity(
                frozen["identity_ref"], [cap_name],
                expected_revision=frozen["config_revision"],
            )
            frozen = employees.get_config(0)
            employees.set_prompt_for_identity(
                frozen["identity_ref"], "当前工作法",
                expected_revision=frozen["config_revision"],
            )
            current = employees.get_config(0)
        self.assertGreater(current["config_revision"], frozen["config_revision"])

        frozen_bundle = db.get_employee_role_bundle(
            frozen["identity_ref"], frozen["config_revision"],
            frozen["config_sha256"],
        )
        self.assertIsNotNone(frozen_bundle)

        with patch.object(
            employees, "get_config",
            side_effect=AssertionError("冻结执行不得按 idx 回查当前配置"),
        ):
            caps = registry.capabilities_for(0, config=frozen)
            bundle = registry.solo_prompt(
                0, {"direction": "核验趋势"}, "", "", config=frozen,
            )
            private_context, _sensitive = meeting._meeting_member_private_context({
                "idx": 0,
                "name": employee["name"],
                "duty": employee["duty"],
                "_employee": employee,
                "_config": frozen,
                "_role_bundle": frozen_bundle,
            })
        frozen_member = {
            **employeeidentity.snapshot(employee),
            "identity_ref": frozen["identity_ref"],
            "config_revision": frozen["config_revision"],
            "config_sha256": frozen["config_sha256"],
            "person_snapshot": frozen_bundle.get("person_snapshot", ""),
            "identity_scheme": frozen_bundle.get("identity_scheme", "legacy-six"),
            "bundle_sha256": frozen_bundle["bundle_sha256"],
        }
        original_one = db.one

        def immutable_config_read(sql, args=()):
            self.assertNotIn("employee_slot", sql)
            return original_one(sql, args)

        with (
            patch.object(db, "one", side_effect=immutable_config_read),
            patch.object(
                employeeidentity, "member_snapshot_contract",
                return_value=[frozen_member],
            ),
            patch.object(
                employeeidentity, "resolve_member_snapshots",
                return_value=[employee],
            ),
            patch.object(
                employees, "is_enabled",
                side_effect=AssertionError("历史会议不得回查当前在岗状态"),
            ),
            patch.object(
                departments, "get",
                side_effect=AssertionError("历史会议不得按 idx 回查当前员工"),
            ),
        ):
            loaded = meeting._meeting_member_briefs({
                "emp_idxs_json": "[0]",
                "member_snapshot_json": json.dumps([frozen_member]),
            })

        class FrozenExecutionConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, args=()):
                self_outer.assertNotIn("employee_slot", sql)
                return self.connection.execute(sql, args)

        self_outer = self
        with db.atomic() as connection:
            self.assertTrue(main._role_binding_matches(
                FrozenExecutionConnection(connection),
                {
                    "emp_idx": 0,
                    **employeeidentity.task_fields(employee, config=frozen),
                },
                require_current=False,
            ))
        self.assertFalse(next(c for c in caps if c["name"] == cap_name)["enabled"])
        self.assertIn("冻结工作法", bundle.system)
        self.assertNotIn("当前工作法", bundle.system)
        self.assertIn("冻结工作法", private_context)
        self.assertNotIn("当前工作法", private_context)
        self.assertEqual(frozen["config_revision"], loaded[0]["_config"]["config_revision"])

    def test_provider_role_binding_is_strictly_all_or_none(self):
        employee = employeeidentity._core_employee(0)
        with patch.object(departments, "get_active", return_value=None):
            config = employees.ensure_role_config(employee)
        bundle = db.get_employee_role_bundle(
            config["identity_ref"], config["config_revision"],
            config["config_sha256"],
        )
        self.assertIsNotNone(bundle)
        with self.assertRaisesRegex(providers.ProviderError, "四元"):
            providers.text_model_for(0, identity_ref=config["identity_ref"])
        with self.assertRaisesRegex(providers.ProviderError, "四元"):
            providers.image_model_for(
                0, config_revision=config["config_revision"],
            )
        for resolver in (
            providers.text_model_for,
            providers.image_model_for,
            providers.vision_model_for,
        ):
            with self.subTest(resolver=resolver.__name__):
                with self.assertRaisesRegex(providers.ProviderError, "四元"):
                    resolver(None, identity_ref=config["identity_ref"])
                with self.assertRaisesRegex(providers.ProviderError, "不得携带"):
                    resolver(
                        None,
                        identity_ref=config["identity_ref"],
                        config_revision=config["config_revision"],
                        config_sha256=config["config_sha256"],
                        bundle_sha256=bundle["bundle_sha256"],
                    )
        self.assertEqual(
            providers.DEFAULT_TEXT,
            providers.text_model_for(
                0,
                identity_ref=config["identity_ref"],
                config_revision=config["config_revision"],
                config_sha256=config["config_sha256"],
                bundle_sha256=bundle["bundle_sha256"],
            ),
        )

    def test_meeting_action_reference_collision_fails_closed(self):
        members = [{
            "idx": 0,
            "name": "趋势官",
            "_employee": employeeidentity._core_employee(0),
        }]
        with patch.object(meeting, "_action_key", return_value="a" * 20):
            with self.assertRaisesRegex(RuntimeError, "行动.*碰撞"):
                meeting._normalize_actions([
                    {"idx": 0, "task": "交付A"},
                    {"idx": 0, "task": "交付B"},
                ], members)

    def test_stale_new_task_and_meeting_bindings_fail_before_charge(self):
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 10})
        auth.set_current({
            "id": 20, "tenant_id": 2, "username": "owner", "role": "owner",
            "modules": ["content"],
        })
        self.addCleanup(auth.set_current, None)
        employee = employeeidentity._core_employee(0)
        with patch.object(departments, "get_active", return_value=None):
            old_config = employees.ensure_role_config(employee)
            old_bundle = db.get_employee_role_bundle(
                old_config["identity_ref"], old_config["config_revision"],
                old_config["config_sha256"],
            )
            self.assertIsNotNone(old_bundle)
            old_fields = employeeidentity.task_fields(employee, config=old_config)
            employees.set_prompt_for_identity(
                old_config["identity_ref"], "新工作法",
                expected_revision=old_config["config_revision"],
            )

        with patch.object(departments, "identity_versions", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "已更新"):
                main._create_charged_expert_task({
                    "emp_idx": 0,
                    **old_fields,
                    "tenant_id": 2,
                    "brief_json": '{"direction":"stale"}',
                })
        self.assertEqual(10, billing.balance(2))
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM task")["n"])

        frozen_member = {
            **employeeidentity.snapshot(employee),
            "identity_ref": old_config["identity_ref"],
            "config_revision": old_config["config_revision"],
            "config_sha256": old_config["config_sha256"],
            "bundle_sha256": old_bundle["bundle_sha256"],
        }
        with self.assertRaisesRegex(RuntimeError, "已更新"):
            main._create_charged_meeting({
                "tenant_id": 2,
                "question": "stale meeting",
                "emp_idxs_json": "[0]",
                "member_snapshot_json": json.dumps([frozen_member]),
                "phase": "queued",
            }, 1)
        self.assertEqual(10, billing.balance(2))
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM meeting")["n"])

    def test_current_role_profile_drives_exact_capabilities_and_private_prompt(self):
        current = employeeidentity.active_employee(1001)
        profile = current["professional_profile"]
        config = employees.ensure_role_config(current)
        self.assertEqual(profile, config["professional_profile"])

        caps = departments.capabilities_for(
            current["idx"], [], employee=current,
        )
        self.assertEqual(profile["capabilities"], [cap["desc"] for cap in caps])
        bundle = departments.build_task_prompt(
            current,
            {"direction": "按岗位档案核验本周异常", "industry": "茶咖"},
            "", "", caps,
        )
        for marker in (
            "## 岗位档案与专业范围",
            "## 行业知识域",
            "## 核心数据对象",
            "## 岗位技能树",
            "## 专业能力",
            "## 只读工具权限",
            "## 升级矩阵",
            "## 持续进修路径",
        ):
            self.assertIn(marker, bundle.system)
        self.assertIn(profile["skill_tree"][0], bundle.system)
        self.assertIn(profile["capabilities"][0], bundle.system)
        self.assertNotIn(profile["scope"], bundle.user)
        self.assertNotIn(profile["scope"], bundle.research)

    def test_role_update_preserves_old_revision_for_existing_task(self):
        current = employeeidentity.active_employee(1001)
        first = employees.get_config(1001)
        employees.set_prompt(1001, "第一版工作法")
        frozen = employees.get_config(1001)
        self.assertGreater(frozen["config_revision"], first["config_revision"])

        fields = employeeidentity.task_fields(current)
        self.assertEqual(frozen["config_revision"], fields["employee_config_revision"])
        self.assertEqual(frozen["config_sha256"], fields["employee_config_sha256"])

        employees.set_prompt(1001, "第二版工作法")
        latest = employees.get_config(1001)
        self.assertGreater(latest["config_revision"], frozen["config_revision"])
        old = employees.get_config_by_identity(
            fields["employee_identity_ref"],
            revision=fields["employee_config_revision"],
        )
        self.assertEqual("第一版工作法", old["prompt_template"])
        self.assertEqual(fields["employee_config_sha256"], old["config_sha256"])

    def test_all_role_setters_use_identity_revision_compare_and_swap(self):
        current = employeeidentity.active_employee(1001)
        config = employees.get_config(1001)
        identity_ref = config["identity_ref"]
        employees.set_settings_for_identity(
            identity_ref, {"tone": "evidence-first"},
            expected_revision=config["config_revision"],
        )
        settings_revision = employees.get_config_by_identity(identity_ref)
        self.assertEqual(
            {"tone": "evidence-first"}, settings_revision["settings"]
        )
        with self.assertRaisesRegex(RuntimeError, "已更新"):
            employees.set_caps_off_for_identity(
                identity_ref, ["web"],
                expected_revision=config["config_revision"],
            )
        employees.set_caps_off_for_identity(
            identity_ref, ["web"],
            expected_revision=settings_revision["config_revision"],
        )
        caps_revision = employees.get_config_by_identity(identity_ref)
        employees.set_models_for_identity(
            identity_ref,
            model_text="gpt-5.5",
            expected_revision=caps_revision["config_revision"],
        )
        latest = employees.get_config_by_identity(identity_ref)
        self.assertEqual(["web"], latest["caps_off"])
        self.assertEqual("gpt-5.5", latest["model_text"])
        self.assertEqual(
            employeeidentity.identity_ref(current), latest["identity_ref"]
        )

    def test_domain_mutators_reject_calls_without_complete_cas_binding(self):
        employee = employeeidentity._core_employee(0)
        with patch.object(departments, "get_active", return_value=None):
            config = employees.ensure_role_config(employee)
        identity_ref = config["identity_ref"]
        calls = (
            lambda: employees._upsert_identity(
                identity_ref, {"prompt_template": "unsafe"},
            ),
            lambda: employees.set_prompt_for_identity(identity_ref, "unsafe"),
            lambda: employees.set_skills_for_identity(identity_ref, []),
            lambda: employees.set_settings_for_identity(identity_ref, {}),
            lambda: employees.set_models_for_identity(
                identity_ref, model_text="gpt-5.5",
            ),
            lambda: employees.set_caps_off_for_identity(identity_ref, []),
            lambda: employees.set_enabled(0, False),
            lambda: employees.learn(employee),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(TypeError):
                    call()
        with self.assertRaisesRegex(ValueError, "修订号必填"):
            employees.set_prompt_for_identity(
                identity_ref, "unsafe", expected_revision=None,
            )
        with self.assertRaisesRegex(ValueError, "状态版本必填"):
            employees.set_enabled(0, False, expected_row_version=None)
        unchanged = employees.get_config_by_identity(identity_ref)
        self.assertEqual(config["config_revision"], unchanged["config_revision"])
        self.assertEqual(config["config_sha256"], unchanged["config_sha256"])
        slot = employees.slot_state(0)
        updated_slot = employees.set_enabled(
            0, False, expected_row_version=slot["row_version"],
        )
        self.assertEqual(slot["row_version"] + 1, updated_slot["row_version"])
        with self.assertRaisesRegex(RuntimeError, "在岗状态已更新"):
            employees.set_enabled(
                0, True, expected_row_version=slot["row_version"],
            )

        # Schema55 retains V1/V2/V3 role rows as immutable historical records;
        # they are readable for replay but cannot be mutated or promoted by a
        # current-role setter.
        historical = db._frozen_v1_employee_identities()[1001]
        historical_ref = db.employee_identity_ref(historical)
        historical_config = employees.get_config_by_identity(historical_ref)
        self.assertIsNotNone(historical_config)
        with self.assertRaises((ValueError, RuntimeError)):
            employees.set_prompt_for_identity(
                historical_ref, "unsafe", expected_revision=1,
            )
        self.assertEqual(
            historical_config["config_sha256"],
            employees.get_config_by_identity(historical_ref)["config_sha256"],
        )

    def test_learning_uses_and_updates_one_exact_current_role_revision(self):
        current = employeeidentity.active_employee(1001)
        config = employees.get_config(1001)
        response = {
            "data": {"skills": [{
                "title": "新证据规则", "detail": "先核对来源与时效。",
                "source": "权威公开来源",
            }]},
            "cost_usd": 0.01,
        }
        with patch.object(
            providers, "call_text_json", new=AsyncMock(return_value=response)
        ) as model_call:
            result = asyncio.run(employees.learn(
                current,
                identity_ref=config["identity_ref"],
                expected_revision=config["config_revision"],
                expected_config_sha256=config["config_sha256"],
            ))
        self.assertEqual(1, result["new"])
        self.assertEqual(
            config["identity_ref"], model_call.await_args.kwargs["identity_ref"]
        )
        self.assertEqual(
            config["config_revision"],
            model_call.await_args.kwargs["config_revision"],
        )
        latest = employees.get_config_by_identity(config["identity_ref"])
        self.assertGreater(latest["config_revision"], config["config_revision"])
        self.assertIn("新证据规则", {
            skill["title"] for skill in latest["skills"]
        })

        with patch.object(
            providers, "call_text_json", new=AsyncMock()
        ) as rejected_call:
            with self.assertRaisesRegex(RuntimeError, "岗位身份已更新"):
                asyncio.run(employees.learn(
                    current,
                    identity_ref="f" * 64,
                    expected_revision=config["config_revision"],
                    expected_config_sha256=config["config_sha256"],
                ))
        rejected_call.assert_not_awaited()

    def test_meeting_historical_member_body_never_renders_current_same_idx(self):
        versions = departments.identity_versions(1001)
        current = versions[0]
        historical = next(
            row for row in versions if row.get("catalog_version") == "v1"
        )
        self.assertNotEqual(current["name"], historical["name"])
        config = employees.ensure_role_config(historical)
        historical_bundle = db.get_employee_role_bundle(
            config["identity_ref"], config["config_revision"],
            config["config_sha256"],
        )
        self.assertIsNotNone(historical_bundle)
        frozen = {
            **employeeidentity.snapshot(historical),
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "person_snapshot": historical_bundle.get("person_snapshot", ""),
            "identity_scheme": historical_bundle.get("identity_scheme", "legacy-six"),
            "bundle_sha256": historical_bundle["bundle_sha256"],
        }
        member = meeting._meeting_member_briefs({
            "emp_idxs_json": "[1001]",
            "member_snapshot_json": json.dumps([frozen], ensure_ascii=False),
        })[0]
        self.assertEqual(historical["duty"], member["duty"])
        self.assertEqual(historical["md"][:2500], member["md"])
        self.assertIn(historical["name"], member["name"])
        self.assertNotIn(current["name"], member["name"])
        self.assertEqual(
            employeeidentity.identity_ref(historical),
            member["_config"]["identity_ref"],
        )

    def test_provider_routes_by_exact_identity_revision(self):
        current = employeeidentity.active_employee(1001)
        current_ref = employeeidentity.identity_ref(current)
        employees.set_models(1001, model_text="gpt-5.5")
        first_cfg = employees.get_config_by_identity(current_ref)
        first_bundle = db.get_employee_role_bundle(
            current_ref, first_cfg["config_revision"], first_cfg["config_sha256"],
        )
        self.assertIsNotNone(first_bundle)
        employees.set_models(1001, model_text="claude-opus-4-8")
        latest_cfg = employees.get_config_by_identity(current_ref)
        latest_bundle = db.get_employee_role_bundle(
            current_ref, latest_cfg["config_revision"], latest_cfg["config_sha256"],
        )
        self.assertIsNotNone(latest_bundle)
        self.assertNotEqual(
            first_cfg["config_revision"], latest_cfg["config_revision"]
        )
        self.assertEqual("gpt-5.5", providers.text_model_for(
            1001,
            identity_ref=current_ref,
            config_revision=first_cfg["config_revision"],
            config_sha256=first_cfg["config_sha256"],
            bundle_sha256=first_bundle["bundle_sha256"],
        ))
        self.assertEqual("claude-opus-4-8", providers.text_model_for(
            1001,
            identity_ref=current_ref,
            config_revision=latest_cfg["config_revision"],
            config_sha256=latest_cfg["config_sha256"],
            bundle_sha256=latest_bundle["bundle_sha256"],
        ))

    def test_missing_frozen_config_fails_before_model_and_refunds(self):
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 0})
        historical = next(
            row for row in departments.identity_versions(1001)
            if row.get("catalog_version") == "v1"
        )
        fields = employeeidentity.task_fields(historical)
        fields["employee_config_revision"] = 999999
        fields["employee_config_sha256"] = "f" * 64
        task_id = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": 1001,
            **fields,
            "brief_json": json.dumps({"direction": "复核历史任务"}, ensure_ascii=False),
            "status": "queued",
            "billing_status": "charged",
            "billing_points": 1,
        })
        with patch.object(
            providers, "call_text", new=AsyncMock(
                side_effect=AssertionError("缺失冻结配置后不得调用模型")
            )
        ) as model_call:
            asyncio.run(taskrunner.run_task(task_id, lambda _event: None))
        model_call.assert_not_awaited()
        row = db.one(
            "SELECT status,billing_status FROM task WHERE id=?", (task_id,)
        )
        self.assertEqual("failed", row["status"])
        self.assertEqual("refunded", row["billing_status"])


if __name__ == "__main__":
    unittest.main()
