"""全局任务中心：把分散在各业务表里的真实任务汇总成一张老板总账。

这里只做只读聚合，不复制任务、不维护第二套状态。每一条记录都保留原业务表 ID，
前端据此跳回专家任务、内容工单、会议室、摄影棚、工具箱或发布渠道。
"""
from __future__ import annotations

import json

from . import db, departments, employeeidentity, employees, inspection
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
STATUS_FILTERS = {
    "all", "open", "active", "waiting", "done", "failed", "cancelled",
}
KIND_FILTERS = {"all", *KIND_META}

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


def _json_array(values) -> str:
    """把内部 ID/枚举集合压成单个 SQLite JSON 参数，避开变量数量上限。"""
    rows = list(values)
    rows.sort(key=lambda value: json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ))
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


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


def _safe_meeting_snapshots_sql(alias: str) -> str:
    """Only expose a persisted meeting roster when it is a valid JSON array."""
    value = f"{alias}.member_snapshot_json"
    return (
        "CASE WHEN json_valid(" + value + ") THEN "
        "CASE WHEN json_type(" + value + ")='array' "
        "THEN " + value + " ELSE '[]' END "
        "ELSE '[]' END"
    )


def _task_identity_visibility_sql(alias: str) -> str:
    """Match a task against one exact catalog snapshot JSON parameter."""
    allowed = "allowed.value"
    exact = (
        f"{alias}.employee_key=json_extract({allowed},'$.key') AND "
        f"{alias}.employee_catalog_version="
        f"json_extract({allowed},'$.catalog_version') AND "
        f"{alias}.employee_name_snapshot=json_extract({allowed},'$.name') AND "
        f"{alias}.employee_dept_key=json_extract({allowed},'$.dept_key') AND "
        f"{alias}.employee_spec_sha256="
        f"json_extract({allowed},'$.spec_sha256')"
    )
    core_legacy = (
        f"COALESCE(json_extract({allowed},'$.core_legacy'),0)=1 AND "
        f"{alias}.employee_key='legacy.idx.'||CAST({alias}.emp_idx AS TEXT) AND "
        f"{alias}.employee_catalog_version='legacy-unknown' AND "
        f"{alias}.employee_dept_key='content' AND "
        f"{alias}.employee_name_snapshot IN (json_extract({allowed},'$.name'),"
        f"'历史员工#'||CAST({alias}.emp_idx AS TEXT)) AND "
        f"({alias}.employee_spec_sha256='legacy-unknown' OR ("
        f"LENGTH({alias}.employee_spec_sha256)=64 AND "
        f"{alias}.employee_spec_sha256 NOT GLOB '*[^0-9a-f]*'))"
    )
    return (
        "EXISTS (SELECT 1 FROM json_each(?) allowed WHERE "
        f"CAST(json_extract({allowed},'$.idx') AS INTEGER)={alias}.emp_idx "
        f"AND (({exact}) OR ({core_legacy})))"
    )


def _meeting_visibility_sql(alias: str) -> str:
    """Authorize meetings exclusively from their persisted roster snapshot.

    The parameter is a JSON array of exact active and retained V1 identities.
    Missing, malformed or unknown snapshots fail closed.  The only wildcard is
    the deliberately narrow pre-schema53 built-in content compatibility shape.
    """
    members = _safe_meeting_members_sql(alias)
    snapshots = _safe_meeting_snapshots_sql(alias)
    allowed = "allowed.value"
    frozen_value = "frozen.value"
    exact = (
        f"json_extract({frozen_value},'$.key')="
        f"json_extract({allowed},'$.key') AND "
        f"json_extract({frozen_value},'$.name')="
        f"json_extract({allowed},'$.name') AND "
        f"json_extract({frozen_value},'$.dept_key')="
        f"json_extract({allowed},'$.dept_key') AND "
        f"json_extract({frozen_value},'$.catalog_version')="
        f"json_extract({allowed},'$.catalog_version') AND "
        f"json_extract({frozen_value},'$.spec_sha256')="
        f"json_extract({allowed},'$.spec_sha256')"
    )
    core_legacy = (
        f"COALESCE(json_extract({allowed},'$.core_legacy'),0)=1 AND "
        f"json_extract({frozen_value},'$.key')='legacy.idx.'||"
        f"CAST(json_extract({frozen_value},'$.idx') AS TEXT) AND "
        f"json_extract({frozen_value},'$.catalog_version')='legacy-unknown' AND "
        f"json_extract({frozen_value},'$.dept_key')='content' AND "
        f"json_extract({frozen_value},'$.name') IN ("
        f"json_extract({allowed},'$.name'),'历史员工#'||"
        f"CAST(json_extract({frozen_value},'$.idx') AS TEXT)) AND "
        f"(json_extract({frozen_value},'$.spec_sha256')='legacy-unknown' OR ("
        f"LENGTH(json_extract({frozen_value},'$.spec_sha256'))=64 AND "
        f"json_extract({frozen_value},'$.spec_sha256') "
        f"NOT GLOB '*[^0-9a-f]*'))"
    )
    return (
        f"EXISTS (SELECT 1 FROM json_each({snapshots}) frozen) "
        f"AND json_array_length({members})=json_array_length({snapshots}) "
        f"AND json_array_length({members})=("
        f"SELECT COUNT(DISTINCT CAST(value AS INTEGER)) FROM json_each({members})"
        f") "
        f"AND NOT EXISTS (SELECT 1 FROM json_each({members}) member "
        "WHERE member.type!='integer') "
        f"AND NOT EXISTS ("
        f"SELECT 1 FROM json_each({snapshots}) frozen "
        "WHERE frozen.type!='object' "
        "OR json_type(frozen.value,'$.idx')!='integer' "
        f"OR CAST(json_extract(frozen.value,'$.idx') AS INTEGER)!="
        f"CAST(json_extract({members},'$['||frozen.key||']') AS INTEGER) "
        "OR json_type(frozen.value,'$.key')!='text' "
        "OR json_type(frozen.value,'$.name')!='text' "
        "OR json_type(frozen.value,'$.catalog_version')!='text' "
        "OR json_type(frozen.value,'$.spec_sha256')!='text' "
        "OR json_type(frozen.value,'$.dept_key')!='text' "
        "OR COALESCE(json_extract(frozen.value,'$.key'),'')='' "
        "OR json_extract(frozen.value,'$.key')!=TRIM(json_extract(frozen.value,'$.key')) "
        "OR COALESCE(json_extract(frozen.value,'$.name'),'')='' "
        "OR json_extract(frozen.value,'$.name')!=TRIM(json_extract(frozen.value,'$.name')) "
        "OR COALESCE(json_extract(frozen.value,'$.catalog_version'),'')='' "
        "OR json_extract(frozen.value,'$.catalog_version')!="
        "TRIM(json_extract(frozen.value,'$.catalog_version')) "
        "OR COALESCE(json_extract(frozen.value,'$.spec_sha256'),'')='' "
        "OR json_extract(frozen.value,'$.spec_sha256')!="
        "TRIM(json_extract(frozen.value,'$.spec_sha256')) "
        "OR COALESCE(json_extract(frozen.value,'$.dept_key'),'')='' "
        "OR json_extract(frozen.value,'$.dept_key')!="
        "TRIM(json_extract(frozen.value,'$.dept_key')) "
        "OR NOT EXISTS (SELECT 1 FROM json_each(?) allowed WHERE "
        f"CAST(json_extract({allowed},'$.idx') AS INTEGER)="
        f"CAST(json_extract({frozen_value},'$.idx') AS INTEGER) "
        f"AND (({exact}) OR ({core_legacy})))"
        ")"
    )


def _like_value(value: str, max_length: int = 100) -> str:
    """与 main._like_value 同款:转义 LIKE 元字符,防通配符注入。"""
    raw = (value or "").strip()[:max_length]
    return ("%" + raw.replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_") + "%")


def _safe_json_search_sql(
    column: str,
    path: str | None = None,
    *,
    fallback_to_document: bool = False,
) -> str:
    """生成不会被损坏历史 JSON 拖垮的搜索表达式。

    SQLite 的 ``json_extract`` 遇到一条非法 JSON 会中止整条 UNION 查询。
    只有 ``json_valid`` 已证明合法时才解析字段；非法载荷退回原始文本，既
    保证任务中心可用，也让运维仍能用已知片段找到并处理坏记录。仅需全文
    搜索的 JSON 列完全不调用 JSON1，因此同样不受损坏载荷影响。
    """
    document = f"COALESCE(CAST({column} AS TEXT),'')"
    if not path:
        return document

    safe_path = path.replace("'", "''")
    missing_value = document if fallback_to_document else "''"
    extracted = (
        f"COALESCE(CAST(json_extract({column},'{safe_path}') AS TEXT),"
        f"{missing_value})"
    )
    return (
        f"(CASE WHEN json_valid({column}) THEN {extracted} "
        f"ELSE {document} END)"
    )


def _visible_header_query(tenant_id: int, allowed_modules: set[str],
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

    frozen_modules = {
        str(value).strip() for value in allowed_modules
        if str(value).strip() not in {"unknown", "__denied__"}
    }
    frozen_identities = employeeidentity.visible_catalog_snapshots(frozen_modules)
    if frozen_identities:
        add(
            "expert", "task", "status",
            "tenant_id=? AND deleted_at IS NULL AND "
            "(thread_id IS NULL OR id=(SELECT tt.current_task_id "
            "FROM task_thread tt WHERE tt.id=task.thread_id "
            "AND tt.tenant_id=task.tenant_id)) AND emp_idx<>? AND "
            + _task_identity_visibility_sql("task"),
            (
                tenant_id, inspection.EMPLOYEE_IDX,
                _json_array(frozen_identities),
            ),
            search_expr=_safe_json_search_sql(
                "brief_json", "$.direction"
            ),
        )

    inspection_modules = {
        str(value).strip() for value in allowed_modules
        if str(value).strip() not in {"", "unknown", "__denied__"}
    }
    if inspection_modules:
        add(
            "expert", "task", "status",
            "tenant_id=? AND deleted_at IS NULL AND emp_idx=? AND EXISTS("
            "SELECT 1 FROM inspection_visit iv WHERE iv.task_id=task.id "
            "AND iv.tenant_id=task.tenant_id AND iv.deleted_at IS NULL "
            "AND iv.industry_key IN (SELECT value FROM json_each(?)))",
            (
                tenant_id,
                inspection.EMPLOYEE_IDX,
                _json_array(inspection_modules),
            ),
            search_expr=_safe_json_search_sql(
                "brief_json", "$.direction"
            ),
        )

    if "content" in allowed_modules:
        add("content", "job", "status",
            "tenant_id=? AND deleted_at IS NULL", (tenant_id,),
            search_expr=_safe_json_search_sql(
                "brief_json", "$.direction"
            ))
        add("video", "tv_job", "status", "tenant_id=?", (tenant_id,),
            search_expr=_safe_json_search_sql(
                "params_json", "$.title", fallback_to_document=True
            ))
        # 工具单列表标题显示的是 TOOL_NAMES 中文名,搜索必须能按它命中,
        # 否则老板照着屏幕上的「今日必发」搜出 0 条
        tool_name_case = ("CASE kind " + " ".join(
            f"WHEN '{key}' THEN '{name}'" for key, name in TOOL_NAMES.items()
        ) + " ELSE kind END")
        add("tool", "tool_job", "status", "tenant_id=?", (tenant_id,),
            search_expr=(
                f"({tool_name_case})||"
                + _safe_json_search_sql("params_json")
            ))
        add("publish", "pub_task", "status", "tenant_id=?", (tenant_id,),
            search_expr=_safe_json_search_sql(
                "payload_json", "$.title"
            ))
        add("wechat", "wechat_draft_delivery", "status",
            "tenant_id=?", (tenant_id,),
            search_expr="COALESCE(title,'')")

    if frozen_identities:
        add(
            "meeting", "meeting",
            "COALESCE(NULLIF(phase,''),status,'queued')",
            "tenant_id=? AND " + _meeting_visibility_sql("meeting"),
            (tenant_id, _json_array(frozen_identities)),
            search_expr="COALESCE(question,'')",
        )

    if "avatar" in allowed_modules:
        add("avatar", "avatar_job", "status",
            "tenant_id=? AND deleted_at IS NULL", (tenant_id,),
            search_expr=_safe_json_search_sql("params_json"))

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


def list_items(
    tenant_id: int,
    allowed_modules: set[str],
    limit: int = 300,
    offset: int = 0,
    q: str = "",
    status: str = "all",
    kind: str = "all",
) -> dict:
    """返回当前租户可见的统一任务流。

    counts 基于全部匹配记录；items 是稳定排序后的一个 offset 分页。调用方必须使用
    next_offset 继续取下一页，不能用放大 limit 的方式反复重取第一页。
    """
    allowed_modules = set(allowed_modules or ())
    limit = max(1, min(int(limit or 300), 5000))
    offset = max(0, int(offset or 0))
    status = str(status or "all").strip().lower()
    kind = str(kind or "all").strip().lower()
    if status not in STATUS_FILTERS:
        raise ValueError("任务状态筛选无效")
    if kind not in KIND_FILTERS:
        raise ValueError("任务类型筛选无效")
    counts = {"all": 0, "active": 0, "waiting": 0, "done": 0,
              "failed": 0, "cancelled": 0}
    kind_counts: dict[str, int] = {}

    headers_sql, header_args = _visible_header_query(
        tenant_id, allowed_modules, q=q
    )
    if not headers_sql:
        counts["open"] = 0
        return {
            "items": [], "counts": counts, "kind_counts": {},
            "filtered_total": 0,
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
        row_kind = str(row.get("kind") or "")
        counts["all"] += n
        counts[group] = counts.get(group, 0) + n
        kind_counts[row_kind] = kind_counts.get(row_kind, 0) + n
    counts["open"] = counts["active"] + counts["waiting"]

    page_filters: list[str] = []
    page_filter_args: list[str] = []
    if status == "open":
        page_filters.append("status_group IN ('active','waiting')")
    elif status != "all":
        page_filters.append("status_group=?")
        page_filter_args.append(status)
    if kind != "all":
        page_filters.append("kind=?")
        page_filter_args.append(kind)
    page_where = (
        " WHERE " + " AND ".join(page_filters)
        if page_filters else ""
    )
    filtered_total = int((
        db.one(
            "SELECT COUNT(*) AS n FROM (" + headers_sql
            + ") filtered_headers" + page_where,
            tuple(header_args + page_filter_args),
        ) or {}
    ).get("n") or 0)

    page_headers = db.q(
        "SELECT kind,record_id,status,status_group,created_at,updated_at FROM ("
        + headers_sql + ") paged_headers"
        + page_where + " "
        "ORDER BY CASE status_group WHEN 'waiting' THEN 2 "
        "WHEN 'active' THEN 1 ELSE 0 END DESC,"
        "updated_at DESC,created_at DESC,kind DESC,record_id DESC "
        "LIMIT ? OFFSET ?",
        tuple(header_args + page_filter_args) + (limit, offset),
    )
    ids_by_kind: dict[str, list[int]] = {}
    for header in page_headers:
        ids_by_kind.setdefault(str(header["kind"]), []).append(
            int(header["record_id"])
        )

    details: dict[str, dict[int, dict]] = {}
    details["expert"] = _rows_for_ids(
        "id,emp_idx,brief_json,status,source_meeting_id,source_task_id,"
        "thread_id,revision_no,phase,billing_status,retry_count,created_by,"
        "employee_key,employee_catalog_version,employee_name_snapshot,"
        "employee_dept_key,employee_spec_sha256,"
        "employee_identity_ref,employee_config_revision,"
        "employee_config_sha256,person_snapshot,identity_scheme,bundle_sha256,"
        "created_at,updated_at",
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
        "member_snapshot_json,"
        "execution_task_ids_json,created_by,created_at,updated_at",
        "meeting", tenant_id, ids_by_kind.get("meeting", []),
    )
    details["avatar"] = _rows_for_ids(
        "id,params_json,status,billing_status,retry_count,created_by,created_at,updated_at",
        "avatar_job", tenant_id, ids_by_kind.get("avatar", []),
        extra_where="deleted_at IS NULL",
    )
    details["video"] = _rows_for_ids(
        "id,job_id,params_json,status,billing_status,retry_count,"
        "created_by,created_at,updated_at",
        "tv_job", tenant_id, ids_by_kind.get("video", []),
    )
    details["tool"] = _rows_for_ids(
        "id,kind,params_json,status,billing_status,retry_count,"
        "created_by,created_at,updated_at",
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
    expert_configs = employees.get_configs({
        int(row.get("emp_idx") or -1) for row in expert_rows
    })
    inspection_task_ids = {
        int(row["id"]) for row in expert_rows
        if int(row.get("emp_idx") or -1) == inspection.EMPLOYEE_IDX
    }
    inspection_visits_by_task: dict[int, dict] = {}
    if inspection_task_ids:
        permitted_inspection_modules = {
            str(value).strip() for value in allowed_modules
            if str(value).strip() not in {"", "unknown", "__denied__"}
        }
        inspection_visits_by_task = {
            int(row["task_id"]): {
                "id": int(row["id"]),
                "industry_key": str(row["industry_key"]),
            }
            for row in db.q(
                "SELECT id,task_id,industry_key FROM inspection_visit WHERE tenant_id=? "
                "AND deleted_at IS NULL AND industry_key IN "
                "(SELECT value FROM json_each(?)) AND task_id IN "
                "(SELECT CAST(value AS INTEGER) FROM json_each(?))",
                (
                    tenant_id,
                    _json_array(permitted_inspection_modules),
                    _json_array(inspection_task_ids),
                ),
            )
        }
    source_meeting_ids = {
        int(row["source_meeting_id"]) for row in expert_rows
        if row.get("source_meeting_id")
    }
    visible_source_meetings: dict[int, dict] = {}
    frozen_modules = {
        str(value).strip() for value in allowed_modules
        if str(value).strip() not in {"unknown", "__denied__"}
    }
    frozen_identities = employeeidentity.visible_catalog_snapshots(frozen_modules)
    if source_meeting_ids and frozen_identities:
        rows = db.q(
            "SELECT id,question FROM meeting WHERE tenant_id=? "
            "AND id IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) "
            "AND " + _meeting_visibility_sql("meeting"),
            (
                tenant_id,
                _json_array(source_meeting_ids),
                _json_array(frozen_identities),
            ),
        )
        visible_source_meetings = {int(row["id"]): row for row in rows}

    parent_task_ids = {
        int(row["source_task_id"]) for row in expert_rows
        if row.get("source_task_id")
    }
    visible_parent_task_ids: set[int] = set()
    if parent_task_ids and frozen_identities:
        visible_parent_task_ids = {
            int(row["id"]) for row in db.q(
                "SELECT id FROM task WHERE tenant_id=? AND deleted_at IS NULL "
                "AND id IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) "
                "AND " + _task_identity_visibility_sql("task"),
                (
                    tenant_id,
                    _json_array(parent_task_ids),
                    _json_array(frozen_identities),
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
        module = str(row.get("employee_dept_key") or "")
        brief = parsed.get(("expert", row["id"])) or {}
        mid = row.get("source_meeting_id")
        parent = row.get("source_task_id")
        revision_no = max(1, int(row.get("revision_no") or 1))
        inspection_visit = inspection_visits_by_task.get(int(row["id"]))
        inspection_visit_id = (
            int(inspection_visit["id"]) if inspection_visit else None
        )
        if inspection_visit:
            source_label = "巡店工作台 · 到店检查"
            source_route = (
                f"#/inspections/0/{inspection_visit['industry_key']}"
            )
            source_detail = "照片问题、整改计划与人工复核闭环"
        elif row.get("thread_id"):
            source_label = f"持续协作 · 第{revision_no}轮"
            source_route = ""
            source_detail = (
                "这是当前最新版本；历史版本可在任务详情中查看"
            )
        elif mid:
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
        item = _item(
            "expert", row, title=brief.get("direction"),
            assignee=(
                f"{str(row.get('person_snapshot') or '').strip()}·"
                f"{str(row.get('employee_name_snapshot') or '').strip()}"
            ).strip("·") or "历史岗位版本",
            source_label=source_label, source_detail=source_detail,
            source_route=source_route,
            target_route=(
                f"#/inspections/{inspection_visit_id}/"
                f"{inspection_visit['industry_key']}"
                if inspection_visit else f"#/tasks/{row['id']}"
            ),
            module=module,
            subkind="inspection" if inspection_visit_id else "",
        )
        item["thread_id"] = row.get("thread_id")
        item["revision_no"] = revision_no
        roster = employeeidentity.roster_metadata_from_task(row) or {
            "roster_status": "legacy", "can_assign": False,
        }
        enabled = bool(
            expert_configs.get(
                int(row.get("emp_idx") or -1), {"enabled": True}
            ).get("enabled", True)
        )
        item["roster_status"] = roster["roster_status"]
        item["can_assign"] = bool(roster["can_assign"] and enabled)
        built[("expert", row["id"])] = item

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
        snapshots = db.jloads(row.get("member_snapshot_json"), [])
        names = [
            str(item.get("name") or "").strip()
            for item in snapshots if isinstance(item, dict)
            and str(item.get("name") or "").strip()
        ] if isinstance(snapshots, list) else []
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
    for kind in ("expert", "content", "meeting", "avatar", "video", "tool"):
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
    has_more = next_offset < filtered_total
    return {
        "items": items,
        "counts": counts,
        "kind_counts": kind_counts,
        "filtered_total": filtered_total,
        "truncated": has_more,
        "has_more": has_more,
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset if has_more else None,
    }
