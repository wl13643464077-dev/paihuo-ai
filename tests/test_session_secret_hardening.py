import math
import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app import auth, db
from app.session_secret import (
    CONFIG_KEY_ENV,
    MIN_EFFECTIVE_ENTROPY_BITS,
    validate_session_secret,
)
from deploy import preflight, session_secret_env


STRONG_SECRET = (
    "A1b2C3d4E5f6G7h8I9j0K_l-MnOpQrStUvWxYz0123456789abcdef"
)


class SessionSecretRuntimeCase(unittest.TestCase):
    def setUp(self):
        self.previous_ephemeral = auth._ephemeral_session_secret
        auth._ephemeral_session_secret = None

    def tearDown(self):
        auth._ephemeral_session_secret = self.previous_ephemeral

    def test_development_fallback_is_process_local_and_never_touches_sqlite(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(
                    db, "get_setting", side_effect=AssertionError("DB read forbidden")
                ), patch.object(
                    db, "set_setting", side_effect=AssertionError("DB write forbidden")
                ):
            first = auth._secret()
            second = auth._secret()

        self.assertIs(first, second)
        self.assertGreaterEqual(len(first), 32)

    def test_production_requirement_fails_closed_when_secret_is_missing_or_weak(self):
        with patch.dict(
            os.environ,
            {"CONTENTCREW_REQUIRE_SESSION_SECRET": "1"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "CONTENTCREW_SESSION_SECRET"):
                auth._secret()

        with patch.dict(
            os.environ,
            {
                "CONTENTCREW_REQUIRE_SESSION_SECRET": "1",
                "CONTENTCREW_SESSION_SECRET": "x" * 64,
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "强度"):
                auth._secret()

        with patch.dict(
            os.environ,
            {
                "CONTENTCREW_REQUIRE_SESSION_SECRET": "1",
                "CONTENTCREW_SESSION_SECRET": STRONG_SECRET,
            },
            clear=True,
        ):
            self.assertEqual(STRONG_SECRET.encode("ascii"), auth._secret())

        with patch.dict(
            os.environ,
            {"CONTENTCREW_REQUIRE_SESSION_SECRET": "yes"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "CONTENTCREW_REQUIRE_SESSION_SECRET"
            ):
                auth._secret()

    def test_validator_requires_32_bytes_and_high_effective_entropy(self):
        for value in ("a" * 31, "a" * 64, "abcd" * 16, "has whitespace " * 8):
            with self.subTest(length=len(value)), self.assertRaises(ValueError):
                validate_session_secret(value)
        validated = validate_session_secret(STRONG_SECRET)
        self.assertEqual(STRONG_SECRET.encode("ascii"), validated)
        self.assertGreaterEqual(
            len(STRONG_SECRET)
            * -sum(
                (STRONG_SECRET.count(char) / len(STRONG_SECRET))
                * math.log2(STRONG_SECRET.count(char) / len(STRONG_SECRET))
                for char in set(STRONG_SECRET)
            ),
            MIN_EFFECTIVE_ENTROPY_BITS,
        )


class SessionSecretPreflightCase(unittest.TestCase):
    def test_manual_preflight_without_production_requirement_does_not_false_fail(self):
        with patch.dict(os.environ, {}, clear=True):
            report = preflight._check_session_secret_environment()
        self.assertEqual(
            {"required": False, "configured": False, "strong": False},
            report,
        )

    def test_preflight_reports_only_safe_secret_metadata(self):
        with patch.dict(
            os.environ,
            {
                "CONTENTCREW_REQUIRE_SESSION_SECRET": "1",
                "CONTENTCREW_SESSION_SECRET": STRONG_SECRET,
            },
            clear=True,
        ):
            report = preflight._check_session_secret_environment()

        self.assertEqual(
            {"required": True, "configured": True, "strong": True},
            report,
        )
        self.assertNotIn(STRONG_SECRET, repr(report))

    def test_preflight_fails_closed_for_required_missing_or_weak_secret(self):
        with patch.dict(
            os.environ,
            {"CONTENTCREW_REQUIRE_SESSION_SECRET": "1"},
            clear=True,
        ):
            with self.assertRaises(preflight.PreflightError):
                preflight._check_session_secret_environment()

        with patch.dict(
            os.environ,
            {
                "CONTENTCREW_REQUIRE_SESSION_SECRET": "1",
                "CONTENTCREW_SESSION_SECRET": "z" * 64,
            },
            clear=True,
        ):
            with self.assertRaises(preflight.PreflightError):
                preflight._check_session_secret_environment()


class SessionSecretEnvironmentFileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name).resolve() / "paihuo"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "paihuo.env"
        self.owner_uid = os.geteuid()
        self.owner_gid = os.getegid()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *, rotate=False):
        return session_secret_env.ensure_session_secret(
            self.path,
            rotate=rotate,
            owner_uid=self.owner_uid,
            owner_gid=self.owner_gid,
        )

    def _values(self) -> dict[str, str]:
        values = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            key, marker, value = line.partition("=")
            if marker:
                values[key] = value
        return values

    def test_ensure_creates_root_only_style_file_without_returning_secret(self):
        result = self._run()

        self.assertEqual(
            {"status": "created", "created": 2, "rotated": 0, "preserved": 0},
            result,
        )
        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))
        self.assertEqual(self.owner_uid, self.path.stat().st_uid)
        self.assertEqual(self.owner_gid, self.path.stat().st_gid)
        values = self._values()
        validate_session_secret(values["CONTENTCREW_SESSION_SECRET"])
        validate_session_secret(values[CONFIG_KEY_ENV])
        self.assertNotEqual(
            values["CONTENTCREW_SESSION_SECRET"],
            values[CONFIG_KEY_ENV],
        )
        self.assertNotIn(values["CONTENTCREW_SESSION_SECRET"], repr(result))
        self.assertNotIn(values[CONFIG_KEY_ENV], repr(result))

    def test_ensure_preserves_other_values_and_rotate_only_changes_session_key(self):
        self.path.write_text(
            "YUNWU_API_KEY=preserve-me\nOTHER=value\n",
            encoding="utf-8",
        )
        self.path.chmod(0o600)
        created = self._run()
        first_values = self._values()
        first_body = self.path.read_bytes()

        self.assertEqual("created", created["status"])
        self.assertEqual(2, created["created"])
        self.assertIn(b"YUNWU_API_KEY=preserve-me", first_body)
        self.assertEqual("unchanged", self._run()["status"])
        self.assertEqual(first_body, self.path.read_bytes())

        rotated = self._run(rotate=True)
        self.assertEqual("rotated", rotated["status"])
        self.assertEqual(1, rotated["rotated"])
        self.assertEqual(1, rotated["preserved"])
        values = self._values()
        self.assertNotEqual(
            first_values["CONTENTCREW_SESSION_SECRET"],
            values["CONTENTCREW_SESSION_SECRET"],
        )
        self.assertEqual(first_values[CONFIG_KEY_ENV], values[CONFIG_KEY_ENV])
        self.assertNotEqual(values["CONTENTCREW_SESSION_SECRET"], values[CONFIG_KEY_ENV])
        self.assertIn("YUNWU_API_KEY=preserve-me", self.path.read_text())
        self.assertNotIn(values["CONTENTCREW_SESSION_SECRET"], repr(rotated))
        self.assertNotIn(values[CONFIG_KEY_ENV], repr(rotated))

    def test_existing_session_key_is_preserved_while_config_key_is_added(self):
        self.path.write_text(
            "CONTENTCREW_SESSION_SECRET=" + STRONG_SECRET + "\n",
            encoding="utf-8",
        )
        self.path.chmod(0o600)

        result = self._run()

        self.assertEqual(
            {"status": "created", "created": 1, "rotated": 0, "preserved": 1},
            result,
        )
        values = self._values()
        self.assertEqual(STRONG_SECRET, values["CONTENTCREW_SESSION_SECRET"])
        validate_session_secret(values[CONFIG_KEY_ENV])
        self.assertNotEqual(STRONG_SECRET, values[CONFIG_KEY_ENV])

    def test_equal_existing_keys_fail_closed_until_explicit_rotation(self):
        self.path.write_text(
            "CONTENTCREW_SESSION_SECRET=" + STRONG_SECRET + "\n"
            + CONFIG_KEY_ENV + "=" + STRONG_SECRET + "\n",
            encoding="utf-8",
        )
        self.path.chmod(0o600)

        with self.assertRaisesRegex(
            session_secret_env.SecretEnvError, "independent"
        ):
            self._run()

        report = self._run(rotate=True)
        values = self._values()
        self.assertEqual(1, report["rotated"])
        self.assertEqual(1, report["preserved"])
        self.assertNotEqual(
            values["CONTENTCREW_SESSION_SECRET"],
            values[CONFIG_KEY_ENV],
        )

    def test_configuration_wrapping_key_cannot_be_rotated_without_rewrap(self):
        self._run()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(
            session_secret_env.SecretEnvError, "atomic rewrap"
        ):
            session_secret_env.ensure_secret_keys(
                self.path,
                rotate_keys=frozenset((CONFIG_KEY_ENV,)),
                owner_uid=self.owner_uid,
                owner_gid=self.owner_gid,
            )
        self.assertEqual(before, self.path.read_bytes())

    def test_root_only_secret_escrow_is_idempotent_and_never_returns_values(self):
        self._run()
        values = self._values()
        escrow_dir = self.directory.parent / "escrow"
        escrow_dir.mkdir(mode=0o700)
        escrow = escrow_dir / "paihuo-env-schema47-r6.backup"

        created = session_secret_env.backup_secret_keys(
            self.path,
            escrow,
            owner_uid=self.owner_uid,
            owner_gid=self.owner_gid,
        )
        self.assertEqual(
            {"status": "created", "validated": 2},
            created,
        )
        self.assertEqual(0o600, stat.S_IMODE(escrow.stat().st_mode))
        self.assertEqual(self.path.read_bytes(), escrow.read_bytes())
        self.assertNotIn(values["CONTENTCREW_SESSION_SECRET"], repr(created))
        self.assertNotIn(values[CONFIG_KEY_ENV], repr(created))

        checked = session_secret_env.check_secret_backup(
            self.path,
            escrow,
            owner_uid=self.owner_uid,
            owner_gid=self.owner_gid,
        )
        self.assertEqual(
            {"status": "verified", "validated": 2},
            checked,
        )
        unchanged = session_secret_env.backup_secret_keys(
            self.path,
            escrow,
            owner_uid=self.owner_uid,
            owner_gid=self.owner_gid,
        )
        self.assertEqual(
            {"status": "unchanged", "validated": 2},
            unchanged,
        )

    def test_secret_escrow_refuses_mismatch_or_insecure_backup(self):
        self._run()
        escrow_dir = self.directory.parent / "escrow"
        escrow_dir.mkdir(mode=0o700)
        escrow = escrow_dir / "paihuo-env-schema47-r6.backup"
        escrow.write_text(
            "CONTENTCREW_SESSION_SECRET=" + STRONG_SECRET + "\n",
            encoding="utf-8",
        )
        escrow.chmod(0o600)

        with self.assertRaises(session_secret_env.SecretEnvError):
            session_secret_env.backup_secret_keys(
                self.path,
                escrow,
                owner_uid=self.owner_uid,
                owner_gid=self.owner_gid,
            )
        self.assertEqual(
            "CONTENTCREW_SESSION_SECRET=" + STRONG_SECRET + "\n",
            escrow.read_text(encoding="utf-8"),
        )

        escrow.unlink()
        escrow.write_bytes(self.path.read_bytes())
        escrow.chmod(0o644)
        with self.assertRaises(session_secret_env.SecretEnvError):
            session_secret_env.check_secret_backup(
                self.path,
                escrow,
                owner_uid=self.owner_uid,
                owner_gid=self.owner_gid,
            )

    def test_command_line_backup_and_check_emit_only_safe_status(self):
        self._run()
        values = self._values()
        escrow_dir = self.directory.parent / "escrow"
        escrow_dir.mkdir(mode=0o700)
        escrow = escrow_dir / "paihuo-env-schema47-r6.backup"

        output = io.StringIO()
        with patch.object(
            session_secret_env.os, "geteuid", return_value=0
        ), patch.object(
            session_secret_env.os, "getegid", return_value=self.owner_gid
        ), patch.object(
            session_secret_env, "backup_secret_keys"
        ) as backup, redirect_stdout(output):
            backup.return_value = {"status": "created", "validated": 2}
            self.assertEqual(
                0,
                session_secret_env.main(
                    [
                        "--path",
                        str(self.path),
                        "--backup-to",
                        str(escrow),
                    ]
                ),
            )
            backup.assert_called_once_with(str(self.path), str(escrow))
        self.assertIn('"status": "created"', output.getvalue())
        for value in values.values():
            self.assertNotIn(value, output.getvalue())

        output = io.StringIO()
        with patch.object(
            session_secret_env.os, "geteuid", return_value=0
        ), patch.object(
            session_secret_env, "check_secret_backup"
        ) as check, redirect_stdout(output):
            check.return_value = {"status": "verified", "validated": 2}
            self.assertEqual(
                0,
                session_secret_env.main(
                    [
                        "--path",
                        str(self.path),
                        "--check-backup",
                        str(escrow),
                    ]
                ),
            )
            check.assert_called_once_with(str(self.path), str(escrow))
        self.assertIn('"status": "verified"', output.getvalue())
        for value in values.values():
            self.assertNotIn(value, output.getvalue())

    def test_check_mode_is_read_only_and_requires_both_keys(self):
        with self.assertRaises(session_secret_env.SecretEnvError):
            session_secret_env.check_secret_keys(
                self.path,
                owner_uid=self.owner_uid,
                owner_gid=self.owner_gid,
            )
        self.assertFalse(self.path.exists())

        self._run()
        before = self.path.read_bytes()
        result = session_secret_env.check_secret_keys(
            self.path,
            owner_uid=self.owner_uid,
            owner_gid=self.owner_gid,
        )
        self.assertEqual(
            {"status": "unchanged", "created": 0, "rotated": 0, "preserved": 2},
            result,
        )
        self.assertEqual(before, self.path.read_bytes())

    def test_ensure_creates_one_missing_secure_parent_directory(self):
        missing_directory = self.directory.parent / "new-paihuo"
        target = missing_directory / "paihuo.env"

        result = session_secret_env.ensure_session_secret(
            target,
            owner_uid=self.owner_uid,
            owner_gid=self.owner_gid,
        )

        self.assertEqual(2, result["created"])
        self.assertEqual(0o700, stat.S_IMODE(missing_directory.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))

    def test_rejects_symlink_target_and_insecure_existing_file(self):
        target = self.directory / "target.env"
        target.write_text("CONTENTCREW_SESSION_SECRET=" + STRONG_SECRET + "\n")
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaises(session_secret_env.SecretEnvError):
            self._run(rotate=True)

        self.path.unlink()
        self.path.write_text("CONTENTCREW_SESSION_SECRET=" + STRONG_SECRET + "\n")
        self.path.chmod(0o644)
        with self.assertRaises(session_secret_env.SecretEnvError):
            self._run()

    def test_failed_replace_preserves_existing_file_and_cleans_temporary(self):
        self.path.write_text(
            "CONTENTCREW_SESSION_SECRET=" + STRONG_SECRET + "\n",
            encoding="utf-8",
        )
        self.path.chmod(0o600)
        before = self.path.read_bytes()

        with patch.object(
            session_secret_env.os,
            "replace",
            side_effect=OSError("injected replace failure"),
        ):
            with self.assertRaises(OSError):
                self._run(rotate=True)

        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(["paihuo.env"], sorted(item.name for item in self.directory.iterdir()))

    def test_command_line_is_root_only(self):
        stderr = io.StringIO()
        with patch.object(
            session_secret_env.os, "geteuid", return_value=501
        ), redirect_stderr(stderr):
            self.assertEqual(1, session_secret_env.main(["--path", str(self.path)]))
        self.assertIn("RootRequired", stderr.getvalue())
        self.assertNotIn(STRONG_SECRET, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
