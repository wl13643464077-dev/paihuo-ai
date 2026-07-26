"""Validation shared by the session signer and production preflight.

The configured value is deliberately restricted to an unquoted, portable
token so the same bytes reach systemd, Python and the root-owned env-file
maintenance utility without shell parsing ambiguities.
"""
from __future__ import annotations

from collections import Counter
import math
import re


SESSION_SECRET_ENV = "CONTENTCREW_SESSION_SECRET"
CONFIG_KEY_ENV = "CONTENTCREW_CONFIG_KEY"
REQUIRE_SESSION_SECRET_ENV = "CONTENTCREW_REQUIRE_SESSION_SECRET"
REQUIRE_CONFIG_KEY_ENV = "CONTENTCREW_REQUIRE_CONFIG_KEY"
MIN_SECRET_BYTES = 32
MIN_EFFECTIVE_ENTROPY_BITS = 160.0
MAX_SECRET_BYTES = 512
_PORTABLE_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_secret_token(value: str, *, variable: str) -> bytes:
    """Return the exact configured bytes or reject weak/ambiguous values.

    This cannot prove how an operator generated a token.  It does fail closed
    for short values, repeated/pattern-like low-diversity values, whitespace
    and quoting mistakes.  The deployment writer generates 48 random bytes
    using ``secrets`` and encodes them as URL-safe base64.
    """
    if not isinstance(value, str):
        raise ValueError(f"{variable} 必须是字符串")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{variable} 必须是未加引号的 URL-safe ASCII token"
        ) from exc
    if not (MIN_SECRET_BYTES <= len(encoded) <= MAX_SECRET_BYTES):
        raise ValueError(
            f"{variable} 至少需要 {MIN_SECRET_BYTES} 字节"
        )
    if not _PORTABLE_TOKEN.fullmatch(value):
        raise ValueError(
            f"{variable} 必须是未加引号的 URL-safe ASCII token"
        )

    counts = Counter(encoded)
    length = len(encoded)
    entropy_per_symbol = -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )
    effective_entropy = length * entropy_per_symbol
    if effective_entropy < MIN_EFFECTIVE_ENTROPY_BITS:
        raise ValueError(f"{variable} 强度不足")
    return encoded


def validate_session_secret(value: str) -> bytes:
    return validate_secret_token(value, variable=SESSION_SECRET_ENV)
