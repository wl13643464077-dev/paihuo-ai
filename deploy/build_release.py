#!/usr/bin/env python3
"""Build a deterministic, allowlisted Paihuo release archive.

The builder deliberately does not mirror the repository.  It packages only a
small, explicit set of runtime inputs, normalizes their metadata, embeds a
content manifest and then re-opens the finished archive to verify every member.
Runtime state and credential-shaped files are never release inputs.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
from typing import Sequence
import uuid


UTC = timezone.utc
MANIFEST_NAME = "RELEASE-MANIFEST.json"
MANIFEST_FORMAT = "paihuo-release-manifest/v1"
RECEIPT_FORMAT = "paihuo-release-receipt/v1"
MAX_RELEASE_FILE_BYTES = 64 * 1024 * 1024
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCHEMA_IN_RELEASE_ID = re.compile(
    r"(?<![A-Za-z0-9])schema(?P<version>[1-9][0-9]*)(?![A-Za-z0-9])"
)

DEFAULT_ROOT_FILES = (
    ".gitignore",
    "PROJECT.md",
    "README.md",
    "requirements.lock.txt",
    "requirements.txt",
    "run.sh",
    "诊断报告-20260725.md",
    "诊断报告2-功能与体验-20260725.md",
)
DEFAULT_ROOT_DIRS = (
    "app",
    "deploy",
    "docs",
    "scripts",
    "static",
    "tests",
)
DEFAULT_DATA_FILES = (
    "data/gate_rules.json",
    "data/departments",
)
DEFAULT_EMPTY_DIRS = ()

_IMMUTABLE_SEED_PATHS = (
    ("data/departments", "config/departments"),
    ("data/gate_rules.json", "config/gate_rules.default.json"),
)
_IGNORED_DEPARTMENT_SEED_SOURCES = frozenset(
    {"data/departments/_source_ten.csv"}
)

_EXECUTABLE_PATHS = frozenset(
    {
        "run.sh",
        "deploy/upgrade.sh",
        "tests/run.sh",
    }
)
_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
_IGNORED_FILE_NAMES = frozenset({".DS_Store"})
_IGNORED_FILE_SUFFIXES = (".pyc", ".pyo", ".swp", ".tmp")
_ARCHIVE_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tgz",
    ".tbz2",
    ".txz",
    ".zip",
    ".7z",
    ".rar",
)
_SENSITIVE_EXACT_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.staging",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_ecdsa",
        "id_rsa",
        "secret",
        "secrets",
        "secrets.json",
    }
)
_SENSITIVE_SUFFIXES = (
    ".backup",
    ".bak",
    ".env",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
)
_SENSITIVE_STEMS = frozenset({"credential", "credentials", "secret", "secrets"})
_RUNTIME_DATA_PREFIXES = (
    "data/assets",
    "data/backups",
    "data/llmwork",
    "data/promo",
    "data/public",
)
_RUNTIME_DATA_EXACT = frozenset(
    {
        "data/contentcrew.db",
        "data/learn_progress.log",
        "data/learn_status.json",
    }
)
_CREDENTIAL_RULES = (
    (
        "aws-access-key",
        re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    ),
    (
        "private-key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    (
        "provider-secret",
        re.compile(
            rb"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-)?"
            rb"[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])"
        ),
    ),
    (
        "github-token",
        re.compile(
            rb"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,}(?![A-Za-z0-9])"
        ),
    ),
    (
        "google-api-key",
        re.compile(rb"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"),
    ),
    (
        "slack-token",
        re.compile(
            rb"(?<![A-Za-z0-9])xox[baprs]-[0-9A-Za-z-]{20,}(?![A-Za-z0-9-])"
        ),
    ),
    (
        "stripe-live-key",
        re.compile(
            rb"(?<![A-Za-z0-9])sk_live_[0-9A-Za-z]{20,}(?![A-Za-z0-9])"
        ),
    ),
    (
        "managed-secret-assignment",
        re.compile(
            rb"(?m)^[ \t]*(?:export[ \t]+)?"
            rb"(?:CONTENTCREW_CONFIG_KEY|CONTENTCREW_SESSION_SECRET|"
            rb"[A-Z][A-Z0-9_]*(?:API_KEY|ACCESS_KEY|SECRET_KEY|AUTH_TOKEN|"
            rb"ACCESS_TOKEN|REFRESH_TOKEN|PRIVATE_KEY|PASSWORD))"
            rb"[ \t]*=[ \t]*[\"']?"
            rb"[A-Za-z0-9_./+=:@-]{24,}[\"']?[ \t]*(?:#.*)?$"
        ),
    ),
)


class ReleaseBuildError(RuntimeError):
    """The source tree cannot be proven safe to package."""


@dataclass(frozen=True)
class _Entry:
    path: str
    kind: str
    mode: int
    body: bytes | None
    source_mode: int | None

    def payload_record(self) -> dict:
        record = {
            "mode": f"{self.mode:04o}",
            "path": self.path,
            "type": self.kind,
        }
        if self.kind == "file":
            assert self.body is not None
            record.update(
                {
                    "sha256": hashlib.sha256(self.body).hexdigest(),
                    "size": len(self.body),
                }
            )
        return record

    def source_record(self) -> dict:
        record = self.payload_record()
        if self.source_mode is not None:
            record["source_mode"] = f"{self.source_mode:04o}"
        return record


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


def _tree_digest(records: Sequence[dict]) -> str:
    return _sha256(_canonical_json(list(records)))


def _relative_path(value: str | os.PathLike[str], *, label: str) -> str:
    raw = os.fspath(value)
    candidate = PurePosixPath(raw.replace("\\", "/"))
    if (
        not raw
        or candidate.is_absolute()
        or candidate in (PurePosixPath("."), PurePosixPath(".."))
        or any(part in ("", ".", "..") for part in candidate.parts)
    ):
        raise ReleaseBuildError(f"{label} contains an unsafe relative path")
    normalized = candidate.as_posix()
    if normalized == MANIFEST_NAME:
        raise ReleaseBuildError(f"{label} collides with the release manifest")
    return normalized


def _sensitive_filename(name: str) -> bool:
    lowered = name.casefold()
    if lowered in _SENSITIVE_EXACT_NAMES:
        return True
    if lowered.startswith(".env.") or lowered.endswith(_SENSITIVE_SUFFIXES):
        return True
    if lowered.endswith(_ARCHIVE_SUFFIXES):
        return True
    return Path(lowered).stem in _SENSITIVE_STEMS


def _ignored_path(relative: PurePosixPath, *, directory: bool) -> bool:
    if directory:
        return relative.name in _IGNORED_DIRECTORY_NAMES
    return (
        relative.name in _IGNORED_FILE_NAMES
        or relative.name.endswith(_IGNORED_FILE_SUFFIXES)
    )


def _runtime_data_path(relative: str) -> bool:
    if relative in _RUNTIME_DATA_EXACT:
        return True
    if relative.startswith("data/contentcrew.db"):
        return True
    if relative.startswith("data/") and relative.casefold().endswith(
        (".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3")
    ):
        return True
    return any(
        relative == prefix or relative.startswith(prefix + "/")
        for prefix in _RUNTIME_DATA_PREFIXES
    )


def _validate_metadata(
    metadata: os.stat_result,
    relative: str,
    *,
    allow_directory: bool,
) -> str:
    if stat.S_ISLNK(metadata.st_mode):
        raise ReleaseBuildError(f"symlink is forbidden in release source: {relative}")
    if stat.S_ISREG(metadata.st_mode):
        if metadata.st_nlink != 1:
            raise ReleaseBuildError(
                f"hard link is forbidden in release source: {relative}"
            )
        kind = "file"
    elif allow_directory and stat.S_ISDIR(metadata.st_mode):
        kind = "dir"
    else:
        raise ReleaseBuildError(
            f"special filesystem entry is forbidden in release source: {relative}"
        )
    if metadata.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise ReleaseBuildError(
            f"setuid/setgid entry is forbidden in release source: {relative}"
        )
    return kind


def _scan_source_tree(source: Path) -> None:
    """Fail on repository hazards, including sensitive unallowlisted names."""
    pending = [source]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise ReleaseBuildError("release source could not be inspected") from exc
        for child in children:
            path = Path(child.path)
            relative_path = path.relative_to(source)
            relative = PurePosixPath(*relative_path.parts)
            metadata = child.stat(follow_symlinks=False)
            is_directory = stat.S_ISDIR(metadata.st_mode)
            if _ignored_path(relative, directory=is_directory):
                continue
            if _sensitive_filename(child.name):
                raise ReleaseBuildError(
                    f"sensitive filename found in release source: {relative.as_posix()}"
                )
            kind = _validate_metadata(
                metadata,
                relative.as_posix(),
                allow_directory=True,
            )
            if kind == "dir":
                pending.append(path)


def _secure_read(path: Path, expected: os.stat_result, relative: str) -> bytes:
    if expected.st_size > MAX_RELEASE_FILE_BYTES:
        raise ReleaseBuildError(f"release file exceeds size limit: {relative}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        actual = os.fstat(descriptor)
        if (
            actual.st_dev != expected.st_dev
            or actual.st_ino != expected.st_ino
            or actual.st_nlink != 1
            or not stat.S_ISREG(actual.st_mode)
            or actual.st_mode & (stat.S_ISUID | stat.S_ISGID)
        ):
            raise ReleaseBuildError(
                f"release source changed during inspection: {relative}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65_536, MAX_RELEASE_FILE_BYTES + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RELEASE_FILE_BYTES:
                raise ReleaseBuildError(f"release file exceeds size limit: {relative}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size != actual.st_size
            or after.st_mtime_ns != actual.st_mtime_ns
            or after.st_ctime_ns != actual.st_ctime_ns
        ):
            raise ReleaseBuildError(
                f"release source changed during inspection: {relative}"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _check_credential_content(body: bytes, relative: str) -> None:
    for rule, pattern in _CREDENTIAL_RULES:
        if pattern.search(body):
            raise ReleaseBuildError(
                "credential-like content found in allowlisted file "
                f"(rule: {rule}): {relative}"
            )


def _release_schema_claim(release_id: str) -> int:
    matches = list(_SCHEMA_IN_RELEASE_ID.finditer(release_id))
    if len(matches) != 1:
        raise ReleaseBuildError(
            "release id must contain exactly one canonical schema claim"
        )
    return int(matches[0].group("version"))


def _source_schema_version(entries: Sequence[_Entry]) -> int:
    candidates = [
        entry for entry in entries
        if entry.path == "app/db.py" and entry.kind == "file"
    ]
    if len(candidates) != 1 or candidates[0].body is None:
        raise ReleaseBuildError(
            "app/db.py must be an allowlisted regular file for schema verification"
        )
    try:
        source = candidates[0].body.decode("utf-8")
        module = ast.parse(source, filename="app/db.py", mode="exec")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ReleaseBuildError(
            "source schema declaration could not be parsed"
        ) from exc

    mutations = [
        node for node in ast.walk(module)
        if (
            isinstance(node, ast.Name)
            and node.id == "LATEST_SCHEMA_VERSION"
            and isinstance(node.ctx, (ast.Store, ast.Del))
        )
    ]
    if len(mutations) != 1:
        raise ReleaseBuildError(
            "source must assign LATEST_SCHEMA_VERSION exactly once"
        )

    values: list[ast.expr] = []
    for statement in module.body:
        if isinstance(statement, ast.Assign):
            if any(
                isinstance(target, ast.Name)
                and target.id == "LATEST_SCHEMA_VERSION"
                for target in statement.targets
            ):
                values.append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "LATEST_SCHEMA_VERSION"
            and statement.value is not None
        ):
            values.append(statement.value)
    if len(values) != 1:
        raise ReleaseBuildError(
            "source must declare LATEST_SCHEMA_VERSION exactly once"
        )
    value = values[0]
    if (
        not isinstance(value, ast.Constant)
        or isinstance(value.value, bool)
        or not isinstance(value.value, int)
        or value.value <= 0
    ):
        raise ReleaseBuildError(
            "source LATEST_SCHEMA_VERSION must be a positive integer literal"
        )
    return value.value


def _verify_schema_consensus(
    expected: int,
    *,
    release_id_claim: int,
    manifest: dict,
    receipt: dict | None = None,
) -> None:
    claims = [
        expected,
        release_id_claim,
        manifest.get("schema_version"),
    ]
    if receipt is not None:
        claims.append(receipt.get("schema_version"))
    if any(
        isinstance(claim, bool)
        or not isinstance(claim, int)
        or claim <= 0
        or claim != expected
        for claim in claims
    ):
        raise ReleaseBuildError(
            "source, release id, manifest and receipt schema claims do not agree"
        )


def _normalized_mode(relative: str, *, kind: str) -> int:
    if kind == "dir":
        return 0o755
    return 0o755 if relative in _EXECUTABLE_PATHS else 0o644


def _collect_path(
    *,
    source: Path,
    relative: str,
    expect: str | None,
    entries: dict[str, _Entry],
) -> None:
    if _runtime_data_path(relative):
        raise ReleaseBuildError(f"runtime data is forbidden in a release: {relative}")
    path = source.joinpath(*PurePosixPath(relative).parts)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise ReleaseBuildError(f"allowlisted release input is missing: {relative}") from exc
    kind = _validate_metadata(metadata, relative, allow_directory=True)
    if expect is not None and kind != expect:
        raise ReleaseBuildError(
            f"allowlisted release input has the wrong type: {relative}"
        )
    if relative in entries:
        existing = entries[relative]
        if existing.kind != kind:
            raise ReleaseBuildError(f"duplicate release input disagrees: {relative}")
        return
    if kind == "file":
        body = _secure_read(path, metadata, relative)
        _check_credential_content(body, relative)
        entries[relative] = _Entry(
            path=relative,
            kind="file",
            mode=_normalized_mode(relative, kind="file"),
            body=body,
            source_mode=stat.S_IMODE(metadata.st_mode),
        )
        return

    entries[relative] = _Entry(
        path=relative,
        kind="dir",
        mode=_normalized_mode(relative, kind="dir"),
        body=None,
        source_mode=stat.S_IMODE(metadata.st_mode),
    )
    try:
        with os.scandir(path) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
    except OSError as exc:
        raise ReleaseBuildError(f"allowlisted directory cannot be read: {relative}") from exc
    for child in children:
        child_relative = f"{relative}/{child.name}"
        child_pure = PurePosixPath(child_relative)
        child_metadata = child.stat(follow_symlinks=False)
        child_is_directory = stat.S_ISDIR(child_metadata.st_mode)
        if _ignored_path(child_pure, directory=child_is_directory):
            continue
        if _sensitive_filename(child.name):
            raise ReleaseBuildError(
                f"sensitive filename found in allowlisted source: {child_relative}"
            )
        _collect_path(
            source=source,
            relative=child_relative,
            expect=None,
            entries=entries,
        )


def _collect_entries(
    source: Path,
    *,
    root_files: Sequence[str],
    root_dirs: Sequence[str],
    data_files: Sequence[str],
    empty_dirs: Sequence[str],
) -> list[_Entry]:
    entries: dict[str, _Entry] = {}
    groups = (
        ("root file allowlist", root_files, "file"),
        ("root directory allowlist", root_dirs, "dir"),
        ("data allowlist", data_files, None),
    )
    seen_inputs: set[str] = set()
    for label, values, expected in groups:
        for value in values:
            relative = _relative_path(value, label=label)
            if relative in seen_inputs:
                raise ReleaseBuildError(f"duplicate allowlist entry: {relative}")
            seen_inputs.add(relative)
            _collect_path(
                source=source,
                relative=relative,
                expect=expected,
                entries=entries,
            )
    for value in empty_dirs:
        relative = _relative_path(value, label="empty directory allowlist")
        if _runtime_data_path(relative) and relative != "data/public":
            raise ReleaseBuildError(
                f"runtime data directory cannot be synthesized: {relative}"
            )
        parts = PurePosixPath(relative).parts
        for index in range(1, len(parts) + 1):
            ancestor = PurePosixPath(*parts[:index]).as_posix()
            existing = entries.get(ancestor)
            if existing is not None and existing.kind != "dir":
                raise ReleaseBuildError(
                    f"empty directory collides with a file: {ancestor}"
                )
            if existing is None:
                entries[ancestor] = _Entry(
                    path=ancestor,
                    kind="dir",
                    mode=0o755,
                    body=None,
                    source_mode=None,
                )

    # Tar archives should describe directory ownership and modes explicitly,
    # rather than relying on an extractor to synthesize parents for a nested
    # allowlisted file.
    for relative in tuple(entries):
        parts = PurePosixPath(relative).parts
        for index in range(1, len(parts)):
            ancestor = PurePosixPath(*parts[:index]).as_posix()
            existing = entries.get(ancestor)
            if existing is not None:
                if existing.kind != "dir":
                    raise ReleaseBuildError(
                        f"release path parent is not a directory: {ancestor}"
                    )
                continue
            source_parent = source.joinpath(*PurePosixPath(ancestor).parts)
            try:
                metadata = os.lstat(source_parent)
            except FileNotFoundError as exc:
                raise ReleaseBuildError(
                    f"release path parent is missing: {ancestor}"
                ) from exc
            kind = _validate_metadata(metadata, ancestor, allow_directory=True)
            if kind != "dir":
                raise ReleaseBuildError(
                    f"release path parent is not a directory: {ancestor}"
                )
            entries[ancestor] = _Entry(
                path=ancestor,
                kind="dir",
                mode=0o755,
                body=None,
                source_mode=stat.S_IMODE(metadata.st_mode),
            )
    collected = [entries[path] for path in sorted(entries)]
    return _map_immutable_seed_entries(collected)


def _immutable_payload_path(relative: str) -> str | None:
    """Map source-only mutable seed paths into immutable release config."""
    if relative == "data":
        return None
    for source_prefix, target_prefix in _IMMUTABLE_SEED_PATHS:
        if relative == source_prefix:
            return target_prefix
        if relative.startswith(source_prefix + "/"):
            return target_prefix + relative[len(source_prefix):]
    if relative.startswith("data/"):
        raise ReleaseBuildError(
            f"release payload cannot contain mutable data path: {relative}"
        )
    return relative


def _map_immutable_seed_entries(entries: Sequence[_Entry]) -> list[_Entry]:
    mapped: dict[str, _Entry] = {}
    for entry in entries:
        if entry.path in _IGNORED_DEPARTMENT_SEED_SOURCES:
            continue
        if entry.path.startswith("data/departments/"):
            child = entry.path.removeprefix("data/departments/")
            if (
                "/" in child
                or entry.kind != "file"
                or not child.endswith(".json")
            ):
                raise ReleaseBuildError(
                    "department release seed contains an unexpected entry: "
                    f"{entry.path}"
                )
        target = _immutable_payload_path(entry.path)
        if target is None:
            continue
        replacement = _Entry(
            path=target,
            kind=entry.kind,
            mode=entry.mode,
            body=entry.body,
            source_mode=entry.source_mode,
        )
        existing = mapped.get(target)
        if existing is not None and existing != replacement:
            raise ReleaseBuildError(
                f"immutable seed mapping collides with release input: {target}"
            )
        mapped[target] = replacement

    for relative in tuple(mapped):
        parts = PurePosixPath(relative).parts
        for index in range(1, len(parts)):
            parent = PurePosixPath(*parts[:index]).as_posix()
            existing = mapped.get(parent)
            if existing is not None:
                if existing.kind != "dir":
                    raise ReleaseBuildError(
                        f"release path parent is not a directory: {parent}"
                    )
                continue
            mapped[parent] = _Entry(
                path=parent,
                kind="dir",
                mode=0o755,
                body=None,
                source_mode=None,
            )
    if any(
        entry.path == "data" or entry.path.startswith("data/")
        for entry in mapped.values()
    ):
        raise ReleaseBuildError("release payload cannot contain data")
    return [mapped[path] for path in sorted(mapped)]


def _write_file(path: Path, body: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(body)
        offset = 0
        while offset < len(view):
            count = os.write(descriptor, view[offset:])
            if count <= 0:
                raise OSError("short write while staging release")
            offset += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_entries(staging: Path, entries: Sequence[_Entry]) -> None:
    staging.mkdir(mode=0o755)
    for entry in entries:
        target = staging.joinpath(*PurePosixPath(entry.path).parts)
        if entry.kind == "dir":
            target.mkdir(parents=True, exist_ok=True, mode=entry.mode)
            target.chmod(entry.mode)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        assert entry.body is not None
        _write_file(target, entry.body, entry.mode)


def _verify_staging(
    staging: Path,
    *,
    entries: Sequence[_Entry],
    manifest_body: bytes,
) -> None:
    expected = _expected_archive_members(entries, manifest_body)
    expected.pop(".")
    expected = {
        name.removeprefix("./"): value for name, value in expected.items()
    }
    seen: set[str] = set()
    pending = [staging]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            path = Path(child.path)
            relative = PurePosixPath(*path.relative_to(staging).parts).as_posix()
            if relative in seen or relative not in expected:
                raise ReleaseBuildError("staged release contains an unexpected entry")
            seen.add(relative)
            metadata = child.stat(follow_symlinks=False)
            kind, mode, body = expected[relative]
            actual_kind = _validate_metadata(
                metadata,
                relative,
                allow_directory=True,
            )
            if actual_kind != kind or stat.S_IMODE(metadata.st_mode) != mode:
                raise ReleaseBuildError("staged release metadata verification failed")
            if kind == "dir":
                pending.append(path)
            else:
                actual = _secure_read(path, metadata, relative)
                if actual != (body or b""):
                    raise ReleaseBuildError("staged release content verification failed")
    if seen != set(expected):
        raise ReleaseBuildError("staged release member set mismatch")


def _tar_info(name: str, *, mode: int, epoch: int, kind: str, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.mtime = epoch
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.pax_headers = {}
    if kind == "dir":
        info.type = tarfile.DIRTYPE
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.size = size
    return info


def _write_archive(
    archive_path: Path,
    *,
    entries: Sequence[_Entry],
    manifest_body: bytes,
    epoch: int,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(archive_path, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw,
                mtime=epoch,
            ) as compressed:
                with tarfile.open(
                    mode="w",
                    fileobj=compressed,
                    format=tarfile.GNU_FORMAT,
                    dereference=False,
                ) as archive:
                    archive.addfile(
                        _tar_info(".", mode=0o755, epoch=epoch, kind="dir")
                    )
                    manifest_info = _tar_info(
                        f"./{MANIFEST_NAME}",
                        mode=0o644,
                        epoch=epoch,
                        kind="file",
                        size=len(manifest_body),
                    )
                    archive.addfile(manifest_info, io.BytesIO(manifest_body))
                    for entry in entries:
                        name = f"./{entry.path}"
                        if entry.kind == "dir":
                            archive.addfile(
                                _tar_info(
                                    name,
                                    mode=entry.mode,
                                    epoch=epoch,
                                    kind="dir",
                                )
                            )
                        else:
                            assert entry.body is not None
                            info = _tar_info(
                                name,
                                mode=entry.mode,
                                epoch=epoch,
                                kind="file",
                                size=len(entry.body),
                            )
                            archive.addfile(info, io.BytesIO(entry.body))
            raw.flush()
            os.fsync(raw.fileno())
        os.fchmod(descriptor, 0o644)
    finally:
        os.close(descriptor)


def _expected_archive_members(
    entries: Sequence[_Entry],
    manifest_body: bytes,
) -> dict[str, tuple[str, int, bytes | None]]:
    expected: dict[str, tuple[str, int, bytes | None]] = {
        ".": ("dir", 0o755, None),
        f"./{MANIFEST_NAME}": ("file", 0o644, manifest_body),
    }
    for entry in entries:
        expected[f"./{entry.path}"] = (entry.kind, entry.mode, entry.body)
    return expected


def _verify_archive(
    archive_path: Path,
    *,
    entries: Sequence[_Entry],
    manifest: dict,
    manifest_body: bytes,
    epoch: int,
    schema_version: int,
    release_id_claim: int,
) -> dict:
    expected = _expected_archive_members(entries, manifest_body)
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) != len(expected):
                raise ReleaseBuildError("release archive member count mismatch")
            for member in members:
                if member.name in seen:
                    raise ReleaseBuildError("release archive contains duplicate members")
                seen.add(member.name)
                expected_entry = expected.get(member.name)
                if expected_entry is None:
                    raise ReleaseBuildError("release archive contains an unexpected member")
                kind, mode, body = expected_entry
                if (
                    member.uid != 0
                    or member.gid != 0
                    or member.uname != "root"
                    or member.gname != "root"
                    or int(member.mtime) != epoch
                    or member.mode != mode
                    or member.pax_headers
                ):
                    raise ReleaseBuildError("release archive metadata verification failed")
                if kind == "dir":
                    if not member.isdir() or member.size != 0:
                        raise ReleaseBuildError("release archive directory verification failed")
                else:
                    if not member.isfile() or member.size != len(body or b""):
                        raise ReleaseBuildError("release archive file verification failed")
                    extracted = archive.extractfile(member)
                    if extracted is None or extracted.read() != body:
                        raise ReleaseBuildError("release archive content verification failed")
            if seen != set(expected):
                raise ReleaseBuildError("release archive member set mismatch")
            embedded_member = archive.getmember(f"./{MANIFEST_NAME}")
            embedded = archive.extractfile(embedded_member)
            if embedded is None:
                raise ReleaseBuildError("embedded release manifest is missing")
            try:
                decoded = json.loads(embedded.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReleaseBuildError("embedded release manifest is invalid") from exc
            if decoded != manifest:
                raise ReleaseBuildError("embedded release manifest does not match")
            _verify_schema_consensus(
                schema_version,
                release_id_claim=release_id_claim,
                manifest=decoded,
            )
    except (OSError, tarfile.TarError) as exc:
        if isinstance(exc, ReleaseBuildError):
            raise
        raise ReleaseBuildError("release archive could not be verified") from exc
    archive_body = archive_path.read_bytes()
    return {
        "archive_sha256": _sha256(archive_body),
        "archive_size": len(archive_body),
        "member_count": len(expected),
    }


def _validate_root(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        metadata = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise ReleaseBuildError(f"{label} does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ReleaseBuildError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseBuildError(f"{label} is not a directory")
    # macOS commonly exposes /var as a system-managed alias for /private/var.
    # Reject a symlink at the caller-supplied root itself, while canonicalizing
    # trusted ancestor aliases so containment checks remain correct.
    return absolute.resolve(strict=True)


def _prepare_output(path: Path) -> Path:
    absolute = path.absolute()
    if absolute.exists():
        return _validate_root(absolute, label="release output directory")
    parent = absolute.parent
    _validate_root(parent, label="release output parent")
    absolute.mkdir(mode=0o755)
    return _validate_root(absolute, label="release output directory")


def build_release(
    *,
    source: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    release_id: str,
    source_date_epoch: int,
    root_files: Sequence[str] = DEFAULT_ROOT_FILES,
    root_dirs: Sequence[str] = DEFAULT_ROOT_DIRS,
    data_files: Sequence[str] = DEFAULT_DATA_FILES,
    empty_dirs: Sequence[str] = DEFAULT_EMPTY_DIRS,
) -> dict:
    """Build and self-verify one immutable release candidate."""
    if not _RELEASE_ID.fullmatch(release_id):
        raise ReleaseBuildError("release id contains unsafe characters")
    release_id_schema = _release_schema_claim(release_id)
    if (
        isinstance(source_date_epoch, bool)
        or not isinstance(source_date_epoch, int)
        or source_date_epoch < 0
    ):
        raise ReleaseBuildError("SOURCE_DATE_EPOCH must be a non-negative integer")
    source_root = _validate_root(Path(source), label="release source")
    output_root = _prepare_output(Path(output_dir))
    if output_root == source_root or source_root in output_root.parents:
        raise ReleaseBuildError("release output must be outside the source tree")

    final_staging = output_root / release_id
    final_archive = output_root / f"{release_id}.tar.gz"
    final_receipt = output_root / f"{release_id}.receipt.json"
    for target in (final_staging, final_archive, final_receipt):
        if target.exists() or target.is_symlink():
            raise ReleaseBuildError("release output already exists")

    _scan_source_tree(source_root)
    entries = _collect_entries(
        source_root,
        root_files=tuple(root_files),
        root_dirs=tuple(root_dirs),
        data_files=tuple(data_files),
        empty_dirs=tuple(empty_dirs),
    )
    source_schema = _source_schema_version(entries)
    if source_schema != release_id_schema:
        raise ReleaseBuildError(
            "source schema does not match the release id schema claim"
        )
    payload_records = [entry.payload_record() for entry in entries]
    source_records = [entry.source_record() for entry in entries]
    payload_tree_sha256 = _tree_digest(payload_records)
    source_tree_sha256 = _tree_digest(source_records)
    schema_version = source_schema
    created_at = (
        datetime.fromtimestamp(source_date_epoch, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    manifest = {
        "created_at": created_at,
        "entries": payload_records,
        "format": MANIFEST_FORMAT,
        "payload_file_count": sum(entry.kind == "file" for entry in entries),
        "payload_member_count": len(entries),
        "payload_tree_sha256": payload_tree_sha256,
        "release_id": release_id,
        "schema_source": "app/db.py:LATEST_SCHEMA_VERSION",
        "schema_version": schema_version,
        "source_date_epoch": source_date_epoch,
        "source_tree_sha256": source_tree_sha256,
    }
    _verify_schema_consensus(
        schema_version,
        release_id_claim=release_id_schema,
        manifest=manifest,
    )
    manifest_body = _canonical_json(manifest)

    workspace = output_root / f".{release_id}.build-{uuid.uuid4().hex}"
    workspace.mkdir(mode=0o700)
    staged_payload = workspace / "payload"
    temporary_archive = workspace / "release.tar.gz"
    temporary_receipt = workspace / "receipt.json"
    try:
        _stage_entries(staged_payload, entries)
        _write_file(staged_payload / MANIFEST_NAME, manifest_body, 0o644)
        _verify_staging(
            staged_payload,
            entries=entries,
            manifest_body=manifest_body,
        )
        _write_archive(
            temporary_archive,
            entries=entries,
            manifest_body=manifest_body,
            epoch=source_date_epoch,
        )
        verified = _verify_archive(
            temporary_archive,
            entries=entries,
            manifest=manifest,
            manifest_body=manifest_body,
            epoch=source_date_epoch,
            schema_version=schema_version,
            release_id_claim=release_id_schema,
        )
        receipt = {
            "archive_filename": final_archive.name,
            "archive_sha256": verified["archive_sha256"],
            "archive_size": verified["archive_size"],
            "created_at": created_at,
            "format": RECEIPT_FORMAT,
            "manifest_sha256": _sha256(manifest_body),
            "member_count": verified["member_count"],
            "payload_tree_sha256": payload_tree_sha256,
            "release_id": release_id,
            "schema_source": "app/db.py:LATEST_SCHEMA_VERSION",
            "schema_version": schema_version,
            "self_verified": True,
            "source_date_epoch": source_date_epoch,
            "source_tree_sha256": source_tree_sha256,
        }
        _verify_schema_consensus(
            schema_version,
            release_id_claim=release_id_schema,
            manifest=manifest,
            receipt=receipt,
        )
        receipt_body = _canonical_json(receipt)
        _write_file(temporary_receipt, receipt_body, 0o644)
        decoded_receipt = json.loads(
            temporary_receipt.read_text(encoding="utf-8")
        )
        if decoded_receipt != receipt:
            raise ReleaseBuildError("external release receipt verification failed")
        _verify_schema_consensus(
            schema_version,
            release_id_claim=release_id_schema,
            manifest=manifest,
            receipt=decoded_receipt,
        )

        os.replace(staged_payload, final_staging)
        os.replace(temporary_archive, final_archive)
        os.replace(temporary_receipt, final_receipt)
        os.chmod(final_archive, 0o644)
        os.chmod(final_receipt, 0o644)
        output_descriptor = os.open(
            output_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(output_descriptor)
        finally:
            os.close(output_descriptor)
    except Exception:
        for target in (final_staging, final_archive, final_receipt):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
        raise
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    return {
        **receipt,
        "archive": str(final_archive),
        "receipt": str(final_receipt),
        "staging": str(final_staging),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and self-verify an allowlisted deterministic Paihuo release."
    )
    parser.add_argument("--source", default=".")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        report = build_release(
            source=arguments.source,
            output_dir=arguments.output_dir,
            release_id=arguments.release_id,
            source_date_epoch=arguments.source_date_epoch,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "status": "failed"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
