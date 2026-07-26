"""Meeting execution handoff must follow the real derived-task lifecycle."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app import auth, db, main, taskrunner


FOREIGN_SENTINEL = "FOREIGN_TENANT_DELIVERY_SECRET_7f41e8b186fd"


class MeetingDeliveryBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "meeting-delivery.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "本租户", "balance": 10})
        db.insert("tenants", {"id": 2, "name": "其他租户", "balance": 10})
        self.user_id = db.insert("users", {
            "tenant_id": 1,
            "username": "meeting-owner",
            "password_hash": "fixture",
            "role": "owner",
            "modules_json": json.dumps(["content"]),
            "enabled": 1,
            "must_change_password": 0,
        })
        auth.set_current({
            "id": self.user_id,
            "tenant_id": 1,
            "username": "meeting-owner",
            "role": "owner",
            "modules": ["content"],
        })
        self.meeting_id = db.insert("meeting", {
            "tenant_id": 1,
            "question": "完成本周发布",
            "emp_idxs_json": "[0,1]",
            "status": "done",
            "phase": "completed",
            "decision": "GO",
            "consensus_md": "# 会议共识\n\n## Next Action\n开始执行",
            "summary_md": "GO · 已锁定两个行动",
            "next_action": "开始执行",
            "billing_status": "included",
        })

    def tearDown(self):
        auth.set_current(None)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _register_action(self, key: str, idx: int, task: str):
        meeting = self._meeting()
        actions = db.jloads(meeting.get("actions_json"), []) or []
        actions.append({
            "key": key,
            "idx": idx,
            "who": f"员工 {idx}",
            "task": task,
            "acceptance": "形成可核验交付",
        })
        db.update(
            "meeting",
            self.meeting_id,
            {"actions_json": json.dumps(actions, ensure_ascii=False)},
        )

    def _task(self, *, tenant_id=1, status="queued", direction="执行行动",
              output="", summary="", deleted_at=None, emp_idx=0,
              action_key=None, action_idx=None, register_action=True):
        key = action_key or f"action-{tenant_id}-{direction}"
        if register_action:
            self._register_action(
                key,
                emp_idx if action_idx is None else action_idx,
                direction,
            )
        return db.insert("task", {
            "tenant_id": tenant_id,
            "emp_idx": emp_idx,
            "brief_json": json.dumps(
                {"direction": direction}, ensure_ascii=False
            ),
            "status": status,
            "output_md": output or None,
            "summary_md": summary or None,
            "source_meeting_id": self.meeting_id,
            "source_action_key": key,
            "billing_status": "included",
            "billing_points": 0,
            "deleted_at": deleted_at,
        })

    def _meeting(self):
        return db.one("SELECT * FROM meeting WHERE id=?", (self.meeting_id,))

    def test_done_results_replace_one_marker_idempotently(self):
        first = self._task(
            status="done",
            direction="完成样品",
            output="# 完整结果\n这是完整正文",
            summary="样品已完成并通过内部评审",
        )
        second = self._task(status="running", direction="整理证据")

        self.assertTrue(taskrunner.sync_meeting_delivery_for_task(first))
        self.assertTrue(taskrunner.sync_meeting_delivery_for_task(first))
        partial = self._meeting()
        self.assertEqual(
            1,
            partial["consensus_md"].count(taskrunner._MEETING_DELIVERY_START),
        )
        self.assertEqual(
            1,
            partial["consensus_md"].count(taskrunner._MEETING_DELIVERY_END),
        )
        self.assertIn("样品已完成并通过内部评审", partial["consensus_md"])
        self.assertIn("1/2 已交付", partial["next_action"])

        db.update("task", second, {
            "status": "done",
            "output_md": "# 证据清单\n已形成三条可追溯证据",
            "summary_md": "三条证据已整理",
        })
        self.assertTrue(taskrunner.sync_meeting_delivery_for_task(second))
        completed = self._meeting()
        self.assertEqual(
            1,
            completed["consensus_md"].count(
                taskrunner._MEETING_DELIVERY_START
            ),
        )
        self.assertIn("三条证据已整理", completed["consensus_md"])
        self.assertIn("2/2", completed["next_action"])
        self.assertIn("执行交付：2/2 已完成", completed["summary_md"])

    def test_failure_and_retry_refresh_meeting_immediately(self):
        task_id = self._task(status="running", direction="核验市场证据")

        self.assertTrue(taskrunner.settle_failure(
            task_id,
            "任务执行失败，已安全收口，可免费重试",
        ))
        failed = self._meeting()
        self.assertIn("执行失败，可免费重试", failed["consensus_md"])
        self.assertIn("1 个会议派生任务失败", failed["next_action"])

        self.assertTrue(taskrunner.prepare_retry(task_id, 1))
        retried = self._meeting()
        self.assertIn("执行中", retried["consensus_md"])
        self.assertNotIn("执行失败，可免费重试", retried["consensus_md"])
        self.assertIn("0/1 已交付", retried["next_action"])

    def test_delete_and_restore_refresh_same_handoff_marker(self):
        task_id = self._task(
            status="done",
            direction="生成发布清单",
            summary="发布清单已完成",
        )
        taskrunner.sync_meeting_delivery_for_task(task_id)

        with patch.object(main.llm, "kill"):
            self.assertTrue(main.task_delete(task_id)["soft_deleted"])
        deleted = self._meeting()
        self.assertIn("已移入回收站", deleted["consensus_md"])
        self.assertEqual(
            1,
            deleted["consensus_md"].count(taskrunner._MEETING_DELIVERY_START),
        )

        self.assertTrue(main.trash_restore("task", task_id)["restored"])
        restored = self._meeting()
        self.assertIn("发布清单已完成", restored["consensus_md"])
        self.assertNotIn("已移入回收站", restored["consensus_md"])
        self.assertEqual(
            1,
            restored["consensus_md"].count(taskrunner._MEETING_DELIVERY_START),
        )

    def test_foreign_tenant_task_cannot_enter_or_mutate_meeting_handoff(self):
        local = self._task(
            status="done",
            direction="本租户行动",
            summary="本租户交付摘要",
        )
        foreign = self._task(
            tenant_id=2,
            status="done",
            direction="其他租户行动",
            summary=FOREIGN_SENTINEL,
        )

        self.assertTrue(taskrunner.sync_meeting_delivery_for_task(local))
        before = self._meeting()
        self.assertNotIn(FOREIGN_SENTINEL, before["consensus_md"])
        self.assertNotIn(f"任务 #{foreign}", before["consensus_md"])

        self.assertFalse(taskrunner.sync_meeting_delivery_for_task(foreign))
        after = self._meeting()
        self.assertEqual(before["consensus_md"], after["consensus_md"])
        self.assertNotIn(FOREIGN_SENTINEL, after["summary_md"])

    def test_same_tenant_non_participant_task_cannot_enter_handoff(self):
        local = self._task(
            status="done",
            direction="本会议行动",
            summary="本会议交付摘要",
        )
        self.assertTrue(taskrunner.sync_meeting_delivery_for_task(local))
        before = self._meeting()

        auth.set_current({
            "id": self.user_id,
            "tenant_id": 1,
            "username": "content-member",
            "role": "member",
            "modules": ["content"],
        })
        self.assertFalse(auth.allowed("auto"))
        unrelated = self._task(
            status="done",
            direction="未授权板块行动",
            output=FOREIGN_SENTINEL,
            summary=FOREIGN_SENTINEL,
            emp_idx=1601,
        )
        self.assertFalse(taskrunner.sync_meeting_delivery_for_task(unrelated))
        after = self._meeting()
        self.assertEqual(before["consensus_md"], after["consensus_md"])
        self.assertNotIn(FOREIGN_SENTINEL, after["consensus_md"])
        self.assertNotIn(f"任务 #{unrelated}", after["consensus_md"])

    def test_unlisted_action_key_cannot_enter_or_mutate_handoff(self):
        local = self._task(
            status="done",
            direction="白名单内行动",
            summary="白名单交付摘要",
        )
        self.assertTrue(taskrunner.sync_meeting_delivery_for_task(local))
        before = self._meeting()

        unrelated = self._task(
            status="done",
            direction="未列入会议行动",
            output=FOREIGN_SENTINEL,
            summary=FOREIGN_SENTINEL,
            action_key="not-in-meeting-actions",
            register_action=False,
        )
        self.assertFalse(taskrunner.sync_meeting_delivery_for_task(unrelated))
        after = self._meeting()
        self.assertEqual(before["consensus_md"], after["consensus_md"])
        self.assertNotIn(FOREIGN_SENTINEL, after["consensus_md"])
        self.assertNotIn(f"任务 #{unrelated}", after["consensus_md"])

    def test_action_key_owner_idx_mismatch_cannot_enter_handoff(self):
        local = self._task(
            status="done",
            direction="员工零号行动",
            summary="员工零号交付摘要",
        )
        self.assertTrue(taskrunner.sync_meeting_delivery_for_task(local))
        before = self._meeting()

        mismatched = self._task(
            status="done",
            direction="归属不一致行动",
            output=FOREIGN_SENTINEL,
            summary=FOREIGN_SENTINEL,
            emp_idx=1,
            action_key="owner-is-zero",
            action_idx=0,
        )
        self.assertFalse(taskrunner.sync_meeting_delivery_for_task(mismatched))
        after = self._meeting()
        self.assertEqual(before["consensus_md"], after["consensus_md"])
        self.assertNotIn(FOREIGN_SENTINEL, after["consensus_md"])
        self.assertNotIn(f"任务 #{mismatched}", after["consensus_md"])

    def test_action_key_cannot_authorize_a_different_task_brief(self):
        key = "trusted-action-key"
        self._register_action(key, 0, "会议批准的行动")
        spoofed = self._task(
            status="done",
            direction="未经会议批准的不同任务",
            output=FOREIGN_SENTINEL,
            summary=FOREIGN_SENTINEL,
            action_key=key,
            register_action=False,
        )
        before = self._meeting()

        self.assertFalse(taskrunner.sync_meeting_delivery_for_task(spoofed))

        after = self._meeting()
        self.assertEqual(before["consensus_md"], after["consensus_md"])
        self.assertNotIn(FOREIGN_SENTINEL, after["consensus_md"])

    def test_failed_legacy_output_is_never_copied_into_meeting(self):
        task_id = self._task(
            status="failed",
            direction="失败行动",
            output=FOREIGN_SENTINEL,
        )
        self.assertTrue(taskrunner.sync_meeting_delivery_for_task(task_id))
        meeting = self._meeting()
        self.assertIn("执行失败，可免费重试", meeting["consensus_md"])
        self.assertNotIn(FOREIGN_SENTINEL, meeting["consensus_md"])


if __name__ == "__main__":
    unittest.main()
