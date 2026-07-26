"""异步边界回归：同步 SQLite 不得再冻结事件循环。

审核报告 P0-1:引擎流水线与 async 路由此前在事件循环上直接调 db,
busy_timeout=30s 下任何一次写锁等待都会把整个进程的所有协程(全部租户的
SSE、全部请求)一起冻住。修复方式是 db 异步门面(专用有界线程池)+
引擎协程的写路径全部经门面卸载。这些测试钉住修复:

- 门面语义:aq/aone/aexecute/arun 与同步版结果一致,事务整体进池;
- 核心断言:另一线程持有写锁时,事件循环仍能按时跳动;
- 引擎协程的源码不再包含直连写调用。
"""
import asyncio
import json
import os
import re
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

    def test_adrain_flushes_all_prior_submit_writes(self):
        """收尾冲刷:adrain 返回后,此前提交的异步写必须全部落地。"""
        async def scenario():
            tid = await db.ainsert("tenants", {"name": "初始"})
            for n in range(8):
                db.submit_write(db.update, "tenants", tid, {"name": f"第{n}版"})
            await db.adrain()
            row = await db.aone("SELECT name FROM tenants WHERE id=?", (tid,))
            self.assertTrue(str(row["name"]).startswith("第"),
                            f"冲刷后仍是初始值: {row['name']}")

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


class EngineCoroutineSourceTests(unittest.TestCase):
    """引擎协程内不得再出现直连写调用(读在 WAL 下不等写锁,允许保留)。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app", "engine.py",
        )
        with open(path, encoding="utf-8") as handle:
            cls.source = handle.read()

    def _async_bodies(self):
        """粗粒度切出每个 async def 的函数体(到下一个同级 def 为止)。"""
        pattern = re.compile(
            r"^    async def (\w+).*?(?=^    (?:async )?def |\Z)",
            re.M | re.S,
        )
        return {m.group(1): m.group(0) for m in pattern.finditer(self.source)}

    def test_async_methods_have_no_direct_write_calls(self):
        offenders = []
        for name, body in self._async_bodies().items():
            # 嵌套的同步事务闭包(def _xx_tx)里的写是合法的——它们经 arun 进池。
            stripped = re.sub(
                r"^\s+def _\w+\(.*?(?=^\s{8}\w|\Z)", "", body, flags=re.M | re.S)
            for bad in ("db.execute(", "db.update(", "db.insert(",
                        "with db.atomic()"):
                if bad in stripped:
                    offenders.append(f"{name}: {bad}")
        self.assertEqual(
            [], offenders,
            "引擎协程存在未经门面的直连写调用(会在写锁竞争时冻结事件循环)",
        )

    def test_progress_recorder_does_not_write_inline(self):
        recorder = self.source.split("def _recorder", 1)[1].split(
            "async def start", 1)[0]
        self.assertNotIn(
            "db.update(", recorder.replace("db.submit_write(db.update,", ""),
            "进度回调仍在调用线程上直接写库",
        )
        self.assertIn("db.submit_write", recorder)


if __name__ == "__main__":
    unittest.main()
