"""Direct-employee task threads with paid, idempotent follow-up revisions.

The module deliberately owns no HTTP, authentication, billing price, or worker
lifecycle policy.  A caller injects the existing charged-task creator; this
module keeps that creation and the thread pointer CAS in one SQLite transaction.
Only the current delivery body is read to prepare the next prompt.  Thread
summaries expose bounded revision metadata and never return historical bodies.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from . import db


ACTIVE_TASK_STATUSES = frozenset({"pending_charge", "queued", "running"})
MAX_FREE_RETRIES = 3
THREAD_STATUSES = frozenset({"active", "satisfied"})
TASK_PHASES = frozenset({"delivery", "revision"})
MAX_REVISIONS_RETURNED = 50
MAX_LEGACY_REVISIONS = 64
MAX_FEEDBACK_CHARS = 2000
MAX_MATERIAL_CHARS = 12000
MAX_PREVIOUS_EXCERPT_CHARS = 12000
INSPECTION_EMPLOYEE_IDX = 10
_REQUEST_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{11,127}$")
EVIDENCE_ITEMS_ABSENT = object()
_TASK_IDENTITY_FIELDS = (
    "employee_key", "employee_catalog_version", "employee_name_snapshot",
    "employee_dept_key", "employee_spec_sha256",
    "employee_identity_ref", "employee_config_revision",
    "employee_config_sha256", "person_snapshot", "identity_scheme",
    "bundle_sha256",
)


def _task_identity(task: dict) -> tuple[str, ...]:
    values = tuple(str(task.get(field) or "").strip() for field in _TASK_IDENTITY_FIELDS)
    required = tuple(
        value for field, value in zip(_TASK_IDENTITY_FIELDS, values)
        if field != "person_snapshot"
    )
    if not all(required):
        raise ThreadConflict("employee_identity_missing", "任务的员工身份快照缺失")
    return values


class TaskThreadError(RuntimeError):
    """Base domain error with a stable code for the HTTP adapter."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TaskThreadNotFound(TaskThreadError):
    pass


class ThreadConflict(TaskThreadError):
    pass


class IdempotencyConflict(TaskThreadError):
    pass


class InvalidFollowup(TaskThreadError):
    pass


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise InvalidFollowup("invalid_identifier", f"{label}无效")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidFollowup("invalid_identifier", f"{label}无效") from exc
    if result < 1:
        raise InvalidFollowup("invalid_identifier", f"{label}无效")
    return result


def _employee_int(value: Any) -> int:
    if isinstance(value, bool):
        raise InvalidFollowup("invalid_identifier", "员工编号无效")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidFollowup("invalid_identifier", "员工编号无效") from exc
    # Core content employees start at idx=0; industry employees use 100+.
    if result < 0:
        raise InvalidFollowup("invalid_identifier", "员工编号无效")
    return result


def _clean_request_key(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidFollowup("invalid_request_key", "请求编号格式无效")
    value = value.strip()
    if not _REQUEST_KEY.fullmatch(value):
        raise InvalidFollowup(
            "invalid_request_key",
            "请求编号应为 12–128 位字母、数字或 ._:-",
        )
    return value


def normalize_request_key(value: Any) -> str:
    """公开的任务幂等号规范化入口；首轮与续轮共用同一契约。"""
    return _clean_request_key(value)


def _clean_feedback(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidFollowup("invalid_feedback", "修改意见格式无效")
    value = value.strip()
    if not value:
        raise InvalidFollowup("empty_feedback", "请先写下这一轮希望修改的内容")
    if len(value) > MAX_FEEDBACK_CHARS:
        raise InvalidFollowup(
            "feedback_too_long",
            f"修改意见最多 {MAX_FEEDBACK_CHARS} 个字符",
        )
    return value


def _clean_material(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise InvalidFollowup("invalid_material", "补充材料格式无效")
    value = value.strip()
    if len(value) > MAX_MATERIAL_CHARS:
        raise InvalidFollowup(
            "material_too_long",
            f"补充材料最多 {MAX_MATERIAL_CHARS} 个字符",
        )
    return value


def _combined_material(original: Any, supplemental: str) -> str:
    original = str(original or "").strip()
    if not supplemental:
        return original[:MAX_MATERIAL_CHARS]
    if not original:
        return supplemental[:MAX_MATERIAL_CHARS]
    separator = "\n\n【本轮补充材料】\n"
    combined = original + separator + supplemental
    if len(combined) <= MAX_MATERIAL_CHARS:
        return combined
    # 总上限固定时优先保留本轮新约束，旧材料保留最近的尾部。
    # 这使多轮追问不会因首版长材料占满额度而丢掉上一轮补充。
    new_part = supplemental[: MAX_MATERIAL_CHARS // 2]
    old_room = MAX_MATERIAL_CHARS - len(separator) - len(new_part)
    old_part = original[-old_room:] if old_room > 0 else ""
    return old_part + separator + new_part


def _employee_thread_policy(connection, emp_idx: int) -> tuple[bool, bool, str | None]:
    """Return (can_continue, can_accept, blocked_code) for one employee.

    Employee configuration is global in the existing product model.  Read it
    through the caller's transaction so a disable and a follow-up cannot race.
    """
    emp_idx = int(emp_idx)
    if emp_idx == INSPECTION_EMPLOYEE_IDX:
        return False, False, "inspection_workbench_required"
    row = connection.execute(
        "SELECT s.enabled AS slot_enabled,c.enabled AS legacy_enabled "
        "FROM (SELECT ? AS idx) x "
        "LEFT JOIN employee_slot s ON s.idx=x.idx "
        "LEFT JOIN employee_config c ON c.idx=x.idx",
        (emp_idx,),
    ).fetchone()
    if row is not None and (
        (row["slot_enabled"] is not None and int(row["slot_enabled"] or 0) == 0)
        or (row["legacy_enabled"] is not None and int(row["legacy_enabled"] or 0) == 0)
    ):
        # 已交付版仍可标记满意，但停用员工不得再开新轮。
        return False, True, "employee_disabled"
    return True, True, None


def _require_followup_employee(connection, emp_idx: int) -> None:
    can_continue, _can_accept, code = _employee_thread_policy(
        connection, emp_idx
    )
    if can_continue:
        return
    if code == "inspection_workbench_required":
        raise ThreadConflict(
            code,
            "巡店经理必须在巡店工作台上传照片并按整改闭环继续",
        )
    raise ThreadConflict(
        "employee_disabled", "该数字员工已停用，重新启用后才能继续修改"
    )


def _require_accept_employee(connection, emp_idx: int) -> None:
    _can_continue, can_accept, code = _employee_thread_policy(
        connection, emp_idx
    )
    if can_accept:
        return
    raise ThreadConflict(
        code or "employee_unavailable",
        "巡店任务需在巡店工作台完成整改与复检确认",
    )


def _now(value: float | None) -> float:
    return float(time.time() if value is None else value)


def _row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _live_task(connection, task_id: int, tenant_id: int) -> dict:
    row = connection.execute(
        "SELECT * FROM task WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (task_id, tenant_id),
    ).fetchone()
    if not row:
        raise TaskThreadNotFound("task_not_found", "任务不存在或已被删除")
    return dict(row)


def _thread_row(connection, thread_id: int, tenant_id: int) -> dict:
    row = connection.execute(
        "SELECT * FROM task_thread WHERE id=? AND tenant_id=?",
        (thread_id, tenant_id),
    ).fetchone()
    if not row:
        raise ThreadConflict(
            "orphaned_thread",
            "任务的协作会话记录缺失，请联系平台修复后再继续",
        )
    return dict(row)


def _latest_delivered_revision(
    connection, thread_id: int, tenant_id: int
) -> dict | None:
    row = connection.execute(
        "SELECT * FROM task WHERE thread_id=? AND tenant_id=? "
        "AND status='done' AND deleted_at IS NULL "
        "ORDER BY revision_no DESC LIMIT 1",
        (int(thread_id), int(tenant_id)),
    ).fetchone()
    return dict(row) if row is not None else None


def _failed_revision_recovery(
    connection,
    thread: dict,
    current: dict,
    tenant_id: int,
) -> tuple[dict | None, str | None]:
    """Return the last usable delivery after an exhausted failed leaf.

    A failed revision remains immutable history and stays the thread's current
    (highest-numbered) anchor.  Once its free retries are exhausted, the user
    may branch the *next* numbered revision from the last successful body, or
    accept that body.  The lineage still points through the failed leaf so the
    audit trail never silently erases the failed attempt.
    """
    if current.get("status") != "failed":
        return None, None
    if current.get("billing_status") not in {"refunded", "included"}:
        return None, "refund_pending"
    if int(current.get("retry_count") or 0) < MAX_FREE_RETRIES:
        return None, "free_retry_available"
    delivered = _latest_delivered_revision(
        connection, int(thread["id"]), int(tenant_id)
    )
    if delivered is None:
        return None, "no_delivered_revision"
    return delivered, None


def _validate_thread_contract(
    connection,
    thread: dict,
    tenant_id: int,
    *,
    member_task: dict | None = None,
) -> tuple[dict, dict, int]:
    if thread.get("status") not in THREAD_STATUSES:
        raise ThreadConflict("invalid_thread_status", "协作会话状态异常")
    root = connection.execute(
        "SELECT id,tenant_id,emp_idx,thread_id,revision_no,phase,deleted_at,"
        "employee_key,employee_catalog_version,employee_name_snapshot,"
        "employee_dept_key,employee_spec_sha256,employee_identity_ref,"
        "employee_config_revision,employee_config_sha256,person_snapshot,"
        "identity_scheme,bundle_sha256 "
        "FROM task WHERE id=? AND tenant_id=?",
        (thread["root_task_id"], tenant_id),
    ).fetchone()
    current = connection.execute(
        "SELECT id,tenant_id,emp_idx,thread_id,revision_no,phase,status,"
        "billing_status,retry_count,deleted_at,employee_key,"
        "employee_catalog_version,employee_name_snapshot,employee_dept_key,"
        "employee_spec_sha256,employee_identity_ref,employee_config_revision,"
        "employee_config_sha256,person_snapshot,identity_scheme,bundle_sha256 "
        "FROM task "
        "WHERE id=? AND tenant_id=?",
        (thread["current_task_id"], tenant_id),
    ).fetchone()
    if not root or not current:
        raise ThreadConflict(
            "orphaned_thread_anchor",
            "协作会话的首版或当前版本缺失，已停止继续生成",
        )
    root, current = dict(root), dict(current)
    expected_thread = int(thread["id"])
    expected_emp = int(thread["emp_idx"])
    expected_key = str(thread.get("employee_key") or "").strip()
    expected_version = str(thread.get("employee_catalog_version") or "").strip()
    expected_ref = str(thread.get("employee_identity_ref") or "").strip()
    expected_config_hash = str(thread.get("employee_config_sha256") or "").strip()
    expected_person = str(thread.get("person_snapshot") or "").strip()
    expected_scheme = str(thread.get("identity_scheme") or "").strip()
    expected_bundle = str(thread.get("bundle_sha256") or "").strip()
    try:
        expected_config_revision = int(thread.get("employee_config_revision") or 0)
    except (TypeError, ValueError):
        expected_config_revision = 0
    if (
        not expected_key or not expected_version or len(expected_ref) != 64
        or len(expected_config_hash) != 64 or expected_config_revision < 1
        or not expected_scheme or len(expected_bundle) != 64
    ):
        raise ThreadConflict("employee_identity_missing", "协作会话的员工身份缺失")
    expected_identity = _task_identity(root)
    for anchor in (root, current):
        if (
            int(anchor["tenant_id"]) != tenant_id
            or int(anchor["emp_idx"]) != expected_emp
            or int(anchor.get("thread_id") or 0) != expected_thread
            or anchor.get("phase") not in TASK_PHASES
            or _task_identity(anchor) != expected_identity
            or str(anchor.get("employee_key") or "") != expected_key
            or str(anchor.get("employee_catalog_version") or "") != expected_version
            or str(anchor.get("employee_identity_ref") or "") != expected_ref
            or int(anchor.get("employee_config_revision") or 0)
            != expected_config_revision
            or str(anchor.get("employee_config_sha256") or "")
            != expected_config_hash
            or str(anchor.get("person_snapshot") or "") != expected_person
            or str(anchor.get("identity_scheme") or "") != expected_scheme
            or str(anchor.get("bundle_sha256") or "") != expected_bundle
        ):
            raise ThreadConflict(
                "thread_anchor_mismatch",
                "协作会话的任务归属不一致，已停止继续生成",
            )
    if root.get("deleted_at") is not None or current.get("deleted_at") is not None:
        raise ThreadConflict(
            "deleted_thread_anchor",
            "协作会话的首版或当前版本已被删除",
        )
    if member_task is not None and (
        int(member_task.get("thread_id") or 0) != expected_thread
        or int(member_task["tenant_id"]) != tenant_id
        or int(member_task["emp_idx"]) != expected_emp
        or _task_identity(member_task) != expected_identity
    ):
        raise ThreadConflict(
            "thread_member_mismatch",
            "任务不属于该协作会话",
        )
    stats = connection.execute(
        "SELECT COUNT(*) AS n,COALESCE(MAX(revision_no),0) AS max_revision,"
        "SUM(CASE WHEN employee_key=? AND employee_catalog_version=? "
        "AND employee_name_snapshot=? AND employee_dept_key=? "
        "AND employee_spec_sha256=? AND employee_identity_ref=? "
        "AND employee_config_revision=? AND employee_config_sha256=? "
        "AND person_snapshot=? AND identity_scheme=? AND bundle_sha256=? "
        "THEN 1 ELSE 0 END) AS identity_matches "
        "FROM task WHERE thread_id=? AND tenant_id=?",
        (*expected_identity, expected_thread, tenant_id),
    ).fetchone()
    count = int(stats["n"] or 0)
    maximum = int(stats["max_revision"] or 0)
    if (
        count < 1
        or count != int(thread.get("revision_count") or 0)
        or maximum != int(thread.get("revision_count") or 0)
        or int(stats["identity_matches"] or 0) != count
        or int(root.get("revision_no") or 0) != 1
        or int(current.get("revision_no") or 0) != maximum
    ):
        raise ThreadConflict(
            "thread_revision_mismatch",
            "协作会话的版本记录不完整，已停止继续生成",
        )
    return root, current, count


def _legacy_children(connection, task: dict) -> list[dict]:
    rows = connection.execute(
        "SELECT * FROM task WHERE source_task_id=? AND tenant_id=? "
        "AND emp_idx=? AND deleted_at IS NULL ORDER BY id",
        (task["id"], task["tenant_id"], task["emp_idx"]),
    ).fetchall()
    return [dict(row) for row in rows]


def _legacy_linear_chain(connection, task: dict) -> list[dict]:
    """Return the full unthreaded linear legacy chain containing ``task``.

    Old ``/redo`` calls could technically branch.  A linear thread cannot
    truthfully choose a winning branch, so ambiguity is surfaced instead of
    being hidden by an arbitrary id ordering.
    """
    if task.get("status") != "done":
        raise ThreadConflict(
            "task_not_done",
            "这一版还没交付，请等它完成后再继续沟通",
        )
    if task.get("thread_id") is not None:
        raise ThreadConflict("already_threaded", "任务已属于协作会话")

    seen = {int(task["id"])}
    ancestors: list[dict] = []
    cursor = task
    while cursor.get("source_task_id"):
        if len(seen) >= MAX_LEGACY_REVISIONS:
            raise ThreadConflict("legacy_chain_too_long", "旧版任务链过长，请联系平台整理")
        try:
            parent_id = int(cursor["source_task_id"])
        except (TypeError, ValueError):
            break
        if parent_id in seen:
            raise ThreadConflict("legacy_cycle", "旧版任务来源存在循环，无法安全收养")
        parent_row = connection.execute(
            "SELECT * FROM task WHERE id=? AND tenant_id=? AND emp_idx=? "
            "AND deleted_at IS NULL",
            (parent_id, task["tenant_id"], task["emp_idx"]),
        ).fetchone()
        if not parent_row:
            break
        parent = dict(parent_row)
        if parent.get("thread_id") is not None:
            raise ThreadConflict(
                "legacy_partial_thread",
                "旧版任务链已被部分收养，请联系平台修复",
            )
        if parent.get("status") != "done":
            raise ThreadConflict(
                "legacy_nonfinal_parent",
                "旧版任务链中有未交付版本，无法安全收养",
            )
        children = _legacy_children(connection, parent)
        if len(children) != 1 or int(children[0]["id"]) != int(cursor["id"]):
            raise ThreadConflict(
                "legacy_branch",
                "旧版任务存在多个分支，请从希望保留的最新版重新派活",
            )
        ancestors.append(parent)
        seen.add(parent_id)
        cursor = parent

    chain = list(reversed(ancestors)) + [task]
    cursor = task
    while True:
        if len(chain) >= MAX_LEGACY_REVISIONS:
            children = _legacy_children(connection, cursor)
            if children:
                raise ThreadConflict(
                    "legacy_chain_too_long",
                    "旧版任务链过长，请联系平台整理",
                )
            break
        children = _legacy_children(connection, cursor)
        if not children:
            break
        if len(children) != 1:
            raise ThreadConflict(
                "legacy_branch",
                "旧版任务存在多个分支，请从希望保留的最新版重新派活",
            )
        child = children[0]
        child_id = int(child["id"])
        if child_id in seen:
            raise ThreadConflict("legacy_cycle", "旧版任务来源存在循环，无法安全收养")
        if child.get("thread_id") is not None:
            raise ThreadConflict(
                "legacy_partial_thread",
                "旧版任务链已被部分收养，请联系平台修复",
            )
        if child.get("status") != "done":
            raise ThreadConflict(
                "legacy_nonfinal_child",
                "旧版任务链中还有未交付版本，请先等它收口",
            )
        chain.append(child)
        seen.add(child_id)
        cursor = child
    return chain


def _adopt_thread(
    connection,
    task: dict,
    tenant_id: int,
    actor_id: int | None,
    now: float,
) -> dict:
    chain = _legacy_linear_chain(connection, task)
    root, current = chain[0], chain[-1]
    root_identity = _task_identity(root)
    if any(_task_identity(member) != root_identity for member in chain):
        raise ThreadConflict(
            "employee_identity_mismatch",
            "旧版任务链的员工目录版本不一致，无法安全收养",
        )
    created_by = actor_id
    if created_by is None:
        created_by = root.get("created_by")
    cursor = connection.execute(
        "INSERT INTO task_thread(tenant_id,emp_idx,employee_key,"
        "employee_catalog_version,employee_identity_ref,"
        "employee_config_revision,employee_config_sha256,person_snapshot,"
        "identity_scheme,bundle_sha256,"
        "root_task_id,current_task_id,accepted_task_id,status,revision_count,"
        "created_by,satisfied_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,'active',?,?,NULL,?,?)",
        (
            tenant_id,
            int(root["emp_idx"]),
            root["employee_key"],
            root["employee_catalog_version"],
            root["employee_identity_ref"],
            root["employee_config_revision"],
            root["employee_config_sha256"],
            root["person_snapshot"],
            root["identity_scheme"],
            root["bundle_sha256"],
            int(root["id"]),
            int(current["id"]),
            len(chain),
            created_by,
            now,
            now,
        ),
    )
    thread_id = int(cursor.lastrowid)
    for revision_no, member in enumerate(chain, 1):
        changed = connection.execute(
            "UPDATE task SET thread_id=?,revision_no=?,phase=?,updated_at=? "
            "WHERE id=? AND tenant_id=? AND emp_idx=? AND thread_id IS NULL "
            "AND status='done' AND deleted_at IS NULL",
            (
                thread_id,
                revision_no,
                "delivery" if revision_no == 1 else "revision",
                now,
                member["id"],
                tenant_id,
                root["emp_idx"],
            ),
        )
        if changed.rowcount != 1:
            raise ThreadConflict(
                "adoption_race",
                "任务版本在收养时发生了变化，请刷新后重试",
            )
    return _thread_row(connection, thread_id, tenant_id)


def _ensure_thread_in_transaction(
    connection,
    task: dict,
    tenant_id: int,
    actor_id: int | None,
    now: float,
) -> dict:
    if task.get("thread_id") is None:
        return _adopt_thread(connection, task, tenant_id, actor_id, now)
    thread = _thread_row(connection, int(task["thread_id"]), tenant_id)
    _validate_thread_contract(
        connection,
        thread,
        tenant_id,
        member_task=task,
    )
    return thread


def _summary(
    connection,
    thread: dict,
    tenant_id: int,
    *,
    limit: int = 24,
) -> dict:
    limit = max(1, min(MAX_REVISIONS_RETURNED, int(limit or 24)))
    _root, current, total = _validate_thread_contract(
        connection, thread, tenant_id
    )
    rows = connection.execute(
        "SELECT id,revision_no,phase,status,created_at,updated_at,deleted_at "
        "FROM task WHERE thread_id=? AND tenant_id=? "
        "ORDER BY revision_no DESC LIMIT ?",
        (thread["id"], tenant_id, limit),
    ).fetchall()
    revisions = []
    for raw in reversed(rows):
        row = dict(raw)
        deleted = row.get("deleted_at") is not None
        task_id = int(row["id"])
        revisions.append({
            "task_id": task_id,
            "revision_no": int(row.get("revision_no") or 0),
            "phase": row.get("phase") or "revision",
            "status": "deleted" if deleted else row.get("status"),
            "is_current": task_id == int(thread["current_task_id"]),
            "is_accepted": (
                thread.get("accepted_task_id") is not None
                and task_id == int(thread["accepted_task_id"])
            ),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        })
    current_done = (
        current.get("status") == "done"
        and current.get("deleted_at") is None
    )
    recovery, recovery_blocked_by = _failed_revision_recovery(
        connection, thread, current, tenant_id
    )
    resumable = current if current_done else recovery
    can_continue_employee, can_accept_employee, blocked_code = (
        _employee_thread_policy(connection, int(thread["emp_idx"]))
    )
    return {
        "thread_id": int(thread["id"]),
        "status": thread["status"],
        "emp_idx": int(thread["emp_idx"]),
        "root_task_id": int(thread["root_task_id"]),
        "current_task_id": int(thread["current_task_id"]),
        "accepted_task_id": (
            int(thread["accepted_task_id"])
            if thread.get("accepted_task_id") is not None
            else None
        ),
        "revision_count": total,
        "can_continue": (
            thread["status"] == "active"
            and resumable is not None
            and can_continue_employee
        ),
        "can_accept": (
            thread["status"] == "active"
            and resumable is not None
            and can_accept_employee
        ),
        "continue_blocked_by": (
            blocked_code
            if not can_continue_employee
            else recovery_blocked_by
        ),
        "resume_task_id": int(resumable["id"]) if resumable else None,
        "failed_current_task_id": (
            int(current["id"]) if current.get("status") == "failed" else None
        ),
        "revisions": revisions,
        "revisions_truncated": total > len(revisions),
        "satisfied_at": thread.get("satisfied_at"),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
    }


def ensure_thread(
    task_id: int,
    tenant_id: int,
    actor_id: int | None = None,
    *,
    now: float | None = None,
    summary_limit: int = 24,
) -> dict:
    """Lazily adopt a completed legacy task/linear redo chain into a thread."""
    task_id = _positive_int(task_id, "任务编号")
    tenant_id = _positive_int(tenant_id, "企业编号")
    timestamp = _now(now)
    with db.atomic() as connection:
        task = _live_task(connection, task_id, tenant_id)
        thread = _ensure_thread_in_transaction(
            connection, task, tenant_id, actor_id, timestamp
        )
        return _summary(
            connection, thread, tenant_id, limit=summary_limit
        )


def _idempotent_replay(
    connection,
    *,
    task_id: int,
    tenant_id: int,
    request_key: str,
    feedback: str,
    material: str,
    expected_decision_evidence,
    actor_id: int | None,
    expected_emp_idx: int | None,
    summary_limit: int,
) -> dict | None:
    existing_row = connection.execute(
        "SELECT * FROM task WHERE tenant_id=? AND request_key=?",
        (tenant_id, request_key),
    ).fetchone()
    if not existing_row:
        return None
    existing = dict(existing_row)
    brief = db.jloads(existing.get("brief_json"), {})
    same_feedback = (
        isinstance(brief, dict)
        and str(brief.get("feedback") or "").strip() == feedback
    )
    source_row = connection.execute(
        "SELECT brief_json FROM task WHERE id=? AND tenant_id=?",
        (task_id, tenant_id),
    ).fetchone()
    source_brief = db.jloads(source_row["brief_json"], {}) if source_row else {}
    expected_material = _combined_material(
        source_brief.get("material") if isinstance(source_brief, dict) else "",
        material,
    )
    same_material = (
        isinstance(brief, dict)
        and str(brief.get("material") or "").strip() == expected_material
    )
    same_revision_material = (
        isinstance(brief, dict)
        and str(brief.get("revision_material") or "").strip() == material
    )
    same_decision_evidence = (
        isinstance(brief, dict)
        and brief.get("decision_evidence") == expected_decision_evidence
    )
    same_actor = (
        actor_id is None
        or existing.get("created_by") is None
        or int(existing["created_by"]) == int(actor_id)
    )
    same_employee = (
        expected_emp_idx is None
        or int(existing["emp_idx"]) == int(expected_emp_idx)
    )
    if (
        existing.get("deleted_at") is not None
        or int(existing.get("source_task_id") or 0) != task_id
        or existing.get("thread_id") is None
        or existing.get("phase") != "revision"
        or not same_feedback
        or not same_material
        or not same_revision_material
        or not same_decision_evidence
        or not same_actor
        or not same_employee
    ):
        raise IdempotencyConflict(
            "request_key_reused",
            "这个请求编号已用于其他修改，请刷新页面后重试",
        )
    thread = _thread_row(connection, int(existing["thread_id"]), tenant_id)
    _validate_thread_contract(
        connection,
        thread,
        tenant_id,
        member_task=existing,
    )
    return {
        "created": False,
        "task_id": int(existing["id"]),
        "thread": _summary(
            connection, thread, tenant_id, limit=summary_limit
        ),
    }


def _normalized_revision_brief(
    current_brief: str,
    output_md: str,
    feedback: str,
    material: str,
    *,
    employee: dict,
    tenant_id: int,
    decision_evidence,
) -> dict:
    base = db.jloads(current_brief, {})
    if not isinstance(base, dict):
        raise ThreadConflict(
            "invalid_root_brief",
            "首版任务书格式异常，无法继续生成",
        )
    from .taskrunner import validate_persisted_task_brief

    try:
        base, _current_manifest = validate_persisted_task_brief(
            base, employee, tenant_id
        )
    except ValueError as exc:
        raise ThreadConflict("invalid_root_brief", str(exc)) from exc
    base.pop("feedback", None)
    base.pop("prev_excerpt", None)
    base["feedback"] = feedback
    combined_material = _combined_material(base.get("material"), material)
    if combined_material:
        base["material"] = combined_material
    else:
        base.pop("material", None)
    if material:
        base["revision_material"] = material
    else:
        base.pop("revision_material", None)
    base["prev_excerpt"] = (output_md or "")[:MAX_PREVIOUS_EXCERPT_CHARS]
    if decision_evidence is not None:
        base["decision_evidence"] = decision_evidence
    else:
        base.pop("decision_evidence", None)
    # Import lazily so this persistence module stays usable by migrations and
    # avoids making the worker engine part of its import-time dependency graph.
    from .taskrunner import normalize_brief

    try:
        manifest = base.pop("decision_evidence", None)
        clean = normalize_brief(base)
        if manifest is not None:
            clean["decision_evidence"] = manifest
        return clean
    except ValueError as exc:
        raise ThreadConflict("invalid_root_brief", str(exc)) from exc


def create_followup(
    task_id: int,
    tenant_id: int,
    request_key: str,
    feedback: str,
    create_task: Callable[[dict, str], int],
    *,
    material: str = "",
    evidence_items=EVIDENCE_ITEMS_ABSENT,
    actor_id: int | None = None,
    expected_emp_idx: int | None = None,
    now: float | None = None,
    summary_limit: int = 24,
) -> dict:
    """Create one paid next revision and atomically advance the thread.

    ``create_task`` must synchronously create and charge the task represented by
    the supplied data, returning its id.  It is called inside ``db.atomic``;
    nested ``db.atomic``/billing savepoints are supported and tested.  Worker
    launch belongs to the HTTP/service adapter *after* this function commits.
    """
    task_id = _positive_int(task_id, "任务编号")
    tenant_id = _positive_int(tenant_id, "企业编号")
    request_key = _clean_request_key(request_key)
    feedback = _clean_feedback(feedback)
    material = _clean_material(material)
    if not callable(create_task):
        raise InvalidFollowup("invalid_creator", "任务创建器无效")
    if expected_emp_idx is not None:
        expected_emp_idx = _employee_int(expected_emp_idx)
    timestamp = _now(now)

    with db.atomic() as connection:
        # 先锁定来源任务与员工状态，再处理幂等重放。这既避免
        # 请求编号侧信道，也确保停用/巡店员工不能借重放入口继续。
        task = _live_task(connection, task_id, tenant_id)
        if (
            expected_emp_idx is not None
            and int(task["emp_idx"]) != expected_emp_idx
        ):
            raise ThreadConflict(
                "employee_mismatch",
                "该任务不属于当前数字员工",
            )
        _require_followup_employee(connection, int(task["emp_idx"]))
        from . import departments, employeeidentity

        employee = employeeidentity.resolve_task(task)
        if not employee:
            raise ThreadConflict(
                "employee_identity_mismatch",
                "任务的员工目录版本无法验证，已停止继续生成",
            )
        source_brief = db.jloads(task.get("brief_json"), {})
        if not isinstance(source_brief, dict):
            raise ThreadConflict("invalid_root_brief", "首版任务书格式异常，无法继续生成")
        existing_manifest = source_brief.get("decision_evidence")
        if departments.is_decision_employee(employee):
            try:
                if existing_manifest is not None:
                    existing_manifest = departments.validate_decision_evidence(
                        employee, tenant_id, existing_manifest
                    )
                if evidence_items is EVIDENCE_ITEMS_ABSENT:
                    expected_decision_evidence = existing_manifest
                else:
                    expected_decision_evidence = departments.normalize_decision_evidence(
                        employee,
                        tenant_id,
                        evidence_items,
                        base_manifest=existing_manifest,
                    )
            except ValueError as exc:
                raise InvalidFollowup("invalid_evidence_items", str(exc)) from exc
        else:
            # V1 keeps its historical request semantics: the newly introduced
            # evidence field is ignored and can never enter its brief/prompt.
            if existing_manifest is not None:
                raise ThreadConflict(
                    "invalid_root_brief", "非 V2 任务带有无效决策证据"
                )
            expected_decision_evidence = None
        replay = _idempotent_replay(
            connection,
            task_id=task_id,
            tenant_id=tenant_id,
            request_key=request_key,
            feedback=feedback,
            material=material,
            expected_decision_evidence=expected_decision_evidence,
            actor_id=actor_id,
            expected_emp_idx=expected_emp_idx,
            summary_limit=summary_limit,
        )
        if replay is not None:
            return replay

        if task.get("thread_id") is None:
            if task.get("status") != "done":
                raise ThreadConflict(
                    "task_not_done",
                    "这一版还没交付，请等它完成后再继续沟通",
                )
            thread = _ensure_thread_in_transaction(
                connection, task, tenant_id, actor_id, timestamp
            )
        else:
            thread = _thread_row(connection, int(task["thread_id"]), tenant_id)
            _validate_thread_contract(
                connection, thread, tenant_id, member_task=task
            )
        # Adoption updates the row in this same transaction; reload instead of
        # validating the pre-adoption snapshot whose thread_id was still NULL.
        task = _live_task(connection, task_id, tenant_id)
        _root, current, _count = _validate_thread_contract(
            connection,
            thread,
            tenant_id,
            member_task=task,
        )
        if thread["status"] == "satisfied":
            raise ThreadConflict(
                "thread_satisfied",
                "这个协作会话已标记满意；新需求请重新派活",
            )
        if int(thread["current_task_id"]) != task_id:
            raise ThreadConflict(
                "stale_revision",
                "这不是最新版，请打开当前版本后再继续沟通",
            )
        context_task = current if current.get("status") == "done" else None
        if context_task is None:
            context_task, blocked_by = _failed_revision_recovery(
                connection, thread, current, tenant_id
            )
            if context_task is None:
                messages = {
                    "free_retry_available": "这一轮还可免费重试，先用完免费重试再开新一轮",
                    "refund_pending": "这一轮的退点还未安全完成，请稍后再试",
                    "no_delivered_revision": "这个会话还没有可以继续的已交付版本",
                }
                raise ThreadConflict(
                    blocked_by or "task_not_done",
                    messages.get(
                        blocked_by,
                        "这一版还没交付，请等它完成后再继续沟通",
                    ),
                )
        active = connection.execute(
            "SELECT id FROM task WHERE thread_id=? AND tenant_id=? "
            "AND deleted_at IS NULL AND status IN ('pending_charge','queued','running') "
            "LIMIT 1",
            (thread["id"], tenant_id),
        ).fetchone()
        if active:
            raise ThreadConflict(
                "active_revision_exists",
                "这个员工正在生成新版，请等它交付后再追问",
            )

        current_body = connection.execute(
            "SELECT brief_json FROM task WHERE id=? AND tenant_id=?",
            (task_id, tenant_id),
        ).fetchone()
        context_body = connection.execute(
            "SELECT output_md FROM task WHERE id=? AND tenant_id=? "
            "AND status='done' AND deleted_at IS NULL",
            (int(context_task["id"]), tenant_id),
        ).fetchone()
        if not current_body or not context_body:
            raise ThreadConflict(
                "missing_revision_context",
                "这个协作会话的上下文不完整",
            )
        brief = _normalized_revision_brief(
            current_body["brief_json"], context_body["output_md"] or "", feedback,
            material,
            employee=employee,
            tenant_id=tenant_id,
            decision_evidence=expected_decision_evidence,
        )
        next_revision = int(current["revision_no"]) + 1
        task_data = {
            "emp_idx": int(task["emp_idx"]),
            **{field: task[field] for field in _TASK_IDENTITY_FIELDS},
            "tenant_id": tenant_id,
            "source_task_id": task_id,
            "thread_id": int(thread["id"]),
            "revision_no": next_revision,
            "phase": "revision",
            "request_key": request_key,
            "brief_json": json.dumps(brief, ensure_ascii=False),
        }
        new_task_id = create_task(
            task_data,
            f"第{next_revision}轮继续沟通",
        )
        new_task_id = _positive_int(new_task_id, "新任务编号")
        created_row = connection.execute(
            "SELECT * FROM task WHERE id=?",
            (new_task_id,),
        ).fetchone()
        created = dict(created_row) if created_row else None
        if (
            not created
            or int(created.get("tenant_id") or 0) != tenant_id
            or int(created.get("emp_idx") or 0) != int(thread["emp_idx"])
            or _task_identity(created) != _task_identity(task)
            or int(created.get("source_task_id") or 0) != task_id
            or int(created.get("thread_id") or 0) != int(thread["id"])
            or int(created.get("revision_no") or 0) != next_revision
            or created.get("phase") != "revision"
            or created.get("request_key") != request_key
            or created.get("deleted_at") is not None
            or created.get("status") not in ACTIVE_TASK_STATUSES
            or created.get("billing_status") not in {"charged", "included"}
        ):
            raise ThreadConflict(
                "created_task_mismatch",
                "新版任务未按协作会话契约完整创建，本次已回滚",
            )
        advanced = connection.execute(
            "UPDATE task_thread SET current_task_id=?,revision_count=?,updated_at=? "
            "WHERE id=? AND tenant_id=? AND status='active' "
            "AND current_task_id=? AND revision_count=?",
            (
                new_task_id,
                next_revision,
                timestamp,
                thread["id"],
                tenant_id,
                task_id,
                next_revision - 1,
            ),
        )
        if advanced.rowcount != 1:
            raise ThreadConflict(
                "thread_cas_lost",
                "协作会话已被另一个请求推进，本次已回滚",
            )
        updated = _thread_row(connection, int(thread["id"]), tenant_id)
        return {
            "created": True,
            "task_id": new_task_id,
            "thread": _summary(
                connection, updated, tenant_id, limit=summary_limit
            ),
        }


def mark_satisfied(
    task_id: int,
    tenant_id: int,
    actor_id: int | None = None,
    *,
    expected_emp_idx: int | None = None,
    now: float | None = None,
    summary_limit: int = 24,
) -> dict:
    """Accept the current completed revision and close the thread."""
    task_id = _positive_int(task_id, "任务编号")
    tenant_id = _positive_int(tenant_id, "企业编号")
    if expected_emp_idx is not None:
        expected_emp_idx = _employee_int(expected_emp_idx)
    timestamp = _now(now)
    with db.atomic() as connection:
        task = _live_task(connection, task_id, tenant_id)
        if (
            expected_emp_idx is not None
            and int(task["emp_idx"]) != expected_emp_idx
        ):
            raise ThreadConflict(
                "employee_mismatch", "该任务不属于当前数字员工"
            )
        _require_accept_employee(connection, int(task["emp_idx"]))
        if task.get("status") != "done":
            raise ThreadConflict(
                "task_not_done",
                "只有已交付的当前版本才能标记满意",
            )
        thread = _ensure_thread_in_transaction(
            connection, task, tenant_id, actor_id, timestamp
        )
        task = _live_task(connection, task_id, tenant_id)
        _root, current, _count = _validate_thread_contract(
            connection, thread, tenant_id, member_task=task
        )
        if thread["status"] == "satisfied":
            if int(thread.get("accepted_task_id") or 0) == task_id:
                return _summary(
                    connection, thread, tenant_id, limit=summary_limit
                )
            raise ThreadConflict(
                "thread_satisfied",
                "这个协作会话已以其他版本标记满意",
            )
        current_id = int(thread["current_task_id"])
        acceptable = current_id == task_id and current.get("status") == "done"
        if not acceptable:
            recovery, _blocked_by = _failed_revision_recovery(
                connection, thread, current, tenant_id
            )
            acceptable = bool(
                recovery is not None and int(recovery["id"]) == task_id
            )
        if not acceptable:
            raise ThreadConflict(
                "stale_revision",
                "只能验收最新可用的已交付版本",
            )
        active = connection.execute(
            "SELECT id FROM task WHERE thread_id=? AND tenant_id=? "
            "AND deleted_at IS NULL AND status IN ('pending_charge','queued','running') "
            "LIMIT 1",
            (thread["id"], tenant_id),
        ).fetchone()
        if active:
            raise ThreadConflict(
                "active_revision_exists",
                "新版正在生成，暂时不能标记满意",
            )
        changed = connection.execute(
            "UPDATE task_thread SET status='satisfied',accepted_task_id=?,"
            "satisfied_at=?,updated_at=? WHERE id=? AND tenant_id=? "
            "AND status='active' AND current_task_id=?",
            (
                task_id,
                timestamp,
                timestamp,
                thread["id"],
                tenant_id,
                current_id,
            ),
        )
        if changed.rowcount != 1:
            raise ThreadConflict(
                "thread_cas_lost",
                "协作会话状态已发生变化，请刷新后重试",
            )
        updated = _thread_row(connection, int(thread["id"]), tenant_id)
        return _summary(
            connection, updated, tenant_id, limit=summary_limit
        )


def thread_summary_for_task(
    task_id: int,
    tenant_id: int,
    *,
    limit: int = 24,
) -> dict:
    """Return bounded metadata for a task's existing thread without adopting."""
    task_id = _positive_int(task_id, "任务编号")
    tenant_id = _positive_int(tenant_id, "企业编号")
    connection = db.conn()
    task = _live_task(connection, task_id, tenant_id)
    if task.get("thread_id") is None:
        can_continue_employee, can_accept_employee, blocked_code = (
            _employee_thread_policy(connection, int(task["emp_idx"]))
        )
        done = task.get("status") == "done"
        return {
            "thread_id": None,
            "status": "standalone",
            "emp_idx": int(task["emp_idx"]),
            "root_task_id": task_id,
            "current_task_id": task_id,
            "accepted_task_id": None,
            "revision_count": 1,
            "can_continue": done and can_continue_employee,
            "can_accept": done and can_accept_employee,
            "continue_blocked_by": (
                blocked_code if not can_continue_employee else None
            ),
            "revisions": [{
                "task_id": task_id,
                "revision_no": 1,
                "phase": "delivery",
                "status": task.get("status"),
                "is_current": True,
                "is_accepted": False,
                "created_at": task.get("created_at"),
                "updated_at": task.get("updated_at"),
            }],
            "revisions_truncated": False,
            "satisfied_at": None,
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
        }
    thread = _thread_row(connection, int(task["thread_id"]), tenant_id)
    _validate_thread_contract(
        connection, thread, tenant_id, member_task=task
    )
    return _summary(connection, thread, tenant_id, limit=limit)


def _task_deletion_guard(connection, task: dict, tenant_id: int) -> dict:
    """Evaluate the soft-delete invariant on the caller's DB snapshot."""
    task_id = int(task["id"])
    if int(task.get("emp_idx") or 0) == INSPECTION_EMPLOYEE_IDX:
        return {
            "allowed": False,
            "code": "inspection_audit_record",
            "message": "巡店任务是问题、整改和复核的审计记录，不能单独删除",
        }
    children = connection.execute(
        "SELECT id,thread_id FROM task WHERE source_task_id=? AND tenant_id=? "
        "AND deleted_at IS NULL LIMIT 2",
        (task_id, tenant_id),
    ).fetchall()
    if task.get("thread_id") is None:
        if children:
            return {
                "allowed": False,
                "code": "legacy_parent",
                "message": "该旧任务还有后续版本，不能删除来源锚点",
            }
        return {"allowed": True, "code": "standalone", "message": ""}
    thread = _thread_row(connection, int(task["thread_id"]), tenant_id)
    _validate_thread_contract(
        connection, thread, tenant_id, member_task=task
    )
    if int(thread["root_task_id"]) == task_id:
        return {
            "allowed": False,
            "code": "thread_root",
            "message": "这是协作会话的首版锚点，不能单独删除",
        }
    if int(thread["current_task_id"]) == task_id:
        return {
            "allowed": False,
            "code": "thread_current",
            "message": "这是协作会话的当前版本，不能单独删除",
        }
    if (
        thread.get("accepted_task_id") is not None
        and int(thread["accepted_task_id"]) == task_id
    ):
        return {
            "allowed": False,
            "code": "thread_accepted",
            "message": "这是已确认满意的版本，不能单独删除",
        }
    foreign_children = [
        row for row in children
        if int(row["thread_id"] or 0) != int(thread["id"])
    ]
    if foreign_children:
        return {
            "allowed": False,
            "code": "external_child",
            "message": "该任务还被其他后续任务引用，不能删除",
        }
    return {"allowed": True, "code": "historical_revision", "message": ""}


def task_deletion_guard(
    task_id: int,
    tenant_id: int,
    *,
    connection=None,
) -> dict:
    """Protect live thread anchors while allowing unrelated soft deletion."""
    task_id = _positive_int(task_id, "任务编号")
    tenant_id = _positive_int(tenant_id, "企业编号")
    connection = connection or db.conn()
    task = _live_task(connection, task_id, tenant_id)
    return _task_deletion_guard(connection, task, tenant_id)


def soft_delete_task(
    task_id: int,
    tenant_id: int,
    *,
    actor_id: int | None,
    reason: str = "用户移入回收站",
    now: float | None = None,
) -> dict:
    """Guard and soft-delete in one BEGIN IMMEDIATE transaction.

    Billing settlement/worker cancellation belongs to the caller and must have
    reached a terminal state first.  Rechecking the complete thread invariant
    in this same write transaction closes the former follow-up-vs-delete race.
    """
    task_id = _positive_int(task_id, "任务编号")
    tenant_id = _positive_int(tenant_id, "企业编号")
    timestamp = _now(now)
    with db.atomic() as connection:
        task = _live_task(connection, task_id, tenant_id)
        guard = _task_deletion_guard(connection, task, tenant_id)
        if not guard.get("allowed"):
            raise ThreadConflict(
                str(guard.get("code") or "delete_blocked"),
                str(guard.get("message") or "该任务不能单独删除"),
            )
        if task.get("status") not in {"done", "failed"}:
            raise ThreadConflict(
                "task_not_terminal", "任务还未安全收口，暂时不能移入回收站"
            )
        if (
            task.get("status") == "failed"
            and task.get("billing_status") == "charged"
        ):
            raise ThreadConflict(
                "refund_pending", "任务退点尚未完成，请稍后重试删除"
            )
        changed = connection.execute(
            "UPDATE task SET deleted_at=?,deleted_by=?,delete_reason=?,updated_at=? "
            "WHERE id=? AND tenant_id=? AND deleted_at IS NULL "
            "AND status IN ('done','failed')",
            (
                timestamp,
                actor_id,
                str(reason or "用户移入回收站")[:160],
                timestamp,
                task_id,
                tenant_id,
            ),
        )
        if changed.rowcount != 1:
            raise ThreadConflict(
                "delete_cas_lost", "任务状态刚刚发生变化，请刷新后再删除"
            )
        return {
            "ok": True,
            "soft_deleted": True,
            "deleted_at": timestamp,
            "guard_code": guard["code"],
        }


def restore_task(
    task_id: int,
    tenant_id: int,
    *,
    now: float | None = None,
) -> dict:
    """Restore a task while preserving any existing thread audit invariant."""
    task_id = _positive_int(task_id, "任务编号")
    tenant_id = _positive_int(tenant_id, "企业编号")
    timestamp = _now(now)
    with db.atomic() as connection:
        raw = connection.execute(
            "SELECT * FROM task WHERE id=? AND tenant_id=? "
            "AND deleted_at IS NOT NULL",
            (task_id, tenant_id),
        ).fetchone()
        if not raw:
            raise TaskThreadNotFound(
                "deleted_task_not_found", "回收站中没有这条任务"
            )
        task = dict(raw)
        thread = None
        if task.get("thread_id") is not None:
            thread = _thread_row(
                connection, int(task["thread_id"]), tenant_id
            )
            # 新版本仅允许删除非锚点历史版；此校验保证恢复也不会
            # 把一条已损坏/跨租户的记录重新暴露到会话中。
            _validate_thread_contract(
                connection, thread, tenant_id, member_task=task
            )
        else:
            hidden_anchor = connection.execute(
                "SELECT 1 FROM task_thread WHERE tenant_id=? AND "
                "(root_task_id=? OR current_task_id=? OR accepted_task_id=?) "
                "LIMIT 1",
                (tenant_id, task_id, task_id, task_id),
            ).fetchone()
            threaded_child = connection.execute(
                "SELECT 1 FROM task WHERE tenant_id=? AND source_task_id=? "
                "AND thread_id IS NOT NULL LIMIT 1",
                (tenant_id, task_id),
            ).fetchone()
            if hidden_anchor or threaded_child:
                raise ThreadConflict(
                    "restore_thread_mismatch",
                    "任务的协作会话归属不完整，已停止恢复以避免破坏审计链",
                )
        changed = connection.execute(
            "UPDATE task SET deleted_at=NULL,deleted_by=NULL,delete_reason=NULL,"
            "updated_at=? WHERE id=? AND tenant_id=? AND deleted_at IS NOT NULL",
            (timestamp, task_id, tenant_id),
        )
        if changed.rowcount != 1:
            raise ThreadConflict(
                "restore_cas_lost", "任务状态刚刚发生变化，请刷新后再恢复"
            )
        if thread is not None:
            restored = dict(task)
            restored["deleted_at"] = None
            _validate_thread_contract(
                connection, thread, tenant_id, member_task=restored
            )
        return {"ok": True, "restored": True, "id": task_id}


def task_hard_delete_guard(
    task_id: int,
    tenant_id: int,
    *,
    include_deleted: bool = False,
    connection=None,
) -> dict:
    """Physical purge guard: threaded/source tasks remain durable anchors."""
    task_id = _positive_int(task_id, "任务编号")
    tenant_id = _positive_int(tenant_id, "企业编号")
    connection = connection or db.conn()
    if include_deleted:
        raw = connection.execute(
            "SELECT * FROM task WHERE id=? AND tenant_id=?",
            (task_id, tenant_id),
        ).fetchone()
        if not raw:
            raise TaskThreadNotFound("task_not_found", "任务不存在")
        task = dict(raw)
    else:
        task = _live_task(connection, task_id, tenant_id)
    if task.get("thread_id") is not None:
        return {
            "allowed": False,
            "code": "thread_history",
            "message": "协作会话版本必须保留审计链，不能物理删除",
        }
    if int(task.get("emp_idx") or 0) == INSPECTION_EMPLOYEE_IDX:
        return {
            "allowed": False,
            "code": "inspection_audit_record",
            "message": "巡店任务必须保留整改与复核审计链，不能物理删除",
        }
    referenced = connection.execute(
        "SELECT 1 FROM task WHERE source_task_id=? AND tenant_id=? LIMIT 1",
        (task_id, tenant_id),
    ).fetchone()
    anchored = connection.execute(
        "SELECT 1 FROM task_thread WHERE tenant_id=? AND "
        "(root_task_id=? OR current_task_id=? OR accepted_task_id=?) LIMIT 1",
        (tenant_id, task_id, task_id, task_id),
    ).fetchone()
    inspection_ref = connection.execute(
        "SELECT 1 FROM inspection_visit WHERE tenant_id=? AND task_id=? LIMIT 1",
        (tenant_id, task_id),
    ).fetchone()
    if referenced or anchored or inspection_ref:
        return {
            "allowed": False,
            "code": "referenced_task",
            "message": "任务仍是后续版本的来源锚点，不能物理删除",
        }
    return {"allowed": True, "code": "unreferenced", "message": ""}
