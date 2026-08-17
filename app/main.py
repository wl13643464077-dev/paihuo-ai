"""ContentCrew Web 服务:API + SSE 推送 + 静态资源."""
import asyncio
import contextvars
import ipaddress
import json
import logging
import math
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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import (analyzer, assetfiles, auth, billing, bossdashboard, db, departments, employeeidentity, employeelearning, employees, expertmatch,
               export, feishu, funnel, llm, meeting, obs, providers, scheduler,
               inspection, inspectionimport, inspectionoverrides, inspectionstandards,
               learningevidence, purchases, secureconfig,
               taskcenter, taskrunner, taskthreads)
from .engine import engine
from .skills import registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("main")
app = FastAPI(title="派活 PaiHuo — 老板会派活，数字员工去干活")


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _sync_platform_industry_scope() -> int:
    """让平台总部拥有显式行业映射；普通租户仍必须逐项授权。"""
    now = time.time()
    rows = [
        str(item.get("key") or "").strip()
        for item in departments.list_depts()
        if str(item.get("key") or "").strip()
    ]
    with db.atomic() as connection:
        tenant = connection.execute(
            "SELECT id FROM tenants WHERE id=1 AND enabled=1"
        ).fetchone()
        if not tenant:
            return 0
        changed = 0
        for position, industry_key in enumerate(dict.fromkeys(rows)):
            cursor = connection.execute(
                "INSERT OR IGNORE INTO tenant_industry(tenant_id,industry_key,"
                "is_primary,created_at) VALUES(1,?,?,?)",
                (industry_key, 1 if position == 0 else 0, now),
            )
            changed += max(0, int(cursor.rowcount or 0))
        return changed

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
_INSPECTION_UPLOAD_MAX_BYTES = 38 * 1024 * 1024
_INSPECTION_ANALYSIS_MODEL_TIMEOUT_SECONDS = 300
_INSPECTION_CONTRACT_MARKER = "【最终权威JSON合同·运行时动态生成】"
# 前端复查上传等待 120s；后端必须更早收口并降级人工复核，
# 否则客户端先超时重试会在首个请求仍运行时重复落复查照片。
_INSPECTION_RECHECK_MODEL_TIMEOUT_SECONDS = 90
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
    ("POST", "/api/inspections"): (
        "inspection",
        "*work",
        _INSPECTION_UPLOAD_MAX_BYTES + _UPLOAD_MULTIPART_OVERHEAD_BYTES,
    ),
    ("POST", "/api/inspections/rechecks"): (
        "inspection-recheck",
        "*work",
        _INSPECTION_UPLOAD_MAX_BYTES + _UPLOAD_MULTIPART_OVERHEAD_BYTES,
    ),
}
_TRANSIENT_UPLOAD_RESERVED = contextvars.ContextVar(
    "transient_upload_reserved",
    default=False,
)
_TRANSIENT_UPLOAD_ROUTES = {
    ("POST", "/api/inspections/branches/imports"): (
        "inspection-branch-import",
        "*admin",
        inspectionimport.MAX_FILE_BYTES + _UPLOAD_MULTIPART_OVERHEAD_BYTES,
    ),
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
    if module == "*admin":
        return auth.is_admin()
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
        try:
            response = await call_next(request)
        except Exception as exc:
            # 必须在这里拦下:交给 Starlette 的 ServerErrorMiddleware 会在
            # 发送响应后无条件 re-raise,uvicorn 随即把完整堆栈打进 journal,
            # 违反「日志只记异常类型」的保密口径。中文文案与 _unhandled_exception 一致。
            logging.getLogger("main").error(
                "unhandled error path=%s error_type=%s",
                request.url.path, type(exc).__name__,
            )
            response = JSONResponse(
                {"detail": "系统开小差了,这一步没做成。请再试一次;"
                           "反复失败请点右下角 💬 反馈给我们,会有人跟进"},
                status_code=500,
            )
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
    app.state.learning_batch_shutting_down = False
    await asyncio.to_thread(db.conn)
    # Close the lifecycle gap for idle tenants before serving traffic.  The
    # sweep is deliberately bounded and cross-tenant; request paths retain a
    # tenant-local lazy fallback for quota recovery.
    try:
        retention = await db.arun(inspectionimport.cleanup_expired_previews)
        log.info(
            "inspection import retention startup sweep scanned=%d expired=%d "
            "compacted=%d wal_checkpointed=%d wal_busy=%d",
            int((retention or {}).get("scanned", 0)),
            int((retention or {}).get("expired", 0)),
            int((retention or {}).get("compacted", 0)),
            int((retention or {}).get("wal_checkpointed", 0)),
            int((retention or {}).get("wal_busy", -1)),
        )
    except Exception as exc:
        # A transient cleanup failure must not make the API unavailable; the
        # scheduler retries the same bounded sweep on its next periodic tick.
        log.error(
            "inspection import retention startup sweep failed error_type=%s",
            type(exc).__name__,
        )
    secret_migration = await db.arun(secureconfig.migrate_legacy_secrets)
    if isinstance(secret_migration, dict):
        # 只记录聚合计数，绝不把 key、租户、密文或原始凭据写进 journal。
        log.info(
            "secure configuration migration settings_encrypted=%d "
            "settings_verified=%d field_rows_scanned=%d field_rows_updated=%d "
            "fields_encrypted=%d fields_verified=%d field_rows_failed=%d "
            "field_migration_complete=%s field_migration_skipped=%s",
            int(secret_migration.get("encrypted") or 0),
            int(secret_migration.get("verified") or 0),
            int(secret_migration.get("field_rows_scanned") or 0),
            int(secret_migration.get("field_rows_updated") or 0),
            int(secret_migration.get("fields_encrypted") or 0),
            int(secret_migration.get("fields_verified") or 0),
            int(secret_migration.get("field_rows_failed") or 0),
            bool(secret_migration.get("field_migration_complete")),
            bool(secret_migration.get("field_migration_skipped")),
        )
    await db.arun(auth.bootstrap)
    synced_platform_industries = await db.arun(_sync_platform_industry_scope)
    if synced_platform_industries:
        log.info(
            "platform industry scopes synchronized count=%d",
            synced_platform_industries,
        )
    # 组件量规:读取时惰性求值,记录侧零开销。
    obs.register_gauge("engine_queue_depth", lambda: engine.queue.qsize())
    obs.register_gauge("engine_job_locks", lambda: len(engine.locks))
    obs.register_gauge("sse_subscribers", lambda: len(engine.subscribers))
    obs.register_gauge(
        "db_pool_backlog",
        lambda: db._pool()._work_queue.qsize(),
    )
    recovered_asset_transactions = await asyncio.to_thread(
        avatar.recover_asset_transactions,
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
        await db.aget_setting("wechat_delivery_migration_conflicts"), []
    ) or []
    if draft_conflicts:
        log.error(
            "wechat delivery migration found %d tenant/job conflicts; "
            "review /api/admin/wechat-delivery-alerts before clearing them",
            len(draft_conflicts),
        )
    recovered_interventions = await db.arun(meeting.recover_interventions)
    if recovered_interventions:
        log.warning(
            "recovered %d interrupted meeting interventions",
            recovered_interventions,
        )
    recovered_retros = await db.arun(pubtrack.recover_interrupted)
    if recovered_retros:
        log.warning("recovered %d interrupted publication retros", recovered_retros)
    recovered_drafts, protected_draft_ops = await db.arun(
        _recover_wechat_deliveries
    )
    if recovered_drafts:
        log.warning("recovered %d interrupted wechat deliveries", recovered_drafts)
    settled_subscriptions = await db.arun(
        billing.settle_legacy_subscriptions
    )
    if settled_subscriptions:
        log.info(
            "settled %d legacy successful subscription operations",
            settled_subscriptions,
        )
    recovered_learning = await db.arun(
        employeelearning.recover_interrupted_runs
    )
    if recovered_learning:
        log.warning(
            "failed %d interrupted employee learning runs before refund",
            recovered_learning,
        )
    settled_learning, protected_learning_ops = await db.arun(
        _recover_employee_learning_billing
    )
    if settled_learning:
        log.info(
            "settled %d delivered employee learning operations",
            settled_learning,
        )
    recovered_billing = await db.arun(
        billing.recover_interrupted_operations,
        exclude_op_keys=(protected_draft_ops | protected_learning_ops),
    )
    if recovered_billing:
        log.warning("recovered %d interrupted billed operations", recovered_billing)
    orphaned_learning_batches = await db.arun(
        _detect_orphaned_learning_batches_for_restart
    )
    if orphaned_learning_batches:
        log.warning(
            "detected %d employee learning batches awaiting an explicit, "
            "tenant-scoped resume after restart",
            orphaned_learning_batches,
        )
    await engine.start()
    asyncio.create_task(scheduler.loop(engine))
    asyncio.create_task(analyzer.loop())
    taskrunner.resume_pending(engine.broadcast)
    await _resume_inspection_tasks()
    avatar.resume_pending(engine.broadcast)      # 数字人:queued重开/running退点
    meeting.resume_pending(engine.broadcast)     # 圆桌会:queued重开/running标失败
    from . import textvideo as _tv
    _tv.resume_pending(engine.broadcast)         # 图文成片:queued重开/running退点
    matrixpub.resume_pending(engine.broadcast)   # 矩阵发布:中断标失败+给重试/半自动出路
    _recover_interrupted_tool_jobs()
    _ensure_tool_running_index()
    _start_tool_watchdog()


@app.on_event("shutdown")
async def _learning_batch_shutdown_marker():
    # Coordinator recovery retries remain persistent during normal service,
    # but a deliberate process shutdown must be allowed to stop.  Interrupted
    # researching rows are compensated by the existing startup recovery.
    app.state.learning_batch_shutting_down = True


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
    return int(_file_access_scope(path).get("tenant_id") or 0)


def _file_access_scope(path: str) -> dict:
    """解析文件的租户与最窄板块；巡店证据保留行业边界。"""
    m = _re_files.match(r"/files/avatar-public/([^/]+)$", path)
    if m:
        return {
            "tenant_id": int(avatar.asset_owner(m.group(1)) or 0),
            "industry_key": None,
            # /files/avatar-public 是登录态素材预览；供外部供应商
            # 拉取的现有 /pub 公开传输端点不经过这个分支。
            "required_module": "avatar",
        }
    return assetfiles.file_access_scope(path)


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
    uid = await db.arun(
        auth.parse_session, request.cookies.get("cc_sess") or ""
    )
    user = await db.arun(auth.get_user, uid) if uid else None
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
    public_purchase_catalog = (
        request.method.upper() == "GET"
        and path == "/api/purchases/catalog"
    )
    # 游客可点开员工公开介绍；员工配置写接口继续统一拦截。
    TOUR_BLOCK = ("/api/employees/",)
    if (path.startswith("/api/") and not path.startswith("/api/auth/login")
            and not path.startswith("/api/guest/")
            and path != "/api/funnel/events"
            and not public_purchase_catalog
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
        if not await db.arun(_upload_permission_allowed, module):
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
        file_scope = await db.arun(_file_access_scope, path)
        owner_tid = int(file_scope.get("tenant_id") or 0)
        industry_key = str(file_scope.get("industry_key") or "")
        required_module = str(file_scope.get("required_module") or "")
        cur = auth.current()
        cur_tid = cur["tenant_id"] if cur else None
        if (
            not cur
            or owner_tid < 1
            # 看板的跨租户结构化统计权不是原文附件读取权；
            # root/命名 boss 也必须与文件归属租户严格一致。
            or int(cur_tid or 0) != owner_tid
        ):
            return JSONResponse({"detail": "无权访问该文件"}, status_code=403)
        role = str(cur.get("role") or "")
        if role not in ("root", "owner", "member"):
            return JSONResponse({"detail": "无权访问该文件"}, status_code=403)
        # 所有受管文件族都必须声明最窄板块，任何遗漏一律
        # fail closed。owner/root 对 content/avatar 等基础板块仍由
        # auth.allowed 正常放行；但行业被租户撤权后，owner 不能继续
        # 借管理角色直读该行业的历史巡店证据。
        if (
            not required_module
            or not await db.arun(auth.allowed, required_module)
        ):
            detail = (
                "无权访问该行业文件"
                if industry_key and required_module == industry_key
                else "无权访问该文件"
            )
            return JSONResponse({"detail": detail}, status_code=403)
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


def _is_tour() -> bool:
    return (auth.current() or {}).get("role") == "tour"


def _need_boss():
    """Enforce the named super-account boundary, not merely a database role."""
    if not _is_boss():
        raise HTTPException(403, "仅超级管理账号 boss 可访问数字员工内部资料")


_PUBLIC_STATION_TASK_GUIDES = {
    "trend": {
        "task_placeholder": "例如：追踪[行业/品牌]最近[时间范围]的市场变化，筛出适合我们跟进的内容机会。",
        "material_placeholder": "可补充品牌定位、目标客群、近期活动和重点关注的平台；没有现成材料也可直接说明业务目标。",
        "input_tips": ["关注的行业、品牌或人群", "希望覆盖的平台和时间范围", "本次选题要服务的业务目标"],
        "output_hint": "得到经过筛选的趋势判断、选题优先级和建议跟进时点",
    },
    "research": {
        "task_placeholder": "例如：围绕[具体主题]核实关键事实、数据与案例，为后续内容准备可靠素材。",
        "material_placeholder": "可粘贴待核实的说法、已有链接、数据口径和优先来源；请标明哪些信息仍不确定。",
        "input_tips": ["要核实的主题和核心问题", "优先关注的地区、时间或来源", "已有链接、说法或待验证数据"],
        "output_hint": "得到带来源的事实摘要、证据强弱和仍待核验的问题",
    },
    "benchmark": {
        "task_placeholder": "例如：拆解[主题/账号/作品]为什么有效，并提炼适合我们借鉴的表达方式。",
        "material_placeholder": "可粘贴对标账号、帖子链接、截图文字和希望重点拆解的维度。",
        "input_tips": ["要研究的主题或对标对象", "目标平台与目标受众", "最关心的内容、结构或转化问题"],
        "output_hint": "得到对标差异、可借鉴做法、不可照搬风险和验证建议",
    },
    "draft": {
        "task_placeholder": "例如：为[目标人群]撰写一篇关于[主题]的[平台/文体]初稿，重点传达[核心观点]。",
        "material_placeholder": "可粘贴产品卖点、事实素材、活动规则、品牌口吻和参考文章。",
        "input_tips": ["主题、核心观点和目标读者", "发布平台与内容形式", "必须包含或不能出现的信息"],
        "output_hint": "得到结构完整、可继续修改或直接评审的内容初稿",
    },
    "style": {
        "task_placeholder": "例如：把这份内容调整为[品牌/个人]的表达风格，保持观点不变并提升辨识度。",
        "material_placeholder": "请粘贴待改原文，并补充品牌语气、常用表达、禁用词和必须保留的事实。",
        "input_tips": ["需要改写的原文", "希望接近的语气与风格", "品牌常用词、禁用词或参考作品"],
        "output_hint": "得到语气统一、自然且符合账号人设的定稿建议",
    },
    "media": {
        "task_placeholder": "例如：为[主题内容]规划适合[目标平台]的视觉素材和画面表达。",
        "material_placeholder": "可粘贴正文、视觉参考、品牌色、图片尺寸、已有素材和版权限制。",
        "input_tips": ["正文、主题或重点信息", "目标平台和画面尺寸", "品牌视觉、素材来源与版权限制"],
        "output_hint": "得到与正文对应的视觉方案、素材需求和使用位置",
    },
    "cover": {
        "task_placeholder": "例如：为[内容主题]设计适合[目标平台]的封面方向，突出[第一眼卖点]。",
        "material_placeholder": "可粘贴标题、品牌色、参考风格、封面尺寸，以及必须出现的文字或图片说明。",
        "input_tips": ["标题、主题和核心卖点", "目标平台与目标人群", "品牌视觉或必须保留的元素"],
        "output_hint": "得到可比较的封面方向、关键信息层级和视觉建议",
    },
    "deck": {
        "task_placeholder": "例如：把[现有内容]整理成面向[听众/场景]的演示结构，突出[核心结论]。",
        "material_placeholder": "可粘贴原始正文、关键数据、汇报对象、演示时长和已有页面结构。",
        "input_tips": ["现有正文、报告或要点", "听众、使用场景和演示时长", "必须讲清的结论与行动要求"],
        "output_hint": "得到清晰的演示结构、页面重点和讲解顺序",
    },
    "publish": {
        "task_placeholder": "例如：把这份成品适配到[目标平台]，整理发布文案、标签和发布节奏。",
        "material_placeholder": "可粘贴已确认的定稿、账号信息、发布时间限制和各平台审核注意事项。",
        "input_tips": ["已经确认的内容成品", "目标平台与发布时间要求", "账号限制、审核要求和运营节奏"],
        "output_hint": "得到各平台可直接审核的发布包和发布检查项",
    },
    "retro": {
        "task_placeholder": "例如：复盘[内容/活动]在[时间范围]的表现，找出有效做法和下一轮调整重点。",
        "material_placeholder": "可粘贴曝光、点击、互动、转化等汇总数据，以及评论摘要、发布时间和异常事件。",
        "input_tips": ["要复盘的内容与发布时间", "曝光、互动、转化等可用数据", "原定目标、异常事件与用户反馈"],
        "output_hint": "得到表现诊断、原因假设、复用项和下一轮改进动作",
    },
    "inspection": {
        "task_placeholder": "例如：检查[门店/区域]本次现场照片，找出可见问题并给出整改与复查计划。",
        "material_placeholder": "请优先从巡店工作台上传现场照片；可补充门店、区域、检查范围、责任人和整改期限要求。",
        "input_tips": ["门店、区域和巡检日期", "1～8张覆盖不同区域的现场照片", "本次重点、负责人和期限要求"],
        "output_hint": "得到带照片证据的问题分级、整改责任与期限、复查标准和门店记录",
    },
}


def _public_station_task_guide(s: dict) -> dict:
    """内容部的公开派活提示；与内部模板、能力和模型配置完全隔离。"""
    guide = _PUBLIC_STATION_TASK_GUIDES.get(s.get("key")) or {
        "task_placeholder": f"例如：请「{s.get('name') or '数字员工'}」围绕[具体目标]完成[具体任务]。",
        "material_placeholder": "可粘贴与当前任务直接相关的资料、数据和参考链接。",
        "input_tips": ["具体目标和使用场景", "已有材料与限制条件", "期望完成时间"],
        "output_hint": "得到一份围绕当前目标的可执行结果",
    }
    return {
        **guide,
        "industry_placeholder": "例如：所属行业、产品类别、目标人群或具体业务场景",
    }


_EMPLOYEE_IDENTITY_PUBLIC_FIELDS = (
    "person_status", "identity_status", "identity_ref", "config_revision",
    "config_sha256", "bundle_sha256", "can_assign_new", "can_continue", "can_learn",
    "slot_row_version", "role_profile_summary",
)
_ROLE_WRITE_BINDING_FIELDS = (
    "identity_ref", "config_revision", "config_sha256", "bundle_sha256",
)


def _employee_public_contract(
    employee: dict,
    *,
    config: dict | None = None,
    include_profile: bool = False,
) -> dict:
    """Expose the two independent schema-54 identity axes.

    A person slot may remain active while an old task or meeting keeps using a
    historical role identity.  Callers handling frozen work may pass its exact
    config revision; we never recover that revision from ``idx``.
    """
    # registry.STATIONS is intentionally a lightweight execution registry and
    # predates the frozen schema-54 identity fields.  Public API callers still
    # pass those core rows in several places, so normalize only an exact core
    # key/idx match to its canonical current identity before building the
    # public contract.  Industry/history rows must already be exact and are
    # never active-first substituted here.
    if not employee.get("dept_key"):
        try:
            active = employeeidentity.active_employee(int(employee.get("idx")))
        except (TypeError, ValueError):
            active = None
        if (
            active
            and active.get("dept_key") == "content"
            and str(active.get("key") or "") == str(employee.get("key") or "")
        ):
            employee = active
    view = employeeidentity.identity_view(
        employee, include_profile=include_profile,
    )
    if config is not None:
        if str(config.get("identity_ref") or "") != str(view["identity_ref"]):
            raise RuntimeError("员工岗位与配置身份不一致")
        view["config_revision"] = int(config.get("config_revision") or 0)
        view["config_sha256"] = str(config.get("config_sha256") or "")
        view["bundle_sha256"] = str(config.get("bundle_sha256") or "")
        if include_profile:
            view["professional_profile"] = (
                config.get("effective_profile")
                or config.get("professional_profile")
                or {}
            )
    result = {
        field: view.get(field) for field in _EMPLOYEE_IDENTITY_PUBLIC_FIELDS
    }
    if include_profile:
        result["professional_profile"] = view.get("professional_profile") or {}
        role_key = str(view.get("key") or employee.get("key") or "")
        cap_details = departments.capability_details_for(role_key)
        if not result["professional_profile"]:
            # 餐饮/内容部老岗位没有 V4 档案：附加发布内出厂能力档案，仅
            # 用于展示层；身份、配置包与任务提示词永远不读这份 sidecar。
            sidecar = departments.factory_profile_for(role_key)
            if sidecar:
                result["professional_profile"] = sidecar["professional_profile"]
                cap_details = sidecar.get("capability_details") or {}
        if cap_details:
            result["capability_details"] = cap_details
    # One-release aliases keep old clients readable. New UI decisions use only
    # person_status + identity_status and the explicit capability booleans.
    result["roster_status"] = (
        "active" if result["identity_status"] == "current" else "legacy"
    )
    result["can_assign"] = bool(result["can_assign_new"])
    return result


def _employee_current_write_binding(idx: int, body: dict | None = None) -> dict:
    """Bind a mutable request to the exact current role/config it rendered.

    ``idx`` is a person slot, never write authority.  Every mutable/new-work
    request must echo the full immutable role/config triple it rendered.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "员工岗位请求格式无效")
    if any(body.get(field) in (None, "") for field in _ROLE_WRITE_BINDING_FIELDS):
        raise HTTPException(400, "员工岗位身份、配置与能力包绑定必须完整提交")
    employee = employeeidentity.active_employee(idx)
    if not employee:
        raise HTTPException(404, "当前岗位不存在")
    config = employees.get_config(int(employee["idx"]))
    if not config:
        raise HTTPException(409, "员工岗位配置完整性校验失败")
    expected_identity = str(body.get("identity_ref") or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", expected_identity) is None:
        raise HTTPException(400, "岗位身份引用无效")
    if expected_identity != str(config.get("identity_ref") or ""):
        raise HTTPException(409, "员工岗位已更新，请刷新后重试")
    raw_revision = body.get("config_revision")
    if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
        raise HTTPException(400, "岗位配置版本无效")
    expected_revision = raw_revision
    if expected_revision < 1:
        raise HTTPException(400, "岗位配置版本无效")
    if expected_revision != int(config.get("config_revision") or 0):
        raise HTTPException(409, "员工岗位配置已更新，请刷新后重试")
    expected_hash = str(body.get("config_sha256") or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
        raise HTTPException(400, "岗位配置摘要无效")
    if expected_hash != str(config.get("config_sha256") or ""):
        raise HTTPException(409, "员工岗位配置已更新，请刷新后重试")
    expected_bundle = str(body.get("bundle_sha256") or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", expected_bundle) is None:
        raise HTTPException(400, "岗位能力包摘要无效")
    if expected_bundle != str(config.get("bundle_sha256") or ""):
        raise HTTPException(409, "员工岗位能力包已更新，请刷新后重试")
    return {
        "employee": employee,
        "config": config,
        "identity": _employee_public_contract(employee, config=config),
    }


def _employee_effective_view(employee: dict, config: dict) -> dict:
    """Overlay only an approved effective bundle onto an immutable identity."""
    return {
        **employee,
        "professional_profile": (
            config.get("effective_profile")
            or employee.get("professional_profile")
            or {}
        ),
        "workflow": (
            config.get("effective_workflow") or employee.get("workflow") or []
        ),
        "steps": (
            config.get("effective_workflow") or employee.get("steps") or []
        ),
    }


def _public_station(
    s: dict, *, include_task_guide: bool = False,
    config: dict | None = None,
) -> dict:
    """内容部员工的对外名片：只含展示信息，不含岗位实现与模型配置。"""
    public = {
        k: s[k]
        for k in ("idx", "key", "name", "dept", "emoji", "color", "intro")
    } | _employee_public_contract(s, config=config)
    if include_task_guide:
        public["task_guide"] = _public_station_task_guide(s)
    return public


def _public_expert(
    e: dict, *, include_task_guide: bool = False,
    config: dict | None = None,
    include_profile: bool = False,
) -> dict:
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
    public = {k: e.get(k) for k in ("idx", "name", "emoji", "color", "person", "dept_name")} | {
        "intro": intro,
        "catalog_version": e.get("catalog_version") or "v1",
    } | _employee_public_contract(
        e, config=config, include_profile=include_profile,
    )
    if include_task_guide:
        public["task_guide"] = departments.public_task_guide(e)
    return public


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


async def _drain_task_despite_cancellation(task: asyncio.Task):
    """Wait for an already-submitted task through any outer cancellations.

    Executors cannot revoke a SQLite write that has already started.  Repeated
    request cancellation therefore must not cancel the child task or let the
    caller guess whether it committed.  Child failures are deliberately read
    from ``task.result()`` and propagated unchanged.
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _run_db_safely(fn, *args, **kwargs):
    """Linearize an already-submitted DB mutation through request cancellation.

    Once a billing/status commit reaches the executor, its caller must observe
    the real result.  Otherwise the worker can commit after cancellation while
    the handler concurrently refunds or deletes the committed artifact.
    """
    operation = asyncio.create_task(db.arun(fn, *args, **kwargs))
    return await _drain_task_despite_cancellation(operation)


async def _run_db_then_start_worker_safely(
    fn,
    *args,
    start_worker,
    should_start=None,
    settle_unstarted=None,
    **kwargs,
):
    """Linearize a durable queue mutation with its in-process worker start.

    ``db.arun`` runs work in an executor, so cancelling the request cannot
    reliably cancel a SQLite transaction that has already begun.  The caller
    must therefore observe the final DB result and, when it committed a queued
    record, schedule its worker before propagating cancellation.  If scheduling
    itself fails, the optional settlement callback closes/refunds the durable
    record instead of leaving a charged orphan.
    """
    operation = asyncio.create_task(db.arun(fn, *args, **kwargs))
    cancellation = None
    try:
        result = await asyncio.shield(operation)
    except asyncio.CancelledError as exc:
        cancellation = exc
        result = await _drain_task_despite_cancellation(operation)

    must_start = should_start(result) if should_start else True
    if must_start:
        try:
            start_worker(result)
        except Exception:
            if settle_unstarted:
                await _run_db_safely(settle_unstarted, result)
            raise

    if cancellation is not None:
        raise cancellation
    return result


async def _start_billing_operation_safely(
    fn,
    *args,
    cancel_reason: str,
    **kwargs,
) -> str:
    """Start durable billing off-loop without leaving a cancelled charge."""
    start_task = asyncio.create_task(db.arun(fn, *args, **kwargs))
    try:
        return await asyncio.shield(start_task)
    except asyncio.CancelledError:
        op_key = await _drain_task_despite_cancellation(start_task)
        if op_key:
            try:
                await _run_db_safely(
                    billing.fail_operation,
                    op_key,
                    cancel_reason,
                )
            except Exception as exc:
                log.error(
                    "cancelled billing start refund failed op=%s error_type=%s",
                    op_key,
                    type(exc).__name__,
                )
                raise
        raise


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
    # 退点也是正向流水,但把它计成「充值」会让累计充值虚高、月度两列同抬:
    # 失败一单先计消耗再计充值。按 reason「退回」前缀单列。
    agg = db.one(
        "SELECT COALESCE(SUM(CASE WHEN delta>0 AND reason NOT LIKE '退回%' "
        "THEN delta END),0) recharged, "
        "COALESCE(SUM(CASE WHEN delta>0 AND reason LIKE '退回%' "
        "THEN delta END),0) refunded, "
        "COALESCE(-SUM(CASE WHEN delta<0 THEN delta END),0) spent, COUNT(*) n "
        "FROM billing_log WHERE tenant_id=?", (TEN(),)) or {}
    # 近30天按动作聚合消耗:必须从 billing_log 流水算——核心扣点路径
    # (内容工单/专家任务/会议/成片/工具/定时)走 charge_if_claimed,
    # 只写流水不写 billing_operation;此前从后者聚合会把大头全部漏掉。
    # 口径:扣款按 reason 的价目 label 归类,「退回:」流水按 label 冲抵。
    label_to_action = {
        (row.get("label") or act): act
        for act, row in billing.prices().items()
    }
    spend_map: dict = {}
    for flow in db.q(
            "SELECT delta, reason FROM billing_log "
            "WHERE tenant_id=? AND created_at>?",
            (TEN(), time.time() - 30 * 86400)):
        reason = flow.get("reason") or ""
        delta = float(flow.get("delta") or 0)
        is_refund = reason.startswith("退回:")
        core = reason[3:] if is_refund else reason
        action = label_to_action.get(core.split(" · ", 1)[0].strip())
        if not action:
            continue
        entry = spend_map.setdefault(
            action, {"action": action, "n": 0, "points": 0.0})
        if delta < 0 and not is_refund:
            entry["n"] += 1
            entry["points"] += -delta
        elif delta > 0 and is_refund:
            entry["n"] -= 1
            entry["points"] -= delta
    spend_by_action = sorted(
        ({**e, "n": max(1, e["n"]), "points": round(e["points"], 1)}
         for e in spend_map.values() if e["points"] > 0.01),
        key=lambda e: -e["points"])
    # 按月对账(北京时区自然月,近6个月):老板问"这个月花了多少"要有答案
    monthly = db.q(
        "SELECT strftime('%Y-%m', created_at, 'unixepoch', '+8 hours') AS ym, "
        "COALESCE(SUM(CASE WHEN delta>0 AND reason NOT LIKE '退回%' "
        "THEN delta END),0) AS recharged, "
        "COALESCE(SUM(CASE WHEN delta>0 AND reason LIKE '退回%' "
        "THEN delta END),0) AS refunded, "
        "COALESCE(-SUM(CASE WHEN delta<0 THEN delta END),0) AS spent "
        "FROM billing_log WHERE tenant_id=? AND created_at>? "
        "GROUP BY ym ORDER BY ym DESC LIMIT 6",
        (TEN(), time.time() - 200 * 86400))
    return {"balance": (t or {}).get("balance") or 0,
            "plan": (t or {}).get("plan") or "",
            "plan_expires": (t or {}).get("plan_expires"),
            "is_platform": TEN() == 1,
            "recharged": agg.get("recharged") or 0, "spent": agg.get("spent") or 0,
            "refunded_total": agg.get("refunded") or 0,
            "txn_n": agg.get("n") or 0,
            "prices": price_rows, "plans": billing.PLANS,
            "periods": billing.PERIODS, "log": log_rows,
            "log_limit": 300,
            "log_truncated": int(agg.get("n") or 0) > len(log_rows),
            "spend_by_action": spend_by_action,
            "monthly": monthly}


def _raise_purchase_error(exc: Exception):
    if isinstance(exc, purchases.PurchaseNotFound):
        status_code = 404
    elif isinstance(exc, purchases.PurchaseForbidden):
        status_code = 403
    elif isinstance(exc, purchases.PurchaseConflict):
        status_code = 409
    else:
        status_code = 400
    raise HTTPException(status_code, str(exc)) from None


@app.get("/api/purchases/catalog")
def purchase_catalog():
    """Authoritative catalogue; this is an offline application, not checkout."""
    return purchases.catalog()


@app.post("/api/purchases")
def purchase_create(body: dict):
    user = auth.current() or {}
    if user.get("role") not in {"root", "owner"}:
        raise HTTPException(403, "仅企业主账号可以提交购买申请")
    if any(
        key in body
        for key in ("price", "points", "amount", "quoted_price", "quoted_points")
    ):
        raise HTTPException(400, "价格和点数由服务器计算，请勿自行传入")
    try:
        return purchases.create_intent(
            int(user["tenant_id"]),
            int(user["id"]),
            request_key=body.get("request_id"),
            plan_key=body.get("plan"),
            period_key=body.get("period"),
            contact=body.get("contact"),
            note=body.get("note") or "",
            source=body.get("source") or "billing",
        )
    except (purchases.PurchaseError, ValueError) as exc:
        _raise_purchase_error(exc)


@app.get("/api/purchases")
def purchase_list(
        status: str | None = None,
        limit: int = 20,
        offset: int = 0):
    user = auth.current() or {}
    if user.get("role") not in {"root", "owner"}:
        raise HTTPException(403, "仅企业主账号可以查看购买申请")
    try:
        return purchases.list_own(
            int(user["tenant_id"]),
            int(user["id"]),
            status=status,
            limit=limit,
            offset=offset,
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)


def _purchase_admin_scope() -> int | None:
    _need_admin()
    return None if auth.is_root() else TEN()


@app.get("/api/admin/purchases")
def purchase_admin_list(
        tenant_id: int | None = None,
        status: str | None = None,
        plan: str | None = None,
        period: str | None = None,
        limit: int = 50,
        offset: int = 0):
    try:
        return purchases.list_admin(
            scope_tid=_purchase_admin_scope(),
            tenant_id=tenant_id,
            status=status,
            plan=plan,
            period=period,
            limit=limit,
            offset=offset,
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)


@app.get("/api/admin/purchases/stats")
def purchase_admin_stats(
        tenant_id: int | None = None,
        status: str | None = None,
        plan: str | None = None,
        period: str | None = None):
    try:
        return purchases.stats(
            scope_tid=_purchase_admin_scope(),
            tenant_id=tenant_id,
            status=status,
            plan=plan,
            period=period,
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)


@app.patch("/api/admin/purchases/{intent_id}")
def purchase_admin_transition(intent_id: int, body: dict):
    _need_root()
    user = auth.current() or {}
    try:
        return purchases.transition(
            intent_id,
            expected_status=body.get("expected_status"),
            target_status=body.get("status"),
            actor_id=int(user["id"]),
            note=body.get("note") or "",
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)


@app.get("/api/records/export.xlsx")
def records_export(kind: str = "billing"):
    """租户级数据带走:积分流水/发布台账/审查记录/人设档案。

    此前只有沉淀/资产两类能批量导出,"我的数据我能带走"缺了一大半。
    财务与人设(含语料心血)归主账号,台账与审查记录随 content 板块。
    """
    def ts(v):
        return (time.strftime("%Y-%m-%d %H:%M", time.localtime(v))
                if v else "")

    if kind == "billing":
        _need_admin()
        rows = db.q("SELECT delta,balance,reason,created_at FROM billing_log "
                    "WHERE tenant_id=? ORDER BY id DESC", (TEN(),))
        out = [{"时间": ts(r["created_at"]),
                "类型": ("退回" if (r["delta"] > 0
                                    and str(r.get("reason") or "")
                                    .startswith("退回"))
                         else "充值" if r["delta"] > 0 else "消耗"),
                "项目": r.get("reason") or "",
                "积分变动": r["delta"],
                "余额": r["balance"]} for r in rows]
        headers, name = ["时间", "类型", "项目", "积分变动", "余额"], "积分流水"
    elif kind == "publog":
        _need_module("content")
        state_cn = {"pending": "待复盘", "notified": "已提醒",
                    "done": "已复盘", "processing": "复盘中"}
        rows = db.q("SELECT * FROM publish_log WHERE tenant_id=? "
                    "ORDER BY id DESC", (TEN(),))
        out = []
        for r in rows:
            retro = db.jloads(r.get("retro_json"), {}) or {}
            day = lambda d: state_cn.get(
                (retro.get(d) or {}).get("state") or "", "—")
            out.append({"平台": r.get("platform") or "",
                        "标题": r.get("title") or "",
                        "链接": r.get("url") or "",
                        "发布时间": ts(r.get("published_at")),
                        "登记来源": "草稿箱自动" if r.get("source") == "draft"
                                    else "手动登记",
                        "T+1": day("1"), "T+3": day("3"), "T+7": day("7"),
                        "登记时间": ts(r.get("created_at"))})
        headers = ["平台", "标题", "链接", "发布时间", "登记来源",
                   "T+1", "T+3", "T+7", "登记时间"]
        name = "发布台账"
    elif kind == "censor":
        _need_module("content")
        rows = db.q("SELECT * FROM censor_log WHERE tenant_id=? "
                    "ORDER BY id DESC", (TEN(),))
        out = [{"类型": "发前审查" if r.get("kind") == "pre" else "发后复盘",
                "平台": r.get("platform") or "",
                "标题": r.get("title") or "",
                "结论": r.get("verdict") or "",
                "合规分": r.get("score"),
                "问题数": len(db.jloads(r.get("issues_json"), []) or []),
                # Excel 单元格上限 32767 字,报告留足余量
                "完整报告": (r.get("report") or "")[:30000],
                "时间": ts(r.get("created_at"))} for r in rows]
        headers = ["类型", "平台", "标题", "结论", "合规分", "问题数",
                   "完整报告", "时间"]
        name = "审查记录"
    elif kind == "profiles":
        _need_admin()
        rows = db.q("SELECT * FROM account_profile WHERE tenant_id=? "
                    "AND deleted_at IS NULL ORDER BY id", (TEN(),))
        out = []
        for r in rows:
            p = db.jloads(r.get("persona_json"), {}) or {}
            out.append({"名称": r.get("name") or "",
                        "定位": p.get("positioning") or "",
                        "受众": p.get("audience") or "",
                        "语气": p.get("tone") or "",
                        "禁忌": p.get("taboo") or "",
                        "视觉规范": p.get("visual") or "",
                        "口头禅": p.get("catchphrases") or "",
                        "文风特征": p.get("style_notes") or "",
                        "历史语料": (p.get("corpus") or "")[:30000],
                        "创建时间": ts(r.get("created_at"))})
        headers = ["名称", "定位", "受众", "语气", "禁忌", "视觉规范",
                   "口头禅", "文风特征", "历史语料", "创建时间"]
        name = "人设档案"
    else:
        raise HTTPException(400, "kind 支持 billing/publog/censor/profiles")
    return Response(
        export.rows_to_xlsx(out, headers, name),
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{kind}.xlsx"'})


_feedback_ips: dict = {}   # ip -> (当天已提交条数, 日序号);重启即清,轻量防灌


@app.post("/api/feedback")
async def feedback_submit(request: Request, body: dict):
    txt = (body.get("text") or "").strip()[:2000]
    if not txt:
        raise HTTPException(400, "反馈内容不能为空")
    # 游客也能留资(转化出口),但要挡住无限灌邮箱:每 IP 每天限 10 条
    client_ip = (request.client.host if request.client else "") or "?"
    day_no = int(time.time() // 86400)
    used, marker = _feedback_ips.get(client_ip, (0, day_no))
    if marker != day_no:
        used = 0
    if used >= 10:
        raise HTTPException(429, "今天反馈次数已达上限,明天再来或直接联系顾问")
    _feedback_ips[client_ip] = (used + 1, day_no)
    u = auth.current() or {}
    t = await db.aone(
        "SELECT name FROM tenants WHERE id=?", (u.get("tenant_id", 1),)
    ) or {}
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
        # order_id 是当前协议；op_key 仅为已接入早期预览版的客户端保留。
        # 两者都缺失时 billing.subscribe 会明确拒绝，绝不临时造随机键伪装幂等。
        order_id = body.get("order_id") or body.get("op_key")
        return billing.subscribe(
            tid,
            body.get("plan"),
            body.get("period"),
            op_key=str(order_id or "").strip(),
        )
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
    actor_is_root = auth.is_root()
    actor_tenant_id = TEN()
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
    with db.atomic() as connection:
        current_row = connection.execute(
            "SELECT * FROM users WHERE id=?", (uid,)
        ).fetchone()
        if not current_row:
            raise HTTPException(404)
        u = dict(current_row)
        if not actor_is_root and int(u["tenant_id"]) != int(actor_tenant_id):
            raise HTTPException(404)
        if u["role"] == "root" and not actor_is_root:
            raise HTTPException(403)
        if data:
            connection.execute(
                "UPDATE users SET "
                + ",".join(f"{key}=?" for key in data)
                + ",updated_at=? WHERE id=?",
                (*data.values(), time.time(), uid),
            )
            # 停用成员等同强制下线；会话撤销与 enabled 更新必须同事务。
            # 否则账号重新启用时，停用前 Cookie 会重新变成有效。
            if (
                int(u.get("enabled") or 0) == 1
                and data.get("enabled") == 0
            ):
                auth.revoke_sessions(uid)
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
    with db.atomic() as connection:
        tid = db.insert("tenants", {
            "name": name,
            "industries_json": json.dumps(inds, ensure_ascii=False),
        })
        for position, industry_key in enumerate(dict.fromkeys(inds)):
            connection.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,"
                "is_primary,created_at) VALUES(?,?,?,?)",
                (
                    tid,
                    industry_key,
                    1 if position == 0 else 0,
                    time.time(),
                ),
            )
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
    if not db.one("SELECT id FROM tenants WHERE id=?", (tid,)):
        raise HTTPException(404)
    with db.atomic() as connection:
        connection.execute(
            "UPDATE tenants SET industries_json=?,updated_at=? WHERE id=?",
            (json.dumps(inds, ensure_ascii=False), time.time(), tid),
        )
        connection.execute("DELETE FROM tenant_industry WHERE tenant_id=?", (tid,))
        for position, industry_key in enumerate(dict.fromkeys(inds)):
            connection.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                "VALUES(?,?,?,?)",
                (tid, industry_key, 1 if position == 0 else 0, time.time()),
            )
    return {"ok": True}


@app.put("/api/team/tenants/{tid}")
def team_tenant_toggle(tid: int, body: dict):
    _need_root()
    with db.atomic() as connection:
        tenant_row = connection.execute(
            "SELECT id,enabled FROM tenants WHERE id=?", (tid,)
        ).fetchone()
        if not tenant_row:
            raise HTTPException(404)
        tenant = dict(tenant_row)
        if "enabled" in body:
            target_enabled = 1 if body["enabled"] else 0
            connection.execute(
                "UPDATE tenants SET enabled=?,updated_at=? WHERE id=?",
                (target_enabled, time.time(), tid),
            )
            # 停用企业必须同时永久撤销全部已签发会话。否则企业重新启用后，
            # 停用前的旧 Cookie 会复活，绕过管理员的下线意图。
            if int(tenant.get("enabled") or 0) == 1 and target_enabled == 0:
                for row in connection.execute(
                    "SELECT id FROM users WHERE tenant_id=?", (tid,)
                ):
                    auth.revoke_sessions(int(row["id"]))
        if body.get("name"):
            connection.execute(
                "UPDATE tenants SET name=?,updated_at=? WHERE id=?",
                (body["name"], time.time(), tid),
            )
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
    skills_n = sum(
        len(config.get("skills") or [])
        for config in employees.get_configs(
            [*registry.BY_IDX, *departments.specialists()]
        ).values()
    )
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
        employee = employeeidentity.active_employee(idx)
        if not employee:
            return None
        cfg = employees.get_config(idx)
        identity = _employee_public_contract(employee, config=cfg)
        saved_text_model = cfg.get("model_text")
        saved_image_model = cfg.get("model_image")
        return {"idx": idx,
                "name": str(employee.get("name") or name),
                "person": str(employee.get("person") or name),
                "catalog_version": str(employee.get("catalog_version") or "v1"),
                "dept": dept, "web_required": web_required,
                "is_custom": bool(cfg["prompt_template"]),
                "enabled": identity["person_status"] == "active",
                "core": idx < 100,
                "skills_n": len(cfg["skills"]),
                "model_text": (saved_text_model
                               if providers.text_model_available(saved_text_model)
                               else ""),
                "model_image": (saved_image_model
                                if providers.image_model_available(saved_image_model)
                                else ""),
                **identity}

    rows = []
    for s in registry.STATIONS:
        rows.append(emp_row(s["idx"], s["name"], "内容生产部",
                            web_required=s["key"] in ("trend", "research", "benchmark")))
    for d in departments.list_depts():
        for e in d["employees"]:
            rows.append(emp_row(e["idx"], e["name"], d["name"]))
    rows = [row for row in rows if row]
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
    binding = _employee_current_write_binding(idx, body)
    mt, mi = body.get("model_text"), body.get("model_image")
    if ("model_text" in body and mt not in (None, "")
            and not providers.text_model_available(mt)):
        raise HTTPException(400, "未知或不可用的文本模型")
    if ("model_image" in body and mi not in (None, "")
            and not providers.image_model_available(mi)):
        raise HTTPException(400, "未知或不可用的生图模型")
    try:
        employees.set_models_for_identity(
            binding["identity"]["identity_ref"],
            mt if "model_text" in body else None,
            mi if "model_image" in body else None,
            expected_revision=binding["config"]["config_revision"],
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    fresh = employees.get_config(idx)
    return {
        "ok": True,
        **_employee_public_contract(binding["employee"], config=fresh),
    }


@app.put("/api/admin/employees/{idx}/enabled")
def admin_emp_enabled(idx: int, body: dict):
    _need_boss()
    if not _is_emp(idx):
        raise HTTPException(404)
    if idx < 100:
        raise HTTPException(400, "内容部工位是流水线必备,不能停用")
    binding = _employee_current_write_binding(idx, body)
    expected = body.get("slot_row_version")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise HTTPException(400, "员工在岗状态版本必填且必须有效")
    try:
        slot = employees.set_enabled(
            idx, bool(body.get("enabled")),
            expected_row_version=expected,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, **_employee_public_contract(
        binding["employee"], config=employees.get_config(idx)
    ), "slot_row_version": slot["row_version"]}


@app.get("/api/admin/employees/{idx}/detail")
def admin_emp_detail(idx: int):
    """后台员工全档案:技能/能力/统计/开关."""
    _need_boss()
    if not _is_emp(idx):
        raise HTTPException(404)
    employee = employeeidentity.active_employee(idx)
    if not employee:
        raise HTTPException(404)
    cfg = employees.get_config(idx)
    identity = _employee_public_contract(
        employee, config=cfg, include_profile=True,
    )
    learning_history = (
        _employee_learning_history(cfg.get("identity_ref"), limit=5)
        if str(employee.get("catalog_version") or "")
        == departments.DECISION_V4_CATALOG_VERSION
        else {"runs": [], "researching": False, "activated": 0}
    )
    if idx in registry.BY_IDX:
        s = registry.BY_IDX[idx]
        info = {
            "name": s["name"], "person": str(employee.get("person") or s["name"]),
            "catalog_version": str(employee.get("catalog_version") or "v1"),
            "dept": s["dept"], "duty": s["duty"], "core": True,
        }
        caps = registry.capabilities_for(idx)
        stats = db.one("SELECT COUNT(*) n, SUM(cost_usd) cost FROM station_run "
                       "WHERE station_idx=? AND status IN ('done','awaiting_review')", (idx,)) or {}
    else:
        e = employee
        info = {
            "name": e["name"], "person": str(e.get("person") or ""),
            "catalog_version": str(e.get("catalog_version") or "v1"),
            "dept": e["dept_name"], "duty": e["duty"], "core": False,
        }
        caps = departments.capabilities_for(
            idx, cfg.get("caps_off"),
            employee=_employee_effective_view(e, cfg),
        )
        identity_where, identity_args = _employee_task_where(employee)
        stats = db.one(
            "SELECT COUNT(*) n, SUM(cost_usd) cost FROM task "
            "WHERE status='done' AND " + identity_where,
            identity_args,
        ) or {}
    return {**info, "idx": idx,
            "enabled": identity["person_status"] == "active",
            "skills": cfg["skills"], "learned_at": cfg["learned_at"],
            "effective_workflow": cfg.get("effective_workflow") or [],
            "learning_evidence": cfg.get("learning_evidence") or [],
            "learning": idx in employees.LEARNING or learning_history["researching"],
            "learning_run": (
                learning_history["runs"][0] if learning_history["runs"] else None
            ),
            "learning_runs": learning_history["runs"],
            "activated_learning_runs": learning_history["activated"],
            "capabilities": caps, **identity,
            "stats": {"runs": stats.get("n", 0), "cost_usd": stats.get("cost") or 0}}


@app.get("/api/admin/employees/{idx}/prompt")
def admin_emp_prompt(idx: int):
    _need_boss()
    if not _is_emp(idx):
        raise HTTPException(404)
    employee = employeeidentity.active_employee(idx)
    if not employee:
        raise HTTPException(404)
    cfg = employees.get_config(idx)
    identity = _employee_public_contract(
        employee, config=cfg, include_profile=True,
    )
    if idx in registry.BY_IDX:
        s = registry.BY_IDX[idx]
        return {"idx": idx, "name": s["name"],
                "default_template": registry.DEFAULT_PROMPTS[s["key"]],
                "placeholders": registry.PLACEHOLDERS[s["key"]],
                "prompt_template": cfg["prompt_template"], **identity}
    return {"idx": idx, "name": employee["name"],
            "default_template": "(专家默认提示词由岗位手册+任务书自动拼装;在此填写即完全接管,"
                                "支持 {direction} {industry} {material} 占位符)",
            "placeholders": {"direction": "任务内容", "industry": "行业/业态",
                             "material": "补充材料"},
            "prompt_template": cfg["prompt_template"], **identity}


# ---------------- 元数据 ----------------
@app.get("/api/meta")
def meta():
    loaded_departments = departments.list_depts()
    if _is_boss():
        stations = []
        for station in registry.STATIONS:
            item = {k: v for k, v in station.items() if k != "run"}
            item["model"] = providers.text_model_for(station["idx"])
            config = employees.get_config(station["idx"])
            item.update(_employee_public_contract(station, config=config))
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
              "voice_clone_points": (billing.prices().get("voice_clone") or {}).get("points", 9),
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
    # 通知标题/摘要/链接本身也是业务数据。查询与 mark-read 统一经过
    # notify 的角色 + 板块白名单，不能先把跨模块广播取出来再交给前端隐藏。
    from . import notify as _nt
    notifications = _nt.unread_for_user(TEN(), auth.current(), limit=30)
    # V26:余额+新手引导进度(首页头卡与开工清单用)
    ten_row = db.one("SELECT balance, plan FROM tenants WHERE id=?", (TEN(),)) or {}
    from . import wechat as _wc
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
        # 通知是租户级共享的:一键全读会替企业主清掉「等拍板/发布失败/该复盘」等待办,
        # 所以只允许主账号操作;成员对自己看过的单条点已读不受限。
        if not auth.is_admin():
            raise HTTPException(403, "一键全读会替企业主清掉待办,请单条已读或让主账号操作")
        changed = notify.mark_all_read(TEN(), auth.current())
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
    changed = notify.mark_read(TEN(), auth.current(), ids)
    return {"ok": True, "updated": changed}


@app.get("/api/notifications")
def notifications_list(
        limit: int = 40,
        offset: int = 0,
        unread: bool = False):
    page_limit, page_offset, _ = _pagination(limit, offset, 40)
    result = notify.history_for_user(
        TEN(),
        auth.current(),
        limit=page_limit,
        offset=page_offset,
        unread_only=bool(unread),
    )
    return _page_result(
        result["items"],
        result["total"],
        page_limit,
        page_offset,
        unread=bool(unread),
    )


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
        # 发起人:副账号开的单老板事后能查到;系统内部路径(如定时任务)无会话则留空
        "created_by": (auth.current() or {}).get("id"),
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
            "content_job", tid, claim,
            note=f"工单#{job_id}·{note}"[:160], points=points,
            job_id=job_id)
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


def _start_content_job_worker(job_id: int) -> None:
    engine.notify(job_id)
    engine.touch(job_id)


def _settle_unstarted_content_job(job_id: int) -> bool:
    return engine.settle_failure(
        job_id,
        "内容工单启动失败，系统已安全终止并退回本次点数",
    )


@app.post("/api/jobs")
async def create_job(body: dict):
    _need_module("content")
    brief = _validated_brief(body.get("brief"))
    running = await db.aq(
        "SELECT id FROM job WHERE tenant_id=? AND deleted_at IS NULL AND "
        "status NOT IN ('done','cancelled','failed')",
        (TEN(),),
    )
    if len(running) >= 3:
        raise HTTPException(429, "并行工单已达上限(3),请先处理进行中的工单")
    profile_id = await db.arun(
        _profile_id_for_tenant, body.get("profile_id")
    )
    job_id = await _run_db_then_start_worker_safely(
        _create_charged_content_job,
        {
            "brief_json": json.dumps(brief, ensure_ascii=False),
            "profile_id": profile_id,
            "tenant_id": TEN(),
            "mode": _validated_mode(body.get("mode")),
        },
        note=brief["direction"][:20],
        start_worker=_start_content_job_worker,
        settle_unstarted=_settle_unstarted_content_job,
    )
    asyncio.create_task(
        _record_first_work_best_effort(TEN(), "content")
    )
    return {"job_id": job_id}


def _job_or_404(job_id: int) -> dict:
    _need_module("content")
    j = db.one("SELECT * FROM job WHERE id=? AND deleted_at IS NULL", (job_id,))
    if not j or j.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    return j


def _tenant_username(uid) -> str | None:
    """按当前租户查用户名做展示;跨租户/已删除账号一律返回 None,不泄露别家用户。"""
    try:
        uid = int(uid or 0)
    except (TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    row = db.one(
        "SELECT username FROM users WHERE id=? AND tenant_id=?",
        (uid, TEN()),
    )
    return row["username"] if row else None


def _notify_member_review(job: dict, idx: int, action) -> None:
    """member 代老板拍板(通过/打回)后推送 member_reviewed,老板不再毫不知情。

    放在 HTTP 层而非 engine:审批事务已提交、操作者会话上下文确定,
    且"按角色决定要不要惊动老板"属于账号策略,不属于流水线状态机。
    """
    u = auth.current() or {}
    if u.get("role") != "member" or action not in ("approve", "edit", "reject"):
        return
    station = registry.BY_IDX.get(idx) or {}
    from . import notify
    notify.push(int(job.get("tenant_id") or TEN()), "member_reviewed", {
        "job_id": int(job["id"]),
        "user": u.get("username") or "",
        "station": station.get("name") or f"{idx + 1}",
        "approved": action != "reject",
        "title": engine._job_title(job["id"]),
    })


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
    # 拍板人用户名不是内部资料:member 也应看到某工位是谁批的(限同租户)。
    item["reviewed_by_name"] = _tenant_username(item.get("reviewed_by"))
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
            "reviewed_by_name",
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
    # 发起人:副账号开的单,老板在详情页一眼可见;查不到(历史单/跨租户)为 None
    j["created_by_name"] = _tenant_username(j.get("created_by"))
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
    # 审批已成功提交;若是副账号代拍板,同步告知老板(站内必达,配企微再外推)
    _notify_member_review(job, idx, body.get("action"))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/gate")
def gate_action(job_id: int, body: dict):
    job = _job_or_404(job_id)
    action = body.get("action")
    try:
        engine.gate_action(job_id, action)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 审查拦截被 member 放行/重检是合规场景,比普通拍板更需要老板知情
    u = auth.current() or {}
    if u.get("role") == "member":
        from . import notify
        notify.push(int(job.get("tenant_id") or TEN()), "member_reviewed", {
            "job_id": int(job_id),
            "user": u.get("username") or "",
            "station": "审查关卡",
            "approved": action == "override",
            "title": engine._job_title(job_id),
        })
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
_TASK_IDENTITY_COLUMNS = (
    "employee_key", "employee_catalog_version", "employee_name_snapshot",
    "employee_dept_key", "employee_spec_sha256", "employee_identity_ref",
    "employee_config_revision", "employee_config_sha256", "person_snapshot",
    "identity_scheme", "bundle_sha256",
)


def _employee_task_signature(employee: dict) -> tuple:
    return (int(employee["idx"]), employeeidentity.identity_ref(employee))


def _employee_task_where(employee: dict, alias: str = "task") -> tuple[str, tuple]:
    signature = _employee_task_signature(employee)
    # Config revisions are mutable generations within one immutable role
    # identity.  Card statistics include every revision of that role, while an
    # older V1/V2 role sharing the numeric person slot remains excluded by ref.
    columns = ("emp_idx", "employee_identity_ref")
    return (
        " AND ".join(f"{alias}.{column}=?" for column in columns),
        signature,
    )


@app.get("/api/depts")
def depts_list():
    stats = {}
    for r in db.q(
        "SELECT emp_idx,employee_identity_ref,"
        "COUNT(*) n,SUM(status IN ('running','queued')) run "
        "FROM task WHERE tenant_id=? AND deleted_at IS NULL "
        "GROUP BY emp_idx,employee_identity_ref",
        (TEN(),),
    ):
        stats[(r["emp_idx"], r["employee_identity_ref"])] = r
    visible = [d for d in departments.list_depts() if auth.dept_visible(d["key"])]
    # 一次性批量取所有可见员工的配置,避免逐人 get_config+is_enabled 的 N+1(几百员工×2查库)
    cfgs = employees.get_configs([e["idx"] for d in visible for e in d["employees"]])
    out = []
    internal = _is_boss()
    for d in visible:
        emps = []
        for e in d["employees"]:
            cfg = cfgs.get(e["idx"]) or employees.get_config(e["idx"])
            scoped_employee = {
                **e, "dept_key": d["key"], "dept_name": d["name"],
            }
            st = stats.get(_employee_task_signature(scoped_employee)) or {}
            enabled = bool(cfg.get("enabled", True))
            public = _public_expert(
                scoped_employee, config=cfg,
            ) | {
                "group": e["group"], "enabled": enabled,
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
    e = employeeidentity.active_employee(idx)
    if not e:
        raise HTTPException(404)
    show_profile = (auth.current() or {}).get("role") != "tour"
    if show_profile:
        _need_module(e["dept_key"])
    cfg = employees.get_config(idx)
    identity_where, identity_args = _employee_task_where(e)
    tasks = db.q(
        "SELECT id,status,brief_json,cost_usd,created_at FROM task "
        "WHERE tenant_id=? AND deleted_at IS NULL AND " + identity_where + " "
        "ORDER BY id DESC LIMIT 20",
        (TEN(), *identity_args),
    )
    for t in tasks:
        t["brief"] = db.jloads(t.pop("brief_json"))
    stats = db.one(
        "SELECT COUNT(*) n,SUM(cost_usd) cost FROM task WHERE tenant_id=? "
        "AND status='done' AND deleted_at IS NULL AND " + identity_where,
        (TEN(), *identity_args),
    ) or {}
    identity = _employee_public_contract(
        e, config=cfg, include_profile=show_profile,
    )
    enabled = bool(cfg.get("enabled", True))
    public = _public_expert(
        e,
        include_task_guide=not _is_tour(),
        config=cfg,
        include_profile=show_profile,
    ) | {
        "tasks": tasks,
        "enabled": enabled,
    }
    if not _is_boss():
        return public
    effective_employee = _employee_effective_view(e, cfg)
    learning_history = _employee_learning_history(cfg.get("identity_ref"), limit=5)
    return {**public,
            **{k: e[k] for k in ("duty", "desc", "group", "inputs", "steps", "deliverables")},
            "capabilities": (
                departments.capabilities_for(
                    idx, cfg.get("caps_off"), employee=effective_employee,
                )
            ),
            "skills": cfg["skills"] if identity["can_learn"] else [],
            "effective_workflow": cfg.get("effective_workflow") or [],
            "learning_evidence": cfg.get("learning_evidence") or [],
            "learned_at": cfg["learned_at"] if identity["can_learn"] else None,
            "learning": identity["can_learn"] and (
                idx in employees.LEARNING or learning_history["researching"]
            ),
            "learning_run": (
                learning_history["runs"][0] if learning_history["runs"] else None
            ),
            "learning_runs": learning_history["runs"],
            "activated_learning_runs": learning_history["activated"],
            "prompt_template": cfg["prompt_template"],
            "is_custom": bool(cfg["prompt_template"]),
            "stats": {"runs": stats.get("n", 0), "cost_usd": stats.get("cost") or 0}}


def _create_charged_expert_task(task_data: dict, note: str = "") -> int:
    """先落 pending 任务，再用同一事务抢占并扣点，避免扣费后没有任务记录。"""
    snapshot_names = {
        "employee_key", "employee_catalog_version", "employee_name_snapshot",
        "employee_dept_key", "employee_spec_sha256", "person_snapshot",
        "identity_scheme",
    }
    required_snapshot_names = snapshot_names - {"person_snapshot"}
    config_names = {
        "employee_identity_ref", "employee_config_revision",
        "employee_config_sha256", "bundle_sha256",
    }
    identity_scheme = str(task_data.get("identity_scheme") or "").strip()
    has_frozen_snapshot = bool(
        all(
            str(task_data.get(field) or "").strip()
            for field in required_snapshot_names
        )
        and (
            identity_scheme != "v2-person"
            or bool(str(task_data.get("person_snapshot") or "").strip())
        )
    )
    supplied_config = {
        field for field in config_names
        if task_data.get(field) not in (None, "")
    }
    if supplied_config and (
        not has_frozen_snapshot or supplied_config != config_names
    ):
        raise RuntimeError("任务员工配置身份字段不完整")
    binding = (
        employeeidentity.resolve_task_binding(task_data)
        if has_frozen_snapshot and supplied_config == config_names else None
    )
    if has_frozen_snapshot and supplied_config == config_names and not binding:
        raise RuntimeError("任务员工配置版本无法验证")
    employee = (
        binding["employee"] if binding
        else employeeidentity.resolve_task(task_data) if has_frozen_snapshot
        else employeeidentity.active_employee(task_data.get("emp_idx"))
    )
    if not employee:
        raise RuntimeError("不允许向未知员工创建任务")
    config = binding["config"] if binding else employees.ensure_role_config(employee)
    identity_fields = employeeidentity.task_fields(employee, config=config)
    compared_fields = snapshot_names | supplied_config
    if has_frozen_snapshot and any(
        str(task_data.get(field) or "") != str(value)
        for field, value in identity_fields.items() if field in compared_fields
    ):
        raise RuntimeError("任务员工身份与冻结目录不一致")
    task_data = {**task_data, **identity_fields, "emp_idx": int(employee["idx"])}
    tid = int(task_data.get("tenant_id") or TEN())
    points = 0.0 if tid == 1 else float(
        (billing.prices().get("expert_task") or {"points": 1})["points"])
    task_id = db.insert("task", {
        **task_data,
        "status": "pending_charge",
        "billing_status": "pending",
        "billing_points": points,
        # 发起人:记录是哪个账号派的活;无会话的内部路径留空
        "created_by": task_data.get("created_by", (auth.current() or {}).get("id")),
    })

    def claim(connection):
        derived_frozen_work = bool(
            task_data.get("source_task_id") or task_data.get("source_meeting_id")
        )
        if not _role_binding_matches(
            connection, task_data, require_current=not derived_frozen_work,
        ):
            raise RuntimeError("员工岗位配置已更新，请刷新后重试")
        changed = connection.execute(
            "UPDATE task SET status='queued',billing_status='charged',updated_at=? "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (time.time(), task_id),
        )
        return changed.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            "expert_task", tid, claim,
            note=f"任务#{task_id}·{note}"[:160], points=points)
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


def _role_binding_matches(
    connection, frozen: dict, *, require_current: bool,
) -> bool:
    """Check an exact role triple inside the same transaction as charging."""
    identity_ref = str(
        frozen.get("employee_identity_ref", frozen.get("identity_ref")) or ""
    ).strip()
    config_sha256 = str(
        frozen.get("employee_config_sha256", frozen.get("config_sha256")) or ""
    ).strip()
    bundle_sha256 = str(frozen.get("bundle_sha256") or "").strip()
    raw_revision = frozen.get(
        "employee_config_revision", frozen.get("config_revision")
    )
    raw_idx = frozen.get("emp_idx", frozen.get("idx"))
    try:
        revision = int(raw_revision)
        idx = int(raw_idx)
    except (TypeError, ValueError):
        return False
    if (
        re.fullmatch(r"[0-9a-f]{64}", identity_ref) is None
        or re.fullmatch(r"[0-9a-f]{64}", config_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", bundle_sha256) is None
        or revision < 1
    ):
        return False
    row = connection.execute(
        "SELECT * FROM employee_role_config WHERE identity_ref=? "
        "AND config_revision=?",
        (identity_ref, revision),
    ).fetchone()
    if row is None and not require_current:
        row = connection.execute(
            "SELECT * FROM employee_role_config_history WHERE identity_ref=? "
            "AND config_revision=?",
            (identity_ref, revision),
        ).fetchone()
    exact = bool(
        row
        and db.employee_role_config_row_valid(row)
        and int(row["idx"]) == idx
        and int(row["config_revision"]) == revision
        and str(row["config_sha256"]) == config_sha256
    )
    bundle = connection.execute(
        "SELECT * FROM employee_role_bundle_revision WHERE identity_ref=? "
        "AND config_revision=? AND config_sha256=? AND bundle_sha256=?",
        (identity_ref, revision, config_sha256, bundle_sha256),
    ).fetchone()
    exact = bool(exact and bundle and db.employee_role_bundle_row_valid(bundle))
    if not exact or not require_current:
        return exact
    slot = connection.execute(
        "SELECT active_identity_ref,enabled FROM employee_slot WHERE idx=?",
        (idx,),
    ).fetchone()
    return bool(
        slot
        and str(slot["active_identity_ref"] or "") == identity_ref
        and int(slot["enabled"] or 0) == 1
    )


def _initial_task_replay(
    task_data: dict,
    request_key: str,
    actor_id: int | None,
) -> dict | None:
    """只读复核首轮幂等身份；不同输入复用同号一律拒绝。"""
    row = db.one(
        "SELECT id,tenant_id,emp_idx,brief_json,created_by,source_task_id,"
        "phase,deleted_at,employee_key,employee_catalog_version,"
        "employee_name_snapshot,employee_dept_key,employee_spec_sha256,"
        "employee_identity_ref,employee_config_revision,"
        "employee_config_sha256,person_snapshot,identity_scheme,bundle_sha256 "
        "FROM task WHERE tenant_id=? AND request_key=?",
        (int(task_data["tenant_id"]), request_key),
    )
    if not row:
        return None
    expected_brief = db.jloads(task_data.get("brief_json"), None)
    actual_brief = db.jloads(row.get("brief_json"), None)
    same_actor = (
        (actor_id is None and row.get("created_by") is None)
        or (
            actor_id is not None
            and row.get("created_by") is not None
            and int(row["created_by"]) == int(actor_id)
        )
    )
    if (
        row.get("deleted_at") is not None
        or row.get("emp_idx") is None
        or int(row["emp_idx"]) != int(task_data["emp_idx"])
        or any(
            row.get(field) != task_data.get(field)
            for field in _TASK_IDENTITY_COLUMNS
        )
        or actual_brief != expected_brief
        or not same_actor
        or row.get("source_task_id") is not None
        or str(row.get("phase") or "delivery") != "delivery"
    ):
        raise taskthreads.IdempotencyConflict(
            "request_key_reused",
            "这个请求编号已用于其他任务，请刷新页面后重试",
        )
    return {"created": False, "task_id": int(row["id"])}


def _create_idempotent_expert_task(
    task_data: dict,
    request_key: str,
    actor_id: int | None,
    note: str = "",
) -> dict:
    """在同一 BEGIN IMMEDIATE 中核对幂等号、建任务并扣点。"""
    complete_binding = all(
        task_data.get(field) not in (None, "")
        for field in _TASK_IDENTITY_COLUMNS
    )
    if complete_binding:
        binding = employeeidentity.resolve_task_binding(task_data)
        employee = binding["employee"] if binding else None
        config = binding["config"] if binding else None
    elif all(
        str(task_data.get(field) or "").strip()
        for field in _TASK_IDENTITY_COLUMNS[:5]
    ):
        employee = employeeidentity.resolve_task(task_data)
        config = employees.ensure_role_config(employee) if employee else None
    else:
        employee = employeeidentity.active_employee(task_data.get("emp_idx"))
        config = employees.ensure_role_config(employee) if employee else None
    if not employee:
        raise RuntimeError("不允许向未知员工创建任务")
    task_data = {
        **task_data,
        **employeeidentity.task_fields(employee, config=config),
        "emp_idx": int(employee["idx"]),
    }
    with db.atomic() as connection:
        replay = _initial_task_replay(task_data, request_key, actor_id)
        if replay is not None:
            return replay
        if not _role_binding_matches(
            connection, task_data, require_current=True,
        ):
            raise taskthreads.IdempotencyConflict(
                "employee_binding_stale",
                "员工岗位配置已更新，请刷新后重试",
            )
        task_id = _create_charged_expert_task(
            {**task_data, "request_key": request_key, "created_by": actor_id},
            note=note,
        )
        return {"created": True, "task_id": int(task_id)}


def _start_expert_task_worker(task_id: int):
    return asyncio.create_task(
        taskrunner.run_task(task_id, engine.broadcast)
    )


def _settle_unstarted_expert_task(task_id: int) -> bool:
    return taskrunner.settle_failure(
        task_id,
        "任务启动失败，系统已安全终止并退回本次点数",
    )


async def _record_first_work_best_effort(tid: int, kind: str) -> None:
    try:
        await db.arun(funnel.record_first_work, tid, kind)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(
            "first-work funnel record skipped kind=%s error_type=%s",
            kind,
            type(exc).__name__,
        )


@app.post("/api/tasks")
async def task_create(body: dict):
    idx = body.get("emp_idx")
    if isinstance(idx, bool):
        raise HTTPException(400, "员工编号无效")
    employee = employeeidentity.active_employee(idx)
    if not employee:
        raise HTTPException(404, "员工不存在")
    idx = int(employee["idx"])
    binding = _employee_current_write_binding(idx, body)
    if not binding["identity"]["can_assign_new"]:
        raise HTTPException(409, "当前岗位不可新派活")
    if idx == inspection.EMPLOYEE_IDX:
        raise HTTPException(
            400,
            "巡店经理必须从“巡店”工作台上传现场照片后派活，不能创建无照片任务",
        )
    try:
        brief = taskrunner.normalize_task_brief(body.get("brief"), employee, TEN())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        request_key = taskthreads.normalize_request_key(body.get("request_key"))
    except taskthreads.TaskThreadError as exc:
        _raise_task_thread_error(exc)
    expert = departments.get_active(idx)
    if expert:
        await db.arun(_need_module, expert["dept_key"])
    else:
        await db.arun(_need_module, "content")
    if not await db.arun(employees.is_enabled, idx):
        raise HTTPException(400, "该员工已被停用(后台可重新启用)")
    actor_id = int((auth.current() or {}).get("id") or 0) or None
    task_data = {
        "emp_idx": idx,
        **employeeidentity.task_fields(employee, config=binding["config"]),
        "tenant_id": TEN(),
        "brief_json": json.dumps(brief, ensure_ascii=False),
        "created_by": actor_id,
    }
    try:
        replay = await db.arun(
            _initial_task_replay, task_data, request_key, actor_id,
        )
    except taskthreads.TaskThreadError as exc:
        _raise_task_thread_error(exc)
    if replay is not None:
        return {**replay, "replayed": True}

    # 派单预检:仅产业部专家(idx>=100)且未强制派单时,先判断任务书是否对口。
    # 不对口→直接返回引导,不扣点不建任务;LLM 异常/超时一律放行(降级可用,绝不拦死派单)。
    if isinstance(idx, int) and idx >= 100 and expert and not body.get("force"):
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
    try:
        created = await _run_db_then_start_worker_safely(
            _create_idempotent_expert_task,
            task_data,
            request_key,
            actor_id,
            note=(brief.get("direction") or "")[:20],
            start_worker=lambda result: _start_expert_task_worker(result["task_id"]),
            should_start=lambda result: bool(result.get("created")),
            settle_unstarted=lambda result: _settle_unstarted_expert_task(
                result["task_id"]
            ),
        )
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    except taskthreads.TaskThreadError as exc:
        _raise_task_thread_error(exc)
    asyncio.create_task(
        _record_first_work_best_effort(TEN(), "expert")
    )
    return {
        "task_id": int(created["task_id"]),
        "created": bool(created.get("created")),
        "replayed": not bool(created.get("created")),
        "identity_ref": binding["identity"]["identity_ref"],
        "config_revision": binding["config"]["config_revision"],
        "config_sha256": binding["config"]["config_sha256"],
        "bundle_sha256": binding["config"]["bundle_sha256"],
    }


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
def task_center(
    limit: int = 300,
    offset: int = 0,
    q: str = "",
    status: str = "all",
    kind: str = "all",
):
    """老板任务总账：聚合真实业务表，不复制状态，也不跨租户/越板块展示。"""
    modules = {m["key"] for m in auth.all_modules() if auth.allowed(m["key"])}
    try:
        result = taskcenter.list_items(
            TEN(),
            modules,
            limit=limit,
            offset=offset,
            q=(q or "").strip()[:100],
            status=status,
            kind=kind,
        )
        items = []
        for raw in result.get("items") or []:
            item = dict(raw)
            if item.get("kind") == "expert":
                identity = _task_identity_public(item["record_id"], TEN())
                if identity:
                    item.update(identity)
                if (identity or {}).get("identity_status") == "unknown":
                    item["assignee"] = "岗位身份待核"
            items.append(item)
        return {**result, "items": items}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


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
        row = await db.arun(_job_or_404, rid)
        usable = await db.arun(engine._has_usable_delivery, rid)
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
        tenant_id = TEN()

        def _retry_content():
            with db.atomic() as connection:
                if connection.execute(
                    "SELECT id FROM wechat_draft_delivery "
                    "WHERE tenant_id=? AND job_id=? AND status IN "
                    "('pending_charge','processing','submitting','submitted') "
                    "LIMIT 1",
                    (tenant_id, rid),
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
                        tenant_id,
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

        await _run_db_then_start_worker_safely(
            _retry_content,
            start_worker=lambda _result: _start_content_job_worker(rid),
            settle_unstarted=(
                lambda _result: _settle_unstarted_content_job(rid)
            ),
        )
        return {
            "ok": True, "kind": kind, "job_id": rid, "record_id": rid,
            "free_retry": True, "retry_count": retries + 1,
        }

    if kind == "video":
        row = await db.aone(
            "SELECT * FROM tv_job WHERE id=? AND tenant_id=?", (rid, TEN()))
        if not row:
            raise HTTPException(404)
        meta = taskcenter.retry_meta(kind, row)
        if not meta["retryable"]:
            _raise_retry_denied(meta)
        retries = int(row.get("retry_count") or 0)
        from . import textvideo

        def _retry_video():
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
            if changed == 1:
                try:
                    textvideo.cleanup_job_assets(rid, row)
                except Exception:
                    textvideo.settle_failure(
                        rid,
                        "成片任务重试准备失败，已安全终止",
                    )
                    raise
            return changed

        changed = await _run_db_then_start_worker_safely(
            _retry_video,
            start_worker=lambda _changed: _start_text_video_worker(rid),
            should_start=lambda value: value == 1,
            settle_unstarted=lambda _changed: textvideo.settle_failure(
                rid,
                "成片任务启动失败，已安全终止",
            ),
        )
        if changed != 1:
            raise HTTPException(
                409, "这个任务已经不在失败状态了——多半是刚刚已被重试(正在排队执行)或已被删除。刷新看最新进度即可,不会重复扣点")
        engine.broadcast({"type": "tv_done", "tv_id": rid, "tenant_id": TEN()})
        return {
            "ok": True, "kind": kind, "record_id": rid,
            "free_retry": True, "retry_count": retries + 1,
        }

    if kind == "tool":
        row = await db.aone(
            "SELECT * FROM tool_job WHERE id=? AND tenant_id=?", (rid, TEN()))
        if not row:
            raise HTTPException(404)
        meta = taskcenter.retry_meta(kind, row)
        if not meta["retryable"]:
            _raise_retry_denied(meta)
        retries = int(row.get("retry_count") or 0)
        def _retry_tool():
            return db.execute(
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

        def _settle_unstarted_tool(_changed):
            current = db.one(
                "SELECT * FROM tool_job WHERE id=? AND tenant_id=?",
                (rid, TEN()),
            )
            return bool(current) and _settle_tool_failure(
                current,
                "工具任务启动失败，已安全终止",
                "启动失败退回",
            )

        try:
            changed = await _run_db_then_start_worker_safely(
                _retry_tool,
                start_worker=lambda _changed: _spawn_tool_worker(rid),
                should_start=lambda value: value == 1,
                settle_unstarted=_settle_unstarted_tool,
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                409, "这个工具已有任务正在运行，请等它完成后再重试") from exc
        if changed != 1:
            raise HTTPException(
                409, "这个任务已经不在失败状态了——多半是刚刚已被重试(正在排队执行)或已被删除。刷新看最新进度即可,不会重复扣点")
        _broadcast_tool(TEN(), row.get("kind") or "")
        return {
            "ok": True, "kind": kind, "record_id": rid,
            "free_retry": True, "retry_count": retries + 1,
        }

    if kind == "meeting":
        row = await db.arun(_meeting_row_or_404, rid)
        meta = taskcenter.retry_meta(kind, row)
        if not meta["retryable"]:
            _raise_retry_denied(meta)
        retries = int(row.get("retry_count") or 0)
        tenant_id = TEN()

        def _retry_meeting():
            with db.atomic() as connection:
                current = connection.execute(
                    "SELECT execution_task_ids_json FROM meeting "
                    "WHERE id=? AND tenant_id=?",
                    (rid, tenant_id),
                ).fetchone()
                execution_ids = db.jloads(
                    current["execution_task_ids_json"] if current else None,
                    [],
                ) or []
                has_derived = bool(execution_ids) or bool(connection.execute(
                    "SELECT id FROM task WHERE tenant_id=? "
                    "AND source_meeting_id=? AND deleted_at IS NULL LIMIT 1",
                    (tenant_id, rid),
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
                        now,
                        rid,
                        tenant_id,
                        row.get("billing_status"),
                        retries,
                    ),
                )
                if changed.rowcount != 1:
                    raise HTTPException(
                        409, "会议状态刚刚发生变化，请刷新后再重试")

        await _run_db_then_start_worker_safely(
            _retry_meeting,
            start_worker=lambda _result: _start_meeting_worker(rid),
            settle_unstarted=lambda _result: meeting.settle_failure(
                rid,
                "会议重试启动失败，已安全终止",
            ),
        )
        engine.broadcast({
            "type": "meeting_update",
            "tenant_id": tenant_id,
            "_required_modules": meeting._event_scope(row)[1],
            "meeting_id": rid,
        })
        return {
            "ok": True, "kind": kind, "record_id": rid,
            "free_retry": True, "retry_count": retries + 1,
        }

    if kind == "publish":
        row = await db.aone(
            "SELECT * FROM pub_task WHERE id=? AND tenant_id=?", (rid, TEN()))
        if not row:
            raise HTTPException(404)
        meta = taskcenter.retry_meta(kind, row)
        if not meta["retryable"]:
            _raise_retry_denied(meta)
        retries = int(row.get("retry_count") or 0)
        tenant_id = TEN()

        def _retry_publish():
            return db.execute(
                "UPDATE pub_task SET status='queued',fail_json=NULL,"
                "retry_count=retry_count+1,submit_started_at=NULL,updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='failed' "
                "AND submission_state='not_submitted' AND retry_count=?",
                (now, rid, tenant_id, retries),
            )

        def _settle_unstarted_publish(_changed):
            fail = {
                "kind": "unknown",
                "why": "发布任务未能启动",
                "fix": "可点击免费重试重新排队",
                "err": "自动发布未开始，内容未发出",
                "shot": "",
                "home": "",
            }
            return db.execute(
                "UPDATE pub_task SET status='failed',fail_json=?,updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='queued' "
                "AND submission_state='not_submitted'",
                (
                    json.dumps(fail, ensure_ascii=False),
                    time.time(),
                    rid,
                    tenant_id,
                ),
            ) == 1

        changed = await _run_db_then_start_worker_safely(
            _retry_publish,
            start_worker=lambda _changed: _start_publish_worker(rid),
            should_start=lambda value: value == 1,
            settle_unstarted=_settle_unstarted_publish,
        )
        if changed != 1:
            raise HTTPException(
                409, "发布任务状态刚刚发生变化，请刷新后再重试")
        engine.broadcast({
            "type": "pub_update",
            "tenant_id": tenant_id,
            "task_id": rid,
        })
        return {
            "ok": True, "kind": kind, "record_id": rid,
            "free_retry": True, "retry_count": retries + 1,
            "note": "已按原发布单免费重新排队",
        }

    row = await db.aone(
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
    if int(t.get("emp_idx") or 0) == inspection.EMPLOYEE_IDX:
        try:
            t["_inspection_scope"] = inspection.task_scope(
                TEN(), int((auth.current() or {}).get("id") or 0), tid
            )
        except inspection.InspectionError as exc:
            # 任务详情不泄露其他租户/行业是否存在巡店记录。
            raise HTTPException(404, "巡店任务不存在或无权访问") from exc
        module = str(t["_inspection_scope"]["industry_key"])
    else:
        binding = employeeidentity.resolve_task_binding(t)
        if not binding:
            raise HTTPException(404)
        employee = binding["employee"]
        module = str(t.get("employee_dept_key") or "")
        t["_roster_meta"] = employeeidentity.roster_metadata_from_task(t)
        t["_frozen_employee"] = employee
        t["_frozen_employee_config"] = binding["config"]
    if not auth.allowed(module):
        raise HTTPException(404)
    return t


@app.get("/api/tasks/{tid}")
def task_get(tid: int):
    t = _task_row_or_404(tid)
    inspection_scope = t.pop("_inspection_scope", None)
    roster_meta = t.pop("_roster_meta", None)
    frozen_employee = t.pop("_frozen_employee", None)
    frozen_config = t.pop("_frozen_employee_config", None)
    t["brief"] = db.jloads(t.pop("brief_json"))
    t["steps"] = _steps_for_view(
        t.pop("steps_json"), _is_boss(), status=t.get("status")
    )
    t["emp_name"] = (
        f"{str(t.get('person_snapshot') or '').strip()}·"
        f"{str(t.get('employee_name_snapshot') or '').strip()}"
    ).strip("·") or "岗位身份待核"
    if roster_meta is None:
        roster_meta = employeeidentity.roster_metadata_from_task(t) or {
            "roster_status": "legacy", "can_assign": False,
            "person_status": "inactive", "identity_status": "unknown",
            "can_assign_new": False, "can_continue": False,
            "can_learn": False,
        }
    if inspection_scope:
        inspection_employee = employeeidentity.active_employee(
            int(t.get("emp_idx") or inspection.EMPLOYEE_IDX)
        )
        inspection_config = (
            employees.get_config(int(inspection_employee["idx"]))
            if inspection_employee else None
        )
        identity = (
            _employee_public_contract(
                inspection_employee,
                config=inspection_config,
                include_profile=True,
            )
            if inspection_employee and inspection_config else {
                "person_status": "active", "identity_status": "current",
                "identity_ref": "inspection", "config_revision": 0,
                "config_sha256": "", "can_assign_new": True,
                "can_continue": False, "can_learn": True,
                "role_profile_summary": {}, "professional_profile": {},
                "roster_status": "active", "can_assign": True,
            }
        )
    elif frozen_employee and frozen_config:
        identity = _employee_public_contract(
            frozen_employee, config=frozen_config, include_profile=True,
        )
    else:
        identity = {
            "person_status": roster_meta["person_status"],
            "identity_status": roster_meta["identity_status"],
            "identity_ref": str(t.get("employee_identity_ref") or ""),
            "config_revision": int(t.get("employee_config_revision") or 0),
            "config_sha256": str(t.get("employee_config_sha256") or ""),
            "can_assign_new": False,
            "can_continue": bool(roster_meta["can_continue"]),
            "can_learn": False,
            "role_profile_summary": {},
            "professional_profile": {},
            "roster_status": roster_meta["roster_status"],
            "can_assign": False,
        }
    t.update(identity)
    if departments.is_decision_employee(frozen_employee):
        # The guide is regenerated only from the employee which matched all
        # frozen task identity fields in _task_row_or_404.  Never resolve by a
        # possibly reused current emp_idx and never echo client requirements.
        t["task_guide"] = departments.public_task_guide(frozen_employee)
    dept_key = str(t.get("employee_dept_key") or "")
    if dept_key == "content":
        t["dept_name"] = "内容生产部"
    else:
        # 历史任务只能按创建时冻结的部门 key 解释。不能再用 emp_idx
        # 回查当前目录，否则同一编号被新员工复用后会把旧任务改挂到新部门。
        frozen_dept = next(
            (
                item for item in departments.list_depts()
                if str(item.get("key") or "") == dept_key
            ),
            None,
        )
        t["dept_name"] = (
            str(frozen_dept.get("name") or dept_key)
            if frozen_dept else f"岗位部门 {dept_key}"
        )
    # 发起人:是哪个账号派的活;查不到(历史任务/跨租户)为 None
    t["created_by_name"] = _tenant_username(t.get("created_by"))
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
    if inspection_scope:
        t["source"] = {
            "type": "inspection",
            "label": f"巡店记录 #{inspection_scope['visit_id']}",
            "route": (
                f"#/inspections/{inspection_scope['visit_id']}/"
                f"{inspection_scope['industry_key']}"
            ),
            "detail": "现场照片、问题证据、整改与人工复核在巡店工作台闭环",
        }
    elif t.get("source_meeting_id"):
        m = db.one(
            "SELECT question,emp_idxs_json,member_snapshot_json FROM meeting "
            "WHERE id=? AND tenant_id=?",
                   (t["source_meeting_id"], TEN()))
        visible = bool(m and _meeting_visible(m))
        t["source"] = {
            "type": "meeting",
            "label": f"AI会议 #{t['source_meeting_id']}" if visible else "原会议已删除",
            "route": f"#/meetings/{t['source_meeting_id']}" if visible else "",
            "detail": m.get("question") if visible else "",
        }
    elif t.get("source_task_id"):
        try:
            parent = _task_row_or_404(int(t["source_task_id"]))
        except (HTTPException, TypeError, ValueError):
            parent = None
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
    try:
        if inspection_scope:
            t["thread"] = {
                "status": "unsupported",
                "can_continue": False,
                "can_accept": False,
                "revisions": [],
                "reason_code": "inspection_workflow_only",
            }
        else:
            t["thread"] = taskthreads.thread_summary_for_task(tid, TEN())
    except taskthreads.TaskThreadError as exc:
        # 数据不完整时不能把损坏的会话伪装成可继续；正文仍可只读查看。
        t["thread"] = {
            "status": "unavailable",
            "can_continue": False,
            "can_accept": False,
            "revisions": [],
            "reason_code": exc.code,
        }
    t["can_continue"] = bool(
        t.get("can_continue") and t["thread"].get("can_continue")
    )
    return t


def _raise_task_thread_error(exc: taskthreads.TaskThreadError):
    if isinstance(exc, taskthreads.TaskThreadNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, taskthreads.InvalidFollowup):
        raise HTTPException(400, str(exc)) from exc
    raise HTTPException(409, str(exc)) from exc


def _require_frozen_task_write_binding(body: dict, task: dict) -> None:
    if any(body.get(field) in (None, "") for field in _ROLE_WRITE_BINDING_FIELDS):
        raise HTTPException(400, "任务岗位四元绑定必须完整提交")
    expected_identity = str(body.get("identity_ref") or "").strip()
    expected_hash = str(body.get("config_sha256") or "").strip()
    expected_bundle = str(body.get("bundle_sha256") or "").strip()
    raw_revision = body.get("config_revision")
    if (
        re.fullmatch(r"[0-9a-f]{64}", expected_identity) is None
        or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        or re.fullmatch(r"[0-9a-f]{64}", expected_bundle) is None
        or isinstance(raw_revision, bool)
        or not isinstance(raw_revision, int)
        or raw_revision < 1
    ):
        raise HTTPException(400, "任务岗位四元绑定无效")
    if (
        expected_identity != str(task.get("employee_identity_ref") or "")
        or raw_revision != int(task.get("employee_config_revision") or 0)
        or expected_hash != str(task.get("employee_config_sha256") or "")
        or expected_bundle != str(task.get("bundle_sha256") or "")
    ):
        raise HTTPException(409, "任务岗位配置已变更，请刷新后重试")


async def _create_task_followup(tid: int, body: dict):
    if not isinstance(body, dict):
        raise HTTPException(400, "继续沟通请求格式无效")
    if {"decision_evidence", "provenance", "web_sources"} & set(body):
        raise HTTPException(400, "请求包含不可由客户端写入的证据字段")
    task = await db.arun(_task_row_or_404, tid)
    if int(task.get("emp_idx") or 0) == inspection.EMPLOYEE_IDX:
        raise HTTPException(
            409,
            "巡店任务需要用整改进度和复查照片继续，不支持文本重做",
        )
    _require_frozen_task_write_binding(body, task)
    frozen_employee = task.get("_frozen_employee")
    if not frozen_employee or not employeeidentity.identity_view(
        frozen_employee,
    )["can_continue"]:
        raise HTTPException(409, "员工当前不可继续这条任务线程")
    feedback = body.get("feedback")
    material = body.get("material")
    evidence_items = (
        body.get("evidence_items")
        if "evidence_items" in body
        else taskthreads.EVIDENCE_ITEMS_ABSENT
    )
    request_key = body.get("request_key")
    try:
        result = await _run_db_then_start_worker_safely(
            taskthreads.create_followup,
            tid,
            TEN(),
            request_key,
            feedback,
            _create_charged_expert_task,
            material=material,
            evidence_items=evidence_items,
            actor_id=int((auth.current() or {}).get("id") or 0) or None,
            expected_emp_idx=int(task["emp_idx"]),
            start_worker=lambda row: _start_expert_task_worker(row["task_id"]),
            should_start=lambda row: bool(row.get("created")),
            settle_unstarted=lambda row: _settle_unstarted_expert_task(
                row["task_id"]
            ),
        )
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    except taskthreads.TaskThreadError as exc:
        _raise_task_thread_error(exc)
    return result


@app.post("/api/tasks/{tid}/followups")
async def task_followup(tid: int, body: dict):
    return await _create_task_followup(tid, body)


@app.post("/api/tasks/{tid}/redo")
async def task_redo(tid: int, body: dict):
    """旧客户端兼容入口；所有重做统一进入线性协作会话。"""
    feedback = body.get("feedback")
    request_key = body.get("request_key")
    if not isinstance(request_key, str) or not request_key.strip():
        raise HTTPException(
            400,
            "请求编号必填；请生成唯一 request_key 后重试，"
            "同一次重试必须复用原编号",
        )
    return await _create_task_followup(
        tid,
        {
            "feedback": feedback,
            "material": body.get("material"),
            "request_key": request_key,
            "identity_ref": body.get("identity_ref"),
            "config_revision": body.get("config_revision"),
            "config_sha256": body.get("config_sha256"),
            "bundle_sha256": body.get("bundle_sha256"),
            **(
                {"evidence_items": body.get("evidence_items")}
                if "evidence_items" in body else {}
            ),
        },
    )


@app.post("/api/task-threads/{thread_id}/accept")
async def task_thread_accept(thread_id: int, body: dict | None = None):
    body = body if isinstance(body, dict) else {}
    task_id = body.get("task_id")
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
        raise HTTPException(400, "当前任务编号无效")
    task = await db.arun(_task_row_or_404, task_id)
    if int(task.get("emp_idx") or 0) == inspection.EMPLOYEE_IDX:
        raise HTTPException(
            409,
            "巡店问题只能在上传复查照片后由企业主人工关单",
        )
    was_standalone = task.get("thread_id") is None
    if was_standalone and int(thread_id) != int(task_id):
        raise HTTPException(409, "任务协作会话尚未建立，请刷新后重试")
    if not was_standalone and int(task.get("thread_id") or 0) != int(thread_id):
        raise HTTPException(409, "任务不属于这个协作会话")
    try:
        summary = await db.arun(
            taskthreads.mark_satisfied,
            task_id,
            TEN(),
            int((auth.current() or {}).get("id") or 0) or None,
            expected_emp_idx=int(task["emp_idx"]),
        )
    except taskthreads.TaskThreadError as exc:
        _raise_task_thread_error(exc)
    if not was_standalone and int(summary.get("thread_id") or 0) != int(thread_id):
        raise HTTPException(409, "协作会话刚刚发生变化，请刷新后重试")
    return {"ok": True, "thread": summary}


@app.post("/api/tasks/{tid}/retry")
async def task_retry(tid: int):
    """失败任务原单免费重试；用 CAS 防止双击或并发重复开工。"""
    task = await db.arun(_task_row_or_404, tid)
    if task.get("status") != "failed":
        raise HTTPException(409, "只有失败任务可以免费重试")
    is_inspection = int(task.get("emp_idx") or 0) == inspection.EMPLOYEE_IDX
    prepared = await _run_db_then_start_worker_safely(
        _prepare_inspection_retry if is_inspection else taskrunner.prepare_retry,
        tid,
        TEN(),
        start_worker=(
            (lambda _prepared: asyncio.create_task(_run_inspection_task(tid)))
            if is_inspection
            else (lambda _prepared: _start_expert_task_worker(tid))
        ),
        should_start=bool,
        settle_unstarted=(
            (lambda _prepared: _settle_inspection_task_by_id(
                tid, "巡店重试未能启动，已安全终止"
            ))
            if is_inspection
            else (lambda _prepared: _settle_unstarted_expert_task(tid))
        ),
    )
    if not prepared:
        current = await db.aone(
            "SELECT retry_count FROM task WHERE id=? AND tenant_id=?",
            (tid, TEN()),
        ) or {}
        if (current.get("retry_count") or 0) >= taskrunner.MAX_FREE_RETRIES:
            raise HTTPException(429, "该任务免费重试次数已用完，请新建任务")
        if is_inspection:
            raise HTTPException(
                409,
                "这条巡店的原照片或巡店记录不完整，无法安全免费重试；"
                "请回到巡店工作台重新发起",
            )
        raise HTTPException(409, "这个任务已经不在失败状态了——多半是刚刚已被重试(正在排队执行)或已被删除。刷新看最新进度即可,不会重复扣点")
    inspection_scope = task.get("_inspection_scope") or {}
    required_module = str(
        inspection_scope.get("industry_key")
        or task.get("employee_dept_key")
        or ""
    ).strip()
    if required_module in {"", "unknown", "__denied__"}:
        raise HTTPException(404)
    engine.broadcast(
        {
            "type": "task_update",
            "tenant_id": TEN(),
            "_required_modules": (required_module,),
            "task_id": tid,
            "idx": task["emp_idx"],
        }
    )
    row = await db.aone(
        "SELECT retry_count FROM task WHERE id=?", (tid,)
    ) or {}
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
    if int(task.get("emp_idx") or 0) == inspection.EMPLOYEE_IDX:
        raise HTTPException(
            409,
            "巡店任务是问题、整改和人工复核的审计记录，不能单独删除",
        )
    guard = taskthreads.task_deletion_guard(tid, TEN())
    if not guard.get("allowed"):
        raise HTTPException(409, guard.get("message") or "该任务属于协作版本链，不能单独删除")
    llm.kill(f"task{tid}:")
    if (task.get("status") == "pending_charge"
            and task.get("billing_status") == "pending"):
        terminal_at = time.time()
        db.execute(
            "UPDATE task SET status='failed',billing_status='void',"
            "output_md=?,terminal_at=?,updated_at=? "
            "WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending' AND deleted_at IS NULL",
            ("任务在扣费前被移入回收站", terminal_at, terminal_at, tid),
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
    try:
        deleted = taskthreads.soft_delete_task(
            tid,
            TEN(),
            actor_id=int((auth.current() or {}).get("id") or 0) or None,
        )
    except taskthreads.TaskThreadError as exc:
        _raise_task_thread_error(exc)
    taskrunner.sync_meeting_delivery_for_task(tid)
    return deleted


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
            out.append(
                _public_station(
                    s, include_task_guide=not _is_tour(), config=cfg,
                )
            )
            continue
        model_id = providers.text_model_for(s["idx"])
        out.append({
            **{k: v for k, v in s.items() if k != "run"},
            **_employee_public_contract(
                s, config=cfg, include_profile=True,
            ),
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
            "task_guide": _public_station_task_guide(s),
            "stats": {"runs": st.get("runs", 0), "cost_usd": st.get("cost") or 0,
                      "tokens": st.get("tokens") or 0, "avg_ms": st.get("avg_ms") or 0},
        })
    return out


def _is_emp(idx: int) -> bool:
    return idx in registry.BY_IDX or departments.get_active(idx) is not None


@app.put("/api/employees/{idx}/prompt")
def employee_prompt(idx: int, body: dict):
    _need_boss()
    binding = _employee_current_write_binding(idx, body)
    try:
        employees.set_prompt_for_identity(
            binding["identity"]["identity_ref"],
            body.get("template"),
            expected_revision=binding["config"]["config_revision"],
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    fresh = employees.get_config(idx)
    return {
        "ok": True,
        "is_custom": bool((body.get("template") or "").strip()),
        **_employee_public_contract(binding["employee"], config=fresh),
    }


@app.put("/api/employees/{idx}/skills")
def employee_skills(idx: int, body: dict):
    _need_boss()   # employee_config 是全平台共享表，仅命名超级账号可改。
    binding = _employee_current_write_binding(idx, body)
    skills = body.get("skills")
    if not isinstance(skills, list):
        raise HTTPException(400, "skills 必须是数组")
    if not auth.is_root():
        # 非 root 客户端拿到的技能卡没有 source(脱敏),整表回存时把库里原有来源补回来
        old_src = {(s.get("title"), s.get("detail")): s.get("source")
                   for s in binding["config"]["skills"]}
        for s in skills:
            if "source" not in s:
                src = old_src.get((s.get("title"), s.get("detail")))
                if src is not None:
                    s["source"] = src
    try:
        employees.set_skills_for_identity(
            binding["identity"]["identity_ref"],
            skills,
            expected_revision=binding["config"]["config_revision"],
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    fresh = employees.get_config(idx)
    return {"ok": True, **_employee_public_contract(
        binding["employee"], config=fresh,
    )}


@app.put("/api/employees/{idx}/settings")
def employee_settings(idx: int, body: dict):
    _need_boss()   # 全局共享配置,仅命名超级账号
    """员工工作配置(V3):趋势官/情报员检索渠道、拆解师对标与维度。传 {} 恢复默认."""
    if idx not in registry.BY_IDX:
        raise HTTPException(404)
    binding = _employee_current_write_binding(idx, body)
    settings = body.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(400, "settings 必须是对象")
    try:
        employees.set_settings_for_identity(
            binding["identity"]["identity_ref"], settings,
            expected_revision=binding["config"]["config_revision"],
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    fresh = employees.get_config(idx)
    return {
        "ok": True,
        "settings": registry.station_settings(registry.BY_IDX[idx]["key"]),
        **_employee_public_contract(binding["employee"], config=fresh),
    }


@app.put("/api/employees/{idx}/capabilities")
def employee_capabilities(idx: int, body: dict):
    _need_boss()   # 全局共享配置,仅命名超级账号
    """员工能力开关(V4):body.caps_off = 停用的能力名列表."""
    binding = _employee_current_write_binding(idx, body)
    caps_off = body.get("caps_off")
    if not isinstance(caps_off, list):
        raise HTTPException(400, "caps_off 必须是数组")
    if idx in registry.BY_IDX:
        valid = {c["name"] for c in registry.CAPABILITIES.get(registry.BY_IDX[idx]["key"], [])}
        caps = None
    else:
        valid = {
            c["name"] for c in departments.capabilities_for(
                idx, [],
                employee=_employee_effective_view(
                    binding["employee"], binding["config"],
                ),
            )
        }
        caps = None
    try:
        employees.set_caps_off_for_identity(
            binding["identity"]["identity_ref"],
            [c for c in caps_off if c in valid],
            expected_revision=binding["config"]["config_revision"],
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    fresh = employees.get_config(idx)
    caps = (
        registry.capabilities_for(idx) if idx in registry.BY_IDX
        else departments.capabilities_for(
            idx, fresh["caps_off"],
            employee=_employee_effective_view(binding["employee"], fresh),
        )
    )
    return {
        "ok": True, "capabilities": caps,
        **_employee_public_contract(binding["employee"], config=fresh),
    }


_LEARNING_REQUIRED_ARTIFACT_KINDS = (
    "knowledge", "skill", "capability", "workflow",
)


def _learning_run_checkpoint(run: dict) -> dict:
    value = run.get("checkpoint_json")
    if isinstance(value, dict):
        return dict(value)
    parsed = db.jloads(value, {})
    return dict(parsed) if isinstance(parsed, dict) else {}


def _expire_learning_run_if_due(run_id: int) -> dict:
    """Lazily expire an unfired/pending run without expiring future rows.

    ``employeelearning.expire_run(..., now=...)`` historically treats any
    explicit ``now`` as authority to expire unconditionally.  Keep the due
    comparison and mutation in one write transaction and deliberately call
    it without that unsafe override.  Queued rows have delivered no research,
    so any defensive internal reservation is released before terminalizing;
    awaiting-review rows retain their already-consumed research accounting.
    """
    with db.atomic():
        run = employeelearning.get_run(int(run_id))
        if str(run.get("status") or "") not in {
            employeelearning.RUN_QUEUED,
            employeelearning.RUN_AWAITING_APPROVAL,
        }:
            return run
        try:
            expires_at = float(run.get("expires_at"))
        except (TypeError, ValueError):
            return run
        if expires_at > time.time():
            return run
        if run.get("status") == employeelearning.RUN_QUEUED:
            employeelearning.release_budget(int(run_id))
        employeelearning.expire_run(int(run_id))
        return employeelearning.get_run(int(run_id))


def _expire_due_learning_identity_runs(identity_ref: str) -> int:
    """Release expired live owners before a new idempotent run is created."""
    role_ref = str(identity_ref or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", role_ref) is None:
        return 0
    rows = db.q(
        "SELECT id FROM employee_learning_run WHERE identity_ref=? "
        "AND status IN (?,?) AND expires_at IS NOT NULL AND expires_at<=? "
        "ORDER BY id LIMIT 20",
        (
            role_ref,
            employeelearning.RUN_QUEUED,
            employeelearning.RUN_AWAITING_APPROVAL,
            time.time(),
        ),
    )
    for row in rows:
        _expire_learning_run_if_due(int(row["id"]))
    return len(rows)


def _expire_due_learning_batch_runs(batch_id: int) -> int:
    """Bounded read-path sweep for one at-most-360-target manifest."""
    rows = db.q(
        "SELECT id FROM employee_learning_run WHERE batch_id=? "
        "AND status IN (?,?) AND expires_at IS NOT NULL AND expires_at<=? "
        "ORDER BY id LIMIT ?",
        (
            int(batch_id),
            employeelearning.RUN_QUEUED,
            employeelearning.RUN_AWAITING_APPROVAL,
            time.time(),
            _LEARNING_BATCH_MAX_TARGETS,
        ),
    )
    for row in rows:
        _expire_learning_run_if_due(int(row["id"]))
    return len(rows)


def _learning_owned_run(run_id: int) -> tuple[dict, dict]:
    """Resolve one run through its tenant-owned batch, fail closed."""
    try:
        run = employeelearning.get_run(int(run_id))
        batch = employeelearning.get_batch(int(run["batch_id"]))
    except (TypeError, ValueError, employeelearning.LearningError) as exc:
        raise HTTPException(404, "进修记录不存在") from exc
    try:
        tenant_id = int(batch.get("tenant_id"))
    except (TypeError, ValueError):
        tenant_id = -1
    if tenant_id != TEN():
        raise HTTPException(404, "进修记录不存在")
    run = _expire_learning_run_if_due(int(run["id"]))
    return run, employeelearning.get_batch(int(run["batch_id"]))


def _learning_run_idx(run: dict) -> int:
    try:
        idx = int(run.get("employee_idx") or 0)
    except (TypeError, ValueError):
        idx = 0
    if idx <= 0:
        raise HTTPException(409, "进修记录缺少员工身份快照")
    return idx


def _learning_run_bundle_sha256(run: dict) -> str:
    value = str(
        _learning_run_checkpoint(run).get("expected_bundle_sha256") or ""
    ).strip()
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise HTTPException(409, "进修记录缺少能力包快照")
    return value


def _learning_billing_op_key(tenant_id: int, run_id: int) -> str:
    return f"employee-learning:{int(tenant_id)}:{int(run_id)}"


def _start_learning_billing_at_frozen_price(
    run_id: int, tenant_id: int, note: str,
) -> str:
    """Validate the frozen three-point contract and charge in one DB lock."""
    with db.atomic():
        run = employeelearning.get_run(int(run_id))
        batch = employeelearning.get_batch(int(run["batch_id"]))
        if int(batch.get("tenant_id") or -1) != int(tenant_id):
            raise employeelearning.InvalidTransitionError(
                "进修计费租户绑定已变化"
            )
        metadata = _learning_batch_metadata(batch)
        schema = str(metadata.get("schema") or "")
        expected_points = float(run.get("budget_points") or 0)
        if schema == "schema55-learning-batch-v1":
            try:
                frozen_points = float(metadata["points_per_employee"])
            except (KeyError, TypeError, ValueError) as exc:
                raise employeelearning.InvalidTransitionError(
                    "批次进修计费证明缺失"
                ) from exc
        elif schema == "schema55-learning-single-v1":
            frozen_points = _LEARNING_BATCH_POINTS_PER_EMPLOYEE
        else:
            raise employeelearning.InvalidTransitionError(
                "进修计费模式不可验证"
            )
        try:
            live_points = float((billing.prices().get("learn") or {})["points"])
        except (KeyError, TypeError, ValueError) as exc:
            raise employeelearning.InvalidTransitionError(
                "进修计费单价不可用"
            ) from exc
        if (
            not all(math.isfinite(value) for value in (
                expected_points, frozen_points, live_points,
            ))
            or abs(expected_points - _LEARNING_BATCH_POINTS_PER_EMPLOYEE) > 1e-9
            or abs(frozen_points - expected_points) > 1e-9
            or abs(live_points - frozen_points) > 1e-9
        ):
            raise employeelearning.InvalidTransitionError(
                "进修计费单价已变化，本次未扣款也未联网"
            )
        op_key = _learning_billing_op_key(int(tenant_id), int(run_id))
        started = billing.start_operation(
            "learn",
            tid=int(tenant_id),
            note=str(note or "")[:200],
            op_key=op_key,
        )
        operation = db.one(
            "SELECT points,status FROM billing_operation WHERE op_key=?",
            (op_key,),
        )
        wallet_points = (
            0.0 if int(tenant_id) == 1 else expected_points
        )
        try:
            operation_points = float((operation or {}).get("points"))
        except (TypeError, ValueError):
            operation_points = float("nan")
        if (
            started != op_key
            or not operation
            or str(operation.get("status") or "") != "charged"
            or not math.isfinite(wallet_points)
            or not math.isfinite(operation_points)
            or abs(operation_points - wallet_points) > 1e-9
        ):
            raise employeelearning.InvalidTransitionError(
                "进修计费操作与冻结预算不一致"
            )
        return op_key


def _learning_checkpoint_billing_op(run: dict) -> str:
    value = str(_learning_run_checkpoint(run).get("billing_op_key") or "").strip()
    return value if re.fullmatch(r"employee-learning:\d+:\d+", value) else ""


def _learning_run_has_durable_proposal_delivery(run: dict) -> bool:
    """Prove that research produced the exact immutable proposal ledger."""
    try:
        run_id = int(run["id"])
        proposal = db.jloads(run.get("proposal_json"), {})
        checkpoint = _learning_run_checkpoint(run)
        if (
            not isinstance(proposal, dict)
            or proposal.get("proposal_only") is not True
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(checkpoint.get("expected_bundle_sha256") or ""),
            ) is None
        ):
            return False
        source_ids = sorted({int(value) for value in proposal.get("source_ids", [])})
        artifact_ids = sorted({
            int(value) for value in proposal.get("artifact_ids", [])
        })
        if not source_ids or not artifact_ids or min(source_ids + artifact_ids) < 1:
            return False
        actual_sources = [
            int(row["id"]) for row in db.q(
                "SELECT id FROM employee_learning_source WHERE run_id=? ORDER BY id",
                (run_id,),
            )
        ]
        artifacts = db.q(
            "SELECT id,source_ids_json FROM employee_learning_artifact "
            "WHERE run_id=? ORDER BY id",
            (run_id,),
        )
        if actual_sources != source_ids or [
            int(row["id"]) for row in artifacts
        ] != artifact_ids:
            return False
        for artifact in artifacts:
            linked = db.jloads(artifact.get("source_ids_json"), [])
            if (
                not isinstance(linked, list)
                or not linked
                or any(int(value) not in source_ids for value in linked)
            ):
                return False
        return True
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _terminalize_learning_run_after_worker_failure(
    run_id: int, reason: str,
) -> dict:
    with db.atomic():
        run = employeelearning.get_run(int(run_id))
        if run.get("status") in {
            employeelearning.RUN_QUEUED,
            employeelearning.RUN_RESEARCHING,
        }:
            employeelearning.cancel_run(
                int(run_id), reason=str(reason or "WORKER_FAILED")[:500],
            )
        employeelearning.release_budget(int(run_id))
        return employeelearning.get_run(int(run_id))


def _force_terminalize_learning_run_after_worker_failure(
    run_id: int, reason: str,
) -> dict:
    """Last-resort service fallback after repeated transition-hook faults."""
    with db.atomic():
        run = employeelearning.get_run(int(run_id))
        if run.get("status") in {
            employeelearning.RUN_QUEUED,
            employeelearning.RUN_RESEARCHING,
        }:
            employeelearning._set_run_status(
                run,
                employeelearning.RUN_CANCELLED,
                error_code=str(reason or "WORKER_FAILED")[:500],
            )
            employeelearning._refresh_batch_progress(int(run["batch_id"]))
        employeelearning.release_budget(int(run_id))
        return employeelearning.get_run(int(run_id))


async def _terminalize_learning_run_safely(
    run_id: int, reason: str,
) -> dict:
    last_error = None
    for _attempt in range(3):
        try:
            return await _run_db_safely(
                _terminalize_learning_run_after_worker_failure,
                int(run_id),
                reason,
            )
        except BaseException as exc:
            last_error = exc
    try:
        return await _run_db_safely(
            _force_terminalize_learning_run_after_worker_failure,
            int(run_id),
            reason,
        )
    except BaseException:
        if last_error is not None:
            raise last_error
        raise


def _settle_failed_learning_run_atomically(
    run_id: int,
    op_key: str,
    billing_reason: str,
    terminal_reason: str,
    *,
    force_terminal: bool = False,
) -> dict:
    """Refund, terminalize and release the internal cap in one commit."""
    with db.atomic():
        run = employeelearning.get_run(int(run_id))
        delivered = run.get("status") in {
            employeelearning.RUN_AWAITING_APPROVAL,
            employeelearning.RUN_ACTIVATED,
            employeelearning.RUN_REJECTED,
            employeelearning.RUN_STALE,
        } or (
            run.get("status") == employeelearning.RUN_EXPIRED
            and _learning_run_has_durable_proposal_delivery(run)
        )
        if delivered:
            # A notification/scheduler failure after the proposal commit is
            # not a research failure. Preserve its consumed cap and settle
            # the wallet operation as delivered.
            if op_key:
                billing.complete_operation(op_key)
            employeelearning._refresh_batch_progress(int(run["batch_id"]))
            return employeelearning.get_run(int(run_id))
        if op_key:
            billing.fail_operation(op_key, billing_reason)
        if run.get("status") in {
            employeelearning.RUN_QUEUED,
            employeelearning.RUN_RESEARCHING,
        }:
            if force_terminal:
                employeelearning._set_run_status(
                    run,
                    employeelearning.RUN_CANCELLED,
                    error_code=str(terminal_reason or "WORKER_FAILED")[:500],
                )
                employeelearning._refresh_batch_progress(int(run["batch_id"]))
            else:
                employeelearning.cancel_run(
                    int(run_id),
                    reason=str(terminal_reason or "WORKER_FAILED")[:500],
                )
        employeelearning.release_budget(int(run_id))
        # ``research_run`` can already have written failed/evidence_insufficient
        # before this settlement begins.  Refresh even when no status mutation
        # was needed so the durable batch counters agree with the terminal run.
        employeelearning._refresh_batch_progress(int(run["batch_id"]))
        return employeelearning.get_run(int(run_id))


async def _settle_failed_learning_run_safely(
    run_id: int,
    op_key: str,
    billing_reason: str,
    terminal_reason: str,
) -> dict:
    last_error = None
    for _attempt in range(3):
        try:
            return await _run_db_safely(
                _settle_failed_learning_run_atomically,
                int(run_id),
                op_key,
                billing_reason,
                terminal_reason,
            )
        except BaseException as exc:
            last_error = exc
    try:
        return await _run_db_safely(
            _settle_failed_learning_run_atomically,
            int(run_id),
            op_key,
            billing_reason,
            terminal_reason,
            force_terminal=True,
        )
    except BaseException:
        if last_error is not None:
            raise last_error
        raise


def _recover_employee_learning_billing() -> tuple[int, set[str]]:
    """Settle delivered evidence runs and protect them from generic refunds."""
    settled = 0
    protected = set()
    for row in db.q("SELECT id FROM employee_learning_run ORDER BY id"):
        try:
            run = employeelearning.get_run(int(row["id"]))
            op_key = _learning_checkpoint_billing_op(run)
        except (employeelearning.LearningError, HTTPException, TypeError, ValueError):
            continue
        if not op_key:
            continue
        delivered = run.get("status") in {
            employeelearning.RUN_AWAITING_APPROVAL,
            employeelearning.RUN_ACTIVATED,
            employeelearning.RUN_REJECTED,
            employeelearning.RUN_STALE,
        } or (
            run.get("status") == employeelearning.RUN_EXPIRED
            and _learning_run_has_durable_proposal_delivery(run)
        )
        if delivered:
            protected.add(op_key)
            if billing.complete_operation(op_key):
                settled += 1
        elif run.get("status") == employeelearning.RUN_EXPIRED:
            # Expired queued/in-flight work has no delivered proposal and is
            # therefore refundable.  Settle both ledgers before the generic
            # billing recovery sees the operation.
            _settle_failed_learning_run_atomically(
                int(run["id"]),
                op_key,
                "未交付的过期进修自动退回",
                "EXPIRED_WITHOUT_DELIVERY",
            )
    return settled, protected


def _learning_frozen_request_binding(run: dict, body: dict) -> dict:
    """Validate only the browser echo against this run's frozen four-tuple."""
    if not isinstance(body, dict):
        raise HTTPException(400, "进修审批请求格式无效")
    expected = {
        "identity_ref": str(run.get("identity_ref") or ""),
        "config_revision": int(
            run.get("base_config_revision") or run.get("config_revision") or 0
        ),
        "config_sha256": str(run.get("base_config_sha256") or ""),
        "bundle_sha256": _learning_run_bundle_sha256(run),
    }
    if any(body.get(field) in (None, "") for field in _ROLE_WRITE_BINDING_FIELDS):
        raise HTTPException(400, "进修审批必须提交完整岗位四元绑定")
    if (
        str(body.get("identity_ref") or "").strip() != expected["identity_ref"]
        or type(body.get("config_revision")) is not int
        or int(body["config_revision"]) != expected["config_revision"]
        or str(body.get("config_sha256") or "").strip()
        != expected["config_sha256"]
        or str(body.get("bundle_sha256") or "").strip()
        != expected["bundle_sha256"]
    ):
        raise HTTPException(409, "进修提案绑定已变化，请刷新后重试")
    return expected


def _learning_current_run_binding(run: dict) -> dict:
    """Compare the frozen proposal with authoritative current server state."""
    expected = {
        "identity_ref": str(run.get("identity_ref") or ""),
        "config_revision": int(
            run.get("base_config_revision") or run.get("config_revision") or 0
        ),
        "config_sha256": str(run.get("base_config_sha256") or ""),
        "bundle_sha256": _learning_run_bundle_sha256(run),
    }
    try:
        binding = _employee_current_write_binding(
            _learning_run_idx(run), expected,
        )
    except HTTPException as exc:
        if exc.status_code in {404, 409}:
            raise HTTPException(
                409, "员工岗位或配置已变更，该进修提案已过期"
            ) from exc
        raise
    if not binding["identity"].get("can_learn"):
        raise HTTPException(409, "员工当前不可进修，该进修提案已过期")
    return binding


def _learning_request_binding(run: dict, body: dict) -> dict:
    """Validate browser echo first, then authoritative current server CAS."""
    _learning_frozen_request_binding(run, body)
    return _learning_current_run_binding(run)


def _learning_high_risk(employee: dict) -> bool:
    if str(employee.get("dept_key") or "") in {
        "auto", "beauty", "fitness", "pet", "pharmacy",
    }:
        return True
    decision = str(employee.get("primary_decision") or "")
    return bool(re.search(
        r"人身安全|食品安全|医疗|药品|处方|用药|隐私|金融|信贷|法律|许可",
        decision,
    ))


def _learning_evidence_config() -> learningevidence.EvidenceConfig:
    """Patchable boundary around the release-owned evidence sidecar loader."""
    return learningevidence.load_default_config()


def _learning_public_search_brief(employee: dict) -> str:
    """Send only catalog-declared public topics, never employee/private data."""
    public_industries = {
        "auto": "汽车后市场", "beauty": "美容美业", "convenience": "便利店",
        "fitness": "健身瑜伽", "grocery": "商超零售", "hotel": "酒店住宿",
        "pet": "宠物服务", "pharmacy": "零售药房", "restaurant": "餐饮",
        "snack": "量贩零食", "tea_coffee": "茶咖现制",
    }
    industry = public_industries.get(
        str(employee.get("dept_key") or ""), "线下零售与服务业",
    )
    raw_topics = employee.get("public_research_topics")
    topics = []
    if isinstance(raw_topics, list):
        for raw in raw_topics:
            topic = re.sub(r"\s+", " ", str(raw or "")).strip()
            if (
                2 <= len(topic) <= 120
                and not re.search(r"[\x00-\x1f\x7f]", topic)
                and topic not in topics
            ):
                topics.append(topic)
    is_v4 = (
        str(employee.get("catalog_version") or "")
        == departments.DECISION_V4_CATALOG_VERSION
    )
    if is_v4:
        person = str(employee.get("person") or "").strip()
        employee_idx = str(employee.get("idx") or "").strip()
        job_title = str(employee.get("name") or "").strip()
        forbidden_values = {
            str(employee.get(field) or "").strip()
            for field in (
                "identity_ref", "config_sha256", "bundle_sha256",
                "employee_spec_sha256", "spec_sha256",
            )
            if str(employee.get(field) or "").strip()
        }
        if (
            not 3 <= len(topics) <= 6
            or not job_title
            or job_title not in topics
            or any(person and person in topic for topic in topics)
            or any(employee_idx and employee_idx in topic for topic in topics)
            or any(secret in topic for secret in forbidden_values for topic in topics)
        ):
            raise employeelearning.LearningValidationError(
                "岗位公开研究主题未通过隐私与完整性校验"
            )
    topic_block = ""
    if topics:
        topic_block = "公开岗位研究主题：" + "；".join(topics) + "。"
    anchor_block = ""
    if topics:
        groups = _learning_role_anchor_groups(employee)
        compact_groups = []
        for group in groups:
            public_values = [
                *group["objects"], *group["methods"], group["topic"],
            ]
            if (
                any(person and person in value for value in public_values)
                or any(employee_idx and employee_idx in value for value in public_values)
                or any(
                    secret in value
                    for secret in forbidden_values for value in public_values
                )
            ):
                raise employeelearning.LearningValidationError(
                    "岗位公开研究锚点未通过隐私校验"
                )
            compact_groups.append(
                f"{group['topic']}（业务对象：{'、'.join(sorted(group['objects'])[:4])}；"
                f"专业方法：{'、'.join(sorted(group['methods'])[:3])}）"
            )
        anchor_block = "公开检索约束：" + "；".join(compact_groups) + "。"
    english_aliases = learningevidence.search_aliases(
        employee, config=_learning_evidence_config(), maximum=24,
    )
    alias_block = ""
    if english_aliases:
        alias_block = (
            "Bounded public English search aliases: "
            + "; ".join(english_aliases)
            + "."
        )
    return (
        f"公开行业类别：{industry}。{topic_block}{anchor_block}{alias_block}"
        "仅围绕以上公开主题检索近期可核验的官方规则、行业标准、专业协会方法、"
        "高质量研究与实务案例；覆盖知识更新、专业方法、异常识别和可复核工作流程。"
    )[:3000]


_LEARNING_GENERIC_TERMS = {
    "行业", "方法", "流程", "工作", "数据", "证据", "规则", "标准", "管理",
    "能力", "技能", "知识", "分析", "决策", "识别", "核验", "复核", "负责",
    "当前", "是否", "执行", "形成", "输出", "来源", "公开", "专业", "更新",
    "对象", "适用", "情况", "系统", "岗位", "员工", "人工", "业务", "服务",
}

# Reviewed public vocabulary for the ten V4 industries.  A source must match
# one of these anchors *and* one role topic.  This prevents a generic article
# about, for example, electricity demand forecasting from training a tea-shop
# demand employee merely because both pages say “需求预测”.
_LEARNING_INDUSTRY_ANCHORS = {
    "tea_coffee": {"茶咖", "茶饮", "咖啡", "现制饮品", "奶茶", "咖啡店"},
    "convenience": {"便利店", "便利零售", "即时零售", "便利门店"},
    "snack": {"零食", "休闲食品", "散装食品", "零食门店"},
    "grocery": {"商超", "超市", "生鲜零售", "大型卖场"},
    "pharmacy": {"药房", "药店", "药品零售", "零售药店"},
    "hotel": {"酒店", "住宿业", "旅馆", "客房"},
    "auto": {"汽车维修", "汽修", "机动车维修", "车辆维修", "新能源车"},
    "fitness": {"健身", "健身房", "健身场馆", "私教", "团课"},
    "beauty": {"美业", "美容", "美容院", "生活美容", "医疗美容"},
    "pet": {"宠物", "动物诊疗", "兽医", "犬猫", "宠物医院"},
}


def _learning_role_anchor_groups(employee: dict) -> list[dict]:
    """Load the explicit public object+method contract frozen in V4."""
    raw = employee.get("public_research_anchor_groups")
    if not isinstance(raw, list):
        raise employeelearning.LearningValidationError("岗位缺少公开研究锚点组")
    groups = []
    employee_idx = str(employee.get("idx") or "").strip()
    person = str(
        employee.get("person_snapshot", employee.get("person")) or ""
    ).strip()
    for group in raw:
        if not isinstance(group, dict):
            raise employeelearning.LearningValidationError("岗位公开研究锚点组无效")
        topic = str(group.get("topic") or "").strip()
        raw_objects = group.get("object_anchors")
        raw_methods = group.get("method_anchors")
        if (
            not isinstance(raw_objects, list)
            or not isinstance(raw_methods, list)
            or not all(isinstance(value, str) for value in raw_objects)
            or not all(isinstance(value, str) for value in raw_methods)
        ):
            raise employeelearning.LearningValidationError("岗位公开研究锚点组无效")
        objects = {
            str(value).strip().lower()
            for value in raw_objects
            if str(value).strip()
        }
        methods = {
            str(value).strip().lower()
            for value in raw_methods
            if str(value).strip()
        }
        if not topic or not objects or not methods:
            raise employeelearning.LearningValidationError("岗位公开研究锚点组不完整")
        if any(
            (person and person in value)
            or (employee_idx and employee_idx in value)
            or re.search(
                r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", value,
            ) is not None
            for value in [*objects, *methods]
        ):
            raise employeelearning.LearningValidationError("岗位公开研究锚点包含身份字段")
        if any(
            obj in method or method in obj
            for obj in objects for method in methods
        ):
            raise employeelearning.LearningValidationError("岗位公开研究锚点组相互重叠")
        groups.append({"topic": topic, "objects": objects, "methods": methods})
    if len(groups) < 2:
        raise employeelearning.LearningValidationError("岗位公开研究锚点组不足")
    return groups


def _learning_semantic_haystack(source: dict) -> str:
    """Normalize a small reviewed set of industry-method synonyms.

    This is intentionally bounded and deterministic; it improves recall for
    common professional paraphrases without asking a model to decide whether
    arbitrary page text is relevant.
    """
    text = " ".join(str(source.get(key) or "") for key in (
        "title", "publisher", "excerpt",
    )).lower()
    replacements = {
        # Public wording -> frozen business-object anchors.  These are
        # reviewed phrase mappings, not arbitrary substrings or model output.
        "每十五分钟订单需求": "pos订单到达",
        "十五分钟订单需求": "pos订单到达",
        "每十五分钟订单量": "pos订单到达",
        "十五分钟订单量": "pos订单到达",
        "十五分钟订单": "pos订单到达",
        "货架布局方案": "总部陈列图版本",
        "替换商品": "缺货替代临时陈列",
        "新版布局落店": "确认改版落地",
        "通道遮挡安全": "货架安全可视规则",
        "经理批准": "例外审批",
        "平均绝对百分比误差": "mape",
        "平均绝对百分误差": "mape",
        "分位回归": "分位数",
        "分位点": "分位数",
        "预测区间": "置信区间",
        "样本外偏移": "样本外漂移",
        "每15分钟": "十五分钟",
        "每十五分钟": "十五分钟",
        "实际客房": "pms物理房号",
        "线上售卖房类": "虚拟房型",
        "建立对应关系": "映射",
        "对应关系": "映射",
        "建立映射": "映射",
        "升等路径": "升级链",
        "连续入住订单": "连住订单",
        "连续入住": "连住",
        "中断裂": "截断",
        "关键路径法识别": "关键路径分析",
    }
    for source_value, target_value in replacements.items():
        text = text.replace(source_value, target_value)
    return text


def _learning_relevant_sources(employee: dict, sources: list[dict]) -> list[dict]:
    """Keep only evidence with role-specific lexical support.

    Reachability and authority are necessary but not sufficient: unrelated
    public pages must never be converted into a role capability merely because
    they were fetched successfully.
    """
    prepared = []
    for source in sources or []:
        row = dict(source)
        row["semantic_text"] = _learning_semantic_haystack(row)
        prepared.append(row)
    graph = learningevidence.evaluate_evidence(
        employee,
        prepared,
        config=_learning_evidence_config(),
        high_risk=_learning_high_risk(employee),
    )
    return list(graph["sources"])


def _learning_semantic_source_gate(employee: dict, run: dict) -> list[dict]:
    sources = [dict(row) for row in db.q(
        "SELECT id,canonical_url AS url,title,publisher,source_level AS authority_level,"
        "published_at,fetched_at,http_status,"
        "CASE WHEN certificate_status='valid' THEN 1 ELSE 0 END AS tls_valid,"
        "content_sha256,excerpt,"
        "json_extract(metadata_json,'$.capture_event_id') AS capture_event_id,"
        "json_extract(metadata_json,'$.capture_provider') AS capture_provider "
        "FROM employee_learning_source WHERE run_id=? ORDER BY id",
        (int(run["id"]),),
    )]
    return _learning_relevant_sources(employee, sources)


def _learning_verify_frozen_artifact_evidence(
    run: dict, relevant_sources: list[dict],
) -> None:
    """Bind every proposed delta to recomputed catalog topics and source hashes."""
    by_index = {index + 1: row for index, row in enumerate(relevant_sources)}
    rows = db.q(
        "SELECT kind,statement,payload_json,source_ids_json "
        "FROM employee_learning_artifact WHERE run_id=? ORDER BY id",
        (int(run["id"]),),
    )
    if {str(row.get("kind") or "") for row in rows} != set(
        _LEARNING_REQUIRED_ARTIFACT_KINDS
    ):
        raise employeelearning.LearningValidationError(
            "进修产物缺少完整能力维度"
        )
    for row in rows:
        payload = db.jloads(row.get("payload_json"), {})
        refs = sorted({int(value) for value in db.jloads(
            row.get("source_ids_json"), [],
        )})
        evidence = payload.get("evidence_sources") if isinstance(payload, dict) else None
        declared_topics = payload.get("evidence_topics") if isinstance(payload, dict) else None
        if not isinstance(evidence, list) or not isinstance(declared_topics, list):
            raise employeelearning.LearningValidationError(
                "进修产物缺少冻结证据主题"
            )
        seen_ids: set[int] = set()
        source_indexes: set[int] = set()
        verified_topics: set[str] = set()
        for item in evidence:
            if not isinstance(item, dict) or type(item.get("source_index")) is not int:
                raise employeelearning.LearningValidationError("进修证据索引无效")
            source = by_index.get(int(item["source_index"]))
            if not source or int(source.get("id") or 0) not in refs:
                raise employeelearning.LearningValidationError("进修证据与来源账本断链")
            if str(item.get("content_sha256") or "") != str(
                source.get("content_sha256") or ""
            ):
                raise employeelearning.LearningValidationError("进修证据内容摘要漂移")
            source_topics = {str(value) for value in source.get("semantic_topics") or []}
            item_topics = {str(value) for value in item.get("semantic_topics") or []}
            if not item_topics or not item_topics <= source_topics:
                raise employeelearning.LearningValidationError("进修证据主题无法回链")
            seen_ids.add(int(source["id"]))
            source_indexes.add(int(item["source_index"]))
            verified_topics.update(item_topics)
        if seen_ids != set(refs) or verified_topics != {
            str(value) for value in declared_topics
        }:
            raise employeelearning.LearningValidationError("进修证据集与产物声明不一致")
        artifact_topic = learningevidence.validate_artifact_evidence(
            source_indexes, relevant_sources,
        )
        if artifact_topic not in verified_topics:
            raise learningevidence.EvidenceGateError(
                "EVIDENCE_ARTIFACT_TOPIC_MISMATCH",
                "进修产物未引用同专题的直接与互补证据",
            )
        statement = str(row.get("statement") or "")
        if not any(topic in statement for topic in verified_topics):
            raise employeelearning.LearningValidationError("进修产物未体现来源支持的专题")


def _normalize_learning_artifacts(raw, source_count: int) -> list[dict]:
    payload = raw.get("data") if isinstance(raw, dict) else None
    rows = payload.get("artifacts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise employeelearning.LearningValidationError("进修模型未交付产物数组")
    by_kind = {}
    statements = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in _LEARNING_REQUIRED_ARTIFACT_KINDS or kind in by_kind:
            continue
        title = str(item.get("title") or "").strip()[:300]
        statement = str(item.get("statement") or "").strip()[:4000]
        indexes = item.get("source_indexes")
        if not title or not statement or statement in statements:
            continue
        if not isinstance(indexes, list):
            continue
        try:
            indexes = sorted({int(value) for value in indexes})
        except (TypeError, ValueError):
            continue
        if len(indexes) < 2 or any(value < 1 or value > source_count for value in indexes):
            continue
        artifact_payload = item.get("payload")
        if not isinstance(artifact_payload, dict):
            artifact_payload = {}
        if kind == "workflow" and not str(artifact_payload.get("step") or "").strip():
            artifact_payload = {**artifact_payload, "step": statement}
        by_kind[kind] = {
            "kind": kind,
            "title": title,
            "statement": statement,
            "payload": artifact_payload,
            "source_indexes": indexes,
        }
        statements.add(statement)
    missing = [kind for kind in _LEARNING_REQUIRED_ARTIFACT_KINDS if kind not in by_kind]
    if missing:
        raise employeelearning.LearningValidationError(
            "进修结果未同时更新知识、技能、能力和工作流程"
        )
    return [by_kind[kind] for kind in _LEARNING_REQUIRED_ARTIFACT_KINDS]


def _evidence_backed_learning_artifacts(employee: dict, sources: list[dict]) -> list[dict]:
    """Project verified public evidence locally; no employee data leaves here."""
    if len(sources) < 5:
        raise employeelearning.LearningValidationError("公开证据不足，不能形成进修提案")

    public_topics = [
        str(value).strip() for value in employee.get("public_research_topics") or []
        if str(value).strip()
    ]
    if public_topics and any(not source.get("semantic_topics") for source in sources):
        sources = _learning_relevant_sources(employee, sources)

    def label(index: int) -> str:
        source = sources[index - 1]
        if not re.sub(r"\s+", " ", str(source.get("excerpt") or "")).strip():
            raise employeelearning.LearningValidationError("公开证据缺少可核验摘要")
        # Untrusted page title/body never becomes an executable role prompt;
        # reviewers inspect it through the separate immutable source ledger.
        return f"来源{index}"

    def evidence_payload(
        indexes: list[int], required_topic: str | None = None,
    ) -> tuple[list[str], list[dict]]:
        rows = []
        topics: set[str] = set()
        for index in indexes:
            source = sources[index - 1]
            semantic_topics = [
                str(value).strip() for value in source.get("semantic_topics") or []
                if str(value).strip() in public_topics
                and (required_topic is None or str(value).strip() == required_topic)
            ]
            if public_topics and not semantic_topics:
                raise employeelearning.LearningValidationError(
                    "公开证据未命中岗位专属研究主题"
                )
            if not semantic_topics:
                semantic_topics = [str(
                    (employee.get("decision_contract") or {}).get("decision")
                    or employee.get("name") or "岗位专业更新"
                ).strip()[:160]]
            digest = str(source.get("content_sha256") or "").strip().lower()
            if public_topics and not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise employeelearning.LearningValidationError("公开证据内容摘要无效")
            rows.append({
                "source_index": index,
                "content_sha256": digest,
                "semantic_topics": sorted(set(semantic_topics)),
            })
            topics.update(semantic_topics)
        return sorted(topics), rows

    job = str(employee.get("name") or "行业专属岗位").strip()[:80]
    contract = employee.get("decision_contract")
    contract = contract if isinstance(contract, dict) else {}
    decision = str(
        contract.get("decision") or employee.get("primary_decision") or job
    ).strip()[:260]
    workflow = [
        str(value).strip()[:180]
        for value in (
            employee.get("workflow") or contract.get("workflow") or []
        )
        if str(value).strip()
    ]
    profile = employee.get("professional_profile")
    profile = profile if isinstance(profile, dict) else {}
    tracks = [
        str(value).strip()[:120]
        for value in profile.get("learning_tracks") or []
        if str(value).strip()
    ]
    capabilities = [
        str(value).strip()[:120]
        for value in profile.get("capabilities") or []
        if str(value).strip()
    ]
    outputs = [
        str(value).strip()[:120]
        for value in contract.get("outputs") or employee.get("outputs") or []
        if str(value).strip()
    ]
    track = tracks[0] if tracks else decision
    capability = capabilities[0] if capabilities else f"识别{decision}的证据缺口"
    baseline_step = workflow[0] if workflow else f"核对{decision}的必需输入"
    next_step = workflow[1] if len(workflow) > 1 else baseline_step
    output = outputs[0] if outputs else "岗位决策证据包"
    if public_topics:
        eligible_pairs: list[tuple[list[int], str]] = []
        for direct_index, source in enumerate(sources, start=1):
            for topic_name, topic_gate in (
                source.get("evidence_topics") or {}
            ).items():
                if not topic_gate.get("direct"):
                    continue
                for complement_index, complement in enumerate(sources, start=1):
                    if complement_index == direct_index:
                        continue
                    complement_gate = (
                        complement.get("evidence_topics") or {}
                    ).get(topic_name, {})
                    if complement_gate.get("application") or complement_gate.get("method"):
                        pair = ([direct_index, complement_index], str(topic_name))
                        if pair not in eligible_pairs:
                            eligible_pairs.append(pair)
        if not eligible_pairs:
            raise learningevidence.EvidenceGateError(
                "EVIDENCE_ARTIFACT_COMPLEMENT_MISSING",
                "进修证据没有可供产物引用的同专题直接与互补来源",
            )
        selected_pairs = [
            eligible_pairs[index % len(eligible_pairs)] for index in range(4)
        ]
        for indexes, _topic in selected_pairs:
            learningevidence.validate_artifact_evidence(indexes, sources)
    else:
        # Compatibility for non-V4 pure projection callers; all real Schema55
        # runs take the strict public-topic branch above.
        selected_pairs = [
            ([1, 2], ""), ([2, 3], ""), ([3, 4], ""), ([4, 5], ""),
        ]
    evidence_rows = [
        evidence_payload(indexes, topic or None)
        for indexes, topic in selected_pairs
    ]
    pair_indexes = [pair[0] for pair in selected_pairs]
    rows = (
        (
            "knowledge", f"{track}规则知识更新",
            f"来源支持的岗位专题“{' / '.join(evidence_rows[0][0])}”更新了"
            f"“{decision}”的“{track}”知识基线；"
            f"{label(pair_indexes[0][0])}与{label(pair_indexes[0][1])}"
            "适用版本和证据冲突须在人工审批时逐项核对。",
            {"decision_scope": decision, "learning_track": track,
             "evidence_topics": evidence_rows[0][0],
             "evidence_sources": evidence_rows[0][1]}, pair_indexes[0],
        ),
        (
            "skill", f"{baseline_step}证据校准技能",
            f"将“{' / '.join(evidence_rows[1][0])}”转化为岗位校准技能：执行"
            f"“{baseline_step}”时，须把{label(pair_indexes[1][0])}与"
            f"{label(pair_indexes[1][1])}按适用对象、"
            f"发布日期和证据强度交叉校准，再进入“{next_step}”。",
            {"method": "岗位步骤内多来源交叉校准", "baseline_step": baseline_step,
             "evidence_topics": evidence_rows[1][0],
             "evidence_sources": evidence_rows[1][1]}, pair_indexes[1],
        ),
        (
            "capability", f"{capability}增强能力",
            f"基于“{' / '.join(evidence_rows[2][0])}”为“{decision}”增强"
            f"“{capability}”：用{label(pair_indexes[2][0])}和"
            f"{label(pair_indexes[2][1])}识别"
            f"规则变化、适用边界与来源冲突；冲突未消解时不得直接生成“{output}”。",
            {"decision_scope": decision, "output_boundary": output,
             "evidence_topics": evidence_rows[2][0],
             "evidence_sources": evidence_rows[2][1]}, pair_indexes[2],
        ),
        (
            "workflow", f"{job}证据门禁流程增量",
            f"将“{' / '.join(evidence_rows[3][0])}”落到流程：在“{baseline_step}”与"
            f"“{next_step}”之间增加岗位专属证据门禁："
            f"核对{label(pair_indexes[3][0])}和{label(pair_indexes[3][1])}后，"
            f"才可形成“{output}”的待审版本。",
            {"step": (
                f"完成“{baseline_step}”后，核验来源版本、适用对象和冲突项；"
                f"证据满足再进入“{next_step}”，否则暂停“{output}”并提交人工复核"
            ), "evidence_topics": evidence_rows[3][0],
                "evidence_sources": evidence_rows[3][1]}, pair_indexes[3],
        ),
    )
    return [
        {
            "kind": kind, "title": title, "statement": statement,
            "payload": payload, "source_indexes": indexes,
        }
        for kind, title, statement, payload, indexes in rows
    ]


def _learning_gate_checkpoint(run_id: int, employee: dict) -> str:
    """Freeze the exact release gate before any provider evidence is accepted."""
    config = _learning_evidence_config()
    # Binding the role here also detects a catalog/sidecar mismatch before a
    # network result can become an approvable proposal.
    learningevidence.search_aliases(employee, config=config, maximum=24)
    run = employeelearning.get_run(int(run_id))
    checkpoint = _learning_run_checkpoint(run)
    frozen = str(checkpoint.get("evidence_gate_digest") or "")
    if frozen and frozen != config.digest:
        raise learningevidence.EvidenceGateError(
            "EVIDENCE_GATE_DIGEST_DRIFT",
            "进修运行冻结的证据门禁版本已漂移",
        )
    checkpoint["evidence_gate_digest"] = config.digest
    employeelearning.checkpoint(int(run_id), checkpoint)
    return config.digest


def _verify_learning_gate_checkpoint(employee: dict, run: dict) -> str:
    config = _learning_evidence_config()
    learningevidence.search_aliases(employee, config=config, maximum=24)
    frozen = str(
        _learning_run_checkpoint(run).get("evidence_gate_digest") or ""
    ).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", frozen) or frozen != config.digest:
        raise learningevidence.EvidenceGateError(
            "EVIDENCE_GATE_DIGEST_DRIFT",
            "审批使用的证据门禁版本与研究冻结版本不一致",
        )
    return frozen


def _record_learning_evidence_gate_outcome(
    run_id: int, error_code: str | None,
) -> dict:
    """Persist a batch-wide consecutive-zero-direct circuit breaker."""
    with db.atomic():
        run = employeelearning.get_run(int(run_id))
        batch = employeelearning.get_batch(int(run["batch_id"]))
        checkpoint = _learning_batch_json(batch.get("checkpoint_json"), {})
        if error_code == "EVIDENCE_ZERO_DIRECT":
            streak = int(checkpoint.get("evidence_zero_direct_streak") or 0) + 1
        elif error_code is not None or str(run.get("status") or "") in {
            employeelearning.RUN_AWAITING_APPROVAL,
            employeelearning.RUN_ACTIVATED,
        }:
            streak = 0
        else:
            return batch
        checkpoint["evidence_zero_direct_streak"] = min(streak, 3)
        checkpoint["last_evidence_gate_error"] = error_code
        now = time.time()
        db.execute(
            "UPDATE employee_learning_batch SET checkpoint_json=?,updated_at=? "
            "WHERE id=?",
            (
                json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                now,
                int(batch["id"]),
            ),
        )
        queued = int((db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_run "
            "WHERE batch_id=? AND status=?",
            (int(batch["id"]), employeelearning.RUN_QUEUED),
        ) or {}).get("n") or 0)
        if streak >= 3 and queued > 0 and str(batch.get("status") or "") in {
            employeelearning.BATCH_QUEUED,
            employeelearning.BATCH_RUNNING,
        }:
            checkpoint["coordinator_generation"] = (
                _learning_batch_coordinator_generation(batch) + 1
            )
            db.execute(
                "UPDATE employee_learning_batch SET status=?,paused_reason=?,"
                "checkpoint_json=?,updated_at=? WHERE id=? AND status IN (?,?)",
                (
                    employeelearning.BATCH_PAUSED,
                    "连续3个岗位未检出同专题直接证据，已自动暂停",
                    json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                    now,
                    int(batch["id"]),
                    employeelearning.BATCH_QUEUED,
                    employeelearning.BATCH_RUNNING,
                ),
            )
        return employeelearning.get_batch(int(batch["id"]))


async def _employee_learning_research_worker(run_id: int, binding: dict) -> None:
    """Capture real web evidence and leave an inert four-part proposal."""
    employee = dict(binding["employee"])
    try:
        await _run_db_safely(_learning_gate_checkpoint, run_id, employee)
        engine.broadcast({
            "type": "employee_learning_update", "run_id": run_id,
            "employee_idx": int(employee["idx"]), "status": "researching",
        })
        evidence = await providers.call_verified_learning_research(
            _learning_public_search_brief(employee),
            timeout=600,
            token=f"employee-learning:{run_id}",
            min_queries=3,
            max_sources=12,
        )
        sources = evidence.get("sources") if isinstance(evidence, dict) else None
        if not isinstance(sources, list):
            sources = []
        sources = _learning_relevant_sources(employee, sources)
        # The role-specific projection happens locally.  Public page excerpts
        # remain untrusted bounded strings and can only be cited by index; they
        # are never interpreted as instructions and cannot add a URL/source.
        artifacts = _evidence_backed_learning_artifacts(employee, sources)
        await _run_db_safely(
            employeelearning.research_run,
            run_id,
            lambda _context: {"sources": sources, "artifacts": artifacts},
        )
        await _run_db_safely(
            _record_learning_evidence_gate_outcome, run_id, None,
        )
        try:
            await _run_db_safely(_auto_activate_delivered_learning_run, int(run_id))
        except BaseException as activate_exc:
            # The immutable proposal is already durable.  A later operator
            # retry can still use the explicit approve endpoint.
            logging.getLogger("employeelearning").error(
                "employee learning auto-activate failed run_id=%s error_type=%s",
                run_id,
                type(activate_exc).__name__,
            )
    except BaseException as exc:
        # research_run owns the typed terminal transition and never persists
        # raw provider errors or page content in an error field.
        def fail_research(_context, error=exc):
            raise error

        try:
            current = await _run_db_safely(employeelearning.get_run, run_id)
            if current.get("status") == employeelearning.RUN_RESEARCHING:
                await _run_db_safely(
                    employeelearning.research_run, run_id, fail_research,
                )
        except BaseException:
            pass
        if isinstance(exc, learningevidence.EvidenceGateError):
            try:
                await _run_db_safely(
                    _record_learning_evidence_gate_outcome,
                    run_id,
                    exc.code,
                )
            except BaseException:
                pass
        # ``research_run`` intentionally catches ordinary provider failures,
        # but asyncio cancellation is a BaseException.  If it could not write
        # a terminal state, force one through the cancellation-safe DB drain
        # so the coordinator and immutable identity owner cannot hang forever.
        try:
            current = await _run_db_safely(employeelearning.get_run, run_id)
            if current.get("status") == employeelearning.RUN_RESEARCHING:
                await _terminalize_learning_run_safely(
                    run_id,
                    (
                        "RESEARCH_CANCELLED"
                        if isinstance(exc, asyncio.CancelledError)
                        else "RESEARCH_FAILED"
                    ),
                )
        except BaseException:
            pass
        logging.getLogger("employeelearning").error(
            "employee learning research failed run_id=%s error_type=%s",
            run_id, type(exc).__name__,
        )
    finally:
        try:
            current = await _run_db_safely(employeelearning.get_run, run_id)
            status = str(current.get("status") or "failed")
        except BaseException:
            current = {}
            status = "failed"
        billing_op = _learning_checkpoint_billing_op(current)
        if billing_op:
            try:
                if status in {
                    employeelearning.RUN_AWAITING_APPROVAL,
                    employeelearning.RUN_ACTIVATED,
                    employeelearning.RUN_REJECTED,
                    employeelearning.RUN_STALE,
                }:
                    await _run_db_safely(billing.complete_operation, billing_op)
                else:
                    await _settle_failed_learning_run_safely(
                        run_id,
                        billing_op,
                        "员工证据进修失败自动退回",
                        "RESEARCH_FAILED",
                    )
            except BaseException as settle_exc:
                logging.getLogger("employeelearning").error(
                    "employee learning settlement failed run_id=%s error_type=%s",
                    run_id, type(settle_exc).__name__,
                )
        try:
            engine.broadcast({
                "type": "employee_learning_update", "run_id": run_id,
                "employee_idx": int(employee["idx"]), "status": status,
            })
        except BaseException as notify_exc:
            # Notification is observability only.  A completed immutable
            # proposal and its succeeded billing operation must never be
            # reclassified/refunded because a websocket listener failed.
            logging.getLogger("employeelearning").warning(
                "employee learning update broadcast failed run_id=%s "
                "error_type=%s",
                run_id,
                type(notify_exc).__name__,
            )


def _learning_run_public(run: dict) -> dict:
    run_id = int(run["id"])
    checkpoint = _learning_run_checkpoint(run)
    gate_digest = str(checkpoint.get("evidence_gate_digest") or "").lower()
    gate_status = "missing"
    if re.fullmatch(r"[0-9a-f]{64}", gate_digest):
        try:
            gate_status = (
                "verified"
                if gate_digest == _learning_evidence_config().digest
                else "drift"
            )
        except employeelearning.LearningValidationError:
            gate_status = "drift"
    sources = []
    for row in db.q(
        "SELECT * FROM employee_learning_source WHERE run_id=? ORDER BY id",
        (run_id,),
    ):
        sources.append({
            "id": int(row["id"]),
            "url": str(row.get("canonical_url") or row.get("url") or ""),
            "source_url": str(row.get("canonical_url") or row.get("url") or ""),
            "canonical_url": str(row.get("canonical_url") or row.get("url") or ""),
            "title": str(row.get("title") or ""),
            "source_title": str(row.get("title") or ""),
            "publisher": str(row.get("publisher") or ""),
            "authority_level": str(
                row.get("authority_level") or row.get("source_level") or ""
            ),
            "published_at": row.get("published_at"),
            "fetched_at": row.get("fetched_at"),
            "retrieved_at": row.get("fetched_at"),
            "content_sha256": str(row.get("content_sha256") or ""),
            "excerpt": str(row.get("excerpt") or ""),
        })
    artifacts = []
    for row in db.q(
        "SELECT * FROM employee_learning_artifact WHERE run_id=? ORDER BY id",
        (run_id,),
    ):
        artifacts.append({
            "id": int(row["id"]),
            "kind": str(row.get("kind") or row.get("artifact_type") or ""),
            "title": str(row.get("title") or row.get("claim_text") or ""),
            "statement": str(row.get("statement") or row.get("claim_text") or ""),
            "payload": db.jloads(
                row.get("payload_json") or row.get("delta_json"), {}
            ),
            "source_ids": db.jloads(row.get("source_ids_json"), []),
            "status": str(row.get("status") or "proposed"),
            "reviewer_id": (
                int(row["reviewer_id"])
                if row.get("reviewer_id") is not None else None
            ),
            "reviewed_at": row.get("reviewed_at"),
        })
    proposal = run.get("proposal_json")
    if isinstance(proposal, str):
        proposal = db.jloads(proposal, {})
    return {
        "id": run_id,
        "batch_id": int(run["batch_id"]),
        "employee_idx": _learning_run_idx(run),
        "identity_ref": str(run.get("identity_ref") or ""),
        "base_config_revision": int(
            run.get("base_config_revision") or run.get("config_revision") or 0
        ),
        "base_config_sha256": str(run.get("base_config_sha256") or ""),
        "bundle_sha256": _learning_run_bundle_sha256(run),
        "status": str(run.get("status") or ""),
        "high_risk": bool(run.get("high_risk")),
        "budget_points": float(run.get("budget_points") or 0),
        "spent_points": float(run.get("spent_points") or 0),
        "error_code": run.get("error_code"),
        "evidence_gate_status": gate_status,
        "evidence_gate_digest_prefix": (
            gate_digest[:12]
            if re.fullmatch(r"[0-9a-f]{64}", gate_digest) else None
        ),
        "reviewer_id": (
            int(run["reviewer_id"])
            if run.get("reviewer_id") is not None else None
        ),
        "reviewed_at": run.get("reviewed_at"),
        "proposal": proposal if isinstance(proposal, dict) else {},
        "sources": sources,
        "artifacts": artifacts,
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def _employee_learning_history(identity_ref: str, *, limit: int = 5) -> dict:
    """Return a bounded, tenant-scoped ledger for one immutable role."""
    role_ref = str(identity_ref or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", role_ref) is None:
        return {"runs": [], "activated": 0, "researching": False}
    rows = db.q(
        "SELECT r.id FROM employee_learning_run r "
        "JOIN employee_learning_batch b ON b.id=r.batch_id "
        "WHERE b.tenant_id=? AND r.identity_ref=? "
        "ORDER BY r.id DESC LIMIT ?",
        (TEN(), role_ref, max(1, min(int(limit), 5))),
    )
    runs = []
    for row in rows:
        try:
            runs.append(_learning_run_public(
                employeelearning.get_run(int(row["id"])),
            ))
        except (employeelearning.LearningError, HTTPException, ValueError, TypeError):
            # An incomplete pre-schema55 record is not an authorizable
            # proposal and must not be repaired from today's current config.
            continue
    activated = int((db.one(
        "SELECT COUNT(*) AS n FROM employee_learning_run r "
        "JOIN employee_learning_batch b ON b.id=r.batch_id "
        "WHERE b.tenant_id=? AND r.identity_ref=? AND r.status='activated'",
        (TEN(), role_ref),
    ) or {}).get("n") or 0)
    researching = any(
        run.get("status") in {
            employeelearning.RUN_QUEUED, employeelearning.RUN_RESEARCHING,
        }
        for run in runs
    )
    return {"runs": runs, "activated": activated, "researching": researching}


_LEARNING_BATCH_POINTS_PER_EMPLOYEE = 3.0
_LEARNING_BATCH_MAX_TARGETS = 360
_LEARNING_BATCH_COORDINATORS: dict[int, asyncio.Task] = {}
_LEARNING_BATCH_ACTIVE_RUNS: set[int] = set()
_LEARNING_BATCH_TERMINAL_RUNS = {
    employeelearning.RUN_ACTIVATED,
    employeelearning.RUN_REJECTED,
    employeelearning.RUN_STALE,
    employeelearning.RUN_EXPIRED,
    employeelearning.RUN_CANCELLED,
    employeelearning.RUN_FAILED,
    employeelearning.RUN_EVIDENCE_INSUFFICIENT,
}


def _learning_batch_json(value, default):
    if isinstance(value, dict):
        return dict(value)
    parsed = db.jloads(value, default)
    return dict(parsed) if isinstance(parsed, dict) else dict(default)


def _learning_batch_metadata(batch: dict) -> dict:
    return _learning_batch_json(batch.get("metadata_json"), {})


def _learning_batch_v4_employees() -> list[dict]:
    rows = []
    for department in departments.list_depts():
        for raw in department.get("employees") or []:
            employee = {
                **raw,
                "dept_key": str(raw.get("dept_key") or department.get("key") or ""),
                "dept_name": str(raw.get("dept_name") or department.get("name") or ""),
            }
            if (
                str(employee.get("catalog_version") or "")
                == departments.DECISION_V4_CATALOG_VERSION
            ):
                rows.append(employee)
    rows.sort(key=lambda item: int(item["idx"]))
    return rows


def _learning_batch_request_key(body: dict) -> str:
    value = str(body.get("request_key") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,190}", value) is None:
        raise HTTPException(400, "批量进修请求幂等键无效")
    return value


def _learning_batch_selection(body: dict) -> tuple[list[dict], dict]:
    if not isinstance(body, dict):
        raise HTTPException(400, "批量进修请求格式无效")
    all_v4 = _learning_batch_v4_employees()
    by_idx = {int(employee["idx"]): employee for employee in all_v4}
    raw_idxs = body.get("idxs")
    has_explicit_idxs = "idxs" in body
    industry_key = str(body.get("industry_key") or "").strip()
    if has_explicit_idxs and industry_key:
        raise HTTPException(400, "行业范围与员工编号范围只能选择一种")
    if has_explicit_idxs:
        if not isinstance(raw_idxs, list) or not raw_idxs:
            raise HTTPException(400, "idxs 必须是非空员工编号数组")
        if len(raw_idxs) > _LEARNING_BATCH_MAX_TARGETS:
            raise HTTPException(400, "单批最多进修 360 名员工")
        idxs = []
        for value in raw_idxs:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise HTTPException(400, "idxs 含无效员工编号")
            if value not in idxs:
                idxs.append(value)
        missing = [value for value in idxs if value not in by_idx]
        if missing:
            raise HTTPException(409, "批量进修仅适用于当前 V4 行业专属员工")
        selected = [by_idx[value] for value in sorted(idxs)]
        scope = {"mode": "employees", "idxs": sorted(idxs), "industry_key": None}
    elif industry_key:
        if re.fullmatch(r"[a-z][a-z0-9_]{1,39}", industry_key) is None:
            raise HTTPException(400, "行业范围无效")
        selected = [
            employee for employee in all_v4
            if str(employee.get("dept_key") or "") == industry_key
        ]
        if not selected:
            raise HTTPException(404, "当前 V4 员工目录中没有这个行业")
        scope = {"mode": "industry", "idxs": [], "industry_key": industry_key}
    else:
        selected = all_v4
        if len(selected) != _LEARNING_BATCH_MAX_TARGETS:
            raise HTTPException(503, "当前 V4 360 人目录不完整，不能创建全员进修批次")
        scope = {"mode": "all_v4", "idxs": [], "industry_key": None}
    return selected, scope


def _learning_batch_preview_contract(body: dict, *, tenant_id: int) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(400, "批量进修请求格式无效")
    request_key = _learning_batch_request_key(body)
    try:
        configured_points = float(
            (billing.prices().get("learn") or {}).get("points")
        )
    except (TypeError, ValueError):
        configured_points = -1
    if (
        not math.isfinite(configured_points)
        or abs(configured_points - _LEARNING_BATCH_POINTS_PER_EMPLOYEE) > 1e-9
    ):
        raise HTTPException(503, "进修计费单价不是每人 3 点，批次已安全拦截")
    employees_selected, scope = _learning_batch_selection(body)
    raw_concurrency = body.get("max_concurrency", 2)
    if (
        isinstance(raw_concurrency, bool)
        or not isinstance(raw_concurrency, int)
        or not 1 <= raw_concurrency <= 8
    ):
        raise HTTPException(400, "最大并发必须是 1 到 8 的整数")
    target_rows = []
    industry_counts: dict[str, int] = {}
    for employee in employees_selected:
        config = employees.get_config(int(employee["idx"]))
        identity = _employee_public_contract(employee, config=config)
        if not identity.get("can_learn"):
            raise HTTPException(
                409,
                f"员工 {int(employee['idx'])} 当前不可进修，请先恢复在岗状态",
            )
        frozen = {
            "idx": int(employee["idx"]),
            "person": str(employee.get("person") or ""),
            "name": str(employee.get("name") or ""),
            "industry_key": str(employee.get("dept_key") or ""),
            "high_risk": _learning_high_risk(employee),
            "identity_ref": str(config.get("identity_ref") or ""),
            "config_revision": int(config.get("config_revision") or 0),
            "config_sha256": str(config.get("config_sha256") or ""),
            "bundle_sha256": str(config.get("bundle_sha256") or ""),
        }
        if (
            frozen["config_revision"] < 1
            or any(
                re.fullmatch(r"[0-9a-f]{64}", frozen[field]) is None
                for field in ("identity_ref", "config_sha256", "bundle_sha256")
            )
        ):
            raise HTTPException(409, "员工岗位四元组不完整，不能进入批量进修")
        target_rows.append(frozen)
        industry_counts[frozen["industry_key"]] = (
            industry_counts.get(frozen["industry_key"], 0) + 1
        )
    target_count = len(target_rows)
    required_budget = target_count * _LEARNING_BATCH_POINTS_PER_EMPLOYEE
    # Tenant 1 is the platform headquarters and billing.start_operation has
    # always treated it as plan-included. Keep the research-unit cap explicit
    # while reporting the actual wallet debit truthfully to the boss.
    billing_mode = "platform_included" if int(tenant_id) == 1 else "tenant_points"
    wallet_charge_points = 0.0 if billing_mode == "platform_included" else required_budget
    raw_budget = body.get("budget_cap_points", required_budget)
    if isinstance(raw_budget, bool):
        raise HTTPException(400, "批量进修预算上限无效")
    try:
        budget_cap = float(raw_budget)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "批量进修预算上限无效") from exc
    if (
        not math.isfinite(budget_cap)
        or abs(budget_cap - required_budget) > 1e-9
    ):
        raise HTTPException(
            400,
            f"批量进修预算必须严格等于每人 3 点，共 {required_budget:g} 点",
        )
    target_digest = hashlib.sha256(json.dumps(
        [
            {
                key: row[key]
                for key in (
                    "idx", "industry_key", "identity_ref", "config_revision",
                    "config_sha256", "bundle_sha256", "high_risk",
                )
            }
            for row in target_rows
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    token_payload = {
        "schema": "schema55-learning-batch-preview-v1",
        "tenant_id": int(tenant_id),
        "request_key": request_key,
        "scope": scope,
        "target_count": target_count,
        "target_digest": target_digest,
        "budget_cap_points": required_budget,
        "wallet_charge_points": wallet_charge_points,
        "billing_mode": billing_mode,
        "max_concurrency": raw_concurrency,
        "auto_approve": False,
    }
    preview_token = hashlib.sha256(json.dumps(
        token_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        **token_payload,
        "preview_token": preview_token,
        "points_per_employee": _LEARNING_BATCH_POINTS_PER_EMPLOYEE,
        "industry_counts": dict(sorted(industry_counts.items())),
        "target_sample": [
            {
                key: row[key]
                for key in (
                    "idx", "person", "name", "industry_key",
                    "config_revision", "identity_ref", "config_sha256",
                    "bundle_sha256",
                )
            }
            for row in target_rows[:8]
        ],
        "_targets": target_rows,
    }


def _learning_batch_public_preview(preview: dict) -> dict:
    return {key: value for key, value in preview.items() if key != "_targets"}


def _learning_owned_batch(batch_id: int) -> dict:
    try:
        batch = employeelearning.get_batch(int(batch_id))
    except (TypeError, ValueError, employeelearning.LearningError) as exc:
        raise HTTPException(404, "进修批次不存在") from exc
    try:
        tenant_id = int(batch.get("tenant_id"))
    except (TypeError, ValueError):
        tenant_id = -1
    if tenant_id != TEN():
        raise HTTPException(404, "进修批次不存在")
    if str(
        _learning_batch_metadata(batch).get("schema") or ""
    ) != "schema55-learning-batch-v1":
        raise HTTPException(404, "进修批次不存在")
    return batch


def _learning_batch_has_live_coordinator(batch_id: int) -> bool:
    task = _LEARNING_BATCH_COORDINATORS.get(int(batch_id))
    return bool(task is not None and not task.done())


def _learning_run_refundable_without_delivery(run: dict) -> bool:
    status = str(run.get("status") or "")
    if status in {
        employeelearning.RUN_FAILED,
        employeelearning.RUN_CANCELLED,
        employeelearning.RUN_EVIDENCE_INSUFFICIENT,
    }:
        return True
    return (
        status == employeelearning.RUN_EXPIRED
        and not _learning_run_has_durable_proposal_delivery(run)
    )


def _reconcile_learning_batch_for_owner(batch_id: int, tenant_id: int) -> dict:
    """Converge one explicitly tenant-owned batch without starting network work.

    This is intentionally lazy and scope-bound.  Startup only reports orphaned
    queued manifests; a named boss's list/detail/resume request authorizes
    reconciliation of that tenant's selected batch, never every tenant's
    historical learning rows.
    """
    batch_id = int(batch_id)
    tenant_id = int(tenant_id)
    live_coordinator = _learning_batch_has_live_coordinator(batch_id)
    with db.atomic():
        batch = employeelearning.get_batch(batch_id)
        if int(batch.get("tenant_id") or -1) != tenant_id:
            raise HTTPException(404, "进修批次不存在")
        if str(
            _learning_batch_metadata(batch).get("schema") or ""
        ) != "schema55-learning-batch-v1":
            raise HTTPException(404, "进修批次不存在")

        _expire_due_learning_batch_runs(batch_id)
        runs = employeelearning.list_batch_runs(batch_id)
        for run in runs:
            run_id = int(run["id"])
            status = str(run.get("status") or "")
            op_key = _learning_checkpoint_billing_op(run) or (
                _learning_billing_op_key(tenant_id, run_id)
            )
            operation = db.one(
                "SELECT status FROM billing_operation "
                "WHERE op_key=? AND tenant_id=? AND action='learn'",
                (op_key, tenant_id),
            )
            operation_status = str((operation or {}).get("status") or "")

            if (
                status == employeelearning.RUN_RESEARCHING
                and run_id not in _LEARNING_BATCH_ACTIVE_RUNS
            ):
                _settle_failed_learning_run_atomically(
                    run_id,
                    op_key if operation else "",
                    "无执行器的进修运行自动退回",
                    "ORPHANED_RESEARCH_WORKER",
                )
                continue
            if (
                float(run.get("spent_points") or 0) > 0
                and _learning_run_refundable_without_delivery(run)
                and operation_status != "succeeded"
            ):
                _settle_failed_learning_run_atomically(
                    run_id,
                    op_key if operation_status == "charged" else "",
                    "未交付的进修运行自动退回",
                    "REFUNDABLE_TERMINAL_RECONCILE",
                )

        employeelearning._refresh_batch_progress(batch_id)
        batch = employeelearning.get_batch(batch_id)
        runs = employeelearning.list_batch_runs(batch_id)
        queued = any(
            run.get("status") == employeelearning.RUN_QUEUED for run in runs
        )
        if (
            queued
            and not live_coordinator
            and str(batch.get("status") or "") in {
                employeelearning.BATCH_QUEUED,
                employeelearning.BATCH_RUNNING,
            }
        ):
            db.execute(
                "UPDATE employee_learning_batch "
                "SET status=?,paused_reason=?,updated_at=? WHERE id=? "
                "AND tenant_id=? AND status IN (?,?)",
                (
                    employeelearning.BATCH_PAUSED,
                    "服务重启后需老板显式恢复批次",
                    time.time(),
                    batch_id,
                    tenant_id,
                    employeelearning.BATCH_QUEUED,
                    employeelearning.BATCH_RUNNING,
                ),
            )
        return employeelearning.get_batch(batch_id)


def _learning_batch_compact_run(run: dict) -> dict:
    checkpoint = _learning_run_checkpoint(run)
    bundle_hash = str(checkpoint.get("expected_bundle_sha256") or "")
    bundle = None
    if re.fullmatch(r"[0-9a-f]{64}", bundle_hash):
        bundle = db.get_employee_role_bundle(
            str(run.get("identity_ref") or ""),
            int(run.get("base_config_revision") or run.get("config_revision") or 0),
            str(run.get("base_config_sha256") or ""),
            bundle_hash,
        )
    return {
        "id": int(run["id"]),
        "employee_idx": _learning_run_idx(run),
        "person": str((bundle or {}).get("person_snapshot") or ""),
        "name": str((bundle or {}).get("employee_name_snapshot") or ""),
        "industry_key": str(run.get("industry_key") or ""),
        "status": str(run.get("status") or ""),
        "identity_ref": str(run.get("identity_ref") or ""),
        "config_revision": int(
            run.get("base_config_revision") or run.get("config_revision") or 0
        ),
        "config_sha256": str(run.get("base_config_sha256") or ""),
        "bundle_sha256": bundle_hash,
        "budget_points": float(run.get("budget_points") or 0),
        "spent_points": float(run.get("spent_points") or 0),
        "error_code": run.get("error_code"),
        "updated_at": run.get("updated_at"),
    }


def _learning_batch_manifest_digest(runs: list[dict]) -> str | None:
    targets = []
    for run in sorted(runs, key=lambda row: _learning_run_idx(row)):
        checkpoint = _learning_run_checkpoint(run)
        frozen = {
            "idx": _learning_run_idx(run),
            "industry_key": str(run.get("industry_key") or ""),
            "identity_ref": str(run.get("identity_ref") or ""),
            "config_revision": int(
                run.get("base_config_revision")
                or run.get("config_revision") or 0
            ),
            "config_sha256": str(run.get("base_config_sha256") or ""),
            "bundle_sha256": str(
                checkpoint.get("expected_bundle_sha256") or ""
            ),
            "high_risk": bool(run.get("high_risk")),
        }
        if (
            frozen["config_revision"] <= 0
            or any(
                re.fullmatch(r"[0-9a-f]{64}", frozen[field]) is None
                for field in (
                    "identity_ref", "config_sha256", "bundle_sha256",
                )
            )
        ):
            return None
        targets.append(frozen)
    return hashlib.sha256(json.dumps(
        targets,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _learning_batch_actual_wallet_debit(
    batch: dict, runs: list[dict],
) -> tuple[float, bool]:
    """Return the current net durable debit, never the planned campaign cap."""
    if not runs:
        return 0.0, True
    tenant_id = int(batch.get("tenant_id") or 0)
    op_keys = [
        _learning_billing_op_key(tenant_id, int(run["id"])) for run in runs
    ]
    placeholders = ",".join("?" for _ in op_keys)
    rows = db.q(
        "SELECT op_key,points,status FROM billing_operation "
        "WHERE tenant_id=? AND action='learn' "
        f"AND op_key IN ({placeholders})",
        (tenant_id, *op_keys),
    )
    expected_points = 0.0 if tenant_id == 1 else _LEARNING_BATCH_POINTS_PER_EMPLOYEE
    valid = len(rows) <= len(op_keys)
    total = 0.0
    for row in rows:
        try:
            points = float(row.get("points"))
        except (TypeError, ValueError):
            valid = False
            continue
        status = str(row.get("status") or "")
        if (
            row.get("op_key") not in op_keys
            or status not in {"pending", "charged", "succeeded", "refunded"}
            or not math.isfinite(points)
            or abs(points - expected_points) > 1e-9
        ):
            valid = False
        if status in {"charged", "succeeded"} and math.isfinite(points):
            total += points
    return total, valid


def _learning_batch_public(batch: dict, *, include_runs: bool = True) -> dict:
    _expire_due_learning_batch_runs(int(batch["id"]))
    batch = employeelearning.get_batch(int(batch["id"]))
    runs = employeelearning.list_batch_runs(int(batch["id"]))
    counts: dict[str, int] = {}
    for run in runs:
        status = str(run.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    queued = counts.get(employeelearning.RUN_QUEUED, 0)
    researching = counts.get(employeelearning.RUN_RESEARCHING, 0)
    pending_review = counts.get(employeelearning.RUN_AWAITING_APPROVAL, 0)
    failed = sum(counts.get(status, 0) for status in {
        employeelearning.RUN_FAILED,
        employeelearning.RUN_EVIDENCE_INSUFFICIENT,
        employeelearning.RUN_STALE,
        employeelearning.RUN_EXPIRED,
        employeelearning.RUN_CANCELLED,
    })
    completed = sum(counts.get(status, 0) for status in _LEARNING_BATCH_TERMINAL_RUNS)
    metadata = _learning_batch_metadata(batch)
    billing_mode_value = str(metadata.get("billing_mode") or "").strip()
    target_digest_value = str(metadata.get("target_digest") or "").strip()
    try:
        wallet_charge_value = float(metadata["wallet_charge_points"])
        points_per_employee_value = float(metadata["points_per_employee"])
    except (KeyError, TypeError, ValueError):
        wallet_charge_value = -1.0
        points_per_employee_value = -1.0
    try:
        frozen_target_count = int(metadata["target_count"])
    except (KeyError, TypeError, ValueError):
        frozen_target_count = -1
    manifest_digest = _learning_batch_manifest_digest(runs)
    batch_cap = float(
        batch.get("budget_cap_points") or batch.get("budget_points") or 0
    )
    expected_planned_wallet = (
        0.0
        if billing_mode_value == "platform_included"
        else points_per_employee_value * len(runs)
    )
    actual_wallet_debit, actual_ledger_valid = (
        _learning_batch_actual_wallet_debit(batch, runs)
    )
    billing_proof_complete = (
        str(metadata.get("schema") or "") == "schema55-learning-batch-v1"
        and billing_mode_value in {"platform_included", "tenant_points"}
        and wallet_charge_value >= 0
        and points_per_employee_value > 0
        and re.fullmatch(r"[0-9a-f]{64}", target_digest_value) is not None
        and frozen_target_count == len(runs)
        and target_digest_value == manifest_digest
        and abs(batch_cap - points_per_employee_value * len(runs)) <= 1e-9
        and abs(wallet_charge_value - expected_planned_wallet) <= 1e-9
        and actual_ledger_valid
        and actual_wallet_debit <= wallet_charge_value + 1e-9
    )
    if billing_proof_complete:
        billing_mode = billing_mode_value
        planned_wallet_charge_points = wallet_charge_value
        wallet_charge_points = wallet_charge_value
        points_per_employee = points_per_employee_value
        target_digest = target_digest_value
    else:
        # Historical/foreign rows without the frozen proof must never infer a
        # zero wallet debit from tenant id or current run count.
        billing_mode = None
        wallet_charge_points = None
        planned_wallet_charge_points = None
        points_per_employee = None
        target_digest = None
    stored_status = str(batch.get("status") or employeelearning.BATCH_QUEUED)
    if stored_status == employeelearning.BATCH_PAUSED:
        display_status = employeelearning.BATCH_PAUSED
    elif queued or researching:
        display_status = (
            employeelearning.BATCH_RUNNING
            if researching else employeelearning.BATCH_QUEUED
        )
    elif pending_review:
        display_status = "awaiting_approval"
    elif runs and completed == len(runs):
        display_status = employeelearning.BATCH_COMPLETED
    else:
        display_status = stored_status
    result = {
        "id": int(batch["id"]),
        "request_key": str(
            batch.get("request_key") or batch.get("idempotency_key") or ""
        ),
        "status": display_status,
        "stored_status": stored_status,
        "target_count": len(runs),
        "budget_cap_points": float(
            batch.get("budget_cap_points") or batch.get("budget_points") or 0
        ),
        "spent_points": float(batch.get("spent_points") or 0),
        "max_concurrency": int(metadata.get("max_concurrency") or 1),
        "scope": metadata.get("scope") or {},
        "billing_mode": billing_mode,
        "wallet_charge_points": wallet_charge_points,
        "planned_wallet_charge_points": planned_wallet_charge_points,
        "actual_wallet_debit_points": actual_wallet_debit,
        "actual_wallet_debit_proof_status": (
            "verified" if billing_proof_complete else "proof_missing"
        ),
        "points_per_employee": points_per_employee,
        "target_digest": target_digest,
        "billing_proof_status": (
            "verified" if billing_proof_complete else "proof_missing"
        ),
        "auto_approve": False,
        "counts": {
            "queued": queued,
            "researching": researching,
            "completed": completed,
            "failed": failed,
            "pending_review": pending_review,
            "activated": counts.get(employeelearning.RUN_ACTIVATED, 0),
            "rejected": counts.get(employeelearning.RUN_REJECTED, 0),
        },
        "can_pause": bool(queued) and stored_status not in {
            employeelearning.BATCH_PAUSED,
            employeelearning.BATCH_COMPLETED,
            employeelearning.BATCH_CANCELLED,
        },
        "can_resume": bool(queued) and (
            stored_status == employeelearning.BATCH_PAUSED
            or int(batch["id"]) not in _LEARNING_BATCH_COORDINATORS
        ),
        "paused_reason": batch.get("paused_reason"),
        "created_at": batch.get("created_at"),
        "updated_at": batch.get("updated_at"),
    }
    if include_runs:
        result["runs"] = [_learning_batch_compact_run(run) for run in runs]
    return result


def _materialize_learning_batch_manifest(preview: dict, *, actor_id: int) -> dict:
    metadata = {
        "schema": "schema55-learning-batch-v1",
        "preview_token": preview["preview_token"],
        "target_digest": preview["target_digest"],
        "target_count": preview["target_count"],
        "scope": preview["scope"],
        "max_concurrency": preview["max_concurrency"],
        "points_per_employee": _LEARNING_BATCH_POINTS_PER_EMPLOYEE,
        "billing_mode": preview["billing_mode"],
        "wallet_charge_points": preview["wallet_charge_points"],
        "auto_approve": False,
    }
    batch = employeelearning.create_batch(
        preview["request_key"],
        budget_cap_points=preview["budget_cap_points"],
        tenant_id=preview["tenant_id"],
    )
    existing_metadata = _learning_batch_metadata(batch)
    if existing_metadata and existing_metadata.get("schema"):
        if (
            str(existing_metadata.get("preview_token") or "")
            != preview["preview_token"]
        ):
            raise employeelearning.LearningValidationError(
                "相同幂等键不能改变员工范围、预算或并发"
            )
    else:
        db.execute(
            "UPDATE employee_learning_batch SET metadata_json=?,max_runs=?,"
            "created_by=?,checkpoint_json=?,updated_at=? WHERE id=?",
            (
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                int(preview["target_count"]),
                int(actor_id) or None,
                json.dumps({"stage": "preparing"}, ensure_ascii=False),
                time.time(),
                int(batch["id"]),
            ),
        )
    for target in preview["_targets"]:
        run_key = "batch-run-" + hashlib.sha256(
            (
                f"{preview['tenant_id']}:{preview['request_key']}:"
                f"{target['idx']}:{target['identity_ref']}"
            ).encode("utf-8")
        ).hexdigest()
        run = employeelearning.create_run(
            int(batch["id"]),
            run_key,
            employee_idx=int(target["idx"]),
            identity_ref=target["identity_ref"],
            base_config_revision=int(target["config_revision"]),
            base_config_sha256=target["config_sha256"],
            industry_key=target["industry_key"],
            budget_points=_LEARNING_BATCH_POINTS_PER_EMPLOYEE,
            high_risk=bool(target["high_risk"]),
            expires_at=time.time() + 7 * 24 * 3600,
        )
        if (
            int(run.get("batch_id") or 0) != int(batch["id"])
            or int(run.get("employee_idx") or 0) != int(target["idx"])
            or str(run.get("identity_ref") or "") != target["identity_ref"]
            or int(run.get("base_config_revision") or 0)
            != int(target["config_revision"])
            or str(run.get("base_config_sha256") or "")
            != target["config_sha256"]
            or abs(float(run.get("budget_points") or 0) - 3.0) > 1e-9
        ):
            raise employeelearning.LearningValidationError(
                "批次运行幂等记录与预览四元组不一致"
            )
        checkpoint = _learning_run_checkpoint(run)
        expected = {
            "identity_ref": target["identity_ref"],
            "config_revision": int(target["config_revision"]),
            "config_sha256": target["config_sha256"],
            "expected_bundle_sha256": target["bundle_sha256"],
        }
        if checkpoint:
            for key, value in expected.items():
                if checkpoint.get(key) not in (None, "", value):
                    raise employeelearning.LearningValidationError(
                        "批次运行冻结四元组已漂移"
                    )
        if run.get("status") == employeelearning.RUN_QUEUED:
            employeelearning.checkpoint(
                int(run["id"]),
                {
                    **checkpoint,
                    **expected,
                    "stage": str(checkpoint.get("stage") or "queued"),
                },
            )
    manifest_count = len(employeelearning.list_batch_runs(int(batch["id"])))
    if manifest_count != int(preview["target_count"]):
        raise employeelearning.LearningValidationError("批次员工清单不完整")
    latest_batch = employeelearning.get_batch(int(batch["id"]))
    batch_checkpoint = _learning_batch_json(
        latest_batch.get("checkpoint_json"), {},
    )
    batch_checkpoint.update({
        "stage": "queued",
        "manifest_count": manifest_count,
    })
    db.execute(
        "UPDATE employee_learning_batch SET checkpoint_json=?,updated_at=? WHERE id=?",
        (
            json.dumps(batch_checkpoint, ensure_ascii=False, sort_keys=True),
            time.time(),
            int(batch["id"]),
        ),
    )
    return employeelearning.get_batch(int(batch["id"]))


def _create_learning_batch_manifest(preview: dict, *, actor_id: int) -> dict:
    """Create one complete frozen manifest or leave no batch/run rows behind.

    ``employeelearning.create_batch`` and ``create_run`` are individually
    atomic, but a manifest spans up to 360 runs.  Without this outer
    transaction, a live-identity conflict late in the list leaves the batch
    and earlier runs committed.  ``BEGIN IMMEDIATE`` also serializes the full
    owner preflight with creation, closing the check/create race between two
    concurrent campaigns.
    """
    targets = preview.get("_targets")
    if not isinstance(targets, list) or not targets:
        raise employeelearning.LearningValidationError("批次员工清单不完整")
    identities = [str(target.get("identity_ref") or "") for target in targets]
    if len(set(identities)) != len(identities):
        raise employeelearning.LearningValidationError("批次员工身份重复")

    with db.atomic():
        for target in targets:
            idx = int(target.get("idx") or 0)
            employee = employeeidentity.active_employee(idx)
            config = employees.get_config(idx) if employee else None
            identity = (
                _employee_public_contract(employee, config=config)
                if employee and config else {}
            )
            bundle = db.get_employee_role_bundle(
                str(target.get("identity_ref") or ""),
                int(target.get("config_revision") or 0),
                str(target.get("config_sha256") or ""),
                str(target.get("bundle_sha256") or ""),
            )
            if (
                not employee
                or str(employee.get("catalog_version") or "")
                != departments.DECISION_V4_CATALOG_VERSION
                or not config
                or not identity.get("can_learn")
                or employeeidentity.identity_ref(employee)
                != str(target.get("identity_ref") or "")
                or str(config.get("identity_ref") or "")
                != str(target.get("identity_ref") or "")
                or int(config.get("config_revision") or 0)
                != int(target.get("config_revision") or 0)
                or str(config.get("config_sha256") or "")
                != str(target.get("config_sha256") or "")
                or str(config.get("bundle_sha256") or "")
                != str(target.get("bundle_sha256") or "")
                or not bundle
            ):
                raise employeelearning.LearningValidationError(
                    "员工岗位四元绑定已变化，请重新预览"
                )
        existing = db.one(
            "SELECT * FROM employee_learning_batch "
            "WHERE (request_key=? OR idempotency_key=?) "
            "AND (tenant_id=? OR tenant_id IS NULL) ORDER BY id LIMIT 1",
            (
                preview["request_key"],
                preview["request_key"],
                int(preview["tenant_id"]),
            ),
        )
        if existing and str(
            _learning_batch_metadata(existing).get("schema") or ""
        ) != "schema55-learning-batch-v1":
            raise employeelearning.LearningValidationError(
                "相同幂等键已用于其他进修模式"
            )
        replay_batch_id = int(existing["id"]) if existing else None
        for identity_ref in identities:
            _expire_due_learning_identity_runs(identity_ref)
            owner = employeelearning._identity_run_owner(identity_ref)
            if owner and int(owner.get("batch_id") or 0) != replay_batch_id:
                raise employeelearning.InvalidTransitionError(
                    "批次包含已有未结束进修运行的员工，"
                    "请先完成或终止原运行"
                )
        return _materialize_learning_batch_manifest(preview, actor_id=actor_id)


def _create_learning_batch_manifest_for_schedule(
    preview: dict, *, actor_id: int,
) -> dict:
    """Return the manifest plus whether this request owns its compensation.

    The existence check and manifest creation share one write transaction, so
    a failed worker start can terminalize only rows created by this request.
    An idempotent replay is durable state owned by the earlier request and must
    never be deleted or cancelled by a later caller.
    """
    with db.atomic():
        existing = db.one(
            "SELECT id FROM employee_learning_batch "
            "WHERE (request_key=? OR idempotency_key=?) "
            "AND (tenant_id=? OR tenant_id IS NULL) ORDER BY id LIMIT 1",
            (
                preview["request_key"],
                preview["request_key"],
                int(preview["tenant_id"]),
            ),
        )
        batch = _create_learning_batch_manifest(preview, actor_id=actor_id)
        checkpoint = _learning_batch_json(batch.get("checkpoint_json"), {})
        schedule_generation = _learning_batch_coordinator_generation(batch) + 1
        checkpoint["coordinator_generation"] = schedule_generation
        db.execute(
            "UPDATE employee_learning_batch SET checkpoint_json=?,updated_at=? "
            "WHERE id=?",
            (
                json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                time.time(),
                int(batch["id"]),
            ),
        )
        return {
            "batch": employeelearning.get_batch(int(batch["id"])),
            "created": existing is None,
            "schedule_generation": schedule_generation,
        }


def _settle_unstarted_learning_batch_manifest(result: dict) -> bool:
    """Close a newly-created orphan; fail-safe pause an idempotent replay."""
    batch = dict(result.get("batch") or {})
    batch_id = int(batch.get("id") or 0)
    if batch_id <= 0:
        return False
    created = bool(result.get("created"))
    try:
        schedule_generation = int(result.get("schedule_generation"))
    except (TypeError, ValueError):
        return False
    reason = "批次协调器启动失败，本次新建清单已安全终结"
    with db.atomic():
        current = employeelearning.get_batch(batch_id)
        if _learning_batch_coordinator_generation(current) != schedule_generation:
            # A same-key replay has durably taken a newer scheduling lease.
            # This failed caller no longer owns compensation for the manifest.
            return False
        if str(current.get("status") or "") not in {
            employeelearning.BATCH_QUEUED,
            employeelearning.BATCH_RUNNING,
        }:
            # An explicit pause/terminal transition is also a newer owner even
            # for legacy rows that predate schedule generations.
            return False
        if created:
            for run in employeelearning.list_batch_runs(batch_id):
                run_id = int(run["id"])
                employeelearning.release_budget(run_id)
                if str(run.get("status") or "") not in _LEARNING_BATCH_TERMINAL_RUNS:
                    employeelearning.cancel_run(
                        run_id, reason="COORDINATOR_START_FAILED",
                    )
            db.execute(
                "UPDATE employee_learning_batch "
                "SET status=?,paused_reason=?,checkpoint_json=?,updated_at=? "
                "WHERE id=?",
                (
                    employeelearning.BATCH_CANCELLED,
                    reason,
                    json.dumps({
                        "stage": "start_failed",
                        "reason": "COORDINATOR_START_FAILED",
                    }, ensure_ascii=False, sort_keys=True),
                    time.time(),
                    batch_id,
                ),
            )
            return True
        if str(current.get("status") or "") in {
            employeelearning.BATCH_QUEUED,
            employeelearning.BATCH_RUNNING,
        }:
            db.execute(
                "UPDATE employee_learning_batch "
                "SET status=?,paused_reason=?,updated_at=? WHERE id=?",
                (
                    employeelearning.BATCH_PAUSED,
                    "批次协调器启动失败，请显式恢复",
                    time.time(),
                    batch_id,
                ),
            )
            return True
        return False


def _learning_batch_frozen_binding(run: dict) -> dict | None:
    checkpoint = _learning_run_checkpoint(run)
    identity_ref = str(run.get("identity_ref") or "")
    revision = int(run.get("base_config_revision") or run.get("config_revision") or 0)
    config_hash = str(run.get("base_config_sha256") or "")
    bundle_hash = str(checkpoint.get("expected_bundle_sha256") or "")
    if (
        checkpoint.get("identity_ref") != identity_ref
        or checkpoint.get("config_revision") != revision
        or checkpoint.get("config_sha256") != config_hash
        or re.fullmatch(r"[0-9a-f]{64}", bundle_hash) is None
    ):
        return None
    employee = employeeidentity.employee_by_identity_ref(identity_ref)
    config = employees.get_config_by_identity(
        identity_ref, revision=revision, config_sha256=config_hash,
    )
    bundle = db.get_employee_role_bundle(
        identity_ref, revision, config_hash, bundle_hash,
    )
    active = employeeidentity.active_employee(_learning_run_idx(run))
    current = employees.get_config(_learning_run_idx(run)) if active else None
    identity = (
        _employee_public_contract(active, config=current)
        if active and current else {}
    )
    if (
        not employee
        or not config
        or not bundle
        or not active
        or not identity.get("can_learn")
        or employeeidentity.identity_ref(active) != identity_ref
        or str((current or {}).get("identity_ref") or "") != identity_ref
        or int((current or {}).get("config_revision") or 0) != revision
        or str((current or {}).get("config_sha256") or "") != config_hash
        or str((current or {}).get("bundle_sha256") or "") != bundle_hash
    ):
        return None
    return {
        "employee": employee,
        "config": config,
        "identity": identity,
        "role_bundle": bundle,
    }


def _claim_learning_batch_run_for_launch(run_id: int) -> dict:
    """Linearize expiry/eligibility with reserve, start and first checkpoint."""
    with db.atomic():
        run = _expire_learning_run_if_due(int(run_id))
        if run.get("status") != employeelearning.RUN_QUEUED:
            return {"claimed": False, "run": run, "binding": None}
        batch = employeelearning.get_batch(int(run["batch_id"]))
        if str(batch.get("status") or "") == employeelearning.BATCH_PAUSED:
            return {"claimed": False, "run": run, "binding": None}
        binding = _learning_batch_frozen_binding(run)
        if not binding:
            run = employeelearning.mark_stale(
                int(run_id),
                reason="IDENTITY_CONFIG_OR_ELIGIBILITY_STALE_BEFORE_RESEARCH",
            )
            return {"claimed": False, "run": run, "binding": None}
        employeelearning.reserve_budget(int(run_id))
        # Defensive re-read within the same BEGIN IMMEDIATE boundary.  It is
        # normally identical because concurrent writers are excluded, but it
        # also closes re-entrant slot/config mutations made by maintenance
        # hooks during reservation.
        run = _expire_learning_run_if_due(int(run_id))
        binding = (
            _learning_batch_frozen_binding(run)
            if run.get("status") == employeelearning.RUN_QUEUED else None
        )
        if not binding:
            employeelearning.release_budget(int(run_id))
            if run.get("status") == employeelearning.RUN_QUEUED:
                run = employeelearning.mark_stale(
                    int(run_id),
                    reason="IDENTITY_CONFIG_OR_ELIGIBILITY_STALE_AT_LAUNCH_CLAIM",
                )
            return {"claimed": False, "run": run, "binding": None}
        employeelearning.start_run(int(run_id), allow_existing=False)
        checkpoint = _learning_run_checkpoint(
            employeelearning.get_run(int(run_id))
        )
        run = employeelearning.checkpoint(
            int(run_id),
            {**checkpoint, "stage": "billing_pending"},
        )
        return {"claimed": True, "run": run, "binding": binding}


async def _execute_learning_batch_run(run_id: int, tenant_id: int) -> None:
    op_key = _learning_billing_op_key(tenant_id, run_id)
    billing_started = False
    try:
        prepared = await _run_db_safely(
            _claim_learning_batch_run_for_launch, run_id,
        )
        if not prepared["claimed"]:
            return
        run = dict(prepared["run"])
        binding = dict(prepared["binding"])
        checkpoint = _learning_run_checkpoint(run)
        billing_op = await _run_db_safely(
            _start_learning_billing_at_frozen_price,
            run_id,
            int(tenant_id),
            (
                f"{binding['employee'].get('person') or binding['employee'].get('name') or '数字员工'}"
                "批量证据进修"
            ),
        )
        billing_started = True
        await _run_db_safely(
            employeelearning.checkpoint,
            run_id,
            {**checkpoint, "stage": "researching", "billing_op_key": billing_op},
        )
        await _employee_learning_research_worker(run_id, binding)
    except billing.InsufficientPoints:
        try:
            await _run_db_safely(
                employeelearning.defer_run_for_billing,
                run_id,
                reason="INSUFFICIENT_POINTS",
            )
            run = await _run_db_safely(employeelearning.get_run, run_id)
            await _run_db_safely(
                _pause_learning_batch_without_interrupting,
                int(run["batch_id"]),
                "余额不足，批次已暂停",
            )
        except BaseException:
            pass
    except BaseException as exc:
        settled = False
        if billing_started:
            try:
                # The post-charge checkpoint may be precisely the write that
                # failed, so the durable run cannot be trusted to contain the
                # op key yet.  Refund by the deterministic local key retained
                # from before the charge.
                await _settle_failed_learning_run_safely(
                    run_id,
                    op_key,
                    "批量员工进修未启动自动退回",
                    "START_FAILED",
                )
                settled = True
            except BaseException as refund_exc:
                logging.getLogger("employeelearning").error(
                    "employee learning batch refund failed run_id=%s error_type=%s",
                    run_id,
                    type(refund_exc).__name__,
                )
        if not settled:
            try:
                await _terminalize_learning_run_safely(run_id, "START_FAILED")
            except BaseException:
                pass
        logging.getLogger("employeelearning").error(
            "employee learning batch run failed run_id=%s error_type=%s",
            run_id,
            type(exc).__name__,
        )
    finally:
        _LEARNING_BATCH_ACTIVE_RUNS.discard(int(run_id))


def _pause_learning_batch_without_interrupting(batch_id: int, reason: str) -> dict:
    """Pause future launches while allowing already-running evidence capture to finish."""
    with db.atomic():
        batch = employeelearning.get_batch(batch_id)
        if str(batch.get("status") or "") in {
            employeelearning.BATCH_COMPLETED,
            employeelearning.BATCH_CANCELLED,
        }:
            return batch
        queued = db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_run "
            "WHERE batch_id=? AND status=?",
            (int(batch_id), employeelearning.RUN_QUEUED),
        )
        if int((queued or {}).get("n") or 0) <= 0:
            return employeelearning.get_batch(batch_id)
        checkpoint = _learning_batch_json(batch.get("checkpoint_json"), {})
        checkpoint["coordinator_generation"] = (
            _learning_batch_coordinator_generation(batch) + 1
        )
        db.execute(
            "UPDATE employee_learning_batch SET status=?,paused_reason=?,"
            "checkpoint_json=?,updated_at=? "
            "WHERE id=? AND status IN (?,?)",
            (
                employeelearning.BATCH_PAUSED,
                str(reason or "老板手动暂停")[:500],
                json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                time.time(),
                int(batch_id),
                employeelearning.BATCH_QUEUED,
                employeelearning.BATCH_RUNNING,
            ),
        )
        return employeelearning.get_batch(batch_id)


def _settle_learning_batch_coordinator_failure(
    batch_id: int, reason: str, *, expected_generation: int | None = None,
) -> bool:
    """Turn ownerless queued work into an explicit, retryable paused state."""
    with db.atomic():
        batch = employeelearning.get_batch(int(batch_id))
        if str(
            _learning_batch_metadata(batch).get("schema") or ""
        ) != "schema55-learning-batch-v1":
            return False
        if (
            expected_generation is not None
            and _learning_batch_coordinator_generation(batch)
            != int(expected_generation)
        ):
            return False
        queued = any(
            run.get("status") == employeelearning.RUN_QUEUED
            for run in employeelearning.list_batch_runs(int(batch_id))
        )
        if not queued or str(batch.get("status") or "") not in {
            employeelearning.BATCH_QUEUED,
            employeelearning.BATCH_RUNNING,
        }:
            return False
        db.execute(
            "UPDATE employee_learning_batch SET status=?,paused_reason=?,"
            "updated_at=? WHERE id=?",
            (
                employeelearning.BATCH_PAUSED,
                str(reason or "批次协调器中断，请显式恢复")[:500],
                time.time(),
                int(batch_id),
            ),
        )
        return True


def _learning_batch_coordinator_generation(batch: dict) -> int:
    checkpoint = _learning_batch_json(batch.get("checkpoint_json"), {})
    try:
        value = int(checkpoint.get("coordinator_generation") or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, value)


def _detect_orphaned_learning_batches_for_restart() -> int:
    """Read-only startup signal; never mutate or auto-start historical work.

    A named boss may later reconcile one tenant-owned batch through its list,
    detail or explicit resume path.  Startup deliberately has no authority to
    pause or otherwise rewrite every tenant's historical queued manifests.
    """
    row = db.one(
        "SELECT COUNT(*) AS n FROM employee_learning_batch AS batch "
        "WHERE batch.status IN (?,?) "
        "AND json_extract(CASE WHEN json_valid(batch.metadata_json) "
        "THEN batch.metadata_json ELSE '{}' END,'$.schema')=? "
        "AND EXISTS (SELECT 1 FROM employee_learning_run AS run "
        "WHERE run.batch_id=batch.id AND run.status=?)",
        (
            employeelearning.BATCH_QUEUED,
            employeelearning.BATCH_RUNNING,
            "schema55-learning-batch-v1",
            employeelearning.RUN_QUEUED,
        ),
    )
    return int((row or {}).get("n") or 0)


async def _employee_learning_batch_coordinator(
    batch_id: int, coordinator_generation: int = 0,
) -> None:
    tasks: set[asyncio.Task] = set()
    paused_exit = False
    coordinator_failure = None
    try:
        while True:
            batch = await db.arun(employeelearning.get_batch, batch_id)
            metadata = _learning_batch_metadata(batch)
            max_concurrency = max(1, min(int(metadata.get("max_concurrency") or 1), 8))
            status = str(batch.get("status") or "")
            runs = await db.arun(employeelearning.list_batch_runs, batch_id)
            orphaned_researching = [
                run for run in runs
                if run.get("status") == employeelearning.RUN_RESEARCHING
                and int(run["id"]) not in _LEARNING_BATCH_ACTIVE_RUNS
            ]
            if orphaned_researching:
                for orphan in orphaned_researching:
                    await _settle_failed_learning_run_safely(
                        int(orphan["id"]),
                        _learning_checkpoint_billing_op(orphan),
                        "无执行器的进修运行自动退回",
                        "ORPHANED_RESEARCH_WORKER",
                    )
                await _run_db_safely(
                    _settle_learning_batch_coordinator_failure,
                    int(batch_id),
                    "检测到无执行器的研究运行，请显式恢复",
                    expected_generation=int(coordinator_generation),
                )
                paused_exit = True
                break
            researching_ids = {
                int(run["id"]) for run in runs
                if run.get("status") == employeelearning.RUN_RESEARCHING
            }
            # A child is registered before its DB launch claim.  Count that
            # pre-claim ACTIVE window against this batch's capacity as well;
            # otherwise a coordinator handoff could launch a second child
            # while the first still appears queued in SQLite.
            batch_run_ids = {int(run["id"]) for run in runs}
            active_ids = batch_run_ids.intersection(_LEARNING_BATCH_ACTIVE_RUNS)
            launch_owners = researching_ids.union(active_ids)
            if status not in {
                employeelearning.BATCH_PAUSED,
                employeelearning.BATCH_COMPLETED,
                employeelearning.BATCH_CANCELLED,
            }:
                capacity = max(0, max_concurrency - len(launch_owners))
                for run in (
                    row for row in runs
                    if row.get("status") == employeelearning.RUN_QUEUED
                ):
                    run_id = int(run["id"])
                    if capacity <= 0:
                        break
                    if run_id in _LEARNING_BATCH_ACTIVE_RUNS:
                        continue
                    _LEARNING_BATCH_ACTIVE_RUNS.add(run_id)
                    worker_coro = _execute_learning_batch_run(
                        run_id, int(batch.get("tenant_id") or 0),
                    )
                    try:
                        task = asyncio.create_task(worker_coro)
                    except BaseException:
                        worker_coro.close()
                        _LEARNING_BATCH_ACTIVE_RUNS.discard(run_id)
                        raise
                    tasks.add(task)
                    def release_child(
                        completed: asyncio.Task, *, claimed_run_id: int = run_id,
                    ) -> None:
                        tasks.discard(completed)
                        # A task cancelled before its coroutine's first step
                        # never enters ``_execute_learning_batch_run.finally``.
                        # Registry ownership therefore also belongs to this
                        # done callback, with the run id frozen per child.
                        _LEARNING_BATCH_ACTIVE_RUNS.discard(claimed_run_id)

                    task.add_done_callback(release_child)
                    capacity -= 1
            if tasks:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                continue
            if status == employeelearning.BATCH_PAUSED:
                # Resume can commit while this coordinator is between its
                # paused snapshot and exit.  The resume endpoint then sees
                # this task as active and correctly avoids a duplicate; re-read
                # before exit so that active task also observes the hand-off.
                latest = await db.arun(employeelearning.get_batch, batch_id)
                if str(latest.get("status") or "") != employeelearning.BATCH_PAUSED:
                    continue
                paused_exit = True
                break
            runs = await db.arun(employeelearning.list_batch_runs, batch_id)
            if any(
                run.get("status") in {
                    employeelearning.RUN_QUEUED,
                    employeelearning.RUN_RESEARCHING,
                }
                for run in runs
            ):
                await asyncio.sleep(0.5)
                continue
            break
    except BaseException as exc:
        coordinator_failure = exc
        raise
    finally:
        current = _LEARNING_BATCH_COORDINATORS.get(int(batch_id))
        if current is asyncio.current_task():
            shutting_down = bool(getattr(
                app.state, "learning_batch_shutting_down", False,
            ))
            if coordinator_failure is not None and not paused_exit:
                settlement_attempt = 0
                while not shutting_down:
                    try:
                        await _run_db_safely(
                            _settle_learning_batch_coordinator_failure,
                            int(batch_id),
                            "批次协调器异常中断，请显式恢复",
                            expected_generation=int(coordinator_generation),
                        )
                        break
                    except BaseException as exc:
                        settlement_attempt += 1
                        if settlement_attempt in {1, 10}:
                            logging.getLogger("employeelearning").warning(
                                "employee learning coordinator settlement retry "
                                "batch_id=%s attempt=%s error_type=%s",
                                batch_id,
                                settlement_attempt,
                                type(exc).__name__,
                            )
                        try:
                            await asyncio.sleep(min(0.05 * settlement_attempt, 0.5))
                        except asyncio.CancelledError:
                            pass
                        shutting_down = bool(getattr(
                            app.state, "learning_batch_shutting_down", False,
                        ))

            if (
                not shutting_down
                and (paused_exit or coordinator_failure is not None)
            ):
                # Keep registry ownership while durable handoff reads retry.
                # Once the latest state is known, pop + optional successor
                # scheduling run without an await, closing the final-read race.
                handoff_attempt = 0
                while not shutting_down:
                    try:
                        latest = await _run_db_safely(
                            employeelearning.get_batch, int(batch_id),
                        )
                        should_schedule = False
                        if str(latest.get("status") or "") in {
                            employeelearning.BATCH_QUEUED,
                            employeelearning.BATCH_RUNNING,
                        }:
                            runs = await _run_db_safely(
                                employeelearning.list_batch_runs, int(batch_id),
                            )
                            should_schedule = any(
                                run.get("status") == employeelearning.RUN_QUEUED
                                for run in runs
                            )
                        _LEARNING_BATCH_COORDINATORS.pop(int(batch_id), None)
                        if should_schedule:
                            try:
                                _schedule_employee_learning_batch(int(batch_id))
                            except BaseException:
                                # Scheduling failed before registry ownership
                                # transferred. Restore this recovery supervisor
                                # and retry until it can settle/handoff safely.
                                _LEARNING_BATCH_COORDINATORS[int(batch_id)] = (
                                    asyncio.current_task()
                                )
                                raise
                        break
                    except BaseException as exc:
                        handoff_attempt += 1
                        if handoff_attempt in {1, 10}:
                            logging.getLogger("employeelearning").warning(
                                "employee learning coordinator handoff retry "
                                "batch_id=%s attempt=%s error_type=%s",
                                batch_id,
                                handoff_attempt,
                                type(exc).__name__,
                            )
                        try:
                            await asyncio.sleep(min(0.05 * handoff_attempt, 0.5))
                        except asyncio.CancelledError:
                            pass
                        shutting_down = bool(getattr(
                            app.state, "learning_batch_shutting_down", False,
                        ))
            else:
                _LEARNING_BATCH_COORDINATORS.pop(int(batch_id), None)


def _schedule_employee_learning_batch(batch_id: int) -> bool:
    batch_id = int(batch_id)
    current = _LEARNING_BATCH_COORDINATORS.get(batch_id)
    if current and not current.done():
        return False
    batch = employeelearning.get_batch(batch_id)
    coordinator = _employee_learning_batch_coordinator(
        batch_id, _learning_batch_coordinator_generation(batch),
    )
    try:
        task = asyncio.create_task(coordinator)
    except BaseException:
        # ``asyncio.create_task(coro)`` does not consume/close ``coro`` when
        # there is no running loop.  Close it explicitly so a failed schedule
        # cannot also leak an un-awaited coroutine warning.
        coordinator.close()
        raise
    _LEARNING_BATCH_COORDINATORS[batch_id] = task
    return True


def _resume_employee_learning_batch_state(
    batch_id: int, tenant_id: int,
) -> dict:
    """Atomically validate and persist an idempotent resume transition."""
    with db.atomic():
        try:
            batch = employeelearning.get_batch(int(batch_id))
        except (TypeError, ValueError, employeelearning.LearningError) as exc:
            raise HTTPException(404, "进修批次不存在") from exc
        if int(batch.get("tenant_id") or -1) != int(tenant_id):
            raise HTTPException(404, "进修批次不存在")
        if str(
            _learning_batch_metadata(batch).get("schema") or ""
        ) != "schema55-learning-batch-v1":
            raise HTTPException(404, "进修批次不存在")
        runs = employeelearning.list_batch_runs(int(batch_id))
        queued = sum(
            str(run.get("status") or "") == employeelearning.RUN_QUEUED
            for run in runs
        )
        if queued <= 0:
            raise employeelearning.InvalidTransitionError(
                "当前批次没有排队中的员工"
            )
        previous_status = str(batch.get("status") or "")
        if previous_status in {
            employeelearning.BATCH_COMPLETED,
            employeelearning.BATCH_CANCELLED,
        }:
            raise employeelearning.InvalidTransitionError("终态批次不可恢复")
        if previous_status not in {
            employeelearning.BATCH_PAUSED,
            employeelearning.BATCH_QUEUED,
            employeelearning.BATCH_RUNNING,
        }:
            raise employeelearning.InvalidTransitionError("当前批次不可恢复")

        transitioned = previous_status == employeelearning.BATCH_PAUSED
        resumed_at = time.time()
        checkpoint = _learning_batch_json(batch.get("checkpoint_json"), {})
        generation = _learning_batch_coordinator_generation(batch) + 1
        checkpoint["coordinator_generation"] = generation
        db.execute(
            "UPDATE employee_learning_batch SET status=?,paused_reason=?,"
            "checkpoint_json=?,updated_at=? WHERE id=? AND tenant_id=?",
            (
                (
                    employeelearning.BATCH_QUEUED
                    if transitioned else previous_status
                ),
                None if transitioned else batch.get("paused_reason"),
                json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                resumed_at,
                int(batch_id),
                int(tenant_id),
            ),
        )
        return {
            "batch_id": int(batch_id),
            "tenant_id": int(tenant_id),
            "transitioned": transitioned,
            "previous_status": previous_status,
            "previous_paused_reason": batch.get("paused_reason"),
            "resumed_at": resumed_at,
            "coordinator_generation": generation,
        }


def _settle_unstarted_employee_learning_batch(state: dict) -> bool:
    """Fail closed when an accepted resume cannot start its coordinator."""
    batch_id = int(state["batch_id"])
    tenant_id = int(state["tenant_id"])
    reason = (
        str(state.get("previous_paused_reason") or "").strip()
        if state.get("transitioned")
        else "批次协调器启动失败，请重新恢复"
    )
    reason = reason or "批次协调器启动失败，请重新恢复"
    with db.atomic():
        batch = employeelearning.get_batch(batch_id)
        if int(batch.get("tenant_id") or -1) != tenant_id:
            return False
        try:
            expected_generation = int(state.get("coordinator_generation"))
        except (TypeError, ValueError):
            return False
        if _learning_batch_coordinator_generation(batch) != expected_generation:
            # A later explicit resume owns this generation; an older failed
            # scheduler must not pause work already handed to its successor.
            return False
        if str(batch.get("status") or "") not in {
            employeelearning.BATCH_QUEUED,
            employeelearning.BATCH_RUNNING,
        }:
            return False
        queued = db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_run "
            "WHERE batch_id=? AND status=?",
            (batch_id, employeelearning.RUN_QUEUED),
        )
        if int((queued or {}).get("n") or 0) <= 0:
            return False
        changed = db.execute(
            "UPDATE employee_learning_batch "
            "SET status=?,paused_reason=?,updated_at=? "
            "WHERE id=? AND tenant_id=? AND status IN (?,?)",
            (
                employeelearning.BATCH_PAUSED,
                reason[:500],
                time.time(),
                batch_id,
                tenant_id,
                employeelearning.BATCH_QUEUED,
                employeelearning.BATCH_RUNNING,
            ),
        )
        return changed == 1


@app.post("/api/employee-learning/batches/dry-run")
async def employee_learning_batch_dry_run(body: dict):
    """Preview an exact V4 campaign without creating runs or charging points."""
    _need_boss()
    preview = await db.arun(
        _learning_batch_preview_contract, body, tenant_id=TEN(),
    )
    return {"ok": True, "preview": _learning_batch_public_preview(preview)}


@app.post("/api/employee-learning/batches")
async def employee_learning_batch_create(body: dict):
    """Create an inert manifest only after the exact preview is confirmed."""
    _need_boss()
    if not isinstance(body, dict) or body.get("confirm_execute") is not True:
        raise HTTPException(400, "必须先预览并明确提交 confirm_execute=true")
    if body.get("auto_approve") not in (None, False):
        raise HTTPException(400, "批量进修不允许自动批准")
    preview = await db.arun(
        _learning_batch_preview_contract, body, tenant_id=TEN(),
    )
    preview_token = str(body.get("preview_token") or "").strip()
    if preview_token != preview["preview_token"]:
        raise HTTPException(409, "员工、岗位版本、预算或并发已变化，请重新预览")
    schedule_result = {"started": False}

    def start_coordinator(result: dict) -> None:
        schedule_result["started"] = _schedule_employee_learning_batch(
            int(result["batch"]["id"])
        )

    try:
        result = await _run_db_then_start_worker_safely(
            _create_learning_batch_manifest_for_schedule,
            preview,
            actor_id=int((auth.current() or {}).get("id") or 0),
            start_worker=start_coordinator,
            should_start=lambda value: str(
                value["batch"].get("status") or ""
            ) in {
                employeelearning.BATCH_QUEUED,
                employeelearning.BATCH_RUNNING,
            },
            settle_unstarted=_settle_unstarted_learning_batch_manifest,
        )
    except (
        employeelearning.LearningValidationError,
        employeelearning.InvalidTransitionError,
    ) as exc:
        raise HTTPException(409, str(exc)) from exc
    batch = result["batch"]
    return {
        "ok": True,
        "started": schedule_result["started"],
        "batch": await db.arun(_learning_batch_public, batch),
    }


@app.get("/api/employee-learning/batches")
def employee_learning_batches_list(limit: int = 10):
    _need_boss()
    bounded = max(1, min(int(limit), 20))
    rows = db.q(
        "SELECT * FROM employee_learning_batch WHERE tenant_id=? "
        "AND json_extract(CASE WHEN json_valid(metadata_json) "
        "THEN metadata_json ELSE '{}' END,'$.schema')=? "
        "ORDER BY id DESC LIMIT ?",
        (TEN(), "schema55-learning-batch-v1", bounded),
    )
    return {
        "batches": [
            _learning_batch_public(dict(row), include_runs=False) for row in rows
        ]
    }


@app.get("/api/employee-learning/batches/{batch_id}")
def employee_learning_batch_get(batch_id: int):
    _need_boss()
    return {"batch": _learning_batch_public(_learning_owned_batch(batch_id))}


@app.post("/api/employee-learning/batches/{batch_id}/pause")
def employee_learning_batch_pause(batch_id: int, body: dict | None = None):
    _need_boss()
    batch = _learning_owned_batch(batch_id)
    public = _learning_batch_public(batch, include_runs=False)
    if not public["can_pause"]:
        raise HTTPException(409, "当前批次没有可暂停的排队或研究任务")
    try:
        paused = _pause_learning_batch_without_interrupting(
            int(batch_id), str((body or {}).get("reason") or "老板手动暂停"),
        )
    except employeelearning.InvalidTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "batch": _learning_batch_public(paused)}


@app.post("/api/employee-learning/batches/{batch_id}/resume")
async def employee_learning_batch_resume(batch_id: int):
    _need_boss()
    tenant_id = TEN()
    # Resume is the explicit, named-boss authorization boundary for repairing
    # this one tenant-owned batch after a restart.  Reconciliation never
    # schedules work; the accepted resume below is the only launch decision.
    await db.arun(
        _reconcile_learning_batch_for_owner,
        int(batch_id),
        int(tenant_id),
    )
    schedule_result = {"started": False}

    def start_coordinator(state: dict) -> None:
        schedule_result["started"] = _schedule_employee_learning_batch(
            int(state["batch_id"])
        )

    try:
        await _run_db_then_start_worker_safely(
            _resume_employee_learning_batch_state,
            int(batch_id),
            int(tenant_id),
            start_worker=start_coordinator,
            settle_unstarted=_settle_unstarted_employee_learning_batch,
        )
    except employeelearning.InvalidTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    resumed = await db.arun(_learning_owned_batch, int(batch_id))
    return {
        "ok": True,
        "started": schedule_result["started"],
        "batch": await db.arun(_learning_batch_public, resumed),
    }


def _prepare_single_employee_learning_run(
    *,
    idx: int,
    request_key: str,
    tenant_id: int,
    actor_id: int,
    binding: dict,
) -> dict:
    """Atomically freeze, reserve, start and checkpoint one run.

    The transaction is the durable hand-off boundary for the async launcher.
    It also permanently binds an idempotency key to its original employee and
    four-tuple, and prevents single/bulk requests from sharing one manifest.
    """
    employee = dict(binding["employee"])
    config = dict(binding["config"])
    identity_ref = str(config.get("identity_ref") or "")
    expected_bundle = str(config.get("bundle_sha256") or "")
    run_key = "run-" + hashlib.sha256(
        f"{int(tenant_id)}:{request_key}:{int(idx)}".encode("utf-8")
    ).hexdigest()
    single_schema = "schema55-learning-single-v1"
    with db.atomic():
        _expire_due_learning_identity_runs(identity_ref)

        # Close the binding/read-to-manifest TOCTOU and the disabled-slot gap.
        active = employeeidentity.active_employee(int(idx))
        current = employees.get_config(int(idx)) if active else None
        identity = (
            _employee_public_contract(active, config=current)
            if active and current else {}
        )
        bundle = db.get_employee_role_bundle(
            identity_ref,
            int(config.get("config_revision") or 0),
            str(config.get("config_sha256") or ""),
            expected_bundle,
        )
        if (
            not active
            or not current
            or not identity.get("can_learn")
            or employeeidentity.identity_ref(active) != identity_ref
            or str(current.get("identity_ref") or "") != identity_ref
            or int(current.get("config_revision") or 0)
            != int(config.get("config_revision") or 0)
            or str(current.get("config_sha256") or "")
            != str(config.get("config_sha256") or "")
            or str(current.get("bundle_sha256") or "") != expected_bundle
            or not bundle
        ):
            raise employeelearning.InvalidTransitionError(
                "员工当前岗位四元绑定已变化或不可进修"
            )

        existing_batch = db.one(
            "SELECT * FROM employee_learning_batch "
            "WHERE (request_key=? OR idempotency_key=?) "
            "AND (tenant_id=? OR tenant_id IS NULL) ORDER BY id LIMIT 1",
            (request_key, request_key, int(tenant_id)),
        )
        if existing_batch:
            metadata = _learning_batch_metadata(existing_batch)
            if str(metadata.get("schema") or "") != single_schema:
                raise employeelearning.InvalidTransitionError(
                    "相同幂等键已用于其他进修模式"
                )
        batch = employeelearning.create_batch(
            request_key,
            budget_cap_points=_LEARNING_BATCH_POINTS_PER_EMPLOYEE,
            tenant_id=int(tenant_id),
        )
        if not existing_batch:
            db.execute(
                "UPDATE employee_learning_batch SET metadata_json=?,max_runs=?,"
                "created_by=?,updated_at=? WHERE id=?",
                (
                    json.dumps({
                        "schema": single_schema,
                        "public_request_key": request_key,
                        "employee_idx": int(idx),
                        "identity_ref": identity_ref,
                        "config_revision": int(config["config_revision"]),
                        "config_sha256": str(config["config_sha256"]),
                        "bundle_sha256": expected_bundle,
                    }, ensure_ascii=False, sort_keys=True),
                    1,
                    int(actor_id) or None,
                    time.time(),
                    int(batch["id"]),
                ),
            )

        existing_run = db.one(
            "SELECT * FROM employee_learning_run "
            "WHERE batch_id=? AND idempotency_key=?",
            (int(batch["id"]), run_key),
        )
        other_run = db.one(
            "SELECT id FROM employee_learning_run WHERE batch_id=? "
            "AND (idempotency_key<>? OR idempotency_key IS NULL) LIMIT 1",
            (int(batch["id"]), run_key),
        )
        if other_run:
            raise employeelearning.InvalidTransitionError(
                "单人进修幂等批次清单已变化"
            )
        run = employeelearning.create_run(
            int(batch["id"]),
            run_key,
            employee_idx=int(idx),
            identity_ref=identity_ref,
            base_config_revision=int(config["config_revision"]),
            base_config_sha256=str(config["config_sha256"]),
            industry_key=str(employee.get("dept_key") or ""),
            budget_points=_LEARNING_BATCH_POINTS_PER_EMPLOYEE,
            high_risk=_learning_high_risk(employee),
            expires_at=time.time() + 7 * 24 * 3600,
        )
        checkpoint = _learning_run_checkpoint(run)
        frozen_matches = (
            int(run.get("employee_idx") or 0) == int(idx)
            and str(run.get("identity_ref") or "") == identity_ref
            and int(
                run.get("base_config_revision")
                or run.get("config_revision") or 0
            ) == int(config["config_revision"])
            and str(run.get("base_config_sha256") or "")
            == str(config["config_sha256"])
            and (
                existing_run is None
                or str(checkpoint.get("expected_bundle_sha256") or "")
                == expected_bundle
            )
        )
        if not frozen_matches:
            raise employeelearning.InvalidTransitionError(
                "相同幂等键不能改变员工或岗位四元绑定"
            )
        if run.get("status") != employeelearning.RUN_QUEUED:
            return {"batch": batch, "run": run, "claimed": False}

        employeelearning.reserve_budget(int(run["id"]))
        try:
            employeelearning.start_run(int(run["id"]), allow_existing=False)
        except employeelearning.InvalidTransitionError:
            return {
                "batch": batch,
                "run": employeelearning.get_run(int(run["id"])),
                "claimed": False,
            }
        run = employeelearning.checkpoint(
            int(run["id"]),
            {
                **checkpoint,
                "stage": "billing_pending",
                "expected_bundle_sha256": expected_bundle,
            },
        )
        return {"batch": batch, "run": run, "claimed": True}


async def _start_single_employee_learning_run(
    *,
    idx: int,
    request_key: str,
    tenant_id: int,
    actor_id: int,
    binding: dict,
) -> dict:
    """Launch one atomically-prepared run and compensate every failed handoff."""
    prepared = await db.arun(
        _prepare_single_employee_learning_run,
        idx=int(idx),
        request_key=request_key,
        tenant_id=int(tenant_id),
        actor_id=int(actor_id),
        binding=binding,
    )
    run = dict(prepared["run"])
    if not prepared["claimed"]:
        return {"started": False, "run": run}

    run_id = int(run["id"])
    op_key = _learning_billing_op_key(int(tenant_id), run_id)
    billing_started = False
    worker_coro = None
    try:
        billing_op = await _run_db_safely(
            _start_learning_billing_at_frozen_price,
            run_id,
            int(tenant_id),
            (
                f"{binding['employee'].get('person') or binding['employee'].get('name') or '数字员工'}"
                "证据进修"
            ),
        )
        billing_started = True
        if billing_op != op_key:
            raise RuntimeError("进修计费操作编号不一致")
        run = await _run_db_safely(
            employeelearning.checkpoint,
            run_id,
            {
                **_learning_run_checkpoint(run),
                "stage": "researching",
                "expected_bundle_sha256": binding["config"]["bundle_sha256"],
                "billing_op_key": billing_op,
            },
        )
        worker_coro = _employee_learning_research_worker(run_id, binding)
        try:
            asyncio.create_task(worker_coro)
        except BaseException:
            worker_coro.close()
            raise
        return {"started": True, "run": run}
    except billing.InsufficientPoints:
        await _run_db_safely(
            employeelearning.defer_run_for_billing,
            run_id,
            reason="INSUFFICIENT_POINTS",
        )
        raise
    except BaseException:
        settled = False
        if billing_started:
            try:
                await _settle_failed_learning_run_safely(
                    run_id,
                    op_key,
                    "员工证据进修未启动自动退回",
                    "START_FAILED",
                )
                settled = True
            except BaseException:
                logging.getLogger("employeelearning").error(
                    "employee learning start refund failed run_id=%s", run_id,
                )
        if not settled:
            try:
                await _terminalize_learning_run_safely(run_id, "START_FAILED")
            except BaseException:
                pass
        raise


@app.post("/api/employees/{idx}/learning-runs")
async def employee_learning_run_create(idx: int, body: dict):
    """Start one V4 employee's evidence proposal; never a bulk campaign."""
    _need_boss()
    binding = _employee_current_write_binding(idx, body)
    employee = binding["employee"]
    if str(employee.get("catalog_version") or "") != departments.DECISION_V4_CATALOG_VERSION:
        raise HTTPException(409, "证据进修仅适用于当前 V4 行业专属员工")
    if not binding["identity"].get("can_learn"):
        raise HTTPException(409, "当前岗位不可进修")
    request_key = str(body.get("request_key") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,190}", request_key) is None:
        raise HTTPException(400, "进修请求幂等键无效")
    tenant_id = TEN()
    actor_id = int((auth.current() or {}).get("id") or 0)
    operation = asyncio.create_task(_start_single_employee_learning_run(
        idx=int(idx),
        request_key=request_key,
        tenant_id=int(tenant_id),
        actor_id=actor_id,
        binding=binding,
    ))
    cancellation = None
    try:
        try:
            result = await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            cancellation = exc
            result = await _drain_task_despite_cancellation(operation)
        if cancellation is not None:
            raise cancellation
    except employeelearning.LearningValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    except billing.InsufficientPoints as exc:
        raise HTTPException(402, str(exc)) from exc
    except (
        employeelearning.BudgetExceededError,
        employeelearning.InvalidTransitionError,
        employeelearning.LearningError,
    ) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "ok": True,
        "started": bool(result["started"]),
        "run": _learning_run_public(result["run"]),
    }


@app.get("/api/employee-learning/runs/{run_id}")
def employee_learning_run_get(run_id: int):
    _need_boss()
    run, _batch = _learning_owned_run(run_id)
    return {"run": _learning_run_public(run)}


def _learning_auto_activate_reviewer_id(run: dict) -> int:
    """Prefer the batch owner; fall back to the named boss account id."""
    try:
        created_by = int(
            (employeelearning.get_batch(int(run["batch_id"])) or {}).get(
                "created_by"
            ) or 0
        )
    except (TypeError, ValueError, employeelearning.LearningError):
        created_by = 0
    if created_by > 0:
        return created_by
    boss = db.one(
        "SELECT id FROM users WHERE username=? AND role=? AND enabled=1 "
        "ORDER BY id LIMIT 1",
        ("boss", "root"),
    )
    if boss and int(boss["id"] or 0) > 0:
        return int(boss["id"])
    return 1


def _auto_activate_delivered_learning_run(run_id: int) -> dict:
    run = employeelearning.get_run(int(run_id))
    return _activate_employee_learning_run(
        int(run_id), _learning_auto_activate_reviewer_id(run),
    )


def _activate_employee_learning_run(run_id: int, reviewer_id: int) -> dict:
    """CAS-activate an evidence-backed proposal; shared by auto and boss approve."""
    run = employeelearning.get_run(int(run_id))
    if str(run.get("status") or "") != employeelearning.RUN_AWAITING_APPROVAL:
        raise employeelearning.InvalidTransitionError(
            f"运行不可审批: {run['status']}"
        )
    try:
        binding = _learning_current_run_binding(run)
    except HTTPException as exc:
        if (
            exc.status_code == 409
            and run.get("status") == employeelearning.RUN_AWAITING_APPROVAL
        ):
            try:
                employeelearning.mark_stale(run_id, reason="IDENTITY_CONFIG_CAS_STALE")
            except employeelearning.LearningError:
                pass
        raise
    _verify_learning_gate_checkpoint(binding["employee"], run)
    relevant_sources = _learning_semantic_source_gate(binding["employee"], run)
    _learning_verify_frozen_artifact_evidence(run, relevant_sources)
    expected_bundle = _learning_run_bundle_sha256(run)

    def activate(**kwargs):
        # ``approve_run`` invokes this callback inside its BEGIN IMMEDIATE.
        # Re-read slot eligibility and the current four-tuple in that same
        # transaction so a disable/config write cannot slip between the API's
        # preflight and the activation CAS.
        active = employeeidentity.active_employee(_learning_run_idx(run))
        current = (
            employees.get_config(_learning_run_idx(run)) if active else None
        )
        identity = (
            _employee_public_contract(active, config=current)
            if active and current else {}
        )
        if (
            not active
            or not current
            or not identity.get("can_learn")
            or employeeidentity.identity_ref(active)
            != str(kwargs.get("expected_identity_ref") or "")
            or str(current.get("identity_ref") or "")
            != str(kwargs.get("expected_identity_ref") or "")
            or int(current.get("config_revision") or 0)
            != int(kwargs.get("expected_config_revision") or 0)
            or str(current.get("config_sha256") or "")
            != str(kwargs.get("expected_config_sha256") or "")
            or str(current.get("bundle_sha256") or "") != expected_bundle
        ):
            raise db.StaleWriteError(
                "employee learning activation eligibility changed"
            )
        return employees.activate_learning_bundle(
            **kwargs, expected_bundle_sha256=expected_bundle,
        )

    return employeelearning.approve_run(
        int(run_id),
        activate,
        reviewer_id=int(reviewer_id),
    )


@app.post("/api/employee-learning/runs/{run_id}/approve")
def employee_learning_run_approve(run_id: int, body: dict):
    """Activate a leftover proposal; live research now auto-activates after evidence."""
    _need_boss()
    run, _batch = _learning_owned_run(run_id)
    # A wrong/stale browser echo is a request conflict, not proof that the
    # authoritative employee changed.  Never mutate the proposal on this
    # first-layer 409.
    _learning_frozen_request_binding(run, body)
    try:
        approved = _activate_employee_learning_run(
            run_id,
            int((auth.current() or {}).get("id") or 0),
        )
    except learningevidence.EvidenceConfigError as exc:
        raise HTTPException(409, "证据门禁配置不可用，不能激活能力版本") from exc
    except learningevidence.EvidenceGateError as exc:
        if exc.code == "EVIDENCE_GATE_DIGEST_DRIFT":
            raise HTTPException(409, "证据门禁版本已变化，请重新发起进修") from exc
        raise HTTPException(409, "来源与该岗位不相关，不能激活能力版本") from exc
    except employeelearning.StaleActivationError as exc:
        raise HTTPException(409, "员工岗位或配置已变更，该进修提案已过期") from exc
    except employeelearning.InvalidTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except employeelearning.LearningValidationError as exc:
        current = employeelearning.get_run(int(run_id))
        if str(current.get("status") or "") == employeelearning.RUN_AWAITING_APPROVAL:
            raise HTTPException(409, "来源与该岗位不相关，不能激活能力版本") from exc
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except db.StaleWriteError as exc:
        try:
            employeelearning.mark_stale(run_id, reason="IDENTITY_CONFIG_CAS_STALE")
        except employeelearning.LearningError:
            pass
        raise HTTPException(409, "员工岗位或配置已变更，该进修提案已过期") from exc
    except RuntimeError as exc:
        # Activation integrity/storage failures are not authoritative evidence
        # of identity drift.  ``approve_run`` rolled its transaction back, so
        # preserve the awaiting proposal and owner for a safe operator retry.
        logging.getLogger("employeelearning").error(
            "employee learning activation failed run_id=%s error_type=%s",
            run_id,
            type(exc).__name__,
        )
        raise HTTPException(503, "进修提案激活暂时失败，请稍后重试") from exc
    return {"ok": True, "run": _learning_run_public(approved)}


@app.post("/api/employee-learning/runs/{run_id}/reject")
def employee_learning_run_reject(run_id: int, body: dict):
    _need_boss()
    run, _batch = _learning_owned_run(run_id)
    # Rejection terminates the exact frozen proposal and never activates a
    # current config, so it intentionally needs no current-server CAS.  This
    # keeps an auditable release path even after the live role has advanced.
    _learning_frozen_request_binding(run, body)
    reason = str(body.get("reason") or "老板拒绝该进修提案").strip()[:200]
    try:
        rejected = employeelearning.reject_run(
            run_id,
            reason=reason,
            reviewer_id=int((auth.current() or {}).get("id") or 0),
        )
    except employeelearning.InvalidTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "run": _learning_run_public(rejected)}


@app.post("/api/employees/{idx}/learn")
async def employee_learn(idx: int, body: dict | None = None):
    _need_boss()   # 进修会覆写全平台共享的员工技能库,只有 boss 能发起
    binding = _employee_current_write_binding(idx, body or {})
    if (
        str(binding["employee"].get("catalog_version") or "")
        == departments.DECISION_V4_CATALOG_VERSION
    ):
        raise HTTPException(
            409,
            "V4 行业专属员工必须使用可核验全网证据进修，请刷新后重试",
        )
    if not binding["identity"]["can_learn"]:
        raise HTTPException(409, "当前岗位不可进修")
    s = registry.BY_IDX.get(idx) or (
        departments.get_active(idx) and departments.learn_station(idx)
    )
    if not s:
        raise HTTPException(404)
    if not employees.claim_learning(idx):
        raise HTTPException(429, "该员工正在进修中")
    try:
        billing_op = await _start_billing_operation_safely(
            billing.start_operation,
            "learn",
            tid=TEN(),
            note=f"{s.get('name', '数字员工')}进修",
            cancel_reason="员工进修请求中断自动退回",
        )
    except billing.InsufficientPoints as e:
        employees.LEARNING.discard(idx)
        raise HTTPException(402, str(e))
    except BaseException:
        employees.LEARNING.discard(idx)
        raise

    tid = TEN()
    emp_name = s.get("name", "数字员工")

    async def _bg():
        from . import notify
        try:
            result = await employees.learn(
                s,
                broadcast=engine.broadcast,
                claimed=True,
                identity_ref=binding["identity"]["identity_ref"],
                expected_revision=binding["config"]["config_revision"],
                expected_config_sha256=binding["config"]["config_sha256"],
            )
        except BaseException as exc:
            try:
                await _run_db_safely(
                    billing.fail_operation,
                    billing_op,
                    "员工进修失败自动退回",
                )
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
                await notify.push_async(
                    tid,
                    "learn_failed",
                    {"title": emp_name},
                )
            except Exception:
                logging.getLogger("employees").warning(
                    "employee %s learn-failed notify failed", idx)
        else:
            fresh = int((result or {}).get("new") or 0)
            if fresh:
                await _run_db_safely(
                    billing.complete_operation,
                    billing_op,
                )
            else:
                # 一条新技能都没学到就不收钱,与全站「没产出就退点」口径一致。
                try:
                    await _run_db_safely(
                        billing.fail_operation,
                        billing_op,
                        "进修未学到新技能自动退回",
                    )
                except Exception:
                    logging.getLogger("employees").error(
                        "employee %s zero-fresh refund failed", idx)
            try:
                await notify.push_async(
                    tid,
                    "learn_done",
                    {
                        "title": emp_name,
                        "new": fresh,
                        "total": (result or {}).get("total"),
                        "summary": (
                            f"「{emp_name}」新学 {fresh} 条技能,技能库共 "
                            f"{(result or {}).get('total', '?')} 条"
                            + ("" if fresh else "(全部与已有重复,3 点已退回)")
                        ),
                    },
                )
            except Exception:
                logging.getLogger("employees").warning(
                    "employee %s learn-done notify failed", idx)
        finally:
            # employees.learn owns normal cleanup; this also protects tests,
            # cancellations before coroutine entry, and future implementation swaps.
            employees.LEARNING.discard(idx)

    try:
        asyncio.create_task(_bg())
    except BaseException:
        employees.LEARNING.discard(idx)
        try:
            await _run_db_safely(
                billing.fail_operation,
                billing_op,
                "员工进修未启动自动退回",
            )
        except Exception as refund_exc:
            logging.getLogger("employees").error(
                "employee %s launch refund failed error_type=%s",
                idx,
                type(refund_exc).__name__,
            )
        raise
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
    row = await db.aone(
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
    row = await db.aone(
        "SELECT tenant_id FROM asset WHERE id=? AND deleted_at IS NULL",
        (aid,),
    )
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
_PURGE_MARKER_PREFIX = "__purge_pending_v1__:"
_PURGED_CONTENT = "[已彻底删除]"

# job 彻底删除保留矩阵（schema50，派生记录显式关联 job_id）：
# - DELETE：job、station_run、asset、knowledge、tv_job、站内通知、受管文件。
# - REDACT+RETAIN：censor_log / publish_log / pub_task 的审计与防重放骨架；
#   标题、正文、报告、日志、账号名、URL、素材路径全部不可逆清空。
# - OPAQUE RETAIN：billing_log 金额/余额/时间，billing_operation 的 op_key/
#   action/status/points，以及 wechat_draft_delivery 的 request_hash/request_key/
#   status/op_key/media_id。它们承担退款、对账和“外部提交不能重复”的幂等职责，
#   只清空 note/error/title/report 等业务内容。
# v50 之前少数标题型审查/账务记录无法可靠区分同租户同标题工单。遇到这种
# 历史歧义必须拒绝硬删并保留全部数据，绝不能为了“删干净”串改另一笔业务。
_JOB_PURGE_RETENTION_MATRIX = {
    "delete": (
        "job", "station_run", "asset", "knowledge", "tv_job",
        "notification", "managed_files",
    ),
    "redact_keep": ("censor_log", "publish_log", "pub_task"),
    "opaque_keep": (
        "billing_log", "billing_operation", "wechat_draft_delivery",
    ),
}


def _is_purge_marker(value) -> bool:
    return str(value or "").startswith(_PURGE_MARKER_PREFIX)


def _new_purge_marker() -> str:
    return _PURGE_MARKER_PREFIX + os.urandom(16).hex()


def _purge_tombstone_key(kind: str, tid: int, rid: int) -> str:
    """无业务正文的幂等墓碑；重复请求可返回成功且不泄露别的租户记录。"""
    return f"purged:v1:{kind}:{int(tid)}:{int(rid)}"


def _purge_tombstone_exists(kind: str, tid: int, rid: int) -> bool:
    return db.get_setting(_purge_tombstone_key(kind, tid, rid)) == "1"


def _record_purge_tombstone(
        connection, kind: str, tid: int, rid: int, now: float) -> None:
    connection.execute(
        "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        "updated_at=excluded.updated_at",
        (_purge_tombstone_key(kind, tid, rid), "1", now),
    )


def _purge_collect_titles(value, out: set[str]) -> None:
    """只收集标题类字段，供 schema49 无 job_id 的派生日志做精确脱敏。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"title", "direction", "report_name"}:
                text = " ".join(str(item or "").split()).strip()
                if text and text != _PURGED_CONTENT:
                    out.add(text[:500])
            elif key == "title_candidates" and isinstance(item, list):
                for candidate in item:
                    text = " ".join(str(candidate or "").split()).strip()
                    if text and text != _PURGED_CONTENT:
                        out.add(text[:500])
            if isinstance(item, (dict, list)):
                _purge_collect_titles(item, out)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                _purge_collect_titles(item, out)


def _purge_json_titles(raw, out: set[str]) -> None:
    _purge_collect_titles(db.jloads(raw, {}) or {}, out)


def _purge_rows_tuple(rows: list[dict], columns: tuple[str, ...]) -> tuple:
    return tuple(
        tuple(row.get(column) for column in columns)
        for row in rows
    )


def _purge_billing_reason_matches(
        reason: str, job_id: int) -> bool:
    """只识别旧版正文中自带工单编号的确定性锚点。"""
    text = str(reason or "")
    markers = (
        f"工单#{int(job_id)}",
        f"工单 #{int(job_id)}",
    )
    return any(
        marker in text and (
            text.endswith(marker)
            or f"{marker}·" in text
            or f"{marker} " in text
        )
        for marker in markers
    )


def _purge_legacy_billing_title_matches(
        reason: str, titles: tuple[str, ...]) -> bool:
    """识别没有 job_id 的旧版标题型流水；只能用于阻断，不能用于改写。"""
    text = str(reason or "")
    suffixes = {
        f" · {title[:size]}"
        for title in titles
        for size in (16, 20)
        if title[:size]
    }
    # 深审与自动复盘把 ``平台·标题[:14]`` / ``《标题[:14]》`` 放在
    # note 尾部，billing_log 会再在前面拼动作标签。仍只按真实格式的精确
    # 尾缀匹配，避免扫描/改写无关流水。
    suffixes.update(
        suffix
        for title in titles
        for suffix in (
            f"·{title[:14]}",
            f"·《{title[:14]}》",
        )
        if title[:14]
    )
    return any(text.endswith(suffix) for suffix in suffixes)


def _purge_billing_note_matches(
        note: str, titles: tuple[str, ...]) -> bool:
    """识别代码实际写入的标题型 note，不对任意正文做模糊包含匹配。"""
    text = str(note or "")
    if not text:
        return False
    exact = {
        title[:size]
        for title in titles
        for size in (14, 16, 20)
        if title[:size]
    }
    if text in exact:
        return True
    suffixes = {
        suffix
        for title in titles
        for suffix in (
            f"·{title[:14]}",
            f"·《{title[:14]}》",
        )
        if title[:14]
    }
    return any(text.endswith(suffix) for suffix in suffixes)


def _purge_redacted_billing_reason(reason: str) -> str:
    text = str(reason or "")
    if " · " in text:
        return text.split(" · ", 1)[0][:120] + f" · {_PURGED_CONTENT}"
    for marker in ("工单 #", "工单#"):
        if marker in text:
            return text.split(marker, 1)[0][:120] + _PURGED_CONTENT
    return _PURGED_CONTENT


def _purge_job_snapshot(connection, tid: int, job_id: int, job_row) -> dict:
    """固定 job 的全部派生内容与幂等锚点；阶段三必须逐项完全相同。"""
    station_rows = [
        dict(row) for row in connection.execute(
            "SELECT id,output_json,review_comment,steps_json "
            "FROM station_run WHERE job_id=? ORDER BY id",
            (job_id,),
        ).fetchall()
    ]
    asset_rows = [
        dict(row) for row in connection.execute(
            "SELECT id,payload_json,meta_json FROM asset "
            "WHERE tenant_id=? AND job_id=? ORDER BY id",
            (tid, job_id),
        ).fetchall()
    ]
    knowledge_rows = [
        dict(row) for row in connection.execute(
            "SELECT id,title,content,tags_json,meta_json FROM knowledge "
            "WHERE tenant_id=? AND job_id=? ORDER BY id",
            (tid, job_id),
        ).fetchall()
    ]
    tv_rows = [
        dict(row) for row in connection.execute(
            "SELECT id,params_json,script,status,billing_status,video_file,"
            "error,steps_json FROM tv_job "
            "WHERE tenant_id=? AND job_id=? ORDER BY id",
            (tid, job_id),
        ).fetchall()
    ]
    delivery_rows = [
        dict(row) for row in connection.execute(
            "SELECT id,request_hash,request_key,title,status,billing_status,"
            "op_key,media_id,publish_log_id,report_json,error "
            "FROM wechat_draft_delivery "
            "WHERE tenant_id=? AND job_id=? ORDER BY id",
            (tid, job_id),
        ).fetchall()
    ]
    delivery_publish_ids = {
        int(row["publish_log_id"])
        for row in delivery_rows
        if row.get("publish_log_id")
    }
    publish_rows = [
        dict(row) for row in connection.execute(
            "SELECT id,platform,title,url,source,retro_json "
            "FROM publish_log WHERE tenant_id=? AND job_id=? ORDER BY id",
            (tid, job_id),
        ).fetchall()
    ]
    if delivery_publish_ids:
        marks = ",".join("?" for _ in delivery_publish_ids)
        known = {int(row["id"]) for row in publish_rows}
        publish_rows.extend(
            dict(row) for row in connection.execute(
                "SELECT id,platform,title,url,source,retro_json "
                f"FROM publish_log WHERE tenant_id=? AND id IN ({marks}) "
                "ORDER BY id",
                (tid, *sorted(delivery_publish_ids)),
            ).fetchall()
            if int(row["id"]) not in known
        )
        publish_rows.sort(key=lambda row: int(row["id"]))
    pub_rows = [
        dict(row) for row in connection.execute(
            "SELECT id,platform,account,payload_json,status,submission_state,"
            "submit_started_at,log,fail_json FROM pub_task "
            "WHERE tenant_id=? AND json_valid(payload_json) "
            "AND CAST(json_extract(payload_json,'$.job_id') AS INTEGER)=? "
            "ORDER BY id",
            (tid, job_id),
        ).fetchall()
    ]

    titles: set[str] = set()
    _purge_json_titles(job_row["brief_json"], titles)
    for row in station_rows:
        _purge_json_titles(row.get("output_json"), titles)
    for row in asset_rows:
        _purge_json_titles(row.get("payload_json"), titles)
    for row in knowledge_rows:
        text = " ".join(str(row.get("title") or "").split()).strip()
        if text:
            titles.add(text[:500])
    for row in tv_rows:
        _purge_json_titles(row.get("params_json"), titles)
    for row in delivery_rows + publish_rows:
        text = " ".join(str(row.get("title") or "").split()).strip()
        if text and text != _PURGED_CONTENT:
            titles.add(text[:500])
    for row in pub_rows:
        _purge_json_titles(row.get("payload_json"), titles)
    title_tuple = tuple(sorted(titles))
    censor_titles = {title[:80] for title in title_tuple if title[:80]}

    censor_rows = [
        dict(row) for row in connection.execute(
            "SELECT id,job_id,kind,platform,title,verdict,score,issues_json,"
            "report FROM censor_log WHERE tenant_id=? AND job_id=? "
            "ORDER BY id",
            (tid, job_id),
        ).fetchall()
    ]
    notification_rows = []
    for row in connection.execute(
            "SELECT id,job_id,kind,title,body,link,user_id FROM notification "
            "WHERE tenant_id=? ORDER BY id", (tid,)).fetchall():
        item = dict(row)
        link = str(item.get("link") or "")
        directly_linked = link in {
            f"#/job/{job_id}", f"#/delivery/{job_id}"
        }
        if item.get("job_id") == job_id or directly_linked:
            notification_rows.append(item)

    billing_rows = [
        dict(row) for row in connection.execute(
            "SELECT id,job_id,delta,balance,reason FROM billing_log "
            "WHERE tenant_id=? ORDER BY id",
            (tid,),
        ).fetchall()
        if (
            row["job_id"] == job_id
            or _purge_billing_reason_matches(row["reason"], job_id)
        )
    ]
    delivery_op_keys = {
        str(row["op_key"])
        for row in delivery_rows
        if row.get("op_key")
    }
    billing_operation_rows = [
        dict(row) for row in connection.execute(
            "SELECT op_key,job_id,action,units,points,note,status,error "
            "FROM billing_operation WHERE tenant_id=? ORDER BY op_key",
            (tid,),
        ).fetchall()
        if (
            row["job_id"] == job_id
            or
            str(row["op_key"]) in delivery_op_keys
        )
    ]

    # 旧版没有 job_id 的标题型记录可能属于同租户另一笔同标题业务。它们
    # 只能触发“无法安全归因”的阻断，绝不纳入脱敏集合。
    unattributed = [
        ("censor_log", int(row["id"]))
        for row in connection.execute(
            "SELECT id,title FROM censor_log "
            "WHERE tenant_id=? AND job_id IS NULL ORDER BY id",
            (tid,),
        ).fetchall()
        if str(row["title"] or "") in censor_titles
    ]
    unattributed.extend(
        ("notification", int(row["id"]))
        for row in connection.execute(
            "SELECT id,title,body,link FROM notification "
            "WHERE tenant_id=? AND job_id IS NULL ORDER BY id",
            (tid,),
        ).fetchall()
        if (
            str(row["link"] or "") not in {
                f"#/job/{job_id}", f"#/delivery/{job_id}"
            }
            and (
                str(row["title"] or "") in title_tuple
                or str(row["body"] or "") in title_tuple
                or any(
                    title[:20]
                    and f"《{title[:20]}》" in str(row["title"] or "")
                    for title in title_tuple
                )
            )
        )
    )
    unattributed.extend(
        ("billing_log", int(row["id"]))
        for row in connection.execute(
            "SELECT id,reason FROM billing_log "
            "WHERE tenant_id=? AND job_id IS NULL ORDER BY id",
            (tid,),
        ).fetchall()
        if (
            not _purge_billing_reason_matches(row["reason"], job_id)
            and _purge_legacy_billing_title_matches(
                row["reason"], title_tuple)
        )
    )
    unattributed.extend(
        ("billing_operation", str(row["op_key"]))
        for row in connection.execute(
            "SELECT op_key,note FROM billing_operation "
            "WHERE tenant_id=? AND job_id IS NULL ORDER BY op_key",
            (tid,),
        ).fetchall()
        if (
            str(row["op_key"]) not in delivery_op_keys
            and _purge_billing_note_matches(row["note"], title_tuple)
        )
    )

    pub_files = []
    for row in pub_rows:
        fail = db.jloads(row.get("fail_json"), {}) or {}
        shot = fail.get("shot") if isinstance(fail, dict) else None
        # 只有 /files/ 下的地址可能是本服务管理的本地截图。历史外链也要随
        # fail_json 脱敏，但不能因为外部 URL 不可删除而阻断彻底删除。
        if isinstance(shot, str) and shot.strip().startswith("/files/"):
            pub_files.append((int(row["id"]), str(shot)))

    active = []
    active.extend(
        ("tv_job", int(row["id"]))
        for row in tv_rows
        if (
            row.get("status") in {"pending_charge", "queued", "running"}
            or row.get("billing_status") in {"pending", "charged"}
        )
    )
    active.extend(
        ("wechat_draft_delivery", int(row["id"]))
        for row in delivery_rows
        if (
            row.get("status") in {
                "pending_charge", "processing", "submitting", "submitted"
            }
            or row.get("billing_status") in {"pending", "charged"}
        )
    )
    active.extend(
        ("pub_task", int(row["id"]))
        for row in pub_rows
        if row.get("status") in {"queued", "running"}
    )
    for row in publish_rows:
        retro = db.jloads(row.get("retro_json"), {}) or {}
        if any(
            isinstance(state, dict) and state.get("state") == "processing"
            for state in retro.values()
        ):
            active.append(("publish_log", int(row["id"])))
    active.extend(
        ("billing_operation", str(row["op_key"]))
        for row in billing_operation_rows
        if row.get("status") in {"pending", "charged"}
    )

    return {
        "titles": title_tuple,
        "station": _purge_rows_tuple(
            station_rows, ("id", "output_json", "review_comment", "steps_json")),
        "asset": _purge_rows_tuple(
            asset_rows, ("id", "payload_json", "meta_json")),
        "knowledge": _purge_rows_tuple(
            knowledge_rows,
            ("id", "title", "content", "tags_json", "meta_json"),
        ),
        "tv": _purge_rows_tuple(
            tv_rows,
            (
                "id", "params_json", "script", "status", "billing_status",
                "video_file", "error", "steps_json",
            ),
        ),
        "delivery": _purge_rows_tuple(
            delivery_rows,
            (
                "id", "request_hash", "request_key", "title", "status",
                "billing_status", "op_key", "media_id", "publish_log_id",
                "report_json", "error",
            ),
        ),
        "publish": _purge_rows_tuple(
            publish_rows,
            ("id", "platform", "title", "url", "source", "retro_json"),
        ),
        "pub": _purge_rows_tuple(
            pub_rows,
            (
                "id", "platform", "account", "payload_json", "status",
                "submission_state", "submit_started_at", "log", "fail_json",
            ),
        ),
        "censor": _purge_rows_tuple(
            censor_rows,
            (
                "id", "job_id", "kind", "platform", "title", "verdict", "score",
                "issues_json", "report",
            ),
        ),
        "notification": _purge_rows_tuple(
            notification_rows,
            ("id", "job_id", "kind", "title", "body", "link", "user_id"),
        ),
        "billing_log": _purge_rows_tuple(
            billing_rows, ("id", "job_id", "delta", "balance", "reason")),
        "billing_operation": _purge_rows_tuple(
            billing_operation_rows,
            (
                "op_key", "job_id", "action", "units", "points", "note",
                "status", "error",
            ),
        ),
        "tv_files": tuple(
            (int(row["id"]), row.get("video_file"))
            for row in tv_rows
            if row.get("video_file")
        ),
        "pub_files": tuple(sorted(pub_files)),
        "active": tuple(active),
        "unattributed": tuple(unattributed),
    }


def _trash_module(kind: str, row: dict) -> str:
    if kind == "task":
        if int(row.get("emp_idx") or 0) == inspection.EMPLOYEE_IDX:
            scoped = db.one(
                "SELECT industry_key FROM inspection_visit WHERE task_id=? "
                "AND tenant_id=? AND deleted_at IS NULL",
                (int(row.get("id") or 0), TEN()),
            )
            # 孤儿巡店任务 fail closed，不降级为 content 泄露。
            return str((scoped or {}).get("industry_key") or "__denied__")
        employee = employeeidentity.resolve_task(row)
        if not employee:
            return "__denied__"
        return str(row.get("employee_dept_key") or "__denied__")
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
                 NULL AS employee_key,
                 NULL AS employee_catalog_version,
                 NULL AS employee_name_snapshot,
                 NULL AS employee_dept_key,
                 NULL AS employee_spec_sha256,
                 deleted_at,created_at,delete_reason
          FROM job WHERE tenant_id=? AND deleted_at IS NOT NULL
          UNION ALL
          SELECT 'task' AS kind,id,brief_json,NULL AS title,
                 NULL AS params_json,status,emp_idx,
                 employee_key,employee_catalog_version,
                 employee_name_snapshot,employee_dept_key,employee_spec_sha256,
                 deleted_at,created_at,delete_reason
          FROM task WHERE tenant_id=? AND deleted_at IS NOT NULL
          UNION ALL
          SELECT 'knowledge' AS kind,id,NULL AS brief_json,title,
                 NULL AS params_json,'' AS status,NULL AS emp_idx,
                 NULL AS employee_key,
                 NULL AS employee_catalog_version,
                 NULL AS employee_name_snapshot,
                 NULL AS employee_dept_key,
                 NULL AS employee_spec_sha256,
                 deleted_at,created_at,delete_reason
          FROM knowledge WHERE tenant_id=? AND deleted_at IS NOT NULL
          UNION ALL
          SELECT 'avatar' AS kind,id,NULL AS brief_json,NULL AS title,
                 params_json,status,NULL AS emp_idx,
                 NULL AS employee_key,
                 NULL AS employee_catalog_version,
                 NULL AS employee_name_snapshot,
                 NULL AS employee_dept_key,
                 NULL AS employee_spec_sha256,
                 deleted_at,created_at,delete_reason
          FROM avatar_job WHERE tenant_id=? AND deleted_at IS NOT NULL
          UNION ALL
          SELECT 'profile' AS kind,id,NULL AS brief_json,name AS title,
                 NULL AS params_json,'' AS status,NULL AS emp_idx,
                 NULL AS employee_key,
                 NULL AS employee_catalog_version,
                 NULL AS employee_name_snapshot,
                 NULL AS employee_dept_key,
                 NULL AS employee_spec_sha256,
                 deleted_at,created_at,delete_reason
          FROM account_profile WHERE tenant_id=? AND deleted_at IS NOT NULL
          UNION ALL
          SELECT 'asset' AS kind,id,NULL AS brief_json,NULL AS title,
                 payload_json AS params_json,'' AS status,NULL AS emp_idx,
                 NULL AS employee_key,
                 NULL AS employee_catalog_version,
                 NULL AS employee_name_snapshot,
                 NULL AS employee_dept_key,
                 NULL AS employee_spec_sha256,
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
        if module == "__denied__" or not auth.allowed(module):
            continue
        items.append(
            {
                "kind": kind,
                "id": row["id"],
                "title": _trash_title(kind, row),
                "status": row.get("status") or "",
                "assignee": (
                    str(row.get("employee_name_snapshot") or "")
                    if kind == "task" else ""
                ),
                "deleted_at": row.get("deleted_at") or 0,
                "created_at": row.get("created_at") or 0,
                "reason": (
                    "彻底删除尚未完成，请再次点击彻底删除继续处理"
                    if _is_purge_marker(row.get("delete_reason"))
                    else (row.get("delete_reason") or "")[:160]
                ),
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
    module = _trash_module(kind, row) if row else "__denied__"
    if not row or module == "__denied__" or not auth.allowed(module):
        raise HTTPException(404)
    try:
        with db.atomic() as connection:
            current = connection.execute(
                f"SELECT * FROM {table} WHERE id=? AND tenant_id=? "
                "AND deleted_at IS NOT NULL",
                (rid, TEN()),
            ).fetchone()
            if not current:
                raise HTTPException(
                    409, "记录状态刚刚发生变化，请刷新后再恢复"
                )
            if _is_purge_marker(current["delete_reason"]):
                raise HTTPException(
                    409,
                    "该记录正在等待彻底删除，部分文件可能已经销毁，不能恢复；"
                    "请再次点击彻底删除完成清理",
                )
            if kind == "task":
                # 使用与协作会话共享的恢复守卫；嵌套 savepoint 与
                # 上面的 marker 检查处于同一写事务。
                taskthreads.restore_task(rid, TEN())
                changed = 1
            else:
                changed = connection.execute(
                    f"UPDATE {table} SET deleted_at=NULL,deleted_by=NULL,"
                    "delete_reason=NULL,updated_at=? "
                    "WHERE id=? AND tenant_id=? AND deleted_at IS NOT NULL",
                    (time.time(), rid, TEN()),
                ).rowcount
    except taskthreads.TaskThreadError as exc:
        _raise_task_thread_error(exc)
    except sqlite3.IntegrityError as exc:
        # 已有新的定时工单/会议行动占用同一幂等键时，不能破坏唯一性。
        raise HTTPException(
            409, "该记录的来源位置已生成新任务，暂不能直接恢复"
        ) from exc
    if changed != 1:
        raise HTTPException(409, "记录状态刚刚发生变化，请刷新后再恢复")
    if kind == "task":
        taskrunner.sync_meeting_delivery_for_task(rid)
        module = _trash_module(kind, row)
        engine.broadcast(
            {
                "type": "task_update",
                "tenant_id": TEN(),
                "_required_modules": (
                    module,
                ),
                "task_id": rid,
                "idx": row.get("emp_idx"),
            }
        )
    elif kind == "job":
        engine.touch(rid, TEN())
    elif kind == "avatar":
        engine.broadcast({
            "type": "avatar_update",
            "tenant_id": TEN(),
            "job_id": rid,
        })
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
    返回 (成功删除的文件数, 删除失败的文件数)。调用方必须在失败数为 0
    之后才能硬删数据库锚点。
    """
    removed = failed = 0
    root = os.path.realpath(assetfiles.ASSET_ROOT)
    if kind == "job":
        job_dir = os.path.join(root, f"job{rid}")
        if os.path.lexists(job_dir):
            if (
                os.path.islink(job_dir)
                or not os.path.isdir(job_dir)
                or os.path.realpath(job_dir) != job_dir
            ):
                return 0, 1
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
            root, "avatar", f"avatar_{rid}.mp4")
        if os.path.lexists(clip):
            if (
                os.path.islink(clip)
                or not os.path.isfile(clip)
                or os.path.realpath(clip) != clip
            ):
                return 0, 1
            try:
                os.remove(clip)
                removed += 1
            except OSError:
                failed += 1
    return removed, failed


def _purge_tv_files(file_urls: list[str], tid: int, job_id: int) -> tuple[int, int]:
    """销毁工单关联成片；只接受数据库可证明属于该租户与工单的本地路径。"""
    removed = failed = 0
    root = os.path.realpath(assetfiles.ASSET_ROOT)
    for file_url in file_urls:
        try:
            canonical = assetfiles.canonical_file_url(file_url)
            if (
                assetfiles.file_owner_tid(canonical) != int(tid)
                or assetfiles.file_job_id(canonical) != int(job_id)
            ):
                raise assetfiles.AssetAccessError("asset ownership mismatch")
            lexical = os.path.abspath(
                os.path.join(root, canonical.removeprefix("/files/"))
            )
            resolved = os.path.realpath(lexical)
            if (
                os.path.commonpath((root, resolved)) != root
                or lexical != resolved
            ):
                # 不跟随软链接删除其目标，也不把软链接本身当成已销毁交付物。
                raise assetfiles.AssetAccessError("asset path is unsafe")
        except (assetfiles.AssetAccessError, TypeError, ValueError):
            failed += 1
            continue
        if not os.path.lexists(lexical):
            continue
        if not os.path.isfile(lexical):
            failed += 1
            continue
        try:
            os.remove(lexical)
            removed += 1
        except OSError:
            failed += 1
    return removed, failed


def _purge_pub_files(items: list[tuple[int, str]]) -> tuple[int, int]:
    """销毁关联发布任务的失败截图，不接受 payload 或客户端提供的任意路径。

    pub_task 没有独立文件所有权目录；唯一由代码生成的受管截图是
    ``/files/pub/fail_{pub_task.id}.png``。必须同时满足精确文件名、位于
    ASSET_ROOT、不是软链接，才允许删除。
    """
    removed = failed = 0
    root = os.path.realpath(assetfiles.ASSET_ROOT)
    for pub_id, file_url in items:
        try:
            canonical = assetfiles.canonical_file_url(file_url)
            expected = f"/files/pub/fail_{int(pub_id)}.png"
            if canonical != expected:
                raise assetfiles.AssetAccessError("publish screenshot mismatch")
            lexical = os.path.abspath(
                os.path.join(root, canonical.removeprefix("/files/"))
            )
            resolved = os.path.realpath(lexical)
            if (
                os.path.commonpath((root, resolved)) != root
                or lexical != resolved
            ):
                raise assetfiles.AssetAccessError("publish screenshot unsafe")
        except (assetfiles.AssetAccessError, TypeError, ValueError):
            failed += 1
            continue
        if not os.path.lexists(lexical):
            continue
        if not os.path.isfile(lexical):
            failed += 1
            continue
        try:
            os.remove(lexical)
            removed += 1
        except OSError:
            failed += 1
    return removed, failed


def _purge_ids(snapshot: dict, key: str) -> tuple:
    return tuple(row[0] for row in snapshot.get(key, ()))


def _purge_job_relations(
        connection, tid: int, job_id: int, snapshot: dict, now: float) -> None:
    """按保留矩阵删除正文表、脱敏审计表，保留退款与防重放锚点。"""
    asset_ids = _purge_ids(snapshot, "asset")
    knowledge_ids = _purge_ids(snapshot, "knowledge")
    tv_ids = _purge_ids(snapshot, "tv")
    notification_ids = _purge_ids(snapshot, "notification")
    censor_ids = _purge_ids(snapshot, "censor")
    publish_ids = _purge_ids(snapshot, "publish")
    pub_ids = _purge_ids(snapshot, "pub")
    delivery_ids = _purge_ids(snapshot, "delivery")

    def delete_ids(table: str, ids: tuple, tenant_scoped: bool = True):
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        tenant_sql = " AND tenant_id=?" if tenant_scoped else ""
        params = (*ids, tid) if tenant_scoped else ids
        connection.execute(
            f"DELETE FROM {table} WHERE id IN ({marks}){tenant_sql}",
            params,
        )

    # 正文、brief、输出、素材引用所在的派生行必须物理删除。
    connection.execute("DELETE FROM station_run WHERE job_id=?", (job_id,))
    delete_ids("asset", asset_ids)
    delete_ids("knowledge", knowledge_ids)
    delete_ids("tv_job", tv_ids)
    delete_ids("notification", notification_ids)

    # 审核与发布骨架留作统计/防重放，但不保留任何可恢复的业务内容。
    if censor_ids:
        marks = ",".join("?" for _ in censor_ids)
        connection.execute(
            "UPDATE censor_log SET title=?,issues_json='[]',report='',"
            f"updated_at=? WHERE tenant_id=? AND id IN ({marks})",
            (_PURGED_CONTENT, now, tid, *censor_ids),
        )
    if publish_ids:
        marks = ",".join("?" for _ in publish_ids)
        connection.execute(
            "UPDATE publish_log SET title=?,url='',retro_json='{}',"
            f"updated_at=? WHERE tenant_id=? AND id IN ({marks})",
            (_PURGED_CONTENT, now, tid, *publish_ids),
        )
    if pub_ids:
        marks = ",".join("?" for _ in pub_ids)
        anchor_payload = json.dumps(
            {"job_id": int(job_id), "purged": True},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        connection.execute(
            "UPDATE pub_task SET account=NULL,payload_json=?,log='',"
            f"fail_json='{{}}',updated_at=? "
            f"WHERE tenant_id=? AND id IN ({marks})",
            (anchor_payload, now, tid, *pub_ids),
        )
    if delivery_ids:
        marks = ",".join("?" for _ in delivery_ids)
        connection.execute(
            "UPDATE wechat_draft_delivery SET title=?,report_json=NULL,"
            f"error=NULL,updated_at=? WHERE tenant_id=? AND id IN ({marks})",
            (_PURGED_CONTENT, now, tid, *delivery_ids),
        )

    # 流水金额、余额、动作、状态与幂等键完整保留，只去掉标题/错误正文。
    for billing_id, _job_id, _delta, _balance, reason in snapshot.get(
            "billing_log", ()):
        connection.execute(
            "UPDATE billing_log SET reason=?,updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (
                _purge_redacted_billing_reason(reason),
                now,
                billing_id,
                tid,
            ),
        )
    for (
            op_key, _job_id, _action, _units, _points, _note, _status,
            _error) in (
            snapshot.get("billing_operation", ())):
        connection.execute(
            "UPDATE billing_operation SET note=?,error=NULL,updated_at=? "
            "WHERE op_key=? AND tenant_id=?",
            (_PURGED_CONTENT, now, op_key, tid),
        )


@app.post("/api/trash/{kind}/{rid}/purge")
def trash_purge(kind: str, rid: int):
    """合规出路:彻底删除回收站记录并连带销毁交付文件。

    账目和防重复投递锚点必须留存；job 的业务正文和派生载荷按上方矩阵
    物理删除或不可逆脱敏。
    """
    _need_admin()
    meta = _TRASH_TABLES.get(kind)
    if not meta or rid < 1:
        raise HTTPException(404)
    tid = TEN()
    table, _ = meta
    row = db.one(
        f"SELECT * FROM {table} WHERE id=? AND tenant_id=? "
        "AND deleted_at IS NOT NULL",
        (rid, tid),
    )
    if not row:
        if _purge_tombstone_exists(kind, tid, rid):
            return {
                "ok": True,
                "purged": True,
                "kind": kind,
                "id": rid,
                "files_removed": 0,
                "files_failed": 0,
            }
        raise HTTPException(404)
    module = _trash_module(kind, row)
    if module == "__denied__" or not auth.allowed(module):
        raise HTTPException(404)
    if kind == "task":
        guard = taskthreads.task_hard_delete_guard(
            rid, tid, include_deleted=True
        )
        if not guard.get("allowed"):
            raise HTTPException(
                409,
                guard.get("message")
                or "该任务属于持续协作版本链，不能彻底删除",
            )
    active = _TRASH_ACTIVE_STATUS.get(kind, ())
    if (row.get("status") or "") in active:
        raise HTTPException(409, "该记录仍在进行中，请先等它收口再彻底删除")

    marker = _new_purge_marker()
    job_snapshot = None
    # 阶段一只持有一个短写事务：重验权限/状态、固定关联快照并用随机 marker
    # 认领软删行。随后立刻释放 SQLite 写锁。
    with db.atomic() as connection:
        current = connection.execute(
            f"SELECT * FROM {table} WHERE id=? AND tenant_id=? "
            "AND deleted_at IS NOT NULL",
            (rid, tid),
        ).fetchone()
        if not current:
            raise HTTPException(409, "记录状态刚刚发生变化，请刷新后再删除")
        current_row = dict(current)
        current_module = _trash_module(kind, current_row)
        if current_module == "__denied__" or not auth.allowed(current_module):
            raise HTTPException(404)
        if kind == "task":
            guard = taskthreads.task_hard_delete_guard(
                rid,
                tid,
                include_deleted=True,
                connection=connection,
            )
            if not guard.get("allowed"):
                raise HTTPException(
                    409,
                    guard.get("message")
                    or "该任务属于持续协作版本链，不能彻底删除",
                )
        if active and (current["status"] or "") in active:
            raise HTTPException(409, "该记录仍在进行中，请先等它收口再彻底删除")
        if kind == "job":
            job_snapshot = _purge_job_snapshot(
                connection, tid, rid, current
            )
            if job_snapshot["unattributed"]:
                raise HTTPException(
                    409,
                    "发现升级前留下且无法安全归属到具体工单的审查或账务记录。"
                    "为避免误删同标题任务，系统已停止彻底删除；请联系平台完成"
                    "历史记录归因后再试",
                )
            if job_snapshot["active"]:
                raise HTTPException(
                    409,
                    "该工单仍有成片、发布、公众号投递或计费操作在执行/对账，"
                    "请等待它们收口后再彻底删除",
                )
        changed = connection.execute(
            f"UPDATE {table} SET delete_reason=?,updated_at=? "
            "WHERE id=? AND tenant_id=? AND deleted_at IS NOT NULL",
            (marker, time.time(), rid, tid),
        ).rowcount
        if changed != 1:
            raise HTTPException(409, "记录状态刚刚发生变化，请刷新后再删除")

    # 阶段二在事务外做可能很慢的磁盘 I/O；删除失败时 marker 和数据库锚点
    # 保持在位，下一次点击会重新认领并安全重试。
    files_removed, files_failed = _purge_local_files(kind, rid)
    tv_files = (
        [path for _, path in job_snapshot["tv_files"] if path]
        if job_snapshot else []
    )
    tv_removed, tv_failed = _purge_tv_files(tv_files, tid, rid)
    files_removed += tv_removed
    files_failed += tv_failed
    pub_removed = pub_failed = 0
    if job_snapshot:
        pub_removed, pub_failed = _purge_pub_files(
            list(job_snapshot["pub_files"])
        )
        files_removed += pub_removed
        files_failed += pub_failed
    if files_failed:
        raise HTTPException(
            409,
            f"仍有 {files_failed} 个交付文件未能销毁，记录已锁定并保留；"
            "修复存储权限后请再次点击彻底删除，当前不能恢复以免找回残缺内容",
        )

    # 阶段三再次短事务：marker、租户、状态和全部派生关系都必须与阶段一
    # 一致。关联若在 I/O 期间变化就保留锚点，下次重试会纳入新关联。
    with db.atomic() as connection:
        current = connection.execute(
            f"SELECT * FROM {table} WHERE id=? AND tenant_id=? "
            "AND deleted_at IS NOT NULL",
            (rid, tid),
        ).fetchone()
        if not current or current["delete_reason"] != marker:
            raise HTTPException(
                409, "另一条彻底删除请求已接管该记录，请刷新回收站确认结果"
            )
        if active and (current["status"] or "") in active:
            raise HTTPException(409, "记录状态发生变化，已保留删除锚点")
        if kind == "job":
            current_snapshot = _purge_job_snapshot(
                connection, tid, rid, current
            )
            if current_snapshot != job_snapshot:
                raise HTTPException(
                    409,
                    "清理期间新增或变更了关联交付文件/业务记录，记录已保留；"
                    "请再次点击彻底删除",
                )
            if current_snapshot["active"]:
                raise HTTPException(
                    409, "关联任务状态发生变化，已保留删除锚点"
                )
        guard = ""
        args = [rid, tid, marker]
        if active:
            guard = " AND status NOT IN (%s)" % ",".join("?" * len(active))
            args.extend(active)
        if kind == "job":
            _purge_job_relations(
                connection, tid, rid, job_snapshot, time.time()
            )
        changed = connection.execute(
            f"DELETE FROM {table} WHERE id=? AND tenant_id=? "
            f"AND deleted_at IS NOT NULL AND delete_reason=?{guard}",
            args,
        ).rowcount
        if changed != 1:
            raise HTTPException(409, "记录状态刚刚发生变化，请刷新后再删除")
        _record_purge_tombstone(
            connection, kind, tid, rid, time.time()
        )
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
            "total_fields": len(_COMPANY_FIELDS),
            "has_prev": bool(db.get_setting(f"company_profile_prev:{tid}"))}


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
    materials, kb = await asyncio.gather(
        db.aget_setting(f"company_materials:{tid}"),
        db.aq(
            "SELECT title, content FROM knowledge WHERE tenant_id=? "
            "AND deleted_at IS NULL "
            "ORDER BY pinned DESC, id DESC LIMIT 20",
            (tid,),
        ),
    )
    materials = materials or ""
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
    # 覆盖前留一份上一版:提炼会重写老板手工调校的字段,必须有后悔药
    previous = await db.aget_setting(f"company_profile:{tid}")
    if previous:
        await db.aset_setting(f"company_profile_prev:{tid}", previous)
    await db.aset_setting(
        f"company_profile:{tid}", json.dumps(prof, ensure_ascii=False)
    )
    return {"profile": prof, "cost_usd": r.get("cost_usd", 0),
            "can_undo": bool(previous)}


@app.post("/api/company/restore-prev")
def company_restore_prev():
    """一键撤销上次提炼:换回覆盖前的那一版档案(仅保留一步)。"""
    _need_admin()
    tid = TEN()
    previous = db.get_setting(f"company_profile_prev:{tid}")
    if not previous:
        raise HTTPException(404, "没有可撤销的版本(只保留最近一次提炼前的档案)")
    current = db.get_setting(f"company_profile:{tid}")
    db.set_setting(f"company_profile:{tid}", previous)
    # 两版互换:撤销之后还能"撤销撤销"
    db.set_setting(f"company_profile_prev:{tid}", current)
    return {"ok": True,
            "profile": db.jloads(previous, {}) or {}}


# ---------------- V51:区域经理巡店 ----------------
def _raise_inspection_error(exc: inspection.InspectionError):
    if isinstance(exc, inspection.InspectionForbidden):
        raise HTTPException(403, str(exc)) from exc
    if isinstance(exc, inspection.InspectionNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, inspection.InspectionConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def _inspection_actor_id() -> int:
    uid = int((auth.current() or {}).get("id") or 0)
    if uid < 1:
        raise HTTPException(401, "请先登录")
    return uid


def _inspection_scope(industry_key: str | None = None) -> tuple[str, list[dict]]:
    current = auth.current() or {}
    role = str(current.get("role") or "")
    if role not in {"root", "owner", "member"}:
        raise inspection.InspectionForbidden("当前账号角色不允许使用巡店能力")
    if role == "root" and int(current.get("tenant_id") or 0) != 1:
        raise inspection.InspectionForbidden("平台管理员账号归属无效")
    catalog = {
        str(item.get("key") or ""): item
        for item in departments.list_depts()
        if str(item.get("key") or "")
    }
    rows = db.q(
        "SELECT industry_key,is_primary FROM tenant_industry WHERE tenant_id=? "
        "ORDER BY is_primary DESC,industry_key",
        (TEN(),),
    )
    choices = [
        {
            "key": row["industry_key"],
            "name": str(catalog[row["industry_key"]].get("name") or row["industry_key"]),
            "emoji": str(catalog[row["industry_key"]].get("emoji") or ""),
            "is_primary": bool(row.get("is_primary")),
        }
        for row in rows
        if row.get("industry_key") in catalog
    ]
    if role == "member":
        # 企业可经营多个行业，但成员只能进入自己被明确分配的行业。
        # 默认项也必须从这个子集选择，不能先选企业主行业再靠下游 403。
        member_modules = {
            str(item).strip()
            for item in (current.get("modules") or [])
            if str(item).strip()
        }
        choices = [
            item for item in choices if item["key"] in member_modules
        ]
    if not choices:
        raise inspection.InspectionForbidden("当前账号尚未授权可巡店行业")
    selected = str(industry_key or "").strip() or choices[0]["key"]
    if selected not in {item["key"] for item in choices}:
        raise inspection.InspectionForbidden("企业未授权该行业")
    return selected, choices


def _inspection_manager_scope(
    industry_key: str | None = None,
) -> tuple[str, list[dict]]:
    """批量主数据可改写门店、店长 PII 与经营数据，仅主账号可用。"""
    selected, choices = _inspection_scope(industry_key)
    inspection._actor(
        TEN(), _inspection_actor_id(), selected, manager=True
    )
    return selected, choices


_IMPORT_NOT_FOUND_CODES = {"IMPORT_NOT_FOUND", "BRANCH_NOT_FOUND"}
_IMPORT_CONFLICT_CODES = {
    "REQUEST_KEY_CONFLICT",
    "IMPORT_HAS_ERRORS",
    "IMPORT_STATE_CONFLICT",
    "IMPORT_SOURCE_ACTIVE",
    "IMPORT_PREVIEW_EXPIRED",
}
_IMPORT_RATE_LIMIT_CODES = {"IMPORT_PREVIEW_QUOTA_EXCEEDED"}


def _raise_inspection_import_error(exc: inspectionimport.ImportContractError):
    if exc.code == "SCOPE_FORBIDDEN":
        status = 403
    elif exc.code in _IMPORT_NOT_FOUND_CODES:
        status = 404
    elif exc.code in _IMPORT_CONFLICT_CODES:
        status = 409
    elif exc.code in _IMPORT_RATE_LIMIT_CODES:
        status = 429
    else:
        status = 400
    raise HTTPException(
        status,
        exc.safe_message,
        headers={"X-Paihuo-Error-Code": exc.code},
    ) from exc


def _raise_inspection_override_error(
    exc: inspectionoverrides.InspectionOverrideError,
):
    if exc.code == "OVERRIDE_FORBIDDEN":
        status = 403
    elif exc.code == "OVERRIDE_NOT_FOUND":
        status = 404
    elif exc.code in {"OVERRIDE_CONFLICT", "OVERRIDE_STATE_INVALID"}:
        status = 409
    else:
        status = 400
    raise HTTPException(
        status,
        exc.safe_message,
        headers={"X-Paihuo-Error-Code": exc.code},
    ) from exc


def _inspection_search_text(value, *, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise inspection.InspectionError(f"{field}格式无效")
    clean = value.strip()
    if len(clean) > limit or any(ord(char) < 32 for char in clean):
        raise inspection.InspectionError(f"{field}格式无效")
    return clean


def _inspection_branch_search_db(
    tid: int,
    uid: int,
    industry_key: str,
    *,
    q: str = "",
    region: str = "",
    limit: int = 20,
    before_id: int | None = None,
) -> dict:
    """服务端权威 tenant + actor + industry 作用域的有界门店搜索。"""
    inspection._actor(int(tid), int(uid), industry_key)
    clean_q = _inspection_search_text(q, field="门店搜索词", limit=80)
    clean_region = _inspection_search_text(region, field="门店区域", limit=60)
    if isinstance(limit, bool):
        raise inspection.InspectionError("门店分页条数无效")
    try:
        page_size = int(limit)
    except (TypeError, ValueError):
        raise inspection.InspectionError("门店分页条数无效") from None
    if not 1 <= page_size <= 50:
        raise inspection.InspectionError("门店分页条数必须在 1-50 之间")
    cursor = None
    if before_id is not None:
        if isinstance(before_id, bool):
            raise inspection.InspectionError("门店分页游标无效")
        try:
            cursor = int(before_id)
        except (TypeError, ValueError):
            raise inspection.InspectionError("门店分页游标无效") from None
        if cursor < 1:
            raise inspection.InspectionError("门店分页游标无效")

    conditions = ["tenant_id=?", "industry_key=?", "active=1"]
    params: list = [int(tid), industry_key]

    def like(value: str) -> str:
        return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    if clean_q:
        conditions.append(
            "(COALESCE(store_code,'') LIKE ? ESCAPE '\\' "
            "OR name LIKE ? ESCAPE '\\')"
        )
        pattern = like(clean_q)
        params.extend((pattern, pattern))
    if clean_region:
        conditions.append("region LIKE ? ESCAPE '\\'")
        params.append(like(clean_region))
    if cursor is not None:
        conditions.append("id<?")
        params.append(cursor)
    rows = db.q(
        "SELECT id,industry_key,store_code,name,region,address,active "
        "FROM store_branch WHERE "
        + " AND ".join(conditions)
        + " ORDER BY id DESC LIMIT ?",
        (*params, page_size + 1),
    )
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    items = [{
        "id": int(row["id"]),
        "industry_key": str(row["industry_key"]),
        "store_code": str(row.get("store_code") or ""),
        "name": str(row.get("name") or ""),
        "region": str(row.get("region") or ""),
        "address": str(row.get("address") or ""),
        "active": bool(row.get("active")),
    } for row in rows]
    return {
        "items": items,
        "next_before_id": items[-1]["id"] if has_more and items else None,
        "limit": page_size,
    }


def _inspection_checklist_db(
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
) -> dict:
    inspection._actor(int(tid), int(uid), industry_key)
    branch = inspection._branch_scope(int(tid), industry_key, int(branch_id))
    try:
        snapshot = inspectionoverrides.effective_snapshot(
            int(tid), int(uid), industry_key, int(branch["id"]),
        )
        items = snapshot["items"]
        slots = snapshot["capture_slots"]
        registry = inspectionstandards.source_registry()
    except (
        inspectionstandards.InspectionStandardError,
        inspectionoverrides.InspectionOverrideError,
    ) as exc:
        raise inspection.InspectionError("当前行业巡店标准不可用") from exc
    try:
        comparison = inspectionimport.business_comparison(
            int(tid), industry_key, int(branch["id"])
        )
    except inspectionimport.ImportContractError:
        raise
    source_codes = sorted({
        str(item.get("source_no") or "") for item in items
        if str(item.get("source_no") or "") in registry
    })
    return {
        "industry_key": industry_key,
        "branch_id": int(branch["id"]),
        "branch": {
            "id": int(branch["id"]),
            "name": str(branch.get("name") or ""),
            "region": str(branch.get("region") or ""),
        },
        "catalog_version": snapshot["base_catalog_version"],
        "template_version": snapshot["template_version"],
        "as_of": snapshot["as_of"],
        "catalog_sha256": snapshot["catalog_sha256"],
        "base_catalog_sha256": snapshot["base_catalog_sha256"],
        "override_summary": snapshot["override_summary"],
        "items": items,
        "capture_slots": slots,
        "sources": {code: registry[code] for code in source_codes},
        "metrics": comparison["metrics"],
        "business_comparison": comparison,
    }


def _assert_inspection_http_replay_contract(
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
    visit_id: int,
    raw: dict,
    prepared: list[dict],
) -> None:
    """Reject request-key reuse when any persisted HTTP input has changed."""
    inspection._actor(int(tid), int(uid), industry_key)
    inspection._branch_scope(int(tid), industry_key, int(branch_id))
    row = db.one(
        "SELECT request_key,industry_key,branch_id,visit_at,template_key,"
        "template_version,template_snapshot_json,observations_json "
        "FROM inspection_visit WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (int(visit_id), int(tid)),
    )
    if not row:
        raise inspection.InspectionNotFound("巡店记录不存在")
    event = db.one(
        "SELECT payload_json FROM inspection_event WHERE tenant_id=? AND visit_id=? "
        "AND kind='visit_created' ORDER BY id LIMIT 1",
        (int(tid), int(visit_id)),
    )
    snapshot = db.jloads(row.get("template_snapshot_json"), None)
    request = inspection.normalize_visit_input(
        raw,
        industry_key=industry_key,
        standard_snapshot=snapshot if isinstance(snapshot, dict) else None,
    )
    stored_observations = db.jloads(row.get("observations_json"), None)
    created_payload = db.jloads((event or {}).get("payload_json"), None)
    mismatch = (
        str(row.get("request_key") or "") != request["request_key"]
        or str(row.get("industry_key") or "") != industry_key
        or int(row.get("branch_id") or 0) != int(branch_id)
        or str(row.get("template_key") or "")
        != str(request.get("template_key") or "")
        or str(row.get("template_version") or "")
        != str(request.get("template_version") or "")
        or not isinstance(snapshot, dict)
        or list(snapshot.get("file_slots") or [])
        != list(request.get("file_slots") or [])
        or stored_observations != request.get("observations")
        or not isinstance(created_payload, dict)
        or str(created_payload.get("note") or "") != str(request.get("note") or "")
    )
    if raw.get("visit_at") not in (None, ""):
        mismatch = mismatch or float(row.get("visit_at") or 0) != float(
            request["visit_at"]
        )

    stored_photos = db.q(
        "SELECT sha256,capture_slot,item_code FROM inspection_photo "
        "WHERE tenant_id=? AND visit_id=? AND phase='before' ORDER BY id",
        (int(tid), int(visit_id)),
    )
    if stored_photos:
        stored_fingerprints = [
            (
                str(item.get("sha256") or ""),
                str(item.get("capture_slot") or ""),
                str(item.get("item_code") or ""),
            )
            for item in stored_photos
        ]
        incoming_fingerprints = [
            (
                str(item.get("sha256") or ""),
                str(item.get("capture_slot") or ""),
                str(item.get("item_code") or ""),
            )
            for item in prepared
        ]
        mismatch = mismatch or stored_fingerprints != incoming_fingerprints
    if mismatch:
        raise inspection.InspectionConflict(
            "巡店请求号已用于不同内容，请刷新后重新提交"
        )


def _normalize_inspection_image(data: bytes, filename: str) -> dict:
    """校验、纠正方向并重编码，彻底移除 EXIF 与上传文件名。"""
    import io
    from PIL import Image, ImageOps

    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ValueError("巡店照片仅支持 JPEG、PNG 或 WebP")
    avatar.validate_upload_media(data, ext, "photo")
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            if "A" in image.getbands():
                base = Image.new("RGB", image.size, "white")
                base.paste(image, mask=image.getchannel("A"))
                image = base
            else:
                image = image.convert("RGB")
            image.thumbnail((4096, 4096))
            width, height = image.size
            output = io.BytesIO()
            image.save(output, "JPEG", quality=88, optimize=True)
            normalized = output.getvalue()
    except (OSError, ValueError) as exc:
        raise ValueError("巡店照片无法安全解析") from exc
    if not normalized or len(normalized) > inspection.MAX_PHOTO_BYTES:
        raise ValueError("巡店照片重编码后超过 8MB")
    return {
        "data": normalized,
        "mime_type": "image/jpeg",
        "byte_size": len(normalized),
        "sha256": hashlib.sha256(normalized).hexdigest(),
        "width": width,
        "height": height,
    }


async def _prepare_inspection_uploads(files: list[UploadFile]) -> list[dict]:
    if not files or len(files) > inspection.MAX_PHOTOS:
        raise HTTPException(400, f"请上传 1-{inspection.MAX_PHOTOS} 张巡店照片")
    declared = 0
    for file in files:
        try:
            declared += max(0, int(getattr(file, "size", 0) or 0))
        except (TypeError, ValueError):
            pass
    if declared > _INSPECTION_UPLOAD_MAX_BYTES:
        raise HTTPException(413, "巡店照片总大小不能超过 38MB")
    await asyncio.to_thread(
        _assert_persistent_upload_capacity,
        TEN(),
        max(1, declared),
        incoming_files=len(files),
    )
    prepared = []
    total = 0
    for file in files:
        data = await _read_limited(
            file,
            inspection.MAX_PHOTO_BYTES,
            "单张巡店照片不能超过 8MB",
        )
        try:
            item = await asyncio.to_thread(
                _normalize_inspection_image,
                data,
                file.filename or "photo.jpg",
            )
        except (avatar.InvalidAvatarMedia, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        total += int(item["byte_size"])
        if total > _INSPECTION_UPLOAD_MAX_BYTES:
            raise HTTPException(413, "巡店照片总大小不能超过 38MB")
        prepared.append(item)
    await asyncio.to_thread(
        _assert_persistent_upload_capacity,
        TEN(),
        max(1, total),
        incoming_files=len(prepared),
    )
    return prepared


def _store_inspection_images(tid: int, visit_id: int, items: list[dict]) -> list[dict]:
    root = os.path.realpath(assetfiles.ASSET_ROOT)
    if not os.path.isdir(root) or os.path.islink(root):
        raise ValueError("巡店素材根目录不安全")
    directory = root
    for component in ("inspections", str(int(tid)), str(int(visit_id))):
        candidate = os.path.abspath(os.path.join(directory, component))
        try:
            inside = os.path.commonpath((root, candidate)) == root
        except ValueError:
            inside = False
        if not inside:
            raise ValueError("巡店照片目录不安全")
        try:
            os.mkdir(candidate, 0o750)
        except FileExistsError:
            pass
        if (
            os.path.islink(candidate)
            or not os.path.isdir(candidate)
            or os.path.realpath(candidate) != candidate
        ):
            raise ValueError("巡店照片目录不安全")
        directory = candidate
    records = []
    created_paths: list[str] = []
    try:
        for item in items:
            filename = os.urandom(16).hex() + ".jpg"
            path = os.path.abspath(os.path.join(directory, filename))
            if os.path.commonpath((directory, path)) != directory:
                raise ValueError("巡店照片路径不安全")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            fd = os.open(path, flags, 0o640)
            created_paths.append(path)
            try:
                with os.fdopen(fd, "wb", closefd=True) as handle:
                    fd = -1
                    handle.write(item["data"])
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    os.unlink(path)
                    created_paths.remove(path)
                except (OSError, ValueError):
                    pass
                raise
            record = {
                key: item[key]
                for key in ("mime_type", "byte_size", "sha256", "width", "height")
            } | {"storage_key": f"inspections/{int(tid)}/{int(visit_id)}/{filename}"}
            for key in ("capture_slot", "item_code"):
                if item.get(key) not in (None, ""):
                    record[key] = item[key]
            records.append(record)
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        directory_fd = os.open(directory, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        # 批量落图必须是文件层面的 all-or-nothing，不遗留前几张。
        for path in created_paths:
            try:
                if not os.path.islink(path):
                    os.unlink(path)
            except FileNotFoundError:
                pass
        raise
    return records


def _cleanup_unreferenced_inspection_images(records: list[dict]) -> None:
    root = os.path.realpath(assetfiles.ASSET_ROOT)
    for record in records:
        storage_key = str(record.get("storage_key") or "")
        if not storage_key or db.one(
            "SELECT 1 AS ok FROM inspection_photo WHERE storage_key=? LIMIT 1",
            (storage_key,),
        ):
            continue
        path = os.path.abspath(os.path.join(root, storage_key))
        resolved = os.path.realpath(path)
        if (
            os.path.commonpath((root, path)) != root
            or os.path.commonpath((root, resolved)) != root
            or resolved != path
            or os.path.islink(path)
        ):
            continue
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _cleanup_empty_shell_inspection_files(tid: int, visit_id: int) -> int:
    """清理进程崩溃留下的未入库初检文件，只处理空 preparing shell。"""
    shell = db.one(
        "SELECT id FROM inspection_visit WHERE id=? AND tenant_id=? "
        "AND status='preparing' AND task_id IS NULL AND deleted_at IS NULL "
        "AND NOT EXISTS(SELECT 1 FROM inspection_photo p "
        "WHERE p.tenant_id=inspection_visit.tenant_id "
        "AND p.visit_id=inspection_visit.id)",
        (int(visit_id), int(tid)),
    )
    if not shell:
        return 0
    root = os.path.realpath(assetfiles.ASSET_ROOT)
    directory = os.path.abspath(
        os.path.join(root, "inspections", str(int(tid)), str(int(visit_id)))
    )
    try:
        safe = (
            os.path.commonpath((root, directory)) == root
            and os.path.realpath(directory) == directory
            and not os.path.islink(directory)
        )
    except ValueError:
        safe = False
    if not safe or not os.path.isdir(directory):
        return 0
    removed = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not re.fullmatch(r"[a-f0-9]{32}\.jpg", entry.name):
                continue
            try:
                if entry.is_file(follow_symlinks=False):
                    os.unlink(entry.path)
                    removed += 1
            except FileNotFoundError:
                pass
    if removed:
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return removed


async def _run_inspection_file_safely(fn, *args, **kwargs):
    """等待已提交的文件写/删真实收口，避免请求取消后留孤儿文件。"""
    operation = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    return await _drain_task_despite_cancellation(operation)


def _abandon_empty_inspection_shell(
    tid: int,
    visit_id: int,
    *,
    industry_key: str,
) -> bool:
    """删掉还没有照片/任务的准备态空壳，让同一幂等号可安全重试。"""
    with db.atomic() as connection:
        row = connection.execute(
            "SELECT id FROM inspection_visit WHERE id=? AND tenant_id=? "
            "AND industry_key=? AND status='preparing' AND task_id IS NULL "
            "AND deleted_at IS NULL AND NOT EXISTS(SELECT 1 FROM inspection_photo p "
            "WHERE p.tenant_id=inspection_visit.tenant_id "
            "AND p.visit_id=inspection_visit.id)",
            (int(visit_id), int(tid), industry_key),
        ).fetchone()
        if not row:
            return False
        connection.execute(
            "DELETE FROM inspection_event WHERE tenant_id=? AND visit_id=?",
            (int(tid), int(visit_id)),
        )
        changed = connection.execute(
            "DELETE FROM inspection_visit WHERE id=? AND tenant_id=? "
            "AND status='preparing' AND task_id IS NULL",
            (int(visit_id), int(tid)),
        )
        return changed.rowcount == 1


def _inspection_brief(industry_key: str, branch: dict, note: str) -> dict:
    return taskrunner.normalize_brief({
        "direction": f"巡检门店“{branch.get('name') or '门店'}”，形成问题、整改与复查闭环",
        "industry": industry_key,
        "material": str(note or "")[:12000],
        "length": "std",
    })


def _activate_inspection_job(
    tid: int,
    uid: int,
    industry_key: str,
    visit_id: int,
    photo_records: list[dict],
    brief: dict,
) -> dict:
    with db.atomic() as connection:
        existing = connection.execute(
            "SELECT task_id,status FROM inspection_visit WHERE id=? AND tenant_id=? "
            "AND industry_key=? AND deleted_at IS NULL",
            (visit_id, tid, industry_key),
        ).fetchone()
        if not existing:
            raise inspection.InspectionNotFound("巡店记录不存在")
        if existing["task_id"]:
            return {
                "created": False,
                "inspection_id": visit_id,
                "task_id": int(existing["task_id"]),
            }
        inspection.attach_visit_photos(
            tid, uid, industry_key, visit_id, photo_records
        )
        task_id = _create_charged_expert_task(
            {
                "emp_idx": inspection.EMPLOYEE_IDX,
                "tenant_id": tid,
                "brief_json": json.dumps(brief, ensure_ascii=False),
            },
            note="巡店照片分析",
        )
        changed = connection.execute(
            "UPDATE inspection_visit SET task_id=?,updated_at=? WHERE id=? "
            "AND tenant_id=? AND industry_key=? AND task_id IS NULL "
            "AND status='analyzing'",
            (task_id, time.time(), visit_id, tid, industry_key),
        )
        if changed.rowcount != 1:
            raise inspection.InspectionConflict("巡店任务已被另一个请求接管")
        return {
            "created": True,
            "inspection_id": visit_id,
            "task_id": task_id,
        }


def _claim_inspection_task(task_id: int) -> dict | None:
    with db.atomic() as connection:
        row = connection.execute(
            "SELECT t.*,v.id inspection_id,v.industry_key,"
            "v.created_by inspection_creator FROM task t "
            "JOIN inspection_visit v ON v.task_id=t.id "
            "AND v.tenant_id=t.tenant_id WHERE t.id=? AND t.emp_idx=? "
            "AND t.status='queued' AND t.billing_status IN ('charged','included') "
            "AND t.deleted_at IS NULL AND v.deleted_at IS NULL "
            "AND v.status='analyzing' AND EXISTS("
            "SELECT 1 FROM inspection_photo p WHERE p.tenant_id=v.tenant_id "
            "AND p.visit_id=v.id AND p.phase='before')",
            (task_id, inspection.EMPLOYEE_IDX),
        ).fetchone()
        if not row:
            task = connection.execute(
                "SELECT status FROM task WHERE id=? AND emp_idx=? "
                "AND deleted_at IS NULL",
                (task_id, inspection.EMPLOYEE_IDX),
            ).fetchone()
            if not task or task["status"] != "queued":
                return None
            raise inspection.InspectionConflict(
                "巡店任务缺少可恢复的巡店记录或初检照片"
            )
        changed = connection.execute(
            "UPDATE task SET status='running',summary_md=NULL,terminal_at=NULL,updated_at=? "
            "WHERE id=? AND emp_idx=? AND status='queued' "
            "AND billing_status IN ('charged','included') AND deleted_at IS NULL",
            (time.time(), task_id, inspection.EMPLOYEE_IDX),
        )
        if changed.rowcount != 1:
            return None
        return dict(row)


def _inspection_authoritative_contract(allowed_photo_ids: set[int]) -> str:
    """生成位于所有可编辑模板之后的本次巡店唯一结构合同。"""
    allowed = sorted(int(value) for value in allowed_photo_ids)
    if not allowed or any(value <= 0 for value in allowed):
        raise inspection.InspectionError("巡店照片标识无效")
    expected = len(allowed)
    schema = {
        "type": "object",
        "required": [
            "analysis_status", "summary", "score", "photo_reviews", "issues",
        ],
        "properties": {
            "analysis_status": {
                "type": "string",
                "enum": ["issues_found", "clean_candidate"],
            },
            "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            "score": {"type": "number", "minimum": 0, "maximum": 100},
            "photo_reviews": {
                "type": "array",
                "minItems": expected,
                "maxItems": expected,
                "items": {
                    "type": "object",
                    "required": [
                        "photo_id", "analyzable", "verdict", "confidence",
                        "visible_facts",
                    ],
                    "properties": {
                        "photo_id": {"type": "integer", "enum": allowed},
                        "analyzable": {"type": "boolean"},
                        "verdict": {
                            "type": "string",
                            "enum": ["clean", "issue"],
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": inspection.MIN_PHOTO_REVIEW_CONFIDENCE,
                            "maximum": 1,
                        },
                        "visible_facts": {
                            "type": "array", "minItems": 1, "maxItems": 12,
                            "items": {"type": "string", "minLength": 1, "maxLength": 300},
                        },
                    },
                },
            },
            "issues": {
                "type": "array", "maxItems": 30,
                "items": {
                    "type": "object",
                    "required": [
                        "title", "description", "severity", "category",
                        "confidence", "root_cause", "evidence", "action",
                    ],
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 120},
                        "description": {"type": "string", "minLength": 1, "maxLength": 1500},
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "high", "medium", "low"],
                        },
                        "category": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9_\\-\\u4e00-\\u9fff]{1,50}$",
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "root_cause": {"type": "string", "maxLength": 800},
                        "evidence": {
                            "type": "array", "minItems": 1, "maxItems": expected,
                            "items": {
                                "type": "object",
                                "required": ["photo_id", "note"],
                                "properties": {
                                    "photo_id": {"type": "integer", "enum": allowed},
                                    "note": {"type": "string", "maxLength": 300},
                                    "bbox": {
                                        "type": ["array", "null"],
                                        "minItems": 4, "maxItems": 4,
                                        "items": {"type": "number", "minimum": 0, "maximum": 1},
                                    },
                                },
                            },
                        },
                        "action": {
                            "type": "object",
                            "required": ["plan", "owner", "due_days"],
                            "properties": {
                                "plan": {"type": "string", "minLength": 1, "maxLength": 1200},
                                "owner": {"type": "string", "maxLength": 60},
                                "due_days": {"type": "number", "minimum": 0, "maximum": 90},
                            },
                        },
                    },
                },
            },
        },
    }
    return "\n".join((
        _INSPECTION_CONTRACT_MARKER,
        "本合同覆盖前文所有 JSON 样例、编号和字段说明；只能输出一个 JSON 对象，不要 Markdown。",
        f"allowed_photo_ids={json.dumps(allowed, ensure_ascii=False)}",
        f"expected_photo_review_count={expected}",
        "所有 allowed_photo_ids 必须在 photo_reviews 中各出现一次，不得缺失、重复或引用外部 ID。",
        "analyzable=false 表示照片不可分析，不得猜测或改成 true；每张可分析照片的 confidence 必须 >=0.8。",
        "verdict=issue 的 photo_id 集合必须与 issues[*].evidence[*].photo_id 集合完全一致。",
        "issues 非空时 analysis_status=issues_found；issues 为空时 analysis_status=clean_candidate。",
        "完整 JSON Schema：" + json.dumps(
            schema, ensure_ascii=False, separators=(",", ":")
        ),
    ))


def _inspection_attempt_system(
    base_system: str,
    allowed_photo_ids: set[int],
    *,
    validation_code: str | None = None,
    extra_instruction: str = "",
) -> str:
    """确保每次调用只有一份、且最后出现的动态权威合同。"""
    prefix = str(base_system or "").split(_INSPECTION_CONTRACT_MARKER, 1)[0].rstrip()
    pieces = [prefix]
    if extra_instruction:
        pieces.append(str(extra_instruction).strip())
    if validation_code is not None:
        safe_code = str(validation_code or "")
        if not re.fullmatch(r"IC_[A-Z0-9_]{3,64}", safe_code):
            safe_code = "IC_CONTRACT_INVALID"
        pieces.append(
            "【上一次仅格式校验未通过】"
            f"validation_code={safe_code}。不提供上一版原文；"
            "请重新独立查看同一批图片，严格遵守下方合同。"
        )
    pieces.append(_inspection_authoritative_contract(allowed_photo_ids))
    return "\n\n".join(item for item in pieces if item)


_INSPECTION_MODEL_ITEM_FIELDS = (
    "item_code", "area_code", "label", "tier", "required", "evidence",
    "shot_guide", "severity", "condition", "jurisdiction", "source_no",
)
_INSPECTION_MODEL_SLOT_FIELDS = (
    "slot_code", "area_code", "label", "required", "shot_guide",
    "min_photos", "max_photos",
)


def _inspection_frozen_standard_block(snapshot: dict) -> str:
    """Render only visual-inspection instructions from the frozen snapshot.

    The snapshot may also carry business metric definitions and submitted
    observations for boss-facing views.  Those fields, source URLs and any
    unexpected employee/tenant data must never be forwarded to the model.
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return ""

    def whitelist(rows, fields: tuple[str, ...]) -> list[dict]:
        return [
            {
                key: row[key]
                for key in fields
                if key in row and row[key] is not None
            }
            for row in (rows or [])
            if isinstance(row, dict)
        ]

    safe_snapshot = {
        key: snapshot[key]
        for key in ("template_key", "template_version", "as_of", "catalog_sha256")
        if key in snapshot and snapshot[key] is not None
    }
    safe_snapshot["items"] = whitelist(
        snapshot.get("items"), _INSPECTION_MODEL_ITEM_FIELDS
    )
    safe_snapshot["capture_slots"] = whitelist(
        snapshot.get("capture_slots"), _INSPECTION_MODEL_SLOT_FIELDS
    )
    if not safe_snapshot["items"] and not safe_snapshot["capture_slots"]:
        return ""
    return "【本次冻结巡店检查标准】\n" + json.dumps(
        safe_snapshot, ensure_ascii=False, separators=(",", ":")
    )


def _inspection_prompt_bundle(
    tid: int,
    visit: dict,
    *,
    include_initial_contract: bool = True,
) -> providers.PromptBundle:
    station = registry.BY_IDX[inspection.EMPLOYEE_IDX]
    config = employees.get_config(inspection.EMPLOYEE_IDX)
    capabilities = [
        item for item in registry.capabilities_for(inspection.EMPLOYEE_IDX)
        if item.get("enabled")
    ]
    caps_text = "\n".join(
        f"- {item['name']}：{item['desc']}" for item in capabilities
    )
    skills_text = employees.skills_block(inspection.EMPLOYEE_IDX)
    template = str(
        config.get("prompt_template")
        or registry.DEFAULT_PROMPTS["inspection"]
    )[:12000]
    private_template = employees.render(template, {
        "photos": "（读取用户消息中的照片编号）",
        "scope": "（读取用户消息中的检查重点）",
        "store": "（读取用户消息中的门店信息）",
    })
    standard_snapshot = visit.get("standard_snapshot")
    if not isinstance(standard_snapshot, dict):
        standard_snapshot = {}
    slot_labels = {
        str(item.get("slot_code") or ""): str(item.get("label") or "")
        for item in (standard_snapshot.get("capture_slots") or [])
        if isinstance(item, dict) and str(item.get("slot_code") or "")
    }
    frozen_standard = _inspection_frozen_standard_block(standard_snapshot)
    photo_rows = [
        {
            # photo_id 只用于服务端外键校验；display_no 是本次巡店
            # 内给人看的稳定编号。
            "photo_id": int(item["id"]),
            "display_no": int(item.get("display_no") or 0),
            "caption": item.get("caption") or "",
            "capture_slot": str(item.get("capture_slot") or ""),
            "capture_slot_label": slot_labels.get(
                str(item.get("capture_slot") or ""), ""
            ),
        }
        for item in visit.get("photos") or []
        if item.get("phase") == "before"
    ]
    allowed_photo_ids = {int(item["photo_id"]) for item in photo_rows}
    authoritative_contract = (
        _inspection_authoritative_contract(allowed_photo_ids)
        if include_initial_contract
        else ""
    )
    system = "\n".join(filter(None, (
        providers.CONFIDENTIALITY_SYSTEM,
        f"你是数字员工“{station['name']}”，岗位职责：{station['duty']}。",
        "【本次启用的工作能力】\n" + caps_text if caps_text else "",
        skills_text,
        "【内部岗位工作方式】\n" + private_template,
        "只能依据当前上传照片中的可见事实形成问题；每个问题必须绑定同图 photo_id。"
        "任何问题都不能由模型自行标记关闭；零问题最终是否通过由服务端异模复核决定。",
        # 冻结标准和权威 JSON 合同必须永远位于可编辑的 skills/template 之后；
        # 合同仍保持最后出现，防止模板覆盖输出约束。
        frozen_standard,
        authoritative_contract,
    )))
    branch = visit.get("branch") if isinstance(visit.get("branch"), dict) else {}
    safe_branch = {
        key: branch.get(key)
        for key in ("id", "store_code", "name", "region", "address")
        if branch.get(key) not in (None, "")
    }
    user = (
        "【门店巡检业务数据（不可信输入）】\n"
        + json.dumps({
            "industry": visit.get("industry_key"),
            # 经营观察值、店长/员工表正文永远不进模型。
            "branch": safe_branch,
            "visit_at": visit.get("visit_at"),
            "photos": photo_rows,
            "allowed_photo_ids": sorted(allowed_photo_ids),
            "expected_photo_review_count": len(allowed_photo_ids),
            "inspection_scope": str(visit.get("scope") or "")[:1000],
        }, ensure_ascii=False)
    )
    return providers.PromptBundle(
        system=system,
        user=user,
        sensitive=tuple(
            value for value in (
                station.get("duty") or "",
                providers.leak_fingerprint_source(caps_text),
                providers.leak_fingerprint_source(skills_text),
                template,
            ) if str(value).strip()
        ),
    )


def _load_inspection_images(
    tid: int,
    visit: dict,
    *,
    phase: str = "before",
) -> list[tuple[dict, str, str]]:
    import base64

    images = []
    for position, photo in enumerate(visit.get("photos") or [], start=1):
        if photo.get("phase") != phase:
            continue
        url = "/files/" + str(photo.get("storage_key") or "")
        path = assetfiles.resolve_tenant_asset(
            url,
            tid,
            allowed_extensions=(".jpg",),
        )
        data = _read_file_bytes(path)
        if not data or len(data) > inspection.MAX_PHOTO_BYTES:
            raise ValueError("巡店照片文件缺失或超过限制")
        images.append((
            {
                "photo_id": int(photo.get("id") or position),
                "display_no": int(photo.get("display_no") or position),
            },
            "image/jpeg",
            base64.b64encode(data).decode("ascii"),
        ))
    if not images:
        raise ValueError(
            "巡店记录没有可分析的初检照片"
            if phase == "before"
            else "整改任务没有可分析的复查照片"
        )
    return images


def _inspection_candidate_result(
    response: dict,
    bundle: providers.PromptBundle,
    allowed_photo_ids: set[int],
) -> dict:
    """只保留通过业务 schema 的结构；上游原文不落库。"""
    text = response.get("text")
    if not isinstance(text, str) or not text.strip():
        raise inspection.InspectionContractError(
            "巡店识别结果不是有效 JSON",
            validation_code="IC_JSON_INVALID",
        )
    providers.assert_no_private_leak(text, bundle.sensitive)
    try:
        raw = llm.extract_json(text)
    except llm.LLMError as exc:
        raise inspection.InspectionContractError(
            "巡店识别结果不是有效 JSON",
            validation_code="IC_JSON_INVALID",
        ) from exc
    return inspection.normalize_model_result(
        raw,
        allowed_photo_ids,
        allow_clean_candidate=True,
    )


def _inspection_usage_add(total: dict, response: dict) -> None:
    """只累计网关明确返回的实际用量，不从文本推测。"""
    total["cost_usd"] = (
        float(total.get("cost_usd") or 0)
        + float(response.get("cost_usd") or 0)
    )
    total["tokens"] = (
        int(total.get("tokens") or 0)
        + int(response.get("tokens") or 0)
    )


async def _inspection_visual_candidate(
    *,
    bundle: providers.PromptBundle,
    images: list[tuple[dict, str, str]],
    allowed_photo_ids: set[int],
    model: str,
    deadline: float,
    stage: str,
    token_prefix: str,
    slot_label: str,
    extra_instruction: str = "",
) -> tuple[dict, dict]:
    """在共享绝对截止时间内获取一个严格候选。

    只有 JSON/字段/覆盖等合同遵循错误可以在不传第一版原文的
    前提下同模重做一次。不可分析、低置信度、泄露、上游错误和
    取消一律原样失败。
    """
    usage = {"cost_usd": 0.0, "tokens": 0}
    validation_code: str | None = None
    loop = asyncio.get_running_loop()
    for attempt in range(2):
        remaining = float(deadline) - loop.time()
        if remaining <= 0:
            raise TimeoutError("巡店视觉分析超时")
        system_prompt = _inspection_attempt_system(
            bundle.system,
            allowed_photo_ids,
            validation_code=validation_code,
            extra_instruction=extra_instruction,
        )
        # timeout 包住 AI 槽等待与供应商调用；每轮都使用同一
        # absolute deadline 的剩余值，不得重置 300s。
        async with asyncio.timeout(remaining):
            async with _free_ai_slot(slot_label):
                provider_remaining = float(deadline) - loop.time()
                if provider_remaining <= 0:
                    raise TimeoutError("巡店视觉分析超时")
                response = await providers.call_vision(
                    inspection.EMPLOYEE_IDX,
                    bundle.user,
                    images,
                    timeout=provider_remaining,
                    token=f"{token_prefix}:attempt:{attempt + 1}",
                    system_prompt=system_prompt,
                    max_tokens=5000,
                    model_override=model,
                )
        _inspection_usage_add(usage, response)
        try:
            candidate = _inspection_candidate_result(
                response,
                bundle,
                allowed_photo_ids,
            )
        except providers.PrivatePromptLeak:
            raise
        except inspection.InspectionContractError as exc:
            code = str(exc.validation_code)
            # 日志/指标只包含有限稳定码与固定阶段，不记任何
            # 照片、门店、任务 ID、业务文字或模型原文。
            log.warning(
                "inspection candidate rejected stage=%s attempt=%d validation_code=%s",
                stage,
                attempt + 1,
                code,
            )
            obs.count(f"inspection.validation.{code}")
            if not exc.retryable or attempt == 1:
                raise
            obs.count("inspection.validation.format_retry")
            validation_code = code
            continue
        if attempt:
            obs.count("inspection.validation.format_retry_succeeded")
        return candidate, usage
    raise inspection.InspectionContractError(
        "巡店识别结果未通过合同",
        validation_code="IC_CONTRACT_INVALID",
    )


def _finalize_inspection_candidates(
    primary: dict,
    review: dict | None,
    *,
    primary_model: str,
    review_model: str | None,
) -> dict:
    """风险取发现问题的复核结果；零问题必须双模完整 clean。"""
    if review is None:
        if not primary["issues"]:
            raise inspection.InspectionError("零问题巡店结果未经异模复核")
        return {**primary, "analysis_status": "issues_found"}
    if not review_model or review_model == primary_model:
        raise inspection.InspectionError("巡店复核模型必须与主模型不同")
    if review["issues"]:
        return {**review, "analysis_status": "issues_found"}
    if primary["issues"]:
        return {**primary, "analysis_status": "issues_found"}
    conservative = (
        primary if float(primary["score"]) <= float(review["score"]) else review
    )
    return {
        **conservative,
        "analysis_status": "clean_verified",
        "score": min(float(primary["score"]), float(review["score"])),
        "verification": {
            "primary_model": primary_model,
            "review_model": review_model,
            "both_clean": True,
        },
    }


def _inspection_markdown(visit: dict) -> str:
    branch = visit.get("branch") or {}
    lines = [
        f"# {branch.get('name') or '门店'}巡店记录",
        "",
        f"- 综合评分：{visit.get('score') if visit.get('score') is not None else '待人工确认'}",
        f"- 巡店结论：{visit.get('summary') or ''}",
        "",
        "## 问题与整改计划",
    ]
    for index, issue in enumerate(visit.get("issues") or [], 1):
        action = issue.get("action") or {}
        photos = "、".join(
            f"照片{item.get('display_no') or '?'}"
            for item in issue.get("evidence") or []
        )
        lines.extend((
            f"### {index}. [{issue.get('severity')}] {issue.get('title')}",
            str(issue.get("description") or ""),
            f"- 证据：{photos or '待人工核查'}",
            f"- 整改：{action.get('plan') or '待确认'}",
            f"- 负责人：{action.get('owner') or '待指派'}",
            "",
        ))
    lines.append("## 下一步")
    lines.append("整改负责人提交复查照片后，由企业主人工确认是否真正关闭问题。")
    return "\n".join(lines)


def _commit_inspection_delivery(
    task_id: int,
    tid: int,
    uid: int,
    industry_key: str,
    visit_id: int,
    model_result: dict,
    usage: dict,
) -> bool:
    with db.atomic() as connection:
        visit = inspection.complete_visit(
            tid, uid, industry_key, visit_id, model_result
        )
        markdown = _inspection_markdown(visit)
        now = time.time()
        changed = connection.execute(
            "UPDATE task SET status='done',output_md=?,summary_md=?,cost_usd=?,"
            "tokens=?,steps_json=?,billing_status=CASE WHEN billing_status='charged' "
            "THEN 'succeeded' ELSE billing_status END,terminal_at=?,updated_at=? "
            "WHERE id=? "
            "AND status='running' AND billing_status IN ('charged','included') "
            "AND deleted_at IS NULL",
            (
                markdown,
                str(visit.get("summary") or "")[:800],
                float(usage.get("cost_usd") or 0),
                int(usage.get("tokens") or 0),
                json.dumps([
                    {"step": "photo_review", "msg": "现场照片已逐张核查"},
                    {"step": "capa", "msg": "问题、整改与复查计划已形成"},
                ], ensure_ascii=False),
                now,
                now,
                task_id,
            ),
        )
        if changed.rowcount != 1:
            raise inspection.InspectionConflict("巡店任务状态已发生变化")
        connection.execute(
            "INSERT INTO asset(type,tenant_id,payload_json,created_at,updated_at) "
            "VALUES('report',?,?,?,?)",
            (
                tid,
                json.dumps({
                    "title": f"{(visit.get('branch') or {}).get('name') or '门店'}巡店记录",
                    "emp": "巡店经理",
                    "task_id": task_id,
                    "inspection_id": visit_id,
                    "route": (
                        f"#/inspections/{visit_id}/"
                        f"{visit.get('industry_key') or ''}"
                    ).rstrip("/"),
                }, ensure_ascii=False),
                now,
                now,
            ),
        )
        return True


def _settle_inspection_failure(
    task_id: int,
    tid: int,
    uid: int,
    visit_id: int,
    message: str,
) -> bool:
    with db.atomic():
        settled = taskrunner.settle_failure(task_id, message)
        inspection._mark_visit_failed(
            tid, uid, visit_id, RuntimeError("inspection_failed")
        )
        return settled


def _settle_inspection_task_by_id(task_id: int, message: str) -> bool:
    """不依赖请求作用域收口巡店任务，供启动恢复/启动失败使用。"""
    row = db.one(
        "SELECT t.tenant_id,v.id visit_id,v.created_by FROM task t "
        "LEFT JOIN inspection_visit v ON v.task_id=t.id "
        "AND v.tenant_id=t.tenant_id AND v.deleted_at IS NULL "
        "WHERE t.id=? AND t.emp_idx=?",
        (int(task_id), inspection.EMPLOYEE_IDX),
    )
    if not row:
        return False
    settled = taskrunner.settle_failure(int(task_id), message)
    if row.get("visit_id"):
        inspection._mark_visit_failed(
            int(row["tenant_id"]),
            int(row.get("created_by") or 0),
            int(row["visit_id"]),
            RuntimeError("inspection_worker_unavailable"),
        )
    return settled


def _prepare_inspection_retry(task_id: int, tenant_id: int) -> bool:
    """将失败巡店的 task + visit 在同一 SQLite 事务里恢复。"""
    with db.atomic() as connection:
        row = connection.execute(
            "SELECT v.id FROM task t JOIN inspection_visit v ON v.task_id=t.id "
            "AND v.tenant_id=t.tenant_id WHERE t.id=? AND t.tenant_id=? "
            "AND t.emp_idx=? AND t.status='failed' "
            "AND t.billing_status IN ('refunded','included') "
            "AND v.status='failed' AND v.deleted_at IS NULL "
            "AND EXISTS(SELECT 1 FROM inspection_photo p "
            "WHERE p.tenant_id=v.tenant_id AND p.visit_id=v.id "
            "AND p.phase='before')",
            (int(task_id), int(tenant_id), inspection.EMPLOYEE_IDX),
        ).fetchone()
        if not row:
            return False
        if not taskrunner.prepare_retry(int(task_id), int(tenant_id)):
            return False
        changed = connection.execute(
            "UPDATE inspection_visit SET status='analyzing',terminal_at=NULL,updated_at=?,"
            "version=version+1 WHERE id=? AND tenant_id=? AND status='failed' "
            "AND deleted_at IS NULL",
            (time.time(), int(row["id"]), int(tenant_id)),
        )
        if changed.rowcount != 1:
            raise inspection.InspectionConflict(
                "巡店记录已更新，请刷新后重试"
            )
        return True


def _recover_inspection_tasks() -> dict:
    """服务重启时只恢复有完整 visit + before photo 证据的巡店任务。"""
    resumable: list[int] = []
    invalid: list[int] = []
    rows = db.q(
        "SELECT t.id,t.status,t.billing_status,t.tenant_id,"
        "v.id visit_id,v.status visit_status,v.created_by,"
        "EXISTS(SELECT 1 FROM inspection_photo p "
        "WHERE p.tenant_id=t.tenant_id AND p.visit_id=v.id "
        "AND p.phase='before') has_before FROM task t "
        "LEFT JOIN inspection_visit v ON v.task_id=t.id "
        "AND v.tenant_id=t.tenant_id AND v.deleted_at IS NULL "
        "WHERE t.emp_idx=? AND t.deleted_at IS NULL "
        "AND t.status IN ('queued','running','failed')",
        (inspection.EMPLOYEE_IDX,),
    )
    for row in rows:
        task_id = int(row["id"])
        status = str(row.get("status") or "")
        if status == "failed":
            # generic resume 已会幂等退回 charged；这里补齐 visit 终态。
            if row.get("visit_id") and row.get("visit_status") in {
                "preparing", "analyzing"
            }:
                inspection._mark_visit_failed(
                    int(row["tenant_id"]),
                    int(row.get("created_by") or 0),
                    int(row["visit_id"]),
                    RuntimeError("inspection_restart_recovery"),
                )
            continue
        if (
            row.get("visit_id")
            and row.get("visit_status") == "analyzing"
            and bool(row.get("has_before"))
            and row.get("billing_status") in {"charged", "included"}
        ):
            changed = db.execute(
                "UPDATE task SET status='queued',terminal_at=NULL,updated_at=? WHERE id=? "
                "AND emp_idx=? AND status IN ('queued','running') "
                "AND billing_status IN ('charged','included') "
                "AND deleted_at IS NULL",
                (time.time(), task_id, inspection.EMPLOYEE_IDX),
            )
            if changed == 1:
                resumable.append(task_id)
            continue
        invalid.append(task_id)
    for task_id in invalid:
        _settle_inspection_task_by_id(
            task_id,
            "巡店任务的现场证据不完整，已安全终止并退回点数",
        )
    return {"task_ids": resumable, "invalid": len(invalid)}


async def _resume_inspection_tasks() -> dict:
    recovered = await db.arun(_recover_inspection_tasks)
    for task_id in recovered["task_ids"]:
        asyncio.create_task(_run_inspection_task(int(task_id)))
    if recovered["task_ids"] or recovered["invalid"]:
        log.warning(
            "inspection recovery resumed=%d invalid=%d",
            len(recovered["task_ids"]),
            int(recovered["invalid"]),
        )
    return recovered


async def _run_inspection_task(task_id: int):
    try:
        claimed = await _run_db_safely(_claim_inspection_task, task_id)
    except inspection.InspectionError as exc:
        log.error(
            "inspection claim failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        await db.arun(
            _settle_inspection_task_by_id,
            task_id,
            "巡店任务的现场证据不完整，已安全终止并退回点数",
        )
        return
    if not claimed:
        return
    tid = int(claimed["tenant_id"])
    uid = int(claimed.get("inspection_creator") or claimed.get("created_by") or 0)
    visit_id = int(claimed["inspection_id"])
    industry_key = str(claimed["industry_key"])
    engine.broadcast({
        "type": "task_update",
        "tenant_id": tid,
        "_required_modules": (industry_key,),
        "task_id": task_id,
        "idx": inspection.EMPLOYEE_IDX,
    })
    try:
        analysis_deadline = (
            asyncio.get_running_loop().time()
            + _INSPECTION_ANALYSIS_MODEL_TIMEOUT_SECONDS
        )
        visit = await db.arun(
            inspection.get_visit, tid, uid, industry_key, visit_id
        )
        brief = db.jloads(claimed.get("brief_json"), {}) or {}
        visit["scope"] = str(brief.get("material") or "")[:1000]
        bundle = await db.arun(_inspection_prompt_bundle, tid, visit)
        images = await asyncio.to_thread(_load_inspection_images, tid, visit)
        allowed_photo_ids = {
            int(photo["id"])
            for photo in visit.get("photos") or []
            if photo.get("phase") == "before"
        }
        primary_model = await db.arun(
            providers.vision_model_for,
            inspection.EMPLOYEE_IDX,
        )
        primary, primary_usage = await _inspection_visual_candidate(
            bundle=bundle,
            images=images,
            allowed_photo_ids=allowed_photo_ids,
            model=primary_model,
            deadline=analysis_deadline,
            stage="primary",
            token_prefix=f"inspection:{visit_id}:primary",
            slot_label="store-inspection",
        )
        review = None
        review_model = None
        review_usage = {"cost_usd": 0.0, "tokens": 0}
        if not primary["issues"]:
            review_model = providers.vision_review_model_for(primary_model)
            review_instruction = (
                "【独立异模复核】不要假设主模型结论正确，独立逐图检查。"
                "尤其审查通道遮挡、积水、电线、堆箱、卫生、消防与设备风险。"
                "仍严格输出本次最终权威 JSON 合同。"
            )
            review, review_usage = await _inspection_visual_candidate(
                bundle=bundle,
                images=images,
                allowed_photo_ids=allowed_photo_ids,
                model=review_model,
                deadline=analysis_deadline,
                stage="review",
                token_prefix=f"inspection:{visit_id}:review",
                slot_label="store-inspection-review",
                extra_instruction=review_instruction,
            )
        model_result = _finalize_inspection_candidates(
            primary,
            review,
            primary_model=primary_model,
            review_model=review_model,
        )
        usage = {
            "cost_usd": (
                float(primary_usage.get("cost_usd") or 0)
                + float(review_usage.get("cost_usd") or 0)
            ),
            "tokens": (
                int(primary_usage.get("tokens") or 0)
                + int(review_usage.get("tokens") or 0)
            ),
        }
        await _run_db_safely(
            _commit_inspection_delivery,
            task_id,
            tid,
            uid,
            industry_key,
            visit_id,
            model_result,
            usage,
        )
    except asyncio.CancelledError:
        await _run_db_safely(
            _settle_inspection_failure,
            task_id,
            tid,
            uid,
            visit_id,
            "巡店分析被服务中断，已自动退回点数，请免费重试",
        )
        raise
    except Exception as exc:
        log.error(
            "inspection task failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        await _run_db_safely(
            _settle_inspection_failure,
            task_id,
            tid,
            uid,
            visit_id,
            providers.public_failure_message(exc),
        )
    finally:
        engine.broadcast({
            "type": "task_update",
            "tenant_id": tid,
            "_required_modules": (industry_key,),
            "task_id": task_id,
            "idx": inspection.EMPLOYEE_IDX,
        })


def _start_inspection_task(result: dict):
    return asyncio.create_task(_run_inspection_task(int(result["task_id"])))


@app.get("/api/inspections/meta")
def inspection_meta(industry_key: str | None = None):
    try:
        selected, choices = _inspection_scope(industry_key)
        branch_page = _inspection_branch_search_db(
            TEN(), _inspection_actor_id(), selected, limit=20
        )
        is_manager = auth.is_admin()
        return {
            "industry_key": selected,
            "industries": choices,
            # 只保留首页兼容旧前端，数千门店必须走有界搜索。
            "branches": branch_page["items"],
            "branch_search": {
                "enabled": True,
                "endpoint": "/api/inspections/branches/search",
                "default_limit": 20,
                "max_limit": 50,
                "next_before_id": branch_page["next_before_id"],
            },
            "permissions": {
                "can_import_branches": is_manager,
                "can_create_branch": True,
                "can_review": is_manager,
            },
            "employee": _public_station(registry.BY_IDX[inspection.EMPLOYEE_IDX]),
        }
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@app.get("/api/inspections/branches/import-template")
async def inspection_branch_import_template(industry_key: str):
    _need_admin()
    try:
        await db.arun(_inspection_manager_scope, industry_key)
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    path = os.path.join(ROOT, "static", "inspection-store-import-template.xlsx")
    static_root = os.path.realpath(os.path.join(ROOT, "static"))
    real_path = os.path.realpath(path)
    try:
        safe = os.path.commonpath((static_root, real_path)) == static_root
    except ValueError:
        safe = False
    if not safe or not os.path.isfile(real_path) or os.path.islink(path):
        raise HTTPException(404, "巡店门店导入模板不存在")
    return FileResponse(
        real_path,
        filename="inspection-store-import-template.xlsx",
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/inspections/branches/imports")
async def inspection_branch_import_preview(
    industry_key: str = Form(...),
    request_key: str = Form(...),
    file: UploadFile = File(...),
):
    _need_admin()
    filename = file.filename or "branches.xlsx"
    try:
        selected, _choices = await db.arun(
            _inspection_manager_scope, industry_key
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    size = getattr(file, "size", None)
    if size is not None:
        try:
            if int(size) > inspectionimport.MAX_FILE_BYTES:
                raise HTTPException(
                    413,
                    f"XLSX 文件超过 {inspectionimport.MAX_FILE_MIB}MB",
                )
        except (TypeError, ValueError):
            raise HTTPException(400, "上传文件大小无效") from None
    try:
        try:
            data = await _read_limited(
                file,
                inspectionimport.MAX_FILE_BYTES,
                f"XLSX 文件超过 {inspectionimport.MAX_FILE_MIB}MB",
            )
        except HTTPException as exc:
            too_large_message = (
                f"XLSX 文件超过 {inspectionimport.MAX_FILE_MIB}MB"
            )
            if exc.status_code == 400 and exc.detail == too_large_message:
                raise HTTPException(413, str(exc.detail)) from exc
            raise
    finally:
        await file.close()
    try:
        return await _run_db_safely(
            inspectionimport.preview_import,
            TEN(),
            _inspection_actor_id(),
            selected,
            request_key,
            filename,
            data,
        )
    except inspectionimport.ImportContractError as exc:
        _raise_inspection_import_error(exc)


@app.get("/api/inspections/branches/imports/{import_id}")
async def inspection_branch_import_detail(
    import_id: int,
    industry_key: str,
    limit: int = inspectionimport.DEFAULT_IMPORT_PAGE_LIMIT,
    cursor: str | None = None,
    errors_only: bool = False,
    row_kind: str | None = None,
):
    _need_admin()
    try:
        selected, _choices = await db.arun(
            _inspection_manager_scope, industry_key
        )
        return await db.arun(
            inspectionimport.get_import,
            TEN(),
            _inspection_actor_id(),
            import_id,
            selected,
            limit=limit,
            cursor=cursor,
            errors_only=errors_only,
            row_kind=row_kind,
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    except inspectionimport.ImportContractError as exc:
        _raise_inspection_import_error(exc)


@app.post("/api/inspections/branches/imports/{import_id}/commit")
async def inspection_branch_import_commit(import_id: int, body: dict):
    _need_admin()
    try:
        selected, _choices = await db.arun(
            _inspection_manager_scope, body.get("industry_key")
        )
        return await _run_db_safely(
            inspectionimport.commit_import,
            TEN(),
            _inspection_actor_id(),
            import_id,
            selected,
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    except inspectionimport.ImportContractError as exc:
        _raise_inspection_import_error(exc)


@app.get("/api/inspections/branches/search")
async def inspection_branch_search(
    industry_key: str,
    q: str = "",
    region: str = "",
    limit: int = 20,
    before_id: int | None = None,
):
    try:
        selected, _choices = await db.arun(
            _inspection_scope, industry_key
        )
        return await db.arun(
            _inspection_branch_search_db,
            TEN(),
            _inspection_actor_id(),
            selected,
            q=q,
            region=region,
            limit=limit,
            before_id=before_id,
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@app.get("/api/inspections/standards/overrides")
async def inspection_standard_overrides(
    industry_key: str,
    scope_kind: str | None = None,
    scope_key: str | None = None,
):
    _need_admin()
    try:
        selected, _choices = await db.arun(
            _inspection_manager_scope, industry_key,
        )
        return await db.arun(
            inspectionoverrides.list_overrides,
            TEN(),
            _inspection_actor_id(),
            selected,
            scope_kind=scope_kind,
            scope_key=scope_key,
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    except inspectionoverrides.InspectionOverrideError as exc:
        _raise_inspection_override_error(exc)


@app.put("/api/inspections/standards/overrides")
async def inspection_standard_override_put(body: dict):
    _need_admin()
    try:
        selected, _choices = await db.arun(
            _inspection_manager_scope, body.get("industry_key"),
        )
        return await _run_db_safely(
            inspectionoverrides.upsert_override,
            TEN(),
            _inspection_actor_id(),
            selected,
            body,
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    except inspectionoverrides.InspectionOverrideError as exc:
        _raise_inspection_override_error(exc)


@app.delete("/api/inspections/standards/overrides/{override_id}")
async def inspection_standard_override_delete(override_id: int, body: dict):
    _need_admin()
    try:
        selected, _choices = await db.arun(
            _inspection_manager_scope, body.get("industry_key"),
        )
        return await _run_db_safely(
            inspectionoverrides.disable_override,
            TEN(),
            _inspection_actor_id(),
            selected,
            override_id,
            body.get("expected_version"),
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    except inspectionoverrides.InspectionOverrideError as exc:
        _raise_inspection_override_error(exc)


@app.get("/api/inspections/checklist")
async def inspection_checklist(industry_key: str, branch_id: int):
    try:
        selected, _choices = await db.arun(
            _inspection_scope, industry_key
        )
        return await db.arun(
            _inspection_checklist_db,
            TEN(),
            _inspection_actor_id(),
            selected,
            branch_id,
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    except inspectionimport.ImportContractError as exc:
        _raise_inspection_import_error(exc)


@app.post("/api/inspections/branches")
def inspection_branch_create(body: dict, industry_key: str | None = None):
    try:
        selected, _choices = _inspection_scope(industry_key or body.get("industry_key"))
        return inspection.create_branch(
            TEN(), _inspection_actor_id(), selected, body
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


_INSPECTION_RISK_BRANCH_LIMIT = 20
_INSPECTION_REGION_SUMMARY_LIMIT = 50


def _bounded_inspection_summary(
    summary: dict,
    *,
    selected_branch_id: int | None = None,
) -> dict:
    """Keep dashboard summary payloads bounded for very large branch fleets.

    ``inspection.aggregate`` already orders both collections by operational
    risk.  Preserve those arrays for the current frontend, but return only the
    highest-priority rows plus exact fleet/coverage counts.  When history is
    filtered to a lower-risk branch, retain that branch in the bounded array so
    the existing selected-branch UI can still resolve its label.
    """
    result = dict(summary or {})
    raw_branches = result.get("branches")
    branches = raw_branches if isinstance(raw_branches, list) else []
    selected_id = int(selected_branch_id) if selected_branch_id is not None else None
    selected_row = None
    computed_visited = 0
    for item in branches:
        if not isinstance(item, dict):
            continue
        if int(item.get("visits") or 0) > 0:
            computed_visited += 1
        if selected_id is not None and int(item.get("id") or 0) == selected_id:
            selected_row = item

    top_branches = [
        item for item in branches[:_INSPECTION_RISK_BRANCH_LIMIT]
        if isinstance(item, dict)
    ]
    if selected_row is not None and not any(
        int(item.get("id") or 0) == selected_id for item in top_branches
    ):
        if len(top_branches) >= _INSPECTION_RISK_BRANCH_LIMIT:
            top_branches[-1] = selected_row
        else:
            top_branches.append(selected_row)

    raw_regions = result.get("regions")
    regions = raw_regions if isinstance(raw_regions, list) else []
    top_regions = [
        item for item in regions[:_INSPECTION_REGION_SUMMARY_LIMIT]
        if isinstance(item, dict)
    ]
    total_branches = int(result.get("total_branches") or len(branches))
    visited_branches = (
        int(result["visited_branches"])
        if result.get("visited_branches") is not None
        else computed_visited
    )
    total_regions = int(result.get("total_regions") or len(regions))
    result.update({
        "branches": top_branches,
        "regions": top_regions,
        "total_branches": total_branches,
        "visited_branches": visited_branches,
        "total_regions": total_regions,
        "branch_summary_limit": _INSPECTION_RISK_BRANCH_LIMIT,
        "region_summary_limit": _INSPECTION_REGION_SUMMARY_LIMIT,
        "branches_truncated": total_branches > len(top_branches),
        "regions_truncated": total_regions > len(top_regions),
    })
    return result


@app.get("/api/inspections")
def inspection_list(
    industry_key: str | None = None,
    branch_id: int | None = None,
    region: str | None = None,
    limit: int = 40,
    before_id: int | None = None,
):
    try:
        selected, _choices = _inspection_scope(industry_key)
        uid = _inspection_actor_id()
        result = inspection.list_visits(
            TEN(), uid, selected, branch_id=branch_id, region=region,
            limit=limit, before_id=before_id,
        )
        try:
            # 门店筛选只缩小下方巡店记录；风险优先门店与
            # 区域汇总保持全局，才能直接切到另一家店。
            result["summary"] = _bounded_inspection_summary(
                inspection.aggregate(
                    TEN(),
                    uid,
                    selected,
                    branch_limit=_INSPECTION_RISK_BRANCH_LIMIT,
                    region_limit=_INSPECTION_REGION_SUMMARY_LIMIT,
                    pinned_branch_id=branch_id,
                ),
                selected_branch_id=branch_id,
            )
        except inspection.InspectionForbidden:
            result["summary"] = {"availability": False}
        return result
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@app.get("/api/inspections/{visit_id}")
def inspection_detail(visit_id: int, industry_key: str | None = None):
    try:
        selected, _choices = _inspection_scope(industry_key)
        return inspection.get_visit(
            TEN(), _inspection_actor_id(), selected, visit_id
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@app.post("/api/inspections")
async def inspection_create(
    branch_id: int = Form(...),
    visit_at: str = Form(""),
    scope: str = Form(""),
    request_key: str = Form(...),
    industry_key: str = Form(""),
    files: list[UploadFile] = File(...),
    file_slots: list[str] = Form(...),
    template_version: str = Form(...),
    observations_json: str = Form(""),
):
    try:
        selected, _choices = await db.arun(
            _inspection_scope, industry_key or None
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    uid, tid = _inspection_actor_id(), TEN()
    visit_timestamp = None
    if visit_at:
        try:
            visit_timestamp = time.mktime(time.strptime(visit_at, "%Y-%m-%d"))
        except ValueError as exc:
            raise HTTPException(400, "巡检日期格式无效") from exc
    if len(files) != len(file_slots):
        raise HTTPException(400, "上传文件与照片采集位必须一一对应")
    clean_slots = []
    for value in file_slots:
        clean = str(value or "").strip()
        if not clean or len(clean) > 80:
            raise HTTPException(400, "照片采集位格式无效")
        clean_slots.append(clean)
    if len(observations_json) > 50_000:
        raise HTTPException(400, "巡店观察值内容过长")
    try:
        observations = json.loads(observations_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "巡店观察值格式无效") from exc
    if not isinstance(observations, dict):
        raise HTTPException(400, "巡店观察值格式无效")
    raw = {
        "request_key": request_key,
        "visit_at": visit_timestamp,
        "note": scope,
        "require_checklist": True,
        "template_version": template_version,
        "file_slots": clean_slots,
        "observations": observations,
    }
    visit_id = 0
    records: list[dict] = []
    async with _persistent_upload_slot("inspection"):
        prepared = await _prepare_inspection_uploads(files)
        prepared = [
            {**item, "capture_slot": clean_slots[index]}
            for index, item in enumerate(prepared)
        ]
        try:
            shell = await _run_db_safely(
                inspection.create_visit_shell,
                tid,
                uid,
                selected,
                branch_id,
                raw,
            )
            visit_id = int(shell["id"])
            await _run_db_safely(
                _assert_inspection_http_replay_contract,
                tid,
                uid,
                selected,
                branch_id,
                visit_id,
                raw,
                prepared,
            )
            if shell.get("task_id"):
                # 同一 request_key 的重放只返回原任务，不二次落图/扣点。
                return {
                    "created": False,
                    "inspection_id": visit_id,
                    "task_id": int(shell["task_id"]),
                    "status": shell.get("status"),
                }
            if shell.get("status") == "analyzing" and shell.get("photos"):
                # 上次可能已经绑图，但在创建计费任务前中断；复用原证据。
                photo_records = [
                    {
                        key: photo.get(key)
                        for key in (
                            "storage_key", "mime_type", "byte_size", "sha256",
                            "width", "height", "caption", "capture_slot",
                            "item_code",
                        )
                    }
                    for photo in shell.get("photos") or []
                    if photo.get("phase") == "before"
                ]
            elif shell.get("status") == "preparing" and not shell.get("photos"):
                await _run_inspection_file_safely(
                    _cleanup_empty_shell_inspection_files, tid, visit_id
                )
                records = await _run_inspection_file_safely(
                    _store_inspection_images, tid, visit_id, prepared
                )
                photo_records = records
            elif shell.get("status") == "failed":
                raise inspection.InspectionConflict(
                    "这次巡店已失败，请在原任务上点击免费重试"
                )
            else:
                raise inspection.InspectionConflict(
                    "巡店请求正在处理，请刷新查看原记录"
                )
            brief = _inspection_brief(selected, shell["branch"], scope)
            result = await _run_db_then_start_worker_safely(
                _activate_inspection_job,
                tid,
                uid,
                selected,
                visit_id,
                photo_records,
                brief,
                start_worker=_start_inspection_task,
                should_start=lambda row: bool(row.get("created")),
                settle_unstarted=lambda row: _settle_inspection_failure(
                    row["task_id"], tid, uid, visit_id,
                    "巡店任务未能启动，已自动退回点数",
                ),
            )
        except billing.InsufficientPoints as exc:
            raise HTTPException(402, str(exc)) from exc
        except inspection.InspectionError as exc:
            _raise_inspection_error(exc)
        finally:
            try:
                if records:
                    await _run_inspection_file_safely(
                        _cleanup_unreferenced_inspection_images, records
                    )
            finally:
                if visit_id:
                    await _run_db_safely(
                        _abandon_empty_inspection_shell,
                        tid,
                        visit_id,
                        industry_key=selected,
                    )
    return result


@app.patch("/api/inspections/{visit_id}/issues/{issue_id}")
def inspection_action_update(visit_id: int, issue_id: int, body: dict):
    try:
        selected, _choices = _inspection_scope(body.get("industry_key"))
        action_id = int(body.get("action_id") or 0)
        if action_id < 1:
            raise inspection.InspectionError("整改任务编号无效")
        detail = inspection.get_visit(
            TEN(), _inspection_actor_id(), selected, visit_id
        )
        issue = next(
            (
                item for item in detail.get("issues") or []
                if int(item.get("id") or 0) == int(issue_id)
            ),
            None,
        )
        scoped_action = (issue or {}).get("action") or {}
        if int(scoped_action.get("id") or 0) != action_id:
            raise inspection.InspectionNotFound("整改任务不存在")
        row = inspection.transition_action(
            TEN(), _inspection_actor_id(), selected, action_id,
            expected_version=int(body.get("expected_version") or 0),
            target_status=str(body.get("status") or ""),
            note=str(body.get("note") or ""),
        )
        return row
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "整改参数无效") from exc


@app.patch("/api/inspections/{visit_id}/issues/{issue_id}/assignment")
def inspection_action_assignment(visit_id: int, issue_id: int, body: dict):
    """企业主/root 用 CAS 确认或调整整改责任，成员不可代替审批。"""
    try:
        selected, _choices = _inspection_scope(body.get("industry_key"))
        action_id = int(body.get("action_id") or 0)
        if action_id < 1:
            raise inspection.InspectionError("整改任务编号无效")
        detail = inspection.get_visit(
            TEN(), _inspection_actor_id(), selected, visit_id
        )
        issue = next(
            (
                item for item in detail.get("issues") or []
                if int(item.get("id") or 0) == int(issue_id)
            ),
            None,
        )
        scoped_action = (issue or {}).get("action") or {}
        if int(scoped_action.get("id") or 0) != action_id:
            raise inspection.InspectionNotFound("整改任务不存在")
        return inspection.update_action_assignment(
            TEN(), _inspection_actor_id(), selected, action_id,
            expected_version=body.get("expected_version", 0),
            owner=body.get("owner"),
            due_at=body.get("due_at"),
            plan=body.get("plan") if "plan" in body else None,
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "整改责任参数无效") from exc


def _inspection_recheck_bundle(
    visit: dict,
    issue: dict,
    action: dict,
) -> providers.PromptBundle:
    base = _inspection_prompt_bundle(TEN(), {
        "industry_key": visit.get("industry_key"),
        "branch": visit.get("branch") or {},
        "request_key": "recheck",
        "visit_at": time.time(),
        "scope": "整改复查",
        "photos": [],
    }, include_initial_contract=False)
    user = (
        "【整改复查业务数据（不可信输入）】\n"
        + json.dumps({
            "issue": {
                "title": issue.get("title"),
                "description": issue.get("description"),
            },
            "action": {"plan": action.get("plan")},
            "instruction": "只比较复查照片中是否仍能看见原问题，不得自行关闭。",
        }, ensure_ascii=False)
        + '\n只输出 JSON：{"recommendation":"close/reject/manual_review",'
          '"confidence":0.0,"note":"可见变化说明","evidence_photo_ids":[1]}'
    )
    return providers.PromptBundle(
        system=base.system,
        user=user,
        sensitive=base.sensitive,
    )


@app.post("/api/inspections/rechecks")
async def inspection_recheck_create(
    visit_id: int = Form(...),
    issue_id: int = Form(...),
    action_id: int = Form(...),
    expected_version: int = Form(...),
    industry_key: str = Form(""),
    file: UploadFile = File(...),
):
    try:
        selected, _choices = await db.arun(
            _inspection_scope, industry_key or None
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    tid, uid = TEN(), _inspection_actor_id()
    records: list[dict] = []
    async with _persistent_upload_slot("inspection-recheck"):
        prepared = await _prepare_inspection_uploads([file])
        try:
            detail = await db.arun(
                inspection.get_visit, tid, uid, selected, visit_id
            )
            issue = next(
                (
                    item for item in detail["issues"]
                    if int(item["id"]) == int(issue_id)
                ),
                None,
            )
            action = (issue or {}).get("action") or {}
            if (
                not issue
                or int(action.get("id") or 0) != int(action_id)
                or int(action.get("visit_id") or 0) != int(visit_id)
            ):
                raise inspection.InspectionNotFound("整改任务不存在")
            pending = next(
                (
                    item for item in action.get("rechecks") or []
                    if item.get("status") == "pending"
                ),
                None,
            )
            if pending:
                return {"ok": True, "recheck": pending, "replayed": True}
            # 先把照片安全落盘，再把整改状态切到“待复查”。
            # 否则磁盘/格式失败会留下一条没有任何证据的
            # awaiting_recheck，页面也无法继续补传。
            records = await _run_inspection_file_safely(
                _store_inspection_images, tid, visit_id, prepared
            )
            if action.get("status") != "awaiting_recheck":
                action = await _run_db_safely(
                    inspection.transition_action,
                    tid, uid, selected, action_id,
                    expected_version=expected_version,
                    target_status="awaiting_recheck",
                    note="已提交复查照片",
                )
            photos = await _run_db_safely(
                inspection.add_recheck_photos,
                tid, uid, selected, action_id, records,
            )
            cancellation = None
            try:
                # 从照片入库起就进入可收口区：bundle 构建、读图或
                # 模型调用任一阶段失败/取消，都必须留下 pending 人审锚点。
                bundle = await db.arun(
                    _inspection_recheck_bundle, detail, issue, action
                )
                images = await asyncio.to_thread(
                    _load_inspection_images,
                    tid,
                    {"photos": photos},
                    phase="recheck",
                )
                # 不只把超时参数传给 HTTP 客户端：连同模型队列等待在内，
                # 整段视觉调用都必须先于前端 120s 超时完成或降级人工复核。
                async with asyncio.timeout(
                    _INSPECTION_RECHECK_MODEL_TIMEOUT_SECONDS
                ):
                    async with _free_ai_slot("inspection-recheck"):
                        response = await providers.call_vision(
                            inspection.EMPLOYEE_IDX,
                            bundle.user,
                            images,
                            timeout=_INSPECTION_RECHECK_MODEL_TIMEOUT_SECONDS,
                            token=f"inspection-recheck:{action_id}",
                            system_prompt=bundle.system,
                            max_tokens=1000,
                        )
                providers.assert_no_private_leak(
                    response.get("text") or "", bundle.sensitive
                )
                analysis = llm.extract_json(response.get("text") or "")
            except asyncio.CancelledError as exc:
                # 照片与待复核状态已经持久化；即使客户端断开，
                # 也要先落一条人工复核记录，避免重试再写一组照片。
                cancellation = exc
                analysis = {
                    "recommendation": "manual_review",
                    "confidence": 0,
                    "note": "复查请求中断，请企业主人工对照整改前后照片",
                    "evidence_photo_ids": [int(item["id"]) for item in photos],
                }
            except Exception as exc:
                log.warning(
                    "inspection recheck degraded action_id=%s error_type=%s",
                    action_id,
                    type(exc).__name__,
                )
                analysis = {
                    "recommendation": "manual_review",
                    "confidence": 0,
                    "note": "AI复查未形成可靠判断，请企业主人工对照整改前后照片",
                    "evidence_photo_ids": [int(item["id"]) for item in photos],
                }
            analysis["evidence_photo_ids"] = [int(item["id"]) for item in photos]
            # record_recheck 是这批文件的幂等锚点。若取消恰好发生
            # 在它的 SQLite 事务进池之后，必须先观测真实提交结果，
            # 再向上传播取消；否则客户端重试会再落一组照片。
            record_operation = asyncio.create_task(db.arun(
                inspection.record_recheck,
                tid,
                uid,
                selected,
                action_id,
                analysis,
            ))
            try:
                record = await asyncio.shield(record_operation)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                record = await _drain_task_despite_cancellation(record_operation)
            if cancellation is not None:
                raise cancellation
        except inspection.InspectionError as exc:
            _raise_inspection_error(exc)
        finally:
            if records:
                await _run_inspection_file_safely(
                    _cleanup_unreferenced_inspection_images, records
                )
    return {"ok": True, "recheck": record}


@app.post("/api/inspections/rechecks/{recheck_id}/review")
def inspection_recheck_review(recheck_id: int, body: dict):
    try:
        selected, _choices = _inspection_scope(body.get("industry_key"))
        return inspection.review_recheck(
            TEN(), _inspection_actor_id(), selected, recheck_id,
            decision=str(body.get("decision") or ""),
            expected_action_version=int(body.get("expected_action_version") or 0),
            note=str(body.get("note") or ""),
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "复核参数无效") from exc


# ---------------- V51:行业老板决策看板 ----------------
def _raise_dashboard_error(exc: bossdashboard.DashboardError):
    if isinstance(exc, bossdashboard.DashboardAccessDenied):
        raise HTTPException(403, str(exc)) from exc
    if isinstance(exc, bossdashboard.DashboardValidationError):
        raise HTTPException(400, str(exc)) from exc
    raise HTTPException(404, str(exc)) from exc


def _dashboard_legacy_identity_ref(employee: dict) -> str:
    frozen = employeeidentity.snapshot(employee)
    signature = (
        frozen["idx"], frozen["key"], frozen["catalog_version"],
        frozen["name"], frozen["dept_key"], frozen["spec_sha256"],
    )
    return hashlib.sha256(
        json.dumps(signature, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:20]


def _dashboard_employee_binding(
    idx: int, identity_ref: str | None,
) -> dict | None:
    """Resolve the dashboard's compatibility ref to one exact role identity."""
    value = str(identity_ref or "").strip()
    if value == "inspection":
        employee = employeeidentity.active_employee(idx)
    elif re.fullmatch(r"[0-9a-f]{64}", value):
        employee = employeeidentity.employee_by_identity_ref(value)
        if employee and int(employee.get("idx") or 0) != int(idx):
            employee = None
    else:
        versions = getattr(departments, "identity_versions", None)
        candidates = versions(idx) if callable(versions) else []
        active = employeeidentity.active_employee(idx)
        if active and not any(
            employeeidentity.identity_ref(candidate)
            == employeeidentity.identity_ref(active)
            for candidate in candidates
        ):
            candidates = [active, *candidates]
        employee = next(
            (
                candidate for candidate in candidates
                if _dashboard_legacy_identity_ref(candidate) == value
            ),
            None,
        )
    if not employee:
        return None
    config = employees.ensure_role_config(employee)
    return {
        "employee": employee,
        "config": config,
        "legacy_identity_ref": _dashboard_legacy_identity_ref(employee),
        "public": _employee_public_contract(employee, config=config),
    }


def _dashboard_unknown_identity(row: dict) -> dict:
    return {
        "person_status": "inactive", "identity_status": "unknown",
        "identity_ref": str(row.get("identity_ref") or ""),
        "config_revision": 0, "config_sha256": "",
        "can_assign_new": False, "can_continue": False, "can_learn": False,
        "role_profile_summary": {}, "roster_status": "legacy",
        "can_assign": False,
    }


def _dashboard_enrich_employee(row: dict) -> dict:
    binding = _dashboard_employee_binding(
        int(row.get("idx") or 0), row.get("identity_ref"),
    )
    if not binding:
        return {**row, **_dashboard_unknown_identity(row)}
    return {
        **row,
        **binding["public"],
        "compat_identity_ref": binding["legacy_identity_ref"],
    }


def _task_identity_public(task_id: int, tenant_id: int) -> dict | None:
    row = db.one(
        "SELECT emp_idx," + ",".join(_TASK_IDENTITY_COLUMNS) + " "
        "FROM task WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (int(task_id), int(tenant_id)),
    )
    if not row:
        return None
    binding = employeeidentity.resolve_task_binding(row)
    if not binding:
        return _dashboard_unknown_identity(row)
    return _employee_public_contract(
        binding["employee"], config=binding["config"],
    )


def _dashboard_enrich_result(result: dict) -> dict:
    enriched = dict(result)
    enriched["employees"] = [
        _dashboard_enrich_employee(row)
        for row in result.get("employees") or []
    ]
    tenant_id = int((result.get("scope") or {}).get("tenant_id") or TEN())
    activity = []
    for row in result.get("recent_activity") or []:
        item = dict(row)
        if item.get("kind") == "task":
            identity = _task_identity_public(item["record_id"], tenant_id)
        else:
            employee = employeeidentity.active_employee(item.get("employee_idx"))
            config = employees.get_config(int(employee["idx"])) if employee else None
            identity = (
                _employee_public_contract(employee, config=config)
                if employee and config else None
            )
        if identity:
            item.update(identity)
        activity.append(item)
    enriched["recent_activity"] = activity
    return enriched


@app.get("/api/boss/dashboard/scopes")
def boss_dashboard_scopes():
    try:
        return bossdashboard.scopes(auth.current(), is_boss=_is_boss())
    except bossdashboard.DashboardError as exc:
        _raise_dashboard_error(exc)


@app.get("/api/boss/dashboard/summary")
def boss_dashboard_summary(
    tenant_id: int | None = None,
    industry_key: str | None = None,
    days: int = 30,
):
    try:
        result = bossdashboard.summary(
            auth.current(),
            is_boss=_is_boss(),
            tenant_id=tenant_id,
            industry_key=industry_key,
            days=days,
        )
        return _dashboard_enrich_result(result)
    except bossdashboard.DashboardError as exc:
        _raise_dashboard_error(exc)


@app.get("/api/boss/dashboard/employees/{employee_idx}")
def boss_dashboard_employee(
    employee_idx: int,
    identity_ref: str | None = None,
    tenant_id: int | None = None,
    industry_key: str | None = None,
    limit: int = 25,
    offset: int = 0,
    days: int = 30,
):
    try:
        translated_ref = identity_ref
        selected_binding = None
        if identity_ref not in (None, "inspection"):
            selected_binding = _dashboard_employee_binding(
                employee_idx, identity_ref,
            )
            if not selected_binding:
                raise bossdashboard.DashboardValidationError(
                    "员工身份参数无效"
                )
            translated_ref = (
                "inspection"
                if employee_idx == inspection.EMPLOYEE_IDX
                else selected_binding["legacy_identity_ref"]
            )
        result = bossdashboard.employee_detail(
            auth.current(),
            employee_idx=employee_idx,
            identity_ref=translated_ref,
            is_boss=_is_boss(),
            tenant_id=tenant_id,
            industry_key=industry_key,
            limit=limit,
            offset=offset,
            days=days,
        )
        result = dict(result)
        result["employee"] = _dashboard_enrich_employee(result["employee"])
        scope_tenant = int((result.get("scope") or {}).get("tenant_id") or TEN())
        task_items = []
        for row in (result.get("tasks") or {}).get("items") or []:
            item = dict(row)
            identity = _task_identity_public(item["id"], scope_tenant)
            if identity:
                item.update(identity)
            task_items.append(item)
        if "tasks" in result:
            result["tasks"] = {**result["tasks"], "items": task_items}
        return result
    except bossdashboard.DashboardError as exc:
        _raise_dashboard_error(exc)


# ---------------- V22:老板视角——员工产出总览(产出/token/费用) ----------------
def _emp_name_dept(idx: int):
    employee = employeeidentity.active_employee(idx)
    if employee:
        if str(employee.get("dept_key") or "") == "content":
            return employee["name"], employee.get("dept") or "内容生产部"
        nm = f"{employee.get('person','')}·{employee['name']}".strip("·")
        return nm, employee["dept_name"]
    return f"#{idx}", ""


_PRODUCTION_IDENTITY_FIELDS = (
    "employee_key",
    "employee_catalog_version",
    "employee_name_snapshot",
    "employee_dept_key",
    "employee_spec_sha256",
    "employee_identity_ref",
    "employee_config_revision",
    "employee_config_sha256",
    "person_snapshot",
    "identity_scheme",
    "bundle_sha256",
)


def _production_dept_name(dept_key: str) -> str:
    if dept_key == "content":
        return "内容生产部"
    for item in departments.list_depts():
        if str(item.get("key") or "") == dept_key:
            return str(item.get("name") or dept_key)
    return f"岗位部门·{dept_key}" if dept_key else "岗位部门待核"


def _production_compat_ref(signature: tuple) -> str:
    """One-release lookup alias for links created before 64-char refs."""
    return hashlib.sha256(
        json.dumps(
            signature[:6], ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]


def _production_identity(row: dict) -> tuple[tuple, dict]:
    """按 task 的完整岗位+配置冻结归档，绝不只按 idx 合并。"""
    idx = int(row.get("idx", row.get("emp_idx")) or 0)
    frozen = {}
    for field in _PRODUCTION_IDENTITY_FIELDS:
        value = row.get(field)
        frozen[field] = (
            int(value or 0) if field == "employee_config_revision"
            else str(value or "").strip()
        )
    signature = (idx, *(frozen[field] for field in _PRODUCTION_IDENTITY_FIELDS))
    binding = employeeidentity.resolve_task_binding({"emp_idx": idx, **frozen})
    if not binding:
        return signature, {
            "idx": idx,
            "name": f"岗位身份待核 #{idx}" if idx else "岗位身份待核",
            "dept": "岗位部门待核",
            "dept_key": "unknown",
            "catalog_version": "unknown",
            "person_status": "inactive",
            "identity_status": "unknown",
            "identity_ref": frozen["employee_identity_ref"],
            "config_revision": frozen["employee_config_revision"],
            "config_sha256": frozen["employee_config_sha256"],
            "can_assign_new": False,
            "can_continue": False,
            "can_learn": False,
            "role_profile_summary": {},
            "roster_status": "legacy",
            "can_assign": False,
            "compat_identity_ref": _production_compat_ref(signature),
        }
    employee, config = binding["employee"], binding["config"]
    identity = _employee_public_contract(employee, config=config)
    # Display the immutable work snapshot.  A reused idx may now point at a
    # different person, so production history must never read today's person.
    name = str(frozen["employee_name_snapshot"] or employee.get("name") or "")
    if str(employee.get("dept_key") or "") != "content":
        name = f"{frozen['person_snapshot']}·{name}".strip("·")
    return signature, {
        "idx": idx,
        "name": name,
        "dept": str(employee.get("dept_name") or employee.get("dept") or
                    _production_dept_name(frozen["employee_dept_key"])),
        "dept_key": frozen["employee_dept_key"],
        "catalog_version": frozen["employee_catalog_version"],
        "person_snapshot": frozen["person_snapshot"],
        "identity_scheme": frozen["identity_scheme"],
        **identity,
        "compat_identity_ref": _production_compat_ref(signature),
    }


def _production_active_identity(idx: int) -> tuple[tuple, dict]:
    employee = employeeidentity.active_employee(idx)
    if employee:
        config = employees.get_config(idx)
        return _production_identity({
            "idx": idx,
            **employeeidentity.task_fields(employee, config=config),
        })
    return _production_identity({"idx": idx})


def _production_identity_visible(public: dict) -> bool:
    """Old production routes must obey the same frozen department scope.

    Unknown/malformed historical identities are useful for internal migration
    diagnostics, but they have no authorizable business module and therefore
    must never expose titles or output previews through an owner-facing route.
    """
    dept_key = str(public.get("dept_key") or "").strip()
    if dept_key in {"", "unknown", "mixed", "__denied__"}:
        return False
    return auth.allowed(dept_key)


@app.get("/api/boss/production")
def boss_production():
    """本租户每个数字员工的产出汇总:产出条数 / tokens / 费用(USD)."""
    _need_admin()
    tid = TEN()
    agg = {}

    def _acc(_frozen_signature, public, n, tokens, cost, last):
        if not _production_identity_visible(public):
            return
        identity = (int(public["idx"]), str(public["identity_ref"]))
        a = agg.setdefault(
            identity,
            {**public, "n": 0, "tokens": 0, "cost": 0.0, "last": 0},
        )
        # One immutable role can legitimately have several config revisions.
        # Keep one role-level card and expose its newest resolved config while
        # every individual output below retains the exact frozen revision.
        if int(public.get("config_revision") or 0) > int(
            a.get("config_revision") or 0
        ):
            a.update(public)
        a["n"] += n or 0
        a["tokens"] += tokens or 0
        a["cost"] += cost or 0
        a["last"] = max(a["last"] or 0, last or 0)

    for r in db.q("SELECT sr.station_idx idx, COUNT(*) n, COALESCE(SUM(sr.tokens),0) tk, "
                  "COALESCE(SUM(sr.cost_usd),0) cost, MAX(sr.created_at) last "
                  "FROM station_run sr JOIN job j ON sr.job_id=j.id "
                  "WHERE j.tenant_id=? AND sr.status IN ('done','awaiting_review') "
                  "GROUP BY sr.station_idx", (tid,)):
        identity, public = _production_active_identity(int(r["idx"]))
        _acc(identity, public, r["n"], r["tk"], r["cost"], r["last"])
    for r in db.q(
        "SELECT emp_idx idx,employee_key,employee_catalog_version,"
        "employee_name_snapshot,employee_dept_key,employee_spec_sha256,"
        "employee_identity_ref,employee_config_revision,employee_config_sha256,"
        "person_snapshot,identity_scheme,bundle_sha256,"
        "COUNT(*) n,COALESCE(SUM(tokens),0) tk,"
        "COALESCE(SUM(cost_usd),0) cost,MAX(created_at) last FROM task "
        "WHERE tenant_id=? AND deleted_at IS NULL AND status='done' "
        "GROUP BY emp_idx,employee_key,employee_catalog_version,"
        "employee_name_snapshot,employee_dept_key,employee_spec_sha256,"
        "employee_identity_ref,employee_config_revision,employee_config_sha256,"
        "person_snapshot,identity_scheme,bundle_sha256",
        (tid,),
    ):
        identity, public = _production_identity(r)
        _acc(identity, public, r["n"], r["tk"], r["cost"], r["last"])

    rows = []
    for a in agg.values():
        rows.append({
            **{key: a.get(key) for key in (
                "idx", "name", "dept", "dept_key", "catalog_version",
                "person_snapshot", "identity_scheme",
                "person_status", "identity_status", "identity_ref",
                "config_revision", "config_sha256", "bundle_sha256", "can_assign_new",
                "can_continue", "can_learn", "role_profile_summary",
                "roster_status", "can_assign",
            )},
            "runs": a["n"],
            "tokens": a["tokens"],
            "cost_usd": round(a["cost"], 4),
            "last_at": a["last"],
        })
    rows.sort(key=lambda x: -x["cost_usd"])
    total = {"employees": len(rows), "runs": sum(r["runs"] for r in rows),
             "tokens": sum(r["tokens"] for r in rows),
             "cost_usd": round(sum(r["cost_usd"] for r in rows), 4)}
    return {"employees": rows, "total": total}


@app.get("/api/boss/production/{idx}")
def boss_production_detail(idx: int, identity_ref: str | None = None):
    """某个员工的逐条产出:标题/预览/tokens/费用/时间,可点进原件."""
    _need_admin()
    tid = TEN()
    items = []
    identities = {}
    selected_identity = None
    _active_identity, active_public = _production_active_identity(idx)
    if identity_ref is not None:
        if re.fullmatch(r"(?:[0-9a-f]{20}|[0-9a-f]{64})", identity_ref) is None:
            raise HTTPException(400, "员工身份参数无效")
        candidates = {}

        def _remember_candidate(public: dict) -> None:
            role_ref = str(public["identity_ref"])
            candidate = (role_ref, public)
            for lookup in (role_ref, public["compat_identity_ref"]):
                previous = candidates.get(lookup)
                if previous is None or int(public.get("config_revision") or 0) > int(
                    previous[1].get("config_revision") or 0
                ):
                    candidates[lookup] = candidate

        if _production_identity_visible(active_public):
            _remember_candidate(active_public)
        for frozen in db.q(
            "SELECT DISTINCT employee_key,employee_catalog_version,"
            "employee_name_snapshot,employee_dept_key,employee_spec_sha256,"
            "employee_identity_ref,employee_config_revision,employee_config_sha256,"
            "person_snapshot,identity_scheme,bundle_sha256 "
            "FROM task WHERE emp_idx=? AND tenant_id=? AND deleted_at IS NULL",
            (idx, tid),
        ):
            _signature, public = _production_identity({"idx": idx, **frozen})
            if _production_identity_visible(public):
                _remember_candidate(public)
        selected_identity = candidates.get(identity_ref)
        if selected_identity is None:
            raise HTTPException(404)

    task_sql = (
        "SELECT id,output_md,tokens,cost_usd,created_at,status,"
        "employee_key,employee_catalog_version,employee_name_snapshot,"
        "employee_dept_key,employee_spec_sha256,employee_identity_ref,"
        "employee_config_revision,employee_config_sha256,person_snapshot,"
        "identity_scheme,bundle_sha256 FROM task "
        "WHERE emp_idx=? AND tenant_id=? AND deleted_at IS NULL"
    )
    task_args: tuple = (idx, tid)
    if selected_identity is not None:
        selected_ref, _selected_public = selected_identity
        task_sql += " AND employee_identity_ref=?"
        task_args += (selected_ref,)
    task_sql += " ORDER BY id DESC LIMIT 50"
    for t in db.q(task_sql, task_args):
        _identity, public = _production_identity({"idx": idx, **t})
        if not _production_identity_visible(public):
            continue
        previous = identities.get(public["identity_ref"])
        if previous is None or int(public.get("config_revision") or 0) > int(
            previous.get("config_revision") or 0
        ):
            identities[public["identity_ref"]] = public
        md = t["output_md"] or ""
        title = next((ln.lstrip("# ").strip() for ln in md.splitlines() if ln.startswith("#")),
                     (md[:24] or "(无标题)"))
        items.append({"kind": "task", "id": t["id"], "title": title[:50], "preview": md[:240],
                      "tokens": t["tokens"] or 0, "cost_usd": round(t["cost_usd"] or 0, 4),
                      "at": t["created_at"], "status": t["status"], "employee": public})
    include_station_runs = (
        _production_identity_visible(active_public)
        and (
            selected_identity is None
            or selected_identity[0] == active_public["identity_ref"]
        )
    )
    station_rows = db.q(
        "SELECT sr.id, sr.job_id, sr.output_json, sr.tokens, sr.cost_usd, sr.created_at, "
        "sr.status, j.brief_json FROM station_run sr JOIN job j ON sr.job_id=j.id "
        "WHERE sr.station_idx=? AND j.tenant_id=? AND sr.status IN ('done','awaiting_review') "
        "ORDER BY sr.id DESC LIMIT 50", (idx, tid),
    ) if include_station_runs else []
    for r in station_rows:
        o = db.jloads(r["output_json"], {})
        direction = (db.jloads(r["brief_json"], {}) or {}).get("direction", "")
        title = (o.get("title") or (o.get("title_candidates") or [None])[0]
                 or (direction and f"工单#{r['job_id']}·{direction}") or f"工单#{r['job_id']} 产出")
        preview = (o.get("body") or json.dumps(o, ensure_ascii=False))[:240]
        _identity, public = _production_active_identity(idx)
        previous = identities.get(public["identity_ref"])
        if previous is None or int(public.get("config_revision") or 0) > int(
            previous.get("config_revision") or 0
        ):
            identities[public["identity_ref"]] = public
        items.append({"kind": "station", "id": r["id"], "job_id": r["job_id"],
                      "title": str(title)[:50], "preview": preview, "tokens": r["tokens"] or 0,
                      "cost_usd": round(r["cost_usd"] or 0, 4), "at": r["created_at"],
                      "status": r["status"], "employee": public})
    if identity_ref is not None and not items:
        raise HTTPException(404)
    items.sort(key=lambda x: -(x["at"] or 0))
    public_identities = sorted(
        identities.values(),
        key=lambda item: (
            item["identity_status"] != "current",
            item["config_revision"], item["identity_ref"],
        ),
    )
    if len(public_identities) == 1:
        public = public_identities[0]
    elif public_identities:
        public = {
            "name": "多个岗位版本",
            "dept": "多个岗位部门",
            "dept_key": "mixed",
            "catalog_version": "mixed",
            "person_status": "inactive",
            "identity_status": "unknown",
            "config_revision": 0,
            "config_sha256": "",
            "can_assign_new": False,
            "can_continue": False,
            "can_learn": False,
            "role_profile_summary": {},
            "roster_status": "mixed",
            "can_assign": False,
            "identity_ref": None,
        }
    else:
        _identity, public = _production_active_identity(idx)
        if not _production_identity_visible(public):
            raise HTTPException(404)
    return {
        "idx": idx,
        **public,
        "identities": public_identities,
        "items": items[:80],
    }


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
        if data["enabled"]:
            data["fail_streak"] = 0   # 复通后连续失败告警从头计数
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
    """Aggregate all tenant-owned persistent media without crossing tenants."""
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
    asset_root = os.path.realpath(assetfiles.ASSET_ROOT)
    inspection_root = os.path.abspath(
        os.path.join(asset_root, "inspections", str(int(tid)))
    )
    try:
        inspection_inside = (
            os.path.commonpath((asset_root, inspection_root)) == asset_root
        )
    except ValueError:
        inspection_inside = False
    if (
        inspection_inside
        and os.path.isdir(inspection_root)
        and not os.path.islink(inspection_root)
        and os.path.realpath(inspection_root) == inspection_root
    ):
        for current_root, directories, filenames in os.walk(
            inspection_root, followlinks=False
        ):
            directories[:] = [
                name
                for name in directories
                if not os.path.islink(os.path.join(current_root, name))
            ]
            for name in filenames:
                path = os.path.join(current_root, name)
                try:
                    if os.path.isfile(path) and not os.path.islink(path):
                        files += 1
                        used_bytes += os.stat(path, follow_symlinks=False).st_size
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


def _prepare_avatar_clone_sample(raw_name, tid: int) -> str:
    """Validate and privately copy a voice sample under the asset registry lock."""
    sample_descriptor = -1
    sample_path = ""
    with avatar.asset_library_lock(tid):
        name = _avatar_asset_name(raw_name, "audio_name", {"voice"})
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
                    shutil.copyfileobj(source, target, length=1 << 20)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            if sample_descriptor >= 0:
                os.close(sample_descriptor)
            try:
                os.remove(sample_path)
            except OSError:
                pass
            raise
    return sample_path


def _cleanup_avatar_clone_sample(sample_path: str, tid: int) -> None:
    try:
        os.remove(sample_path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("voice clone work sample cleanup failed tenant=%s", tid)


async def _prepare_avatar_clone_sample_safely(
    raw_name,
    tid: int,
) -> str:
    """Copy the clone sample without leaking it when the request is cancelled."""
    copy_task = asyncio.create_task(
        asyncio.to_thread(_prepare_avatar_clone_sample, raw_name, tid)
    )
    try:
        return await asyncio.shield(copy_task)
    except asyncio.CancelledError:
        sample_path = ""
        try:
            sample_path = await copy_task
        except BaseException:
            pass
        if sample_path:
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(
                    _cleanup_avatar_clone_sample,
                    sample_path,
                    tid,
                )
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
        raise


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
        p = await db.aone(
            "SELECT * FROM account_profile WHERE id=? AND tenant_id=? "
            "AND deleted_at IS NULL",
            (body["profile_id"], TEN()),
        )
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
    await db.arun(_need_module, "avatar")
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
    profile_id = await db.arun(
        _profile_id_for_tenant,
        body.get("profile_id"),
    )
    safe_body = {"style": style, "profile_id": profile_id}
    from . import linkgrab
    try:  # 防 SSRF:先卡掉内网/本机地址,再扣费(别为一次被拦的请求收钱)
        await linkgrab._guard_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        billing_op = await _start_billing_operation_safely(
            billing.start_operation,
            "link_extract",
            tid=TEN(),
            note="爆款链接提取",
            cancel_reason="爆款链接提取请求中断自动退回",
        )
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    try:
        result = await _avatar_script_from_link_work(safe_body, url, dur)
    except BaseException as exc:
        try:
            await _run_db_safely(
                billing.fail_operation,
                billing_op,
                "爆款链接提取失败自动退回",
            )
        except Exception as refund_exc:
            logging.getLogger("billing").error(
                "link extraction refund failed op=%s error_type=%s",
                billing_op,
                type(refund_exc).__name__,
            )
        raise
    await _run_db_safely(billing.complete_operation, billing_op)
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
    await db.arun(_need_module, "avatar")
    tid = TEN()
    # Validation and the private copy share the asset lock, but the potentially
    # large copy/fsync runs on the default I/O executor rather than the loop or
    # the scarce DB executor.
    sample_path = await _prepare_avatar_clone_sample_safely(
        body.get("audio_name"),
        tid,
    )

    try:
        op_key = await _start_billing_operation_safely(
            _start_billed_operation,
            "voice_clone",
            note="声音克隆",
            cancel_reason="声音克隆请求中断",
        )
    except BaseException:
        await asyncio.to_thread(_cleanup_avatar_clone_sample, sample_path, tid)
        raise
    try:
        try:
            voice = await avatar.clone_voice(
                sample_path, body.get("label") or "我的声音", save=False
            )
        finally:
            await asyncio.to_thread(
                _cleanup_avatar_clone_sample,
                sample_path,
                tid,
            )

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

        if not await _run_db_safely(
            billing.complete_operation_if_claimed,
            op_key,
            claim,
        ):
            raise RuntimeError("声音克隆本地结算状态冲突")
    except asyncio.CancelledError:
        try:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "声音克隆请求中断",
            )
        except Exception as refund_exc:
            log.error(
                "voice clone cancellation refund failed op=%s error_type=%s",
                op_key,
                type(refund_exc).__name__,
            )
        raise
    except Exception as exc:
        try:
            settled = await _run_db_safely(
                billing.fail_operation,
                op_key,
                "声音克隆失败自动退回",
            )
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
        "created_by": int((auth.current() or {}).get("id") or 0) or None,
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


def _start_avatar_job_worker(job_id: int):
    return asyncio.create_task(
        avatar.run_job(job_id, engine.broadcast)
    )


def _settle_unstarted_avatar_job(job_id: int) -> bool:
    return avatar.settle_failure(
        job_id,
        "数字人任务启动失败，系统已安全终止并退回本次点数",
    )


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
    tid = TEN()

    def create_job() -> int:
        all_voices = avatar.cloned_voices() + avatar.VOICES
        voice = next(
            (item for item in all_voices
             if item["id"] == body.get("voice_id")),
            avatar.VOICES[0],
        )
        # Validation and the charged job row form one asset-library critical
        # section. A concurrent delete can run only after the durable job
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
            params = {
                "photo_name": photo_name,
                "script": script,
                "voice_id": voice["id"],
                "voice_label": voice["label"],
                "own_audio_name": own_audio_name,
                "engine": body.get("engine") or "",
                "duration": dur,
                "prompt": (body.get("prompt") or "").strip(),
            }
            return _create_charged_avatar_job(params, tid)

    jid = await _run_db_then_start_worker_safely(
        create_job,
        start_worker=_start_avatar_job_worker,
        settle_unstarted=_settle_unstarted_avatar_job,
    )
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
    row = await db.aone(
        "SELECT tenant_id,status FROM avatar_job "
        "WHERE id=? AND deleted_at IS NULL",
        (jid,),
    )
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    if row.get("status") != "failed":
        raise HTTPException(409, "只有失败任务可以免费重试")
    prepared = await _run_db_then_start_worker_safely(
        avatar.prepare_retry,
        jid,
        TEN(),
        start_worker=lambda _prepared: _start_avatar_job_worker(jid),
        should_start=bool,
        settle_unstarted=(
            lambda _prepared: _settle_unstarted_avatar_job(jid)
        ),
    )
    if not prepared:
        current = await db.aone(
            "SELECT retry_count FROM avatar_job WHERE id=? AND tenant_id=?",
            (jid, TEN()),
        ) or {}
        if (current.get("retry_count") or 0) >= avatar.MAX_FREE_RETRIES:
            raise HTTPException(429, "该任务免费重试次数已用完，请新建任务")
        raise HTTPException(409, "这个任务已经不在失败状态了——多半是刚刚已被重试(正在排队执行)或已被删除。刷新看最新进度即可,不会重复扣点")
    engine.broadcast({"type": "avatar_update", "job_id": jid})
    current = await db.aone(
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


def _meeting_member_view(idx: int, *, binding: dict | None = None) -> dict:
    """会议花名片遵守员工资料权限：外部只显示姓名与公开介绍。"""
    employee = (
        binding.get("employee") if isinstance(binding, dict)
        else employeeidentity.active_employee(idx)
    )
    if not employee:
        return {}
    config = (
        binding.get("config") if isinstance(binding, dict)
        else employees.get_config(idx)
    )
    b = meeting.emp_brief(
        idx, active_only=True, employee=employee, config=config,
    )
    if not b:
        return {}
    identity = _employee_public_contract(employee, config=config)
    enabled = identity["person_status"] == "active"
    if _is_boss():
        return {
            k: v for k, v in b.items()
            if k != "md" and not str(k).startswith("_")
        } | identity | {"enabled": enabled}
    if employee.get("dept_key") != "content":
        public = _public_expert(employee, config=config)
        intro = public["intro"]
    else:
        intro = employee.get("intro", "")
    return (
        {k: b[k] for k in ("idx", "name", "color", "emoji")}
        | {"intro": intro}
        | identity
        | {"enabled": enabled}
    )


def _meeting_binding_snapshot(binding: dict) -> dict:
    """Freeze the exact person, role, config and effective bundle for a meeting."""
    employee = binding["employee"]
    config = binding["config"]
    frozen = employeeidentity.snapshot(employee)
    return {
        **frozen,
        "identity_ref": config["identity_ref"],
        "config_revision": config["config_revision"],
        "config_sha256": config["config_sha256"],
        "person_snapshot": str(
            frozen.get("person_snapshot") or config.get("person_snapshot") or ""
        ),
        "identity_scheme": str(
            frozen.get("identity_scheme")
            or config.get("identity_scheme")
            or "legacy-six"
        ),
        "bundle_sha256": config["bundle_sha256"],
    }


def _meeting_frozen_members(m: dict) -> list[dict] | None:
    return employeeidentity.member_snapshot_contract(
        db.jloads(m.get("emp_idxs_json"), []),
        db.jloads(m.get("member_snapshot_json"), []),
    )


def _meeting_history_member_views(m: dict) -> list[dict]:
    frozen_rows = _meeting_frozen_members(m) or []
    views = []
    for frozen in frozen_rows:
        employee = employeeidentity.resolve_snapshot(frozen)
        config = employees.get_config_by_identity(
            str(frozen.get("identity_ref") or ""),
            revision=frozen.get("config_revision"),
            config_sha256=frozen.get("config_sha256"),
        )
        if not employee or not config:
            continue
        identity = _employee_public_contract(employee, config=config)
        enabled = identity["person_status"] == "active"
        display_name = (
            f"{employee.get('person', '')}·{employee.get('name', '')}".strip("·")
            if employee.get("dept_key") != "content"
            else str(employee.get("name") or frozen["name"])
        )
        brief = {
            "idx": int(frozen["idx"]),
            "name": display_name,
            "duty": str(employee.get("duty") or ""),
            "color": str(employee.get("color") or "#7d756a"),
            "emoji": str(employee.get("emoji") or "🧑‍💼"),
        }
        if _is_boss():
            public = {
                k: v for k, v in brief.items()
                if k != "md" and not str(k).startswith("_")
            }
        else:
            public = {
                k: brief[k] for k in ("idx", "name", "color", "emoji")
            } | {"intro": str(employee.get("intro") or "岗位档案可回看")}
        views.append(public | identity | {"enabled": enabled})
    return views


def _meeting_visible(m: dict) -> bool:
    """成员账号必须拥有会议涉及的全部板块，避免混合部门会议泄露。"""
    frozen_rows = _meeting_frozen_members(m)
    if not frozen_rows:
        return False
    for frozen in frozen_rows:
        if not auth.allowed(frozen["dept_key"]):
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
        "created_by": int((auth.current() or {}).get("id") or 0) or None,
        "status": "pending_charge",
        "billing_status": "pending",
        "billing_points": points,
    })
    strict_snapshots = (
        db.jloads(data.get("member_snapshot_json"), None)
        if "member_snapshot_json" in data else None
    )
    strict_indices = (
        db.jloads(data.get("emp_idxs_json"), None)
        if "member_snapshot_json" in data else None
    )
    if strict_snapshots is not None and (
        not isinstance(strict_indices, list)
        or not isinstance(strict_snapshots, list)
        or len(strict_indices) != len(strict_snapshots)
        or len(strict_snapshots) != int(member_count)
    ):
        db.q(
            "DELETE FROM meeting WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (meeting_id,),
        )
        raise RuntimeError("会议员工冻结绑定结构无效")

    def claim(connection):
        if strict_snapshots is not None and any(
            type(idx) is not int
            or not isinstance(frozen, dict)
            or int(frozen.get("idx", -1)) != idx
            or not _role_binding_matches(
                connection, frozen, require_current=True,
            )
            for idx, frozen in zip(strict_indices, strict_snapshots)
        ):
            raise RuntimeError("会议员工岗位配置已更新，请刷新后重试")
        changed = connection.execute(
            "UPDATE meeting SET status='queued',billing_status='charged',updated_at=? "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (time.time(), meeting_id),
        )
        return changed.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            "expert_task", tid, claim, note=f"会议#{meeting_id}·圆桌会议",
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


def _start_meeting_worker(meeting_id: int):
    return asyncio.create_task(
        meeting.run(meeting_id, engine.broadcast)
    )


def _settle_unstarted_meeting(meeting_id: int) -> bool:
    return meeting.settle_failure(
        meeting_id,
        "会议启动失败，系统已安全终止并退回本次点数",
    )


def _meeting_current_write_bindings(raw_idxs, raw_bindings) -> list[dict]:
    """Validate one exact current role/config binding for every roster slot."""
    if not isinstance(raw_idxs, list) or not isinstance(raw_bindings, list):
        raise HTTPException(400, "会议成员与岗位绑定必须是数组")
    if len(raw_idxs) > meeting.MAX_MEMBERS:
        raise HTTPException(400, f"会议最多 {meeting.MAX_MEMBERS} 位成员")
    if len(raw_idxs) != len(raw_bindings):
        raise HTTPException(400, "会议成员与岗位绑定数量不一致")
    if any(type(idx) is not int for idx in raw_idxs):
        raise HTTPException(400, "会议员工编号无效")
    if len(set(raw_idxs)) != len(raw_idxs):
        raise HTTPException(400, "会议成员不得重复")
    by_idx: dict[int, dict] = {}
    for item in raw_bindings:
        if not isinstance(item, dict) or type(item.get("idx")) is not int:
            raise HTTPException(400, "会议岗位绑定结构无效")
        if any(item.get(field) in (None, "") for field in _ROLE_WRITE_BINDING_FIELDS):
            raise HTTPException(400, "会议岗位身份、配置与能力包绑定不完整")
        idx = int(item["idx"])
        if idx in by_idx:
            raise HTTPException(400, "会议岗位绑定不得重复")
        by_idx[idx] = item
    if set(by_idx) != set(raw_idxs):
        raise HTTPException(400, "会议岗位绑定存在缺失或额外成员")
    ordered = []
    for idx in raw_idxs:
        binding = _employee_current_write_binding(idx, by_idx[idx])
        if not binding["identity"].get("can_assign_new"):
            raise HTTPException(409, "会议成员岗位已变更，请刷新后重试")
        ordered.append(binding)
    return ordered


@app.post("/api/meetings")
async def meeting_create(body: dict):
    # 成员列表与身份、配置、能力包绑定必须精确一一对应，任何静默过滤都会改变参会人与计费。
    raw_idxs = body.get("emp_idxs")
    raw_bindings = body.get("member_bindings")

    def select_members() -> tuple[list[int], list[dict], list[dict]]:
        bindings = _meeting_current_write_bindings(raw_idxs, raw_bindings)
        idxs = []
        member_views = []
        for binding in bindings:
            idx = int(binding["employee"]["idx"])
            if idx == inspection.EMPLOYEE_IDX:
                raise HTTPException(400, "巡店经理不参加无照片圆桌会议")
            if not binding["config"].get("enabled", True):
                raise HTTPException(409, "会议成员已停用，请刷新后重试")
            if not meeting.emp_brief(
                idx,
                active_only=True,
                employee=binding["employee"],
                config=binding["config"],
            ):
                raise HTTPException(409, "会议成员当前不可用")
            dept_key = str(binding["employee"].get("dept_key") or "content")
            _need_module(dept_key)
            idxs.append(idx)
            member_views.append(_meeting_member_view(idx, binding=binding))
        return idxs, member_views, bindings

    idxs, member_views, bindings = await db.arun(select_members)
    q = (body.get("question") or "").strip()
    if not q or len(idxs) < 2:
        raise HTTPException(400, "议题必填,且至少拉 2 位员工进群")
    if len(q) > 2000:
        raise HTTPException(400, "议题太长了,请压缩到 2000 字以内")
    constraints = (body.get("constraints") or "").strip()[:1200]
    acceptance = (body.get("acceptance_criteria") or "").strip()[:1200]
    # 按公示价「会议每人1点」整单原子扣费；建会失败不会出现扣点无记录。
    try:
        mid = await _run_db_then_start_worker_safely(
            _create_charged_meeting,
            {
                "tenant_id": TEN(),
                "question": q,
                "constraints": constraints,
                "acceptance_criteria": acceptance,
                "emp_idxs_json": json.dumps(idxs),
                "member_snapshot_json": json.dumps(
                    [_meeting_binding_snapshot(binding) for binding in bindings],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "auto_execute": 0 if body.get("auto_execute") is False else 1,
                "phase": "queued",
                "round_no": 0,
            },
            len(idxs),
            start_worker=_start_meeting_worker,
            settle_unstarted=_settle_unstarted_meeting,
        )
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"meeting_id": mid, "members": member_views}


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

    def build_roster() -> list[dict]:
        candidates = []
        if auth.allowed("content"):
            candidates.extend(
                {"idx": s["idx"], "name": s["name"], "duty": s["duty"]}
                for s in registry.STATIONS
                if int(s.get("idx") or 0) != inspection.EMPLOYEE_IDX
            )
        for department in departments.list_depts():
            if not auth.allowed(department["key"]):
                continue
            candidates.extend(
                {
                    "idx": employee["idx"],
                    "name": f"{employee.get('person', '')}{employee['name']}",
                    "duty": employee["duty"],
                }
                for employee in department["employees"]
            )
        configs = employees.get_configs(item["idx"] for item in candidates)
        return [
            item
            for item in candidates
            if configs.get(item["idx"], {"enabled": True})["enabled"]
        ]

    roster = await db.arun(build_roster)
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
    members = await db.arun(
        lambda: [_meeting_member_view(i) for i in idxs]
    )
    return {"idxs": idxs, "members": members}


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
        "member_snapshot_json,tenant_id, created_at FROM meeting "
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
        r["task_count"] = len(meeting.validated_execution_task_ids(r))
        r["members"] = _meeting_history_member_views(r)
        r.pop("emp_idxs_json", None)
        r.pop("member_snapshot_json", None)
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
            f"SELECT id,emp_idx,status,brief_json,employee_key,"
            f"employee_catalog_version,employee_name_snapshot,"
            f"employee_dept_key,employee_spec_sha256,employee_identity_ref,"
            f"employee_config_revision,employee_config_sha256,person_snapshot,"
            f"identity_scheme,bundle_sha256,created_at FROM task "
            f"WHERE tenant_id=? AND deleted_at IS NULL "
            f"AND id IN ({marks})",
            (TEN(), *task_ids),
        )
        by_id = {r["id"]: r for r in rows}
        for tid in task_ids:
            if tid not in by_id:
                continue
            t = by_id[tid]
            binding = employeeidentity.resolve_task_binding(t)
            if (
                not binding
                or not auth.allowed(str(t.get("employee_dept_key") or ""))
            ):
                continue
            brief = db.jloads(t.pop("brief_json"), {})
            t["name"] = t.pop("employee_name_snapshot", None) or "岗位身份待核"
            t["task"] = (brief.get("direction") or "")[:360]
            t.update(_employee_public_contract(
                binding["employee"], config=binding["config"],
            ))
            tasks.append(t)
    m["execution_tasks"] = tasks
    m["members"] = _meeting_history_member_views(m)
    m.pop("emp_idxs_json", None)
    m.pop("member_snapshot_json", None)
    return m


@app.post("/api/meetings/{mid}/execute")
async def meeting_execute(mid: int):
    m = await db.arun(_meeting_row_or_404, mid)
    if m.get("phase") == "completed":
        return {
            "ok": True,
            "task_ids": await db.arun(
                meeting.validated_execution_task_ids, m, repair=True
            ),
        }
    if not await db.arun(meeting.claim_execution, mid):
        raise HTTPException(409, "会议尚未形成可执行决定,或执行已经启动")
    try:
        task_ids = await meeting.execute_actions(mid, engine.broadcast)
    except Exception:
        await db.aupdate(
            "meeting", mid, {"status": "done", "phase": "awaiting_execution"}
        )
        raise HTTPException(500, "派活启动失败,请稍后重试")
    return {"ok": True, "task_ids": task_ids}


@app.post("/api/meetings/{mid}/ask")
async def meeting_ask(mid: int, body: dict):
    m = await db.arun(_meeting_row_or_404, mid)
    q = (body.get("question") or "").strip()
    if not q:
        raise HTTPException(400, "先输入您想追问或挑战的话")
    if len(q) > 1000:
        raise HTTPException(400, "追问太长了,请压缩到 1000 字以内")
    # 追问会让全部参会成员各答一轮,按人头扣(原来只扣1点,6人会每次追问漏收5点)
    n = len(db.jloads(m.get("emp_idxs_json"), [])) or 1
    try:
        billing_op = await _run_db_then_start_worker_safely(
            meeting.begin_intervention,
            mid,
            q,
            n,
            start_worker=lambda op_key: asyncio.create_task(
                meeting.ask(mid, q, engine.broadcast, op_key)
            ),
            should_start=bool,
            settle_unstarted=lambda op_key: meeting.abort_intervention(
                mid,
                op_key,
                "会议追问启动失败，点数已退回",
            ),
        )
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    if not billing_op:
        raise HTTPException(
            409,
            f"会议正在运行,或已达到最多 {meeting.MAX_INTERVENTIONS} 次介入",
        )
    return {"ok": True}


# ---------------- V10:任务产出 编辑/导出/入库 ----------------
def _task_or_404(tid: int) -> dict:
    return _task_row_or_404(tid)


@app.put("/api/tasks/{tid}/output")
def task_output_edit(tid: int, body: dict):
    task = _task_or_404(tid)
    if int(task.get("emp_idx") or 0) == inspection.EMPLOYEE_IDX:
        raise HTTPException(
            409,
            "巡店结论来自照片证据链，不能在通用编辑器改写",
        )
    raw_md = body.get("md")
    if not isinstance(raw_md, str):
        raise HTTPException(400, "交付内容格式无效")
    stored_md = raw_md.strip()
    decision_summary = None
    frozen_employee = task.get("_frozen_employee")
    if departments.is_decision_employee(frozen_employee):
        # Manual correction is still a V2 delivery boundary.  Revalidate the
        # frozen manifest; corruption or absence deliberately becomes a
        # provenance-less gate call and therefore HOLD, never a bypass to GO.
        provenance = None
        try:
            _brief, provenance = taskrunner.validate_persisted_task_brief(
                db.jloads(task.get("brief_json"), None),
                frozen_employee,
                int(task.get("tenant_id") or TEN()),
            )
        except ValueError:
            provenance = None
        gate = departments.enforce_decision_output(
            frozen_employee, stored_md, provenance=provenance
        )
        stored_md = gate["output"]
        decision_summary = "\n".join(
            taskrunner._decision_summary_lines(
                stored_md, frozen_employee, decision_gate=gate
            )
        )
    with db.atomic() as connection:
        current = connection.execute(
            "SELECT id,status,thread_id FROM task WHERE id=? AND tenant_id=? "
            "AND deleted_at IS NULL",
            (tid, TEN()),
        ).fetchone()
        if not current:
            raise HTTPException(404)
        current = dict(current)
        if current.get("status") != "done":
            raise HTTPException(409, "只有已交付任务才能手工编辑")
        if current.get("thread_id") is not None:
            raise HTTPException(
                409,
                "连续协作的每轮交付是不可改的版本记录；请用“继续沟通”生成下一轮",
            )
        if decision_summary is None:
            changed = connection.execute(
                "UPDATE task SET output_md=?,updated_at=? WHERE id=? AND tenant_id=? "
                "AND status='done' AND thread_id IS NULL AND deleted_at IS NULL",
                (stored_md, time.time(), tid, TEN()),
            )
        else:
            changed = connection.execute(
                "UPDATE task SET output_md=?,summary_md=?,updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='done' "
                "AND thread_id IS NULL AND deleted_at IS NULL",
                (stored_md, decision_summary, time.time(), tid, TEN()),
            )
        if changed.rowcount != 1:
            raise HTTPException(409, "任务版本刚刚发生变化，请刷新后重试")
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
    existing_user, existing_apply = await asyncio.gather(
        db.aone("SELECT id FROM users WHERE username=?", (phone,)),
        db.aone(
            "SELECT id FROM account_apply "
            "WHERE phone=? AND (status=0 OR username IS NOT NULL)",
            (phone,),
        ),
    )
    if existing_user or existing_apply:
        return {"ok": True, "msg": "这个手机号已申请过 / 已有账号啦:直接登录就行;"
                                   "忘了密码或还没收到账号,联系客服帮您处理"}
    # 同IP日限:防脚本刷号(不重复的申请才计次,已去重的重复提交不占额度)
    if _apply_ip_over_limit(_client_ip(request)):
        return {"ok": True, "msg": "今天的申请次数已达上限,明天再试,或直接联系客服帮您开通"}
    recent = await db.aone(
        "SELECT id FROM account_apply WHERE phone=? AND created_at>?",
        (phone, time.time() - 3600),
    )
    if recent:
        return {"ok": True, "msg": "申请已收到,我们会在 1 个工作日内联系您开通账号"}
    aid = await db.ainsert(
        "account_apply",
        {"phone": phone, "name": name, "company": company, "note": note},
    )
    await db.arun(
        funnel.record_safe,
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
    auto_approve, daily_cap = await asyncio.gather(
        db.aget_setting("auto_approve_apply"),
        db.aget_setting("auto_approve_daily_cap"),
    )
    if auto_approve == "1":
        cap = int(float(daily_cap or 20))
        today = int(time.time() // 86400)
        if _auto_opens[1] != today:
            _auto_opens[0], _auto_opens[1] = 0, today
        if _auto_opens[0] >= cap:
            # 当天自动名额已满 → 申请保留为待处理,转老板人工「⚡一键开通」
            return {"ok": True, "msg": "今日体验名额已满,已转人工,我们会尽快为您开通"}
        try:
            a, trial_setting = await asyncio.gather(
                db.aone("SELECT * FROM account_apply WHERE id=?", (aid,)),
                db.aget_setting("trial_points"),
            )
            trial = float(trial_setting or 20)
            r = await db.arun(
                _open_account_from_apply, a, trial_points=trial
            )
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
    old = await db.aone("SELECT * FROM guests WHERE phone=?", (phone,))
    if old and old["used"]:
        raise HTTPException(403, "这个手机号已经体验过啦,想继续用请联系我们开通账号")
    created = False
    if old:
        gid = old["id"]
    else:
        if not _claim_guest_trial_slot(_client_ip(request)):
            raise HTTPException(429, "今日免费体验名额已达上限，请明天再试或申请正式账号")
        def _register_guest():
            with db.atomic() as connection:
                existing = connection.execute(
                    "SELECT id,used FROM guests WHERE phone=? "
                    "ORDER BY id LIMIT 1",
                    (phone,),
                ).fetchone()
                if existing:
                    if existing["used"]:
                        raise HTTPException(
                            403,
                            "这个手机号已经体验过啦,想继续用请联系我们开通账号",
                        )
                    return existing["id"], False
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
                return cursor.lastrowid, True

        gid, created = await db.arun(_register_guest)
    if created:
        await db.arun(
            funnel.record_safe,
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
    g = await db.aone("SELECT * FROM guests WHERE id=?", (gid,))
    if not g or g["used"]:
        raise HTTPException(403, "体验次数已用完,联系我们开通账号继续用")
    q = (body.get("question") or "").strip()[:500]
    if not q:
        raise HTTPException(400, "先输入您想问的问题")
    claimed = await db.aexecute(
        "UPDATE guests SET used=1,updated_at=? WHERE id=? AND used=0",
        (time.time(), gid),
    )
    if claimed != 1:
        raise HTTPException(403, "体验次数已用完,联系我们开通账号继续用")
    from . import providers
    content_count = len(registry.STATIONS)
    expert_count = len(departments.specialists())
    industry_count = len(departments.list_depts())
    prompt = (f"你是「派活 PaiHuo」的金牌数字员工,正在给一位来体验的老板露一手。"
              f"老板的问题:{q}\n要求:直接给出专业、可落地的回答(300字内,结构清晰);"
              f"结尾用一句话自然带出:平台上还有{content_count + expert_count}位数字员工"
              f"({content_count}个内容岗位/{industry_count}个行业的{expert_count}位产业专家/数字人视频),"
              f"开通账号就能把活派给他们。")
    try:
        r = await providers.call_text(
            None, prompt, timeout=180, token=f"guest:{gid}"
        )
    except Exception:
        await db.aexecute(
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
        # 成片视频一并入包:此前"全平台发布包"独缺视频,老板只能网页右键另存
        for tv_row in db.q(
                "SELECT id,video_file FROM tv_job WHERE job_id=? AND tenant_id=? "
                "AND status='done' AND video_file IS NOT NULL",
                (job_id, TEN())):
            try:
                clip = assetfiles.resolve_tenant_asset(
                    tv_row["video_file"], TEN())
            except assetfiles.AssetAccessError:
                continue
            z.write(clip,
                    f"成片视频/成片{tv_row['id']}{os.path.splitext(clip)[1]}")
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
    p = await db.aone(
        "SELECT * FROM account_profile WHERE id=? AND deleted_at IS NULL",
        (pid,),
    )
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
    await db.aupdate(
        "account_profile",
        pid,
        {"persona_json": json.dumps(persona, ensure_ascii=False)},
    )
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
    user = auth.current() or {}
    # member 只订阅自己板块相关的事件;owner/root 传 None = 全收
    modules = (frozenset(user.get("modules") or [])
               if user.get("role") == "member" else None)
    engine.subscribers[q_] = (
        TEN(),
        _is_boss(),
        modules,
        str(user.get("role") or ""),
    )

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
        "tenant_id", "job_id", "kind", "platform", "title", "verdict", "score",
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
            job_id=job_id,
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
            ) | {"job_id": int(delivery["job_id"])},
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
            ) | {"job_id": int(current["job_id"])},
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
    await db.arun(_need_module, "content")
    delivery = await db.arun(_wechat_delivery_or_404, delivery_id)
    if delivery["status"] in ("done", "blocked"):
        return _wechat_delivery_result(delivery)
    if delivery["status"] == "submitted":
        try:
            if not await _run_db_safely(_finalize_wechat_delivery, delivery):
                raise RuntimeError("草稿台账结算状态冲突")
        except Exception as exc:
            raise HTTPException(503, "好消息:文章已经进入公众号草稿箱✅ 系统记录稍后自动补齐,请勿重复发送") from exc
        return _wechat_delivery_result(
            await db.arun(_wechat_delivery_or_404, delivery_id)
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
    if not await _run_db_safely(
        _mark_wechat_submitted,
        delivery_id,
        delivery["op_key"],
        media_id,
    ):
        raise HTTPException(409, "这篇的发送状态刚刚更新了(可能已送达或已退点),刷新页面看最新结果,别重复点发送")
    delivery = await db.arun(_wechat_delivery_or_404, delivery_id)
    try:
        if not await _run_db_safely(_finalize_wechat_delivery, delivery):
            raise RuntimeError("草稿台账结算状态冲突")
    except Exception as exc:
        raise HTTPException(503, "文章已确认在公众号草稿箱✅ 系统记录稍后自动补齐,请勿重复发送") from exc
    return _wechat_delivery_result(
        await db.arun(_wechat_delivery_or_404, delivery_id)
    )


@app.post("/api/wechat-deliveries/{delivery_id}/confirm-not-delivered")
async def confirm_wechat_delivery_not_delivered(delivery_id: int, body: dict):
    """管理员人工确认微信无草稿后退款解锁；提交前再做一次只读核对。"""
    await db.arun(_need_module, "content")
    _need_admin()
    delivery = await db.arun(_wechat_delivery_or_404, delivery_id)
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
        if not await _run_db_safely(
            _mark_wechat_submitted,
            delivery_id,
            delivery["op_key"],
            media_id,
        ):
            raise HTTPException(409, "这篇的发送状态刚刚更新了(可能已送达或已退点),刷新页面看最新结果,别重复点发送")
        delivery = await db.arun(_wechat_delivery_or_404, delivery_id)
        try:
            if not await _run_db_safely(_finalize_wechat_delivery, delivery):
                raise RuntimeError("草稿台账结算状态冲突")
        except Exception as exc:
            raise HTTPException(503, "已在公众号草稿箱里找到这篇文章✅ 系统记录稍后自动补齐,请勿重复发送") from exc
        return _wechat_delivery_result(
            await db.arun(_wechat_delivery_or_404, delivery_id)
        )
    try:
        settled = await _run_db_safely(
            _fail_wechat_delivery,
            delivery,
            "管理员人工核对公众号草稿箱后确认未送达",
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
    await db.arun(_job_or_404, job_id)
    theme = body.get("theme") or mplayout.DEFAULT_THEME
    tid = TEN()
    conf = await db.arun(wechat.get_conf, tid if tid != 1 else 1)
    if not (conf.get("appid") and conf.get("secret")):
        raise HTTPException(400, "还没配置公众号:去「📣 发布渠道」页填 AppID/AppSecret(1分钟搞定)")
    p = await db.arun(_mp_payload, job_id, theme)
    request_hash = _wechat_delivery_hash(job_id, theme, p)
    # 先处理同工单的任意旧投递。老板若在不确定期间改了正文/hash，也不能
    # 绕过旧锚点再扣一次，必须先核对或人工解锁旧投递。
    existing = await db.arun(_wechat_delivery_active, tid, job_id)
    if not existing:
        existing = await db.arun(
            _wechat_delivery_latest, tid, job_id, request_hash
        )
    if existing and existing["status"] in ("done", "blocked"):
        return _wechat_delivery_result(existing)
    if existing and existing["status"] == "submitted":
        try:
            if not await db.arun(_finalize_wechat_delivery, existing):
                raise RuntimeError("草稿台账结算状态冲突")
        except Exception as exc:
            raise HTTPException(
                503, "文章已经进入公众号草稿箱✅ 系统记录稍后自动补齐;请勿再点发送,不会重复扣点"
            ) from exc
        return _wechat_delivery_result(
            await db.aone(
                "SELECT * FROM wechat_draft_delivery WHERE id=?",
                (existing["id"],),
            )
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
        if not await db.arun(
                _mark_wechat_submitted,
                existing["id"], existing["op_key"], media_id):
            raise HTTPException(409, "这篇的发送状态刚刚更新了,刷新页面看最新结果;确实没送达的话再重试,不会重复扣点")
        existing = await db.aone(
            "SELECT * FROM wechat_draft_delivery WHERE id=?", (existing["id"],)
        )
        try:
            await db.arun(_finalize_wechat_delivery, existing)
        except Exception as exc:
            raise HTTPException(
                503, "文章已确认在公众号草稿箱✅ 系统记录稍后自动补齐,请勿重复发送"
            ) from exc
        return _wechat_delivery_result(
            await db.aone(
                "SELECT * FROM wechat_draft_delivery WHERE id=?",
                (existing["id"],),
            )
        )
    if existing and existing["status"] in ("processing", "pending_charge"):
        raise HTTPException(409, "这篇内容正在发送公众号草稿箱，请勿重复点击")
    try:
        delivery, created = await db.arun(
            _create_wechat_delivery, tid, job_id, request_hash, p["title"]
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
            if not await db.arun(
                    _complete_blocked_wechat_delivery, delivery, report):
                raise RuntimeError("草稿审查结算状态冲突")
            return _wechat_delivery_result(
                await db.aone(
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
                image_bytes = await asyncio.to_thread(_read_file_bytes, lp)
                wx_url = await wechat.upload_content_img(
                    tid, image_bytes, os.path.basename(lp)
                )
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
            cover_bytes = await asyncio.to_thread(
                _read_file_bytes, cover_lp
            )
        else:
            color = (mplayout.THEMES.get(theme) or {}).get("color", "#ff7f2a")
            cover_bytes = await asyncio.to_thread(
                wechat.make_cover_png, p["title"], color
            )
        thumb_id = await wechat.upload_thumb(tid, cover_bytes)
        # ④ 进草稿箱 + 登记发布台账(T+1/3/7 自动复盘的钟表从此起算)
        marker = f"<!-- paihuo-draft:{delivery['request_key']} -->"
        html = html + "\n" + marker
        changed = await db.aexecute(
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
        if not await db.arun(
                _mark_wechat_submitted,
                delivery["id"], delivery["op_key"], media_id):
            raise HTTPException(
                503, "文章已经进入公众号草稿箱✅ 系统记录稍后自动补齐;请勿再点发送,不会重复扣点"
            )
        delivery = await db.aone(
            "SELECT * FROM wechat_draft_delivery WHERE id=?", (delivery["id"],)
        )
        try:
            if not await db.arun(_finalize_wechat_delivery, delivery):
                raise RuntimeError("草稿台账结算状态冲突")
        except Exception as exc:
            raise HTTPException(
                503, "草稿已进入公众号，平台台账正在补记；稍后重试不会重复发送"
            ) from exc
        return _wechat_delivery_result(
            await db.aone(
                "SELECT * FROM wechat_draft_delivery WHERE id=?",
                (delivery["id"],),
            )
        )
    except wechat.WeChatError as e:
        try:
            settled = await db.arun(
                _fail_wechat_delivery, delivery, f"微信明确拒绝:{e}"
            )
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
                await db.arun(
                    _fail_wechat_delivery,
                    delivery,
                    "请求中断，尚未提交微信",
                )
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
                settled = await db.arun(
                    _fail_wechat_delivery, delivery, "投递前校验失败"
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
        raise exc
    except Exception as exc:
        if not external_started:
            try:
                settled = await db.arun(
                    _fail_wechat_delivery,
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
    await db.arun(_need_module, "content")
    tid = TEN()
    title = (body.get("title") or "").strip()[:120]
    text = (body.get("body") or "").strip()
    if not text:
        raise HTTPException(400, "先贴上要审查的正文")
    platform = body.get("platform") or "公众号"
    op_key = await _start_billing_operation_safely(
        _start_billed_operation,
        "censor_check",
        note=f"{platform}·{title[:14]}",
        cancel_reason="审查请求中断",
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

        if not await _run_db_safely(
            billing.complete_operation_if_claimed,
            op_key,
            claim,
        ):
            raise RuntimeError("审查结算状态冲突")
    except asyncio.CancelledError:
        try:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "审查请求中断",
            )
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
            settled = await _run_db_safely(
                billing.fail_operation,
                op_key,
                "审查失败自动退回",
            )
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
            settled = await _run_db_safely(
                billing.fail_operation,
                op_key,
                "审查失败自动退回",
            )
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
    await db.arun(_need_module, "content")
    tid = TEN()
    data_text = (body.get("data_text") or "").strip()
    if not data_text:
        raise HTTPException(400, "先把发布后的数据贴进来(阅读/点赞/评论/涨粉…截图可先传文件识别)")
    platform = body.get("platform") or "公众号"
    title = (body.get("title") or "")[:120]
    op_key = await _start_billing_operation_safely(
        _start_billed_operation,
        "censor_retro",
        note=platform,
        cancel_reason="复盘请求中断",
    )
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

        if not await _run_db_safely(
            billing.complete_operation_if_claimed,
            op_key,
            claim,
        ):
            raise RuntimeError("复盘结算状态冲突")
    except asyncio.CancelledError:
        try:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "复盘请求中断",
            )
        except Exception as refund_exc:
            log.error(
                "censor retro cancellation refund failed op=%s error_type=%s",
                op_key,
                type(refund_exc).__name__,
            )
        raise
    except Exception as exc:
        try:
            settled = await _run_db_safely(
                billing.fail_operation,
                op_key,
                "复盘失败自动退回",
            )
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
    await db.arun(_job_or_404, job_id)
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        raise HTTPException(400, "图片地址无效")
    r = await db.aone(
        "SELECT * FROM station_run WHERE job_id=? AND station_idx=5 "
        "AND status IN ('done','awaiting_review') "
        "ORDER BY version DESC LIMIT 1",
        (job_id,),
    )
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
    fpath = await asyncio.to_thread(
        imagehunt._save_job_file, job_id, name, data
    )
    out = db.jloads(r["output_json"], {})
    entry = {"slot": "真实图补充", "desc": (body.get("query") or "")[:40], "platform": "通用",
             "file": fpath, "engine": "真实图·全网抓取", "source": body.get("page") or url}
    out["images"] = (out.get("images") or []) + [entry]
    await db.aupdate(
        "station_run",
        r["id"],
        {"output_json": json.dumps(out, ensure_ascii=False)},
    )
    engine.touch(job_id)
    return {"ok": True, "image": entry}


# ================ V25:成片 / 台账 / 工具箱 / 矩阵发布 / 通知 ================
from . import growth, matrixpub, notify, pubtrack, textvideo  # noqa: E402


def _start_publish_worker(task_id: int):
    return asyncio.create_task(
        matrixpub.run_task(task_id, engine.broadcast)
    )


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
        "created_by": int((auth.current() or {}).get("id") or 0) or None,
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
            job_id=job_id,
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


def _start_text_video_worker(tvid: int):
    return asyncio.create_task(
        textvideo.run_job(tvid, engine.broadcast)
    )


def _settle_unstarted_text_video(tvid: int) -> bool:
    return textvideo.settle_failure(
        tvid,
        "成片任务启动失败，系统已安全终止并退回本次点数",
    )


@app.post("/api/jobs/{job_id}/text-video")
async def job_text_video(job_id: int, body: dict):
    _need_module("content")
    bgm = _validated_tv_bgm(body)
    tid = TEN()

    def create_job_video() -> int:
        _job_or_404(job_id)
        delivery = build_delivery(job_id)
        version = next(
            (
                item for item in (delivery.get("versions") or [])
                if item.get("platform") in ("抖音", "视频号")
            ),
            None,
        )
        text = (version or {}).get("body") or delivery["body"]
        if not (text or "").strip():
            raise HTTPException(400, "工单还没有定稿正文")
        images = []
        for image in delivery.get("images") or []:
            file_url = image.get("file") or ""
            if not file_url.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            try:
                assetfiles.resolve_tenant_asset(
                    file_url,
                    tid,
                    expected_job_id=job_id,
                    allowed_extensions=(".png", ".jpg", ".jpeg", ".webp"),
                )
            except assetfiles.AssetAccessError as exc:
                raise HTTPException(400, str(exc)) from exc
            images.append(assetfiles.canonical_file_url(file_url))
        params = {
            "title": delivery["title"],
            "body": text,
            "images": images,
            "voice_id": body.get("voice_id") or "",
            "image_query": delivery["title"][:16],
            "bgm": bgm,
        }
        return _create_charged_tv_job(
            params,
            tenant_id=tid,
            job_id=job_id,
            note=delivery["title"][:16],
        )

    tvid = await _run_db_then_start_worker_safely(
        create_job_video,
        start_worker=_start_text_video_worker,
        settle_unstarted=_settle_unstarted_text_video,
    )
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
        tid = TEN()

        def create_clip_video() -> int:
            # Selection and persistence share the same tenant lock with DELETE,
            # so a paid job can never be born pointing at a just-removed clip.
            with textvideo.clip_library_lock(tid):
                clips = []
                seen = set()
                for name in raw_clips[:50]:
                    path = textvideo.resolve_clip_path(tid, name)
                    if path and path not in seen:
                        seen.add(path)
                        clips.append(path)
                    if len(clips) >= 12:
                        break
                if not clips:
                    raise HTTPException(400, "先勾选至少 1 段素材视频")
                params = {
                    "mode": "clips",
                    "clips": clips,
                    "title": (body.get("title") or topic or "")[:40],
                    "script": script[:2000],
                    "topic": topic[:60],
                    "voice_id": body.get("voice_id") or "",
                    "bgm": bgm,
                    "end_text": (body.get("end_text") or "")[:40],
                }
                return _create_charged_tv_job(
                    params,
                    tenant_id=tid,
                    note=(params.get("title") or script or topic)[:16],
                )

        tvid = await _run_db_then_start_worker_safely(
            create_clip_video,
            start_worker=_start_text_video_worker,
            settle_unstarted=_settle_unstarted_text_video,
        )
    else:
        if len(script) < 20:
            raise HTTPException(400, "口播稿太短(至少20字)")
        params = {"title": (body.get("title") or "")[:40], "script": script[:2000],
                  "voice_id": body.get("voice_id") or "",
                  "image_query": (body.get("image_query") or body.get("title") or "")[:30],
                  "bgm": bgm,
                  "end_text": (body.get("end_text") or "")[:40]}
        tvid = await _run_db_then_start_worker_safely(
            _create_charged_tv_job,
            params,
            tenant_id=TEN(),
            note=(params.get("title") or script or topic)[:16],
            start_worker=_start_text_video_worker,
            settle_unstarted=_settle_unstarted_text_video,
        )
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
def publog_list(limit: int = None, offset: int = 0):
    """发布台账此前硬截 60 条且不告知;接入标准分页契约,老板能翻到月初。"""
    _need_module("content")
    page_limit, page_offset, paged = _pagination(limit, offset, 60)
    rows = pubtrack.entries(TEN(), limit=page_limit, offset=page_offset)
    if not paged:
        return rows
    return _page_result(rows, pubtrack.entries_total(TEN()),
                        page_limit, page_offset)


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
    r = await db.aone(
        "SELECT * FROM publish_log WHERE id=? AND tenant_id=?",
        (pid, TEN()),
    )
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


def _settle_unstarted_tool_result(result: dict) -> bool:
    job_id = int((result or {}).get("job_id") or 0)
    row = db.one("SELECT * FROM tool_job WHERE id=?", (job_id,))
    if not row:
        return False
    return _settle_tool_failure(
        row,
        "工具任务启动失败，系统已安全终止并退回本次点数",
        "启动失败退回",
    )


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


def _recover_stale_tool_jobs(
    now: float = None, defer_broadcast: bool = False
) -> int | tuple[int, list[tuple[int, str]]]:
    """按创建时间执行绝对总时限；心跳只供展示，不能把截止时间越续越长。"""
    now = now or time.time()
    recovered = 0
    events = []
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
                if defer_broadcast:
                    events.append((row["tenant_id"], row["kind"]))
                else:
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
    return (recovered, events) if defer_broadcast else recovered


async def _tool_watchdog_loop():
    while True:
        try:
            await asyncio.sleep(60)
            _recovered, events = await db.arun(
                _recover_stale_tool_jobs, None, True
            )
            for tenant_id, kind in events:
                _broadcast_tool(tenant_id, kind)
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
        row = await db.aone("SELECT * FROM tool_job WHERE id=?", (jid,))
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
            db.submit_write(
                db.execute,
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
        def _commit_result():
            with db.atomic() as connection:
                return _persist_tool_result(
                    connection, row, r, time.time()
                )

        changed = await db.arun(_commit_result)
        if changed:
            try:
                await asyncio.to_thread(
                    notify.push,
                    tid,
                    "report",
                    {
                        "report_name": (
                            f"{TOOL_KINDS.get(kind, kind)}跑完了"
                        ),
                        "summary": "结果已经摆在工具箱里,回来就能看",
                        "link": "#/tools",
                    },
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
        await db.arun(
            _settle_tool_failure,
            row,
            f"运行超过{minutes}分钟仍未完成，已自动结束并退回点数，请重试",
            "超时自动退回",
        )
        try:
            log.warning("tool_job %s(%s) timed out", jid, kind)
        except Exception:
            pass
    except asyncio.CancelledError:
        await db.arun(
            _settle_tool_failure,
            row,
            "任务被服务中断，已自动结束，请重新发起",
            "服务中断退回",
        )
        raise
    except Exception as e:
        # 先收口状态和退款，再记日志；即使日志组件自身出错，用户也不会再看到永久 running。
        public_error = providers.public_failure_message(e)
        await db.arun(
            _settle_tool_failure,
            row,
            public_error,
            "后台任务失败退回",
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


def _tool_enqueue_record(kind: str, params: dict, note: str = "") -> dict:
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
            "created_by": int((auth.current() or {}).get("id") or 0) or None,
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
            action, tid, claim,
            note=(f"工具单#{jid}·{note}" if note else f"工具单#{jid}")[:160],
            points=points
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
    return {"job_id": jid, "note": "已挂到后台跑:您随便去忙别的,回工具箱就能看到;跑完还会推微信"}


def _tool_enqueue(kind: str, params: dict, note: str = "") -> dict:
    """同步兼容入口；HTTP 协程使用 ``_tool_enqueue_async``。"""
    result = _tool_enqueue_record(kind, params, note)
    _spawn_tool_worker(result["job_id"])
    return result


async def _tool_enqueue_async(
    kind: str, params: dict, note: str = ""
) -> dict:
    """完整扣费事务进 DB 池，回到事件循环后再创建 asyncio worker。"""
    await db.arun(_tool_require_idle, kind)
    result = await _run_db_then_start_worker_safely(
        _tool_enqueue_record,
        kind,
        params,
        note,
        start_worker=lambda queued: _spawn_tool_worker(queued["job_id"]),
        settle_unstarted=_settle_unstarted_tool_result,
    )
    return result


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
    await db.arun(_need_module, "content")
    ym = body.get("ym") or time.strftime("%Y-%m")
    return await _tool_enqueue_async(
        "pcal",
        {
            "ym": ym,
            "industry": (body.get("industry") or "通用")[:20],
            "focus": (body.get("focus") or "")[:200],
        },
        note=ym,
    )


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
    await db.arun(_need_module, "content")
    industry = (body.get("industry") or "通用")[:20]
    return await _tool_enqueue_async(
        "hot",
        {
            "industry": industry,
            "channels": (body.get("channels") or [])[:10],
        },
        note=industry,
    )


@app.post("/api/tools/warmup")
async def warmup_gen(body: dict):
    await db.arun(_need_module, "content")
    persona_text = ""
    if body.get("profile_id"):
        pr = await db.aone(
            "SELECT * FROM account_profile WHERE id=? AND tenant_id=? "
            "AND deleted_at IS NULL",
            (body["profile_id"], TEN()),
        )
        if pr:
            persona_text = registry._persona_text({"persona": db.jloads(pr["persona_json"], {})})
    return await _tool_enqueue_async(
        "warm",
        {
            "platform": body.get("platform") or "小红书",
            "industry": (body.get("industry") or "通用")[:20],
            "positioning": (body.get("positioning") or "")[:200],
            "persona_text": persona_text[:1500],
        },
        note=(body.get("platform") or "") + (body.get("industry") or ""),
    )


@app.post("/api/tools/leads")
async def leads_gen(body: dict):
    await db.arun(_need_module, "content")
    return await _tool_enqueue_async(
        "leads",
        {
            "industry": (body.get("industry") or "通用")[:20],
            "city": (body.get("city") or "")[:20],
            "product": (body.get("product") or "")[:60],
        },
        note=(body.get("city") or "") + (body.get("industry") or ""),
    )


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
    await db.arun(_need_module, "content")
    if not (await db.arun(growth.watch_conf, TEN())).get("targets"):
        raise HTTPException(400, "先在上面添加要盯的对标账号并保存")
    return await _tool_enqueue_async("bench", {}, note="手动")


def _tool_image_base64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode()


def _store_tool_image(data: bytes, tid: int) -> tuple[str, str]:
    """Durably store one generated image and remove partial writes on failure."""
    import uuid
    directory = os.path.join(ROOT, "data", "assets", "tools", str(tid))
    os.makedirs(directory, exist_ok=True)
    name = f"shot_{uuid.uuid4().hex[:10]}.png"
    path = os.path.join(directory, name)
    try:
        with open(path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path, name


def _remove_tool_image(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


async def _store_tool_image_safely(data: bytes, tid: int) -> tuple[str, str]:
    """Keep blocking writes off-loop and avoid an orphan on cancellation."""
    write_task = asyncio.create_task(
        asyncio.to_thread(_store_tool_image, data, tid)
    )
    try:
        return await asyncio.shield(write_task)
    except asyncio.CancelledError:
        stored = None
        try:
            stored = await write_task
        except BaseException:
            pass
        if stored:
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(_remove_tool_image, stored[0])
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
        raise


@app.post("/api/tools/menu-copy")
async def menu_copy_api(file: _UploadFile = _File(...), want: str = Form("")):
    await db.arun(_need_module, "content")
    raw = await _read_limited(file, 8 * 1024 * 1024, "图片太大(≤8MB)")
    op_key = await _start_billing_operation_safely(
        _start_billed_operation,
        "menu_copy",
        cancel_reason="识图请求中断自动退回",
    )
    mime = file.content_type if (file.content_type or "").startswith("image/") else "image/jpeg"
    try:
        result = await growth.menu_copy(
            TEN(),
            await asyncio.to_thread(_tool_image_base64, raw),
            mime,
            want,
        )
        if not await _run_db_safely(billing.complete_operation, op_key):
            raise RuntimeError("计费操作状态冲突")
        return result
    except asyncio.CancelledError:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "请求中断自动退回",
        )
        raise
    except Exception:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "识图失败自动退回",
        )
        raise HTTPException(500, "识图失败,点数已退回,换张清晰的图试试")


@app.post("/api/tools/product-shot")
async def product_shot_api(file: _UploadFile = _File(...), scene: str = Form("")):
    await db.arun(_need_module, "content")
    raw = await _read_limited(file, 8 * 1024 * 1024, "图片太大(≤8MB)")
    op_key = await _start_billing_operation_safely(
        _start_billed_operation,
        "product_shot",
        cancel_reason="商品图请求中断自动退回",
    )
    path = ""
    try:
        data = await growth.product_shot(TEN(), raw, scene)
        path, name = await _store_tool_image_safely(data, TEN())
        if not await _run_db_safely(billing.complete_operation, op_key):
            raise RuntimeError("计费操作状态冲突")
        return {"file": f"/files/tools/{TEN()}/{name}"}
    except asyncio.CancelledError:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "请求中断自动退回",
        )
        await asyncio.to_thread(_remove_tool_image, path)
        raise
    except Exception:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "商品图生成或保存失败自动退回",
        )
        await asyncio.to_thread(_remove_tool_image, path)
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
    await db.arun(_need_module, "content")
    raw = await _read_limited(file, 8 * 1024 * 1024, "图片太大(≤8MB)")
    mime = file.content_type if (file.content_type or "").startswith("image/") else "image/jpeg"
    b64 = await asyncio.to_thread(_tool_image_base64, raw)

    # 两条腿的计费操作先后开好:第二条点数不足时退掉第一条,给合并后的
    # 提示,而不是让老板看到"本次需 1 点"这种只说半截的话。
    shot_op = await _start_billing_operation_safely(
        _start_billed_operation,
        "product_shot",
        cancel_reason="拍照工厂商品图请求中断自动退回",
    )
    try:
        copy_op = await _start_billing_operation_safely(
            _start_billed_operation,
            "menu_copy",
            cancel_reason="拍照工厂文案请求中断自动退回",
        )
    except BaseException as exc:
        await _run_db_safely(
            billing.fail_operation,
            shot_op,
            "拍照工厂另一半未启动,整体退回",
        )
        if isinstance(exc, HTTPException) and exc.status_code == 402:
            raise HTTPException(
                402, "拍照工厂一次需 3 点(出图2+文案1),当前余额不足。请充值后再试"
            ) from exc
        raise

    async def _shot_leg(op_key):
        path = ""
        try:
            data = await growth.product_shot(TEN(), raw, scene)
            path, name = await _store_tool_image_safely(data, TEN())
            if not await _run_db_safely(
                billing.complete_operation,
                op_key,
            ):
                raise RuntimeError("计费操作状态冲突")
            return {"file": f"/files/tools/{TEN()}/{name}"}
        except asyncio.CancelledError:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "请求中断自动退回",
            )
            await asyncio.to_thread(_remove_tool_image, path)
            raise
        except Exception:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "商品图生成或保存失败自动退回",
            )
            await asyncio.to_thread(_remove_tool_image, path)
            return {"error": "美化没成功,这条腿的 2 点已退回;可换张更清晰的图重试"}

    async def _copy_leg(op_key):
        try:
            result = await growth.menu_copy(TEN(), b64, mime, want)
            if not await _run_db_safely(
                billing.complete_operation,
                op_key,
            ):
                raise RuntimeError("计费操作状态冲突")
            return {"menu": result}
        except asyncio.CancelledError:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "请求中断自动退回",
            )
            raise
        except Exception:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "识图失败自动退回",
            )
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
    await db.arun(_need_module, "content")
    script = (body.get("script") or "").strip()
    if len(script) < 30:
        raise HTTPException(400, "先贴一篇口播稿(至少30字)")
    op_key = await _start_billing_operation_safely(
        _start_billed_operation,
        "matrix_variants",
        cancel_reason="裂变请求中断自动退回",
    )
    try:
        result = await growth.script_variants(
            TEN(), script, body.get("n") or 3, body.get("styles") or ""
        )
        if not await _run_db_safely(billing.complete_operation, op_key):
            raise RuntimeError("计费操作状态冲突")
        return result
    except asyncio.CancelledError:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "请求中断自动退回",
        )
        raise
    except Exception:
        await _run_db_safely(
            billing.fail_operation,
            op_key,
            "裂变失败自动退回",
        )
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
    await db.arun(_need_module, "content")
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
    tenant_id = TEN()

    def validate_payload() -> None:
        if job_id is not None:
            _job_or_404(job_id)
            payload["job_id"] = job_id
        payload["images"] = [
            assetfiles.resolve_tenant_asset(
                value,
                tenant_id,
                expected_job_id=job_id,
                allowed_extensions=(".png", ".jpg", ".jpeg"),
            )
            for value in (body.get("images") or [])[:9]
        ]
        if body.get("video"):
            payload["video"] = assetfiles.resolve_tenant_asset(
                body["video"],
                tenant_id,
                expected_job_id=job_id,
                allowed_extensions=(".mp4",),
            )

    try:
        await db.arun(validate_payload)
    except assetfiles.AssetAccessError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    enqueue_task = asyncio.create_task(
        db.arun(
            matrixpub.enqueue,
            tenant_id,
            platform,
            body.get("account"),
            payload,
        )
    )
    try:
        pid = await asyncio.shield(enqueue_task)
    except asyncio.CancelledError:
        pid = None
        try:
            pid = await enqueue_task
        except BaseException:
            pass
        if pid is not None:
            asyncio.create_task(matrixpub.run_task(pid, engine.broadcast))
        raise
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


_HTML_ENTRY_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/login")
def login_page():
    with open(os.path.join(ROOT, "static", "login.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers=_HTML_ENTRY_NO_CACHE_HEADERS)


@app.get("/promo")
def promo_page():
    with open(os.path.join(ROOT, "static", "promo.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


_ENTRY_ASSET_VERSION: str | None = None


def _entry_asset_version() -> str:
    """app.js 内容哈希：发版换文件 → 换 URL → 浏览器必拉新版，不再靠手工改 ?v=。"""
    global _ENTRY_ASSET_VERSION
    if _ENTRY_ASSET_VERSION is None:
        try:
            with open(os.path.join(ROOT, "static", "app.js"), "rb") as fh:
                _ENTRY_ASSET_VERSION = hashlib.sha256(fh.read()).hexdigest()[:12]
        except OSError:
            _ENTRY_ASSET_VERSION = "unversioned"
    return _ENTRY_ASSET_VERSION


def _inject_entry_asset_version(html: str) -> str:
    return re.sub(
        r"(/static/app\.js\?v=)[0-9A-Za-z]+",
        lambda match: match.group(1) + _entry_asset_version(),
        html,
    )


@app.get("/")
def index():
    with open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8") as f:
        return HTMLResponse(
            _inject_entry_asset_version(f.read()),
            headers=_HTML_ENTRY_NO_CACHE_HEADERS,
        )
