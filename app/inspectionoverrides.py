"""Tenant/region/branch inspection-standard overrides.

The industry catalog in :mod:`inspectionstandards` is immutable product data.
This module stores only the small, validated patches a company applies on top
of that catalog and produces the exact snapshot that must be frozen onto a
visit.  Reads are available to an authorised industry member; mutations remain
owner/root-only and use compare-and-swap versions.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any

from . import db, inspectionstandards


SCOPE_KINDS = ("tenant", "region", "branch")
ALLOWED_PATCH_FIELDS = ("enabled", "required", "weight", "severity", "shot_guide")
_MAX_SCOPE_KEY = 120
_MAX_PATCH_BYTES = 2_000


class InspectionOverrideError(ValueError):
    """Stable, non-sensitive standard-override failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.safe_message = str(message)
        super().__init__(f"{self.code}: {self.safe_message}")


def _fail(code: str, message: str) -> None:
    raise InspectionOverrideError(code, message)


def _text(value: Any, *, field: str, limit: int, required: bool = False) -> str:
    if value is None:
        clean = ""
    elif isinstance(value, str):
        clean = value.strip()
    else:
        _fail("OVERRIDE_INVALID", f"{field}格式无效")
    if required and not clean:
        _fail("OVERRIDE_INVALID", f"{field}不能为空")
    if len(clean) > limit or any(ord(char) < 32 for char in clean):
        _fail("OVERRIDE_INVALID", f"{field}格式无效")
    return clean


def _ids(tenant_id: int, actor_id: int) -> tuple[int, int]:
    if (
        isinstance(tenant_id, bool)
        or isinstance(actor_id, bool)
        or not isinstance(tenant_id, int)
        or not isinstance(actor_id, int)
        or tenant_id <= 0
        or actor_id <= 0
    ):
        _fail("OVERRIDE_FORBIDDEN", "当前账号无权管理巡店标准")
    return tenant_id, actor_id


def _canonical_positive_int(value: Any, *, field: str) -> int:
    """Accept an integer or canonical decimal path/query value, never coercions."""
    if isinstance(value, bool):
        _fail("OVERRIDE_INVALID", f"{field}格式无效")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value)
        if value != str(result):
            _fail("OVERRIDE_INVALID", f"{field}格式无效")
    else:
        _fail("OVERRIDE_INVALID", f"{field}格式无效")
    if result <= 0:
        _fail("OVERRIDE_INVALID", f"{field}格式无效")
    return result


def _expected_version(value: Any) -> int:
    # CAS versions travel in JSON bodies.  Reject booleans, floats and numeric
    # strings so two clients cannot disagree about a coerced version value.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("OVERRIDE_INVALID", "版本号无效")
    return value


def _authorize(
    tenant_id: int,
    actor_id: int,
    industry_key: str,
    *,
    manager: bool,
) -> tuple[int, int, str]:
    # Lazy import prevents a module cycle: inspection resolves snapshots only
    # after its own module initialization has completed.
    from . import inspection

    tid, uid = _ids(tenant_id, actor_id)
    industry = _text(
        industry_key, field="行业", limit=80, required=True,
    )
    try:
        inspection._actor(tid, uid, industry, manager=manager)
    except inspection.InspectionError:
        _fail("OVERRIDE_FORBIDDEN", "当前账号无权管理巡店标准")
    return tid, uid, industry


def _branch(
    tenant_id: int, industry_key: str, branch_id: int,
) -> dict[str, Any]:
    from . import inspection

    try:
        canonical_id = _canonical_positive_int(branch_id, field="门店范围")
        return inspection._branch_scope(
            int(tenant_id), industry_key, canonical_id,
        )
    except (InspectionOverrideError, inspection.InspectionError):
        _fail("OVERRIDE_NOT_FOUND", "门店不存在")


def _scope_key(
    tenant_id: int,
    industry_key: str,
    scope_kind: Any,
    raw_scope_key: Any,
) -> tuple[str, str]:
    kind = _text(
        scope_kind, field="覆盖范围", limit=20, required=True,
    )
    if kind not in SCOPE_KINDS:
        _fail("OVERRIDE_INVALID", "覆盖范围无效")
    if kind == "tenant":
        if raw_scope_key not in (None, ""):
            _fail("OVERRIDE_INVALID", "企业级标准不能携带区域或门店")
        return kind, ""
    if kind == "region":
        key = _text(
            raw_scope_key, field="区域", limit=_MAX_SCOPE_KEY, required=True,
        )
        if not db.one(
            "SELECT 1 ok FROM store_branch WHERE tenant_id=? AND industry_key=? "
            "AND region=? AND active=1 LIMIT 1",
            (int(tenant_id), industry_key, key),
        ):
            _fail("OVERRIDE_NOT_FOUND", "区域不存在或没有启用门店")
        return kind, key
    branch_id = _canonical_positive_int(raw_scope_key, field="门店范围")
    branch = _branch(tenant_id, industry_key, branch_id)
    return kind, str(int(branch["id"]))


def _patch(value: Any, industry_key: str, item_code: str, layer: str) -> dict:
    if not isinstance(value, Mapping) or not value:
        _fail("OVERRIDE_INVALID", "覆盖内容不能为空")
    clean = dict(value)
    try:
        serialized = json.dumps(
            clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError):
        _fail("OVERRIDE_INVALID", "覆盖内容格式无效")
    if len(serialized.encode("utf-8")) > _MAX_PATCH_BYTES:
        _fail("OVERRIDE_INVALID", "覆盖内容过长")
    try:
        # Use the product catalog's only allow-list and mandatory-item guard;
        # do not maintain a weaker duplicate validator here.
        inspectionstandards.effective_checklist(
            industry_key, {layer: {item_code: clean}},
        )
    except inspectionstandards.InspectionStandardError as exc:
        _fail("OVERRIDE_INVALID", str(exc))
    return clean


def _row_public(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        row_id = int(row["id"])
        version = int(row["row_version"])
        active_number = int(row["active"])
    except (KeyError, TypeError, ValueError, OverflowError):
        _fail("OVERRIDE_STATE_INVALID", "企业巡店标准数据损坏")
    if row_id <= 0 or version <= 0 or active_number not in (0, 1):
        _fail("OVERRIDE_STATE_INVALID", "企业巡店标准数据损坏")
    try:
        patch = json.loads(row.get("patch_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("OVERRIDE_STATE_INVALID", "企业巡店标准数据损坏")
    if not isinstance(patch, dict):
        _fail("OVERRIDE_STATE_INVALID", "企业巡店标准数据损坏")
    try:
        _patch(
            patch,
            str(row.get("industry_key") or ""),
            str(row.get("item_code") or ""),
            str(row.get("scope_kind") or ""),
        )
    except InspectionOverrideError:
        _fail("OVERRIDE_STATE_INVALID", "企业巡店标准数据损坏")
    return {
        "id": row_id,
        "industry_key": str(row["industry_key"]),
        "scope_kind": str(row["scope_kind"]),
        "scope_key": str(row["scope_key"] or ""),
        "item_code": str(row["item_code"]),
        "patch": patch,
        "version": version,
        "active": bool(active_number),
        "updated_at": row.get("updated_at"),
    }


def _relevant_rows(
    tenant_id: int,
    industry_key: str,
    branch: Mapping[str, Any],
) -> list[dict[str, Any]]:
    region = str(branch.get("region") or "")
    rows = db.q(
        "SELECT * FROM inspection_standard_override WHERE tenant_id=? "
        "AND industry_key=? AND ("
        "(scope_kind='tenant' AND scope_key='') OR "
        "(scope_kind='region' AND scope_key=?) OR "
        "(scope_kind='branch' AND scope_key=?)) "
        "ORDER BY CASE scope_kind WHEN 'tenant' THEN 1 WHEN 'region' THEN 2 "
        "ELSE 3 END,item_code,id",
        (int(tenant_id), industry_key, region, str(int(branch["id"]))),
    )
    return [_row_public(row) for row in rows]


def _industry_revision_token(tenant_id: int, industry_key: str) -> dict[str, int]:
    """Return a bounded, monotonic ledger token for every override mutation.

    The token prevents an effective version from returning to the bare catalog
    version when a region is renamed or an override is disabled.  It is an
    aggregate query rather than an unbounded scan, so checklist reads stay
    constant-size even for large chains.
    """
    row = db.one(
        "SELECT COUNT(*) row_count,COALESCE(SUM(row_version),0) version_sum "
        "FROM inspection_standard_override WHERE tenant_id=? AND industry_key=?",
        (int(tenant_id), industry_key),
    ) or {}
    try:
        row_count = int(row.get("row_count") or 0)
        version_sum = int(row.get("version_sum") or 0)
    except (TypeError, ValueError, OverflowError):
        _fail("OVERRIDE_STATE_INVALID", "企业巡店标准数据损坏")
    if row_count < 0 or version_sum < row_count:
        _fail("OVERRIDE_STATE_INVALID", "企业巡店标准数据损坏")
    return {"row_count": row_count, "version_sum": version_sum}


def _validate_connection_state(
    connection: Any,
    tenant_id: int,
    industry_key: str,
) -> None:
    """Reject a write that would poison any existing branch's effective tree."""
    raw_rows = connection.execute(
        "SELECT * FROM inspection_standard_override WHERE tenant_id=? "
        "AND industry_key=? AND active=1 ORDER BY CASE scope_kind "
        "WHEN 'tenant' THEN 1 WHEN 'region' THEN 2 ELSE 3 END,item_code,id",
        (int(tenant_id), industry_key),
    ).fetchall()
    tenant_layer: dict[str, dict] = {}
    region_layers: dict[str, dict[str, dict]] = {}
    branch_layers: dict[str, dict[str, dict]] = {}
    try:
        for raw_row in raw_rows:
            row = _row_public(dict(raw_row))
            if row["scope_kind"] == "tenant":
                tenant_layer[row["item_code"]] = row["patch"]
            elif row["scope_kind"] == "region":
                region_layers.setdefault(row["scope_key"], {})[
                    row["item_code"]
                ] = row["patch"]
            else:
                branch_layers.setdefault(row["scope_key"], {})[
                    row["item_code"]
                ] = row["patch"]

        branches = connection.execute(
            "SELECT id,region FROM store_branch WHERE tenant_id=? "
            "AND industry_key=? ORDER BY id",
            (int(tenant_id), industry_key),
        ).fetchall()
        if not branches:
            inspectionstandards.effective_checklist(
                industry_key,
                {"tenant": tenant_layer, "region": {}, "branch": {}},
            )
        seen_combinations: set[str] = set()
        for branch in branches:
            layers = {
                "tenant": tenant_layer,
                "region": region_layers.get(str(branch["region"] or ""), {}),
                "branch": branch_layers.get(str(int(branch["id"])), {}),
            }
            signature = json.dumps(
                layers, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            if signature in seen_combinations:
                continue
            seen_combinations.add(signature)
            inspectionstandards.effective_checklist(industry_key, layers)
    except (inspectionstandards.InspectionStandardError, InspectionOverrideError):
        _fail(
            "OVERRIDE_INVALID",
            "该覆盖与现有企业、区域或门店标准冲突，请刷新后调整",
        )


def effective_snapshot(
    tenant_id: int,
    actor_id: int,
    industry_key: str,
    branch_id: int,
) -> dict[str, Any]:
    """Resolve tenant → region → branch overrides and freeze a stable snapshot."""
    tid, _uid, industry = _authorize(
        tenant_id, actor_id, industry_key, manager=False,
    )
    branch = _branch(tid, industry, branch_id)
    revision_rows = _relevant_rows(tid, industry, branch)
    industry_revision = _industry_revision_token(tid, industry)
    rows = [row for row in revision_rows if row["active"]]
    layers: dict[str, dict[str, dict]] = {
        "tenant": {}, "region": {}, "branch": {},
    }
    for row in rows:
        layers[row["scope_kind"]][row["item_code"]] = row["patch"]
    try:
        base = inspectionstandards.version_summary(industry)
        items = inspectionstandards.effective_checklist(industry, layers)
        slots = inspectionstandards.capture_slots(industry)
        metrics = inspectionstandards.metric_catalog(industry)
    except inspectionstandards.InspectionStandardError as exc:
        _fail("OVERRIDE_STATE_INVALID", "当前企业巡店标准不可用")
    applied = [{
        "scope_kind": row["scope_kind"],
        "scope_key": row["scope_key"],
        "item_code": row["item_code"],
        "patch": row["patch"],
        "version": row["version"],
    } for row in rows]
    revisions = [{
        "id": row["id"],
        "scope_kind": row["scope_kind"],
        "scope_key": row["scope_key"],
        "item_code": row["item_code"],
        "patch": row["patch"],
        "version": row["version"],
        "active": row["active"],
    } for row in revision_rows]
    item_scopes: dict[str, list[str]] = {}
    field_sources: dict[str, dict[str, str]] = {}
    for row in rows:
        scopes = item_scopes.setdefault(row["item_code"], [])
        if row["scope_kind"] not in scopes:
            scopes.append(row["scope_kind"])
        sources = field_sources.setdefault(row["item_code"], {})
        for field in row["patch"]:
            sources[field] = row["scope_kind"]
    canonical = json.dumps(
        {
            "base_catalog_sha256": base["sha256"],
            "industry_key": industry,
            "industry_revision": industry_revision,
            "revision_overrides": revisions,
            "active_overrides": applied,
            "items": items,
            "slots": slots,
            "metrics": metrics,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    template_version = (
        inspectionstandards.CATALOG_VERSION
        if not industry_revision["row_count"]
        else f"{inspectionstandards.CATALOG_VERSION}+{digest[:12]}"
    )
    return {
        "template_key": industry,
        "template_version": template_version,
        "base_catalog_version": inspectionstandards.CATALOG_VERSION,
        "as_of": inspectionstandards.CATALOG_AS_OF,
        "catalog_sha256": digest,
        "base_catalog_sha256": base["sha256"],
        "items": items,
        "capture_slots": slots,
        "metrics": metrics,
        "override_summary": {
            "applied_count": len(applied),
            "scopes": list(dict.fromkeys(row["scope_kind"] for row in applied)),
            "revision_count": len(revisions),
            "industry_revision_count": industry_revision["row_count"],
            "item_scopes": item_scopes,
            "field_sources": field_sources,
        },
    }


def list_overrides(
    tenant_id: int,
    actor_id: int,
    industry_key: str,
    *,
    scope_kind: str | None = None,
    scope_key: str | int | None = None,
) -> dict[str, Any]:
    tid, _uid, industry = _authorize(
        tenant_id, actor_id, industry_key, manager=True,
    )
    if scope_kind is None:
        _fail("OVERRIDE_INVALID", "必须指定企业、区域或门店覆盖范围")
    conditions = ["tenant_id=?", "industry_key=?"]
    params: list[Any] = [tid, industry]
    resolved_kind: str | None = None
    resolved_key = ""
    resolved_kind, resolved_key = _scope_key(
        tid, industry, scope_kind, scope_key,
    )
    conditions.extend(("scope_kind=?", "scope_key=?"))
    params.extend((resolved_kind, resolved_key))
    rows = db.q(
        "SELECT * FROM inspection_standard_override WHERE "
        + " AND ".join(conditions)
        + " ORDER BY scope_kind,scope_key,item_code,id",
        tuple(params),
    )
    return {
        "industry_key": industry,
        "scope_kind": resolved_kind,
        "scope_key": resolved_key,
        "allowed_fields": list(ALLOWED_PATCH_FIELDS),
        "base_items": inspectionstandards.effective_checklist(industry),
        "items": [_row_public(row) for row in rows],
    }


def upsert_override(
    tenant_id: int,
    actor_id: int,
    industry_key: str,
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _fail("OVERRIDE_INVALID", "覆盖内容格式无效")
    tid, uid, industry = _authorize(
        tenant_id, actor_id, industry_key, manager=True,
    )
    kind, key = _scope_key(
        tid, industry, raw.get("scope_kind"), raw.get("scope_key"),
    )
    item_code = _text(
        raw.get("item_code"), field="检查项", limit=80, required=True,
    )
    patch = _patch(raw.get("patch"), industry, item_code, kind)
    expected = _expected_version(raw.get("expected_version", 0))
    now = time.time()
    patch_json = json.dumps(
        patch, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    with db.atomic() as connection:
        current = connection.execute(
            "SELECT * FROM inspection_standard_override WHERE tenant_id=? "
            "AND industry_key=? AND scope_kind=? AND scope_key=? AND item_code=?",
            (tid, industry, kind, key, item_code),
        ).fetchone()
        if current is None:
            if expected != 0:
                _fail("OVERRIDE_CONFLICT", "企业巡店标准已变更，请刷新")
            cursor = connection.execute(
                "INSERT INTO inspection_standard_override(tenant_id,industry_key,"
                "scope_kind,scope_key,item_code,patch_json,row_version,active,"
                "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,1,1,?,?,?)",
                (tid, industry, kind, key, item_code, patch_json, uid, now, now),
            )
            override_id = int(cursor.lastrowid)
        else:
            if int(current["row_version"]) != expected:
                _fail("OVERRIDE_CONFLICT", "企业巡店标准已变更，请刷新")
            changed = connection.execute(
                "UPDATE inspection_standard_override SET patch_json=?,active=1,"
                "row_version=row_version+1,updated_at=? WHERE id=? AND tenant_id=? "
                "AND row_version=?",
                (patch_json, now, int(current["id"]), tid, expected),
            )
            if changed.rowcount != 1:
                _fail("OVERRIDE_CONFLICT", "企业巡店标准已变更，请刷新")
            override_id = int(current["id"])
        _validate_connection_state(connection, tid, industry)
        saved = connection.execute(
            "SELECT * FROM inspection_standard_override WHERE id=? AND tenant_id=?",
            (override_id, tid),
        ).fetchone()
    return _row_public(dict(saved))


def disable_override(
    tenant_id: int,
    actor_id: int,
    industry_key: str,
    override_id: int,
    expected_version: int,
) -> dict[str, Any]:
    tid, _uid, industry = _authorize(
        tenant_id, actor_id, industry_key, manager=True,
    )
    oid = _canonical_positive_int(override_id, field="标准记录")
    expected = _expected_version(expected_version)
    if expected <= 0:
        _fail("OVERRIDE_INVALID", "版本号无效")
    now = time.time()
    with db.atomic() as connection:
        current = connection.execute(
            "SELECT * FROM inspection_standard_override WHERE id=? AND tenant_id=? "
            "AND industry_key=?",
            (oid, tid, industry),
        ).fetchone()
        if current is None:
            _fail("OVERRIDE_NOT_FOUND", "企业巡店标准不存在")
        if int(current["row_version"]) != expected:
            _fail("OVERRIDE_CONFLICT", "企业巡店标准已变更，请刷新")
        if not int(current["active"] or 0):
            _fail("OVERRIDE_CONFLICT", "企业巡店标准已恢复继承，请刷新")
        changed = connection.execute(
            "UPDATE inspection_standard_override SET active=0,"
            "row_version=row_version+1,updated_at=? WHERE id=? AND tenant_id=? "
            "AND row_version=?",
            (now, oid, tid, expected),
        )
        if changed.rowcount != 1:
            _fail("OVERRIDE_CONFLICT", "企业巡店标准已变更，请刷新")
        _validate_connection_state(connection, tid, industry)
        saved = connection.execute(
            "SELECT * FROM inspection_standard_override WHERE id=? AND tenant_id=?",
            (oid, tid),
        ).fetchone()
    return _row_public(dict(saved))
