import asyncio
import os
import socket
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import linkgrab


class LinkgrabSecurityCase(unittest.TestCase):
    def test_video_domain_matching_is_boundary_aware(self):
        self.assertTrue(linkgrab.is_video_link("https://www.douyin.com/video/1"))
        self.assertTrue(linkgrab.is_video_link("https://b23.tv/abc"))
        self.assertFalse(
            linkgrab.is_video_link("https://douyin.com.attacker.example/video/1")
        )
        self.assertFalse(
            linkgrab.is_video_link("https://attacker.example/?next=douyin.com")
        )

    def test_local_video_downloader_is_disabled_until_all_hops_are_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = os.path.join(tmp, "yt-dlp")
            with open(executable, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\nexit 0\n")
            os.chmod(executable, 0o700)
            with patch.dict(
                os.environ, {"CONTENTCREW_YTDLP_PATH": executable}, clear=False
            ), patch("subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "受控联网能力网关"):
                    asyncio.run(
                        linkgrab.transcribe_link(
                            "https://www.bilibili.com/video/BV1"
                        )
                    )
            run.assert_not_called()

    def test_public_dns_rejects_the_servers_own_public_interface(self):
        answer = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("203.0.113.10", 0),
            )
        ]
        with patch.object(
            linkgrab.socket, "getaddrinfo", return_value=answer
        ), patch.object(
            linkgrab,
            "_local_interface_addresses",
            return_value=frozenset({"203.0.113.10"}),
        ):
            self.assertEqual((), linkgrab._public_host_addresses("paihuo-self.test"))


class LinkgrabPageEvidenceCase(unittest.IsolatedAsyncioTestCase):
    async def test_public_dns_resolution_has_a_hard_timeout(self):
        def slow_dns(_host):
            time.sleep(0.05)
            return ("203.0.113.10",)

        with patch.object(
            linkgrab, "_public_host_addresses", side_effect=slow_dns
        ), patch.object(linkgrab, "DNS_TIMEOUT_SECONDS", 0.005):
            with self.assertRaisesRegex(ValueError, "域名解析超时"):
                await linkgrab._guard_url("https://public.example/post/1")

    async def test_page_evidence_returns_guarded_final_url_and_text(self):
        body = (
            "<html><head><title>成都装修怎么选</title></head><body>"
            + "这是一篇业主比较装修服务和预算方案的真实公开讨论。" * 8
            + "</body></html>"
        )

        def handler(request):
            if request.url.path == "/start":
                return httpx.Response(
                    302,
                    headers={"location": "/question/123"},
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=body.encode(),
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            with patch.object(
                linkgrab, "_guard_url", new=AsyncMock(return_value=())
            ):
                evidence = await linkgrab.fetch_page_evidence(
                    "https://public.example/start",
                    client=client,
                    max_bytes=64 * 1024,
                )

        self.assertEqual(
            evidence["source_url"],
            "https://public.example/question/123",
        )
        self.assertEqual(evidence["source_title"], "成都装修怎么选")
        self.assertIn("真实公开讨论", evidence["text"])

    async def test_guard_rejects_nonstandard_web_ports_before_dns(self):
        with patch.object(
            linkgrab, "_resolve_public_host", new=AsyncMock()
        ) as resolve:
            with self.assertRaisesRegex(ValueError, "只允许标准 Web 端口"):
                await linkgrab._guard_url(
                    "https://203.0.113.10:5902/private"
                )
        resolve.assert_not_awaited()

    async def test_page_evidence_timeout_is_a_total_redirect_budget(self):
        async def handler(request):
            await asyncio.sleep(0.05)
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=(
                    "<title>超时测试</title>"
                    + "这是用于验证整个网页核验总时限的公开中文正文。" * 10
                ).encode(),
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            with patch.object(
                linkgrab, "_guard_url", new=AsyncMock(return_value=())
            ):
                with self.assertRaisesRegex(ValueError, "网页核验总超时"):
                    await linkgrab.fetch_page_evidence(
                        "https://public.example/slow",
                        client=client,
                        timeout=0.01,
                    )

    async def test_page_evidence_revalidates_every_redirect_target(self):
        def handler(request):
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/private"},
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            with patch.object(
                linkgrab,
                "_guard_url",
                new=AsyncMock(
                    side_effect=[(), ValueError("这个地址指向内网/本机,已拒绝访问")]
                ),
            ):
                with self.assertRaisesRegex(ValueError, "内网/本机"):
                    await linkgrab.fetch_page_evidence(
                        "https://public.example/start",
                        client=client,
                    )


if __name__ == "__main__":
    unittest.main()
