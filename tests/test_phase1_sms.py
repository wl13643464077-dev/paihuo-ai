"""第 1 期：短信验证码登录的行为测试（签名向量、限流、验证码、账号查找、配置）。"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from urllib.parse import parse_qsl, urlsplit

from app import auth, db, secureconfig, smslogin, timeutil


class SignatureCase(unittest.TestCase):
    """阿里云 RPC 签名：用阿里云官方文档里的两组示例向量逐字节核对。"""

    def test_official_describe_regions_vector(self):
        params = {
            "AccessKeyId": "testid", "Action": "DescribeRegions", "Format": "XML",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": "3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf",
            "SignatureVersion": "1.0", "Timestamp": "2016-02-23T12:46:24Z",
            "Version": "2014-05-26",
        }
        self.assertEqual(
            "GET&%2F&AccessKeyId%3Dtestid%26Action%3DDescribeRegions%26Format%3DXML"
            "%26SignatureMethod%3DHMAC-SHA1%26SignatureNonce%3D3ee8c1b8-83d3-44af-a94f-"
            "4e0ad82fd6cf%26SignatureVersion%3D1.0%26Timestamp%3D2016-02-23T12%253A46"
            "%253A24Z%26Version%3D2014-05-26",
            smslogin.string_to_sign(params),
        )
        self.assertEqual("OLeaidS1JvxuMvnyHOwuJ+uX5qY=",
                         smslogin.rpc_signature(params, "testsecret"))

    def test_official_send_sms_vector_with_chinese_sign_name(self):
        params = {
            "AccessKeyId": "testId", "Action": "SendSms", "Format": "XML",
            "OutId": "123", "PhoneNumbers": "15300000001", "RegionId": "cn-hangzhou",
            "SignName": "阿里云短信测试专用", "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": "45e25e9b-0a6f-4070-8c85-2956eda1b466",
            "SignatureVersion": "1.0", "TemplateCode": "SMS_71390007",
            "TemplateParam": '{"customer":"test"}',
            "Timestamp": "2017-07-12T02:42:19Z", "Version": "2017-05-25",
        }
        self.assertEqual("zJDF+Lrzhj/ThnlvIToysFRq6t4=",
                         smslogin.rpc_signature(params, "testSecret"))

    def test_percent_encode_follows_pop_rules(self):
        self.assertEqual("a%20b", smslogin.percent_encode("a b"))
        self.assertEqual("%2A", smslogin.percent_encode("*"))
        self.assertEqual("~-_.", smslogin.percent_encode("~-_."))
        self.assertEqual("%2F%3D%26%2B", smslogin.percent_encode("/=&+"))
        self.assertEqual("%E6%B4%BE", smslogin.percent_encode("派"))

    def test_signed_url_is_self_consistent(self):
        conf = {"key_id": "LTAI-test", "key_secret": "s3cret",
                "sign_name": "派活", "template_code": "SMS_1"}
        url = smslogin.signed_url(conf, "13800000000", "123456",
                                  nonce="n-1", timestamp="2026-09-25T00:00:00Z")
        parts = urlsplit(url)
        self.assertEqual("https://dysmsapi.aliyuncs.com/",
                         f"{parts.scheme}://{parts.netloc}{parts.path}")
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        signature = query.pop("Signature")
        self.assertEqual("SendSms", query["Action"])
        self.assertEqual("13800000000", query["PhoneNumbers"])
        self.assertEqual({"code": "123456"}, json.loads(query["TemplateParam"]))
        self.assertEqual("派活", query["SignName"])
        # 独立按规范重算一遍签名
        sts = "GET&%2F&" + smslogin.percent_encode("&".join(
            f"{smslogin.percent_encode(k)}={smslogin.percent_encode(query[k])}"
            for k in sorted(query)))
        expected = base64.b64encode(hmac.new(
            b"s3cret&", sts.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(expected, signature)


class _FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        return self.response


class SendCase(unittest.TestCase):
    conf = {"key_id": "id", "key_secret": "sec", "sign_name": "派活", "template_code": "SMS_1"}

    def test_ok_response_passes(self):
        client = _FakeClient(_FakeResponse(200, {"Code": "OK", "Message": "OK"}))
        asyncio.run(smslogin.send_code_sms(self.conf, "13800000000", "654321", client=client))
        self.assertEqual(1, len(client.urls))
        self.assertIn("PhoneNumbers=13800000000", client.urls[0])

    def test_provider_rejection_raises(self):
        for response in (_FakeResponse(200, {"Code": "isv.BUSINESS_LIMIT_CONTROL"}),
                         _FakeResponse(500, {})):
            with self.subTest(status=response.status_code):
                with self.assertRaises(smslogin.SmsSendError):
                    asyncio.run(smslogin.send_code_sms(
                        self.conf, "13800000000", "1", client=_FakeClient(response)))


class CodeStoreCase(unittest.TestCase):
    T0 = 1_790_000_000.0

    def test_code_is_six_digits_and_single_use(self):
        store = smslogin.CodeStore()
        code = store.issue("13800000000", "1.1.1.1", now=self.T0)
        self.assertRegex(code, r"^\d{6}$")
        self.assertTrue(store.verify("13800000000", code, now=self.T0 + 10))
        self.assertFalse(store.verify("13800000000", code, now=self.T0 + 11))

    def test_code_expires_after_five_minutes(self):
        store = smslogin.CodeStore()
        code = store.issue("13800000000", "1.1.1.1", now=self.T0)
        self.assertFalse(store.verify("13800000000", code, now=self.T0 + 301))

    def test_wrong_code_attempts_invalidate(self):
        store = smslogin.CodeStore()
        code = store.issue("13800000000", "1.1.1.1", now=self.T0)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(smslogin.MAX_VERIFY_FAILS):
            self.assertFalse(store.verify("13800000000", wrong, now=self.T0 + 1))
        self.assertFalse(store.verify("13800000000", code, now=self.T0 + 2))

    def test_verify_rejects_other_phone_and_garbage(self):
        store = smslogin.CodeStore()
        code = store.issue("13800000000", "1.1.1.1", now=self.T0)
        self.assertFalse(store.verify("13900000000", code, now=self.T0 + 1))
        self.assertFalse(store.verify("13800000000", None, now=self.T0 + 1))
        self.assertFalse(store.verify("13800000000", code + "0", now=self.T0 + 1))
        self.assertTrue(store.verify("13800000000", code, now=self.T0 + 1))

    def test_phone_cooldown_60s(self):
        store = smslogin.CodeStore()
        store.issue("13800000000", "1.1.1.1", now=self.T0)
        with self.assertRaises(smslogin.SmsLimitError) as ctx:
            store.issue("13800000000", "2.2.2.2", now=self.T0 + 30)
        self.assertLessEqual(ctx.exception.retry_after, 31)
        store.issue("13800000000", "2.2.2.2", now=self.T0 + 61)

    def test_new_code_replaces_old_one(self):
        store = smslogin.CodeStore()
        first = store.issue("13800000000", "1.1.1.1", now=self.T0)
        second = store.issue("13800000000", "1.1.1.1", now=self.T0 + 61)
        if first != second:
            self.assertFalse(store.verify("13800000000", first, now=self.T0 + 62))
        self.assertTrue(store.verify("13800000000", second, now=self.T0 + 63))

    def test_phone_daily_limit_resets_on_beijing_midnight(self):
        store = smslogin.CodeStore(ip_hourly=1000, ip_daily=1000)
        day_start = timeutil.cn_day_start_ts(self.T0)
        t = day_start + 60
        for i in range(smslogin.PHONE_DAILY_MAX):
            store.issue("13800000000", f"10.0.0.{i}", now=t + i * 61)
        with self.assertRaises(smslogin.SmsLimitError):
            store.issue("13800000000", "10.0.1.1", now=t + 20 * 61)
        # 北京时间第二天 00:00 之后重新计数
        store.issue("13800000000", "10.0.1.1", now=day_start + 86400 + 1)

    def test_ip_rate_limit(self):
        store = smslogin.CodeStore()
        for i in range(smslogin.IP_HOURLY_MAX):
            store.issue(f"1380000{i:04d}", "9.9.9.9", now=self.T0 + i)
        with self.assertRaises(smslogin.SmsLimitError):
            store.issue("13900000000", "9.9.9.9", now=self.T0 + 30)
        # 别的 IP 不受影响；一小时后同 IP 恢复
        store.issue("13900000000", "8.8.8.8", now=self.T0 + 30)
        store.issue("13900000001", "9.9.9.9", now=self.T0 + 3601)

    def test_ip_daily_limit(self):
        store = smslogin.CodeStore(ip_hourly=1000)
        day_start = timeutil.cn_day_start_ts(self.T0)
        for i in range(smslogin.IP_DAILY_MAX):
            store.issue(f"1370000{i:04d}", "7.7.7.7", now=day_start + 10 + i)
        with self.assertRaises(smslogin.SmsLimitError):
            store.issue("13900000000", "7.7.7.7", now=day_start + 5000)

    def test_memory_is_bounded(self):
        store = smslogin.CodeStore(max_keys=50, ip_hourly=10**6, ip_daily=10**6)
        for i in range(200):
            store.issue(f"1360000{i:04d}", f"ip{i}", now=self.T0 + i)
        self.assertLessEqual(len(store._codes), 51)
        self.assertLessEqual(len(store._phones), 51)
        self.assertLessEqual(len(store._ips), 51)

    def test_phone_normalization(self):
        self.assertEqual("13800000000", smslogin.normalize_phone(" 138-0000-0000 "))
        self.assertEqual("13800000000", smslogin.normalize_phone("+8613800000000"))
        for bad in ("", None, "2380000000", "1380000000", "138000000001", "abc"):
            with self.subTest(bad=bad):
                self.assertEqual("", smslogin.normalize_phone(bad))


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "sms.db")
        db.conn()
        if not db.one("SELECT id FROM tenants WHERE id=1"):
            db.insert("tenants", {"name": "平台总部"})
        self.store = smslogin.CodeStore()

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _owner(self, username, *, tenant_enabled=1, enabled=1, role="owner"):
        tid = db.insert("tenants", {"name": "店", "enabled": tenant_enabled})
        return db.insert("users", {
            "tenant_id": tid, "username": username,
            "password_hash": auth.hash_pw("Some-Pass-2026"), "role": role,
            "modules_json": "[]", "enabled": enabled, "must_change_password": 0,
        })


class AccountLookupCase(_DbCase):
    def test_username_phone_and_bound_phone_resolve(self):
        uid = self._owner("13800000000")
        self.assertEqual(uid, smslogin.resolve_login_user("13800000000")["id"])
        other = self._owner("wangji-boss")
        db.insert("account_apply", {"phone": "13911112222", "status": 1,
                                    "username": "wangji-boss"})
        self.assertEqual(other, smslogin.resolve_login_user("13911112222")["id"])

    def test_disabled_root_and_unknown_do_not_resolve(self):
        self._owner("13800000001", enabled=0)
        self._owner("13800000002", tenant_enabled=0)
        self._owner("13800000003", role="root")
        db.insert("account_apply", {"phone": "13800000004", "status": 0})
        for phone in ("13800000001", "13800000002", "13800000003",
                      "13800000004", "13800000009", "bad"):
            with self.subTest(phone=phone):
                self.assertIsNone(smslogin.resolve_login_user(phone))

    def test_unknown_phone_consumes_limits_but_gets_no_code(self):
        code, user = smslogin.request_code("13700000000", "1.1.1.1", store=self.store)
        self.assertIsNone(code)
        self.assertIsNone(user)
        # 和真实账号一样占 60 秒冷却：不能用「冷却与否」探测手机号是否注册
        with self.assertRaises(smslogin.SmsLimitError):
            smslogin.request_code("13700000000", "1.1.1.1", store=self.store)
        self.assertIsNone(smslogin.verify_login("13700000000", "000000", store=self.store))

    def test_full_login_flow(self):
        uid = self._owner("13800000000")
        code, user = smslogin.request_code("13800000000", "1.1.1.1", store=self.store)
        self.assertEqual(uid, user["id"])
        self.assertIsNone(smslogin.verify_login("13800000000", "x", store=self.store))
        self.assertEqual(uid, smslogin.verify_login("13800000000", code, store=self.store)["id"])
        # 一次性：再用同一个码失败
        self.assertIsNone(smslogin.verify_login("13800000000", code, store=self.store))

    def test_bad_phone_format_rejected(self):
        with self.assertRaises(ValueError):
            smslogin.request_code("12345", "1.1.1.1", store=self.store)


class ConfigCase(_DbCase):
    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.pop(k, None) for k in (
            "CONTENTCREW_CONFIG_KEY", "CONTENTCREW_REQUIRE_CONFIG_KEY")}

    def tearDown(self):
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
        super().tearDown()

    def test_default_off_and_requires_all_fields(self):
        self.assertFalse(smslogin.is_enabled())
        smslogin.save_config({"enabled": True})
        self.assertFalse(smslogin.is_enabled())   # 开关开了但没配齐
        smslogin.save_config({"key_id": "LTAI1", "key_secret": "sec",
                              "sign_name": "派活", "template_code": "SMS_1"})
        self.assertTrue(smslogin.is_enabled())
        smslogin.save_config({"enabled": False})
        self.assertFalse(smslogin.is_enabled())

    def test_blank_keys_keep_existing_and_public_view_hides_secrets(self):
        smslogin.save_config({"key_id": "LTAI1", "key_secret": "very-secret",
                              "sign_name": "派活", "template_code": "SMS_1",
                              "enabled": True})
        smslogin.save_config({"key_id": "", "key_secret": "", "sign_name": "派活2"})
        conf = smslogin.get_config()
        self.assertEqual("very-secret", conf["key_secret"])
        self.assertEqual("派活2", conf["sign_name"])
        public = smslogin.public_config()
        self.assertNotIn("very-secret", json.dumps(public, ensure_ascii=False))
        self.assertTrue(public["key_secret_set"])
        self.assertTrue(public["active"])
        smslogin.save_config({"clear_keys": True})
        self.assertFalse(smslogin.public_config()["key_id_set"])

    def test_keys_are_encrypted_when_wrapping_key_present(self):
        import secrets
        from app.session_secret import CONFIG_KEY_ENV
        # 与部署工具同格式：48 字节随机数的 URL-safe base64
        os.environ[CONFIG_KEY_ENV] = secrets.token_urlsafe(48)
        try:
            self.assertTrue(secureconfig.validate_runtime_key())
            smslogin.save_config({"key_id": "LTAI-plain", "key_secret": "plain-secret"})
            raw = db.get_setting(smslogin.KEY_SECRET_SETTING)
            self.assertTrue(raw.startswith(secureconfig.ENCRYPTED_PREFIX))
            self.assertNotIn("plain-secret", raw)
            self.assertEqual("plain-secret", smslogin.get_config()["key_secret"])
        finally:
            os.environ.pop(CONFIG_KEY_ENV, None)

    def test_sms_keys_registered_in_secure_store(self):
        for name in (smslogin.KEY_ID_SETTING, smslogin.KEY_SECRET_SETTING):
            self.assertIn(name, secureconfig.SECRET_SETTING_KEYS)


class LoginPageBrowserCase(unittest.IsolatedAsyncioTestCase):
    """登录页：没配置时看不到验证码入口；配置后可切换并走完发码/登录请求。"""

    @classmethod
    def setUpClass(cls):
        import threading
        from functools import partial
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                return

        root = Path(__file__).resolve().parents[1]
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         partial(Quiet, directory=str(root)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    async def _open(self, playwright, enabled, calls):
        from urllib.parse import urlparse
        executable = next((p for p in (
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "",
            "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        ) if p and os.path.isfile(p)), "")
        options = {"headless": True}
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        page = await browser.new_page(viewport={"width": 375, "height": 800})

        async def api_route(route):
            request = route.request
            path = urlparse(request.url).path
            calls.append((request.method, path,
                          request.post_data_json if request.post_data else None))
            status, payload = 200, {}
            if path == "/api/auth/login/sms/config":
                payload = {"enabled": enabled}
            elif path == "/api/auth/login/sms/send":
                payload = {"ok": True, "msg": smslogin.SENT_MSG, "cooldown": 60}
            elif path == "/api/auth/login/sms/verify":
                status, payload = 401, {"detail": smslogin.VERIFY_FAIL_MSG}
            await route.fulfill(status=status, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        await page.route("**/api/**", api_route)
        await page.goto(f"{self.base}/static/login.html")
        return browser, page

    async def test_hidden_when_not_configured(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("playwright 不可用")
        calls = []
        async with async_playwright() as playwright:
            browser, page = await self._open(playwright, False, calls)
            await page.wait_for_timeout(300)
            self.assertFalse(await page.locator("#login-tabs").is_visible())
            self.assertFalse(await page.locator("#sms-pane").is_visible())
            self.assertTrue(await page.locator("#login-btn").is_visible())
            await browser.close()

    async def test_switch_send_and_verify(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("playwright 不可用")
        calls = []
        async with async_playwright() as playwright:
            browser, page = await self._open(playwright, True, calls)
            await page.wait_for_selector("#login-tabs", state="visible")
            await page.click("#tab-sms")
            self.assertTrue(await page.locator("#sms-pane").is_visible())
            self.assertFalse(await page.locator("#pw-pane").is_visible())
            await page.fill("#sms-phone", "1380000")
            await page.click("#sms-send")
            self.assertIn("11 位手机号", await page.locator("#err").inner_text())
            await page.fill("#sms-phone", "13800000000")
            await page.click("#sms-send")
            await page.wait_for_function(
                "document.querySelector('#sms-send').textContent.includes('秒后重发')")
            self.assertTrue(await page.locator("#sms-send").is_disabled())
            self.assertIn(("POST", "/api/auth/login/sms/send", {"phone": "13800000000"}), calls)
            await page.fill("#sms-code", "123456")
            await page.click("#sms-login-btn")
            await page.wait_for_function(
                "document.querySelector('#err').textContent.includes('验证码不对')")
            self.assertIn(("POST", "/api/auth/login/sms/verify",
                           {"phone": "13800000000", "code": "123456"}), calls)
            # 切回密码登录，原来的登录按钮照常可用
            await page.click("#tab-pw")
            self.assertTrue(await page.locator("#login-btn").is_visible())
            await browser.close()


if __name__ == "__main__":
    unittest.main()
