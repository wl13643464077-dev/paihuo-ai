"""第 2 期提醒与升级：时间窗、免打扰顺延、每级只发一次、@手机号消息格式、按人通知权限。"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from app import checklist, db, notify, reminders

from tests.test_phase2_checklist import D0, DAY, H, Phase2Base

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"


def _notes(uid=None, kind=None):
    sql = "SELECT * FROM notification WHERE tenant_id=2"
    args = []
    if uid is not None:
        sql += " AND user_id=?"
        args.append(uid)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return db.q(sql + " ORDER BY id", tuple(args))


class PureRuleTests(unittest.TestCase):
    def test_quiet_hours_window_beijing_time(self):
        self.assertFalse(reminders.in_quiet_hours(D0 + 21 * H + 59 * 60))
        self.assertTrue(reminders.in_quiet_hours(D0 + 22 * H))
        self.assertTrue(reminders.in_quiet_hours(D0 + 3 * H))
        self.assertTrue(reminders.in_quiet_hours(D0 + 7 * H + 29 * 60))
        self.assertFalse(reminders.in_quiet_hours(D0 + 7 * H + 30 * 60))
        # 顺延到下一个 7:30
        self.assertEqual(D0 + 86400 + 7.5 * H, reminders.quiet_resume_ts(D0 + 23 * H))
        self.assertEqual(D0 + 7.5 * H, reminders.quiet_resume_ts(D0 + 2 * H))
        self.assertEqual(D0 + 12 * H, reminders.quiet_resume_ts(D0 + 12 * H))

    def test_pending_stages_follow_the_ladder(self):
        due = D0 + 15 * H
        s = reminders
        self.assertEqual([], s.pending_stages(due, due - 2 * H - 1, 0))
        self.assertEqual([s.STAGE_PRE], s.pending_stages(due, due - 2 * H, 0))
        self.assertEqual([], s.pending_stages(due, due - H, s.STAGE_PRE))
        self.assertEqual([s.STAGE_DUE], s.pending_stages(due, due, s.STAGE_PRE))
        self.assertEqual([], s.pending_stages(due, due + 60, s.STAGE_PRE | s.STAGE_DUE))
        self.assertEqual([s.STAGE_ESC1], s.pending_stages(
            due, due + s.ESCALATE_GRACE, s.STAGE_PRE | s.STAGE_DUE))
        # 系统停了一天：一次补上到点、升级1、升级2，但不补「截止前」
        self.assertEqual([s.STAGE_DUE, s.STAGE_ESC1, s.STAGE_ESC2],
                         s.pending_stages(due, due + 25 * H, 0))
        self.assertEqual([], s.pending_stages(due, due + 30 * H, 15))

    def test_task_sent_mask_uses_last_remind_and_level(self):
        due = D0 + 15 * H
        s = reminders
        self.assertEqual(0, s.task_sent_mask({"due_at": due, "last_remind_at": None}))
        self.assertEqual(s.STAGE_PRE, s.task_sent_mask(
            {"due_at": due, "last_remind_at": due - H}))
        self.assertEqual(s.STAGE_PRE | s.STAGE_DUE | s.STAGE_ESC1, s.task_sent_mask(
            {"due_at": due, "last_remind_at": due + 1, "escalated_level": 1}))
        # 截止时间被改晚：之前的提醒不再算数
        self.assertEqual(0, s.task_sent_mask(
            {"due_at": due + 5 * H, "last_remind_at": due - H}))

    def test_text_message_mentions_clean_mobiles_only(self):
        body = notify.text_message("朝阳店开店清单还差 3 项", [
            "13800000022", " 138-0000-0023 ", "+8613800000024", "8613800000025",
            "12345", "", None, "13800000022", "@all"])
        self.assertEqual("text", body["msgtype"])
        self.assertEqual("朝阳店开店清单还差 3 项", body["text"]["content"])
        self.assertEqual(["13800000022", "13800000023", "13800000024", "13800000025"],
                         body["text"]["mentioned_mobile_list"])
        plain = notify.text_message("没人可以 @", ["", "abc"])
        self.assertNotIn("mentioned_mobile_list", plain["text"])
        long = notify.text_message("字" * 2000)
        self.assertLessEqual(len(long["text"]["content"].encode()), notify.TEXT_MAX_BYTES)
        self.assertTrue(long["text"]["content"].endswith("…"))

    def test_webhook_text_groups_by_stage_and_caps_lines(self):
        lines = {reminders.STAGE_ESC1: ["a"], reminders.STAGE_PRE: [str(i) for i in range(15)]}
        text = reminders.webhook_text(lines, max_lines=5)
        self.assertLess(text.index("快到截止时间"), text.index("请店长跟进"))
        self.assertIn("另有 11 条", text)


class ReminderFlowTests(Phase2Base):
    def add_task(self, due, *, assignee=23, branch=None, status="todo", title="补货上架"):
        return db.insert("staff_task", {
            "tenant_id": 2, "branch_id": branch or self.a, "assignee_user_id": assignee,
            "title": title, "due_at": due, "status": status, "created_at": D0,
        })

    def tick(self, ts):
        return reminders.process_tenant(2, ts)

    def test_staff_task_ladder_each_level_once(self):
        due = D0 + 15 * H
        task = self.add_task(due)
        self.tick(due - 3 * H)
        self.assertEqual([], _notes())
        self.tick(due - 90 * 60)                               # 截止前 2 小时内
        pre = _notes(23, "staff_remind")
        self.assertEqual(1, len(pre))
        self.assertIn("快到截止时间", pre[0]["title"])
        self.assertIn("朝阳店", pre[0]["body"])
        self.tick(due - 60 * 60)                               # 不重复
        self.assertEqual(1, len(_notes()))
        self.tick(due + 5 * 60)                                # 到点
        self.assertEqual(2, len(_notes(23, "staff_remind")))
        self.tick(due + 10 * 60)
        self.assertEqual(2, len(_notes()))
        self.tick(due + 35 * 60)                               # 升级 1：店长
        esc1 = _notes(22, "staff_escalate")
        self.assertEqual(1, len(esc1))
        self.assertIn("店长跟进", esc1[0]["body"])
        self.assertEqual("#/staff-tasks", esc1[0]["link"])
        self.assertEqual([], _notes(20))
        self.tick(due + 2 * H)
        self.tick(due + 24 * H + 5 * 60)                       # 升级 2：老板
        boss = _notes(20, "staff_escalate")
        self.assertEqual(1, len(boss))
        self.assertIn("老板过问", boss[0]["body"])
        self.tick(due + 26 * H)
        self.assertEqual(4, len(_notes()))                     # 提醒 2 + 店长 1 + 老板 1
        row = db.one("SELECT * FROM staff_task WHERE id=?", (task,))
        self.assertEqual(2, row["remind_count"])
        self.assertEqual(2, row["escalated_level"])
        kinds = [e["kind"] for e in db.q(
            "SELECT kind FROM staff_task_event WHERE task_id=? ORDER BY id", (task,))]
        self.assertEqual(["reminded", "reminded", "escalated", "escalated"], kinds)

    def test_finished_or_cancelled_tasks_are_left_alone(self):
        due = D0 + 15 * H
        self.add_task(due, status="submitted")
        self.add_task(due, status="cancelled")
        self.tick(due + 25 * H)
        self.assertEqual([], _notes())

    def test_quiet_hours_defer_to_morning_for_staff_and_boss(self):
        due = D0 + 23 * H
        self.add_task(due)
        stats = self.tick(D0 + 22 * H + 10 * 60)              # 截止前但在免打扰时段
        self.assertEqual([], _notes())
        self.assertGreater(stats["deferred"], 0)
        self.tick(D0 + 86400 + 3 * H)                          # 半夜
        self.assertEqual([], _notes())
        self.tick(D0 + 86400 + 7.5 * H + 60)                   # 早上 7:31
        self.assertEqual(1, len(_notes(23, "staff_remind")))   # 只发「到点」，不补「截止前」
        self.assertIn("还没做完", _notes(23)[0]["body"])
        self.assertEqual(1, len(_notes(22, "staff_escalate")))
        # 升级给老板的时刻(次日 23:00)也在免打扰里，顺延到第三天早上
        self.tick(D0 + 2 * 86400 - H + 60)
        self.assertEqual([], _notes(20))
        self.tick(D0 + 2 * 86400 + 7.5 * H)
        self.assertEqual(1, len(_notes(20, "staff_escalate")))

    def test_unassigned_task_reminds_all_bound_members_and_skips_self_escalation(self):
        due = D0 + 15 * H
        self.add_task(due, assignee=None)
        self.tick(due - H)
        self.assertEqual({22, 23}, {n["user_id"] for n in _notes(kind="staff_remind")})
        # 店长自己的活超时：升级 1 不再发给他自己，24 小时后到老板
        self.add_task(due, assignee=22, title="店长自己的活")
        self.tick(due + H)
        esc = [n for n in _notes(kind="staff_escalate") if "店长自己的活" in n["body"]]
        self.assertEqual([], esc)

    def test_checklist_runs_use_setting_log_and_prune(self):
        checklist.ensure_templates(2, now=D0 - 86400)
        checklist.generate_runs(date=DAY, now=D0 + 60)
        now = D0 + 9 * H                                       # 开店 10:00 截止前 1 小时
        reminders.run_once(now)
        a_open = _notes(22, "staff_remind")                    # A 店指派给店长
        self.assertEqual(1, len(a_open))
        self.assertIn("开店清单", a_open[0]["body"])
        self.assertEqual(1, len(_notes(24, "staff_remind")))   # B 店没指派：绑定店员都收到
        log = json.loads(db.get_setting("reminder_log:2"))
        self.assertEqual(2, len(log))
        self.assertTrue(all(v == reminders.STAGE_PRE for v in log.values()))
        reminders.run_once(now + 300)
        self.assertEqual(2, len(_notes()))
        reminders.run_once(D0 + 10 * H + 60)                   # 到点：标 missed + 再提醒
        self.assertEqual({"missed"}, {r["status"] for r in self.runs(kind="open")})
        self.assertEqual(4, len(_notes(kind="staff_remind")))
        # B 店补做完 → 从记录里清掉，不再升级
        run_b = self.runs(self.b, "open")[0]
        db.execute("UPDATE checklist_run SET completed_at=? WHERE id=?",
                   (D0 + 10 * H + 120, run_b["id"]))
        reminders.run_once(D0 + 10 * H + 40 * 60)
        log = json.loads(db.get_setting("reminder_log:2"))
        self.assertFalse(any(k.startswith(f"cr:{run_b['id']}:") for k in log))
        # A 店指派的就是店长本人：升级 1 没有别的店长可发
        self.assertEqual([], _notes(kind="staff_escalate"))

    def test_inspection_action_assignee_is_reminded_and_escalated(self):
        visit = db.insert("inspection_visit", {"tenant_id": 2, "industry_key": "tea_coffee",
                                               "branch_id": self.a, "status": "completed"})
        issue = db.insert("inspection_issue", {"tenant_id": 2, "visit_id": visit,
                                               "title": "吧台积水", "severity": "high"})
        due = D0 + 15 * H
        action = db.insert("inspection_action", {
            "tenant_id": 2, "visit_id": visit, "issue_id": issue, "plan": "拖干并加防滑垫",
            "status": "open", "due_at": due, "assignee_user_id": 23})
        self.tick(due + H)
        self.assertIn("吧台积水", _notes(23, "staff_remind")[0]["body"])
        self.assertEqual(1, len(_notes(22, "staff_escalate")))
        self.tick(due + 2 * H)
        self.assertEqual(2, len(_notes()))
        # 改派给别人：对新负责人重新提醒
        db.execute("UPDATE inspection_action SET assignee_user_id=24 WHERE id=?", (action,))
        self.tick(due + 3 * H)
        self.assertEqual(1, len(_notes(24, "staff_remind")))
        # 提交复查等老板审核：不再催
        db.execute("UPDATE inspection_action SET status='awaiting_recheck' WHERE id=?",
                   (action,))
        self.tick(due + 25 * H)
        self.assertEqual([], _notes(20))

    def test_webhook_gets_one_text_message_with_mentions(self):
        notify.set_webhook(2, WEBHOOK)
        due = D0 + 15 * H
        self.add_task(due, title="擦玻璃")
        self.add_task(due, assignee=24, branch=self.b, title="清点库存")
        with mock.patch.object(notify, "send_text_sync", return_value=True) as sent:
            stats = self.tick(due + 5 * 60)
        self.assertEqual(1, sent.call_count)
        tid, content, mobiles = sent.call_args.args
        self.assertEqual(2, tid)
        self.assertIn("擦玻璃", content)
        self.assertIn("清点库存", content)
        self.assertEqual(["13800000023"], mobiles)            # 24 没填手机号就不 @
        self.assertEqual(1, stats["webhook"])
        with mock.patch.object(notify, "send_text_sync", return_value=True) as sent:
            self.tick(due + 10 * 60)
        sent.assert_not_called()

    def test_send_text_sync_posts_mentioned_mobile_list(self):
        notify.set_webhook(2, WEBHOOK)
        response = mock.Mock()
        response.json.return_value = {"errcode": 0}
        with mock.patch.object(notify.httpx, "post", return_value=response) as post:
            self.assertTrue(notify.send_text_sync(2, "开店清单还差 2 项", ["13800000022"]))
        url = post.call_args.args[0]
        body = post.call_args.kwargs["json"]
        self.assertEqual(WEBHOOK, url)
        self.assertEqual({"msgtype": "text", "text": {
            "content": "开店清单还差 2 项", "mentioned_mobile_list": ["13800000022"]}}, body)
        self.assertEqual(["13800000022", "13800000023"],
                         notify.mobiles_for_users(2, [22, 23, 24, 30, "x"]))

    def test_push_to_users_records_per_person_and_mentions(self):
        notify.set_webhook(2, WEBHOOK)
        with mock.patch.object(notify, "send_text_sync", return_value=True) as sent, \
                mock.patch.object(notify, "_run_detached", side_effect=lambda fn, *a: fn(*a)):
            ids = notify.push_to_users(2, "staff_remind",
                                       {"headline": "快到点了", "text": "朝阳店开店清单"},
                                       [22, 23, 23])
        self.assertEqual(2, len(ids))
        self.assertEqual((2, "朝阳店开店清单", ["13800000022", "13800000023"]),
                         sent.call_args.args)

    def test_personal_notifications_are_private_to_the_recipient(self):
        self.assertIsNone(notify.record(2, "staff_remind", {"text": "x"}))   # 不广播
        nid = notify.record(2, "staff_remind", {"headline": "快到点了", "text": "开店清单"},
                            target_user_id=23)
        staff = {"id": 23, "role": "member", "modules": [], "enabled": 1}
        other = {"id": 24, "role": "member", "modules": ["tea_coffee"], "enabled": 1}
        owner = {"id": 20, "role": "owner", "enabled": 1}
        self.assertEqual([nid], [n["id"] for n in notify.unread_for_user(2, staff)])
        self.assertEqual([], notify.unread_for_user(2, other))
        self.assertEqual([], notify.unread_for_user(2, owner))
        self.assertEqual(1, notify.mark_read(2, staff, [nid]))
        self.assertFalse(notify.can_view(staff, {"kind": "staff_remind", "user_id": None}))

    def test_run_once_generates_marks_and_is_safe_without_data(self):
        with mock.patch("app.scheduler._run_daily_digest") as digest:
            stats = reminders.run_once(D0 + 6 * H)             # 6 点：还没到早报时间
            digest.assert_not_called()
            self.assertEqual(6, stats["generated"])
            reminders.run_once(D0 + 8 * H + 60)
            digest.assert_called_once()
        self.assertEqual(6, len(self.runs()))


if __name__ == "__main__":
    unittest.main()
