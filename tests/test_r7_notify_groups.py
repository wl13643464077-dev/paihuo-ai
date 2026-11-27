"""第七轮：提醒通知瘦身 + 结构化信息（今天页按它把同类通知合并展示）。

- 标题只说「什么事 + 到哪一步」，正文只说「哪家店 + 细节」，两边不重复；
- 站内写门店短名、日期写「9 月 21 日」、没人负责写「还没指派负责人」；
- 新通知带 meta（obj/stage/name/date/branch/remaining/due_at/assignee），老通知没有、不回填。
"""
from __future__ import annotations

import json

from app import checklist, db, notify, reminders

from tests.test_phase2_checklist import D0, DAY, Phase2Base

H = 3600
OWNER = {"id": 20, "role": "owner", "enabled": 1}


def _notes(uid=None, kind=None):
    sql, args = "SELECT * FROM notification WHERE tenant_id=2", []
    if uid is not None:
        sql += " AND user_id=?"
        args.append(uid)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return db.q(sql + " ORDER BY id", tuple(args))


class ReminderTextTests(Phase2Base):
    def setUp(self):
        super().setUp()
        db.execute("UPDATE tenants SET name='阿花奶茶' WHERE id=2")
        db.execute("UPDATE store_branch SET name='阿花奶茶·朝阳店' WHERE id=?", (self.a,))
        db.execute("UPDATE store_branch SET name='阿花奶茶（静安店）' WHERE id=?", (self.b,))
        checklist.ensure_templates(2, now=D0 - 86400)
        checklist.generate_runs(date=DAY, now=D0 + 60)

    def boss_notes(self):
        # 闭店 22:30 截止；第二天 23:00 之后(过 24 小时、已过免打扰)升级到老板
        reminders.process_tenant(2, D0 + 86400 + 23 * H, quiet=False)
        return _notes(20, "staff_escalate")

    def test_short_branch_only_strips_the_company_prefix(self):
        self.assertEqual("天府三街店", reminders.short_branch("王记火锅·天府三街店", "王记火锅"))
        self.assertEqual("天府三街店", reminders.short_branch("王记火锅（天府三街店）", "王记火锅"))
        self.assertEqual("王记火锅", reminders.short_branch("王记火锅", "王记火锅"))
        self.assertEqual("老李烧烤·东门店", reminders.short_branch("老李烧烤·东门店", "王记火锅"))
        self.assertEqual("朝阳店", reminders.short_branch("朝阳店", ""))
        self.assertEqual("9 月 21 日", reminders.date_cn("2026-09-21"))
        self.assertEqual("随便写的", reminders.date_cn("随便写的"))

    def test_boss_escalation_is_slim_and_carries_structured_meta(self):
        notes = self.boss_notes()
        close = [n for n in notes if "闭店清单" in n["title"]]
        self.assertEqual(2, len(close))                       # 两家店各一条
        month_day = reminders.date_cn(DAY)
        for note in close:
            self.assertEqual("闭店清单超时一天多，请过问", note["title"])
            body = note["body"]
            self.assertNotIn("阿花奶茶", body)                 # 站内只写门店短名
            self.assertNotIn(DAY, body)                        # 不写 ISO 日期
            self.assertNotIn("闭店清单", body)                 # 正文不重复标题
            self.assertIn(month_day, body)
            self.assertIn("请您过问", body)
            self.assertNotIn("老板", body)
            self.assertEqual("#/checklists", note["link"])
        by_branch = {json.loads(n["meta_json"])["branch_id"]: n for n in close}
        a_meta = json.loads(by_branch[self.a]["meta_json"])
        b_meta = json.loads(by_branch[self.b]["meta_json"])
        self.assertEqual(
            {"obj": "checklist", "stage": "esc2", "name": "闭店清单", "date": DAY,
             "branch_id": self.a, "branch": "朝阳店", "assignee": "u22"},
            {k: a_meta[k] for k in ("obj", "stage", "name", "date", "branch_id", "branch", "assignee")})
        self.assertGreater(a_meta["remaining"], 0)
        self.assertIn(f"还差 {a_meta['remaining']} 项", by_branch[self.a]["body"])
        self.assertIn("负责人 u22", by_branch[self.a]["body"])
        self.assertEqual(("静安店", ""), (b_meta["branch"], b_meta["assignee"]))
        self.assertIn("还没指派负责人", by_branch[self.b]["body"])
        self.assertTrue(by_branch[self.b]["body"].startswith("静安店 · "))
        # 同一类、同一天的两条，分组用的四个字段完全一样
        key = lambda m: (m["obj"], m["stage"], m["name"], m["date"])
        self.assertEqual(key(a_meta), key(b_meta))

    def test_state_feed_exposes_meta_and_old_rows_stay_plain(self):
        legacy = db.insert("notification", {
            "tenant_id": 2, "kind": "staff_escalate", "user_id": 20, "link": "#/checklists",
            "title": "超时一天多，请过问：阿花奶茶·朝阳店·2026-09-20 闭店清单（还差 8 项）",
            "body": "阿花奶茶·朝阳店·2026-09-20 闭店清单（还差 8 项）已超时 43 小时"})
        self.boss_notes()
        feed = notify.unread_for_user(2, OWNER)
        old = next(n for n in feed if n["id"] == legacy)
        self.assertNotIn("meta", old)                          # 老通知不回填，前端按单条显示
        self.assertNotIn("meta_json", old)
        fresh = [n for n in feed if n.get("meta")]
        self.assertTrue(fresh)
        self.assertTrue(all(n["meta"]["obj"] == "checklist" for n in fresh))
        self.assertTrue(all("meta_json" not in n for n in feed))
        # 整组已读 = 组里每条标已读，别的不动
        group = [n["id"] for n in fresh if n["meta"]["name"] == "闭店清单"]
        self.assertEqual(len(group), notify.mark_read(2, OWNER, group))
        left = {n["id"] for n in notify.unread_for_user(2, OWNER)}
        self.assertFalse(left & set(group))
        self.assertIn(legacy, left)

    def test_task_and_stage_messages_keep_title_and_body_apart(self):
        obj = {"type": "task", "id": 1, "branch_id": self.a, "branch_name": "阿花奶茶·朝阳店",
               "assignee": 23, "due_at": D0 + 15 * H, "label": "派的活「擦玻璃」",
               "group_name": "派给店员的活", "item": "擦玻璃", "date": DAY, "photo": True}
        pre = reminders.build_message(obj, reminders.STAGE_PRE, D0 + 14 * H, "小李", "阿花奶茶")
        self.assertEqual("staff_remind", pre["kind"])
        self.assertEqual("派的活「擦玻璃」快到截止时间了", pre["payload"]["headline"])
        self.assertTrue(pre["payload"]["summary"].startswith("朝阳店 · 还有 1 小时截止"))
        self.assertIn("记得拍照", pre["payload"]["summary"])
        self.assertNotIn("擦玻璃", pre["payload"]["summary"])
        self.assertIn("阿花奶茶·朝阳店", pre["line"])           # 企微群那一行保留全名
        self.assertEqual({"obj": "task", "stage": "pre", "name": "派给店员的活", "item": "擦玻璃"},
                         {k: pre["payload"]["meta"][k] for k in ("obj", "stage", "name", "item")})
        self.assertNotIn("remaining", pre["payload"]["meta"])
        esc = reminders.build_message({**obj, "assignee": None}, reminders.STAGE_ESC1,
                                      D0 + 16 * H, "还没指派", "阿花奶茶")
        self.assertEqual("朝阳店 · 超时 1 小时 · 还没指派负责人，请店长跟进。", esc["payload"]["summary"])
        self.assertEqual("", esc["payload"]["meta"]["assignee"])

    def test_bad_meta_is_dropped_not_stored(self):
        big = notify.record(2, "staff_remind", {"headline": "a", "text": "b",
                                               "meta": {"x": "长" * 5000}}, target_user_id=23)
        odd = notify.record(2, "staff_remind", {"headline": "a", "text": "b",
                                               "meta": ["不是字典"]}, target_user_id=23)
        rows = {r["id"]: r for r in _notes(23)}
        self.assertIsNone(rows[big]["meta_json"])
        self.assertIsNone(rows[odd]["meta_json"])
