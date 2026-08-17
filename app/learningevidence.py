"""Deterministic cross-language evidence gate for Schema 55 learning.

The model may find pages, but it never decides relevance or authority.  This
module binds public V4 role contracts to a separately generated bilingual
sidecar and derives authority only from the final, verified URL.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from . import employeelearning


SCHEMA = "learning-evidence-gate-v1"
DEFAULT_PATH = Path(__file__).with_name("learning_evidence_gate_v1.json")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\."
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_AUTHORITY_KINDS = {
    "regulator", "standard", "official", "association", "research", "industry",
}
_AUTHORITATIVE_KINDS = _AUTHORITY_KINDS - {"industry"}
_HIGH_RISK_OFFICIAL_KINDS = {"regulator", "standard", "official"}
_TOP_KEYS = {
    "schema", "catalog_version", "source_catalog_sha256",
    "authority_policy_sha256", "industry_aliases", "employees",
    "authority_registry",
}
_INDUSTRY_KEYS = {"industry_key", "aliases_zh", "aliases_en"}
_EMPLOYEE_KEYS = {
    "employee_key", "industry_key", "source_public_contract_sha256",
    "job_label_en", "topics",
}
_TOPIC_KEYS = {
    "topic_id", "canonical_topic", "canonical_topic_sha256", "label_en",
    "object_aliases_en", "method_aliases_en",
}
_ALIAS_KEYS = {"alias", "source_anchor"}
_AUTHORITY_KEYS = {"host", "match", "kind"}


class EvidenceConfigError(employeelearning.LearningValidationError):
    """The signed/release-bound evidence sidecar is missing or malformed."""


class EvidenceGateError(employeelearning.LearningValidationError):
    """Captured evidence fails one deterministic evidence-graph invariant."""

    def __init__(
        self, code: str, message: str, *, counts: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.counts = dict(counts or {})


@dataclass(frozen=True)
class EvidenceConfig:
    digest: str
    catalog_version: str
    source_catalog_sha256: str
    authority_policy_sha256: str
    industries: Mapping[str, Mapping[str, Any]]
    employees: Mapping[str, Mapping[str, Any]]
    authority_registry: tuple[Mapping[str, str], ...]
    canonical_data: Mapping[str, Any]


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_CACHE: tuple[str, int, int, EvidenceConfig] | None = None


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_text(value: Any) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).casefold().split()
    )


def _contains_cjk(value: str) -> bool:
    return any(
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
        for character in value
    )


def alias_matches(text: Any, alias: Any) -> bool:
    haystack = normalize_text(text)
    needle = normalize_text(alias)
    if not needle:
        return False
    if _contains_cjk(needle):
        return needle in haystack
    return re.search(
        rf"(?<![0-9a-z]){re.escape(needle)}(?![0-9a-z])", haystack,
    ) is not None


def _exact_keys(value: Any, keys: set[str], where: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise EvidenceConfigError(
            f"{where} 字段无效: expected={sorted(keys)!r} actual={actual!r}"
        )
    return value


def _string(value: Any, where: str, *, max_length: int = 160) -> str:
    if not isinstance(value, str):
        raise EvidenceConfigError(f"{where} 必须是字符串")
    result = unicodedata.normalize("NFKC", value).strip()
    if not result or len(result) > max_length or re.search(r"[\x00-\x1f\x7f]", result):
        raise EvidenceConfigError(f"{where} 字符串无效")
    return result


def _sha(value: Any, where: str) -> str:
    result = _string(value, where, max_length=64).lower()
    if not _SHA256_RE.fullmatch(result):
        raise EvidenceConfigError(f"{where} 摘要无效")
    return result


def _string_list(
    value: Any, where: str, *, minimum: int = 1, maximum: int = 24,
    item_max: int = 96,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise EvidenceConfigError(f"{where} 数组长度无效")
    rows = tuple(_string(item, f"{where}[]", max_length=item_max) for item in value)
    normalized = [normalize_text(item) for item in rows]
    if len(normalized) != len(set(normalized)):
        raise EvidenceConfigError(f"{where} 存在重复别名")
    return rows


def _alias_rows(value: Any, where: str) -> tuple[Mapping[str, str], ...]:
    if not isinstance(value, list) or not 3 <= len(value) <= 12:
        raise EvidenceConfigError(f"{where} 数组长度无效")
    rows: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        row = _exact_keys(raw, _ALIAS_KEYS, f"{where}[{index}]")
        alias = _string(row["alias"], f"{where}[{index}].alias", max_length=96)
        source_anchor = _string(
            row["source_anchor"], f"{where}[{index}].source_anchor", max_length=120,
        )
        key = normalize_text(alias)
        if key in seen:
            raise EvidenceConfigError(f"{where} 存在重复英文别名")
        seen.add(key)
        rows.append(MappingProxyType({
            "alias": alias, "source_anchor": source_anchor,
        }))
    return tuple(rows)


def load_config_data(raw: Any) -> EvidenceConfig:
    root = _exact_keys(raw, _TOP_KEYS, "evidence sidecar")
    if root["schema"] != SCHEMA:
        raise EvidenceConfigError("evidence sidecar schema 无效")
    catalog_version = _string(root["catalog_version"], "catalog_version")
    source_catalog_sha256 = _sha(
        root["source_catalog_sha256"], "source_catalog_sha256",
    )

    raw_registry = root["authority_registry"]
    if not isinstance(raw_registry, list) or not raw_registry:
        raise EvidenceConfigError("authority_registry 必须是非空数组")
    registry: list[Mapping[str, str]] = []
    registry_keys: set[tuple[str, str]] = set()
    for index, raw_row in enumerate(raw_registry):
        row = _exact_keys(raw_row, _AUTHORITY_KEYS, f"authority_registry[{index}]")
        host = _string(row["host"], f"authority_registry[{index}].host").lower()
        match = _string(row["match"], f"authority_registry[{index}].match").lower()
        kind = _string(row["kind"], f"authority_registry[{index}].kind").lower()
        if not _HOST_RE.fullmatch(host) or host.startswith("xn--"):
            raise EvidenceConfigError("authority_registry host 必须是裸 ASCII DNS 名")
        if match not in {"exact", "suffix"} or kind not in _AUTHORITY_KINDS:
            raise EvidenceConfigError("authority_registry match/kind 无效")
        if (host, match) in registry_keys:
            raise EvidenceConfigError("authority_registry 主机规则重复")
        registry_keys.add((host, match))
        registry.append(MappingProxyType({"host": host, "match": match, "kind": kind}))
    authority_policy_sha256 = _sha(
        root["authority_policy_sha256"], "authority_policy_sha256",
    )
    if authority_policy_sha256 != canonical_sha256(raw_registry):
        raise EvidenceConfigError("authority_policy_sha256 与注册表不一致")

    raw_industries = root["industry_aliases"]
    if not isinstance(raw_industries, list) or not raw_industries:
        raise EvidenceConfigError("industry_aliases 必须是非空数组")
    industries: dict[str, Mapping[str, Any]] = {}
    for index, raw_row in enumerate(raw_industries):
        row = _exact_keys(raw_row, _INDUSTRY_KEYS, f"industry_aliases[{index}]")
        key = _string(row["industry_key"], f"industry_aliases[{index}].industry_key")
        if key in industries:
            raise EvidenceConfigError("industry_key 重复")
        industries[key] = MappingProxyType({
            "industry_key": key,
            "aliases_zh": _string_list(
                row["aliases_zh"], f"industry_aliases[{index}].aliases_zh",
            ),
            "aliases_en": _string_list(
                row["aliases_en"], f"industry_aliases[{index}].aliases_en",
            ),
        })

    raw_employees = root["employees"]
    if not isinstance(raw_employees, list) or not raw_employees:
        raise EvidenceConfigError("employees 必须是非空数组")
    employees: dict[str, Mapping[str, Any]] = {}
    for employee_index, raw_employee in enumerate(raw_employees):
        row = _exact_keys(
            raw_employee, _EMPLOYEE_KEYS, f"employees[{employee_index}]",
        )
        employee_key = _string(
            row["employee_key"], f"employees[{employee_index}].employee_key",
        )
        industry_key = _string(
            row["industry_key"], f"employees[{employee_index}].industry_key",
        )
        if employee_key in employees or industry_key not in industries:
            raise EvidenceConfigError("employee_key 重复或 industry_key 未定义")
        topics_raw = row["topics"]
        if not isinstance(topics_raw, list) or not 1 <= len(topics_raw) <= 12:
            raise EvidenceConfigError("employee topics 数组长度无效")
        topics: list[Mapping[str, Any]] = []
        topic_ids: set[str] = set()
        canonical_topics: set[str] = set()
        for topic_index, raw_topic in enumerate(topics_raw):
            where = f"employees[{employee_index}].topics[{topic_index}]"
            topic = _exact_keys(raw_topic, _TOPIC_KEYS, where)
            topic_id = _string(topic["topic_id"], f"{where}.topic_id")
            canonical_topic = _string(
                topic["canonical_topic"], f"{where}.canonical_topic",
            )
            if topic_id in topic_ids or canonical_topic in canonical_topics:
                raise EvidenceConfigError("employee topic 重复")
            topic_ids.add(topic_id)
            canonical_topics.add(canonical_topic)
            topic_sha = _sha(
                topic["canonical_topic_sha256"], f"{where}.canonical_topic_sha256",
            )
            if topic_sha != canonical_sha256(canonical_topic):
                raise EvidenceConfigError("canonical_topic_sha256 不一致")
            topics.append(MappingProxyType({
                "topic_id": topic_id,
                "canonical_topic": canonical_topic,
                "canonical_topic_sha256": topic_sha,
                "label_en": _string(topic["label_en"], f"{where}.label_en", max_length=96),
                "object_aliases_en": _alias_rows(
                    topic["object_aliases_en"], f"{where}.object_aliases_en",
                ),
                "method_aliases_en": _alias_rows(
                    topic["method_aliases_en"], f"{where}.method_aliases_en",
                ),
            }))
        employees[employee_key] = MappingProxyType({
            "employee_key": employee_key,
            "industry_key": industry_key,
            "source_public_contract_sha256": _sha(
                row["source_public_contract_sha256"],
                f"employees[{employee_index}].source_public_contract_sha256",
            ),
            "job_label_en": _string(
                row["job_label_en"], f"employees[{employee_index}].job_label_en",
                max_length=96,
            ),
            "topics": tuple(topics),
        })

    # Preserve the exact validated JSON shape for a release-stable digest.
    canonical_data = json.loads(canonical_json(root))
    return EvidenceConfig(
        digest=canonical_sha256(canonical_data),
        catalog_version=catalog_version,
        source_catalog_sha256=source_catalog_sha256,
        authority_policy_sha256=authority_policy_sha256,
        industries=MappingProxyType(industries),
        employees=MappingProxyType(employees),
        authority_registry=tuple(registry),
        canonical_data=MappingProxyType(canonical_data),
    )


def load_config(path: str | Path = DEFAULT_PATH) -> EvidenceConfig:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceConfigError("学习证据门禁 sidecar 缺失或不可读") from exc
    return load_config_data(raw)


def load_default_config() -> EvidenceConfig:
    global _DEFAULT_CACHE
    path = DEFAULT_PATH
    try:
        stat = path.stat()
    except OSError as exc:
        raise EvidenceConfigError("学习证据门禁 sidecar 缺失或不可读") from exc
    cache_key = (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    with _DEFAULT_LOCK:
        if _DEFAULT_CACHE and _DEFAULT_CACHE[:3] == cache_key:
            return _DEFAULT_CACHE[3]
        config = load_config(path)
        _DEFAULT_CACHE = (*cache_key, config)
        return config


def employee_public_contract(employee: Mapping[str, Any]) -> dict:
    return {
        "public_research_topics": employee.get("public_research_topics"),
        "public_research_anchor_groups": employee.get("public_research_anchor_groups"),
    }


def _bound_role(
    employee: Mapping[str, Any], config: EvidenceConfig,
) -> tuple[Mapping[str, Any], Mapping[str, Any], dict[str, dict[str, tuple[str, ...]]]]:
    employee_key = str(employee.get("key") or employee.get("employee_key") or "").strip()
    role = config.employees.get(employee_key)
    if not role:
        raise EvidenceConfigError("岗位缺少证据门禁 sidecar 绑定")
    industry_key = str(employee.get("dept_key") or employee.get("industry_key") or "").strip()
    if role["industry_key"] != industry_key:
        raise EvidenceConfigError("岗位行业与证据门禁 sidecar 不一致")
    if str(employee.get("catalog_version") or "") != config.catalog_version:
        raise EvidenceConfigError("岗位目录版本与证据门禁 sidecar 不一致")
    if role["source_public_contract_sha256"] != canonical_sha256(
        employee_public_contract(employee)
    ):
        raise EvidenceConfigError("岗位公开研究合同与证据门禁 sidecar 漂移")
    raw_groups = employee.get("public_research_anchor_groups")
    if not isinstance(raw_groups, list):
        raise EvidenceConfigError("岗位公开研究锚点缺失")
    groups: dict[str, dict[str, tuple[str, ...]]] = {}
    for raw in raw_groups:
        if not isinstance(raw, Mapping):
            raise EvidenceConfigError("岗位公开研究锚点无效")
        topic = str(raw.get("topic") or "").strip()
        objects = tuple(str(item).strip() for item in raw.get("object_anchors") or [])
        methods = tuple(str(item).strip() for item in raw.get("method_anchors") or [])
        if not topic or not objects or not methods or topic in groups:
            raise EvidenceConfigError("岗位公开研究锚点不完整")
        groups[topic] = {"objects": objects, "methods": methods}
    if {topic["canonical_topic"] for topic in role["topics"]} != set(groups):
        raise EvidenceConfigError("证据门禁 sidecar 未完整覆盖岗位专题")
    for topic in role["topics"]:
        anchors = groups[topic["canonical_topic"]]
        if any(
            row["source_anchor"] not in anchors["objects"]
            for row in topic["object_aliases_en"]
        ) or any(
            row["source_anchor"] not in anchors["methods"]
            for row in topic["method_aliases_en"]
        ):
            raise EvidenceConfigError("英文别名未逐字绑定公开研究锚点")
    return role, config.industries[industry_key], groups


def search_aliases(
    employee: Mapping[str, Any], *, config: EvidenceConfig | None = None,
    maximum: int = 24,
) -> tuple[str, ...]:
    config = config or load_default_config()
    role, industry, _groups = _bound_role(employee, config)
    maximum = max(1, min(int(maximum), 32))
    candidates: list[str] = [role["job_label_en"], *industry["aliases_en"]]
    for topic in role["topics"]:
        candidates.append(topic["label_en"])
        candidates.extend(row["alias"] for row in topic["object_aliases_en"])
        candidates.extend(row["alias"] for row in topic["method_aliases_en"])
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = normalize_text(candidate)
        if key and key not in seen:
            seen.add(key)
            result.append(candidate)
        if len(result) >= maximum:
            break
    return tuple(result)


def authority_for_url(
    final_url: Any, *, config: EvidenceConfig | None = None,
) -> str:
    config = config or load_default_config()
    try:
        parsed = urlsplit(str(final_url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return "industry"
    if parsed.scheme != "https" or not host:
        return "industry"
    matches: list[tuple[int, int, str]] = []
    for row in config.authority_registry:
        registered = row["host"]
        if row["match"] == "exact":
            matched = host == registered
            exact_rank = 1
        else:
            matched = host == registered or host.endswith("." + registered)
            exact_rank = 0
        if matched:
            matches.append((len(registered), exact_rank, row["kind"]))
    if not matches:
        return "industry"
    return max(matches)[2]


def _source_text(source: Mapping[str, Any]) -> str:
    return " ".join(str(source.get(key) or "") for key in (
        "title", "publisher", "excerpt", "semantic_text",
    ))


def evaluate_evidence(
    employee: Mapping[str, Any], sources: Iterable[Mapping[str, Any]], *,
    config: EvidenceConfig | None = None, high_risk: bool = False,
) -> dict:
    config = config or load_default_config()
    role, industry, groups = _bound_role(employee, config)
    annotated: list[dict] = []
    seen_urls: set[str] = set()
    for raw_source in sources or []:
        if not isinstance(raw_source, Mapping):
            continue
        # ``url`` is the provider's redirect-verified final URL; persisted
        # rows expose the same value as ``canonical_url``.
        final_url = str(
            raw_source.get("final_url")
            or raw_source.get("canonical_url")
            or raw_source.get("url")
            or ""
        ).strip()
        try:
            parsed = urlsplit(final_url)
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if parsed.scheme != "https" or not host or final_url in seen_urls:
            continue
        seen_urls.add(final_url)
        text = _source_text(raw_source)
        industry_hits = [
            alias for alias in (*industry["aliases_zh"], *industry["aliases_en"])
            if alias_matches(text, alias)
        ]
        if not industry_hits:
            continue
        topic_rows: dict[str, dict[str, Any]] = {}
        for topic in role["topics"]:
            canonical = topic["canonical_topic"]
            anchors = groups[canonical]
            object_aliases = [
                *anchors["objects"],
                *(row["alias"] for row in topic["object_aliases_en"]),
            ]
            method_aliases = [
                *anchors["methods"],
                *(row["alias"] for row in topic["method_aliases_en"]),
            ]
            object_hits = sorted({
                alias for alias in object_aliases if alias_matches(text, alias)
            })
            method_hits = sorted({
                alias for alias in method_aliases if alias_matches(text, alias)
            })
            if not object_hits and not method_hits:
                continue
            topic_rows[canonical] = {
                "object_hits": object_hits,
                "method_hits": method_hits,
                "application": bool(object_hits),
                "method": bool(method_hits),
                "direct": bool(object_hits and method_hits),
            }
        if not topic_rows:
            continue
        authority_kind = authority_for_url(final_url, config=config)
        row = dict(raw_source)
        row.update({
            "final_url": final_url,
            "domain": host,
            # Explicitly overwrite anything asserted by a model/provider.
            "authority_level": authority_kind,
            "authority_kind": authority_kind,
            "semantic_industry_hits": sorted(set(industry_hits)),
            "semantic_topics": sorted(topic_rows),
            "evidence_topics": topic_rows,
        })
        annotated.append(row)

    counts = {
        "sources": len(annotated),
        "domains": len({row["domain"] for row in annotated}),
        "direct": sum(any(
            topic["direct"] for topic in row["evidence_topics"].values()
        ) for row in annotated),
        "application": sum(any(
            topic["application"] for topic in row["evidence_topics"].values()
        ) for row in annotated),
        "method_authority": sum(
            row["authority_kind"] in _AUTHORITATIVE_KINDS
            and any(topic["method"] for topic in row["evidence_topics"].values())
            for row in annotated
        ),
        "authoritative": sum(
            row["authority_kind"] in _AUTHORITATIVE_KINDS for row in annotated
        ),
        "official_authority": sum(
            row["authority_kind"] in _HIGH_RISK_OFFICIAL_KINDS for row in annotated
        ),
    }
    required_sources = 6 if high_risk else 5
    required_direct = 2 if high_risk else 1
    required_authority = 2 if high_risk else 1
    checks = (
        (counts["direct"] >= required_direct, "EVIDENCE_ZERO_DIRECT" if counts["direct"] == 0 else "EVIDENCE_DIRECT_INSUFFICIENT", "证据缺少同专题业务对象与专业方法的直接来源"),
        (counts["sources"] >= required_sources, "EVIDENCE_SOURCES_INSUFFICIENT", "证据来源数量不足"),
        (counts["domains"] >= 3, "EVIDENCE_DOMAINS_INSUFFICIENT", "证据来源域名不足"),
        (counts["application"] >= 2, "EVIDENCE_APPLICATION_INSUFFICIENT", "证据缺少业务应用来源"),
        (counts["authoritative"] >= required_authority, "EVIDENCE_AUTHORITY_INSUFFICIENT", "证据权威来源不足"),
        (counts["method_authority"] >= 1, "EVIDENCE_METHOD_AUTHORITY_INSUFFICIENT", "证据缺少权威专业方法来源"),
        (not high_risk or counts["official_authority"] >= 1, "EVIDENCE_OFFICIAL_INSUFFICIENT", "高风险岗位缺少监管、标准或官方来源"),
    )
    for passed, code, message in checks:
        if not passed:
            raise EvidenceGateError(code, message, counts=counts)
    return {
        "gate_digest": config.digest,
        "counts": counts,
        "sources": annotated,
    }


def validate_artifact_evidence(
    source_indexes: Iterable[int], annotated_sources: list[Mapping[str, Any]],
) -> str:
    try:
        indexes = sorted({int(value) for value in source_indexes})
    except (TypeError, ValueError) as exc:
        raise EvidenceGateError("EVIDENCE_ARTIFACT_LINK_INVALID", "产物来源索引无效") from exc
    if len(indexes) < 2 or any(
        value < 1 or value > len(annotated_sources) for value in indexes
    ):
        raise EvidenceGateError(
            "EVIDENCE_ARTIFACT_COMPLEMENT_MISSING", "产物缺少互补证据来源",
        )
    selected = [annotated_sources[index - 1] for index in indexes]
    direct_topics = {
        topic_name
        for source in selected
        for topic_name, topic in (source.get("evidence_topics") or {}).items()
        if topic.get("direct")
    }
    for topic_name in sorted(direct_topics):
        direct_sources = [
            source for source in selected
            if (source.get("evidence_topics") or {}).get(topic_name, {}).get("direct")
        ]
        complementary = [
            source for source in selected
            if source not in direct_sources
            and (
                (source.get("evidence_topics") or {}).get(topic_name, {}).get("application")
                or (source.get("evidence_topics") or {}).get(topic_name, {}).get("method")
            )
        ]
        # A second direct source is also independent complementary evidence.
        if complementary or len(direct_sources) >= 2:
            return topic_name
    raise EvidenceGateError(
        "EVIDENCE_ARTIFACT_COMPLEMENT_MISSING",
        "每项产物必须引用同专题直接来源与互补来源",
    )


__all__ = [
    "DEFAULT_PATH", "EvidenceConfig", "EvidenceConfigError", "EvidenceGateError",
    "alias_matches", "authority_for_url", "canonical_json", "canonical_sha256",
    "employee_public_contract", "evaluate_evidence", "load_config",
    "load_config_data", "load_default_config", "normalize_text", "search_aliases",
    "validate_artifact_evidence",
]
