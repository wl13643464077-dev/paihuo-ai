"""异地备份:加密推送、验回、保留、回执与新鲜度监控(审核 P0-7)。

设计约束逐条钉住:
- 未配置目的地/密钥即拒绝(配置 = 老板的授权动作,绝不擅自外传);
- 远端只见密文,密钥不进推送内容与子进程 argv;
- 推送后必须验回,远端内容不符即失败;
- 回执带 HMAC,伪造/过期都会让新鲜度检查 fail closed;
- 密文可用同一密钥精确还原(恢复演练的最小闭环)。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from deploy.offsite_backup import (
    DEST_ENV,
    KEY_ENV,
    OffsiteError,
    check_offsite_freshness,
    decrypt_backup,
    encrypt_backup,
    latest_verified_backup,
    parse_destination,
    push_offsite,
    read_receipt,
)

UTC = timezone.utc


def _make_backup(directory: Path, name: str = "db-2026-07-26T120000Z.db") -> Path:
    path = directory / name
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE tenants(id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO tenants(name) VALUES('甲')")
        connection.commit()
    finally:
        connection.close()
    return path


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        self.dest_dir = self.root / "offsite"
        self.receipt = self.root / "receipt.json"
        self.key = secrets.token_urlsafe(48)
        self.now = datetime(2026, 7, 26, 12, 30, tzinfo=UTC)


class AuthorizationGateTests(_Case):
    """配置即授权:缺目的地或缺密钥都必须拒绝运行。"""

    def test_missing_destination_is_refused(self):
        _make_backup(self.backup_dir)
        with patch.dict(os.environ, {DEST_ENV: "", KEY_ENV: self.key}, clear=False):
            with self.assertRaises(OffsiteError) as caught:
                push_offsite(self.backup_dir, receipt_path=self.receipt, now=self.now)
        self.assertIn(DEST_ENV, str(caught.exception))

    def test_missing_key_is_refused(self):
        _make_backup(self.backup_dir)
        with self.assertRaises(OffsiteError) as caught:
            push_offsite(
                self.backup_dir,
                destination=str(self.dest_dir),
                key_material="",
                receipt_path=self.receipt,
                now=self.now,
            )
        self.assertIn(KEY_ENV, str(caught.exception))

    def test_weak_key_is_refused(self):
        _make_backup(self.backup_dir)
        with self.assertRaises(ValueError):
            push_offsite(
                self.backup_dir,
                destination=str(self.dest_dir),
                key_material="short",
                receipt_path=self.receipt,
                now=self.now,
            )

    def test_relative_local_destination_is_refused(self):
        with self.assertRaises(OffsiteError):
            parse_destination("relative/path")

    def test_malformed_ssh_destination_is_refused(self):
        with self.assertRaises(OffsiteError):
            parse_destination("ssh://user@host/no-colon")


class EncryptionTests(_Case):
    def test_remote_payload_is_ciphertext_only_and_round_trips(self):
        source = _make_backup(self.backup_dir)
        plaintext = source.read_bytes()
        staged = encrypt_backup(source, self.root, self.key, now=self.now)

        cipher = staged["payload_path"].read_bytes()
        self.assertNotIn(b"SQLite format 3", cipher, "密文中泄露了明文库头")
        self.assertNotEqual(plaintext, cipher)

        restored = decrypt_backup(staged["payload_path"], self.key)
        self.assertEqual(plaintext, restored, "解密结果与原库不一致")

        manifest = staged["manifest"]
        self.assertEqual(
            hashlib.sha256(plaintext).hexdigest(), manifest["plaintext_sha256"]
        )
        self.assertEqual(
            hashlib.sha256(cipher).hexdigest(), manifest["ciphertext_sha256"]
        )

    def test_wrong_key_cannot_decrypt(self):
        source = _make_backup(self.backup_dir)
        staged = encrypt_backup(source, self.root, self.key, now=self.now)
        with self.assertRaises(OffsiteError):
            decrypt_backup(staged["payload_path"], secrets.token_urlsafe(48))

    def test_oversized_database_is_refused(self):
        source = _make_backup(self.backup_dir)
        with patch("deploy.offsite_backup.MAX_PLAINTEXT_BYTES", 16):
            with self.assertRaises(OffsiteError):
                encrypt_backup(source, self.root, self.key, now=self.now)


class LocalPushTests(_Case):
    def _push(self, **kwargs):
        return push_offsite(
            self.backup_dir,
            destination=str(self.dest_dir),
            key_material=self.key,
            receipt_path=self.receipt,
            now=kwargs.pop("now", self.now),
            **kwargs,
        )

    def test_push_verify_and_receipt(self):
        source = _make_backup(self.backup_dir)
        report = self._push()

        self.assertTrue(report["ok"])
        self.assertEqual(source.name, report["source_backup"])
        pushed = self.dest_dir / f"{source.name}.enc"
        self.assertTrue(pushed.is_file())
        # 目的地只有密文与清单,没有任何明文库。
        names = {entry.name for entry in self.dest_dir.iterdir()}
        self.assertEqual(
            {f"{source.name}.enc", f"{source.name}.manifest.json"}, names
        )
        # 回执可验证且内容与推送一致。
        body = read_receipt(self.receipt, self.key)
        self.assertEqual(report["ciphertext_sha256"], body["ciphertext_sha256"])

    def test_latest_backup_is_selected(self):
        _make_backup(self.backup_dir, "db-2026-07-25T120000Z.db")
        newest = _make_backup(self.backup_dir, "db-2026-07-26T090000Z.db")
        self.assertEqual(newest, latest_verified_backup(self.backup_dir))

    def test_no_backups_fails_closed(self):
        with self.assertRaises(OffsiteError):
            latest_verified_backup(self.backup_dir)

    def test_remote_retention_keeps_newest_pairs(self):
        for day in (23, 24, 25, 26):
            _make_backup(self.backup_dir, f"db-2026-07-{day}T120000Z.db")
            self._push(keep=2)
        payloads = sorted(
            entry.name
            for entry in self.dest_dir.iterdir()
            if entry.name.endswith(".enc")
        )
        self.assertEqual(
            ["db-2026-07-25T120000Z.db.enc", "db-2026-07-26T120000Z.db.enc"],
            payloads,
        )
        manifests = {
            entry.name
            for entry in self.dest_dir.iterdir()
            if entry.name.endswith(".manifest.json")
        }
        self.assertEqual(2, len(manifests), "清单应与密文成对保留")

    def test_tampered_remote_copy_fails_verification(self):
        _make_backup(self.backup_dir)

        real_verify = None
        from deploy import offsite_backup as module

        original = module._push_local

        def sabotage(dest_path, files):
            original(dest_path, files)
            for entry in dest_path.iterdir():
                if entry.name.endswith(".enc"):
                    entry.write_bytes(entry.read_bytes() + b"x")

        with patch.object(module, "_push_local", sabotage):
            with self.assertRaises(OffsiteError) as caught:
                self._push()
        self.assertIn("verification", str(caught.exception))

    def test_group_accessible_destination_is_refused(self):
        _make_backup(self.backup_dir)
        self.dest_dir.mkdir(mode=0o755)
        with self.assertRaises(OffsiteError):
            self._push()


class SshCommandTests(_Case):
    """ssh 形态只验证命令构造与验回逻辑;不真连网络。"""

    def test_key_never_appears_in_subprocess_argv(self):
        source = _make_backup(self.backup_dir)
        commands: list[list[str]] = []

        cipher_sha = {}

        def fake_run(command, *, timeout):
            commands.append(command)

            class _Proc:
                returncode = 0
                stdout = ""

            proc = _Proc()
            if command[0] == "ssh" and "sha256sum" in command[-1]:
                name = command[-1].split("/")[-1]
                proc.stdout = f"{cipher_sha.get(name, '0' * 64)}  x\n"
            return proc

        from deploy import offsite_backup as module

        original_encrypt = module.encrypt_backup

        def capture_encrypt(*args, **kwargs):
            staged = original_encrypt(*args, **kwargs)
            cipher_sha[staged["payload_path"].name] = staged["manifest"][
                "ciphertext_sha256"
            ]
            cipher_sha[staged["manifest_path"].name] = hashlib.sha256(
                staged["manifest_path"].read_bytes()
            ).hexdigest()
            return staged

        with (
            patch.object(module, "_run", fake_run),
            patch.object(module, "encrypt_backup", capture_encrypt),
        ):
            report = push_offsite(
                self.backup_dir,
                destination="ssh://backup@offsite.example:/srv/paihuo",
                key_material=self.key,
                receipt_path=self.receipt,
                now=self.now,
            )

        self.assertTrue(report["ok"])
        joined = " ".join(" ".join(command) for command in commands)
        self.assertNotIn(self.key, joined, "密钥泄漏进了子进程参数")
        self.assertIn("rsync", commands[0][0])
        self.assertTrue(
            any("sha256sum" in command[-1] for command in commands if command),
            "推送后未做远端哈希验回",
        )

    def test_remote_hash_mismatch_fails(self):
        _make_backup(self.backup_dir)

        def fake_run(command, *, timeout):
            class _Proc:
                returncode = 0
                stdout = f"{'a' * 64}  x\n"

            return _Proc()

        from deploy import offsite_backup as module

        with patch.object(module, "_run", fake_run):
            with self.assertRaises(OffsiteError):
                push_offsite(
                    self.backup_dir,
                    destination="ssh://backup@offsite.example:/srv/paihuo",
                    key_material=self.key,
                    receipt_path=self.receipt,
                    now=self.now,
                )


class FreshnessTests(_Case):
    def _pushed(self):
        _make_backup(self.backup_dir)
        return push_offsite(
            self.backup_dir,
            destination=str(self.dest_dir),
            key_material=self.key,
            receipt_path=self.receipt,
            now=self.now,
        )

    def test_fresh_receipt_passes(self):
        self._pushed()
        report = check_offsite_freshness(
            self.receipt,
            key_material=self.key,
            now=self.now + timedelta(hours=5),
        )
        self.assertTrue(report["ok"])
        self.assertAlmostEqual(5.0, report["age_hours"], places=2)

    def test_stale_receipt_fails_closed(self):
        self._pushed()
        with self.assertRaises(OffsiteError) as caught:
            check_offsite_freshness(
                self.receipt,
                key_material=self.key,
                now=self.now + timedelta(hours=40),
            )
        self.assertIn("stale", str(caught.exception))

    def test_missing_receipt_fails_closed(self):
        with self.assertRaises(OffsiteError):
            check_offsite_freshness(self.receipt, key_material=self.key)

    def test_forged_receipt_fails_closed(self):
        self._pushed()
        body = json.loads(self.receipt.read_text(encoding="utf-8"))
        body["pushed_at_utc"] = (
            (self.now + timedelta(hours=100)).isoformat().replace("+00:00", "Z")
        )
        self.receipt.write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(OffsiteError) as caught:
            check_offsite_freshness(
                self.receipt,
                key_material=self.key,
                now=self.now + timedelta(hours=1),
            )
        self.assertIn("authentication", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
