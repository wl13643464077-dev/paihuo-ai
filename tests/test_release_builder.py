import hashlib
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest

from deploy import build_release


class ReleaseBuilderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "source"
        self.root.mkdir()
        (self.root / "app").mkdir()
        (self.root / "app" / "main.py").write_text(
            "print('candidate')\n",
            encoding="utf-8",
        )
        (self.root / "app" / "db.py").write_text(
            "LATEST_SCHEMA_VERSION = 47\n",
            encoding="utf-8",
        )
        (self.root / "run.sh").write_text(
            "#!/bin/bash\nexec python -m app.main\n",
            encoding="utf-8",
        )
        (self.root / "run.sh").chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, output: Path):
        return build_release.build_release(
            source=self.root,
            output_dir=output,
            release_id="20260726T010203Z-schema47-r6",
            source_date_epoch=1_785_000_000,
            root_files=("run.sh",),
            root_dirs=("app",),
            data_files=(),
            empty_dirs=(),
        )

    def test_default_allowlist_is_self_contained_in_public_checkout(self):
        checkout = Path(__file__).resolve().parents[1]
        for relative in build_release.DEFAULT_ROOT_FILES:
            with self.subTest(kind="file", relative=relative):
                self.assertTrue((checkout / relative).is_file())
        for relative in build_release.DEFAULT_ROOT_DIRS:
            with self.subTest(kind="directory", relative=relative):
                self.assertTrue((checkout / relative).is_dir())
        for relative in build_release.DEFAULT_DATA_FILES:
            with self.subTest(kind="data", relative=relative):
                self.assertTrue((checkout / relative).exists())

    def test_allowlisted_archive_is_reproducible_and_self_verified(self):
        first = self._build(Path(self.tmp.name) / "out-a")
        second = self._build(Path(self.tmp.name) / "out-b")

        self.assertEqual(first["archive_sha256"], second["archive_sha256"])
        self.assertEqual(first["payload_tree_sha256"], second["payload_tree_sha256"])
        self.assertEqual(
            hashlib.sha256(Path(first["archive"]).read_bytes()).hexdigest(),
            first["archive_sha256"],
        )
        receipt = json.loads(Path(first["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(first["archive_sha256"], receipt["archive_sha256"])
        self.assertEqual(first["payload_tree_sha256"], receipt["payload_tree_sha256"])

        with tarfile.open(first["archive"], "r:gz") as archive:
            members = {member.name: member for member in archive.getmembers()}
            manifest_file = archive.extractfile("./RELEASE-MANIFEST.json")
            self.assertIsNotNone(manifest_file)
            manifest = json.loads(manifest_file.read().decode("utf-8"))
        self.assertEqual(
            {
                ".",
                "./RELEASE-MANIFEST.json",
                "./app",
                "./app/db.py",
                "./app/main.py",
                "./run.sh",
            },
            set(members),
        )
        self.assertTrue(members["./run.sh"].isfile())
        self.assertEqual(0o755, members["./run.sh"].mode)
        self.assertEqual(0o644, members["./app/main.py"].mode)
        self.assertTrue(members["./app"].isdir())
        self.assertEqual(47, manifest["schema_version"])
        self.assertEqual(47, receipt["schema_version"])
        self.assertEqual(47, first["schema_version"])

    def test_mutable_data_seeds_are_mapped_to_immutable_config(self):
        departments = self.root / "data" / "departments"
        departments.mkdir(parents=True)
        (departments / "industry.json").write_text(
            '{"key":"industry","employees":[]}\n',
            encoding="utf-8",
        )
        (departments / "_source_ten.csv").write_text(
            "build,source,only\n",
            encoding="utf-8",
        )
        knowledge = self.root / "data" / "industry_knowledge"
        knowledge.mkdir(parents=True)
        (knowledge / "industry.json").write_text(
            '{"key":"industry","name":"行业","metrics":[]}\n',
            encoding="utf-8",
        )
        (self.root / "data" / "gate_rules.json").write_text(
            '{"sensitive_words":["fixture"]}\n',
            encoding="utf-8",
        )
        result = build_release.build_release(
            source=self.root,
            output_dir=Path(self.tmp.name) / "out-config",
            release_id="20260726T010203Z-schema47-r6",
            source_date_epoch=1_785_000_000,
            root_files=("run.sh",),
            root_dirs=("app",),
            data_files=build_release.DEFAULT_DATA_FILES,
            empty_dirs=(),
        )

        with tarfile.open(result["archive"], "r:gz") as archive:
            names = {member.name for member in archive.getmembers()}
            manifest_file = archive.extractfile("./RELEASE-MANIFEST.json")
            self.assertIsNotNone(manifest_file)
            manifest = json.loads(manifest_file.read().decode("utf-8"))
        self.assertIn("./config/departments/industry.json", names)
        self.assertIn("./config/industry_knowledge/industry.json", names)
        self.assertIn("./config/gate_rules.default.json", names)
        self.assertNotIn("./config/departments/_source_ten.csv", names)
        self.assertFalse(any(name == "./data" or name.startswith("./data/") for name in names))
        self.assertFalse(
            any(
                row["path"] == "data" or row["path"].startswith("data/")
                for row in manifest["entries"]
            )
        )
        self.assertFalse((Path(result["staging"]) / "data").exists())

    def test_unexpected_department_seed_entry_fails_closed(self):
        departments = self.root / "data" / "departments"
        departments.mkdir(parents=True)
        (departments / "industry.json").write_text(
            '{"key":"industry","employees":[]}\n',
            encoding="utf-8",
        )
        knowledge = self.root / "data" / "industry_knowledge"
        knowledge.mkdir(parents=True)
        (knowledge / "industry.json").write_text(
            '{"key":"industry","name":"行业","metrics":[]}\n',
            encoding="utf-8",
        )
        (departments / "unexpected.csv").write_text(
            "not,runtime,config\n",
            encoding="utf-8",
        )
        (self.root / "data" / "gate_rules.json").write_text(
            '{"sensitive_words":[]}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            build_release.ReleaseBuildError,
            "departments release seed",
        ):
            build_release.build_release(
                source=self.root,
                output_dir=Path(self.tmp.name) / "out-unexpected-seed",
                release_id="20260726T010203Z-schema47-r6",
                source_date_epoch=1_785_000_000,
                root_files=("run.sh",),
                root_dirs=("app",),
                data_files=build_release.DEFAULT_DATA_FILES,
                empty_dirs=(),
            )

    def test_source_schema_and_release_claim_must_match(self):
        (self.root / "app" / "db.py").write_text(
            "LATEST_SCHEMA_VERSION = 46\n",
            encoding="utf-8",
        )
        output = Path(self.tmp.name) / "out-mismatch"
        with self.assertRaisesRegex(
            build_release.ReleaseBuildError,
            "schema",
        ):
            self._build(output)
        self.assertFalse(output.exists() and any(output.iterdir()))

    def test_source_schema_must_be_one_static_positive_literal(self):
        invalid_sources = (
            "OTHER_VERSION = 47\n",
            "LATEST_SCHEMA_VERSION = 47\nLATEST_SCHEMA_VERSION = 47\n",
            "LATEST_SCHEMA_VERSION = 40 + 7\n",
            (
                "LATEST_SCHEMA_VERSION = 47\n"
                "if True:\n"
                "    LATEST_SCHEMA_VERSION = 46\n"
            ),
        )
        for index, source in enumerate(invalid_sources, start=1):
            with self.subTest(index=index):
                (self.root / "app" / "db.py").write_text(
                    source,
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    build_release.ReleaseBuildError,
                    "LATEST_SCHEMA_VERSION|schema",
                ):
                    self._build(Path(self.tmp.name) / f"out-schema-source-{index}")

    def test_release_id_must_contain_one_canonical_schema_claim(self):
        for release_id in (
            "20260726T010203Z-r6",
            "20260726T010203Z-schema047-r6",
            "20260726T010203Z-schema47-schema46-r6",
        ):
            with self.subTest(release_id=release_id), self.assertRaisesRegex(
                build_release.ReleaseBuildError,
                "schema",
            ):
                build_release.build_release(
                    source=self.root,
                    output_dir=Path(self.tmp.name) / f"out-{release_id}",
                    release_id=release_id,
                    source_date_epoch=1_785_000_000,
                    root_files=("run.sh",),
                    root_dirs=("app",),
                    data_files=(),
                    empty_dirs=(),
                )

    def test_unallowlisted_runtime_data_is_not_packaged(self):
        (self.root / "data").mkdir()
        (self.root / "data" / "contentcrew.db").write_bytes(b"not-a-release-file")
        result = self._build(Path(self.tmp.name) / "out")

        with tarfile.open(result["archive"], "r:gz") as archive:
            names = {member.name for member in archive.getmembers()}
        self.assertNotIn("./data/contentcrew.db", names)

    def test_sensitive_filename_anywhere_in_source_fails_closed(self):
        secret = self.root / "operator.env"
        secret.write_text("TOKEN=do-not-package\n", encoding="utf-8")
        secret.chmod(0o600)

        with self.assertRaisesRegex(
            build_release.ReleaseBuildError,
            "sensitive",
        ):
            self._build(Path(self.tmp.name) / "out")

    def test_sensitive_backup_filename_anywhere_in_source_fails_closed(self):
        for index, suffix in enumerate((".backup", ".bak"), start=1):
            backup = self.root / f"operator{suffix}"
            backup.write_text("not-release-input\n", encoding="utf-8")
            with self.subTest(suffix=suffix), self.assertRaisesRegex(
                build_release.ReleaseBuildError,
                "sensitive",
            ):
                self._build(Path(self.tmp.name) / f"out-backup-{index}")
            backup.unlink()

    def test_symlink_or_hardlinked_allowed_entry_fails_closed(self):
        target = self.root / "outside.py"
        target.write_text("print('outside')\n", encoding="utf-8")
        link = self.root / "app" / "linked.py"
        link.symlink_to(target)
        with self.assertRaisesRegex(
            build_release.ReleaseBuildError,
            "symlink",
        ):
            self._build(Path(self.tmp.name) / "out-link")

        link.unlink()
        hardlink = self.root / "app" / "hardlinked.py"
        os.link(self.root / "app" / "main.py", hardlink)
        self.assertGreater(os.lstat(hardlink).st_nlink, 1)
        with self.assertRaisesRegex(
            build_release.ReleaseBuildError,
            "hard link",
        ):
            self._build(Path(self.tmp.name) / "out-hardlink")

    def test_special_or_setid_allowed_entry_fails_closed(self):
        pipe = self.root / "app" / "unsafe-pipe"
        os.mkfifo(pipe)
        with self.assertRaisesRegex(
            build_release.ReleaseBuildError,
            "special",
        ):
            self._build(Path(self.tmp.name) / "out-pipe")

        pipe.unlink()
        setid = self.root / "app" / "unsafe-helper"
        setid.write_bytes(b"fixture")
        setid.chmod(0o4755)
        # Some macOS test volumes strip the set-id bit at chmod time. Exercise
        # the fail-closed branch on filesystems that preserve it.
        if os.lstat(setid).st_mode & stat.S_ISUID:
            with self.assertRaisesRegex(
                build_release.ReleaseBuildError,
                "setuid",
            ):
                self._build(Path(self.tmp.name) / "out-setid")

    def test_high_confidence_secret_content_fails_without_echoing_value(self):
        marker = "AKIA" + "A" * 16
        (self.root / "app" / "bad.py").write_text(
            f"credential = '{marker}'\n",
            encoding="utf-8",
        )
        with self.assertRaises(build_release.ReleaseBuildError) as failed:
            self._build(Path(self.tmp.name) / "out")
        self.assertIn("credential-like", str(failed.exception))
        self.assertNotIn(marker, str(failed.exception))

    def test_managed_secret_assignments_fail_without_echoing_value(self):
        for index, variable in enumerate(
            (
                "CONTENTCREW_CONFIG_KEY",
                "CONTENTCREW_SESSION_SECRET",
                "YUNWU_API_KEY",
            ),
            start=1,
        ):
            marker = f"StrongToken{index}_" + "Ab3-" * 14
            bad = self.root / "app" / "managed-secret.py"
            bad.write_text(
                f"{variable}={marker}\n",
                encoding="utf-8",
            )
            with self.subTest(variable=variable):
                with self.assertRaises(
                    build_release.ReleaseBuildError
                ) as failed:
                    self._build(Path(self.tmp.name) / f"out-managed-{index}")
                self.assertIn(
                    "managed-secret-assignment",
                    str(failed.exception),
                )
                self.assertNotIn(marker, str(failed.exception))
            bad.unlink()


if __name__ == "__main__":
    unittest.main()
