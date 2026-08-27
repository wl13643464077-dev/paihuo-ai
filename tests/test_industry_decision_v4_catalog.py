from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from deploy import preflight


ROOT = Path(__file__).resolve().parents[1]
V3_DIR = ROOT / "data" / "industry_decisions_v3"
V4_DIR = ROOT / "data" / "industry_decisions_v4"
GENERATOR = ROOT / "tools" / "generate_industry_decisions_v4.py"
EVIDENCE_GATE_GENERATOR = ROOT / "tools" / "generate_learning_evidence_gate.py"
EVIDENCE_GATE_SEED = ROOT / "data" / "learning_evidence_gate_v1.seed.json"
EVIDENCE_GATE_SIDECAR = ROOT / "app" / "learning_evidence_gate_v1.json"

# This is the byte-level baseline for the already-reviewed V3 release.  The V4
# generator is deliberately not allowed to rewrite or normalize V3 artifacts.
V3_SHA256 = {
    "auto.json": "9bf03098b786b8dcaac82117a888d9d75fb6efb433cc10774d21abc9c57b70f6",
    "beauty.json": "51ec95d564df7865bfc43caf852415bb9b83444f9c3247b306aad88077c71c69",
    "convenience.json": "4a5b10cd89549d072cc600e3fda8853d0d401ad65d9c69ae25bb4bfd645d6802",
    "fitness.json": "4fc22e99c04590aa81a6ca2401e7d2f9ba08ce13717f2dea5b50eea25569b215",
    "grocery.json": "9f45022e57f6cd8660615922b8e02a67bfff9d2a3c81c2c22700539bab5d7266",
    "hotel.json": "1537fdce7b00ec6796807317d438f74480e26a415a9dbc41f7e2a16393d3e02b",
    "pet.json": "687ded5855298d6055da13327c29bcc935a48698467d293a71f28c4ccbee443a",
    "pharmacy.json": "db7d7b6bebcb93295d2509447c93fb7c70621490074c342bec1af547cc31bf93",
    "snack.json": "b8ea41241de1702791ccf8e2c1cbfdca9c04be47dd665154934b1400b53ba16a",
    "tea_coffee.json": "c3604e168ebb0ccf6bf5b2263ced418de89093f23c5cc478d653159a7835e7a5",
}

# These are intentionally broad: a person display name must not be a role,
# store/location/business noun with a suffix that makes the roster look templated.
FORBIDDEN_PERSON_TOKENS = (
    "市场",
    "商圈",
    "客群",
    "店型",
    "选址",
    "商品",
    "成本",
    "标准",
    "新品",
    "组合",
    "需求",
    "供应",
    "采购",
    "库存",
    "损耗",
    "门店",
    "新店",
    "产能",
    "质量",
    "投诉",
    "本地",
    "社媒",
    "评价",
    "会员",
    "促销",
    "组织",
    "员工",
    "技能",
    "绩效",
    "日结",
    "单店",
    "KP",
    "多店",
    "法规",
    "隐私",
    "业务",
)


def _catalogs(directory: Path) -> list[tuple[Path, dict]]:
    return [(path, json.loads(path.read_text(encoding="utf-8"))) for path in sorted(directory.glob("*.json"))]


def _learning_evidence_fixture(base: Path) -> tuple[Path, Path, Path]:
    catalog_dir = base / "industry_decisions_v4"
    catalog_dir.mkdir()
    catalog = {
        "key": "auto",
        "catalog_version": "2026.08.v4",
        "employees": [{
            "idx": 1601,
            "key": "auto-v4-1601",
            "name": "VIN技术资料版本审查官",
            "person": "Alice",
            "public_research_topics": [
                "VIN技术资料版本审查官",
                "召回状态核验",
            ],
            "public_research_anchor_groups": [{
                "topic": "召回状态核验",
                "object_anchors": [
                    "VIN车辆识别码", "制造商召回公告", "召回车辆档案",
                ],
                "method_anchors": ["召回状态核验"],
            }],
        }],
    }
    (catalog_dir / "auto.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    seed = {
        "schema": "learning-evidence-gate-v1",
        "catalog_version": "2026.08.v4",
        "industry_aliases": [{
            "industry_key": "auto",
            "aliases_zh": ["汽车后市场"],
            "aliases_en": ["automotive aftermarket"],
        }],
        "employees": [{
            "employee_key": "auto-v4-1601",
            "industry_key": "auto",
            "job_label_en": "VIN Technical Information Version Reviewer",
            "topics": [{
                "topic_id": "t01",
                "canonical_topic": "召回状态核验",
                "label_en": "Recall Status Verification",
                "object_aliases_en": [{
                    "alias": "vehicle identification number",
                    "source_anchor": "VIN车辆识别码",
                }, {
                    "alias": "manufacturer recall bulletin",
                    "source_anchor": "制造商召回公告",
                }, {
                    "alias": "recall vehicle record",
                    "source_anchor": "召回车辆档案",
                }],
                "method_aliases_en": [{
                    "alias": "recall status verification",
                    "source_anchor": "召回状态核验",
                }, {
                    "alias": "recall eligibility validation",
                    "source_anchor": "召回状态核验",
                }, {
                    "alias": "recall coverage confirmation",
                    "source_anchor": "召回状态核验",
                }],
            }],
        }],
        "authority_registry": [{
            "host": "www.nhtsa.gov",
            "match": "exact",
            "kind": "regulator",
        }, {
            "host": "standards.iso.org",
            "match": "suffix",
            "kind": "standard",
        }],
    }
    seed_path = base / "learning_evidence_gate_v1.seed.json"
    seed_path.write_text(
        json.dumps(seed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return catalog_dir, seed_path, base / "learning_evidence_gate_v1.json"


def _run_evidence_gate_generator(
    catalog_dir: Path,
    seed: Path,
    output: Path,
    *,
    check: bool = False,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(EVIDENCE_GATE_GENERATOR),
        "--catalog-dir", str(catalog_dir),
        "--seed", str(seed),
        "--output", str(output),
        "--expected-industries", "1",
        "--expected-roles", "1",
        "--expected-topics", "1",
    ]
    if check:
        command.append("--check")
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_v4_catalog_is_complete_and_versioned():
    catalogs = _catalogs(V4_DIR)
    assert len(catalogs) == 10
    assert {data["catalog_version"] for _, data in catalogs} == {"2026.08.v4"}
    assert all(len(data["pain_points"]) == 8 for _, data in catalogs)
    assert all(len(data["employees"]) == 36 for _, data in catalogs)

    employees = [employee for _, data in catalogs for employee in data["employees"]]
    assert len(employees) == 360
    assert len({employee["idx"] for employee in employees}) == 360
    assert len({employee["person"] for employee in employees}) == 360
    assert len({employee["name"] for employee in employees}) >= 200
    assert len({employee["key"] for employee in employees}) == 360
    assert all("v4" in employee["key"] for employee in employees)
    assert all(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", employee["person"]) for employee in employees)
    assert all(not any(token in employee["person"] for token in FORBIDDEN_PERSON_TOKENS) for employee in employees)
    assert all(3 <= len(employee["public_research_topics"]) <= 6 for employee in employees)
    assert all(employee["name"] in employee["public_research_topics"] for employee in employees)
    assert all(employee["person"] not in employee["public_research_topics"] for employee in employees)
    assert all(
        len(set(employee["public_research_topics"]))
        == len(employee["public_research_topics"])
        for employee in employees
    )
    for employee in employees:
        groups = employee["public_research_anchor_groups"]
        assert {group["topic"] for group in groups} == (
            set(employee["public_research_topics"]) - {employee["name"]}
        )
        for group in groups:
            objects = group["object_anchors"]
            methods = group["method_anchors"]
            assert 2 <= len(objects) <= 24
            assert 1 <= len(methods) <= 12
            assert len(objects) == len(set(objects))
            assert len(methods) == len(set(methods))
            assert not any(
                obj.lower() in method.lower() or method.lower() in obj.lower()
                for obj in objects for method in methods
            )

    old_by_idx = {
        employee["idx"]: employee
        for _, data in _catalogs(V3_DIR)
        for employee in data["employees"]
    }
    assert all(employee["person"] != old_by_idx[employee["idx"]]["person"] for employee in employees)
    assert {employee["idx"] for employee in employees} == set(old_by_idx)

    registry = [entry for _, data in catalogs for entry in data["name_registry"]]
    assert len(registry) == 360
    assert {entry["idx"] for entry in registry} == {employee["idx"] for employee in employees}
    assert {entry["person"] for entry in registry} == {employee["person"] for employee in employees}
    assert all(entry["synthetic"] is True for entry in registry)
    assert all(entry["source_version"] == "2026.08.v4" for entry in registry)
    assert all(entry.get("reviewed") == "machine_review_pending" for entry in registry)
    assert all("reviewed_by" not in entry for entry in registry)
    assert all(re.fullmatch(r"[a-z]+(?:-[a-z]+)+", entry["canonical_pinyin"]) for entry in registry)


def test_v4_carries_complete_professional_contracts():
    required_profile = {
        "scope",
        "decisions",
        "knowledge_domains",
        "data_objects",
        "tool_permissions",
        "skill_tree",
        "capabilities",
        "operating_rhythm",
        "escalation_matrix",
        "learning_tracks",
    }
    required_contract = {
        "decision",
        "decision_states",
        "triggers",
        "required_inputs",
        "evidence_required",
        "workflow",
        "outputs",
        "success_metrics",
        "approval_boundary",
        "forbidden_actions",
        "fallback",
        "requires_human_approval",
        "allowed_side_effects",
        "go_semantics",
    }
    for _, data in _catalogs(V4_DIR):
        for employee in data["employees"]:
            profile = employee["professional_profile"]
            contract = employee["decision_contract"]
            assert required_profile <= profile.keys()
            assert required_contract <= contract.keys()
            assert all(profile[key] for key in required_profile)
            assert all(contract[key] or contract[key] == [] for key in required_contract)
            assert employee["pain_codes"]


def test_generator_check_is_byte_stable_and_preserves_v3():
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(V3_DIR.glob("*.json"))
    }
    assert before == V3_SHA256
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(V3_DIR.glob("*.json"))
    }
    assert after == before == V3_SHA256


def test_release_preflight_rejects_missing_public_research_topics():
    legacy_ids = {
        int(employee["idx"])
        for _, catalog in _catalogs(V3_DIR)
        for employee in catalog["employees"]
    }
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "industry_decisions_v4"
        shutil.copytree(V4_DIR, target)
        path = target / "tea_coffee.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        catalog["employees"][0].pop("public_research_topics", None)
        path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with unittest.TestCase().assertRaisesRegex(
            preflight.PreflightError, "public research topics",
        ):
            preflight._validate_industry_decision_configs(
                target,
                legacy_employee_ids=legacy_ids,
                catalog_version="2026.08.v4",
                expected_count=36,
                allow_original_ids=True,
            )


def test_release_preflight_accepts_current_v4_catalogs():
    legacy_ids = {
        int(employee["idx"])
        for _, catalog in _catalogs(V3_DIR)
        for employee in catalog["employees"]
    }
    result = preflight._validate_industry_decision_configs(
        V4_DIR,
        legacy_employee_ids=legacy_ids,
        catalog_version="2026.08.v4",
        expected_count=36,
        allow_original_ids=True,
    )
    assert result == {"files": 10, "employees": 360}


def test_release_preflight_rejects_overlapping_public_research_anchors():
    legacy_ids = {
        int(employee["idx"])
        for _, catalog in _catalogs(V3_DIR)
        for employee in catalog["employees"]
    }
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "industry_decisions_v4"
        shutil.copytree(V4_DIR, target)
        path = target / "tea_coffee.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        group = catalog["employees"][0]["public_research_anchor_groups"][0]
        group["object_anchors"][0] = group["method_anchors"][0]
        path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with unittest.TestCase().assertRaisesRegex(
            preflight.PreflightError, "public research anchor groups",
        ):
            preflight._validate_industry_decision_configs(
                target,
                legacy_employee_ids=legacy_ids,
                catalog_version="2026.08.v4",
                expected_count=36,
                allow_original_ids=True,
            )


def test_release_preflight_rejects_identity_material_in_public_research_anchors():
    legacy_ids = {
        int(employee["idx"])
        for _, catalog in _catalogs(V3_DIR)
        for employee in catalog["employees"]
    }
    for secret in ("1001", "a" * 64):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "industry_decisions_v4"
            shutil.copytree(V4_DIR, target)
            path = target / "tea_coffee.json"
            catalog = json.loads(path.read_text(encoding="utf-8"))
            catalog["employees"][0]["public_research_anchor_groups"][0][
                "object_anchors"
            ][0] = f"公开对象{secret}"
            path.write_text(
                json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with unittest.TestCase().assertRaisesRegex(
                preflight.PreflightError, "public research anchor groups",
            ):
                preflight._validate_industry_decision_configs(
                    target,
                    legacy_employee_ids=legacy_ids,
                    catalog_version="2026.08.v4",
                    expected_count=36,
                    allow_original_ids=True,
                )


def test_learning_evidence_sidecar_fixture_is_canonical_and_byte_stable():
    with tempfile.TemporaryDirectory() as tmp:
        catalog_dir, seed, output = _learning_evidence_fixture(Path(tmp))
        generated = _run_evidence_gate_generator(catalog_dir, seed, output)
        assert generated.returncode == 0, generated.stdout + generated.stderr
        before = output.read_bytes()
        checked = _run_evidence_gate_generator(
            catalog_dir, seed, output, check=True,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert output.read_bytes() == before
        payload = json.loads(before.decode("utf-8"))
        assert "person" not in payload["employees"][0]
        assert "idx" not in payload["employees"][0]
        assert "identity_ref" not in before.decode("utf-8")
        assert re.fullmatch(
            r"[0-9a-f]{64}", payload["source_catalog_sha256"]
        )
        assert re.fullmatch(
            r"[0-9a-f]{64}",
            payload["employees"][0]["source_public_contract_sha256"],
        )
        assert re.fullmatch(
            r"[0-9a-f]{64}",
            payload["employees"][0]["topics"][0][
                "canonical_topic_sha256"
            ],
        )
        report = preflight._validate_learning_evidence_gate(
            output,
            catalog_dir,
            expected_industries=1,
            expected_roles=1,
            expected_topics=1,
        )
        assert report["industries"] == 1
        assert report["roles"] == 1
        assert report["topics"] == 1


def test_learning_evidence_sidecar_rejects_unsourced_or_unsafe_aliases():
    cases = {
        "wrong_source_anchor": (
            ("employees", 0, "topics", 0, "object_aliases_en", 0,
             "source_anchor"),
            "未冻结对象",
        ),
        "person": (
            ("employees", 0, "topics", 0, "object_aliases_en", 0, "alias"),
            "Alice vehicle record",
        ),
        "idx": (
            ("employees", 0, "topics", 0, "object_aliases_en", 0, "alias"),
            "vehicle record 1601",
        ),
        "digest": (
            ("employees", 0, "topics", 0, "object_aliases_en", 0, "alias"),
            "a" * 64,
        ),
        "control": (
            ("employees", 0, "topics", 0, "object_aliases_en", 0, "alias"),
            "vehicle\nrecord",
        ),
        "generic_only": (
            ("employees", 0, "topics", 0, "object_aliases_en", 0, "alias"),
            "data analysis",
        ),
    }
    for name, (path, value) in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            catalog_dir, seed_path, output = _learning_evidence_fixture(
                Path(tmp)
            )
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
            target = seed
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            seed_path.write_text(
                json.dumps(seed, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            result = _run_evidence_gate_generator(
                catalog_dir, seed_path, output,
            )
            assert result.returncode != 0, name


def test_learning_evidence_sidecar_rejects_nfkc_overlap_and_unsafe_suffix():
    mutations = (
        "nfkc_duplicate", "casefold_duplicate", "object_method_overlap",
        "underfilled", "unsafe_suffix",
    )
    for mutation in mutations:
        with tempfile.TemporaryDirectory() as tmp:
            catalog_dir, seed_path, output = _learning_evidence_fixture(
                Path(tmp)
            )
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
            topic = seed["employees"][0]["topics"][0]
            if mutation == "nfkc_duplicate":
                topic["object_aliases_en"].append({
                    "alias": "ｖｅｈｉｃｌｅ identification number",
                    "source_anchor": "制造商召回公告",
                })
            elif mutation == "casefold_duplicate":
                topic["object_aliases_en"].append({
                    "alias": "Vehicle Identification Number",
                    "source_anchor": "制造商召回公告",
                })
            elif mutation == "object_method_overlap":
                topic["method_aliases_en"][0]["alias"] = (
                    "Vehicle Identification Number Check"
                )
            elif mutation == "underfilled":
                topic["method_aliases_en"] = topic["method_aliases_en"][:2]
            else:
                seed["authority_registry"][1]["host"] = "gov.cn"
            seed_path.write_text(
                json.dumps(seed, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            result = _run_evidence_gate_generator(
                catalog_dir, seed_path, output,
            )
            assert result.returncode != 0, mutation


def test_learning_evidence_sidecar_is_bound_to_the_canonical_v4_contract():
    with tempfile.TemporaryDirectory() as tmp:
        catalog_dir, seed, output = _learning_evidence_fixture(Path(tmp))
        generated = _run_evidence_gate_generator(catalog_dir, seed, output)
        assert generated.returncode == 0, generated.stdout + generated.stderr
        catalog_path = catalog_dir / "auto.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["employees"][0]["public_research_anchor_groups"][0][
            "object_anchors"
        ][0] = "被篡改的公开对象"
        catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with unittest.TestCase().assertRaisesRegex(
            preflight.PreflightError,
            "source catalog digest drifted",
        ):
            preflight._validate_learning_evidence_gate(
                output,
                catalog_dir,
                expected_industries=1,
                expected_roles=1,
                expected_topics=1,
            )


def test_production_learning_evidence_sidecar_covers_exact_v4_contract():
    checked = subprocess.run(
        [sys.executable, str(EVIDENCE_GATE_GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    report = preflight._validate_learning_evidence_gate(
        EVIDENCE_GATE_SIDECAR,
        V4_DIR,
    )
    assert report["industries"] == 10
    assert report["roles"] == 360
    assert report["topics"] == 1800


class IndustryDecisionV4CatalogTests(unittest.TestCase):
    """Keep the catalog gate runnable in the repository's stdlib-only smoke lane."""

    def test_complete_and_versioned(self):
        test_v4_catalog_is_complete_and_versioned()

    def test_complete_professional_contracts(self):
        test_v4_carries_complete_professional_contracts()

    def test_generator_check_and_v3_stability(self):
        test_generator_check_is_byte_stable_and_preserves_v3()

    def test_release_preflight_requires_public_research_topics(self):
        test_release_preflight_rejects_missing_public_research_topics()

    def test_release_preflight_accepts_current_v4_catalogs(self):
        test_release_preflight_accepts_current_v4_catalogs()

    def test_release_preflight_rejects_overlapping_public_research_anchors(self):
        test_release_preflight_rejects_overlapping_public_research_anchors()

    def test_release_preflight_rejects_identity_material_in_public_research_anchors(self):
        test_release_preflight_rejects_identity_material_in_public_research_anchors()

    def test_learning_evidence_sidecar_fixture_contract_and_stability(self):
        test_learning_evidence_sidecar_fixture_is_canonical_and_byte_stable()

    def test_learning_evidence_sidecar_rejects_unsourced_or_unsafe_aliases(self):
        test_learning_evidence_sidecar_rejects_unsourced_or_unsafe_aliases()

    def test_learning_evidence_sidecar_rejects_nfkc_overlap_and_unsafe_suffix(self):
        test_learning_evidence_sidecar_rejects_nfkc_overlap_and_unsafe_suffix()

    def test_learning_evidence_sidecar_is_bound_to_canonical_v4(self):
        test_learning_evidence_sidecar_is_bound_to_the_canonical_v4_contract()

    def test_production_learning_evidence_sidecar_covers_exact_v4_contract(self):
        test_production_learning_evidence_sidecar_covers_exact_v4_contract()
