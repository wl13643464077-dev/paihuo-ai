"""派给店员(真人)的任务：HTTP 路由(第 2 期)。

业务逻辑、权限和校验全部在 ``stafftask``(可测试)，这里只做：取当前用户、
读 multipart 照片、把业务异常翻译成 HTTP 状态码、提交后异步跑 AI 验照片。
不要 import main.py(会循环引用)。
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Request

from . import auth, db, photoproof, stafftask

log = logging.getLogger("api_staff")
router = APIRouter()

# 店员交差照片上传：前端压到长边 1600 的 JPEG(一般几百 KB)，一次最多 9 张。
# 整个请求上限与巡店一致(Caddy 40MB 以内)，单张在读取时再按 12MB 卡。
_SUBMIT_PATH_RE = re.compile(r"^/api/staff/tasks/[1-9][0-9]{0,11}/submit$")
SUBMIT_REQUEST_LIMIT = 38 * 1024 * 1024 + 1024 * 1024
_BACKGROUND: set[asyncio.Task] = set()


def upload_policy(method: str, path: str):
    """给 main.py 上传中间件用：返回 (动作名, 板块, 请求上限) 或 None。

    提交路径带任务编号，没法放进 main.py 按精确路径登记的白名单字典，
    所以单独按正则匹配。板块用 *work：老板，或开通了任意业务板块的成员。
    """
    if str(method or "").upper() == "POST" and _SUBMIT_PATH_RE.match(str(path or "")):
        return ("staff-task-photo", "*work", SUBMIT_REQUEST_LIMIT)
    return None


@asynccontextmanager
async def upload_slot(action: str):
    """店员交差的上传闸门：按人限并发/频率，不占用“每企业同时一个”的大文件通道。"""
    del action
    user = auth.current() or {}
    try:
        key = stafftask.UPLOAD_GATE.acquire(
            int(auth.tenant_id() or 0), int(user.get("id") or 0),
        )
    except stafftask.StaffTaskError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    try:
        yield
    finally:
        stafftask.UPLOAD_GATE.release(key)


def _me() -> tuple[int, dict]:
    user = auth.current()
    if not user or str(user.get("role") or "") not in ("root", "owner", "member"):
        raise HTTPException(403, "请用门店账号登录")
    return int(user["tenant_id"]), user


def _raise(exc: stafftask.StaffTaskError):
    raise HTTPException(exc.status, str(exc)) from None


def _body(body) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(400, "请求格式不对")
    return body


@router.get("/api/staff/todo")
def staff_todo():
    tid, user = _me()
    try:
        return stafftask.todo_for_user(tid, int(user["id"]))
    except stafftask.StaffTaskError as exc:
        _raise(exc)


@router.get("/api/staff/meta")
def staff_meta():
    """派活表单用：我能派活的门店、每家店的人、AI 验照片开关。"""
    tid, user = _me()
    try:
        return stafftask.dispatch_options(tid, user)
    except stafftask.StaffTaskError as exc:
        _raise(exc)


@router.put("/api/staff/settings")
def staff_settings(body: dict):
    tid, user = _me()
    body = _body(body)
    try:
        return stafftask.set_ai_check_enabled(
            tid, user, bool(body.get("ai_check_enabled")),
        )
    except stafftask.StaffTaskError as exc:
        _raise(exc)


@router.get("/api/staff/tasks")
def staff_tasks(
    status: str = "",
    branch: int = 0,
    assignee: int = 0,
    limit: int = 50,
    before_id: int = 0,
):
    tid, user = _me()
    try:
        return stafftask.list_tasks(
            tid, user, status=status or None, branch_id=branch or None,
            assignee_user_id=assignee or None, limit=limit,
            before_id=before_id or None,
        )
    except stafftask.StaffTaskError as exc:
        _raise(exc)


@router.post("/api/staff/tasks")
def staff_task_create(body: dict):
    tid, user = _me()
    body = _body(body)
    source = str(body.get("source") or "boss")
    if source not in ("boss", "ai_action"):
        # 清单/巡店来源只允许系统内部创建
        raise HTTPException(400, "任务来源无效")
    try:
        return stafftask.create_task(
            tid, user,
            title=body.get("title"),
            detail=body.get("detail") or "",
            branch_id=body.get("branch_id"),
            assignee_user_id=body.get("assignee_user_id"),
            due_at=body.get("due_at"),
            require_photo=body.get("require_photo", True) is not False,
            priority=str(body.get("priority") or "normal"),
            source=source,
            source_ref=body.get("source_ref") or "",
            request_key=body.get("request_key") or None,
        )
    except stafftask.StaffTaskError as exc:
        _raise(exc)


@router.post("/api/staff/tasks/parse")
async def staff_task_parse(body: dict):
    """一句话派活：只返回草稿，不落库；老板确认后再逐条 POST /api/staff/tasks。"""
    tid, user = _me()
    body = _body(body)
    try:
        return await stafftask.parse_one_liner(tid, user, body.get("text"))
    except stafftask.StaffTaskError as exc:
        _raise(exc)


@router.get("/api/staff/tasks/{task_id}")
def staff_task_get(task_id: int):
    tid, user = _me()
    try:
        return stafftask.get_task(tid, user, task_id)
    except stafftask.StaffTaskError as exc:
        _raise(exc)


@router.post("/api/staff/tasks/{task_id}/assign")
def staff_task_assign(task_id: int, body: dict):
    tid, user = _me()
    body = _body(body)
    kwargs = {}
    if "due_at" in body:
        kwargs["due_at"] = body.get("due_at")
    try:
        return stafftask.assign_task(
            tid, user, task_id, body.get("assignee_user_id"), **kwargs,
        )
    except stafftask.StaffTaskError as exc:
        _raise(exc)


async def _read_photo(upload) -> bytes:
    data = await upload.read(photoproof.MAX_UPLOAD_BYTES + 1)
    if len(data) > photoproof.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "照片太大了(超过 12MB)，请重新拍一张")
    return data


def _schedule_ai_check(tid: int, task_id: int) -> None:
    async def runner():
        try:
            await stafftask.run_ai_check(tid, task_id)
        except Exception as exc:   # run_ai_check 自己兜底；这里再挡一层
            log.warning("AI 验照片异常 tid=%s task=%s error_type=%s",
                        tid, task_id, type(exc).__name__)

    task = asyncio.get_running_loop().create_task(runner())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


@router.post("/api/staff/tasks/{task_id}/submit")
async def staff_task_submit(task_id: int, request: Request):
    """店员交差：multipart，photos(可多张，也认 photos[]) + note。"""
    tid, user = _me()
    try:
        form = await request.form()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "照片没传完整，请重试") from None
    uploads = [
        item for name in ("photos", "photos[]", "photo")
        for item in form.getlist(name)
        if hasattr(item, "read")
    ]
    if len(uploads) > stafftask.MAX_PHOTOS:
        raise HTTPException(400, f"一次最多交 {stafftask.MAX_PHOTOS} 张照片")
    photos = [await _read_photo(item) for item in uploads]
    note = form.get("note")
    note = note if isinstance(note, str) else ""
    try:
        result = await db.arun(
            stafftask.submit_task, tid, user, task_id, photos=photos, note=note,
        )
    except stafftask.StaffTaskError as exc:
        _raise(exc)
    if result.get("ai_check_pending") and not result.get("replayed"):
        _schedule_ai_check(tid, int(result["id"]))
    return result


@router.post("/api/staff/tasks/{task_id}/review")
def staff_task_review(task_id: int, body: dict):
    tid, user = _me()
    body = _body(body)
    action = str(body.get("action") or "").strip().lower()
    if action not in ("approve", "reject"):
        if isinstance(body.get("approve"), bool):
            action = "approve" if body["approve"] else "reject"
        else:
            raise HTTPException(400, "请选择通过还是打回")
    try:
        return stafftask.review_task(
            tid, user, task_id, approve=action == "approve",
            note=body.get("note") or "",
        )
    except stafftask.StaffTaskError as exc:
        _raise(exc)


@router.post("/api/staff/tasks/{task_id}/cancel")
def staff_task_cancel(task_id: int, body: dict | None = None):
    tid, user = _me()
    body = body if isinstance(body, dict) else {}
    try:
        return stafftask.cancel_task(tid, user, task_id, note=body.get("note") or "")
    except stafftask.StaffTaskError as exc:
        _raise(exc)
