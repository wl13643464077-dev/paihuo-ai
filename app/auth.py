"""租户与账号体系(V8):企业租户 → 主账号(owner) → 副账号(member,按板块授权).

- 数据隔离:业务表带 tenant_id,请求经中间件解析会话后把租户写入 contextvar,
  业务层用 auth.tenant_id() 取当前租户;
- 会话:无状态 HMAC cookie cc_sess = "v2.<uid>.<expires>.<完整sig>";
- 板块(modules):content(内容部)/restaurant(餐饮部)/avatar(数字人)…行业部门键与
  departments/*.json 的 key 对齐,新部门入驻自动成为可分配板块;
- 角色:root(平台老板,跨租户+管理后台) / owner(企业主账号) / member(副账号)。
"""
import contextvars
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

from . import db
from .session_secret import (
    REQUIRE_SESSION_SECRET_ENV,
    SESSION_SECRET_ENV,
    validate_session_secret,
)

log = logging.getLogger("auth")

_current_user: contextvars.ContextVar = contextvars.ContextVar("user", default=None)

SESSION_DAYS = 30
BOOTSTRAP_PASSWORD_ENV = "CONTENTCREW_BOOTSTRAP_PASSWORD"
_ephemeral_session_secret: bytes | None = None
_ephemeral_secret_lock = threading.Lock()


def _secret() -> bytes:
    requirement = os.environ.get(REQUIRE_SESSION_SECRET_ENV)
    if requirement not in (None, "", "0", "1"):
        raise RuntimeError(f"{REQUIRE_SESSION_SECRET_ENV} 只能配置为 0 或 1")
    configured = os.environ.get(SESSION_SECRET_ENV)
    if configured is not None:
        try:
            return validate_session_secret(configured)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
    if requirement == "1":
        raise RuntimeError(f"{SESSION_SECRET_ENV} 未配置，生产启动已拒绝")

    # Local development and tests deliberately get a process-only key.  It is
    # stable for this process, never read from or persisted into the business
    # database, and naturally invalidates sessions after a local restart.
    global _ephemeral_session_secret
    if _ephemeral_session_secret is None:
        with _ephemeral_secret_lock:
            if _ephemeral_session_secret is None:
                _ephemeral_session_secret = secrets.token_bytes(32)
    return _ephemeral_session_secret


PBKDF2_ITERS = 200_000
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128


def password_policy_error(password: str) -> str:
    """Return a user-safe validation message, or an empty string when strong."""
    if not isinstance(password, str):
        return "密码必须是字符串"
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"密码至少 {PASSWORD_MIN_LENGTH} 位"
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"密码不能超过 {PASSWORD_MAX_LENGTH} 位"
    classes = (
        any(char.isalpha() for char in password),
        any(char.isdigit() for char in password),
        any(not char.isalnum() for char in password),
    )
    if sum(classes) < 2:
        return "密码至少包含字母、数字、符号中的两类"
    return ""


def hash_pw(pw: str) -> str:
    if not isinstance(pw, str):
        raise TypeError("password must be a string")
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), PBKDF2_ITERS)
    return f"pbkdf2:{PBKDF2_ITERS}:{salt}:{dk.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        if stored.startswith("pbkdf2:"):
            _, iters, salt, h = stored.split(":", 3)
            dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(iters))
            return hmac.compare_digest(dk.hex(), h)
        # 旧格式(V8~V19):salt:sha256(salt+pw),登录成功后由调用方升级为 pbkdf2
        salt, h = stored.split(":", 1)
        return hmac.compare_digest(hashlib.sha256((salt + pw).encode()).hexdigest(), h)
    except (ValueError, AttributeError):
        return False


def needs_rehash(stored: str) -> bool:
    return not (stored or "").startswith("pbkdf2:")


def _session_epoch(uid: int) -> int:
    try:
        return max(0, int(db.get_setting(f"session_epoch:{int(uid)}") or 0))
    except (TypeError, ValueError):
        return 0


def _session_signature(raw: str, password_hash: str, uid: int) -> str:
    """密码哈希和撤销版本参与签名，不在 cookie 中暴露任一内部值。"""
    message = f"{raw}.{password_hash}.{_session_epoch(uid)}".encode()
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def make_session(uid: int) -> str:
    user = db.one("SELECT password_hash FROM users WHERE id=? AND enabled=1", (uid,))
    if not user:
        raise ValueError("用户不存在或已停用")
    exp = int(time.time() + SESSION_DAYS * 86400)
    raw = f"v2.{uid}.{exp}"
    sig = _session_signature(raw, user["password_hash"], uid)
    return f"{raw}.{sig}"


def parse_session(token: str):
    try:
        version, uid, exp, sig = token.split(".")
        if version != "v2" or len(sig) != hashlib.sha256().digest_size * 2:
            return None
        if int(exp) < time.time():
            return None
        uid_int = int(uid)
        user = db.one("SELECT password_hash FROM users WHERE id=? AND enabled=1", (uid_int,))
        if not user:
            return None
        raw = f"{version}.{uid}.{exp}"
        if not hmac.compare_digest(
                _session_signature(raw, user["password_hash"], uid_int), sig):
            return None
        return uid_int
    except (ValueError, AttributeError, TypeError):
        return None


def revoke_sessions(uid: int):
    """让该账号所有已签发 cookie 立即失效；下一次登录会使用新版本。"""
    uid = int(uid)
    db.set_setting(f"session_epoch:{uid}", str(_session_epoch(uid) + 1))


def get_user(uid: int):
    u = db.one("SELECT * FROM users WHERE id=? AND enabled=1", (uid,))
    if u:
        u["modules"] = db.jloads(u.pop("modules_json"), [])
    return u


def set_current(user):
    _current_user.set(user)


def current():
    return _current_user.get()


def tenant_id() -> int:
    u = current()
    return u["tenant_id"] if u else 1


def is_admin() -> bool:
    u = current()
    return bool(u) and u["role"] in ("root", "owner")


def is_root() -> bool:
    u = current()
    return bool(u) and u["role"] == "root"


BASE_MODULES = ("content", "avatar", "library")


def tenant_industries() -> list:
    """当前租户被授权的行业部门 key 列表;空=不限(平台方全开)."""
    u = current()
    if not u:
        return []
    tid = u["tenant_id"]
    if tid == 1 or u["role"] == "root":
        return []  # 平台方/root 不限行业
    r = db.one("SELECT industries_json FROM tenants WHERE id=?", (tid,))
    return db.jloads((r or {}).get("industries_json"), []) or []


def dept_visible(dept_key: str) -> bool:
    """该行业部门当前租户是否可见(受租户行业授权限制)."""
    inds = tenant_industries()
    return (not inds) or dept_key in inds


def allowed(module: str) -> bool:
    """板块权限:先过租户行业限制,再过角色/成员板块授权."""
    u = current()
    if not u:
        return False
    # 行业部门:必须在租户授权的行业内
    if module not in BASE_MODULES and not dept_visible(module):
        return False
    if u["role"] in ("root", "owner"):
        return True
    return module in u["modules"]


def all_modules() -> list:
    """可分配板块 = 内置 + 本租户可见的行业部门."""
    from . import departments
    mods = [{"key": "content", "label": "🎬 内容生产部"},
            {"key": "avatar", "label": "🎥 数字人摄影棚"}]
    for d in departments.list_depts():
        if dept_visible(d["key"]):
            mods.append({"key": d["key"], "label": f"{d['emoji']} {d['name']}"})
    mods.append({"key": "library", "label": "🗂️ 资产库/沉淀库"})
    return mods


def all_industries() -> list:
    """全部行业部门(供注册/开租户时选择),不受当前租户限制."""
    from . import departments
    return [{"key": d["key"], "name": d["name"], "emoji": d["emoji"]}
            for d in departments.list_depts()]


def bootstrap() -> dict:
    """首次启动仅使用显式环境密钥建 root；绝不记录或持久化明文密码。"""
    # 清除旧版本可能遗留在 app_setting 中的明文临时密码。
    db.set_setting("bootstrap_pw", None)
    if db.one("SELECT id FROM users LIMIT 1"):
        return {}
    pw = os.environ.get(BOOTSTRAP_PASSWORD_ENV) or ""
    policy_error = password_policy_error(pw)
    if policy_error:
        raise RuntimeError(
            f"空数据库启动前必须设置 {BOOTSTRAP_PASSWORD_ENV}（{policy_error}）"
        )
    tid = db.insert("tenants", {"name": "老板的AI集团·总部"})
    db.insert("users", {"tenant_id": tid, "username": "boss", "password_hash": hash_pw(pw),
                        "role": "root", "modules_json": "[]", "enabled": 1})
    log.warning("bootstrap root account created from %s; password was not logged",
                BOOTSTRAP_PASSWORD_ENV)
    return {"username": "boss"}
