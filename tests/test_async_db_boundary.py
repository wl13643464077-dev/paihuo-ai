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


if __name__ == "__main__":
    unittest.main()
