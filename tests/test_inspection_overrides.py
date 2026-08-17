"""Enterprise -> region -> branch inspection-standard override contract."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from app import auth, db, inspection, inspectionoverrides, inspectionstandards, main


class InspectionOverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "inspection-overrides.db")
        db.conn()

        for tenant_id, name in (
            (1, "平台租户"),
            (2, "覆盖测试企业"),
            (3, "其他企业"),
        ):
            db.execute(
                "INSERT OR IGNORE INTO tenants(id,name,balance,enabled,industries_json) "
                "VALUES(?,?,20,1,'[]')",
                (tenant_id, name),
            )
        for tenant_id, industry_key in (
            (1, "restaurant"),
            (2, "restaurant"),
            (2, "auto"),
            (3, "restaurant"),
        ):
            db.execute(
                "INSERT OR IGNORE INTO tenant_industry(tenant_id,industry_key,"
                "is_primary,created_at) VALUES(?,?,?,0)",
                (tenant_id, industry_key, int(industry_key == "restaurant")),
            )
        for user_id, tenant_id, role, modules, enabled in (
            (10, 1, "root", [], 1),
            (20, 2, "owner", [], 1),
            (21, 2, "member", ["restaurant"], 1),
            (22, 2, "member", ["auto"], 1),
            (23, 2, "owner", [], 0),
            (30, 3, "owner", [], 1),
        ):
            db.execute(
                "INSERT OR REPLACE INTO users(id,tenant_id,username,password_hash,"
                "role,modules_json,enabled,created_at) VALUES(?,?,?,?,?,?,?,0)",
                (
                    user_id,
                    tenant_id,
                    f"override-{user_id}",
                    "x",
                    role,
                    json.dumps(modules),
                    enabled,
                ),
            )
        self.owner = {
            "id": 20,
            "tenant_id": 2,
            "username": "override-20",
            "role": "owner",
            "modules": [],
        }
        self.member = {
            "id": 21,
            "tenant_id": 2,
            "username": "override-21",
            "role": "member",
            "modules": ["restaurant"],
        }
        auth.set_current(self.owner)
        self.branch = inspection.create_branch(
            2, 20, "restaurant", {"name": "华北一店", "region": "华北"}
        )
        self.second_branch = inspection.create_branch(
            2, 20, "restaurant", {"name": "华东一店", "region": "华东"}
        )
        self.auto_branch = inspection.create_branch(
            2, 20, "auto", {"name": "汽修一店", "region": "华北"}
        )
        self.foreign_branch = inspection.create_branch(
            3, 30, "restaurant", {"name": "其他企业门店", "region": "华北"}
        )
        self.operations_item = next(
            item
            for item in inspectionstandards.effective_checklist("restaurant")
            if item["tier"] == "operations"
        )
        self.mandatory_item = next(
            item
            for item in inspectionstandards.effective_checklist("restaurant")
            if item["tier"] == "mandatory"
        )

    def tearDown(self):
        auth.set_current(None)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _put(self, *, scope_kind, scope_key, patch, expected_version=0):
        return inspectionoverrides.upsert_override(
            2,
            20,
            "restaurant",
            {
                "scope_kind": scope_kind,
                "scope_key": scope_key,
                "item_code": self.operations_item["item_code"],
                "patch": patch,
                "expected_version": expected_version,
            },
        )

    @staticmethod
    def _item(snapshot, code):
        return next(item for item in snapshot["items"] if item["item_code"] == code)

    def _strict_raw(self, snapshot, request_key):
        return {
            "request_key": request_key,
            "require_checklist": True,
            "template_key": "restaurant",
            "template_version": snapshot["template_version"],
            "file_slots": [
                slot["slot_code"]
                for slot in snapshot["capture_slots"]
                if slot["required"]
            ],
            "observations": {"metrics": [], "checklist": []},
        }

    def test_three_layers_apply_in_order_and_report_effective_sources(self):
        code = self.operations_item["item_code"]
        self._put(
            scope_kind="tenant",
            scope_key=None,
            patch={"shot_guide": "企业统一拍摄说明"},
        )
        self._put(
            scope_kind="region",
            scope_key="华北",
            patch={"weight": 37},
        )
        self._put(
            scope_kind="branch",
            scope_key=self.branch["id"],
            patch={"shot_guide": "本店最终拍摄说明", "severity": "critical"},
        )

        snapshot = inspectionoverrides.effective_snapshot(
            2, 21, "restaurant", self.branch["id"]
        )
        item = self._item(snapshot, code)
        self.assertEqual("本店最终拍摄说明", item["shot_guide"])
        self.assertEqual(37, item["weight"])
        self.assertEqual("critical", item["severity"])
        sources = snapshot["override_summary"]["field_sources"][code]
        self.assertEqual("branch", sources["shot_guide"])
        self.assertEqual("region", sources["weight"])
        self.assertEqual("branch", sources["severity"])
        self.assertEqual(
            ["tenant", "region", "branch"],
            snapshot["override_summary"]["item_scopes"][code],
        )

        # A different region receives only the enterprise layer.
        other = inspectionoverrides.effective_snapshot(
            2, 21, "restaurant", self.second_branch["id"]
        )
        other_item = self._item(other, code)
        self.assertEqual("企业统一拍摄说明", other_item["shot_guide"])
        self.assertNotEqual(37, other_item["weight"])

    def test_mandatory_items_cannot_be_disabled_or_downgraded(self):
        code = self.mandatory_item["item_code"]
        invalid = (
            {"enabled": False},
            {"required": False},
            {"weight": max(0, float(self.mandatory_item["weight"]) - 1)},
            {"severity": "low"},
        )
        for patch in invalid:
            with self.subTest(patch=patch), self.assertRaises(
                inspectionoverrides.InspectionOverrideError
            ) as caught:
                inspectionoverrides.upsert_override(
                    2,
                    20,
                    "restaurant",
                    {
                        "scope_kind": "tenant",
                        "scope_key": None,
                        "item_code": code,
                        "patch": patch,
                        "expected_version": 0,
                    },
                )
            self.assertEqual("OVERRIDE_INVALID", caught.exception.code)

    def test_write_cannot_poison_a_deeper_mandatory_layer(self):
        code = self.mandatory_item["item_code"]
        raised = inspectionoverrides.upsert_override(
            2,
            20,
            "restaurant",
            {
                "scope_kind": "tenant",
                "scope_key": None,
                "item_code": code,
                "patch": {"weight": 80, "severity": "critical"},
                "expected_version": 0,
            },
        )
        with self.assertRaises(inspectionoverrides.InspectionOverrideError) as conflict:
            inspectionoverrides.upsert_override(
                2,
                20,
                "restaurant",
                {
                    "scope_kind": "branch",
                    "scope_key": self.branch["id"],
                    "item_code": code,
                    # Both values are valid against the product baseline but
                    # would downgrade the already-effective enterprise layer.
                    "patch": {
                        "weight": max(1, int(self.mandatory_item["weight"])),
                        "severity": self.mandatory_item["severity"],
                    },
                    "expected_version": 0,
                },
            )
        self.assertEqual("OVERRIDE_INVALID", conflict.exception.code)
        self.assertIsNone(db.one(
            "SELECT id FROM inspection_standard_override WHERE tenant_id=2 "
            "AND industry_key='restaurant' AND scope_kind='branch' "
            "AND scope_key=? AND item_code=?",
            (str(self.branch["id"]), code),
        ))
        snapshot = inspectionoverrides.effective_snapshot(
            2, 20, "restaurant", self.branch["id"]
        )
        item = self._item(snapshot, code)
        self.assertEqual(80, item["weight"])
        self.assertEqual("critical", item["severity"])
        self.assertEqual(1, raised["version"])

    def test_cas_is_strict_and_disable_does_not_create_template_version_aba(self):
        base = inspectionoverrides.effective_snapshot(
            2, 20, "restaurant", self.branch["id"]
        )
        row = self._put(
            scope_kind="tenant",
            scope_key=None,
            patch={"shot_guide": "第一版"},
        )
        changed = inspectionoverrides.effective_snapshot(
            2, 20, "restaurant", self.branch["id"]
        )
        self.assertNotEqual(base["template_version"], changed["template_version"])

        with self.assertRaises(inspectionoverrides.InspectionOverrideError) as caught:
            self._put(
                scope_kind="tenant",
                scope_key=None,
                patch={"shot_guide": "过期覆盖"},
                expected_version=0,
            )
        self.assertEqual("OVERRIDE_CONFLICT", caught.exception.code)

        for invalid_version in (True, 1.2, "1.0"):
            with self.subTest(expected_version=invalid_version), self.assertRaises(
                inspectionoverrides.InspectionOverrideError
            ) as invalid:
                self._put(
                    scope_kind="tenant",
                    scope_key=None,
                    patch={"shot_guide": "非法版本"},
                    expected_version=invalid_version,
                )
            self.assertEqual("OVERRIDE_INVALID", invalid.exception.code)

        disabled = inspectionoverrides.disable_override(
            2, 20, "restaurant", row["id"], row["version"]
        )
        self.assertFalse(disabled["active"])
        after_disable = inspectionoverrides.effective_snapshot(
            2, 20, "restaurant", self.branch["id"]
        )
        self.assertEqual(
            self.operations_item["shot_guide"],
            self._item(after_disable, self.operations_item["item_code"])["shot_guide"],
        )
        self.assertNotEqual(base["template_version"], after_disable["template_version"])
        self.assertNotEqual(changed["template_version"], after_disable["template_version"])

        with self.assertRaises(inspectionoverrides.InspectionOverrideError) as stale:
            inspectionoverrides.disable_override(
                2, 20, "restaurant", row["id"], row["version"]
            )
        self.assertEqual("OVERRIDE_CONFLICT", stale.exception.code)

    def test_member_can_read_effective_only_and_all_scope_fail_closed(self):
        self._put(
            scope_kind="tenant",
            scope_key=None,
            patch={"shot_guide": "成员可见生效值"},
        )
        effective = inspectionoverrides.effective_snapshot(
            2, 21, "restaurant", self.branch["id"]
        )
        self.assertEqual(
            "成员可见生效值",
            self._item(effective, self.operations_item["item_code"])["shot_guide"],
        )
        for action in (
            lambda: inspectionoverrides.list_overrides(2, 21, "restaurant"),
            lambda: inspectionoverrides.upsert_override(
                2,
                21,
                "restaurant",
                {
                    "scope_kind": "tenant",
                    "scope_key": None,
                    "item_code": self.operations_item["item_code"],
                    "patch": {"shot_guide": "越权"},
                    "expected_version": 1,
                },
            ),
        ):
            with self.assertRaises(inspectionoverrides.InspectionOverrideError) as denied:
                action()
            self.assertEqual("OVERRIDE_FORBIDDEN", denied.exception.code)

        with self.assertRaises(inspectionoverrides.InspectionOverrideError):
            inspectionoverrides.effective_snapshot(
                2, 21, "auto", self.auto_branch["id"]
            )
        with self.assertRaises(inspectionoverrides.InspectionOverrideError):
            inspectionoverrides.effective_snapshot(
                2, 21, "restaurant", self.foreign_branch["id"]
            )
        with self.assertRaises(inspectionoverrides.InspectionOverrideError):
            inspectionoverrides.upsert_override(
                2,
                20,
                "restaurant",
                {
                    "scope_kind": "branch",
                    "scope_key": self.auto_branch["id"],
                    "item_code": self.operations_item["item_code"],
                    "patch": {"shot_guide": "跨行业越权"},
                    "expected_version": 0,
                },
            )
        with self.assertRaises(inspectionoverrides.InspectionOverrideError):
            inspectionoverrides.effective_snapshot(
                2, 23, "restaurant", self.branch["id"]
            )
        db.execute("UPDATE tenants SET enabled=0 WHERE id=2")
        with self.assertRaises(inspectionoverrides.InspectionOverrideError):
            inspectionoverrides.effective_snapshot(
                2, 20, "restaurant", self.branch["id"]
            )

    def test_checklist_new_visit_and_history_share_one_frozen_snapshot(self):
        row = self._put(
            scope_kind="branch",
            scope_key=self.branch["id"],
            patch={"shot_guide": "巡店第一版"},
        )
        checklist = main._inspection_checklist_db(
            2, 20, "restaurant", self.branch["id"]
        )
        snapshot = inspectionoverrides.effective_snapshot(
            2, 20, "restaurant", self.branch["id"]
        )
        self.assertEqual(snapshot["template_version"], checklist["template_version"])
        self.assertEqual(snapshot["catalog_sha256"], checklist["catalog_sha256"])

        visit = inspection.create_visit_shell(
            2,
            20,
            "restaurant",
            self.branch["id"],
            self._strict_raw(snapshot, "override-freeze-1"),
        )
        self.assertEqual(snapshot["template_version"], visit["template_version"])
        self.assertEqual(
            snapshot["catalog_sha256"], visit["standard_snapshot"]["catalog_sha256"]
        )

        self._put(
            scope_kind="branch",
            scope_key=self.branch["id"],
            patch={"shot_guide": "巡店第二版"},
            expected_version=row["version"],
        )
        current = inspectionoverrides.effective_snapshot(
            2, 20, "restaurant", self.branch["id"]
        )
        self.assertNotEqual(snapshot["template_version"], current["template_version"])
        stored = inspection.get_visit(2, 20, "restaurant", visit["id"])
        self.assertEqual(snapshot["template_version"], stored["template_version"])
        self.assertEqual(
            "巡店第一版",
            self._item(
                stored["standard_snapshot"], self.operations_item["item_code"]
            )["shot_guide"],
        )
        with self.assertRaises(inspection.InspectionError) as stale:
            inspection.create_visit_shell(
                2,
                20,
                "restaurant",
                self.branch["id"],
                self._strict_raw(snapshot, "override-freeze-stale"),
            )
        self.assertIn("版本已更新", str(stale.exception))

    def test_owner_and_platform_root_http_crud_maps_conflicts(self):
        body = {
            "industry_key": "restaurant",
            "scope_kind": "tenant",
            "scope_key": None,
            "item_code": self.operations_item["item_code"],
            "patch": {"shot_guide": "HTTP 企业标准"},
            "expected_version": 0,
        }
        saved = asyncio.run(main.inspection_standard_override_put(body))
        with self.assertRaises(HTTPException) as unbounded:
            asyncio.run(
                main.inspection_standard_overrides(
                    industry_key="restaurant", scope_kind=None, scope_key=None
                )
            )
        self.assertEqual(400, unbounded.exception.status_code)
        listed = asyncio.run(
            main.inspection_standard_overrides(
                industry_key="restaurant", scope_kind="tenant", scope_key=None
            )
        )
        self.assertEqual(saved["id"], listed["items"][0]["id"])
        with self.assertRaises(HTTPException) as conflict:
            asyncio.run(main.inspection_standard_override_put(body))
        self.assertEqual(409, conflict.exception.status_code)
        self.assertEqual(
            "OVERRIDE_CONFLICT",
            conflict.exception.headers["X-Paihuo-Error-Code"],
        )
        disabled = asyncio.run(
            main.inspection_standard_override_delete(
                saved["id"],
                {
                    "industry_key": "restaurant",
                    "expected_version": saved["version"],
                },
            )
        )
        self.assertFalse(disabled["active"])

        # Platform root CRUD is valid only inside its own platform tenant scope.
        root = {
            "id": 10,
            "tenant_id": 1,
            "username": "override-10",
            "role": "root",
            "modules": [],
        }
        auth.set_current(root)
        root_branch = inspection.create_branch(
            1, 10, "restaurant", {"name": "平台演示门店", "region": "平台区"}
        )
        root_body = dict(body, scope_kind="branch", scope_key=root_branch["id"])
        root_saved = asyncio.run(main.inspection_standard_override_put(root_body))
        self.assertEqual("branch", root_saved["scope_kind"])

        auth.set_current(self.member)
        with self.assertRaises(HTTPException) as denied:
            asyncio.run(main.inspection_standard_override_put(body))
        self.assertEqual(403, denied.exception.status_code)

    def test_manager_ui_is_admin_only_and_exposes_three_scopes_and_cas(self):
        source = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn("function inspectionStandardAdminHtml", source)
        self.assertIn("if(!isAdmin())return \"\"", source)
        self.assertIn('value="tenant"', source)
        self.assertIn('value="region"', source)
        self.assertIn('value="branch"', source)
        self.assertIn("/inspections/standards/overrides", source)
        self.assertIn('method:"PUT"', source)
        self.assertIn('method:"DELETE"', source)
        self.assertIn("expected_version", source)
        self.assertIn("field_sources", source)


if __name__ == "__main__":
    unittest.main()
