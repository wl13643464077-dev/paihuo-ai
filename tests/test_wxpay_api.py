"""微信支付路由层契约(依赖 fastapi，CI 上运行)。

业务与协议细节见 tests/test_wxpay_payments.py；这里只钉住路由边界：
回调无需登录但必须验签、下单拒绝客户端金额、配置接口仅 root 可用且不回显密钥。
"""
import asyncio
import base64
import json
import os
import tempfile
import time
import unittest

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException

from app import auth, db, main, wxpay

APIV3_KEY = "0123456789abcdefABCDEF0123456789"
PUB_KEY_ID = "PUB_KEY_ID_0114232134912410000"


def _pem_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return key, private_pem, public_pem


class WxPayRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.merchant = _pem_pair()
        cls.platform = _pem_pair()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "wxpay-api.db")
        db.conn()
        for tid, name in ((1, "平台"), (2, "企业甲")):
            db.insert("tenants", {"id": tid, "name": name, "balance": 0})
        for uid, tid, role in ((1, 1, "root"), (20, 2, "owner")):
            db.insert("users", {
                "id": uid, "tenant_id": tid, "username": f"u{uid}",
                "password_hash": "x", "role": role, "modules_json": "[]",
                "enabled": 1,
            })

    def tearDown(self):
        auth.set_current(None)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _config(self):
        return {
            "enabled": True,
            "mchid": "1900000109",
            "appid": "wxd678efh567hg6787",
            "apiv3_key": APIV3_KEY,
            "private_key": self.merchant[1],
            "merchant_serial_no": "5157F09EFDC096DE15EBE81A47057A7232F1B8E1",
            "public_key_id": PUB_KEY_ID,
            "public_key": self.platform[2],
            "notify_url": "https://pay.example.com/api/pay/wxpay/notify",
        }

    def _signed(self, body: bytes) -> dict:
        ts = str(int(time.time()))
        message = f"{ts}\nnonce1\n{body.decode('utf-8')}\n".encode("utf-8")
        signature = self.platform[0].sign(message, padding.PKCS1v15(), hashes.SHA256())
        return {
            "Content-Type": "application/json",
            "Wechatpay-Timestamp": ts,
            "Wechatpay-Nonce": "nonce1",
            "Wechatpay-Signature": base64.b64encode(signature).decode("ascii"),
            "Wechatpay-Serial": PUB_KEY_ID,
        }

    def _post_notify(self, body: bytes, headers: dict):
        async def scenario():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="https://paihuo.test"
            ) as client:
                notify = await client.post(
                    "/api/pay/wxpay/notify", content=body, headers=headers
                )
                orders = await client.get("/api/pay/wxpay/orders/PH1")
                config = await client.get("/api/admin/wxpay/config")
            return notify, orders, config

        return asyncio.run(scenario())

    def test_notify_is_public_but_other_pay_routes_need_login(self):
        notify, orders, config = self._post_notify(b"{}", {"Content-Type": "application/json"})
        # 没配置时回调返回 503(让微信稍后重试)，而不是“请先登录”。
        self.assertEqual(503, notify.status_code)
        self.assertEqual("FAIL", notify.json()["code"])
        self.assertEqual(401, orders.status_code)
        self.assertEqual(401, config.status_code)

    def test_notify_rejects_bad_signature_with_401(self):
        wxpay.save_config(self._config())
        body = json.dumps({"event_type": "TRANSACTION.SUCCESS", "resource": {}}).encode()
        headers = self._signed(body)
        headers["Wechatpay-Signature"] = base64.b64encode(b"\1" * 256).decode()
        notify, _, _ = self._post_notify(body, headers)
        self.assertEqual(401, notify.status_code)
        self.assertEqual("FAIL", notify.json()["code"])

    def test_notify_valid_non_payment_event_is_acknowledged(self):
        wxpay.save_config(self._config())
        body = json.dumps({"event_type": "REFUND.SUCCESS", "resource": {}}).encode()
        notify, _, _ = self._post_notify(body, self._signed(body))
        self.assertEqual(200, notify.status_code)
        self.assertEqual("SUCCESS", notify.json()["code"])

    def test_order_create_rejects_client_amount_and_reports_disabled(self):
        auth.set_current(auth.get_user(20))
        with self.assertRaises(HTTPException) as forged:
            main.wxpay_order_create({"plan": "trial", "period": "month", "amount": 1})
        self.assertEqual(400, forged.exception.status_code)
        with self.assertRaises(HTTPException) as disabled:
            main.wxpay_order_create({"plan": "trial", "period": "month"})
        self.assertEqual(409, disabled.exception.status_code)
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM pay_order")["n"])

    def test_config_is_root_only_and_masks_secrets(self):
        auth.set_current(auth.get_user(20))
        with self.assertRaises(HTTPException) as owner:
            main.wxpay_config_get()
        self.assertEqual(403, owner.exception.status_code)
        auth.set_current(auth.get_user(1))
        view = main.wxpay_config_put(self._config())
        self.assertTrue(view["ready"])
        text = json.dumps(main.wxpay_config_get(), ensure_ascii=False)
        self.assertNotIn(APIV3_KEY, text)
        self.assertNotIn("PRIVATE KEY", text)
        with self.assertRaises(HTTPException) as bad:
            main.wxpay_config_put({"apiv3_key": "short"})
        self.assertEqual(400, bad.exception.status_code)
        self.assertTrue(main.purchase_catalog()["online_pay"]["wxpay"])


if __name__ == "__main__":
    unittest.main()
