"""广扫批次的钱与状态合同:进修结算诚实、深审降级不丢免费结果、定时恢复清残留。

对应老板视角走查的三类伤害:
- 进修花 3 点但"学到了什么"从不落库,新学 0 条也照收钱、后台失败零通知;
- 深审失败整个响应变报错,免费的规则词库结果陪葬;
- 定时任务因点数不足暂停后,重新打开开关仍挂着"已暂停"残留文案。
"""
import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import auth, billing, db, employees, main, providers
from app.skills import registry


class SweepFixCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "sweep.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "测试企业", "balance": 50})
        self.idx = next(iter(registry.BY_IDX))

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _as_boss(self):
        auth.set_current({
            "id": 1, "tenant_id": 2, "username": "boss",
            "role": "root", "modules": [],
        })

    def _as_owner(self):
        auth.set_current({
            "id": 20, "tenant_id": 2, "username": "owner",
            "role": "owner", "modules": ["content"],
        })

    def _balance(self):
        return db.one("SELECT balance FROM tenants WHERE id=2")["balance"]

    def _notes(self, kind):
        return db.q(
            "SELECT title,body FROM notification WHERE tenant_id=2 AND kind=?",
            (kind,),
        )

    async def _run_learn(self):
        result = await main.employee_learn(self.idx)
        self.assertTrue(result["started"])
        for _ in range(20):
            await asyncio.sleep(0.01)
            if not any(
                t for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and not t.done()
            ):
                break

    async def test_learn_zero_fresh_refunds_and_notifies(self):
        self._as_boss()
        with patch.object(
            employees, "learn",
            AsyncMock(return_value={"new": 0, "total": 5, "cost_usd": 0}),
        ):
            await self._run_learn()
        self.assertEqual(50, self._balance(), "新学 0 条必须退回 3 点")
        notes = self._notes("learn_done")
        self.assertEqual(1, len(notes))
        self.assertIn("退回", notes[0]["body"])

    async def test_learn_success_charges_and_notifies_result(self):
        self._as_boss()
        with patch.object(
            employees, "learn",
            AsyncMock(return_value={"new": 2, "total": 7, "cost_usd": 0.1}),
        ):
            await self._run_learn()
        self.assertEqual(47, self._balance(), "进修成功应收 3 点")
        notes = self._notes("learn_done")
        self.assertEqual(1, len(notes))
        self.assertIn("2", notes[0]["body"])

    async def test_learn_failure_refunds_and_notifies(self):
        self._as_boss()
        with patch.object(
            employees, "learn",
            AsyncMock(side_effect=RuntimeError("上游超时")),
        ):
            await self._run_learn()
        self.assertEqual(50, self._balance())
        self.assertEqual(1, len(self._notes("learn_failed")))

    async def test_concurrent_learn_requests_have_one_charge_and_one_claim(self):
        """并发点击只能创建一笔进修操作，第二次必须立即收到 429。"""
        self._as_boss()
        entered = threading.Event()
        release = threading.Event()
        original = billing.start_operation

        def slow_start(*args, **kwargs):
            entered.set()
            release.wait(timeout=2)
            return original(*args, **kwargs)

        with patch.object(
            billing,
            "start_operation",
            side_effect=slow_start,
        ), patch.object(
            employees,
            "learn",
            AsyncMock(return_value={"new": 1, "total": 1, "cost_usd": 0}),
        ):
            first = asyncio.create_task(main.employee_learn(self.idx))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            with self.assertRaises(HTTPException) as raised:
                await main.employee_learn(self.idx)
            self.assertEqual(429, raised.exception.status_code)
            release.set()
            self.assertTrue((await first)["started"])
            for _ in range(50):
                if self.idx not in employees.LEARNING:
                    break
                await asyncio.sleep(0.01)

        self.assertNotIn(self.idx, employees.LEARNING)
        self.assertEqual(47, self._balance())
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_log "
                "WHERE tenant_id=2 AND delta=-3"
            )["n"],
        )

    async def test_learn_claim_covers_settlement_without_aba_release(self):
        """进修返回后到结算完成前仍须占位，旧任务不能误删新任务的 claim。"""
        self._as_boss()
        entered = threading.Event()
        release = threading.Event()
        original = billing.complete_operation

        def slow_complete(op_key):
            entered.set()
            release.wait(timeout=2)
            return original(op_key)

        with patch.object(
            employees,
            "learn",
            AsyncMock(return_value={"new": 1, "total": 1, "cost_usd": 0}),
        ), patch.object(
            billing,
            "complete_operation",
            side_effect=slow_complete,
        ):
            self.assertTrue((await main.employee_learn(self.idx))["started"])
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertIn(self.idx, employees.LEARNING)
            with self.assertRaises(HTTPException) as raised:
                await main.employee_learn(self.idx)
            self.assertEqual(429, raised.exception.status_code)
            release.set()
            for _ in range(50):
                if self.idx not in employees.LEARNING:
                    break
                await asyncio.sleep(0.01)

        self.assertNotIn(self.idx, employees.LEARNING)
        self.assertEqual(47, self._balance())

    async def test_cancelled_learn_start_refunds_late_charge_and_claim(self):
        """开单 worker 已进入后取消请求，晚到的扣点必须收口退款。"""
        self._as_boss()
        entered = threading.Event()
        release = threading.Event()
        original = billing.start_operation

        def slow_start(*args, **kwargs):
            entered.set()
            release.wait(timeout=2)
            return original(*args, **kwargs)

        with patch.object(
            billing,
            "start_operation",
            side_effect=slow_start,
        ):
            request = asyncio.create_task(main.employee_learn(self.idx))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            request.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await request

        self.assertNotIn(self.idx, employees.LEARNING)
        self.assertEqual(50, self._balance())
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_operation "
                "WHERE tenant_id=2 AND action='learn' AND status='charged'"
            )["n"],
        )

    async def test_censor_deep_failure_returns_free_scan_and_refunds(self):
        self._as_owner()
        with patch.object(
            providers, "call_text_json",
            AsyncMock(side_effect=RuntimeError("模型不可用")),
        ):
            report = await main.censor_check_api({
                "title": "招牌",
                "body": "我们家的麻辣烫是全城最好的选择",
                "platform": "公众号",
            })
        self.assertTrue(report["degraded"])
        self.assertIn("退回", report["summary"])
        self.assertTrue(
            any("最好" in (i.get("detail") or "") for i in report["issues"]),
            "免费规则词库的命中结果必须保留在降级响应里",
        )
        self.assertEqual(50, self._balance(), "深审失败必须退回 1 点")

    async def test_schedule_reenable_clears_paused_note(self):
        self._as_owner()
        sid = db.insert("schedule", {
            "tenant_id": 2, "name": "每日选题",
            "brief_json": '{"direction":"本行业新动态"}',
            "kind": "daily", "at_time": "09:00",
            "enabled": 0, "last_note": "已暂停:点数不足(需 18 点)",
        })
        main.schedule_update(sid, {"enabled": True})
        row = db.one("SELECT enabled,last_note,next_run_at FROM schedule "
                     "WHERE id=?", (sid,))
        self.assertEqual(1, row["enabled"])
        self.assertEqual("已重新启用,下次到点自动开工", row["last_note"])
        self.assertTrue(row["next_run_at"])

    async def test_company_get_reports_filled_field_count(self):
        self._as_owner()
        db.set_setting(
            "company_profile:2",
            '{"brand":"川小福","tone":"亲切"}',
        )
        data = main.company_get()
        self.assertTrue(data["injected"])
        self.assertEqual(2, data["filled"])
        self.assertEqual(7, data["total_fields"])


if __name__ == "__main__":
    unittest.main()
