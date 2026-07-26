#!/usr/bin/env python3
"""Trusted, standard-library-only first-release bootstrap.

This module is copied out of band to a root-only fixed path and executed with
``/usr/bin/python3 -I``.  It never imports candidate code.  It asks the
separately trusted standalone release verifier to re-check retained artifact
evidence and the materialized live tree, then copies only a small explicit
fixed-ops closure into a root-only staging directory.  The staging launcher is
entered only through :func:`launch_stage`, which revalidates the entire stage
and live virtualenv immediately before ``execve``.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import subprocess
import sys
from typing import Callable, Sequence
import uuid


UTC = timezone.utc
BOOTSTRAP_FORMAT = "paihuo-release-bootstrap/v1"
MANIFEST_FORMAT = "paihuo-release-manifest/v1"
ARTIFACT_FORMAT = "paihuo-release-attestation/v1"
MATERIALIZED_FORMAT = "paihuo-materialized-release-attestation/v1"
WHEELHOUSE_FORMAT = "paihuo-wheelhouse-attestation/v1"
RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_OPS_FILE_BYTES = 16 * 1024 * 1024
MAX_VENV_FILE_BYTES = 256 * 1024 * 1024
MAX_VENV_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_VENV_ENTRIES = 250_000

OPS_FILES = (
    "app/__init__.py",
    "app/session_secret.py",
    "deploy/__init__.py",
    "deploy/backup_db.py",
    "deploy/backup_health.py",
    "deploy/bootstrap_release.py",
    "deploy/control_plane.py",
    "deploy/failure_alert.py",
    "deploy/session_secret_env.py",
    "deploy/smoke_readonly.py",
    "deploy/start_guard.py",
    "deploy/upgrade.sh",
    "deploy/upgrade_release.py",
    "deploy/verify_backup.py",
    "deploy/verify_release.py",
    "deploy/wheelhouse.py",
)

ATTESTATION_KEYS = frozenset(
    {
        "artifact_archive_sha256",
        "artifact_attestation_sha256",
        "attested_at",
        "format",
        "launcher_sha256",
        "manifest_sha256",
        "materialized_attestation_sha256",
        "ok",
        "ops_digest",
        "ops_file_count",
        "payload_tree_sha256",
        "release_dir",
        "release_id",
        "schema_version",
        "source_tree_sha256",
        "state_dir",
        "venv_bytes",
        "venv_entry_count",
        "venv_tree_sha256",
        "wheelhouse_attestation_path",
        "wheelhouse_attestation_sha256",
        "wheelhouse_hashed_lock_sha256",
        "wheelhouse_source_lock_sha256",
        "wheelhouse_total_bytes",
        "wheelhouse_wheel_count",
        "wheelhouse_wheel_set_sha256",
    }
)


class BootstrapError(RuntimeError):
    """A release cannot be admitted into the root execution trust boundary."""


@dataclass(frozen=True)
class Layout:
    control_root: Path = Path("/var/lib/paihuo-upgrade")
    releases_root: Path = Path("/srv/paihuo/releases")
    state_dir: Path = Path("/var/lib/paihuo/data")
    trusted_verifier: Path = Path(
        "/var/lib/paihuo-upgrade/bootstrap/verify_release.py"
    )
    trusted_bootstrap: Path = Path(
        "/var/lib/paihuo-upgrade/bootstrap/bootstrap_release.py"
    )
    trusted_wheelhouse: Path = Path(
        "/var/lib/paihuo-upgrade/bootstrap/wheelhouse.py"
    )
    wheelhouse_root: Path = Path("/var/cache/paihuo-wheelhouse")
    python: Path = Path("/usr/bin/python3")


def _owner_uid() -> int:
    return 0 if os.geteuid() == 0 else os.geteuid()


def _owner_gid() -> int:
    return 0 if os.geteuid() == 0 else os.getegid()


def _assert_secure_ancestors(
    path: Path,
    *,
    label: str,
    allowed_uids: set[int] | None = None,
) -> None:
    """Prove every directory used to reach *path* is rename-safe.

    Candidate artifacts are intentionally opened by absolute path.  A secure
    leaf is not enough if any directory above it can be renamed by another
    account, so every ancestor must be a real directory owned by root or by
    the current trusted operator and must not be group/other writable.
    """
    safe_uids = {0, _owner_uid()} if allowed_uids is None else allowed_uids

    def check_chain(start: Path, *, lexical: bool) -> None:
        current = start
        while True:
            try:
                metadata = os.lstat(current)
            except FileNotFoundError as exc:
                raise BootstrapError(f"{label} ancestor is missing") from exc
            if stat.S_ISLNK(metadata.st_mode):
                # Some platforms expose immutable root aliases such as /var.
                # A caller-owned symlink is never accepted as a trust ancestor.
                _require(
                    lexical and metadata.st_uid == 0,
                    f"{label} ancestor ownership or mode is unsafe",
                )
            else:
                _require(
                    stat.S_ISDIR(metadata.st_mode)
                    and metadata.st_uid in safe_uids
                    and not stat.S_IMODE(metadata.st_mode) & 0o022,
                    f"{label} ancestor ownership or mode is unsafe",
                )
            parent = current.parent
            if parent == current:
                break
            current = parent

    absolute = path.absolute()
    check_chain(absolute.parent, lexical=True)
    canonical = absolute.resolve(strict=False)
    if canonical != absolute:
        check_chain(canonical.parent, lexical=False)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BootstrapError(message)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_release_id(value: str) -> str:
    _require(
        isinstance(value, str) and RELEASE_ID_RE.fullmatch(value) is not None,
        "release id is unsafe",
    )
    return value


def _release_id_argument(value: str) -> str:
    try:
        return _safe_release_id(value)
    except BootstrapError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _assert_owner_dir(
    path: Path,
    *,
    label: str,
    exact_mode: int | None = 0o700,
    create: bool = False,
) -> Path:
    absolute = path.absolute()
    if create:
        absolute.mkdir(parents=True, exist_ok=True, mode=exact_mode or 0o700)
    _assert_secure_ancestors(absolute, label=label)
    try:
        before = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise BootstrapError(f"{label} is missing") from exc
    _require(
        stat.S_ISDIR(before.st_mode)
        and not stat.S_ISLNK(before.st_mode)
        and before.st_uid == _owner_uid()
        and before.st_gid == _owner_gid()
        and not stat.S_IMODE(before.st_mode) & 0o022
        and (
            exact_mode is None
            or stat.S_IMODE(before.st_mode) == exact_mode
        ),
        f"{label} ownership or mode is unsafe",
    )
    resolved = absolute.resolve(strict=True)
    after = os.lstat(resolved)
    _require(
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino,
        f"{label} changed while being resolved",
    )
    return resolved


def _service_identity() -> tuple[int, int]:
    """Return the production service identity, with a non-root test fallback."""
    if os.geteuid() != 0:
        return os.geteuid(), os.getegid()
    try:
        account = pwd.getpwnam("paihuo")
        group = grp.getgrnam("paihuo")
    except KeyError as exc:
        raise BootstrapError("paihuo service identity is missing") from exc
    _require(
        account.pw_gid == group.gr_gid,
        "paihuo service user and group disagree",
    )
    return account.pw_uid, group.gr_gid


def _assert_state_dir(path: Path, *, label: str) -> Path:
    """Validate the service-owned mutable state without trusting it as code."""
    absolute = path.absolute()
    service_uid, service_gid = _service_identity()
    _assert_secure_ancestors(
        absolute,
        label=label,
        allowed_uids={0, service_uid},
    )
    try:
        before = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise BootstrapError(f"{label} is missing") from exc
    _require(
        stat.S_ISDIR(before.st_mode)
        and not stat.S_ISLNK(before.st_mode)
        and before.st_uid == service_uid
        and before.st_gid == service_gid
        and stat.S_IMODE(before.st_mode) == 0o750,
        f"{label} ownership or mode is unsafe",
    )
    resolved = absolute.resolve(strict=True)
    after = os.lstat(resolved)
    _require(
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino,
        f"{label} changed while being resolved",
    )
    return resolved


def _secure_read(
    path: Path,
    *,
    label: str,
    maximum: int,
    expected_mode: int | None = None,
    require_owner: bool = True,
) -> tuple[bytes, os.stat_result]:
    absolute = path.absolute()
    _assert_secure_ancestors(absolute, label=label)
    try:
        before = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise BootstrapError(f"{label} is missing") from exc
    _require(
        stat.S_ISREG(before.st_mode) and not stat.S_ISLNK(before.st_mode),
        f"{label} must be a regular file, not a symlink",
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise BootstrapError(f"{label} could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        mode = stat.S_IMODE(opened.st_mode)
        _require(
            stat.S_ISREG(opened.st_mode)
            and opened.st_dev == before.st_dev
            and opened.st_ino == before.st_ino
            and opened.st_nlink == 1,
            f"{label} changed during secure open",
        )
        if require_owner:
            _require(
                opened.st_uid == _owner_uid()
                and opened.st_gid == _owner_gid(),
                f"{label} owner is unsafe",
            )
        if expected_mode is not None:
            _require(mode == expected_mode, f"{label} mode is unsafe")
        else:
            _require(not mode & 0o022, f"{label} mode is writable")
        _require(opened.st_size <= maximum, f"{label} exceeds size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65_536, maximum + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            _require(total <= maximum, f"{label} exceeds size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        _require(
            after.st_dev == opened.st_dev
            and after.st_ino == opened.st_ino
            and after.st_mode == opened.st_mode
            and after.st_uid == opened.st_uid
            and after.st_gid == opened.st_gid
            and after.st_nlink == opened.st_nlink
            and after.st_size == opened.st_size
            and after.st_mtime_ns == opened.st_mtime_ns
            and after.st_ctime_ns == opened.st_ctime_ns,
            f"{label} changed while being read",
        )
        return b"".join(chunks), opened
    finally:
        os.close(descriptor)


def _decode_canonical_json(body: bytes, *, label: str) -> dict:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"{label} is not valid JSON") from exc
    _require(isinstance(decoded, dict), f"{label} must be a JSON object")
    _require(
        _canonical_json(decoded) == body,
        f"{label} is not canonical JSON",
    )
    return decoded


def _trusted_tool(path: Path, *, label: str) -> Path:
    parent = _assert_owner_dir(
        path.absolute().parent,
        label=f"{label} parent",
        exact_mode=0o700,
    )
    absolute = parent / path.name
    _secure_read(
        absolute,
        label=label,
        maximum=MAX_OPS_FILE_BYTES,
        expected_mode=0o600,
    )
    return absolute


def _require_trusted_cli_entry(layout: Layout) -> Path:
    """Bind the CLI process to the out-of-band root-only bootstrap inode."""
    _require(os.geteuid() == 0, "bootstrap CLI must run as root")
    trusted = _trusted_tool(
        Path(layout.trusted_bootstrap),
        label="trusted bootstrap tool",
    )
    trusted_body, trusted_metadata = _secure_read(
        trusted,
        label="trusted bootstrap tool",
        maximum=MAX_OPS_FILE_BYTES,
        expected_mode=0o600,
    )
    loaded = Path(__file__).absolute()
    loaded_body, loaded_metadata = _secure_read(
        loaded,
        label="loaded bootstrap module",
        maximum=MAX_OPS_FILE_BYTES,
        expected_mode=0o600,
    )
    _require(
        loaded.resolve(strict=True) == trusted.resolve(strict=True)
        and loaded_metadata.st_dev == trusted_metadata.st_dev
        and loaded_metadata.st_ino == trusted_metadata.st_ino
        and _sha256(loaded_body) == _sha256(trusted_body),
        "loaded bootstrap module is not the trusted fixed tool",
    )
    return trusted


def _minimal_environment() -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _system_python(layout: Layout) -> Path:
    supplied_python = Path(layout.python).absolute()
    try:
        python = supplied_python.resolve(strict=True)
        python_info = python.lstat()
    except (OSError, RuntimeError) as exc:
        raise BootstrapError("trusted system Python is unsafe") from exc
    production_owner_ok = (
        python_info.st_uid == 0
        and python_info.st_gid == 0
        and not stat.S_IMODE(python_info.st_mode) & 0o022
    )
    local_test_owner_ok = os.geteuid() != 0 and os.access(python, os.X_OK)
    _require(
        python.is_file()
        and not python.is_symlink()
        and stat.S_ISREG(python_info.st_mode)
        and (production_owner_ok or local_test_owner_ok)
        and os.access(python, os.X_OK),
        "trusted system Python is unsafe",
    )
    return python


def _run_verifier(
    arguments: Sequence[str],
    *,
    layout: Layout,
) -> dict:
    verifier = _trusted_tool(
        Path(layout.trusted_verifier),
        label="trusted verifier",
    )
    python = _system_python(layout)
    try:
        completed = subprocess.run(
            [str(python), "-I", str(verifier), *arguments],
            cwd="/",
            env=_minimal_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError("trusted verifier could not be executed") from exc
    _require(
        completed.returncode == 0,
        "trusted verifier rejected release evidence",
    )
    _require(
        len(completed.stdout) <= MAX_JSON_BYTES,
        "trusted verifier output exceeds size limit",
    )
    try:
        report = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("trusted verifier returned invalid JSON") from exc
    _require(
        isinstance(report, dict) and report.get("ok") is True,
        "trusted verifier did not prove success",
    )
    return report


def _run_wheelhouse_verifier(
    wheelhouse: Path,
    *,
    layout: Layout,
) -> dict:
    """Run only the out-of-band wheelhouse verifier with a clean interpreter."""
    tool = _trusted_tool(
        Path(layout.trusted_wheelhouse),
        label="trusted wheelhouse tool",
    )
    python = _system_python(layout)
    try:
        completed = subprocess.run(
            [
                str(python),
                "-I",
                str(tool),
                "--verify",
                "--output",
                str(wheelhouse),
            ],
            cwd="/",
            env=_minimal_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError(
            "trusted wheelhouse verifier could not be executed"
        ) from exc
    _require(
        completed.returncode == 0
        and len(completed.stdout) <= MAX_JSON_BYTES,
        "trusted wheelhouse verifier rejected sealed dependencies",
    )
    try:
        report = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(
            "trusted wheelhouse verifier returned invalid JSON"
        ) from exc
    _require(
        isinstance(report, dict)
        and report.get("format") == WHEELHOUSE_FORMAT
        and report.get("status") == "verified",
        "trusted wheelhouse verifier did not prove success",
    )
    return report


def _fixed_paths(release_id: str, layout: Layout) -> dict[str, Path]:
    release_id = _safe_release_id(release_id)
    control = _assert_owner_dir(
        Path(layout.control_root),
        label="control root",
        exact_mode=0o700,
    )
    bootstrap = _assert_owner_dir(
        control / "bootstrap",
        label="bootstrap root",
        exact_mode=0o700,
    )
    incoming_root = _assert_owner_dir(
        control / "incoming",
        label="incoming root",
        exact_mode=0o700,
    )
    incoming = _assert_owner_dir(
        incoming_root / release_id,
        label="release incoming directory",
        exact_mode=0o700,
    )
    releases_root = _assert_owner_dir(
        Path(layout.releases_root),
        label="releases root",
        exact_mode=None,
    )
    release = releases_root / release_id
    release_resolved = _assert_owner_dir(
        release,
        label="materialized release",
        exact_mode=None,
    )
    _require(
        release_resolved == release.absolute(),
        "materialized release path is not canonical",
    )
    state = _assert_state_dir(
        Path(layout.state_dir),
        label="state directory",
    )
    wheelhouse_root = _assert_owner_dir(
        Path(layout.wheelhouse_root),
        label="wheelhouse root",
        exact_mode=0o755,
    )
    wheelhouse = _assert_owner_dir(
        wheelhouse_root / release_id,
        label="sealed release wheelhouse",
        exact_mode=0o555,
    )
    stage_parent = _assert_owner_dir(
        bootstrap / "releases",
        label="bootstrap releases directory",
        exact_mode=0o700,
        create=True,
    )
    return {
        "archive": incoming / f"{release_id}.tar.gz",
        "artifact": incoming / "release-artifact-attestation.json",
        "bootstrap": bootstrap,
        "incoming": incoming,
        "manifest": release_resolved / "RELEASE-MANIFEST.json",
        "materialized": incoming / "materialized-release-attestation.json",
        "receipt": incoming / f"{release_id}.receipt.json",
        "release": release_resolved,
        "stage": stage_parent / release_id,
        "stage_parent": stage_parent,
        "state": state,
        "wheelhouse": wheelhouse,
        "wheelhouse_attestation": (
            wheelhouse / "wheelhouse-attestation.json"
        ),
    }


def _verify_reports(paths: dict[str, Path], *, layout: Layout) -> tuple[dict, dict]:
    artifact = _run_verifier(
        [
            "--archive",
            str(paths["archive"]),
            "--receipt",
            str(paths["receipt"]),
            "--artifact-attestation",
            str(paths["artifact"]),
        ],
        layout=layout,
    )
    materialized = _run_verifier(
        [
            "--materialized-release",
            str(paths["release"]),
            "--state",
            str(paths["state"]),
            "--artifact-attestation",
            str(paths["artifact"]),
            "--materialized-attestation",
            str(paths["materialized"]),
        ],
        layout=layout,
    )
    release_id = paths["release"].name
    identity = (
        "manifest_sha256",
        "payload_tree_sha256",
        "release_id",
        "schema_version",
        "source_tree_sha256",
    )
    for key in identity:
        _require(
            artifact.get(key) == materialized.get(key),
            "artifact and materialized identities disagree",
        )
    _require(
        artifact.get("format") == ARTIFACT_FORMAT
        and materialized.get("format") == MATERIALIZED_FORMAT
        and artifact.get("release_id") == release_id
        and Path(str(artifact.get("extract_dir") or "")).resolve(strict=False)
        == paths["release"]
        and Path(
            str(materialized.get("materialized_release") or "")
        ).resolve(strict=False)
        == paths["release"]
        and Path(str(materialized.get("data_target") or "")).resolve(
            strict=False
        )
        == paths["state"],
        "release evidence paths or formats are inconsistent",
    )
    for report in (artifact, materialized):
        for key in (
            "manifest_sha256",
            "payload_tree_sha256",
            "source_tree_sha256",
        ):
            _require(
                isinstance(report.get(key), str)
                and SHA256_RE.fullmatch(report[key]) is not None,
                "release evidence contains an invalid digest",
            )
    return artifact, materialized


def _manifest(paths: dict[str, Path], release_id: str) -> tuple[dict, dict[str, dict], bytes]:
    body, _ = _secure_read(
        paths["manifest"],
        label="release manifest",
        maximum=MAX_JSON_BYTES,
        expected_mode=0o644,
    )
    manifest = _decode_canonical_json(body, label="release manifest")
    _require(
        manifest.get("format") == MANIFEST_FORMAT
        and manifest.get("release_id") == release_id,
        "release manifest identity is invalid",
    )
    entries = manifest.get("entries")
    _require(isinstance(entries, list), "release manifest entries are invalid")
    by_path: dict[str, dict] = {}
    for entry in entries:
        _require(
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and entry["path"] not in by_path,
            "release manifest contains invalid or duplicate entries",
        )
        by_path[entry["path"]] = entry
    return manifest, by_path, body


def _expected_op(
    release: Path,
    relative: str,
    entries: dict[str, dict],
) -> tuple[bytes, dict]:
    _require(
        relative in OPS_FILES
        and "\\" not in relative
        and PurePosixPath(relative).as_posix() == relative,
        "fixed ops path is unsafe",
    )
    record = entries.get(relative)
    expected_mode = 0o755 if relative == "deploy/upgrade.sh" else 0o644
    _require(
        isinstance(record, dict)
        and record.get("type") == "file"
        and record.get("mode") == f"{expected_mode:04o}"
        and isinstance(record.get("size"), int)
        and isinstance(record.get("sha256"), str)
        and SHA256_RE.fullmatch(record["sha256"]) is not None,
        f"manifest fixed ops entry is invalid: {relative}",
    )
    body, metadata = _secure_read(
        release / PurePosixPath(relative),
        label=f"candidate fixed op {relative}",
        maximum=MAX_OPS_FILE_BYTES,
        expected_mode=expected_mode,
    )
    _require(
        metadata.st_size == record["size"]
        and _sha256(body) == record["sha256"],
        f"candidate fixed op content differs from manifest: {relative}",
    )
    return body, record


def _expected_payload_file(
    release: Path,
    relative: str,
    entries: dict[str, dict],
    *,
    maximum: int,
) -> tuple[bytes, dict]:
    _require(
        "\\" not in relative
        and PurePosixPath(relative).as_posix() == relative
        and not PurePosixPath(relative).is_absolute(),
        "release payload path is unsafe",
    )
    record = entries.get(relative)
    _require(
        isinstance(record, dict)
        and record.get("type") == "file"
        and record.get("mode") == "0644"
        and isinstance(record.get("size"), int)
        and isinstance(record.get("sha256"), str)
        and SHA256_RE.fullmatch(record["sha256"]) is not None,
        f"manifest payload entry is invalid: {relative}",
    )
    body, metadata = _secure_read(
        release / PurePosixPath(relative),
        label=f"candidate payload {relative}",
        maximum=maximum,
        expected_mode=0o644,
    )
    _require(
        metadata.st_size == record["size"]
        and _sha256(body) == record["sha256"],
        f"candidate payload content differs from manifest: {relative}",
    )
    return body, record


def _wheelhouse_evidence(
    paths: dict[str, Path],
    *,
    lock_body: bytes,
    layout: Layout,
) -> dict:
    report = _run_wheelhouse_verifier(paths["wheelhouse"], layout=layout)
    digests = (
        "hashed_lock_sha256",
        "source_lock_sha256",
        "wheel_set_sha256",
    )
    _require(
        all(
            isinstance(report.get(key), str)
            and SHA256_RE.fullmatch(str(report[key])) is not None
            for key in digests
        )
        and report.get("source_lock_sha256") == _sha256(lock_body)
        and isinstance(report.get("wheel_count"), int)
        and report["wheel_count"] > 0
        and isinstance(report.get("total_bytes"), int)
        and report["total_bytes"] > 0,
        "sealed wheelhouse is not bound to the release dependency lock",
    )
    attestation_body, _ = _secure_read(
        paths["wheelhouse_attestation"],
        label="wheelhouse attestation",
        maximum=MAX_JSON_BYTES,
        expected_mode=0o444,
    )
    attestation = _decode_canonical_json(
        attestation_body,
        label="wheelhouse attestation",
    )
    _require(
        attestation.get("format") == WHEELHOUSE_FORMAT
        and all(attestation.get(key) == report.get(key) for key in digests)
        and attestation.get("wheel_count") == report.get("wheel_count")
        and attestation.get("total_bytes") == report.get("total_bytes"),
        "wheelhouse attestation differs from trusted verification",
    )
    return {
        "wheelhouse_attestation_path": str(
            paths["wheelhouse_attestation"]
        ),
        "wheelhouse_attestation_sha256": _sha256(attestation_body),
        "wheelhouse_hashed_lock_sha256": report["hashed_lock_sha256"],
        "wheelhouse_source_lock_sha256": report["source_lock_sha256"],
        "wheelhouse_total_bytes": report["total_bytes"],
        "wheelhouse_wheel_count": report["wheel_count"],
        "wheelhouse_wheel_set_sha256": report["wheel_set_sha256"],
    }


def validate_dependency_closure(
    release: str | os.PathLike[str],
    ops_files: Sequence[str] = OPS_FILES,
) -> dict:
    """Reject fixed modules that import candidate-local code outside the bundle."""
    root = Path(release).resolve(strict=True)
    allowed = set(ops_files)
    checked = 0
    for relative in ops_files:
        if not relative.endswith(".py"):
            continue
        body, _ = _secure_read(
            root / PurePosixPath(relative),
            label=f"fixed dependency source {relative}",
            maximum=MAX_OPS_FILE_BYTES,
            expected_mode=0o644,
        )
        try:
            tree = ast.parse(body, filename=relative)
        except SyntaxError as exc:
            raise BootstrapError(
                f"fixed dependency source is invalid: {relative}"
            ) from exc
        checked += 1
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    raise BootstrapError(
                        f"relative dependency is not allowed: {relative}"
                    )
                if node.module:
                    modules.append(node.module)
            for module in modules:
                if module == "app" or module.startswith("app."):
                    candidate = module.replace(".", "/") + ".py"
                elif module == "deploy" or module.startswith("deploy."):
                    candidate = module.replace(".", "/") + ".py"
                else:
                    continue
                if candidate in {"app.py", "deploy.py"}:
                    candidate = candidate[:-3] + "/__init__.py"
                _require(
                    candidate in allowed,
                    f"fixed dependency escapes allowlist: {module}",
                )
    return {"checked_python_files": checked, "ok": True}


def _venv_tree(venv: Path) -> dict:
    root = _assert_owner_dir(
        venv,
        label="release venv",
        exact_mode=None,
    )
    digest = hashlib.sha256()
    pending = [root]
    count = 0
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise BootstrapError("release venv could not be traversed") from exc
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            metadata = child.stat(follow_symlinks=False)
            count += 1
            _require(count <= MAX_VENV_ENTRIES, "release venv has too many entries")
            _require(
                metadata.st_uid == _owner_uid()
                and metadata.st_gid == _owner_gid()
                and not stat.S_IMODE(metadata.st_mode) & 0o022,
                "release venv ownership or mode is unsafe",
            )
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(f"{stat.S_IMODE(metadata.st_mode):04o}".encode("ascii"))
            digest.update(b"\0")
            if stat.S_ISDIR(metadata.st_mode):
                digest.update(b"D\0")
                pending.append(path)
                continue
            _require(
                stat.S_ISREG(metadata.st_mode)
                and not stat.S_ISLNK(metadata.st_mode)
                and metadata.st_nlink == 1,
                "release venv contains a symlink, hardlink, or special file",
            )
            body, opened = _secure_read(
                path,
                label=f"release venv file {relative}",
                maximum=MAX_VENV_FILE_BYTES,
                expected_mode=stat.S_IMODE(metadata.st_mode),
            )
            total_bytes += len(body)
            _require(
                total_bytes <= MAX_VENV_TOTAL_BYTES,
                "release venv exceeds total size limit",
            )
            _require(
                opened.st_dev == metadata.st_dev
                and opened.st_ino == metadata.st_ino,
                "release venv entry changed during traversal",
            )
            digest.update(b"F\0")
            digest.update(str(len(body)).encode("ascii"))
            digest.update(b"\0")
            digest.update(_sha256(body).encode("ascii"))
            digest.update(b"\0")
    return {
        "venv_bytes": total_bytes,
        "venv_entry_count": count,
        "venv_tree_sha256": digest.hexdigest(),
    }


def _ops_digest(records: dict[str, dict]) -> str:
    return _sha256(_canonical_json(records))


def _live_evidence(release_id: str, layout: Layout) -> tuple[dict, dict[str, bytes]]:
    paths = _fixed_paths(release_id, layout)
    artifact, materialized = _verify_reports(paths, layout=layout)
    manifest, entries, manifest_body = _manifest(paths, release_id)
    _require(
        _sha256(manifest_body) == artifact.get("manifest_sha256"),
        "release manifest digest differs from artifact proof",
    )
    bodies: dict[str, bytes] = {}
    records: dict[str, dict] = {}
    for relative in OPS_FILES:
        body, record = _expected_op(paths["release"], relative, entries)
        bodies[relative] = body
        records[relative] = {
            "mode": record["mode"],
            "sha256": record["sha256"],
            "size": record["size"],
        }
    validate_dependency_closure(paths["release"], OPS_FILES)
    lock_body, _ = _expected_payload_file(
        paths["release"],
        "requirements.lock.txt",
        entries,
        maximum=MAX_OPS_FILE_BYTES,
    )
    wheelhouse = _wheelhouse_evidence(
        paths,
        lock_body=lock_body,
        layout=layout,
    )
    venv = _venv_tree(paths["release"] / "venv")
    artifact_body, _ = _secure_read(
        paths["artifact"],
        label="artifact attestation",
        maximum=MAX_JSON_BYTES,
        expected_mode=0o600,
    )
    materialized_body, _ = _secure_read(
        paths["materialized"],
        label="materialized attestation",
        maximum=MAX_JSON_BYTES,
        expected_mode=0o600,
    )
    report = {
        "artifact_archive_sha256": artifact["archive_sha256"],
        "artifact_attestation_sha256": _sha256(artifact_body),
        "attested_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "format": BOOTSTRAP_FORMAT,
        "launcher_sha256": records["deploy/upgrade.sh"]["sha256"],
        "manifest_sha256": artifact["manifest_sha256"],
        "materialized_attestation_sha256": _sha256(materialized_body),
        "ok": True,
        "ops_digest": _ops_digest(records),
        "ops_file_count": len(records),
        "payload_tree_sha256": manifest["payload_tree_sha256"],
        "release_dir": str(paths["release"]),
        "release_id": release_id,
        "schema_version": manifest["schema_version"],
        "source_tree_sha256": manifest["source_tree_sha256"],
        "state_dir": str(paths["state"]),
        **wheelhouse,
        **venv,
    }
    return report, bodies


def _write_new(
    path: Path,
    body: bytes,
    *,
    mode: int,
) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, _owner_uid(), _owner_gid())
        offset = 0
        while offset < len(body):
            offset += os.write(descriptor, body[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_expected_paths(stage: Path) -> set[str]:
    expected = {"attestation.json", "ops", "paihuo-upgrade"}
    for relative in OPS_FILES:
        parts = PurePosixPath("ops", relative).parts
        for index in range(1, len(parts)):
            expected.add(PurePosixPath(*parts[:index]).as_posix())
        expected.add(PurePosixPath(*parts).as_posix())
    return expected


def _assert_no_stage_extras(stage: Path) -> None:
    expected = _stage_expected_paths(stage)
    seen: set[str] = set()
    pending = [stage]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(stage).as_posix()
            metadata = child.stat(follow_symlinks=False)
            _require(
                relative in expected,
                "bootstrap stage contains an unexpected entry",
            )
            seen.add(relative)
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
    _require(seen == expected, "bootstrap stage is incomplete")


def _verify_stage_files(
    stage: Path,
    *,
    bodies: dict[str, bytes],
    report: dict,
) -> None:
    _assert_owner_dir(
        stage,
        label="bootstrap release stage",
        exact_mode=0o700,
    )
    attestation_body, _ = _secure_read(
        stage / "attestation.json",
        label="bootstrap attestation",
        maximum=MAX_JSON_BYTES,
        expected_mode=0o600,
    )
    attestation = _decode_canonical_json(
        attestation_body,
        label="bootstrap attestation",
    )
    _require(
        set(attestation) == ATTESTATION_KEYS
        and attestation.get("format") == BOOTSTRAP_FORMAT
        and attestation.get("ok") is True,
        "bootstrap attestation fields are invalid",
    )
    for key, value in report.items():
        if key == "attested_at":
            continue
        _require(
            attestation.get(key) == value,
            f"bootstrap attestation does not match live {key}",
        )
    records: dict[str, dict] = {}
    for relative, expected_body in bodies.items():
        mode = 0o755 if relative == "deploy/upgrade.sh" else 0o644
        body, metadata = _secure_read(
            stage / "ops" / PurePosixPath(relative),
            label=f"staged fixed op {relative}",
            maximum=MAX_OPS_FILE_BYTES,
            expected_mode=mode,
        )
        _require(body == expected_body, "staged fixed op content changed")
        records[relative] = {
            "mode": f"{mode:04o}",
            "sha256": _sha256(body),
            "size": metadata.st_size,
        }
    _require(
        _ops_digest(records) == attestation["ops_digest"],
        "staged fixed ops digest changed",
    )
    launcher, _ = _secure_read(
        stage / "paihuo-upgrade",
        label="bootstrap launcher",
        maximum=MAX_OPS_FILE_BYTES,
        expected_mode=0o755,
    )
    _require(
        launcher == bodies["deploy/upgrade.sh"]
        and _sha256(launcher) == attestation["launcher_sha256"],
        "bootstrap launcher content changed",
    )
    _assert_no_stage_extras(stage)


def prepare_stage(
    release_id: str,
    *,
    layout: Layout = Layout(),
) -> dict:
    """Create one immutable root-only launcher bundle, or recheck an existing one."""
    release_id = _safe_release_id(release_id)
    paths = _fixed_paths(release_id, layout)
    if paths["stage"].exists() or paths["stage"].is_symlink():
        return check_stage(release_id, layout=layout)
    report, bodies = _live_evidence(release_id, layout)
    staged = paths["stage_parent"] / (
        f".{release_id}.stage-{uuid.uuid4().hex}"
    )
    staged.mkdir(mode=0o700)
    os.chown(staged, _owner_uid(), _owner_gid())
    try:
        ops = staged / "ops"
        ops.mkdir(mode=0o700)
        os.chown(ops, _owner_uid(), _owner_gid())
        created_dirs = {ops}
        for relative, body in bodies.items():
            destination = ops / PurePosixPath(relative)
            missing: list[Path] = []
            parent = destination.parent
            while parent != ops and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir(mode=0o700)
                os.chown(directory, _owner_uid(), _owner_gid())
                created_dirs.add(directory)
            mode = 0o755 if relative == "deploy/upgrade.sh" else 0o644
            _write_new(destination, body, mode=mode)
            _fsync_dir(destination.parent)
        _write_new(
            staged / "paihuo-upgrade",
            bodies["deploy/upgrade.sh"],
            mode=0o755,
        )
        _write_new(
            staged / "attestation.json",
            _canonical_json(report),
            mode=0o600,
        )
        for directory in sorted(
            created_dirs,
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            _fsync_dir(directory)
        _fsync_dir(staged)
        _verify_stage_files(staged, bodies=bodies, report=report)
        os.replace(staged, paths["stage"])
        _fsync_dir(paths["stage_parent"])
    except BaseException:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        raise
    checked = check_stage(release_id, layout=layout)
    return {**checked, "status": "created"}


def check_stage(
    release_id: str,
    *,
    layout: Layout = Layout(),
) -> dict:
    """Recompute live artifact, source and venv evidence before root execution."""
    release_id = _safe_release_id(release_id)
    paths = _fixed_paths(release_id, layout)
    report, bodies = _live_evidence(release_id, layout)
    _verify_stage_files(paths["stage"], bodies=bodies, report=report)
    checked = {
        "launcher": str(paths["stage"] / "paihuo-upgrade"),
        "ops_root": str(paths["stage"] / "ops"),
        "release_id": release_id,
        "status": "verified_existing",
        "venv_tree_sha256": report["venv_tree_sha256"],
    }
    for key in (
        "launcher_sha256",
        "ops_digest",
        "wheelhouse_attestation_path",
        "wheelhouse_attestation_sha256",
        "wheelhouse_hashed_lock_sha256",
        "wheelhouse_source_lock_sha256",
        "wheelhouse_total_bytes",
        "wheelhouse_wheel_count",
        "wheelhouse_wheel_set_sha256",
    ):
        checked[key] = report[key]
    return checked


def launch_stage(
    release_id: str,
    *,
    layout: Layout = Layout(),
    execve: Callable[[str, Sequence[str], dict[str, str]], object] = os.execve,
) -> object:
    """Check the stage with the out-of-band helper, then replace this process."""
    release_id = _safe_release_id(release_id)
    _trusted_tool(
        Path(layout.trusted_bootstrap),
        label="trusted bootstrap tool",
    )
    checked = check_stage(release_id, layout=layout)
    launcher = checked["launcher"]
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PAIHUO_BOOTSTRAP_LAUNCHED": "1",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": checked["ops_root"],
    }
    return execve(
        launcher,
        [launcher, "--release-id", release_id],
        environment,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Prepare, verify or enter a trusted Paihuo bootstrap stage.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare-stage", action="store_true")
    action.add_argument("--check-stage", action="store_true")
    action.add_argument("--launch", action="store_true")
    parser.add_argument("--release-id", required=True, type=_release_id_argument)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        _require_trusted_cli_entry(Layout())
        args = _parser().parse_args(argv)
        if args.prepare_stage:
            report = prepare_stage(args.release_id)
        elif args.check_stage:
            report = check_stage(args.release_id)
        else:
            launch_stage(args.release_id)
            raise BootstrapError("bootstrap launcher unexpectedly returned")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except BootstrapError as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "status": "failed"},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
