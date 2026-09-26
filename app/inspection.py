"""区域巡店的可审计服务层。

模块只管业务约束和 SQLite 事务，不知道 FastAPI ``UploadFile``、
磁盘目录或任何具体模型供应商。``run_inspection`` 把图片持久化和
视觉识别作为回调注入，因此 HTTP 层可继续复用全局的上传限额、
文件魔数校验、模型路由和计费逻辑。

安全不变量：
- tenant / industry / branch 每次都从数据库反查，不信前端隐藏字段；
- 模型只能引用当前巡店照片，不能生成“已整改/已关闭”事实；
- 负责人提交整改后仍需企业主/平台 root 人工关单；
- 所有可竞争状态迁移均使用版本 CAS。
"""
from __future__ import annotations

import copy
import inspect as pyinspect
import json
import math
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from . import db, departments, inspectionstandards


EMPLOYEE_IDX = 10
MAX_PHOTOS = 8
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_TOTAL_PHOTO_BYTES = 40 * 1024 * 1024
MAX_IMAGE_EDGE = 8192
MAX_IMAGE_PIXELS = 20_000_000
MIN_PHOTO_REVIEW_CONFIDENCE = 0.8
SEVERITIES = ("critical", "high", "medium", "low")
RECHECK_RECOMMENDATIONS = ("close", "reject", "manual_review")
_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
_STORAGE_RE = re.compile(r"^[A-Za-z0-9._/-]{1,240}$")
_CATEGORY_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,50}$")
_STANDARD_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class InspectionError(ValueError):
    """巡店输入或状态不符合业务契约。"""


class InspectionForbidden(InspectionError):
    """账号或租户没有该行业的操作权限。"""


class InspectionNotFound(InspectionError):
    """在当前 tenant / industry 作用域内不存在。"""


class InspectionConflict(InspectionError):
    """对象已被其他请求更新，不能覆盖。"""


class InspectionContractError(InspectionError):
    """视觉候选未通过巡店合同，带稳定、无业务数据的分类码。"""

    def __init__(
        self,
        message: str,
        *,
        validation_code: str,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.validation_code = str(validation_code)
        self.retryable = bool(retryable)


def _contract_error(
    validation_code: str,
    message: str,
    *,
    retryable: bool = True,
) -> InspectionContractError:
    return InspectionContractError(
        message,
        validation_code=validation_code,
        retryable=retryable,
    )


def _text(
    value: Any,
    *,
    field: str,
    limit: int,
    required: bool = False,
) -> str:
    if value is not None and not isinstance(value, str):
        raise InspectionError(f"{field}格式无效")
    clean = str(value or "").strip()
    if required and not clean:
        raise InspectionError(f"{field}必填")
    if len(clean) > limit:
        raise InspectionError(f"{field}不能超过 {limit} 个字")
    if _CONTROL_RE.search(clean):
        raise InspectionError(f"{field}不能包含控制字符")
    return clean


def _bounded_float(
    value: Any,
    *,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool):
        raise InspectionError(f"{field}格式无效")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InspectionError(f"{field}格式无效") from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise InspectionError(f"{field}必须在 {minimum:g}-{maximum:g} 之间")
    return number


def _positive_id(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise InspectionError(f"{field}格式无效")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise InspectionError(f"{field}格式无效") from None
    if result <= 0 or str(value).strip() not in {str(result), f"{result}.0"}:
        # JSON 数字 1.0 可接受，“1abc”或负数不接受。
        if not isinstance(value, float) or value != result:
            raise InspectionError(f"{field}格式无效")
    return result


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _strict_checklist_requested(raw: Mapping[str, Any]) -> bool:
    value = raw.get("require_checklist")
    if value in (None, "", False, 0, "0", "false", "False"):
        return False
    if value in (True, 1, "1", "true", "True"):
        return True
    raise InspectionError("巡店标准开关格式无效")


def _decoded_json_field(
    raw: Mapping[str, Any],
    field: str,
    json_field: str,
    default: Any,
) -> Any:
    value = raw.get(field)
    if value is None and json_field in raw:
        value = raw.get(json_field)
    if value is None or value == "":
        return copy.deepcopy(default)
    if isinstance(value, str):
        if len(value) > 50_000:
            raise InspectionError(f"{field}内容过长")
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise InspectionError(f"{field}格式无效") from None
    return value


def _snapshot_indexes(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a frozen server snapshot without consulting today's catalog."""
    if not isinstance(snapshot, Mapping):
        raise InspectionConflict("巡店标准快照无效")
    template_key = snapshot.get("template_key")
    template_version = snapshot.get("template_version")
    if (
        not isinstance(template_key, str)
        or not _STANDARD_CODE_RE.fullmatch(template_key)
        or not isinstance(template_version, str)
        or not template_version.strip()
        or len(template_version) > 40
    ):
        raise InspectionConflict("巡店标准快照无效")

    slots = snapshot.get("capture_slots")
    items = snapshot.get("items")
    metrics = snapshot.get("metrics")
    if (
        not isinstance(slots, list)
        or not isinstance(items, list)
        or not isinstance(metrics, list)
        or not 1 <= len(slots) <= MAX_PHOTOS
    ):
        raise InspectionConflict("巡店标准快照无效")

    slot_by_code: dict[str, dict] = {}
    required_slots: set[str] = set()
    for slot in slots:
        if not isinstance(slot, Mapping):
            raise InspectionConflict("巡店标准快照无效")
        code = slot.get("slot_code")
        required = slot.get("required")
        if (
            not isinstance(code, str)
            or not _STANDARD_CODE_RE.fullmatch(code)
            or code in slot_by_code
            or not isinstance(required, bool)
        ):
            raise InspectionConflict("巡店标准快照无效")
        slot_by_code[code] = dict(slot)
        if required:
            required_slots.add(code)

    item_by_code: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise InspectionConflict("巡店标准快照无效")
        code = item.get("item_code")
        if (
            not isinstance(code, str)
            or not _STANDARD_CODE_RE.fullmatch(code)
            or code in item_by_code
        ):
            raise InspectionConflict("巡店标准快照无效")
        item_by_code[code] = dict(item)

    metric_by_code: dict[str, dict] = {}
    for metric in metrics:
        if not isinstance(metric, Mapping):
            raise InspectionConflict("巡店标准快照无效")
        code = metric.get("metric_code")
        allowed_units = metric.get("allowed_units")
        if (
            not isinstance(code, str)
            or not _STANDARD_CODE_RE.fullmatch(code)
            or code in metric_by_code
            or not isinstance(allowed_units, list)
            or not allowed_units
            or any(
                not isinstance(unit, str) or not unit or len(unit) > 40
                for unit in allowed_units
            )
        ):
            raise InspectionConflict("巡店标准快照无效")
        metric_by_code[code] = dict(metric)
    return {
        "template_key": template_key,
        "template_version": template_version,
        "slot_by_code": slot_by_code,
        "required_slots": required_slots,
        "item_by_code": item_by_code,
        "metric_by_code": metric_by_code,
    }


def _normalize_file_slots(value: Any, snapshot: Mapping[str, Any]) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise InspectionError("照片采集位格式无效")
    indexes = _snapshot_indexes(snapshot)
    result: list[str] = []
    for raw_code in value:
        code = _text(raw_code, field="照片采集位", limit=80, required=True)
        if code not in indexes["slot_by_code"]:
            raise InspectionError("照片采集位不在当前巡店标准中")
        if code in result:
            raise InspectionError("同一照片采集位只能上传一张照片")
        result.append(code)
    if len(result) > MAX_PHOTOS:
        raise InspectionError(f"每次巡店最多上传 {MAX_PHOTOS} 张照片")
    missing = indexes["required_slots"] - set(result)
    if missing:
        raise InspectionError("巡店照片未完整覆盖全部必拍采集位")
    return result


def _normalize_observations(
    value: Any,
    snapshot: Mapping[str, Any],
) -> dict[str, list[dict]]:
    if not isinstance(value, Mapping):
        raise InspectionError("巡店观察值格式无效")
    unknown_top = set(value) - {"metrics", "checklist"}
    if unknown_top:
        raise InspectionError("巡店观察值包含未知字段")
    indexes = _snapshot_indexes(snapshot)
    metric_rows = value.get("metrics", [])
    checklist_rows = value.get("checklist", [])
    for label, rows in (("经营指标", metric_rows), ("检查观察", checklist_rows)):
        if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
            raise InspectionError(f"{label}格式无效")

    metrics: list[dict] = []
    seen_metrics: set[str] = set()
    for raw_metric in metric_rows:
        if not isinstance(raw_metric, Mapping):
            raise InspectionError("经营指标格式无效")
        if set(raw_metric) - {"metric_code", "value", "unit"}:
            raise InspectionError("经营指标包含未知字段")
        code = _text(
            raw_metric.get("metric_code"),
            field="经营指标编码",
            limit=80,
            required=True,
        )
        metric = indexes["metric_by_code"].get(code)
        if metric is None:
            raise InspectionError("经营指标不在当前巡店标准中")
        if code in seen_metrics:
            raise InspectionError("经营指标不能重复")
        raw_number = raw_metric.get("value")
        if isinstance(raw_number, bool) or raw_number in (None, ""):
            raise InspectionError("经营指标数值格式无效")
        try:
            number = float(raw_number)
        except (TypeError, ValueError, OverflowError):
            raise InspectionError("经营指标数值格式无效") from None
        if not math.isfinite(number):
            raise InspectionError("经营指标数值必须是有限数字")
        unit = _text(
            raw_metric.get("unit"),
            field="经营指标单位",
            limit=40,
            required=True,
        )
        if unit not in metric["allowed_units"]:
            raise InspectionError("经营指标单位与当前标准不一致")
        seen_metrics.add(code)
        metrics.append({"metric_code": code, "value": number, "unit": unit})

    checklist: list[dict] = []
    seen_items: set[str] = set()
    for raw_item in checklist_rows:
        if not isinstance(raw_item, Mapping):
            raise InspectionError("检查观察格式无效")
        if set(raw_item) - {"item_code", "value"}:
            raise InspectionError("检查观察包含未知字段")
        code = _text(
            raw_item.get("item_code"),
            field="检查项编码",
            limit=80,
            required=True,
        )
        item = indexes["item_by_code"].get(code)
        if item is None:
            raise InspectionError("检查项不在当前巡店标准中")
        if code in seen_items:
            raise InspectionError("同一检查项只能记录一次观察值")
        raw_value = raw_item.get("value")
        input_type = str(item.get("input_type") or "").strip()
        if input_type == "boolean":
            if not isinstance(raw_value, bool):
                raise InspectionError("是否类检查项只能提交是或否")
            normalized_value: Any = raw_value
        elif input_type == "document":
            if not isinstance(raw_value, str):
                raise InspectionError("记录凭证类检查项必须填写可追溯记录")
            normalized_value = _text(
                raw_value,
                field="检查观察值",
                limit=1000,
                required=True,
            )
        elif input_type == "number":
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise InspectionError("数值类检查项格式无效")
            try:
                number = float(raw_value)
            except OverflowError:
                raise InspectionError("检查观察值必须是有限数字") from None
            if not math.isfinite(number):
                raise InspectionError("检查观察值必须是有限数字")
            normalized_value = number
        elif input_type in {"text", "select"}:
            if not isinstance(raw_value, str):
                raise InspectionError("检查观察值格式无效")
            normalized_value = _text(
                raw_value,
                field="检查观察值",
                limit=1000,
                required=True,
            )
        else:
            raise InspectionError("当前检查项类型不受支持")
        seen_items.add(code)
        checklist.append({"item_code": code, "value": normalized_value})
    return {"metrics": metrics, "checklist": checklist}


def _build_strict_visit_contract(
    raw: Mapping[str, Any],
    industry_key: str,
    *,
    standard_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not _strict_checklist_requested(raw):
        return None
    claimed_key = raw.get("template_key")
    if claimed_key not in (None, "") and str(claimed_key).strip() != industry_key:
        raise InspectionError("巡店模板行业与当前行业不一致")
    version = _text(
        raw.get("template_version"),
        field="巡店模板版本",
        limit=40,
        required=True,
    )
    try:
        if standard_snapshot is None:
            snapshot = {
                "template_key": industry_key,
                "template_version": inspectionstandards.CATALOG_VERSION,
                "as_of": inspectionstandards.CATALOG_AS_OF,
                "catalog_sha256": inspectionstandards.version_summary(industry_key)["sha256"],
                "items": inspectionstandards.effective_checklist(industry_key),
                "capture_slots": inspectionstandards.capture_slots(industry_key),
                "metrics": inspectionstandards.metric_catalog(industry_key),
            }
        elif not isinstance(standard_snapshot, Mapping):
            raise inspectionstandards.InspectionStandardError(
                "企业巡店标准快照格式无效"
            )
        else:
            snapshot = copy.deepcopy(dict(standard_snapshot))
    except inspectionstandards.InspectionStandardError as exc:
        raise InspectionError("当前行业巡店标准不可用") from exc
    if (
        snapshot.get("template_key") != industry_key
        or not isinstance(snapshot.get("template_version"), str)
        or not snapshot.get("template_version")
    ):
        raise InspectionError("当前行业巡店标准不可用")
    standard_refreshed = False
    if version != snapshot["template_version"]:
        # 第 2 期：同一产品基线下只是企业/区域/门店覆盖变了，就用服务端最新快照
        # 重新核对本次填写的内容；真正影响到填写项才要求重填。产品基线本身
        # 换代（采集位/检查项整体变化）仍要求刷新。
        if standard_snapshot is None or not _same_base_catalog(version, snapshot):
            raise InspectionError("巡店模板版本已更新，请刷新后重新提交")
        standard_refreshed = True
    raw_file_slots = _decoded_json_field(raw, "file_slots", "file_slots_json", [])
    raw_observations = _decoded_json_field(
        raw,
        "observations",
        "observations_json",
        {"metrics": [], "checklist": []},
    )
    try:
        file_slots = _normalize_file_slots(raw_file_slots, snapshot)
        snapshot["file_slots"] = list(file_slots)
        observations = _normalize_observations(raw_observations, snapshot)
    except InspectionError as exc:
        if not standard_refreshed:
            raise
        raise InspectionError(
            _refresh_conflict_message(industry_key, raw_observations, snapshot, exc)
        ) from exc
    return {
        "template_key": industry_key,
        "template_version": str(snapshot["template_version"]),
        "template_snapshot": snapshot,
        "observations": observations,
        "file_slots": file_slots,
        "standard_refreshed": standard_refreshed,
    }


def _same_base_catalog(claimed_version: str, snapshot: Mapping[str, Any]) -> bool:
    base = str(
        snapshot.get("base_catalog_version") or inspectionstandards.CATALOG_VERSION
    )
    return claimed_version == base or claimed_version.startswith(base + "+")


def _refresh_conflict_message(
    industry_key: str,
    raw_observations: Any,
    snapshot: Mapping[str, Any],
    error: InspectionError,
) -> str:
    """标准刚改过且影响到本次填写项时，告诉店员具体是哪几项要重填。"""
    current = {
        str(item.get("item_code") or "")
        for item in snapshot.get("items") or []
        if isinstance(item, Mapping)
    }
    try:
        labels = {
            str(item.get("item_code") or ""): str(item.get("label") or "")
            for item in inspectionstandards.effective_checklist(industry_key)
        }
    except inspectionstandards.InspectionStandardError:
        labels = {}
    affected: list[str] = []
    rows = (
        raw_observations.get("checklist")
        if isinstance(raw_observations, Mapping) else None
    )
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
        for row in rows:
            code = str(row.get("item_code") or "") if isinstance(row, Mapping) else ""
            if code and code not in current:
                affected.append(labels.get(code) or code)
    if affected:
        names = "、".join(dict.fromkeys(affected[:5]))
        return f"老板刚调整了巡店标准，「{names}」已不在本店检查范围内。请刷新页面，按新标准重新填写后再提交"
    return f"老板刚调整了巡店标准，本次填写的内容不符合新标准（{error}）。请刷新页面后重新填写"


def _stored_visit_contract(row: Mapping[str, Any]) -> dict[str, Any] | None:
    template_key = row.get("template_key")
    template_version = row.get("template_version")
    snapshot_json = row.get("template_snapshot_json")
    observations_json = row.get("observations_json")
    # Schema 52 made the JSON columns non-null and deliberately retained
    # ``{}``/``[]`` as the on-disk sentinel for pre-checklist visits.  Treat
    # that exact combination as legacy; a partially populated strict
    # contract must still fail closed below.
    if (
        template_key in (None, "")
        and template_version in (None, "")
        and snapshot_json in (None, "", "{}")
        and observations_json in (None, "", "[]")
    ):
        return None
    if any(value in (None, "") for value in (
        template_key, template_version, snapshot_json, observations_json,
    )):
        raise InspectionConflict("巡店标准快照不完整")
    snapshot = db.jloads(snapshot_json, None)
    indexes = _snapshot_indexes(snapshot)
    if (
        str(template_key) != indexes["template_key"]
        or str(template_version) != indexes["template_version"]
    ):
        raise InspectionConflict("巡店模板版本与快照不一致")
    file_slots = _normalize_file_slots(snapshot.get("file_slots"), snapshot)
    observations_raw = db.jloads(observations_json, None)
    observations = _normalize_observations(observations_raw, snapshot)
    return {
        "template_key": str(template_key),
        "template_version": str(template_version),
        "template_snapshot": copy.deepcopy(dict(snapshot)),
        "observations": observations,
        "file_slots": file_slots,
    }


def _model_safe_standard_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Expose inspection instructions, never visit operating observations.

    Metric definitions and every submitted observation are intentionally left
    out: revenue, staffing and record values belong to the boss-facing detail,
    not the visual model prompt.
    """
    return copy.deepcopy({
        key: snapshot.get(key)
        for key in (
            "template_key", "template_version", "as_of", "catalog_sha256",
            "items", "capture_slots", "file_slots",
        )
    })


def _valid_industry_keys() -> set[str]:
    return {
        str(item.get("key") or "")
        for item in departments.list_depts()
        if isinstance(item, dict) and item.get("key")
    }


def _tenant_industry(tid: int, industry_key: str) -> dict:
    tenant = db.one(
        "SELECT id,enabled,industries_json FROM tenants WHERE id=?",
        (int(tid),),
    )
    if not tenant or not int(tenant.get("enabled") or 0):
        raise InspectionForbidden("企业账号不存在或已停用")
    industry = _text(
        industry_key,
        field="行业",
        limit=50,
        required=True,
    )
    if industry not in _valid_industry_keys():
        raise InspectionForbidden("该行业未开通巡店能力")
    mapped = db.q(
        "SELECT industry_key FROM tenant_industry WHERE tenant_id=?",
        (int(tid),),
    )
    configured = {
        str(item["industry_key"])
        for item in mapped
        if item.get("industry_key")
    }
    # schema 51 已把旧 industries_json 中的显式值迁移到
    # tenant_industry；运行期不能再用旧字段降级成“空=全开”。
    # 平台租户同样由 startup 同步出显式映射，便于审计。
    if not configured or industry not in configured:
        raise InspectionForbidden("企业未授权该行业")
    return {**tenant, "industry_key": industry}


# 门店可见范围：老板/平台管理员/总监看全部门店；经理/员工只能看、
# 操作老板绑定给自己的门店（user_branch）。
NO_BRANCH_NOTICE = "老板还没给你分配门店，请联系老板"
UNASSIGNED_REGION = "未分区"
_REGION_LABEL_SQL = "COALESCE(NULLIF(b.region,''),'未分区')"
# 逾期口径：已提交复查、等老板审核的整改不算逾期（球在老板手里）。
_OVERDUE_EXCLUDED_SQL = "('closed','awaiting_recheck')"
_MEMBER_TITLES = ("director", "manager", "staff")
MAX_MEMBER_BRANCHES = 2000


def _member_title(user: Mapping[str, Any]) -> str:
    title = str(user.get("job_title") or "staff")
    return title if title in _MEMBER_TITLES else "staff"


def sees_all_branches(user: Mapping[str, Any]) -> bool:
    """老板、平台管理员和总监不受门店绑定限制。"""
    role = str(user.get("role") or "")
    if role in {"root", "owner"}:
        return True
    return role == "member" and _member_title(user) == "director"


def can_manage_branches(user: Mapping[str, Any]) -> bool:
    """新建/停用门店、复查通过或驳回：仅老板/平台管理员/总监。"""
    return sees_all_branches(user)


def branch_scope_sql(
    user: Mapping[str, Any],
    tid: int,
    column: str,
) -> tuple[str, tuple]:
    """返回追加到 WHERE 的门店范围条件；看全部时为空串。

    用子查询而不是展开 IN 列表：经理绑定上千家门店也不会碰到
    SQLite 绑定变量上限。
    """
    if sees_all_branches(user):
        return "", ()
    if not re.fullmatch(r"[a-z_]{1,20}(\.[a-z_]{1,20})?", column):
        raise ValueError("invalid branch column")
    return (
        f" AND {column} IN (SELECT ub.branch_id FROM user_branch ub "
        "WHERE ub.tenant_id=? AND ub.user_id=?)",
        (int(tid), int(user["id"])),
    )


def _assert_branch_visible(
    user: Mapping[str, Any],
    tid: int,
    branch_id: int,
) -> None:
    if sees_all_branches(user):
        return
    bound = db.one(
        "SELECT 1 AS ok FROM user_branch WHERE tenant_id=? AND user_id=? "
        "AND branch_id=?",
        (int(tid), int(user["id"]), int(branch_id)),
    )
    if not bound:
        # 与“不存在”同样返回 404，不泄露别人负责的门店。
        raise InspectionNotFound("门店不存在或未分配给你")


def _actor(
    tid: int,
    uid: int,
    industry_key: str,
    *,
    manager: bool = False,
    branch_admin: bool = False,
) -> dict:
    _tenant_industry(tid, industry_key)
    user = db.one(
        "SELECT id,tenant_id,role,modules_json,enabled,job_title FROM users WHERE id=?",
        (int(uid),),
    )
    if not user or not int(user.get("enabled") or 0):
        raise InspectionForbidden("账号不存在或已停用")
    role = str(user.get("role") or "")
    is_platform_root = role == "root" and int(user["tenant_id"]) == 1
    if role not in {"root", "owner", "member"}:
        raise InspectionForbidden("当前账号角色不允许使用巡店能力")
    if role == "root" and not is_platform_root:
        raise InspectionForbidden("平台管理员账号归属无效")
    if not is_platform_root and int(user["tenant_id"]) != int(tid):
        raise InspectionForbidden("不能访问其他企业的巡店数据")
    if manager and role not in {"root", "owner"}:
        raise InspectionForbidden("仅企业主或平台管理员可完成该操作")
    if branch_admin and not can_manage_branches(user):
        raise InspectionForbidden("仅老板或总监可以完成该操作")
    if role == "member":
        modules = db.jloads(user.get("modules_json"), []) or []
        if not isinstance(modules, list) or industry_key not in modules:
            raise InspectionForbidden("账号未授权该行业板块")
    return user


def task_scope(tid: int, uid: int, task_id: int) -> dict:
    """返回巡店任务的服务端权威作用域。

    通用任务路由不得把 ``emp_idx=10`` 当成 content 任务；行业必须
    由 inspection_visit 反查，并再走一次 actor 授权。
    """
    row = db.one(
        "SELECT v.id visit_id,v.tenant_id,v.industry_key,v.branch_id,v.status "
        "FROM inspection_visit v JOIN task t ON t.id=v.task_id "
        "AND t.tenant_id=v.tenant_id WHERE t.id=? AND t.tenant_id=? "
        "AND t.emp_idx=? AND v.deleted_at IS NULL",
        (int(task_id), int(tid), EMPLOYEE_IDX),
    )
    if not row:
        raise InspectionNotFound("巡店任务不存在")
    user = _actor(tid, uid, str(row["industry_key"]))
    _branch_scope(
        tid, str(row["industry_key"]), int(row["branch_id"]), actor=user,
    )
    return {
        "visit_id": int(row["visit_id"]),
        "tenant_id": int(row["tenant_id"]),
        "industry_key": str(row["industry_key"]),
        "branch_id": int(row["branch_id"]),
        "status": str(row["status"]),
    }


def normalize_branch_input(raw: Mapping[str, Any]) -> dict:
    if not isinstance(raw, Mapping):
        raise InspectionError("门店信息格式无效")
    return {
        "name": _text(raw.get("name"), field="门店名称", limit=80, required=True),
        "region": _text(raw.get("region"), field="所属区域", limit=60),
        "address": _text(raw.get("address"), field="门店地址", limit=200),
    }


def _branch_scope(
    tid: int,
    industry_key: str,
    branch_id: int,
    *,
    actor: Mapping[str, Any] | None = None,
) -> dict:
    """反查可巡门店；传入 actor 时同时校验经理/员工的门店绑定。"""
    row = db.one(
        "SELECT id,tenant_id,industry_key,name,region,address,active,"
        "created_by,created_at,updated_at FROM store_branch "
        "WHERE id=? AND tenant_id=? AND industry_key=? AND active=1",
        (int(branch_id), int(tid), industry_key),
    )
    if not row:
        # 404 而非 403：不泄露其他租户是否存在该门店。
        raise InspectionNotFound("门店不存在或已停用")
    if actor is not None:
        _assert_branch_visible(actor, tid, int(row["id"]))
    row["active"] = bool(row.get("active"))
    return row


def create_branch(
    tid: int,
    uid: int,
    industry_key: str,
    raw: Mapping[str, Any],
) -> dict:
    """创建一个租户内的门店。

    ``industry_key`` 必须由路由层的当前行业上下文传入。如果客户端
    额外塞了 industry/industry_key，只用于检测篡改，绝不用它选行业。
    """
    # 新建门店只允许老板/平台管理员/总监；经理和员工只能巡
    # 老板分配给自己的门店，不能自建门店绕开分配。
    _actor(tid, uid, industry_key, branch_admin=True)
    body = normalize_branch_input(raw)
    for key in ("industry", "industry_key"):
        claimed = raw.get(key)
        if claimed not in (None, "") and str(claimed) != industry_key:
            raise InspectionForbidden("门店行业与企业授权不一致")
    now = time.time()
    try:
        with db.atomic() as connection:
            cursor = connection.execute(
                "INSERT INTO store_branch(tenant_id,industry_key,name,region,address,"
                "active,created_by,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?)",
                (
                    int(tid), industry_key, body["name"], body["region"],
                    body["address"], int(uid), now, now,
                ),
            )
            branch_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise InspectionConflict("同行业下已有同名门店") from exc
    return _branch_scope(tid, industry_key, branch_id)


def list_branches(
    tid: int,
    uid: int,
    industry_key: str,
    *,
    include_inactive: bool = False,
) -> list[dict]:
    """列出当前企业、当前行业的可巡门店（经理/员工只含分配给自己的）。"""
    user = _actor(tid, uid, industry_key)
    scope_sql, scope_params = branch_scope_sql(user, tid, "id")
    rows = db.q(
        "SELECT id,tenant_id,industry_key,name,region,address,active,created_by,"
        "created_at,updated_at FROM store_branch WHERE tenant_id=? "
        "AND industry_key=? "
        + ("" if include_inactive else "AND active=1 ")
        + scope_sql
        + " ORDER BY active DESC,region,name,id",
        (int(tid), industry_key, *scope_params),
    )
    for row in rows:
        row["id"] = int(row["id"])
        row["tenant_id"] = int(row["tenant_id"])
        row["active"] = bool(row.get("active"))
    return rows


def set_branch_active(
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
    *,
    active: bool,
) -> dict:
    """停用/恢复门店，仅老板/平台管理员/总监可操作。

    停用后门店不再出现在可巡列表，其未闭环整改也不再计入 KPI；
    历史巡店记录仍可查看。
    """
    _actor(tid, uid, industry_key, branch_admin=True)
    if not isinstance(active, bool):
        raise InspectionError("门店启用状态格式无效")
    target_id = _positive_id(branch_id, "门店")
    now = time.time()
    with db.atomic() as connection:
        row = connection.execute(
            "SELECT id,active FROM store_branch WHERE id=? AND tenant_id=? "
            "AND industry_key=?",
            (target_id, int(tid), industry_key),
        ).fetchone()
        if not row:
            raise InspectionNotFound("门店不存在")
        if bool(row["active"]) != active:
            # row_version 同步推进，避免停用前生成的导入预览静默覆盖。
            connection.execute(
                "UPDATE store_branch SET active=?,row_version=row_version+1,"
                "updated_at=? WHERE id=? AND tenant_id=?",
                (1 if active else 0, now, target_id, int(tid)),
            )
        current = connection.execute(
            "SELECT id,tenant_id,industry_key,name,region,address,active,"
            "created_by,created_at,updated_at FROM store_branch WHERE id=?",
            (target_id,),
        ).fetchone()
    result = dict(current)
    result["active"] = bool(result.get("active"))
    return result


def branch_scope_info(tid: int, uid: int, industry_key: str) -> dict:
    """巡店页需要的门店范围与按钮权限，全部由服务端判定。"""
    user = _actor(tid, uid, industry_key)
    role = str(user.get("role") or "")
    all_branches = sees_all_branches(user)
    assigned = None
    if not all_branches:
        assigned = int((db.one(
            "SELECT COUNT(*) n FROM user_branch ub JOIN store_branch b "
            "ON b.id=ub.branch_id AND b.tenant_id=ub.tenant_id "
            "WHERE ub.tenant_id=? AND ub.user_id=? AND b.industry_key=? "
            "AND b.active=1",
            (int(tid), int(user["id"]), industry_key),
        ) or {}).get("n") or 0)
    return {
        "all_branches": all_branches,
        "assigned_branches": assigned,
        "notice": NO_BRANCH_NOTICE if assigned == 0 else "",
        "can_manage_branches": can_manage_branches(user),
        "can_review": can_manage_branches(user),
        "can_assign_actions": role in {"root", "owner"},
    }


def _search_text(value: Any, *, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InspectionError(f"{field}格式无效")
    clean = value.strip()
    if len(clean) > limit or any(ord(char) < 32 for char in clean):
        raise InspectionError(f"{field}格式无效")
    return clean


def _branch_assignees(tid: int, branch_ids: Sequence[int]) -> dict[int, list[dict]]:
    ids = [int(value) for value in branch_ids][:200]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    result: dict[int, list[dict]] = {}
    for row in db.q(
        "SELECT ub.branch_id,u.id,u.username,u.job_title FROM user_branch ub "
        "JOIN users u ON u.id=ub.user_id AND u.tenant_id=ub.tenant_id "
        f"WHERE ub.tenant_id=? AND ub.branch_id IN ({placeholders}) "
        "AND u.role='member' ORDER BY u.id",
        (int(tid), *ids),
    ):
        result.setdefault(int(row["branch_id"]), []).append({
            "id": int(row["id"]),
            "username": str(row.get("username") or ""),
            "job_title": _member_title(row),
        })
    return result


def search_branches(
    tid: int,
    uid: int,
    industry_key: str,
    *,
    q: str = "",
    region: str = "",
    limit: int = 20,
    before_id: int | None = None,
) -> dict:
    """服务端权威 tenant + actor + industry + 门店绑定作用域的有界门店搜索。"""
    user = _actor(int(tid), int(uid), industry_key)
    clean_q = _search_text(q, field="门店搜索词", limit=80)
    clean_region = _search_text(region, field="门店区域", limit=60)
    if isinstance(limit, bool):
        raise InspectionError("门店分页条数无效")
    try:
        page_size = int(limit)
    except (TypeError, ValueError):
        raise InspectionError("门店分页条数无效") from None
    if not 1 <= page_size <= 50:
        raise InspectionError("门店分页条数必须在 1-50 之间")
    cursor = None
    if before_id is not None:
        if isinstance(before_id, bool):
            raise InspectionError("门店分页游标无效")
        try:
            cursor = int(before_id)
        except (TypeError, ValueError):
            raise InspectionError("门店分页游标无效") from None
        if cursor < 1:
            raise InspectionError("门店分页游标无效")

    conditions = ["tenant_id=?", "industry_key=?", "active=1"]
    params: list = [int(tid), industry_key]

    def like(value: str) -> str:
        return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    if clean_q:
        conditions.append(
            "(COALESCE(store_code,'') LIKE ? ESCAPE '\\' "
            "OR name LIKE ? ESCAPE '\\')"
        )
        pattern = like(clean_q)
        params.extend((pattern, pattern))
    if clean_region:
        conditions.append("region LIKE ? ESCAPE '\\'")
        params.append(like(clean_region))
    if cursor is not None:
        conditions.append("id<?")
        params.append(cursor)
    scope_sql, scope_params = branch_scope_sql(user, tid, "id")
    rows = db.q(
        "SELECT id,industry_key,store_code,name,region,address,active "
        "FROM store_branch WHERE "
        + " AND ".join(conditions)
        + scope_sql
        + " ORDER BY id DESC LIMIT ?",
        (*params, *scope_params, page_size + 1),
    )
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    items = [{
        "id": int(row["id"]),
        "industry_key": str(row["industry_key"]),
        "store_code": str(row.get("store_code") or ""),
        "name": str(row.get("name") or ""),
        "region": str(row.get("region") or ""),
        "address": str(row.get("address") or ""),
        "active": bool(row.get("active")),
    } for row in rows]
    if can_manage_branches(user):
        # 老板/总监在门店列表里直接看到每家店的负责人。
        assignees = _branch_assignees(tid, [item["id"] for item in items])
        for item in items:
            item["assignees"] = assignees.get(item["id"], [])
    return {
        "items": items,
        "next_before_id": items[-1]["id"] if has_more and items else None,
        "limit": page_size,
    }


def _team_admin(actor_uid: int) -> dict:
    actor = db.one(
        "SELECT u.id,u.tenant_id,u.role,u.enabled FROM users u "
        "JOIN tenants t ON t.id=u.tenant_id WHERE u.id=? AND t.enabled=1",
        (int(actor_uid),),
    )
    if (
        not actor
        or not int(actor.get("enabled") or 0)
        or str(actor.get("role") or "") not in {"owner", "root"}
    ):
        raise InspectionForbidden("仅老板可以给成员分配门店")
    return actor


def member_branch_assignments(tid: int) -> dict[int, list[int]]:
    """团队页：每个成员当前负责的门店编号。"""
    result: dict[int, list[int]] = {}
    for row in db.q(
        "SELECT user_id,branch_id FROM user_branch WHERE tenant_id=? "
        "ORDER BY user_id,branch_id",
        (int(tid),),
    ):
        result.setdefault(int(row["user_id"]), []).append(int(row["branch_id"]))
    return result


def assignable_branches(tid: int, *, limit: int = MAX_MEMBER_BRANCHES) -> dict:
    """团队页的门店多选候选：本企业所有启用门店（含行业，便于按成员板块过滤）。"""
    rows = db.q(
        "SELECT id,industry_key,store_code,name,region FROM store_branch "
        "WHERE tenant_id=? AND active=1 ORDER BY industry_key,region,name,id "
        "LIMIT ?",
        (int(tid), int(limit) + 1),
    )
    truncated = len(rows) > int(limit)
    return {
        "items": [{
            "id": int(row["id"]),
            "industry_key": str(row["industry_key"]),
            "store_code": str(row.get("store_code") or ""),
            "name": str(row.get("name") or ""),
            "region": str(row.get("region") or ""),
        } for row in rows[: int(limit)]],
        "truncated": truncated,
    }


def set_member_branches(
    actor_uid: int,
    member_uid: int,
    branch_ids: Any,
) -> dict:
    """老板整体替换某成员负责的门店（增删绑定在同一事务里完成）。"""
    actor = _team_admin(actor_uid)
    if isinstance(branch_ids, (str, bytes, bytearray)) or not isinstance(
        branch_ids, Sequence
    ):
        raise InspectionError("门店名单格式无效")
    ids: list[int] = []
    for value in branch_ids:
        branch_id = _positive_id(value, "门店")
        if branch_id not in ids:
            ids.append(branch_id)
    if len(ids) > MAX_MEMBER_BRANCHES:
        raise InspectionError(f"一个成员最多负责 {MAX_MEMBER_BRANCHES} 家门店")
    member = db.one(
        "SELECT id,tenant_id,role,modules_json FROM users WHERE id=?",
        (int(member_uid),),
    )
    if not member or (
        str(actor["role"]) != "root"
        and int(member["tenant_id"]) != int(actor["tenant_id"])
    ):
        raise InspectionNotFound("成员不存在")
    if str(member.get("role") or "") != "member":
        raise InspectionError("老板账号本来就能看全部门店，不需要分配")
    tid = int(member["tenant_id"])
    modules = db.jloads(member.get("modules_json"), []) or []
    modules = set(modules) if isinstance(modules, list) else set()
    found: dict[int, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        for row in db.q(
            "SELECT id,industry_key,name,active FROM store_branch "
            f"WHERE tenant_id=? AND id IN ({placeholders})",
            (tid, *chunk),
        ):
            found[int(row["id"])] = row
    for branch_id in ids:
        row = found.get(branch_id)
        if row is None:
            raise InspectionNotFound("门店不存在")
        if not int(row.get("active") or 0):
            raise InspectionConflict(f"门店「{row['name']}」已停用，不能分配")
        if str(row["industry_key"]) not in modules:
            raise InspectionError(
                f"门店「{row['name']}」所在行业未对该成员开通，请先在板块里开通"
            )
    now = time.time()
    with db.atomic() as connection:
        connection.execute(
            "DELETE FROM user_branch WHERE user_id=?", (int(member["id"]),)
        )
        connection.executemany(
            "INSERT INTO user_branch(tenant_id,user_id,branch_id,created_by,"
            "created_at) VALUES(?,?,?,?,?)",
            [
                (tid, int(member["id"]), branch_id, int(actor["id"]), now)
                for branch_id in ids
            ],
        )
    return {"user_id": int(member["id"]), "branch_ids": sorted(ids)}


def visit_files_visible(tid: int, uid: int, visit_id: int) -> bool:
    """巡店照片直链：经理/员工只能读自己负责门店的巡店照片。

    租户与行业板块已由文件中间件校验；这里只补门店绑定这一层。
    """
    user = db.one(
        "SELECT id,tenant_id,role,job_title,enabled FROM users WHERE id=?",
        (int(uid),),
    )
    if not user or not int(user.get("enabled") or 0):
        return False
    if sees_all_branches(user):
        return True
    row = db.one(
        "SELECT v.branch_id FROM inspection_visit v JOIN user_branch ub "
        "ON ub.branch_id=v.branch_id AND ub.tenant_id=v.tenant_id "
        "WHERE v.id=? AND v.tenant_id=? AND ub.user_id=? "
        "AND v.deleted_at IS NULL",
        (int(visit_id), int(tid), int(user["id"])),
    )
    return bool(row)


def normalize_visit_input(
    raw: Mapping[str, Any],
    *,
    industry_key: str | None = None,
    standard_snapshot: Mapping[str, Any] | None = None,
) -> dict:
    if not isinstance(raw, Mapping):
        raise InspectionError("巡店信息格式无效")
    request_key = _text(
        raw.get("request_key"),
        field="巡店请求号",
        limit=160,
        required=True,
    )
    if not _REQUEST_KEY_RE.fullmatch(request_key):
        raise InspectionError("巡店请求号无效，请刷新后重试")
    visit_at_raw = raw.get("visit_at")
    if visit_at_raw in (None, ""):
        visit_at = time.time()
    else:
        visit_at = _bounded_float(
            visit_at_raw,
            field="到店时间",
            minimum=946684800,
            maximum=time.time() + 7 * 86400,
        )
    strict_contract = None
    if _strict_checklist_requested(raw):
        if not industry_key:
            raise InspectionError("严格巡店必须指定服务端行业")
        strict_contract = _build_strict_visit_contract(
            raw,
            industry_key,
            standard_snapshot=standard_snapshot,
        )
    result = {
        "request_key": request_key,
        "visit_at": visit_at,
        "note": _text(raw.get("note"), field="巡店说明", limit=1000),
    }
    if strict_contract:
        result.update(strict_contract)
    else:
        result.update({
            "template_key": None,
            "template_version": None,
            "template_snapshot": None,
            "observations": None,
            "file_slots": None,
        })
    return result


def _normalize_photo(
    raw: Mapping[str, Any],
    tid: int,
    *,
    phase: str,
    standard_snapshot: Mapping[str, Any] | None = None,
) -> dict:
    if not isinstance(raw, Mapping):
        raise InspectionError("照片存储结果格式无效")
    storage_key = _text(
        raw.get("storage_key"),
        field="照片存储标识",
        limit=240,
        required=True,
    )
    expected_prefix = f"inspections/{int(tid)}/"
    if (
        not _STORAGE_RE.fullmatch(storage_key)
        or storage_key.startswith("/")
        or ".." in storage_key.split("/")
        or not storage_key.startswith(expected_prefix)
    ):
        raise InspectionError("照片存储标识不在当前企业目录")
    mime_type = _text(
        raw.get("mime_type"),
        field="照片格式",
        limit=40,
        required=True,
    ).lower()
    if mime_type not in _MIME_TYPES:
        raise InspectionError("巡店照片仅支持 JPEG、PNG 或 WebP")
    byte_size = _positive_id(raw.get("byte_size"), "照片大小")
    if byte_size > MAX_PHOTO_BYTES:
        raise InspectionError("单张巡店照片不能超过 8MB")
    digest = _text(raw.get("sha256"), field="照片摘要", limit=64, required=True).lower()
    if not _SHA256_RE.fullmatch(digest):
        raise InspectionError("照片摘要无效")
    width = _positive_id(raw.get("width"), "照片宽度")
    height = _positive_id(raw.get("height"), "照片高度")
    if (
        width > MAX_IMAGE_EDGE
        or height > MAX_IMAGE_EDGE
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise InspectionError("照片像素过大")
    if phase not in {"before", "recheck"}:
        raise InspectionError("照片阶段无效")
    capture_slot = _text(
        raw.get("capture_slot"),
        field="照片采集位",
        limit=80,
    ) or None
    item_code = _text(
        raw.get("item_code"),
        field="照片检查项",
        limit=80,
    ) or None
    for field, value in (("照片采集位", capture_slot), ("照片检查项", item_code)):
        if value is not None and not _STANDARD_CODE_RE.fullmatch(value):
            raise InspectionError(f"{field}格式无效")
    if standard_snapshot is not None and phase == "before":
        indexes = _snapshot_indexes(standard_snapshot)
        if capture_slot is None:
            raise InspectionError("严格巡店的每张照片必须绑定采集位")
        if capture_slot not in indexes["slot_by_code"]:
            raise InspectionError("照片采集位不在冻结的巡店标准中")
        if item_code is not None and item_code not in indexes["item_by_code"]:
            raise InspectionError("照片检查项不在冻结的巡店标准中")
    return {
        "storage_key": storage_key,
        "mime_type": mime_type,
        "byte_size": byte_size,
        "sha256": digest,
        "phase": phase,
        "caption": _text(raw.get("caption"), field="照片说明", limit=300),
        "width": width,
        "height": height,
        "capture_slot": capture_slot,
        "item_code": item_code,
    }


def _normalize_photos(
    records: Sequence[Mapping[str, Any]],
    tid: int,
    *,
    phase: str,
    standard_snapshot: Mapping[str, Any] | None = None,
    expected_file_slots: Sequence[str] | None = None,
) -> list[dict]:
    if isinstance(records, (str, bytes, bytearray)) or not isinstance(records, Sequence):
        raise InspectionError("照片列表格式无效")
    if not records:
        raise InspectionError("请至少上传 1 张巡店照片")
    if len(records) > MAX_PHOTOS:
        raise InspectionError(f"每次巡店最多上传 {MAX_PHOTOS} 张照片")
    photos = [
        _normalize_photo(
            item,
            tid,
            phase=phase,
            standard_snapshot=standard_snapshot,
        )
        for item in records
    ]
    if sum(item["byte_size"] for item in photos) > MAX_TOTAL_PHOTO_BYTES:
        raise InspectionError("巡店照片总大小不能超过 40MB")
    storage_keys = {item["storage_key"] for item in photos}
    if len(storage_keys) != len(photos):
        raise InspectionError("巡店照片不能重复")
    if standard_snapshot is not None and phase == "before":
        slots = [str(item["capture_slot"]) for item in photos]
        # Reuse the same catalog and required-slot gate used for the declared
        # multipart plan; this also rejects duplicate/unknown slots.
        normalized_slots = _normalize_file_slots(slots, standard_snapshot)
        expected = (
            list(expected_file_slots)
            if expected_file_slots is not None
            else list(standard_snapshot.get("file_slots") or [])
        )
        if normalized_slots != expected:
            raise InspectionError("上传文件与照片采集位未一一对应")
    return photos


def _validate_persisted_photo_contract(
    photo_rows: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any] | None,
) -> None:
    if contract is None:
        return
    snapshot = contract["template_snapshot"]
    indexes = _snapshot_indexes(snapshot)
    slots: list[str] = []
    for row in photo_rows:
        slot = row.get("capture_slot")
        item_code = row.get("item_code")
        if not isinstance(slot, str) or slot not in indexes["slot_by_code"]:
            raise InspectionConflict("巡店照片采集位不完整")
        if item_code not in (None, "") and item_code not in indexes["item_by_code"]:
            raise InspectionConflict("巡店照片检查项不在冻结标准中")
        slots.append(slot)
    normalized_slots = _normalize_file_slots(slots, snapshot)
    if normalized_slots != list(contract["file_slots"]):
        raise InspectionConflict("巡店照片与冻结采集计划不一致")


def sniff_photo_ext(data: bytes) -> str | None:
    """按文件内容（魔数）判断图片类型；不看文件名。

    部分安卓/微信内置浏览器上传的文件名没有扩展名（如 ``image``、``blob``），
    按扩展名判断会把正常照片误拒。
    """
    head = bytes(data[:16]) if isinstance(data, (bytes, bytearray)) else b""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def normalize_photo_upload(data: bytes, filename: str = "") -> dict:
    """校验、纠正方向并重编码为 JPEG，彻底移除 EXIF 与上传文件名。

    类型只认文件内容；``filename`` 仅为兼容旧调用保留，不参与判断。
    """
    import hashlib
    import io

    from PIL import Image, ImageOps

    from . import avatar

    ext = sniff_photo_ext(data)
    if ext is None:
        raise ValueError("巡店照片仅支持 JPEG、PNG 或 WebP，请直接用手机相机拍照上传")
    try:
        avatar.validate_upload_media(data, ext, "photo")
    except avatar.InvalidAvatarMedia as exc:
        raise ValueError(str(exc)) from exc
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            if "A" in image.getbands():
                base = Image.new("RGB", image.size, "white")
                base.paste(image, mask=image.getchannel("A"))
                image = base
            else:
                image = image.convert("RGB")
            image.thumbnail((4096, 4096))
            width, height = image.size
            output = io.BytesIO()
            image.save(output, "JPEG", quality=88, optimize=True)
            normalized = output.getvalue()
    except (OSError, ValueError) as exc:
        raise ValueError("巡店照片无法安全解析") from exc
    if not normalized or len(normalized) > MAX_PHOTO_BYTES:
        raise ValueError("巡店照片重编码后超过 8MB")
    return {
        "data": normalized,
        "mime_type": "image/jpeg",
        "byte_size": len(normalized),
        "sha256": hashlib.sha256(normalized).hexdigest(),
        "width": width,
        "height": height,
    }


def _find_request(tid: int, request_key: str) -> dict | None:
    return db.one(
        "SELECT id,tenant_id,industry_key,branch_id,created_by,status "
        "FROM inspection_visit WHERE tenant_id=? AND request_key=? "
        "AND deleted_at IS NULL",
        (int(tid), request_key),
    )


def _assert_replay_scope(
    row: dict,
    *,
    industry_key: str,
    branch_id: int,
) -> None:
    if (
        row["industry_key"] != industry_key
        or int(row["branch_id"]) != int(branch_id)
    ):
        raise InspectionConflict("巡店请求号已用于其他门店")


def _insert_visit(
    connection,
    *,
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
    request: dict,
    status: str,
    task_id: int | None,
) -> int:
    now = time.time()
    try:
        cursor = connection.execute(
            "INSERT INTO inspection_visit(tenant_id,industry_key,branch_id,"
            "employee_idx,task_id,request_key,status,created_by,version,visit_at,"
            "template_key,template_version,template_snapshot_json,observations_json,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?, ?,0,?,?,?,?,?,?,?)",
            (
                int(tid), industry_key, int(branch_id), EMPLOYEE_IDX,
                int(task_id) if task_id is not None else None,
                request["request_key"], status, int(uid), request["visit_at"],
                request.get("template_key"), request.get("template_version"),
                _json(request["template_snapshot"])
                if request.get("template_snapshot") is not None else "{}",
                _json(request["observations"])
                if request.get("observations") is not None else "[]",
                now, now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        existing = connection.execute(
            "SELECT id,industry_key,branch_id FROM inspection_visit "
            "WHERE tenant_id=? AND request_key=? AND deleted_at IS NULL",
            (int(tid), request["request_key"]),
        ).fetchone()
        if existing:
            if (
                existing["industry_key"] != industry_key
                or int(existing["branch_id"]) != int(branch_id)
            ):
                raise InspectionConflict("巡店请求号已用于其他门店") from None
            return int(existing["id"])
        raise InspectionConflict("巡店请求已提交，请勿重复操作") from exc
    visit_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO inspection_event(tenant_id,visit_id,kind,payload_json,"
        "created_by,created_at) VALUES(?,?,?,?,?,?)",
        (
            int(tid), visit_id, "visit_created",
            _json({
                "note": request["note"],
                # 标准在填写期间被老板改过、服务端已按最新标准自动核对通过。
                **({"standard_refreshed": True} if request.get("standard_refreshed") else {}),
            }),
            int(uid), now,
        ),
    )
    return visit_id


def _insert_photos(
    connection,
    *,
    tid: int,
    uid: int,
    visit_id: int,
    photos: Sequence[dict],
) -> list[int]:
    ids: list[int] = []
    now = time.time()
    for item in photos:
        cursor = connection.execute(
            "INSERT INTO inspection_photo(tenant_id,visit_id,storage_key,mime_type,"
            "byte_size,sha256,phase,caption,width,height,capture_slot,item_code,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(tid), int(visit_id), item["storage_key"], item["mime_type"],
                item["byte_size"], item["sha256"], item["phase"],
                item["caption"], item["width"], item["height"],
                item.get("capture_slot") or "", item.get("item_code") or "",
                int(uid), now,
            ),
        )
        ids.append(int(cursor.lastrowid))
    return ids


def _effective_standard_snapshot(
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
    raw: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Resolve the server-owned effective standard only for strict visits."""
    if not _strict_checklist_requested(raw):
        return None
    from . import inspectionoverrides

    try:
        return inspectionoverrides.effective_snapshot(
            int(tid), int(uid), industry_key, int(branch_id),
        )
    except inspectionoverrides.InspectionOverrideError as exc:
        raise InspectionError(exc.safe_message) from exc


def create_visit_draft(
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
    raw: Mapping[str, Any],
    photo_records: Sequence[Mapping[str, Any]],
    *,
    task_id: int | None = None,
) -> dict:
    """持久化已经过 HTTP 层媒体校验的初检照片。"""
    user = _actor(tid, uid, industry_key)
    branch = _branch_scope(tid, industry_key, branch_id, actor=user)
    standard_snapshot = _effective_standard_snapshot(
        tid, uid, industry_key, int(branch["id"]), raw,
    )
    request = normalize_visit_input(
        raw,
        industry_key=industry_key,
        standard_snapshot=standard_snapshot,
    )
    photos = _normalize_photos(
        photo_records,
        tid,
        phase="before",
        standard_snapshot=request.get("template_snapshot"),
        expected_file_slots=request.get("file_slots"),
    )
    existing = _find_request(tid, request["request_key"])
    if existing:
        _assert_replay_scope(existing, industry_key=industry_key, branch_id=branch["id"])
        return get_visit(tid, uid, industry_key, int(existing["id"]))
    with db.atomic() as connection:
        visit_id = _insert_visit(
            connection,
            tid=tid,
            uid=uid,
            industry_key=industry_key,
            branch_id=branch["id"],
            request=request,
            status="analyzing",
            task_id=task_id,
        )
        current = connection.execute(
            "SELECT status FROM inspection_visit WHERE id=? AND tenant_id=?",
            (visit_id, int(tid)),
        ).fetchone()
        if current and current["status"] == "analyzing":
            count = connection.execute(
                "SELECT COUNT(*) n FROM inspection_photo WHERE visit_id=? AND tenant_id=?",
                (visit_id, int(tid)),
            ).fetchone()["n"]
            if not count:
                _insert_photos(
                    connection,
                    tid=tid,
                    uid=uid,
                    visit_id=visit_id,
                    photos=photos,
                )
    return get_visit(tid, uid, industry_key, visit_id)


def create_visit_shell(
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
    raw: Mapping[str, Any],
    *,
    task_id: int | None = None,
) -> dict:
    """先建立 ``preparing`` 巡店锚点，供 HTTP 层按 visit_id 安全落图。"""
    user = _actor(tid, uid, industry_key)
    branch = _branch_scope(tid, industry_key, branch_id, actor=user)
    standard_snapshot = _effective_standard_snapshot(
        tid, uid, industry_key, int(branch["id"]), raw,
    )
    request = normalize_visit_input(
        raw,
        industry_key=industry_key,
        standard_snapshot=standard_snapshot,
    )
    existing = _find_request(tid, request["request_key"])
    if existing:
        _assert_replay_scope(
            existing,
            industry_key=industry_key,
            branch_id=branch["id"],
        )
        return get_visit(tid, uid, industry_key, int(existing["id"]))
    with db.atomic() as connection:
        visit_id = _insert_visit(
            connection,
            tid=tid,
            uid=uid,
            industry_key=industry_key,
            branch_id=branch["id"],
            request=request,
            status="preparing",
            task_id=task_id,
        )
    return get_visit(tid, uid, industry_key, visit_id)


def attach_visit_photos(
    tid: int,
    uid: int,
    industry_key: str,
    visit_id: int,
    photo_records: Sequence[Mapping[str, Any]],
) -> dict:
    """把已校验的照片一次性绑定到 shell，并推进为 ``analyzing``。"""
    user = _actor(tid, uid, industry_key)
    with db.atomic() as connection:
        visit = connection.execute(
            "SELECT id,status,branch_id,template_key,template_version,"
            "template_snapshot_json,observations_json FROM inspection_visit WHERE id=? "
            "AND tenant_id=? AND industry_key=? AND deleted_at IS NULL",
            (int(visit_id), int(tid), industry_key),
        ).fetchone()
        if not visit:
            raise InspectionNotFound("巡店记录不存在")
        _branch_scope(tid, industry_key, int(visit["branch_id"]), actor=user)
        contract = _stored_visit_contract(dict(visit))
        photos = _normalize_photos(
            photo_records,
            tid,
            phase="before",
            standard_snapshot=(contract or {}).get("template_snapshot"),
            expected_file_slots=(contract or {}).get("file_slots"),
        )
        existing = connection.execute(
            "SELECT storage_key,capture_slot,item_code FROM inspection_photo WHERE tenant_id=? "
            "AND visit_id=? AND phase='before' ORDER BY id",
            (int(tid), int(visit_id)),
        ).fetchall()
        if existing:
            if [
                (
                    row["storage_key"],
                    row["capture_slot"] or None,
                    row["item_code"] or None,
                )
                for row in existing
            ] != [
                (item["storage_key"], item.get("capture_slot"), item.get("item_code"))
                for item in photos
            ]:
                raise InspectionConflict("巡店照片已由另一个请求提交")
            if visit["status"] not in {"analyzing", "completed"}:
                raise InspectionConflict("巡店照片状态异常")
            return get_visit(tid, uid, industry_key, int(visit_id))
        if visit["status"] != "preparing":
            raise InspectionConflict("巡店照片已由另一个请求处理")
        _insert_photos(
            connection,
            tid=tid,
            uid=uid,
            visit_id=int(visit_id),
            photos=photos,
        )
        changed = connection.execute(
            "UPDATE inspection_visit SET status='analyzing',updated_at=? "
            "WHERE id=? AND tenant_id=? AND status='preparing'",
            (time.time(), int(visit_id), int(tid)),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("巡店照片已由另一个请求处理")
    return get_visit(tid, uid, industry_key, int(visit_id))


def _normalize_bbox(value: Any) -> list[float] | None:
    if value in (None, "", []):
        return None
    if isinstance(value, Mapping):
        value = [value.get("x"), value.get("y"), value.get("width"), value.get("height")]
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence) or len(value) != 4:
        raise InspectionError("证据标注框格式无效")
    return [
        _bounded_float(item, field="证据标注框", minimum=0, maximum=1)
        for item in value
    ]


def _canonical_confidence(value: Any) -> Any:
    """只规范化无歧义的置信度表示，不为缺失值猜测默认值。"""
    if value is None or isinstance(value, bool):
        return value
    original = value
    percent_suffix = False
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return value
        if cleaned.endswith("%"):
            percent_suffix = True
            cleaned = cleaned[:-1].strip()
        if not re.fullmatch(r"[+]?(?:\d+(?:\.\d*)?|\.\d+)", cleaned):
            return value
        try:
            value = float(cleaned)
        except ValueError:
            return original
    elif isinstance(value, (int, float)):
        value = float(value)
    else:
        return value
    if not math.isfinite(value):
        return original
    if percent_suffix:
        return value / 100.0 if 0 <= value <= 100 else original
    if 0 <= value <= 1:
        return value
    # JSON 中大于 1 的置信度数字只可能是百分数；不接受
    # 超界数字，也不把缺失值补成通过阈值。
    if 1 < value <= 100:
        return value / 100.0
    return original


def _canonical_bbox(value: Any) -> list[float] | None:
    """不可信标注框只是表现层辅助；无效时丢弃而不影响证据本身。"""
    if value in (None, "", []):
        return None
    if isinstance(value, Mapping):
        value = [value.get("x"), value.get("y"), value.get("width"), value.get("height")]
    if (
        isinstance(value, (str, bytes, bytearray))
        or not isinstance(value, Sequence)
        or len(value) != 4
    ):
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool):
            return None
        try:
            number = float(item)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or not 0 <= number <= 1:
            return None
        result.append(number)
    return result


def canonicalize_model_result(raw: Any) -> Any:
    """规范化视觉模型常见的无歧义表现差异。

    严禁在这里补 ``photo_id``/缺失的逐图结果或证据，也不得
    改写 analyzable/verdict。规范化后仍必须走同一个严格合同。
    """
    if not isinstance(raw, Mapping):
        return raw
    value = copy.deepcopy(dict(raw))
    reviews = value.get("photo_reviews")
    if (
        isinstance(reviews, Sequence)
        and not isinstance(reviews, (str, bytes, bytearray))
    ):
        for review in reviews:
            if not isinstance(review, dict):
                continue
            analyzable = review.get("analyzable")
            if isinstance(analyzable, str):
                marker = analyzable.strip().lower()
                if marker in {"true", "false"}:
                    review["analyzable"] = marker == "true"
            facts = review.get("visible_facts")
            if isinstance(facts, str) and facts.strip():
                review["visible_facts"] = [facts.strip()]
            if "confidence" in review:
                review["confidence"] = _canonical_confidence(review["confidence"])

    issues = value.get("issues")
    if (
        isinstance(issues, Sequence)
        and not isinstance(issues, (str, bytes, bytearray))
    ):
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if "confidence" in issue:
                issue["confidence"] = _canonical_confidence(issue["confidence"])
            category = issue.get("category")
            if not isinstance(category, str) or not _CATEGORY_RE.fullmatch(category.strip()):
                issue["category"] = "other"
            else:
                issue["category"] = category.strip()
            evidence_items = issue.get("evidence")
            if (
                isinstance(evidence_items, Sequence)
                and not isinstance(evidence_items, (str, bytes, bytearray))
            ):
                for evidence in evidence_items:
                    if not isinstance(evidence, dict) or "bbox" not in evidence:
                        continue
                    bbox = _canonical_bbox(evidence.get("bbox"))
                    if bbox is None:
                        evidence.pop("bbox", None)
                    else:
                        evidence["bbox"] = bbox
            action = issue.get("action")
            if not isinstance(action, dict):
                continue
            if "due_days" in action:
                due = action.get("due_days")
                if isinstance(due, str) and due.strip():
                    cleaned_due = due.strip()
                    if cleaned_due == "立即":
                        action["due_days"] = 0
                    else:
                        match = re.fullmatch(r"(\d{1,2})\s*天", cleaned_due)
                        if match and 0 <= int(match.group(1)) <= 90:
                            action["due_days"] = int(match.group(1))
            plan = action.get("plan")
            if (
                isinstance(plan, Sequence)
                and not isinstance(plan, (str, bytes, bytearray))
                and plan
                and all(isinstance(item, str) for item in plan)
            ):
                pieces = [item.strip()[:300] for item in plan[:12] if item.strip()]
                if pieces:
                    action["plan"] = "；".join(pieces)[:1200]
    return value


RETAKE_REASONS = {
    "unanalyzable": "照片看不清或拍错了地方",
    "low_confidence": "照片不够清楚，AI 没把握",
    "unresolved": "多次补拍仍看不清，请人工查看",
}
# 同一次巡店最多补拍 3 轮；再看不清就把这张标成“需人工查看”并出报告，
# 不让巡店永远卡在待补拍。
MAX_RETAKE_ROUNDS = 3


def _retake_review(
    photo_id: int,
    value: Mapping[str, Any],
    *,
    reason: str,
    confidence: float | None = None,
) -> dict:
    """看不清的照片只记“需补拍”，不读模型对它给出的结论。"""
    raw_facts = value.get("visible_facts")
    facts: list[str] = []
    if isinstance(raw_facts, Sequence) and not isinstance(
        raw_facts, (str, bytes, bytearray)
    ):
        for item in list(raw_facts)[:12]:
            if isinstance(item, str) and item.strip() and not _CONTROL_RE.search(item):
                facts.append(item.strip()[:300])
    if confidence is None:
        raw_confidence = value.get("confidence")
        if (
            isinstance(raw_confidence, (int, float))
            and not isinstance(raw_confidence, bool)
            and math.isfinite(float(raw_confidence))
            and 0 <= float(raw_confidence) <= 1
        ):
            confidence = float(raw_confidence)
    stored_reason = value.get("retake_reason")
    if stored_reason in RETAKE_REASONS:
        reason = str(stored_reason)
    return {
        "photo_id": int(photo_id),
        "analyzable": reason != "unanalyzable",
        "verdict": "retake",
        "confidence": confidence,
        "visible_facts": facts,
        "needs_retake": True,
        "retake_reason": reason,
    }


def _normalize_model_result_strict(
    raw: Mapping[str, Any],
    allowed_photo_ids: set[int],
    *,
    allow_clean_candidate: bool = False,
) -> dict:
    """将不可信的视觉模型 JSON 收紧为可写入的业务事实。"""
    if not isinstance(raw, Mapping):
        raise InspectionError("巡店识别结果格式无效")
    if not allowed_photo_ids:
        raise InspectionError("巡店识别结果没有可校验照片")
    summary = _text(
        raw.get("summary", raw.get("summary_md")),
        field="巡店结论",
        limit=4000,
        required=True,
    )
    score = _bounded_float(raw.get("score"), field="巡店得分", minimum=0, maximum=100)
    if "issues" not in raw:
        raise InspectionError("巡店识别结果必须显式提供问题列表")
    raw_reviews = raw.get("photo_reviews")
    if (
        isinstance(raw_reviews, (str, bytes, bytearray))
        or not isinstance(raw_reviews, Sequence)
    ):
        raise InspectionError("逐图核查结果格式无效")
    if len(raw_reviews) > MAX_PHOTOS:
        raise InspectionError("逐图核查结果超过照片上限")
    photo_reviews: list[dict] = []
    review_by_photo_id: dict[int, dict] = {}
    for position, value in enumerate(raw_reviews, start=1):
        if not isinstance(value, Mapping):
            raise InspectionError(f"第 {position} 张照片的核查结果无效")
        photo_id = _positive_id(value.get("photo_id"), "逐图核查照片")
        if photo_id not in allowed_photo_ids:
            raise InspectionError("逐图核查引用了不属于本次巡店的照片")
        if photo_id in review_by_photo_id:
            raise InspectionError("逐图核查结果包含重复照片")
        analyzable = value.get("analyzable")
        if not isinstance(analyzable, bool):
            raise InspectionError("逐图核查必须明确照片是否可分析")
        if not analyzable:
            # 第 2 期：一张照片看不清只让店员补拍这一张，其他照片的结果保留，
            # 不再让整次巡店作废。补拍照片不能作为问题证据。
            review = _retake_review(photo_id, value, reason="unanalyzable")
            review_by_photo_id[photo_id] = review
            photo_reviews.append(review)
            continue
        confidence = _bounded_float(
            value.get("confidence"),
            field="逐图核查置信度",
            minimum=0,
            maximum=1,
        )
        if confidence < MIN_PHOTO_REVIEW_CONFIDENCE or value.get("needs_retake") is True:
            review = _retake_review(
                photo_id, value, reason="low_confidence", confidence=confidence,
            )
            review_by_photo_id[photo_id] = review
            photo_reviews.append(review)
            continue
        verdict = _text(
            value.get("verdict"),
            field="逐图核查结论",
            limit=20,
            required=True,
        ).lower()
        if verdict not in {"clean", "issue"}:
            raise InspectionError("逐图核查结论只能为 clean/issue")
        raw_facts = value.get("visible_facts")
        if (
            isinstance(raw_facts, (str, bytes, bytearray))
            or not isinstance(raw_facts, Sequence)
            or not raw_facts
            or len(raw_facts) > 12
        ):
            raise InspectionError("逐图核查必须提供非空可见事实")
        visible_facts = [
            _text(item, field="逐图可见事实", limit=300, required=True)
            for item in raw_facts
        ]
        review = {
            "photo_id": photo_id,
            "analyzable": True,
            "verdict": verdict,
            "confidence": confidence,
            "visible_facts": visible_facts,
        }
        review_by_photo_id[photo_id] = review
        photo_reviews.append(review)
    if set(review_by_photo_id) != allowed_photo_ids:
        raise InspectionError("逐图核查未精确覆盖全部初检照片")
    retake_photo_ids = {
        photo_id
        for photo_id, review in review_by_photo_id.items()
        if review.get("needs_retake")
    }

    raw_issues = raw.get("issues")
    if isinstance(raw_issues, (str, bytes, bytearray)) or not isinstance(raw_issues, Sequence):
        raise InspectionError("问题列表格式无效")
    if len(raw_issues) > 30:
        raise InspectionError("单次巡店最多记录 30 个问题")
    issues: list[dict] = []
    for position, value in enumerate(raw_issues, start=1):
        if not isinstance(value, Mapping):
            raise InspectionError(f"第 {position} 个问题格式无效")
        severity = _text(
            value.get("severity"),
            field="问题严重度",
            limit=20,
            required=True,
        ).lower()
        if severity not in SEVERITIES:
            raise InspectionError("问题严重度只能为 critical/high/medium/low")
        category = _text(
            value.get("category", "other"),
            field="问题分类",
            limit=50,
            required=True,
        )
        if not _CATEGORY_RE.fullmatch(category):
            raise InspectionError("问题分类格式无效")
        confidence = _bounded_float(
            value.get("confidence", 0),
            field="问题置信度",
            minimum=0,
            maximum=1,
        )
        raw_evidence = value.get("evidence", [])
        if isinstance(raw_evidence, (str, bytes, bytearray)) or not isinstance(raw_evidence, Sequence):
            raise InspectionError("问题证据格式无效")
        if not raw_evidence:
            raise InspectionError("每个图片巡店问题都必须绑定证据照片")
        if len(raw_evidence) > MAX_PHOTOS:
            raise InspectionError("单个问题的证据照片过多")
        evidence: list[dict] = []
        seen_photo_ids: set[int] = set()
        for evidence_value in raw_evidence:
            if not isinstance(evidence_value, Mapping):
                raise InspectionError("问题证据格式无效")
            photo_id = _positive_id(evidence_value.get("photo_id"), "证据照片")
            if photo_id not in allowed_photo_ids:
                raise InspectionError("问题引用了不属于本次巡店的照片")
            if photo_id in seen_photo_ids:
                continue
            seen_photo_ids.add(photo_id)
            if photo_id in retake_photo_ids:
                # 看不清的照片不能当证据；补拍后会重新分析这一张。
                continue
            evidence.append({
                "photo_id": photo_id,
                "note": _text(evidence_value.get("note"), field="证据说明", limit=300),
                "bbox": _normalize_bbox(evidence_value.get("bbox")),
            })
        action_value = value.get("action")
        if not isinstance(action_value, Mapping):
            raise InspectionError("每个巡店问题都必须有整改计划")
        if not {"plan", "owner", "due_days"}.issubset(action_value):
            raise _contract_error(
                "IC_ACTION_REQUIRED",
                "整改计划必须显式提供 plan/owner/due_days",
            )
        plan_value = action_value["plan"]
        owner_value = action_value["owner"]
        due_value = action_value["due_days"]
        if (
            plan_value is None
            or (isinstance(plan_value, str) and not plan_value.strip())
            or (
                isinstance(plan_value, Sequence)
                and not isinstance(plan_value, (str, bytes, bytearray))
                and (
                    not plan_value
                    or (
                        all(isinstance(item, str) for item in plan_value)
                        and not any(item.strip() for item in plan_value)
                    )
                )
            )
            or owner_value is None
            or (isinstance(owner_value, str) and not owner_value.strip())
            or due_value is None
            or (isinstance(due_value, str) and not due_value.strip())
        ):
            raise _contract_error(
                "IC_ACTION_REQUIRED",
                "整改计划的 plan/owner/due_days 均必须为非空明确值",
            )
        due_raw = action_value["due_days"]
        due_days = _bounded_float(
            due_raw,
            field="整改天数",
            minimum=0,
            maximum=90,
        )
        # “立即/0 天”生成的期限等于入库时刻，整改单一出生就逾期；
        # 最短给 1 天，让店里有时间处理和拍复查照片。
        due_days = max(1.0, due_days)
        if not evidence:
            # 证据全部落在待补拍照片上：先不记这个问题，补拍后再判断。
            continue
        issues.append({
            "title": _text(value.get("title"), field="问题标题", limit=120, required=True),
            "description": _text(value.get("description"), field="问题描述", limit=1500, required=True),
            "severity": severity,
            "category": category,
            "confidence": confidence,
            "needs_human_check": confidence < 0.65,
            "root_cause": _text(value.get("root_cause"), field="根因建议", limit=800),
            "evidence": evidence,
            # 不读取模型返回的 status/closed/verified；新问题始终 open。
            "action": {
                "plan": _text(
                    plan_value,
                    field="整改计划",
                    limit=1200,
                    required=True,
                ),
                "owner": _text(
                    owner_value,
                    field="整改负责人",
                    limit=60,
                    required=True,
                ),
                "due_days": due_days,
            },
        })
    issue_evidence_photo_ids = {
        int(evidence["photo_id"])
        for item in issues
        for evidence in item["evidence"]
    }
    issue_verdict_photo_ids = {
        photo_id
        for photo_id, review in review_by_photo_id.items()
        if review["verdict"] == "issue"
    }
    if issue_verdict_photo_ids != issue_evidence_photo_ids:
        raise InspectionError("逐图问题结论必须与同图问题证据一致")

    result = {
        "analysis_status": "issues_found" if issues else "clean_candidate",
        "summary": summary,
        "score": score,
        "photo_reviews": photo_reviews,
        "issues": issues,
        "retake_photo_ids": sorted(retake_photo_ids),
    }
    if issues:
        return {**result, "analysis_status": "issues_found"}
    if allow_clean_candidate:
        return result
    if raw.get("analysis_status") != "clean_verified":
        raise InspectionError("零问题巡店结果必须经异模复核")
    verification = raw.get("verification")
    if not isinstance(verification, Mapping):
        raise InspectionError("零问题巡店结果缺少异模复核证明")
    primary_model = _text(
        verification.get("primary_model"),
        field="主视觉模型",
        limit=80,
        required=True,
    )
    review_model = _text(
        verification.get("review_model"),
        field="复核视觉模型",
        limit=80,
        required=True,
    )
    if primary_model == review_model or verification.get("both_clean") is not True:
        raise InspectionError("零问题巡店结果未完成异模双重确认")
    return {
        **result,
        "analysis_status": "clean_verified",
        "verification": {
            "primary_model": primary_model,
            "review_model": review_model,
            "both_clean": True,
        },
    }


def _contract_validation_code(message: str) -> tuple[str, bool]:
    """把内部中文校验文案收敛为无业务数据的稳定观测码。"""
    rules = (
        ("巡店识别结果格式无效", "IC_RESULT_SHAPE", True),
        ("巡店识别结果没有可校验照片", "IC_CONTEXT_PHOTOS", False),
        ("必须显式提供问题列表", "IC_REQUIRED_ISSUES", True),
        ("逐图核查结果格式无效", "IC_REVIEW_SHAPE", True),
        ("逐图核查结果超过照片上限", "IC_REVIEW_SHAPE", True),
        ("张照片的核查结果无效", "IC_REVIEW_SHAPE", True),
        ("逐图核查引用了不属于本次巡店的照片", "IC_REVIEW_FOREIGN_ID", True),
        ("逐图核查结果包含重复照片", "IC_REVIEW_DUPLICATE", True),
        ("逐图核查必须明确照片是否可分析", "IC_REVIEW_REQUIRED", True),
        ("逐图核查结论只能为", "IC_REVIEW_VERDICT", True),
        ("逐图核查置信度格式无效", "IC_REVIEW_CONFIDENCE_REQUIRED", True),
        ("逐图核查必须提供非空可见事实", "IC_REVIEW_FACTS", True),
        ("逐图核查未精确覆盖全部初检照片", "IC_REVIEW_COVERAGE", True),
        ("问题列表格式无效", "IC_ISSUE_SHAPE", True),
        ("单次巡店最多记录", "IC_ISSUE_SHAPE", True),
        ("个问题格式无效", "IC_ISSUE_SHAPE", True),
        ("问题证据格式无效", "IC_EVIDENCE_SHAPE", True),
        ("每个图片巡店问题都必须绑定证据照片", "IC_EVIDENCE_REQUIRED", True),
        ("单个问题的证据照片过多", "IC_EVIDENCE_SHAPE", True),
        ("问题引用了不属于本次巡店的照片", "IC_EVIDENCE_FOREIGN_ID", True),
        ("每个巡店问题都必须有整改计划", "IC_ACTION_REQUIRED", True),
        ("逐图问题结论必须与同图问题证据一致", "IC_VERDICT_EVIDENCE", True),
        ("零问题巡店结果", "IC_CLEAN_VERIFICATION", False),
    )
    for marker, code, retryable in rules:
        if marker in message:
            return code, retryable
    if "必填" in message:
        return "IC_REQUIRED_FIELD", True
    if "格式无效" in message or "只能为" in message:
        return "IC_FIELD_FORMAT", True
    if "不能超过" in message or "必须在" in message:
        return "IC_FIELD_RANGE", True
    return "IC_CONTRACT_INVALID", True


def normalize_model_result(
    raw: Mapping[str, Any],
    allowed_photo_ids: set[int],
    *,
    allow_clean_candidate: bool = False,
) -> dict:
    """规范化无歧义表现差异后，仍以完整严格合同校验。"""
    try:
        return _normalize_model_result_strict(
            canonicalize_model_result(raw),
            allowed_photo_ids,
            allow_clean_candidate=allow_clean_candidate,
        )
    except InspectionContractError:
        raise
    except InspectionError as exc:
        code, retryable = _contract_validation_code(str(exc))
        raise _contract_error(
            code,
            str(exc),
            retryable=retryable,
        ) from exc


# 第 2 期：确定性评分。模型给的 0-100 分只作“AI 参考分”，不参与排行。
SCORE_RULE = "severity_v1"
SEVERITY_DEDUCTIONS = {"critical": 25, "high": 15, "medium": 8, "low": 3}
FALSE_POSITIVE = "false_positive"


def deterministic_score(issues: Sequence[Mapping[str, Any]]) -> float:
    """门店得分 = 100 − Σ 问题扣分（同类问题同次只扣最重的一次，误报不扣），下限 0。"""
    worst: dict[str, int] = {}
    for item in issues or []:
        if not isinstance(item, Mapping) or item.get("false_positive"):
            continue
        points = SEVERITY_DEDUCTIONS.get(str(item.get("severity") or "").lower(), 0)
        category = str(item.get("category") or "").strip().lower() or "other"
        worst[category] = max(worst.get(category, 0), points)
    return float(max(0, 100 - sum(worst.values())))


def _score_issue_rows(connection, tid: int, visit_id: int) -> list[dict]:
    return [{
        "severity": row["severity"],
        "category": row["category"],
        "false_positive": str(row["close_reason"] or "") == FALSE_POSITIVE,
    } for row in connection.execute(
        "SELECT i.severity,i.category,COALESCE(a.close_reason,'') close_reason "
        "FROM inspection_issue i LEFT JOIN inspection_action a "
        "ON a.issue_id=i.id AND a.tenant_id=i.tenant_id AND a.visit_id=i.visit_id "
        "WHERE i.tenant_id=? AND i.visit_id=?",
        (int(tid), int(visit_id)),
    ).fetchall()]


def _recompute_visit_score(connection, tid: int, visit_id: int) -> float | None:
    """按当前问题（排除误报）重算已完成巡店的得分；模型原分留作 AI 参考分。"""
    row = connection.execute(
        "SELECT status,score,model_json FROM inspection_visit "
        "WHERE id=? AND tenant_id=?",
        (int(visit_id), int(tid)),
    ).fetchone()
    if not row or row["status"] != "completed":
        return None
    model = db.jloads(row["model_json"], {})
    if not isinstance(model, dict):
        model = {}
    if "ai_reference_score" not in model:
        # 历史记录：model_json 里的 score 就是当时模型给的分。
        legacy = model.get("score", row["score"])
        model["ai_reference_score"] = (
            float(legacy)
            if isinstance(legacy, (int, float)) and not isinstance(legacy, bool)
            else None
        )
    model["score_rule"] = SCORE_RULE
    score = deterministic_score(_score_issue_rows(connection, tid, visit_id))
    connection.execute(
        "UPDATE inspection_visit SET score=?,model_json=? WHERE id=? AND tenant_id=?",
        (score, _json(model), int(visit_id), int(tid)),
    )
    return score


def backfill_scores(batch_size: int = 200) -> int:
    """把历史巡店按新规则重算一批；只处理还没有新口径标记的，重复执行无副作用。

    返回本批处理条数；为 0 表示全部完成。调用方按批循环，别一次锁住整库。
    """
    size = max(1, min(int(batch_size), 1000))
    rows = db.q(
        "SELECT id,tenant_id FROM inspection_visit WHERE status='completed' "
        "AND deleted_at IS NULL "
        "AND instr(COALESCE(model_json,''),'\"score_rule\"')=0 "
        "ORDER BY id LIMIT ?",
        (size,),
    )
    if not rows:
        return 0
    with db.atomic() as connection:
        for row in rows:
            _recompute_visit_score(connection, int(row["tenant_id"]), int(row["id"]))
    return len(rows)


def _retake_state(model_json: Any) -> dict | None:
    """读出“待补拍”的中间结果；没有就返回 None。"""
    model = db.jloads(model_json, {}) if isinstance(model_json, str) else model_json
    if not isinstance(model, Mapping):
        return None
    retake = model.get("retake")
    if model.get("analysis_status") != "needs_retake" or not isinstance(retake, Mapping):
        return None
    try:
        photo_ids = [int(value) for value in retake.get("photo_ids") or []]
        pending = [int(value) for value in retake.get("pending") or []]
        round_no = int(retake.get("round") or 1)
    except (TypeError, ValueError):
        raise InspectionConflict("巡店补拍记录损坏") from None
    if not photo_ids or not set(pending) <= set(photo_ids):
        raise InspectionConflict("巡店补拍记录损坏")
    state = copy.deepcopy(dict(model))
    state["retake"] = {"photo_ids": photo_ids, "pending": pending, "round": round_no}
    return state


def analysis_photo_ids(visit: Mapping[str, Any]) -> set[int]:
    """这一轮要交给模型看的初检照片：补拍轮只看补拍的那几张。"""
    before = {
        int(photo["id"])
        for photo in visit.get("photos") or []
        if photo.get("phase") == "before"
    }
    retake = visit.get("retake")
    if isinstance(retake, Mapping) and retake.get("photo_ids"):
        chosen = {int(value) for value in retake["photo_ids"]} & before
        if chosen:
            return chosen
    return before


def union_retake_flags(
    chosen: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict:
    """零问题双模复核：任一模型看不清的照片，在最终结果里都记为需补拍。"""
    result = copy.deepcopy(dict(chosen))
    flagged: dict[int, dict] = {}
    for candidate in candidates:
        for review in candidate.get("photo_reviews") or []:
            if isinstance(review, Mapping) and review.get("needs_retake"):
                flagged.setdefault(int(review["photo_id"]), dict(review))
    if not flagged:
        return result
    reviews = []
    for review in result.get("photo_reviews") or []:
        photo_id = int(review.get("photo_id") or 0)
        if photo_id in flagged and not review.get("needs_retake"):
            review = _retake_review(
                photo_id,
                flagged[photo_id],
                reason=str(flagged[photo_id].get("retake_reason") or "low_confidence"),
            )
        reviews.append(review)
    result["photo_reviews"] = reviews
    result["retake_photo_ids"] = sorted(
        {int(value) for value in result.get("retake_photo_ids") or []} | set(flagged)
    )
    return result


def _merge_retake_round(
    prior: Mapping[str, Any] | None,
    normalized: Mapping[str, Any],
) -> tuple[dict, list[int], int]:
    """把补拍轮的结果并回上一轮：没补拍的照片结论和已发现的问题全部保留。"""
    current = copy.deepcopy(dict(normalized))
    retake_ids = [int(value) for value in current.get("retake_photo_ids") or []]
    if prior is None:
        return current, retake_ids, 1
    retaken = {int(value) for value in prior["retake"]["photo_ids"]}
    reviews = [
        copy.deepcopy(review)
        for review in prior.get("photo_reviews") or []
        if int(review.get("photo_id") or 0) not in retaken
    ] + list(current.get("photo_reviews") or [])
    reviews.sort(key=lambda review: int(review.get("photo_id") or 0))
    issues = copy.deepcopy(list(prior.get("issues") or [])) + list(
        current.get("issues") or []
    )
    summary = "\n\n".join(
        piece for piece in (
            str(prior.get("summary") or "").strip(),
            "补拍复核：" + str(current.get("summary") or "").strip(),
        ) if piece
    )[:4000]
    scores = [
        float(value) for value in (prior.get("score"), current.get("score"))
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    merged: dict[str, Any] = {
        "analysis_status": "issues_found" if issues else "clean_verified",
        "summary": summary,
        "score": min(scores) if scores else None,
        "photo_reviews": reviews,
        "issues": issues,
        "retake_photo_ids": retake_ids,
    }
    verification = current.get("verification") or prior.get("verification")
    if not issues and verification:
        merged["verification"] = copy.deepcopy(verification)
    return merged, retake_ids, int(prior["retake"].get("round") or 1) + 1


def _single_branch_manager(connection, tid: int, branch_id: int, industry_key: str) -> dict | None:
    """门店只有一个店长时返回他，用来默认指派 AI 生成的整改。"""
    rows = connection.execute(
        "SELECT u.id,u.username,u.modules_json FROM user_branch ub JOIN users u "
        "ON u.id=ub.user_id AND u.tenant_id=ub.tenant_id WHERE ub.tenant_id=? "
        "AND ub.branch_id=? AND u.role='member' AND u.job_title='manager' "
        "AND COALESCE(u.enabled,1)=1 ORDER BY u.id LIMIT 2",
        (int(tid), int(branch_id)),
    ).fetchall()
    if len(rows) != 1:
        return None
    modules = db.jloads(rows[0]["modules_json"], []) or []
    if not isinstance(modules, list) or industry_key not in modules:
        return None
    return {"id": int(rows[0]["id"]), "username": str(rows[0]["username"] or "")}


def complete_visit(
    tid: int,
    uid: int,
    industry_key: str,
    visit_id: int,
    model_result: Mapping[str, Any],
) -> dict:
    _actor(tid, uid, industry_key)
    visit = db.one(
        "SELECT id,status,branch_id,version,template_key,template_version,"
        "template_snapshot_json,observations_json,model_json FROM inspection_visit "
        "WHERE id=? AND tenant_id=? AND industry_key=? AND deleted_at IS NULL",
        (int(visit_id), int(tid), industry_key),
    )
    if not visit:
        raise InspectionNotFound("巡店记录不存在")
    _branch_scope(tid, industry_key, int(visit["branch_id"]))
    if visit["status"] == "completed":
        return get_visit(tid, uid, industry_key, int(visit_id))
    if visit["status"] != "analyzing":
        raise InspectionConflict("巡店记录当前不可写入识别结果")
    contract = _stored_visit_contract(visit)
    photo_rows = db.q(
        "SELECT id,capture_slot,item_code FROM inspection_photo WHERE tenant_id=? "
        "AND visit_id=? AND phase='before' ORDER BY id",
        (int(tid), int(visit_id)),
    )
    _validate_persisted_photo_contract(photo_rows, contract)
    all_photo_ids = {int(item["id"]) for item in photo_rows}
    if not all_photo_ids:
        raise InspectionConflict("巡店记录没有可用的初检照片")
    prior = _retake_state(visit.get("model_json"))
    allowed_photo_ids = (
        {int(value) for value in prior["retake"]["photo_ids"]} & all_photo_ids
        if prior else all_photo_ids
    )
    if not allowed_photo_ids:
        raise InspectionConflict("巡店补拍记录与照片不一致")
    normalized = normalize_model_result(model_result, allowed_photo_ids)
    merged, retake_ids, round_no = _merge_retake_round(prior, normalized)
    now = time.time()
    with db.atomic() as connection:
        current = connection.execute(
            "SELECT status,version,template_key,template_version,"
            "template_snapshot_json,observations_json FROM inspection_visit "
            "WHERE id=? AND tenant_id=? "
            "AND industry_key=? AND deleted_at IS NULL",
            (int(visit_id), int(tid), industry_key),
        ).fetchone()
        if not current:
            raise InspectionNotFound("巡店记录不存在")
        if current["status"] == "completed":
            return get_visit(tid, uid, industry_key, int(visit_id))
        if current["status"] != "analyzing" or int(current["version"]) != int(visit["version"]):
            raise InspectionConflict("巡店结果已被其他请求更新")
        current_contract = _stored_visit_contract(dict(current))
        if current_contract != contract:
            raise InspectionConflict("巡店标准快照已被其他请求更新")
        existing_issue = connection.execute(
            "SELECT id FROM inspection_issue WHERE tenant_id=? AND visit_id=? LIMIT 1",
            (int(tid), int(visit_id)),
        ).fetchone()
        if existing_issue:
            raise InspectionConflict("巡店结果已生成，不能重复写入")
        if retake_ids and round_no <= MAX_RETAKE_ROUNDS:
            # 有照片看不清：其他照片的结论先存着，只让店员补拍这几张。
            state = {
                **merged,
                "analysis_status": "needs_retake",
                "retake": {
                    "photo_ids": list(retake_ids),
                    "pending": list(retake_ids),
                    "round": round_no,
                },
            }
            changed = connection.execute(
                "UPDATE inspection_visit SET status='needs_retake',summary_md=?,"
                "model_json=?,updated_at=?,version=version+1 "
                "WHERE id=? AND tenant_id=? AND industry_key=? AND status='analyzing' "
                "AND version=? AND deleted_at IS NULL",
                (
                    merged["summary"], _json(state), now, int(visit_id), int(tid),
                    industry_key, int(visit["version"]),
                ),
            )
            if changed.rowcount != 1:
                raise InspectionConflict("巡店结果已被其他请求更新")
            connection.execute(
                "INSERT INTO inspection_event(tenant_id,visit_id,kind,payload_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (
                    int(tid), int(visit_id), "retake_requested",
                    _json({"photo_ids": list(retake_ids), "round": round_no}),
                    int(uid), now,
                ),
            )
            return get_visit(tid, uid, industry_key, int(visit_id))
        if retake_ids:
            # 补拍次数用完仍看不清：标成“需人工查看”，照样出报告。
            unresolved = set(retake_ids)
            for review in merged["photo_reviews"]:
                if int(review.get("photo_id") or 0) in unresolved:
                    review["needs_retake"] = False
                    review["verdict"] = "unresolved"
                    review["retake_reason"] = "unresolved"
            merged["unresolved_photo_ids"] = sorted(unresolved)
        merged["retake_photo_ids"] = []
        branch_id = int(visit["branch_id"])
        default_assignee = (
            _single_branch_manager(connection, tid, branch_id, industry_key)
            if merged["issues"] else None
        )
        assigned_actions: list[dict] = []
        for item in merged["issues"]:
            due_at = now + item["action"]["due_days"] * 86400
            owner_text = (
                default_assignee["username"] if default_assignee
                else item["action"]["owner"]
            )
            issue_cursor = connection.execute(
                "INSERT INTO inspection_issue(tenant_id,visit_id,title,description,"
                "severity,category,status,owner,due_at,confidence,needs_human_check,"
                "root_cause,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'detected',?,?,?,?,?,?,?)",
                (
                    int(tid), int(visit_id), item["title"], item["description"],
                    item["severity"], item["category"], owner_text,
                    due_at, item["confidence"], 1 if item["needs_human_check"] else 0,
                    item["root_cause"], now, now,
                ),
            )
            issue_id = int(issue_cursor.lastrowid)
            action_cursor = connection.execute(
                "INSERT INTO inspection_action(tenant_id,visit_id,issue_id,status,"
                "plan,owner,due_at,version,assignee_user_id,created_at,updated_at) "
                "VALUES(?,?,?,'open',?,?,?,1,?,?,?)",
                (
                    int(tid), int(visit_id), issue_id, item["action"]["plan"],
                    owner_text, due_at,
                    default_assignee["id"] if default_assignee else None,
                    now, now,
                ),
            )
            action_id = int(action_cursor.lastrowid)
            if default_assignee:
                assigned_actions.append({
                    "action_id": action_id, "title": item["title"], "due_at": due_at,
                })
            for evidence in item["evidence"]:
                connection.execute(
                    "INSERT INTO inspection_evidence(tenant_id,visit_id,issue_id,"
                    "photo_id,note,bbox_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        int(tid), int(visit_id), issue_id, evidence["photo_id"],
                        evidence["note"],
                        _json(evidence["bbox"]) if evidence["bbox"] is not None else None,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO inspection_event(tenant_id,visit_id,issue_id,action_id,"
                "kind,payload_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    int(tid), int(visit_id), issue_id, action_id, "issue_created",
                    _json({
                        "severity": item["severity"],
                        "needs_human_check": item["needs_human_check"],
                        "assignee_user_id": (
                            default_assignee["id"] if default_assignee else None
                        ),
                    }),
                    int(uid), now,
                ),
            )
        ai_score = merged.get("score")
        merged["ai_reference_score"] = ai_score
        merged["score_rule"] = SCORE_RULE
        score = deterministic_score(merged["issues"])
        changed = connection.execute(
            "UPDATE inspection_visit SET status='completed',score=?,summary_md=?,"
            "model_json=?,completed_at=?,terminal_at=?,updated_at=?,version=version+1 "
            "WHERE id=? AND tenant_id=? AND industry_key=? AND status='analyzing' "
            "AND version=? AND deleted_at IS NULL",
            (
                score, merged["summary"], _json(merged),
                now, now, now, int(visit_id), int(tid), industry_key,
                int(visit["version"]),
            ),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("巡店结果已被其他请求更新")
        connection.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,kind,payload_json,"
            "created_by,created_at) VALUES(?,?,?,?,?,?)",
            (
                int(tid), int(visit_id), "analysis_completed",
                _json({
                    "analysis_status": merged["analysis_status"],
                    "score": score,
                    "ai_reference_score": ai_score,
                    "issue_count": len(merged["issues"]),
                }),
                int(uid), now,
            ),
        )
        if default_assignee and assigned_actions:
            # 与巡店结果同一事务写站内通知：结果回滚时不会留下“幽灵”提醒。
            branch_name = str((connection.execute(
                "SELECT name FROM store_branch WHERE id=? AND tenant_id=?",
                (branch_id, int(tid)),
            ).fetchone() or {"name": ""})["name"] or "")
            _record_assignment_notice(
                tid,
                default_assignee["id"],
                visit_id=int(visit_id),
                industry_key=industry_key,
                branch_name=branch_name,
                actions=assigned_actions,
                webhook=False,
            )
    return get_visit(tid, uid, industry_key, int(visit_id))


def retake_target(
    tid: int,
    uid: int,
    industry_key: str,
    visit_id: int,
    photo_id: int,
) -> dict:
    """补拍前的校验：巡店正在等补拍，且这张照片确实在待补拍名单里。"""
    user = _actor(tid, uid, industry_key)
    visit = db.one(
        "SELECT id,status,branch_id,model_json FROM inspection_visit WHERE id=? "
        "AND tenant_id=? AND industry_key=? AND deleted_at IS NULL",
        (_positive_id(visit_id, "巡店记录"), int(tid), industry_key),
    )
    if not visit:
        raise InspectionNotFound("巡店记录不存在")
    _branch_scope(tid, industry_key, int(visit["branch_id"]), actor=user)
    if visit["status"] != "needs_retake":
        raise InspectionConflict("这次巡店现在不需要补拍")
    state = _retake_state(visit.get("model_json"))
    target_id = _positive_id(photo_id, "补拍照片")
    if state is None or target_id not in state["retake"]["pending"]:
        raise InspectionConflict("这张照片已经补拍过了，或不需要补拍")
    photo = db.one(
        "SELECT id,storage_key,sha256,capture_slot,item_code FROM inspection_photo "
        "WHERE id=? AND tenant_id=? AND visit_id=? AND phase='before'",
        (target_id, int(tid), int(visit["id"])),
    )
    if not photo:
        raise InspectionNotFound("补拍照片不存在")
    return {
        "visit_id": int(visit["id"]),
        "photo_id": target_id,
        "capture_slot": photo.get("capture_slot") or None,
        "item_code": photo.get("item_code") or None,
        "old_storage_key": str(photo.get("storage_key") or ""),
    }


def replace_retake_photo(
    tid: int,
    uid: int,
    industry_key: str,
    visit_id: int,
    photo_id: int,
    record: Mapping[str, Any],
) -> dict:
    """店员补拍一张：原地替换这张照片（编号不变，其他照片的结论保留）。

    待补拍的照片全部补齐后，巡店回到“分析中”，原巡店任务在同一事务里免费
    重新排队；返回 ``ready``/``task_id``，调用方据此启动分析。
    """
    target = retake_target(tid, uid, industry_key, visit_id, photo_id)
    raw = dict(record) if isinstance(record, Mapping) else {}
    # 采集位和检查项以原照片为准，不信上传方。
    raw["capture_slot"] = target["capture_slot"]
    raw["item_code"] = target["item_code"]
    now = time.time()
    with db.atomic() as connection:
        visit = connection.execute(
            "SELECT status,version,model_json,template_key,template_version,"
            "template_snapshot_json,observations_json FROM inspection_visit "
            "WHERE id=? AND tenant_id=? AND industry_key=? AND deleted_at IS NULL",
            (target["visit_id"], int(tid), industry_key),
        ).fetchone()
        if not visit or visit["status"] != "needs_retake":
            raise InspectionConflict("这次巡店现在不需要补拍")
        state = _retake_state(visit["model_json"])
        if state is None or target["photo_id"] not in state["retake"]["pending"]:
            raise InspectionConflict("这张照片已经补拍过了，或不需要补拍")
        contract = _stored_visit_contract(dict(visit))
        photo = _normalize_photo(
            raw,
            tid,
            phase="before",
            standard_snapshot=(contract or {}).get("template_snapshot"),
        )
        changed = connection.execute(
            "UPDATE inspection_photo SET storage_key=?,mime_type=?,byte_size=?,"
            "sha256=?,width=?,height=?,created_by=?,created_at=? "
            "WHERE id=? AND tenant_id=? AND visit_id=? AND phase='before'",
            (
                photo["storage_key"], photo["mime_type"], photo["byte_size"],
                photo["sha256"], photo["width"], photo["height"], int(uid), now,
                target["photo_id"], int(tid), target["visit_id"],
            ),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("补拍照片已被其他请求更新")
        pending = [
            value for value in state["retake"]["pending"]
            if value != target["photo_id"]
        ]
        state["retake"]["pending"] = pending
        ready = not pending
        changed = connection.execute(
            "UPDATE inspection_visit SET status=?,model_json=?,updated_at=?,"
            "version=version+1 WHERE id=? AND tenant_id=? AND status='needs_retake' "
            "AND version=?",
            (
                "analyzing" if ready else "needs_retake", _json(state), now,
                target["visit_id"], int(tid), int(visit["version"]),
            ),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("巡店记录已被其他请求更新，请刷新")
        task_id = None
        if ready:
            # 补齐后原巡店任务免费重新排队（首轮已扣过点，这轮不再扣）。
            row = connection.execute(
                "SELECT task_id FROM inspection_visit WHERE id=? AND tenant_id=?",
                (target["visit_id"], int(tid)),
            ).fetchone()
            task_id = int(row["task_id"]) if row and row["task_id"] else None
            if task_id is not None:
                requeued = connection.execute(
                    "UPDATE task SET status='queued',billing_status='included',"
                    "output_md=NULL,summary_md=NULL,terminal_at=NULL,updated_at=? "
                    "WHERE id=? AND tenant_id=? AND emp_idx=? AND status='done' "
                    "AND deleted_at IS NULL",
                    (now, task_id, int(tid), EMPLOYEE_IDX),
                )
                if requeued.rowcount != 1:
                    raise InspectionConflict("巡店任务状态已变化，请刷新后再补拍")
        connection.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,kind,payload_json,"
            "created_by,created_at) VALUES(?,?,?,?,?,?)",
            (
                int(tid), target["visit_id"], "photo_retaken",
                _json({
                    "photo_id": target["photo_id"],
                    "round": state["retake"]["round"],
                    "remaining": len(pending),
                }),
                int(uid), now,
            ),
        )
    return {
        "visit_id": target["visit_id"],
        "photo_id": target["photo_id"],
        "ready": ready,
        "remaining": len(pending),
        "task_id": task_id,
        "old_storage_key": target["old_storage_key"],
    }


def _scoped_action(
    tid: int,
    industry_key: str,
    action_id: int,
    *,
    actor: Mapping[str, Any] | None = None,
) -> dict:
    row = db.one(
        "SELECT a.*,v.industry_key,v.branch_id,v.deleted_at AS visit_deleted_at "
        "FROM inspection_action a JOIN inspection_visit v ON v.id=a.visit_id "
        "WHERE a.id=? AND a.tenant_id=? AND v.tenant_id=? AND v.industry_key=? "
        "AND v.deleted_at IS NULL",
        (int(action_id), int(tid), int(tid), industry_key),
    )
    if not row:
        raise InspectionNotFound("整改任务不存在")
    _branch_scope(tid, industry_key, int(row["branch_id"]), actor=actor)
    return row


def _action_public(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "visit_id": int(row["visit_id"]),
        "issue_id": int(row["issue_id"]),
        "status": row["status"],
        "plan": row.get("plan") or "",
        "owner": row.get("owner") or "",
        "due_at": row.get("due_at"),
        "version": int(row.get("version") or 0),
        "closed_by": row.get("closed_by"),
        "closed_at": row.get("closed_at"),
        "assignee_user_id": (
            int(row["assignee_user_id"])
            if row.get("assignee_user_id") is not None else None
        ),
        "close_reason": str(row.get("close_reason") or ""),
        "dismiss_note": (
            str(row.get("completion_note") or "")
            if str(row.get("close_reason") or "") == FALSE_POSITIVE else ""
        ),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def update_action_assignment(
    tid: int,
    uid: int,
    industry_key: str,
    action_id: int,
    *,
    expected_version: int,
    owner: str,
    due_at: float,
    plan: str | None = None,
) -> dict:
    """由企业主/root 确认实际整改负责人和期限。

    模型产生的 owner / due_at 只是建议。人工确认和后续修改都用
    action.version 做 CAS，并同步 issue 的便捷查询字段。
    """
    user = _actor(tid, uid, industry_key, manager=True)
    action = _scoped_action(tid, industry_key, action_id, actor=user)
    if str(action.get("status") or "") == "closed":
        raise InspectionConflict("已闭环整改任务不能修改责任信息")
    if isinstance(expected_version, bool):
        raise InspectionError("整改版本格式无效")
    try:
        expected = int(expected_version)
        expected_number = float(expected_version)
    except (TypeError, ValueError, OverflowError):
        raise InspectionError("整改版本格式无效") from None
    if not math.isfinite(expected_number) or expected < 0 or expected_number != expected:
        raise InspectionError("整改版本格式无效")
    clean_owner = _text(owner, field="整改负责人", limit=60, required=True)
    clean_due_at = _bounded_float(
        due_at,
        field="整改期限",
        minimum=946684800,
        maximum=4_102_444_800,
    )
    clean_plan = (
        str(action.get("plan") or "")
        if plan is None
        else _text(plan, field="整改计划", limit=1200, required=True)
    )
    now = time.time()
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE inspection_action SET owner=?,due_at=?,plan=?,"
            "version=version+1,updated_at=? WHERE id=? AND tenant_id=? "
            "AND visit_id=? AND issue_id=? AND status!='closed' AND version=?",
            (
                clean_owner, clean_due_at, clean_plan, now, int(action_id),
                int(tid), int(action["visit_id"]), int(action["issue_id"]),
                expected,
            ),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("整改记录已更新，请刷新后再修改")
        issue_changed = connection.execute(
            "UPDATE inspection_issue SET owner=?,due_at=?,updated_at=? "
            "WHERE id=? AND tenant_id=? AND visit_id=? AND status!='closed'",
            (
                clean_owner, clean_due_at, now, int(action["issue_id"]),
                int(tid), int(action["visit_id"]),
            ),
        )
        if issue_changed.rowcount != 1:
            raise InspectionConflict("巡店问题已变更，请刷新后再修改")
        connection.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,issue_id,action_id,"
            "kind,payload_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(tid), int(action["visit_id"]), int(action["issue_id"]),
                int(action_id), "action_assignment_updated",
                _json({
                    "from": {
                        "owner": str(action.get("owner") or ""),
                        "due_at": action.get("due_at"),
                    },
                    "to": {"owner": clean_owner, "due_at": clean_due_at},
                    "plan_changed": clean_plan != str(action.get("plan") or ""),
                }),
                int(uid), now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM inspection_action WHERE id=? AND tenant_id=?",
            (int(action_id), int(tid)),
        ).fetchone()
    return _action_public(dict(row))


_ACTION_TRANSITIONS = {
    "open": {"in_progress", "awaiting_recheck"},
    "in_progress": {"awaiting_recheck"},
    "reopened": {"in_progress", "awaiting_recheck"},
}


def transition_action(
    tid: int,
    uid: int,
    industry_key: str,
    action_id: int,
    *,
    expected_version: int,
    target_status: str,
    note: str = "",
) -> dict:
    """整改负责人推进状态，但无权自己将问题标记为已验证。

    经理/员工只能推进分配给自己的门店的整改单。
    """
    user = _actor(tid, uid, industry_key)
    action = _scoped_action(tid, industry_key, action_id, actor=user)
    target = _text(target_status, field="整改状态", limit=40, required=True)
    if target not in _ACTION_TRANSITIONS.get(str(action["status"]), set()):
        raise InspectionConflict("整改任务当前不能转入该状态")
    expected = _positive_id(expected_version + 1, "整改版本") - 1
    clean_note = _text(note, field="整改进展", limit=1000)
    now = time.time()
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE inspection_action SET status=?,version=version+1,updated_at=? "
            "WHERE id=? AND tenant_id=? AND visit_id=? AND status=? AND version=?",
            (
                target, now, int(action_id), int(tid), int(action["visit_id"]),
                action["status"], expected,
            ),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("整改记录已更新，请刷新后再操作")
        issue_target = (
            "awaiting_recheck" if target == "awaiting_recheck" else "rectifying"
        )
        connection.execute(
            "UPDATE inspection_issue SET status=?,updated_at=? WHERE id=? "
            "AND tenant_id=? AND visit_id=? AND status!='closed'",
            (
                issue_target, now, int(action["issue_id"]), int(tid),
                int(action["visit_id"]),
            ),
        )
        connection.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,issue_id,action_id,"
            "kind,payload_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(tid), int(action["visit_id"]), int(action["issue_id"]),
                int(action_id), "action_transition",
                _json({"from": action["status"], "to": target, "note": clean_note}),
                int(uid), now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM inspection_action WHERE id=? AND tenant_id=?",
            (int(action_id), int(tid)),
        ).fetchone()
    return _action_public(dict(row))


# ---------------- 第 2 期：整改派到人 / 误报作废 ----------------
ASSIGNED_NOTICE_KIND = "inspection_action_assigned"
_TITLE_LABELS = {"director": "总监", "manager": "店长", "staff": "店员"}
_RESTORABLE_ACTION_STATUSES = {
    "open": "detected",
    "in_progress": "rectifying",
    "awaiting_recheck": "awaiting_recheck",
    "reopened": "reopened",
}


def _record_assignment_notice(
    tid: int,
    user_id: int,
    *,
    visit_id: int,
    industry_key: str,
    branch_name: str,
    actions: Sequence[Mapping[str, Any]],
    webhook: bool = True,
) -> None:
    """按人通知被指派的整改；失败只记日志，不影响派活本身。"""
    from . import notify, timeutil

    if not actions:
        return
    first = actions[0]
    count = len(actions)
    title = str(first.get("title") or "门店整改")[:60]
    due_at = first.get("due_at")
    due_text = (
        f"，{timeutil.format_cn(float(due_at), '%m月%d日')}前完成"
        if isinstance(due_at, (int, float)) and due_at else ""
    )
    payload = {
        "user_id": int(user_id),
        "headline": "门店整改派给了你",
        "title": title,
        "summary": (
            f"{branch_name or '门店'}：{title}"
            + (f" 等 {count} 条" if count > 1 else "")
            + due_text
        ),
        "visit_id": int(visit_id),
        "action_id": int(first["action_id"]),
        "action_ids": [int(item["action_id"]) for item in actions],
        "branch_name": branch_name or "",
        "due_at": due_at,
        "link": f"#/inspections/{int(visit_id)}/{industry_key}",
    }
    if webhook:
        notify.push(int(tid), ASSIGNED_NOTICE_KIND, payload)
    else:
        notify.record(int(tid), ASSIGNED_NOTICE_KIND, payload)


def _action_context(tid: int, action_id: int) -> dict:
    """整改单的服务端权威上下文（行业、门店、问题标题都从库里反查）。"""
    row = db.one(
        "SELECT a.*,v.industry_key,v.branch_id,b.name AS branch_name,"
        "i.title AS issue_title,i.severity AS issue_severity "
        "FROM inspection_action a JOIN inspection_visit v ON v.id=a.visit_id "
        "AND v.tenant_id=a.tenant_id JOIN store_branch b ON b.id=v.branch_id "
        "AND b.tenant_id=v.tenant_id JOIN inspection_issue i ON i.id=a.issue_id "
        "AND i.tenant_id=a.tenant_id WHERE a.id=? AND a.tenant_id=? "
        "AND v.deleted_at IS NULL",
        (_positive_id(action_id, "整改任务"), int(tid)),
    )
    if not row:
        raise InspectionNotFound("整改任务不存在")
    return row


def _can_assign_branch(user: Mapping[str, Any], tid: int, branch_id: int) -> bool:
    """老板/总监可指派任何门店；店长只能指派自己负责的门店。"""
    if sees_all_branches(user):
        return True
    if str(user.get("role") or "") != "member" or _member_title(user) != "manager":
        return False
    return bool(db.one(
        "SELECT 1 AS ok FROM user_branch WHERE tenant_id=? AND user_id=? "
        "AND branch_id=?",
        (int(tid), int(user["id"]), int(branch_id)),
    ))


def assignable_users(tid: int, branch_id: int, industry_key: str) -> list[dict]:
    """可以接这家门店整改的人：该门店绑定的成员，外加老板和总监。"""
    rows = db.q(
        "SELECT id,username,role,job_title,modules_json FROM users "
        "WHERE tenant_id=? AND COALESCE(enabled,1)=1 AND ("
        "role IN ('owner','root') OR (role='member' AND (job_title='director' "
        "OR id IN (SELECT ub.user_id FROM user_branch ub WHERE ub.tenant_id=? "
        "AND ub.branch_id=?)))) ORDER BY CASE role WHEN 'member' THEN 1 ELSE 0 END,"
        "CASE job_title WHEN 'manager' THEN 0 WHEN 'staff' THEN 1 ELSE 2 END,id "
        "LIMIT 200",
        (int(tid), int(tid), int(branch_id)),
    )
    result: list[dict] = []
    for row in rows:
        role = str(row.get("role") or "")
        if role == "member":
            modules = db.jloads(row.get("modules_json"), []) or []
            if not isinstance(modules, list) or industry_key not in modules:
                continue
            title = _member_title(row)
            label = _TITLE_LABELS.get(title, "店员")
        else:
            title = ""
            label = "老板"
        result.append({
            "id": int(row["id"]),
            "username": str(row.get("username") or ""),
            "role": role,
            "job_title": title,
            "label": f"{row.get('username') or ''}（{label}）",
        })
    return result


def assign_action(
    tid: int,
    actor_uid: int,
    action_id: int,
    assignee_user_id: int,
    due_at: float | None = None,
) -> dict:
    """把整改指派给具体账号，并按人通知他。

    只有老板/总监/该门店店长可指派；被指派人必须绑定该门店，或是老板/总监。
    owner 文本同步成账号名，兼容只认 owner 文字的旧页面。
    """
    context = _action_context(tid, action_id)
    industry_key = str(context["industry_key"])
    branch_id = int(context["branch_id"])
    user = _actor(tid, actor_uid, industry_key)
    _branch_scope(tid, industry_key, branch_id, actor=user)
    if not _can_assign_branch(user, tid, branch_id):
        raise InspectionForbidden("只有老板、总监或这家门店的店长可以指派整改")
    if str(context.get("status") or "") == "closed":
        raise InspectionConflict("已关闭的整改不能再指派")
    target_id = _positive_id(assignee_user_id, "被指派人")
    target = next(
        (
            item for item in assignable_users(tid, branch_id, industry_key)
            if item["id"] == target_id
        ),
        None,
    )
    if target is None:
        raise InspectionError("只能指派给这家门店的店长、店员，或老板、总监")
    clean_due_at = None
    if due_at not in (None, ""):
        clean_due_at = _bounded_float(
            due_at, field="整改期限", minimum=946684800, maximum=4_102_444_800,
        )
    now = time.time()
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE inspection_action SET assignee_user_id=?,owner=?,"
            "due_at=COALESCE(?,due_at),version=version+1,updated_at=? "
            "WHERE id=? AND tenant_id=? AND status!='closed' AND version=?",
            (
                target_id, target["username"], clean_due_at, now,
                int(context["id"]), int(tid), int(context["version"]),
            ),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("整改记录已更新，请刷新后再指派")
        connection.execute(
            "UPDATE inspection_issue SET owner=?,due_at=COALESCE(?,due_at),"
            "updated_at=? WHERE id=? AND tenant_id=? AND status!='closed'",
            (
                target["username"], clean_due_at, now, int(context["issue_id"]),
                int(tid),
            ),
        )
        connection.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,issue_id,action_id,"
            "kind,payload_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(tid), int(context["visit_id"]), int(context["issue_id"]),
                int(context["id"]), "action_assigned",
                _json({
                    "from_user_id": context.get("assignee_user_id"),
                    "to_user_id": target_id,
                    "due_at": clean_due_at,
                }),
                int(actor_uid), now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM inspection_action WHERE id=? AND tenant_id=?",
            (int(context["id"]), int(tid)),
        ).fetchone()
    public = _action_public(dict(row))
    public["assignee"] = target
    if target_id != int(actor_uid):
        try:
            _record_assignment_notice(
                tid,
                target_id,
                visit_id=int(context["visit_id"]),
                industry_key=industry_key,
                branch_name=str(context.get("branch_name") or ""),
                actions=[{
                    "action_id": int(context["id"]),
                    "title": context.get("issue_title"),
                    "due_at": public.get("due_at"),
                }],
            )
        except Exception:
            # 通知失败不回滚指派；站内待办仍能看到这条整改。
            pass
    return public


def _action_next_step(status: str, pending_rechecks: int) -> dict:
    if status in {"open", "reopened"}:
        return {
            "next_step": "start",
            "button": "开始整改",
            "hint": "按整改要求处理好，再拍一张整改后的照片",
        }
    if status == "awaiting_recheck" and pending_rechecks:
        return {
            "next_step": "wait_review",
            "button": "",
            "hint": "整改照片已交，等老板确认",
        }
    return {
        "next_step": "upload_recheck",
        "button": "拍整改后照片",
        "hint": "对着原来的位置拍一张，交给老板确认",
    }


def actions_for_assignee(tid: int, uid: int) -> list[dict]:
    """店员“我的待办”里的巡店整改：指派给我、还没关闭的。

    经理/员工仍只看自己绑定门店的；停用门店、已删除巡店不出现。
    """
    user = db.one(
        "SELECT id,tenant_id,role,job_title,enabled,modules_json FROM users WHERE id=?",
        (int(uid),),
    )
    if (
        not user
        or not int(user.get("enabled") or 0)
        or int(user.get("tenant_id") or 0) != int(tid)
        or str(user.get("role") or "") not in {"root", "owner", "member"}
    ):
        return []
    scope_sql, scope_params = branch_scope_sql(user, tid, "v.branch_id")
    rows = db.q(
        "SELECT a.id,a.visit_id,a.issue_id,a.status,a.plan,a.due_at,a.version,"
        "a.updated_at,v.industry_key,v.branch_id,b.name AS branch_name,"
        "i.title AS issue_title,i.severity,"
        "(SELECT COUNT(*) FROM inspection_recheck r WHERE r.tenant_id=a.tenant_id "
        " AND r.action_id=a.id AND r.status='pending') pending_rechecks "
        "FROM inspection_action a JOIN inspection_visit v ON v.id=a.visit_id "
        "AND v.tenant_id=a.tenant_id JOIN tenant_industry ti "
        "ON ti.tenant_id=v.tenant_id AND ti.industry_key=v.industry_key "
        "JOIN store_branch b ON b.id=v.branch_id "
        "AND b.tenant_id=v.tenant_id AND b.active=1 JOIN inspection_issue i "
        "ON i.id=a.issue_id AND i.tenant_id=a.tenant_id "
        "WHERE a.tenant_id=? AND a.assignee_user_id=? AND a.status!='closed' "
        "AND v.deleted_at IS NULL"
        + scope_sql
        + " ORDER BY a.due_at IS NULL,a.due_at,a.id LIMIT 200",
        (int(tid), int(user["id"]), *scope_params),
    )
    modules = None
    if str(user.get("role") or "") == "member":
        raw_modules = db.jloads(user.get("modules_json"), []) or []
        modules = set(raw_modules) if isinstance(raw_modules, list) else set()
    now = time.time()
    result: list[dict] = []
    for row in rows:
        if modules is not None and str(row["industry_key"]) not in modules:
            continue
        status = str(row["status"])
        due_at = row.get("due_at")
        step = _action_next_step(status, int(row.get("pending_rechecks") or 0))
        result.append({
            "kind": "inspection_action",
            "id": int(row["id"]),
            "visit_id": int(row["visit_id"]),
            "issue_id": int(row["issue_id"]),
            "branch_id": int(row["branch_id"]),
            "branch_name": str(row.get("branch_name") or ""),
            "industry_key": str(row["industry_key"]),
            "title": str(row.get("issue_title") or ""),
            "severity": str(row.get("severity") or ""),
            "plan": str(row.get("plan") or ""),
            "due_at": due_at,
            "overdue": bool(
                due_at is not None
                and float(due_at) < now
                and status not in {"awaiting_recheck"}
            ),
            "status": status,
            "version": int(row.get("version") or 0),
            **step,
            # 前端直接用：开始整改走 PATCH，交照片走已有复查上传接口。
            "start_endpoint": (
                f"/api/inspections/{int(row['visit_id'])}/issues/{int(row['issue_id'])}"
            ),
            "recheck_endpoint": "/api/inspections/rechecks",
            "link": f"#/inspections/{int(row['visit_id'])}/{row['industry_key']}",
        })
    return result


def dismiss_action(
    tid: int,
    actor_uid: int,
    action_id: int,
    reason: str,
) -> dict:
    """老板/总监把 AI 误报的问题作废：不算问题、不算逾期、不扣门店分。"""
    context = _action_context(tid, action_id)
    industry_key = str(context["industry_key"])
    user = _actor(tid, actor_uid, industry_key, branch_admin=True)
    _assert_branch_visible(user, tid, int(context["branch_id"]))
    clean_reason = _text(reason, field="误报原因", limit=500, required=True)
    status = str(context.get("status") or "")
    if status == "closed":
        raise InspectionConflict("这条整改已经关闭，不能再标记误报")
    now = time.time()
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE inspection_action SET status='closed',close_reason=?,"
            "completion_note=?,closed_by=?,closed_at=?,version=version+1,"
            "updated_at=? WHERE id=? AND tenant_id=? AND status=? AND version=?",
            (
                FALSE_POSITIVE, clean_reason, int(actor_uid), now, now,
                int(context["id"]), int(tid), status, int(context["version"]),
            ),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("整改记录已更新，请刷新后再操作")
        connection.execute(
            "UPDATE inspection_issue SET status='closed',closure_evidence=?,"
            "updated_at=? WHERE id=? AND tenant_id=?",
            (f"误报：{clean_reason}", now, int(context["issue_id"]), int(tid)),
        )
        connection.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,issue_id,action_id,"
            "kind,payload_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(tid), int(context["visit_id"]), int(context["issue_id"]),
                int(context["id"]), "action_dismissed",
                _json({"from_status": status, "reason": clean_reason}),
                int(actor_uid), now,
            ),
        )
        score = _recompute_visit_score(connection, tid, int(context["visit_id"]))
        row = connection.execute(
            "SELECT * FROM inspection_action WHERE id=? AND tenant_id=?",
            (int(context["id"]), int(tid)),
        ).fetchone()
    public = _action_public(dict(row))
    public["visit_score"] = score
    return public


def restore_dismissed_action(
    tid: int,
    actor_uid: int,
    action_id: int,
    note: str = "",
) -> dict:
    """撤销误报：整改回到作废前的状态，重新计入问题数和门店扣分。"""
    context = _action_context(tid, action_id)
    industry_key = str(context["industry_key"])
    user = _actor(tid, actor_uid, industry_key, branch_admin=True)
    _assert_branch_visible(user, tid, int(context["branch_id"]))
    clean_note = _text(note, field="撤销说明", limit=500)
    if (
        str(context.get("status") or "") != "closed"
        or str(context.get("close_reason") or "") != FALSE_POSITIVE
    ):
        raise InspectionConflict("这条整改没有被标记为误报")
    last = db.one(
        "SELECT payload_json FROM inspection_event WHERE tenant_id=? AND action_id=? "
        "AND kind='action_dismissed' ORDER BY id DESC LIMIT 1",
        (int(tid), int(context["id"])),
    )
    previous = str((db.jloads((last or {}).get("payload_json"), {}) or {}).get(
        "from_status"
    ) or "open")
    if previous not in _RESTORABLE_ACTION_STATUSES:
        previous = "open"
    now = time.time()
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE inspection_action SET status=?,close_reason='',completion_note='',"
            "closed_by=NULL,closed_at=NULL,version=version+1,updated_at=? "
            "WHERE id=? AND tenant_id=? AND status='closed' AND close_reason=? "
            "AND version=?",
            (
                previous, now, int(context["id"]), int(tid), FALSE_POSITIVE,
                int(context["version"]),
            ),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("整改记录已更新，请刷新后再操作")
        connection.execute(
            "UPDATE inspection_issue SET status=?,closure_evidence='',updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (
                _RESTORABLE_ACTION_STATUSES[previous], now,
                int(context["issue_id"]), int(tid),
            ),
        )
        connection.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,issue_id,action_id,"
            "kind,payload_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(tid), int(context["visit_id"]), int(context["issue_id"]),
                int(context["id"]), "action_dismiss_reverted",
                _json({"to_status": previous, "note": clean_note}),
                int(actor_uid), now,
            ),
        )
        score = _recompute_visit_score(connection, tid, int(context["visit_id"]))
        row = connection.execute(
            "SELECT * FROM inspection_action WHERE id=? AND tenant_id=?",
            (int(context["id"]), int(tid)),
        ).fetchone()
    public = _action_public(dict(row))
    public["visit_score"] = score
    return public


def normalize_recheck_result(raw: Mapping[str, Any], allowed_photo_ids: set[int]) -> dict:
    if not isinstance(raw, Mapping):
        raise InspectionError("复查结果格式无效")
    recommendation = _text(
        raw.get("recommendation"),
        field="复查建议",
        limit=30,
        required=True,
    ).lower()
    if recommendation not in RECHECK_RECOMMENDATIONS:
        raise InspectionError("复查建议只能为 close/reject/manual_review")
    confidence = _bounded_float(
        raw.get("confidence", 0),
        field="复查置信度",
        minimum=0,
        maximum=1,
    )
    if recommendation == "close" and confidence < 0.8:
        recommendation = "manual_review"
    ids_value = raw.get("evidence_photo_ids", [])
    if isinstance(ids_value, (str, bytes, bytearray)) or not isinstance(ids_value, Sequence):
        raise InspectionError("复查证据格式无效")
    ids: list[int] = []
    for value in ids_value:
        photo_id = _positive_id(value, "复查证据照片")
        if photo_id not in allowed_photo_ids:
            raise InspectionError("复查引用了不属于本次巡店的照片")
        if photo_id not in ids:
            ids.append(photo_id)
    if not ids:
        raise InspectionError("复查必须提供证据照片")
    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "note": _text(raw.get("note"), field="复查说明", limit=1200, required=True),
        "evidence_photo_ids": ids,
    }


def _recheck_public(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "visit_id": int(row["visit_id"]),
        "issue_id": int(row["issue_id"]),
        "action_id": int(row["action_id"]),
        "task_id": row.get("task_id"),
        "status": row["status"],
        "note": row.get("note") or "",
        "model_recommendation": row.get("model_recommendation") or "manual_review",
        "created_by": int(row["created_by"]),
        "reviewed_by": row.get("reviewed_by"),
        "reviewed_at": row.get("reviewed_at"),
        "created_at": row.get("created_at"),
    }


def add_recheck_photos(
    tid: int,
    uid: int,
    industry_key: str,
    action_id: int,
    photo_records: Sequence[Mapping[str, Any]],
) -> list[dict]:
    """持久化整改后的复查照片，与初检证据分阶段保存。"""
    user = _actor(tid, uid, industry_key)
    action = _scoped_action(tid, industry_key, action_id, actor=user)
    if action["status"] != "awaiting_recheck":
        raise InspectionConflict("只有待复查整改任务可上传复查照片")
    photos = _normalize_photos(photo_records, tid, phase="recheck")
    with db.atomic() as connection:
        ids = _insert_photos(
            connection,
            tid=tid,
            uid=uid,
            visit_id=int(action["visit_id"]),
            photos=photos,
        )
        connection.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,issue_id,action_id,"
            "kind,payload_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(tid), int(action["visit_id"]), int(action["issue_id"]),
                int(action_id), "recheck_photos_added",
                _json({"photo_ids": ids}), int(uid), time.time(),
            ),
        )
    return [
        {"id": photo_id, "visit_id": int(action["visit_id"]), **item}
        for photo_id, item in zip(ids, photos)
    ]


def record_recheck(
    tid: int,
    uid: int,
    industry_key: str,
    action_id: int,
    raw: Mapping[str, Any],
    *,
    task_id: int | None = None,
) -> dict:
    user = _actor(tid, uid, industry_key)
    action = _scoped_action(tid, industry_key, action_id, actor=user)
    if action["status"] != "awaiting_recheck":
        raise InspectionConflict("整改任务尚未提交复查")
    photos = db.q(
        "SELECT id FROM inspection_photo WHERE tenant_id=? AND visit_id=? "
        "AND phase='recheck'",
        (int(tid), int(action["visit_id"])),
    )
    normalized = normalize_recheck_result(raw, {int(item["id"]) for item in photos})
    now = time.time()
    with db.atomic() as connection:
        existing = connection.execute(
            "SELECT * FROM inspection_recheck WHERE tenant_id=? AND action_id=? "
            "AND status='pending' ORDER BY id DESC LIMIT 1",
            (int(tid), int(action_id)),
        ).fetchone()
        if existing:
            return _recheck_public(dict(existing))
        cursor = connection.execute(
            "INSERT INTO inspection_recheck(tenant_id,visit_id,issue_id,action_id,"
            "task_id,status,note,model_recommendation,created_by,created_at) "
            "VALUES(?,?,?,?,?,'pending',?,?,?,?)",
            (
                int(tid), int(action["visit_id"]), int(action["issue_id"]),
                int(action_id), int(task_id) if task_id is not None else None,
                normalized["note"], normalized["recommendation"], int(uid), now,
            ),
        )
        recheck_id = int(cursor.lastrowid)
        placeholders = ",".join("?" for _ in normalized["evidence_photo_ids"])
        connection.execute(
            "UPDATE inspection_photo SET recheck_id=? WHERE tenant_id=? "
            "AND visit_id=? AND phase='recheck' "
            f"AND id IN ({placeholders})",
            (
                recheck_id, int(tid), int(action["visit_id"]),
                *normalized["evidence_photo_ids"],
            ),
        )
        for photo_id in normalized["evidence_photo_ids"]:
            connection.execute(
                "INSERT OR IGNORE INTO inspection_evidence(tenant_id,visit_id,issue_id,"
                "photo_id,note,bbox_json,created_at) VALUES(?,?,?,?,?,NULL,?)",
                (
                    int(tid), int(action["visit_id"]), int(action["issue_id"]),
                    photo_id, f"复查证据：{normalized['note']}", now,
                ),
            )
        connection.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,issue_id,action_id,"
            "kind,payload_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(tid), int(action["visit_id"]), int(action["issue_id"]),
                int(action_id), "recheck_submitted",
                _json({
                    "recheck_id": recheck_id,
                    "recommendation": normalized["recommendation"],
                    "confidence": normalized["confidence"],
                    "photo_ids": normalized["evidence_photo_ids"],
                }),
                int(uid), now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM inspection_recheck WHERE id=?", (recheck_id,)
        ).fetchone()
    return _recheck_public(dict(row))


def review_recheck(
    tid: int,
    uid: int,
    industry_key: str,
    recheck_id: int,
    *,
    decision: str,
    expected_action_version: int,
    note: str,
) -> dict:
    # 复查通过/驳回：老板、平台管理员或总监。
    user = _actor(tid, uid, industry_key, branch_admin=True)
    clean_decision = _text(decision, field="复核决定", limit=20, required=True).lower()
    if clean_decision not in {"close", "reject"}:
        raise InspectionError("复核决定只能为 close 或 reject")
    clean_note = _text(note, field="复核意见", limit=1200, required=True)
    expected = _positive_id(expected_action_version + 1, "整改版本") - 1
    recheck = db.one(
        "SELECT r.*,v.industry_key,v.branch_id FROM inspection_recheck r "
        "JOIN inspection_visit v ON v.id=r.visit_id "
        "WHERE r.id=? AND r.tenant_id=? AND v.tenant_id=? AND v.industry_key=? "
        "AND v.deleted_at IS NULL",
        (int(recheck_id), int(tid), int(tid), industry_key),
    )
    if not recheck:
        raise InspectionNotFound("复查记录不存在")
    _branch_scope(tid, industry_key, int(recheck["branch_id"]), actor=user)
    if recheck["status"] != "pending":
        raise InspectionConflict("复查记录已完成人工复核")
    action = _scoped_action(
        tid, industry_key, int(recheck["action_id"]), actor=user,
    )
    if action["status"] != "awaiting_recheck":
        raise InspectionConflict("整改任务当前不在待复核状态")
    now = time.time()
    action_status = "closed" if clean_decision == "close" else "reopened"
    issue_status = "closed" if clean_decision == "close" else "reopened"
    recheck_status = "approved" if clean_decision == "close" else "rejected"
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE inspection_action SET status=?,version=version+1,closed_by=?,"
            "closed_at=?,close_reason=?,updated_at=? WHERE id=? AND tenant_id=? "
            "AND visit_id=? AND status='awaiting_recheck' AND version=?",
            (
                action_status,
                int(uid) if clean_decision == "close" else None,
                now if clean_decision == "close" else None,
                "fixed" if clean_decision == "close" else "",
                now, int(action["id"]), int(tid), int(action["visit_id"]), expected,
            ),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("整改记录已更新，请刷新后再复核")
        issue_changed = connection.execute(
            "UPDATE inspection_issue SET status=?,closure_evidence=?,verified_by=?,"
            "verified_at=?,updated_at=? WHERE id=? AND tenant_id=? AND visit_id=?",
            (
                issue_status, clean_note,
                int(uid) if clean_decision == "close" else None,
                now if clean_decision == "close" else None,
                now, int(action["issue_id"]), int(tid), int(action["visit_id"]),
            ),
        )
        if issue_changed.rowcount != 1:
            raise InspectionConflict("巡店问题已变更，请刷新后再复核")
        recheck_changed = connection.execute(
            "UPDATE inspection_recheck SET status=?,note=?,reviewed_by=?,reviewed_at=? "
            "WHERE id=? AND tenant_id=? AND status='pending'",
            (
                recheck_status, clean_note, int(uid), now,
                int(recheck_id), int(tid),
            ),
        )
        if recheck_changed.rowcount != 1:
            raise InspectionConflict("复查记录已被其他人复核")
        connection.execute(
            "INSERT INTO inspection_event(tenant_id,visit_id,issue_id,action_id,"
            "kind,payload_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(tid), int(action["visit_id"]), int(action["issue_id"]),
                int(action["id"]), "recheck_reviewed",
                _json({"recheck_id": int(recheck_id), "decision": clean_decision, "note": clean_note}),
                int(uid), now,
            ),
        )
        action_row = connection.execute(
            "SELECT * FROM inspection_action WHERE id=?", (int(action["id"]),)
        ).fetchone()
        recheck_row = connection.execute(
            "SELECT * FROM inspection_recheck WHERE id=?", (int(recheck_id),)
        ).fetchone()
    result = _recheck_public(dict(recheck_row))
    result["action"] = _action_public(dict(action_row))
    result["issue_status"] = issue_status
    return result


def get_visit(
    tid: int,
    uid: int,
    industry_key: str,
    visit_id: int,
) -> dict:
    user = _actor(tid, uid, industry_key)
    row = db.one(
        "SELECT v.*,b.name AS branch_name,b.region AS branch_region,"
        "b.address AS branch_address FROM inspection_visit v "
        "JOIN store_branch b ON b.id=v.branch_id AND b.tenant_id=v.tenant_id "
        "WHERE v.id=? AND v.tenant_id=? AND v.industry_key=? AND v.deleted_at IS NULL",
        (int(visit_id), int(tid), industry_key),
    )
    if not row:
        raise InspectionNotFound("巡店记录不存在")
    try:
        # 历史记录允许查看已停用门店，但经理/员工必须负责该门店。
        _assert_branch_visible(user, tid, int(row["branch_id"]))
    except InspectionNotFound:
        raise InspectionNotFound("巡店记录不存在") from None
    contract = _stored_visit_contract(row)
    photos = db.q(
        "SELECT id,recheck_id,storage_key,mime_type,byte_size,sha256,phase,caption,width,"
        "height,capture_slot,item_code,created_at FROM inspection_photo "
        "WHERE tenant_id=? AND visit_id=? "
        "ORDER BY id",
        (int(tid), int(visit_id)),
    )
    photo_by_id: dict[int, dict] = {}
    photos_by_recheck: dict[int, list[dict]] = {}
    for display_no, photo in enumerate(photos, start=1):
        photo_id = int(photo["id"])
        photo["id"] = photo_id
        photo["display_no"] = display_no
        # Empty strings are the schema-52 storage sentinel; keep the public
        # service contract optional/semantic for both legacy and strict rows.
        photo["capture_slot"] = photo.get("capture_slot") or None
        photo["item_code"] = photo.get("item_code") or None
        recheck_id = photo.get("recheck_id")
        photo["recheck_id"] = int(recheck_id) if recheck_id is not None else None
        photo_by_id[photo_id] = photo
        if photo["recheck_id"] is not None:
            photos_by_recheck.setdefault(photo["recheck_id"], []).append(photo)
    issues = db.q(
        "SELECT * FROM inspection_issue WHERE tenant_id=? AND visit_id=? "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END,id",
        (int(tid), int(visit_id)),
    )
    actions = {
        int(item["issue_id"]): item
        for item in db.q(
            "SELECT * FROM inspection_action WHERE tenant_id=? AND visit_id=? ORDER BY id",
            (int(tid), int(visit_id)),
        )
    }
    evidence_by_issue: dict[int, list[dict]] = {}
    for item in db.q(
        "SELECT id,issue_id,photo_id,note,bbox_json,created_at "
        "FROM inspection_evidence WHERE tenant_id=? AND visit_id=? ORDER BY id",
        (int(tid), int(visit_id)),
    ):
        item["bbox"] = db.jloads(item.pop("bbox_json"), None)
        photo = photo_by_id.get(int(item["photo_id"]))
        item["display_no"] = photo.get("display_no") if photo else None
        evidence_by_issue.setdefault(int(item["issue_id"]), []).append(item)
    rechecks_by_action: dict[int, list[dict]] = {}
    for item in db.q(
        "SELECT * FROM inspection_recheck WHERE tenant_id=? AND visit_id=? ORDER BY id",
        (int(tid), int(visit_id)),
    ):
        public_recheck = _recheck_public(item)
        public_recheck["photos"] = photos_by_recheck.get(public_recheck["id"], [])
        rechecks_by_action.setdefault(int(item["action_id"]), []).append(public_recheck)
    # 页面时间线只需结构化事件；内部 payload 可含业务备注与
    # 识别细节，不在详情 API 中返回。
    events = [{
        "id": int(item["id"]),
        "issue_id": int(item["issue_id"]) if item.get("issue_id") is not None else None,
        "action_id": int(item["action_id"]) if item.get("action_id") is not None else None,
        "kind": str(item["kind"]),
        "created_at": item.get("created_at"),
    } for item in db.q(
        "SELECT id,issue_id,action_id,kind,created_at FROM inspection_event "
        "WHERE tenant_id=? AND visit_id=? ORDER BY id",
        (int(tid), int(visit_id)),
    )]
    public_issues: list[dict] = []
    for item in issues:
        issue_id = int(item["id"])
        action_row = actions.get(issue_id)
        public_action = _action_public(action_row) if action_row else None
        if public_action:
            public_action["rechecks"] = rechecks_by_action.get(public_action["id"], [])
        public_issues.append({
            "id": issue_id,
            "title": item["title"],
            "description": item["description"],
            "severity": item["severity"],
            "category": item["category"],
            "status": item["status"],
            "owner": item.get("owner") or "",
            "due_at": item.get("due_at"),
            "confidence": item.get("confidence"),
            "needs_human_check": bool(item.get("needs_human_check")),
            "root_cause": item.get("root_cause") or "",
            "closure_evidence": item.get("closure_evidence") or "",
            "verified_by": item.get("verified_by"),
            "verified_at": item.get("verified_at"),
            "evidence": evidence_by_issue.get(issue_id, []),
            "action": public_action,
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        })
    normalized_model = db.jloads(row.get("model_json"), {}) or {}
    if not isinstance(normalized_model, dict):
        normalized_model = {}
    retake = None
    if row["status"] != "completed":
        # 补拍补齐后状态回到“分析中”（或分析失败待重试），仍要带着补拍名单，
        # 分析时只看补拍的那几张。
        state = _retake_state(normalized_model)
        retake = state["retake"] if state else None
    review_by_photo = {
        int(review.get("photo_id") or 0): review
        for review in normalized_model.get("photo_reviews") or []
        if isinstance(review, dict)
    }
    pending_retake = set((retake or {}).get("pending") or [])
    for photo in photos:
        review = review_by_photo.get(int(photo["id"])) if photo["phase"] == "before" else None
        reason = str((review or {}).get("retake_reason") or "")
        photo["needs_retake"] = int(photo["id"]) in pending_retake
        photo["retake_note"] = (
            RETAKE_REASONS.get(reason, "")
            if photo["needs_retake"] or reason == "unresolved" else ""
        )
    can_assign = _can_assign_branch(user, tid, int(row["branch_id"]))
    ai_reference_score = normalized_model.get("ai_reference_score")
    if ai_reference_score is None and row["status"] == "completed":
        ai_reference_score = normalized_model.get("score")
    return {
        "id": int(row["id"]),
        "tenant_id": int(row["tenant_id"]),
        "industry_key": row["industry_key"],
        "branch": {
            "id": int(row["branch_id"]),
            "name": row["branch_name"],
            "region": row.get("branch_region") or "",
            "address": row.get("branch_address") or "",
        },
        "employee_idx": int(row.get("employee_idx") or EMPLOYEE_IDX),
        "task_id": row.get("task_id"),
        "request_key": row["request_key"],
        "status": row["status"],
        "score": row.get("score"),
        # 门店得分按问题严重度确定性计算；模型原分只作参考，不参与排行。
        "score_rule": normalized_model.get("score_rule") or "",
        "ai_reference_score": ai_reference_score,
        "summary": row.get("summary_md") or "",
        "analysis_status": normalized_model.get("analysis_status"),
        "photo_reviews": normalized_model.get("photo_reviews") or [],
        "retake": copy.deepcopy(retake),
        "unresolved_photo_ids": list(normalized_model.get("unresolved_photo_ids") or []),
        "can_assign": can_assign,
        "assignable_users": (
            assignable_users(tid, int(row["branch_id"]), industry_key)
            if can_assign else []
        ),
        "template_key": (contract or {}).get("template_key"),
        "template_version": (contract or {}).get("template_version"),
        "standard_snapshot": copy.deepcopy(
            (contract or {}).get("template_snapshot")
        ),
        "observations": copy.deepcopy(
            (contract or {}).get("observations")
            or {"metrics": [], "checklist": []}
        ),
        "version": int(row.get("version") or 0),
        "visit_at": row.get("visit_at"),
        "completed_at": row.get("completed_at"),
        "created_by": int(row["created_by"]),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "photos": photos,
        "issues": public_issues,
        "events": events,
    }


def list_visits(
    tid: int,
    uid: int,
    industry_key: str,
    *,
    branch_id: int | None = None,
    region: str | None = None,
    limit: int = 30,
    before_id: int | None = None,
) -> dict:
    """分页返回巡店历史摘要，详细证据由 ``get_visit`` 按需加载。"""
    user = _actor(tid, uid, industry_key)
    selected_branch = None
    if branch_id is not None:
        selected_branch = _branch_scope(
            tid, industry_key, int(branch_id), actor=user,
        )
    if selected_branch is not None and region is not None:
        raise InspectionError("门店与区域筛选不能同时使用")
    clean_region = None
    if region is not None:
        if not isinstance(region, str):
            raise InspectionError("区域筛选格式无效")
        clean_region = region.strip()
        if len(clean_region) > 60 or any(ord(char) < 32 for char in clean_region):
            raise InspectionError("区域筛选格式无效")
    if isinstance(limit, bool):
        raise InspectionError("分页条数无效")
    try:
        page_size = int(limit)
    except (TypeError, ValueError):
        raise InspectionError("分页条数无效") from None
    if not 1 <= page_size <= 100:
        raise InspectionError("分页条数必须在 1-100 之间")
    conditions = [
        "v.tenant_id=?", "v.industry_key=?", "v.deleted_at IS NULL",
    ]
    params: list[Any] = [int(tid), industry_key]
    if selected_branch:
        conditions.append("v.branch_id=?")
        params.append(int(selected_branch["id"]))
    elif clean_region is not None:
        # 区域汇总把空区域显示为“未分区”；下钻用同一表达式，
        # 否则点“未分区 → 看记录”永远是空列表。
        conditions.append(f"{_REGION_LABEL_SQL}=?")
        params.append(clean_region or UNASSIGNED_REGION)
    scope_sql, scope_params = branch_scope_sql(user, tid, "v.branch_id")
    if scope_sql:
        conditions.append(scope_sql.removeprefix(" AND "))
        params.extend(scope_params)
    if before_id is not None:
        cursor_id = _positive_id(before_id, "分页游标")
        conditions.append("v.id<?")
        params.append(cursor_id)
    rows = db.q(
        "SELECT v.id,v.branch_id,v.employee_idx,v.task_id,v.status,v.score,"
        "v.summary_md,v.version,v.visit_at,v.completed_at,v.created_by,v.created_at,"
        "v.updated_at,b.name branch_name,b.region branch_region,"
        # 误报作废的问题不算问题数。
        "(SELECT COUNT(*) FROM inspection_issue i WHERE i.tenant_id=v.tenant_id "
        " AND i.visit_id=v.id AND NOT EXISTS(SELECT 1 FROM inspection_action fa "
        " WHERE fa.tenant_id=i.tenant_id AND fa.issue_id=i.id "
        " AND fa.close_reason='false_positive')) issue_count,"
        "(SELECT COUNT(*) FROM inspection_issue i WHERE i.tenant_id=v.tenant_id "
        " AND i.visit_id=v.id AND i.status!='closed') open_issue_count,"
        "(SELECT COUNT(*) FROM inspection_action a WHERE a.tenant_id=v.tenant_id "
        " AND a.visit_id=v.id AND a.status NOT IN ('closed','awaiting_recheck') "
        " AND a.due_at<?) overdue_count "
        "FROM inspection_visit v JOIN store_branch b ON b.id=v.branch_id "
        "AND b.tenant_id=v.tenant_id WHERE "
        + " AND ".join(conditions)
        + " ORDER BY v.id DESC LIMIT ?",
        (time.time(), *params, page_size + 1),
    )
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    items = [{
        "id": int(row["id"]),
        "branch": {
            "id": int(row["branch_id"]),
            "name": row["branch_name"],
            "region": row.get("branch_region") or "",
        },
        "employee_idx": int(row.get("employee_idx") or EMPLOYEE_IDX),
        "task_id": row.get("task_id"),
        "status": row["status"],
        "score": row.get("score"),
        "summary": row.get("summary_md") or "",
        "version": int(row.get("version") or 0),
        "visit_at": row.get("visit_at"),
        "completed_at": row.get("completed_at"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "issue_count": int(row.get("issue_count") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "overdue_count": int(row.get("overdue_count") or 0),
    } for row in rows]
    return {
        "items": items,
        "next_before_id": items[-1]["id"] if has_more and items else None,
    }


def _time_range(since: float | None, until: float | None) -> tuple[float | None, float | None]:
    start = None if since is None else _bounded_float(
        since, field="统计开始时间", minimum=0, maximum=4_102_444_800
    )
    end = None if until is None else _bounded_float(
        until, field="统计结束时间", minimum=0, maximum=4_102_444_800
    )
    if start is not None and end is not None and start > end:
        raise InspectionError("统计开始时间不能晚于结束时间")
    return start, end


def _aggregate_limit(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InspectionError(f"{field}无效")
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise InspectionError(f"{field}无效") from None
    if not 1 <= limit <= 200:
        raise InspectionError(f"{field}必须在 1-200 之间")
    return limit


def aggregate(
    tid: int,
    uid: int,
    industry_key: str,
    *,
    branch_id: int | None = None,
    since: float | None = None,
    until: float | None = None,
    now: float | None = None,
    branch_limit: int | None = None,
    region_limit: int | None = None,
    pinned_branch_id: int | None = None,
) -> dict:
    """返回当前行业的巡店风险汇总，不读取任何业务正文。

    已授权的区域经理需要依此排定巡店与整改优先级，但仍只能
    看到自己 tenant + industry 内的结构化计数；经理/员工只统计
    分配给自己的门店。统计口径统一排除已停用门店。
    """
    user = _actor(tid, uid, industry_key)
    selected_branch = None
    if branch_id is not None:
        selected_branch = _branch_scope(
            tid, industry_key, int(branch_id), actor=user,
        )
    pinned_branch = None
    if pinned_branch_id is not None:
        pinned_branch = _branch_scope(
            tid, industry_key, int(pinned_branch_id), actor=user,
        )
        if selected_branch and int(pinned_branch["id"]) != int(selected_branch["id"]):
            raise InspectionError("固定门店与汇总门店不一致")
    branch_cap = _aggregate_limit(branch_limit, field="门店汇总条数")
    region_cap = _aggregate_limit(region_limit, field="区域汇总条数")
    start, end = _time_range(since, until)
    current_time = time.time() if now is None else float(now)
    params: list[Any] = [int(tid), industry_key]
    conditions = [
        "v.tenant_id=?", "v.industry_key=?", "v.deleted_at IS NULL",
        "v.status='completed'",
        # 停用门店的整改单已无法推进，不能继续拉低 KPI、计入逾期。
        "EXISTS(SELECT 1 FROM store_branch sb WHERE sb.id=v.branch_id "
        "AND sb.tenant_id=v.tenant_id AND sb.active=1)",
    ]
    scope_sql, scope_params = branch_scope_sql(user, tid, "v.branch_id")
    if scope_sql:
        conditions.append(scope_sql.removeprefix(" AND "))
        params.extend(scope_params)
    if selected_branch:
        conditions.append("v.branch_id=?")
        params.append(int(selected_branch["id"]))
    if start is not None:
        conditions.append("COALESCE(v.visit_at,v.created_at)>=?")
        params.append(start)
    if end is not None:
        conditions.append("COALESCE(v.visit_at,v.created_at)<=?")
        params.append(end)
    where = " AND ".join(conditions)
    visits = db.one(
        f"SELECT COUNT(*) visits,AVG(v.score) average_score,MIN(v.score) lowest_score,"
        f"MAX(v.score) highest_score FROM inspection_visit v WHERE {where}",
        tuple(params),
    ) or {}
    severity = {name: 0 for name in SEVERITIES}
    issue_rows = db.q(
        "SELECT i.severity,i.status,COUNT(*) n FROM inspection_issue i "
        "JOIN inspection_visit v ON v.id=i.visit_id AND v.tenant_id=i.tenant_id "
        f"WHERE {where} AND i.tenant_id=? GROUP BY i.severity,i.status",
        (*params, int(tid)),
    )
    open_issues = 0
    for item in issue_rows:
        if item["status"] != "closed":
            open_issues += int(item["n"])
            if item["severity"] in severity:
                severity[item["severity"]] += int(item["n"])
    # 误报作废的整改既不算总数也不算“已核验”，否则会虚抬整改率。
    action_metrics = db.one(
        "SELECT SUM(CASE WHEN a.close_reason='false_positive' THEN 0 ELSE 1 END) total,"
        "SUM(CASE WHEN a.status='closed' AND a.close_reason!='false_positive' "
        "THEN 1 ELSE 0 END) verified,"
        "SUM(CASE WHEN a.close_reason='false_positive' THEN 1 ELSE 0 END) dismissed,"
        f"SUM(CASE WHEN a.status NOT IN {_OVERDUE_EXCLUDED_SQL} AND a.due_at<? "
        "THEN 1 ELSE 0 END) overdue "
        "FROM inspection_action a JOIN inspection_visit v "
        "ON v.id=a.visit_id AND v.tenant_id=a.tenant_id "
        f"WHERE {where} AND a.tenant_id=?",
        (current_time, *params, int(tid)),
    ) or {}
    total_actions = int(action_metrics.get("total") or 0)
    verified_actions = int(action_metrics.get("verified") or 0)
    overdue_actions = int(action_metrics.get("overdue") or 0)
    dismissed_actions = int(action_metrics.get("dismissed") or 0)
    pending_rechecks = int((db.one(
        "SELECT COUNT(*) n FROM inspection_recheck r JOIN inspection_visit v "
        "ON v.id=r.visit_id AND v.tenant_id=r.tenant_id "
        f"WHERE {where} AND r.tenant_id=? AND r.status='pending'",
        (*params, int(tid)),
    ) or {}).get("n") or 0)

    branch_params: list[Any] = [int(tid), industry_key]
    branch_conditions = ["b.tenant_id=?", "b.industry_key=?", "b.active=1"]
    branch_scope, branch_scope_params = branch_scope_sql(user, tid, "b.id")
    if branch_scope:
        branch_conditions.append(branch_scope.removeprefix(" AND "))
        branch_params.extend(branch_scope_params)
    if selected_branch:
        branch_conditions.append("b.id=?")
        branch_params.append(int(selected_branch["id"]))
    metrics_cte = (
        "WITH filtered_visits AS ("
        " SELECT v.id,v.branch_id,v.score,COALESCE(v.visit_at,v.created_at) visit_time"
        f" FROM inspection_visit v WHERE {where}"
        "), visit_metrics AS ("
        " SELECT branch_id,COUNT(*) visits,AVG(score) average_score,"
        " SUM(score) score_sum,COUNT(score) scored_visits,"
        " MAX(visit_time) last_visit_at FROM filtered_visits GROUP BY branch_id"
        "), issue_metrics AS ("
        " SELECT fv.branch_id,"
        " SUM(CASE WHEN i.status!='closed' THEN 1 ELSE 0 END) open_issues"
        " FROM filtered_visits fv JOIN inspection_issue i ON i.visit_id=fv.id"
        " AND i.tenant_id=? GROUP BY fv.branch_id"
        "), action_metrics AS ("
        " SELECT fv.branch_id,"
        f" SUM(CASE WHEN a.status NOT IN {_OVERDUE_EXCLUDED_SQL} AND a.due_at<?"
        " THEN 1 ELSE 0 END) overdue_actions"
        " FROM filtered_visits fv JOIN inspection_action a ON a.visit_id=fv.id"
        " AND a.tenant_id=? GROUP BY fv.branch_id"
        ") "
    )
    branch_select = (
        "SELECT b.id,b.name,b.region,COALESCE(vm.visits,0) visits,"
        "vm.average_score,vm.score_sum,COALESCE(vm.scored_visits,0) scored_visits,"
        "vm.last_visit_at,COALESCE(im.open_issues,0) open_issues,"
        "COALESCE(am.overdue_actions,0) overdue_actions FROM store_branch b "
        "LEFT JOIN visit_metrics vm ON vm.branch_id=b.id "
        "LEFT JOIN issue_metrics im ON im.branch_id=b.id "
        "LEFT JOIN action_metrics am ON am.branch_id=b.id WHERE "
    )
    metric_params = (
        *params,
        int(tid),
        current_time,
        int(tid),
        *branch_params,
    )

    if branch_cap is None and region_cap is None:
        # Preserve the original full-list service contract for explicit
        # back-office callers.  HTTP callers pass limits below and never enter
        # this materializing path.
        branch_rows = db.q(
            metrics_cte + branch_select + " AND ".join(branch_conditions)
            + " ORDER BY b.region,b.name,b.id",
            metric_params,
        )
        branches = [{
            "id": int(branch["id"]),
            "name": branch["name"],
            "region": branch.get("region") or "",
            "visits": int(branch.get("visits") or 0),
            "average_score": round(float(branch["average_score"]), 1)
            if branch.get("average_score") is not None else None,
            "_score_sum": float(branch.get("score_sum") or 0),
            "_scored_visits": int(branch.get("scored_visits") or 0),
            "open_issues": int(branch.get("open_issues") or 0),
            "overdue_actions": int(branch.get("overdue_actions") or 0),
            "last_visit_at": branch.get("last_visit_at"),
        } for branch in branch_rows]
        branches.sort(key=lambda item: (
            -item["overdue_actions"], -item["open_issues"],
            item["average_score"] is None,
            item["average_score"] if item["average_score"] is not None else 101,
            item["id"],
        ))
        region_buckets: dict[str, dict] = {}
        for branch in branches:
            region = str(branch.get("region") or UNASSIGNED_REGION)
            bucket = region_buckets.setdefault(region, {
                "region": region, "branches": 0, "visits": 0,
                "score_weighted_sum": 0.0, "scored_visits": 0,
                "open_issues": 0, "overdue_actions": 0,
                "last_visit_at": None,
            })
            visits_count = int(branch.get("visits") or 0)
            bucket["branches"] += 1
            bucket["visits"] += visits_count
            if branch["_scored_visits"]:
                bucket["score_weighted_sum"] += branch["_score_sum"]
                bucket["scored_visits"] += branch["_scored_visits"]
            bucket["open_issues"] += int(branch.get("open_issues") or 0)
            bucket["overdue_actions"] += int(branch.get("overdue_actions") or 0)
            last_visit = branch.get("last_visit_at")
            if last_visit is not None and (
                bucket["last_visit_at"] is None or last_visit > bucket["last_visit_at"]
            ):
                bucket["last_visit_at"] = last_visit
        regions = [{
            "region": bucket["region"],
            "branches": bucket["branches"],
            "visits": bucket["visits"],
            "average_score": round(
                bucket["score_weighted_sum"] / bucket["scored_visits"], 1
            ) if bucket["scored_visits"] else None,
            "open_issues": bucket["open_issues"],
            "overdue_actions": bucket["overdue_actions"],
            "last_visit_at": bucket["last_visit_at"],
        } for bucket in region_buckets.values()]
        regions.sort(key=lambda item: (
            -item["overdue_actions"], -item["open_issues"],
            item["average_score"] is None,
            item["average_score"] if item["average_score"] is not None else 101,
            item["region"],
        ))
        for branch in branches:
            branch.pop("_score_sum", None)
            branch.pop("_scored_visits", None)
        total_branches = len(branches)
        visited_branches = sum(int(item["visits"] > 0) for item in branches)
        total_regions = len(regions)
    else:
        counts = db.one(
            "WITH filtered_visits AS (SELECT v.branch_id FROM inspection_visit v WHERE "
            + where
            + "), visited AS (SELECT DISTINCT branch_id FROM filtered_visits) "
            "SELECT COUNT(*) total_branches,"
            "COALESCE(SUM(CASE WHEN visited.branch_id IS NULL THEN 0 ELSE 1 END),0) "
            "visited_branches,COUNT(DISTINCT COALESCE(NULLIF(b.region,''),'未分区')) "
            "total_regions FROM store_branch b LEFT JOIN visited ON visited.branch_id=b.id "
            "WHERE " + " AND ".join(branch_conditions),
            (*params, *branch_params),
        ) or {}
        total_branches = int(counts.get("total_branches") or 0)
        visited_branches = int(counts.get("visited_branches") or 0)
        total_regions = int(counts.get("total_regions") or 0)

        branch_sql = (
            metrics_cte + branch_select + " AND ".join(branch_conditions)
            + " ORDER BY COALESCE(am.overdue_actions,0) DESC,"
            "COALESCE(im.open_issues,0) DESC,(vm.average_score IS NULL) ASC,"
            "ROUND(vm.average_score,1) ASC,b.id ASC"
        )
        branch_args = metric_params
        if branch_cap is not None:
            branch_sql += " LIMIT ?"
            branch_args = (*branch_args, branch_cap)
        branch_rows = db.q(branch_sql, branch_args)

        def public_branch(branch: Mapping[str, Any]) -> dict:
            return {
                "id": int(branch["id"]),
                "name": branch["name"],
                "region": branch.get("region") or "",
                "visits": int(branch.get("visits") or 0),
                "average_score": round(float(branch["average_score"]), 1)
                if branch.get("average_score") is not None else None,
                "open_issues": int(branch.get("open_issues") or 0),
                "overdue_actions": int(branch.get("overdue_actions") or 0),
                "last_visit_at": branch.get("last_visit_at"),
            }

        branches = [public_branch(branch) for branch in branch_rows]
        if pinned_branch and not any(
            int(item["id"]) == int(pinned_branch["id"]) for item in branches
        ):
            pinned_rows = db.q(
                metrics_cte + branch_select + " AND ".join(
                    (*branch_conditions, "b.id=?")
                ) + " LIMIT 1",
                (*metric_params, int(pinned_branch["id"])),
            )
            if pinned_rows:
                pinned_public = public_branch(pinned_rows[0])
                if branch_cap is not None and len(branches) >= branch_cap:
                    branches[-1] = pinned_public
                else:
                    branches.append(pinned_public)

        region_sql = (
            metrics_cte
            + ", branch_metrics AS (SELECT "
            "COALESCE(NULLIF(b.region,''),'未分区') region,"
            "COALESCE(vm.visits,0) visits,COALESCE(vm.score_sum,0) score_sum,"
            "COALESCE(vm.scored_visits,0) scored_visits,vm.last_visit_at,"
            "COALESCE(im.open_issues,0) open_issues,"
            "COALESCE(am.overdue_actions,0) overdue_actions FROM store_branch b "
            "LEFT JOIN visit_metrics vm ON vm.branch_id=b.id "
            "LEFT JOIN issue_metrics im ON im.branch_id=b.id "
            "LEFT JOIN action_metrics am ON am.branch_id=b.id WHERE "
            + " AND ".join(branch_conditions)
            + "), region_rollup AS (SELECT region,COUNT(*) branches,SUM(visits) visits,"
            "SUM(score_sum) score_weighted_sum,SUM(scored_visits) scored_visits,"
            "SUM(open_issues) open_issues,"
            "SUM(overdue_actions) overdue_actions,MAX(last_visit_at) last_visit_at "
            "FROM branch_metrics GROUP BY region) "
            "SELECT region,branches,visits,score_weighted_sum,scored_visits,"
            "open_issues,overdue_actions,last_visit_at FROM region_rollup "
            "ORDER BY overdue_actions DESC,open_issues DESC,(scored_visits=0) ASC,"
            "CASE WHEN scored_visits>0 THEN score_weighted_sum/scored_visits END ASC,"
            "region ASC"
        )
        region_args = metric_params
        if region_cap is not None:
            region_sql += " LIMIT ?"
            region_args = (*region_args, region_cap)
        region_rows = db.q(region_sql, region_args)
        regions = [{
            "region": str(row.get("region") or "未分区"),
            "branches": int(row.get("branches") or 0),
            "visits": int(row.get("visits") or 0),
            "average_score": round(
                float(row["score_weighted_sum"]) / int(row["scored_visits"]), 1
            ) if int(row.get("scored_visits") or 0) else None,
            "open_issues": int(row.get("open_issues") or 0),
            "overdue_actions": int(row.get("overdue_actions") or 0),
            "last_visit_at": row.get("last_visit_at"),
        } for row in region_rows]
    average_score = visits.get("average_score")
    return {
        "tenant_id": int(tid),
        "industry_key": industry_key,
        "branch_id": int(selected_branch["id"]) if selected_branch else None,
        "visits": int(visits.get("visits") or 0),
        "average_score": round(float(average_score), 1) if average_score is not None else None,
        "lowest_score": float(visits["lowest_score"]) if visits.get("lowest_score") is not None else None,
        "highest_score": float(visits["highest_score"]) if visits.get("highest_score") is not None else None,
        "open_issues": open_issues,
        "severity": severity,
        "total_actions": total_actions,
        "verified_actions": verified_actions,
        "rectification_rate": round(verified_actions * 100 / total_actions, 1)
        if total_actions else None,
        "overdue_actions": overdue_actions,
        "dismissed_actions": dismissed_actions,
        "pending_rechecks": pending_rechecks,
        "branches": branches,
        "regions": regions,
        "total_branches": total_branches,
        "visited_branches": visited_branches,
        "total_regions": total_regions,
        "branch_summary_limit": branch_cap,
        "region_summary_limit": region_cap,
        "branches_truncated": total_branches > len(branches),
        "regions_truncated": total_regions > len(regions),
        "since": start,
        "until": end,
    }


async def _maybe_await(value: Any) -> Any:
    return await value if pyinspect.isawaitable(value) else value


def _mark_visit_failed(tid: int, uid: int, visit_id: int, error: BaseException) -> None:
    now = time.time()
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE inspection_visit SET status='failed',terminal_at=?,updated_at=?,"
            "version=version+1 "
            "WHERE id=? AND tenant_id=? AND status IN ('preparing','analyzing') "
            "AND deleted_at IS NULL",
            (now, now, int(visit_id), int(tid)),
        )
        if changed.rowcount:
            # 只记错误类型，不把供应商响应/密钥/业务正文落库。
            connection.execute(
                "INSERT INTO inspection_event(tenant_id,visit_id,kind,payload_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (
                    int(tid), int(visit_id), "inspection_failed",
                    _json({"error_type": type(error).__name__}), int(uid), now,
                ),
            )


def _prepare_run_context(
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
    raw: Mapping[str, Any],
) -> tuple[dict, dict, dict | None]:
    """在 DB worker 中完成运行前的作用域与幂等检查。"""
    user = _actor(tid, uid, industry_key)
    branch = _branch_scope(tid, industry_key, branch_id, actor=user)
    standard_snapshot = _effective_standard_snapshot(
        tid, uid, industry_key, int(branch["id"]), raw,
    )
    request = normalize_visit_input(
        raw,
        industry_key=industry_key,
        standard_snapshot=standard_snapshot,
    )
    existing = _find_request(tid, request["request_key"])
    replay = None
    if existing:
        _assert_replay_scope(
            existing,
            industry_key=industry_key,
            branch_id=branch["id"],
        )
        replay = get_visit(tid, uid, industry_key, int(existing["id"]))
    return branch, request, replay


def _create_preparing_visit(
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
    request: dict,
    task_id: int | None,
) -> tuple[int, dict | None]:
    """用 BEGIN IMMEDIATE 收紧并发相同 request_key 的竞争窗口。"""
    with db.atomic() as connection:
        existing = connection.execute(
            "SELECT id,industry_key,branch_id FROM inspection_visit "
            "WHERE tenant_id=? AND request_key=? AND deleted_at IS NULL",
            (int(tid), request["request_key"]),
        ).fetchone()
        if existing:
            existing_dict = dict(existing)
            _assert_replay_scope(
                existing_dict,
                industry_key=industry_key,
                branch_id=branch_id,
            )
            existing_id = int(existing["id"])
        else:
            existing_id = _insert_visit(
                connection,
                tid=tid,
                uid=uid,
                industry_key=industry_key,
                branch_id=branch_id,
                request=request,
                status="preparing",
                task_id=task_id,
            )
            return existing_id, None
    return existing_id, get_visit(tid, uid, industry_key, existing_id)


def _persist_run_photos(
    tid: int,
    uid: int,
    visit_id: int,
    saved: Sequence[Mapping[str, Any]],
) -> list[dict]:
    with db.atomic() as connection:
        current = connection.execute(
            "SELECT status,template_key,template_version,template_snapshot_json,"
            "observations_json FROM inspection_visit WHERE id=? AND tenant_id=?",
            (visit_id, int(tid)),
        ).fetchone()
        if not current or current["status"] != "preparing":
            raise InspectionConflict("巡店照片已被其他请求处理")
        contract = _stored_visit_contract(dict(current))
        normalized_photos = _normalize_photos(
            saved,
            tid,
            phase="before",
            standard_snapshot=(contract or {}).get("template_snapshot"),
            expected_file_slots=(contract or {}).get("file_slots"),
        )
        photo_ids = _insert_photos(
            connection,
            tid=tid,
            uid=uid,
            visit_id=visit_id,
            photos=normalized_photos,
        )
        changed = connection.execute(
            "UPDATE inspection_visit SET status='analyzing',updated_at=? "
            "WHERE id=? AND tenant_id=? AND status='preparing'",
            (time.time(), visit_id, int(tid)),
        )
        if changed.rowcount != 1:
            raise InspectionConflict("巡店照片已被其他请求处理")
    return [
        {"id": photo_id, **item}
        for photo_id, item in zip(photo_ids, normalized_photos)
    ]


async def run_inspection(
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
    raw: Mapping[str, Any],
    uploads: Sequence[Any],
    *,
    save_photo: Callable[[int, int, int, Any], Mapping[str, Any] | Awaitable[Mapping[str, Any]]],
    analyze_photos: Callable[[dict, list[dict]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]],
    cleanup_photo: Callable[[Mapping[str, Any]], Any | Awaitable[Any]] | None = None,
    task_id: int | None = None,
) -> dict:
    """用注入的 IO 回调执行一次完整巡店。

    ``save_photo`` 必须返回 ``storage_key/mime_type/byte_size/sha256/width/height``；
    ``analyze_photos`` 返回 ``normalize_model_result`` 定义的 JSON。两个回调均可
    为同步或 async 函数。
    """
    if isinstance(uploads, (str, bytes, bytearray)) or not isinstance(uploads, Sequence):
        raise InspectionError("巡店照片列表格式无效")
    if not uploads or len(uploads) > MAX_PHOTOS:
        raise InspectionError(f"请上传 1-{MAX_PHOTOS} 张巡店照片")
    branch, request, replay = await db.arun(
        _prepare_run_context,
        tid,
        uid,
        industry_key,
        branch_id,
        raw,
    )
    if replay is not None:
        return replay
    visit_id, concurrent_replay = await db.arun(
        _create_preparing_visit,
        tid,
        uid,
        industry_key,
        int(branch["id"]),
        request,
        task_id,
    )
    if concurrent_replay is not None:
        return concurrent_replay
    saved: list[Mapping[str, Any]] = []
    try:
        for index, upload in enumerate(uploads):
            record = await _maybe_await(save_photo(int(tid), visit_id, index, upload))
            if not isinstance(record, Mapping):
                raise InspectionError("照片存储回调未返回有效结果")
            saved.append(record)
        public_photos = await db.arun(
            _persist_run_photos,
            tid,
            uid,
            visit_id,
            saved,
        )
        context = {
            "visit_id": visit_id,
            "industry_key": industry_key,
            "branch": {
                "id": int(branch["id"]),
                "name": branch["name"],
                "region": branch.get("region") or "",
                "address": branch.get("address") or "",
            },
            "visit_at": request["visit_at"],
            "note": request["note"],
        }
        if request.get("template_snapshot") is not None:
            context.update({
                "template_key": request["template_key"],
                "template_version": request["template_version"],
                "standard_snapshot": _model_safe_standard_snapshot(
                    request["template_snapshot"]
                ),
            })
        # Do not add request["observations"] here. Revenue, staffing and
        # record values are boss-facing evidence and must not enter the model.
        model_result = await _maybe_await(analyze_photos(context, public_photos))
        if not isinstance(model_result, Mapping):
            raise InspectionError("视觉模型未返回有效的巡店结果")
        return await db.arun(
            complete_visit,
            tid,
            uid,
            industry_key,
            visit_id,
            model_result,
        )
    except BaseException as exc:
        # 未入库的部分上传可删；视觉分析失败时照片已入库，保留以便
        # 后续显式重试，不让区域经理重复上传。
        has_persisted_photos = bool(await db.aone(
            "SELECT id FROM inspection_photo WHERE tenant_id=? AND visit_id=? LIMIT 1",
            (int(tid), visit_id),
        ))
        if cleanup_photo and not has_persisted_photos:
            for record in saved:
                try:
                    await _maybe_await(cleanup_photo(record))
                except Exception:
                    pass
        await db.arun(_mark_visit_failed, tid, uid, visit_id, exc)
        raise
