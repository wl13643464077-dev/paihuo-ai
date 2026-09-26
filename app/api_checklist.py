"""开闭店清单 / 门店排行的 HTTP 路由（第 2 期）。

业务逻辑都在 app/checklist.py、app/storerank.py（可测试）；这里只做
取当前用户、解析参数、把业务异常翻成 HTTP 状态码。不 import main.py。
打勾、拍照、看排行都不扣点。
"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from . import auth, checklist, db, photoproof, storerank

router = APIRouter()

# 照片在前端已压到长边 ≤1600 的 JPEG，一般几百 KB；服务端上限与 photoproof 一致。
PHOTO_MAX_BYTES = photoproof.MAX_UPLOAD_BYTES


def _who() -> tuple[int, int]:
    user = auth.current()
    if not user or user.get("role") == "tour" or not int(user.get("id") or 0):
        raise HTTPException(403, "请先用自己的账号登录")
    return int(auth.tenant_id()), int(user["id"])


def _fail(exc: Exception):
    status = int(getattr(exc, "status", 400) or 400)
    raise HTTPException(status, str(exc)) from None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() not in {"0", "false", "no", "off"}


@router.get("/api/checklist/templates")
async def checklist_templates():
    tid, uid = _who()
    try:
        items = await db.arun(checklist.list_templates, tid, uid)
    except checklist.ChecklistError as exc:
        _fail(exc)
    return {"items": items}


@router.post("/api/checklist/templates")
async def checklist_template_create(body: dict):
    tid, uid = _who()
    try:
        return await db.arun(checklist.create_template, tid, uid, body)
    except checklist.ChecklistError as exc:
        _fail(exc)


@router.put("/api/checklist/templates/{template_id}")
async def checklist_template_update(template_id: int, body: dict):
    tid, uid = _who()
    try:
        return await db.arun(checklist.update_template, tid, uid, template_id, body)
    except checklist.ChecklistError as exc:
        _fail(exc)


@router.put("/api/checklist/duty")
async def checklist_duty(body: dict):
    """指定某门店清单的默认值班人（user_id 为空=清除，回到按店长指派）。"""
    tid, uid = _who()
    try:
        branch_id = int((body or {}).get("branch_id") or 0)
        raw_user = (body or {}).get("user_id")
        user_id = int(raw_user) if raw_user not in (None, "", 0) else None
    except (TypeError, ValueError):
        raise HTTPException(400, "门店或值班人格式不对") from None
    try:
        return await db.arun(checklist.set_duty_user, tid, uid, branch_id, user_id)
    except checklist.ChecklistError as exc:
        _fail(exc)


@router.get("/api/checklist/runs")
async def checklist_runs(date: str = "", mine: int = 0):
    """老板端：今天（或 date 那天）各店完成情况；mine=1 只要「我的清单」。"""
    tid, uid = _who()
    try:
        if mine:
            return {"items": await db.arun(checklist.runs_for_user, tid, uid, date or None)}
        return await db.arun(checklist.runs_overview, tid, uid, date or None)
    except checklist.ChecklistError as exc:
        _fail(exc)


@router.post("/api/checklist/runs/{run_id}/items/{item_key}")
async def checklist_item(
    run_id: int,
    item_key: str,
    photo: UploadFile | None = File(None),
    note: str = Form(""),
    done: str = Form("1"),
):
    """店员给某一项打勾（done=0 取消），可带一张现场照片和备注。"""
    tid, uid = _who()
    payload = None
    if photo is not None:
        data = await photo.read(PHOTO_MAX_BYTES + 1)
        if len(data) > PHOTO_MAX_BYTES:
            raise HTTPException(413, "照片太大了(超过 12MB)，请重新拍一张")
        if data:
            payload = {"data": data}
    try:
        return await db.arun(
            checklist.complete_item, tid, uid, run_id, item_key,
            photo=payload, note=note, done=_truthy(done),
        )
    except (checklist.ChecklistError, photoproof.PhotoError) as exc:
        _fail(exc)


@router.get("/api/stores/ranking")
async def stores_ranking(period: str = "week"):
    tid, uid = _who()
    try:
        return await db.arun(storerank.ranking, tid, uid, period)
    except (checklist.ChecklistError, storerank.RankError) as exc:
        _fail(exc)
