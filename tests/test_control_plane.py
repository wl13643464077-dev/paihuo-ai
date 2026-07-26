import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from deploy import control_plane


class ControlPlaneCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.control = self.base / "control"
        self.control.mkdir(mode=0o700)
        self.release = self.base / "schema47-r6"
        (self.release / "deploy").mkdir(parents=True)
        for relative in control_plane.OPS_FILES:
            path = self.release / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"candidate:{relative}\n", encoding="utf-8")
        for name in control_plane.UNIT_FILES:
            body = f"[Unit]\nDescription=candidate {name}\n"
            if name == "contentcrew.service":
                body += (
                    "[Service]\n"
                    "EnvironmentFile=/etc/paihuo/paihuo.env\n"
                    "Environment=CONTENTCREW_REQUIRE_SESSION_SECRET=1\n"
                    "Environment=CONTENTCREW_REQUIRE_CONFIG_KEY=1\n"
                    "ExecStartPre=+/usr/bin/env PYTHONPATH=/usr/local/lib/paihuo-ops "
                    "/usr/bin/python3 -m deploy.session_secret_env --check "
                    "--path /etc/paihuo/paihuo.env\n"
                )
            (self.release / "deploy" / name).write_text(body, encoding="utf-8")
        (self.release / "deploy" / "caddy-paihuo-guard.conf").write_text(
            "[Service]\nSupplementaryGroups=paihuo-public\n", encoding="utf-8"
        )
        (self.release / "deploy" / "Caddyfile").write_text(
            "paihuo.invalid { respond /healthz 200 }\n", encoding="utf-8"
        )

        self.ops_parent = self.base / "usr-local-lib"
        self.ops_parent.mkdir()
        self.old_ops = self.ops_parent / "paihuo-ops-old"
        for relative in control_plane.OPS_FILES:
            path = self.old_ops / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"old:{relative}\n", encoding="utf-8")
        self.ops_link = self.ops_parent / "paihuo-ops"
        self.ops_link.symlink_to(self.old_ops, target_is_directory=True)
        self.systemd = self.base / "systemd"
        self.systemd.mkdir()
        for name in control_plane.UNIT_FILES:
            (self.systemd / name).write_text(
                f"old unit {name}\n", encoding="utf-8"
            )
        self.dropin = self.systemd / "caddy.service.d" / "paihuo-guard.conf"
        self.dropin.parent.mkdir()
        self.dropin.write_text("old dropin\n", encoding="utf-8")
        self.caddyfile = self.base / "etc-caddy" / "Caddyfile"
        self.caddyfile.parent.mkdir()
        self.caddyfile.write_text("old caddy\n", encoding="utf-8")
        self.caddy_bin = self.base / "caddy"
        self.caddy_bin.write_text("# fixture\n", encoding="utf-8")
        self.launcher = self.base / "usr-local-sbin" / "paihuo-upgrade"
        self.launcher.parent.mkdir()
        self.launcher.write_text("old launcher\n", encoding="utf-8")
        self.launcher.chmod(0o755)
        self.layout = control_plane.Layout(
            ops_link=self.ops_link,
            launcher=self.launcher,
            systemd_dir=self.systemd,
            caddy_dropin=self.dropin,
            caddyfile=self.caddyfile,
            caddy_bin=self.caddy_bin,
            start_permit=self.base / "run" / "start.permit",
        )
        self.original = {
            path: path.read_bytes()
            for path in [
                *(self.systemd / name for name in control_plane.UNIT_FILES),
                self.dropin,
                self.caddyfile,
                self.launcher,
                *(self.old_ops / relative for relative in control_plane.OPS_FILES),
            ]
        }
        self.commands = []

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, command):
        command = list(command)
        self.commands.append(command)
        stdout = ""
        if command[:3] == ["systemctl", "show", "contentcrew.service"]:
            property_name = command[4]
            values = {
                "FragmentPath": str(self.systemd / "contentcrew.service"),
                "DropInPaths": "",
                "EnvironmentFiles": "/etc/paihuo/paihuo.env (ignore_errors=no)",
                "Environment": (
                    "CONTENTCREW_REQUIRE_SESSION_SECRET=1 "
                    "CONTENTCREW_REQUIRE_CONFIG_KEY=1"
                ),
                "ExecStartPre": (
                    "/usr/bin/env PYTHONPATH=/usr/local/lib/paihuo-ops "
                    "/usr/bin/python3 -m deploy.session_secret_env --check "
                    "--path /etc/paihuo/paihuo.env"
                ),
            }
            stdout = values[property_name] + ("\n" if values[property_name] else "")
        elif command[:3] == ["systemctl", "show", "caddy.service"]:
            property_name = command[4]
            values = {
                "DropInPaths": str(self.dropin),
                "ExecStartPre": (
                    "/usr/bin/python3 /usr/local/lib/paihuo-ops/deploy/"
                    "start_guard.py --mode proxy "
                    "--control-dir /var/lib/paihuo-upgrade "
                    "--permit /run/paihuo-upgrade/start.permit"
                ),
                "SupplementaryGroups": "paihuo-public",
            }
            stdout = values[property_name] + "\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def test_prepare_snapshots_installs_and_attests_without_contents(self):
        report = control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        self.assertEqual("installed", report["status"])
        self.assertEqual(
            self.ops_parent / "paihuo-ops-schema47-r6",
            self.ops_link.resolve(),
        )
        self.assertEqual(
            (self.release / "deploy" / "contentcrew.service").read_bytes(),
            (self.systemd / "contentcrew.service").read_bytes(),
        )
        self.assertEqual(
            (self.release / "deploy" / "upgrade.sh").read_bytes(),
            self.launcher.read_bytes(),
        )
        self.assertEqual(0o755, self.launcher.stat().st_mode & 0o777)
        attestation = Path(report["attestation_path"])
        self.assertEqual(0o600, attestation.stat().st_mode & 0o777)
        self.assertEqual(0o700, attestation.parent.stat().st_mode & 0o777)
        decoded = json.loads(attestation.read_text(encoding="utf-8"))
        serialized = json.dumps(decoded)
        self.assertNotIn("old unit", serialized)
        self.assertNotIn("candidate:deploy", serialized)
        self.assertTrue(decoded["installed"]["validation"]["caddy_validate"])
        self.assertIn(["systemctl", "daemon-reload"], self.commands)
        self.assertTrue(
            any(command[:2] == ["systemd-analyze", "verify"] for command in self.commands)
        )

    def test_unknown_systemd_state_blocks_before_snapshot_or_install(self):
        def runner(command):
            if list(command)[:3] == [
                "systemctl", "is-active", "--quiet"
            ]:
                return SimpleNamespace(returncode=4, stdout="", stderr="")
            return self.runner(command)

        with self.assertRaisesRegex(
            control_plane.ControlPlaneError, "could not be proven"
        ):
            control_plane.prepare_control_plane(
                release=self.release,
                control_dir=self.control,
                layout=self.layout,
                runner=runner,
            )
        self.assertEqual(self.old_ops, self.ops_link.resolve())

    def test_explicit_restore_atomically_restores_original_bytes_and_link(self):
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        report = control_plane.restore_control_plane(
            control_dir=self.control,
            release_id=self.release.name,
            layout=self.layout,
            runner=self.runner,
        )
        self.assertEqual("restored", report["status"])
        self.assertEqual(self.old_ops, self.ops_link.resolve())
        for path, body in self.original.items():
            self.assertEqual(body, path.read_bytes(), path)

    def test_validation_failure_restores_every_original_object(self):
        def failing_runner(command):
            if list(command)[:2] == ["systemd-analyze", "verify"]:
                raise control_plane.ControlPlaneError("injected")
            return self.runner(command)

        # Restoration also validates; fail only the first verify invocation.
        failed = False

        def once_runner(command):
            nonlocal failed
            if list(command)[:2] == ["systemd-analyze", "verify"] and not failed:
                failed = True
                raise control_plane.ControlPlaneError("injected")
            return self.runner(command)

        with self.assertRaises(control_plane.ControlPlaneError):
            control_plane.prepare_control_plane(
                release=self.release,
                control_dir=self.control,
                layout=self.layout,
                runner=once_runner,
            )
        self.assertEqual(self.old_ops, self.ops_link.resolve())
        for path, body in self.original.items():
            self.assertEqual(body, path.read_bytes(), path)

    def test_existing_mismatched_snapshot_is_not_overwritten(self):
        snapshot = self.control / "control-plane" / self.release.name
        snapshot.mkdir(parents=True, mode=0o700)
        snapshot.parent.chmod(0o700)
        attestation = snapshot / "attestation.json"
        attestation.write_text(
            json.dumps(
                {
                    "candidate_digest": "different",
                    "format": control_plane.ATTESTATION_FORMAT,
                    "release_id": self.release.name,
                    "status": "snapshotted",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        attestation.chmod(0o600)
        before = attestation.read_bytes()
        with self.assertRaisesRegex(
            control_plane.ControlPlaneError, "does not match"
        ):
            control_plane.prepare_control_plane(
                release=self.release,
                control_dir=self.control,
                layout=self.layout,
                runner=self.runner,
            )
        self.assertEqual(before, attestation.read_bytes())
        self.assertEqual(self.old_ops, self.ops_link.resolve())

    def test_reused_snapshot_is_self_verified_before_any_control_mutation(self):
        report = control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        attestation = Path(report["attestation_path"])
        decoded = json.loads(attestation.read_text(encoding="utf-8"))
        object_record = next(
            record
            for record in decoded["original"].values()
            if record.get("type") == "file"
        )
        snapshot_object = attestation.parent / object_record["snapshot"]
        snapshot_object.write_bytes(b"tampered")
        snapshot_object.chmod(0o600)
        selected_before = self.ops_link.resolve()
        unit_before = (self.systemd / "contentcrew.service").read_bytes()
        with self.assertRaisesRegex(
            control_plane.ControlPlaneError, "integrity mismatch"
        ):
            control_plane.prepare_control_plane(
                release=self.release,
                control_dir=self.control,
                layout=self.layout,
                runner=self.runner,
            )
        self.assertEqual(selected_before, self.ops_link.resolve())
        self.assertEqual(
            unit_before, (self.systemd / "contentcrew.service").read_bytes()
        )

    def test_wrong_effective_fragment_fails_closed_and_restores(self):
        def runner(command):
            result = self.runner(command)
            if list(command)[:3] == [
                "systemctl",
                "show",
                "contentcrew.service",
            ]:
                result.stdout = str(self.base / "wrong.service")
            return result

        with self.assertRaises(control_plane.ControlPlaneError):
            control_plane.prepare_control_plane(
                release=self.release,
                control_dir=self.control,
                layout=self.layout,
                runner=runner,
            )
        self.assertEqual(self.old_ops, self.ops_link.resolve())

    def test_caddy_effective_override_cannot_remove_fixed_proxy_guard(self):
        def runner(command):
            result = self.runner(command)
            command = list(command)
            if (
                command[:3] == ["systemctl", "show", "caddy.service"]
                and command[4] == "ExecStartPre"
            ):
                result.stdout = ""
            return result

        with self.assertRaisesRegex(
            control_plane.ControlPlaneError, "proxy start guard"
        ):
            control_plane.prepare_control_plane(
                release=self.release,
                control_dir=self.control,
                layout=self.layout,
                runner=runner,
            )
        self.assertEqual(self.old_ops, self.ops_link.resolve())

    def _write_upgrade_receipt(self, status, *, release=None, phase="fixture"):
        path = self.control / f"upgrade-{status}.json"
        path.write_text(
            json.dumps(
                {
                    "created_at_utc": (
                        control_plane.datetime.now(tz=control_plane.UTC).isoformat()
                    ),
                    "phase": phase,
                    "release": str(release or self.release),
                    "status": status,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def test_gate_rejects_incomplete_receipt_for_different_release(self):
        other = self.base / "other-schema47-r6"
        other.mkdir()
        self._write_upgrade_receipt(
            "in_progress", release=other, phase="candidate_selected"
        )
        with self.assertRaisesRegex(
            control_plane.ControlPlaneError, "different release"
        ):
            control_plane.gate_existing_upgrade(
                control_dir=self.control,
                release=self.release,
            )
        self.assertEqual(self.old_ops, self.ops_link.resolve())

    def test_gate_rejects_reusing_closed_release_snapshot_without_marker(self):
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        with self.assertRaisesRegex(
            control_plane.ControlPlaneError, "release id already"
        ):
            control_plane.gate_existing_upgrade(
                control_dir=self.control,
                release=self.release,
            )

    def test_attestation_publish_failure_is_inside_restore_domain(self):
        original = control_plane._atomic_json

        def publish(path, payload):
            if payload.get("status") == "installed":
                raise OSError("injected attestation fsync failure")
            return original(path, payload)

        with patch.object(
            control_plane, "_atomic_json", side_effect=publish
        ):
            with self.assertRaises(OSError):
                control_plane.prepare_control_plane(
                    release=self.release,
                    control_dir=self.control,
                    layout=self.layout,
                    runner=self.runner,
                )
        self.assertEqual(self.old_ops, self.ops_link.resolve())
        for path, body in self.original.items():
            self.assertEqual(body, path.read_bytes(), path)

    def test_restore_restarts_snapshot_active_units_and_never_starts_template(self):
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        self.commands.clear()
        control_plane.restore_control_plane(
            control_dir=self.control,
            release_id=self.release.name,
            layout=self.layout,
            runner=self.runner,
        )
        self.assertIn(
            ["systemctl", "restart", "contentcrew.service"], self.commands
        )
        self.assertIn(
            ["systemctl", "restart", "paihuo-backup.timer"], self.commands
        )
        self.assertIn(
            ["systemctl", "restart", "caddy.service"], self.commands
        )
        self.assertFalse(
            any(
                "paihuo-failure-alert@.service" in command
                and command[1] in {"start", "stop", "restart", "try-restart"}
                for command in self.commands
                if len(command) > 1
            )
        )

    def test_committed_failure_reconcile_retains_candidate_and_contains_proxy(self):
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        self._write_upgrade_receipt(
            "cutover_committed_proxy_failed",
            phase="committed_activation_failed",
        )
        active = {"caddy.service": True, "contentcrew.service": True}

        def runner(command):
            command = list(command)
            if command[:2] == ["systemctl", "stop"] and command[2] in active:
                active[command[2]] = False
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if (
                command[:3] == ["systemctl", "is-active", "--quiet"]
                and command[3] in active
            ):
                return SimpleNamespace(
                    returncode=0 if active[command[3]] else 3,
                    stdout="",
                    stderr="",
                )
            return self.runner(command)

        report = control_plane.reconcile_failed_upgrade(
            control_dir=self.control,
            release_id=self.release.name,
            layout=self.layout,
            runner=runner,
        )
        self.assertEqual("retained_committed_failure", report["status"])
        self.assertEqual(
            self.ops_parent / "paihuo-ops-schema47-r6",
            self.ops_link.resolve(),
        )
        self.assertFalse(active["caddy.service"])
        self.assertFalse(active["contentcrew.service"])

    def test_rolled_back_failure_reconcile_restores_old_control_runtime(self):
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        self._write_upgrade_receipt("rolled_back", phase="rollback_complete")
        report = control_plane.reconcile_failed_upgrade(
            control_dir=self.control,
            release_id=self.release.name,
            layout=self.layout,
            runner=self.runner,
        )
        self.assertEqual("restored", report["status"])
        self.assertEqual(self.old_ops, self.ops_link.resolve())

    def test_no_receipt_reconcile_restores_prepare_or_secret_failure(self):
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        report = control_plane.reconcile_failed_upgrade(
            control_dir=self.control,
            release_id=self.release.name,
            layout=self.layout,
            runner=self.runner,
        )
        self.assertEqual("restored", report["status"])
        self.assertEqual(self.old_ops, self.ops_link.resolve())

    def test_old_succeeded_receipt_cannot_commit_new_prepare(self):
        self._write_upgrade_receipt("succeeded", phase="complete")
        receipt = next(self.control.glob("upgrade-*.json"))
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["created_at_utc"] = "2000-01-01T00:00:00+00:00"
        receipt.write_text(json.dumps(value) + "\n", encoding="utf-8")
        receipt.chmod(0o600)
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        report = control_plane.reconcile_failed_upgrade(
            control_dir=self.control,
            release_id=self.release.name,
            layout=self.layout,
            runner=self.runner,
        )
        self.assertEqual("restored", report["status"])

    def test_in_progress_reconcile_contains_before_effective_verification(self):
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        self._write_upgrade_receipt(
            "in_progress", phase="candidate_selected"
        )
        active = {"caddy.service": True, "contentcrew.service": True}
        commands = []

        def runner(command):
            command = list(command)
            commands.append(command)
            if command[:2] == ["systemctl", "stop"] and command[2] in active:
                active[command[2]] = False
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if (
                command[:3] == ["systemctl", "is-active", "--quiet"]
                and command[3] in active
            ):
                return SimpleNamespace(
                    returncode=0 if active[command[3]] else 3,
                    stdout="",
                    stderr="",
                )
            if command[:3] == ["systemctl", "show", "contentcrew.service"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="/definitely/wrong\n",
                    stderr="",
                )
            return self.runner(command)

        with self.assertRaises(control_plane.ControlPlaneError):
            control_plane.reconcile_failed_upgrade(
                control_dir=self.control,
                release_id=self.release.name,
                layout=self.layout,
                runner=runner,
            )
        self.assertEqual(
            [
                ["systemctl", "stop", "caddy.service"],
                ["systemctl", "is-active", "--quiet", "caddy.service"],
                ["systemctl", "stop", "contentcrew.service"],
                ["systemctl", "is-active", "--quiet", "contentcrew.service"],
            ],
            commands[:4],
        )
        self.assertFalse(any(active.values()))
        self.assertEqual(
            self.ops_parent / "paihuo-ops-schema47-r6",
            self.ops_link.resolve(),
        )

    def test_wrapper_transaction_marker_blocks_other_release_and_is_durable(self):
        started = control_plane.begin_wrapper_transaction(
            control_dir=self.control,
            release=self.release,
        )
        marker = Path(started["transaction_path"])
        self.assertEqual(0o600, marker.stat().st_mode & 0o777)
        other = self.base / "other-schema47-r6"
        other.mkdir()
        with self.assertRaisesRegex(
            control_plane.ControlPlaneError, "different release"
        ):
            control_plane.gate_existing_upgrade(
                control_dir=self.control, release=other
            )
        resumed = control_plane.gate_existing_upgrade(
            control_dir=self.control, release=self.release
        )
        self.assertTrue(resumed["same_release"] is False)
        control_plane.finish_wrapper_transaction(
            control_dir=self.control, release_id=self.release.name
        )
        self.assertFalse(marker.exists())

    def test_incomplete_receipt_keeps_marker_for_same_release_recovery(self):
        started = control_plane.begin_wrapper_transaction(
            control_dir=self.control,
            release=self.release,
        )
        marker = Path(started["transaction_path"])
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        self._write_upgrade_receipt(
            "in_progress", phase="candidate_selected"
        )
        finish = control_plane.finish_wrapper_transaction(
            control_dir=self.control, release_id=self.release.name
        )
        self.assertEqual("retained_incomplete", finish["status"])
        self.assertTrue(marker.exists())
        gate = control_plane.gate_existing_upgrade(
            control_dir=self.control, release=self.release
        )
        self.assertTrue(gate["same_release"])

    def test_resumed_prepare_never_hides_older_same_release_incomplete(self):
        control_plane.begin_wrapper_transaction(
            control_dir=self.control,
            release=self.release,
        )
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        receipt = self._write_upgrade_receipt(
            "in_progress", phase="candidate_selected"
        )
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["created_at_utc"] = "2000-01-01T00:00:00+00:00"
        receipt.write_text(json.dumps(value) + "\n", encoding="utf-8")
        receipt.chmod(0o600)
        # A crash/restart of the same wrapper reuses the immutable snapshot.
        control_plane.prepare_control_plane(
            release=self.release,
            control_dir=self.control,
            layout=self.layout,
            runner=self.runner,
        )
        active = {"caddy.service": True, "contentcrew.service": True}

        def runner(command):
            command = list(command)
            if command[:2] == ["systemctl", "stop"] and command[2] in active:
                active[command[2]] = False
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if (
                command[:3] == ["systemctl", "is-active", "--quiet"]
                and command[3] in active
            ):
                return SimpleNamespace(
                    returncode=0 if active[command[3]] else 3,
                    stdout="",
                    stderr="",
                )
            return self.runner(command)

        report = control_plane.reconcile_failed_upgrade(
            control_dir=self.control,
            release_id=self.release.name,
            layout=self.layout,
            runner=runner,
        )
        self.assertEqual("retained_incomplete", report["status"])
        self.assertFalse(any(active.values()))

    def test_release_evidence_creation_is_idempotently_rechecked_on_resume(self):
        incoming = self.control / "incoming"
        incoming.mkdir(mode=0o700)
        evidence = incoming / self.release.name
        evidence.mkdir(mode=0o700)
        archive = evidence / f"{self.release.name}.tar.gz"
        receipt = evidence / f"{self.release.name}.receipt.json"
        artifact = evidence / "release-artifact-attestation.json"
        materialized = evidence / "materialized-release-attestation.json"
        for path in (archive, receipt, artifact):
            path.write_text("fixture\n", encoding="utf-8")
        artifact.chmod(0o600)
        state = self.base / "state"
        state.mkdir()
        identity = {
            "manifest_sha256": "1" * 64,
            "payload_tree_sha256": "2" * 64,
            "release_id": self.release.name,
            "schema_version": 47,
            "source_tree_sha256": "3" * 64,
        }
        artifact_report = {
            **identity,
            "archive_sha256": "4" * 64,
        }
        materialized_report = {
            **identity,
            "artifact_archive_sha256": "4" * 64,
            "artifact_attestation_sha256": "5" * 64,
            "artifact_receipt_sha256": "6" * 64,
            "ok": True,
        }

        def create_materialized(**kwargs):
            Path(kwargs["attestation"]).write_text(
                json.dumps(materialized_report) + "\n",
                encoding="utf-8",
            )
            Path(kwargs["attestation"]).chmod(0o600)
            return materialized_report

        with (
            patch(
                "deploy.verify_release.verify_release_artifact",
                return_value=artifact_report,
            ) as verify_artifact,
            patch(
                "deploy.verify_release.verify_materialized_release",
                side_effect=create_materialized,
            ) as create_proof,
            patch(
                "deploy.verify_release.verify_materialized_attestation",
                return_value=materialized_report,
            ) as verify_proof,
        ):
            first = control_plane.attest_release_evidence(
                release=self.release,
                state=state,
                control_dir=self.control,
                archive=archive,
                receipt=receipt,
                artifact_attestation=artifact,
                materialized_attestation=materialized,
            )
            proof_before = materialized.read_bytes()
            second = control_plane.attest_release_evidence(
                release=self.release,
                state=state,
                control_dir=self.control,
                archive=archive,
                receipt=receipt,
                artifact_attestation=artifact,
                materialized_attestation=materialized,
            )

        self.assertEqual("created", first["status"])
        self.assertEqual("verified_existing", second["status"])
        self.assertEqual(proof_before, materialized.read_bytes())
        self.assertEqual(2, verify_artifact.call_count)
        self.assertEqual(1, create_proof.call_count)
        self.assertEqual(2, verify_proof.call_count)

    def test_bootstrap_runtime_binds_loaded_module_ops_and_launcher(self):
        trusted = self.base / "trusted-ops"
        for relative in control_plane.OPS_FILES:
            path = trusted / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"trusted:{relative}\n", encoding="utf-8")
            path.chmod(0o755 if relative == "deploy/upgrade.sh" else 0o644)
        trusted.chmod(0o755)
        launcher = self.base / "fixed-launcher"
        launcher.write_bytes((trusted / "deploy" / "upgrade.sh").read_bytes())
        launcher.chmod(0o755)

        with patch.object(
            control_plane,
            "__file__",
            str(trusted / "deploy" / "control_plane.py"),
        ):
            report = control_plane.verify_bootstrap_runtime(
                release_id=self.release.name,
                ops_root=trusted,
                launcher=launcher,
            )
            self.assertEqual("trusted", report["status"])
            launcher.write_text("tampered\n", encoding="utf-8")
            launcher.chmod(0o755)
            with self.assertRaisesRegex(
                control_plane.ControlPlaneError, "launcher"
            ):
                control_plane.verify_bootstrap_runtime(
                    release_id=self.release.name,
                    ops_root=trusted,
                    launcher=launcher,
                )


if __name__ == "__main__":
    unittest.main()
