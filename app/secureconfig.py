"""Authenticated storage for supplier credentials kept in ``app_setting``.

The database is backed up and handled by more code paths than the root-owned
systemd environment.  Supplier credentials therefore use an independent
root-provided wrapping key and are never persisted as plaintext in production.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .session_secret import (
    CONFIG_KEY_ENV,
    REQUIRE_CONFIG_KEY_ENV,
    validate_secret_token,
)


ENCRYPTED_PREFIX = "enc:v1:"
MAX_SECRET_CHARS = 16_384

SECRET_SETTING_KEYS = (
    "yunwu_key",
    "heygen_key",
    "feishu_app_secret",
    "smtp_authcode",
    "runninghub_key",
)
_SECRET_SETTING_SET = frozenset(SECRET_SETTING_KEYS)

# 部分凭据不是独立的 app_setting 行,而是存在按租户切分的 JSON 里
# (公众号 AppSecret 在 wechat_mp:{tid}、平台登录态 Cookie 在 matrix_accounts:{tid})。
# 它们同样不能明文落库,用与上面一致的密钥和绑定语义做字段级加密。
SECRET_FIELD_DOMAINS = (
    "wechat_mp_secret",
    "matrix_cookie",
)
_SECRET_FIELD_SET = frozenset(SECRET_FIELD_DOMAINS)


class SecureConfigError(RuntimeError):
    """A production secret is missing, weak, corrupt, or bound to another key."""


def _required() -> bool:
    return os.environ.get(REQUIRE_CONFIG_KEY_ENV) == "1"


def _configured_fernet() -> Fernet | None:
    raw = (os.environ.get(CONFIG_KEY_ENV) or "").strip()
    if not raw:
        if _required():
            raise SecureConfigError("production configuration key is missing")
        return None
    try:
        material = validate_secret_token(raw, variable=CONFIG_KEY_ENV)
    except ValueError as exc:
        raise SecureConfigError("production configuration key is invalid") from exc
    # The deployment tool uses the same high-entropy portable token format for
    # both root secrets.  Domain-separated SHA-256 turns that material into the
    # exact 32-byte Fernet key format without placing a second key in the env.
    derived = hashlib.sha256(b"paihuo-config-v1\0" + material).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def validate_runtime_key() -> bool:
    """Fail closed in production; return whether encryption is configured."""
    return _configured_fernet() is not None


def _validate_name(name: str) -> str:
    if name not in _SECRET_SETTING_SET:
        raise ValueError("unsupported secret setting")
    return name


def _encrypt(name: str, value: str, cipher: Fernet) -> str:
    payload = name.encode("utf-8") + b"\0" + value.encode("utf-8")
    return ENCRYPTED_PREFIX + cipher.encrypt(payload).decode("ascii")


def _decrypt(name: str, stored: str, cipher: Fernet) -> str:
    token = stored[len(ENCRYPTED_PREFIX):]
    try:
        payload = cipher.decrypt(token.encode("ascii"))
        bound_name, value = payload.split(b"\0", 1)
        if bound_name.decode("utf-8") != name:
            raise SecureConfigError("encrypted configuration binding is invalid")
        return value.decode("utf-8")
    except (
        InvalidToken,
        UnicodeDecodeError,
        UnicodeEncodeError,
        ValueError,
    ) as exc:
        raise SecureConfigError("encrypted configuration is invalid") from exc


def is_encrypted(stored) -> bool:
    return str(stored or "").startswith(ENCRYPTED_PREFIX)


def encrypt_field(domain: str, value: str | None) -> str:
    """加密嵌在 JSON 里的凭据字段;与 set_secret 同密钥、同绑定语义。

    没有配置包装密钥时(仅本地开发,生产由 REQUIRE_CONFIG_KEY=1 兜住)原样返回,
    与 set_secret 的降级行为保持一致。
    """
    if domain not in _SECRET_FIELD_SET:
        raise ValueError("unsupported secret field domain")
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if len(normalized) > MAX_SECRET_CHARS:
        raise ValueError("secret field is too long")
    if normalized.startswith(ENCRYPTED_PREFIX):
        return normalized          # 已是密文,避免重复保存时二次加密
    cipher = _configured_fernet()
    if cipher is None:
        return normalized
    return _encrypt(domain, normalized, cipher)


def decrypt_field(domain: str, stored: str | None) -> str:
    """读取嵌在 JSON 里的凭据字段;兼容升级前写入的明文历史值。"""
    if domain not in _SECRET_FIELD_SET:
        raise ValueError("unsupported secret field domain")
    text = str(stored or "")
    if not text:
        return ""
    if not text.startswith(ENCRYPTED_PREFIX):
        # 升级前的明文历史值:保持可用,下一次保存时会被自动加密。
        _configured_fernet()       # 生产缺密钥时仍然 fail-closed
        return text
    cipher = _configured_fernet()
    if cipher is None:
        raise SecureConfigError("configuration key is required for encrypted settings")
    return _decrypt(domain, text, cipher)


def set_secret(name: str, value: str | None) -> None:
    """Persist a supplier credential, encrypted whenever a wrapping key exists."""
    name = _validate_name(name)
    normalized = str(value or "").strip()
    if len(normalized) > MAX_SECRET_CHARS:
        raise ValueError("secret setting is too long")
    if not normalized:
        db.set_setting(name, None)
        return
    cipher = _configured_fernet()
    if cipher is None:
        # Local development remains usable without a machine-level key.  The
        # production systemd unit sets REQUIRE_CONFIG_KEY=1, so this branch is
        # unreachable in production.
        db.set_setting(name, normalized)
        return
    db.set_setting(name, _encrypt(name, normalized, cipher))


def get_secret(name: str, default: str = "") -> str:
    """Return one credential without ever accepting corrupt encrypted data."""
    name = _validate_name(name)
    stored = db.get_setting(name)
    if not stored:
        # Missing individual supplier credentials are a normal "not configured"
        # state.  The wrapping key itself is still mandatory in production.
        _configured_fernet()
        return default
    stored = str(stored)
    cipher = _configured_fernet()
    if stored.startswith(ENCRYPTED_PREFIX):
        if cipher is None:
            raise SecureConfigError("configuration key is required for encrypted settings")
        return _decrypt(name, stored, cipher)
    if cipher is None:
        return stored

    # A production upgrade can encounter one legacy plaintext value before the
    # startup migration.  Encrypt it transactionally before returning it.
    with db.atomic():
        current = db.get_setting(name)
        if current == stored:
            db.set_setting(name, _encrypt(name, stored, cipher))
        elif current and str(current).startswith(ENCRYPTED_PREFIX):
            return _decrypt(name, str(current), cipher)
        else:
            raise SecureConfigError("configuration changed during encryption")
    return stored


def migrate_legacy_secrets() -> dict[str, int]:
    """Atomically encrypt all legacy plaintext supplier credentials.

    Existing encrypted values are authenticated as part of startup.  One corrupt
    value aborts the transaction and therefore aborts the candidate service.
    """
    cipher = _configured_fernet()
    if cipher is None:
        return {"encrypted": 0, "verified": 0}
    encrypted = 0
    verified = 0
    marks = ",".join("?" for _ in SECRET_SETTING_KEYS)
    with db.atomic() as connection:
        rows = connection.execute(
            f"SELECT key,value FROM app_setting WHERE key IN ({marks})",
            SECRET_SETTING_KEYS,
        ).fetchall()
        for row in rows:
            name = str(row["key"])
            stored = row["value"]
            if not stored:
                continue
            stored = str(stored)
            if stored.startswith(ENCRYPTED_PREFIX):
                _decrypt(name, stored, cipher)
                verified += 1
                continue
            if len(stored) > MAX_SECRET_CHARS:
                raise SecureConfigError("legacy configuration value is too long")
            db.set_setting(name, _encrypt(name, stored, cipher))
            encrypted += 1
    return {"encrypted": encrypted, "verified": verified}
