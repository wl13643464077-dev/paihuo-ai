#!/usr/bin/env python3
"""Seal and verify an offline, hash-pinned Python wheelhouse.

This module intentionally uses only the Python standard library.  It never
executes a wheel, imports candidate code, invokes pip, or mutates the download
directory.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import compat32
import grp
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import sys
import uuid
import zipfile


ATTESTATION_FORMAT = "paihuo-wheelhouse-attestation/v1"
DEFAULT_TARGET_PLATFORM = "linux_x86_64"
DEFAULT_TARGET_PYTHON = "cp312"
HASHED_LOCK_NAME = "requirements.hashed.lock"
ATTESTATION_NAME = "wheelhouse-attestation.json"
WHEELS_DIR_NAME = "wheels"
MAX_INPUT_SIZE = 1 << 30
MAX_METADATA_SIZE = 1 << 20
MAX_VENV_FILES = 200_000
MAX_VENV_BYTES = 8 << 30
MAX_VENV_DEPTH = 64
FIXED_TOOL_PATH = Path("/var/lib/paihuo-upgrade/bootstrap/wheelhouse.py")
DEFAULT_RELEASES_ROOT = Path("/srv/paihuo/releases")
DEFAULT_QUARANTINE_ROOT = Path("/srv/paihuo/.venv-quarantine")
DEFAULT_PROC_ROOT = Path("/proc")
BUILD_USER = "paihuo-build"
BUILD_GROUP = "paihuo-build"
SERVICE_USER = "paihuo"
SERVICE_GROUP = "paihuo"
_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_EXACT_REQUIREMENT = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"=="
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)"
)
_SAFE_DIGEST = re.compile(r"[0-9a-f]{64}")
_ATTESTATION_KEYS = {
    "format",
    "hashed_lock_sha256",
    "requirements_count",
    "source_lock_sha256",
    "target_platform",
    "target_python",
    "total_bytes",
    "wheel_count",
    "wheel_set_sha256",
    "wheels",
}


class WheelhouseError(RuntimeError):
    """A wheelhouse input or sealed output failed a trust check."""


@dataclass(frozen=True)
class IdentityPolicy:
    root_uid: int
    root_gid: int
    build_uid: int
    build_gid: int
    production: bool


@dataclass(frozen=True)
class AdoptLayout:
    releases_root: Path = DEFAULT_RELEASES_ROOT
    quarantine_root: Path = DEFAULT_QUARANTINE_ROOT
    proc_root: Path = DEFAULT_PROC_ROOT


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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def resolve_identity_policy() -> IdentityPolicy:
    """Resolve root/build custody, with a non-root test/workstation fallback."""

    if os.geteuid() != 0:
        return IdentityPolicy(
            root_uid=os.geteuid(),
            root_gid=os.getegid(),
            build_uid=os.geteuid(),
            build_gid=os.getegid(),
            production=False,
        )
    try:
        account = pwd.getpwnam(BUILD_USER)
        group = grp.getgrnam(BUILD_GROUP)
        service_account = pwd.getpwnam(SERVICE_USER)
        service_group = grp.getgrnam(SERVICE_GROUP)
    except KeyError as exc:
        raise WheelhouseError(
            "paihuo-build or paihuo service identity is missing"
        ) from exc
    if account.pw_gid != group.gr_gid:
        raise WheelhouseError("paihuo-build primary group is inconsistent")
    try:
        build_groups = set(os.getgrouplist(BUILD_USER, account.pw_gid))
    except OSError as exc:
        raise WheelhouseError(
            "paihuo-build supplementary groups cannot be proven"
        ) from exc
    if (
        account.pw_uid == 0
        or group.gr_gid == 0
        or service_account.pw_uid == account.pw_uid
        or service_group.gr_gid == group.gr_gid
        or service_account.pw_gid != service_group.gr_gid
        or build_groups != {group.gr_gid}
    ):
        raise WheelhouseError("paihuo-build identity is not isolated")
    return IdentityPolicy(
        root_uid=0,
        root_gid=0,
        build_uid=account.pw_uid,
        build_gid=group.gr_gid,
        production=True,
    )


def _check_owner(
    info: os.stat_result,
    label: str,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    if info.st_uid != expected_uid or info.st_gid != expected_gid:
        raise WheelhouseError(f"{label} has an unsafe owner")


def _check_directory(
    path: Path,
    label: str,
    *,
    exact_mode: int | None = None,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise WheelhouseError(f"{label} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WheelhouseError(f"{label} is not a regular directory")
    if expected_uid is None or expected_gid is None:
        identity = resolve_identity_policy()
        expected_uid, expected_gid = identity.root_uid, identity.root_gid
    _check_owner(
        info,
        label,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    mode = stat.S_IMODE(info.st_mode)
    if exact_mode is not None:
        if mode != exact_mode:
            raise WheelhouseError(
                f"{label} mode is {mode:04o}, expected {exact_mode:04o}"
            )
    elif mode & 0o022:
        raise WheelhouseError(f"{label} has an unsafe writable mode")
    return info


def _check_regular_info(
    info: os.stat_result,
    label: str,
    *,
    exact_mode: int | None,
    expected_uid: int,
    expected_gid: int,
) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WheelhouseError(f"{label} is not a regular file or is a symlink")
    if info.st_nlink != 1:
        raise WheelhouseError(f"{label} has an unsafe link count")
    _check_owner(
        info,
        label,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    mode = stat.S_IMODE(info.st_mode)
    if exact_mode is not None:
        if mode != exact_mode:
            raise WheelhouseError(
                f"{label} mode is {mode:04o}, expected {exact_mode:04o}"
            )
    elif mode & 0o022:
        raise WheelhouseError(f"{label} has an unsafe writable mode")
    if info.st_size > MAX_INPUT_SIZE:
        raise WheelhouseError(f"{label} is too large")


def _read_regular(
    path: Path,
    label: str,
    *,
    exact_mode: int | None = None,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> bytes:
    """Read once through O_NOFOLLOW and bind lstat/fstat against replacement."""

    if expected_uid is None or expected_gid is None:
        identity = resolve_identity_policy()
        expected_uid, expected_gid = identity.root_uid, identity.root_gid
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise WheelhouseError(f"{label} is missing") from exc
    _check_regular_info(
        before,
        label,
        exact_mode=exact_mode,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WheelhouseError(f"{label} could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        _check_regular_info(
            opened,
            label,
            exact_mode=exact_mode,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise WheelhouseError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise WheelhouseError(f"{label} changed while it was read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise WheelhouseError(f"{label} changed while it was read")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise WheelhouseError(f"{label} changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _hash_regular(
    path: Path,
    label: str,
    *,
    expected_uid: int,
    expected_gid: int,
) -> tuple[str, int]:
    """Hash a potentially large regular file without retaining its contents."""

    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise WheelhouseError(f"{label} is missing") from exc
    _check_regular_info(
        before,
        label,
        exact_mode=None,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WheelhouseError(f"{label} could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        _check_regular_info(
            opened,
            label,
            exact_mode=None,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise WheelhouseError(f"{label} changed while it was opened")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if size > MAX_INPUT_SIZE:
                raise WheelhouseError(f"{label} is too large")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) or size != opened.st_size:
            raise WheelhouseError(f"{label} changed while it was read")
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _check_ancestor_chain(
    directory: Path,
    label: str,
    *,
    identities: IdentityPolicy,
) -> None:
    """Reject rename-capable or untrusted ancestors through the filesystem root."""

    # macOS exposes /var as a system-owned compatibility symlink.  Production
    # remains lexical/fail-closed; non-production tests inspect its resolved
    # physical ancestor chain.
    absolute = (
        directory.absolute()
        if identities.production
        else directory.resolve(strict=True)
    )
    chain = [absolute, *absolute.parents]
    for ancestor in reversed(chain):
        try:
            info = ancestor.lstat()
        except FileNotFoundError as exc:
            raise WheelhouseError(f"{label} ancestor is missing") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WheelhouseError(f"{label} ancestor is not a directory")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            raise WheelhouseError(f"{label} ancestor has an unsafe mode")
        if identities.production:
            if info.st_uid != identities.root_uid or info.st_gid != identities.root_gid:
                raise WheelhouseError(f"{label} ancestor has an unsafe owner")
        else:
            # System ancestors on the test workstation are root-owned, while
            # the temporary subtree is owned by the invoking account.
            if info.st_uid not in (0, identities.root_uid):
                raise WheelhouseError(f"{label} ancestor has an unsafe owner")
            if (
                info.st_uid == identities.root_uid
                and info.st_gid != identities.root_gid
            ):
                raise WheelhouseError(f"{label} ancestor has an unsafe owner")


def parse_exact_lock(
    path: str | os.PathLike[str],
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> list[dict[str, str]]:
    """Parse a strict ``name==version`` runtime lock with no directives."""

    lock_path = Path(path)
    body = _read_regular(
        lock_path,
        "source lock",
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WheelhouseError("source lock is not UTF-8") from exc
    requirements: dict[str, dict[str, str]] = {}
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _EXACT_REQUIREMENT.fullmatch(line)
        if match is None:
            raise WheelhouseError(
                f"source lock line {number} is not exact name==version"
            )
        name = _canonical_name(match.group("name"))
        if name in requirements:
            raise WheelhouseError(f"source lock has duplicate requirement {name}")
        requirements[name] = {
            "name": name,
            "version": match.group("version"),
        }
    if not requirements:
        raise WheelhouseError("source lock has no requirements")
    return [requirements[name] for name in sorted(requirements)]


def _parse_wheel_filename(filename: str) -> dict[str, str]:
    if not filename.endswith(".whl"):
        raise WheelhouseError(f"extra non-wheel file {filename}")
    stem = filename[:-4]
    parts = stem.split("-")
    if len(parts) not in (5, 6) or not all(parts):
        raise WheelhouseError(f"wheel filename is invalid: {filename}")
    return {
        "name": _canonical_name(parts[0]),
        "version": parts[1],
        "python_tag": parts[-3],
        "abi_tag": parts[-2],
        "platform_tag": parts[-1],
    }


def _target_compatible(
    parsed: dict[str, str],
    *,
    target_platform: str,
    target_python: str,
) -> bool:
    if target_platform != DEFAULT_TARGET_PLATFORM:
        raise WheelhouseError(f"unsupported target platform {target_platform}")
    if target_python != DEFAULT_TARGET_PYTHON:
        raise WheelhouseError(f"unsupported target Python {target_python}")
    python_tags = parsed["python_tag"].split(".")
    abi_tags = parsed["abi_tag"].split(".")
    python_ok = target_python in python_tags or "py3" in python_tags
    if "py2" in python_tags and "py3" in python_tags:
        python_ok = True
    for tag in python_tags:
        match = re.fullmatch(r"cp3(?P<minor>[0-9]{2})", tag)
        if match and "abi3" in abi_tags and int(match.group("minor")) <= 12:
            python_ok = True
    platforms = parsed["platform_tag"].split(".")
    platform_ok = "any" in platforms or any(
        (
            platform == "linux_x86_64"
            or (
                platform.startswith("manylinux")
                and platform.endswith("_x86_64")
            )
        )
        for platform in platforms
    )
    return python_ok and platform_ok


def _metadata_identity(data: bytes, filename: str) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise WheelhouseError(f"wheel {filename} has duplicate ZIP entries")
            metadata_names: list[str] = []
            for entry in archive.infolist():
                pure = PurePosixPath(entry.filename)
                if (
                    entry.filename.startswith("/")
                    or "\\" in entry.filename
                    or ".." in pure.parts
                ):
                    raise WheelhouseError(
                        f"wheel {filename} has an unsafe ZIP path"
                    )
                encoded_mode = entry.external_attr >> 16
                encoded_type = stat.S_IFMT(encoded_mode)
                if encoded_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise WheelhouseError(
                        f"wheel {filename} has a special ZIP entry"
                    )
                if entry.filename.endswith(".dist-info/METADATA"):
                    metadata_names.append(entry.filename)
            if len(metadata_names) != 1:
                raise WheelhouseError(
                    f"wheel {filename} must contain exactly one METADATA"
                )
            info = archive.getinfo(metadata_names[0])
            if info.file_size > MAX_METADATA_SIZE:
                raise WheelhouseError(f"wheel {filename} METADATA is too large")
            metadata_body = archive.read(info)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise WheelhouseError(f"wheel {filename} is not a safe ZIP archive") from exc
    message = BytesParser(policy=compat32).parsebytes(metadata_body)
    names = message.get_all("Name", [])
    versions = message.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise WheelhouseError(
            f"wheel {filename} METADATA lacks a unique Name and Version"
        )
    return _canonical_name(names[0].strip()), versions[0].strip()


def _inspect_wheel(
    path: Path,
    *,
    target_platform: str,
    target_python: str,
    exact_mode: int | None = None,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> tuple[dict[str, object], bytes]:
    parsed = _parse_wheel_filename(path.name)
    if not _target_compatible(
        parsed,
        target_platform=target_platform,
        target_python=target_python,
    ):
        raise WheelhouseError(f"wheel {path.name} is incompatible with target")
    data = _read_regular(
        path,
        f"wheel {path.name}",
        exact_mode=exact_mode,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    metadata_name, metadata_version = _metadata_identity(data, path.name)
    if metadata_name != parsed["name"] or metadata_version != parsed["version"]:
        raise WheelhouseError(
            f"wheel {path.name} METADATA does not match its filename"
        )
    record: dict[str, object] = {
        "filename": path.name,
        "name": parsed["name"],
        "sha256": _sha256(data),
        "size": len(data),
        "version": parsed["version"],
    }
    return record, data


def _collect_source_wheels(
    source_wheels: Path,
    *,
    target_platform: str,
    target_python: str,
    identities: IdentityPolicy,
) -> dict[str, tuple[dict[str, object], bytes]]:
    _check_ancestor_chain(
        source_wheels.parent,
        "source wheel directory",
        identities=identities,
    )
    _check_directory(
        source_wheels,
        "source wheel directory",
        expected_uid=identities.build_uid,
        expected_gid=identities.build_gid,
    )
    wheels: dict[str, tuple[dict[str, object], bytes]] = {}
    try:
        children = sorted(source_wheels.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise WheelhouseError("source wheel directory cannot be scanned") from exc
    if not children:
        raise WheelhouseError("source wheel directory is empty")
    for child in children:
        record, data = _inspect_wheel(
            child,
            target_platform=target_platform,
            target_python=target_python,
            expected_uid=identities.build_uid,
            expected_gid=identities.build_gid,
        )
        name = str(record["name"])
        if name in wheels:
            raise WheelhouseError(f"duplicate wheel for requirement {name}")
        wheels[name] = (record, data)
    return wheels


def _write_file(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_stage(path: Path) -> None:
    if not path.exists():
        return
    for root, directories, files in os.walk(path):
        os.chmod(root, 0o700)
        for directory in directories:
            candidate = Path(root) / directory
            if not candidate.is_symlink():
                os.chmod(candidate, 0o700)
        for filename in files:
            candidate = Path(root) / filename
            if not candidate.is_symlink():
                os.chmod(candidate, 0o600)
    shutil.rmtree(path)


def seal_wheelhouse(
    source_lock: str | os.PathLike[str],
    source_wheels: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    target_platform: str = DEFAULT_TARGET_PLATFORM,
    target_python: str = DEFAULT_TARGET_PYTHON,
    identities: IdentityPolicy | None = None,
) -> dict[str, object]:
    """Validate downloads and atomically publish an immutable wheelhouse."""

    lock_path = Path(source_lock)
    wheel_source = Path(source_wheels)
    output = Path(output_dir)
    custody = identities or resolve_identity_policy()
    if output.exists() or output.is_symlink():
        raise WheelhouseError("output directory already exists")
    if not output.is_absolute():
        raise WheelhouseError("output directory must be absolute")
    _check_ancestor_chain(output.parent, "output parent", identities=custody)
    _check_ancestor_chain(lock_path.parent, "source lock", identities=custody)
    _check_directory(
        output.parent,
        "output parent",
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )

    requirements = parse_exact_lock(
        lock_path,
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )
    lock_body = _read_regular(
        lock_path,
        "source lock",
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )
    source_lock_sha256 = _sha256(lock_body)
    wheels = _collect_source_wheels(
        wheel_source,
        target_platform=target_platform,
        target_python=target_python,
        identities=custody,
    )
    expected_names = {row["name"] for row in requirements}
    actual_names = set(wheels)
    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    if missing:
        raise WheelhouseError(f"missing wheels for: {', '.join(missing)}")
    if extra:
        raise WheelhouseError(f"extra wheels for: {', '.join(extra)}")

    records: list[dict[str, object]] = []
    lock_lines: list[str] = []
    for requirement in requirements:
        record, _ = wheels[requirement["name"]]
        if record["version"] != requirement["version"]:
            raise WheelhouseError(
                f"wheel version mismatch for {requirement['name']}"
            )
        records.append(record)
        lock_lines.append(
            f"{requirement['name']}=={requirement['version']} "
            f"--hash=sha256:{record['sha256']}"
        )
    hashed_lock = ("\n".join(lock_lines) + "\n").encode("utf-8")
    wheel_set_sha256 = _sha256(_canonical_json(records))
    attestation: dict[str, object] = {
        "format": ATTESTATION_FORMAT,
        "hashed_lock_sha256": _sha256(hashed_lock),
        "requirements_count": len(requirements),
        "source_lock_sha256": source_lock_sha256,
        "target_platform": target_platform,
        "target_python": target_python,
        "total_bytes": sum(int(row["size"]) for row in records),
        "wheel_count": len(records),
        "wheel_set_sha256": wheel_set_sha256,
        "wheels": records,
    }
    attestation_body = _canonical_json(attestation)

    stage = output.parent / f".{output.name}.stage-{uuid.uuid4().hex}"
    try:
        stage.mkdir(mode=0o700)
        wheel_output = stage / WHEELS_DIR_NAME
        wheel_output.mkdir(mode=0o700)
        for record in records:
            _, data = wheels[str(record["name"])]
            _write_file(wheel_output / str(record["filename"]), data, 0o444)
        _write_file(stage / HASHED_LOCK_NAME, hashed_lock, 0o444)
        _write_file(stage / ATTESTATION_NAME, attestation_body, 0o444)
        _fsync_directory(wheel_output)
        wheel_output.chmod(0o555)
        _fsync_directory(stage)
        stage.chmod(0o555)
        os.replace(stage, output)
        _fsync_directory(output.parent)
    except BaseException:
        _cleanup_stage(stage)
        raise

    verified = verify_wheelhouse(output, identities=custody)
    verified["status"] = "created"
    return verified


def _read_attestation(
    output: Path,
    *,
    identities: IdentityPolicy,
) -> tuple[dict[str, object], bytes]:
    body = _read_regular(
        output / ATTESTATION_NAME,
        "wheelhouse attestation",
        exact_mode=0o444,
        expected_uid=identities.root_uid,
        expected_gid=identities.root_gid,
    )
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WheelhouseError("wheelhouse attestation is not valid JSON") from exc
    if not isinstance(value, dict) or _canonical_json(value) != body:
        raise WheelhouseError("wheelhouse attestation is not canonical JSON")
    if set(value) != _ATTESTATION_KEYS:
        raise WheelhouseError("wheelhouse attestation fields are invalid")
    if value.get("format") != ATTESTATION_FORMAT:
        raise WheelhouseError("wheelhouse attestation format is invalid")
    return value, body


def verify_wheelhouse(
    output_dir: str | os.PathLike[str],
    *,
    identities: IdentityPolicy | None = None,
) -> dict[str, object]:
    """Recompute every sealed wheel and all aggregate digests."""

    output = Path(output_dir)
    custody = identities or resolve_identity_policy()
    _check_ancestor_chain(output.parent, "sealed wheelhouse", identities=custody)
    _check_directory(
        output,
        "sealed wheelhouse",
        exact_mode=0o555,
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )
    wheel_directory = output / WHEELS_DIR_NAME
    _check_directory(
        wheel_directory,
        "sealed wheels",
        exact_mode=0o555,
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )
    expected_top = {WHEELS_DIR_NAME, HASHED_LOCK_NAME, ATTESTATION_NAME}
    actual_top = {path.name for path in output.iterdir()}
    if actual_top != expected_top:
        raise WheelhouseError("sealed wheelhouse has missing or extra entries")
    attestation, _ = _read_attestation(output, identities=custody)
    target_platform = attestation.get("target_platform")
    target_python = attestation.get("target_python")
    if (
        target_platform != DEFAULT_TARGET_PLATFORM
        or target_python != DEFAULT_TARGET_PYTHON
    ):
        raise WheelhouseError("wheelhouse attestation target is invalid")
    attested_records = attestation.get("wheels")
    if not isinstance(attested_records, list):
        raise WheelhouseError("wheelhouse attestation wheel list is invalid")

    actual_records: list[dict[str, object]] = []
    actual_files = sorted(wheel_directory.iterdir(), key=lambda path: path.name)
    for path in actual_files:
        record, _ = _inspect_wheel(
            path,
            target_platform=str(target_platform),
            target_python=str(target_python),
            exact_mode=0o444,
            expected_uid=custody.root_uid,
            expected_gid=custody.root_gid,
        )
        actual_records.append(record)
    actual_records.sort(key=lambda row: str(row["name"]))
    if actual_records != attested_records:
        raise WheelhouseError("sealed wheel hash, size, or metadata is invalid")
    names = [str(row["name"]) for row in actual_records]
    if len(names) != len(set(names)):
        raise WheelhouseError("sealed wheelhouse has duplicate distributions")

    wheel_set_sha256 = _sha256(_canonical_json(actual_records))
    if attestation.get("wheel_set_sha256") != wheel_set_sha256:
        raise WheelhouseError("wheel set hash is invalid")
    hashed_lock = _read_regular(
        output / HASHED_LOCK_NAME,
        "hashed requirements lock",
        exact_mode=0o444,
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )
    expected_hashed_lock = (
        "\n".join(
            f"{row['name']}=={row['version']} --hash=sha256:{row['sha256']}"
            for row in actual_records
        )
        + "\n"
    ).encode("utf-8")
    if hashed_lock != expected_hashed_lock:
        raise WheelhouseError("hashed requirements lock content is invalid")
    if attestation.get("hashed_lock_sha256") != _sha256(hashed_lock):
        raise WheelhouseError("hashed requirements lock hash is invalid")
    expected_scalars = {
        "requirements_count": len(actual_records),
        "wheel_count": len(actual_records),
        "total_bytes": sum(int(row["size"]) for row in actual_records),
    }
    for key, expected in expected_scalars.items():
        if attestation.get(key) != expected:
            raise WheelhouseError(f"wheelhouse attestation {key} is invalid")
    if not isinstance(attestation.get("source_lock_sha256"), str) or not (
        _SAFE_DIGEST.fullmatch(str(attestation["source_lock_sha256"]))
    ):
        raise WheelhouseError("source lock hash is invalid")
    result = dict(attestation)
    result["status"] = "verified"
    return result


def _validate_release_id(release_id: str) -> str:
    if _RELEASE_ID.fullmatch(release_id) is None:
        raise WheelhouseError("release id is unsafe")
    return release_id


def _release_id_argument(release_id: str) -> str:
    try:
        return _validate_release_id(release_id)
    except WheelhouseError as exc:
        raise argparse.ArgumentTypeError("release id is unsafe") from exc


def _scan_venv_tree(
    root: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> dict[str, object]:
    try:
        root_info = root.lstat()
    except FileNotFoundError as exc:
        raise WheelhouseError("venv is missing") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise WheelhouseError("venv is not a regular directory or is a symlink")
    _check_owner(
        root_info,
        "venv",
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    root_mode = stat.S_IMODE(root_info.st_mode)
    if root_mode != 0o755:
        raise WheelhouseError(
            "venv root mode must be 0755 for the service identity"
        )
    if root_mode & 0o022:
        raise WheelhouseError("venv root has an unsafe mode")
    if root_mode & 0o7000:
        raise WheelhouseError("venv root has unsafe special mode bits")
    tree_device = root_info.st_dev

    records: list[dict[str, object]] = []
    paths: list[Path] = []
    file_count = 0
    total_bytes = 0

    def add_directory(path: Path, relative: str) -> None:
        nonlocal file_count
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise WheelhouseError("venv tree changed during inspection") from exc
        if stat.S_ISLNK(info.st_mode):
            raise WheelhouseError(f"venv contains a symlink: {relative}")
        if not stat.S_ISDIR(info.st_mode):
            raise WheelhouseError(f"venv contains a special entry: {relative}")
        if info.st_dev != tree_device:
            raise WheelhouseError(f"venv crosses a filesystem: {relative}")
        _check_owner(
            info,
            f"venv entry {relative}",
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            raise WheelhouseError(f"venv entry {relative} has an unsafe mode")
        if mode & 0o7000:
            raise WheelhouseError(
                f"venv entry {relative} has unsafe special mode bits"
            )
        if mode & 0o005 != 0o005:
            raise WheelhouseError(
                f"venv entry {relative} is not traversable by the service identity"
            )
        records.append(
            {
                "mode": f"{mode:04o}",
                "path": relative,
                "type": "directory",
            }
        )
        paths.append(path)
        if len(records) > MAX_VENV_FILES:
            raise WheelhouseError("venv tree exceeds the entry limit")

    add_directory(root, ".")

    def walk_error(error: OSError) -> None:
        raise WheelhouseError("venv tree could not be scanned") from error

    for current, directory_names, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        current_path = Path(current)
        directory_names.sort()
        filenames.sort()
        current_relative = current_path.relative_to(root)
        if len(current_relative.parts) > MAX_VENV_DEPTH:
            raise WheelhouseError("venv tree exceeds the depth limit")
        for directory_name in directory_names:
            candidate = current_path / directory_name
            relative = candidate.relative_to(root).as_posix()
            add_directory(candidate, relative)
        for filename in filenames:
            candidate = current_path / filename
            relative = candidate.relative_to(root).as_posix()
            try:
                info = candidate.lstat()
            except FileNotFoundError as exc:
                raise WheelhouseError(
                    "venv tree changed during inspection"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise WheelhouseError(f"venv contains a symlink: {relative}")
            if not stat.S_ISREG(info.st_mode):
                raise WheelhouseError(
                    f"venv contains a special entry: {relative}"
                )
            if info.st_dev != tree_device:
                raise WheelhouseError(f"venv crosses a filesystem: {relative}")
            if info.st_nlink != 1:
                raise WheelhouseError(
                    f"venv entry {relative} has an unsafe link count"
                )
            _check_owner(
                info,
                f"venv entry {relative}",
                expected_uid=expected_uid,
                expected_gid=expected_gid,
            )
            mode = stat.S_IMODE(info.st_mode)
            if mode & 0o022:
                raise WheelhouseError(
                    f"venv entry {relative} has an unsafe mode"
                )
            if mode & 0o7000:
                raise WheelhouseError(
                    f"venv entry {relative} has unsafe special mode bits"
                )
            if mode & 0o004 != 0o004:
                raise WheelhouseError(
                    f"venv entry {relative} is not readable by the service identity"
                )
            if mode & 0o111 and not mode & 0o001:
                raise WheelhouseError(
                    f"venv entry {relative} is not executable by the service identity"
                )
            digest, size = _hash_regular(
                candidate,
                f"venv entry {relative}",
                expected_uid=expected_uid,
                expected_gid=expected_gid,
            )
            file_count += 1
            total_bytes += size
            if (
                len(records) + 1 > MAX_VENV_FILES
                or total_bytes > MAX_VENV_BYTES
            ):
                raise WheelhouseError("venv tree exceeds its safety limits")
            records.append(
                {
                    "mode": f"{mode:04o}",
                    "path": relative,
                    "sha256": digest,
                    "size": size,
                    "type": "file",
                }
            )
            paths.append(candidate)
    records.sort(key=lambda row: str(row["path"]))
    return {
        "_paths": paths,
        "entry_count": len(records),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "venv_tree_sha256": _sha256(_canonical_json(records)),
    }


def inspect_venv_tree(
    root: str | os.PathLike[str],
    *,
    expected_uid: int,
    expected_gid: int,
) -> dict[str, object]:
    """Return a content/mode tree digest without exposing individual paths."""

    result = _scan_venv_tree(
        Path(root),
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    result.pop("_paths", None)
    return result


def _uid_has_process(uid: int, proc_root: Path) -> bool:
    try:
        info = proc_root.lstat()
    except FileNotFoundError as exc:
        raise WheelhouseError("process table is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise WheelhouseError("process table is unsafe")
    try:
        processes = list(proc_root.iterdir())
    except OSError as exc:
        raise WheelhouseError("process table cannot be scanned") from exc
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            with (process / "status").open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.startswith("Uid:"):
                        continue
                    fields = line.split()[1:]
                    if len(fields) != 4:
                        raise WheelhouseError("process identity is malformed")
                    if uid in {int(value) for value in fields}:
                        return True
                    break
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            raise WheelhouseError("process identity cannot be verified") from exc
    return False


def _chown_scanned_tree(
    scan: dict[str, object],
    *,
    from_uid: int,
    from_gid: int,
    to_uid: int,
    to_gid: int,
) -> None:
    paths = scan.get("_paths")
    if not isinstance(paths, list):
        raise WheelhouseError("internal venv scan is incomplete")
    # Deepest entries first; the root directory is transferred last.
    ordered = sorted(
        (Path(path) for path in paths),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in ordered:
        before = path.lstat()
        _check_owner(
            before,
            "quarantined venv entry",
            expected_uid=from_uid,
            expected_gid=from_gid,
        )
        if stat.S_ISLNK(before.st_mode):
            raise WheelhouseError("quarantined venv gained a symlink")
        if not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
            raise WheelhouseError("quarantined venv gained a special entry")
        if stat.S_ISREG(before.st_mode) and before.st_nlink != 1:
            raise WheelhouseError("quarantined venv gained a hardlink")
        os.chown(path, to_uid, to_gid, follow_symlinks=False)
        after = path.lstat()
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise WheelhouseError("quarantined venv changed during adoption")
        _check_owner(
            after,
            "adopted venv entry",
            expected_uid=to_uid,
            expected_gid=to_gid,
        )


def adopt_venv(
    release_id: str,
    *,
    layout: AdoptLayout = AdoptLayout(),
    identities: IdentityPolicy | None = None,
    process_checker=None,
) -> dict[str, object]:
    """Atomically quarantine and adopt a build-user-owned virtual environment."""

    safe_release_id = _validate_release_id(release_id)
    custody = identities or resolve_identity_policy()
    if custody.production and (
        custody.root_uid != 0
        or custody.root_gid != 0
        or custody.build_uid == 0
    ):
        raise WheelhouseError("production identity policy is unsafe")
    checker = process_checker or _uid_has_process
    releases_root = Path(layout.releases_root)
    release = releases_root / safe_release_id
    venv = release / "venv"
    quarantine_root = Path(layout.quarantine_root)

    _check_ancestor_chain(
        releases_root.parent,
        "release root",
        identities=custody,
    )
    _check_directory(
        releases_root,
        "release root",
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )
    _check_directory(
        release,
        "release",
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )
    _check_ancestor_chain(
        quarantine_root.parent,
        "quarantine",
        identities=custody,
    )
    _check_directory(
        quarantine_root,
        "quarantine",
        exact_mode=0o700,
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )
    initial = _scan_venv_tree(
        venv,
        expected_uid=custody.build_uid,
        expected_gid=custody.build_gid,
    )
    quarantine_device = quarantine_root.stat().st_dev
    if (
        release.stat().st_dev != quarantine_device
        or venv.lstat().st_dev != quarantine_device
    ):
        raise WheelhouseError("release and quarantine are on different filesystems")
    if checker(custody.build_uid, Path(layout.proc_root)):
        raise WheelhouseError("paihuo-build process is still running")

    quarantined = (
        quarantine_root
        / f"{safe_release_id}.venv-{uuid.uuid4().hex}"
    )
    if quarantined.exists() or quarantined.is_symlink():
        raise WheelhouseError("quarantine target already exists")
    os.rename(venv, quarantined)
    _fsync_directory(release)
    _fsync_directory(quarantine_root)

    if checker(custody.build_uid, Path(layout.proc_root)):
        raise WheelhouseError(
            "paihuo-build process appeared after quarantine"
        )
    quarantined_scan = _scan_venv_tree(
        quarantined,
        expected_uid=custody.build_uid,
        expected_gid=custody.build_gid,
    )
    if (
        quarantined_scan["venv_tree_sha256"]
        != initial["venv_tree_sha256"]
    ):
        raise WheelhouseError("venv changed across quarantine")
    _chown_scanned_tree(
        quarantined_scan,
        from_uid=custody.build_uid,
        from_gid=custody.build_gid,
        to_uid=custody.root_uid,
        to_gid=custody.root_gid,
    )
    adopted_scan = _scan_venv_tree(
        quarantined,
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )
    if adopted_scan["venv_tree_sha256"] != initial["venv_tree_sha256"]:
        raise WheelhouseError("venv changed during ownership adoption")
    if checker(custody.build_uid, Path(layout.proc_root)):
        raise WheelhouseError(
            "paihuo-build process appeared before publication"
        )
    if venv.exists() or venv.is_symlink():
        raise WheelhouseError("final venv path was unexpectedly recreated")
    os.rename(quarantined, venv)
    _fsync_directory(release)
    _fsync_directory(quarantine_root)
    return {
        "entry_count": adopted_scan["entry_count"],
        "file_count": adopted_scan["file_count"],
        "release_id": safe_release_id,
        "status": "adopted",
        "total_bytes": adopted_scan["total_bytes"],
        "venv_tree_sha256": adopted_scan["venv_tree_sha256"],
    }


_LOADED_TOOL_PATH = Path(__file__).absolute()
_LOADED_TOOL_INFO = _LOADED_TOOL_PATH.lstat()
with _LOADED_TOOL_PATH.open("rb") as _loaded_tool_handle:
    _LOADED_TOOL_SHA256 = _sha256(_loaded_tool_handle.read())


def _assert_trusted_entrypoint(
    *,
    fixed_path: Path = FIXED_TOOL_PATH,
    loaded_path: Path = _LOADED_TOOL_PATH,
    loaded_info: os.stat_result = _LOADED_TOOL_INFO,
    loaded_sha256: str = _LOADED_TOOL_SHA256,
    identities: IdentityPolicy | None = None,
) -> None:
    custody = identities or IdentityPolicy(
        root_uid=0,
        root_gid=0,
        build_uid=-1,
        build_gid=-1,
        production=True,
    )
    if loaded_path != fixed_path:
        raise WheelhouseError("wheelhouse CLI is not the fixed trusted tool")
    _check_ancestor_chain(
        fixed_path.parent,
        "fixed wheelhouse tool",
        identities=custody,
    )
    body = _read_regular(
        fixed_path,
        "fixed wheelhouse tool",
        exact_mode=0o600,
        expected_uid=custody.root_uid,
        expected_gid=custody.root_gid,
    )
    current = fixed_path.lstat()
    if (
        current.st_dev,
        current.st_ino,
    ) != (
        loaded_info.st_dev,
        loaded_info.st_ino,
    ):
        raise WheelhouseError("fixed wheelhouse tool identity changed")
    if _sha256(body) != loaded_sha256:
        raise WheelhouseError("fixed wheelhouse tool hash changed")


class _CliParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if parsed.seal and not (
            parsed.source_lock and parsed.source_wheels and parsed.output
        ):
            self.error(
                "--seal requires --source-lock, --source-wheels, and --output"
            )
        if parsed.verify and not parsed.output:
            self.error("--verify requires --output")
        if parsed.adopt_venv and not parsed.release_id:
            self.error("--adopt-venv requires --release-id")
        return parsed


def _parser() -> argparse.ArgumentParser:
    parser = _CliParser(
        description="Seal or verify an offline Python wheelhouse."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--seal", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--adopt-venv", action="store_true")
    parser.add_argument("--source-lock")
    parser.add_argument("--source-wheels")
    parser.add_argument("--output")
    parser.add_argument("--release-id", type=_release_id_argument)
    parser.add_argument(
        "--target-platform",
        default=DEFAULT_TARGET_PLATFORM,
        choices=[DEFAULT_TARGET_PLATFORM],
    )
    parser.add_argument(
        "--target-python",
        default=DEFAULT_TARGET_PYTHON,
        choices=[DEFAULT_TARGET_PYTHON],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if os.geteuid() != 0:
        print(
            json.dumps(
                {"ok": False, "error": "RootRequired"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    try:
        _assert_trusted_entrypoint()
        identities = resolve_identity_policy()
    except WheelhouseError as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.seal:
            result = seal_wheelhouse(
                args.source_lock,
                args.source_wheels,
                args.output,
                target_platform=args.target_platform,
                target_python=args.target_python,
                identities=identities,
            )
        elif args.verify:
            if args.source_lock or args.source_wheels:
                parser.error("--verify does not accept source paths")
            result = verify_wheelhouse(args.output, identities=identities)
        else:
            if args.source_lock or args.source_wheels or args.output:
                parser.error("--adopt-venv accepts only --release-id")
            result = adopt_venv(args.release_id, identities=identities)
    except WheelhouseError as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
