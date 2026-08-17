"""V29 任务中心：统一聚合、来源追踪、租户/板块隔离与未完任务优先。"""
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import db, departments, employeeidentity, employees, taskcenter
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
        # Schema54 requires every executable task fixture to bind the exact
        # immutable role-config revision.  Individual fail-closed tests below
        # deliberately overwrite parts of this valid core binding.
        frozen = employeeidentity.task_fields(
            employeeidentity.active_employee(0)
        )
        frozen.update(extra)
        return db.insert("task", {"tenant_id": tenant, "emp_idx": 0,
                                   "brief_json": json.dumps({"direction": direction}),
                                   "status": status, "created_at": at, "updated_at": at,
                                   **frozen})

    @staticmethod
    def _member_snapshot(idx, dept="content", name=None, employee=None):
        employee = employee or employeeidentity.active_employee(idx)
        if employee and employee.get("dept_key") == dept:
            frozen = employeeidentity.snapshot(employee)
            if name is not None:
                frozen["name"] = name
            config = employees.ensure_role_config(employee)
            bundle = db.get_employee_role_bundle(
                config["identity_ref"], config["config_revision"],
                config["config_sha256"],
            )
            if not bundle:
                raise RuntimeError("测试员工 role bundle 缺失")
            return {
                **frozen,
                "identity_ref": employeeidentity.identity_ref(frozen),
                "config_revision": config["config_revision"],
                "config_sha256": config["config_sha256"],
                "person_snapshot": bundle.get("person_snapshot", ""),
                "identity_scheme": bundle.get("identity_scheme", "legacy-six"),
                "bundle_sha256": bundle["bundle_sha256"],
            }
        return {
            "idx": int(idx),
            "key": f"legacy.idx.{int(idx)}",
            "name": name or f"历史员工#{int(idx)}",
            "dept_key": dept,
            "catalog_version": "legacy-unknown",
            "spec_sha256": "legacy-unknown",
        }

    def _meeting(
        self, question, indices, *, tenant=1, depts=None,
        member_snapshots=None, **extra,
    ):
        depts = list(depts or ["content"] * len(indices))
        payload = {
            "tenant_id": tenant,
            "question": question,
            "emp_idxs_json": json.dumps(indices),
            "member_snapshot_json": json.dumps(
                member_snapshots or [
                    self._member_snapshot(idx, dept)
                    for idx, dept in zip(indices, depts)
                ],
                ensure_ascii=False,
            ),
        }
        payload.update(extra)
        return db.insert("meeting", payload)

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
        mid = self._meeting(
            "新品要不要上线", [0], status="done", phase="completed",
            created_at=20, updated_at=20,
        )
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

    def test_task_thread_lists_only_current_revision_with_round_label(self):
        first = self._task("初版工作", at=30)
        second = self._task(
            "第二轮改稿", at=40, source_task_id=first,
            revision_no=2, phase="revision",
        )
        now = 50
        thread_id = db.insert("task_thread", {
            "tenant_id": 1,
            "emp_idx": 0,
            "root_task_id": first,
            "current_task_id": second,
            "status": "active",
            "revision_count": 2,
            "created_at": now,
            "updated_at": now,
        })
        db.execute(
            "UPDATE task SET thread_id=?,revision_no=1 WHERE id=?",
            (thread_id, first),
        )
        db.execute(
            "UPDATE task SET thread_id=? WHERE id=?",
            (thread_id, second),
        )

        result = taskcenter.list_items(1, {"content"})
        rows = [x for x in result["items"] if x["kind"] == "expert"]
        self.assertEqual([second], [row["record_id"] for row in rows])
        self.assertEqual(1, result["kind_counts"]["expert"])
        self.assertEqual(2, rows[0]["revision_no"])
        self.assertEqual(thread_id, rows[0]["thread_id"])
        self.assertEqual("持续协作 · 第2轮", rows[0]["source_label"])

    def test_inspection_employee_task_routes_to_inspection_record(self):
        branch_id = db.insert("store_branch", {
            "tenant_id": 1,
            "industry_key": "restaurant",
            "name": "东城店",
            "region": "华东",
            "active": 1,
        })
        task_id = db.insert("task", {
            "tenant_id": 1,
            "emp_idx": 10,
            "brief_json": json.dumps({"direction": "巡店·东城店"}),
            "status": "done",
            "billing_status": "included",
            "created_at": 30,
            "updated_at": 40,
        })
        visit_id = db.insert("inspection_visit", {
            "tenant_id": 1,
            "industry_key": "restaurant",
            "branch_id": branch_id,
            "employee_idx": 10,
            "task_id": task_id,
            "request_key": "task-center-inspection-1",
            "status": "completed",
            "created_at": 30,
            "updated_at": 40,
        })

        result = taskcenter.list_items(1, {"content", "restaurant"})
        row = next(item for item in result["items"] if item["record_id"] == task_id)
        self.assertEqual("inspection", row["subkind"])
        self.assertEqual(
            f"#/inspections/{visit_id}/restaurant", row["target_route"]
        )
        self.assertEqual("巡店工作台 · 到店检查", row["source_label"])
        self.assertEqual("#/inspections/0/restaurant", row["source_route"])

    def test_inspection_task_is_filtered_by_visit_industry_not_shared_employee_idx(self):
        def add_visit(industry_key: str, direction: str, request_key: str) -> int:
            branch_id = db.insert("store_branch", {
                "tenant_id": 1,
                "industry_key": industry_key,
                "name": f"{industry_key}-store",
                "active": 1,
            })
            task_id = db.insert("task", {
                "tenant_id": 1,
                "emp_idx": 10,
                "brief_json": json.dumps({"direction": direction}),
                "status": "done",
            })
            db.insert("inspection_visit", {
                "tenant_id": 1,
                "industry_key": industry_key,
                "branch_id": branch_id,
                "employee_idx": 10,
                "task_id": task_id,
                "request_key": request_key,
                "status": "completed",
            })
            return task_id

        restaurant_task = add_visit(
            "restaurant", "RESTAURANT_VISIBLE", "inspection-restaurant"
        )
        auto_task = add_visit("auto", "AUTO_ONLY_SENTINEL", "inspection-auto")

        page = taskcenter.list_items(1, {"content", "restaurant"})
        ids = {item["record_id"] for item in page["items"]}
        self.assertIn(restaurant_task, ids)
        self.assertNotIn(auto_task, ids)
        self.assertNotIn(
            "AUTO_ONLY_SENTINEL",
            json.dumps(page, ensure_ascii=False),
        )

    def test_open_work_is_never_pushed_out_by_new_done_rows(self):
        running = self._task("很早开始但还没收口", status="running", at=1)
        for i in range(8):
            self._task(f"较新的完结任务{i}", status="done", at=100 + i)
        result = taskcenter.list_items(1, {"content"}, limit=1)
        self.assertEqual(result["items"][0]["record_id"], running)
        self.assertEqual(result["counts"]["open"], 1)
        self.assertEqual(result["counts"]["done"], 8)
        self.assertTrue(result["truncated"])

    def test_server_filters_before_pagination_and_keeps_global_counts(self):
        for i in range(101):
            self._task(
                f"进行中任务{i}",
                status="running",
                at=200 + i,
            )
        done = self._task("历史已完成任务", status="done", at=1)
        video = db.insert(
            "tv_job",
            {
                "tenant_id": 1,
                "params_json": json.dumps({"title": "历史图文成片"}),
                "status": "done",
                "created_at": 2,
                "updated_at": 2,
            },
        )

        page = taskcenter.list_items(
            1,
            {"content"},
            limit=100,
            offset=0,
            status="done",
            kind="expert",
        )
        self.assertEqual(101, page["counts"]["open"])
        self.assertEqual(2, page["counts"]["done"])
        self.assertEqual(103, page["counts"]["all"])
        self.assertEqual(102, page["kind_counts"]["expert"])
        self.assertEqual(1, page["kind_counts"]["video"])
        self.assertEqual(1, page["filtered_total"])
        self.assertEqual([done], [
            item["record_id"] for item in page["items"]
        ])
        self.assertFalse(page["has_more"])

        video_page = taskcenter.list_items(
            1,
            {"content"},
            limit=100,
            offset=0,
            status="all",
            kind="video",
        )
        self.assertEqual(1, video_page["filtered_total"])
        self.assertEqual([video], [
            item["record_id"] for item in video_page["items"]
        ])

    def test_global_search_survives_corrupt_json_in_every_json_backed_branch(self):
        """一条历史坏载荷不能让老板的整张任务总账报 malformed JSON。"""
        corrupt_rows = {
            "expert": self._task(
                "损坏专家检索片段", brief_json="损坏专家检索片段{",
                status="done", at=11,
            ),
            "content": db.insert("job", {
                "tenant_id": 1,
                "brief_json": "损坏内容检索片段{",
                "status": "done",
                "created_at": 12,
                "updated_at": 12,
            }),
            "video": db.insert("tv_job", {
                "tenant_id": 1,
                "params_json": "损坏视频检索片段{",
                "status": "done",
                "created_at": 13,
                "updated_at": 13,
            }),
            "tool": db.insert("tool_job", {
                "tenant_id": 1,
                "kind": "leads",
                "params_json": "损坏工具检索片段{",
                "status": "done",
                "created_at": 14,
                "updated_at": 14,
            }),
            "publish": db.insert("pub_task", {
                "tenant_id": 1,
                "platform": "小红书",
                "payload_json": "损坏发布检索片段{",
                "status": "done",
                "created_at": 15,
                "updated_at": 15,
            }),
            "avatar": db.insert("avatar_job", {
                "tenant_id": 1,
                "params_json": "损坏数字人检索片段{",
                "status": "done",
                "created_at": 16,
                "updated_at": 16,
            }),
        }
        queries = {
            "expert": "损坏专家检索片段",
            "content": "损坏内容检索片段",
            "video": "损坏视频检索片段",
            "tool": "损坏工具检索片段",
            "publish": "损坏发布检索片段",
            "avatar": "损坏数字人检索片段",
        }

        for kind, token in queries.items():
            with self.subTest(kind=kind):
                result = taskcenter.list_items(
                    1,
                    {"content", "avatar"},
                    limit=100,
                    offset=0,
                    q=token,
                )
                self.assertEqual(result["counts"]["all"], 1)
                self.assertEqual(result["filtered_total"], 1)
                self.assertEqual(
                    [item["key"] for item in result["items"]],
                    [f"{kind}:{corrupt_rows[kind]}"],
                )

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

        self.assertIn("const TC_PAGE_SIZE=100", source)
        self.assertIn(
            "function tcApiUrl(offset=0,snapshot=tcFilterSnapshot())", source
        )
        self.assertIn("status:snapshot.status", source)
        self.assertIn("kind:snapshot.kind", source)
        self.assertIn("function tcFilterSnapshot()", source)
        self.assertIn("request.seq===TC_REQUEST_SEQ", source)
        self.assertIn("tcFilterMatches(request.snapshot)", source)
        self.assertIn("api(tcApiUrl(0,snapshot))", source)
        self.assertIn("api(tcApiUrl(offset,snapshot))", source)
        self.assertIn("if(!tcRequestIsCurrent(request)||TC_DATA!==baseData) return", source)
        self.assertIn("TC_DATA.next_offset", source)
        self.assertIn("page.next_offset", source)
        self.assertIn("new Set(baseData.items.map", source)
        self.assertNotIn("limit=500&offset=0", source)
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
                # Playwright is an optional production dependency.  Keep this
                # deterministic browser fixture self-contained instead of
                # requiring the package merely to patch its import path.
                fake_playwright_pkg = types.ModuleType("playwright")
                fake_playwright_pkg.__path__ = []
                fake_playwright_api = types.ModuleType("playwright.async_api")
                fake_playwright_api.async_playwright = (
                    lambda: FakePlaywrightContext(page)
                )
                with patch.object(
                    matrixpub, "accounts", return_value=[account]
                ), patch.object(
                    matrixpub,
                    "_probe_login",
                    new=AsyncMock(return_value=(True, "测试账号")),
                ), patch.dict(
                    sys.modules,
                    {
                        "playwright": fake_playwright_pkg,
                        "playwright.async_api": fake_playwright_api,
                    },
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
        db.execute(
            "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
            "VALUES(2,'auto',1,0)"
        )
        visible_meeting = self._meeting(
            "汽车门店增长计划", [1601], tenant=2, depts=["auto"],
            **{
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

        with patch.object(main, "_start_meeting_worker"):
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

        hidden_meeting = self._meeting(
            "汽车渠道预算", [1602], tenant=2, depts=["auto"],
            **{
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
        meeting_id = self._meeting(
            "是否上线新品", [0, 1], tenant=2,
            **{
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

        with patch.object(main, "_start_text_video_worker") as start_video, \
                patch.object(main, "_spawn_tool_worker") as spawn_tool, \
                patch.object(main, "_start_meeting_worker") as start_meeting, \
                patch.object(main, "_start_publish_worker") as start_publish:
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
        start_video.assert_called_once_with(video_id)
        spawn_tool.assert_called_once_with(tool_id)
        start_meeting.assert_called_once_with(meeting_id)
        start_publish.assert_called_once_with(publish_id)
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
        foreign_mid = self._meeting("别家商业机密", [0], tenant=2)
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

    def test_frozen_task_scope_never_falls_back_to_live_or_content(self):
        from app import auth, main

        legacy = departments.legacy_specialists()[1401]
        pharmacy_id = self._task(
            "历史药房审计",
            emp_idx=legacy["idx"],
            **employeeidentity.task_fields(legacy),
        )
        unknown_id = self._task(
            "未知历史员工任务",
            emp_idx=99998,
            employee_key="legacy.unknown",
            employee_catalog_version="legacy-unknown",
            employee_name_snapshot="未知历史员工",
            employee_dept_key="unknown",
            employee_spec_sha256="b" * 64,
        )

        content_rows = taskcenter.list_items(1, {"content"})["items"]
        self.assertFalse(any(
            row["key"] in {f"expert:{pharmacy_id}", f"expert:{unknown_id}"}
            for row in content_rows
        ))
        pharmacy_rows = taskcenter.list_items(1, {"pharmacy"})["items"]
        frozen = next(
            row for row in pharmacy_rows
            if row["key"] == f"expert:{pharmacy_id}"
        )
        self.assertEqual(
            f"{legacy.get('person', '')}·{legacy['name']}".strip("·"),
            frozen["assignee"],
        )
        self.assertEqual("pharmacy", frozen["module"])
        self.assertEqual("legacy", frozen["roster_status"])
        self.assertFalse(frozen["can_assign"])
        self.assertFalse(any(
            row["key"] == f"expert:{unknown_id}" for row in pharmacy_rows
        ))

        auth.set_current({
            "id": 8, "tenant_id": 1, "role": "member", "modules": ["content"],
        })
        with self.assertRaises(HTTPException) as wrong_scope:
            main._task_row_or_404(pharmacy_id)
        self.assertEqual(404, wrong_scope.exception.status_code)
        with self.assertRaises(HTTPException) as unknown:
            main._task_row_or_404(unknown_id)
        self.assertEqual(404, unknown.exception.status_code)

        auth.set_current({
            "id": 8, "tenant_id": 1, "role": "member", "modules": ["pharmacy"],
        })
        self.assertEqual(pharmacy_id, main._task_row_or_404(pharmacy_id)["id"])
        pharmacy_detail = main.task_get(pharmacy_id)
        self.assertEqual(
            f"{legacy.get('person', '')}·{legacy['name']}".strip("·"),
            pharmacy_detail["emp_name"],
        )
        self.assertEqual(
            "零售药房行业痛点数字员工",
            pharmacy_detail["dept_name"],
        )
        self.assertEqual("legacy", pharmacy_detail["roster_status"])
        self.assertFalse(pharmacy_detail["can_assign"])

    def test_forged_or_padded_known_department_identity_fails_closed(self):
        from app import auth, main

        forged = self._task(
            "伪装成汽车岗位",
            emp_idx=0,
            employee_key="legacy.idx.0",
            employee_catalog_version="legacy-unknown",
            employee_name_snapshot="趋势官",
            employee_dept_key="auto",
            employee_spec_sha256="broken",
        )
        bad_core = self._task(
            "伪造内容部旧身份",
            emp_idx=0,
            employee_key="legacy.idx.0",
            employee_catalog_version="legacy-unknown",
            employee_name_snapshot="趋势官",
            employee_dept_key="content",
            employee_spec_sha256="broken",
        )
        active = departments.get_active(1601)
        padded_fields = employeeidentity.task_fields(active)
        padded_fields["employee_key"] += " "
        padded = self._task(
            "带空格的冻结身份", emp_idx=active["idx"], **padded_fields
        )
        exact = self._task(
            "真实现役汽车岗位",
            emp_idx=active["idx"],
            **employeeidentity.task_fields(active),
        )

        rows = taskcenter.list_items(1, {"auto"})
        keys = {row["key"] for row in rows["items"]}
        self.assertIn(f"expert:{exact}", keys)
        self.assertNotIn(f"expert:{forged}", keys)
        self.assertNotIn(f"expert:{padded}", keys)
        self.assertEqual(1, rows["counts"]["all"])
        current = next(row for row in rows["items"] if row["record_id"] == exact)
        self.assertEqual("active", current["roster_status"])
        self.assertTrue(current["can_assign"])

        auth.set_current({
            "id": 8, "tenant_id": 1, "role": "member", "modules": ["auto"],
        })
        self.assertEqual(exact, main.task_get(exact)["id"])
        for task_id in (forged, padded):
            with self.subTest(task_id=task_id), self.assertRaises(HTTPException) as denied:
                main.task_get(task_id)
            self.assertEqual(404, denied.exception.status_code)
        auth.set_current({
            "id": 8, "tenant_id": 1, "role": "member", "modules": ["content"],
        })
        content_keys = {
            row["key"] for row in taskcenter.list_items(1, {"content"})["items"]
        }
        self.assertNotIn(f"expert:{bad_core}", content_keys)
        with self.assertRaises(HTTPException) as denied_core:
            main.task_get(bad_core)
        self.assertEqual(404, denied_core.exception.status_code)

    def test_department_employee_uses_exact_identity_and_disabled_state(self):
        from app import auth, main

        active = departments.get_active(1601)
        pharmacy = departments.get_active(1401)
        good = self._task(
            "本岗位任务", emp_idx=active["idx"],
            **employeeidentity.task_fields(active),
        )
        wrong_fields = employeeidentity.task_fields(pharmacy)
        leaked = self._task(
            "同编号伪药房任务", emp_idx=active["idx"], **wrong_fields
        )
        employees.set_enabled(
            active["idx"], False,
            expected_row_version=employees.slot_state(active["idx"])[
                "row_version"
            ],
        )
        auth.set_current({
            "id": 1, "tenant_id": 1, "role": "root", "username": "boss",
            "modules": [],
        })

        dept = next(row for row in main.depts_list() if row["key"] == "auto")
        card = next(row for row in dept["employees"] if row["idx"] == active["idx"])
        self.assertEqual(1, card["tasks_n"])
        self.assertFalse(card["enabled"])
        self.assertFalse(card["can_assign"])
        self.assertFalse(card["can_learn"])

        detail = main.dept_emp(active["idx"])
        self.assertEqual([good], [row["id"] for row in detail["tasks"]])
        self.assertNotIn(leaked, [row["id"] for row in detail["tasks"]])
        self.assertEqual(1, detail["stats"]["runs"])
        self.assertFalse(detail["can_assign"])
        self.assertFalse(detail["can_learn"])
        task_card = next(
            row for row in taskcenter.list_items(1, {"auto"})["items"]
            if row["record_id"] == good
        )
        self.assertFalse(task_card["can_assign"])
        self.assertFalse(main.task_get(good)["can_assign"])

    def test_trash_task_requires_complete_frozen_identity(self):
        from app import auth, main

        good = self._task(
            "可恢复内容任务", deleted_at=10, delete_reason="用户移入回收站",
        )
        forged = self._task(
            "伪造冻结身份的回收站任务",
            emp_idx=0,
            employee_key="legacy.idx.0",
            employee_catalog_version="legacy-unknown",
            employee_name_snapshot="趋势官",
            employee_dept_key="auto",
            employee_spec_sha256="broken",
            deleted_at=11,
            delete_reason="用户移入回收站",
        )
        auth.set_current({
            "id": 1, "tenant_id": 1, "role": "owner", "modules": [],
        })
        keys = {(row["kind"], row["id"]) for row in main.trash_list()["items"]}
        self.assertIn(("task", good), keys)
        self.assertNotIn(("task", forged), keys)
        with self.assertRaises(HTTPException) as denied:
            main.trash_restore("task", forged)
        self.assertEqual(404, denied.exception.status_code)

    def test_meeting_visibility_uses_complete_frozen_roster_contract(self):
        historical_auto = next(
            employee for employee in departments.identity_versions(1601)
            if employee["catalog_version"] == "v1"
        )
        frozen_mid = self._meeting(
            "历史汽车会议", [1601], depts=["auto"], status="done",
            phase="completed", member_snapshots=[
                self._member_snapshot(
                    1601, "auto", employee=historical_auto,
                )
            ],
        )
        malformed_mid = db.insert("meeting", {
            "tenant_id": 1,
            "question": "成员编号与快照被调换",
            "emp_idxs_json": "[0]",
            "member_snapshot_json": json.dumps([
                self._member_snapshot(1, "content")
            ]),
            "status": "done",
            "phase": "completed",
        })
        duplicate_mid = db.insert("meeting", {
            "tenant_id": 1,
            "question": "重复成员编号不得进入任务中心",
            "emp_idxs_json": "[0,0]",
            "member_snapshot_json": json.dumps([
                self._member_snapshot(0, "content"),
                self._member_snapshot(0, "content"),
            ]),
            "status": "done",
            "phase": "completed",
        })

        self.assertFalse(any(
            row["key"] == f"meeting:{frozen_mid}"
            for row in taskcenter.list_items(1, {"content"})["items"]
        ))
        auto_rows = taskcenter.list_items(1, {"auto"})["items"]
        self.assertTrue(any(
            row["key"] == f"meeting:{frozen_mid}" for row in auto_rows
        ))
        self.assertFalse(any(
            row["key"] == f"meeting:{malformed_mid}" for row in auto_rows
        ))
        self.assertFalse(any(
            row["key"] == f"meeting:{duplicate_mid}"
            for row in taskcenter.list_items(1, {"content"})["items"]
        ))

        from app import auth, main
        auth.set_current({
            "id": 1, "tenant_id": 1, "role": "owner", "modules": [],
        })
        member = main.meeting_get(frozen_mid)["members"][0]
        self.assertEqual("legacy", member["roster_status"])
        self.assertFalse(member["can_assign"])

    def test_pseudo_industry_meeting_legacy_marker_is_hidden(self):
        from app import auth, main

        mid = db.insert("meeting", {
            "tenant_id": 1,
            "question": "伪装汽车员工会议",
            "emp_idxs_json": "[0]",
            "member_snapshot_json": json.dumps([{
                "idx": 0,
                "key": "legacy.idx.0",
                "name": "趋势官",
                "dept_key": "auto",
                "catalog_version": "legacy-unknown",
                "spec_sha256": "broken",
            }], ensure_ascii=False),
            "status": "done",
            "phase": "completed",
        })
        self.assertFalse(any(
            row["record_id"] == mid and row["kind"] == "meeting"
            for row in taskcenter.list_items(1, {"auto"})["items"]
        ))
        auth.set_current({
            "id": 1, "tenant_id": 1, "role": "owner", "modules": [],
        })
        self.assertFalse(any(row["id"] == mid for row in main.meetings_list()))
        with self.assertRaises(HTTPException) as denied:
            main.meeting_get(mid)
        self.assertEqual(404, denied.exception.status_code)

    def test_disabled_active_meeting_member_keeps_active_roster_label(self):
        from app import auth, main

        active = departments.get_active(1601)
        mid = self._meeting(
            "已停用员工的历史会议",
            [active["idx"]],
            depts=["auto"],
            status="done",
            phase="completed",
        )
        employees.set_enabled(
            active["idx"], False,
            expected_row_version=employees.slot_state(active["idx"])[
                "row_version"
            ],
        )
        auth.set_current({
            "id": 1, "tenant_id": 1, "role": "owner", "modules": [],
        })
        member = main.meeting_get(mid)["members"][0]
        self.assertEqual("active", member["roster_status"])
        self.assertFalse(member["enabled"])
        self.assertFalse(member["can_assign"])

    def test_non_integer_meeting_member_index_fails_closed(self):
        from app import auth, main

        frozen = self._member_snapshot(1601, "auto")
        mid = db.insert("meeting", {
            "tenant_id": 1,
            "question": "字符串成员编号",
            "emp_idxs_json": json.dumps([str(frozen["idx"])]),
            "member_snapshot_json": json.dumps([frozen], ensure_ascii=False),
            "status": "done",
            "phase": "completed",
        })
        self.assertFalse(any(
            row["record_id"] == mid and row["kind"] == "meeting"
            for row in taskcenter.list_items(1, {"auto"})["items"]
        ))
        auth.set_current({
            "id": 1, "tenant_id": 1, "role": "owner", "modules": [],
        })
        with self.assertRaises(HTTPException) as denied:
            main.meeting_get(mid)
        self.assertEqual(404, denied.exception.status_code)

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
        from app import auth, main

        industry_idx = 101
        mid = self._meeting(
            "联合内容和餐饮团队上新", [0, industry_idx],
            depts=["content", "restaurant"], status="running",
            phase="brainstorm", created_at=10, updated_at=10,
        )
        sourced_task = self._task("只执行内容部行动", source_meeting_id=mid)
        with patch.object(main, "_start_meeting_worker"):
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

        with patch.object(
            employeeidentity.departments, "get_active", return_value=None,
        ):
            config = employees.get_config(0)
        bundle = db.get_employee_role_bundle(
            config["identity_ref"], config["config_revision"],
            config["config_sha256"],
        )
        self.assertIsNotNone(bundle)
        body = {
            "emp_idx": 0,
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": bundle["bundle_sha256"],
            "brief": {"direction": "客户端正常活"},
            "request_key": "manual-task-source-forgery-0001",
            "source_meeting_id": 88881,
            "source_action_key": "forged-action",
            "source_task_id": 88882,
        }
        with patch.object(main, "_charge"), \
                patch.object(
                    main,
                    "_start_expert_task_worker",
                    return_value=None,
                ):
            result = asyncio.run(main.task_create(body))

        row = db.one("SELECT source_meeting_id, source_action_key, source_task_id "
                     "FROM task WHERE id=?", (result["task_id"],))
        self.assertEqual(row, {"source_meeting_id": None,
                               "source_action_key": None,
                               "source_task_id": None})


if __name__ == "__main__":
    unittest.main()
