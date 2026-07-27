"""通知定向合同:财务类只发企业主;广播的按人已读,互不吞未读。"""
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
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 30})
        self.owner_id = db.insert("users", {
            "tenant_id": 2, "username": "laoban",
            "password_hash": "x", "role": "owner"})
        self.member_id = db.insert("users", {
            "tenant_id": 2, "username": "yuangong",
            "password_hash": "x", "role": "member",
            "modules_json": '["content"]'})

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _as(self, uid, role, modules=("content",)):
        auth.set_current({"id": uid, "tenant_id": 2, "username": "u",
                          "role": role, "modules": list(modules)})

    def _visible_ids(self):
        return {n["id"] for n in main.state()["notifications"]}

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

    def test_member_cannot_read_owner_targeted_notification(self):
        nid = notify.record(2, "schedule_paused", {"title": "日更"})
        self._as(self.member_id, "member")
        self.assertEqual(
            0, main.notifications_read({"ids": [nid]})["updated"],
            "定向给老板的通知,员工既看不到也标不了已读")
        self._as(self.owner_id, "owner")
        self.assertIn(nid, self._visible_ids())


if __name__ == "__main__":
    unittest.main()
