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

    def test_meta_exposes_loaded_department_counts_without_internal_employee_details(self):
        from app import departments, main

        departments._cache = [{
            "key": "test",
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
        from app import main

        internal_duty = "内部岗位档案：使用秘密渠道矩阵与专属拆解步骤"
        public = main._public_expert({
            "idx": 101,
            "name": "经营顾问",
            "role": "经营顾问",
            "dept_name": "企业增长部",
            "duty": internal_duty,
            "intro": "",
        })

        self.assertTrue(public["intro"])
        self.assertNotIn(internal_duty, public["intro"])
        self.assertNotIn("秘密渠道矩阵", public["intro"])

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
        self.assertEqual(48, db.one("PRAGMA user_version")["user_version"])

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
