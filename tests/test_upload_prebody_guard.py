import asyncio
import unittest
from unittest import mock

from starlette.requests import Request
from starlette.responses import Response

from app import auth, main


class UploadPrebodyGuardCase(unittest.TestCase):
    def setUp(self):
        auth.set_current(None)
        main._transient_upload_active_tenants.clear()

    def tearDown(self):
        auth.set_current(None)
        main._transient_upload_active_tenants.clear()

    @staticmethod
    def _request(path: str, content_length: int) -> Request:
        async def unread_body():
            raise AssertionError(
                "multipart body must not be consumed before the guard"
            )

        return Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "headers": [
                    (b"cookie", b"cc_sess=test-session"),
                    (
                        b"content-length",
                        str(content_length).encode("ascii"),
                    ),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("paihuo.ai", 443),
            },
            receive=unread_body,
        )

    @staticmethod
    def _user(*, role: str = "owner", modules=None) -> dict:
        return {
            "id": 20,
            "tenant_id": 2,
            "username": "upload-user",
            "role": role,
            "modules": list(modules or []),
            "must_change_password": 0,
        }

    @staticmethod
    async def _middleware(request, call_next, user):
        with mock.patch.object(
            main.auth,
            "parse_session",
            return_value=user["id"],
        ), mock.patch.object(
            main.auth,
            "get_user",
            return_value=user,
        ):
            return await main._auth_mw(request, call_next)

    def test_transient_upload_routes_reject_declared_oversize_before_parse(self):
        cases = (
            ("/api/parse-file", 21 * 1024 * 1024 + 1),
            ("/api/tools/menu-copy", 9 * 1024 * 1024 + 1),
            ("/api/tools/product-shot", 9 * 1024 * 1024 + 1),
        )
        user = self._user(modules=["content"])

        for path, size in cases:
            called = False

            async def call_next(_request):
                nonlocal called
                called = True
                return Response(status_code=204)

            with self.subTest(path=path):
                response = asyncio.run(
                    self._middleware(
                        self._request(path, size),
                        call_next,
                        user,
                    )
                )
                self.assertEqual(413, response.status_code)
                self.assertFalse(called)
                self.assertEqual(
                    set(),
                    main._transient_upload_active_tenants,
                )

    def test_actual_body_limit_rejects_chunked_or_false_length_request(self):
        limit = main._TRANSIENT_UPLOAD_ROUTES[
            ("POST", "/api/tools/menu-copy")
        ][2]
        messages = iter(
            (
                {
                    "type": "http.request",
                    "body": b"x" * (limit + 1),
                    "more_body": False,
                },
            )
        )

        async def receive():
            return next(
                messages,
                {"type": "http.disconnect"},
            )

        request = self._request("/api/tools/menu-copy", 1)
        request._receive = receive
        reached_handler = False

        async def call_next(guarded_request):
            nonlocal reached_handler
            await guarded_request.receive()
            reached_handler = True
            return Response(status_code=204)

        response = asyncio.run(
            self._middleware(
                request,
                call_next,
                self._user(modules=["content"]),
            )
        )
        self.assertEqual(413, response.status_code)
        self.assertFalse(reached_handler)
        self.assertEqual(set(), main._transient_upload_active_tenants)

    def test_false_length_returns_413_through_real_asgi_stack(self):
        boundary = b"paihuo-test-boundary"
        body = (
            b"--"
            + boundary
            + b'\r\nContent-Disposition: form-data; name="file"; '
            b'filename="oversize.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
            + b"x" * 256
            + b"\r\n--"
            + boundary
            + b"--\r\n"
        )
        messages = iter(
            (
                {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                },
            )
        )
        sent = []

        async def receive():
            return next(
                messages,
                {"type": "http.disconnect"},
            )

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/tools/menu-copy",
            "raw_path": b"/api/tools/menu-copy",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"cookie", b"cc_sess=test-session"),
                (b"content-length", b"1"),
                (
                    b"content-type",
                    b"multipart/form-data; boundary=" + boundary,
                ),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("paihuo.ai", 443),
        }
        user = self._user(modules=["content"])

        async def scenario():
            with mock.patch.object(
                main.auth,
                "parse_session",
                return_value=user["id"],
            ), mock.patch.object(
                main.auth,
                "get_user",
                return_value=user,
            ), mock.patch.dict(
                main._TRANSIENT_UPLOAD_ROUTES,
                {
                    ("POST", "/api/tools/menu-copy"): (
                        "menu-copy",
                        "content",
                        128,
                    )
                },
                clear=False,
            ):
                await main.app(scope, receive, send)

        asyncio.run(scenario())
        starts = [
            message
            for message in sent
            if message.get("type") == "http.response.start"
        ]
        self.assertEqual([413], [message["status"] for message in starts])
        self.assertEqual(set(), main._transient_upload_active_tenants)

    def test_parse_file_permission_is_rejected_before_parse(self):
        called = False

        async def call_next(_request):
            nonlocal called
            called = True
            return Response(status_code=204)

        response = asyncio.run(
            self._middleware(
                self._request("/api/parse-file", 1024),
                call_next,
                self._user(role="member", modules=[]),
            )
        )
        self.assertEqual(403, response.status_code)
        self.assertFalse(called)
        self.assertEqual(set(), main._transient_upload_active_tenants)

    def test_transient_upload_slot_wraps_multipart_parsing(self):
        observed = {}
        user = self._user(modules=["content"])

        async def call_next(_request):
            observed["reserved"] = main._TRANSIENT_UPLOAD_RESERVED.get()
            observed["active"] = 2 in main._transient_upload_active_tenants
            return Response(status_code=204)

        response = asyncio.run(
            self._middleware(
                self._request("/api/tools/menu-copy", 1024),
                call_next,
                user,
            )
        )
        self.assertEqual(204, response.status_code)
        self.assertEqual(
            {"reserved": True, "active": True},
            observed,
        )
        self.assertEqual(set(), main._transient_upload_active_tenants)

    def test_same_tenant_concurrency_is_rejected_before_second_parse(self):
        user = self._user(modules=["content"])
        second_called = False

        async def second_next(_request):
            nonlocal second_called
            second_called = True
            return Response(status_code=204)

        async def first_next(_request):
            second = await self._middleware(
                self._request("/api/tools/product-shot", 1024),
                second_next,
                user,
            )
            self.assertEqual(429, second.status_code)
            return Response(status_code=204)

        response = asyncio.run(
            self._middleware(
                self._request("/api/tools/menu-copy", 1024),
                first_next,
                user,
            )
        )
        self.assertEqual(204, response.status_code)
        self.assertFalse(second_called)
        self.assertEqual(set(), main._transient_upload_active_tenants)


if __name__ == "__main__":
    unittest.main()
