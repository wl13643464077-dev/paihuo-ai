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
    user = db.one(
        "SELECT u.password_hash FROM users u "
        "JOIN tenants t ON t.id=u.tenant_id "
        "WHERE u.id=? AND u.enabled=1 AND t.enabled=1",
        (uid,),
    )
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
        user = db.one(
            "SELECT u.password_hash FROM users u "
            "JOIN tenants t ON t.id=u.tenant_id "
            "WHERE u.id=? AND u.enabled=1 AND t.enabled=1",
            (uid_int,),
        )
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
    # 必须是一条原子递增。SELECT 后再 set 会在并发退出/停用时丢更新，
    # 让两次撤销之间签发的 Cookie 在第二次撤销后继续有效。
    db.execute(
        "INSERT INTO app_setting(key,value,updated_at) VALUES(?, '1', ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "value=CAST(COALESCE(app_setting.value,'0') AS INTEGER)+1,"
        "updated_at=excluded.updated_at",
        (f"session_epoch:{uid}", time.time()),
    )


def get_user(uid: int):
    u = db.one(
        "SELECT u.* FROM users u JOIN tenants t ON t.id=u.tenant_id "
        "WHERE u.id=? AND u.enabled=1 AND t.enabled=1",
        (uid,),
    )
    if u:
        u["modules"] = db.jloads(u.pop("modules_json"), [])
        raw_allowed = db.jloads(u.pop("allowed_emp_idxs_json", None), None)
        u["allowed_emp_idxs"] = (
            sorted({int(v) for v in raw_allowed if str(v).lstrip("-").isdigit()})
            if isinstance(raw_allowed, list) else None
        )
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


# 副账号职级：老板(owner)全权;总监/经理可在自己行业内给下级分配数字员工。
JOB_TITLES = ("director", "manager", "staff")
JOB_TITLE_RANK = {"director": 2, "manager": 1, "staff": 0}


def job_title() -> str:
    """member 的职级；owner/root/游客返回空串（不参与职级体系）。"""
    u = current()
    if not u or u.get("role") != "member":
        return ""
    title = str(u.get("job_title") or "staff")
    return title if title in JOB_TITLES else "staff"


def can_allocate_members() -> bool:
    """能否给团队成员分配数字员工：老板/root，或总监/经理。"""
    u = current()
    if not u:
        return False
    if u["role"] in ("root", "owner"):
        return True
    return job_title() in ("director", "manager")


def employee_allowed(emp_idx: int, dept_key: str) -> bool:
    """行业数字员工的使用权：板块授权之上再过白名单。

    owner/root 全通；member 必须行业板块在授权内，且（未设白名单=行业内
    全部可用，设了白名单=只有名单内的数字员工可见可派）。内容部流水线
    是整体板块，不做员工级白名单；tour 等非成员角色维持板块级语义。
    """
    u = current()
    if not u:
        return False
    if u["role"] in ("root", "owner"):
        return True
    if dept_key == "content" or u["role"] != "member":
        return allowed(dept_key)
    if not allowed(dept_key):
        return False
    whitelist = u.get("allowed_emp_idxs")
    if whitelist is None:
        return True
    return int(emp_idx) in whitelist


BASE_MODULES = ("content", "avatar", "library")


def tenant_industries() -> list:
    """当前租户被授权的显式行业列表；平台租户/root 单独视为全开。"""
    u = current()
    if not u:
        return []
    tid = u["tenant_id"]
    if tid == 1 or u["role"] == "root":
        return []  # 平台方/root 不限行业
    return [
        str(row["industry_key"])
        for row in db.q(
            "SELECT industry_key FROM tenant_industry WHERE tenant_id=? "
            "ORDER BY is_primary DESC,industry_key",
            (tid,),
        )
        if row.get("industry_key")
    ]


def dept_visible(dept_key: str) -> bool:
    """该行业部门当前租户是否可见(受租户行业授权限制)."""
    u = current()
    if not u:
        return False
    if u.get("tenant_id") == 1 or u.get("role") == "root":
        return True
    inds = tenant_industries()
    # 非平台租户没有显式行业绑定时 fail closed，不能再把空列表解释为全行业。
    return dept_key in inds


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
