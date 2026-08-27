"""会议 API 的已完成任务引用必须继续遵守租户与回收站边界。"""
import asyncio
import json
import os
import tempfile
import unittest

from app import auth, db, departments, employeeidentity, main, meeting


class MeetingApiIsolationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "fresh.db")
        db.conn()
        self.member_snapshot_json = json.dumps(
            employeeidentity.member_snapshots([0, 1], active_only=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.auto_employee = next(
            employee
            for employee in departments.specialists().values()
            if employee["dept_key"] == "auto"
        )
        auth.set_current({
            "id": 1,
            "tenant_id": 1,
            "role": "root",
            "modules": [],
        })
        db.insert("tenants", {"id": 2, "name": "其他企业", "balance": 10})

    def tearDown(self):
        auth.set_current(None)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    @staticmethod
    def _task_identity(idx: int) -> dict:
        employee = employeeidentity.active_employee(idx)
        if not employee:
            raise AssertionError(f"测试员工 {idx} 不可用")
        return employeeidentity.task_fields(employee)

    @staticmethod
    def _action():
        task = "完成本企业趋势报告"
        return {
            "idx": 0,
            "who": "趋势官",
            "task": task,
            "acceptance": "报告可核验",
            "key": meeting._action_key(0, task),
        }

    def _completed_meeting(self, task_ids):
        return db.insert("meeting", {
            "tenant_id": 1,
            "question": "是否执行趋势报告",
            "emp_idxs_json": "[0,1]",
            "member_snapshot_json": self.member_snapshot_json,
            "status": "done",
            "phase": "completed",
            "decision": "GO",
            "actions_json": json.dumps([self._action()], ensure_ascii=False),
            "execution_task_ids_json": json.dumps(task_ids),
            "billing_status": "included",
        })

    def test_completed_execute_drops_foreign_task_id_and_repairs_meeting(self):
        mid = self._completed_meeting([])
        foreign_id = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": 0,
            **self._task_identity(0),
            "brief_json": "{}",
            "status": "done",
            "source_meeting_id": mid,
            "source_action_key": self._action()["key"],
            "billing_status": "included",
        })
        db.update("meeting", mid, {
            "execution_task_ids_json": json.dumps([foreign_id]),
        })

        response = asyncio.run(main.meeting_execute(mid))

        self.assertEqual({"ok": True, "task_ids": []}, response)
        self.assertEqual(
            [],
            db.jloads(
                db.one(
                    "SELECT execution_task_ids_json FROM meeting WHERE id=?",
                    (mid,),
                )["execution_task_ids_json"],
                [],
            ),
        )
        listed = next(row for row in main.meetings_list() if row["id"] == mid)
        self.assertEqual(0, listed["task_count"])

    def test_completed_execute_drops_deleted_task_id(self):
        mid = self._completed_meeting([])
        local_id = db.insert("task", {
            "tenant_id": 1,
            "emp_idx": 0,
            **self._task_identity(0),
            "brief_json": "{}",
            "status": "done",
            "source_meeting_id": mid,
            "source_action_key": self._action()["key"],
            "billing_status": "included",
            "deleted_at": 123,
        })
        db.update("meeting", mid, {
            "execution_task_ids_json": json.dumps([local_id]),
        })

        response = asyncio.run(main.meeting_execute(mid))

        self.assertEqual({"ok": True, "task_ids": []}, response)
        self.assertEqual(
            [],
            db.jloads(
                db.one(
                    "SELECT execution_task_ids_json FROM meeting WHERE id=?",
                    (mid,),
                )["execution_task_ids_json"],
                [],
            ),
        )

    def test_detail_does_not_expose_same_tenant_unrelated_module_task(self):
        auth.set_current({
            "id": 8,
            "tenant_id": 1,
            "role": "member",
            "modules": ["content"],
        })
        secret = "AUTO_MODULE_SECRET_13c4a15f"
        unrelated_idx = self.auto_employee["idx"]
        unrelated_id = db.insert("task", {
            "tenant_id": 1,
            "emp_idx": unrelated_idx,
            **self._task_identity(unrelated_idx),
            "brief_json": json.dumps({"direction": secret}),
            "status": "done",
            "billing_status": "included",
        })
        action = self._action() | {"task_id": unrelated_id}
        mid = db.insert("meeting", {
            "tenant_id": 1,
            "question": "内容部门会议",
            "emp_idxs_json": "[0,1]",
            "member_snapshot_json": self.member_snapshot_json,
            "status": "done",
            "phase": "completed",
            "decision": "GO",
            "actions_json": json.dumps([action], ensure_ascii=False),
            "execution_task_ids_json": json.dumps([unrelated_id]),
            "billing_status": "included",
        })

        response = main.meeting_get(mid)

        self.assertEqual([], response["execution_tasks"])
        self.assertNotIn("task_id", response["actions"][0])
        self.assertNotIn(secret, json.dumps(response, ensure_ascii=False))

    def test_completed_execute_rejects_task_whose_brief_does_not_match_action(self):
        mid = self._completed_meeting([])
        spoofed_id = db.insert("task", {
            "tenant_id": 1,
            "emp_idx": 0,
            **self._task_identity(0),
            "brief_json": json.dumps({
                "direction": "与会议行动完全无关的任务",
            }, ensure_ascii=False),
            "status": "done",
            "source_meeting_id": mid,
            "source_action_key": self._action()["key"],
            "billing_status": "included",
        })
        db.update("meeting", mid, {
            "execution_task_ids_json": json.dumps([spoofed_id]),
        })

        response = asyncio.run(main.meeting_execute(mid))

        self.assertEqual({"ok": True, "task_ids": []}, response)
        self.assertEqual(
            [],
            db.jloads(
                db.one(
                    "SELECT execution_task_ids_json FROM meeting WHERE id=?",
                    (mid,),
                )["execution_task_ids_json"],
                [],
            ),
        )


if __name__ == "__main__":
    unittest.main()
