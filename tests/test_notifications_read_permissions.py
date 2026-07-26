"""「全部已读」收归主账号:member 只能单条已读,不能替老板清空待办."""

import os
import tempfile
import unittest

from fastapi import HTTPException

from app import auth, db, main, notify


class NotificationsReadPermissionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "notif.db")
        db.conn()
        db.insert(
            "tenants",
            {"id": 2, "name": "测试企业", "balance": 20, "enabled": 1},
        )

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _as_owner(self):
        auth.set_current(
            {
                "id": 20,
                "tenant_id": 2,
                "username": "owner",
                "role": "owner",
                "modules": [],
            }
        )

    def _as_member(self):
        auth.set_current(
            {
                "id": 21,
                "tenant_id": 2,
                "username": "member",
                "role": "member",
                "modules": ["content"],
            }
        )

    def _push_two_unread(self):
        notify.push(2, "pub", {"ok": False, "platform": "小红书",
                               "title": "发布失败的笔记", "why": "登录态失效",
                               "fix": "重新绑定 Cookie"})
        notify.push(2, "retro_due", {"title": "该复盘了"})
        rows = db.q(
            "SELECT id FROM notification WHERE tenant_id=2 AND read_at IS NULL "
            "ORDER BY id"
        )
        self.assertEqual(2, len(rows))
        return [r["id"] for r in rows]

    def _unread_count(self):
        return db.one(
            "SELECT COUNT(*) n FROM notification "
            "WHERE tenant_id=2 AND read_at IS NULL"
        )["n"]

    def test_member_cannot_read_all_without_ids(self):
        self._push_two_unread()
        self._as_member()
        with self.assertRaises(HTTPException) as denied:
            main.notifications_read({})
        self.assertEqual(403, denied.exception.status_code)
        self.assertIn("一键全读", denied.exception.detail)
        # 老板的待办一条都不能少
        self.assertEqual(2, self._unread_count())

    def test_member_can_mark_single_notification_read(self):
        first_id, _second_id = self._push_two_unread()
        self._as_member()
        result = main.notifications_read({"ids": [first_id]})
        self.assertEqual(1, result["updated"])
        self.assertEqual(1, self._unread_count())

    def test_owner_read_all_clears_tenant_unread(self):
        self._push_two_unread()
        self._as_owner()
        result = main.notifications_read({})
        self.assertEqual(2, result["updated"])
        self.assertEqual(0, self._unread_count())


if __name__ == "__main__":
    unittest.main()
