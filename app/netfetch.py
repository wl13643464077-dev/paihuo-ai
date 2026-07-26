"""受控下载外部供应商返回的媒体。

供应商响应里的 URL 仍属于不可信输入。这里统一执行：

- 仅允许无凭据的 http/https；
- DNS 解析结果必须全部是公网地址，并把连接固定到本次解析的 IP；
- 每一跳重定向重新校验；
- 流式读取并执行硬字节上限；
- 拒绝明显不匹配的 Content-Type，并校验媒体魔数；
- 大视频直接写临时文件后原子替换，避免整块进入内存。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import os
import socket
import tempfile
import threading
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import httpx

from .linkgrab import _local_interface_addresses


_EXTRA_BLOCK = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
)
DNS_TIMEOUT_SECONDS = 5
_DNS_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="paihuo-media-dns",
)
_DNS_OUTSTANDING = threading.BoundedSemaphore(8)
_GENERIC_MEDIA_TYPES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "application/download",
}


def _public_host_addresses(host: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return ()
    if not infos:
        return ()
    local_addresses = _local_interface_addresses()
    addresses: list[str] = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return ()
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or any(address in network for network in _EXTRA_BLOCK)
        ):
            return ()
        text = str(address)
        if text in local_addresses:
            return ()
        if text not in addresses:
            addresses.append(text)
    return tuple(addresses)


async def _resolve_public_host(host: str) -> tuple[str, ...]:
    """Resolve on a bounded pool with a real deadline for blocking DNS calls."""
    if not _DNS_OUTSTANDING.acquire(blocking=False):
        raise ValueError("远程媒体域名解析繁忙，请稍后重试")
    concurrent_future = _DNS_EXECUTOR.submit(_public_host_addresses, host)

    def release_slot(_future):
        try:
            _DNS_OUTSTANDING.release()
        except ValueError:
            pass

    concurrent_future.add_done_callback(release_slot)
    future = asyncio.wrap_future(concurrent_future)
    try:
        return await asyncio.wait_for(
            asyncio.shield(future),
            timeout=DNS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise ValueError("远程媒体域名解析超时") from exc


async def guard_public_url(url: str) -> tuple[str, ...]:
    parsed = urlparse(str(url or ""))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("远程媒体地址无效")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("远程媒体端口无效") from exc
    default_port = 443 if parsed.scheme == "https" else 80
    if port not in (None, default_port):
        raise ValueError("远程媒体只允许标准 Web 端口")
    addresses = await _resolve_public_host(parsed.hostname.rstrip("."))
    if not addresses:
        raise ValueError("远程媒体地址指向本机或内网")
    return addresses


def _pinned_request(url: str, addresses: tuple[str, ...]) -> tuple[str, dict, dict]:
    """连接固定到已校验 IP，同时保留 HTTP Host 与 TLS SNI。"""
    if not addresses:  # 便于受控测试 transport 注入。
        return url, {}, {}
    parsed = urlsplit(url)
    address = addresses[0]
    ip_host = f"[{address}]" if ":" in address else address
    netloc = ip_host + (f":{parsed.port}" if parsed.port is not None else "")
    target = urlunsplit(
        (parsed.scheme, netloc, parsed.path or "/", parsed.query, "")
    )
    original_host = (
        f"[{parsed.hostname}]"
        if ":" in (parsed.hostname or "")
        else parsed.hostname
    )
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port is not None and parsed.port != default_port:
        original_host = f"{original_host}:{parsed.port}"
    return target, {"Host": original_host}, {"sni_hostname": parsed.hostname}


def _content_type_allowed(value: str, kind: str) -> bool:
    content_type = str(value or "").split(";", 1)[0].strip().lower()
    if content_type in _GENERIC_MEDIA_TYPES:
        return True
    if kind == "image":
        return content_type.startswith("image/")
    if kind == "audio":
        return content_type.startswith("audio/") or content_type in {
            "application/ogg",
            "application/x-mpegurl",
        }
    if kind == "video":
        return content_type.startswith("video/") or content_type in {
            "application/mp4",
            "application/webm",
            "application/ogg",
        }
    return False


def _looks_image(data: bytes) -> bool:
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith((b"GIF87a", b"GIF89a"))
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )


def _looks_audio(data: bytes) -> bool:
    return (
        data.startswith(b"ID3")
        or (
            len(data) >= 2
            and data[0] == 0xFF
            and (data[1] & 0xE0) == 0xE0
        )
        or (data.startswith(b"RIFF") and data[8:12] == b"WAVE")
        or data.startswith(b"fLaC")
        or data.startswith(b"OggS")
        or (len(data) >= 12 and data[4:8] == b"ftyp")
    )


def _looks_video(data: bytes) -> bool:
    return (
        (len(data) >= 12 and data[4:8] == b"ftyp")
        or data.startswith(b"\x1aE\xdf\xa3")  # WebM/Matroska EBML
        or data.startswith(b"OggS")
        or (data.startswith(b"RIFF") and data[8:12] == b"AVI ")
    )


def validate_media_bytes(data: bytes, kind: str, max_bytes: int) -> bytes:
    if not data or len(data) > int(max_bytes):
        raise ValueError("远程媒体为空或超过大小限制")
    validators = {
        "image": _looks_image,
        "audio": _looks_audio,
        "video": _looks_video,
    }
    validator = validators.get(kind)
    if validator is None or not validator(data[:512]):
        raise ValueError(f"远程响应不是有效{kind}媒体")
    return data


async def _open_public_response(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict | None,
    max_bytes: int,
    kind: str,
):
    """Yield the first terminal response after guarded redirects."""
    current = str(url or "")
    for _ in range(6):
        addresses = await guard_public_url(current)
        target, pinned_headers, extensions = _pinned_request(current, addresses)
        request_headers = dict(headers or {})
        request_headers.update(pinned_headers)
        stream = client.stream(
            "GET",
            target,
            headers=request_headers,
            extensions=extensions,
        )
        response = await stream.__aenter__()
        location = response.headers.get("location")
        if response.is_redirect and location:
            await stream.__aexit__(None, None, None)
            current = urljoin(current, location)
            continue
        try:
            response.raise_for_status()
            if not _content_type_allowed(response.headers.get("content-type"), kind):
                raise ValueError("远程响应的媒体类型不匹配")
            declared = response.headers.get("content-length")
            if declared:
                try:
                    if int(declared) > max_bytes:
                        raise ValueError("远程媒体超过大小限制")
                except ValueError:
                    if str(declared).isdigit():
                        raise
            return stream, response
        except Exception:
            await stream.__aexit__(None, None, None)
            raise
    raise ValueError("远程媒体重定向次数过多")


async def fetch_public_media(
    url: str,
    *,
    kind: str,
    max_bytes: int,
    timeout: float = 120,
    headers: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> bytes:
    if timeout <= 0:
        raise ValueError("远程媒体下载时限无效")
    own_client = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=20),
        follow_redirects=False,
        limits=httpx.Limits(max_keepalive_connections=0),
    )
    stream = response = None
    try:
        try:
            async with asyncio.timeout(timeout):
                stream, response = await _open_public_response(
                    http,
                    url,
                    headers=headers,
                    max_bytes=max_bytes,
                    kind=kind,
                )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError("远程媒体超过大小限制")
                return validate_media_bytes(bytes(body), kind, max_bytes)
        except TimeoutError as exc:
            raise ValueError("远程媒体下载总超时") from exc
    finally:
        if stream is not None:
            await stream.__aexit__(None, None, None)
        if own_client:
            await http.aclose()


async def download_public_media(
    url: str,
    target_path: str,
    *,
    kind: str,
    max_bytes: int,
    timeout: float = 300,
    headers: dict | None = None,
) -> int:
    """流式下载到同目录临时文件，校验后原子替换目标。"""
    if timeout <= 0:
        raise ValueError("远程媒体下载时限无效")
    directory = os.path.realpath(os.path.dirname(target_path))
    os.makedirs(directory, mode=0o750, exist_ok=True)
    target_real = os.path.realpath(target_path)
    if os.path.commonpath([directory, target_real]) != directory:
        raise ValueError("媒体输出路径越界")

    descriptor, temporary = tempfile.mkstemp(
        prefix=".media-", suffix=".part", dir=directory
    )
    os.fchmod(descriptor, 0o600)
    stream = response = None
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=20),
        follow_redirects=False,
        limits=httpx.Limits(max_keepalive_connections=0),
    )
    total = 0
    prefix = bytearray()
    try:
        try:
            async with asyncio.timeout(timeout):
                stream, response = await _open_public_response(
                    http,
                    url,
                    headers=headers,
                    max_bytes=max_bytes,
                    kind=kind,
                )
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    descriptor = -1
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("远程媒体超过大小限制")
                        if len(prefix) < 512:
                            prefix.extend(chunk[: 512 - len(prefix)])
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                validate_media_bytes(bytes(prefix), kind, max_bytes)
                if total <= 0:
                    raise ValueError("远程媒体为空")
                os.replace(temporary, target_real)
                os.chmod(target_real, 0o640)
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                return total
        except TimeoutError as exc:
            raise ValueError("远程媒体下载总超时") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if stream is not None:
            await stream.__aexit__(None, None, None)
        await http.aclose()
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
