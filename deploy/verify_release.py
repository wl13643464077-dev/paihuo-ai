#!/usr/bin/env python3
"""Standalone, standard-library-only Paihuo release verifier.

This file is intentionally self-contained so an operator can copy it onto a
server and verify an archive before any candidate code is extracted or trusted.
It also verifies an already materialized release.  In that mode the only
entries outside the signed manifest are the exact ``data`` state symlink and a
real ``venv`` directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import uuid


UTC = timezone.utc
MANIFEST_NAME = "RELEASE-MANIFEST.json"
MANIFEST_FORMAT = "paihuo-release-manifest/v1"
RECEIPT_FORMAT = "paihuo-release-receipt/v1"
ARCHIVE_ATTESTATION_FORMAT = "paihuo-release-attestation/v1"
MATERIALIZED_ATTESTATION_FORMAT = (
    "paihuo-materialized-release-attestation/v1"
)
MAX_RECEIPT_BYTES = 1 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RELEASE_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_PAYLOAD_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCHEMA_IN_RELEASE_ID = re.compile(
    r"(?<![A-Za-z0-9])schema(?P<version>[1-9][0-9]*)(?![A-Za-z0-9])"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = frozenset(
    {
        "created_at",
        "entries",
        "format",
        "payload_file_count",
        "payload_member_count",
        "payload_tree_sha256",
        "release_id",
        "schema_source",
        "schema_version",
        "source_date_epoch",
        "source_tree_sha256",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "archive_filename",
        "archive_sha256",
        "archive_size",
        "created_at",
        "format",
        "manifest_sha256",
        "member_count",
        "payload_tree_sha256",
        "release_id",
        "schema_source",
        "schema_version",
        "self_verified",
        "source_date_epoch",
        "source_tree_sha256",
    }
)
_ARTIFACT_ATTESTATION_KEYS = frozenset(
    {
        "archive_sha256",
        "archive_size",
        "attested_at",
        "extract_dir",
        "format",
        "manifest_sha256",
        "member_count",
        "ok",
        "payload_tree_sha256",
        "receipt_sha256",
        "release_id",
        "schema_version",
        "source_tree_sha256",
    }
)
_MATERIALIZED_ATTESTATION_KEYS = frozenset(
    {
        "artifact_archive_sha256",
        "artifact_attestation_sha256",
        "artifact_receipt_sha256",
        "attested_at",
        "data_target",
        "format",
        "manifest_sha256",
        "materialized_release",
        "ok",
        "payload_tree_sha256",
        "release_id",
        "schema_version",
        "source_tree_sha256",
    }
)


class ReleaseVerifyError(RuntimeError):
    """A release cannot be proven to match its signed build evidence."""


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


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseVerifyError(message)


def _positive_int(value: object, *, label: str, allow_zero: bool = False) -> int:
    _require(
        isinstance(value, int)
        and not isinstance(value, bool)
        and (value >= 0 if allow_zero else value > 0),
        f"{label} must be a valid integer",
    )
    return value


def _hash_claim(value: object, *, label: str) -> str:
    _require(
        isinstance(value, str) and _SHA256.fullmatch(value) is not None,
        f"{label} must be a lowercase SHA-256 digest",
    )
    return value


def _created_at(epoch: int) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _release_schema_claim(release_id: object) -> int:
    _require(
        isinstance(release_id, str)
        and _RELEASE_ID.fullmatch(release_id) is not None,
        "release id is invalid",
    )
    matches = list(_SCHEMA_IN_RELEASE_ID.finditer(release_id))
    _require(
        len(matches) == 1,
        "release id must contain exactly one canonical schema claim",
    )
    return int(matches[0].group("version"))


def _safe_payload_path(value: object) -> str:
    _require(isinstance(value, str) and value, "manifest path is invalid")
    _require("\\" not in value, "manifest path contains a backslash")
    candidate = PurePosixPath(value)
    _require(
        not candidate.is_absolute()
        and candidate not in (PurePosixPath("."), PurePosixPath(".."))
        and all(part not in ("", ".", "..") for part in candidate.parts),
        "manifest path is unsafe",
    )
    normalized = candidate.as_posix()
    _require(normalized == value, "manifest path is not canonical")
    _require(
        normalized != MANIFEST_NAME,
        "manifest entry collides with embedded manifest",
    )
    _require(
        normalized != "data"
        and not normalized.startswith("data/")
        and normalized != "venv"
        and not normalized.startswith("venv/"),
        "manifest payload contains mutable runtime state",
    )
    return normalized


def _read_regular(path: Path, *, label: str, maximum: int) -> bytes:
    absolute = path.absolute()
    try:
        metadata = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise ReleaseVerifyError(f"{label} is missing") from exc
    _require(
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_nlink == 1,
        f"{label} must be one regular unlinked file",
    )
    _require(metadata.st_size <= maximum, f"{label} exceeds size limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise ReleaseVerifyError(f"{label} could not be opened safely") from exc
    try:
        actual = os.fstat(descriptor)
        _require(
            actual.st_dev == metadata.st_dev
            and actual.st_ino == metadata.st_ino
            and actual.st_nlink == 1
            and stat.S_ISREG(actual.st_mode),
            f"{label} changed during open",
        )
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
            after.st_size == actual.st_size
            and after.st_mtime_ns == actual.st_mtime_ns
            and after.st_ctime_ns == actual.st_ctime_ns,
            f"{label} changed while being read",
        )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_canonical_json(body: bytes, *, label: str) -> dict:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseVerifyError(f"{label} is invalid JSON") from exc
    _require(isinstance(value, dict), f"{label} must be a JSON object")
    _require(
        body == _canonical_json(value),
        f"{label} is not canonical JSON",
    )
    return value


def _validate_entry(value: object) -> dict:
    _require(isinstance(value, dict), "manifest entry must be an object")
    kind = value.get("type")
    _require(kind in ("dir", "file"), "manifest entry type is invalid")
    expected_keys = (
        {"mode", "path", "type", "sha256", "size"}
        if kind == "file"
        else {"mode", "path", "type"}
    )
    _require(set(value) == expected_keys, "manifest entry fields are invalid")
    path = _safe_payload_path(value["path"])
    expected_mode = "0755" if kind == "dir" else (
        "0755"
        if path in {"run.sh", "deploy/upgrade.sh", "tests/run.sh"}
        else "0644"
    )
    _require(value.get("mode") == expected_mode, "manifest entry mode is invalid")
    normalized = {
        "mode": expected_mode,
        "path": path,
        "type": kind,
    }
    if kind == "file":
        normalized["sha256"] = _hash_claim(
            value.get("sha256"),
            label="manifest file sha256",
        )
        normalized["size"] = _positive_int(
            value.get("size"),
            label="manifest file size",
            allow_zero=True,
        )
        _require(
            normalized["size"] <= MAX_RELEASE_FILE_BYTES,
            "manifest file exceeds size limit",
        )
    return normalized


def _validate_manifest(value: dict) -> tuple[dict, dict[str, dict]]:
    _require(set(value) == _MANIFEST_KEYS, "manifest fields are invalid")
    _require(value.get("format") == MANIFEST_FORMAT, "manifest format is unsupported")
    release_id = value.get("release_id")
    schema_claim = _release_schema_claim(release_id)
    schema_version = _positive_int(
        value.get("schema_version"),
        label="manifest schema version",
    )
    _require(schema_version == schema_claim, "manifest schema claims disagree")
    _require(
        value.get("schema_source") == "app/db.py:LATEST_SCHEMA_VERSION",
        "manifest schema source is invalid",
    )
    epoch = _positive_int(
        value.get("source_date_epoch"),
        label="manifest source date epoch",
        allow_zero=True,
    )
    _require(
        value.get("created_at") == _created_at(epoch),
        "manifest timestamp does not match source date epoch",
    )
    entries = value.get("entries")
    _require(isinstance(entries, list), "manifest entries must be a list")
    _require(len(entries) <= MAX_MEMBERS - 2, "manifest has too many entries")
    normalized = [_validate_entry(entry) for entry in entries]
    paths = [entry["path"] for entry in normalized]
    _require(paths == sorted(paths), "manifest entries are not sorted")
    _require(len(paths) == len(set(paths)), "manifest contains duplicate paths")
    total_size = sum(
        entry.get("size", 0) for entry in normalized
    )
    _require(
        total_size <= MAX_TOTAL_PAYLOAD_BYTES,
        "manifest total payload exceeds size limit",
    )
    _require(
        value.get("payload_member_count") == len(normalized),
        "manifest payload member count is invalid",
    )
    _require(
        value.get("payload_file_count")
        == sum(entry["type"] == "file" for entry in normalized),
        "manifest payload file count is invalid",
    )
    payload_tree = _sha256(_canonical_json(normalized))
    _require(
        value.get("payload_tree_sha256") == payload_tree,
        "manifest payload tree digest is invalid",
    )
    _hash_claim(
        value.get("source_tree_sha256"),
        label="manifest source tree sha256",
    )
    return value, {entry["path"]: entry for entry in normalized}


def _validate_receipt(
    value: dict,
    *,
    archive_path: Path,
    archive_sha256: str,
    archive_size: int,
    manifest: dict,
    manifest_body: bytes,
    member_count: int,
) -> dict:
    _require(set(value) == _RECEIPT_KEYS, "receipt fields are invalid")
    _require(value.get("format") == RECEIPT_FORMAT, "receipt format is unsupported")
    _require(value.get("self_verified") is True, "receipt is not self-verified")
    _require(
        value.get("archive_filename") == archive_path.name
        and PurePosixPath(value["archive_filename"]).name
        == value["archive_filename"],
        "receipt archive filename does not match archive",
    )
    _require(
        value.get("archive_sha256") == archive_sha256,
        "receipt archive sha256 does not match archive",
    )
    _require(
        value.get("archive_size") == archive_size,
        "receipt archive size does not match archive",
    )
    _require(
        value.get("manifest_sha256") == _sha256(manifest_body),
        "receipt manifest sha256 does not match manifest",
    )
    _require(
        value.get("member_count") == member_count,
        "receipt archive member count is invalid",
    )
    for key in (
        "created_at",
        "payload_tree_sha256",
        "release_id",
        "schema_source",
        "schema_version",
        "source_date_epoch",
        "source_tree_sha256",
    ):
        _require(value.get(key) == manifest.get(key), f"receipt {key} disagrees")
    _release_schema_claim(value.get("release_id"))
    _hash_claim(value.get("source_tree_sha256"), label="receipt source tree sha256")
    return value


def _tar_member_metadata(
    member: tarfile.TarInfo,
    *,
    expected_type: str,
    expected_mode: int,
    epoch: int,
) -> None:
    _require(
        member.uid == 0
        and member.gid == 0
        and member.uname == "root"
        and member.gname == "root"
        and int(member.mtime) == epoch
        and member.mode == expected_mode
        and not member.pax_headers
        and not member.linkname
        and member.devmajor == 0
        and member.devminor == 0,
        "release archive metadata verification failed",
    )
    if expected_type == "dir":
        _require(
            member.type == tarfile.DIRTYPE
            and member.isdir()
            and member.size == 0,
            "release archive directory metadata verification failed",
        )
    else:
        _require(
            member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
            and member.isfile(),
            "release archive file metadata verification failed",
        )


def _read_tar_body(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    maximum: int,
) -> bytes:
    _require(member.size <= maximum, "release archive member exceeds size limit")
    stream = archive.extractfile(member)
    _require(stream is not None, "release archive file could not be read")
    body = stream.read(maximum + 1)
    _require(
        len(body) == member.size and len(body) <= maximum,
        "release archive file size verification failed",
    )
    return body


def _verify_open_archive(
    file_object,
    *,
    archive_path: Path,
    archive_sha256: str,
    archive_size: int,
    receipt: dict,
) -> tuple[dict, dict[str, dict], bytes]:
    try:
        file_object.seek(0)
        with tarfile.open(fileobj=file_object, mode="r:gz") as archive:
            root_member = archive.next()
            manifest_member = archive.next()
            _require(
                root_member is not None and manifest_member is not None,
                "release archive member count is invalid",
            )
            _require(
                root_member.name == "."
                and manifest_member.name == f"./{MANIFEST_NAME}",
                "release archive order is invalid",
            )
            _tar_member_metadata(
                root_member,
                expected_type="dir",
                expected_mode=0o755,
                epoch=_positive_int(
                    receipt.get("source_date_epoch"),
                    label="receipt source date epoch",
                    allow_zero=True,
                ),
            )
            _tar_member_metadata(
                manifest_member,
                expected_type="file",
                expected_mode=0o644,
                epoch=receipt["source_date_epoch"],
            )
            manifest_body = _read_tar_body(
                archive,
                manifest_member,
                maximum=MAX_MANIFEST_BYTES,
            )
            manifest, entries = _validate_manifest(
                _decode_canonical_json(
                    manifest_body,
                    label="embedded release manifest",
                )
            )
            epoch = manifest["source_date_epoch"]
            for path, entry in entries.items():
                member = archive.next()
                _require(
                    member is not None,
                    "release archive is missing manifest members",
                )
                _require(
                    member.name == f"./{path}",
                    "release archive member set or order does not match manifest",
                )
                mode = int(entry["mode"], 8)
                _tar_member_metadata(
                    member,
                    expected_type=entry["type"],
                    expected_mode=mode,
                    epoch=epoch,
                )
                if entry["type"] == "file":
                    _require(
                        member.size == entry["size"],
                        "release archive content size does not match manifest",
                    )
                    body = _read_tar_body(
                        archive,
                        member,
                        maximum=MAX_RELEASE_FILE_BYTES,
                    )
                    _require(
                        _sha256(body) == entry["sha256"],
                        "release archive content sha256 does not match manifest",
                    )
            extra = archive.next()
            _require(
                extra is None,
                (
                    "release archive has too many members"
                    if len(entries) + 2 >= MAX_MEMBERS
                    else "release archive contains an unexpected member"
                ),
            )
            _validate_receipt(
                receipt,
                archive_path=archive_path,
                archive_sha256=archive_sha256,
                archive_size=archive_size,
                manifest=manifest,
                manifest_body=manifest_body,
                member_count=len(entries) + 2,
            )
            return manifest, entries, manifest_body
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ReleaseVerifyError("release archive could not be verified") from exc


def _open_archive_descriptor(path: Path) -> tuple[int, os.stat_result]:
    absolute = path.absolute()
    try:
        before = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise ReleaseVerifyError("release archive is missing") from exc
    _require(
        stat.S_ISREG(before.st_mode)
        and not stat.S_ISLNK(before.st_mode)
        and before.st_nlink == 1
        and before.st_size <= MAX_ARCHIVE_BYTES,
        "release archive must be one bounded regular file",
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise ReleaseVerifyError("release archive could not be opened safely") from exc
    actual = os.fstat(descriptor)
    try:
        _require(
            actual.st_dev == before.st_dev
            and actual.st_ino == before.st_ino
            and actual.st_nlink == 1
            and stat.S_ISREG(actual.st_mode)
            and actual.st_size <= MAX_ARCHIVE_BYTES,
            "release archive changed during open",
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, actual


def _hash_descriptor(descriptor: int, maximum: int) -> tuple[str, int]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, 65_536)
        if not chunk:
            break
        total += len(chunk)
        _require(total <= maximum, "release archive exceeds size limit")
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest(), total


def _write_all(descriptor: int, body: bytes) -> None:
    offset = 0
    while offset < len(body):
        written = os.write(descriptor, body[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _write_new_file(path: Path, body: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _new_directory_target(path: Path, *, label: str) -> tuple[Path, Path]:
    absolute = path.absolute()
    _require(
        not absolute.exists() and not absolute.is_symlink(),
        f"{label} already exists",
    )
    parent = absolute.parent.resolve(strict=True)
    metadata = os.lstat(parent)
    _require(stat.S_ISDIR(metadata.st_mode), f"{label} parent is not a directory")
    return absolute, parent


def _extract_verified_archive(
    descriptor: int,
    *,
    target: Path,
    target_parent: Path,
    manifest: dict,
    entries: dict[str, dict],
    manifest_body: bytes,
) -> None:
    staging = target_parent / f".{target.name}.verify-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            with tarfile.open(fileobj=stream, mode="r:gz") as archive:
                root_member = archive.next()
                manifest_member = archive.next()
                _require(
                    root_member is not None
                    and root_member.name == "."
                    and manifest_member is not None
                    and manifest_member.name == f"./{MANIFEST_NAME}",
                    "release archive changed before extraction",
                )
                for relative, entry in entries.items():
                    member = archive.next()
                    _require(
                        member is not None
                        and member.name == f"./{relative}",
                        "release archive changed before extraction",
                    )
                    destination = staging.joinpath(
                        *PurePosixPath(relative).parts
                    )
                    if entry["type"] == "dir":
                        destination.mkdir(
                            parents=True,
                            exist_ok=False,
                            mode=int(entry["mode"], 8),
                        )
                        destination.chmod(int(entry["mode"], 8))
                        continue
                    destination.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                        mode=0o755,
                    )
                    body = _read_tar_body(
                        archive,
                        member,
                        maximum=MAX_RELEASE_FILE_BYTES,
                    )
                    _require(
                        len(body) == entry["size"]
                        and _sha256(body) == entry["sha256"],
                        "release archive changed before extraction",
                    )
                    _write_new_file(
                        destination,
                        body,
                        int(entry["mode"], 8),
                    )
                _require(
                    archive.next() is None,
                    "release archive changed before extraction",
                )
        _write_new_file(
            staging / MANIFEST_NAME,
            manifest_body,
            0o644,
        )
        staging.chmod(0o755)
        _verify_materialized_payload(
            staging,
            manifest=manifest,
            entries=entries,
            allow_runtime=False,
            expected_data_target=None,
        )
        os.replace(staging, target)
        _fsync_directory(target_parent)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _file_digest(path: Path, *, maximum: int) -> tuple[str, int]:
    body = _read_regular(path, label="materialized release file", maximum=maximum)
    return _sha256(body), len(body)


def _verify_materialized_payload(
    root: Path,
    *,
    manifest: dict,
    entries: dict[str, dict],
    allow_runtime: bool,
    expected_data_target: Path | None,
) -> None:
    root_metadata = os.lstat(root)
    _require(
        stat.S_ISDIR(root_metadata.st_mode)
        and not stat.S_ISLNK(root_metadata.st_mode)
        and stat.S_IMODE(root_metadata.st_mode) == 0o755,
        "materialized release root metadata is invalid",
    )
    expected = {MANIFEST_NAME, *entries}
    seen: set[str] = set()
    runtime_seen: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            path = Path(child.path)
            relative = PurePosixPath(
                *path.relative_to(root).parts
            ).as_posix()
            metadata = child.stat(follow_symlinks=False)
            if allow_runtime and relative in ("data", "venv"):
                runtime_seen.add(relative)
                if relative == "data":
                    _require(
                        stat.S_ISLNK(metadata.st_mode),
                        "materialized data must be the exact state symlink",
                    )
                    try:
                        resolved = path.resolve(strict=True)
                    except OSError as exc:
                        raise ReleaseVerifyError(
                            "materialized data symlink cannot be resolved"
                        ) from exc
                    _require(
                        expected_data_target is not None
                        and resolved == expected_data_target,
                        "materialized data symlink target is incorrect",
                    )
                else:
                    _require(
                        stat.S_ISDIR(metadata.st_mode)
                        and not stat.S_ISLNK(metadata.st_mode),
                        "materialized venv must be a real directory",
                    )
                continue
            _require(
                relative in expected,
                f"materialized release contains unexpected entry: {relative}",
            )
            seen.add(relative)
            if relative == MANIFEST_NAME:
                _require(
                    stat.S_ISREG(metadata.st_mode)
                    and not stat.S_ISLNK(metadata.st_mode)
                    and stat.S_IMODE(metadata.st_mode) == 0o644,
                    "materialized manifest metadata is invalid",
                )
                body = _read_regular(
                    path,
                    label="materialized manifest",
                    maximum=MAX_MANIFEST_BYTES,
                )
                _require(
                    body == _canonical_json(manifest),
                    "materialized manifest content is invalid",
                )
                continue
            entry = entries[relative]
            expected_mode = int(entry["mode"], 8)
            _require(
                stat.S_IMODE(metadata.st_mode) == expected_mode,
                "materialized release metadata is invalid",
            )
            if entry["type"] == "dir":
                _require(
                    stat.S_ISDIR(metadata.st_mode)
                    and not stat.S_ISLNK(metadata.st_mode),
                    "materialized release directory is invalid",
                )
                pending.append(path)
            else:
                _require(
                    stat.S_ISREG(metadata.st_mode)
                    and not stat.S_ISLNK(metadata.st_mode)
                    and metadata.st_nlink == 1,
                    "materialized release file is invalid",
                )
                digest, size = _file_digest(
                    path,
                    maximum=MAX_RELEASE_FILE_BYTES,
                )
                _require(
                    size == entry["size"] and digest == entry["sha256"],
                    "materialized release content does not match manifest",
                )
    _require(seen == expected, "materialized release is missing manifest entries")
    if allow_runtime:
        _require(
            runtime_seen == {"data", "venv"},
            "materialized release must contain exact data and venv entries",
        )


def _write_attestation(path: Path, report: dict) -> None:
    absolute, parent, _parent_metadata = _proof_parent(path)
    _require(
        not absolute.exists() and not absolute.is_symlink(),
        "attestation target already exists",
    )
    body = _canonical_json(report)
    _write_new_file(absolute, body, 0o600)
    _fsync_directory(parent)
    _require(
        _read_owner_proof(
            absolute,
            label="written attestation",
        ) == body,
        "written attestation verification failed",
    )


def _proof_parent(path: Path) -> tuple[Path, Path, os.stat_result]:
    supplied = path.absolute()
    parent = supplied.parent.resolve(strict=True)
    metadata = os.lstat(parent)
    _require(
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid in {0, os.geteuid()}
        and metadata.st_gid in {0, os.getegid()}
        and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH),
        "attestation parent must be owner-controlled and not group/other writable",
    )
    return parent / supplied.name, parent, metadata


def _read_owner_proof(path: Path, *, label: str) -> bytes:
    absolute, _parent, parent_metadata = _proof_parent(path)
    try:
        before = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise ReleaseVerifyError(f"{label} is missing") from exc
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise ReleaseVerifyError(f"{label} could not be opened safely") from exc
    try:
        actual = os.fstat(descriptor)
        _require(
            actual.st_dev == before.st_dev
            and actual.st_ino == before.st_ino
            and stat.S_ISREG(actual.st_mode)
            and not stat.S_ISLNK(actual.st_mode)
            and actual.st_nlink == 1
            and stat.S_IMODE(actual.st_mode) == 0o600
            and actual.st_uid == parent_metadata.st_uid
            and actual.st_gid == parent_metadata.st_gid
            and actual.st_size <= MAX_RECEIPT_BYTES,
            f"{label} ownership or metadata is invalid",
        )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65_536, MAX_RECEIPT_BYTES + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            _require(total <= MAX_RECEIPT_BYTES, f"{label} exceeds size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        _require(
            after.st_dev == actual.st_dev
            and after.st_ino == actual.st_ino
            and after.st_nlink == actual.st_nlink
            and after.st_uid == actual.st_uid
            and after.st_gid == actual.st_gid
            and after.st_mode == actual.st_mode
            and after.st_size == actual.st_size
            and after.st_mtime_ns == actual.st_mtime_ns
            and after.st_ctime_ns == actual.st_ctime_ns,
            f"{label} changed while being read",
        )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_artifact_attestation(path: Path) -> tuple[dict, bytes]:
    body = _read_owner_proof(
        path,
        label="release artifact attestation",
    )
    decoded = _decode_canonical_json(
        body,
        label="release artifact attestation",
    )
    _require(
        set(decoded) == _ARTIFACT_ATTESTATION_KEYS,
        "release artifact attestation fields are invalid",
    )
    _require(
        decoded.get("format") == ARCHIVE_ATTESTATION_FORMAT
        and decoded.get("ok") is True,
        "release artifact attestation format is invalid",
    )
    _hash_claim(
        decoded.get("archive_sha256"),
        label="artifact archive sha256",
    )
    _hash_claim(
        decoded.get("manifest_sha256"),
        label="artifact manifest sha256",
    )
    _hash_claim(
        decoded.get("receipt_sha256"),
        label="artifact receipt sha256",
    )
    _hash_claim(
        decoded.get("payload_tree_sha256"),
        label="artifact payload tree sha256",
    )
    _hash_claim(
        decoded.get("source_tree_sha256"),
        label="artifact source tree sha256",
    )
    _positive_int(
        decoded.get("archive_size"),
        label="artifact archive size",
        allow_zero=True,
    )
    _positive_int(
        decoded.get("member_count"),
        label="artifact member count",
    )
    _positive_int(
        decoded.get("schema_version"),
        label="artifact schema version",
    )
    _release_schema_claim(decoded.get("release_id"))
    _require(
        isinstance(decoded.get("extract_dir"), str)
        and Path(decoded["extract_dir"]).is_absolute(),
        "release artifact extract directory is invalid",
    )
    try:
        datetime.fromisoformat(
            str(decoded.get("attested_at", "")).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ReleaseVerifyError(
            "release artifact attested_at is invalid"
        ) from exc
    return decoded, body


def _bind_artifact_to_manifest(
    artifact: dict,
    *,
    manifest: dict,
    manifest_body: bytes,
) -> None:
    expected = {
        "manifest_sha256": _sha256(manifest_body),
        "member_count": len(manifest["entries"]) + 2,
        "payload_tree_sha256": manifest["payload_tree_sha256"],
        "release_id": manifest["release_id"],
        "schema_version": manifest["schema_version"],
        "source_tree_sha256": manifest["source_tree_sha256"],
    }
    for key, value in expected.items():
        _require(
            artifact.get(key) == value,
            f"release artifact {key} does not match manifest",
        )


def _attested_at() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def verify_release(
    *,
    archive: str | os.PathLike[str],
    receipt: str | os.PathLike[str],
    extract_dir: str | os.PathLike[str],
    attestation: str | os.PathLike[str],
) -> dict:
    """Verify archive/receipt/manifest and safely extract to one new directory."""
    archive_path = Path(archive).absolute()
    receipt_path = Path(receipt).absolute()
    extract_path, extract_parent = _new_directory_target(
        Path(extract_dir),
        label="extract directory",
    )
    attestation_path = Path(attestation).absolute()
    _require(
        not attestation_path.exists() and not attestation_path.is_symlink(),
        "attestation target already exists",
    )
    receipt_body = _read_regular(
        receipt_path,
        label="release receipt",
        maximum=MAX_RECEIPT_BYTES,
    )
    decoded_receipt = _decode_canonical_json(
        receipt_body,
        label="release receipt",
    )
    descriptor, opened = _open_archive_descriptor(archive_path)
    extracted = False
    try:
        archive_sha256, archive_size = _hash_descriptor(
            descriptor,
            MAX_ARCHIVE_BYTES,
        )
        with os.fdopen(os.dup(descriptor), "rb") as archive_stream:
            manifest, entries, manifest_body = _verify_open_archive(
                archive_stream,
                archive_path=archive_path,
                archive_sha256=archive_sha256,
                archive_size=archive_size,
                receipt=decoded_receipt,
            )
        after = os.fstat(descriptor)
        _require(
            after.st_size == opened.st_size
            and after.st_mtime_ns == opened.st_mtime_ns
            and after.st_ctime_ns == opened.st_ctime_ns,
            "release archive changed during verification",
        )
        _extract_verified_archive(
            descriptor,
            target=extract_path,
            target_parent=extract_parent,
            manifest=manifest,
            entries=entries,
            manifest_body=manifest_body,
        )
        extracted = True
        report = {
            "archive_sha256": archive_sha256,
            "archive_size": archive_size,
            "attested_at": _attested_at(),
            "extract_dir": str(extract_path),
            "format": ARCHIVE_ATTESTATION_FORMAT,
            "manifest_sha256": _sha256(manifest_body),
            "member_count": len(entries) + 2,
            "ok": True,
            "payload_tree_sha256": manifest["payload_tree_sha256"],
            "receipt_sha256": _sha256(receipt_body),
            "release_id": manifest["release_id"],
            "schema_version": manifest["schema_version"],
            "source_tree_sha256": manifest["source_tree_sha256"],
        }
        _write_attestation(attestation_path, report)
        return report
    except Exception:
        if extracted:
            shutil.rmtree(extract_path, ignore_errors=True)
        try:
            attestation_path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)


def verify_release_artifact(
    *,
    archive: str | os.PathLike[str],
    receipt: str | os.PathLike[str],
    artifact_attestation: str | os.PathLike[str],
) -> dict:
    """Re-verify retained archive, receipt and artifact attestation in place."""
    archive_path = Path(archive).absolute()
    receipt_body = _read_regular(
        Path(receipt),
        label="release receipt",
        maximum=MAX_RECEIPT_BYTES,
    )
    decoded_receipt = _decode_canonical_json(
        receipt_body,
        label="release receipt",
    )
    descriptor, opened = _open_archive_descriptor(archive_path)
    try:
        archive_sha256, archive_size = _hash_descriptor(
            descriptor,
            MAX_ARCHIVE_BYTES,
        )
        with os.fdopen(os.dup(descriptor), "rb") as archive_stream:
            manifest, _entries, manifest_body = _verify_open_archive(
                archive_stream,
                archive_path=archive_path,
                archive_sha256=archive_sha256,
                archive_size=archive_size,
                receipt=decoded_receipt,
            )
        after = os.fstat(descriptor)
        _require(
            after.st_size == opened.st_size
            and after.st_mtime_ns == opened.st_mtime_ns
            and after.st_ctime_ns == opened.st_ctime_ns,
            "release archive changed during verification",
        )
    finally:
        os.close(descriptor)
    artifact, _artifact_body = _read_artifact_attestation(
        Path(artifact_attestation)
    )
    _bind_artifact_to_manifest(
        artifact,
        manifest=manifest,
        manifest_body=manifest_body,
    )
    expected = {
        "archive_sha256": archive_sha256,
        "archive_size": archive_size,
        "receipt_sha256": _sha256(receipt_body),
    }
    for key, value in expected.items():
        _require(
            artifact.get(key) == value,
            f"release artifact {key} does not match archive or receipt",
        )
    return artifact


def _materialized_evidence(
    *,
    release_dir: str | os.PathLike[str],
    state_dir: str | os.PathLike[str],
    artifact_attestation: str | os.PathLike[str],
) -> tuple[Path, Path, dict, bytes, dict, bytes]:
    supplied = Path(release_dir).absolute()
    try:
        metadata = os.lstat(supplied)
    except FileNotFoundError as exc:
        raise ReleaseVerifyError("materialized release is missing") from exc
    _require(
        stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode),
        "materialized release must be a real directory",
    )
    root = supplied.resolve(strict=True)
    state = Path(state_dir).resolve(strict=True)
    _require(state.is_dir(), "expected data target must be a directory")
    manifest_path = root / MANIFEST_NAME
    manifest_body = _read_regular(
        manifest_path,
        label="materialized manifest",
        maximum=MAX_MANIFEST_BYTES,
    )
    manifest, entries = _validate_manifest(
        _decode_canonical_json(
            manifest_body,
            label="materialized manifest",
        )
    )
    artifact, artifact_body = _read_artifact_attestation(
        Path(artifact_attestation)
    )
    _bind_artifact_to_manifest(
        artifact,
        manifest=manifest,
        manifest_body=manifest_body,
    )
    _require(
        Path(artifact["extract_dir"]).resolve(strict=False) == root,
        "release artifact extract directory does not match materialized release",
    )
    _verify_materialized_payload(
        root,
        manifest=manifest,
        entries=entries,
        allow_runtime=True,
        expected_data_target=state,
    )
    return root, state, manifest, manifest_body, artifact, artifact_body


def verify_materialized_release(
    *,
    release_dir: str | os.PathLike[str],
    expected_data_target: str | os.PathLike[str],
    artifact_attestation: str | os.PathLike[str],
    attestation: str | os.PathLike[str],
) -> dict:
    """Verify a materialized release and create a new owner-only proof."""
    (
        root,
        state,
        manifest,
        manifest_body,
        artifact,
        artifact_body,
    ) = _materialized_evidence(
        release_dir=release_dir,
        state_dir=expected_data_target,
        artifact_attestation=artifact_attestation,
    )
    report = {
        "artifact_archive_sha256": artifact["archive_sha256"],
        "artifact_attestation_sha256": _sha256(artifact_body),
        "artifact_receipt_sha256": artifact["receipt_sha256"],
        "attested_at": _attested_at(),
        "data_target": str(state),
        "format": MATERIALIZED_ATTESTATION_FORMAT,
        "manifest_sha256": _sha256(manifest_body),
        "materialized_release": str(root),
        "ok": True,
        "payload_tree_sha256": manifest["payload_tree_sha256"],
        "release_id": manifest["release_id"],
        "schema_version": manifest["schema_version"],
        "source_tree_sha256": manifest["source_tree_sha256"],
    }
    _write_attestation(Path(attestation), report)
    return report


def _read_materialized_attestation(path: Path) -> tuple[dict, bytes]:
    body = _read_owner_proof(
        path,
        label="materialized release attestation",
    )
    decoded = _decode_canonical_json(
        body,
        label="materialized release attestation",
    )
    _require(
        set(decoded) == _MATERIALIZED_ATTESTATION_KEYS,
        "materialized release attestation fields are invalid",
    )
    _require(
        decoded.get("format") == MATERIALIZED_ATTESTATION_FORMAT
        and decoded.get("ok") is True,
        "materialized release attestation format is invalid",
    )
    for key in (
        "artifact_archive_sha256",
        "artifact_attestation_sha256",
        "artifact_receipt_sha256",
        "manifest_sha256",
        "payload_tree_sha256",
        "source_tree_sha256",
    ):
        _hash_claim(
            decoded.get(key),
            label=f"materialized {key}",
        )
    _positive_int(
        decoded.get("schema_version"),
        label="materialized schema version",
    )
    _release_schema_claim(decoded.get("release_id"))
    for key in ("data_target", "materialized_release"):
        _require(
            isinstance(decoded.get(key), str)
            and Path(decoded[key]).is_absolute(),
            f"materialized {key} is invalid",
        )
    try:
        datetime.fromisoformat(
            str(decoded.get("attested_at", "")).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ReleaseVerifyError(
            "materialized attested_at is invalid"
        ) from exc
    return decoded, body


def verify_materialized_attestation(
    *,
    release_dir: str | os.PathLike[str],
    state_dir: str | os.PathLike[str],
    artifact_attestation: str | os.PathLike[str],
    attestation: str | os.PathLike[str],
) -> dict:
    """Read-only crash/retry proof: recompute the live tree and bind both proofs."""
    (
        root,
        state,
        manifest,
        manifest_body,
        artifact,
        artifact_body,
    ) = _materialized_evidence(
        release_dir=release_dir,
        state_dir=state_dir,
        artifact_attestation=artifact_attestation,
    )
    proof, _proof_body = _read_materialized_attestation(Path(attestation))
    expected = {
        "artifact_archive_sha256": artifact["archive_sha256"],
        "artifact_attestation_sha256": _sha256(artifact_body),
        "artifact_receipt_sha256": artifact["receipt_sha256"],
        "data_target": str(state),
        "format": MATERIALIZED_ATTESTATION_FORMAT,
        "manifest_sha256": _sha256(manifest_body),
        "materialized_release": str(root),
        "ok": True,
        "payload_tree_sha256": manifest["payload_tree_sha256"],
        "release_id": manifest["release_id"],
        "schema_version": manifest["schema_version"],
        "source_tree_sha256": manifest["source_tree_sha256"],
    }
    for key, value in expected.items():
        _require(
            proof.get(key) == value,
            f"materialized attestation {key} does not match live release",
        )
    return proof


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and safely materialize a Paihuo release."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--archive")
    mode.add_argument("--materialized-release")
    parser.add_argument("--receipt")
    parser.add_argument("--extract-dir")
    parser.add_argument("--state")
    parser.add_argument("--artifact-attestation")
    parser.add_argument("--attestation")
    parser.add_argument("--materialized-attestation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.archive:
            if args.extract_dir:
                _require(
                    bool(args.receipt)
                    and bool(args.attestation)
                    and not args.artifact_attestation
                    and not args.materialized_attestation
                    and not args.state,
                    "archive extraction requires receipt, extract-dir and attestation",
                )
                report = verify_release(
                    archive=args.archive,
                    receipt=args.receipt,
                    extract_dir=args.extract_dir,
                    attestation=args.attestation,
                )
            else:
                _require(
                    bool(args.receipt)
                    and bool(args.artifact_attestation)
                    and not args.attestation
                    and not args.materialized_attestation
                    and not args.state,
                    "artifact verification requires receipt and artifact-attestation",
                )
                report = verify_release_artifact(
                    archive=args.archive,
                    receipt=args.receipt,
                    artifact_attestation=args.artifact_attestation,
                )
        else:
            common = (
                bool(args.materialized_release)
                and bool(args.state)
                and bool(args.artifact_attestation)
                and not args.receipt
                and not args.extract_dir
            )
            if args.materialized_attestation:
                _require(
                    common and not args.attestation,
                    "materialized proof verification requires only the existing proof",
                )
                report = verify_materialized_attestation(
                    release_dir=args.materialized_release,
                    state_dir=args.state,
                    artifact_attestation=args.artifact_attestation,
                    attestation=args.materialized_attestation,
                )
            else:
                _require(
                    common and bool(args.attestation),
                    "materialized proof creation requires attestation",
                )
                report = verify_materialized_release(
                    release_dir=args.materialized_release,
                    expected_data_target=args.state,
                    artifact_attestation=args.artifact_attestation,
                    attestation=args.attestation,
                )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ReleaseVerifyError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "status": "failed",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
