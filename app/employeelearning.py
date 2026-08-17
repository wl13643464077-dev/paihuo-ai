"""Schema 55 employee-learning evidence and activation service.

This module is intentionally a small, synchronous service boundary. It does
not know how to call a web search provider and it never mutates an employee
role bundle. A caller injects a researcher which returns captured web
evidence, then injects an activation callback which performs the authoritative
identity/config CAS and writes the new role bundle. Keeping those two effects
outside this module makes it possible to run deterministic tests without
network access, model calls, or billing.

The tables are created by the schema migration owned by the database team. A
few releases may use an expanded table shape, so writes are filtered to columns
that are present while preserving the canonical field names used by schema 55.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import db


class LearningError(RuntimeError):
    """Base error for the learning service."""


class LearningValidationError(LearningError, ValueError):
    """Input or captured evidence violates the learning contract."""


class BudgetExceededError(LearningError):
    """A batch cannot reserve another run within its points cap."""


class InvalidTransitionError(LearningError):
    """A batch/run state transition is not permitted."""


class ApprovalRequiredError(LearningError):
    """An external CAS activation callback is required."""


class StaleActivationError(LearningError):
    """The identity/config CAS failed while activating a proposal."""


TABLE_BATCH = "employee_learning_batch"
TABLE_RUN = "employee_learning_run"
TABLE_SOURCE = "employee_learning_source"
TABLE_ARTIFACT = "employee_learning_artifact"

RUN_QUEUED = "queued"
RUN_RESEARCHING = "researching"
RUN_AWAITING_APPROVAL = "awaiting_approval"
RUN_ACTIVATED = "activated"
RUN_REJECTED = "rejected"
RUN_STALE = "stale"
RUN_EXPIRED = "expired"
RUN_CANCELLED = "cancelled"
RUN_FAILED = "failed"
RUN_EVIDENCE_INSUFFICIENT = "evidence_insufficient"

BATCH_QUEUED = "queued"
BATCH_RUNNING = "running"
BATCH_PAUSED = "paused"
BATCH_COMPLETED = "completed"
BATCH_CANCELLED = "cancelled"

_TERMINAL_RUNS = {
    RUN_ACTIVATED, RUN_REJECTED, RUN_STALE, RUN_EXPIRED, RUN_CANCELLED,
    RUN_FAILED, RUN_EVIDENCE_INSUFFICIENT,
}
_AUTHORITY_LEVELS = {"official", "regulator", "standard", "association"}
_ALLOWED_AUTHORITIES = _AUTHORITY_LEVELS | {"industry", "research", "vendor"}
_ALLOWED_ARTIFACT_KINDS = {
    "knowledge", "skill", "capability", "data_object", "tool", "workflow",
    "escalation", "learning_track",
}
_CAPTURE_PROVIDERS = {
    "websearch", "web_search", "browser_capture", "source_capture", "netfetch",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,190}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "msclkid", "ref", "ref_src"}


def _now() -> float:
    return time.time()


def _json(value, default):
    if isinstance(value, str):
        return db.jloads(value, default)
    return value if value is not None else default


def _json_text(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _columns(table: str) -> set[str]:
    """Return physical columns without assuming the migration is latest."""
    try:
        return {str(row["name"]) for row in db.q(f"PRAGMA table_info({table})")}
    except Exception as exc:  # pragma: no cover - defensive migration boundary
        raise LearningError(f"学习表不可用: {table}") from exc


def _filter_columns(table: str, values: Mapping) -> dict:
    columns = _columns(table)
    return {key: value for key, value in values.items() if key in columns}


def _insert(table: str, values: Mapping) -> int:
    filtered = _filter_columns(table, values)
    if not filtered:
        raise LearningError(f"学习表缺少可写字段: {table}")
    # app.db.insert historically adds updated_at unconditionally. The schema55
    # source table is append-only and intentionally has no updated_at, so use
    # the public execute/one helpers for that shape.
    if "updated_at" not in _columns(table):
        cols = ",".join(filtered)
        placeholders = ",".join("?" for _ in filtered)
        db.execute(
            f"INSERT INTO {table}({cols}) VALUES({placeholders})",
            tuple(filtered.values()),
        )
        row = db.one("SELECT last_insert_rowid() AS id")
        return int(row["id"])
    return db.insert(table, filtered)


def _update(table: str, row_id: int, values: Mapping) -> None:
    filtered = _filter_columns(table, values)
    if not filtered:
        return
    db.update(table, row_id, filtered)


def _value(row: Mapping, *names, default=None):
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _require_reviewer_id(value) -> int:
    """Return one auditable human actor id, rejecting bool/zero/system gaps."""
    if isinstance(value, bool):
        raise LearningValidationError("审批人 ID 无效")
    try:
        reviewer_id = int(value)
    except (TypeError, ValueError) as exc:
        raise LearningValidationError("审批人 ID 无效") from exc
    if reviewer_id <= 0:
        raise LearningValidationError("审批人 ID 无效")
    return reviewer_id


def _get_batch(batch_id: int):
    row = db.one(f"SELECT * FROM {TABLE_BATCH} WHERE id=?", (int(batch_id),))
    if not row:
        raise LearningError("学习批次不存在")
    return row


def _batch_key_column() -> str:
    columns = _columns(TABLE_BATCH)
    if "idempotency_key" in columns:
        return "idempotency_key"
    if "request_key" in columns:
        return "request_key"
    raise LearningError("学习批次缺少幂等键字段")


def _batch_budget_column() -> str:
    columns = _columns(TABLE_BATCH)
    if "budget_cap_points" in columns:
        return "budget_cap_points"
    if "budget_points" in columns:
        return "budget_points"
    raise LearningError("学习批次缺少预算字段")


def _run_meta(row: Mapping) -> dict:
    raw = _value(row, "result_json", default="{}")
    meta = _json(raw, {})
    if not isinstance(meta, Mapping):
        return {}
    service = meta.get("_employeelearning")
    return dict(service) if isinstance(service, Mapping) else {}


def _decorate_run(row: Mapping) -> dict:
    out = dict(row)
    meta = _run_meta(row)
    for key, value in meta.items():
        out.setdefault(key, value)
    if "service_status" in meta:
        out["status"] = meta["service_status"]
    return out


def _run_status_for_storage(status: str) -> str:
    columns = _columns(TABLE_RUN)
    # The first schema55 migration shipped without explicit expired/cancelled
    # states. Preserve the service state in result_json while satisfying its
    # physical CHECK constraint with failed.
    if status in {RUN_EXPIRED, RUN_CANCELLED, RUN_EVIDENCE_INSUFFICIENT}:
        sql = db.one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE_RUN,),
        )
        sql_text = str(sql.get("sql") or "") if sql else ""
        if sql_text and "check(" in sql_text.lower() and status not in sql_text:
            return RUN_FAILED
    return status


def _set_run_meta(run: Mapping, values: Mapping) -> dict:
    current = _run_meta(run)
    current.update(values)
    encoded = _json_text({"_employeelearning": current})
    _update(TABLE_RUN, int(run["id"]), {"result_json": encoded})
    return current


def _set_run_status(run: Mapping, status: str, **extra) -> None:
    raw_status = _run_status_for_storage(status)
    values = {"status": raw_status, "updated_at": _now()}
    if "error_code" in extra:
        values["error_code"] = extra["error_code"]
    if "proposal_json" in extra:
        values["proposal_json"] = extra["proposal_json"]
    if "checkpoint_json" in extra:
        values["checkpoint_json"] = extra["checkpoint_json"]
    _update(TABLE_RUN, int(run["id"]), values)
    meta = {"service_status": status}
    for key in ("proposal_json", "checkpoint_json", "expires_at"):
        if key in extra:
            meta[key] = extra[key]
    _set_run_meta(run, meta)


def _get_run(run_id: int):
    row = db.one(f"SELECT * FROM {TABLE_RUN} WHERE id=?", (int(run_id),))
    if not row:
        raise LearningError("学习运行不存在")
    return _decorate_run(row)


def _identity_run_owner(
    identity_ref: str, *, exclude_run_id: int | None = None,
) -> dict | None:
    """Return the oldest live run that owns one immutable identity.

    The lookup is always called inside ``db.atomic()`` for a create/start
    decision.  ``BEGIN IMMEDIATE`` serializes the read-before-insert sequence
    across worker connections, so two batches cannot both enqueue the same
    identity and later charge it twice.  Treat unknown future states as live;
    only an explicitly terminal state releases the identity.
    """
    terminal = sorted(_TERMINAL_RUNS)
    placeholders = ",".join("?" for _ in terminal)
    args: list[object] = [str(identity_ref), *terminal]
    exclude_sql = ""
    if exclude_run_id is not None:
        exclude_sql = " AND id<>?"
        args.append(int(exclude_run_id))
    return db.one(
        f"SELECT * FROM {TABLE_RUN} WHERE identity_ref=? "
        f"AND status NOT IN ({placeholders}){exclude_sql} ORDER BY id LIMIT 1",
        tuple(args),
    )


def _assert_idempotency(value: str, label: str = "幂等键") -> str:
    text = str(value or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(text):
        raise LearningValidationError(f"{label}格式无效")
    return text


def _canonical_url(raw: str) -> tuple[str, str]:
    value = str(raw or "").strip()
    if not value or _CONTROL_RE.search(value) or len(value) > 4096:
        raise LearningValidationError("来源 URL 无效")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise LearningValidationError("来源 URL 无效") from exc
    if parsed.scheme.lower() != "https" or not host:
        raise LearningValidationError("来源必须是 HTTPS URL")
    if parsed.username or parsed.password:
        raise LearningValidationError("来源 URL 不得包含用户信息")
    if port is not None and not 1 <= port <= 65535:
        raise LearningValidationError("来源端口无效")
    hostname = host.rstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9._:-]+", hostname):
        raise LearningValidationError("来源域名无效")
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if port is not None and not (
        (parsed.scheme.lower() == "https" and port == 443)
        or (parsed.scheme.lower() == "http" and port == 80)
    ):
        netloc = f"{netloc}:{port}"
    path = parsed.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query_items = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        key_l = key.lower()
        if key_l.startswith("utm_") or key_l in _TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, item))
    query_items.sort()
    query = urlencode(query_items, doseq=True)
    canonical = urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))
    return canonical, hostname.strip("[]")


def validate_source(source: Mapping) -> dict:
    """Validate one captured source, never a model-written citation."""
    if not isinstance(source, Mapping):
        raise LearningValidationError("来源必须是对象")
    raw_url = source.get("canonical_url") or source.get("url")
    canonical_url, domain = _canonical_url(raw_url)
    provider = str(source.get("capture_provider") or "").strip().lower()
    if provider not in _CAPTURE_PROVIDERS:
        raise LearningValidationError("来源必须来自可捕获的 WebSearch 事件")
    event_id = str(source.get("capture_event_id") or "").strip()
    if not event_id or _CONTROL_RE.search(event_id) or len(event_id) > 240:
        raise LearningValidationError("缺少真实来源捕获事件")
    title = str(source.get("title") or "").strip()
    publisher = str(source.get("publisher") or "").strip()
    authority = str(source.get("authority_level") or "").strip().lower()
    if not title or not publisher or len(title) > 500 or len(publisher) > 300:
        raise LearningValidationError("来源标题或发布者无效")
    if authority not in _ALLOWED_AUTHORITIES:
        raise LearningValidationError("来源层级无效")
    try:
        status = int(source.get("http_status"))
    except (TypeError, ValueError) as exc:
        raise LearningValidationError("来源 HTTP 状态无效") from exc
    if not 200 <= status < 300 or not bool(source.get("tls_valid", False)):
        raise LearningValidationError("不可达、非 2xx 或 TLS 无效的来源不计入证据")
    digest = str(source.get("content_sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise LearningValidationError("来源内容摘要无效")
    excerpt = str(source.get("excerpt") or "").strip()
    if not excerpt or len(excerpt) > 6000 or _CONTROL_RE.search(excerpt):
        raise LearningValidationError("来源证据摘录无效")
    try:
        fetched_at = float(source.get("fetched_at"))
    except (TypeError, ValueError) as exc:
        raise LearningValidationError("来源抓取时间无效") from exc
    if fetched_at <= 0:
        raise LearningValidationError("来源抓取时间无效")
    published_at = source.get("published_at")
    if published_at is not None:
        published_at = str(published_at).strip()[:80] or None
    return {
        "canonical_url": canonical_url,
        "domain": domain,
        "title": title,
        "publisher": publisher,
        "authority_level": authority,
        "published_at": published_at,
        "fetched_at": fetched_at,
        "http_status": status,
        "tls_valid": 1,
        "content_sha256": digest,
        "excerpt": excerpt,
        "capture_event_id": event_id,
        "capture_provider": provider,
    }


def source_gate(sources: Iterable[Mapping]) -> dict:
    """Return de-duplicated source/domain/authority counts without raising."""
    normalized = [validate_source(source) for source in (sources or [])]
    by_url = {row["canonical_url"]: row for row in normalized}
    unique = list(by_url.values())
    return {
        "sources": len(unique),
        "domains": len({row["domain"] for row in unique}),
        "authoritative": len({
            row["canonical_url"] for row in unique
            if row["authority_level"] in _AUTHORITY_LEVELS
        }),
    }


def enforce_source_gate(
    sources: Iterable[Mapping], *, high_risk: bool = False
) -> dict:
    counts = source_gate(sources)
    required_sources = 6 if high_risk else 5
    required_authority = 2 if high_risk else 1
    if (
        counts["sources"] < required_sources
        or counts["domains"] < 3
        or counts["authoritative"] < required_authority
    ):
        raise LearningValidationError(
            f"来源证据不足: 至少{required_sources}个来源、3个域、"
            f"{required_authority}个权威来源"
        )
    return counts


def build_learning_proposal(
    run_id: int,
    sources: Iterable[Mapping],
    artifacts: Iterable[Mapping],
) -> dict:
    """Persist a verified proposal from separately captured sources/artifacts.

    This is the integration-friendly path for
    ``providers.call_verified_learning_research``: the provider returns only
    captured sources, while a model/assembler can produce artifact claims whose
    ``source_indexes`` are resolved here to real source IDs. No role bundle is
    touched and the run remains inert until ``approve_run``.
    """
    run = _get_run(run_id)
    if run["status"] != RUN_RESEARCHING:
        raise InvalidTransitionError(f"运行不可形成提案: {run['status']}")
    normalized_sources = [validate_source(source) for source in (sources or [])]
    counts = enforce_source_gate(
        normalized_sources, high_risk=bool(run.get("high_risk"))
    )
    source_rows = record_verified_sources(run_id, normalized_sources)
    by_position = {
        index + 1: int(row["id"]) for index, row in enumerate(source_rows)
    }
    normalized_artifacts = []
    for raw in artifacts or []:
        if not isinstance(raw, Mapping):
            raise LearningValidationError("研究产物必须是对象")
        indexes = raw.get("source_indexes")
        if not isinstance(indexes, (list, tuple, set)):
            raise LearningValidationError("研究产物必须使用捕获来源索引")
        try:
            source_ids = [by_position[int(value)] for value in indexes]
        except (KeyError, TypeError, ValueError) as exc:
            raise LearningValidationError("研究产物来源索引无效") from exc
        normalized_artifacts.append({**raw, "source_ids": source_ids})
    artifact_rows = draft_artifacts(run_id, normalized_artifacts)
    proposal = {
        "source_gate": counts,
        "source_ids": [int(row["id"]) for row in source_rows],
        "artifact_ids": [int(row["id"]) for row in artifact_rows],
        "proposal_only": True,
    }
    with db.atomic():
        run_latest = _get_run(run_id)
        if "proposal_json" in _columns(TABLE_RUN):
            _update(TABLE_RUN, run_id, {
                "status": RUN_AWAITING_APPROVAL,
                "proposal_json": _json_text(proposal),
                "updated_at": _now(),
            })
        else:
            _update(TABLE_RUN, run_id, {
                "status": RUN_AWAITING_APPROVAL,
                "updated_at": _now(),
            })
        _set_run_meta(
            run_latest,
            {"service_status": RUN_AWAITING_APPROVAL, "proposal_json": proposal},
        )
    return {
        **_get_run(run_id),
        "source_count": counts["sources"],
        "artifact_count": len(artifact_rows),
    }


def validate_artifact(artifact: Mapping, known_source_ids: set[int]) -> dict:
    if not isinstance(artifact, Mapping):
        raise LearningValidationError("学习产物必须是对象")
    kind = str(artifact.get("kind") or "").strip().lower()
    if kind not in _ALLOWED_ARTIFACT_KINDS:
        raise LearningValidationError("学习产物类型无效")
    title = str(artifact.get("title") or "").strip()
    statement = str(artifact.get("statement") or "").strip()
    if not title or not statement or len(title) > 300 or len(statement) > 4000:
        raise LearningValidationError("学习产物标题或陈述无效")
    payload = artifact.get("payload", {})
    if not isinstance(payload, Mapping):
        raise LearningValidationError("学习产物 payload 必须是对象")
    raw_ids = artifact.get("source_ids")
    if not isinstance(raw_ids, (list, tuple, set)) or not raw_ids:
        raise LearningValidationError("学习产物必须回链真实来源 ID")
    try:
        source_ids = sorted({int(value) for value in raw_ids})
    except (TypeError, ValueError) as exc:
        raise LearningValidationError("学习产物来源 ID 无效") from exc
    if any(value <= 0 or value not in known_source_ids for value in source_ids):
        raise LearningValidationError("学习产物引用了不存在或非本次运行来源")
    return {
        "kind": kind,
        "title": title,
        "statement": statement,
        "payload": dict(payload),
        "source_ids": source_ids,
    }


def create_batch(
    idempotency_key: str,
    *,
    budget_cap_points: float,
    tenant_id: int = 1,
) -> dict:
    key = _assert_idempotency(idempotency_key, "批次幂等键")
    try:
        cap = float(budget_cap_points)
    except (TypeError, ValueError) as exc:
        raise LearningValidationError("批次预算无效") from exc
    if cap <= 0 or cap > 1_000_000:
        raise LearningValidationError("批次预算必须在有效范围内")
    key_column = _batch_key_column()
    budget_column = _batch_budget_column()
    with db.atomic():
        existing = db.one(
            f"SELECT * FROM {TABLE_BATCH} WHERE {key_column}=?"
            " AND (tenant_id=? OR tenant_id IS NULL)",
            (key, int(tenant_id)),
        )
        if existing:
            if abs(float(_value(
                existing, "budget_cap_points", "budget_points", default=0
            )) - cap) > 1e-9:
                raise LearningValidationError("相同幂等键的预算不可改变")
            return existing
        now = _now()
        batch_values = {
            "tenant_id": int(tenant_id),
            "idempotency_key": key,
            "request_key": key,
            "status": BATCH_QUEUED,
            "budget_cap_points": cap,
            "budget_points": cap,
            "spent_points": 0.0,
            "checkpoint_json": "{}",
            "total_runs": 0,
            "completed_runs": 0,
            "paused_reason": None,
            "created_at": now,
            "updated_at": now,
        }
        batch_id = _insert(TABLE_BATCH, batch_values)
        return _get_batch(batch_id)


def create_run(
    batch_id: int,
    idempotency_key: str,
    *,
    employee_idx: int,
    identity_ref: str,
    base_config_revision: int,
    base_config_sha256: str,
    industry_key: str | None = None,
    budget_points: float,
    high_risk: bool = False,
    expires_at: float | None = None,
) -> dict:
    key = _assert_idempotency(idempotency_key, "运行幂等键")
    identity = str(identity_ref or "").strip()
    config_sha = str(base_config_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(identity) or not _SHA256_RE.fullmatch(config_sha):
        raise LearningValidationError("运行身份或配置摘要无效")
    try:
        points = float(budget_points)
        revision = int(base_config_revision)
        idx = int(employee_idx)
    except (TypeError, ValueError) as exc:
        raise LearningValidationError("运行身份或预算无效") from exc
    if points <= 0 or revision <= 0 or idx <= 0:
        raise LearningValidationError("运行身份或预算无效")
    batch = _get_batch(batch_id)
    run_columns = _columns(TABLE_RUN)
    has_idempotency = "idempotency_key" in run_columns
    with db.atomic():
        if has_idempotency:
            existing = db.one(
                f"SELECT * FROM {TABLE_RUN} WHERE batch_id=? AND idempotency_key=?",
                (int(batch["id"]), key),
            )
        else:
            existing = db.one(
                f"SELECT * FROM {TABLE_RUN} WHERE batch_id=? AND identity_ref=?",
                (int(batch["id"]), identity),
            )
        if existing:
            return _decorate_run(existing)
        owner = _identity_run_owner(identity)
        if owner:
            raise InvalidTransitionError(
                "该员工已有未结束的进修运行，请先完成或终止原运行"
            )
        now = _now()
        run_values = {
            "batch_id": int(batch["id"]),
            "idempotency_key": key,
            "employee_idx": idx,
            "identity_ref": identity,
            "config_revision": revision,
            "base_config_revision": revision,
            "base_config_sha256": config_sha,
            "industry_key": str(industry_key or "").strip() or None,
            "high_risk": 1 if high_risk else 0,
            "budget_points": points,
            "spent_points": 0.0,
            "status": RUN_QUEUED,
            "checkpoint_json": "{}",
            "proposal_json": None,
            "error_code": None,
            "expires_at": float(expires_at) if expires_at is not None else None,
            "result_json": _json_text({
                "_employeelearning": {
                    "idempotency_key": key,
                    "employee_idx": idx,
                    "industry_key": str(industry_key or "").strip() or None,
                    "high_risk": bool(high_risk),
                    "checkpoint_json": "{}",
                    "proposal_json": None,
                    "expires_at": (
                        float(expires_at) if expires_at is not None else None
                    ),
                    "service_status": RUN_QUEUED,
                }
            }),
            "created_at": now,
            "updated_at": now,
        }
        run_id = _insert(TABLE_RUN, run_values)
        # The count is informational; budget is charged only by reserve_budget.
        if "total_runs" in _columns(TABLE_BATCH):
            db.execute(
                f"UPDATE {TABLE_BATCH} SET total_runs=COALESCE(total_runs,0)+1, "
                "updated_at=? WHERE id=?",
                (now, int(batch["id"])),
            )
        return _get_run(run_id)


def get_batch(batch_id: int) -> dict:
    return _get_batch(batch_id)


def get_run(run_id: int) -> dict:
    return {
        **_get_run(run_id),
        "artifacts": [
            dict(row) for row in db.q(
                f"SELECT * FROM {TABLE_ARTIFACT} WHERE run_id=? ORDER BY id",
                (int(run_id),),
            )
        ],
    }


def reserve_budget(run_id: int) -> float:
    """Atomically reserve this run's full declared points, idempotently."""
    with db.atomic():
        run = _get_run(run_id)
        already = float(_value(run, "spent_points", default=0) or 0)
        target = float(
            _value(run, "budget_points", "budget_cap_points", default=0) or 0
        )
        if already >= target:
            return already
        batch = _get_batch(_value(run, "batch_id"))
        if "spent_points" in _columns(TABLE_BATCH):
            spent = float(_value(batch, "spent_points", default=0) or 0)
        else:
            spent = sum(
                float(_value(row, "spent_points", default=0) or 0)
                for row in db.q(
                    f"SELECT spent_points FROM {TABLE_RUN} WHERE batch_id=?",
                    (int(batch["id"]),),
                )
            )
        cap = float(
            _value(batch, "budget_cap_points", "budget_points", default=0) or 0
        )
        delta = target - already
        if spent + delta > cap + 1e-9:
            raise BudgetExceededError("学习批次预算上限已到")
        now = _now()
        _update(TABLE_RUN, int(run["id"]), {
            "spent_points": target, "updated_at": now,
        })
        if "spent_points" in _columns(TABLE_BATCH):
            db.execute(
                f"UPDATE {TABLE_BATCH} SET spent_points=COALESCE(spent_points,0)+?, "
                "updated_at=? WHERE id=?",
                (delta, now, int(batch["id"])),
            )
        return target


def release_budget(run_id: int) -> float:
    """Release an internal batch reservation after a refunded/aborted run.

    This ledger is a campaign guard, not the tenant wallet.  Tenant wallet
    settlement is owned by ``billing_operation``; keeping both ledgers in
    sync prevents one failed web-research attempt from permanently consuming
    the batch cap and blocking a safe retry.
    """
    with db.atomic():
        run = _get_run(run_id)
        reserved = float(_value(run, "spent_points", default=0) or 0)
        if reserved <= 0:
            return 0.0
        now = _now()
        _update(TABLE_RUN, int(run["id"]), {
            "spent_points": 0.0, "updated_at": now,
        })
        if "spent_points" in _columns(TABLE_BATCH):
            db.execute(
                f"UPDATE {TABLE_BATCH} SET spent_points="
                "MAX(0,COALESCE(spent_points,0)-?),updated_at=? WHERE id=?",
                (reserved, now, int(run["batch_id"])),
            )
        return reserved


def defer_run_for_billing(run_id: int, reason: str = "BILLING_WAIT") -> dict:
    """Return an unfunded, not-yet-researched run to the retryable queue.

    Insufficient tenant points are not a completed research outcome.  Keeping
    the frozen target queued lets the owner top up and explicitly resume the
    batch without silently dropping employees from the original manifest.
    """
    with db.atomic():
        run = _get_run(run_id)
        if run["status"] not in {RUN_QUEUED, RUN_RESEARCHING}:
            raise InvalidTransitionError(
                f"运行不可进入计费等待: {run['status']}"
            )
        sources = db.one(
            f"SELECT COUNT(*) AS n FROM {TABLE_SOURCE} WHERE run_id=?",
            (int(run_id),),
        )
        artifacts = db.one(
            f"SELECT COUNT(*) AS n FROM {TABLE_ARTIFACT} WHERE run_id=?",
            (int(run_id),),
        )
        if int((sources or {}).get("n") or 0) or int(
            (artifacts or {}).get("n") or 0
        ):
            raise InvalidTransitionError("已有研究证据的运行不可退回计费队列")
        release_budget(run_id)
        now = _now()
        _set_run_status(
            _get_run(run_id), RUN_QUEUED,
            error_code=str(reason or "BILLING_WAIT")[:500],
            updated_at=now,
        )
        current = _get_run(run_id)
        checkpoint_value = _value(current, "checkpoint_json", default={})
        if isinstance(checkpoint_value, str):
            try:
                checkpoint_value = json.loads(checkpoint_value or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                checkpoint_value = {}
        checkpoint_value = checkpoint_value if isinstance(checkpoint_value, dict) else {}
        checkpoint_value.update({"stage": "billing_wait", "billing_wait": True})
        checkpoint(run_id, checkpoint_value)
        return _get_run(run_id)


def recover_interrupted_runs() -> int:
    """Fail interrupted in-flight research before generic billing recovery.

    A process restart destroys the live WebSearch/model call.  Reusing a
    partially populated run would mix two evidence sessions, so researching
    runs are failed closed and their internal reservations are released.  The
    startup billing recovery then refunds any matching charged operation.
    Queued runs have not started an evidence session and remain retryable.
    """
    rows = db.q(
        f"SELECT id,batch_id FROM {TABLE_RUN} WHERE status=? ORDER BY id",
        (RUN_RESEARCHING,),
    )
    recovered = 0
    for raw in rows:
        run_id = int(raw["id"])
        batch_id = int(raw["batch_id"])
        with db.atomic():
            current = _get_run(run_id)
            if current["status"] != RUN_RESEARCHING:
                continue
            _set_run_status(
                current, RUN_FAILED, error_code="SERVICE_RESTARTED",
            )
            release_budget(run_id)
            _refresh_batch_progress(batch_id)
        recovered += 1
    return recovered


def list_batch_runs(batch_id: int) -> list[dict]:
    """Return one batch's runs in stable order for bounded orchestration/UI."""
    _get_batch(batch_id)
    return [
        _decorate_run(row)
        for row in db.q(
            f"SELECT * FROM {TABLE_RUN} WHERE batch_id=? ORDER BY id",
            (int(batch_id),),
        )
    ]


def start_run(run_id: int, *, allow_existing: bool = True) -> dict:
    with db.atomic():
        run = _get_run(run_id)
        status = str(run["status"])
        if status != RUN_QUEUED:
            if status == RUN_RESEARCHING and allow_existing:
                return run
            raise InvalidTransitionError(f"运行不可开始: {status}")
        owner = _identity_run_owner(
            str(run["identity_ref"]), exclude_run_id=int(run["id"]),
        )
        if owner:
            raise InvalidTransitionError(
                "该员工已被另一未结束的进修运行占用"
            )
        batch = _get_batch(_value(run, "batch_id"))
        if batch["status"] == BATCH_PAUSED:
            raise InvalidTransitionError("批次已暂停")
        now = _now()
        _set_run_status(run, RUN_RESEARCHING)
        if batch["status"] == BATCH_QUEUED:
            _update(TABLE_BATCH, int(batch["id"]), {
                "status": BATCH_RUNNING, "updated_at": now,
            })
        return _get_run(run_id)


def checkpoint(run_id: int, payload: Mapping) -> dict:
    if not isinstance(payload, Mapping):
        raise LearningValidationError("checkpoint 必须是对象")
    encoded = _json_text(dict(payload))
    if len(encoded) > 20_000:
        raise LearningValidationError("checkpoint 过大")
    with db.atomic():
        run = _get_run(run_id)
        if run["status"] in _TERMINAL_RUNS:
            raise InvalidTransitionError("终态运行不可写 checkpoint")
        if "checkpoint_json" in _columns(TABLE_RUN):
            _update(TABLE_RUN, run_id, {
                "checkpoint_json": encoded, "updated_at": _now(),
            })
        _set_run_meta(run, {"checkpoint_json": dict(payload)})
        return _get_run(run_id)


def pause_batch(batch_id: int, reason: str = "") -> dict:
    with db.atomic():
        batch = _get_batch(batch_id)
        if batch["status"] in {BATCH_COMPLETED, BATCH_CANCELLED}:
            raise InvalidTransitionError("终态批次不可暂停")
        now = _now()
        _update(TABLE_BATCH, batch_id, {
            "status": BATCH_PAUSED,
            "paused_reason": str(reason or "")[:500] or None,
            "updated_at": now,
        })
        # A researching run can resume from its persisted page/cursor.
        researching = db.q(
            f"SELECT id FROM {TABLE_RUN} WHERE batch_id=? AND status=?",
            (batch_id, RUN_RESEARCHING),
        )
        for row in researching:
            _set_run_status(
                _get_run(int(row["id"])), RUN_QUEUED, updated_at=now
            )
        return _get_batch(batch_id)


def resume_batch(batch_id: int) -> dict:
    with db.atomic():
        batch = _get_batch(batch_id)
        if batch["status"] != BATCH_PAUSED:
            if batch["status"] == BATCH_QUEUED:
                return batch
            raise InvalidTransitionError("批次不在暂停状态")
        _update(TABLE_BATCH, batch_id, {
            "status": BATCH_QUEUED, "paused_reason": None, "updated_at": _now(),
        })
        return _get_batch(batch_id)


def record_verified_sources(run_id: int, sources: Iterable[Mapping]) -> list[dict]:
    normalized = []
    seen = set()
    for source in sources or []:
        row = validate_source(source)
        if row["canonical_url"] in seen:
            continue
        seen.add(row["canonical_url"])
        normalized.append(row)
    if not normalized:
        raise LearningValidationError("研究结果没有可验证来源")
    with db.atomic():
        run = _get_run(run_id)
        if run["status"] != RUN_RESEARCHING:
            raise InvalidTransitionError(f"运行不可写来源: {run['status']}")
        out = []
        for source in normalized:
            existing = db.one(
                f"SELECT * FROM {TABLE_SOURCE} WHERE run_id=? AND canonical_url=?",
                (run_id, source["canonical_url"]),
            )
            if existing:
                out.append(existing)
                continue
            now = _now()
            source_values = {
                "run_id": int(run_id),
                "url": source["canonical_url"],
                **source,
                "source_level": source["authority_level"],
                "certificate_status": "valid",
                "metadata_json": _json_text({
                    "domain": source["domain"],
                    "capture_provider": source["capture_provider"],
                    "capture_event_id": source["capture_event_id"],
                }),
                "created_at": now,
            }
            source_id = _insert(TABLE_SOURCE, source_values)
            out.append(db.one(
                f"SELECT * FROM {TABLE_SOURCE} WHERE id=?", (source_id,)
            ))
        return out


def _run_sources(run_id: int) -> list[dict]:
    return db.q(
        f"SELECT * FROM {TABLE_SOURCE} WHERE run_id=? ORDER BY id", (run_id,)
    )


def draft_artifacts(run_id: int, artifacts: Iterable[Mapping]) -> list[dict]:
    source_rows = _run_sources(run_id)
    known = {int(row["id"]) for row in source_rows}
    normalized = [validate_artifact(item, known) for item in artifacts or []]
    if not normalized:
        raise LearningValidationError("研究结果没有学习产物")
    with db.atomic():
        run = _get_run(run_id)
        if run["status"] != RUN_RESEARCHING:
            raise InvalidTransitionError(f"运行不可写产物: {run['status']}")
        out = []
        for item in normalized:
            now = _now()
            artifact_values = {
                "run_id": int(run_id),
                "kind": item["kind"],
                "artifact_type": item["kind"],
                "title": item["title"],
                "statement": item["statement"],
                "claim_text": item["statement"],
                "payload_json": _json_text(item["payload"]),
                "delta_json": _json_text(item["payload"]),
                "source_ids_json": _json_text(item["source_ids"]),
                "evidence_json": _json_text({
                    "source_ids": item["source_ids"],
                }),
                "status": "proposed",
                "created_at": now,
                "updated_at": now,
            }
            artifact_id = _insert(TABLE_ARTIFACT, artifact_values)
            out.append(db.one(
                f"SELECT * FROM {TABLE_ARTIFACT} WHERE id=?", (artifact_id,)
            ))
        return out


def _research_call(researcher: Callable, run: Mapping):
    if not callable(researcher):
        raise LearningValidationError("researcher 必须是可注入的可调用对象")
    # Only sanitized immutable identity context reaches the researcher.
    context = {
        "run_id": int(run["id"]),
        "employee_idx": int(run["employee_idx"]),
        "identity_ref": str(run["identity_ref"]),
        "industry_key": run.get("industry_key"),
        "high_risk": bool(run.get("high_risk")),
        "base_config_revision": int(run["base_config_revision"]),
        "base_config_sha256": str(run["base_config_sha256"]),
    }
    result = researcher(context)
    if not isinstance(result, Mapping):
        raise LearningValidationError("研究器必须返回对象")
    return result


def research_run(run_id: int, researcher: Callable) -> dict:
    run = _get_run(run_id)
    if run["status"] != RUN_RESEARCHING:
        raise InvalidTransitionError(f"运行不可研究: {run['status']}")
    if run.get("expires_at") and float(run["expires_at"]) <= _now():
        return expire_run(run_id)
    try:
        result = _research_call(researcher, run)
        raw_sources = result.get("sources")
        normalized_sources = [
            validate_source(item) for item in (raw_sources or [])
        ]
        counts = enforce_source_gate(
            normalized_sources, high_risk=bool(run.get("high_risk"))
        )
        source_rows = record_verified_sources(run_id, normalized_sources)
        by_position = {
            index + 1: int(row["id"]) for index, row in enumerate(source_rows)
        }
        artifacts = []
        for raw in result.get("artifacts") or []:
            if not isinstance(raw, Mapping):
                raise LearningValidationError("研究产物必须是对象")
            indexes = raw.get("source_indexes")
            if not isinstance(indexes, (list, tuple, set)):
                raise LearningValidationError("研究产物必须使用捕获来源索引")
            try:
                source_ids = [by_position[int(value)] for value in indexes]
            except (KeyError, TypeError, ValueError) as exc:
                raise LearningValidationError("研究产物来源索引无效") from exc
            artifacts.append({**raw, "source_ids": source_ids})
        artifact_rows = draft_artifacts(run_id, artifacts)
        proposal = {
            "source_gate": counts,
            "source_ids": [int(row["id"]) for row in source_rows],
            "artifact_ids": [int(row["id"]) for row in artifact_rows],
            "proposal_only": True,
        }
        with db.atomic():
            if "proposal_json" in _columns(TABLE_RUN):
                _update(TABLE_RUN, run_id, {
                    "status": RUN_AWAITING_APPROVAL,
                    "proposal_json": _json_text(proposal),
                    "updated_at": _now(),
                })
            else:
                _update(TABLE_RUN, run_id, {
                    "status": RUN_AWAITING_APPROVAL,
                    "updated_at": _now(),
                })
            _set_run_meta(
                _get_run(run_id),
                {"service_status": RUN_AWAITING_APPROVAL, "proposal_json": proposal},
            )
        return {
            **_get_run(run_id),
            "source_count": counts["sources"],
            "artifact_count": len(artifact_rows),
        }
    except LearningValidationError as exc:
        with db.atomic():
            current = _get_run(run_id)
            _set_run_status(
                current,
                RUN_EVIDENCE_INSUFFICIENT,
                error_code=str(getattr(exc, "code", type(exc).__name__))[:120],
            )
            _refresh_batch_progress(int(current["batch_id"]))
        raise
    except Exception:
        with db.atomic():
            current = _get_run(run_id)
            _set_run_status(
                current, RUN_FAILED, error_code="RESEARCH_FAILED"
            )
            _refresh_batch_progress(int(current["batch_id"]))
        raise


def _proposal(run_id: int) -> dict:
    run = _get_run(run_id)
    proposal = _json(run.get("proposal_json"), {})
    if not isinstance(proposal, Mapping) or not proposal.get("proposal_only"):
        raise LearningValidationError("运行缺少不可变学习提案")
    return dict(proposal)


def _artifact_rows(run_id: int, artifact_ids=None) -> list[dict]:
    if artifact_ids is None:
        return db.q(
            f"SELECT * FROM {TABLE_ARTIFACT} WHERE run_id=? ORDER BY id",
            (run_id,),
        )
    clean = sorted({int(value) for value in artifact_ids})
    if not clean or min(clean) < 1:
        raise LearningValidationError("进修提案缺少固定产物清单")
    placeholders = ",".join("?" for _ in clean)
    rows = db.q(
        f"SELECT * FROM {TABLE_ARTIFACT} WHERE run_id=? "
        f"AND id IN ({placeholders}) ORDER BY id",
        (run_id, *clean),
    )
    if [int(row["id"]) for row in rows] != clean:
        raise LearningValidationError("进修提案产物已缺失或漂移")
    return rows


def _artifact_status_for_storage(status: str) -> str:
    sql = db.one(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE_ARTIFACT,),
    )
    if sql and status not in str(sql.get("sql") or ""):
        if status == "expired":
            return "stale"
        if status == "cancelled":
            return "rejected"
    return status


def _set_artifact_status(
    run_id: int,
    status: str,
    *,
    reviewer_id: int | None = None,
    reviewed_at: float | None = None,
) -> None:
    stored = _artifact_status_for_storage(status)
    columns = _columns(TABLE_ARTIFACT)
    values: dict[str, object] = {"status": stored}
    if reviewer_id is not None or reviewed_at is not None:
        if not {"reviewer_id", "reviewed_at"} <= columns:
            raise LearningError("学习产物审批审计字段缺失")
        if reviewer_id is None or reviewed_at is None:
            raise LearningValidationError("审批审计信息不完整")
        values["reviewer_id"] = _require_reviewer_id(reviewer_id)
        values["reviewed_at"] = float(reviewed_at)
    if "updated_at" in columns:
        values["updated_at"] = _now()
    setters = ",".join(f"{key}=?" for key in values)
    changed = db.execute(
        f"UPDATE {TABLE_ARTIFACT} SET {setters} WHERE run_id=?",
        (*values.values(), int(run_id)),
    )
    if changed <= 0 and reviewer_id is not None:
        raise LearningValidationError("进修运行没有可更新的学习产物")


def _effective_delta(
    run: Mapping, proposal: Mapping, rows: list[Mapping]
) -> dict:
    expected_artifacts = sorted({
        int(value) for value in proposal.get("artifact_ids", [])
    })
    actual_artifacts = [int(row["id"]) for row in rows]
    if not expected_artifacts or actual_artifacts != expected_artifacts:
        raise LearningValidationError("进修激活只能使用提案冻结的产物")
    changes = []
    all_source_ids = set()
    for row in rows:
        ids = _json(_value(row, "source_ids_json", "source_ids"), [])
        if not isinstance(ids, list) or not ids:
            raise LearningValidationError("学习产物缺少来源回链")
        parsed_ids = sorted({int(value) for value in ids})
        all_source_ids.update(parsed_ids)
        changes.append({
            "artifact_id": int(row["id"]),
            "kind": str(_value(row, "kind", "artifact_type", default="")),
            "title": str(_value(row, "title", "claim_text", default="")),
            "statement": str(_value(row, "statement", "claim_text", default="")),
            "payload": _json(
                _value(row, "payload_json", "delta_json", "payload"), {}
            ),
            "source_ids": parsed_ids,
        })
    expected_sources = {
        int(value) for value in proposal.get("source_ids", [])
    }
    if not all_source_ids.issubset(expected_sources):
        raise LearningValidationError("学习产物来源超出提案范围")
    return {
        "proposal_only": False,
        "artifact_ids": [change["artifact_id"] for change in changes],
        "source_ids": sorted(all_source_ids),
        "changes": changes,
        "employee_idx": int(run["employee_idx"]),
        "identity_ref": str(run["identity_ref"]),
        "base_config_revision": int(run["base_config_revision"]),
        "base_config_sha256": str(run["base_config_sha256"]),
    }


def approve_run(
    run_id: int,
    activation_callback: Callable | None,
    *,
    reviewer_id: int,
) -> dict:
    reviewer_id = _require_reviewer_id(reviewer_id)
    if not callable(activation_callback):
        raise ApprovalRequiredError("审批必须提供外部身份/config CAS 回调")
    try:
        with db.atomic():
            run = _get_run(run_id)
            if run["status"] != RUN_AWAITING_APPROVAL:
                raise InvalidTransitionError(
                    f"运行不可审批: {run['status']}"
                )
            if run.get("expires_at") and float(run["expires_at"]) <= _now():
                raise InvalidTransitionError("进修提案已过期")
            proposal = _proposal(run_id)
            rows = _artifact_rows(run_id, proposal.get("artifact_ids"))
            # A proposal is immutable once awaiting approval.  Extra artifacts
            # are never silently activated and indicate a tampered/drifted run.
            all_ids = [int(row["id"]) for row in _artifact_rows(run_id)]
            if all_ids != sorted({int(v) for v in proposal["artifact_ids"]}):
                raise LearningValidationError("待审批进修提案含未冻结产物")
            delta = _effective_delta(run, proposal, rows)
            activation = activation_callback(
                run_id=int(run["id"]),
                batch_id=int(run["batch_id"]),
                expected_identity_ref=str(run["identity_ref"]),
                expected_config_revision=int(run["base_config_revision"]),
                expected_config_sha256=str(run["base_config_sha256"]),
                effective_role_bundle_delta=delta,
                artifact_ids=list(delta["artifact_ids"]),
                source_ids=list(delta["source_ids"]),
            )
            if not isinstance(activation, Mapping):
                raise LearningValidationError("CAS 回调必须返回激活结果")
            if str(activation.get("status") or "activated") in {
                "stale", "conflict",
            }:
                raise StaleActivationError("身份或配置已变更，提案过期")
            status = str(activation.get("status") or "activated")
            if status != RUN_ACTIVATED:
                raise LearningValidationError("CAS 回调未确认 activated")
            reviewed_at = _now()
            review = {
                "decision": "approved",
                "reviewer_id": reviewer_id,
                "reviewed_at": reviewed_at,
            }
            activation_payload = {
                **proposal,
                "proposal_only": False,
                "activation": dict(activation),
                "effective_role_bundle_delta": delta,
                "review": review,
                "reviewer_id": reviewer_id,
                "reviewed_at": reviewed_at,
            }
            run_latest = _get_run(run_id)
            changed = _update(TABLE_RUN, run_id, {
                "status": RUN_ACTIVATED,
                "proposal_json": _json_text(activation_payload),
                "updated_at": _now(),
            })
            _set_run_meta(
                run_latest,
                {
                    "service_status": RUN_ACTIVATED,
                    "proposal_json": activation_payload,
                    "activation": dict(activation),
                    "review": review,
                    "reviewer_id": reviewer_id,
                    "reviewed_at": reviewed_at,
                },
            )
            _set_artifact_status(
                run_id,
                "activated",
                reviewer_id=reviewer_id,
                reviewed_at=reviewed_at,
            )
            _refresh_batch_progress(int(run["batch_id"]))
    except (StaleActivationError, db.StaleWriteError):
        mark_stale(run_id, reason="IDENTITY_CONFIG_CAS_STALE")
        raise
    return {
        **get_run(run_id),
        "status": RUN_ACTIVATED,
        "new_config_revision": activation.get("new_config_revision"),
        "new_config_sha256": activation.get("new_config_sha256"),
        "bundle_sha256": activation.get("bundle_sha256"),
    }


def _refresh_batch_progress(batch_id: int) -> None:
    rows = db.q(
        f"SELECT status FROM {TABLE_RUN} WHERE batch_id=?", (batch_id,)
    )
    if not rows:
        return
    completed = sum(1 for row in rows if row["status"] in _TERMINAL_RUNS)
    current = _get_batch(batch_id)
    status = (
        BATCH_COMPLETED if completed == len(rows)
        else BATCH_PAUSED
        if current["status"] == BATCH_PAUSED
        else BATCH_RUNNING
    )
    db.execute(
        f"UPDATE {TABLE_BATCH} SET completed_runs=?, status=?, updated_at=? "
        "WHERE id=?",
        (completed, status, _now(), batch_id),
    )


def reject_run(
    run_id: int, reason: str = "", *, reviewer_id: int,
) -> dict:
    reviewer_id = _require_reviewer_id(reviewer_id)
    with db.atomic():
        run = _get_run(run_id)
        if run["status"] != RUN_AWAITING_APPROVAL:
            raise InvalidTransitionError(f"运行不可拒绝: {run['status']}")
        proposal = _proposal(run_id)
        reviewed_at = _now()
        clean_reason = str(reason or "REJECTED")[:500]
        review = {
            "decision": "rejected",
            "reason": clean_reason,
            "reviewer_id": reviewer_id,
            "reviewed_at": reviewed_at,
        }
        rejection_payload = {
            **proposal,
            "review": review,
            "reviewer_id": reviewer_id,
            "reviewed_at": reviewed_at,
        }
        _set_run_status(
            run,
            RUN_REJECTED,
            error_code=clean_reason,
            proposal_json=_json_text(rejection_payload),
        )
        _set_run_meta(
            _get_run(run_id),
            {
                "review": review,
                "reviewer_id": reviewer_id,
                "reviewed_at": reviewed_at,
            },
        )
        _set_artifact_status(
            run_id,
            "rejected",
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
        )
        _refresh_batch_progress(int(run["batch_id"]))
        return get_run(run_id)


def mark_stale(run_id: int, reason: str = "STALE") -> dict:
    with db.atomic():
        run = _get_run(run_id)
        if run["status"] in _TERMINAL_RUNS and run["status"] != RUN_STALE:
            raise InvalidTransitionError(f"运行不可标记 stale: {run['status']}")
        _set_run_status(
            run, RUN_STALE, error_code=str(reason or "STALE")[:500]
        )
        _set_artifact_status(run_id, "stale")
        _refresh_batch_progress(int(run["batch_id"]))
        return _get_run(run_id)


def expire_run(run_id: int, *, now: float | None = None) -> dict:
    with db.atomic():
        run = _get_run(run_id)
        if run["status"] == RUN_EXPIRED:
            return run
        if run["status"] in _TERMINAL_RUNS:
            raise InvalidTransitionError(f"运行不可过期: {run['status']}")
        expiry = run.get("expires_at")
        effective_now = _now() if now is None else float(now)
        if expiry is not None and float(expiry) > effective_now:
            raise InvalidTransitionError("运行尚未到期")
        _set_run_status(run, RUN_EXPIRED, error_code="EXPIRED")
        _set_artifact_status(run_id, "expired")
        _refresh_batch_progress(int(run["batch_id"]))
        return _get_run(run_id)


def cancel_run(run_id: int, reason: str = "CANCELLED") -> dict:
    with db.atomic():
        run = _get_run(run_id)
        if run["status"] in _TERMINAL_RUNS:
            raise InvalidTransitionError(f"运行不可取消: {run['status']}")
        _set_run_status(
            run, RUN_CANCELLED, error_code=str(reason or "CANCELLED")[:500]
        )
        _set_artifact_status(run_id, "cancelled")
        _refresh_batch_progress(int(run["batch_id"]))
        return _get_run(run_id)


__all__ = [
    "LearningError", "LearningValidationError", "BudgetExceededError",
    "InvalidTransitionError", "ApprovalRequiredError", "StaleActivationError",
    "RUN_QUEUED", "RUN_RESEARCHING", "RUN_AWAITING_APPROVAL", "RUN_ACTIVATED",
    "RUN_REJECTED", "RUN_STALE", "RUN_EXPIRED", "RUN_CANCELLED", "RUN_FAILED",
    "RUN_EVIDENCE_INSUFFICIENT", "validate_source", "source_gate",
    "enforce_source_gate", "validate_artifact", "create_batch", "create_run",
    "build_learning_proposal",
    "get_batch", "get_run", "list_batch_runs", "reserve_budget",
    "release_budget", "defer_run_for_billing", "recover_interrupted_runs", "start_run", "checkpoint",
    "pause_batch", "resume_batch", "record_verified_sources", "draft_artifacts",
    "research_run", "approve_run", "reject_run", "mark_stale", "expire_run",
    "cancel_run",
]
