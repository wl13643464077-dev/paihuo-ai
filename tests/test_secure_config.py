"""Supplier credentials are authenticated at rest under a root-owned key."""
import base64
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, secureconfig
from deploy import preflight


class SecureConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "secure.db")
        db.conn()
        self.key = secrets.token_urlsafe(48)

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _env(self, **extra):
        values = {
            secureconfig.CONFIG_KEY_ENV: self.key,
            secureconfig.REQUIRE_CONFIG_KEY_ENV: "1",
        }
        values.update(extra)
        return patch.dict(os.environ, values, clear=False)

    def test_round_trip_is_encrypted_and_plaintext_never_reaches_database(self):
        value = "supplier-secret-material"
        with self._env():
            secureconfig.set_secret("yunwu_key", value)
            stored = db.get_setting("yunwu_key")
            self.assertTrue(stored.startswith(secureconfig.ENCRYPTED_PREFIX))
            self.assertNotIn(value, stored)
            self.assertEqual(value, secureconfig.get_secret("yunwu_key"))

    def test_ciphertext_is_bound_to_its_setting_name(self):
        with self._env():
            secureconfig.set_secret("yunwu_key", "first-key")
            ciphertext = db.get_setting("yunwu_key")
            db.set_setting("heygen_key", ciphertext)
            with self.assertRaisesRegex(
                secureconfig.SecureConfigError, "binding"
            ):
                secureconfig.get_secret("heygen_key")

    def test_startup_migration_encrypts_every_legacy_secret_atomically(self):
        values = {
            "yunwu_key": "legacy-yunwu",
            "heygen_key": "legacy-heygen",
            "smtp_authcode": "legacy-mail",
        }
        for name, value in values.items():
            db.set_setting(name, value)
        with self._env():
            report = secureconfig.migrate_legacy_secrets()
            self.assertEqual(3, report["encrypted"])
            self.assertEqual(0, report["verified"])
            for name, value in values.items():
                stored = db.get_setting(name)
                self.assertTrue(stored.startswith(secureconfig.ENCRYPTED_PREFIX))
                self.assertNotIn(value, stored)
                self.assertEqual(value, secureconfig.get_secret(name))

    def test_missing_or_weak_production_key_fails_closed(self):
        clean = {
            secureconfig.CONFIG_KEY_ENV: "",
            secureconfig.REQUIRE_CONFIG_KEY_ENV: "1",
        }
        with patch.dict(os.environ, clean, clear=False):
            with self.assertRaisesRegex(
                secureconfig.SecureConfigError, "missing"
            ):
                secureconfig.validate_runtime_key()
            with self.assertRaises(secureconfig.SecureConfigError):
                secureconfig.set_secret("yunwu_key", "must-not-persist")
        self.assertIsNone(db.get_setting("yunwu_key"))

        weak = base64.urlsafe_b64encode(b"short").decode("ascii")
        with patch.dict(
            os.environ,
            {
                secureconfig.CONFIG_KEY_ENV: weak,
                secureconfig.REQUIRE_CONFIG_KEY_ENV: "1",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                secureconfig.SecureConfigError, "invalid"
            ):
                secureconfig.validate_runtime_key()

    def test_preflight_and_systemd_require_the_configuration_key(self):
        with patch.dict(
            os.environ,
            {
                secureconfig.CONFIG_KEY_ENV: "",
                secureconfig.REQUIRE_CONFIG_KEY_ENV: "1",
            },
            clear=False,
        ):
            with self.assertRaises(preflight.PreflightError):
                preflight._check_config_key_environment()
        with self._env():
            report = preflight._check_config_key_environment()
        self.assertEqual(
            {"required": True, "configured": True, "strong": True},
            report,
        )
        unit = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "contentcrew.service"
        ).read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=/etc/paihuo/paihuo.env", unit)
        self.assertIn("Environment=CONTENTCREW_REQUIRE_CONFIG_KEY=1", unit)
        self.assertIn("-m deploy.session_secret_env --check", unit)

    def test_corruption_aborts_migration_without_partial_plaintext_rewrite(self):
        with self._env():
            secureconfig.set_secret("yunwu_key", "valid")
            db.set_setting(
                "heygen_key",
                secureconfig.ENCRYPTED_PREFIX + "not-a-token",
            )
            db.set_setting("smtp_authcode", "legacy")
            with self.assertRaises(secureconfig.SecureConfigError):
                secureconfig.migrate_legacy_secrets()
            self.assertEqual("legacy", db.get_setting("smtp_authcode"))

    def test_development_without_machine_key_keeps_legacy_compatibility(self):
        with patch.dict(
            os.environ,
            {
                secureconfig.CONFIG_KEY_ENV: "",
                secureconfig.REQUIRE_CONFIG_KEY_ENV: "",
            },
            clear=False,
        ):
            secureconfig.set_secret("yunwu_key", "local-key")
            self.assertEqual("local-key", db.get_setting("yunwu_key"))
            self.assertEqual("local-key", secureconfig.get_secret("yunwu_key"))

    def test_unknown_setting_names_are_rejected(self):
        with self.assertRaises(ValueError):
            secureconfig.set_secret("session_secret", "not-this-store")
        with self.assertRaises(ValueError):
            secureconfig.get_secret("arbitrary_key")

    def test_runtime_modules_do_not_bypass_authenticated_secret_store(self):
        root = Path(__file__).resolve().parents[1] / "app"
        for filename in (
            "main.py",
            "providers.py",
            "avatar.py",
            "feishu.py",
            "mailer.py",
            "runninghub.py",
        ):
            source = (root / filename).read_text(encoding="utf-8")
            for name in secureconfig.SECRET_SETTING_KEYS:
                with self.subTest(filename=filename, name=name):
                    if f'db.get_setting("{name}")' in source:
                        self.fail(
                            f"{filename} bypasses app.secureconfig for {name}"
                        )


if __name__ == "__main__":
    unittest.main()
