"""拍照工厂合并端点:一次上传跑双腿,按腿计费退款,余额语义合并。

此前前端并行调 product-shot 与 menu-copy,同一张照片上传两遍;失败时
老板分不清哪条腿的钱退没退。这组测试钉住合并端点的钱与结果语义:
- 双成:两腿各自完成计费,余额减 3;
- 单败:失败腿退点、成功腿保留,响应分别说明;
- 双败:HTTP 500 且 3 点全退;
- 半途没钱:第二腿 402 时第一腿也退,给合并后的提示。
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, billing, db, growth, main


PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


class PhotoFactoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(cls.tmp.name, "factory.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "租户甲", "balance": 100})
        cls.uid = db.insert("users", {
            "tenant_id": 2, "username": "factory-owner",
            "password_hash": "x", "role": "owner",
        })
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = cls.old_path
        cls.tmp.cleanup()

    def setUp(self):
        db.execute("UPDATE tenants SET balance=100 WHERE id=2")
        self.client.cookies.set("cc_sess", auth.make_session(self.uid))

    def _balance(self):
        return db.one("SELECT balance FROM tenants WHERE id=2")["balance"]

    def _post(self):
        return self.client.post(
            "/api/tools/photo-factory",
            files={"file": ("p.png", PNG, "image/png")},
            data={"scene": "木桌暖光", "want": "外卖描述"},
        )

    def test_both_legs_succeed_costs_three_points(self):
        async def ok_shot(tid, raw, scene):
            return b"fakepng"

        async def ok_copy(tid, b64, mime, want):
            return {"item": "招牌菜", "desc": "好吃"}

        with (
            patch.object(growth, "product_shot", ok_shot),
            patch.object(growth, "menu_copy", ok_copy),
        ):
            response = self._post()
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertTrue(body["file"].startswith("/files/tools/2/"))
        self.assertEqual("招牌菜", body["menu"]["item"])
        self.assertEqual("", body["image_error"])
        self.assertEqual("", body["copy_error"])
        self.assertEqual(97, self._balance(), "双成应恰好扣 3 点")

    def test_one_leg_fails_refunds_only_that_leg(self):
        async def bad_shot(tid, raw, scene):
            raise RuntimeError("上游挂了")

        async def ok_copy(tid, b64, mime, want):
            return {"item": "招牌菜"}

        with (
            patch.object(growth, "product_shot", bad_shot),
            patch.object(growth, "menu_copy", ok_copy),
        ):
            response = self._post()
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("", body["file"])
        self.assertIn("已退回", body["image_error"])
        self.assertEqual("招牌菜", body["menu"]["item"])
        self.assertEqual(99, self._balance(), "败腿的 2 点应退,成腿的 1 点保留")

    def test_both_legs_fail_refunds_everything(self):
        async def boom(*args, **kwargs):
            raise RuntimeError("全挂")

        with (
            patch.object(growth, "product_shot", boom),
            patch.object(growth, "menu_copy", boom),
        ):
            response = self._post()
        self.assertEqual(500, response.status_code)
        self.assertIn("全部退回", response.json()["detail"])
        self.assertEqual(100, self._balance())

    def test_insufficient_for_second_leg_refunds_first(self):
        db.execute("UPDATE tenants SET balance=2 WHERE id=2")   # 只够出图腿
        response = self._post()
        self.assertEqual(402, response.status_code)
        self.assertIn("3 点", response.json()["detail"])
        self.assertEqual(2, self._balance(), "第一腿必须退回,余额不动")

    def test_upload_limit_map_covers_new_endpoint(self):
        self.assertIn(
            ("POST", "/api/tools/photo-factory"),
            main._TRANSIENT_UPLOAD_ROUTES,
            "新端点必须进上传路由限额表,否则超大请求可绕过体积闸门",
        )


if __name__ == "__main__":
    unittest.main()
