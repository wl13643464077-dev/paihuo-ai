"""手机协同小队的持久编排层。

每一棒仍通过现有 ``POST /api/tasks`` 对应的服务端创建函数派单、计费和
执行。本模块只保存小队快照、审批、依赖、幂等号与恢复状态，不另造任务执行器。
迁移必须先调用 ``teamrun_schema.install_schema``。
"""

import asyncio
import hashlib
import json
import re
import time
from typing import Awaitable, Callable

from . import billing, db


DEPTH_LENGTH = {
    "simple": "lite",
    "comprehensive": "std",
    "professional": "full",
}
DEPTH_STANDARD = {
    "simple": "简单档：先给结论与最多三步行动；缺失信息要标明，不展开未经核验的细节。",
    "comprehensive": "全面档：说明目标与假设、分工方法、可执行步骤、交付检查点和主要风险；重要事实标来源。",
    "professional": "专业档：区分已核验事实、假设与建议；交代方法推导、可追溯证据、替代方案、执行责任与验收标准，并明确合规及失败边界。不得把推测写成已证实结果。",
}
MODES = frozenset({"semi", "auto"})
CLAIM_SECONDS = 90
DISPATCH_ERROR = "派单失败，请检查账号点数或稍后重试；未成功创建的任务不会从小队中消失"


class TeamRunError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TeamRunNotFound(TeamRunError):
    pass


class TeamRunConflict(TeamRunError):
    pass


def _positive(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TeamRunError("invalid_identifier", f"{name}无效")
    return value


def _member_idx(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TeamRunError("invalid_member", "小队成员编号无效")
    return value


def _text(value, name: str, limit: int, *, required=False) -> str:
    if not isinstance(value, str):
        raise TeamRunError("invalid_text", f"{name}格式无效")
    result = value.strip()
    if required and not result:
        raise TeamRunError("invalid_text", f"{name}不能为空")
    if len(result) > limit:
        raise TeamRunError("invalid_text", f"{name}最多 {limit} 个字符")
    return result


def _normalized_team(team: dict) -> dict:
    if not isinstance(team, dict):
        raise TeamRunError("invalid_team", "小队资料无效，请重新匹配")
    raw_members = team.get("members")
    if not isinstance(raw_members, list) or not (2 <= len(raw_members) <= 8):
        raise TeamRunError("invalid_team", "小队应有 2～8 位成员")
    members = []
    seen = set()
    for position, raw in enumerate(raw_members):
        if not isinstance(raw, dict):
            raise TeamRunError("invalid_team", "小队成员资料无效")
        idx = _member_idx(raw.get("idx"))
        if idx in seen:
            raise TeamRunError("invalid_team", "同一数字员工不能在小队中重复")
        seen.add(idx)
        deps = raw.get("dependsOn") or []
        if not isinstance(deps, list) or len(deps) > 8:
            raise TeamRunError("invalid_team", "成员依赖资料无效")
        parsed_deps = []
        for dep in deps:
            dep = _member_idx(dep)
            if dep == idx or dep in parsed_deps:
                raise TeamRunError("invalid_team", "成员依赖不能重复或指向自己")
            parsed_deps.append(dep)
        members.append({
            "position": position,
            "idx": idx,
            "name": _text(raw.get("name") or "", "成员姓名", 80),
            "role": _text(raw.get("role") or "", "岗位", 80),
            "roleInTeam": _text(raw.get("roleInTeam") or "", "小队角色", 30),
            "task": _text(raw.get("task") or "", "分工", 400, required=True),
            "dependsOn": parsed_deps,
        })
    lead = next((m for m in members if m["roleInTeam"] == "队长"), members[0])
    for member in members:
        member["roleInTeam"] = (
            "队长" if member["idx"] == lead["idx"]
            else ("协同" if member["roleInTeam"] == "队长" else member["roleInTeam"] or "协同")
        )
        if member["idx"] == lead["idx"]:
            member["dependsOn"] = []
        elif lead["idx"] not in member["dependsOn"]:
            # 队长的前置拆解是全队共同前置条件，不允许某条自定义依赖绕过。
            member["dependsOn"] = [lead["idx"], *member["dependsOn"]]
        if any(dep not in seen for dep in member["dependsOn"]):
            raise TeamRunError("invalid_team", "成员依赖必须指向本小队成员")
    by_idx = {m["idx"]: m for m in members}
    active = set()
    done = set()

    def visit(idx):
        if idx in active:
            raise TeamRunError("invalid_team", "小队分工存在循环依赖，请重新匹配")
        if idx in done:
            return
        active.add(idx)
        for dep in by_idx[idx]["dependsOn"]:
            visit(dep)
        active.remove(idx)
        done.add(idx)

    for member in members:
        visit(member["idx"])
    return {
        "team_name": _text(team.get("teamName") or "经营协同小队", "小队名称", 80),
        "team_summary": _text(team.get("summary") or "", "小队说明", 500),
        "leader_emp_idx": lead["idx"],
        "members": members,
    }


def _run_row(run_id: int, tenant_id: int, connection=None):
    row = (connection or db.conn()).execute(
        "SELECT * FROM team_run WHERE id=? AND tenant_id=?",
        (run_id, tenant_id),
    ).fetchone()
    if row is None:
        raise TeamRunNotFound("run_not_found", "小队不存在或不属于当前企业")
    return dict(row)


def _member_row(run_id: int, tenant_id: int, member_id: int, connection=None):
    row = (connection or db.conn()).execute(
        "SELECT * FROM team_run_member WHERE id=? AND team_run_id=? AND tenant_id=?",
        (member_id, run_id, tenant_id),
    ).fetchone()
    if row is None:
        raise TeamRunNotFound("member_not_found", "小队成员不存在或不属于当前企业")
    return dict(row)


def create_run(
    tenant_id: int, actor_id: int, query: str, team: dict, *,
    mode: str, depth: str, request_key: str,
) -> dict:
    """固化一句话匹配快照。相同租户/幂等号/内容重复请求返回同一小队。"""
    tenant_id = _positive(tenant_id, "企业编号")
    actor_id = _positive(actor_id, "操作人编号")
    query = _text(query, "老板需求", 500, required=True)
    if mode not in MODES:
        raise TeamRunError("invalid_mode", "请选择逐个确认或全自动模式")
    if depth not in DEPTH_LENGTH:
        raise TeamRunError("invalid_depth", "请选择简单、全面或专业输出深度")
    if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{12,128}", request_key):
        raise TeamRunError("invalid_request_key", "小队请求编号无效，请重新发起")
    normalized = _normalized_team(team)
    payload = {"query": query, "team": normalized, "mode": mode, "depth": depth}
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    now = time.time()
    with db.atomic() as connection:
        existing = connection.execute(
            "SELECT id,actor_id,payload_sha256 FROM team_run "
            "WHERE tenant_id=? AND request_key=?",
            (tenant_id, request_key),
        ).fetchone()
        if existing:
            if existing["payload_sha256"] != fingerprint or existing["actor_id"] != actor_id:
                raise TeamRunConflict("request_key_reused", "这个小队请求编号已用于其他内容，请刷新后重试")
            run_id = int(existing["id"])
        else:
            run_id = db.insert("team_run", {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_key": request_key,
                "payload_sha256": fingerprint,
                "query": query,
                "team_name": normalized["team_name"],
                "team_summary": normalized["team_summary"],
                "mode": mode,
                "depth": depth,
                "leader_emp_idx": normalized["leader_emp_idx"],
                "status": "running" if mode == "auto" else "awaiting_approval",
                "created_at": now,
                "updated_at": now,
            })
            for member in normalized["members"]:
                db.insert("team_run_member", {
                    "team_run_id": run_id,
                    "tenant_id": tenant_id,
                    "position": member["position"],
                    "emp_idx": member["idx"],
                    "name": member["name"],
                    "role": member["role"],
                    "role_in_team": member["roleInTeam"],
                    "task_text": member["task"],
                    "depends_on_json": json.dumps(member["dependsOn"]),
                    "approved": 1 if mode == "auto" else 0,
                    "approved_at": now if mode == "auto" else None,
                    "created_at": now,
                    "updated_at": now,
                })
    return get_run(run_id, tenant_id)


def _latest_delivered_task(connection, task_id: int | None,
                           tenant_id: int) -> dict | None:
    """Resolve the newest real delivered revision of an anchored team task."""
    if not task_id:
        return None
    root = connection.execute(
        "SELECT id,thread_id,status,output_md,created_at,updated_at "
        "FROM task WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (task_id, tenant_id),
    ).fetchone()
    if not root:
        return None
    if root["thread_id"]:
        latest = connection.execute(
            "SELECT id,thread_id,status,output_md,created_at,updated_at "
            "FROM task WHERE thread_id=? AND tenant_id=? AND status='done' "
            "AND deleted_at IS NULL ORDER BY revision_no DESC,id DESC LIMIT 1",
            (root["thread_id"], tenant_id),
        ).fetchone()
        if latest:
            return dict(latest)
    return dict(root) if root["status"] == "done" else None


def _summary_is_stale(connection, run: dict, members: list[dict]) -> bool:
    if not run.get("summary_task_id") or run.get("summary_status") != "done":
        return False
    summary = connection.execute(
        "SELECT created_at FROM task WHERE id=? AND tenant_id=? "
        "AND deleted_at IS NULL",
        (run["summary_task_id"], run["tenant_id"]),
    ).fetchone()
    if not summary:
        return True
    for member in members:
        if member["status"] != "done":
            continue
        latest = _latest_delivered_task(
            connection, member["task_id"], run["tenant_id"],
        )
        if latest and float(latest["updated_at"] or 0) > float(summary["created_at"] or 0):
            return True
    return False


def get_run(run_id: int, tenant_id: int) -> dict:
    """只按租户读取小队；页面刷新后可从服务端恢复全部进度。"""
    run_id, tenant_id = _positive(run_id, "小队编号"), _positive(tenant_id, "企业编号")
    row = _run_row(run_id, tenant_id)
    members = db.q(
        "SELECT * FROM team_run_member WHERE team_run_id=? AND tenant_id=? ORDER BY position",
        (run_id, tenant_id),
    )
    public_members = []
    leader_plan_md = ""
    for member in members:
        m = {
            "id": member["id"],
            "idx": member["emp_idx"],
            "emp_idx": member["emp_idx"],
            "name": member["name"],
            "role": member["role"],
            "roleInTeam": member["role_in_team"],
            "task": member["task_text"],
            "dependsOn": db.jloads(member["depends_on_json"], []),
            "status": member["status"],
            "approved": bool(member["approved"]),
            "task_id": member["task_id"],
            "attempt_no": member["attempt_no"],
            "last_error": member["last_error"],
        }
        public_members.append(m)
        if member["emp_idx"] == row["leader_emp_idx"] and member["status"] == "done":
            task = _latest_delivered_task(db.conn(), member["task_id"], tenant_id)
            leader_plan_md = (task or {}).get("output_md") or ""
    summary_md = ""
    summary_stale = False
    if row["summary_task_id"] and row["summary_status"] == "done":
        summary = _latest_delivered_task(db.conn(), row["summary_task_id"], tenant_id)
        summary_md = (summary or {}).get("output_md") or ""
        summary_stale = _summary_is_stale(db.conn(), row, members)
    unit_points = (
        0.0 if tenant_id == 1 else
        float((billing.prices().get("expert_task") or {"points": 1})["points"])
    )
    charged = db.one(
        "SELECT COALESCE(SUM(CASE WHEN billing_status='charged' "
        "THEN billing_points ELSE 0 END),0) AS points, COUNT(*) AS tasks "
        "FROM task WHERE tenant_id=? AND request_key LIKE ?",
        (tenant_id, f"teamrun:{run_id}:%"),
    ) or {}
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "query": row["query"],
        "team_name": row["team_name"],
        "team_summary": row["team_summary"],
        "mode": row["mode"],
        "depth": row["depth"],
        "status": row["status"],
        "leader_emp_idx": row["leader_emp_idx"],
        "leader_plan_md": leader_plan_md,
        "summary_task_id": row["summary_task_id"],
        "summary_status": row["summary_status"],
        "summary_error": row["summary_error"],
        "summary_output_md": summary_md,
        "summary_stale": summary_stale,
        "estimated_tasks": len(members) + 1,
        "estimated_points": unit_points * (len(members) + 1),
        "unit_points": unit_points,
        "charged_points": float(charged.get("points") or 0),
        "created_task_count": int(charged.get("tasks") or 0),
        "members": public_members,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_runs(tenant_id: int, *, limit: int = 20) -> list[dict]:
    tenant_id = _positive(tenant_id, "企业编号")
    limit = max(1, min(int(limit), 50))
    rows = db.q(
        "SELECT id FROM team_run WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
        (tenant_id, limit),
    )
    return [get_run(row["id"], tenant_id) for row in rows]


def pending_run_ids(tenant_id: int) -> list[int]:
    """仅作恢复候选列表；自动恢复仍应在已认证租户请求里运行。"""
    tenant_id = _positive(tenant_id, "企业编号")
    return [row["id"] for row in db.q(
        "SELECT id FROM team_run WHERE tenant_id=? AND status NOT IN ('done','failed') "
        "ORDER BY id DESC LIMIT 50",
        (tenant_id,),
    )]


def approve_member(run_id: int, tenant_id: int, member_id: int) -> dict:
    run_id, tenant_id, member_id = (
        _positive(run_id, "小队编号"), _positive(tenant_id, "企业编号"),
        _positive(member_id, "成员编号"),
    )
    with db.atomic() as connection:
        run = _run_row(run_id, tenant_id, connection)
        member = _member_row(run_id, tenant_id, member_id, connection)
        if run["mode"] != "semi":
            raise TeamRunConflict("not_semi", "全自动小队不需要逐人确认")
        if run["summary_task_id"]:
            raise TeamRunConflict("already_summarizing", "小队已进入收尾阶段")
        if member["emp_idx"] != run["leader_emp_idx"]:
            leader = connection.execute(
                "SELECT id,status,task_id FROM team_run_member "
                "WHERE team_run_id=? AND tenant_id=? AND emp_idx=?",
                (run_id, tenant_id, run["leader_emp_idx"]),
            ).fetchone()
            plan = (
                _latest_delivered_task(connection, leader["task_id"], tenant_id)
                if leader and leader["status"] == "done" else None
            )
            if not plan or not str(plan.get("output_md") or "").strip():
                raise TeamRunConflict(
                    "leader_plan_required",
                    "请先确认队长开工，待拆解计划交付后再逐人确认其他成员",
                )
        if member["status"] in {"failed", "skipped"}:
            raise TeamRunConflict("member_terminal", "该成员需先重试，不能直接批准")
        if not member["approved"]:
            now = time.time()
            connection.execute(
                "UPDATE team_run_member SET approved=1,approved_at=?,updated_at=? "
                "WHERE id=? AND team_run_id=? AND tenant_id=?",
                (now, now, member_id, run_id, tenant_id),
            )
        _sync_status(connection, run_id, tenant_id)
    return get_run(run_id, tenant_id)


def retry_member(run_id: int, tenant_id: int, member_id: int) -> dict:
    run_id, tenant_id, member_id = (
        _positive(run_id, "小队编号"), _positive(tenant_id, "企业编号"),
        _positive(member_id, "成员编号"),
    )
    with db.atomic() as connection:
        run = _run_row(run_id, tenant_id, connection)
        member = _member_row(run_id, tenant_id, member_id, connection)
        if run["summary_task_id"]:
            raise TeamRunConflict("already_summarizing", "小队已进入收尾阶段")
        if member["status"] != "failed":
            raise TeamRunConflict("not_failed", "只有失败的成员可以重试")
        now = time.time()
        connection.execute(
            "UPDATE team_run_member SET status='pending',approved=1,approved_at=?,"
            "attempt_no=attempt_no+1,task_id=NULL,claim_until=NULL,last_error=NULL,updated_at=? "
            "WHERE id=? AND team_run_id=? AND tenant_id=?",
            (now, now, member_id, run_id, tenant_id),
        )
        _sync_status(connection, run_id, tenant_id)
    return get_run(run_id, tenant_id)


def skip_member(run_id: int, tenant_id: int, member_id: int) -> dict:
    """老板明确放弃失败/尚未派出的分工后，其他成员可继续，收尾标注缺口。"""
    run_id, tenant_id, member_id = (
        _positive(run_id, "小队编号"), _positive(tenant_id, "企业编号"),
        _positive(member_id, "成员编号"),
    )
    with db.atomic() as connection:
        run = _run_row(run_id, tenant_id, connection)
        member = _member_row(run_id, tenant_id, member_id, connection)
        if member["emp_idx"] == run["leader_emp_idx"]:
            raise TeamRunConflict("leader_plan_required", "队长前置拆解不能跳过，请重试队长任务")
        if run["summary_task_id"] or member["status"] not in {"pending", "failed"}:
            raise TeamRunConflict("cannot_skip", "该成员已有正在执行的任务，不能跳过")
        now = time.time()
        connection.execute(
            "UPDATE team_run_member SET status='skipped',approved=1,approved_at=?,"
            "claim_until=NULL,updated_at=? WHERE id=? AND team_run_id=? AND tenant_id=?",
            (now, now, member_id, run_id, tenant_id),
        )
        _sync_status(connection, run_id, tenant_id)
    return get_run(run_id, tenant_id)


def _member_request_key(run_id: int, emp_idx: int, attempt_no: int) -> str:
    return f"teamrun:{run_id}:member:{emp_idx}:a{attempt_no}"


def _summary_request_key(run_id: int, attempt_no: int) -> str:
    return f"teamrun:{run_id}:summary:a{attempt_no}"


def _task_status(raw: str) -> str:
    if raw == "done":
        return "done"
    if raw in {"failed", "cancelled"}:
        return "failed"
    if raw == "running":
        return "running"
    return "queued"


def _sync_status(connection, run_id: int, tenant_id: int):
    run = _run_row(run_id, tenant_id, connection)
    members = [dict(row) for row in connection.execute(
        "SELECT status,approved FROM team_run_member WHERE team_run_id=? AND tenant_id=?",
        (run_id, tenant_id),
    )]
    if run["summary_status"] == "done":
        status = "done"
    elif run["summary_status"] == "failed":
        status = "needs_attention"
    elif run["summary_status"] in {"dispatching", "queued", "running"}:
        status = "summarizing"
    elif any(m["status"] in {"dispatching", "queued", "running"} for m in members):
        status = "running"
    elif any(m["approved"] and m["status"] == "pending" for m in members):
        # 半自动是逐人批准、逐人开工，不要求全员先批准。
        status = "running"
    elif any(m["status"] == "failed" for m in members):
        status = "needs_attention"
    elif any(not m["approved"] and m["status"] == "pending" for m in members):
        status = "awaiting_approval"
    elif all(m["status"] in {"done", "skipped"} for m in members):
        status = "failed" if all(m["status"] == "skipped" for m in members) else "summarizing"
    else:
        status = "running"
    if run["status"] != status:
        connection.execute(
            "UPDATE team_run SET status=?,updated_at=? WHERE id=? AND tenant_id=?",
            (status, time.time(), run_id, tenant_id),
        )
    return status


def refresh_run(run_id: int, tenant_id: int) -> dict:
    """从真实任务状态修正小队；兼容进程重启后创建成功但未回填的派单。"""
    run_id, tenant_id = _positive(run_id, "小队编号"), _positive(tenant_id, "企业编号")
    with db.atomic() as connection:
        run = _run_row(run_id, tenant_id, connection)
        now = time.time()
        members = [dict(row) for row in connection.execute(
            "SELECT * FROM team_run_member WHERE team_run_id=? AND tenant_id=? ORDER BY position",
            (run_id, tenant_id),
        )]
        for member in members:
            if member["status"] in {"pending", "skipped"}:
                continue
            task = None
            if member["task_id"]:
                task = connection.execute(
                    "SELECT id,emp_idx,status FROM task WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                    (member["task_id"], tenant_id),
                ).fetchone()
            elif member["status"] == "dispatching":
                task = connection.execute(
                    "SELECT id,emp_idx,status FROM task WHERE tenant_id=? AND request_key=? "
                    "AND deleted_at IS NULL",
                    (tenant_id, _member_request_key(run_id, member["emp_idx"], member["attempt_no"])),
                ).fetchone()
            if task and task["emp_idx"] == member["emp_idx"]:
                new_status = _task_status(task["status"])
                connection.execute(
                    "UPDATE team_run_member SET task_id=?,status=?,claim_until=NULL,"
                    "last_error=?,updated_at=? WHERE id=? AND tenant_id=?",
                    (task["id"], new_status,
                     "成员任务执行失败，可重试或跳过" if new_status == "failed" else None,
                     now, member["id"], tenant_id),
                )
            elif member["task_id"]:
                connection.execute(
                    "UPDATE team_run_member SET status='failed',claim_until=NULL,last_error=?,"
                    "updated_at=? WHERE id=? AND tenant_id=?",
                    ("成员任务已不可用，可重试或跳过", now, member["id"], tenant_id),
                )
        if run["summary_status"] in {"dispatching", "queued", "running"}:
            summary = None
            if run["summary_task_id"]:
                summary = connection.execute(
                    "SELECT id,emp_idx,status FROM task WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                    (run["summary_task_id"], tenant_id),
                ).fetchone()
            else:
                summary = connection.execute(
                    "SELECT id,emp_idx,status FROM task WHERE tenant_id=? AND request_key=? "
                    "AND deleted_at IS NULL",
                    (tenant_id, _summary_request_key(run_id, run["summary_attempt_no"])),
                ).fetchone()
            if summary and summary["emp_idx"] == run["leader_emp_idx"]:
                new_status = _task_status(summary["status"])
                connection.execute(
                    "UPDATE team_run SET summary_task_id=?,summary_status=?,summary_claim_until=NULL,"
                    "summary_error=?,updated_at=? WHERE id=? AND tenant_id=?",
                    (summary["id"], new_status,
                     "收尾任务执行失败，可重试" if new_status == "failed" else None,
                     now, run_id, tenant_id),
                )
            elif run["summary_task_id"]:
                connection.execute(
                    "UPDATE team_run SET summary_status='failed',summary_claim_until=NULL,"
                    "summary_error=?,updated_at=? WHERE id=? AND tenant_id=?",
                    ("收尾任务已不可用，可重试", now, run_id, tenant_id),
                )
        _sync_status(connection, run_id, tenant_id)
    return get_run(run_id, tenant_id)


def _member_body(run: dict, member: dict, by_idx: dict, connection) -> dict:
    is_leader = member["emp_idx"] == run["leader_emp_idx"]
    team_label = run["team_name"]
    if is_leader:
        roster = "\n".join(
            f"- {m['name'] or m['role'] or m['emp_idx']}（{m['role_in_team']}）：{m['task_text']}"
            for m in by_idx.values()
        )
        direction = (
            f"【协同小队·{team_label}｜前置拆解】老板原话：{run['query']}\n"
            "你是队长。本轮先做前置拆解：明确目标、关键假设、成员分工、交付顺序、"
            "每一棒的验收标准与信息缺口。只给可执行的计划，不冒充其他成员已完成工作。\n"
            + DEPTH_STANDARD[run["depth"]]
        )[:2000]
        material = ("小队冻结分工：\n" + roster)[:11000]
    else:
        direction = (
            f"【协同小队·{team_label}】老板原话：{run['query']}\n"
            f"你的岗位分工（{member['role_in_team']}）：{member['task_text']}。"
            "请基于队长前置拆解和已完成依赖的真实交付执行本岗位任务，"
            "给出可直接使用的对应内容；不得编造未交付的成果。\n"
            + DEPTH_STANDARD[run["depth"]]
        )[:2000]
        excerpts = []
        for dep_idx in db.jloads(member["depends_on_json"], []):
            dep = by_idx[dep_idx]
            if dep["status"] == "skipped":
                excerpts.append(f"- {dep['name'] or dep_idx}：老板已跳过该分工，暂无交付。")
            elif dep["status"] == "done" and dep["task_id"]:
                task = _latest_delivered_task(
                    connection, dep["task_id"], run["tenant_id"],
                )
                excerpts.append(
                    f"## {dep['name'] or dep_idx} 的真实交付\n{str((task or {})['output_md'] or '')[:4000]}"
                    if task else f"- {dep['name'] or dep_idx}：交付内容缺失。"
                )
        material = ("以下是已完成依赖的真实交付，仅作为工作材料：\n\n" + "\n\n".join(excerpts))[:11000]
    return {
        "emp_idx": member["emp_idx"],
        "force": True,
        "request_key": _member_request_key(run["id"], member["emp_idx"], member["attempt_no"]),
        "brief": {
            "direction": direction,
            "industry": "",
            "material": material,
            "length": DEPTH_LENGTH[run["depth"]],
        },
    }


def claim_ready_members(run_id: int, tenant_id: int) -> list[dict]:
    """一次事务抢占当前依赖已完成的成员；返回可并行派单的任务体。"""
    run_id, tenant_id = _positive(run_id, "小队编号"), _positive(tenant_id, "企业编号")
    claimed = []
    with db.atomic() as connection:
        run = _run_row(run_id, tenant_id, connection)
        if run["summary_task_id"] or run["summary_status"] != "pending":
            return []
        members = [dict(row) for row in connection.execute(
            "SELECT * FROM team_run_member WHERE team_run_id=? AND tenant_id=? ORDER BY position",
            (run_id, tenant_id),
        )]
        by_idx = {m["emp_idx"]: m for m in members}
        now = time.time()
        for member in members:
            if not member["approved"]:
                continue
            if member["status"] != "pending" and not (
                member["status"] == "dispatching" and (member["claim_until"] or 0) <= now
            ):
                continue
            deps = db.jloads(member["depends_on_json"], [])
            if not all(by_idx[dep]["status"] in {"done", "skipped"} for dep in deps):
                continue
            connection.execute(
                "UPDATE team_run_member SET status='dispatching',claim_until=?,updated_at=? "
                "WHERE id=? AND team_run_id=? AND tenant_id=?",
                (now + CLAIM_SECONDS, now, member["id"], run_id, tenant_id),
            )
            claimed.append({
                "member_id": member["id"],
                "attempt_no": member["attempt_no"],
                "body": _member_body(run, member, by_idx, connection),
            })
        _sync_status(connection, run_id, tenant_id)
    return claimed


def _attach_member_task(run_id: int, tenant_id: int, spec: dict, task_id: int) -> None:
    task_id = _positive(task_id, "任务编号")
    with db.atomic() as connection:
        _run_row(run_id, tenant_id, connection)
        member = _member_row(run_id, tenant_id, spec["member_id"], connection)
        task = connection.execute(
            "SELECT id,tenant_id,emp_idx,status,request_key FROM task WHERE id=? AND tenant_id=? "
            "AND deleted_at IS NULL",
            (task_id, tenant_id),
        ).fetchone()
        if not task or task["emp_idx"] != member["emp_idx"] or task["request_key"] != spec["body"]["request_key"]:
            raise TeamRunConflict("task_mismatch", "小队任务身份校验失败，请联系管理员")
        if member["attempt_no"] != spec["attempt_no"]:
            return
        if member["task_id"] and member["task_id"] != task_id:
            raise TeamRunConflict("task_conflict", "成员已绑定另一项任务")
        now = time.time()
        connection.execute(
            "UPDATE team_run_member SET task_id=?,status=?,claim_until=NULL,last_error=NULL,"
            "updated_at=? WHERE id=? AND team_run_id=? AND tenant_id=?",
            (task_id, _task_status(task["status"]), now, member["id"], run_id, tenant_id),
        )
        _sync_status(connection, run_id, tenant_id)


def _member_dispatch_failed(run_id: int, tenant_id: int, spec: dict) -> None:
    with db.atomic() as connection:
        member = _member_row(run_id, tenant_id, spec["member_id"], connection)
        if member["attempt_no"] != spec["attempt_no"] or member["task_id"]:
            return
        # 创建任务/扣点可能已经提交，只是回包或回填失败。先按既有任务幂等号
        # 找到它，绝不能误报失败后让老板重试再付一次钱。
        task = connection.execute(
            "SELECT id,emp_idx,status FROM task WHERE tenant_id=? AND request_key=? "
            "AND deleted_at IS NULL",
            (tenant_id, spec["body"]["request_key"]),
        ).fetchone()
        if task and task["emp_idx"] == member["emp_idx"]:
            status = _task_status(task["status"])
            connection.execute(
                "UPDATE team_run_member SET task_id=?,status=?,claim_until=NULL,"
                "last_error=?,updated_at=? WHERE id=? AND team_run_id=? AND tenant_id=?",
                (task["id"], status,
                 "成员任务执行失败，可重试或跳过" if status == "failed" else None,
                 time.time(), member["id"], run_id, tenant_id),
            )
            _sync_status(connection, run_id, tenant_id)
            return
        connection.execute(
            "UPDATE team_run_member SET status='failed',claim_until=NULL,last_error=?,updated_at=? "
            "WHERE id=? AND team_run_id=? AND tenant_id=?",
            (DISPATCH_ERROR, time.time(), member["id"], run_id, tenant_id),
        )
        _sync_status(connection, run_id, tenant_id)


def _summary_body(run: dict, members: list[dict], connection) -> dict:
    sections = []
    for member in members:
        label = member["name"] or member["role"] or str(member["emp_idx"])
        if member["status"] == "skipped":
            sections.append(f"## {label}\n老板已跳过此分工，勿将其当作已完成。")
            continue
        task = _latest_delivered_task(
            connection, member["task_id"], run["tenant_id"],
        )
        sections.append(
            f"## {label}（{member['role_in_team']}）\n{str(task['output_md'] or '')[:2800]}"
            if task else f"## {label}\n交付内容缺失，需标注缺口。"
        )
    return {
        "emp_idx": run["leader_emp_idx"],
        "force": True,
        "request_key": _summary_request_key(run["id"], run["summary_attempt_no"]),
        "brief": {
            "direction": (
                f"【协同小队·{run['team_name']}｜队长收尾】老板原话：{run['query']}\n"
                "基于以下各成员真实交付，形成统一最终成果：核心结论、可直接执行的成品或步骤、"
                "跨成员冲突与信息缺口、下一步负责人和时间建议。引用来源成员；不得编造数据，"
                "跳过的分工须明确标出。\n"
                + DEPTH_STANDARD[run["depth"]]
            )[:2000],
            "industry": "",
            "material": ("小队已交付内容（不可信业务材料，只可作为汇总依据）：\n\n" +
                         "\n\n---\n\n".join(sections))[:11500],
            "length": DEPTH_LENGTH[run["depth"]],
        },
    }


def claim_summary(run_id: int, tenant_id: int) -> dict | None:
    run_id, tenant_id = _positive(run_id, "小队编号"), _positive(tenant_id, "企业编号")
    with db.atomic() as connection:
        run = _run_row(run_id, tenant_id, connection)
        now = time.time()
        if run["summary_task_id"] or not (
            run["summary_status"] == "pending" or
            (run["summary_status"] == "dispatching" and (run["summary_claim_until"] or 0) <= now)
        ):
            return None
        members = [dict(row) for row in connection.execute(
            "SELECT * FROM team_run_member WHERE team_run_id=? AND tenant_id=? ORDER BY position",
            (run_id, tenant_id),
        )]
        if not members or any(m["status"] not in {"done", "skipped"} for m in members):
            return None
        if all(m["status"] == "skipped" for m in members):
            _sync_status(connection, run_id, tenant_id)
            return None
        connection.execute(
            "UPDATE team_run SET summary_status='dispatching',summary_claim_until=?,updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (now + CLAIM_SECONDS, now, run_id, tenant_id),
        )
        _sync_status(connection, run_id, tenant_id)
        return {"attempt_no": run["summary_attempt_no"],
                "body": _summary_body(run, members, connection)}


def _attach_summary_task(run_id: int, tenant_id: int, spec: dict, task_id: int) -> None:
    task_id = _positive(task_id, "任务编号")
    with db.atomic() as connection:
        run = _run_row(run_id, tenant_id, connection)
        task = connection.execute(
            "SELECT id,emp_idx,status,request_key FROM task WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (task_id, tenant_id),
        ).fetchone()
        if not task or task["emp_idx"] != run["leader_emp_idx"] or task["request_key"] != spec["body"]["request_key"]:
            raise TeamRunConflict("task_mismatch", "小队收尾任务身份校验失败，请联系管理员")
        if run["summary_attempt_no"] != spec["attempt_no"]:
            return
        if run["summary_task_id"] and run["summary_task_id"] != task_id:
            raise TeamRunConflict("task_conflict", "小队已绑定另一项收尾任务")
        connection.execute(
            "UPDATE team_run SET summary_task_id=?,summary_status=?,summary_claim_until=NULL,"
            "summary_error=NULL,updated_at=? WHERE id=? AND tenant_id=?",
            (task_id, _task_status(task["status"]), time.time(), run_id, tenant_id),
        )
        _sync_status(connection, run_id, tenant_id)


def _summary_dispatch_failed(run_id: int, tenant_id: int, spec: dict) -> None:
    with db.atomic() as connection:
        run = _run_row(run_id, tenant_id, connection)
        if run["summary_attempt_no"] != spec["attempt_no"] or run["summary_task_id"]:
            return
        task = connection.execute(
            "SELECT id,emp_idx,status FROM task WHERE tenant_id=? AND request_key=? "
            "AND deleted_at IS NULL",
            (tenant_id, spec["body"]["request_key"]),
        ).fetchone()
        if task and task["emp_idx"] == run["leader_emp_idx"]:
            status = _task_status(task["status"])
            connection.execute(
                "UPDATE team_run SET summary_task_id=?,summary_status=?,"
                "summary_claim_until=NULL,summary_error=?,updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (task["id"], status,
                 "收尾任务执行失败，可重试" if status == "failed" else None,
                 time.time(), run_id, tenant_id),
            )
            _sync_status(connection, run_id, tenant_id)
            return
        connection.execute(
            "UPDATE team_run SET summary_status='failed',summary_claim_until=NULL,"
            "summary_error=?,updated_at=? WHERE id=? AND tenant_id=?",
            ("收尾派单失败，请检查点数或稍后重试", time.time(), run_id, tenant_id),
        )
        _sync_status(connection, run_id, tenant_id)


def retry_summary(run_id: int, tenant_id: int) -> dict:
    run_id, tenant_id = _positive(run_id, "小队编号"), _positive(tenant_id, "企业编号")
    with db.atomic() as connection:
        run = _run_row(run_id, tenant_id, connection)
        if run["summary_status"] != "failed":
            members = [dict(row) for row in connection.execute(
                "SELECT status,task_id FROM team_run_member "
                "WHERE team_run_id=? AND tenant_id=?", (run_id, tenant_id),
            )]
            if run["summary_status"] != "done" or not _summary_is_stale(
                connection, run, members,
            ):
                raise TeamRunConflict(
                    "summary_not_stale",
                    "只有失败的收尾任务或成员交付已更新的小队可以重新汇总",
                )
        connection.execute(
            "UPDATE team_run SET summary_status='pending',summary_task_id=NULL,"
            "summary_attempt_no=summary_attempt_no+1,summary_claim_until=NULL,"
            "summary_error=NULL,updated_at=? WHERE id=? AND tenant_id=?",
            (time.time(), run_id, tenant_id),
        )
        _sync_status(connection, run_id, tenant_id)
    return get_run(run_id, tenant_id)


async def advance_run(
    run_id: int, tenant_id: int,
    create_task: Callable[[dict], Awaitable[dict]],
) -> dict:
    """推进一轮：已完成依赖的成员并行派单；所有成员完成后队长收尾。"""
    await db.arun(refresh_run, run_id, tenant_id)
    specs = await db.arun(claim_ready_members, run_id, tenant_id)

    async def dispatch_member(spec):
        try:
            result = await create_task(spec["body"])
            task_id = result.get("task_id") if isinstance(result, dict) else None
            if not task_id:
                raise TeamRunConflict("dispatch_empty", "派单未返回任务编号")
            await db.arun(_attach_member_task, run_id, tenant_id, spec, int(task_id))
        except Exception:
            # 不记录外部异常原文；它可能含模型请求或凭据。确定性请求号可重试恢复。
            await db.arun(_member_dispatch_failed, run_id, tenant_id, spec)

    if specs:
        await asyncio.gather(*(dispatch_member(spec) for spec in specs))
    await db.arun(refresh_run, run_id, tenant_id)
    summary = await db.arun(claim_summary, run_id, tenant_id)
    if summary:
        try:
            result = await create_task(summary["body"])
            task_id = result.get("task_id") if isinstance(result, dict) else None
            if not task_id:
                raise TeamRunConflict("dispatch_empty", "收尾派单未返回任务编号")
            await db.arun(_attach_summary_task, run_id, tenant_id, summary, int(task_id))
        except Exception:
            await db.arun(_summary_dispatch_failed, run_id, tenant_id, summary)
    return await db.arun(refresh_run, run_id, tenant_id)


async def watch_run(
    run_id: int, tenant_id: int,
    create_task: Callable[[dict], Awaitable[dict]], *,
    poll_seconds: float = 3.0, max_seconds: float = 7200.0,
) -> dict:
    """在已授权请求创建的后台协程中持续推进；重启后可再次调用恢复。"""
    deadline = time.monotonic() + max_seconds
    while True:
        result = await advance_run(run_id, tenant_id, create_task)
        if result["status"] in {"done", "failed", "needs_attention", "awaiting_approval"}:
            return result
        if time.monotonic() >= deadline:
            return result
        await asyncio.sleep(max(0.05, poll_seconds))
