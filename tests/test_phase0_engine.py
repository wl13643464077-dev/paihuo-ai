"""Phase0：内容流水线稳定性 + 计费漏洞的行为测试(走临时 SQLite,真实调用引擎)。"""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import auth, billing, db, gate, notify, providers
from app.engine import (
    MAX_USER_RERUNS,
    RERUN_LIMIT_MSG,
    WORKER_COUNT,
    Engine,
)
from app.skills import registry


class _EngineDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "phase0-engine.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "付费租户", "balance": 20})
        self.owner = {
            "id": 2, "tenant_id": 2, "username": "owner",
            "role": "owner", "modules": ["content"], "enabled": 1,
        }
        auth.set_current(self.owner)
        self.engine = Engine()

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _charged_job(self, mode="copilot", direction="门店周末活动"):
        jid = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": direction}, ensure_ascii=False),
            "mode": mode,
            "status": "pending_charge",
            "billing_status": "pending",
            "billing_points": 18,
        })

        def claim(c):
            cur = c.execute(
                "UPDATE job SET billing_status='charged', status='running' "
                "WHERE id=? AND billing_status='pending'",
                (jid,),
            )
            return cur.rowcount == 1

        self.assertTrue(billing.charge_if_claimed(
            "content_job", 2, claim, note="测试", points=18))
        return jid

    def _run(self, jid, idx, status, output=None, version=1, reviewed_by=None):
        return db.insert("station_run", {
            "job_id": jid,
            "station_idx": idx,
            "version": version,
            "status": status,
            "reviewed_by": reviewed_by,
            "output_json": (json.dumps(output, ensure_ascii=False)
                            if output is not None else None),
        })

    def _job(self, jid):
        return db.one(
            "SELECT status,billing_status FROM job WHERE id=?", (jid,))

    def _failure_notices(self, jid):
        return db.q(
            "SELECT * FROM notification WHERE job_id=? AND kind='job_failed'",
            (jid,),
        )


# ---------------------------------------------------------------- 1. worker 自愈
class WorkerSelfHealingCase(_EngineDbCase):
    def test_worker_keeps_running_when_settlement_and_cleanup_raise(self):
        processed = []

        async def broken_advance(job_id):
            processed.append(job_id)
            raise RuntimeError("provider exploded")

        def broken_settle(*_args, **_kwargs):
            raise RuntimeError("database is locked")

        async def broken_aone(*_args, **_kwargs):
            raise RuntimeError("database is locked")

        async def scenario():
            self.engine._loop = asyncio.get_running_loop()
            worker = self.engine._spawn_worker()
            with patch.object(self.engine, "_advance", broken_advance), \
                    patch.object(self.engine, "settle_failure", broken_settle), \
                    patch.object(db, "aone", broken_aone):
                for job_id in (101, 102, 103):
                    self.engine.queue.put_nowait(job_id)
                for _ in range(200):
                    if len(processed) == 3 and self.engine.queue.empty():
                        break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
            alive = not worker.done()
            worker.cancel()
            return alive

        self.assertTrue(asyncio.run(scenario()))
        self.assertEqual([101, 102, 103], processed)

    def test_crashing_advance_settles_job_as_failed_and_refunds(self):
        jid = self._charged_job()
        self._run(jid, 1, "running")

        async def broken_advance(_job_id):
            raise RuntimeError("boom")

        async def scenario():
            self.engine._loop = asyncio.get_running_loop()
            worker = self.engine._spawn_worker()
            with patch.object(self.engine, "_advance", broken_advance):
                self.engine.queue.put_nowait(jid)
                for _ in range(300):
                    if self._job(jid)["status"] == "failed":
                        break
                    await asyncio.sleep(0.01)
            worker.cancel()

        asyncio.run(scenario())
        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"}, self._job(jid))
        self.assertEqual(20, billing.balance(2))

    def test_supervisor_respawns_unexpected_exit_but_not_cancellation(self):
        calls = []

        async def flaky_worker():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("worker died")
            await asyncio.Event().wait()

        async def scenario():
            self.engine._loop = asyncio.get_running_loop()
            with patch.object(self.engine, "_worker", flaky_worker):
                self.engine._ensure_workers()
                for _ in range(20):
                    await asyncio.sleep(0)
                after_crash = len(self.engine._workers)
                victim = next(iter(self.engine._workers))
                victim.cancel()
                for _ in range(20):
                    await asyncio.sleep(0)
                after_cancel = len(self.engine._workers)
                # start() 再次补满到 WORKER_COUNT,不会多开
                self.engine._ensure_workers()
                refilled = len(self.engine._workers)
                for task in list(self.engine._workers):
                    task.cancel()
                await asyncio.sleep(0)
            return after_crash, after_cancel, refilled

        after_crash, after_cancel, refilled = asyncio.run(scenario())
        self.assertEqual(WORKER_COUNT, after_crash)
        self.assertEqual(WORKER_COUNT + 1, len(calls))   # 4 个原始 + 1 个补建
        self.assertEqual(WORKER_COUNT - 1, after_cancel)  # 正常取消不补建
        self.assertEqual(WORKER_COUNT, refilled)


# ---------------------------------------------------------------- 2. 通知 best-effort
class NotificationBestEffortCase(_EngineDbCase):
    def test_awaiting_notice_error_does_not_fail_pending_review_job(self):
        jid = self._charged_job(mode="copilot")

        async def fake_trend(_ctx):
            return {"data": {"topics": [{"title": "A"}, {"title": "B"}]},
                    "tokens": 1, "cost_usd": 0.0}

        def broken_webhook(_tid):
            raise RuntimeError("settings table locked")

        with patch.dict(registry.BY_IDX[0], {"run": fake_trend}), \
                patch.object(providers, "text_model_for", lambda _idx: "m"), \
                patch.object(notify, "get_webhook", broken_webhook):
            finished = asyncio.run(self.engine._advance_once(jid))

        self.assertTrue(finished)
        self.assertEqual(
            {"status": "awaiting_review", "billing_status": "charged"},
            self._job(jid),
        )
        run = db.one(
            "SELECT status FROM station_run WHERE job_id=? AND station_idx=0",
            (jid,))
        self.assertEqual("awaiting_review", run["status"])
        # 站内通知在 webhook 读取前已落库
        self.assertEqual(1, len(db.q(
            "SELECT id FROM notification WHERE job_id=? AND kind='awaiting'",
            (jid,))))

    def test_notice_meta_lookup_error_is_swallowed(self):
        def broken_meta(_job_id):
            raise RuntimeError("db gone")

        with patch.object(self.engine, "_job_notification_meta", broken_meta):
            ok = asyncio.run(self.engine._push_job_notice(1, "gate"))
        self.assertFalse(ok)

    def test_refunded_failure_writes_one_inbox_notice_visible_to_owner(self):
        jid = self._charged_job(direction="新品试吃推广")
        self._run(jid, 1, "running")

        self.assertTrue(self.engine.settle_failure(jid, "供应商失败"))
        self.assertFalse(self.engine.settle_failure(jid, "看门狗重复处理"))

        notices = self._failure_notices(jid)
        self.assertEqual(1, len(notices))
        self.assertIn("18 点已全部退回", notices[0]["body"])
        self.assertIn("新品试吃推广", notices[0]["body"])
        self.assertEqual(f"#/job/{jid}", notices[0]["link"])
        visible = notify.unread_for_user(2, self.owner)
        self.assertIn(notices[0]["id"], [row["id"] for row in visible])

    def test_failure_with_usable_draft_notice_says_points_kept(self):
        jid = self._charged_job()
        self._run(jid, 3, "done", {"body": "可用正文"})

        self.assertFalse(self.engine.settle_failure(jid, "发布辅助失败"))
        self.assertFalse(self.engine.settle_failure(jid, "重复处理"))

        notices = self._failure_notices(jid)
        self.assertEqual(1, len(notices))
        self.assertIn("点数不退", notices[0]["body"])

    def test_refund_error_notice_says_refund_pending(self):
        jid = self._charged_job()
        self._run(jid, 1, "running")

        def broken_refund(*_a, **_k):
            raise RuntimeError("billing down")

        with patch.object(billing, "refund_amount_if_claimed", broken_refund):
            self.assertFalse(self.engine.settle_failure(jid, "供应商失败"))
        self.assertEqual(
            {"status": "failed", "billing_status": "charged"}, self._job(jid))
        notices = self._failure_notices(jid)
        self.assertEqual(1, len(notices))
        self.assertIn("自动补退", notices[0]["body"])

    def test_notice_write_error_never_breaks_settlement(self):
        jid = self._charged_job()
        self._run(jid, 1, "running")

        def broken_record(*_a, **_k):
            raise RuntimeError("notification table missing")

        with patch.object(notify, "record", broken_record):
            self.assertTrue(self.engine.settle_failure(jid, "供应商失败"))
        self.assertEqual(
            {"status": "failed", "billing_status": "refunded"}, self._job(jid))


# ---------------------------------------------------------------- 3. 取消白拿
class CancelRefundPurgeCase(_EngineDbCase):
    def test_full_refund_cancel_purges_research_outputs_and_topic_assets(self):
        jid = self._charged_job()
        self._run(jid, 0, "done", {"topics": [{"title": "A"}, {"title": "B"}],
                                   "selected": 0})
        self._run(jid, 1, "done", {"facts": ["联网研究结论"], "sources": []})
        self._run(jid, 2, "awaiting_review", {"notes": "爆款拆解"})
        self._run(jid, 3, "running")
        db.insert("asset", {
            "type": "topic", "job_id": jid, "tenant_id": 2,
            "payload_json": json.dumps({"title": "B"}, ensure_ascii=False),
        })

        self.assertTrue(self.engine.settle_cancel(jid, "老板取消"))

        self.assertEqual(
            {"status": "cancelled", "billing_status": "refunded"}, self._job(jid))
        self.assertEqual(20, billing.balance(2))
        rows = db.q(
            "SELECT station_idx,status,output_json FROM station_run "
            "WHERE job_id=? ORDER BY station_idx", (jid,))
        self.assertEqual([None] * 4, [r["output_json"] for r in rows])
        self.assertEqual({"cancelled"}, {r["status"] for r in rows})
        # 前端工单详情/交付包读的就是这些行与 collect_outputs
        self.assertEqual({}, self.engine.collect_outputs(jid))
        self.assertEqual(0, len(db.q(
            "SELECT id FROM asset WHERE job_id=? AND type='topic'", (jid,))))

    def test_cancel_with_usable_draft_keeps_outputs_and_points(self):
        jid = self._charged_job()
        self._run(jid, 1, "done", {"facts": ["研究"]})
        self._run(jid, 3, "awaiting_review", {"body": "正文"})

        self.assertFalse(self.engine.settle_cancel(jid, "老板取消"))

        self.assertEqual(
            {"status": "cancelled", "billing_status": "charged"}, self._job(jid))
        self.assertEqual(2, billing.balance(2))
        outputs = self.engine.collect_outputs(jid)
        self.assertEqual({"facts": ["研究"]}, outputs[1])
        self.assertEqual({"body": "正文"}, outputs[3])

    def test_legacy_cancelled_charged_job_refund_also_purges(self):
        jid = self._charged_job()
        self._run(jid, 1, "done", {"facts": ["研究"]})
        db.q("UPDATE job SET status='cancelled' WHERE id=?", (jid,))

        self.assertTrue(self.engine.settle_cancel(jid, "补退"))
        self.assertEqual({}, self.engine.collect_outputs(jid))
        self.assertIsNone(db.one(
            "SELECT output_json FROM station_run WHERE job_id=?",
            (jid,))["output_json"])


# ---------------------------------------------------------------- 4. 重跑上限
class RerunLimitCase(_EngineDbCase):
    def _next_version_awaiting(self, jid, idx):
        latest = db.one(
            "SELECT MAX(version) AS v FROM station_run "
            "WHERE job_id=? AND station_idx=?", (jid, idx))["v"]
        self._run(jid, idx, "awaiting_review", {"body": "新版本"},
                  version=int(latest) + 1)

    def test_reject_and_rerun_share_three_per_station_quota(self):
        jid = self._charged_job()
        self._run(jid, 3, "awaiting_review", {"body": "v1"})

        self.engine.user_action(jid, 3, "reject", {"comment": "再口语一点"})
        self._next_version_awaiting(jid, 3)
        self.engine.user_action(jid, 3, "rerun", {"comment": "换个开头"})
        self._next_version_awaiting(jid, 3)
        self.engine.user_action(jid, 3, "reject", {"comment": "短一点"})
        self._next_version_awaiting(jid, 3)

        for action in ("reject", "rerun"):
            with self.assertRaises(ValueError) as ctx:
                self.engine.user_action(jid, 3, action, {"comment": "还不行"})
            self.assertEqual(RERUN_LIMIT_MSG, str(ctx.exception))
        # 超限不改动最新版本,老板仍可直接通过
        latest = db.one(
            "SELECT status FROM station_run WHERE job_id=? AND station_idx=3 "
            "ORDER BY version DESC LIMIT 1", (jid,))
        self.assertEqual("awaiting_review", latest["status"])
        self.engine.user_action(jid, 3, "approve", {})
        self.assertEqual(MAX_USER_RERUNS, 3)

    def test_system_rejections_and_other_stations_do_not_count(self):
        jid = self._charged_job()
        # 服务重启/打断恢复留下的 rejected 版本没有拍板人
        for version in (1, 2, 3):
            self._run(jid, 3, "rejected", {"body": "旧"}, version=version)
        self._run(jid, 3, "awaiting_review", {"body": "v4"}, version=4)
        self._run(jid, 0, "awaiting_review",
                  {"topics": [{"title": "A"}]})
        for _ in range(3):
            self.engine.user_action(jid, 3, "reject", {"comment": "改"})
            self._next_version_awaiting(jid, 3)
        with self.assertRaises(ValueError):
            self.engine.user_action(jid, 3, "reject", {"comment": "再改"})
        # 工位 3 用满不影响工位 0
        self.engine.user_action(jid, 0, "reject", {"comment": "换选题"})

    def test_rerun_without_session_still_counts(self):
        auth.set_current(None)
        jid = self._charged_job()
        self._run(jid, 3, "awaiting_review", {"body": "v1"})
        for _ in range(3):
            self.engine.user_action(jid, 3, "rerun", {})
            self._next_version_awaiting(jid, 3)
        with self.assertRaises(ValueError):
            self.engine.user_action(jid, 3, "rerun", {})


# ---------------------------------------------------------------- 5. 审批 payload 校验
class ApprovalPayloadValidationCase(_EngineDbCase):
    def test_bad_selection_types_are_rejected_without_touching_job(self):
        jid = self._charged_job()
        self._run(jid, 0, "awaiting_review",
                  {"topics": [{"title": "A"}, {"title": "B"}]})
        self._run(jid, 3, "awaiting_review",
                  {"body": "正文", "title_candidates": ["T1", "T2"]})
        bad = [
            (0, {"selected": "1"}),
            (0, {"selected": True}),
            (0, {"selected": 5}),
            (0, {"selected": -1}),
            (0, {"edits": {"selected": "0"}}),
            (3, {"selected_title": "abc"}),
            (3, {"selected_title": 1.5}),
            (3, {"selected_title": None}),
            (3, {"edits": ["body"]}),
            (3, {"edits": {"title_candidates": "T1"}}),
            (3, {"comment": 123}),
            (3, ["approve"]),
        ]
        for idx, payload in bad:
            with self.subTest(idx=idx, payload=payload):
                with self.assertRaises(ValueError):
                    self.engine.user_action(jid, idx, "approve", payload)
                self.assertEqual("running", self._job(jid)["status"])
        self.assertEqual(
            {"awaiting_review"},
            {r["status"] for r in db.q(
                "SELECT status FROM station_run WHERE job_id=?", (jid,))},
        )
        with self.assertRaises(ValueError):
            self.engine.user_action(jid, 3, "reject", {"comment": ["x"]})

        self.engine.user_action(jid, 3, "approve", {"selected_title": 1})
        self.assertEqual("T2", self.engine._job_title(jid))
        self.engine.user_action(jid, 0, "approve", {"selected": 1})

    def test_job_title_tolerates_legacy_bad_selection(self):
        jid = self._charged_job()
        self._run(jid, 3, "done",
                  {"title_candidates": ["T1", "T2"], "selected_title": "1"})
        self.assertEqual("T1", self.engine._job_title(jid))
        self._run(jid, 4, "done",
                  {"title_candidates": "not-a-list", "selected_title": 0})
        self.assertEqual("T1", self.engine._job_title(jid))


# ---------------------------------------------------------------- 8. 北京时间
class _FakeDatetime(datetime):
    """固定在 UTC 2026-09-25 17:30,也就是北京时间 9 月 26 日凌晨。"""

    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 9, 25, 17, 30, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


class BeijingTodayCase(_EngineDbCase):
    def test_station_context_today_uses_beijing_date(self):
        jid = self._charged_job()
        seen = {}

        async def capture(ctx):
            seen["today"] = ctx["today"]
            return {"data": {"topics": []}, "tokens": 0, "cost_usd": 0.0}

        with patch.object(gate, "datetime", _FakeDatetime), \
                patch.object(providers, "text_model_for", lambda _idx: "m"):
            ok = asyncio.run(self.engine._execute(
                jid, 0,
                {"skill": "trend", "name": "趋势官", "run": capture},
                {"direction": "测试"}, None, 1, None, None,
            ))
        self.assertTrue(ok)
        self.assertEqual("2026-09-26", seen["today"])


if __name__ == "__main__":
    unittest.main()
