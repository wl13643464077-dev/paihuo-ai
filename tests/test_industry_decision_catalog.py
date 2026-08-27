"""Schema53 historical and schema54 current decision catalogs."""
from __future__ import annotations

import glob
import json
import os
import re
import unittest
from difflib import SequenceMatcher
from urllib.parse import urlparse


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECISION_DIR = os.path.join(ROOT, "data", "industry_decisions")
DECISION_V3_DIR = os.path.join(ROOT, "data", "industry_decisions_v3")
EXPECTED = {
    "auto", "beauty", "convenience", "fitness", "grocery", "hotel",
    "pet", "pharmacy", "snack", "tea_coffee",
}
GO_SEMANTICS = "证据足以进入人工审批，不代表允许系统执行任何业务写操作"
METRIC_FIELDS = {
    "key", "name", "formula", "window", "source", "baseline_policy",
    "target_policy",
}
GENERIC_BOUNDARY = "本员工始终不得自动采购、改价、报废、退款、生成处方、停店、排班或放行"


def _catalogs() -> dict[str, dict]:
    rows = {}
    for path in sorted(glob.glob(os.path.join(DECISION_DIR, "*.json"))):
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        rows[os.path.splitext(os.path.basename(path))[0]] = value
    return rows


def _v3_catalogs() -> dict[str, dict]:
    rows = {}
    for path in sorted(glob.glob(os.path.join(DECISION_V3_DIR, "*.json"))):
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        rows[os.path.splitext(os.path.basename(path))[0]] = value
    return rows


def _by_id(rows: list[dict], value: str) -> dict:
    matched = [row for row in rows if row.get("id") == value]
    if len(matched) != 1:
        raise AssertionError(f"source {value} cardinality={len(matched)}")
    return matched[0]


def _pain(catalog: dict, code: str) -> dict:
    matched = [row for row in catalog["pain_points"] if row.get("code") == code]
    if len(matched) != 1:
        raise AssertionError(f"pain {code} cardinality={len(matched)}")
    return matched[0]


class IndustryDecisionCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogs = _catalogs()

    def test_corpus_and_references_are_closed(self):
        self.assertEqual(EXPECTED, set(self.catalogs))
        employee_ids = set()
        for key, catalog in self.catalogs.items():
            self.assertEqual(key, catalog["key"])
            self.assertEqual(6, len(catalog["pain_points"]))
            self.assertEqual(6, len(catalog["employees"]))
            source_ids = [source["id"] for source in catalog["sources"]]
            self.assertEqual(len(source_ids), len(set(source_ids)))
            for source in catalog["sources"]:
                for field in (
                    "id", "title", "publisher", "source_type",
                    "published_at", "scope",
                ):
                    self.assertIsInstance(source.get(field), str)
                    self.assertTrue(source[field].strip())
                self.assertEqual("https", urlparse(source["url"]).scheme)
            for pain in catalog["pain_points"]:
                self.assertTrue(pain["source_ids"])
                self.assertLessEqual(set(pain["source_ids"]), set(source_ids))
            for employee in catalog["employees"]:
                self.assertNotIn(employee["idx"], employee_ids)
                employee_ids.add(employee["idx"])
        self.assertEqual(60, len(employee_ids))

    def test_every_decision_requires_human_approval_and_has_no_side_effect(self):
        boundaries = []
        for catalog in self.catalogs.values():
            for employee in catalog["employees"]:
                contract = employee["decision_contract"]
                self.assertEqual(
                    ["GO", "HOLD", "ESCALATE", "ADVISE"],
                    contract["decision_states"],
                )
                self.assertIs(contract["requires_human_approval"], True)
                self.assertEqual([], contract["allowed_side_effects"])
                self.assertEqual(GO_SEMANTICS, contract["go_semantics"])
                self.assertGreaterEqual(len(contract["forbidden_actions"]), 3)
                self.assertNotIn(GENERIC_BOUNDARY, contract["approval_boundary"])
                boundaries.append(contract["approval_boundary"])
        self.assertEqual(60, len(boundaries))
        self.assertEqual(60, len(set(boundaries)))

    def test_all_180_metrics_are_computable_and_evidence_bound(self):
        metric_keys = set()
        metric_count = 0
        forbidden_baseline = re.compile(
            r"行业默认|行业平均|全国平均|行业均值|全国均值"
        )
        for catalog in self.catalogs.values():
            employee_metric_names = set()
            for employee in catalog["employees"]:
                contract = employee["decision_contract"]
                metrics = contract["success_metrics"]
                self.assertEqual(3, len(metrics))
                names = tuple(metric["name"] for metric in metrics)
                self.assertNotIn(names, employee_metric_names)
                employee_metric_names.add(names)
                for metric in metrics:
                    metric_count += 1
                    self.assertEqual(METRIC_FIELDS, set(metric))
                    self.assertTrue(all(
                        isinstance(metric[field], str) and metric[field].strip()
                        for field in METRIC_FIELDS
                    ))
                    self.assertRegex(metric["key"], r"^[a-z][a-z0-9_]{2,63}$")
                    self.assertNotIn(metric["key"], metric_keys)
                    metric_keys.add(metric["key"])
                    self.assertTrue(any(
                        token in metric["formula"] for token in ("÷", "−", "Σ")
                    ))
                    if "× 100%" in metric["formula"]:
                        self.assertIn("分母为0时记N/A", metric["formula"])
                    self.assertIn("本企业最近4个完整统计窗口", metric["baseline_policy"])
                    self.assertIn("不得用行业值或臆测值补齐", metric["baseline_policy"])
                    self.assertIn("由有权负责人", metric["target_policy"])
                    self.assertIn("书面批准", metric["target_policy"])
                    self.assertIn("目录不预设行业阈值", metric["target_policy"])
                    self.assertIsNone(forbidden_baseline.search(metric["baseline_policy"]))
                    self.assertIsNone(forbidden_baseline.search(metric["target_policy"]))
                    self.assertIn(contract["required_inputs"][0], metric["source"])
                    self.assertIn(contract["required_inputs"][1], metric["source"])
        self.assertEqual(180, metric_count)
        self.assertEqual(180, len(metric_keys))

    def test_corrected_regulatory_metadata_and_claim_linkage(self):
        hotel = self.catalogs["hotel"]
        self.assertEqual(
            "2025-04-23",
            _by_id(hotel["sources"], "hotel-industry-report-2025")["published_at"],
        )

        pharmacy = self.catalogs["pharmacy"]
        gsp = _by_id(pharmacy["sources"], "PH-S2")
        self.assertEqual("official", gsp["source_type"])
        self.assertEqual("2016-07-13", gsp["published_at"])
        self.assertEqual("2015-06-25", gsp["instrument_published_at"])
        self.assertEqual("2016-07-13", gsp["revised_at"])
        self.assertEqual("2021-07-03", gsp["page_published_at"])

        snack = self.catalogs["snack"]
        food_law = _by_id(snack["sources"], "SN-S4")
        self.assertEqual("flk.npc.gov.cn", urlparse(food_law["url"]).hostname)
        self.assertEqual("2025-09-12", food_law["published_at"])
        self.assertIn("2025-12-01", food_law["scope"])

        for key, source_id in (
            ("beauty", "beauty-prepaid-2025"),
            ("fitness", "fitness-prepaid-2025"),
        ):
            prepaid = _by_id(self.catalogs[key]["sources"], source_id)
            self.assertEqual(
                "https://www.court.gov.cn/zixun/xiangqing/459321.html",
                prepaid["url"],
            )
            for date in ("2025-03-13", "2025-03-14", "2025-05-01"):
                self.assertIn(date, prepaid["scope"])

        for key, source_id in (
            ("convenience", "CV-S1"),
            ("grocery", "GR-S1"),
            ("tea_coffee", "TC-S2"),
        ):
            self.assertIn(
                "运行时不得依赖在线可达性",
                _by_id(self.catalogs[key]["sources"], source_id)["scope"],
            )

    def test_privacy_labor_and_accounting_sources_are_bound_to_claims(self):
        pipl_url = "https://www.npc.gov.cn/npc/c2/c30834/202108/t20210820_313088.html"
        for key, source_id, pain_code in (
            ("convenience", "CV-S8", "CV-COMMUNITY"),
            ("tea_coffee", "TC-S6", "TC-MEMBER"),
            ("pharmacy", "PH-S7", "PH-DTP"),
            ("pet", "pet-pipl-2021", "PET-TRUST"),
        ):
            catalog = self.catalogs[key]
            record = _by_id(catalog["sources"], source_id)
            self.assertEqual(pipl_url, record["url"])
            self.assertEqual("official", record["source_type"])
            self.assertIn(source_id, _pain(catalog, pain_code)["source_ids"])

        convenience = self.catalogs["convenience"]
        labor = _by_id(convenience["sources"], "CV-S7")
        self.assertEqual("www.mohrss.gov.cn", urlparse(labor["url"]).hostname)
        self.assertIn("CV-S7", _pain(convenience, "CV-NIGHT")["source_ids"])

        grocery = self.catalogs["grocery"]
        accounting = _by_id(grocery["sources"], "GR-S6")
        self.assertEqual("中华人民共和国财政部", accounting["publisher"])
        self.assertEqual("kjs.mof.gov.cn", urlparse(accounting["url"]).hostname)
        self.assertIn("GR-S6", _pain(grocery, "GR-CONTRACT")["source_ids"])


class IndustryDecisionV3CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogs = _v3_catalogs()

    def test_v3_preserves_all_360_original_employee_slots(self):
        self.assertEqual(EXPECTED, set(self.catalogs))
        expected_ranges = {
            "tea_coffee": range(1001, 1037), "convenience": range(1101, 1137),
            "snack": range(1201, 1237), "grocery": range(1301, 1337),
            "pharmacy": range(1401, 1437), "hotel": range(1501, 1537),
            "auto": range(1601, 1637), "fitness": range(1701, 1737),
            "beauty": range(1801, 1837), "pet": range(1901, 1937),
        }
        all_ids, all_keys, all_names, all_decisions = [], [], [], []
        for industry, catalog in self.catalogs.items():
            self.assertEqual("2026.08.v3", catalog["catalog_version"])
            employees = catalog["employees"]
            self.assertEqual(36, len(employees), industry)
            self.assertEqual(list(expected_ranges[industry]), [e["idx"] for e in employees])
            self.assertEqual([5, 5, 5, 5, 5, 4, 4, 3], [len(g["members"]) for g in catalog["groups"]])
            all_ids.extend(e["idx"] for e in employees)
            all_keys.extend(e["key"] for e in employees)
            all_names.extend(e["name"] for e in employees)
            all_decisions.extend(e["primary_decision"] for e in employees)
        for values in (all_ids, all_keys, all_names, all_decisions):
            self.assertEqual(360, len(values))
            self.assertEqual(360, len(set(values)))

    def test_v3_roles_are_evidence_bound_and_have_native_value_scores(self):
        metric_keys = set()
        metric_names = set()
        metric_formulas = set()
        for catalog in self.catalogs.values():
            ranks = []
            for employee in catalog["employees"]:
                contract = employee["decision_contract"]
                self.assertEqual(employee["primary_decision"], contract["decision"])
                self.assertGreaterEqual(len(contract["required_inputs"]), 4)
                self.assertLessEqual(len(contract["required_inputs"]), 8)
                self.assertGreaterEqual(len(contract["outputs"]), 2)
                self.assertEqual(3, len(contract["success_metrics"]))
                self.assertIs(contract["requires_human_approval"], True)
                self.assertEqual([], contract["allowed_side_effects"])
                self.assertIn("HOLD", contract["fallback"])
                score = employee["priority_score"]
                parts = [score[name] for name in (
                    "pain_severity", "usage_frequency", "economic_value",
                    "data_availability",
                )]
                self.assertTrue(all(type(value) is int and 1 <= value <= 5 for value in parts))
                self.assertEqual(sum(parts), score["total"])
                self.assertTrue(employee["usage_cadence"].strip())
                self.assertTrue(employee["selection_rationale"].strip())
                ranks.append(employee["priority_rank"])
                generic_metric_markers = (
                    "能回链", "反证及人工结论", "人工复核可解释率",
                    "批准截止前补齐证据并关闭", "到期关闭率",
                )
                native_metrics = 0
                for metric in contract["success_metrics"]:
                    self.assertNotIn(metric["key"], metric_keys)
                    metric_keys.add(metric["key"])
                    self.assertNotIn(metric["name"], metric_names)
                    metric_names.add(metric["name"])
                    self.assertNotIn(metric["formula"], metric_formulas)
                    metric_formulas.add(metric["formula"])
                    metric_text = f"{metric['name']}\n{metric['formula']}"
                    if not any(marker in metric_text for marker in generic_metric_markers):
                        native_metrics += 1
                self.assertGreaterEqual(
                    native_metrics, 3,
                    f"{employee['idx']} 的 3 个指标必须全部是岗位原生业务指标",
                )
            self.assertEqual(list(range(1, 37)), sorted(ranks))
        self.assertEqual(1080, len(metric_keys))
        self.assertEqual(1080, len(metric_names))
        self.assertEqual(1080, len(metric_formulas))

    def test_v3_professional_profiles_are_complete_and_operational(self):
        required_profile_keys = {
            "scope", "decisions", "knowledge_domains", "data_objects",
            "tool_permissions", "skill_tree", "capabilities", "operating_rhythm",
            "escalation_matrix", "learning_tracks",
        }
        for industry, catalog in self.catalogs.items():
            self.assertEqual(8, len(catalog["pain_points"]), industry)
            cadences = set()
            for employee in catalog["employees"]:
                profile = employee["professional_profile"]
                self.assertEqual(required_profile_keys, set(profile))
                self.assertTrue(profile["scope"].strip())
                self.assertIn(employee["primary_decision"], profile["decisions"])
                self.assertGreaterEqual(len(profile["decisions"]), 2)
                self.assertGreaterEqual(len(profile["knowledge_domains"]), 3)
                self.assertGreaterEqual(len(profile["data_objects"]), 3)
                self.assertGreaterEqual(len(profile["skill_tree"]), 5)
                self.assertGreaterEqual(len(profile["capabilities"]), 4)
                self.assertGreaterEqual(len(profile["learning_tracks"]), 3)
                self.assertEqual(
                    {"daily", "event_driven", "review"},
                    set(profile["operating_rhythm"]),
                )
                self.assertTrue(all(profile["operating_rhythm"].values()))
                self.assertGreaterEqual(len(profile["escalation_matrix"]), 2)
                for row in profile["escalation_matrix"]:
                    self.assertEqual(
                        {"level", "condition", "owner", "action"}, set(row)
                    )
                    self.assertTrue(all(str(value).strip() for value in row.values()))
                self.assertGreaterEqual(len(profile["tool_permissions"]), 2)
                for row in profile["tool_permissions"]:
                    self.assertEqual({"tool", "access", "scope"}, set(row))
                    self.assertEqual("read_only", row["access"])
                    self.assertTrue(row["tool"].strip())
                    self.assertTrue(row["scope"].strip())
                    self.assertNotIn(
                        row["tool"],
                        {"企业事实查询器", "证据版本追溯器", "通用数据平台", "业务系统"},
                    )
                workflow = employee["decision_contract"]["workflow"]
                generic_workflow_markers = (
                    "冻结判断对象、门店范围、数据时点、批准责任与",
                    "标明GO/HOLD/ESCALATE/ADVISE依据",
                )
                professional_steps = [
                    step for step in workflow
                    if not any(marker in step for marker in generic_workflow_markers)
                ]
                self.assertGreaterEqual(
                    len(professional_steps), 4,
                    f"{employee['idx']} 至少需要 4 个真实专业执行步骤",
                )
                for track in profile["learning_tracks"]:
                    self.assertFalse(
                        track.startswith(("方法进修：", "案例进修：", "结果校准：")),
                        f"{employee['idx']} 学习路径不得使用通用三段式模板",
                    )
                for row in profile["escalation_matrix"]:
                    condition = row["condition"]
                    self.assertNotIn("时发现不可由岗位消除的重大影响", condition)
                    self.assertNotIn("触及企业书面红线", condition)
                    self.assertNotIn("仍无法对齐同一对象与时点，则停止当前判断", condition)
                    self.assertNotIn("停售、停产、资金、隐私或恢复红线", condition)
                    self.assertNotIn("授权内隔离影响", condition)
                cadences.add(employee["usage_cadence"])
            self.assertGreaterEqual(len(cadences), 3, industry)

    def test_v3_priority_rank_is_computed_from_business_value(self):
        for industry, catalog in self.catalogs.items():
            ranked = sorted(
                catalog["employees"],
                key=lambda employee: (
                    -employee["priority_score"]["total"],
                    -employee["priority_score"]["usage_frequency"],
                    -employee["priority_score"]["economic_value"],
                    -employee["priority_score"]["pain_severity"],
                    -employee["priority_score"]["data_availability"],
                    employee["idx"],
                ),
            )
            self.assertEqual(
                list(range(1, 37)),
                [employee["priority_rank"] for employee in ranked],
                industry,
            )

    def test_v3_role_substance_is_not_copy_paste(self):
        removable = re.compile(
            r"\d+|GO|HOLD|ESCALATE|ADVISE|人工审批|系统|岗位|员工|本企业|"
            r"证据|数据|建议|输出|复核|目录|行业阈值|分母为0时记N/A",
            re.I,
        )

        def canon(value, employee, catalog) -> str:
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            for token in (
                employee.get("name"), employee.get("key"), employee.get("group"),
                employee.get("idx"), employee.get("num"), catalog.get("name"),
                catalog.get("industry"), catalog.get("key"),
            ):
                if token is not None and str(token).strip():
                    value = value.replace(str(token).strip(), "")
            value = removable.sub("", value)
            return re.sub(r"[^\w\u4e00-\u9fff]+", "", value).lower()

        for industry, catalog in self.catalogs.items():
            employees = catalog["employees"]
            for field, getter in (
                ("workflow", lambda e: e["decision_contract"]["workflow"]),
                ("outputs", lambda e: e["decision_contract"]["outputs"]),
                ("metrics", lambda e: [
                    {key: metric[key] for key in ("name", "formula", "window", "source")}
                    for metric in e["decision_contract"]["success_metrics"]
                ]),
                ("selection_rationale", lambda e: e["selection_rationale"]),
                ("profile_scope", lambda e: e["professional_profile"]["scope"]),
                ("profile_decisions", lambda e: e["professional_profile"]["decisions"]),
                ("knowledge_domains", lambda e: e["professional_profile"]["knowledge_domains"]),
                ("tool_permissions", lambda e: e["professional_profile"]["tool_permissions"]),
                ("skill_tree", lambda e: e["professional_profile"]["skill_tree"]),
                ("capabilities", lambda e: e["professional_profile"]["capabilities"]),
                ("data_objects", lambda e: e["professional_profile"]["data_objects"]),
                ("operating_rhythm", lambda e: e["professional_profile"]["operating_rhythm"]),
                ("escalation_matrix", lambda e: e["professional_profile"]["escalation_matrix"]),
                ("learning_tracks", lambda e: e["professional_profile"]["learning_tracks"]),
            ):
                values = [canon(getter(employee), employee, catalog) for employee in employees]
                self.assertEqual(36, len(set(values)), f"{industry}/{field}")
                for left in range(len(values)):
                    for right in range(left + 1, len(values)):
                        ratio = SequenceMatcher(None, values[left], values[right]).ratio()
                        self.assertLess(
                            ratio, 0.86,
                            f"{industry}/{field}: {employees[left]['idx']} vs "
                            f"{employees[right]['idx']} ratio={ratio:.3f}",
                        )

    def test_v3_profile_sentence_skeletons_are_not_field_substitution(self):
        """Replacing business nouns must not expose one shared prompt skeleton."""
        def skeleton(value, employee) -> str:
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            contract = employee["decision_contract"]
            profile = employee["professional_profile"]
            tokens = [
                employee.get(field) for field in (
                    "name", "key", "group", "role", "duty", "desc", "intro",
                    "primary_decision", "person",
                )
            ]
            tokens.extend(contract.get("required_inputs") or [])
            tokens.extend(contract.get("outputs") or [])
            tokens.extend(profile.get("data_objects") or [])
            for metric in contract.get("success_metrics") or []:
                tokens.extend((metric.get("name"), metric.get("formula")))
            for token in sorted(
                {str(token) for token in tokens if token}, key=len, reverse=True
            ):
                value = value.replace(token, "§")
            value = re.sub(r"\d+", "N", value)
            return re.sub(r"[^\w\u4e00-\u9fff§]+", "", value).lower()

        for industry, catalog in self.catalogs.items():
            employees = catalog["employees"]
            for field, getter in (
                ("tool_permissions", lambda e: e["professional_profile"]["tool_permissions"]),
                ("capabilities", lambda e: e["professional_profile"]["capabilities"]),
                ("workflow", lambda e: e["decision_contract"]["workflow"]),
                ("metrics", lambda e: [
                    {key: metric[key] for key in ("name", "formula", "window", "source")}
                    for metric in e["decision_contract"]["success_metrics"]
                ]),
                ("learning_tracks", lambda e: e["professional_profile"]["learning_tracks"]),
                ("escalation_matrix", lambda e: e["professional_profile"]["escalation_matrix"]),
            ):
                values = [skeleton(getter(employee), employee) for employee in employees]
                self.assertGreaterEqual(
                    len(set(values)), 30,
                    f"{industry}/{field} is a field-substitution template",
                )


if __name__ == "__main__":
    unittest.main()
