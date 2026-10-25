#!/usr/bin/env python3
"""Fail closed when Paihuo's backup chain is stale or lacks working space."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, Sequence

from deploy.backup_db import (
    MANAGED_BACKUP_RE,
    OFFSITE_STATUS_NAME,
    REMOTE_ENV,
    BackupError,
    list_asset_snapshots,
    parse_remote,
)
from deploy.verify_backup import VerificationError, verify_database


UTC = timezone.utc
MIN_FREE_BYTES = 512 * 1024 * 1024
DEFAULT_ATTESTATION = "/var/lib/paihuo-upgrade/latest-periodic-backup.json"
REQUIRED_CORE_TABLES = {"tenants", "users", "job"}


class BackupHealthError(RuntimeError):
    """The backup chain is not safe enough for another write or release."""


def _free_bytes(path: Path) -> int:
    stats = os.statvfs(path)
    return int(stats.f_bavail * stats.f_frsize)


def check_disk_capacity(
    database: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> dict[str, int]:
    database_path = Path(database)
    destination_path = Path(destination)
    if database_path.is_symlink() or not database_path.is_file():
        raise BackupHealthError(f"database is not a regular file: {database_path}")
    if destination_path.is_symlink() or not destination_path.is_dir():
        raise BackupHealthError(
            f"backup destination is not a real directory: {destination_path}"
        )
    size = database_path.stat().st_size
    # A final checkpoint, a verified restore and a rollback staging file may
    # coexist. Keep an additional fixed margin for WAL growth and metadata.
    required = max(MIN_FREE_BYTES, size * 4 + 256 * 1024 * 1024)
    free = _free_bytes(destination_path)
    if free < required:
        raise BackupHealthError(
            f"insufficient backup space: free={free}, required={required}"
        )
    return {"free_bytes": free, "required_free_bytes": required}


def _managed_backups(directory: Path) -> list[Path]:
    candidates: list[Path] = []
    for entry in directory.iterdir():
        if MANAGED_BACKUP_RE.fullmatch(entry.name) is None:
            continue
        metadata = entry.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise BackupHealthError(
                f"managed backup is not a regular file: {entry}"
            )
        candidates.append(entry)
    return candidates


def _secure_backup_directory(directory: Path) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise BackupHealthError(f"backup directory is unsafe: {directory}")
    metadata = directory.lstat()
    if (
        metadata.st_uid != _owner_uid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BackupHealthError(
            f"backup directory must be owner-controlled 0700: {directory}"
        )


def _require_core_schema(report: dict[str, Any]) -> None:
    tables = set((report.get("table_counts") or {}).keys())
    missing = sorted(REQUIRED_CORE_TABLES - tables)
    if missing:
        raise BackupHealthError(
            "backup is missing core tables: " + ",".join(missing)
        )


def _owner_uid() -> int:
    return 0 if os.geteuid() == 0 else os.geteuid()


def _secure_control_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BackupHealthError(f"attestation directory is unsafe: {path}")
    metadata = path.lstat()
    if (
        metadata.st_uid != _owner_uid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BackupHealthError(
            f"attestation directory must be owner-controlled 0700: {path}"
        )


def _atomic_attestation(path: Path, report: dict[str, Any]) -> None:
    _secure_control_directory(path.parent)
    if path.is_symlink():
        raise BackupHealthError(f"attestation must not be a symlink: {path}")
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{path.name}.partial-", dir=path.parent
    )
    staged = Path(staged_name)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != _owner_uid()
            or metadata.st_nlink != 1
        ):
            raise BackupHealthError("attestation staging file is unsafe")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(report, handle, ensure_ascii=False, sort_keys=True)
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
            metadata = os.fstat(verify_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != _owner_uid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise BackupHealthError("published attestation is unsafe")
        finally:
            os.close(verify_fd)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _read_attestation(path: Path) -> dict[str, Any]:
    _secure_control_directory(path.parent)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != _owner_uid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise BackupHealthError(f"backup attestation is unsafe: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            report = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BackupHealthError(f"cannot read backup attestation: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(report, dict) or report.get("status") != "succeeded":
        raise BackupHealthError("backup attestation does not record success")
    return report


def record_backup_success(
    *,
    backup_report: dict[str, Any],
    backup_dir: str | os.PathLike[str],
    attestation_path: str | os.PathLike[str] = DEFAULT_ATTESTATION,
    now: datetime | None = None,
    max_candidate_age_hours: float = 2,
) -> dict[str, Any]:
    directory = Path(backup_dir).resolve(strict=True)
    _secure_backup_directory(directory)
    supplied = Path(str(backup_report.get("backup_path") or ""))
    if supplied.is_symlink():
        raise BackupHealthError("exact backup report points to a symlink")
    latest = supplied.resolve(strict=True)
    if latest.parent != directory or MANAGED_BACKUP_RE.fullmatch(latest.name) is None:
        raise BackupHealthError("exact backup report is outside the managed directory")
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        raise BackupHealthError("now must be timezone-aware")
    current = current.astimezone(UTC)
    try:
        created = datetime.fromisoformat(
            str(backup_report["created_at_utc"]).replace("Z", "+00:00")
        ).astimezone(UTC)
    except (KeyError, ValueError) as exc:
        raise BackupHealthError("exact backup report has an invalid timestamp") from exc
    age = current - created
    if age < timedelta(minutes=-5) or age > timedelta(hours=max_candidate_age_hours):
        raise BackupHealthError("new backup timestamp is outside the attestation window")
    try:
        verification = verify_database(latest)
    except VerificationError as exc:
        raise BackupHealthError(f"new backup is not restorable: {exc}") from exc
    if not hmac.compare_digest(
        str(verification["sha256"]), str(backup_report.get("sha256") or "")
    ):
        raise BackupHealthError("exact backup report SHA-256 does not match")
    if verification["schema_digest"] != backup_report.get("schema_digest"):
        raise BackupHealthError("exact backup report schema digest does not match")
    if verification["table_counts"] != backup_report.get("table_counts"):
        raise BackupHealthError("exact backup report table counts do not match")
    _require_core_schema(verification)
    report = {
        "status": "succeeded",
        "recorded_at_utc": current.isoformat().replace("+00:00", "Z"),
        "backup_created_at_utc": created.isoformat().replace("+00:00", "Z"),
        "backup_path": str(latest),
        "sha256": verification["sha256"],
        "integrity_check": verification["integrity_check"],
        "schema_digest": verification["schema_digest"],
        "table_counts": verification["table_counts"],
    }
    _atomic_attestation(Path(attestation_path).absolute(), report)
    return report


OFFSITE_NOT_CONFIGURED = (
    "未配置异地备份(PAIHUO_BACKUP_REMOTE):本机备份挡不住整机损坏、误删或"
    "勒索加密,请按 BACKUP_RECOVERY.md 配置对象存储或另一台主机"
)


def _parse_utc(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def backup_warnings(
    *,
    backup_dir: str | os.PathLike[str],
    remote: str | None,
    now: datetime,
    offsite_max_age_hours: float = 48,
    assets_max_age_hours: float = 48,
    assets_dir: str | os.PathLike[str] | None = None,
) -> list[str]:
    """不致命、但运维必须知道的备份缺口:异地备份、素材快照是否跟上。"""
    warnings: list[str] = []
    directory = Path(backup_dir)
    try:
        parsed = parse_remote(remote)
    except BackupError:
        parsed = None
        warnings.append(f"{REMOTE_ENV} 格式不对,异地备份没有生效")
    else:
        if parsed is None:
            warnings.append(OFFSITE_NOT_CONFIGURED)
    if parsed is not None:
        status: dict[str, Any] = {}
        status_path = directory / OFFSITE_STATUS_NAME
        try:
            if status_path.is_file() and not status_path.is_symlink():
                loaded = json.loads(status_path.read_text(encoding="utf-8"))
                status = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            status = {}
        last_success = _parse_utc(status.get("last_success_at_utc"))
        if last_success is None:
            warnings.append("已配置异地备份,但还没有成功同步过")
        elif now - last_success > timedelta(hours=offsite_max_age_hours):
            warnings.append(
                f"异地备份已超过 {offsite_max_age_hours:g} 小时没有成功同步"
            )
        if status.get("status") == "failed":
            warnings.append("最近一次异地同步失败,详见 paihuo-backup-simple.service 日志")
    snapshot_root = Path(assets_dir) if assets_dir else directory / "assets"
    try:
        snapshots = list_asset_snapshots(snapshot_root)
    except BackupError:
        snapshots = []
        warnings.append("素材快照目录里有异常条目,请人工检查")
    if not snapshots:
        warnings.append("没有找到素材文件快照(data/assets、/srv/paihuo-pub 未备份)")
    elif now - snapshots[0][0] > timedelta(hours=assets_max_age_hours):
        warnings.append(
            f"最新素材快照已超过 {assets_max_age_hours:g} 小时"
        )
    return warnings


def check_backup_health(
    *,
    database: str | os.PathLike[str],
    backup_dir: str | os.PathLike[str],
    max_age_hours: float = 24,
    attestation_path: str | os.PathLike[str] = DEFAULT_ATTESTATION,
    now: datetime | None = None,
    disk_only: bool = False,
    remote: str | None = None,
) -> dict[str, Any]:
    if max_age_hours <= 0:
        raise BackupHealthError("max_age_hours must be positive")
    database_path = Path(database).resolve(strict=True)
    supplied_dir = Path(backup_dir)
    if supplied_dir.is_symlink():
        raise BackupHealthError(f"backup directory must not be a symlink: {supplied_dir}")
    directory = supplied_dir.resolve(strict=True)
    _secure_backup_directory(directory)
    disk = check_disk_capacity(database_path, directory)
    result: dict[str, Any] = {"ok": True, **disk}
    if disk_only:
        result["disk_only"] = True
        return result

    attestation = _read_attestation(Path(attestation_path).absolute())
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        raise BackupHealthError("now must be timezone-aware")
    current = current.astimezone(UTC)
    try:
        recorded = datetime.fromisoformat(
            str(attestation["recorded_at_utc"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        created = datetime.fromisoformat(
            str(attestation["backup_created_at_utc"]).replace("Z", "+00:00")
        ).astimezone(UTC)
    except (KeyError, ValueError) as exc:
        raise BackupHealthError("backup attestation has invalid timestamps") from exc
    age = current - recorded
    if age < timedelta(minutes=-5):
        raise BackupHealthError("backup attestation timestamp is in the future")
    if age > timedelta(hours=max_age_hours):
        raise BackupHealthError(
            f"latest successful backup is older than {max_age_hours:g} hours"
        )
    if current - created > timedelta(hours=max_age_hours):
        raise BackupHealthError(
            f"attested backup is older than {max_age_hours:g} hours"
        )
    supplied_backup = Path(str(attestation.get("backup_path") or ""))
    if supplied_backup.is_symlink():
        raise BackupHealthError("attested backup path is a symlink")
    latest = supplied_backup.resolve(strict=True)
    if latest.parent != directory or MANAGED_BACKUP_RE.fullmatch(latest.name) is None:
        raise BackupHealthError("attested backup is outside the managed directory")
    try:
        verification = verify_database(latest)
    except VerificationError as exc:
        raise BackupHealthError(f"latest backup is not restorable: {exc}") from exc
    expected_sha = str(attestation.get("sha256") or "")
    if not expected_sha or not hmac.compare_digest(
        expected_sha, str(verification["sha256"])
    ):
        raise BackupHealthError("attested backup SHA-256 does not match")
    if verification["schema_digest"] != attestation.get("schema_digest"):
        raise BackupHealthError("attested backup schema digest does not match")
    if verification["table_counts"] != attestation.get("table_counts"):
        raise BackupHealthError("attested backup table counts do not match")
    _require_core_schema(verification)
    result.update({
        "latest_backup": str(latest),
        "latest_backup_age_seconds": max(0, int(age.total_seconds())),
        "latest_backup_sha256": verification["sha256"],
        "integrity_check": verification["integrity_check"],
        "warnings": backup_warnings(
            backup_dir=directory,
            remote=os.environ.get(REMOTE_ENV, "") if remote is None else remote,
            now=current,
        ),
    })
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Require enough disk space and a verified backup newer than 24 hours."
    )
    parser.add_argument(
        "--database",
        default=os.environ.get(
            "CONTENTCREW_DB_PATH", "/var/lib/paihuo/data/contentcrew.db"
        ),
    )
    parser.add_argument(
        "--backup-dir", default="/var/backups/paihuo"
    )
    parser.add_argument("--max-age-hours", type=float, default=24)
    parser.add_argument("--attestation", default=DEFAULT_ATTESTATION)
    parser.add_argument("--disk-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = check_backup_health(
            database=args.database,
            backup_dir=args.backup_dir,
            max_age_hours=args.max_age_hours,
            attestation_path=args.attestation,
            disk_only=args.disk_only,
        )
        for warning in report.get("warnings") or []:
            print(f"WARNING: {warning}", file=sys.stderr)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (BackupHealthError, OSError, ValueError) as exc:
        print(f"backup health check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
