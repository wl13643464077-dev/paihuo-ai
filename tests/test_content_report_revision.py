"""已交付复盘报告的续改：旧版、原扣费、失败恢复和并发闸。"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app import auth, db
from app.engine import Engine, LAST_IDX, REPORT_REVISION_SKILL
from app.skills import registry


class CompletedReportRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "report-revision.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "商家", "balance": 20})
        auth.set_current({"id": 2, "tenant_id": 2, "role": "owner"})
        self.engine = Engine()
        self.job_id = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "门店活动复盘"}, ensure_ascii=False),
            "status": "done",
            "current_idx": LAST_IDX,
            "mode": "fullauto",
            "billing_status": "charged",
            "billing_points": 18,
        })
        for idx in range(LAST_IDX + 1):
            db.insert("station_run", {
                "job_id": self.job_id,
                "station_idx": idx,
                "skill_id": registry.BY_IDX[idx]["skill"],
                "version": 1,
                "status": "done",
                "output_json": json.dumps(
                    {"report": "第一版复盘报告", "next_topics": []}
                    if idx == LAST_IDX else {},
                    ensure_ascii=False,
                ),
            })
        self.knowledge_id = db.insert("knowledge", {
            "tenant_id": 2,
            "job_id": self.job_id,
            "title": "《门店活动》交付复盘",
            "content": "第一版复盘报告",
            "tags_json": '["自动沉淀"]',
            "source": "auto",
        })
        self.asset_id = db.insert("asset", {
            "tenant_id": 2,
            "job_id": self.job_id,
            "type": "final",
            "payload_json": '{"title":"门店活动"}',
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _runs(self):
        return db.q(
            "SELECT version,status,skill_id,output_json,review_comment "
            "FROM station_run WHERE job_id=? AND station_idx=? ORDER BY version",
            (self.job_id, LAST_IDX),
        )

    def test_success_appends_v2_and_keeps_v1_delivery_without_second_charge(self):
        seen = []

        async def revised(ctx):
            seen.append((ctx["version"], ctx["revision_note"], ctx["prev_output"]))
            return {
                "data": {"report": "第二版复盘报告", "next_topics": []},
                "tokens": 120,
                "cost_usd": 0.03,
            }

        cfg = registry.BY_IDX[LAST_IDX]
        with patch.dict(cfg, {"run": revised}), patch("app.notify.push"):
            self.assertEqual(2, self.engine.redo_completed_report(
                self.job_id, "把下周行动拆成三步"))
            self.assertEqual("done", self._runs()[0]["status"])
            self.assertEqual("queued", self._runs()[1]["status"])
            self.assertEqual("第一版复盘报告",
                             self.engine.collect_outputs(self.job_id)[9]["report"])
            asyncio.run(self.engine._advance(self.job_id))

        runs = self._runs()
        self.assertEqual([(1, "done"), (2, "done")],
                         [(r["version"], r["status"]) for r in runs])
        self.assertEqual("第一版复盘报告", db.jloads(runs[0]["output_json"])["report"])
        self.assertEqual("第二版复盘报告", db.jloads(runs[1]["output_json"])["report"])
        self.assertEqual([(2, "把下周行动拆成三步",
                           {"report": "第一版复盘报告", "next_topics": []})], seen)
        self.assertEqual("第二版复盘报告",
                         self.engine.collect_outputs(self.job_id)[9]["report"])
        self.assertEqual("done", db.one(
            "SELECT status FROM job WHERE id=?", (self.job_id,))["status"])
        self.assertEqual(20, db.one(
            "SELECT balance FROM tenants WHERE id=2")["balance"])
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_log WHERE job_id=?",
            (self.job_id,))["n"])
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM asset WHERE job_id=? AND type='final' "
            "AND deleted_at IS NULL",
            (self.job_id,))["n"])
        knowledge = db.one(
            "SELECT id,content FROM knowledge WHERE job_id=? AND source='auto'",
            (self.job_id,),
        )
        self.assertEqual(self.knowledge_id, knowledge["id"])
        self.assertIn("第二版复盘报告", knowledge["content"])
        self.assertNotIn("第一版复盘报告", knowledge["content"])

    def test_failure_restores_completed_job_and_prior_report(self):
        attempts = []

        async def fails(ctx):
            attempts.append(ctx["version"])
            raise ValueError("模拟供应商故障")

        cfg = registry.BY_IDX[LAST_IDX]
        with patch.dict(cfg, {"run": fails}), patch("app.notify.push"):
            self.engine.redo_completed_report(self.job_id, "增加数据来源说明")
            asyncio.run(self.engine._advance(self.job_id))

        self.assertEqual([2, 2, 2], attempts)
        self.assertEqual([(1, "done"), (2, "failed")],
                         [(r["version"], r["status"]) for r in self._runs()])
        self.assertIsNone(self._runs()[1]["output_json"])
        self.assertEqual("done", db.one(
            "SELECT status FROM job WHERE id=?", (self.job_id,))["status"])
        self.assertEqual("第一版复盘报告",
                         self.engine.collect_outputs(self.job_id)[9]["report"])
        self.assertEqual(20, db.one(
            "SELECT balance FROM tenants WHERE id=2")["balance"])
        self.assertEqual("第一版复盘报告", db.one(
            "SELECT content FROM knowledge WHERE id=?",
            (self.knowledge_id,))["content"])

    def test_empty_new_report_is_not_delivered_over_prior_version(self):
        async def empty_report(_ctx):
            return {"data": {"report": "  "}, "tokens": 1, "cost_usd": 0.001}

        with patch.dict(registry.BY_IDX[LAST_IDX], {"run": empty_report}), \
                patch("app.notify.push"):
            self.engine.redo_completed_report(self.job_id, "补充会议现场结论")
            asyncio.run(self.engine._advance(self.job_id))
        self.assertEqual("done", db.one(
            "SELECT status FROM job WHERE id=?", (self.job_id,))["status"])
        self.assertEqual("failed", self._runs()[1]["status"])
        self.assertEqual("第一版复盘报告",
                         self.engine.collect_outputs(self.job_id)[9]["report"])

    def test_second_request_during_running_is_rejected_then_next_version_is_v3(self):
        self.assertEqual(2, self.engine.redo_completed_report(self.job_id, "第一条意见"))
        with self.assertRaisesRegex(ValueError, "仅已完成"):
            self.engine.redo_completed_report(self.job_id, "并发第二条意见")
        self.assertEqual(2, len(self._runs()))
        self.engine.settle_cancel(self.job_id, "取消这次改版")
        self.assertEqual("done", db.one(
            "SELECT status FROM job WHERE id=?", (self.job_id,))["status"])
        self.assertEqual("cancelled", self._runs()[1]["status"])
        self.assertEqual(3, self.engine.redo_completed_report(self.job_id, "重新改一版"))
        self.assertEqual([1, 2, 3], [r["version"] for r in self._runs()])

    def test_inflight_provider_cannot_overwrite_old_report_after_revision_cancel(self):
        self.engine.redo_completed_report(self.job_id, "调整结论")

        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()

            async def delayed(_ctx):
                entered.set()
                await release.wait()
                return {
                    "data": {"report": "取消后不应交付", "next_topics": []},
                    "tokens": 50,
                    "cost_usd": 0.01,
                }

            with patch.dict(registry.BY_IDX[LAST_IDX], {"run": delayed}):
                task = asyncio.create_task(self.engine._advance(self.job_id))
                await asyncio.wait_for(entered.wait(), 3)
                self.engine.settle_cancel(self.job_id, "只取消本次改版")
                release.set()
                await asyncio.wait_for(task, 3)

        asyncio.run(scenario())
        self.assertEqual("done", db.one(
            "SELECT status FROM job WHERE id=?", (self.job_id,))["status"])
        self.assertEqual("cancelled", self._runs()[1]["status"])
        self.assertIsNone(self._runs()[1]["output_json"])
        self.assertEqual("第一版复盘报告",
                         self.engine.collect_outputs(self.job_id)[9]["report"])

    def test_pause_resume_keeps_original_revision_instruction(self):
        self.engine.redo_completed_report(self.job_id, "保留原结构，增加线下会销段落")
        pending = self._runs()[1]
        self.assertEqual(REPORT_REVISION_SKILL, pending["skill_id"])
        db.q("UPDATE station_run SET status='running' WHERE job_id=? AND station_idx=9 "
             "AND version=2", (self.job_id,))
        with patch("app.engine.llm.kill", return_value=1):
            self.engine.pause(self.job_id)
        self.assertEqual("保留原结构，增加线下会销段落",
                         self._runs()[1]["review_comment"])
        self.engine.resume(self.job_id)
        self.assertEqual("rejected", self._runs()[1]["status"])
        self.assertEqual("保留原结构，增加线下会销段落",
                         self._runs()[1]["review_comment"])

    def test_restart_reuses_pending_version_and_original_instruction(self):
        self.engine.redo_completed_report(self.job_id, "重新核对活动结果")
        db.q("UPDATE station_run SET status='running' WHERE job_id=? AND station_idx=9 "
             "AND version=2", (self.job_id,))
        seen = []

        async def revised(ctx):
            seen.append((ctx["version"], ctx["revision_note"]))
            return {
                "data": {"report": "重启后第二版", "next_topics": []},
                "tokens": 8,
                "cost_usd": 0.002,
            }

        async def scenario():
            await self.engine.start()
            for _ in range(100):
                if db.one("SELECT status FROM job WHERE id=?",
                          (self.job_id,))["status"] == "done":
                    return
                await asyncio.sleep(0.03)
            self.fail("重启后报告改版未完成")

        with patch.dict(registry.BY_IDX[LAST_IDX], {"run": revised}), \
                patch("app.notify.push"):
            asyncio.run(scenario())
        self.assertEqual([(2, "重新核对活动结果")], seen)
        self.assertEqual([(1, "done"), (2, "done")],
                         [(r["version"], r["status"]) for r in self._runs()])

    def test_rejects_missing_report_and_invalid_comment(self):
        with self.assertRaisesRegex(ValueError, "修改的地方"):
            self.engine.redo_completed_report(self.job_id, " ")
        db.q("UPDATE station_run SET output_json='{}' WHERE job_id=? AND station_idx=9",
             (self.job_id,))
        with self.assertRaisesRegex(ValueError, "原复盘报告"):
            self.engine.redo_completed_report(self.job_id, "补充结论")
        self.assertEqual("done", db.one(
            "SELECT status FROM job WHERE id=?", (self.job_id,))["status"])


if __name__ == "__main__":
    unittest.main()
