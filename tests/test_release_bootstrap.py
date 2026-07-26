import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy import bootstrap_release


def _canonical(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


class ReleaseBootstrapCase(unittest.TestCase):
    RELEASE_ID = "20260726T170000Z-schema47-r6"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.control = self.base / "control"
        self.bootstrap = self.control / "bootstrap"
        self.incoming = self.control / "incoming" / self.RELEASE_ID
        self.release = self.base / "releases" / self.RELEASE_ID
        self.state = self.base / "state"
        self.wheelhouse_root = self.base / "wheelhouses"
        for directory in (
            self.control,
            self.bootstrap,
            self.control / "incoming",
            self.incoming,
            self.base / "releases",
            self.release,
            self.state,
        ):
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
        self.state.chmod(0o750)
        self.wheelhouse_root.mkdir(mode=0o755)
        (self.release / "venv").mkdir(mode=0o755)
        (self.release / "venv" / "bootstrap-marker").write_bytes(b"clean\n")
        (self.release / "venv" / "bootstrap-marker").chmod(0o644)

        self.entries = []
        self.original_bodies = {}
        for relative in bootstrap_release.OPS_FILES:
            path = self.release / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative == "deploy/upgrade.sh":
                body = (
                    b"#!/bin/sh\n"
                    b'test "$1" = "--release-id"\n'
                    b'test -n "$2"\n'
                    b'exit 0\n'
                )
                mode = 0o755
            elif relative == "deploy/upgrade_release.py":
                body = (
                    b"from app.session_secret import CONFIG_KEY_ENV\n"
                    b"from deploy.backup_db import backup_database\n"
                    b"from deploy.backup_health import check_backup_health\n"
                    b"from deploy.control_plane import verify_bootstrap_runtime\n"
                    b"from deploy.session_secret_env import check_secret_backup\n"
                    b"from deploy.verify_backup import verify_database\n"
                    b"from deploy.verify_release import verify_release_artifact\n"
                )
                mode = 0o644
            else:
                body = f"# fixed op: {relative}\n".encode()
                mode = 0o644
            path.write_bytes(body)
            path.chmod(mode)
            self.original_bodies[relative] = body
            self.entries.append(
                {
                    "mode": f"{mode:04o}",
                    "path": relative,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body),
                    "type": "file",
                }
            )
        self.lock_body = b"demo-runtime==1.0\n"
        lock_path = self.release / "requirements.lock.txt"
        lock_path.write_bytes(self.lock_body)
        lock_path.chmod(0o644)
        self.entries.append(
            {
                "mode": "0644",
                "path": "requirements.lock.txt",
                "sha256": hashlib.sha256(self.lock_body).hexdigest(),
                "size": len(self.lock_body),
                "type": "file",
            }
        )
        self.entries.sort(key=lambda row: row["path"])
        self.payload_tree = hashlib.sha256(_canonical(self.entries)).hexdigest()
        self.source_tree = "1" * 64
        self.manifest = {
            "created_at": "2026-07-26T09:00:00Z",
            "entries": self.entries,
            "format": "paihuo-release-manifest/v1",
            "payload_file_count": len(self.entries),
            "payload_member_count": len(self.entries),
            "payload_tree_sha256": self.payload_tree,
            "release_id": self.RELEASE_ID,
            "schema_source": "app/db.py:LATEST_SCHEMA_VERSION",
            "schema_version": 47,
            "source_date_epoch": 1_785_056_400,
            "source_tree_sha256": self.source_tree,
        }
        manifest_body = _canonical(self.manifest)
        (self.release / "RELEASE-MANIFEST.json").write_bytes(manifest_body)
        (self.release / "RELEASE-MANIFEST.json").chmod(0o644)
        self.manifest_sha = hashlib.sha256(manifest_body).hexdigest()

        self.artifact = {
            "archive_sha256": "2" * 64,
            "archive_size": 123,
            "attested_at": "2026-07-26T09:00:01Z",
            "extract_dir": str(self.release),
            "format": "paihuo-release-attestation/v1",
            "manifest_sha256": self.manifest_sha,
            "member_count": len(self.entries) + 2,
            "ok": True,
            "payload_tree_sha256": self.payload_tree,
            "receipt_sha256": "3" * 64,
            "release_id": self.RELEASE_ID,
            "schema_version": 47,
            "source_tree_sha256": self.source_tree,
        }
        artifact_path = self.incoming / "release-artifact-attestation.json"
        artifact_path.write_bytes(_canonical(self.artifact))
        artifact_path.chmod(0o600)
        self.materialized = {
            "artifact_archive_sha256": self.artifact["archive_sha256"],
            "artifact_attestation_sha256": hashlib.sha256(
                artifact_path.read_bytes()
            ).hexdigest(),
            "artifact_receipt_sha256": self.artifact["receipt_sha256"],
            "attested_at": "2026-07-26T09:00:02Z",
            "data_target": str(self.state),
            "format": "paihuo-materialized-release-attestation/v1",
            "manifest_sha256": self.manifest_sha,
            "materialized_release": str(self.release),
            "ok": True,
            "payload_tree_sha256": self.payload_tree,
            "release_id": self.RELEASE_ID,
            "schema_version": 47,
            "source_tree_sha256": self.source_tree,
        }
        materialized_path = (
            self.incoming / "materialized-release-attestation.json"
        )
        materialized_path.write_bytes(_canonical(self.materialized))
        materialized_path.chmod(0o600)
        for name in (
            f"{self.RELEASE_ID}.tar.gz",
            f"{self.RELEASE_ID}.receipt.json",
        ):
            path = self.incoming / name
            path.write_bytes(b"fixture")
            path.chmod(0o600)

        verifier = self.bootstrap / "verify_release.py"
        verifier.write_text(
            "import json,sys\n"
            f"artifact={self.artifact!r}\n"
            f"materialized={self.materialized!r}\n"
            "print(json.dumps(artifact if '--archive' in sys.argv else materialized,"
            "sort_keys=True))\n",
            encoding="utf-8",
        )
        verifier.chmod(0o600)
        trusted_bootstrap = self.bootstrap / "bootstrap_release.py"
        trusted_bootstrap.write_text("# trusted fixture\n", encoding="utf-8")
        trusted_bootstrap.chmod(0o600)
        sealed_wheelhouse = self.wheelhouse_root / self.RELEASE_ID
        sealed_wheelhouse.mkdir(mode=0o700)
        wheel_record = {
            "filename": "demo_runtime-1.0-py3-none-any.whl",
            "name": "demo-runtime",
            "sha256": "4" * 64,
            "size": 42,
            "version": "1.0",
        }
        wheel_attestation = {
            "format": "paihuo-wheelhouse-attestation/v1",
            "hashed_lock_sha256": "5" * 64,
            "requirements_count": 1,
            "source_lock_sha256": hashlib.sha256(
                self.lock_body
            ).hexdigest(),
            "target_platform": "linux_x86_64",
            "target_python": "cp312",
            "total_bytes": 42,
            "wheel_count": 1,
            "wheel_set_sha256": "6" * 64,
            "wheels": [wheel_record],
        }
        wheel_attestation_path = (
            sealed_wheelhouse / "wheelhouse-attestation.json"
        )
        wheel_attestation_path.write_bytes(_canonical(wheel_attestation))
        wheel_attestation_path.chmod(0o444)
        sealed_wheelhouse.chmod(0o555)
        self.wheelhouse_report = {
            **wheel_attestation,
            "status": "verified",
        }
        trusted_wheelhouse = self.bootstrap / "wheelhouse.py"
        trusted_wheelhouse.write_text(
            "import json\n"
            f"print(json.dumps({self.wheelhouse_report!r}, sort_keys=True))\n",
            encoding="utf-8",
        )
        trusted_wheelhouse.chmod(0o600)
        self.layout = bootstrap_release.Layout(
            control_root=self.control,
            releases_root=self.base / "releases",
            state_dir=self.state,
            trusted_verifier=verifier,
            trusted_bootstrap=trusted_bootstrap,
            trusted_wheelhouse=trusted_wheelhouse,
            wheelhouse_root=self.wheelhouse_root,
            python=Path(sys.executable),
        )

    def tearDown(self):
        self.tmp.cleanup()

    @property
    def stage(self):
        return (
            self.bootstrap
            / "releases"
            / self.RELEASE_ID
        )

    def test_prepare_stage_and_read_only_check_are_idempotent(self):
        created = bootstrap_release.prepare_stage(
            self.RELEASE_ID,
            layout=self.layout,
        )
        self.assertEqual("created", created["status"])
        self.assertEqual(0o700, self.stage.stat().st_mode & 0o777)
        self.assertEqual(0o600, (self.stage / "attestation.json").stat().st_mode & 0o777)
        self.assertEqual(
            (self.release / "deploy" / "upgrade.sh").read_bytes(),
            (self.stage / "paihuo-upgrade").read_bytes(),
        )
        for relative in bootstrap_release.OPS_FILES:
            self.assertTrue((self.stage / "ops" / relative).is_file())

        checked = bootstrap_release.check_stage(
            self.RELEASE_ID,
            layout=self.layout,
        )
        self.assertEqual("verified_existing", checked["status"])
        before = (self.stage / "attestation.json").read_bytes()
        repeated = bootstrap_release.prepare_stage(
            self.RELEASE_ID,
            layout=self.layout,
        )
        self.assertEqual("verified_existing", repeated["status"])
        self.assertEqual(before, (self.stage / "attestation.json").read_bytes())

    def test_candidate_script_or_venv_tamper_fails_without_stage(self):
        script = self.release / "deploy" / "upgrade_release.py"
        script.write_text("# candidate tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(
            bootstrap_release.BootstrapError,
            "manifest|content",
        ):
            bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        self.assertFalse(self.stage.exists())

        script.write_bytes(self.original_bodies["deploy/upgrade_release.py"])
        script.chmod(0o644)
        # Recreate the fixture cleanly for the independent venv boundary.
        shutil.rmtree(self.release / "venv")
        (self.release / "venv").symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(
            bootstrap_release.BootstrapError,
            "venv",
        ):
            bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        self.assertFalse(self.stage.exists())

    def test_symlink_mode_and_open_swap_are_rejected(self):
        target = self.release / "deploy" / "backup_db.py"
        body = target.read_bytes()
        target.unlink()
        target.symlink_to(self.release / "deploy" / "backup_health.py")
        with self.assertRaisesRegex(bootstrap_release.BootstrapError, "regular|symlink"):
            bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        target.unlink()
        target.write_bytes(body)
        target.chmod(0o666)
        with self.assertRaisesRegex(bootstrap_release.BootstrapError, "mode"):
            bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)

        target.chmod(0o644)
        replacement = target.with_name("replacement.py")
        replacement.write_bytes(body)
        replacement.chmod(0o644)
        original_open = bootstrap_release.os.open
        swapped = {"done": False}

        def swap(path, flags, *args, **kwargs):
            if not swapped["done"] and Path(path).name == target.name:
                os.replace(replacement, target)
                swapped["done"] = True
            return original_open(path, flags, *args, **kwargs)

        with (
            patch.object(bootstrap_release.os, "open", side_effect=swap),
            self.assertRaisesRegex(bootstrap_release.BootstrapError, "changed"),
        ):
            bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        self.assertFalse(self.stage.exists())

    def test_untrusted_verifier_or_publish_failure_changes_nothing(self):
        self.layout.trusted_verifier.chmod(0o644)
        with self.assertRaisesRegex(bootstrap_release.BootstrapError, "trusted verifier"):
            bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        self.assertFalse(self.stage.exists())
        self.layout.trusted_verifier.chmod(0o600)

        sentinel = self.bootstrap / "legacy-fixed-missing"
        original_replace = bootstrap_release.os.replace

        def fail_publish(source, destination):
            if Path(destination).resolve(strict=False) == self.stage.resolve(
                strict=False
            ):
                raise OSError("publish fault")
            return original_replace(source, destination)

        with (
            patch.object(
                bootstrap_release.os,
                "replace",
                side_effect=fail_publish,
            ),
            self.assertRaises(OSError),
        ):
            bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        self.assertFalse(self.stage.exists())
        self.assertFalse(sentinel.exists())
        self.assertFalse(
            any(
                child.name.startswith(f".{self.RELEASE_ID}.stage-")
                for child in (self.bootstrap / "releases").iterdir()
            )
        )

    def test_check_stage_detects_ops_launcher_and_attestation_tamper(self):
        bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        staged_op = self.stage / "ops" / "deploy" / "backup_db.py"
        staged_op.write_text("# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(bootstrap_release.BootstrapError, "content"):
            bootstrap_release.check_stage(self.RELEASE_ID, layout=self.layout)

        self.stage.rename(self.stage.with_name(self.stage.name + "-bad"))
        # A fresh stage gives each remaining proof test an independent baseline.
        bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        (self.stage / "paihuo-upgrade").chmod(0o700)
        with self.assertRaisesRegex(bootstrap_release.BootstrapError, "launcher|mode"):
            bootstrap_release.check_stage(self.RELEASE_ID, layout=self.layout)

    def test_launch_checks_before_fixed_exec_and_uses_minimal_environment(self):
        bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        called = {}

        def capture(path, argv, env):
            called.update(path=path, argv=argv, env=env)
            raise RuntimeError("exec captured")

        with self.assertRaisesRegex(RuntimeError, "captured"):
            bootstrap_release.launch_stage(
                self.RELEASE_ID,
                layout=self.layout,
                execve=capture,
            )
        self.assertEqual(
            str((self.stage / "paihuo-upgrade").resolve()),
            called["path"],
        )
        self.assertEqual(
            [
                str((self.stage / "paihuo-upgrade").resolve()),
                "--release-id",
                self.RELEASE_ID,
            ],
            called["argv"],
        )
        self.assertEqual("1", called["env"]["PAIHUO_BOOTSTRAP_LAUNCHED"])
        self.assertEqual(
            str((self.stage / "ops").resolve()),
            called["env"]["PYTHONPATH"],
        )
        self.assertNotIn("HOME", called["env"])

        (self.stage / "paihuo-upgrade").write_text("# tampered\n")
        called.clear()
        with self.assertRaises(bootstrap_release.BootstrapError):
            bootstrap_release.launch_stage(
                self.RELEASE_ID,
                layout=self.layout,
                execve=capture,
            )
        self.assertEqual({}, called)

    def test_launch_rejects_ordinary_venv_file_tamper_before_exec(self):
        bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        called = {}

        def capture(path, argv, env):
            called.update(path=path, argv=argv, env=env)
            raise RuntimeError("must not execute")

        marker = self.release / "venv" / "bootstrap-marker"
        marker.write_bytes(b"tampered\n")
        with self.assertRaisesRegex(
            bootstrap_release.BootstrapError,
            "attestation|venv",
        ):
            bootstrap_release.launch_stage(
                self.RELEASE_ID,
                layout=self.layout,
                execve=capture,
            )
        self.assertEqual({}, called)

    def test_wheelhouse_lock_and_attestation_are_bound_before_exec(self):
        bad_report = {
            **self.wheelhouse_report,
            "source_lock_sha256": "0" * 64,
        }
        with (
            patch.object(
                bootstrap_release,
                "_run_wheelhouse_verifier",
                return_value=bad_report,
            ),
            self.assertRaisesRegex(
                bootstrap_release.BootstrapError,
                "dependency lock",
            ),
        ):
            bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        self.assertFalse(self.stage.exists())

        bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        attestation = (
            self.wheelhouse_root
            / self.RELEASE_ID
            / "wheelhouse-attestation.json"
        )
        sealed = attestation.parent
        sealed.chmod(0o755)
        attestation.chmod(0o644)
        value = json.loads(attestation.read_text(encoding="utf-8"))
        value["wheel_set_sha256"] = "f" * 64
        attestation.write_bytes(_canonical(value))
        attestation.chmod(0o444)
        sealed.chmod(0o555)
        called = {}

        def capture(path, argv, env):
            called.update(path=path, argv=argv, env=env)
            raise RuntimeError("must not execute")

        with self.assertRaisesRegex(
            bootstrap_release.BootstrapError,
            "wheelhouse",
        ):
            bootstrap_release.launch_stage(
                self.RELEASE_ID,
                layout=self.layout,
                execve=capture,
            )
        self.assertEqual({}, called)

    def test_unsafe_candidate_ancestor_is_rejected(self):
        candidate_parent = self.release / "deploy"
        candidate_parent.chmod(0o777)
        with self.assertRaisesRegex(
            bootstrap_release.BootstrapError,
            "ancestor",
        ):
            bootstrap_release.prepare_stage(self.RELEASE_ID, layout=self.layout)
        self.assertFalse(self.stage.exists())

    def test_service_owned_state_directory_is_valid_in_root_mode(self):
        service_uid = 4242
        service_gid = 4243
        real_uid = os.geteuid()
        original_lstat = os.lstat
        service_paths = {
            self.state.absolute(),
            self.state.resolve(),
            self.state.parent.absolute(),
            self.state.parent.resolve(),
        }

        def production_lstat(path):
            metadata = original_lstat(path)
            absolute = Path(path).absolute()
            if absolute in service_paths:
                uid, gid = service_uid, service_gid
            elif metadata.st_uid == real_uid:
                uid, gid = 0, 0
            else:
                uid, gid = metadata.st_uid, metadata.st_gid
            return SimpleNamespace(
                st_ctime_ns=metadata.st_ctime_ns,
                st_dev=metadata.st_dev,
                st_gid=gid,
                st_ino=metadata.st_ino,
                st_mode=metadata.st_mode,
                st_mtime_ns=metadata.st_mtime_ns,
                st_nlink=metadata.st_nlink,
                st_size=metadata.st_size,
                st_uid=uid,
            )

        with (
            patch.object(bootstrap_release.os, "geteuid", return_value=0),
            patch.object(
                bootstrap_release.os,
                "lstat",
                side_effect=production_lstat,
            ),
            patch.object(bootstrap_release.pwd, "getpwnam") as get_user,
            patch.object(bootstrap_release.grp, "getgrnam") as get_group,
        ):
            get_user.return_value = SimpleNamespace(
                pw_uid=service_uid,
                pw_gid=service_gid,
            )
            get_group.return_value = SimpleNamespace(gr_gid=service_gid)
            self.assertEqual(
                self.state.resolve(),
                bootstrap_release._assert_state_dir(
                    self.state,
                    label="state directory",
                ),
            )

            get_group.return_value = SimpleNamespace(gr_gid=service_gid + 1)
            with self.assertRaisesRegex(
                bootstrap_release.BootstrapError,
                "disagree",
            ):
                bootstrap_release._assert_state_dir(
                    self.state,
                    label="state directory",
                )

    def test_cli_requires_root_and_exact_fixed_helper_inode(self):
        with (
            patch.object(bootstrap_release.os, "geteuid", return_value=1234),
            self.assertRaisesRegex(bootstrap_release.BootstrapError, "root"),
        ):
            bootstrap_release._require_trusted_cli_entry(self.layout)

        uid = os.geteuid()
        gid = os.getegid()
        candidate = self.base / "candidate-bootstrap.py"
        candidate.write_bytes(self.layout.trusted_bootstrap.read_bytes())
        candidate.chmod(0o600)
        with (
            patch.object(bootstrap_release.os, "geteuid", return_value=0),
            patch.object(bootstrap_release, "_owner_uid", return_value=uid),
            patch.object(bootstrap_release, "_owner_gid", return_value=gid),
            patch.object(bootstrap_release, "__file__", str(candidate)),
            self.assertRaisesRegex(
                bootstrap_release.BootstrapError,
                "not the trusted fixed tool",
            ),
        ):
            bootstrap_release._require_trusted_cli_entry(self.layout)

        with (
            patch.object(bootstrap_release.os, "geteuid", return_value=0),
            patch.object(bootstrap_release, "_owner_uid", return_value=uid),
            patch.object(bootstrap_release, "_owner_gid", return_value=gid),
            patch.object(
                bootstrap_release,
                "__file__",
                str(self.layout.trusted_bootstrap),
            ),
        ):
            self.assertEqual(
                self.layout.trusted_bootstrap.resolve(),
                bootstrap_release._require_trusted_cli_entry(self.layout),
            )

    def test_dependency_closure_and_cli_are_fail_closed(self):
        report = bootstrap_release.validate_dependency_closure(
            self.release,
            bootstrap_release.OPS_FILES,
        )
        self.assertTrue(report["ok"])
        bad = self.release / "deploy" / "upgrade_release.py"
        bad.write_text("from deploy.not_allowed import danger\n", encoding="utf-8")
        with self.assertRaisesRegex(bootstrap_release.BootstrapError, "dependency"):
            bootstrap_release.validate_dependency_closure(
                self.release,
                bootstrap_release.OPS_FILES,
            )

        parser = bootstrap_release._parser()
        options = [
            option
            for action in parser._actions
            for option in action.option_strings
        ]
        self.assertEqual(len(options), len(set(options)))
        with self.assertRaises(SystemExit):
            parser.parse_args(["--check-stage", "--release-id", "../escape"])


if __name__ == "__main__":
    unittest.main()
