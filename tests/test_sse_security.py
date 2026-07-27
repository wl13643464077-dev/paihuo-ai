"""SSE 授权与异步租户解析的安全回归。"""
import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, meeting
from app.engine import Engine


class MeetingSseAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "meeting-sse.db")
        db.conn()

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def test_meeting_body_only_reaches_fully_authorized_or_privileged_users(self):
        meeting_id = db.insert(
            "meeting",
            {
                "tenant_id": 2,
                "question": "新品计划",
                "emp_idxs_json": json.dumps([0, 101]),
            },
        )
        engine = Engine()
        queues = {
            "authorized": asyncio.Queue(),
            "partial": asyncio.Queue(),
            "owner": asyncio.Queue(),
            "root": asyncio.Queue(),
            "foreign": asyncio.Queue(),
        }
        engine.subscribers = {
            queues["authorized"]: (
                2,
                False,
                frozenset({"content", "curtain"}),
                "member",
            ),
            queues["partial"]: (2, False, frozenset({"content"}), "member"),
            queues["owner"]: (2, False, None, "owner"),
            queues["root"]: (1, True, None, "root"),
            queues["foreign"]: (3, False, None, "owner"),
        }

        def expert(idx):
            return {"dept_key": "curtain"} if idx == 101 else None

        with patch.object(meeting.departments, "get", side_effect=expert):
            await meeting._push(
                meeting_id,
                engine.broadcast,
                "趋势官",
                "未公开新品定价与投放计划",
            )

        for key in ("authorized", "owner", "root"):
            event = queues[key].get_nowait()
            self.assertEqual("meeting_msg", event["type"])
            self.assertEqual(
                "未公开新品定价与投放计划",
                event["msg"]["text"],
            )
            self.assertNotIn("_required_modules", event)
        self.assertTrue(queues["partial"].empty())
        self.assertTrue(queues["foreign"].empty())

    async def test_invalid_empty_meeting_scope_fails_closed_for_member(self):
        meeting_id = db.insert(
            "meeting",
            {
                "tenant_id": 2,
                "question": "异常会议",
                "emp_idxs_json": "[]",
            },
        )
        engine = Engine()
        member = asyncio.Queue()
        owner = asyncio.Queue()
        engine.subscribers = {
            member: (2, False, frozenset({"content"}), "member"),
            owner: (2, False, None, "owner"),
        }

        await meeting._push(
            meeting_id,
            engine.broadcast,
            "系统",
            "异常范围也不能广播给普通成员",
        )

        self.assertTrue(member.empty())
        self.assertEqual("meeting_msg", owner.get_nowait()["type"])

    async def test_meeting_update_uses_same_tenant_and_module_scope_as_body(self):
        engine = Engine()
        authorized = asyncio.Queue()
        partial = asyncio.Queue()
        owner = asyncio.Queue()
        foreign = asyncio.Queue()
        engine.subscribers = {
            authorized: (2, False, frozenset({"content", "curtain"}), "member"),
            partial: (2, False, frozenset({"content"}), "member"),
            owner: (2, False, None, "owner"),
            foreign: (3, False, None, "owner"),
        }
        row = {
            "tenant_id": 2,
            "emp_idxs_json": json.dumps([0, 101]),
        }

        def expert(idx):
            return {"dept_key": "curtain"} if idx == 101 else None

        with patch.object(meeting.departments, "get", side_effect=expert):
            meeting._emit_meeting_update(engine.broadcast, 9, row)

        for queue in (authorized, owner):
            event = queue.get_nowait()
            self.assertEqual("meeting_update", event["type"])
            self.assertEqual(9, event["meeting_id"])
            self.assertEqual(2, event["tenant_id"])
            self.assertNotIn("_required_modules", event)
        self.assertTrue(partial.empty())
        self.assertTrue(foreign.empty())

    def test_all_meeting_update_emitters_use_scoped_helper(self):
        source = Path(meeting.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            1,
            source.count('"type": "meeting_update"'),
            "新增会议状态事件必须统一经过 _emit_meeting_update 授权",
        )


class AsyncBroadcastTenantResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "async-broadcast.db")
        db.conn()
        self.job_id = db.insert(
            "job",
            {
                "tenant_id": 2,
                "brief_json": "{}",
                "status": "running",
            },
        )

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def test_cache_miss_under_exclusive_sqlite_lock_does_not_stop_heartbeat(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        locked = threading.Event()

        def hold_exclusive_lock():
            connection = sqlite3.connect(
                db.DB_PATH,
                timeout=2,
                check_same_thread=False,
            )
            try:
                connection.execute("PRAGMA locking_mode=EXCLUSIVE")
                connection.execute("BEGIN EXCLUSIVE")
                connection.execute(
                    "UPDATE job SET updated_at=updated_at WHERE id=?",
                    (self.job_id,),
                )
                locked.set()
                time.sleep(0.4)
                connection.rollback()
            finally:
                connection.close()

        locker = threading.Thread(target=hold_exclusive_lock, daemon=True)
        locker.start()
        self.assertTrue(await asyncio.to_thread(locked.wait, 2))

        engine = Engine()
        engine._loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        engine.subscribers[queue] = (
            2,
            False,
            frozenset({"content"}),
            "member",
        )
        started = asyncio.get_running_loop().time()
        engine.broadcast({"type": "job_update", "job_id": self.job_id})
        await asyncio.sleep(0.05)
        heartbeat_elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(
            heartbeat_elapsed,
            0.2,
            "租户解析不得在事件循环等待 SQLite 锁",
        )
        event = await asyncio.wait_for(queue.get(), timeout=2)
        self.assertEqual("job_update", event["type"])
        locker.join(timeout=2)
        self.assertFalse(locker.is_alive())

    async def test_explicit_tenant_id_never_calls_database(self):
        engine = Engine()
        queue = asyncio.Queue()
        engine.subscribers[queue] = (
            2,
            False,
            frozenset({"avatar"}),
            "member",
        )
        with patch.object(
            db,
            "aone",
            side_effect=AssertionError("显式 tenant_id 不应查询数据库"),
        ):
            engine.broadcast(
                {
                    "type": "avatar_step",
                    "tenant_id": 2,
                    "job_id": 7,
                    "step": {"k": "working", "l": "内部执行细节"},
                }
            )
        self.assertEqual("avatar_step", queue.get_nowait()["type"])
