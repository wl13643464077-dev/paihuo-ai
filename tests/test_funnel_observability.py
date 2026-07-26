import json
import os
import tempfile
import unittest

from fastapi import HTTPException
from starlette.requests import Request

from app import auth, db, funnel


def _request(
    path: str = "/api/funnel/events",
    *,
    method: str = "POST",
    peer: str = "198.51.100.42",
    user_agent: str = "FunnelTest/1.0",
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"user-agent", user_agent.encode())],
            "client": (peer, 12345),
            "server": ("paihuo.test", 443),
        }
    )


class FunnelObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "funnel.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台"})
        db.insert("tenants", {"id": 2, "name": "测试企业"})
        self.uid = db.insert(
            "users",
            {
                "tenant_id": 2,
                "username": "owner-test",
                "password_hash": auth.hash_pw("correct-password"),
                "role": "owner",
                "modules_json": "[]",
                "enabled": 1,
            },
        )
        auth.set_current(
            {
                "id": self.uid,
                "tenant_id": 2,
                "username": "owner-test",
                "role": "owner",
                "modules": [],
            }
        )
        from app import main

        main._funnel_public_hits.clear()
        funnel._last_retention_day = ""

    def tearDown(self):
        from app import main

        main._funnel_public_hits.clear()
        auth.set_current(None)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_db_path
        funnel._last_retention_day = ""
        self.tmp.cleanup()

    def test_only_allowlisted_events_and_dimensions_can_be_stored(self):
        with self.assertRaisesRegex(ValueError, "未知漏斗事件"):
            funnel.record(
                "password_captured",
                "anything",
                tenant_id=2,
                actor_key="user:1",
            )
        with self.assertRaisesRegex(ValueError, "维度无效"):
            funnel.record(
                "lead_submitted",
                "raw-campaign-controlled-by-client",
                tenant_id=2,
                actor_key="user:1",
            )
        funnel.record(
            "page_view",
            "../../../../arbitrary",
            tenant_id=2,
            actor_key="user:1",
        )
        row = db.one("SELECT event,dimension FROM funnel_event")
        self.assertEqual({"event": "page_view", "dimension": "other"}, row)

    def test_actor_identifiers_and_extra_client_fields_are_never_persisted(self):
        from app import main

        auth.set_current(None)
        body = {
            "event": "landing_view",
            "dimension": "promo",
            "phone": "13800138000",
            "password": "super-secret",
            "brief": "客户的完整经营方案",
        }
        self.assertEqual({"ok": True}, main.funnel_event(_request(), body))
        self.assertEqual({"ok": True}, main.funnel_event(_request(), body))
        rows = db.q("SELECT * FROM funnel_event")
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["hits"])
        self.assertEqual(64, len(rows[0]["actor_hash"]))
        stored = json.dumps(rows, ensure_ascii=False)
        for forbidden in (
            "198.51.100.42",
            "FunnelTest",
            "13800138000",
            "super-secret",
            "客户的完整经营方案",
        ):
            self.assertNotIn(forbidden, stored)

    def test_anonymous_clients_cannot_forge_business_conversion_events(self):
        from app import main

        auth.set_current(None)
        with self.assertRaises(HTTPException) as caught:
            main.funnel_event(
                _request(),
                {"event": "registration_complete", "dimension": "application"},
            )
        self.assertEqual(403, caught.exception.status_code)
        self.assertIsNone(db.one("SELECT event FROM funnel_event"))

    def test_authenticated_collection_is_content_free_and_account_scoped(self):
        from app import main

        self.assertEqual(
            {"ok": True},
            main.funnel_event(
                _request(),
                {"event": "page_view", "dimension": "tasks", "message": "不要保存"},
            ),
        )
        row = db.one("SELECT tenant_id,event,dimension,actor_hash FROM funnel_event")
        self.assertEqual(2, row["tenant_id"])
        self.assertEqual("page_view", row["event"])
        self.assertEqual("tasks", row["dimension"])
        self.assertNotIn(
            "不要保存",
            json.dumps(db.q("SELECT * FROM funnel_event"), ensure_ascii=False),
        )

    def test_first_assignment_is_exactly_once_per_tenant_across_kinds(self):
        self.assertTrue(funnel.record_first_work(2, "content"))
        self.assertFalse(funnel.record_first_work(2, "expert"))
        rows = db.q(
            "SELECT event,dimension,hits FROM funnel_event "
            "WHERE event='first_job_submitted'"
        )
        self.assertEqual(
            [{"event": "first_job_submitted", "dimension": "content", "hits": 1}],
            rows,
        )

    def test_report_exposes_platform_counts_and_conversion_rates(self):
        funnel.record(
            "landing_view", "promo", tenant_id=0, actor_key="visitor:1"
        )
        funnel.record(
            "landing_view", "promo", tenant_id=0, actor_key="visitor:2"
        )
        funnel.record(
            "lead_submitted", "account_apply", tenant_id=0, actor_key="lead:1"
        )
        db.insert(
            "account_apply",
            {
                "phone": "1",
                "name": "测试",
                "company": "测试企业",
                "status": 1,
                "tenant_id": 2,
                "username": "owner-test",
            },
        )
        funnel.record(
            "registration_complete",
            "application",
            tenant_id=2,
            actor_key="lead:1",
        )
        funnel.record_first_work(2, "content")
        report = funnel.report(30)
        self.assertEqual(2, report["summary"]["landing_view"]["actors"])
        self.assertEqual(1, report["summary"]["lead_submitted"]["actors"])
        self.assertEqual(50.0, report["conversion"]["landing_to_lead_pct"])
        self.assertEqual(
            100.0, report["conversion"]["lead_to_registration_pct"]
        )
        self.assertEqual(
            100.0, report["conversion"]["registration_to_first_job_pct"]
        )

    def test_only_named_boss_can_read_global_funnel(self):
        from app import main

        with self.assertRaises(HTTPException) as caught:
            main.admin_funnel(30)
        self.assertEqual(403, caught.exception.status_code)
        auth.set_current(
            {
                "id": 1,
                "tenant_id": 1,
                "username": "boss",
                "role": "root",
                "modules": [],
            }
        )
        report = main.admin_funnel(30)
        self.assertEqual(30, report["days"])
        self.assertIn("registration_to_first_job_pct", report["conversion"])

    def test_successful_login_is_recorded_by_the_authoritative_backend(self):
        from app import main

        auth.set_current(None)
        response = main.auth_login(
            {"username": "owner-test", "password": "correct-password"},
            _request("/api/auth/login"),
        )
        self.assertEqual(200, response.status_code)
        row = db.one(
            "SELECT tenant_id,event,dimension FROM funnel_event "
            "WHERE event='login_success'"
        )
        self.assertEqual(
            {"tenant_id": 2, "event": "login_success", "dimension": "password"},
            row,
        )

    def test_release_smoke_login_does_not_pollute_product_funnel(self):
        from app import main

        password = "one-time-smoke-password"
        uid = db.insert(
            "users",
            {
                "tenant_id": 2,
                "username": "__release_smoke_fixture",
                "password_hash": auth.hash_pw(password),
                "role": "member",
                "modules_json": "[]",
                "enabled": 1,
            },
        )
        response = main.auth_login(
            {
                "username": "__release_smoke_fixture",
                "password": password,
            },
            _request("/api/auth/login"),
        )
        self.assertEqual(200, response.status_code)
        self.assertIsNone(
            db.one(
                "SELECT event FROM funnel_event "
                "WHERE event='login_success' AND tenant_id=2"
            )
        )
        db.execute("DELETE FROM users WHERE id=?", (uid,))


if __name__ == "__main__":
    unittest.main()
