"""微信支付 Native 扫码付款：协议层(签名/验签/解密)与订单落账的行为测试。

全部走真实函数 + 临时 SQLite；微信支付接口用 httpx.MockTransport 模拟，
“微信平台”的应答同样用测试生成的 RSA 密钥签名，完整走一遍验签。
不依赖 fastapi。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import billing, db, purchases, qrsvg, wxpay
from app.session_secret import CONFIG_KEY_ENV, REQUIRE_CONFIG_KEY_ENV

ROOT = Path(__file__).resolve().parents[1]
APIV3_KEY = "0123456789abcdefABCDEF0123456789"
PUB_KEY_ID = "PUB_KEY_ID_0114232134912410000"


def _keypair():
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


class _Keys:
    merchant = None
    platform = None

    @classmethod
    def load(cls):
        if cls.merchant is None:
            cls.merchant = _keypair()
            cls.platform = _keypair()
        return cls


def _config(**updates) -> dict:
    keys = _Keys.load()
    config = {
        "enabled": True,
        "mchid": "1900000109",
        "appid": "wxd678efh567hg6787",
        "apiv3_key": APIV3_KEY,
        "private_key": keys.merchant[1],
        "merchant_serial_no": "5157F09EFDC096DE15EBE81A47057A7232F1B8E1",
        "public_key_id": PUB_KEY_ID,
        "public_key": keys.platform[2],
        "notify_url": "https://pay.example.com/api/pay/wxpay/notify",
    }
    config.update(updates)
    return config


def _platform_headers(body: bytes, *, timestamp=None, nonce="n0nce123", serial=PUB_KEY_ID) -> dict:
    keys = _Keys.load()
    ts = str(int(timestamp if timestamp is not None else time.time()))
    message = f"{ts}\n{nonce}\n{body.decode('utf-8')}\n".encode("utf-8")
    signature = keys.platform[0].sign(message, padding.PKCS1v15(), hashes.SHA256())
    return {
        "Wechatpay-Timestamp": ts,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Signature": base64.b64encode(signature).decode("ascii"),
        "Wechatpay-Serial": serial,
    }


class WxPayProtocolTests(unittest.TestCase):
    """签名、验签、GCM 解密：用自己生成的密钥对和已知明文做往返。"""

    def setUp(self):
        self.keys = _Keys.load()
        self.config = _config()

    def test_request_signature_round_trips_with_merchant_public_key(self):
        body = '{"amount":{"total":19900}}'
        header = wxpay.build_authorization(
            self.config, "POST", "/v3/pay/transactions/native", body,
            timestamp="1700000000", nonce="abc123",
        )
        self.assertTrue(header.startswith("WECHATPAY2-SHA256-RSA2048 "))
        fields = dict(re.findall(r'(\w+)="([^"]*)"', header))
        self.assertEqual("1900000109", fields["mchid"])
        self.assertEqual(self.config["merchant_serial_no"], fields["serial_no"])
        message = (
            "POST\n/v3/pay/transactions/native\n1700000000\nabc123\n" + body + "\n"
        ).encode("utf-8")
        # 用商户公钥验证：签名串格式与算法都符合 APIv3。
        self.keys.merchant[0].public_key().verify(
            base64.b64decode(fields["signature"]),
            message, padding.PKCS1v15(), hashes.SHA256(),
        )

    def test_platform_signature_verification_accepts_valid_and_rejects_tampering(self):
        body = b'{"code_url":"weixin://wxpay/bizpayurl?pr=abc"}'
        headers = _platform_headers(body)
        wxpay.verify_headers(self.config, headers, body)
        with self.assertRaises(wxpay.WxPaySignatureError):
            wxpay.verify_headers(self.config, headers, body.replace(b"abc", b"abd"))
        with self.assertRaises(wxpay.WxPaySignatureError):
            wxpay.verify_headers(self.config, _platform_headers(body, serial="PUB_KEY_ID_OTHER01"), body)
        with self.assertRaises(wxpay.WxPaySignatureError):
            wxpay.verify_headers(
                self.config, _platform_headers(body, timestamp=time.time() - 3600), body
            )
        probe = dict(headers, **{"Wechatpay-Signature": "WECHATPAY/SIGNTEST/xxxx"})
        with self.assertRaises(wxpay.WxPaySignatureError):
            wxpay.verify_headers(self.config, probe, body)
        # 用商户自己的密钥冒充平台签名也不行。
        other = _config(public_key=self.keys.merchant[2])
        with self.assertRaises(wxpay.WxPaySignatureError):
            wxpay.verify_headers(other, headers, body)
        # 头名大小写不敏感。
        wxpay.verify_headers(self.config, {k.lower(): v for k, v in headers.items()}, body)

    def test_gcm_resource_round_trip_and_rejects_wrong_key_or_tamper(self):
        plaintext = json.dumps({"out_trade_no": "PH1", "trade_state": "SUCCESS"})
        resource = wxpay.encrypt_resource(APIV3_KEY, plaintext, nonce="fdasflkja484")
        self.assertEqual(
            {"out_trade_no": "PH1", "trade_state": "SUCCESS"},
            wxpay.decrypt_resource(APIV3_KEY, resource),
        )
        with self.assertRaises(wxpay.WxPaySignatureError):
            wxpay.decrypt_resource("x" * 32, resource)
        raw = bytearray(base64.b64decode(resource["ciphertext"]))
        raw[0] ^= 1
        with self.assertRaises(wxpay.WxPaySignatureError):
            wxpay.decrypt_resource(
                APIV3_KEY, dict(resource, ciphertext=base64.b64encode(bytes(raw)).decode())
            )
        with self.assertRaises(wxpay.WxPaySignatureError):
            wxpay.decrypt_resource(APIV3_KEY, dict(resource, associated_data="other"))

    def test_time_expire_is_beijing_rfc3339(self):
        self.assertEqual(
            "2023-11-15T06:13:20+08:00", wxpay.format_time_expire(1700000000.5)
        )


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        self._reset()
        db.DB_PATH = os.path.join(self.tmp.name, "wxpay.db")
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(CONFIG_KEY_ENV, None)
        os.environ.pop(REQUIRE_CONFIG_KEY_ENV, None)
        db.conn()
        for tid, name in ((1, "平台"), (2, "企业甲"), (3, "企业乙")):
            db.insert("tenants", {"id": tid, "name": name, "balance": 0})
        for uid, tid, role in ((1, 1, "root"), (20, 2, "owner"), (21, 2, "member"), (30, 3, "owner")):
            db.insert("users", {
                "id": uid, "tenant_id": tid, "username": f"u{uid}",
                "password_hash": "x", "role": role, "modules_json": "[]", "enabled": 1,
            })
        purchases._ALERTED_MISMATCH.clear()

    def tearDown(self):
        self._reset()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    @staticmethod
    def _reset():
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None


class WxPayConfigTests(_DbCase):
    def test_default_is_disabled_and_catalog_hides_online_pay(self):
        self.assertFalse(wxpay.is_enabled())
        catalog = purchases.catalog()
        self.assertEqual({"wxpay": False}, catalog["online_pay"])
        self.assertEqual("offline_confirmation", catalog["payment_mode"])
        with self.assertRaises(purchases.PaymentUnavailable):
            purchases.create_wxpay_order(2, 20, plan_key="trial", period_key="month")

    def test_save_validates_masks_secrets_and_keeps_blank_secrets(self):
        with self.assertRaises(wxpay.WxPayConfigError):
            wxpay.save_config({"apiv3_key": "short"})
        with self.assertRaises(wxpay.WxPayConfigError):
            wxpay.save_config({"notify_url": "http://pay.example.com/notify"})
        with self.assertRaises(wxpay.WxPayConfigError):
            wxpay.save_config({"private_key": "-----BEGIN PRIVATE KEY-----\nxx\n"})
        # 缺字段时不允许打开开关。
        with self.assertRaises(wxpay.WxPayConfigError) as missing:
            wxpay.save_config({"enabled": True, "mchid": "1900000109"})
        self.assertIn("APIv3 密钥", str(missing.exception))

        view = wxpay.save_config(_config())
        self.assertTrue(view["ready"])
        dumped = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(APIV3_KEY, dumped)
        self.assertNotIn("PRIVATE KEY", dumped)
        self.assertTrue(view["apiv3_key_set"] and view["private_key_set"])

        # 再次保存时密钥留空=不修改。
        wxpay.save_config({"apiv3_key": "", "private_key": "", "appid": "wxd678efh567hg6788"})
        stored = wxpay.load_config()
        self.assertEqual(APIV3_KEY, stored["apiv3_key"])
        self.assertEqual("wxd678efh567hg6788", stored["appid"])
        self.assertTrue(purchases.catalog()["online_pay"]["wxpay"])

        wxpay.save_config({"enabled": False})
        self.assertFalse(wxpay.is_enabled())
        wxpay.save_config({"clear": True})
        self.assertEqual("", wxpay.load_config()["mchid"])


class _FakeWeChat:
    """模拟微信支付服务端：记录请求、按脚本回应答，并用平台私钥签名。"""

    def __init__(self, test):
        self.test = test
        self.requests: list[httpx.Request] = []
        self.trade_state = "NOTPAY"
        self.paid_total = None
        self.native_status = 200

    def transaction(self, out_trade_no, total, state="SUCCESS", transaction_id=None):
        config = wxpay.load_config()
        return {
            "mchid": config["mchid"],
            "appid": config["appid"],
            "out_trade_no": out_trade_no,
            "transaction_id": transaction_id or ("4200" + out_trade_no[-12:]),
            "trade_type": "NATIVE",
            "trade_state": state,
            "trade_state_desc": "支付成功",
            "success_time": "2026-09-25T10:00:00+08:00",
            "amount": {"total": total, "payer_total": total, "currency": "CNY"},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == wxpay.NATIVE_PATH:
            if self.native_status != 200:
                return self._reply(self.native_status, {"code": "SYSTEM_ERROR", "message": "系统繁忙"}, sign=False)
            payload = json.loads(request.content)
            return self._reply(200, {"code_url": "weixin://wxpay/bizpayurl?pr=" + payload["out_trade_no"][-8:]})
        match = re.fullmatch(r"/v3/pay/transactions/out-trade-no/([^/]+)(/close)?", path)
        if match and match.group(2):
            return self._reply(204, None)
        if match:
            order = db.one("SELECT amount_fen FROM pay_order WHERE out_trade_no=?", (match.group(1),))
            total = self.paid_total if self.paid_total is not None else order["amount_fen"]
            return self._reply(200, self.transaction(match.group(1), total, self.trade_state))
        return self._reply(404, {"code": "NOT_FOUND"}, sign=False)

    def _reply(self, status, payload, sign=True):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = _platform_headers(body) if sign else {}
        return httpx.Response(status, content=body, headers=headers)

    def client(self):
        return httpx.Client(
            base_url=wxpay.API_BASE, transport=httpx.MockTransport(self.handler)
        )


class PayOrderTests(_DbCase):
    def setUp(self):
        super().setUp()
        wxpay.save_config(_config())
        self.fake = _FakeWeChat(self)
        client_patch = patch.object(wxpay, "_client", self.fake.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.quote = billing.subscription_quote("startup", "quarter")

    def _create(self, uid=20, tid=2, plan="startup", period="quarter", now=None):
        return purchases.create_wxpay_order(tid, uid, plan_key=plan, period_key=period, now=now)

    def _notify(self, order_no, total=None, *, event_type="TRANSACTION.SUCCESS", headers=None, body=None, now=None):
        if body is None:
            row = db.one("SELECT amount_fen FROM pay_order WHERE out_trade_no=?", (order_no,))
            transaction = self.fake.transaction(order_no, total if total is not None else row["amount_fen"])
            event = {
                "id": "EV-2018022511223320873",
                "create_time": "2026-09-25T10:00:01+08:00",
                "resource_type": "encrypt-resource",
                "event_type": event_type,
                "summary": "支付成功",
                "resource": {
                    "original_type": "transaction",
                    **wxpay.encrypt_resource(APIV3_KEY, json.dumps(transaction)),
                },
            }
            body = json.dumps(event).encode("utf-8")
        return purchases.handle_wxpay_notify(headers or _platform_headers(body), body, now=now)

    def _balance(self, tid=2):
        return db.one("SELECT balance FROM tenants WHERE id=?", (tid,))["balance"] or 0

    def _subscribe_ops(self):
        return db.one("SELECT COUNT(*) n FROM billing_operation WHERE action='subscribe'")["n"]

    def test_order_amount_comes_only_from_server_quote_and_request_is_signed(self):
        result = self._create()
        item = result["item"]
        self.assertTrue(result["created"])
        self.assertEqual(self.quote["price"] * 100, item["amount_fen"])
        self.assertTrue(item["code_url"].startswith("weixin://"))
        self.assertIn("<svg", item["qr_svg"])
        request = self.fake.requests[-1]
        payload = json.loads(request.content)
        self.assertEqual({"total": self.quote["price"] * 100, "currency": "CNY"}, payload["amount"])
        self.assertEqual(item["order_no"], payload["out_trade_no"])
        self.assertEqual("https://pay.example.com/api/pay/wxpay/notify", payload["notify_url"])
        self.assertTrue(payload["time_expire"].endswith("+08:00"))
        # 请求头签名可以用商户公钥验证。
        fields = dict(re.findall(r'(\w+)="([^"]*)"', request.headers["Authorization"]))
        message = (
            f"POST\n{wxpay.NATIVE_PATH}\n{fields['timestamp']}\n{fields['nonce_str']}\n"
            f"{request.content.decode('utf-8')}\n"
        ).encode("utf-8")
        _Keys.merchant[0].public_key().verify(
            base64.b64decode(fields["signature"]), message, padding.PKCS1v15(), hashes.SHA256()
        )
        stored = db.one("SELECT * FROM pay_order WHERE out_trade_no=?", (item["order_no"],))
        self.assertEqual("created", stored["status"])
        self.assertAlmostEqual(stored["created_at"] + 7200, stored["expires_at"], places=3)

        # 同一套餐未过期的订单直接复用，不重复下单。
        again = self._create()
        self.assertFalse(again["created"])
        self.assertEqual(item["order_no"], again["item"]["order_no"])
        self.assertEqual(1, sum(1 for r in self.fake.requests if r.url.path == wxpay.NATIVE_PATH))

    def test_member_platform_and_invalid_plan_cannot_order(self):
        with self.assertRaises(purchases.PurchaseForbidden):
            self._create(uid=21)
        with self.assertRaises(purchases.PurchaseForbidden):
            self._create(uid=1, tid=1)
        with self.assertRaises(purchases.PurchaseError):
            self._create(plan="free-gift")
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM pay_order")["n"])

    def test_gateway_failure_closes_local_order(self):
        self.fake.native_status = 500
        with self.assertRaises(purchases.PaymentGatewayError):
            self._create()
        row = db.one("SELECT status,close_reason FROM pay_order")
        self.assertEqual("closed", row["status"])

    def test_notify_activates_once_even_when_replayed(self):
        order_no = self._create()["item"]["order_no"]
        status, reply = self._notify(order_no)
        self.assertEqual((200, "SUCCESS"), (status, reply["code"]))
        self.assertEqual(self.quote["points"], self._balance())
        tenant = db.one("SELECT plan,plan_expires FROM tenants WHERE id=2")
        self.assertIn("创业版", tenant["plan"])
        row = db.one("SELECT * FROM pay_order WHERE out_trade_no=?", (order_no,))
        self.assertEqual("paid", row["status"])
        self.assertEqual(f"wxpay:{order_no}", row["subscription_op_key"])
        digest = json.loads(row["notify_digest"])
        self.assertEqual(64, len(digest["body_sha256"]))
        self.assertNotIn("payer", digest)

        for _ in range(3):
            status, reply = self._notify(order_no)
            self.assertEqual((200, "SUCCESS"), (status, reply["code"]))
        # 查单再确认一次也不会重复开通。
        self.fake.trade_state = "SUCCESS"
        purchases.pay_order_status(2, 20, order_no, now=time.time() + 60)
        self.assertEqual(self.quote["points"], self._balance())
        self.assertEqual(1, self._subscribe_ops())
        self.assertEqual(
            1, db.one("SELECT COUNT(*) n FROM billing_log WHERE tenant_id=2 AND delta>0")["n"]
        )
        view = purchases.pay_order_status(2, 20, order_no)["item"]
        self.assertEqual("paid", view["status"])
        self.assertTrue(view["activated"])
        self.assertEqual("", view["code_url"])

    def test_bad_signature_returns_401_and_changes_nothing(self):
        order_no = self._create()["item"]["order_no"]
        row = db.one("SELECT amount_fen FROM pay_order WHERE out_trade_no=?", (order_no,))
        body = json.dumps({
            "event_type": "TRANSACTION.SUCCESS",
            "resource": wxpay.encrypt_resource(
                APIV3_KEY, json.dumps(self.fake.transaction(order_no, row["amount_fen"]))
            ),
        }).encode("utf-8")
        forged = _platform_headers(body)
        forged["Wechatpay-Signature"] = base64.b64encode(b"\0" * 256).decode()
        status, reply = self._notify(order_no, body=body, headers=forged)
        self.assertEqual((401, "FAIL"), (status, reply["code"]))
        # 签名对但报文被改过也不行。
        status, _ = self._notify(order_no, body=body + b" ", headers=_platform_headers(body))
        self.assertEqual(401, status)
        self.assertEqual("created", db.one("SELECT status FROM pay_order")["status"])
        self.assertEqual(0, self._balance())
        self.assertEqual(0, self._subscribe_ops())

    def test_amount_mismatch_is_rejected_without_state_change(self):
        order_no = self._create()["item"]["order_no"]
        status, reply = self._notify(order_no, total=1)
        self.assertEqual((400, "FAIL"), (status, reply["code"]))
        self.assertEqual("created", db.one("SELECT status FROM pay_order")["status"])
        self.assertEqual(0, self._balance())
        # 查单返回的金额不对同样不入账。
        self.fake.trade_state = "SUCCESS"
        self.fake.paid_total = 100
        purchases.pay_order_status(2, 20, order_no, now=time.time() + 60)
        self.assertEqual("created", db.one("SELECT status FROM pay_order")["status"])
        self.assertEqual(0, self._subscribe_ops())

    def test_unknown_order_and_non_success_events(self):
        self._create()
        body = json.dumps({"event_type": "REFUND.SUCCESS", "resource": {}}).encode()
        self.assertEqual(200, self._notify("x", body=body)[0])
        missing = self.fake.transaction("PH20260925000000ABCDEF123456", 100)
        body = json.dumps({
            "event_type": "TRANSACTION.SUCCESS",
            "resource": wxpay.encrypt_resource(APIV3_KEY, json.dumps(missing)),
        }).encode()
        self.assertEqual(404, self._notify("x", body=body)[0])
        self.assertEqual(0, self._subscribe_ops())

    def test_status_polling_other_tenant_cannot_see_order(self):
        order_no = self._create()["item"]["order_no"]
        with self.assertRaises(purchases.PurchaseNotFound):
            purchases.pay_order_status(3, 30, order_no)
        with self.assertRaises(purchases.PurchaseNotFound):
            purchases.pay_order_status(2, 20, "../../etc")

    def test_expired_order_is_closed_after_remote_check_and_late_payment_still_opens_once(self):
        start = time.time()
        order_no = self._create(now=start)["item"]["order_no"]
        report = purchases.sweep_expired_pay_orders(now=start + 7200 - 1)
        self.assertEqual(0, report["checked"])
        report = purchases.sweep_expired_pay_orders(now=start + 7200 + 1)
        self.assertEqual({"checked": 1, "closed": 1, "paid": 0}, report)
        row = db.one("SELECT status,close_reason FROM pay_order")
        self.assertEqual("closed", row["status"])
        self.assertIn("2 小时", row["close_reason"])
        paths = [r.url.path for r in self.fake.requests]
        self.assertTrue(any(p.endswith("/close") for p in paths))
        self.assertEqual("", purchases.pay_order_status(2, 20, order_no)["item"]["code_url"])

        # 关单前最后一刻付的钱，回调晚到：照样开通，而且只开一次。
        self.assertEqual(200, self._notify(order_no)[0])
        self.assertEqual(200, self._notify(order_no)[0])
        self.assertEqual("paid", db.one("SELECT status FROM pay_order")["status"])
        self.assertEqual(self.quote["points"], self._balance())
        self.assertEqual(1, self._subscribe_ops())

    def test_sweep_settles_orders_paid_right_before_expiry(self):
        start = time.time()
        self._create(now=start)
        self.fake.trade_state = "SUCCESS"
        report = purchases.sweep_expired_pay_orders(now=start + 7300)
        self.assertEqual({"checked": 1, "closed": 0, "paid": 1}, report)
        self.assertEqual(self.quote["points"], self._balance())

    def test_sweep_without_config_closes_locally(self):
        start = time.time()
        self._create(now=start)
        wxpay.save_config({"clear": True})
        report = purchases.sweep_expired_pay_orders(now=start + 7300)
        self.assertEqual(1, report["closed"])

    def test_lazy_status_poll_closes_expired_order(self):
        start = time.time()
        order_no = self._create(now=start)["item"]["order_no"]
        item = purchases.pay_order_status(2, 20, order_no, now=start + 7300)["item"]
        self.assertEqual("closed", item["status"])

    def test_paid_order_marks_matching_purchase_intent_paid(self):
        intent = purchases.create_intent(
            2, 20, request_key="request-owner-a-0001", plan_key="startup",
            period_key="quarter", contact="微信 a",
        )["item"]
        order_no = self._create()["item"]["order_no"]
        self.assertEqual(intent["id"], db.one("SELECT intent_id FROM pay_order")["intent_id"])
        self._notify(order_no)
        row = db.one("SELECT status,subscription_op_key FROM purchase_intent WHERE id=?", (intent["id"],))
        self.assertEqual(("paid", f"wxpay:{order_no}"), (row["status"], row["subscription_op_key"]))
        own = purchases.list_own(2, 20)["items"][0]
        self.assertIn("微信付款已到账", own["status_message"])
        # root 之后再点“确认到账”不会重复开通。
        result = purchases.transition(
            intent["id"], expected_status="requested", target_status="paid", actor_id=1
        )
        self.assertFalse(result["changed"])
        self.assertEqual(self.quote["points"], self._balance())
        self.assertEqual(1, self._subscribe_ops())

    def test_price_change_after_order_keeps_money_but_flags_manual_activation(self):
        order_no = self._create()["item"]["order_no"]
        changed_plans = [dict(plan) for plan in billing.PLANS]
        for plan in changed_plans:
            if plan["key"] == "startup":
                plan["sale"] = 249
        with patch.object(billing, "PLANS", changed_plans):
            status, _ = self._notify(order_no)
        self.assertEqual(200, status)
        row = db.one("SELECT status,subscription_op_key,activation_error FROM pay_order")
        self.assertEqual("paid", row["status"])
        self.assertIsNone(row["subscription_op_key"])
        self.assertIn("价格", row["activation_error"])
        self.assertEqual(0, self._balance())
        admin = purchases.list_pay_orders_admin()
        self.assertEqual(1, admin["paid_count"])
        self.assertTrue(admin["items"][0]["activation_error"])


class ManualConfirmationStillOpensOnceTests(_DbCase):
    """抽出 activate_paid_subscription 后，root 人工确认到账的老路径行为不变。"""

    def test_root_paid_transition_opens_plan_once(self):
        intent = purchases.create_intent(
            2, 20, request_key="request-owner-a-0002", plan_key="trial",
            period_key="month", contact="电话 1",
        )["item"]
        result = purchases.transition(
            intent["id"], expected_status="requested", target_status="paid", actor_id=1
        )
        self.assertTrue(result["changed"])
        quote = billing.subscription_quote("trial", "month")
        self.assertEqual(quote["points"], db.one("SELECT balance FROM tenants WHERE id=2")["balance"])
        again = purchases.transition(
            intent["id"], expected_status="requested", target_status="paid", actor_id=1
        )
        self.assertFalse(again["changed"])
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM billing_operation")["n"])


class PointExamplesAndReferenceTests(_DbCase):
    def test_point_examples_follow_price_settings(self):
        examples = {item["action"]: item["points"] for item in purchases.catalog()["point_examples"]}
        self.assertEqual(billing.DEFAULT_PRICES["content_job"]["points"], examples["content_job"])
        custom = json.loads(json.dumps(billing.DEFAULT_PRICES))
        custom["content_job"]["points"] = 25
        db.set_setting("prices", json.dumps(custom))
        examples = {item["action"]: item["points"] for item in purchases.catalog()["point_examples"]}
        self.assertEqual(25, examples["content_job"])

    def test_embedded_reference_prices_match_server_catalog(self):
        expected = purchases.reference_catalog()
        promo = (ROOT / "static" / "promo.html").read_text(encoding="utf-8")
        app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        embedded_promo = re.search(
            r'<script type="application/json" id="plan-reference">(.*?)</script>', promo, re.S
        ).group(1)
        embedded_app = re.search(r"^const PLAN_REFERENCE=(.*?);$", app_js, re.M).group(1)
        self.assertEqual(expected, json.loads(embedded_promo))
        self.assertEqual(expected, json.loads(embedded_app))
        for quote in expected["quotes"]:
            self.assertEqual(
                billing.subscription_quote(quote["plan"], quote["period"]), quote
            )


class QrSvgTests(unittest.TestCase):
    def test_builtin_encoder_emits_valid_format_information(self):
        text = "weixin://wxpay/bizpayurl?pr=AbCdEfG12"
        grid = qrsvg.matrix(text)
        self.assertEqual(29, len(grid))  # 版本 3
        # 三个定位角：7x7 外框为深色。
        for x0, y0 in ((0, 0), (22, 0), (0, 22)):
            self.assertTrue(all(grid[y0][x0 + i] and grid[y0 + 6][x0 + i] for i in range(7)))
        # 读回左上角格式信息，必须是纠错等级 M 的合法 BCH 码字。
        bits = 0
        coords = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
        for i, (x, y) in enumerate(coords):
            bits |= int(grid[y][x]) << i
        data = (bits ^ 0x5412) >> 10
        self.assertEqual(0, data >> 3)  # 纠错等级 M
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        self.assertEqual(bits, (data << 10 | rem) ^ 0x5412)
        svg = qrsvg.svg(text)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('viewBox="0 0 37 37"', svg)
        with self.assertRaises(ValueError):
            qrsvg.matrix("x" * 400)
        self.assertEqual("", qrsvg.svg(""))


class SchemaV59MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        _DbCase._reset()
        db.DB_PATH = os.path.join(self.tmp.name, "schema.db")

    def tearDown(self):
        _DbCase._reset()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _raw(self, *statements):
        _DbCase._reset()
        connection = sqlite3.connect(db.DB_PATH)
        try:
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        finally:
            connection.close()

    def test_fresh_database_is_v59_with_pay_order_contract(self):
        db.conn()
        self.assertEqual(59, db.LATEST_SCHEMA_VERSION)
        self.assertEqual(59, db.one("PRAGMA user_version")["user_version"])
        self.assertEqual(
            "wxpay-native-pay-order",
            db.one("SELECT name FROM schema_version WHERE version=59")["name"],
        )
        db.execute(
            "INSERT INTO pay_order(tenant_id,created_by,plan_key,period_key,plan_name,"
            "period_label,quoted_points,amount_fen,out_trade_no,expires_at) "
            "VALUES(2,20,'trial','month','体验版','月付',150,6900,'PH1',1)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO pay_order(tenant_id,created_by,plan_key,period_key,plan_name,"
                "period_label,quoted_points,amount_fen,out_trade_no,expires_at) "
                "VALUES(3,30,'trial','month','体验版','月付',150,6900,'PH1',1)"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO pay_order(tenant_id,created_by,plan_key,period_key,plan_name,"
                "period_label,quoted_points,amount_fen,out_trade_no,expires_at) "
                "VALUES(3,30,'trial','month','体验版','月付',150,0,'PH2',1)"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE pay_order SET status='weird'")

    def test_v58_database_upgrades_to_v59(self):
        db.conn()
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 5})
        self._raw(
            "DROP TABLE pay_order",
            "DELETE FROM schema_version WHERE version=59",
            "PRAGMA user_version=58",
        )
        db.conn()
        self.assertEqual(59, db.one("PRAGMA user_version")["user_version"])
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM schema_version WHERE version=59")["n"])
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM pay_order")["n"])
        self.assertEqual(5, db.one("SELECT balance FROM tenants WHERE id=2")["balance"])
        indexes = {row["name"] for row in db.q("PRAGMA index_list(pay_order)")}
        self.assertTrue({
            "idx_pay_order_out_trade_no", "idx_pay_order_transaction",
            "idx_pay_order_tenant_created", "idx_pay_order_status_expires",
        } <= indexes)

    def test_validator_requires_pay_order_unique_index(self):
        db.conn()
        connection = db.conn()
        connection.execute("DROP INDEX idx_pay_order_out_trade_no")
        connection.execute("CREATE INDEX idx_pay_order_out_trade_no ON pay_order(out_trade_no)")
        with self.assertRaises(RuntimeError):
            db._validate_migrated_database(connection)
        connection.execute("DROP INDEX idx_pay_order_out_trade_no")
        with self.assertRaises(RuntimeError):
            db._validate_migrated_database(connection)
        connection.rollback()


if __name__ == "__main__":
    unittest.main()
