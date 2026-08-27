"""巡店服务层的数据、证据与整改状态机契约。

这组测试不经过 HTTP，故意钉住最难在路由层补救的边界：
- 行业授权与门店归属必须由服务端反查，不信前端字段；
- 模型只能引用本次巡店的图片，且不能直接关闭问题；
- 整改与人工复核用版本 CAS，防止双击或两个区域经理互相覆盖；
- 仪表盘的汇总永远按 tenant + industry + store 收口。
"""
from __future__ import annotations

import asyncio
import copy
import os
import tempfile
import unittest
from unittest import mock

from app import db, inspection, inspectionstandards


SCHEMA = """
CREATE TABLE IF NOT EXISTS store_branch(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  industry_key TEXT NOT NULL,
  name TEXT NOT NULL,
  region TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER,
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS inspection_visit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  industry_key TEXT NOT NULL,
  branch_id INTEGER NOT NULL,
  employee_idx INTEGER,
  task_id INTEGER,
  request_key TEXT NOT NULL,
  status TEXT NOT NULL,
  score REAL,
  summary_md TEXT,
  model_json TEXT,
  template_key TEXT,
  template_version TEXT,
  template_snapshot_json TEXT,
  observations_json TEXT,
  created_by INTEGER NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  visit_at REAL,
  completed_at REAL,
  deleted_at REAL,
  deleted_by INTEGER,
  delete_reason TEXT,
  created_at REAL,
  updated_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inspection_visit_request
  ON inspection_visit(tenant_id,request_key);
CREATE TABLE IF NOT EXISTS inspection_photo(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  visit_id INTEGER NOT NULL,
  storage_key TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  phase TEXT NOT NULL DEFAULT 'before',
  caption TEXT,
  capture_slot TEXT,
  item_code TEXT,
  width INTEGER,
  height INTEGER,
  created_by INTEGER,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS inspection_issue(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  visit_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  severity TEXT NOT NULL,
  category TEXT NOT NULL,
  status TEXT NOT NULL,
  owner TEXT,
  due_at REAL,
  confidence REAL,
  needs_human_check INTEGER NOT NULL DEFAULT 0,
  root_cause TEXT,
  closure_evidence TEXT,
  verified_by INTEGER,
  verified_at REAL,
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS inspection_evidence(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  visit_id INTEGER NOT NULL,
  issue_id INTEGER NOT NULL,
  photo_id INTEGER NOT NULL,
  note TEXT,
  bbox_json TEXT,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS inspection_action(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  visit_id INTEGER NOT NULL,
  issue_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  plan TEXT NOT NULL,
  owner TEXT,
  due_at REAL,
  version INTEGER NOT NULL DEFAULT 0,
  closed_by INTEGER,
  closed_at REAL,
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS inspection_recheck(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  visit_id INTEGER NOT NULL,
  issue_id INTEGER NOT NULL,
  action_id INTEGER NOT NULL,
  task_id INTEGER,
  status TEXT NOT NULL,
  note TEXT,
  model_recommendation TEXT,
  created_by INTEGER NOT NULL,
  reviewed_by INTEGER,
  reviewed_at REAL,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS inspection_event(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  visit_id INTEGER NOT NULL,
  issue_id INTEGER,
  action_id INTEGER,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_by INTEGER,
  created_at REAL
);
"""


def photo(
    key: str,
    *,
    digest: str = "a" * 64,
    capture_slot: str | None = None,
    item_code: str | None = None,
) -> dict:
    result = {
        "storage_key": key,
        "mime_type": "image/jpeg",
        "byte_size": 120_000,
        "sha256": digest,
        "width": 1200,
        "height": 900,
    }
    if capture_slot is not None:
        result["capture_slot"] = capture_slot
    if item_code is not None:
        result["item_code"] = item_code
    return result


def analysis_result(photos: list[dict], payload: dict) -> dict:
    """为非质量门测试补齐已验证的逐图合同。"""
    payload = copy.deepcopy(payload)
    defaults = {"critical": 0, "high": 1, "medium": 3, "low": 7}
    for issue in payload.get("issues") or []:
        action = issue.get("action") if isinstance(issue, dict) else None
        if isinstance(action, dict):
            action.setdefault("owner", "测试店长")
            action.setdefault("due_days", defaults.get(issue.get("severity"), 3))
    issue_photo_ids = {
        int(evidence["photo_id"])
        for issue in payload.get("issues") or []
        for evidence in issue.get("evidence") or []
        if isinstance(evidence, dict) and evidence.get("photo_id") is not None
    }
    before = [item for item in photos if item.get("phase", "before") == "before"]
    result = {
        **payload,
        "photo_reviews": [{
            "photo_id": int(item["id"]),
            "analyzable": True,
            "verdict": "issue" if int(item["id"]) in issue_photo_ids else "clean",
            "confidence": 0.95,
            "visible_facts": ["画面主体、通道与物品状态清晰可见"],
        } for item in before],
    }
    if payload.get("issues"):
        result["analysis_status"] = "issues_found"
    else:
        result["analysis_status"] = "clean_verified"
        result["verification"] = {
            "primary_model": "gpt-5.5",
            "review_model": "claude-opus-4-8",
            "both_clean": True,
        }
    return result


class StoreInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "inspection.db")
        db.conn().executescript(SCHEMA)
        for tenant in (
            {"id": 1, "name": "平台", "industries_json": "[]"},
            {"id": 2, "name": "餐饮企业", "industries_json": '["restaurant"]'},
            {"id": 3, "name": "酒店企业", "industries_json": '["hotel"]'},
        ):
            db.insert("tenants", tenant)
        for tenant_id, industry_key in ((1, "restaurant"), (1, "hotel"),
                                        (2, "restaurant"), (3, "hotel")):
            db.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                "VALUES(?,?,1,0)",
                (tenant_id, industry_key),
            )
        for user in (
            {"id": 1, "tenant_id": 1, "username": "root", "role": "root", "modules_json": "[]"},
            {"id": 20, "tenant_id": 2, "username": "owner-a", "role": "owner", "modules_json": "[]"},
            {"id": 21, "tenant_id": 2, "username": "member-a", "role": "member", "modules_json": '["restaurant"]'},
            {"id": 22, "tenant_id": 2, "username": "member-no-module", "role": "member", "modules_json": "[]"},
            {"id": 30, "tenant_id": 3, "username": "owner-b", "role": "owner", "modules_json": "[]"},
            {"id": 31, "tenant_id": 2, "username": "tour-a", "role": "tour", "modules_json": '["restaurant"]'},
            {"id": 32, "tenant_id": 2, "username": "unknown-a", "role": "auditor", "modules_json": '["restaurant"]'},
        ):
            db.insert("users", {
                **user,
                "password_hash": "x",
                "enabled": 1,
            })
        self.branch = inspection.create_branch(
            2,
            20,
            "restaurant",
            {"name": "朝阳一店", "region": "华北区", "address": "朝阳路 1 号"},
        )

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _draft(self, *, request_key="visit-restaurant-0001", photos=None):
        return inspection.create_visit_draft(
            2,
            21,
            "restaurant",
            self.branch["id"],
            {"request_key": request_key, "note": "周一例行巡店"},
            photos or [photo("inspections/2/v1/front.jpg")],
        )

    def _strict_request(
        self,
        *,
        request_key: str,
        file_slots: list[str] | None = None,
        observations: dict | None = None,
        template_version: str | None = None,
    ) -> dict:
        required = [
            slot["slot_code"]
            for slot in inspectionstandards.capture_slots("restaurant")
            if slot["required"]
        ]
        return {
            "request_key": request_key,
            "note": "严格标准巡店",
            "require_checklist": True,
            "template_version": (
                inspectionstandards.CATALOG_VERSION
                if template_version is None else template_version
            ),
            "file_slots": required if file_slots is None else file_slots,
            "observations": (
                {"metrics": [], "checklist": []}
                if observations is None else observations
            ),
        }

    def _strict_photos(
        self,
        file_slots: list[str] | None = None,
    ) -> list[dict]:
        slots = file_slots or [
            slot["slot_code"]
            for slot in inspectionstandards.capture_slots("restaurant")
            if slot["required"]
        ]
        return [
            photo(
                f"inspections/2/strict/{index}.jpg",
                digest=f"{index + 1:x}" * 64,
                capture_slot=slot,
            )
            for index, slot in enumerate(slots)
        ]

    def test_industry_actor_and_branch_scope_are_server_authoritative(self):
        with self.assertRaises(inspection.InspectionForbidden):
            inspection.create_branch(2, 20, "hotel", {"name": "伪造酒店"})
        member_branch = inspection.create_branch(
            2, 21, "restaurant", {"name": "区域经理新建店", "region": "华东区"}
        )
        self.assertEqual(21, member_branch["created_by"])
        with self.assertRaises(inspection.InspectionForbidden):
            inspection.create_branch(2, 22, "restaurant", {"name": "无权门店"})
        db.execute("UPDATE users SET enabled=0 WHERE id=21")
        with self.assertRaises(inspection.InspectionForbidden):
            inspection.create_branch(2, 21, "restaurant", {"name": "停用账号门店"})
        db.execute("UPDATE users SET enabled=1 WHERE id=21")
        for unauthorized_user in (31, 32):
            with self.assertRaises(inspection.InspectionForbidden):
                inspection.list_branches(2, unauthorized_user, "restaurant")

        foreign = inspection.create_branch(
            3, 30, "hotel", {"name": "租户 B 门店"}
        )
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.create_visit_draft(
                2,
                21,
                "restaurant",
                foreign["id"],
                {"request_key": "cross-tenant-0001"},
                [photo("inspections/2/v2/front.jpg")],
            )
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) n FROM inspection_visit WHERE request_key=?",
                ("cross-tenant-0001",),
            )["n"],
        )

    def test_visit_history_region_filter_is_exact_and_exclusive(self):
        north = self._draft(request_key="region-filter-north-0001")
        east_branch = inspection.create_branch(
            2, 21, "restaurant",
            {"name": "华东二店", "region": "华东区", "address": "东路 2 号"},
        )
        east = inspection.create_visit_draft(
            2, 21, "restaurant", east_branch["id"],
            {"request_key": "region-filter-east-0001"},
            [photo("inspections/2/regions/east.jpg", digest="a" * 64)],
        )
        unassigned_branch = inspection.create_branch(
            2, 21, "restaurant",
            {"name": "待分区门店", "region": "", "address": "待补地址"},
        )
        unassigned = inspection.create_visit_draft(
            2, 21, "restaurant", unassigned_branch["id"],
            {"request_key": "region-filter-empty-0001"},
            [photo("inspections/2/regions/empty.jpg", digest="b" * 64)],
        )

        self.assertEqual(
            [north["id"]],
            [item["id"] for item in inspection.list_visits(
                2, 21, "restaurant", region="华北区",
            )["items"]],
        )
        self.assertEqual(
            [east["id"]],
            [item["id"] for item in inspection.list_visits(
                2, 21, "restaurant", region="华东区",
            )["items"]],
        )
        self.assertEqual(
            [unassigned["id"]],
            [item["id"] for item in inspection.list_visits(
                2, 21, "restaurant", region="",
            )["items"]],
        )
        with self.assertRaises(inspection.InspectionError):
            inspection.list_visits(
                2, 21, "restaurant",
                branch_id=self.branch["id"], region="华北区",
            )

    def test_strict_visit_freezes_versioned_standard_slots_and_observations(self):
        raw = self._strict_request(
            request_key="strict-snapshot-0001",
            observations={
                "metrics": [{
                    "metric_code": "common.net_revenue",
                    "value": "123.50",
                    "unit": "CNY",
                }],
                "checklist": [
                    {
                        "item_code": "common.fire_inspection_log",
                        "value": "记录已由店长现场核对",
                    },
                    {"item_code": "common.fire_exit", "value": True},
                    {"item_code": "common.price_display", "value": False},
                ],
            },
        )
        photos = self._strict_photos()
        photos[0]["item_code"] = "common.fire_exit"
        visit = inspection.create_visit_draft(
            2, 21, "restaurant", self.branch["id"], raw, photos,
        )

        self.assertEqual("restaurant", visit["template_key"])
        self.assertEqual(
            inspectionstandards.CATALOG_VERSION,
            visit["template_version"],
        )
        snapshot = visit["standard_snapshot"]
        self.assertEqual("restaurant", snapshot["template_key"])
        self.assertEqual(raw["file_slots"], snapshot["file_slots"])
        self.assertEqual(
            raw["file_slots"],
            [item["capture_slot"] for item in visit["photos"]],
        )
        self.assertEqual("common.fire_exit", visit["photos"][0]["item_code"])
        # A capture slot is mandatory in strict mode; an item association is
        # optional and uses the schema's empty-string storage sentinel.
        self.assertIsNone(visit["photos"][1]["item_code"])
        self.assertEqual(
            "",
            db.one(
                "SELECT item_code FROM inspection_photo WHERE id=?",
                (visit["photos"][1]["id"],),
            )["item_code"],
        )
        self.assertEqual(123.5, visit["observations"]["metrics"][0]["value"])
        self.assertEqual(
            "CNY", visit["observations"]["metrics"][0]["unit"]
        )
        # The frozen metric catalog carries definitions only, never actual
        # operating numbers supplied by this visit.
        metric = next(
            item for item in snapshot["metrics"]
            if item["metric_code"] == "common.net_revenue"
        )
        self.assertIsNone(metric["value"])
        self.assertNotIn("123.5", repr(snapshot))
        stored = db.one(
            "SELECT template_key,template_version,template_snapshot_json,"
            "observations_json FROM inspection_visit WHERE id=?",
            (visit["id"],),
        )
        self.assertEqual("restaurant", stored["template_key"])
        self.assertEqual(visit["template_version"], stored["template_version"])
        self.assertEqual(snapshot, db.jloads(stored["template_snapshot_json"], {}))

    def test_legacy_draft_uses_schema52_sentinels_without_enabling_strict_mode(self):
        visit = self._draft(request_key="legacy-schema52-0001")

        self.assertIsNone(visit["template_key"])
        self.assertIsNone(visit["template_version"])
        self.assertIsNone(visit["standard_snapshot"])
        self.assertEqual(
            {"metrics": [], "checklist": []},
            visit["observations"],
        )
        self.assertIsNone(visit["photos"][0]["capture_slot"])
        self.assertIsNone(visit["photos"][0]["item_code"])
        stored = db.one(
            "SELECT template_snapshot_json,observations_json FROM inspection_visit "
            "WHERE id=?",
            (visit["id"],),
        )
        self.assertEqual("{}", stored["template_snapshot_json"])
        self.assertEqual("[]", stored["observations_json"])
        stored_photo = db.one(
            "SELECT capture_slot,item_code FROM inspection_photo WHERE visit_id=?",
            (visit["id"],),
        )
        self.assertEqual("", stored_photo["capture_slot"])
        self.assertEqual("", stored_photo["item_code"])

    def test_strict_capture_slots_fail_closed_for_missing_duplicate_unknown_and_mismatch(self):
        required = self._strict_request(
            request_key="slot-base-0001"
        )["file_slots"]
        cases = {
            "missing": (required[:-1], required[:-1]),
            "duplicate": (required[:-1] + [required[0]], required[:-1] + [required[0]]),
            "unknown": (required[:-1] + ["restaurant.unknown"], required[:-1] + ["restaurant.unknown"]),
            "file_photo_mismatch": (required, [required[1], required[0], *required[2:]]),
        }
        before = db.one("SELECT COUNT(*) n FROM inspection_visit")["n"]
        for index, (name, (declared, photographed)) in enumerate(cases.items()):
            raw = self._strict_request(
                request_key=f"strict-slot-{index:04d}",
                file_slots=list(declared),
            )
            with self.subTest(name=name), self.assertRaises(inspection.InspectionError):
                inspection.create_visit_draft(
                    2, 21, "restaurant", self.branch["id"], raw,
                    self._strict_photos(list(photographed)),
                )
        self.assertEqual(
            before,
            db.one("SELECT COUNT(*) n FROM inspection_visit")["n"],
        )

    def test_strict_template_version_and_observation_contract_reject_drift(self):
        with self.assertRaisesRegex(inspection.InspectionError, "版本"):
            inspection.create_visit_draft(
                2, 21, "restaurant", self.branch["id"],
                self._strict_request(
                    request_key="strict-version-drift-0001",
                    template_version="2025.01.0",
                ),
                self._strict_photos(),
            )

        invalid_observations = {
            "not_an_object": [],
            "non_finite": {"metrics": [{
                "metric_code": "common.net_revenue", "value": float("nan"),
                "unit": "CNY",
            }]},
            "unknown_metric": {"metrics": [{
                "metric_code": "unknown.sales", "value": 1, "unit": "CNY",
            }]},
            "wrong_unit": {"metrics": [{
                "metric_code": "common.net_revenue", "value": 1,
                "unit": "person",
            }]},
            "duplicate_metric": {"metrics": [
                {"metric_code": "common.net_revenue", "value": 1, "unit": "CNY"},
                {"metric_code": "common.net_revenue", "value": 2, "unit": "元"},
            ]},
            "unknown_item": {"checklist": [{
                "item_code": "restaurant.not-real", "value": "已检查",
            }]},
            "boolean_as_text": {"checklist": [{
                "item_code": "common.fire_exit", "value": "任意文本也通过",
            }]},
            "boolean_as_number": {"checklist": [{
                "item_code": "common.fire_exit", "value": 1,
            }]},
            "document_as_boolean": {"checklist": [{
                "item_code": "common.fire_inspection_log", "value": True,
            }]},
            "non_finite_checklist": {"checklist": [{
                "item_code": "common.fire_exit", "value": float("inf"),
            }]},
            "overflowing_checklist": {"checklist": [{
                "item_code": "common.fire_exit", "value": 10 ** 1000,
            }]},
            "structured_checklist_value": {"checklist": [{
                "item_code": "common.fire_exit", "value": {"secret": 1},
            }]},
            "oversized_checklist_value": {"checklist": [{
                "item_code": "common.fire_exit", "value": "x" * 1001,
            }]},
            "duplicate_checklist": {"checklist": [
                {"item_code": "common.fire_exit", "value": True},
                {"item_code": "common.fire_exit", "value": False},
            ]},
        }
        for index, (name, observations) in enumerate(invalid_observations.items()):
            with self.subTest(name=name), self.assertRaises(inspection.InspectionError):
                inspection.create_visit_draft(
                    2, 21, "restaurant", self.branch["id"],
                    self._strict_request(
                        request_key=f"strict-observation-{index:04d}",
                        observations=observations,
                    ),
                    self._strict_photos(),
                )

    def test_strict_shell_binds_declared_file_slots_one_to_one(self):
        raw = self._strict_request(request_key="strict-shell-0001")
        shell = inspection.create_visit_shell(
            2, 21, "restaurant", self.branch["id"], raw,
        )
        wrong = self._strict_photos([
            raw["file_slots"][1], raw["file_slots"][0], *raw["file_slots"][2:]
        ])
        with self.assertRaises(inspection.InspectionError):
            inspection.attach_visit_photos(
                2, 21, "restaurant", shell["id"], wrong,
            )
        attached = inspection.attach_visit_photos(
            2, 21, "restaurant", shell["id"], self._strict_photos(),
        )
        self.assertEqual("analyzing", attached["status"])
        self.assertEqual(
            raw["file_slots"],
            [item["capture_slot"] for item in attached["photos"]],
        )

    def test_strict_completion_uses_frozen_snapshot_not_current_catalog(self):
        raw = self._strict_request(request_key="strict-history-0001")
        visit = inspection.create_visit_draft(
            2, 21, "restaurant", self.branch["id"], raw,
            self._strict_photos(),
        )
        frozen = copy.deepcopy(visit["standard_snapshot"])
        with mock.patch.object(
            inspectionstandards, "CATALOG_VERSION", "2099.99.9"
        ), mock.patch.object(
            inspectionstandards,
            "capture_slots",
            return_value=[{
                "slot_code": "future.slot", "required": True,
                "min_photos": 1, "max_photos": 1,
            }],
        ):
            completed = inspection.complete_visit(
                2, 21, "restaurant", visit["id"],
                analysis_result(
                    visit["photos"],
                    {"summary": "历史标准下完成", "score": 100, "issues": []},
                ),
            )
            self.assertEqual(frozen, completed["standard_snapshot"])
            self.assertEqual(raw["template_version"], completed["template_version"])

    def test_strict_completion_revalidates_persisted_capture_coverage(self):
        visit = inspection.create_visit_draft(
            2, 21, "restaurant", self.branch["id"],
            self._strict_request(request_key="strict-complete-slots-0001"),
            self._strict_photos(),
        )
        db.execute(
            "DELETE FROM inspection_photo WHERE id=?",
            (visit["photos"][-1]["id"],),
        )
        with self.assertRaises(inspection.InspectionError):
            inspection.complete_visit(
                2, 21, "restaurant", visit["id"],
                analysis_result(
                    visit["photos"][:-1],
                    {"summary": "不应完成", "score": 100, "issues": []},
                ),
            )

    def test_run_context_exposes_frozen_standard_but_not_operating_values(self):
        raw = self._strict_request(
            request_key="strict-model-privacy-0001",
            observations={"metrics": [{
                "metric_code": "common.net_revenue",
                "value": 98765.5,
                "unit": "CNY",
            }]},
        )
        slots = raw["file_slots"]

        async def save_photo(tid, visit_id, index, _upload):
            return photo(
                f"inspections/{tid}/{visit_id}/{index}.jpg",
                digest=f"{index + 1:x}" * 64,
                capture_slot=slots[index],
            )

        async def analyze(context, photos):
            self.assertEqual(raw["template_version"], context["template_version"])
            self.assertIn("standard_snapshot", context)
            self.assertNotIn("observations", context)
            self.assertNotIn("98765.5", repr(context))
            return analysis_result(
                photos,
                {"summary": "严格巡店完成", "score": 100, "issues": []},
            )

        result = asyncio.run(inspection.run_inspection(
            2, 21, "restaurant", self.branch["id"], raw,
            [object() for _ in slots],
            save_photo=save_photo,
            analyze_photos=analyze,
        ))
        self.assertEqual("completed", result["status"])
        self.assertEqual(98765.5, result["observations"]["metrics"][0]["value"])

    def test_model_issues_require_owned_evidence_and_valid_severity(self):
        visit = self._draft(photos=[
            photo("inspections/2/v1/front.jpg"),
            photo("inspections/2/v1/kitchen.jpg", digest="b" * 64),
        ])
        photo_ids = [item["id"] for item in visit["photos"]]
        foreign = inspection.create_branch(3, 30, "hotel", {"name": "酒店店"})
        foreign_visit = inspection.create_visit_draft(
            3,
            30,
            "hotel",
            foreign["id"],
            {"request_key": "foreign-visit-0001"},
            [photo("inspections/3/v1/front.jpg", digest="c" * 64)],
        )
        foreign_photo_id = foreign_visit["photos"][0]["id"]

        base = {
            "summary": "后厨有一项高风险问题",
            "score": 72,
            "issues": [{
                "title": "消防通道被占用",
                "description": "纸箱占用后厨消防通道",
                "severity": "high",
                "category": "safety",
                "confidence": 0.91,
                "evidence": [{"photo_id": photo_ids[1], "note": "通道右侧"}],
                "action": {"plan": "立即清走纸箱并拍照复核", "owner": "店长", "due_days": 1},
            }],
        }
        bad_foreign = {**base, "issues": [{**base["issues"][0], "evidence": [{"photo_id": foreign_photo_id}]}]}
        with self.assertRaises(inspection.InspectionError):
            inspection.complete_visit(2, 21, "restaurant", visit["id"], analysis_result(visit["photos"], bad_foreign))
        bad_severity = {**base, "issues": [{**base["issues"][0], "severity": "blocker"}]}
        with self.assertRaises(inspection.InspectionError):
            inspection.complete_visit(2, 21, "restaurant", visit["id"], analysis_result(visit["photos"], bad_severity))
        no_evidence = {**base, "issues": [{**base["issues"][0], "evidence": []}]}
        with self.assertRaises(inspection.InspectionError):
            inspection.complete_visit(2, 21, "restaurant", visit["id"], analysis_result(visit["photos"], no_evidence))
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM inspection_issue")["n"])

        completed = inspection.complete_visit(2, 21, "restaurant", visit["id"], analysis_result(visit["photos"], base))
        self.assertEqual("completed", completed["status"])
        self.assertEqual(1, len(completed["issues"]))
        issue = completed["issues"][0]
        self.assertEqual("high", issue["severity"])
        self.assertEqual("detected", issue["status"])
        self.assertEqual("open", issue["action"]["status"])
        self.assertEqual(photo_ids[1], issue["evidence"][0]["photo_id"])

    def test_photo_review_contract_rejects_blind_or_incomplete_results(self):
        visit = self._draft(request_key="blind-production-shape-0001")
        with self.assertRaises(inspection.InspectionError):
            inspection.complete_visit(2, 21, "restaurant", visit["id"], {
                "summary": "未发现问题", "score": 100, "issues": [],
            })
        self.assertEqual(
            {"status": "analyzing", "model_json": None},
            db.one(
                "SELECT status,model_json FROM inspection_visit WHERE id=?",
                (visit["id"],),
            ),
        )

        allowed = {11, 12}
        base = analysis_result(
            [{"id": 11}, {"id": 12}],
            {"summary": "两张照片均已核验", "score": 99, "issues": []},
        )
        invalid: dict[str, dict] = {
            "production_blind_shape": {
                "summary": "未发现问题", "score": 100, "issues": [],
            },
            "missing_issues": {key: value for key, value in base.items() if key != "issues"},
            "missing_photo": {
                **base, "photo_reviews": base["photo_reviews"][:1],
            },
            "duplicate_photo": {
                **base,
                "photo_reviews": [base["photo_reviews"][0], base["photo_reviews"][0]],
            },
            "foreign_photo": {
                **base,
                "photo_reviews": [
                    base["photo_reviews"][0],
                    {**base["photo_reviews"][1], "photo_id": 99},
                ],
            },
            "unanalyzable": {
                **base,
                "photo_reviews": [
                    {**base["photo_reviews"][0], "analyzable": False},
                    base["photo_reviews"][1],
                ],
            },
            "low_confidence": {
                **base,
                "photo_reviews": [
                    {**base["photo_reviews"][0], "confidence": 0.79},
                    base["photo_reviews"][1],
                ],
            },
            "empty_visible_facts": {
                **base,
                "photo_reviews": [
                    {**base["photo_reviews"][0], "visible_facts": []},
                    base["photo_reviews"][1],
                ],
            },
        }
        for name, value in invalid.items():
            with self.subTest(name=name), self.assertRaises(inspection.InspectionError):
                inspection.normalize_model_result(value, allowed)

        single_model = copy.deepcopy(base)
        single_model.pop("verification")
        single_model["analysis_status"] = "clean_candidate"
        with self.assertRaisesRegex(inspection.InspectionError, "异模复核"):
            inspection.normalize_model_result(single_model, allowed)

        issue_without_same_photo_evidence = copy.deepcopy(base)
        issue_without_same_photo_evidence["photo_reviews"][0]["verdict"] = "issue"
        with self.assertRaisesRegex(inspection.InspectionError, "同图问题证据"):
            inspection.normalize_model_result(
                issue_without_same_photo_evidence,
                allowed,
                allow_clean_candidate=True,
            )

        normalized = inspection.normalize_model_result(base, allowed)
        self.assertEqual("clean_verified", normalized["analysis_status"])
        self.assertEqual(allowed, {item["photo_id"] for item in normalized["photo_reviews"]})

    def test_model_result_canonicalization_is_bounded_and_keeps_hard_gates(self):
        raw = {
            "analysis_status": "issues_found",
            "summary": "可见通道存在风险",
            "score": 73,
            "photo_reviews": [{
                "photo_id": 11,
                "analyzable": "true",
                "verdict": "issue",
                "confidence": "95%",
                "visible_facts": "通道中可见电线",
            }],
            "issues": [{
                "title": "通道电线",
                "description": "电线横跨可通行区域",
                "severity": "high",
                "category": "safety / hazard",
                "confidence": 96,
                "root_cause": "布线原因待人工核查",
                "evidence": [{
                    "photo_id": 11,
                    "note": "地面可见电线",
                    "bbox": [2, 0, 1, 1],
                }],
                "action": {
                    "plan": ["立即设置警示", "固定布线并拍照复查"],
                    "owner": "值班店长",
                    "due_days": "立即",
                },
            }],
        }
        normalized = inspection.normalize_model_result(
            raw,
            {11},
            allow_clean_candidate=True,
        )
        self.assertIs(True, normalized["photo_reviews"][0]["analyzable"])
        self.assertEqual(.95, normalized["photo_reviews"][0]["confidence"])
        self.assertEqual(
            ["通道中可见电线"],
            normalized["photo_reviews"][0]["visible_facts"],
        )
        issue = normalized["issues"][0]
        self.assertEqual(.96, issue["confidence"])
        self.assertEqual("other", issue["category"])
        self.assertIsNone(issue["evidence"][0]["bbox"])
        self.assertEqual("立即设置警示；固定布线并拍照复查", issue["action"]["plan"])
        self.assertEqual("值班店长", issue["action"]["owner"])
        self.assertEqual(0, issue["action"]["due_days"])

        missing_confidence = copy.deepcopy(raw)
        missing_confidence["photo_reviews"][0].pop("confidence")
        with self.assertRaises(inspection.InspectionContractError) as missing:
            inspection.normalize_model_result(
                missing_confidence,
                {11},
                allow_clean_candidate=True,
            )
        self.assertEqual("IC_REVIEW_CONFIDENCE_REQUIRED", missing.exception.validation_code)
        self.assertTrue(missing.exception.retryable)

        for field, value, code in (
            ("confidence", "79%", "IC_REVIEW_CONFIDENCE_LOW"),
            ("analyzable", "false", "IC_REVIEW_UNANALYZABLE"),
        ):
            candidate = copy.deepcopy(raw)
            candidate["photo_reviews"][0][field] = value
            with self.subTest(field=field), self.assertRaises(
                inspection.InspectionContractError
            ) as blocked:
                inspection.normalize_model_result(
                    candidate,
                    {11},
                    allow_clean_candidate=True,
                )
            self.assertEqual(code, blocked.exception.validation_code)
            self.assertFalse(blocked.exception.retryable)

        missing_id = copy.deepcopy(raw)
        missing_id["photo_reviews"][0].pop("photo_id")
        with self.assertRaises(inspection.InspectionContractError):
            inspection.normalize_model_result(
                missing_id,
                {11},
                allow_clean_candidate=True,
            )

        invalid_actions = {
            "action_missing": None,
            "plan_missing": {"owner": "店长", "due_days": 1},
            "plan_null": {"plan": None, "owner": "店长", "due_days": 1},
            "plan_empty": {"plan": " ", "owner": "店长", "due_days": 1},
            "plan_empty_array": {"plan": [], "owner": "店长", "due_days": 1},
            "plan_blank_array": {"plan": [" ", ""], "owner": "店长", "due_days": 1},
            "owner_missing": {"plan": "清理", "due_days": 1},
            "owner_null": {"plan": "清理", "owner": None, "due_days": 1},
            "owner_empty": {"plan": "清理", "owner": " ", "due_days": 1},
            "due_missing": {"plan": "清理", "owner": "店长"},
            "due_null": {"plan": "清理", "owner": "店长", "due_days": None},
            "due_empty": {"plan": "清理", "owner": "店长", "due_days": " "},
        }
        for name, invalid_action in invalid_actions.items():
            invalid_candidate = copy.deepcopy(raw)
            if invalid_action is None:
                invalid_candidate["issues"][0].pop("action")
            else:
                invalid_candidate["issues"][0]["action"] = invalid_action
            with self.subTest(invalid_action=name), self.assertRaises(
                inspection.InspectionContractError
            ) as missing_action:
                inspection.normalize_model_result(
                    invalid_candidate,
                    {11},
                    allow_clean_candidate=True,
                )
            self.assertEqual(
                "IC_ACTION_REQUIRED",
                missing_action.exception.validation_code,
            )
            self.assertTrue(missing_action.exception.retryable)

    def test_low_confidence_is_flagged_and_model_cannot_close_an_issue(self):
        visit = self._draft()
        pid = visit["photos"][0]["id"]
        result = inspection.complete_visit(2, 21, "restaurant", visit["id"], analysis_result(visit["photos"], {
            "summary": "需要人工复核",
            "score": 90,
            "issues": [{
                "title": "疑似物料标签不清",
                "description": "画面模糊，无法确认效期",
                "severity": "medium",
                "category": "inventory",
                "confidence": 0.42,
                "status": "closed",
                "evidence": [{"photo_id": pid}],
                "action": {"plan": "店长现场核对标签", "status": "verified"},
            }],
        }))
        issue = result["issues"][0]
        self.assertTrue(issue["needs_human_check"])
        self.assertEqual("detected", issue["status"])
        self.assertEqual("open", issue["action"]["status"])

    def test_rectification_and_human_review_use_compare_and_swap(self):
        visit = self._draft()
        pid = visit["photos"][0]["id"]
        completed = inspection.complete_visit(2, 21, "restaurant", visit["id"], analysis_result(visit["photos"], {
            "summary": "待整改",
            "score": 80,
            "issues": [{
                "title": "门头灯箱不亮",
                "description": "右侧字样灯不亮",
                "severity": "medium",
                "category": "brand",
                "confidence": 0.95,
                "evidence": [{"photo_id": pid}],
                "action": {"plan": "联系维修并上传点亮后照片", "owner": "值班店长", "due_days": 3},
            }],
        }))
        action = completed["issues"][0]["action"]
        started = inspection.transition_action(
            2, 21, "restaurant", action["id"], expected_version=1,
            target_status="in_progress", note="已报修",
        )
        self.assertEqual(2, started["version"])
        with self.assertRaises(inspection.InspectionConflict):
            inspection.transition_action(
                2, 21, "restaurant", action["id"], expected_version=1,
                target_status="awaiting_recheck",
            )
        pending = inspection.transition_action(
            2, 21, "restaurant", action["id"], expected_version=2,
            target_status="awaiting_recheck", note="已修复",
        )
        self.assertEqual(3, pending["version"])

        recheck_photos = inspection.add_recheck_photos(
            2,
            21,
            "restaurant",
            action["id"],
            [photo("inspections/2/v1/recheck-light.jpg", digest="e" * 64)],
        )
        recheck_pid = recheck_photos[0]["id"]

        recheck = inspection.record_recheck(
            2, 21, "restaurant", action["id"], {
                "recommendation": "close",
                "note": "新照片中灯箱已全部点亮",
                "confidence": 0.94,
                "evidence_photo_ids": [recheck_pid],
            },
        )
        with self.assertRaises(inspection.InspectionForbidden):
            inspection.review_recheck(
                2, 21, "restaurant", recheck["id"],
                decision="close", expected_action_version=3,
                note="成员不能最终关单",
            )
        reviewed = inspection.review_recheck(
            2, 20, "restaurant", recheck["id"],
            decision="close", expected_action_version=3,
            note="区域经理已复核通过",
        )
        self.assertEqual("closed", reviewed["action"]["status"])
        self.assertEqual("closed", reviewed["issue_status"])
        self.assertEqual("approved", reviewed["status"])

    def test_only_owner_or_platform_root_can_cas_confirm_action_assignment(self):
        visit = self._draft(request_key="assignment-visit-0001")
        completed = inspection.complete_visit(2, 21, "restaurant", visit["id"], analysis_result(visit["photos"], {
            "summary": "待确认整改责任",
            "score": 78,
            "issues": [{
                "title": "后场物料未归位",
                "description": "照片中可见物料占用操作区",
                "severity": "medium",
                "category": "operations",
                "confidence": .95,
                "evidence": [{"photo_id": visit["photos"][0]["id"]}],
                "action": {"plan": "整理物料", "owner": "AI建议：店长", "due_days": 3},
            }],
        }))
        issue = completed["issues"][0]
        action = issue["action"]
        due_at = 2_000_000_000.0

        with self.assertRaises(inspection.InspectionForbidden):
            inspection.update_action_assignment(
                2, 21, "restaurant", action["id"],
                expected_version=action["version"], owner="张店长",
                due_at=due_at,
            )

        assigned = inspection.update_action_assignment(
            2, 20, "restaurant", action["id"],
            expected_version=action["version"], owner="张店长",
            due_at=due_at, plan="当天完成归位并拍照复查",
        )
        self.assertEqual("张店长", assigned["owner"])
        self.assertEqual(due_at, assigned["due_at"])
        self.assertEqual(2, assigned["version"])
        self.assertEqual("当天完成归位并拍照复查", assigned["plan"])
        synced = db.one(
            "SELECT owner,due_at FROM inspection_issue WHERE id=?",
            (issue["id"],),
        )
        self.assertEqual({"owner": "张店长", "due_at": due_at}, synced)
        event = db.one(
            "SELECT kind,payload_json,created_by FROM inspection_event "
            "WHERE action_id=? ORDER BY id DESC LIMIT 1",
            (action["id"],),
        )
        self.assertEqual("action_assignment_updated", event["kind"])
        self.assertEqual(20, event["created_by"])
        payload = db.jloads(event["payload_json"], {})
        self.assertEqual("AI建议：店长", payload["from"]["owner"])
        self.assertEqual("张店长", payload["to"]["owner"])

        with self.assertRaises(inspection.InspectionConflict):
            inspection.update_action_assignment(
                2, 20, "restaurant", action["id"],
                expected_version=action["version"], owner="过期覆盖",
                due_at=due_at,
            )

        root_updated = inspection.update_action_assignment(
            2, 1, "restaurant", action["id"],
            expected_version=assigned["version"], owner="王区域经理",
            due_at=due_at + 86400,
        )
        self.assertEqual("王区域经理", root_updated["owner"])
        self.assertEqual(assigned["plan"], root_updated["plan"])

    def test_callback_orchestrator_is_idempotent_and_keeps_io_in_main(self):
        calls = {"save": 0, "analyze": 0}

        async def save_photo(tid, visit_id, index, upload):
            calls["save"] += 1
            return photo(f"inspections/{tid}/{visit_id}/{index}.jpg", digest=f"{index + 1:x}" * 64)

        async def analyze(context, photos):
            calls["analyze"] += 1
            self.assertEqual("restaurant", context["industry_key"])
            self.assertEqual(self.branch["id"], context["branch"]["id"])
            return analysis_result(
                photos,
                {"summary": "本次未发现问题", "score": 100, "issues": []},
            )

        async def scenario():
            first = await inspection.run_inspection(
                2, 21, "restaurant", self.branch["id"],
                {"request_key": "callback-visit-0001"},
                [object(), object()],
                save_photo=save_photo,
                analyze_photos=analyze,
            )
            replay = await inspection.run_inspection(
                2, 21, "restaurant", self.branch["id"],
                {"request_key": "callback-visit-0001"},
                [object(), object()],
                save_photo=save_photo,
                analyze_photos=analyze,
            )
            return first, replay

        first, replay = asyncio.run(scenario())
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual("completed", replay["status"])
        self.assertEqual({"save": 2, "analyze": 1}, calls)

    def test_aggregate_is_scoped_and_surfaces_boss_decisions(self):
        first = self._draft(request_key="aggregate-visit-0001")
        pid = first["photos"][0]["id"]
        inspection.complete_visit(2, 21, "restaurant", first["id"], analysis_result(first["photos"], {
            "summary": "一项高风险",
            "score": 60,
            "issues": [{
                "title": "通道占用", "description": "需立即清理",
                "severity": "high", "category": "safety", "confidence": .9,
                "evidence": [{"photo_id": pid}],
                "action": {"plan": "清理", "due_days": 0},
            }],
        }))
        second = self._draft(request_key="aggregate-visit-0002")
        inspection.complete_visit(2, 21, "restaurant", second["id"], analysis_result(second["photos"], {
            "summary": "正常", "score": 100, "issues": [],
        }))

        hotel = inspection.create_branch(3, 30, "hotel", {"name": "酒店店"})
        foreign = inspection.create_visit_draft(
            3, 30, "hotel", hotel["id"],
            {"request_key": "aggregate-foreign-0001"},
            [photo("inspections/3/v9/front.jpg", digest="d" * 64)],
        )
        inspection.complete_visit(3, 30, "hotel", foreign["id"], analysis_result(foreign["photos"], {
            "summary": "外租户", "score": 10, "issues": [],
        }))

        metrics = inspection.aggregate(2, 20, "restaurant")
        self.assertEqual(2, metrics["visits"])
        self.assertEqual(80.0, metrics["average_score"])
        self.assertEqual(1, metrics["open_issues"])
        self.assertEqual(1, metrics["severity"]["high"])
        self.assertEqual(1, metrics["overdue_actions"])
        self.assertEqual(1, len(metrics["branches"]))
        self.assertEqual(self.branch["id"], metrics["branches"][0]["id"])
        self.assertEqual("华北区", metrics["regions"][0]["region"])
        self.assertEqual(80.0, metrics["regions"][0]["average_score"])
        self.assertEqual(1, metrics["regions"][0]["open_issues"])
        self.assertEqual(1, metrics["regions"][0]["overdue_actions"])
        self.assertIsNotNone(metrics["regions"][0]["last_visit_at"])
        bounded = inspection.aggregate(
            2, 20, "restaurant", branch_limit=20, region_limit=50
        )
        self.assertEqual(metrics["branches"], bounded["branches"])
        self.assertEqual(metrics["regions"], bounded["regions"])
        self.assertEqual(1, bounded["total_branches"])
        self.assertEqual(1, bounded["visited_branches"])
        self.assertEqual(1, bounded["total_regions"])
        self.assertFalse(bounded["branches_truncated"])
        self.assertFalse(bounded["regions_truncated"])
        member_metrics = inspection.aggregate(2, 21, "restaurant")
        self.assertEqual(metrics["branches"], member_metrics["branches"])
        self.assertEqual(metrics["regions"], member_metrics["regions"])
        history = inspection.list_visits(
            2, 21, "restaurant", branch_id=self.branch["id"], limit=1
        )
        self.assertEqual(1, len(history["items"]))
        self.assertIsNotNone(history["next_before_id"])
        self.assertEqual(1, len(inspection.list_branches(2, 21, "restaurant")))

    def test_visit_photos_and_issue_evidence_share_local_display_numbers(self):
        # 先在其他租户产生图片，确保真实 photo.id 不会恰好等于本次展示号。
        foreign_branch = inspection.create_branch(
            3, 30, "hotel", {"name": "编号占位店"}
        )
        inspection.create_visit_draft(
            3, 30, "hotel", foreign_branch["id"],
            {"request_key": "display-foreign-0001"},
            [photo("inspections/3/display/foreign.jpg", digest="9" * 64)],
        )
        visit = self._draft(
            request_key="display-local-0001",
            photos=[
                photo("inspections/2/display/front.jpg", digest="1" * 64),
                photo("inspections/2/display/back.jpg", digest="2" * 64),
            ],
        )
        raw_photo_id = visit["photos"][1]["id"]
        self.assertNotEqual(2, raw_photo_id)
        completed = inspection.complete_visit(2, 21, "restaurant", visit["id"], analysis_result(visit["photos"], {
            "summary": "后场需整改",
            "score": 75,
            "issues": [{
                "title": "杂物堆放", "description": "后场可见杂物",
                "severity": "medium", "category": "operations", "confidence": .9,
                "evidence": [{"photo_id": raw_photo_id, "note": "右侧"}],
                "action": {"plan": "清理", "due_days": 1},
            }],
        }))
        self.assertEqual([1, 2], [item["display_no"] for item in completed["photos"]])
        evidence = completed["issues"][0]["evidence"][0]
        self.assertEqual(raw_photo_id, evidence["photo_id"])
        self.assertEqual(2, evidence["display_no"])

    def test_detail_traces_before_reject_second_recheck_and_close_without_payloads(self):
        visit = self._draft(request_key="timeline-visit-0001")
        completed = inspection.complete_visit(2, 21, "restaurant", visit["id"], analysis_result(visit["photos"], {
            "summary": "待整改",
            "score": 70,
            "issues": [{
                "title": "通道有杂物", "description": "通道需清理",
                "severity": "high", "category": "safety", "confidence": .95,
                "evidence": [{"photo_id": visit["photos"][0]["id"]}],
                "action": {"plan": "清理并复查", "owner": "店长", "due_days": 1},
            }],
        }))
        issue = completed["issues"][0]
        action = inspection.transition_action(
            2, 21, "restaurant", issue["action"]["id"],
            expected_version=issue["action"]["version"],
            target_status="in_progress", note="开始清理",
        )
        action = inspection.transition_action(
            2, 21, "restaurant", action["id"],
            expected_version=action["version"], target_status="awaiting_recheck",
        )
        first_photo = inspection.add_recheck_photos(
            2, 21, "restaurant", action["id"],
            [photo("inspections/2/timeline/first.jpg", digest="3" * 64)],
        )[0]
        first_recheck = inspection.record_recheck(
            2, 21, "restaurant", action["id"], {
                "recommendation": "reject", "confidence": .94,
                "note": "仍有遮挡", "evidence_photo_ids": [first_photo["id"]],
            },
        )
        rejected = inspection.review_recheck(
            2, 20, "restaurant", first_recheck["id"], decision="reject",
            expected_action_version=action["version"], note="继续清理",
        )
        action = inspection.transition_action(
            2, 21, "restaurant", rejected["action"]["id"],
            expected_version=rejected["action"]["version"],
            target_status="awaiting_recheck", note="已再次整改",
        )
        second_photo = inspection.add_recheck_photos(
            2, 21, "restaurant", action["id"],
            [photo("inspections/2/timeline/second.jpg", digest="4" * 64)],
        )[0]
        second_recheck = inspection.record_recheck(
            2, 21, "restaurant", action["id"], {
                "recommendation": "close", "confidence": .96,
                "note": "通道已清空", "evidence_photo_ids": [second_photo["id"]],
            },
        )
        inspection.review_recheck(
            2, 20, "restaurant", second_recheck["id"], decision="close",
            expected_action_version=action["version"], note="人工确认通道已清空",
        )

        detail = inspection.get_visit(2, 21, "restaurant", visit["id"])
        self.assertEqual(
            [(1, "before"), (2, "recheck"), (3, "recheck")],
            [(item["display_no"], item["phase"]) for item in detail["photos"]],
        )
        rechecks = detail["issues"][0]["action"]["rechecks"]
        self.assertEqual(["rejected", "approved"], [item["status"] for item in rechecks])
        self.assertEqual([[2], [3]], [
            [photo_item["display_no"] for photo_item in item["photos"]]
            for item in rechecks
        ])
        kinds = [item["kind"] for item in detail["events"]]
        self.assertIn("visit_created", kinds)
        self.assertIn("action_transition", kinds)
        self.assertEqual(2, kinds.count("recheck_submitted"))
        self.assertEqual(2, kinds.count("recheck_reviewed"))
        self.assertTrue(all(
            set(item) == {"id", "issue_id", "action_id", "kind", "created_at"}
            for item in detail["events"]
        ))


if __name__ == "__main__":
    unittest.main()
