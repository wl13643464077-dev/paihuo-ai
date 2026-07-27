"""异步边界回归：同步 SQLite 不得再冻结事件循环。

审核报告 P0-1:引擎流水线与 async 路由此前在事件循环上直接调 db,
busy_timeout=30s 下任何一次写锁等待都会把整个进程的所有协程(全部租户的
SSE、全部请求)一起冻住。修复方式是 db 异步门面(专用有界线程池)+
引擎协程的写路径全部经门面卸载。这些测试钉住修复:

- 门面语义:aq/aone/aexecute/arun 与同步版结果一致,事务整体进池;
- 核心断言:另一线程持有写锁时,事件循环仍能按时跳动;
- submit_write 严格 FIFO，旧进度不能覆盖新进度，同时不占用并发读池;
- 全 app 协程的源码不再包含直连同步 DB 调用。
"""
import asyncio
import ast
import contextvars
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from app import db


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "async.db")
        db.conn()

        def _restore():
            if db._conn is not None:
                db._conn.close()
            db._conn = None
            db.DB_PATH = self.old_path

        self.addCleanup(_restore)


class AsyncFacadeSemanticsTests(_DbCase):
    def test_async_wrappers_match_sync_results(self):
        async def scenario():
            tid = await db.ainsert("tenants", {"name": "异步租户"})
            row = await db.aone("SELECT * FROM tenants WHERE id=?", (tid,))
            self.assertEqual("异步租户", row["name"])
            await db.aupdate("tenants", tid, {"name": "改名"})
            rows = await db.aq("SELECT name FROM tenants WHERE id=?", (tid,))
            self.assertEqual([{"name": "改名"}], rows)
            n = await db.aexecute(
                "UPDATE tenants SET name=? WHERE id=?", ("再改", tid))
            self.assertEqual(1, n)
            await db.aset_setting("k1", "v1")
            self.assertEqual("v1", await db.aget_setting("k1"))

        asyncio.run(scenario())

    def test_arun_executes_whole_transaction_in_pool(self):
        """事务体必须整体进池:持有 BEGIN 的过程中不存在 await 点。"""
        async def scenario():
            def _tx():
                self.assertTrue(
                    threading.current_thread().name.startswith("dbio"),
                    "事务应在 db 线程池中执行",
                )
                with db.atomic() as c:
                    c.execute("INSERT INTO tenants(name) VALUES('tx甲')")
                    c.execute("INSERT INTO tenants(name) VALUES('tx乙')")
                return True

            self.assertTrue(await db.arun(_tx))
            n = await db.aone("SELECT COUNT(*) AS n FROM tenants")
            self.assertEqual(2, n["n"])

        asyncio.run(scenario())

    def test_arun_propagates_exceptions(self):
        async def scenario():
            def _boom():
                raise ValueError("inner")

            with self.assertRaises(ValueError):
                await db.arun(_boom)

        asyncio.run(scenario())

    def test_arun_preserves_request_context(self):
        """租户/用户 ContextVar 必须随 DB 卸载传播，不能在线程池里退回默认租户。"""
        marker = contextvars.ContextVar("marker", default="missing")

        async def scenario():
            token = marker.set("tenant-42")
            try:
                self.assertEqual(
                    "tenant-42",
                    await db.arun(marker.get),
                )
            finally:
                marker.reset(token)

        asyncio.run(scenario())

    def test_strict_arun_switches_to_new_database_generation(self):
        """测试/维护切回 DB_PATH 后，新严格写应落新库而非抛 StaleWriteError。"""
        new_path = os.path.join(self.tmp.name, "strict-generation.db")

        async def scenario():
            await db.ainsert("tenants", {"name": "旧代"})
            db.DB_PATH = new_path
            tenant_id = await db.ainsert("tenants", {"name": "新代"})
            row = await db.aone(
                "SELECT name FROM tenants WHERE id=?", (tenant_id,)
            )
            self.assertEqual("新代", row["name"])
            self.assertEqual(os.path.abspath(new_path), db._conn_path)

        asyncio.run(scenario())

    def test_arun_waits_for_inflight_generation_switch_without_freezing_loop(self):
        """切库 drain 旧 worker 时，新严格写等待并最终只写入新代。"""
        entered = threading.Event()
        release = threading.Event()
        new_path = os.path.join(self.tmp.name, "concurrent-generation.db")

        def old_generation_work():
            db.conn()
            entered.set()
            release.wait(timeout=5)

        async def scenario():
            old_task = asyncio.create_task(db.arun(old_generation_work))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            db.DB_PATH = new_path
            strict_write = asyncio.create_task(
                db.ainsert("tenants", {"name": "切换期间提交"})
            )
            # 如果 generation lock 在事件循环上同步等待，这个 sleep 无法返回。
            await asyncio.wait_for(asyncio.sleep(0.05), timeout=0.5)
            self.assertFalse(strict_write.done())
            release.set()
            await old_task
            tenant_id = await asyncio.wait_for(strict_write, timeout=5)
            row = await db.aone(
                "SELECT name FROM tenants WHERE id=?", (tenant_id,)
            )
            self.assertEqual("切换期间提交", row["name"])
            self.assertEqual(os.path.abspath(new_path), db._conn_path)

        try:
            asyncio.run(scenario())
        finally:
            release.set()

    def test_adrain_flushes_all_prior_submit_writes_in_order(self):
        """收尾冲刷后必须是最后快照，不能被较早的慢写反向覆盖。"""
        async def scenario():
            tid = await db.ainsert("tenants", {"name": "初始"})

            def _snapshot(version):
                # 第一帧刻意很慢。多 worker 会让后续帧先提交、最终又被第一帧覆盖；
                # FIFO 单写队列必须仍以最后提交的第 7 版收口。
                if version == 0:
                    time.sleep(0.15)
                db.update("tenants", tid, {"name": f"第{version}版"})

            for n in range(8):
                db.submit_write(_snapshot, n)
            await db.adrain()
            row = await db.aone("SELECT name FROM tenants WHERE id=?", (tid,))
            self.assertEqual("第7版", row["name"])

        asyncio.run(scenario())

    def test_concurrent_append_transactions_do_not_lose_updates(self):
        """读-改-写整事务进池后,并发追加一条都不能丢(会议发言/发布日志的语义)。"""
        async def scenario():
            tid = await db.ainsert("tenants", {"name": "[]"})

            def _append(i):
                with db.atomic() as c:
                    row = c.execute(
                        "SELECT name FROM tenants WHERE id=?", (tid,)).fetchone()
                    items = json.loads(row["name"])
                    items.append(i)
                    c.execute("UPDATE tenants SET name=? WHERE id=?",
                              (json.dumps(items), tid))

            for i in range(12):
                db.submit_write(_append, i)
            await db.adrain()
            row = await db.aone("SELECT name FROM tenants WHERE id=?", (tid,))
            self.assertEqual(set(range(12)), set(json.loads(row["name"])),
                             "并发追加丢失了更新")

        asyncio.run(scenario())

    def test_submit_write_lands_eventually_and_swallows_errors(self):
        async def scenario():
            tid = await db.ainsert("tenants", {"name": "快照"})
            db.submit_write(db.update, "tenants", tid, {"name": "落库"})
            # 坏写入不得让池线程带着异常死掉。
            db.submit_write(db.execute, "THIS IS NOT SQL", ())
            for _ in range(50):
                row = await db.aone(
                    "SELECT name FROM tenants WHERE id=?", (tid,))
                if row["name"] == "落库":
                    return
                await asyncio.sleep(0.02)
            self.fail("submit_write 的写入始终未落库")

        asyncio.run(scenario())

    def test_ordered_writes_do_not_consume_read_workers(self):
        """单写队列被慢快照占住时，读仍从并发池立即返回。"""
        entered = threading.Event()
        release = threading.Event()

        async def scenario():
            tid = await db.ainsert("tenants", {"name": "可读"})

            def _slow_snapshot():
                entered.set()
                release.wait(timeout=5)
                db.update("tenants", tid, {"name": "写完"})

            db.submit_write(_slow_snapshot)
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            row = await asyncio.wait_for(
                db.aone("SELECT name FROM tenants WHERE id=?", (tid,)),
                timeout=1,
            )
            self.assertEqual("可读", row["name"])
            release.set()
            await db.adrain()

        try:
            asyncio.run(scenario())
        finally:
            release.set()

    def test_database_generation_switch_reclaims_worker_connections(self):
        """反复切库后连接数必须稳定，不能每代遗留一组已关闭连接。"""
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db._close_thread_connection()
        counts = []

        async def open_all_workers():
            barrier = threading.Barrier(4)

            def use_read_connection():
                db.conn()
                barrier.wait(timeout=5)

            await asyncio.gather(*(db.arun(use_read_connection) for _ in range(4)))
            db.submit_write(db.get_setting, "generation-probe")
            await db.adrain()

        for generation in range(3):
            db.DB_PATH = os.path.join(
                self.tmp.name, f"generation-{generation}.db"
            )
            db.conn()
            asyncio.run(open_all_workers())
            counts.append(len(db._all_connections))

        self.assertEqual(
            [6, 6, 6],
            counts,
            f"数据库代际切换后连接持续增长: {counts}",
        )

    def test_meeting_transcript_append_is_awaited_and_reliable(self):
        """逐字稿是业务记录；返回前必须落库，写失败必须冒泡。"""
        from app import meeting

        async def scenario():
            meeting_id = await db.ainsert(
                "meeting",
                {
                    "tenant_id": 1,
                    "question": "可靠写测试",
                    "emp_idxs_json": "[0,1]",
                },
            )
            events = []
            await meeting._push(
                meeting_id,
                events.append,
                "系统",
                "已可靠落库",
            )
            row = await db.aone(
                "SELECT messages_json FROM meeting WHERE id=?",
                (meeting_id,),
            )
            self.assertEqual(
                ["已可靠落库"],
                [item["text"] for item in json.loads(row["messages_json"])],
            )
            self.assertEqual(1, len(events))

            with mock.patch.object(
                meeting.db,
                "arun",
                new=mock.AsyncMock(side_effect=sqlite3.OperationalError("disk")),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    await meeting._push(
                        meeting_id,
                        events.append,
                        "系统",
                        "不能假装写成功",
                    )
            self.assertEqual(1, len(events), "落库失败后不应广播幽灵消息")

        asyncio.run(scenario())

    def test_matrix_log_append_is_not_fire_and_forget(self):
        """发布日志是追溯证据，不得交给会吞异常的 submit_write。"""
        import inspect
        from app import matrixpub

        source = inspect.getsource(matrixpub._log)
        self.assertNotIn("submit_write", source)

        async def scenario():
            pid = await db.ainsert(
                "pub_task",
                {
                    "tenant_id": 1,
                    "platform": "xhs",
                    "account": "test",
                    "payload_json": "{}",
                },
            )
            await db.arun(matrixpub._log, pid, "可靠日志")
            row = await db.aone(
                "SELECT log FROM pub_task WHERE id=?", (pid,)
            )
            self.assertIn("可靠日志", row["log"])

        asyncio.run(scenario())


class EventLoopFreezeTests(_DbCase):
    """本轮修复的核心断言:写锁竞争不再冻结事件循环。"""

    def test_loop_stays_responsive_while_write_lock_is_held(self):
        release = threading.Event()
        holding = threading.Event()

        def hold_write_lock():
            raw = sqlite3.connect(db.DB_PATH, timeout=30)
            try:
                raw.execute("PRAGMA busy_timeout=30000")
                raw.execute("BEGIN IMMEDIATE")     # 占住唯一写者位
                raw.execute("INSERT INTO tenants(name) VALUES('霸占者')")
                holding.set()
                release.wait(timeout=10)
                raw.commit()
            finally:
                raw.close()

        locker = threading.Thread(target=hold_write_lock, daemon=True)
        locker.start()
        self.assertTrue(holding.wait(timeout=5), "写锁线程未就绪")

        async def scenario():
            # 经门面发起一次会等写锁的写入——它应当只挂起池线程,不挂起循环。
            blocked_write = asyncio.ensure_future(
                db.aexecute("INSERT INTO tenants(name) VALUES('等锁者')"))

            # 同时事件循环必须保持心跳:100ms 的 sleep 不得被拖到秒级。
            beats = []
            for _ in range(5):
                t0 = time.monotonic()
                await asyncio.sleep(0.05)
                beats.append(time.monotonic() - t0)
            self.assertLess(
                max(beats), 1.0,
                f"事件循环被 db 写锁冻结: 心跳间隔 {beats}",
            )
            self.assertFalse(
                blocked_write.done(),
                "写入不应在锁释放前完成——否则本测试没有真正制造竞争",
            )

            release.set()                          # 放锁,等锁的写入应正常完成
            await asyncio.wait_for(blocked_write, timeout=10)

        asyncio.run(scenario())
        locker.join(timeout=5)

    def test_pool_is_bounded(self):
        """池子必须有界:SQLite 只有一个写者,无界线程只会排队占资源。"""
        pool = db._pool()
        self.assertLessEqual(pool._max_workers, 8)
        self.assertEqual(1, db._ordered_write_pool()._max_workers)


class AppCoroutineSourceTests(unittest.TestCase):
    """整个 app 的协程都必须遵守同步 SQLite 边界。"""

    SYNC_DB_CALLS = {
        "q", "one", "execute", "update", "insert",
        "get_setting", "set_setting", "atomic", "conn",
    }

    @classmethod
    def setUpClass(cls):
        cls.app_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"
        )
        engine_path = os.path.join(cls.app_dir, "engine.py")
        with open(engine_path, encoding="utf-8") as handle:
            cls.engine_source = handle.read()

    @classmethod
    def _db_calls(cls, node):
        calls = []
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "db"
                and func.attr in cls.SYNC_DB_CALLS
            ):
                calls.append((child.lineno, func.attr))
        return calls

    def test_all_app_coroutines_have_no_inline_sync_db_calls(self):
        offenders = []
        for filename in sorted(os.listdir(self.app_dir)):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(self.app_dir, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for coroutine in (
                node for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef)
            ):
                calls = []

                class DirectVisitor(ast.NodeVisitor):
                    def visit_AsyncFunctionDef(self, node):
                        if node is coroutine:
                            self.generic_visit(node)

                    def visit_FunctionDef(self, node):
                        return

                    def visit_Lambda(self, node):
                        return

                    def visit_Call(self, node):
                        func = node.func
                        if (
                            isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "db"
                            and func.attr in self_outer.SYNC_DB_CALLS
                        ):
                            calls.append((node.lineno, func.attr))
                        self.generic_visit(node)

                self_outer = self
                DirectVisitor().visit(coroutine)
                offenders.extend(
                    f"{filename}:{line} {coroutine.name}:db.{name}"
                    for line, name in calls
                )
        self.assertEqual(
            [], offenders,
            "app 协程存在未经门面的同步 DB 调用(会在锁竞争/磁盘 I/O 时冻结事件循环)",
        )

    def test_nested_db_transactions_are_only_dispatched_through_facade(self):
        """协程内同步事务闭包只能作为 arun/submit_write 的工作项，不能直接调用。"""
        offenders = []
        for filename in sorted(os.listdir(self.app_dir)):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(self.app_dir, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for coroutine in (
                node for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef)
            ):
                safe_names = set()
                for call in ast.walk(coroutine):
                    if not isinstance(call, ast.Call) or not call.args:
                        continue
                    func = call.func
                    if (
                        isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "db"
                        and func.attr in {"arun", "submit_write"}
                        and isinstance(call.args[0], ast.Name)
                    ):
                        safe_names.add(call.args[0].id)
                for nested in (
                    node for node in ast.walk(coroutine)
                    if isinstance(node, ast.FunctionDef)
                ):
                    calls = self._db_calls(nested)
                    if calls and nested.name not in safe_names:
                        offenders.append(
                            f"{filename}:{nested.lineno} "
                            f"{coroutine.name}.{nested.name}"
                        )
        self.assertEqual(
            [], offenders,
            "协程中的同步 DB 闭包没有交给 arun/submit_write",
        )

    def test_progress_recorder_does_not_write_inline(self):
        recorder = self.engine_source.split("def _recorder", 1)[1].split(
            "async def start", 1)[0]
        self.assertNotIn(
            "db.update(", recorder.replace("db.submit_write(db.update,", ""),
            "进度回调仍在调用线程上直接写库",
        )
        self.assertIn("db.submit_write", recorder)

    def test_submit_write_is_reserved_for_overwriting_progress_snapshots(self):
        """fire-and-forget 只准用于可丢且后续会整体覆盖的进度快照。"""
        expected = {
            "avatar.py": 1,
            "engine.py": 1,
            "main.py": 1,
            "taskrunner.py": 1,
        }
        found = {}
        for filename in sorted(os.listdir(self.app_dir)):
            if not filename.endswith(".py") or filename == "db.py":
                continue
            path = os.path.join(self.app_dir, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            count = sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "db"
                and node.func.attr == "submit_write"
            )
            if count:
                found[filename] = count
        self.assertEqual(expected, found)


class ReviewedAsyncCallGraphTests(unittest.TestCase):
    """钉住审查确认的 async→同步 DB helper 调用边。

    只扫本次放行范围，不把一次可靠性修复膨胀成全仓改写。helper 作为
    ``db.arun(helper, ...)`` 参数不会形成调用边；直接 ``helper(...)`` 会。
    """

    TARGETS = {
        ("engine.py", "_advance_once"): {
            "self._latest_run", "self._job_tenant", "self._job_title",
            "notify.push",
        },
        ("engine.py", "_run_gate"): {
            "self.collect_outputs", "self._job_tenant", "self._job_title",
            "notify.push",
        },
        ("engine.py", "_execute"): {"providers.text_model_for"},
        ("main.py", "_auth_mw"): {
            "auth.parse_session", "auth.get_user",
            "_upload_permission_allowed", "_file_owner_tid",
        },
        ("main.py", "task_create"): {
            "_need_module", "employees.is_enabled",
            "_create_charged_expert_task", "funnel.record_first_work",
        },
        ("main.py", "task_redo"): {
            "_task_row_or_404", "_create_charged_expert_task",
        },
        ("main.py", "avatar_job_create"): {
            "avatar.cloned_voices", "_avatar_asset_name",
            "_create_charged_avatar_job",
        },
        ("main.py", "meeting_create"): {
            "meeting.emp_brief", "_create_charged_meeting",
        },
        ("main.py", "job_text_video"): {
            "_job_or_404", "build_delivery", "_create_charged_tv_job",
        },
        ("main.py", "text_video_create"): {
            "textvideo.resolve_clip_path", "_create_charged_tv_job",
        },
        ("main.py", "_tool_watchdog_loop"): {"_recover_stale_tool_jobs"},
        ("main.py", "_tool_enqueue_async"): {
            "_tool_require_idle", "_tool_enqueue_record",
        },
        ("main.py", "pcal_gen"): {"_tool_require_idle", "_tool_enqueue"},
        ("main.py", "hotpick_gen"): {"_tool_require_idle", "_tool_enqueue"},
        ("main.py", "warmup_gen"): {"_tool_require_idle", "_tool_enqueue"},
        ("main.py", "leads_gen"): {"_tool_require_idle", "_tool_enqueue"},
        ("main.py", "bench_run"): {
            "growth.watch_conf", "_tool_require_idle", "_tool_enqueue",
        },
        ("matrixpub.py", "check_account"): {"accounts", "_save"},
        ("matrixpub.py", "_publish_xhs"): {
            "_log", "_mark_submission_uncertain",
        },
        ("matrixpub.py", "_publish_douyin"): {
            "_log", "_mark_submission_uncertain",
        },
        ("matrixpub.py", "_run_task_inner"): {"_log"},
        ("textvideo.py", "run_job"): {"_steps_append"},
        ("textvideo.py", "_run_job_inner"): {"_steps_append"},
        ("avatar.py", "clone_voice"): {
            "providers.yunwu_conf", "cloned_voices", "save_cloned_voices",
        },
        ("expertmatch.py", "match_experts"): {"_visible_specialists"},
        ("expertmatch.py", "preflight_fit"): {"_dept_peers"},
        ("providers.py", "chat"): {"yunwu_conf"},
        ("providers.py", "_chat_content"): {"yunwu_conf"},
        ("providers.py", "call_vision"): {"text_model_for"},
        ("providers.py", "image"): {"yunwu_conf"},
        ("providers.py", "call_image"): {"image_model_for"},
        ("providers.py", "_image_edit"): {"yunwu_conf"},
        ("providers.py", "edit_image"): {"image_model_for"},
        ("providers.py", "call_text"): {"text_model_for", "yunwu_conf"},
        ("providers.py", "call_web_json"): {
            "text_model_for", "yunwu_conf",
        },
    }

    @staticmethod
    def _call_name(call):
        parts = []
        node = call.func
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return ""

    def test_reviewed_async_call_graph_has_no_blocking_sync_edges(self):
        app_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"
        )
        offenders = []
        for (filename, function_name), forbidden in self.TARGETS.items():
            path = os.path.join(app_dir, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            matches = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == function_name
            ]
            self.assertEqual(
                1, len(matches), f"{filename}:{function_name} 不再是唯一 async 入口"
            )
            target = matches[0]

            class EdgeVisitor(ast.NodeVisitor):
                def visit_AsyncFunctionDef(self, node):
                    if node is target:
                        self.generic_visit(node)

                def visit_FunctionDef(self, node):
                    return

                def visit_Lambda(self, node):
                    return

                def visit_Call(self, node):
                    name = ReviewedAsyncCallGraphTests._call_name(node)
                    if name in forbidden:
                        offenders.append(
                            f"{filename}:{node.lineno} "
                            f"{function_name}->{name}"
                        )
                    self.generic_visit(node)

            EdgeVisitor().visit(target)
        self.assertEqual(
            [],
            offenders,
            "审查范围仍存在 async→同步 DB helper 调用边:\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
