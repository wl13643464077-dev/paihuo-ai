"""Migration fragment for tenant-scoped activity artwork attachments.

The host database migration calls ``install_schema(conn)`` after ``task``
exists. This module deliberately does not modify or initialize the database.
"""
from __future__ import annotations

import sqlite3


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS task_activity_image(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          task_id INTEGER NOT NULL,
          group_key TEXT NOT NULL,
          file_path TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN
            ('needs_manual_review','failed_qa','passed')),
          quality_json TEXT NOT NULL DEFAULT '{}',
          required_text_json TEXT NOT NULL DEFAULT '{}',
          billing_op_key TEXT,
          brand_package_id INTEGER NOT NULL,
          brand_version INTEGER NOT NULL,
          created_at REAL NOT NULL,
          UNIQUE(tenant_id,task_id,file_path)
        )
    """)
    # A local schema-59 checkout may have created the attachment table before
    # manual review was added. Keep the fragment idempotent for that state.
    columns = {
        row[1] for row in connection.execute(
            "PRAGMA table_info(task_activity_image)"
        ).fetchall()
    }
    if "required_text_json" not in columns:
        connection.execute(
            "ALTER TABLE task_activity_image ADD COLUMN "
            "required_text_json TEXT NOT NULL DEFAULT '{}'"
        )
    if "billing_op_key" not in columns:
        connection.execute(
            "ALTER TABLE task_activity_image ADD COLUMN billing_op_key TEXT"
        )
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_activity_image_scope_group
        ON task_activity_image(tenant_id,task_id,group_key,id)
    """)
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_activity_image_billing_op
        ON task_activity_image(billing_op_key)
        WHERE billing_op_key IS NOT NULL
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS task_activity_image_review(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          task_id INTEGER NOT NULL,
          image_id INTEGER NOT NULL,
          reviewer_id INTEGER NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
          result_status TEXT NOT NULL CHECK(result_status IN
            ('needs_manual_review','failed_qa','passed')),
          note TEXT NOT NULL DEFAULT '',
          observed_text TEXT NOT NULL DEFAULT '',
          logo_match INTEGER NOT NULL CHECK(logo_match IN (0,1)),
          no_extra_claims INTEGER NOT NULL CHECK(no_extra_claims IN (0,1)),
          quality_json TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_activity_review_scope
        ON task_activity_image_review(tenant_id,task_id,image_id,id)
    """)
