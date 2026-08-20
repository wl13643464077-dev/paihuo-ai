"""ContentCrew 数据层:SQLite,单文件,线程安全."""
import json
import hashlib
import hmac
import os
import re
import sqlite3
import stat
import threading
import time
import atexit
from contextlib import contextmanager

import fcntl

DB_PATH = os.environ.get(
    "CONTENTCREW_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "data", "contentcrew.db"),
)
class StaleWriteError(RuntimeError):
    """异步写池发现目标库已被切换;该快照写应被丢弃而非执行代际切换。"""


_init_lock = threading.RLock()
_thread = threading.local()
# ``_conn`` remains the process anchor for backward-compatible lifecycle checks
# (tests and maintenance commands explicitly close/reset it).  Request workers use
# their own thread-local connection so one long query/transaction no longer holds
# a Python lock around every other tenant.
_conn = None
_conn_path = None
_connection_generation = 0
_all_connections: set[sqlite3.Connection] = set()
# Serializes a DB_PATH generation switch with async-pool selection.  It is
# deliberately separate from ``_init_lock``: switching drains old workers
# before taking the schema lock, because a queued worker may still need that
# schema lock to finish.
_generation_lock = threading.RLock()
_generation_switching = threading.Event()
LATEST_SCHEMA_VERSION = 57
MIGRATION_LOCK_SUFFIX = ".migration.lock"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_profile(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  persona_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS job(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brief_json TEXT NOT NULL,
  profile_id INTEGER,
  mode TEXT NOT NULL DEFAULT 'copilot',      -- autopilot / copilot / manual
  status TEXT NOT NULL DEFAULT 'running',    -- running/awaiting_review/gate_blocked/failed/done/cancelled
  current_idx INTEGER NOT NULL DEFAULT 0,
  source_schedule_id INTEGER,
  source_schedule_occurrence TEXT,
  gate_json TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0,
  billing_status TEXT NOT NULL DEFAULT 'charged', -- pending/charged/refunded
  billing_points REAL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  deleted_at REAL, deleted_by INTEGER, delete_reason TEXT,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS station_run(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  station_idx INTEGER NOT NULL,
  skill_id TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  output_json TEXT,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued/running/awaiting_review/done/failed/stale/skipped/rejected
  review_comment TEXT,
  latency_ms INTEGER, tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_run_job ON station_run(job_id, station_idx, version);
CREATE TABLE IF NOT EXISTS asset(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,           -- topic / final / benchmark
  payload_json TEXT NOT NULL,
  job_id INTEGER,
  created_at REAL, updated_at REAL
);
"""


def _canonical_db_path(path) -> str:
    """Resolve every DB alias to the one on-disk migration lock identity."""
    raw = os.fspath(path)
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        raise RuntimeError("数据库路径必须是可固化的本地文件")
    return os.path.realpath(os.path.abspath(raw))


def _migration_lock_path(db_path) -> str:
    """Return the lock adjacent to the canonical database target."""
    return _canonical_db_path(db_path) + MIGRATION_LOCK_SUFFIX


def _verified_lock_stat(fd: int, lock_path: str):
    """Fail closed if the named lock is not our private, single-link inode."""
    opened = os.fstat(fd)
    try:
        named = os.stat(lock_path, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("unsafe migration lock: lock inode disappeared") from exc
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise RuntimeError("unsafe migration lock: lock inode was replaced")
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise RuntimeError("unsafe migration lock: regular single-link file required")
    if opened.st_uid != os.geteuid():
        raise RuntimeError("unsafe migration lock: unexpected file owner")
    if stat.S_IMODE(opened.st_mode) & 0o077:
        raise RuntimeError("unsafe migration lock: group/other permissions forbidden")
    return opened


@contextmanager
def _migration_process_lock(db_path):
    """Serialize schema discovery and migration across independent processes.

    The lock name derives from the canonical DB target, so a symlink alias cannot
    create a second migration lane. ``O_NOFOLLOW`` plus inode/owner/mode checks
    prevents a crafted symlink, hardlink or permissive file from being accepted
    as the coordination primitive.  ``flock`` is released by the kernel after a
    crash, allowing the next process to run SQLite recovery and retry migration.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("unsafe migration lock: O_NOFOLLOW is unavailable")
    lock_path = _migration_lock_path(db_path)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError("unsafe migration lock: cannot safely open lock file") from exc
    locked = False
    try:
        _verified_lock_stat(fd, lock_path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        except OSError as exc:
            raise RuntimeError("unsafe migration lock: exclusive lock failed") from exc
        # Re-check after a potentially long wait.  If the path was replaced while
        # waiting, locking the old inode must not authorize schema changes.
        _verified_lock_stat(fd, lock_path)
        yield
    finally:
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _execute_migration_script(c, script: str) -> None:
    """Execute a trusted schema script without sqlite3's implicit COMMIT.

    ``Connection.executescript`` commits any active transaction before running.
    Splitting only at complete SQLite statements keeps every migration DDL/DML
    inside the surrounding ``BEGIN IMMEDIATE`` transaction instead.
    """
    pending = []
    for character in script:
        pending.append(character)
        if character != ";":
            continue
        statement = "".join(pending)
        if sqlite3.complete_statement(statement):
            c.execute(statement)
            pending.clear()
    if "".join(pending).strip():
        raise RuntimeError("incomplete SQL statement in migration script")


def _column_exists(c, table: str, column: str) -> bool:
    if not table.replace("_", "").isalnum():
        raise ValueError("invalid table")
    return any(row["name"] == column for row in c.execute(f"PRAGMA table_info({table})"))


def _add_column(c, table: str, column: str, declaration: str):
    """只在列确实缺失时迁移；不再把锁库、磁盘满等真实错误静默吞掉。"""
    if not _column_exists(c, table, column):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _table_sql(c, table: str) -> str:
    if not table.replace("_", "").isalnum():
        raise ValueError("invalid table")
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return str(row["sql"] or "") if row else ""


def _inspection_import_status_contract(sql: str) -> str:
    return "".join(str(sql or "").lower().split())


def _upgrade_inspection_import_status_check(c) -> None:
    """Upgrade the exact two-state schema52 import ledger in one transaction.

    SQLite cannot ALTER a CHECK constraint.  The table is therefore rebuilt
    only when its normalized SQL is the known schema52 contract; unknown table
    shapes remain fail-closed in the post-migration validator.  Foreign keys
    are intentionally absent in this schema, while stable ids are copied
    verbatim so import rows and business values keep their authoritative links.
    """
    sql = _table_sql(c, "inspection_branch_import")
    normalized = _inspection_import_status_contract(sql)
    current = "check(statusin('previewed','committed','expired'))"
    legacy = "check(statusin('previewed','committed'))"
    if not sql or current in normalized or legacy not in normalized:
        return
    columns = [
        str(row["name"])
        for row in c.execute("PRAGMA table_info(inspection_branch_import)")
    ]
    required = {
        "id", "tenant_id", "industry_key", "request_key", "source_sha256",
        "filename", "status", "total_rows", "create_count", "update_count",
        "skip_count", "error_count", "created_by", "created_at", "updated_at",
    }
    if not required.issubset(columns):
        return
    allowed = {
        "id", "tenant_id", "industry_key", "request_key", "source_sha256",
        "filename", "catalog_version", "catalog_sha256", "business_values_json",
        "status", "total_rows", "create_count", "update_count", "skip_count",
        "error_count", "business_create_count", "business_update_count",
        "business_skip_count", "business_error_count", "created_by",
        "committed_by", "committed_at", "staging_purged_at", "created_at",
        "updated_at", "audit_archive_sha256", "audit_archive_bytes",
        "audit_archive_rows", "audit_archived_at", "audit_actions_json",
        "archive_sha256", "archive_size", "archive_row_count", "archived_at",
    }
    if set(columns) - allowed:
        return
    if _table_sql(c, "inspection_branch_import_schema53"):
        # The rebuild lives inside the same atomic migration transaction, so a
        # legitimate crash cannot leave this table committed.  Refuse an
        # unexpected pre-existing object instead of deleting an unknown table.
        raise RuntimeError("导入账本状态约束迁移发现未知临时表")
    c.execute("""
        CREATE TABLE inspection_branch_import_schema53(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          industry_key TEXT NOT NULL,
          request_key TEXT NOT NULL,
          source_sha256 TEXT NOT NULL,
          filename TEXT NOT NULL,
          catalog_version TEXT NOT NULL DEFAULT '',
          catalog_sha256 TEXT NOT NULL DEFAULT '',
          business_values_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'previewed'
            CHECK(status IN ('previewed','committed','expired')),
          total_rows INTEGER NOT NULL DEFAULT 0,
          create_count INTEGER NOT NULL DEFAULT 0,
          update_count INTEGER NOT NULL DEFAULT 0,
          skip_count INTEGER NOT NULL DEFAULT 0,
          error_count INTEGER NOT NULL DEFAULT 0,
          business_create_count INTEGER NOT NULL DEFAULT 0,
          business_update_count INTEGER NOT NULL DEFAULT 0,
          business_skip_count INTEGER NOT NULL DEFAULT 0,
          business_error_count INTEGER NOT NULL DEFAULT 0,
          created_by INTEGER NOT NULL,
          committed_by INTEGER,
          committed_at REAL,
          staging_purged_at REAL,
          audit_archive_sha256 TEXT,
          audit_archive_bytes INTEGER,
          audit_archive_rows INTEGER,
          audit_archived_at REAL,
          audit_actions_json TEXT,
          archive_sha256 TEXT,
          archive_size INTEGER,
          archive_row_count INTEGER,
          archived_at REAL,
          created_at REAL,
          updated_at REAL
        )
    """)
    target_columns = [
        str(row["name"])
        for row in c.execute("PRAGMA table_info(inspection_branch_import_schema53)")
    ]
    common = [column for column in target_columns if column in columns]
    quoted = ",".join(f'"{column}"' for column in common)
    c.execute(
        f"INSERT INTO inspection_branch_import_schema53({quoted}) "
        f"SELECT {quoted} FROM inspection_branch_import"
    )
    before = c.execute(
        "SELECT COUNT(*) AS n FROM inspection_branch_import"
    ).fetchone()["n"]
    after = c.execute(
        "SELECT COUNT(*) AS n FROM inspection_branch_import_schema53"
    ).fetchone()["n"]
    if int(before) != int(after):
        raise RuntimeError("导入账本状态约束迁移行数不一致")
    c.execute("DROP TABLE inspection_branch_import")
    c.execute(
        "ALTER TABLE inspection_branch_import_schema53 "
        "RENAME TO inspection_branch_import"
    )


def _v1_seed_directory() -> str:
    """Return the one authoritative V1 seed directory for this tree.

    If an immutable config path exists in any form it is authoritative and
    must be validated as-is; a damaged release path must never fall back to a
    mutable source directory.
    """
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    immutable = os.path.join(root, "config", "departments")
    source = os.path.join(root, "data", "departments")
    return immutable if os.path.lexists(immutable) else source


def _v2_seed_directory() -> str:
    """Return the authoritative, retained V2 decision-role archive."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    immutable = os.path.join(root, "config", "industry_decisions")
    source = os.path.join(root, "data", "industry_decisions")
    return immutable if os.path.lexists(immutable) else source


def _v3_seed_directory() -> str:
    """Return the immutable V3 decision archive used by schema55."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    immutable = os.path.join(root, "config", "industry_decisions_v3")
    source = os.path.join(root, "data", "industry_decisions_v3")
    return immutable if os.path.lexists(immutable) else source


def _v4_seed_directory() -> str:
    """Return the immutable V4 current decision catalog used by schema55."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    immutable = os.path.join(root, "config", "industry_decisions_v4")
    source = os.path.join(root, "data", "industry_decisions_v4")
    return immutable if os.path.lexists(immutable) else source


_SCHEMA54_V1_DECISION_RANGES = {
    "tea_coffee": frozenset(range(1001, 1037)),
    "convenience": frozenset(range(1101, 1137)),
    "snack": frozenset(range(1201, 1237)),
    "grocery": frozenset(range(1301, 1337)),
    "pharmacy": frozenset(range(1401, 1437)),
    "hotel": frozenset(range(1501, 1537)),
    "auto": frozenset(range(1601, 1637)),
    "fitness": frozenset(range(1701, 1737)),
    "beauty": frozenset(range(1801, 1837)),
    "pet": frozenset(range(1901, 1937)),
}
_SCHEMA54_V1_RANGES = {
    **_SCHEMA54_V1_DECISION_RANGES,
    "restaurant": frozenset(range(101, 161)),
}
_SCHEMA54_V2_RANGES = {
    industry_key: frozenset(range(20000 + offset * 1000 + 1,
                                  20000 + offset * 1000 + 7))
    for offset, industry_key in enumerate(_SCHEMA54_V1_DECISION_RANGES)
}
_SCHEMA55_V3_RANGES = dict(_SCHEMA54_V1_DECISION_RANGES)
_SCHEMA55_V4_RANGES = dict(_SCHEMA54_V1_DECISION_RANGES)


def _read_schema54_seed_directory(directory: str, label: str) -> dict[str, dict]:
    """Read one immutable catalog directory without tolerating path aliases."""
    if not os.path.isdir(directory) or os.path.islink(directory):
        raise RuntimeError(f"schema54 {label} 目录缺失或不安全")
    filenames = sorted(
        filename for filename in os.listdir(directory)
        if filename.endswith(".json")
    )
    if not filenames:
        raise RuntimeError(f"schema54 {label} 目录中没有 JSON 种子")
    rows: dict[str, dict] = {}
    for filename in filenames:
        path = os.path.join(directory, filename)
        if not os.path.isfile(path) or os.path.islink(path):
            raise RuntimeError(f"schema54 {label} 文件不安全: {filename}")
        try:
            with open(path, encoding="utf-8") as handle:
                row = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"schema54 {label} JSON 解析失败: {filename}"
            ) from exc
        if not isinstance(row, dict):
            raise RuntimeError(f"schema54 {label} 结构无效: {filename}")
        key = str(row.get("key") or "").strip()
        if not key or key in rows:
            raise RuntimeError(f"schema54 {label} 部门 key 空或重复")
        if filename != f"{key}.json":
            raise RuntimeError(
                f"schema54 {label} 文件名与部门 key 不一致: {filename}"
            )
        rows[key] = row
    return rows


def _frozen_v1_employee_identities() -> dict[int, dict]:
    """Read immutable V1 industry identities without importing app runtime.

    Database initialization must stay below the departments/providers import
    graph.  The release's config directory is authoritative; source checkouts
    use data/departments only when no immutable config exists.
    """
    directory = _v1_seed_directory()
    identities: dict[int, dict] = {}
    departments = _read_schema54_seed_directory(directory, "V1 seed")
    if set(departments) != set(_SCHEMA54_V1_RANGES):
        missing = sorted(set(_SCHEMA54_V1_RANGES) - set(departments))
        extra = sorted(set(departments) - set(_SCHEMA54_V1_RANGES))
        raise RuntimeError(
            "schema54 V1 seed 必须精确覆盖十行业与餐饮档案; "
            f"missing={missing}, extra={extra}"
        )
    employee_keys: set[str] = set()
    for dept_key, department in departments.items():
        dept_key = str(department.get("key") or "").strip()
        raw_employees = department.get("employees")
        if not dept_key or not isinstance(raw_employees, list):
            raise RuntimeError(f"schema54 V1 seed 部门字段无效: {dept_key}")
        expected_ids = _SCHEMA54_V1_RANGES[dept_key]
        raw_ids = []
        for employee in raw_employees:
            if not isinstance(employee, dict):
                raise RuntimeError(f"schema54 V1 seed 员工结构无效: {dept_key}")
            try:
                if isinstance(employee.get("idx"), bool):
                    raise ValueError
                idx = int(employee.get("idx"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"schema54 V1 seed 员工 idx 无效: {dept_key}"
                ) from exc
            raw_ids.append(idx)
            if idx in identities:
                raise RuntimeError(f"schema54 V1 seed 员工 idx 重复: {idx}")
            employee_key = str(employee.get("key") or "").strip()
            employee_name = str(employee.get("name") or "").strip()
            if not employee_key or not employee_name:
                raise RuntimeError(
                    f"schema54 V1 seed 员工身份字段无效: {dept_key}/{idx}"
                )
            if employee_key in employee_keys:
                raise RuntimeError(
                    f"schema54 V1 seed 员工 key 重复: {employee_key}"
                )
            employee_keys.add(employee_key)
            frozen = {
                "idx": idx,
                "key": employee_key,
                "name": employee_name,
                "dept_key": dept_key,
                "catalog_version": "v1",
                "person_snapshot": str(employee.get("person") or "").strip(),
                "identity_scheme": "legacy-six",
            }
            spec = {**employee, "catalog_version": "v1"}
            payload = json.dumps(
                spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            frozen["spec_sha256"] = hashlib.sha256(payload).hexdigest()
            identities[idx] = frozen
        if len(raw_ids) != len(expected_ids) or set(raw_ids) != expected_ids:
            raise RuntimeError(
                f"schema54 V1 seed {dept_key} 必须精确覆盖"
                f" {len(expected_ids)} 名原员工号段"
            )
    decision_ids = set().union(*_SCHEMA54_V1_DECISION_RANGES.values())
    if len(decision_ids) != 360 or set(identities) & decision_ids != decision_ids:
        raise RuntimeError("schema54 V1 seed 必须精确保留 10×36=360 名历史员工")
    return identities


def _frozen_v2_employee_identities() -> dict[int, dict]:
    """Load and normalize the complete, immutable 10×6 V2 role archive."""
    decision_rows = _read_schema54_seed_directory(
        _v2_seed_directory(), "V2 历史 seed"
    )
    if set(decision_rows) != set(_SCHEMA54_V2_RANGES):
        missing = sorted(set(_SCHEMA54_V2_RANGES) - set(decision_rows))
        extra = sorted(set(decision_rows) - set(_SCHEMA54_V2_RANGES))
        raise RuntimeError(
            "schema54 V2 历史 seed 必须精确覆盖十行业; "
            f"missing={missing}, extra={extra}"
        )
    base_rows = _read_schema54_seed_directory(_v1_seed_directory(), "V1 seed")
    if not set(_SCHEMA54_V2_RANGES) <= set(base_rows):
        raise RuntimeError("schema54 V2 历史 seed 缺少受信 V1 行业基底")

    # Reuse the catalog's deterministic normalizer so the archived spec hash is
    # byte-for-byte identical to the hash schema53 persisted.  Importing this
    # module does not load the active V3 roster; only the two supplied immutable
    # JSON objects participate in normalization.
    from . import departments as department_catalog

    identities: dict[int, dict] = {}
    employee_keys: set[str] = set()
    for industry_key, expected_ids in _SCHEMA54_V2_RANGES.items():
        raw = decision_rows[industry_key]
        if str(raw.get("catalog_version") or "").strip() != "2026.08.v2":
            raise RuntimeError(
                f"schema54 V2 历史 seed 版本无效: {industry_key}"
            )
        raw_employees = raw.get("employees")
        if not isinstance(raw_employees, list):
            raise RuntimeError(
                f"schema54 V2 历史 seed 员工结构无效: {industry_key}"
            )
        try:
            raw_ids = [int(employee.get("idx")) for employee in raw_employees]
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"schema54 V2 历史 seed 员工 idx 无效: {industry_key}"
            ) from exc
        if len(raw_ids) != 6 or len(raw_ids) != len(set(raw_ids)) \
                or set(raw_ids) != expected_ids:
            raise RuntimeError(
                f"schema54 V2 历史 seed {industry_key} 必须精确覆盖 6 名员工"
            )
        try:
            normalized = department_catalog._normalize_decision_department(
                raw,
                base_rows[industry_key],
                catalog_version="2026.08.v2",
                roster_status="legacy",
            )
        except Exception as exc:
            raise RuntimeError(
                f"schema54 V2 历史 seed 归档校验失败: {industry_key}"
            ) from exc
        normalized_employees = normalized.get("employees") or []
        if len(normalized_employees) != 6:
            raise RuntimeError(
                f"schema54 V2 历史 seed 归档不完整: {industry_key}"
            )
        for employee in normalized_employees:
            idx = int(employee["idx"])
            employee_key = str(employee.get("key") or "").strip()
            employee_name = str(employee.get("name") or "").strip()
            spec_sha256 = str(employee.get("employee_spec_sha256") or "").strip()
            if (
                idx in identities or not employee_key or not employee_name
                or employee_key in employee_keys
                or not re.fullmatch(r"[0-9a-f]{64}", spec_sha256)
            ):
                raise RuntimeError(
                    f"schema54 V2 历史 seed 员工身份重复或无效: {idx}"
                )
            employee_keys.add(employee_key)
            identities[idx] = {
                "idx": idx,
                "key": employee_key,
                "name": employee_name,
                "dept_key": industry_key,
                "catalog_version": "2026.08.v2",
                "spec_sha256": spec_sha256,
                "person_snapshot": str(employee.get("person") or "").strip(),
                "identity_scheme": "legacy-six",
            }
    if len(identities) != 60 or set(identities) != set().union(
        *_SCHEMA54_V2_RANGES.values()
    ):
        raise RuntimeError("schema54 V2 历史 seed 必须精确保留 10×6=60 名员工")
    return identities


def _frozen_decision_employee_identities(
    directory: str,
    *,
    catalog_version: str,
    ranges: dict[str, frozenset],
    label: str,
    include_person: bool,
) -> list[dict]:
    """Load one complete decision catalog without collapsing reused idx values."""
    decision_rows = _read_schema54_seed_directory(directory, label)
    if set(decision_rows) != set(ranges):
        missing = sorted(set(ranges) - set(decision_rows))
        extra = sorted(set(decision_rows) - set(ranges))
        raise RuntimeError(
            f"schema55 {label} 必须精确覆盖十行业; missing={missing}, extra={extra}"
        )
    base_rows = _read_schema54_seed_directory(_v1_seed_directory(), "V1 seed")
    from . import departments as department_catalog

    identities: list[dict] = []
    keys: set[str] = set()
    for industry_key, expected_ids in ranges.items():
        raw = decision_rows[industry_key]
        if str(raw.get("catalog_version") or "").strip() != catalog_version:
            raise RuntimeError(f"schema55 {label} 版本无效: {industry_key}")
        raw_employees = raw.get("employees")
        if not isinstance(raw_employees, list):
            raise RuntimeError(f"schema55 {label} 员工结构无效: {industry_key}")
        try:
            raw_ids = [int(employee.get("idx")) for employee in raw_employees]
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"schema55 {label} 员工 idx 无效: {industry_key}") from exc
        if set(raw_ids) != set(expected_ids) or len(raw_ids) != len(expected_ids):
            raise RuntimeError(f"schema55 {label} {industry_key} 员工号段不完整")
        try:
            normalized = department_catalog._normalize_decision_department(
                raw,
                base_rows[industry_key],
                catalog_version=catalog_version,
                roster_status="legacy" if catalog_version != "2026.08.v4" else "active",
            )
        except Exception as exc:
            raise RuntimeError(f"schema55 {label} 归档校验失败: {industry_key}") from exc
        for employee in normalized.get("employees") or []:
            idx = int(employee["idx"])
            key = str(employee.get("key") or "").strip()
            name = str(employee.get("name") or "").strip()
            spec = str(employee.get("employee_spec_sha256") or "").strip()
            if (
                not key or not name or key in keys
                or not re.fullmatch(r"[0-9a-f]{64}", spec)
            ):
                raise RuntimeError(f"schema55 {label} 员工身份无效或重复: {idx}")
            keys.add(key)
            frozen = {
                "idx": idx, "key": key, "name": name,
                "dept_key": industry_key, "catalog_version": catalog_version,
                "spec_sha256": spec,
                "professional_profile": employee.get("professional_profile") or {},
                "decision_contract": employee.get("decision_contract") or {},
                "workflow": employee.get("decision_contract", {}).get("workflow", []),
                "outputs": employee.get("decision_contract", {}).get("outputs", []),
            }
            if include_person:
                person = str(
                    employee.get("person_snapshot", employee.get("person")) or ""
                ).strip()
                if not person:
                    raise RuntimeError(f"schema55 V4 员工缺少 person_snapshot: {idx}")
                frozen["person_snapshot"] = person
                frozen["identity_scheme"] = "v2-person"
            identities.append(frozen)
    expected_count = sum(len(values) for values in ranges.values())
    if len(identities) != expected_count:
        raise RuntimeError(f"schema55 {label} 员工数量不完整")
    return identities


def _frozen_v3_employee_identities() -> list[dict]:
    rows = _frozen_decision_employee_identities(
        _v3_seed_directory(), catalog_version="2026.08.v3",
        ranges=_SCHEMA55_V3_RANGES, label="V3 seed", include_person=True,
    )
    # V3 person is now stored for historical display, but it remains outside
    # the legacy six-field identity digest.
    for row in rows:
        row["identity_scheme"] = "legacy-six"
    return rows


def _frozen_v4_employee_identities() -> list[dict]:
    return _frozen_decision_employee_identities(
        _v4_seed_directory(), catalog_version="2026.08.v4",
        ranges=_SCHEMA55_V4_RANGES, label="V4 seed", include_person=True,
    )


def _frozen_core_employee_identities() -> dict[int, dict]:
    """Build the exact trusted core.v1 directory, never a legacy-unknown row."""
    from .skills import registry

    expected_ids = set(range(11))
    if set(registry.BY_IDX) != expected_ids:
        raise RuntimeError("schema54 核心员工历史目录不完整")
    identities = {}
    for idx in sorted(expected_ids):
        station = registry.BY_IDX[idx]
        spec = {
            field: station.get(field)
            for field in (
                "idx", "key", "name", "skill", "emoji", "dept", "color",
                "duty", "intro", "approval", "optional", "solo_only",
            )
            if station.get(field) is not None
        }
        payload = json.dumps(
            spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        identities[idx] = {
            "idx": idx,
            "key": str(station.get("key") or "").strip(),
            "name": str(station.get("name") or "").strip(),
            "dept_key": "content",
            "catalog_version": "core.v1",
            "spec_sha256": hashlib.sha256(payload).hexdigest(),
        }
        employee_identity_ref(identities[idx])
    return identities


def _schema54_trusted_identity_catalog() -> dict[int, dict]:
    """Return the complete schema53-era identity directory keyed by exact idx."""
    catalogs = (
        _frozen_core_employee_identities(),
        _frozen_v1_employee_identities(),
        _frozen_v2_employee_identities(),
    )
    merged: dict[int, dict] = {}
    for catalog in catalogs:
        for idx, frozen in catalog.items():
            if idx in merged:
                raise RuntimeError(f"schema54 受信员工历史目录 idx 冲突: {idx}")
            # Schema54's exact resolver is intentionally six-field.  Schema55
            # keeps person snapshots in its separate identity-ref registry;
            # leaking those optional keys here would make old snapshot equality
            # reject an otherwise byte-stable V1/V2/V3 task.
            merged[idx] = {
                field: frozen[field] for field in _EMPLOYEE_IDENTITY_FIELDS
            }
    if len(merged) != 491:
        raise RuntimeError("schema54 受信员工历史目录不完整")
    return merged


def _schema55_trusted_identity_catalog() -> dict[str, dict]:
    """Return every V4/V3/V2/V1 identity keyed by its immutable ref.

    Unlike the schema54 idx map, this registry intentionally retains multiple
    generations sharing one slot number.  It is used only by the schema55
    migration/validator and never changes the legacy six-field helper above.
    """
    rows: list[dict] = []
    rows.extend(_frozen_core_employee_identities().values())
    rows.extend(_frozen_v1_employee_identities().values())
    rows.extend(_frozen_v2_employee_identities().values())
    rows.extend(_frozen_v3_employee_identities())
    rows.extend(_frozen_v4_employee_identities())
    out: dict[str, dict] = {}
    for frozen in rows:
        ref = (
            employee_identity_ref_v4(frozen)
            if frozen.get("identity_scheme") == "v2-person"
            else employee_identity_ref(frozen)
        )
        if ref in out and out[ref] != frozen:
            raise RuntimeError("schema55 员工身份摘要冲突")
        out[ref] = frozen
    expected = 11 + 420 + 60 + 360 + 360
    if len(out) != expected:
        raise RuntimeError(
            f"schema55 受信员工身份目录不完整: expected={expected}, actual={len(out)}"
        )
    return out


_EMPLOYEE_IDENTITY_FIELDS = (
    "idx", "key", "catalog_version", "name", "dept_key", "spec_sha256",
)
_EMPLOYEE_CONFIG_MUTABLE_FIELDS = (
    "prompt_template", "skills_json", "learned_at", "settings_json",
    "caps_off_json", "model_text", "model_image", "professional_profile_json",
)


def employee_identity_ref(frozen: dict) -> str:
    """Return the full immutable employee-role identity digest.

    Keep this tiny helper independent from ``departments`` so migrations can
    freeze schema-53 rows before the runtime catalog import graph exists.
    ``app.employeeidentity.identity_ref`` deliberately uses the same canonical
    six-value payload.
    """
    if not isinstance(frozen, dict):
        raise ValueError("员工身份快照无效")
    try:
        idx = int(frozen.get("idx"))
    except (TypeError, ValueError) as exc:
        raise ValueError("员工身份编号无效") from exc
    values = [
        idx,
        str(frozen.get("key") or "").strip(),
        str(frozen.get("catalog_version") or "").strip(),
        str(frozen.get("name") or "").strip(),
        str(frozen.get("dept_key") or "").strip(),
        str(frozen.get("spec_sha256") or "").strip(),
    ]
    if any(not value for value in values[1:]):
        raise ValueError("员工身份快照不完整")
    payload = json.dumps(
        values, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def employee_identity_ref_v4(frozen: dict) -> str:
    """Hash a V4 identity including the human-facing person snapshot.

    ``employee_identity_ref`` above is intentionally frozen for V1--V3.  A
    separate helper makes the algorithm explicit and prevents a catalog refresh
    from silently reinterpreting historical six-field references.
    """
    if not isinstance(frozen, dict):
        raise ValueError("V4 员工身份快照无效")
    try:
        idx = int(frozen.get("idx"))
    except (TypeError, ValueError) as exc:
        raise ValueError("V4 员工身份编号无效") from exc
    values = [
        "v2-person",
        idx,
        str(frozen.get("key") or "").strip(),
        str(frozen.get("catalog_version") or "").strip(),
        str(frozen.get("name") or "").strip(),
        str(frozen.get("person_snapshot", frozen.get("person")) or "").strip(),
        str(frozen.get("dept_key") or "").strip(),
        str(frozen.get("spec_sha256") or "").strip(),
    ]
    if values[3] != "2026.08.v4" or any(not value for value in values[2:]):
        raise ValueError("V4 员工身份快照不完整")
    payload = json.dumps(
        values, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_config_json(value, expected_type, default) -> tuple[object, str]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = default
    if not isinstance(parsed, expected_type):
        parsed = default
    return parsed, json.dumps(
        parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def normalize_employee_config(raw=None) -> dict:
    """Canonicalize a legacy/current mutable role configuration."""
    row = dict(raw or {})
    skills, skills_json = _canonical_config_json(
        row.get("skills_json", row.get("skills", [])), list, []
    )
    settings, settings_json = _canonical_config_json(
        row.get("settings_json", row.get("settings", {})), dict, {}
    )
    caps_off, caps_off_json = _canonical_config_json(
        row.get("caps_off_json", row.get("caps_off", [])), list, []
    )
    professional_profile, professional_profile_json = _canonical_config_json(
        row.get(
            "professional_profile_json", row.get("professional_profile", {})
        ),
        dict,
        {},
    )
    learned_at = row.get("learned_at")
    if learned_at is not None:
        try:
            learned_at = float(learned_at)
        except (TypeError, ValueError):
            learned_at = None
    prompt_template = row.get("prompt_template")
    prompt_template = (
        str(prompt_template).strip() or None
        if prompt_template is not None else None
    )
    model_text = row.get("model_text")
    model_text = str(model_text).strip() or None if model_text is not None else None
    model_image = row.get("model_image")
    model_image = str(model_image).strip() or None if model_image is not None else None
    return {
        "prompt_template": prompt_template,
        "skills": skills,
        "skills_json": skills_json,
        "learned_at": learned_at,
        "settings": settings,
        "settings_json": settings_json,
        "caps_off": caps_off,
        "caps_off_json": caps_off_json,
        "model_text": model_text,
        "model_image": model_image,
        "professional_profile": professional_profile,
        "professional_profile_json": professional_profile_json,
    }


def employee_config_sha256(
    identity_ref: str, revision: int, config: dict,
) -> str:
    """Hash one exact role-configuration revision, not merely its row id."""
    identity_ref = str(identity_ref or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", identity_ref):
        raise ValueError("员工身份引用无效")
    revision = int(revision)
    if revision < 1:
        raise ValueError("配置修订号无效")
    clean = normalize_employee_config(config)
    payload = json.dumps(
        [
            identity_ref,
            revision,
            clean["prompt_template"],
            clean["skills"],
            clean["learned_at"],
            clean["settings"],
            clean["caps_off"],
            clean["model_text"],
            clean["model_image"],
            clean["professional_profile"],
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def employee_role_config_row_valid(raw) -> bool:
    """Recompute both immutable identity and canonical config integrity.

    Stored digests are evidence only after this recomputation.  JSON fields
    must also already be in canonical form so malformed JSON cannot normalize
    back to the same default and accidentally pass a semantic hash check.
    """
    try:
        row = dict(raw or {})
        frozen_identity = {
            "idx": row.get("idx"),
            "key": row.get("employee_key"),
            "catalog_version": row.get("employee_catalog_version"),
            "name": row.get("employee_name_snapshot"),
            "dept_key": row.get("employee_dept_key"),
            "spec_sha256": row.get("employee_spec_sha256"),
        }
        if str(row.get("identity_scheme") or "") == "v2-person":
            frozen_identity.update({
                "person_snapshot": row.get("person_snapshot"),
                "identity_scheme": row.get("identity_scheme"),
            })
            identity = employee_identity_ref_v4(frozen_identity)
        else:
            identity = employee_identity_ref(frozen_identity)
        stored_identity = str(row.get("identity_ref") or "").strip()
        if not hmac.compare_digest(identity, stored_identity):
            return False
        revision = int(row.get("config_revision"))
        clean = normalize_employee_config(row)
        for raw_field, clean_field in (
            ("skills_json", "skills_json"),
            ("settings_json", "settings_json"),
            ("caps_off_json", "caps_off_json"),
            ("professional_profile_json", "professional_profile_json"),
        ):
            if str(row.get(raw_field) or "") != clean[clean_field]:
                return False
        for field in ("prompt_template", "model_text", "model_image"):
            if row.get(field) != clean[field]:
                return False
        raw_learned = row.get("learned_at")
        if raw_learned is None:
            if clean["learned_at"] is not None:
                return False
        elif float(raw_learned) != clean["learned_at"]:
            return False
        expected_hash = employee_config_sha256(identity, revision, clean)
        stored_hash = str(row.get("config_sha256") or "").strip()
        return bool(
            re.fullmatch(r"[0-9a-f]{64}", stored_hash)
            and hmac.compare_digest(expected_hash, stored_hash)
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _require_schema53_catalog_identity(
    frozen: dict, identities: dict[int, dict], *, owner: str,
) -> dict:
    """Resolve all six frozen fields against the complete trusted directory."""
    if not isinstance(frozen, dict):
        try:
            frozen = dict(frozen)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"schema54 {owner} 员工身份快照无效") from exc
    task_shape = "emp_idx" in frozen
    raw_idx = frozen.get("emp_idx" if task_shape else "idx")
    if isinstance(raw_idx, bool):
        raise RuntimeError(f"schema54 {owner} 员工身份快照无效")
    field_names = (
        (
            "employee_key", "employee_catalog_version",
            "employee_name_snapshot", "employee_dept_key",
            "employee_spec_sha256",
        )
        if task_shape else
        ("key", "catalog_version", "name", "dept_key", "spec_sha256")
    )
    if any(
        not isinstance(frozen.get(field), str)
        or not frozen[field]
        or frozen[field] != frozen[field].strip()
        for field in field_names
    ):
        raise RuntimeError(f"schema54 {owner} 员工身份快照无效")
    try:
        normalized = _schema54_frozen_identity(frozen)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"schema54 {owner} 员工身份快照无效") from exc
    idx = int(normalized["idx"])
    expected = identities.get(idx)
    if expected is None or normalized != expected:
        version = normalized.get("catalog_version") or "unknown"
        catalog_label = "V1" if version == "v1" else (
            "V2" if version == "2026.08.v2" else str(version)
        )
        raise RuntimeError(
            f"schema54 {catalog_label} {owner} 员工身份无法通过"
            f"完整历史目录精确解析: idx={idx}"
        )
    return dict(expected)


def _backfill_employee_identity_snapshots(c) -> None:
    identities = _schema54_trusted_identity_catalog()

    def identity(idx) -> dict:
        try:
            employee_idx = int(idx)
        except (TypeError, ValueError):
            employee_idx = 0
        frozen = identities.get(employee_idx)
        if not frozen:
            raise RuntimeError(
                f"schema54 无法从完整历史目录解析员工: {employee_idx}"
            )
        return frozen

    task_employee_rows = list(c.execute(
        "SELECT DISTINCT emp_idx FROM task "
        "WHERE employee_key='' OR employee_catalog_version='' "
        "OR employee_name_snapshot='' OR employee_dept_key='' "
        "OR employee_spec_sha256=''"
    ))
    for row in task_employee_rows:
        frozen = identity(row["emp_idx"])
        c.execute(
            "UPDATE task SET employee_key=?,employee_catalog_version=?,"
            "employee_name_snapshot=?,employee_dept_key=?,employee_spec_sha256=? "
            "WHERE emp_idx=? AND (employee_key='' OR employee_catalog_version='' "
            "OR employee_name_snapshot='' OR employee_dept_key='' "
            "OR employee_spec_sha256='')",
            (
                frozen["key"], frozen["catalog_version"], frozen["name"],
                frozen["dept_key"], frozen["spec_sha256"], int(row["emp_idx"]),
            ),
        )
    c.execute(
        "UPDATE task_thread SET "
        "employee_key=COALESCE(NULLIF(employee_key,''),("
        "SELECT employee_key FROM task WHERE task.id=task_thread.root_task_id)),"
        "employee_catalog_version=COALESCE(NULLIF(employee_catalog_version,''),("
        "SELECT employee_catalog_version FROM task WHERE task.id=task_thread.root_task_id)) "
        "WHERE employee_key='' OR employee_catalog_version=''"
    )
    meeting_rows = list(c.execute(
        "SELECT id,emp_idxs_json FROM meeting WHERE member_snapshot_json='' "
        "OR member_snapshot_json='[]'"
    ))
    for row in meeting_rows:
        try:
            member_ids = json.loads(row["emp_idxs_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            member_ids = []
        snapshots = []
        for value in member_ids if isinstance(member_ids, list) else []:
            frozen = identity(value)
            snapshots.append({
                "idx": frozen["idx"], "key": frozen["key"],
                "name": frozen["name"], "dept_key": frozen["dept_key"],
                "catalog_version": frozen["catalog_version"],
                "spec_sha256": frozen["spec_sha256"],
            })
        c.execute(
            "UPDATE meeting SET member_snapshot_json=? WHERE id=?",
            (json.dumps(snapshots, ensure_ascii=False, separators=(",", ":")), row["id"]),
        )


def _schema54_frozen_identity(row: dict) -> dict:
    return {
        "idx": int(row.get("emp_idx", row.get("idx"))),
        "key": str(row.get("employee_key", row.get("key")) or "").strip(),
        "catalog_version": str(
            row.get("employee_catalog_version", row.get("catalog_version")) or ""
        ).strip(),
        "name": str(
            row.get("employee_name_snapshot", row.get("name")) or ""
        ).strip(),
        "dept_key": str(
            row.get("employee_dept_key", row.get("dept_key")) or ""
        ).strip(),
        "spec_sha256": str(
            row.get("employee_spec_sha256", row.get("spec_sha256")) or ""
        ).strip(),
    }


def _schema55_frozen_identity(row: dict, *, task_shape: bool = False) -> dict:
    """Normalize a V4 or legacy snapshot while preserving optional person data."""
    frozen = _schema54_frozen_identity(row)
    # Catalog loaders attach the immutable professional role payload alongside
    # identity fields.  Keep it available for baseline bundle/config creation;
    # task snapshots simply omit it when only six-field identity is persisted.
    for field in ("professional_profile", "decision_contract", "workflow", "outputs"):
        if field in row:
            frozen[field] = row.get(field)
    identity_scheme = row.get("identity_scheme")
    person_field = "person_snapshot"
    if task_shape and person_field not in row:
        person_field = "employee_person_snapshot"
    person = row.get(person_field)
    if str(identity_scheme or "").strip() == "v2-person" or (
        frozen["catalog_version"] == "2026.08.v4" and person is not None
    ):
        frozen["person_snapshot"] = str(person or "").strip()
        frozen["identity_scheme"] = str(identity_scheme or "v2-person").strip()
    elif person is not None:
        # V1--V3 keep their historical six-field identity digest, but Schema55
        # still freezes the human-facing name beside that digest.  Keeping this
        # display snapshot does not reinterpret or re-hash the legacy identity;
        # it only prevents an old task/meeting from borrowing today's V4 name.
        frozen["person_snapshot"] = str(person or "").strip()
        frozen["identity_scheme"] = "legacy-six"
    return frozen


def _schema55_identity_ref(frozen: dict) -> str:
    if str(frozen.get("identity_scheme") or "") == "v2-person":
        return employee_identity_ref_v4(frozen)
    return employee_identity_ref(frozen)


def _schema54_historical_catalog(frozen: dict) -> bool:
    idx = int(frozen["idx"])
    version = str(frozen.get("catalog_version") or "")
    return (
        (1001 <= idx <= 1936 and version == "v1")
        or (20000 <= idx <= 29999)
    )


def _schema54_insert_role_config(
    c, frozen: dict, raw_config=None, *, archived_at=None,
) -> tuple[str, int, str]:
    frozen = _schema54_frozen_identity(frozen)
    identity_ref = employee_identity_ref(frozen)
    revision = 1
    clean = normalize_employee_config(raw_config)
    config_sha256 = employee_config_sha256(identity_ref, revision, clean)
    now = time.time()
    c.execute(
        "INSERT OR IGNORE INTO employee_role_config("
        "identity_ref,idx,employee_key,employee_catalog_version,"
        "employee_name_snapshot,employee_dept_key,employee_spec_sha256,"
        "prompt_template,skills_json,learned_at,settings_json,caps_off_json,"
        "model_text,model_image,professional_profile_json,config_revision,"
        "config_sha256,archived_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity_ref, frozen["idx"], frozen["key"],
            frozen["catalog_version"], frozen["name"], frozen["dept_key"],
            frozen["spec_sha256"], clean["prompt_template"],
            clean["skills_json"], clean["learned_at"],
            clean["settings_json"], clean["caps_off_json"],
            clean["model_text"], clean["model_image"],
            clean["professional_profile_json"], revision,
            config_sha256, archived_at, now, now,
        ),
    )
    existing = c.execute(
        "SELECT * FROM employee_role_config "
        "WHERE identity_ref=?",
        (identity_ref,),
    ).fetchone()
    if not existing or any(
        str(existing[column]) != str(expected)
        for column, expected in (
            ("idx", frozen["idx"]),
            ("employee_key", frozen["key"]),
            ("employee_catalog_version", frozen["catalog_version"]),
            ("employee_name_snapshot", frozen["name"]),
            ("employee_dept_key", frozen["dept_key"]),
            ("employee_spec_sha256", frozen["spec_sha256"]),
        )
    ):
        raise RuntimeError("员工身份引用发生哈希冲突")
    if not employee_role_config_row_valid(existing):
        raise RuntimeError("员工岗位配置完整性校验失败")
    return identity_ref, revision, config_sha256


def _schema55_insert_role_config(
    c, frozen: dict, raw_config=None, *, archived_at=None,
    empty_config: bool = False,
) -> tuple[str, int, str]:
    """Insert one schema55 role config without inheriting another generation."""
    frozen = _schema55_frozen_identity(frozen)
    identity_ref = _schema55_identity_ref(frozen)
    revision = 1
    if empty_config:
        # New V4 configs deliberately do not inherit mutable prompt/skills/model
        # state, but they must carry the immutable catalog's professional role
        # profile so identity/config equality and the UI档案 remain valid.
        raw_config = {
            "professional_profile": frozen.get("professional_profile") or {},
        }
    clean = normalize_employee_config(raw_config)
    config_sha256 = employee_config_sha256(identity_ref, revision, clean)
    now = time.time()
    c.execute(
        "INSERT OR IGNORE INTO employee_role_config("
        "identity_ref,idx,employee_key,employee_catalog_version,"
        "employee_name_snapshot,employee_dept_key,employee_spec_sha256,"
        "person_snapshot,identity_scheme,prompt_template,skills_json,learned_at,"
        "settings_json,caps_off_json,model_text,model_image,professional_profile_json,"
        "config_revision,config_sha256,archived_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity_ref, frozen["idx"], frozen["key"], frozen["catalog_version"],
            frozen["name"], frozen["dept_key"], frozen["spec_sha256"],
            frozen.get("person_snapshot", ""), frozen.get("identity_scheme", "legacy-six"),
            clean["prompt_template"], clean["skills_json"], clean["learned_at"],
            clean["settings_json"], clean["caps_off_json"], clean["model_text"],
            clean["model_image"], clean["professional_profile_json"], revision,
            config_sha256, archived_at, now, now,
        ),
    )
    existing = c.execute(
        "SELECT * FROM employee_role_config WHERE identity_ref=?", (identity_ref,)
    ).fetchone()
    if not existing or not employee_role_config_row_valid(existing):
        raise RuntimeError("schema55 员工岗位配置完整性校验失败")
    return identity_ref, revision, config_sha256


def _backfill_schema54_employee_bindings(
    c, *, source_schema_version: int,
) -> None:
    """Freeze exact identity + role-config revisions for every persisted work item.

    Schema 53 keyed mutable role data by ``idx``.  Reusing the original
    1001-1936 person numbers for V3 is therefore safe only after the old mutable
    row is copied to the V1 identity and every existing task/meeting points at
    an immutable revision.  Unknown idx rows are rejected transactionally.
    """
    # A current schema54 database must never be silently repaired from old
    # catalogs. Its own immutable role/config rows are authoritative and the
    # post-migration validator below rejects any corruption.
    if int(source_schema_version) >= 54:
        return

    trusted_identities = _schema54_trusted_identity_catalog()
    legacy_configs = {
        int(row["idx"]): dict(row)
        for row in c.execute("SELECT * FROM employee_config")
    }
    unknown = sorted(
        idx for idx in legacy_configs if idx not in trusted_identities
    )
    if unknown:
        raise RuntimeError(
            "schema54 无法判定旧员工配置归属: "
            + ",".join(str(value) for value in unknown[:20])
        )

    now = time.time()
    # Preserve every known schema53 configuration even when no task has used it
    # yet. V1 decision roles and all V2 roles are historical; core and the
    # unchanged restaurant roster remain current until a later exact catalog
    # activation explicitly changes their slot.
    for idx, raw_config in legacy_configs.items():
        frozen = trusted_identities[idx]
        archived = now if _schema54_historical_catalog(frozen) else None
        active_ref, _revision, _config_sha256 = _schema54_insert_role_config(
            c, frozen, raw_config, archived_at=archived
        )

        # ``enabled`` belongs to the real person/slot.  V2's temporary 20xxx
        # identifiers are retained role identities, not extra employees.
        if idx <= 10 or 101 <= idx <= 160 or 1001 <= idx <= 1936:
            enabled = 0 if int(raw_config.get("enabled", 1) or 0) == 0 else 1
            if 1001 <= idx <= 1936:
                active_ref = None
            c.execute(
                "INSERT INTO employee_slot(idx,active_identity_ref,enabled,"
                "row_version,created_at,updated_at) VALUES(?,?,?,1,?,?) "
                "ON CONFLICT(idx) DO UPDATE SET enabled=excluded.enabled,"
                "updated_at=excluded.updated_at",
                (idx, active_ref, enabled, now, now),
            )

    # Every schema53 task must match all six fields in the complete trusted
    # directory. A valid-looking hash or a known numeric range is insufficient.
    task_rows = list(c.execute("SELECT * FROM task"))
    for raw_row in task_rows:
        row = dict(raw_row)
        frozen = _require_schema53_catalog_identity(
            row, trusted_identities, owner=f"task#{row['id']}"
        )
        archived = now if _schema54_historical_catalog(frozen) else None
        identity_ref, revision, config_sha256 = _schema54_insert_role_config(
            c,
            frozen,
            legacy_configs.get(int(frozen["idx"])),
            archived_at=archived,
        )
        existing_ref = str(row.get("employee_identity_ref") or "").strip()
        existing_hash = str(row.get("employee_config_sha256") or "").strip()
        try:
            existing_revision = int(row.get("employee_config_revision") or 0)
        except (TypeError, ValueError):
            existing_revision = 0
        present = (bool(existing_ref), existing_revision > 0, bool(existing_hash))
        if any(present) and (
            not all(present)
            or existing_ref != identity_ref
            or existing_revision != revision
            or existing_hash != config_sha256
        ):
            raise RuntimeError(
                f"schema54 task#{row['id']} 员工冻结配置与历史目录不一致"
            )
        c.execute(
            "UPDATE task SET employee_identity_ref=?,"
            "employee_config_revision=?,employee_config_sha256=? WHERE id=?",
            (identity_ref, revision, config_sha256, row["id"]),
        )

    # A thread is one exact role/config lineage. Its schema53 key/version/idx
    # must agree with the exact root task before the root's binding is copied.
    thread_rows = list(c.execute(
        "SELECT th.*,t.emp_idx AS root_emp_idx,t.employee_key AS root_employee_key,"
        "t.employee_catalog_version AS root_employee_catalog_version,"
        "t.employee_identity_ref AS root_identity_ref,"
        "t.employee_config_revision AS root_config_revision,"
        "t.employee_config_sha256 AS root_config_sha256 "
        "FROM task_thread th LEFT JOIN task t ON t.id=th.root_task_id"
    ))
    for raw_thread in thread_rows:
        thread = dict(raw_thread)
        if thread.get("root_emp_idx") is None:
            raise RuntimeError(
                f"schema54 task_thread#{thread['id']} 根任务不存在"
            )
        if (
            int(thread["emp_idx"]) != int(thread["root_emp_idx"])
            or str(thread.get("employee_key") or "").strip()
            != str(thread.get("root_employee_key") or "").strip()
            or str(thread.get("employee_catalog_version") or "").strip()
            != str(thread.get("root_employee_catalog_version") or "").strip()
        ):
            raise RuntimeError(
                f"schema54 task_thread#{thread['id']} 员工身份与根任务不一致"
            )
        root_binding = (
            str(thread["root_identity_ref"]),
            int(thread["root_config_revision"]),
            str(thread["root_config_sha256"]),
        )
        existing_ref = str(thread.get("employee_identity_ref") or "").strip()
        existing_hash = str(thread.get("employee_config_sha256") or "").strip()
        try:
            existing_revision = int(thread.get("employee_config_revision") or 0)
        except (TypeError, ValueError):
            existing_revision = 0
        present = (bool(existing_ref), existing_revision > 0, bool(existing_hash))
        if any(present) and (
            not all(present)
            or (existing_ref, existing_revision, existing_hash) != root_binding
        ):
            raise RuntimeError(
                f"schema54 task_thread#{thread['id']} 员工冻结配置与根任务不一致"
            )
        c.execute(
            "UPDATE task_thread SET employee_identity_ref=?,"
            "employee_config_revision=?,employee_config_sha256=? WHERE id=?",
            (*root_binding, thread["id"]),
        )

    # The existing member_snapshot_json remains the one meeting roster ledger;
    # enrich each member in place so retries and derived tasks reuse the exact
    # role/config generation which entered the room.
    # Materialize before updating ``meeting``.  SQLite invalidates/reuses a
    # write cursor while the SELECT is active; iterating the live cursor can
    # silently skip the second and subsequent meetings during a 53 -> 54
    # migration.  The migration is already inside the outer transaction, so
    # this bounded snapshot preserves atomic rollback without cursor churn.
    meeting_rows = list(c.execute(
        "SELECT id,emp_idxs_json,member_snapshot_json FROM meeting"
    ))
    for meeting in meeting_rows:
        try:
            indices = json.loads(meeting["emp_idxs_json"] or "[]")
            snapshots = json.loads(meeting["member_snapshot_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("会议员工身份快照无法迁移") from exc
        if not isinstance(indices, list) or not isinstance(snapshots, list):
            raise RuntimeError("会议员工身份快照结构无效")
        if not indices and not snapshots:
            continue
        if len(indices) != len(snapshots):
            raise RuntimeError("会议员工身份快照数量不一致")
        changed = False
        enriched = []
        for raw_idx, raw_snapshot in zip(indices, snapshots):
            if not isinstance(raw_snapshot, dict) or int(raw_idx) != int(
                raw_snapshot.get("idx")
            ):
                raise RuntimeError("会议员工身份快照归属无效")
            frozen = _require_schema53_catalog_identity(
                raw_snapshot,
                trusted_identities,
                owner=f"meeting#{meeting['id']}",
            )
            existing_ref = str(raw_snapshot.get("identity_ref") or "").strip()
            existing_hash = str(raw_snapshot.get("config_sha256") or "").strip()
            try:
                existing_revision = int(raw_snapshot.get("config_revision") or 0)
            except (TypeError, ValueError):
                existing_revision = 0
            archived = now if _schema54_historical_catalog(frozen) else None
            identity_ref, revision, config_sha256 = _schema54_insert_role_config(
                c,
                frozen,
                legacy_configs.get(int(frozen["idx"])),
                archived_at=archived,
            )
            present = (
                bool(existing_ref), existing_revision > 0, bool(existing_hash)
            )
            if any(present) and (
                not all(present)
                or existing_ref != identity_ref
                or existing_revision != revision
                or existing_hash != config_sha256
            ):
                raise RuntimeError(
                    f"schema54 meeting#{meeting['id']} 员工冻结配置与历史目录不一致"
                )
            enriched.append({
                **raw_snapshot,
                "identity_ref": identity_ref,
                "config_revision": revision,
                "config_sha256": config_sha256,
            })
            changed = True
        if changed:
            c.execute(
                "UPDATE meeting SET member_snapshot_json=? WHERE id=?",
                (
                    json.dumps(
                        enriched, ensure_ascii=False, separators=(",", ":")
                    ),
                    meeting["id"],
                ),
            )


def _schema55_bundle_payload(frozen: dict, config: dict | None = None) -> dict:
    """Build a deterministic baseline role bundle for one frozen identity."""
    clean = normalize_employee_config(config)
    return {
        "identity": dict(frozen),
        "professional_profile": frozen.get("professional_profile") or {},
        "decision_contract": frozen.get("decision_contract") or {},
        "workflow": frozen.get("workflow") or [],
        "outputs": frozen.get("outputs") or [],
        "config": clean,
    }


def employee_role_bundle_sha256(row_or_identity_ref, config_revision=None,
                                config_sha256=None, frozen=None, config=None) -> str:
    """Recompute one persisted role-bundle digest from canonical content.

    The public row form is used by startup validators and exact task binding;
    the positional form is retained for migration code constructing a new row.
    Stored ``bundle_sha256`` is never treated as evidence without this helper.
    """
    if isinstance(row_or_identity_ref, dict):
        row = dict(row_or_identity_ref)
        identity_ref = str(row.get("identity_ref") or "").strip()
        revision = int(row.get("config_revision") or 0)
        config_hash = str(row.get("config_sha256") or "").strip()
        def _json(value):
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return None
            return value
        baseline = _json(row.get("baseline_json"))
        effective = _json(row.get("effective_json"))
        payload = {
            "identity_ref": identity_ref,
            "config_revision": revision,
            "config_sha256": config_hash,
            "person_snapshot": str(row.get("person_snapshot") or ""),
            "identity_scheme": str(row.get("identity_scheme") or "legacy-six"),
            "baseline": baseline,
            "effective": effective,
        }
    else:
        identity_ref = str(row_or_identity_ref or "").strip()
        revision = int(config_revision or 0)
        config_hash = str(config_sha256 or "").strip()
        payload = {
            "identity_ref": identity_ref,
            "config_revision": revision,
            "config_sha256": config_hash,
            "person_snapshot": str((frozen or {}).get("person_snapshot") or ""),
            "identity_scheme": str((frozen or {}).get("identity_scheme") or "legacy-six"),
            "baseline": _schema55_bundle_payload(frozen or {}, config),
            "effective": _schema55_bundle_payload(frozen or {}, config),
        }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _schema55_bundle_sha256(
    identity_ref: str, config_revision: int, config_sha256: str,
    frozen: dict, config: dict | None = None,
) -> str:
    return employee_role_bundle_sha256(
        identity_ref, config_revision, config_sha256, frozen, config,
    )


def get_employee_role_bundle(
    identity_ref: str, config_revision: int, config_sha256: str,
    bundle_sha256: str | None = None,
):
    """Load and revalidate one immutable role bundle revision."""
    row = one(
        "SELECT * FROM employee_role_bundle_revision "
        "WHERE identity_ref=? AND config_revision=? AND config_sha256=?",
        (str(identity_ref or "").strip(), int(config_revision),
         str(config_sha256 or "").strip()),
    )
    if not row:
        return None
    stored = str(row.get("bundle_sha256") or "").strip()
    if not employee_role_bundle_row_valid(row) or (
        bundle_sha256 is not None and str(bundle_sha256).strip() != stored
    ):
        return None
    try:
        baseline = json.loads(row.get("baseline_json") or "{}")
        effective = json.loads(row.get("effective_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return {**row, "baseline": baseline, "effective": effective}


def employee_role_bundle_row_valid(row) -> bool:
    """Fail-closed integrity predicate for a stored role-bundle row."""
    try:
        raw = dict(row or {})
        identity_ref = str(raw.get("identity_ref") or "").strip()
        config_sha = str(raw.get("config_sha256") or "").strip()
        bundle_sha = str(raw.get("bundle_sha256") or "").strip()
        revision = int(raw.get("config_revision"))
        if (
            not re.fullmatch(r"[0-9a-f]{64}", identity_ref)
            or not re.fullmatch(r"[0-9a-f]{64}", config_sha)
            or not re.fullmatch(r"[0-9a-f]{64}", bundle_sha)
            or revision < 1
        ):
            return False
        baseline = raw.get("baseline_json")
        effective = raw.get("effective_json")
        if isinstance(baseline, str):
            baseline = json.loads(baseline)
        if isinstance(effective, str):
            effective = json.loads(effective)
        if not isinstance(baseline, dict) or not isinstance(effective, dict):
            return False
        # The digest covers the triple, but a row with a freshly recomputed
        # digest must still be rejected if its JSON payload describes another
        # employee/config revision.  Check both baseline and effective identity
        # snapshots explicitly so callers cannot cross-bind a valid hash to a
        # different role row.
        baseline_identity = baseline.get("identity")
        effective_identity = effective.get("identity")
        if not isinstance(baseline_identity, dict) or not isinstance(
            effective_identity, dict
        ):
            return False
        for identity in (baseline_identity, effective_identity):
            try:
                if _schema55_identity_ref(identity) != identity_ref:
                    return False
            except (TypeError, ValueError, OverflowError):
                return False
            if (
                str(identity.get("key") or "") != str(raw.get("employee_key") or "")
                or str(identity.get("catalog_version") or "")
                != str(raw.get("employee_catalog_version") or "")
                or str(identity.get("name") or "")
                != str(raw.get("employee_name_snapshot") or "")
                or str(identity.get("person_snapshot") or "")
                != str(raw.get("person_snapshot") or "")
                or str(identity.get("identity_scheme") or "legacy-six")
                != str(raw.get("identity_scheme") or "legacy-six")
            ):
                return False
        baseline_config = baseline.get("config")
        effective_config = effective.get("config")
        if not isinstance(baseline_config, dict) or not isinstance(
            effective_config, dict
        ):
            return False
        # config_sha256 identifies the immutable base revision.  An effective
        # proposal may intentionally differ, but both JSON objects must still
        # be normalizable before the row's content hash is trusted.
        if employee_config_sha256(identity_ref, revision, baseline_config) != config_sha:
            return False
        normalize_employee_config(effective_config)
        expected = employee_role_bundle_sha256(raw)
        return hmac.compare_digest(expected, bundle_sha)
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return False


def _schema55_insert_bundle(
    c, frozen: dict, config_row: dict, *, status: str = "active",
) -> str:
    frozen = _schema55_frozen_identity(frozen)
    identity_ref = _schema55_identity_ref(frozen)
    revision = int(config_row.get("config_revision") or 1)
    config_sha256 = str(config_row.get("config_sha256") or "").strip()
    effective = _schema55_bundle_payload(frozen, config_row)
    baseline_json = json.dumps(
        effective, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    bundle_sha256 = _schema55_bundle_sha256(
        identity_ref, revision, config_sha256, frozen, config_row,
    )
    now = time.time()
    c.execute(
        "INSERT OR IGNORE INTO employee_role_bundle_revision("
        "identity_ref,idx,employee_key,employee_catalog_version,employee_name_snapshot,"
        "person_snapshot,identity_scheme,config_revision,config_sha256,bundle_sha256,"
        "baseline_json,effective_json,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity_ref, frozen["idx"], frozen["key"], frozen["catalog_version"],
            frozen["name"], frozen.get("person_snapshot", ""),
            frozen.get("identity_scheme", "legacy-six"), revision, config_sha256,
            bundle_sha256, baseline_json, baseline_json, status, now, now,
        ),
    )
    row = c.execute(
        "SELECT bundle_sha256 FROM employee_role_bundle_revision "
        "WHERE identity_ref=? AND config_revision=?",
        (identity_ref, revision),
    ).fetchone()
    if not row or str(row["bundle_sha256"]) != bundle_sha256:
        raise RuntimeError("schema55 role bundle 完整性校验失败")
    return bundle_sha256


def _schema55_migrate(c, *, source_schema_version: int) -> None:
    """Migrate schema54 bindings to V4 identities in one outer transaction."""
    if int(source_schema_version) >= 55:
        return
    trusted = _schema55_trusted_identity_catalog()
    by_idx = {}
    for ref, frozen in trusted.items():
        by_idx.setdefault(int(frozen["idx"]), []).append((ref, frozen))

    # Existing role rows must resolve against the complete retained catalog;
    # only after every row passes do we begin changing slot pointers.
    role_rows = [dict(row) for row in c.execute(
        "SELECT * FROM employee_role_config"
    )]
    for row in role_rows:
        stored_ref = str(row.get("identity_ref") or "").strip()
        trusted_frozen = trusted.get(stored_ref)
        frozen = trusted_frozen or _schema55_frozen_identity(row)
        ref = stored_ref or _schema55_identity_ref(frozen)
        validation_row = dict(row)
        if trusted_frozen:
            validation_row.update({
                "employee_key": trusted_frozen["key"],
                "employee_catalog_version": trusted_frozen["catalog_version"],
                "employee_name_snapshot": trusted_frozen["name"],
                "employee_dept_key": trusted_frozen["dept_key"],
                "employee_spec_sha256": trusted_frozen["spec_sha256"],
                "person_snapshot": trusted_frozen.get("person_snapshot", ""),
                "identity_scheme": trusted_frozen.get("identity_scheme", "legacy-six"),
            })
        if ref not in trusted or not employee_role_config_row_valid(validation_row):
            raise RuntimeError("schema55 旧岗位配置无法通过完整身份校验")

    # Add historical display snapshots to existing configs without changing
    # their old identity refs or hashes.
    for row in role_rows:
        stored_ref = str(row.get("identity_ref") or "").strip()
        frozen = trusted.get(stored_ref)
        if frozen is None:
            frozen = trusted[_schema55_identity_ref(_schema55_frozen_identity(row))]
        c.execute(
            "UPDATE employee_role_config SET person_snapshot=?,identity_scheme=? "
            "WHERE identity_ref=?",
            (frozen.get("person_snapshot", ""), frozen.get("identity_scheme", "legacy-six"),
             row["identity_ref"]),
        )
    role_rows = [dict(row) for row in c.execute(
        "SELECT * FROM employee_role_config"
    )]
    role_by_ref = {str(row["identity_ref"]): row for row in role_rows}

    # Every persisted task/meeting snapshot must point to an existing retained
    # identity.  This is checked before any V4 slot switch and therefore rolls
    # the whole migration back on unknown idx/hash drift.
    task_rows = list(c.execute(
        "SELECT id,emp_idx,employee_key,employee_catalog_version,"
        "employee_name_snapshot,employee_dept_key,employee_spec_sha256 "
        "FROM task"
    ))
    for raw in task_rows:
        row = dict(raw)
        frozen = _schema55_frozen_identity(row)
        ref = _schema55_identity_ref(frozen)
        if ref not in trusted or int(frozen["idx"]) != int(row["emp_idx"]):
            raise RuntimeError(f"schema55 task#{row['id']} 员工身份无法精确解析")
    meeting_rows = list(c.execute(
        "SELECT id,emp_idxs_json,member_snapshot_json FROM meeting"
    ))
    for raw in meeting_rows:
        row = dict(raw)
        try:
            indices = json.loads(row.get("emp_idxs_json") or "[]")
            snapshots = json.loads(row.get("member_snapshot_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"schema55 meeting#{row['id']} 快照无法解析") from exc
        if not isinstance(indices, list) or not isinstance(snapshots, list):
            raise RuntimeError(f"schema55 meeting#{row['id']} 快照结构无效")
        if len(indices) != len(snapshots):
            raise RuntimeError(f"schema55 meeting#{row['id']} 快照数量不一致")
        for raw_snapshot in snapshots:
            frozen = _schema55_frozen_identity(raw_snapshot)
            if _schema55_identity_ref(frozen) not in trusted:
                raise RuntimeError(f"schema55 meeting#{row['id']} 员工身份无法精确解析")

    # Backfill bundles and exact person/bundle snapshots for old work.  A V3
    # task continues using its archived V3 config; it never sees the V4 slot.
    bundle_by_ref = {}
    for ref, frozen in trusted.items():
        config = role_by_ref.get(ref)
        if config is None:
            # V4 receives an intentionally empty config.  Historical V1--V3
            # generations also receive an empty baseline when an old database
            # never had a mutable row; this makes every retained identity
            # addressable without borrowing another idx's config.
            ref, revision, config_hash = _schema55_insert_role_config(
                c, frozen, None, empty_config=True,
            )
            config = dict(c.execute(
                "SELECT * FROM employee_role_config WHERE identity_ref=?", (ref,)
            ).fetchone())
            role_by_ref[ref] = config
        bundle_by_ref[ref] = _schema55_insert_bundle(c, frozen, config, status=(
            "active" if frozen.get("catalog_version") == "2026.08.v4" else "historical"
        ))

    for raw in task_rows:
        row = dict(raw)
        frozen = _schema55_frozen_identity(row)
        ref = _schema55_identity_ref(frozen)
        config = role_by_ref.get(ref)
        if config is None or ref not in bundle_by_ref:
            raise RuntimeError(f"schema55 task#{row['id']} 岗位 bundle 缺失")
        trusted_frozen = trusted[ref]
        c.execute(
            "UPDATE task SET person_snapshot=?,identity_scheme=?,bundle_sha256=? WHERE id=?",
            (trusted_frozen.get("person_snapshot", ""),
             trusted_frozen.get("identity_scheme", "legacy-six"),
             bundle_by_ref[ref], row["id"]),
        )
    # Threads inherit the root task's exact identity/bundle tuple.
    c.execute(
        "UPDATE task_thread SET person_snapshot=(SELECT person_snapshot FROM task WHERE task.id=task_thread.root_task_id),"
        "identity_scheme=(SELECT identity_scheme FROM task WHERE task.id=task_thread.root_task_id),"
        "bundle_sha256=(SELECT bundle_sha256 FROM task WHERE task.id=task_thread.root_task_id)"
    )
    meeting_rows = list(c.execute("SELECT id,emp_idxs_json,member_snapshot_json FROM meeting"))
    for raw in meeting_rows:
        row = dict(raw)
        snapshots = json.loads(row.get("member_snapshot_json") or "[]")
        enriched = []
        for raw_snapshot in snapshots:
            frozen = _schema55_frozen_identity(raw_snapshot)
            ref = _schema55_identity_ref(frozen)
            if ref not in bundle_by_ref:
                raise RuntimeError(f"schema55 meeting#{row['id']} 岗位 bundle 缺失")
            trusted_frozen = trusted[ref]
            enriched.append({
                **raw_snapshot,
                "person_snapshot": trusted_frozen.get("person_snapshot", ""),
                "identity_scheme": trusted_frozen.get(
                    "identity_scheme", "legacy-six"
                ),
                "bundle_sha256": bundle_by_ref[ref],
            })
        c.execute(
            "UPDATE meeting SET member_snapshot_json=? WHERE id=?",
            (json.dumps(enriched, ensure_ascii=False, separators=(",", ":")), row["id"]),
        )

    # V4 is current for the 360 reused industry slots; preserve enabled and
    # increment row_version as the identity pointer changes.
    now = time.time()
    for idx, candidates in by_idx.items():
        v4 = next((item for item in candidates if item[1].get("catalog_version") == "2026.08.v4"), None)
        if not v4:
            continue
        ref, frozen = v4
        row = c.execute(
            "SELECT active_identity_ref,enabled,row_version FROM employee_slot WHERE idx=?",
            (idx,),
        ).fetchone()
        if row is None:
            continue
        current_ref = str(row["active_identity_ref"] or "")
        if current_ref == ref:
            continue
        c.execute(
            "UPDATE employee_slot SET active_identity_ref=?,row_version=row_version+1,updated_at=? WHERE idx=?",
            (ref, now, idx),
        )

def _existing_schema_version(c) -> tuple[int, int, int]:
    """在任何 WAL/DDL 之前只读识别版本，防止旧程序先改未来库再拒绝。"""
    user_version = int(c.execute("PRAGMA user_version").fetchone()[0] or 0)
    has_ledger = c.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='schema_version'"
    ).fetchone()
    ledger_version = 0
    if has_ledger:
        row = c.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_version"
        ).fetchone()
        ledger_version = int((row or [0])[0] or 0)
    return max(user_version, ledger_version), ledger_version, user_version


def _columns(c, table: str) -> set[str]:
    if not table.replace("_", "").isalnum():
        raise ValueError("invalid table")
    return {str(row["name"]) for row in c.execute(f"PRAGMA table_info({table})")}


def _require_columns(c, table: str, required: set[str], stage: str) -> None:
    actual = _columns(c, table)
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(
            f"数据库{stage}结构不完整：{table} 缺少列 {','.join(missing)}"
        )


def _require_index_contract(
    c,
    table: str,
    name: str,
    columns: tuple[str, ...],
    *,
    unique: bool,
    partial: bool,
) -> None:
    """Validate semantics for indexes used by SQLite conflict targets.

    Merely checking an index name is unsafe: an early schema-52 candidate used
    the same natural-index name with one extra column.  SQLite then accepts the
    database at startup but rejects every matching ``ON CONFLICT`` statement at
    commit time.  Refuse startup before serving traffic when the exact ordered
    key, uniqueness or partial-index contract differs.
    """
    for identifier in (table, name, *columns):
        if not identifier.replace("_", "").isalnum():
            raise ValueError("invalid index contract")
    index_row = next((
        row for row in c.execute(f"PRAGMA index_list({table})")
        if str(row["name"]) == name
    ), None)
    actual_columns = tuple(
        str(row["name"])
        for row in c.execute(f"PRAGMA index_info({name})")
    ) if index_row is not None else ()
    if (
        index_row is None
        or bool(index_row["unique"]) is not bool(unique)
        or bool(index_row["partial"]) is not bool(partial)
        or actual_columns != tuple(columns)
    ):
        raise RuntimeError(
            "数据库迁移后索引结构不完整："
            f"{name} 必须是"
            f"{'唯一' if unique else '普通'}"
            f"{'部分' if partial else '非部分'}索引，列为 {','.join(columns)}"
        )


def _require_unique_columns_contract(
    c, table: str, columns: tuple[str, ...],
) -> None:
    """Require an exact non-partial UNIQUE key, including table constraints."""
    if not table.replace("_", "").isalnum():
        raise ValueError("invalid unique contract")
    for row in c.execute(f"PRAGMA index_list({table})"):
        if not bool(row["unique"]) or bool(row["partial"]):
            continue
        name = str(row["name"])
        if not name.replace("_", "").isalnum():
            continue
        actual = tuple(
            str(item["name"])
            for item in c.execute(f"PRAGMA index_info('{name}')")
        )
        if actual == tuple(columns):
            return
    raise RuntimeError(
        "数据库迁移后索引结构不完整："
        f"{table} 必须按 {','.join(columns)} 唯一"
    )


def _validate_existing_database(c, ledger_version: int) -> None:
    """拒绝把无关/残缺 SQLite 文件叠加迁移成“最新库”标记。"""
    tables = {
        str(row["name"])
        for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables:
        return
    if "job" not in tables:
        raise RuntimeError("数据库旧版结构不完整：缺少核心表 job")
    _require_columns(
        c,
        "job",
        {
            "id", "brief_json", "mode", "status", "current_idx",
            "created_at", "updated_at",
        },
        "旧版",
    )
    # v8 起已经是多租户；无版本账本的生产旧库也必须有这组可识别签名。
    if ledger_version >= 8 or "schema_version" not in tables:
        for table, required in {
            "tenants": {"id", "name"},
            "users": {
                "id", "tenant_id", "username", "password_hash", "role"
            },
        }.items():
            if table not in tables:
                raise RuntimeError(f"数据库旧版结构不完整：缺少核心表 {table}")
            _require_columns(c, table, required, "旧版")


def _schema54_exact_snapshot_ref(raw: dict, *, task_shape: bool) -> str:
    """Validate canonical frozen identity fields and return their full ref."""
    idx_field = "emp_idx" if task_shape else "idx"
    raw_idx = raw.get(idx_field)
    if isinstance(raw_idx, bool):
        raise RuntimeError("员工身份快照编号无效")
    field_names = (
        (
            "employee_key", "employee_catalog_version",
            "employee_name_snapshot", "employee_dept_key",
            "employee_spec_sha256",
        )
        if task_shape else
        ("key", "catalog_version", "name", "dept_key", "spec_sha256")
    )
    if any(
        not isinstance(raw.get(field), str)
        or not raw[field]
        or raw[field] != raw[field].strip()
        for field in field_names
    ):
        raise RuntimeError("员工身份快照字段不完整或不规范")
    frozen = _schema54_frozen_identity(raw)
    if not re.fullmatch(r"[0-9a-f]{64}", frozen["spec_sha256"]):
        raise RuntimeError("员工身份快照摘要无效")
    # Schema55 V4 rows use the new person-aware digest; V1--V3 continue down
    # the exact legacy path below.
    if frozen["catalog_version"] == "2026.08.v4":
        person = raw.get("person_snapshot")
        scheme = raw.get("identity_scheme")
        if not isinstance(person, str) or not person.strip() or scheme != "v2-person":
            raise RuntimeError("V4 员工身份快照缺少 person_snapshot/identity_scheme")
        frozen["person_snapshot"] = person.strip()
        frozen["identity_scheme"] = scheme
        return employee_identity_ref_v4(frozen)
    return employee_identity_ref(frozen)


def _schema54_role_revision_exists(
    c, identity_ref: str, revision: int, config_sha256: str,
) -> bool:
    return c.execute(
        "SELECT 1 FROM employee_role_config WHERE identity_ref=? "
        "AND config_revision=? AND config_sha256=? "
        "UNION ALL SELECT 1 FROM employee_role_config_history "
        "WHERE identity_ref=? AND config_revision=? AND config_sha256=? LIMIT 1",
        (
            identity_ref, revision, config_sha256,
            identity_ref, revision, config_sha256,
        ),
    ).fetchone() is not None


def _validate_schema54_frozen_bindings(c) -> None:
    """Revalidate task, thread and meeting identity/config bindings at startup."""
    for raw_task in c.execute(
        "SELECT id,emp_idx,employee_key,employee_catalog_version,"
        "employee_name_snapshot,employee_dept_key,employee_spec_sha256,"
        "employee_identity_ref,employee_config_revision,employee_config_sha256,"
        "person_snapshot,identity_scheme,bundle_sha256 "
        "FROM task"
    ):
        task = dict(raw_task)
        try:
            expected_ref = _schema54_exact_snapshot_ref(task, task_shape=True)
            revision = int(task["employee_config_revision"])
        except (TypeError, ValueError, OverflowError, RuntimeError) as exc:
            raise RuntimeError(
                f"数据库任务员工身份快照无效: task#{task['id']}"
            ) from exc
        stored_ref = str(task.get("employee_identity_ref") or "")
        stored_hash = str(task.get("employee_config_sha256") or "")
        stored_person = str(task.get("person_snapshot") or "")
        stored_scheme = str(task.get("identity_scheme") or "legacy-six")
        stored_bundle = str(task.get("bundle_sha256") or "")
        if (
            stored_ref != stored_ref.strip()
            or stored_hash != stored_hash.strip()
            or stored_ref != expected_ref
            or revision < 1
            or not re.fullmatch(r"[0-9a-f]{64}", stored_hash)
            or not re.fullmatch(r"[0-9a-f]{64}", stored_bundle)
            or stored_scheme not in {"legacy-six", "v2-person"}
            or not _schema54_role_revision_exists(
                c, stored_ref, revision, stored_hash,
            )
        ):
            raise RuntimeError(
                f"数据库任务员工冻结配置无效: task#{task['id']}"
            )
        bundle = c.execute(
            "SELECT * FROM employee_role_bundle_revision "
            "WHERE identity_ref=? AND config_revision=? AND config_sha256=? "
            "AND bundle_sha256=?",
            (stored_ref, revision, stored_hash, stored_bundle),
        ).fetchone()
        if bundle is None or not employee_role_bundle_row_valid(bundle):
            raise RuntimeError(
                f"数据库任务 role bundle 无效: task#{task['id']}"
            )
        if stored_scheme == "v2-person":
            # ``_schema54_exact_snapshot_ref`` above already recomputed the
            # person-aware reference; this explicit check keeps an empty person
            # from ever being accepted through a legacy fallback path.
            if not stored_person:
                raise RuntimeError(
                    f"数据库任务 V4 person_snapshot 缺失: task#{task['id']}"
                )
            if str(bundle["identity_scheme"] or "") != "v2-person" or (
                str(bundle["person_snapshot"] or "") != stored_person
            ):
                raise RuntimeError(
                    f"数据库任务 V4 role bundle 身份不一致: task#{task['id']}"
                )
        elif stored_scheme != "legacy-six":
            raise RuntimeError(
                f"数据库任务 identity_scheme 无效: task#{task['id']}"
            )

    if c.execute(
        "SELECT 1 FROM task_thread th LEFT JOIN task root "
        "ON root.id=th.root_task_id WHERE root.id IS NULL "
        "OR th.emp_idx<>root.emp_idx OR th.employee_key<>root.employee_key "
        "OR th.employee_catalog_version<>root.employee_catalog_version "
        "OR th.employee_identity_ref<>root.employee_identity_ref "
        "OR th.employee_config_revision<>root.employee_config_revision "
        "OR th.employee_config_sha256<>root.employee_config_sha256 "
        "OR th.person_snapshot<>root.person_snapshot "
        "OR th.identity_scheme<>root.identity_scheme "
        "OR th.bundle_sha256<>root.bundle_sha256 LIMIT 1"
    ).fetchone():
        raise RuntimeError("数据库任务线程员工身份与根任务不一致")

    for raw_meeting in c.execute(
        "SELECT id,emp_idxs_json,member_snapshot_json FROM meeting"
    ):
        meeting = dict(raw_meeting)
        try:
            indices = json.loads(meeting.get("emp_idxs_json") or "[]")
            snapshots = json.loads(meeting.get("member_snapshot_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"数据库会议员工快照无法解析: meeting#{meeting['id']}"
            ) from exc
        if (
            not isinstance(indices, list)
            or not isinstance(snapshots, list)
            or len(indices) != len(snapshots)
        ):
            raise RuntimeError(
                f"数据库会议员工快照数量无效: meeting#{meeting['id']}"
            )
        for raw_idx, raw_snapshot in zip(indices, snapshots):
            if (
                isinstance(raw_idx, bool)
                or not isinstance(raw_snapshot, dict)
                or isinstance(raw_snapshot.get("idx"), bool)
            ):
                raise RuntimeError(
                    f"数据库会议员工快照归属无效: meeting#{meeting['id']}"
                )
            try:
                if int(raw_idx) != int(raw_snapshot.get("idx")):
                    raise ValueError
                expected_ref = _schema54_exact_snapshot_ref(
                    raw_snapshot, task_shape=False,
                )
                revision = int(raw_snapshot.get("config_revision"))
            except (TypeError, ValueError, OverflowError, RuntimeError) as exc:
                raise RuntimeError(
                    f"数据库会议员工快照无效: meeting#{meeting['id']}"
                ) from exc
            stored_ref = str(raw_snapshot.get("identity_ref") or "")
            stored_hash = str(raw_snapshot.get("config_sha256") or "")
            stored_person = str(raw_snapshot.get("person_snapshot") or "")
            stored_scheme = str(raw_snapshot.get("identity_scheme") or "legacy-six")
            stored_bundle = str(raw_snapshot.get("bundle_sha256") or "")
            if (
                stored_ref != stored_ref.strip()
                or stored_hash != stored_hash.strip()
                or stored_ref != expected_ref
                or revision < 1
                or not re.fullmatch(r"[0-9a-f]{64}", stored_hash)
                or not re.fullmatch(r"[0-9a-f]{64}", stored_bundle)
                or stored_scheme not in {"legacy-six", "v2-person"}
                or not _schema54_role_revision_exists(
                    c, stored_ref, revision, stored_hash,
                )
            ):
                raise RuntimeError(
                    f"数据库会议员工冻结配置无效: meeting#{meeting['id']}"
                )
            bundle = c.execute(
                "SELECT * FROM employee_role_bundle_revision "
                "WHERE identity_ref=? AND config_revision=? AND config_sha256=? "
                "AND bundle_sha256=?",
                (stored_ref, revision, stored_hash, stored_bundle),
            ).fetchone()
            if bundle is None or not employee_role_bundle_row_valid(bundle):
                raise RuntimeError(
                    f"数据库会议 role bundle 无效: meeting#{meeting['id']}"
                )
            if stored_scheme == "v2-person":
                if not stored_person or str(bundle["identity_scheme"] or "") != "v2-person" \
                        or str(bundle["person_snapshot"] or "") != stored_person:
                    raise RuntimeError(
                        f"数据库会议 V4 role bundle 身份不一致: meeting#{meeting['id']}"
                    )


def _validate_migrated_database(c) -> None:
    contracts = {
        "tenants": {"id", "name", "enabled", "balance"},
        "users": {
            "id", "tenant_id", "username", "password_hash", "role",
            "modules_json", "enabled", "must_change_password",
        },
        "job": {
            "id", "brief_json", "mode", "status", "current_idx", "tenant_id",
            "billing_status", "billing_points", "retry_count", "deleted_at",
            "deleted_by", "delete_reason", "created_by",
            "created_at", "updated_at",
        },
        "task": {
            "id", "emp_idx", "brief_json", "status", "tenant_id",
            "employee_key", "employee_catalog_version",
            "employee_name_snapshot", "employee_dept_key",
            "employee_spec_sha256", "employee_identity_ref",
            "employee_config_revision", "employee_config_sha256",
            "person_snapshot", "identity_scheme", "bundle_sha256",
            "billing_status", "billing_points", "retry_count", "deleted_at",
            "deleted_by", "delete_reason", "created_by", "thread_id",
            "revision_no", "phase", "request_key", "terminal_at",
            "refunded_at",
        },
        "task_thread": {
            "id", "tenant_id", "emp_idx", "root_task_id",
            "employee_key", "employee_catalog_version",
            "employee_identity_ref", "employee_config_revision",
            "employee_config_sha256",
            "person_snapshot", "identity_scheme", "bundle_sha256",
            "current_task_id", "accepted_task_id", "status",
            "revision_count", "created_by", "satisfied_at",
            "created_at", "updated_at",
        },
        "employee_slot": {
            "idx", "active_identity_ref", "enabled", "row_version",
            "created_at", "updated_at",
        },
        "employee_role_config": {
            "identity_ref", "idx", "employee_key",
            "employee_catalog_version", "employee_name_snapshot",
            "employee_dept_key", "employee_spec_sha256", "prompt_template",
            "person_snapshot", "identity_scheme",
            "skills_json", "learned_at", "settings_json", "caps_off_json",
            "model_text", "model_image", "professional_profile_json",
            "config_revision", "config_sha256",
            "archived_at", "created_at", "updated_at",
        },
        "employee_role_config_history": {
            "identity_ref", "config_revision", "idx", "employee_key",
            "employee_catalog_version", "employee_name_snapshot",
            "employee_dept_key", "employee_spec_sha256", "prompt_template",
            "person_snapshot", "identity_scheme",
            "skills_json", "learned_at", "settings_json", "caps_off_json",
            "model_text", "model_image", "professional_profile_json",
            "config_sha256", "archived_at",
            "created_at", "updated_at", "superseded_at",
        },
        "employee_role_bundle_revision": {
            "identity_ref", "config_revision", "idx", "employee_key",
            "employee_catalog_version", "employee_name_snapshot",
            "person_snapshot", "identity_scheme", "config_sha256",
            "bundle_sha256", "baseline_json", "effective_json", "status",
            "created_at", "updated_at",
        },
        "employee_learning_batch": {
            "id", "tenant_id", "request_key", "status", "budget_points",
            "max_runs", "completed_runs", "failed_runs", "metadata_json",
            "created_by", "created_at", "updated_at",
        },
        "employee_learning_run": {
            "id", "batch_id", "identity_ref", "config_revision",
            "base_config_revision", "base_config_sha256", "status",
            "budget_points", "spent_points", "result_json", "error_code",
            "error_message", "created_at", "updated_at",
        },
        "employee_learning_source": {
            "id", "run_id", "url", "canonical_url", "title", "publisher",
            "source_level", "published_at", "fetched_at", "http_status",
            "certificate_status", "content_sha256", "excerpt", "metadata_json",
            "created_at",
        },
        "employee_learning_artifact": {
            "id", "run_id", "artifact_type", "status", "claim_text",
            "delta_json", "source_ids_json", "evidence_json", "reviewer_id",
            "reviewed_at", "created_at", "updated_at",
        },
        "tenant_industry": {
            "tenant_id", "industry_key", "is_primary", "created_at",
        },
        "store_branch": {
            "id", "tenant_id", "industry_key", "name", "region",
            "address", "active", "created_by", "created_at", "updated_at",
            "store_code", "province", "city", "district", "manager_name",
            "manager_employee_no", "manager_phone", "store_type", "opened_on",
            "area_sqm", "seat_count", "longitude", "latitude", "remark",
            "row_version",
        },
        "inspection_branch_import": {
            "id", "tenant_id", "industry_key", "request_key", "source_sha256",
            "filename", "status", "total_rows", "create_count", "update_count",
            "skip_count", "error_count", "created_by", "committed_by",
            "business_values_json", "catalog_version", "catalog_sha256",
            "business_create_count", "business_update_count",
            "business_skip_count", "business_error_count",
            "created_at", "updated_at", "committed_at", "staging_purged_at",
            "audit_archive_sha256", "audit_archive_bytes", "audit_archive_rows",
            "audit_archived_at", "audit_actions_json", "archive_sha256", "archive_size",
            "archive_row_count", "archived_at",
        },
        "inspection_branch_import_archive": {
            "id", "archive_sha256", "payload_zlib", "uncompressed_bytes",
            "row_count", "created_at",
        },
        "inspection_branch_import_row": {
            "id", "import_id", "tenant_id", "row_number", "store_code",
            "action", "error_code", "error_message", "payload_json",
            "masked_payload_json", "existing_branch_id",
            "existing_row_version", "existing_business_value_id",
            "existing_business_row_version", "created_at",
        },
        "inspection_business_value": {
            "id", "tenant_id", "industry_key", "branch_id", "import_id",
            "metric_key", "period_start", "period_end", "value", "unit",
            "source_ref", "remark", "row_version", "created_at", "updated_at",
        },
        "inspection_standard_override": {
            "id", "tenant_id", "industry_key", "scope_kind", "scope_key",
            "item_code", "patch_json", "row_version", "active", "created_by",
            "created_at", "updated_at",
        },
        "inspection_visit": {
            "id", "tenant_id", "industry_key", "branch_id", "status",
            "score", "summary_md", "employee_idx", "task_id",
            "request_key", "visit_at", "created_by", "created_at",
            "updated_at", "completed_at", "terminal_at",
            "template_key", "template_version", "template_snapshot_json",
            "observations_json",
        },
        "inspection_photo": {
            "id", "tenant_id", "visit_id", "storage_key", "mime_type",
            "byte_size", "sha256", "phase", "created_by", "created_at",
            "capture_slot", "item_code",
        },
        "inspection_issue": {
            "id", "tenant_id", "visit_id", "title", "description",
            "severity", "status", "due_at", "created_at", "updated_at",
        },
        "inspection_evidence": {
            "id", "tenant_id", "visit_id", "issue_id", "photo_id",
            "note", "created_at",
        },
        "inspection_action": {
            "id", "tenant_id", "visit_id", "issue_id", "status", "plan",
            "due_at", "closed_at", "created_at", "updated_at",
        },
        "inspection_recheck": {
            "id", "tenant_id", "visit_id", "issue_id", "action_id",
            "status", "created_by", "created_at",
        },
        "inspection_event": {
            "id", "tenant_id", "visit_id", "kind", "payload_json",
            "created_by", "created_at",
        },
        "station_run": {
            "id", "job_id", "station_idx", "status", "reviewed_by",
        },
        "account_profile": {
            "id", "tenant_id", "name", "deleted_at", "deleted_by",
            "delete_reason",
        },
        "asset": {
            "id", "tenant_id", "type", "deleted_at", "deleted_by",
            "delete_reason",
        },
        "schedule": {
            "id", "tenant_id", "name", "enabled", "fail_streak",
        },
        "knowledge": {"id", "tenant_id", "title", "content", "deleted_at"},
        "avatar_job": {
            "id", "tenant_id", "params_json", "status", "billing_status",
            "retry_count", "deleted_at", "created_by",
        },
        "meeting": {
            "id", "tenant_id", "question", "status", "phase",
            "billing_status", "retry_count", "created_by",
            "member_snapshot_json",
        },
        "tv_job": {
            "id", "tenant_id", "params_json", "status", "billing_status",
            "retry_count", "created_by",
        },
        "tool_job": {
            "id", "tenant_id", "kind", "status", "billing_status",
            "retry_count", "retry_started_at", "created_by",
        },
        "pub_task": {
            "id", "tenant_id", "platform", "status", "retry_count",
            "submission_state", "submit_started_at",
        },
        "notification": {
            "id", "tenant_id", "kind", "title", "body", "link",
            "job_id", "user_id", "read_by", "read_at", "created_at",
        },
        "funnel_event": {
            "day", "event", "dimension", "tenant_id", "actor_hash", "hits",
            "first_at", "last_at",
        },
        "billing_operation": {
            "op_key", "tenant_id", "job_id", "action", "points", "status",
        },
        "billing_log": {
            "id", "tenant_id", "job_id", "delta", "balance", "reason",
        },
        "censor_log": {
            "id", "tenant_id", "job_id", "kind", "title", "report",
        },
        "purchase_intent": {
            "id", "tenant_id", "created_by", "request_key",
            "plan_key", "period_key", "plan_name", "period_label",
            "quoted_price", "quoted_points", "contact", "customer_note",
            "status", "handler_note", "handled_by", "contacted_at",
            "lost_at", "paid_at", "subscription_op_key", "receipt_json",
            "created_at", "updated_at",
        },
        "wechat_draft_delivery": {
            "id", "tenant_id", "job_id", "request_hash", "status",
            "billing_status", "op_key",
        },
    }
    tables = {
        str(row["name"])
        for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for table, required in contracts.items():
        if table not in tables:
            raise RuntimeError(f"数据库迁移后结构不完整：缺少核心表 {table}")
        _require_columns(c, table, required, "迁移后")
    if c.execute(
        "SELECT 1 FROM task WHERE employee_key='' "
        "OR employee_catalog_version='' OR employee_name_snapshot='' "
        "OR employee_dept_key='' OR employee_spec_sha256='' LIMIT 1"
    ).fetchone():
        raise RuntimeError("数据库迁移后结构不完整：task 员工身份快照缺失")
    if c.execute(
        "SELECT 1 FROM task_thread WHERE employee_key='' "
        "OR employee_catalog_version='' LIMIT 1"
    ).fetchone():
        raise RuntimeError("数据库迁移后结构不完整：task_thread 员工身份缺失")
    for table in ("task", "task_thread"):
        if c.execute(
            f"SELECT 1 FROM {table} WHERE length(employee_identity_ref)<>64 "
            "OR employee_config_revision<1 "
            "OR length(employee_config_sha256)<>64 LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                f"数据库迁移后结构不完整：{table} 员工配置快照缺失"
            )
        if c.execute(
            f"SELECT 1 FROM {table} WHERE length(bundle_sha256)<>64 "
            "OR identity_scheme='' LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                f"数据库迁移后结构不完整：{table} role bundle 快照缺失"
            )
    for table in ("employee_role_config", "employee_role_config_history"):
        for row in c.execute(f"SELECT * FROM {table}"):
            if not employee_role_config_row_valid(row):
                raise RuntimeError(
                    f"数据库岗位配置完整性校验失败：{table}"
                )
    for row in c.execute("SELECT * FROM employee_role_bundle_revision"):
        stored_bundle = str(row["bundle_sha256"] or "").strip()
        if not employee_role_bundle_row_valid(row):
            raise RuntimeError(
                f"数据库 role bundle 完整性校验失败: {row['identity_ref']}"
            )
    _validate_schema54_frozen_bindings(c)
    if c.execute(
        "SELECT 1 FROM task t LEFT JOIN employee_role_bundle_revision b ON "
        "b.identity_ref=t.employee_identity_ref "
        "AND b.config_revision=t.employee_config_revision "
        "AND b.config_sha256=t.employee_config_sha256 "
        "AND b.bundle_sha256=t.bundle_sha256 "
        "WHERE b.identity_ref IS NULL LIMIT 1"
    ).fetchone():
        raise RuntimeError("数据库迁移后结构不完整：任务 role bundle 档案缺失")
    # Every frozen task revision must still be retrievable either as the role's
    # current row or from immutable history.  This is the no-cross-version
    # execution invariant and is checked before serving any request.
    if c.execute(
        "SELECT 1 FROM task t LEFT JOIN employee_role_config r ON "
        "r.identity_ref=t.employee_identity_ref "
        "AND r.config_revision=t.employee_config_revision "
        "AND r.config_sha256=t.employee_config_sha256 "
        "LEFT JOIN employee_role_config_history h ON "
        "h.identity_ref=t.employee_identity_ref "
        "AND h.config_revision=t.employee_config_revision "
        "AND h.config_sha256=t.employee_config_sha256 "
        "WHERE r.identity_ref IS NULL AND h.identity_ref IS NULL LIMIT 1"
    ).fetchone():
        raise RuntimeError("数据库迁移后结构不完整：任务冻结配置档案缺失")
    import_table_sql_row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='inspection_branch_import'"
    ).fetchone()
    import_table_sql = "".join(
        str(import_table_sql_row["sql"] or "").lower().split()
    ) if import_table_sql_row is not None else ""
    if "check(statusin('previewed','committed','expired'))" not in import_table_sql:
        # A pre-release schema-52 candidate only allowed previewed/committed.
        # CREATE TABLE IF NOT EXISTS cannot repair that CHECK safely, and silently
        # accepting it would make TTL cleanup fail only after serving traffic.
        raise RuntimeError(
            "数据库迁移后结构不完整：inspection_branch_import "
            "状态约束不完整"
        )
    indexes = {
        str(row["name"])
        for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    required_indexes = {
        "idx_purchase_intent_request",
        "idx_purchase_intent_owner_created",
        "idx_purchase_intent_admin_status",
        "idx_purchase_intent_subscription_op",
        "idx_task_thread_root",
        "idx_task_thread_revision",
        "idx_task_request_key",
        "idx_task_thread_one_active",
        "idx_task_dashboard_employee",
        "idx_tenant_industry_scope",
        "idx_inspection_visit_scope",
        "idx_inspection_visit_dashboard",
        "idx_inspection_issue_due",
        "idx_inspection_issue_visit",
        "idx_inspection_action_issue",
        "idx_store_branch_name",
        "idx_store_branch_code",
        "idx_inspection_branch_import_request",
        "idx_inspection_branch_import_source",
        "idx_inspection_branch_import_status_updated",
        "idx_inspection_branch_import_retention",
        "idx_inspection_branch_import_archive_hash",
        "idx_inspection_import_row",
        "idx_inspection_business_value_natural",
        "idx_inspection_business_value_period",
        "idx_inspection_standard_override_scope",
    }
    missing_indexes = sorted(required_indexes - indexes)
    if missing_indexes:
        raise RuntimeError(
            "数据库迁移后结构不完整：purchase_intent 缺少索引 "
            + ",".join(missing_indexes)
        )
    _require_index_contract(
        c,
        "store_branch",
        "idx_store_branch_name",
        ("tenant_id", "industry_key", "name"),
        unique=False,
        partial=False,
    )
    _require_index_contract(
        c,
        "store_branch",
        "idx_store_branch_code",
        ("tenant_id", "industry_key", "store_code"),
        unique=True,
        partial=True,
    )
    _require_index_contract(
        c,
        "inspection_branch_import",
        "idx_inspection_branch_import_status_updated",
        ("tenant_id", "industry_key", "status", "updated_at"),
        unique=False,
        partial=False,
    )
    _require_index_contract(
        c,
        "inspection_branch_import",
        "idx_inspection_branch_import_retention",
        ("updated_at", "tenant_id", "id"),
        unique=False,
        partial=True,
    )
    _require_index_contract(
        c,
        "inspection_branch_import_archive",
        "idx_inspection_branch_import_archive_hash",
        ("archive_sha256",),
        unique=True,
        partial=False,
    )
    _require_index_contract(
        c,
        "inspection_business_value",
        "idx_inspection_business_value_natural",
        (
            "tenant_id", "industry_key", "branch_id", "metric_key",
            "period_start", "period_end",
        ),
        unique=True,
        partial=False,
    )
    _require_unique_columns_contract(
        c,
        "inspection_standard_override",
        ("tenant_id", "industry_key", "scope_kind", "scope_key", "item_code"),
    )
    _require_index_contract(
        c,
        "inspection_standard_override",
        "idx_inspection_standard_override_scope",
        ("tenant_id", "industry_key", "active", "scope_kind", "scope_key"),
        unique=False,
        partial=False,
    )


def _initialize_anchor(expected_path=None):
    """Initialize one process anchor under the canonical OS migration lock."""
    global _conn
    if _conn is not None:
        return _conn
    path = _canonical_db_path(DB_PATH if expected_path is None else expected_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _migration_process_lock(path):
        # Another thread in this process may have completed initialization while
        # this caller waited for the cross-process lock.
        if _conn is not None:
            return _conn
        try:
            return _initialize_anchor_locked(path)
        except BaseException:
            # Python exceptions roll back here.  SIGKILL/os._exit closes the file
            # descriptor in the kernel; the still-open transaction is recovered
            # by SQLite before the next lock holder migrates.
            if _conn is not None:
                try:
                    if _conn.in_transaction:
                        _conn.rollback()
                except sqlite3.Error:
                    pass
                try:
                    _conn.close()
                except sqlite3.Error:
                    pass
                _all_connections.discard(_conn)
                _conn = None
            raise


def _initialize_anchor_locked(path: str):
    """Read, validate and migrate while caller holds the process lock."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        _all_connections.add(_conn)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA busy_timeout=30000")
        # Preview staging contains encrypted PII.  Retention UPDATE/DELETE must
        # overwrite freed cell payloads so a later SQLite backup cannot carry
        # still-decryptable ciphertext from page slack.
        _conn.execute("PRAGMA secure_delete=ON")
        try:
            found_version, ledger_version, _ = _existing_schema_version(_conn)
        except Exception:
            _conn.close()
            _all_connections.discard(_conn)
            _conn = None
            raise
        if found_version > LATEST_SCHEMA_VERSION:
            _conn.close()
            _all_connections.discard(_conn)
            _conn = None
            raise RuntimeError(
                f"数据库 schema v{found_version} 高于当前程序支持的 "
                f"v{LATEST_SCHEMA_VERSION}，已拒绝降级启动")
        try:
            _validate_existing_database(_conn, ledger_version)
        except Exception:
            _conn.close()
            _all_connections.discard(_conn)
            _conn = None
            raise
        _conn.execute("PRAGMA journal_mode=WAL")       # 读写不互斥,断电也只丢最后一笔
        _conn.execute("PRAGMA synchronous=NORMAL")
        # All schema/data changes and the final ledger/user_version stamp share
        # one transaction.  A crash therefore leaves the preceding version
        # intact; a waiting process re-reads that version after acquiring flock.
        _conn.execute("BEGIN IMMEDIATE")
        _execute_migration_script(_conn, SCHEMA)
        _add_column(_conn, "job", "billing_status", "TEXT NOT NULL DEFAULT 'charged'")
        _add_column(_conn, "job", "billing_points", "REAL")
        _add_column(_conn, "station_run", "steps_json", "TEXT")
        # v2 迁移:数字员工配置(可编辑提示词 + 全网进修技能库)
        _conn.execute("""CREATE TABLE IF NOT EXISTS employee_config(
          idx INTEGER PRIMARY KEY,
          prompt_template TEXT,
          skills_json TEXT NOT NULL DEFAULT '[]',
          learned_at REAL,
          created_at REAL, updated_at REAL
        )""")
        _add_column(_conn, "employee_config", "settings_json", "TEXT")
        _add_column(_conn, "employee_config", "caps_off_json", "TEXT")
        # v4 迁移:全局设置 / 知识沉淀库 / 定时任务
        _execute_migration_script(_conn, """
        CREATE TABLE IF NOT EXISTS app_setting(
          key TEXT PRIMARY KEY,
          value TEXT,
          updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS notification(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          job_id INTEGER,
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL DEFAULT '',
          link TEXT NOT NULL DEFAULT '#/',
          read_at REAL,
          created_at REAL,
          updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS knowledge(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          content TEXT NOT NULL DEFAULT '',
          tags_json TEXT NOT NULL DEFAULT '[]',
          source TEXT,                -- auto:job交付自动沉淀 / manual:老板手记
          job_id INTEGER,
          pinned INTEGER NOT NULL DEFAULT 0,
          deleted_at REAL, deleted_by INTEGER, delete_reason TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS task(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          emp_idx INTEGER NOT NULL,          -- 专家员工 idx(100+)
          employee_key TEXT NOT NULL DEFAULT '',
          employee_catalog_version TEXT NOT NULL DEFAULT '',
          employee_name_snapshot TEXT NOT NULL DEFAULT '',
          employee_dept_key TEXT NOT NULL DEFAULT '',
          employee_spec_sha256 TEXT NOT NULL DEFAULT '',
          brief_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',  -- queued/running/done/failed
          output_md TEXT,
          summary_md TEXT,
          steps_json TEXT,
          source_meeting_id INTEGER,
          source_action_key TEXT,
          source_task_id INTEGER,
          cost_usd REAL NOT NULL DEFAULT 0,
          tokens INTEGER NOT NULL DEFAULT 0,
          billing_status TEXT NOT NULL DEFAULT 'included',
          billing_points REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          deleted_at REAL, deleted_by INTEGER, delete_reason TEXT,
          created_at REAL, updated_at REAL, terminal_at REAL, refunded_at REAL
        );
        CREATE TABLE IF NOT EXISTS avatar_job(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          params_json TEXT NOT NULL,           -- photo_name/voice_id/script/prompt
          status TEXT NOT NULL DEFAULT 'queued',  -- queued/running/done/failed
          billing_status TEXT NOT NULL DEFAULT 'charged',
          billing_points REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          steps_json TEXT,
          audio_file TEXT, video_file TEXT, error TEXT,
          deleted_at REAL, deleted_by INTEGER, delete_reason TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS schedule(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          brief_json TEXT NOT NULL,
          mode TEXT NOT NULL DEFAULT 'copilot',
          profile_id INTEGER,
          kind TEXT NOT NULL DEFAULT 'daily',   -- daily / weekly / interval
          at_time TEXT,                          -- 'HH:MM' 北京时间(daily/weekly)
          weekday INTEGER,                       -- 0=周一 … 6=周日(weekly)
          every_hours INTEGER,                   -- interval:每 N 小时
          enabled INTEGER NOT NULL DEFAULT 1,
          last_run_at REAL, next_run_at REAL, last_note TEXT,
          claim_until REAL, claim_token TEXT,
          created_at REAL, updated_at REAL
        );
        """)
        # 旧版“访问口令”从未参与真实登录鉴权，却以明文保存。升级时删除该
        # 失效界面遗留值，统一只保留账号密码 + 会话认证。
        _conn.execute("DELETE FROM app_setting WHERE key='boss_pin'")
        _add_column(_conn, "task", "billing_status", "TEXT NOT NULL DEFAULT 'included'")
        _add_column(_conn, "task", "billing_points", "REAL")
        _add_column(_conn, "avatar_job", "billing_status",
                    "TEXT NOT NULL DEFAULT 'charged'")
        _add_column(_conn, "avatar_job", "billing_points", "REAL")
        _add_column(_conn, "employee_config", "enabled", "INTEGER NOT NULL DEFAULT 1")
        _add_column(_conn, "schedule", "claim_until", "REAL")
        # 连续非计费失败要累计,静默 10 分钟重试一天 144 次老板却毫不知情
        _add_column(_conn, "schedule", "fail_streak",
                    "INTEGER NOT NULL DEFAULT 0")
        # 通知定向:user_id 为空=租户广播;read_by 存广播的按人已读集合
        # (逗号包裹串,如 ",5,20,",instr 即可判含),财务类通知只发企业主。
        _add_column(_conn, "notification", "user_id", "INTEGER")
        _add_column(_conn, "notification", "read_by", "TEXT")
        _add_column(_conn, "schedule", "claim_token", "TEXT")
        for col in ("model_text", "model_image"):  # v6 迁移:员工级模型路由
            _add_column(_conn, "employee_config", col, "TEXT")
        for tbl in ("knowledge", "asset"):  # v5 迁移:多维评估打标
            _add_column(_conn, tbl, "meta_json", "TEXT")
        # v8 迁移:多租户(企业/账号/数据隔离)
        _execute_migration_script(_conn, """
        CREATE TABLE IF NOT EXISTS tenants(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'member',   -- root/owner/member
          modules_json TEXT NOT NULL DEFAULT '[]',
          job_title TEXT NOT NULL DEFAULT 'staff',  -- member职级:director/manager/staff
          allowed_emp_idxs_json TEXT,           -- NULL=行业内全部;JSON数组=数字员工白名单
          enabled INTEGER NOT NULL DEFAULT 1,
          must_change_password INTEGER NOT NULL DEFAULT 0,
          created_at REAL, updated_at REAL
        );
        """)
        had_password_policy = _column_exists(
            _conn, "users", "must_change_password")
        _add_column(
            _conn,
            "users",
            "must_change_password",
            "INTEGER NOT NULL DEFAULT 0",
        )
        if not had_password_policy:
            # 哈希无法证明旧明文是否达到新策略。升级时对全部存量账号强制一次
            # 自助改密；新库/升级后新建账号按写入口显式决定是否需要首登改密。
            _conn.execute(
                "UPDATE users SET must_change_password=1 WHERE enabled=1"
            )
        for tbl in ("job", "task", "knowledge", "asset", "avatar_job", "schedule",
                    "account_profile"):
            _add_column(_conn, tbl, "tenant_id", "INTEGER NOT NULL DEFAULT 1")
        # v8.1 迁移:计费(点数钱包/套餐/流水)
        for col, typ in (("balance", "REAL NOT NULL DEFAULT 0"), ("plan", "TEXT"),
                         ("plan_expires", "REAL"), ("industries_json", "TEXT")):
            _add_column(_conn, "tenants", col, typ)
        _execute_migration_script(_conn, """
        CREATE TABLE IF NOT EXISTS meeting(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL DEFAULT 1,
          question TEXT NOT NULL,
          constraints TEXT,
          acceptance_criteria TEXT,
          emp_idxs_json TEXT NOT NULL,
          member_snapshot_json TEXT NOT NULL DEFAULT '[]',
          messages_json TEXT NOT NULL DEFAULT '[]',
          actions_json TEXT,
          summary_md TEXT,
          status TEXT NOT NULL DEFAULT 'queued',
          phase TEXT NOT NULL DEFAULT 'queued',
          round_no INTEGER NOT NULL DEFAULT 0,
          decision TEXT,
          consensus_md TEXT,
          next_action TEXT,
          proposals_json TEXT NOT NULL DEFAULT '[]',
          validations_json TEXT NOT NULL DEFAULT '[]',
          execution_task_ids_json TEXT NOT NULL DEFAULT '[]',
          auto_execute INTEGER NOT NULL DEFAULT 1,
          team_execute INTEGER NOT NULL DEFAULT 0,
          intervention_count INTEGER NOT NULL DEFAULT 0,
          intervention_state TEXT,
          intervention_op_key TEXT,
          intervention_snapshot_json TEXT,
          intervention_question TEXT,
          intervention_started_at REAL,
          billing_status TEXT NOT NULL DEFAULT 'included',
          billing_points REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS guests(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          phone TEXT NOT NULL, company TEXT, name TEXT,
          used INTEGER NOT NULL DEFAULT 0,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS account_apply(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          phone TEXT NOT NULL, name TEXT, company TEXT, note TEXT,
          status INTEGER NOT NULL DEFAULT 0,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS billing_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          job_id INTEGER,
          delta REAL NOT NULL, balance REAL NOT NULL,
          reason TEXT, created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS billing_operation(
          op_key TEXT PRIMARY KEY,
          tenant_id INTEGER NOT NULL,
          job_id INTEGER,
          action TEXT NOT NULL,
          units INTEGER NOT NULL DEFAULT 1,
          points REAL NOT NULL DEFAULT 0,
          note TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          error TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_billing_operation_status
          ON billing_operation(status, created_at);
        CREATE TABLE IF NOT EXISTS purchase_intent(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          created_by INTEGER NOT NULL,
          request_key TEXT NOT NULL,
          plan_key TEXT NOT NULL,
          period_key TEXT NOT NULL,
          plan_name TEXT NOT NULL,
          period_label TEXT NOT NULL,
          quoted_price REAL NOT NULL,
          quoted_points REAL NOT NULL,
          contact TEXT NOT NULL,
          customer_note TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'requested'
            CHECK(status IN ('requested','contacted','lost','paid')),
          handler_note TEXT,
          handled_by INTEGER,
          contacted_at REAL,
          lost_at REAL,
          paid_at REAL,
          subscription_op_key TEXT,
          receipt_json TEXT,
          created_at REAL,
          updated_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_intent_request
          ON purchase_intent(tenant_id,request_key);
        CREATE INDEX IF NOT EXISTS idx_purchase_intent_owner_created
          ON purchase_intent(tenant_id,created_by,id DESC);
        CREATE INDEX IF NOT EXISTS idx_purchase_intent_admin_status
          ON purchase_intent(status,tenant_id,id DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_intent_subscription_op
          ON purchase_intent(subscription_op_key)
          WHERE subscription_op_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS wechat_draft_delivery(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          job_id INTEGER NOT NULL,
          request_hash TEXT NOT NULL,
          request_key TEXT NOT NULL,
          title TEXT,
          status TEXT NOT NULL DEFAULT 'pending_charge',
          billing_status TEXT NOT NULL DEFAULT 'pending',
          billing_points REAL,
          op_key TEXT,
          media_id TEXT,
          publish_log_id INTEGER,
          report_json TEXT,
          error TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_wechat_delivery_request
          ON wechat_draft_delivery(tenant_id,job_id,request_hash,id DESC);
        CREATE INDEX IF NOT EXISTS idx_wechat_delivery_active_lookup
          ON wechat_draft_delivery(tenant_id,job_id,status);
        CREATE TABLE IF NOT EXISTS client_error(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          user_id INTEGER,
          route TEXT,
          kind TEXT,
          message TEXT,
          user_agent TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_client_error_tenant_created
          ON client_error(tenant_id, id DESC);
        CREATE TABLE IF NOT EXISTS funnel_event(
          day TEXT NOT NULL,
          event TEXT NOT NULL,
          dimension TEXT NOT NULL,
          tenant_id INTEGER NOT NULL DEFAULT 0,
          actor_hash TEXT NOT NULL,
          hits INTEGER NOT NULL DEFAULT 1,
          first_at REAL NOT NULL,
          last_at REAL NOT NULL,
          PRIMARY KEY(day,event,dimension,tenant_id,actor_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_funnel_event_day_event
          ON funnel_event(day,event);
        CREATE INDEX IF NOT EXISTS idx_funnel_event_tenant_day
          ON funnel_event(tenant_id,day,event);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_funnel_first_work
          ON funnel_event(event,tenant_id,actor_hash)
          WHERE event='first_job_submitted';
        CREATE TABLE IF NOT EXISTS censor_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          job_id INTEGER,
          kind TEXT NOT NULL DEFAULT 'pre',      -- pre:发前审查 / retro:发后复盘
          platform TEXT, title TEXT,
          verdict TEXT, score REAL,
          issues_json TEXT, report TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS tv_job(       -- V25:图文转视频
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          job_id INTEGER,                        -- 关联内容工单(可空=独立成片)
          params_json TEXT NOT NULL,             -- title/body/script/images/voice_id/image_query
          script TEXT,
          status TEXT NOT NULL DEFAULT 'queued', -- pending_charge/queued/running/done/failed/cancelled
          billing_status TEXT NOT NULL DEFAULT 'charged',
          billing_points REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          steps_json TEXT, video_file TEXT, error TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS publish_log(  -- V25:发布台账(自动复盘的钟表)
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          platform TEXT, title TEXT,
          job_id INTEGER, url TEXT,
          source TEXT DEFAULT 'manual',          -- draft:草稿箱推送 / manual:手动登记
          published_at REAL,
          retro_json TEXT,                       -- {"1":{"due":ts,"state":"pending/notified/done"},"3":...,"7":...}
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS tool_job(     -- V25.4:工具箱后台作业(挂起可回看)
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          kind TEXT NOT NULL,                    -- hot/pcal/warm/leads/bench
          params_json TEXT,
          status TEXT NOT NULL DEFAULT 'running',-- pending_charge/running/done/failed
          billing_status TEXT NOT NULL DEFAULT 'charged',
          billing_points REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          retry_started_at REAL,
          result_json TEXT, error TEXT, progress TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS pub_task(     -- V25:矩阵真发布队列(beta)
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          platform TEXT NOT NULL, account TEXT,
          payload_json TEXT NOT NULL,            -- title/body/images/video
          status TEXT NOT NULL DEFAULT 'queued', -- queued/running/done/failed
          retry_count INTEGER NOT NULL DEFAULT 0,
          submission_state TEXT NOT NULL DEFAULT 'not_submitted',
          submit_started_at REAL,
          log TEXT, created_at REAL, updated_at REAL
        );
        """)
        _add_column(_conn, "meeting", "actions_json", "TEXT")
        # 旧预览版索引按 request_hash 隔离，会允许同一工单换主题后并发双投。
        _conn.execute("DROP INDEX IF EXISTS idx_wechat_delivery_active")
        delivery_conflicts = list(_conn.execute(
            "SELECT tenant_id,job_id,COUNT(*) AS n "
            "FROM wechat_draft_delivery "
            "WHERE status IN "
            "('pending_charge','processing','submitting','submitted') "
            "GROUP BY tenant_id,job_id HAVING COUNT(*)>1"
        ))
        if not delivery_conflicts:
            _conn.execute(
                "CREATE UNIQUE INDEX idx_wechat_delivery_active "
                "ON wechat_draft_delivery(tenant_id,job_id) "
                "WHERE status IN "
                "('pending_charge','processing','submitting','submitted')"
            )
            _conn.execute(
                "DELETE FROM app_setting "
                "WHERE key='wechat_delivery_migration_conflicts'"
            )
        else:
            # 旧版本可能已把同一工单投递多次；submitting/submitted 不可盲退。
            # 保留全部锚点让服务可启动，并记录待人工对账清单；新 claim 仍会阻断再扣。
            payload = [
                {
                    "tenant_id": int(row["tenant_id"]),
                    "job_id": int(row["job_id"]),
                    "count": int(row["n"]),
                }
                for row in delivery_conflicts
            ]
            _conn.execute(
                "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                "updated_at=excluded.updated_at",
                (
                    "wechat_delivery_migration_conflicts",
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                ),
            )
        _add_column(_conn, "meeting", "billing_status", "TEXT NOT NULL DEFAULT 'included'")
        _add_column(_conn, "meeting", "billing_points", "REAL")
        for col in ("tenant_id INTEGER", "username TEXT"):  # v25.5:申请一键开通,记录开的账号
            name, declaration = col.split(" ", 1)
            _add_column(_conn, "account_apply", name, declaration)
        _add_column(_conn, "tool_job", "progress", "TEXT")
        _add_column(_conn, "tool_job", "billing_status",
                    "TEXT NOT NULL DEFAULT 'charged'")
        _add_column(_conn, "tool_job", "billing_points", "REAL")
        # 旧版图文转视频工单在升级前已经按旧逻辑结算。只在首次增加列时
        # 回填历史终态，避免历史失败工单因服务重启再次退款。
        had_tv_billing = _column_exists(_conn, "tv_job", "billing_status")
        _add_column(_conn, "tv_job", "billing_status",
                    "TEXT NOT NULL DEFAULT 'charged'")
        _add_column(_conn, "tv_job", "billing_points", "REAL")
        if not had_tv_billing:
            _conn.execute(
                "UPDATE tv_job SET billing_status='succeeded' "
                "WHERE status='done'"
            )
            _conn.execute(
                "UPDATE tv_job SET billing_status='refunded' "
                "WHERE status IN ('failed','cancelled')"
            )
        _add_column(_conn, "task", "summary_md", "TEXT")
        _add_column(_conn, "meeting", "summary_md", "TEXT")
        _add_column(_conn, "pub_task", "fail_json", "TEXT")
        had_pub_submission_state = _column_exists(
            _conn, "pub_task", "submission_state")
        _add_column(
            _conn, "pub_task", "submission_state",
            "TEXT NOT NULL DEFAULT 'legacy_unknown'",
        )
        _add_column(_conn, "pub_task", "submit_started_at", "REAL")
        if not had_pub_submission_state:
            # 老版本没有“点击发布前”的持久锚点，历史运行/失败记录无法证明
            # 平台是否已经收到提交。全部按不确定处理，禁止升级后盲目重发。
            _conn.execute(
                "UPDATE pub_task SET submission_state='submitted' "
                "WHERE status='done'"
            )
            _conn.execute(
                "UPDATE pub_task SET submission_state='not_submitted' "
                "WHERE status='queued'"
            )
        # v28:结果型 AI 会议。把开放式群聊收紧为「提案→验证→执行」状态机，
        # 共识与每轮结构化产出直接留在 SQLite，服务重启后也能继续交接。
        for col, typ in (
                ("phase", "TEXT NOT NULL DEFAULT 'queued'"),
                ("constraints", "TEXT"),
                ("acceptance_criteria", "TEXT"),
                ("round_no", "INTEGER NOT NULL DEFAULT 0"),
                ("decision", "TEXT"),
                ("consensus_md", "TEXT"),
                ("next_action", "TEXT"),
                ("proposals_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("validations_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("execution_task_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("auto_execute", "INTEGER NOT NULL DEFAULT 1"),
                ("team_execute", "INTEGER NOT NULL DEFAULT 0"),
                ("intervention_count", "INTEGER NOT NULL DEFAULT 0"),
                ("intervention_state", "TEXT"),
                ("intervention_op_key", "TEXT"),
                ("intervention_snapshot_json", "TEXT"),
                ("intervention_question", "TEXT"),
                ("intervention_started_at", "REAL"),
        ):
            _add_column(_conn, "meeting", col, typ)
        # 自动执行任务必须可幂等恢复：同一会议同一个行动键只允许生成一次任务。
        for col, typ in (("source_meeting_id", "INTEGER"),
                         ("source_action_key", "TEXT"),
                         ("source_task_id", "INTEGER")):
            _add_column(_conn, "task", col, typ)
        # v42:核心业务对象进入可恢复回收站。删除只隐藏/终止，不再摧毁计费锚点
        # 或交付文件；重试次数也持久化，避免失败重试被并发提交多次。
        # (人设档案与资产库后续也纳入同一回收站,老板误删可自行找回。)
        for table in ("job", "task", "knowledge", "avatar_job",
                      "account_profile", "asset"):
            for col, typ in (
                ("deleted_at", "REAL"),
                ("deleted_by", "INTEGER"),
                ("delete_reason", "TEXT"),
            ):
                _add_column(_conn, table, col, typ)
        for table in (
                "job", "task", "meeting", "avatar_job", "tv_job",
                "tool_job", "pub_task"):
            _add_column(_conn, table, "retry_count",
                        "INTEGER NOT NULL DEFAULT 0")
        _add_column(_conn, "tool_job", "retry_started_at", "REAL")
        _conn.execute("DROP INDEX IF EXISTS idx_task_meeting_action")
        _conn.execute("CREATE UNIQUE INDEX idx_task_meeting_action "
                      "ON task(source_meeting_id, source_action_key) "
                      "WHERE source_meeting_id IS NOT NULL "
                      "AND source_action_key IS NOT NULL AND deleted_at IS NULL")
        _add_column(_conn, "job", "source_schedule_id", "INTEGER")
        _add_column(_conn, "job", "source_schedule_occurrence", "TEXT")
        _conn.execute("DROP INDEX IF EXISTS idx_job_schedule_occurrence")
        _conn.execute(
            "CREATE UNIQUE INDEX idx_job_schedule_occurrence "
            "ON job(source_schedule_id, source_schedule_occurrence) "
            "WHERE source_schedule_id IS NOT NULL "
            "AND source_schedule_occurrence IS NOT NULL AND deleted_at IS NULL"
        )
        # 任务中心的高频读取索引：按租户看最新、按状态筛选、按员工追踪。
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_task_tenant_created "
                      "ON task(tenant_id, id DESC)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_task_tenant_status_created "
                      "ON task(tenant_id, status, id DESC)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_task_tenant_emp_created "
                      "ON task(tenant_id, emp_idx, id DESC)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_job_tenant_created "
                      "ON job(tenant_id, id DESC)")
        for table in ("job", "task", "knowledge", "avatar_job",
                      "account_profile", "asset"):
            _conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant_deleted "
                f"ON {table}(tenant_id, deleted_at, id DESC)"
            )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_tenant_unread "
            "ON notification(tenant_id, read_at, id DESC)"
        )
        # 副账号协作可见:工单/任务记录发起人,工位记录拍板人。老板事后可追溯
        # "这单是哪个副账号开的、这一站是谁批的";历史数据没有操作人,留空即可。
        _add_column(_conn, "job", "created_by", "INTEGER")
        _add_column(_conn, "task", "created_by", "INTEGER")
        # 协作可见补齐:数字人/会议/成片/工具单也记发起人
        for tbl in ("avatar_job", "meeting", "tv_job", "tool_job"):
            _add_column(_conn, tbl, "created_by", "INTEGER")
        _add_column(_conn, "station_run", "reviewed_by", "INTEGER")
        # v50:工单派生的通知、审查与账务记录必须显式归因。历史记录留空，
        # 硬删时只接受显式 job_id 或可证明的旧版锚点，绝不再按标题猜关联。
        for table in (
                "notification", "censor_log", "billing_log",
                "billing_operation"):
            _add_column(_conn, table, "job_id", "INTEGER")
            _conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant_job "
                f"ON {table}(tenant_id, job_id)"
            )
        # v51:员工持续协作、巡店整改闭环与行业老板看板。每轮员工任务仍
        # 使用 task 作为不可变交付；task_thread 只保存当前指针和满意状态，
        # 避免复制长正文或另造一套执行/计费体系。
        had_task_terminal_at = _column_exists(_conn, "task", "terminal_at")
        had_task_refunded_at = _column_exists(_conn, "task", "refunded_at")
        for col, typ in (
                ("thread_id", "INTEGER"),
                ("revision_no", "INTEGER NOT NULL DEFAULT 1"),
                ("phase", "TEXT NOT NULL DEFAULT 'delivery'"),
                ("request_key", "TEXT"),
                ("terminal_at", "REAL"),
                ("refunded_at", "REAL"),
        ):
            _add_column(_conn, "task", col, typ)
        if not had_task_terminal_at:
            # schema51 尚未发布：老 done/failed 的 updated_at 就是当时唯一
            # 可用的终态时钟。只在列首次加入时回填，后续收养会话、
            # 手工编辑、删除/恢复即使改写 updated_at，也不会改变归期。
            _conn.execute(
                "UPDATE task SET terminal_at=updated_at "
                "WHERE status IN ('done','failed') AND terminal_at IS NULL"
            )
        if not had_task_refunded_at:
            # 旧库没有独立退款时钟，只在列首次加入时用当时
            # updated_at 回填；之后的任务编辑或回收站操作不会改写它。
            _conn.execute(
                "UPDATE task SET refunded_at=updated_at "
                "WHERE billing_status='refunded' AND refunded_at IS NULL"
            )
        _execute_migration_script(_conn, """
        CREATE TABLE IF NOT EXISTS task_thread(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          emp_idx INTEGER NOT NULL,
          employee_key TEXT NOT NULL DEFAULT '',
          employee_catalog_version TEXT NOT NULL DEFAULT '',
          root_task_id INTEGER NOT NULL,
          current_task_id INTEGER NOT NULL,
          accepted_task_id INTEGER,
          status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','satisfied')),
          revision_count INTEGER NOT NULL DEFAULT 1,
          created_by INTEGER,
          satisfied_at REAL,
          created_at REAL, updated_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_thread_root
          ON task_thread(tenant_id,root_task_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_thread_revision
          ON task(thread_id,revision_no) WHERE thread_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_request_key
          ON task(tenant_id,request_key) WHERE request_key IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_thread_one_active
          ON task(thread_id) WHERE thread_id IS NOT NULL
          AND deleted_at IS NULL
          AND status IN ('pending_charge','queued','running');
        CREATE INDEX IF NOT EXISTS idx_task_dashboard_employee
          ON task(tenant_id,emp_idx,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_task_dashboard_terminal
          ON task(tenant_id,terminal_at DESC,emp_idx);
        CREATE INDEX IF NOT EXISTS idx_task_dashboard_refunded
          ON task(tenant_id,refunded_at DESC,emp_idx);
        CREATE INDEX IF NOT EXISTS idx_task_thread_current
          ON task_thread(tenant_id,current_task_id);

        CREATE TABLE IF NOT EXISTS tenant_industry(
          tenant_id INTEGER NOT NULL,
          industry_key TEXT NOT NULL,
          is_primary INTEGER NOT NULL DEFAULT 0,
          created_at REAL,
          PRIMARY KEY(tenant_id,industry_key)
        );
        CREATE INDEX IF NOT EXISTS idx_tenant_industry_scope
          ON tenant_industry(industry_key,tenant_id);

        CREATE TABLE IF NOT EXISTS store_branch(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          industry_key TEXT NOT NULL,
          name TEXT NOT NULL,
          region TEXT NOT NULL DEFAULT '',
          address TEXT NOT NULL DEFAULT '',
          active INTEGER NOT NULL DEFAULT 1,
          created_by INTEGER,
          created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_store_branch_name
          ON store_branch(tenant_id,industry_key,name);

        CREATE TABLE IF NOT EXISTS inspection_visit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          industry_key TEXT NOT NULL,
          branch_id INTEGER NOT NULL,
          employee_idx INTEGER NOT NULL DEFAULT 10,
          task_id INTEGER UNIQUE,
          request_key TEXT,
          visit_at REAL,
          status TEXT NOT NULL DEFAULT 'draft',
          score REAL,
          summary_md TEXT,
          model_json TEXT,
          version INTEGER NOT NULL DEFAULT 1,
          created_by INTEGER,
          completed_at REAL,
          terminal_at REAL,
          deleted_at REAL, deleted_by INTEGER, delete_reason TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inspection_request
          ON inspection_visit(tenant_id,request_key)
          WHERE request_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_inspection_visit_scope
          ON inspection_visit(tenant_id,industry_key,branch_id,visit_at DESC);
        CREATE INDEX IF NOT EXISTS idx_inspection_visit_dashboard
          ON inspection_visit(tenant_id,industry_key,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_inspection_visit_status
          ON inspection_visit(tenant_id,status,updated_at DESC);

        CREATE TABLE IF NOT EXISTS inspection_photo(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          visit_id INTEGER NOT NULL,
          recheck_id INTEGER,
          phase TEXT NOT NULL DEFAULT 'before'
            CHECK(phase IN ('before','recheck')),
          storage_key TEXT NOT NULL,
          mime_type TEXT NOT NULL,
          byte_size INTEGER NOT NULL,
          sha256 TEXT NOT NULL,
          width INTEGER,
          height INTEGER,
          caption TEXT NOT NULL DEFAULT '',
          created_by INTEGER,
          created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_inspection_photo_visit
          ON inspection_photo(tenant_id,visit_id,id);

        CREATE TABLE IF NOT EXISTS inspection_issue(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          visit_id INTEGER NOT NULL,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          visible_observation TEXT NOT NULL DEFAULT '',
          severity TEXT NOT NULL
            CHECK(severity IN ('critical','high','medium','low')),
          category TEXT NOT NULL DEFAULT '其他',
          confidence REAL,
          needs_human_check INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'detected'
            CHECK(status IN ('detected','confirmed','rectifying',
              'awaiting_recheck','closed','reopened')),
          owner TEXT NOT NULL DEFAULT '',
          due_at REAL,
          root_cause TEXT NOT NULL DEFAULT '',
          closure_evidence TEXT NOT NULL DEFAULT '',
          verified_by INTEGER,
          verified_at REAL,
          recurrence_of_issue_id INTEGER,
          created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_inspection_issue_due
          ON inspection_issue(tenant_id,status,due_at,severity);
        CREATE INDEX IF NOT EXISTS idx_inspection_issue_visit
          ON inspection_issue(tenant_id,visit_id,status,due_at,severity);

        CREATE TABLE IF NOT EXISTS inspection_evidence(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          visit_id INTEGER NOT NULL,
          issue_id INTEGER NOT NULL,
          photo_id INTEGER NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          bbox_json TEXT,
          created_at REAL,
          UNIQUE(issue_id,photo_id)
        );

        CREATE TABLE IF NOT EXISTS inspection_action(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          visit_id INTEGER NOT NULL,
          issue_id INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'open'
            CHECK(status IN ('open','in_progress','awaiting_recheck',
              'closed','reopened')),
          plan TEXT NOT NULL,
          temporary_control TEXT NOT NULL DEFAULT '',
          owner TEXT NOT NULL DEFAULT '',
          due_at REAL,
          version INTEGER NOT NULL DEFAULT 1,
          completion_note TEXT NOT NULL DEFAULT '',
          closed_by INTEGER,
          closed_at REAL,
          created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_inspection_action_issue
          ON inspection_action(tenant_id,issue_id,status,due_at);

        CREATE TABLE IF NOT EXISTS inspection_recheck(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          visit_id INTEGER NOT NULL,
          issue_id INTEGER NOT NULL,
          action_id INTEGER NOT NULL,
          task_id INTEGER,
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','improved','insufficient','still_visible',
              'approved','rejected')),
          note TEXT NOT NULL DEFAULT '',
          model_recommendation TEXT NOT NULL DEFAULT '',
          created_by INTEGER,
          reviewed_by INTEGER,
          reviewed_at REAL,
          created_at REAL
        );

        CREATE TABLE IF NOT EXISTS inspection_event(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          visit_id INTEGER NOT NULL,
          issue_id INTEGER,
          action_id INTEGER,
          kind TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_by INTEGER,
          created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_inspection_event_visit
          ON inspection_event(tenant_id,visit_id,created_at,id);
        """)
        had_inspection_terminal_at = _column_exists(
            _conn, "inspection_visit", "terminal_at"
        )
        _add_column(_conn, "inspection_visit", "terminal_at", "REAL")
        if not had_inspection_terminal_at:
            _conn.execute(
                "UPDATE inspection_visit SET terminal_at="
                "CASE WHEN status='completed' "
                "THEN COALESCE(completed_at,updated_at) ELSE updated_at END "
                "WHERE status IN ('completed','failed') AND terminal_at IS NULL"
            )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inspection_visit_terminal "
            "ON inspection_visit(tenant_id,industry_key,terminal_at DESC,employee_idx)"
        )
        # v52:门店主数据与可审计的 XLSX 两阶段导入。旧门店没有编号，
        # store_code 必须可空；只对新主数据的非空编号施加租户+行业唯一约束。
        for col, typ in (
                ("template_key", "TEXT"),
                ("template_version", "TEXT"),
                ("template_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("observations_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            _add_column(_conn, "inspection_visit", col, typ)
        for col, typ in (
                ("capture_slot", "TEXT NOT NULL DEFAULT ''"),
                ("item_code", "TEXT NOT NULL DEFAULT ''"),
        ):
            _add_column(_conn, "inspection_photo", col, typ)
        for col, typ in (
                ("store_code", "TEXT"),
                ("province", "TEXT NOT NULL DEFAULT ''"),
                ("city", "TEXT NOT NULL DEFAULT ''"),
                ("district", "TEXT NOT NULL DEFAULT ''"),
                ("manager_name", "TEXT NOT NULL DEFAULT ''"),
                ("manager_employee_no", "TEXT NOT NULL DEFAULT ''"),
                ("manager_phone", "TEXT NOT NULL DEFAULT ''"),
                ("store_type", "TEXT NOT NULL DEFAULT ''"),
                ("opened_on", "TEXT"),
                ("area_sqm", "REAL"),
                ("seat_count", "INTEGER"),
                ("longitude", "REAL"),
                ("latitude", "REAL"),
                ("remark", "TEXT NOT NULL DEFAULT ''"),
                ("row_version", "INTEGER NOT NULL DEFAULT 1"),
        ):
            _add_column(_conn, "store_branch", col, typ)
        # schema51 used the branch name as a unique key.  Store codes are the
        # stable identity in schema52; safely rewrite only the exact legacy
        # index and leave any unknown same-name structure for validation to
        # reject instead of silently dropping it.
        legacy_name_index = next((
            row for row in _conn.execute("PRAGMA index_list(store_branch)")
            if str(row["name"]) == "idx_store_branch_name"
        ), None)
        legacy_name_columns = tuple(
            str(row["name"])
            for row in _conn.execute("PRAGMA index_info(idx_store_branch_name)")
        ) if legacy_name_index is not None else ()
        if (
            legacy_name_index is not None
            and bool(legacy_name_index["unique"])
            and not bool(legacy_name_index["partial"])
            and legacy_name_columns == ("tenant_id", "industry_key", "name")
        ):
            _conn.execute("DROP INDEX idx_store_branch_name")
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_store_branch_name "
            "ON store_branch(tenant_id,industry_key,name)"
        )
        if found_version < 52:
            # v52 发布前的候选库曾把 source_ref 误放进自然键；
            # 只在升级边界重建，不让每次最新版启动都产生 DDL 写。
            _conn.execute(
                "DROP INDEX IF EXISTS idx_inspection_business_value_natural"
            )
        # Retention indexes below refer to this v52 lifecycle field.  Pre-release
        # schema52 databases may already have the table but not the column, and
        # CREATE TABLE IF NOT EXISTS cannot add it.  Add it before any index DDL.
        if _table_sql(_conn, "inspection_branch_import"):
            _add_column(
                _conn,
                "inspection_branch_import",
                "staging_purged_at",
                "REAL",
            )
            if found_version < 53:
                _upgrade_inspection_import_status_check(_conn)
        _execute_migration_script(_conn, """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_store_branch_code
          ON store_branch(tenant_id,industry_key,store_code)
          WHERE store_code IS NOT NULL AND trim(store_code)<>'';

        CREATE TABLE IF NOT EXISTS inspection_branch_import(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          industry_key TEXT NOT NULL,
          request_key TEXT NOT NULL,
          source_sha256 TEXT NOT NULL,
          filename TEXT NOT NULL,
          catalog_version TEXT NOT NULL DEFAULT '',
          catalog_sha256 TEXT NOT NULL DEFAULT '',
          business_values_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'previewed'
            CHECK(status IN ('previewed','committed','expired')),
          total_rows INTEGER NOT NULL DEFAULT 0,
          create_count INTEGER NOT NULL DEFAULT 0,
          update_count INTEGER NOT NULL DEFAULT 0,
          skip_count INTEGER NOT NULL DEFAULT 0,
          error_count INTEGER NOT NULL DEFAULT 0,
          business_create_count INTEGER NOT NULL DEFAULT 0,
          business_update_count INTEGER NOT NULL DEFAULT 0,
          business_skip_count INTEGER NOT NULL DEFAULT 0,
          business_error_count INTEGER NOT NULL DEFAULT 0,
          created_by INTEGER NOT NULL,
          committed_by INTEGER,
          committed_at REAL,
          staging_purged_at REAL,
          created_at REAL, updated_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inspection_branch_import_request
          ON inspection_branch_import(tenant_id,industry_key,request_key);
        CREATE INDEX IF NOT EXISTS idx_inspection_branch_import_source
          ON inspection_branch_import(tenant_id,industry_key,source_sha256);
        CREATE INDEX IF NOT EXISTS idx_inspection_branch_import_status_updated
          ON inspection_branch_import(
            tenant_id,industry_key,status,updated_at
          );
        CREATE INDEX IF NOT EXISTS idx_inspection_branch_import_retention
          ON inspection_branch_import(updated_at,tenant_id,id)
          WHERE staging_purged_at IS NULL
            AND status IN ('previewed','expired');

        -- Compressed content-addressed audit snapshots replace committed
        -- import-row working copies.  The authoritative branch/business/visit
        -- tables remain untouched by retention cleanup.
        CREATE TABLE IF NOT EXISTS inspection_branch_import_archive(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          archive_sha256 TEXT NOT NULL,
          payload_zlib BLOB NOT NULL,
          uncompressed_bytes INTEGER NOT NULL,
          row_count INTEGER NOT NULL,
          created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inspection_branch_import_archive_hash
          ON inspection_branch_import_archive(archive_sha256);

        CREATE TABLE IF NOT EXISTS inspection_branch_import_row(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          import_id INTEGER NOT NULL,
          tenant_id INTEGER NOT NULL,
          row_number INTEGER NOT NULL,
          store_code TEXT,
          action TEXT NOT NULL
            CHECK(action IN ('create','update','skip','error')),
          error_code TEXT,
          error_message TEXT,
          payload_json TEXT NOT NULL,
          masked_payload_json TEXT NOT NULL,
          existing_branch_id INTEGER NOT NULL DEFAULT 0,
          existing_row_version INTEGER NOT NULL DEFAULT 0,
          existing_business_value_id INTEGER NOT NULL DEFAULT 0,
          existing_business_row_version INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,
          UNIQUE(import_id,row_number)
        );
        CREATE INDEX IF NOT EXISTS idx_inspection_import_row
          ON inspection_branch_import_row(tenant_id,import_id,row_number);

        CREATE TABLE IF NOT EXISTS inspection_business_value(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          industry_key TEXT NOT NULL,
          branch_id INTEGER NOT NULL,
          import_id INTEGER NOT NULL,
          metric_key TEXT NOT NULL,
          period_start TEXT NOT NULL,
          period_end TEXT NOT NULL,
          value REAL NOT NULL,
          unit TEXT NOT NULL DEFAULT '',
          source_ref TEXT NOT NULL DEFAULT '',
          remark TEXT NOT NULL DEFAULT '',
          row_version INTEGER NOT NULL DEFAULT 1,
          created_at REAL, updated_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inspection_business_value_natural
          ON inspection_business_value(
            tenant_id,industry_key,branch_id,metric_key,
            period_start,period_end
          );
        CREATE INDEX IF NOT EXISTS idx_inspection_business_value_period
          ON inspection_business_value(
            tenant_id,industry_key,metric_key,period_start,period_end
          );

        CREATE TABLE IF NOT EXISTS inspection_standard_override(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          industry_key TEXT NOT NULL,
          scope_kind TEXT NOT NULL
            CHECK(scope_kind IN ('tenant','region','branch')),
          scope_key TEXT NOT NULL DEFAULT '',
          item_code TEXT NOT NULL,
          patch_json TEXT NOT NULL,
          row_version INTEGER NOT NULL DEFAULT 1,
          active INTEGER NOT NULL DEFAULT 1,
          created_by INTEGER NOT NULL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          UNIQUE(tenant_id,industry_key,scope_kind,scope_key,item_code)
        );
        CREATE INDEX IF NOT EXISTS idx_inspection_standard_override_scope
          ON inspection_standard_override(
            tenant_id,industry_key,active,scope_kind,scope_key
          );
        """)
        _add_column(
            _conn, "inspection_branch_import", "business_values_json",
            "TEXT NOT NULL DEFAULT '[]'",
        )
        _add_column(
            _conn, "inspection_branch_import", "catalog_version",
            "TEXT NOT NULL DEFAULT ''",
        )
        _add_column(
            _conn, "inspection_branch_import", "catalog_sha256",
            "TEXT NOT NULL DEFAULT ''",
        )
        for col in (
            "business_create_count", "business_update_count",
            "business_skip_count", "business_error_count",
        ):
            _add_column(
                _conn, "inspection_branch_import", col,
                "INTEGER NOT NULL DEFAULT 0",
            )
        _add_column(
            _conn, "inspection_branch_import", "staging_purged_at", "REAL",
        )
        for col, typ in (
            ("audit_archive_sha256", "TEXT"),
            ("audit_archive_bytes", "INTEGER"),
            ("audit_archive_rows", "INTEGER"),
            ("audit_archived_at", "REAL"),
            ("audit_actions_json", "TEXT"),
            # Short aliases keep maintenance SQL discoverable while the audit
            # prefixed columns make the retention purpose explicit.
            ("archive_sha256", "TEXT"),
            ("archive_size", "INTEGER"),
            ("archive_row_count", "INTEGER"),
            ("archived_at", "REAL"),
        ):
            _add_column(_conn, "inspection_branch_import", col, typ)
        _add_column(
            _conn, "inspection_branch_import_row", "existing_branch_id",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column(
            _conn, "inspection_branch_import_row", "existing_row_version",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column(
            _conn, "inspection_branch_import_row",
            "existing_business_value_id", "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column(
            _conn, "inspection_branch_import_row",
            "existing_business_row_version", "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column(
            _conn, "inspection_business_value", "remark",
            "TEXT NOT NULL DEFAULT ''",
        )
        _add_column(
            _conn, "inspection_business_value", "row_version",
            "INTEGER NOT NULL DEFAULT 1",
        )
        # v53:行业决策员工目录版本化。新目录绝不复用 V1 idx；任务、线程和
        # 会议冻结员工身份，避免未来目录变更重写历史归因或重试语义。
        for col, typ in (
            ("employee_key", "TEXT NOT NULL DEFAULT ''"),
            ("employee_catalog_version", "TEXT NOT NULL DEFAULT ''"),
            ("employee_name_snapshot", "TEXT NOT NULL DEFAULT ''"),
            ("employee_dept_key", "TEXT NOT NULL DEFAULT ''"),
            ("employee_spec_sha256", "TEXT NOT NULL DEFAULT ''"),
        ):
            _add_column(_conn, "task", col, typ)
        for col, typ in (
            ("employee_key", "TEXT NOT NULL DEFAULT ''"),
            ("employee_catalog_version", "TEXT NOT NULL DEFAULT ''"),
        ):
            _add_column(_conn, "task_thread", col, typ)
        _add_column(
            _conn, "meeting", "member_snapshot_json",
            "TEXT NOT NULL DEFAULT '[]'",
        )
        if found_version < 53:
            _backfill_employee_identity_snapshots(_conn)
        # v54:人员在岗状态与岗位版本配置彻底分离。同一原始 idx 可同时
        # 保留 V1 历史岗位与 V3 当前岗位；所有已存任务/会话/会议绑定
        # full identity_ref + immutable config revision，不再按 idx 读取可变配置。
        _execute_migration_script(_conn, """
        CREATE TABLE IF NOT EXISTS employee_slot(
          idx INTEGER PRIMARY KEY,
          active_identity_ref TEXT UNIQUE,
          enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
          row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_employee_slot_active_identity
          ON employee_slot(active_identity_ref)
          WHERE active_identity_ref IS NOT NULL;

        CREATE TABLE IF NOT EXISTS employee_role_config(
          identity_ref TEXT PRIMARY KEY,
          idx INTEGER NOT NULL,
          employee_key TEXT NOT NULL,
          employee_catalog_version TEXT NOT NULL,
          employee_name_snapshot TEXT NOT NULL,
          employee_dept_key TEXT NOT NULL,
          employee_spec_sha256 TEXT NOT NULL,
          prompt_template TEXT,
          skills_json TEXT NOT NULL DEFAULT '[]',
          learned_at REAL,
          settings_json TEXT NOT NULL DEFAULT '{}',
          caps_off_json TEXT NOT NULL DEFAULT '[]',
          model_text TEXT,
          model_image TEXT,
          professional_profile_json TEXT NOT NULL DEFAULT '{}',
          config_revision INTEGER NOT NULL DEFAULT 1 CHECK(config_revision >= 1),
          config_sha256 TEXT NOT NULL,
          archived_at REAL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_employee_role_config_idx
          ON employee_role_config(idx,employee_catalog_version);

        CREATE TABLE IF NOT EXISTS employee_role_config_history(
          identity_ref TEXT NOT NULL,
          config_revision INTEGER NOT NULL CHECK(config_revision >= 1),
          idx INTEGER NOT NULL,
          employee_key TEXT NOT NULL,
          employee_catalog_version TEXT NOT NULL,
          employee_name_snapshot TEXT NOT NULL,
          employee_dept_key TEXT NOT NULL,
          employee_spec_sha256 TEXT NOT NULL,
          prompt_template TEXT,
          skills_json TEXT NOT NULL,
          learned_at REAL,
          settings_json TEXT NOT NULL,
          caps_off_json TEXT NOT NULL,
          model_text TEXT,
          model_image TEXT,
          professional_profile_json TEXT NOT NULL DEFAULT '{}',
          config_sha256 TEXT NOT NULL,
          archived_at REAL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          superseded_at REAL NOT NULL,
          PRIMARY KEY(identity_ref,config_revision)
        );
        CREATE INDEX IF NOT EXISTS idx_employee_role_history_idx
          ON employee_role_config_history(idx,identity_ref,config_revision);
        """)
        for col, typ in (
            ("employee_identity_ref", "TEXT NOT NULL DEFAULT ''"),
            ("employee_config_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("employee_config_sha256", "TEXT NOT NULL DEFAULT ''"),
        ):
            _add_column(_conn, "task", col, typ)
            _add_column(_conn, "task_thread", col, typ)
        _add_column(
            _conn, "employee_role_config", "professional_profile_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        _add_column(
            _conn, "employee_role_config_history", "professional_profile_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_employee_identity "
            "ON task(employee_identity_ref,employee_config_revision,id)"
        )
        _backfill_schema54_employee_bindings(
            _conn, source_schema_version=found_version,
        )
        # v55: V4 human identity + auditable role bundles/learning proposals.
        # V1--V3 identity refs remain six-field; the optional columns only carry
        # display snapshots and the V4 scheme marker.
        for col, typ in (
            ("person_snapshot", "TEXT NOT NULL DEFAULT ''"),
            ("identity_scheme", "TEXT NOT NULL DEFAULT 'legacy-six'"),
            ("bundle_sha256", "TEXT NOT NULL DEFAULT ''"),
        ):
            _add_column(_conn, "task", col, typ)
            _add_column(_conn, "task_thread", col, typ)
        for table in ("employee_role_config", "employee_role_config_history"):
            _add_column(_conn, table, "person_snapshot", "TEXT NOT NULL DEFAULT ''")
            _add_column(_conn, table, "identity_scheme", "TEXT NOT NULL DEFAULT 'legacy-six'")
        _execute_migration_script(_conn, """
        CREATE TABLE IF NOT EXISTS employee_role_bundle_revision(
          identity_ref TEXT NOT NULL,
          config_revision INTEGER NOT NULL CHECK(config_revision >= 1),
          idx INTEGER NOT NULL,
          employee_key TEXT NOT NULL,
          employee_catalog_version TEXT NOT NULL,
          employee_name_snapshot TEXT NOT NULL,
          person_snapshot TEXT NOT NULL DEFAULT '',
          identity_scheme TEXT NOT NULL DEFAULT 'legacy-six',
          config_sha256 TEXT NOT NULL,
          bundle_sha256 TEXT NOT NULL,
          baseline_json TEXT NOT NULL DEFAULT '{}',
          effective_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','historical','proposed','stale','rejected')),
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY(identity_ref,config_revision)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_employee_role_bundle_hash
          ON employee_role_bundle_revision(bundle_sha256);
        CREATE INDEX IF NOT EXISTS idx_employee_role_bundle_idx
          ON employee_role_bundle_revision(idx,employee_catalog_version);

        CREATE TABLE IF NOT EXISTS employee_learning_batch(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER,
          request_key TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued'
            CHECK(status IN ('queued','running','paused','completed','failed','cancelled')),
          budget_points REAL NOT NULL DEFAULT 0,
          budget_cap_points REAL NOT NULL DEFAULT 0,
          spent_points REAL NOT NULL DEFAULT 0,
          max_runs INTEGER NOT NULL DEFAULT 0,
          total_runs INTEGER NOT NULL DEFAULT 0,
          completed_runs INTEGER NOT NULL DEFAULT 0,
          failed_runs INTEGER NOT NULL DEFAULT 0,
          checkpoint_json TEXT NOT NULL DEFAULT '{}',
          paused_reason TEXT,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_by INTEGER,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          UNIQUE(tenant_id,request_key),
          UNIQUE(tenant_id,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS employee_learning_run(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL,
          idempotency_key TEXT NOT NULL,
          employee_idx INTEGER NOT NULL,
          identity_ref TEXT NOT NULL,
          config_revision INTEGER NOT NULL,
          base_config_revision INTEGER NOT NULL,
          base_config_sha256 TEXT NOT NULL,
          industry_key TEXT,
          high_risk INTEGER NOT NULL DEFAULT 0 CHECK(high_risk IN (0,1)),
          status TEXT NOT NULL DEFAULT 'queued'
            CHECK(status IN ('queued','researching','awaiting_approval','approved','activated','stale','failed','rejected','expired','cancelled','evidence_insufficient')),
          budget_points REAL NOT NULL DEFAULT 0,
          spent_points REAL NOT NULL DEFAULT 0,
          checkpoint_json TEXT NOT NULL DEFAULT '{}',
          proposal_json TEXT,
          expires_at REAL,
          result_json TEXT NOT NULL DEFAULT '{}',
          error_code TEXT,
          error_message TEXT,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          UNIQUE(batch_id,identity_ref),
          UNIQUE(batch_id,idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_employee_learning_run_identity
          ON employee_learning_run(identity_ref,status);
        CREATE TABLE IF NOT EXISTS employee_learning_source(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL,
          url TEXT NOT NULL,
          canonical_url TEXT NOT NULL,
          title TEXT NOT NULL DEFAULT '',
          publisher TEXT NOT NULL DEFAULT '',
          source_level TEXT NOT NULL DEFAULT '',
          authority_level TEXT NOT NULL DEFAULT '',
          published_at TEXT,
          fetched_at REAL,
          http_status INTEGER,
          certificate_status TEXT NOT NULL DEFAULT '',
          tls_valid INTEGER NOT NULL DEFAULT 0 CHECK(tls_valid IN (0,1)),
          content_sha256 TEXT NOT NULL DEFAULT '',
          excerpt TEXT NOT NULL DEFAULT '',
          capture_event_id TEXT NOT NULL DEFAULT '',
          capture_provider TEXT NOT NULL DEFAULT '',
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL,
          UNIQUE(run_id,canonical_url)
        );
        CREATE INDEX IF NOT EXISTS idx_employee_learning_source_run
          ON employee_learning_source(run_id);
        CREATE TABLE IF NOT EXISTS employee_learning_artifact(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL,
          artifact_type TEXT NOT NULL
            CHECK(artifact_type IN ('knowledge','skill','capability','data_object','tool','workflow','escalation','learning_track','profile')),
          kind TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'proposed'
            CHECK(status IN ('proposed','approved','rejected','activated','stale','expired','cancelled','superseded')),
          title TEXT NOT NULL,
          claim_text TEXT NOT NULL,
          statement TEXT NOT NULL,
          delta_json TEXT NOT NULL DEFAULT '{}',
          payload_json TEXT NOT NULL DEFAULT '{}',
          source_ids_json TEXT NOT NULL DEFAULT '[]',
          evidence_json TEXT NOT NULL DEFAULT '{}',
          reviewer_id INTEGER,
          reviewed_at REAL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_employee_learning_artifact_run
          ON employee_learning_artifact(run_id,status);
        """)
        _schema55_migrate(_conn, source_schema_version=found_version)
        # 只把旧 tenants.industries_json 中显式列出的部门迁入规范化映射。
        # 非平台租户的空列表不再被老板看板解释为“全行业”。
        for tenant in _conn.execute(
                "SELECT id,industries_json FROM tenants"):
            industries = []
            try:
                industries = json.loads(tenant["industries_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                industries = []
            clean = [
                value.strip() for value in industries
                if isinstance(value, str) and value.strip()
            ]
            for position, industry_key in enumerate(dict.fromkeys(clean)):
                _conn.execute(
                    "INSERT OR IGNORE INTO tenant_industry("
                    "tenant_id,industry_key,is_primary,created_at) VALUES(?,?,?,?)",
                    (tenant["id"], industry_key, 1 if position == 0 else 0,
                     time.time()),
                )
        # v47:会话签名密钥只能由 root-owned systemd EnvironmentFile 注入。
        # 清除旧版写入业务库的全局密钥，避免只读数据库泄漏演变为会话伪造。
        _conn.execute(
            "DELETE FROM app_setting WHERE key='session_secret'"
        )
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(47,'environment-only-session-secret-migration',?)",
            (time.time(),),
        )
        # 旧会议没有 V28 决策字段；只做展示态回填，绝不把历史会议重新跑一遍。
        _conn.execute("UPDATE meeting SET phase='completed' "
                      "WHERE status='done' AND phase='queued' AND decision IS NULL")
        _conn.execute("UPDATE meeting SET phase='failed' "
                      "WHERE status='failed' AND phase='queued'")
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(48,'collaboration-soft-delete-schedule-fail-streak',?)",
            (time.time(),),
        )
        _validate_migrated_database(_conn)
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(49,'purchase-intent-commercial-loop',?)",
            (time.time(),),
        )
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(50,'explicit-job-attribution-for-safe-purge',?)",
            (time.time(),),
        )
        _validate_migrated_database(_conn)
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(51,'inspection-task-threads-industry-dashboard',?)",
            (time.time(),),
        )
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(52,'inspection-branch-master-import',?)",
            (time.time(),),
        )
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(53,'versioned-industry-decision-employees',?)",
            (time.time(),),
        )
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(54,'employee-slot-role-config-revisions',?)",
            (time.time(),),
        )
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(55,'v4-person-role-bundles-learning-audit',?)",
            (time.time(),),
        )
        # v56:会议 Agent 团队协作执行。GO/NEED_INFO 后按分工接力派活,
        # 每个成员任务自动携带队友已交付内容,最后由队长整合最终交付包。
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(56,'meeting-agent-team-relay',?)",
            (time.time(),),
        )
        # v57:副账号职级体系(总监/经理/员工)与数字员工级白名单分配。
        # 老板全权;总监/经理只能在自己行业内给下级分配数字员工。
        for col, typ in (
                ("job_title", "TEXT NOT NULL DEFAULT 'staff'"),
                ("allowed_emp_idxs_json", "TEXT"),
        ):
            _add_column(_conn, "users", col, typ)
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) "
            "VALUES(57,'member-hierarchy-employee-allocation',?)",
            (time.time(),),
        )
        _conn.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")
        _conn.commit()
    return _conn


def _close_thread_connection():
    connection = getattr(_thread, "connection", None)
    if connection is not None and connection is not _conn:
        try:
            connection.close()
        except sqlite3.Error:
            pass
        _all_connections.discard(connection)
    _thread.connection = None
    _thread.generation = -1
    _thread.db_path = None
    _thread.atomic_depth = 0


def _close_all_connections():
    """Close registered connections during test DB swaps and interpreter exit."""
    for connection in list(_all_connections):
        try:
            connection.close()
        except sqlite3.Error:
            pass
    _all_connections.clear()


def _conn_locked(path: str):
    """Slow connection path; caller holds ``_generation_lock``."""
    global _conn, _conn_path, _connection_generation

    if (
        (_conn is None or _conn_path not in (None, path))
        and threading.current_thread().name.startswith("dbio")
    ):
        # 异步写池线程绝不执行代际切换:走到这里说明库已被换走(测试/维护),
        # 这笔快照写属于已死的库,丢弃即正确语义。若允许池线程做切换,它会
        # 与主线程互相关闭对方正在使用的连接——那是段错误,不是异常。
        raise StaleWriteError(path)

    # 数据库代际切换(测试/维护工具换 DB_PATH)前,先在 _init_lock 外排空异步写池:
    # 在途任务可能正拿着旧库连接执行,任何跨线程 close 都会段错误。
    # generation lock 同时挡住新池的创建，避免 drain 期间重新生出旧代 worker。
    if (
        _conn_path not in (None, path)
        or (_conn is None and _conn_path is not None)
    ):
        _shutdown_async_pool(wait=True)

    with _init_lock:
        # Tests/maintenance tools intentionally switch DB_PATH after closing the
        # anchor.  Treat either signal as a new database generation.
        if _conn is None or _conn_path not in (None, path):
            _close_thread_connection()
            # 不做跨线程关闭:其它线程的旧代连接由各自线程在下次 conn() 的
            # 代际检查里自行关闭(_close_thread_connection),这里只关自己的
            # 与锚点。跨线程 close 一个可能正在执行的连接是段错误之源。
            if _conn is not None:
                try:
                    _conn.close()
                except sqlite3.Error:
                    pass
                _all_connections.discard(_conn)
                _conn = None
            try:
                anchor = _initialize_anchor(path)
            except Exception:
                if _conn is not None:
                    try:
                        _conn.close()
                    except sqlite3.Error:
                        pass
                    _all_connections.discard(_conn)
                    _conn = None
                _conn_path = None
                raise
            _conn_path = path
            _connection_generation += 1
            if threading.current_thread().name.startswith("dbio"):
                # 异步写池线程绝不领养锚点:锚点(_conn)会被测试/维护工具从主线程
                # 直接 close,若此刻池线程正用它执行就是段错误。池线程一律用
                # 自己的独立连接,让"谁的连接谁关"始终成立。
                connection = sqlite3.connect(
                    path, timeout=30, check_same_thread=False
                )
                _all_connections.add(connection)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("PRAGMA synchronous=NORMAL")
                _thread.connection = connection
                _thread.generation = _connection_generation
                _thread.db_path = path
                _thread.atomic_depth = 0
                return connection
            _thread.connection = anchor
            _thread.generation = _connection_generation
            _thread.db_path = path
            _thread.atomic_depth = 0
            return anchor

        if _conn_path is None:
            # Compatibility with an anchor created before this wrapper first ran.
            _conn_path = path
            _connection_generation += 1

        local = getattr(_thread, "connection", None)
        if (
            local is not None
            and getattr(_thread, "generation", -1) != _connection_generation
        ):
            _close_thread_connection()

        connection = sqlite3.connect(
            path, timeout=30, check_same_thread=False
        )
        _all_connections.add(connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        _thread.connection = connection
        _thread.generation = _connection_generation
        _thread.db_path = path
        _thread.atomic_depth = 0
        return connection


def conn():
    """Return a connection owned by the current worker thread.

    Schema initialization still happens once on the process anchor. Afterwards
    each request-worker thread gets an independent SQLite connection configured
    for WAL and busy waiting. A generation switch is serialized with async-pool
    selection, so no new strict async call can enter the pool being drained.
    """
    path = _canonical_db_path(DB_PATH)
    local = getattr(_thread, "connection", None)
    if (
        _conn is not None
        and _conn_path == path
        and local is not None
        and getattr(_thread, "generation", -1) == _connection_generation
        and getattr(_thread, "db_path", None) == path
    ):
        return local

    # DB workers must fail before waiting on the generation lock. The switching
    # thread may be draining this very worker, so waiting here would deadlock.
    if (
        (_conn is None or _conn_path not in (None, path))
        and threading.current_thread().name.startswith("dbio")
    ):
        raise StaleWriteError(path)

    with _generation_lock:
        # Another non-DB thread may have completed the switch while we waited.
        path = _canonical_db_path(DB_PATH)
        local = getattr(_thread, "connection", None)
        if (
            _conn is not None
            and _conn_path == path
            and local is not None
            and getattr(_thread, "generation", -1) == _connection_generation
            and getattr(_thread, "db_path", None) == path
        ):
            return local
        switching = (
            _conn_path not in (None, path)
            or (_conn is None and _conn_path is not None)
        )
        if switching:
            _generation_switching.set()
        try:
            return _conn_locked(path)
        finally:
            if switching:
                _generation_switching.clear()


# ---------------- 全局设置(V4) ----------------
def get_setting(key, default=None):
    r = one("SELECT value FROM app_setting WHERE key=?", (key,))
    return r["value"] if r and r["value"] is not None else default


def set_setting(key, value):
    connection = conn()
    connection.execute(
        "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        "updated_at=excluded.updated_at",
        (key, value, time.time()),
    )
    if not getattr(_thread, "atomic_depth", 0):
        connection.commit()


def q(sql, args=()):
    connection = conn()
    cur = connection.execute(sql, args)
    rows = [dict(r) for r in cur.fetchall()]
    # SELECT/PRAGMA 等只读查询绝不能替外层 db.atomic() 提前提交。
    # 写语句没有结果列，仍保留历史 q("DELETE ...") 的自动提交语义.
    if cur.description is None and not getattr(_thread, "atomic_depth", 0):
        connection.commit()
    return rows


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def insert(table, data):
    data = dict(data)
    data.setdefault("created_at", time.time())
    data.setdefault("updated_at", time.time())
    cols = ",".join(data)
    ph = ",".join("?" * len(data))
    connection = conn()
    cur = connection.execute(
        f"INSERT INTO {table}({cols}) VALUES({ph})", list(data.values()))
    if not getattr(_thread, "atomic_depth", 0):
        connection.commit()
    return cur.lastrowid


def update(table, id_, data):
    data = dict(data)
    data["updated_at"] = time.time()
    sets = ",".join(f"{k}=?" for k in data)
    connection = conn()
    connection.execute(
        f"UPDATE {table} SET {sets} WHERE id=?", list(data.values()) + [id_])
    if not getattr(_thread, "atomic_depth", 0):
        connection.commit()


from contextlib import contextmanager


@contextmanager
def atomic():
    """Run a transaction on the current worker's private connection.

    Nested callers use a savepoint.  Helper calls such as ``db.update`` detect
    the active depth and never commit the outer transaction early.
    """
    c = conn()
    depth = int(getattr(_thread, "atomic_depth", 0) or 0)
    savepoint = f"paihuo_sp_{depth}"
    if depth == 0:
        c.execute("BEGIN IMMEDIATE")
    else:
        c.execute(f"SAVEPOINT {savepoint}")
    _thread.atomic_depth = depth + 1
    try:
        yield c
        if depth == 0:
            c.commit()
        else:
            c.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if depth == 0:
            c.rollback()
        else:
            c.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            c.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    finally:
        _thread.atomic_depth = depth


def execute(sql, args=()) -> int:
    """写语句,返回受影响行数(条件扣费等原子操作用)."""
    connection = conn()
    cur = connection.execute(sql, args)
    if not getattr(_thread, "atomic_depth", 0):
        connection.commit()
    return cur.rowcount


def jloads(s, default=None):
    if not s:
        return default if default is not None else {}
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default if default is not None else {}


# ---------------- 异步门面 ----------------
# SQLite 是同步库,busy_timeout=30s。事件循环上的协程(引擎流水线、SSE 周边、
# async 路由)直接调 db 时,任何一次写锁等待都会把整个循环冻住——所有租户的
# SSE、所有请求一起停。异步侧必须经由这里的门面把 db 调用卸载到专用线程池:
# 连接本就是线程本地的(见 conn()),每个池线程各持一条连接,WAL 下并发安全。
#
# 池子刻意有界:SQLite 同时只有一个写者,更多线程只会排队占内存;4 条足够
# 覆盖「读 + 写 + 看门狗 + 后台任务」的并发形态。
_async_pool = None
_async_pool_lock = threading.Lock()
_write_pool = None
_write_pool_lock = threading.Lock()


def _pool():
    global _async_pool
    with _generation_lock:
        if _async_pool is None:
            with _async_pool_lock:
                if _async_pool is None:
                    from concurrent.futures import ThreadPoolExecutor
                    _async_pool = ThreadPoolExecutor(
                        max_workers=4, thread_name_prefix="dbio")
        return _async_pool


def _ordered_write_pool():
    """Return the FIFO executor used exclusively by ``submit_write``.

    SQLite can commit only one writer at a time.  Sending progress snapshots to
    the four-worker read pool let a newer snapshot commit first and an older
    snapshot overwrite it afterwards.  A dedicated single worker preserves
    submission order while ``arun``/``aq`` keep using the concurrent pool.
    """
    global _write_pool
    with _generation_lock:
        if _write_pool is None:
            with _write_pool_lock:
                if _write_pool is None:
                    from concurrent.futures import ThreadPoolExecutor
                    _write_pool = ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="dbio-write")
        return _write_pool


def _submit_async_call(context, call):
    """Select/create the DB pool in the same generation critical section.

    This helper runs in asyncio's default executor. Waiting for a maintenance
    generation switch therefore never blocks the event-loop thread.
    """
    with _generation_lock:
        path = os.path.abspath(DB_PATH)
        if _conn is None or _conn_path not in (None, path):
            try:
                conn()
            finally:
                # ``conn`` may give this short-lived default-executor thread a
                # local connection. It must not become an untracked generation
                # leak after its sole job (preparing the anchor) is complete.
                _close_thread_connection()
        return _pool().submit(context.run, call)


async def arun(fn, *args, **kwargs):
    """在 db 线程池里执行任意同步函数(含整段 with atomic() 的事务体)。

    这是异步侧访问数据库的唯一正道:事务必须整体进池(不能在持有 BEGIN 的
    情况下 await),所以传入的是完整的同步函数而非单条语句。
    """
    import asyncio
    import contextvars
    import functools
    call = functools.partial(fn, *args, **kwargs) if (args or kwargs) else fn
    context = contextvars.copy_context()
    for attempt in range(2):
        future = await asyncio.to_thread(_submit_async_call, context, call)
        try:
            return await asyncio.wrap_future(future)
        except StaleWriteError:
            # DB_PATH can be assigned immediately after pool selection by a
            # test/maintenance switch. StaleWriteError is raised at conn()'s
            # side-effect-free top boundary, so one re-submit is safe.
            if attempt:
                raise


_pending_writes: list = []
_pending_lock = threading.Lock()


def submit_write(fn, *args, **kwargs):
    """不等待结果地把一次写操作投给 FIFO 单写队列(节流型进度落库用)。

    只适用于「丢了也无碍、下次会整体重写」的快照类写入;需要结果或需要
    返回值的写仍然要走 arun。提交到本门面的快照严格按调用顺序执行，
    因而同一实体的旧进度不可能在新进度之后反向覆盖。异常在单写线程里
    记日志,不向上冒泡。
    需要「先前的异步写都已落地」时,用 adrain() 冲刷。
    """
    import functools
    import logging
    call = functools.partial(fn, *args, **kwargs) if (args or kwargs) else fn
    submitted_path = os.path.abspath(DB_PATH)
    submitted_generation = _connection_generation

    def _guarded():
        try:
            # 数据库在提交后被切换(测试/维护)时,这笔写属于已死的库,直接丢弃;
            # 快照类写入的语义本就允许丢帧,写进错误的库反而是事故。
            # 同时比对代际:路径字符串在切换瞬间可能仍相同(TOCTOU),代际不会。
            if (os.path.abspath(DB_PATH) != submitted_path
                    or _connection_generation != submitted_generation):
                return
            call()
        except StaleWriteError:
            pass       # conn() 在执行中发现库被切换,静默丢弃这笔快照写
        except sqlite3.ProgrammingError:
            pass       # 连接在执行间隙被回收(库已切换),同样按过期快照丢弃
        except Exception as exc:
            logging.getLogger("db").warning(
                "submit_write failed error_type=%s", type(exc).__name__)

    # Progress snapshots are intentionally lossy. If a generation switch is in
    # flight, drop this frame instead of blocking the event loop or recreating
    # the old write pool while it is being drained.
    if _generation_switching.is_set():
        return
    with _generation_lock:
        if _generation_switching.is_set():
            return
        future = _ordered_write_pool().submit(_guarded)
    with _pending_lock:
        _pending_writes.append(future)
        if len(_pending_writes) > 64:      # 顺手清掉已完成的,防列表无限增长
            _pending_writes[:] = [f for f in _pending_writes if not f.done()]


async def adrain():
    """等待此刻之前提交的全部 submit_write 落地(收尾处保证「返回即持久」)。"""
    import asyncio
    with _pending_lock:
        snapshot = [f for f in _pending_writes if not f.done()]
        _pending_writes[:] = snapshot
    for future in snapshot:
        await asyncio.wrap_future(future)


async def aq(sql, args=()):
    return await arun(q, sql, args)


async def aone(sql, args=()):
    return await arun(one, sql, args)


async def ainsert(table, data):
    return await arun(insert, table, data)


async def aupdate(table, id_, data):
    return await arun(update, table, id_, data)


async def aexecute(sql, args=()):
    return await arun(execute, sql, args)


async def aget_setting(key, default=None):
    return await arun(get_setting, key, default)


async def aset_setting(key, value):
    return await arun(set_setting, key, value)


def _shutdown_async_pool(wait: bool = True):
    """停掉异步 DB 池。默认等在途任务收尾——不等就关连接会段错误。"""
    global _async_pool, _write_pool
    pool = write_pool = None
    with _write_pool_lock:
        write_pool = _write_pool
        _write_pool = None
    with _async_pool_lock:
        pool = _async_pool
        _async_pool = None
    def reclaim_worker_connections(executor):
        """让每个 executor 线程亲自关闭它拥有的 SQLite 连接。

        SQLite 连接不能在另一个仍可能执行语句的线程里强关。把与现存线程数
        相同的 barrier 任务排到队尾，可以保证所有旧工作完成后每个 worker
        各领取一个清理任务，再由连接所属线程执行 close。
        """
        workers = len(getattr(executor, "_threads", ()))
        # Python 的 ThreadPoolExecutor 退出钩子早于普通 atexit 回调；解释器
        # 收尾时池可能已被标准库关闭，此时线程已经停止，后续 _close_all_connections
        # 可安全回收注册表，无需再向已 shutdown 的池提交任务。
        if (
            not wait
            or workers <= 0
            or getattr(executor, "_shutdown", False)
        ):
            return
        barrier = threading.Barrier(workers)

        def close_owned_connection():
            try:
                barrier.wait(timeout=30)
            finally:
                _close_thread_connection()

        futures = [
            executor.submit(close_owned_connection) for _ in range(workers)
        ]
        for future in futures:
            future.result()

    # submit_write 可能正使用自己的线程本地连接，必须先等它排空；随后让池线程
    # 自己回收连接，避免每次测试/维护切换 DB_PATH 都泄漏整代连接。
    if write_pool is not None:
        reclaim_worker_connections(write_pool)
        write_pool.shutdown(wait=wait)
    if pool is not None:
        reclaim_worker_connections(pool)
        pool.shutdown(wait=wait)
    if wait:
        with _pending_lock:
            _pending_writes[:] = [
                future for future in _pending_writes if not future.done()
            ]


def _shutdown_all():
    # 顺序是安全性的一部分:必须先等池里的在途任务结束,再关闭连接。
    _shutdown_async_pool(wait=True)
    _close_all_connections()


atexit.register(_shutdown_all)
