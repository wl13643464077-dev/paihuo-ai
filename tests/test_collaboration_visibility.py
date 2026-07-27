"""副账号协作可见:谁发起的、谁拍板的,以及 member 代拍板后的老板通知。"""

import json
import os
import tempfile
import unittest

from app import auth, db, main
from app.engine import engine


OWNER = {
    "id": 20,
    "tenant_id": 2,
    "username": "boss2",
    "role": "owner",
    "modules": [],
}
MEMBER = {
    "id": 21,
    "tenant_id": 2,
    "username": "xiaowang",
    "role": "member",
    "modules": ["content"],
}


class CollaborationVisibilityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "collab.db")
        db.conn()
        db.insert(
            "tenants",
            {"id": 2, "name": "测试企业", "balance": 100, "enabled": 1},
        )
        db.insert(
            "tenants",
            {"id": 3, "name": "别家企业", "balance": 100, "enabled": 1},
        )
        for uid, tid, name, role in (
            (20, 2, "boss2", "owner"),
            (21, 2, "xiaowang", "member"),
            (30, 3, "wairen", "owner"),
        ):
            db.insert(
                "users",
                {
                    "id": uid,
                    "tenant_id": tid,
                    "username": name,
                    "password_hash": "x",
                    "role": role,
                    "modules_json": json.dumps(["content"]),
                    "enabled": 1,
                },
            )
        auth.set_current(dict(OWNER))

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    # ---------- 工具 ----------
    def _job_awaiting(self, idx=3, **extra):
        """一张停在工位 idx 等审批的已扣费工单。"""
        job_id = db.insert(
            "job",
            {
                "tenant_id": 2,
                "brief_json": json.dumps({"direction": "协作测试"}),
                "mode": "manual",
                "status": "awaiting_review",
                "current_idx": idx,
                "billing_status": "charged",
                **extra,
            },
        )
        run_id = db.insert(
            "station_run",
            {
                "job_id": job_id,
                "station_idx": idx,
                "version": 1,
                "status": "awaiting_review",
                "output_json": "{}",
            },
        )
        return job_id, run_id

    @staticmethod
    def _member_notices():
        return db.q(
            "SELECT * FROM notification "
            "WHERE tenant_id=2 AND kind='member_reviewed'"
        )

    # ---------- 发起人落库 ----------
    def test_job_and_task_creation_record_initiator(self):
        job_id = main._create_charged_content_job(
            {
                "brief_json": json.dumps({"direction": "记录发起人"}),
                "tenant_id": 2,
                "mode": "copilot",
            },
            note="记录发起人",
        )
        self.assertEqual(
            20,
            db.one("SELECT created_by FROM job WHERE id=?", (job_id,))[
                "created_by"
            ],
        )

        task_id = main._create_charged_expert_task(
            {
                "emp_idx": 0,
                "tenant_id": 2,
                "brief_json": json.dumps({"direction": "专家任务发起人"}),
            },
            note="专家任务",
        )
        self.assertEqual(
            20,
            db.one("SELECT created_by FROM task WHERE id=?", (task_id,))[
                "created_by"
            ],
        )

    # ---------- 拍板人落库 + member 通知 ----------
    def test_member_approve_records_reviewer_and_notifies_boss(self):
        job_id, run_id = self._job_awaiting()
        auth.set_current(dict(MEMBER))

        self.assertTrue(
            main.station_action(job_id, 3, {"action": "approve", "payload": {}})[
                "ok"
            ]
        )
        run = db.one("SELECT * FROM station_run WHERE id=?", (run_id,))
        self.assertEqual("done", run["status"])
        self.assertEqual(21, run["reviewed_by"])

        notices = self._member_notices()
        self.assertEqual(1, len(notices))
        self.assertIn("xiaowang", notices[0]["title"])
        self.assertIn(f"工单#{job_id}", notices[0]["title"])
        self.assertIn("通过", notices[0]["title"])
        self.assertEqual(f"#/job/{job_id}", notices[0]["link"])

    def test_member_reject_records_reviewer_and_notifies_boss(self):
        job_id, run_id = self._job_awaiting()
        auth.set_current(dict(MEMBER))

        main.station_action(
            job_id,
            3,
            {"action": "reject", "payload": {"comment": "开头不够抓人"}},
        )
        run = db.one("SELECT * FROM station_run WHERE id=?", (run_id,))
        self.assertEqual("rejected", run["status"])
        self.assertEqual(21, run["reviewed_by"])

        notices = self._member_notices()
        self.assertEqual(1, len(notices))
        self.assertIn("xiaowang", notices[0]["title"])
        self.assertIn("打回", notices[0]["title"])
        self.assertEqual(f"#/job/{job_id}", notices[0]["link"])

    def test_owner_approve_records_reviewer_without_member_notice(self):
        job_id, run_id = self._job_awaiting()

        main.station_action(job_id, 3, {"action": "approve", "payload": {}})
        self.assertEqual(
            20,
            db.one("SELECT reviewed_by FROM station_run WHERE id=?", (run_id,))[
                "reviewed_by"
            ],
        )
        self.assertEqual([], self._member_notices())

    # ---------- 详情响应带用户名 + 跨租户不泄露 ----------
    def test_job_detail_exposes_names_but_never_foreign_users(self):
        job_id, run_id = self._job_awaiting(created_by=20)
        db.update(
            "station_run", run_id, {"status": "done", "reviewed_by": 21}
        )
        db.q("UPDATE job SET status='running' WHERE id=?", (job_id,))

        detail = main.job_detail(job_id)
        self.assertEqual("boss2", detail["created_by_name"])
        run = detail["runs"][3]
        self.assertEqual("xiaowang", run["reviewed_by_name"])
        # 非 root 视角:内部字段(技能编号/原始拍板人 id)不外泄,用户名可给
        self.assertNotIn("skill_id", run)
        self.assertNotIn("reviewed_by", run)

        # 脏数据指向别家租户的用户时,名字绝不出现在响应里
        foreign_job, foreign_run = self._job_awaiting(created_by=30)
        db.update(
            "station_run", foreign_run, {"status": "done", "reviewed_by": 30}
        )
        db.q("UPDATE job SET status='running' WHERE id=?", (foreign_job,))
        foreign = main.job_detail(foreign_job)
        self.assertIsNone(foreign["created_by_name"])
        self.assertIsNone(foreign["runs"][3]["reviewed_by_name"])
        self.assertNotIn("wairen", json.dumps(foreign, ensure_ascii=False))

    def test_task_detail_exposes_initiator_within_tenant_only(self):
        def _task(created_by):
            return db.insert(
                "task",
                {
                    "tenant_id": 2,
                    "emp_idx": 0,
                    "brief_json": json.dumps({"direction": "详情发起人"}),
                    "status": "done",
                    "billing_status": "charged",
                    "created_by": created_by,
                },
            )

        self.assertEqual(
            "boss2", main.task_get(_task(20))["created_by_name"]
        )
        foreign = main.task_get(_task(30))
        self.assertIsNone(foreign["created_by_name"])
        self.assertNotIn("wairen", json.dumps(foreign, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
