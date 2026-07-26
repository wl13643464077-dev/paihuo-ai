import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import auth
from deploy.rotate_boss_password import rotate


class PasswordRotationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "contentcrew.db"
        self.output = Path(self.tmp.name) / "boss-credential"
        con = sqlite3.connect(self.db_path)
        con.executescript(
            """
            CREATE TABLE users(
              id INTEGER PRIMARY KEY,
              username TEXT,
              password_hash TEXT,
              role TEXT,
              enabled INTEGER
            );
            CREATE TABLE app_setting(
              key TEXT PRIMARY KEY,
              value TEXT,
              updated_at REAL
            );
            """
        )
        con.execute(
            "INSERT INTO users VALUES(1,'boss',?,'root',1)",
            (auth.hash_pw("weak-old-password"),),
        )
        con.execute(
            "INSERT INTO app_setting VALUES('session_secret','old-secret',0)"
        )
        con.execute(
            "INSERT INTO app_setting VALUES('bootstrap_pw','plaintext',0)"
        )
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rotation_changes_password_and_removes_legacy_session_secrets(self):
        result = rotate(str(self.db_path), str(self.output))
        self.assertEqual("rotated", result["status"])
        self.assertEqual(str(self.output), result["credential_path"])
        self.assertEqual(0o600, os.stat(self.output).st_mode & 0o777)

        fields = dict(
            line.split("=", 1)
            for line in self.output.read_text().splitlines()
            if "=" in line
        )
        con = sqlite3.connect(self.db_path)
        password_hash = con.execute(
            "SELECT password_hash FROM users WHERE username='boss'"
        ).fetchone()[0]
        secret = con.execute(
            "SELECT value FROM app_setting WHERE key='session_secret'"
        ).fetchone()
        legacy = con.execute(
            "SELECT value FROM app_setting WHERE key='bootstrap_pw'"
        ).fetchone()
        con.close()
        self.assertTrue(auth.check_pw(fields["password"], password_hash))
        self.assertFalse(auth.check_pw("weak-old-password", password_hash))
        self.assertIsNone(secret)
        self.assertIsNone(legacy)
        self.assertNotIn(fields["password"], repr(result))

    def test_missing_root_or_existing_output_fails_without_changing_database(self):
        self.output.write_text("preserve")
        with self.assertRaises(FileExistsError):
            rotate(str(self.db_path), str(self.output))
        con = sqlite3.connect(self.db_path)
        password_hash = con.execute(
            "SELECT password_hash FROM users WHERE username='boss'"
        ).fetchone()[0]
        con.close()
        self.assertTrue(auth.check_pw("weak-old-password", password_hash))
        self.assertEqual("preserve", self.output.read_text())


if __name__ == "__main__":
    unittest.main()
