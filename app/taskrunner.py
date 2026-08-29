"""专家任务引擎(V5):行业部门数字员工的单人工单.

与内容部流水线互不干扰:一个任务 = 一名专家 + 一份任务书 → 一份 Markdown 交付物。
全程联网检索,步骤实时 SSE(type=task_step),交付物自动入资产库(type=report)。
"""
import asyncio
import json
import logging
import re
import time

from . import billing, db, departments, employeeidentity, employees, llm, providers

log = logging.getLogger("taskrunner")

RUNNING: set = set()   # task_id 正在执行
MAX_FREE_RETRIES = 3
_MEETING_DELIVERY_START = "<!-- execution-deliveries:start -->"
_MEETING_DELIVERY_END = "<!-- execution-deliveries:end -->"


def _approved_effective_role_context(binding: dict) -> dict:
    """Return the exact approved execution view from a frozen role bundle.

    The config row is the immutable revision anchor, while ``effective`` is the
    approved execution payload for that revision.  Learning proposals and
    current-slot state are deliberately absent from this path: a historical
    task must keep replaying the bundle hash persisted on the task itself.
    """
    if not isinstance(binding, dict):
        raise ValueError("员工岗位绑定缺失")
    role_bundle = binding.get("role_bundle")
    config = binding.get("config")
    employee = binding.get("employee")
    if (
        not isinstance(role_bundle, dict)
        or not isinstance(config, dict)
        or not isinstance(employee, dict)
        or str(role_bundle.get("status") or "") not in {"active", "historical"}
        or not db.employee_role_bundle_row_valid(role_bundle)
    ):
        raise ValueError("员工岗位 role bundle 未批准或完整性校验失败")
    effective = role_bundle.get("effective")
    if not isinstance(effective, dict):
        raise ValueError("员工岗位 role bundle 有效档案缺失")
    effective_profile = effective.get("professional_profile")
    if not isinstance(effective_profile, dict):
        raise ValueError("员工岗位 role bundle 专业档案无效")
    workflow = effective.get("workflow", [])
    if not isinstance(workflow, (str, list, dict)):
        raise ValueError("员工岗位 role bundle 工作流程无效")
    capabilities = effective.get(
        "capabilities", effective_profile.get("capabilities", [])
    )
    if not isinstance(capabilities, (list, dict)):
        raise ValueError("员工岗位 role bundle 专业能力无效")
    effective_config = db.normalize_employee_config(effective.get("config"))
    skills = effective.get("skills", effective_config.get("skills", []))
    if not isinstance(skills, list):
        raise ValueError("员工岗位 role bundle 技能无效")
    effective_config = {**config, **effective_config, "skills": skills}
    contract = effective.get("decision_contract") or {}
    # 泄露检测的指纹源保持与旧实现同构的紧凑 JSON：可读渲染逐行是自然
    # 中文，会把「员工在交付里正常运用岗位口径」误判为 32 字逐行泄露；
    # JSON 形态只在 64 字滑窗粒度拦截大段复制，保护粒度与优化前一致。
    approved_sensitive = json.dumps(
        {
            "professional_profile": effective_profile,
            "workflow": workflow,
            "capabilities": capabilities,
            "skills": skills,
            "decision_contract": contract,
            "outputs": effective.get("outputs") or [],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )[:24000]
    # build_task_prompt 用同一批准配置全文渲染手册、工作流步骤、技能库与
    # （目录版本一致时的）决策合同；这里只补档案与指纹，不再重复原始 JSON。
    approved_context = employees.approved_role_context_text(
        fingerprint=(
            f"config_revision={role_bundle.get('config_revision')} "
            f"config_sha256={str(role_bundle.get('config_sha256') or '')[:16]} "
            f"bundle_sha256={str(role_bundle.get('bundle_sha256') or '')[:16]}"
        ),
        profile=effective_profile,
        workflow=workflow,
        capabilities=capabilities,
        skills=skills,
        outputs=effective.get("outputs") or [],
        decision_contract=contract,
        profile_rendered=False,
        workflow_rendered=False,
        skills_rendered=True,
        contract_rendered=bool(
            contract and contract == (employee.get("decision_contract") or {})
        ),
    )
    return {
        "employee": {**employee, "professional_profile": effective_profile},
        "config": effective_config,
        "role_bundle": role_bundle,
        "effective_profile": effective_profile,
        "workflow": workflow,
        "capabilities": capabilities,
        "skills": skills,
        "approved_context": approved_context,
        "approved_sensitive": approved_sensitive,
    }


def _with_approved_role_context(
    prompt: providers.PromptBundle, context: dict,
) -> providers.PromptBundle:
    approved = context["approved_context"]
    leak_source = context.get("approved_sensitive") or approved
    return providers.PromptBundle(
        system=(
            prompt.system
            + "\n\n【已批准的冻结岗位能力包】\n"
            + approved
            + "\n必须实际使用该版本的岗位档案、技能、能力与工作流程完成任务。"
        ),
        user=prompt.user,
        research=prompt.research,
        sensitive=tuple(prompt.sensitive) + (leak_source,),
    )


def normalize_brief(raw) -> dict:
    """Validate and bound the direct-expert task contract before billing/model use."""
    if not isinstance(raw, dict):
        raise ValueError("任务书格式无效")

    def text(field: str, limit: int, *, required: bool = False) -> str:
        value = raw.get(field, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{field} 格式无效")
        value = value.strip()
        if required and not value:
            raise ValueError("任务内容必填")
        if len(value) > limit:
            raise ValueError(f"{field} 最多 {limit} 个字符")
        return value

    forbidden_server_fields = {
        "decision_evidence", "provenance", "web_sources", "supplemental_provenance",
    }
    injected = sorted(forbidden_server_fields & set(raw))
    if injected:
        raise ValueError("任务书包含不可由客户端写入的证据字段")

    result = {
        "direction": text("direction", 2000, required=True),
    }
    for field, limit in (
        ("industry", 120),
        ("material", 12000),
        # 持续协作的本轮新材料单独保留，防止被首轮长材料
        # 挤出模型实际读取范围。累计 material 仍是完整审计副本。
        ("revision_material", 12000),
        ("feedback", 2000),
        # 持续协作由服务端从同线程上一版读取；上限足以保留一份正常交付，
        # 又避免无界历史在每轮指数膨胀。
        ("prev_excerpt", 12000),
        ("context", 4000),
    ):
        value = text(field, limit)
        if value:
            result[field] = value
    length = raw.get("length", "")
    if length is None:
        length = ""
    if not isinstance(length, str) or length not in {"", "lite", "std", "full"}:
        raise ValueError("篇幅参数无效")
    if length:
        result["length"] = length
    return result


def normalize_task_brief(raw, employee: dict | None, tenant_id: int) -> dict:
    """Normalize a client task brief, then attach server-owned V2 provenance."""
    result = normalize_brief(raw)
    if not departments.is_decision_employee(employee):
        # Unknown brief fields were historically ignored for V1.  Preserve
        # that behavior; evidence_items has no effect and is not persisted.
        return result
    if "evidence_items" not in raw:
        return result
    result["decision_evidence"] = departments.normalize_decision_evidence(
        employee, tenant_id, raw.get("evidence_items")
    )
    return result


def validate_persisted_task_brief(
    raw, employee: dict | None, tenant_id: int,
) -> tuple[dict, dict | None]:
    """Revalidate the canonical manifest every time a task is executed."""
    if not isinstance(raw, dict):
        raise ValueError("任务书格式无效")
    client_fields = {
        key: value for key, value in raw.items()
        if key not in {"decision_evidence"}
    }
    if "evidence_items" in raw:
        raise ValueError("已持久化任务书不得包含客户端证据输入")
    brief = normalize_brief(client_fields)
    manifest = None
    if departments.is_decision_employee(employee):
        raw_manifest = raw.get("decision_evidence")
        if raw_manifest is not None:
            manifest = departments.validate_decision_evidence(
                employee, tenant_id, raw_manifest
            )
            brief["decision_evidence"] = manifest
    elif "decision_evidence" in raw:
        raise ValueError("非 V2 任务不得携带决策证据 manifest")
    return brief, manifest


def _broadcast_safely(broadcast, payload: dict):
    """SSE 是旁路提示，断线或回调故障绝不能改变任务的业务状态。"""
    try:
        broadcast(payload)
    except Exception as exc:
        log.debug(
            "task broadcast skipped error_type=%s",
            type(exc).__name__,
        )


def _claim_task(task_id: int) -> dict | None:
    """用数据库 CAS 抢到唯一 worker；进程内 RUNNING 只能作为性能优化。"""
    now = time.time()
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE task SET status='running',summary_md=NULL,terminal_at=NULL,"
            "updated_at=? "
            "WHERE id=? AND status='queued' "
            "AND billing_status IN ('charged','included') "
            "AND deleted_at IS NULL",
            (now, task_id),
        )
        if changed.rowcount != 1:
            return None
        row = connection.execute(
            "SELECT * FROM task WHERE id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None


def _meeting_delivery_section(
    connection,
    meeting_id: int,
    expected_tenant_id: int | None = None,
    trigger_task_id: int | None = None,
) -> tuple[str, str, str] | None:
    """Build a deterministic handoff section from real derived-task state."""
    meeting = connection.execute(
        "SELECT tenant_id,emp_idxs_json,member_snapshot_json,actions_json,"
        "consensus_md,summary_md "
        "FROM meeting WHERE id=?",
        (meeting_id,),
    ).fetchone()
    if (
        not meeting
        or (
            expected_tenant_id is not None
            and int(meeting["tenant_id"]) != int(expected_tenant_id)
        )
    ):
        return None

    frozen_members = employeeidentity.member_snapshot_contract(
        db.jloads(meeting["emp_idxs_json"], []),
        db.jloads(meeting["member_snapshot_json"], []),
    )
    if not frozen_members:
        return None
    frozen_by_idx = {int(row["idx"]): row for row in frozen_members}
    member_idxs = set(frozen_by_idx)

    # ``source_meeting_id`` alone is not an ownership proof: legacy/corrupt
    # rows can point an unrelated same-tenant task at this meeting.  Only the
    # action keys produced by this meeting, owned by their declared participant,
    # may contribute task titles or delivery text to the shared consensus.
    action_contracts: dict[str, tuple[int, str]] = {}
    conflicting_keys: set[str] = set()
    actions = db.jloads(meeting["actions_json"], []) or []
    if not isinstance(actions, list):
        return None
    for action in actions[:64]:
        if not isinstance(action, dict):
            continue
        key = action.get("key")
        try:
            idx = int(action.get("idx"))
        except (TypeError, ValueError):
            continue
        task_text = " ".join(
            str(action.get("task") or action.get("action") or "").split()
        )[:360]
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 160
            or idx not in member_idxs
            or not task_text
        ):
            continue
        contract = (idx, task_text)
        previous = action_contracts.get(key)
        if previous is not None and previous != contract:
            conflicting_keys.add(key)
            continue
        action_contracts[key] = contract
    for key in conflicting_keys:
        action_contracts.pop(key, None)
    if not action_contracts:
        return None

    placeholders = ",".join("?" for _ in action_contracts)
    rows = connection.execute(
        "SELECT id,emp_idx,brief_json,status,output_md,summary_md,deleted_at,"
        "source_action_key,employee_key,employee_catalog_version,"
        "employee_name_snapshot,employee_dept_key,employee_spec_sha256,"
        "employee_identity_ref,employee_config_revision,employee_config_sha256,"
        "person_snapshot,identity_scheme,bundle_sha256 "
        "FROM task WHERE source_meeting_id=? AND tenant_id=? "
        f"AND source_action_key IN ({placeholders}) ORDER BY id",
        (
            meeting_id,
            int(meeting["tenant_id"]),
            *action_contracts,
        ),
    ).fetchall()
    valid_rows = []
    for row in rows:
        contract = action_contracts.get(row["source_action_key"])
        if not contract or contract[0] != int(row["emp_idx"]):
            continue
        frozen = frozen_by_idx.get(int(row["emp_idx"]))
        if not frozen or any(
            str(row[field] or "") != str(frozen[snapshot_field])
            for field, snapshot_field in (
                ("employee_key", "key"),
                ("employee_catalog_version", "catalog_version"),
                ("employee_name_snapshot", "name"),
                ("employee_dept_key", "dept_key"),
                ("employee_spec_sha256", "spec_sha256"),
                ("employee_identity_ref", "identity_ref"),
                ("employee_config_revision", "config_revision"),
                ("employee_config_sha256", "config_sha256"),
                ("person_snapshot", "person_snapshot"),
                ("identity_scheme", "identity_scheme"),
                ("bundle_sha256", "bundle_sha256"),
            )
        ):
            continue
        if not employeeidentity.resolve_task_binding(dict(row)):
            continue
        brief = db.jloads(row["brief_json"], {}) or {}
        if not isinstance(brief, dict):
            continue
        direction = " ".join(str(brief.get("direction") or "").split())[:360]
        if not direction or direction != contract[1]:
            continue
        valid_rows.append(row)
    rows = valid_rows
    if (
        trigger_task_id is not None
        and int(trigger_task_id) not in {int(row["id"]) for row in rows}
    ):
        return None
    if not rows:
        return None

    from .skills import registry

    delivered = 0
    active = 0
    failed = 0
    lines = [
        _MEETING_DELIVERY_START,
        "## 执行交付结果",
    ]
    for row in rows:
        binding = employeeidentity.resolve_task_binding(dict(row)) or {}
        employee = binding.get("employee") or {}
        name = row["employee_name_snapshot"] or employee.get("name") or f"员工 {row['emp_idx']}"
        brief = db.jloads(row["brief_json"], {}) or {}
        task_name = " ".join(str(brief.get("direction") or "会议行动").split())[:160]
        if row["deleted_at"] is not None:
            state = "已移入回收站"
            detail = ""
        elif row["status"] == "done":
            delivered += 1
            state = "已交付"
            body = (row["summary_md"] or row["output_md"] or "").strip()
            detail = " ".join(body.split())[:500]
        elif row["status"] == "failed":
            failed += 1
            state = "执行失败，可免费重试"
            # 兼容旧版本中可能持久化过供应商正文/堆栈的失败记录。失败只显示
            # 稳定状态和重试动作，不把 output_md 二次复制进会议共识。
            detail = ""
        else:
            active += 1
            state = "执行中"
            detail = ""
        lines.append(f"- 任务 #{row['id']}｜{name}｜**{state}**：{task_name}")
        if detail:
            lines.append(f"  - 结果摘要：{detail}")
    lines.append(_MEETING_DELIVERY_END)

    total = len([row for row in rows if row["deleted_at"] is None])
    if active:
        next_action = f"继续跟踪会议派生任务：{delivered}/{total} 已交付"
    elif failed:
        next_action = f"{failed} 个会议派生任务失败；可免费重试，不重复扣费"
    elif total:
        next_action = f"会议派生任务已全部交付（{delivered}/{total}），请按验收标准查看结果"
    else:
        next_action = "会议派生任务均在回收站，可恢复后继续验收"
    summary = (meeting["summary_md"] or "").strip()
    summary = re.sub(r"\n?执行交付：\d+/\d+[^\n]*$", "", summary).rstrip()
    summary = (summary + f"\n执行交付：{delivered}/{total} 已完成").strip()
    return "\n".join(lines), next_action, summary


def _sync_meeting_delivery(
    connection,
    meeting_id: int,
    expected_tenant_id: int | None = None,
    trigger_task_id: int | None = None,
) -> bool:
    built = _meeting_delivery_section(
        connection,
        meeting_id,
        expected_tenant_id,
        trigger_task_id,
    )
    if not built:
        return False
    section, next_action, summary = built
    row = connection.execute(
        "SELECT consensus_md FROM meeting WHERE id=?", (meeting_id,)
    ).fetchone()
    consensus = (row["consensus_md"] or "") if row else ""
    if _MEETING_DELIVERY_START in consensus:
        before = consensus.split(_MEETING_DELIVERY_START, 1)[0].rstrip()
        tail = consensus.split(_MEETING_DELIVERY_END, 1)
        after = tail[1].lstrip() if len(tail) == 2 else ""
        consensus = "\n\n".join(part for part in (before, section, after) if part)
    else:
        consensus = (consensus.rstrip() + "\n\n" + section).strip()
    changed = connection.execute(
        "UPDATE meeting SET consensus_md=?,summary_md=?,next_action=?,updated_at=? "
        "WHERE id=?",
        (consensus, summary, next_action, time.time(), meeting_id),
    )
    return changed.rowcount == 1


def sync_meeting_delivery_for_task(task_id: int) -> bool:
    with db.atomic() as connection:
        row = connection.execute(
            "SELECT source_meeting_id,tenant_id FROM task WHERE id=?",
            (task_id,),
        ).fetchone()
        if not row or not row["source_meeting_id"]:
            return False
        return _sync_meeting_delivery(
            connection,
            int(row["source_meeting_id"]),
            int(row["tenant_id"]),
            int(task_id),
        )


def prepare_retry(
    task_id: int, tenant_id: int, brief: dict | None = None
) -> bool:
    """Queue one failed task again without a second debit.

    A paid failure has already been refunded.  Its retry therefore becomes an
    ``included`` attempt; a meeting-derived task was included from the start.
    The failed→queued CAS makes concurrent retry clicks idempotent.
    """
    with db.atomic() as connection:
        row = connection.execute(
            "SELECT source_meeting_id,tenant_id FROM task "
            "WHERE id=? AND status='failed' "
            "AND tenant_id=? AND COALESCE(retry_count,0)<? "
            "AND billing_status IN ('refunded','included') "
            "AND deleted_at IS NULL",
            (task_id, tenant_id, MAX_FREE_RETRIES),
        ).fetchone()
        if not row:
            return False
        fields = [
            "status='queued'",
            "billing_status='included'",
            "output_md=NULL",
            "summary_md=NULL",
            "steps_json='[]'",
            "cost_usd=0",
            "tokens=0",
            "terminal_at=NULL",
            "retry_count=COALESCE(retry_count,0)+1",
            "updated_at=?",
        ]
        params: list = [time.time()]
        if brief is not None:
            fields.append("brief_json=?")
            params.append(json.dumps(brief, ensure_ascii=False))
        params.append(task_id)
        params.extend((tenant_id, MAX_FREE_RETRIES))
        changed = connection.execute(
            f"UPDATE task SET {','.join(fields)} "
            "WHERE id=? AND status='failed' "
            "AND tenant_id=? AND COALESCE(retry_count,0)<? "
            "AND billing_status IN ('refunded','included') "
            "AND deleted_at IS NULL",
            tuple(params),
        )
        if changed.rowcount != 1:
            return False
        if row["source_meeting_id"]:
            _sync_meeting_delivery(
                connection,
                int(row["source_meeting_id"]),
                int(row["tenant_id"]),
                int(task_id),
            )
        return True


def settle_failure(task_id: int, message: str) -> bool:
    """把专家任务收口；独立付费任务按实际金额幂等退款，会议内含任务不退款。"""
    row = db.one(
        "SELECT tenant_id,status,billing_status,billing_points,source_meeting_id "
        "FROM task WHERE id=?",
        (task_id,),
    )
    if not row:
        return False
    text = (message or "执行失败")[:500]
    if row.get("billing_status") == "charged":
        points = row.get("billing_points")
        if points is None:  # 仅兼容升级前已扣费、尚未收口的旧任务。
            points = float(
                (billing.prices().get("expert_task") or {"points": 1})["points"]
            )

        def claim(connection):
            terminal_at = time.time()
            changed = connection.execute(
                "UPDATE task SET status='failed',billing_status='refunded',"
                "output_md=?,terminal_at=COALESCE(terminal_at,?),"
                "refunded_at=?,updated_at=? "
                "WHERE id=? AND billing_status='charged' "
                "AND status IN ('pending_charge','queued','running','failed')",
                (text, terminal_at, terminal_at, terminal_at, task_id),
            )
            if changed.rowcount == 1 and row.get("source_meeting_id"):
                _sync_meeting_delivery(
                    connection,
                    int(row["source_meeting_id"]),
                    int(row["tenant_id"]),
                    int(task_id),
                )
            return changed.rowcount == 1

        return billing.refund_amount_if_claimed(
            row.get("tenant_id") or 1,
            points,
            claim,
            "退回:行业专家任务/单独派活 · 执行失败",
        )
    with db.atomic() as connection:
        terminal_at = time.time()
        changed = connection.execute(
            "UPDATE task SET status='failed',output_md=?,terminal_at=?,updated_at=? "
            "WHERE id=? AND billing_status='included' "
            "AND status IN ('queued','running')",
            (text, terminal_at, terminal_at, task_id),
        )
        if changed.rowcount == 1 and row.get("source_meeting_id"):
            _sync_meeting_delivery(
                connection,
                int(row["source_meeting_id"]),
                int(row["tenant_id"]),
                int(task_id),
            )
        return changed.rowcount == 1


def _context_text(tid: int, query: str = "", *, with_meta: bool = False):
    """企业档案 + 公司知识沉淀(按租户隔离),注入专家/单独派活的员工提示词."""
    from .skills import registry
    return registry.context_block(tid, query, with_meta=with_meta)


async def run_task(task_id: int, broadcast):
    from .skills import registry
    if task_id in RUNNING:
        return
    t = await db.aone(
        "SELECT * FROM task WHERE id=? AND deleted_at IS NULL", (task_id,)
    )
    if not t or t["status"] != "queued":
        return
    idx = t["emp_idx"]
    binding = await db.arun(employeeidentity.resolve_task_binding, t)
    if not binding:
        await db.arun(
            settle_failure,
            task_id,
            "员工身份、岗位配置或能力包版本不匹配，已安全停止并退回点数",
        )
        return
    try:
        effective_role = _approved_effective_role_context(binding)
    except (TypeError, ValueError) as exc:
        await db.arun(
            settle_failure,
            task_id,
            "员工已批准能力包无法验证，已安全停止并退回点数",
        )
        log.warning(
            "task %s role bundle rejected error_type=%s",
            task_id,
            type(exc).__name__,
        )
        return
    e = effective_role["employee"]
    cfg = effective_role["config"]
    role_bundle = effective_role["role_bundle"]
    effective_profile = effective_role["effective_profile"]
    workflow = effective_role["workflow"]
    capabilities = effective_role["capabilities"]
    skills = effective_role["skills"]
    solo_content = e.get("dept_key") == "content"
    if solo_content:
        s = e
        e = {**s, "name": s["name"], "dept_name": "内容生产部", "group": s["dept"]}
    required_modules = (
        (str(e.get("dept_key") or "content"),)
        if e
        else ("content",)
    )
    t = await db.arun(_claim_task, task_id)
    if not t:
        return
    RUNNING.add(task_id)
    steps = []
    st = {"save": 0.0}

    def progress(kind, label=""):
        now = time.time()
        if kind == "typing" and steps and steps[-1]["k"] == "typing":
            steps[-1].update(l=str(label)[:300], ts=now)
        else:
            steps.append({"k": kind, "l": str(label)[:300], "ts": now})
        _broadcast_safely(
            broadcast,
            {
                "type": "task_step",
                "tenant_id": t.get("tenant_id") or 1,
                "_required_modules": required_modules,
                "task_id": task_id,
                "idx": t["emp_idx"],
                "n": len(steps),
                "step": steps[-1],
            },
        )
        if kind != "typing" or now - st["save"] > 3:
            # 事件循环上的进度落库进 db 线程池,避免写锁竞争冻结全部协程。
            db.submit_write(
                db.execute,
                "UPDATE task SET steps_json=?,updated_at=? "
                "WHERE id=? AND status='running' AND deleted_at IS NULL",
                (json.dumps(steps, ensure_ascii=False), now, task_id),
            )
            st["save"] = now

    async def cleanup():
        RUNNING.discard(task_id)
        try:
            await db.arun(sync_meeting_delivery_for_task, task_id)
        except Exception as exc:
            log.error(
                "task %s meeting delivery sync failed error_type=%s",
                task_id,
                type(exc).__name__,
            )
        _broadcast_safely(
            broadcast,
            {
                "type": "task_update",
                "tenant_id": t.get("tenant_id") or 1,
                "_required_modules": required_modules,
                "task_id": task_id,
                "idx": t["emp_idx"],
            },
        )

    _broadcast_safely(
        broadcast,
        {
            "type": "task_update",
            "tenant_id": t.get("tenant_id") or 1,
            "_required_modules": required_modules,
            "task_id": task_id,
            "idx": t["emp_idx"],
        },
    )
    md_done = None
    try:
        brief, decision_provenance = validate_persisted_task_brief(
            db.jloads(t["brief_json"], None),
            e,
            int(t.get("tenant_id") or 1),
        )
        ctx_text, recall = await db.arun(
            _context_text,
            t.get("tenant_id") or 1,
            "\n".join(str(value or "") for value in (
                brief.get("direction"),
                brief.get("context"),
                brief.get("industry"),
            )),
            with_meta=True,
        )
        if recall["selected"]:
            progress(
                "tool",
                f"已从公司知识库召回 {recall['selected']}/{recall['total']} 条相关经验",
            )
        if solo_content:
            prompt = registry.solo_prompt(
                idx, brief, employees.skills_block(idx, config=cfg), ctx_text,
                config=cfg,
            )
            web = registry.BY_IDX[idx]["key"] in (
                "trend", "research", "benchmark"
            )
            enabled_caps_count = sum(
                1 for cap in registry.capabilities_for(idx, config=cfg)
                if isinstance(cap, dict) and cap.get("enabled")
            )
        else:
            caps = departments.capabilities_for(
                idx, cfg.get("caps_off"), employee=e
            )
            # 员工自动进化:老板历次验收采纳的实战心得,随岗位配置注入本次任务。
            insights_text = await db.arun(
                employees.adopted_insights_text,
                int(t.get("tenant_id") or 1), idx,
            )
            if insights_text:
                progress(
                    "tool",
                    f"已装载老板验收沉淀的实战心得 {insights_text.count(chr(10)) + 1} 条",
                )
            prompt = departments.build_task_prompt(
                e, brief, employees.skills_block(idx, config=cfg), ctx_text, caps,
                private_template=cfg.get("prompt_template"),
                insights_text=insights_text,
            )
            # 产品承诺「派活即联网核实」:所有产业专家都经能力网关先核实事实与数据。
            web = True
            enabled_caps_count = sum(
                1 for cap in caps if isinstance(cap, dict) and cap.get("enabled")
            )
        # 老板要求每单任务都必须实际调用能力与技能：这里把装载量作为
        # 可见凭证写进任务步骤，老板打开任意任务都能核对。
        enabled_skill_count = sum(
            1 for skill in (skills or [])
            if isinstance(skill, dict) and skill.get("enabled", True)
        )
        progress(
            "tool",
            f"已装载批准能力包 r{cfg.get('config_revision')}："
            f"启用能力 {enabled_caps_count} 项 · 进修技能 {enabled_skill_count} 条，"
            "已全部注入本单执行上下文",
        )
        # Both core and industry employees execute the exact approved effective
        # bundle.  Keeping these named values explicit makes it impossible for
        # a future refactor to silently fall back to static profile data.
        if not isinstance(effective_profile, dict) or not isinstance(skills, list):
            raise ValueError("员工有效岗位档案结构无效")
        if not isinstance(capabilities, (list, dict)) or not isinstance(
            workflow, (str, list, dict)
        ):
            raise ValueError("员工有效能力或工作流程结构无效")
        prompt = _with_approved_role_context(prompt, effective_role)
    except Exception as exc:
        log.error(
            "task %s preparation failed error_type=%s",
            task_id,
            type(exc).__name__,
        )
        public_error = providers.public_failure_message(exc)
        try:
            await db.arun(settle_failure, task_id, public_error)
            progress("error", public_error)
        finally:
            await cleanup()
        return
    try:
        r = await providers.call_text(
            idx,
            prompt.user,
            web=web,
            timeout=900,
            progress=progress,
            token=f"task{task_id}:",
            system_prompt=prompt.system,
            research_brief=prompt.research,
            sensitive_texts=prompt.sensitive,
            identity_ref=cfg["identity_ref"],
            config_revision=cfg["config_revision"],
            config_sha256=cfg["config_sha256"],
            bundle_sha256=cfg["bundle_sha256"],
        )
        md = (r["text"] or "").strip()
        md, extra_cost = await _enforce_length(
            idx, md, brief, progress, config=cfg
        )
        title = next((ln.lstrip("# ").strip() for ln in md.splitlines()
                      if ln.startswith("#")), brief.get("direction", "")[:30])
        # V2 决策员工在模型和篇幅压缩之后、数据库事务之前经过纯函数门禁。
        # 门禁只改交付正文（必要时将状态降为 HOLD 并保留原文），不增加模型
        # 调用、不改变成本/错误结算，也不授予任何自动执行权限；V1 原样通过。
        decision_gate = departments.enforce_decision_output(
            e, md, provenance=decision_provenance
        )
        if decision_gate["is_decision"]:
            md = decision_gate["output"]
            if decision_gate["status"] == "HOLD":
                progress("review", "决策机器门禁：HOLD · 保留原始输出，等待人工复核")
        now = time.time()
        def _commit_delivery():
            with db.atomic() as connection:
                changed = connection.execute(
                    "UPDATE task SET status='done',output_md=?,cost_usd=?,tokens=?,"
                    "steps_json=?,billing_status=CASE WHEN billing_status='charged' "
                    "THEN 'succeeded' ELSE billing_status END,terminal_at=?,updated_at=? "
                    "WHERE id=? AND status='running' "
                    "AND billing_status IN ('charged','included') "
                    "AND deleted_at IS NULL",
                    (
                        md,
                        r["cost_usd"] + extra_cost,
                        r["tokens"],
                        json.dumps(steps, ensure_ascii=False),
                        now,
                        now,
                        task_id,
                    ),
                )
                if changed.rowcount != 1:
                    return False
                connection.execute(
                    "INSERT INTO asset(type,tenant_id,payload_json,created_at,updated_at) "
                    "VALUES('report',?,?,?,?)",
                    (
                        t.get("tenant_id") or 1,
                        json.dumps(
                            {
                                "title": title,
                                "emp": e["name"],
                                "dept": e["dept_name"],
                                "group": e["group"],
                                "task_id": task_id,
                                "brief": brief.get("direction", ""),
                            },
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                    ),
                )
                return True

        if await db.arun(_commit_delivery):
            md_done = md
        if md_done:
            progress("done", f"交付完成 · ${r['cost_usd']:.3f}")
    except llm.LLMError as ex:
        public_error = providers.public_failure_message(ex)
        await db.arun(settle_failure, task_id, public_error)
        progress("error", public_error)
    except Exception as exc:
        log.error(
            "task %s failed error_type=%s",
            task_id,
            type(exc).__name__,
        )
        public_error = providers.public_failure_message(exc)
        await db.arun(settle_failure, task_id, public_error)
        progress("error", public_error)
    finally:
        await cleanup()
    # 任务已交付(done)后再补「老板速览」——绝不拖慢交付感知,失败也不回退状态
    if md_done and len(md_done) > DIGEST_MIN_CHARS:
        await _gen_summary(
            task_id,
            md_done,
            t["emp_idx"],
            t.get("tenant_id") or 1,
            broadcast,
            employee=e,
            decision_gate=decision_gate,
            config=cfg,
        )


DIGEST_MIN_CHARS = 1500

# 篇幅硬保障:提示词软约束压不住 6000 字岗位手册的输出惯性,超过容忍线就再走一道压缩。
# 目标 lite≈800 / std≈2000(与UI口径一致),容忍到 1.3 倍;压缩失败保留原文(宁长勿缺)。
_LEN_TARGET = {"lite": 800, "std": 2000}


async def _enforce_length(
    idx: int, md: str, brief: dict, progress, *, config: dict | None = None,
) -> tuple:
    """返回 (md, 压缩产生的平台LLM成本) —— 成本记进 task.cost_usd,老板产出总览才看得到真实账."""
    target = _LEN_TARGET.get((brief or {}).get("length") or "")
    cost = 0.0
    if not target:
        return md, cost
    for _ in range(2):                          # 一道压不到位就再压一道(输入变短后第二道容易压准)
        if len(md) <= int(target * 1.3):
            return md, cost
        try:
            from . import providers
            progress("run", f"按老板要求压缩篇幅(→{target}字内)…")
            prompt = (
                f"把下面这份交付物压缩到 {int(target*0.85)} 字以内,这是硬性上限,宁可再短也不许超。\n"
                "保留:标题、关键数据和数字、结论、可执行步骤、必要的表格(可精简行列)、"
                "结尾的「下一步建议」;删掉:铺垫、论证过程、重复内容、可有可无的展开。\n"
                "保持 Markdown 结构,开头一行仍是「# 标题」。只输出压缩后的 Markdown,不要任何说明。\n\n"
                + md)
            identity_args = ({
                "identity_ref": config["identity_ref"],
                "config_revision": config["config_revision"],
                "config_sha256": config["config_sha256"],
                "bundle_sha256": config["bundle_sha256"],
            } if config else {})
            r = await providers.call_text(
                idx, prompt, web=False, timeout=300, **identity_args
            )
            cost += r.get("cost_usd") or 0
            short = (r["text"] or "").strip()
            # 压缩结果要像样:非空、确实变短、没有腰斩(至少给到目标的三成)
            if short.startswith("#") and target * 0.3 <= len(short) < len(md):
                md = short
                continue
        except Exception as exc:
            log.debug(
                "篇幅压缩跳过 task idx=%s error_type=%s",
                idx,
                type(exc).__name__,
            )
        break                                   # 压缩失败/结果不像样:保留现状,宁长勿缺
    return md, cost


def _decision_summary_lines(
    md: str,
    employee: dict,
    decision_gate: dict | None = None,
) -> list[str]:
    """从已门禁正文生成 V2 摘要，不再让二次模型改写安全字段。"""
    if decision_gate is not None:
        gate = decision_gate
    else:
        # 任务重启或独立补摘要时正文可能已经带有门禁头。只重审“原始输出”
        # 段，避免门禁头的 HOLD 与原始 GO 被当成两个冲突状态。
        raw_md = md
        marker = "## 原始输出（人工复核）"
        if marker in raw_md:
            raw_md = raw_md.split(marker, 1)[1].lstrip()
        gate = departments.enforce_decision_output(employee, raw_md)
    if not gate.get("is_decision"):
        return []
    contract = departments.decision_output_contract(employee) or {}
    source_md = md
    marker = "## 原始输出（人工复核）"
    if marker in source_md:
        source_md = source_md.split(marker, 1)[1].lstrip()
    gap_sections = departments._decision_field_sections(source_md, "data_gaps")
    gap_state = departments._decision_gap_state(gap_sections)
    gap_detail = " ".join(" ".join(str(item).split()) for item in gap_sections).strip()
    if gap_state == "none":
        gap_line = "已声明无未闭合数据缺口（仍需人工复核）"
    elif gap_state == "present":
        gap_line = f"存在未闭合数据缺口：{gap_detail[:320] or '见原始输出'}"
    else:
        gap_line = "数据缺口声明缺失，必须补齐后重审"
    approval = str(contract.get("approval_boundary") or "未加载有效审批边界；必须人工复核").strip()
    forbidden = "；".join(str(item).strip() for item in contract.get("forbidden_actions") or () if str(item).strip())
    if not forbidden:
        forbidden = "不得执行任何业务写操作"
    lines = [
        f"- 决策状态：{gate.get('status') or 'HOLD'}",
        "- 人工审批语义：GO 仅表示可进入人工审批，不代表允许系统自动执行任何业务写操作。",
        "- 用户提交覆盖：" + str(
            gate.get("coverage_text")
            or "覆盖状态不可用；内容未核验"
        ),
        f"- 数据缺口：{gap_line}",
        f"- 审批边界：{approval}",
        f"- 禁止动作：{forbidden}",
    ]
    reasons = [str(reason).strip() for reason in gate.get("reasons") or () if str(reason).strip()]
    if reasons:
        lines.append(f"- 门禁原因：{'；'.join(dict.fromkeys(reasons))}")
    return lines


async def _gen_summary(
    task_id: int,
    md: str,
    idx: int,
    tid: int,
    broadcast,
    employee: dict | None = None,
    decision_gate: dict | None = None,
    config: dict | None = None,
):
    """给「很忙的老板」补一张十秒读完的速览卡:3-5 条要点 + 一句话行动建议。

    全程 try/except,失败静默(summary_md 留空),绝不影响已交付的产出;
    走通用文本模型(非联网),不额外扣用户点数。
    """
    try:
        expert = employee or employeeidentity.any_employee(idx)
        if departments.is_decision_employee(expert):
            # V2 决策摘要必须继承正文门禁结果，完全绕开普通摘要模型，
            # 防止二次模型把 HOLD 改写成 GO 或丢失禁止动作/审批边界。
            lines = _decision_summary_lines(md, expert, decision_gate=decision_gate)
            if not lines:
                return
            cur = await db.aone(
                "SELECT output_md, cost_usd FROM task WHERE id=?", (task_id,)
            )
            if not cur or (cur.get("output_md") or "").strip() != md.strip():
                return
            await db.aupdate(
                "task",
                task_id,
                {"summary_md": "\n".join(lines)},
            )
            await db.arun(sync_meeting_delivery_for_task, task_id)
            broadcast({
                "type": "task_update",
                "tenant_id": tid,
                "_required_modules": (str((expert or {}).get("dept_key") or "content"),),
                "task_id": task_id,
                "idx": idx,
            })
            return
        from . import providers
        prompt = (
            "你是给「很忙的老板」做速览的助理。下面是数字员工刚交付的完整产出(Markdown)。\n"
            "请把它压缩成手机上十秒能读完的「老板速览」:\n"
            "1)3-5 条要点,每条不超过 40 字,必须具体到数字 / 动作 / 结论,不要空话套话;\n"
            "2)再单独给一句可直接落地的行动建议(告诉老板下一步该干什么)。\n"
            '只输出 JSON:{"points":["要点", ...], "action":"一句话行动建议"}\n\n'
            "【完整产出】\n" + md[:8000])
        identity_args = ({
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        } if config else {})
        r = await providers.call_text_json(
            idx, prompt, web=False, timeout=240, **identity_args
        )
        data = r.get("data") or {}
        points = [str(p).strip()[:60] for p in (data.get("points") or []) if str(p).strip()][:5]
        action = str(data.get("action") or "").strip()[:80]
        if not points:
            return
        lines = [f"- {p}" for p in points]
        if action:
            lines.append(f"- 👉 **一句话行动建议**:{action}")
        # 生成期间(~几十秒)老板可能已手动编辑过正文:重读比对,变了就放弃,不给编辑后的正文盖旧速览
        cur = await db.aone(
            "SELECT output_md, cost_usd FROM task WHERE id=?", (task_id,)
        )
        if not cur or (cur.get("output_md") or "").strip() != md.strip():
            return
        await db.aupdate(
            "task",
            task_id,
            {
                "summary_md": "\n".join(lines),
                "cost_usd": (
                    (cur.get("cost_usd") or 0) + (r.get("cost_usd") or 0)
                ),
            },
        )
        await db.arun(sync_meeting_delivery_for_task, task_id)
        expert = employeeidentity.any_employee(idx)
        broadcast({
            "type": "task_update",
            "tenant_id": tid,
            "_required_modules": (
                str((expert or {}).get("dept_key") or "content"),
            ),
            "task_id": task_id,
            "idx": idx,
        })
    except Exception as exc:
        log.debug(
            "老板速览生成跳过 task=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )


def resume_pending(broadcast):
    """服务重启后:running 置回 queued 并重新拉起."""
    # pending_charge/pending 还未扣点；进程在“落任务→原子扣点”之间退出时清掉空壳。
    db.q(
        "DELETE FROM task WHERE status='pending_charge' "
        "AND billing_status='pending' AND deleted_at IS NULL"
    )
    for row in db.q(
        "SELECT id FROM task WHERE status='failed' AND billing_status='charged' "
        "AND deleted_at IS NULL"
    ):
        settle_failure(row["id"], "服务启动补做失败结算")
    for r in db.q(
        "SELECT id FROM task WHERE status IN ('queued','running') "
        "AND billing_status IN ('charged','included') AND deleted_at IS NULL "
        "AND emp_idx!=10"
    ):
        db.update("task", r["id"], {"status": "queued", "terminal_at": None})
        asyncio.create_task(run_task(r["id"], broadcast))
