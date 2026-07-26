#!/usr/bin/env python3
"""使用 SQLite 在线备份 API 创建一致、可恢复、原子发布的数据库备份。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from deploy.verify_backup import (
    VerificationError,
    _restore_drill_verified,
    verify_database,
)


UTC = timezone.utc
MANAGED_BACKUP_RE = re.compile(
    r"^db-(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:(?:T)(?P<time>\d{6})Z)?\.db$"
)
STALE_PARTIAL_RE = re.compile(
    r"^\.paihuo-backup\.partial-[A-Za-z0-9_-]+\.db"
    r"(?:-(?:journal|wal|shm))?$"
)


class BackupError(RuntimeError):
    """备份未能安全完成。"""


def _live_read_only_uri(path: Path) -> str:
    # 不能加 immutable：在线源库的已提交数据可能仍在 WAL 中。
    return f"{path.resolve().as_uri()}?mode=ro"


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_database_artifacts(path: Path) -> None:
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


@contextmanager
def _exclusive_backup_lock(backup_dir: Path) -> Iterator[None]:
    lock_path = backup_dir / ".backup.lock"
    descriptor = os.open(
        lock_path,
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BackupError(f"backup lock is not a private regular file: {lock_path}")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError(
                f"another backup is already running (lock: {lock_path})"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _copy_live_database(source_path: Path, staged_path: Path) -> None:
    """抓取 SQLite 一致快照，包含源库已提交但尚在 WAL 中的事务。"""
    try:
        with closing(
            sqlite3.connect(
                _live_read_only_uri(source_path),
                uri=True,
                timeout=30,
            )
        ) as source:
            source.execute("PRAGMA query_only=ON")
            source.execute("PRAGMA busy_timeout=30000")
            with closing(sqlite3.connect(staged_path, timeout=30)) as destination:
                destination.execute("PRAGMA synchronous=FULL")
                source.backup(destination, pages=1024, sleep=0.05)
                destination.commit()
                mode = str(
                    destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                ).lower()
                if mode != "delete":
                    raise BackupError(
                        f"backup journal mode is {mode!r}, expected 'delete'"
                    )
                destination.commit()
        os.chmod(staged_path, 0o600)
        _fsync_file(staged_path)
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"SQLite online backup failed: {exc}") from exc
    finally:
        for sidecar in (
            Path(f"{staged_path}-journal"),
            Path(f"{staged_path}-wal"),
            Path(f"{staged_path}-shm"),
        ):
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass


def _publish_without_overwrite(staged_path: Path, destination_path: Path) -> None:
    """原子发布完整文件，同时绝不覆盖已有备份。"""
    try:
        os.link(staged_path, destination_path)
    except FileExistsError as exc:
        raise BackupError(f"backup destination already exists: {destination_path}") from exc
    except OSError as exc:
        raise BackupError(f"cannot publish backup {destination_path}: {exc}") from exc
    staged_path.unlink()
    _fsync_directory(destination_path.parent)


def _managed_timestamp(path: Path) -> datetime | None:
    match = MANAGED_BACKUP_RE.fullmatch(path.name)
    if match is None:
        return None
    date_part = match.group("date")
    time_part = match.group("time") or "000000"
    try:
        return datetime.strptime(
            f"{date_part}T{time_part}Z",
            "%Y-%m-%dT%H%M%SZ",
        ).replace(tzinfo=UTC)
    except ValueError:
        return None


def _cleanup_stale_partials(backup_dir: Path) -> list[str]:
    """持有全局备份锁时，清理被 SIGKILL/断电遗留的精确命名临时文件。"""
    removed: list[str] = []
    for path in sorted(backup_dir.iterdir()):
        if STALE_PARTIAL_RE.fullmatch(path.name) is None:
            continue
        if path.is_dir() and not path.is_symlink():
            raise BackupError(f"stale backup partial is unexpectedly a directory: {path}")
        try:
            path.unlink()
        except OSError as exc:
            raise BackupError(f"cannot remove stale backup partial {path}: {exc}") from exc
        removed.append(str(path.absolute()))
    if removed:
        _fsync_directory(backup_dir)
    return removed


def _prune_old_backups(
    backup_dir: Path,
    *,
    now: datetime,
    keep_days: int,
    keep_minimum: int,
) -> list[str]:
    managed = [
        (timestamp, path)
        for path in backup_dir.iterdir()
        if path.is_file() and (timestamp := _managed_timestamp(path)) is not None
    ]
    managed.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    minimum_kept = {path for _, path in managed[:keep_minimum]}
    cutoff = now - timedelta(days=keep_days)
    candidates = [
        path
        for timestamp, path in managed
        if path not in minimum_kept and timestamp < cutoff
    ]

    pruned: list[str] = []
    for path in sorted(candidates):
        try:
            path.unlink()
            for sidecar in (
                Path(f"{path}-journal"),
                Path(f"{path}-wal"),
                Path(f"{path}-shm"),
            ):
                try:
                    sidecar.unlink()
                except FileNotFoundError:
                    pass
        except OSError as exc:
            raise BackupError(f"cannot prune old backup {path}: {exc}") from exc
        pruned.append(str(path.resolve()))
    if pruned:
        _fsync_directory(backup_dir)
    return pruned


def _normalise_now(now: datetime | None) -> datetime:
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        raise BackupError("now must be timezone-aware")
    return current.astimezone(UTC).replace(microsecond=0)


def backup_database(
    database_path: str | os.PathLike[str],
    backup_dir: str | os.PathLike[str],
    *,
    keep_days: int = 14,
    keep_minimum: int = 7,
    run_restore_drill: bool = False,
    restore_dir: str | os.PathLike[str] | None = None,
    output_path: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """创建、校验、可选恢复演练、原子发布，并在成功后执行保留策略。"""
    source_path = Path(database_path)
    destination_dir = Path(backup_dir)
    if not source_path.is_file():
        raise BackupError(f"source database does not exist: {source_path}")
    if keep_days < 0:
        raise BackupError("keep_days must be zero or greater")
    if keep_minimum < 1:
        raise BackupError("keep_minimum must be at least 1")

    current = _normalise_now(now)
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination_dir = destination_dir.resolve()
    if output_path is None:
        destination_path = destination_dir / current.strftime("db-%Y-%m-%dT%H%M%SZ.db")
    else:
        destination_path = Path(output_path).resolve()
        if destination_path.parent != destination_dir:
            raise BackupError("output_path must be inside backup_dir")

    if destination_path.exists() or destination_path.is_symlink():
        raise BackupError(f"backup destination already exists: {destination_path}")

    with _exclusive_backup_lock(destination_dir):
        removed_stale_partials = _cleanup_stale_partials(destination_dir)
        # 锁内再次检查，避免两个进程在锁等待/获取之间选择同一目标。
        if destination_path.exists() or destination_path.is_symlink():
            raise BackupError(f"backup destination already exists: {destination_path}")
        descriptor, staged_name = tempfile.mkstemp(
            prefix=".paihuo-backup.partial-",
            suffix=".db",
            dir=destination_dir,
        )
        os.close(descriptor)
        staged_path = Path(staged_name)
        published = False
        try:
            _copy_live_database(source_path, staged_path)
            verification = verify_database(staged_path)
            drill_report = (
                _restore_drill_verified(
                    staged_path,
                    verification,
                    restore_dir=restore_dir,
                )
                if run_restore_drill
                else None
            )
            _publish_without_overwrite(staged_path, destination_path)
            published = True
            pruned = _prune_old_backups(
                destination_dir,
                now=current,
                keep_days=keep_days,
                keep_minimum=keep_minimum,
            )
            return {
                "ok": True,
                "backup_path": str(destination_path),
                "created_at_utc": current.isoformat().replace("+00:00", "Z"),
                "size_bytes": verification["size_bytes"],
                "sha256": verification["sha256"],
                "integrity_check": verification["integrity_check"],
                "schema_digest": verification["schema_digest"],
                "schema_objects": verification["schema_objects"],
                "table_counts": verification["table_counts"],
                "restore_drill": drill_report,
                "removed_stale_partials": removed_stale_partials,
                "pruned_backups": pruned,
            }
        except BackupError:
            raise
        except VerificationError as exc:
            raise BackupError(f"backup verification failed: {exc}") from exc
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise BackupError(f"backup failed: {exc}") from exc
        finally:
            _remove_database_artifacts(staged_path)
            # 发布之后的保留策略错误不能撤销一个已经确认完整的新备份。
            if published:
                _fsync_directory(destination_dir)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create an atomic, verified SQLite online backup."
    )
    parser.add_argument(
        "--database",
        default=os.environ.get(
            "CONTENTCREW_DB_PATH", "/var/lib/paihuo/data/contentcrew.db"
        ),
        help="live SQLite database path",
    )
    parser.add_argument(
        "--backup-dir",
        default="/var/backups/paihuo",
        help="directory for managed backups",
    )
    parser.add_argument("--output", help="optional new destination path inside backup-dir")
    parser.add_argument("--keep-days", type=int, default=14)
    parser.add_argument("--keep-minimum", type=int, default=7)
    parser.add_argument(
        "--restore-drill",
        action="store_true",
        help="restore the staged backup in a temporary directory and re-verify it",
    )
    parser.add_argument(
        "--restore-dir",
        help="existing parent directory for the temporary restore drill",
    )
    parser.add_argument(
        "--success-attestation",
        help=(
            "root-owned 0600 path that will attest this exact backup path/SHA; "
            "the command fails if attestation cannot be published"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.restore_dir and not args.restore_drill:
        print("backup failed: --restore-dir requires --restore-drill", file=sys.stderr)
        return 2
    try:
        report = backup_database(
            args.database,
            args.backup_dir,
            keep_days=args.keep_days,
            keep_minimum=args.keep_minimum,
            run_restore_drill=args.restore_drill,
            restore_dir=args.restore_dir,
            output_path=args.output,
        )
        if args.success_attestation:
            try:
                from deploy.backup_health import record_backup_success

                record_backup_success(
                    backup_report=report,
                    backup_dir=args.backup_dir,
                    attestation_path=args.success_attestation,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise BackupError(
                    f"backup succeeded but exact success attestation failed: {exc}"
                ) from exc
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (BackupError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
