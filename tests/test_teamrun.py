"""手机小队运行：服务端状态、依赖调度、租户隔离与幂等恢复。"""
import asyncio
import json
import os
import tempfile
import unittest

from app import db, teamrun, teamrun_schema


TEAM = {
    "teamName": "周年庆小队",
    "summary": "策划、执行和复核协作",
    "members": [
        {"idx": 101, "name": "队长", "role": "策划顾问", "roleInTeam": "队长",
         "task": "拆解周年庆目标和分工", "dependsOn": []},
        {"idx": 102, "name": "物料官", "role": "活动策划", "roleInTeam": "策划",
         "task": "生成活动物料清单", "dependsOn": [101]},
        {"idx": 103, "name": "投放官", "role": "流量增长", "roleInTeam": "执行",
         "task": "设计引流执行计划", "dependsOn": [101]},
    ],
}


class TeamRunCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "teamrun.db")
        db.conn()
        with db.atomic() as connection:
            teamrun_schema.install_schema(connection)
        db.insert("tenants", {"id": 2, "name": "甲公司"})
        db.insert("tenants", {"id": 3, "name": "乙公司"})
        self.sent = []

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def dispatch(self, body):
        self.sent.append(body)
        task_id = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": body["emp_idx"],
            "brief_json": json.dumps(body["brief"], ensure_ascii=False),
            "status": "queued",
            "request_key": body["request_key"],
        })
        return {"task_id": task_id, "created": True}

    def create(self, *, mode="auto", depth="comprehensive", key="team-request-0001"):
        return teamrun.create_run(
            2, 11, "做一场门店周年庆", TEAM,
            mode=mode, depth=depth, request_key=key,
        )

    async def test_auto_plan_parallel_members_and_final_summary(self):
        run = self.create(depth="professional")
        run_id = run["id"]
        self.assertEqual(4, run["estimated_tasks"])
        self.assertEqual(4, run["estimated_points"])
        first = await teamrun.advance_run(run_id, 2, self.dispatch)
        self.assertEqual(1, len(self.sent))
        self.assertEqual(101, self.sent[0]["emp_idx"])
        self.assertEqual("full", self.sent[0]["brief"]["length"])
        self.assertIn("前置拆解", self.sent[0]["brief"]["direction"])
        leader_task = first["members"][0]["task_id"]
        db.update("task", leader_task, {"status": "done", "output_md": "先调研客群，再出海报"})

        second = await teamrun.advance_run(run_id, 2, self.dispatch)
        self.assertEqual({102, 103}, {x["emp_idx"] for x in self.sent[1:]})
        self.assertTrue(all("先调研客群" in x["brief"]["material"] for x in self.sent[1:]))
        for m in second["members"][1:]:
            db.update("task", m["task_id"], {
                "status": "done", "output_md": f"{m['name']}真实交付",
            })

        last = await teamrun.advance_run(run_id, 2, self.dispatch)
        self.assertEqual(4, len(self.sent))
        self.assertEqual(101, self.sent[-1]["emp_idx"])
        self.assertIn("物料官真实交付", self.sent[-1]["brief"]["material"])
        self.assertIn("投放官真实交付", self.sent[-1]["brief"]["material"])
        db.update("task", last["summary_task_id"], {
            "status": "done", "output_md": "最终汇总",
        })
        complete = await teamrun.advance_run(run_id, 2, self.dispatch)
        self.assertEqual("done", complete["status"])
        self.assertEqual("最终汇总", complete["summary_output_md"])
        self.assertEqual(4, len(self.sent))

    async def test_semi_requires_each_approval_and_depth_is_simple(self):
        run = self.create(mode="semi", depth="simple")
        rid = run["id"]
        await teamrun.advance_run(rid, 2, self.dispatch)
        self.assertEqual([], self.sent)
        teamrun.approve_member(rid, 2, run["members"][0]["id"])
        after_lead = await teamrun.advance_run(rid, 2, self.dispatch)
        self.assertEqual(1, len(self.sent))
        self.assertEqual("lite", self.sent[0]["brief"]["length"])
        db.update("task", after_lead["members"][0]["task_id"], {
            "status": "done", "output_md": "拆解结果",
        })
        await teamrun.advance_run(rid, 2, self.dispatch)
        self.assertEqual(1, len(self.sent))
        teamrun.approve_member(rid, 2, run["members"][1]["id"])
        await teamrun.advance_run(rid, 2, self.dispatch)
        self.assertEqual(2, len(self.sent))
        self.assertEqual(102, self.sent[-1]["emp_idx"])
        self.assertEqual("running", teamrun.get_run(rid, 2)["status"])

    async def test_semi_cannot_preapprove_members_before_leader_plan(self):
        run = self.create(mode="semi", key="team-request-semi-plan-gate")
        with self.assertRaises(teamrun.TeamRunConflict):
            teamrun.approve_member(run["id"], 2, run["members"][1]["id"])
        teamrun.approve_member(run["id"], 2, run["members"][0]["id"])
        dispatched = await teamrun.advance_run(run["id"], 2, self.dispatch)
        with self.assertRaises(teamrun.TeamRunConflict):
            teamrun.approve_member(run["id"], 2, run["members"][1]["id"])
        db.update("task", dispatched["members"][0]["task_id"], {
            "status": "done", "output_md": "目标、分工与验收标准",
        })
        teamrun.refresh_run(run["id"], 2)
        approved = teamrun.approve_member(run["id"], 2, run["members"][1]["id"])
        self.assertTrue(approved["members"][1]["approved"])

    async def test_professional_depth_has_evidence_and_risk_standard(self):
        run = self.create(depth="professional", key="team-request-pro-depth")
        await teamrun.advance_run(run["id"], 2, self.dispatch)
        self.assertIn("可追溯证据", self.sent[0]["brief"]["direction"])
        self.assertIn("验收标准", self.sent[0]["brief"]["direction"])

    async def test_updated_member_delivery_can_trigger_fresh_summary(self):
        run = self.create(key="team-request-summary-stale")
        first = await teamrun.advance_run(run["id"], 2, self.dispatch)
        db.update("task", first["members"][0]["task_id"], {
            "status": "done", "output_md": "队长初版计划",
        })
        second = await teamrun.advance_run(run["id"], 2, self.dispatch)
        for member in second["members"][1:]:
            db.update("task", member["task_id"], {
                "status": "done", "output_md": "初版交付",
            })
        summary_run = await teamrun.advance_run(run["id"], 2, self.dispatch)
        first_summary_task = summary_run["summary_task_id"]
        db.update("task", first_summary_task, {
            "status": "done", "output_md": "第一次汇总",
        })
        await teamrun.advance_run(run["id"], 2, self.dispatch)
        self.assertFalse(teamrun.get_run(run["id"], 2)["summary_stale"])
        updated_member = second["members"][1]["task_id"]
        db.execute(
            "UPDATE task SET output_md=?,updated_at=? WHERE id=?",
            ("更新后的真实交付", 4102444800.0, updated_member),
        )
        self.assertTrue(teamrun.get_run(run["id"], 2)["summary_stale"])
        teamrun.retry_summary(run["id"], 2)
        refreshed = await teamrun.advance_run(run["id"], 2, self.dispatch)
        self.assertNotEqual(first_summary_task, refreshed["summary_task_id"])
        self.assertIn("更新后的真实交付", self.sent[-1]["brief"]["material"])

    async def test_semi_watcher_starts_approved_leader_before_remaining_approvals(self):
        run = self.create(mode="semi", key="team-request-semi-watch")
        teamrun.approve_member(run["id"], 2, run["members"][0]["id"])
        self.assertEqual("running", teamrun.get_run(run["id"], 2)["status"])

        async def complete_immediately(body):
            result = await self.dispatch(body)
            db.update("task", result["task_id"], {
                "status": "done", "output_md": "队长真实拆解" if body["emp_idx"] == 101 else "成员真实交付",
            })
            return result

        first = await teamrun.watch_run(
            run["id"], 2, complete_immediately,
            poll_seconds=0.01, max_seconds=2,
        )
        self.assertEqual([101], [body["emp_idx"] for body in self.sent])
        self.assertEqual("awaiting_approval", first["status"])
        teamrun.approve_member(run["id"], 2, run["members"][1]["id"])
        second = await teamrun.watch_run(
            run["id"], 2, complete_immediately,
            poll_seconds=0.01, max_seconds=2,
        )
        self.assertEqual([101, 102], [body["emp_idx"] for body in self.sent])
        self.assertEqual("awaiting_approval", second["status"])

    async def test_same_request_replays_and_other_tenant_cannot_read_or_act(self):
        first = self.create()
        replay = self.create()
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(teamrun.TeamRunConflict):
            self.create(depth="simple")
        with self.assertRaises(teamrun.TeamRunNotFound):
            teamrun.get_run(first["id"], 3)
        with self.assertRaises(teamrun.TeamRunNotFound):
            teamrun.approve_member(first["id"], 3, first["members"][0]["id"])
        await asyncio.gather(
            teamrun.advance_run(first["id"], 2, self.dispatch),
            teamrun.advance_run(first["id"], 2, self.dispatch),
        )
        self.assertEqual(1, len(self.sent))
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM team_run WHERE tenant_id=2")["n"])

    async def test_dispatch_failure_retry_uses_new_task_key(self):
        run = self.create()
        calls = []

        async def fail_once(body):
            calls.append(body["request_key"])
            if len(calls) == 1:
                raise RuntimeError("private provider secret")
            return await self.dispatch(body)

        failed = await teamrun.advance_run(run["id"], 2, fail_once)
        self.assertEqual("failed", failed["members"][0]["status"])
        self.assertNotIn("private provider secret", failed["members"][0]["last_error"])
        teamrun.retry_member(run["id"], 2, run["members"][0]["id"])
        recovered = await teamrun.advance_run(run["id"], 2, fail_once)
        self.assertNotEqual(calls[0], calls[1])
        self.assertEqual(1, len(self.sent))
        self.assertEqual("queued", recovered["members"][0]["status"])

    async def test_server_persists_run_after_database_reopen(self):
        run = self.create(mode="semi")
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.conn()
        loaded = teamrun.get_run(run["id"], 2)
        self.assertEqual("周年庆小队", loaded["team_name"])
        self.assertEqual(3, len(loaded["members"]))
        self.assertEqual([run["id"]], teamrun.pending_run_ids(2))

    async def test_committed_task_is_recovered_when_dispatch_reply_fails(self):
        run = self.create()

        async def commit_then_drop_reply(body):
            await self.dispatch(body)
            raise ConnectionError("response lost after commit")

        result = await teamrun.advance_run(run["id"], 2, commit_then_drop_reply)
        self.assertEqual(1, len(self.sent))
        self.assertEqual("queued", result["members"][0]["status"])
        self.assertIsNotNone(result["members"][0]["task_id"])
        await teamrun.advance_run(run["id"], 2, self.dispatch)
        self.assertEqual(1, len(self.sent))

    async def test_leader_cannot_be_skipped_and_invalid_dependency_is_rejected(self):
        run = self.create()
        with self.assertRaises(teamrun.TeamRunConflict):
            teamrun.skip_member(run["id"], 2, run["members"][0]["id"])
        invalid = {**TEAM, "members": [dict(m) for m in TEAM["members"]]}
        invalid["members"][1]["dependsOn"] = [103]
        invalid["members"][2]["dependsOn"] = [102]
        with self.assertRaises(teamrun.TeamRunError):
            teamrun.create_run(
                2, 11, "做一场门店周年庆", invalid,
                mode="auto", depth="comprehensive", request_key="team-request-cycle",
            )

    async def test_watch_run_advances_to_done_without_browser_polling(self):
        run = self.create()

        async def complete_immediately(body):
            result = await self.dispatch(body)
            db.update("task", result["task_id"], {
                "status": "done", "output_md": f"员工 {body['emp_idx']} 完成",
            })
            return result

        finished = await teamrun.watch_run(
            run["id"], 2, complete_immediately,
            poll_seconds=0.01, max_seconds=2,
        )
        self.assertEqual("done", finished["status"])
        self.assertEqual(4, len(self.sent))
        self.assertIn("员工 103 完成", self.sent[-1]["brief"]["material"])

    async def test_summary_failed_can_retry_with_new_key(self):
        run = self.create()
        fail_summary = True

        async def dispatch_with_summary_failure(body):
            nonlocal fail_summary
            if ":summary:" in body["request_key"] and fail_summary:
                fail_summary = False
                raise RuntimeError("provider unavailable")
            result = await self.dispatch(body)
            db.update("task", result["task_id"], {
                "status": "done", "output_md": "真实交付",
            })
            return result

        for _ in range(3):
            state = await teamrun.advance_run(run["id"], 2, dispatch_with_summary_failure)
        self.assertEqual("needs_attention", state["status"])
        self.assertEqual("failed", state["summary_status"])
        retried = teamrun.retry_summary(run["id"], 2)
        self.assertEqual("pending", retried["summary_status"])
        final = await teamrun.advance_run(run["id"], 2, dispatch_with_summary_failure)
        self.assertEqual("done", final["status"])
        self.assertIn(":summary:a2", self.sent[-1]["request_key"])


if __name__ == "__main__":
    unittest.main()
