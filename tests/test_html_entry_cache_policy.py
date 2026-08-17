"""HTML 入口必须绕过浏览器缓存，避免发布后继续启动旧 SPA。"""
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app import auth, db, main


class HtmlEntryCachePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(cls.tmp.name, "html-entry-cache.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "HTML 入口缓存测试租户"})
        user_id = db.insert("users", {
            "tenant_id": 2,
            "username": "html-entry-cache-owner",
            "password_hash": "fixture",
            "role": "owner",
        })
        cls.client = TestClient(main.app)
        cls.client.cookies.set("cc_sess", auth.make_session(user_id))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = cls.old_db_path
        cls.tmp.cleanup()

    def test_spa_and_login_html_are_never_cached(self):
        expected_cache_control = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )

        for path in ("/", "/login"):
            with self.subTest(path=path):
                response = self.client.get(path)

                self.assertEqual(200, response.status_code)
                self.assertTrue(
                    response.headers["content-type"].startswith("text/html")
                )
                self.assertEqual(
                    expected_cache_control,
                    response.headers.get("cache-control"),
                )
                self.assertEqual("no-cache", response.headers.get("pragma"))
                self.assertEqual("0", response.headers.get("expires"))

    def test_static_assets_do_not_inherit_html_no_store_policy(self):
        response = self.client.get("/static/app.js")

        self.assertEqual(200, response.status_code)
        static_policy = response.headers.get("cache-control", "").lower()
        self.assertNotIn("no-store", static_policy)
        self.assertNotIn("no-cache", static_policy)

    def test_spa_entry_pins_app_js_to_its_content_hash(self):
        """入口不缓存 + 脚本 URL 随内容哈希变：发版后浏览器必然拉到新 app.js。

        ?v=54 手工版本号在 schema55 连续发版中从未被更新过，老板端因此
        一直复用旧脚本缓存——版本号必须由服务端按文件内容注入。
        """
        import hashlib

        response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        with open(
            os.path.join(main.ROOT, "static", "app.js"), "rb"
        ) as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()[:12]
        self.assertIn(f"/static/app.js?v={digest}", response.text)
        self.assertNotIn("/static/app.js?v=54", response.text)


if __name__ == "__main__":
    unittest.main()
