"""手机号 + 短信验证码登录（可配置，默认关闭）。

- 短信通道：阿里云短信 SendSms，按阿里云 RPC 风格 OpenAPI 签名规范（HMAC-SHA1，
  SignatureVersion=1.0）用 httpx 直接调用，不引入 SDK；签名算法是纯函数，见
  ``percent_encode`` / ``string_to_sign`` / ``rpc_signature``。
- 配置：AccessKey ID / Secret 走 secureconfig 加密存储；签名名称、模板 Code、
  总开关是普通 app_setting。四项齐全且开关打开才算启用，否则登录页不显示入口。
- 验证码：6 位、5 分钟有效、一次性；存进程内存（单进程部署）；
  同一手机号 60 秒 1 次、每天（北京时间）最多 10 次；同 IP 每小时/每天限流；
  校验用常量时间比较，同一验证码最多错 5 次就作废。
- 手机号必须是已存在账号的用户名，或开户申请里绑定的手机号；不存在时对外文案
  与存在时完全一样，也照样占用限流额度，不暴露账号是否存在。

本模块不依赖 fastapi，main.py 只做路由层调用。
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from . import db, secureconfig, timeutil

# ---------------- 配置 ----------------
KEY_ID_SETTING = "aliyun_sms_key_id"            # 加密
KEY_SECRET_SETTING = "aliyun_sms_key_secret"    # 加密
SIGN_NAME_SETTING = "aliyun_sms_sign_name"
TEMPLATE_SETTING = "aliyun_sms_template_code"
ENABLED_SETTING = "sms_login_enabled"

ENDPOINT = "https://dysmsapi.aliyuncs.com/"
API_VERSION = "2017-05-25"
REGION_ID = "cn-hangzhou"

CODE_TTL_S = 300
PHONE_COOLDOWN_S = 60
PHONE_DAILY_MAX = 10
IP_HOURLY_MAX = 10
IP_DAILY_MAX = 30
MAX_VERIFY_FAILS = 5
_CACHE_MAX = 20_000

# 对外统一文案：手机号存在与否都一样。
SENT_MSG = "如果这个手机号已开通账号，验证码马上会发到手机上，5 分钟内有效"
VERIFY_FAIL_MSG = "验证码不对或已过期，请重新获取"

_PHONE_RE = re.compile(r"^1\d{10}$")


def normalize_phone(value) -> str:
    """去掉空格/横线/+86 前缀后必须是 11 位大陆手机号，否则返回空串。"""
    text = re.sub(r"[\s\-]", "", str(value or ""))
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("0086"):
        text = text[4:]
    return text if _PHONE_RE.match(text) else ""


def get_config() -> dict:
    return {
        "key_id": secureconfig.get_secret(KEY_ID_SETTING),
        "key_secret": secureconfig.get_secret(KEY_SECRET_SETTING),
        "sign_name": db.get_setting(SIGN_NAME_SETTING) or "",
        "template_code": db.get_setting(TEMPLATE_SETTING) or "",
        "enabled": db.get_setting(ENABLED_SETTING) == "1",
    }


def is_configured(conf: dict | None = None) -> bool:
    conf = conf if conf is not None else get_config()
    return all(str(conf.get(k) or "").strip()
               for k in ("key_id", "key_secret", "sign_name", "template_code"))


def is_enabled(conf: dict | None = None) -> bool:
    conf = conf if conf is not None else get_config()
    return bool(conf.get("enabled")) and is_configured(conf)


def public_config() -> dict:
    """后台展示用：密钥只回「已设置」，不回明文。"""
    conf = get_config()
    return {
        "enabled": bool(conf["enabled"]),
        "configured": is_configured(conf),
        "active": is_enabled(conf),
        "key_id_set": bool(conf["key_id"]),
        "key_secret_set": bool(conf["key_secret"]),
        "sign_name": conf["sign_name"],
        "template_code": conf["template_code"],
    }


def save_config(body) -> dict:
    """后台保存。密钥留空表示不改；传 clear_keys=True 清空密钥。"""
    body = body if isinstance(body, dict) else {}
    if body.get("clear_keys"):
        secureconfig.set_secret(KEY_ID_SETTING, None)
        secureconfig.set_secret(KEY_SECRET_SETTING, None)
    for field, setting in (("key_id", KEY_ID_SETTING),
                           ("key_secret", KEY_SECRET_SETTING)):
        value = str(body.get(field) or "").strip()
        if value:
            if len(value) > 200:
                raise ValueError("AccessKey 太长了，请检查是否复制错")
            secureconfig.set_secret(setting, value)
    for field, setting, limit in (("sign_name", SIGN_NAME_SETTING, 40),
                                  ("template_code", TEMPLATE_SETTING, 40)):
        if field in body:
            value = re.sub(r"[\x00-\x1f\x7f]", "", str(body.get(field) or "")).strip()
            db.set_setting(setting, value[:limit] or None)
    if "enabled" in body:
        db.set_setting(ENABLED_SETTING, "1" if body.get("enabled") else "0")
    return public_config()


# ---------------- 阿里云 RPC 签名（纯函数） ----------------
def percent_encode(value) -> str:
    """阿里云 POP 规范的 URL 编码：UTF-8，仅 A-Z a-z 0-9 - _ . ~ 不编码，
    空格编成 %20（不是 +），* 编成 %2A，~ 保持原样。"""
    return quote(str(value), safe="-_.~", encoding="utf-8")


def canonicalized_query(params: dict) -> str:
    """按参数名字典序排序后 key=value 用 & 连接（名与值都先 percent_encode）。"""
    return "&".join(
        f"{percent_encode(k)}={percent_encode(params[k])}"
        for k in sorted(params)
    )


def string_to_sign(params: dict, method: str = "GET") -> str:
    return (
        f"{method.upper()}&{percent_encode('/')}&"
        f"{percent_encode(canonicalized_query(params))}"
    )


def rpc_signature(params: dict, access_key_secret: str, method: str = "GET") -> str:
    """HMAC-SHA1(key=AccessKeySecret + "&", StringToSign) 再 Base64。"""
    digest = hmac.new(
        (str(access_key_secret) + "&").encode("utf-8"),
        string_to_sign(params, method).encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def build_send_params(conf: dict, phone: str, code: str, *,
                      nonce: str | None = None, timestamp: str | None = None) -> dict:
    """SendSms 的公共参数 + 业务参数（未含 Signature）。"""
    return {
        "AccessKeyId": conf["key_id"],
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": REGION_ID,
        "SignName": conf["sign_name"],
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": nonce or uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "TemplateCode": conf["template_code"],
        "TemplateParam": json.dumps({"code": code}, ensure_ascii=False,
                                    separators=(",", ":")),
        "Timestamp": timestamp or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "Version": API_VERSION,
    }


def signed_url(conf: dict, phone: str, code: str, **kwargs) -> str:
    params = build_send_params(conf, phone, code, **kwargs)
    signature = rpc_signature(params, conf["key_secret"], "GET")
    return (f"{ENDPOINT}?Signature={percent_encode(signature)}&"
            f"{canonicalized_query(params)}")


class SmsSendError(RuntimeError):
    """短信通道报错（只进日志，不把供应商原文回给用户）。"""


async def send_code_sms(conf: dict, phone: str, code: str, *, client=None) -> None:
    """调阿里云 SendSms；返回 Code != OK 抛 SmsSendError。"""
    import httpx
    url = signed_url(conf, phone, code)
    if client is None:
        async with httpx.AsyncClient(timeout=10) as own:
            response = await own.get(url)
    else:
        response = await client.get(url)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 200 or str(payload.get("Code") or "") != "OK":
        raise SmsSendError(
            f"aliyun sms rejected status={response.status_code} "
            f"code={str(payload.get('Code') or '')[:60]}"
        )


# ---------------- 账号查找 ----------------
def resolve_login_user(phone: str):
    """手机号 → 可登录的账号行；找不到返回 None（调用方不得据此改变对外文案）。

    先按用户名精确匹配，再看开户申请里绑定的手机号（account_apply.phone → username）。
    平台 root 账号不允许用短信登录。
    """
    phone = normalize_phone(phone)
    if not phone:
        return None
    sql = ("SELECT u.* FROM users u JOIN tenants t ON t.id=u.tenant_id "
           "WHERE u.username=? AND u.enabled=1 AND t.enabled=1 AND u.role!='root'")
    user = db.one(sql, (phone,))
    if user:
        return user
    for row in db.q(
        "SELECT username FROM account_apply WHERE phone=? AND username IS NOT NULL "
        "AND username!='' ORDER BY id DESC LIMIT 5",
        (phone,),
    ):
        user = db.one(sql, (row["username"],))
        if user:
            return user
    return None


# ---------------- 验证码与限流（进程内存） ----------------
class SmsLimitError(Exception):
    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class CodeStore:
    """验证码 + 发送限流。线程安全，缓存有界，按北京时间换日。"""

    def __init__(self, *, ttl=CODE_TTL_S, cooldown=PHONE_COOLDOWN_S,
                 phone_daily=PHONE_DAILY_MAX, ip_hourly=IP_HOURLY_MAX,
                 ip_daily=IP_DAILY_MAX, max_fails=MAX_VERIFY_FAILS,
                 max_keys=_CACHE_MAX):
        self.ttl = ttl
        self.cooldown = cooldown
        self.phone_daily = phone_daily
        self.ip_hourly = ip_hourly
        self.ip_daily = ip_daily
        self.max_fails = max_fails
        self.max_keys = max_keys
        self._codes: dict = {}      # phone -> {"code","expires","fails"}
        self._phones: dict = {}     # phone -> {"last","day","count"}
        self._ips: dict = {}        # ip -> {"hits":[ts...],"day","count"}
        self._lock = threading.Lock()

    @staticmethod
    def _now(now):
        return time.time() if now is None else float(now)

    def _trim(self, now: float):
        for phone, entry in list(self._codes.items()):
            if entry["expires"] <= now:
                self._codes.pop(phone, None)
        today = timeutil.cn_day_index(now)
        for table in (self._phones, self._ips):
            if len(table) > self.max_keys:
                for key in [k for k, v in table.items() if v.get("day") != today]:
                    table.pop(key, None)
            while len(table) > self.max_keys:
                table.pop(next(iter(table)))
        while len(self._codes) > self.max_keys:
            self._codes.pop(next(iter(self._codes)))

    def issue(self, phone: str, ip: str, now=None) -> str:
        """检查限流并生成新验证码（旧码作废）。超限抛 SmsLimitError。"""
        now = self._now(now)
        today = timeutil.cn_day_index(now)
        with self._lock:
            self._trim(now)
            ip_entry = self._ips.get(ip) or {"hits": [], "day": today, "count": 0}
            if ip_entry["day"] != today:
                ip_entry = {"hits": [], "day": today, "count": 0}
            hits = [t for t in ip_entry["hits"] if now - t < 3600]
            if len(hits) >= self.ip_hourly or ip_entry["count"] >= self.ip_daily:
                wait = 3600 - (now - hits[0]) if len(hits) >= self.ip_hourly else 3600
                raise SmsLimitError("获取验证码太频繁了，请稍后再试", wait)
            ph = self._phones.get(phone) or {"last": 0.0, "day": today, "count": 0}
            if ph["day"] != today:
                ph = {"last": ph["last"], "day": today, "count": 0}
            if now - ph["last"] < self.cooldown:
                raise SmsLimitError(
                    f"验证码已发送，请 {int(self.cooldown - (now - ph['last'])) + 1} 秒后再获取",
                    self.cooldown - (now - ph["last"]),
                )
            if ph["count"] >= self.phone_daily:
                raise SmsLimitError("这个手机号今天获取验证码次数已达上限，请明天再试或用密码登录",
                                    3600)
            code = f"{secrets.randbelow(1_000_000):06d}"
            ph.update(last=now, count=ph["count"] + 1)
            self._phones[phone] = ph
            hits.append(now)
            ip_entry.update(hits=hits, count=ip_entry["count"] + 1)
            self._ips[ip] = ip_entry
            self._codes[phone] = {"code": code, "expires": now + self.ttl, "fails": 0}
            return code

    def discard(self, phone: str):
        """短信没发出去时作废这条码（限流计数保留，防止刷接口）。"""
        with self._lock:
            self._codes.pop(phone, None)

    def verify(self, phone: str, code, now=None) -> bool:
        """常量时间比较；成功即作废（一次性），错满 max_fails 次也作废。"""
        now = self._now(now)
        submitted = str(code or "").strip()
        with self._lock:
            entry = self._codes.get(phone)
            expected = entry["code"] if entry else "000000"
            # 即使没有记录也做一次同样的比较，减少时间差异。
            matched = hmac.compare_digest(
                expected.encode("ascii"),
                submitted.encode("utf-8", "replace"),
            )
            if not entry:
                return False
            if entry["expires"] <= now:
                self._codes.pop(phone, None)
                return False
            if matched and len(submitted) == 6:
                self._codes.pop(phone, None)
                return True
            entry["fails"] += 1
            if entry["fails"] >= self.max_fails:
                self._codes.pop(phone, None)
            return False

    def clear(self):
        with self._lock:
            self._codes.clear()
            self._phones.clear()
            self._ips.clear()


CODES = CodeStore()


def request_code(phone_raw, ip: str, *, store: CodeStore = CODES, now=None):
    """发码的业务决策（不做网络 IO）：

    返回 (code, user)：user 为 None 表示手机号没有对应账号——此时照样占用限流、
    照样返回统一文案，只是不真的发短信。手机号格式不对抛 ValueError。
    """
    phone = normalize_phone(phone_raw)
    if not phone:
        raise ValueError("请填 11 位手机号")
    code = store.issue(phone, str(ip or "?"), now=now)
    user = resolve_login_user(phone)
    if not user:
        store.discard(phone)
        return None, None
    return code, user


def verify_login(phone_raw, code, *, store: CodeStore = CODES, now=None):
    """校验验证码，成功返回账号行，失败返回 None（对外只给统一文案）。"""
    phone = normalize_phone(phone_raw)
    if not phone:
        return None
    if not store.verify(phone, code, now=now):
        return None
    return resolve_login_user(phone)
