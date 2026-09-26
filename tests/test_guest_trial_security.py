import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import httpx
from starlette.requests import Request

from app import db


def _request(peer: str = "198.51.100.80", cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/guest/try",
            "raw_path": b"/api/guest/try",
            "query_string": b"",
            "headers": headers,
            "client": (peer, 12345),
            "server": ("paihuo.test", 443),
        }
    )


class GuestTrialSecurityCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "guest.db")
        db.conn()
        from app import main

        main._guest_trial_ips.clear()
        main._guest_trial_total[:] = [0, 0]
        main._guest_register_ip_counter.clear()
        main._apply_ip_counter.clear()
        main._auto_open_quota.reset()

    async def asyncTearDown(self):
        from app import main

        main._guest_trial_ips.clear()
        main._guest_trial_total[:] = [0, 0]
        main._guest_register_ip_counter.clear()
        main._apply_ip_counter.clear()
        main._auto_open_quota.reset()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def test_registration_has_trusted_ip_and_global_daily_caps(self):
        from app import main

        request = _request()
        with patch.object(main, "_GUEST_TRIAL_IP_DAILY", 1), patch.object(
            main, "_GUEST_TRIAL_GLOBAL_DAILY", 10
        ):
            first = await main.guest_register(
                {"phone": "13800000001", "name": "甲"}, request
            )
            self.assertEqual(200, first.status_code)
            with self.assertRaises(HTTPException) as caught:
                await main.guest_register(
                    {"phone": "13800000002", "name": "乙"}, request
                )
        self.assertEqual(429, caught.exception.status_code)

    async def test_guest_cookies_are_http_only_lax_and_secure_on_https(self):
        from app import main

        tour = await main.guest_tour(_request())
        tour_cookie = tour.headers.get("set-cookie") or ""
        for flag in ("HttpOnly", "SameSite=lax", "Secure"):
            self.assertIn(flag, tour_cookie)

        registered = await main.guest_register(
            {"phone": "13800000009", "name": "安全测试"},
            _request(),
        )
        registered_cookie = registered.headers.get("set-cookie") or ""
        for flag in ("HttpOnly", "SameSite=lax", "Secure"):
            self.assertIn(flag, registered_cookie)

    async def test_concurrent_try_claims_guest_once_before_model_call(self):
        from app import main

        gid = db.insert(
            "guests", {"phone": "13800000003", "company": "", "name": "测试"}
        )
        cookie = f"cc_guest={gid}.{main._guest_sign(gid)}"
        request = _request(cookie=cookie)
        provider = AsyncMock(return_value={"text": "回答"})
        with patch("app.providers.call_text", provider):
            results = await asyncio.gather(
                main.guest_try(request, {"question": "怎么提升转化？"}),
                main.guest_try(request, {"question": "怎么提升转化？"}),
                return_exceptions=True,
            )
        self.assertEqual(1, provider.await_count)
        self.assertIsNone(provider.await_args.args[0])
        self.assertEqual(1, sum(isinstance(item, dict) for item in results))
        denied = [item for item in results if isinstance(item, HTTPException)]
        self.assertEqual(1, len(denied))
        self.assertEqual(403, denied[0].status_code)

    async def test_provider_failure_releases_claim_for_a_real_retry(self):
        from app import main

        gid = db.insert(
            "guests", {"phone": "13800000004", "company": "", "name": "测试"}
        )
        request = _request(cookie=f"cc_guest={gid}.{main._guest_sign(gid)}")
        with patch(
            "app.providers.call_text",
            AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            with self.assertRaises(RuntimeError):
                await main.guest_try(request, {"question": "测试问题"})
        self.assertEqual(0, db.one("SELECT used FROM guests WHERE id=?", (gid,))["used"])

    async def test_tour_boundary_promises_public_intro_not_internal_capabilities(self):
        from app import main

        guest_id = 99
        cookie = f"{guest_id}.{main._guest_sign(guest_id)}"
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://paihuo.test",
            cookies={"cc_guest": cookie},
        ) as client:
            response = await client.get("/api/employees/0")

        self.assertEqual(403, response.status_code)
        detail = response.json()["detail"]
        self.assertIn("浏览员工介绍并派活", detail)
        self.assertNotIn("详细能力", detail)

    async def test_repeat_registration_mails_only_once_and_truncates_fields(self):
        from app import main

        notify = AsyncMock()
        with patch("app.mailer.notify_lead", notify):
            for peer in ("198.51.100.81", "198.51.100.82", "198.51.100.83"):
                response = await main.guest_register(
                    {"phone": "13800000010", "name": "王" * 500,
                     "company": "店" * 500},
                    _request(peer),
                )
                self.assertEqual(200, response.status_code)
        self.assertEqual(1, notify.call_count)
        _, mailed_name, mailed_company = notify.call_args.args
        self.assertEqual(30, len(mailed_name))
        self.assertEqual(60, len(mailed_company))
        row = db.one("SELECT name,company FROM guests WHERE phone='13800000010'")
        self.assertEqual(30, len(row["name"]))
        self.assertEqual(60, len(row["company"]))

    async def test_register_lookup_is_rate_limited_per_ip_before_phone_lookup(self):
        from app import main
        from app import signup

        db.insert("guests", {"phone": "13800000011", "company": "", "name": "",
                             "used": 1})
        with patch.object(main, "_guest_register_ip_counter",
                          signup.DailyCounter(1)):
            with self.assertRaises(HTTPException) as first:
                await main.guest_register({"phone": "13800000011"}, _request())
            self.assertEqual(403, first.exception.status_code)
            with self.assertRaises(HTTPException) as second:
                await main.guest_register({"phone": "13800000011"}, _request())
        self.assertEqual(429, second.exception.status_code)

    async def test_apply_ip_limit_runs_before_existing_account_check(self):
        from app import main
        from app import signup

        tid = db.insert("tenants", {"name": "已有客户"})
        db.insert("users", {"tenant_id": tid, "username": "13900000001",
                            "password_hash": "x", "role": "owner",
                            "modules_json": "[]", "enabled": 1})
        with patch("app.mailer.notify_apply", AsyncMock()), patch.object(
                main, "_apply_ip_counter", signup.DailyCounter(1)):
            existing = await main.guest_apply(
                {"phone": "13900000001"}, _request("198.51.100.90"))
            fresh = await main.guest_apply(
                {"phone": "13900000002"}, _request("198.51.100.91"))
            limited = await main.guest_apply(
                {"phone": "13900000001"}, _request("198.51.100.90"))
        # 已有账号与新申请回同一句话,不能用来探测手机号
        self.assertEqual(main._APPLY_RECEIVED_MSG, existing["msg"])
        self.assertEqual(existing["msg"], fresh["msg"])
        self.assertIn("上限", limited["msg"])

    async def test_auto_open_daily_cap_is_reserved_atomically(self):
        from app import main

        if not db.one("SELECT id FROM tenants WHERE id=1"):
            db.insert("tenants", {"name": "平台总部"})
        db.set_setting("auto_approve_apply", "1")
        db.set_setting("auto_approve_daily_cap", "1")
        db.set_setting("trial_points", "0")
        with patch("app.mailer.notify_apply", AsyncMock()):
            results = await asyncio.gather(*[
                main.guest_apply(
                    {"phone": f"1370000000{i}", "industry": "tea_coffee"},
                    _request(f"198.51.100.{100 + i}"),
                )
                for i in range(4)
            ])
        opened = [r for r in results if r.get("account")]
        self.assertEqual(1, len(opened))
        self.assertEqual(1, main._auto_open_quota.used())
        tenant_id = db.one(
            "SELECT tenant_id FROM account_apply WHERE username=?",
            (opened[0]["account"]["username"],),
        )["tenant_id"]
        bound = db.one(
            "SELECT industry_key FROM tenant_industry WHERE tenant_id=?",
            (tenant_id,),
        )
        self.assertEqual("tea_coffee", bound["industry_key"])

    async def test_failed_auto_open_releases_reserved_slot(self):
        from app import main

        db.set_setting("auto_approve_apply", "1")
        db.set_setting("auto_approve_daily_cap", "1")
        with patch("app.mailer.notify_apply", AsyncMock()), patch.object(
                main, "_open_account_from_apply",
                side_effect=RuntimeError("boom")):
            result = await main.guest_apply(
                {"phone": "13600000001"}, _request("198.51.100.120"))
        self.assertNotIn("account", result)
        self.assertEqual(0, main._auto_open_quota.used())


if __name__ == "__main__":
    unittest.main()
