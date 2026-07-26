import json
import os
import tempfile
import unittest

from app import auth, billing, db, meeting


class MeetingBillingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "meeting-billing.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 10})
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner",
            "role": "owner",
            "modules": ["content"],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_meeting_is_created_before_atomic_charge_and_failure_refunds_once(self):
        from app import main

        mid = main._create_charged_meeting({
            "tenant_id": 2,
            "question": "新品是否上线",
            "emp_idxs_json": json.dumps([0, 1]),
            "phase": "queued",
        }, 2)
        self.assertEqual(8, billing.balance(2))
        self.assertEqual(
            {"status": "queued", "billing_status": "charged", "billing_points": 2},
            db.one(
                "SELECT status,billing_status,billing_points "
                "FROM meeting WHERE id=?",
                (mid,),
            ),
        )

        db.update("meeting", mid, {"status": "running", "phase": "brainstorm"})
        self.assertTrue(meeting.settle_failure(mid, "供应商失败"))
        self.assertFalse(meeting.settle_failure(mid, "重复结算"))
        self.assertEqual(10, billing.balance(2))
        self.assertEqual(
            {"status": "failed", "phase": "failed", "billing_status": "refunded"},
            db.one(
                "SELECT status,phase,billing_status FROM meeting WHERE id=?",
                (mid,),
            ),
        )
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) AS n FROM billing_log "
                "WHERE tenant_id=2 AND delta=2"
            )["n"],
        )


if __name__ == "__main__":
    unittest.main()
