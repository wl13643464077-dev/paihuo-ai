"""Schema 52 巡店 HTTP 合同与文件绑定边界。"""
from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from fastapi import HTTPException, UploadFile
from starlette.requests import Request
from starlette.responses import Response

from app import auth, db, inspection, inspectionimport, inspectionstandards, main


def _prepared_photo(seed: str = "a") -> dict:
    return {
        "data": b"jpeg",
        "mime_type": "image/jpeg",
        "byte_size": 4,
        "sha256": seed * 64,
        "width": 2,
        "height": 2,
    }


class InspectionV52HTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "inspection-v52-http.db")
        db.conn()
        db.insert("tenants", {
            "id": 2,
            "name": "巡店 HTTP 测试企业",
            "balance": 20,
            "enabled": 1,
            "industries_json": "[]",
        })
        db.insert("tenants", {
            "id": 3,
            "name": "其他企业",
            "balance": 20,
            "enabled": 1,
            "industries_json": "[]",
        })
        for tenant_id, industry_key in ((2, "restaurant"), (2, "auto"), (3, "restaurant")):
            db.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                "VALUES(?,?,?,0)",
                (tenant_id, industry_key, int(industry_key == "restaurant")),
            )
        for user_id, tenant_id, role, modules in (
            (20, 2, "owner", []),
            (21, 2, "member", ["restaurant"]),
            (22, 2, "member", ["auto"]),
            (30, 3, "owner", []),
        ):
            db.insert("users", {
                "id": user_id,
                "tenant_id": tenant_id,
                "username": f"u{user_id}",
                "password_hash": "x",
                "role": role,
                "modules_json": json.dumps(modules),
                "enabled": 1,
            })
        self.owner = {
            "id": 20,
            "tenant_id": 2,
            "username": "u20",
            "role": "owner",
            "modules": [],
        }
        self.member = {
            "id": 21,
            "tenant_id": 2,
            "username": "u21",
            "role": "member",
            "modules": ["restaurant"],
        }
        auth.set_current(self.owner)
        self.branch = inspection.create_branch(
            2, 20, "restaurant", {"name": "朝阳一店", "region": "华北"}
        )
        db.execute(
            "UPDATE store_branch SET store_code='S001' WHERE id=?",
            (self.branch["id"],),
        )
        main._transient_upload_active_tenants.clear()

    def tearDown(self):
        auth.set_current(None)
        main._transient_upload_active_tenants.clear()
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _new_branch(self, number: int, *, region: str = "华北") -> int:
        branch = inspection.create_branch(
            2,
            20,
            "restaurant",
            {"name": f"测试门店 {number:03d}", "region": region},
        )
        db.execute(
            "UPDATE store_branch SET store_code=? WHERE id=?",
            (f"S{number:03d}", branch["id"]),
        )
        return int(branch["id"])

    def test_specific_routes_precede_visit_id_and_upload_has_exact_transient_limit(self):
        paths = [getattr(route, "path", "") for route in main.app.routes]
        generic = paths.index("/api/inspections/{visit_id}")
        for path in (
            "/api/inspections/branches/search",
            "/api/inspections/checklist",
            "/api/inspections/branches/import-template",
        ):
            self.assertLess(paths.index(path), generic)
        policy = main._TRANSIENT_UPLOAD_ROUTES[
            ("POST", "/api/inspections/branches/imports")
        ]
        self.assertEqual("*admin", policy[1])
        self.assertEqual(
            inspectionimport.MAX_FILE_BYTES
            + main._UPLOAD_MULTIPART_OVERHEAD_BYTES,
            policy[2],
        )

    def test_import_prebody_guard_rejects_member_and_oversize_before_parse(self):
        path = "/api/inspections/branches/imports"
        request_limit = main._TRANSIENT_UPLOAD_ROUTES[("POST", path)][2]

        def request(content_length: int) -> Request:
            async def unread_body():
                raise AssertionError("multipart body must remain unread")

            return Request({
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "headers": [
                    (b"cookie", b"cc_sess=test-session"),
                    (b"content-length", str(content_length).encode("ascii")),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("paihuo.ai", 443),
            }, receive=unread_body)

        async def exercise(user: dict, size: int):
            reached_handler = False

            async def call_next(_request):
                nonlocal reached_handler
                reached_handler = True
                return Response(status_code=204)

            with mock.patch.object(
                main.auth, "parse_session", return_value=user["id"]
            ), mock.patch.object(
                main.auth, "get_user", return_value=user
            ):
                response = await main._auth_mw(request(size), call_next)
            return response, reached_handler

        member_response, member_reached = asyncio.run(exercise(self.member, 1024))
        self.assertEqual(403, member_response.status_code)
        self.assertFalse(member_reached)

        owner_response, owner_reached = asyncio.run(
            exercise(self.owner, request_limit + 1)
        )
        self.assertEqual(413, owner_response.status_code)
        self.assertFalse(owner_reached)
        self.assertEqual(set(), main._transient_upload_active_tenants)

    def test_meta_is_bounded_and_declares_search_and_role_permissions(self):
        for number in range(2, 58):
            self._new_branch(number)
        result = main.inspection_meta("restaurant")
        self.assertLessEqual(len(result["branches"]), 20)
        self.assertEqual(20, result["branch_search"]["default_limit"])
        self.assertEqual(50, result["branch_search"]["max_limit"])
        self.assertTrue(result["branch_search"]["enabled"])
        self.assertEqual({
            "can_import_branches": True,
            "can_create_branch": True,
            "can_review": True,
        }, result["permissions"])

        auth.set_current(self.member)
        member = main.inspection_meta("restaurant")
        self.assertEqual({
            "can_import_branches": False,
            "can_create_branch": True,
            "can_review": False,
        }, member["permissions"])

    def test_list_summary_is_bounded_for_fifty_thousand_branches(self):
        connection = db.conn()
        connection.executemany(
            "INSERT INTO store_branch(tenant_id,industry_key,name,region,address,"
            "active,created_by,created_at,updated_at) VALUES(2,'restaurant',?,?,''"
            ",1,20,0,0)",
            (
                (f"规模门店 {index:05d}", f"规模区域 {index:05d}")
                for index in range(49_999)
            ),
        )
        connection.commit()
        selected_id = int(db.one(
            "SELECT MAX(id) id FROM store_branch WHERE tenant_id=2"
        )["id"])
        real_q = db.q
        real_aggregate = inspection.aggregate
        observed_queries: list[tuple[str, int]] = []
        aggregate_kwargs: list[dict] = []

        def tracked_q(sql, args=()):
            rows = real_q(sql, args)
            observed_queries.append((" ".join(str(sql).split()), len(rows)))
            return rows

        def tracked_aggregate(*args, **kwargs):
            aggregate_kwargs.append(dict(kwargs))
            return real_aggregate(*args, **kwargs)

        with mock.patch.object(
            inspection.db, "q", side_effect=tracked_q
        ), mock.patch.object(
            inspection, "aggregate", side_effect=tracked_aggregate
        ):
            result = main.inspection_list(
                industry_key="restaurant",
                branch_id=selected_id,
                limit=40,
            )
        summary = result["summary"]
        self.assertEqual(50_000, summary["total_branches"])
        self.assertEqual(0, summary["visited_branches"])
        self.assertEqual(50_000, summary["total_regions"])
        self.assertLessEqual(
            len(summary["branches"]), main._INSPECTION_RISK_BRANCH_LIMIT
        )
        self.assertLessEqual(
            len(summary["regions"]), main._INSPECTION_REGION_SUMMARY_LIMIT
        )
        self.assertIn(selected_id, {item["id"] for item in summary["branches"]})
        self.assertTrue(summary["branches_truncated"])
        self.assertTrue(summary["regions_truncated"])
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 50_000)
        branch_queries = [
            (sql, row_count) for sql, row_count in observed_queries
            if "FROM store_branch b" in sql
        ]
        self.assertTrue(branch_queries)
        self.assertTrue(any(" LIMIT " in sql for sql, _count in branch_queries))
        self.assertFalse(any(
            "ORDER BY b.region,b.name,b.id" in sql
            for sql, _count in branch_queries
        ))
        self.assertLessEqual(max(count for _sql, count in branch_queries), 50)
        self.assertLessEqual(max(count for _sql, count in observed_queries), 50)
        self.assertEqual([{
            "branch_limit": 20,
            "region_limit": 50,
            "pinned_branch_id": selected_id,
        }], aggregate_kwargs)

    def test_branch_search_is_bounded_cursor_scoped_and_off_event_loop(self):
        north_id = self._new_branch(2, region="华北")
        self._new_branch(3, region="华东")
        loop_thread = threading.get_ident()
        original_one = db.one

        def guarded_one(*args, **kwargs):
            self.assertNotEqual(loop_thread, threading.get_ident())
            return original_one(*args, **kwargs)

        async def exercise():
            with mock.patch.object(db, "one", side_effect=guarded_one):
                return await main.inspection_branch_search(
                    industry_key="restaurant",
                    q="S002",
                    region="华北",
                    limit=1,
                    before_id=None,
                )

        result = asyncio.run(exercise())
        self.assertEqual([north_id], [row["id"] for row in result["items"]])
        self.assertEqual("S002", result["items"][0]["store_code"])
        self.assertNotIn("manager_phone", result["items"][0])

        auth.set_current({
            "id": 22, "tenant_id": 2, "username": "u22",
            "role": "member", "modules": ["auto"],
        })
        with self.assertRaises(HTTPException) as forbidden:
            asyncio.run(main.inspection_branch_search("restaurant", "", "", 20, None))
        self.assertEqual(403, forbidden.exception.status_code)

    def test_checklist_returns_version_sources_and_real_null_comparisons(self):
        db.execute(
            "INSERT INTO inspection_business_value(tenant_id,industry_key,branch_id,"
            "import_id,metric_key,period_start,period_end,value,unit,source_ref,remark,"
            "created_at,updated_at) VALUES(2,'restaurant',?,1,'common.net_revenue',"
            "'2026-07-01','2026-07-31',1234.5,'CNY','audited-pos','',0,0)",
            (self.branch["id"],),
        )
        result = asyncio.run(
            main.inspection_checklist("restaurant", self.branch["id"])
        )
        self.assertEqual(inspectionstandards.CATALOG_VERSION, result["catalog_version"])
        self.assertEqual(inspectionstandards.CATALOG_AS_OF, result["as_of"])
        self.assertTrue(result["items"])
        self.assertTrue(result["capture_slots"])
        source_codes = {item["source_no"] for item in result["items"]}
        self.assertEqual(source_codes, set(result["sources"]))
        actual = next(
            item for item in result["metrics"]
            if item["metric_code"] == "common.net_revenue"
        )
        self.assertEqual(1234.5, actual["actual"])
        self.assertIsNone(actual["target"])
        self.assertIsNone(actual["benchmark"])
        missing = next(item for item in result["metrics"] if not item["availability"])
        self.assertIsNone(missing["actual"])
        self.assertIsNone(missing["previous_period"])
        self.assertIsNone(missing["same_period_last_year"])

    def test_bounded_region_average_counts_only_scored_visits(self):
        db.execute(
            "UPDATE store_branch SET region='共同区域' WHERE id=?",
            (self.branch["id"],),
        )
        second = inspection.create_branch(
            2, 20, "restaurant", {"name": "共同区域二店", "region": "共同区域"}
        )
        for index, (branch_id, score) in enumerate((
            (self.branch["id"], 0),
            (self.branch["id"], None),
            (second["id"], 100),
        )):
            db.insert("inspection_visit", {
                "tenant_id": 2,
                "industry_key": "restaurant",
                "branch_id": branch_id,
                "employee_idx": inspection.EMPLOYEE_IDX,
                "request_key": f"bounded-score-{index:04d}",
                "status": "completed",
                "score": score,
                "created_by": 20,
                "created_at": float(index + 1),
                "updated_at": float(index + 1),
            })
        bounded = inspection.aggregate(
            2, 20, "restaurant", branch_limit=20, region_limit=50
        )
        legacy = inspection.aggregate(2, 20, "restaurant")
        self.assertEqual(3, bounded["visits"])
        self.assertEqual(2, bounded["visited_branches"])
        self.assertEqual(50.0, bounded["regions"][0]["average_score"])
        self.assertEqual(
            legacy["regions"][0]["average_score"],
            bounded["regions"][0]["average_score"],
        )

    def test_import_routes_require_manager_map_safe_errors_and_do_not_cache_template(self):
        response = asyncio.run(
            main.inspection_branch_import_template("restaurant")
        )
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual("nosniff", response.headers["x-content-type-options"])

        auth.set_current(self.member)
        with self.assertRaises(HTTPException) as denied:
            asyncio.run(main.inspection_branch_import_template("restaurant"))
        self.assertEqual(403, denied.exception.status_code)
        unread = UploadFile(filename="never-read.xlsx", file=io.BytesIO(b"unused"))
        with self.assertRaises(HTTPException) as denied_upload:
            asyncio.run(main.inspection_branch_import_preview(
                industry_key="restaurant",
                request_key="member-import-0001",
                file=unread,
            ))
        self.assertEqual(403, denied_upload.exception.status_code)

        auth.set_current(self.owner)
        cases = {
            "SCOPE_FORBIDDEN": 403,
            "IMPORT_NOT_FOUND": 404,
            "REQUEST_KEY_CONFLICT": 409,
            "IMPORT_SOURCE_ACTIVE": 409,
            "IMPORT_PREVIEW_EXPIRED": 409,
            "IMPORT_PREVIEW_QUOTA_EXCEEDED": 429,
            "ROW_INVALID": 400,
        }
        for code, expected in cases.items():
            with self.subTest(code=code), mock.patch.object(
                main,
                "_run_db_safely",
                new=mock.AsyncMock(
                    side_effect=inspectionimport.ImportContractError(code, "安全错误")
                ),
            ):
                upload = UploadFile(filename="valid.xlsx", file=io.BytesIO(b"xlsx"))
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(main.inspection_branch_import_preview(
                        industry_key="restaurant",
                        request_key="owner-import-0001",
                        file=upload,
                    ))
                self.assertEqual(expected, caught.exception.status_code)
                self.assertEqual("安全错误", caught.exception.detail)

    def test_import_preview_reads_at_most_16mb_and_submits_via_safe_db_runner(self):
        too_large = UploadFile(
            filename="large.xlsx",
            file=io.BytesIO(b"x" * (inspectionimport.MAX_FILE_BYTES + 1)),
        )
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.inspection_branch_import_preview(
                industry_key="restaurant",
                request_key="large-import-0001",
                file=too_large,
            ))
        self.assertEqual(413, caught.exception.status_code)

        runner = mock.AsyncMock(return_value={"import_id": 7, "status": "previewed"})
        upload = UploadFile(filename="branches.xlsx", file=io.BytesIO(b"xlsx"))
        with mock.patch.object(main, "_run_db_safely", new=runner):
            result = asyncio.run(main.inspection_branch_import_preview(
                industry_key="restaurant",
                request_key="owner-import-0002",
                file=upload,
            ))
        self.assertEqual(7, result["import_id"])
        args = runner.await_args.args
        self.assertIs(inspectionimport.preview_import, args[0])
        self.assertEqual((2, 20, "restaurant", "owner-import-0002", "branches.xlsx", b"xlsx"), args[1:])

    def test_import_detail_forwards_bounded_row_page_filters(self):
        seen = {}

        async def runner(fn, *args, **kwargs):
            if fn is main._inspection_manager_scope:
                return "restaurant", ["restaurant"]
            if fn is inspectionimport.get_import:
                seen["args"] = args
                seen["kwargs"] = kwargs
                return {
                    "import_id": 9,
                    "rows": [],
                    "limit": kwargs["limit"],
                    "cursor": kwargs["cursor"],
                    "next_cursor": None,
                    "has_more": False,
                }
            self.fail(f"unexpected db function: {fn}")

        with mock.patch.object(main.db, "arun", side_effect=runner):
            result = asyncio.run(main.inspection_branch_import_detail(
                9,
                "restaurant",
                limit=125,
                cursor="100004",
                errors_only=True,
                row_kind="business",
            ))
        self.assertEqual(9, result["import_id"])
        self.assertEqual((2, 20, 9, "restaurant"), seen["args"])
        self.assertEqual({
            "limit": 125,
            "cursor": "100004",
            "errors_only": True,
            "row_kind": "business",
        }, seen["kwargs"])

    def test_create_binds_each_file_slot_and_passes_strict_contract_to_service(self):
        slots = [
            item["slot_code"]
            for item in inspectionstandards.capture_slots("restaurant")
            if item["required"]
        ]
        prepared = [_prepared_photo(format(index + 1, "x")) for index in range(len(slots))]
        seen: dict = {}

        async def db_runner(fn, *args, **kwargs):
            if fn is inspection.create_visit_shell:
                seen["raw"] = args[4]
                return {
                    "id": 41,
                    "status": "preparing",
                    "task_id": None,
                    "photos": [],
                    "branch": {"id": self.branch["id"], "name": "朝阳一店", "region": "华北"},
                }
            if fn is main._assert_inspection_http_replay_contract:
                return None
            if fn is main._abandon_empty_inspection_shell:
                return False
            self.fail(f"unexpected db runner function: {fn}")

        async def file_runner(fn, *args, **kwargs):
            if fn is main._cleanup_empty_shell_inspection_files:
                return 0
            if fn is main._cleanup_unreferenced_inspection_images:
                return None
            if fn is main._store_inspection_images:
                seen["stored"] = args[2]
                return [{
                    key: value for key, value in item.items() if key != "data"
                } | {"storage_key": f"inspections/2/41/{index:032x}.jpg"}
                    for index, item in enumerate(args[2], start=1)]
            self.fail(f"unexpected file runner function: {fn}")

        async def activate(_fn, *args, **kwargs):
            seen["activated_photos"] = args[4]
            return {"created": True, "inspection_id": 41, "task_id": 99}

        observations = {
            "metrics": [{
                "metric_code": "common.net_revenue",
                "value": 123.5,
                "unit": "CNY",
            }],
            "checklist": [],
        }
        with mock.patch.object(
            main, "_prepare_inspection_uploads", new=mock.AsyncMock(return_value=prepared)
        ), mock.patch.object(
            main, "_run_db_safely", new=mock.AsyncMock(side_effect=db_runner)
        ), mock.patch.object(
            main, "_run_inspection_file_safely", new=mock.AsyncMock(side_effect=file_runner)
        ), mock.patch.object(
            main, "_run_db_then_start_worker_safely", new=mock.AsyncMock(side_effect=activate)
        ):
            result = asyncio.run(main.inspection_create(
                branch_id=self.branch["id"],
                visit_at="",
                scope="消防与后厨",
                request_key="strict-route-0001",
                industry_key="restaurant",
                files=[object() for _ in slots],
                file_slots=slots,
                template_version=inspectionstandards.CATALOG_VERSION,
                observations_json=json.dumps(observations),
            ))
        self.assertEqual(41, result["inspection_id"])
        self.assertTrue(seen["raw"]["require_checklist"])
        self.assertEqual(slots, seen["raw"]["file_slots"])
        self.assertEqual(observations, seen["raw"]["observations"])
        self.assertEqual(slots, [item["capture_slot"] for item in seen["stored"]])
        self.assertEqual(slots, [item["capture_slot"] for item in seen["activated_photos"]])

    def test_create_replay_does_not_store_images(self):
        slots = [
            item["slot_code"]
            for item in inspectionstandards.capture_slots("restaurant")
            if item["required"]
        ]
        shell = {
            "id": 42,
            "status": "analyzing",
            "task_id": 100,
            "photos": [{"capture_slot": slot} for slot in slots],
        }
        with mock.patch.object(
            main,
            "_prepare_inspection_uploads",
            new=mock.AsyncMock(return_value=[_prepared_photo() for _ in slots]),
        ), mock.patch.object(
            main,
            "_run_db_safely",
            new=mock.AsyncMock(return_value=shell),
        ), mock.patch.object(
            main,
            "_run_inspection_file_safely",
            new=mock.AsyncMock(side_effect=AssertionError("replay must not store files")),
        ):
            result = asyncio.run(main.inspection_create(
                branch_id=self.branch["id"],
                visit_at="",
                scope="",
                request_key="strict-route-replay-0001",
                industry_key="restaurant",
                files=[object() for _ in slots],
                file_slots=slots,
                template_version=inspectionstandards.CATALOG_VERSION,
                observations_json='{"metrics":[],"checklist":[]}',
            ))
        self.assertFalse(result["created"])
        self.assertEqual(100, result["task_id"])

    def test_create_replay_rejects_changed_observations_before_file_write(self):
        slots = [
            item["slot_code"]
            for item in inspectionstandards.capture_slots("restaurant")
            if item["required"]
        ]
        prepared = [
            {**_prepared_photo(format(index + 1, "x")), "capture_slot": slot}
            for index, slot in enumerate(slots)
        ]
        request_key = "strict-route-conflict-0001"
        original_raw = {
            "request_key": request_key,
            "visit_at": None,
            "note": "原检查重点",
            "require_checklist": True,
            "template_version": inspectionstandards.CATALOG_VERSION,
            "file_slots": slots,
            "observations": {"metrics": [], "checklist": []},
        }
        shell = inspection.create_visit_shell(
            2, 20, "restaurant", self.branch["id"], original_raw
        )
        photo_records = [{
            key: value for key, value in item.items() if key != "data"
        } | {
            "storage_key": f"inspections/2/{shell['id']}/{index:032x}.jpg",
        } for index, item in enumerate(prepared, start=1)]
        inspection.attach_visit_photos(
            2, 20, "restaurant", shell["id"], photo_records
        )
        db.execute(
            "UPDATE inspection_visit SET task_id=100 WHERE id=?",
            (shell["id"],),
        )
        changed = {
            "metrics": [],
            "checklist": [{
                "item_code": inspectionstandards.effective_checklist("restaurant")[0]["item_code"],
                "value": True,
            }],
        }
        with mock.patch.object(
            main,
            "_prepare_inspection_uploads",
            new=mock.AsyncMock(return_value=[
                {key: value for key, value in item.items() if key != "capture_slot"}
                for item in prepared
            ]),
        ), mock.patch.object(
            main,
            "_run_inspection_file_safely",
            new=mock.AsyncMock(side_effect=AssertionError("conflict must not write files")),
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.inspection_create(
                    branch_id=self.branch["id"],
                    visit_at="",
                    scope="原检查重点",
                    request_key=request_key,
                    industry_key="restaurant",
                    files=[object() for _ in slots],
                    file_slots=slots,
                    template_version=inspectionstandards.CATALOG_VERSION,
                    observations_json=json.dumps(changed),
                ))
        self.assertEqual(409, caught.exception.status_code)

    def test_prompt_gets_frozen_standards_but_excludes_business_and_employee_data(self):
        slot = inspectionstandards.capture_slots("restaurant")[0]
        industry_item = next(
            item for item in inspectionstandards.effective_checklist("restaurant")
            if item["item_code"].startswith("restaurant.")
        )
        industry_item["source_url"] = "https://PRIVATE_SOURCE_URL/full-text"
        visit = {
            "industry_key": "restaurant",
            "branch": {
                "id": self.branch["id"],
                "name": "朝阳一店",
                "region": "华北",
                "address": "朝阳路 1 号",
                "manager_name": "PRIVATE_MANAGER_NAME",
                "manager_phone": "13800138000",
            },
            "request_key": "prompt-slot-0001",
            "visit_at": 1,
            "scope": "消防通道",
            "standard_snapshot": {
                "template_key": "restaurant",
                "template_version": inspectionstandards.CATALOG_VERSION,
                "as_of": inspectionstandards.CATALOG_AS_OF,
                "items": [industry_item],
                "capture_slots": [slot],
                "metrics": [{
                    "metric_code": "PRIVATE_METRIC_DEFINITION",
                    "formula": "PRIVATE_METRIC_FORMULA",
                }],
                "employee_rows": "PRIVATE_EMPLOYEE_TABLE_BODY",
                "sources": {"private": {
                    "url": "https://PRIVATE_REGISTRY_URL/full-text",
                }},
            },
            "observations": {
                "metrics": [{"metric_code": "common.net_revenue", "value": 98765.5, "unit": "CNY"}],
                "checklist": [{"item_code": "common.fire_exit", "value": "PRIVATE_RECORD_BODY"}],
            },
            "photos": [{
                "id": 73,
                "display_no": 1,
                "phase": "before",
                "caption": "",
                "capture_slot": slot["slot_code"],
            }],
        }
        with mock.patch.object(
            main.registry,
            "context_block",
            side_effect=AssertionError("巡店视觉模型不得读取企业档案或知识库正文"),
        ):
            bundle = main._inspection_prompt_bundle(2, visit)
        self.assertIn(slot["label"], bundle.user)
        self.assertIn("【本次冻结巡店检查标准】", bundle.system)
        self.assertIn(industry_item["item_code"], bundle.system)
        self.assertIn(industry_item["label"], bundle.system)
        self.assertIn(slot["label"], bundle.system)
        self.assertLess(
            bundle.system.index("【本次冻结巡店检查标准】"),
            bundle.system.index(main._INSPECTION_CONTRACT_MARKER),
        )
        for secret in (
            "98765.5", "PRIVATE_RECORD_BODY", "PRIVATE_MANAGER_NAME", "13800138000",
            "PRIVATE_METRIC_DEFINITION", "PRIVATE_METRIC_FORMULA",
            "PRIVATE_EMPLOYEE_TABLE_BODY", "PRIVATE_SOURCE_URL",
            "PRIVATE_REGISTRY_URL",
        ):
            self.assertNotIn(secret, bundle.user)
            self.assertNotIn(secret, bundle.system)


if __name__ == "__main__":
    unittest.main()
