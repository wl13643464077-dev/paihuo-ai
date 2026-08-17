#!/usr/bin/env python3
"""Independently audit the 360 current V3 industry roles.

This checker intentionally does not import either catalog generator.  It is the
second opinion used by QA and can therefore catch a generator which merely
changes business nouns while keeping one shared role skeleton.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sys
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
CATALOG_DIR = ROOT / "data" / "industry_decisions_v3"
EXPECTED_RANGES = {
    "tea_coffee": range(1001, 1037),
    "convenience": range(1101, 1137),
    "snack": range(1201, 1237),
    "grocery": range(1301, 1337),
    "pharmacy": range(1401, 1437),
    "hotel": range(1501, 1537),
    "auto": range(1601, 1637),
    "fitness": range(1701, 1737),
    "beauty": range(1801, 1837),
    "pet": range(1901, 1937),
}
GROUP_SIZES = [5, 5, 5, 5, 5, 4, 4, 3]
PROFILE_KEYS = {
    "scope", "decisions", "knowledge_domains", "data_objects",
    "tool_permissions", "skill_tree", "capabilities", "operating_rhythm",
    "escalation_matrix", "learning_tracks",
}
GENERIC_TOOLS = {
    "企业事实查询器", "证据版本追溯器", "通用数据平台", "业务系统",
}
GENERIC_WORKFLOW_MARKERS = (
    "冻结判断对象、门店范围、数据时点、批准责任与",
    "标明GO/HOLD/ESCALATE/ADVISE依据",
)
GENERIC_METRIC_MARKERS = (
    "能回链", "反证及人工结论", "人工复核可解释率",
    "批准截止前补齐证据并关闭", "到期关闭率",
)
GENERIC_METRIC_FORMULAS = (
    re.compile(
        r"^无需返工即通过.+的.+记录数\s*÷\s*同期进入.+的.+记录总数"
    ),
    re.compile(
        r"^在.+后完成.+且.+无未决冲突的事项数\s*÷\s*同期应完成.+的事项总数"
    ),
)
GENERIC_ESCALATION_MARKERS = (
    "时发现不可由岗位消除的重大影响",
    "触及企业书面红线",
    "仍无法对齐同一对象与时点，则停止当前判断",
    "停售、停产、资金、隐私或恢复红线",
    "授权内隔离影响",
)
GENERIC_LEARNING_PREFIXES = ("方法进修：", "案例进修：", "结果校准：")
REMOVABLE = re.compile(
    r"\d+|GO|HOLD|ESCALATE|ADVISE|人工审批|系统|岗位|员工|本企业|"
    r"证据|数据|建议|输出|复核|目录|行业阈值|分母为0时记N/A",
    re.I,
)


class Audit:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.stats: dict[str, dict] = {}

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.errors.append(message)


def _load_catalogs(audit: Audit) -> dict[str, dict]:
    rows = {}
    for path in sorted(CATALOG_DIR.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            audit.errors.append(f"{path.name}: invalid JSON: {exc}")
            continue
        if not isinstance(value, dict):
            audit.errors.append(f"{path.name}: top-level JSON must be an object")
            continue
        rows[path.stem] = value
    audit.require(set(rows) == set(EXPECTED_RANGES), "catalog set must be the ten V3 industries")
    return rows


def _canon(value: object, employee: dict, catalog: dict) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True,
    )
    tokens = [
        employee.get(field) for field in (
            "name", "key", "group", "role", "duty", "desc", "intro",
            "primary_decision", "person", "idx", "num",
        )
    ]
    tokens.extend((catalog.get("name"), catalog.get("industry"), catalog.get("key")))
    for token in sorted({str(token) for token in tokens if token}, key=len, reverse=True):
        text = text.replace(token, "")
    text = REMOVABLE.sub("", text)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text).lower()


def _pairwise_max(values: list[str]) -> tuple[float, tuple[int, int] | None]:
    maximum = 0.0
    pair = None
    for left in range(len(values)):
        for right in range(left + 1, len(values)):
            ratio = SequenceMatcher(None, values[left], values[right]).ratio()
            if ratio > maximum:
                maximum = ratio
                pair = (left, right)
    return maximum, pair


def _audit_role(audit: Audit, industry: str, employee: dict) -> None:
    idx = employee.get("idx")
    who = f"{industry}/{idx}"
    contract = employee.get("decision_contract")
    profile = employee.get("professional_profile")
    audit.require(isinstance(contract, dict), f"{who}: missing decision_contract")
    audit.require(isinstance(profile, dict), f"{who}: missing professional_profile")
    if not isinstance(contract, dict) or not isinstance(profile, dict):
        return
    audit.require(set(profile) == PROFILE_KEYS, f"{who}: profile key set is incomplete")
    list_minimums = {
        "decisions": 2, "knowledge_domains": 3, "data_objects": 3,
        "skill_tree": 5, "capabilities": 4, "learning_tracks": 3,
    }
    for field, minimum in list_minimums.items():
        values = profile.get(field)
        audit.require(
            isinstance(values, list) and len(values) >= minimum
            and len(values) == len(set(values))
            and all(isinstance(value, str) and value.strip() for value in values),
            f"{who}: {field} must have at least {minimum} unique strings",
        )
    audit.require(
        employee.get("primary_decision") == contract.get("decision")
        and employee.get("primary_decision") in (profile.get("decisions") or []),
        f"{who}: primary decision is not consistently bound",
    )
    tools = profile.get("tool_permissions")
    audit.require(
        isinstance(tools, list) and len(tools) >= 2
        and all(
            isinstance(row, dict) and set(row) == {"tool", "access", "scope"}
            and row.get("access") == "read_only"
            and str(row.get("tool") or "").strip()
            and str(row.get("scope") or "").strip()
            and row.get("tool") not in GENERIC_TOOLS
            for row in tools
        ),
        f"{who}: tools must be real role-specific read-only systems",
    )
    rhythm = profile.get("operating_rhythm")
    audit.require(
        isinstance(rhythm, dict)
        and set(rhythm) == {"daily", "event_driven", "review"}
        and all(str(value or "").strip() for value in rhythm.values()),
        f"{who}: operating rhythm is incomplete",
    )
    escalations = profile.get("escalation_matrix")
    audit.require(
        isinstance(escalations, list) and len(escalations) >= 2
        and all(
            isinstance(row, dict)
            and set(row) == {"level", "condition", "owner", "action"}
            and all(str(value or "").strip() for value in row.values())
            for row in escalations
        ),
        f"{who}: escalation matrix is incomplete",
    )
    if isinstance(escalations, list):
        for row in escalations:
            if isinstance(row, dict):
                audit.require(
                    not any(marker in str(row.get("condition") or "")
                            for marker in GENERIC_ESCALATION_MARKERS),
                    f"{who}: escalation condition is a field-substitution template",
                )
    audit.require(
        all(not value.startswith(GENERIC_LEARNING_PREFIXES)
            for value in profile.get("learning_tracks") or []),
        f"{who}: learning tracks use a generic three-part template",
    )
    workflow = contract.get("workflow")
    audit.require(
        isinstance(workflow, list)
        and sum(
            not any(marker in step for marker in GENERIC_WORKFLOW_MARKERS)
            for step in workflow if isinstance(step, str)
        ) >= 4,
        f"{who}: fewer than four real professional workflow steps",
    )
    metrics = contract.get("success_metrics")
    native_metrics = 0
    if isinstance(metrics, list):
        for metric in metrics:
            if isinstance(metric, dict):
                text = f"{metric.get('name', '')}\n{metric.get('formula', '')}"
                if not any(marker in text for marker in GENERIC_METRIC_MARKERS):
                    native_metrics += 1
                formula = str(metric.get("formula") or "").strip()
                audit.require(
                    not any(pattern.search(formula) for pattern in GENERIC_METRIC_FORMULAS),
                    f"{who}: metric formula is a skill/object substitution template",
                )
    audit.require(
        isinstance(metrics, list) and len(metrics) == 3 and native_metrics == 3,
        f"{who}: all three metrics must be native business metrics",
    )
    score = employee.get("priority_score")
    fields = ("pain_severity", "usage_frequency", "economic_value", "data_availability")
    audit.require(
        isinstance(score, dict) and set(score) == {*fields, "total"}
        and all(type(score.get(field)) is int and 1 <= score[field] <= 5 for field in fields)
        and score.get("total") == sum(score[field] for field in fields),
        f"{who}: priority score is invalid",
    )


def _audit_similarity(
    audit: Audit,
    industry: str,
    catalog: dict,
    field: str,
    getter: Callable[[dict], object],
) -> dict:
    employees = catalog.get("employees") or []
    values = [_canon(getter(employee), employee, catalog) for employee in employees]
    unique = len(set(values))
    maximum, pair = _pairwise_max(values)
    audit.require(unique == 36, f"{industry}/{field}: exact role skeleton duplicated")
    if pair is not None:
        left, right = pair
        audit.require(
            maximum < 0.86,
            f"{industry}/{field}: {employees[left].get('idx')} vs "
            f"{employees[right].get('idx')} similarity={maximum:.3f}",
        )
    return {"unique": unique, "max_similarity": round(maximum, 4)}


def _audit_cross_industry_similarity(
    audit: Audit,
    rows: list[tuple[str, dict, dict]],
    field: str,
    getter: Callable[[dict], object],
) -> dict:
    """Reject one shared role skeleton reused across different industries.

    Per-industry checks catch duplicates inside one catalog.  They cannot catch
    a generator that copies a tea role into a hotel role and changes only the
    employee/industry labels.  Canonicalize every row against its own identity
    tokens, require all 360 skeletons to be exact-unique, then compare only
    cross-industry pairs.  Cross-industry roles use the same strict 0.86
    ceiling as roles inside one industry; shared safety vocabulary does not
    justify a copied operating skeleton.
    """
    values = [
        _canon(getter(employee), employee, catalog)
        for _industry, catalog, employee in rows
    ]
    unique = len(set(values))
    audit.require(
        unique == len(rows),
        f"global/{field}: exact role skeleton duplicated across industries",
    )
    maximum = 0.0
    pair: tuple[int, int] | None = None
    for left in range(len(values)):
        for right in range(left + 1, len(values)):
            if rows[left][0] == rows[right][0]:
                continue
            ratio = SequenceMatcher(None, values[left], values[right]).ratio()
            if ratio > maximum:
                maximum = ratio
                pair = (left, right)
    if pair is not None:
        left, right = pair
        audit.require(
            maximum < 0.86,
            f"global/{field}: {rows[left][0]}/{rows[left][2].get('idx')} vs "
            f"{rows[right][0]}/{rows[right][2].get('idx')} "
            f"cross-industry similarity={maximum:.3f}",
        )
    return {"unique": unique, "max_similarity": round(maximum, 4)}


def run() -> tuple[dict, list[str]]:
    audit = Audit()
    catalogs = _load_catalogs(audit)
    all_rows: list[tuple[str, dict, dict]] = []
    global_fields: dict[str, list[object]] = defaultdict(list)
    field_getters: tuple[tuple[str, Callable[[dict], object]], ...] = (
        ("workflow", lambda e: e["decision_contract"]["workflow"]),
        ("outputs", lambda e: e["decision_contract"]["outputs"]),
        ("metrics", lambda e: [
            {key: metric[key] for key in ("name", "formula", "window", "source")}
            for metric in e["decision_contract"]["success_metrics"]
        ]),
        ("selection_rationale", lambda e: e["selection_rationale"]),
        ("scope", lambda e: e["professional_profile"]["scope"]),
        ("decisions", lambda e: e["professional_profile"]["decisions"]),
        ("knowledge", lambda e: e["professional_profile"]["knowledge_domains"]),
        ("objects", lambda e: e["professional_profile"]["data_objects"]),
        ("tools", lambda e: e["professional_profile"]["tool_permissions"]),
        ("skills", lambda e: e["professional_profile"]["skill_tree"]),
        ("capabilities", lambda e: e["professional_profile"]["capabilities"]),
        ("rhythm", lambda e: e["professional_profile"]["operating_rhythm"]),
        ("escalation", lambda e: e["professional_profile"]["escalation_matrix"]),
        ("learning", lambda e: e["professional_profile"]["learning_tracks"]),
    )
    for industry, catalog in catalogs.items():
        employees = catalog.get("employees") or []
        audit.require(catalog.get("catalog_version") == "2026.08.v3", f"{industry}: wrong V3 version")
        audit.require(len(catalog.get("pain_points") or []) == 8, f"{industry}: needs eight pain clusters")
        audit.require(len(employees) == 36, f"{industry}: needs 36 current employees")
        audit.require(
            [employee.get("idx") for employee in employees]
            == list(EXPECTED_RANGES[industry]),
            f"{industry}: original employee ids were not preserved",
        )
        audit.require(
            [len(group.get("members") or []) for group in catalog.get("groups") or []]
            == GROUP_SIZES,
            f"{industry}: group sizes must be {GROUP_SIZES}",
        )
        for employee in employees:
            _audit_role(audit, industry, employee)
            all_rows.append((industry, catalog, employee))
            for field in ("idx", "key", "name", "primary_decision"):
                global_fields[field].append(employee.get(field))
        ranks = sorted(
            employees,
            key=lambda employee: (
                -employee["priority_score"]["total"],
                -employee["priority_score"]["usage_frequency"],
                -employee["priority_score"]["economic_value"],
                -employee["priority_score"]["pain_severity"],
                -employee["priority_score"]["data_availability"],
                employee["idx"],
            ),
        ) if len(employees) == 36 and all(isinstance(e.get("priority_score"), dict) for e in employees) else []
        audit.require(
            len(ranks) == 36 and [employee.get("priority_rank") for employee in ranks] == list(range(1, 37)),
            f"{industry}: priority ranks do not follow business value",
        )
        stats = {
            "employees": len(employees),
            "pain_clusters": len(catalog.get("pain_points") or []),
            "fields": {},
        }
        if all(isinstance(e.get("professional_profile"), dict) for e in employees):
            for field, getter in field_getters:
                stats["fields"][field] = _audit_similarity(
                    audit, industry, catalog, field, getter,
                )
        audit.stats[industry] = stats
    for field, values in global_fields.items():
        audit.require(
            len(values) == 360 and len(set(values)) == 360,
            f"global {field} values must be unique across all 360 employees",
        )
    for label, getter in (
        ("key", lambda metric: metric["key"]),
        ("name", lambda metric: metric["name"]),
        ("formula", lambda metric: metric["formula"]),
    ):
        values = [
            getter(metric)
            for _industry, _catalog, employee in all_rows
            for metric in employee.get("decision_contract", {}).get("success_metrics", [])
        ]
        audit.require(
            len(values) == 1080 and len(values) == len(set(values)),
            f"global metric {label} values must be unique across all 1080 metrics",
        )
    global_similarity = {}
    if len(all_rows) == 360 and all(
        isinstance(employee.get("professional_profile"), dict)
        and isinstance(employee.get("decision_contract"), dict)
        for _industry, _catalog, employee in all_rows
    ):
        for field, getter in field_getters:
            global_similarity[field] = _audit_cross_industry_similarity(
                audit, all_rows, field, getter,
            )
    report = {
        "catalogs": len(catalogs),
        "employees": len(all_rows),
        "expected_active_industry_employees": 420,
        "expected_active_total_with_core": 431,
        "industries": audit.stats,
        "global_similarity": global_similarity,
        "passed": not audit.errors,
        "error_count": len(audit.errors),
    }
    return report, audit.errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()
    report, errors = run()
    if args.json:
        print(json.dumps({**report, "errors": errors}, ensure_ascii=False, indent=2))
    else:
        print(
            f"V3 audit: catalogs={report['catalogs']} employees={report['employees']} "
            f"errors={report['error_count']}"
        )
        for industry, row in report["industries"].items():
            maximum = max(
                (value["max_similarity"] for value in row["fields"].values()),
                default=0.0,
            )
            print(
                f"- {industry}: employees={row['employees']} pains={row['pain_clusters']} "
                f"max_similarity={maximum:.4f}"
            )
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
