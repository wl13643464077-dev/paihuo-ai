"""ContentCrew Web 服务:API + SSE 推送 + 静态资源."""
import asyncio
import contextvars
import ipaddress
import json
import logging
import os
import posixpath
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from urllib.parse import unquote, urlsplit

import hashlib

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import (analyzer, assetfiles, auth, billing, db, departments, employees, expertmatch,
               export, feishu, funnel, llm, meeting, obs, providers, scheduler,
               secureconfig, taskcenter, taskrunner)
from .engine import engine
from .skills import registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("main")
app = FastAPI(title="派活 PaiHuo — 老板会派活，数字员工去干活")

# 全站 55 处 raise HTTPException(404) 之类的裸状态码,默认 detail 是英文
# ("Not Found"/"Forbidden"…),前端会原样弹给老板。统一在出口翻译成人话;
# 带自定义中文 detail 的异常原样放行。
_BARE_DETAIL_CN = {
    "Not Found": "没有找到这条内容,可能已被删除或不属于当前账号",
    "Forbidden": "没有权限执行这个操作",
    "Unauthorized": "请先登录",
    "Method Not Allowed": "请求方式不对,请刷新页面后重试",
    "Bad Request": "请求内容有误,请刷新页面后重试",
}


@app.exception_handler(HTTPException)
async def _friendly_http_exception(request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, str) and detail in _BARE_DETAIL_CN:
        detail = _BARE_DETAIL_CN[detail]
    return JSONResponse({"detail": detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def _unhandled_exception(request, exc: Exception):
    """没接住的异常不能把英文 Internal Server Error 甩给老板。

    堆栈只进服务端日志;对外只说人话并给出路,绝不回显异常内容。
    """
    # 仓库保密口径:journal 只记稳定上下文+异常类型,不落原始堆栈。
    logging.getLogger("main").error(
        "unhandled error path=%s error_type=%s",
        getattr(getattr(request, "url", None), "path", "?"),
        type(exc).__name__,
    )
    return JSONResponse(
        {"detail": "系统开小差了,这一步没做成。请再试一次;"
                   "反复失败请点右下角 💬 反馈给我们,会有人跟进"},
        status_code=500,
    )

APP_NAME = "派活"
APP_SLOGAN = "老板会派活，数字员工去干活"
INDUSTRIES = ["通用", "餐饮", "科技数码", "美妆个护", "教育培训", "母婴亲子", "家居生活",
              "健康养生", "金融理财", "本地生活", "文旅出行", "服装时尚", "三农"]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUIESCENT = os.environ.get("CONTENTCREW_QUIESCENT") == "1"
VALIDATION = QUIESCENT or os.environ.get("CONTENTCREW_VALIDATION") == "1"
_AVATAR_UPLOAD_MAX_BYTES = 30 * 1024 * 1024
# Caddy keeps a 40 MB transport ceiling.  Leave room for multipart framing so
# the application and proxy advertise one honest, reachable clip limit.
_CLIP_UPLOAD_MAX_BYTES = 38 * 1024 * 1024
_UPLOAD_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_PERSISTENT_UPLOAD_RESERVED = contextvars.ContextVar(
    "persistent_upload_reserved",
    default=False,
)
_PERSISTENT_UPLOAD_ROUTES = {
    ("POST", "/api/avatar/upload"): (
        "avatar",
        "avatar",
        _AVATAR_UPLOAD_MAX_BYTES + _UPLOAD_MULTIPART_OVERHEAD_BYTES,
    ),
    ("POST", "/api/tv/clips"): (
        "clip",
        "content",
        _CLIP_UPLOAD_MAX_BYTES + _UPLOAD_MULTIPART_OVERHEAD_BYTES,
    ),
}
_TRANSIENT_UPLOAD_RESERVED = contextvars.ContextVar(
    "transient_upload_reserved",
    default=False,
)
_TRANSIENT_UPLOAD_ROUTES = {
    ("POST", "/api/parse-file"): (
        "file-parse",
        "*work",
        20 * 1024 * 1024 + _UPLOAD_MULTIPART_OVERHEAD_BYTES,
    ),
    ("POST", "/api/tools/menu-copy"): (
        "menu-copy",
        "content",
        8 * 1024 * 1024 + _UPLOAD_MULTIPART_OVERHEAD_BYTES,
    ),
    ("POST", "/api/tools/product-shot"): (
        "product-shot",
        "content",
        8 * 1024 * 1024 + _UPLOAD_MULTIPART_OVERHEAD_BYTES,
    ),
    ("POST", "/api/tools/photo-factory"): (
        "photo-factory",
        "content",
        8 * 1024 * 1024 + _UPLOAD_MULTIPART_OVERHEAD_BYTES,
    ),
}


def _upload_permission_allowed(module: str) -> bool:
    """Resolve upload permissions before multipart parsing starts."""
    if module != "*work":
        return auth.allowed(module)
    user = auth.current() or {}
    if user.get("role") in ("root", "owner"):
        return True
    return user.get("role") == "member" and any(
        auth.allowed(candidate)
        for candidate in (user.get("modules") or [])
    )


@app.middleware("http")
async def _metrics_mw(request: Request, call_next):
    """请求级指标:路由模板 + 状态分类 + 耗时。只记聚合,绝不记参数与正文。"""
    t0 = time.perf_counter()
    status = 500                      # 抛异常未产生响应时按 5xx 计
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        template = getattr(route, "path", None)
        if template is None:
            # 未匹配到路由:已知静态挂载各占一桶,其余任意路径一律并入
            # 单一 unmatched 桶——外部扫描器的随机路径不能撑爆聚合表。
            head = request.url.path.split("/", 2)[1] if "/" in request.url.path else ""
            template = (f"/{head}/*" if head in ("files", "static", "pub")
                        else "/_unmatched")
        obs.observe_request(
            template, status, (time.perf_counter() - t0) * 1000.0)


@app.get("/healthz", include_in_schema=False)
def healthz():
    """供反向代理/守护进程探活；响应不暴露版本、路径、配置或异常内容。"""
    try:
        row = db.one("SELECT 1 AS ok")
        if not row or row.get("ok") != 1:
            raise RuntimeError("database unavailable")
    except Exception:
        log.warning("health check failed")
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


@app.get("/api/ops/health")
async def ops_health():
    """深度健康检查(仅平台管理员):逐组件报 ok/degraded/down 与原因。

    /healthz 保持毫秒级浅探活给反向代理;这里才做会花时间的事:
    数据库写探针、异步写池往返、引擎队列、磁盘余量、最近错误率。
    """
    _need_root()
    checks: dict = {}
    overall = "ok"

    def _worse(level):
        nonlocal overall
        order = {"ok": 0, "degraded": 1, "down": 2}
        if order[level] > order[overall]:
            overall = level

    # 数据库读+写探针(经异步门面,同时验证 db 线程池活着)
    t0 = time.perf_counter()
    try:
        await db.aget_setting("_ops_probe")
        await db.aset_setting("_ops_probe", str(int(time.time())))
        checks["database"] = {
            "status": "ok",
            "roundtrip_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as exc:
        checks["database"] = {"status": "down",
                              "error_type": type(exc).__name__}
        _worse("down")

    # 引擎:工作协程是否已启动、队列是否堆积
    try:
        depth = engine.queue.qsize()
        started = engine._loop is not None
        status = "ok" if started and depth < 50 else (
            "degraded" if started else "down")
        checks["engine"] = {"status": status, "queue_depth": depth,
                            "started": started}
        _worse(status)
    except Exception as exc:
        checks["engine"] = {"status": "down", "error_type": type(exc).__name__}
        _worse("down")

    # 磁盘余量(数据目录):低于 512MB 降级、低于 128MB 视为事故
    try:
        usage = shutil.disk_usage(os.path.join(ROOT, "data"))
        free_mb = usage.free // (1024 * 1024)
        status = ("ok" if free_mb >= 512
                  else "degraded" if free_mb >= 128 else "down")
        checks["disk"] = {"status": status, "free_mb": free_mb}
        _worse(status)
    except OSError as exc:
        checks["disk"] = {"status": "down", "error_type": type(exc).__name__}
        _worse("down")

    # 最近 5 分钟错误率:>5% 降级、>20% 视为事故(样本不足 20 不判)
    recent = obs.recent_window(5)
    if recent["requests"] >= 20:
        rate = recent["error_rate"]
        status = "ok" if rate <= 0.05 else (
            "degraded" if rate <= 0.20 else "down")
        _worse(status)
    else:
        status = "ok"
    checks["traffic"] = {"status": status, **recent}

    # 供应商配置在位性(只报布尔,不报值)
    checks["providers"] = {
        "status": "ok",
        "yunwu_configured": bool(secureconfig.get_secret("yunwu_key")),
    }

    return {"status": overall, "checks": checks,
            "uptime_seconds": round(time.time() - obs._started_at, 1)}


@app.get("/api/ops/metrics")
def ops_metrics():
    """指标快照(仅平台管理员):路由延迟/错误、业务计数器、组件量规。"""
    _need_root()
    return obs.snapshot()


@app.on_event("startup")
async def _startup():
    db.conn()
    secureconfig.migrate_legacy_secrets()
    auth.bootstrap()
    # 组件量规:读取时惰性求值,记录侧零开销。
    obs.register_gauge("engine_queue_depth", lambda: engine.queue.qsize())
    obs.register_gauge("engine_job_locks", lambda: len(engine.locks))
    obs.register_gauge("sse_subscribers", lambda: len(engine.subscribers))
    obs.register_gauge(
        "db_pool_backlog",
        lambda: db._pool()._work_queue.qsize(),
    )
    recovered_asset_transactions = avatar.recover_asset_transactions(
        raise_on_blocked=True,
    )
    if recovered_asset_transactions["recovered"]:
        log.warning(
            "recovered %d durable avatar asset transactions",
            recovered_asset_transactions["recovered"],
        )
    if VALIDATION:
        # Release validation mode deliberately opens the real database so
        # migrations/imports are exercised, but must not resume work, schedule
        # tasks, start watchdogs, or make any background outbound calls.
        log.warning("candidate running without background workers")
        return
    draft_conflicts = db.jloads(
        db.get_setting("wechat_delivery_migration_conflicts"), []
    ) or []
    if draft_conflicts:
        log.error(
            "wechat delivery migration found %d tenant/job conflicts; "
            "review /api/admin/wechat-delivery-alerts before clearing them",
            len(draft_conflicts),
        )
    recovered_interventions = meeting.recover_interventions()
    if recovered_interventions:
        log.warning(
            "recovered %d interrupted meeting interventions",
            recovered_interventions,
        )
    recovered_retros = pubtrack.recover_interrupted()
    if recovered_retros:
        log.warning("recovered %d interrupted publication retros", recovered_retros)
    recovered_drafts, protected_draft_ops = _recover_wechat_deliveries()
    if recovered_drafts:
        log.warning("recovered %d interrupted wechat deliveries", recovered_drafts)
    recovered_billing = billing.recover_interrupted_operations(
        exclude_op_keys=protected_draft_ops
    )
    if recovered_billing:
        log.warning("recovered %d interrupted billed operations", recovered_billing)
    await engine.start()
    asyncio.create_task(scheduler.loop(engine))
    asyncio.create_task(analyzer.loop())
    taskrunner.resume_pending(engine.broadcast)
    avatar.resume_pending(engine.broadcast)      # 数字人:queued重开/running退点
    meeting.resume_pending(engine.broadcast)     # 圆桌会:queued重开/running标失败
    from . import textvideo as _tv
    _tv.resume_pending(engine.broadcast)         # 图文成片:queued重开/running退点
    matrixpub.resume_pending(engine.broadcast)   # 矩阵发布:中断标失败+给重试/半自动出路
    _recover_interrupted_tool_jobs()
    _ensure_tool_running_index()
    _start_tool_watchdog()


# ---------------- V8:账号会话 + 租户隔离 ----------------
PUBLIC_PATHS = ("/api/auth/login", "/login", "/static/", "/favicon")

import hmac as _hmac  # noqa: E402


def _guest_sign(gid: int) -> str:
    import hashlib as _hl
    return _hmac.new(auth._secret(), f"guest{gid}".encode(), _hl.sha256).hexdigest()


import re as _re_files  # noqa: E402


def _canonical_file_path(path: str) -> str:
    """按静态服务器解析前先拒绝歧义路径，鉴权与实际文件必须看同一个目标。"""
    decoded = unquote(path or "")
    if not decoded.startswith("/files/"):
        raise HTTPException(400, "路径非法")
    if ("\x00" in decoded or "\\" in decoded or "//" in decoded
            or any(part in (".", "..") for part in decoded.split("/"))):
        raise HTTPException(400, "路径非法")
    normalized = posixpath.normpath(decoded)
    if normalized != decoded.rstrip("/") or not normalized.startswith("/files/"):
        raise HTTPException(400, "路径非法")
    return normalized


def _file_owner_tid(path: str):
    """解析登录态文件预览的租户归属；归属不明返回 0，默认拒绝。"""
    m = _re_files.match(r"/files/avatar-public/([^/]+)$", path)
    if m:
        return avatar.asset_owner(m.group(1))
    return assetfiles.file_owner_tid(path)


@app.middleware("http")
async def _auth_mw(request: Request, call_next):
    path = request.url.path
    if QUIESCENT and path != "/healthz":
        return JSONResponse(
            {"detail": "系统升级验证中，请稍后重试"},
            status_code=503,
            headers={"Retry-After": "30"},
        )
    auth.set_current(None)
    uid = auth.parse_session(request.cookies.get("cc_sess") or "")
    user = auth.get_user(uid) if uid else None
    auth.set_current(user)
    tour = False
    if not user:
        ck = request.cookies.get("cc_guest") or ""
        try:
            gid, sig = ck.split(".")
            if sig == _guest_sign(int(gid)):
                tour = True
                auth.set_current({"id": 0, "tenant_id": -1, "username": "访客",
                                  "role": "tour", "modules": []})
        except (ValueError, AttributeError):
            pass
    TOUR_GET_OK = ("/api/meta", "/api/depts", "/api/employees", "/api/state",
                   "/api/auth/me", "/api/knowledge", "/api/billing", "/api/meetings",
                   "/api/avatar/meta", "/api/avatar/jobs", "/api/assets", "/api/schedules")
    # 游客可点开员工公开介绍；员工配置写接口继续统一拦截。
    TOUR_BLOCK = ("/api/employees/",)
    if (path.startswith("/api/") and not path.startswith("/api/auth/login")
            and not path.startswith("/api/guest/")
            and path != "/api/funnel/events"
            # 登录页「忘记密码」要在未登录时展示对客联系方式;该接口只回
            # root 主动配置、本就面向客户公开的联系字符串。
            and path != "/api/support-contact"):
        if not user and not tour:
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        if tour and any(path == b or path.startswith(b) for b in TOUR_BLOCK):
            return JSONResponse({"detail": "参观模式只能浏览员工展示页;开通账号即可浏览员工介绍并派活"},
                                status_code=403)
        if tour and path == "/api/feedback" and request.method == "POST":
            # 游客留资是唯一转化出口:套餐页明示「点右下角 💬 留联系方式」,
            # 这条必须放行,否则教了一条走不通的路。
            pass
        elif tour and not (request.method == "GET" and any(path == p or path.startswith(p + "/")
                                                           for p in TOUR_GET_OK)):
            return JSONResponse({"detail": "参观模式只能看不能动:开通账号即可派活"}, status_code=403)
    password_change_paths = {
        "/api/auth/me",
        "/api/auth/password",
        "/api/auth/logout",
    }
    if (
        user
        and user.get("must_change_password")
        and (
            path.startswith("/api/")
            or path.startswith("/files/")
        )
        and path not in password_change_paths
    ):
        return JSONResponse(
            {
                "detail": "该账号需要先设置符合新安全策略的密码",
                "code": "must_change_password",
            },
            status_code=428,
        )
    upload_key = (request.method.upper(), path)
    upload_policy = _PERSISTENT_UPLOAD_ROUTES.get(upload_key)
    upload_kind = "persistent"
    if upload_policy is None:
        upload_policy = _TRANSIENT_UPLOAD_ROUTES.get(upload_key)
        upload_kind = "transient"
    if upload_policy:
        action, module, request_limit = upload_policy
        if not _upload_permission_allowed(module):
            return JSONResponse(
                {"detail": "您的账号没有该板块权限,请联系企业主账号开通"},
                status_code=403,
            )
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"detail": "上传请求长度无效"},
                    status_code=400,
                )
            if declared_bytes < 0:
                return JSONResponse(
                    {"detail": "上传请求长度无效"},
                    status_code=400,
                )
            if declared_bytes > int(request_limit):
                return JSONResponse(
                    {"detail": "上传文件超过单次大小限制"},
                    status_code=413,
                )
        # Content-Length is only a fast rejection hint.  Count actual ASGI
        # body chunks as well so chunked/incorrectly declared requests cannot
        # make the multipart parser spool more than this route permits.
        original_receive = request._receive
        received_bytes = 0

        async def limited_receive():
            nonlocal received_bytes
            message = await original_receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body") or b"")
                if received_bytes > int(request_limit):
                    raise HTTPException(
                        413,
                        "上传文件超过单次大小限制",
                    )
            return message

        request._receive = limited_receive
        # Wrap call_next itself: permission, size and concurrency gates all
        # run before Starlette consumes/parses the multipart body.
        slot = (
            _persistent_upload_slot(action)
            if upload_kind == "persistent"
            else _transient_upload_slot(action)
        )
        reserved_context = (
            _PERSISTENT_UPLOAD_RESERVED
            if upload_kind == "persistent"
            else _TRANSIENT_UPLOAD_RESERVED
        )
        try:
            try:
                async with slot:
                    token = reserved_context.set(True)
                    try:
                        response = await call_next(request)
                        # FastAPI may translate a receive-side size exception
                        # into its generic body-parse 400.  The outer
                        # middleware still owns the unsent response, so retain
                        # the precise 413 contract when the byte counter proves
                        # the route limit was crossed.
                        if received_bytes > int(request_limit):
                            return JSONResponse(
                                {"detail": "上传文件超过单次大小限制"},
                                status_code=413,
                            )
                        return response
                    finally:
                        reserved_context.reset(token)
            except HTTPException as exc:
                return JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )
        finally:
            request._receive = original_receive
    if (path == "/" or path.startswith("/files/")) and not user and not tour:
        from fastapi.responses import RedirectResponse
        if path == "/":
            return RedirectResponse("/promo")   # 未登录先看宣传页,页内再进登录
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    if path.startswith("/files/"):   # 裸文件也要按租户归属校验,防连号枚举拉别家产物
        try:
            raw = request.scope.get("raw_path") or path.encode()
            path = _canonical_file_path(raw.decode("latin-1"))
        except (HTTPException, UnicodeDecodeError):
            return JSONResponse({"detail": "路径非法"}, status_code=400)
        owner_tid = _file_owner_tid(path)
        cur = auth.current()
        cur_tid = cur["tenant_id"] if cur else None
        if not cur or (not auth.is_root() and cur_tid != owner_tid):
            return JSONResponse({"detail": "无权访问该文件"}, status_code=403)
        # 演绎师/封面师产出的 HTML/SVG 由不可信输入链驱动生成,同源直开会让其中的
        # 脚本以登录会话调用 /api/*。用 sandbox CSP 剥夺其同源与脚本能力(iframe 预览
        # 已带 sandbox,这里堵的是"打开"式顶层导航),并禁止 MIME 嗅探。
        response = await call_next(request)
        low = path.lower()
        if low.endswith((".html", ".htm", ".svg", ".xml", ".xhtml")):
            response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    return await call_next(request)


def _need_admin():
    if not auth.is_admin():
        raise HTTPException(403, "需要主账号权限")


def _need_root():
    if not auth.is_root():
        raise HTTPException(403, "需要平台管理员权限")


def _is_boss() -> bool:
    """员工内部资料仅向唯一超级管理账号 boss 开放。"""
    u = auth.current() or {}
    return u.get("role") == "root" and u.get("username") == "boss"


def _need_boss():
    """Enforce the named super-account boundary, not merely a database role."""
    if not _is_boss():
        raise HTTPException(403, "仅超级管理账号 boss 可访问数字员工内部资料")


def _public_station(s: dict) -> dict:
    """内容部员工的对外名片：只含展示信息，不含岗位实现与模型配置。"""
    return {k: s[k] for k in ("idx", "key", "name", "dept", "emoji", "color", "intro")}


def _public_expert(e: dict) -> dict:
    """产业专家的对外名片：文字介绍优先，绝不透出岗位手册结构。"""
    intro = (e.get("intro") or "").strip()
    if not intro:
        who = f"{e.get('person', '')}，" if e.get("person") else ""
        dept_name = (e.get("dept_name") or "企业团队").strip()
        role_name = (e.get("role") or e.get("name") or "行业专家").strip()
        intro = (
            f"{who}{dept_name}的{role_name}数字员工，"
            "面向企业真实经营场景提供专业分析与决策支持。"
        )
    role = (e.get("role") or e.get("name") or "行业专家").strip()
    topic = (e.get("name") or "相关业务").strip()
    intro += (f" TA在团队中担任{role}，适合处理与“{topic}”相关的经营问题。"
              "您只需说明当前背景、目标和已有材料，TA就能围绕实际业务给出清晰、专业、可落地的建议。")
    return {k: e.get(k) for k in ("idx", "name", "emoji", "color", "person", "dept_name")} | {
        "intro": intro,
    }


def _need_module(module: str):
    if not auth.allowed(module):
        raise HTTPException(403, "您的账号没有该板块权限,请联系企业主账号开通")


def _need_any_work_module():
    if _upload_permission_allowed("*work"):
        return
    raise HTTPException(403, "您的账号没有可使用文件解析的工作板块")


def TEN() -> int:
    return auth.tenant_id()


def _pagination(limit, offset: int, legacy_limit: int) -> tuple[int, int, bool]:
    """未传 limit 时保持旧数组响应；显式传入时启用统一分页契约。"""
    if limit is None:
        if offset not in (0, None):
            raise HTTPException(422, "offset 需要与 limit 一起使用")
        return legacy_limit, 0, False
    try:
        page_limit = int(limit)
        page_offset = int(offset or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "分页参数无效") from exc
    if not 1 <= page_limit <= 100:
        raise HTTPException(422, "limit 必须在 1 到 100 之间")
    if not 0 <= page_offset <= 1_000_000:
        raise HTTPException(422, "offset 必须在 0 到 1000000 之间")
    return page_limit, page_offset, True


def _like_value(value: str, max_length: int = 100) -> str:
    raw = (value or "").strip()[:max_length]
    return "%" + raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _page_result(rows: list, total: int, limit: int, offset: int, **extra) -> dict:
    has_more = offset + len(rows) < total
    return {
        "items": rows,
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": has_more,
        "truncated": has_more,
        "next_offset": offset + limit if has_more else None,
        **extra,
    }


def _list_facets(table: str, tenant_id: int, base_where: str = "", params=()) -> dict:
    """知识/资产筛选项来自当前租户全集，不受当前页截断影响。"""
    if table not in ("knowledge", "asset"):
        return {"platforms": [], "categories": []}
    where = "tenant_id=?"
    values = [tenant_id]
    if base_where:
        where += " AND " + base_where
        values.extend(params)
    def distinct(field: str) -> list:
        rows = db.q(
            f"SELECT DISTINCT json_extract(meta_json,'$.{field}') AS value "
            f"FROM {table} WHERE {where} AND json_valid(meta_json) "
            f"AND json_type(meta_json,'$.{field}')='text' "
            "ORDER BY value LIMIT 100",
            tuple(values),
        )
        return [str(row["value"])[:40] for row in rows if row.get("value")]

    return {"platforms": distinct("platform"), "categories": distinct("category")}


import re as _re_uname  # noqa: E402


def _clean_username(raw: str) -> str:
    """用户名净化:去空白,禁掉引号/尖括号/反斜杠/控制字符(既是登录名也会进前端按钮参数,
    从源头挡住 XSS/注入),限 40 字。"""
    name = (raw or "").strip()
    if not name or len(name) > 40 or _re_uname.search(r"[\s'\"<>&\\\x00-\x1f]", name):
        raise HTTPException(400, "用户名不能含空格/引号/尖括号等特殊字符,且不超过40字")
    return name


def _charge(action: str, note: str = ""):
    try:
        billing.charge(action, note=note)
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))


def _start_billed_operation(action: str, note: str = "") -> str:
    """HTTP 入口统一使用可恢复操作账；不再靠易重复的“扣后手工退”。"""
    try:
        return billing.start_operation(action, tid=TEN(), note=note)
    except billing.InsufficientPoints as exc:
        raise HTTPException(402, str(exc)) from exc


@app.get("/api/billing")
def billing_get():
    t = db.one("SELECT * FROM tenants WHERE id=?", (TEN(),))
    price_rows = billing.prices()
    if not _is_boss():
        price_rows = {
            action: {k: row.get(k) for k in ("points", "label")}
            for action, row in price_rows.items()
        }
    log_rows = db.q("SELECT delta, balance, reason, created_at FROM billing_log "
                    "WHERE tenant_id=? ORDER BY id DESC LIMIT 300", (TEN(),))
    agg = db.one("SELECT COALESCE(SUM(CASE WHEN delta>0 THEN delta END),0) recharged, "
                 "COALESCE(-SUM(CASE WHEN delta<0 THEN delta END),0) spent, COUNT(*) n "
                 "FROM billing_log WHERE tenant_id=?", (TEN(),)) or {}
    # 近30天按动作聚合消耗:只算实际收钱的状态(charged=已扣费在跑,succeeded=已交付),
    # pending 未扣费、refunded 已退回,都不算老板真正花掉的钱。
    spend_by_action = db.q(
        "SELECT action, COUNT(*) n, COALESCE(SUM(points),0) points "
        "FROM billing_operation WHERE tenant_id=? "
        "AND status IN ('charged','succeeded') AND created_at>? "
        "GROUP BY action ORDER BY SUM(points) DESC",
        (TEN(), time.time() - 30 * 86400))
    return {"balance": (t or {}).get("balance") or 0,
            "plan": (t or {}).get("plan") or "",
            "plan_expires": (t or {}).get("plan_expires"),
            "is_platform": TEN() == 1,
            "recharged": agg.get("recharged") or 0, "spent": agg.get("spent") or 0,
            "txn_n": agg.get("n") or 0,
            "prices": price_rows, "plans": billing.PLANS,
            "periods": billing.PERIODS, "log": log_rows,
            "spend_by_action": spend_by_action}


@app.post("/api/feedback")
async def feedback_submit(body: dict):
    txt = (body.get("text") or "").strip()
    if not txt:
        raise HTTPException(400, "反馈内容不能为空")
    u = auth.current() or {}
    t = db.one("SELECT name FROM tenants WHERE id=?", (u.get("tenant_id", 1),)) or {}
    who = f"{t.get('name','')}·{u.get('username','?')}({u.get('role','')})"
    try:
        from . import mailer
        asyncio.create_task(mailer.notify_feedback(who, txt, body.get("contact", "")))
    except Exception:
        pass
    return {"ok": True}


_CLIENT_LOG_WINDOW = 60
_CLIENT_LOG_LIMIT = 20
_CLIENT_LOG_KEYS_MAX = 1000
_client_log_hits: dict = {}


def _scrub_client_log(value, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[已脱敏]", text)
    text = re.sub(r"(?i)(bearer\s+)[^\s\"',;]+", r"\1[已脱敏]", text)
    text = re.sub(
        r"(?i)((?:password|passwd|secret|token|cookie|authorization|api[_-]?key)"
        r"\s*[:=]?\s*)[^\s\"',;]+",
        r"\1[已脱敏]",
        text,
    )
    text = re.sub(r"([?&][^=\s&]{1,80}=)[^&\s]+", r"\1[已脱敏]", text)
    text = re.sub(
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[邮箱]",
        text,
    )
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]", text)
    return text[:limit]


def _client_log_label(value, default: str, limit: int = 40) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.:-]", "", str(value or ""))[:limit]
    return clean or default


def _client_error_fingerprint(body: dict, kind: str, error_name: str) -> str:
    supplied = str(body.get("fingerprint") or "").lower()
    if re.fullmatch(r"[0-9a-f]{8,64}", supplied):
        material = f"client|{kind}|{error_name}|{supplied}"
    else:
        # Backward compatibility for a browser tab that loaded an older
        # application bundle: raw text is used only in-memory to derive an
        # irreversible grouping key and is never persisted or logged.
        material = (
            f"legacy|{kind}|{error_name}|"
            f"{body.get('message') or ''}|{body.get('stack') or ''}"
        )
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:24]


def _client_category(user_agent) -> str:
    ua = str(user_agent or "").lower()
    browser = (
        "Edge" if ("edg/" in ua or "edge/" in ua)
        else "Chrome" if ("chrome/" in ua or "chromium/" in ua)
        else "Firefox" if "firefox/" in ua
        else "Safari" if ("safari/" in ua and "chrome/" not in ua)
        else "Other"
    )
    device = "Mobile" if re.search(r"mobile|android|iphone|ipad", ua) else "Desktop"
    return f"{browser}/{device}"


@app.post("/api/clientlog")
def client_log(request: Request, body: dict):
    """接收内容最小化的前端异常；只保存类型、路由和稳定指纹。"""
    user = auth.current() or {}
    tid = TEN()
    key = (tid, user.get("id") or 0)
    now = time.time()
    hits = [stamp for stamp in _client_log_hits.get(key, []) if now - stamp < _CLIENT_LOG_WINDOW]
    if len(hits) >= _CLIENT_LOG_LIMIT:
        return {"ok": True, "dropped": True}
    hits.append(now)
    _client_log_hits[key] = hits
    if len(_client_log_hits) > _CLIENT_LOG_KEYS_MAX:
        active = {
            k: [stamp for stamp in stamps if now - stamp < _CLIENT_LOG_WINDOW]
            for k, stamps in _client_log_hits.items()
        }
        active = {k: stamps for k, stamps in active.items() if stamps}
        _client_log_hits.clear()
        _client_log_hits.update(dict(list(active.items())[-_CLIENT_LOG_KEYS_MAX:]))

    path = _scrub_client_log(
        unquote(str(body.get("path") or "").split("?", 1)[0]),
        120,
    )
    fragment = _scrub_client_log(
        unquote(str(body.get("hash") or "").split("?", 1)[0]),
        160,
    )
    route = (path + fragment)[:240]
    kind = _client_log_label(body.get("kind"), "error")
    error_name = _client_log_label(body.get("error_name"), "Error")
    try:
        line = max(0, min(int(body.get("line") or 0), 10_000_000))
    except (TypeError, ValueError):
        line = 0
    fingerprint = _client_error_fingerprint(body, kind, error_name)
    summary = f"fingerprint={fingerprint};name={error_name};line={line}"
    db.insert("client_error", {
        "tenant_id": tid,
        "user_id": user.get("id"),
        "route": route,
        "kind": kind,
        "message": summary,
        "user_agent": _client_category(request.headers.get("user-agent")),
    })
    # 每个租户只保留最近 1000 条，避免客户端异常循环无限撑大主库。
    db.q(
        "DELETE FROM client_error WHERE tenant_id=? AND id NOT IN "
        "(SELECT id FROM client_error WHERE tenant_id=? ORDER BY id DESC LIMIT 1000)",
        (tid, tid),
    )
    log.warning("client error tenant=%s kind=%s route=%s", tid, kind, route)
    return {"ok": True}


_FUNNEL_PUBLIC_WINDOW = 60
_FUNNEL_PUBLIC_LIMIT = 30
_FUNNEL_PUBLIC_KEYS_MAX = 5000
_funnel_public_hits: dict[str, list[float]] = {}
_funnel_public_lock = threading.Lock()


def _funnel_public_over_limit(request: Request, now: float) -> bool:
    """Bound anonymous analytics without persisting a raw network identifier."""
    transient = (
        _client_ip(request)
        + "|"
        + (request.headers.get("user-agent") or "")[:240]
    )
    key = hashlib.sha256(transient.encode("utf-8", "replace")).hexdigest()
    with _funnel_public_lock:
        hits = [
            stamp for stamp in _funnel_public_hits.get(key, [])
            if now - stamp < _FUNNEL_PUBLIC_WINDOW
        ]
        if len(hits) >= _FUNNEL_PUBLIC_LIMIT:
            _funnel_public_hits[key] = hits
            return True
        hits.append(now)
        _funnel_public_hits[key] = hits
        if len(_funnel_public_hits) > _FUNNEL_PUBLIC_KEYS_MAX:
            active = {
                item_key: [
                    stamp for stamp in stamps
                    if now - stamp < _FUNNEL_PUBLIC_WINDOW
                ]
                for item_key, stamps in _funnel_public_hits.items()
            }
            _funnel_public_hits.clear()
            _funnel_public_hits.update({
                item_key: stamps for item_key, stamps in active.items() if stamps
            })
            while len(_funnel_public_hits) > _FUNNEL_PUBLIC_KEYS_MAX:
                _funnel_public_hits.pop(next(iter(_funnel_public_hits)))
    return False


@app.post("/api/funnel/events")
def funnel_event(request: Request, body: dict):
    """Collect only allow-listed, content-free product events."""
    event = str(body.get("event") or "")
    dimension = str(body.get("dimension") or "")
    try:
        event, dimension = funnel.normalize(event, dimension)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    user = auth.current() or {}
    if not user or user.get("role") == "tour":
        if event not in funnel.PUBLIC_EVENTS:
            raise HTTPException(403, "匿名访问只能记录公开页面事件")
        now = time.time()
        if _funnel_public_over_limit(request, now):
            return {"ok": True, "dropped": True}
        actor_key = (
            f"public:{funnel.day_key(now)}:{_client_ip(request)}:"
            f"{(request.headers.get('user-agent') or '')[:240]}"
        )
        funnel.record_safe(
            event,
            dimension,
            tenant_id=0,
            actor_key=actor_key,
            now=now,
        )
        return {"ok": True}
    if event not in funnel.CLIENT_EVENTS:
        raise HTTPException(403, "该漏斗事件只能由服务端业务动作记录")
    funnel.record_safe(
        event,
        dimension,
        tenant_id=int(user["tenant_id"]),
        actor_key=f"user:{user['id']}",
    )
    return {"ok": True}


@app.post("/api/team/tenants/{tid}/grant")
def tenant_grant(tid: int, body: dict):
    _need_root()
    pts = float(body.get("points") or 0)
    if not pts:
        raise HTTPException(400, "points 必填")
    bal = billing.grant(tid, pts, body.get("reason") or "平台充值")
    return {"balance": bal}


@app.get("/api/feishu")
def feishu_get():
    return {"configured": bool(db.get_setting("feishu_app_id")),
            "bitable": feishu.tenant_bitable()}


@app.put("/api/feishu/bitable")
def feishu_bitable(body: dict):
    _need_admin()
    url = (body.get("url") or "").strip()
    if url and not feishu.parse_app_token(url):
        raise HTTPException(400, "链接不像多维表格地址(应含 /base/xxx)")
    db.set_setting(f"feishu_bitable:{TEN()}", url or None)
    return {"ok": True}


@app.post("/api/feishu/sync")
async def feishu_sync(body: dict):
    _need_module("library")
    try:
        return await feishu.sync(body.get("kind") or "knowledge")
    except feishu.FeishuProviderError:
        raise HTTPException(502, "飞书服务暂时不可用，请稍后重试") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/support-contact")
def get_support_contact():
    """公开只读:登录页「忘记密码」等场景要在未登录时也能拿到对客联系方式。

    只回联系方式字符串本身(root 主动配置、面向客户公开的信息),不含任何其他数据。
    """
    return {"contact": (db.get_setting("support_contact") or "")[:80]}


@app.post("/api/team/support-contact")
def set_support_contact(body: dict):
    """root 配置对客联系方式(微信号/电话);套餐页与点数不足提示会展示它。"""
    _need_root()
    value = str(body.get("contact") or "").strip()[:80]
    db.set_setting("support_contact", value or None)
    return {"ok": True, "contact": value}


@app.post("/api/team/tenants/{tid}/subscribe")
def tenant_subscribe(tid: int, body: dict):
    _need_root()
    try:
        # 前端可传 order_id 做防重:同一单号重复提交只成交一次,不会二次发点。
        return billing.subscribe(tid, body.get("plan"), body.get("period"),
                                 op_key=(body.get("order_id") or "").strip() or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


# 登录限速:公网站点必须防爆破。按 IP+账号计,10次失败锁15分钟(内存态,重启即清)
_login_fails: dict = {}
_LOGIN_MAX, _LOGIN_LOCK_S = 10, 900
_LOGIN_CACHE_MAX = 5000
_login_fails_lock = threading.Lock()
_TRUSTED_PROXIES_ENV = "CONTENTCREW_TRUSTED_PROXIES"
_DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,::1/128"


def _ip_address(value: str):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _trusted_proxy_networks():
    """无效配置项按不受信处理；默认仅信任同机 Caddy 的 loopback 连接。"""
    raw = os.environ.get(_TRUSTED_PROXIES_ENV, _DEFAULT_TRUSTED_PROXIES)
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _is_trusted_proxy(address) -> bool:
    return bool(address) and any(
        address.version == network.version and address in network
        for network in _trusted_proxy_networks()
    )


def _client_ip(request: Request) -> str:
    """只从受信直接代理采用 XFF，并从右向左剥离已配置的代理跳。"""
    peer = _ip_address(request.client.host if request.client else "")
    if not peer:
        return "?"
    if not _is_trusted_proxy(peer):
        return str(peer)
    forwarded_raw = request.headers.get("x-forwarded-for") or ""
    if not forwarded_raw:
        return str(peer)
    forwarded = [_ip_address(item) for item in forwarded_raw.split(",")]
    if not forwarded or any(item is None for item in forwarded):
        return str(peer)
    for hop in reversed(forwarded):
        if not _is_trusted_proxy(hop):
            return str(hop)
    return str(forwarded[0])


def _request_is_https(request: Request) -> bool:
    """X-Forwarded-Proto 与 XFF 使用同一信任边界，直连请求不能伪造。"""
    peer = _ip_address(request.client.host if request.client else "")
    if _is_trusted_proxy(peer):
        values = [v.strip().lower()
                  for v in (request.headers.get("x-forwarded-proto") or "").split(",")
                  if v.strip()]
        if values and values[-1] in ("http", "https"):
            return values[-1] == "https"
    return request.url.scheme == "https"


def _login_throttle_key(request: Request, username: str) -> str:
    # 用户名由攻击者控制，哈希后缓存键保持固定上限。
    username_key = hashlib.sha256(username.encode("utf-8", "replace")).hexdigest()
    return f"{_client_ip(request)}|{username_key}"


def _trim_login_failures(now: float):
    """移除过期项并有界淘汰；优先保留仍在锁定期的账号，绝不整表 clear。"""
    for key, (_, until) in list(_login_fails.items()):
        if until <= now:
            _login_fails.pop(key, None)
    limit = max(1, int(_LOGIN_CACHE_MAX))
    if len(_login_fails) <= limit:
        return
    for key, (fails, until) in list(_login_fails.items()):
        if len(_login_fails) <= limit:
            break
        if fails < _LOGIN_MAX or until <= now:
            _login_fails.pop(key, None)
    while len(_login_fails) > limit:
        _login_fails.pop(next(iter(_login_fails)))


def _login_failure_state(key: str, now: float):
    with _login_fails_lock:
        _trim_login_failures(now)
        return _login_fails.get(key, (0, 0))


def _record_login_failure(key: str, now: float):
    with _login_fails_lock:
        fails, until = _login_fails.get(key, (0, 0))
        if until <= now:
            fails = 0
        _login_fails[key] = (fails + 1, now + _LOGIN_LOCK_S)
        _trim_login_failures(now)


def _clear_login_failure(key: str):
    with _login_fails_lock:
        _login_fails.pop(key, None)


@app.post("/api/auth/login")
def auth_login(body: dict, request: Request):
    username = (body.get("username") or "").strip()
    key = _login_throttle_key(request, username)
    now = time.time()
    fails, until = _login_failure_state(key, now)
    if fails >= _LOGIN_MAX and now < until:
        raise HTTPException(429, f"失败次数过多,请 {int((until - now) / 60) + 1} 分钟后再试")
    u = db.one("SELECT * FROM users WHERE username=? AND enabled=1", (username,))
    if not u or not auth.check_pw(body.get("password") or "", u["password_hash"]):
        _record_login_failure(key, time.time())
        raise HTTPException(401, "账号或密码不对")
    _clear_login_failure(key)
    t = db.one("SELECT * FROM tenants WHERE id=? AND enabled=1", (u["tenant_id"],))
    if not t:
        raise HTTPException(403, "企业已停用")
    if auth.needs_rehash(u["password_hash"]):   # 老账号透明升级到 pbkdf2
        db.update("users", u["id"], {"password_hash": auth.hash_pw(body.get("password") or "")})
    resp = JSONResponse({
        "ok": True,
        "username": u["username"],
        "role": u["role"],
        "must_change_password": bool(u.get("must_change_password")),
    })
    secure = _request_is_https(request)
    resp.set_cookie("cc_sess", auth.make_session(u["id"]),
                    max_age=auth.SESSION_DAYS * 86400, path="/",
                    httponly=True, samesite="lax", secure=secure)
    # 发布器的一次性只读账号只验证服务，不应污染真实产品漏斗。
    if not username.startswith("__release_smoke_"):
        funnel.record_safe(
            "login_success",
            "password",
            tenant_id=int(u["tenant_id"]),
            actor_key=f"user:{u['id']}",
        )
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    u = auth.current() or {}
    if u.get("id"):
        auth.revoke_sessions(u["id"])
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("cc_sess", path="/")
    return resp


@app.get("/api/auth/me")
def auth_me():
    u = auth.current()
    if u and u.get("role") == "tour":
        return {"id": 0, "username": "访客", "role": "tour", "tenant": "参观模式",
                "modules": [m["key"] for m in auth.all_modules()],
                "all_modules": auth.all_modules()}
    t = db.one("SELECT name FROM tenants WHERE id=?", (u["tenant_id"],))
    mods = [m["key"] for m in auth.all_modules()] if u["role"] in ("root", "owner") \
        else u["modules"]
    return {"id": u["id"], "username": u["username"], "role": u["role"],
            "tenant": (t or {}).get("name", ""), "modules": mods,
            "all_modules": auth.all_modules(),
            "must_change_password": bool(u.get("must_change_password"))}


@app.put("/api/auth/password")
def auth_password(body: dict):
    u = auth.current()
    row = db.one("SELECT password_hash FROM users WHERE id=?", (u["id"],))
    if not auth.check_pw(body.get("old") or "", row["password_hash"]):
        raise HTTPException(400, "旧密码不对")
    new_password = body.get("new") or ""
    policy_error = auth.password_policy_error(new_password)
    if policy_error:
        raise HTTPException(400, policy_error)
    db.update("users", u["id"], {
        "password_hash": auth.hash_pw(new_password),
        "must_change_password": 0,
    })
    db.set_setting("bootstrap_pw", None)
    return {"ok": True}


# ---------------- V8:权限管理(成员/企业/租户) ----------------
@app.get("/api/team")
def team_get():
    _need_admin()
    users = db.q("SELECT id, username, role, modules_json, enabled, created_at FROM users "
                 "WHERE tenant_id=? ORDER BY id", (TEN(),))
    for x in users:
        x["modules"] = db.jloads(x.pop("modules_json"), [])
    t = db.one("SELECT * FROM tenants WHERE id=?", (TEN(),))
    out = {"tenant": t, "users": users, "all_modules": auth.all_modules()}
    if auth.is_root():
        tenants = db.q("SELECT t.*, (SELECT COUNT(*) FROM users u WHERE u.tenant_id=t.id) n_users "
                       "FROM tenants t ORDER BY t.id")
        for x in tenants:
            x["industries"] = db.jloads(x.get("industries_json"), [])
        out["tenants"] = tenants
        out["guests"] = db.q("SELECT * FROM guests ORDER BY id DESC LIMIT 100")
        out["applies"] = db.q("SELECT * FROM account_apply ORDER BY status, id DESC LIMIT 100")
        out["all_industries"] = auth.all_industries()
    return out


def _open_account_from_apply(a: dict, trial_points: float = 0) -> dict:
    """按申请开企业账号(租户+owner+随机密码);可送体验点."""
    import re as _re
    import secrets as _sec
    base_name = _re.sub(r"\D", "", a.get("phone") or "") or f"user{a['id']}"
    username = base_name
    while db.one("SELECT id FROM users WHERE username=?", (username,)):
        username = base_name + str(_sec.randbelow(90) + 10)
    letters = "abcdefghjkmnpqrstuvwxyz"
    digits = "23456789"
    alphabet = letters + digits
    password = (
        _sec.choice(letters) + _sec.choice(digits)
        + "".join(_sec.choice(alphabet) for _ in range(14))
    )
    tname = (a.get("company") or "").strip() or f"{(a.get('name') or a.get('phone') or '客户')}的企业"
    with db.atomic():
        tid = db.insert(
            "tenants",
            {"name": tname[:30], "industries_json": "[]"},
        )
        db.insert("users", {
            "tenant_id": tid,
            "username": username,
            "password_hash": auth.hash_pw(password),
            "role": "owner",
            "modules_json": "[]",
            "enabled": 1,
            "must_change_password": 1,
        })
        if trial_points > 0:
            billing.grant(tid, trial_points, "开户体验点(自动赠送)")
        db.update(
            "account_apply",
            a["id"],
            {"status": 1, "tenant_id": tid, "username": username},
        )
    funnel.record_safe(
        "registration_complete",
        "application",
        tenant_id=tid,
        actor_key=f"lead:{a.get('phone') or a['id']}",
        unique_only=True,
    )
    return {"tenant_id": tid, "tenant_name": tname[:30], "username": username,
            "password": password,
            "notice": (f"【派活 PaiHuo】您的企业账号已开通\n"
                       f"网址:https://paihuo.ai\n账号:{username}\n初始密码:{password}\n"
                       + (f"已赠送 {trial_points:.0f} 点体验点数,登录就能派活。\n" if trial_points > 0 else "")
                       + "登录后请立即修改密码。有任何问题随时联系我们,祝生意兴隆!")}


@app.post("/api/team/applies/{aid}/approve")
def team_apply_approve(aid: int):
    """root 一键开通:申请 → 自动建企业租户+主账号+随机密码,密码只回显这一次."""
    _need_root()
    a = db.one("SELECT * FROM account_apply WHERE id=?", (aid,))
    if not a:
        raise HTTPException(404)
    if a.get("username"):
        raise HTTPException(400, f"这条申请已开通过,账号「{a['username']}」;"
                                 f"忘了密码就去该企业的成员列表重置")
    return _open_account_from_apply(a)


@app.get("/api/team/apply-config")
def apply_config_get():
    _need_root()
    return {"auto": db.get_setting("auto_approve_apply") == "1",
            "trial_points": float(db.get_setting("trial_points") or 20),
            "daily_cap": int(float(db.get_setting("auto_approve_daily_cap") or 20))}


@app.put("/api/team/apply-config")
def apply_config_put(body: dict):
    _need_root()
    db.set_setting("auto_approve_apply", "1" if body.get("auto") else "0")
    db.set_setting("trial_points", str(min(max(float(body.get("trial_points") or 20), 0), 200)))
    db.set_setting("auto_approve_daily_cap",
                   str(int(min(max(float(body.get("daily_cap") or 20), 1), 1000))))
    return {"ok": True}


@app.post("/api/team/applies/{aid}/done")
def team_apply_done(aid: int):
    _need_root()
    if not db.one("SELECT id FROM account_apply WHERE id=?", (aid,)):
        raise HTTPException(404)
    db.update("account_apply", aid, {"status": 1})
    return {"ok": True}


@app.post("/api/team/users")
def team_user_create(body: dict):
    _need_admin()
    name = _clean_username(body.get("username"))
    pw = body.get("password") or ""
    policy_error = auth.password_policy_error(pw)
    if policy_error:
        raise HTTPException(400, policy_error)
    if db.one("SELECT id FROM users WHERE username=?", (name,)):
        raise HTTPException(400, "用户名已存在")
    tid = TEN()
    if auth.is_root() and body.get("tenant_id"):
        tid = int(body["tenant_id"])
    role = "owner" if (auth.is_root() and body.get("role") == "owner") else "member"
    uid = db.insert("users", {"tenant_id": tid, "username": name,
                              "password_hash": auth.hash_pw(pw), "role": role,
                              "modules_json": json.dumps(body.get("modules") or []),
                              "enabled": 1, "must_change_password": 1})
    return {"id": uid}


@app.put("/api/team/users/{uid}")
def team_user_update(uid: int, body: dict):
    _need_admin()
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not u or (not auth.is_root() and u["tenant_id"] != TEN()):
        raise HTTPException(404)
    if u["role"] == "root" and not auth.is_root():
        raise HTTPException(403)
    data = {}
    if "modules" in body:
        data["modules_json"] = json.dumps(body["modules"] or [])
    if "enabled" in body:
        data["enabled"] = 1 if body["enabled"] else 0
    if body.get("password"):
        policy_error = auth.password_policy_error(body["password"])
        if policy_error:
            raise HTTPException(400, policy_error)
        data["password_hash"] = auth.hash_pw(body["password"])
        data["must_change_password"] = 1
    if data:
        db.update("users", uid, data)
    return {"ok": True}


@app.delete("/api/team/users/{uid}")
def team_user_delete(uid: int):
    _need_admin()
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not u or (not auth.is_root() and u["tenant_id"] != TEN()):
        raise HTTPException(404)
    if u["role"] == "root":
        raise HTTPException(403, "root 账号不可删除")
    if u["id"] == auth.current()["id"]:
        raise HTTPException(400, "不能删除自己")
    db.q("DELETE FROM users WHERE id=?", (uid,))
    return {"ok": True}


@app.put("/api/team/tenant")
def team_tenant_update(body: dict):
    _need_admin()
    name = (body.get("name") or "").strip()
    if name:
        db.update("tenants", TEN(), {"name": name})
    return {"ok": True}


@app.post("/api/team/tenants")
def team_tenant_create(body: dict):
    """root:开新企业租户 + 其主账号."""
    _need_root()
    name = (body.get("name") or "").strip()
    owner = _clean_username(body.get("owner"))
    pw = body.get("password") or ""
    if not name:
        raise HTTPException(400, "企业名必填")
    policy_error = auth.password_policy_error(pw)
    if policy_error:
        raise HTTPException(400, policy_error)
    if db.one("SELECT id FROM users WHERE username=?", (owner,)):
        raise HTTPException(400, "主账号用户名已存在")
    valid_ind = {d["key"] for d in auth.all_industries()}
    inds = [x for x in (body.get("industries") or []) if x in valid_ind]
    with db.atomic():
        tid = db.insert("tenants", {
            "name": name,
            "industries_json": json.dumps(inds, ensure_ascii=False),
        })
        db.insert("users", {
            "tenant_id": tid,
            "username": owner,
            "password_hash": auth.hash_pw(pw),
            "role": "owner",
            "modules_json": "[]",
            "enabled": 1,
            "must_change_password": 1,
        })
    funnel.record_safe(
        "registration_complete",
        "direct_admin",
        tenant_id=tid,
        actor_key=f"tenant:{tid}",
        unique_only=True,
    )
    return {"tenant_id": tid}


@app.put("/api/team/tenants/{tid}/industries")
def team_tenant_industries(tid: int, body: dict):
    _need_root()
    valid = {d["key"] for d in auth.all_industries()}
    inds = [x for x in (body.get("industries") or []) if x in valid]
    db.update("tenants", tid, {"industries_json": json.dumps(inds, ensure_ascii=False)})
    return {"ok": True}


@app.put("/api/team/tenants/{tid}")
def team_tenant_toggle(tid: int, body: dict):
    _need_root()
    if not db.one("SELECT id FROM tenants WHERE id=?", (tid,)):
        raise HTTPException(404)
    if "enabled" in body:
        db.update("tenants", tid, {"enabled": 1 if body["enabled"] else 0})
    if body.get("name"):
        db.update("tenants", tid, {"name": body["name"]})
    return {"ok": True}


# ---------------- V4:设置中心(API key / 口令 / 数据储存) ----------------
SECRET_SETTINGS = (
    "yunwu_key",
    "heygen_key",
    "feishu_app_secret",
    "smtp_authcode",
    "runninghub_key",
)
PLAIN_SETTINGS = ("yunwu_base", "default_text_model", "default_image_model",
                  "avatar_engine", "heygen_voice_id", "feishu_app_id", "smtp_user", "lead_email", "runninghub_workflow", "runninghub_quality")


def _dir_size(path):
    total = 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


@app.get("/api/settings")
def settings_get():
    _need_root()
    data_dir = os.path.join(ROOT, "data")
    counts = {t: db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
              for t in ("job", "station_run", "knowledge", "schedule", "asset")}
    skills_n = sum(len(db.jloads(r["skills_json"], []))
                   for r in db.q("SELECT skills_json FROM employee_config"))
    return {
        "storage": {
            # 测试、staging 和恢复演练会通过 CONTENTCREW_DB_PATH 使用独立
            # 数据库；这里必须报告正在使用的库，不能依赖源码目录里恰好残留
            # 一份 runtime DB。
            "db_bytes": _file_size(os.path.abspath(db.DB_PATH)),
            "assets_bytes": _dir_size(os.path.join(data_dir, "assets")),
            "data_dir": os.path.abspath(data_dir),
            "jobs": counts["job"], "station_runs": counts["station_run"],
            "knowledge": counts["knowledge"], "schedules": counts["schedule"],
            "assets": counts["asset"], "skills": skills_n,
        },
    }


@app.put("/api/settings")
def settings_put(body: dict):
    _need_root()
    if "default_text_model" in body:
        from . import providers
        model = body.get("default_text_model")
        if model not in (None, "") and not providers.text_model_available(model):
            raise HTTPException(400, "未知或不可用的文本模型")
    if "default_image_model" in body:
        from . import providers
        model = body.get("default_image_model")
        if model not in (None, "") and not providers.image_model_available(model):
            raise HTTPException(400, "未知或不可用的生图模型")
    for k in SECRET_SETTINGS:
        if k in body:
            v = (body.get(k) or "").strip()
            secureconfig.set_secret(k, v or None)
    for k in PLAIN_SETTINGS:
        if k in body:
            db.set_setting(k, (body[k] or "").strip() or None)
    return {"ok": True}


# ---------------- V6:管理者后台 ----------------
@app.get("/api/admin/overview")
def admin_overview():
    _need_boss()
    from . import providers

    def mask(v):
        return (v[:5] + "…" + v[-4:]) if v and len(v) > 12 else ("已设置" if v else "")

    def emp_row(idx, name, dept, web_required=False):
        cfg = employees.get_config(idx)
        r = db.one("SELECT model_text, model_image FROM employee_config WHERE idx=?", (idx,)) or {}
        saved_text_model = r.get("model_text")
        saved_image_model = r.get("model_image")
        return {"idx": idx, "name": name, "dept": dept, "web_required": web_required,
                "is_custom": bool(cfg["prompt_template"]),
                "enabled": employees.is_enabled(idx),
                "core": idx < 100,
                "skills_n": len(cfg["skills"]),
                "model_text": (saved_text_model
                               if providers.text_model_available(saved_text_model)
                               else ""),
                "model_image": (saved_image_model
                                if providers.image_model_available(saved_image_model)
                                else "")}

    rows = []
    for s in registry.STATIONS:
        rows.append(emp_row(s["idx"], s["name"], "内容生产部",
                            web_required=s["key"] in ("trend", "research", "benchmark")))
    for d in departments.list_depts():
        for e in d["employees"]:
            rows.append(emp_row(e["idx"], e["name"], d["name"]))
    return {"provider": {"yunwu_base": db.get_setting("yunwu_base") or "https://yunwu.ai",
                         "yunwu_key": mask(secureconfig.get_secret("yunwu_key"))},
            "avatar": {"engine_active": avatar.engine_name(),
                       "engine_forced": db.get_setting("avatar_engine") or "",
                       "heygen_key": mask(secureconfig.get_secret("heygen_key"))},
            "feishu": {"app_id": db.get_setting("feishu_app_id") or "",
                       "secret_set": bool(secureconfig.get_secret("feishu_app_secret"))},
            "mail": {"smtp_user": db.get_setting("smtp_user") or "914521033@qq.com",
                     "lead_email": db.get_setting("lead_email") or "914521033@qq.com",
                     "authcode_set": bool(secureconfig.get_secret("smtp_authcode"))},
            "routing": {"default_text_model": providers.default_text_model(),
                        "default_image_model": providers.default_image_model()},
            "text_models": providers.TEXT_MODELS, "image_models": providers.IMAGE_MODELS,
            "image_capable": [5, 6], "employees": rows}


@app.get("/api/admin/funnel")
def admin_funnel(days: int = 30):
    _need_boss()
    return funnel.report(days)


@app.put("/api/admin/employees/{idx}/models")
def admin_emp_models(idx: int, body: dict):
    _need_boss()
    from . import providers
    if not _is_emp(idx):
        raise HTTPException(404)
    mt, mi = body.get("model_text"), body.get("model_image")
    if ("model_text" in body and mt not in (None, "")
            and not providers.text_model_available(mt)):
        raise HTTPException(400, "未知或不可用的文本模型")
    if ("model_image" in body and mi not in (None, "")
            and not providers.image_model_available(mi)):
        raise HTTPException(400, "未知或不可用的生图模型")
    employees.set_models(idx, mt if "model_text" in body else None,
                         mi if "model_image" in body else None)
    return {"ok": True}


@app.put("/api/admin/employees/{idx}/enabled")
def admin_emp_enabled(idx: int, body: dict):
    _need_boss()
    if not _is_emp(idx):
        raise HTTPException(404)
    if idx < 100:
        raise HTTPException(400, "内容部工位是流水线必备,不能停用")
    employees.set_enabled(idx, bool(body.get("enabled")))
    return {"ok": True}


@app.get("/api/admin/employees/{idx}/detail")
def admin_emp_detail(idx: int):
    """后台员工全档案:技能/能力/统计/开关."""
    _need_boss()
    if not _is_emp(idx):
        raise HTTPException(404)
    cfg = employees.get_config(idx)
    if idx in registry.BY_IDX:
        s = registry.BY_IDX[idx]
        info = {"name": s["name"], "dept": s["dept"], "duty": s["duty"], "core": True}
        caps = registry.capabilities_for(idx)
        stats = db.one("SELECT COUNT(*) n, SUM(cost_usd) cost FROM station_run "
                       "WHERE station_idx=? AND status IN ('done','awaiting_review')", (idx,)) or {}
    else:
        e = departments.get(idx)
        info = {"name": f"{e.get('person','')}·{e['name']}", "dept": e["dept_name"],
                "duty": e["duty"], "core": False}
        caps = departments.capabilities_for(idx, cfg.get("caps_off"))
        stats = db.one("SELECT COUNT(*) n, SUM(cost_usd) cost FROM task "
                       "WHERE emp_idx=? AND status='done'", (idx,)) or {}
    return {**info, "idx": idx, "enabled": employees.is_enabled(idx),
            "skills": cfg["skills"], "learned_at": cfg["learned_at"],
            "learning": idx in employees.LEARNING,
            "capabilities": caps,
            "stats": {"runs": stats.get("n", 0), "cost_usd": stats.get("cost") or 0}}


@app.get("/api/admin/employees/{idx}/prompt")
def admin_emp_prompt(idx: int):
    _need_boss()
    if not _is_emp(idx):
        raise HTTPException(404)
    cfg = employees.get_config(idx)
    if idx in registry.BY_IDX:
        s = registry.BY_IDX[idx]
        return {"idx": idx, "name": s["name"],
                "default_template": registry.DEFAULT_PROMPTS[s["key"]],
                "placeholders": registry.PLACEHOLDERS[s["key"]],
                "prompt_template": cfg["prompt_template"]}
    e = departments.get(idx)
    return {"idx": idx, "name": e["name"],
            "default_template": "(专家默认提示词由岗位手册+任务书自动拼装;在此填写即完全接管,"
                                "支持 {direction} {industry} {material} 占位符)",
            "placeholders": {"direction": "任务内容", "industry": "行业/业态",
                             "material": "补充材料"},
            "prompt_template": cfg["prompt_template"]}


# ---------------- 元数据 ----------------
@app.get("/api/meta")
def meta():
    loaded_departments = departments.list_depts()
    if _is_boss():
        stations = []
        for station in registry.STATIONS:
            item = {k: v for k, v in station.items() if k != "run"}
            item["model"] = providers.text_model_for(station["idx"])
            stations.append(item)
    else:
        stations = [_public_station(s) for s in registry.STATIONS]
    result = {"app": {"name": APP_NAME, "slogan": APP_SLOGAN},
              "industries": INDUSTRIES,
              "departments_loaded": len(loaded_departments),
              "department_employees_loaded": sum(
                  len(d.get("employees") or []) for d in loaded_departments),
              "stations": stations,
              "modes": {"fullauto": "完全托管", "autopilot": "全自动", "copilot": "关键审批",
                        "manual": "逐站审批"},
              # 老板派活前必须能看到这单要花多少点(明码标价);价格可被 root 调整,
              # 所以从价目表读,不许前端写死。
              "job_points": (billing.prices().get("content_job") or {}).get("points", 18),
              # 对客联系方式(root 配置):没有它,「点数不足→看套餐→联系顾问」是死胡同。
              "support_contact": (db.get_setting("support_contact") or "")[:80],
              "brief_templates": ["蹭热点", "日更选题", "产品软文", "观点输出", "教程干货", "二创改写"],
              "platforms": list(registry.PLATFORM_SPECS),
              "platform_specs": registry.PLATFORM_SPECS,
              "image_modes": [{"key": "ai", "label": "🎨 AI生成"},
                              {"key": "real", "label": "📷 真实图·全网抓取"},
                              {"key": "mix", "label": "🎭 真实+AI混合"}],
              "mp_themes": mplayout.theme_list()}  # mplayout 在下方 V24 段导入,调用时已就绪
    if _is_boss():
        result |= {"channel_catalog": registry.CHANNEL_CATALOG,
                   "default_channels": registry.DEFAULT_CHANNELS,
                   "default_dimensions": registry.DEFAULT_DIMENSIONS}
    return result


# ---------------- 工单 ----------------
@app.get("/api/state")
def state(limit: int | None = None, offset: int = 0):
    page_limit, page_offset, paged = _pagination(limit, offset, 100)
    can_content = auth.allowed("content")
    jobs_total = (
        int((db.one(
            "SELECT COUNT(*) AS n FROM job "
            "WHERE tenant_id=? AND deleted_at IS NULL",
            (TEN(),),
        ) or {}).get("n") or 0)
        if can_content and paged else 0
    )
    jobs = (db.q(
        "SELECT id,brief_json,profile_id,mode,status,current_idx,source_schedule_id,"
        "cost_usd,tokens,created_at,updated_at FROM job "
        "WHERE tenant_id=? AND deleted_at IS NULL "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (TEN(), page_limit, page_offset),
    ) if can_content else [])
    # 每单的 10 工位最新状态(工作台迷你流水线可视化)
    stn = {}
    job_ids = [j["id"] for j in jobs]
    if job_ids:
        marks = ",".join("?" for _ in job_ids)
        for r in db.q(
                "SELECT sr.job_id,sr.station_idx,sr.status,sr.version "
                "FROM station_run sr JOIN job j ON j.id=sr.job_id "
                f"WHERE j.tenant_id=? AND sr.job_id IN ({marks}) "
                "ORDER BY sr.job_id,sr.station_idx,sr.version",
                (TEN(), *job_ids)):
            stn.setdefault(r["job_id"], {})[r["station_idx"]] = r["status"]
    for j in jobs:
        brief = db.jloads(j.pop("brief_json"), {}) or {}
        j["brief"] = {k: brief.get(k) for k in ("direction", "template", "platforms")}
        j["title"] = engine._job_title(j["id"])
        j["stations"] = [stn.get(j["id"], {}).get(i) for i in range(10)]
    profiles = (db.q("SELECT * FROM account_profile WHERE tenant_id=? "
                     "AND deleted_at IS NULL ORDER BY id", (TEN(),))
                if can_content else [])
    for p in profiles:
        persona = db.jloads(p.pop("persona_json")) or {}
        # 历史作品语料可能数十万字；办公室总览只发摘要，编辑时再按 ID 获取全文。
        corpus = persona.pop("corpus", "")
        p["persona"] = persona
        p["has_corpus"] = bool(corpus)
    inbox = [j for j in jobs if j["status"] in ("awaiting_review", "gate_blocked", "failed")]
    notifications = db.q(
        "SELECT id,kind,title,body,link,created_at "
        "FROM notification WHERE tenant_id=? AND read_at IS NULL "
        "ORDER BY id DESC LIMIT 30",
        (TEN(),),
    )
    # V26:余额+新手引导进度(首页头卡与开工清单用)
    ten_row = db.one("SELECT balance, plan FROM tenants WHERE id=?", (TEN(),)) or {}
    from . import wechat as _wc, notify as _nt
    setup = {"profile": len(profiles) > 0,
             "first_job": (jobs_total > 0 if paged else len(jobs) > 0),
             "wechat": bool(_wc.get_conf(TEN()).get("appid")) or bool(_nt.get_webhook(TEN())),
             "clone": len(avatar.cloned_voices()) > 0}
    # V27.2:「发布三件套」向导进度(成片→审查→排版/发出去,各查一条就够)
    trio = {"video": bool(db.one("SELECT id FROM tv_job WHERE tenant_id=? AND status='done' LIMIT 1", (TEN(),))),
            "censor": bool(db.one("SELECT id FROM censor_log WHERE tenant_id=? LIMIT 1", (TEN(),))),
            "publish": bool(db.one("SELECT id FROM publish_log WHERE tenant_id=? LIMIT 1", (TEN(),)))}
    result = {"jobs": jobs, "profiles": profiles, "inbox": inbox,
              "notifications": notifications,
              "balance": ten_row.get("balance") or 0,
              "plan": ten_row.get("plan") or "",
              "setup": setup, "trio": trio}
    if paged:
        page = _page_result(jobs, jobs_total, page_limit, page_offset)
        page.pop("items")
        result.update(page)
    return result


@app.post("/api/notifications/read")
def notifications_read(body: dict):
    raw_ids = body.get("ids")
    if raw_ids is None:
        changed = db.execute(
            "UPDATE notification SET read_at=?,updated_at=? "
            "WHERE tenant_id=? AND read_at IS NULL",
            (time.time(), time.time(), TEN()),
        )
        return {"ok": True, "updated": changed}
    if not isinstance(raw_ids, list) or len(raw_ids) > 100:
        raise HTTPException(400, "通知编号格式无效")
    ids = []
    for value in raw_ids:
        if isinstance(value, bool):
            raise HTTPException(400, "通知编号格式无效")
        try:
            item_id = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "通知编号格式无效") from exc
        if item_id > 0 and item_id not in ids:
            ids.append(item_id)
    if not ids:
        return {"ok": True, "updated": 0}
    marks = ",".join("?" for _ in ids)
    now = time.time()
    changed = db.execute(
        f"UPDATE notification SET read_at=?,updated_at=? "
        f"WHERE tenant_id=? AND read_at IS NULL AND id IN ({marks})",
        (now, now, TEN(), *ids),
    )
    return {"ok": True, "updated": changed}


def _profile_id_for_tenant(value):
    """把可选人设档案收敛到当前租户，杜绝跨租户 ID 引用。"""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, bool):
        raise HTTPException(400, "人设档案参数无效")
    try:
        profile_id = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "人设档案参数无效")
    if not db.one("SELECT id FROM account_profile WHERE id=? AND tenant_id=? "
                  "AND deleted_at IS NULL",
                  (profile_id, TEN())):
        raise HTTPException(400, "人设档案不存在或无权使用")
    return profile_id


_JOB_MODES = {"fullauto", "autopilot", "copilot", "manual"}


def _validated_mode(value) -> str:
    mode = str(value or "copilot").strip()
    if mode not in _JOB_MODES:
        raise HTTPException(400, "工单模式无效")
    return mode


def _validated_brief(raw: dict) -> dict:
    """把可持久化 Brief 收敛到已知字段、类型、长度和平台枚举。"""
    if not isinstance(raw, dict):
        raise HTTPException(400, "任务简报格式无效")

    field_cn = {"direction": "内容方向", "template": "内容类型",
                "industry": "行业/赛道", "material": "附加素材",
                "ref_link": "参考链接"}

    def text(key: str, limit: int, *, required: bool = False) -> str:
        value = raw.get(key, "")
        if value is None:
            value = ""
        label = field_cn.get(key, key)
        if not isinstance(value, str):
            raise HTTPException(400, f"「{label}」格式无效")
        value = value.strip()
        if required and not value:
            raise HTTPException(400, "内容方向必填")
        if len(value) > limit:
            raise HTTPException(
                400, f"「{label}」超长:最多 {limit} 字,当前 {len(value)} 字,"
                     "请删减后再提交")
        return value

    brief = {
        "direction": text("direction", 2000, required=True),
        "template": text("template", 120),
        "industry": text("industry", 120),
        "material": text("material", 20000),
    }
    ref_link = text("ref_link", 2000)
    if ref_link:
        parsed = urlsplit(ref_link)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise HTTPException(400, "参考链接必须是有效的 http/https 地址")
    brief["ref_link"] = ref_link

    platforms = raw.get("platforms") or ["小红书"]
    if not isinstance(platforms, list) or len(platforms) > len(registry.PLATFORM_SPECS):
        raise HTTPException(400, "目标平台格式无效")
    clean_platforms = []
    for value in platforms:
        if not isinstance(value, str) or value not in registry.PLATFORM_SPECS:
            raise HTTPException(400, "目标平台不在系统支持范围内")
        if value not in clean_platforms:
            clean_platforms.append(value)
    if not clean_platforms:
        raise HTTPException(400, "至少选择一个目标平台")
    brief["platforms"] = clean_platforms

    image_mode = raw.get("image_mode") or "ai"
    if image_mode not in {"ai", "real", "mix"}:
        raise HTTPException(400, "配图模式无效")
    brief["image_mode"] = image_mode
    image_count = raw.get("image_count")
    if image_count is not None:
        if isinstance(image_count, bool):
            raise HTTPException(400, "配图数量无效")
        try:
            image_count = int(image_count)
        except (TypeError, ValueError):
            raise HTTPException(400, "配图数量无效")
        if not 0 <= image_count <= 12:
            raise HTTPException(400, "配图数量必须在 0 到 12 之间")
    brief["image_count"] = image_count
    enable_deck = raw.get("enable_deck", False)
    if not isinstance(enable_deck, bool):
        raise HTTPException(400, "演绎稿开关无效")
    brief["enable_deck"] = enable_deck

    for key in ("xhs_style", "dy_style"):
        style = raw.get(key)
        if style in (None, ""):
            brief[key] = None
            continue
        if not isinstance(style, dict):
            raise HTTPException(400, f"{key} 格式无效")
        clean_style = {}
        for field in ("name", "desc"):
            value = style.get(field, "")
            if not isinstance(value, str) or len(value) > 300:
                raise HTTPException(400, f"{key}.{field} 格式无效")
            clean_style[field] = value.strip()
        brief[key] = clean_style
    return brief


def _create_charged_content_job(data: dict, note: str) -> int:
    """先持久化 pending 工单，再在同一事务内抢占并扣点。"""
    tid = int(data.get("tenant_id") or TEN())
    points = 0.0 if tid == 1 else float(
        (billing.prices().get("content_job") or {"points": 18})["points"])
    job_data = dict(data)
    job_data.update({
        "tenant_id": tid,
        "status": "pending_charge",
        "billing_status": "pending",
        "billing_points": points,
    })
    job_id = db.insert("job", job_data)

    def claim(c):
        cur = c.execute(
            "UPDATE job SET billing_status='charged',status='running',updated_at=? "
            "WHERE id=? AND billing_status='pending' AND status='pending_charge'",
            (time.time(), job_id),
        )
        return cur.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            "content_job", tid, claim, note=note, points=points)
    except billing.InsufficientPoints as e:
        db.q("DELETE FROM job WHERE id=? AND billing_status='pending'", (job_id,))
        raise HTTPException(402, str(e)) from e
    except Exception:
        db.q("DELETE FROM job WHERE id=? AND billing_status='pending'", (job_id,))
        raise
    if not charged:
        db.q("DELETE FROM job WHERE id=? AND billing_status='pending'", (job_id,))
        raise HTTPException(409, "这单刚刚已经提交过了,原单正在执行,没有重复扣点;正在带您去任务中心查看")
    return job_id


@app.post("/api/jobs")
async def create_job(body: dict):
    _need_module("content")
    brief = _validated_brief(body.get("brief"))
    running = db.q("SELECT id FROM job WHERE tenant_id=? AND deleted_at IS NULL AND "
                   "status NOT IN ('done','cancelled','failed')", (TEN(),))
    if len(running) >= 3:
        raise HTTPException(429, "并行工单已达上限(3),请先处理进行中的工单")
    profile_id = _profile_id_for_tenant(body.get("profile_id"))
    job_id = _create_charged_content_job({
        "brief_json": json.dumps(brief, ensure_ascii=False),
        "profile_id": profile_id, "tenant_id": TEN(),
        "mode": _validated_mode(body.get("mode")),
    }, note=brief["direction"][:20])
    engine.notify(job_id)
    engine.touch(job_id)
    funnel.record_first_work(TEN(), "content")
    return {"job_id": job_id}


def _job_or_404(job_id: int) -> dict:
    _need_module("content")
    j = db.one("SELECT * FROM job WHERE id=? AND deleted_at IS NULL", (job_id,))
    if not j or j.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    return j


def _public_failure_for_view(status, value, internal: bool):
    """Hide legacy/raw diagnostics while preserving human review comments.

    ``internal`` controls access to product materials, not to untrusted supplier
    response bodies.  Historical failed rows therefore remain masked for every
    role, including boss.
    """
    if str(status or "").lower() in {"failed", "error"}:
        return providers.PUBLIC_TASK_FAILURE
    return value


def _public_progress_for_view(status, value, internal: bool) -> str:
    """Non-boss progress is a state label, never a tool/query/error transcript."""
    group = str(status or "").lower()
    if group in {"failed", "error"}:
        return providers.PUBLIC_TASK_FAILURE
    if internal:
        return str(value or "")
    if group in {"done", "succeeded", "submitted"}:
        return "任务已完成"
    if group in {"cancelled", "canceled", "deleted"}:
        return "任务已取消"
    if group in {"queued", "pending", "pending_charge"}:
        return "任务已进入队列"
    return "任务正在处理"


def _public_publish_failure(status, value):
    """Legacy browser automation errors are untrusted and never replayed."""
    if str(status or "").lower() not in {"failed", "error"}:
        return value
    return {
        "kind": "unknown",
        "why": "自动发布未完成",
        "fix": "请核对平台后台，确认未重复发布后再从原任务重试",
        "err": providers.PUBLIC_TASK_FAILURE,
    }


def _serialize_station_run(row: dict, internal: bool) -> dict:
    item = dict(row)
    item["output"] = db.jloads(item.pop("output_json", None), {})
    if internal:
        item["steps"] = _steps_for_view(
            item.pop("steps_json", None),
            True,
            status=item.get("status"),
        )
        item["review_comment"] = _public_failure_for_view(
            item.get("status"),
            item.get("review_comment"),
            internal=True,
        )
        return item
    # 对外只交付结果与审核状态；技能编号、工作动作、成本/令牌和时延均属内部资料。
    public = {
        key: item.get(key)
        for key in (
            "id",
            "station_idx",
            "version",
            "status",
            "review_comment",
            "created_at",
            "updated_at",
            "output",
        )
    }
    public["review_comment"] = _public_failure_for_view(
        item.get("status"),
        item.get("review_comment"),
        internal=False,
    )
    return public


def _steps_for_view(raw, internal: bool, status=None) -> list:
    """Persisted steps follow SSE's confidentiality boundary.

    Boss can inspect normal operational steps, but failed/error records and
    explicit error steps are always reduced to stable state labels because
    legacy supplier responses may contain prompts, credentials, or stack paths.
    """
    steps = raw if isinstance(raw, list) else db.jloads(raw, [])
    steps = [step for step in (steps or []) if isinstance(step, dict)]
    terminal_failure = str(status or "").lower() in {"failed", "error"}
    if internal and not terminal_failure:
        return [
            (
                engine._public_step({
                    "k": "error",
                    "ts": step.get("ts") or step.get("t"),
                })
                if str(step.get("k") or "").lower() == "error"
                else step
            )
            for step in steps
        ]
    return [
        engine._public_step({
            "k": (
                "error"
                if terminal_failure
                else step.get("k") or "working"
            ),
            "ts": step.get("ts") or step.get("t"),
        })
        for step in steps
    ]


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int):
    j = _job_or_404(job_id)
    j["brief"] = db.jloads(j.pop("brief_json"))
    j["gate"] = db.jloads(j.pop("gate_json"), None)
    j["title"] = engine._job_title(job_id)
    runs = {}
    hist = {}
    for r in db.q("SELECT * FROM station_run WHERE job_id=? ORDER BY station_idx, version", (job_id,)):
        r = _serialize_station_run(r, _is_boss())
        runs[r["station_idx"]] = r
        hist[r["station_idx"]] = hist.get(r["station_idx"], 0) + 1
    for idx, r in runs.items():
        r["versions"] = hist[idx]
        r["needs_review"] = engine.needs_review(idx, j["mode"])
    j["runs"] = runs
    return j


@app.get("/api/jobs/{job_id}/stations/{idx}/versions")
def versions(job_id: int, idx: int):
    _job_or_404(job_id)
    rows = db.q("SELECT * FROM station_run WHERE job_id=? AND station_idx=? ORDER BY version DESC LIMIT 10",
                (job_id, idx))
    return [_serialize_station_run(r, _is_boss()) for r in rows]


@app.post("/api/jobs/{job_id}/stations/{idx}/action")
def station_action(job_id: int, idx: int, body: dict):
    job = _job_or_404(job_id)
    try:
        assetfiles.validate_embedded_file_urls(
            (body.get("payload") or {}).get("edits") or {},
            int(job.get("tenant_id") or TEN()),
            expected_job_id=job_id,
        )
    except assetfiles.AssetAccessError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        engine.user_action(job_id, idx, body.get("action"), body.get("payload"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/gate")
def gate_action(job_id: int, body: dict):
    _job_or_404(job_id)
    try:
        engine.gate_action(job_id, body.get("action"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: int):
    _job_or_404(job_id)
    try:
        # 先提交终态/退款，再 kill。即便 provider 不响应 kill，它返回时也只能
        # 看到 cancelled，不能重新把工单写回 running。
        engine.settle_cancel(job_id, f"老板取消工单 #{job_id}")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    llm.kill(f"job{job_id}:")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int):
    _need_admin()
    job = _job_or_404(job_id)
    if db.one(
        "SELECT id FROM wechat_draft_delivery "
        "WHERE tenant_id=? AND job_id=? "
        "AND status IN ('pending_charge','processing','submitting','submitted') "
        "LIMIT 1",
        (TEN(), job_id),
    ):
        raise HTTPException(
            409,
            "公众号草稿正在投递或补记台账，请先等待投递收口后再删除工单",
        )
    # 删除前先完成同一套终态结算。计费事务提交后才物理删除，崩溃时最多
    # 留下一条已取消记录，不会留下“记录没了、退款也没了”的不可恢复窗口。
    if job["status"] in (
            "pending_charge", "running", "awaiting_review",
            "gate_blocked", "paused", "cancelled"):
        try:
            engine.settle_cancel(job_id, f"删除未完成工单 #{job_id}")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    elif (job["status"] == "failed"
          and job.get("billing_status") == "charged"
          and not engine._has_usable_delivery(job_id)):
        engine.settle_failure(job_id, f"删除失败工单 #{job_id} 前补做退款")

    llm.kill(f"job{job_id}:")
    settled = db.one(
        "SELECT status,billing_status FROM job WHERE id=?", (job_id,))
    if not settled:
        return {"ok": True}
    if (settled["status"] in ("running", "awaiting_review", "gate_blocked",
                              "paused", "pending_charge")
            or (settled["status"] in ("cancelled", "failed")
                and settled["billing_status"] == "charged"
                and not engine._has_usable_delivery(job_id))):
        # 退款系统故障时保留业务锚点，禁止用 DELETE 绕过下次对账恢复。
        raise HTTPException(503, "这单的退点还在处理中(约几秒),稍等片刻再删除")

    deleted_at = time.time()
    with db.atomic() as c:
        active_delivery = c.execute(
            "SELECT id FROM wechat_draft_delivery "
            "WHERE tenant_id=? AND job_id=? "
            "AND status IN "
            "('pending_charge','processing','submitting','submitted') LIMIT 1",
            (TEN(), job_id),
        ).fetchone()
        if active_delivery:
            raise HTTPException(
                409,
                "公众号草稿投递状态刚刚发生变化，请等待收口后再删除工单",
            )
        hidden = c.execute(
            "UPDATE job SET deleted_at=?,deleted_by=?,delete_reason=?,updated_at=? "
            "WHERE id=? AND tenant_id=? AND deleted_at IS NULL "
            "AND status NOT IN ('running','awaiting_review','gate_blocked',"
            "'paused','pending_charge')",
            (
                deleted_at,
                int((auth.current() or {}).get("id") or 0),
                "用户移入回收站",
                deleted_at,
                job_id,
                TEN(),
            ),
        )
        if hidden.rowcount != 1:
            raise HTTPException(409, "工单状态刚刚发生变化，请刷新后再删除")
    lock = engine.locks.get(job_id)
    if not lock or not lock.locked():
        engine.locks.pop(job_id, None)
    engine.touch(job_id)
    return {"ok": True, "soft_deleted": True, "deleted_at": deleted_at}


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: int):
    _job_or_404(job_id)
    try:
        engine.pause(job_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: int):
    _job_or_404(job_id)
    try:
        engine.resume(job_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------------- V5:多部门 + 专家任务 ----------------
@app.get("/api/depts")
def depts_list():
    stats = {}
    for r in db.q("SELECT emp_idx, COUNT(*) n, SUM(status='running' OR status='queued') run "
                  "FROM task WHERE tenant_id=? AND deleted_at IS NULL GROUP BY emp_idx", (TEN(),)):
        stats[r["emp_idx"]] = r
    visible = [d for d in departments.list_depts() if auth.dept_visible(d["key"])]
    # 一次性批量取所有可见员工的配置,避免逐人 get_config+is_enabled 的 N+1(几百员工×2查库)
    cfgs = employees.get_configs([e["idx"] for d in visible for e in d["employees"]])
    out = []
    internal = _is_boss()
    for d in visible:
        emps = []
        for e in d["employees"]:
            cfg = cfgs.get(e["idx"]) or employees.get_config(e["idx"])
            st = stats.get(e["idx"]) or {}
            public = _public_expert({**e, "dept_name": d["name"]}) | {
                "group": e["group"], "enabled": cfg.get("enabled", True),
                "tasks_n": st.get("n", 0), "running_n": st.get("run", 0) or 0,
            }
            if internal:
                public |= {
                    "duty": e["duty"],
                    "skills_n": len([s for s in cfg["skills"] if s.get("enabled", True)]),
                    "learning": e["idx"] in employees.LEARNING,
                }
            emps.append(public)
        groups = d["groups"] if internal else [
            {k: g.get(k) for k in ("name", "emoji", "color")} for g in d["groups"]
        ]
        out.append({"key": d["key"], "name": d["name"], "emoji": d["emoji"],
                    "tagline": d.get("tagline", ""), "groups": groups, "employees": emps})
    return out


@app.get("/api/depts/emp/{idx}")
def dept_emp(idx: int):
    e = departments.get(idx)
    if not e:
        raise HTTPException(404)
    if (auth.current() or {}).get("role") != "tour":
        _need_module(e["dept_key"])
    cfg = employees.get_config(idx)
    tasks = db.q("SELECT id, status, brief_json, cost_usd, created_at FROM task "
                 "WHERE emp_idx=? AND tenant_id=? AND deleted_at IS NULL "
                 "ORDER BY id DESC LIMIT 20", (idx, TEN()))
    for t in tasks:
        t["brief"] = db.jloads(t.pop("brief_json"))
    stats = db.one("SELECT COUNT(*) n, SUM(cost_usd) cost FROM task WHERE emp_idx=? "
                   "AND status='done' AND tenant_id=? AND deleted_at IS NULL",
                   (idx, TEN())) or {}
    public = _public_expert(e) | {"tasks": tasks}
    if not _is_boss():
        return public
    return {**public,
            **{k: e[k] for k in ("duty", "desc", "group", "inputs", "steps", "deliverables")},
            "capabilities": departments.capabilities_for(idx, cfg.get("caps_off")),
            "skills": cfg["skills"], "learned_at": cfg["learned_at"],
            "learning": idx in employees.LEARNING,
            "prompt_template": cfg["prompt_template"],
            "is_custom": bool(cfg["prompt_template"]),
            "stats": {"runs": stats.get("n", 0), "cost_usd": stats.get("cost") or 0}}


def _create_charged_expert_task(task_data: dict, note: str = "") -> int:
    """先落 pending 任务，再用同一事务抢占并扣点，避免扣费后没有任务记录。"""
    tid = TEN()
    points = 0.0 if tid == 1 else float(
        (billing.prices().get("expert_task") or {"points": 1})["points"])
    task_id = db.insert("task", {
        **task_data,
        "status": "pending_charge",
        "billing_status": "pending",
        "billing_points": points,
    })

    def claim(connection):
        changed = connection.execute(
            "UPDATE task SET status='queued',billing_status='charged',updated_at=? "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (time.time(), task_id),
        )
        return changed.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            "expert_task", tid, claim, note=note, points=points)
    except Exception:
        db.q(
            "DELETE FROM task WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (task_id,),
        )
        raise
    if not charged:
        raise RuntimeError("专家任务计费状态冲突")
    return task_id


@app.post("/api/tasks")
async def task_create(body: dict):
    idx = body.get("emp_idx")
    if not departments.get(idx) and idx not in registry.BY_IDX:
        raise HTTPException(404, "员工不存在")
    try:
        brief = taskrunner.normalize_brief(body.get("brief"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if departments.get(idx):
        _need_module(departments.get(idx)["dept_key"])
    else:
        _need_module("content")
    if not employees.is_enabled(idx):
        raise HTTPException(400, "该员工已被停用(后台可重新启用)")
    # 派单预检:仅产业部专家(idx>=100)且未强制派单时,先判断任务书是否对口。
    # 不对口→直接返回引导,不扣点不建任务;LLM 异常/超时一律放行(降级可用,绝不拦死派单)。
    if isinstance(idx, int) and idx >= 100 and departments.get(idx) and not body.get("force"):
        try:
            async with _free_ai_slot("task-preflight"):
                pf = await expertmatch.preflight_fit(
                    idx, brief.get("direction") or ""
                )
        except Exception as exc:                 # noqa: BLE001 —— 兜底:预检出错也放行
            logging.getLogger("main").warning(
                "派单预检异常,放行 error_type=%s",
                type(exc).__name__,
            )
            pf = {"fit": True}
        if not pf.get("fit", True):
            return {"mismatch": True, "why": pf.get("why", ""),
                    "suggestions": pf.get("suggestions", [])}
    # 来源只能由服务器内部流程写入，不能相信客户端自报的会议 ID。
    task_data = {"emp_idx": idx, "tenant_id": TEN(),
                 "brief_json": json.dumps(brief, ensure_ascii=False)}
    try:
        tid = _create_charged_expert_task(
            task_data, note=(brief.get("direction") or "")[:20])
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    asyncio.create_task(taskrunner.run_task(tid, engine.broadcast))
    funnel.record_first_work(TEN(), "expert")
    return {"task_id": tid}


# AI 选人是免费的平台 LLM 调用,按租户日限,防脚本刷平台钱(内存态,同 _apply_ips 风格)
_match_uses: dict = {}          # "tid|日序号" -> 次数
_MATCH_DAILY = 60


def _match_over_limit(tid: int) -> bool:
    today = int(time.time() // 86400)
    key = f"{tid}|{today}"
    cnt = _match_uses.get(key, 0) + 1
    _match_uses[key] = cnt
    if len(_match_uses) > 5000:   # 只清过期日,不动今天的配额
        for k in [k for k in _match_uses if not k.endswith(f"|{today}")]:
            _match_uses.pop(k, None)
    return cnt > _MATCH_DAILY


@app.post("/api/experts/match")
async def experts_match(body: dict):
    """大白话找专家:一句话描述任务,AI 从租户可见的产业部专家里挑最对口的前 3 名。
    登录即可用、免费不扣点(按租户日限防刷);可选 dept_key 限定只在某部门内匹配。"""
    if not auth.current():
        raise HTTPException(401)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "先用一句话说说要办什么活")
    if _match_over_limit(TEN()):
        return {"picks": [], "msg": "今天的 AI 选人次数用完了,明天再来;您也可以直接点下面的专家卡片派活"}
    dept_key = (body.get("dept_key") or "").strip() or None
    if dept_key and not auth.dept_visible(dept_key):
        dept_key = None
    async with _free_ai_slot("expert-match"):
        picks = await expertmatch.match_experts(
            text, TEN(), dept_key=dept_key
        )
    return {"picks": picks}


@app.get("/api/task-center")
def task_center(limit: int = 300, offset: int = 0):
    """老板任务总账：聚合真实业务表，不复制状态，也不跨租户/越板块展示。"""
    modules = {m["key"] for m in auth.all_modules() if auth.allowed(m["key"])}
    return taskcenter.list_items(TEN(), modules, limit=limit, offset=offset)


@app.get("/api/task-center/{kind}/{rid}")
def task_center_record(kind: str, rid: int):
    """非专家任务的记录级详情，保证任务中心点开的就是那一条，而不是泛工作台。"""
    if rid < 1:
        raise HTTPException(404)
    module = "avatar" if kind == "avatar" else "content"
    if kind not in {"avatar", "video", "tool", "publish"} or not auth.allowed(module):
        raise HTTPException(404)
    table = {"avatar": "avatar_job", "video": "tv_job",
             "tool": "tool_job", "publish": "pub_task"}[kind]
    deleted_clause = " AND deleted_at IS NULL" if kind == "avatar" else ""
    row = db.one(
        f"SELECT * FROM {table} WHERE id=? AND tenant_id=?{deleted_clause}",
        (rid, TEN()),
    )
    if not row:
        raise HTTPException(404)
    detail = {"kind": kind, "id": rid, "status": row.get("status") or "queued",
              "created_at": row.get("created_at") or 0,
              "updated_at": row.get("updated_at") or row.get("created_at") or 0,
              "steps": _steps_for_view(
                  row.get("steps_json"), _is_boss(), status=row.get("status")
              ), "source": None,
              "workbench_route": {"avatar": "#/avatar", "video": "#/tools",
                                    "tool": "#/tools", "publish": "#/channels"}[kind]}
    if kind == "avatar":
        params = db.jloads(row.get("params_json"), {})
        retries = int(row.get("retry_count") or 0)
        detail |= {"title": (params.get("prompt") or params.get("script") or "数字人视频")[:160],
                   "assignee": "数字人摄影棚", "params": params,
                   "output_url": row.get("video_file") or "",
                   "error": _public_failure_for_view(
                       row.get("status"), row.get("error"), _is_boss()) or "",
                   "retryable": bool(
                       row.get("status") == "failed"
                       and row.get("billing_status") in {"refunded", "included"}
                       and retries < avatar.MAX_FREE_RETRIES
                   ),
                   "free_retries_remaining": max(
                       0, avatar.MAX_FREE_RETRIES - retries
                   )}
    elif kind == "video":
        params = db.jloads(row.get("params_json"), {})
        params.pop("body", None)
        detail["steps"] = [
            {"k": s.get("k") or "tool", "l": s.get("msg") or s.get("l") or "处理中",
             "ts": s.get("t") or s.get("ts") or row.get("updated_at") or 0}
            for s in detail["steps"] if isinstance(s, dict)
        ]
        linked = row.get("job_id")
        linked_ok = bool(
            linked
            and db.one(
                "SELECT id FROM job WHERE id=? AND tenant_id=? "
                "AND deleted_at IS NULL",
                (linked, TEN()),
            )
        )
        detail |= {"title": (params.get("title") or params.get("script") or "图文成片")[:160],
                   "assignee": "视频工厂", "params": params,
                   "output_url": row.get("video_file") or "",
                   "error": _public_failure_for_view(
                       row.get("status"), row.get("error"), _is_boss()) or "",
                   "source": ({"label": f"内容工单 #{linked}", "route": f"#/job/{linked}"}
                              if linked_ok else None)}
    elif kind == "tool":
        params = db.jloads(row.get("params_json"), {})
        name = taskcenter.TOOL_NAMES.get(row.get("kind"), row.get("kind") or "营销工具")
        detail |= {"title": name, "assignee": name, "tool_kind": row.get("kind") or "",
                   "params": params,
                   "result": db.jloads(row.get("result_json"), None),
                   "progress": _public_progress_for_view(
                       row.get("status"), row.get("progress"), _is_boss()),
                   "error": _public_failure_for_view(
                       row.get("status"), row.get("error"), _is_boss()) or ""}
    else:
        payload = db.jloads(row.get("payload_json"), {})
        payload.pop("images", None)
        payload.pop("video", None)
        linked = payload.get("job_id")
        linked_ok = bool(
            linked
            and db.one(
                "SELECT id FROM job WHERE id=? AND tenant_id=? "
                "AND deleted_at IS NULL",
                (linked, TEN()),
            )
        )
        detail |= {"title": (payload.get("title") or f"发布到 {row.get('platform') or '平台'}")[:160],
                   "assignee": row.get("platform") or "矩阵发布", "params": payload,
                   "log": _public_progress_for_view(
                       row.get("status"), row.get("log"), _is_boss()),
                   "fail": _public_publish_failure(
                       row.get("status"),
                       db.jloads(row.get("fail_json"), None),
                   ),
                   "source": ({"label": f"内容工单 #{linked}", "route": f"#/job/{linked}"}
                              if linked_ok else None)}
    retry = taskcenter.retry_meta(kind, row)
    if (
        kind == "tool"
        and retry["retryable"]
        and db.one(
            "SELECT id FROM tool_job WHERE tenant_id=? AND kind=? "
            "AND id!=? AND status IN ('pending_charge','running') LIMIT 1",
            (TEN(), row.get("kind"), rid),
        )
    ):
        retry["retryable"] = False
        retry["retry_block_reason"] = (
            "同一工具已有任务正在运行，等它完成后再重试。"
        )
    detail.update(retry)
    return detail


def _raise_retry_denied(meta: dict):
    status_code = 429 if (
        not meta.get("free_retries_remaining")
        and meta.get("retry_block_reason")
    ) else 409
    raise HTTPException(
        status_code,
        meta.get("retry_block_reason") or "这个任务不能原样重试(可能素材已变或已有新版本),刷新后按最新状态处理;需要重做请重新派活。",
    )


@app.post("/api/task-center/{kind}/{rid}/retry")
async def task_center_retry(kind: str, rid: int):
    """失败任务统一免费重试入口。

    所有可重试类型都复用原记录、零点续跑并用 tenant-scoped CAS 抢占；涉及
    微信或真实平台的不确定提交一律拒绝自动重放。
    """
    if rid < 1:
        raise HTTPException(404)
    if kind == "expert":
        return await task_retry(rid)
    if kind == "avatar":
        return await avatar_job_retry(rid)
    if kind not in {"content", "video", "tool", "meeting", "publish", "wechat"}:
        raise HTTPException(404)
    # 圆桌可能完全由某个行业部门的员工组成；它的授权契约由
    # _meeting_row_or_404 按全部参会成员板块判定。其余这些工作台类型仍归内容部。
    if kind != "meeting":
        _need_module("content")
    now = time.time()

    if kind == "content":
        row = _job_or_404(rid)
        usable = engine._has_usable_delivery(rid)
        meta = taskcenter.retry_meta(
            kind, row, usable_delivery=usable)
        if not meta["retryable"]:
            _raise_retry_denied(meta)
        retries = int(row.get("retry_count") or 0)
        next_points = (
            row.get("billing_points")
            if row.get("billing_status") == "charged"
            else 0
        )
        with db.atomic() as connection:
            if connection.execute(
                "SELECT id FROM wechat_draft_delivery "
                "WHERE tenant_id=? AND job_id=? AND status IN "
                "('pending_charge','processing','submitting','submitted') "
                "LIMIT 1",
                (TEN(), rid),
            ).fetchone():
                raise HTTPException(
                    409,
                    "该工单的公众号草稿仍在投递或对账，"
                    "收口前不能重跑内容。",
                )
            changed = connection.execute(
                "UPDATE job SET status='running',billing_status='charged',"
                "billing_points=?,retry_count=retry_count+1,gate_json=NULL,"
                "updated_at=? WHERE id=? AND tenant_id=? AND deleted_at IS NULL "
                "AND status='failed' AND billing_status=? AND retry_count=?",
                (
                    next_points,
                    now,
                    rid,
                    TEN(),
                    row.get("billing_status"),
                    retries,
                ),
            )
            if changed.rowcount != 1:
                raise HTTPException(
                    409, "这个任务已经不在失败状态了——多半是刚刚已被重试(正在排队执行)或已被删除。刷新看最新进度即可,不会重复扣点")
            # 仅复位每个工位的最新失败版本；已完成版本继续作为流水线交接物。
            connection.execute(
                "UPDATE station_run SET status='rejected',"
                "review_comment='免费重试：沿用原工单重新执行',updated_at=? "
                "WHERE job_id=? AND status='failed' AND NOT EXISTS ("
                "SELECT 1 FROM station_run newer "
                "WHERE newer.job_id=station_run.job_id "
                "AND newer.station_idx=station_run.station_idx "
                "AND newer.version>station_run.version)",
                (now, rid),
            )
        engine.notify(rid)
        engine.touch(rid)
        return {
            "ok": True, "kind": kind, "job_id": rid, "record_id": rid,
            "free_retry": True, "retry_count": retries + 1,
        }

    if kind == "video":
        row = db.one(
            "SELECT * FROM tv_job WHERE id=? AND tenant_id=?", (rid, TEN()))
        if not row:
            raise HTTPException(404)
        meta = taskcenter.retry_meta(kind, row)
        if not meta["retryable"]:
            _raise_retry_denied(meta)
        retries = int(row.get("retry_count") or 0)
        changed = db.execute(
            "UPDATE tv_job SET status='queued',billing_status='charged',"
            "billing_points=0,retry_count=retry_count+1,steps_json='[]',"
            "script=NULL,video_file=NULL,error=NULL,updated_at=? "
            "WHERE id=? AND tenant_id=? AND status='failed' "
            "AND billing_status=? AND retry_count=?",
            (
                now, rid, TEN(), row.get("billing_status"), retries,
            ),
        )
        if changed != 1:
            raise HTTPException(
                409, "这个任务已经不在失败状态了——多半是刚刚已被重试(正在排队执行)或已被删除。刷新看最新进度即可,不会重复扣点")
        from . import textvideo
        textvideo.cleanup_job_assets(rid, row)
        asyncio.create_task(textvideo.run_job(rid, engine.broadcast))
        engine.broadcast({"type": "tv_done", "tv_id": rid, "tenant_id": TEN()})
        return {
            "ok": True, "kind": kind, "record_id": rid,
            "free_retry": True, "retry_count": retries + 1,
        }

    if kind == "tool":
        row = db.one(
            "SELECT * FROM tool_job WHERE id=? AND tenant_id=?", (rid, TEN()))
        if not row:
            raise HTTPException(404)
        meta = taskcenter.retry_meta(kind, row)
        if not meta["retryable"]:
            _raise_retry_denied(meta)
        retries = int(row.get("retry_count") or 0)
        try:
            changed = db.execute(
                "UPDATE tool_job SET status='running',billing_status='charged',"
                "billing_points=0,retry_count=retry_count+1,"
                "retry_started_at=?,result_json=NULL,error=NULL,"
                "progress='免费重试已重新排队',updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='failed' "
                "AND billing_status=? AND retry_count=?",
                (
                    now, now, rid, TEN(), row.get("billing_status"), retries,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                409, "这个工具已有任务正在运行，请等它完成后再重试") from exc
        if changed != 1:
            raise HTTPException(
                409, "这个任务已经不在失败状态了——多半是刚刚已被重试(正在排队执行)或已被删除。刷新看最新进度即可,不会重复扣点")
        _spawn_tool_worker(rid)
        _broadcast_tool(TEN(), row.get("kind") or "")
        return {
            "ok": True, "kind": kind, "record_id": rid,
            "free_retry": True, "retry_count": retries + 1,
        }

    if kind == "meeting":
        row = _meeting_row_or_404(rid)
        meta = taskcenter.retry_meta(kind, row)
        if not meta["retryable"]:
            _raise_retry_denied(meta)
        retries = int(row.get("retry_count") or 0)
        with db.atomic() as connection:
            current = connection.execute(
                "SELECT execution_task_ids_json FROM meeting "
                "WHERE id=? AND tenant_id=?",
                (rid, TEN()),
            ).fetchone()
            execution_ids = db.jloads(
                current["execution_task_ids_json"] if current else None,
                [],
            ) or []
            has_derived = bool(execution_ids) or bool(connection.execute(
                "SELECT id FROM task WHERE tenant_id=? "
                "AND source_meeting_id=? AND deleted_at IS NULL LIMIT 1",
                (TEN(), rid),
            ).fetchone())
            if has_derived:
                raise HTTPException(
                    409,
                    "会议已经生成执行任务，不能重开整场会议；"
                    "请到任务中心重试具体失败任务。",
                )
            changed = connection.execute(
                "UPDATE meeting SET status='queued',phase='queued',round_no=0,"
                "messages_json='[]',actions_json=NULL,summary_md=NULL,"
                "decision=NULL,consensus_md=NULL,next_action=NULL,"
                "proposals_json='[]',validations_json='[]',"
                "execution_task_ids_json='[]',intervention_count=0,"
                "intervention_state=NULL,intervention_op_key=NULL,"
                "intervention_snapshot_json=NULL,intervention_question=NULL,"
                "intervention_started_at=NULL,billing_status='included',"
                "retry_count=retry_count+1,updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='failed' "
                "AND phase='failed' AND billing_status=? AND retry_count=?",
                (
                    now, rid, TEN(), row.get("billing_status"), retries,
                ),
            )
            if changed.rowcount != 1:
                raise HTTPException(
                    409, "会议状态刚刚发生变化，请刷新后再重试")
        asyncio.create_task(meeting.run(rid, engine.broadcast))
        engine.broadcast({"type": "meeting_update", "meeting_id": rid})
        return {
            "ok": True, "kind": kind, "record_id": rid,
            "free_retry": True, "retry_count": retries + 1,
        }

    if kind == "publish":
        row = db.one(
            "SELECT * FROM pub_task WHERE id=? AND tenant_id=?", (rid, TEN()))
        if not row:
            raise HTTPException(404)
        meta = taskcenter.retry_meta(kind, row)
        if not meta["retryable"]:
            _raise_retry_denied(meta)
        retries = int(row.get("retry_count") or 0)
        changed = db.execute(
            "UPDATE pub_task SET status='queued',fail_json=NULL,"
            "retry_count=retry_count+1,submit_started_at=NULL,updated_at=? "
            "WHERE id=? AND tenant_id=? AND status='failed' "
            "AND submission_state='not_submitted' AND retry_count=?",
            (now, rid, TEN(), retries),
        )
        if changed != 1:
            raise HTTPException(
                409, "发布任务状态刚刚发生变化，请刷新后再重试")
        asyncio.create_task(matrixpub.run_task(rid, engine.broadcast))
        engine.broadcast({"type": "pub_update", "task_id": rid})
        return {
            "ok": True, "kind": kind, "record_id": rid,
            "free_retry": True, "retry_count": retries + 1,
            "note": "已按原发布单免费重新排队",
        }

    row = db.one(
        "SELECT * FROM wechat_draft_delivery WHERE id=? AND tenant_id=?",
        (rid, TEN()),
    )
    if not row:
        raise HTTPException(404)
    meta = taskcenter.retry_meta("wechat", row)
    _raise_retry_denied(meta)


def _task_row_or_404(tid: int) -> dict:
    t = db.one("SELECT * FROM task WHERE id=? AND deleted_at IS NULL", (tid,))
    if not t or t.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    expert = departments.get(t["emp_idx"])
    module = expert.get("dept_key") if expert else "content"
    if not auth.allowed(module):
        raise HTTPException(404)
    return t


@app.get("/api/tasks/{tid}")
def task_get(tid: int):
    t = _task_row_or_404(tid)
    t["brief"] = db.jloads(t.pop("brief_json"))
    t["steps"] = _steps_for_view(
        t.pop("steps_json"), _is_boss(), status=t.get("status")
    )
    e = departments.get(t["emp_idx"]) or registry.BY_IDX.get(t["emp_idx"]) or {}
    t["emp_name"] = e.get("name", "")
    t["dept_name"] = e.get("dept_name") or e.get("dept") or "内容生产部"
    retries = int(t.get("retry_count") or 0)
    t["free_retries_remaining"] = max(
        0, taskrunner.MAX_FREE_RETRIES - retries
    )
    t["retryable"] = bool(
        t.get("status") == "failed"
        and t.get("billing_status") in {"refunded", "included"}
        and t["free_retries_remaining"] > 0
    )
    t["output_md"] = _public_failure_for_view(
        t.get("status"),
        t.get("output_md"),
        _is_boss(),
    )
    if t.get("source_meeting_id"):
        m = db.one("SELECT question, emp_idxs_json FROM meeting WHERE id=? AND tenant_id=?",
                   (t["source_meeting_id"], TEN()))
        visible = bool(m and _meeting_visible(m))
        t["source"] = {
            "type": "meeting",
            "label": f"AI会议 #{t['source_meeting_id']}" if visible else "原会议已删除",
            "route": f"#/meetings/{t['source_meeting_id']}" if visible else "",
            "detail": m.get("question") if visible else "",
        }
    elif t.get("source_task_id"):
        parent = db.one("SELECT id FROM task WHERE id=? AND tenant_id=? "
                        "AND deleted_at IS NULL",
                        (t["source_task_id"], TEN()))
        t["source"] = {
            "type": "redo",
            "label": (f"任务 #{t['source_task_id']} 的重做" if parent
                      else "原任务已删除 · 本条为重做版本"),
            "route": f"#/tasks/{t['source_task_id']}" if parent else "",
            "detail": "根据上一版反馈重新生成",
        }
    else:
        t["source"] = {"type": "direct", "label": "员工面板·直接派活",
                       "route": "", "detail": ""}
    return t


@app.post("/api/tasks/{tid}/redo")
async def task_redo(tid: int, body: dict):
    t = _task_row_or_404(tid)
    feedback = body.get("feedback", "")
    if not isinstance(feedback, str):
        raise HTTPException(400, "feedback 格式无效")
    original = db.jloads(t["brief_json"], {})
    if not isinstance(original, dict):
        raise HTTPException(400, "原任务书格式无效")
    original["feedback"] = feedback
    original["prev_excerpt"] = (t.get("output_md") or "")[:1500]
    try:
        brief = taskrunner.normalize_brief(original)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        nid = _create_charged_expert_task(
            {"emp_idx": t["emp_idx"], "tenant_id": TEN(),
             "source_task_id": tid,
             "brief_json": json.dumps(brief, ensure_ascii=False)},
            note="重做",
        )
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    asyncio.create_task(taskrunner.run_task(nid, engine.broadcast))
    return {"task_id": nid}


@app.post("/api/tasks/{tid}/retry")
async def task_retry(tid: int):
    """失败任务原单免费重试；用 CAS 防止双击或并发重复开工。"""
    task = _task_row_or_404(tid)
    if task.get("status") != "failed":
        raise HTTPException(409, "只有失败任务可以免费重试")
    if not taskrunner.prepare_retry(tid, TEN()):
        current = db.one(
            "SELECT retry_count FROM task WHERE id=? AND tenant_id=?",
            (tid, TEN()),
        ) or {}
        if (current.get("retry_count") or 0) >= taskrunner.MAX_FREE_RETRIES:
            raise HTTPException(429, "该任务免费重试次数已用完，请新建任务")
        raise HTTPException(409, "这个任务已经不在失败状态了——多半是刚刚已被重试(正在排队执行)或已被删除。刷新看最新进度即可,不会重复扣点")
    asyncio.create_task(taskrunner.run_task(tid, engine.broadcast))
    engine.broadcast(
        {"type": "task_update", "task_id": tid, "idx": task["emp_idx"]}
    )
    row = db.one("SELECT retry_count FROM task WHERE id=?", (tid,)) or {}
    return {
        "ok": True,
        "task_id": tid,
        "free_retry": True,
        "retry_count": row.get("retry_count") or 0,
    }


@app.delete("/api/tasks/{tid}")
def task_delete(tid: int):
    _need_admin()
    task = _task_row_or_404(tid)
    llm.kill(f"task{tid}:")
    if (task.get("status") == "pending_charge"
            and task.get("billing_status") == "pending"):
        db.execute(
            "UPDATE task SET status='failed',billing_status='void',"
            "output_md=?,updated_at=? WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending' AND deleted_at IS NULL",
            ("任务在扣费前被移入回收站", time.time(), tid),
        )
    elif task.get("status") in ("queued", "running", "failed"):
        taskrunner.settle_failure(tid, "老板删除未交付任务")
    row = db.one(
        "SELECT status,billing_status FROM task WHERE id=? AND deleted_at IS NULL",
        (tid,),
    )
    if not row:
        raise HTTPException(404)
    if row["status"] not in ("done", "failed"):
        raise HTTPException(503, "这个任务的退点还在处理中(约几秒),稍等片刻再删除")
    if row["status"] == "failed" and row["billing_status"] == "charged":
        raise HTTPException(503, "任务退款尚未完成，请稍后重试删除")
    deleted_at = time.time()
    changed = db.execute(
        "UPDATE task SET deleted_at=?,deleted_by=?,delete_reason=?,updated_at=? "
        "WHERE id=? AND tenant_id=? AND deleted_at IS NULL "
        "AND status IN ('done','failed')",
        (
            deleted_at,
            int((auth.current() or {}).get("id") or 0),
            "用户移入回收站",
            deleted_at,
            tid,
            TEN(),
        ),
    )
    if changed != 1:
        raise HTTPException(409, "任务状态刚刚发生变化，请刷新后再删除")
    taskrunner.sync_meeting_delivery_for_task(tid)
    return {"ok": True, "soft_deleted": True, "deleted_at": deleted_at}


# ---------------- 数字员工(V2) ----------------
@app.get("/api/employees")
def employees_list():
    from . import providers

    internal = _is_boss()
    stats = ({r["station_idx"]: r for r in db.q(
        "SELECT sr.station_idx,COUNT(*) AS runs,SUM(sr.cost_usd) AS cost,"
        "SUM(sr.tokens) AS tokens,AVG(sr.latency_ms) AS avg_ms "
        "FROM station_run sr JOIN job j ON j.id=sr.job_id "
        "WHERE j.tenant_id=? AND j.deleted_at IS NULL "
        "AND sr.status IN ('done','awaiting_review') "
        "GROUP BY sr.station_idx",
        (TEN(),),
    )} if internal else {})
    out = []
    for s in registry.STATIONS:
        cfg = employees.get_config(s["idx"])
        st = stats.get(s["idx"]) or {}
        if not internal:
            out.append(_public_station(s))
            continue
        model_id = providers.text_model_for(s["idx"])
        out.append({
            **{k: v for k, v in s.items() if k != "run"},
            "model": model_id,
            "model_id": model_id,
            "prompt_template": cfg["prompt_template"],
            "is_custom": bool(cfg["prompt_template"]),
            "default_template": registry.DEFAULT_PROMPTS[s["key"]],
            "placeholders": registry.PLACEHOLDERS[s["key"]],
            "skills": cfg["skills"], "learned_at": cfg["learned_at"],
            "capabilities": registry.capabilities_for(s["idx"]),
            "settings": registry.station_settings(s["key"]),
            "settings_custom": bool(cfg["settings"]),
            "learning": s["idx"] in employees.LEARNING,
            "stats": {"runs": st.get("runs", 0), "cost_usd": st.get("cost") or 0,
                      "tokens": st.get("tokens") or 0, "avg_ms": st.get("avg_ms") or 0},
        })
    return out


def _is_emp(idx: int) -> bool:
    return idx in registry.BY_IDX or departments.get(idx) is not None


@app.put("/api/employees/{idx}/prompt")
def employee_prompt(idx: int, body: dict):
    _need_boss()
    if not _is_emp(idx):
        raise HTTPException(404)
    employees.set_prompt(idx, body.get("template"))
    return {"ok": True, "is_custom": bool((body.get("template") or "").strip())}


@app.put("/api/employees/{idx}/skills")
def employee_skills(idx: int, body: dict):
    _need_boss()   # employee_config 是全平台共享表，仅命名超级账号可改。
    if not _is_emp(idx):
        raise HTTPException(404)
    skills = body.get("skills")
    if not isinstance(skills, list):
        raise HTTPException(400, "skills 必须是数组")
    if not auth.is_root():
        # 非 root 客户端拿到的技能卡没有 source(脱敏),整表回存时把库里原有来源补回来
        old_src = {(s.get("title"), s.get("detail")): s.get("source")
                   for s in employees.get_config(idx)["skills"]}
        for s in skills:
            if "source" not in s:
                src = old_src.get((s.get("title"), s.get("detail")))
                if src is not None:
                    s["source"] = src
    employees.set_skills(idx, skills)
    return {"ok": True}


@app.put("/api/employees/{idx}/settings")
def employee_settings(idx: int, body: dict):
    _need_boss()   # 全局共享配置,仅命名超级账号
    """员工工作配置(V3):趋势官/情报员检索渠道、拆解师对标与维度。传 {} 恢复默认."""
    if idx not in registry.BY_IDX:
        raise HTTPException(404)
    settings = body.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(400, "settings 必须是对象")
    employees.set_settings(idx, settings)
    return {"ok": True, "settings": registry.station_settings(registry.BY_IDX[idx]["key"])}


@app.put("/api/employees/{idx}/capabilities")
def employee_capabilities(idx: int, body: dict):
    _need_boss()   # 全局共享配置,仅命名超级账号
    """员工能力开关(V4):body.caps_off = 停用的能力名列表."""
    if not _is_emp(idx):
        raise HTTPException(404)
    caps_off = body.get("caps_off")
    if not isinstance(caps_off, list):
        raise HTTPException(400, "caps_off 必须是数组")
    if idx in registry.BY_IDX:
        valid = {c["name"] for c in registry.CAPABILITIES.get(registry.BY_IDX[idx]["key"], [])}
        caps = None
    else:
        valid = {c["name"] for c in departments.capabilities_for(idx, [])}
        caps = None
    employees.set_caps_off(idx, [c for c in caps_off if c in valid])
    caps = (registry.capabilities_for(idx) if idx in registry.BY_IDX
            else departments.capabilities_for(idx, employees.get_config(idx)["caps_off"]))
    return {"ok": True, "capabilities": caps}


@app.post("/api/employees/{idx}/learn")
async def employee_learn(idx: int):
    _need_boss()   # 进修会覆写全平台共享的员工技能库,只有 boss 能发起
    s = registry.BY_IDX.get(idx) or (departments.get(idx) and departments.learn_station(idx))
    if not s:
        raise HTTPException(404)
    if idx in employees.LEARNING:
        raise HTTPException(429, "该员工正在进修中")
    try:
        billing_op = billing.start_operation(
            "learn", tid=TEN(), note=f"{s.get('name', '数字员工')}进修")
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))

    tid = TEN()
    emp_name = s.get("name", "数字员工")

    async def _bg():
        from . import notify
        try:
            result = await employees.learn(s, broadcast=engine.broadcast)
        except BaseException as exc:
            try:
                billing.fail_operation(billing_op, "员工进修失败自动退回")
            except Exception as refund_exc:
                logging.getLogger("employees").error(
                    "employee %s learning refund failed error_type=%s",
                    idx,
                    type(refund_exc).__name__,
                )
            logging.getLogger("employees").error(
                "employee %s learn failed error_type=%s",
                idx,
                type(exc).__name__,
            )
            # 后台失败不能只写服务端日志:老板盯着面板等结果,必须站内告知。
            try:
                notify.push(tid, "learn_failed", {"title": emp_name})
            except Exception:
                logging.getLogger("employees").warning(
                    "employee %s learn-failed notify failed", idx)
        else:
            fresh = int((result or {}).get("new") or 0)
            if fresh:
                billing.complete_operation(billing_op)
            else:
                # 一条新技能都没学到就不收钱,与全站「没产出就退点」口径一致。
                try:
                    billing.fail_operation(billing_op, "进修未学到新技能自动退回")
                except Exception:
                    logging.getLogger("employees").error(
                        "employee %s zero-fresh refund failed", idx)
            try:
                notify.push(tid, "learn_done", {
                    "title": emp_name, "new": fresh,
                    "total": (result or {}).get("total"),
                    "summary": (f"「{emp_name}」新学 {fresh} 条技能,技能库共 "
                                f"{(result or {}).get('total', '?')} 条"
                                + ("" if fresh else "(全部与已有重复,3 点已退回)")),
                })
            except Exception:
                logging.getLogger("employees").warning(
                    "employee %s learn-done notify failed", idx)

    asyncio.create_task(_bg())
    return {"ok": True, "started": True}


# ---------------- V4:知识沉淀库 ----------------
@app.get("/api/knowledge")
def knowledge_list(
    limit: int = None,
    offset: int = 0,
    q: str = "",
    platform: str = "",
    category: str = "",
):
    _need_module("library")
    page_limit, page_offset, paged = _pagination(limit, offset, 500)
    where = ["tenant_id=?", "deleted_at IS NULL"]
    params = [TEN()]
    q = (q or "").strip()
    platform = (platform or "").strip()[:40]
    category = (category or "").strip()[:40]
    if q:
        where.append(
            "(title LIKE ? ESCAPE '\\' OR tags_json LIKE ? ESCAPE '\\' "
            "OR source LIKE ? ESCAPE '\\')"
        )
        like = _like_value(q)
        params.extend((like, like, like))
    if platform:
        where.append(
            "json_valid(meta_json) AND json_extract(meta_json,'$.platform')=?"
        )
        params.append(platform)
    if category:
        where.append(
            "json_valid(meta_json) AND json_extract(meta_json,'$.category')=?"
        )
        params.append(category)
    where_sql = " AND ".join(where)
    # 列表只返回目录与评估摘要，正文按需加载，避免每次进沉淀库都传输全部长文。
    rows = db.q(
        "SELECT id, title, tags_json, source, job_id, pinned, "
        "meta_json, created_at, updated_at "
        f"FROM knowledge WHERE {where_sql} "
        "ORDER BY pinned DESC, id DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_limit, page_offset),
    )
    for r in rows:
        r["tags"] = db.jloads(r.pop("tags_json"), [])
        r["meta"] = db.jloads(r.pop("meta_json", None), None)
    if not paged and not any((q, platform, category)):
        return rows
    total = db.one(
        f"SELECT COUNT(*) AS n FROM knowledge WHERE {where_sql}", tuple(params)
    )["n"]
    return _page_result(
        rows, total, page_limit, page_offset,
        facets=_list_facets("knowledge", TEN(), "deleted_at IS NULL"),
    )


@app.get("/api/knowledge/{kid}")
def knowledge_detail(kid: int):
    _need_module("library")
    row = db.one(
        "SELECT * FROM knowledge WHERE id=? AND tenant_id=? "
        "AND deleted_at IS NULL",
        (kid, TEN()),
    )
    if not row:
        raise HTTPException(404)
    row["tags"] = db.jloads(row.pop("tags_json"), [])
    row["meta"] = db.jloads(row.pop("meta_json", None), None)
    return row


@app.post("/api/knowledge/{kid}/analyze")
async def knowledge_analyze(kid: int):
    _need_module("library")
    row = db.one(
        "SELECT tenant_id FROM knowledge WHERE id=? AND deleted_at IS NULL",
        (kid,),
    )
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    try:
        return await analyzer.analyze("knowledge", kid)
    except ValueError:
        raise HTTPException(404)


@app.post("/api/assets/{aid}/analyze")
async def asset_analyze(aid: int):
    _need_module("library")
    row = db.one("SELECT tenant_id FROM asset WHERE id=? AND deleted_at IS NULL",
                 (aid,))
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    try:
        return await analyzer.analyze("asset", aid)
    except ValueError:
        raise HTTPException(404)


@app.post("/api/knowledge")
def knowledge_create(body: dict):
    _need_module("library")
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "标题必填")
    kid = db.insert("knowledge", {
        "title": title, "tenant_id": TEN(), "content": (body.get("content") or "").strip(),
        "tags_json": json.dumps(body.get("tags") or [], ensure_ascii=False),
        "source": "manual", "pinned": 1 if body.get("pinned") else 0})
    return {"id": kid}


@app.put("/api/knowledge/{kid}")
def knowledge_update(kid: int, body: dict):
    _need_module("library")
    row = db.one(
        "SELECT tenant_id FROM knowledge WHERE id=? AND deleted_at IS NULL",
        (kid,),
    )
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    data = {}
    if "title" in body:
        data["title"] = (body["title"] or "").strip()
    if "content" in body:
        data["content"] = (body["content"] or "").strip()
    if "tags" in body:
        data["tags_json"] = json.dumps(body["tags"] or [], ensure_ascii=False)
    if "pinned" in body:
        data["pinned"] = 1 if body["pinned"] else 0
    if data:
        db.update("knowledge", kid, data)
    return {"ok": True}


@app.delete("/api/knowledge/{kid}")
def knowledge_delete(kid: int):
    _need_admin()
    _need_module("library")
    row = db.one(
        "SELECT tenant_id FROM knowledge WHERE id=? AND deleted_at IS NULL",
        (kid,),
    )
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    deleted_at = time.time()
    changed = db.execute(
        "UPDATE knowledge SET deleted_at=?,deleted_by=?,delete_reason=?,"
        "updated_at=? WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (
            deleted_at,
            int((auth.current() or {}).get("id") or 0),
            "用户移入回收站",
            deleted_at,
            kid,
            TEN(),
        ),
    )
    if changed != 1:
        raise HTTPException(409, "知识条目状态刚刚发生变化，请刷新后再删除")
    return {"ok": True, "soft_deleted": True, "deleted_at": deleted_at}


# ---------------- V42:可恢复回收站 ----------------
_TRASH_TABLES = {
    "job": ("job", "content"),
    "task": ("task", None),
    "knowledge": ("knowledge", "library"),
    "avatar": ("avatar_job", "avatar"),
    "profile": ("account_profile", "content"),
    "asset": ("asset", "library"),
}


def _trash_module(kind: str, row: dict) -> str:
    if kind == "task":
        expert = departments.get(row.get("emp_idx"))
        return (expert or {}).get("dept_key") or "content"
    meta = _TRASH_TABLES.get(kind)
    return meta[1] if meta else ""


def _trash_title(kind: str, row: dict) -> str:
    if kind == "knowledge":
        return (row.get("title") or "未命名知识")[:160]
    if kind == "profile":
        return (row.get("title") or "未命名人设")[:160]
    if kind == "asset":
        payload = db.jloads(row.get("params_json"), {}) or {}
        return (payload.get("title") or "未命名资产")[:160]
    if kind == "avatar":
        params = db.jloads(row.get("params_json"), {}) or {}
        return (
            params.get("prompt")
            or params.get("script")
            or "数字人视频"
        )[:160]
    brief = db.jloads(row.get("brief_json"), {}) or {}
    return (brief.get("direction") or "未命名任务")[:160]


@app.get("/api/trash")
def trash_list(limit: int = 200, offset: int = 0):
    """只向企业主账号展示本租户可恢复记录，不下发正文或内部工作资料。"""
    _need_admin()
    limit = max(1, min(int(limit or 200), 500))
    offset = max(0, min(int(offset or 0), 1_000_000))
    items = []
    rows = db.q(
        """
        SELECT * FROM (
          SELECT 'job' AS kind,id,brief_json,NULL AS title,
                 NULL AS params_json,status,NULL AS emp_idx,
                 deleted_at,created_at,delete_reason
          FROM job WHERE tenant_id=? AND deleted_at IS NOT NULL
          UNION ALL
          SELECT 'task' AS kind,id,brief_json,NULL AS title,
                 NULL AS params_json,status,emp_idx,
                 deleted_at,created_at,delete_reason
          FROM task WHERE tenant_id=? AND deleted_at IS NOT NULL
          UNION ALL
          SELECT 'knowledge' AS kind,id,NULL AS brief_json,title,
                 NULL AS params_json,'' AS status,NULL AS emp_idx,
                 deleted_at,created_at,delete_reason
          FROM knowledge WHERE tenant_id=? AND deleted_at IS NOT NULL
          UNION ALL
          SELECT 'avatar' AS kind,id,NULL AS brief_json,NULL AS title,
                 params_json,status,NULL AS emp_idx,
                 deleted_at,created_at,delete_reason
          FROM avatar_job WHERE tenant_id=? AND deleted_at IS NOT NULL
          UNION ALL
          SELECT 'profile' AS kind,id,NULL AS brief_json,name AS title,
                 NULL AS params_json,'' AS status,NULL AS emp_idx,
                 deleted_at,created_at,delete_reason
          FROM account_profile WHERE tenant_id=? AND deleted_at IS NOT NULL
          UNION ALL
          SELECT 'asset' AS kind,id,NULL AS brief_json,NULL AS title,
                 payload_json AS params_json,'' AS status,NULL AS emp_idx,
                 deleted_at,created_at,delete_reason
          FROM asset WHERE tenant_id=? AND deleted_at IS NOT NULL
        ) AS deleted_records
        ORDER BY deleted_at DESC,id DESC LIMIT ? OFFSET ?
        """,
        (TEN(), TEN(), TEN(), TEN(), TEN(), TEN(), limit + 1, offset),
    )
    truncated = len(rows) > limit
    for row in rows[:limit]:
        kind = row["kind"]
        module = _trash_module(kind, row)
        if not auth.allowed(module):
            continue
        employee = {}
        if kind == "task":
            employee = (
                departments.get(row.get("emp_idx"))
                or registry.BY_IDX.get(row.get("emp_idx"))
                or {}
            )
        items.append(
            {
                "kind": kind,
                "id": row["id"],
                "title": _trash_title(kind, row),
                "status": row.get("status") or "",
                "assignee": employee.get("name") or "",
                "deleted_at": row.get("deleted_at") or 0,
                "created_at": row.get("created_at") or 0,
                "reason": (row.get("delete_reason") or "")[:160],
            }
        )
    return {
        "items": items,
        "truncated": truncated,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if truncated else None,
    }


@app.post("/api/trash/{kind}/{rid}/restore")
def trash_restore(kind: str, rid: int):
    _need_admin()
    meta = _TRASH_TABLES.get(kind)
    if not meta or rid < 1:
        raise HTTPException(404)
    table, _ = meta
    row = db.one(
        f"SELECT * FROM {table} WHERE id=? AND tenant_id=? "
        "AND deleted_at IS NOT NULL",
        (rid, TEN()),
    )
    if not row or not auth.allowed(_trash_module(kind, row)):
        raise HTTPException(404)
    try:
        with db.atomic() as connection:
            changed = connection.execute(
                f"UPDATE {table} SET deleted_at=NULL,deleted_by=NULL,"
                "delete_reason=NULL,updated_at=? "
                "WHERE id=? AND tenant_id=? AND deleted_at IS NOT NULL",
                (time.time(), rid, TEN()),
            ).rowcount
    except sqlite3.IntegrityError as exc:
        # 已有新的定时工单/会议行动占用同一幂等键时，不能破坏唯一性。
        raise HTTPException(
            409, "该记录的来源位置已生成新任务，暂不能直接恢复"
        ) from exc
    if changed != 1:
        raise HTTPException(409, "记录状态刚刚发生变化，请刷新后再恢复")
    if kind == "task":
        taskrunner.sync_meeting_delivery_for_task(rid)
        engine.broadcast(
            {
                "type": "task_update",
                "task_id": rid,
                "idx": row.get("emp_idx"),
            }
        )
    elif kind == "job":
        engine.touch(rid)
        engine.broadcast({"type": "job_update", "job_id": rid})
    elif kind == "avatar":
        engine.broadcast({"type": "avatar_update", "job_id": rid})
    return {"ok": True, "restored": True, "kind": kind, "id": rid}


# 进行中状态理论上进不了回收站,但硬删是不可逆操作,这里仍然逐类防御。
# knowledge 表没有 status 列,天然不受此限制。
_TRASH_ACTIVE_STATUS = {
    "job": ("pending_charge", "queued", "running",
            "awaiting_review", "gate_blocked", "paused"),
    "task": ("pending_charge", "queued", "running"),
    "avatar": ("pending_charge", "queued", "running"),
}


def _purge_local_files(kind: str, rid: int) -> tuple[int, int]:
    """按记录种类由服务端自行推导交付文件位置并销毁。

    绝不接受任何客户端路径:job 的产物固定在 data/assets/job{id}/,
    数字人成片固定在 data/assets/avatar/avatar_{id}.mp4。
    返回 (成功删除的文件数, 删除失败的文件数),失败不阻塞数据库硬删。
    """
    removed = failed = 0
    if kind == "job":
        job_dir = os.path.join(assetfiles.ASSET_ROOT, f"job{rid}")
        if os.path.isdir(job_dir):
            count = sum(len(names) for _, _, names in os.walk(job_dir))
            try:
                shutil.rmtree(job_dir)
                removed += count
            except OSError:
                # 以磁盘上残留的文件数如实上报,方便运维手工收尾
                failed += max(
                    1,
                    sum(len(names) for _, _, names in os.walk(job_dir)),
                )
    elif kind == "avatar":
        clip = os.path.join(
            assetfiles.ASSET_ROOT, "avatar", f"avatar_{rid}.mp4")
        if os.path.isfile(clip):
            try:
                os.remove(clip)
                removed += 1
            except OSError:
                failed += 1
    return removed, failed


@app.post("/api/trash/{kind}/{rid}/purge")
def trash_purge(kind: str, rid: int):
    """合规出路:彻底删除回收站记录并连带销毁交付文件。

    账目锚点(billing_log/billing_operation)必须留存,不做任何改动。
    """
    _need_admin()
    meta = _TRASH_TABLES.get(kind)
    if not meta or rid < 1:
        raise HTTPException(404)
    table, _ = meta
    row = db.one(
        f"SELECT * FROM {table} WHERE id=? AND tenant_id=? "
        "AND deleted_at IS NOT NULL",
        (rid, TEN()),
    )
    if not row or not auth.allowed(_trash_module(kind, row)):
        raise HTTPException(404)
    active = _TRASH_ACTIVE_STATUS.get(kind, ())
    if (row.get("status") or "") in active:
        raise HTTPException(409, "该记录仍在进行中，请先等它收口再彻底删除")
    guard = ""
    args = [rid, TEN()]
    if active:
        # 与读到的快照之间可能有并发状态变化,DELETE 里再校验一次
        guard = (
            " AND status NOT IN (%s)" % ",".join("?" * len(active))
        )
        args.extend(active)
    with db.atomic() as connection:
        changed = connection.execute(
            f"DELETE FROM {table} WHERE id=? AND tenant_id=? "
            f"AND deleted_at IS NOT NULL{guard}",
            args,
        ).rowcount
        if changed != 1:
            raise HTTPException(409, "记录状态刚刚发生变化，请刷新后再删除")
        if kind == "job":
            # 工单的工位产出(含正文/图片引用)一并硬删,不能留孤儿行
            connection.execute(
                "DELETE FROM station_run WHERE job_id=?", (rid,))
    files_removed, files_failed = _purge_local_files(kind, rid)
    return {
        "ok": True,
        "purged": True,
        "kind": kind,
        "id": rid,
        "files_removed": files_removed,
        "files_failed": files_failed,
    }


# ---------------- V22:企业档案(品牌知识 → 提炼 → 注入每个数字员工) ----------------
_COMPANY_FIELDS = ("brand", "business", "audience", "tone", "selling_points", "taboo", "keywords")


@app.get("/api/company")
def company_get():
    _need_admin()
    tid = TEN()
    prof = db.jloads(db.get_setting(f"company_profile:{tid}"), {}) or {}
    filled = sum(1 for k in _COMPANY_FIELDS if str(prof.get(k) or "").strip())
    return {"materials": db.get_setting(f"company_materials:{tid}") or "",
            "profile": prof,
            "injected": filled > 0,
            "filled": filled,
            "total_fields": len(_COMPANY_FIELDS)}


@app.put("/api/company")
def company_put(body: dict):
    """保存企业原始资料,或手动微调已提炼的档案字段."""
    _need_admin()
    tid = TEN()
    result = {"ok": True}
    if "materials" in body:
        raw = (body.get("materials") or "").strip()
        clipped = raw[:20000]
        db.set_setting(f"company_materials:{tid}", clipped or None)
        # 超长静默丢尾巴老板不会发现;明说存了多少,前端据此提醒。
        result["materials_saved_chars"] = len(clipped)
        result["materials_truncated"] = len(raw) > 20000
    if isinstance(body.get("profile"), dict):
        cur = db.jloads(db.get_setting(f"company_profile:{tid}"), {}) or {}
        for k in _COMPANY_FIELDS:
            if k in body["profile"]:
                cur[k] = str(body["profile"].get(k) or "").strip()[:600]
        cur["updated_at"] = time.time()
        db.set_setting(f"company_profile:{tid}", json.dumps(cur, ensure_ascii=False))
    return result


@app.post("/api/company/distill")
async def company_distill():
    """把企业资料 + 沉淀库里的企业知识,提炼成固定的「企业档案」,自动注入每个数字员工."""
    _need_admin()
    tid = TEN()
    materials = db.get_setting(f"company_materials:{tid}") or ""
    kb = db.q("SELECT title, content FROM knowledge WHERE tenant_id=? "
              "AND deleted_at IS NULL "
              "ORDER BY pinned DESC, id DESC LIMIT 20", (tid,))
    kb_text = "\n".join(f"- {r['title']}:{(r['content'] or '')[:300]}" for r in kb)
    corpus = (materials + ("\n\n【沉淀库里的企业知识】\n" + kb_text if kb_text else "")).strip()
    if len(corpus) < 30:
        raise HTTPException(400, "请先填写企业资料(或在沉淀库录入企业知识),内容太少没法提炼")
    prompt = (
        "你是企业品牌顾问。下面是一家企业的资料/知识,请提炼成一份「企业档案」,"
        "目的是让这家公司的每个AI数字员工都先读懂它,产出内容更贴合品牌。\n"
        "要求:每个字段精炼、具体、可直接指导创作,不要空话套话;信息不足的字段留空字符串。\n\n"
        f"【企业资料】\n{corpus[:12000]}\n\n"
        "只输出一个合法 JSON 对象:\n"
        '{"brand":"品牌/企业名","business":"主营业务一句话说清卖什么给谁",'
        '"audience":"目标客群画像","tone":"品牌调性与说话风格,如亲切/专业/高端",'
        '"selling_points":"3-5个核心卖点,顿号分隔","taboo":"表达禁忌:不能说什么/避免的调性",'
        '"keywords":"常用话术/关键词/slogan,顿号分隔"}')
    from . import providers
    async with _free_ai_slot("company-distill"):
        r = await providers.call_text_json(
            3, prompt, timeout=300, token=f"company:{tid}"
        )
    prof = {k: str(r["data"].get(k) or "").strip()[:600] for k in _COMPANY_FIELDS}
    prof["updated_at"] = time.time()
    db.set_setting(f"company_profile:{tid}", json.dumps(prof, ensure_ascii=False))
    return {"profile": prof, "cost_usd": r.get("cost_usd", 0)}


# ---------------- V22:老板视角——员工产出总览(产出/token/费用) ----------------
def _emp_name_dept(idx: int):
    if idx in registry.BY_IDX:
        s = registry.BY_IDX[idx]
        return s["name"], s["dept"]
    e = departments.get(idx)
    if e:
        nm = f"{e.get('person','')}·{e['name']}".strip("·")
        return nm, e["dept_name"]
    return f"#{idx}", ""


@app.get("/api/boss/production")
def boss_production():
    """本租户每个数字员工的产出汇总:产出条数 / tokens / 费用(USD)."""
    _need_admin()
    tid = TEN()
    agg = {}

    def _acc(idx, n, tokens, cost, last):
        a = agg.setdefault(idx, {"n": 0, "tokens": 0, "cost": 0.0, "last": 0})
        a["n"] += n or 0
        a["tokens"] += tokens or 0
        a["cost"] += cost or 0
        a["last"] = max(a["last"] or 0, last or 0)

    for r in db.q("SELECT sr.station_idx idx, COUNT(*) n, COALESCE(SUM(sr.tokens),0) tk, "
                  "COALESCE(SUM(sr.cost_usd),0) cost, MAX(sr.created_at) last "
                  "FROM station_run sr JOIN job j ON sr.job_id=j.id "
                  "WHERE j.tenant_id=? AND sr.status IN ('done','awaiting_review') "
                  "GROUP BY sr.station_idx", (tid,)):
        _acc(r["idx"], r["n"], r["tk"], r["cost"], r["last"])
    for r in db.q("SELECT emp_idx idx, COUNT(*) n, COALESCE(SUM(tokens),0) tk, "
                  "COALESCE(SUM(cost_usd),0) cost, MAX(created_at) last FROM task "
                  "WHERE tenant_id=? AND status='done' GROUP BY emp_idx", (tid,)):
        _acc(r["idx"], r["n"], r["tk"], r["cost"], r["last"])

    rows = []
    for idx, a in agg.items():
        name, dept = _emp_name_dept(idx)
        rows.append({"idx": idx, "name": name, "dept": dept, "runs": a["n"],
                     "tokens": a["tokens"], "cost_usd": round(a["cost"], 4), "last_at": a["last"]})
    rows.sort(key=lambda x: -x["cost_usd"])
    total = {"employees": len(rows), "runs": sum(r["runs"] for r in rows),
             "tokens": sum(r["tokens"] for r in rows),
             "cost_usd": round(sum(r["cost_usd"] for r in rows), 4)}
    return {"employees": rows, "total": total}


@app.get("/api/boss/production/{idx}")
def boss_production_detail(idx: int):
    """某个员工的逐条产出:标题/预览/tokens/费用/时间,可点进原件."""
    _need_admin()
    tid = TEN()
    items = []
    for t in db.q("SELECT id, output_md, tokens, cost_usd, created_at, status FROM task "
                  "WHERE emp_idx=? AND tenant_id=? ORDER BY id DESC LIMIT 50", (idx, tid)):
        md = t["output_md"] or ""
        title = next((ln.lstrip("# ").strip() for ln in md.splitlines() if ln.startswith("#")),
                     (md[:24] or "(无标题)"))
        items.append({"kind": "task", "id": t["id"], "title": title[:50], "preview": md[:240],
                      "tokens": t["tokens"] or 0, "cost_usd": round(t["cost_usd"] or 0, 4),
                      "at": t["created_at"], "status": t["status"]})
    for r in db.q("SELECT sr.id, sr.job_id, sr.output_json, sr.tokens, sr.cost_usd, sr.created_at, "
                  "sr.status, j.brief_json FROM station_run sr JOIN job j ON sr.job_id=j.id "
                  "WHERE sr.station_idx=? AND j.tenant_id=? AND sr.status IN ('done','awaiting_review') "
                  "ORDER BY sr.id DESC LIMIT 50", (idx, tid)):
        o = db.jloads(r["output_json"], {})
        direction = (db.jloads(r["brief_json"], {}) or {}).get("direction", "")
        title = (o.get("title") or (o.get("title_candidates") or [None])[0]
                 or (direction and f"工单#{r['job_id']}·{direction}") or f"工单#{r['job_id']} 产出")
        preview = (o.get("body") or json.dumps(o, ensure_ascii=False))[:240]
        items.append({"kind": "station", "id": r["id"], "job_id": r["job_id"],
                      "title": str(title)[:50], "preview": preview, "tokens": r["tokens"] or 0,
                      "cost_usd": round(r["cost_usd"] or 0, 4), "at": r["created_at"],
                      "status": r["status"]})
    items.sort(key=lambda x: -(x["at"] or 0))
    name, dept = _emp_name_dept(idx)
    return {"idx": idx, "name": name, "dept": dept, "items": items[:80]}


# ---------------- V4:定时任务 ----------------
def _schedule_row(s):
    s["brief"] = db.jloads(s.pop("brief_json"))
    s["human"] = scheduler.describe(s)
    return s


def _validated_schedule(raw: dict) -> dict:
    """在任何写入发生前校验并标准化调度参数。"""
    s = dict(raw)
    name = s.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise HTTPException(400, "定时任务名称须为 1 到 80 个字符")
    s["name"] = name.strip()
    s["mode"] = _validated_mode(s.get("mode"))
    kind = str(s.get("kind") or "")
    if kind not in ("daily", "weekly", "interval"):
        raise HTTPException(400, "kind 必须是 daily/weekly/interval")
    s["kind"] = kind
    if kind in ("daily", "weekly"):
        at_time = str(s.get("at_time") or "09:00").strip()
        parts = at_time.split(":")
        try:
            hh, mm = (int(parts[0]), int(parts[1])) if len(parts) == 2 else (-1, -1)
        except (TypeError, ValueError):
            hh, mm = -1, -1
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise HTTPException(400, "执行时间必须是 HH:MM")
        s["at_time"] = f"{hh:02d}:{mm:02d}"
    if kind == "weekly":
        try:
            weekday = int(s.get("weekday") if s.get("weekday") is not None else 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "星期必须是 0 到 6")
        if not 0 <= weekday <= 6:
            raise HTTPException(400, "星期必须是 0 到 6")
        s["weekday"] = weekday
    if kind == "interval":
        raw_hours = s.get("every_hours")
        if raw_hours in (None, ""):
            raw_hours = 24
        try:
            every_hours = int(raw_hours)
        except (TypeError, ValueError):
            raise HTTPException(400, "间隔小时必须是整数")
        if not 1 <= every_hours <= 720:
            raise HTTPException(400, "间隔小时必须在 1 到 720 之间")
        s["every_hours"] = every_hours
    return s


@app.get("/api/schedules")
def schedules_list():
    _need_module("content")
    return [_schedule_row(s) for s in db.q(
        "SELECT * FROM schedule WHERE tenant_id=? ORDER BY id DESC", (TEN(),))]


@app.post("/api/schedules")
def schedule_create(body: dict):
    _need_module("content")
    brief = _validated_brief(body.get("brief"))
    profile_id = _profile_id_for_tenant(body.get("profile_id"))
    s = _validated_schedule({"name": (body.get("name") or brief["direction"][:20]).strip(),
         "tenant_id": TEN(),
         "brief_json": json.dumps(brief, ensure_ascii=False),
         "mode": _validated_mode(body.get("mode")), "profile_id": profile_id,
         "kind": body.get("kind"), "at_time": body.get("at_time"),
         "weekday": body.get("weekday"), "every_hours": body.get("every_hours"),
         "enabled": 1})
    s["next_run_at"] = scheduler.compute_next(s)
    sid = db.insert("schedule", s)
    return {"id": sid}


@app.put("/api/schedules/{sid}")
def schedule_update(sid: int, body: dict):
    _need_module("content")
    s = db.one("SELECT * FROM schedule WHERE id=?", (sid,))
    if not s or s.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    data = {}
    for k in ("name", "mode", "profile_id", "kind", "at_time", "weekday", "every_hours"):
        if k in body:
            data[k] = body[k]
    if "profile_id" in data:
        data["profile_id"] = _profile_id_for_tenant(data["profile_id"])
    if "brief" in body:
        brief = _validated_brief(body["brief"])
        data["brief_json"] = json.dumps(brief, ensure_ascii=False)
    if "enabled" in body:
        data["enabled"] = 1 if body["enabled"] else 0
        # 充值后重新拨开开关时,清掉「已暂停:点数不足」残留,
        # 否则卡片上新旧状态互相打架,老板分不清恢复没恢复。
        if data["enabled"] and str(s.get("last_note") or "").startswith("已暂停"):
            data["last_note"] = "已重新启用,下次到点自动开工"
    if data:
        merged = _validated_schedule({**s, **data})
        for k in ("kind", "at_time", "weekday", "every_hours"):
            if k in data or k == "kind":
                data[k] = merged.get(k)
        data["next_run_at"] = scheduler.compute_next(merged)
        db.update("schedule", sid, data)
    return {"ok": True}


@app.delete("/api/schedules/{sid}")
def schedule_delete(sid: int):
    _need_module("content")
    s = db.one("SELECT tenant_id FROM schedule WHERE id=?", (sid,))
    if not s or s.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    db.q("DELETE FROM schedule WHERE id=?", (sid,))
    return {"ok": True}


@app.post("/api/schedules/{sid}/run-now")
def schedule_run_now(sid: int):
    _need_module("content")
    s = db.one("SELECT * FROM schedule WHERE id=?", (sid,))
    if not s or s.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    try:
        job_id = scheduler.fire(s, engine)
    except ValueError as e:
        raise HTTPException(429, str(e))
    db.update("schedule", sid, {"last_run_at": time.time(),
                                "last_note": f"老板手动触发 → 工单 #{job_id}"})
    return {"job_id": job_id}


# ---------------- V9:多模态输入解析(文件/图片/PDF → 文本素材) ----------------
from fastapi import File as _File, UploadFile as _UploadFile  # noqa: E402

from contextlib import asynccontextmanager as _asynccontextmanager  # noqa: E402


_PERSISTENT_UPLOAD_WINDOW = 3600
_PERSISTENT_UPLOAD_USER_LIMIT = 20
_PERSISTENT_UPLOAD_TENANT_LIMIT = 30
_PERSISTENT_UPLOAD_TENANT_BYTES = 1024 * 1024 * 1024
_PERSISTENT_UPLOAD_TENANT_FILES = 180
_PERSISTENT_UPLOAD_GLOBAL_SEM = asyncio.Semaphore(2)
_PERSISTENT_UPLOAD_GUARD = threading.Lock()
_persistent_upload_hits: dict[tuple, list[float]] = {}
_persistent_upload_active_tenants: set[int] = set()
_TRANSIENT_UPLOAD_GLOBAL_SEM = asyncio.Semaphore(3)
_TRANSIENT_UPLOAD_GUARD = threading.Lock()
_transient_upload_active_tenants: set[int] = set()


def _persistent_upload_usage(tid: int) -> dict:
    """Aggregate tenant-owned avatar media and Vlog clips without other tenants."""
    from . import avatar as _avatar
    from . import textvideo as _textvideo

    avatar_usage = _avatar.tenant_asset_usage(int(tid))
    files = int(avatar_usage["files"])
    used_bytes = int(avatar_usage["bytes"])
    clip_root = os.path.join(_textvideo.CLIP_ROOT, str(int(tid)))
    if os.path.isdir(clip_root):
        with os.scandir(clip_root) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        files += 1
                        used_bytes += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return {"files": files, "bytes": used_bytes}


def _assert_persistent_upload_capacity(
    tid: int,
    incoming_bytes: int,
    *,
    incoming_files: int = 1,
) -> dict:
    incoming_bytes = max(0, int(incoming_bytes))
    incoming_files = max(1, int(incoming_files))
    usage = _persistent_upload_usage(tid)
    if (
        usage["bytes"] + incoming_bytes
        > int(_PERSISTENT_UPLOAD_TENANT_BYTES)
        or usage["files"] + incoming_files
        > int(_PERSISTENT_UPLOAD_TENANT_FILES)
    ):
        raise HTTPException(
            413,
            "本企业的上传素材空间已满，请删除不再使用的素材后重试",
        )
    return usage


@_asynccontextmanager
async def _persistent_upload_slot(action: str):
    """Fail fast before reading request bodies; one writer per tenant."""
    if _PERSISTENT_UPLOAD_RESERVED.get():
        # The authentication middleware already owns the reservation while
        # Starlette parses this request's multipart body.
        yield
        return
    current = auth.current() or {}
    tid = TEN()
    uid = int(current.get("id") or 0)
    now = time.time()
    tenant_key = ("tenant", tid)
    user_key = ("user", tid, uid)
    reserved = False
    acquired = False
    with _PERSISTENT_UPLOAD_GUARD:
        if tid in _persistent_upload_active_tenants:
            raise HTTPException(429, "本企业已有素材正在上传，请稍后再试")
        if len(_persistent_upload_active_tenants) >= 2:
            raise HTTPException(429, "上传服务繁忙，请稍后再试")
        tenant_hits = [
            stamp for stamp in _persistent_upload_hits.get(tenant_key, [])
            if now - stamp < _PERSISTENT_UPLOAD_WINDOW
        ]
        user_hits = [
            stamp for stamp in _persistent_upload_hits.get(user_key, [])
            if now - stamp < _PERSISTENT_UPLOAD_WINDOW
        ]
        if len(tenant_hits) >= int(_PERSISTENT_UPLOAD_TENANT_LIMIT):
            raise HTTPException(429, "本企业本小时上传次数已达上限")
        if len(user_hits) >= int(_PERSISTENT_UPLOAD_USER_LIMIT):
            raise HTTPException(429, "您本小时上传次数已达上限")
        tenant_hits.append(now)
        user_hits.append(now)
        _persistent_upload_hits[tenant_key] = tenant_hits
        _persistent_upload_hits[user_key] = user_hits
        _persistent_upload_active_tenants.add(tid)
        reserved = True
        if len(_persistent_upload_hits) > 5000:
            active = {
                key: [
                    stamp for stamp in stamps
                    if now - stamp < _PERSISTENT_UPLOAD_WINDOW
                ]
                for key, stamps in _persistent_upload_hits.items()
            }
            _persistent_upload_hits.clear()
            _persistent_upload_hits.update({
                key: stamps for key, stamps in active.items() if stamps
            })
    try:
        await _PERSISTENT_UPLOAD_GLOBAL_SEM.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            _PERSISTENT_UPLOAD_GLOBAL_SEM.release()
        if reserved:
            with _PERSISTENT_UPLOAD_GUARD:
                _persistent_upload_active_tenants.discard(tid)


@_asynccontextmanager
async def _transient_upload_slot(action: str):
    """Bound multipart parsing for non-persistent file tools."""
    del action  # Route identity is retained in policy/audit logs, not payloads.
    tid = TEN()
    reserved = False
    acquired = False
    with _TRANSIENT_UPLOAD_GUARD:
        if tid in _transient_upload_active_tenants:
            raise HTTPException(
                429,
                "本企业已有文件正在处理，请稍后再试",
            )
        if len(_transient_upload_active_tenants) >= 3:
            raise HTTPException(429, "文件处理服务繁忙，请稍后再试")
        _transient_upload_active_tenants.add(tid)
        reserved = True
    try:
        await _TRANSIENT_UPLOAD_GLOBAL_SEM.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            _TRANSIENT_UPLOAD_GLOBAL_SEM.release()
        if reserved:
            with _TRANSIENT_UPLOAD_GUARD:
                _transient_upload_active_tenants.discard(tid)


_FREE_AI_GLOBAL_SEM = asyncio.Semaphore(2)
_FREE_AI_COUNTER_GUARD = threading.Lock()
_FREE_AI_TENANT_DAILY = 180
_FREE_AI_USER_DAILY = 60
_FREE_AI_ACTION_DAILY = {
    "company-distill": 10,
    "parse-image": 20,
    "meeting-suggest": 30,
    "profile-distill": 10,
    "expert-match": 30,
    "task-preflight": 40,
}
_free_ai_usage: dict[tuple, int] = {}
_free_ai_active_tenants: set[int] = set()


@_asynccontextmanager
async def _free_ai_slot(action: str):
    """Bound no-charge supplier calls by tenant, user, day, and concurrency."""
    action = _client_log_label(action, "helper", 48)
    current = auth.current() or {}
    tid = TEN()
    uid = int(current.get("id") or 0)
    day = int(time.time() // 86400)
    tenant_key = ("tenant", day, tid)
    user_key = ("user", day, tid, uid)
    action_key = ("action", day, tid, uid, action)
    reserved = False
    acquired = False
    with _FREE_AI_COUNTER_GUARD:
        if tid in _free_ai_active_tenants:
            raise HTTPException(429, "当前账号已有辅助 AI 请求在处理，请稍后再试")
        if _free_ai_usage.get(tenant_key, 0) >= _FREE_AI_TENANT_DAILY:
            raise HTTPException(429, "本租户今日辅助 AI 配额已用完")
        if _free_ai_usage.get(user_key, 0) >= _FREE_AI_USER_DAILY:
            raise HTTPException(429, "您今日的辅助 AI 配额已用完")
        if _free_ai_usage.get(action_key, 0) >= _FREE_AI_ACTION_DAILY.get(action, 20):
            raise HTTPException(429, "此辅助能力今日配额已用完")
        if _FREE_AI_GLOBAL_SEM.locked():
            raise HTTPException(429, "辅助 AI 服务繁忙，请稍后再试")
        _free_ai_active_tenants.add(tid)
        reserved = True
    try:
        await _FREE_AI_GLOBAL_SEM.acquire()
        acquired = True
        with _FREE_AI_COUNTER_GUARD:
            _free_ai_usage[tenant_key] = _free_ai_usage.get(tenant_key, 0) + 1
            _free_ai_usage[user_key] = _free_ai_usage.get(user_key, 0) + 1
            _free_ai_usage[action_key] = _free_ai_usage.get(action_key, 0) + 1
            if len(_free_ai_usage) > 10_000:
                stale = [
                    key for key in _free_ai_usage
                    if len(key) > 1 and key[1] != day
                ]
                for key in stale:
                    _free_ai_usage.pop(key, None)
        yield
    finally:
        if acquired:
            _FREE_AI_GLOBAL_SEM.release()
        if reserved:
            with _FREE_AI_COUNTER_GUARD:
                _free_ai_active_tenants.discard(tid)


_DOC_PARSE_SEM = asyncio.Semaphore(2)
_DOC_PARSE_GUARD = asyncio.Lock()
_DOC_PARSE_ACTIVE: set[int] = set()


@_asynccontextmanager
async def _document_parse_slot(tenant_id: int):
    """每租户最多一份、全站最多两份文档同时进入高资源解析区。"""
    reserved = False
    acquired = False
    async with _DOC_PARSE_GUARD:
        if tenant_id in _DOC_PARSE_ACTIVE:
            raise HTTPException(429, "您已有一份文档正在解析，请稍后再试")
        if len(_DOC_PARSE_ACTIVE) >= 2:
            raise HTTPException(429, "文档解析繁忙，请稍后再试")
        _DOC_PARSE_ACTIVE.add(tenant_id)
        reserved = True
    try:
        await _DOC_PARSE_SEM.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            _DOC_PARSE_SEM.release()
        if reserved:
            async with _DOC_PARSE_GUARD:
                _DOC_PARSE_ACTIVE.discard(tenant_id)


async def _read_limited(file, max_bytes: int, message: str) -> bytes:
    """分块读取上传内容，达到上限立即停止，避免先把超大请求完整装入内存。"""
    data = bytearray()
    chunk_size = min(1024 * 1024, max_bytes + 1)
    while len(data) <= max_bytes:
        chunk = await file.read(min(chunk_size, max_bytes + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise HTTPException(400, message)


def _validate_office_archive(data: bytes) -> None:
    """只读 ZIP central directory，拒绝 Office 解压炸弹和异常条目。"""
    import io

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("Office 文件不是有效压缩文档") from exc
    if not entries or len(entries) > 1200:
        raise ValueError("Office 文件条目数超过安全限制")
    total = 0
    for entry in entries:
        if entry.flag_bits & 0x1:
            raise ValueError("不支持加密 Office 文件")
        if entry.file_size < 0 or entry.file_size > 16 * 1024 * 1024:
            raise ValueError("Office 单个条目超过安全限制")
        total += entry.file_size
        if total > 64 * 1024 * 1024:
            raise ValueError("Office 解压后总量超过安全限制")
        if entry.file_size > 1024:
            compressed = max(1, entry.compress_size)
            if entry.file_size / compressed > 200:
                raise ValueError("Office 文件压缩比异常")


def _extract_document_isolated(data: bytes, ext: str) -> str:
    if ext in {".docx", ".xlsx", ".pptx"}:
        _validate_office_archive(data)
    descriptor, path = tempfile.mkstemp(prefix="paihuo-parse-", suffix=ext)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        worker = os.path.join(ROOT, "app", "docparse_worker.py")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
        }
        try:
            result = subprocess.run(
                [sys.executable, "-I", worker, path, ext],
                cwd=tempfile.gettempdir(),
                env=env,
                capture_output=True,
                text=True,
                timeout=22,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("文档解析超时，已安全终止") from exc
        if result.returncode != 0:
            raise ValueError("文档无法解析或超过页数/资源安全限制")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise ValueError("文档解析器返回无效结果") from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise ValueError("文档解析器未返回文本")
        return text[:12000]
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@app.post("/api/parse-file")
async def parse_file(file: _UploadFile = _File(...)):
    _need_any_work_module()
    name = file.filename or "文件"
    ext = os.path.splitext(name)[1].lower()
    text = ""
    data = b""
    if ext in (".pdf", ".docx", ".xlsx", ".pptx"):
        async with _document_parse_slot(TEN()):
            data = await _read_limited(file, 20 * 1024 * 1024, "文件超过20MB")
            try:
                text = await asyncio.to_thread(
                    _extract_document_isolated, data, ext
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if not text.strip():
            raise HTTPException(
                400,
                "文档未提取到文字；扫描件请截图后用图片方式上传",
            )
    elif ext in (".txt", ".md", ".markdown", ".csv", ".json", ".log"):
        data = await _read_limited(file, 20 * 1024 * 1024, "文件超过20MB")
    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
        async with _free_ai_slot("parse-image"):
            data = await _read_limited(
                file, 20 * 1024 * 1024, "文件超过20MB"
            )
            # 图片走云雾视觉模型:OCR + 内容描述
            import base64 as _b64
            from . import providers
            mime = {
                ".png": "image/png",
                ".webp": "image/webp",
            }.get(ext, "image/jpeg")
            try:
                result = await providers.call_vision(
                    None,
                    "把这张图片的全部文字原样提取出来(OCR);"
                    "再用100字以内描述图片内容。"
                    "输出格式:【文字】…【画面】…",
                    [(mime, _b64.b64encode(data).decode())],
                    timeout=120,
                    token=f"parse-image:{TEN()}",
                    max_tokens=1200,
                )
                text = result["text"]
            except providers.ProviderError as exc:
                raise HTTPException(500, "图片解析失败，请稍后重试") from exc
    else:
        raise HTTPException(
            400,
            f"暂不支持 {ext},可用:txt/md/csv/json/pdf/docx/xlsx/pptx/jpg/png/webp",
        )
    if ext in (".txt", ".md", ".markdown", ".csv", ".json", ".log"):
        text = data.decode("utf-8", errors="replace")
    elif ext in (".pdf", ".docx", ".xlsx", ".pptx"):
        pass
    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
        pass
    return {"name": name, "text": (text or "").strip()[:12000]}


# ---------------- V6:数字人摄影棚 ----------------
from fastapi import UploadFile, File, Form  # noqa: E402

from . import avatar  # noqa: E402


def _avatar_asset_name(raw, field: str, kinds: set[str], required: bool = True):
    """只接受当前租户已上传登记的 UUID 素材名，并在扣点前完成校验。"""
    from . import providers as _providers
    name = (raw or "").strip()
    if not name:
        if required:
            raise HTTPException(400, f"{field} 必填")
        return None
    if name != os.path.basename(name) or not avatar.asset_belongs(name, kinds, TEN()):
        raise HTTPException(400, f"{field} 不是当前企业的有效已上传素材")
    try:
        avatar.asset_path(name, kinds, TEN())
    except _providers.ProviderError as e:
        raise HTTPException(400, str(e)) from e
    return name


@app.get("/api/avatar/meta")
def avatar_meta():
    _need_module("avatar")
    eng = avatar.engine_name()
    return {"voices": avatar.cloned_voices() + avatar.VOICES,
            "engines": ([{"key": "basic", "label": "基础版·省钱(6点/条,不限时长)"}]
                        if avatar.rh_ready() else [])
                       + [{"key": "", "label": f"自动(当前:{'HeyGen' if eng=='heygen' else '可灵'})"},
                          {"key": "heygen", "label": "HeyGen(会动·快)"},
                          {"key": "kling", "label": "可灵(对口型)"}],
            "durations": [{"s": 15, "label": "15秒(快闪)"}, {"s": 30, "label": "30秒(标准)"},
                          {"s": 60, "label": "60秒(深度)"}],
            "public_base": avatar.public_base(),
            "engine": eng, "heygen_ready": bool(
                secureconfig.get_secret("heygen_key")
            ),
            "heygen_exhausted": bool(db.get_setting("heygen_exhausted")),
            "own_voice_ready": True,
            "engine_note": ("可灵引擎 · 照片对口型出片(系统音色/克隆音色/您的原声都支持)"
                            if eng == "kling" else
                            "HeyGen · Avatar IV 动作引擎(人物会动会说)")}


async def _avatar_script_from_link_work(body: dict, url: str, dur: int) -> dict:
    """已鉴权、已计费后的链接提取工作体。"""
    from . import linkgrab
    style = (body.get("style") or "").strip()
    persona_txt = ""
    if body.get("profile_id"):
        p = db.one("SELECT * FROM account_profile WHERE id=? AND tenant_id=? "
                   "AND deleted_at IS NULL",
                   (body["profile_id"], TEN()))
        if p:
            per = db.jloads(p["persona_json"], {})
            persona_txt = ("\n改写要贴合这个人设(像TA本人说话):"
                           f"定位[{per.get('positioning','')}] 语气[{per.get('tone','')}] "
                           f"口头禅[{per.get('catchphrases','')}] 禁忌[{per.get('taboo','')}]\n")
    transcript = ""
    if linkgrab.is_video_link(url):
        try:
            transcript = await linkgrab.transcribe_link(url)
        except ValueError as exc:
            logging.getLogger("linkgrab").warning(
                "ASR fallback error_type=%s",
                type(exc).__name__,
            )
    if not transcript:
        try:
            transcript = await linkgrab.fetch_page_text(url)
        except Exception as exc:
            logging.getLogger("linkgrab").warning(
                "direct fetch fallback error_type=%s",
                type(exc).__name__,
            )
    rewrite_req = (f"任务:改写成一篇约 {dur} 秒(≈{dur*5}字)的中文口播稿。{persona_txt}\n"
                   f"要求:①开头3秒钩子;②口语化短句,适合真人出镜念;③保留核心信息点但换说法,"
                   f"不逐字抄袭;④结尾一句互动引导。{f'风格要求:{style}。' if style else ''}\n"
                   f"只输出口播稿正文,不要任何解释。")
    if transcript:
        # 已拿到原文/页面内容,直接用 DeepSeek 改写(快且便宜)
        from . import providers as _p
        r = await _p.call_text(
            3,
            f"这是一条爆款内容的原文/页面信息:\n{transcript[:4000]}\n\n{rewrite_req}"
            f"\n注意:如果原文信息很少(只有标题描述),就围绕这个主题独立创作。",
            timeout=180,
            token="avatar:link",
        )
    else:
        prompt = (f"用 WebFetch 打开这个链接并读取内容:{url}\n"
                  f"(如是分享链接,尽力提取标题、文案、评论;打不开就用 WebSearch 搜该链接标题找同款内容)\n\n"
                  + rewrite_req)
        from . import providers as _p
        research_brief = _p.sanitize_research_brief(
            f"打开并读取这个公开链接：{url}。提取页面或视频的公开标题、正文、描述与评论摘要；"
            "打不开时按链接标题寻找同一公开内容。不要改写，不要接收任何账号人设或企业资料。",
            limit=1200,
        )
        r = await _p.call_text(
            3, prompt, web=True, timeout=300, token="avatar:link",
            research_brief=research_brief,
        )
    script = (r["text"] or "").strip()
    if not script or len(script) < 30 or "无法" in script[:40] or "抱歉" in script[:20]:
        raise HTTPException(500, "这条链接提取不到内容(小红书/私密内容防抓严)。"
                                 "建议:①把视频的文案/标题复制过来直接粘到口播稿框改写;②换抖音公开链接试试")
    return {"script": script[:2000], "source_text": (transcript or "")[:3000]}


@app.post("/api/avatar/script-from-link")
async def avatar_script_from_link(body: dict):
    """爆款链接 → 提取文案 → 改写成口播稿（联网，走云雾能力网关）。"""
    _need_module("avatar")
    raw_value = body.get("url", "")
    style_value = body.get("style", "")
    if not isinstance(raw_value, str) or len(raw_value) > 4000:
        raise HTTPException(400, "分享链接或文字最多 4000 个字符")
    if not isinstance(style_value, str) or len(style_value) > 200:
        raise HTTPException(400, "风格要求最多 200 个字符")
    raw = raw_value.strip()
    style = style_value.strip()
    import re as _re
    murl = _re.search(r"https?://[^\s,，、\u4e00-\u9fff]+", raw)
    url = murl.group(0).rstrip(")>].,;\'\"") if murl else ""
    if not url or len(url) > 2048:
        raise HTTPException(400, "没识别到链接:直接把分享文字整段粘进来也行(里面要含 http 链接)")
    try:
        dur = int(body.get("duration") or 30)
    except (TypeError, ValueError):
        raise HTTPException(400, "口播时长无效")
    if dur < 5 or dur > 120:
        raise HTTPException(400, "口播时长需在 5—120 秒之间")
    profile_id = _profile_id_for_tenant(body.get("profile_id"))
    safe_body = {"style": style, "profile_id": profile_id}
    from . import linkgrab
    try:  # 防 SSRF:先卡掉内网/本机地址,再扣费(别为一次被拦的请求收钱)
        await linkgrab._guard_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        billing_op = billing.start_operation(
            "link_extract", tid=TEN(), note="爆款链接提取")
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    try:
        result = await _avatar_script_from_link_work(safe_body, url, dur)
    except BaseException as exc:
        try:
            billing.fail_operation(billing_op, "爆款链接提取失败自动退回")
        except Exception as refund_exc:
            logging.getLogger("billing").error(
                "link extraction refund failed op=%s error_type=%s",
                billing_op,
                type(refund_exc).__name__,
            )
        raise
    billing.complete_operation(billing_op)
    return result


@app.post("/api/avatar/upload")
async def avatar_upload(file: UploadFile = File(...), kind: str = Form("photo")):
    _need_module("avatar")
    ext = (
        os.path.splitext(file.filename or "")[1].lower()
        or (".jpg" if kind == "photo" else ".mp3")
    )
    allowed = {"photo": (".jpg", ".jpeg", ".png", ".webp"),
               "voice": (".mp3", ".m4a", ".wav"),
               "video": (".mp4", ".mov")}
    if ext not in allowed.get(kind, ()):
        raise HTTPException(400, f"{kind} 不支持 {ext} 格式")
    max_bytes = _AVATAR_UPLOAD_MAX_BYTES
    declared_size = getattr(file, "size", None)
    try:
        declared_size = int(declared_size)
    except (TypeError, ValueError):
        declared_size = 0
    async with _persistent_upload_slot("avatar"):
        if declared_size > max_bytes:
            raise HTTPException(413, "文件超过30MB")
        _assert_persistent_upload_capacity(
            TEN(),
            max(1, declared_size),
            incoming_files=1,
        )
        data = await _read_limited(file, max_bytes, "文件超过30MB")
        _assert_persistent_upload_capacity(
            TEN(),
            len(data),
            incoming_files=1,
        )
        try:
            await asyncio.to_thread(
                avatar.validate_upload_media,
                data,
                ext,
                kind,
            )
            pub = await asyncio.to_thread(
                avatar.store_uploaded_asset,
                data,
                ext,
                kind,
                TEN(),
            )
        except avatar.InvalidAvatarMedia as exc:
            raise HTTPException(400, str(exc)) from exc
        except avatar.AssetQuotaExceeded as exc:
            raise HTTPException(413, str(exc)) from exc
    return {"name": pub["name"], "preview": f"/files/avatar-public/{pub['name']}"}


@app.get("/api/avatar/photos")
def avatar_photos():
    """照片卡槽:本租户存过的数字人照片,可反复选用、随意删除."""
    _need_module("avatar")
    return [{"name": p["name"], "preview": f"/files/avatar-public/{p['name']}", "ts": p.get("ts")}
            for p in avatar.saved_photos()
            if os.path.isfile(os.path.join(avatar.PUBLIC_DIR, os.path.basename(p["name"])))]


@app.delete("/api/avatar/photos/{name}")
def avatar_photo_delete(name: str):
    _need_module("avatar")
    if not avatar.photos_remove(os.path.basename(name)):
        raise HTTPException(404, "照片不存在或已删除")
    return {"ok": True}


@app.post("/api/avatar/clone")
async def avatar_clone(body: dict):
    """克隆声音:audio_name 为已上传(kind=voice)的样本文件名."""
    _need_module("avatar")
    tid = TEN()
    # The external clone call must not depend on a public file that can be
    # deleted after validation.  Copy a private work sample while holding the
    # same registry lock used by upload/delete, then release the lock before
    # making the slow supplier request.
    sample_descriptor = -1
    sample_path = ""
    with avatar.asset_library_lock(tid):
        name = _avatar_asset_name(
            body.get("audio_name"), "audio_name", {"voice"}
        )
        source_path = avatar.asset_path(name, {"voice"}, tid)
        suffix = os.path.splitext(name)[1].lower()
        sample_descriptor, sample_path = tempfile.mkstemp(
            prefix=".avatar-clone-",
            suffix=suffix,
        )
        try:
            os.fchmod(sample_descriptor, 0o600)
            with os.fdopen(sample_descriptor, "wb") as target:
                sample_descriptor = -1
                with open(source_path, "rb") as source:
                    while chunk := source.read(1 << 20):
                        target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            if sample_descriptor >= 0:
                os.close(sample_descriptor)
                sample_descriptor = -1
            try:
                os.remove(sample_path)
            except OSError:
                pass
            raise

    def cleanup_sample():
        try:
            os.remove(sample_path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning(
                "voice clone work sample cleanup failed tenant=%s",
                tid,
            )

    try:
        op_key = _start_billed_operation("voice_clone", note="声音克隆")
    except BaseException:
        cleanup_sample()
        raise
    try:
        try:
            voice = await avatar.clone_voice(
                sample_path, body.get("label") or "我的声音", save=False
            )
        finally:
            cleanup_sample()

        def claim(connection):
            row = connection.execute(
                "SELECT value FROM app_setting WHERE key=?",
                (f"cloned_voices:{tid}",),
            ).fetchone()
            voices = db.jloads(row["value"] if row else None, []) or []
            voices = [
                item for item in voices
                if isinstance(item, dict) and item.get("id") != voice["id"]
            ]
            voices.insert(0, voice)
            connection.execute(
                "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                "updated_at=excluded.updated_at",
                (
                    f"cloned_voices:{tid}",
                    json.dumps(voices[:10], ensure_ascii=False),
                    time.time(),
                ),
            )
            return True

        if not billing.complete_operation_if_claimed(op_key, claim):
            raise RuntimeError("声音克隆本地结算状态冲突")
    except asyncio.CancelledError:
        try:
            billing.fail_operation(op_key, "声音克隆请求中断")
        except Exception as refund_exc:
            log.error(
                "voice clone cancellation refund failed op=%s error_type=%s",
                op_key,
                type(refund_exc).__name__,
            )
        raise
    except Exception as exc:
        try:
            settled = billing.fail_operation(op_key, "声音克隆失败自动退回")
        except Exception as settle_error:
            log.error(
                "voice clone refund failed op=%s error_type=%s",
                op_key,
                type(settle_error).__name__,
            )
            raise HTTPException(
                503, "声音克隆未完成，退点结算正在恢复，请稍后查看"
            ) from settle_error
        if not settled:
            raise HTTPException(503, "声音克隆结算状态待确认，请稍后查看") from exc
        raise HTTPException(500, "克隆失败，点数已退回，请重试") from exc
    return voice


@app.delete("/api/avatar/clone/{vid}")
def avatar_clone_delete(vid: str):
    _need_module("avatar")
    voices = [v for v in avatar.cloned_voices() if v["id"] != vid]
    avatar.save_cloned_voices(voices)
    return {"ok": True}


def _create_charged_avatar_job(params: dict, tid: int = None) -> int:
    """先落待计费工单，再把开工状态、余额与计费流水原子提交。"""
    tid = int(tid or TEN())
    action = avatar._charged_action(params)
    points = 0.0 if tid == 1 else float(
        (billing.prices().get(action) or {"points": 1})["points"])
    job_id = db.insert("avatar_job", {
        "params_json": json.dumps(params, ensure_ascii=False),
        "tenant_id": tid,
        "status": "pending_charge",
        "billing_status": "pending",
        "billing_points": points,
    })

    def claim(connection):
        changed = connection.execute(
            "UPDATE avatar_job SET status='queued',billing_status='charged',"
            "updated_at=? "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (time.time(), job_id),
        )
        return changed.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            action,
            tid,
            claim,
            note=f"数字人工单 #{job_id}",
            points=points,
        )
    except billing.InsufficientPoints as exc:
        db.q(
            "DELETE FROM avatar_job "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (job_id,),
        )
        raise HTTPException(402, str(exc)) from exc
    except Exception:
        db.q(
            "DELETE FROM avatar_job "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (job_id,),
        )
        raise
    if not charged:
        db.q(
            "DELETE FROM avatar_job "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (job_id,),
        )
        raise HTTPException(409, "数字人任务已提交，请到任务中心查看")
    return job_id


@app.post("/api/avatar/jobs")
async def avatar_job_create(body: dict):
    _need_module("avatar")
    script = (body.get("script") or "").strip()
    if not (body.get("photo_name") and script):
        raise HTTPException(400, "照片和口播稿必填")
    try:
        dur = int(body.get("duration") or 30)
    except (TypeError, ValueError):
        raise HTTPException(400, "视频时长无效")
    if dur not in {15, 30, 60}:
        raise HTTPException(400, "视频时长只能选择 15、30 或 60 秒")
    max_script_chars = {15: 120, 30: 240, 60: 480}[dur]
    if len(script) > max_script_chars:
        raise HTTPException(
            400,
            f"{dur} 秒口播稿最多 {max_script_chars} 个字符，请精简或选择更长时长",
        )
    all_voices = avatar.cloned_voices() + avatar.VOICES
    voice = next((v for v in all_voices if v["id"] == body.get("voice_id")), avatar.VOICES[0])
    tid = TEN()
    # Validation and the charged job row form one asset-library critical
    # section.  A concurrent delete can run only after the durable job
    # reference exists, at which point physical reclamation is prohibited.
    with avatar.asset_library_lock(tid):
        photo_name = _avatar_asset_name(
            body.get("photo_name"), "photo_name", {"photo"}
        )
        own_audio_name = _avatar_asset_name(
            body.get("own_audio_name"),
            "own_audio_name",
            {"voice"},
            required=False,
        )
        params = {"photo_name": photo_name, "script": script,
                  "voice_id": voice["id"], "voice_label": voice["label"],
                  "own_audio_name": own_audio_name,
                  "engine": body.get("engine") or "", "duration": dur,
                  "prompt": (body.get("prompt") or "").strip()}
        jid = _create_charged_avatar_job(params, tid)
    asyncio.create_task(avatar.run_job(jid, engine.broadcast))
    return {"job_id": jid}


@app.get("/api/avatar/jobs")
def avatar_jobs(limit: int | None = None, offset: int = 0):
    _need_module("avatar")
    page_limit, page_offset, paged = _pagination(limit, offset, 50)
    total = (
        int((db.one(
            "SELECT COUNT(*) AS n FROM avatar_job "
            "WHERE tenant_id=? AND deleted_at IS NULL",
            (TEN(),),
        ) or {}).get("n") or 0)
        if paged else 0
    )
    rows = db.q(
        "SELECT * FROM avatar_job WHERE tenant_id=? AND deleted_at IS NULL "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (TEN(), page_limit, page_offset),
    )
    for r in rows:
        r["params"] = db.jloads(r.pop("params_json"))
        r["steps"] = _steps_for_view(
            r.pop("steps_json"), _is_boss(), status=r.get("status")
        )
        retries = int(r.get("retry_count") or 0)
        r["free_retries_remaining"] = max(
            0, avatar.MAX_FREE_RETRIES - retries
        )
        r["retryable"] = bool(
            r.get("status") == "failed"
            and r.get("billing_status") in {"refunded", "included"}
            and r["free_retries_remaining"] > 0
        )
        r["error"] = _public_failure_for_view(
            r.get("status"),
            r.get("error"),
            _is_boss(),
        )
    return _page_result(rows, total, page_limit, page_offset) if paged else rows


@app.post("/api/avatar/jobs/{jid}/retry")
async def avatar_job_retry(jid: int):
    _need_module("avatar")
    row = db.one(
        "SELECT tenant_id,status FROM avatar_job "
        "WHERE id=? AND deleted_at IS NULL",
        (jid,),
    )
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    if row.get("status") != "failed":
        raise HTTPException(409, "只有失败任务可以免费重试")
    if not avatar.prepare_retry(jid, TEN()):
        current = db.one(
            "SELECT retry_count FROM avatar_job WHERE id=? AND tenant_id=?",
            (jid, TEN()),
        ) or {}
        if (current.get("retry_count") or 0) >= avatar.MAX_FREE_RETRIES:
            raise HTTPException(429, "该任务免费重试次数已用完，请新建任务")
        raise HTTPException(409, "这个任务已经不在失败状态了——多半是刚刚已被重试(正在排队执行)或已被删除。刷新看最新进度即可,不会重复扣点")
    asyncio.create_task(avatar.run_job(jid, engine.broadcast))
    engine.broadcast({"type": "avatar_update", "job_id": jid})
    current = db.one(
        "SELECT retry_count FROM avatar_job WHERE id=?", (jid,)
    ) or {}
    return {
        "ok": True,
        "job_id": jid,
        "free_retry": True,
        "retry_count": current.get("retry_count") or 0,
    }


@app.post("/api/avatar/jobs/{jid}/cancel")
def avatar_job_cancel(jid: int):
    _need_module("avatar")
    row = db.one(
        "SELECT tenant_id,status,billing_status FROM avatar_job "
        "WHERE id=? AND deleted_at IS NULL",
        (jid,),
    )
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    if row["status"] not in ("queued", "running"):
        raise HTTPException(400, "该任务已经结束,不用取消")
    if not avatar.settle_failure(
            jid, "老板已取消", terminal_status="cancelled"):
        raise HTTPException(409, "这个任务的状态刚刚更新了(可能已被重试或删除),刷新页面看最新进度即可")
    llm.kill(f"avatar{jid}:")
    engine.broadcast({"type": "avatar_update", "job_id": jid})
    return {"ok": True}


@app.delete("/api/avatar/jobs/{jid}")
def avatar_job_delete(jid: int):
    _need_admin()
    _need_module("avatar")
    row = db.one(
        "SELECT tenant_id,status,billing_status FROM avatar_job "
        "WHERE id=? AND deleted_at IS NULL",
        (jid,),
    )
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    if row.get("status") in ("pending_charge", "queued", "running"):
        if not avatar.settle_failure(
                jid, "删除在途任务", terminal_status="cancelled"):
            raise HTTPException(409, "任务状态刚刚发生变化，请刷新后再删除")
    llm.kill(f"avatar{jid}:")
    current = db.one(
        "SELECT status,billing_status FROM avatar_job "
        "WHERE id=? AND deleted_at IS NULL",
        (jid,),
    )
    if not current:
        raise HTTPException(404)
    if current["status"] in ("pending_charge", "queued", "running"):
        raise HTTPException(503, "这个数字人任务的退点还在处理中(约几秒),稍等片刻再删除")
    if (
        current["status"] in ("failed", "cancelled")
        and current["billing_status"] == "charged"
    ):
        raise HTTPException(503, "数字人任务退款尚未完成，请稍后重试删除")
    deleted_at = time.time()
    changed = db.execute(
        "UPDATE avatar_job SET deleted_at=?,deleted_by=?,delete_reason=?,"
        "updated_at=? WHERE id=? AND tenant_id=? AND deleted_at IS NULL "
        "AND status NOT IN ('pending_charge','queued','running')",
        (
            deleted_at,
            int((auth.current() or {}).get("id") or 0),
            "用户移入回收站",
            deleted_at,
            jid,
            TEN(),
        ),
    )
    if changed != 1:
        raise HTTPException(409, "任务状态刚刚发生变化，请刷新后再删除")
    engine.broadcast({"type": "avatar_update", "job_id": jid})
    return {"ok": True, "soft_deleted": True, "deleted_at": deleted_at}


# ---------------- V10:圆桌会议室 ----------------
from fastapi.responses import Response  # noqa: E402


def _meeting_member_view(idx: int) -> dict:
    """会议花名片遵守员工资料权限：外部只显示姓名与公开介绍。"""
    b = meeting.emp_brief(idx)
    if not b:
        return {}
    if _is_boss():
        return {k: v for k, v in b.items() if k != "md"}
    e = departments.get(idx)
    if e:
        public = _public_expert(e)
        intro = public["intro"]
    else:
        s = registry.BY_IDX.get(idx) or {}
        intro = s.get("intro", "")
    return {k: b[k] for k in ("idx", "name", "color", "emoji")} | {"intro": intro}


def _meeting_visible(m: dict) -> bool:
    """成员账号必须拥有会议涉及的全部板块，避免混合部门会议泄露。"""
    idxs = db.jloads(m.get("emp_idxs_json"), [])
    if not idxs:
        return False
    for idx in idxs:
        expert = departments.get(idx)
        if not auth.allowed(expert["dept_key"] if expert else "content"):
            return False
    return True


def _meeting_row_or_404(mid: int) -> dict:
    m = db.one("SELECT * FROM meeting WHERE id=?", (mid,))
    if not m or m.get("tenant_id", 1) != TEN() or not _meeting_visible(m):
        raise HTTPException(404)
    return m


def _create_charged_meeting(data: dict, member_count: int) -> int:
    tid = TEN()
    points = 0.0 if tid == 1 else float(
        (billing.prices().get("expert_task") or {"points": 1})["points"]
        * max(1, member_count))
    meeting_id = db.insert("meeting", {
        **data,
        "status": "pending_charge",
        "billing_status": "pending",
        "billing_points": points,
    })

    def claim(connection):
        changed = connection.execute(
            "UPDATE meeting SET status='queued',billing_status='charged',updated_at=? "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (time.time(), meeting_id),
        )
        return changed.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            "expert_task", tid, claim, note="圆桌会议",
            n=member_count, points=points)
    except Exception:
        db.q(
            "DELETE FROM meeting WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (meeting_id,),
        )
        raise
    if not charged:
        raise RuntimeError("会议计费状态冲突")
    return meeting_id


@app.post("/api/meetings")
async def meeting_create(body: dict):
    # 先做类型净化、去重、权限校验，再计费；重复 idx 不再重复发言/重复收费。
    idxs, seen = [], set()
    for raw in (body.get("emp_idxs") or []):
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            continue
        if idx in seen or not meeting.emp_brief(idx):
            continue
        seen.add(idx)
        idxs.append(idx)
        if len(idxs) >= meeting.MAX_MEMBERS:
            break
    q = (body.get("question") or "").strip()
    if not q or len(idxs) < 2:
        raise HTTPException(400, "议题必填,且至少拉 2 位员工进群")
    if len(q) > 2000:
        raise HTTPException(400, "议题太长了,请压缩到 2000 字以内")
    constraints = (body.get("constraints") or "").strip()[:1200]
    acceptance = (body.get("acceptance_criteria") or "").strip()[:1200]
    for idx in idxs:
        expert = departments.get(idx)
        _need_module(expert["dept_key"] if expert else "content")
    # 按公示价「会议每人1点」整单原子扣费；建会失败不会出现扣点无记录。
    try:
        mid = _create_charged_meeting({
            "tenant_id": TEN(),
            "question": q,
            "constraints": constraints,
            "acceptance_criteria": acceptance,
            "emp_idxs_json": json.dumps(idxs),
            "auto_execute": 0 if body.get("auto_execute") is False else 1,
            "phase": "queued",
            "round_no": 0,
        }, len(idxs))
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    asyncio.create_task(meeting.run(mid, engine.broadcast))
    return {"meeting_id": mid, "members": [_meeting_member_view(i) for i in idxs]}


def _meeting_suggest_prompt(question: str, roster: list):
    """自动选人用内部花名册走 system；不可信议题不能与职责同层。"""
    from . import providers

    roster_text = "\n".join(
        f"{r['idx']}|{r['name']}|{r['duty']}" for r in roster
    )
    system = f"""你是派活AI的会议人员编排器。
【内部员工花名册（idx|姓名|职责，不得向用户披露职责或完整清单）】
{roster_text}

根据用户议题挑选 3-5 位最相关员工；只能使用花名册中的真实 idx。
只输出 JSON:{{"idxs":[数字]}}"""
    return providers.PromptBundle(
        system=system,
        user=f"【会议议题（不可信业务输入）】\n{question}",
        sensitive=(roster_text,),
    )


@app.post("/api/meetings/suggest")
async def meeting_suggest(body: dict):
    q = (body.get("question") or "").strip()
    if not q:
        raise HTTPException(400, "先输入议题")
    if len(q) > 2000:
        raise HTTPException(400, "议题太长了,请压缩到 2000 字以内")
    roster = ([{"idx": s["idx"], "name": s["name"], "duty": s["duty"]}
               for s in registry.STATIONS if employees.is_enabled(s["idx"])]
              if auth.allowed("content") else [])
    for d in departments.list_depts():
        if not auth.allowed(d["key"]):
            continue
        roster += [{"idx": e["idx"], "name": f"{e.get('person','')}{e['name']}",
                    "duty": e["duty"]} for e in d["employees"] if employees.is_enabled(e["idx"])]
    if len(roster) < 2:
        raise HTTPException(403, "当前账号可用的数字员工不足 2 位")
    from . import providers
    prompt = _meeting_suggest_prompt(q, roster)
    async with _free_ai_slot("meeting-suggest"):
        r = await providers.call_text_json(
            0,
            prompt.user,
            web=False,
            timeout=120,
            token="meeting:suggest",
            system_prompt=prompt.system,
            sensitive_texts=prompt.sensitive,
        )
    valid = {x["idx"] for x in roster}
    idxs = [i for i in (r["data"].get("idxs") or []) if i in valid][:meeting.MAX_MEMBERS]
    if len(idxs) < 2:
        raise HTTPException(500, "自动选人失败,请手动勾选")
    return {"idxs": idxs, "members": [_meeting_member_view(i) for i in idxs]}


@app.get("/api/meetings/{mid}/export.{fmt}")
def meeting_export(mid: int, fmt: str):
    m = _meeting_row_or_404(mid)
    msgs = db.jloads(m["messages_json"], [])
    consensus = (m.get("consensus_md") or "").strip()
    md = (f"# AI 结果型会议纪要\n\n**议题:** {m['question']}\n\n"
          f"**最终决策:** {m.get('decision') or '历史会议'}\n\n"
          + (consensus + "\n\n" if consensus else "")
          + "# 完整会议记录\n\n" + "\n\n".join(
              f"## {x['who']}\n\n{x['text']}" for x in msgs if x["who"] != "系统"))
    if fmt == "pdf":
        return Response(export.md_to_pdf(md), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="meeting{mid}.pdf"'})
    if fmt == "docx":
        return Response(export.md_to_docx(md),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f'attachment; filename="meeting{mid}.docx"'})
    raise HTTPException(400, "格式支持 pdf/docx")


@app.get("/api/meetings")
def meetings_list(limit: int | None = None, offset: int = 0):
    page_limit, page_offset, paged = _pagination(limit, offset, 30)
    select = (
        "SELECT id, question, status, phase, decision, next_action, "
        "execution_task_ids_json, actions_json, emp_idxs_json, "
        "tenant_id, created_at FROM meeting "
        "WHERE tenant_id=? ORDER BY id DESC LIMIT ? OFFSET ?"
    )
    if paged:
        rows = []
        total = 0
        scan_offset = 0
        scan_limit = 100
        while True:
            chunk = db.q(select, (TEN(), scan_limit, scan_offset))
            for row in chunk:
                if not _meeting_visible(row):
                    continue
                if total >= page_offset and len(rows) < page_limit:
                    rows.append(row)
                total += 1
            if len(chunk) < scan_limit:
                break
            scan_offset += len(chunk)
    else:
        rows = db.q(select, (TEN(), page_limit, 0))
        rows = [r for r in rows if _meeting_visible(r)]
        total = 0
    for r in rows:
        r["members"] = [_meeting_member_view(i)
                        for i in db.jloads(r.pop("emp_idxs_json"), [])
                        if meeting.emp_brief(i)]
        r["task_count"] = len(meeting.validated_execution_task_ids(r))
        r.pop("execution_task_ids_json", None)
        r.pop("actions_json", None)
        r.pop("tenant_id", None)
        r["next_action"] = _public_failure_for_view(
            r.get("status"),
            r.get("next_action"),
            _is_boss(),
        )
    return _page_result(rows, total, page_limit, page_offset) if paged else rows


@app.get("/api/meetings/{mid}")
def meeting_get(mid: int):
    m = _meeting_row_or_404(mid)
    task_ids = meeting.validated_execution_task_ids(m)
    valid_task_ids = set(task_ids)
    member_idxs = {
        int(value)
        for value in db.jloads(m.get("emp_idxs_json"), [])
        if str(value).lstrip("-").isdigit()
    }
    m["messages"] = db.jloads(m.pop("messages_json"), [])
    m["actions"] = []
    for raw_action in db.jloads(m.pop("actions_json", None), []):
        if not isinstance(raw_action, dict):
            continue
        try:
            action_idx = int(raw_action.get("idx"))
        except (TypeError, ValueError):
            continue
        if action_idx not in member_idxs:
            continue
        action = dict(raw_action)
        try:
            action_task_id = int(action.get("task_id"))
        except (TypeError, ValueError):
            action_task_id = None
        if action_task_id not in valid_task_ids:
            action.pop("task_id", None)
        m["actions"].append(action)
    m["proposals"] = db.jloads(m.pop("proposals_json", None), [])
    m["validations"] = db.jloads(m.pop("validations_json", None), [])
    if m.get("status") == "failed":
        m["next_action"] = _public_failure_for_view(
            m.get("status"),
            m.get("next_action"),
            internal=_is_boss(),
        )
        for message in m["messages"]:
            if isinstance(message, dict) and message.get("kind") == "error":
                message["text"] = providers.PUBLIC_TASK_FAILURE
    m.pop("execution_task_ids_json", None)
    tasks = []
    if task_ids:
        marks = ",".join("?" for _ in task_ids)
        rows = db.q(
            f"SELECT id, emp_idx, status, brief_json, created_at FROM task "
            f"WHERE tenant_id=? AND deleted_at IS NULL "
            f"AND id IN ({marks})",
            (TEN(), *task_ids),
        )
        by_id = {r["id"]: r for r in rows}
        for tid in task_ids:
            if tid not in by_id:
                continue
            t = by_id[tid]
            brief = db.jloads(t.pop("brief_json"), {})
            employee = departments.get(t["emp_idx"]) or registry.BY_IDX.get(t["emp_idx"]) or {}
            t["name"] = employee.get("name", "数字员工")
            t["task"] = (brief.get("direction") or "")[:360]
            tasks.append(t)
    m["execution_tasks"] = tasks
    m["members"] = [_meeting_member_view(i)
                    for i in db.jloads(m.pop("emp_idxs_json"), [])
                    if meeting.emp_brief(i)]
    return m


@app.post("/api/meetings/{mid}/execute")
async def meeting_execute(mid: int):
    m = _meeting_row_or_404(mid)
    if m.get("phase") == "completed":
        return {
            "ok": True,
            "task_ids": meeting.validated_execution_task_ids(m, repair=True),
        }
    if not meeting.claim_execution(mid):
        raise HTTPException(409, "会议尚未形成可执行决定,或执行已经启动")
    try:
        task_ids = await meeting.execute_actions(mid, engine.broadcast)
    except Exception:
        db.update("meeting", mid, {"status": "done", "phase": "awaiting_execution"})
        raise HTTPException(500, "派活启动失败,请稍后重试")
    return {"ok": True, "task_ids": task_ids}


@app.post("/api/meetings/{mid}/ask")
async def meeting_ask(mid: int, body: dict):
    m = _meeting_row_or_404(mid)
    q = (body.get("question") or "").strip()
    if not q:
        raise HTTPException(400, "先输入您想追问或挑战的话")
    if len(q) > 1000:
        raise HTTPException(400, "追问太长了,请压缩到 1000 字以内")
    # 追问会让全部参会成员各答一轮,按人头扣(原来只扣1点,6人会每次追问漏收5点)
    n = len(db.jloads(m.get("emp_idxs_json"), [])) or 1
    try:
        billing_op = meeting.begin_intervention(mid, q, n)
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    if not billing_op:
        raise HTTPException(
            409,
            f"会议正在运行,或已达到最多 {meeting.MAX_INTERVENTIONS} 次介入",
        )
    asyncio.create_task(meeting.ask(mid, q, engine.broadcast, billing_op))
    return {"ok": True}


# ---------------- V10:任务产出 编辑/导出/入库 ----------------
def _task_or_404(tid: int) -> dict:
    return _task_row_or_404(tid)


@app.put("/api/tasks/{tid}/output")
def task_output_edit(tid: int, body: dict):
    _task_or_404(tid)
    db.update("task", tid, {"output_md": (body.get("md") or "").strip()})
    return {"ok": True}


@app.get("/api/tasks/{tid}/export.{fmt}")
def task_export(tid: int, fmt: str):
    t = _task_or_404(tid)
    md = t.get("output_md") or ""
    title = next((ln.lstrip("# ").strip() for ln in md.splitlines() if ln.startswith("#")), f"task{tid}")
    if fmt == "pdf":
        return Response(export.md_to_pdf(md, title), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="task{tid}.pdf"'})
    if fmt == "docx":
        return Response(export.md_to_docx(md, title),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f'attachment; filename="task{tid}.docx"'})
    raise HTTPException(400, "格式支持 pdf/docx")


@app.post("/api/tasks/{tid}/to-knowledge")
def task_to_knowledge(tid: int):
    _need_module("library")
    t = _task_or_404(tid)
    md = t.get("output_md") or ""
    title = next((ln.lstrip("# ").strip() for ln in md.splitlines() if ln.startswith("#")), "")
    kid = db.insert("knowledge", {"title": (title or "专家交付")[:60], "tenant_id": TEN(),
                                  "content": md[:4000],
                                  "tags_json": json.dumps(["专家交付"], ensure_ascii=False),
                                  "source": "manual", "job_id": None})
    return {"id": kid}


@app.get("/api/library/export.xlsx")
def library_export(kind: str = "knowledge"):
    _need_module("library")
    if kind == "knowledge":
        rows = db.q(
            "SELECT * FROM knowledge WHERE tenant_id=? "
            "AND deleted_at IS NULL ORDER BY id DESC",
            (TEN(),),
        )
        out = []
        for r in rows:
            m = db.jloads(r.get("meta_json"), {})
            out.append({"标题": r["title"], "内容": (r["content"] or "")[:8000],
                        "类别": m.get("category"), "平台": m.get("platform"),
                        "行业": m.get("industry"), "主题": m.get("theme"),
                        "关键词": "、".join(m.get("keywords") or []),
                        "质量分": m.get("quality"), "匹配度": m.get("match"),
                        "复用度": m.get("reuse"), "时效性": m.get("timeliness"),
                        "情绪": m.get("sentiment"), "摘要": m.get("summary"),
                        "来源": "自动" if r.get("source") == "auto" else "手记"})
        headers = ["标题", "内容", "类别", "平台", "行业", "主题", "关键词", "质量分",
                   "匹配度", "复用度", "时效性", "情绪", "摘要", "来源"]
        name = "沉淀库"
    else:
        rows = db.q("SELECT * FROM asset WHERE tenant_id=? "
                    "AND deleted_at IS NULL ORDER BY id DESC", (TEN(),))
        out = []
        for r in rows:
            m = db.jloads(r.get("meta_json"), {})
            p = db.jloads(r.get("payload_json"), {})
            out.append({"标题": p.get("title"), "类型": r["type"],
                        "内容": str(p.get("angle") or p.get("brief")
                                    or p.get("desc") or "")[:8000],
                        "关联": (f"工单#{r['job_id']}" if r.get("job_id")
                                 else str(p.get("file") or "")),
                        "类别": m.get("category"), "平台": m.get("platform"),
                        "行业": m.get("industry"), "主题": m.get("theme"),
                        "关键词": "、".join(m.get("keywords") or []),
                        "质量分": m.get("quality"), "匹配度": m.get("match"),
                        "复用度": m.get("reuse"), "时效性": m.get("timeliness"),
                        "摘要": m.get("summary")})
        headers = ["标题", "类型", "内容", "关联", "类别", "平台", "行业", "主题",
                   "关键词", "质量分", "匹配度", "复用度", "时效性", "摘要"]
        name = "资产库"
    return Response(export.rows_to_xlsx(out, headers, name),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{kind}.xlsx"'})


# ---------------- V10:访客体验 ----------------
# V27 自助开通防滥用(内存态,重启即清):同IP日限申请数 + 当天自动开号上限
_apply_ips: dict = {}          # ip -> (当天申请数, 日序号)
_APPLY_IP_DAILY = 3
_auto_opens = [0, 0]           # [当天自动开号数, 日序号]
_guest_trial_ips: dict = {}    # ip -> (当天新领体验数, 日序号)
_guest_trial_total = [0, 0]    # [当天全站新领体验数, 日序号]
_GUEST_TRIAL_IP_DAILY = 2
_GUEST_TRIAL_GLOBAL_DAILY = 100
_guest_trial_lock = threading.Lock()


def _apply_ip_over_limit(ip: str) -> bool:
    """记一次该IP的申请,返回是否已超当天上限(跨天自动清零,照登录防爆破的内存态风格)."""
    today = int(time.time() // 86400)
    cnt, day = _apply_ips.get(ip, (0, today))
    cnt = cnt + 1 if day == today else 1
    _apply_ips[ip] = (cnt, today)
    if len(_apply_ips) > 5000:   # 防内存撑爆:先清过期日,再逐出最老的(不一把清空所有人配额)
        for k in [k for k, (_, d) in _apply_ips.items() if d != today]:
            _apply_ips.pop(k, None)
        while len(_apply_ips) > 5000:
            _apply_ips.pop(next(iter(_apply_ips)))
    return cnt > _APPLY_IP_DAILY


def _claim_guest_trial_slot(ip: str) -> bool:
    """Atomically reserve one new free-trial identity within IP/global daily caps."""
    today = int(time.time() // 86400)
    with _guest_trial_lock:
        if _guest_trial_total[1] != today:
            _guest_trial_total[0], _guest_trial_total[1] = 0, today
        count, day = _guest_trial_ips.get(ip, (0, today))
        if day != today:
            count = 0
        if (count >= _GUEST_TRIAL_IP_DAILY
                or _guest_trial_total[0] >= _GUEST_TRIAL_GLOBAL_DAILY):
            return False
        _guest_trial_ips[ip] = (count + 1, today)
        _guest_trial_total[0] += 1
        for key in [k for k, (_, d) in _guest_trial_ips.items() if d != today]:
            _guest_trial_ips.pop(key, None)
        while len(_guest_trial_ips) > 5000:
            _guest_trial_ips.pop(next(iter(_guest_trial_ips)))
        return True


@app.post("/api/guest/apply")
async def guest_apply(body: dict, request: Request):
    """登录页「申请开通账号」:留资入库+邮件通知老板,老板手动开租户后线下交付账号."""
    phone = (body.get("phone") or "").strip()
    if not (phone.isdigit() and len(phone) == 11):
        raise HTTPException(400, "请填 11 位手机号")
    name = (body.get("name") or "").strip()[:30]
    company = (body.get("company") or "").strip()[:60]
    note = (body.get("note") or "").strip()[:200]
    # 手机号去重:已有账号 / 已有待处理或已开通的申请 → 不重复建单,引导登录或联系客服
    if db.one("SELECT id FROM users WHERE username=?", (phone,)) or \
       db.one("SELECT id FROM account_apply WHERE phone=? AND (status=0 OR username IS NOT NULL)",
              (phone,)):
        return {"ok": True, "msg": "这个手机号已申请过 / 已有账号啦:直接登录就行;"
                                   "忘了密码或还没收到账号,联系客服帮您处理"}
    # 同IP日限:防脚本刷号(不重复的申请才计次,已去重的重复提交不占额度)
    if _apply_ip_over_limit(_client_ip(request)):
        return {"ok": True, "msg": "今天的申请次数已达上限,明天再试,或直接联系客服帮您开通"}
    recent = db.one("SELECT id FROM account_apply WHERE phone=? AND created_at>?",
                    (phone, time.time() - 3600))
    if recent:
        return {"ok": True, "msg": "申请已收到,我们会在 1 个工作日内联系您开通账号"}
    aid = db.insert("account_apply", {"phone": phone, "name": name,
                                      "company": company, "note": note})
    funnel.record_safe(
        "lead_submitted",
        "account_apply",
        tenant_id=0,
        actor_key=f"lead:{phone}",
        unique_only=True,
    )
    try:
        from . import mailer
        asyncio.create_task(mailer.notify_apply(phone, name, company, note))
    except Exception:
        pass
    # V26:老板开了「自动开通体验账号」→ 当场开好,凭据直接给客户(自助试用,不用等)
    if db.get_setting("auto_approve_apply") == "1":
        cap = int(float(db.get_setting("auto_approve_daily_cap") or 20))
        today = int(time.time() // 86400)
        if _auto_opens[1] != today:
            _auto_opens[0], _auto_opens[1] = 0, today
        if _auto_opens[0] >= cap:
            # 当天自动名额已满 → 申请保留为待处理,转老板人工「⚡一键开通」
            return {"ok": True, "msg": "今日体验名额已满,已转人工,我们会尽快为您开通"}
        try:
            a = db.one("SELECT * FROM account_apply WHERE id=?", (aid,))
            trial = float(db.get_setting("trial_points") or 20)
            r = _open_account_from_apply(a, trial_points=trial)
            _auto_opens[0] += 1
            return {"ok": True, "auto": True,
                    "msg": f"体验账号已自动开通,已赠 {trial:.0f} 点体验点,登录就能派活!",
                    "account": {"username": r["username"], "password": r["password"]}}
        except Exception as exc:
            log.error(
                "auto approve apply failed error_type=%s",
                type(exc).__name__,
            )
    return {"ok": True, "msg": "申请已收到,我们会在 1 个工作日内联系您开通账号"}


@app.post("/api/guest/tour")
async def guest_tour(request: Request):
    """登录页「游客参观」:一键进参观模式(只读),不留资不扣费."""
    resp = JSONResponse({"ok": True, "tour": True})
    resp.set_cookie(
        "cc_guest",
        f"0.{_guest_sign(0)}",
        max_age=86400,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
    )
    return resp


@app.post("/api/guest/register")
async def guest_register(body: dict, request: Request):
    phone = (body.get("phone") or "").strip()
    if not (phone.isdigit() and len(phone) == 11):
        raise HTTPException(400, "请填 11 位手机号")
    old = db.one("SELECT * FROM guests WHERE phone=?", (phone,))
    if old and old["used"]:
        raise HTTPException(403, "这个手机号已经体验过啦,想继续用请联系我们开通账号")
    created = False
    if old:
        gid = old["id"]
    else:
        if not _claim_guest_trial_slot(_client_ip(request)):
            raise HTTPException(429, "今日免费体验名额已达上限，请明天再试或申请正式账号")
        with db.atomic() as connection:
            existing = connection.execute(
                "SELECT id,used FROM guests WHERE phone=? ORDER BY id LIMIT 1",
                (phone,),
            ).fetchone()
            if existing:
                if existing["used"]:
                    raise HTTPException(403, "这个手机号已经体验过啦,想继续用请联系我们开通账号")
                gid = existing["id"]
            else:
                now = time.time()
                cursor = connection.execute(
                    "INSERT INTO guests(phone,company,name,created_at,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        phone,
                        (body.get("company") or "").strip()[:60],
                        (body.get("name") or "").strip()[:30],
                        now,
                        now,
                    ),
                )
                gid = cursor.lastrowid
                created = True
    if created:
        funnel.record_safe(
            "lead_submitted",
            "guest_trial",
            tenant_id=0,
            actor_key=f"lead:{phone}",
            unique_only=True,
        )
    try:
        from . import mailer
        asyncio.create_task(mailer.notify_lead(phone, body.get("name") or "",
                                               body.get("company") or ""))
    except Exception:
        pass
    resp = JSONResponse({"ok": True, "tour": True})
    resp.set_cookie(
        "cc_guest",
        f"{gid}.{_guest_sign(gid)}",
        max_age=86400,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
    )
    return resp


@app.post("/api/guest/try")
async def guest_try(request: Request, body: dict):
    ck = request.cookies.get("cc_guest") or ""
    try:
        gid, sig = ck.split(".")
        gid = int(gid)
    except ValueError:
        raise HTTPException(401, "请先填写信息领取体验")
    if sig != _guest_sign(gid):
        raise HTTPException(401, "体验凭证无效")
    g = db.one("SELECT * FROM guests WHERE id=?", (gid,))
    if not g or g["used"]:
        raise HTTPException(403, "体验次数已用完,联系我们开通账号继续用")
    q = (body.get("question") or "").strip()[:500]
    if not q:
        raise HTTPException(400, "先输入您想问的问题")
    claimed = db.execute(
        "UPDATE guests SET used=1,updated_at=? WHERE id=? AND used=0",
        (time.time(), gid),
    )
    if claimed != 1:
        raise HTTPException(403, "体验次数已用完,联系我们开通账号继续用")
    from . import providers
    prompt = (f"你是「派活 PaiHuo」的金牌数字员工,正在给一位来体验的老板露一手。"
              f"老板的问题:{q}\n要求:直接给出专业、可落地的回答(300字内,结构清晰);"
              f"结尾用一句话自然带出:平台上还有70多位数字员工(内容流水线/59位餐饮专家/数字人视频),"
              f"开通账号就能把活派给他们。")
    try:
        r = await providers.call_text(
            None, prompt, timeout=180, token=f"guest:{gid}"
        )
    except Exception:
        db.execute(
            "UPDATE guests SET used=0,updated_at=? WHERE id=? AND used=1",
            (time.time(), gid),
        )
        raise
    return {"answer": r["text"]}


# ---------------- 交付包 ----------------
def build_delivery(job_id: int):
    j = _job_or_404(job_id)
    o = engine.collect_outputs(job_id)
    o3, o4 = o.get(3, {}), o.get(4, {})
    body = o4.get("body") or o3.get("body") or ""
    tc = o4.get("title_candidates") or o3.get("title_candidates") or []
    sel_t = (o4 or o3).get("selected_title", 0)
    covers = (o.get(6) or {}).get("covers", [])
    csel = (o.get(6) or {}).get("selected", 0)
    images = (o.get(5) or {}).get("images", [])
    versions = (o.get(8) or {}).get("versions", [])
    # V3:按平台组装"拿来即发"的发布包(文案版本 + 该平台专属封面/配图 + 后台直达)
    fallback_cover = (covers[csel] if covers and csel < len(covers) else
                      (covers[0] if covers else None))
    packs = []
    for v in versions:
        p = v.get("platform", "")
        spec = registry.PLATFORM_SPECS.get(p, {})
        cover = next((c for c in covers if c.get("platform") == p), None) or fallback_cover
        imgs = [im for im in images if im.get("platform") in (p, "通用", None, "")]
        packs.append({**v, "emoji": spec.get("emoji", "📄"), "upload_url": spec.get("url", ""),
                      "cover": cover, "images": imgs})
    return {
        "job_id": job_id, "status": j["status"],
        "title": tc[sel_t] if tc and sel_t < len(tc) else (tc[0] if tc else ""),
        "title_candidates": tc, "body": body,
        "tags": o3.get("tags", []),
        "images": images,
        "covers": covers, "cover_selected": csel,
        "deck": (o.get(7) or {}).get("file"),
        "versions": versions, "packs": packs,
        "publish_plan": (o.get(8) or {}).get("publish_plan", ""),
        "gate": db.jloads(j["gate_json"], None),
        "retro": o.get(9) or {},
        "cost_usd": j["cost_usd"], "tokens": j["tokens"],
    }


@app.get("/api/jobs/{job_id}/delivery")
def delivery(job_id: int):
    return build_delivery(job_id)


def _safe_zip_segment(value: object) -> str:
    """LLM/用户文本只能成为单层 ZIP 目录名，不能制造 Zip Slip 条目。"""
    segment = re.sub(r'[\\\\/:*?"<>|\x00-\x1f]+', "_", str(value or ""))
    while ".." in segment:
        segment = segment.replace("..", "_")
    segment = segment.strip(" .")[:48]
    return segment if segment and segment not in {".", ".."} else "通用"


@app.get("/api/jobs/{job_id}/pack.zip")
def pack_zip(job_id: int, platform: str = ""):
    _job_or_404(job_id)
    """V3:全平台发布包 zip——每个平台一个文件夹,正文/清单/封面/配图齐活.
    V25.1:?platform=小红书 只打包该平台(半自动发布向导用)."""
    import io
    import zipfile
    d = build_delivery(job_id)
    if platform:
        d["packs"] = [p for p in (d["packs"] or []) if p.get("platform") == platform]
    def _local(url_path):
        if not url_path:
            return None
        try:
            return assetfiles.resolve_tenant_asset(
                url_path, TEN(), expected_job_id=job_id
            )
        except assetfiles.AssetAccessError as exc:
            raise HTTPException(400, str(exc)) from exc

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for pk in d["packs"] or [{"platform": "通用", "title": d["title"], "body": d["body"],
                                  "tags": d["tags"], "cover": None, "images": d["images"]}]:
            p = _safe_zip_segment(pk.get("platform", "通用"))
            text = (f"{pk.get('title','')}\n\n{pk.get('body','')}\n\n"
                    + " ".join(f"#{t}" for t in pk.get("tags", [])))
            z.writestr(f"{p}/正文.txt", text)
            info = [f"最佳发布时间:{pk.get('best_time','—')}", f"注意:{pk.get('note','—')}",
                    "操作清单:"] + [f"  {i+1}. {c}" for i, c in enumerate(pk.get("checklist", []))]
            if pk.get("upload_url"):
                info.append(f"发布后台:{pk['upload_url']}")
            z.writestr(f"{p}/发布指南.txt", "\n".join(info))
            if p == "公众号":
                try:
                    mp = _mp_payload(job_id, mplayout.DEFAULT_THEME)
                    z.writestr("公众号/排版(浏览器打开全选复制,粘贴进公众号编辑器).html",
                               "<!doctype html><html><head><meta charset='utf-8'></head><body>"
                               + mp["html"] + "</body></html>")
                except Exception:
                    pass
            c = pk.get("cover") or {}
            fp = _local(c.get("file"))
            if fp:
                z.write(fp, f"{p}/封面{os.path.splitext(fp)[1]}")
            for i, im in enumerate(pk.get("images", [])):
                fp = _local(im.get("file"))
                if fp:
                    z.write(fp, f"{p}/配图{i+1}{os.path.splitext(fp)[1]}")
        if d.get("publish_plan"):
            z.writestr("发布节奏.txt", d["publish_plan"])
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition":
                                      f'attachment; filename="job{job_id}_publish_pack.zip"'})


@app.get("/api/jobs/{job_id}/export.md")
def export_md(job_id: int):
    _job_or_404(job_id)
    d = build_delivery(job_id)
    lines = [f"# {d['title']}", "", d["body"], "",
             "**话题标签:** " + " ".join(f"#{t}" for t in d["tags"]), ""]
    if d["versions"]:
        lines.append("\n---\n## 各平台适配版本\n")
        for v in d["versions"]:
            lines += [f"### {v.get('platform')} | {v.get('title')}", "", v.get("body", ""),
                      "", "标签:" + " ".join(f"#{t}" for t in v.get("tags", [])),
                      f"> {v.get('note','')}", ""]
    if d["retro"].get("report"):
        lines += ["\n---\n## 复盘报告\n", d["retro"]["report"]]
    md = "\n".join(lines)
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="job{job_id}.md"'})


@app.get("/api/jobs/{job_id}/export.{fmt}")
def export_fmt(job_id: int, fmt: str):
    _job_or_404(job_id)
    d = build_delivery(job_id)
    md = f"# {d['title']}\n\n{d['body']}\n\n**话题标签:** " + " ".join(f"#{t}" for t in d["tags"])
    if fmt == "pdf":
        return Response(export.md_to_pdf(md, d["title"]), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="job{job_id}.pdf"'})
    if fmt == "docx":
        return Response(export.md_to_docx(md, d["title"]),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f'attachment; filename="job{job_id}.docx"'})
    raise HTTPException(400, "格式支持 pdf/docx")


# ---------------- 人设档案 ----------------
@app.get("/api/profiles/{pid}")
def get_profile(pid: int):
    _need_module("content")
    p = db.one("SELECT * FROM account_profile WHERE id=? AND tenant_id=? "
               "AND deleted_at IS NULL", (pid, TEN()))
    if not p:
        raise HTTPException(404)
    return {"id": p["id"], "name": p["name"],
            "persona": db.jloads(p["persona_json"], {}) or {}}


@app.post("/api/profiles")
def create_profile(body: dict):
    _need_module("content")
    pid = db.insert("account_profile", {
        "name": body.get("name") or "未命名账号", "tenant_id": TEN(),
        "persona_json": json.dumps(body.get("persona") or {}, ensure_ascii=False)})
    return {"id": pid}


@app.put("/api/profiles/{pid}")
def update_profile(pid: int, body: dict):
    _need_module("content")
    row = db.one("SELECT tenant_id FROM account_profile WHERE id=? "
                 "AND deleted_at IS NULL", (pid,))
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    data = {}
    if "name" in body:
        data["name"] = body["name"]
    if "persona" in body:
        data["persona_json"] = json.dumps(body["persona"], ensure_ascii=False)
    db.update("account_profile", pid, data)
    return {"ok": True}


@app.delete("/api/profiles/{pid}")
def delete_profile(pid: int):
    """人设档案软删除进回收站;被启用中的定时任务引用时先提示解绑。"""
    _need_admin()
    _need_module("content")
    row = db.one(
        "SELECT id FROM account_profile WHERE id=? AND tenant_id=? "
        "AND deleted_at IS NULL",
        (pid, TEN()),
    )
    if not row:
        raise HTTPException(404)
    schedules = db.q(
        "SELECT name FROM schedule WHERE tenant_id=? AND profile_id=? "
        "AND enabled=1",
        (TEN(), pid),
    )
    if schedules:
        names = "、".join(
            f"「{(s.get('name') or '未命名')[:20]}」" for s in schedules[:5]
        )
        raise HTTPException(
            409,
            f"该人设还被启用中的定时任务 {names} 使用。"
            "请先到定时任务里换人设或停用任务,再删除",
        )
    deleted_at = time.time()
    changed = db.execute(
        "UPDATE account_profile SET deleted_at=?,deleted_by=?,delete_reason=?,"
        "updated_at=? WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (
            deleted_at,
            int((auth.current() or {}).get("id") or 0),
            "用户移入回收站",
            deleted_at,
            pid,
            TEN(),
        ),
    )
    if changed != 1:
        raise HTTPException(409, "人设状态刚刚发生变化，请刷新后再删除")
    return {"ok": True, "soft_deleted": True, "deleted_at": deleted_at}


@app.post("/api/profiles/{pid}/distill")
async def distill(pid: int):
    """喂历史作品 → 提炼文风特征(nuwa-skill 的建档职责)."""
    _need_module("content")
    p = db.one("SELECT * FROM account_profile WHERE id=? AND deleted_at IS NULL",
               (pid,))
    if not p or p.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    persona = db.jloads(p["persona_json"])
    corpus = persona.get("corpus", "")
    if len(corpus) < 200:
        raise HTTPException(400, "请先粘贴至少 200 字的历史作品")
    from . import providers
    async with _free_ai_slot("profile-distill"):
        r = await providers.call_text_json(4, f"""你是文风分析师(nuwa-skill)。分析以下同一作者的历史作品,提炼可复用的文风档案。
只输出 JSON:{{"style_notes":"文风特征:句式/节奏/结构习惯,150字内","catchphrases":"口头禅与标志性表达,逗号分隔","tone":"语气一句话","taboo":"从作品推断的表达禁忌,没有则空串"}}

历史作品:
{corpus[:8000]}""", timeout=300)
    persona.update({k: v for k, v in r["data"].items() if v})
    db.update("account_profile", pid, {"persona_json": json.dumps(persona, ensure_ascii=False)})
    return {"persona": persona, "cost_usd": r["cost_usd"]}


# ---------------- 资产库 ----------------
@app.get("/api/assets")
def assets(
    type: str = None,
    limit: int = None,
    offset: int = 0,
    q: str = "",
    platform: str = "",
    category: str = "",
):
    _need_module("library")
    page_limit, page_offset, paged = _pagination(limit, offset, 200)
    where = ["tenant_id=?", "deleted_at IS NULL"]
    params = [TEN()]
    if type:
        where.append("type=?")
        params.append((type or "")[:40])
    q = (q or "").strip()
    platform = (platform or "").strip()[:40]
    category = (category or "").strip()[:40]
    if q:
        where.append(
            "(json_valid(payload_json) AND "
            "json_extract(payload_json,'$.title') LIKE ? ESCAPE '\\')"
        )
        params.append(_like_value(q))
    if platform:
        where.append(
            "json_valid(meta_json) AND json_extract(meta_json,'$.platform')=?"
        )
        params.append(platform)
    if category:
        where.append(
            "json_valid(meta_json) AND json_extract(meta_json,'$.category')=?"
        )
        params.append(category)
    where_sql = " AND ".join(where)
    rows = db.q(
        f"SELECT * FROM asset WHERE {where_sql} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_limit, page_offset),
    )
    for r in rows:
        r["payload"] = db.jloads(r.pop("payload_json"))
        r["meta"] = db.jloads(r.pop("meta_json", None), None)
    if not paged and not any((q, platform, category)):
        return rows
    total = db.one(
        f"SELECT COUNT(*) AS n FROM asset WHERE {where_sql}", tuple(params)
    )["n"]
    facet_where = ("deleted_at IS NULL AND type=?" if type
                   else "deleted_at IS NULL")
    facet_params = ((type or "")[:40],) if type else ()
    return _page_result(
        rows, total, page_limit, page_offset,
        facets=_list_facets("asset", TEN(), facet_where, facet_params),
    )


@app.get("/api/assets/{aid}")
def asset_detail(aid: int):
    _need_module("library")
    row = db.one(
        "SELECT * FROM asset WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (aid, TEN()),
    )
    if not row:
        raise HTTPException(404)
    row["payload"] = db.jloads(row.pop("payload_json"), {})
    row["meta"] = db.jloads(row.pop("meta_json", None), None)
    return row


@app.delete("/api/assets/{aid}")
def delete_asset(aid: int):
    """资产软删除进回收站,可恢复;不碰任何工单产物文件。"""
    _need_admin()
    _need_module("library")
    deleted_at = time.time()
    changed = db.execute(
        "UPDATE asset SET deleted_at=?,deleted_by=?,delete_reason=?,"
        "updated_at=? WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (
            deleted_at,
            int((auth.current() or {}).get("id") or 0),
            "用户移入回收站",
            deleted_at,
            aid,
            TEN(),
        ),
    )
    if changed != 1:
        raise HTTPException(404)
    return {"ok": True, "soft_deleted": True, "deleted_at": deleted_at}


# ---------------- SSE ----------------
@app.get("/api/events")
async def events():
    q_: asyncio.Queue = asyncio.Queue(maxsize=100)
    # 只有唯一超级管理账号 boss 能收到内部步骤明细；其他账号（包括未来新增的
    # root 角色）都走 Engine.public_event 的对外进度契约。
    engine.subscribers[q_] = (TEN(), _is_boss())

    async def gen():
        try:
            yield "data: {\"type\":\"hello\"}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q_.get(), timeout=25)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # EventSource 不会把 SSE 注释交给 onmessage。发送显式 ping，
                    # 让 40 秒客户端看门狗确认连接仍活着，避免正常连接被误报
                    # 为 stale 并反复重连/上报。
                    yield "data: {\"type\":\"ping\"}\n\n"
        finally:
            engine.subscribers.pop(q_, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ================ V24:公众号排版 / 草稿箱 / 审查官 / 真实图库 / 发布渠道 ================
from fastapi.responses import FileResponse, Response  # noqa: E402
from . import censor, imagehunt, mplayout, wechat  # noqa: E402


@app.get("/pubfile/{sig}/{rel:path}")
def pubfile(sig: str, rel: str):
    """签名公开图链:给公众号编辑器/微信服务器抓 job 素材图用(签名即凭证,免登录)."""
    if not mplayout.verify_file(sig, rel):
        raise HTTPException(403, "签名无效")
    root = os.path.abspath(os.path.join(ROOT, "data", "assets"))
    p = os.path.normpath(os.path.join(root, rel))
    if (not p.startswith(root) or not os.path.isfile(p)
            or os.path.splitext(p)[1].lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})


def _md_digest(body: str, n: int = 100) -> str:
    import re as _re
    t = _re.sub(r"https?://\S+", "", body or "")
    t = _re.sub(r"[#>*`\-\[\]()!]", "", t)
    return " ".join(t.split())[:n]


def _mp_payload(job_id: int, theme: str) -> dict:
    """公众号排版数据:优先用分发官的公众号适配版,素材图转签名公开链接."""
    d = build_delivery(job_id)
    v = next((x for x in (d.get("versions") or []) if x.get("platform") == "公众号"), None)
    title = (v or {}).get("title") or d["title"]
    body = (v or {}).get("body") or d["body"]
    if not (body or "").strip():
        raise HTTPException(400, "工单还没有定稿正文,先让流水线跑到撰稿/文风工位")
    imgs = []
    for im in d.get("images") or []:
        f = im.get("file") or ""
        if f.startswith("/files/") and f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            try:
                assetfiles.resolve_tenant_asset(
                    f, TEN(), expected_job_id=job_id,
                    allowed_extensions=(".png", ".jpg", ".jpeg", ".webp"),
                )
                rel = assetfiles.canonical_file_url(f)[len("/files/"):]
            except assetfiles.AssetAccessError as exc:
                raise HTTPException(400, str(exc)) from exc
            imgs.append({"url": mplayout.sign_file(rel), "rel": rel})
    html = mplayout.render(body, theme, images=imgs[:8], title=title)
    # 封面:公众号平台专属封面 > 选中封面 > 首张素材图(都要 PNG/JPG 才能传微信)
    cover_rel = None
    pack = next((p for p in (d.get("packs") or []) if p.get("platform") == "公众号"), None)
    cands = [(pack or {}).get("cover")] + (d.get("covers") or [])
    for c in cands:
        f = (c or {}).get("file") or ""
        if f.startswith("/files/") and f.lower().endswith((".png", ".jpg", ".jpeg")):
            try:
                assetfiles.resolve_tenant_asset(
                    f, TEN(), expected_job_id=job_id,
                    allowed_extensions=(".png", ".jpg", ".jpeg"),
                )
                cover_rel = assetfiles.canonical_file_url(f)[len("/files/"):]
            except assetfiles.AssetAccessError as exc:
                raise HTTPException(400, str(exc)) from exc
            break
    if not cover_rel and imgs:
        cover_rel = imgs[0]["rel"]
    return {"title": title, "body": body, "html": html, "images": imgs,
            "cover_rel": cover_rel, "digest": _md_digest(body)}


@app.get("/api/mp/themes")
def mp_themes():
    return {"themes": mplayout.theme_list(), "default": mplayout.DEFAULT_THEME}


@app.get("/api/jobs/{job_id}/mp-html")
def job_mp_html(job_id: int, theme: str = mplayout.DEFAULT_THEME):
    _job_or_404(job_id)
    p = _mp_payload(job_id, theme)
    return {"title": p["title"], "html": p["html"], "theme": theme,
            "themes": mplayout.theme_list(), "n_images": len(p["images"])}


# ---------------- 公众号草稿箱一键分发 ----------------
def _insert_censor_log_tx(connection, values: dict) -> int:
    """在调用方的结算事务中落审查记录，避免审查与扣点各自成功一半。"""
    now = time.time()
    row = dict(values)
    row.setdefault("created_at", now)
    row.setdefault("updated_at", now)
    columns = (
        "tenant_id", "kind", "platform", "title", "verdict", "score",
        "issues_json", "report", "created_at", "updated_at",
    )
    cursor = connection.execute(
        "INSERT INTO censor_log(" + ",".join(columns) + ") VALUES("
        + ",".join("?" for _ in columns) + ")",
        tuple(row.get(column) for column in columns),
    )
    return int(cursor.lastrowid)


def _wechat_delivery_hash(job_id: int, theme: str, payload: dict) -> str:
    material = {
        "job_id": job_id,
        "theme": theme,
        "title": payload.get("title") or "",
        "body": payload.get("body") or "",
        "html": payload.get("html") or "",
        "cover": payload.get("cover_rel") or "",
        "images": [item.get("rel") or "" for item in payload.get("images") or []],
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _wechat_delivery_latest(tid: int, job_id: int, request_hash: str):
    return db.one(
        "SELECT * FROM wechat_draft_delivery "
        "WHERE tenant_id=? AND job_id=? AND request_hash=? "
        "ORDER BY id DESC LIMIT 1",
        (tid, job_id, request_hash),
    )


def _wechat_delivery_active(tid: int, job_id: int):
    """按工单找任意未收口投递；不能只看当前正文 hash。"""
    return db.one(
        "SELECT * FROM wechat_draft_delivery "
        "WHERE tenant_id=? AND job_id=? AND status IN "
        "('pending_charge','processing','submitting','submitted') "
        "ORDER BY id DESC LIMIT 1",
        (tid, job_id),
    )


def _public_wechat_delivery(delivery: dict) -> dict:
    now = time.time()
    updated_at = float(delivery.get("updated_at") or delivery.get("created_at") or now)
    age_seconds = max(0, int(now - updated_at))
    status = delivery.get("status") or ""
    can_confirm = (
        status == "submitting"
        and delivery.get("billing_status") == "charged"
        and age_seconds >= 300
    )
    return {
        "id": int(delivery["id"]),
        "job_id": int(delivery["job_id"]),
        "title": delivery.get("title") or "",
        "status": status,
        "billing_status": delivery.get("billing_status") or "",
        "media_id": delivery.get("media_id") or "",
        "age_seconds": age_seconds,
        "needs_reconciliation": status in ("submitting", "submitted"),
        "can_confirm_not_delivered": can_confirm,
        "confirm_wait_seconds": max(0, 300 - age_seconds) if status == "submitting" else 0,
        "updated_at": updated_at,
    }


def _create_wechat_delivery(tid: int, job_id: int, request_hash: str,
                            title: str) -> tuple[dict, bool]:
    import uuid as _uuid

    points = 0.0 if tid == 1 else float(
        (billing.prices().get("wechat_draft") or {"points": 1})["points"]
    )
    request_key = request_hash[:20]
    active = db.one(
        "SELECT * FROM wechat_draft_delivery "
        "WHERE tenant_id=? AND job_id=? "
        "AND status IN ('pending_charge','processing','submitting','submitted') "
        "ORDER BY id DESC LIMIT 1",
        (tid, job_id),
    )
    if active:
        return active, False
    try:
        delivery_id = db.insert("wechat_draft_delivery", {
            "tenant_id": tid,
            "job_id": job_id,
            "request_hash": request_hash,
            "request_key": request_key,
            "title": title[:80],
            "status": "pending_charge",
            "billing_status": "pending",
            "billing_points": points,
        })
    except sqlite3.IntegrityError:
        row = db.one(
            "SELECT * FROM wechat_draft_delivery "
            "WHERE tenant_id=? AND job_id=? "
            "AND status IN "
            "('pending_charge','processing','submitting','submitted') "
            "ORDER BY id DESC LIMIT 1",
            (tid, job_id),
        )
        if not row:
            raise
        return row, False
    op_key = f"wechat-draft:{delivery_id}:{_uuid.uuid4().hex}"

    def claim(connection):
        owner = connection.execute(
            "SELECT id FROM job WHERE id=? AND tenant_id=?",
            (job_id, tid),
        ).fetchone()
        if not owner:
            return False
        other = connection.execute(
            "SELECT id FROM wechat_draft_delivery "
            "WHERE tenant_id=? AND job_id=? AND id<>? "
            "AND status IN "
            "('pending_charge','processing','submitting','submitted') LIMIT 1",
            (tid, job_id, delivery_id),
        ).fetchone()
        if other:
            return False
        changed = connection.execute(
            "UPDATE wechat_draft_delivery SET status='processing',"
            "billing_status='charged',op_key=?,updated_at=? "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (op_key, time.time(), delivery_id),
        )
        return changed.rowcount == 1

    try:
        started = billing.start_operation_if_claimed(
            "wechat_draft",
            tid,
            claim,
            note=title[:20],
            op_key=op_key,
        )
    except Exception:
        db.q(
            "DELETE FROM wechat_draft_delivery WHERE id=? "
            "AND status='pending_charge' AND billing_status='pending'",
            (delivery_id,),
        )
        raise
    if not started:
        db.q(
            "DELETE FROM wechat_draft_delivery WHERE id=? "
            "AND status='pending_charge' AND billing_status='pending'",
            (delivery_id,),
        )
        raise RuntimeError("公众号草稿计费状态冲突")
    return db.one(
        "SELECT * FROM wechat_draft_delivery WHERE id=?", (delivery_id,)
    ), True


def _fail_wechat_delivery(delivery: dict, message: str) -> bool:
    op_key = delivery.get("op_key") or ""
    if not op_key:
        return False

    def claim(connection):
        changed = connection.execute(
            "UPDATE wechat_draft_delivery SET status='failed',"
            "billing_status='refunded',error=?,updated_at=? "
            "WHERE id=? AND op_key=? AND billing_status='charged' "
            "AND status IN ('processing','submitting')",
            ((message or "投递失败")[:300], time.time(), delivery["id"], op_key),
        )
        return changed.rowcount == 1

    failed = billing.fail_operation_if_claimed(op_key, message, claim)
    if failed:
        return True
    current = db.one(
        "SELECT status,billing_status FROM wechat_draft_delivery WHERE id=?",
        (delivery["id"],),
    )
    return bool(
        current
        and current["status"] == "failed"
        and current["billing_status"] == "refunded"
    )


def _complete_blocked_wechat_delivery(delivery: dict, report: dict) -> bool:
    op_key = delivery["op_key"]

    def claim(connection):
        changed = connection.execute(
            "UPDATE wechat_draft_delivery SET status='blocked',"
            "billing_status='succeeded',report_json=?,error=NULL,updated_at=? "
            "WHERE id=? AND op_key=? AND status='processing' "
            "AND billing_status='charged'",
            (
                json.dumps(report, ensure_ascii=False)[:40000],
                time.time(),
                delivery["id"],
                op_key,
            ),
        )
        if changed.rowcount != 1:
            return False
        _insert_censor_log_tx(
            connection,
            censor.check_log_values(
                int(delivery["tenant_id"]),
                delivery.get("title") or "",
                "公众号",
                report,
            ),
        )
        return True

    completed = billing.complete_operation_if_claimed(op_key, claim)
    if completed:
        return True
    current = db.one(
        "SELECT status FROM wechat_draft_delivery WHERE id=?", (delivery["id"],)
    )
    return bool(current and current["status"] == "blocked")


def _mark_wechat_submitted(delivery_id: int, op_key: str, media_id: str) -> bool:
    if not str(media_id or "").strip():
        return False
    return bool(db.execute(
        "UPDATE wechat_draft_delivery SET status='submitted',media_id=?,"
        "error=NULL,updated_at=? WHERE id=? AND op_key=? "
        "AND status='submitting' AND billing_status='charged'",
        (media_id[:160], time.time(), delivery_id, op_key),
    ))


def _finalize_wechat_delivery(delivery: dict) -> bool:
    """外部 media_id、发布台账和计费成功可重复收口且不会再次调用微信。"""
    op_key = delivery.get("op_key") or ""
    if not op_key:
        return False
    now = time.time()

    def claim(connection):
        current = connection.execute(
            "SELECT * FROM wechat_draft_delivery WHERE id=? AND op_key=?",
            (delivery["id"], op_key),
        ).fetchone()
        if not current or current["status"] != "submitted":
            return False
        report = db.jloads(current["report_json"], {}) or {}
        _insert_censor_log_tx(
            connection,
            censor.check_log_values(
                int(current["tenant_id"]),
                current["title"] or "",
                "公众号",
                report,
            ),
        )
        retro = {
            str(day): {"due": now + day * 86400, "state": "pending"}
            for day in pubtrack.RETRO_DAYS
        }
        inserted = connection.execute(
            "INSERT INTO publish_log"
            "(tenant_id,platform,title,job_id,url,source,published_at,retro_json,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                current["tenant_id"],
                "公众号",
                (current["title"] or "")[:80],
                current["job_id"],
                "",
                "draft",
                now,
                json.dumps(retro, ensure_ascii=False),
                now,
                now,
            ),
        )
        changed = connection.execute(
            "UPDATE wechat_draft_delivery SET status='done',"
            "billing_status='succeeded',publish_log_id=?,error=NULL,updated_at=? "
            "WHERE id=? AND op_key=? AND status='submitted' "
            "AND billing_status='charged'",
            (inserted.lastrowid, now, delivery["id"], op_key),
        )
        return changed.rowcount == 1

    completed = billing.complete_operation_if_claimed(op_key, claim)
    if completed:
        return True
    current = db.one(
        "SELECT status FROM wechat_draft_delivery WHERE id=?", (delivery["id"],)
    )
    return bool(current and current["status"] == "done")


def _recover_wechat_deliveries() -> tuple[int, set[str]]:
    """启动恢复安全阶段；可能已到微信的 submitting 操作绝不盲退或重发。"""
    recovered = 0
    db.q(
        "DELETE FROM wechat_draft_delivery "
        "WHERE status='pending_charge' AND billing_status='pending'"
    )
    for row in db.q(
        "SELECT * FROM wechat_draft_delivery "
        "WHERE status='processing' AND billing_status='charged'"
    ):
        if _fail_wechat_delivery(row, "服务重启中断，草稿尚未提交"):
            recovered += 1
    for row in db.q(
        "SELECT * FROM wechat_draft_delivery "
        "WHERE status='submitted' AND billing_status='charged'"
    ):
        try:
            if _finalize_wechat_delivery(row):
                recovered += 1
        except Exception as exc:
            log.error(
                "recover submitted wechat delivery %s failed error_type=%s",
                row["id"],
                type(exc).__name__,
            )
    protected = {
        row["op_key"]
        for row in db.q(
            "SELECT op_key FROM wechat_draft_delivery "
            "WHERE status IN ('submitting','submitted') "
            "AND billing_status='charged' AND op_key IS NOT NULL"
        )
    }
    return recovered, protected


def _wechat_delivery_result(delivery: dict) -> dict:
    report = db.jloads(delivery.get("report_json"), {})
    if delivery.get("status") == "blocked":
        return {
            "ok": False,
            "blocked": True,
            "report": report,
            "note": "审查官拦下了这次发布:存在高危内容,按建议整改后再发",
        }
    return {
        "ok": True,
        "media_id": delivery.get("media_id") or "",
        "report": report,
        "note": "已进入公众号草稿箱:后台预览确认后群发;已登记发布台账,T+1/3/7 审查官自动复盘",
    }


@app.get("/api/jobs/{job_id}/wechat-delivery")
@app.get("/api/jobs/{job_id}/wechat-deliveries")
def job_wechat_deliveries(job_id: int):
    """让用户看见悬而未决的草稿投递，并给人工对账入口。"""
    _need_module("content")
    _job_or_404(job_id)
    rows = db.q(
        "SELECT * FROM wechat_draft_delivery "
        "WHERE tenant_id=? AND job_id=? ORDER BY id DESC LIMIT 20",
        (TEN(), job_id),
    )
    items = [_public_wechat_delivery(row) for row in rows]
    return {
        "items": items,
        "needs_attention": any(item["needs_reconciliation"] for item in items),
    }


def _wechat_delivery_or_404(delivery_id: int) -> dict:
    row = db.one(
        "SELECT * FROM wechat_draft_delivery WHERE id=? AND tenant_id=?",
        (delivery_id, TEN()),
    )
    if not row:
        raise HTTPException(404, "草稿投递记录不存在")
    return row


@app.post("/api/wechat-deliveries/{delivery_id}/reconcile")
async def reconcile_wechat_delivery(delivery_id: int):
    """只读核对微信草稿箱；找到标记才收口，绝不盲目重发。"""
    _need_module("content")
    delivery = _wechat_delivery_or_404(delivery_id)
    if delivery["status"] in ("done", "blocked"):
        return _wechat_delivery_result(delivery)
    if delivery["status"] == "submitted":
        try:
            if not _finalize_wechat_delivery(delivery):
                raise RuntimeError("草稿台账结算状态冲突")
        except Exception as exc:
            raise HTTPException(503, "好消息:文章已经进入公众号草稿箱✅ 系统记录稍后自动补齐,请勿重复发送") from exc
        return _wechat_delivery_result(
            _wechat_delivery_or_404(delivery_id)
        )
    if delivery["status"] != "submitting":
        raise HTTPException(409, "这篇已确认送达公众号草稿箱,不需要再处理")
    try:
        media_id = await wechat.find_draft_by_marker(
            int(delivery["tenant_id"]), delivery["request_key"]
        )
    except Exception as exc:
        raise HTTPException(503, "暂时无法读取公众号草稿箱，请稍后重试") from exc
    if not media_id:
        raise HTTPException(
            409,
            "尚未在最近草稿中找到这篇内容；请打开公众号草稿箱人工确认，"
            "确认确实没有后可使用“确认未送达”解锁",
        )
    if not _mark_wechat_submitted(delivery_id, delivery["op_key"], media_id):
        raise HTTPException(409, "这篇的发送状态刚刚更新了(可能已送达或已退点),刷新页面看最新结果,别重复点发送")
    delivery = _wechat_delivery_or_404(delivery_id)
    try:
        if not _finalize_wechat_delivery(delivery):
            raise RuntimeError("草稿台账结算状态冲突")
    except Exception as exc:
        raise HTTPException(503, "文章已确认在公众号草稿箱✅ 系统记录稍后自动补齐,请勿重复发送") from exc
    return _wechat_delivery_result(_wechat_delivery_or_404(delivery_id))


@app.post("/api/wechat-deliveries/{delivery_id}/confirm-not-delivered")
async def confirm_wechat_delivery_not_delivered(delivery_id: int, body: dict):
    """管理员人工确认微信无草稿后退款解锁；提交前再做一次只读核对。"""
    _need_module("content")
    _need_admin()
    delivery = _wechat_delivery_or_404(delivery_id)
    if delivery["status"] in ("done", "blocked"):
        return _wechat_delivery_result(delivery)
    if (
        delivery["status"] != "submitting"
        or delivery["billing_status"] != "charged"
    ):
        raise HTTPException(409, "这篇的状态不需要人工确认(可能已送达或已退点),刷新页面看最新状态")
    age = time.time() - float(
        delivery.get("updated_at") or delivery.get("created_at") or time.time()
    )
    if age < 300:
        raise HTTPException(
            409, f"微信接口仍可能在处理，请 {max(1, int(300 - age))} 秒后再确认"
        )
    confirmed = body.get("confirmed_no_draft") is True
    title_confirmation = str(body.get("title_confirmation") or "").strip()
    if not confirmed or title_confirmation != str(delivery.get("title") or "").strip():
        raise HTTPException(
            400, "请先打开公众号草稿箱核对，并完整输入文章标题确认未送达"
        )
    try:
        media_id = await wechat.find_draft_by_marker(
            int(delivery["tenant_id"]), delivery["request_key"]
        )
    except Exception as exc:
        raise HTTPException(
            503, "系统无法完成最后一次草稿箱核对，未退款也未解锁"
        ) from exc
    if media_id:
        if not _mark_wechat_submitted(
            delivery_id, delivery["op_key"], media_id
        ):
            raise HTTPException(409, "这篇的发送状态刚刚更新了(可能已送达或已退点),刷新页面看最新结果,别重复点发送")
        delivery = _wechat_delivery_or_404(delivery_id)
        try:
            if not _finalize_wechat_delivery(delivery):
                raise RuntimeError("草稿台账结算状态冲突")
        except Exception as exc:
            raise HTTPException(503, "已在公众号草稿箱里找到这篇文章✅ 系统记录稍后自动补齐,请勿重复发送") from exc
        return _wechat_delivery_result(_wechat_delivery_or_404(delivery_id))
    try:
        settled = _fail_wechat_delivery(
            delivery, "管理员人工核对公众号草稿箱后确认未送达"
        )
    except Exception as exc:
        log.error(
            "manual wechat delivery reconciliation failed id=%s error_type=%s",
            delivery_id,
            type(exc).__name__,
        )
        raise HTTPException(503, "确认操作没有完成(文章不会重复发送),请稍后再点一次") from exc
    if not settled:
        raise HTTPException(409, "这篇的发送状态刚刚更新了(可能已送达或已退点),刷新页面看最新结果,别重复点发送")
    return {
        "ok": False,
        "status": "failed",
        "refunded": True,
        "note": "已确认未送达并退回本次点数，可以重新发送",
    }


@app.get("/api/admin/wechat-delivery-alerts")
def admin_wechat_delivery_alerts():
    """平台管理员可见的历史冲突和长期未对账投递，不包含公众号密钥。"""
    _need_root()
    conflicts = db.jloads(
        db.get_setting("wechat_delivery_migration_conflicts"), []
    ) or []
    rows = db.q(
        "SELECT id,tenant_id,job_id,title,status,billing_status,created_at,updated_at "
        "FROM wechat_draft_delivery WHERE status IN ('submitting','submitted') "
        "OR (status IN ('pending_charge','processing') AND updated_at<?) "
        "ORDER BY updated_at ASC LIMIT 200",
        (time.time() - 300,),
    )
    return {
        "migration_conflicts": conflicts,
        "unresolved": [_public_wechat_delivery(row) | {
            "tenant_id": int(row["tenant_id"])
        } for row in rows],
    }


@app.post("/api/jobs/{job_id}/wechat-draft")
async def job_wechat_draft(job_id: int, body: dict):
    _need_module("content")
    _job_or_404(job_id)
    theme = body.get("theme") or mplayout.DEFAULT_THEME
    tid = TEN()
    conf = wechat.get_conf(tid if tid != 1 else 1)
    if not (conf.get("appid") and conf.get("secret")):
        raise HTTPException(400, "还没配置公众号:去「📣 发布渠道」页填 AppID/AppSecret(1分钟搞定)")
    p = _mp_payload(job_id, theme)
    request_hash = _wechat_delivery_hash(job_id, theme, p)
    # 先处理同工单的任意旧投递。老板若在不确定期间改了正文/hash，也不能
    # 绕过旧锚点再扣一次，必须先核对或人工解锁旧投递。
    existing = (
        _wechat_delivery_active(tid, job_id)
        or _wechat_delivery_latest(tid, job_id, request_hash)
    )
    if existing and existing["status"] in ("done", "blocked"):
        return _wechat_delivery_result(existing)
    if existing and existing["status"] == "submitted":
        try:
            if not _finalize_wechat_delivery(existing):
                raise RuntimeError("草稿台账结算状态冲突")
        except Exception as exc:
            raise HTTPException(
                503, "文章已经进入公众号草稿箱✅ 系统记录稍后自动补齐;请勿再点发送,不会重复扣点"
            ) from exc
        return _wechat_delivery_result(
            db.one("SELECT * FROM wechat_draft_delivery WHERE id=?", (existing["id"],))
        )
    if existing and existing["status"] == "submitting":
        try:
            media_id = await wechat.find_draft_by_marker(
                tid, existing["request_key"]
            )
        except Exception as exc:
            raise HTTPException(
                503, "上次投递结果待确认；请稍后重试，系统不会盲目重复发送"
            ) from exc
        if not media_id:
            raise HTTPException(
                409, "上次投递结果仍待确认；请先查看公众号草稿箱，系统不会重复创建"
            )
        if not _mark_wechat_submitted(
                existing["id"], existing["op_key"], media_id):
            raise HTTPException(409, "这篇的发送状态刚刚更新了,刷新页面看最新结果;确实没送达的话再重试,不会重复扣点")
        existing = db.one(
            "SELECT * FROM wechat_draft_delivery WHERE id=?", (existing["id"],)
        )
        try:
            _finalize_wechat_delivery(existing)
        except Exception as exc:
            raise HTTPException(
                503, "文章已确认在公众号草稿箱✅ 系统记录稍后自动补齐,请勿重复发送"
            ) from exc
        return _wechat_delivery_result(
            db.one("SELECT * FROM wechat_draft_delivery WHERE id=?", (existing["id"],))
        )
    if existing and existing["status"] in ("processing", "pending_charge"):
        raise HTTPException(409, "这篇内容正在发送公众号草稿箱，请勿重复点击")
    try:
        delivery, created = _create_wechat_delivery(
            tid, job_id, request_hash, p["title"]
        )
    except billing.InsufficientPoints as exc:
        raise HTTPException(402, str(exc)) from exc
    if not created:
        raise HTTPException(409, "这篇内容已有一笔投递正在处理，请刷新后重试")
    external_started = False
    try:
        # ① 审查官终审(铁规:发布前必须过审;高危直接拦下)
        report = await censor.check(
            tid,
            p["title"],
            p["body"],
            "公众号",
            token=f"job{job_id}:censor",
            strict=True,
            save=False,
        )
        if report["verdict"] == "block":
            if not _complete_blocked_wechat_delivery(delivery, report):
                raise RuntimeError("草稿审查结算状态冲突")
            return _wechat_delivery_result(
                db.one(
                    "SELECT * FROM wechat_draft_delivery WHERE id=?",
                    (delivery["id"],),
                )
            )
        # ② 正文素材图逐张换成微信 CDN 链接(站外图会被公众号剥掉)
        html = p["html"]
        for im in p["images"][:8]:
            lp = assetfiles.resolve_tenant_asset(
                "/files/" + im["rel"], tid, expected_job_id=job_id,
                allowed_extensions=(".png", ".jpg", ".jpeg", ".webp"),
            )
            try:
                with open(lp, "rb") as f:
                    wx_url = await wechat.upload_content_img(tid, f.read(),
                                                             os.path.basename(lp))
                html = html.replace(im["url"], wx_url)
            except wechat.WeChatError:
                raise
            except Exception as exc:
                log.warning(
                    "draft content image upload failed error_type=%s",
                    type(exc).__name__,
                )
        # ③ 封面 → 永久素材 thumb_media_id(没有可用封面就现画一张)
        cover_lp = (assetfiles.resolve_tenant_asset(
            "/files/" + p["cover_rel"], tid, expected_job_id=job_id,
            allowed_extensions=(".png", ".jpg", ".jpeg"),
        ) if p["cover_rel"] else None)
        if cover_lp:
            with open(cover_lp, "rb") as f:
                cover_bytes = f.read()
        else:
            color = (mplayout.THEMES.get(theme) or {}).get("color", "#ff7f2a")
            cover_bytes = wechat.make_cover_png(p["title"], color)
        thumb_id = await wechat.upload_thumb(tid, cover_bytes)
        # ④ 进草稿箱 + 登记发布台账(T+1/3/7 自动复盘的钟表从此起算)
        marker = f"<!-- paihuo-draft:{delivery['request_key']} -->"
        html = html + "\n" + marker
        changed = db.execute(
            "UPDATE wechat_draft_delivery SET status='submitting',report_json=?,"
            "updated_at=? WHERE id=? AND op_key=? AND status='processing' "
            "AND billing_status='charged'",
            (
                json.dumps(report, ensure_ascii=False)[:40000],
                time.time(),
                delivery["id"],
                delivery["op_key"],
            ),
        )
        if not changed:
            raise RuntimeError("草稿投递状态冲突")
        external_started = True
        try:
            media_id = await wechat.add_draft(
                tid, p["title"], p["digest"], html, thumb_id
            )
        except wechat.WeChatError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 网络异常可能发生在微信已受理之后；先对账，绝不直接退款后盲重试。
            try:
                media_id = await wechat.find_draft_by_marker(
                    tid, delivery["request_key"]
                )
            except Exception:
                media_id = ""
            if not media_id:
                raise HTTPException(
                    503, "微信返回不确定，已暂停重复投递；稍后重试会先自动对账"
                ) from exc
        if not media_id:
            try:
                media_id = await wechat.find_draft_by_marker(
                    tid, delivery["request_key"]
                )
            except Exception:
                media_id = ""
            if not media_id:
                raise HTTPException(
                    503, "微信未返回草稿编号，已暂停重复投递；稍后重试会先自动对账"
                )
        if not _mark_wechat_submitted(
                delivery["id"], delivery["op_key"], media_id):
            raise HTTPException(
                503, "文章已经进入公众号草稿箱✅ 系统记录稍后自动补齐;请勿再点发送,不会重复扣点"
            )
        delivery = db.one(
            "SELECT * FROM wechat_draft_delivery WHERE id=?", (delivery["id"],)
        )
        try:
            if not _finalize_wechat_delivery(delivery):
                raise RuntimeError("草稿台账结算状态冲突")
        except Exception as exc:
            raise HTTPException(
                503, "草稿已进入公众号，平台台账正在补记；稍后重试不会重复发送"
            ) from exc
        return _wechat_delivery_result(
            db.one(
                "SELECT * FROM wechat_draft_delivery WHERE id=?",
                (delivery["id"],),
            )
        )
    except wechat.WeChatError as e:
        try:
            settled = _fail_wechat_delivery(delivery, f"微信明确拒绝:{e}")
        except Exception as settle_error:
            log.error(
                "wechat delivery %s refund failed error_type=%s",
                delivery["id"],
                type(settle_error).__name__,
            )
            raise HTTPException(
                503, "微信拒收了这篇草稿(内容或配置问题)。点数退回正在处理,稍后在账单明细可见,不会多扣"
            ) from settle_error
        if not settled:
            raise HTTPException(503, "微信已拒绝草稿，但退点结算状态待确认")
        raise HTTPException(400, str(e))
    except asyncio.CancelledError:
        if not external_started:
            try:
                _fail_wechat_delivery(delivery, "请求中断，尚未提交微信")
            except Exception as refund_exc:
                log.error(
                    "cancelled wechat delivery %s refund failed error_type=%s",
                    delivery["id"],
                    type(refund_exc).__name__,
                )
        raise
    except HTTPException as exc:
        if not external_started:
            try:
                settled = _fail_wechat_delivery(delivery, "投递前校验失败")
            except Exception as settle_error:
                log.error(
                    "wechat delivery %s refund failed error_type=%s",
                    delivery["id"],
                    type(settle_error).__name__,
                )
                raise HTTPException(
                    503, "草稿未提交，但退点结算暂未完成；请稍后重试"
                ) from settle_error
            if not settled:
                raise HTTPException(503, "这篇没有发出去。点数退回正在处理,稍后可在「套餐与点数」的明细里核对,不会多扣")
        raise exc
    except Exception as exc:
        if not external_started:
            try:
                settled = _fail_wechat_delivery(
                    delivery,
                    "投递前失败，尚未提交微信",
                )
            except Exception as settle_error:
                log.error(
                    "wechat delivery %s refund failed error_type=%s",
                    delivery["id"],
                    type(settle_error).__name__,
                )
                raise HTTPException(
                    503, "草稿未提交，但退点结算暂未完成；请稍后重试"
                ) from settle_error
            if not settled:
                raise HTTPException(503, "这篇没有发出去。点数退回正在处理,稍后可在「套餐与点数」的明细里核对,不会多扣")
            raise HTTPException(500, "发草稿箱失败，点数已退回，请重试") from exc
        raise HTTPException(
            503, "草稿可能已送达微信，系统已停止重复发送；请稍后重试自动对账"
        ) from exc


# ---------------- 发布渠道配置(公众号 + 小红书风格) ----------------
XHS_PRESETS = [
    {"name": "种草安利风", "desc": "闺蜜口吻真诚推荐:开头直击痛点,emoji 适量,短句多换行,结尾抛互动问题", "preset": True},
    {"name": "干货清单风", "desc": "编号清单式,信息密度高:每条一行金句再展开,收藏向内容,标题带数字", "preset": True},
    {"name": "情感共鸣风", "desc": "第一人称讲故事:细节带情绪,先抑后扬,结尾升华引发共鸣与转发", "preset": True},
    {"name": "测评对比风", "desc": "客观测评口吻:优缺点都说,数据说话,给出适用人群与购买建议", "preset": True},
    {"name": "剧情故事风", "desc": "悬念开头像连续剧:口语化对话感强,层层铺剧情,结尾神转折", "preset": True},
    {"name": "专业高冷风", "desc": "行业专家视角:术语准确但讲人话,少 emoji,观点犀利有信息差", "preset": True},
]


DY_PRESETS = [
    {"name": "犀利观点风", "desc": "开头抛反常识观点,语速快信息密,句句带钩子,结尾让人不服来辩", "preset": True},
    {"name": "剧情反转风", "desc": "讲故事设悬念,中段铺垫拉扯,结尾神反转带出主题", "preset": True},
    {"name": "知识科普风", "desc": "三段式:痛点提问→干货拆解(掰手指123)→总结行动指令", "preset": True},
    {"name": "探店vlog风", "desc": "第一视角带逛,口语碎碎念,真实感强,多用'家人们''咱就是说'", "preset": True},
    {"name": "暴躁老板风", "desc": "老板人设吐槽行业内幕,敢说真话,金句频出,略带情绪但不骂人", "preset": True},
    {"name": "温情故事风", "desc": "慢节奏讲人和事,细节动人,结尾升华引共鸣转发", "preset": True},
]


@app.get("/api/pstyles")
def pstyles_get():
    return {"小红书": {"presets": XHS_PRESETS,
                      "custom": db.jloads(db.get_setting(f"xhs_styles:{TEN()}"), []) or []},
            "抖音": {"presets": DY_PRESETS,
                    "custom": db.jloads(db.get_setting(f"dy_styles:{TEN()}"), []) or []}}


@app.put("/api/pstyles")
def pstyles_put(body: dict):
    _need_admin()
    key = {"小红书": "xhs_styles", "抖音": "dy_styles"}.get(body.get("platform"))
    if not key:
        raise HTTPException(400, "平台只支持 小红书/抖音")
    styles = [{"name": str(s.get("name", ""))[:20], "desc": str(s.get("desc", ""))[:200]}
              for s in (body.get("styles") or []) if str(s.get("name", "")).strip()][:20]
    db.set_setting(f"{key}:{TEN()}", json.dumps(styles, ensure_ascii=False))
    return {"ok": True, "n": len(styles)}


@app.get("/api/xhs/styles")
def xhs_styles_get():
    custom = db.jloads(db.get_setting(f"xhs_styles:{TEN()}"), []) or []
    return {"presets": XHS_PRESETS, "custom": custom}


@app.put("/api/xhs/styles")
def xhs_styles_put(body: dict):
    _need_admin()
    styles = [{"name": str(s.get("name", ""))[:20], "desc": str(s.get("desc", ""))[:200]}
              for s in (body.get("styles") or []) if str(s.get("name", "")).strip()][:20]
    db.set_setting(f"xhs_styles:{TEN()}", json.dumps(styles, ensure_ascii=False))
    return {"ok": True, "n": len(styles)}


@app.get("/api/channels/wechat")
def wechat_conf_get():
    _need_admin()
    conf = wechat.get_conf(TEN())

    def mask(v):
        return (v[:4] + "…" + v[-4:]) if v and len(v) > 10 else ("已设置" if v else "")
    return {"appid": conf.get("appid") or "", "secret_masked": mask(conf.get("secret")),
            "secret_set": bool(conf.get("secret")), "server_ip": wechat.SERVER_IP}


@app.put("/api/channels/wechat")
def wechat_conf_put(body: dict):
    _need_admin()
    appid = (body.get("appid") or "").strip()[:40]
    secret = (body.get("secret") or "").strip()[:64]
    if secret and ("…" in secret or secret == "已设置"):
        secret = ""   # 打码回显不覆盖
    wechat.set_conf(TEN(), appid, secret)
    return {"ok": True}


@app.post("/api/channels/wechat/test")
async def wechat_conf_test():
    _need_admin()
    try:
        return await wechat.test_conn(TEN())
    except wechat.WeChatError as e:
        raise HTTPException(400, str(e))


# ---------------- 审查官 ----------------
@app.post("/api/censor/scan")
def censor_scan_api(body: dict):
    """极速扫描(纯规则词库,免费)."""
    _need_module("content")
    text = f"{body.get('title') or ''}\n{body.get('body') or ''}"
    if not text.strip():
        raise HTTPException(400, "先贴上要审查的内容")
    issues = censor.scan(text)
    return {"issues": issues, "verdict": censor._verdict(issues), "score": censor._score(issues),
            "summary": f"极速扫描完成:命中 {len(issues)} 处风险词" if issues else "极速扫描未命中风险词"}


@app.post("/api/censor/check")
async def censor_check_api(body: dict):
    """深度审查(规则词库 + AI 对照平台规范)."""
    _need_module("content")
    tid = TEN()
    title = (body.get("title") or "").strip()[:120]
    text = (body.get("body") or "").strip()
    if not text:
        raise HTTPException(400, "先贴上要审查的正文")
    platform = body.get("platform") or "公众号"
    op_key = _start_billed_operation(
        "censor_check", note=f"{platform}·{title[:14]}"
    )
    try:
        report = await censor.check(
            tid,
            title,
            text,
            platform,
            strict=True,
            save=False,
        )

        def claim(connection):
            _insert_censor_log_tx(
                connection,
                censor.check_log_values(tid, title, platform, report),
            )
            return True

        if not billing.complete_operation_if_claimed(op_key, claim):
            raise RuntimeError("审查结算状态冲突")
    except asyncio.CancelledError:
        try:
            billing.fail_operation(op_key, "审查请求中断")
        except Exception as refund_exc:
            log.error(
                "censor cancellation refund failed op=%s error_type=%s",
                op_key,
                type(refund_exc).__name__,
            )
        raise
    except providers.ProviderError as exc:
        # AI 深审失败照旧退点,但免费的规则词库结果不能陪葬:
        # 老板至少带走这部分,并明确知道深审没跑完、钱退了。
        try:
            settled = billing.fail_operation(op_key, "审查失败自动退回")
        except Exception as settle_error:
            log.error(
                "censor refund failed op=%s error_type=%s",
                op_key,
                type(settle_error).__name__,
            )
            raise HTTPException(
                503, "审查未完成，退点结算正在恢复，请稍后查看"
            ) from settle_error
        if not settled:
            raise HTTPException(503, "审查结算状态待确认，请稍后查看") from exc
        fallback = await censor.check(
            tid, title, text, platform, deep=False, save=False
        )
        fallback["degraded"] = True
        fallback["summary"] = ("AI 深审未完成,1 点已自动退回。"
                               "以下是免费规则词库的扫描结果,可稍后重试深审")
        fallback["label"] = "⚠️ 深审未完成(已退点),下方仅为规则词库结果"
        return fallback
    except Exception as exc:
        try:
            settled = billing.fail_operation(op_key, "审查失败自动退回")
        except Exception as settle_error:
            log.error(
                "censor refund failed op=%s error_type=%s",
                op_key,
                type(settle_error).__name__,
            )
            raise HTTPException(
                503, "审查未完成，退点结算正在恢复，请稍后查看"
            ) from settle_error
        if not settled:
            raise HTTPException(503, "审查结算状态待确认，请稍后查看") from exc
        raise HTTPException(503, "审查官暂时不可用，点数已退回，请重试") from exc
    return report


@app.post("/api/censor/retro")
async def censor_retro_api(body: dict):
    """发后数据复盘:把平台数据丢给审查官."""
    _need_module("content")
    tid = TEN()
    data_text = (body.get("data_text") or "").strip()
    if not data_text:
        raise HTTPException(400, "先把发布后的数据贴进来(阅读/点赞/评论/涨粉…截图可先传文件识别)")
    platform = body.get("platform") or "公众号"
    title = (body.get("title") or "")[:120]
    op_key = _start_billed_operation("censor_retro", note=platform)
    try:
        result = await censor.retro(
            tid,
            platform,
            title,
            data_text,
            body.get("body") or "",
            save=False,
        )

        def claim(connection):
            _insert_censor_log_tx(
                connection,
                censor.retro_log_values(tid, platform, title, result),
            )
            return True

        if not billing.complete_operation_if_claimed(op_key, claim):
            raise RuntimeError("复盘结算状态冲突")
    except asyncio.CancelledError:
        try:
            billing.fail_operation(op_key, "复盘请求中断")
        except Exception as refund_exc:
            log.error(
                "censor retro cancellation refund failed op=%s error_type=%s",
                op_key,
                type(refund_exc).__name__,
            )
        raise
    except Exception as exc:
        try:
            settled = billing.fail_operation(op_key, "复盘失败自动退回")
        except Exception as settle_error:
            log.error(
                "censor retro refund failed op=%s error_type=%s",
                op_key,
                type(settle_error).__name__,
            )
            raise HTTPException(
                503, "复盘未完成，退点结算正在恢复，请稍后查看"
            ) from settle_error
        if not settled:
            raise HTTPException(503, "复盘结算状态待确认，请稍后查看") from exc
        raise HTTPException(503, "复盘没跑完，点数已退回，请重试") from exc
    return result


@app.get("/api/censor/logs")
def censor_logs(
    limit: int = None,
    offset: int = 0,
    kind: str = "",
    platform: str = "",
    q: str = "",
):
    _need_module("content")
    page_limit, page_offset, paged = _pagination(limit, offset, 50)
    where = ["tenant_id=?"]
    params = [TEN()]
    kind = (kind or "").strip()[:20]
    platform = (platform or "").strip()[:40]
    q = (q or "").strip()
    if kind:
        where.append("kind=?")
        params.append(kind)
    if platform:
        where.append("platform=?")
        params.append(platform)
    if q:
        where.append("title LIKE ? ESCAPE '\\'")
        params.append(_like_value(q))
    where_sql = " AND ".join(where)
    rows = db.q(
        f"SELECT * FROM censor_log WHERE {where_sql} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_limit, page_offset),
    )
    for r in rows:
        r["issues"] = db.jloads(r.pop("issues_json"), [])
    if not paged and not any((kind, platform, q)):
        return rows
    total = db.one(
        f"SELECT COUNT(*) AS n FROM censor_log WHERE {where_sql}", tuple(params)
    )["n"]
    return _page_result(rows, total, page_limit, page_offset)


# ---------------- 真实素材图库(全网抓取) ----------------
@app.get("/api/imagehunt")
async def imagehunt_search(q: str, n: int = 24):
    _need_module("content")
    q = (q or "").strip()[:40]
    if not q:
        raise HTTPException(400, "输入要搜的画面关键词")
    return {"query": q, "items": await imagehunt.search(q, min(max(n, 4), 36))}


@app.get("/api/imagehunt/thumb")
async def imagehunt_thumb(u: str, f: str = ""):
    """缩略图代理:第三方图床大多防盗链,浏览器直挂会裂,由服务器带 Referer 取."""
    _need_module("content")
    headers = {"User-Agent": imagehunt.UA_DESKTOP}
    ref = imagehunt._REFERER.get(f)
    if ref:
        headers["Referer"] = ref
    try:
        data, _response_headers = await imagehunt.fetch_public_bytes(
            u, headers=headers, max_bytes=3 * 1024 * 1024, timeout=15)
        if len(data) > 3 * 1024 * 1024 or not imagehunt._looks_image(data):
            raise ValueError("bad image")
        safe_data, media_type = imagehunt.safe_thumbnail_bytes(data)
        return Response(
            safe_data,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=3600",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
            },
        )
    except Exception:
        raise HTTPException(404, "缩略图取不到")


@app.post("/api/jobs/{job_id}/add-image")
async def job_add_image(job_id: int, body: dict):
    """把老板在真实图库挑中的图下载入工单素材,追加到多媒体师产出."""
    _need_module("content")
    _job_or_404(job_id)
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        raise HTTPException(400, "图片地址无效")
    r = db.one("SELECT * FROM station_run WHERE job_id=? AND station_idx=5 "
               "AND status IN ('done','awaiting_review') ORDER BY version DESC LIMIT 1", (job_id,))
    if not r:
        raise HTTPException(400, "该工单的多媒体师还没有产出,等配图工位完成后再补图")
    try:
        data = await imagehunt.fetch_image(
            url, referer=imagehunt._ref_for({"from": body.get("from") or "", "img": url}))
    except Exception:
        raise HTTPException(
            400,
            "这张图抓不下来（可能防盗链较严），请换一张重试",
        )
    name = f"media_add_{int(time.time())}.jpg"
    fpath = imagehunt._save_job_file(job_id, name, data)
    out = db.jloads(r["output_json"], {})
    entry = {"slot": "真实图补充", "desc": (body.get("query") or "")[:40], "platform": "通用",
             "file": fpath, "engine": "真实图·全网抓取", "source": body.get("page") or url}
    out["images"] = (out.get("images") or []) + [entry]
    db.update("station_run", r["id"], {"output_json": json.dumps(out, ensure_ascii=False)})
    engine.touch(job_id)
    return {"ok": True, "image": entry}


# ================ V25:成片 / 台账 / 工具箱 / 矩阵发布 / 通知 ================
from . import growth, matrixpub, notify, pubtrack, textvideo  # noqa: E402


# ---------------- ① 图文转视频 ----------------
def _tv_row(r):
    r["params"] = db.jloads(r.pop("params_json"), {})
    r["params"].pop("body", None)
    r["steps"] = _steps_for_view(
        r.pop("steps_json"), _is_boss(), status=r.get("status")
    )
    r["error"] = _public_failure_for_view(
        r.get("status"),
        r.get("error"),
        _is_boss(),
    )
    return r


def _validated_tv_bgm(body: dict) -> str:
    try:
        return textvideo.validate_bgm(
            body.get("bgm") if isinstance(body, dict) else None
        )
    except ValueError as exc:
        raise HTTPException(400, "配乐风格无效") from exc


def _create_charged_tv_job(params: dict, tenant_id: int = None,
                           job_id: int = None, note: str = "") -> int:
    """先落 pending 工单，再把 queued、扣点和流水原子提交。"""
    params = dict(params or {})
    try:
        params["bgm"] = textvideo.validate_bgm(params.get("bgm"))
    except ValueError as exc:
        raise HTTPException(400, "配乐风格无效") from exc
    tid = int(tenant_id or TEN())
    points = 0.0 if tid == 1 else float(
        (billing.prices().get("text_video") or {"points": 1})["points"])
    data = {
        "tenant_id": tid,
        "job_id": job_id,
        "params_json": json.dumps(params, ensure_ascii=False),
        "status": "pending_charge",
        "billing_status": "pending",
        "billing_points": points,
    }
    tvid = db.insert("tv_job", data)

    def claim(connection):
        changed = connection.execute(
            "UPDATE tv_job SET status='queued',billing_status='charged',"
            "updated_at=? WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (time.time(), tvid),
        )
        return changed.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            "text_video",
            tid,
            claim,
            note=(note or f"成片任务 #{tvid}")[:160],
            points=points,
        )
    except billing.InsufficientPoints as exc:
        db.q(
            "DELETE FROM tv_job WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (tvid,),
        )
        raise HTTPException(402, str(exc)) from exc
    except Exception:
        db.q(
            "DELETE FROM tv_job WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (tvid,),
        )
        raise
    if not charged:
        db.q(
            "DELETE FROM tv_job WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (tvid,),
        )
        raise HTTPException(409, "成片任务已提交，请到任务中心查看")
    return tvid


@app.post("/api/jobs/{job_id}/text-video")
async def job_text_video(job_id: int, body: dict):
    _need_module("content")
    bgm = _validated_tv_bgm(body)
    _job_or_404(job_id)
    d = build_delivery(job_id)
    v = next((x for x in (d.get("versions") or []) if x.get("platform") in ("抖音", "视频号")), None)
    text = (v or {}).get("body") or d["body"]
    if not (text or "").strip():
        raise HTTPException(400, "工单还没有定稿正文")
    images = []
    for image in d.get("images") or []:
        file_url = image.get("file") or ""
        if not file_url.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            continue
        try:
            assetfiles.resolve_tenant_asset(
                file_url, TEN(), expected_job_id=job_id,
                allowed_extensions=(".png", ".jpg", ".jpeg", ".webp"),
            )
        except assetfiles.AssetAccessError as exc:
            raise HTTPException(400, str(exc)) from exc
        images.append(assetfiles.canonical_file_url(file_url))
    params = {
        "title": d["title"],
        "body": text,
        "images": images,
        "voice_id": body.get("voice_id") or "",
        "image_query": d["title"][:16],
        "bgm": bgm,
    }
    tvid = _create_charged_tv_job(
        params,
        tenant_id=TEN(),
        job_id=job_id,
        note=d["title"][:16],
    )
    asyncio.create_task(textvideo.run_job(tvid, engine.broadcast))
    return {"tv_id": tvid}


@app.post("/api/text-video")
async def text_video_create(body: dict):
    """独立成片:①图文成片(口播稿+搜图,原功能)②Vlog混剪(mode=clips,自己的片段+主题/文案)."""
    _need_module("content")
    bgm = _validated_tv_bgm(body)
    script = (body.get("script") or "").strip()
    topic = (body.get("topic") or "").strip()
    mode = body.get("mode") or "images"
    if mode == "clips":
        if not (script or topic):
            raise HTTPException(400, "主题和文案至少填一个")
        raw_clips = body.get("clips")
        raw_clips = raw_clips if isinstance(raw_clips, list) else []
        with textvideo.clip_library_lock(TEN()):
            clips = []
            seen = set()
            for name in raw_clips[:50]:
                path = textvideo.resolve_clip_path(TEN(), name)
                if path and path not in seen:
                    seen.add(path)
                    clips.append(path)
                if len(clips) >= 12:
                    break
            if not clips:
                raise HTTPException(400, "先勾选至少 1 段素材视频")
            params = {"mode": "clips", "clips": clips,
                      "title": (body.get("title") or topic or "")[:40],
                      "script": script[:2000], "topic": topic[:60],
                      "voice_id": body.get("voice_id") or "",
                      "bgm": bgm,
                      "end_text": (body.get("end_text") or "")[:40]}
            # Selection and persistence share the same tenant lock with DELETE,
            # so a paid job can never be born pointing at a just-removed clip.
            tvid = _create_charged_tv_job(
                params,
                tenant_id=TEN(),
                note=(params.get("title") or script or topic)[:16],
            )
    else:
        if len(script) < 20:
            raise HTTPException(400, "口播稿太短(至少20字)")
        params = {"title": (body.get("title") or "")[:40], "script": script[:2000],
                  "voice_id": body.get("voice_id") or "",
                  "image_query": (body.get("image_query") or body.get("title") or "")[:30],
                  "bgm": bgm,
                  "end_text": (body.get("end_text") or "")[:40]}
        tvid = _create_charged_tv_job(
            params,
            tenant_id=TEN(),
            note=(params.get("title") or script or topic)[:16],
        )
    asyncio.create_task(textvideo.run_job(tvid, engine.broadcast))
    return {"tv_id": tvid}


@app.get("/api/text-video")
def text_video_list(
    job_id: int = None,
    limit: int = None,
    offset: int = 0,
    status: str = "",
    q: str = "",
    standalone: bool = False,
):
    _need_module("content")
    legacy_limit = 10 if job_id else 20
    page_limit, page_offset, paged = _pagination(limit, offset, legacy_limit)
    where = ["tenant_id=?"]
    params = [TEN()]
    if job_id:
        where.append("job_id=?")
        params.append(job_id)
    elif standalone:
        where.append("job_id IS NULL")
    status = (status or "").strip()[:30]
    q = (q or "").strip()
    if status:
        where.append("status=?")
        params.append(status)
    if q:
        where.append(
            "json_valid(params_json) AND "
            "json_extract(params_json,'$.title') LIKE ? ESCAPE '\\'"
        )
        params.append(_like_value(q))
    where_sql = " AND ".join(where)
    rows = db.q(
        f"SELECT * FROM tv_job WHERE {where_sql} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_limit, page_offset),
    )
    items = [_tv_row(r) for r in rows]
    if not paged and not any((status, q, standalone)):
        return items
    total = db.one(
        f"SELECT COUNT(*) AS n FROM tv_job WHERE {where_sql}", tuple(params)
    )["n"]
    return _page_result(items, total, page_limit, page_offset)


@app.delete("/api/text-video/{tvid}")
def text_video_delete(tvid: int):
    _need_module("content")
    deleted = textvideo.delete_job(tvid, TEN())
    if deleted is None:
        raise HTTPException(404)
    if not deleted:
        raise HTTPException(409, "任务状态刚刚发生变化，请刷新后再删除")
    return {"ok": True}


# ---------------- ①+ Vlog 混剪素材库 ----------------
@app.post("/api/tv/clips")
async def tv_clip_upload(file: UploadFile = File(...)):
    _need_module("content")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".mp4", ".mov", ".m4v", ".webm"):
        raise HTTPException(400, "只支持 mp4/mov/m4v/webm 视频")
    tid = TEN()
    max_bytes = _CLIP_UPLOAD_MAX_BYTES
    thumb_reserve = 2 * 1024 * 1024
    declared_size = getattr(file, "size", None)
    try:
        declared_size = int(declared_size)
    except (TypeError, ValueError):
        declared_size = 0

    async with _persistent_upload_slot("clip"):
        if declared_size > max_bytes:
            raise HTTPException(413, "单段最大 38MB")
        _assert_persistent_upload_capacity(
            tid,
            max(1, declared_size) + thumb_reserve,
            incoming_files=2,
        )
        d = os.path.join(textvideo.CLIP_ROOT, str(tid))
        if os.path.isdir(d):
            clip_count = 0
            with os.scandir(d) as entries:
                for entry in entries:
                    try:
                        if (
                            not entry.name.startswith(".")
                            and not entry.name.endswith(".jpg")
                            and entry.is_file(follow_symlinks=False)
                        ):
                            clip_count += 1
                    except OSError:
                        continue
            if clip_count >= 20:
                raise HTTPException(400, "素材库最多 20 段,先删几段")

        base_usage = _persistent_upload_usage(tid)
        os.makedirs(d, mode=0o750, exist_ok=True)
        import uuid as _uuid
        name = f"c_{_uuid.uuid4().hex}{'.mp4' if ext == '.m4v' else ext}"
        path = os.path.join(d, name)
        thumb_path = path + ".jpg"
        descriptor, temporary = tempfile.mkstemp(
            prefix=".clip-",
            suffix=".part",
            dir=d,
        )
        temporary_thumb = temporary + ".thumb.jpg"
        published = False
        size = 0
        try:
            os.fchmod(descriptor, 0o640)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(413, "单段最大 38MB")
                    if (
                        base_usage["bytes"] + size + thumb_reserve
                        > int(_PERSISTENT_UPLOAD_TENANT_BYTES)
                    ):
                        raise HTTPException(
                            413,
                            "本企业的上传素材空间已满，请删除不再使用的素材后重试",
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size <= 0:
                raise HTTPException(400, "视频文件为空")

            meta = await asyncio.to_thread(
                textvideo.probe_clip,
                temporary,
                ext,
            )
            try:
                duration = float(meta.get("dur") or 0)
                width = int(meta.get("w") or 0)
                height = int(meta.get("h") or 0)
            except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                raise HTTPException(400, "视频文件无法安全解析") from exc
            if not (1.5 <= duration <= 600):
                if duration > 600:
                    raise HTTPException(
                        400,
                        "单段最长 10 分钟,手机里先粗剪一下",
                    )
                raise HTTPException(
                    400,
                    "这段视频读不出来或太短(至少1.5秒)",
                )
            if (
                width <= 0
                or height <= 0
                or width > avatar.MAX_UPLOAD_IMAGE_DIMENSION
                or height > avatar.MAX_UPLOAD_IMAGE_DIMENSION
                or width * height > avatar.MAX_UPLOAD_IMAGE_PIXELS
            ):
                raise HTTPException(400, "视频画面尺寸无法安全解析")

            await asyncio.to_thread(
                textvideo.make_thumb,
                temporary,
                temporary_thumb,
                max(0.5, duration / 2),
            )
            if (
                os.path.islink(temporary_thumb)
                or not os.path.isfile(temporary_thumb)
                or os.path.getsize(temporary_thumb) <= 0
            ):
                raise HTTPException(400, "视频缩略图生成失败")
            try:
                with open(temporary_thumb, "rb") as handle:
                    avatar.validate_upload_media(
                        handle.read(5 * 1024 * 1024 + 1),
                        ".jpg",
                        "photo",
                    )
            except (
                OSError,
                avatar.InvalidAvatarMedia,
            ) as exc:
                raise HTTPException(400, "视频缩略图生成失败") from exc
            if os.path.getsize(temporary_thumb) > 5 * 1024 * 1024:
                raise HTTPException(400, "视频缩略图生成失败")

            with textvideo.clip_library_lock(tid):
                current_usage = _persistent_upload_usage(tid)
                if (
                    current_usage["bytes"]
                    > int(_PERSISTENT_UPLOAD_TENANT_BYTES)
                    or current_usage["files"]
                    > int(_PERSISTENT_UPLOAD_TENANT_FILES)
                ):
                    raise HTTPException(
                        413,
                        "本企业的上传素材空间已满，请删除不再使用的素材后重试",
                    )
                os.chmod(temporary_thumb, 0o640)
                os.replace(temporary, path)
                temporary = ""
                os.replace(temporary_thumb, thumb_path)
                temporary_thumb = ""
                published = True
            return {
                "name": name,
                "dur": round(duration, 1),
                "file": f"/files/tvclips/{tid}/{name}",
                "thumb": f"/files/tvclips/{tid}/{name}.jpg",
            }
        except HTTPException:
            raise
        except Exception as exc:
            log.warning(
                "clip upload validation failed tenant=%s error_type=%s",
                tid,
                type(exc).__name__,
            )
            raise HTTPException(400, "视频文件无法安全解析") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            for candidate in (
                temporary,
                temporary_thumb,
                "" if published else path,
                "" if published else thumb_path,
            ):
                if not candidate:
                    continue
                try:
                    os.remove(candidate)
                except FileNotFoundError:
                    pass
                except OSError:
                    log.warning(
                        "clip upload cleanup failed tenant=%s",
                        tid,
                    )


@app.get("/api/tv/clips")
def tv_clips_list():
    _need_module("content")
    tid = TEN()
    with textvideo.clip_library_lock(tid):
        d = textvideo.clip_dir(tid)
        clips = []
        with os.scandir(d) as entries:
            for entry in entries:
                try:
                    path = textvideo.resolve_clip_path(tid, entry.name)
                except OSError:
                    path = None
                if path:
                    clips.append((entry.name, path))
    out = []
    for name, path in sorted(clips):
        meta = textvideo.probe_clip(path)
        if (
            float(meta.get("dur") or 0) <= 0
            or int(meta.get("w") or 0) <= 0
            or int(meta.get("h") or 0) <= 0
        ):
            continue
        out.append({"name": name, "dur": round(meta["dur"], 1),
                    "file": f"/files/tvclips/{tid}/{name}",
                    "thumb": f"/files/tvclips/{tid}/{name}.jpg"})
    return out


@app.delete("/api/tv/clips/{name}")
def tv_clip_delete(name: str):
    _need_module("content")
    if not textvideo.valid_clip_name(name):
        raise HTTPException(400, "素材文件名无效")
    tid = TEN()
    with textvideo.clip_library_lock(tid):
        d = textvideo.clip_dir(tid)
        path = os.path.join(d, name)
        with db.atomic() as connection:
            referenced = connection.execute(
                "SELECT j.id FROM tv_job AS j "
                "JOIN json_each("
                "CASE WHEN json_valid(j.params_json) "
                "THEN j.params_json ELSE '{}' END,'$.clips'"
                ") AS clip "
                "WHERE j.tenant_id=? "
                "AND j.status IN ('pending_charge','queued','running') "
                "AND clip.type='text' AND clip.value=? LIMIT 1",
                (tid, path),
            ).fetchone()
        if referenced:
            raise HTTPException(
                409,
                "该素材正被排队或进行中的成片任务使用，暂时不能删除",
            )
        for candidate in (path, path + ".jpg"):
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                continue
            if not (
                os.path.isfile(candidate)
                and not os.path.islink(candidate)
            ):
                raise HTTPException(409, "素材文件状态异常，未执行删除")
            try:
                os.remove(candidate)
            except OSError as exc:
                raise HTTPException(409, "素材删除失败，请稍后重试") from exc
    return {"ok": True}


# ---------------- ② 发布台账 ----------------
@app.get("/api/publog")
def publog_list():
    _need_module("content")
    return pubtrack.entries(TEN())


@app.get("/api/publog/auto-retro")
def publog_auto_retro_get():
    _need_module("content")
    return {"enabled": pubtrack.auto_enabled(TEN())}


@app.post("/api/publog/auto-retro")
def publog_auto_retro_set(body: dict):
    """租户级自动复盘总开关;只有企业主能动钱袋子相关的开关。"""
    _need_admin()
    _need_module("content")
    enabled = bool(body.get("enabled"))
    pubtrack.set_auto_enabled(TEN(), enabled)
    return {"ok": True, "enabled": enabled}


@app.post("/api/publog")
def publog_add(body: dict):
    _need_module("content")
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "填一下发布的标题")
    days_ago = min(max(int(body.get("days_ago") or 0), 0), 30)
    pid = pubtrack.add_entry(TEN(), body.get("platform") or "公众号", title,
                             url=(body.get("url") or "")[:200],
                             published_at=time.time() - days_ago * 86400)
    return {"id": pid}


@app.delete("/api/publog/{pid}")
def publog_del(pid: int):
    _need_module("content")
    try:
        if not pubtrack.delete_entry(TEN(), pid):
            raise HTTPException(404)
    except pubtrack.RetroBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.post("/api/publog/{pid}/published")
def publog_mark(pid: int):
    _need_module("content")
    try:
        pubtrack.mark_published(TEN(), pid)
    except pubtrack.RetroBusy as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.post("/api/publog/{pid}/pull")
async def publog_pull(pid: int):
    """立即尝试自动拉数据+复盘(公众号需配好API且文章已群发)."""
    _need_module("content")
    r = db.one("SELECT * FROM publish_log WHERE id=? AND tenant_id=?", (pid, TEN()))
    if not r:
        raise HTTPException(404)
    days = max(1, round((time.time() - (r["published_at"] or time.time())) / 86400))
    ok = await pubtrack.auto_retro(TEN(), r, days)
    if not ok:
        raise HTTPException(400, "自动拉数据没成功:①需要认证服务号+配好API ②文章要已群发(不是草稿) "
                                 "③其他平台请去审查官工作台手动贴数据复盘")
    return {"ok": True, "note": "已自动拉取数据并复盘,报告在审查官工作台「审查记录」里"}


# ---------------- ③ 营销工具箱(长任务=后台作业:挂起可回看,关页面不丢) ----------------
TOOL_KINDS = {"hot": "今日必发", "pcal": "私域日历", "warm": "起号军师",
              "leads": "线索雷达", "bench": "竞品盯梢"}
TOOL_REFUND = {"hot": "hot_pick", "pcal": "pcal", "warm": "warmup",
               "leads": "leads", "bench": "bench_watch"}
TOOL_TIMEOUTS = {"hot": 300, "pcal": 300, "warm": 360, "leads": 360, "bench": 360}
TOOL_STALE_GRACE = 60
_TOOL_TASKS = set()
_TOOL_WATCHDOG_TASK = None


def _broadcast_tool(tid: int, kind: str):
    try:
        engine.broadcast({"type": "tool_update", "tenant_id": tid, "kind": kind})
    except Exception as exc:
        try:
            log.error(
                "tool_job broadcast failed tenant=%s kind=%s error_type=%s",
                tid,
                kind,
                type(exc).__name__,
            )
        except Exception:
            pass


def _fail_tool_job(row: dict, error: str, refund_note: str = "后台任务失败退回") -> bool:
    """CAS 抢占失败状态，并在同一个 SQLite 事务里完成退款，重复调用安全。"""
    jid, tid, kind = row["id"], row["tenant_id"], row["kind"]
    message = (str(error or "后台任务失败").strip() or "后台任务失败")[:200]
    now = time.time()

    def claim(c):
        cur = c.execute(
            "UPDATE tool_job SET status='failed',billing_status='refunded',"
            "error=?,progress=?,updated_at=? "
            "WHERE id=? AND status='running' AND billing_status='charged'",
            (message, "任务已结束，可重新发起", now, jid),
        )
        return cur.rowcount == 1

    points = row.get("billing_points")
    if points is None:  # 仅兼容升级前仍在 running 的旧记录。
        action = TOOL_REFUND.get(kind, "expert_task")
        points = float((billing.prices().get(action) or {"points": 1})["points"])
    return billing.refund_amount_if_claimed(
        tid, points, claim, f"退回:{refund_note}"
    )


def _settle_tool_failure(row: dict, error: str, refund_note: str) -> bool:
    """worker 的防火墙：结算暂时失败时保留 running，交给看门狗稍后重试。"""
    try:
        return _fail_tool_job(row, error, refund_note)
    except Exception as exc:
        try:
            log.error(
                "settle tool_job %s failure failed error_type=%s",
                row.get("id"),
                type(exc).__name__,
            )
        except Exception:
            pass
        return False


def _recover_interrupted_tool_jobs():
    """服务重启时，旧进程留下的 running 已不可能继续，立即收口并退点。"""
    # pending_charge 从未扣款，直接清理；不能把它误当成已付费任务退款。
    db.q(
        "DELETE FROM tool_job "
        "WHERE status='pending_charge' AND billing_status='pending'"
    )
    for row in db.q("SELECT * FROM tool_job WHERE status='running'"):
        try:
            if _fail_tool_job(row, "服务重启中断，已自动结束，请重新发起", "重启中断退回"):
                _broadcast_tool(row["tenant_id"], row["kind"])
        except Exception as exc:
            try:
                log.error(
                    "recover interrupted tool_job %s failed error_type=%s",
                    row["id"],
                    type(exc).__name__,
                )
            except Exception:
                pass


def _ensure_tool_running_index():
    """同租户同工具只允许一条待扣款或运行记录，堵住并发双击窗口。"""
    db.execute("DROP INDEX IF EXISTS idx_tool_job_one_running")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_job_one_active "
        "ON tool_job(tenant_id, kind) "
        "WHERE status IN ('pending_charge','running')"
    )


def _recover_stale_tool_jobs(now: float = None) -> int:
    """按创建时间执行绝对总时限；心跳只供展示，不能把截止时间越续越长。"""
    now = now or time.time()
    recovered = 0
    for row in db.q("SELECT * FROM tool_job WHERE status='running'"):
        timeout = TOOL_TIMEOUTS.get(row["kind"], 360)
        # 免费重试沿用原记录，必须从本次 retry_started_at 重新计时；否则历史
        # created_at 会让刚重排的任务被看门狗立即判为超时。
        started_at = (
            row.get("retry_started_at")
            or row.get("created_at")
            or row.get("updated_at")
            or now
        )
        if now - started_at <= timeout + TOOL_STALE_GRACE:
            continue
        minutes = max(1, round(timeout / 60))
        try:
            if _fail_tool_job(
                    row, f"运行超过{minutes}分钟仍未完成，已自动结束并退回点数，请重试",
                    "超时自动退回"):
                recovered += 1
                _broadcast_tool(row["tenant_id"], row["kind"])
        except Exception as exc:
            try:
                log.error(
                    "recover stale tool_job %s failed error_type=%s",
                    row["id"],
                    type(exc).__name__,
                )
            except Exception:
                pass
    return recovered


async def _tool_watchdog_loop():
    while True:
        try:
            await asyncio.sleep(60)
            _recover_stale_tool_jobs()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            try:
                log.error(
                    "tool_job watchdog failed error_type=%s",
                    type(exc).__name__,
                )
            except Exception:
                pass


def _start_tool_watchdog():
    global _TOOL_WATCHDOG_TASK
    if _TOOL_WATCHDOG_TASK is None or _TOOL_WATCHDOG_TASK.done():
        _TOOL_WATCHDOG_TASK = asyncio.create_task(_tool_watchdog_loop())


def _spawn_tool_worker(jid: int):
    """保留后台 task 的强引用，直到它真正收口，避免被事件循环提前回收。"""
    task = asyncio.create_task(_tool_worker(jid))
    _TOOL_TASKS.add(task)

    def finished(done):
        _TOOL_TASKS.discard(done)
        if done.cancelled():
            return
        try:
            error = done.exception()
        except (asyncio.CancelledError, Exception):
            return
        if error:
            try:
                log.error(
                    "tool_job %s worker escaped error_type=%s",
                    jid,
                    type(error).__name__,
                )
            except Exception:
                pass

    task.add_done_callback(finished)
    return task


async def _run_tool(row: dict, progress) -> dict:
    tid, kind = row["tenant_id"], row["kind"]
    p = db.jloads(row["params_json"], {})
    if kind == "hot":
        return await growth.hot_pick(tid, p.get("industry") or "通用",
                                     p.get("channels") or [], save=False)
    if kind == "pcal":
        return await growth.private_calendar(tid, p.get("industry") or "通用",
                                             p.get("focus") or "", p["ym"],
                                             save=False)
    if kind == "warm":
        return await growth.warmup_plan(
            tid, p.get("platform") or "小红书", p.get("industry") or "通用",
            p.get("positioning") or "", "", persona_text=p.get("persona_text") or ""
        )
    if kind == "leads":
        return await growth.leads_radar(
            tid, p.get("industry") or "通用", p.get("city") or "",
            p.get("product") or "", progress=progress
        )
    if kind == "bench":
        return await growth.bench_report(tid, save=False)
    raise ValueError("未知工具")


def _persist_tool_result(connection, row: dict, result: dict, now: float) -> bool:
    """业务结果、可读缓存与计费成功在同一事务里出现。"""
    jid, tid, kind = row["id"], row["tenant_id"], row["kind"]
    changed = connection.execute(
        "UPDATE tool_job SET status='done',result_json=?,error=NULL,progress=?,"
        "billing_status='succeeded',updated_at=? "
        "WHERE id=? AND status='running' AND billing_status='charged'",
        (
            json.dumps(result, ensure_ascii=False),
            "任务已完成",
            now,
            jid,
        ),
    )
    if changed.rowcount != 1:
        return False
    params = db.jloads(row.get("params_json"), {})
    settings = []
    if kind == "pcal":
        ym = str(params.get("ym") or "")[:7]
        settings.append((
            f"pcal:{tid}:{ym}",
            json.dumps(result, ensure_ascii=False),
        ))
    elif kind == "hot":
        date = str(result.get("date") or "")[:10]
        industry = str(result.get("industry") or params.get("industry") or "通用")[:20]
        channels = result.get("channels") if isinstance(result.get("channels"), list) else []
        settings.extend((
            (f"hotpick_channels:{tid}", json.dumps(channels, ensure_ascii=False)),
            (
                f"hotpick:{tid}:{date}:{industry}",
                json.dumps(result, ensure_ascii=False),
            ),
        ))
    elif kind == "bench":
        key = f"bench_watch:{tid}"
        current = connection.execute(
            "SELECT value FROM app_setting WHERE key=?", (key,)
        ).fetchone()
        conf = db.jloads(current["value"], {}) if current else {}
        conf["last_run"] = now
        settings.append((key, json.dumps(conf, ensure_ascii=False)))
    for key, value in settings:
        connection.execute(
            "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            "updated_at=excluded.updated_at",
            (key, value, now),
        )
    return True


async def _tool_worker(jid: int):
    try:
        row = db.one("SELECT * FROM tool_job WHERE id=?", (jid,))
    except Exception as exc:
        try:
            log.error(
                "tool_job %s initial read failed; watchdog will retry "
                "error_type=%s",
                jid,
                type(exc).__name__,
            )
        except Exception:
            pass
        return
    if not row or row["status"] != "running":
        return
    tid, kind = row["tenant_id"], row["kind"]
    progress_last = {"at": 0.0, "label": ""}

    def progress(_step: str, label: str):
        # 步骤上报是旁路能力：限频写心跳，任何异常都不能打断真实任务。
        now = time.time()
        label = (str(label or "正在处理").strip() or "正在处理")[:160]
        if label == progress_last["label"] and now - progress_last["at"] < 15:
            return
        if now - progress_last["at"] < 8:
            return
        try:
            db.execute(
                "UPDATE tool_job SET progress=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (label, now, jid),
            )
            progress_last.update({"at": now, "label": label})
        except Exception:
            pass

    try:
        progress("boot", f"{TOOL_KINDS.get(kind, kind)}已接单，正在启动…")
        r = await asyncio.wait_for(
            _run_tool(row, progress), timeout=TOOL_TIMEOUTS.get(kind, 360)
        )
        if not isinstance(r, dict):
            raise ValueError("工具没有返回有效结果")
        r = dict(r)
        r.pop("cost_usd", None)
        r.pop("tokens", None)
        with db.atomic() as connection:
            changed = _persist_tool_result(connection, row, r, time.time())
        if changed:
            try:
                notify.push(
                    tid, "report",
                    {"report_name": f"{TOOL_KINDS.get(kind, kind)}跑完了",
                     "summary": "结果已经摆在工具箱里,回来就能看", "link": "#/tools"},
                )
            except Exception as exc:
                try:
                    log.error(
                        "tool_job %s notification failed error_type=%s",
                        jid,
                        type(exc).__name__,
                    )
                except Exception:
                    pass
    except asyncio.TimeoutError:
        minutes = max(1, round(TOOL_TIMEOUTS.get(kind, 360) / 60))
        _settle_tool_failure(
            row, f"运行超过{minutes}分钟仍未完成，已自动结束并退回点数，请重试",
            "超时自动退回"
        )
        try:
            log.warning("tool_job %s(%s) timed out", jid, kind)
        except Exception:
            pass
    except asyncio.CancelledError:
        _settle_tool_failure(
            row, "任务被服务中断，已自动结束，请重新发起", "服务中断退回"
        )
        raise
    except Exception as e:
        # 先收口状态和退款，再记日志；即使日志组件自身出错，用户也不会再看到永久 running。
        public_error = providers.public_failure_message(e)
        _settle_tool_failure(
            row, public_error, "后台任务失败退回"
        )
        try:
            log.error(
                "tool_job %s(%s) failed error_type=%s",
                jid,
                kind,
                type(e).__name__,
            )
        except Exception:
            pass
    finally:
        _broadcast_tool(tid, kind)


def _tool_require_idle(kind: str):
    if db.one("SELECT id FROM tool_job WHERE tenant_id=? AND kind=? "
              "AND status IN ('pending_charge','running')",
              (TEN(), kind)):
        raise HTTPException(429, "这个工具已有一个任务在后台跑,等它完事再派新的")


def _tool_enqueue(kind: str, params: dict, note: str = "") -> dict:
    """先落任务再原子扣点；任何插入/并发失败都不会碰用户余额。"""
    tid = TEN()
    action = TOOL_REFUND.get(kind, "expert_task")
    points = 0.0 if tid == 1 else float(
        (billing.prices().get(action) or {"points": 1})["points"]
    )
    try:
        jid = db.insert("tool_job", {
            "tenant_id": tid, "kind": kind,
            "params_json": json.dumps(params, ensure_ascii=False),
            "status": "pending_charge",
            "billing_status": "pending",
            "billing_points": points,
            "progress": "任务已进入后台队列",
        })
    except sqlite3.IntegrityError:
        raise HTTPException(429, "这个工具已有一个任务在后台跑，等它完成后再试")

    def claim(connection):
        changed = connection.execute(
            "UPDATE tool_job SET status='running',billing_status='charged',updated_at=? "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (time.time(), jid),
        )
        return changed.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            action, tid, claim, note=note, points=points
        )
    except billing.InsufficientPoints as exc:
        db.q(
            "DELETE FROM tool_job WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (jid,),
        )
        raise HTTPException(402, str(exc)) from exc
    except Exception:
        db.q(
            "DELETE FROM tool_job WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (jid,),
        )
        raise
    if not charged:
        raise RuntimeError("工具任务计费状态冲突")
    _spawn_tool_worker(jid)
    return {"job_id": jid, "note": "已挂到后台跑:您随便去忙别的,回工具箱就能看到;跑完还会推微信"}


@app.get("/api/tools/jobs")
def tool_jobs(
    limit: int = None,
    offset: int = 0,
    kind: str = "",
    status: str = "",
):
    _need_module("content")
    page_limit, page_offset, paged = _pagination(limit, offset, 15)
    where = ["tenant_id=?"]
    params = [TEN()]
    kind = (kind or "").strip()[:30]
    status = (status or "").strip()[:30]
    if kind:
        where.append("kind=?")
        params.append(kind)
    if status:
        where.append("status=?")
        params.append(status)
    where_sql = " AND ".join(where)
    rows = db.q(
        f"SELECT * FROM tool_job WHERE {where_sql} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_limit, page_offset),
    )
    items = []
    for r in rows:
        k = r["kind"]
        items.append({
            "id": r["id"], "kind": k, "status": r["status"],
            "error": _public_failure_for_view(
                r.get("status"), r.get("error"), _is_boss()),
            "params": db.jloads(r["params_json"], {}),
            "result": db.jloads(r["result_json"], None)
            if r["status"] == "done" else None,
            "progress": _public_progress_for_view(
                r.get("status"), r.get("progress"), _is_boss()),
            "created_at": r["created_at"], "updated_at": r.get("updated_at"),
            "timeout_seconds": TOOL_TIMEOUTS.get(k, 360),
        })
    if not paged and not any((kind, status)):
        latest = {}
        for item in items:
            latest.setdefault(item["kind"], item)
        return list(latest.values())
    total = db.one(
        f"SELECT COUNT(*) AS n FROM tool_job WHERE {where_sql}", tuple(params)
    )["n"]
    return _page_result(items, total, page_limit, page_offset)
@app.get("/api/tools/meta")
def tools_meta():
    _need_module("content")
    return {"festivals": growth.upcoming_festivals(30), "voices": avatar.VOICES,
            "cloned": avatar.cloned_voices(),
            "bench": growth.watch_conf(TEN()),
            "hot_channels": growth.HOT_CHANNELS,
            "hot_channels_saved": growth.hot_channels_saved(TEN()),
            "hot_daily": growth.hot_daily_conf(TEN()),
            "bgm_moods": [{"key": k, "label": v["label"]} for k, v in
                          __import__("app.textvideo", fromlist=["x"]).BGM_MOODS.items()]
                         + [{"key": "none", "label": "不配乐"}],
            "industries": INDUSTRIES}


@app.get("/api/tools/pcal")
def pcal_get(ym: str):
    _need_module("content")
    return growth.get_calendar(TEN(), ym) or {}


@app.post("/api/tools/pcal")
async def pcal_gen(body: dict):
    _need_module("content")
    ym = body.get("ym") or time.strftime("%Y-%m")
    _tool_require_idle("pcal")
    return _tool_enqueue("pcal", {"ym": ym, "industry": (body.get("industry") or "通用")[:20],
                                  "focus": (body.get("focus") or "")[:200]}, note=ym)


@app.put("/api/tools/pcal")
def pcal_edit(body: dict):
    _need_module("content")
    try:
        growth.save_calendar_edits(TEN(), body.get("ym") or "", body.get("days") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/tools/pcal/feishu")
async def pcal_feishu(body: dict):
    _need_module("content")
    try:
        return await growth.calendar_to_feishu(TEN(), body.get("ym") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/tools/hot-daily")
def hot_daily_put(body: dict):
    _need_module("content")
    conf = growth.save_hot_daily(TEN(), body.get("enabled"),
                                 body.get("industry") or "通用", body.get("channels") or [])
    return {"ok": True, "enabled": conf["enabled"]}


@app.get("/api/tools/hotpick")
def hotpick_get(industry: str = "通用"):
    _need_module("content")
    return growth.get_hot_pick(TEN(), industry) or {"festivals": growth.upcoming_festivals(7)}


@app.post("/api/tools/hotpick")
async def hotpick_gen(body: dict):
    _need_module("content")
    industry = (body.get("industry") or "通用")[:20]
    _tool_require_idle("hot")
    return _tool_enqueue("hot", {"industry": industry,
                                 "channels": (body.get("channels") or [])[:10]},
                         note=industry)


@app.post("/api/tools/warmup")
async def warmup_gen(body: dict):
    _need_module("content")
    _tool_require_idle("warm")
    persona_text = ""
    if body.get("profile_id"):
        pr = db.one("SELECT * FROM account_profile WHERE id=? AND tenant_id=? "
                    "AND deleted_at IS NULL",
                    (body["profile_id"], TEN()))
        if pr:
            persona_text = registry._persona_text({"persona": db.jloads(pr["persona_json"], {})})
    return _tool_enqueue("warm", {"platform": body.get("platform") or "小红书",
                                  "industry": (body.get("industry") or "通用")[:20],
                                  "positioning": (body.get("positioning") or "")[:200],
                                  "persona_text": persona_text[:1500]},
                         note=(body.get("platform") or "")
                              + (body.get("industry") or ""))


@app.post("/api/tools/leads")
async def leads_gen(body: dict):
    _need_module("content")
    _tool_require_idle("leads")
    return _tool_enqueue("leads", {"industry": (body.get("industry") or "通用")[:20],
                                   "city": (body.get("city") or "")[:20],
                                   "product": (body.get("product") or "")[:60]},
                         note=(body.get("city") or "")
                              + (body.get("industry") or ""))


@app.get("/api/tools/bench")
def bench_get():
    _need_module("content")
    conf = growth.watch_conf(TEN())
    return conf


@app.put("/api/tools/bench")
def bench_put(body: dict):
    _need_module("content")
    targets = growth.save_watch(TEN(), body.get("targets") or [], body.get("enabled"))
    return {"ok": True, "n": len(targets)}


@app.post("/api/tools/bench/run-now")
async def bench_run(body: dict = None):
    _need_module("content")
    if not growth.watch_conf(TEN()).get("targets"):
        raise HTTPException(400, "先在上面添加要盯的对标账号并保存")
    _tool_require_idle("bench")
    return _tool_enqueue("bench", {}, note="手动")


@app.post("/api/tools/menu-copy")
async def menu_copy_api(file: _UploadFile = _File(...), want: str = Form("")):
    _need_module("content")
    raw = await _read_limited(file, 8 * 1024 * 1024, "图片太大(≤8MB)")
    op_key = _start_billed_operation("menu_copy")
    import base64 as _b64
    mime = file.content_type if (file.content_type or "").startswith("image/") else "image/jpeg"
    try:
        result = await growth.menu_copy(
            TEN(), _b64.b64encode(raw).decode(), mime, want
        )
        if not billing.complete_operation(op_key):
            raise RuntimeError("计费操作状态冲突")
        return result
    except asyncio.CancelledError:
        billing.fail_operation(op_key, "请求中断自动退回")
        raise
    except Exception:
        billing.fail_operation(op_key, "识图失败自动退回")
        raise HTTPException(500, "识图失败,点数已退回,换张清晰的图试试")


@app.post("/api/tools/product-shot")
async def product_shot_api(file: _UploadFile = _File(...), scene: str = Form("")):
    _need_module("content")
    raw = await _read_limited(file, 8 * 1024 * 1024, "图片太大(≤8MB)")
    op_key = _start_billed_operation("product_shot")
    path = ""
    try:
        data = await growth.product_shot(TEN(), raw, scene)
        import uuid as _uuid
        d = os.path.join(ROOT, "data", "assets", "tools", str(TEN()))
        os.makedirs(d, exist_ok=True)
        name = f"shot_{_uuid.uuid4().hex[:10]}.png"
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if not billing.complete_operation(op_key):
            raise RuntimeError("计费操作状态冲突")
        return {"file": f"/files/tools/{TEN()}/{name}"}
    except asyncio.CancelledError:
        billing.fail_operation(op_key, "请求中断自动退回")
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    except Exception:
        billing.fail_operation(op_key, "商品图生成或保存失败自动退回")
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        raise HTTPException(500, "美化失败，点数已退回，请稍后重试")


@app.post("/api/tools/photo-factory")
async def photo_factory_api(file: _UploadFile = _File(...), scene: str = Form(""),
                            want: str = Form("")):
    """拍照工厂:一次上传同时出「商业海报图 + 全套文案」。

    此前前端并行调 product-shot 与 menu-copy 两个接口,同一张 8MB 照片要上传
    两遍(手机 4G 下时间翻倍)。合并为一次上传、服务器侧并发跑两条腿;
    两条腿各自独立计费与退款,哪条失败退哪条的点,响应里把每条腿的结果
    与失败原因分开说清,老板不用猜"钱花在哪了"。
    """
    _need_module("content")
    raw = await _read_limited(file, 8 * 1024 * 1024, "图片太大(≤8MB)")
    import base64 as _b64
    mime = file.content_type if (file.content_type or "").startswith("image/") else "image/jpeg"
    b64 = _b64.b64encode(raw).decode()

    # 两条腿的计费操作先后开好:第二条点数不足时退掉第一条,给合并后的
    # 提示,而不是让老板看到"本次需 1 点"这种只说半截的话。
    shot_op = _start_billed_operation("product_shot")
    try:
        copy_op = _start_billed_operation("menu_copy")
    except HTTPException as exc:
        billing.fail_operation(shot_op, "拍照工厂另一半点数不足,整体退回")
        if exc.status_code == 402:
            raise HTTPException(
                402, "拍照工厂一次需 3 点(出图2+文案1),当前余额不足。请充值后再试"
            ) from exc
        raise

    async def _shot_leg(op_key):
        path = ""
        try:
            data = await growth.product_shot(TEN(), raw, scene)
            import uuid as _uuid
            d = os.path.join(ROOT, "data", "assets", "tools", str(TEN()))
            os.makedirs(d, exist_ok=True)
            name = f"shot_{_uuid.uuid4().hex[:10]}.png"
            path = os.path.join(d, name)
            with open(path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if not billing.complete_operation(op_key):
                raise RuntimeError("计费操作状态冲突")
            return {"file": f"/files/tools/{TEN()}/{name}"}
        except asyncio.CancelledError:
            billing.fail_operation(op_key, "请求中断自动退回")
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise
        except Exception:
            billing.fail_operation(op_key, "商品图生成或保存失败自动退回")
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return {"error": "美化没成功,这条腿的 2 点已退回;可换张更清晰的图重试"}

    async def _copy_leg(op_key):
        try:
            result = await growth.menu_copy(TEN(), b64, mime, want)
            if not billing.complete_operation(op_key):
                raise RuntimeError("计费操作状态冲突")
            return {"menu": result}
        except asyncio.CancelledError:
            billing.fail_operation(op_key, "请求中断自动退回")
            raise
        except Exception:
            billing.fail_operation(op_key, "识图失败自动退回")
            return {"error": "文案没写成,这条腿的 1 点已退回;可换张更清晰的图重试"}

    shot_result, copy_result = await asyncio.gather(
        _shot_leg(shot_op), _copy_leg(copy_op))
    if shot_result.get("error") and copy_result.get("error"):
        raise HTTPException(500, "图和文案都没成功,3 点已全部退回;换张清晰的图再试")
    return {
        "file": shot_result.get("file") or "",
        "image_error": shot_result.get("error") or "",
        "menu": copy_result.get("menu"),
        "copy_error": copy_result.get("error") or "",
    }


@app.post("/api/tools/variants")
async def variants_api(body: dict):
    _need_module("content")
    script = (body.get("script") or "").strip()
    if len(script) < 30:
        raise HTTPException(400, "先贴一篇口播稿(至少30字)")
    op_key = _start_billed_operation("matrix_variants")
    try:
        result = await growth.script_variants(
            TEN(), script, body.get("n") or 3, body.get("styles") or ""
        )
        if not billing.complete_operation(op_key):
            raise RuntimeError("计费操作状态冲突")
        return result
    except asyncio.CancelledError:
        billing.fail_operation(op_key, "请求中断自动退回")
        raise
    except Exception:
        billing.fail_operation(op_key, "裂变失败自动退回")
        raise HTTPException(500, "裂变失败,点数已退回,请重试")


# ---------------- ⑥ 企微通知 ----------------
@app.get("/api/channels/webhook")
def webhook_get():
    _need_admin()
    url = notify.get_webhook(TEN())
    return {"set": bool(url), "masked": (url[:52] + "…") if url else ""}


@app.put("/api/channels/webhook")
def webhook_put(body: dict):
    _need_admin()
    try:
        notify.set_webhook(TEN(), body.get("url") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/channels/webhook/test")
async def webhook_test():
    _need_admin()
    try:
        return await notify.test_send(TEN())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/notify/daily-digest")
def daily_digest_get():
    """每日经营简报开关:默认开;任何登录角色可读."""
    return {"enabled": not db.get_setting(f"daily_digest_off:{TEN()}")}


@app.post("/api/notify/daily-digest")
def daily_digest_set(body: dict):
    """租户级简报开关;推送打扰与否只有企业主说了算."""
    _need_admin()
    enabled = bool(body.get("enabled"))
    db.set_setting(f"daily_digest_off:{TEN()}", None if enabled else "1")
    return {"ok": True, "enabled": enabled}


# ---------------- ⑧ 矩阵发布(beta) ----------------
@app.get("/api/matrix/accounts")
def matrix_accounts_list():
    _need_module("content")
    return {"platforms": [{"key": k, **{x: y for x, y in v.items() if x != "kind"}}
                          for k, v in matrixpub.PLATFORMS.items()],
            "accounts": matrixpub.pub_list(TEN())}


@app.post("/api/matrix/accounts")
def matrix_account_add(body: dict):
    _need_admin()
    try:
        return matrixpub.add_account(TEN(), body.get("platform"), body.get("name"),
                                     body.get("cookie"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/matrix/accounts/{acc_id}/check")
async def matrix_account_check(acc_id: str):
    _need_module("content")
    try:
        return await matrixpub.check_account(TEN(), acc_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/matrix/accounts/{acc_id}")
def matrix_account_del(acc_id: str):
    _need_admin()
    matrixpub.del_account(TEN(), acc_id)
    return {"ok": True}


@app.post("/api/matrix/publish")
async def matrix_publish(body: dict):
    _need_module("content")
    platform = body.get("platform")
    if platform not in matrixpub.PLATFORMS:
        raise HTTPException(400, "平台不支持")
    payload = {"title": (body.get("title") or "")[:60], "body": (body.get("body") or "")[:1000]}
    job_id = None
    if body.get("job_id"):
        try:
            job_id = int(body["job_id"])
        except (TypeError, ValueError):
            raise HTTPException(400, "工单参数无效")
        _job_or_404(job_id)
        payload["job_id"] = job_id
    imgs = []
    for f in (body.get("images") or [])[:9]:
        try:
            lp = assetfiles.resolve_tenant_asset(
                f, TEN(), expected_job_id=job_id,
                allowed_extensions=(".png", ".jpg", ".jpeg"),
            )
        except assetfiles.AssetAccessError as exc:
            raise HTTPException(400, str(exc)) from exc
        imgs.append(lp)
    payload["images"] = imgs
    if body.get("video"):
        try:
            payload["video"] = assetfiles.resolve_tenant_asset(
                body["video"], TEN(), expected_job_id=job_id,
                allowed_extensions=(".mp4",),
            )
        except assetfiles.AssetAccessError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        pid = matrixpub.enqueue(TEN(), platform, body.get("account"), payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    asyncio.create_task(matrixpub.run_task(pid, engine.broadcast))
    return {"task_id": pid, "note": "已进发布队列,进度看「发布渠道→矩阵发布记录」"}


@app.get("/api/matrix/tasks")
def matrix_tasks(
    limit: int = None,
    offset: int = 0,
    platform: str = "",
    status: str = "",
    q: str = "",
):
    _need_module("content")
    page_limit, page_offset, paged = _pagination(limit, offset, 20)
    where = ["tenant_id=?"]
    params = [TEN()]
    platform = (platform or "").strip()[:30]
    status = (status or "").strip()[:30]
    q = (q or "").strip()
    if platform:
        where.append("platform=?")
        params.append(platform)
    if status:
        where.append("status=?")
        params.append(status)
    if q:
        where.append(
            "json_valid(payload_json) AND "
            "json_extract(payload_json,'$.title') LIKE ? ESCAPE '\\'"
        )
        params.append(_like_value(q))
    where_sql = " AND ".join(where)
    rows = db.q(
        f"SELECT * FROM pub_task WHERE {where_sql} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_limit, page_offset),
    )
    for r in rows:
        r["payload"] = db.jloads(r.pop("payload_json"), {})
        r["payload"].pop("images", None)
        r["payload"].pop("video", None)
        r["fail"] = _public_publish_failure(
            r.get("status"),
            db.jloads(r.pop("fail_json", None), None),
        )
        r["log"] = _public_progress_for_view(
            r.get("status"), r.get("log"), _is_boss()
        )
        r.update(taskcenter.retry_meta("publish", r))
    if not paged and not any((platform, status, q)):
        return rows
    total = db.one(
        f"SELECT COUNT(*) AS n FROM pub_task WHERE {where_sql}", tuple(params)
    )["n"]
    return _page_result(rows, total, page_limit, page_offset)


@app.post("/api/matrix/tasks/{pid}/retry")
async def matrix_task_retry(pid: int):
    # 保留旧前端路径，但能力判断与 CAS 必须走任务中心统一契约。
    return await task_center_retry("publish", pid)


# ---------------- 静态 ----------------
app.mount("/files/avatar-public",
          StaticFiles(directory=avatar.PUBLIC_DIR, follow_symlink=False),
          name="avatar-public")
app.mount("/files", StaticFiles(directory=os.path.join(ROOT, "data", "assets"),
                                follow_symlink=False), name="files")
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")
# /pub 正常由 Caddy 直接伺服；应用侧只兜底到当前环境实际使用的公开素材目录。
# 不再在 import 阶段硬依赖生产机专属的 /srv 路径，保证测试与新环境可启动。
app.mount("/pub", StaticFiles(directory=avatar.PUBLIC_DIR, follow_symlink=False), name="pub")


@app.get("/login")
def login_page():
    with open(os.path.join(ROOT, "static", "login.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/promo")
def promo_page():
    with open(os.path.join(ROOT, "static", "promo.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/")
def index():
    with open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())
