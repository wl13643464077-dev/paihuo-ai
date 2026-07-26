"""三人设走查修复批A/B的合同:报错说人话、500 不吐英文、游客留资出口放行、
台账分页、定时连续失败告警、套餐临期提醒。"""
import asyncio
import os
import tempfile
import time
import types
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException

from app import auth, db, main, pubtrack, scheduler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class WalkthroughFixContracts(unittest.TestCase):
    def test_brief_field_errors_use_chinese_labels(self):
        with self.assertRaises(HTTPException) as ctx:
            main._validated_brief({"direction": "x", "material": "字" * 20001})
        self.assertIn("附加素材", ctx.exception.detail)
        self.assertNotIn("material", ctx.exception.detail)
        with self.assertRaises(HTTPException) as bad_type:
            main._validated_brief({"direction": "x", "ref_link": 123})
        self.assertIn("参考链接", bad_type.exception.detail)

    def test_unhandled_exception_returns_chinese_without_leak(self):
        request = types.SimpleNamespace(
            url=types.SimpleNamespace(path="/api/x"))
        secret = RuntimeError("secret-internal-detail")
        response = asyncio.run(main._unhandled_exception(request, secret))
        self.assertEqual(500, response.status_code)
        body = response.body.decode("utf-8")
        self.assertNotIn("secret-internal-detail", body)
        self.assertNotIn("Internal Server Error", body)
        self.assertIn("系统开小差", body)

    def test_tour_middleware_allows_feedback_lead_capture(self):
        src = _read("app/main.py")
        self.assertIn('path == "/api/feedback" and request.method == "POST"',
                      src, "游客留资出口必须在参观模式拦截前放行")

    def test_render_catch_treats_403_as_permission_not_network(self):
        src = _read("static/app.js")
        self.assertIn("这个页面需要更高权限", src)
        self.assertIn('e?.status===403', src)

    def test_job_detail_delete_button_admin_gated(self):
        src = _read("static/app.js")
        self.assertNotIn('title="彻底删除工单及全部产物"', src,
                         "工单详情页删除钮的文案与回收站语义矛盾且对 member 裸奔")

    def test_meeting_pool_filters_by_member_modules(self):
        src = _read("static/app.js")
        self.assertIn("DEPTS.filter(d=>can(d.key)).flatMap", src,
                      "会议室手工选人池必须按板块过滤,否则 member 勾了专家才 403")


class ReturningBossContracts(unittest.TestCase):
    """批B:第30天回头老板的对账合同。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "returning.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 30})
        auth.set_current({"id": 20, "tenant_id": 2, "username": "owner",
                          "role": "owner", "modules": ["content"]})

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_publog_paginated_contract_and_legacy_list(self):
        for i in range(3):
            pubtrack.add_entry(2, "公众号", f"文章{i}")
        page = main.publog_list(limit=2, offset=0)
        self.assertEqual(3, page["total"])
        self.assertEqual(2, len(page["items"]))
        self.assertTrue(page["has_more"])
        self.assertEqual(2, page["next_offset"])
        legacy = main.publog_list()
        self.assertIsInstance(legacy, list, "旧调用方仍拿裸数组,不破坏兼容")

    def test_schedule_consecutive_failure_notifies_at_three(self):
        sid = db.insert("schedule", {
            "tenant_id": 2, "name": "日更", "brief_json": "{}",
            "kind": "daily", "at_time": "09:00", "enabled": 1,
            "next_run_at": time.time() - 60, "fail_streak": 2,
        })
        with patch.object(scheduler, "fire",
                          side_effect=RuntimeError("上游挂了")):
            scheduler._tick(None)
        row = db.one("SELECT fail_streak,last_note FROM schedule WHERE id=?",
                     (sid,))
        self.assertEqual(3, row["fail_streak"])
        self.assertIn("连续失败 3 次", row["last_note"])
        notes = db.q("SELECT title FROM notification WHERE tenant_id=2 "
                     "AND kind='schedule_failed'")
        self.assertEqual(1, len(notes), "连续第 3 次失败必须告警一次")

    def test_billing_monthly_aggregation_by_beijing_month(self):
        now = time.time()
        db.insert("billing_log", {"tenant_id": 2, "delta": -18,
                                  "balance": 12, "reason": "内容工单",
                                  "created_at": now})
        db.insert("billing_log", {"tenant_id": 2, "delta": 100,
                                  "balance": 112, "reason": "充值",
                                  "created_at": now})
        db.insert("billing_log", {"tenant_id": 1, "delta": -99,
                                  "balance": 0, "reason": "别家",
                                  "created_at": now})
        data = main.billing_get()
        months = {m["ym"]: m for m in data["monthly"]}
        this_month = time.strftime("%Y-%m", time.localtime(now))
        self.assertIn(this_month, months)
        self.assertEqual(18, months[this_month]["spent"])
        self.assertEqual(100, months[this_month]["recharged"])

    def test_records_export_four_kinds_and_permission(self):
        db.insert("billing_log", {"tenant_id": 2, "delta": -1, "balance": 29,
                                  "reason": "审查", "created_at": time.time()})
        pubtrack.add_entry(2, "公众号", "一篇")
        db.insert("censor_log", {"tenant_id": 2, "kind": "pre",
                                 "platform": "公众号", "title": "一篇",
                                 "verdict": "pass", "score": 90,
                                 "issues_json": "[]", "report": "全文报告",
                                 "created_at": time.time()})
        db.insert("account_profile", {"tenant_id": 2, "name": "主理人",
                                      "persona_json": "{}"})
        for kind in ("billing", "publog", "censor", "profiles"):
            response = main.records_export(kind)
            self.assertEqual(200, response.status_code, kind)
            self.assertGreater(len(response.body), 500, kind)
        with self.assertRaises(HTTPException) as bad:
            main.records_export("nope")
        self.assertEqual(400, bad.exception.status_code)
        auth.set_current({"id": 21, "tenant_id": 2, "username": "member",
                          "role": "member", "modules": ["content"]})
        for kind in ("billing", "profiles"):
            with self.assertRaises(HTTPException) as denied:
                main.records_export(kind)
            self.assertEqual(403, denied.exception.status_code, kind)

    def test_daily_digest_sent_for_expiring_plan_without_activity(self):
        db.execute("UPDATE tenants SET plan='标准版',plan_expires=? WHERE id=2",
                   (time.time() + 3 * 86400,))
        scheduler._run_daily_digest(datetime.now(scheduler.TZ))
        note = db.one("SELECT body FROM notification WHERE tenant_id=2 "
                      "AND kind='daily_digest'")
        self.assertIsNotNone(note, "套餐临期即使昨日无活动也要提醒")
        self.assertIn("到期", note["body"])


if __name__ == "__main__":
    unittest.main()
