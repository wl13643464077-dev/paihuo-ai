import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from deploy import build_release, verify_release


class ReleaseVerifierCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "source"
        (self.source / "app").mkdir(parents=True)
        (self.source / "app" / "main.py").write_text(
            "print('verified')\n",
            encoding="utf-8",
        )
        (self.source / "app" / "db.py").write_text(
            "LATEST_SCHEMA_VERSION = 47\n",
            encoding="utf-8",
        )
        (self.source / "run.sh").write_text(
            "#!/bin/sh\nexec python -m app.main\n",
            encoding="utf-8",
        )
        (self.source / "run.sh").chmod(0o755)
        self.built = build_release.build_release(
            source=self.source,
            output_dir=self.base / "built",
            release_id="20260726T010203Z-schema47-r6",
            source_date_epoch=1_785_000_000,
            root_files=("run.sh",),
            root_dirs=("app",),
            data_files=(),
            empty_dirs=(),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _verify(self, *, archive=None, receipt=None, suffix="verified"):
        return verify_release.verify_release(
            archive=archive or self.built["archive"],
            receipt=receipt or self.built["receipt"],
            extract_dir=self.base / suffix,
            attestation=self.base / f"{suffix}.attestation.json",
        )

    def _rewrite_archive(self, mutate):
        source = Path(self.built["archive"])
        tampered = self.base / "tampered.tar.gz"
        members = []
        with tarfile.open(source, "r:gz") as archive:
            for original in archive.getmembers():
                member = copy.copy(original)
                extracted = archive.extractfile(original) if original.isfile() else None
                body = extracted.read() if extracted is not None else None
                members.append((member, body))
        mutate(members)
        with tarfile.open(tampered, "w:gz", format=tarfile.GNU_FORMAT) as archive:
            for member, body in members:
                if body is None:
                    archive.addfile(member)
                else:
                    import io
                    archive.addfile(member, io.BytesIO(body))
        receipt = json.loads(Path(self.built["receipt"]).read_text(encoding="utf-8"))
        archive_body = tampered.read_bytes()
        receipt.update(
            {
                "archive_filename": tampered.name,
                "archive_sha256": hashlib.sha256(archive_body).hexdigest(),
                "archive_size": len(archive_body),
            }
        )
        receipt_path = self.base / "tampered.receipt.json"
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        return tampered, receipt_path

    def test_verifies_extracts_to_new_directory_and_attests_owner_only(self):
        report = self._verify()
        extracted = self.base / "verified"
        attestation = self.base / "verified.attestation.json"

        self.assertTrue(report["ok"])
        self.assertEqual("paihuo-release-attestation/v1", report["format"])
        self.assertEqual(0o600, os.stat(attestation).st_mode & 0o777)
        self.assertEqual("print('verified')\n", (extracted / "app" / "main.py").read_text())
        decoded = json.loads(attestation.read_text(encoding="utf-8"))
        self.assertEqual(report, decoded)
        self.assertEqual(
            hashlib.sha256(Path(self.built["archive"]).read_bytes()).hexdigest(),
            decoded["archive_sha256"],
        )

    def test_refuses_existing_extract_or_attestation_target(self):
        extract = self.base / "existing"
        extract.mkdir()
        with self.assertRaisesRegex(verify_release.ReleaseVerifyError, "exist"):
            verify_release.verify_release(
                archive=self.built["archive"],
                receipt=self.built["receipt"],
                extract_dir=extract,
                attestation=self.base / "unused.attestation.json",
            )
        self.assertFalse((self.base / "unused.attestation.json").exists())

    def test_rejects_receipt_tamper_without_extracting(self):
        receipt = json.loads(Path(self.built["receipt"]).read_text(encoding="utf-8"))
        receipt["archive_sha256"] = "0" * 64
        tampered = self.base / "receipt.json"
        tampered.write_text(
            json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(verify_release.ReleaseVerifyError, "archive"):
            self._verify(receipt=tampered, suffix="receipt-failed")
        self.assertFalse((self.base / "receipt-failed").exists())
        self.assertFalse((self.base / "receipt-failed.attestation.json").exists())

    def test_rejects_tar_metadata_even_when_receipt_hash_matches(self):
        def mutate(members):
            target = next(member for member, _ in members if member.name == "./app/main.py")
            target.uid = 1000

        archive, receipt = self._rewrite_archive(mutate)
        with self.assertRaisesRegex(verify_release.ReleaseVerifyError, "metadata"):
            self._verify(
                archive=archive,
                receipt=receipt,
                suffix="metadata-failed",
            )
        self.assertFalse((self.base / "metadata-failed").exists())

    def test_archive_header_count_is_bounded_before_member_collection(self):
        def mutate(members):
            extra = copy.copy(members[0][0])
            extra.name = "./unexpected-zero-byte-directory"
            members.append((extra, None))

        archive, receipt = self._rewrite_archive(mutate)
        with (
            patch.object(verify_release, "MAX_MEMBERS", 6),
            self.assertRaisesRegex(
                verify_release.ReleaseVerifyError,
                "too many",
            ),
        ):
            self._verify(
                archive=archive,
                receipt=receipt,
                suffix="header-limit",
            )
        self.assertFalse((self.base / "header-limit").exists())

        source = Path(verify_release.__file__).read_text(encoding="utf-8")
        self.assertNotIn(".getmembers(", source)

    def test_rejects_payload_content_even_when_receipt_hash_matches(self):
        def mutate(members):
            for index, (member, body) in enumerate(members):
                if member.name == "./app/main.py":
                    changed = b"print('tampered')\n"
                    member.size = len(changed)
                    members[index] = (member, changed)
                    return
            self.fail("fixture member missing")

        archive, receipt = self._rewrite_archive(mutate)
        with self.assertRaisesRegex(verify_release.ReleaseVerifyError, "content|sha256"):
            self._verify(
                archive=archive,
                receipt=receipt,
                suffix="content-failed",
            )
        self.assertFalse((self.base / "content-failed").exists())

    def test_rejects_manifest_tree_tamper_even_when_receipt_hashes_match(self):
        changed_manifest = {}

        def mutate(members):
            for index, (member, body) in enumerate(members):
                if member.name == "./RELEASE-MANIFEST.json":
                    manifest = json.loads(body.decode("utf-8"))
                    manifest["payload_tree_sha256"] = "0" * 64
                    changed = (
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                    member.size = len(changed)
                    members[index] = (member, changed)
                    changed_manifest["body"] = changed
                    return
            self.fail("fixture manifest missing")

        archive, receipt_path = self._rewrite_archive(mutate)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["manifest_sha256"] = hashlib.sha256(
            changed_manifest["body"]
        ).hexdigest()
        receipt["payload_tree_sha256"] = "0" * 64
        receipt_path.write_text(
            json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            verify_release.ReleaseVerifyError,
            "tree",
        ):
            self._verify(
                archive=archive,
                receipt=receipt_path,
                suffix="tree-failed",
            )
        self.assertFalse((self.base / "tree-failed").exists())

    def test_single_file_cli_runs_before_candidate_is_extracted(self):
        isolated = self.base / "isolated"
        isolated.mkdir()
        script = isolated / "verify_release.py"
        shutil.copy2(Path(verify_release.__file__), script)
        extracted = self.base / "cli-extracted"
        attestation = self.base / "cli.attestation.json"

        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(script),
                "--archive",
                self.built["archive"],
                "--receipt",
                self.built["receipt"],
                "--extract-dir",
                str(extracted),
                "--attestation",
                str(attestation),
            ],
            cwd=isolated,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(extracted.is_dir())
        self.assertEqual(0o600, attestation.stat().st_mode & 0o777)
        self.assertTrue(json.loads(result.stdout)["ok"])

        verify_existing = subprocess.run(
            [
                sys.executable,
                "-I",
                str(script),
                "--archive",
                self.built["archive"],
                "--receipt",
                self.built["receipt"],
                "--artifact-attestation",
                str(attestation),
            ],
            cwd=isolated,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, verify_existing.returncode, verify_existing.stderr)
        self.assertEqual(
            "paihuo-release-attestation/v1",
            json.loads(verify_existing.stdout)["format"],
        )

    def test_existing_artifact_attestation_is_cross_checked_with_archive_and_receipt(self):
        self._verify(suffix="artifact")
        artifact = self.base / "artifact.attestation.json"
        report = verify_release.verify_release_artifact(
            archive=self.built["archive"],
            receipt=self.built["receipt"],
            artifact_attestation=artifact,
        )
        self.assertTrue(report["ok"])

        tampered = json.loads(artifact.read_text(encoding="utf-8"))
        tampered["archive_sha256"] = "0" * 64
        artifact.write_text(
            json.dumps(
                tampered,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        artifact.chmod(0o600)
        with self.assertRaisesRegex(
            verify_release.ReleaseVerifyError,
            "artifact.*archive",
        ):
            verify_release.verify_release_artifact(
                archive=self.built["archive"],
                receipt=self.built["receipt"],
                artifact_attestation=artifact,
            )

    def test_artifact_proof_rejects_swap_wide_parent_and_wrong_owner(self):
        self._verify(suffix="proof-security")
        artifact = self.base / "proof-security.attestation.json"
        replacement = self.base / "replacement.attestation.json"
        replacement.write_bytes(artifact.read_bytes())
        replacement.chmod(0o600)
        original_open = verify_release.os.open
        swapped = {"done": False}

        def swap_before_open(path, flags, *args, **kwargs):
            if (
                not swapped["done"]
                and Path(path).name == artifact.name
            ):
                os.replace(replacement, artifact)
                swapped["done"] = True
            return original_open(path, flags, *args, **kwargs)

        with (
            patch.object(
                verify_release.os,
                "open",
                side_effect=swap_before_open,
            ),
            self.assertRaisesRegex(
                verify_release.ReleaseVerifyError,
                "metadata",
            ),
        ):
            verify_release._read_artifact_attestation(artifact)

        self.base.chmod(0o770)
        try:
            with self.assertRaisesRegex(
                verify_release.ReleaseVerifyError,
                "parent",
            ):
                verify_release._read_artifact_attestation(artifact)
        finally:
            self.base.chmod(0o700)

        original_fstat = verify_release.os.fstat

        def wrong_owner(descriptor):
            actual = original_fstat(descriptor)
            values = list(actual)
            values[4] = actual.st_uid + 10_000
            return os.stat_result(values)

        with (
            patch.object(
                verify_release.os,
                "fstat",
                side_effect=wrong_owner,
            ),
            self.assertRaisesRegex(
                verify_release.ReleaseVerifyError,
                "ownership",
            ),
        ):
            verify_release._read_artifact_attestation(artifact)

    def test_materialized_release_allows_only_exact_data_link_and_venv(self):
        self._verify(suffix="materialized")
        release = self.base / "materialized"
        artifact = self.base / "materialized.attestation.json"
        state = self.base / "state"
        state.mkdir()
        (release / "data").symlink_to(state, target_is_directory=True)
        (release / "venv").mkdir()
        (release / "venv" / "installed.txt").write_text("fixture")
        attestation = self.base / "materialized-tree.attestation.json"

        report = verify_release.verify_materialized_release(
            release_dir=release,
            expected_data_target=state,
            artifact_attestation=artifact,
            attestation=attestation,
        )
        self.assertTrue(report["ok"])
        self.assertEqual(
            "paihuo-materialized-release-attestation/v1",
            report["format"],
        )
        self.assertEqual(0o600, attestation.stat().st_mode & 0o777)
        proof_before = attestation.read_bytes()
        with self.assertRaisesRegex(
            verify_release.ReleaseVerifyError,
            "already exists",
        ):
            verify_release.verify_materialized_release(
                release_dir=release,
                expected_data_target=state,
                artifact_attestation=artifact,
                attestation=attestation,
            )
        self.assertEqual(proof_before, attestation.read_bytes())
        self.assertEqual(
            report,
            verify_release.verify_materialized_attestation(
                release_dir=release,
                state_dir=state,
                artifact_attestation=artifact,
                attestation=attestation,
            ),
        )
        cli_recheck = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(verify_release.__file__)),
                "--materialized-release",
                str(release),
                "--state",
                str(state),
                "--artifact-attestation",
                str(artifact),
                "--materialized-attestation",
                str(attestation),
            ],
            cwd=self.base,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, cli_recheck.returncode, cli_recheck.stderr)
        self.assertEqual(report, json.loads(cli_recheck.stdout))
        attestation.chmod(0o644)
        try:
            with self.assertRaisesRegex(
                verify_release.ReleaseVerifyError,
                "ownership",
            ):
                verify_release.verify_materialized_attestation(
                    release_dir=release,
                    state_dir=state,
                    artifact_attestation=artifact,
                    attestation=attestation,
                )
        finally:
            attestation.chmod(0o600)

        (release / "unexpected.txt").write_text("not allowed")
        with self.assertRaisesRegex(
            verify_release.ReleaseVerifyError,
            "unexpected",
        ):
            verify_release.verify_materialized_release(
                release_dir=release,
                expected_data_target=state,
                artifact_attestation=artifact,
                attestation=self.base / "unexpected.attestation.json",
            )

    def test_materialized_attestation_recheck_detects_live_tree_tamper(self):
        self._verify(suffix="recheck-tree")
        release = self.base / "recheck-tree"
        artifact = self.base / "recheck-tree.attestation.json"
        state = self.base / "recheck-tree-state"
        state.mkdir()
        (release / "data").symlink_to(state, target_is_directory=True)
        (release / "venv").mkdir()
        materialized = self.base / "recheck-tree.materialized.json"
        verify_release.verify_materialized_release(
            release_dir=release,
            expected_data_target=state,
            artifact_attestation=artifact,
            attestation=materialized,
        )

        (release / "app" / "main.py").write_text(
            "print('changed after proof')\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            verify_release.ReleaseVerifyError,
            "content",
        ):
            verify_release.verify_materialized_attestation(
                release_dir=release,
                state_dir=state,
                artifact_attestation=artifact,
                attestation=materialized,
            )

    def test_materialized_attestation_recheck_detects_proof_tamper(self):
        self._verify(suffix="recheck-proof")
        release = self.base / "recheck-proof"
        artifact = self.base / "recheck-proof.attestation.json"
        state = self.base / "recheck-proof-state"
        state.mkdir()
        (release / "data").symlink_to(state, target_is_directory=True)
        (release / "venv").mkdir()
        materialized = self.base / "recheck-proof.materialized.json"
        verify_release.verify_materialized_release(
            release_dir=release,
            expected_data_target=state,
            artifact_attestation=artifact,
            attestation=materialized,
        )
        proof = json.loads(materialized.read_text(encoding="utf-8"))
        proof["artifact_receipt_sha256"] = "0" * 64
        materialized.write_text(
            json.dumps(
                proof,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        materialized.chmod(0o600)

        with self.assertRaisesRegex(
            verify_release.ReleaseVerifyError,
            "materialized.*artifact_receipt",
        ):
            verify_release.verify_materialized_attestation(
                release_dir=release,
                state_dir=state,
                artifact_attestation=artifact,
                attestation=materialized,
            )

    def test_materialized_release_rejects_wrong_data_symlink(self):
        self._verify(suffix="wrong-link")
        release = self.base / "wrong-link"
        artifact = self.base / "wrong-link.attestation.json"
        expected = self.base / "expected-state"
        wrong = self.base / "wrong-state"
        expected.mkdir()
        wrong.mkdir()
        (release / "data").symlink_to(wrong, target_is_directory=True)
        (release / "venv").mkdir()

        with self.assertRaisesRegex(
            verify_release.ReleaseVerifyError,
            "data",
        ):
            verify_release.verify_materialized_release(
                release_dir=release,
                expected_data_target=expected,
                artifact_attestation=artifact,
                attestation=self.base / "wrong-link.materialized.attestation.json",
            )

    def test_cli_parser_has_no_duplicate_option_strings(self):
        options = [
            option
            for action in verify_release._parser()._actions
            for option in action.option_strings
        ]
        self.assertEqual(len(options), len(set(options)))


if __name__ == "__main__":
    unittest.main()
