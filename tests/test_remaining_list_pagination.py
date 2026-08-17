"""工单、数字人和会议列表必须越过历史硬上限。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from fastapi import HTTPException

from app import auth, db, employeeidentity


class RemainingListPaginationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "remaining-pagination.db")
        db.conn()
        self.member_snapshot_json = json.dumps(
            employeeidentity.member_snapshots([0, 1], active_only=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        db.insert("tenants", {"id": 2, "name": "租户甲"})
        db.insert("tenants", {"id": 3, "name": "租户乙"})
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner-a",
            "role": "owner",
            "modules": ["content", "avatar", "library"],
        })

    def tearDown(self):
        auth.set_current(None)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _insert_many(self, table, count, values):
        return [db.insert(table, values(index)) for index in range(count)]

    def _assert_page(self, page, item_key, limit, offset, total):
        items = page[item_key]
        self.assertEqual(limit, page["limit"])
        self.assertEqual(offset, page["offset"])
        self.assertEqual(total, page["total"])
        self.assertEqual(page["has_more"], page["truncated"])
        expected_next = offset + limit if offset + len(items) < total else None
        self.assertEqual(expected_next, page["next_offset"])

    def test_jobs_cross_100_with_stable_order_and_tenant_isolation(self):
        from app import main

        own_ids = self._insert_many(
            "job",
            105,
            lambda index: {
                "tenant_id": 2,
                "brief_json": json.dumps({
                    "direction": f"工单-{index:03d}",
                    "template": "观点输出",
                    "platforms": ["公众号"],
                }),
                "mode": "copilot",
                "status": "done",
                "current_idx": 9,
            },
        )
        foreign_id = db.insert("job", {
            "tenant_id": 3,
            "brief_json": json.dumps({"direction": "外租户工单"}),
            "mode": "copilot",
            "status": "done",
            "current_idx": 9,
        })

        page = main.state(limit=10, offset=100)

        self._assert_page(page, "jobs", 10, 100, 105)
        self.assertEqual(own_ids[:5][::-1], [row["id"] for row in page["jobs"]])
        self.assertNotIn(foreign_id, [row["id"] for row in page["jobs"]])
        legacy = main.state()
        self.assertIsInstance(legacy["jobs"], list)
        self.assertEqual(100, len(legacy["jobs"]))
        self.assertNotIn("total", legacy)

    def test_avatar_jobs_cross_50_with_stable_order_and_tenant_isolation(self):
        from app import main

        own_ids = self._insert_many(
            "avatar_job",
            55,
            lambda index: {
                "tenant_id": 2,
                "params_json": json.dumps({"script": f"数字人口播-{index:03d}"}),
                "status": "done",
                "billing_status": "charged",
                "steps_json": "[]",
            },
        )
        foreign_id = db.insert("avatar_job", {
            "tenant_id": 3,
            "params_json": json.dumps({"script": "外租户数字人"}),
            "status": "done",
            "billing_status": "charged",
            "steps_json": "[]",
        })

        page = main.avatar_jobs(limit=10, offset=50)

        self._assert_page(page, "items", 10, 50, 55)
        self.assertEqual(own_ids[:5][::-1], [row["id"] for row in page["items"]])
        self.assertNotIn(foreign_id, [row["id"] for row in page["items"]])
        self.assertIsInstance(main.avatar_jobs(), list)
        self.assertEqual(50, len(main.avatar_jobs()))

    def test_meetings_cross_30_with_stable_order_and_tenant_isolation(self):
        from app import main

        own_ids = self._insert_many(
            "meeting",
            35,
            lambda index: {
                "tenant_id": 2,
                "question": f"会议-{index:03d}",
                "emp_idxs_json": "[0,1]",
                "member_snapshot_json": self.member_snapshot_json,
                "status": "done",
                "phase": "completed",
                "execution_task_ids_json": "[]",
                "actions_json": "[]",
            },
        )
        foreign_id = db.insert("meeting", {
            "tenant_id": 3,
            "question": "外租户会议",
            "emp_idxs_json": "[0,1]",
            "member_snapshot_json": self.member_snapshot_json,
            "status": "done",
            "phase": "completed",
            "execution_task_ids_json": "[]",
            "actions_json": "[]",
        })

        page = main.meetings_list(limit=10, offset=30)

        self._assert_page(page, "items", 10, 30, 35)
        self.assertEqual(own_ids[:5][::-1], [row["id"] for row in page["items"]])
        self.assertNotIn(foreign_id, [row["id"] for row in page["items"]])
        self.assertIsInstance(main.meetings_list(), list)
        self.assertEqual(30, len(main.meetings_list()))

    def test_module_boundaries_and_invalid_pagination(self):
        from app import main

        db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "无权查看"}),
            "mode": "copilot",
            "status": "done",
            "current_idx": 9,
        })
        db.insert("meeting", {
            "tenant_id": 2,
            "question": "内容部会议",
            "emp_idxs_json": "[0,1]",
            "member_snapshot_json": self.member_snapshot_json,
            "status": "done",
            "phase": "completed",
            "execution_task_ids_json": "[]",
            "actions_json": "[]",
        })
        auth.set_current({
            "id": 21,
            "tenant_id": 2,
            "username": "member-a",
            "role": "member",
            "modules": [],
        })

        state_page = main.state(limit=10, offset=0)
        self._assert_page(state_page, "jobs", 10, 0, 0)
        meeting_page = main.meetings_list(limit=10, offset=0)
        self._assert_page(meeting_page, "items", 10, 0, 0)
        with self.assertRaises(HTTPException) as caught:
            main.avatar_jobs(limit=10, offset=0)
        self.assertEqual(403, caught.exception.status_code)

        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner-a",
            "role": "owner",
            "modules": ["content", "avatar", "library"],
        })
        cases = (
            (main.state, "jobs"),
            (main.avatar_jobs, "items"),
            (main.meetings_list, "items"),
        )
        for function, item_key in cases:
            with self.subTest(function=function.__name__):
                empty = function(limit=100, offset=1_000_000)
                self._assert_page(
                    empty,
                    item_key,
                    100,
                    1_000_000,
                    empty["total"],
                )
                self.assertEqual([], empty[item_key])
                with self.assertRaises(HTTPException) as caught:
                    function(limit=0, offset=0)
                self.assertEqual(422, caught.exception.status_code)
                with self.assertRaises(HTTPException) as caught:
                    function(limit=10, offset=-1)
                self.assertEqual(422, caught.exception.status_code)
                with self.assertRaises(HTTPException) as caught:
                    function(limit=101, offset=0)
                self.assertEqual(422, caught.exception.status_code)
                with self.assertRaises(HTTPException) as caught:
                    function(limit=10, offset=1_000_001)
                self.assertEqual(422, caught.exception.status_code)
