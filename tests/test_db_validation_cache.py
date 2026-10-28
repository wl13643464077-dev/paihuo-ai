"""Exact-content memoization must not turn a stored hash into trust."""
import json
import os
import sqlite3
import tempfile
import unittest

from app import db


class ScalarValidationCacheTests(unittest.TestCase):
    def test_only_exact_typed_content_is_reused(self):
        seen = []

        @db._memoize_scalar_row_validation
        def validate(row):
            seen.append(dict(row))
            return row.get("value") == "original"

        row = {"digest": "unchanged", "value": "original", "revision": 1}
        self.assertTrue(validate(row))
        self.assertTrue(validate(dict(reversed(list(row.items())))))
        self.assertEqual(len(seen), 1)
        row["value"] = "tampered"
        self.assertFalse(validate(row))
        row["revision"] = True
        self.assertFalse(validate(row))
        self.assertEqual(len(seen), 3)

    def test_nested_and_large_values_are_not_retained(self):
        @db._memoize_scalar_row_validation
        def validate(row):
            return bool(row)

        self.assertTrue(validate({"nested": {"a": 1}}))
        self.assertTrue(validate({"large": "x" * 32769}))
        self.assertEqual(validate.cache_info().currsize, 0)

    def test_signed_zero_has_distinct_canonical_content(self):
        @db._memoize_scalar_row_validation
        def validate(row):
            return json.dumps(row["value"]) == "0.0"

        self.assertTrue(validate({"value": 0.0}))
        self.assertFalse(validate({"value": -0.0}))
        self.assertEqual(validate.cache_info().misses, 2)

    def test_entry_count_is_bounded_and_clearable(self):
        @db._memoize_scalar_row_validation
        def validate(row):
            return True

        for i in range(2200):
            validate({"value": i})
        self.assertEqual(validate.cache_info().currsize, 2048)
        validate.cache_clear()
        self.assertEqual(validate.cache_info().currsize, 0)

    def test_sqlite_row_and_dict_share_content_not_identity(self):
        @db._memoize_scalar_row_validation
        def validate(row):
            return row["value"] == 1

        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT 1 AS value").fetchone()
            self.assertTrue(validate(row))
            self.assertTrue(validate(dict(row)))
        self.assertEqual(validate.cache_info().hits, 1)


class RoleValidationCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = db._conn_path = None
        db.DB_PATH = os.path.join(cls.tmp.name, "cache.db")
        db.conn()
        cls.config = db.one("SELECT * FROM employee_role_config LIMIT 1")
        cls.bundle = db.one("SELECT * FROM employee_role_bundle_revision LIMIT 1")

    @classmethod
    def tearDownClass(cls):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = db._conn_path = None
        db.DB_PATH = cls.old_path
        cls.tmp.cleanup()

    def test_same_config_digest_does_not_hide_changed_content(self):
        self.assertTrue(db.employee_role_config_row_valid(self.config))
        for field, value in (("prompt_template", "tampered"),
                             ("skills_json", '["tampered"]'),
                             ("employee_name_snapshot", "tampered"),
                             ("config_revision", 999)):
            with self.subTest(field=field):
                self.assertFalse(db.employee_role_config_row_valid(
                    {**self.config, field: value}))
        self.assertTrue(db.employee_role_config_row_valid(self.config))

    def test_same_bundle_digest_does_not_hide_changed_payload(self):
        self.assertTrue(db.employee_role_bundle_row_valid(self.bundle))
        for field in ("baseline_json", "effective_json"):
            payload = json.loads(self.bundle[field])
            payload["config"]["prompt_template"] = "tampered"
            with self.subTest(field=field):
                self.assertFalse(db.employee_role_bundle_row_valid(
                    {**self.bundle, field: json.dumps(payload)}))
        self.assertFalse(db.employee_role_bundle_row_valid(
            {**self.bundle, "employee_key": "another-role"}))
        self.assertTrue(db.employee_role_bundle_row_valid(self.bundle))

    def test_mutable_bundle_payload_is_revalidated_after_mutation(self):
        row = dict(self.bundle)
        row["baseline_json"] = json.loads(row["baseline_json"])
        row["effective_json"] = json.loads(row["effective_json"])
        self.assertTrue(db.employee_role_bundle_row_valid(row))
        row["effective_json"]["config"]["prompt_template"] = "tampered"
        self.assertFalse(db.employee_role_bundle_row_valid(row))
