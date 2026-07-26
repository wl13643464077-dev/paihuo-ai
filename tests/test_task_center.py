"""V29 任务中心：统一聚合、来源追踪、租户/板块隔离与未完任务优先。"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import db, taskcenter
from app.engine import Engine


class TaskCenterDatabaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "fresh.db")
        db.conn()

    def tearDown(self):
        from app import auth
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _task(self, direction, status="done", tenant=1, at=100, **extra):
        return db.insert("task", {"tenant_id": tenant, "emp_idx": 0,
                                   "brief_json": json.dumps({"direction": direction}),
                                   "status": status, "created_at": at, "updated_at": at,
                                   **extra})

    @staticmethod
    def _discard_coro(coro):
        coro.close()
        return None

    def test_fresh_database_has_trace_columns_and_list_indexes(self):
        from app import main

        main._ensure_tool_running_index()
        task_cols = {r["name"] for r in db.q("PRAGMA table_info(task)")}
        job_cols = {r["name"] for r in db.q("PRAGMA table_info(job)")}
        indexes = {r["name"] for r in db.q("PRAGMA index_list(task)")}
        tool_cols = {r["name"] for r in db.q("PRAGMA table_info(tool_job)")}
        tool_indexes = {r["name"] for r in db.q("PRAGMA index_list(tool_job)")}
        self.assertTrue({"source_meeting_id", "source_action_key", "source_task_id"} <= task_cols)
        self.assertIn("source_schedule_id", job_cols)
        self.assertTrue({"idx_task_tenant_created", "idx_task_tenant_status_created",
                         "idx_task_tenant_emp_created"} <= indexes)
        self.assertTrue({"progress", "billing_status", "billing_points"} <= tool_cols)
        self.assertIn("idx_tool_job_one_active", tool_indexes)

    def test_sources_are_traceable_compact_and_tenant_scoped(self):
        mid = db.insert("meeting", {"tenant_id": 1, "question": "新品要不要上线",
                                    "emp_idxs_json": "[0]", "status": "done",
                                    "phase": "completed", "created_at": 20, "updated_at": 20})
        direct = self._task("直接任务", at=30)
        from_meeting = self._task("会议执行任务", at=40, source_meeting_id=mid,
                                  source_action_key="action-a")
        redo = self._task("重做任务", status="failed", at=50, source_task_id=direct)
        self._task("别的企业任务", tenant=2, status="running", at=999)

        result = taskcenter.list_items(1, {"content"})
        rows = {x["record_id"]: x for x in result["items"] if x["kind"] == "expert"}
        self.assertEqual(set(rows), {direct, from_meeting, redo})
        self.assertEqual(rows[from_meeting]["source_label"], f"AI会议 #{mid}")
        self.assertEqual(rows[from_meeting]["source_detail"], "新品要不要上线")
        self.assertEqual(rows[redo]["source_route"], f"#/tasks/{direct}")
        forbidden = {"brief_json", "output_md", "steps_json", "tenant_id", "source_action_key"}
        self.assertFalse(forbidden & set(rows[direct]))

    def test_open_work_is_never_pushed_out_by_new_done_rows(self):
        running = self._task("很早开始但还没收口", status="running", at=1)
        for i in range(8):
            self._task(f"较新的完结任务{i}", status="done", at=100 + i)
        result = taskcenter.list_items(1, {"content"}, limit=1)
        self.assertEqual(result["items"][0]["record_id"], running)
        self.assertEqual(result["counts"]["open"], 1)
        self.assertEqual(result["counts"]["done"], 8)
        self.assertTrue(result["truncated"])

    def test_stable_offset_pages_do_not_repeat_or_skip_existing_tasks(self):
        self._task("早期进行中", status="running", at=1)
        self._task("较新进行中", status="running", at=2)
        for i in range(5):
            self._task(f"同一时刻完结任务{i}", status="done", at=100)

        expected = [
            item["key"]
            for item in taskcenter.list_items(
                1, {"content"}, limit=50, offset=0
            )["items"]
        ]
        seen = []
        offset = 0
        while True:
            page = taskcenter.list_items(
                1, {"content"}, limit=2, offset=offset
            )
            keys = [item["key"] for item in page["items"]]
            self.assertFalse(set(keys) & set(seen))
            seen.extend(keys)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["has_more"], page["next_offset"] is not None)
            self.assertEqual(page["truncated"], page["has_more"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]

        self.assertEqual(seen, expected)
        self.assertEqual(
            [item["key"] for item in taskcenter.list_items(
                1, {"content"}, limit=50, offset=0
            )["items"]],
            expected,
        )

    def test_wechat_draft_deliveries_are_counted_status_mapped_and_tenant_scoped(self):
        def content_job(tenant, suffix, at):
            return db.insert("job", {
                "tenant_id": tenant,
                "brief_json": json.dumps({"direction": f"公众号选题{suffix}"}),
                "status": "done",
                "created_at": at,
                "updated_at": at,
            })

        local_jobs = {
            status: content_job(1, status, 10 + index)
            for index, status in enumerate(
                ("processing", "submitted", "blocked", "done")
            )
        }
        foreign_job = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "别家公众号选题"}),
            "status": "done",
            "created_at": 20,
            "updated_at": 20,
        })

        def delivery(tenant, job_id, status, suffix, at):
            return db.insert("wechat_draft_delivery", {
                "tenant_id": tenant,
                "job_id": job_id,
                "request_hash": f"hash-{suffix}",
                "request_key": f"key-{suffix}",
                "title": f"公众号草稿{suffix}",
                "status": status,
                "billing_status": "succeeded" if status in {"done", "blocked"} else "charged",
                "created_at": at,
                "updated_at": at,
            })

        processing = delivery(1, local_jobs["processing"], "processing", "处理中", 30)
        submitted = delivery(1, local_jobs["submitted"], "submitted", "待确认", 31)
        blocked = delivery(1, local_jobs["blocked"], "blocked", "被拦截", 32)
        done = delivery(1, local_jobs["done"], "done", "已完成", 33)
        foreign = delivery(2, foreign_job, "processing", "别家", 999)

        result = taskcenter.list_items(1, {"content"}, limit=50, offset=0)
        rows = {
            item["record_id"]: item
            for item in result["items"]
            if item["kind"] == "wechat"
        }

        self.assertEqual(set(rows), {processing, submitted, blocked, done})
        self.assertNotIn(foreign, rows)
        self.assertEqual(rows[processing]["status_group"], "active")
        self.assertEqual(rows[processing]["status_label"], "正在准备草稿")
        self.assertEqual(rows[submitted]["status_group"], "active")
        self.assertEqual(rows[submitted]["status_label"], "已提交 · 正在确认")
        self.assertEqual(rows[blocked]["status_group"], "waiting")
        self.assertEqual(rows[blocked]["status_label"], "合规审查待处理")
        self.assertEqual(rows[done]["status_group"], "done")
        self.assertEqual(
            rows[done]["source_route"], f"#/job/{local_jobs['done']}"
        )
        self.assertEqual(
            rows[done]["target_route"], f"#/job/{local_jobs['done']}"
        )
        self.assertFalse(rows[done]["target_exact"])
        self.assertEqual(result["kind_counts"]["wechat"], 4)

        avatar_only = taskcenter.list_items(
            1, {"avatar"}, limit=50, offset=0
        )
        self.assertFalse(any(item["kind"] == "wechat"
                             for item in avatar_only["items"]))

    def test_task_center_frontend_loads_incremental_offset_pages(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "static", "app.js"
        )
        with open(path, encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn('/task-center?limit=500&offset=0', source)
        self.assertIn("TC_DATA.next_offset", source)
        self.assertIn("page.next_offset", source)
        self.assertIn("new Set(TC_DATA.items.map", source)
        self.assertNotIn('api("/task-center?limit=5000")', source)

    def test_every_replayable_task_table_has_persistent_retry_state(self):
        for table in ("job", "meeting", "tv_job", "tool_job", "pub_task"):
            with self.subTest(table=table):
                columns = {
                    row["name"] for row in db.q(f"PRAGMA table_info({table})")
                }
                self.assertIn("retry_count", columns)
        tool_columns = {
            row["name"] for row in db.q("PRAGMA table_info(tool_job)")
        }
        self.assertIn("retry_started_at", tool_columns)
        publish_columns = {
            row["name"] for row in db.q("PRAGMA table_info(pub_task)")
        }
        self.assertTrue(
            {"submission_state", "submit_started_at"} <= publish_columns
        )

    def test_legacy_publish_rows_are_migrated_by_terminal_safety(self):
        """旧发布表没有提交锚点时，只把明确排队态判为可安全重放。"""
        connection = db.conn()
        connection.executescript(
            """
            CREATE TABLE pub_task_legacy(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id INTEGER NOT NULL,
              platform TEXT NOT NULL,
              account TEXT,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued',
              retry_count INTEGER NOT NULL DEFAULT 0,
              log TEXT,
              fail_json TEXT,
              created_at REAL,
              updated_at REAL
            );
            INSERT INTO pub_task_legacy(
              tenant_id,platform,account,payload_json,status,log
            ) VALUES
              (1,'xhs','a','{}','done',''),
              (1,'xhs','a','{}','queued',''),
              (1,'xhs','a','{}','running',''),
              (1,'xhs','a','{}','failed','');
            DROP TABLE pub_task;
            ALTER TABLE pub_task_legacy RENAME TO pub_task;
            """
        )
        connection.execute("DELETE FROM schema_version")
        connection.execute(
            "INSERT INTO schema_version(version,name,applied_at) "
            "VALUES(44,'production-r5',0)"
        )
        connection.execute("PRAGMA user_version=44")
        connection.commit()
        connection.close()
        db._conn = None

        db.conn()

        migrated = db.q(
            "SELECT status,submission_state,submit_started_at "
            "FROM pub_task ORDER BY id"
        )
        self.assertEqual(
            [
                ("done", "submitted", None),
                ("queued", "not_submitted", None),
                ("running", "legacy_unknown", None),
                ("failed", "legacy_unknown", None),
            ],
            [
                (
                    row["status"],
                    row["submission_state"],
                    row["submit_started_at"],
                )
                for row in migrated
            ],
        )
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION,
            db.one("PRAGMA user_version")["user_version"],
        )

        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.conn()

        self.assertEqual(
            [
                ("done", "submitted", None),
                ("queued", "not_submitted", None),
                ("running", "legacy_unknown", None),
                ("failed", "legacy_unknown", None),
            ],
            [
                (
                    row["status"],
                    row["submission_state"],
                    row["submit_started_at"],
                )
                for row in db.q(
                    "SELECT status,submission_state,submit_started_at "
                    "FROM pub_task ORDER BY id"
                )
            ],
        )

    def test_publish_worker_uses_single_cas_claim_before_external_work(self):
        from app import matrixpub

        publish_id = db.insert("pub_task", {
            "tenant_id": 1,
            "platform": "xhs",
            "account": "account-1",
            "payload_json": "{}",
            "status": "queued",
            "submission_state": "not_submitted",
        })

        async def race():
            matrixpub._PUB_SEM = None
            try:
                with patch.object(
                        matrixpub, "_run_task_inner",
                        new_callable=AsyncMock) as worker:
                    await asyncio.gather(
                        matrixpub.run_task(publish_id),
                        matrixpub.run_task(publish_id),
                    )
                    return worker.await_count
            finally:
                matrixpub._PUB_SEM = None

        self.assertEqual(asyncio.run(race()), 1)
        self.assertEqual(
            db.one(
                "SELECT status,submission_state FROM pub_task WHERE id=?",
                (publish_id,),
            ),
            {"status": "running", "submission_state": "not_submitted"},
        )

    def test_matrix_publish_persists_uncertain_before_click_and_blocks_replay(self):
        from app import auth, main, matrixpub

        class FakeLocator:
            def __init__(self, *, count=0, on_click=None):
                self._count = count
                self._on_click = on_click

            @property
            def first(self):
                return self

            async def count(self):
                return self._count

            async def click(self):
                if self._on_click:
                    await self._on_click()

            async def fill(self, _value):
                return None

            async def set_input_files(self, _value):
                return None

        class FakePage:
            def __init__(self, on_publish_click):
                self.url = ""
                self._button = FakeLocator(
                    count=1, on_click=on_publish_click
                )
                self._file = FakeLocator(count=1)

            async def goto(self, url, **_kwargs):
                self.url = url

            async def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                if selector == "input[type=file]":
                    return self._file
                if selector == "button:has-text('发布')":
                    return self._button
                return FakeLocator()

            async def screenshot(self, **_kwargs):
                raise RuntimeError("fixture screenshot unavailable")

        class FakeContext:
            def __init__(self, page):
                self._page = page

            async def add_cookies(self, _cookies):
                return None

            async def new_page(self):
                return self._page

        class FakeBrowser:
            def __init__(self, page):
                self._page = page

            async def new_context(self, **_kwargs):
                return FakeContext(self._page)

            async def close(self):
                return None

        class FakeChromium:
            def __init__(self, page):
                self._page = page

            async def launch(self, **_kwargs):
                return FakeBrowser(self._page)

        class FakePlaywright:
            def __init__(self, page):
                self.chromium = FakeChromium(page)

        class FakePlaywrightContext:
            def __init__(self, page):
                self._playwright = FakePlaywright(page)

            async def __aenter__(self):
                return self._playwright

            async def __aexit__(self, *_args):
                return False

        auth.set_current({
            "id": 8,
            "tenant_id": 1,
            "role": "member",
            "modules": ["content"],
        })
        account = {
            "id": "account-1",
            "name": "测试账号",
            "platform": "xhs",
            "cookie": "session=" + ("x" * 80),
        }

        for platform in ("xhs", "douyin"):
            with self.subTest(platform=platform):
                media_path = os.path.join(
                    self.tmp.name, f"{platform}-asset.bin"
                )
                with open(media_path, "wb") as handle:
                    handle.write(b"fixture")
                payload = (
                    {"title": "测试发布", "images": [media_path]}
                    if platform == "xhs"
                    else {"title": "测试发布", "video": media_path}
                )
                publish_id = db.insert("pub_task", {
                    "tenant_id": 1,
                    "platform": platform,
                    "account": account["id"],
                    "payload_json": json.dumps(payload),
                    "status": "running",
                    "submission_state": "not_submitted",
                    "retry_count": 0,
                })
                states_seen_at_click = []

                async def fail_after_real_click():
                    states_seen_at_click.append(
                        db.one(
                            "SELECT submission_state FROM pub_task WHERE id=?",
                            (publish_id,),
                        )["submission_state"]
                    )
                    raise RuntimeError("browser failed after click")

                page = FakePage(fail_after_real_click)
                row = db.one(
                    "SELECT * FROM pub_task WHERE id=?", (publish_id,)
                )
                with patch.object(
                    matrixpub, "accounts", return_value=[account]
                ), patch.object(
                    matrixpub,
                    "_probe_login",
                    new=AsyncMock(return_value=(True, "测试账号")),
                ), patch(
                    "playwright.async_api.async_playwright",
                    return_value=FakePlaywrightContext(page),
                ), patch.object(matrixpub.notify, "push"):
                    asyncio.run(matrixpub._run_task_inner(publish_id, row))

                self.assertEqual(
                    ["submission_uncertain"], states_seen_at_click
                )
                self.assertEqual(
                    {
                        "status": "failed",
                        "submission_state": "submission_uncertain",
                    },
                    db.one(
                        "SELECT status,submission_state FROM pub_task "
                        "WHERE id=?",
                        (publish_id,),
                    ),
                )
                with self.assertRaises(HTTPException) as denied:
                    asyncio.run(
                        main.task_center_retry("publish", publish_id)
                    )
                self.assertEqual(409, denied.exception.status_code)
                self.assertEqual(
                    0,
                    db.one(
                        "SELECT retry_count FROM pub_task WHERE id=?",
                        (publish_id,),
                    )["retry_count"],
                )

    def test_industry_only_member_can_retry_visible_meeting_but_not_hidden_one(self):
        from app import auth, main

        db.insert("tenants", {
            "id": 2,
            "name": "行业会议企业",
            "industries_json": json.dumps(["auto"]),
        })
        visible_meeting = db.insert("meeting", {
            "tenant_id": 2,
            "question": "汽车门店增长计划",
            "emp_idxs_json": "[1601]",
            "messages_json": "[]",
            "status": "failed",
            "phase": "failed",
            "billing_status": "refunded",
            "retry_count": 0,
        })
        auth.set_current({
            "id": 8,
            "tenant_id": 2,
            "role": "member",
            "modules": ["auto"],
        })

        with patch.object(
            main.asyncio, "create_task", side_effect=self._discard_coro
        ):
            retried = asyncio.run(
                main.task_center_retry("meeting", visible_meeting)
            )

        self.assertTrue(retried["free_retry"])
        self.assertEqual(
            {"status": "queued", "phase": "queued", "retry_count": 1},
            db.one(
                "SELECT status,phase,retry_count FROM meeting WHERE id=?",
                (visible_meeting,),
            ),
        )

        content_job = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "内容部失败工单"}),
            "status": "failed",
            "billing_status": "refunded",
            "retry_count": 0,
        })
        with self.assertRaises(HTTPException) as wrong_module:
            asyncio.run(main.task_center_retry("content", content_job))
        self.assertEqual(403, wrong_module.exception.status_code)
        self.assertEqual(
            {"status": "failed", "retry_count": 0},
            db.one(
                "SELECT status,retry_count FROM job WHERE id=?",
                (content_job,),
            ),
        )

        hidden_meeting = db.insert("meeting", {
            "tenant_id": 2,
            "question": "汽车渠道预算",
            "emp_idxs_json": "[1602]",
            "messages_json": "[]",
            "status": "failed",
            "phase": "failed",
            "billing_status": "refunded",
            "retry_count": 0,
        })
        auth.set_current({
            "id": 9,
            "tenant_id": 2,
            "role": "member",
            "modules": ["content"],
        })
        with self.assertRaises(HTTPException) as hidden:
            asyncio.run(main.task_center_retry("meeting", hidden_meeting))
        self.assertEqual(404, hidden.exception.status_code)
        self.assertEqual(
            {"status": "failed", "retry_count": 0},
            db.one(
                "SELECT status,retry_count FROM meeting WHERE id=?",
                (hidden_meeting,),
            ),
        )

    def test_content_job_free_retry_is_original_row_cas_and_never_recharges(self):
        from app import auth, main

        db.insert("tenants", {"id": 2, "name": "重试企业"})
        db.execute("UPDATE tenants SET balance=100 WHERE id=2")
        job_id = db.insert("job", {
            "tenant_id": 2,
            "brief_json": json.dumps({"direction": "重试这条内容工单"}),
            "status": "failed",
            "billing_status": "refunded",
            "billing_points": 18,
            "retry_count": 0,
            "current_idx": 2,
        })
        run_id = db.insert("station_run", {
            "job_id": job_id,
            "station_idx": 2,
            "version": 1,
            "status": "failed",
            "review_comment": "供应商失败",
        })
        auth.set_current({
            "id": 8, "tenant_id": 2, "role": "member",
            "modules": ["content"],
        })
        before = db.one("SELECT balance FROM tenants WHERE id=2")["balance"]
        logs_before = db.one(
            "SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2"
        )["n"]

        with patch.object(main.engine, "notify") as notify, \
                patch.object(main.engine, "touch"), \
                patch.object(main.engine, "broadcast"):
            result = asyncio.run(main.task_center_retry("content", job_id))
            with self.assertRaises(HTTPException) as duplicate:
                asyncio.run(main.task_center_retry("content", job_id))

        self.assertTrue(result["free_retry"])
        self.assertEqual(duplicate.exception.status_code, 409)
        self.assertEqual(
            db.one(
                "SELECT status,billing_status,billing_points,retry_count "
                "FROM job WHERE id=?", (job_id,)
            ),
            {
                "status": "running",
                "billing_status": "charged",
                "billing_points": 0,
                "retry_count": 1,
            },
        )
        self.assertEqual(
            db.one("SELECT status FROM station_run WHERE id=?", (run_id,))[
                "status"
            ],
            "rejected",
        )
        self.assertEqual(
            db.one("SELECT balance FROM tenants WHERE id=2")["balance"],
            before,
        )
        self.assertEqual(
            db.one("SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2")["n"],
            logs_before,
        )
        notify.assert_called_once_with(job_id)

    def test_video_tool_meeting_and_safe_publish_retry_without_new_charge(self):
        from app import auth, main

        db.insert("tenants", {"id": 2, "name": "综合重试企业"})
        db.execute("UPDATE tenants SET balance=88 WHERE id=2")
        auth.set_current({
            "id": 8, "tenant_id": 2, "role": "member",
            "modules": ["content"],
        })
        video_id = db.insert("tv_job", {
            "tenant_id": 2,
            "params_json": json.dumps({"title": "失败成片"}),
            "status": "failed",
            "billing_status": "refunded",
            "billing_points": 3,
            "retry_count": 0,
            "steps_json": json.dumps([{"msg": "失败"}]),
            "error": "供应商失败",
        })
        tool_id = db.insert("tool_job", {
            "tenant_id": 2,
            "kind": "leads",
            "params_json": json.dumps({"industry": "家装"}),
            "status": "failed",
            "billing_status": "refunded",
            "billing_points": 1,
            "retry_count": 0,
            "result_json": json.dumps({"stale": True}),
            "error": "联网失败",
        })
        meeting_id = db.insert("meeting", {
            "tenant_id": 2,
            "question": "是否上线新品",
            "emp_idxs_json": "[0,1]",
            "messages_json": json.dumps([{"who": "系统", "text": "中断"}]),
            "status": "failed",
            "phase": "failed",
            "billing_status": "refunded",
            "billing_points": 2,
            "retry_count": 0,
            "round_no": 2,
            "proposals_json": "[{\"id\":1}]",
            "validations_json": "[{\"verdict\":\"UNKNOWN\"}]",
        })
        publish_id = db.insert("pub_task", {
            "tenant_id": 2,
            "platform": "xhs",
            "account": "account-1",
            "payload_json": json.dumps({"title": "待发布"}),
            "status": "failed",
            "retry_count": 0,
            "fail_json": json.dumps({"kind": "cookie"}),
            "log": "登录态预检失败",
        })
        balance_before = db.one(
            "SELECT balance FROM tenants WHERE id=2"
        )["balance"]
        logs_before = db.one(
            "SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2"
        )["n"]

        with patch.object(
                main.asyncio, "create_task", side_effect=self._discard_coro
        ) as create_task, patch.object(main, "_spawn_tool_worker") as spawn_tool:
            video = asyncio.run(main.task_center_retry("video", video_id))
            tool = asyncio.run(main.task_center_retry("tool", tool_id))
            meeting_result = asyncio.run(
                main.task_center_retry("meeting", meeting_id)
            )
            publish = asyncio.run(
                main.task_center_retry("publish", publish_id)
            )

        self.assertTrue(all(
            item["free_retry"]
            for item in (video, tool, meeting_result, publish)
        ))
        self.assertEqual(create_task.call_count, 3)
        spawn_tool.assert_called_once_with(tool_id)
        self.assertEqual(
            db.one(
                "SELECT status,billing_status,billing_points,retry_count,"
                "steps_json,error FROM tv_job WHERE id=?", (video_id,)
            ),
            {
                "status": "queued",
                "billing_status": "charged",
                "billing_points": 0,
                "retry_count": 1,
                "steps_json": "[]",
                "error": None,
            },
        )
        tool = db.one(
            "SELECT status,billing_status,billing_points,retry_count,"
            "retry_started_at,result_json,error FROM tool_job WHERE id=?",
            (tool_id,),
        )
        self.assertEqual(
            {key: tool[key] for key in (
                "status", "billing_status", "billing_points",
                "retry_count", "result_json", "error"
            )},
            {
                "status": "running",
                "billing_status": "charged",
                "billing_points": 0,
                "retry_count": 1,
                "result_json": None,
                "error": None,
            },
        )
        self.assertGreater(tool["retry_started_at"], 0)
        self.assertEqual(
            db.one(
                "SELECT status,phase,billing_status,retry_count,round_no,"
                "messages_json,proposals_json,validations_json "
                "FROM meeting WHERE id=?", (meeting_id,)
            ),
            {
                "status": "queued",
                "phase": "queued",
                "billing_status": "included",
                "retry_count": 1,
                "round_no": 0,
                "messages_json": "[]",
                "proposals_json": "[]",
                "validations_json": "[]",
            },
        )
        self.assertEqual(
            db.one(
                "SELECT status,retry_count,fail_json FROM pub_task WHERE id=?",
                (publish_id,),
            ),
            {"status": "queued", "retry_count": 1, "fail_json": None},
        )
        self.assertEqual(
            db.one("SELECT balance FROM tenants WHERE id=2")["balance"],
            balance_before,
        )
        self.assertEqual(
            db.one("SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2")["n"],
            logs_before,
        )

    def test_uncertain_external_delivery_retries_fail_closed(self):
        from app import auth, main

        auth.set_current({
            "id": 8, "tenant_id": 1, "role": "member",
            "modules": ["content"],
        })
        unsafe_publish = db.insert("pub_task", {
            "tenant_id": 1,
            "platform": "douyin",
            "account": "account-1",
            "payload_json": "{}",
            "status": "failed",
            "retry_count": 0,
            "fail_json": json.dumps({"kind": "net"}),
            "log": "标题正文已填充,提交发布…\n网络超时",
        })
        job_id = db.insert("job", {
            "tenant_id": 1,
            "brief_json": json.dumps({"direction": "公众号稿"}),
            "status": "done",
        })
        wechat_failed = db.insert("wechat_draft_delivery", {
            "tenant_id": 1,
            "job_id": job_id,
            "request_hash": "failed-hash",
            "request_key": "failed-key",
            "title": "失败草稿",
            "status": "failed",
            "billing_status": "refunded",
        })
        wechat_submitted_job = db.insert("job", {
            "tenant_id": 1,
            "brief_json": json.dumps({"direction": "待确认公众号稿"}),
            "status": "done",
        })
        wechat_submitted = db.insert("wechat_draft_delivery", {
            "tenant_id": 1,
            "job_id": wechat_submitted_job,
            "request_hash": "submitted-hash",
            "request_key": "submitted-key",
            "title": "待确认草稿",
            "status": "submitted",
            "billing_status": "charged",
        })

        for kind, rid in (
            ("publish", unsafe_publish),
            ("wechat", wechat_failed),
            ("wechat", wechat_submitted),
        ):
            with self.subTest(kind=kind, rid=rid):
                with self.assertRaises(HTTPException) as blocked:
                    asyncio.run(main.task_center_retry(kind, rid))
                self.assertEqual(blocked.exception.status_code, 409)

        self.assertEqual(
            db.one(
                "SELECT status,retry_count FROM pub_task WHERE id=?",
                (unsafe_publish,),
            ),
            {"status": "failed", "retry_count": 0},
        )
        failed_meta = next(
            item for item in taskcenter.list_items(
                1, {"content"}, limit=100, offset=0
            )["items"]
            if item["key"] == f"wechat:{wechat_failed}"
        )
        submitted_meta = next(
            item for item in taskcenter.list_items(
                1, {"content"}, limit=100, offset=0
            )["items"]
            if item["key"] == f"wechat:{wechat_submitted}"
        )
        self.assertFalse(failed_meta["retryable"])
        self.assertIn("回原内容工单", failed_meta["retry_block_reason"])
        self.assertFalse(submitted_meta["retryable"])
        self.assertIn("对账", submitted_meta["retry_block_reason"])

    def test_task_center_frontend_exposes_only_server_authorized_retry(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "static", "app.js"
        )
        with open(path, encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("/task-center/${encodeURIComponent(kind)}/${id}/retry", source)
        self.assertIn("x.retryable", source)
        self.assertIn("x.retry_block_reason", source)
        self.assertIn("t.retryable", source)
        self.assertNotIn(
            '<button class="btn sm" onclick="mxRetry(${t.id},this)">🔁 重试</button>',
            source,
        )

    def test_schedule_source_and_module_scope(self):
        sid = db.insert("schedule", {"tenant_id": 1, "name": "每日行业选题",
                                     "brief_json": "{}"})
        jid = db.insert("job", {"tenant_id": 1,
                                "brief_json": json.dumps({"direction": "今天发什么"}),
                                "status": "running", "source_schedule_id": sid,
                                "created_at": 5, "updated_at": 5})
        aid = db.insert("avatar_job", {"tenant_id": 1,
                                       "params_json": json.dumps({"script": "数字人口播"}),
                                       "status": "queued", "created_at": 6, "updated_at": 6})

        content = taskcenter.list_items(1, {"content"})["items"]
        job = next(x for x in content if x["kind"] == "content" and x["record_id"] == jid)
        self.assertEqual(job["source_label"], "定时任务·每日行业选题")
        self.assertEqual(job["source_route"], f"#/schedules/{sid}")
        self.assertFalse(any(x["kind"] == "avatar" for x in content))

        avatar = taskcenter.list_items(1, {"avatar"})["items"]
        self.assertTrue(any(x["kind"] == "avatar" and x["record_id"] == aid for x in avatar))
        self.assertFalse(any(x["kind"] in {"content", "expert", "meeting"} for x in avatar))

    def test_orphan_or_cross_tenant_meeting_source_does_not_leak_question(self):
        foreign_mid = db.insert("meeting", {"tenant_id": 2, "question": "别家商业机密",
                                            "emp_idxs_json": "[0]"})
        tid = self._task("来源异常任务", source_meeting_id=foreign_mid)
        row = next(x for x in taskcenter.list_items(1, {"content"})["items"]
                   if x["kind"] == "expert" and x["record_id"] == tid)
        self.assertEqual(row["source_detail"], "")
        self.assertNotIn("商业机密", json.dumps(row, ensure_ascii=False))

    def test_sse_distinguishes_root_from_ordinary_tenant_one_member(self):
        task_id = self._task("租户二任务", tenant=2, status="running")
        engine = Engine()
        q_member1, q_root, q_tenant2 = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
        engine.subscribers = {q_member1: (1, False), q_root: (1, True), q_tenant2: (2, False)}
        engine.broadcast({"type": "task_update", "task_id": task_id})
        self.assertTrue(q_member1.empty())
        self.assertFalse(q_root.empty())
        self.assertFalse(q_tenant2.empty())

        # 无法解析归属的事件只给平台 root，不得向全体广播。
        q_root.get_nowait()
        q_tenant2.get_nowait()
        engine.broadcast({"type": "employee_update", "idx": 0})
        self.assertTrue(q_member1.empty())
        self.assertFalse(q_root.empty())
        self.assertTrue(q_tenant2.empty())

    def test_sse_redacts_internal_station_actions_for_nonboss_only(self):
        job_id = db.insert("job", {
            "tenant_id": 1,
            "brief_json": json.dumps({"direction": "公开业务任务"}),
            "status": "running",
        })
        engine = Engine()
        q_member, q_root = asyncio.Queue(), asyncio.Queue()
        engine.subscribers = {q_member: (1, False), q_root: (1, True)}
        internal = {
            "type": "station_step",
            "job_id": job_id,
            "idx": 0,
            "n": 1,
            "step": {
                "k": "start",
                "l": "v1 开始执行(岗位:趋势官 / Skill:Horizon)",
                "ts": 123,
            },
        }

        engine.broadcast(internal)

        member_event = q_member.get_nowait()
        root_event = q_root.get_nowait()
        self.assertEqual(root_event, internal)
        self.assertEqual(member_event["type"], "station_step")
        self.assertEqual(member_event["step"]["l"], "员工正在处理任务")
        self.assertNotIn("Skill", json.dumps(member_event, ensure_ascii=False))
        self.assertNotIn("趋势官", json.dumps(member_event, ensure_ascii=False))

    def test_persisted_steps_are_generic_for_member_and_detailed_for_boss(self):
        from app import main

        raw = json.dumps([{
            "k": "search",
            "l": "使用 Skill:Horizon 检索内部渠道矩阵",
            "ts": 123,
        }], ensure_ascii=False)

        member = main._steps_for_view(raw, internal=False)
        boss = main._steps_for_view(raw, internal=True)

        self.assertEqual(member[0]["l"], "员工正在处理任务")
        self.assertNotIn("Horizon", json.dumps(member, ensure_ascii=False))
        self.assertEqual(boss[0]["l"], "使用 Skill:Horizon 检索内部渠道矩阵")

    def test_task_detail_obeys_member_module_scope(self):
        from app import auth, main
        tid = self._task("内容部单独派活")
        auth.set_current({"id": 8, "tenant_id": 1, "role": "member", "modules": ["avatar"]})
        with self.assertRaises(HTTPException) as denied:
            main._task_row_or_404(tid)
        self.assertEqual(denied.exception.status_code, 404)

        auth.set_current({"id": 8, "tenant_id": 1, "role": "member", "modules": ["content"]})
        detail = main.task_get(tid)
        self.assertEqual(detail["id"], tid)
        self.assertEqual(detail["source"]["type"], "direct")

    def test_tools_jobs_preserves_traceable_lead_urls(self):
        from app import auth, main
        url = "https://www.zhihu.com/question/123"
        jid = db.insert("tool_job", {
            "tenant_id": 1, "kind": "leads", "params_json": "{}",
            "result_json": json.dumps({"leads": [{"source_url": url}]}),
            "status": "done",
        })
        auth.set_current({"id": 8, "tenant_id": 1, "role": "member",
                          "modules": ["content"]})
        row = next(item for item in main.tool_jobs() if item["id"] == jid)
        self.assertEqual(row["result"]["leads"][0]["source_url"], url)

    def test_duplicate_lead_run_is_rejected_before_charging(self):
        from app import auth, main

        db.insert("tool_job", {
            "tenant_id": 1, "kind": "leads", "params_json": "{}",
            "status": "running",
        })
        auth.set_current({"id": 8, "tenant_id": 1, "role": "member",
                          "modules": ["content"]})
        with patch.object(main, "_charge") as charge:
            with self.assertRaises(HTTPException) as denied:
                asyncio.run(main.leads_gen({
                    "industry": "家装", "city": "成都", "product": "全屋定制",
                }))
        self.assertEqual(denied.exception.status_code, 429)
        charge.assert_not_called()

    def test_state_omits_large_profile_corpus_and_detail_loads_it_on_demand(self):
        from app import auth, main

        corpus = "历史作品" * 30000
        pid = db.insert("account_profile", {
            "tenant_id": 1, "name": "老板人设",
            "persona_json": json.dumps({
                "positioning": "企业经营",
                "corpus": corpus,
            }, ensure_ascii=False),
        })
        auth.set_current({"id": 8, "tenant_id": 1, "role": "member",
                          "modules": ["content"]})
        profile = next(item for item in main.state()["profiles"] if item["id"] == pid)
        self.assertNotIn("corpus", profile["persona"])
        self.assertTrue(profile["has_corpus"])
        self.assertEqual(main.get_profile(pid)["persona"]["corpus"], corpus)


    def test_paused_work_is_waiting_and_counted_as_open(self):
        tid = self._task("等老板恢复的任务", status="paused")

        result = taskcenter.list_items(1, {"content"})
        row = next(x for x in result["items"]
                   if x["kind"] == "expert" and x["record_id"] == tid)
        self.assertEqual(row["status_group"], "waiting")
        self.assertEqual(row["status_label"], "已暂停 · 等您恢复")
        self.assertEqual(result["counts"]["waiting"], 1)
        self.assertEqual(result["counts"]["open"], 1)

    def test_mixed_module_meeting_requires_every_participant_module(self):
        from app import auth, departments, main

        industry_idx = 9001
        industry_expert = {
            "idx": industry_idx, "dept_key": "restaurant", "dept_name": "餐饮产业部",
            "name": "经营顾问", "person": "", "duty": "经营诊断", "md": "岗位手册",
            "color": "#123456", "emoji": "🍜", "intro": "帮助门店诊断经营问题。",
        }
        mid = db.insert("meeting", {
            "tenant_id": 1, "question": "联合内容和餐饮团队上新",
            "emp_idxs_json": json.dumps([0, industry_idx]), "status": "running",
            "phase": "brainstorm", "created_at": 10, "updated_at": 10,
        })
        sourced_task = self._task("只执行内容部行动", source_meeting_id=mid)
        real_get = departments.get

        def expert_get(idx):
            return industry_expert if idx == industry_idx else real_get(idx)

        with patch.object(departments, "get", side_effect=expert_get):
            auth.set_current({"id": 8, "tenant_id": 1, "role": "member",
                              "modules": ["content"]})
            rows = taskcenter.list_items(1, {"content"})["items"]
            self.assertFalse(any(x["kind"] == "meeting" and x["record_id"] == mid
                                 for x in rows))
            source_row = next(x for x in rows if x["key"] == f"expert:{sourced_task}")
            self.assertEqual(source_row["source_route"], "")
            self.assertEqual(source_row["source_detail"], "")
            self.assertFalse(any(x["id"] == mid for x in main.meetings_list()))
            with self.assertRaises(HTTPException) as denied:
                main.meeting_get(mid)
            self.assertEqual(denied.exception.status_code, 404)
            with self.assertRaises(HTTPException):
                asyncio.run(main.meeting_execute(mid))
            with self.assertRaises(HTTPException):
                asyncio.run(main.meeting_ask(mid, {"question": "继续说"}))
            source_detail = main.task_get(sourced_task)["source"]
            self.assertEqual(source_detail["route"], "")
            self.assertEqual(source_detail["detail"], "")

            auth.set_current({"id": 8, "tenant_id": 1, "role": "member",
                              "modules": ["content", "restaurant"]})
            rows = taskcenter.list_items(1, {"content", "restaurant"})["items"]
            self.assertTrue(any(x["kind"] == "meeting" and x["record_id"] == mid
                                for x in rows))
            self.assertEqual(main.meeting_get(mid)["id"], mid)

    def test_auxiliary_details_obey_tenant_and_module_and_normalize_video_steps(self):
        from app import auth, main

        local = {
            "avatar": db.insert("avatar_job", {
                "tenant_id": 1, "params_json": json.dumps({"script": "本企业口播"}),
                "status": "queued", "created_at": 1, "updated_at": 1,
            }),
            "video": db.insert("tv_job", {
                "tenant_id": 1, "params_json": json.dumps({"title": "本企业成片"}),
                "steps_json": json.dumps([{"t": 12, "msg": "抽取素材"},
                                          {"t": 13, "msg": "渲染成片"}]),
                "status": "running", "created_at": 2, "updated_at": 13,
            }),
            "tool": db.insert("tool_job", {
                "tenant_id": 1, "kind": "leads", "params_json": "{}",
                "result_json": json.dumps({"leads": [{
                    "source_url": "https://www.zhihu.com/question/123",
                }]}),
                "status": "done", "created_at": 3, "updated_at": 3,
            }),
            "publish": db.insert("pub_task", {
                "tenant_id": 1, "platform": "小红书", "payload_json": "{}",
                "status": "queued", "created_at": 4, "updated_at": 4,
            }),
        }
        foreign = {
            "avatar": db.insert("avatar_job", {
                "tenant_id": 2, "params_json": "{}", "status": "queued",
            }),
            "video": db.insert("tv_job", {
                "tenant_id": 2, "params_json": "{}", "status": "queued",
            }),
            "tool": db.insert("tool_job", {
                "tenant_id": 2, "kind": "hot", "params_json": "{}", "status": "queued",
            }),
            "publish": db.insert("pub_task", {
                "tenant_id": 2, "platform": "小红书", "payload_json": "{}",
                "status": "queued",
            }),
        }

        for kind, rid in local.items():
            module = "avatar" if kind == "avatar" else "content"
            with self.subTest(kind=kind, boundary="module"):
                auth.set_current({"id": 8, "tenant_id": 1, "role": "member", "modules": []})
                with self.assertRaises(HTTPException) as denied:
                    main.task_center_record(kind, rid)
                self.assertEqual(denied.exception.status_code, 404)

            with self.subTest(kind=kind, boundary="tenant"):
                auth.set_current({"id": 8, "tenant_id": 1, "role": "member",
                                  "modules": [module]})
                self.assertEqual(main.task_center_record(kind, rid)["id"], rid)
                with self.assertRaises(HTTPException) as denied:
                    main.task_center_record(kind, foreign[kind])
                self.assertEqual(denied.exception.status_code, 404)

        video = main.task_center_record("video", local["video"])
        self.assertEqual(video["steps"], [
            {"k": "working", "l": "员工正在处理任务", "ts": 12},
            {"k": "working", "l": "员工正在处理任务", "ts": 13},
        ])
        auth.set_current({"id": 1, "tenant_id": 1, "username": "boss",
                          "role": "root", "modules": ["content", "avatar"]})
        boss_video = main.task_center_record("video", local["video"])
        self.assertEqual(boss_video["steps"], [
            {"k": "tool", "l": "抽取素材", "ts": 12},
            {"k": "tool", "l": "渲染成片", "ts": 13},
        ])
        tool = main.task_center_record("tool", local["tool"])
        self.assertEqual(tool["tool_kind"], "leads")
        self.assertEqual(
            tool["result"]["leads"][0]["source_url"],
            "https://www.zhihu.com/question/123",
        )

    def test_deleted_sources_have_no_dead_link_in_list_or_exact_detail(self):
        from app import auth, main

        meeting_task = self._task("会议已删除", source_meeting_id=99991)
        redo_task = self._task("原任务已删除", source_task_id=99992)
        schedule_job = db.insert("job", {
            "tenant_id": 1, "brief_json": json.dumps({"direction": "日程已删除"}),
            "status": "queued", "source_schedule_id": 99993,
            "created_at": 3, "updated_at": 3,
        })
        video_job = db.insert("tv_job", {
            "tenant_id": 1, "job_id": 99994, "params_json": "{}", "status": "queued",
            "created_at": 4, "updated_at": 4,
        })
        publish_job = db.insert("pub_task", {
            "tenant_id": 1, "platform": "小红书",
            "payload_json": json.dumps({"job_id": 99995}), "status": "queued",
            "created_at": 5, "updated_at": 5,
        })

        rows = {x["key"]: x for x in taskcenter.list_items(1, {"content"})["items"]}
        expected = {
            f"expert:{meeting_task}": "原会议已删除",
            f"expert:{redo_task}": "原任务已删除 · 本条为重做版本",
            f"content:{schedule_job}": "原定时任务已删除",
            f"video:{video_job}": "原内容工单已删除",
            f"publish:{publish_job}": "原内容工单已删除",
        }
        for key, label in expected.items():
            with self.subTest(key=key):
                self.assertEqual(rows[key]["source_label"], label)
                self.assertEqual(rows[key]["source_route"], "")

        auth.set_current({"id": 8, "tenant_id": 1, "role": "member",
                          "modules": ["content"]})
        detail = main.task_get(meeting_task)
        self.assertEqual(detail["source"]["label"], "原会议已删除")
        self.assertEqual(detail["source"]["route"], "")

    def test_manual_task_ignores_client_forged_source_fields(self):
        from app import auth, main

        auth.set_current({"id": 8, "tenant_id": 1, "role": "member",
                          "modules": ["content"]})

        def discard(coro):
            coro.close()
            return None

        body = {
            "emp_idx": 0,
            "brief": {"direction": "客户端正常活"},
            "source_meeting_id": 88881,
            "source_action_key": "forged-action",
            "source_task_id": 88882,
        }
        with patch.object(main, "_charge"), \
                patch.object(main.asyncio, "create_task", side_effect=discard):
            result = asyncio.run(main.task_create(body))

        row = db.one("SELECT source_meeting_id, source_action_key, source_task_id "
                     "FROM task WHERE id=?", (result["task_id"],))
        self.assertEqual(row, {"source_meeting_id": None,
                               "source_action_key": None,
                               "source_task_id": None})


if __name__ == "__main__":
    unittest.main()
