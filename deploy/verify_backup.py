#!/usr/bin/env python3
"""校验 SQLite 备份，并可在隔离路径完成一次真实恢复演练。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence


class VerificationError(RuntimeError):
    """备份无法被安全验证或恢复。"""


def _read_only_uri(path: Path) -> str:
    """生成不会创建 WAL/SHM 边车文件的 SQLite URI。"""
    return f"{path.resolve().as_uri()}?mode=ro&immutable=1"


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema_rows(connection: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    rows = connection.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') "
        "FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name, tbl_name"
    ).fetchall()
    return [(str(kind), str(name), str(table), str(sql)) for kind, name, table, sql in rows]


def _schema_digest(rows: list[tuple[str, str, str, str]]) -> str:
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    table_names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for table_name in table_names:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"
        ).fetchone()
        if row is None:
            raise VerificationError(f"cannot count table {table_name!r}")
        counts[table_name] = int(row[0])
    return counts


def _inspect_connection(
    connection: sqlite3.Connection,
    *,
    path: Path,
    include_hash: bool,
) -> dict[str, Any]:
    integrity_rows = [
        str(row[0])
        for row in connection.execute("PRAGMA integrity_check").fetchall()
    ]
    if integrity_rows != ["ok"]:
        detail = "; ".join(integrity_rows[:10]) or "no result"
        raise VerificationError(f"PRAGMA integrity_check failed: {detail}")

    schema = _schema_rows(connection)
    report: dict[str, Any] = {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "integrity_check": "ok",
        "schema_digest": _schema_digest(schema),
        "schema_objects": len(schema),
        "table_counts": _table_counts(connection),
        "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
        "freelist_count": int(connection.execute("PRAGMA freelist_count").fetchone()[0]),
        "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(connection.execute("PRAGMA application_id").fetchone()[0]),
    }
    if include_hash:
        report["sha256"] = _sha256(path)
    return report


def verify_database(database_path: str | os.PathLike[str]) -> dict[str, Any]:
    """只读校验一个独立 SQLite 文件，不在其旁边创建任何边车文件。"""
    supplied_path = Path(database_path)
    if supplied_path.is_symlink():
        raise VerificationError(f"backup path must not be a symlink: {supplied_path}")
    path = supplied_path.resolve()
    if not path.is_file():
        raise VerificationError(f"backup file does not exist: {path}")
    for sidecar_kind in ("WAL", "rollback journal"):
        suffix = "-wal" if sidecar_kind == "WAL" else "-journal"
        sidecar_path = Path(f"{path}{suffix}")
        if sidecar_path.exists() and sidecar_path.stat().st_size > 0:
            raise VerificationError(
                "backup is not standalone because a non-empty "
                f"{sidecar_kind} exists: {sidecar_path}"
            )
    try:
        with closing(
            sqlite3.connect(
                _read_only_uri(path),
                uri=True,
                timeout=30,
            )
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            return _inspect_connection(connection, path=path, include_hash=True)
    except VerificationError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise VerificationError(f"cannot verify {path}: {exc}") from exc


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


def _copy_with_sqlite_backup(source_path: Path, destination_path: Path) -> None:
    """用 SQLite 在线备份 API 复制数据库，绝不 raw copy。"""
    source_uri = _read_only_uri(source_path)
    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source:
            source.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(destination_path, timeout=30)) as destination:
                destination.execute("PRAGMA synchronous=FULL")
                source.backup(destination, pages=1024, sleep=0.05)
                destination.commit()
                # 恢复产物必须是单文件，不依赖临时目录里的 WAL/SHM。
                mode = str(
                    destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                ).lower()
                if mode != "delete":
                    raise VerificationError(
                        f"restored database journal mode is {mode!r}, expected 'delete'"
                    )
                destination.commit()
        os.chmod(destination_path, 0o600)
        _fsync_file(destination_path)
    except VerificationError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise VerificationError(f"restore copy failed: {exc}") from exc
    finally:
        for sidecar in (
            Path(f"{destination_path}-journal"),
            Path(f"{destination_path}-wal"),
            Path(f"{destination_path}-shm"),
        ):
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass


def _assert_equivalent(
    source_report: dict[str, Any],
    restored_report: dict[str, Any],
) -> None:
    comparisons = (
        ("schema_digest", "schema"),
        ("table_counts", "table row counts"),
        ("user_version", "user_version"),
        ("application_id", "application_id"),
    )
    for key, label in comparisons:
        if source_report[key] != restored_report[key]:
            raise VerificationError(
                f"restore drill mismatch in {label}: "
                f"source={source_report[key]!r}, restored={restored_report[key]!r}"
            )


def _publish_without_overwrite(staged_path: Path, target_path: Path) -> None:
    """用同文件系统硬链接原子发布，并保留 no-clobber 语义。"""
    try:
        os.link(staged_path, target_path)
    except FileExistsError as exc:
        raise VerificationError(f"restore target already exists: {target_path}") from exc
    except OSError as exc:
        raise VerificationError(f"cannot publish restore target {target_path}: {exc}") from exc
    staged_path.unlink()
    _fsync_directory(target_path.parent)


def _restore_to_persistent_target(
    source_path: Path,
    target_path: Path,
    source_report: dict[str, Any],
) -> dict[str, Any]:
    if target_path.exists() or target_path.is_symlink():
        raise VerificationError(f"restore target already exists: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.partial-",
        suffix=".db",
        dir=target_path.parent,
    )
    os.close(descriptor)
    staged_path = Path(staged_name)
    try:
        _copy_with_sqlite_backup(source_path, staged_path)
        restored_report = verify_database(staged_path)
        _assert_equivalent(source_report, restored_report)
        _publish_without_overwrite(staged_path, target_path)
        return {
            "ok": True,
            "temporary": False,
            "restored_path": str(target_path.resolve()),
            "source_table_counts": source_report["table_counts"],
            "restored_table_counts": restored_report["table_counts"],
            "restored_sha256": restored_report["sha256"],
        }
    finally:
        _remove_database_artifacts(staged_path)


def restore_drill(
    backup_path: str | os.PathLike[str],
    *,
    restore_dir: str | os.PathLike[str] | None = None,
    restore_to: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """把备份真实恢复到临时路径并复检；可选择保留一个已验证恢复文件。"""
    # 先验证源文件，损坏备份不会创建恢复目录或任何目标文件。
    source_report = verify_database(backup_path)
    source_path = Path(source_report["path"])
    return _restore_drill_verified(
        source_path,
        source_report,
        restore_dir=restore_dir,
        restore_to=restore_to,
    )


def _restore_drill_verified(
    source_path: Path,
    source_report: dict[str, Any],
    *,
    restore_dir: str | os.PathLike[str] | None = None,
    restore_to: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """恢复一个刚由 verify_database 验证过的文件，避免重复全盘扫描。"""

    if restore_to is not None:
        if restore_dir is not None:
            raise VerificationError("--restore-dir and --restore-to cannot be used together")
        return _restore_to_persistent_target(
            source_path,
            Path(restore_to),
            source_report,
        )

    parent = Path(restore_dir) if restore_dir is not None else None
    if parent is not None and not parent.is_dir():
        raise VerificationError(f"restore directory does not exist: {parent}")

    with tempfile.TemporaryDirectory(prefix="paihuo-restore-", dir=parent) as temp_name:
        restored_path = Path(temp_name) / "contentcrew-restored.db"
        _copy_with_sqlite_backup(source_path, restored_path)
        restored_report = verify_database(restored_path)
        _assert_equivalent(source_report, restored_report)
        result = {
            "ok": True,
            "temporary": True,
            "restored_path": None,
            "source_table_counts": source_report["table_counts"],
            "restored_table_counts": restored_report["table_counts"],
            "restored_sha256": restored_report["sha256"],
        }
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a SQLite backup and optionally perform a restore drill."
    )
    parser.add_argument("database", help="SQLite backup file to verify")
    parser.add_argument(
        "--restore-drill",
        action="store_true",
        help="restore into an automatically removed temporary directory and re-verify",
    )
    parser.add_argument(
        "--restore-dir",
        help="existing parent directory for the temporary restore drill",
    )
    parser.add_argument(
        "--restore-to",
        help="publish a verified standalone restore file at this new path",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        verification = verify_database(args.database)
        report: dict[str, Any] = {
            "verification": verification,
        }
        if args.restore_drill or args.restore_to:
            report["restore_drill"] = _restore_drill_verified(
                Path(verification["path"]),
                verification,
                restore_dir=args.restore_dir,
                restore_to=args.restore_to,
            )
        elif args.restore_dir:
            raise VerificationError("--restore-dir requires --restore-drill")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (VerificationError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
