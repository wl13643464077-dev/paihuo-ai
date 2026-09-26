"""区域经理巡店的 HTTP 路由 + 巡店分析后台作业(第 3 期从 main.py 机械拆分，函数体未改)。

业务逻辑在 app/inspection*.py；main.py 启动段仍调用这里的
_resume_inspection_tasks / _backfill_inspection_scores(经 main 重新导出)。
上传白名单(_PERSISTENT_UPLOAD_ROUTES 等)仍登记在 main.py。不 import main.py。
"""


import asyncio
import json
import logging
import os
import re
import time

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .. import (
    assetfiles, auth, avatar, billing, db, departments, employees, inspection,
    inspectionimport, inspectionoverrides, inspectionstandards, llm, obs, providers,
    taskrunner,
)
from ..engine import engine
from ..skills import registry
from ..web_common import (
    ROOT, TEN, _INSPECTION_UPLOAD_MAX_BYTES, _assert_persistent_upload_capacity,
    _create_charged_expert_task, _drain_task_despite_cancellation, _free_ai_slot, _need_admin,
    _persistent_upload_slot, _public_station, _read_file_bytes, _read_limited, _run_db_safely,
    _run_db_then_start_worker_safely,
)


log = logging.getLogger("main")  # 与拆分前同名，日志检索口径不变
router = APIRouter()


_INSPECTION_ANALYSIS_MODEL_TIMEOUT_SECONDS = 300
_INSPECTION_CONTRACT_MARKER = "【最终权威JSON合同·运行时动态生成】"
# 前端复查上传等待 120s；后端必须更早收口并降级人工复核，
# 否则客户端先超时重试会在首个请求仍运行时重复落复查照片。
_INSPECTION_RECHECK_MODEL_TIMEOUT_SECONDS = 90


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
    """服务端权威 tenant + actor + industry + 门店绑定作用域的有界门店搜索。"""
    return inspection.search_branches(
        int(tid), int(uid), industry_key,
        q=q, region=region, limit=limit, before_id=before_id,
    )


def _inspection_checklist_db(
    tid: int,
    uid: int,
    industry_key: str,
    branch_id: int,
) -> dict:
    actor = inspection._actor(int(tid), int(uid), industry_key)
    branch = inspection._branch_scope(
        int(tid), industry_key, int(branch_id), actor=actor,
    )
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
    actor = inspection._actor(int(tid), int(uid), industry_key)
    inspection._branch_scope(
        int(tid), industry_key, int(branch_id), actor=actor,
    )
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
    """校验、纠正方向并重编码，彻底移除 EXIF 与上传文件名。

    第 2 期：按文件内容（魔数）判断类型，不看扩展名；实现见
    ``inspection.normalize_photo_upload``（可在无 fastapi 环境下测试）。
    """
    return inspection.normalize_photo_upload(data, filename)


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
                        # 第 2 期：看不清就如实给低分，服务端只让店员补拍这一张。
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
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
        "analyzable=false 表示照片不可分析，不得猜测或改成 true；"
        f"看不清、拍偏或没把握的照片如实给 analyzable=false 或 confidence<{inspection.MIN_PHOTO_REVIEW_CONFIDENCE}，"
        "系统只会让店员补拍这一张，不要为它编造问题或结论。",
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
    前提下同模重做一次。泄露、上游错误和取消一律原样失败；
    不可分析、低置信度的照片（第 2 期）不重做，只标记这一张需补拍。
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
        # 零问题时只要任一模型认为某张照片看不清，就让店员补拍这一张。
        **inspection.union_retake_flags(conservative, (primary, review)),
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
        f"- 门店得分（按问题严重度扣分）：{visit.get('score') if visit.get('score') is not None else '待人工确认'}",
        f"- AI 参考分（不参与排行）：{visit.get('ai_reference_score') if visit.get('ai_reference_score') is not None else '—'}",
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
        if visit.get("status") == "needs_retake":
            # 有照片看不清：这一轮分析算完成（点数不退），其他照片结论已保留；
            # 店员补拍后同一任务免费重新排队，只分析补拍的那几张。
            return _commit_inspection_retake_wait(
                connection, task_id, visit, usage,
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


def _commit_inspection_retake_wait(
    connection,
    task_id: int,
    visit: dict,
    usage: dict,
) -> bool:
    retake = visit.get("retake") or {}
    count = len(retake.get("pending") or [])
    photo_labels = "、".join(
        f"照片{photo.get('display_no')}"
        for photo in visit.get("photos") or []
        if photo.get("needs_retake")
    )
    now = time.time()
    changed = connection.execute(
        "UPDATE task SET status='done',output_md=?,summary_md=?,cost_usd=?,"
        "tokens=?,billing_status=CASE WHEN billing_status='charged' "
        "THEN 'succeeded' ELSE billing_status END,terminal_at=?,updated_at=? "
        "WHERE id=? AND status='running' "
        "AND billing_status IN ('charged','included') AND deleted_at IS NULL",
        (
            f"# 需要补拍 {count} 张照片\n\n{photo_labels} 看不清，"
            "其他照片的检查结果已保留。请店员在巡店记录里补拍这几张，"
            "补拍后会自动继续分析，不再扣点。",
            f"需要补拍 {count} 张照片"[:800],
            float(usage.get("cost_usd") or 0),
            int(usage.get("tokens") or 0),
            now,
            now,
            task_id,
        ),
    )
    if changed.rowcount != 1:
        raise inspection.InspectionConflict("巡店任务状态已发生变化")
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


async def _backfill_inspection_scores() -> int:
    """每批 200 条、批间让出 0.2 秒；出错只记日志，下次启动接着补。"""
    total = 0
    try:
        while True:
            done = await db.arun(inspection.backfill_scores, 200)
            if not done:
                break
            total += int(done)
            await asyncio.sleep(0.2)
    except Exception as exc:
        log.warning(
            "inspection score backfill stopped error_type=%s",
            type(exc).__name__,
        )
    if total:
        log.info("inspection score backfill updated=%d", total)
    return total


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
        # 补拍轮只把补拍的那几张交给模型，其他照片的结论已保留。
        allowed_photo_ids = inspection.analysis_photo_ids(visit)
        visit["photos"] = [
            photo for photo in visit.get("photos") or []
            if photo.get("phase") == "before"
            and int(photo["id"]) in allowed_photo_ids
        ]
        bundle = await db.arun(_inspection_prompt_bundle, tid, visit)
        images = await asyncio.to_thread(_load_inspection_images, tid, visit)
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


@router.get("/api/inspections/meta")
def inspection_meta(industry_key: str | None = None):
    try:
        selected, choices = _inspection_scope(industry_key)
        branch_page = _inspection_branch_search_db(
            TEN(), _inspection_actor_id(), selected, limit=20
        )
        is_manager = auth.is_admin()
        # 门店范围与按钮权限由服务层按职级/门店绑定判定。
        scope_info = inspection.branch_scope_info(
            TEN(), _inspection_actor_id(), selected
        )
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
                "can_create_branch": scope_info["can_manage_branches"],
                "can_manage_branches": scope_info["can_manage_branches"],
                "can_review": scope_info["can_review"],
                "can_assign_actions": scope_info["can_assign_actions"],
            },
            "branch_scope": {
                "all_branches": scope_info["all_branches"],
                "assigned_branches": scope_info["assigned_branches"],
                "notice": scope_info["notice"],
            },
            "employee": _public_station(registry.BY_IDX[inspection.EMPLOYEE_IDX]),
        }
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/api/inspections/branches/import-template")
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


@router.post("/api/inspections/branches/imports")
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


@router.get("/api/inspections/branches/imports/{import_id}")
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


@router.post("/api/inspections/branches/imports/{import_id}/commit")
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


@router.get("/api/inspections/branches/search")
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


@router.get("/api/inspections/standards/overrides")
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


@router.put("/api/inspections/standards/overrides")
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


@router.delete("/api/inspections/standards/overrides/{override_id}")
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


@router.get("/api/inspections/checklist")
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


@router.post("/api/inspections/branches")
def inspection_branch_create(body: dict, industry_key: str | None = None):
    try:
        selected, _choices = _inspection_scope(industry_key or body.get("industry_key"))
        return inspection.create_branch(
            TEN(), _inspection_actor_id(), selected, body
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@router.patch("/api/inspections/branches/{branch_id}")
def inspection_branch_update(branch_id: int, body: dict):
    """停用/恢复门店：老板或总监。"""
    try:
        selected, _choices = _inspection_scope(body.get("industry_key"))
        if "active" not in body:
            raise inspection.InspectionError("缺少门店启用状态")
        return inspection.set_branch_active(
            TEN(), _inspection_actor_id(), selected, branch_id,
            active=body.get("active"),
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


@router.get("/api/inspections")
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


@router.get("/api/inspections/{visit_id}")
def inspection_detail(visit_id: int, industry_key: str | None = None):
    try:
        selected, _choices = _inspection_scope(industry_key)
        return inspection.get_visit(
            TEN(), _inspection_actor_id(), selected, visit_id
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@router.post("/api/inspections")
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


@router.patch("/api/inspections/{visit_id}/issues/{issue_id}")
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


@router.patch("/api/inspections/{visit_id}/issues/{issue_id}/assignment")
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


# ---------------- 第 2 期：整改派到人 / 误报作废 / 单张补拍 ----------------
@router.put("/api/inspections/actions/{action_id}/assignee")
def inspection_action_assignee(action_id: int, body: dict):
    """把整改指派给具体账号（老板/总监/该门店店长），并按人通知。"""
    try:
        _inspection_scope(body.get("industry_key"))
        return inspection.assign_action(
            TEN(), _inspection_actor_id(), action_id,
            body.get("assignee_user_id"), body.get("due_at"),
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@router.post("/api/inspections/actions/{action_id}/dismiss")
def inspection_action_dismiss(action_id: int, body: dict):
    """老板/总监把 AI 误报的问题作废（可撤销）。"""
    try:
        _inspection_scope(body.get("industry_key"))
        return inspection.dismiss_action(
            TEN(), _inspection_actor_id(), action_id, body.get("reason"),
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@router.post("/api/inspections/actions/{action_id}/reopen")
def inspection_action_reopen(action_id: int, body: dict):
    """撤销误报作废，整改回到作废前的状态。"""
    try:
        _inspection_scope(body.get("industry_key"))
        return inspection.restore_dismissed_action(
            TEN(), _inspection_actor_id(), action_id, body.get("note") or "",
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@router.post("/api/inspections/retakes")
async def inspection_retake_upload(
    visit_id: int = Form(...),
    photo_id: int = Form(...),
    industry_key: str = Form(""),
    file: UploadFile = File(...),
):
    """补拍一张看不清的巡店照片；补齐后自动继续分析，不再扣点。"""
    try:
        selected, _choices = await db.arun(
            _inspection_scope, industry_key or None
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)
    tid, uid = TEN(), _inspection_actor_id()
    records: list[dict] = []
    result: dict | None = None
    async with _persistent_upload_slot("inspection-retake"):
        try:
            # 先确认这张确实在待补拍名单里，再读图落盘。
            await db.arun(
                inspection.retake_target, tid, uid, selected, visit_id, photo_id,
            )
        except inspection.InspectionError as exc:
            _raise_inspection_error(exc)
        prepared = await _prepare_inspection_uploads([file])
        try:
            records = await _run_inspection_file_safely(
                _store_inspection_images, tid, visit_id, prepared
            )
            result = await _run_db_safely(
                inspection.replace_retake_photo,
                tid, uid, selected, visit_id, photo_id, records[0],
            )
        except inspection.InspectionError as exc:
            _raise_inspection_error(exc)
        finally:
            if records:
                await _run_inspection_file_safely(
                    _cleanup_unreferenced_inspection_images, records
                )
    if result and result.get("old_storage_key"):
        # 旧的模糊照片已不被引用，删掉，免得留孤儿文件。
        await _run_inspection_file_safely(
            _cleanup_unreferenced_inspection_images,
            [{"storage_key": result["old_storage_key"]}],
        )
    if result and result.get("ready") and result.get("task_id"):
        _start_inspection_task({"task_id": int(result["task_id"])})
    return {
        "ok": True,
        "visit_id": int(visit_id),
        "photo_id": int(photo_id),
        "remaining": int((result or {}).get("remaining") or 0),
        "analyzing": bool((result or {}).get("ready")),
    }


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


@router.post("/api/inspections/rechecks")
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


@router.post("/api/inspections/rechecks/{recheck_id}/review")
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
