import json
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.backup_db import BackupError, backup_database
from deploy import backup_health, session_secret_env, upgrade_release
from deploy.control_plane import ATTESTATION_FORMAT


UTC = timezone.utc


class UpgradeReleaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.state = self.base / "state"
        self.state.mkdir()
        self.backups = self.state / "backups"
        self.backups.mkdir(mode=0o700)
        self.control = self.base / "root-upgrade-control"
        self.previous = self._release("release-old", "old")
        self.release = self._release("release-new", "new")
        self.current = self.base / "current"
        self.current.symlink_to(self.previous, target_is_directory=True)
        self.credentials = self.base / "smoke.credentials"
        self.credentials.write_text(
            "username=boss\npassword=one-time-secret\n", encoding="utf-8"
        )
        self.credentials.chmod(0o600)
        self.smoke_client = Path(__file__).resolve().parents[1] / "deploy" / "smoke_readonly.py"
        self.start_guard = Path(__file__).resolve().parents[1] / "deploy" / "start_guard.py"
        self.start_permit = self.base / "run" / "start.permit"
        connection = sqlite3.connect(self.state / "contentcrew.db")
        connection.execute(
            "CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE tenants(id INTEGER PRIMARY KEY, name TEXT, enabled INTEGER)"
        )
        connection.execute(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id INTEGER, "
            "username TEXT, password_hash TEXT, role TEXT, modules_json TEXT, "
            "enabled INTEGER, created_at REAL, updated_at REAL)"
        )
        connection.execute(
            "CREATE TABLE job(id INTEGER PRIMARY KEY, brief_json TEXT, mode TEXT, "
            "status TEXT, current_idx INTEGER, created_at REAL, updated_at REAL)"
        )
        connection.execute("INSERT INTO items(value) VALUES('before-upgrade')")
        connection.execute(
            "INSERT INTO tenants(id,name,enabled) VALUES(1,'fixture',1)"
        )
        connection.commit()
        connection.close()
        self.periodic = backup_database(
            self.state / "contentcrew.db",
            self.backups,
            run_restore_drill=True,
        )
        self.control.mkdir(mode=0o700)
        self.env_dir = self.base / "etc-paihuo"
        self.env_dir.mkdir(mode=0o700)
        self.env_file = self.env_dir / "paihuo.env"
        self.session_value = (
            "A1b2C3d4E5f6G7h8I9j0K_l-MnOpQrStUvWxYz0123456789abcdef"
        )
        self.config_value = (
            "z9Y8x7W6v5U4t3S2r1Q0P-onMLKJIHGFEDCBA9876543210fedcba"
        )
        self.env_file.write_text(
            "CONTENTCREW_SESSION_SECRET=" + self.session_value + "\n"
            "CONTENTCREW_CONFIG_KEY=" + self.config_value + "\n",
            encoding="utf-8",
        )
        self.env_file.chmod(0o600)
        self.secret_backup = self.control / "credentials" / "secrets.backup"
        session_secret_env.backup_secret_keys(
            self.env_file,
            self.secret_backup,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )
        self.control_attestation_dir = (
            self.control / "control-plane" / self.release.name
        )
        self.control_attestation_dir.mkdir(parents=True, mode=0o700)
        self.control_attestation = (
            self.control_attestation_dir / "attestation.json"
        )
        self.control_attestation.write_text(
            json.dumps(
                {
                    "candidate_digest": "fixture-control-plane",
                    "format": ATTESTATION_FORMAT,
                    "release": str(self.release),
                    "release_id": self.release.name,
                    "status": "installed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.control_attestation.chmod(0o600)
        self.evidence_dir = self.control / "incoming" / self.release.name
        self.evidence_dir.mkdir(parents=True, mode=0o700)
        self.release_archive = (
            self.evidence_dir / f"{self.release.name}.tar.gz"
        )
        self.release_archive.write_bytes(b"fixture archive\n")
        self.release_build_receipt = (
            self.evidence_dir / f"{self.release.name}.receipt.json"
        )
        self.release_build_receipt.write_text(
            '{"fixture":true}\n', encoding="utf-8"
        )
        self.release_artifact_attestation = (
            self.evidence_dir / "release-artifact-attestation.json"
        )
        self.release_artifact_attestation.write_text(
            '{"fixture":"artifact"}\n', encoding="utf-8"
        )
        self.release_artifact_attestation.chmod(0o600)
        self.materialized_release_attestation = (
            self.evidence_dir / "materialized-release-attestation.json"
        )
        self.materialized_release_attestation.write_text(
            '{"fixture":"materialized"}\n', encoding="utf-8"
        )
        self.materialized_release_attestation.chmod(0o600)
        self.bootstrap_stage_dir = (
            self.control / "bootstrap" / "releases" / self.release.name
        )
        self.bootstrap_stage_dir.mkdir(parents=True, mode=0o700)
        self.bootstrap_stage_attestation = (
            self.bootstrap_stage_dir / "attestation.json"
        )
        self.bootstrap_venv_tree_sha256 = "6" * 64
        self.bootstrap_wheelhouse_path = str(
            Path("/var/cache/paihuo-wheelhouse")
            / self.release.name
            / "wheelhouse-attestation.json"
        )
        self.bootstrap_verification = {
            "launcher_sha256": "7" * 64,
            "ops_digest": "8" * 64,
            "release_id": self.release.name,
            "venv_tree_sha256": self.bootstrap_venv_tree_sha256,
            "wheelhouse_attestation_path": self.bootstrap_wheelhouse_path,
            "wheelhouse_attestation_sha256": "9" * 64,
            "wheelhouse_hashed_lock_sha256": "a" * 64,
            "wheelhouse_source_lock_sha256": "b" * 64,
            "wheelhouse_total_bytes": 141_655_420,
            "wheelhouse_wheel_count": 52,
            "wheelhouse_wheel_set_sha256": "c" * 64,
        }
        self.bootstrap_stage_attestation.write_text(
            json.dumps(
                {
                    "format": "paihuo-release-bootstrap/v1",
                    "ok": True,
                    "release_dir": str(self.release),
                    "state_dir": str(self.state),
                    **self.bootstrap_verification,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.bootstrap_stage_attestation.chmod(0o600)
        self.artifact_identity = {
            "manifest_sha256": "1" * 64,
            "payload_tree_sha256": "2" * 64,
            "release_id": self.release.name,
            "schema_version": 47,
            "source_tree_sha256": "3" * 64,
        }
        self.attestation = self.control / "latest-periodic-backup.json"
        backup_health.record_backup_success(
            backup_report=self.periodic,
            backup_dir=self.backups,
            attestation_path=self.attestation,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _release(self, name: str, marker: str) -> Path:
        release = self.base / name
        (release / "app").mkdir(parents=True)
        (release / "deploy").mkdir()
        (release / "tests").mkdir()
        (release / "venv" / "bin").mkdir(parents=True)
        (release / "app" / "main.py").write_text(
            f"MARKER={marker!r}\n", encoding="utf-8"
        )
        (release / "deploy" / "preflight.py").write_text(
            "# fixture\n", encoding="utf-8"
        )
        (release / "tests" / "smoke.py").write_text(
            "# fixture\n", encoding="utf-8"
        )
        (release / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (release / "run.sh").chmod(0o755)
        (release / "requirements.lock.txt").write_text(
            "fastapi==0.139.0\n", encoding="utf-8"
        )
        (release / "RELEASE-MANIFEST.json").write_text(
            json.dumps(
                {
                    "release_id": name,
                    "schema_version": 47,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        python = release / "venv" / "bin" / "python"
        python.write_text("#!/bin/sh\n", encoding="utf-8")
        python.chmod(0o755)
        ytdlp = release / "venv" / "bin" / "yt-dlp"
        ytdlp.write_text(f"#!{python}\n", encoding="utf-8")
        ytdlp.chmod(0o755)
        (release / "data").symlink_to(self.state, target_is_directory=True)
        return release

    def _rows(self):
        connection = sqlite3.connect(self.state / "contentcrew.db")
        try:
            return connection.execute(
                "SELECT value FROM items ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

    def _upgrade(self, **overrides):
        arguments = {
            "release": self.release,
            "current": self.current,
            "state": self.state,
            "backup_dir": self.backups,
            "control_dir": self.control,
            "smoke_credentials": self.credentials,
            "smoke_client": self.smoke_client,
            "start_guard": self.start_guard,
            "start_permit": self.start_permit,
            "managed_env_file": self.env_file,
            "secret_backup": self.secret_backup,
            "control_plane_attestation": self.control_attestation,
            "release_archive": self.release_archive,
            "release_build_receipt": self.release_build_receipt,
            "release_artifact_attestation": (
                self.release_artifact_attestation
            ),
            "materialized_release_attestation": (
                self.materialized_release_attestation
            ),
            "bootstrap_stage_attestation": (
                self.bootstrap_stage_attestation
            ),
            "command_runner": lambda _command, **_kwargs: None,
            "health_checker": lambda _url: True,
            "post_commit_verifier": lambda **_kwargs: {
                "ok": True,
                "backup_schema_version": 47,
                "https_status": 200,
            },
            "control_plane_verifier": lambda path: {
                "attestation_sha256": (
                    upgrade_release.control_plane_attestation_digest(path)
                ),
                "status": "installed",
            },
            "artifact_verifier": lambda **_kwargs: {
                **self.artifact_identity,
                "archive_sha256": "4" * 64,
                "archive_size": self.release_archive.stat().st_size,
                "extract_dir": str(self.release),
                "format": "paihuo-release-attestation/v1",
                "member_count": 10,
                "ok": True,
                "receipt_sha256": "5" * 64,
            },
            "materialized_verifier": lambda **_kwargs: {
                **self.artifact_identity,
                "artifact_archive_sha256": "4" * 64,
                "artifact_attestation_sha256": (
                    upgrade_release._file_sha256(
                        self.release_artifact_attestation
                    )
                ),
                "artifact_receipt_sha256": "5" * 64,
                "data_target": str(self.state),
                "format": (
                    "paihuo-materialized-release-attestation/v1"
                ),
                "materialized_release": str(self.release),
                "ok": True,
            },
            "bootstrap_verifier": lambda release_id: {
                **self.bootstrap_verification,
                "release_id": release_id,
            },
            "inactive_checker": lambda _service: True,
            "health_timeout": 0,
        }
        arguments.update(overrides)
        return upgrade_release.upgrade_release(**arguments)

    def _receipt(self) -> dict:
        receipts = list(self.control.glob("upgrade-*.json"))
        self.assertEqual(1, len(receipts))
        return json.loads(receipts[0].read_text(encoding="utf-8"))

    def _restore_with_distinct_but_equivalent_bytes(
        self,
        original_restore,
    ):
        def restore(source, *, restore_to=None, **kwargs):
            report = original_restore(
                source,
                restore_to=restore_to,
                **kwargs,
            )
            if (
                restore_to is None
                or not Path(restore_to).name.startswith(".rollback-ready-")
            ):
                return report

            source_report = upgrade_release.verify_database(source)
            restored_path = Path(restore_to)
            with closing(sqlite3.connect(restored_path)) as connection:
                connection.execute(
                    "CREATE TABLE __rollback_layout_probe(value TEXT)"
                )
                connection.execute("DROP TABLE __rollback_layout_probe")
                connection.commit()
            restored_report = upgrade_release.verify_database(restored_path)
            self.assertEqual(
                source_report["schema_digest"],
                restored_report["schema_digest"],
            )
            self.assertEqual(
                source_report["table_counts"],
                restored_report["table_counts"],
            )
            self.assertEqual(
                source_report["user_version"],
                restored_report["user_version"],
            )
            self.assertEqual(
                source_report["application_id"],
                restored_report["application_id"],
            )
            self.assertNotEqual(
                source_report["sha256"],
                restored_report["sha256"],
            )
            report = dict(report)
            report["restored_sha256"] = restored_report["sha256"]
            return report

        return restore

    def test_online_checkpoint_is_root_controlled_but_not_rollback_point(self):
        report = upgrade_release.create_upgrade_checkpoint(
            release=self.release,
            current=self.current,
            state=self.state,
            backup_dir=self.control,
        )
        self.assertEqual("checkpoint_ready", report["status"])
        self.assertEqual(
            upgrade_release.release_digest(self.release),
            report["release_sha256"],
        )
        self.assertTrue(report["online_backup"]["restore_drill"]["ok"])
        self.assertEqual(1, report["online_backup"]["table_counts"]["items"])
        self.assertEqual(0o700, self.control.stat().st_mode & 0o777)
        receipt_path = Path(report["receipt_path"])
        self.assertEqual(0o600, receipt_path.stat().st_mode & 0o777)
        self.assertNotIn(str(self.state), str(receipt_path))
        self.assertNotIn("final_backup", report)

    def test_success_uses_stopped_service_final_checkpoint_and_quiescent_start(self):
        commands = []
        inserted = False
        start_statuses = []
        rollback_ready_path = None

        def runner(command, **kwargs):
            nonlocal inserted, rollback_ready_path
            command = list(command)
            commands.append((command, kwargs))
            if command[:2] == ["systemctl", "start"] and command[2] in {
                "contentcrew.service", "caddy.service"
            }:
                start_statuses.append((command[2], self._receipt()["status"]))
            if (
                kwargs.get("cwd") == self.release
                and "deploy.preflight" in command
            ):
                rollback_ready_path = Path(
                    self._receipt()["rollback_ready"]["path"]
                )
                self.assertTrue(rollback_ready_path.is_file())
                self.assertEqual(self.control, rollback_ready_path.parent)
                self.assertEqual(
                    0o600, rollback_ready_path.stat().st_mode & 0o777
                )
            if (
                command == ["systemctl", "stop", "contentcrew.service"]
                and not inserted
            ):
                # This transaction lands after the online pre-checkpoint but
                # before systemctl reports the old service stopped.
                connection = sqlite3.connect(self.state / "contentcrew.db")
                connection.execute(
                    "INSERT INTO items(value) VALUES('last-old-service-write')"
                )
                connection.commit()
                connection.close()
                inserted = True

        report = self._upgrade(command_runner=runner)

        self.assertEqual("succeeded", report["status"])
        self.assertEqual(self.release, self.current.resolve())
        self.assertEqual(
            [("before-upgrade",), ("last-old-service-write",)], self._rows()
        )
        self.assertEqual(2, report["final_backup"]["table_counts"]["items"])
        flat = [command for command, _ in commands]
        set_env = [
            "systemctl", "set-environment",
            "CONTENTCREW_QUIESCENT=1", "CONTENTCREW_MAINTENANCE=1",
        ]
        unset_env = [
            "systemctl", "unset-environment",
            "CONTENTCREW_QUIESCENT", "CONTENTCREW_VALIDATION",
            "CONTENTCREW_MAINTENANCE",
        ]
        self.assertIn(set_env, flat)
        self.assertIn(unset_env, flat)
        self.assertLess(flat.index(set_env), flat.index(unset_env))
        self.assertEqual(
            3, flat.count(["systemctl", "start", "contentcrew.service"])
        )
        self.assertEqual(
            [
                ("contentcrew.service", "in_progress"),
                ("contentcrew.service", "in_progress"),
                ("contentcrew.service", "cutover_committed"),
                ("caddy.service", "cutover_committed"),
            ],
            start_statuses,
        )
        self.assertLess(
            flat.index(["systemctl", "stop", "caddy.service"]),
            flat.index(["systemctl", "stop", "contentcrew.service"]),
        )
        self.assertLess(
            flat.index(["systemctl", "stop", "paihuo-backup.service"]),
            flat.index(["systemctl", "stop", "contentcrew.service"]),
        )
        self.assertIsNotNone(rollback_ready_path)
        self.assertFalse(rollback_ready_path.exists())
        self.assertEqual("complete", self._receipt()["phase"])

    def test_candidate_runtime_gets_both_keys_and_mandatory_flags_only(self):
        seen = []
        candidate_commands = []

        def runner(command, **kwargs):
            if str(self.release / "venv" / "bin" / "python") in command:
                candidate_commands.append(list(command))
            if "-m" in command and "deploy.preflight" in command:
                seen.append((kwargs["cwd"], dict(kwargs["env"])))

        report = self._upgrade(command_runner=runner)
        self.assertEqual("succeeded", report["status"])
        previous_env = next(env for cwd, env in seen if cwd == self.previous)
        candidate_env = next(env for cwd, env in seen if cwd == self.release)
        self.assertNotIn("CONTENTCREW_SESSION_SECRET", previous_env)
        self.assertNotIn("CONTENTCREW_CONFIG_KEY", previous_env)
        self.assertEqual(
            "1", candidate_env["CONTENTCREW_REQUIRE_SESSION_SECRET"]
        )
        self.assertEqual(
            "1", candidate_env["CONTENTCREW_REQUIRE_CONFIG_KEY"]
        )
        self.assertEqual(
            self.session_value, candidate_env["CONTENTCREW_SESSION_SECRET"]
        )
        self.assertEqual(
            self.config_value, candidate_env["CONTENTCREW_CONFIG_KEY"]
        )
        self.assertTrue(candidate_commands)
        for command in candidate_commands:
            self.assertEqual("runuser", Path(command[0]).name)
            self.assertEqual(
                ["-u", "paihuo", "--preserve-environment", "--"],
                command[1:5],
            )

    def test_success_receipt_binds_secret_backup_and_control_attestation(self):
        report = self._upgrade()
        self.assertEqual(str(self.secret_backup), report["secret_backup_path"])
        self.assertEqual(
            str(self.control_attestation),
            report["control_plane_attestation_path"],
        )
        self.assertEqual(
            upgrade_release.control_plane_attestation_digest(
                self.control_attestation
            ),
            report["control_plane_attestation_sha256"],
        )
        self.assertEqual(
            str(self.release_archive), report["release_archive_path"]
        )
        self.assertEqual("4" * 64, report["release_archive_sha256"])
        self.assertEqual(
            str(self.release_build_receipt),
            report["release_build_receipt_path"],
        )
        self.assertEqual("5" * 64, report["release_build_receipt_sha256"])
        self.assertEqual(
            str(self.release_artifact_attestation),
            report["release_artifact_attestation_path"],
        )
        self.assertEqual(
            str(self.materialized_release_attestation),
            report["materialized_release_attestation_path"],
        )
        self.assertEqual("1" * 64, report["release_manifest_sha256"])
        self.assertEqual("2" * 64, report["release_payload_tree_sha256"])
        self.assertEqual("3" * 64, report["release_source_tree_sha256"])
        self.assertEqual(
            str(self.bootstrap_stage_attestation),
            report["bootstrap_stage_attestation_path"],
        )
        self.assertEqual(
            upgrade_release._file_sha256(self.bootstrap_stage_attestation),
            report["bootstrap_stage_attestation_sha256"],
        )
        self.assertEqual(
            self.bootstrap_venv_tree_sha256,
            report["bootstrap_venv_tree_sha256"],
        )
        self.assertEqual(
            self.bootstrap_wheelhouse_path,
            report["bootstrap_wheelhouse_attestation_path"],
        )
        self.assertEqual(
            "c" * 64,
            report["bootstrap_wheelhouse_wheel_set_sha256"],
        )
        self.assertEqual(
            52,
            report["bootstrap_wheelhouse_wheel_count"],
        )
        self.assertTrue(report["post_commit_proof"]["ok"])

    def test_materialized_proof_tamper_before_complete_never_succeeds(self):
        calls = 0

        def materialized_verifier(**_kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise upgrade_release.ReleaseVerifyError(
                    "materialized proof changed"
                )
            return {
                **self.artifact_identity,
                "artifact_archive_sha256": "4" * 64,
                "artifact_attestation_sha256": (
                    upgrade_release._file_sha256(
                        self.release_artifact_attestation
                    )
                ),
                "artifact_receipt_sha256": "5" * 64,
                "data_target": str(self.state),
                "materialized_release": str(self.release),
                "ok": True,
            }

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "no database rollback"
        ):
            self._upgrade(materialized_verifier=materialized_verifier)
        receipt = self._receipt()
        self.assertEqual(2, calls)
        self.assertNotEqual("succeeded", receipt["status"])
        self.assertNotEqual("complete", receipt["phase"])

    def test_bootstrap_venv_proof_tamper_before_complete_never_succeeds(self):
        calls = 0

        def bootstrap_verifier(release_id):
            nonlocal calls
            calls += 1
            return {
                **self.bootstrap_verification,
                "release_id": release_id,
                "venv_tree_sha256": (
                    self.bootstrap_venv_tree_sha256
                    if calls == 1
                    else "9" * 64
                ),
            }

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "no database rollback"
        ):
            self._upgrade(bootstrap_verifier=bootstrap_verifier)
        receipt = self._receipt()
        self.assertEqual(2, calls)
        self.assertNotEqual("succeeded", receipt["status"])
        self.assertNotEqual("complete", receipt["phase"])

    def test_wheelhouse_proof_tamper_before_complete_never_succeeds(self):
        calls = 0

        def bootstrap_verifier(release_id):
            nonlocal calls
            calls += 1
            return {
                **self.bootstrap_verification,
                "release_id": release_id,
                "wheelhouse_wheel_set_sha256": (
                    "c" * 64 if calls == 1 else "d" * 64
                ),
            }

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "no database rollback"
        ):
            self._upgrade(bootstrap_verifier=bootstrap_verifier)
        receipt = self._receipt()
        self.assertEqual(2, calls)
        self.assertNotEqual("succeeded", receipt["status"])
        self.assertNotEqual("complete", receipt["phase"])

    def test_committed_recovery_uses_receipt_evidence_not_new_cli_paths(self):
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "no database rollback"
        ):
            self._upgrade(
                post_commit_verifier=lambda **_kwargs: (_ for _ in ()).throw(
                    upgrade_release.UpgradeError("injected post proof failure")
                )
            )
        failed = self._receipt()
        self.assertTrue(str(failed["status"]).startswith("cutover_committed"))
        seen = []

        def artifact_verifier(**kwargs):
            seen.append(("artifact", Path(kwargs["archive"])))
            return {
                **self.artifact_identity,
                "archive_sha256": "4" * 64,
                "extract_dir": str(self.release),
                "ok": True,
                "receipt_sha256": "5" * 64,
            }

        def materialized_verifier(**kwargs):
            seen.append(("materialized", Path(kwargs["attestation"])))
            return {
                **self.artifact_identity,
                "artifact_archive_sha256": "4" * 64,
                "artifact_attestation_sha256": (
                    upgrade_release._file_sha256(
                        self.release_artifact_attestation
                    )
                ),
                "artifact_receipt_sha256": "5" * 64,
                "data_target": str(self.state),
                "materialized_release": str(self.release),
                "ok": True,
            }

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError,
            "interrupted committed cutover was completed",
        ):
            self._upgrade(
                release_archive=self.base / "new-cli-archive",
                release_build_receipt=self.base / "new-cli-receipt",
                release_artifact_attestation=self.base / "new-cli-artifact",
                materialized_release_attestation=(
                    self.base / "new-cli-materialized"
                ),
                bootstrap_stage_attestation=(
                    self.base / "new-cli-bootstrap"
                ),
                artifact_verifier=artifact_verifier,
                materialized_verifier=materialized_verifier,
            )
        recovered = self._receipt()
        self.assertEqual("succeeded", recovered["status"])
        self.assertEqual("complete", recovered["phase"])
        self.assertIn(("artifact", self.release_archive), seen)
        self.assertIn(
            ("materialized", self.materialized_release_attestation), seen
        )
        self.assertNotIn(("artifact", self.base / "new-cli-archive"), seen)
        self.assertEqual(
            str(self.bootstrap_stage_attestation),
            recovered["bootstrap_stage_attestation_path"],
        )

    def test_artifact_paths_and_identity_are_fixed_before_downtime(self):
        commands = []
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "paths are not fixed"
        ):
            self._upgrade(
                release_archive=self.base / "substituted.tar.gz",
                command_runner=lambda command, **_kwargs: commands.append(
                    list(command)
                ),
            )
        self.assertEqual([], commands)

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "identities disagree"
        ):
            self._upgrade(
                artifact_verifier=lambda **_kwargs: {
                    **self.artifact_identity,
                    "release_id": "different-schema47-r6",
                    "archive_sha256": "4" * 64,
                    "extract_dir": str(self.release),
                    "ok": True,
                    "receipt_sha256": "5" * 64,
                },
                command_runner=lambda command, **_kwargs: commands.append(
                    list(command)
                ),
            )
        self.assertEqual([], commands)

    def test_post_commit_proof_failure_never_writes_succeeded_receipt(self):
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "no database rollback"
        ):
            self._upgrade(
                post_commit_verifier=lambda **_kwargs: (_ for _ in ()).throw(
                    upgrade_release.UpgradeError("HTTPS proof failed")
                )
            )
        receipt = self._receipt()
        self.assertNotEqual("succeeded", receipt["status"])
        self.assertNotEqual("complete", receipt["phase"])

    def test_locked_runtime_comparison_rejects_missing_and_wrong_versions(self):
        locked = {"fastapi": "1.2.3", "uvicorn": "4.5.6"}
        with self.assertRaisesRegex(upgrade_release.UpgradeError, "missing"):
            upgrade_release._compare_locked_versions(
                locked, {"fastapi": "1.2.3"}
            )
        with self.assertRaisesRegex(upgrade_release.UpgradeError, "mismatches"):
            upgrade_release._compare_locked_versions(
                locked, {"fastapi": "9.9.9", "uvicorn": "4.5.6"}
            )
        with self.assertRaisesRegex(upgrade_release.UpgradeError, "unexpected"):
            upgrade_release._compare_locked_versions(
                locked,
                {
                    "fastapi": "1.2.3",
                    "uvicorn": "4.5.6",
                    "unlocked-addon": "1.0",
                },
            )
        # Virtualenv bootstrap tooling is intentionally outside app locking.
        upgrade_release._compare_locked_versions(
            locked,
            {
                "fastapi": "1.2.3",
                "uvicorn": "4.5.6",
                "pip": "99",
            },
        )

    def test_default_post_commit_proof_binds_new_backup_schema_and_https(self):
        with closing(sqlite3.connect(
            self.state / "contentcrew.db"
        )) as connection:
            connection.execute(
                "CREATE TABLE schema_version("
                "version INTEGER PRIMARY KEY,name TEXT,applied_at REAL)"
            )
            connection.execute(
                "INSERT INTO schema_version VALUES(47,'fixture',1)"
            )
            connection.commit()
        committed = datetime.now(tz=UTC)
        backup_now = committed + timedelta(seconds=2)
        backup = backup_database(
            self.state / "contentcrew.db",
            self.backups,
            run_restore_drill=True,
            now=backup_now,
        )
        backup_health.record_backup_success(
            backup_report=backup,
            backup_dir=self.backups,
            attestation_path=self.attestation,
            now=backup_now,
        )
        report = upgrade_release._post_commit_proof(
            state=self.state,
            periodic_backup_dir=self.backups,
            attestation_path=self.attestation,
            committed_at_utc=committed.isoformat(),
            candidate_schema_version=47,
            https_health_url="https://paihuo.invalid/healthz",
            https_health_checker=lambda _url: True,
        )
        self.assertTrue(report["ok"])
        self.assertEqual(47, report["backup_schema_version"])
        self.assertEqual(200, report["https_status"])
        with self.assertRaisesRegex(upgrade_release.UpgradeError, "HTTPS"):
            upgrade_release._post_commit_proof(
                state=self.state,
                periodic_backup_dir=self.backups,
                attestation_path=self.attestation,
                committed_at_utc=committed.isoformat(),
                candidate_schema_version=47,
                https_health_url="https://paihuo.invalid/healthz",
                https_health_checker=lambda _url: False,
            )

    def test_failed_candidate_restores_final_not_online_checkpoint(self):
        commands = []
        old_write_inserted = False

        def runner(command, **_kwargs):
            nonlocal old_write_inserted
            command = list(command)
            commands.append(command)
            if (
                command == ["systemctl", "stop", "contentcrew.service"]
                and self.current.resolve() == self.previous
                and not old_write_inserted
            ):
                connection = sqlite3.connect(self.state / "contentcrew.db")
                connection.execute(
                    "INSERT INTO items(value) VALUES('last-old-service-write')"
                )
                connection.commit()
                connection.close()
                old_write_inserted = True
            if (
                command == ["systemctl", "start", "contentcrew.service"]
                and self.current.resolve() == self.release
            ):
                connection = sqlite3.connect(self.state / "contentcrew.db")
                connection.execute(
                    "INSERT INTO items(value) VALUES('candidate-write')"
                )
                connection.commit()
                connection.close()

        def healthy(_url):
            return self.current.resolve() == self.previous

        with self.assertRaisesRegex(upgrade_release.UpgradeError, "rolled back"):
            self._upgrade(command_runner=runner, health_checker=healthy)

        self.assertEqual(self.previous, self.current.resolve())
        self.assertEqual(
            [("before-upgrade",), ("last-old-service-write",)], self._rows()
        )
        receipt = self._receipt()
        self.assertEqual("rolled_back", receipt["status"])
        self.assertEqual(2, receipt["final_backup"]["table_counts"]["items"])
        self.assertTrue(
            (Path(receipt["failed_database_quarantine"]) / "contentcrew.db").is_file()
        )

    def test_failure_after_commit_never_restores_old_database(self):
        commands = []
        health_calls = 0

        def runner(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if (
                command == ["systemctl", "start", "contentcrew.service"]
                and self._receipt()["status"] == "cutover_committed"
            ):
                with closing(sqlite3.connect(
                    self.state / "contentcrew.db"
                )) as connection:
                    connection.execute(
                        "INSERT INTO items(value) VALUES('committed-worker-write')"
                    )
                    connection.commit()

        def health(_url):
            nonlocal health_calls
            health_calls += 1
            return health_calls < 3

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "no database rollback"
        ):
            self._upgrade(command_runner=runner, health_checker=health)

        self.assertEqual(self.release, self.current.resolve())
        self.assertIn(("committed-worker-write",), self._rows())
        self.assertEqual(
            "cutover_committed_activation_failed",
            self._receipt()["status"],
        )
        self.assertNotIn(
            ["systemctl", "start", "caddy.service"], commands
        )

    def test_online_backup_failure_prevents_any_service_command(self):
        commands = []
        with patch.object(
            upgrade_release,
            "backup_database",
            side_effect=BackupError("injected failure"),
        ):
            with self.assertRaisesRegex(
                upgrade_release.UpgradeError, "backup/restore drill failed"
            ):
                self._upgrade(
                    command_runner=lambda command, **_kwargs: commands.append(command)
                )
        self.assertEqual([], commands)
        self.assertEqual(self.previous, self.current.resolve())

    def test_missing_fixed_smoke_or_start_guard_blocks_before_downtime(self):
        for argument in ("smoke_client", "start_guard"):
            commands = []
            with self.subTest(argument=argument):
                with self.assertRaisesRegex(
                    upgrade_release.UpgradeError, "trusted"
                ):
                    self._upgrade(
                        **{
                            argument: self.base / f"missing-{argument}",
                            "command_runner": (
                                lambda command, **_kwargs: commands.append(
                                    list(command)
                                )
                            ),
                        }
                    )
                self.assertEqual([], commands)

    def test_final_backup_failure_restarts_previous_without_cutover(self):
        commands = []
        original = upgrade_release.backup_database
        calls = 0

        def backup(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise BackupError("final checkpoint failed")
            return original(*args, **kwargs)

        with patch.object(upgrade_release, "backup_database", side_effect=backup):
            with self.assertRaisesRegex(
                upgrade_release.UpgradeError, "rolled back"
            ):
                self._upgrade(
                    command_runner=lambda command, **_kwargs: commands.append(
                        list(command)
                    ),
                    health_checker=lambda _url: self.current.resolve()
                    == self.previous,
                )
        self.assertEqual(2, calls)
        self.assertEqual(self.previous, self.current.resolve())
        self.assertEqual([("before-upgrade",)], self._rows())
        self.assertEqual(
            "failed_before_cutover_recovered", self._receipt()["status"]
        )
        self.assertEqual("previous_stopped", self._receipt()["failure_phase"])
        self.assertIn(
            "final checkpoint failed",
            self._receipt()["error_message"],
        )
        self.assertNotIn(
            [
                "systemctl", "set-environment",
                "CONTENTCREW_QUIESCENT=1", "CONTENTCREW_MAINTENANCE=1",
            ],
            commands,
        )

    def test_rollback_ready_allocation_failure_blocks_candidate(self):
        commands = []
        original_restore = upgrade_release.restore_drill

        def restore(source, *, restore_to=None, **kwargs):
            if (
                restore_to is not None
                and Path(restore_to).name.startswith(".rollback-ready-")
            ):
                raise OSError("injected state-volume ENOSPC")
            return original_restore(source, restore_to=restore_to, **kwargs)

        with patch.object(
            upgrade_release, "restore_drill", side_effect=restore
        ):
            with self.assertRaisesRegex(
                upgrade_release.UpgradeError, "rolled back"
            ):
                self._upgrade(
                    command_runner=lambda command, **_kwargs: commands.append(
                        list(command)
                    ),
                    health_checker=lambda _url: self.current.resolve()
                    == self.previous,
                )

        self.assertEqual(self.previous, self.current.resolve())
        self.assertEqual([("before-upgrade",)], self._rows())
        self.assertNotIn(
            ["systemctl", "set-environment", "CONTENTCREW_QUIESCENT=1",
             "CONTENTCREW_MAINTENANCE=1"],
            commands,
        )
        self.assertEqual(
            "failed_before_cutover_recovered", self._receipt()["status"]
        )
        self.assertEqual("previous_stopped", self._receipt()["failure_phase"])
        self.assertIn(
            "injected state-volume ENOSPC",
            self._receipt()["error_message"],
        )

    def test_rollback_ready_binds_source_and_distinct_restored_hashes(self):
        original_restore = upgrade_release.restore_drill
        with patch.object(
            upgrade_release,
            "restore_drill",
            side_effect=self._restore_with_distinct_but_equivalent_bytes(
                original_restore
            ),
        ):
            ready = upgrade_release._prepare_rollback_ready(
                Path(self.periodic["backup_path"]),
                self.periodic["sha256"],
                self.state,
                self.control,
            )

        ready_path = Path(ready["path"])
        try:
            self.assertEqual(self.periodic["sha256"], ready["source_sha256"])
            self.assertNotEqual(self.periodic["sha256"], ready["sha256"])
            self.assertEqual(
                ready["sha256"],
                upgrade_release.verify_database(ready_path)["sha256"],
            )
        finally:
            ready_path.unlink(missing_ok=True)

    def test_candidate_preflight_runs_after_final_snapshot_and_is_rolled_back(self):
        commands = []
        mutated = False

        def runner(command, **kwargs):
            nonlocal mutated
            command = list(command)
            commands.append(command)
            if (
                kwargs.get("cwd") == self.release
                and "deploy.preflight" in command
            ):
                with closing(sqlite3.connect(
                    self.state / "contentcrew.db"
                )) as connection:
                    connection.execute(
                        "INSERT INTO items(value) VALUES('preflight-damage')"
                    )
                    connection.commit()
                mutated = True
                raise upgrade_release.UpgradeError(
                    "candidate preflight failed"
                )

        with self.assertRaisesRegex(upgrade_release.UpgradeError, "rolled back"):
            self._upgrade(
                command_runner=runner,
                health_checker=lambda _url: self.current.resolve()
                == self.previous,
            )

        self.assertTrue(mutated)
        self.assertEqual([("before-upgrade",)], self._rows())
        self.assertEqual(
            "release_runtime_validation",
            self._receipt()["failure_phase"],
        )
        self.assertIn(
            "candidate preflight failed",
            self._receipt()["error_message"],
        )
        stop = ["systemctl", "stop", "contentcrew.service"]
        candidate_preflight = next(
            index
            for index, command in enumerate(commands)
            if "deploy.preflight" in command
            and str(self.release / "venv" / "bin" / "python") in command
        )
        self.assertLess(commands.index(stop), candidate_preflight)
        self.assertEqual("rolled_back", self._receipt()["status"])

    def test_stale_periodic_backup_blocks_before_service_stop(self):
        report = json.loads(self.attestation.read_text(encoding="utf-8"))
        old = datetime.now(tz=UTC) - timedelta(hours=25)
        report["recorded_at_utc"] = old.isoformat().replace("+00:00", "Z")
        report["backup_created_at_utc"] = old.isoformat().replace("+00:00", "Z")
        backup_health._atomic_attestation(self.attestation, report)
        commands = []
        with self.assertRaisesRegex(
            upgrade_release.BackupHealthError
            if hasattr(upgrade_release, "BackupHealthError")
            else Exception,
            "older than 24",
        ):
            self._upgrade(
                command_runner=lambda command, **_kwargs: commands.append(list(command))
            )
        self.assertNotIn(
            ["systemctl", "stop", "contentcrew.service"], commands
        )
        self.assertEqual(self.previous, self.current.resolve())

    def test_touching_app_owned_backup_cannot_refresh_root_attestation(self):
        old = datetime.now(tz=UTC) - timedelta(hours=25)
        report = json.loads(self.attestation.read_text(encoding="utf-8"))
        report["recorded_at_utc"] = old.isoformat().replace("+00:00", "Z")
        report["backup_created_at_utc"] = old.isoformat().replace("+00:00", "Z")
        backup_health._atomic_attestation(self.attestation, report)
        now = datetime.now(tz=UTC).timestamp()
        os.utime(self.periodic["backup_path"], (now, now))
        with self.assertRaisesRegex(
            backup_health.BackupHealthError, "older than 24"
        ):
            backup_health.check_backup_health(
                database=self.state / "contentcrew.db",
                backup_dir=self.backups,
                attestation_path=self.attestation,
            )

    def test_low_disk_blocks_checkpoint(self):
        with patch("deploy.backup_health._free_bytes", return_value=1):
            with self.assertRaisesRegex(
                upgrade_release.BackupHealthError, "insufficient backup space"
            ):
                self._upgrade()
        self.assertEqual(self.previous, self.current.resolve())

    def test_release_rejects_every_symlink_except_top_level_data(self):
        outside = self.base / "outside"
        outside.write_text("secret", encoding="utf-8")
        (self.release / "app" / "forbidden").symlink_to(outside)
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "forbidden symlink"
        ):
            upgrade_release.release_digest(self.release)

    def test_release_rejects_application_writable_controlled_content(self):
        source = self.release / "app" / "main.py"
        source.chmod(0o664)
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "release file is writable"
        ):
            upgrade_release.release_digest(self.release)

    def test_release_digest_covers_modes_contents_and_data_link(self):
        before = upgrade_release.release_digest(self.release)
        source = self.release / "app" / "main.py"
        source.write_text("MARKER='changed'\n", encoding="utf-8")
        after_source = upgrade_release.release_digest(self.release)
        self.assertNotEqual(before, after_source)
        source.chmod(0o700)
        after_mode = upgrade_release.release_digest(self.release)
        self.assertNotEqual(after_source, after_mode)
        other_state = self.base / "other-state"
        other_state.mkdir()
        (self.release / "data").unlink()
        (self.release / "data").symlink_to(other_state, target_is_directory=True)
        self.assertNotEqual(
            after_mode, upgrade_release.release_digest(self.release)
        )

    def test_candidate_and_previous_require_independent_real_virtualenvs(self):
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "must not share"
        ):
            self._upgrade(
                previous_venv=self.release / "venv",
            )
        (self.previous / "venv" / "bin" / "python").unlink()
        (self.previous / "venv" / "bin" / "python").symlink_to("/bin/sh")
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "regular copy"
        ):
            self._upgrade()

    def test_candidate_rejects_console_script_with_stale_build_shebang(self):
        ytdlp = self.release / "venv" / "bin" / "yt-dlp"
        ytdlp.write_text(
            "#!/var/tmp/paihuo-release.old/venv/bin/python\n",
            encoding="utf-8",
        )
        commands = []
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "shebang"
        ):
            self._upgrade(
                command_runner=lambda command, **_kwargs: commands.append(
                    list(command)
                )
            )
        self.assertEqual([], commands)

    def test_smoke_runs_as_service_user_with_minimal_environment(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((list(command), kwargs))

        with patch.dict(
            os.environ,
            {"LEAK_ME": "must-not-reach-smoke", "CLOUD_API_KEY": "secret"},
        ):
            self._upgrade(command_runner=runner)
        smoke_calls = [
            (command, kwargs)
            for command, kwargs in calls
            if any(str(part).endswith("smoke_readonly.py") for part in command)
        ]
        self.assertEqual(1, len(smoke_calls))
        command, kwargs = smoke_calls[0]
        self.assertEqual("runuser", Path(command[0]).name)
        self.assertEqual(
            ["-u", "paihuo-smoke", "--preserve-environment", "--"],
            command[1:5],
        )
        environment = kwargs["env"]
        self.assertEqual(
            {
                "PATH", "LANG", "LC_ALL", "HOME", "PYTHONDONTWRITEBYTECODE",
                "SMOKE_USERNAME", "SMOKE_PASSWORD",
            },
            set(environment),
        )
        self.assertNotIn("LEAK_ME", environment)
        self.assertNotIn("CLOUD_API_KEY", environment)

    def test_symlink_switch_fsync_failure_uses_actual_target_and_rolls_back(self):
        original_fsync = upgrade_release._fsync_directory
        injected = False

        def fail_after_candidate_replace(path):
            nonlocal injected
            if (
                not injected
                and Path(path) == self.current.parent
                and self.current.resolve() == self.release
            ):
                injected = True
                raise OSError("injected directory fsync failure")
            return original_fsync(path)

        with patch.object(
            upgrade_release, "_fsync_directory", side_effect=fail_after_candidate_replace
        ):
            with self.assertRaisesRegex(
                upgrade_release.UpgradeError, "rolled back"
            ):
                self._upgrade(
                    health_checker=lambda _url: self.current.resolve() == self.previous
                )
        self.assertTrue(injected)
        self.assertEqual(self.previous, self.current.resolve())
        self.assertEqual([("before-upgrade",)], self._rows())
        self.assertEqual("rolled_back", self._receipt()["status"])

    def test_signal_style_exception_after_candidate_selection_rolls_back(self):
        commands = []

        def runner(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if (
                command == ["systemctl", "start", "contentcrew.service"]
                and self.current.resolve() == self.release
            ):
                raise upgrade_release.UpgradeInterrupted("injected SIGTERM")

        with self.assertRaisesRegex(upgrade_release.UpgradeError, "rolled back"):
            self._upgrade(
                command_runner=runner,
                health_checker=lambda _url: self.current.resolve() == self.previous,
            )
        self.assertEqual(self.previous, self.current.resolve())
        self.assertEqual("rolled_back", self._receipt()["status"])

    def test_tampered_final_backup_is_never_restored_and_service_stays_stopped(self):
        commands = []
        tampered = False

        def runner(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if (
                command == ["systemctl", "start", "contentcrew.service"]
                and self.current.resolve() == self.release
            ):
                connection = sqlite3.connect(self.state / "contentcrew.db")
                connection.execute(
                    "INSERT INTO items(value) VALUES('candidate-write')"
                )
                connection.commit()
                connection.close()

        def unhealthy(_url):
            nonlocal tampered
            if self.current.resolve() == self.release and not tampered:
                receipt = self._receipt()
                Path(receipt["final_backup"]["backup_path"]).write_bytes(b"tampered")
                tampered = True
            return False

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "service kept stopped"
        ):
            self._upgrade(command_runner=runner, health_checker=unhealthy)
        self.assertTrue(tampered)
        self.assertEqual(
            ["systemctl", "stop", "contentcrew.service"], commands[-1]
        )
        self.assertIn(("candidate-write",), self._rows())
        self.assertEqual("rollback_failed", self._receipt()["status"])

        retry_commands = []
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "explicit manual repair"
        ):
            self._upgrade(
                command_runner=lambda command, **_kwargs: retry_commands.append(
                    list(command)
                )
            )
        self.assertEqual(
            [
                ["systemctl", "stop", "caddy.service"],
                ["systemctl", "stop", "contentcrew.service"],
            ],
            retry_commands,
        )

    def test_rollback_main_path_never_disappears_before_atomic_replace(self):
        live = self.state / "contentcrew.db"
        ready = upgrade_release._prepare_rollback_ready(
            Path(self.periodic["backup_path"]),
            self.periodic["sha256"],
            self.state,
            self.control,
        )
        original_replace = upgrade_release.os.replace
        observed = False

        def inspect_replace(source, destination):
            nonlocal observed
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                destination_path == live
                and source_path.name.startswith(".rollback-")
            ):
                observed = True
                self.assertTrue(
                    live.is_file(),
                    "live DB must still exist at the atomic replacement point",
                )
                raise OSError("injected before atomic replacement")
            return original_replace(source, destination)

        with patch.object(
            upgrade_release.os, "replace", side_effect=inspect_replace
        ):
            with self.assertRaisesRegex(OSError, "injected"):
                upgrade_release._restore_live_database(
                    Path(self.periodic["backup_path"]),
                    self.periodic["sha256"],
                    self.state,
                    self.release.name,
                    prepared_path=Path(ready["path"]),
                    prepared_sha256=ready["sha256"],
                )
        self.assertTrue(observed)
        self.assertTrue(live.is_file())
        self.assertEqual([("before-upgrade",)], self._rows())

    def test_rollback_failure_always_finishes_with_service_stop(self):
        commands = []
        candidate_started = False

        def runner(command, **_kwargs):
            nonlocal candidate_started
            command = list(command)
            commands.append(command)
            if (
                command == ["systemctl", "start", "contentcrew.service"]
                and self.current.resolve() == self.release
            ):
                candidate_started = True
            if (
                command == ["systemctl", "start", "contentcrew.service"]
                and candidate_started
                and self.current.resolve() == self.previous
            ):
                raise upgrade_release.UpgradeError("old service cannot start")

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "service kept stopped"
        ):
            self._upgrade(
                command_runner=runner,
                health_checker=lambda _url: self.current.resolve() == self.previous,
            )
        self.assertEqual(
            ["systemctl", "stop", "contentcrew.service"], commands[-1]
        )
        self.assertEqual("rollback_failed", self._receipt()["status"])

    def test_previous_release_tampering_blocks_rollback_and_keeps_service_stopped(self):
        commands = []
        tampered = False

        def runner(command, **_kwargs):
            nonlocal tampered
            command = list(command)
            commands.append(command)
            if (
                command == ["systemctl", "start", "contentcrew.service"]
                and self.current.resolve() == self.release
                and not tampered
            ):
                (self.previous / "app" / "main.py").write_text(
                    "MARKER='tampered'\n", encoding="utf-8"
                )
                tampered = True

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "service kept stopped"
        ):
            self._upgrade(command_runner=runner, health_checker=lambda _url: False)
        self.assertTrue(tampered)
        self.assertEqual(
            ["systemctl", "stop", "contentcrew.service"], commands[-1]
        )
        self.assertEqual("rollback_failed", self._receipt()["status"])

    def test_interrupted_receipt_is_recovered_before_new_upgrade(self):
        receipt = upgrade_release.create_upgrade_checkpoint(
            release=self.release,
            current=self.current,
            state=self.state,
            backup_dir=self.control,
        )
        receipt_path = Path(receipt["receipt_path"])
        receipt.update({
            "status": "in_progress",
            "phase": "candidate_selected",
            "candidate_venv": str(self.release / "venv"),
            "previous_venv": str(self.previous / "venv"),
            "previous_release_sha256": upgrade_release.release_digest(self.previous),
            "final_backup": receipt["online_backup"],
        })
        upgrade_release._atomic_json(receipt_path, receipt)
        self.current.unlink()
        self.current.symlink_to(self.release, target_is_directory=True)
        connection = sqlite3.connect(self.state / "contentcrew.db")
        connection.execute("INSERT INTO items(value) VALUES('interrupted-write')")
        connection.commit()
        connection.close()

        original_restore = upgrade_release._restore_live_database
        protected_restore_calls = []

        def inspect_protected_restore(*args, **kwargs):
            prepared_path = Path(kwargs["prepared_path"])
            prepared_sha256 = kwargs["prepared_sha256"]
            persisted = json.loads(
                receipt_path.read_text(encoding="utf-8")
            )["rollback_ready"]
            self.assertEqual(self.control, prepared_path.parent)
            self.assertNotEqual(self.state, prepared_path.parent)
            self.assertEqual(str(prepared_path), persisted["path"])
            self.assertEqual(prepared_sha256, persisted["sha256"])
            protected_restore_calls.append(prepared_path)
            return original_restore(*args, **kwargs)

        with patch.object(
            upgrade_release,
            "_restore_live_database",
            side_effect=inspect_protected_restore,
        ):
            with self.assertRaisesRegex(
                upgrade_release.UpgradeError,
                "interrupted upgrade was rolled back",
            ):
                self._upgrade(
                    health_checker=lambda _url: self.current.resolve()
                    == self.previous
                )
        self.assertEqual(1, len(protected_restore_calls))
        self.assertEqual(self.previous, self.current.resolve())
        self.assertEqual([("before-upgrade",)], self._rows())
        recovered = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("recovered_rollback", recovered["status"])
        self.assertEqual(1, len(list(self.control.glob("upgrade-*.json"))))

    def test_recovery_continues_when_rollback_replace_consumed_ready_file(self):
        receipt = upgrade_release.create_upgrade_checkpoint(
            release=self.release,
            current=self.current,
            state=self.state,
            backup_dir=self.control,
        )
        receipt_path = Path(receipt["receipt_path"])
        final = receipt["online_backup"]
        original_restore = upgrade_release.restore_drill
        with patch.object(
            upgrade_release,
            "restore_drill",
            side_effect=self._restore_with_distinct_but_equivalent_bytes(
                original_restore
            ),
        ):
            ready = upgrade_release._prepare_rollback_ready(
                Path(final["backup_path"]),
                final["sha256"],
                self.state,
                self.control,
            )
        self.assertNotEqual(final["sha256"], ready["sha256"])
        receipt.update({
            "status": "in_progress",
            "phase": "rollback_restore_committing",
            "candidate_venv": str(self.release / "venv"),
            "previous_venv": str(self.previous / "venv"),
            "previous_release_sha256": upgrade_release.release_digest(
                self.previous
            ),
            "final_backup": final,
            "rollback_ready": ready,
            "rollback_restore_required": True,
        })
        upgrade_release._atomic_json(receipt_path, receipt)
        self.current.unlink()
        self.current.symlink_to(self.release, target_is_directory=True)
        with closing(
            sqlite3.connect(self.state / "contentcrew.db")
        ) as connection:
            connection.execute(
                "INSERT INTO items(value) VALUES('candidate-write')"
            )
            connection.commit()

        # Simulate SIGKILL after os.replace consumed rollback-ready but before
        # rollback_database_restored was persisted.
        upgrade_release._restore_live_database(
            Path(final["backup_path"]),
            final["sha256"],
            self.state,
            self.release.name,
            prepared_path=Path(ready["path"]),
            prepared_sha256=ready["sha256"],
        )
        self.assertFalse(Path(ready["path"]).exists())
        self.assertEqual([("before-upgrade",)], self._rows())

        with (
            patch.object(
                upgrade_release,
                "_prepare_rollback_ready",
                side_effect=AssertionError(
                    "consumed ready must not be prepared again"
                ),
            ) as prepare_again,
            patch.object(
                upgrade_release,
                "_restore_live_database",
                side_effect=AssertionError(
                    "already-restored live DB must not be restored again"
                ),
            ) as restore_again,
        ):
            with self.assertRaisesRegex(
                upgrade_release.UpgradeError,
                "interrupted upgrade was rolled back",
            ):
                self._upgrade(
                    health_checker=lambda _url: self.current.resolve()
                    == self.previous
                )
        prepare_again.assert_not_called()
        restore_again.assert_not_called()

        recovered = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("recovered_rollback", recovered["status"])
        self.assertEqual(self.previous, self.current.resolve())
        self.assertEqual([("before-upgrade",)], self._rows())

    def test_missing_ready_does_not_accept_live_matching_source_hash(self):
        receipt = upgrade_release.create_upgrade_checkpoint(
            release=self.release,
            current=self.current,
            state=self.state,
            backup_dir=self.control,
        )
        receipt_path = Path(receipt["receipt_path"])
        final = receipt["online_backup"]
        original_restore_drill = upgrade_release.restore_drill
        with patch.object(
            upgrade_release,
            "restore_drill",
            side_effect=self._restore_with_distinct_but_equivalent_bytes(
                original_restore_drill
            ),
        ):
            ready = upgrade_release._prepare_rollback_ready(
                Path(final["backup_path"]),
                final["sha256"],
                self.state,
                self.control,
            )
        self.assertNotEqual(final["sha256"], ready["sha256"])
        receipt.update({
            "status": "in_progress",
            "phase": "rollback_restore_committing",
            "candidate_venv": str(self.release / "venv"),
            "previous_venv": str(self.previous / "venv"),
            "previous_release_sha256": upgrade_release.release_digest(
                self.previous
            ),
            "final_backup": final,
            "rollback_ready": ready,
            "rollback_restore_required": True,
        })
        upgrade_release._atomic_json(receipt_path, receipt)
        self.current.unlink()
        self.current.symlink_to(self.release, target_is_directory=True)
        Path(ready["path"]).unlink()

        live = self.state / "contentcrew.db"
        staged_live = self.state / ".source-hash-live.db"
        shutil.copyfile(final["backup_path"], staged_live)
        os.chmod(staged_live, live.stat().st_mode & 0o777)
        os.replace(staged_live, live)
        self.assertEqual(
            final["sha256"],
            upgrade_release.verify_database(live)["sha256"],
        )

        original_prepare = upgrade_release._prepare_rollback_ready
        original_restore = upgrade_release._restore_live_database
        with (
            patch.object(
                upgrade_release,
                "_prepare_rollback_ready",
                wraps=original_prepare,
            ) as prepare_again,
            patch.object(
                upgrade_release,
                "_restore_live_database",
                wraps=original_restore,
            ) as restore_again,
        ):
            with self.assertRaisesRegex(
                upgrade_release.UpgradeError,
                "interrupted upgrade was rolled back",
            ):
                self._upgrade(
                    health_checker=lambda _url: self.current.resolve()
                    == self.previous
                )

        self.assertEqual(1, prepare_again.call_count)
        self.assertEqual(1, restore_again.call_count)
        recovered = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("recovered_rollback", recovered["status"])
        self.assertIn("failed_database_quarantine", recovered)
        self.assertEqual(self.previous, self.current.resolve())
        self.assertEqual([("before-upgrade",)], self._rows())

    def test_replace_committed_then_fsync_error_uses_prepared_live_hash(self):
        receipt = upgrade_release.create_upgrade_checkpoint(
            release=self.release,
            current=self.current,
            state=self.state,
            backup_dir=self.control,
        )
        receipt_path = Path(receipt["receipt_path"])
        final = receipt["online_backup"]
        original_restore_drill = upgrade_release.restore_drill
        with patch.object(
            upgrade_release,
            "restore_drill",
            side_effect=self._restore_with_distinct_but_equivalent_bytes(
                original_restore_drill
            ),
        ):
            ready = upgrade_release._prepare_rollback_ready(
                Path(final["backup_path"]),
                final["sha256"],
                self.state,
                self.control,
            )
        self.assertNotEqual(final["sha256"], ready["sha256"])
        receipt.update({
            "status": "in_progress",
            "phase": "rollback_restore_committing",
            "candidate_venv": str(self.release / "venv"),
            "previous_venv": str(self.previous / "venv"),
            "previous_release_sha256": upgrade_release.release_digest(
                self.previous
            ),
            "final_backup": final,
            "rollback_ready": ready,
            "rollback_restore_required": True,
        })
        upgrade_release._atomic_json(receipt_path, receipt)
        self.current.unlink()
        self.current.symlink_to(self.release, target_is_directory=True)
        with closing(
            sqlite3.connect(self.state / "contentcrew.db")
        ) as connection:
            connection.execute(
                "INSERT INTO items(value) VALUES('candidate-write')"
            )
            connection.commit()

        original_restore = upgrade_release._restore_live_database

        def restore_then_report_fsync_failure(*args, **kwargs):
            original_restore(*args, **kwargs)
            raise OSError("injected directory fsync uncertainty")

        with (
            patch.object(
                upgrade_release,
                "_prepare_rollback_ready",
                side_effect=AssertionError(
                    "committed replacement must not be prepared again"
                ),
            ) as prepare_again,
            patch.object(
                upgrade_release,
                "_restore_live_database",
                side_effect=restore_then_report_fsync_failure,
            ) as restore_once,
        ):
            with self.assertRaisesRegex(
                upgrade_release.UpgradeError,
                "interrupted upgrade was rolled back",
            ):
                self._upgrade(
                    health_checker=lambda _url: self.current.resolve()
                    == self.previous
                )

        prepare_again.assert_not_called()
        self.assertEqual(1, restore_once.call_count)
        recovered = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("recovered_rollback", recovered["status"])
        self.assertEqual(self.previous, self.current.resolve())
        self.assertEqual([("before-upgrade",)], self._rows())

    def test_tampered_prepared_hash_is_rejected_before_live_replace(self):
        final = self.periodic
        original_restore_drill = upgrade_release.restore_drill
        with patch.object(
            upgrade_release,
            "restore_drill",
            side_effect=self._restore_with_distinct_but_equivalent_bytes(
                original_restore_drill
            ),
        ):
            ready = upgrade_release._prepare_rollback_ready(
                Path(final["backup_path"]),
                final["sha256"],
                self.state,
                self.control,
            )

        ready_path = Path(ready["path"])
        with closing(sqlite3.connect(ready_path)) as connection:
            connection.execute(
                "INSERT INTO items(value) VALUES('tampered-ready')"
            )
            connection.commit()
        self.assertNotEqual(
            ready["sha256"],
            upgrade_release.verify_database(ready_path)["sha256"],
        )

        live = self.state / "contentcrew.db"
        live_inode = live.stat().st_ino
        live_hash = upgrade_release.verify_database(live)["sha256"]
        source_hash = upgrade_release.verify_database(
            final["backup_path"]
        )["sha256"]
        original_replace = upgrade_release.os.replace
        live_replacements = []

        def observe_replace(source, destination):
            if Path(destination) == live:
                live_replacements.append((Path(source), Path(destination)))
            return original_replace(source, destination)

        with patch.object(
            upgrade_release.os,
            "replace",
            side_effect=observe_replace,
        ):
            with self.assertRaisesRegex(
                upgrade_release.UpgradeError,
                "SHA-256 no longer matches",
            ):
                upgrade_release._restore_live_database(
                    Path(final["backup_path"]),
                    final["sha256"],
                    self.state,
                    self.release.name,
                    prepared_path=ready_path,
                    prepared_sha256=ready["sha256"],
                )

        self.assertEqual([], live_replacements)
        self.assertEqual(live_inode, live.stat().st_ino)
        self.assertEqual(
            live_hash,
            upgrade_release.verify_database(live)["sha256"],
        )
        self.assertEqual(
            source_hash,
            upgrade_release.verify_database(final["backup_path"])["sha256"],
        )
        self.assertEqual([("before-upgrade",)], self._rows())

    def test_failed_chown_cannot_replace_live_with_wrong_owner(self):
        final = self.periodic
        ready = upgrade_release._prepare_rollback_ready(
            Path(final["backup_path"]),
            final["sha256"],
            self.state,
            self.control,
        )
        ready_path = Path(ready["path"])
        ready_stat = ready_path.stat()
        live = self.state / "contentcrew.db"
        live_stat = live.stat()
        live_hash = upgrade_release.verify_database(live)["sha256"]
        fake_prepared_stat = SimpleNamespace(
            st_mode=ready_stat.st_mode,
            st_nlink=1,
            st_dev=ready_stat.st_dev,
            st_ino=ready_stat.st_ino,
            st_uid=live_stat.st_uid + 1,
            st_gid=live_stat.st_gid,
        )
        original_replace = upgrade_release.os.replace
        live_replacements = []

        def observe_replace(source, destination):
            if Path(destination) == live:
                live_replacements.append((Path(source), Path(destination)))
            return original_replace(source, destination)

        with (
            patch.object(
                upgrade_release.os,
                "chown",
                side_effect=PermissionError("injected chown failure"),
            ),
            patch.object(
                upgrade_release.os,
                "fstat",
                return_value=fake_prepared_stat,
            ),
            patch.object(
                upgrade_release.os,
                "replace",
                side_effect=observe_replace,
            ),
        ):
            with self.assertRaisesRegex(
                upgrade_release.UpgradeError,
                "ownership or mode could not be prepared",
            ):
                upgrade_release._restore_live_database(
                    Path(final["backup_path"]),
                    final["sha256"],
                    self.state,
                    self.release.name,
                    prepared_path=ready_path,
                    prepared_sha256=ready["sha256"],
                )

        self.assertEqual([], live_replacements)
        self.assertEqual(live_stat.st_ino, live.stat().st_ino)
        self.assertEqual(
            live_hash,
            upgrade_release.verify_database(live)["sha256"],
        )
        self.assertEqual([("before-upgrade",)], self._rows())

    def test_missing_source_link_on_dual_hash_receipt_fails_closed(self):
        receipt = upgrade_release.create_upgrade_checkpoint(
            release=self.release,
            current=self.current,
            state=self.state,
            backup_dir=self.control,
        )
        receipt_path = Path(receipt["receipt_path"])
        final = receipt["online_backup"]
        original_restore_drill = upgrade_release.restore_drill
        with patch.object(
            upgrade_release,
            "restore_drill",
            side_effect=self._restore_with_distinct_but_equivalent_bytes(
                original_restore_drill
            ),
        ):
            ready = upgrade_release._prepare_rollback_ready(
                Path(final["backup_path"]),
                final["sha256"],
                self.state,
                self.control,
            )
        self.assertNotEqual(final["sha256"], ready["sha256"])
        del ready["source_sha256"]
        receipt.update({
            "status": "in_progress",
            "phase": "rollback_restore_committing",
            "candidate_venv": str(self.release / "venv"),
            "previous_venv": str(self.previous / "venv"),
            "previous_release_sha256": upgrade_release.release_digest(
                self.previous
            ),
            "final_backup": final,
            "rollback_ready": ready,
            "rollback_restore_required": True,
        })
        upgrade_release._atomic_json(receipt_path, receipt)
        self.current.unlink()
        self.current.symlink_to(self.release, target_is_directory=True)
        live_hash = upgrade_release.verify_database(
            self.state / "contentcrew.db"
        )["sha256"]

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError,
            "interrupted upgrade recovery failed",
        ):
            self._upgrade()

        failed = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("rollback_failed", failed["status"])
        self.assertTrue(failed["stop_verified"])
        self.assertEqual(
            live_hash,
            upgrade_release.verify_database(
                self.state / "contentcrew.db"
            )["sha256"],
        )

    def test_interrupted_candidate_is_stopped_before_new_release_is_parsed(self):
        receipt = upgrade_release.create_upgrade_checkpoint(
            release=self.release,
            current=self.current,
            state=self.state,
            backup_dir=self.control,
        )
        receipt_path = Path(receipt["receipt_path"])
        receipt.update({
            "status": "in_progress",
            "phase": "candidate_selected",
            "candidate_venv": str(self.release / "venv"),
            "previous_venv": str(self.previous / "venv"),
            "previous_release_sha256": upgrade_release.release_digest(
                self.previous
            ),
            "final_backup": receipt["online_backup"],
        })
        upgrade_release._atomic_json(receipt_path, receipt)
        self.current.unlink()
        self.current.symlink_to(self.release, target_is_directory=True)
        shutil.rmtree(self.release)
        commands = []

        with self.assertRaises(upgrade_release.UpgradeError):
            self._upgrade(
                release=self.base / "also-missing-new-release",
                command_runner=lambda command, **_kwargs: commands.append(
                    list(command)
                ),
            )

        self.assertEqual(
            ["systemctl", "stop", "caddy.service"],
            commands[0],
        )
        self.assertEqual(
            ["systemctl", "stop", "contentcrew.service"],
            commands[1],
        )
        recovered = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("recovered_rollback", recovered["status"])
        self.assertEqual(self.previous, self.current.resolve())

    def test_unprovable_stop_is_recorded_as_uncontained(self):
        receipt = upgrade_release.create_upgrade_checkpoint(
            release=self.release,
            current=self.current,
            state=self.state,
            backup_dir=self.control,
        )
        receipt_path = Path(receipt["receipt_path"])
        receipt.update({
            "status": "rollback_failed",
            "phase": "rollback_failed",
            "candidate_venv": str(self.release / "venv"),
            "previous_venv": str(self.previous / "venv"),
            "previous_release_sha256": upgrade_release.release_digest(
                self.previous
            ),
        })
        upgrade_release._atomic_json(receipt_path, receipt)

        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "could not be proven inactive"
        ):
            self._upgrade(inactive_checker=lambda _service: False)

        failed = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("rollback_failed_uncontained", failed["status"])
        self.assertFalse(failed["stop_verified"])

    def test_lock_and_receipt_symlinks_are_rejected_without_following(self):
        self.control.mkdir(mode=0o700, exist_ok=True)
        target = self.base / "attacker-file"
        target.write_text("do-not-touch", encoding="utf-8")
        (self.control / "upgrade.lock").symlink_to(target)
        with self.assertRaises(OSError):
            with upgrade_release._upgrade_lock(self.control / "upgrade.lock"):
                pass
        self.assertEqual("do-not-touch", target.read_text(encoding="utf-8"))

        (self.control / "upgrade.lock").unlink()
        (self.control / "upgrade-malicious.json").symlink_to(target)
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "receipt is a symlink"
        ):
            upgrade_release._incomplete_receipts(self.control)

    def test_legacy_boss_credential_file_is_never_read_or_sent_to_candidate(self):
        alias = self.base / "credentials-link"
        alias.symlink_to(self.credentials)
        calls = []
        self._upgrade(
            smoke_credentials=alias,
            command_runner=lambda command, **kwargs: calls.append(
                (list(command), kwargs)
            ),
        )
        smoke_env = next(
            kwargs["env"]
            for command, kwargs in calls
            if any(str(part).endswith("smoke_readonly.py") for part in command)
        )
        self.assertNotEqual("boss", smoke_env["SMOKE_USERNAME"])
        self.assertNotEqual("one-time-secret", smoke_env["SMOKE_PASSWORD"])
        with closing(
            sqlite3.connect(self.state / "contentcrew.db")
        ) as connection:
            self.assertFalse(
                connection.execute(
                    "SELECT 1 FROM users "
                    "WHERE username LIKE '__release_smoke_%'"
                ).fetchone()
            )

    def test_orphan_smoke_cleanup_does_not_delete_similar_normal_username(self):
        now = datetime.now(tz=UTC).timestamp()
        with closing(
            sqlite3.connect(self.state / "contentcrew.db")
        ) as connection:
            for username in (
                "__release_smoke_orphan",
                "aarelease-smoke-user",
            ):
                connection.execute(
                    "INSERT INTO users("
                    "tenant_id,username,password_hash,role,modules_json,"
                    "enabled,created_at,updated_at"
                    ") VALUES(1,?,'hash','member','[]',1,?,?)",
                    (username, now, now),
                )
            connection.commit()

        upgrade_release._purge_orphaned_smoke_identities(self.state)

        with closing(
            sqlite3.connect(self.state / "contentcrew.db")
        ) as connection:
            usernames = {
                row[0]
                for row in connection.execute(
                    "SELECT username FROM users"
                ).fetchall()
            }
        self.assertNotIn("__release_smoke_orphan", usernames)
        self.assertIn("aarelease-smoke-user", usernames)

    def test_application_state_cannot_be_used_as_upgrade_control_directory(self):
        with self.assertRaisesRegex(
            upgrade_release.UpgradeError, "outside application state"
        ):
            self._upgrade(control_dir=self.state / "attacker-control")


if __name__ == "__main__":
    unittest.main()
