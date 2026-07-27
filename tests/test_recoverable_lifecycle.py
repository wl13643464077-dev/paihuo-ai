"""Recoverable deletion and no-double-charge retry contracts."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import auth, avatar, billing, db, main, notify, taskcenter, taskrunner


class RecoverableLifecycleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "lifecycle.db")
        db.conn()
        db.insert(
            "tenants",
            {"id": 2, "name": "测试企业", "balance": 20, "enabled": 1},
        )
        auth.set_current(
            {
                "id": 20,
                "tenant_id": 2,
                "username": "owner",
                "role": "owner",
                "modules": [],
            }
        )

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _failed_task(self, **extra):
        return db.insert(
            "task",
            {
                "tenant_id": 2,
                "emp_idx": 0,
                "brief_json": json.dumps({"direction": "失败任务"}),
                "status": "failed",
                "billing_status": "refunded",
                "billing_points": 1,
                **extra,
            },
        )

    def _failed_avatar(self):
        return db.insert(
            "avatar_job",
            {
                "tenant_id": 2,
                "params_json": json.dumps(
                    {"script": "失败口播", "duration": 30}
                ),
                "status": "failed",
                "billing_status": "refunded",
                "billing_points": 12,
            },
        )

    def test_expert_retry_reuses_same_record_without_balance_change(self):
        task_id = self._failed_task()
        before = billing.balance(2)

        with patch.object(
            taskrunner, "run_task", new=AsyncMock()
        ) as worker:
            result = asyncio.run(main.task_retry(task_id))

        self.assertEqual(task_id, result["task_id"])
        self.assertTrue(result["free_retry"])
        self.assertEqual(before, billing.balance(2))
        self.assertEqual(
            {
                "status": "queued",
                "billing_status": "included",
                "retry_count": 1,
            },
            db.one(
                "SELECT status,billing_status,retry_count FROM task WHERE id=?",
                (task_id,),
            ),
        )
        worker.assert_awaited_once()
        self.assertFalse(taskrunner.prepare_retry(task_id, 2))

    def test_industry_retry_and_restore_emit_exact_sse_scope(self):
        retry_id = self._failed_task(emp_idx=1601)
        with patch.object(
            taskrunner, "run_task", new=AsyncMock()
        ), patch.object(main.engine, "broadcast") as broadcast:
            asyncio.run(main.task_retry(retry_id))
        retry_event = broadcast.call_args.args[0]
        self.assertEqual(2, retry_event["tenant_id"])
        self.assertEqual(("auto",), retry_event["_required_modules"])

        restore_id = db.insert(
            "task",
            {
                "tenant_id": 2,
                "emp_idx": 1601,
                "brief_json": json.dumps({"direction": "恢复行业任务"}),
                "status": "done",
                "billing_status": "included",
                "deleted_at": 123,
                "delete_reason": "用户移入回收站",
            },
        )
        with patch.object(main.engine, "broadcast") as broadcast:
            main.trash_restore("task", restore_id)
        restore_event = broadcast.call_args.args[0]
        self.assertEqual(2, restore_event["tenant_id"])
        self.assertEqual(("auto",), restore_event["_required_modules"])

    def test_avatar_retry_reuses_same_record_without_balance_change(self):
        job_id = self._failed_avatar()
        before = billing.balance(2)

        with patch.object(avatar, "run_job", new=AsyncMock()) as worker:
            result = asyncio.run(main.avatar_job_retry(job_id))

        self.assertEqual(job_id, result["job_id"])
        self.assertTrue(result["free_retry"])
        self.assertEqual(before, billing.balance(2))
        self.assertEqual(
            {
                "status": "queued",
                "billing_status": "included",
                "retry_count": 1,
            },
            db.one(
                "SELECT status,billing_status,retry_count "
                "FROM avatar_job WHERE id=?",
                (job_id,),
            ),
        )
        worker.assert_awaited_once()
        self.assertFalse(avatar.prepare_retry(job_id, 2))

    def test_uncharged_pending_shell_cannot_become_free_retry(self):
        task_id = db.insert(
            "task",
            {
                "tenant_id": 2,
                "emp_idx": 0,
                "brief_json": json.dumps({"direction": "尚未扣费"}),
                "status": "pending_charge",
                "billing_status": "pending",
                "billing_points": 1,
            },
        )
        before = billing.balance(2)

        self.assertTrue(main.task_delete(task_id)["soft_deleted"])
        self.assertTrue(main.trash_restore("task", task_id)["restored"])
        self.assertEqual(
            {"status": "failed", "billing_status": "void"},
            db.one(
                "SELECT status,billing_status FROM task WHERE id=?", (task_id,)
            ),
        )
        with self.assertRaises(HTTPException) as rejected:
            asyncio.run(main.task_retry(task_id))
        self.assertEqual(409, rejected.exception.status_code)
        self.assertEqual(before, billing.balance(2))
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2"
            )["n"],
        )

    def test_retry_is_tenant_scoped_and_free_attempts_are_bounded(self):
        capped_task = self._failed_task(retry_count=taskrunner.MAX_FREE_RETRIES)
        capped_avatar = db.insert(
            "avatar_job",
            {
                "tenant_id": 2,
                "params_json": "{}",
                "status": "failed",
                "billing_status": "included",
                "retry_count": avatar.MAX_FREE_RETRIES,
            },
        )
        with self.assertRaises(HTTPException) as task_limit:
            asyncio.run(main.task_retry(capped_task))
        self.assertEqual(429, task_limit.exception.status_code)
        with self.assertRaises(HTTPException) as avatar_limit:
            asyncio.run(main.avatar_job_retry(capped_avatar))
        self.assertEqual(429, avatar_limit.exception.status_code)

        foreign_task = self._failed_task()
        auth.set_current(
            {
                "id": 1,
                "tenant_id": 1,
                "username": "boss",
                "role": "owner",
                "modules": [],
            }
        )
        with self.assertRaises(HTTPException) as hidden:
            asyncio.run(main.task_retry(foreign_task))
        self.assertEqual(404, hidden.exception.status_code)
        self.assertEqual(
            "failed",
            db.one("SELECT status FROM task WHERE id=?", (foreign_task,))[
                "status"
            ],
        )

    def test_soft_deleted_records_leave_active_lists_and_can_restore(self):
        job_id = db.insert(
            "job",
            {
                "tenant_id": 2,
                "brief_json": json.dumps({"direction": "已交付内容"}),
                "status": "done",
                "billing_status": "succeeded",
            },
        )
        task_id = self._failed_task()
        knowledge_id = db.insert(
            "knowledge",
            {
                "tenant_id": 2,
                "title": "可恢复知识",
                "content": "正文",
            },
        )
        avatar_id = self._failed_avatar()

        self.assertTrue(main.delete_job(job_id)["soft_deleted"])
        self.assertTrue(main.task_delete(task_id)["soft_deleted"])
        self.assertTrue(main.knowledge_delete(knowledge_id)["soft_deleted"])
        self.assertTrue(main.avatar_job_delete(avatar_id)["soft_deleted"])

        self.assertFalse(
            any(
                item["record_id"] in {job_id, task_id, avatar_id}
                for item in taskcenter.list_items(
                    2, {"content", "avatar", "library"}
                )["items"]
            )
        )
        self.assertFalse(
            any(row["id"] == knowledge_id for row in main.knowledge_list())
        )
        self.assertFalse(
            any(row["id"] == avatar_id for row in main.avatar_jobs())
        )

        trash = main.trash_list()
        self.assertEqual(
            {("job", job_id), ("task", task_id),
             ("knowledge", knowledge_id), ("avatar", avatar_id)},
            {(item["kind"], item["id"]) for item in trash["items"]},
        )
        for kind, record_id in (
            ("job", job_id),
            ("task", task_id),
            ("knowledge", knowledge_id),
            ("avatar", avatar_id),
        ):
            self.assertTrue(main.trash_restore(kind, record_id)["restored"])

        for table, record_id in (
            ("job", job_id),
            ("task", task_id),
            ("knowledge", knowledge_id),
            ("avatar_job", avatar_id),
        ):
            self.assertIsNone(
                db.one(
                    f"SELECT deleted_at FROM {table} WHERE id=?", (record_id,)
                )["deleted_at"]
            )

    def test_profile_and_asset_soft_delete_show_in_trash_and_restore(self):
        pid = db.insert(
            "account_profile",
            {"tenant_id": 2, "name": "主理人人设", "persona_json": "{}"},
        )
        aid = db.insert(
            "asset",
            {
                "tenant_id": 2,
                "type": "topic",
                "payload_json": json.dumps(
                    {"title": "选题A"}, ensure_ascii=False
                ),
            },
        )
        self.assertTrue(main.delete_profile(pid)["soft_deleted"])
        self.assertTrue(main.delete_asset(aid)["soft_deleted"])

        with self.assertRaises(HTTPException) as gone:
            main.get_profile(pid)
        self.assertEqual(404, gone.exception.status_code)
        self.assertFalse(
            any(row["id"] == aid for row in main.assets(type="topic"))
        )
        with self.assertRaises(HTTPException) as detail_gone:
            main.asset_detail(aid)
        self.assertEqual(404, detail_gone.exception.status_code)

        by_key = {
            (item["kind"], item["id"]): item
            for item in main.trash_list()["items"]
        }
        self.assertEqual("主理人人设", by_key[("profile", pid)]["title"])
        self.assertEqual("选题A", by_key[("asset", aid)]["title"])

        self.assertTrue(main.trash_restore("profile", pid)["restored"])
        self.assertTrue(main.trash_restore("asset", aid)["restored"])
        self.assertEqual(pid, main.get_profile(pid)["id"])
        self.assertEqual(aid, main.asset_detail(aid)["id"])

    def test_profile_delete_blocked_while_enabled_schedule_references_it(self):
        pid = db.insert(
            "account_profile",
            {"tenant_id": 2, "name": "被引用人设", "persona_json": "{}"},
        )
        db.insert(
            "schedule",
            {
                "tenant_id": 2,
                "name": "每日小红书",
                "brief_json": "{}",
                "profile_id": pid,
                "enabled": 1,
            },
        )
        with self.assertRaises(HTTPException) as blocked:
            main.delete_profile(pid)
        self.assertEqual(409, blocked.exception.status_code)
        self.assertIn("每日小红书", blocked.exception.detail)

        db.execute("UPDATE schedule SET enabled=0 WHERE profile_id=?", (pid,))
        self.assertTrue(main.delete_profile(pid)["soft_deleted"])

    def test_restore_unique_conflict_rolls_back_connection(self):
        deleted = self._failed_task(
            source_meeting_id=7,
            source_action_key="same-action",
            deleted_at=100,
        )
        self._failed_task(
            source_meeting_id=7,
            source_action_key="same-action",
        )

        with self.assertRaises(HTTPException) as conflict:
            main.trash_restore("task", deleted)
        self.assertEqual(409, conflict.exception.status_code)
        self.assertFalse(db.conn().in_transaction)
        with db.atomic() as connection:
            connection.execute(
                "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?)",
                ("after-conflict", "ok", 1),
            )
        self.assertEqual("ok", db.get_setting("after-conflict"))

    def test_trash_is_tenant_scoped_and_member_cannot_delete_or_restore(self):
        foreign = db.insert(
            "knowledge",
            {
                "tenant_id": 1,
                "title": "平台私有知识",
                "content": "不可见",
                "deleted_at": 100,
            },
        )
        self.assertFalse(
            any(
                item["id"] == foreign and item["kind"] == "knowledge"
                for item in main.trash_list()["items"]
            )
        )
        with self.assertRaises(HTTPException) as missing:
            main.trash_restore("knowledge", foreign)
        self.assertEqual(404, missing.exception.status_code)

        own = db.insert(
            "knowledge",
            {"tenant_id": 2, "title": "成员不可删", "content": ""},
        )
        auth.set_current(
            {
                "id": 21,
                "tenant_id": 2,
                "username": "member",
                "role": "member",
                "modules": ["library"],
            }
        )
        with self.assertRaises(HTTPException) as denied:
            main.knowledge_delete(own)
        self.assertEqual(403, denied.exception.status_code)
        with self.assertRaises(HTTPException) as denied_trash:
            main.trash_list()
        self.assertEqual(403, denied_trash.exception.status_code)

    def test_notification_falls_back_to_unread_inbox_without_webhook(self):
        self.assertEqual("", notify.get_webhook(2))
        notify.push(
            2,
            "report",
            {
                "report_name": "定时竞品周报",
                "summary": "本周发现三个新动作",
                "link": "#/knowledge",
            },
        )
        row = db.one(
            "SELECT id,title,body,link,read_at FROM notification "
            "WHERE tenant_id=2"
        )
        self.assertEqual("定时竞品周报", row["title"])
        self.assertEqual("本周发现三个新动作", row["body"])
        self.assertEqual("#/knowledge", row["link"])
        self.assertIsNone(row["read_at"])
        self.assertEqual(row["id"], main.state()["notifications"][0]["id"])

        self.assertEqual(
            1, main.notifications_read({"ids": [row["id"]]})["updated"]
        )
        self.assertEqual([], main.state()["notifications"])

    def test_database_uses_one_connection_per_worker_thread(self):
        barrier = threading.Barrier(2)

        def connection_id():
            connection = db.conn()
            barrier.wait(timeout=2)
            connection.execute("SELECT 1").fetchone()
            return id(connection)

        main_connection = id(db.conn())
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker_ids = set(pool.map(lambda _index: connection_id(), range(2)))
        self.assertEqual(2, len(worker_ids))
        self.assertNotIn(main_connection, worker_ids)


if __name__ == "__main__":
    unittest.main()
