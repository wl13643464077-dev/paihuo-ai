#!/usr/bin/env python3
"""Generate and verify the immutable multilingual learning-evidence sidecar.

Translations remain an independently reviewed seed.  This generator copies no
private identity material: it binds translations to the immutable V4 public
research contract through public employee keys and canonical SHA-256 digests.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy import preflight  # noqa: E402


DEFAULT_CATALOG_DIR = ROOT / "data" / "industry_decisions_v4"
DEFAULT_SEED = ROOT / "data" / "learning_evidence_gate_v1.seed.json"
DEFAULT_OUTPUT = ROOT / "app" / "learning_evidence_gate_v1.json"

_SEED_TOP_KEYS = {
    "schema", "catalog_version", "source_catalog_sha256",
    "authority_policy_sha256", "industry_aliases", "employees",
    "authority_registry",
}


class EvidenceGateGenerationError(RuntimeError):
    pass


def _read_seed(path: Path) -> dict:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise EvidenceGateGenerationError(
            f"learning evidence seed missing: {path}"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or not 0 < metadata.st_size <= 16 * 1024 * 1024
    ):
        raise EvidenceGateGenerationError(
            f"learning evidence seed must be a bounded regular file: {path}"
        )
    try:
        seed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceGateGenerationError(
            f"learning evidence seed is not valid JSON: {path}"
        ) from exc
    if not isinstance(seed, dict) or not set(seed) <= _SEED_TOP_KEYS:
        raise EvidenceGateGenerationError(
            "learning evidence seed top-level shape is invalid"
        )
    required = {
        "schema", "catalog_version", "industry_aliases", "employees",
        "authority_registry",
    }
    if not required <= set(seed):
        raise EvidenceGateGenerationError(
            "learning evidence seed is missing a required field"
        )
    return seed


def _accept_or_fill_digest(container: dict, key: str, expected: str) -> None:
    supplied = container.get(key)
    if supplied not in (None, expected):
        raise EvidenceGateGenerationError(
            f"learning evidence seed {key} does not match the V4 contract"
        )
    container[key] = expected


def _canonical_alias_rows(rows, *, label: str) -> list[dict]:
    if not isinstance(rows, list):
        raise EvidenceGateGenerationError(f"{label} must be a list")
    result = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"alias", "source_anchor"}
        ):
            raise EvidenceGateGenerationError(
                f"{label} entry shape is invalid"
            )
        result.append({
            "alias": row.get("alias"),
            "source_anchor": row.get("source_anchor"),
        })
    return result


def build_sidecar(
    *,
    catalog_dir: Path,
    seed_path: Path,
    expected_industries: int = 10,
    expected_roles: int = 360,
    expected_topics: int = 1800,
) -> dict:
    contract = preflight._learning_evidence_public_contract(
        catalog_dir,
        expected_industries=expected_industries,
        expected_roles=expected_roles,
        expected_topics=expected_topics,
    )
    seed = copy.deepcopy(_read_seed(seed_path))
    if (
        seed.get("schema") != preflight._LEARNING_EVIDENCE_SCHEMA
        or seed.get("catalog_version")
        != preflight._LEARNING_EVIDENCE_CATALOG_VERSION
    ):
        raise EvidenceGateGenerationError(
            "learning evidence seed schema or catalog version is invalid"
        )
    _accept_or_fill_digest(
        seed, "source_catalog_sha256", contract["catalog_digest"]
    )

    registry = seed.get("authority_registry")
    authority_digest = preflight._validate_learning_evidence_authorities(
        registry
    )
    _accept_or_fill_digest(
        seed, "authority_policy_sha256", authority_digest
    )

    raw_industries = seed.get("industry_aliases")
    if not isinstance(raw_industries, list):
        raise EvidenceGateGenerationError(
            "learning evidence seed industry_aliases must be a list"
        )
    industries_by_key: dict[str, dict] = {}
    for row in raw_industries:
        if (
            not isinstance(row, dict)
            or set(row) != {"industry_key", "aliases_zh", "aliases_en"}
        ):
            raise EvidenceGateGenerationError(
                "learning evidence seed industry alias shape is invalid"
            )
        industry_key = str(row.get("industry_key") or "").strip()
        if not industry_key or industry_key in industries_by_key:
            raise EvidenceGateGenerationError(
                "learning evidence seed industry key is missing or duplicate"
            )
        industries_by_key[industry_key] = {
            "industry_key": industry_key,
            "aliases_zh": row.get("aliases_zh"),
            "aliases_en": row.get("aliases_en"),
        }

    raw_employees = seed.get("employees")
    if not isinstance(raw_employees, list):
        raise EvidenceGateGenerationError(
            "learning evidence seed employees must be a list"
        )
    employees_by_key: dict[str, dict] = {}
    for row in raw_employees:
        if not isinstance(row, dict):
            raise EvidenceGateGenerationError(
                "learning evidence seed employee must be an object"
            )
        allowed = preflight._LEARNING_EVIDENCE_EMPLOYEE_KEYS
        required = {"employee_key", "industry_key", "job_label_en", "topics"}
        if not required <= set(row) <= allowed:
            raise EvidenceGateGenerationError(
                "learning evidence seed employee shape is invalid"
            )
        employee_key = str(row.get("employee_key") or "").strip()
        source = contract["roles_by_key"].get(employee_key)
        if source is None or employee_key in employees_by_key:
            raise EvidenceGateGenerationError(
                "learning evidence seed employee key is unknown or duplicate"
            )
        source_digest = source["role_digest"]
        _accept_or_fill_digest(
            row, "source_public_contract_sha256", source_digest
        )
        raw_topics = row.get("topics")
        if not isinstance(raw_topics, list):
            raise EvidenceGateGenerationError(
                f"learning evidence seed topics must be a list: {employee_key}"
            )
        topics_by_name: dict[str, dict] = {}
        for topic in raw_topics:
            required_topic = {
                "topic_id", "canonical_topic", "label_en",
                "object_aliases_en", "method_aliases_en",
            }
            if (
                not isinstance(topic, dict)
                or not required_topic <= set(topic)
                <= preflight._LEARNING_EVIDENCE_TOPIC_KEYS
            ):
                raise EvidenceGateGenerationError(
                    f"learning evidence seed topic shape is invalid: {employee_key}"
                )
            canonical_topic = topic.get("canonical_topic")
            if (
                not isinstance(canonical_topic, str)
                or canonical_topic not in source["groups"]
                or canonical_topic in topics_by_name
            ):
                raise EvidenceGateGenerationError(
                    f"learning evidence seed topic is unknown or duplicate: {employee_key}"
                )
            _accept_or_fill_digest(
                topic,
                "canonical_topic_sha256",
                preflight._learning_evidence_sha256(canonical_topic),
            )
            topics_by_name[canonical_topic] = {
                "topic_id": topic.get("topic_id"),
                "canonical_topic": canonical_topic,
                "canonical_topic_sha256": topic["canonical_topic_sha256"],
                "label_en": topic.get("label_en"),
                "object_aliases_en": _canonical_alias_rows(
                    topic.get("object_aliases_en"),
                    label=f"object aliases for {employee_key}",
                ),
                "method_aliases_en": _canonical_alias_rows(
                    topic.get("method_aliases_en"),
                    label=f"method aliases for {employee_key}",
                ),
            }
        employees_by_key[employee_key] = {
            "employee_key": employee_key,
            "industry_key": row.get("industry_key"),
            "source_public_contract_sha256": row[
                "source_public_contract_sha256"
            ],
            "job_label_en": row.get("job_label_en"),
            "topics": [
                topics_by_name[topic]
                for topic in source["ordered_group_topics"]
                if topic in topics_by_name
            ],
        }

    return {
        "schema": preflight._LEARNING_EVIDENCE_SCHEMA,
        "catalog_version": preflight._LEARNING_EVIDENCE_CATALOG_VERSION,
        "source_catalog_sha256": seed["source_catalog_sha256"],
        "authority_policy_sha256": seed["authority_policy_sha256"],
        "industry_aliases": [
            industries_by_key[key] for key in sorted(industries_by_key)
        ],
        "employees": [
            employees_by_key[key]
            for key in sorted(
                employees_by_key,
                key=lambda value: (
                    contract["roles_by_key"][value]["industry_key"], value
                ),
            )
        ],
        "authority_registry": registry,
    }


def render_sidecar(sidecar: dict) -> bytes:
    return (
        json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _validate_rendered(
    body: bytes,
    *,
    catalog_dir: Path,
    output_parent: Path,
    expected_industries: int,
    expected_roles: int,
    expected_topics: int,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".learning-evidence-gate-",
        suffix=".json",
        dir=output_parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        preflight._validate_learning_evidence_gate(
            temporary,
            catalog_dir,
            expected_industries=expected_industries,
            expected_roles=expected_roles,
            expected_topics=expected_topics,
        )
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-industries", type=int, default=10)
    parser.add_argument("--expected-roles", type=int, default=360)
    parser.add_argument("--expected-topics", type=int, default=1800)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify canonical bytes without writing",
    )
    args = parser.parse_args(argv)
    if any(
        value <= 0
        for value in (
            args.expected_industries,
            args.expected_roles,
            args.expected_topics,
        )
    ):
        parser.error("expected coverage counts must be positive")
    try:
        sidecar = build_sidecar(
            catalog_dir=args.catalog_dir,
            seed_path=args.seed,
            expected_industries=args.expected_industries,
            expected_roles=args.expected_roles,
            expected_topics=args.expected_topics,
        )
        body = render_sidecar(sidecar)
        if args.check:
            if not args.output.is_file() or args.output.is_symlink():
                raise EvidenceGateGenerationError(
                    f"learning evidence sidecar missing: {args.output}"
                )
            if args.output.read_bytes() != body:
                raise EvidenceGateGenerationError(
                    "learning evidence sidecar byte drift; regenerate it"
                )
            preflight._validate_learning_evidence_gate(
                args.output,
                args.catalog_dir,
                expected_industries=args.expected_industries,
                expected_roles=args.expected_roles,
                expected_topics=args.expected_topics,
            )
            print(
                "OK: learning evidence sidecar byte-stable; "
                f"{args.expected_roles} roles / {args.expected_topics} topics"
            )
            return 0
        args.output.parent.mkdir(parents=True, exist_ok=True)
        _validate_rendered(
            body,
            catalog_dir=args.catalog_dir,
            output_parent=args.output.parent,
            expected_industries=args.expected_industries,
            expected_roles=args.expected_roles,
            expected_topics=args.expected_topics,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{args.output.name}.",
            dir=args.output.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o644)
            os.replace(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)
        print(
            f"WROTE: {args.output}; {args.expected_roles} roles / "
            f"{args.expected_topics} topics"
        )
        return 0
    except (
        EvidenceGateGenerationError,
        preflight.PreflightError,
        OSError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
