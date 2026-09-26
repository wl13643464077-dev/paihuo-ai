#!/usr/bin/env python3
"""使用 SQLite 在线备份 API 创建一致、可恢复、原子发布的数据库备份。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
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


# ---------------------------------------------------------------------------
# 素材文件快照(data/assets、/srv/paihuo-pub)
#
# 做法等同 rsync --link-dest:每个快照是一棵完整目录树,和上一个快照相比
# 大小与修改时间都没变的文件直接硬链接到上一个快照的同一 inode,只有新增/
# 改动的文件才真正复制。于是每个快照都能单独拿来恢复,但只占"变化量"的空间。
#
# 源目录归应用账号可写,而备份以 root 运行:全程用目录 fd + O_NOFOLLOW 逐级
# 打开,跳过符号链接和特殊文件,防止被换成指向系统文件的链接后越权读取。
# ---------------------------------------------------------------------------

ASSET_SNAPSHOT_RE = re.compile(
    r"^assets-(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{6})Z$"
)
ASSET_PARTIAL_RE = re.compile(r"^\.assets\.partial-[A-Za-z0-9_-]+$")
ASSET_LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ASSET_SOURCES_ENV = "PAIHUO_BACKUP_ASSET_SOURCES"
ASSET_MANIFEST = ".snapshot.json"
_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def parse_asset_sources(specs: Sequence[str]) -> dict[str, Path]:
    """解析 ``标签=绝对路径`` 列表(命令行多次给出,或环境变量逗号分隔)。"""
    sources: dict[str, Path] = {}
    for raw in specs:
        for item in str(raw).split(","):
            item = item.strip()
            if not item:
                continue
            label, sep, path = item.partition("=")
            label = label.strip()
            path = path.strip()
            if not sep or ASSET_LABEL_RE.fullmatch(label) is None:
                raise BackupError(f"invalid asset source label: {item!r}")
            if not path.startswith("/"):
                raise BackupError(f"asset source path must be absolute: {item!r}")
            if label in sources:
                raise BackupError(f"duplicate asset source label: {label}")
            sources[label] = Path(path)
    return sources


def _asset_snapshot_timestamp(name: str) -> datetime | None:
    match = ASSET_SNAPSHOT_RE.fullmatch(name)
    if match is None:
        return None
    try:
        return datetime.strptime(
            f"{match.group('date')}T{match.group('time')}Z", "%Y-%m-%dT%H%M%SZ"
        ).replace(tzinfo=UTC)
    except ValueError:
        return None


def list_asset_snapshots(snapshot_root: str | os.PathLike[str]) -> list[tuple[datetime, Path]]:
    """受管素材快照,按时间从新到旧。"""
    root = Path(snapshot_root)
    if not root.is_dir():
        return []
    snapshots = []
    for entry in root.iterdir():
        stamp = _asset_snapshot_timestamp(entry.name)
        if stamp is None:
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise BackupError(f"managed asset snapshot is not a directory: {entry}")
        snapshots.append((stamp, entry))
    snapshots.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return snapshots


def _same_content_hint(previous: os.stat_result, current: os.stat_result) -> bool:
    return (
        stat.S_ISREG(previous.st_mode)
        and previous.st_size == current.st_size
        and previous.st_mtime_ns == current.st_mtime_ns
    )


def _copy_file_from_fd(source_fd: int, target: Path, metadata: os.stat_result) -> int:
    out = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    copied = 0
    try:
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(out, view)
                view = view[written:]
                copied += written
        os.fsync(out)
    finally:
        os.close(out)
    # 记下复制前看到的 mtime:复制过程中源文件若又被改,下次快照会因为
    # mtime 不同而重新复制,不会把半新半旧的内容一直硬链接下去。
    os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    return copied


def _snapshot_tree(
    source_dir_fd: int,
    target_dir: Path,
    previous_dir: Path | None,
    stats: dict[str, int],
) -> None:
    stack: list[tuple[int, Path, Path | None]] = [
        (os.dup(source_dir_fd), target_dir, previous_dir)
    ]
    try:
        _drain_snapshot_stack(stack, stats)
    finally:
        # 出错中断时,把还没处理的目录 fd 全部关掉。
        for pending_fd, _, _ in stack:
            os.close(pending_fd)


def _drain_snapshot_stack(
    stack: list[tuple[int, Path, Path | None]],
    stats: dict[str, int],
) -> None:
    while stack:
        dir_fd, target, previous = stack.pop()
        try:
            with os.scandir(dir_fd) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                try:
                    metadata = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    stats["skipped_links"] += 1
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    try:
                        child_fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise BackupError(
                            f"cannot open asset directory {target / name}: {exc}"
                        ) from exc
                    child_target = target / name
                    os.mkdir(child_target, 0o700)
                    stats["dirs"] += 1
                    stack.append((
                        child_fd,
                        child_target,
                        previous / name if previous is not None else None,
                    ))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    stats["skipped_special"] += 1
                    continue
                destination = target / name
                if previous is not None:
                    try:
                        prior = os.stat(previous / name, follow_symlinks=False)
                    except OSError:
                        prior = None
                    if prior is not None and _same_content_hint(prior, metadata):
                        try:
                            os.link(previous / name, destination, follow_symlinks=False)
                            stats["files"] += 1
                            stats["linked"] += 1
                            stats["bytes_total"] += metadata.st_size
                            continue
                        except OSError:
                            pass  # 硬链接数到上限等:退回真实复制
                try:
                    file_fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
                except FileNotFoundError:
                    continue
                try:
                    opened = os.fstat(file_fd)
                    if not stat.S_ISREG(opened.st_mode):
                        stats["skipped_special"] += 1
                        continue
                    copied = _copy_file_from_fd(file_fd, destination, opened)
                finally:
                    os.close(file_fd)
                stats["files"] += 1
                stats["copied"] += 1
                stats["bytes_copied"] += copied
                stats["bytes_total"] += copied
        finally:
            os.close(dir_fd)


def snapshot_assets(
    sources: dict[str, Path],
    snapshot_root: str | os.PathLike[str],
    *,
    keep_days: int = 14,
    keep_minimum: int = 3,
    min_interval_hours: float = 23.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """为素材目录拍一个硬链接增量快照,并按保留策略清理旧快照。

    距上一个快照不足 ``min_interval_hours`` 时直接跳过(每小时的备份定时器
    只有大约每天一次真正拍素材快照)。保留:``keep_days`` 天内全部保留,
    且无论多旧至少保留最新 ``keep_minimum`` 个。
    """
    if keep_days < 0:
        raise BackupError("assets keep_days must be zero or greater")
    if keep_minimum < 1:
        raise BackupError("assets keep_minimum must be at least 1")
    current = _normalise_now(now)
    root = Path(snapshot_root)
    if root.is_symlink():
        raise BackupError(f"asset snapshot root must not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = root.resolve()
    with _exclusive_backup_lock(root):
        removed_partials: list[str] = []
        for entry in sorted(root.iterdir()):
            if ASSET_PARTIAL_RE.fullmatch(entry.name) and not entry.is_symlink():
                shutil.rmtree(entry)
                removed_partials.append(str(entry))
        existing = list_asset_snapshots(root)
        latest = existing[0] if existing else None
        if latest is not None and (
            current - latest[0] < timedelta(hours=min_interval_hours)
        ):
            return {
                "ok": True,
                "skipped": True,
                "reason": "recent snapshot exists",
                "latest_snapshot": str(latest[1]),
                "removed_stale_partials": removed_partials,
            }
        final = root / current.strftime("assets-%Y-%m-%dT%H%M%SZ")
        if final.exists() or final.is_symlink():
            raise BackupError(f"asset snapshot already exists: {final}")
        staging = Path(tempfile.mkdtemp(prefix=".assets.partial-", dir=root))
        published = False
        stats = {
            "files": 0, "linked": 0, "copied": 0, "dirs": 0,
            "bytes_copied": 0, "bytes_total": 0,
            "skipped_links": 0, "skipped_special": 0,
        }
        missing: list[str] = []
        try:
            os.chmod(staging, 0o700)
            for label, source in sorted(sources.items()):
                target = staging / label
                os.mkdir(target, 0o700)
                try:
                    source_fd = os.open(source, _DIR_FLAGS)
                except FileNotFoundError:
                    missing.append(label)
                    continue
                except OSError as exc:
                    raise BackupError(
                        f"cannot safely open asset source {label}={source}: {exc}"
                    ) from exc
                try:
                    _snapshot_tree(
                        source_fd,
                        target,
                        latest[1] / label if latest is not None else None,
                        stats,
                    )
                finally:
                    os.close(source_fd)
            manifest = {
                "created_at_utc": current.isoformat().replace("+00:00", "Z"),
                "sources": {label: str(path) for label, path in sorted(sources.items())},
                "missing_sources": missing,
                **stats,
            }
            manifest_path = staging / ASSET_MANIFEST
            with open(manifest_path, "x", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            os.chmod(manifest_path, 0o600)
            os.rename(staging, final)
            published = True
            _fsync_directory(root)
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(f"asset snapshot failed: {exc}") from exc
        finally:
            if not published:
                shutil.rmtree(staging, ignore_errors=True)

        # 保留策略:只在新快照发布成功后执行,只删受管命名的快照目录。
        snapshots = list_asset_snapshots(root)
        protected = {path for _, path in snapshots[:keep_minimum]}
        cutoff = current - timedelta(days=keep_days)
        pruned: list[str] = []
        for stamp, path in snapshots:
            if path in protected or stamp >= cutoff:
                continue
            try:
                shutil.rmtree(path)
            except OSError as exc:
                raise BackupError(f"cannot prune asset snapshot {path}: {exc}") from exc
            pruned.append(str(path))
        if pruned:
            _fsync_directory(root)
        return {
            "ok": True,
            "skipped": False,
            "snapshot": str(final),
            "missing_sources": missing,
            "removed_stale_partials": removed_partials,
            "pruned_snapshots": pruned,
            **stats,
        }


# ---------------------------------------------------------------------------
# 异地同步(可选)
#
# PAIHUO_BACKUP_REMOTE 支持两种写法:
#   rclone:<remote>:<路径>    例如 rclone:paihuo-oss:paihuo-backup/prod
#   rsync:<目标>              例如 rsync:backup@10.0.0.8:/srv/paihuo-offsite
# 每次数据库备份成功后:上传这次的数据库备份 + 对应 .sha256;素材快照有新的
# 才同步素材。只增不删(rclone copy / rsync 不带 --delete),本机备份被误删或
# 加密勒索也不会连带删掉异地副本;异地保留期用对象存储的生命周期规则控制。
# ---------------------------------------------------------------------------

REMOTE_ENV = "PAIHUO_BACKUP_REMOTE"
OFFSITE_STATUS_NAME = ".offsite-status.json"
OFFSITE_EXIT_CODE = 75   # 本地备份已成功、附加步骤失败:unit 按成功处理,另发告警
_UNSAFE_TARGET_RE = re.compile(r"[\s\x00-\x1f\x7f]")


def parse_remote(spec: str | None) -> tuple[str, str] | None:
    """解析异地目标;未配置返回 None,写法不对抛 BackupError。"""
    raw = (spec or "").strip()
    if not raw:
        return None
    kind, sep, target = raw.partition(":")
    if not sep or kind not in ("rclone", "rsync"):
        raise BackupError(
            f"{REMOTE_ENV} must start with 'rclone:' or 'rsync:'"
        )
    target = target.strip().rstrip("/")
    if (
        not target
        or target.startswith("-")
        or _UNSAFE_TARGET_RE.search(target)
        or (kind == "rclone" and ":" not in target)
    ):
        raise BackupError(f"{REMOTE_ENV} target is invalid")
    return kind, target


def _run_command(command: Sequence[str], timeout: float) -> None:
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackupError(f"offsite tool is not installed: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            f"offsite command timed out after {int(timeout)}s: {command[0]} {command[1]}"
        ) from exc
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or "").split())[-300:]
        raise BackupError(
            f"offsite command failed: {command[0]} {command[1]} "
            f"exit={completed.returncode}: {detail}"
        )


def _rsync_ssh_option() -> list[str]:
    key = (os.environ.get("PAIHUO_BACKUP_SSH_KEY") or "").strip()
    known = (os.environ.get("PAIHUO_BACKUP_SSH_KNOWN_HOSTS") or "").strip()
    if not key and not known:
        return []
    parts = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    for value in (key, known):
        if value and (not value.startswith("/") or _UNSAFE_TARGET_RE.search(value)):
            raise BackupError("PAIHUO_BACKUP_SSH_* must be absolute paths without spaces")
    if key:
        parts += ["-i", key]
    if known:
        parts += ["-o", f"UserKnownHostsFile={known}"]
    return ["-e", " ".join(parts)]


def _read_offsite_status(backup_dir: Path) -> dict[str, Any]:
    path = backup_dir / OFFSITE_STATUS_NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return {}
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return data if isinstance(data, dict) else {}


def _write_offsite_status(backup_dir: Path, status: dict[str, Any]) -> None:
    descriptor, staged = tempfile.mkstemp(prefix=".offsite-status.partial-", dir=backup_dir)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(status, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, backup_dir / OFFSITE_STATUS_NAME)
        _fsync_directory(backup_dir)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass


def offsite_sync(
    remote_spec: str,
    *,
    backup_dir: str | os.PathLike[str],
    db_backup_path: str | os.PathLike[str],
    db_sha256: str,
    asset_snapshot: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 600,
    runner=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把本次数据库备份(和新的素材快照)复制到异地;结果写入状态文件供巡检读取。"""
    runner = runner if runner is not None else _run_command
    current = _normalise_now(now)
    directory = Path(backup_dir).resolve()
    parsed = parse_remote(remote_spec)
    if parsed is None:
        raise BackupError(f"{REMOTE_ENV} is not configured")
    kind, target = parsed
    database = Path(db_backup_path)
    if not re.fullmatch(r"[0-9a-f]{64}", str(db_sha256 or "")):
        raise BackupError("offsite sync requires the verified backup SHA-256")
    previous = _read_offsite_status(directory)
    snapshot = Path(asset_snapshot) if asset_snapshot else None
    sync_assets = snapshot is not None and (
        previous.get("assets_snapshot") != snapshot.name
        or previous.get("status") != "succeeded"
    )
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))

    def run(command: list[str]) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackupError("offsite sync exceeded its time budget")
        runner(command, remaining)

    status: dict[str, Any] = {
        "remote_kind": kind,
        "attempted_at_utc": current.isoformat().replace("+00:00", "Z"),
        "db_backup": database.name,
        "db_sha256": db_sha256,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="paihuo-offsite-") as scratch:
            sidecar = Path(scratch) / f"{database.name}.sha256"
            sidecar.write_text(f"{db_sha256}  {database.name}\n", encoding="utf-8")
            if kind == "rclone":
                run(["rclone", "copyto", str(database), f"{target}/db/{database.name}"])
                run(["rclone", "copyto", str(sidecar), f"{target}/db/{sidecar.name}"])
                if sync_assets:
                    run(["rclone", "copy", f"{snapshot}/", f"{target}/assets/current"])
            else:
                ssh = _rsync_ssh_option()
                run(["rsync", "-t", *ssh, str(database), str(sidecar), f"{target}/db/"])
                if sync_assets:
                    run(["rsync", "-rt", *ssh, f"{snapshot}/", f"{target}/assets/current/"])
    except BackupError as exc:
        status.update({
            "status": "failed",
            "error": str(exc)[:400],
            "last_success_at_utc": previous.get("last_success_at_utc"),
            "assets_snapshot": previous.get("assets_snapshot"),
        })
        _write_offsite_status(directory, status)
        raise
    status.update({
        "status": "succeeded",
        "last_success_at_utc": status["attempted_at_utc"],
        "assets_snapshot": (
            snapshot.name if snapshot is not None else previous.get("assets_snapshot")
        ),
        "assets_synced": bool(sync_assets),
    })
    _write_offsite_status(directory, status)
    return status


def raise_alert(label: str, runner=None) -> bool:
    """借用现有 OnFailure 告警模板发企业微信通知(deploy/failure_alert.py)。

    本地数据库备份已成功时,素材/异地步骤失败不能让整个 unit 失败(升级流程
    会同步等待该 unit),所以这里主动拉起告警单元。
    """
    command = [
        "systemctl", "start", "--no-block",
        f"paihuo-failure-alert@{label}.service",
    ]
    try:
        if runner is not None:
            runner(command)
        else:
            subprocess.run(
                command, stdin=subprocess.DEVNULL, capture_output=True,
                timeout=30, check=True,
            )
        return True
    except (OSError, subprocess.SubprocessError, BackupError) as exc:
        print(f"cannot raise backup alert {label}: {type(exc).__name__}", file=sys.stderr)
        return False


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
        "--asset-source",
        action="append",
        default=None,
        metavar="LABEL=/ABS/PATH",
        help=(
            "asset directory to snapshot (repeatable); defaults to "
            f"${ASSET_SOURCES_ENV} (comma separated), none if unset"
        ),
    )
    parser.add_argument(
        "--assets-backup-dir",
        help="asset snapshot root (default: <backup-dir>/assets)",
    )
    parser.add_argument("--assets-keep-days", type=int, default=14)
    parser.add_argument("--assets-keep-minimum", type=int, default=3)
    parser.add_argument(
        "--assets-interval-hours",
        type=float,
        default=23.0,
        help="take a new asset snapshot only when the latest is older than this",
    )
    parser.add_argument(
        "--remote",
        default=os.environ.get(REMOTE_ENV, ""),
        help=f"offsite target (rclone:<remote>:<path> or rsync:<target>); default ${REMOTE_ENV}",
    )
    parser.add_argument("--remote-timeout-seconds", type=float, default=1200)
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
        if args.asset_source:
            parse_asset_sources(args.asset_source)   # 参数写错时尽早失败
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
    except (BackupError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    # 以下是数据库备份已成功之后的附加步骤:失败只告警 + 退出码 75
    # (unit 里 SuccessExitStatus=75),不能让升级流程等待的备份 unit 判失败。
    extra_failures = _run_extra_steps(args, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return OFFSITE_EXIT_CODE if extra_failures else 0


def _run_extra_steps(args: argparse.Namespace, report: dict[str, Any], *,
                     runner=None, alert=None) -> list[str]:
    runner = runner if runner is not None else _run_command
    alert = alert if alert is not None else raise_alert
    failures: list[str] = []
    try:
        specs = args.asset_source
        if specs is None:
            env_value = os.environ.get(ASSET_SOURCES_ENV, "")
            specs = [env_value] if env_value.strip() else []
        sources = parse_asset_sources(specs)
    except BackupError as exc:
        sources = {}
        failures.append("paihuo-backup-assets")
        print(f"asset backup failed: {exc}", file=sys.stderr)
    snapshot_root = Path(args.assets_backup_dir or Path(args.backup_dir) / "assets")
    if sources:
        try:
            report["assets"] = snapshot_assets(
                sources,
                snapshot_root,
                keep_days=args.assets_keep_days,
                keep_minimum=args.assets_keep_minimum,
                min_interval_hours=args.assets_interval_hours,
            )
        except (BackupError, OSError) as exc:
            failures.append("paihuo-backup-assets")
            report["assets"] = {"ok": False, "error": str(exc)[:400]}
            print(f"asset backup failed: {exc}", file=sys.stderr)
    try:
        remote = parse_remote(args.remote)
    except BackupError as exc:
        remote = None
        failures.append("paihuo-backup-offsite")
        print(f"offsite sync failed: {exc}", file=sys.stderr)
    if remote is not None:
        try:
            latest = list_asset_snapshots(snapshot_root) if sources else []
            report["offsite"] = offsite_sync(
                args.remote,
                backup_dir=args.backup_dir,
                db_backup_path=report["backup_path"],
                db_sha256=report["sha256"],
                asset_snapshot=latest[0][1] if latest else None,
                timeout_seconds=args.remote_timeout_seconds,
                runner=runner,
            )
        except (BackupError, OSError) as exc:
            failures.append("paihuo-backup-offsite")
            report["offsite"] = {"ok": False, "error": str(exc)[:400]}
            print(f"offsite sync failed: {exc}", file=sys.stderr)
    elif "paihuo-backup-offsite" not in failures:
        report["offsite"] = {"configured": False}
        print(
            f"WARNING: offsite backup is not configured ({REMOTE_ENV}); "
            "local backups cannot survive loss of this server",
            file=sys.stderr,
        )
    for label in dict.fromkeys(failures):
        alert(label)
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
