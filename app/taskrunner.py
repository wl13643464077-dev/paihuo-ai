"""专家任务引擎(V5):行业部门数字员工的单人工单.

与内容部流水线互不干扰:一个任务 = 一名专家 + 一份任务书 → 一份 Markdown 交付物。
全程联网检索,步骤实时 SSE(type=task_step),交付物自动入资产库(type=report)。
"""
import asyncio
import json
import logging
import re
import time

from . import billing, db, departments, employees, llm, providers

log = logging.getLogger("taskrunner")

RUNNING: set = set()   # task_id 正在执行
MAX_FREE_RETRIES = 3
_MEETING_DELIVERY_START = "<!-- execution-deliveries:start -->"
_MEETING_DELIVERY_END = "<!-- execution-deliveries:end -->"


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

    result = {
        "direction": text("direction", 2000, required=True),
    }
    for field, limit in (
        ("industry", 120),
        ("material", 12000),
        ("feedback", 2000),
        ("prev_excerpt", 1500),
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
            "UPDATE task SET status='running',summary_md=NULL,updated_at=? "
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
        "SELECT tenant_id,emp_idxs_json,actions_json,consensus_md,summary_md "
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

    member_idxs = set()
    for raw_idx in db.jloads(meeting["emp_idxs_json"], []) or []:
        try:
            member_idxs.add(int(raw_idx))
        except (TypeError, ValueError):
            continue
    if not member_idxs:
        return None

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
        "source_action_key FROM task WHERE source_meeting_id=? AND tenant_id=? "
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
        employee = departments.get(row["emp_idx"]) or registry.BY_IDX.get(
            row["emp_idx"]) or {}
        name = employee.get("name") or f"员工 {row['emp_idx']}"
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
            changed = connection.execute(
                "UPDATE task SET status='failed',billing_status='refunded',"
                "output_md=?,updated_at=? "
                "WHERE id=? AND billing_status='charged' "
                "AND status IN ('pending_charge','queued','running','failed')",
                (text, time.time(), task_id),
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
        changed = connection.execute(
            "UPDATE task SET status='failed',output_md=?,updated_at=? "
            "WHERE id=? AND billing_status='included' "
            "AND status IN ('queued','running')",
            (text, time.time(), task_id),
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
    t = db.one(
        "SELECT * FROM task WHERE id=? AND deleted_at IS NULL", (task_id,)
    )
    if not t or t["status"] != "queued":
        return
    idx = t["emp_idx"]
    e = departments.get(idx)
    solo_content = e is None and idx in registry.BY_IDX
    if not e and not solo_content:
        settle_failure(task_id, "员工不存在")
        return
    if solo_content:
        s = registry.BY_IDX[idx]
        e = {"name": s["name"], "dept_name": "内容生产部", "group": s["dept"]}
    t = _claim_task(task_id)
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
            {"type": "task_step", "task_id": task_id, "idx": t["emp_idx"],
             "n": len(steps), "step": steps[-1]},
        )
        if kind != "typing" or now - st["save"] > 3:
            db.execute(
                "UPDATE task SET steps_json=?,updated_at=? "
                "WHERE id=? AND status='running' AND deleted_at IS NULL",
                (json.dumps(steps, ensure_ascii=False), now, task_id),
            )
            st["save"] = now

    def cleanup():
        RUNNING.discard(task_id)
        try:
            sync_meeting_delivery_for_task(task_id)
        except Exception as exc:
            log.error(
                "task %s meeting delivery sync failed error_type=%s",
                task_id,
                type(exc).__name__,
            )
        _broadcast_safely(
            broadcast,
            {"type": "task_update", "task_id": task_id, "idx": t["emp_idx"]},
        )

    _broadcast_safely(
        broadcast,
        {"type": "task_update", "task_id": task_id, "idx": t["emp_idx"]},
    )
    md_done = None
    try:
        brief = normalize_brief(db.jloads(t["brief_json"], None))
        cfg = employees.get_config(idx)
        ctx_text, recall = _context_text(
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
                idx, brief, employees.skills_block(idx), ctx_text
            )
            web = registry.BY_IDX[idx]["key"] in (
                "trend", "research", "benchmark"
            )
        else:
            caps = departments.capabilities_for(idx, cfg.get("caps_off"))
            prompt = departments.build_task_prompt(
                e, brief, employees.skills_block(idx), ctx_text, caps,
                private_template=cfg.get("prompt_template"),
            )
            # 产品承诺「派活即联网核实」:所有产业专家都经能力网关先核实事实与数据。
            web = True
    except Exception as exc:
        log.error(
            "task %s preparation failed error_type=%s",
            task_id,
            type(exc).__name__,
        )
        public_error = providers.public_failure_message(exc)
        try:
            settle_failure(task_id, public_error)
            progress("error", public_error)
        finally:
            cleanup()
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
        )
        md = (r["text"] or "").strip()
        md, extra_cost = await _enforce_length(idx, md, brief, progress)
        title = next((ln.lstrip("# ").strip() for ln in md.splitlines()
                      if ln.startswith("#")), brief.get("direction", "")[:30])
        now = time.time()
        with db.atomic() as connection:
            changed = connection.execute(
                "UPDATE task SET status='done',output_md=?,cost_usd=?,tokens=?,"
                "steps_json=?,billing_status=CASE WHEN billing_status='charged' "
                "THEN 'succeeded' ELSE billing_status END,updated_at=? "
                "WHERE id=? AND status='running' "
                "AND billing_status IN ('charged','included') "
                "AND deleted_at IS NULL",
                (
                    md,
                    r["cost_usd"] + extra_cost,
                    r["tokens"],
                    json.dumps(steps, ensure_ascii=False),
                    now,
                    task_id,
                ),
            )
            if changed.rowcount == 1:
                connection.execute(
                    "INSERT INTO asset(type,tenant_id,payload_json,created_at,updated_at) "
                    "VALUES('report',?,?,?,?)",
                    (
                        t.get("tenant_id") or 1,
                        json.dumps(
                            {"title": title, "emp": e["name"], "dept": e["dept_name"],
                             "group": e["group"], "task_id": task_id,
                             "brief": brief.get("direction", "")},
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                    ),
                )
                md_done = md
        if md_done:
            progress("done", f"交付完成 · ${r['cost_usd']:.3f}")
    except llm.LLMError as ex:
        public_error = providers.public_failure_message(ex)
        settle_failure(task_id, public_error)
        progress("error", public_error)
    except Exception as exc:
        log.error(
            "task %s failed error_type=%s",
            task_id,
            type(exc).__name__,
        )
        public_error = providers.public_failure_message(exc)
        settle_failure(task_id, public_error)
        progress("error", public_error)
    finally:
        cleanup()
    # 任务已交付(done)后再补「老板速览」——绝不拖慢交付感知,失败也不回退状态
    if md_done and len(md_done) > DIGEST_MIN_CHARS:
        await _gen_summary(task_id, md_done, t["emp_idx"], t.get("tenant_id") or 1, broadcast)


DIGEST_MIN_CHARS = 1500

# 篇幅硬保障:提示词软约束压不住 6000 字岗位手册的输出惯性,超过容忍线就再走一道压缩。
# 目标 lite≈800 / std≈2000(与UI口径一致),容忍到 1.3 倍;压缩失败保留原文(宁长勿缺)。
_LEN_TARGET = {"lite": 800, "std": 2000}


async def _enforce_length(idx: int, md: str, brief: dict, progress) -> tuple:
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
            r = await providers.call_text(idx, prompt, web=False, timeout=300)
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


async def _gen_summary(task_id: int, md: str, idx: int, tid: int, broadcast):
    """给「很忙的老板」补一张十秒读完的速览卡:3-5 条要点 + 一句话行动建议。

    全程 try/except,失败静默(summary_md 留空),绝不影响已交付的产出;
    走通用文本模型(非联网),不额外扣用户点数。
    """
    try:
        from . import providers
        prompt = (
            "你是给「很忙的老板」做速览的助理。下面是数字员工刚交付的完整产出(Markdown)。\n"
            "请把它压缩成手机上十秒能读完的「老板速览」:\n"
            "1)3-5 条要点,每条不超过 40 字,必须具体到数字 / 动作 / 结论,不要空话套话;\n"
            "2)再单独给一句可直接落地的行动建议(告诉老板下一步该干什么)。\n"
            '只输出 JSON:{"points":["要点", ...], "action":"一句话行动建议"}\n\n'
            "【完整产出】\n" + md[:8000])
        r = await providers.call_text_json(idx, prompt, web=False, timeout=240)
        data = r.get("data") or {}
        points = [str(p).strip()[:60] for p in (data.get("points") or []) if str(p).strip()][:5]
        action = str(data.get("action") or "").strip()[:80]
        if not points:
            return
        lines = [f"- {p}" for p in points]
        if action:
            lines.append(f"- 👉 **一句话行动建议**:{action}")
        # 生成期间(~几十秒)老板可能已手动编辑过正文:重读比对,变了就放弃,不给编辑后的正文盖旧速览
        cur = db.one("SELECT output_md, cost_usd FROM task WHERE id=?", (task_id,))
        if not cur or (cur.get("output_md") or "").strip() != md.strip():
            return
        db.update("task", task_id, {"summary_md": "\n".join(lines),
                                    "cost_usd": (cur.get("cost_usd") or 0) + (r.get("cost_usd") or 0)})
        sync_meeting_delivery_for_task(task_id)
        broadcast({"type": "task_update", "task_id": task_id, "idx": idx})
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
        "AND billing_status IN ('charged','included') AND deleted_at IS NULL"
    ):
        db.update("task", r["id"], {"status": "queued"})
        asyncio.create_task(run_task(r["id"], broadcast))
