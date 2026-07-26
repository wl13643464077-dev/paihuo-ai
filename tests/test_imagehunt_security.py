import io
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from app import imagehunt


class ImageFetchSecurityCase(unittest.IsolatedAsyncioTestCase):
    async def test_each_redirect_target_is_guarded_before_fetch(self):
        requested = []

        def handler(request):
            requested.append(str(request.url))
            if request.url.host == "public.example":
                return httpx.Response(
                    302,
                    headers={"location": "http://127.0.0.1/latest/meta-data"},
                )
            raise AssertionError("内网重定向目标不应发出 HTTP 请求")

        async def guard(url):
            if "127.0.0.1" in url:
                raise ValueError("这个地址指向内网/本机,已拒绝访问")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            with patch("app.imagehunt._guard_url", new=AsyncMock(side_effect=guard)) as guarded:
                with self.assertRaisesRegex(ValueError, "内网"):
                    await imagehunt.fetch_public_bytes(
                        "https://public.example/start", client=client)

        self.assertEqual(["https://public.example/start"], requested)
        self.assertEqual(
            ["https://public.example/start", "http://127.0.0.1/latest/meta-data"],
            [call.args[0] for call in guarded.await_args_list],
        )

    async def test_response_is_streamed_with_a_hard_byte_limit(self):
        def handler(_request):
            return httpx.Response(200, content=b"x" * 33)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            with patch("app.imagehunt._guard_url", new=AsyncMock(return_value=None)):
                with self.assertRaisesRegex(ValueError, "大小限制"):
                    await imagehunt.fetch_public_bytes(
                        "https://images.example/a.jpg",
                        max_bytes=32,
                        client=client,
                    )

    async def test_safe_public_redirect_returns_body_and_headers(self):
        png = b"\x89PNG\r\n\x1a\nfixture"

        def handler(request):
            if request.url.path == "/start":
                return httpx.Response(301, headers={"location": "/final"})
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            with patch("app.imagehunt._guard_url", new=AsyncMock(return_value=None)) as guarded:
                body, headers = await imagehunt.fetch_public_bytes(
                    "https://images.example/start",
                    max_bytes=1024,
                    client=client,
                )

        self.assertEqual(png, body)
        self.assertEqual("image/png", headers["content-type"])
        self.assertEqual(2, guarded.await_count)

    async def test_connection_is_pinned_to_guarded_ip_with_original_host_and_sni(self):
        observed = {}

        def handler(request):
            observed["url"] = str(request.url)
            observed["host"] = request.headers["host"]
            observed["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, content=b"ok")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            with patch(
                "app.imagehunt._guard_url",
                new=AsyncMock(return_value=("93.184.216.34",)),
            ):
                body, _ = await imagehunt.fetch_public_bytes(
                    "https://images.example/a.jpg", client=client
                )

        self.assertEqual(b"ok", body)
        self.assertEqual("https://93.184.216.34/a.jpg", observed["url"])
        self.assertEqual("images.example", observed["host"])
        self.assertEqual("images.example", observed["sni"])


class ImageDecodeSecurityCase(unittest.TestCase):
    @staticmethod
    def _png(width=32, height=24):
        output = io.BytesIO()
        Image.new("RGB", (width, height), "red").save(output, "PNG")
        return output.getvalue()

    def test_thumbnail_is_decoded_and_reencoded_with_fixed_safe_type(self):
        body, media_type = imagehunt.safe_thumbnail_bytes(self._png())
        self.assertEqual("image/jpeg", media_type)
        self.assertTrue(body.startswith(b"\xff\xd8\xff"))

    def test_pixel_limit_is_checked_before_full_decode(self):
        with self.assertRaisesRegex(ValueError, "安全限制"):
            imagehunt._decode_image(self._png(100, 100), max_pixels=1_000)

    def test_magic_prefix_plus_html_is_not_accepted_as_an_image(self):
        with self.assertRaises(Exception):
            imagehunt.safe_thumbnail_bytes(
                b"GIF89a<html><script>alert(1)</script></html>"
            )


if __name__ == "__main__":
    unittest.main()
