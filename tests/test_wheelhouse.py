import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from deploy import wheelhouse


def _wheel(
    directory,
    name,
    version,
    *,
    metadata_name=None,
    metadata_version=None,
    filename_name=None,
    tag="py3-none-any",
):
    filename = f"{filename_name or name.replace('-', '_')}-{version}-{tag}.whl"
    path = directory / filename
    dist_info = f"{(filename_name or name).replace('-', '_')}-{version}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\n"
        f"Name: {metadata_name or name}\n"
        f"Version: {metadata_version or version}\n\n"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(f"{name.replace('-', '_')}/__init__.py", "")
    path.chmod(0o600)
    return path


class WheelhouseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "download"
        self.source.mkdir(mode=0o700)
        self.lock = self.base / "requirements.lock.txt"
        self.lock.write_text(
            "# exact runtime closure\n"
            "Demo-Pkg==1.2.3\n"
            "pure-helper==2.0\n",
            encoding="utf-8",
        )
        self.lock.chmod(0o600)
        _wheel(
            self.source,
            "Demo-Pkg",
            "1.2.3",
            filename_name="demo_pkg",
            tag="cp312-cp312-manylinux_2_17_x86_64",
        )
        _wheel(self.source, "pure-helper", "2.0")

    def tearDown(self):
        self.tmp.cleanup()

    def test_seal_and_verify_happy_path(self):
        output = self.base / "sealed"
        result = wheelhouse.seal_wheelhouse(self.lock, self.source, output)
        self.assertEqual("created", result["status"])
        self.assertEqual("paihuo-wheelhouse-attestation/v1", result["format"])
        self.assertEqual("linux_x86_64", result["target_platform"])
        self.assertEqual("cp312", result["target_python"])
        self.assertEqual(2, result["requirements_count"])
        self.assertEqual(2, result["wheel_count"])
        self.assertEqual(0o555, stat.S_IMODE(output.stat().st_mode))
        self.assertEqual(
            0o444,
            stat.S_IMODE((output / "wheelhouse-attestation.json").stat().st_mode),
        )
        self.assertEqual(
            {
                "demo-pkg==1.2.3",
                "pure-helper==2.0",
            },
            {
                line.split(" --hash=", 1)[0]
                for line in (
                    output / "requirements.hashed.lock"
                ).read_text(encoding="utf-8").splitlines()
            },
        )
        checked = wheelhouse.verify_wheelhouse(output)
        self.assertEqual("verified", checked["status"])
        self.assertEqual(result["wheel_set_sha256"], checked["wheel_set_sha256"])

    def test_missing_extra_and_duplicate_requirements_fail_closed(self):
        missing = next(self.source.glob("pure_helper-*.whl"))
        missing.unlink()
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "missing"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, self.base / "missing"
            )

        _wheel(self.source, "pure-helper", "2.0")
        _wheel(self.source, "surprise", "9.9")
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "extra"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, self.base / "extra"
            )
        next(self.source.glob("surprise-*.whl")).unlink()
        original = next(self.source.glob("demo_pkg-*.whl"))
        duplicate = self.source / "demo_pkg-1.2.3-1-py3-none-any.whl"
        duplicate.write_bytes(original.read_bytes())
        duplicate.chmod(0o600)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "duplicate wheel"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, self.base / "duplicate-wheel"
            )

        duplicate_lock = self.base / "duplicate.lock"
        duplicate_lock.write_text("demo_pkg==1.2.3\nDemo.Pkg==1.2.3\n")
        duplicate_lock.chmod(0o600)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "duplicate"):
            wheelhouse.parse_exact_lock(duplicate_lock)

    def test_metadata_or_filename_mismatch_is_rejected(self):
        target = next(self.source.glob("pure_helper-*.whl"))
        target.unlink()
        _wheel(
            self.source,
            "pure-helper",
            "2.0",
            metadata_version="2.1",
        )
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "METADATA"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, self.base / "metadata-bad"
            )

        target = next(self.source.glob("pure_helper-*.whl"))
        target.unlink()
        _wheel(
            self.source,
            "pure-helper",
            "2.0",
            metadata_name="another-name",
        )
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "METADATA"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, self.base / "name-bad"
            )

    def test_links_special_entries_and_unsafe_modes_are_rejected(self):
        target = next(self.source.glob("pure_helper-*.whl"))
        body = target.read_bytes()
        target.unlink()
        target.symlink_to(next(self.source.glob("demo_pkg-*.whl")))
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "regular|symlink"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, self.base / "symlink"
            )

        target.unlink()
        target.write_bytes(body)
        target.chmod(0o666)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "mode"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, self.base / "mode"
            )

        target.chmod(0o600)
        hardlink = self.base / "wheel-hardlink"
        os.link(target, hardlink)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "link"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, self.base / "hardlink"
            )
        hardlink.unlink()

        fifo = self.source / "special-1.0-py3-none-any.whl"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "regular"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, self.base / "special"
            )

    def test_atomic_publish_failure_leaves_no_output_or_staging(self):
        output = self.base / "atomic"
        with (
            patch.object(wheelhouse.os, "replace", side_effect=OSError("fault")),
            self.assertRaisesRegex(OSError, "fault"),
        ):
            wheelhouse.seal_wheelhouse(self.lock, self.source, output)
        self.assertFalse(output.exists())
        self.assertFalse(
            any(
                path.name.startswith(".atomic.stage-")
                for path in self.base.iterdir()
            )
        )

    def test_output_is_deterministic_and_tamper_is_detected(self):
        first = self.base / "first"
        second = self.base / "second"
        one = wheelhouse.seal_wheelhouse(self.lock, self.source, first)
        two = wheelhouse.seal_wheelhouse(self.lock, self.source, second)
        self.assertEqual(
            (first / "requirements.hashed.lock").read_bytes(),
            (second / "requirements.hashed.lock").read_bytes(),
        )
        self.assertEqual(
            (first / "wheelhouse-attestation.json").read_bytes(),
            (second / "wheelhouse-attestation.json").read_bytes(),
        )
        self.assertEqual(one["wheel_set_sha256"], two["wheel_set_sha256"])

        copied = next((first / "wheels").glob("pure_helper-*.whl"))
        copied.chmod(0o644)
        copied.write_bytes(copied.read_bytes() + b"tamper")
        copied.chmod(0o444)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "hash|size"):
            wheelhouse.verify_wheelhouse(first)

    def test_exact_lock_and_cli_reject_ambiguous_input(self):
        bad = self.base / "bad.lock"
        for content in (
            "name>=1\n",
            "name==1; python_version>'3'\n",
            "-r other.txt\n",
            "name==1 --hash=sha256:abcd\n",
        ):
            bad.write_text(content)
            bad.chmod(0o600)
            with self.subTest(content=content):
                with self.assertRaises(wheelhouse.WheelhouseError):
                    wheelhouse.parse_exact_lock(bad)

        parser = wheelhouse._parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--seal", "--source-lock", str(self.lock)])
        with (
            patch.object(wheelhouse.os, "geteuid", return_value=501),
            patch.object(
                wheelhouse,
                "_parser",
                side_effect=AssertionError("parsed before root check"),
            ),
        ):
            self.assertEqual(1, wheelhouse.main(["--verify"]))

    def test_trusted_cli_entrypoint_binds_path_inode_mode_and_hash(self):
        fixed = self.base / "fixed-wheelhouse.py"
        fixed.write_bytes(b"# fixed tool\n")
        fixed.chmod(0o600)
        identity = wheelhouse.IdentityPolicy(
            root_uid=os.geteuid(),
            root_gid=os.getegid(),
            build_uid=os.geteuid(),
            build_gid=os.getegid(),
            production=False,
        )
        info = fixed.lstat()
        digest = hashlib.sha256(fixed.read_bytes()).hexdigest()
        wheelhouse._assert_trusted_entrypoint(
            fixed_path=fixed,
            loaded_path=fixed,
            loaded_info=info,
            loaded_sha256=digest,
            identities=identity,
        )
        fixed.write_bytes(b"# changed tool\n")
        fixed.chmod(0o600)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "identity|hash"):
            wheelhouse._assert_trusted_entrypoint(
                fixed_path=fixed,
                loaded_path=fixed,
                loaded_info=info,
                loaded_sha256=digest,
                identities=identity,
            )

    def test_production_identity_resolution_and_build_owned_source(self):
        users = {
            "paihuo-build": SimpleNamespace(pw_uid=2101, pw_gid=2102),
            "paihuo": SimpleNamespace(pw_uid=2201, pw_gid=2202),
        }
        groups = {
            "paihuo-build": SimpleNamespace(gr_gid=2102),
            "paihuo": SimpleNamespace(gr_gid=2202),
        }
        with (
            patch.object(wheelhouse.os, "geteuid", return_value=0),
            patch.object(
                wheelhouse.pwd,
                "getpwnam",
                side_effect=users.__getitem__,
            ) as get_user,
            patch.object(
                wheelhouse.grp,
                "getgrnam",
                side_effect=groups.__getitem__,
            ) as get_group,
            patch.object(
                wheelhouse.os,
                "getgrouplist",
                return_value=[2102],
            ) as get_groups,
        ):
            identity = wheelhouse.resolve_identity_policy()
        self.assertTrue(identity.production)
        self.assertEqual((0, 0), (identity.root_uid, identity.root_gid))
        self.assertEqual((2101, 2102), (identity.build_uid, identity.build_gid))
        self.assertEqual(
            ["paihuo-build", "paihuo"],
            [entry.args[0] for entry in get_user.call_args_list],
        )
        self.assertEqual(
            ["paihuo-build", "paihuo"],
            [entry.args[0] for entry in get_group.call_args_list],
        )
        get_groups.assert_called_once_with("paihuo-build", 2102)

        for label, service_uid, service_gid, build_groups in (
            ("shared uid", 2101, 2202, [2102]),
            ("shared gid", 2201, 2102, [2102]),
            ("supplementary group", 2201, 2202, [2102, 2202]),
        ):
            isolated_users = {
                "paihuo-build": SimpleNamespace(pw_uid=2101, pw_gid=2102),
                "paihuo": SimpleNamespace(
                    pw_uid=service_uid,
                    pw_gid=service_gid,
                ),
            }
            isolated_groups = {
                "paihuo-build": SimpleNamespace(gr_gid=2102),
                "paihuo": SimpleNamespace(gr_gid=service_gid),
            }
            with (
                self.subTest(label=label),
                patch.object(wheelhouse.os, "geteuid", return_value=0),
                patch.object(
                    wheelhouse.pwd,
                    "getpwnam",
                    side_effect=isolated_users.__getitem__,
                ),
                patch.object(
                    wheelhouse.grp,
                    "getgrnam",
                    side_effect=isolated_groups.__getitem__,
                ),
                patch.object(
                    wheelhouse.os,
                    "getgrouplist",
                    return_value=build_groups,
                ),
                self.assertRaisesRegex(
                    wheelhouse.WheelhouseError,
                    "not isolated",
                ),
            ):
                wheelhouse.resolve_identity_policy()

        wrong_build = wheelhouse.IdentityPolicy(
            root_uid=os.geteuid(),
            root_gid=os.getegid(),
            build_uid=os.geteuid() + 1,
            build_gid=os.getegid(),
            production=True,
        )
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "owner"):
            wheelhouse.seal_wheelhouse(
                self.lock,
                self.source,
                self.base / "wrong-owner",
                identities=wrong_build,
            )

    def test_musllinux_and_writable_output_ancestor_are_rejected(self):
        target = next(self.source.glob("demo_pkg-*.whl"))
        body = target.read_bytes()
        target.unlink()
        musl = self.source / "demo_pkg-1.2.3-cp312-cp312-musllinux_1_2_x86_64.whl"
        musl.write_bytes(body)
        musl.chmod(0o600)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "incompatible"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, self.base / "musl"
            )

        musl.unlink()
        target.write_bytes(body)
        target.chmod(0o600)
        unsafe = self.base / "unsafe-parent"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "ancestor|mode"):
            wheelhouse.seal_wheelhouse(
                self.lock, self.source, unsafe / "sealed"
            )


class AdoptVenvCase(unittest.TestCase):
    RELEASE_ID = "20260726T180000Z-schema47-r6"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.paihuo = self.base / "srv" / "paihuo"
        self.releases = self.paihuo / "releases"
        self.release = self.releases / self.RELEASE_ID
        self.quarantine = self.paihuo / ".venv-quarantine"
        for directory in (
            self.base / "srv",
            self.paihuo,
            self.releases,
            self.release,
            self.quarantine,
        ):
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
        self.venv = self.release / "venv"
        (self.venv / "bin").mkdir(parents=True, mode=0o755)
        (self.venv / "lib" / "python3.12").mkdir(parents=True, mode=0o755)
        self.venv.chmod(0o755)
        (self.venv / "bin" / "python").write_bytes(b"ELF fixture")
        (self.venv / "bin" / "python").chmod(0o755)
        (self.venv / "lib" / "python3.12" / "site.py").write_text(
            "# fixture\n",
            encoding="utf-8",
        )
        (self.venv / "lib" / "python3.12" / "site.py").chmod(0o644)
        self.identity = wheelhouse.IdentityPolicy(
            root_uid=os.geteuid(),
            root_gid=os.getegid(),
            build_uid=os.geteuid(),
            build_gid=os.getegid(),
            production=False,
        )
        self.layout = wheelhouse.AdoptLayout(
            releases_root=self.releases,
            quarantine_root=self.quarantine,
            proc_root=self.base / "proc",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _adopt(self, **overrides):
        arguments = {
            "layout": self.layout,
            "identities": self.identity,
            "process_checker": lambda _uid, _proc: False,
        }
        arguments.update(overrides)
        return wheelhouse.adopt_venv(self.RELEASE_ID, **arguments)

    def test_successfully_adopts_tree_and_returns_stable_digest(self):
        result = self._adopt()
        self.assertEqual("adopted", result["status"])
        self.assertEqual(self.RELEASE_ID, result["release_id"])
        self.assertRegex(result["venv_tree_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(2, result["file_count"])
        self.assertTrue(self.venv.is_dir())
        self.assertEqual(
            result["venv_tree_sha256"],
            wheelhouse.inspect_venv_tree(
                self.venv,
                expected_uid=self.identity.root_uid,
                expected_gid=self.identity.root_gid,
            )["venv_tree_sha256"],
        )
        self.assertEqual([], list(self.quarantine.iterdir()))
        self.assertEqual(0o755, stat.S_IMODE(self.venv.stat().st_mode))
        for current, _directories, filenames in os.walk(self.venv):
            current_path = Path(current)
            self.assertEqual(
                0o005,
                stat.S_IMODE(current_path.stat().st_mode) & 0o005,
            )
            for filename in filenames:
                mode = stat.S_IMODE((current_path / filename).stat().st_mode)
                self.assertTrue(mode & 0o004)
                if mode & 0o111:
                    self.assertTrue(mode & 0o001)

    def test_runtime_inaccessible_modes_fail_before_quarantine(self):
        cases = (
            (self.venv, 0o700, "root mode"),
            (self.venv / "lib", 0o700, "traversable"),
            (
                self.venv / "lib" / "python3.12" / "site.py",
                0o600,
                "readable",
            ),
            (self.venv / "bin" / "python", 0o744, "executable"),
        )
        for target, bad_mode, message in cases:
            original_mode = stat.S_IMODE(target.stat().st_mode)
            with (
                self.subTest(target=target.name),
                self.assertRaisesRegex(
                    wheelhouse.WheelhouseError,
                    message,
                ),
            ):
                target.chmod(bad_mode)
                try:
                    self._adopt()
                finally:
                    target.chmod(original_mode)
            self.assertTrue(self.venv.is_dir())
            self.assertEqual([], list(self.quarantine.iterdir()))

    def test_running_build_process_blocks_before_rename(self):
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "process"):
            self._adopt(process_checker=lambda _uid, _proc: True)
        self.assertTrue(self.venv.is_dir())
        self.assertEqual([], list(self.quarantine.iterdir()))

        checks = iter((False, True))
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "after quarantine"):
            self._adopt(
                process_checker=lambda _uid, _proc: next(checks)
            )
        self.assertFalse(self.venv.exists())
        self.assertEqual(1, len(list(self.quarantine.iterdir())))

    def test_symlink_hardlink_owner_and_mode_fail_before_quarantine(self):
        link = self.venv / "lib" / "escape"
        link.symlink_to(self.base)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "symlink"):
            self._adopt()
        link.unlink()

        original = self.venv / "lib" / "python3.12" / "site.py"
        hardlink = self.venv / "lib" / "python3.12" / "site-copy.py"
        os.link(original, hardlink)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "link"):
            self._adopt()
        hardlink.unlink()

        original.chmod(0o666)
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "mode"):
            self._adopt()
        original.chmod(0o644)

        wrong_owner = wheelhouse.IdentityPolicy(
            root_uid=os.geteuid(),
            root_gid=os.getegid(),
            build_uid=os.geteuid() + 1,
            build_gid=os.getegid(),
            production=False,
        )
        with self.assertRaisesRegex(wheelhouse.WheelhouseError, "owner"):
            wheelhouse.adopt_venv(
                self.RELEASE_ID,
                layout=self.layout,
                identities=wrong_owner,
                process_checker=lambda _uid, _proc: False,
            )

    def test_rename_or_chown_failure_never_publishes_partial_tree(self):
        with (
            patch.object(wheelhouse.os, "rename", side_effect=OSError("rename fault")),
            self.assertRaisesRegex(OSError, "rename fault"),
        ):
            self._adopt()
        self.assertTrue(self.venv.is_dir())
        self.assertEqual([], list(self.quarantine.iterdir()))

        with (
            patch.object(wheelhouse.os, "chown", side_effect=OSError("chown fault")),
            self.assertRaisesRegex(OSError, "chown fault"),
        ):
            self._adopt()
        self.assertFalse(self.venv.exists())
        self.assertEqual(1, len(list(self.quarantine.iterdir())))

    def test_final_publish_rename_failure_leaves_root_owned_quarantine(self):
        real_rename = wheelhouse.os.rename
        calls = {"count": 0}

        def fail_second(source, destination):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("publish fault")
            return real_rename(source, destination)

        with (
            patch.object(wheelhouse.os, "rename", side_effect=fail_second),
            self.assertRaisesRegex(OSError, "publish fault"),
        ):
            self._adopt()
        self.assertFalse(self.venv.exists())
        quarantined = list(self.quarantine.iterdir())
        self.assertEqual(1, len(quarantined))
        report = wheelhouse.inspect_venv_tree(
            quarantined[0],
            expected_uid=self.identity.root_uid,
            expected_gid=self.identity.root_gid,
        )
        self.assertRegex(report["venv_tree_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
