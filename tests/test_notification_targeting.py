"""通知隔离合同:板块白名单、管理账号精确定向、按人已读与租户隔离。"""
import json
import os
import tempfile
import unittest

from app import auth, db, main, notify


class NotificationTargetingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "target.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "总部", "balance": 100})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 30})
        db.insert("tenants", {"id": 3, "name": "别家企业", "balance": 30})
        self.root_id = db.insert("users", {
            "id": 10, "tenant_id": 1, "username": "boss",
            "password_hash": "x", "role": "root", "enabled": 1})
        self.hq_owner_id = db.insert("users", {
            "id": 11, "tenant_id": 1, "username": "hq-owner",
            "password_hash": "x", "role": "owner", "enabled": 1})
        self.disabled_root_id = db.insert("users", {
            "id": 12, "tenant_id": 1, "username": "root-disabled",
            "password_hash": "x", "role": "root", "enabled": 0})
        self.owner_id = db.insert("users", {
            "tenant_id": 2, "username": "laoban",
            "password_hash": "x", "role": "owner"})
        self.member_id = db.insert("users", {
            "tenant_id": 2, "username": "yuangong",
            "password_hash": "x", "role": "member",
            "modules_json": '["content"]'})
        self.avatar_member_id = db.insert("users", {
            "tenant_id": 2, "username": "avatar-member",
            "password_hash": "x", "role": "member",
            "modules_json": '["avatar"]'})
        self.foreign_owner_id = db.insert("users", {
            "id": 30, "tenant_id": 3, "username": "foreign-owner",
            "password_hash": "x", "role": "owner", "enabled": 1})

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _as(self, uid, role, modules=("content",), tenant_id=2, enabled=1):
        auth.set_current({"id": uid, "tenant_id": tenant_id, "username": "u",
                          "role": role, "modules": list(modules),
                          "enabled": enabled})

    def _visible_ids(self):
        return {n["id"] for n in main.state()["notifications"]}

    def _visible_notifications(self):
        return main.state()["notifications"]

    def test_financial_kind_targets_owner_only(self):
        nid = notify.record(2, "daily_digest", {"date": "07-27",
                                                "summary": "昨日消耗 18 点"})
        self._as(self.owner_id, "owner")
        self.assertIn(nid, self._visible_ids(), "企业主必须能看到经营简报")
        self._as(self.member_id, "member")
        self.assertNotIn(nid, self._visible_ids(),
                         "财务类通知不该出现在员工的收件箱里")

    def test_broadcast_read_is_per_user(self):
        nid = notify.record(2, "pub", {"ok": True, "title": "一篇",
                                       "platform": "小红书"})
        self._as(self.member_id, "member")
        self.assertIn(nid, self._visible_ids())
        self.assertEqual(
            1, main.notifications_read({"ids": [nid]})["updated"])
        self.assertNotIn(nid, self._visible_ids(), "自己读过就不再显示")
        self._as(self.owner_id, "owner")
        self.assertIn(nid, self._visible_ids(),
                      "员工的单条已读绝不能吞掉老板的未读")

    def test_admin_read_all_clears_for_everyone(self):
        notify.record(2, "pub", {"ok": True, "title": "一篇"})
        notify.record(2, "daily_digest", {"date": "07-27", "summary": "x"})
        self._as(self.owner_id, "owner")
        self.assertGreaterEqual(
            main.notifications_read({})["updated"], 2)
        self.assertEqual(set(), self._visible_ids())
        self._as(self.member_id, "member")
        self.assertEqual(set(), self._visible_ids())

    def test_member_reviewed_targets_owner_and_taskcenter_shows_creator(self):
        # 「成员代拍板」定向老板:广播回员工自己只是回声噪音
        nid = notify.record(2, "member_reviewed", {
            "user": "yuangong", "job_id": 1, "station": 3, "approved": True})
        self._as(self.member_id, "member")
        self.assertNotIn(nid, self._visible_ids())
        self._as(self.owner_id, "owner")
        self.assertIn(nid, self._visible_ids())
        # 任务中心列表带发起人用户名(严格限本租户解析)
        import json as _json
        from app import taskcenter
        db.insert("job", {"tenant_id": 2, "status": "done",
                          "created_by": self.member_id,
                          "brief_json": _json.dumps(
                              {"direction": "开业稿"}, ensure_ascii=False)})
        items = taskcenter.list_items(2, {"content"})["items"]
        self.assertEqual("yuangong", items[0].get("creator"))
        # 补齐轮:数字人任务同样带发起人
        db.insert("avatar_job", {"tenant_id": 2, "status": "done",
                                 "created_by": self.member_id,
                                 "params_json": '{"script":"口播"}'})
        avatar_items = [i for i in taskcenter.list_items(
            2, {"content", "avatar"})["items"] if i["kind"] == "avatar"]
        self.assertEqual("yuangong", avatar_items[0].get("creator"))

    def test_owner_missing_skips_inapp_instead_of_broadcast(self):
        db.execute("UPDATE users SET enabled=0 WHERE id=?", (self.owner_id,))
        nid = notify.record(2, "daily_digest", {"date": "07-27",
                                                "summary": "x"})
        self.assertIsNone(nid, "无企业主时宁可不落站内,绝不降级广播")
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) n FROM notification WHERE tenant_id=2")["n"])

    def test_multiple_owners_each_get_financial_notice(self):
        second = db.insert("users", {
            "tenant_id": 2, "username": "laoban2",
            "password_hash": "x", "role": "owner"})
        notify.record(2, "schedule_paused", {"title": "日更"})
        rows = db.q("SELECT user_id FROM notification WHERE tenant_id=2 "
                    "AND kind='schedule_paused'")
        self.assertEqual({self.owner_id, second},
                         {r["user_id"] for r in rows}, "每位企业主各收一份")

    def test_member_cannot_read_owner_targeted_notification(self):
        nid = notify.record(2, "schedule_paused", {"title": "日更"})
        self._as(self.member_id, "member")
        self.assertEqual(
            0, main.notifications_read({"ids": [nid]})["updated"],
            "定向给老板的通知,员工既看不到也标不了已读")
        self._as(self.owner_id, "owner")
        self.assertIn(nid, self._visible_ids())

    def test_module_scoped_broadcast_hides_body_link_and_guessed_mark_read(self):
        content_id = notify.record(2, "pub", {
            "ok": False,
            "title": "CONTENT-ONLY-TITLE",
            "platform": "小红书",
            "why": "CONTENT-ONLY-SUMMARY",
        })
        video_id = notify.record(2, "video", {
            "title": "CONTENT-VIDEO-TITLE",
            "file": "/files/content-video.mp4",
        })

        self._as(self.member_id, "member", ("content",))
        content_view = self._visible_notifications()
        content_text = json.dumps(content_view, ensure_ascii=False)
        self.assertTrue(
            {content_id, video_id}.issubset(
                {row["id"] for row in content_view}
            )
        )
        self.assertIn("CONTENT-VIDEO-TITLE", content_text)
        self.assertEqual(
            2,
            main.notifications_read(
                {"ids": [content_id, video_id]}
            )["updated"],
        )

        self._as(self.avatar_member_id, "member", ("avatar",))
        avatar_view = self._visible_notifications()
        avatar_text = json.dumps(avatar_view, ensure_ascii=False)
        self.assertFalse(
            {content_id, video_id}.intersection(
                {row["id"] for row in avatar_view}
            )
        )
        self.assertNotIn("CONTENT-ONLY-TITLE", avatar_text)
        self.assertNotIn("CONTENT-ONLY-SUMMARY", avatar_text)
        self.assertNotIn("CONTENT-VIDEO-TITLE", avatar_text)
        self.assertEqual(
            0,
            main.notifications_read(
                {"ids": [content_id, video_id]}
            )["updated"],
            "猜中内容板块通知 id 也不能把它标成已读",
        )

        self._as(self.owner_id, "owner")
        self.assertTrue(
            {content_id, video_id}.issubset(self._visible_ids()))
        self._as(self.foreign_owner_id, "owner", tenant_id=3)
        self.assertFalse(
            {content_id, video_id}.intersection(self._visible_ids()))
        self.assertEqual(
            0, main.notifications_read(
                {"ids": [content_id, video_id]})["updated"])

    def test_root_only_and_root_plus_owner_are_exactly_targeted(self):
        root_only_id = notify.record(1, "platform_alert", {
            "title": "总部平台告警", "summary": "仅超级管理员处理"})
        root_rows = db.q(
            "SELECT id,user_id FROM notification "
            "WHERE tenant_id=1 AND kind='platform_alert'")
        self.assertEqual(
            [(root_only_id, self.root_id)],
            [(row["id"], row["user_id"]) for row in root_rows],
        )

        self._as(self.hq_owner_id, "owner", tenant_id=1)
        self.assertNotIn(root_only_id, self._visible_ids())
        self.assertEqual(
            0, main.notifications_read({"ids": [root_only_id]})["updated"])
        self._as(self.root_id, "root", (), tenant_id=1)
        self.assertIn(root_only_id, self._visible_ids())

        boss_notice_id = notify.record(
            1, "daily_digest", {"date": "07-27", "summary": "总部经营简报"})
        boss_rows = db.q(
            "SELECT id,user_id FROM notification "
            "WHERE tenant_id=1 AND kind='daily_digest' ORDER BY user_id")
        self.assertEqual(
            {self.root_id, self.hq_owner_id},
            {row["user_id"] for row in boss_rows},
        )
        self.assertTrue(all(row["user_id"] is not None for row in boss_rows))
        self.assertNotIn(
            self.disabled_root_id, {row["user_id"] for row in boss_rows})
        self.assertIn(boss_notice_id, self._visible_ids())

        self._as(self.hq_owner_id, "owner", tenant_id=1)
        self.assertEqual(
            1,
            len([
                row for row in self._visible_notifications()
                if row["kind"] == "daily_digest"
            ]),
        )

    def test_legacy_root_only_broadcast_is_still_hidden_from_owner(self):
        legacy_id = db.insert("notification", {
            "tenant_id": 1,
            "kind": "platform_alert",
            "title": "LEGACY-ROOT-SECRET",
            "body": "内部进修错误",
            "link": "#/",
        })
        self._as(self.hq_owner_id, "owner", tenant_id=1)
        self.assertNotIn(legacy_id, self._visible_ids())
        self.assertEqual(
            0, main.notifications_read({"ids": [legacy_id]})["updated"])
        self._as(self.root_id, "root", (), tenant_id=1)
        self.assertIn(legacy_id, self._visible_ids())

    def test_owner_read_all_does_not_clear_peer_targeted_notice(self):
        second_owner = db.insert("users", {
            "tenant_id": 2, "username": "laoban-peer",
            "password_hash": "x", "role": "owner", "enabled": 1})
        notify.record(2, "schedule_failed", {
            "title": "日更", "streak": 3})
        rows = db.q(
            "SELECT id,user_id,read_at FROM notification "
            "WHERE tenant_id=2 AND kind='schedule_failed' ORDER BY user_id")
        self.assertEqual(
            {self.owner_id, second_owner},
            {row["user_id"] for row in rows},
        )
        self.assertTrue(all(row["user_id"] is not None for row in rows))

        self._as(self.owner_id, "owner")
        self.assertEqual(1, main.notifications_read({})["updated"])
        own = db.one(
            "SELECT read_at FROM notification "
            "WHERE tenant_id=2 AND kind='schedule_failed' AND user_id=?",
            (self.owner_id,),
        )
        peer = db.one(
            "SELECT read_at FROM notification "
            "WHERE tenant_id=2 AND kind='schedule_failed' AND user_id=?",
            (second_owner,),
        )
        self.assertIsNotNone(own["read_at"])
        self.assertIsNone(peer["read_at"])
        self._as(second_owner, "owner")
        self.assertEqual(
            1,
            len([
                row for row in self._visible_notifications()
                if row["kind"] == "schedule_failed"
            ]),
        )

    def test_disabled_accounts_receive_nothing_and_cannot_mark_read(self):
        disabled_owner = db.insert("users", {
            "tenant_id": 2, "username": "disabled-owner",
            "password_hash": "x", "role": "owner", "enabled": 0})
        notify.record(2, "daily_digest", {
            "date": "07-27", "summary": "只给有效老板"})
        targets = {
            row["user_id"] for row in db.q(
                "SELECT user_id FROM notification "
                "WHERE tenant_id=2 AND kind='daily_digest'")
        }
        self.assertNotIn(disabled_owner, targets)
        self.assertNotIn(None, targets)

        video_id = notify.record(2, "video", {
            "title": "成片", "file": "/files/video.mp4"})
        self._as(
            self.avatar_member_id, "member", ("avatar",), enabled=0)
        self.assertEqual([], self._visible_notifications())
        self.assertEqual(
            0, main.notifications_read({"ids": [video_id]})["updated"])

    def test_notification_history_preserves_read_items_and_module_isolation(self):
        content_id = notify.record(2, "pub", {
            "ok": True, "title": "内容通知", "platform": "小红书"})
        video_id = notify.record(2, "video", {
            "title": "图文成片通知", "file": "/files/video.mp4"})

        self._as(self.member_id, "member", ("content",))
        first = main.notifications_list(limit=20, offset=0, unread=False)
        self.assertEqual(
            [video_id, content_id],
            [row["id"] for row in first["items"]],
        )
        self.assertFalse(first["items"][0]["is_read"])
        self.assertEqual(2, first["total"])

        self.assertEqual(
            2,
            main.notifications_read(
                {"ids": [content_id, video_id]}
            )["updated"],
        )
        unread = main.notifications_list(
            limit=20, offset=0, unread=True)
        history = main.notifications_list(
            limit=20, offset=0, unread=False)
        self.assertEqual([], unread["items"])
        self.assertEqual(
            [video_id, content_id],
            [row["id"] for row in history["items"]],
        )
        self.assertTrue(all(row["is_read"] for row in history["items"]))

        self._as(self.avatar_member_id, "member", ("avatar",))
        avatar_history = main.notifications_list(
            limit=20, offset=0, unread=False)
        self.assertEqual([], avatar_history["items"])

    def test_record_persists_explicit_job_attribution_only_when_supplied(self):
        linked_id = notify.record(2, "video", {
            "job_id": 123,
            "title": "关联工单成片",
            "file": "/files/video.mp4",
        })
        standalone_id = notify.record(2, "video", {
            "title": "独立成片",
            "file": "/files/video-standalone.mp4",
        })

        self.assertEqual(
            123,
            db.one(
                "SELECT job_id FROM notification WHERE id=?", (linked_id,)
            )["job_id"],
        )
        self.assertIsNone(
            db.one(
                "SELECT job_id FROM notification WHERE id=?",
                (standalone_id,),
            )["job_id"]
        )

    def test_frontend_exposes_paged_notification_history(self):
        app_js = os.path.join(
            os.path.dirname(__file__), "..", "static", "app.js")
        with open(app_js, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"notifications":notificationsView', source)
        self.assertIn(
            "/notifications?limit=40&offset=${pageOffset}", source)
        self.assertIn("重要进展读过后仍可追溯", source)
        self.assertIn("notificationHistoryRead", source)


if __name__ == "__main__":
    unittest.main()
