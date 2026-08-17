"""行业老板看板的纯读服务层。

这个模块只读取任务和巡店表中的结构化字段，不读取任务描述、
产出正文、
巡店总结、问题详情或整改正文。行业授权只信任 ``tenant_industry`` 的显式
映射：空映射、旧 ``industries_json`` 或未完成的迁移都不会被解释为
“全行业”。

路由层应将当前登录用户作为 ``actor`` 传入，并且只在已经通过命名 boss
校验时传 ``is_boss=True``。单独传该布尔值不会让 owner 升权；全局访问同时
要求 ``role=root``。
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from typing import Any

from . import db, departments
from .skills import registry


MIN_DAYS = 1
MAX_DAYS = 90
DEFAULT_DAYS = 30
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_OFFSET = 100_000
RECENT_ACTIVITY_LIMIT = 12
STALE_AFTER_SECONDS = 24 * 60 * 60
INSPECTION_EMPLOYEE_IDX = 10

_TASK_IDENTITY_FIELDS = (
    "employee_key",
    "employee_catalog_version",
    "employee_name_snapshot",
    "employee_dept_key",
    "employee_spec_sha256",
)
_SPEC_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Every report query joins against this bounded JSON roster.  Matching the
# complete frozen identity prevents a row with the same numeric idx but a
# different catalog/name/department/specification from being attributed to a
# current employee or to a successor.
_TASK_IDENTITY_MATCH_SQL = """
EXISTS (
  SELECT 1 FROM json_each(?) frozen_identity
  WHERE CAST(json_extract(frozen_identity.value,'$.idx') AS INTEGER)=task.emp_idx
    AND json_extract(frozen_identity.value,'$.employee_key')=task.employee_key
    AND json_extract(frozen_identity.value,'$.employee_catalog_version')=
        task.employee_catalog_version
    AND json_extract(frozen_identity.value,'$.employee_name_snapshot')=
        task.employee_name_snapshot
    AND json_extract(frozen_identity.value,'$.employee_dept_key')=
        task.employee_dept_key
    AND json_extract(frozen_identity.value,'$.employee_spec_sha256')=
        task.employee_spec_sha256
)
"""

# 行业经营指标只定义“老板应关心什么”。在订单、POS、
# 排班或库存等可信数据源接入前，值始终为 None，绝不用任务数伪造经营数。
INDUSTRY_METRIC_CATALOG: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "restaurant": (
        ("sales", "营业额", "元", "POS/收银系统"),
        ("labor_cost_ratio", "人工成本率", "%", "排班+工资+营业额"),
        ("food_cost_ratio", "食材成本率", "%", "采购+库存+营业额"),
        ("table_turnover", "翻台率", "次/台", "POS+桌台数"),
        ("service_complaint_rate", "服务投诉率", "%", "客诉+订单数"),
    ),
    "tea_coffee": (
        ("sales", "营业额", "元", "POS/收银系统"),
        ("cups", "出杯数", "杯", "POS订单明细"),
        ("peak_wait", "高峰等待时长", "分钟", "叫号/制作时间"),
        ("waste_rate", "原料损耗率", "%", "库存+报损"),
    ),
    "convenience": (
        ("sales", "营业额", "元", "POS"),
        ("gross_margin", "毛利率", "%", "POS+采购成本"),
        ("inventory_turnover", "库存周转", "次", "库存+销售"),
        ("stockout_rate", "缺货率", "%", "库存+商品档案"),
    ),
    "grocery": (
        ("sales", "营业额", "元", "POS"),
        ("gross_margin", "毛利率", "%", "POS+采购成本"),
        ("fresh_loss_rate", "生鲜损耗率", "%", "称重+报损+库存"),
        ("inventory_turnover", "库存周转", "次", "库存+销售"),
    ),
    "snack": (
        ("sales", "营业额", "元", "POS"),
        ("gross_margin", "毛利率", "%", "POS+采购成本"),
        ("inventory_turnover", "库存周转", "次", "库存+销售"),
        ("expiry_loss_rate", "临期损耗率", "%", "效期+报损"),
    ),
    "auto": (
        ("work_orders", "工单数", "单", "DMS/工单系统"),
        ("bay_productivity", "工位产能", "单/工位", "工单+工位"),
        ("first_time_fix", "一次修复率", "%", "工单+返修记录"),
        ("parts_turnover", "配件周转", "次", "配件库存+领料"),
    ),
    "beauty": (
        ("appointment_conversion", "预约到店率", "%", "预约+到店"),
        ("repeat_rate", "复购率", "%", "CRM+订单"),
        ("room_utilization", "房间利用率", "%", "排班+房间"),
        ("service_cycle", "平均服务时长", "分钟", "服务开始/结束时间"),
    ),
    "fitness": (
        ("new_members", "新增会员", "人", "会员系统"),
        ("renewal_rate", "续费率", "%", "会员合同"),
        ("attendance_rate", "到场率", "%", "预约+门禁"),
        ("class_utilization", "课程满班率", "%", "课程+预约"),
    ),
    "hotel": (
        ("occupancy", "入住率", "%", "PMS"),
        ("adr", "平均房价 ADR", "元", "PMS"),
        ("revpar", "每间可售房收入 RevPAR", "元", "PMS"),
        ("room_clean_cycle", "客房清扫时长", "分钟", "客房任务系统"),
    ),
    "pet": (
        ("service_orders", "服务订单", "单", "POS/预约"),
        ("repeat_rate", "复购率", "%", "CRM+订单"),
        ("appointment_utilization", "预约产能利用率", "%", "预约+排班"),
        ("inventory_turnover", "商品库存周转", "次", "库存+销售"),
    ),
    "pharmacy": (
        ("sales", "营业额", "元", "POS"),
        ("gross_margin", "毛利率", "%", "POS+采购成本"),
        ("inventory_turnover", "库存周转", "次", "库存+销售"),
        ("expiry_loss_rate", "近效期损耗率", "%", "批号效期+报损"),
        ("compliance_events", "合规异常", "次", "质量/审方记录"),
    ),
}

ACTIVE_STATUSES = frozenset(
    {
        "queued",
        "running",
        "brainstorm",
        "validate",
        "execute",
        "executing",
        "pending_charge",
        "processing",
        "submitting",
        "submitted",
    }
)
WAITING_STATUSES = frozenset(
    {
        "awaiting_review",
        "gate_blocked",
        "awaiting_execution",
        "paused",
        "blocked",
    }
)
COMPLETED_STATUSES = frozenset({"done", "completed", "success", "succeed"})
FAILED_STATUSES = frozenset({"failed", "error"})
CANCELLED_STATUSES = frozenset({"cancelled", "stopped"})

INSPECTION_COMPLETED_STATUSES = frozenset(
    {"done", "completed", "closed", "success", "succeed"}
)
INSPECTION_FAILED_STATUSES = frozenset({"failed", "error", "cancelled"})
CLOSED_ISSUE_STATUSES = frozenset(
    {"done", "completed", "closed", "resolved", "cancelled"}
)
CRITICAL_SEVERITIES = frozenset(
    {"critical", "high", "urgent", "严重", "高", "紧急"}
)

_INSPECTION_CORE_COLUMNS = {
    "store_branch": {"id", "tenant_id", "industry_key"},
    "inspection_visit": {
        "id",
        "tenant_id",
        "industry_key",
        "branch_id",
        "status",
        "score",
        "employee_idx",
        "task_id",
        "visit_at",
        "created_at",
        "updated_at",
        "completed_at",
        "terminal_at",
    },
    "inspection_issue": {
        "id", "tenant_id", "visit_id", "severity", "status", "due_at",
    },
    "inspection_action": {
        "id", "tenant_id", "visit_id", "issue_id", "status", "due_at",
        "closed_at",
    },
}


class DashboardError(RuntimeError):
    """老板看板服务层错误基类。"""


class DashboardAccessDenied(DashboardError):
    """调用者没有该看板范围的权限。"""


class DashboardScopeUnavailable(DashboardError):
    """租户/行业不存在、已停用或没有显式授权。"""


class DashboardValidationError(DashboardError):
    """请求参数超出明确边界。"""


def _quoted(values: frozenset[str]) -> str:
    """只对模块内静态枚举生成 SQL 字面量。"""
    return ",".join("'" + value.replace("'", "''") + "'" for value in sorted(values))


def _status_case(column: str) -> str:
    normalized = f"LOWER(COALESCE({column},'queued'))"
    return (
        "CASE "
        f"WHEN {normalized} IN ({_quoted(ACTIVE_STATUSES)}) THEN 'active' "
        f"WHEN {normalized} IN ({_quoted(WAITING_STATUSES)}) THEN 'waiting' "
        f"WHEN {normalized} IN ({_quoted(COMPLETED_STATUSES)}) THEN 'completed' "
        f"WHEN {normalized} IN ({_quoted(FAILED_STATUSES)}) THEN 'failed' "
        f"WHEN {normalized} IN ({_quoted(CANCELLED_STATUSES)}) THEN 'cancelled' "
        "ELSE 'active' END"
    )


TASK_STATUS_CASE = _status_case("status")
TASK_STATUS_CASE_T = _status_case("t.status")


def _status_group(value: Any) -> str:
    normalized = str(value or "queued").strip().lower()
    if normalized in ACTIVE_STATUSES:
        return "active"
    if normalized in WAITING_STATUSES:
        return "waiting"
    if normalized in COMPLETED_STATUSES:
        return "completed"
    if normalized in FAILED_STATUSES:
        return "failed"
    if normalized in CANCELLED_STATUSES:
        return "cancelled"
    return "active"


def _actor_scope(actor: Mapping[str, Any] | None, is_boss: bool) -> tuple[int, bool]:
    """在执行任何数据库查询前拒绝非管理账号。"""
    if not isinstance(actor, Mapping):
        raise DashboardAccessDenied("需要企业主账号")
    if actor.get("enabled") != 1:
        raise DashboardAccessDenied("账号不可用")
    role = str(actor.get("role") or "").strip().lower()
    if role not in {"owner", "root"}:
        raise DashboardAccessDenied("仅企业主可查看老板看板")
    tenant_id = actor.get("tenant_id")
    if isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id <= 0:
        raise DashboardAccessDenied("账号租户范围无效")
    return tenant_id, bool(is_boss and role == "root")


def _industry_catalog() -> dict[str, dict[str, Any]]:
    """只将公开部门元数据暴露给看板，不返回员工档案。"""
    catalog: dict[str, dict[str, Any]] = {}
    for item in departments.list_depts():
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        catalog[key] = {
            "key": key,
            "name": str(item.get("name") or key),
            "emoji": str(item.get("emoji") or ""),
        }
    return catalog


def _employees_for_industry(industry_key: str) -> dict[int, dict[str, Any]]:
    employees: dict[int, dict[str, Any]] = {}
    for idx, employee in departments.specialists().items():
        if str(employee.get("dept_key") or "") != industry_key:
            continue
        employees[int(idx)] = {
            "idx": int(idx),
            "name": str(employee.get("name") or f"员工 {idx}"),
            "employee_key": str(employee.get("key") or ""),
            "employee_catalog_version": str(
                employee.get("catalog_version") or ""
            ),
            "employee_name_snapshot": str(employee.get("name") or ""),
            "employee_dept_key": str(employee.get("dept_key") or ""),
            "employee_spec_sha256": str(
                employee.get("employee_spec_sha256") or ""
            ),
            "roster_status": "active",
            "can_assign": True,
        }
    return employees


def _identity_signature(row: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Return one trustworthy frozen task identity, or fail closed.

    Reporting treats the persisted snapshot as the historical authority.  The
    explicit schema53 unknown marker is intentionally not accepted as an
    industry identity: it has no provable department and must never be folded
    into an active roster merely because its numeric idx happens to resemble
    another employee.
    """
    try:
        employee_idx = int(row.get("emp_idx", row.get("idx")))
    except (TypeError, ValueError):
        return None
    if employee_idx < 1 or employee_idx == INSPECTION_EMPLOYEE_IDX:
        return None
    values = []
    for field in _TASK_IDENTITY_FIELDS:
        raw = row.get(field)
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            return None
        values.append(raw)
    key, version, name, dept_key, spec_sha256 = values
    if (
        version == "legacy-unknown"
        or dept_key == "unknown"
        or key.startswith("legacy.idx.")
        or not _SPEC_SHA256.fullmatch(spec_sha256)
    ):
        return None
    return (employee_idx, key, version, name, dept_key, spec_sha256)


def _identity_employee(
    signature: tuple[Any, ...],
    *,
    roster_status: str,
) -> dict[str, Any]:
    idx, key, version, name, dept_key, spec_sha256 = signature
    identity_ref = hashlib.sha256(
        json.dumps(signature, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    return {
        "idx": int(idx),
        "name": str(name),
        "employee_key": str(key),
        "employee_catalog_version": str(version),
        "employee_name_snapshot": str(name),
        "employee_dept_key": str(dept_key),
        "employee_spec_sha256": str(spec_sha256),
        "roster_status": roster_status,
        "can_assign": roster_status == "active",
        "identity_ref": identity_ref,
    }


def _report_employees(scope: Mapping[str, Any]) -> dict[tuple[Any, ...], dict[str, Any]]:
    """Combine the active roster with exact historical identities that have work."""
    active: dict[tuple[Any, ...], dict[str, Any]] = {}
    for employee in scope["employees"].values():
        signature = _identity_signature(employee)
        if signature is None:
            # A malformed active catalog is a deployment/configuration error;
            # do not broaden the report with an idx-only fallback.
            continue
        active[signature] = _identity_employee(signature, roster_status="active")

    historical: dict[tuple[Any, ...], dict[str, Any]] = {}
    rows = db.q(
        "SELECT DISTINCT emp_idx,employee_key,employee_catalog_version,"
        "employee_name_snapshot,employee_dept_key,employee_spec_sha256 "
        "FROM task WHERE tenant_id=? AND deleted_at IS NULL "
        "AND employee_dept_key=?",
        (int(scope["tenant_id"]), str(scope["industry_key"])),
    )
    for row in rows:
        signature = _identity_signature(row)
        if signature is None or signature[4] != scope["industry_key"]:
            continue
        if signature not in active:
            historical[signature] = _identity_employee(
                signature,
                roster_status="legacy",
            )
    return {**active, **historical}


def _employee_identities_json(
    employees: Mapping[tuple[Any, ...], Mapping[str, Any]],
) -> str:
    payload = [
        {
            "idx": int(employee["idx"]),
            **{
                field: str(employee[field])
                for field in _TASK_IDENTITY_FIELDS
            },
        }
        for _signature, employee in sorted(
            employees.items(), key=lambda item: item[0]
        )
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _inspection_employee() -> dict[str, Any]:
    """只信任 registry 明确声明的巡店员工，不根据 ID 猜角色。"""
    station = registry.BY_IDX.get(INSPECTION_EMPLOYEE_IDX) or {}
    if station.get("key") != "inspection":
        raise DashboardScopeUnavailable("巡店员工配置不可用")
    return {
        "idx": INSPECTION_EMPLOYEE_IDX,
        "name": str(station.get("name") or "巡店经理"),
    }


def _table_exists(table: str) -> bool:
    row = db.one(
        "SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return bool(row)


def _table_columns(table: str) -> set[str]:
    # 只允许模块内部的固定表名到达 PRAGMA。
    allowed = {"tenant_industry", *_INSPECTION_CORE_COLUMNS}
    if table not in allowed:
        raise DashboardValidationError("未授权的数据表")
    if not _table_exists(table):
        return set()
    return {str(row.get("name") or "") for row in db.q(f"PRAGMA table_info({table})")}


def _require_scope_schema() -> None:
    required = {"tenant_id", "industry_key", "is_primary", "created_at"}
    if not required.issubset(_table_columns("tenant_industry")):
        raise DashboardScopeUnavailable("行业授权迁移尚未完成")


def _mapped_rows(tenant_id: int | None = None) -> list[dict[str, Any]]:
    _require_scope_schema()
    sql = (
        "SELECT t.id,t.name,ti.industry_key,ti.is_primary "
        "FROM tenants t JOIN tenant_industry ti ON ti.tenant_id=t.id "
        "WHERE t.enabled=1"
    )
    args: tuple[Any, ...] = ()
    if tenant_id is not None:
        sql += " AND t.id=?"
        args = (tenant_id,)
    sql += " ORDER BY t.id,ti.is_primary DESC,ti.industry_key"
    return db.q(sql, args)


def _positive_tenant(value: Any, fallback: int) -> int:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DashboardValidationError("租户参数无效")
    return value


def _resolve_scope(
    actor: Mapping[str, Any] | None,
    *,
    is_boss: bool,
    tenant_id: int | None,
    industry_key: str | None,
) -> dict[str, Any]:
    actor_tenant_id, can_cross = _actor_scope(actor, is_boss)
    target_tenant_id = _positive_tenant(tenant_id, actor_tenant_id)
    if not can_cross and target_tenant_id != actor_tenant_id:
        raise DashboardAccessDenied("不能查看其他租户")

    catalog = _industry_catalog()
    rows = _mapped_rows(target_tenant_id)
    valid_rows = [row for row in rows if row.get("industry_key") in catalog]
    if not valid_rows:
        raise DashboardScopeUnavailable("租户不存在、已停用或未配置行业")

    requested = str(industry_key or "").strip()
    if not requested:
        if len(valid_rows) != 1:
            raise DashboardValidationError("请选择一个行业")
        requested = str(valid_rows[0]["industry_key"])

    selected = next(
        (row for row in valid_rows if row.get("industry_key") == requested),
        None,
    )
    if selected is None:
        if can_cross:
            raise DashboardScopeUnavailable("行业不存在或租户未获授权")
        raise DashboardAccessDenied("当前租户未获授该行业")

    employees = _employees_for_industry(requested)
    if not employees:
        raise DashboardScopeUnavailable("行业员工配置不可用")
    meta = catalog[requested]
    return {
        "tenant_id": target_tenant_id,
        "tenant_name": str(selected.get("name") or ""),
        "industry_key": requested,
        "industry_name": meta["name"],
        "industry_emoji": meta["emoji"],
        "can_cross_tenant": can_cross,
        "employees": employees,
    }


def scopes(
    actor: Mapping[str, Any] | None,
    *,
    is_boss: bool = False,
) -> dict[str, Any]:
    """返回调用者可选的显式租户/行业范围。"""
    actor_tenant_id, can_cross = _actor_scope(actor, is_boss)
    rows = _mapped_rows(None if can_cross else actor_tenant_id)
    catalog = _industry_catalog()
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("industry_key") or "")
        if key not in catalog:
            # 配置中的未知行业不可被当作公开范围。
            continue
        tenant = grouped.setdefault(
            int(row["id"]),
            {
                "id": int(row["id"]),
                "name": str(row.get("name") or ""),
                "industries": [],
            },
        )
        meta = catalog[key]
        tenant["industries"].append(
            {
                "key": key,
                "name": meta["name"],
                "emoji": meta["emoji"],
                "is_primary": bool(row.get("is_primary")),
            }
        )
    tenants = [grouped[key] for key in sorted(grouped)]
    return {
        "can_cross_tenant": can_cross,
        "tenants": tenants,
    }


def _strict_days(days: Any) -> int:
    if isinstance(days, bool) or not isinstance(days, int):
        raise DashboardValidationError("天数必须是整数")
    if days < MIN_DAYS or days > MAX_DAYS:
        raise DashboardValidationError(f"天数必须介于 {MIN_DAYS} 与 {MAX_DAYS} 之间")
    return days


def _strict_now(now: Any) -> float:
    if now is None:
        return time.time()
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise DashboardValidationError("时间参数无效")
    value = float(now)
    if not math.isfinite(value) or value <= 0:
        raise DashboardValidationError("时间参数无效")
    return value


# 看板日界与产品其余部分(工具箱/热点)一致，使用中国时区；上海无夏令时，
# 固定 +8 偏移即可与 SQL 的 '+8 hours' 修饰符严格对齐。
TZ_CN = _datetime.timezone(_datetime.timedelta(hours=8))


def _period(days: int, now: float) -> dict[str, Any]:
    today = _datetime.datetime.fromtimestamp(now, TZ_CN).date()
    first_day = today - _datetime.timedelta(days=days - 1)
    started = _datetime.datetime.combine(
        first_day,
        _datetime.time.min,
        tzinfo=TZ_CN,
    ).timestamp()
    return {
        "days": days,
        "started_at": started,
        "ended_at": now,
        "first_day": first_day,
        "last_day": today,
    }


def _number(value: Any, digits: int = 4) -> float:
    return round(float(value or 0), digits)


def _nullable_number(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _task_aggregate(
    tenant_id: int,
    employee_identities_json: str,
    started_at: float,
    now: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    row = db.one(
        f"""
        WITH bounds(started_at,ended_at,stale_before) AS (VALUES(?,?,?))
        SELECT
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS total,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE}='active' THEN 1 ELSE 0 END),0) AS active,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE}='waiting' THEN 1 ELSE 0 END),0) AS waiting,
          COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='completed'
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS completed,
          COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='failed'
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS failed,
          COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='cancelled'
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS cancelled,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE}='completed' THEN 1 ELSE 0 END),0) AS cohort_completed,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE}='failed' THEN 1 ELSE 0 END),0) AS cohort_failed,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE}='cancelled' THEN 1 ELSE 0 END),0) AS cohort_cancelled,
          AVG(CASE WHEN {TASK_STATUS_CASE}='completed'
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            AND terminal_at>=created_at THEN terminal_at-created_at END) AS average_cycle,
          COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='completed'
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            AND terminal_at>=created_at THEN 1 ELSE 0 END),0) AS cycle_count,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN tokens ELSE 0 END),0) AS tokens,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN cost_usd ELSE 0 END),0) AS cost_usd,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND LOWER(COALESCE(billing_status,'')) IN ('charged','succeeded')
            AND billing_points>0
            THEN billing_points ELSE 0 END),0) AS billing_points
          ,COALESCE(SUM(CASE WHEN refunded_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS refunded
          ,COALESCE(SUM(CASE WHEN refunded_at BETWEEN bounds.started_at AND bounds.ended_at
            AND billing_points>0 THEN billing_points ELSE 0 END),0) AS refunded_points
          ,COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='active'
            THEN 1 ELSE 0 END),0) AS backlog_active
          ,COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='waiting'
            THEN 1 ELSE 0 END),0) AS backlog_waiting
          ,COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='active'
            AND COALESCE(updated_at,created_at,0)<bounds.stale_before
            THEN 1 ELSE 0 END),0) AS backlog_stale_active
        FROM task
        CROSS JOIN bounds
        WHERE tenant_id=? AND deleted_at IS NULL
          AND {_TASK_IDENTITY_MATCH_SQL}
        """,
        (
            started_at,
            now,
            now - STALE_AFTER_SECONDS,
            tenant_id,
            employee_identities_json,
        ),
    ) or {}
    total = int(row.get("total") or 0)
    completed = int(row.get("completed") or 0)
    task_metrics = {
        "total": total,
        "active": int(row.get("active") or 0),
        "waiting": int(row.get("waiting") or 0),
        "completed": completed,
        "failed": int(row.get("failed") or 0),
        "cancelled": int(row.get("cancelled") or 0),
        "cohort_completed": int(row.get("cohort_completed") or 0),
        "cohort_failed": int(row.get("cohort_failed") or 0),
        "cohort_cancelled": int(row.get("cohort_cancelled") or 0),
        "refunded": int(row.get("refunded") or 0),
        "refunded_points": _number(row.get("refunded_points")),
        "completion_rate": (
            round(int(row.get("cohort_completed") or 0) / total, 4)
            if total else None
        ),
    }
    efficiency = {
        "average_cycle_seconds": _nullable_number(row.get("average_cycle")),
        "cycle_count": int(row.get("cycle_count") or 0),
        "tokens": int(row.get("tokens") or 0),
        "cost_usd": _number(row.get("cost_usd")),
        "billing_points": _number(row.get("billing_points")),
        "backlog_active": int(row.get("backlog_active") or 0),
        "backlog_waiting": int(row.get("backlog_waiting") or 0),
        "backlog_stale_active": int(row.get("backlog_stale_active") or 0),
    }
    return task_metrics, efficiency


def _employee_task_rows(
    tenant_id: int,
    employee_identities_json: str,
    started_at: float,
    ended_at: float,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    rows = db.q(
        f"""
        WITH bounds(started_at,ended_at) AS (VALUES(?,?))
        SELECT
          emp_idx,
          employee_key,
          employee_catalog_version,
          employee_name_snapshot,
          employee_dept_key,
          employee_spec_sha256,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS total,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE}='active' THEN 1 ELSE 0 END),0) AS active,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE}='waiting' THEN 1 ELSE 0 END),0) AS waiting,
          COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='completed'
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS completed,
          COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='failed'
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS failed,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE}='completed' THEN 1 ELSE 0 END),0) AS cohort_completed,
          AVG(CASE WHEN {TASK_STATUS_CASE}='completed'
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            AND terminal_at>=created_at THEN terminal_at-created_at END) AS average_cycle,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN tokens ELSE 0 END),0) AS tokens,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN cost_usd ELSE 0 END),0) AS cost_usd,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND LOWER(COALESCE(billing_status,'')) IN ('charged','succeeded')
            AND billing_points>0
            THEN billing_points ELSE 0 END),0) AS billing_points,
          COALESCE(SUM(CASE WHEN refunded_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS refunded,
          COALESCE(SUM(CASE WHEN refunded_at BETWEEN bounds.started_at AND bounds.ended_at
            AND billing_points>0 THEN billing_points ELSE 0 END),0) AS refunded_points,
          MAX(CASE
            WHEN {TASK_STATUS_CASE} IN ('completed','failed','cancelled')
              AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
              THEN terminal_at
            WHEN refunded_at BETWEEN bounds.started_at AND bounds.ended_at
              THEN refunded_at
            WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at THEN created_at
          END) AS last_activity_at
        FROM task
        CROSS JOIN bounds
        WHERE tenant_id=? AND deleted_at IS NULL
          AND {_TASK_IDENTITY_MATCH_SQL}
          AND (created_at BETWEEN bounds.started_at AND bounds.ended_at
            OR ({TASK_STATUS_CASE} IN ('completed','failed','cancelled')
              AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at)
            OR refunded_at BETWEEN bounds.started_at AND bounds.ended_at)
        GROUP BY emp_idx,employee_key,employee_catalog_version,
          employee_name_snapshot,employee_dept_key,employee_spec_sha256
        """,
        (started_at, ended_at, tenant_id, employee_identities_json),
    )
    result = {}
    for row in rows:
        signature = _identity_signature(row)
        if signature is not None:
            result[signature] = row
    return result


def _empty_inspection_metrics(reason_code: str) -> dict[str, Any]:
    return {
        "availability": False,
        "reason_code": reason_code,
        "visits": None,
        "completed_visits": None,
        "cohort_completed_visits": None,
        "cohort_failed_visits": None,
        "completion_rate": None,
        "active_visits": None,
        "failed_visits": None,
        "branches_visited": None,
        "average_score": None,
        "average_cycle_seconds": None,
        "cycle_count": 0,
        "tokens": 0,
        "cost_usd": 0.0,
        "billing_points": 0.0,
        "refunded": 0,
        "refunded_points": 0.0,
        "linked_task_metrics": {
            "total": 0,
            "active": 0,
            "waiting": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "cohort_completed": 0,
            "cohort_failed": 0,
            "cohort_cancelled": 0,
            "backlog_active": 0,
            "backlog_waiting": 0,
            "backlog_stale_active": 0,
        },
        "issues": None,
        "open_issues": None,
        "critical_issues": None,
        "overdue_issues": None,
        "actions": None,
        "open_actions": None,
        "overdue_actions": None,
        "backlog": {
            "availability": False,
            "scope": "all_open_records",
            "open_issues": None,
            "critical_issues": None,
            "overdue_issues": None,
            "open_actions": None,
            "overdue_actions": None,
        },
    }


def _inspection_schema() -> dict[str, Any] | None:
    columns: dict[str, set[str]] = {}
    for table, required in _INSPECTION_CORE_COLUMNS.items():
        actual = _table_columns(table)
        if not required.issubset(actual):
            return None
        columns[table] = actual
    action_columns = columns["inspection_action"]
    if "issue_id" in action_columns:
        action_relation = "issue"
    elif "visit_id" in action_columns:
        action_relation = "visit"
    else:
        return None
    return {"action_relation": action_relation}


def _inspection_aggregate(
    tenant_id: int,
    industry_key: str,
    started_at: float,
    now: float,
) -> tuple[dict[str, Any], dict[int, dict[str, Any]], list[dict[str, Any]]]:
    schema = _inspection_schema()
    if schema is None:
        return _empty_inspection_metrics("inspection_schema_unavailable"), {}, []

    completed = _quoted(INSPECTION_COMPLETED_STATUSES)
    failed = _quoted(INSPECTION_FAILED_STATUSES)
    visit = db.one(
        f"""
        WITH bounds(started_at,ended_at) AS (VALUES(?,?))
        SELECT
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS visits,
          COALESCE(SUM(CASE WHEN LOWER(status) IN ({completed})
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS completed,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND LOWER(status) IN ({completed})
            THEN 1 ELSE 0 END),0) AS cohort_completed,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND LOWER(status) IN ({failed})
            THEN 1 ELSE 0 END),0) AS cohort_failed,
          COALESCE(SUM(CASE WHEN LOWER(status) IN ({failed})
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS failed,
          COALESCE(SUM(CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND LOWER(status) NOT IN ({completed},{failed})
            THEN 1 ELSE 0 END),0) AS active,
          COUNT(DISTINCT CASE WHEN created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN branch_id END) AS branches,
          AVG(CASE WHEN LOWER(status) IN ({completed})
            AND terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            AND score IS NOT NULL THEN score END) AS average_score
        FROM inspection_visit
        CROSS JOIN bounds
        WHERE tenant_id=? AND industry_key=? AND deleted_at IS NULL
        """,
        (started_at, now, tenant_id, industry_key),
    ) or {}

    closed = _quoted(CLOSED_ISSUE_STATUSES)
    critical = _quoted(CRITICAL_SEVERITIES)
    issue = db.one(
        f"""
        WITH bounds(started_at,now_at) AS (VALUES(?,?))
        SELECT
          COALESCE(SUM(CASE WHEN v.created_at>=bounds.started_at
            THEN 1 ELSE 0 END),0) AS issues,
          COALESCE(SUM(CASE WHEN v.created_at>=bounds.started_at
            AND LOWER(i.status) NOT IN ({closed})
            THEN 1 ELSE 0 END),0) AS open_issues,
          COALESCE(SUM(CASE WHEN v.created_at>=bounds.started_at
            AND LOWER(i.status) NOT IN ({closed})
            AND LOWER(i.severity) IN ({critical}) THEN 1 ELSE 0 END),0) AS critical_issues,
          COALESCE(SUM(CASE WHEN v.created_at>=bounds.started_at
            AND LOWER(i.status) NOT IN ({closed})
            AND i.due_at IS NOT NULL AND i.due_at<bounds.now_at
            THEN 1 ELSE 0 END),0) AS overdue_issues,
          COALESCE(SUM(CASE WHEN LOWER(i.status) NOT IN ({closed})
            THEN 1 ELSE 0 END),0) AS backlog_open_issues,
          COALESCE(SUM(CASE WHEN LOWER(i.status) NOT IN ({closed})
            AND LOWER(i.severity) IN ({critical})
            THEN 1 ELSE 0 END),0) AS backlog_critical_issues,
          COALESCE(SUM(CASE WHEN LOWER(i.status) NOT IN ({closed})
            AND i.due_at IS NOT NULL AND i.due_at<bounds.now_at
            THEN 1 ELSE 0 END),0) AS backlog_overdue_issues
        FROM inspection_issue i
        JOIN inspection_visit v ON v.id=i.visit_id
        CROSS JOIN bounds
        WHERE v.tenant_id=? AND v.industry_key=? AND v.deleted_at IS NULL
        """,
        (started_at, now, tenant_id, industry_key),
    ) or {}

    if schema["action_relation"] == "issue":
        action_from = (
            "inspection_action a "
            "JOIN inspection_issue i ON i.id=a.issue_id "
            "JOIN inspection_visit v ON v.id=i.visit_id"
        )
    else:
        action_from = (
            "inspection_action a "
            "JOIN inspection_visit v ON v.id=a.visit_id"
        )
    action = db.one(
        f"""
        WITH bounds(started_at,now_at) AS (VALUES(?,?))
        SELECT
          COALESCE(SUM(CASE WHEN v.created_at>=bounds.started_at
            THEN 1 ELSE 0 END),0) AS actions,
          COALESCE(SUM(CASE WHEN v.created_at>=bounds.started_at
            AND LOWER(a.status) NOT IN ({closed})
            AND a.closed_at IS NULL THEN 1 ELSE 0 END),0) AS open_actions,
          COALESCE(SUM(CASE WHEN v.created_at>=bounds.started_at
            AND LOWER(a.status) NOT IN ({closed})
            AND a.closed_at IS NULL AND a.due_at IS NOT NULL
            AND a.due_at<bounds.now_at THEN 1 ELSE 0 END),0) AS overdue_actions,
          COALESCE(SUM(CASE WHEN LOWER(a.status) NOT IN ({closed})
            AND a.closed_at IS NULL THEN 1 ELSE 0 END),0) AS backlog_open_actions,
          COALESCE(SUM(CASE WHEN LOWER(a.status) NOT IN ({closed})
            AND a.closed_at IS NULL AND a.due_at IS NOT NULL
            AND a.due_at<bounds.now_at THEN 1 ELSE 0 END),0) AS backlog_overdue_actions
        FROM {action_from}
        CROSS JOIN bounds
        WHERE v.tenant_id=? AND v.industry_key=? AND v.deleted_at IS NULL
        """,
        (started_at, now, tenant_id, industry_key),
    ) or {}

    employee_rows = db.q(
        f"""
        WITH bounds(started_at,ended_at,stale_before) AS (VALUES(?,?,?))
        SELECT v.employee_idx,
          COALESCE(SUM(CASE WHEN v.created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS visits,
          COALESCE(SUM(CASE WHEN LOWER(v.status) IN ({completed})
            AND v.terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS completed,
          COALESCE(SUM(CASE WHEN LOWER(v.status) IN ({failed})
            AND v.terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS failed,
          COALESCE(SUM(CASE WHEN v.created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND LOWER(v.status) IN ({completed}) THEN 1 ELSE 0 END),0)
            AS cohort_completed,
          COALESCE(SUM(CASE WHEN v.created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND LOWER(v.status) NOT IN ({completed},{failed}) THEN 1 ELSE 0 END),0)
            AS active,
          COALESCE(SUM(CASE WHEN t.created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS task_total,
          COALESCE(SUM(CASE WHEN t.created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE_T}='active' THEN 1 ELSE 0 END),0) AS task_active,
          COALESCE(SUM(CASE WHEN t.created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE_T}='waiting' THEN 1 ELSE 0 END),0) AS task_waiting,
          COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE_T}='completed'
            AND t.terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS task_completed,
          COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE_T}='failed'
            AND t.terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS task_failed,
          COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE_T}='cancelled'
            AND t.terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS task_cancelled,
          COALESCE(SUM(CASE WHEN t.created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE_T}='completed' THEN 1 ELSE 0 END),0)
            AS task_cohort_completed,
          COALESCE(SUM(CASE WHEN t.created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE_T}='failed' THEN 1 ELSE 0 END),0)
            AS task_cohort_failed,
          COALESCE(SUM(CASE WHEN t.created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND {TASK_STATUS_CASE_T}='cancelled' THEN 1 ELSE 0 END),0)
            AS task_cohort_cancelled,
          COALESCE(SUM(CASE WHEN t.id IS NOT NULL AND {TASK_STATUS_CASE_T}='active'
            THEN 1 ELSE 0 END),0) AS task_backlog_active,
          COALESCE(SUM(CASE WHEN t.id IS NOT NULL AND {TASK_STATUS_CASE_T}='waiting'
            THEN 1 ELSE 0 END),0) AS task_backlog_waiting,
          COALESCE(SUM(CASE WHEN t.id IS NOT NULL AND {TASK_STATUS_CASE_T}='active'
            AND COALESCE(t.updated_at,t.created_at,0)<bounds.stale_before
            THEN 1 ELSE 0 END),0) AS task_backlog_stale_active,
          MAX(CASE
            WHEN LOWER(v.status) IN ({completed},{failed})
              AND v.terminal_at BETWEEN bounds.started_at AND bounds.ended_at
              THEN v.terminal_at
            WHEN v.created_at BETWEEN bounds.started_at AND bounds.ended_at
              THEN v.created_at END) AS last_activity_at,
          COALESCE(SUM(CASE WHEN v.created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN t.tokens ELSE 0 END),0) AS tokens,
          COALESCE(SUM(CASE WHEN v.created_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN t.cost_usd ELSE 0 END),0) AS cost_usd,
          COALESCE(SUM(CASE WHEN v.created_at BETWEEN bounds.started_at AND bounds.ended_at
            AND LOWER(COALESCE(t.billing_status,'')) IN ('charged','succeeded')
            AND t.billing_points>0 THEN t.billing_points ELSE 0 END),0)
            AS billing_points,
          COALESCE(SUM(CASE WHEN t.refunded_at BETWEEN bounds.started_at AND bounds.ended_at
            THEN 1 ELSE 0 END),0) AS refunded,
          COALESCE(SUM(CASE WHEN t.refunded_at BETWEEN bounds.started_at AND bounds.ended_at
            AND t.billing_points>0 THEN t.billing_points ELSE 0 END),0)
            AS refunded_points,
          AVG(CASE WHEN {TASK_STATUS_CASE_T}='completed'
            AND t.terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            AND t.terminal_at>=t.created_at THEN t.terminal_at-t.created_at END)
            AS average_cycle_seconds,
          COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE_T}='completed'
            AND t.terminal_at BETWEEN bounds.started_at AND bounds.ended_at
            AND t.terminal_at>=t.created_at THEN 1 ELSE 0 END),0) AS cycle_count
        FROM inspection_visit v
        LEFT JOIN task t ON t.id=v.task_id AND t.tenant_id=v.tenant_id
          AND t.emp_idx={INSPECTION_EMPLOYEE_IDX} AND t.deleted_at IS NULL
        CROSS JOIN bounds
        WHERE v.tenant_id=? AND v.industry_key=? AND v.deleted_at IS NULL
          AND v.employee_idx IS NOT NULL
          AND (v.created_at BETWEEN bounds.started_at AND bounds.ended_at
            OR (LOWER(v.status) IN ({completed},{failed})
              AND v.terminal_at BETWEEN bounds.started_at AND bounds.ended_at)
            OR t.refunded_at BETWEEN bounds.started_at AND bounds.ended_at
            OR (t.id IS NOT NULL AND {TASK_STATUS_CASE_T} IN ('active','waiting')))
        GROUP BY v.employee_idx
        """,
        (started_at, now, now - STALE_AFTER_SECONDS, tenant_id, industry_key),
    )
    employee_visits = {
        int(row["employee_idx"]): row
        for row in employee_rows
    }
    execution = employee_visits.get(INSPECTION_EMPLOYEE_IDX, {})
    trend = db.q(
        f"""
        WITH events AS (
          SELECT strftime('%Y-%m-%d',v.created_at,'unixepoch','+8 hours') AS day,
            COUNT(*) AS visits,0 AS completed_visits,
            0.0 AS score_sum,0 AS score_count,
            0 AS tasks_created,0 AS tasks_completed,0 AS tasks_failed
          FROM inspection_visit v
          WHERE v.tenant_id=? AND v.industry_key=? AND v.deleted_at IS NULL
            AND v.created_at BETWEEN ? AND ?
          GROUP BY day
          UNION ALL
          SELECT strftime('%Y-%m-%d',terminal_at,'unixepoch','+8 hours') AS day,
            0 AS visits,COUNT(*) AS completed_visits,
            COALESCE(SUM(CASE WHEN score IS NOT NULL THEN score ELSE 0 END),0) AS score_sum,
            COALESCE(SUM(CASE WHEN score IS NOT NULL THEN 1 ELSE 0 END),0) AS score_count,
            0 AS tasks_created,0 AS tasks_completed,0 AS tasks_failed
          FROM inspection_visit
          WHERE tenant_id=? AND industry_key=? AND deleted_at IS NULL
            AND LOWER(status) IN ({completed})
            AND terminal_at BETWEEN ? AND ?
          GROUP BY day
          UNION ALL
          SELECT strftime('%Y-%m-%d',t.created_at,'unixepoch','+8 hours') AS day,
            0 AS visits,0 AS completed_visits,0.0 AS score_sum,0 AS score_count,
            COUNT(*) AS tasks_created,0 AS tasks_completed,0 AS tasks_failed
          FROM inspection_visit v
          JOIN task t ON t.id=v.task_id AND t.tenant_id=v.tenant_id
            AND t.emp_idx={INSPECTION_EMPLOYEE_IDX} AND t.deleted_at IS NULL
          WHERE v.tenant_id=? AND v.industry_key=? AND v.deleted_at IS NULL
            AND t.created_at BETWEEN ? AND ?
          GROUP BY day
          UNION ALL
          SELECT strftime('%Y-%m-%d',t.terminal_at,'unixepoch','+8 hours') AS day,
            0 AS visits,0 AS completed_visits,0.0 AS score_sum,0 AS score_count,
            0 AS tasks_created,
            COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE_T}='completed' THEN 1 ELSE 0 END),0)
              AS tasks_completed,
            COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE_T}='failed' THEN 1 ELSE 0 END),0)
              AS tasks_failed
          FROM inspection_visit v
          JOIN task t ON t.id=v.task_id AND t.tenant_id=v.tenant_id
            AND t.emp_idx={INSPECTION_EMPLOYEE_IDX} AND t.deleted_at IS NULL
          WHERE v.tenant_id=? AND v.industry_key=? AND v.deleted_at IS NULL
            AND {TASK_STATUS_CASE_T} IN ('completed','failed')
            AND t.terminal_at BETWEEN ? AND ?
          GROUP BY day
        )
        SELECT day,SUM(visits) AS visits,
          SUM(completed_visits) AS completed_visits,
          SUM(tasks_created) AS tasks_created,
          SUM(tasks_completed) AS tasks_completed,
          SUM(tasks_failed) AS tasks_failed,
          CASE WHEN SUM(score_count)>0 THEN SUM(score_sum)/SUM(score_count) END AS average_score
        FROM events
        GROUP BY day
        ORDER BY day
        """,
        (
            tenant_id, industry_key, started_at, now,
            tenant_id, industry_key, started_at, now,
            tenant_id, industry_key, started_at, now,
            tenant_id, industry_key, started_at, now,
        ),
    )
    visit_count = int(visit.get("visits") or 0)
    cohort_completed_visits = int(visit.get("cohort_completed") or 0)
    metrics = {
        "availability": True,
        "reason_code": None,
        "visits": visit_count,
        "completed_visits": int(visit.get("completed") or 0),
        "cohort_completed_visits": cohort_completed_visits,
        "cohort_failed_visits": int(visit.get("cohort_failed") or 0),
        "completion_rate": (
            round(cohort_completed_visits / visit_count, 4)
            if visit_count else None
        ),
        "active_visits": int(visit.get("active") or 0),
        "failed_visits": int(visit.get("failed") or 0),
        "branches_visited": int(visit.get("branches") or 0),
        "average_score": _nullable_number(visit.get("average_score")),
        "average_cycle_seconds": _nullable_number(
            execution.get("average_cycle_seconds")
        ),
        "cycle_count": int(execution.get("cycle_count") or 0),
        "tokens": int(execution.get("tokens") or 0),
        "cost_usd": _number(execution.get("cost_usd")),
        "billing_points": _number(execution.get("billing_points")),
        "refunded": int(execution.get("refunded") or 0),
        "refunded_points": _number(execution.get("refunded_points")),
        "linked_task_metrics": {
            "total": int(execution.get("task_total") or 0),
            "active": int(execution.get("task_active") or 0),
            "waiting": int(execution.get("task_waiting") or 0),
            "completed": int(execution.get("task_completed") or 0),
            "failed": int(execution.get("task_failed") or 0),
            "cancelled": int(execution.get("task_cancelled") or 0),
            "cohort_completed": int(
                execution.get("task_cohort_completed") or 0
            ),
            "cohort_failed": int(execution.get("task_cohort_failed") or 0),
            "cohort_cancelled": int(
                execution.get("task_cohort_cancelled") or 0
            ),
            "backlog_active": int(execution.get("task_backlog_active") or 0),
            "backlog_waiting": int(execution.get("task_backlog_waiting") or 0),
            "backlog_stale_active": int(
                execution.get("task_backlog_stale_active") or 0
            ),
        },
        "issues": int(issue.get("issues") or 0),
        "open_issues": int(issue.get("open_issues") or 0),
        "critical_issues": int(issue.get("critical_issues") or 0),
        "overdue_issues": int(issue.get("overdue_issues") or 0),
        "actions": int(action.get("actions") or 0),
        "open_actions": int(action.get("open_actions") or 0),
        "overdue_actions": int(action.get("overdue_actions") or 0),
        # 风险总账故意不受当前时间筛选影响；它是单独标明口径的
        # 全量未闭环 backlog，不能混进“近 N 天新增/完成”的分子分母。
        "backlog": {
            "availability": True,
            "scope": "all_open_records",
            "open_issues": int(issue.get("backlog_open_issues") or 0),
            "critical_issues": int(issue.get("backlog_critical_issues") or 0),
            "overdue_issues": int(issue.get("backlog_overdue_issues") or 0),
            "open_actions": int(action.get("backlog_open_actions") or 0),
            "overdue_actions": int(action.get("backlog_overdue_actions") or 0),
        },
    }
    return metrics, employee_visits, trend


def _task_trend(
    tenant_id: int,
    employee_identities_json: str,
    started_at: float,
    ended_at: float,
) -> list[dict[str, Any]]:
    return db.q(
        f"""
        WITH events AS (
          SELECT strftime('%Y-%m-%d',created_at,'unixepoch','+8 hours') AS day,
            COUNT(*) AS created,0 AS completed,0 AS failed
          FROM task
          WHERE tenant_id=? AND deleted_at IS NULL
            AND created_at BETWEEN ? AND ?
            AND {_TASK_IDENTITY_MATCH_SQL}
          GROUP BY day
          UNION ALL
          SELECT strftime('%Y-%m-%d',terminal_at,'unixepoch','+8 hours') AS day,
            0 AS created,
            COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='completed' THEN 1 ELSE 0 END),0) AS completed,
            COALESCE(SUM(CASE WHEN {TASK_STATUS_CASE}='failed' THEN 1 ELSE 0 END),0) AS failed
          FROM task
          WHERE tenant_id=? AND deleted_at IS NULL
            AND {TASK_STATUS_CASE} IN ('completed','failed')
            AND terminal_at BETWEEN ? AND ?
            AND {_TASK_IDENTITY_MATCH_SQL}
          GROUP BY day
        )
        SELECT
          day,SUM(created) AS created,SUM(completed) AS completed,SUM(failed) AS failed
        FROM events
        GROUP BY day
        ORDER BY day
        """,
        (
            tenant_id, started_at, ended_at, employee_identities_json,
            tenant_id, started_at, ended_at, employee_identities_json,
        ),
    )


def _merge_trend(
    period: Mapping[str, Any],
    task_rows: list[dict[str, Any]],
    inspection_rows: list[dict[str, Any]],
    inspection_available: bool,
) -> list[dict[str, Any]]:
    tasks = {str(row.get("day") or ""): row for row in task_rows}
    inspections = {str(row.get("day") or ""): row for row in inspection_rows}
    result = []
    day = period["first_day"]
    for _ in range(int(period["days"])):
        key = day.isoformat()
        task = tasks.get(key, {})
        inspection = inspections.get(key, {})
        result.append(
            {
                "day": key,
                "tasks_created": (
                    int(task.get("created") or 0)
                    + int(inspection.get("tasks_created") or 0)
                ),
                "tasks_completed": (
                    int(task.get("completed") or 0)
                    + int(inspection.get("tasks_completed") or 0)
                ),
                "tasks_failed": (
                    int(task.get("failed") or 0)
                    + int(inspection.get("tasks_failed") or 0)
                ),
                "inspection_visits": (
                    int(inspection.get("visits") or 0)
                    if inspection_available
                    else None
                ),
                "inspection_completed": (
                    int(inspection.get("completed_visits") or 0)
                    if inspection_available
                    else None
                ),
                "inspection_average_score": (
                    _nullable_number(inspection.get("average_score"))
                    if inspection_available
                    else None
                ),
            }
        )
        day += _datetime.timedelta(days=1)
    return result


def _employee_cards(
    employees: Mapping[tuple[Any, ...], Mapping[str, Any]],
    task_rows: Mapping[tuple[Any, ...], Mapping[str, Any]],
    inspection_visits: Mapping[int, Mapping[str, Any]],
    inspection_available: bool,
) -> list[dict[str, Any]]:
    cards = []
    for signature, public_employee in employees.items():
        idx = int(public_employee["idx"])
        row = task_rows.get(signature, {})
        inspection_row = inspection_visits.get(idx, {})
        total = int(row.get("total") or 0)
        completed = int(row.get("completed") or 0)
        cohort_completed = int(row.get("cohort_completed") or 0)
        cards.append(
            {
                "idx": idx,
                "name": public_employee["name"],
                "employee_kind": "industry_specialist",
                "catalog_version": public_employee["employee_catalog_version"],
                "dept_key": public_employee["employee_dept_key"],
                "roster_status": public_employee["roster_status"],
                "can_assign": bool(public_employee["can_assign"]),
                "identity_ref": public_employee["identity_ref"],
                "tasks": total,
                "active": int(row.get("active") or 0),
                "waiting": int(row.get("waiting") or 0),
                "completed": completed,
                "failed": int(row.get("failed") or 0),
                "completion_rate": (
                    round(cohort_completed / total, 4) if total else None
                ),
                "average_cycle_seconds": _nullable_number(row.get("average_cycle")),
                "tokens": int(row.get("tokens") or 0),
                "cost_usd": _number(row.get("cost_usd")),
                "billing_points": _number(row.get("billing_points")),
                "refunded": int(row.get("refunded") or 0),
                "refunded_points": _number(row.get("refunded_points")),
                "inspection_visits": (
                    int(inspection_row.get("visits") or 0)
                    if inspection_available
                    else None
                ),
                "last_activity_at": row.get("last_activity_at"),
            }
    )
    inspection_employee = _inspection_employee()
    inspection_row = inspection_visits.get(INSPECTION_EMPLOYEE_IDX, {})
    inspection_task_count = (
        int(inspection_row.get("visits") or 0)
        if inspection_available else 0
    )
    inspection_cohort_completed = int(
        inspection_row.get("cohort_completed") or 0
    )
    cards.append(
        {
            "idx": inspection_employee["idx"],
            "name": inspection_employee["name"],
            "employee_kind": "inspection",
            "catalog_version": "core.v1",
            "dept_key": "content",
            "roster_status": "active",
            "can_assign": True,
            "identity_ref": "inspection",
            # task.emp_idx=10 是内容库历史空间，不具有行业归属。
            # 巡店经理只通过 visit.industry_key 进入当前行业。
            "tasks": inspection_task_count,
            "active": (
                int(inspection_row.get("active") or 0)
                if inspection_available else 0
            ),
            "waiting": (
                int(inspection_row.get("task_waiting") or 0)
                if inspection_available else 0
            ),
            "completed": (
                int(inspection_row.get("completed") or 0)
                if inspection_available else 0
            ),
            "failed": (
                int(inspection_row.get("failed") or 0)
                if inspection_available else 0
            ),
            "completion_rate": (
                round(inspection_cohort_completed / inspection_task_count, 4)
                if inspection_available and inspection_task_count else None
            ),
            "average_cycle_seconds": inspection_row.get("average_cycle_seconds"),
            "tokens": int(inspection_row.get("tokens") or 0),
            "cost_usd": _number(inspection_row.get("cost_usd")),
            "billing_points": _number(inspection_row.get("billing_points")),
            "refunded": int(inspection_row.get("refunded") or 0),
            "refunded_points": _number(inspection_row.get("refunded_points")),
            "inspection_visits": (
                inspection_task_count
                if inspection_available
                else None
            ),
            "last_activity_at": (
                inspection_row.get("last_activity_at")
                if inspection_available else None
            ),
        }
    )
    cards.sort(
        key=lambda item: (
            -(
                int(item["tasks"])
                + int(item["inspection_visits"] or 0)
            ),
            int(item["idx"]),
        )
    )
    return cards


def _recent_activity(
    tenant_id: int,
    industry_key: str,
    employee_identities_json: str,
    started_at: float,
    ended_at: float,
    inspection_available: bool,
    allow_target_routes: bool,
) -> list[dict[str, Any]]:
    """返回老板可点开的最近执行索引，不读取任务或巡店正文。"""
    inspection_terminal = _quoted(
        INSPECTION_COMPLETED_STATUSES | INSPECTION_FAILED_STATUSES
    )
    task_sql = (
        "SELECT 'task' kind,id record_id,emp_idx employee_idx,status,"
        "employee_name_snapshot employee_name,"
        f"revision_no,CASE WHEN {TASK_STATUS_CASE} IN "
        "('completed','failed','cancelled') THEN terminal_at ELSE created_at END "
        "occurred_at FROM task "
        "WHERE tenant_id=? AND deleted_at IS NULL "
        f"AND CASE WHEN {TASK_STATUS_CASE} IN "
        "('completed','failed','cancelled') THEN terminal_at ELSE created_at END "
        "BETWEEN ? AND ? "
        f"AND {_TASK_IDENTITY_MATCH_SQL} "
        "AND (thread_id IS NULL OR id=(SELECT tt.current_task_id "
        "FROM task_thread tt WHERE tt.id=task.thread_id "
        "AND tt.tenant_id=task.tenant_id))"
    )
    args: list[Any] = [
        tenant_id, started_at, ended_at, employee_identities_json,
    ]
    if inspection_available:
        task_sql += (
            " UNION ALL SELECT 'inspection' kind,id record_id,employee_idx,"
            f"status,NULL employee_name,1 revision_no,"
            f"CASE WHEN LOWER(status) IN ({inspection_terminal}) "
            "THEN terminal_at ELSE created_at END occurred_at "
            "FROM inspection_visit WHERE tenant_id=? AND industry_key=? "
            "AND deleted_at IS NULL "
            f"AND CASE WHEN LOWER(status) IN ({inspection_terminal}) "
            "THEN terminal_at ELSE created_at END BETWEEN ? AND ?"
        )
        args.extend((tenant_id, industry_key, started_at, ended_at))
    rows = db.q(
        "SELECT * FROM (" + task_sql + ") recent_activity "
        "ORDER BY occurred_at DESC,record_id DESC LIMIT ?",
        (*args, RECENT_ACTIVITY_LIMIT),
    )
    inspector = _inspection_employee() if inspection_available else None
    items = []
    for row in rows:
        kind = str(row.get("kind") or "task")
        employee_idx = int(
            row.get("employee_idx") or INSPECTION_EMPLOYEE_IDX
        )
        employee_name = (
            inspector["name"]
            if kind == "inspection" and inspector
            else str(row.get("employee_name") or "历史数字员工")
        )
        items.append({
            "kind": kind,
            "record_id": int(row["record_id"]),
            "employee_idx": employee_idx,
            "employee_name": employee_name,
            "status": str(row.get("status") or "queued"),
            "status_group": _status_group(row.get("status")),
            "revision_no": max(1, int(row.get("revision_no") or 1)),
            "occurred_at": row.get("occurred_at"),
            # 普通任务/巡店详情接口都固定在当前登录租户。命名 Boss
            # 跨租户查看聚合时不能复用这些路由，否则会把一个不可访问的
            # ID 暴露成可点击链接。跨租户详情后续应使用单独的审计契约。
            "target_route": (
                (
                    f"#/inspections/{int(row['record_id'])}/{industry_key}"
                    if kind == "inspection"
                    else f"#/tasks/{int(row['record_id'])}"
                )
                if allow_target_routes
                else None
            ),
        })
    return items


def _public_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tenant_id": int(scope["tenant_id"]),
        "tenant_name": str(scope["tenant_name"]),
        "industry_key": str(scope["industry_key"]),
        "industry_name": str(scope["industry_name"]),
        "industry_emoji": str(scope["industry_emoji"]),
    }


def summary(
    actor: Mapping[str, Any] | None,
    *,
    is_boss: bool = False,
    tenant_id: int | None = None,
    industry_key: str | None = None,
    days: int = DEFAULT_DAYS,
    now: float | None = None,
) -> dict[str, Any]:
    """返回指定行业的任务、效率、风险、趋势与巡店聚合。"""
    days = _strict_days(days)
    now_value = _strict_now(now)
    scope = _resolve_scope(
        actor,
        is_boss=is_boss,
        tenant_id=tenant_id,
        industry_key=industry_key,
    )
    period = _period(days, now_value)
    report_employees = _report_employees(scope)
    employee_identities_json = _employee_identities_json(report_employees)
    task_metrics, efficiency = _task_aggregate(
        scope["tenant_id"],
        employee_identities_json,
        period["started_at"],
        now_value,
    )
    employee_rows = _employee_task_rows(
        scope["tenant_id"],
        employee_identities_json,
        period["started_at"],
        now_value,
    )
    inspection, inspection_employee_rows, inspection_trend = _inspection_aggregate(
        scope["tenant_id"],
        scope["industry_key"],
        period["started_at"],
        now_value,
    )
    if inspection.get("availability"):
        # task.emp_idx=10 的历史空间没有行业属性，只有经过
        # inspection_visit.task_id + tenant + industry 精确归属的执行账才能
        # 合并进老板总览，同时保留巡店业务指标的独立口径。
        linked = inspection.get("linked_task_metrics") or {}
        for key in (
            "total", "active", "waiting", "completed", "failed", "cancelled",
            "cohort_completed", "cohort_failed", "cohort_cancelled",
        ):
            task_metrics[key] += int(linked.get(key) or 0)
        task_metrics["completion_rate"] = (
            round(task_metrics["cohort_completed"] / task_metrics["total"], 4)
            if task_metrics["total"] else None
        )
        task_metrics["refunded"] += int(inspection.get("refunded") or 0)
        task_metrics["refunded_points"] = _number(
            task_metrics["refunded_points"]
            + float(inspection.get("refunded_points") or 0)
        )
        task_cycle_count = int(efficiency.get("cycle_count") or 0)
        inspection_cycle_count = int(inspection.get("cycle_count") or 0)
        cycle_count = task_cycle_count + inspection_cycle_count
        if cycle_count:
            task_cycle_total = (
                float(efficiency.get("average_cycle_seconds") or 0)
                * task_cycle_count
            )
            inspection_cycle_total = (
                float(inspection.get("average_cycle_seconds") or 0)
                * inspection_cycle_count
            )
            efficiency["average_cycle_seconds"] = round(
                (task_cycle_total + inspection_cycle_total) / cycle_count, 2
            )
        efficiency["cycle_count"] = cycle_count
        efficiency["backlog_active"] += int(linked.get("backlog_active") or 0)
        efficiency["backlog_waiting"] += int(linked.get("backlog_waiting") or 0)
        efficiency["backlog_stale_active"] += int(
            linked.get("backlog_stale_active") or 0
        )
        efficiency["tokens"] += int(inspection.get("tokens") or 0)
        efficiency["cost_usd"] = _number(
            efficiency["cost_usd"] + float(inspection.get("cost_usd") or 0)
        )
        efficiency["billing_points"] = _number(
            efficiency["billing_points"]
            + float(inspection.get("billing_points") or 0)
        )
    inspection_backlog = inspection.get("backlog") or {}
    task_trend = _task_trend(
        scope["tenant_id"],
        employee_identities_json,
        period["started_at"],
        now_value,
    )
    efficiency_metrics = {
        "average_cycle_seconds": efficiency["average_cycle_seconds"],
        "completed_per_day": round(task_metrics["completed"] / days, 4),
        "tokens": efficiency["tokens"],
        "cost_usd": efficiency["cost_usd"],
        "billing_points": efficiency["billing_points"],
        "refunded_points": task_metrics["refunded_points"],
    }
    risk_metrics = {
        "stale_active_tasks": efficiency["backlog_stale_active"],
        "waiting_for_decision": efficiency["backlog_waiting"],
        "failed_tasks": task_metrics["failed"],
        "refunded_tasks": task_metrics["refunded"],
        "critical_inspection_issues": (
            inspection_backlog.get("critical_issues")
            if inspection["availability"] else None
        ),
        "overdue_inspection_issues": (
            inspection_backlog.get("overdue_issues")
            if inspection["availability"] else None
        ),
        "overdue_inspection_actions": (
            inspection_backlog.get("overdue_actions")
            if inspection["availability"] else None
        ),
    }
    return {
        "scope": _public_scope(scope),
        # 命名 Boss 可跨租户看结构化数据，但不能从当前会话
        # 跳转到其他租户的可写工作台。
        "can_open_records": (
            int(actor.get("tenant_id") or 0) == int(scope["tenant_id"])
        ),
        "period": {
            "days": days,
            "started_at": period["started_at"],
            "ended_at": period["ended_at"],
        },
        "task_metrics": task_metrics,
        "efficiency_metrics": efficiency_metrics,
        "risk_metrics": risk_metrics,
        "inspection_metrics": inspection,
        "trend": _merge_trend(
            period,
            task_trend,
            inspection_trend,
            bool(inspection["availability"]),
        ),
        "employees": _employee_cards(
            report_employees,
            employee_rows,
            inspection_employee_rows,
            bool(inspection["availability"]),
        ),
        "recent_activity": _recent_activity(
            scope["tenant_id"],
            scope["industry_key"],
            employee_identities_json,
            period["started_at"],
            now_value,
            bool(inspection["availability"]),
            int(actor.get("tenant_id") or 0) == int(scope["tenant_id"]),
        ),
        # 展示该行业的决策指标目录，但在可信业务数据源
        # 接入前一律为“待接入”，不拿派活任务数冒充经营结果。
        "business_metrics": _industry_business_metrics(scope["industry_key"]),
        "generated_at": now_value,
    }


def _strict_pagination(limit: Any, offset: Any) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise DashboardValidationError("分页大小必须是整数")
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise DashboardValidationError(f"分页大小必须介于 1 与 {MAX_PAGE_SIZE} 之间")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise DashboardValidationError("分页偏移必须是整数")
    if offset < 0 or offset > MAX_OFFSET:
        raise DashboardValidationError(f"分页偏移不能超过 {MAX_OFFSET}")
    return limit, offset


def _industry_business_metrics(industry_key: str) -> dict[str, Any]:
    catalog = INDUSTRY_METRIC_CATALOG.get(industry_key, ())
    return {
        "availability": False,
        "reason_code": "industry_operating_data_unavailable",
        "industry_key": industry_key,
        "metrics": [
            {
                "key": key,
                "label": label,
                "unit": unit,
                "value": None,
                "availability": False,
                "source_required": source,
            }
            for key, label, unit, source in catalog
        ],
        # 保留旧客户端已经使用的字段，但始终显式不可用。
        "revenue": None,
        "profit": None,
        "roi": None,
    }


def _employee_task_detail(
    tenant_id: int,
    employee_identities_json: str,
    limit: int,
    offset: int,
    allow_target_routes: bool,
    started_at: float,
    ended_at: float,
) -> dict[str, Any]:
    terminal = "('completed','failed','cancelled')"
    total = db.one(
        "SELECT COUNT(*) AS n FROM task "
        "WHERE tenant_id=? AND deleted_at IS NULL "
        f"AND {_TASK_IDENTITY_MATCH_SQL} "
        f"AND (created_at BETWEEN ? AND ? OR ({TASK_STATUS_CASE} IN {terminal} "
        "AND terminal_at BETWEEN ? AND ?))",
        (
            tenant_id, employee_identities_json, started_at, ended_at,
            started_at, ended_at,
        ),
    ) or {}
    rows = db.q(
        """
        SELECT
          id,status,created_at,updated_at,terminal_at,tokens,cost_usd,
          billing_status,billing_points,retry_count,
          source_meeting_id,source_task_id
        FROM task
        WHERE tenant_id=? AND deleted_at IS NULL
          AND {_TASK_IDENTITY_MATCH_SQL}
          AND (created_at BETWEEN ? AND ? OR ({TASK_STATUS_CASE} IN {terminal}
            AND terminal_at BETWEEN ? AND ?))
        ORDER BY CASE
          WHEN {TASK_STATUS_CASE} IN {terminal}
            AND terminal_at BETWEEN ? AND ?
            THEN terminal_at
          ELSE created_at END DESC,id DESC
        LIMIT ? OFFSET ?
        """.format(
            TASK_STATUS_CASE=TASK_STATUS_CASE,
            terminal=terminal,
            _TASK_IDENTITY_MATCH_SQL=_TASK_IDENTITY_MATCH_SQL,
        ),
        (
            tenant_id, employee_identities_json,
            started_at, ended_at, started_at, ended_at,
            started_at, ended_at,
            limit, offset,
        ),
    )
    items = []
    for row in rows:
        status_group = _status_group(row.get("status"))
        terminal_in_period = (
            status_group in {"completed", "failed", "cancelled"}
            and float(row.get("terminal_at") or 0)
            >= started_at
            and float(row.get("terminal_at") or 0)
            <= ended_at
        )
        items.append(
            {
                "id": int(row["id"]),
                "status": str(row.get("status") or "queued"),
                "status_group": status_group,
                "period_event_kind": status_group if terminal_in_period else "created",
                "period_event_at": (
                    row.get("terminal_at")
                    if terminal_in_period else row.get("created_at")
                ),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "terminal_at": row.get("terminal_at"),
                "tokens": int(row.get("tokens") or 0),
                "cost_usd": _number(row.get("cost_usd")),
                "billing_status": str(row.get("billing_status") or ""),
                "billing_points": _number(row.get("billing_points")),
                "retry_count": int(row.get("retry_count") or 0),
                "source_meeting_id": row.get("source_meeting_id"),
                "source_task_id": row.get("source_task_id"),
                "target_route": (
                    f"#/tasks/{int(row['id'])}" if allow_target_routes else None
                ),
            }
        )
    return {
        "total": int(total.get("n") or 0),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


def _inspection_only_task_detail(limit: int, offset: int) -> dict[str, Any]:
    """idx=10 不从 task 表猜测行业；该员工只有巡店工作台记录。"""
    return {
        "total": 0,
        "limit": limit,
        "offset": offset,
        "items": [],
    }


def _employee_inspection_detail(
    tenant_id: int,
    industry_key: str,
    employee_idx: int,
    limit: int,
    offset: int,
    allow_target_routes: bool,
    started_at: float,
    ended_at: float,
) -> dict[str, Any]:
    if _inspection_schema() is None:
        return {
            "availability": False,
            "reason_code": "inspection_schema_unavailable",
            "total": None,
            "limit": limit,
            "offset": offset,
            "items": [],
        }
    terminal_statuses = (
        INSPECTION_COMPLETED_STATUSES | INSPECTION_FAILED_STATUSES
    )
    terminal_sql = _quoted(terminal_statuses)
    total = db.one(
        f"SELECT COUNT(*) AS n FROM inspection_visit "
        "WHERE tenant_id=? AND industry_key=? AND employee_idx=? "
        "AND deleted_at IS NULL AND (created_at BETWEEN ? AND ? "
        f"OR (LOWER(status) IN ({terminal_sql}) AND terminal_at BETWEEN ? AND ?))",
        (
            tenant_id, industry_key, employee_idx,
            started_at, ended_at, started_at, ended_at,
        ),
    ) or {}
    rows = db.q(
        f"""
        SELECT
          id,branch_id,status,score,task_id,visit_at,
          created_at,updated_at,completed_at,terminal_at
        FROM inspection_visit
        WHERE tenant_id=? AND industry_key=? AND employee_idx=?
          AND deleted_at IS NULL AND (created_at BETWEEN ? AND ?
            OR (LOWER(status) IN ({terminal_sql}) AND terminal_at BETWEEN ? AND ?))
        ORDER BY CASE WHEN LOWER(status) IN ({terminal_sql})
          AND terminal_at BETWEEN ? AND ? THEN terminal_at ELSE created_at END DESC,
          id DESC
        LIMIT ? OFFSET ?
        """,
        (
            tenant_id, industry_key, employee_idx,
            started_at, ended_at, started_at, ended_at,
            started_at, ended_at, limit, offset,
        ),
    )
    items = []
    for row in rows:
        normalized_status = str(row.get("status") or "").strip().lower()
        terminal_in_period = (
            normalized_status in terminal_statuses
            and float(row.get("terminal_at") or 0) >= started_at
            and float(row.get("terminal_at") or 0) <= ended_at
        )
        items.append({
            "id": int(row["id"]),
            "branch_id": int(row["branch_id"]),
            "status": str(row.get("status") or "queued"),
            "status_group": _status_group(row.get("status")),
            "period_event_kind": (
                _status_group(row.get("status"))
                if terminal_in_period else "created"
            ),
            "period_event_at": (
                row.get("terminal_at") if terminal_in_period
                else row.get("created_at")
            ),
            "score": _nullable_number(row.get("score")),
            "task_id": row.get("task_id"),
            "visit_at": row.get("visit_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "completed_at": row.get("completed_at"),
            "terminal_at": row.get("terminal_at"),
            "target_route": (
                f"#/inspections/{int(row['id'])}/{industry_key}"
                if allow_target_routes else None
            ),
        })
    return {
        "availability": True,
        "reason_code": None,
        "total": int(total.get("n") or 0),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


def employee_detail(
    actor: Mapping[str, Any] | None,
    *,
    employee_idx: int,
    identity_ref: str | None = None,
    is_boss: bool = False,
    tenant_id: int | None = None,
    industry_key: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
    days: int = DEFAULT_DAYS,
    now: float | None = None,
) -> dict[str, Any]:
    """返回员工的结构化任务/巡店索引，绝不返回产出正文。"""
    limit, offset = _strict_pagination(limit, offset)
    days = _strict_days(days)
    now_value = _strict_now(now)
    period = _period(days, now_value)
    scope = _resolve_scope(
        actor,
        is_boss=is_boss,
        tenant_id=tenant_id,
        industry_key=industry_key,
    )
    if isinstance(employee_idx, bool) or not isinstance(employee_idx, int):
        raise DashboardValidationError("员工参数无效")
    employee_kind = "industry_specialist"
    report_employees = _report_employees(scope)
    matching_identities = {
        signature: employee
        for signature, employee in report_employees.items()
        if int(employee["idx"]) == employee_idx
    }
    if employee_idx == INSPECTION_EMPLOYEE_IDX:
        if identity_ref not in (None, "inspection"):
            raise DashboardValidationError("员工身份参数无效")
    elif identity_ref is not None:
        if not isinstance(identity_ref, str) or not re.fullmatch(
            r"[0-9a-f]{20}", identity_ref
        ):
            raise DashboardValidationError("员工身份参数无效")
        matching_identities = {
            signature: employee
            for signature, employee in matching_identities.items()
            if employee.get("identity_ref") == identity_ref
        }
    employee = next(iter(matching_identities.values()), None)
    if employee_idx == INSPECTION_EMPLOYEE_IDX:
        employee = _inspection_employee()
        employee_kind = "inspection"
    elif employee is None or len(matching_identities) != 1:
        # 不告知调用者该 ID 是否在其他行业存在。
        raise DashboardAccessDenied("当前行业没有该员工")
    allow_target_routes = (
        int(actor.get("tenant_id") or 0) == int(scope["tenant_id"])
    )
    return {
        "scope": _public_scope(scope),
        "employee": {
            "idx": employee_idx,
            "name": employee["name"],
            "employee_kind": employee_kind,
            "catalog_version": (
                "core.v1" if employee_kind == "inspection"
                else employee["employee_catalog_version"]
            ),
            "dept_key": (
                "content" if employee_kind == "inspection"
                else employee["employee_dept_key"]
            ),
            "roster_status": (
                "active" if employee_kind == "inspection"
                else employee["roster_status"]
            ),
            "can_assign": (
                True if employee_kind == "inspection"
                else bool(employee["can_assign"])
            ),
            "identity_ref": (
                "inspection" if employee_kind == "inspection"
                else employee["identity_ref"]
            ),
        },
        "can_open_records": allow_target_routes,
        "period": {
            "days": days,
            "started_at": period["started_at"],
            "ended_at": period["ended_at"],
        },
        "tasks": (
            _inspection_only_task_detail(limit, offset)
            if employee_kind == "inspection"
            else _employee_task_detail(
                scope["tenant_id"],
                _employee_identities_json(matching_identities),
                limit,
                offset,
                allow_target_routes, period["started_at"], now_value,
            )
        ),
        "inspection_visits": _employee_inspection_detail(
            scope["tenant_id"],
            scope["industry_key"],
            employee_idx,
            limit,
            offset,
            allow_target_routes,
            period["started_at"],
            now_value,
        ),
        "generated_at": now_value,
    }


__all__ = [
    "DashboardAccessDenied",
    "DashboardError",
    "DashboardScopeUnavailable",
    "DashboardValidationError",
    "employee_detail",
    "scopes",
    "summary",
]
