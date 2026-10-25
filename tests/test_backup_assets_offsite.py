"""素材文件硬链接增量快照、可选异地同步与备份巡检告警。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from deploy import backup_db, backup_health
from deploy.backup_db import (
    BackupError,
    list_asset_snapshots,
    offsite_sync,
    parse_asset_sources,
    parse_remote,
    snapshot_assets,
)


UTC = timezone.utc
T0 = datetime(2026, 7, 25, 2, 0, 0, tzinfo=UTC)
SHA = "a" * 64


class AssetSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.assets = self.root / "data" / "assets"
        self.pub = self.root / "pub"
        (self.assets / "job1").mkdir(parents=True)
        self.pub.mkdir()
        (self.assets / "job1" / "cover.png").write_bytes(b"png-v1")
        (self.assets / "job1" / "deck.pdf").write_bytes(b"pdf-v1")
        (self.pub / "voice.mp3").write_bytes(b"mp3")
        self.snapshots = self.root / "backups" / "assets"
        self.sources = {"assets": self.assets, "pub": self.pub}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_second_snapshot_hardlinks_unchanged_files_and_copies_changes(self) -> None:
        first = snapshot_assets(self.sources, self.snapshots, now=T0)
        self.assertFalse(first["skipped"])
        self.assertEqual((3, 3, 0), (first["files"], first["copied"], first["linked"]))
        snap1 = Path(first["snapshot"])
        self.assertEqual("assets-2026-07-25T020000Z", snap1.name)
        self.assertEqual(b"png-v1", (snap1 / "assets" / "job1" / "cover.png").read_bytes())
        self.assertEqual(b"mp3", (snap1 / "pub" / "voice.mp3").read_bytes())
        self.assertEqual(0o700, snap1.stat().st_mode & 0o777)
        self.assertEqual(0o600, (snap1 / "pub" / "voice.mp3").stat().st_mode & 0o777)
        manifest = json.loads((snap1 / ".snapshot.json").read_text())
        self.assertEqual(3, manifest["files"])

        # 1 小时后:还没到间隔,跳过
        skipped = snapshot_assets(self.sources, self.snapshots, now=T0 + timedelta(hours=1))
        self.assertTrue(skipped["skipped"])
        self.assertEqual(1, len(list_asset_snapshots(self.snapshots)))

        cover = self.assets / "job1" / "cover.png"
        cover.write_bytes(b"png-v2-longer")
        os.utime(cover, (1_900_000_000, 1_900_000_000))
        (self.assets / "job1" / "new.txt").write_bytes(b"new")
        (self.assets / "job1" / "deck.pdf").unlink()
        second = snapshot_assets(self.sources, self.snapshots, now=T0 + timedelta(days=1))
        snap2 = Path(second["snapshot"])
        self.assertEqual((3, 1, 2), (second["files"], second["linked"], second["copied"]))
        # 没变的文件与上一快照共用 inode(不占新空间),变了的是独立副本
        self.assertEqual(
            (snap1 / "pub" / "voice.mp3").stat().st_ino,
            (snap2 / "pub" / "voice.mp3").stat().st_ino,
        )
        self.assertNotEqual(
            (snap1 / "assets" / "job1" / "cover.png").stat().st_ino,
            (snap2 / "assets" / "job1" / "cover.png").stat().st_ino,
        )
        self.assertEqual(b"png-v1", (snap1 / "assets" / "job1" / "cover.png").read_bytes())
        self.assertEqual(b"png-v2-longer", (snap2 / "assets" / "job1" / "cover.png").read_bytes())
        # 源里删掉的文件在旧快照里仍可恢复
        self.assertTrue((snap1 / "assets" / "job1" / "deck.pdf").exists())
        self.assertFalse((snap2 / "assets" / "job1" / "deck.pdf").exists())

    def test_symlinks_and_special_files_are_never_followed(self) -> None:
        secret = self.root / "outside-secret"
        secret.write_bytes(b"do-not-back-up")
        (self.assets / "evil-link").symlink_to(secret)
        (self.assets / "evil-dir").symlink_to(self.root)
        os.mkfifo(self.assets / "pipe")
        report = snapshot_assets(self.sources, self.snapshots, now=T0)
        snap = Path(report["snapshot"])
        self.assertEqual(2, report["skipped_links"])
        self.assertEqual(1, report["skipped_special"])
        self.assertFalse((snap / "assets" / "evil-link").exists())
        self.assertFalse((snap / "assets" / "evil-dir").exists())
        self.assertFalse((snap / "assets" / "pipe").exists())
        for path in snap.rglob("*"):
            if path.is_file():
                self.assertNotEqual(b"do-not-back-up", path.read_bytes())

    def test_symlinked_source_root_is_refused(self) -> None:
        link = self.root / "assets-link"
        link.symlink_to(self.assets)
        with self.assertRaisesRegex(BackupError, "cannot safely open asset source"):
            snapshot_assets({"assets": link}, self.snapshots, now=T0)
        self.assertEqual([], list_asset_snapshots(self.snapshots))
        self.assertEqual([], [p for p in self.snapshots.iterdir()
                              if p.name.startswith(".assets.partial-")])

    def test_missing_source_is_reported_not_fatal(self) -> None:
        report = snapshot_assets(
            {"assets": self.assets, "pub": self.root / "missing"},
            self.snapshots, now=T0,
        )
        self.assertEqual(["pub"], report["missing_sources"])
        self.assertEqual(2, report["files"])

    def test_retention_keeps_recent_days_and_minimum_count(self) -> None:
        stamps = [T0 + timedelta(days=offset) for offset in range(0, 20, 2)]
        for stamp in stamps:
            snapshot_assets(self.sources, self.snapshots, now=stamp,
                            keep_days=7, keep_minimum=2)
        # 另放一个不受管的目录,保留策略绝不能碰
        (self.snapshots / "operator-notes").mkdir()
        (self.snapshots / ".assets.partial-crashed").mkdir()
        final = snapshot_assets(self.sources, self.snapshots,
                                now=T0 + timedelta(days=20), keep_days=7, keep_minimum=2)
        names = [path.name for _, path in list_asset_snapshots(self.snapshots)]
        self.assertEqual(
            ["assets-2026-08-14T020000Z", "assets-2026-08-12T020000Z",
             "assets-2026-08-10T020000Z", "assets-2026-08-08T020000Z"],
            names,
        )
        self.assertTrue((self.snapshots / "operator-notes").is_dir())
        self.assertFalse((self.snapshots / ".assets.partial-crashed").exists())
        self.assertTrue(final["pruned_snapshots"])
        # 被删快照里的硬链接不影响存活快照的内容
        self.assertEqual(
            b"mp3",
            (self.snapshots / names[-1] / "pub" / "voice.mp3").read_bytes(),
        )

    def test_asset_source_parsing(self) -> None:
        self.assertEqual(
            {"assets": Path("/var/lib/paihuo/data/assets"), "pub": Path("/srv/paihuo-pub")},
            parse_asset_sources(["assets=/var/lib/paihuo/data/assets,pub=/srv/paihuo-pub"]),
        )
        for bad in ("assets", "Assets=/x", "a=relative", "a=/x,a=/y", "../x=/y"):
            with self.assertRaises(BackupError, msg=bad):
                parse_asset_sources([bad])


class OffsiteSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir(mode=0o700)
        self.db_backup = self.backup_dir / "db-2026-07-25T020000Z.db"
        self.db_backup.write_bytes(b"sqlite")
        self.snapshot = self.backup_dir / "assets" / "assets-2026-07-25T020000Z"
        self.snapshot.mkdir(parents=True)
        self.commands: list[list[str]] = []
        self.sidecars: list[str] = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _runner(self, command, timeout):
        self.assertGreater(timeout, 0)
        self.commands.append(list(command))
        for arg in command:
            if arg.endswith(".sha256") and os.path.isfile(arg):
                self.sidecars.append(Path(arg).read_text())

    def _status(self) -> dict:
        path = self.backup_dir / ".offsite-status.json"
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        return json.loads(path.read_text())

    def test_remote_spec_parsing(self) -> None:
        self.assertIsNone(parse_remote(""))
        self.assertEqual(("rclone", "paihuo-oss:bucket/prod"),
                         parse_remote("rclone:paihuo-oss:bucket/prod/"))
        self.assertEqual(("rsync", "backup@10.0.0.8:/srv/off"),
                         parse_remote("rsync:backup@10.0.0.8:/srv/off"))
        for bad in ("oss:bucket", "rclone:no-colon", "rclone:-x:y",
                    "rsync:host:/a b", "rsync:", "s3:x"):
            with self.assertRaises(BackupError, msg=bad):
                parse_remote(bad)

    def test_rclone_uploads_db_sidecar_and_new_assets_once(self) -> None:
        status = offsite_sync(
            "rclone:oss:bucket/prod", backup_dir=self.backup_dir,
            db_backup_path=self.db_backup, db_sha256=SHA,
            asset_snapshot=self.snapshot, runner=self._runner, now=T0,
        )
        self.assertEqual("succeeded", status["status"])
        self.assertEqual([
            ["rclone", "copyto", str(self.db_backup),
             "oss:bucket/prod/db/db-2026-07-25T020000Z.db"],
            ["rclone", "copyto", self.commands[1][2],
             "oss:bucket/prod/db/db-2026-07-25T020000Z.db.sha256"],
            ["rclone", "copy", f"{self.snapshot}/", "oss:bucket/prod/assets/current"],
        ], self.commands)
        self.assertEqual([f"{SHA}  db-2026-07-25T020000Z.db\n"], self.sidecars)
        self.assertEqual("assets-2026-07-25T020000Z", self._status()["assets_snapshot"])
        # 下一小时:素材快照没变,只传数据库
        self.commands.clear()
        offsite_sync(
            "rclone:oss:bucket/prod", backup_dir=self.backup_dir,
            db_backup_path=self.db_backup, db_sha256=SHA,
            asset_snapshot=self.snapshot, runner=self._runner,
            now=T0 + timedelta(hours=1),
        )
        self.assertEqual(2, len(self.commands))
        self.assertFalse(any(cmd[1] == "copy" for cmd in self.commands))

    def test_rsync_uses_fixed_ssh_options_without_shell(self) -> None:
        with patch.dict(os.environ, {
            "PAIHUO_BACKUP_SSH_KEY": "/etc/paihuo/backup_ed25519",
            "PAIHUO_BACKUP_SSH_KNOWN_HOSTS": "/etc/paihuo/backup_known_hosts",
        }):
            offsite_sync(
                "rsync:backup@10.0.0.8:/srv/off", backup_dir=self.backup_dir,
                db_backup_path=self.db_backup, db_sha256=SHA,
                asset_snapshot=None, runner=self._runner, now=T0,
            )
        self.assertEqual(1, len(self.commands))
        command = self.commands[0]
        self.assertEqual(["rsync", "-t", "-e"], command[:3])
        self.assertIn("StrictHostKeyChecking=yes", command[3])
        self.assertIn("-i /etc/paihuo/backup_ed25519", command[3])
        self.assertEqual("backup@10.0.0.8:/srv/off/db/", command[-1])

    def test_failure_is_recorded_and_raised(self) -> None:
        def failing(command, timeout):
            raise BackupError("offsite command failed: rclone copyto exit=1: denied")

        with self.assertRaisesRegex(BackupError, "exit=1"):
            offsite_sync(
                "rclone:oss:bucket/prod", backup_dir=self.backup_dir,
                db_backup_path=self.db_backup, db_sha256=SHA,
                runner=failing, now=T0,
            )
        status = self._status()
        self.assertEqual("failed", status["status"])
        self.assertIsNone(status["last_success_at_utc"])
        with self.assertRaisesRegex(BackupError, "SHA-256"):
            offsite_sync("rclone:oss:b/p", backup_dir=self.backup_dir,
                         db_backup_path=self.db_backup, db_sha256="bad",
                         runner=self._runner, now=T0)


class BackupCliExtraStepsTests(unittest.TestCase):
    """数据库备份成功后:素材快照 + 异地同步;失败走告警且不让 unit 失败。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "contentcrew.db"
        with contextlib.closing(sqlite3.connect(self.source)) as connection:
            connection.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY)")
            connection.commit()
        self.backup_dir = self.root / "backups"
        self.assets = self.root / "assets"
        self.assets.mkdir()
        (self.assets / "a.png").write_bytes(b"a")
        self.alerts: list[str] = []
        self.commands: list[list[str]] = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _main(self, *extra: str, runner=None) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()

        def record(command, timeout):
            self.commands.append(list(command))

        with patch.object(backup_db, "_run_command", runner or record), \
                patch.object(backup_db, "raise_alert", self.alerts.append), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = backup_db.main([
                "--database", str(self.source),
                "--backup-dir", str(self.backup_dir),
                "--asset-source", f"assets={self.assets}",
                *extra,
            ])
        report = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
        return code, report, stderr.getvalue()

    def test_without_remote_snapshots_assets_and_warns(self) -> None:
        with patch.dict(os.environ, {"PAIHUO_BACKUP_REMOTE": ""}):
            code, report, stderr = self._main()
        self.assertEqual(0, code, stderr)
        self.assertEqual(1, report["assets"]["files"])
        self.assertEqual({"configured": False}, report["offsite"])
        self.assertIn("offsite backup is not configured", stderr)
        self.assertEqual([], self.alerts)
        self.assertEqual([], self.commands)

    def test_remote_success_uploads_after_db_backup(self) -> None:
        code, report, stderr = self._main("--remote", "rclone:oss:bucket/prod")
        self.assertEqual(0, code, stderr)
        self.assertEqual("succeeded", report["offsite"]["status"])
        self.assertEqual(report["backup_path"], self.commands[0][2])
        self.assertEqual("copy", self.commands[-1][1])
        self.assertEqual([], self.alerts)

    def test_remote_failure_alerts_and_exits_75_but_keeps_local_backup(self) -> None:
        def failing(command, timeout):
            raise BackupError("offsite command failed: rclone copyto exit=5: boom")

        code, report, stderr = self._main(
            "--remote", "rclone:oss:bucket/prod", runner=failing)
        self.assertEqual(backup_db.OFFSITE_EXIT_CODE, code)
        self.assertEqual(["paihuo-backup-offsite"], self.alerts)
        self.assertIn("offsite sync failed", stderr)
        self.assertTrue(Path(report["backup_path"]).is_file())
        self.assertFalse(report["offsite"]["ok"])

    def test_unit_treats_75_as_success_and_declares_sources(self) -> None:
        unit = (Path(__file__).resolve().parents[1] / "deploy"
                / "simple" / "paihuo-backup-simple.service").read_text()
        self.assertIn(f"SuccessExitStatus={backup_db.OFFSITE_EXIT_CODE}", unit)
        self.assertIn("--asset-source assets=/var/lib/paihuo/data/assets", unit)
        self.assertIn("--asset-source pub=/srv/paihuo-pub", unit)
        self.assertIn("EnvironmentFile=-/etc/paihuo/backup.env", unit)

    def test_raise_alert_uses_existing_failure_alert_template(self) -> None:
        calls = []
        self.assertTrue(backup_db.raise_alert("paihuo-backup-offsite", runner=calls.append))
        self.assertEqual(
            [["systemctl", "start", "--no-block",
              "paihuo-failure-alert@paihuo-backup-offsite.service"]],
            calls,
        )


class BackupHealthWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.backup_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_missing_remote_gives_explicit_warning(self) -> None:
        warnings = backup_health.backup_warnings(
            backup_dir=self.backup_dir, remote="", now=T0)
        self.assertIn(backup_health.OFFSITE_NOT_CONFIGURED, warnings)
        self.assertTrue(any("素材文件快照" in item for item in warnings))

    def test_configured_remote_checks_last_success_and_asset_age(self) -> None:
        (self.backup_dir / "assets" / "assets-2026-07-24T020000Z").mkdir(parents=True)
        warnings = backup_health.backup_warnings(
            backup_dir=self.backup_dir, remote="rclone:oss:b/p", now=T0)
        self.assertEqual(["已配置异地备份,但还没有成功同步过"], warnings)
        (self.backup_dir / ".offsite-status.json").write_text(json.dumps({
            "status": "failed", "last_success_at_utc": "2026-07-20T00:00:00Z",
        }))
        warnings = backup_health.backup_warnings(
            backup_dir=self.backup_dir, remote="rclone:oss:b/p",
            now=T0 + timedelta(days=3))
        self.assertTrue(any("没有成功同步" in item for item in warnings))
        self.assertTrue(any("最近一次异地同步失败" in item for item in warnings))
        self.assertTrue(any("最新素材快照已超过" in item for item in warnings))
        (self.backup_dir / ".offsite-status.json").write_text(json.dumps({
            "status": "succeeded", "last_success_at_utc": "2026-07-25T01:00:00Z",
        }))
        self.assertEqual([], backup_health.backup_warnings(
            backup_dir=self.backup_dir, remote="rclone:oss:b/p", now=T0))
        self.assertIn(
            "PAIHUO_BACKUP_REMOTE 格式不对,异地备份没有生效",
            backup_health.backup_warnings(
                backup_dir=self.backup_dir, remote="ftp:x", now=T0),
        )


if __name__ == "__main__":
    unittest.main()
