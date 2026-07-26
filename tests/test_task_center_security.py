"""V29 任务中心相关写入边界：租户、板块权限与先校验后写入。"""
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app import db


class TaskCenterSecurityCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "security.db")
        db.conn()

        # main 延迟到临时库就绪后加载，避免模块初始化碰到开发/生产数据库。
        from app import auth, main
        self.auth = auth
        self.main = main
        self._as_member(["content"])

    def tearDown(self):
        self.auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _as_member(self, modules, tenant_id=1):
        self.auth.set_current({
            "id": 101,
            "tenant_id": tenant_id,
            "role": "member",
            "modules": list(modules),
        })

    @staticmethod
    def _profile(name, tenant_id):
        return db.insert("account_profile", {
            "tenant_id": tenant_id,
            "name": name,
            "persona_json": "{}",
        })

    @staticmethod
    def _schedule(profile_id=None):
        return db.insert("schedule", {
            "tenant_id": 1,
            "name": "每日选题",
            "brief_json": json.dumps({"direction": "行业选题"}, ensure_ascii=False),
            "mode": "copilot",
            "profile_id": profile_id,
            "kind": "daily",
            "at_time": "09:00",
            "enabled": 1,
        })

    async def test_cross_tenant_profile_is_rejected_on_job_and_schedule_writes(self):
        own_profile = self._profile("本企业人设", 1)
        foreign_profile = self._profile("别家企业人设", 2)

        # 内容工单必须在计费、建单、唤醒引擎之前拒绝外租户 profile_id。
        with patch.object(self.main, "_charge") as charge, \
                patch.object(self.main.engine, "notify") as notify, \
                patch.object(self.main.engine, "touch") as touch:
            with self.assertRaises(HTTPException) as denied:
                await self.main.create_job({
                    "brief": {"direction": "新品发布"},
                    "profile_id": foreign_profile,
                })
            self.assertEqual(denied.exception.status_code, 400)
            charge.assert_not_called()
            notify.assert_not_called()
            touch.assert_not_called()
            self.assertIsNone(db.one("SELECT id FROM job LIMIT 1"))

            accepted = await self.main.create_job({
                "brief": {"direction": "本企业新品发布"},
                "profile_id": own_profile,
            })
            self.assertEqual(
                db.one("SELECT profile_id FROM job WHERE id=?", (accepted["job_id"],))["profile_id"],
                own_profile,
            )

        # 定时任务创建和更新都不能引用别家 profile；失败后不得留下半条记录或部分更新。
        before_count = db.one("SELECT COUNT(*) n FROM schedule")["n"]
        with self.assertRaises(HTTPException) as denied:
            self.main.schedule_create({
                "brief": {"direction": "外租户人设排程"},
                "profile_id": foreign_profile,
                "kind": "daily",
                "at_time": "10:00",
            })
        self.assertEqual(denied.exception.status_code, 400)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM schedule")["n"], before_count)

        sid = self._schedule(own_profile)
        before = db.one("SELECT * FROM schedule WHERE id=?", (sid,))
        with self.assertRaises(HTTPException) as denied:
            self.main.schedule_update(sid, {
                "name": "不应写入的名称",
                "profile_id": foreign_profile,
            })
        self.assertEqual(denied.exception.status_code, 400)
        self.assertEqual(db.one("SELECT * FROM schedule WHERE id=?", (sid,)), before)

    def test_schedule_endpoints_require_content_module_before_any_side_effect(self):
        sid = self._schedule()
        before = db.one("SELECT * FROM schedule WHERE id=?", (sid,))
        self._as_member(["avatar"])

        with self.assertRaises(HTTPException) as denied:
            self.main.schedules_list()
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(db.one("SELECT * FROM schedule WHERE id=?", (sid,)), before)

        with self.assertRaises(HTTPException) as denied:
            self.main.schedule_update(sid, {"name": "越权修改"})
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(db.one("SELECT * FROM schedule WHERE id=?", (sid,)), before)

        with patch.object(self.main.scheduler, "fire") as fire:
            with self.assertRaises(HTTPException) as denied:
                self.main.schedule_run_now(sid)
            self.assertEqual(denied.exception.status_code, 403)
            fire.assert_not_called()
        self.assertEqual(db.one("SELECT * FROM schedule WHERE id=?", (sid,)), before)

        with self.assertRaises(HTTPException) as denied:
            self.main.schedule_delete(sid)
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(db.one("SELECT * FROM schedule WHERE id=?", (sid,)), before)

    def test_invalid_schedule_parameters_are_rejected_before_insert_or_update(self):
        invalid_creates = (
            {"kind": "monthly"},
            {"kind": "daily", "at_time": "24:00"},
            {"kind": "weekly", "at_time": "09:00", "weekday": 7},
            {"kind": "interval", "every_hours": -1},
        )
        for timing in invalid_creates:
            with self.subTest(create=timing):
                before_count = db.one("SELECT COUNT(*) n FROM schedule")["n"]
                with self.assertRaises(HTTPException) as denied:
                    self.main.schedule_create({
                        "brief": {"direction": "参数边界测试"},
                        **timing,
                    })
                self.assertEqual(denied.exception.status_code, 400)
                self.assertEqual(db.one("SELECT COUNT(*) n FROM schedule")["n"], before_count)

        sid = self._schedule()
        before = db.one("SELECT * FROM schedule WHERE id=?", (sid,))
        with self.assertRaises(HTTPException) as denied:
            self.main.schedule_update(sid, {
                "name": "绝不能提前写入",
                "kind": "not-a-schedule-kind",
            })
        self.assertEqual(denied.exception.status_code, 400)
        self.assertEqual(db.one("SELECT * FROM schedule WHERE id=?", (sid,)), before)

    def test_interval_zero_is_invalid_and_never_persisted(self):
        """0 小时会造成无意义的高频调度，不能被默认值逻辑吞成 24 小时。"""
        with self.assertRaises(HTTPException) as denied:
            self.main.schedule_create({
                "brief": {"direction": "零间隔测试"},
                "kind": "interval",
                "every_hours": 0,
            })
        self.assertEqual(denied.exception.status_code, 400)
        self.assertIsNone(db.one("SELECT id FROM schedule LIMIT 1"))

    async def test_matrix_publish_rejects_foreign_job_before_enqueue_or_async_run(self):
        foreign_job = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "别家素材"}, ensure_ascii=False),
            "status": "done",
        })

        # run_task 用普通 Mock 替身，确保回归时也不会制造未 await 协程噪声。
        run_task_stub = Mock(return_value=object())
        with patch.object(self.main.matrixpub, "enqueue") as enqueue, \
                patch.object(self.main.matrixpub, "run_task", run_task_stub), \
                patch.object(self.main.asyncio, "create_task") as create_task:
            with self.assertRaises(HTTPException) as denied:
                await self.main.matrix_publish({
                    "platform": "xhs",
                    "account": "account-1",
                    "title": "不得发布",
                    "body": "不得读取外租户工单",
                    "job_id": foreign_job,
                })
            self.assertEqual(denied.exception.status_code, 404)
            enqueue.assert_not_called()
            run_task_stub.assert_not_called()
            create_task.assert_not_called()
        self.assertIsNone(db.one("SELECT id FROM pub_task LIMIT 1"))


if __name__ == "__main__":
    unittest.main()
