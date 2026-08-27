"""Versioned employee identity shared by tasks, threads and meetings."""
from __future__ import annotations

import hashlib
import json
import re

from . import db, departments
from .skills import registry


CORE_CATALOG_VERSION = "core.v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def identity_ref(employee_or_snapshot: dict) -> str:
    """Return the full canonical SHA-256 identity reference.

    The reference represents one role generation, not the human slot.  A V1
    role and today's V3 role may therefore share ``idx`` while their refs remain
    different and old tasks stay replayable.
    """
    if not isinstance(employee_or_snapshot, dict):
        raise departments.DepartmentConfigError("员工身份无效")
    if "spec_sha256" in employee_or_snapshot:
        frozen = {
            "idx": int(employee_or_snapshot.get("idx")),
            "key": str(employee_or_snapshot.get("key") or "").strip(),
            "name": str(employee_or_snapshot.get("name") or "").strip(),
            "dept_key": str(employee_or_snapshot.get("dept_key") or "").strip(),
            "catalog_version": str(
                employee_or_snapshot.get("catalog_version") or ""
            ).strip(),
            "spec_sha256": str(
                employee_or_snapshot.get("spec_sha256") or ""
            ).strip(),
        }
        if employee_or_snapshot.get("identity_scheme") == "v2-person":
            frozen["person_snapshot"] = str(
                employee_or_snapshot.get("person_snapshot", employee_or_snapshot.get("person"))
                or ""
            ).strip()
            frozen["identity_scheme"] = "v2-person"
    else:
        frozen = snapshot(employee_or_snapshot)
    try:
        if frozen.get("identity_scheme") == "v2-person":
            return db.employee_identity_ref_v4(frozen)
        return db.employee_identity_ref(frozen)
    except (TypeError, ValueError) as exc:
        raise departments.DepartmentConfigError("员工身份快照不完整") from exc


def _core_employee(idx: int):
    station = registry.BY_IDX.get(int(idx))
    if not station:
        return None
    frozen = {
        field: station.get(field)
        for field in (
            "idx", "key", "name", "skill", "emoji", "dept", "color",
            "duty", "intro", "approval", "optional", "solo_only",
        )
        if station.get(field) is not None
    }
    payload = json.dumps(
        frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        **station,
        "dept_key": "content",
        "dept_name": "内容生产部",
        "group": station.get("dept") or "内容生产部",
        "catalog_version": CORE_CATALOG_VERSION,
        "employee_spec_sha256": hashlib.sha256(payload).hexdigest(),
        "roster_status": "active",
        "can_assign": True,
        "can_learn": True,
    }


def active_employee(idx: int):
    try:
        employee_idx = int(idx)
    except (TypeError, ValueError):
        return None
    return departments.get_active(employee_idx) or _core_employee(employee_idx)


def any_employee(idx: int):
    try:
        employee_idx = int(idx)
    except (TypeError, ValueError):
        return None
    return departments.get(employee_idx) or _core_employee(employee_idx)


def employee_by_identity_ref(value: str):
    """Resolve one retained role generation by its full immutable ref."""
    expected = str(value or "").strip()
    if not _SHA256_RE.fullmatch(expected):
        return None
    all_versions = getattr(departments, "all_identity_versions", None)
    industry_rows = (
        all_versions() if callable(all_versions)
        else [
            *departments.specialists().values(),
            *departments.legacy_specialists().values(),
        ]
    )
    for employee in industry_rows:
        if identity_ref(employee) == expected:
            return employee
    for idx in registry.BY_IDX:
        employee = _core_employee(idx)
        if employee and identity_ref(employee) == expected:
            return employee
    return None


def _identity_candidates(idx: int) -> list[dict]:
    """Return every catalog generation for an idx, current first.

    ``departments.get`` intentionally exposes an active-first convenience
    view. Frozen records cannot use that shortcut: an old V1 employee must
    remain resolvable even after a later catalog reuses the same numeric idx.
    """
    try:
        employee_idx = int(idx)
    except (TypeError, ValueError):
        return []
    candidates = []
    industry_versions = getattr(departments, "identity_versions", None)
    if callable(industry_versions):
        industry_candidates = industry_versions(employee_idx)
    else:
        industry_candidates = [
            departments.get_active(employee_idx),
            departments.legacy_specialists().get(employee_idx),
        ]
    for employee in (*industry_candidates, _core_employee(employee_idx)):
        if not employee:
            continue
        signature = tuple(snapshot(employee).values())
        if any(tuple(snapshot(row).values()) == signature for row in candidates):
            continue
        candidates.append(employee)
    return candidates


def _raw_identity(frozen: dict, *, task_shape: bool) -> dict | None:
    if not isinstance(frozen, dict):
        return None
    raw_idx = frozen.get("emp_idx" if task_shape else "idx")
    if isinstance(raw_idx, bool):
        return None
    try:
        employee_idx = int(raw_idx)
    except (TypeError, ValueError):
        return None
    mapping = {
        "key": "employee_key" if task_shape else "key",
        "name": "employee_name_snapshot" if task_shape else "name",
        "dept_key": "employee_dept_key" if task_shape else "dept_key",
        "catalog_version": (
            "employee_catalog_version" if task_shape else "catalog_version"
        ),
        "spec_sha256": "employee_spec_sha256" if task_shape else "spec_sha256",
    }
    raw = {"idx": employee_idx}
    for target, source in mapping.items():
        value = frozen.get(source)
        if not isinstance(value, str) or not value or value != value.strip():
            return None
        raw[target] = value
    scheme = frozen.get("identity_scheme")
    person = frozen.get("person_snapshot")
    if scheme is not None or person is not None:
        if scheme in (None, "", "legacy-six"):
            # Schema55 stores uniform display columns on every row.  Legacy
            # V1--V3/core identities deliberately keep the six-field digest;
            # their optional person text must not be reinterpreted as V4.
            return raw
        if not isinstance(scheme, str) or scheme != "v2-person":
            return None
        if not isinstance(person, str) or not person or person != person.strip():
            return None
        raw["identity_scheme"] = scheme
        raw["person_snapshot"] = person
    return raw


def _core_legacy_employee(frozen: dict) -> dict | None:
    """Resolve the one deliberate pre-schema53 compatibility identity."""
    try:
        employee_idx = int(frozen.get("idx"))
    except (TypeError, ValueError):
        return None
    employee = _core_employee(employee_idx)
    if not employee:
        return None
    if (
        frozen.get("key") == f"legacy.idx.{employee_idx}"
        and frozen.get("dept_key") == "content"
        and frozen.get("catalog_version") == "legacy-unknown"
        and frozen.get("name") in {
            str(employee.get("name") or ""), f"历史员工#{employee_idx}",
        }
        and (
            frozen.get("spec_sha256") == "legacy-unknown"
            or bool(_SHA256_RE.fullmatch(str(frozen.get("spec_sha256") or "")))
        )
    ):
        return employee
    return None


def _exact_candidate(frozen: dict) -> dict | None:
    if not _SHA256_RE.fullmatch(str(frozen.get("spec_sha256") or "")):
        return None
    for employee in _identity_candidates(frozen.get("idx")):
        if snapshot(employee) == frozen:
            return employee
    return None


def snapshot(employee: dict) -> dict:
    if str(employee.get("dept_key") or "") != "content":
        return departments.identity_snapshot(employee)
    values = {
        "idx": int(employee.get("idx")),
        "key": str(employee.get("key") or "").strip(),
        "name": str(employee.get("name") or "").strip(),
        "dept_key": "content",
        "catalog_version": str(employee.get("catalog_version") or "").strip(),
        "spec_sha256": str(employee.get("employee_spec_sha256") or "").strip(),
    }
    if any(not values[field] for field in (
        "key", "name", "catalog_version", "spec_sha256",
    )):
        raise departments.DepartmentConfigError("核心员工身份快照字段不完整")
    return values


def task_fields(employee: dict, *, config: dict | None = None) -> dict:
    frozen = snapshot(employee)
    from . import employees

    config = config or employees.ensure_role_config(employee)
    ref = identity_ref(frozen)
    try:
        config_idx = int(config.get("idx"))
        revision = int(config.get("config_revision"))
    except (TypeError, ValueError):
        config_idx = -1
        revision = 0
    config_hash = str(config.get("config_sha256") or "").strip()
    if (
        str(config.get("identity_ref") or "") != ref
        or config_idx != int(frozen["idx"])
        or revision < 1
        or not _SHA256_RE.fullmatch(config_hash)
        or db.employee_config_sha256(ref, revision, config) != config_hash
        or (employee.get("professional_profile") or {})
        != (config.get("professional_profile") or {})
    ):
        raise departments.DepartmentConfigError("员工配置与岗位身份不一致")
    bundle = db.get_employee_role_bundle(
        ref, revision, config_hash,
    )
    if not bundle or not db.employee_role_bundle_row_valid(bundle):
        raise departments.DepartmentConfigError("员工岗位 role bundle 不存在或完整性校验失败")
    result = {
        "employee_key": frozen["key"],
        "employee_catalog_version": frozen["catalog_version"],
        "employee_name_snapshot": frozen["name"],
        "employee_dept_key": frozen["dept_key"],
        "employee_spec_sha256": frozen["spec_sha256"],
        "employee_identity_ref": ref,
        "employee_config_revision": revision,
        "employee_config_sha256": config_hash,
        "bundle_sha256": bundle["bundle_sha256"],
    }
    if frozen.get("identity_scheme") == "v2-person":
        result.update({
            "person_snapshot": frozen["person_snapshot"],
            "identity_scheme": frozen["identity_scheme"],
        })
    else:
        # Keep a uniform task row shape for historical V1--V3 while retaining
        # the old six-field identity digest.
        result.update({
            "person_snapshot": frozen.get("person_snapshot") or bundle.get("person_snapshot", ""),
            "identity_scheme": frozen.get("identity_scheme", "legacy-six"),
        })
    return result


def resolve_task(task: dict):
    """Resolve the exact frozen employee; identity drift fails closed."""
    actual = _raw_identity(task, task_shape=True)
    if not actual:
        return None
    employee = _core_legacy_employee(actual) or _exact_candidate(actual)
    if not employee:
        return None
    persisted_ref = str(task.get("employee_identity_ref") or "").strip()
    if persisted_ref and persisted_ref != identity_ref(actual):
        return None
    return employee


def resolve_task_binding(task: dict) -> dict | None:
    """Resolve one task's exact role identity and immutable config revision."""
    employee = resolve_task(task)
    if not employee:
        return None
    persisted_ref = str(task.get("employee_identity_ref") or "").strip()
    persisted_hash = str(task.get("employee_config_sha256") or "").strip()
    persisted_bundle_hash = str(
        task.get("employee_role_bundle_sha256")
        or task.get("bundle_sha256")
        or ""
    ).strip()
    try:
        revision = int(task.get("employee_config_revision") or 0)
    except (TypeError, ValueError):
        return None
    if (
        persisted_ref != identity_ref(employee)
        or not _SHA256_RE.fullmatch(persisted_ref)
        or not _SHA256_RE.fullmatch(persisted_hash)
        or revision < 1
        or not _SHA256_RE.fullmatch(persisted_bundle_hash)
    ):
        return None
    from . import employees
    config = employees.get_config_by_identity(
        persisted_ref, revision=revision, config_sha256=persisted_hash,
    )
    try:
        config_idx = int(config.get("idx")) if config else -1
    except (TypeError, ValueError):
        config_idx = -1
    if not config or config_idx != int(employee["idx"]):
        return None
    if (employee.get("professional_profile") or {}) != (
        config.get("professional_profile") or {}
    ):
        return None
    bundle = db.get_employee_role_bundle(
        persisted_ref, revision, persisted_hash, persisted_bundle_hash,
    )
    if not bundle:
        return None
    if (
        str(task.get("identity_scheme") or "legacy-six").strip()
        != str(bundle.get("identity_scheme") or "legacy-six").strip()
        or str(task.get("person_snapshot") or "").strip()
        != str(bundle.get("person_snapshot") or "").strip()
    ):
        return None
    return {"employee": employee, "config": config, "role_bundle": bundle}


def _profile_count(value) -> int:
    if isinstance(value, list):
        return len(value) + sum(_profile_count(item) for item in value)
    if isinstance(value, dict):
        return sum(_profile_count(item) for item in value.values())
    return 0


def _retained_v2_lineage(frozen: dict) -> bool:
    """V2 used temporary ids but its already-created work must stay continuable."""
    return (
        str(frozen.get("catalog_version") or "")
        == str(getattr(departments, "HISTORICAL_DECISION_CATALOG_VERSION", ""))
        and 20000 <= int(frozen.get("idx") or 0) <= 29999
    )


def identity_view(employee: dict, *, include_profile: bool = False) -> dict:
    """Return the two-axis current/historical + active/inactive UI contract."""
    frozen = snapshot(employee)
    ref = identity_ref(frozen)
    active = active_employee(frozen["idx"])
    is_current = bool(active and snapshot(active) == frozen)
    from . import employees
    config = employees.ensure_role_config(employee)
    slot = employees.slot_state(frozen["idx"])
    person_active = bool(
        slot["enabled"] and (active or _retained_v2_lineage(frozen))
    )
    profile = (
        config.get("effective_profile")
        or config.get("professional_profile")
        or {}
    )
    capabilities = profile.get("capabilities")
    skill_tree = profile.get("skill_tree")
    summary = {
        "capability_count": _profile_count(capabilities),
        "skill_count": _profile_count(skill_tree),
        "has_profile": bool(profile),
    }
    result = {
        **frozen,
        "identity_ref": ref,
        "config_revision": config["config_revision"],
        "config_sha256": config["config_sha256"],
        "bundle_sha256": config.get("bundle_sha256", ""),
        "person_status": "active" if person_active else "inactive",
        "identity_status": "current" if is_current else "historical",
        "can_assign_new": bool(person_active and is_current),
        "can_continue": bool(person_active),
        "can_learn": bool(person_active and is_current),
        "slot_row_version": slot["row_version"],
        "role_profile_summary": summary,
    }
    if include_profile:
        result["professional_profile"] = profile
    return result


def resolve_snapshot(frozen: dict):
    """Resolve one exact persisted identity snapshot.

    A catalog entry may still be available by ``idx`` after its specification
    changes.  That is not sufficient for execution: key, catalog version,
    display name, department and specification digest must all remain exact.
    The only compatibility exception is a pre-schema53 built-in employee,
    whose migration snapshot is deliberately marked ``legacy-unknown``.
    """
    actual = _raw_identity(frozen, task_shape=False)
    if not actual:
        return None
    employee = _core_legacy_employee(actual) or _exact_candidate(actual)
    if not employee:
        return None
    persisted_ref = str(frozen.get("identity_ref") or "").strip()
    if persisted_ref and persisted_ref != identity_ref(actual):
        return None
    return employee


def roster_metadata_from_snapshot(frozen: dict) -> dict | None:
    """Describe whether an exact frozen identity is still assignable."""
    actual = _raw_identity(frozen, task_shape=False)
    employee = resolve_snapshot(frozen)
    if not actual or not employee:
        return None
    active = active_employee(actual["idx"])
    is_active = bool(
        active
        and (
            snapshot(active) == actual
            or (
                active.get("dept_key") == "content"
                and _core_legacy_employee(actual) is not None
            )
        )
    )
    from . import employees
    enabled = employees.is_enabled(actual["idx"])
    person_active = bool(
        enabled and (active or _retained_v2_lineage(actual))
    )
    return {
        # Compatibility fields for old clients. New clients must use the two
        # independent axes below; a historical role is not an inactive person.
        "roster_status": "active" if is_active else "legacy",
        "can_assign": bool(person_active and is_active),
        "person_status": "active" if person_active else "inactive",
        "identity_status": "current" if is_active else "historical",
        "can_assign_new": bool(person_active and is_active),
        "can_continue": person_active,
        "can_learn": bool(person_active and is_active),
    }


def roster_metadata_from_task(task: dict) -> dict | None:
    actual = _raw_identity(task, task_shape=True)
    if not actual:
        return None
    return roster_metadata_from_snapshot(actual)


def visible_catalog_snapshots(modules) -> list[dict]:
    """Return exact active/legacy identities allowed for a read scope."""
    allowed = {
        str(value).strip() for value in (modules or ())
        if str(value).strip() not in {"", "unknown", "__denied__"}
    }
    rows = []
    seen = set()
    all_versions = getattr(departments, "all_identity_versions", None)
    if callable(all_versions):
        industry_rows = all_versions()
    else:
        industry_rows = [
            *departments.specialists().values(),
            *departments.legacy_specialists().values(),
        ]
    for employee in industry_rows:
        frozen = snapshot(employee)
        if frozen["dept_key"] not in allowed:
            continue
        signature = tuple(frozen.values())
        if signature not in seen:
            rows.append(frozen)
            seen.add(signature)
    if "content" in allowed:
        for idx in sorted(registry.BY_IDX):
            employee = _core_employee(idx)
            if not employee:
                continue
            frozen = {**snapshot(employee), "core_legacy": True}
            signature = tuple(snapshot(employee).values())
            if signature not in seen:
                rows.append(frozen)
                seen.add(signature)
    return rows


def member_snapshot_contract(indices, persisted):
    """Validate a roster against exact current or retained V1 identities."""
    if not isinstance(indices, list) or not isinstance(persisted, list):
        return None
    if len(indices) != len(persisted) or not indices:
        return None
    snapshots_out = []
    seen = set()
    for raw_idx, frozen in zip(indices, persisted):
        if type(raw_idx) is not int:
            return None
        try:
            employee_idx = int(raw_idx)
        except (TypeError, ValueError):
            return None
        if employee_idx in seen or not isinstance(frozen, dict):
            return None
        raw_frozen_idx = frozen.get("idx")
        if type(raw_frozen_idx) is not int:
            return None
        try:
            frozen_idx = int(raw_frozen_idx)
        except (TypeError, ValueError):
            return None
        if frozen_idx != employee_idx:
            return None
        normalized = _raw_identity(frozen, task_shape=False)
        if not normalized or normalized["idx"] != employee_idx:
            return None
        employee = resolve_snapshot(normalized)
        if not employee:
            return None
        identity = str(frozen.get("identity_ref") or "").strip()
        config_hash = str(frozen.get("config_sha256") or "").strip()
        bundle_hash = str(frozen.get("bundle_sha256") or "").strip()
        identity_scheme = str(frozen.get("identity_scheme") or "legacy-six").strip()
        person_snapshot = frozen.get("person_snapshot", "")
        if not isinstance(person_snapshot, str) or person_snapshot != person_snapshot.strip():
            return None
        try:
            revision = int(frozen.get("config_revision") or 0)
        except (TypeError, ValueError):
            return None
        if (
            identity != identity_ref(normalized)
            or not _SHA256_RE.fullmatch(identity)
            or not _SHA256_RE.fullmatch(config_hash)
            or not _SHA256_RE.fullmatch(bundle_hash)
            or revision < 1
            or identity_scheme not in {"legacy-six", "v2-person"}
        ):
            return None
        if identity_scheme == "v2-person":
            if not person_snapshot or normalized.get("identity_scheme") != "v2-person":
                return None
            if person_snapshot != normalized.get("person_snapshot"):
                return None
        from . import employees
        config = employees.get_config_by_identity(
            identity, revision=revision, config_sha256=config_hash,
        )
        try:
            config_idx = int(config.get("idx")) if config else -1
        except (TypeError, ValueError):
            config_idx = -1
        if (
            not config
            or config_idx != employee_idx
            or (employee.get("professional_profile") or {})
            != (config.get("professional_profile") or {})
        ):
            return None
        bundle = db.get_employee_role_bundle(
            identity, revision, config_hash, bundle_hash,
        )
        if not bundle or not db.employee_role_bundle_row_valid(bundle):
            return None
        if (
            identity_scheme
            != str(bundle.get("identity_scheme") or "legacy-six").strip()
            or person_snapshot
            != str(bundle.get("person_snapshot") or "").strip()
        ):
            return None
        seen.add(employee_idx)
        snapshots_out.append({
            **normalized,
            "identity_ref": identity,
            "config_revision": revision,
            "config_sha256": config_hash,
            "person_snapshot": person_snapshot,
            "identity_scheme": identity_scheme,
            "bundle_sha256": bundle_hash,
        })
    return snapshots_out


def resolve_member_snapshots(indices, persisted):
    """Return exact employees for a persisted meeting roster, or ``None``.

    Ordering and cardinality are part of the meeting contract.  A malformed,
    shortened or reordered snapshot must never silently fall back to the live
    roster because that would change who is allowed to execute the meeting.
    """
    frozen_rows = member_snapshot_contract(indices, persisted)
    if not frozen_rows:
        return None
    employees_out = []
    for frozen in frozen_rows:
        employee = resolve_snapshot(frozen)
        if not employee:
            return None
        employees_out.append(employee)
    return employees_out


def member_snapshots(indices: list[int], *, active_only: bool) -> list[dict]:
    rows = []
    for idx in indices:
        employee = active_employee(idx) if active_only else any_employee(idx)
        if not employee:
            raise departments.DepartmentConfigError("会议包含未知或历史员工")
        frozen = snapshot(employee)
        from . import employees
        config = employees.ensure_role_config(employee)
        bundle = db.get_employee_role_bundle(
            identity_ref(frozen), config["config_revision"], config["config_sha256"],
        )
        if not bundle or not db.employee_role_bundle_row_valid(bundle):
            raise departments.DepartmentConfigError(
                "会议员工岗位 role bundle 不存在或完整性校验失败"
            )
        rows.append({
            **frozen,
            "identity_ref": identity_ref(frozen),
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "person_snapshot": (
                frozen.get("person_snapshot")
                or bundle.get("person_snapshot", "")
            ),
            "identity_scheme": frozen.get("identity_scheme", "legacy-six"),
            "bundle_sha256": bundle["bundle_sha256"],
        })
    return rows
