"""爆款链接内容提取(V10).

- 抖音/快手/B站等视频链接:yt-dlp 抓音频 → 云雾 whisper-1 转文字;
- 图片:走 /api/parse-file 的视觉OCR(前端直接传图);
- 公众号/网页文章：回退云雾能力网关联网读取（在 main 里）。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import ipaddress
import logging
import os
import socket
import struct
import threading
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import httpx

log = logging.getLogger("linkgrab")


# 额外拉黑段:RFC6598 CGNAT(整个 tailnet 都在 100.64/10,必须拦)/ IPv6 ULA
_EXTRA_BLOCK = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fc00::/7"))
DNS_TIMEOUT_SECONDS = 5
_DNS_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="paihuo-dns",
)
_DNS_OUTSTANDING = threading.BoundedSemaphore(8)


@lru_cache(maxsize=1)
def _local_interface_addresses() -> frozenset[str]:
    """Collect local interface IPs, including public addresses bound to the host."""
    addresses: set[str] = set()
    configured = os.environ.get("CONTENTCREW_BLOCKED_FETCH_IPS") or ""
    for raw in configured.split(","):
        try:
            addresses.add(str(ipaddress.ip_address(raw.strip())))
        except ValueError:
            continue
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addresses.add(str(ipaddress.ip_address(info[4][0])))
    except (OSError, ValueError):
        pass

    # Linux exposes the server's directly bound public IPv4 address through
    # SIOCGIFADDR even when /etc/hosts maps the hostname only to 127.0.1.1.
    try:
        import fcntl
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for _, interface in socket.if_nameindex():
                try:
                    packed = struct.pack(
                        "256s", interface.encode("utf-8")[:15]
                    )
                    reply = fcntl.ioctl(probe.fileno(), 0x8915, packed)
                    addresses.add(socket.inet_ntoa(reply[20:24]))
                except (OSError, ValueError):
                    continue
    except (ImportError, OSError):
        pass

    try:
        with open("/proc/net/if_inet6", encoding="ascii") as handle:
            for line in handle:
                packed = line.split()[0]
                addresses.add(str(ipaddress.ip_address(int(packed, 16))))
    except (OSError, ValueError, IndexError):
        pass
    return frozenset(addresses)


def _public_host_addresses(host: str) -> tuple[str, ...]:
    """Resolve once and return public IPs; mixed public/private answers fail closed."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return ()
    if not infos:
        return ()
    local_addresses = _local_interface_addresses()
    addresses = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return ()
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
            return ()
        if any(addr in net for net in _EXTRA_BLOCK):
            return ()
        text = str(addr)
        if text in local_addresses:
            return ()
        if text not in addresses:
            addresses.append(text)
    return tuple(addresses)


def _host_is_public(host: str) -> bool:
    return bool(_public_host_addresses(host))


async def _guard_url(url: str):
    """Validate a URL and return the exact public IP set to pin the connection."""
    p = urlparse(url)
    if (p.scheme not in ("http", "https") or not p.hostname
            or p.username is not None or p.password is not None):
        raise ValueError("只支持 http/https 链接")
    try:
        port = p.port
    except ValueError as exc:
        raise ValueError("链接端口无效") from exc
    default_port = 443 if p.scheme == "https" else 80
    if port not in (None, default_port):
        raise ValueError("链接只允许标准 Web 端口")
    addresses = await _resolve_public_host(p.hostname)
    if not addresses:
        raise ValueError("这个地址指向内网/本机,已拒绝访问")
    return addresses


async def _resolve_public_host(host: str) -> tuple[str, ...]:
    """Resolve on a small bounded pool so slow DNS cannot exhaust app workers."""
    if not _DNS_OUTSTANDING.acquire(blocking=False):
        raise ValueError("域名解析繁忙，请稍后重试")
    concurrent_future = _DNS_EXECUTOR.submit(_public_host_addresses, host)

    def release_slot(_future):
        try:
            _DNS_OUTSTANDING.release()
        except ValueError:
            pass

    concurrent_future.add_done_callback(release_slot)
    future = asyncio.wrap_future(concurrent_future)
    try:
        addresses = await asyncio.wait_for(
            asyncio.shield(future),
            timeout=DNS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise ValueError("域名解析超时") from exc
    return addresses


def _pinned_request(url: str, addresses) -> tuple[str, dict, dict]:
    """Build an IP-pinned request while preserving HTTP Host and TLS SNI."""
    if not addresses:  # tests/custom transports may deliberately stub resolution
        return url, {}, {}
    parsed = urlsplit(url)
    address = str(addresses[0])
    ip_host = f"[{address}]" if ":" in address else address
    netloc = ip_host + (f":{parsed.port}" if parsed.port is not None else "")
    target = urlunsplit(
        (parsed.scheme, netloc, parsed.path or "/", parsed.query, "")
    )
    original_host = (
        f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
    )
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port is not None and parsed.port != default_port:
        original_host = f"{original_host}:{parsed.port}"
    return target, {"Host": original_host}, {"sni_hostname": parsed.hostname}

VIDEO_HOSTS = ("douyin.com", "iesdouyin.com", "kuaishou.com", "chenzhongtech.com",
               "bilibili.com", "b23.tv", "xiaohongshu.com", "xhslink.com")


def is_video_link(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in VIDEO_HOSTS)


UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


async def fetch_page_evidence(
        url: str, *, max_bytes: int = 2 * 1024 * 1024,
        timeout: float = 30, client: httpx.AsyncClient | None = None,
        min_zh_chars: int = 40) -> dict:
    """安全打开公开网页，返回终跳 URL、标题和正文证据。

    每次跳转都会重新做公网地址校验并固定解析到已校验 IP，避免搜索卡片、
    短链或中途重定向把抓取器带回内网。调用方可注入 client 便于测试；注入
    的 client 生命周期由调用方负责。
    """
    import re
    # 手动跟跳转:每一跳都要重新校验,防止公网短链 302 到内网(SSRF 绕过)
    if max_bytes <= 0:
        raise ValueError("网页大小限制无效")
    if timeout <= 0:
        raise ValueError("网页核验时限无效")
    owns_client = client is None
    cli = client or httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        limits=httpx.Limits(max_keepalive_connections=0),
    )
    try:
        async with asyncio.timeout(timeout):
            cur, html = url, ""
            for _ in range(6):
                addresses = await _guard_url(cur)
                target, pinned_headers, extensions = _pinned_request(
                    cur, addresses)
                request_headers = {
                    "User-Agent": UA_MOBILE,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    **pinned_headers,
                }
                async with cli.stream(
                        "GET", target, headers=request_headers,
                        extensions=extensions) as r:
                    loc = r.headers.get("location")
                    if r.is_redirect and loc:
                        cur = urljoin(cur, loc)
                        continue
                    r.raise_for_status()
                    declared = r.headers.get("content-length")
                    if declared:
                        try:
                            if int(declared) > max_bytes:
                                raise ValueError(
                                    "网页响应超过大小限制")
                        except ValueError:
                            if declared.isdigit():
                                raise
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise ValueError("网页响应超过大小限制")
                    encoding = r.encoding or "utf-8"
                    try:
                        html = bytes(body).decode(
                            encoding, errors="replace")
                    except LookupError:
                        html = bytes(body).decode(
                            "utf-8", errors="replace")
                    break
            else:
                raise ValueError("网页重定向次数过多")
    except TimeoutError as exc:
        raise ValueError("网页核验总超时") from exc
    finally:
        if owns_client:
            await cli.aclose()
    metas = []
    for pat in (r'<title[^>]*>(.*?)</title>',
                r'og:title"?\s+content="([^"]+)"', r'content="([^"]+)"\s+name="description"',
                r'name="description"\s+content="([^"]+)"', r'og:description"?\s+content="([^"]+)"',
                r'"desc":"([^"]{20,500})"', r'"title":"([^"]{5,120})"'):
        for m in re.findall(pat, html, re.S | re.I)[:2]:
            t = re.sub(r"\s+", " ", m).strip()
            if t and t not in metas:
                metas.append(t)
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"&[a-z]+;", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    # 公众号等文章页正文很长;JS 页正文很短则靠 meta
    text = ("【标题/描述】" + " | ".join(metas[:5]) + "\n【正文】" + body[:6000]) if metas else body[:6000]
    if min_zh_chars > 0:
        zh = len(re.findall(r"[一-鿿]", text))
        if zh < min_zh_chars:
            raise ValueError("页面没抓到有效中文内容")
    elif len(text) < 80:
        raise ValueError("页面没抓到足够正文内容")
    return {
        "source_url": cur,
        "source_title": metas[0][:160] if metas else "",
        "text": text,
    }


async def fetch_page_text(url: str) -> str:
    """直连抓页面正文/元信息(公众号文章全文可取;抖音/小红书至少能拿到标题+描述)."""
    return (await fetch_page_evidence(url))["text"]


async def transcribe_link(url: str) -> str:
    """视频链接 → 文案文字。当前统一回退到受控云端联网能力。"""
    if not is_video_link(url):
        raise ValueError("视频下载只允许受支持的平台域名")
    raise ValueError(
        "本地视频下载无法逐跳验证媒体地址，已切换受控联网能力网关"
    )
