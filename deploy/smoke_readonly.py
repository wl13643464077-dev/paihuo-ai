#!/usr/bin/env python3
"""Fixed, minimal and read-only authenticated production smoke client."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _request(
    base: str,
    method: str,
    path: str,
    body: dict | None = None,
    cookie: str | None = None,
) -> tuple[int, dict, str]:
    request = urllib.request.Request(
        base + path,
        data=(json.dumps(body).encode("utf-8") if body is not None else None),
        method=method,
        headers={"Content-Type": "application/json"},
    )
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read() or b"{}")
            return response.status, payload, response.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        return exc.code, {}, ""


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    base = sys.argv[1].rstrip("/")
    username = os.environ.get("SMOKE_USERNAME") or ""
    password = os.environ.get("SMOKE_PASSWORD") or ""
    if not username or not password:
        return 2
    status, payload, _ = _request(base, "GET", "/healthz")
    if status != 200 or payload != {"status": "ok"}:
        return 1
    status, _, _ = _request(base, "GET", "/api/auth/me")
    if status != 401:
        return 1
    status, payload, headers = _request(
        base,
        "POST",
        "/api/auth/login",
        {"username": username, "password": password},
    )
    cookie = headers.split(";", 1)[0]
    if status != 200 or not cookie or payload.get("role") != "member":
        return 1
    status, payload, _ = _request(
        base, "GET", "/api/auth/me", cookie=cookie
    )
    if (
        status != 200
        or payload.get("username") != username
        or payload.get("role") != "member"
        or payload.get("modules") != []
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
