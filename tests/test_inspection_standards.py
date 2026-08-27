"""巡店标准库的稳定合同。

这组测试刻意不依赖数据库：标准是可版本化的产品数据，租户只能在
明确边界内覆盖，不能把法定强制项关掉、降级或偷换来源。
"""
from __future__ import annotations

import copy
import json
import unittest

from app import inspectionstandards as standards


class InspectionStandardsTests(unittest.TestCase):
    def test_all_eleven_industries_have_layered_catalogs(self):
        self.assertEqual(11, len(standards.INDUSTRIES))
        for industry in standards.INDUSTRIES:
            with self.subTest(industry=industry):
                items = standards.effective_checklist(industry)
                self.assertGreaterEqual(len(items), 10)
                self.assertEqual(
                    {"mandatory", "recommended", "operations"},
                    {item["tier"] for item in items},
                )
                areas = {item["area_code"] for item in items}
                self.assertIn("license", areas)
                self.assertIn("personnel", areas)
                self.assertIn("records", areas)
                self.assertTrue(any(
                    item["evidence"] in {"photo", "observation"}
                    and item["area_code"] not in {"license", "personnel", "records"}
                    for item in items
                ), "每个行业都要有现场检查项")
                self.assertTrue(any(
                    item["severity"] == "critical" for item in items
                ), "每个行业都要有红线项")

    def test_item_and_slot_codes_are_unique_and_complete(self):
        required = {
            "item_code", "area_code", "label", "input_type", "required",
            "evidence", "shot_guide", "weight", "severity", "source_no",
            "source_url", "effective", "as_of", "tier", "condition",
            "jurisdiction",
        }
        item_codes = []
        for item in standards.catalog_items():
            self.assertTrue(required.issubset(item))
            self.assertTrue(item["condition"])
            self.assertIn(item["jurisdiction"], {"CN", "CN+local"})
            self.assertEqual(
                item["tier"] == "mandatory", item["required"],
                "推荐与运营项不能伪装成法定强制项",
            )
            item_codes.append(item["item_code"])
        self.assertEqual(len(item_codes), len(set(item_codes)))

        slot_codes = [slot["slot_code"] for slot in standards.catalog_slots()]
        self.assertEqual(len(slot_codes), len(set(slot_codes)))

    def test_sources_and_dates_are_registered(self):
        sources = standards.source_registry()
        self.assertTrue(sources)
        self.assertEqual("2026.08.2", standards.CATALOG_VERSION)
        self.assertEqual(
            "2026-03-20",
            sources["SAMR-FOOD-SALES-CHAIN-114"]["effective"],
        )
        self.assertIn("第114号令", sources["SAMR-FOOD-SALES-CHAIN-114"]["title"])
        self.assertEqual(
            "2019-11-01", sources["NHC-GB37487-2019"]["effective"]
        )
        self.assertIn("GB 37487", sources["NHC-GB37487-2019"]["title"])
        for source_no, source in sources.items():
            self.assertEqual(source_no, source["source_no"])
            self.assertTrue(source["authority"])
            self.assertTrue(source["url"].startswith("https://"))
        for item in standards.catalog_items():
            with self.subTest(item=item["item_code"]):
                self.assertIn(item["source_no"], sources)
                self.assertEqual(
                    sources[item["source_no"]]["url"], item["source_url"]
                )
                self.assertRegex(item["effective"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertRegex(item["as_of"], r"^\d{4}-\d{2}-\d{2}$")

    def test_capture_slots_are_industry_specific_and_bounded(self):
        signatures = set()
        for industry in standards.INDUSTRIES:
            slots = standards.capture_slots(industry)
            self.assertEqual(7, len(slots))
            self.assertEqual(len(slots), len({s["slot_code"] for s in slots}))
            self.assertTrue(any(s["area_code"] == "facade" for s in slots))
            signatures.add(tuple(s["slot_code"] for s in slots))
        self.assertEqual(11, len(signatures), "行业采集位不应只是换行业名")

    def test_domain_specific_risks_are_not_generic_placeholders(self):
        expected_codes = {
            "restaurant": {
                "supplier_traceability", "temperature_control",
                "expiry_management", "cleaning_disinfection",
                "risk_governance", "food_safety_staff",
            },
            "tea_coffee": {
                "supplier_traceability", "temperature_control",
                "expiry_management", "cleaning_disinfection",
                "risk_governance", "food_safety_staff",
            },
            "convenience": {
                "supplier_traceability", "temperature_control",
                "expiry_rotation", "cleaning_disinfection",
                "risk_governance", "food_safety_staff",
            },
            "grocery": {
                "supplier_traceability", "cold_chain_display",
                "expiry_management", "cleaning_disinfection",
                "risk_governance", "food_safety_staff",
            },
            "snack": {
                "supplier_traceability", "temperature_control",
                "expiry_management", "cleaning_disinfection",
                "risk_governance", "food_safety_staff",
            },
            "hotel": {
                "health_license", "personnel_health", "linen_separation",
                "guest_registration", "water_hygiene", "gas_safety",
                "special_equipment",
            },
            "auto": {
                "repair_filing", "technical_personnel",
                "parts_traceability", "quality_inspection",
                "hazardous_waste", "high_voltage_workflow",
            },
            "pharmacy": {
                "drug_license", "pharmacist_on_duty",
                "prescription_audit", "gsp_storage", "cold_chain",
                "expiry_quarantine", "recall_stop_sale",
            },
            "beauty": {
                "health_license", "personnel_health", "tool_disinfection",
                "cosmetic_traceability", "cosmetic_expiry",
                "medical_beauty_boundary",
            },
            "fitness": {
                "high_risk_license", "qualified_staff", "equipment_check",
                "aed_readiness", "pool_water_quality", "lifeguard_positions",
            },
            "pet": {
                "clinic_license", "registered_veterinarian",
                "animal_isolation", "medical_record", "medicine_management",
                "clinical_waste",
            },
        }
        for industry, suffixes in expected_codes.items():
            with self.subTest(industry=industry):
                items = standards.effective_checklist(industry)
                actual = {
                    item["item_code"].removeprefix(f"{industry}.")
                    for item in items
                    if item["item_code"].startswith(f"{industry}.")
                }
                self.assertTrue(suffixes.issubset(actual))

    def test_conditional_rules_expose_applicability_instead_of_overreaching(self):
        conditional_codes = {
            "restaurant.risk_governance",
            "convenience.risk_governance",
            "hotel.special_equipment",
            "auto.high_voltage_workflow",
            "beauty.medical_beauty_boundary",
            "pharmacy.cold_chain",
            "fitness.high_risk_license",
            "pet.clinic_license",
        }
        by_code = {
            item["item_code"]: item for item in standards.catalog_items()
        }
        for code in conditional_codes:
            with self.subTest(code=code):
                self.assertIn(code, by_code)
                self.assertNotEqual("all_stores", by_code[code]["condition"])
        self.assertEqual("recommended", by_code["fitness.aed_readiness"]["tier"])
        self.assertFalse(by_code["fitness.aed_readiness"]["required"])
        self.assertEqual(
            "recommended", by_code["auto.high_voltage_workflow"]["tier"]
        )
        self.assertFalse(by_code["auto.high_voltage_workflow"]["required"])

    def test_metrics_are_structured_but_never_invent_values_or_thresholds(self):
        for industry in standards.INDUSTRIES:
            metrics = standards.metric_catalog(industry)
            self.assertEqual(7, len(metrics))
            by_code = {metric["metric_code"]: metric for metric in metrics}
            self.assertEqual("人", by_code["common.employee_count"]["unit"])
            self.assertEqual("小时", by_code["common.labor_hours"]["unit"])
            for metric in metrics:
                self.assertTrue(metric["metric_code"])
                self.assertTrue(metric["formula"])
                self.assertTrue(metric["source_required"])
                self.assertIn(metric["unit"], metric["allowed_units"])
                self.assertEqual(
                    len(metric["allowed_units"]),
                    len(set(metric["allowed_units"])),
                )
                self.assertIsNone(metric["value"])
                self.assertIsNone(metric["target"])
                self.assertIsNone(metric["benchmark"])
                self.assertTrue(metric["required_inputs"])

    def test_three_override_layers_are_applied_in_order(self):
        result = standards.effective_checklist("restaurant", {
            "tenant": {
                "common.price_display": {"weight": 11},
                "restaurant.pass_log": {"enabled": False},
            },
            "region": {"common.price_display": {"weight": 12}},
            "branch": {"common.price_display": {"weight": 13}},
        })
        by_code = {item["item_code"]: item for item in result}
        self.assertEqual(13, by_code["common.price_display"]["weight"])
        self.assertNotIn("restaurant.pass_log", by_code)

    def test_overrides_fail_closed(self):
        invalid = (
            {"tenant": {"unknown.item": {"enabled": False}}},
            {"tenant": {"common.fire_exit": {"enabled": False}}},
            {"tenant": {"common.fire_exit": {"required": False}}},
            {"tenant": {"common.fire_exit": {"severity": "low"}}},
            {"tenant": {"common.price_display": {"source_url": "https://evil"}}},
            {"tenant": {"common.price_display": {"weight": -1}}},
            {"tenant": {}, "unexpected": {}},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(standards.StandardOverrideError):
                    standards.effective_checklist("restaurant", overrides)

    def test_simple_override_form_is_supported_but_still_closed(self):
        items = standards.effective_checklist(
            "restaurant", {"restaurant.pass_log": {"enabled": False}}
        )
        self.assertNotIn("restaurant.pass_log", {i["item_code"] for i in items})

        records = standards.effective_checklist("restaurant", [{
            "item_code": "restaurant.pass_log", "enabled": False,
        }])
        self.assertNotIn("restaurant.pass_log", {i["item_code"] for i in records})
        with self.assertRaises(standards.StandardOverrideError):
            standards.effective_checklist("restaurant", [
                {"item_code": "restaurant.pass_log", "enabled": False},
                {"item_code": "restaurant.pass_log", "enabled": True},
            ])

    def test_returns_are_deep_copies_and_snapshot_is_stable(self):
        before = standards.version_summary("hotel")
        checklist = standards.effective_checklist("hotel")
        checklist[0]["label"] = "被调用方污染"
        slots = standards.capture_slots("hotel")
        slots[0]["label"] = "被污染"
        metrics = standards.metric_catalog("hotel")
        metrics[0]["value"] = 999
        after = standards.version_summary("hotel")
        self.assertEqual(before, after)
        json.dumps(after, sort_keys=True, ensure_ascii=False)

    def test_unknown_industry_is_rejected_everywhere(self):
        for function in (
            standards.effective_checklist,
            standards.capture_slots,
            standards.metric_catalog,
            standards.version_summary,
        ):
            with self.subTest(function=function.__name__):
                with self.assertRaises(standards.UnknownIndustryError):
                    function("not-an-industry")

    def test_source_registry_is_copy_safe(self):
        first = standards.source_registry()
        changed = copy.deepcopy(first)
        key = next(iter(changed))
        changed[key]["title"] = "被污染"
        self.assertNotEqual(changed, standards.source_registry())


if __name__ == "__main__":
    unittest.main()
