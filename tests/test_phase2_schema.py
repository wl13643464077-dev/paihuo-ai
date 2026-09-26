"""Schema v64:老板把活派给真人店员的闭环(派活任务/交差照片/审计轨迹/清单)。

全部走临时 SQLite 真实迁移：新库直接建好、v63 旧库升级保数据、
CHECK/唯一约束生效、关键约束或索引被改坏时拒绝启动。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from app import db


STAFF_TASK_COLUMNS = {
    "id", "tenant_id", "branch_id", "assignee_user_id", "title", "detail",
    "source", "source_ref", "require_photo", "due_at", "status", "priority",
    "created_by", "created_at", "updated_at", "submitted_at", "submit_note",
    "reviewed_at", "reviewed_by", "review_note", "ai_check_json",
    "remind_count", "last_remind_at", "escalated_level", "request_key",
    "deleted_at",
}
STAFF_TASK_PHOTO_COLUMNS = {
    "id", "tenant_id", "task_id", "storage_key", "sha256", "mime_type",
    "byte_size", "width", "height", "received_at", "watermark_text",
    "created_by", "created_at",
}
STAFF_TASK_EVENT_COLUMNS = {
    "id", "tenant_id", "task_id", "actor_user_id", "kind", "note", "created_at",
}
CHECKLIST_TEMPLATE_COLUMNS = {
    "id", "tenant_id", "industry_key", "kind", "name", "items_json",
    "due_time", "active", "created_by", "created_at", "updated_at",
}
CHECKLIST_RUN_COLUMNS = {
    "id", "tenant_id", "branch_id", "template_id", "run_date", "kind",
    "status", "assignee_user_id", "items_json", "completed_at",
    "completed_by", "due_at", "created_at", "updated_at",
}
# 名称 -> (表, 有序列, 是否唯一, 是否部分索引)
INDEX_CONTRACTS = {
    "idx_staff_task_assignee": (
        "staff_task", ("tenant_id", "assignee_user_id", "status", "due_at"),
        False, False,
    ),
    "idx_staff_task_branch": (
        "staff_task", ("tenant_id", "branch_id", "status"), False, False,
    ),
    "idx_staff_task_due": (
        "staff_task", ("tenant_id", "status", "due_at"), False, False,
    ),
    "idx_staff_task_request": (
        "staff_task", ("tenant_id", "request_key"), True, True,
    ),
    "idx_staff_task_photo_task": (
        "staff_task_photo", ("tenant_id", "task_id"), False, False,
    ),
    "idx_staff_task_event_task": (
        "staff_task_event", ("tenant_id", "task_id", "id"), False, False,
    ),
    "idx_checklist_template_active": (
        "checklist_template", ("tenant_id", "active"), False, False,
    ),
    "idx_checklist_run_date": (
        "checklist_run", ("tenant_id", "run_date", "status"), False, False,
    ),
    "idx_checklist_run_assignee": (
        "checklist_run", ("tenant_id", "assignee_user_id", "run_date"),
        False, False,
    ),
    "idx_users_tenant_phone": ("users", ("tenant_id", "phone"), False, False),
}
NEW_TABLES = (
    "staff_task", "staff_task_photo", "staff_task_event",
    "checklist_template", "checklist_run",
)


class SchemaV64Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        self._reset()
        db.DB_PATH = os.path.join(self.tmp.name, "phase2.db")

    def tearDown(self):
        self._reset()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    @staticmethod
    def _reset():
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

    def _raw(self, *statements):
        self._reset()
        connection = sqlite3.connect(db.DB_PATH)
        try:
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _columns(table):
        return {row["name"] for row in db.q(f"PRAGMA table_info({table})")}

    def _assert_v64_structure(self):
        self.assertEqual(64, db.one("PRAGMA user_version")["user_version"])
        self.assertEqual(
            "staff-task-loop",
            db.one("SELECT name FROM schema_version WHERE version=64")["name"],
        )
        for table, expected in (
            ("staff_task", STAFF_TASK_COLUMNS),
            ("staff_task_photo", STAFF_TASK_PHOTO_COLUMNS),
            ("staff_task_event", STAFF_TASK_EVENT_COLUMNS),
            ("checklist_template", CHECKLIST_TEMPLATE_COLUMNS),
            ("checklist_run", CHECKLIST_RUN_COLUMNS),
        ):
            self.assertEqual(expected, self._columns(table), table)
        self.assertTrue(
            {"assignee_user_id", "close_reason"} <= self._columns("inspection_action")
        )
        self.assertIn("phone", self._columns("users"))
        for name, (table, columns, unique, partial) in INDEX_CONTRACTS.items():
            row = next(
                (r for r in db.q(f"PRAGMA index_list({table})") if r["name"] == name),
                None,
            )
            self.assertIsNotNone(row, name)
            self.assertEqual(
                (columns, unique, partial),
                (
                    tuple(r["name"] for r in db.q(f"PRAGMA index_info({name})")),
                    bool(row["unique"]), bool(row["partial"]),
                ),
                name,
            )

    def test_fresh_database_has_all_v64_tables_columns_and_indexes(self):
        db.conn()
        self.assertEqual(64, db.LATEST_SCHEMA_VERSION)
        self._assert_v64_structure()
        # 默认值按约定落库，其他开发者照此写代码。
        task_id = db.insert("staff_task", {"tenant_id": 2, "title": "擦玻璃"})
        task = db.one("SELECT * FROM staff_task WHERE id=?", (task_id,))
        self.assertEqual(
            ("boss", "todo", "normal", 1, 0, 0, "", "", "", ""),
            (
                task["source"], task["status"], task["priority"],
                task["require_photo"], task["remind_count"],
                task["escalated_level"], task["detail"], task["source_ref"],
                task["submit_note"], task["review_note"],
            ),
        )
        self.assertIsNone(task["assignee_user_id"])
        self.assertIsNone(task["request_key"])
        run_id = db.insert("checklist_run", {
            "tenant_id": 2, "branch_id": 5, "template_id": 1,
            "run_date": "2026-09-25", "kind": "open",
        })
        run = db.one("SELECT status,items_json FROM checklist_run WHERE id=?", (run_id,))
        self.assertEqual(("open", "[]"), (run["status"], run["items_json"]))

    def test_v63_database_upgrades_and_keeps_existing_data(self):
        db.conn()
        db.insert("tenants", {"id": 2, "name": "企业", "industries_json": "[]"})
        db.insert("users", {
            "id": 22, "tenant_id": 2, "username": "u22", "password_hash": "x",
            "role": "member", "modules_json": "[]", "enabled": 1,
        })
        db.insert("inspection_action", {
            "id": 7, "tenant_id": 2, "visit_id": 3, "issue_id": 4,
            "status": "closed", "plan": "补货", "created_at": 1, "updated_at": 1,
        })
        # 模拟 v63 旧库：已含现网品牌/媒体与门店/支付，尚无店员任务。
        self._raw(
            *(f"DROP TABLE {table}" for table in NEW_TABLES),
            "DROP INDEX idx_users_tenant_phone",
            "ALTER TABLE users DROP COLUMN phone",
            "ALTER TABLE inspection_action DROP COLUMN assignee_user_id",
            "ALTER TABLE inspection_action DROP COLUMN close_reason",
            "DELETE FROM schema_version WHERE version>=64",
            "PRAGMA user_version=63",
        )
        connection = sqlite3.connect(db.DB_PATH)
        try:
            self.assertNotIn("phone", {
                row[1] for row in connection.execute("PRAGMA table_info(users)")
            })
        finally:
            connection.close()

        db.conn()
        self._assert_v64_structure()
        self.assertEqual(
            1, db.one("SELECT COUNT(*) n FROM schema_version WHERE version=64")["n"],
        )
        user = db.one("SELECT username,phone FROM users WHERE id=22")
        self.assertEqual(("u22", None), (user["username"], user["phone"]))
        action = db.one("SELECT * FROM inspection_action WHERE id=7")
        self.assertEqual(
            ("closed", "补货", None, ""),
            (action["status"], action["plan"], action["assignee_user_id"],
             action["close_reason"]),
        )
        for table in NEW_TABLES:
            self.assertEqual(0, db.one(f"SELECT COUNT(*) n FROM {table}")["n"])
        # 误报作废不改 inspection_action 的 CHECK，用 closed + false_positive 表达。
        db.execute(
            "UPDATE inspection_action SET assignee_user_id=22,"
            "close_reason='false_positive' WHERE id=7"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE inspection_action SET status='false_positive' WHERE id=7")

    def test_check_constraints_reject_invalid_values(self):
        db.conn()
        db.insert("staff_task", {"tenant_id": 2, "title": "盘点"})
        for column, value in (
            ("status", "done"), ("source", "wechat"), ("priority", "urgent"),
        ):
            with self.assertRaises(sqlite3.IntegrityError, msg=column):
                db.execute(
                    f"INSERT INTO staff_task(tenant_id,title,{column}) VALUES(2,'x',?)",
                    (value,),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE staff_task SET status='weird'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO checklist_template(tenant_id,kind,name) "
                "VALUES(2,'lunch','午市')"
            )
        db.execute(
            "INSERT INTO checklist_template(tenant_id,kind,name) VALUES(2,'open','开店')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO checklist_run(tenant_id,branch_id,template_id,run_date,"
                "kind,status) VALUES(2,5,1,'2026-09-25','open','skipped')"
            )
        # 同店同模板同一天只能有一次清单；换一天可以。
        db.execute(
            "INSERT INTO checklist_run(tenant_id,branch_id,template_id,run_date,kind) "
            "VALUES(2,5,1,'2026-09-25','open')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO checklist_run(tenant_id,branch_id,template_id,run_date,kind) "
                "VALUES(2,5,1,'2026-09-25','open')"
            )
        db.execute(
            "INSERT INTO checklist_run(tenant_id,branch_id,template_id,run_date,kind) "
            "VALUES(2,5,1,'2026-09-26','open')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO staff_task_photo(tenant_id,task_id,storage_key,sha256,"
                "mime_type,byte_size) VALUES(2,1,'k','s','image/jpeg',10)"
            )  # received_at 必填

    def test_request_key_partial_unique_index(self):
        db.conn()
        insert = (
            "INSERT INTO staff_task(tenant_id,title,request_key) VALUES(?,?,?)"
        )
        db.execute(insert, (2, "补货", "req-1"))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(insert, (2, "补货again", "req-1"))
        db.execute(insert, (3, "别家同键", "req-1"))
        # 没有 request_key 的任务不受唯一约束限制。
        db.execute(insert, (2, "a", None))
        db.execute(insert, (2, "b", None))
        self.assertEqual(
            2,
            db.one(
                "SELECT COUNT(*) n FROM staff_task WHERE tenant_id=2 "
                "AND request_key IS NULL"
            )["n"],
        )

    def test_startup_rejects_non_unique_request_key_index(self):
        db.conn()
        self._raw(
            "DROP INDEX idx_staff_task_request",
            "CREATE INDEX idx_staff_task_request ON staff_task(tenant_id,request_key)",
        )
        with self.assertRaises(RuntimeError) as caught:
            db.conn()
        self.assertIn("idx_staff_task_request", str(caught.exception))

    def test_startup_rejects_wrong_index_columns(self):
        db.conn()
        self._raw(
            "DROP INDEX idx_staff_task_assignee",
            "CREATE INDEX idx_staff_task_assignee ON staff_task(tenant_id,status)",
        )
        with self.assertRaises(RuntimeError) as caught:
            db.conn()
        self.assertIn("idx_staff_task_assignee", str(caught.exception))

    def test_startup_rejects_checklist_run_without_unique_key(self):
        db.conn()
        self._raw(
            "DROP TABLE checklist_run",
            "CREATE TABLE checklist_run(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "tenant_id INTEGER NOT NULL,branch_id INTEGER NOT NULL,"
            "template_id INTEGER NOT NULL,run_date TEXT NOT NULL,kind TEXT NOT NULL,"
            "status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done','missed')),"
            "assignee_user_id INTEGER,items_json TEXT NOT NULL DEFAULT '[]',"
            "completed_at REAL,completed_by INTEGER,due_at REAL,"
            "created_at REAL,updated_at REAL)",
        )
        with self.assertRaises(RuntimeError) as caught:
            db.conn()
        self.assertIn("checklist_run", str(caught.exception))

    def test_startup_rejects_staff_task_without_status_check(self):
        db.conn()
        # 列齐全但没有任何取值约束的同名旧表：CREATE TABLE IF NOT EXISTS 修不了它。
        others = sorted(STAFF_TASK_COLUMNS - {"id", "tenant_id", "title"})
        self._raw(
            "DROP TABLE staff_task",
            "CREATE TABLE staff_task(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "tenant_id INTEGER NOT NULL,title TEXT NOT NULL,"
            + ",".join(others) + ")",
        )
        with self.assertRaises(RuntimeError) as caught:
            db.conn()
        self.assertIn("staff_task", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
