import os
import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from deploy import backup_health, failure_alert, preflight, start_guard


ROOT = Path(__file__).resolve().parents[1]


class DeploymentHardeningContractCase(unittest.TestCase):
    def _preflight_fixture(self, base: Path, *, legacy: bool = False):
        root = base / "current"
        state = base / "state"
        venv = base / "venv"
        (root / "app").mkdir(parents=True)
        (root / "static").mkdir()
        (root / "config" / "departments").mkdir(parents=True)
        (root / "config" / "industry_knowledge").mkdir(parents=True)
        (state / "departments").mkdir(parents=True)
        (state / "public").mkdir()
        (state / "assets" / "avatar").mkdir(parents=True)
        (state / "llmwork").mkdir()
        (venv / "bin").mkdir(parents=True)
        (root / "app" / "main.py").write_text("# fixture")
        (root / "static" / "index.html").write_text("fixture")
        (root / "RELEASE-MANIFEST.json").write_text(
            '{"format":"paihuo-release-manifest/v1"}'
        )
        (root / "config" / "departments" / "content.json").write_text(
            '{"key":"content","employees":[]}'
        )
        (
            root / "config" / "industry_knowledge" / "content.json"
        ).write_text(
            '{"key":"content","name":"内容行业",'
            '"metrics":[{"name":"完成率","formula":"完成数/任务数"}],'
            '"benchmarks":[],"glossary":[],"practices":[],'
            '"compliance":[],"pitfalls":[]}'
        )
        (root / "config" / "gate_rules.default.json").write_text(
            '{"sensitive_words":[],"notes":"fixture"}'
        )
        (root / "run.sh").write_text("#!/bin/sh")
        (root / "run.sh").chmod(0o755)
        (state / "gate_rules.json").write_text(
            '{"sensitive_words":[],"notes":"fixture"}'
        )
        (state / "departments" / "content.json").write_text("{}")
        for binary in (
            venv / "bin" / "python",
            venv / "bin" / "yt-dlp",
            venv / "bin" / "ffmpeg",
        ):
            binary.write_text("#!/bin/sh")
            binary.chmod(0o755)
        (root / "data").symlink_to(state, target_is_directory=True)
        connection = sqlite3.connect(state / "contentcrew.db")
        connection.executescript(
            "CREATE TABLE tenants("
            "id INTEGER PRIMARY KEY,name TEXT NOT NULL,enabled INTEGER DEFAULT 1,"
            "created_at REAL,updated_at REAL);"
            "CREATE TABLE users("
            "id INTEGER PRIMARY KEY,tenant_id INTEGER NOT NULL,"
            "username TEXT NOT NULL,password_hash TEXT NOT NULL,"
            "role TEXT NOT NULL,modules_json TEXT,enabled INTEGER DEFAULT 1,"
            "created_at REAL,updated_at REAL);"
            "CREATE TABLE job("
            "id INTEGER PRIMARY KEY,brief_json TEXT NOT NULL,"
            "profile_id INTEGER,mode TEXT NOT NULL DEFAULT 'copilot',"
            "status TEXT NOT NULL DEFAULT 'running',"
            "current_idx INTEGER NOT NULL DEFAULT 0,gate_json TEXT,"
            "cost_usd REAL DEFAULT 0,tokens INTEGER DEFAULT 0,"
            "created_at REAL,updated_at REAL);"
        )
        if not legacy:
            connection.executescript(
                "CREATE TABLE schema_version("
                "version INTEGER PRIMARY KEY,name TEXT,applied_at REAL);"
                "INSERT INTO schema_version VALUES(1,'fixture',0);"
            )
        connection.close()
        return root, state, venv

    def _check_fixture(self, root: Path, state: Path, venv: Path):
        with patch.dict(
            os.environ,
            {
                "CONTENTCREW_DB_PATH": str(state / "contentcrew.db"),
                "CONTENTCREW_FFMPEG_PATH": str(venv / "bin" / "ffmpeg"),
                "CONTENTCREW_PUBLIC_DIR": str(state / "public"),
            },
        ):
            return preflight.check_layout(
                root, state, venv, check_runtime=False
            )

    def test_application_binds_loopback_and_trusts_only_local_proxy_by_default(self):
        run = (ROOT / "run.sh").read_text()
        self.assertIn("CONTENTCREW_HOST:-127.0.0.1", run)
        self.assertIn("CONTENTCREW_FORWARDED_ALLOW_IPS:-127.0.0.1", run)
        self.assertNotIn("--host 0.0.0.0", run)

    def test_caddy_limits_body_overwrites_forwarded_chain_and_has_health_checks(self):
        caddy = (ROOT / "deploy" / "Caddyfile").read_text()
        for contract in (
            "max_size 40MB",
            "encode zstd gzip",
            "X-Content-Type-Options",
            "header_up X-Forwarded-For {remote_host}",
            "header_up X-Forwarded-Proto {scheme}",
            "health_uri /healthz",
            "response_header_timeout 10m",
            "flush_interval -1",
        ):
            self.assertIn(contract, caddy)

    def test_systemd_runs_as_dedicated_user_with_sandbox_and_limits(self):
        unit = (ROOT / "deploy" / "contentcrew.service").read_text()
        for contract in (
            "User=paihuo",
            "CONTENTCREW_PYTHON=/srv/paihuo/current/venv/bin/python",
            "CONTENTCREW_DB_PATH=/var/lib/paihuo/data/contentcrew.db",
            "CONTENTCREW_YTDLP_PATH=/srv/paihuo/current/venv/bin/yt-dlp",
            "CONTENTCREW_FFMPEG_PATH=/usr/bin/ffmpeg",
            "CONTENTCREW_CLAUDE_PATH=/srv/paihuo/bin/claude",
            "CONTENTCREW_PUBLIC_DIR=/srv/paihuo-pub",
            "EnvironmentFile=/etc/paihuo/paihuo.env",
            "CONTENTCREW_REQUIRE_SESSION_SECRET=1",
            "PLAYWRIGHT_BROWSERS_PATH=/var/lib/paihuo/ms-playwright",
            "ExecStartPre=/srv/paihuo/current/venv/bin/python -m deploy.preflight",
            "ExecStartPre=+/usr/bin/env PYTHONPATH=/usr/local/lib/paihuo-ops "
            "/usr/bin/python3 -m deploy.session_secret_env --check",
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "PrivateTmp=yes",
            "CapabilityBoundingSet=",
            "MemoryMax=3G",
            "TasksMax=512",
            "StartLimitBurst=12",
            "OnFailure=paihuo-failure-alert@%n.service",
        ):
            self.assertIn(contract, unit)
        self.assertNotIn("User=root", unit)
        self.assertIn("/srv/paihuo-pub", (ROOT / "deploy" / "Caddyfile").read_text())
        caddy_drop_in = (
            ROOT / "deploy" / "caddy-paihuo-guard.conf"
        ).read_text()
        self.assertIn("SupplementaryGroups=paihuo-public", caddy_drop_in)
        self.assertFalse((ROOT / "deploy" / "contentcrew-public.service").exists())
        self.assertFalse((ROOT / "deploy" / "contentcrew-tunnel.service").exists())

    def test_backup_unit_uses_fixed_root_owned_ops_and_runs_hourly(self):
        unit = (ROOT / "deploy" / "paihuo-backup.service").read_text()
        timer = (ROOT / "deploy" / "paihuo-backup.timer").read_text()
        for contract in (
            "User=root",
            "Group=root",
            "WorkingDirectory=/usr/local/lib/paihuo-ops",
            "/usr/bin/python3",
            "--database /var/lib/paihuo/data/contentcrew.db",
            "--backup-dir /var/backups/paihuo",
            "OnFailure=paihuo-failure-alert@%n.service",
            "-m deploy.backup_health",
            "--success-attestation",
            "/var/lib/paihuo-upgrade/latest-periodic-backup.json",
            "RequiresMountsFor=/var/backups/paihuo /var/lib/paihuo-upgrade",
            "start_guard.py --mode backup",
        ):
            self.assertIn(contract, unit)
        self.assertNotIn("/root/contentcrew", unit)
        self.assertNotIn("/srv/paihuo/current/venv/bin/python", unit)
        self.assertIn("OnCalendar=hourly", timer)

    def test_boot_guards_block_uncommitted_app_proxy_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / "control"
            control.mkdir(mode=0o700)
            permit = root / "permit.json"
            receipt_path = control / "upgrade-fixture.json"
            receipt = {"status": "in_progress", "phase": "candidate_selected"}
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            receipt_path.chmod(0o600)

            for mode in ("application", "proxy", "backup"):
                with self.assertRaises(start_guard.GuardError):
                    start_guard.check_start(
                        control_dir=control,
                        permit=permit,
                        mode=mode,
                    )

            permit.write_text(
                json.dumps({
                    "receipt_path": str(receipt_path),
                    "issued_at_utc": datetime.now(
                        tz=timezone.utc
                    ).isoformat(),
                }),
                encoding="utf-8",
            )
            permit.chmod(0o600)
            self.assertTrue(start_guard.check_start(
                control_dir=control,
                permit=permit,
                mode="application",
            )["ok"])
            with self.assertRaises(start_guard.GuardError):
                start_guard.check_start(
                    control_dir=control,
                    permit=permit,
                    mode="proxy",
                )

            receipt["status"] = "cutover_committed"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            receipt_path.chmod(0o600)
            self.assertTrue(start_guard.check_start(
                control_dir=control,
                permit=permit,
                mode="proxy",
            )["ok"])
            self.assertTrue(start_guard.check_start(
                control_dir=control,
                permit=root / "missing-permit",
                mode="backup",
            )["ok"])

    def test_systemd_guards_run_before_candidate_preflight(self):
        app_unit = (ROOT / "deploy" / "contentcrew.service").read_text()
        guard = "start_guard.py --mode application"
        preflight_call = (
            "/srv/paihuo/current/venv/bin/python -m deploy.preflight"
        )
        self.assertIn(guard, app_unit)
        secret_check = "-m deploy.session_secret_env --check"
        self.assertIn(secret_check, app_unit)
        self.assertLess(app_unit.index(guard), app_unit.index(secret_check))
        self.assertLess(app_unit.index(guard), app_unit.index(preflight_call))
        proxy_dropin = (
            ROOT / "deploy" / "caddy-paihuo-guard.conf"
        ).read_text()
        self.assertIn("start_guard.py --mode proxy", proxy_dropin)

    def test_backup_cli_defaults_to_root_owned_backup_directory(self):
        source = (ROOT / "deploy" / "backup_db.py").read_text()
        self.assertNotIn('default="/root/contentcrew', source)
        self.assertIn("/var/lib/paihuo/data/contentcrew.db", source)
        self.assertIn("/var/backups/paihuo", source)
        self.assertIn("--success-attestation", source)

    def test_upgrade_entry_enforces_checkpoint_smoke_and_rollback(self):
        source = (ROOT / "deploy" / "upgrade_release.py").read_text()
        wrapper = ROOT / "deploy" / "upgrade.sh"
        guide = (ROOT / "deploy" / "DEPLOYMENT.md").read_text()
        for contract in (
            "create_upgrade_checkpoint",
            "run_restore_drill=True",
            "final-stopped",
            'control_dir or "/var/lib/paihuo-upgrade"',
            "_atomic_symlink",
            "_restore_live_database",
            "_authenticated_smoke",
            "candidate_may_have_run",
            "CONTENTCREW_QUIESCENT=1",
            "rollback_failed",
        ):
            self.assertIn(contract, source)
        self.assertTrue(os.access(wrapper, os.X_OK))
        wrapper_source = wrapper.read_text()
        self.assertIn("-m deploy.session_secret_env", wrapper_source)
        self.assertIn("/etc/paihuo/paihuo.env", wrapper_source)
        self.assertIn("--backup-to", wrapper_source)
        self.assertIn("--check-backup", wrapper_source)
        self.assertIn(
            "$CONTROL_DIR/credentials/paihuo-env-$RELEASE_ID.backup",
            wrapper_source,
        )
        self.assertLess(
            wrapper_source.index("--check-backup"),
            wrapper_source.index("-m deploy.upgrade_release"),
        )
        self.assertIn("常规升级唯一入口", guide)
        self.assertIn("--adopt-venv", guide)
        self.assertIn("--require-hashes", guide)
        self.assertIn("--no-index", guide)
        self.assertIn("--no-deps", guide)
        self.assertNotIn("chown -R", guide)
        self.assertNotIn("chmod -R", guide)
        self.assertNotIn("cp -aL", guide)
        self.assertIn("/var/lib/paihuo-upgrade", guide)

    def test_upgrade_wrapper_orders_locked_control_and_fixed_secret_gates(self):
        # The production wrapper deliberately uses Linux root-only absolute
        # tools (install/stat/flock and the system Python).  Do not execute it
        # on the macOS test host; the transactional behavior is exercised via
        # test_control_plane with injected systemd/Caddy runners.
        source = (ROOT / "deploy" / "upgrade.sh").read_text(encoding="utf-8")
        bootstrap_tool = source.index(
            '/usr/bin/python3 -I "$BOOTSTRAP_TOOL"'
        )
        lock = source.index("/usr/bin/flock -n 9")
        bootstrap_check = source.index(
            '"${TRUSTED_PYTHON[@]}" -m deploy.control_plane bootstrap-check'
        )
        gate = source.index(
            '"${TRUSTED_PYTHON[@]}" -m deploy.control_plane gate'
        )
        begin = source.index(
            '"${TRUSTED_PYTHON[@]}" -m deploy.control_plane begin'
        )
        armed = source.index("CONTROL_PREPARED=1")
        prepare = source.index(
            '"${TRUSTED_PYTHON[@]}" -m deploy.control_plane prepare'
        )
        release_evidence = source.index(
            '"${TRUSTED_PYTHON[@]}" -m deploy.control_plane release-evidence'
        )
        ensure = source.index(
            '"${TRUSTED_PYTHON[@]}" -m deploy.session_secret_env \\\n  --path'
        )
        backup = source.index("--backup-to")
        check = source.index("--check-backup")
        state_machine = source.index(
            '"${TRUSTED_PYTHON[@]}" -m deploy.upgrade_release'
        )
        self.assertLess(bootstrap_tool, bootstrap_check)
        self.assertLess(lock, armed)
        self.assertLess(lock, bootstrap_check)
        self.assertLess(bootstrap_check, release_evidence)
        self.assertLess(release_evidence, gate)
        self.assertLess(gate, begin)
        self.assertLess(begin, armed)
        self.assertLess(armed, prepare)
        self.assertLess(prepare, ensure)
        self.assertLess(ensure, backup)
        self.assertLess(backup, check)
        self.assertLess(check, state_machine)
        self.assertIn("trap on_exit EXIT", source)
        self.assertIn("trap on_signal INT TERM HUP", source)
        self.assertIn('"PYTHONPATH=$TRUSTED_OPS"', source)
        self.assertIn('FIXED_LAUNCHER="/usr/local/sbin/paihuo-upgrade"', source)
        self.assertIn(
            'BOOTSTRAP_TOOL="$CONTROL_DIR/bootstrap/bootstrap_release.py"',
            source,
        )
        self.assertIn('PAIHUO_BOOTSTRAP_LAUNCHED:-', source)
        self.assertIn('--check-stage', source)
        self.assertIn('/usr/bin/python3 -I "$BOOTSTRAP_TOOL"', source)
        self.assertIn('/usr/bin/env -i', source)
        self.assertIn('"PYTHONDONTWRITEBYTECODE=1"', source)
        self.assertNotIn('RELEASE_ROOT/venv/bin/python', source)
        self.assertNotIn('"$PYTHON" -m', source)
        self.assertIn(
            "--control-plane-attestation \"$CONTROL_ATTESTATION\"", source
        )
        for binding in (
            '--archive "$RELEASE_ARCHIVE"',
            '--receipt "$BUILD_RECEIPT"',
            '--artifact-attestation "$ARTIFACT_ATTESTATION"',
            '--materialized-attestation "$MATERIALIZED_ATTESTATION"',
            '--release-archive "$RELEASE_ARCHIVE"',
            '--release-build-receipt "$BUILD_RECEIPT"',
            '--release-artifact-attestation "$ARTIFACT_ATTESTATION"',
            '--materialized-release-attestation "$MATERIALIZED_ATTESTATION"',
            '--bootstrap-stage-attestation "$BOOTSTRAP_ATTESTATION"',
        ):
            self.assertIn(binding, source)
        self.assertIn(
            'ARTIFACT_DIR="$CONTROL_DIR/incoming/$RELEASE_ID"', source
        )
        self.assertIn("-m deploy.control_plane reconcile", source)
        self.assertIn("-m deploy.control_plane finish", source)
        self.assertIn("--control-dir \"$CONTROL_DIR\"", source)
        self.assertIn('UPGRADE_ARGS=()', source)
        self.assertNotIn('CONTENTCREW_CONTROL_DIR:-', source)

    def test_wrapper_double_reconcile_failure_never_finishes_marker(self):
        source = (ROOT / "deploy" / "upgrade.sh").read_text(encoding="utf-8")
        start = source.index("restore_control_plane() {")
        end = source.index("\n}\n\non_exit()", start) + 3
        function = source[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            control = base / "control"
            attestation = (
                control / "control-plane" / "fixture" / "attestation.json"
            )
            attestation.parent.mkdir(parents=True)
            attestation.write_text("{}\n", encoding="utf-8")
            marker = control / "wrapper-transaction.json"
            marker.write_text("{}\n", encoding="utf-8")
            log = base / "calls.log"
            fake_python = base / "candidate-python"
            fake_python.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LOG\"\nexit 1\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            script = (
                "set -u\n"
                + function
                + "\nWRAPPER_LOCK_HELD=1\n"
                "WRAPPER_TRANSACTION_STARTED=1\n"
                "CONTROL_PREPARED=1\n"
                "UPGRADE_SUCCEEDED=0\n"
                f"CONTROL_ATTESTATION={attestation!s}\n"
                f"CONTROL_DIR={control!s}\n"
                "RELEASE_ID=fixture\n"
                f"TRUSTED_PYTHON=({fake_python!s})\n"
                "if restore_control_plane; then exit 0; else exit 7; fi\n"
            )
            completed = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd="/",
                env={**os.environ, "FAKE_LOG": str(log)},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(7, completed.returncode)
            self.assertTrue(marker.exists())
            calls = log.read_text(encoding="utf-8")
            self.assertIn("deploy.control_plane reconcile", calls)
            self.assertNotIn("deploy.control_plane finish", calls)

    def test_wrapper_without_lock_ownership_cannot_touch_active_marker(self):
        source = (ROOT / "deploy" / "upgrade.sh").read_text(encoding="utf-8")
        start = source.index("restore_control_plane() {")
        end = source.index("\n}\n\non_exit()", start) + 3
        function = source[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            control = base / "control"
            control.mkdir()
            marker = control / "wrapper-transaction.json"
            marker.write_text('{"status":"active"}\n', encoding="utf-8")
            fake_python = base / "candidate-python"
            fake_python.write_text(
                "#!/bin/sh\nexit 99\n", encoding="utf-8"
            )
            fake_python.chmod(0o755)
            script = (
                "set -u\n"
                + function
                + "\nWRAPPER_LOCK_HELD=0\n"
                "WRAPPER_TRANSACTION_STARTED=0\n"
                "CONTROL_PREPARED=0\n"
                "UPGRADE_SUCCEEDED=0\n"
                f"CONTROL_ATTESTATION={base / 'missing'}\n"
                f"CONTROL_DIR={control}\n"
                "RELEASE_ID=fixture\n"
                f"TRUSTED_PYTHON=({fake_python})\n"
                "restore_control_plane\n"
            )
            completed = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd="/",
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(marker.exists())

    def test_deployment_installs_every_fixed_guard_and_builds_venv_in_place(self):
        guide = (ROOT / "deploy" / "DEPLOYMENT.md").read_text()
        for contract in (
            "useradd --system",
            "--user-group",
            "paihuo-build",
            "paihuo-smoke",
            "deploy/smoke_readonly.py",
            "deploy/start_guard.py",
            "-m deploy.session_secret_env",
            "deploy/contentcrew.service",
            "deploy/caddy-paihuo-guard.conf",
            "/etc/systemd/system/caddy.service.d/10-paihuo-guard.conf",
            '/usr/bin/python3 -m venv --copies "$release/venv"',
            'test -L "$release/venv/lib64"',
            'readlink "$release/venv/lib64"',
            '/usr/bin/unlink "$release/venv/lib64"',
            'test ! -e "$release/venv/lib64"',
            "不得跟随、复制或泛化删除其他链接",
            "--require-hashes",
            "--find-links",
            "--no-index",
            "--no-deps",
            "--adopt-venv",
            'paihuo-build -g paihuo-build -m 0755 "$release/venv"',
            "umask 0022",
            "venv 根必须是 `root:root 0755`",
            "`paihuo` 服务账号可只读",
            "/var/cache/paihuo-wheelhouse/$release_id",
            "/usr/local/sbin/paihuo-upgrade",
            'test "$build_uid" -ne 0',
            'test "$build_gid" -ne 0',
            'test "$build_uid" != "$app_uid"',
            'test "$build_gid" != "$app_gid"',
            'test "$(id -G paihuo-build)" = "$build_gid"',
            "sudo -u paihuo-build test ! -r /var/lib/paihuo/data",
            "sudo -u paihuo-build test ! -r /etc/paihuo/paihuo.env",
            "sudo -u paihuo-build test ! -r /var/lib/paihuo-upgrade",
            "不得自动",
            "而改由固定 launcher 启动",
        ):
            self.assertIn(contract, guide)
        regular_upgrade = guide.split(
            "## 8. r6后唯一入口（常规升级唯一入口）",
            1,
        )[1].split("## 9.", 1)[0]
        self.assertIn("--prepare-stage", regular_upgrade)
        self.assertIn(
            "/usr/local/sbin/paihuo-upgrade",
            regular_upgrade,
        )
        self.assertNotIn('"$release/venv/bin/yt-dlp" --version', guide)
        self.assertNotIn('sudo "$release/venv/bin/python"', guide)
        self.assertNotIn('sudo "$release/venv/bin/pip"', guide)
        self.assertNotIn('find "$release/venv" -type l -delete', guide)
        self.assertNotIn(
            'paihuo-build -g paihuo-build -m 0700 "$release/venv"',
            guide,
        )
        self.assertNotIn(
            "mktemp -d /var/tmp/paihuo-release", guide
        )

    def test_recovery_guide_uses_root_owned_fixed_backup_contract(self):
        guide = (ROOT / "deploy" / "BACKUP_RECOVERY.md").read_text()
        for contract in (
            "/var/backups/paihuo",
            "/var/lib/paihuo-upgrade/latest-periodic-backup.json",
            "PYTHONPATH=/usr/local/lib/paihuo-ops",
            "/usr/bin/python3 -m deploy.verify_backup",
            "systemctl stop paihuo-backup.timer",
            "systemctl stop caddy.service",
        ):
            self.assertIn(contract, guide)
        self.assertNotIn("/var/lib/paihuo/data/backups", guide)
        self.assertNotIn("sudo -u paihuo", guide)

    def test_backup_freshness_monitor_alerts_and_runs_hourly(self):
        unit = (ROOT / "deploy" / "paihuo-backup-health.service").read_text()
        timer = (ROOT / "deploy" / "paihuo-backup-health.timer").read_text()
        for contract in (
            "OnFailure=paihuo-failure-alert@%n.service",
            "-m deploy.backup_health",
            "--max-age-hours 24",
            "User=root",
            "--attestation /var/lib/paihuo-upgrade/latest-periodic-backup.json",
        ):
            self.assertIn(contract, unit)
        self.assertIn("OnCalendar=hourly", timer)
        self.assertIn("Persistent=true", timer)

    def test_backup_health_rejects_missing_stale_and_low_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "contentcrew.db"
            backups = root / "backups"
            backups.mkdir(mode=0o700)
            control = root / "control"
            control.mkdir(mode=0o700)
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(
                backup_health.BackupHealthError, "attestation"
            ):
                backup_health.check_backup_health(
                    database=database,
                    backup_dir=backups,
                    attestation_path=control / "latest.json",
                )
            with patch.object(backup_health, "_free_bytes", return_value=1):
                with self.assertRaisesRegex(
                    backup_health.BackupHealthError, "insufficient"
                ):
                    backup_health.check_backup_health(
                        database=database, backup_dir=backups, disk_only=True
                    )

    def test_candidate_quiescent_mode_blocks_traffic_and_background_workers(self):
        source = (ROOT / "app" / "main.py").read_text()
        self.assertIn(
            'QUIESCENT = os.environ.get("CONTENTCREW_QUIESCENT") == "1"',
            source,
        )
        self.assertIn(
            'os.environ.get("CONTENTCREW_VALIDATION") == "1"',
            source,
        )
        self.assertIn("if VALIDATION:", source)
        self.assertIn('if QUIESCENT and path != "/healthz":', source)
        self.assertIn("status_code=503", source)

    def test_test_runner_defaults_local_and_requires_production_confirmation(self):
        source = (ROOT / "tests" / "run.sh").read_text()
        self.assertIn("http://127.0.0.1:8899", source)
        self.assertIn("SMOKE_CONFIRM_PRODUCTION", source)
        self.assertNotIn("cd /root/contentcrew", source)

    def test_preflight_proves_release_state_and_writable_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, state, venv = self._preflight_fixture(base)
            report = self._check_fixture(root, state, venv)

            self.assertTrue(report["ok"])
            self.assertEqual("ok", report["integrity"])
            self.assertEqual(1, report["department_files"])
            self.assertEqual(1, report["industry_knowledge_files"])

    def test_preflight_requires_immutable_seeds_but_allows_gate_state_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, venv = self._preflight_fixture(Path(tmp))
            (state / "gate_rules.json").unlink()
            report = self._check_fixture(root, state, venv)
            self.assertTrue(report["ok"])

            (root / "config" / "gate_rules.default.json").unlink()
            with self.assertRaisesRegex(
                preflight.PreflightError,
                "immutable gate seed",
            ):
                self._check_fixture(root, state, venv)

    def test_preflight_rejects_missing_or_mismatched_industry_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, venv = self._preflight_fixture(Path(tmp))
            knowledge = (
                root / "config" / "industry_knowledge" / "content.json"
            )
            knowledge.unlink()
            with self.assertRaisesRegex(
                preflight.PreflightError,
                "industry knowledge is empty",
            ):
                self._check_fixture(root, state, venv)

            knowledge.write_text(
                '{"key":"other","name":"错误行业",'
                '"metrics":[],"benchmarks":[],"glossary":[],'
                '"practices":[],"compliance":[],"pitfalls":[]}'
            )
            with self.assertRaisesRegex(
                preflight.PreflightError,
                "keys do not match",
            ):
                self._check_fixture(root, state, venv)

    def test_preflight_accepts_recognized_legacy_database_without_version_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, venv = self._preflight_fixture(
                Path(tmp), legacy=True
            )
            report = self._check_fixture(root, state, venv)
        self.assertEqual(0, report["schema_version"])
        self.assertEqual(0, report["schema_ledger_version"])

    def test_preflight_rejects_table_name_only_fake_legacy_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, venv = self._preflight_fixture(Path(tmp))
            database = state / "contentcrew.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                "DROP TABLE schema_version;"
                "DROP TABLE job;"
                "CREATE TABLE job(id INTEGER PRIMARY KEY);"
            )
            connection.close()
            with self.assertRaisesRegex(
                preflight.PreflightError, "job is missing columns"
            ):
                self._check_fixture(root, state, venv)

    def test_preflight_rejects_non_executable_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, venv = self._preflight_fixture(Path(tmp))
            (root / "run.sh").chmod(0o644)
            with self.assertRaisesRegex(
                preflight.PreflightError, "entrypoint is not executable"
            ):
                self._check_fixture(root, state, venv)

    def test_preflight_rejects_readonly_database_and_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, venv = self._preflight_fixture(Path(tmp))
            database = state / "contentcrew.db"
            database.chmod(0o444)
            with self.assertRaisesRegex(
                preflight.PreflightError, "database is not writable"
            ):
                self._check_fixture(root, state, venv)
            database.chmod(0o600)
            sidecar = state / "contentcrew.db-wal"
            sidecar.write_bytes(b"fixture")
            sidecar.chmod(0o444)
            with self.assertRaisesRegex(
                preflight.PreflightError, "sidecar is not writable"
            ):
                self._check_fixture(root, state, venv)

    def test_preflight_rejects_unwritable_existing_runtime_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, venv = self._preflight_fixture(Path(tmp))
            runtime_file = state / "assets" / "avatar" / "old-output.mp4"
            runtime_file.write_bytes(b"fixture")
            runtime_file.chmod(0o444)
            with self.assertRaisesRegex(
                preflight.PreflightError, "runtime file is not writable"
            ):
                self._check_fixture(root, state, venv)

    def test_preflight_rejects_untraversable_runtime_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, venv = self._preflight_fixture(Path(tmp))
            child = state / "assets" / "avatar"
            child.chmod(0o000)
            try:
                with self.assertRaisesRegex(
                    preflight.PreflightError,
                    "runtime subdirectory is not writable|cannot be traversed",
                ):
                    self._check_fixture(root, state, venv)
            finally:
                child.chmod(0o755)

    def test_preflight_uses_highest_ledger_or_sqlite_user_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, venv = self._preflight_fixture(
                Path(tmp), legacy=True
            )
            connection = sqlite3.connect(state / "contentcrew.db")
            connection.execute("PRAGMA user_version=999")
            connection.close()
            with self.assertRaisesRegex(
                preflight.PreflightError, "newer than app"
            ):
                self._check_fixture(root, state, venv)

    def test_api_tool_runner_has_no_root_login_fallback(self):
        source = (ROOT / "app" / "llm.py").read_text()
        self.assertIn("CONTENTCREW_CLAUDE_PATH", source)
        self.assertNotIn('"/root/.local/bin/claude"', source)

    def test_api_tool_runner_preflight_accepts_every_required_cli_capability(self):
        from app import llm

        help_text = "\n".join((
            "--safe-mode",
            "--setting-sources",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--no-chrome",
            "--output-format",
            "--verbose",
            "--include-partial-messages",
            "--model",
            "--system-prompt",
            "--tools",
            "--max-budget-usd",
        ))
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "claude"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            with patch.object(
                preflight.subprocess,
                "run",
                side_effect=(
                    preflight.subprocess.CompletedProcess(
                        [str(executable), "--version"],
                        0,
                        stdout="2.1.220\n",
                        stderr="",
                    ),
                    preflight.subprocess.CompletedProcess(
                        [str(executable), "--help"],
                        0,
                        stdout=help_text,
                        stderr="",
                    ),
                ),
            ) as run:
                report = preflight._check_api_tool_runner(executable)

        self.assertTrue(report["ok"])
        self.assertEqual(
            set(preflight.API_TOOL_RUNNER_REQUIRED_ISOLATION_FLAGS),
            set(report["required_isolation_flags"]),
        )
        self.assertEqual(
            set(help_text.splitlines()),
            set(report["validated_flags"]),
        )
        self.assertEqual(13, len(report["validated_flags"]))
        self.assertIn("--max-budget-usd", report["validated_flags"])
        self.assertEqual(
            [
                [str(executable), "--version"],
                [str(executable), "--help"],
            ],
            [call.args[0] for call in run.call_args_list],
        )
        command = llm._runner_command(
            "/opt/claude",
            model="claude-opus-4-8",
            web=True,
            system_prompt="fixture",
        )
        for flag in preflight.API_TOOL_RUNNER_REQUIRED_ISOLATION_FLAGS:
            self.assertIn(flag, command)

    def test_api_tool_runner_preflight_rejects_legacy_cli_missing_safe_mode(self):
        help_text = "\n".join((
            "--setting-sources",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--no-chrome",
            "--output-format",
            "--verbose",
            "--include-partial-messages",
            "--model",
            "--system-prompt",
            "--tools",
            "--max-budget-usd",
        ))
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "claude"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            with patch.object(
                preflight.subprocess,
                "run",
                side_effect=(
                    preflight.subprocess.CompletedProcess(
                        [str(executable), "--version"],
                        0,
                        stdout="1.0.0\n",
                        stderr="",
                    ),
                    preflight.subprocess.CompletedProcess(
                        [str(executable), "--help"],
                        0,
                        stdout=help_text,
                        stderr="",
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    preflight.PreflightError,
                    r"missing required capabilities: --safe-mode",
                ):
                    preflight._check_api_tool_runner(executable)

    def test_api_tool_runner_preflight_fails_closed_when_help_command_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "claude"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            with patch.object(
                preflight.subprocess,
                "run",
                side_effect=(
                    preflight.subprocess.CompletedProcess(
                        [str(executable), "--version"],
                        0,
                        stdout="2.1.0\n",
                        stderr="",
                    ),
                    preflight.subprocess.CompletedProcess(
                        [str(executable), "--help"],
                        2,
                        stdout="",
                        stderr="unsupported",
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    preflight.PreflightError,
                    r"API tool runner --help failed",
                ):
                    preflight._check_api_tool_runner(executable)

    def test_failure_alert_rejects_unapproved_webhook_without_network(self):
        with patch.dict(
            os.environ,
            {"PAIHUO_ALERT_WEBHOOK": "http://127.0.0.1/internal"},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                failure_alert.send_failure_alert("contentcrew.service")


if __name__ == "__main__":
    unittest.main()
