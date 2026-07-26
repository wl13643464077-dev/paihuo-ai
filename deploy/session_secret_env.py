#!/usr/bin/env python3
"""Safely ensure Paihuo's root-owned runtime secrets.

Only the session-signing secret may be rotated here.  The configuration
wrapping key protects ciphertext already stored in SQLite; changing it without
an authenticated decrypt-and-rewrap transaction would make that ciphertext
unrecoverable.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import uuid
import re

from app.session_secret import (
    CONFIG_KEY_ENV,
    SESSION_SECRET_ENV,
    validate_secret_token,
)


MAX_ENV_FILE_BYTES = 1_048_576
DEFAULT_SECRET_KEYS = (SESSION_SECRET_ENV, CONFIG_KEY_ENV)
_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class SecretEnvError(RuntimeError):
    pass


def _validated_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    keys = tuple(keys)
    if (
        not keys
        or len(set(keys)) != len(keys)
        or any(not _ENV_KEY.fullmatch(key) for key in keys)
    ):
        raise SecretEnvError("managed secret keys are invalid")
    return keys


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _validate_parent(path: Path, owner_uid: int, owner_gid: int) -> None:
    absolute = path.absolute()
    if Path(os.path.realpath(absolute)) != absolute:
        raise SecretEnvError("environment directory must not traverse symlinks")
    metadata = os.stat(absolute, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise SecretEnvError("environment parent is not a directory")
    if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
        raise SecretEnvError("environment directory has an unexpected owner")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise SecretEnvError("environment directory must not be group/world writable")


def _ensure_parent(path: Path, owner_uid: int, owner_gid: int) -> None:
    try:
        _validate_parent(path, owner_uid, owner_gid)
        return
    except FileNotFoundError:
        pass

    grandparent = path.parent
    _validate_parent(grandparent, owner_uid, owner_gid)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(grandparent, flags)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=descriptor)
            os.chown(
                path.name,
                owner_uid,
                owner_gid,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            os.chmod(
                path.name,
                0o700,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            _fsync_directory(descriptor)
        except FileExistsError:
            # A concurrent root invocation may have completed the same mkdir.
            pass
    finally:
        os.close(descriptor)
    _validate_parent(path, owner_uid, owner_gid)


def _target_metadata(
    directory_fd: int,
    name: str,
    *,
    owner_uid: int,
    owner_gid: int,
) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise SecretEnvError("environment file must be a regular file")
    if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
        raise SecretEnvError("environment file must have the configured root owner")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SecretEnvError("environment file mode must be 0600")
    if metadata.st_nlink != 1:
        raise SecretEnvError("environment file must not have hard links")
    return metadata


def _read_existing(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        actual = os.fstat(descriptor)
        if (
            actual.st_dev != expected.st_dev
            or actual.st_ino != expected.st_ino
            or not stat.S_ISREG(actual.st_mode)
        ):
            raise SecretEnvError("environment file changed during validation")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, MAX_ENV_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_ENV_FILE_BYTES:
                raise SecretEnvError("environment file exceeds safe size")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _updated_body(
    body: bytes,
    *,
    keys: tuple[str, ...],
    rotate_keys: frozenset[str],
) -> tuple[bytes, dict]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretEnvError("environment file must be UTF-8") from exc
    if "\x00" in text:
        raise SecretEnvError("environment file contains NUL")

    lines = text.splitlines(keepends=True)
    matches: dict[str, list[int]] = {key: [] for key in keys}
    existing_values: dict[str, str] = {}
    for index, line in enumerate(lines):
        candidate = line.rstrip("\r\n")
        key, marker, value = candidate.partition("=")
        normalized = key.strip()
        if marker and normalized in matches:
            matches[normalized].append(index)
            existing_values[normalized] = value
    duplicates = [key for key, indexes in matches.items() if len(indexes) > 1]
    if duplicates:
        raise SecretEnvError("duplicate managed secret entries")

    used_values: set[str] = set()
    preserved = 0
    for key in keys:
        if not matches[key] or key in rotate_keys:
            continue
        value = existing_values[key]
        try:
            validate_secret_token(value, variable=key)
        except ValueError as exc:
            raise SecretEnvError(str(exc)) from exc
        if value in used_values:
            raise SecretEnvError("managed secret values must be independent")
        used_values.add(value)
        preserved += 1

    generated: dict[str, str] = {}
    created = 0
    rotated = 0
    for key in keys:
        if matches[key] and key not in rotate_keys:
            continue
        candidate = secrets.token_urlsafe(48)
        while candidate in used_values:
            candidate = secrets.token_urlsafe(48)
        validate_secret_token(candidate, variable=key)
        used_values.add(candidate)
        generated[key] = candidate
        if matches[key]:
            rotated += 1
        else:
            created += 1

    if not generated:
        return body, {
            "status": "unchanged",
            "created": 0,
            "rotated": 0,
            "preserved": preserved,
        }

    for key, value in generated.items():
        replacement = f"{key}={value}\n"
        if matches[key]:
            lines[matches[key][0]] = replacement
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines.append("\n")
            lines.append(replacement)
    status_value = "rotated" if rotated else "created"
    return "".join(lines).encode("utf-8"), {
        "status": status_value,
        "created": created,
        "rotated": rotated,
        "preserved": preserved,
    }


def _atomic_replace(
    directory_fd: int,
    name: str,
    body: bytes,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    temporary = f".{name}.session-secret-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, owner_uid, owner_gid)
        view = memoryview(body)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        _fsync_directory(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _atomic_create(
    directory_fd: int,
    name: str,
    body: bytes,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    """Atomically create an immutable backup without replacing an existing one."""
    temporary = f".{name}.secret-backup-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    linked = False
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, owner_uid, owner_gid)
        view = memoryview(body)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1

        # link(2) fails with EEXIST and therefore gives this backup operation
        # no-clobber semantics that os.replace cannot provide.
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(temporary, dir_fd=directory_fd)
        linked = False
        _fsync_directory(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        if linked:
            _fsync_directory(directory_fd)


def _absolute_file_path(
    path: str | os.PathLike[str],
    *,
    label: str,
) -> Path:
    target = Path(path)
    if not target.is_absolute() or target.name in ("", ".", ".."):
        raise SecretEnvError(f"{label} path must be an absolute file path")
    return target


def _validate_managed_body(body: bytes, *, keys: tuple[str, ...]) -> dict:
    _, report = _updated_body(
        body,
        keys=keys,
        rotate_keys=frozenset(),
    )
    if report["status"] != "unchanged":
        raise SecretEnvError("environment file is missing managed secrets")
    return report


def _read_validated_secret_file(
    path: str | os.PathLike[str],
    *,
    keys: tuple[str, ...],
    owner_uid: int,
    owner_gid: int,
    label: str,
) -> tuple[bytes, dict]:
    target = _absolute_file_path(path, label=label)
    parent = target.parent
    _validate_parent(parent, owner_uid, owner_gid)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(parent, directory_flags)
    try:
        existing = _target_metadata(
            directory_fd,
            target.name,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        if existing is None:
            raise SecretEnvError(f"{label} file is missing")
        body = _read_existing(directory_fd, target.name, existing)
        report = _validate_managed_body(body, keys=keys)
        return body, report
    finally:
        os.close(directory_fd)


def ensure_secret_keys(
    path: str | os.PathLike[str],
    *,
    keys: tuple[str, ...] = DEFAULT_SECRET_KEYS,
    rotate_keys: frozenset[str] = frozenset(),
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> dict:
    """Ensure independent strong secret entries without returning their values."""
    keys = _validated_keys(keys)
    rotate_keys = frozenset(rotate_keys)
    if not rotate_keys.issubset(keys):
        raise SecretEnvError("rotate keys must be managed keys")
    if CONFIG_KEY_ENV in rotate_keys:
        raise SecretEnvError(
            "configuration wrapping-key rotation requires an atomic rewrap workflow"
        )
    target = Path(path)
    if not target.is_absolute() or target.name in ("", ".", ".."):
        raise SecretEnvError("environment file path must be an absolute file path")
    parent = target.parent
    _ensure_parent(parent, owner_uid, owner_gid)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(parent, directory_flags)
    try:
        existing = _target_metadata(
            directory_fd,
            target.name,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        body = (
            _read_existing(directory_fd, target.name, existing)
            if existing is not None else b""
        )
        updated, report = _updated_body(
            body,
            keys=keys,
            rotate_keys=rotate_keys,
        )
        if report["status"] != "unchanged":
            _atomic_replace(
                directory_fd,
                target.name,
                updated,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            final = _target_metadata(
                directory_fd,
                target.name,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            if final is None:
                raise SecretEnvError("environment file disappeared after update")
        return report
    finally:
        os.close(directory_fd)


def check_secret_keys(
    path: str | os.PathLike[str],
    *,
    keys: tuple[str, ...] = DEFAULT_SECRET_KEYS,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> dict:
    """Read-only validation used by root systemd ExecStartPre."""
    keys = _validated_keys(keys)
    _, report = _read_validated_secret_file(
        path,
        keys=keys,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        label="environment",
    )
    return report


def _read_backup_from_directory(
    directory_fd: int,
    name: str,
    *,
    keys: tuple[str, ...],
    owner_uid: int,
    owner_gid: int,
) -> bytes | None:
    metadata = _target_metadata(
        directory_fd,
        name,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    if metadata is None:
        return None
    body = _read_existing(directory_fd, name, metadata)
    _validate_managed_body(body, keys=keys)
    return body


def backup_secret_keys(
    source: str | os.PathLike[str],
    backup: str | os.PathLike[str],
    *,
    keys: tuple[str, ...] = DEFAULT_SECRET_KEYS,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> dict:
    """Create a no-clobber root-only copy of the validated runtime env file."""
    keys = _validated_keys(keys)
    source_target = _absolute_file_path(source, label="environment")
    backup_target = _absolute_file_path(backup, label="backup")
    if Path(os.path.realpath(source_target)) == Path(os.path.realpath(backup_target)):
        raise SecretEnvError("backup path must differ from the environment file")

    source_body, _ = _read_validated_secret_file(
        source_target,
        keys=keys,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        label="environment",
    )
    _ensure_parent(backup_target.parent, owner_uid, owner_gid)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(backup_target.parent, directory_flags)
    created = False
    try:
        backup_body = _read_backup_from_directory(
            directory_fd,
            backup_target.name,
            keys=keys,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        if backup_body is None:
            try:
                _atomic_create(
                    directory_fd,
                    backup_target.name,
                    source_body,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                )
                created = True
            except FileExistsError:
                # A concurrent invocation may have won the no-clobber create.
                pass
            backup_body = _read_backup_from_directory(
                directory_fd,
                backup_target.name,
                keys=keys,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            if backup_body is None:
                raise SecretEnvError("backup file disappeared after creation")

        current_source, _ = _read_validated_secret_file(
            source_target,
            keys=keys,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            label="environment",
        )
        if not hmac.compare_digest(source_body, current_source):
            if created:
                os.unlink(backup_target.name, dir_fd=directory_fd)
                _fsync_directory(directory_fd)
            raise SecretEnvError("environment file changed while creating backup")
        if not hmac.compare_digest(current_source, backup_body):
            raise SecretEnvError("existing backup does not match environment file")
        return {
            "status": "created" if created else "unchanged",
            "validated": len(keys),
        }
    finally:
        os.close(directory_fd)


def check_secret_backup(
    source: str | os.PathLike[str],
    backup: str | os.PathLike[str],
    *,
    keys: tuple[str, ...] = DEFAULT_SECRET_KEYS,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> dict:
    """Read-only validation that source and root-only backup remain identical."""
    keys = _validated_keys(keys)
    source_target = _absolute_file_path(source, label="environment")
    backup_target = _absolute_file_path(backup, label="backup")
    if Path(os.path.realpath(source_target)) == Path(os.path.realpath(backup_target)):
        raise SecretEnvError("backup path must differ from the environment file")
    source_body, _ = _read_validated_secret_file(
        source_target,
        keys=keys,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        label="environment",
    )
    backup_body, _ = _read_validated_secret_file(
        backup_target,
        keys=keys,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        label="backup",
    )
    current_source, _ = _read_validated_secret_file(
        source_target,
        keys=keys,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        label="environment",
    )
    if (
        not hmac.compare_digest(source_body, current_source)
        or not hmac.compare_digest(current_source, backup_body)
    ):
        raise SecretEnvError("backup does not match environment file")
    return {"status": "verified", "validated": len(keys)}


def ensure_session_secret(
    path: str | os.PathLike[str],
    *,
    rotate: bool = False,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> dict:
    """Ensure both platform keys and optionally rotate only session signing."""
    return ensure_secret_keys(
        path,
        rotate_keys=frozenset((SESSION_SECRET_ENV,)) if rotate else frozenset(),
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ensure Paihuo's root-owned runtime secrets, rotate only the "
            "session-signing secret, or maintain a no-clobber root-only backup."
        )
    )
    parser.add_argument("--path", default="/etc/paihuo/paihuo.env")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--rotate",
        action="store_true",
        help=(
            "rotate only CONTENTCREW_SESSION_SECRET; the configuration "
            "wrapping key is deliberately preserved"
        ),
    )
    mode.add_argument("--check", action="store_true")
    mode.add_argument(
        "--backup-to",
        metavar="PATH",
        help="create or idempotently verify a root-only backup at PATH",
    )
    mode.add_argument(
        "--check-backup",
        metavar="PATH",
        help="read-only verification of the root-only backup at PATH",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if os.geteuid() != 0:
        print(
            json.dumps(
                {"status": "failed", "error": "RootRequired"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    try:
        arguments = _parser().parse_args(argv)
        if arguments.check:
            result = check_secret_keys(arguments.path)
        elif arguments.backup_to:
            result = backup_secret_keys(arguments.path, arguments.backup_to)
        elif arguments.check_backup:
            result = check_secret_backup(arguments.path, arguments.check_backup)
        else:
            result = ensure_session_secret(
                arguments.path,
                rotate=arguments.rotate,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
