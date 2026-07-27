"""全局任务中心：把分散在各业务表里的真实任务汇总成一张老板总账。

这里只做只读聚合，不复制任务、不维护第二套状态。每一条记录都保留原业务表 ID，
前端据此跳回专家任务、内容工单、会议室、摄影棚、工具箱或发布渠道。
"""
from __future__ import annotations

import json

from . import db, departments
from .skills import registry


KIND_META = {
    "expert": ("🧑‍💼", "数字员工任务"),
    "content": ("📝", "内容工单"),
    "meeting": ("🪑", "AI会议"),
    "avatar": ("🎥", "数字人视频"),
    "video": ("🎬", "图文成片"),
    "tool": ("🧰", "营销工具"),
    "publish": ("📣", "发布任务"),
    "wechat": ("📮", "公众号草稿投递"),
}

STATUS_LABELS = {
    "queued": "排队中",
    "running": "进行中",
    "brainstorm": "提案中",
    "validate": "验证中",
    "execute": "决策中",
    "executing": "启动执行",
    "pending_charge": "准备计费",
    "processing": "正在准备草稿",
    "submitting": "正在投递",
    "submitted": "已提交 · 正在确认",
    "blocked": "合规审查待处理",
    "awaiting_review": "等您拍板",
    "gate_blocked": "审查待处理",
    "awaiting_execution": "等待执行",
    "paused": "已暂停 · 等您恢复",
    "done": "已完成",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "stopped": "已停止",
}

ACTIVE = {
    "queued", "running", "brainstorm", "validate", "execute", "executing",
    "pending_charge", "processing", "submitting", "submitted",
}
WAITING = {"awaiting_review", "gate_blocked", "awaiting_execution", "paused", "blocked"}
DONE = {"done", "completed", "success", "succeed"}
FAILED = {"failed", "error"}
CANCELLED = {"cancelled", "stopped"}

TOOL_NAMES = {"hot": "今日必发", "pcal": "私域日历", "warm": "起号军师",
              "leads": "线索雷达", "bench": "竞品盯梢"}
MAX_FREE_RETRIES = 3
PUBLISH_SUBMIT_MARKERS = (
    "提交发布", "已提交", "发布成功", "submission_uncertain",
)


def retry_meta(kind: str, row: dict, *, usable_delivery: bool = False) -> dict:
    """返回服务端唯一可信的免费重试能力。

    前端只能按这里的 ``retryable`` 展示按钮，不能根据 ``failed`` 自行猜测。
    涉及真实平台投递的任务只在持久状态明确证明“尚未提交”时放行。
    """
    retries = max(0, int(row.get("retry_count") or 0))
    remaining = max(0, MAX_FREE_RETRIES - retries)
    status = str(row.get("status") or "").lower()
    billing_status = str(row.get("billing_status") or "").lower()
    result = {
        "retryable": False,
        "free_retries_remaining": remaining,
        "retry_block_reason": "",
    }

    if kind == "wechat":
        if status in {"processing", "submitting", "submitted"}:
            result["retry_block_reason"] = (
                "公众号投递可能已到达平台，系统会先对账；为避免重复草稿，"
                "不能自动重发。请回原内容工单查看或人工确认。"
            )
        elif status == "failed":
            result["retry_block_reason"] = (
                "旧投递没有可安全复用的完整原始载荷，请回原内容工单核对"
                "标题和正文后重新发起。"
            )
        elif status == "blocked":
            result["retry_block_reason"] = (
                "该投递正在人工或合规处理，不能自动重发。请回原内容工单处理。"
            )
        return result

    if status != "failed":
        return result
    if remaining <= 0:
        result["retry_block_reason"] = "免费重试次数已用完，请新建任务。"
        return result

    if kind in {"expert", "avatar", "video", "tool", "meeting"}:
        if billing_status in {"refunded", "included"}:
            result["retryable"] = True
        else:
            result["retry_block_reason"] = (
                "任务费用结算尚未完成，请稍后刷新再试。"
            )
        return result

    if kind == "content":
        if billing_status in {"refunded", "included"}:
            result["retryable"] = True
        elif billing_status == "charged" and usable_delivery:
            # 正文已形成时不会整单退款；允许沿原工单继续，不再扣点。
            result["retryable"] = True
        else:
            result["retry_block_reason"] = (
                "工单退款结算尚未完成，请稍后刷新再试。"
            )
        return result

    if kind == "publish":
        submission_state = str(
            row.get("submission_state") or "legacy_unknown"
        ).lower()
        log_text = str(row.get("log") or "")
        has_submit_marker = any(
            marker in log_text for marker in PUBLISH_SUBMIT_MARKERS)
        if submission_state == "not_submitted" and not has_submit_marker:
            result["retryable"] = True
        else:
            result["retry_block_reason"] = (
                "平台可能已经收到发布请求。为避免重复发帖，不能自动重试；"
                "请先到平台后台核对，再选择人工处理。"
            )
        return result

    result["retry_block_reason"] = "该类型暂不支持安全原单重试。"
    return result


def _text(value, limit: int = 90, fallback: str = "未命名任务") -> str:
    text = " ".join(str(value or "").split())
    return (text[:limit] + ("…" if len(text) > limit else "")) if text else fallback


def _status(raw: str) -> tuple[str, str]:
    raw = (raw or "queued").lower()
    if raw in ACTIVE:
        group = "active"
    elif raw in WAITING:
        group = "waiting"
    elif raw in DONE:
        group = "done"
    elif raw in FAILED:
        group = "failed"
    elif raw in CANCELLED:
        group = "cancelled"
    else:
        group = "active"
    return group, STATUS_LABELS.get(raw, raw)


def _item(kind: str, row: dict, *, title: str, assignee: str, source_label: str,
          target_route: str, source_route: str = "", raw_status: str | None = None,
          module: str = "", subkind: str = "", source_detail: str = "",
          target_exact: bool = True, retry: dict | None = None) -> dict:
    group, status_label = _status(raw_status or row.get("status") or "queued")
    emoji, kind_label = KIND_META[kind]
    item = {
        "key": f"{kind}:{row['id']}", "record_id": row["id"], "kind": kind,
        "kind_label": kind_label, "emoji": emoji, "subkind": subkind,
        "module": module, "title": _text(title), "assignee": assignee,
        "status": raw_status or row.get("status") or "queued",
        "status_group": group, "status_label": status_label,
        "source_label": source_label, "source_detail": _text(source_detail, 120, "") if source_detail else "",
        "target_route": target_route, "source_route": source_route,
        "target_exact": bool(target_exact),
        "created_at": row.get("created_at") or 0,
        "updated_at": row.get("updated_at") or row.get("created_at") or 0,
    }
    item.update(retry or retry_meta(kind, row))
    return item


def _employee(idx: int) -> tuple[dict, str]:
    expert = departments.get(idx)
    if expert:
        return expert, expert.get("dept_key") or ""
    station = registry.BY_IDX.get(idx) or {}
    return station, "content"


def _json_array(values) -> str:
    """把内部 ID/枚举集合压成单个 SQLite JSON 参数，避开变量数量上限。"""
    return json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))


def _status_group_sql(column: str) -> str:
    """生成与 ``_status`` 完全相同的 SQL 状态分组表达式。"""
    def quoted(values):
        return ",".join("'" + value.replace("'", "''") + "'"
                        for value in sorted(values))

    normalized = f"LOWER(COALESCE({column},'queued'))"
    return (
        "CASE "
        f"WHEN {normalized} IN ({quoted(ACTIVE)}) THEN 'active' "
        f"WHEN {normalized} IN ({quoted(WAITING)}) THEN 'waiting' "
        f"WHEN {normalized} IN ({quoted(DONE)}) THEN 'done' "
        f"WHEN {normalized} IN ({quoted(FAILED)}) THEN 'failed' "
        f"WHEN {normalized} IN ({quoted(CANCELLED)}) THEN 'cancelled' "
        "ELSE 'active' END"
    )


def _safe_meeting_members_sql(alias: str) -> str:
    """无论旧数据是否损坏，都只把合法 JSON 数组交给 ``json_each``。"""
    value = f"{alias}.emp_idxs_json"
    return (
        "CASE WHEN json_valid(" + value + ") THEN "
        "CASE WHEN json_type(" + value + ")='array' "
        "THEN " + value + " ELSE '[]' END "
        "ELSE '[]' END"
    )


def _meeting_visibility_sql(alias: str) -> str:
    """会议必须有成员，且每位成员都是当前账号可识别、可见的数字员工。

    参数是一个 JSON 整数数组。未知成员、字符串 ID、损坏 JSON 和空数组都
    fail closed，避免仅凭默认板块猜测权限。
    """
    members = _safe_meeting_members_sql(alias)
    return (
        f"EXISTS (SELECT 1 FROM json_each({members}) member) "
        f"AND NOT EXISTS ("
        f"SELECT 1 FROM json_each({members}) member "
        "WHERE member.type!='integer' OR CAST(member.value AS INTEGER) "
        "NOT IN (SELECT CAST(value AS INTEGER) FROM json_each(?))"
        ")"
    )


def _employee_scope(tenant_id: int, allowed_modules: set[str]
                    ) -> tuple[set[int], set[int]]:
    """只读取去重后的员工编号，计算任务与会议的板块可见范围。

    任务保留历史兼容语义：未知编号仍归内容板块并显示为“员工 N”。会议更
    保守，只有能解析出姓名的真实员工才能成为可见成员。
    """
    rows = db.q(
        "SELECT DISTINCT emp_idx FROM task "
        "WHERE tenant_id=? AND deleted_at IS NULL "
        "UNION "
        "SELECT DISTINCT CAST(member.value AS INTEGER) AS emp_idx "
        "FROM meeting m "
        f"JOIN json_each({_safe_meeting_members_sql('m')}) member "
        "WHERE m.tenant_id=? AND member.type='integer'",
        (tenant_id, tenant_id),
    )
    task_idxs: set[int] = set()
    meeting_idxs: set[int] = set()
    for row in rows:
        try:
            idx = int(row["emp_idx"])
        except (KeyError, TypeError, ValueError):
            continue
        employee, module = _employee(idx)
        if module not in allowed_modules:
            continue
        task_idxs.add(idx)
        if employee.get("name"):
            meeting_idxs.add(idx)
    return task_idxs, meeting_idxs


def _like_value(value: str, max_length: int = 100) -> str:
    """与 main._like_value 同款:转义 LIKE 元字符,防通配符注入。"""
    raw = (value or "").strip()[:max_length]
    return ("%" + raw.replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_") + "%")


def _visible_header_query(tenant_id: int, allowed_modules: set[str],
                          task_idxs: set[int],
                          meeting_idxs: set[int],
                          q: str = "") -> tuple[str, list]:
    """构造只含排序/计数列的跨业务表 UNION，不触碰正文和结果 JSON。

    q 非空时每臂按各自的"标题字段"做参数化 LIKE:计数与分页天然同源,
    搜索结果的 counts 就是命中总数,不再有"假全局搜索"。
    """
    branches: list[str] = []
    args: list = []
    like = _like_value(q) if (q or "").strip() else None

    def add(kind: str, table: str, raw_status: str, where: str,
            values: tuple | list, search_expr: str = None):
        clause = where
        vals = list(values)
        if like and search_expr:
            clause += f" AND {search_expr} LIKE ? ESCAPE '\\'"
            vals.append(like)
        branches.append(
            f"SELECT '{kind}' AS kind,id AS record_id,"
            f"{raw_status} AS raw_status,"
            "COALESCE(created_at,0) AS created_at,"
            "COALESCE(NULLIF(updated_at,0),NULLIF(created_at,0),0) "
            f"AS updated_at FROM {table} WHERE {clause}"
        )
        args.extend(vals)

    if task_idxs:
        add(
            "expert", "task", "status",
            "tenant_id=? AND deleted_at IS NULL AND emp_idx IN "
            "(SELECT CAST(value AS INTEGER) FROM json_each(?))",
            (tenant_id, _json_array(task_idxs)),
            search_expr="COALESCE(json_extract(brief_json,'$.direction'),'')",
        )

    if "content" in allowed_modules:
        add("content", "job", "status",
            "tenant_id=? AND deleted_at IS NULL", (tenant_id,),
            search_expr="COALESCE(json_extract(brief_json,'$.direction'),'')")
        add("video", "tv_job", "status", "tenant_id=?", (tenant_id,),
            search_expr="COALESCE(json_extract(params_json,'$.title'),"
                        "params_json,'')")
        add("tool", "tool_job", "status", "tenant_id=?", (tenant_id,),
            search_expr="COALESCE(params_json,'')")
        add("publish", "pub_task", "status", "tenant_id=?", (tenant_id,),
            search_expr="COALESCE(json_extract(payload_json,'$.title'),'')")
        add("wechat", "wechat_draft_delivery", "status",
            "tenant_id=?", (tenant_id,),
            search_expr="COALESCE(title,'')")

    if meeting_idxs:
        add(
            "meeting", "meeting",
            "COALESCE(NULLIF(phase,''),status,'queued')",
            "tenant_id=? AND " + _meeting_visibility_sql("meeting"),
            (tenant_id, _json_array(meeting_idxs)),
            search_expr="COALESCE(question,'')",
        )

    if "avatar" in allowed_modules:
        add("avatar", "avatar_job", "status",
            "tenant_id=? AND deleted_at IS NULL", (tenant_id,),
            search_expr="COALESCE(params_json,'')")

    if not branches:
        return "", []

    union = " UNION ALL ".join(branches)
    classified = (
        "SELECT kind,record_id,raw_status AS status,created_at,updated_at,"
        + _status_group_sql("raw_status")
        + " AS status_group FROM (" + union + ") visible_task_headers"
    )
    return classified, args


def _rows_for_ids(select_sql: str, table: str, tenant_id: int,
                  ids: list[int], *, extra_where: str = "") -> dict[int, dict]:
    """按当前页 ID 批量读取一种业务详情；一个 JSON 参数支持 5000 条页面。"""
    if not ids:
        return {}
    where = (
        "tenant_id=? AND id IN "
        "(SELECT CAST(value AS INTEGER) FROM json_each(?))"
    )
    if extra_where:
        where += " AND " + extra_where
    rows = db.q(
        f"SELECT {select_sql} FROM {table} WHERE {where}",
        (tenant_id, _json_array(set(ids))),
    )
    return {int(row["id"]): row for row in rows}


def list_items(tenant_id: int, allowed_modules: set[str], limit: int = 300,
               offset: int = 0, q: str = "") -> dict:
    """返回当前租户可见的统一任务流。

    counts 基于全部匹配记录；items 是稳定排序后的一个 offset 分页。调用方必须使用
    next_offset 继续取下一页，不能用放大 limit 的方式反复重取第一页。
    """
    allowed_modules = set(allowed_modules or ())
    limit = max(1, min(int(limit or 300), 5000))
    offset = max(0, int(offset or 0))
    counts = {"all": 0, "active": 0, "waiting": 0, "done": 0,
              "failed": 0, "cancelled": 0}
    kind_counts: dict[str, int] = {}

    task_idxs, meeting_idxs = _employee_scope(
        tenant_id, allowed_modules
    )
    headers_sql, header_args = _visible_header_query(
        tenant_id, allowed_modules, task_idxs, meeting_idxs, q=q
    )
    if not headers_sql:
        counts["open"] = 0
        return {
            "items": [], "counts": counts, "kind_counts": {},
            "truncated": False, "has_more": False, "limit": limit,
            "offset": offset, "next_offset": None,
        }

    grouped = db.q(
        "SELECT kind,status_group,COUNT(*) AS n FROM ("
        + headers_sql + ") counted_headers "
        "GROUP BY kind,status_group",
        tuple(header_args),
    )
    for row in grouped:
        n = int(row.get("n") or 0)
        group = str(row.get("status_group") or "active")
        kind = str(row.get("kind") or "")
        counts["all"] += n
        counts[group] = counts.get(group, 0) + n
        kind_counts[kind] = kind_counts.get(kind, 0) + n
    counts["open"] = counts["active"] + counts["waiting"]

    page_headers = db.q(
        "SELECT kind,record_id,status,status_group,created_at,updated_at FROM ("
        + headers_sql + ") paged_headers "
        "ORDER BY CASE status_group WHEN 'waiting' THEN 2 "
        "WHEN 'active' THEN 1 ELSE 0 END DESC,"
        "updated_at DESC,created_at DESC,kind DESC,record_id DESC "
        "LIMIT ? OFFSET ?",
        tuple(header_args) + (limit, offset),
    )
    ids_by_kind: dict[str, list[int]] = {}
    for header in page_headers:
        ids_by_kind.setdefault(str(header["kind"]), []).append(
            int(header["record_id"])
        )

    details: dict[str, dict[int, dict]] = {}
    details["expert"] = _rows_for_ids(
        "id,emp_idx,brief_json,status,source_meeting_id,source_task_id,"
        "billing_status,retry_count,created_by,created_at,updated_at",
        "task", tenant_id, ids_by_kind.get("expert", []),
        extra_where="deleted_at IS NULL",
    )
    details["content"] = _rows_for_ids(
        "id,brief_json,status,billing_status,retry_count,current_idx,"
        "source_schedule_id,created_by,created_at,updated_at",
        "job", tenant_id, ids_by_kind.get("content", []),
        extra_where="deleted_at IS NULL",
    )
    details["meeting"] = _rows_for_ids(
        "id,question,phase,status,billing_status,retry_count,emp_idxs_json,"
        "execution_task_ids_json,created_at,updated_at",
        "meeting", tenant_id, ids_by_kind.get("meeting", []),
    )
    details["avatar"] = _rows_for_ids(
        "id,params_json,status,billing_status,retry_count,created_at,updated_at",
        "avatar_job", tenant_id, ids_by_kind.get("avatar", []),
        extra_where="deleted_at IS NULL",
    )
    details["video"] = _rows_for_ids(
        "id,job_id,params_json,status,billing_status,retry_count,"
        "created_at,updated_at",
        "tv_job", tenant_id, ids_by_kind.get("video", []),
    )
    details["tool"] = _rows_for_ids(
        "id,kind,params_json,status,billing_status,retry_count,"
        "created_at,updated_at",
        "tool_job", tenant_id, ids_by_kind.get("tool", []),
    )
    details["publish"] = _rows_for_ids(
        "id,platform,payload_json,status,retry_count,submission_state,"
        "log,created_at,updated_at",
        "pub_task", tenant_id, ids_by_kind.get("publish", []),
    )
    details["wechat"] = _rows_for_ids(
        "id,job_id,title,status,billing_status,created_at,updated_at",
        "wechat_draft_delivery", tenant_id, ids_by_kind.get("wechat", []),
    )

    parsed: dict[tuple[str, int], object] = {}
    for kind, column, default in (
            ("expert", "brief_json", {}),
            ("content", "brief_json", {}),
            ("meeting", "emp_idxs_json", []),
            ("avatar", "params_json", {}),
            ("video", "params_json", {}),
            ("tool", "params_json", {}),
            ("publish", "payload_json", {})):
        for row in details[kind].values():
            parsed[(kind, row["id"])] = db.jloads(row.get(column), default)

    # 页内来源/重试所需的辅助状态，也全部限制在本页关联 ID。
    expert_rows = list(details["expert"].values())
    source_meeting_ids = {
        int(row["source_meeting_id"]) for row in expert_rows
        if row.get("source_meeting_id")
    }
    visible_source_meetings: dict[int, dict] = {}
    if source_meeting_ids and meeting_idxs:
        rows = db.q(
            "SELECT id,question FROM meeting WHERE tenant_id=? "
            "AND id IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) "
            "AND " + _meeting_visibility_sql("meeting"),
            (
                tenant_id,
                _json_array(source_meeting_ids),
                _json_array(meeting_idxs),
            ),
        )
        visible_source_meetings = {int(row["id"]): row for row in rows}

    parent_task_ids = {
        int(row["source_task_id"]) for row in expert_rows
        if row.get("source_task_id")
    }
    visible_parent_task_ids: set[int] = set()
    if parent_task_ids and task_idxs:
        visible_parent_task_ids = {
            int(row["id"]) for row in db.q(
                "SELECT id FROM task WHERE tenant_id=? AND deleted_at IS NULL "
                "AND id IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) "
                "AND emp_idx IN "
                "(SELECT CAST(value AS INTEGER) FROM json_each(?))",
                (
                    tenant_id,
                    _json_array(parent_task_ids),
                    _json_array(task_idxs),
                ),
            )
        }

    content_rows = list(details["content"].values())
    content_ids = {int(row["id"]) for row in content_rows}
    usable_job_ids: set[int] = set()
    active_wechat_job_ids: set[int] = set()
    if content_ids:
        ids_json = _json_array(content_ids)
        usable_job_ids = {
            int(row["job_id"]) for row in db.q(
                "SELECT DISTINCT sr.job_id FROM station_run sr "
                "JOIN job j ON j.id=sr.job_id "
                "WHERE j.tenant_id=? AND j.deleted_at IS NULL "
                "AND sr.job_id IN "
                "(SELECT CAST(value AS INTEGER) FROM json_each(?)) "
                "AND sr.station_idx IN (3,4) "
                "AND sr.status IN ('done','awaiting_review') "
                "AND sr.output_json IS NOT NULL AND length(sr.output_json)>2",
                (tenant_id, ids_json),
            )
        }
        active_wechat_job_ids = {
            int(row["job_id"]) for row in db.q(
                "SELECT DISTINCT job_id FROM wechat_draft_delivery "
                "WHERE tenant_id=? AND job_id IN "
                "(SELECT CAST(value AS INTEGER) FROM json_each(?)) "
                "AND status IN "
                "('pending_charge','processing','submitting','submitted')",
                (tenant_id, ids_json),
            )
        }

    schedule_ids = {
        int(row["source_schedule_id"]) for row in content_rows
        if row.get("source_schedule_id")
    }
    schedules: dict[int, dict] = {}
    if schedule_ids:
        schedules = {
            int(row["id"]): row for row in db.q(
                "SELECT id,name FROM schedule WHERE tenant_id=? "
                "AND id IN "
                "(SELECT CAST(value AS INTEGER) FROM json_each(?))",
                (tenant_id, _json_array(schedule_ids)),
            )
        }

    linked_job_ids = {
        int(row["job_id"]) for row in details["video"].values()
        if row.get("job_id")
    }
    for row in details["publish"].values():
        linked = (parsed.get(("publish", row["id"])) or {}).get("job_id")
        if isinstance(linked, int) and not isinstance(linked, bool):
            linked_job_ids.add(linked)
    linked_job_ids.update(
        int(row["job_id"]) for row in details["wechat"].values()
        if row.get("job_id")
    )
    existing_job_ids: set[int] = set()
    if linked_job_ids:
        existing_job_ids = {
            int(row["id"]) for row in db.q(
                "SELECT id FROM job WHERE tenant_id=? AND deleted_at IS NULL "
                "AND id IN "
                "(SELECT CAST(value AS INTEGER) FROM json_each(?))",
                (tenant_id, _json_array(linked_job_ids)),
            )
        }

    tool_kinds = {
        str(row["kind"]) for row in details["tool"].values()
        if row.get("kind")
    }
    active_tool_kinds: set[str] = set()
    if tool_kinds:
        active_tool_kinds = {
            str(row["kind"]) for row in db.q(
                "SELECT DISTINCT kind FROM tool_job WHERE tenant_id=? "
                "AND kind IN (SELECT value FROM json_each(?)) "
                "AND status IN ('pending_charge','running')",
                (tenant_id, _json_array(tool_kinds)),
            )
        }

    selected_meeting_ids = set(details["meeting"])
    derived_meeting_ids: set[int] = set()
    if selected_meeting_ids:
        derived_meeting_ids = {
            int(row["source_meeting_id"]) for row in db.q(
                "SELECT DISTINCT source_meeting_id FROM task "
                "WHERE tenant_id=? AND deleted_at IS NULL "
                "AND source_meeting_id IN "
                "(SELECT CAST(value AS INTEGER) FROM json_each(?))",
                (tenant_id, _json_array(selected_meeting_ids)),
            )
        }

    built: dict[tuple[str, int], dict] = {}
    for row in expert_rows:
        employee, module = _employee(row["emp_idx"])
        brief = parsed.get(("expert", row["id"])) or {}
        mid = row.get("source_meeting_id")
        parent = row.get("source_task_id")
        if mid:
            source = visible_source_meetings.get(int(mid))
            source_label = f"AI会议 #{mid}" if source else "原会议已删除"
            source_route = f"#/meetings/{mid}" if source else ""
            source_detail = (source or {}).get("question", "")
        elif parent:
            exists = int(parent) in visible_parent_task_ids
            source_label = (
                f"任务 #{parent} 的重做"
                if exists else "原任务已删除 · 本条为重做版本"
            )
            source_route = f"#/tasks/{parent}" if exists else ""
            source_detail = "根据上一版反馈重新生成"
        else:
            source_label = "员工面板·直接派活"
            source_route = ""
            source_detail = ""
        built[("expert", row["id"])] = _item(
            "expert", row, title=brief.get("direction"),
            assignee=employee.get("name") or f"员工 {row['emp_idx']}",
            source_label=source_label, source_detail=source_detail,
            source_route=source_route, target_route=f"#/tasks/{row['id']}",
            module=module,
        )

    for row in content_rows:
        brief = parsed.get(("content", row["id"])) or {}
        sid = row.get("source_schedule_id")
        station = registry.BY_IDX.get(row.get("current_idx") or 0) or {}
        if sid:
            schedule = schedules.get(int(sid))
            source_label = (
                f"定时任务·{schedule['name']}"
                if schedule else "原定时任务已删除"
            )
            source_route = f"#/schedules/{sid}" if schedule else ""
        else:
            source_label, source_route = "内容生产部·下达任务", "#/new"
        content_retry = retry_meta(
            "content", row, usable_delivery=row["id"] in usable_job_ids
        )
        if (
            content_retry["retryable"]
            and row["id"] in active_wechat_job_ids
        ):
            content_retry["retryable"] = False
            content_retry["retry_block_reason"] = (
                "该工单的公众号草稿仍在投递或对账，收口前不能重跑内容。"
            )
        built[("content", row["id"])] = _item(
            "content", row, title=brief.get("direction"),
            assignee=f"内容生产部 · {station.get('name') or '流水线'}",
            source_label=source_label, source_route=source_route,
            target_route=f"#/job/{row['id']}", module="content",
            retry=content_retry,
        )

    for row in details["meeting"].values():
        names = []
        for idx in parsed.get(("meeting", row["id"])) or []:
            employee, _module = _employee(idx)
            if employee.get("name"):
                names.append(employee["name"])
        meeting_retry = retry_meta("meeting", row)
        execution_ids = db.jloads(
            row.get("execution_task_ids_json"), []
        )
        if (
            meeting_retry["retryable"]
            and (
                row["id"] in derived_meeting_ids
                or bool(execution_ids)
            )
        ):
            meeting_retry["retryable"] = False
            meeting_retry["retry_block_reason"] = (
                "会议已经生成执行任务，不能重开整场会议；"
                "请重试具体失败任务。"
            )
        built[("meeting", row["id"])] = _item(
            "meeting", row, title=row.get("question"),
            assignee=" · ".join(names[:4]) or "AI会议成员",
            source_label="AI会议室",
            target_route=f"#/meetings/{row['id']}",
            raw_status=row.get("phase") or row.get("status"),
            module="meeting", retry=meeting_retry,
        )

    for row in details["avatar"].values():
        params = parsed.get(("avatar", row["id"])) or {}
        title = params.get("prompt") or params.get("script") or "数字人视频"
        built[("avatar", row["id"])] = _item(
            "avatar", row, title=title, assignee="数字人摄影棚",
            source_label="数字人摄影棚·创建视频",
            target_route=f"#/tasks/avatar:{row['id']}", module="avatar",
        )

    for row in details["video"].values():
        params = parsed.get(("video", row["id"])) or {}
        linked = row.get("job_id")
        linked_exists = int(linked) in existing_job_ids if linked else False
        built[("video", row["id"])] = _item(
            "video", row,
            title=params.get("title") or params.get("script")
            or params.get("topic"),
            assignee="视频工厂",
            source_label=(
                f"内容工单 #{linked}" if linked_exists
                else "原内容工单已删除" if linked else "工具箱·图文成片"
            ),
            source_route=(
                f"#/job/{linked}" if linked_exists
                else "#/tools" if not linked else ""
            ),
            target_route=f"#/tasks/video:{row['id']}",
            module="content", subkind=params.get("mode") or "images",
        )

    for row in details["tool"].values():
        params = parsed.get(("tool", row["id"])) or {}
        name = TOOL_NAMES.get(
            row.get("kind"), row.get("kind") or "营销工具"
        )
        detail = (
            params.get("industry") or params.get("city")
            or params.get("ym") or ""
        )
        tool_retry = retry_meta("tool", row)
        if tool_retry["retryable"] and row.get("kind") in active_tool_kinds:
            tool_retry["retryable"] = False
            tool_retry["retry_block_reason"] = (
                "同一工具已有任务正在运行，等它完成后再重试。"
            )
        built[("tool", row["id"])] = _item(
            "tool", row,
            title=f"{name}{(' · ' + str(detail)) if detail else ''}",
            assignee=name, source_label=f"营销工具箱·{name}",
            target_route=f"#/tasks/tool:{row['id']}", module="content",
            subkind=row.get("kind") or "", retry=tool_retry,
        )

    for row in details["publish"].values():
        payload = parsed.get(("publish", row["id"])) or {}
        linked = payload.get("job_id")
        linked_exists = (
            isinstance(linked, int)
            and not isinstance(linked, bool)
            and linked in existing_job_ids
        )
        built[("publish", row["id"])] = _item(
            "publish", row,
            title=payload.get("title")
            or f"发布到 {row.get('platform') or '平台'}",
            assignee=row.get("platform") or "矩阵发布",
            source_label=(
                f"内容工单 #{linked}" if linked_exists
                else "原内容工单已删除" if linked else "发布渠道·矩阵发布"
            ),
            source_route=(
                f"#/job/{linked}" if linked_exists
                else "#/channels" if not linked else ""
            ),
            target_route=f"#/tasks/publish:{row['id']}", module="content",
            subkind=row.get("platform") or "",
        )

    for row in details["wechat"].values():
        linked = row.get("job_id")
        linked_exists = int(linked) in existing_job_ids if linked else False
        job_route = f"#/job/{linked}" if linked_exists else ""
        built[("wechat", row["id"])] = _item(
            "wechat", row,
            title=row.get("title") or "投递到公众号草稿箱",
            assignee="公众号草稿箱",
            source_label=(
                f"内容工单 #{linked}" if linked_exists
                else "原内容工单已删除"
            ),
            source_route=job_route, target_route=job_route or "#/channels",
            target_exact=False, module="content", subkind="wechat",
        )

    items = [
        built[(str(header["kind"]), int(header["record_id"]))]
        for header in page_headers
        if (str(header["kind"]), int(header["record_id"])) in built
    ]
    # 协作可见:批量把发起人 id 换成用户名(单查询,严格限本租户)
    creator_ids = set()
    for kind in ("expert", "content"):
        for row in details.get(kind, {}).values():
            if row.get("created_by"):
                creator_ids.add(int(row["created_by"]))
    names = {}
    if creator_ids:
        names = {
            int(u["id"]): u["username"]
            for u in db.q(
                "SELECT id,username FROM users WHERE tenant_id=? AND id IN "
                "(SELECT CAST(value AS INTEGER) FROM json_each(?))",
                (tenant_id, _json_array(creator_ids)),
            )
        }
    for item in items:
        row = details.get(item["kind"], {}).get(item["record_id"]) or {}
        creator = names.get(int(row.get("created_by") or 0))
        if creator:
            item["creator"] = creator
    next_offset = offset + len(page_headers)
    has_more = next_offset < counts["all"]
    return {
        "items": items,
        "counts": counts,
        "kind_counts": kind_counts,
        "truncated": has_more,
        "has_more": has_more,
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset if has_more else None,
    }
