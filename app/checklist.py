"""开店 / 闭店 / 交班清单（第 2 期「真人派活闭环」）。

- 行业默认模板在 ``checklist_defaults_v1.json``：11 个行业各三套（开店/闭店/交班），
  另有一套通用兜底。租户第一次用清单时，按它当前开通的行业各复制一份到
  ``checklist_template``，之后老板可以增删改项、改截止时间、停用，互不影响别家。
- 每天（北京时间）给每家启用门店 × 同行业的启用模板生成一份 ``checklist_run``。
  库里有 UNIQUE(tenant_id,branch_id,template_id,run_date)，用 INSERT OR IGNORE 幂等。
  生成时把模板里的项目「拍快照」存进 run，老板中途改模板只影响之后生成的清单。
- 指派人：老板给门店指定的默认值班人 > 绑定该门店的店长 > 留空（门店里所有
  绑定员工都能做）。
- 打勾、拍照都不扣点。需要拍照的项必须有照片；照片走 ``photoproof.store_photo``，
  元数据存进 items_json 对应项。全部做完 → done；过了截止时间还没做完 → missed
  （由提醒循环 ``mark_missed`` 标记；超时之后补做完的仍记 missed，只补上完成时间）。

本模块不 import fastapi，全部函数可以直接在测试里调用。
"""
from __future__ import annotations

import calendar
import copy
import json
import os
import re
import secrets
import time
from typing import Any, Mapping

from . import db, photoproof, timeutil

DEFAULTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "checklist_defaults_v1.json"
)
GENERIC_INDUSTRY = "_generic"
DEFAULT_KINDS = ("open", "close", "handover")
KINDS = ("open", "close", "handover", "custom")
KIND_LABELS = {
    "open": "开店清单",
    "close": "闭店清单",
    "handover": "交班清单",
    "custom": "清单",
}
STATUS_LABELS = {"open": "待完成", "done": "已完成", "missed": "超时没做完"}
MAX_ITEMS = 30
ITEM_TEXT_MAX = 80
NAME_MAX = 30
NOTE_MAX = 200
_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PHOTO_FIELDS = (
    "storage_key", "url", "sha256", "mime_type", "byte_size", "width",
    "height", "received_at", "watermark_text",
)
_DUTY_KEY = "checklist_duty:{tid}"


class ChecklistError(ValueError):
    """参数不对（400），文案直接给用户看。"""

    status = 400


class ChecklistForbidden(ChecklistError):
    status = 403


class ChecklistNotFound(ChecklistError):
    status = 404


class ChecklistConflict(ChecklistError):
    status = 409


# ---------------- 默认模板 ----------------
_defaults_cache: dict | None = None


def load_defaults() -> dict:
    """读默认模板数据文件（进程内缓存，返回深拷贝防止调用方改坏缓存）。"""
    global _defaults_cache
    if _defaults_cache is None:
        with open(DEFAULTS_PATH, encoding="utf-8") as handle:
            _defaults_cache = json.load(handle)
    return copy.deepcopy(_defaults_cache)


def default_industries() -> list[str]:
    """有专门默认模板的行业 key（不含通用兜底）。"""
    return [key for key in load_defaults()["industries"] if key != GENERIC_INDUSTRY]


def default_templates(industry_key: str) -> dict[str, dict]:
    """某行业的三套默认清单；没有专门模板的行业用通用兜底。"""
    industries = load_defaults()["industries"]
    spec = industries.get(str(industry_key or "")) or industries[GENERIC_INDUSTRY]
    out = {}
    for kind in DEFAULT_KINDS:
        tpl = spec[kind]
        out[kind] = {
            "name": tpl["name"],
            "due_time": tpl["due_time"],
            "items": [
                {"key": item["key"], "text": item["text"],
                 "require_photo": bool(item.get("photo"))}
                for item in tpl["items"]
            ],
        }
    return out


# ---------------- 通用小工具 ----------------
def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _clean_text(value: Any, *, field: str, limit: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ChecklistError(f"{field}格式不对")
    clean = " ".join(value.split())
    if required and not clean:
        raise ChecklistError(f"{field}不能为空")
    if len(clean) > limit:
        raise ChecklistError(f"{field}最多 {limit} 个字")
    return clean


def _clean_due_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not _TIME_RE.match(value.strip()):
        raise ChecklistError("截止时间要写成 09:30 这样的格式")
    return value.strip()


def _clean_date(value: Any, now: float | None = None) -> str:
    if value in (None, ""):
        return timeutil.today_cn(_now(now))
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ChecklistError("日期要写成 2026-09-25 这样的格式")
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ChecklistError("日期不存在") from None
    return value


def cn_date_start_ts(run_date: str) -> float:
    """北京时间某天 00:00 的时间戳。"""
    parsed = time.strptime(run_date, "%Y-%m-%d")
    # calendar.timegm 按 UTC 解释，再减 8 小时即北京时间零点。
    return float(calendar.timegm(parsed) - timeutil.CN_OFFSET_SECONDS)


def due_ts(run_date: str, due_time: str) -> float:
    """清单截止时间戳：当天 due_time；没填截止时间按当天 23:59。"""
    start = cn_date_start_ts(run_date)
    match = _TIME_RE.match(str(due_time or ""))
    if not match:
        return start + 23 * 3600 + 59 * 60
    return start + int(match.group(1)) * 3600 + int(match.group(2)) * 60


def _new_key(existing: set[str]) -> str:
    while True:
        key = "x" + secrets.token_hex(3)
        if key not in existing:
            return key


def normalize_items(raw: Any) -> list[dict]:
    """老板提交的清单项 → [{key,text,require_photo}]。

    已有项保留原 key（历史清单按 key 对应）；新项自动生成 key。
    """
    if not isinstance(raw, list):
        raise ChecklistError("清单项格式不对")
    if not raw:
        raise ChecklistError("清单至少要有 1 项")
    if len(raw) > MAX_ITEMS:
        raise ChecklistError(f"一份清单最多 {MAX_ITEMS} 项")
    seen: set[str] = set()
    out: list[dict] = []
    pending: list[dict] = []
    for index, item in enumerate(raw, 1):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, Mapping):
            raise ChecklistError(f"第 {index} 项格式不对")
        text = _clean_text(item.get("text"), field=f"第 {index} 项内容",
                           limit=ITEM_TEXT_MAX)
        key = str(item.get("key") or "").strip()
        photo = item.get("require_photo", item.get("photo", False))
        entry = {"key": "", "text": text, "require_photo": bool(photo)}
        if key and _KEY_RE.match(key) and key not in seen:
            entry["key"] = key
            seen.add(key)
        else:
            pending.append(entry)
        out.append(entry)
    for entry in pending:
        entry["key"] = _new_key(seen)
        seen.add(entry["key"])
    return out


# ---------------- 账号与门店范围 ----------------
def _user(tid: int, uid: int) -> dict:
    row = db.one(
        "SELECT u.id,u.tenant_id,u.username,u.role,u.job_title,u.enabled,u.modules_json "
        "FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE u.id=? AND t.enabled=1",
        (int(uid),),
    )
    if not row or not int(row.get("enabled") or 0):
        raise ChecklistForbidden("账号不存在或已停用")
    role = str(row.get("role") or "")
    if int(row["tenant_id"]) != int(tid):
        raise ChecklistForbidden("不能查看其他企业的清单")
    if role not in {"root", "owner", "member"}:
        raise ChecklistForbidden("当前账号不能使用清单")
    modules = db.jloads(row.pop("modules_json", None), [])
    row["modules"] = modules if isinstance(modules, list) else []
    return row


def sees_all(user: Mapping[str, Any]) -> bool:
    """老板 / 平台管理员 / 总监看全部门店（与巡店口径一致）。"""
    from . import inspection
    return inspection.sees_all_branches(user)


def can_manage(user: Mapping[str, Any]) -> bool:
    """改模板、指定值班人：老板 / 平台管理员 / 总监。"""
    return sees_all(user)


def visible_branch_ids(tid: int, user: Mapping[str, Any]) -> list[int] | None:
    """返回当前可见门店；总监也受当前行业模块约束，不能用 None 绕过。"""
    return [int(row["id"]) for row in db.q(
        "SELECT id FROM store_branch WHERE tenant_id=? AND active=1 ORDER BY id",
        (int(tid),),
    ) if _branch_visible(tid, user, int(row["id"]))]


def _industry_visible(tid: int, user: Mapping[str, Any], industry: str) -> bool:
    if int(user.get("tenant_id") or 0) != int(tid):
        return False
    role = str(user.get("role") or "")
    entitled = set(_tenant_industries(tid))
    if industry and int(tid) != 1 and role != "root" and industry not in entitled:
        return False
    if role in {"owner", "root"}:
        return True
    modules = set(user.get("modules") or [])
    return industry in modules if industry else bool(modules & entitled)


def _branch_visible(tid: int, user: Mapping[str, Any], branch_id: int) -> bool:
    branch = db.one(
        "SELECT industry_key FROM store_branch WHERE id=? AND tenant_id=? AND active=1",
        (int(branch_id), int(tid)),
    )
    if not branch or not _industry_visible(tid, user, str(branch["industry_key"] or "")):
        return False
    if sees_all(user):
        return True
    return bool(db.one(
        "SELECT 1 AS ok FROM user_branch WHERE tenant_id=? AND user_id=? "
        "AND branch_id=?",
        (int(tid), int(user["id"]), int(branch_id)),
    ))


def _tenant_industries(tid: int) -> list[str]:
    """当前显式行业授权；已有门店不能恢复已撤销的授权。"""
    keys: list[str] = []
    for row in db.q(
        "SELECT industry_key FROM tenant_industry WHERE tenant_id=? "
        "ORDER BY is_primary DESC,industry_key",
        (int(tid),),
    ):
        key = str(row.get("industry_key") or "")
        if key and key not in keys:
            keys.append(key)
    return keys


# ---------------- 模板 ----------------
def ensure_templates(tid: int, *, actor_uid: int | None = None,
                     now: float | None = None) -> int:
    """首次使用：给还没有任何模板的行业复制三套默认清单。返回新建份数。

    判断和写入在同一个事务里，两个请求同时进来也不会建出两份。
    某行业只要建过模板（哪怕后来全停用了）就不再补，尊重老板的调整。
    """
    industries = _tenant_industries(tid)
    if not industries:
        return 0
    seeded = {
        str(row["industry_key"]) for row in db.q(
            "SELECT DISTINCT industry_key FROM checklist_template WHERE tenant_id=?",
            (int(tid),),
        )
    }
    if all(industry in seeded for industry in industries):
        return 0                      # 常态:不开写事务,提醒循环每 5 分钟调也不占锁
    ts = _now(now)
    created = 0
    with db.atomic() as connection:
        have = {
            str(row["industry_key"])
            for row in connection.execute(
                "SELECT DISTINCT industry_key FROM checklist_template "
                "WHERE tenant_id=?",
                (int(tid),),
            ).fetchall()
        }
        for industry in industries:
            if industry in have:
                continue
            for kind, tpl in default_templates(industry).items():
                connection.execute(
                    "INSERT INTO checklist_template(tenant_id,industry_key,kind,"
                    "name,items_json,due_time,active,created_by,created_at,"
                    "updated_at) VALUES(?,?,?,?,?,?,1,?,?,?)",
                    (int(tid), industry, kind, tpl["name"],
                     json.dumps(tpl["items"], ensure_ascii=False),
                     tpl["due_time"], actor_uid, ts, ts),
                )
                created += 1
    return created


def _template_public(row: Mapping[str, Any]) -> dict:
    items = db.jloads(row.get("items_json"), []) or []
    return {
        "id": int(row["id"]),
        "industry_key": str(row.get("industry_key") or ""),
        "kind": str(row.get("kind") or ""),
        "kind_label": KIND_LABELS.get(str(row.get("kind") or ""), "清单"),
        "name": str(row.get("name") or ""),
        "items": [
            {"key": str(item.get("key") or ""), "text": str(item.get("text") or ""),
             "require_photo": bool(item.get("require_photo"))}
            for item in items if isinstance(item, Mapping)
        ],
        "due_time": str(row.get("due_time") or ""),
        "active": bool(row.get("active")),
        "updated_at": row.get("updated_at"),
    }


def list_templates(tid: int, uid: int, *, now: float | None = None) -> list[dict]:
    """模板管理页：老板 / 总监可看可改。第一次打开时自动按行业生成。"""
    user = _user(tid, uid)
    if not can_manage(user):
        raise ChecklistForbidden("只有老板或总监可以管理清单模板")
    ensure_templates(tid, actor_uid=int(user["id"]), now=now)
    rows = db.q(
        "SELECT * FROM checklist_template WHERE tenant_id=? "
        "ORDER BY active DESC,industry_key,CASE kind WHEN 'open' THEN 0 "
        "WHEN 'handover' THEN 1 WHEN 'close' THEN 2 ELSE 3 END,id",
        (int(tid),),
    )
    return [_template_public(row) for row in rows
            if _industry_visible(tid, user, str(row.get("industry_key") or ""))]


def create_template(tid: int, uid: int, body: Mapping[str, Any], *,
                    now: float | None = None) -> dict:
    user = _user(tid, uid)
    if not can_manage(user):
        raise ChecklistForbidden("只有老板或总监可以管理清单模板")
    if not isinstance(body, Mapping):
        raise ChecklistError("提交内容格式不对")
    kind = str(body.get("kind") or "custom")
    if kind not in KINDS:
        raise ChecklistError("清单类型只能是开店、闭店、交班或自定义")
    industry = str(body.get("industry_key") or "").strip()
    if industry and industry not in _tenant_industries(tid):
        raise ChecklistError("这个行业还没有开通")
    if not _industry_visible(tid, user, industry):
        raise ChecklistForbidden("当前账号未开通这个行业")
    name = _clean_text(body.get("name") or KIND_LABELS[kind], field="清单名称",
                       limit=NAME_MAX)
    items = normalize_items(body.get("items"))
    due_time = _clean_due_time(body.get("due_time"))
    ts = _now(now)
    template_id = db.insert("checklist_template", {
        "tenant_id": int(tid), "industry_key": industry, "kind": kind,
        "name": name, "items_json": json.dumps(items, ensure_ascii=False),
        "due_time": due_time, "active": 1, "created_by": int(user["id"]),
        "created_at": ts, "updated_at": ts,
    })
    return _template_public(db.one(
        "SELECT * FROM checklist_template WHERE id=?", (template_id,)))


def update_template(tid: int, uid: int, template_id: int,
                    patch: Mapping[str, Any], *, now: float | None = None) -> dict:
    """改名称 / 增删改项 / 改截止时间 / 停用启用。只影响之后生成的清单。"""
    user = _user(tid, uid)
    if not can_manage(user):
        raise ChecklistForbidden("只有老板或总监可以管理清单模板")
    if not isinstance(patch, Mapping):
        raise ChecklistError("提交内容格式不对")
    row = db.one(
        "SELECT * FROM checklist_template WHERE id=? AND tenant_id=?",
        (int(template_id), int(tid)),
    )
    if not row or not _industry_visible(tid, user, str(row.get("industry_key") or "")):
        raise ChecklistNotFound("清单模板不存在")
    changes: dict[str, Any] = {}
    if "name" in patch:
        changes["name"] = _clean_text(patch.get("name"), field="清单名称",
                                      limit=NAME_MAX)
    if "items" in patch:
        changes["items_json"] = json.dumps(normalize_items(patch.get("items")),
                                           ensure_ascii=False)
    if "due_time" in patch:
        changes["due_time"] = _clean_due_time(patch.get("due_time"))
    if "active" in patch:
        if not isinstance(patch.get("active"), bool):
            raise ChecklistError("启用状态格式不对")
        changes["active"] = 1 if patch["active"] else 0
    if not changes:
        return _template_public(row)
    changes["updated_at"] = _now(now)
    sets = ",".join(f"{key}=?" for key in changes)
    db.execute(
        f"UPDATE checklist_template SET {sets} WHERE id=? AND tenant_id=?",
        (*changes.values(), int(template_id), int(tid)),
    )
    return _template_public(db.one(
        "SELECT * FROM checklist_template WHERE id=?", (int(template_id),)))


# ---------------- 值班人 / 指派 ----------------
def duty_map(tid: int) -> dict[int, int]:
    """老板给门店指定的默认值班人 {branch_id: user_id}（存 app_setting）。"""
    raw = db.jloads(db.get_setting(_DUTY_KEY.format(tid=int(tid))), {}) or {}
    out = {}
    for key, value in raw.items() if isinstance(raw, dict) else ():
        try:
            out[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def set_duty_user(tid: int, actor_uid: int, branch_id: int,
                  user_id: int | None) -> dict:
    """指定（或清空）某门店清单的默认值班人；值班人必须绑定了这家门店。"""
    actor = _user(tid, actor_uid)
    if not can_manage(actor):
        raise ChecklistForbidden("只有老板或总监可以指定值班人")
    branch = db.one(
        "SELECT id FROM store_branch WHERE id=? AND tenant_id=?",
        (int(branch_id), int(tid)),
    )
    if not branch or not _branch_visible(tid, actor, int(branch_id)):
        raise ChecklistNotFound("门店不存在")
    duties = duty_map(tid)
    if user_id in (None, "", 0):
        duties.pop(int(branch_id), None)
    else:
        bound = db.one(
            "SELECT u.id FROM user_branch ub JOIN users u ON u.id=ub.user_id "
            "AND u.tenant_id=ub.tenant_id WHERE ub.tenant_id=? AND ub.branch_id=? "
            "AND ub.user_id=? AND u.enabled=1",
            (int(tid), int(branch_id), int(user_id)),
        )
        if not bound or not _branch_visible(tid, _user(tid, int(user_id)), int(branch_id)):
            raise ChecklistError("值班人要先在团队页绑定到这家门店")
        duties[int(branch_id)] = int(user_id)
    db.set_setting(
        _DUTY_KEY.format(tid=int(tid)),
        json.dumps({str(k): v for k, v in sorted(duties.items())}) if duties else None,
    )
    return {"branch_id": int(branch_id), "user_id": duties.get(int(branch_id))}


def branch_managers(tid: int, branch_id: int) -> list[int]:
    """绑定该门店的店长（member + job_title=manager，启用中）。"""
    return [int(row["id"]) for row in db.q(
        "SELECT u.id FROM user_branch ub JOIN users u ON u.id=ub.user_id "
        "AND u.tenant_id=ub.tenant_id WHERE ub.tenant_id=? AND ub.branch_id=? "
        "AND u.role='member' AND u.job_title='manager' AND u.enabled=1 "
        "ORDER BY u.id",
        (int(tid), int(branch_id)),
    )]


def branch_members(tid: int, branch_id: int) -> list[int]:
    """绑定该门店的全部启用账号（店长 + 店员）。"""
    return [int(row["id"]) for row in db.q(
        "SELECT u.id FROM user_branch ub JOIN users u ON u.id=ub.user_id "
        "AND u.tenant_id=ub.tenant_id WHERE ub.tenant_id=? AND ub.branch_id=? "
        "AND u.enabled=1 ORDER BY u.id",
        (int(tid), int(branch_id)),
    )]


def default_assignee(tid: int, branch_id: int,
                     duties: Mapping[int, int] | None = None) -> int | None:
    """默认值班人（仍绑定该店且启用） > 店长 > 留空。"""
    duties = duty_map(tid) if duties is None else duties
    duty = duties.get(int(branch_id))
    if duty and duty in branch_members(tid, branch_id):
        return int(duty)
    managers = branch_managers(tid, branch_id)
    return managers[0] if managers else None


# ---------------- 每日生成 ----------------
def generate_runs(tid: int | None = None, *, date: str | None = None,
                  now: float | None = None) -> int:
    """给每家启用门店 × 同行业启用模板生成当天清单（幂等）。返回新生成份数。

    门店或模板是在当天截止时间之后才建的，当天这份不生成：
    新开通的老板不会一上来就收到一堆「超时没做」。
    """
    ts = _now(now)
    run_date = _clean_date(date, ts)
    if tid is None:
        tenants = [int(row["id"]) for row in db.q(
            "SELECT DISTINCT t.id FROM tenants t JOIN store_branch b "
            "ON b.tenant_id=t.id AND b.active=1 WHERE t.enabled=1 ORDER BY t.id"
        )]
    else:
        tenants = [int(tid)]
    created = 0
    for tenant_id in tenants:
        created += _generate_for_tenant(tenant_id, run_date, ts)
    return created


def _generate_for_tenant(tid: int, run_date: str, ts: float) -> int:
    branches = db.q(
        "SELECT id,industry_key,created_at FROM store_branch WHERE tenant_id=? "
        "AND active=1 ORDER BY id",
        (int(tid),),
    )
    if not branches:
        return 0
    ensure_templates(tid, now=ts)
    templates = db.q(
        "SELECT id,industry_key,kind,items_json,due_time,created_at "
        "FROM checklist_template WHERE tenant_id=? AND active=1 ORDER BY id",
        (int(tid),),
    )
    if not templates:
        return 0
    existing = {
        (int(row["branch_id"]), int(row["template_id"])) for row in db.q(
            "SELECT branch_id,template_id FROM checklist_run WHERE tenant_id=? "
            "AND run_date=?",
            (int(tid), run_date),
        )
    }
    todo = []
    for branch in branches:
        for tpl in templates:
            industry = str(tpl.get("industry_key") or "")
            if industry and industry != str(branch.get("industry_key") or ""):
                continue
            if (int(branch["id"]), int(tpl["id"])) in existing:
                continue
            due_at = due_ts(run_date, str(tpl.get("due_time") or ""))
            born = max(float(tpl.get("created_at") or 0),
                       float(branch.get("created_at") or 0))
            if born >= due_at:
                continue
            todo.append((branch, tpl, due_at))
    if not todo:
        return 0                      # 今天的都生成过了:不开写事务
    duties = duty_map(tid)
    assignees: dict[int, int | None] = {}
    created = 0
    with db.atomic() as connection:
        for branch, tpl, due_at in todo:
            if int(branch["id"]) not in assignees:
                assignees[int(branch["id"])] = default_assignee(
                    tid, int(branch["id"]), duties)
            assignee = assignees[int(branch["id"])]
            items = [
                {"key": item.get("key"), "text": item.get("text"),
                 "require_photo": bool(item.get("require_photo")),
                 "done": False}
                for item in (db.jloads(tpl.get("items_json"), []) or [])
                if isinstance(item, Mapping) and item.get("key")
            ]
            cursor = connection.execute(
                "INSERT OR IGNORE INTO checklist_run(tenant_id,branch_id,"
                "template_id,run_date,kind,status,assignee_user_id,items_json,"
                "due_at,created_at,updated_at) VALUES(?,?,?,?,?,'open',?,?,?,?,?)",
                (int(tid), int(branch["id"]), int(tpl["id"]), run_date,
                 str(tpl["kind"]), assignee,
                 json.dumps(items, ensure_ascii=False), due_at, ts, ts),
            )
            created += int(cursor.rowcount or 0)
    return created


def mark_missed(tid: int | None = None, *, now: float | None = None) -> list[dict]:
    """过了截止时间还没做完的清单标成 missed，返回这次新标的清单。"""
    ts = _now(now)
    params: tuple = (ts,)
    where = "status='open' AND due_at IS NOT NULL AND due_at<=?"
    if tid is not None:
        where += " AND tenant_id=?"
        params += (int(tid),)
    rows = db.q(
        f"SELECT id,tenant_id,branch_id,kind,run_date FROM checklist_run "
        f"WHERE {where}",
        params,
    )
    missed: list[dict] = []
    if not rows:
        return missed                 # 常态:不开写事务
    with db.atomic() as connection:
        for row in rows:
            changed = connection.execute(
                "UPDATE checklist_run SET status='missed',updated_at=? "
                "WHERE id=? AND status='open'",
                (ts, int(row["id"])),
            ).rowcount
            if changed:
                missed.append(dict(row))
    return missed


# ---------------- 读 ----------------
def _names(tid: int, run_rows: list[dict]) -> tuple[dict, dict, dict]:
    branch_ids = sorted({int(r["branch_id"]) for r in run_rows})
    template_ids = sorted({int(r["template_id"]) for r in run_rows})
    user_ids = set()
    for r in run_rows:
        if r.get("assignee_user_id"):
            user_ids.add(int(r["assignee_user_id"]))
        for item in db.jloads(r.get("items_json"), []) or []:
            if isinstance(item, Mapping) and item.get("done_by"):
                try:
                    user_ids.add(int(item["done_by"]))
                except (TypeError, ValueError):
                    pass

    def lookup(table: str, column: str, ids) -> dict[int, str]:
        ids = list(ids)
        out: dict[int, str] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in db.q(
                f"SELECT id,{column} AS label FROM {table} "
                f"WHERE tenant_id=? AND id IN ({marks})",
                (int(tid), *chunk),
            ):
                out[int(row["id"])] = str(row.get("label") or "")
        return out

    return (lookup("store_branch", "name", branch_ids),
            lookup("checklist_template", "name", template_ids),
            lookup("users", "username", sorted(user_ids)))


def _run_public(row: Mapping[str, Any], *, branches: Mapping[int, str],
                templates: Mapping[int, str], users: Mapping[int, str],
                viewer_id: int | None = None, now: float | None = None) -> dict:
    ts = _now(now)
    items = []
    for item in db.jloads(row.get("items_json"), []) or []:
        if not isinstance(item, Mapping):
            continue
        photo = item.get("photo") if isinstance(item.get("photo"), Mapping) else None
        done_by = item.get("done_by")
        items.append({
            "key": str(item.get("key") or ""),
            "text": str(item.get("text") or ""),
            "require_photo": bool(item.get("require_photo")),
            "done": bool(item.get("done")),
            "done_at": item.get("done_at"),
            "done_by": done_by,
            "done_by_name": users.get(int(done_by), "") if done_by else "",
            "note": str(item.get("note") or ""),
            "photo_url": str(photo.get("url") or "") if photo else "",
            "photo_taken_at": photo.get("received_at") if photo else None,
        })
    done = sum(1 for item in items if item["done"])
    kind = str(row.get("kind") or "")
    status = str(row.get("status") or "open")
    assignee = row.get("assignee_user_id")
    due_at = row.get("due_at")
    return {
        "id": int(row["id"]),
        "branch_id": int(row["branch_id"]),
        "branch_name": branches.get(int(row["branch_id"]), ""),
        "template_id": int(row["template_id"]),
        "name": templates.get(int(row["template_id"]), "") or KIND_LABELS.get(kind, "清单"),
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, "清单"),
        "run_date": str(row.get("run_date") or ""),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "due_at": due_at,
        "due_text": (timeutil.format_cn(due_at, "%H:%M") + " 前") if due_at else "",
        "overdue": bool(status == "open" and due_at and float(due_at) <= ts),
        "assignee_user_id": int(assignee) if assignee else None,
        "assignee_name": users.get(int(assignee), "") if assignee else "",
        "assigned_to_me": bool(viewer_id and assignee and int(assignee) == int(viewer_id)),
        "completed_at": row.get("completed_at"),
        "done_count": done,
        "total": len(items),
        "items": items,
    }


def _public_rows(tid: int, rows: list[dict], *, viewer_id: int | None,
                 now: float | None) -> list[dict]:
    branches, templates, users = _names(tid, rows)
    return [
        _run_public(row, branches=branches, templates=templates, users=users,
                    viewer_id=viewer_id, now=now)
        for row in rows
    ]


def runs_for_user(tid: int, uid: int, date: str | None = None, *,
                  now: float | None = None) -> list[dict]:
    """店员「我的待办」里的清单：指派给我的 + 我绑定门店的（没指派的大家都能做）。

    老板 / 总监不依赖绑定，这里只返回指派给他本人或他绑定门店的清单，
    全部门店的完成情况看 ``runs_overview``。
    """
    user = _user(tid, uid)
    run_date = _clean_date(date, now)
    rows = db.q(
        "SELECT * FROM checklist_run WHERE tenant_id=? AND run_date=? AND ("
        "assignee_user_id=? OR branch_id IN (SELECT branch_id FROM user_branch "
        "WHERE tenant_id=? AND user_id=?)) "
        "AND branch_id IN (SELECT id FROM store_branch WHERE tenant_id=? AND active=1) "
        "ORDER BY CASE WHEN assignee_user_id=? THEN 0 ELSE 1 END,"
        "CASE status WHEN 'open' THEN 0 WHEN 'missed' THEN 1 ELSE 2 END,due_at,id "
        "LIMIT 200",
        (int(tid), run_date, int(user["id"]), int(tid), int(user["id"]),
         int(tid), int(user["id"])),
    )
    visible = set(visible_branch_ids(tid, user))
    rows = [row for row in rows if int(row["branch_id"]) in visible]
    return _public_rows(tid, rows, viewer_id=int(user["id"]), now=now)


def runs_overview(tid: int, uid: int, date: str | None = None, *,
                  now: float | None = None) -> dict:
    """老板端「今天各店完成情况」：按门店分组，经理 / 员工只看绑定门店。"""
    user = _user(tid, uid)
    run_date = _clean_date(date, now)
    visible = set(visible_branch_ids(tid, user))
    branches = [row for row in db.q(
        "SELECT id,name,region FROM store_branch WHERE tenant_id=? AND active=1 "
        "ORDER BY region,name,id", (int(tid),),
    ) if int(row["id"]) in visible]
    if not branches:
        return {"date": run_date, "stores": [], "summary": {
            "total": 0, "done": 0, "missed": 0, "open": 0, "rate": None}}
    ids = [int(b["id"]) for b in branches]
    rows: list[dict] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        rows += db.q(
            f"SELECT * FROM checklist_run WHERE tenant_id=? AND run_date=? "
            f"AND branch_id IN ({marks}) ORDER BY due_at,id",
            (int(tid), run_date, *chunk),
        )
    public = _public_rows(tid, rows, viewer_id=int(user["id"]), now=now)
    by_branch: dict[int, list[dict]] = {}
    for run in public:
        by_branch.setdefault(run["branch_id"], []).append(run)
    stores = []
    totals = {"total": 0, "done": 0, "missed": 0, "open": 0}
    for branch in branches:
        runs = by_branch.get(int(branch["id"]), [])
        counts = {
            "total": len(runs),
            "done": sum(1 for r in runs if r["status"] == "done"),
            "missed": sum(1 for r in runs if r["status"] == "missed"),
            "open": sum(1 for r in runs if r["status"] == "open"),
        }
        for key in totals:
            totals[key] += counts[key]
        stores.append({
            "branch_id": int(branch["id"]),
            "branch_name": str(branch.get("name") or ""),
            "region": str(branch.get("region") or ""),
            **counts,
            "runs": runs,
        })
    rate = round(totals["done"] * 100.0 / totals["total"], 1) if totals["total"] else None
    return {"date": run_date, "stores": stores, "summary": {**totals, "rate": rate}}


# ---------------- 打勾 / 拍照 ----------------
def _accept_photo(tid: int, run: Mapping[str, Any], user: Mapping[str, Any],
                  photo: Any, *, now: float, asset_root: str | None) -> dict | None:
    """photo 可以是 {"data": bytes}（在这里压缩水印落盘），也可以是已经
    ``photoproof.store_photo`` 过的元数据（只接受本门店目录下的照片）。"""
    if photo is None:
        return None
    if not isinstance(photo, Mapping):
        raise ChecklistError("照片格式不对，请重新拍一张")
    branch_id = int(run["branch_id"])
    if photo.get("data") is not None:
        data = photo["data"]
        if not isinstance(data, (bytes, bytearray)):
            raise ChecklistError("照片格式不对，请重新拍一张")
        meta = photoproof.store_photo(
            tid, branch_id, bytes(data),
            branch_name=str(run.get("branch_name") or ""),
            person_name=str(user.get("username") or ""),
            now=now, asset_root=asset_root,
        )
        meta["_fresh"] = True
        return meta
    key = str(photo.get("storage_key") or "")
    prefix = f"staff/{int(tid)}/{branch_id}/"
    if not key.startswith(prefix) or not photoproof.STAFF_FILE_RE.match("/files/" + key):
        raise ChecklistError("照片不属于这家门店，请重新拍一张")
    meta = {field: photo.get(field) for field in _PHOTO_FIELDS}
    meta["storage_key"] = key
    meta["url"] = "/files/" + key
    return meta


def complete_item(tid: int, uid: int, run_id: int, item_key: str, *,
                  photo: dict | None = None, note: str = "", done: bool = True,
                  now: float | None = None, asset_root: str | None = None) -> dict:
    """店员给清单某一项打勾（或取消打勾），可附照片和备注。返回整份清单。

    - 需要拍照的项：本次带照片或之前已经拍过，才能打勾。
    - 全部打完 → 截止前完成记 done；截止后补做完仍记 missed，只补完成时间。
    - 已经完成（done）的清单不能再改。
    """
    user = _user(tid, uid)
    ts = _now(now)
    key = str(item_key or "")
    if not _KEY_RE.match(key):
        raise ChecklistNotFound("这一项不存在")
    clean_note = _clean_text(note, field="备注", limit=NOTE_MAX, required=False)
    run = db.one(
        "SELECT r.*,b.name AS branch_name FROM checklist_run r "
        "JOIN store_branch b ON b.id=r.branch_id AND b.tenant_id=r.tenant_id "
        "WHERE r.id=? AND r.tenant_id=?",
        (int(run_id), int(tid)),
    )
    if not run or not _branch_visible(tid, user, int(run["branch_id"])):
        # 与不存在同样 404，不泄露别的门店。
        raise ChecklistNotFound("清单不存在或不是你负责的门店")
    if str(run["status"]) == "done":
        raise ChecklistConflict("这份清单已经全部做完了")
    items = db.jloads(run.get("items_json"), []) or []
    target = next((i for i in items if isinstance(i, Mapping) and i.get("key") == key), None)
    if target is None:
        raise ChecklistNotFound("这一项不存在")
    had_photo = isinstance(target.get("photo"), Mapping)
    if done and target.get("require_photo") and photo is None and not had_photo:
        raise ChecklistError("这一项要拍照，请先拍一张再打勾")
    meta = _accept_photo(tid, run, user, photo, now=ts, asset_root=asset_root)
    fresh = bool(meta and meta.pop("_fresh", False))
    replaced_key = ""
    try:
        with db.atomic() as connection:
            user = _user(tid, uid)
            current = connection.execute(
                "SELECT status,items_json,branch_id FROM checklist_run WHERE id=? AND tenant_id=?",
                (int(run_id), int(tid)),
            ).fetchone()
            if not current or not _branch_visible(tid, user, int(current["branch_id"])):
                raise ChecklistNotFound("清单不存在或不是你负责的门店")
            if int(current["branch_id"]) != int(run["branch_id"]):
                raise ChecklistConflict("清单所属门店刚刚改变，请刷新重试")
            if str(current["status"]) == "done":
                raise ChecklistConflict("这份清单已经全部做完了")
            fresh_items = db.jloads(current["items_json"], []) or []
            entry = next((i for i in fresh_items
                          if isinstance(i, dict) and i.get("key") == key), None)
            if entry is None:
                raise ChecklistNotFound("这一项不存在")
            if meta is not None:
                old = entry.get("photo")
                if isinstance(old, Mapping) and old.get("storage_key") != meta["storage_key"]:
                    replaced_key = str(old.get("storage_key") or "")
                entry["photo"] = meta
            if done and entry.get("require_photo") and not isinstance(entry.get("photo"), Mapping):
                raise ChecklistError("这一项要拍照，请先拍一张再打勾")
            if clean_note:
                entry["note"] = clean_note
            if done:
                entry.update({"done": True, "done_at": ts, "done_by": int(user["id"])})
            else:
                entry.update({"done": False, "done_at": None, "done_by": None})
            all_done = bool(fresh_items) and all(
                bool(i.get("done")) for i in fresh_items if isinstance(i, Mapping))
            status = str(current["status"])
            completed_at = None
            completed_by = None
            if all_done:
                completed_at, completed_by = ts, int(user["id"])
                due_at = run.get("due_at")
                if status == "open" and (due_at is None or ts <= float(due_at)):
                    status = "done"
                else:
                    status = "missed"
            connection.execute(
                "UPDATE checklist_run SET items_json=?,status=?,completed_at=?,"
                "completed_by=?,updated_at=? WHERE id=? AND tenant_id=?",
                (json.dumps(fresh_items, ensure_ascii=False), status, completed_at,
                 completed_by, ts, int(run_id), int(tid)),
            )
    except BaseException:
        if fresh and meta:
            photoproof.remove_photo(meta["storage_key"], asset_root=asset_root)
        raise
    if replaced_key:
        photoproof.remove_photo(replaced_key, asset_root=asset_root)
    row = db.one("SELECT * FROM checklist_run WHERE id=?", (int(run_id),))
    return _public_rows(tid, [row], viewer_id=int(user["id"]), now=ts)[0]
