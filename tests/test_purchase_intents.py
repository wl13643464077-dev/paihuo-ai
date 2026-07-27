import concurrent.futures
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from app import auth, billing, db, main, purchases


class PurchaseIntentApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "purchase.db")
        db.conn()
        for tid, name in ((1, "平台"), (2, "企业甲"), (3, "企业乙")):
            db.insert("tenants", {
                "id": tid,
                "name": name,
                "balance": 0,
            })
        for user in (
            {
                "id": 1, "tenant_id": 1, "username": "boss",
                "role": "root",
            },
            {
                "id": 20, "tenant_id": 2, "username": "owner-a",
                "role": "owner",
            },
            {
                "id": 21, "tenant_id": 2, "username": "member-a",
                "role": "member",
            },
            {
                "id": 22, "tenant_id": 2, "username": "owner-a2",
                "role": "owner",
            },
            {
                "id": 30, "tenant_id": 3, "username": "owner-b",
                "role": "owner",
            },
        ):
            db.insert("users", {
                **user,
                "password_hash": "x",
                "modules_json": "[]",
                "enabled": 1,
            })
        self.plan = billing.PLANS[0]["key"]
        self.period = billing.PERIODS[0]["key"]

    def tearDown(self):
        auth.set_current(None)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _as(self, uid):
        auth.set_current(auth.get_user(uid))

    def _body(self, request_id="request-owner-a-0001", **updates):
        body = {
            "request_id": request_id,
            "plan": self.plan,
            "period": self.period,
            "contact": "微信 owner_a",
            "note": "工作日下午联系",
            "source": "promo",
        }
        body.update(updates)
        return body

    def _create(self, uid=20, **updates):
        self._as(uid)
        return main.purchase_create(self._body(**updates))

    def test_catalog_is_authoritative_and_client_price_fields_are_rejected(self):
        catalog = main.purchase_catalog()
        quote = billing.subscription_quote(self.plan, self.period)
        self.assertEqual("offline_confirmation", catalog["payment_mode"])
        self.assertIn(quote, catalog["quotes"])
        self.assertIn("线下", catalog["payment_notice"])

        self._as(20)
        for field in (
            "price", "points", "amount", "quoted_price", "quoted_points"
        ):
            with self.subTest(field=field):
                body = self._body()
                body[field] = 1
                with self.assertRaises(HTTPException) as caught:
                    main.purchase_create(body)
                self.assertEqual(400, caught.exception.status_code)
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) n FROM purchase_intent")["n"],
        )

        result = main.purchase_create(self._body())
        stored = db.one(
            "SELECT quoted_price,quoted_points FROM purchase_intent "
            "WHERE id=?",
            (result["item"]["id"],),
        )
        self.assertEqual(quote["price"], stored["quoted_price"])
        self.assertEqual(quote["points"], stored["quoted_points"])
        self.assertEqual("offline_confirmation", result["item"]["payment_mode"])
        self.assertEqual("promo", result["item"]["source"])

        with self.assertRaises(HTTPException) as invalid_source:
            main.purchase_create(self._body(
                request_id="request-invalid-source-0001",
                source="forged-campaign",
            ))
        self.assertEqual(400, invalid_source.exception.status_code)

    def test_only_exact_get_catalog_is_public(self):
        async def scenario():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://paihuo.test",
            ) as client:
                catalog = await client.get("/api/purchases/catalog")
                private_list = await client.get("/api/purchases")
                private_create = await client.post(
                    "/api/purchases",
                    json=self._body("request-public-blocked-0001"),
                )
                wrong_method = await client.post("/api/purchases/catalog")
            return catalog, private_list, private_create, wrong_method

        catalog, private_list, private_create, wrong_method = asyncio.run(
            scenario()
        )
        self.assertEqual(200, catalog.status_code)
        self.assertEqual(
            "offline_confirmation", catalog.json()["payment_mode"]
        )
        self.assertEqual(401, private_list.status_code)
        self.assertEqual(401, private_create.status_code)
        self.assertEqual(401, wrong_method.status_code)

    def test_create_is_idempotent_and_request_key_cannot_change_payload_or_owner(self):
        first = self._create()
        replay = self._create()
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["item"], replay["item"])
        self.assertEqual(
            1,
            db.one("SELECT COUNT(*) n FROM purchase_intent")["n"],
        )

        self._as(20)
        with self.assertRaises(HTTPException) as changed:
            main.purchase_create(self._body(note="换成另一个需求"))
        self.assertEqual(409, changed.exception.status_code)

        self._as(22)
        with self.assertRaises(HTTPException) as reused_by_peer:
            main.purchase_create(self._body())
        self.assertEqual(409, reused_by_peer.exception.status_code)

    def test_concurrent_create_has_one_row_and_one_created_result(self):
        body = self._body(request_id="request-concurrent-0001")

        def submit():
            return purchases.create_intent(
                2,
                20,
                request_key=body["request_id"],
                plan_key=body["plan"],
                period_key=body["period"],
                contact=body["contact"],
                note=body["note"],
                source=body["source"],
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _i: submit(), range(6)))
        ids = {result["item"]["id"] for result in results}
        self.assertEqual(1, len(ids))
        self.assertEqual(1, sum(bool(result["created"]) for result in results))
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM purchase_intent "
                "WHERE request_key='request-concurrent-0001'"
            )["n"],
        )

    def test_notification_lookup_failure_does_not_turn_committed_request_into_error(self):
        self._as(20)
        original_q = db.q

        def fail_root_lookup(sql, args=()):
            if "role='root'" in sql:
                raise RuntimeError("notification storage unavailable")
            return original_q(sql, args)

        with patch.object(purchases.db, "q", side_effect=fail_root_lookup):
            result = main.purchase_create(self._body(
                request_id="request-notice-failure-0001"
            ))
        self.assertTrue(result["created"])
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM purchase_intent "
                "WHERE request_key='request-notice-failure-0001'"
            )["n"],
        )

    def test_customer_is_personal_owner_admin_is_tenant_scoped_and_root_is_global(self):
        own = self._create(uid=20, request_id="request-owner-a-0001")
        peer = self._create(uid=22, request_id="request-owner-a2-0001")
        foreign = self._create(uid=30, request_id="request-owner-b-0001")

        self._as(20)
        personal = main.purchase_list()
        self.assertEqual([own["item"]["id"]], [
            item["id"] for item in personal["items"]
        ])
        tenant_admin = main.purchase_admin_list()
        self.assertEqual(
            {own["item"]["id"], peer["item"]["id"]},
            {item["id"] for item in tenant_admin["items"]},
        )
        self.assertEqual(2, main.purchase_admin_stats()["total"])
        with self.assertRaises(HTTPException) as cross_tenant:
            main.purchase_admin_list(tenant_id=3)
        self.assertEqual(403, cross_tenant.exception.status_code)

        self._as(1)
        global_filtered = main.purchase_admin_list(tenant_id=3)
        self.assertEqual([foreign["item"]["id"]], [
            item["id"] for item in global_filtered["items"]
        ])
        self.assertEqual(3, main.purchase_admin_stats()["total"])

        self._as(21)
        with self.assertRaises(HTTPException) as submit:
            main.purchase_create(self._body(request_id="member-request-0001"))
        self.assertEqual(403, submit.exception.status_code)
        with self.assertRaises(HTTPException) as personal_read:
            main.purchase_list()
        self.assertEqual(403, personal_read.exception.status_code)
        with self.assertRaises(HTTPException) as admin_read:
            main.purchase_admin_list()
        self.assertEqual(403, admin_read.exception.status_code)

    def test_status_changes_are_root_only_cas_and_terminal(self):
        intent = self._create()["item"]
        self._as(20)
        with self.assertRaises(HTTPException) as owner_write:
            main.purchase_admin_transition(intent["id"], {
                "expected_status": "requested",
                "status": "contacted",
            })
        self.assertEqual(403, owner_write.exception.status_code)

        self._as(1)
        contacted = main.purchase_admin_transition(intent["id"], {
            "expected_status": "requested",
            "status": "contacted",
            "note": "已通过企业微信联系",
        })
        self.assertTrue(contacted["changed"])
        replay = main.purchase_admin_transition(intent["id"], {
            "expected_status": "requested",
            "status": "contacted",
            "note": "重复请求不会改写备注",
        })
        self.assertFalse(replay["changed"])
        self.assertEqual("已通过企业微信联系", replay["item"]["handler_note"])

        with self.assertRaises(HTTPException) as stale:
            main.purchase_admin_transition(intent["id"], {
                "expected_status": "requested",
                "status": "lost",
                "note": "预算不合适",
            })
        self.assertEqual(409, stale.exception.status_code)
        lost = main.purchase_admin_transition(intent["id"], {
            "expected_status": "contacted",
            "status": "lost",
            "note": "客户暂缓采购",
        })
        self.assertEqual("lost", lost["item"]["status"])
        with self.assertRaises(HTTPException) as terminal:
            main.purchase_admin_transition(intent["id"], {
                "expected_status": "lost",
                "status": "paid",
            })
        self.assertEqual(409, terminal.exception.status_code)
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) n FROM billing_operation")["n"],
        )

    def test_customer_response_hides_internal_followup_fields(self):
        intent = self._create(
            request_id="request-private-followup-0001"
        )["item"]
        self._as(1)
        main.purchase_admin_transition(intent["id"], {
            "expected_status": "requested",
            "status": "contacted",
            "note": "内部：客户预算仍需核验",
        })

        self._as(20)
        customer = main.purchase_list()["items"][0]
        self.assertNotIn("handler_note", customer)
        self.assertNotIn("handled_by", customer)
        self.assertEqual(
            "平台已联系您，请留意沟通消息。",
            customer["status_message"],
        )
        self.assertNotIn("预算", str(customer))

        self._as(1)
        admin = main.purchase_admin_list()["items"][0]
        self.assertEqual("内部：客户预算仍需核验", admin["handler_note"])
        self.assertEqual(1, admin["handled_by"])

    def test_concurrent_paid_reuses_subscription_and_grants_exactly_once(self):
        intent = self._create(
            request_id="request-paid-concurrent-0001"
        )["item"]
        self._as(1)
        main.purchase_admin_transition(intent["id"], {
            "expected_status": "requested",
            "status": "contacted",
        })

        def mark_paid():
            return purchases.transition(
                intent["id"],
                expected_status="contacted",
                target_status="paid",
                actor_id=1,
                note="线下到账已核验",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _i: mark_paid(), range(6)))
        self.assertEqual(1, sum(bool(result["changed"]) for result in results))
        quote = billing.subscription_quote(self.plan, self.period)
        tenant = db.one(
            "SELECT balance,plan FROM tenants WHERE id=2"
        )
        self.assertEqual(quote["points"], tenant["balance"])
        self.assertIn(quote["plan_name"], tenant["plan"])
        logs = db.q(
            "SELECT delta,balance FROM billing_log WHERE tenant_id=2"
        )
        self.assertEqual(
            [(quote["points"], quote["points"])],
            [(row["delta"], row["balance"]) for row in logs],
        )
        operations = db.q(
            "SELECT action,status,op_key FROM billing_operation "
            "WHERE tenant_id=2"
        )
        self.assertEqual(1, len(operations))
        self.assertEqual(("subscribe", "succeeded"), (
            operations[0]["action"], operations[0]["status"]
        ))
        self.assertEqual(
            f"purchase-intent:{intent['id']}",
            operations[0]["op_key"],
        )
        stored = db.one(
            "SELECT status,receipt_json FROM purchase_intent WHERE id=?",
            (intent["id"],),
        )
        self.assertEqual("paid", stored["status"])
        self.assertTrue(stored["receipt_json"])

        root_notice = db.one(
            "SELECT user_id,title,body FROM notification "
            "WHERE kind='purchase_requested'"
        )
        self.assertEqual(1, root_notice["user_id"])
        customer_notices = db.q(
            "SELECT user_id,kind,title,body FROM notification "
            "WHERE kind IN ('purchase_contacted','purchase_paid') ORDER BY id"
        )
        self.assertEqual(
            [(20, "purchase_contacted"), (20, "purchase_paid")],
            [(row["user_id"], row["kind"]) for row in customer_notices],
        )
        rendered_notices = " ".join(
            str(value)
            for row in [root_notice, *customer_notices]
            for value in row.values()
        )
        self.assertNotIn("微信 owner_a", rendered_notices)

        events = db.q(
            "SELECT event,dimension,actor_hash,hits FROM funnel_event "
            "WHERE event LIKE 'purchase_%' ORDER BY event"
        )
        self.assertEqual(
            [("purchase_paid", "subscription", 1),
             ("purchase_requested", "promo", 1)],
            [
                (row["event"], row["dimension"], row["hits"])
                for row in events
            ],
        )
        self.assertTrue(all(
            "owner_a" not in row["actor_hash"] for row in events
        ))

    def test_price_change_blocks_paid_without_points_or_partial_state(self):
        intent = self._create(
            request_id="request-stale-price-0001"
        )["item"]
        self._as(1)
        with patch.dict(billing.PLANS[0], {"sale": 999, "points": 999}):
            with self.assertRaises(HTTPException) as stale_price:
                main.purchase_admin_transition(intent["id"], {
                    "expected_status": "requested",
                    "status": "paid",
                    "note": "不应成交",
                })
        self.assertEqual(409, stale_price.exception.status_code)
        with patch.object(billing, "PLANS", billing.PLANS[1:]):
            with self.assertRaises(HTTPException) as removed_plan:
                main.purchase_admin_transition(intent["id"], {
                    "expected_status": "requested",
                    "status": "paid",
                })
        self.assertEqual(409, removed_plan.exception.status_code)
        self.assertEqual(
            "requested",
            db.one(
                "SELECT status FROM purchase_intent WHERE id=?",
                (intent["id"],),
            )["status"],
        )
        self.assertEqual(
            0,
            db.one("SELECT balance FROM tenants WHERE id=2")["balance"],
        )
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) n FROM billing_operation")["n"],
        )
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) n FROM billing_log")["n"],
        )


class LatestSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "schema49.db")
        db.conn()

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_schema48_database_migrates_purchase_loop_and_job_attribution(self):
        connection = db.conn()
        connection.execute("DROP TABLE purchase_intent")
        connection.execute("DELETE FROM schema_version WHERE version>=49")
        connection.execute("PRAGMA user_version=48")
        connection.commit()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        db.conn()

        columns = {
            row["name"] for row in db.q("PRAGMA table_info(purchase_intent)")
        }
        self.assertTrue({
            "tenant_id", "created_by", "request_key", "plan_key",
            "period_key", "quoted_price", "quoted_points", "status",
            "subscription_op_key", "receipt_json",
        } <= columns)
        indexes = {
            row["name"] for row in db.q("PRAGMA index_list(purchase_intent)")
        }
        self.assertTrue({
            "idx_purchase_intent_request",
            "idx_purchase_intent_owner_created",
            "idx_purchase_intent_admin_status",
            "idx_purchase_intent_subscription_op",
        } <= indexes)
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION,
            db.one("PRAGMA user_version")["user_version"],
        )
        ledger = db.one(
            "SELECT name FROM schema_version WHERE version=49"
        )
        self.assertEqual("purchase-intent-commercial-loop", ledger["name"])
        self.assertEqual(
            "explicit-job-attribution-for-safe-purge",
            db.one(
                "SELECT name FROM schema_version WHERE version=50"
            )["name"],
        )
        for table in (
                "notification", "censor_log", "billing_log",
                "billing_operation"):
            self.assertIn(
                "job_id",
                {
                    row["name"]
                    for row in db.q(f"PRAGMA table_info({table})")
                },
                table,
            )
        self.assertEqual(
            "collaboration-soft-delete-schedule-fail-streak",
            db.one(
                "SELECT name FROM schema_version WHERE version=48"
            )["name"],
        )

    def test_migration_validator_requires_purchase_indexes(self):
        db.execute("DROP INDEX idx_purchase_intent_admin_status")
        with self.assertRaisesRegex(RuntimeError, "purchase_intent 缺少索引"):
            db._validate_migrated_database(db.conn())

    def test_migration_validator_requires_schema48_collaboration_columns(self):
        connection = db.conn()
        connection.execute("ALTER TABLE notification DROP COLUMN user_id")
        connection.execute("ALTER TABLE meeting DROP COLUMN created_by")
        connection.commit()
        with self.assertRaisesRegex(
            RuntimeError,
            r"(notification 缺少列 user_id|meeting 缺少列 created_by)",
        ):
            db._validate_migrated_database(connection)


if __name__ == "__main__":
    unittest.main()
