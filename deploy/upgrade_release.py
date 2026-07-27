#!/usr/bin/env python3
"""Crash-recoverable, fail-closed Paihuo immutable-release upgrade.

The only rollback point is a verified SQLite snapshot taken *after* the old
service has stopped. Upgrade metadata, locks and that snapshot live in a
root-owned control directory which the application user cannot write.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.error import URLError
from urllib.request import urlopen
import uuid

from app.session_secret import CONFIG_KEY_ENV, SESSION_SECRET_ENV
from deploy.backup_db import BackupError, backup_database
from deploy.backup_health import BackupHealthError, check_backup_health, check_disk_capacity
from deploy.bootstrap_release import (
    BOOTSTRAP_FORMAT,
    BootstrapError,
    check_stage as verify_bootstrap_stage,
)
from deploy.control_plane import (
    ControlPlaneError,
    attestation_digest as control_plane_attestation_digest,
    verify_installed_attestation,
)
from deploy.session_secret_env import (
    DEFAULT_SECRET_KEYS,
    SecretEnvError,
    _read_validated_secret_file,
    check_secret_backup,
)
from deploy.verify_backup import VerificationError, restore_drill, verify_database
from deploy.verify_release import (
    ReleaseVerifyError,
    verify_materialized_attestation,
    verify_release_artifact,
)


UTC = timezone.utc
SAFE_RELEASE_RE = re.compile(r"[^A-Za-z0-9._-]+")
TERMINAL_STATUSES = {
    "succeeded",
    "rolled_back",
    "recovered_rollback",
    "failed_before_cutover",
    "failed_before_cutover_recovered",
    "checkpoint_ready",
}
PHASES = (
    "initialized",
    "preflight_complete",
    "stopping_previous",
    "previous_stopped",
    "final_checkpoint_ready",
    "release_runtime_validation",
    "switching_candidate",
    "candidate_selected",
    "quiescent_starting",
    "quiescent_verified",
    "validation_starting",
    "validation_healthy",
    "cutover_committed",
    "normal_starting",
    "normal_healthy",
    "proxy_starting",
)


def _constant_time_text_equal(left: object, right: object) -> bool:
    """Compare arbitrary Unicode evidence values without leaking contents."""
    return hmac.compare_digest(
        str(left).encode("utf-8"),
        str(right).encode("utf-8"),
    )
SAFE_PREFLIGHT_ENV = {
    "CONTENTCREW_PUBLIC_DIR",
    "CONTENTCREW_DB_PATH",
    "CONTENTCREW_FFMPEG_PATH",
    "CONTENTCREW_CLAUDE_PATH",
    "PLAYWRIGHT_BROWSERS_PATH",
}
REQUIRE_SESSION_SECRET_ENV = "CONTENTCREW_REQUIRE_SESSION_SECRET"
REQUIRE_CONFIG_KEY_ENV = "CONTENTCREW_REQUIRE_CONFIG_KEY"
LOCK_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)$"
)
VENV_BOOTSTRAP_DISTRIBUTIONS = frozenset({"pip", "setuptools", "wheel"})


class UpgradeError(RuntimeError):
    """The release could not be proven safe."""


class UpgradeInterrupted(UpgradeError):
    """SIGINT/SIGTERM was converted into a recoverable upgrade failure."""


def _fsync_directory(path: Path) -> None:
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


def _owner_uid() -> int:
    return 0 if os.geteuid() == 0 else os.geteuid()


def _assert_private_regular(
    descriptor: int,
    path: Path,
    *,
    exact_mode: int | None = None,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UpgradeError(f"not a private regular file: {path}")
    if metadata.st_uid != _owner_uid():
        raise UpgradeError(f"file has an untrusted owner: {path}")
    permissions = stat.S_IMODE(metadata.st_mode)
    if exact_mode is not None and permissions != exact_mode:
        raise UpgradeError(
            f"file permissions must be {exact_mode:04o}: {path}"
        )
    return metadata


def _secure_control_directory(
    path: str | os.PathLike[str],
    *,
    state: Path | None = None,
    service_user: str | None = None,
) -> Path:
    supplied = Path(path).absolute()
    if supplied.is_symlink():
        raise UpgradeError(f"control directory must not be a symlink: {supplied}")
    supplied.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = supplied.resolve(strict=True)
    metadata = resolved.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise UpgradeError(f"control path is not a directory: {resolved}")
    if metadata.st_uid != _owner_uid():
        raise UpgradeError(f"control directory has an untrusted owner: {resolved}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise UpgradeError(f"control directory must be mode 0700: {resolved}")
    trusted_owners = {_owner_uid(), 0}
    for ancestor in resolved.parents:
        ancestor_metadata = ancestor.lstat()
        if (
            ancestor_metadata.st_uid not in trusted_owners
            or stat.S_IMODE(ancestor_metadata.st_mode) & 0o022
        ):
            raise UpgradeError(
                f"control directory ancestor is not owner-controlled: {ancestor}"
            )
    if state is not None:
        state_real = state.resolve(strict=True)
        if resolved == state_real or state_real in resolved.parents:
            raise UpgradeError("control directory must be outside application state")
    if os.geteuid() == 0 and service_user:
        try:
            application_uid = pwd.getpwnam(service_user).pw_uid
        except KeyError as exc:
            raise UpgradeError(f"service user does not exist: {service_user}") from exc
        if application_uid == metadata.st_uid:
            raise UpgradeError("application user must not own the upgrade control directory")
    descriptor = os.open(
        resolved,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    os.close(descriptor)
    return resolved


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise UpgradeError(f"receipt parent is unsafe: {path.parent}")
    if path.is_symlink():
        raise UpgradeError(f"receipt path must not be a symlink: {path}")
    staged = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    descriptor = os.open(
        staged,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _assert_private_regular(descriptor, staged)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        verify_fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            _assert_private_regular(verify_fd, path, exact_mode=0o600)
        finally:
            os.close(verify_fd)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        _assert_private_regular(descriptor, path, exact_mode=0o600)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise UpgradeError(f"receipt is not a JSON object: {path}")
    return value


@contextmanager
def _upgrade_lock(path: Path) -> Iterator[None]:
    descriptor = os.open(
        path,
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _assert_private_regular(descriptor, path)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpgradeError(f"another upgrade is active: {path}") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def _interrupt_guard() -> Iterator[None]:
    if not hasattr(signal, "SIGTERM"):
        yield
        return
    previous: dict[int, Any] = {}

    def interrupted(signum: int, _frame: Any) -> None:
        raise UpgradeInterrupted(f"upgrade interrupted by signal {signum}")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupted)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _release_root(path: str | os.PathLike[str]) -> Path:
    supplied = Path(path).absolute()
    if supplied.is_symlink():
        raise UpgradeError(f"release must not be a symlink: {supplied}")
    try:
        metadata = supplied.lstat()
    except FileNotFoundError as exc:
        raise UpgradeError(f"release does not exist: {supplied}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise UpgradeError(f"release is not a directory: {supplied}")
    if metadata.st_uid != _owner_uid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise UpgradeError(
            f"release root must be owner-controlled and immutable: {supplied}"
        )
    trusted_owners = {_owner_uid(), 0}
    for ancestor in supplied.parents:
        ancestor_metadata = ancestor.lstat()
        if (
            ancestor_metadata.st_uid not in trusted_owners
            or stat.S_IMODE(ancestor_metadata.st_mode) & 0o022
        ):
            raise UpgradeError(
                f"release ancestor is not owner-controlled: {ancestor}"
            )
    return supplied.resolve(strict=True)


def _release_entries(root: Path) -> Iterator[tuple[Path, os.stat_result, str | None]]:
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name, reverse=True)
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root)
            metadata = entry.stat(follow_symlinks=False)
            if metadata.st_uid != _owner_uid():
                raise UpgradeError(f"release entry has an untrusted owner: {relative}")
            if stat.S_ISLNK(metadata.st_mode):
                if relative != Path("data"):
                    raise UpgradeError(f"release contains forbidden symlink: {relative}")
                yield relative, metadata, os.readlink(path)
            elif stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) & 0o022:
                    raise UpgradeError(f"release directory is writable: {relative}")
                yield relative, metadata, None
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) & 0o022:
                    raise UpgradeError(f"release file is writable: {relative}")
                yield relative, metadata, None
            else:
                raise UpgradeError(f"unsupported release entry: {relative}")


def release_digest(release: str | os.PathLike[str]) -> str:
    """Hash every controlled release entry; only the top-level data link is legal."""
    root = _release_root(release)
    digest = hashlib.sha256()
    entries = sorted(_release_entries(root), key=lambda item: item[0].as_posix())
    for relative, metadata, link_target in entries:
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(metadata.st_mode):o}".encode("ascii"))
        digest.update(b"\0")
        if link_target is not None:
            digest.update(b"L")
            digest.update(link_target.encode("utf-8"))
        elif stat.S_ISDIR(metadata.st_mode):
            digest.update(b"D")
        else:
            digest.update(b"F")
            path = root / relative
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                ):
                    raise UpgradeError(f"release file changed while hashing: {relative}")
                while True:
                    block = os.read(descriptor, 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
            finally:
                os.close(descriptor)
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_venv(release: Path, venv: Path) -> None:
    expected = release / "venv"
    if expected.is_symlink() or venv.is_symlink():
        raise UpgradeError("release virtualenv must be a real directory")
    if venv.resolve(strict=True) != expected.resolve(strict=True):
        raise UpgradeError(f"release must use its own virtualenv: {expected}")
    python = venv / "bin" / "python"
    if python.is_symlink() or not python.is_file() or not os.access(python, os.X_OK):
        raise UpgradeError(
            "release virtualenv python must be an executable regular copy, not a symlink"
        )
    ytdlp = venv / "bin" / "yt-dlp"
    if ytdlp.is_symlink() or not ytdlp.is_file() or not os.access(ytdlp, os.X_OK):
        raise UpgradeError(
            "release yt-dlp must be an executable regular file, not a symlink"
        )
    with ytdlp.open("rb") as handle:
        first_line = handle.readline(4096)
    if first_line.startswith(b"#!"):
        interpreter = first_line[2:].strip().split(maxsplit=1)[0]
        try:
            interpreter_path = Path(interpreter.decode("utf-8")).absolute()
        except UnicodeDecodeError as exc:
            raise UpgradeError("release yt-dlp has an invalid shebang") from exc
        if interpreter_path != python.absolute():
            raise UpgradeError(
                "release yt-dlp shebang does not point to this release venv"
            )


def _normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _parse_locked_versions(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise UpgradeError("candidate requirements lock is missing or unsafe")
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK_LINE_RE.fullmatch(line)
        if match is None:
            raise UpgradeError("requirements lock must use exact name==version pins")
        name = _normalize_distribution_name(match.group("name"))
        if name in result:
            raise UpgradeError("requirements lock contains duplicate packages")
        result[name] = match.group("version")
    if not result:
        raise UpgradeError("requirements lock contains no pinned packages")
    return result


def _compare_locked_versions(
    locked: dict[str, str], installed: dict[str, str]
) -> None:
    normalized = {
        _normalize_distribution_name(name): str(version)
        for name, version in installed.items()
    }
    missing = sorted(set(locked) - set(normalized))
    wrong = sorted(
        name for name, version in locked.items()
        if name in normalized and normalized[name] != version
    )
    unexpected = sorted(
        set(normalized) - set(locked) - VENV_BOOTSTRAP_DISTRIBUTIONS
    )
    if missing:
        raise UpgradeError("candidate runtime is missing locked packages")
    if wrong:
        raise UpgradeError("candidate runtime has locked package version mismatches")
    if unexpected:
        raise UpgradeError("candidate runtime has unexpected unlocked packages")


def _validate_locked_runtime(
    *,
    release: Path,
    venv: Path,
    service_user: str,
    environment: dict[str, str],
    command_runner: Callable[..., None],
) -> None:
    lock = release / "requirements.lock.txt"
    _parse_locked_versions(lock)
    script = (
        "import importlib.metadata as m,json,re,sys;"
        "p=sys.argv[1];"
        "norm=lambda s:re.sub(r'[-_.]+','-',s).lower();"
        "locked={};"
        "\nfor raw in open(p,encoding='utf-8'):\n"
        " line=raw.strip()\n"
        " if not line or line.startswith('#'): continue\n"
        " if line.count('==')!=1: raise SystemExit(20)\n"
        " name,version=line.split('==',1); key=norm(name)\n"
        " if key in locked or not version: raise SystemExit(21)\n"
        " locked[key]=version\n"
        "installed={norm(d.metadata['Name']):d.version for d in m.distributions() "
        "if d.metadata.get('Name')};"
        "missing=[k for k in locked if k not in installed];"
        "wrong=[k for k,v in locked.items() if installed.get(k)!=v];"
        "unexpected=[k for k in installed if k not in locked "
        "and k not in {'pip','setuptools','wheel'}];"
        "\nif missing or wrong or unexpected: raise SystemExit(22)\n"
        "import fastapi,sqlite3,uvicorn,cryptography,playwright,weasyprint;"
    )
    command_runner(
        _runuser_prefix(service_user)
        + [str(venv / "bin" / "python"), "-c", script, str(lock)],
        cwd=release,
        env=environment,
    )
    command_runner(
        _runuser_prefix(service_user)
        + [str(venv / "bin" / "python"), "-m", "pip", "check"],
        cwd=release,
        env=environment,
    )


def _candidate_schema_version(release: Path) -> int:
    manifest_path = release / "RELEASE-MANIFEST.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise UpgradeError("candidate release manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = manifest["schema_version"]
        release_id = manifest["release_id"]
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise UpgradeError("candidate release manifest is invalid") from exc
    if (
        not isinstance(version, int)
        or version <= 0
        or release_id != release.name
    ):
        raise UpgradeError("candidate release manifest identity/schema mismatch")
    return version


def _managed_candidate_secrets(path: Path) -> dict[str, str]:
    owner_uid = _owner_uid()
    owner_gid = 0 if os.geteuid() == 0 else os.getegid()
    try:
        body, _ = _read_validated_secret_file(
            path,
            keys=DEFAULT_SECRET_KEYS,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            label="environment",
        )
    except SecretEnvError as exc:
        raise UpgradeError("managed candidate environment is unsafe") from exc
    values: dict[str, str] = {}
    for raw in body.decode("utf-8").splitlines():
        key, marker, value = raw.partition("=")
        if marker and key.strip() in DEFAULT_SECRET_KEYS:
            values[key.strip()] = value
    if set(values) != set(DEFAULT_SECRET_KEYS):
        raise UpgradeError("managed candidate environment lacks required keys")
    if hmac.compare_digest(values[SESSION_SECRET_ENV], values[CONFIG_KEY_ENV]):
        raise UpgradeError("managed candidate secrets must be independent")
    return values


def _release_artifact_evidence(
    *,
    release: Path,
    state: Path,
    archive: Path,
    build_receipt: Path,
    artifact_attestation: Path,
    materialized_attestation: Path,
    artifact_verifier: Callable[..., dict[str, Any]],
    materialized_verifier: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    try:
        artifact = artifact_verifier(
            archive=archive,
            receipt=build_receipt,
            artifact_attestation=artifact_attestation,
        )
        materialized = materialized_verifier(
            release_dir=release,
            state_dir=state,
            artifact_attestation=artifact_attestation,
            attestation=materialized_attestation,
        )
        artifact_digest = _file_sha256(artifact_attestation)
        materialized_digest = _file_sha256(materialized_attestation)
    except (OSError, ReleaseVerifyError, ValueError, KeyError) as exc:
        raise UpgradeError("release artifact evidence is invalid") from exc
    identity_keys = (
        "manifest_sha256",
        "payload_tree_sha256",
        "release_id",
        "schema_version",
        "source_tree_sha256",
    )
    for key in identity_keys:
        if artifact.get(key) != materialized.get(key):
            raise UpgradeError("release artifact identities disagree")
    if (
        artifact.get("ok") is not True
        or materialized.get("ok") is not True
        or artifact.get("release_id") != release.name
        or Path(str(artifact.get("extract_dir") or "")).resolve(strict=False)
        != release
        or Path(str(materialized.get("materialized_release") or "")).resolve(
            strict=False
        )
        != release
        or Path(str(materialized.get("data_target") or "")).resolve(strict=False)
        != state
        or materialized.get("artifact_archive_sha256")
        != artifact.get("archive_sha256")
        or materialized.get("artifact_receipt_sha256")
        != artifact.get("receipt_sha256")
        or materialized.get("artifact_attestation_sha256") != artifact_digest
    ):
        raise UpgradeError("release artifact evidence is not bound to candidate")
    return {
        "release_archive_path": str(archive),
        "release_archive_sha256": str(artifact["archive_sha256"]),
        "release_build_receipt_path": str(build_receipt),
        "release_build_receipt_sha256": str(artifact["receipt_sha256"]),
        "release_manifest_sha256": str(artifact["manifest_sha256"]),
        "release_payload_tree_sha256": str(artifact["payload_tree_sha256"]),
        "release_source_tree_sha256": str(artifact["source_tree_sha256"]),
        "release_artifact_attestation_path": str(artifact_attestation),
        "release_artifact_attestation_sha256": artifact_digest,
        "materialized_release_attestation_path": str(
            materialized_attestation
        ),
        "materialized_release_attestation_sha256": materialized_digest,
        "release_artifact_schema_version": int(artifact["schema_version"]),
    }


def _assert_receipt_artifact_evidence(
    *,
    receipt: dict[str, Any],
    state: Path,
    artifact_verifier: Callable[..., dict[str, Any]],
    materialized_verifier: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    try:
        release = _release_root(str(receipt["release"]))
        current = _release_artifact_evidence(
            release=release,
            state=state,
            archive=Path(str(receipt["release_archive_path"])).absolute(),
            build_receipt=Path(
                str(receipt["release_build_receipt_path"])
            ).absolute(),
            artifact_attestation=Path(
                str(receipt["release_artifact_attestation_path"])
            ).absolute(),
            materialized_attestation=Path(
                str(receipt["materialized_release_attestation_path"])
            ).absolute(),
            artifact_verifier=artifact_verifier,
            materialized_verifier=materialized_verifier,
        )
    except (KeyError, OSError, ValueError) as exc:
        raise UpgradeError("committed release evidence is incomplete") from exc
    for key, value in current.items():
        if str(receipt.get(key) or "") != str(value):
            raise UpgradeError("committed release evidence changed")
    return current


def _bootstrap_stage_evidence(
    *,
    release: Path,
    state: Path,
    control_dir: Path,
    attestation: Path,
    bootstrap_verifier: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    expected = (
        control_dir
        / "bootstrap"
        / "releases"
        / release.name
        / "attestation.json"
    )
    if attestation != expected:
        raise UpgradeError("bootstrap attestation path is not fixed")
    try:
        verified = bootstrap_verifier(release.name)
        report = _read_json(attestation)
        digest = _file_sha256(attestation)
    except (BootstrapError, OSError, ValueError, KeyError) as exc:
        raise UpgradeError("bootstrap stage evidence is invalid") from exc
    venv_digest = str(report.get("venv_tree_sha256") or "")
    ops_digest = str(report.get("ops_digest") or "")
    launcher_digest = str(report.get("launcher_sha256") or "")
    wheelhouse_path = str(report.get("wheelhouse_attestation_path") or "")
    expected_wheelhouse_path = str(
        Path("/var/cache/paihuo-wheelhouse")
        / release.name
        / "wheelhouse-attestation.json"
    )
    wheelhouse_digest_keys = (
        "wheelhouse_attestation_sha256",
        "wheelhouse_hashed_lock_sha256",
        "wheelhouse_source_lock_sha256",
        "wheelhouse_wheel_set_sha256",
    )
    wheelhouse_digests = tuple(
        str(report.get(key) or "") for key in wheelhouse_digest_keys
    )
    wheelhouse_count = report.get("wheelhouse_wheel_count")
    wheelhouse_bytes = report.get("wheelhouse_total_bytes")
    if (
        report.get("format") != BOOTSTRAP_FORMAT
        or report.get("ok") is not True
        or report.get("release_id") != release.name
        or Path(str(report.get("release_dir") or "")).resolve(strict=False)
        != release
        or Path(str(report.get("state_dir") or "")).resolve(strict=False)
        != state
        or verified.get("release_id") != release.name
        or verified.get("venv_tree_sha256") != venv_digest
        or wheelhouse_path != expected_wheelhouse_path
        or any(
            str(verified.get(key) or "") != str(report.get(key) or "")
            for key in (
                "launcher_sha256",
                "ops_digest",
                "wheelhouse_attestation_path",
                *wheelhouse_digest_keys,
                "wheelhouse_total_bytes",
                "wheelhouse_wheel_count",
            )
        )
        or not all(
            re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (
                venv_digest,
                ops_digest,
                launcher_digest,
                *wheelhouse_digests,
            )
        )
        or not isinstance(wheelhouse_count, int)
        or wheelhouse_count <= 0
        or not isinstance(wheelhouse_bytes, int)
        or wheelhouse_bytes <= 0
    ):
        raise UpgradeError("bootstrap stage evidence is not bound to candidate")
    evidence = {
        "bootstrap_stage_attestation_path": str(attestation),
        "bootstrap_stage_attestation_sha256": digest,
        "bootstrap_venv_tree_sha256": venv_digest,
        "bootstrap_ops_digest": ops_digest,
        "bootstrap_launcher_sha256": launcher_digest,
        "bootstrap_wheelhouse_attestation_path": wheelhouse_path,
        "bootstrap_wheelhouse_total_bytes": wheelhouse_bytes,
        "bootstrap_wheelhouse_wheel_count": wheelhouse_count,
    }
    for key in wheelhouse_digest_keys:
        evidence[f"bootstrap_{key}"] = str(report[key])
    return evidence


def _assert_receipt_bootstrap_evidence(
    *,
    receipt: dict[str, Any],
    state: Path,
    control_dir: Path,
    bootstrap_verifier: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    try:
        release = _release_root(str(receipt["release"]))
        current = _bootstrap_stage_evidence(
            release=release,
            state=state,
            control_dir=control_dir,
            attestation=Path(
                str(receipt["bootstrap_stage_attestation_path"])
            ).absolute(),
            bootstrap_verifier=bootstrap_verifier,
        )
    except (KeyError, OSError, ValueError) as exc:
        raise UpgradeError("committed bootstrap evidence is incomplete") from exc
    for key, value in current.items():
        if not _constant_time_text_equal(receipt.get(key) or "", value):
            raise UpgradeError("committed bootstrap stage evidence changed")
    return current


def _validate_release_evidence(
    *,
    release: Path,
    state: Path,
    control_dir: Path,
    managed_env_file: Path,
    secret_backup: Path,
    control_plane_attestation: Path,
    release_archive: Path,
    release_build_receipt: Path,
    release_artifact_attestation: Path,
    materialized_release_attestation: Path,
    bootstrap_stage_attestation: Path,
    control_plane_verifier: Callable[..., dict[str, Any]],
    artifact_verifier: Callable[..., dict[str, Any]],
    materialized_verifier: Callable[..., dict[str, Any]],
    bootstrap_verifier: Callable[[str], dict[str, Any]],
) -> tuple[int, dict[str, str], dict[str, Any]]:
    schema_version = _candidate_schema_version(release)
    secrets_map = _managed_candidate_secrets(managed_env_file)
    evidence_root = control_dir / "incoming" / release.name
    expected_paths = (
        evidence_root / f"{release.name}.tar.gz",
        evidence_root / f"{release.name}.receipt.json",
        evidence_root / "release-artifact-attestation.json",
        evidence_root / "materialized-release-attestation.json",
        control_dir
        / "bootstrap"
        / "releases"
        / release.name
        / "attestation.json",
    )
    if (
        release_archive,
        release_build_receipt,
        release_artifact_attestation,
        materialized_release_attestation,
        bootstrap_stage_attestation,
    ) != expected_paths:
        raise UpgradeError("release evidence paths are not fixed")
    artifact_evidence = _release_artifact_evidence(
        release=release,
        state=state,
        archive=release_archive,
        build_receipt=release_build_receipt,
        artifact_attestation=release_artifact_attestation,
        materialized_attestation=materialized_release_attestation,
        artifact_verifier=artifact_verifier,
        materialized_verifier=materialized_verifier,
    )
    if artifact_evidence["release_artifact_schema_version"] != schema_version:
        raise UpgradeError("release evidence schema disagrees with manifest")
    bootstrap_evidence = _bootstrap_stage_evidence(
        release=release,
        state=state,
        control_dir=control_dir,
        attestation=bootstrap_stage_attestation,
        bootstrap_verifier=bootstrap_verifier,
    )
    try:
        backup_report = check_secret_backup(
            managed_env_file,
            secret_backup,
            owner_uid=_owner_uid(),
            owner_gid=0 if os.geteuid() == 0 else os.getegid(),
        )
        verified_control = control_plane_verifier(control_plane_attestation)
        control_digest = str(verified_control["attestation_sha256"])
        control_report = _read_json(control_plane_attestation)
    except (SecretEnvError, ControlPlaneError, OSError, ValueError) as exc:
        raise UpgradeError("release control evidence is invalid") from exc
    if control_report.get("status") != "installed":
        raise UpgradeError("candidate control plane is not attested as installed")
    if control_report.get("release") != str(release):
        raise UpgradeError("control-plane attestation release mismatch")
    evidence = {
        "control_plane_attestation_path": str(control_plane_attestation),
        "control_plane_attestation_sha256": control_digest,
        "control_plane_candidate_digest": str(
            control_report.get("candidate_digest") or ""
        ),
        "secret_backup_path": str(secret_backup),
        "secret_backup_status": str(backup_report.get("status") or ""),
        **artifact_evidence,
        **bootstrap_evidence,
    }
    return schema_version, secrets_map, evidence


def _validate_release_layout(release: Path, state: Path, venv: Path) -> str:
    release = _release_root(release)
    data_link = release / "data"
    if not data_link.is_symlink():
        raise UpgradeError(f"release data must be a symlink: {data_link}")
    if data_link.resolve(strict=True) != state.resolve(strict=True):
        raise UpgradeError(f"release data symlink must point to {state}")
    for required in (
        release / "app" / "main.py",
        release / "deploy" / "preflight.py",
        release / "tests" / "smoke.py",
        release / "run.sh",
    ):
        if required.is_symlink() or not required.is_file():
            raise UpgradeError(f"release file missing or unsafe: {required}")
    if not os.access(release / "run.sh", os.X_OK):
        raise UpgradeError(f"release entrypoint is not executable: {release / 'run.sh'}")
    _validate_venv(release, venv)
    return release_digest(release)


def _current_target(current: Path) -> Path:
    if not current.is_symlink():
        raise UpgradeError(f"current must be an existing symlink: {current}")
    target = current.resolve(strict=True)
    if not target.is_dir():
        raise UpgradeError(f"current target is not a directory: {target}")
    return target


def _try_current_target(current: Path) -> Path | None:
    try:
        return _current_target(current)
    except (OSError, UpgradeError):
        return None


def _atomic_symlink(target: Path, current: Path) -> None:
    parent = current.parent
    if parent.is_symlink():
        raise UpgradeError(f"current parent must not be a symlink: {parent}")
    metadata = parent.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != _owner_uid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise UpgradeError(f"current parent is not owner-controlled: {parent}")
    staged = current.parent / f".{current.name}.next-{uuid.uuid4().hex}"
    try:
        staged.symlink_to(target, target_is_directory=True)
        os.replace(staged, current)
        # A failure here is commit-uncertain: callers must inspect current,
        # never trust a boolean set after this function returns.
        _fsync_directory(current.parent)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _select_for_rollback(target: Path, current: Path) -> None:
    if _try_current_target(current) != target:
        try:
            _atomic_symlink(target, current)
        except BaseException:
            if _try_current_target(current) != target:
                raise
            # The rename happened. Retry the durability barrier; if it still
            # fails, rollback is not proven and the service must remain down.
            _fsync_directory(current.parent)


def _safe_name(release: Path) -> str:
    return SAFE_RELEASE_RE.sub("-", release.name).strip("-") or "release"


def _verified_backup(
    *,
    state: Path,
    control_dir: Path,
    release: Path,
    label: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    name = (
        f"{label}-{_safe_name(release)}-"
        f"{current.strftime('%Y%m%dT%H%M%S')}-"
        f"{current.microsecond:06d}Z-{uuid.uuid4().hex[:12]}.db"
    )
    check_disk_capacity(state / "contentcrew.db", control_dir)
    try:
        result = backup_database(
            state / "contentcrew.db",
            control_dir,
            keep_days=30,
            keep_minimum=24,
            run_restore_drill=True,
            output_path=control_dir / name,
            now=current,
        )
    except (BackupError, VerificationError, OSError, ValueError) as exc:
        raise UpgradeError(f"mandatory backup/restore drill failed: {exc}") from exc
    if not (result.get("restore_drill") or {}).get("ok"):
        raise UpgradeError("mandatory restore drill did not prove success")
    return result


def create_upgrade_checkpoint(
    *,
    release: str | os.PathLike[str],
    current: str | os.PathLike[str],
    state: str | os.PathLike[str],
    backup_dir: str | os.PathLike[str],
    now: datetime | None = None,
    service_user: str | None = None,
) -> dict[str, Any]:
    """Create the online pre-checkpoint; it is never used as rollback state."""
    release_path = _release_root(release)
    state_path = Path(state).resolve(strict=True)
    current_path = Path(current).absolute()
    previous = _current_target(current_path)
    control = _secure_control_directory(
        backup_dir, state=state_path, service_user=service_user
    )
    before_digest = release_digest(release_path)
    checkpoint = _verified_backup(
        state=state_path,
        control_dir=control,
        release=release_path,
        label="online-precheck",
        now=now,
    )
    if release_digest(release_path) != before_digest:
        raise UpgradeError("release changed while its online checkpoint was created")
    current_time = (now or datetime.now(tz=UTC)).astimezone(UTC)
    receipt_path = control / (
        f"upgrade-{_safe_name(release_path)}-"
        f"{current_time.strftime('%Y%m%dT%H%M%S')}-"
        f"{current_time.microsecond:06d}Z-{uuid.uuid4().hex[:12]}.json"
    )
    receipt = {
        "status": "checkpoint_ready",
        "phase": "initialized",
        "created_at_utc": current_time.isoformat().replace("+00:00", "Z"),
        "release": str(release_path),
        "release_sha256": before_digest,
        "previous_release": str(previous),
        "database": str(state_path / "contentcrew.db"),
        "online_backup": checkpoint,
        # Compatibility key for operators/tests written before the final
        # checkpoint distinction. Rollback code never reads this key.
        "backup": checkpoint,
        "receipt_path": str(receipt_path),
    }
    _atomic_json(receipt_path, receipt)
    return receipt


def _transition(
    receipt: dict[str, Any],
    path: Path,
    phase: str,
    **fields: Any,
) -> None:
    if phase not in PHASES:
        raise UpgradeError(f"unknown upgrade phase: {phase}")
    receipt.update(fields)
    receipt["status"] = "in_progress"
    receipt["phase"] = phase
    receipt["updated_at_utc"] = datetime.now(tz=UTC).isoformat()
    _atomic_json(path, receipt)


@contextmanager
def _service_start_permit(path: Path, receipt_path: Path) -> Iterator[None]:
    parent = path.parent
    if parent.is_symlink():
        raise UpgradeError(f"start permit parent is unsafe: {parent}")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = parent.lstat()
    if (
        metadata.st_uid != _owner_uid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise UpgradeError("start permit directory must be owner-controlled 0700")
    _atomic_json(path, {
        "receipt_path": str(receipt_path),
        "issued_at_utc": datetime.now(tz=UTC).isoformat(),
        "nonce": secrets.token_hex(24),
    })
    try:
        yield
    finally:
        try:
            path.unlink()
            _fsync_directory(parent)
        except FileNotFoundError:
            pass


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise UpgradeError(
            f"command failed ({result.returncode}): {' '.join(command)}"
        )


def _read_smoke_credentials(path: Path) -> tuple[str, str]:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        _assert_private_regular(descriptor, path, exact_mode=0o600)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            lines = handle.read().splitlines()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key in {"username", "password"}:
            values[key] = value
    if not values.get("username") or not values.get("password"):
        raise UpgradeError("smoke credential file lacks username/password")
    return values["username"], values["password"]


def _runuser_prefix(service_user: str) -> list[str]:
    return [
        shutil.which("runuser") or "/usr/sbin/runuser",
        "-u",
        service_user,
        "--preserve-environment",
        "--",
    ]


def _preflight_env(
    release: Path,
    state: Path,
    venv: Path,
    *,
    candidate_secrets: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/var/lib/paihuo",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CONTENTCREW_ROOT": str(release),
        "CONTENTCREW_PYTHON": str(venv / "bin" / "python"),
        "CONTENTCREW_DB_PATH": str(state / "contentcrew.db"),
        "CONTENTCREW_YTDLP_PATH": str(venv / "bin" / "yt-dlp"),
    }
    for key in SAFE_PREFLIGHT_ENV:
        if key in os.environ:
            environment[key] = os.environ[key]
    if candidate_secrets is not None:
        if set(candidate_secrets) != {SESSION_SECRET_ENV, CONFIG_KEY_ENV}:
            raise UpgradeError("candidate managed secret set is incomplete")
        environment.update(candidate_secrets)
        environment[REQUIRE_SESSION_SECRET_ENV] = "1"
        environment[REQUIRE_CONFIG_KEY_ENV] = "1"
    return environment


def _run_preflight(
    *,
    release: Path,
    state: Path,
    venv: Path,
    service_user: str,
    command_runner: Callable[..., None],
    candidate_secrets: dict[str, str] | None = None,
) -> None:
    command_runner(
        _runuser_prefix(service_user)
        + [
            str(venv / "bin" / "python"),
            "-m",
            "deploy.preflight",
            "--root",
            str(release),
            "--state",
            str(state),
            "--venv",
            str(venv),
        ],
        cwd=release,
        env=_preflight_env(
            release, state, venv, candidate_secrets=candidate_secrets
        ),
    )


def _probe_venv_runtime(
    *,
    release: Path,
    state: Path,
    venv: Path,
    service_user: str,
    command_runner: Callable[..., None],
    candidate_secrets: dict[str, str] | None = None,
) -> None:
    environment = _preflight_env(
        release, state, venv, candidate_secrets=candidate_secrets
    )
    for executable in (
        (venv / "bin" / "python", "--version"),
        (venv / "bin" / "yt-dlp", "--version"),
    ):
        command_runner(
            _runuser_prefix(service_user)
            + [str(executable[0]), executable[1]],
            cwd=release,
            env=environment,
        )


def _authenticated_smoke(
    *,
    smoke_client: Path,
    base_url: str,
    credentials: tuple[str, str],
    smoke_user: str,
    command_runner: Callable[..., None],
) -> None:
    _validate_fixed_helper(smoke_client, "read-only smoke client")
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/var/lib/paihuo",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SMOKE_USERNAME": credentials[0],
        "SMOKE_PASSWORD": credentials[1],
    }
    command_runner(
        _runuser_prefix(smoke_user)
        + ["/usr/bin/python3", str(smoke_client), base_url],
        cwd=smoke_client.parent,
        env=environment,
    )


def _validate_fixed_helper(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise UpgradeError(f"trusted {label} missing: {path}")
    metadata = path.lstat()
    if (
        metadata.st_uid != _owner_uid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise UpgradeError(f"trusted {label} is not owner-controlled")


def _validate_smoke_user(smoke_user: str, service_user: str) -> None:
    if os.geteuid() != 0:
        return
    try:
        smoke_account = pwd.getpwnam(smoke_user)
        service_account = pwd.getpwnam(service_user)
    except KeyError as exc:
        raise UpgradeError(f"required system user is missing: {exc}") from exc
    if smoke_account.pw_uid in (0, service_account.pw_uid):
        raise UpgradeError("smoke user must be distinct and unprivileged")


def _smoke_password_hash(password: str) -> str:
    iterations = 200_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
    )
    return f"pbkdf2:{iterations}:{salt}:{digest.hex()}"


def _create_smoke_identity(state: Path) -> tuple[int, str, str]:
    database = state / "contentcrew.db"
    username = f"__release_smoke_{secrets.token_hex(10)}"
    password = secrets.token_urlsafe(32)
    connection = sqlite3.connect(database, timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id FROM tenants WHERE enabled=1 ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            raise UpgradeError("no enabled tenant exists for a smoke identity")
        now = time.time()
        cursor = connection.execute(
            "INSERT INTO users("
            "tenant_id,username,password_hash,role,modules_json,enabled,"
            "created_at,updated_at"
            ") VALUES(?,?,?,'member','[]',1,?,?)",
            (int(row[0]), username, _smoke_password_hash(password), now, now),
        )
        user_id = int(cursor.lastrowid)
        connection.commit()
        return user_id, username, password
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _delete_smoke_identity(state: Path, user_id: int, username: str) -> None:
    connection = sqlite3.connect(state / "contentcrew.db", timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "DELETE FROM users WHERE id=? AND username=? AND role='member'",
            (int(user_id), username),
        )
        if cursor.rowcount != 1:
            raise UpgradeError("one-time smoke identity could not be revoked")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _purge_orphaned_smoke_identities(state: Path) -> None:
    """Remove reserved one-time identities left by SIGKILL during validation."""
    connection = sqlite3.connect(state / "contentcrew.db", timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM users "
            "WHERE username GLOB '__release_smoke_*' AND role='member'"
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _health(url: str, timeout: float = 3.0) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read(4096))
            return payload.get("status") == "ok"
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False


def _wait_health(
    checker: Callable[[str], bool],
    url: str,
    timeout: float,
    interval: float,
) -> bool:
    deadline = time.monotonic() + max(0, timeout)
    while True:
        if checker(url):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.01, interval))


def _verified_expected_backup(path: Path, expected_sha256: str) -> None:
    try:
        verification = verify_database(path)
    except VerificationError as exc:
        raise UpgradeError(f"rollback backup verification failed: {exc}") from exc
    actual = str(verification.get("sha256") or "")
    if not expected_sha256 or not hmac.compare_digest(actual, expected_sha256):
        raise UpgradeError("rollback backup SHA-256 no longer matches its receipt")


def _prepare_rollback_ready(
    backup_path: Path,
    expected_sha256: str,
    state: Path,
    reserve_dir: Path,
) -> dict[str, Any]:
    """Materialise and reserve the exact rollback database on the state volume."""
    # A SQLite Backup API restore is logically equivalent to its source but is
    # not guaranteed to be byte-for-byte identical (page layout and header
    # counters may change).  Bind the source and the restored artifact with
    # separate hashes instead of incorrectly comparing the restored bytes with
    # the source hash.
    _verified_expected_backup(backup_path, expected_sha256)
    live = state / "contentcrew.db"
    if live.is_symlink() or not live.is_file():
        raise UpgradeError(f"live database disappeared or is unsafe: {live}")
    if reserve_dir.stat().st_dev != state.stat().st_dev:
        raise UpgradeError(
            "upgrade control directory must share the state filesystem so a "
            "protected rollback database can be reserved atomically"
    )
    prepared = reserve_dir / f".rollback-ready-{uuid.uuid4().hex}.db"
    try:
        restore_report = restore_drill(backup_path, restore_to=prepared)
        _verified_expected_backup(backup_path, expected_sha256)
        restored_path = Path(str(restore_report.get("restored_path") or ""))
        if (
            not restored_path.is_absolute()
            or restored_path.resolve(strict=True) != prepared.resolve(strict=True)
        ):
            raise UpgradeError("rollback-ready restore path did not match its target")
        prepared_sha256 = str(restore_report.get("restored_sha256") or "")
        _verified_expected_backup(prepared, prepared_sha256)
        os.chmod(prepared, 0o600)
        descriptor = os.open(
            prepared,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(reserve_dir)
        return {
            "path": str(prepared),
            "sha256": prepared_sha256,
            "source_sha256": expected_sha256,
            "size_bytes": prepared.stat().st_size,
        }
    except BaseException:
        for artifact in (
            prepared,
            Path(f"{prepared}-journal"),
            Path(f"{prepared}-wal"),
            Path(f"{prepared}-shm"),
        ):
            try:
                artifact.unlink()
            except FileNotFoundError:
                pass
        raise


def _discard_rollback_ready(receipt: dict[str, Any], state: Path) -> None:
    prepared = receipt.get("rollback_ready")
    if not isinstance(prepared, dict):
        return
    path = Path(str(prepared.get("path") or "")).absolute()
    parent_metadata = path.parent.lstat()
    if (
        not path.name.startswith(".rollback-ready-")
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != _owner_uid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or path.parent.stat().st_dev != state.stat().st_dev
    ):
        raise UpgradeError("rollback-ready path escaped protected same-volume storage")
    for artifact in (
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    ):
        try:
            artifact.unlink()
        except FileNotFoundError:
            pass
    _fsync_directory(path.parent)


def _restore_live_database(
    backup_path: Path,
    expected_sha256: str,
    state: Path,
    release_name: str,
    *,
    prepared_path: Path,
    prepared_sha256: str,
) -> Path:
    # Verify the receipt-bound hash before creating quarantine or moving live
    # data. A tampered backup therefore leaves the current database untouched.
    _verified_expected_backup(backup_path, expected_sha256)
    live = state / "contentcrew.db"
    if live.is_symlink() or not live.is_file():
        raise UpgradeError(f"live database disappeared or is unsafe: {live}")
    original_stat = live.stat()
    if not prepared_sha256:
        raise UpgradeError("rollback-ready database hash is missing")
    staged = prepared_path.absolute()
    staged_metadata = staged.lstat()
    staged_parent_metadata = staged.parent.lstat()
    trusted_prepared_owners = {_owner_uid(), original_stat.st_uid}
    if (
        not staged.name.startswith(".rollback-ready-")
        or staged.is_symlink()
        or not staged.is_file()
        or staged_metadata.st_uid not in trusted_prepared_owners
        or stat.S_IMODE(staged_metadata.st_mode) != 0o600
        or staged_metadata.st_dev != live.stat().st_dev
        or not stat.S_ISDIR(staged_parent_metadata.st_mode)
        or staged_parent_metadata.st_uid != _owner_uid()
        or stat.S_IMODE(staged_parent_metadata.st_mode) != 0o700
    ):
        raise UpgradeError("rollback-ready database is missing or unsafe")
    failed_root = state / "failed-upgrades"
    if failed_root.is_symlink():
        raise UpgradeError(f"failed-upgrades path is a symlink: {failed_root}")
    failed_root.mkdir(exist_ok=True, mode=0o700)
    quarantine = failed_root / (
        f"{SAFE_RELEASE_RE.sub('-', release_name)}-"
        f"{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%S')}-"
        f"{uuid.uuid4().hex[:12]}"
    )
    quarantine.mkdir(mode=0o700)
    moved_sidecars: list[tuple[Path, Path]] = []
    replaced_live = False
    try:
        _verified_expected_backup(staged, prepared_sha256)
        # Prepare ownership and mode *before* the atomic rename. A SIGKILL
        # after os.replace must still leave a DB the service user can open.
        os.chmod(staged, stat.S_IMODE(original_stat.st_mode))
        try:
            os.chown(staged, original_stat.st_uid, original_stat.st_gid)
        except PermissionError:
            # A non-root test/developer run may already have the exact target
            # metadata.  The descriptor check below decides; the exception
            # itself never authorises replacement.
            pass
        staged_descriptor = os.open(
            staged,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            prepared_stat = os.fstat(staged_descriptor)
            if (
                not stat.S_ISREG(prepared_stat.st_mode)
                or prepared_stat.st_nlink != 1
                or prepared_stat.st_dev != staged_metadata.st_dev
                or prepared_stat.st_ino != staged_metadata.st_ino
                or prepared_stat.st_uid != original_stat.st_uid
                or prepared_stat.st_gid != original_stat.st_gid
                or stat.S_IMODE(prepared_stat.st_mode)
                != stat.S_IMODE(original_stat.st_mode)
            ):
                raise UpgradeError(
                    "rollback-ready ownership or mode could not be prepared"
                )
            os.fsync(staged_descriptor)
        finally:
            os.close(staged_descriptor)
        # Preserve the failed candidate main DB without ever removing the live
        # pathname. A same-filesystem hardlink is durable evidence and lets the
        # following os.replace be a single atomic live-file transition.
        failed_main = quarantine / live.name
        os.link(live, failed_main)
        failed_descriptor = os.open(
            failed_main,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(failed_descriptor)
        finally:
            os.close(failed_descriptor)
        _fsync_directory(quarantine)
        # Sidecars must not be left beside the restored main file. Move them
        # first while the candidate main file still exists; a crash here is
        # recoverable on the next state-machine run because `live` is present.
        for artifact in (
            Path(f"{live}-wal"),
            Path(f"{live}-shm"),
            Path(f"{live}-journal"),
        ):
            if artifact.exists():
                target = quarantine / artifact.name
                os.replace(artifact, target)
                moved_sidecars.append((artifact, target))
        _fsync_directory(state)
        _fsync_directory(quarantine)
        os.replace(staged, live)
        replaced_live = True
        _fsync_directory(state)
        if staged.parent != state:
            _fsync_directory(staged.parent)
        return quarantine
    except BaseException:
        # Before the atomic main-file replacement, put sidecars back so the
        # still-present candidate DB remains usable. After replacement, the
        # verified final snapshot is already live and must not be undone.
        if not replaced_live:
            for source, target in moved_sidecars:
                if target.exists() and not source.exists():
                    os.replace(target, source)
        try:
            _fsync_directory(state)
        except OSError:
            pass
        raise
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _phase_at_least(receipt: dict[str, Any], phase: str) -> bool:
    try:
        return PHASES.index(str(receipt.get("phase"))) >= PHASES.index(phase)
    except ValueError:
        return False


def _systemd_inactive(service: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", service],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    # systemd rc=3 means a known unit is inactive/failed. rc=4 is "unknown"
    # and must never prove that the intended service was stopped.
    return result.returncode == 3


def _stop_fail_closed(
    command_runner: Callable[..., None],
    service: str,
    inactive_checker: Callable[[str], bool],
) -> None:
    first_error: BaseException | None = None
    try:
        command_runner(["systemctl", "stop", service])
    except BaseException as exc:
        first_error = exc
    if inactive_checker(service):
        return
    try:
        command_runner([
            "systemctl", "kill", "--kill-whom=all", "--signal=SIGKILL", service
        ])
    except BaseException:
        pass
    try:
        command_runner(["systemctl", "stop", service])
    except BaseException as exc:
        if first_error is None:
            first_error = exc
    if not inactive_checker(service):
        raise UpgradeError(
            f"could not prove service inactive after stop/kill: {service}"
        ) from first_error


def _clear_quiescent(
    command_runner: Callable[..., None],
) -> None:
    command_runner([
        "systemctl",
        "unset-environment",
        "CONTENTCREW_QUIESCENT",
        "CONTENTCREW_VALIDATION",
        "CONTENTCREW_MAINTENANCE",
    ])


def _set_validation_environment(
    command_runner: Callable[..., None],
) -> None:
    command_runner([
        "systemctl",
        "unset-environment",
        "CONTENTCREW_QUIESCENT",
    ])
    command_runner([
        "systemctl",
        "set-environment",
        "CONTENTCREW_VALIDATION=1",
        "CONTENTCREW_MAINTENANCE=1",
    ])


def _resume_and_refresh_backups(
    *,
    backup_timer: str,
    backup_service: str,
    command_runner: Callable[..., None],
) -> None:
    # Generate a new root-attested snapshot of the accepted state before
    # traffic is reopened.  This also prevents a rejected candidate snapshot
    # from remaining the newest disaster-recovery point.
    command_runner(["systemctl", "start", backup_service])
    command_runner(["systemctl", "start", backup_timer])
    command_runner(["systemctl", "is-active", "--quiet", backup_timer])


def _sqlite_schema_version(path: Path) -> int:
    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_version"
        ).fetchone()
    except sqlite3.Error as exc:
        raise UpgradeError("attested backup lacks a readable schema ledger") from exc
    finally:
        connection.close()
    version = int(row[0] if row else 0)
    if version <= 0:
        raise UpgradeError("attested backup schema version is invalid")
    return version


def _post_commit_proof(
    *,
    state: Path,
    periodic_backup_dir: Path,
    attestation_path: Path,
    committed_at_utc: str,
    candidate_schema_version: int,
    https_health_url: str,
    https_health_checker: Callable[[str], bool],
) -> dict[str, Any]:
    health = check_backup_health(
        database=state / "contentcrew.db",
        backup_dir=periodic_backup_dir,
        max_age_hours=24,
        attestation_path=attestation_path,
    )
    attestation = _read_json(attestation_path)
    try:
        committed = datetime.fromisoformat(
            committed_at_utc.replace("Z", "+00:00")
        ).astimezone(UTC)
        created = datetime.fromisoformat(
            str(attestation["backup_created_at_utc"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        recorded = datetime.fromisoformat(
            str(attestation["recorded_at_utc"]).replace("Z", "+00:00")
        ).astimezone(UTC)
    except (KeyError, ValueError) as exc:
        raise UpgradeError("post-cutover backup attestation timestamp is invalid") from exc
    if created < committed or recorded < committed:
        raise UpgradeError("latest backup was not generated after cutover commit")
    backup_path = Path(str(health.get("latest_backup") or "")).resolve(strict=True)
    schema_version = _sqlite_schema_version(backup_path)
    if schema_version != candidate_schema_version:
        raise UpgradeError("post-cutover backup schema differs from candidate")
    if not https_health_url.lower().startswith("https://"):
        raise UpgradeError("public health proof must use HTTPS")
    if not https_health_checker(https_health_url):
        raise UpgradeError("public HTTPS health check did not return healthy 200")
    return {
        "backup_attestation_path": str(attestation_path),
        "backup_attestation_sha256": _file_sha256(attestation_path),
        "backup_created_after_cutover": True,
        "backup_integrity_check": str(health.get("integrity_check") or ""),
        "backup_schema_version": schema_version,
        "candidate_schema_version": candidate_schema_version,
        "https_health_url": https_health_url,
        "https_status": 200,
        "ok": True,
    }


def _file_sha256(path: Path) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise UpgradeError("evidence file is not regular")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)
    finally:
        os.close(descriptor)


def _sanitized_error_message(
    error: BaseException, secret_values: Iterable[str] = ()
) -> str:
    message = str(error)[-1000:]
    for value in secret_values:
        if value:
            message = message.replace(value, "[redacted]")
    for variable in DEFAULT_SECRET_KEYS:
        if variable in message:
            return "managed-secret validation failed"
    return message


def _contain_and_record_rollback_failure(
    *,
    receipt: dict[str, Any],
    receipt_path: Path,
    phase: str,
    rollback_error: BaseException,
    service: str,
    proxy_service: str,
    command_runner: Callable[..., None],
    inactive_checker: Callable[[str], bool],
) -> None:
    """Persist whether a failed rollback was actually contained.

    An operator must never be told that the service is stopped merely because
    ``systemctl stop`` was attempted.  Unknown/inconsistent systemd state is a
    distinct, blocking receipt state.
    """
    stop_errors: list[BaseException] = []
    for unit in (proxy_service, service):
        try:
            _stop_fail_closed(command_runner, unit, inactive_checker)
        except BaseException as stop_error:
            stop_errors.append(stop_error)
    if stop_errors:
        stop_error = stop_errors[0]
        receipt.update({
            "status": "rollback_failed_uncontained",
            "phase": phase,
            "rollback_error": type(rollback_error).__name__,
            "stop_verified": False,
            "stop_error": type(stop_error).__name__,
        })
        _atomic_json(receipt_path, receipt)
        raise UpgradeError(
            "rollback failed and the service/proxy could not be proven inactive; "
            f"receipt: {receipt_path}"
        ) from stop_error
    receipt.update({
        "status": "rollback_failed",
        "phase": phase,
        "rollback_error": type(rollback_error).__name__,
        "stop_verified": True,
    })
    _atomic_json(receipt_path, receipt)


def _rollback(
    *,
    receipt: dict[str, Any],
    receipt_path: Path,
    current: Path,
    state: Path,
    service: str,
    proxy_service: str,
    service_user: str,
    smoke_user: str,
    smoke_client: Path,
    start_permit: Path,
    backup_timer: str,
    backup_service: str,
    health_url: str,
    smoke_url: str,
    health_timeout: float,
    health_interval: float,
    command_runner: Callable[..., None],
    health_checker: Callable[[str], bool],
    inactive_checker: Callable[[str], bool],
    restore_database: bool,
    recovered: bool = False,
) -> None:
    _stop_fail_closed(command_runner, proxy_service, inactive_checker)
    _stop_fail_closed(command_runner, service, inactive_checker)
    previous = _release_root(str(receipt["previous_release"]))
    expected_previous_digest = str(receipt["previous_release_sha256"])
    if "rollback_restore_required" in receipt:
        restore_database = bool(receipt["rollback_restore_required"])
    else:
        receipt["rollback_restore_required"] = bool(restore_database)
        _atomic_json(receipt_path, receipt)
    quarantine: Path | None = None
    if restore_database:
        final = receipt.get("final_backup")
        if not isinstance(final, dict):
            raise UpgradeError("final stopped-service checkpoint is missing")
        prepared_record = receipt.get("rollback_ready")
        if prepared_record is None:
            prepared_record = _prepare_rollback_ready(
                Path(str(final["backup_path"])),
                str(final["sha256"]),
                state,
                receipt_path.parent,
            )
            receipt["rollback_ready"] = prepared_record
            _atomic_json(receipt_path, receipt)
        if not isinstance(prepared_record, dict):
            raise UpgradeError("rollback-ready receipt is invalid")
        prepared_value = str(prepared_record.get("path") or "")
        prepared_sha256 = str(prepared_record.get("sha256") or "")
        if not prepared_value or not prepared_sha256:
            raise UpgradeError("rollback-ready receipt is incomplete")
        prepared = Path(prepared_value)
        source_sha256 = str(
            prepared_record.get("source_sha256") or ""
        )
        if not source_sha256:
            # Compatibility with the old single-hash receipt is safe only
            # when that hash is exactly the final source hash.  A new
            # dual-hash record with a missing source link must fail closed.
            if not hmac.compare_digest(
                prepared_sha256, str(final["sha256"])
            ):
                raise UpgradeError(
                    "rollback-ready source hash is missing"
                )
            source_sha256 = str(final["sha256"])
        if not hmac.compare_digest(source_sha256, str(final["sha256"])):
            raise UpgradeError(
                "rollback-ready source hash does not match final checkpoint"
            )
        rollback_phase = str(receipt.get("phase") or "")
        database_already_restored = rollback_phase in {
            "rollback_database_restored",
            "rollback_validating",
            "rollback_committed",
            "rollback_complete",
        }
        if (
            rollback_phase == "rollback_restore_committing"
            and not prepared.exists()
        ):
            # os.replace may have committed just before a crash/fsync error.
            # No service starts before the next durable phase, so an exact
            # prepared-artifact hash proves that the restored DB is live.
            try:
                _verified_expected_backup(
                    state / "contentcrew.db", str(prepared_sha256 or "")
                )
                database_already_restored = True
            except UpgradeError:
                replacement = _prepare_rollback_ready(
                    Path(str(final["backup_path"])),
                    str(final["sha256"]),
                    state,
                    receipt_path.parent,
                )
                receipt["rollback_ready"] = replacement
                prepared = Path(str(replacement["path"]))
                prepared_sha256 = str(replacement["sha256"])
                _atomic_json(receipt_path, receipt)
        if not database_already_restored:
            receipt.update({
                "status": "in_progress",
                "phase": "rollback_restore_committing",
            })
            _atomic_json(receipt_path, receipt)
            try:
                quarantine = _restore_live_database(
                    Path(str(final["backup_path"])),
                    str(final["sha256"]),
                    state,
                    Path(str(receipt["release"])).name,
                    prepared_path=prepared,
                    prepared_sha256=prepared_sha256,
                )
            except BaseException:
                if prepared.exists():
                    raise
                try:
                    _verified_expected_backup(
                        state / "contentcrew.db", str(prepared_sha256 or "")
                    )
                except UpgradeError:
                    replacement = _prepare_rollback_ready(
                        Path(str(final["backup_path"])),
                        str(final["sha256"]),
                        state,
                        receipt_path.parent,
                    )
                    receipt["rollback_ready"] = replacement
                    prepared = Path(str(replacement["path"]))
                    prepared_sha256 = str(replacement["sha256"])
                    _atomic_json(receipt_path, receipt)
                    quarantine = _restore_live_database(
                        Path(str(final["backup_path"])),
                        str(final["sha256"]),
                        state,
                        Path(str(receipt["release"])).name,
                        prepared_path=prepared,
                        prepared_sha256=prepared_sha256,
                    )
            receipt.update({
                "status": "in_progress",
                "phase": "rollback_database_restored",
            })
            if quarantine is not None:
                receipt["failed_database_quarantine"] = str(quarantine)
            _atomic_json(receipt_path, receipt)
    _select_for_rollback(previous, current)
    if release_digest(previous) != expected_previous_digest:
        raise UpgradeError("previous release changed; automatic rollback is unsafe")
    _set_validation_environment(command_runner)
    receipt.update({"status": "in_progress", "phase": "rollback_validating"})
    _atomic_json(receipt_path, receipt)
    _purge_orphaned_smoke_identities(state)
    smoke_id, smoke_name, smoke_password = _create_smoke_identity(state)
    try:
        with _service_start_permit(start_permit, receipt_path):
            command_runner(["systemctl", "start", service])
        if not _wait_health(
            health_checker, health_url, health_timeout, health_interval
        ):
            raise UpgradeError("rollback service did not become healthy")
        _authenticated_smoke(
            smoke_client=smoke_client,
            base_url=smoke_url,
            credentials=(smoke_name, smoke_password),
            smoke_user=smoke_user,
            command_runner=command_runner,
        )
        command_runner(["systemctl", "is-active", "--quiet", service])
    finally:
        _stop_fail_closed(command_runner, service, inactive_checker)
        _delete_smoke_identity(state, smoke_id, smoke_name)
    terminal_status = "recovered_rollback" if recovered else "rolled_back"
    receipt.update({
        "status": "rollback_committed",
        "phase": "rollback_committed",
        "active_release": str(previous),
        "rolled_back_at_utc": datetime.now(tz=UTC).isoformat(),
        "rollback_terminal_status": terminal_status,
    })
    if quarantine is not None and "failed_database_quarantine" not in receipt:
        receipt["failed_database_quarantine"] = str(quarantine)
    _atomic_json(receipt_path, receipt)
    # Only after the rollback decision is durable may the accepted old
    # release start its background workers.
    _clear_quiescent(command_runner)
    with _service_start_permit(start_permit, receipt_path):
        command_runner(["systemctl", "start", service])
    if not _wait_health(
        health_checker, health_url, health_timeout, health_interval
    ):
        raise UpgradeError("rollback service normal activation failed")
    command_runner(["systemctl", "is-active", "--quiet", service])
    _resume_and_refresh_backups(
        backup_timer=backup_timer,
        backup_service=backup_service,
        command_runner=command_runner,
    )
    with _service_start_permit(start_permit, receipt_path):
        command_runner(["systemctl", "start", proxy_service])
    command_runner(["systemctl", "is-active", "--quiet", proxy_service])
    receipt.update({
        "status": terminal_status,
        "phase": "rollback_complete",
        "completed_at_utc": datetime.now(tz=UTC).isoformat(),
    })
    _atomic_json(receipt_path, receipt)


def _incomplete_receipts(control_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    incomplete: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(control_dir.glob("upgrade-*.json")):
        if path.is_symlink():
            raise UpgradeError(f"upgrade receipt is a symlink: {path}")
        receipt = _read_json(path)
        if receipt.get("status") not in TERMINAL_STATUSES:
            incomplete.append((path, receipt))
    return incomplete


def _recover_committed_cutover(
    *,
    receipt: dict[str, Any],
    receipt_path: Path,
    current: Path,
    state: Path,
    service: str,
    proxy_service: str,
    start_permit: Path,
    backup_timer: str,
    backup_service: str,
    health_url: str,
    https_health_url: str,
    health_timeout: float,
    health_interval: float,
    command_runner: Callable[..., None],
    health_checker: Callable[[str], bool],
    https_health_checker: Callable[[str], bool],
    post_commit_verifier: Callable[..., dict[str, Any]],
    artifact_verifier: Callable[..., dict[str, Any]],
    materialized_verifier: Callable[..., dict[str, Any]],
    bootstrap_verifier: Callable[[str], dict[str, Any]],
    inactive_checker: Callable[[str], bool],
) -> None:
    """Finish activation after the database/release decision is committed.

    Once this state is durable, automatic database rollback is forbidden:
    background work may already have produced irreversible external effects.
    """
    try:
        _stop_fail_closed(command_runner, proxy_service, inactive_checker)
        _stop_fail_closed(command_runner, service, inactive_checker)
        candidate = _release_root(str(receipt["release"]))
        if _current_target(current) != candidate:
            raise UpgradeError("committed candidate is not selected by current")
        if release_digest(candidate) != str(receipt["release_sha256"]):
            raise UpgradeError("committed candidate digest changed")
        _assert_receipt_artifact_evidence(
            receipt=receipt,
            state=state,
            artifact_verifier=artifact_verifier,
            materialized_verifier=materialized_verifier,
        )
        _assert_receipt_bootstrap_evidence(
            receipt=receipt,
            state=state,
            control_dir=receipt_path.parent,
            bootstrap_verifier=bootstrap_verifier,
        )
        _discard_rollback_ready(receipt, state)
        _clear_quiescent(command_runner)
        with _service_start_permit(start_permit, receipt_path):
            command_runner(["systemctl", "start", service])
        if not _wait_health(
            health_checker, health_url, health_timeout, health_interval
        ):
            raise UpgradeError("committed candidate activation failed")
        command_runner(["systemctl", "is-active", "--quiet", service])
        _resume_and_refresh_backups(
            backup_timer=backup_timer,
            backup_service=backup_service,
            command_runner=command_runner,
        )
        with _service_start_permit(start_permit, receipt_path):
            command_runner(["systemctl", "start", proxy_service])
        command_runner(["systemctl", "is-active", "--quiet", proxy_service])
        control_path = Path(
            str(receipt.get("control_plane_attestation_path") or "")
        )
        expected_control_digest = str(
            receipt.get("control_plane_attestation_sha256") or ""
        )
        if (
            not expected_control_digest
            or not hmac.compare_digest(
                control_plane_attestation_digest(control_path),
                expected_control_digest,
            )
        ):
            raise UpgradeError("committed control-plane attestation changed")
        receipt["post_commit_proof"] = post_commit_verifier(
            state=state,
            periodic_backup_dir=Path(
                str(receipt["periodic_backup_dir"])
            ).resolve(strict=True),
            attestation_path=receipt_path.parent / "latest-periodic-backup.json",
            committed_at_utc=str(receipt["committed_at_utc"]),
            candidate_schema_version=int(receipt["candidate_schema_version"]),
            https_health_url=https_health_url,
            https_health_checker=https_health_checker,
        )
        if not receipt["post_commit_proof"].get("ok"):
            raise UpgradeError("post-cutover recovery proof did not succeed")
    except BaseException as recovery_error:
        try:
            _stop_fail_closed(command_runner, proxy_service, inactive_checker)
        finally:
            try:
                _stop_fail_closed(command_runner, service, inactive_checker)
            finally:
                receipt.update({
                    "status": "cutover_committed_activation_failed",
                    "phase": "committed_recovery_failed",
                    "activation_error": type(recovery_error).__name__,
                })
                _atomic_json(receipt_path, receipt)
        raise UpgradeError(
            "committed cutover could not be activated; no database rollback "
            f"was attempted; receipt: {receipt_path}"
        ) from recovery_error
    receipt.update({
        "status": "succeeded",
        "phase": "complete",
        "completed_at_utc": datetime.now(tz=UTC).isoformat(),
        "active_release": str(candidate),
        "health_url": health_url,
    })
    _atomic_json(receipt_path, receipt)


def _recover_committed_rollback(
    *,
    receipt: dict[str, Any],
    receipt_path: Path,
    current: Path,
    service: str,
    proxy_service: str,
    start_permit: Path,
    backup_timer: str,
    backup_service: str,
    health_url: str,
    health_timeout: float,
    health_interval: float,
    command_runner: Callable[..., None],
    health_checker: Callable[[str], bool],
    inactive_checker: Callable[[str], bool],
) -> None:
    """Finish activating an already committed old-release rollback."""
    try:
        _stop_fail_closed(command_runner, proxy_service, inactive_checker)
        _stop_fail_closed(command_runner, service, inactive_checker)
        previous = _release_root(str(receipt["previous_release"]))
        if _current_target(current) != previous:
            raise UpgradeError("committed rollback release is not selected")
        if release_digest(previous) != str(receipt["previous_release_sha256"]):
            raise UpgradeError("committed rollback release digest changed")
        _clear_quiescent(command_runner)
        with _service_start_permit(start_permit, receipt_path):
            command_runner(["systemctl", "start", service])
        if not _wait_health(
            health_checker, health_url, health_timeout, health_interval
        ):
            raise UpgradeError("committed rollback activation failed")
        command_runner(["systemctl", "is-active", "--quiet", service])
        _resume_and_refresh_backups(
            backup_timer=backup_timer,
            backup_service=backup_service,
            command_runner=command_runner,
        )
        with _service_start_permit(start_permit, receipt_path):
            command_runner(["systemctl", "start", proxy_service])
        command_runner(["systemctl", "is-active", "--quiet", proxy_service])
    except BaseException as recovery_error:
        try:
            _stop_fail_closed(command_runner, proxy_service, inactive_checker)
        finally:
            try:
                _stop_fail_closed(command_runner, service, inactive_checker)
            finally:
                receipt.update({
                    "status": "rollback_committed_activation_failed",
                    "phase": "rollback_activation_failed",
                    "activation_error": type(recovery_error).__name__,
                })
                _atomic_json(receipt_path, receipt)
        raise UpgradeError(
            "committed rollback could not be activated; its database was not "
            f"modified again; receipt: {receipt_path}"
        ) from recovery_error
    receipt.update({
        "status": str(
            receipt.get("rollback_terminal_status") or "recovered_rollback"
        ),
        "phase": "rollback_complete",
        "completed_at_utc": datetime.now(tz=UTC).isoformat(),
        "active_release": str(previous),
    })
    _atomic_json(receipt_path, receipt)


def _recover_interrupted(
    *,
    control_dir: Path,
    current: Path,
    state: Path,
    service: str,
    proxy_service: str,
    service_user: str,
    smoke_user: str,
    smoke_client: Path,
    start_permit: Path,
    backup_timer: str,
    backup_service: str,
    health_url: str,
    https_health_url: str,
    smoke_url: str,
    health_timeout: float,
    health_interval: float,
    command_runner: Callable[..., None],
    health_checker: Callable[[str], bool],
    https_health_checker: Callable[[str], bool],
    post_commit_verifier: Callable[..., dict[str, Any]],
    artifact_verifier: Callable[..., dict[str, Any]],
    materialized_verifier: Callable[..., dict[str, Any]],
    bootstrap_verifier: Callable[[str], dict[str, Any]],
    inactive_checker: Callable[[str], bool],
) -> None:
    incomplete = _incomplete_receipts(control_dir)
    if len(incomplete) > 1:
        _stop_fail_closed(command_runner, proxy_service, inactive_checker)
        _stop_fail_closed(command_runner, service, inactive_checker)
        raise UpgradeError("multiple incomplete upgrade receipts require manual review")
    if not incomplete:
        return
    path, receipt = incomplete[0]
    status = str(receipt.get("status") or "")
    if status.startswith("rollback_committed"):
        _recover_committed_rollback(
            receipt=receipt,
            receipt_path=path,
            current=current,
            service=service,
            proxy_service=proxy_service,
            start_permit=start_permit,
            backup_timer=backup_timer,
            backup_service=backup_service,
            health_url=health_url,
            health_timeout=health_timeout,
            health_interval=health_interval,
            command_runner=command_runner,
            health_checker=health_checker,
            inactive_checker=inactive_checker,
        )
        raise UpgradeError(
            f"an interrupted committed rollback was completed; inspect {path} "
            "and rerun"
        )
    if status.startswith("cutover_committed"):
        _recover_committed_cutover(
            receipt=receipt,
            receipt_path=path,
            current=current,
            state=state,
            service=service,
            proxy_service=proxy_service,
            start_permit=start_permit,
            backup_timer=backup_timer,
            backup_service=backup_service,
            health_url=health_url,
            https_health_url=https_health_url,
            health_timeout=health_timeout,
            health_interval=health_interval,
            command_runner=command_runner,
            health_checker=health_checker,
            https_health_checker=https_health_checker,
            post_commit_verifier=post_commit_verifier,
            artifact_verifier=artifact_verifier,
            materialized_verifier=materialized_verifier,
            bootstrap_verifier=bootstrap_verifier,
            inactive_checker=inactive_checker,
        )
        raise UpgradeError(
            f"an interrupted committed cutover was completed; inspect {path} "
            "and rerun"
        )
    selected = _try_current_target(current)
    previous_supplied = Path(str(receipt.get("previous_release") or "")).absolute()
    receipt_phase = str(receipt.get("phase") or "")
    service_may_be_stopped = (
        _phase_at_least(receipt, "stopping_previous")
        or receipt_phase.startswith("rollback_")
    )
    risky_or_unknown = (
        status.startswith("rollback_failed")
        or service_may_be_stopped
        or selected is None
        or selected != previous_supplied
    )
    # Fail closed before parsing any receipt-bound release path.  A candidate
    # directory may have been deleted/corrupted after a crash, but that must
    # never leave its still-running process online.
    if risky_or_unknown:
        stop_errors: list[BaseException] = []
        for unit in (proxy_service, service):
            try:
                _stop_fail_closed(command_runner, unit, inactive_checker)
            except BaseException as stop_error:
                stop_errors.append(stop_error)
        if stop_errors:
            stop_error = stop_errors[0]
            receipt.update({
                "status": "rollback_failed_uncontained",
                "phase": "recovery_stop_failed",
                "stop_verified": False,
                "stop_error": type(stop_error).__name__,
            })
            _atomic_json(path, receipt)
            raise UpgradeError(
                "interrupted upgrade service could not be proven inactive; "
                f"receipt: {path}"
            ) from stop_error
    if status.startswith("rollback_failed"):
        receipt["stop_verified"] = True
        _atomic_json(path, receipt)
        raise UpgradeError(
            "a prior rollback failure requires explicit manual repair "
            f"before any new upgrade: {path}"
        )
    if not service_may_be_stopped and selected == previous_supplied:
        receipt.update({
            "status": "failed_before_cutover",
            "phase": "recovered_no_cutover",
            "recovered_at_utc": datetime.now(tz=UTC).isoformat(),
        })
        _atomic_json(path, receipt)
        return
    candidate_supplied = Path(str(receipt.get("release") or "")).absolute()
    candidate_may_have_run = (
        selected == candidate_supplied
        or _phase_at_least(receipt, "release_runtime_validation")
        or _phase_at_least(receipt, "candidate_selected")
        or receipt_phase.startswith("rollback_")
    )
    try:
        if Path(str(receipt.get("database", ""))).absolute() != (
            state / "contentcrew.db"
        ).absolute():
            raise UpgradeError(
                "incomplete receipt belongs to a different database"
            )
        _rollback(
            receipt=receipt,
            receipt_path=path,
            current=current,
            state=state,
            service=service,
            proxy_service=proxy_service,
            service_user=service_user,
            smoke_user=smoke_user,
            smoke_client=smoke_client,
            start_permit=start_permit,
            backup_timer=backup_timer,
            backup_service=backup_service,
            health_url=health_url,
            smoke_url=smoke_url,
            health_timeout=health_timeout,
            health_interval=health_interval,
            command_runner=command_runner,
            health_checker=health_checker,
            inactive_checker=inactive_checker,
            restore_database=candidate_may_have_run,
            recovered=True,
        )
    except BaseException as rollback_error:
        _contain_and_record_rollback_failure(
            receipt=receipt,
            receipt_path=path,
            phase="recovery_failed",
            rollback_error=rollback_error,
            service=service,
            proxy_service=proxy_service,
            command_runner=command_runner,
            inactive_checker=inactive_checker,
        )
        raise UpgradeError(
            f"interrupted upgrade recovery failed; service kept stopped; receipt: {path}"
        ) from rollback_error
    raise UpgradeError(
        f"an interrupted upgrade was rolled back; inspect {path} and rerun"
    )


def upgrade_release(
    *,
    release: str | os.PathLike[str],
    current: str | os.PathLike[str],
    state: str | os.PathLike[str],
    backup_dir: str | os.PathLike[str],
    control_dir: str | os.PathLike[str] | None = None,
    venv: str | os.PathLike[str] | None = None,
    previous_venv: str | os.PathLike[str] | None = None,
    service: str = "contentcrew.service",
    proxy_service: str = "caddy.service",
    service_user: str = "paihuo",
    smoke_user: str = "paihuo-smoke",
    smoke_client: str | os.PathLike[str] = (
        "/usr/local/lib/paihuo-ops/deploy/smoke_readonly.py"
    ),
    start_guard: str | os.PathLike[str] = (
        "/usr/local/lib/paihuo-ops/deploy/start_guard.py"
    ),
    start_permit: str | os.PathLike[str] = (
        "/run/paihuo-upgrade/start.permit"
    ),
    backup_timer: str = "paihuo-backup.timer",
    backup_service: str = "paihuo-backup.service",
    backup_health_timer: str = "paihuo-backup-health.timer",
    health_url: str = "http://127.0.0.1:8899/healthz",
    https_health_url: str = "https://paihuo.ai/healthz",
    smoke_url: str = "http://127.0.0.1:8899",
    smoke_credentials: str | os.PathLike[str] | None = None,
    managed_env_file: str | os.PathLike[str] | None = None,
    secret_backup: str | os.PathLike[str] | None = None,
    control_plane_attestation: str | os.PathLike[str] | None = None,
    release_archive: str | os.PathLike[str] | None = None,
    release_build_receipt: str | os.PathLike[str] | None = None,
    release_artifact_attestation: str | os.PathLike[str] | None = None,
    materialized_release_attestation: str | os.PathLike[str] | None = None,
    bootstrap_stage_attestation: str | os.PathLike[str] | None = None,
    health_timeout: float = 90,
    health_interval: float = 2,
    command_runner: Callable[..., None] = _run_command,
    health_checker: Callable[[str], bool] = _health,
    https_health_checker: Callable[[str], bool] = _health,
    post_commit_verifier: Callable[..., dict[str, Any]] = _post_commit_proof,
    control_plane_verifier: Callable[..., dict[str, Any]] = (
        verify_installed_attestation
    ),
    artifact_verifier: Callable[..., dict[str, Any]] = verify_release_artifact,
    materialized_verifier: Callable[..., dict[str, Any]] = (
        verify_materialized_attestation
    ),
    bootstrap_verifier: Callable[[str], dict[str, Any]] = (
        verify_bootstrap_stage
    ),
    inactive_checker: Callable[[str], bool] = _systemd_inactive,
) -> dict[str, Any]:
    state_path = Path(state).resolve(strict=True)
    current_path = Path(current).absolute()
    control = _secure_control_directory(
        control_dir or "/var/lib/paihuo-upgrade",
        state=state_path,
        service_user=service_user,
    )
    smoke_client_path = Path(smoke_client).absolute()
    start_guard_path = Path(start_guard).absolute()
    start_permit_path = Path(start_permit).absolute()

    with _upgrade_lock(control / "upgrade.lock"), _interrupt_guard():
        _recover_interrupted(
            control_dir=control,
            current=current_path,
            state=state_path,
            service=service,
            proxy_service=proxy_service,
            service_user=service_user,
            smoke_user=smoke_user,
            smoke_client=smoke_client_path,
            start_permit=start_permit_path,
            backup_timer=backup_timer,
            backup_service=backup_service,
            health_url=health_url,
            https_health_url=https_health_url,
            smoke_url=smoke_url,
            health_timeout=health_timeout,
            health_interval=health_interval,
            command_runner=command_runner,
            health_checker=health_checker,
            https_health_checker=https_health_checker,
            post_commit_verifier=post_commit_verifier,
            artifact_verifier=artifact_verifier,
            materialized_verifier=materialized_verifier,
            bootstrap_verifier=bootstrap_verifier,
            inactive_checker=inactive_checker,
        )
        # Current invocation inputs are intentionally resolved only after any
        # interrupted prior cutover has been contained and recovered.
        _validate_fixed_helper(smoke_client_path, "read-only smoke client")
        _validate_fixed_helper(start_guard_path, "systemd start guard")
        _validate_smoke_user(smoke_user, service_user)
        release_path = _release_root(release)
        if (
            managed_env_file is None
            or secret_backup is None
            or control_plane_attestation is None
            or release_archive is None
            or release_build_receipt is None
            or release_artifact_attestation is None
            or materialized_release_attestation is None
            or bootstrap_stage_attestation is None
        ):
            raise UpgradeError(
                "candidate managed environment, secret backup and control "
                "plane/artifact attestations are mandatory"
            )
        managed_env_path = Path(managed_env_file).absolute()
        secret_backup_path = Path(secret_backup).absolute()
        control_attestation_path = Path(control_plane_attestation).absolute()
        release_archive_path = Path(release_archive).absolute()
        build_receipt_path = Path(release_build_receipt).absolute()
        artifact_attestation_path = Path(
            release_artifact_attestation
        ).absolute()
        materialized_attestation_path = Path(
            materialized_release_attestation
        ).absolute()
        bootstrap_attestation_path = Path(
            bootstrap_stage_attestation
        ).absolute()
        (
            candidate_schema_version,
            candidate_secrets,
            release_evidence,
        ) = _validate_release_evidence(
            release=release_path,
            state=state_path,
            control_dir=control,
            managed_env_file=managed_env_path,
            secret_backup=secret_backup_path,
            control_plane_attestation=control_attestation_path,
            release_archive=release_archive_path,
            release_build_receipt=build_receipt_path,
            release_artifact_attestation=artifact_attestation_path,
            materialized_release_attestation=materialized_attestation_path,
            bootstrap_stage_attestation=bootstrap_attestation_path,
            control_plane_verifier=control_plane_verifier,
            artifact_verifier=artifact_verifier,
            materialized_verifier=materialized_verifier,
            bootstrap_verifier=bootstrap_verifier,
        )
        venv_path = Path(venv or (release_path / "venv")).resolve(strict=True)
        periodic_backup_dir = Path(backup_dir).resolve(strict=True)
        previous = _current_target(current_path)
        previous_venv_path = Path(
            previous_venv or (previous / "venv")
        ).resolve(strict=True)
        if venv_path == previous_venv_path:
            raise UpgradeError(
                "candidate and previous release must not share a virtualenv"
            )
        candidate_digest = _validate_release_layout(
            release_path, state_path, venv_path
        )
        previous_digest = _validate_release_layout(
            previous, state_path, previous_venv_path
        )
        receipt = create_upgrade_checkpoint(
            release=release_path,
            current=current_path,
            state=state_path,
            backup_dir=control,
        )
        receipt_path = Path(str(receipt["receipt_path"]))
        receipt.update({
            "status": "in_progress",
            "candidate_venv": str(venv_path),
            "previous_venv": str(previous_venv_path),
            "previous_release_sha256": previous_digest,
            "periodic_backup_dir": str(periodic_backup_dir),
            "candidate_schema_version": candidate_schema_version,
            **release_evidence,
        })
        _transition(receipt, receipt_path, "initialized")

        service_may_be_stopped = False
        candidate_may_have_run = False
        cutover_committed = False
        normal_candidate_healthy = False
        try:
            # A broken periodic backup chain blocks deployment even though the
            # release creates its own final checkpoint.
            command_runner(["systemctl", "is-enabled", "--quiet", backup_timer])
            command_runner(["systemctl", "is-active", "--quiet", backup_timer])
            command_runner([
                "systemctl", "is-enabled", "--quiet", backup_health_timer
            ])
            command_runner([
                "systemctl", "is-active", "--quiet", backup_health_timer
            ])
            command_runner(["systemctl", "is-enabled", "--quiet", proxy_service])
            command_runner(["systemctl", "is-active", "--quiet", proxy_service])
            receipt["backup_readiness"] = check_backup_health(
                database=state_path / "contentcrew.db",
                backup_dir=periodic_backup_dir,
                max_age_hours=24,
                attestation_path=control / "latest-periodic-backup.json",
            )
            if control.stat().st_dev != state_path.stat().st_dev:
                raise UpgradeError(
                    "upgrade control and application state must share a "
                    "filesystem for atomic rollback"
                )
            if release_digest(release_path) != candidate_digest:
                raise UpgradeError("candidate release changed after validation")
            if release_digest(previous) != previous_digest:
                raise UpgradeError("previous release changed after validation")
            _transition(receipt, receipt_path, "preflight_complete")

            _transition(receipt, receipt_path, "stopping_previous")
            # From this point all cleanup goes through rollback: the periodic
            # writer and public proxy may already have been stopped.
            service_may_be_stopped = True
            command_runner(["systemctl", "stop", backup_timer])
            _stop_fail_closed(
                command_runner, backup_service, inactive_checker
            )
            _stop_fail_closed(command_runner, proxy_service, inactive_checker)
            _stop_fail_closed(command_runner, service, inactive_checker)
            _transition(receipt, receipt_path, "previous_stopped")

            # This is the only rollback snapshot. Any transaction committed
            # before systemctl stop returned is included.
            final_backup = _verified_backup(
                state=state_path,
                control_dir=control,
                release=release_path,
                label="final-stopped",
            )
            rollback_ready = _prepare_rollback_ready(
                Path(str(final_backup["backup_path"])),
                str(final_backup["sha256"]),
                state_path,
                control,
            )
            _transition(
                receipt,
                receipt_path,
                "final_checkpoint_ready",
                final_backup=final_backup,
                rollback_ready=rollback_ready,
            )
            if release_digest(release_path) != candidate_digest:
                raise UpgradeError("candidate release changed before cutover")
            if release_digest(previous) != previous_digest:
                raise UpgradeError("previous release changed before cutover")

            # Preflight executes code from each release (its Python, module
            # graph and PDF runtime).  It therefore runs only after the old
            # service is stopped and the final rollback snapshot is durable.
            # From this point onward a failed preflight must restore that
            # snapshot, even though the candidate symlink was not selected.
            _transition(
                receipt, receipt_path, "release_runtime_validation"
            )
            candidate_may_have_run = True
            _probe_venv_runtime(
                release=previous,
                state=state_path,
                venv=previous_venv_path,
                service_user=service_user,
                command_runner=command_runner,
            )
            _probe_venv_runtime(
                release=release_path,
                state=state_path,
                venv=venv_path,
                service_user=service_user,
                command_runner=command_runner,
                candidate_secrets=candidate_secrets,
            )
            _validate_locked_runtime(
                release=release_path,
                venv=venv_path,
                service_user=service_user,
                environment=_preflight_env(
                    release_path,
                    state_path,
                    venv_path,
                    candidate_secrets=candidate_secrets,
                ),
                command_runner=command_runner,
            )
            _run_preflight(
                release=previous,
                state=state_path,
                venv=previous_venv_path,
                service_user=service_user,
                command_runner=command_runner,
            )
            _run_preflight(
                release=release_path,
                state=state_path,
                venv=venv_path,
                service_user=service_user,
                command_runner=command_runner,
                candidate_secrets=candidate_secrets,
            )
            if release_digest(release_path) != candidate_digest:
                raise UpgradeError("candidate release changed during preflight")
            if release_digest(previous) != previous_digest:
                raise UpgradeError("previous release changed during preflight")

            _transition(receipt, receipt_path, "switching_candidate")
            try:
                _atomic_symlink(release_path, current_path)
            except BaseException:
                # os.replace may have committed before fsync failed. Record
                # disk truth and let the unified rollback path handle it.
                candidate_may_have_run = (
                    candidate_may_have_run
                    or _try_current_target(current_path) == release_path
                )
                raise
            if _current_target(current_path) != release_path:
                raise UpgradeError("candidate symlink selection did not commit")
            candidate_may_have_run = True
            _transition(receipt, receipt_path, "candidate_selected")

            command_runner([
                "systemctl",
                "set-environment",
                "CONTENTCREW_QUIESCENT=1",
                "CONTENTCREW_MAINTENANCE=1",
            ])
            _transition(receipt, receipt_path, "quiescent_starting")
            with _service_start_permit(start_permit_path, receipt_path):
                command_runner(["systemctl", "start", service])
            if not _wait_health(
                health_checker, health_url, health_timeout, health_interval
            ):
                raise UpgradeError("quiescent candidate did not become healthy")
            command_runner(["systemctl", "is-active", "--quiet", service])
            _transition(receipt, receipt_path, "quiescent_verified")

            _stop_fail_closed(command_runner, service, inactive_checker)
            _set_validation_environment(command_runner)
            _transition(receipt, receipt_path, "validation_starting")
            smoke_id, smoke_name, smoke_password = _create_smoke_identity(state_path)
            try:
                with _service_start_permit(start_permit_path, receipt_path):
                    command_runner(["systemctl", "start", service])
                if not _wait_health(
                    health_checker, health_url, health_timeout, health_interval
                ):
                    raise UpgradeError("validation candidate did not become healthy")
                _authenticated_smoke(
                    smoke_client=smoke_client_path,
                    base_url=smoke_url,
                    credentials=(smoke_name, smoke_password),
                    smoke_user=smoke_user,
                    command_runner=command_runner,
                )
                command_runner(["systemctl", "is-active", "--quiet", service])
            finally:
                _stop_fail_closed(command_runner, service, inactive_checker)
                _delete_smoke_identity(state_path, smoke_id, smoke_name)
            _transition(receipt, receipt_path, "validation_healthy")

            # The candidate and its database are accepted now.  No code below
            # this durable point may restore the old snapshot because normal
            # workers can create irreversible external side effects.
            receipt.update({
                "status": "cutover_committed",
                "phase": "cutover_committed",
                "committed_at_utc": datetime.now(tz=UTC).isoformat(),
                "active_release": str(release_path),
                "health_url": health_url,
            })
            _atomic_json(receipt_path, receipt)
            cutover_committed = True
            _discard_rollback_ready(receipt, state_path)

            _clear_quiescent(command_runner)
            receipt["phase"] = "normal_starting"
            _atomic_json(receipt_path, receipt)
            with _service_start_permit(start_permit_path, receipt_path):
                command_runner(["systemctl", "start", service])
            if not _wait_health(
                health_checker, health_url, health_timeout, health_interval
            ):
                raise UpgradeError("committed candidate did not become healthy")
            command_runner(["systemctl", "is-active", "--quiet", service])
            normal_candidate_healthy = True
            receipt["phase"] = "normal_healthy"
            _atomic_json(receipt_path, receipt)

            _resume_and_refresh_backups(
                backup_timer=backup_timer,
                backup_service=backup_service,
                command_runner=command_runner,
            )
            receipt["phase"] = "proxy_starting"
            _atomic_json(receipt_path, receipt)
            with _service_start_permit(start_permit_path, receipt_path):
                command_runner(["systemctl", "start", proxy_service])
            command_runner(["systemctl", "is-active", "--quiet", proxy_service])
            committed_control_digest = control_plane_attestation_digest(
                control_attestation_path
            )
            if not hmac.compare_digest(
                committed_control_digest,
                str(receipt["control_plane_attestation_sha256"]),
            ):
                raise UpgradeError(
                    "control-plane attestation changed after release validation"
                )
            _assert_receipt_artifact_evidence(
                receipt=receipt,
                state=state_path,
                artifact_verifier=artifact_verifier,
                materialized_verifier=materialized_verifier,
            )
            _assert_receipt_bootstrap_evidence(
                receipt=receipt,
                state=state_path,
                control_dir=control,
                bootstrap_verifier=bootstrap_verifier,
            )
            receipt["post_commit_proof"] = post_commit_verifier(
                state=state_path,
                periodic_backup_dir=periodic_backup_dir,
                attestation_path=control / "latest-periodic-backup.json",
                committed_at_utc=str(receipt["committed_at_utc"]),
                candidate_schema_version=candidate_schema_version,
                https_health_url=https_health_url,
                https_health_checker=https_health_checker,
            )
            if not receipt["post_commit_proof"].get("ok"):
                raise UpgradeError("post-cutover proof did not succeed")
            receipt.update({
                "status": "succeeded",
                "phase": "complete",
                "completed_at_utc": datetime.now(tz=UTC).isoformat(),
            })
            _atomic_json(receipt_path, receipt)
            return receipt
        except BaseException as upgrade_error:
            receipt["error"] = type(upgrade_error).__name__
            receipt["failure_phase"] = str(receipt.get("phase") or "unknown")
            receipt["error_message"] = _sanitized_error_message(
                upgrade_error, candidate_secrets.values()
            )
            if cutover_committed or str(receipt.get("status", "")).startswith(
                "cutover_committed"
            ):
                try:
                    _stop_fail_closed(
                        command_runner, proxy_service, inactive_checker
                    )
                    if not normal_candidate_healthy:
                        _stop_fail_closed(
                            command_runner, service, inactive_checker
                        )
                    try:
                        _resume_and_refresh_backups(
                            backup_timer=backup_timer,
                            backup_service=backup_service,
                            command_runner=command_runner,
                        )
                    except BaseException as backup_error:
                        receipt["backup_resume_error"] = type(
                            backup_error
                        ).__name__
                finally:
                    receipt.update({
                        "status": (
                            "cutover_committed_proxy_failed"
                            if normal_candidate_healthy
                            else "cutover_committed_activation_failed"
                        ),
                        "phase": "committed_activation_failed",
                    })
                    _atomic_json(receipt_path, receipt)
                raise UpgradeError(
                    "cutover was committed but activation failed; public "
                    "traffic remains closed and no database rollback was "
                    f"attempted; receipt: {receipt_path}"
                ) from upgrade_error
            actual = _try_current_target(current_path)
            candidate_may_have_run = (
                candidate_may_have_run
                or actual == release_path
                or _phase_at_least(receipt, "candidate_selected")
            )
            if not service_may_be_stopped and not candidate_may_have_run:
                receipt.update({
                    "status": "failed_before_cutover",
                    "phase": "failed_before_stop",
                })
                _atomic_json(receipt_path, receipt)
                raise
            try:
                _rollback(
                    receipt=receipt,
                    receipt_path=receipt_path,
                    current=current_path,
                    state=state_path,
                    service=service,
                    proxy_service=proxy_service,
                    service_user=service_user,
                    smoke_user=smoke_user,
                    smoke_client=smoke_client_path,
                    start_permit=start_permit_path,
                    backup_timer=backup_timer,
                    backup_service=backup_service,
                    health_url=health_url,
                    smoke_url=smoke_url,
                    health_timeout=health_timeout,
                    health_interval=health_interval,
                    command_runner=command_runner,
                    health_checker=health_checker,
                    inactive_checker=inactive_checker,
                    restore_database=candidate_may_have_run,
                )
                if not candidate_may_have_run:
                    receipt.update({
                        "status": "failed_before_cutover_recovered",
                        "phase": "previous_restarted",
                    })
                    _atomic_json(receipt_path, receipt)
            except BaseException as rollback_error:
                _contain_and_record_rollback_failure(
                    receipt=receipt,
                    receipt_path=receipt_path,
                    phase="rollback_failed",
                    rollback_error=rollback_error,
                    service=service,
                    proxy_service=proxy_service,
                    command_runner=command_runner,
                    inactive_checker=inactive_checker,
                )
                raise UpgradeError(
                    "upgrade failed and rollback failed; service kept stopped; "
                    f"receipt: {receipt_path}"
                ) from rollback_error
            raise UpgradeError(
                f"upgrade failed and was rolled back; receipt: {receipt_path}"
            ) from upgrade_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Upgrade Paihuo with a stopped-service final checkpoint, "
            "quiescent validation and crash-recoverable rollback."
        )
    )
    parser.add_argument("--release", required=True)
    parser.add_argument("--current", default="/srv/paihuo/current")
    parser.add_argument("--state", default="/var/lib/paihuo/data")
    parser.add_argument(
        "--backup-dir", default="/var/backups/paihuo",
        help="periodic application backup directory checked for <24h freshness",
    )
    parser.add_argument(
        "--control-dir", default="/var/lib/paihuo-upgrade",
        help="root-owned 0700 directory for lock, receipts and release checkpoints",
    )
    parser.add_argument(
        "--venv",
        help="candidate virtualenv; defaults to <release>/venv and must live there",
    )
    parser.add_argument(
        "--previous-venv",
        help="previous virtualenv; defaults to <previous-release>/venv",
    )
    parser.add_argument("--service", default="contentcrew.service")
    parser.add_argument("--proxy-service", default="caddy.service")
    parser.add_argument("--service-user", default="paihuo")
    parser.add_argument("--smoke-user", default="paihuo-smoke")
    parser.add_argument(
        "--smoke-client",
        default="/usr/local/lib/paihuo-ops/deploy/smoke_readonly.py",
    )
    parser.add_argument(
        "--start-guard",
        default="/usr/local/lib/paihuo-ops/deploy/start_guard.py",
    )
    parser.add_argument(
        "--start-permit", default="/run/paihuo-upgrade/start.permit"
    )
    parser.add_argument("--backup-timer", default="paihuo-backup.timer")
    parser.add_argument("--backup-service", default="paihuo-backup.service")
    parser.add_argument(
        "--backup-health-timer", default="paihuo-backup-health.timer"
    )
    parser.add_argument(
        "--health-url", default="http://127.0.0.1:8899/healthz"
    )
    parser.add_argument(
        "--https-health-url", default="https://paihuo.ai/healthz"
    )
    parser.add_argument("--smoke-url", default="http://127.0.0.1:8899")
    parser.add_argument("--managed-env-file", required=True)
    parser.add_argument("--secret-backup", required=True)
    parser.add_argument("--control-plane-attestation", required=True)
    parser.add_argument("--release-archive", required=True)
    parser.add_argument("--release-build-receipt", required=True)
    parser.add_argument("--release-artifact-attestation", required=True)
    parser.add_argument("--materialized-release-attestation", required=True)
    parser.add_argument("--bootstrap-stage-attestation", required=True)
    parser.add_argument(
        "--smoke-credentials",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--health-timeout", type=float, default=90)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = vars(_parser().parse_args(argv))
    if os.geteuid() != 0:
        print("upgrade failed: command must run as root", file=sys.stderr)
        return 1
    try:
        report = upgrade_release(**args)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        UpgradeError,
        BackupError,
        BackupHealthError,
        BootstrapError,
        ControlPlaneError,
        SecretEnvError,
        VerificationError,
        ReleaseVerifyError,
        OSError,
        ValueError,
    ) as exc:
        print(f"upgrade failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
