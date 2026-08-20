"""schema56 会议 Agent 团队协作执行：接力材料、队长整合、幂等与重启恢复。"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import db, employeeidentity, meeting


MEMBERS = [
    {"idx": 0, "name": "趋势官", "duty": "趋势与市场情报", "md": "内部岗位手册",
     "color": "#111111", "emoji": "📡"},
    {"idx": 1, "name": "情报官", "duty": "调研与风险核验", "md": "内部技能库",
     "color": "#222222", "emoji": "🔎"},
]

RAW_ACTIONS = [
    {"idx": 0, "task": "完成首版样品", "acceptance": "样品可评审"},
    {"idx": 1, "task": "完成证据清单", "acceptance": "至少三条来源"},
]


class MeetingTeamCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "fresh.db")
        db.conn()

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    @staticmethod
    def _team_meeting(**overrides):
        row = {
            "tenant_id": 1,
            "question": "新品是否应在本周上线？",
            "emp_idxs_json": "[0,1]",
            "auto_execute": 1,
            "team_execute": 1,
            "status": "running",
            "phase": "execute",
            "round_no": 3,
            "decision": "GO",
            "consensus_md": "# 会议共识\n先做小样",
            "actions_json": json.dumps(RAW_ACTIONS, ensure_ascii=False),
        }
        row.update(overrides)
        idxs = json.loads(row.get("emp_idxs_json") or "[]")
        row.setdefault(
            "member_snapshot_json",
            json.dumps(
                employeeidentity.member_snapshots(idxs, active_only=True),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return db.insert("meeting", row)

    async def test_fresh_database_has_team_execute_column_and_v56_ledger(self):
        cols = {r["name"] for r in db.q("PRAGMA table_info(meeting)")}
        self.assertIn("team_execute", cols)
        row = db.one(
            "SELECT name FROM schema_version WHERE version=56"
        )
        self.assertEqual("meeting-agent-team-relay", (row or {}).get("name"))
        self.assertEqual(
            56, db.one("PRAGMA user_version")["user_version"]
        )

    async def test_team_relay_passes_prior_deliveries_and_lead_integrates(self):
        mid = self._team_meeting()

        async def fake_run_task(task_id, _broadcast):
            db.update("task", task_id, {
                "status": "done",
                "output_md": f"交付正文-{task_id}",
            })

        with patch("app.taskrunner.run_task", new=AsyncMock(side_effect=fake_run_task)), \
                patch.object(meeting, "TEAM_POLL_SECONDS", 0.01):
            task_ids = await meeting.execute_actions_team(mid, lambda _e: None)

        self.assertEqual(3, len(task_ids))
        rows = db.q(
            "SELECT id,emp_idx,status,brief_json FROM task "
            "WHERE source_meeting_id=? ORDER BY id",
            (mid,),
        )
        self.assertEqual(3, len(rows))
        first, second, integration = rows
        self.assertEqual([0, 1, 0], [r["emp_idx"] for r in rows])
        first_brief = db.jloads(first["brief_json"], {})
        second_brief = db.jloads(second["brief_json"], {})
        integration_brief = db.jloads(integration["brief_json"], {})
        # 第一棒没有队友交付；第二棒必须带上第一棒的交付正文。
        self.assertNotIn("队友已交付", first_brief["material"])
        self.assertIn("队友已交付", second_brief["material"])
        self.assertIn(f"交付正文-{first['id']}", second_brief["material"])
        # 队长整合任务：固定文本 + 两位成员的交付都在材料里。
        self.assertEqual(meeting.TEAM_INTEGRATION_TASK, integration_brief["direction"])
        self.assertIn(f"交付正文-{first['id']}", integration_brief["material"])
        self.assertIn(f"交付正文-{second['id']}", integration_brief["material"])

        m = db.one("SELECT * FROM meeting WHERE id=?", (mid,))
        self.assertEqual(("done", "completed"), (m["status"], m["phase"]))
        actions = db.jloads(m["actions_json"], [])
        self.assertEqual(3, len(actions))
        self.assertEqual("integrate", actions[-1].get("team_role"))
        self.assertEqual(
            [r["id"] for r in rows],
            [a.get("task_id") for a in actions],
        )
        self.assertEqual(
            [r["id"] for r in rows],
            db.jloads(m["execution_task_ids_json"], []),
        )
        # 完整性校验必须认下整合任务（actions_json 合同内）。
        self.assertEqual(
            [r["id"] for r in rows],
            meeting.validated_execution_task_ids(m),
        )
        messages = db.jloads(m["messages_json"], [])
        texts = [item.get("text", "") for item in messages]
        self.assertTrue(any("Agent 团队作战开始" in t for t in texts))
        self.assertTrue(any("队长整合" in t for t in texts))
        self.assertTrue(any("Agent 团队收官" in t for t in texts))

    async def test_team_relay_is_idempotent_on_reentry(self):
        mid = self._team_meeting()

        async def fake_run_task(task_id, _broadcast):
            db.update("task", task_id, {
                "status": "done", "output_md": f"交付-{task_id}",
            })

        with patch("app.taskrunner.run_task", new=AsyncMock(side_effect=fake_run_task)), \
                patch.object(meeting, "TEAM_POLL_SECONDS", 0.01):
            first_ids = await meeting.execute_actions_team(mid, lambda _e: None)
            # 模拟重启后再次进入编排器：不允许生成第二批任务。
            db.update("meeting", mid, {"status": "running", "phase": "executing"})
            second_ids = await meeting.execute_actions_team(mid, lambda _e: None)

        self.assertEqual(first_ids, second_ids)
        n = db.one(
            "SELECT COUNT(*) AS n FROM task WHERE source_meeting_id=?", (mid,)
        )["n"]
        self.assertEqual(3, n)

    async def test_team_relay_continues_after_member_failure(self):
        mid = self._team_meeting()
        calls = {"n": 0}

        async def fake_run_task(task_id, _broadcast):
            calls["n"] += 1
            if calls["n"] == 1:
                db.update("task", task_id, {
                    "status": "failed", "output_md": "执行失败",
                })
            else:
                db.update("task", task_id, {
                    "status": "done", "output_md": f"交付-{task_id}",
                })

        with patch("app.taskrunner.run_task", new=AsyncMock(side_effect=fake_run_task)), \
                patch.object(meeting, "TEAM_POLL_SECONDS", 0.01):
            task_ids = await meeting.execute_actions_team(mid, lambda _e: None)

        self.assertEqual(3, len(task_ids))
        rows = db.q(
            "SELECT id,status,brief_json FROM task "
            "WHERE source_meeting_id=? ORDER BY id",
            (mid,),
        )
        self.assertEqual(["failed", "done", "done"], [r["status"] for r in rows])
        integration_brief = db.jloads(rows[-1]["brief_json"], {})
        # 失败棒的内容不得混进整合材料；成功棒必须在。
        self.assertNotIn(f"交付-{rows[0]['id']}", integration_brief["material"])
        self.assertIn(f"交付-{rows[1]['id']}", integration_brief["material"])
        m = db.one("SELECT status,phase FROM meeting WHERE id=?", (mid,))
        self.assertEqual(("done", "completed"), (m["status"], m["phase"]))

    async def test_parallel_meetings_are_not_hijacked_by_team_path(self):
        mid = self._team_meeting(team_execute=0)
        with patch.object(
            meeting, "execute_actions", new=AsyncMock(return_value=[7])
        ) as parallel:
            returned = await meeting.execute_actions_team(mid, lambda _e: None)
        self.assertEqual([7], returned)
        parallel.assert_awaited_once()

    async def test_resume_pending_routes_team_meetings_to_team_orchestrator(self):
        mid = self._team_meeting(status="running", phase="executing")
        with patch.object(
            meeting, "execute_actions_team", new=AsyncMock(return_value=[])
        ) as team, patch.object(
            meeting, "execute_actions", new=AsyncMock(return_value=[])
        ) as parallel:
            meeting.resume_pending(lambda _e: None)
            await asyncio.sleep(0.05)
        team.assert_awaited_once()
        parallel.assert_not_awaited()

    async def test_failed_preparation_returns_meeting_to_manual_execution(self):
        # 身份快照损坏：一棒都派不出去时，回到 awaiting_execution 等老板重试。
        mid = self._team_meeting(member_snapshot_json="[]")
        with patch.object(meeting, "TEAM_POLL_SECONDS", 0.01):
            returned = await meeting.execute_actions_team(mid, lambda _e: None)
        self.assertEqual([], returned)
        m = db.one("SELECT status,phase FROM meeting WHERE id=?", (mid,))
        self.assertEqual(("done", "awaiting_execution"), (m["status"], m["phase"]))


if __name__ == "__main__":
    unittest.main()
