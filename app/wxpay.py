"""微信支付 APIv3 · Native 扫码支付(默认关闭，平台 root 配置后才启用)。

这里只放协议层：配置读写与校验、请求签名、应答/回调验签、回调资源解密，
以及 Native 下单 / 查单 / 关单三个接口。订单状态与开通入账在 purchases.py。

- 请求签名：SHA256withRSA(商户私钥)，签名串
  ``METHOD\\nURL\\n时间戳\\n随机串\\n报文主体\\n``，放进
  ``Authorization: WECHATPAY2-SHA256-RSA2048 ...``。
- 应答与回调验签：签名串 ``时间戳\\n随机串\\n报文主体\\n``，用微信支付公钥
  (“微信支付公钥”模式，配公钥 ID + 公钥即可；也兼容直接粘贴平台证书 PEM)
  校验 ``Wechatpay-Signature``，并核对 ``Wechatpay-Serial`` 与时间戳新鲜度。
- 回调资源：AEAD_AES_256_GCM，密钥为 APIv3 密钥(32 字节)。

配置整体作为一个 JSON 存在 secureconfig 的 ``wxpay_config``(生产环境加密)。
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import json
import logging
import re
import secrets
import time
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import secureconfig

log = logging.getLogger("wxpay")

API_BASE = "https://api.mch.weixin.qq.com"
NATIVE_PATH = "/v3/pay/transactions/native"
AUTH_SCHEMA = "WECHATPAY2-SHA256-RSA2048"
CONFIG_SETTING = "wxpay_config"
# 应答/回调时间戳允许的偏差(秒)，超出即视为重放。
MAX_CLOCK_SKEW = 300
HTTP_TIMEOUT = 10.0
_BEIJING = _dt.timezone(_dt.timedelta(hours=8))

REQUIRED_FIELDS = (
    "mchid",
    "appid",
    "apiv3_key",
    "private_key",
    "merchant_serial_no",
    "public_key_id",
    "public_key",
    "notify_url",
)
SECRET_FIELDS = frozenset({"apiv3_key", "private_key"})
FIELD_LABELS = {
    "mchid": "商户号",
    "appid": "AppID",
    "apiv3_key": "APIv3 密钥",
    "private_key": "商户 API 私钥",
    "merchant_serial_no": "商户证书序列号",
    "public_key_id": "微信支付公钥 ID",
    "public_key": "微信支付公钥",
    "notify_url": "支付结果回调地址",
}

_MCHID_RE = re.compile(r"^\d{8,12}$")
_APPID_RE = re.compile(r"^wx[0-9A-Za-z]{16}$")
_SERIAL_RE = re.compile(r"^[0-9A-Fa-f]{8,64}$")
_PUBKEY_ID_RE = re.compile(r"^[0-9A-Za-z_]{8,80}$")
_OUT_TRADE_NO_RE = re.compile(r"^[0-9A-Za-z_\-|*]{6,32}$")


class WxPayError(RuntimeError):
    """调用微信支付失败(网络、应答码或应答验签)。消息可直接给运维看，不含密钥。"""


class WxPayConfigError(ValueError):
    """配置缺失或格式不对。"""


class WxPaySignatureError(ValueError):
    """应答或回调验签失败：内容不可信，不能据此改任何状态。"""


# ---------------------------------------------------------------- 配置

def _empty_config() -> dict:
    return {"enabled": False, **{field: "" for field in REQUIRED_FIELDS}}


def load_config() -> dict:
    """读出完整配置(含密钥，仅供服务端内部使用)。"""
    raw = secureconfig.get_secret(CONFIG_SETTING, "")
    config = _empty_config()
    if not raw:
        return config
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        log.error("wxpay config is not valid json")
        return config
    if not isinstance(stored, dict):
        return config
    for field in REQUIRED_FIELDS:
        value = stored.get(field)
        config[field] = str(value).strip() if isinstance(value, str) else ""
    config["enabled"] = stored.get("enabled") is True
    return config


def missing_fields(config: dict) -> list[str]:
    return [field for field in REQUIRED_FIELDS if not str(config.get(field) or "").strip()]


def _load_private_key(pem: str):
    try:
        key = serialization.load_pem_private_key(
            str(pem or "").strip().encode("utf-8"), password=None
        )
    except (TypeError, ValueError) as exc:
        raise WxPayConfigError("商户 API 私钥格式不对，请粘贴 apiclient_key.pem 的完整内容") from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise WxPayConfigError("商户 API 私钥必须是 2048 位以上的 RSA 私钥")
    return key


def _load_public_key(pem: str):
    """支持“微信支付公钥”PEM，也兼容直接粘贴平台证书 PEM。"""
    data = str(pem or "").strip().encode("utf-8")
    try:
        if b"BEGIN CERTIFICATE" in data:
            key = x509.load_pem_x509_certificate(data).public_key()
        else:
            key = serialization.load_pem_public_key(data)
    except (TypeError, ValueError) as exc:
        raise WxPayConfigError("微信支付公钥格式不对，请粘贴 pub_key.pem 的完整内容") from exc
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise WxPayConfigError("微信支付公钥必须是 2048 位以上的 RSA 公钥")
    return key


def _validate_field(field: str, value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    if len(clean) > secureconfig.MAX_SECRET_CHARS // 2:
        raise WxPayConfigError(f"{FIELD_LABELS[field]}太长")
    if field == "mchid" and not _MCHID_RE.fullmatch(clean):
        raise WxPayConfigError("商户号应为 8-12 位数字")
    if field == "appid" and not _APPID_RE.fullmatch(clean):
        raise WxPayConfigError("AppID 格式不对(以 wx 开头的 18 位)")
    if field == "apiv3_key" and (
        len(clean.encode("utf-8")) != 32 or not clean.isascii()
    ):
        raise WxPayConfigError("APIv3 密钥必须是 32 位字母或数字")
    if field == "merchant_serial_no" and not _SERIAL_RE.fullmatch(clean):
        raise WxPayConfigError("商户证书序列号格式不对")
    if field == "public_key_id" and not _PUBKEY_ID_RE.fullmatch(clean):
        raise WxPayConfigError("微信支付公钥 ID 格式不对(形如 PUB_KEY_ID_...)")
    if field == "private_key":
        _load_private_key(clean)
    if field == "public_key":
        _load_public_key(clean)
    if field == "notify_url":
        parts = urlsplit(clean)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise WxPayConfigError("回调地址必须是 https:// 开头、不带参数的完整网址")
    return clean


def config_ready(config: dict | None) -> bool:
    """开关打开且字段齐全、可解析，才算可以收款。"""
    if not config or config.get("enabled") is not True:
        return False
    if missing_fields(config):
        return False
    try:
        for field in REQUIRED_FIELDS:
            _validate_field(field, config[field])
    except WxPayConfigError:
        return False
    return True


def is_enabled() -> bool:
    """给前台判断要不要显示“微信扫码付款”；读配置出错一律按关闭处理。"""
    try:
        return config_ready(load_config())
    except Exception as exc:  # noqa: BLE001 - 读配置失败不能让价目页跟着挂
        log.error("wxpay config unreadable error_type=%s", type(exc).__name__)
        return False


def save_config(updates: dict) -> dict:
    """合并保存配置；密钥字段留空表示“保持不变”，``clear`` 为真表示整体清除。"""
    if not isinstance(updates, dict):
        raise WxPayConfigError("配置格式不对")
    if updates.get("clear") is True:
        secureconfig.set_secret(CONFIG_SETTING, None)
        return public_view(_empty_config())
    current = load_config()
    merged = dict(current)
    for field in REQUIRED_FIELDS:
        if field not in updates:
            continue
        value = updates.get(field)
        if value is not None and not isinstance(value, str):
            raise WxPayConfigError(f"{FIELD_LABELS[field]}格式不对")
        clean = _validate_field(field, value or "")
        if field in SECRET_FIELDS and not clean:
            continue  # 密钥不回显，留空=不修改
        merged[field] = clean
    if "enabled" in updates:
        merged["enabled"] = updates.get("enabled") is True
    if merged["enabled"]:
        missing = missing_fields(merged)
        if missing:
            raise WxPayConfigError(
                "还缺：" + "、".join(FIELD_LABELS[field] for field in missing)
                + "，填齐后才能开启在线支付"
            )
    secureconfig.set_secret(
        CONFIG_SETTING,
        json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
    )
    return public_view(merged)


def public_view(config: dict | None = None) -> dict:
    """给 root 后台看的脱敏视图：密钥只回“已设置”。"""
    config = config if config is not None else load_config()
    view = {
        "enabled": bool(config.get("enabled")),
        "ready": config_ready(config),
        "missing": [FIELD_LABELS[field] for field in missing_fields(config)],
    }
    for field in REQUIRED_FIELDS:
        if field in SECRET_FIELDS:
            view[f"{field}_set"] = bool(config.get(field))
        elif field == "public_key":
            view["public_key_set"] = bool(config.get(field))
        else:
            view[field] = str(config.get(field) or "")
    return view


# ---------------------------------------------------------------- 签名与验签

def nonce_str() -> str:
    return secrets.token_hex(16)


def sign_message(private_key_pem: str, message: str) -> str:
    key = _load_private_key(private_key_pem)
    signature = key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def request_message(method: str, url_path: str, timestamp: str, nonce: str, body: str) -> str:
    return f"{method.upper()}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n"


def build_authorization(
    config: dict,
    method: str,
    url_path: str,
    body: str = "",
    *,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> str:
    """生成请求头 Authorization 的值。url_path 含查询串，不含域名。"""
    ts = str(timestamp or int(time.time()))
    nc = nonce or nonce_str()
    signature = sign_message(
        config["private_key"], request_message(method, url_path, ts, nc, body)
    )
    return (
        f'{AUTH_SCHEMA} mchid="{config["mchid"]}",nonce_str="{nc}",'
        f'signature="{signature}",timestamp="{ts}",'
        f'serial_no="{config["merchant_serial_no"]}"'
    )


def verify_message(public_key_pem: str, timestamp: str, nonce: str, body, signature_b64: str) -> bool:
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return False
    message = f"{timestamp}\n{nonce}\n{body}\n".encode("utf-8")
    try:
        signature = base64.b64decode(str(signature_b64 or ""), validate=True)
    except (binascii.Error, ValueError):
        return False
    if not signature:
        return False
    try:
        _load_public_key(public_key_pem).verify(
            signature, message, padding.PKCS1v15(), hashes.SHA256()
        )
    except InvalidSignature:
        return False
    return True


def _header(headers, name: str) -> str:
    if headers is None:
        return ""
    lowered = name.lower()
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).lower() == lowered:
            return str(value or "").strip()
    return ""


def verify_headers(config: dict, headers, body, *, now: float | None = None) -> None:
    """校验应答/回调签名；任何一项不符都抛 WxPaySignatureError。"""
    timestamp = _header(headers, "Wechatpay-Timestamp")
    nonce = _header(headers, "Wechatpay-Nonce")
    signature = _header(headers, "Wechatpay-Signature")
    serial = _header(headers, "Wechatpay-Serial")
    if not (timestamp and nonce and signature and serial):
        raise WxPaySignatureError("缺少微信支付签名头")
    # 微信的“签名探测”流量故意带错签名，必须当作验签失败。
    if signature.startswith("WECHATPAY/SIGNTEST/"):
        raise WxPaySignatureError("签名探测请求")
    if serial != str(config.get("public_key_id") or ""):
        raise WxPaySignatureError("签名公钥 ID 与配置不一致")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise WxPaySignatureError("签名时间戳无效") from exc
    current = time.time() if now is None else float(now)
    if abs(current - ts) > MAX_CLOCK_SKEW:
        raise WxPaySignatureError("签名时间戳已过期")
    if not verify_message(config.get("public_key") or "", timestamp, nonce, body, signature):
        raise WxPaySignatureError("签名不正确")


def encrypt_resource(apiv3_key: str, plaintext: str, *, associated_data: str = "transaction", nonce: str | None = None) -> dict:
    """与 decrypt_resource 对称(微信侧加密方式)，供自测与联调构造报文。"""
    nc = nonce or secrets.token_hex(6)  # 12 字符随机串
    cipher = AESGCM(apiv3_key.encode("utf-8")).encrypt(
        nc.encode("utf-8"), plaintext.encode("utf-8"), associated_data.encode("utf-8")
    )
    return {
        "algorithm": "AEAD_AES_256_GCM",
        "ciphertext": base64.b64encode(cipher).decode("ascii"),
        "associated_data": associated_data,
        "nonce": nc,
    }


def decrypt_resource(apiv3_key: str, resource: dict) -> dict:
    """解密回调 resource，返回明文 JSON 对象。"""
    if not isinstance(resource, dict):
        raise WxPaySignatureError("回调资源格式不对")
    if resource.get("algorithm") != "AEAD_AES_256_GCM":
        raise WxPaySignatureError("不支持的回调加密算法")
    key = str(apiv3_key or "").encode("utf-8")
    if len(key) != 32:
        raise WxPaySignatureError("APIv3 密钥长度不对")
    try:
        ciphertext = base64.b64decode(str(resource.get("ciphertext") or ""), validate=True)
        plaintext = AESGCM(key).decrypt(
            str(resource.get("nonce") or "").encode("utf-8"),
            ciphertext,
            str(resource.get("associated_data") or "").encode("utf-8") or None,
        )
        data = json.loads(plaintext.decode("utf-8"))
    except (binascii.Error, InvalidTag, ValueError, UnicodeDecodeError) as exc:
        raise WxPaySignatureError("回调资源解密失败") from exc
    if not isinstance(data, dict):
        raise WxPaySignatureError("回调资源格式不对")
    return data


# ---------------------------------------------------------------- 接口调用

def _client() -> httpx.Client:
    """单独成函数，测试可替换成 httpx.MockTransport。"""
    return httpx.Client(base_url=API_BASE, timeout=HTTP_TIMEOUT)


def _request(config: dict, method: str, url_path: str, payload: dict | None = None) -> tuple[int, dict]:
    body = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if payload is not None
        else ""
    )
    headers = {
        "Accept": "application/json",
        "Authorization": build_authorization(config, method, url_path, body),
        "User-Agent": "paihuo-wxpay/1.0",
        # 微信支付公钥模式下带上公钥 ID，便于微信侧选择对应密钥。
        "Wechatpay-Serial": config["public_key_id"],
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        with _client() as client:
            response = client.request(
                method, url_path, content=body.encode("utf-8") if body else None,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise WxPayError(f"连接微信支付失败({type(exc).__name__})") from exc
    raw = response.content or b""
    if 200 <= response.status_code < 300:
        # 成功应答必须验签，否则网络中间人可伪造“已支付”。
        try:
            verify_headers(config, response.headers, raw)
        except WxPaySignatureError as exc:
            raise WxPayError(f"微信支付应答验签失败：{exc}") from exc
        if not raw:
            return response.status_code, {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WxPayError("微信支付应答不是有效 JSON") from exc
        return response.status_code, data if isinstance(data, dict) else {}
    try:
        error = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        error = {}
    code = str((error or {}).get("code") or response.status_code)[:60]
    message = str((error or {}).get("message") or "")[:120]
    raise WxPayError(f"微信支付返回错误 {code} {message}".strip())


def format_time_expire(epoch: float) -> str:
    """RFC3339 北京时间，如 2026-09-25T12:00:00+08:00。"""
    return _dt.datetime.fromtimestamp(float(epoch), _BEIJING).replace(microsecond=0).isoformat()


def _check_out_trade_no(out_trade_no: str) -> str:
    clean = str(out_trade_no or "")
    if not _OUT_TRADE_NO_RE.fullmatch(clean):
        raise WxPayError("商户订单号格式不对")
    return clean


def native_order(
    config: dict,
    *,
    out_trade_no: str,
    description: str,
    amount_fen: int,
    time_expire: float,
    attach: str = "",
) -> str:
    """Native 下单，返回 code_url(用来生成付款二维码)。"""
    amount = int(amount_fen)
    if amount <= 0:
        raise WxPayError("订单金额必须大于 0")
    payload = {
        "appid": config["appid"],
        "mchid": config["mchid"],
        "description": str(description or "派活套餐")[:40],
        "out_trade_no": _check_out_trade_no(out_trade_no),
        "time_expire": format_time_expire(time_expire),
        "notify_url": config["notify_url"],
        "amount": {"total": amount, "currency": "CNY"},
    }
    if attach:
        payload["attach"] = str(attach)[:120]
    _, data = _request(config, "POST", NATIVE_PATH, payload)
    code_url = str(data.get("code_url") or "")
    if not code_url.startswith("weixin://"):
        raise WxPayError("微信支付没有返回付款码")
    return code_url


def query_order(config: dict, out_trade_no: str) -> dict:
    """按商户订单号查单，返回交易对象(trade_state 等)。"""
    no = _check_out_trade_no(out_trade_no)
    path = f"/v3/pay/transactions/out-trade-no/{no}?mchid={config['mchid']}"
    _, data = _request(config, "GET", path)
    return data


def close_order(config: dict, out_trade_no: str) -> None:
    """关单：超时未付的订单主动关掉，防止之后再被支付。"""
    no = _check_out_trade_no(out_trade_no)
    _request(
        config,
        "POST",
        f"/v3/pay/transactions/out-trade-no/{no}/close",
        {"mchid": config["mchid"]},
    )
