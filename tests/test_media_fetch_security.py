"""供应商返回的媒体 URL 也必须经过 SSRF、大小和格式边界。"""
import asyncio
import base64
import socket
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import avatar, growth, netfetch, providers


class _ImageResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _ProviderClient:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        return _ImageResponse(self.payload)


class MediaFetchSecurityCase(unittest.TestCase):
    def test_literal_loopback_is_rejected_before_any_request(self):
        with self.assertRaisesRegex(ValueError, "本机或内网"):
            asyncio.run(
                netfetch.guard_public_url(
                    "http://127.0.0.1/api/settings"
                )
            )

    def test_server_own_public_interface_is_rejected(self):
        resolved = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("203.0.113.10", 0),
            )
        ]
        with (
            patch.object(netfetch.socket, "getaddrinfo", return_value=resolved),
            patch.object(
                netfetch,
                "_local_interface_addresses",
                return_value=frozenset({"203.0.113.10"}),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "本机或内网"):
                asyncio.run(
                    netfetch.guard_public_url(
                        "https://media.example/private"
                    )
                )

    def test_nonstandard_ports_are_rejected_before_dns(self):
        with patch.object(
            netfetch,
            "_resolve_public_host",
            new=AsyncMock(return_value=("93.184.216.34",)),
        ) as resolver:
            for url in (
                "http://media.example:8080/file",
                "https://media.example:8443/file",
            ):
                with self.subTest(url=url):
                    with self.assertRaisesRegex(ValueError, "标准"):
                        asyncio.run(netfetch.guard_public_url(url))
            resolver.assert_not_awaited()

    def test_dns_resolution_has_a_hard_deadline(self):
        def slow_resolution(_host):
            time.sleep(0.08)
            return ("93.184.216.34",)

        started = time.monotonic()
        with (
            patch.object(
                netfetch,
                "_public_host_addresses",
                side_effect=slow_resolution,
            ),
            patch.object(netfetch, "DNS_TIMEOUT_SECONDS", 0.01),
        ):
            with self.assertRaisesRegex(ValueError, "解析超时"):
                asyncio.run(
                    netfetch.guard_public_url(
                        "https://media.example/file"
                    )
                )
        self.assertLess(time.monotonic() - started, 0.06)
        # Let the bounded executor release its slot before the next test.
        time.sleep(0.09)

    def test_fetch_and_download_use_one_total_deadline(self):
        async def slow_open(*_args, **_kwargs):
            await asyncio.sleep(0.08)
            raise AssertionError("total timeout should cancel this coroutine")

        with patch.object(
            netfetch,
            "_open_public_response",
            new=AsyncMock(side_effect=slow_open),
        ):
            with self.assertRaisesRegex(ValueError, "总超时"):
                asyncio.run(
                    netfetch.fetch_public_media(
                        "https://media.example/file",
                        kind="image",
                        max_bytes=1024,
                        timeout=0.01,
                    )
                )
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, "总超时"):
                    asyncio.run(
                        netfetch.download_public_media(
                            "https://media.example/file",
                            str(Path(directory) / "file.png"),
                            kind="image",
                            max_bytes=1024,
                            timeout=0.01,
                        )
                    )

    def test_each_redirect_hop_is_guarded(self):
        def handler(request):
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/private"},
                request=request,
            )

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                follow_redirects=False,
            ) as client:
                with patch(
                    "app.netfetch.guard_public_url",
                    new=AsyncMock(
                        side_effect=[
                            (), ValueError("远程媒体地址指向本机或内网")
                        ]
                    ),
                ):
                    return await netfetch.fetch_public_media(
                        "https://media.example/start",
                        kind="image",
                        max_bytes=1024,
                        client=client,
                    )

        with self.assertRaisesRegex(ValueError, "本机或内网"):
            asyncio.run(run())

    def test_declared_and_streamed_size_limits_are_hard(self):
        async def fetch(response):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(request)),
                follow_redirects=False,
            ) as client:
                with patch(
                    "app.netfetch.guard_public_url",
                    new=AsyncMock(return_value=()),
                ):
                    return await netfetch.fetch_public_media(
                        "https://media.example/file",
                        kind="audio",
                        max_bytes=8,
                        client=client,
                    )

        def declared(request):
            return httpx.Response(
                200,
                headers={
                    "content-type": "audio/mpeg",
                    "content-length": "99",
                },
                content=b"ID3",
                request=request,
            )

        def streamed(request):
            return httpx.Response(
                200,
                headers={"content-type": "audio/mpeg"},
                content=b"ID3" + b"x" * 20,
                request=request,
            )

        for response in (declared, streamed):
            with self.subTest(response=response.__name__):
                with self.assertRaisesRegex(ValueError, "大小限制"):
                    asyncio.run(fetch(response))

    def test_content_type_and_magic_must_both_match(self):
        async def run(content_type, body, kind):
            def handler(request):
                return httpx.Response(
                    200,
                    headers={"content-type": content_type},
                    content=body,
                    request=request,
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                follow_redirects=False,
            ) as client:
                with patch(
                    "app.netfetch.guard_public_url",
                    new=AsyncMock(return_value=()),
                ):
                    return await netfetch.fetch_public_media(
                        "https://media.example/file",
                        kind=kind,
                        max_bytes=1024,
                        client=client,
                    )

        with self.assertRaisesRegex(ValueError, "媒体类型"):
            asyncio.run(run("text/html", b"ID3audio", "audio"))
        with self.assertRaisesRegex(ValueError, "有效image"):
            asyncio.run(run("image/png", b"<html>not image</html>", "image"))
        self.assertEqual(
            b"ID3audio",
            asyncio.run(run("audio/mpeg", b"ID3audio", "audio")),
        )

    def test_provider_image_rejects_loopback_url_from_compatible_api(self):
        payload = {
            "data": [{
                "url": "http://127.0.0.1:8899/api/settings",
            }]
        }
        with (
            patch.object(providers, "yunwu_conf", return_value=(
                "https://gateway.example", "secret"
            )),
            patch.object(
                providers.httpx,
                "AsyncClient",
                return_value=_ProviderClient(payload),
            ),
        ):
            with self.assertRaisesRegex(
                providers.ProviderError, "生图媒体下载失败"
            ):
                asyncio.run(providers.image("安全测试"))

    def test_provider_base64_image_is_bounded_and_magic_checked(self):
        payload = {
            "data": [{
                "b64_json": base64.b64encode(
                    b"<html>not an image</html>"
                ).decode(),
            }]
        }
        with (
            patch.object(providers, "yunwu_conf", return_value=(
                "https://gateway.example", "secret"
            )),
            patch.object(
                providers.httpx,
                "AsyncClient",
                return_value=_ProviderClient(payload),
            ),
        ):
            with self.assertRaisesRegex(
                providers.ProviderError, "生图媒体下载失败"
            ):
                asyncio.run(providers.image("安全测试"))

    def test_product_shot_rejects_private_supplier_url(self):
        payload = {
            "data": [{
                "url": "http://169.254.169.254/latest/meta-data/",
            }]
        }
        with (
            patch.object(growth.providers, "yunwu_conf", return_value=(
                "https://gateway.example", "secret"
            )),
            patch.object(
                growth.providers,
                "call_vision",
                AsyncMock(return_value={
                    "text": '{"edit_prompt":"安全产品图编辑"}',
                    "cost_usd": 0,
                    "tokens": 1,
                }),
            ),
            patch.object(
                growth.httpx,
                "AsyncClient",
                return_value=_ProviderClient(payload),
            ),
        ):
            with self.assertRaisesRegex(
                providers.ProviderError, "图生图媒体下载失败"
            ):
                asyncio.run(
                    growth.product_shot(
                        1, b"\x89PNG\r\n\x1a\nsample", "白色背景"
                    )
                )

    def test_avatar_audio_and_video_downloads_do_not_accept_private_or_html(self):
        with self.assertRaises(providers.ProviderError):
            asyncio.run(
                avatar._download_supplier_audio(
                    "http://127.0.0.1:8899/api/settings"
                )
            )

        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "avatar.mp4")

            def handler(request):
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    content=b"<html>secret</html>",
                    request=request,
                )

            fake = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                follow_redirects=False,
            )
            with (
                patch(
                    "app.netfetch.guard_public_url",
                    new=AsyncMock(return_value=()),
                ),
                patch.object(netfetch.httpx, "AsyncClient", return_value=fake),
            ):
                with self.assertRaisesRegex(ValueError, "媒体类型"):
                    asyncio.run(
                        netfetch.download_public_media(
                            "https://media.example/avatar.mp4",
                            target,
                            kind="video",
                            max_bytes=1024,
                        )
                    )
            self.assertFalse(Path(target).exists())

    def test_supplier_media_paths_do_not_use_unbounded_get_content(self):
        root = Path(__file__).resolve().parents[1]
        for name in (
            "providers.py", "growth.py", "avatar.py", "textvideo.py"
        ):
            with self.subTest(name=name):
                source = (root / "app" / name).read_text(encoding="utf-8")
                self.assertNotIn("await cli.get(item[\"url\"])", source)
                self.assertNotIn("await cli.get(audio_url)", source)
                self.assertNotIn("await cli.get(url)", source)


if __name__ == "__main__":
    unittest.main()
