import json
import logging
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app import auth, db


TEST_SESSION_SECRET_A = (
    "A1b2C3d4E5f6G7h8I9j0K_l-MnOpQrStUvWxYz0123456789abcdef"
)
TEST_SESSION_SECRET_B = (
    "Z9y8X7w6V5u4T3s2R1q0P_onMlKjIhGfEdCbA9876543210zyxwv"
)


def _request(peer: str, headers=None, scheme: str = "http") -> Request:
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": scheme,
        "path": "/api/auth/login",
        "raw_path": b"/api/auth/login",
        "query_string": b"",
        "headers": raw_headers,
        "client": (peer, 12345),
        "server": ("paihuo.test", 80),
    })


class AuthEntrySecurityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "auth-entry.db")
        db.conn()

        from app import main
        main._login_fails.clear()

    def tearDown(self):
        from app import main
        main._login_fails.clear()
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_forwarded_chain_is_used_only_from_configured_trusted_proxy(self):
        from app import main

        with patch.dict(os.environ, {
            "CONTENTCREW_TRUSTED_PROXIES": "127.0.0.1/32,10.0.0.0/8",
        }):
            direct = _request(
                "198.51.100.20",
                {"x-forwarded-for": "203.0.113.200"},
            )
            self.assertEqual("198.51.100.20", main._client_ip(direct))

            caddy = _request(
                "127.0.0.1",
                {"x-forwarded-for": "198.51.100.30"},
            )
            self.assertEqual("198.51.100.30", main._client_ip(caddy))

            proxy_chain = _request(
                "127.0.0.1",
                {"x-forwarded-for": "198.51.100.40, 10.2.3.4"},
            )
            self.assertEqual("198.51.100.40", main._client_ip(proxy_chain))

            spoofed_left_edge = _request(
                "127.0.0.1",
                {"x-forwarded-for": "192.0.2.99, 198.51.100.50"},
            )
            self.assertEqual("198.51.100.50", main._client_ip(spoofed_left_edge))

    def test_invalid_forwarded_chain_fails_closed_and_proto_uses_same_trust_boundary(self):
        from app import main

        with patch.dict(os.environ, {
            "CONTENTCREW_TRUSTED_PROXIES": "127.0.0.1/32",
        }):
            malformed = _request(
                "127.0.0.1",
                {"x-forwarded-for": "not-an-ip, 203.0.113.10"},
            )
            self.assertEqual("127.0.0.1", main._client_ip(malformed))

            untrusted_https_spoof = _request(
                "198.51.100.60",
                {"x-forwarded-proto": "https"},
            )
            self.assertFalse(main._request_is_https(untrusted_https_spoof))

            caddy_https = _request(
                "127.0.0.1",
                {"x-forwarded-proto": "https"},
            )
            self.assertTrue(main._request_is_https(caddy_https))

    def test_login_failure_cache_stays_bounded_without_unlocking_protected_key(self):
        from app import main

        request = _request("198.51.100.70")
        with patch.object(main, "_LOGIN_MAX", 3), \
                patch.object(main, "_LOGIN_CACHE_MAX", 3), \
                patch.object(db, "one", return_value=None):
            for _ in range(3):
                with self.assertRaises(HTTPException) as caught:
                    main.auth_login({"username": "protected", "password": "wrong"}, request)
                self.assertEqual(401, caught.exception.status_code)

            for i in range(12):
                with self.assertRaises(HTTPException) as caught:
                    main.auth_login({"username": f"noise-{i}", "password": "wrong"}, request)
                self.assertEqual(401, caught.exception.status_code)

            self.assertLessEqual(len(main._login_fails), 3)
            with self.assertRaises(HTTPException) as caught:
                main.auth_login({"username": "protected", "password": "wrong"}, request)
            self.assertEqual(429, caught.exception.status_code)

    def test_bootstrap_password_comes_from_environment_and_is_never_logged_or_persisted(self):
        secret = "bootstrap-only-secret-2026"
        with patch.dict(os.environ, {
            "CONTENTCREW_BOOTSTRAP_PASSWORD": secret,
            "CONTENTCREW_SESSION_SECRET": TEST_SESSION_SECRET_A,
        }, clear=False), self.assertLogs("auth", level=logging.WARNING) as captured:
            created = auth.bootstrap()

        self.assertEqual({"username": "boss"}, created)
        root = db.one("SELECT * FROM users WHERE username='boss'")
        self.assertTrue(auth.check_pw(secret, root["password_hash"]))
        self.assertEqual(0, root["must_change_password"])
        self.assertIsNone(db.get_setting("bootstrap_pw"))
        self.assertNotIn(secret, "\n".join(captured.output))

        with patch.dict(
            os.environ,
            {"CONTENTCREW_SESSION_SECRET": TEST_SESSION_SECRET_A},
            clear=False,
        ):
            token = auth.make_session(root["id"])
            parts = token.split(".")
            self.assertEqual("v2", parts[0])
            self.assertEqual(64, len(parts[-1]))
            self.assertEqual(root["id"], auth.parse_session(token))
            with patch.dict(
                os.environ,
                {"CONTENTCREW_SESSION_SECRET": TEST_SESSION_SECRET_B},
                clear=False,
            ):
                self.assertIsNone(auth.parse_session(token))
            auth.revoke_sessions(root["id"])
            self.assertIsNone(auth.parse_session(token))
            renewed = auth.make_session(root["id"])
            self.assertEqual(root["id"], auth.parse_session(renewed))
            db.update("users", root["id"], {"password_hash": auth.hash_pw("new-secret")})
            self.assertIsNone(auth.parse_session(renewed))
            self.assertIsNone(auth.parse_session("1.4102444800." + "a" * 64))

        db.set_setting("bootstrap_pw", "legacy-plaintext")
        self.assertEqual({}, auth.bootstrap())
        self.assertIsNone(db.get_setting("bootstrap_pw"))

    def test_empty_database_requires_explicit_bootstrap_secret(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONTENTCREW_BOOTSTRAP_PASSWORD", None)
            with self.assertRaisesRegex(RuntimeError, "CONTENTCREW_BOOTSTRAP_PASSWORD"):
                auth.bootstrap()
        self.assertIsNone(db.one("SELECT id FROM users LIMIT 1"))

    def test_password_policy_rejects_short_or_single_class_values(self):
        self.assertIn("至少 12 位", auth.password_policy_error("Ab1-short"))
        self.assertIn("至少包含", auth.password_policy_error("abcdefghijklmnop"))
        self.assertIn("不能超过", auth.password_policy_error("Aa1!" * 40))
        self.assertIn("字符串", auth.password_policy_error(["correct-horse-2026"]))
        self.assertIn("字符串", auth.password_policy_error({"password": "safe-2026"}))
        self.assertEqual("", auth.password_policy_error("correct-horse-2026"))
        self.assertEqual("", auth.password_policy_error("中文安全长口令2026版"))

    def test_all_account_write_paths_enforce_the_same_strong_password_policy(self):
        from app import main

        tenant_id = db.insert("tenants", {"name": "安全测试企业"})
        owner_id = db.insert("users", {
            "tenant_id": tenant_id,
            "username": "owner-security",
            "password_hash": auth.hash_pw("existing-password-2026"),
            "role": "owner",
            "modules_json": "[]",
            "enabled": 1,
        })
        member_id = db.insert("users", {
            "tenant_id": tenant_id,
            "username": "member-security",
            "password_hash": auth.hash_pw("existing-member-2026"),
            "role": "member",
            "modules_json": "[]",
            "enabled": 1,
        })
        auth.set_current({
            "id": owner_id, "tenant_id": tenant_id, "username": "owner-security",
            "role": "owner", "modules": [],
        })

        weak_calls = (
            lambda: main.auth_password(
                {"old": "existing-password-2026", "new": "123456"}
            ),
            lambda: main.team_user_create(
                {"username": "new-member", "password": "123456"}
            ),
            lambda: main.team_user_update(member_id, {"password": "123456"}),
        )
        for call in weak_calls:
            with self.subTest(call=call):
                with self.assertRaises(HTTPException) as caught:
                    call()
                self.assertEqual(400, caught.exception.status_code)
                self.assertIn("12", str(caught.exception.detail))

        strong = "member-reset-2026"
        self.assertEqual(
            {"ok": True},
            main.team_user_update(member_id, {"password": strong}),
        )
        self.assertTrue(auth.check_pw(
            strong,
            db.one("SELECT password_hash FROM users WHERE id=?", (member_id,))[
                "password_hash"
            ],
        ))

        auth.set_current({
            "id": owner_id, "tenant_id": tenant_id, "username": "boss",
            "role": "root", "modules": [],
        })
        with self.assertRaises(HTTPException) as caught:
            main.team_tenant_create({
                "name": "新企业",
                "owner": "new-owner",
                "password": "123456",
            })
        self.assertEqual(400, caught.exception.status_code)
        self.assertIn("12", str(caught.exception.detail))

    def test_legacy_accounts_are_forced_to_change_password_before_using_apis(self):
        from app import main

        tenant_id = db.insert("tenants", {"name": "旧账号企业"})
        user_id = db.insert("users", {
            "tenant_id": tenant_id,
            "username": "legacy-owner",
            "password_hash": auth.hash_pw("legacy-password-2026"),
            "role": "owner",
            "modules_json": "[]",
            "enabled": 1,
            "must_change_password": 1,
        })
        user = auth.get_user(user_id)
        auth.set_current(user)
        self.assertTrue(main.auth_me()["must_change_password"])

        async def scenario():
            blocked = _request("198.51.100.71")
            blocked.scope["method"] = "GET"
            blocked.scope["path"] = "/api/state"
            blocked.scope["raw_path"] = b"/api/state"
            allowed = _request("198.51.100.71")
            allowed.scope["method"] = "PUT"
            allowed.scope["path"] = "/api/auth/password"
            allowed.scope["raw_path"] = b"/api/auth/password"
            next_call = AsyncMock(return_value="passed")
            with patch.object(auth, "parse_session", return_value=user_id):
                response = await main._auth_mw(blocked, next_call)
                self.assertEqual(428, response.status_code)
                self.assertIn(b"must_change_password", response.body)
                self.assertEqual(
                    "passed",
                    await main._auth_mw(allowed, next_call),
                )

        import asyncio
        asyncio.run(scenario())

        self.assertEqual(
            {"ok": True},
            main.auth_password({
                "old": "legacy-password-2026",
                "new": "new-policy-password-2026",
            }),
        )
        self.assertEqual(
            0,
            db.one(
                "SELECT must_change_password FROM users WHERE id=?",
                (user_id,),
            )["must_change_password"],
        )

    def test_schema44_password_upgrade_is_complete_and_idempotent(self):
        connection = db.conn()
        connection.executescript(
            """
            DROP TABLE users;
            CREATE TABLE users(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id INTEGER NOT NULL,
              username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'member',
              modules_json TEXT NOT NULL DEFAULT '[]',
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at REAL,
              updated_at REAL
            );
            """
        )
        connection.execute(
            "INSERT INTO users("
            "tenant_id,username,password_hash,role,enabled"
            ") VALUES(1,'legacy-enabled',?,'owner',1)",
            (auth.hash_pw("possibly-weak-old-password"),),
        )
        connection.execute(
            "INSERT INTO users("
            "tenant_id,username,password_hash,role,enabled"
            ") VALUES(1,'legacy-disabled',?,'member',0)",
            (auth.hash_pw("possibly-weak-disabled-password"),),
        )
        connection.execute(
            "DELETE FROM schema_version"
        )
        connection.execute(
            "INSERT INTO schema_version(version,name,applied_at) "
            "VALUES(44,'production-r5',0)"
        )
        connection.execute("PRAGMA user_version=44")
        connection.commit()
        connection.close()
        db._conn = None

        db.conn()

        self.assertEqual(
            [
                ("legacy-enabled", 1),
                ("legacy-disabled", 0),
            ],
            [
                (row["username"], row["must_change_password"])
                for row in db.q(
                    "SELECT username,must_change_password "
                    "FROM users ORDER BY id"
                )
            ],
        )
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION,
            db.one("SELECT MAX(version) version FROM schema_version")["version"],
        )
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION,
            db.one("PRAGMA user_version")["user_version"],
        )

        # A completed forced change and an account created after the migration
        # must not be flagged again when the process starts a second time.
        db.execute(
            "UPDATE users SET must_change_password=0 "
            "WHERE username='legacy-enabled'"
        )
        db.insert("users", {
            "tenant_id": 1,
            "username": "post-upgrade",
            "password_hash": auth.hash_pw("post-upgrade-password-2026"),
            "role": "member",
            "modules_json": "[]",
            "enabled": 1,
        })
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        db.conn()

        self.assertEqual(
            [
                ("legacy-enabled", 0),
                ("legacy-disabled", 0),
                ("post-upgrade", 0),
            ],
            [
                (row["username"], row["must_change_password"])
                for row in db.q(
                    "SELECT username,must_change_password "
                    "FROM users ORDER BY id"
                )
            ],
        )

    def test_schema45_candidate_also_flags_enabled_legacy_accounts(self):
        connection = db.conn()
        connection.executescript(
            """
            DROP TABLE users;
            CREATE TABLE users(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id INTEGER NOT NULL,
              username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'member',
              modules_json TEXT NOT NULL DEFAULT '[]',
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at REAL,
              updated_at REAL
            );
            DELETE FROM schema_version;
            INSERT INTO schema_version(version,name,applied_at)
              VALUES(45,'safe-free-retry',0);
            PRAGMA user_version=45;
            """
        )
        connection.execute(
            "INSERT INTO users("
            "tenant_id,username,password_hash,role,enabled"
            ") VALUES(1,'schema45-enabled',?,'owner',1)",
            (auth.hash_pw("schema45-existing-password"),),
        )
        connection.commit()
        connection.close()
        db._conn = None

        db.conn()

        self.assertEqual(
            1,
            db.one(
                "SELECT must_change_password FROM users "
                "WHERE username='schema45-enabled'"
            )["must_change_password"],
        )
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION,
            db.one("PRAGMA user_version")["user_version"],
        )

    def test_tenant_and_owner_creation_is_atomic_on_user_insert_failure(self):
        from app import main

        root_tenant = db.insert("tenants", {"name": "平台"})
        root_id = db.insert("users", {
            "tenant_id": root_tenant,
            "username": "boss",
            "password_hash": auth.hash_pw("root-password-2026"),
            "role": "root",
            "modules_json": "[]",
            "enabled": 1,
        })
        auth.set_current({
            "id": root_id, "tenant_id": root_tenant, "username": "boss",
            "role": "root", "modules": [],
        })
        before = db.one("SELECT COUNT(*) n FROM tenants")["n"]
        original_insert = db.insert

        def fail_user(table, data):
            if table == "users":
                raise RuntimeError("simulated user insert failure")
            return original_insert(table, data)

        with patch.object(db, "insert", side_effect=fail_user):
            with self.assertRaises(RuntimeError):
                main.team_tenant_create({
                    "name": "不能留下孤儿的企业",
                    "owner": "atomic-owner",
                    "password": "atomic-password-2026",
                    "industries": [],
                })
        self.assertEqual(
            before,
            db.one("SELECT COUNT(*) n FROM tenants")["n"],
        )

    def test_deprecated_plaintext_pin_interface_is_removed(self):
        from app import main

        self.assertNotIn("boss_pin", main.SECRET_SETTINGS)
        db.set_setting("boss_pin", "legacy-plaintext-pin")
        with patch.object(main, "_need_root"):
            settings = main.settings_get()
            self.assertNotIn("boss_pin_set", settings)
            self.assertEqual(
                os.path.getsize(db.DB_PATH),
                settings["storage"]["db_bytes"],
            )
        source = (
            os.path.join(
                os.path.dirname(__file__), "..", "static", "app.js"
            )
        )
        with open(source, encoding="utf-8") as handle:
            frontend = handle.read()
        for obsolete in (
            "showLock(",
            "tryUnlock(",
            "savePin(",
            "clearPin(",
            'body:{boss_pin:',
        ):
            self.assertNotIn(obsolete, frontend)

    def test_automatic_account_password_also_meets_policy(self):
        from app import main

        result = main._open_account_from_apply({
            "id": 999,
            "phone": "13800138000",
            "name": "安全测试",
            "company": "自动开户测试",
        })
        self.assertEqual("", auth.password_policy_error(result["password"]))

    def test_healthz_response_is_minimal_on_success_and_failure(self):
        from app import main

        self.assertEqual({"status": "ok"}, main.healthz())
        with patch.object(db, "one", side_effect=RuntimeError("db path and secret detail")):
            response = main.healthz()
        self.assertEqual(503, response.status_code)
        self.assertEqual({"status": "unavailable"}, json.loads(response.body))
        self.assertNotIn(b"db path", response.body)


if __name__ == "__main__":
    unittest.main()
