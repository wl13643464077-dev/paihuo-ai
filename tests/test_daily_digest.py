"""老板「昨日经营简报」每日推送:生成口径、防重、开关与端点合同."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from fastapi import HTTPException

from app import auth, db, scheduler


def _fixed_now() -> datetime:
    """北京时间今天 08:30(落在 8-10 点推送窗口内)."""
    return datetime.now(scheduler.TZ).replace(
        hour=8, minute=30, second=0, microsecond=0
    )


class DailyDigestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "digest.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "活跃企业", "balance": 100})
        db.insert("tenants", {"id": 3, "name": "闲置企业", "balance": 50})
        db.insert("tenants", {"id": 4, "name": "停用企业", "enabled": 0,
                              "balance": 30})
        self.now = _fixed_now()
        day_start = self.now.replace(hour=0, minute=0)
        self.y_ts = (day_start - timedelta(hours=2)).timestamp()   # 昨日 22:00
        self.old_ts = (day_start - timedelta(days=2)).timestamp()  # 前天,不算

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _seed_activity(self, tid: int = 2):
        # 昨日完成 1 单工单、1 件专家任务;前天/今天的完成数不得混入
        db.insert("job", {"brief_json": "{}", "tenant_id": tid,
                          "status": "done", "updated_at": self.y_ts})
        db.insert("job", {"brief_json": "{}", "tenant_id": tid,
                          "status": "done", "updated_at": self.old_ts})
        db.insert("job", {"brief_json": "{}", "tenant_id": tid,
                          "status": "done",
                          "updated_at": self.now.timestamp()})
        db.insert("task", {"emp_idx": 101, "brief_json": "{}",
                           "tenant_id": tid, "status": "done",
                           "updated_at": self.y_ts})
        # 昨日消耗 18 点、失败退回 1 笔;充值不算退点
        db.insert("billing_log", {"tenant_id": tid, "delta": -18,
                                  "balance": 82, "reason": "内容流水线整单",
                                  "created_at": self.y_ts})
        db.insert("billing_log", {"tenant_id": tid, "delta": 3, "balance": 85,
                                  "reason": "退回:员工全网进修 · 模型不可用",
                                  "created_at": self.y_ts})
        db.insert("billing_log", {"tenant_id": tid, "delta": 500,
                                  "balance": 585, "reason": "开通创业版",
                                  "created_at": self.y_ts})
        # 点数不足被自动暂停的定时任务 → 风险项
        db.insert("schedule", {"name": "日更", "brief_json": "{}",
                               "tenant_id": tid, "enabled": 0,
                               "last_note": "已暂停:点数不足"})

    def _digests(self, tid: int):
        return db.q(
            "SELECT * FROM notification WHERE tenant_id=? AND "
            "kind='daily_digest' ORDER BY id",
            (tid,),
        )

    def test_active_tenant_gets_one_digest_with_counts_and_spend(self):
        self._seed_activity(2)
        scheduler._run_daily_digest(self.now)

        rows = self._digests(2)
        self.assertEqual(1, len(rows))
        self.assertIn("昨日经营简报", rows[0]["title"])
        self.assertEqual("#/billing", rows[0]["link"])
        body = rows[0]["body"]
        self.assertIn("内容工单 1 单", body)          # 只算昨日,不混前天/今天
        self.assertIn("专家任务 1 件", body)
        self.assertIn("消耗 18 点", body)
        self.assertIn("退回 1 笔", body)
        self.assertIn("1 个定时任务已暂停", body)
        self.assertIn("余额 100 点", body)
        self.assertIn("约可再跑", body)
        # 幂等标记已按北京时区当日落库
        self.assertEqual(
            self.now.strftime("%Y-%m-%d"),
            db.get_setting("daily_digest_sent:2"),
        )

    def test_paused_schedule_alone_still_warns_and_hides_runway_line(self):
        # 只有暂停风险、没有任何流水消耗:要提醒断更,但日均为 0 不估算天数
        db.insert("schedule", {"name": "日更", "brief_json": "{}",
                               "tenant_id": 2, "enabled": 0,
                               "last_note": "已暂停:点数不足"})
        scheduler._run_daily_digest(self.now)

        rows = self._digests(2)
        self.assertEqual(1, len(rows))
        self.assertIn("1 个定时任务已暂停", rows[0]["body"])
        self.assertNotIn("约可再跑", rows[0]["body"])

    def test_idle_disabled_and_platform_tenants_are_skipped(self):
        self._seed_activity(4)                 # 停用租户即使有活动也不推
        scheduler._run_daily_digest(self.now)

        self.assertEqual([], self._digests(1))
        self.assertEqual([], self._digests(3))  # 无活动无风险 → 不骚扰
        self.assertEqual([], self._digests(4))
        self.assertIsNone(db.get_setting("daily_digest_sent:3"))

    def test_same_day_second_run_does_not_resend(self):
        self._seed_activity(2)
        scheduler._run_daily_digest(self.now)
        scheduler._run_daily_digest(self.now.replace(hour=9, minute=45))

        self.assertEqual(1, len(self._digests(2)))

    def test_switched_off_tenant_is_not_notified(self):
        self._seed_activity(2)
        db.set_setting("daily_digest_off:2", "1")
        scheduler._run_daily_digest(self.now)

        self.assertEqual([], self._digests(2))
        self.assertIsNone(db.get_setting("daily_digest_sent:2"))

    def test_toggle_endpoint_contract(self):
        from app import main

        member = {"id": 21, "tenant_id": 2, "username": "staff",
                  "role": "member", "modules": ["content"]}
        owner = {"id": 20, "tenant_id": 2, "username": "owner",
                 "role": "owner", "modules": ["content"]}

        auth.set_current(member)
        self.assertEqual({"enabled": True}, main.daily_digest_get())
        with self.assertRaises(HTTPException) as denied:
            main.daily_digest_set({"enabled": False})
        self.assertEqual(403, denied.exception.status_code)
        self.assertIsNone(db.get_setting("daily_digest_off:2"))

        auth.set_current(owner)
        self.assertEqual(
            {"ok": True, "enabled": False},
            main.daily_digest_set({"enabled": False}),
        )
        self.assertEqual("1", db.get_setting("daily_digest_off:2"))
        self.assertEqual({"enabled": False}, main.daily_digest_get())

        main.daily_digest_set({"enabled": True})
        self.assertEqual({"enabled": True}, main.daily_digest_get())
        self.assertIsNone(db.get_setting("daily_digest_off:2"))


if __name__ == "__main__":
    unittest.main()
