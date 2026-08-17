import json
import os
import tempfile
import unittest
import asyncio
import sys
import types
import io
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app import auth, avatar, db, llm, providers


class UpgradeSecurityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        self.old_public_dir = avatar.PUBLIC_DIR
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "upgrade-security.db")
        avatar.PUBLIC_DIR = os.path.join(self.tmp.name, "public")
        os.makedirs(avatar.PUBLIC_DIR, exist_ok=True)
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台"})
        db.insert("tenants", {"id": 2, "name": "租户甲"})
        db.insert("tenants", {"id": 3, "name": "租户乙"})
        auth.set_current({
            "id": 20, "tenant_id": 2, "username": "owner-a",
            "role": "owner", "modules": ["avatar", "content", "library"],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        avatar.PUBLIC_DIR = self.old_public_dir
        self.tmp.cleanup()

    def test_file_path_is_canonicalized_before_tenant_lookup_and_unknown_is_denied(self):
        from app import main

        for unsafe in (
            "/files/job1/../job2/secret.png",
            "/files/job1/%2e%2e/job2/secret.png",
            "/files/job1//secret.png",
            r"/files/job1\..\job2\secret.png",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(HTTPException) as caught:
                main._canonical_file_path(unsafe)
            self.assertEqual(400, caught.exception.status_code)

        self.assertEqual(
            "/files/job12/cover.png",
            main._canonical_file_path("/files/job12/cover.png"),
        )
        self.assertEqual(0, main._file_owner_tid("/files/unknown/secret.bin"))

        own_job = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "甲"}),
        })
        foreign_job = db.insert("job", {
            "tenant_id": 3,
            "brief_json": json.dumps({"direction": "乙"}),
        })
        self.assertEqual(2, main._file_owner_tid(f"/files/job{own_job}/cover.png"))
        self.assertEqual(3, main._file_owner_tid(f"/files/job{foreign_job}/cover.png"))

    def test_inspection_file_keeps_industry_scope_inside_same_tenant(self):
        from app import assetfiles, main

        db.conn().executemany(
            "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
            "VALUES(2,?,?,0)",
            (("restaurant", 1), ("auto", 0)),
        )
        branch = db.insert("store_branch", {
            "tenant_id": 2,
            "industry_key": "auto",
            "name": "汽车门店",
        })
        visit = db.insert("inspection_visit", {
            "tenant_id": 2,
            "industry_key": "auto",
            "branch_id": branch,
            "status": "completed",
        })
        storage_key = f"inspections/2/{visit}/" + "a" * 32 + ".jpg"
        db.conn().execute(
            "INSERT INTO inspection_photo(tenant_id,visit_id,storage_key,"
            "mime_type,byte_size,sha256,phase,created_at) VALUES(?,?,?,?,?,?,?,0)",
            (2, visit, storage_key, "image/jpeg", 3, "b" * 64, "before"),
        )
        db.conn().commit()
        path = "/files/" + storage_key
        self.assertEqual("auto", assetfiles.file_required_module(path))

        async def request_as(user):
            request = Request({
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "headers": [(b"cookie", b"cc_sess=test")],
                "client": ("127.0.0.1", 12345),
                "server": ("paihuo.ai", 443),
            })

            async def next_handler(_request):
                return Response(status_code=200)

            with patch.object(auth, "parse_session", return_value=user["id"]), \
                    patch.object(auth, "get_user", return_value=user):
                return await main._auth_mw(request, next_handler)

        member = {
            "id": 20, "tenant_id": 2, "username": "restaurant-member",
            "role": "member", "modules": ["restaurant"], "enabled": 1,
            "must_change_password": 0,
        }
        denied = asyncio.run(request_as(member))
        self.assertEqual(403, denied.status_code)
        allowed = asyncio.run(request_as({**member, "modules": ["restaurant", "auto"]}))
        self.assertEqual(200, allowed.status_code)
        owner = asyncio.run(request_as({**member, "role": "owner", "modules": []}))
        self.assertEqual(200, owner.status_code)
        db.execute(
            "DELETE FROM tenant_industry WHERE tenant_id=2 AND industry_key='auto'"
        )
        revoked_owner = asyncio.run(request_as({
            **member,
            "role": "owner",
            "modules": [],
        }))
        self.assertEqual(403, revoked_owner.status_code)

    def test_every_private_file_family_declares_its_narrowest_module(self):
        from app import assetfiles, main

        content_job = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "私密内容"}),
        })
        avatar_job = db.insert("avatar_job", {
            "tenant_id": 2,
            "params_json": "{}",
        })
        tv_job = db.insert("tv_job", {
            "tenant_id": 2,
            "params_json": "{}",
        })
        pub_task = db.insert("pub_task", {
            "tenant_id": 2,
            "platform": "xhs",
            "payload_json": "{}",
        })
        avatar_name = "c" * 32 + ".mp3"
        with open(os.path.join(avatar.PUBLIC_DIR, avatar_name), "wb") as handle:
            handle.write(b"voice-preview")
        avatar.remember_asset(avatar_name, "voice", 2)

        cases = {
            f"/files/job{content_job}/cover.png": (2, "content"),
            f"/files/avatar/avatar_{avatar_job}.mp4": (2, "avatar"),
            "/files/tvclips/2/clip.mp4": (2, "content"),
            f"/files/tv/tv_{tv_job}.mp4": (2, "content"),
            f"/files/pub/fail_{pub_task}.png": (2, "content"),
            "/files/tools/2/product.png": (2, "content"),
        }
        for path, (tenant_id, module) in cases.items():
            with self.subTest(path=path):
                scope = assetfiles.file_access_scope(path)
                self.assertEqual(tenant_id, scope["tenant_id"])
                self.assertEqual(module, scope["required_module"])
                self.assertEqual(module, assetfiles.file_required_module(path))

        avatar_scope = main._file_access_scope(
            f"/files/avatar-public/{avatar_name}"
        )
        self.assertEqual(2, avatar_scope["tenant_id"])
        self.assertEqual("avatar", avatar_scope["required_module"])

        unknown = assetfiles.file_access_scope("/files/unknown/secret.bin")
        self.assertEqual(0, unknown["tenant_id"])
        self.assertFalse(unknown.get("required_module"))

    def test_private_file_middleware_requires_exact_tenant_and_member_module(self):
        from app import main

        content_job = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "企业私密"}),
        })
        avatar_name = "d" * 32 + ".mp3"
        with open(os.path.join(avatar.PUBLIC_DIR, avatar_name), "wb") as handle:
            handle.write(b"voice-preview")
        avatar.remember_asset(avatar_name, "voice", 2)

        async def request_as(path, user):
            request = Request({
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "headers": [(b"cookie", b"cc_sess=test")],
                "client": ("127.0.0.1", 12345),
                "server": ("paihuo.ai", 443),
            })

            async def next_handler(_request):
                return Response(status_code=200)

            with patch.object(
                auth,
                "parse_session",
                return_value=(user or {}).get("id"),
            ), patch.object(auth, "get_user", return_value=user):
                return await main._auth_mw(request, next_handler)

        job_path = f"/files/job{content_job}/cover.png"
        avatar_path = f"/files/avatar-public/{avatar_name}"
        member = {
            "id": 20,
            "tenant_id": 2,
            "username": "member-a",
            "role": "member",
            "modules": [],
            "enabled": 1,
            "must_change_password": 0,
        }
        self.assertEqual(
            403,
            asyncio.run(request_as(job_path, member)).status_code,
        )
        self.assertEqual(
            200,
            asyncio.run(request_as(
                job_path, {**member, "modules": ["content"]}
            )).status_code,
        )
        self.assertEqual(
            403,
            asyncio.run(request_as(
                avatar_path, {**member, "modules": ["content"]}
            )).status_code,
        )
        self.assertEqual(
            200,
            asyncio.run(request_as(
                avatar_path, {**member, "modules": ["avatar"]}
            )).status_code,
        )

        owner = {**member, "role": "owner", "modules": []}
        self.assertEqual(
            200,
            asyncio.run(request_as(job_path, owner)).status_code,
        )

        # 平台 root/命名 boss 的看板能跨租户看结构化指标，但不能直读原文附件。
        foreign_boss = {
            **member,
            "id": 1,
            "tenant_id": 1,
            "username": "boss",
            "role": "root",
            "modules": ["content", "avatar"],
        }
        self.assertEqual(
            403,
            asyncio.run(request_as(job_path, foreign_boss)).status_code,
        )
        same_tenant_root = {**foreign_boss, "tenant_id": 2}
        self.assertEqual(
            200,
            asyncio.run(request_as(job_path, same_tenant_root)).status_code,
        )

        # 供外部生成供应商拉取的 /pub 端点不属于登录态 /files 预览门，
        # 保留既有的公开音色/素材传输语义。
        self.assertEqual(
            200,
            asyncio.run(request_as(f"/pub/{avatar_name}", None)).status_code,
        )
        self.assertEqual(
            401,
            asyncio.run(request_as(avatar_path, None)).status_code,
        )

    def test_meta_exposes_loaded_department_counts_without_internal_employee_details(self):
        from app import departments, main

        departments._cache = [{
            "key": "test",
            "name": "测试部门",
            "employees": [{"idx": 9001}, {"idx": 9002}],
        }]
        try:
            result = main.meta()
        finally:
            departments._cache = None
        self.assertEqual(1, result["departments_loaded"])
        self.assertEqual(2, result["department_employees_loaded"])

    def test_public_capability_tokens_use_full_sha256_hmac(self):
        from app import main, mplayout

        with patch.object(auth, "_secret", return_value=b"s" * 64):
            self.assertEqual(64, len(main._guest_sign(7)))
            signed = mplayout.sign_file("job7/media.png")
            signature = signed.split("/")[2]
            self.assertEqual(64, len(signature))
            self.assertTrue(mplayout.verify_file(signature, "job7/media.png"))
            self.assertFalse(
                mplayout.verify_file(signature[:20], "job7/media.png")
            )

    def test_wechat_layout_escapes_raw_html_and_rejects_untrusted_images(self):
        from app import mplayout

        rendered = mplayout.render(
            "# 标题\n"
            "<script>alert(document.cookie)</script>\n"
            "<img src=x onerror=alert(1)>\n"
            "![恶意图](javascript:alert(1))\n"
            "[恶意链接](javascript:alert(1))",
            "orange",
        )
        self.assertNotIn("<script", rendered.lower())
        self.assertNotIn("onerror", rendered.lower())
        self.assertNotIn("javascript:", rendered.lower())

    def test_pdf_export_blocks_raw_html_local_files_and_network_fetches(self):
        from app import export

        rendered = export._md_html(
            '<img src="file:///etc/passwd">'
            '<script>alert(1)</script>'
            '![remote](http://127.0.0.1/secret)'
        )
        self.assertNotIn("<img", rendered.lower())
        self.assertNotIn("<script", rendered.lower())
        self.assertNotIn("file://", rendered.lower())

        observed = {}

        class FakeHTML:
            def __init__(self, **kwargs):
                observed.update(kwargs)

            def write_pdf(self):
                return b"%PDF-fixture"

        with patch.dict(
            sys.modules,
            {"weasyprint": types.SimpleNamespace(HTML=FakeHTML)},
        ):
            self.assertEqual(
                b"%PDF-fixture",
                export.md_to_pdf('<img src="file:///etc/passwd">'),
            )
        self.assertIn("url_fetcher", observed)
        with self.assertRaises(ValueError):
            observed["url_fetcher"]("file:///etc/passwd")
        with self.assertRaises(ValueError):
            observed["url_fetcher"]("http://127.0.0.1/metadata")

    def test_avatar_assets_require_uuid_filename_existing_file_and_current_tenant(self):
        from app import main

        own_name = "a" * 32 + ".jpg"
        foreign_name = "b" * 32 + ".jpg"
        for name in (own_name, foreign_name):
            with open(os.path.join(avatar.PUBLIC_DIR, name), "wb") as f:
                f.write(b"fixture")

        avatar.remember_asset(own_name, "photo")
        auth.set_current({
            "id": 30, "tenant_id": 3, "username": "owner-b",
            "role": "owner", "modules": ["avatar"],
        })
        avatar.remember_asset(foreign_name, "photo")
        auth.set_current({
            "id": 20, "tenant_id": 2, "username": "owner-a",
            "role": "owner", "modules": ["avatar"],
        })

        self.assertEqual(
            own_name,
            main._avatar_asset_name(own_name, "photo_name", {"photo"}),
        )
        for invalid in (
            "../../../../root/.ssh/id_rsa",
            "/etc/passwd",
            foreign_name,
            "c" * 32 + ".jpg",
            "not-a-uuid.jpg",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(HTTPException):
                main._avatar_asset_name(invalid, "photo_name", {"photo"})

    def test_provider_errors_share_the_engine_retry_contract(self):
        self.assertTrue(issubclass(providers.ProviderError, llm.LLMError))

    def test_force_review_is_honored_without_exposing_internal_approval_config(self):
        from app import main
        from app.engine import engine
        from app.skills import registry

        publish_idx = next(s["idx"] for s in registry.STATIONS
                           if s["approval"] == registry.APPROVAL_FORCE)
        self.assertTrue(engine.needs_review(publish_idx, "fullauto"))
        public = main._public_station(registry.BY_IDX[publish_idx])
        self.assertNotIn("approval", public)
        self.assertNotIn("skill", public)
        self.assertNotIn("duty", public)

    def test_public_expert_fallback_never_reuses_internal_duty(self):
        from app import departments, main

        # Schema54 的公开名片必须来自受信岗位身份；用真实在岗员工触发
        # 空 intro 回退，仍然验证内部岗位手册不会被拿来拼公开介绍。
        expert = dict(next(iter(departments.specialists().values())))
        internal_duty = expert["duty"]
        expert["intro"] = ""
        public = main._public_expert(expert)

        self.assertTrue(public["intro"])
        self.assertNotIn(internal_duty, public["intro"])

    def test_module_permissions_hide_content_and_block_library_and_avatar_side_effects(self):
        from app import main

        jid = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "企业机密"}),
        })
        db.insert("knowledge", {
            "tenant_id": 2, "title": "机密知识", "content": "不可泄露",
        })
        db.insert("avatar_job", {
            "tenant_id": 2, "params_json": "{}",
        })
        tid = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": 0,
            "brief_json": "{}",
            "output_md": "# 内部交付",
        })
        auth.set_current({
            "id": 21, "tenant_id": 2, "username": "member",
            "role": "member", "modules": [],
        })

        self.assertEqual([], main.state()["jobs"])
        for call in (
            main.knowledge_list,
            main.avatar_jobs,
            lambda: main.job_detail(jid),
            lambda: main.task_to_knowledge(tid),
            main.library_export,
        ):
            with self.subTest(call=call), self.assertRaises(HTTPException) as caught:
                call()
            self.assertEqual(403, caught.exception.status_code)

        auth.set_current({
            "id": 20, "tenant_id": 2, "username": "owner",
            "role": "owner", "modules": [],
        })
        self.assertEqual(1, len(main.knowledge_list()))
        self.assertEqual(1, len(main.avatar_jobs()))
        self.assertEqual(jid, main.job_detail(jid)["id"])

    def test_file_parser_requires_an_authenticated_work_module_not_library_specifically(self):
        from app import main

        auth.set_current({
            "id": 21,
            "tenant_id": 2,
            "username": "content-member",
            "role": "member",
            "modules": ["content"],
        })
        self.assertIsNone(main._need_any_work_module())

        auth.set_current({
            "id": 22,
            "tenant_id": 2,
            "username": "blocked-member",
            "role": "member",
            "modules": [],
        })
        with self.assertRaises(HTTPException) as caught:
            main._need_any_work_module()
        self.assertEqual(403, caught.exception.status_code)

    def test_knowledge_list_is_lightweight_and_detail_is_tenant_scoped(self):
        from app import main

        long_content = "企业沉淀正文" * 5000
        kid = db.insert("knowledge", {
            "tenant_id": 2,
            "title": "长文档",
            "content": long_content,
            "tags_json": json.dumps(["运营"]),
            "meta_json": json.dumps({"summary": "摘要"}),
        })
        foreign = db.insert("knowledge", {
            "tenant_id": 3,
            "title": "其他企业资料",
            "content": "不可见",
        })

        rows = main.knowledge_list()
        self.assertEqual([kid], [row["id"] for row in rows])
        self.assertNotIn("content", rows[0])
        self.assertEqual(["运营"], rows[0]["tags"])

        detail = main.knowledge_detail(kid)
        self.assertEqual(long_content, detail["content"])
        self.assertEqual("摘要", detail["meta"]["summary"])
        with self.assertRaises(HTTPException) as caught:
            main.knowledge_detail(foreign)
        self.assertEqual(404, caught.exception.status_code)

    def test_internal_price_costs_are_visible_only_to_boss_root(self):
        from app import main

        owner_prices = main.billing_get()["prices"]
        self.assertTrue(owner_prices)
        self.assertTrue(all("cost" not in row for row in owner_prices.values()))

        auth.set_current({
            "id": 1,
            "tenant_id": 1,
            "username": "boss",
            "role": "root",
            "modules": [],
        })
        boss_prices = main.billing_get()["prices"]
        self.assertTrue(any("cost" in row for row in boss_prices.values()))

    def test_station_skill_workflow_and_cost_fields_are_boss_only(self):
        from app import main

        job_id = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "公开交付"}),
            "status": "awaiting_review",
        })
        db.insert("station_run", {
            "job_id": job_id,
            "station_idx": 0,
            "skill_id": "horizon-internal-v2",
            "version": 1,
            "status": "awaiting_review",
            "output_json": json.dumps({"topics": [{"title": "公开结果"}]}),
            "steps_json": json.dumps([
                {"k": "search", "l": "内部检索方法与关键词"}
            ]),
            "tokens": 123,
            "cost_usd": 0.45,
            "latency_ms": 4567,
        })

        member_run = main.job_detail(job_id)["runs"][0]
        for private in (
            "skill_id", "steps", "steps_json", "tokens", "cost_usd", "latency_ms"
        ):
            self.assertNotIn(private, member_run)
        self.assertIn(
            "公开结果",
            json.dumps(member_run["output"], ensure_ascii=False),
        )
        member_version = main.versions(job_id, 0)[0]
        self.assertNotIn("skill_id", member_version)
        self.assertNotIn("steps", member_version)

        auth.set_current({
            "id": 1,
            "tenant_id": 2,
            "username": "boss",
            "role": "root",
            "modules": ["content"],
        })
        boss_run = main.job_detail(job_id)["runs"][0]
        self.assertEqual("horizon-internal-v2", boss_run["skill_id"])
        self.assertEqual("内部检索方法与关键词", boss_run["steps"][0]["l"])

    def test_upload_reader_stops_immediately_after_the_byte_limit(self):
        from app import main

        class EndlessUpload:
            def __init__(self):
                self.calls = []

            async def read(self, size):
                self.calls.append(size)
                return b"x" * size

        upload = EndlessUpload()
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main._read_limited(upload, 1024, "too large"))
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual(1025, sum(upload.calls))
        self.assertTrue(all(size > 0 for size in upload.calls))

    def test_office_zip_bomb_is_rejected_before_document_parser_runs(self):
        from app import main

        payload = io.BytesIO()
        with zipfile.ZipFile(
            payload, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("word/document.xml", b"0" * (2 * 1024 * 1024))
        self.assertLess(len(payload.getvalue()), 20 * 1024)
        with self.assertRaisesRegex(ValueError, "压缩比异常"):
            main._validate_office_archive(payload.getvalue())

    def test_document_parser_runs_in_a_bounded_killable_subprocess(self):
        from app import main

        source = Path(main.__file__).read_text(encoding="utf-8")
        worker = (
            Path(main.__file__).with_name("docparse_worker.py")
            .read_text(encoding="utf-8")
        )
        self.assertNotIn("preexec_fn=", source)
        self.assertIn("timeout=22", source)
        self.assertIn("_apply_resource_limits()", worker)
        self.assertIn("RLIMIT_AS", worker)
        self.assertIn("RLIMIT_CPU", worker)
        self.assertIn("RLIMIT_NPROC", worker)

    def test_document_parser_limits_global_and_per_tenant_concurrency(self):
        from app import main

        async def scenario():
            first_entered = asyncio.Event()
            second_entered = asyncio.Event()
            release = asyncio.Event()

            async def hold(tid, entered):
                async with main._document_parse_slot(tid):
                    entered.set()
                    await release.wait()

            first = asyncio.create_task(hold(2, first_entered))
            await first_entered.wait()
            with self.assertRaises(HTTPException) as same_tenant:
                async with main._document_parse_slot(2):
                    pass
            self.assertEqual(429, same_tenant.exception.status_code)

            second = asyncio.create_task(hold(3, second_entered))
            await second_entered.wait()
            with self.assertRaises(HTTPException) as global_limit:
                async with main._document_parse_slot(4):
                    pass
            self.assertEqual(429, global_limit.exception.status_code)
            release.set()
            await asyncio.gather(first, second)

            async with main._document_parse_slot(2):
                pass
            self.assertEqual(set(), main._DOC_PARSE_ACTIVE)

        asyncio.run(scenario())

    def test_free_supplier_ai_guard_enforces_quota_and_tenant_single_flight(self):
        from app import main

        async def scenario():
            main._free_ai_usage.clear()
            main._free_ai_active_tenants.clear()
            with patch.dict(
                main._FREE_AI_ACTION_DAILY,
                {"parse-image": 1},
                clear=True,
            ):
                async with main._free_ai_slot("parse-image"):
                    with self.assertRaises(HTTPException) as concurrent:
                        async with main._free_ai_slot("parse-image"):
                            pass
                    self.assertEqual(429, concurrent.exception.status_code)

                with self.assertRaises(HTTPException) as exhausted:
                    async with main._free_ai_slot("parse-image"):
                        pass
                self.assertEqual(429, exhausted.exception.status_code)

        asyncio.run(scenario())

    def test_image_parse_quota_blocks_supplier_before_upload_or_model_call(self):
        from unittest.mock import AsyncMock
        from app import main

        class Upload:
            filename = "receipt.png"

        async def scenario():
            main._free_ai_usage.clear()
            main._free_ai_active_tenants.clear()
            read = AsyncMock(return_value=b"image")
            vision = AsyncMock(return_value={"text": "不应调用"})
            with patch.dict(
                main._FREE_AI_ACTION_DAILY,
                {"parse-image": 1},
                clear=True,
            ):
                async with main._free_ai_slot("parse-image"):
                    pass
                with patch.object(main, "_read_limited", read), patch.object(
                    providers, "call_vision", vision
                ):
                    with self.assertRaises(HTTPException) as exhausted:
                        await main.parse_file(Upload())
            self.assertEqual(429, exhausted.exception.status_code)
            read.assert_not_awaited()
            vision.assert_not_awaited()

        asyncio.run(scenario())

    def test_all_free_supplier_ai_endpoints_enter_the_shared_guard(self):
        import inspect
        from app import main

        guarded = (
            (main.company_distill, "company-distill"),
            (main.parse_file, "parse-image"),
            (main.meeting_suggest, "meeting-suggest"),
            (main.distill, "profile-distill"),
            (main.experts_match, "expert-match"),
            (main.task_create, "task-preflight"),
        )
        for handler, action in guarded:
            with self.subTest(handler=handler.__name__, action=action):
                self.assertIn(
                    f'_free_ai_slot("{action}")',
                    inspect.getsource(handler),
                )

    def test_content_brief_and_schedule_metadata_reject_stored_xss(self):
        from app import main

        with self.assertRaises(HTTPException):
            main._validated_mode('<img src=x onerror=alert(1)>')
        with self.assertRaises(HTTPException):
            main._validated_brief({
                "direction": "正常任务",
                "platforms": ['<img src=x onerror=alert(1)>'],
            })
        with self.assertRaises(HTTPException):
            main._validated_schedule({
                "name": '<img src=x onerror=alert(1)>' * 10,
                "mode": "copilot",
                "kind": "daily",
                "at_time": "09:00",
            })

        clean = main._validated_brief({
            "direction": "新品上市",
            "platforms": ["小红书", "公众号", "小红书"],
            "image_mode": "mix",
        })
        self.assertEqual(["小红书", "公众号"], clean["platforms"])

    def test_schema_version_is_recorded_after_precise_migrations(self):
        row = db.one("SELECT MAX(version) AS version FROM schema_version")
        self.assertEqual(db.LATEST_SCHEMA_VERSION, row["version"])
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION,
            db.one("PRAGMA user_version")["user_version"],
        )

    def test_schema47_removes_legacy_database_session_secret(self):
        db.set_setting("session_secret", "legacy-database-secret")
        connection = db.conn()
        connection.execute("DELETE FROM schema_version")
        connection.execute(
            "INSERT INTO schema_version(version,name,applied_at) "
            "VALUES(46,'production-r5',0)"
        )
        connection.execute("PRAGMA user_version=46")
        connection.commit()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        db.conn()

        self.assertIsNone(db.get_setting("session_secret"))
        ledger = db.one(
            "SELECT name FROM schema_version WHERE version=47"
        )
        self.assertIsNotNone(ledger)
        self.assertEqual(
            "environment-only-session-secret-migration",
            ledger["name"],
        )

    def test_schema48_migrates_and_validates_new_production_columns(self):
        connection = db.conn()
        connection.execute(
            "DROP INDEX IF EXISTS idx_account_profile_tenant_deleted"
        )
        connection.execute("DROP INDEX IF EXISTS idx_asset_tenant_deleted")
        for table, columns in {
            "schedule": ("fail_streak",),
            "account_profile": ("deleted_at", "deleted_by", "delete_reason"),
            "asset": ("deleted_at", "deleted_by", "delete_reason"),
            "job": ("created_by",),
            "task": ("created_by",),
            "avatar_job": ("created_by",),
            "meeting": ("created_by",),
            "tv_job": ("created_by",),
            "tool_job": ("created_by",),
            "notification": ("user_id", "read_by"),
            "station_run": ("reviewed_by",),
        }.items():
            for column in columns:
                connection.execute(
                    f"ALTER TABLE {table} DROP COLUMN {column}"
                )
        connection.execute(
            "DELETE FROM schema_version WHERE version >= 48"
        )
        connection.execute("PRAGMA user_version=47")
        connection.commit()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        db.conn()

        for table, columns in {
            "schedule": {"fail_streak"},
            "account_profile": {"deleted_at", "deleted_by", "delete_reason"},
            "asset": {"deleted_at", "deleted_by", "delete_reason"},
            "job": {"created_by"},
            "task": {"created_by"},
            "avatar_job": {"created_by"},
            "meeting": {"created_by"},
            "tv_job": {"created_by"},
            "tool_job": {"created_by"},
            "notification": {"user_id", "read_by"},
            "station_run": {"reviewed_by"},
        }.items():
            actual = {
                row["name"] for row in db.q(f"PRAGMA table_info({table})")
            }
            self.assertTrue(columns <= actual, (table, columns - actual))
        ledger = db.one(
            "SELECT name FROM schema_version WHERE version=48"
        )
        self.assertEqual(
            "collaboration-soft-delete-schedule-fail-streak",
            ledger["name"],
        )
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION,
            db.one("PRAGMA user_version")["user_version"],
        )

    def test_schema51_migrates_threads_inspections_and_explicit_industries(self):
        connection = db.conn()
        connection.execute(
            "UPDATE tenants SET industries_json=?,enabled=0 WHERE id=2",
            (json.dumps(["food", "retail", "food"]),),
        )
        connection.execute(
            "UPDATE tenants SET industries_json='[]' WHERE id=3"
        )
        for index in (
            "idx_task_thread_root", "idx_task_thread_revision",
            "idx_task_request_key", "idx_task_thread_one_active",
            "idx_task_dashboard_employee",
            "idx_task_thread_current", "idx_tenant_industry_scope",
            "idx_store_branch_name", "idx_inspection_request",
            "idx_inspection_visit_scope", "idx_inspection_visit_dashboard",
            "idx_inspection_visit_status", "idx_inspection_photo_visit",
            "idx_inspection_issue_due", "idx_inspection_issue_visit",
            "idx_inspection_action_issue", "idx_inspection_event_visit",
        ):
            connection.execute(f"DROP INDEX IF EXISTS {index}")
        for table in (
            "inspection_event", "inspection_recheck", "inspection_action",
            "inspection_evidence", "inspection_issue", "inspection_photo",
            "inspection_visit", "store_branch", "tenant_industry",
            "task_thread",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        for column in ("thread_id", "revision_no", "phase", "request_key"):
            connection.execute(f"ALTER TABLE task DROP COLUMN {column}")
        connection.execute("DELETE FROM schema_version WHERE version>=51")
        connection.execute("PRAGMA user_version=50")
        connection.commit()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        db.conn()

        task_columns = {row["name"] for row in db.q("PRAGMA table_info(task)")}
        self.assertTrue(
            {"thread_id", "revision_no", "phase", "request_key"}
            <= task_columns
        )
        tables = {
            row["name"] for row in db.q(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({
            "task_thread", "tenant_industry", "store_branch",
            "inspection_visit", "inspection_photo", "inspection_issue",
            "inspection_evidence", "inspection_action",
            "inspection_recheck", "inspection_event",
        } <= tables)
        indexes = {
            row["name"] for row in db.q(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        self.assertTrue({
            "idx_task_thread_root", "idx_task_thread_revision",
            "idx_task_request_key", "idx_task_thread_one_active",
            "idx_task_dashboard_employee",
            "idx_inspection_visit_dashboard", "idx_inspection_issue_visit",
            "idx_inspection_action_issue",
        } <= indexes)
        self.assertEqual(
            [("food", 1), ("retail", 0)],
            [
                (row["industry_key"], row["is_primary"])
                for row in db.q(
                    "SELECT industry_key,is_primary FROM tenant_industry "
                    "WHERE tenant_id=2 ORDER BY created_at,industry_key"
                )
            ],
        )
        # 升级时停用不代表丢弃其显式行业配置；重新启用不能变成无行业死户。
        db.execute("UPDATE tenants SET enabled=1 WHERE id=2")
        self.assertEqual(
            2,
            db.one(
                "SELECT COUNT(*) n FROM tenant_industry WHERE tenant_id=2"
            )["n"],
        )
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) n FROM tenant_industry WHERE tenant_id=3"
            )["n"],
        )
        self.assertEqual(
            "inspection-task-threads-industry-dashboard",
            db.one(
                "SELECT name FROM schema_version WHERE version=51"
            )["name"],
        )
        db.insert("task", {
            "tenant_id": 2,
            "emp_idx": 100,
            "brief_json": "{}",
            "status": "queued",
            "thread_id": 999,
            "revision_no": 1,
        })
        with self.assertRaises(sqlite3.IntegrityError):
            db.insert("task", {
                "tenant_id": 2,
                "emp_idx": 100,
                "brief_json": "{}",
                "status": "running",
                "thread_id": 999,
                "revision_no": 2,
            })
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION,
            db.one("PRAGMA user_version")["user_version"],
        )

    def test_schema52_migrates_branch_import_ledger_and_legacy_store_codes(self):
        connection = db.conn()
        connection.execute(
            "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
            "VALUES(2,'restaurant',1,0) ON CONFLICT DO NOTHING"
        )
        legacy_id = db.insert("store_branch", {
            "tenant_id": 2,
            "industry_key": "restaurant",
            "name": "旧版无编号门店",
        })
        for index in (
            "idx_store_branch_code", "idx_inspection_branch_import_request",
            "idx_inspection_branch_import_source",
            "idx_inspection_branch_import_status_updated",
            "idx_inspection_branch_import_retention",
            "idx_inspection_import_row",
            "idx_inspection_business_value_natural",
            "idx_inspection_business_value_period",
        ):
            connection.execute(f"DROP INDEX IF EXISTS {index}")
        for table in (
            "inspection_business_value", "inspection_branch_import_row",
            "inspection_branch_import",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        for table, columns in {
            "inspection_visit": (
                "template_key", "template_version", "template_snapshot_json",
                "observations_json",
            ),
            "inspection_photo": ("capture_slot", "item_code"),
        }.items():
            for column in columns:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        for column in (
            "store_code", "province", "city", "district", "manager_name",
            "manager_employee_no", "manager_phone", "store_type", "opened_on",
            "area_sqm", "seat_count", "longitude", "latitude", "remark",
            "row_version",
        ):
            if column in {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(store_branch)"
                )
            }:
                connection.execute(f"ALTER TABLE store_branch DROP COLUMN {column}")
        connection.execute("DELETE FROM schema_version WHERE version>=52")
        connection.execute("PRAGMA user_version=51")
        connection.commit()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        db.conn()

        branch_columns = {
            row["name"] for row in db.q("PRAGMA table_info(store_branch)")
        }
        self.assertTrue({
            "store_code", "province", "city", "district", "manager_name",
            "manager_employee_no", "manager_phone", "store_type", "opened_on",
            "area_sqm", "seat_count", "longitude", "latitude", "remark",
            "row_version",
        } <= branch_columns)
        tables = {
            row["name"] for row in db.q(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({
            "inspection_branch_import", "inspection_branch_import_row",
            "inspection_business_value",
        } <= tables)
        self.assertTrue({
            "catalog_version", "catalog_sha256",
            "business_create_count", "business_update_count",
            "business_skip_count", "business_error_count",
            "staging_purged_at",
        } <= {
            row["name"]
            for row in db.q("PRAGMA table_info(inspection_branch_import)")
        })
        self.assertTrue({
            "existing_branch_id", "existing_row_version",
            "existing_business_value_id", "existing_business_row_version",
        } <= {
            row["name"]
            for row in db.q("PRAGMA table_info(inspection_branch_import_row)")
        })
        self.assertTrue({
            "template_key", "template_version", "template_snapshot_json",
            "observations_json",
        } <= {
            row["name"] for row in db.q("PRAGMA table_info(inspection_visit)")
        })
        self.assertTrue({"capture_slot", "item_code"} <= {
            row["name"] for row in db.q("PRAGMA table_info(inspection_photo)")
        })
        self.assertIn("row_version", {
            row["name"]
            for row in db.q("PRAGMA table_info(inspection_business_value)")
        })
        indexes = {
            row["name"] for row in db.q(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        self.assertTrue({
            "idx_store_branch_code", "idx_inspection_branch_import_request",
            "idx_inspection_branch_import_source",
            "idx_inspection_branch_import_status_updated",
            "idx_inspection_branch_import_retention",
            "idx_inspection_import_row",
            "idx_inspection_business_value_natural",
            "idx_inspection_business_value_period",
        } <= indexes)
        name_index = next(
            row for row in db.q("PRAGMA index_list(store_branch)")
            if row["name"] == "idx_store_branch_name"
        )
        self.assertEqual(0, name_index["unique"])
        self.assertIsNone(db.one(
            "SELECT store_code FROM store_branch WHERE id=?", (legacy_id,)
        )["store_code"])
        self.assertEqual(
            "inspection-branch-master-import",
            db.one("SELECT name FROM schema_version WHERE version=52")["name"],
        )
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION,
            db.one("PRAGMA user_version")["user_version"],
        )

    def test_stamped_schema52_old_import_status_check_fails_closed(self):
        connection = db.conn()
        schema_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='inspection_branch_import'"
        ).fetchone()["sql"]
        self.assertIn("'expired'", schema_sql)
        old_sql = schema_sql.replace(
            "'previewed','committed','expired'", "'previewed','committed'"
        )
        self.assertNotEqual(schema_sql, old_sql)
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' "
            "AND name='inspection_branch_import'", (old_sql,),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        with self.assertRaisesRegex(RuntimeError, "状态约束不完整"):
            db.conn()
        self.assertIsNone(db._conn)

    def test_real_schema52_two_state_import_ledger_upgrades_atomically(self):
        connection = db.conn()
        for index in (
            "idx_inspection_branch_import_request",
            "idx_inspection_branch_import_source",
            "idx_inspection_branch_import_status_updated",
            "idx_inspection_branch_import_retention",
        ):
            connection.execute(f"DROP INDEX IF EXISTS {index}")
        connection.execute("DROP TABLE inspection_branch_import")
        connection.execute("""
            CREATE TABLE inspection_branch_import(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id INTEGER NOT NULL,
              industry_key TEXT NOT NULL,
              request_key TEXT NOT NULL,
              source_sha256 TEXT NOT NULL,
              filename TEXT NOT NULL,
              catalog_version TEXT NOT NULL DEFAULT '',
              catalog_sha256 TEXT NOT NULL DEFAULT '',
              business_values_json TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL DEFAULT 'previewed'
                CHECK(status IN ('previewed','committed')),
              total_rows INTEGER NOT NULL DEFAULT 0,
              create_count INTEGER NOT NULL DEFAULT 0,
              update_count INTEGER NOT NULL DEFAULT 0,
              skip_count INTEGER NOT NULL DEFAULT 0,
              error_count INTEGER NOT NULL DEFAULT 0,
              business_create_count INTEGER NOT NULL DEFAULT 0,
              business_update_count INTEGER NOT NULL DEFAULT 0,
              business_skip_count INTEGER NOT NULL DEFAULT 0,
              business_error_count INTEGER NOT NULL DEFAULT 0,
              created_by INTEGER NOT NULL,
              committed_by INTEGER,
              committed_at REAL,
              created_at REAL,
              updated_at REAL
            )
        """)
        connection.executemany(
            "INSERT INTO inspection_branch_import("
            "id,tenant_id,industry_key,request_key,source_sha256,filename,"
            "status,total_rows,create_count,created_by,committed_by,"
            "committed_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (7001, 2, "auto", "schema52-preview", "a" * 64,
                 "preview.xlsx", "previewed", 1, 1, 20, None, None, 1.0, 2.0),
                (7002, 2, "auto", "schema52-committed", "b" * 64,
                 "committed.xlsx", "committed", 1, 1, 20, 20, 3.0, 1.0, 3.0),
            ),
        )
        connection.executemany(
            "INSERT INTO inspection_branch_import_row("
            "import_id,tenant_id,row_number,store_code,action,error_code,"
            "error_message,payload_json,masked_payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                (7001, 2, 1, "A001", "create", None, None, "{}", "{}", 1.0),
                (7002, 2, 1, "A002", "create", None, None, "{}", "{}", 1.0),
            ),
        )
        connection.execute("DELETE FROM schema_version WHERE version>=53")
        connection.execute("PRAGMA user_version=52")
        connection.commit()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        db.conn()

        table_sql = db.one(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='inspection_branch_import'"
        )["sql"]
        self.assertIn("'expired'", table_sql)
        self.assertIn("staging_purged_at", {
            row["name"]
            for row in db.q("PRAGMA table_info(inspection_branch_import)")
        })
        self.assertEqual(
            [(7001, "previewed", 1), (7002, "committed", 1)],
            [
                (row["id"], row["status"], row["create_count"])
                for row in db.q(
                    "SELECT id,status,create_count "
                    "FROM inspection_branch_import ORDER BY id"
                )
            ],
        )
        self.assertEqual(
            [(7001, "A001"), (7002, "A002")],
            [
                (row["import_id"], row["store_code"])
                for row in db.q(
                    "SELECT import_id,store_code "
                    "FROM inspection_branch_import_row "
                    "WHERE import_id IN (7001,7002) ORDER BY import_id"
                )
            ],
        )
        new_id = db.insert("inspection_branch_import", {
            "tenant_id": 2,
            "industry_key": "auto",
            "request_key": "schema53-expired",
            "source_sha256": "c" * 64,
            "filename": "expired.xlsx",
            "status": "expired",
            "created_by": 20,
        })
        self.assertGreater(new_id, 7002)
        self.assertEqual("ok", db.one("PRAGMA quick_check")["quick_check"])
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION,
            db.one("PRAGMA user_version")["user_version"],
        )
        self.assertEqual(
            "versioned-industry-decision-employees",
            db.one("SELECT name FROM schema_version WHERE version=53")["name"],
        )

        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.conn()
        self.assertEqual(3, db.one(
            "SELECT COUNT(*) AS n FROM inspection_branch_import "
            "WHERE id IN (7001,7002,?)", (new_id,),
        )["n"])

    def test_stamped_schema52_upgrades_row_versions_and_legacy_name_index(self):
        connection = db.conn()
        connection.execute("DROP INDEX idx_store_branch_name")
        connection.execute(
            "CREATE UNIQUE INDEX idx_store_branch_name "
            "ON store_branch(tenant_id,industry_key,name)"
        )
        for table, column in (
            ("store_branch", "row_version"),
            ("inspection_branch_import_row", "existing_branch_id"),
            ("inspection_branch_import_row", "existing_row_version"),
            ("inspection_branch_import_row", "existing_business_value_id"),
            ("inspection_branch_import_row", "existing_business_row_version"),
            ("inspection_business_value", "row_version"),
            ("inspection_branch_import", "business_create_count"),
            ("inspection_branch_import", "business_update_count"),
            ("inspection_branch_import", "business_skip_count"),
            ("inspection_branch_import", "business_error_count"),
        ):
            if column in {
                row["name"] for row in connection.execute(
                    f"PRAGMA table_info({table})"
                )
            }:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.commit()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        db.conn()

        self.assertIn(
            "row_version",
            {row["name"] for row in db.q("PRAGMA table_info(store_branch)")},
        )
        self.assertTrue({
            "existing_branch_id", "existing_row_version",
            "existing_business_value_id", "existing_business_row_version",
        } <= {
            row["name"]
            for row in db.q("PRAGMA table_info(inspection_branch_import_row)")
        })
        self.assertIn("row_version", {
            row["name"]
            for row in db.q("PRAGMA table_info(inspection_business_value)")
        })
        self.assertTrue({
            "business_create_count", "business_update_count",
            "business_skip_count", "business_error_count",
            "staging_purged_at",
        } <= {
            row["name"]
            for row in db.q("PRAGMA table_info(inspection_branch_import)")
        })
        name_index = next(
            row for row in db.q("PRAGMA index_list(store_branch)")
            if row["name"] == "idx_store_branch_name"
        )
        self.assertEqual(0, name_index["unique"])
        db.insert("store_branch", {
            "tenant_id": 2, "industry_key": "restaurant",
            "store_code": "DUPNAME001", "name": "同名门店",
        })
        db.insert("store_branch", {
            "tenant_id": 2, "industry_key": "restaurant",
            "store_code": "DUPNAME002", "name": "同名门店",
        })

    def test_stamped_schema52_unknown_name_index_shape_fails_closed(self):
        connection = db.conn()
        connection.execute("DROP INDEX idx_store_branch_name")
        connection.execute(
            "CREATE INDEX idx_store_branch_name "
            "ON store_branch(tenant_id,name,industry_key)"
        )
        connection.commit()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        with self.assertRaisesRegex(RuntimeError, "索引结构不完整"):
            db.conn()
        self.assertIsNone(db._conn)

    def test_schema52_stamped_wrong_business_natural_index_fails_closed(self):
        connection = db.conn()
        connection.execute(
            "DROP INDEX idx_inspection_business_value_natural"
        )
        connection.execute(
            "CREATE UNIQUE INDEX idx_inspection_business_value_natural "
            "ON inspection_business_value(tenant_id,industry_key,branch_id,"
            "metric_key,period_start,period_end,source_ref)"
        )
        connection.commit()
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

        with self.assertRaisesRegex(RuntimeError, "索引结构不完整"):
            db.conn()
        self.assertIsNone(db._conn)

        check = sqlite3.connect(db.DB_PATH)
        try:
            columns = [
                row[2] for row in check.execute(
                    "PRAGMA index_info(idx_inspection_business_value_natural)"
                )
            ]
            self.assertEqual([
                "tenant_id", "industry_key", "branch_id", "metric_key",
                "period_start", "period_end", "source_ref",
            ], columns)
            self.assertEqual(
                db.LATEST_SCHEMA_VERSION,
                check.execute("PRAGMA user_version").fetchone()[0],
            )
        finally:
            check.close()

    def test_future_database_is_rejected_before_wal_or_schema_mutation(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        future_path = os.path.join(self.tmp.name, "future.db")
        connection = sqlite3.connect(future_path)
        connection.execute(
            "CREATE TABLE sentinel(id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.execute("INSERT INTO sentinel(value) VALUES('unchanged')")
        connection.execute(f"PRAGMA user_version={db.LATEST_SCHEMA_VERSION + 1}")
        connection.commit()
        connection.close()
        before_mtime = os.stat(future_path).st_mtime_ns

        db.DB_PATH = future_path
        with self.assertRaisesRegex(RuntimeError, "拒绝降级启动"):
            db.conn()

        self.assertIsNone(db._conn)
        self.assertEqual(before_mtime, os.stat(future_path).st_mtime_ns)
        check = sqlite3.connect(future_path)
        try:
            tables = {
                row[0]
                for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual({"sentinel"}, tables)
            self.assertEqual(
                "unchanged",
                check.execute("SELECT value FROM sentinel").fetchone()[0],
            )
            self.assertEqual("delete", check.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            check.close()

    def test_fake_legacy_tables_are_rejected_before_latest_marker(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        fake_path = os.path.join(self.tmp.name, "fake-legacy.db")
        connection = sqlite3.connect(fake_path)
        connection.executescript(
            "CREATE TABLE tenants(id INTEGER PRIMARY KEY);"
            "CREATE TABLE users(id INTEGER PRIMARY KEY);"
            "CREATE TABLE job(id INTEGER PRIMARY KEY);"
        )
        connection.close()
        before_mtime = os.stat(fake_path).st_mtime_ns

        db.DB_PATH = fake_path
        with self.assertRaisesRegex(RuntimeError, "旧版结构不完整"):
            db.conn()

        self.assertIsNone(db._conn)
        self.assertEqual(before_mtime, os.stat(fake_path).st_mtime_ns)
        check = sqlite3.connect(fake_path)
        try:
            tables = {
                row[0]
                for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("schema_version", tables)
            self.assertEqual("delete", check.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            check.close()

    def test_wechat_delivery_migration_keeps_service_bootable_with_old_conflicts(self):
        db.execute("DROP INDEX idx_wechat_delivery_active")
        for suffix in ("a", "b"):
            db.insert("wechat_draft_delivery", {
                "tenant_id": 2,
                "job_id": 999,
                "request_hash": suffix * 64,
                "request_key": suffix * 20,
                "title": f"旧冲突{suffix}",
                "status": "submitting",
                "billing_status": "charged",
                "billing_points": 1,
                "op_key": f"old:{suffix}",
            })
        db._conn.close()
        db._conn = None

        self.assertIsNotNone(db.conn())
        indexes = {
            row["name"]: row["unique"]
            for row in db.q("PRAGMA index_list(wechat_draft_delivery)")
        }
        self.assertNotIn("idx_wechat_delivery_active", indexes)
        self.assertIn("idx_wechat_delivery_active_lookup", indexes)
        conflicts = json.loads(
            db.get_setting("wechat_delivery_migration_conflicts")
        )
        self.assertEqual(
            [{"tenant_id": 2, "job_id": 999, "count": 2}],
            conflicts,
        )

    def test_client_error_log_is_sanitized_bounded_and_rate_limited(self):
        from app import main

        class Request:
            headers = {"user-agent": "Browser/1"}

        main._client_log_hits.clear()
        body = {
            "kind": "render",
            "error_name": "TypeError",
            "message": (
                "客户正文 sentinel-business-body "
                "13800138000 user@example.com password=super-secret sk-abcdefgh1234"
            ),
            "stack": "Authorization: Bearer eyJ-secret /srv/private.py:77",
            "path": "/app?token=raw-secret",
            "hash": "#/jobs/3?key=private",
            "line": 77,
        }
        with patch.object(main, "_CLIENT_LOG_LIMIT", 2):
            self.assertEqual({"ok": True}, main.client_log(Request(), body))
            self.assertEqual({"ok": True}, main.client_log(Request(), body))
            self.assertEqual(
                {"ok": True, "dropped": True},
                main.client_log(Request(), body),
            )

        rows = db.q("SELECT route,message FROM client_error WHERE tenant_id=2")
        self.assertEqual(2, len(rows))
        stored = json.dumps(rows, ensure_ascii=False)
        for secret in (
            "sentinel-business-body",
            "13800138000",
            "user@example.com",
            "super-secret",
            "abcdefgh1234",
            "eyJ-secret",
            "/srv/private.py",
            "raw-secret",
            "private",
        ):
            self.assertNotIn(secret, stored)
        self.assertEqual("/app#/jobs/3", rows[0]["route"])
        self.assertRegex(
            rows[0]["message"],
            r"^fingerprint=[0-9a-f]{24};name=TypeError;line=77$",
        )
        self.assertEqual(rows[0]["message"], rows[1]["message"])


if __name__ == "__main__":
    unittest.main()
