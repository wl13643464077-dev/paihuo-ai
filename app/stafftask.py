"""派给店员(真人)的任务：派活 → 指派 → 拍照交差 → 审核 → 轨迹。

第 2 期「真人派活闭环」的服务层(纯函数、可测试、不 import fastapi)：

- 创建任务(可带 request_key 防重复派单)、指派/改派、店员拍照提交、
  老板/店长审核(通过/打回，打回回到「待做」并记原因)、取消；
  每一步都写 staff_task_event 审计轨迹。
- 权限：老板/总监可以给任何门店派活；店长只能给自己负责的门店派活，
  且只能派给这家店的人；店员不能派活。被指派的人必须同企业、账号启用、
  并且负责这家门店(老板/总监除外)。
- 列表：老板/总监看全部；店长看自己门店的；店员只看派给自己的
  (以及自己门店里还没指派人的)。
- ``todo_for_user``：店员「我的待办」，合并派活任务、今天的开闭店清单
  (checklist.runs_for_user，B 提供)和派给我的巡店整改
  (inspection.actions_for_assignee，C 提供)，按「逾期 → 今天到期 → 其他」排序。
- ``parse_one_liner``：老板说一句话，普通文本模型拆成派活草稿；门店和人
  一律在本地按名字匹配，匹配不上就留空让老板选，绝不编造。
- ``run_ai_check``：店员提交后异步让视觉模型看照片给「建议」，只写
  ai_check_json，最终由人审核；AI 失败不影响提交，扣的点自动退回。

调用大模型的两步按次扣点(价目表 staff_parse / staff_ai_check)，走 billing
的 start_operation / complete_operation / fail_operation，失败自动退点。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import billing, db, timeutil

# ---------------- 常量 ----------------
MAX_TITLE = 60
MAX_DETAIL = 1000
MAX_NOTE = 500
MAX_PHOTOS = 9
MAX_ONE_LINER = 500
MAX_DRAFTS = 10
MAX_LIST = 100
AI_CHECK_PHOTOS = 4
PARSE_ACTION = "staff_parse"
AI_CHECK_ACTION = "staff_ai_check"
PARSE_TIMEOUT_S = 60
AI_CHECK_TIMEOUT_S = 90

STATUSES = ("todo", "submitted", "approved", "rejected", "cancelled")
PRIORITIES = ("low", "normal", "high")
SOURCES = ("boss", "checklist", "inspection", "ai_action")
STATUS_LABELS = {
    "todo": "待做",
    "submitted": "已交，等审核",
    "approved": "已通过",
    "rejected": "被打回",
    "cancelled": "已取消",
}
TITLE_LABELS = {"owner": "老板", "root": "老板", "director": "总监",
                "manager": "店长", "staff": "店员"}
AI_VERDICTS = ("pass", "doubt", "fail")

_REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DUE_TEXT_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?$"
)
_UNSET = object()


class StaffTaskError(ValueError):
    """业务校验失败；文案直接给老板/店员看。"""

    status = 400

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        if status is not None:
            self.status = int(status)


class StaffTaskForbidden(StaffTaskError):
    status = 403


class StaffTaskNotFound(StaffTaskError):
    status = 404


class StaffTaskConflict(StaffTaskError):
    status = 409


# ---------------- 小工具 ----------------
def _text(value: Any, *, field: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise StaffTaskError(f"{field}格式不对")
    text = _CONTROL_RE.sub("", str(value)).strip()
    if required and not text:
        raise StaffTaskError(f"请填写{field}")
    if len(text) > limit:
        raise StaffTaskError(f"{field}太长了，最多 {limit} 个字")
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise StaffTaskError(f"{field}无效")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise StaffTaskError(f"{field}无效") from None
    if number < 1:
        raise StaffTaskError(f"{field}无效")
    return number


def _optional_id(value: Any, field: str) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    return _positive_int(value, field)


def parse_due(value: Any) -> float | None:
    """截止时间：Unix 秒，或北京时间文本「2026-09-26 12:00」/「2026-09-26」。

    只写日期时按当天 18:00(北京时间)算。空值返回 None。
    """
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise StaffTaskError("截止时间格式不对")
    if isinstance(value, (int, float)):
        ts = float(value)
    else:
        text = str(value).strip()
        try:
            ts = float(text)
        except ValueError:
            match = _DUE_TEXT_RE.match(text)
            if not match:
                raise StaffTaskError("截止时间格式不对，例如 2026-09-26 12:00") from None
            year, month, day = (int(match.group(i)) for i in (1, 2, 3))
            hour = int(match.group(4)) if match.group(4) else 18
            minute = int(match.group(5)) if match.group(5) else 0
            try:
                moment = datetime(year, month, day, hour, minute,
                                  tzinfo=timeutil.CN_TZ)
            except ValueError:
                raise StaffTaskError("截止时间不是有效日期") from None
            ts = moment.timestamp()
    if ts != ts or ts < 946684800 or ts > 4102444800:   # NaN / 2000 年前 / 2100 年后
        raise StaffTaskError("截止时间不是有效日期")
    return ts


def _json_or_none(raw: Any) -> dict | None:
    if not raw:
        return None
    value = db.jloads(raw, None)
    return value if isinstance(value, dict) else None


# ---------------- 账号与门店 ----------------
def _member_title(user: Mapping[str, Any]) -> str:
    title = str(user.get("job_title") or "staff")
    return title if title in ("director", "manager", "staff") else "staff"


def sees_all_branches(user: Mapping[str, Any]) -> bool:
    """老板/平台管理员/总监看全部门店(与巡店口径一致)。"""
    role = str(user.get("role") or "")
    if role in ("root", "owner"):
        return True
    return role == "member" and _member_title(user) == "director"


def role_label(user: Mapping[str, Any]) -> str:
    role = str(user.get("role") or "")
    if role in ("root", "owner"):
        return TITLE_LABELS[role]
    return TITLE_LABELS.get(_member_title(user), "店员")


def _load_user(tid: int, uid: Any) -> dict | None:
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    row = db.one(
        "SELECT u.id,u.tenant_id,u.username,u.role,u.job_title,u.modules_json,"
        "u.enabled FROM users u JOIN tenants t ON t.id=u.tenant_id "
        "WHERE u.id=? AND t.enabled=1",
        (uid,),
    )
    if not row or not int(row.get("enabled") or 0):
        return None
    if int(row["tenant_id"]) != int(tid):
        return None
    if str(row.get("role") or "") not in ("root", "owner", "member"):
        return None
    modules = db.jloads(row.pop("modules_json", None), [])
    row["modules"] = modules if isinstance(modules, list) else []
    return row


def _actor(tid: int, actor_user: Mapping[str, Any] | None) -> dict:
    """调用方传进来的 user 只取 id，权限一律按库里的最新状态判定。"""
    if not actor_user:
        raise StaffTaskForbidden("请先登录")
    user = _load_user(tid, actor_user.get("id"))
    if not user:
        raise StaffTaskForbidden("账号不存在或已停用")
    return user


def _bound_branch_ids(tid: int, uid: int) -> set[int]:
    return {
        int(row["branch_id"])
        for row in db.q(
            "SELECT branch_id FROM user_branch WHERE tenant_id=? AND user_id=?",
            (int(tid), int(uid)),
        )
    }


def _branch(tid: int, branch_id: int, *, active_only: bool = True) -> dict:
    row = db.one(
        "SELECT id,tenant_id,industry_key,name,active FROM store_branch "
        "WHERE id=? AND tenant_id=?",
        (int(branch_id), int(tid)),
    )
    if not row or (active_only and not int(row.get("active") or 0)):
        raise StaffTaskNotFound("门店不存在或已停用")
    return row


def _industry_ok(user: Mapping[str, Any], branch: Mapping[str, Any]) -> bool:
    """与照片权限一致：先校验当前租户行业，再校验成员的行业模块。"""
    role = str(user.get("role") or "")
    industry = str(branch.get("industry_key") or "")
    tid = int(user.get("tenant_id") or 0)
    if not industry or (tid != 1 and role != "root" and not db.one(
        "SELECT 1 AS ok FROM tenant_industry WHERE tenant_id=? AND industry_key=?",
        (tid, industry),
    )):
        return False
    if role in ("root", "owner"):
        return True
    return industry in (user.get("modules") or [])


def _branch_visible(user: Mapping[str, Any], branch: Mapping[str, Any]) -> bool:
    if int(user.get("tenant_id") or 0) != int(branch.get("tenant_id") or 0):
        return False
    if sees_all_branches(user):
        return _industry_ok(user, branch)
    return int(branch["id"]) in _bound_branch_ids(
        int(user["tenant_id"]), int(user["id"])
    ) and _industry_ok(user, branch)


def can_dispatch(user: Mapping[str, Any]) -> bool:
    """能不能派活：老板/总监/店长可以，店员不行。"""
    if sees_all_branches(user):
        return True
    return str(user.get("role") or "") == "member" \
        and _member_title(user) == "manager"


def can_dispatch_branch(user: Mapping[str, Any], branch: Mapping[str, Any]) -> bool:
    if not can_dispatch(user):
        return False
    return _branch_visible(user, branch)


def _assert_dispatch_branch(user: Mapping[str, Any], branch: Mapping[str, Any]) -> None:
    if not can_dispatch(user):
        raise StaffTaskForbidden("店员账号不能派活，请让店长或老板来派")
    if not _branch_visible(user, branch):
        # 与“不存在”同样口径，不泄露别人负责的门店
        raise StaffTaskNotFound("门店不存在或不归你管")


def _assignee_ok(
    tid: int,
    branch: Mapping[str, Any],
    assignee: Mapping[str, Any] | None,
    actor: Mapping[str, Any] | None,
) -> bool:
    if not assignee or int(assignee["tenant_id"]) != int(tid):
        return False
    if not _industry_ok(assignee, branch):
        return False
    if sees_all_branches(assignee):
        # 店长只能派给本店的人；老板/总监可以把活派给老板/总监。
        return actor is None or sees_all_branches(actor)
    return int(branch["id"]) in _bound_branch_ids(tid, int(assignee["id"]))


def _check_assignee(
    tid: int,
    branch: Mapping[str, Any],
    assignee_user_id: Any,
    actor: Mapping[str, Any] | None,
) -> dict:
    assignee_id = _positive_int(assignee_user_id, "负责人")
    assignee = _load_user(tid, assignee_id)
    if not _assignee_ok(tid, branch, assignee, actor):
        raise StaffTaskError(
            f"这个人不在「{branch.get('name') or '该门店'}」，只能派给这家店的人"
        )
    return assignee


def branch_members(tid: int, branch: Mapping[str, Any]) -> list[dict]:
    """这家店能接活的人：绑定了这家店的成员(店长在前)。"""
    rows = db.q(
        "SELECT u.id,u.username,u.role,u.job_title,u.modules_json,u.enabled "
        "FROM user_branch ub JOIN users u ON u.id=ub.user_id "
        "WHERE ub.tenant_id=? AND ub.branch_id=? AND u.tenant_id=? "
        "AND u.enabled=1 ORDER BY u.id",
        (int(tid), int(branch["id"]), int(tid)),
    )
    members = []
    for row in rows:
        modules = db.jloads(row.get("modules_json"), [])
        if str(row.get("role") or "") == "member" and (
            not isinstance(modules, list)
            or str(branch.get("industry_key") or "") not in modules
        ):
            continue
        members.append({
            "id": int(row["id"]),
            "name": str(row.get("username") or ""),
            "job_title": _member_title(row) if row.get("role") == "member"
            else str(row.get("role") or ""),
            "title_label": role_label(row),
        })
    members.sort(key=lambda m: (0 if m["job_title"] == "manager" else 1, m["id"]))
    return members


def _dispatch_branches(tid: int, actor: Mapping[str, Any]) -> list[dict]:
    if sees_all_branches(actor):
        rows = db.q(
            "SELECT id,tenant_id,industry_key,name,active FROM store_branch "
            "WHERE tenant_id=? AND active=1 ORDER BY name,id LIMIT 2000",
            (int(tid),),
        )
    else:
        rows = db.q(
            "SELECT b.id,b.tenant_id,b.industry_key,b.name,b.active "
            "FROM store_branch b JOIN user_branch ub ON ub.branch_id=b.id "
            "AND ub.tenant_id=b.tenant_id WHERE b.tenant_id=? AND ub.user_id=? "
            "AND b.active=1 ORDER BY b.name,b.id",
            (int(tid), int(actor["id"])),
        )
    return [row for row in rows if _industry_ok(actor, row)]


def dispatch_options(tid: int, actor_user: Mapping[str, Any]) -> dict:
    """派活表单用：我能派活的门店 + 每家店能接活的人 + 开关状态。"""
    actor = _actor(tid, actor_user)
    branches = []
    if can_dispatch(actor):
        for branch in _dispatch_branches(tid, actor):
            members = branch_members(tid, branch)
            branches.append({
                "id": int(branch["id"]),
                "name": str(branch.get("name") or ""),
                "members": members,
            })
    return {
        "can_dispatch": can_dispatch(actor),
        "can_review": can_dispatch(actor),
        "sees_all": sees_all_branches(actor),
        "role_label": role_label(actor),
        "branches": branches,
        "ai_check_enabled": ai_check_enabled(tid),
        "prices": {
            PARSE_ACTION: _price_points(PARSE_ACTION),
            AI_CHECK_ACTION: _price_points(AI_CHECK_ACTION),
        },
    }


def prefers_staff_home(user: Mapping[str, Any] | None) -> bool:
    """登录后是否默认进店员手机版 /staff。

    只有「店员/店长」并且老板已经给他分了门店的成员才跳：普通的内容运营
    副账号(默认职级也是 staff)没有门店，继续进原来的老板端，不打扰。
    """
    if not user or str(user.get("role") or "") != "member":
        return False
    if _member_title(user) not in ("staff", "manager"):
        return False
    try:
        return bool(db.one(
            "SELECT 1 AS ok FROM user_branch ub JOIN store_branch b "
            "ON b.id=ub.branch_id AND b.tenant_id=ub.tenant_id "
            "WHERE ub.tenant_id=? AND ub.user_id=? AND b.active=1 LIMIT 1",
            (int(user.get("tenant_id") or 0), int(user.get("id") or 0)),
        ))
    except (TypeError, ValueError):
        return False


# ---------------- 设置与价目 ----------------
def _ai_check_key(tid: int) -> str:
    return f"staff_ai_check:{int(tid)}"


def ai_check_enabled(tid: int) -> bool:
    """AI 验照片开关，默认开。"""
    return str(db.get_setting(_ai_check_key(tid)) or "1") != "0"


def set_ai_check_enabled(tid: int, actor_user: Mapping[str, Any], enabled: bool) -> dict:
    actor = _actor(tid, actor_user)
    if str(actor.get("role") or "") not in ("root", "owner"):
        raise StaffTaskForbidden("只有老板可以改这个开关")
    db.set_setting(_ai_check_key(tid), "1" if enabled else "0")
    return {"ai_check_enabled": bool(enabled)}


def _price_row(action: str) -> dict | None:
    """后台保存过的价目表优先；表里没有这个动作(旧价目表)时按免费处理，
    避免 billing 缺省把没登记的动作按 1 点扣。"""
    row = (billing.prices() or {}).get(action)
    return row if isinstance(row, dict) else None


def _price_points(action: str) -> float:
    row = _price_row(action)
    try:
        return max(0.0, float((row or {}).get("points") or 0))
    except (TypeError, ValueError):
        return 0.0


def _start_charge(action: str, tid: int, note: str, op_key: str | None = None) -> str | None:
    """扣点；返回操作编号(免费时返回 None)。点数不足抛 402。"""
    if _price_points(action) <= 0:
        return None
    try:
        return billing.start_operation(action, int(tid), note=note, op_key=op_key)
    except billing.InsufficientPoints:
        points = _price_points(action)
        raise StaffTaskError(
            f"点数不足：这一步要 {points:g} 点，请先充值", status=402,
        ) from None


def _finish_charge(op_key: str | None, ok: bool, reason: str = "") -> None:
    if not op_key:
        return
    try:
        if ok:
            billing.complete_operation(op_key)
        else:
            billing.fail_operation(op_key, reason or "没做成，自动退回")
    except Exception:   # 结算失败留给启动恢复兜底退点
        pass


# ---------------- 读 ----------------
def _names(tid: int, table: str, ids: Iterable[Any], column: str) -> dict[int, str]:
    wanted = sorted({int(i) for i in ids if i})
    result: dict[int, str] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        for row in db.q(
            f"SELECT id,{column} AS label FROM {table} "
            f"WHERE tenant_id=? AND id IN ({marks})",
            (int(tid), *chunk),
        ):
            result[int(row["id"])] = str(row.get("label") or "")
    return result


def _urgency(due_at: Any, now: float) -> str:
    if due_at is None:
        return "later"
    due = float(due_at)
    if due < now:
        return "overdue"
    if due < timeutil.cn_day_start_ts(now) + 86400:
        return "today"
    return "later"


def _photo_public(row: Mapping[str, Any]) -> dict:
    return {
        "id": int(row["id"]),
        "url": "/files/" + str(row["storage_key"]),
        "width": row.get("width"),
        "height": row.get("height"),
        "received_at": row.get("received_at"),
        "watermark_text": row.get("watermark_text") or "",
        "created_at": row.get("created_at"),
    }


def _public(
    tid: int,
    rows: Sequence[Mapping[str, Any]],
    *,
    now: float | None = None,
    with_photos: bool = False,
    with_events: bool = False,
) -> list[dict]:
    now = time.time() if now is None else float(now)
    branch_names = _names(tid, "store_branch", (r.get("branch_id") for r in rows), "name")
    user_ids = []
    for r in rows:
        user_ids += [r.get("assignee_user_id"), r.get("created_by"), r.get("reviewed_by")]
    user_names = _names(tid, "users", user_ids, "username")
    photos: dict[int, list[dict]] = {}
    if with_photos and rows:
        ids = [int(r["id"]) for r in rows]
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for photo in db.q(
                "SELECT id,task_id,storage_key,width,height,received_at,"
                "watermark_text,created_at FROM staff_task_photo "
                f"WHERE tenant_id=? AND task_id IN ({marks}) ORDER BY id",
                (int(tid), *chunk),
            ):
                photos.setdefault(int(photo["task_id"]), []).append(photo)
    items = []
    for r in rows:
        status = str(r.get("status") or "todo")
        due_at = r.get("due_at")
        item = {
            "kind": "task",
            "id": int(r["id"]),
            "title": r.get("title") or "",
            "detail": r.get("detail") or "",
            "branch_id": r.get("branch_id"),
            "branch_name": branch_names.get(int(r.get("branch_id") or 0), ""),
            "assignee_user_id": r.get("assignee_user_id"),
            "assignee_name": user_names.get(int(r.get("assignee_user_id") or 0), ""),
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "priority": r.get("priority") or "normal",
            "source": r.get("source") or "boss",
            "source_ref": r.get("source_ref") or "",
            "require_photo": bool(r.get("require_photo")),
            "due_at": due_at,
            "due_text": timeutil.format_cn(due_at, "%m-%d %H:%M") if due_at else "",
            "urgency": _urgency(due_at, now) if status == "todo" else status,
            "overdue": status == "todo" and due_at is not None and float(due_at) < now,
            "created_by": r.get("created_by"),
            "created_by_name": user_names.get(int(r.get("created_by") or 0), ""),
            "created_at": r.get("created_at"),
            "submitted_at": r.get("submitted_at"),
            "submit_note": r.get("submit_note") or "",
            "reviewed_at": r.get("reviewed_at"),
            "reviewed_by": r.get("reviewed_by"),
            "reviewed_by_name": user_names.get(int(r.get("reviewed_by") or 0), ""),
            "review_note": r.get("review_note") or "",
            "ai_check": _json_or_none(r.get("ai_check_json")),
            "remind_count": int(r.get("remind_count") or 0),
            "escalated_level": int(r.get("escalated_level") or 0),
        }
        if with_photos:
            submitted_at = float(r.get("submitted_at") or 0)
            all_photos = [_photo_public(p) for p in photos.get(int(r["id"]), [])]
            # 本轮提交的照片 = 提交时刻一并写入的那批；打回后(回到待做)全部算旧照片单列。
            item["photos"] = [
                p for p in all_photos
                if status != "todo" and submitted_at
                and float(p.get("created_at") or 0) >= submitted_at
            ]
            item["earlier_photos"] = [p for p in all_photos if p not in item["photos"]]
        if with_events:
            item["events"] = list_events(tid, int(r["id"]))
        items.append(item)
    return items


def _task_row(tid: int, task_id: Any) -> dict:
    task_id = _positive_int(task_id, "任务")
    row = db.one(
        "SELECT * FROM staff_task WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (task_id, int(tid)),
    )
    if not row:
        raise StaffTaskNotFound("这条任务不存在或已删除")
    return row


def _task_scope_visible(actor: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """历史指派/创建记录不授予权限，门店任务始终受当前权限约束。"""
    if int(actor.get("tenant_id") or 0) != int(row.get("tenant_id") or 0):
        return False
    branch_id = int(row.get("branch_id") or 0)
    if not branch_id:
        return True
    try:
        branch = _branch(int(row["tenant_id"]), branch_id, active_only=False)
    except StaffTaskNotFound:
        return False
    return _branch_visible(actor, branch)


def _can_view(actor: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    if not _task_scope_visible(actor, row):
        return False
    uid = int(actor["id"])
    if int(row.get("assignee_user_id") or 0) == uid:
        return True
    if sees_all_branches(actor):
        return True
    if int(row.get("created_by") or 0) == uid:
        return True
    if not row.get("branch_id"):
        return False
    if _member_title(actor) == "manager":
        return True
    # 店员：同门店里还没指派人的活也能看到、能认领
    return row.get("assignee_user_id") is None


def _visible_task(tid: int, actor: Mapping[str, Any], task_id: Any) -> dict:
    row = _task_row(tid, task_id)
    if not _can_view(actor, row):
        raise StaffTaskNotFound("这条任务不存在或已删除")
    return row


def _can_manage(actor: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """能不能改派/审核/取消：老板总监，或负责这家店的店长。"""
    if not can_dispatch(actor):
        return False
    branch_id = row.get("branch_id")
    if branch_id is None:
        return sees_all_branches(actor)
    try:
        branch = _branch(int(row["tenant_id"]), int(branch_id), active_only=False)
    except StaffTaskNotFound:
        return False
    return _branch_visible(actor, branch)


def list_events(tid: int, task_id: int) -> list[dict]:
    rows = db.q(
        "SELECT e.id,e.kind,e.note,e.actor_user_id,e.created_at,u.username "
        "FROM staff_task_event e LEFT JOIN users u ON u.id=e.actor_user_id "
        "AND u.tenant_id=e.tenant_id WHERE e.tenant_id=? AND e.task_id=? "
        "ORDER BY e.id",
        (int(tid), int(task_id)),
    )
    return [{
        "id": int(r["id"]),
        "kind": r.get("kind") or "",
        "note": r.get("note") or "",
        "actor_user_id": r.get("actor_user_id"),
        "actor_name": r.get("username") or ("系统" if r.get("actor_user_id") is None else ""),
        "created_at": r.get("created_at"),
    } for r in rows]


def get_task(tid: int, actor_user: Mapping[str, Any], task_id: Any) -> dict:
    actor = _actor(tid, actor_user)
    row = _visible_task(tid, actor, task_id)
    item = _public(tid, [row], with_photos=True, with_events=True)[0]
    item["can_manage"] = _can_manage(actor, row)
    item["can_submit"] = _can_submit(actor, row)
    return item


def list_tasks(
    tid: int,
    actor_user: Mapping[str, Any],
    *,
    status: str | None = None,
    branch_id: Any = None,
    assignee_user_id: Any = None,
    limit: int = 50,
    before_id: Any = None,
) -> dict:
    """老板看全部(可按门店/状态/负责人筛)；店长只看自己门店；店员只看自己的。"""
    actor = _actor(tid, actor_user)
    uid = int(actor["id"])
    where = [
        "t.tenant_id=?", "t.deleted_at IS NULL",
        "(t.branch_id IS NULL OR EXISTS (SELECT 1 FROM store_branch sb "
        "WHERE sb.id=t.branch_id AND sb.tenant_id=t.tenant_id))",
    ]
    params: list[Any] = [int(tid)]
    if int(tid) != 1 and str(actor.get("role") or "") != "root":
        where.append(
            "(t.branch_id IS NULL OR t.branch_id IN (SELECT sb.id FROM store_branch sb "
            "JOIN tenant_industry ti ON ti.tenant_id=sb.tenant_id "
            "AND ti.industry_key=sb.industry_key WHERE sb.tenant_id=t.tenant_id))"
        )
    if str(actor.get("role") or "") == "member":
        # 成员(含总监)只看自己开通了行业板块的门店的活；不挂门店的活不受限
        where.append(
            "(t.branch_id IS NULL OR t.branch_id IN (SELECT sb.id FROM store_branch sb "
            "WHERE sb.tenant_id=t.tenant_id AND sb.industry_key IN "
            "(SELECT value FROM json_each(?))))"
        )
        params.append(json.dumps([str(m) for m in (actor.get("modules") or [])]))
    if not sees_all_branches(actor):
        bound = "t.branch_id IN (SELECT ub.branch_id FROM user_branch ub " \
                "WHERE ub.tenant_id=? AND ub.user_id=?)"
        where.append(f"(t.branch_id IS NULL OR {bound})")
        params += [int(tid), uid]
        if can_dispatch(actor):   # 店长
            where.append(f"(t.assignee_user_id=? OR t.created_by=? OR {bound})")
            params += [uid, uid, int(tid), uid]
        else:                     # 店员
            where.append(f"(t.assignee_user_id=? OR (t.assignee_user_id IS NULL AND {bound}))")
            params += [uid, int(tid), uid]
    if status:
        wanted = [s for s in str(status).split(",") if s]
        if not wanted or any(s not in STATUSES for s in wanted):
            raise StaffTaskError("状态筛选无效")
        where.append(f"t.status IN ({','.join('?' for _ in wanted)})")
        params += wanted
    branch = _optional_id(branch_id, "门店")
    if branch:
        where.append("t.branch_id=?")
        params.append(branch)
    assignee = _optional_id(assignee_user_id, "负责人")
    if assignee:
        where.append("t.assignee_user_id=?")
        params.append(assignee)
    before = _optional_id(before_id, "翻页位置")
    if before:
        where.append("t.id<?")
        params.append(before)
    try:
        page = max(1, min(int(limit or 50), MAX_LIST))
    except (TypeError, ValueError):
        page = 50
    rows = db.q(
        f"SELECT t.* FROM staff_task t WHERE {' AND '.join(where)} "
        "ORDER BY t.id DESC LIMIT ?",
        (*params, page + 1),
    )
    has_more = len(rows) > page
    rows = rows[:page]
    items = _public(tid, rows, with_photos=True)
    return {
        "items": items,
        "next_before_id": items[-1]["id"] if has_more and items else None,
        "limit": page,
    }


# ---------------- 写 ----------------
def _event(connection, tid: int, task_id: int, actor_id: int | None,
           kind: str, note: str = "", now: float | None = None) -> None:
    connection.execute(
        "INSERT INTO staff_task_event(tenant_id,task_id,actor_user_id,kind,note,"
        "created_at) VALUES(?,?,?,?,?,?)",
        (int(tid), int(task_id), actor_id, str(kind)[:30], str(note or "")[:500],
         time.time() if now is None else float(now)),
    )


def _notify(tid: int, kind: str, payload: dict, user_ids: Iterable[Any]) -> None:
    """通知失败绝不影响派活本身。"""
    try:
        from . import notify
        notify.push_to_users(int(tid), kind, payload, list(user_ids))
    except Exception:
        pass


def _find_by_request(tid: int, request_key: str) -> dict | None:
    return db.one(
        "SELECT * FROM staff_task WHERE tenant_id=? AND request_key=?",
        (int(tid), request_key),
    )


def create_task(
    tid: int,
    actor_user: Mapping[str, Any] | None,
    *,
    title: Any,
    detail: Any = "",
    branch_id: Any = None,
    assignee_user_id: Any = None,
    due_at: Any = None,
    require_photo: Any = True,
    priority: str = "normal",
    source: str = "boss",
    source_ref: Any = "",
    request_key: str | None = None,
    now: float | None = None,
) -> dict:
    """派一件活。actor_user 为 None 表示系统创建(清单/巡店整改自动派)。

    同一个 request_key 重复提交只会建一条，第二次返回原任务(replayed=True)。
    """
    tid = int(tid)
    actor = _actor(tid, actor_user) if actor_user is not None else None
    title_text = _text(title, field="要做的事", limit=MAX_TITLE, required=True)
    detail_text = _text(detail, field="说明", limit=MAX_DETAIL)
    ref_text = _text(source_ref, field="来源", limit=200)
    if priority not in PRIORITIES:
        raise StaffTaskError("紧急程度无效")
    if source not in SOURCES:
        raise StaffTaskError("任务来源无效")
    key = None
    if request_key not in (None, ""):
        key = str(request_key)
        if not _REQUEST_KEY_RE.match(key):
            raise StaffTaskError("请求编号无效")
        existing = _find_by_request(tid, key)
        if existing:
            return _replay(tid, actor, existing)
    due = parse_due(due_at)
    assignee_id = _optional_id(assignee_user_id, "负责人")
    branch_ref = _optional_id(branch_id, "门店")
    if branch_ref is None and assignee_id is not None:
        # 没选门店但选了人：这人只负责一家店时就用那家
        bound = sorted(_bound_branch_ids(tid, assignee_id))
        if len(bound) == 1:
            branch_ref = bound[0]
    if branch_ref is None:
        raise StaffTaskError("请选择是哪家门店的活")
    branch = _branch(tid, branch_ref)
    if actor is not None:
        _assert_dispatch_branch(actor, branch)
    assignee = (
        _check_assignee(tid, branch, assignee_id, actor)
        if assignee_id is not None else None
    )
    ts = time.time() if now is None else float(now)
    actor_id = int(actor["id"]) if actor else None
    try:
        with db.atomic() as connection:
            cursor = connection.execute(
                "INSERT INTO staff_task(tenant_id,branch_id,assignee_user_id,title,"
                "detail,source,source_ref,require_photo,due_at,status,priority,"
                "created_by,created_at,updated_at,request_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,'todo',?,?,?,?,?)",
                (
                    tid, int(branch["id"]),
                    int(assignee["id"]) if assignee else None,
                    title_text, detail_text, source, ref_text,
                    1 if require_photo not in (False, 0, "0", "false", "") else 0,
                    due, priority, actor_id, ts, ts, key,
                ),
            )
            task_id = int(cursor.lastrowid)
            _event(connection, tid, task_id, actor_id, "created",
                   f"派给「{branch.get('name') or ''}」", ts)
            if assignee:
                _event(connection, tid, task_id, actor_id, "assigned",
                       f"指派给 {assignee['username']}", ts)
    except sqlite3.IntegrityError:
        existing = _find_by_request(tid, key) if key else None
        if not existing:
            raise
        return _replay(tid, actor, existing)
    row = _task_row(tid, task_id)
    if assignee and int(assignee["id"]) != (actor_id or 0):
        _notify(tid, "staff_task_assigned", {
            "task_id": task_id, "title": title_text,
            "summary": f"{branch.get('name') or ''}：{title_text}",
            "branch": branch.get("name") or "",
            "due": timeutil.format_cn(due, "%m-%d %H:%M") if due else "",
        }, [assignee["id"]])
    item = _public(tid, [row], now=ts)[0]
    item["replayed"] = False
    return item


def _replay(tid: int, actor: Mapping[str, Any] | None, row: Mapping[str, Any]) -> dict:
    if actor is not None and not _can_view(actor, row):
        # 别人的请求编号撞上了：不泄露那条任务
        raise StaffTaskConflict("请求编号已被使用，请刷新后重试")
    item = _public(tid, [row])[0]
    item["replayed"] = True
    return item


def assign_task(
    tid: int,
    actor_user: Mapping[str, Any],
    task_id: Any,
    assignee_user_id: Any,
    *,
    due_at: Any = _UNSET,
    now: float | None = None,
) -> dict:
    """指派或改派(只有还没交的活能改派)。"""
    tid = int(tid)
    actor = _actor(tid, actor_user)
    row = _visible_task(tid, actor, task_id)
    if not _can_manage(actor, row):
        raise StaffTaskForbidden("你不能改派这条任务")
    if row["status"] != "todo":
        raise StaffTaskConflict("已经交了或结束的任务不能再改派")
    branch = _branch(tid, int(row["branch_id"]))
    assignee = _check_assignee(tid, branch, assignee_user_id, actor)
    due = row.get("due_at") if due_at is _UNSET else parse_due(due_at)
    ts = time.time() if now is None else float(now)
    old = row.get("assignee_user_id")
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE staff_task SET assignee_user_id=?,due_at=?,updated_at=? "
            "WHERE id=? AND tenant_id=? AND status='todo' AND deleted_at IS NULL",
            (int(assignee["id"]), due, ts, int(row["id"]), tid),
        ).rowcount
        if changed != 1:
            raise StaffTaskConflict("任务状态刚变了，请刷新后再试")
        note = ("改派给 " if old else "指派给 ") + str(assignee["username"])
        _event(connection, tid, int(row["id"]), int(actor["id"]), "assigned", note, ts)
    if int(assignee["id"]) != int(actor["id"]) and int(old or 0) != int(assignee["id"]):
        _notify(tid, "staff_task_assigned", {
            "task_id": int(row["id"]), "title": row["title"],
            "summary": f"{branch.get('name') or ''}：{row['title']}",
            "branch": branch.get("name") or "",
            "due": timeutil.format_cn(due, "%m-%d %H:%M") if due else "",
        }, [assignee["id"]])
    return _public(tid, [_task_row(tid, row["id"])], now=ts)[0]


def _can_submit(actor: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    if row.get("status") != "todo" or not _task_scope_visible(actor, row):
        return False
    assignee = row.get("assignee_user_id")
    if assignee is not None:
        return int(assignee) == int(actor["id"])
    branch_id = row.get("branch_id")
    if branch_id is None:
        return False
    try:
        branch = _branch(int(row["tenant_id"]), int(branch_id))
    except StaffTaskNotFound:
        return False
    return _branch_visible(actor, branch)


def submit_task(
    tid: int,
    actor_user: Mapping[str, Any],
    task_id: Any,
    *,
    photos: Sequence[bytes] = (),
    note: Any = "",
    now: float | None = None,
    store: Callable[..., dict] | None = None,
    asset_root: str | None = None,
) -> dict:
    """店员拍照交差。要照片的活没照片不能交；没指派人的活谁交就算谁的。

    照片先落盘再改状态：状态没改成(并发/重复点)就把刚存的照片删掉。
    同一个人重复提交(网络重试)返回已提交的结果(replayed=True)。
    """
    from . import photoproof
    tid = int(tid)
    actor = _actor(tid, actor_user)
    row = _visible_task(tid, actor, task_id)
    note_text = _text(note, field="备注", limit=MAX_NOTE)
    uid = int(actor["id"])
    if row["status"] == "submitted" and int(row.get("assignee_user_id") or 0) == uid:
        item = _public(tid, [row], with_photos=True)[0]
        item["replayed"] = True
        return item
    if row["status"] != "todo":
        raise StaffTaskConflict("这件事已经交过或已结束了")
    if not _can_submit(actor, row):
        raise StaffTaskForbidden("这件事没派给你，不能替别人交")
    blobs = [bytes(p) for p in (photos or []) if p]
    if len(blobs) > MAX_PHOTOS:
        raise StaffTaskError(f"一次最多交 {MAX_PHOTOS} 张照片")
    if int(row.get("require_photo") or 0) and not blobs:
        raise StaffTaskError("这件事要拍照交差，请先拍一张照片")
    branch = _branch(tid, int(row["branch_id"]), active_only=False)
    ts = time.time() if now is None else float(now)
    saver = store or photoproof.store_photo
    stored: list[dict] = []
    try:
        for blob in blobs:
            stored.append(saver(
                tid, int(branch["id"]), blob,
                branch_name=str(branch.get("name") or ""),
                person_name=str(actor.get("username") or ""),
                now=ts, asset_root=asset_root,
            ))
    except photoproof.PhotoError as exc:
        _discard(stored, asset_root)
        raise StaffTaskError(str(exc)) from None
    except Exception:
        _discard(stored, asset_root)
        raise
    try:
        with db.atomic() as connection:
            # 图片处理可能较慢，提交事务内再读当前权限，避免处理中撤权后
            # 仍凭请求开始时的指派/门店快照写入；失败会清理刚保存的图片。
            current_actor = _actor(tid, actor_user)
            current = _visible_task(tid, current_actor, row["id"])
            if current["status"] != "todo":
                raise _Lost()
            if not _can_submit(current_actor, current):
                raise StaffTaskForbidden("这件事没派给你，不能替别人交")
            changed = connection.execute(
                "UPDATE staff_task SET status='submitted',assignee_user_id=?,"
                "submitted_at=?,submit_note=?,ai_check_json=NULL,updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='todo' AND deleted_at IS NULL "
                "AND (assignee_user_id=? OR assignee_user_id IS NULL)",
                (uid, ts, note_text, ts, int(row["id"]), tid, uid),
            ).rowcount
            if changed != 1:
                raise _Lost()
            for meta in stored:
                connection.execute(
                    "INSERT INTO staff_task_photo(tenant_id,task_id,storage_key,sha256,"
                    "mime_type,byte_size,width,height,received_at,watermark_text,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, int(row["id"]), meta["storage_key"], meta["sha256"],
                     meta.get("mime_type") or "image/jpeg", int(meta["byte_size"]),
                     meta.get("width"), meta.get("height"),
                     float(meta.get("received_at") or ts),
                     meta.get("watermark_text") or "", uid, ts),
                )
            summary = f"交了 {len(stored)} 张照片" if stored else "没拍照直接交了"
            if note_text:
                summary += f"；备注：{note_text}"
            _event(connection, tid, int(row["id"]), uid, "submitted", summary, ts)
    except _Lost:
        _discard(stored, asset_root)
        latest = _visible_task(tid, _actor(tid, actor_user), row["id"])
        if latest["status"] == "submitted" and int(latest.get("assignee_user_id") or 0) == uid:
            item = _public(tid, [latest], with_photos=True)[0]
            item["replayed"] = True
            return item
        raise StaffTaskConflict("这件事刚被别人交了或改了，请刷新看看") from None
    except Exception:
        _discard(stored, asset_root)
        raise
    latest = _task_row(tid, row["id"])
    reviewers = _reviewer_ids(tid, latest)
    reviewers.discard(uid)
    _notify(tid, "staff_task_submitted", {
        "task_id": int(row["id"]), "title": row["title"],
        "summary": f"{actor.get('username') or ''} 交了「{row['title']}」"
                   + (f"（{len(stored)} 张照片）" if stored else ""),
        "branch": branch.get("name") or "",
        "user": actor.get("username") or "",
    }, reviewers)
    item = _public(tid, [latest], with_photos=True, now=ts)[0]
    item["replayed"] = False
    item["ai_check_pending"] = bool(stored) and ai_check_enabled(tid)
    return item


class _Lost(Exception):
    """并发下 CAS 没抢到。"""


def _discard(stored: Sequence[Mapping[str, Any]], asset_root: str | None) -> None:
    from . import photoproof
    for meta in stored:
        try:
            photoproof.remove_photo(str(meta.get("storage_key") or ""), asset_root=asset_root)
        except Exception:
            pass


def _reviewer_ids(tid: int, row: Mapping[str, Any]) -> set[int]:
    """交差通知发给派活人；系统派的活(或派活人已停用)发给老板。"""
    creator = _load_user(tid, row.get("created_by")) if row.get("created_by") else None
    if creator:
        return {int(creator["id"])}
    return {
        int(r["id"]) for r in db.q(
            "SELECT id FROM users WHERE tenant_id=? AND role IN ('owner','root') "
            "AND enabled=1", (int(tid),),
        )
    }


def review_task(
    tid: int,
    actor_user: Mapping[str, Any],
    task_id: Any,
    *,
    approve: bool,
    note: Any = "",
    now: float | None = None,
) -> dict:
    """审核：通过 → approved；打回 → 回到「待做」，原因写进 review_note。"""
    tid = int(tid)
    actor = _actor(tid, actor_user)
    row = _visible_task(tid, actor, task_id)
    if not _can_manage(actor, row):
        raise StaffTaskForbidden("只有老板或这家店的店长能审核")
    if int(row.get("assignee_user_id") or 0) == int(actor["id"]) \
            and not sees_all_branches(actor):
        raise StaffTaskForbidden("自己交的活不能自己审，请老板来看")
    if row["status"] != "submitted":
        raise StaffTaskConflict("这件事现在不是「等审核」状态")
    note_text = _text(note, field="审核意见", limit=MAX_NOTE)
    if not approve and not note_text:
        raise StaffTaskError("打回要写一句原因，店员才知道哪里要重做")
    ts = time.time() if now is None else float(now)
    target = "approved" if approve else "todo"
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE staff_task SET status=?,reviewed_at=?,reviewed_by=?,review_note=?,"
            "updated_at=? WHERE id=? AND tenant_id=? AND status='submitted' "
            "AND deleted_at IS NULL",
            (target, ts, int(actor["id"]), note_text, ts, int(row["id"]), tid),
        ).rowcount
        if changed != 1:
            raise StaffTaskConflict("任务状态刚变了，请刷新后再试")
        _event(connection, tid, int(row["id"]), int(actor["id"]),
               "approved" if approve else "rejected",
               note_text or ("通过" if approve else ""), ts)
    if row.get("assignee_user_id") and int(row["assignee_user_id"]) != int(actor["id"]):
        _notify(tid, "staff_task_reviewed", {
            "task_id": int(row["id"]), "title": row["title"], "approved": bool(approve),
            "summary": (f"「{row['title']}」通过了" if approve
                        else f"「{row['title']}」被打回：{note_text}"),
        }, [row["assignee_user_id"]])
    return _public(tid, [_task_row(tid, row["id"])], with_photos=True, now=ts)[0]


def cancel_task(
    tid: int,
    actor_user: Mapping[str, Any],
    task_id: Any,
    *,
    note: Any = "",
    now: float | None = None,
) -> dict:
    tid = int(tid)
    actor = _actor(tid, actor_user)
    row = _visible_task(tid, actor, task_id)
    if not (_can_manage(actor, row) or int(row.get("created_by") or 0) == int(actor["id"])):
        raise StaffTaskForbidden("你不能取消这条任务")
    if row["status"] not in ("todo", "submitted"):
        raise StaffTaskConflict("已经结束的任务不用再取消")
    note_text = _text(note, field="取消原因", limit=MAX_NOTE)
    ts = time.time() if now is None else float(now)
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE staff_task SET status='cancelled',updated_at=? WHERE id=? "
            "AND tenant_id=? AND status IN ('todo','submitted') AND deleted_at IS NULL",
            (ts, int(row["id"]), tid),
        ).rowcount
        if changed != 1:
            raise StaffTaskConflict("任务状态刚变了，请刷新后再试")
        _event(connection, tid, int(row["id"]), int(actor["id"]), "cancelled", note_text, ts)
    return _public(tid, [_task_row(tid, row["id"])], now=ts)[0]


# ---------------- 店员「我的待办」 ----------------
def _optional_module(name: str):
    try:
        import importlib
        return importlib.import_module(f"{__package__}.{name}")
    except ImportError:
        return None


def _checklist_items(tid: int, uid: int, today: str, now: float) -> list[dict]:
    module = _optional_module("checklist")
    func = getattr(module, "runs_for_user", None) if module else None
    if not callable(func):
        return []
    try:
        runs = func(int(tid), int(uid), today) or []
    except Exception:
        return []
    items = []
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        status = str(run.get("status") or "open")
        entries = run.get("items") if isinstance(run.get("items"), list) else []
        done = sum(1 for e in entries if isinstance(e, Mapping) and e.get("done"))
        due_at = run.get("due_at")
        items.append({
            **dict(run),
            "kind": "checklist",
            "id": run.get("id"),
            "title": str(run.get("name") or run.get("template_name")
                         or run.get("title") or "今日清单"),
            "branch_name": str(run.get("branch_name") or ""),
            "status": status,
            "due_at": due_at,
            "due_text": timeutil.format_cn(due_at, "%H:%M") if due_at else "",
            "progress": {"done": done, "total": len(entries)},
            "urgency": _urgency(due_at, now) if status == "open" else status,
        })
    return items


def _action_items(tid: int, uid: int, now: float) -> list[dict]:
    from . import inspection
    func = getattr(inspection, "actions_for_assignee", None)
    if not callable(func):
        return []
    try:
        actions = func(int(tid), int(uid)) or []
    except Exception:
        return []
    items = []
    for action in actions:
        if not isinstance(action, Mapping):
            continue
        status = str(action.get("status") or "open")
        due_at = action.get("due_at")
        items.append({
            **dict(action),
            "kind": "inspection_action",
            "id": action.get("id"),
            "title": str(action.get("issue_title") or action.get("title") or "巡店整改"),
            "branch_name": str(action.get("branch_name") or ""),
            "status": status,
            "due_at": due_at,
            "due_text": timeutil.format_cn(due_at, "%m-%d %H:%M") if due_at else "",
            "urgency": (_urgency(due_at, now)
                        if status in ("open", "in_progress", "reopened") else status),
        })
    return items


_URGENCY_RANK = {"overdue": 0, "today": 1, "later": 2}


def _sort_key(item: Mapping[str, Any]) -> tuple:
    rank = _URGENCY_RANK.get(str(item.get("urgency") or ""), 3)
    due = item.get("due_at")
    try:
        due_value = float(due) if due is not None else float("inf")
    except (TypeError, ValueError):
        due_value = float("inf")
    return (rank, due_value, str(item.get("kind") or ""), str(item.get("id") or ""))


def todo_for_user(tid: int, uid: int, *, now: float | None = None) -> dict:
    """店员「我的待办」：派给我的活 + 今天的清单 + 派给我的巡店整改。

    tasks/checklists/actions 三类分开返回；items 是合并后按
    「逾期 → 今天到期 → 其他 → 已交/已完成」排好序的列表，前端直接渲染。
    店长/老板还会拿到 reviews：自己能审核、等审核的活。
    """
    tid, uid = int(tid), int(uid)
    user = _load_user(tid, uid)
    if not user:
        raise StaffTaskForbidden("账号不存在或已停用")
    ts = time.time() if now is None else float(now)
    today = timeutil.today_cn(ts)
    bound = sorted(
        int(branch["id"])
        for branch in db.q(
            "SELECT sb.id,sb.tenant_id,sb.industry_key FROM store_branch sb "
            "JOIN user_branch ub ON ub.branch_id=sb.id AND ub.tenant_id=sb.tenant_id "
            "WHERE sb.tenant_id=? AND ub.user_id=?", (tid, uid),
        )
        if _industry_ok(user, branch)
    )
    rows = db.q(
        "SELECT * FROM staff_task WHERE tenant_id=? AND deleted_at IS NULL "
        "AND (status='todo' OR (status='submitted' AND submitted_at>=?)) "
        "AND (assignee_user_id=? OR (assignee_user_id IS NULL AND branch_id IN "
        "(SELECT branch_id FROM user_branch WHERE tenant_id=? AND user_id=?))) "
        "ORDER BY id DESC LIMIT 200",
        (tid, ts - 3 * 86400, uid, tid, uid),
    )
    tasks = [
        t for t in _public(tid, [r for r in rows if _can_view(user, r)], now=ts)
        if t["status"] == "todo" or t["assignee_user_id"] == uid
    ]
    for task in tasks:
        if task["status"] == "submitted":
            task["urgency"] = "waiting"
    checklists = _checklist_items(tid, uid, today, ts)
    actions = _action_items(tid, uid, ts)
    items = sorted([*tasks, *checklists, *actions], key=_sort_key)
    reviews: list[dict] = []
    if can_dispatch(user):
        review_rows = list_tasks(tid, user, status="submitted", limit=50)["items"]
        reviews = [r for r in review_rows if r.get("assignee_user_id") != uid
                   or sees_all_branches(user)]
    branch_names = _names(tid, "store_branch", bound, "name")
    return {
        "date": today,
        "date_text": f"{int(today[5:7])} 月 {int(today[8:10])} 日",
        "user": {
            "id": uid,
            "name": user.get("username") or "",
            "role_label": role_label(user),
            "can_dispatch": can_dispatch(user),
        },
        "branches": [{"id": b, "name": branch_names.get(b, "")} for b in bound],
        "tasks": tasks,
        "checklists": checklists,
        "actions": actions,
        "items": items,
        "reviews": reviews,
        "counts": {
            "overdue": sum(1 for i in items if i.get("urgency") == "overdue"),
            "today": sum(1 for i in items if i.get("urgency") == "today"),
            "todo": sum(1 for i in items if i.get("urgency") in _URGENCY_RANK),
            "reviews": len(reviews),
        },
    }


# ---------------- 一句话派活 ----------------
def _norm(text: Any) -> str:
    return re.sub(r"[\s·•,，。.、:：()（）]+", "", str(text or "")).lower()


def _strip_store(text: str) -> str:
    for suffix in ("门店", "分店", "店"):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def match_branch(text: Any, branches: Sequence[Mapping[str, Any]]) -> tuple[dict | None, str]:
    """按门店名在候选门店里找唯一匹配；0 个或多个都返回 None + 说明。"""
    raw = _norm(text)
    if not raw:
        return None, ""
    exact = [b for b in branches if _norm(b.get("name")) == raw]
    if len(exact) == 1:
        return dict(exact[0]), ""
    core = _strip_store(raw)
    if len(core) < 2:
        return None, f"没找到「{text}」这家店，请选一下"
    fuzzy = [
        b for b in branches
        if core in _norm(b.get("name"))
        or (len(_strip_store(_norm(b.get("name")))) >= 2
            and _strip_store(_norm(b.get("name"))) in raw)
    ]
    if len(fuzzy) == 1:
        return dict(fuzzy[0]), ""
    if len(fuzzy) > 1:
        return None, f"「{text}」对得上好几家店，请选一下"
    return None, f"没找到「{text}」这家店，请选一下"


_MANAGER_WORDS = ("店长", "经理", "负责人", "主管")


def match_member(text: Any, members: Sequence[Mapping[str, Any]]) -> tuple[dict | None, str]:
    """在这家店的人里找唯一匹配：「店长」按职级，其他按账号名；拿不准就留空。"""
    raw = _norm(text)
    if not raw:
        return None, ""
    if raw in _MANAGER_WORDS:
        managers = [m for m in members if m.get("job_title") == "manager"]
        if len(managers) == 1:
            return dict(managers[0]), ""
        return None, "这家店没有唯一的店长，请选一下由谁来做"
    exact = [m for m in members if _norm(m.get("name")) == raw]
    if len(exact) == 1:
        return dict(exact[0]), ""
    if len(raw) >= 2:
        fuzzy = [m for m in members if raw in _norm(m.get("name"))
                 or (len(_norm(m.get("name"))) >= 2 and _norm(m.get("name")) in raw)]
        if len(fuzzy) == 1:
            return dict(fuzzy[0]), ""
    return None, f"没对上「{text}」是谁，请选一下"


def build_parse_prompt(text: str, now: float) -> str:
    moment = timeutil.now_cn(now)
    weekday = "一二三四五六日"[moment.weekday()]
    return (
        "你是门店老板的派活助手。把老板说的一句话拆成要派给店员做的具体任务。\n"
        f"现在是北京时间 {moment.strftime('%Y-%m-%d %H:%M')}，星期{weekday}。\n"
        "规则：\n"
        "- 每件要做的事一条；同一件事派给多家店就按店拆成多条；最多 10 条；\n"
        "- title：要做的事，动词开头，20 字以内，例如「把冷柜清洗一遍」；\n"
        "- detail：补充要求，没有就空字符串；\n"
        "- store：原话里提到的门店名，照抄原文，没提就 null，不要猜；\n"
        "- person：原话里提到的人（如「店长」「小王」），照抄原文，没提就 null，不要猜；\n"
        "- due：截止时间换算成「YYYY-MM-DD HH:MM」（北京时间），例如「明天中午前」→ 明天 12:00，"
        "「今晚」→ 今天 21:00，只说日期没说几点就填 18:00，没提截止时间就 null；\n"
        "- require_photo：原话要求拍照/发照片/拍给我看就 true，明确说不用拍照就 false，没提默认 true。\n"
        '只输出一个 JSON 对象：{"tasks":[{"title":"","detail":"","store":null,'
        '"person":null,"due":null,"require_photo":true}]}\n\n'
        f"老板的原话：{text}"
    )


def normalize_parse_output(data: Any) -> list[dict]:
    """结构校验：必须是 {tasks:[...]}，每条至少有能用的 title；其他字段不合规就置空。"""
    items = data.get("tasks") if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        raise StaffTaskError("模型输出缺少 tasks 数组")
    drafts = []
    for item in items[: MAX_DRAFTS * 2]:
        if not isinstance(item, Mapping):
            continue
        title = item.get("title")
        if not isinstance(title, str):
            continue
        title = _CONTROL_RE.sub("", title).strip()[:MAX_TITLE]
        if not title:
            continue
        detail = item.get("detail")
        detail = _CONTROL_RE.sub("", detail).strip()[:MAX_DETAIL] \
            if isinstance(detail, str) else ""
        store = item.get("store")
        store = store.strip()[:40] if isinstance(store, str) else ""
        person = item.get("person")
        person = person.strip()[:20] if isinstance(person, str) else ""
        due = item.get("due")
        due = due.strip() if isinstance(due, str) else ""
        photo = item.get("require_photo")
        drafts.append({
            "title": title,
            "detail": detail,
            "store": store,
            "person": person,
            "due": due,
            "require_photo": photo if isinstance(photo, bool) else True,
        })
        if len(drafts) >= MAX_DRAFTS:
            break
    if not drafts:
        raise StaffTaskError("模型没拆出任何任务")
    return drafts


def _draft_due(text: str, now: float) -> float | None:
    if not text:
        return None
    try:
        due = parse_due(text)
    except StaffTaskError:
        return None
    # 过去超过 1 小时或 90 天以后的，多半是模型算错了，留空让老板填
    if due is None or due < now - 3600 or due > now + 90 * 86400:
        return None
    return due


def resolve_drafts(
    tid: int, actor: Mapping[str, Any], drafts: Sequence[Mapping[str, Any]], now: float,
) -> list[dict]:
    """把模型给的原话片段换成本企业真实的门店和人；对不上就留空并说明。"""
    branches = _dispatch_branches(tid, actor)
    single = branches[0] if len(branches) == 1 else None
    member_cache: dict[int, list[dict]] = {}
    out = []
    for draft in drafts:
        hints = []
        branch, why = match_branch(draft.get("store"), branches)
        if why:
            hints.append(why)
        if branch is None and not draft.get("store") and single is not None:
            branch = dict(single)
        assignee = None
        if branch is not None and draft.get("person"):
            bid = int(branch["id"])
            if bid not in member_cache:
                member_cache[bid] = [
                    m for m in branch_members(tid, branch)
                    if sees_all_branches(actor) or m["job_title"] in ("manager", "staff")
                ]
            assignee, why = match_member(draft.get("person"), member_cache[bid])
            if why:
                hints.append(why)
        elif draft.get("person") and branch is None:
            hints.append(f"先选门店，再选「{draft.get('person')}」是谁")
        due = _draft_due(str(draft.get("due") or ""), now)
        if draft.get("due") and due is None:
            hints.append("截止时间没算准，请手动选一下")
        out.append({
            "title": draft["title"],
            "detail": draft.get("detail") or "",
            "branch_id": int(branch["id"]) if branch else None,
            "branch_name": str(branch.get("name") or "") if branch else "",
            "assignee_user_id": int(assignee["id"]) if assignee else None,
            "assignee_name": str(assignee.get("name") or "") if assignee else "",
            "due_at": due,
            "due_text": timeutil.format_cn(due, "%Y-%m-%d %H:%M") if due else "",
            "require_photo": bool(draft.get("require_photo", True)),
            "said": {"store": draft.get("store") or "", "person": draft.get("person") or ""},
            "hints": hints,
        })
    return out


async def _default_parse_call(prompt: str, tid: int) -> dict:
    from . import providers
    return await providers.call_text_json(
        None, prompt, timeout=PARSE_TIMEOUT_S, retries=1, token=f"staff-parse:{tid}",
    )


async def parse_one_liner(
    tid: int,
    actor_user: Mapping[str, Any],
    text: Any,
    *,
    call: Callable | None = None,
    now: float | None = None,
) -> dict:
    """一句话派活：返回草稿列表(不落库)，老板在表单里确认/修改后再批量创建。

    先扣点再调模型；模型失败或输出不合规都退点。
    """
    tid = int(tid)
    actor = await db.arun(_actor, tid, actor_user)
    if not can_dispatch(actor):
        raise StaffTaskForbidden("店员账号不能派活，请让店长或老板来派")
    said = _text(text, field="要派的活", limit=MAX_ONE_LINER, required=True)
    ts = time.time() if now is None else float(now)
    op_key = await db.arun(_start_charge, PARSE_ACTION, tid, "一句话派活")
    try:
        result = await (call or _default_parse_call)(build_parse_prompt(said, ts), tid)
        drafts = normalize_parse_output((result or {}).get("data"))
    except StaffTaskError as exc:
        await db.arun(_finish_charge, op_key, False, "没拆出任务，自动退回")
        raise StaffTaskError("没听懂这句话，点数已退回。换个说法，或直接填表派活", 502) from exc
    except Exception as exc:
        await db.arun(_finish_charge, op_key, False, "模型调用失败，自动退回")
        raise StaffTaskError("这次没解析出来，点数已退回，请稍后再试或直接填表", 502) from exc
    await db.arun(_finish_charge, op_key, True)
    resolved = await db.arun(resolve_drafts, tid, actor, drafts, ts)
    points = await db.arun(_price_points, PARSE_ACTION) if op_key else 0
    return {"drafts": resolved, "points": points}


# ---------------- AI 验照片 ----------------
def build_ai_check_prompt(task: Mapping[str, Any]) -> str:
    return (
        "你是门店老板的助手，帮忙看店员交差的现场照片。\n"
        f"任务：{task.get('title') or ''}\n"
        + (f"要求：{task.get('detail')}\n" if task.get("detail") else "")
        + (f"店员备注：{task.get('submit_note')}\n" if task.get("submit_note") else "")
        + "判断这些照片能不能证明这件事做完了、做到位了。只看照片里真实能看到的，"
        "看不清或拍的不是这件事就说存疑；照片底部的水印是系统加的，不用评价。\n"
        "verdict 只能是 pass(照片能证明做好了)、doubt(拿不准，建议人再看看)、"
        "fail(明显没做或拍的不对)；reason 用一句大白话(40 字内)说理由；"
        "confidence 是 0 到 1 的把握。\n"
        '只输出 JSON：{"verdict":"pass","reason":"","confidence":0.8}'
    )


def normalize_ai_check(data: Any) -> dict:
    if not isinstance(data, Mapping):
        raise StaffTaskError("AI 验照片输出格式不对")
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in AI_VERDICTS:
        raise StaffTaskError("AI 验照片结论无效")
    reason = data.get("reason")
    reason = _CONTROL_RE.sub("", reason).strip()[:120] if isinstance(reason, str) else ""
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = 0.5
    confidence = float(confidence)
    if confidence != confidence:
        confidence = 0.5
    confidence = round(max(0.0, min(1.0, confidence)), 2)
    return {"verdict": verdict, "reason": reason, "confidence": confidence}


def _ai_check_context(tid: int, task_id: int, asset_root: str | None) -> dict | None:
    row = db.one(
        "SELECT id,title,detail,submit_note,status,submitted_at,ai_check_json "
        "FROM staff_task WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (int(task_id), int(tid)),
    )
    if not row or row["status"] != "submitted" or not row.get("submitted_at"):
        return None
    if row.get("ai_check_json"):
        return None   # 这一轮提交已经看过(或已记下没看成的原因)，不重复扣点
    photos = db.q(
        "SELECT storage_key FROM staff_task_photo WHERE tenant_id=? AND task_id=? "
        "AND created_at>=? ORDER BY id LIMIT ?",
        (int(tid), int(task_id), float(row["submitted_at"]), AI_CHECK_PHOTOS),
    )
    if not photos:
        return None
    from . import assetfiles
    root = os.path.realpath(asset_root or assetfiles.ASSET_ROOT)
    images = []
    for photo in photos:
        path = os.path.realpath(os.path.join(root, str(photo["storage_key"])))
        if os.path.commonpath((root, path)) != root:
            continue
        try:
            with open(path, "rb") as handle:
                images.append(("image/jpeg", base64.b64encode(handle.read()).decode()))
        except OSError:
            continue
    if not images:
        return None
    return {"task": row, "images": images}


def _save_ai_check(tid: int, task_id: int, submitted_at: float, result: dict,
                   ok: bool) -> bool:
    ts = time.time()
    payload = json.dumps({**result, "checked_at": ts}, ensure_ascii=False)
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE staff_task SET ai_check_json=?,updated_at=? WHERE id=? "
            "AND tenant_id=? AND status='submitted' AND submitted_at=?",
            (payload, ts, int(task_id), int(tid), float(submitted_at)),
        ).rowcount
        if changed == 1 and ok:
            label = {"pass": "像是做好了", "doubt": "拿不准", "fail": "像是没做好"}
            _event(connection, tid, task_id, None, "ai_checked",
                   f"AI 建议：{label.get(result['verdict'], '')}。{result.get('reason') or ''}",
                   ts)
    return changed == 1


async def _default_vision_call(prompt: str, images: list, tid: int) -> dict:
    from . import llm, providers
    result = await providers.call_vision(
        None, prompt, images, timeout=AI_CHECK_TIMEOUT_S,
        token=f"staff-ai-check:{tid}", max_tokens=400,
    )
    return llm.extract_json(result.get("text") or "")


async def run_ai_check(
    tid: int,
    task_id: int,
    *,
    call: Callable | None = None,
    asset_root: str | None = None,
) -> dict | None:
    """店员提交后异步跑：视觉模型看照片给建议。任何失败只记一句话、退点，不抛异常。"""
    tid, task_id = int(tid), int(task_id)
    try:
        if not await db.arun(ai_check_enabled, tid):
            return None
        context = await db.arun(_ai_check_context, tid, task_id, asset_root)
    except Exception:
        return None
    if not context:
        return None
    task = context["task"]
    submitted_at = float(task["submitted_at"])
    op_key = None
    try:
        op_key = await db.arun(
            _start_charge, AI_CHECK_ACTION, tid, f"任务 #{task_id}",
            f"staff-ai-check:{tid}:{task_id}:{submitted_at:.6f}",
        )
    except StaffTaskError as exc:      # 点数不足：记一句话给老板看，不调模型
        try:
            await db.arun(_save_ai_check, tid, task_id, submitted_at,
                          {"verdict": "", "reason": f"AI 没看照片：{exc}",
                           "confidence": 0, "error": True}, False)
        except Exception:
            pass
        return None
    except Exception:                  # 同一轮重复触发(计费编号已存在)等：静默跳过
        return None
    try:
        raw = await (call or _default_vision_call)(
            build_ai_check_prompt(task), context["images"], tid,
        )
        result = normalize_ai_check(raw)
    except Exception:
        await db.arun(_finish_charge, op_key, False, "AI 验照片没做成，自动退回")
        try:
            await db.arun(_save_ai_check, tid, task_id, submitted_at,
                          {"verdict": "", "reason": "AI 这次没看成，请直接人工看照片",
                           "confidence": 0, "error": True}, False)
        except Exception:
            pass
        return None
    try:
        saved = await db.arun(_save_ai_check, tid, task_id, submitted_at, result, True)
    except Exception:
        saved = False
    await db.arun(_finish_charge, op_key, True)
    return result if saved else None


# ---------------- 上传闸门(店员拍照提交) ----------------
class UploadGate:
    """店员交差照片的上传并发/频率闸门(与数字人等大文件上传分开)。

    巡店/数字人上传是“每个企业同一时间只允许一个”，放在门店收市时一群店员
    同时交差会大面积 429；这里按人限流：每人同时 1 个、每小时 60 次，
    全站同时最多 ``global_limit`` 个。
    """

    def __init__(self, *, global_limit: int = 8, per_user_hour: int = 60):
        self.global_limit = int(global_limit)
        self.per_user_hour = int(per_user_hour)
        self._lock = threading.Lock()
        self._active: set[tuple[int, int]] = set()
        self._hits: dict[tuple[int, int], list[float]] = {}

    def acquire(self, tid: int, uid: int, now: float | None = None) -> tuple[int, int]:
        key = (int(tid), int(uid))
        ts = time.time() if now is None else float(now)
        with self._lock:
            if key in self._active:
                raise StaffTaskError("上一张还在传，请等它传完", 429)
            if len(self._active) >= self.global_limit:
                raise StaffTaskError("现在交差的人有点多，请过几秒再点一次", 429)
            hits = [t for t in self._hits.get(key, []) if ts - t < 3600]
            if len(hits) >= self.per_user_hour:
                raise StaffTaskError("这一小时交得太多了，请稍后再交", 429)
            hits.append(ts)
            self._hits[key] = hits
            self._active.add(key)
            if len(self._hits) > 5000:
                self._hits = {k: v for k, v in self._hits.items()
                              if v and ts - v[-1] < 3600}
        return key

    def release(self, key: tuple[int, int]) -> None:
        with self._lock:
            self._active.discard(key)


UPLOAD_GATE = UploadGate()
