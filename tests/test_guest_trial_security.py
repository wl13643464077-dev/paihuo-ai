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

    async def asyncTearDown(self):
        from app import main

        main._guest_trial_ips.clear()
        main._guest_trial_total[:] = [0, 0]
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


if __name__ == "__main__":
    unittest.main()
