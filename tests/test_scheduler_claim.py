import json
import os
import tempfile
import unittest

from app import db, scheduler


class SchedulerClaimCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "scheduler.db")
        db.conn()
        self.sid = db.insert("schedule", {
            "tenant_id": 1,
            "name": "日更",
            "brief_json": json.dumps({"direction": "新品"}),
            "mode": "copilot",
            "kind": "daily",
            "enabled": 1,
            "next_run_at": 100,
        })

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_due_schedule_is_claimed_once_and_only_owner_can_finish(self):
        token = scheduler._claim_due(self.sid, 101)
        self.assertTrue(token)
        self.assertIsNone(scheduler._claim_due(self.sid, 101))
        self.assertFalse(
            scheduler._finish_claim(
                self.sid,
                "wrong-token",
                next_run_at=999,
                last_note="不应写入",
            )
        )
        self.assertTrue(
            scheduler._finish_claim(
                self.sid,
                token,
                next_run_at=999,
                last_note="执行完成",
            )
        )
        row = db.one("SELECT * FROM schedule WHERE id=?", (self.sid,))
        self.assertEqual(999, row["next_run_at"])
        self.assertEqual("执行完成", row["last_note"])
        self.assertIsNone(row["claim_token"])
        self.assertIsNone(scheduler._claim_due(self.sid, 101))

    def test_expired_lease_can_be_reclaimed_but_old_owner_cannot_overwrite(self):
        old = scheduler._claim_due(self.sid, 101)
        new = scheduler._claim_due(self.sid, 1002)
        self.assertTrue(new)
        self.assertNotEqual(old, new)
        self.assertFalse(
            scheduler._finish_claim(self.sid, old, next_run_at=2000)
        )
        self.assertTrue(
            scheduler._finish_claim(self.sid, new, next_run_at=3000)
        )
        self.assertEqual(
            3000,
            db.one("SELECT next_run_at FROM schedule WHERE id=?", (self.sid,))[
                "next_run_at"
            ],
        )

    def test_same_occurrence_creates_and_charges_exactly_one_job_after_crash(self):
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 100})
        db.update("schedule", self.sid, {"tenant_id": 2})
        engine = _EngineProbe()
        first_schedule = db.one("SELECT * FROM schedule WHERE id=?", (self.sid,))
        occurrence = scheduler.occurrence_key(first_schedule["next_run_at"])

        # 第一个进程已经开单，但模拟它在推进 next_run_at 之前退出。
        first_job = scheduler.fire(first_schedule, engine, occurrence)
        self.assertEqual(82, db.one("SELECT balance FROM tenants WHERE id=2")["balance"])

        # 租约过期后，新进程会再次处理同一个 next_run_at；必须复用原工单。
        second_job = scheduler.fire(
            db.one("SELECT * FROM schedule WHERE id=?", (self.sid,)),
            engine,
            occurrence,
        )
        self.assertEqual(first_job, second_job)
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM job "
                "WHERE source_schedule_id=? AND source_schedule_occurrence=?",
                (self.sid, occurrence),
            )["n"],
        )
        self.assertEqual(82, db.one("SELECT balance FROM tenants WHERE id=2")["balance"])
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM billing_log "
                "WHERE tenant_id=2 AND delta<0"
            )["n"],
        )

    def test_orphan_pending_occurrence_is_resumed_instead_of_duplicated(self):
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 100})
        db.update("schedule", self.sid, {"tenant_id": 2})
        occurrence = scheduler.occurrence_key(100)
        orphan = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "新品"}),
            "mode": "copilot",
            "status": "pending_charge",
            "billing_status": "pending",
            "billing_points": 18,
            "source_schedule_id": self.sid,
            "source_schedule_occurrence": occurrence,
        })

        job_id = scheduler.fire(
            db.one("SELECT * FROM schedule WHERE id=?", (self.sid,)),
            _EngineProbe(),
            occurrence,
        )

        self.assertEqual(orphan, job_id)
        row = db.one("SELECT status,billing_status FROM job WHERE id=?", (orphan,))
        self.assertEqual(
            {"status": "running", "billing_status": "charged"}, row
        )
        self.assertEqual(82, db.one("SELECT balance FROM tenants WHERE id=2")["balance"])


class _EngineProbe:
    def __init__(self):
        self.notified = []
        self.touched = []

    def notify(self, job_id):
        self.notified.append(job_id)

    def touch(self, job_id):
        self.touched.append(job_id)


if __name__ == "__main__":
    unittest.main()
