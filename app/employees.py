"""数字员工层：人员在岗状态 + 版本化岗位配置 + 全网进修.

- 提示词：按 identity_ref 与修订号存 employee_role_config，历史任务始终可重放；
- 在岗状态：按真实人员 idx 存 employee_slot，不再和岗位版本混用；
  模板里 {占位符} 由工位在运行时注入(未知占位符原样保留,不会崩)。
- 技能库：只允许当前岗位“全网进修”，联网检索岗位最新方法论/平台规则/工具技巧，
  沉淀为技能卡(可停用/删除),启用中的技能自动注入该员工的工作提示词。
"""
import asyncio
import json
import logging
import re
import time

from . import db, llm

log = logging.getLogger("employees")

LEARNING: set = set()          # 正在进修中的工位 idx
MAX_SKILLS_IN_PROMPT = 12      # 注入提示词的技能条数上限

# ---- 员工自动进化:验收沉淀的实战心得(存 app_setting,无 schema 变更) ----
INSIGHT_PENDING_MAX = 20       # 待老板拍板的提案上限
INSIGHT_ADOPTED_MAX = 12       # 注入提示词的已采纳心得上限


def _insight_setting_key(kind: str, tenant: int, idx: int) -> str:
    return f"emp_insights_{kind}:{int(tenant)}:{int(idx)}"


def insight_lists(tenant: int, idx: int) -> dict:
    """返回 {pending:[...], adopted:[...]};损坏数据按空处理。"""
    out = {}
    for kind in ("pending", "adopted"):
        rows = db.jloads(
            db.get_setting(_insight_setting_key(kind, tenant, idx)) or "[]", [],
        )
        out[kind] = rows if isinstance(rows, list) else []
    return out


def save_insights(kind: str, tenant: int, idx: int, rows: list):
    cap = INSIGHT_PENDING_MAX if kind == "pending" else INSIGHT_ADOPTED_MAX
    db.set_setting(
        _insight_setting_key(kind, tenant, idx),
        json.dumps(rows[-cap:], ensure_ascii=False),
    )


def adopted_insights_text(tenant: int, idx: int) -> str:
    """已采纳实战心得 → 注入岗位 system 的文本块(taskrunner 派活时调用)."""
    lines = [str(row.get("insight") or "").strip()
             for row in insight_lists(tenant, idx)["adopted"]
             if isinstance(row, dict)]
    lines = [line for line in lines if line][:INSIGHT_ADOPTED_MAX]
    return "\n".join(f"- {line}" for line in lines)
MAX_SKILLS_CHARS = 2400        # 注入提示词的技能总字数上限


def claim_learning(idx: int) -> bool:
    """Atomically reserve one employee's learning slot on the event loop."""
    if idx in LEARNING:
        return False
    LEARNING.add(idx)
    return True


# ---------------- 配置读写(schema54) ----------------
_ROLE_COLUMNS = (
    "identity_ref", "idx", "employee_key", "employee_catalog_version",
    "employee_name_snapshot", "employee_dept_key", "employee_spec_sha256",
    "person_snapshot", "identity_scheme",
    "prompt_template", "skills_json", "learned_at", "settings_json",
    "caps_off_json", "model_text", "model_image", "config_revision",
    "professional_profile_json", "config_sha256", "archived_at",
    "created_at", "updated_at",
)


def _row_to_config(idx: int, row, *, enabled=True) -> dict | None:
    if not row:
        return None
    clean = db.normalize_employee_config(row)
    identity_ref = str(row["identity_ref"])
    revision = int(row.get("config_revision") or 0)
    config_sha256 = str(row.get("config_sha256") or "")
    bundle = db.get_employee_role_bundle(
        identity_ref, revision, config_sha256,
    )
    if not bundle:
        return None
    effective = bundle.get("effective") or {}
    effective_profile = (
        effective.get("professional_profile")
        if isinstance(effective.get("professional_profile"), dict)
        else clean["professional_profile"]
    )
    effective_workflow = (
        effective.get("workflow")
        if isinstance(effective.get("workflow"), list)
        else []
    )
    return {
        "idx": int(idx),
        "identity_ref": identity_ref,
        "employee_key": str(row.get("employee_key") or ""),
        "employee_catalog_version": str(
            row.get("employee_catalog_version") or ""
        ),
        "person_snapshot": str(row.get("person_snapshot") or ""),
        "identity_scheme": str(
            row.get("identity_scheme") or "legacy-six"
        ),
        "prompt_template": clean["prompt_template"],
        "skills": clean["skills"],
        "learned_at": clean["learned_at"],
        "settings": clean["settings"],
        "caps_off": clean["caps_off"],
        "model_text": clean["model_text"],
        "model_image": clean["model_image"],
        "professional_profile": clean["professional_profile"],
        "effective_profile": effective_profile,
        "effective_workflow": effective_workflow,
        "learning_evidence": (
            effective.get("learning_evidence")
            if isinstance(effective.get("learning_evidence"), list) else []
        ),
        "config_revision": revision,
        "config_sha256": config_sha256,
        "bundle_sha256": str(bundle.get("bundle_sha256") or ""),
        "role_bundle": bundle,
        "archived_at": row.get("archived_at"),
        "enabled": bool(enabled),
    }


def _same_identity(row, frozen: dict) -> bool:
    matches = all(
        str(row.get(column) if isinstance(row, dict) else row[column])
        == str(expected)
        for column, expected in (
            ("idx", frozen["idx"]),
            ("employee_key", frozen["key"]),
            ("employee_catalog_version", frozen["catalog_version"]),
            ("employee_name_snapshot", frozen["name"]),
            ("employee_dept_key", frozen["dept_key"]),
            ("employee_spec_sha256", frozen["spec_sha256"]),
        )
    )
    if not matches:
        return False
    if frozen.get("identity_scheme") == "v2-person":
        getter = row.get if isinstance(row, dict) else row.__getitem__
        return (
            str(getter("person_snapshot") or "")
            == str(frozen.get("person_snapshot") or "")
            and str(getter("identity_scheme") or "") == "v2-person"
        )
    return True


def ensure_role_config(employee: dict) -> dict:
    """Ensure exactly one current config row for an exact role identity.

    New V3 roles always start from empty defaults.  This function never copies
    config from another identity sharing the same numeric person slot.
    """
    from . import employeeidentity

    frozen = employeeidentity.snapshot(employee)
    identity = employeeidentity.identity_ref(frozen)
    now = time.time()
    active = employeeidentity.active_employee(frozen["idx"])
    is_current = bool(
        active and employeeidentity.snapshot(active) == frozen
    )
    with db.atomic() as connection:
        row = connection.execute(
            "SELECT * FROM employee_role_config WHERE identity_ref=?",
            (identity,),
        ).fetchone()
        if row is None:
            # Only unchanged core identities and the never-active 20xxx V2
            # archive may inherit their schema53 idx-keyed row lazily. Reused
            # 1001-1936 V3 person slots must always start with empty custom
            # prompt/skills/settings/models; their old row belongs to V1.
            legacy_seed = None
            if (
                frozen["dept_key"] == "content"
                or frozen["catalog_version"] == "2026.08.v2"
            ):
                legacy_seed = connection.execute(
                    "SELECT * FROM employee_config WHERE idx=?",
                    (frozen["idx"],),
                ).fetchone()
            clean = db.normalize_employee_config({
                **(dict(legacy_seed) if legacy_seed else {}),
                "professional_profile": employee.get("professional_profile") or {},
            })
            revision = 1
            config_hash = db.employee_config_sha256(identity, revision, clean)
            connection.execute(
                "INSERT INTO employee_role_config("
                "identity_ref,idx,employee_key,employee_catalog_version,"
                "employee_name_snapshot,employee_dept_key,employee_spec_sha256,"
                "person_snapshot,identity_scheme,"
                "prompt_template,skills_json,learned_at,settings_json,caps_off_json,"
                "model_text,model_image,professional_profile_json,"
                "config_revision,config_sha256,archived_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity, frozen["idx"], frozen["key"],
                    frozen["catalog_version"], frozen["name"],
                    frozen["dept_key"], frozen["spec_sha256"],
                    frozen.get("person_snapshot", ""),
                    frozen.get("identity_scheme", "legacy-six"),
                    clean["prompt_template"], clean["skills_json"],
                    clean["learned_at"], clean["settings_json"],
                    clean["caps_off_json"], clean["model_text"],
                    clean["model_image"], clean["professional_profile_json"],
                    revision, config_hash,
                    None if is_current else now, now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM employee_role_config WHERE identity_ref=?",
                (identity,),
            ).fetchone()
            if row is not None:
                bundle_identity = {
                    **frozen,
                    "professional_profile": employee.get("professional_profile") or {},
                    "decision_contract": employee.get("decision_contract") or {},
                    "workflow": employee.get("workflow") or [],
                    "outputs": employee.get("outputs") or [],
                }
                db._schema55_insert_bundle(
                    connection, bundle_identity, dict(row)
                )
        if not row or not db.employee_role_config_row_valid(row):
            raise RuntimeError("员工岗位配置完整性校验失败")
        if not _same_identity(row, frozen):
            raise RuntimeError("员工身份引用与配置档案不一致")
        expected_profile = db.normalize_employee_config({
            "professional_profile": employee.get("professional_profile") or {},
        })["professional_profile"]
        stored_profile = db.normalize_employee_config(dict(row))[
            "professional_profile"
        ]
        if stored_profile != expected_profile:
            raise RuntimeError("岗位专业档案与身份版本不一致")
        if is_current:
            connection.execute(
                "INSERT INTO employee_slot(idx,active_identity_ref,enabled,"
                "row_version,created_at,updated_at) VALUES(?,?,1,1,?,?) "
                "ON CONFLICT(idx) DO UPDATE SET "
                "active_identity_ref=excluded.active_identity_ref,"
                "row_version=CASE WHEN employee_slot.active_identity_ref IS "
                "excluded.active_identity_ref THEN employee_slot.row_version "
                "ELSE employee_slot.row_version+1 END,updated_at=excluded.updated_at",
                (frozen["idx"], identity, now, now),
            )
        slot = connection.execute(
            "SELECT enabled FROM employee_slot WHERE idx=?", (frozen["idx"],)
        ).fetchone()
        return _row_to_config(
            frozen["idx"], dict(row),
            enabled=(slot is None or int(slot["enabled"] or 0) != 0),
        )


def get_config_by_identity(
    identity_ref: str,
    *,
    revision: int | None = None,
    config_sha256: str | None = None,
) -> dict | None:
    identity_ref = str(identity_ref or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", identity_ref):
        return None
    args: list = [identity_ref]
    if revision is None:
        row = db.one(
            "SELECT * FROM employee_role_config WHERE identity_ref=?", args
        )
    else:
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            return None
        row = db.one(
            "SELECT * FROM employee_role_config WHERE identity_ref=? "
            "AND config_revision=?",
            (identity_ref, revision),
        )
        if not row:
            row = db.one(
                "SELECT * FROM employee_role_config_history "
                "WHERE identity_ref=? AND config_revision=?",
                (identity_ref, revision),
            )
    if not row:
        return None
    if not db.employee_role_config_row_valid(row):
        return None
    if config_sha256 is not None and str(row.get("config_sha256") or "") != str(
        config_sha256
    ):
        return None
    # An identity/revision lookup is an immutable historical read.  Do not
    # decorate it with the mutable current person-slot state: task/meeting
    # execution must not acquire any hidden idx -> current dependency.
    return _row_to_config(row["idx"], row, enabled=True)


def get_config(
    idx: int,
    *,
    identity_ref: str | None = None,
    revision: int | None = None,
    config_sha256: str | None = None,
) -> dict:
    if identity_ref is not None:
        return get_config_by_identity(
            identity_ref, revision=revision, config_sha256=config_sha256
        )
    from . import employeeidentity
    employee = employeeidentity.active_employee(idx)
    if not employee:
        return {
            "idx": int(idx), "identity_ref": None, "prompt_template": None,
            "skills": [], "learned_at": None, "settings": {}, "caps_off": [],
            "model_text": None, "model_image": None, "config_revision": 0,
            "professional_profile": {}, "config_sha256": None,
            "enabled": is_enabled(idx),
        }
    return ensure_role_config(employee)


def get_configs(idxs) -> dict:
    """Return current-role config for each requested real employee slot."""
    result = {}
    for raw_idx in dict.fromkeys(int(value) for value in idxs):
        config = get_config(raw_idx)
        if config.get("identity_ref"):
            result[raw_idx] = config
    return result


def _upsert_identity(
    identity_ref: str, data: dict, *, expected_revision: int,
):
    allowed = {
        "prompt_template", "skills_json", "learned_at", "settings_json",
        "caps_off_json", "model_text", "model_image",
    }
    if not data or not set(data) <= allowed:
        raise ValueError("员工岗位配置字段无效")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise ValueError("员工岗位配置修订号必填且必须有效")
    with db.atomic() as connection:
        row_value = connection.execute(
            "SELECT * FROM employee_role_config WHERE identity_ref=?",
            (identity_ref,),
        ).fetchone()
        if not row_value:
            raise ValueError("员工岗位配置档案不存在")
        row = dict(row_value)
        if not db.employee_role_config_row_valid(row):
            raise RuntimeError("员工岗位配置完整性校验失败")
        slot = connection.execute(
            "SELECT active_identity_ref FROM employee_slot WHERE idx=?",
            (int(row["idx"]),),
        ).fetchone()
        if (
            row.get("archived_at") is not None
            or not slot
            or str(slot["active_identity_ref"] or "") != str(identity_ref)
        ):
            raise ValueError("历史员工岗位配置只读，不可修改")
        if expected_revision != int(row["config_revision"]):
            raise RuntimeError("员工岗位配置已更新，请刷新后再保存")
        bundle_value = connection.execute(
            "SELECT * FROM employee_role_bundle_revision "
            "WHERE identity_ref=? AND config_revision=? AND config_sha256=?",
            (
                identity_ref, row["config_revision"],
                row["config_sha256"],
            ),
        ).fetchone()
        if not bundle_value or not db.employee_role_bundle_row_valid(bundle_value):
            raise RuntimeError("员工岗位 role bundle 完整性校验失败")
        old_bundle = dict(bundle_value)
        now = time.time()
        history_columns = list(_ROLE_COLUMNS) + ["superseded_at"]
        connection.execute(
            f"INSERT INTO employee_role_config_history({','.join(history_columns)}) "
            f"VALUES({','.join('?' for _ in history_columns)})",
            [row[column] for column in _ROLE_COLUMNS] + [now],
        )
        merged = {**row, **data}
        clean = db.normalize_employee_config(merged)
        revision = int(row["config_revision"]) + 1
        config_hash = db.employee_config_sha256(identity_ref, revision, clean)
        changed = connection.execute(
            "UPDATE employee_role_config SET prompt_template=?,skills_json=?,"
            "learned_at=?,settings_json=?,caps_off_json=?,model_text=?,"
            "model_image=?,professional_profile_json=?,config_revision=?,"
            "config_sha256=?,updated_at=? "
            "WHERE identity_ref=? AND config_revision=?",
            (
                clean["prompt_template"], clean["skills_json"],
                clean["learned_at"], clean["settings_json"],
                clean["caps_off_json"], clean["model_text"],
                clean["model_image"], clean["professional_profile_json"],
                revision, config_hash, now,
                identity_ref, row["config_revision"],
            ),
        )
        if changed.rowcount != 1:
            raise RuntimeError("员工岗位配置已被其他请求更新")
        try:
            baseline = json.loads(old_bundle["baseline_json"])
            effective = json.loads(old_bundle["effective_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("员工岗位 role bundle 无法解析") from exc
        if not isinstance(baseline, dict) or not isinstance(effective, dict):
            raise RuntimeError("员工岗位 role bundle 结构无效")
        # Mutable prompt/model/settings/skill changes advance the exact role
        # bundle revision in the same transaction.  Approved learning overlays
        # already present in ``effective`` remain intact; only the compatible
        # config snapshot is replaced.
        baseline = {**baseline, "config": clean}
        effective = {**effective, "config": clean}
        baseline_json = json.dumps(
            baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        effective_json = json.dumps(
            effective, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        bundle_row = {
            **old_bundle,
            "config_revision": revision,
            "config_sha256": config_hash,
            "baseline_json": baseline_json,
            "effective_json": effective_json,
        }
        bundle_hash = db.employee_role_bundle_sha256(bundle_row)
        connection.execute(
            "UPDATE employee_role_bundle_revision SET status='historical',"
            "updated_at=? WHERE identity_ref=? AND config_revision=?",
            (now, identity_ref, row["config_revision"]),
        )
        connection.execute(
            "INSERT INTO employee_role_bundle_revision("
            "identity_ref,config_revision,idx,employee_key,"
            "employee_catalog_version,employee_name_snapshot,person_snapshot,"
            "identity_scheme,config_sha256,bundle_sha256,baseline_json,"
            "effective_json,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity_ref, revision, old_bundle["idx"],
                old_bundle["employee_key"],
                old_bundle["employee_catalog_version"],
                old_bundle["employee_name_snapshot"],
                old_bundle.get("person_snapshot") or "",
                old_bundle.get("identity_scheme") or "legacy-six",
                config_hash, bundle_hash, baseline_json, effective_json,
                "active", now, now,
            ),
        )
        inserted = connection.execute(
            "SELECT * FROM employee_role_bundle_revision "
            "WHERE identity_ref=? AND config_revision=?",
            (identity_ref, revision),
        ).fetchone()
        if not inserted or not db.employee_role_bundle_row_valid(inserted):
            raise RuntimeError("员工岗位 role bundle 修订写入失败")


def activate_learning_bundle(
    run_id: int,
    batch_id: int,
    expected_identity_ref: str,
    expected_config_revision: int,
    expected_config_sha256: str,
    effective_role_bundle_delta: dict,
    artifact_ids: list[int],
    source_ids: list[int],
    expected_bundle_sha256: str | None = None,
) -> dict:
    """Approve one evidence-backed learning proposal atomically.

    Learning is an overlay on the immutable V4 catalog identity.  Approval
    advances both config and the complete effective role bundle in the same
    transaction; old tasks keep their historical revision and bundle hash.
    """
    identity_ref = str(expected_identity_ref or "").strip()
    config_hash = str(expected_config_sha256 or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", identity_ref):
        raise ValueError("员工岗位身份引用无效")
    if not re.fullmatch(r"[0-9a-f]{64}", config_hash):
        raise ValueError("员工岗位配置摘要无效")
    if not isinstance(effective_role_bundle_delta, dict):
        raise ValueError("进修能力包变更无效")
    changes = effective_role_bundle_delta.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("进修能力包没有可审批的变更")
    required_kinds = {"knowledge", "skill", "capability", "workflow"}
    kinds = {str(change.get("kind") or "").strip() for change in changes
             if isinstance(change, dict)}
    if not required_kinds <= kinds:
        raise ValueError("进修必须同时更新知识、技能、能力和工作流程")
    try:
        clean_artifact_ids = sorted({int(value) for value in artifact_ids})
        clean_source_ids = sorted({int(value) for value in source_ids})
        revision_expected = int(expected_config_revision)
        run_id = int(run_id)
        batch_id = int(batch_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("进修审批引用无效") from exc
    if (
        revision_expected < 1 or run_id < 1 or batch_id < 1
        or not clean_artifact_ids or not clean_source_ids
        or min(clean_artifact_ids) < 1 or min(clean_source_ids) < 1
    ):
        raise ValueError("进修审批证据引用无效")
    if (
        str(effective_role_bundle_delta.get("identity_ref") or "")
        != identity_ref
        or int(effective_role_bundle_delta.get("base_config_revision") or 0)
        != revision_expected
        or str(effective_role_bundle_delta.get("base_config_sha256") or "")
        != config_hash
    ):
        raise RuntimeError("进修提案已过期，请重新研究")

    def append_unique(rows: list, value):
        marker = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if all(json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) != marker for row in rows):
            rows.append(value)

    with db.atomic() as connection:
        run = connection.execute(
            "SELECT * FROM employee_learning_run WHERE id=?",
            (run_id,),
        ).fetchone()
        if not run or (
            int(run["batch_id"]) != batch_id
            or str(run["identity_ref"] or "") != identity_ref
            or int(run["base_config_revision"] or 0) != revision_expected
            or str(run["base_config_sha256"] or "") != config_hash
            or str(run["status"] or "") != "awaiting_approval"
        ):
            raise RuntimeError("进修运行与待审批提案不一致")
        artifact_placeholders = ",".join("?" for _ in clean_artifact_ids)
        source_placeholders = ",".join("?" for _ in clean_source_ids)
        linked_artifacts = connection.execute(
            "SELECT COUNT(*) AS n FROM employee_learning_artifact "
            f"WHERE run_id=? AND id IN ({artifact_placeholders})",
            (run_id, *clean_artifact_ids),
        ).fetchone()
        linked_sources = connection.execute(
            "SELECT COUNT(*) AS n FROM employee_learning_source "
            f"WHERE run_id=? AND id IN ({source_placeholders})",
            (run_id, *clean_source_ids),
        ).fetchone()
        if (
            int(linked_artifacts["n"] or 0) != len(clean_artifact_ids)
            or int(linked_sources["n"] or 0) != len(clean_source_ids)
        ):
            raise RuntimeError("进修产物与真实来源账本断链")
        raw = connection.execute(
            "SELECT * FROM employee_role_config WHERE identity_ref=?",
            (identity_ref,),
        ).fetchone()
        if not raw or not db.employee_role_config_row_valid(raw):
            raise RuntimeError("员工岗位配置完整性校验失败")
        row = dict(raw)
        if (
            int(row["config_revision"]) != revision_expected
            or str(row["config_sha256"]) != config_hash
        ):
            raise db.StaleWriteError("员工岗位配置已更新")
        slot = connection.execute(
            "SELECT active_identity_ref FROM employee_slot WHERE idx=?",
            (int(row["idx"]),),
        ).fetchone()
        if not slot or str(slot["active_identity_ref"] or "") != identity_ref:
            raise db.StaleWriteError("员工岗位身份已更新")
        raw_bundle = connection.execute(
            "SELECT * FROM employee_role_bundle_revision "
            "WHERE identity_ref=? AND config_revision=? AND config_sha256=?",
            (identity_ref, revision_expected, config_hash),
        ).fetchone()
        if not raw_bundle or not db.employee_role_bundle_row_valid(raw_bundle):
            raise RuntimeError("员工岗位 role bundle 完整性校验失败")
        old_bundle = dict(raw_bundle)
        if expected_bundle_sha256 is not None and (
            str(expected_bundle_sha256).strip()
            != str(old_bundle["bundle_sha256"])
        ):
            raise db.StaleWriteError("员工岗位能力包已更新")
        baseline = json.loads(old_bundle["baseline_json"])
        effective = json.loads(old_bundle["effective_json"])
        profile = dict(effective.get("professional_profile") or {})
        workflow = list(effective.get("workflow") or [])
        clean = db.normalize_employee_config(row)
        skills = list(clean["skills"])
        evidence_rows = list(effective.get("learning_evidence") or [])
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError("进修变更结构无效")
            kind = str(change.get("kind") or "").strip()
            title = str(change.get("title") or "").strip()
            statement = str(change.get("statement") or "").strip()
            payload = change.get("payload") or {}
            refs = sorted({int(value) for value in change.get("source_ids") or []})
            if not title or not statement or not refs or not set(refs) <= set(clean_source_ids):
                raise ValueError("进修变更缺少真实证据回链")
            evidence = {
                "run_id": run_id, "batch_id": batch_id,
                "artifact_id": int(change.get("artifact_id") or 0),
                "kind": kind, "title": title, "statement": statement,
                "source_ids": refs,
            }
            append_unique(evidence_rows, evidence)
            if kind == "knowledge":
                rows = list(profile.get("knowledge_domains") or [])
                append_unique(rows, statement)
                profile["knowledge_domains"] = rows
            elif kind == "skill":
                card = {
                    "title": title, "detail": statement,
                    "source_ids": refs, "research_run_id": run_id,
                    "enabled": True, "learned_at": time.time(),
                }
                append_unique(skills, card)
                rows = list(profile.get("skill_tree") or [])
                append_unique(rows, statement)
                profile["skill_tree"] = rows
            elif kind == "capability":
                rows = list(profile.get("capabilities") or [])
                append_unique(rows, statement)
                profile["capabilities"] = rows
            elif kind == "workflow":
                step = payload.get("step") if isinstance(payload, dict) else None
                append_unique(workflow, str(step or statement).strip())
            elif kind == "data_object":
                rows = list(profile.get("data_objects") or [])
                append_unique(rows, statement)
                profile["data_objects"] = rows
            elif kind == "tool":
                tool = payload.get("tool") if isinstance(payload, dict) else None
                access = str((payload or {}).get("access") or "read_only")
                if access != "read_only":
                    raise ValueError("进修不得自行增加写权限")
                rows = list(profile.get("tool_permissions") or [])
                append_unique(rows, {
                    "tool": str(tool or title), "access": "read_only",
                    "scope": statement,
                })
                profile["tool_permissions"] = rows
            elif kind == "escalation":
                rows = list(profile.get("escalation_matrix") or [])
                append_unique(rows, {
                    "level": "learned", "condition": statement,
                    "owner": "人工负责人", "action": "复核后处置",
                })
                profile["escalation_matrix"] = rows
            elif kind == "learning_track":
                rows = list(profile.get("learning_tracks") or [])
                append_unique(rows, statement)
                profile["learning_tracks"] = rows
        now = time.time()
        clean = db.normalize_employee_config({
            **row,
            "skills_json": json.dumps(skills, ensure_ascii=False),
            "learned_at": now,
        })
        revision = revision_expected + 1
        new_config_hash = db.employee_config_sha256(identity_ref, revision, clean)
        history_columns = list(_ROLE_COLUMNS) + ["superseded_at"]
        connection.execute(
            f"INSERT INTO employee_role_config_history({','.join(history_columns)}) "
            f"VALUES({','.join('?' for _ in history_columns)})",
            [row[column] for column in _ROLE_COLUMNS] + [now],
        )
        changed = connection.execute(
            "UPDATE employee_role_config SET skills_json=?,learned_at=?,"
            "config_revision=?,config_sha256=?,updated_at=? "
            "WHERE identity_ref=? AND config_revision=? AND config_sha256=?",
            (
                clean["skills_json"], clean["learned_at"], revision,
                new_config_hash, now, identity_ref, revision_expected,
                config_hash,
            ),
        )
        if changed.rowcount != 1:
            raise db.StaleWriteError("员工岗位配置已更新")
        baseline = {**baseline, "config": clean}
        effective = {
            **effective,
            "professional_profile": profile,
            "workflow": workflow,
            "config": clean,
            "learning_evidence": evidence_rows,
        }
        baseline_json = json.dumps(
            baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        effective_json = json.dumps(
            effective, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        bundle_row = {
            **old_bundle,
            "config_revision": revision,
            "config_sha256": new_config_hash,
            "baseline_json": baseline_json,
            "effective_json": effective_json,
        }
        new_bundle_hash = db.employee_role_bundle_sha256(bundle_row)
        connection.execute(
            "UPDATE employee_role_bundle_revision SET status='historical',"
            "updated_at=? WHERE identity_ref=? AND config_revision=?",
            (now, identity_ref, revision_expected),
        )
        connection.execute(
            "INSERT INTO employee_role_bundle_revision("
            "identity_ref,config_revision,idx,employee_key,"
            "employee_catalog_version,employee_name_snapshot,person_snapshot,"
            "identity_scheme,config_sha256,bundle_sha256,baseline_json,"
            "effective_json,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity_ref, revision, old_bundle["idx"],
                old_bundle["employee_key"],
                old_bundle["employee_catalog_version"],
                old_bundle["employee_name_snapshot"],
                old_bundle.get("person_snapshot") or "",
                old_bundle.get("identity_scheme") or "legacy-six",
                new_config_hash, new_bundle_hash, baseline_json,
                effective_json, "active", now, now,
            ),
        )
        inserted = connection.execute(
            "SELECT * FROM employee_role_bundle_revision "
            "WHERE identity_ref=? AND config_revision=?",
            (identity_ref, revision),
        ).fetchone()
        if not inserted or not db.employee_role_bundle_row_valid(inserted):
            raise RuntimeError("进修 role bundle 修订写入失败")
    return {
        "status": "activated",
        "new_config_revision": revision,
        "new_config_sha256": new_config_hash,
        "bundle_sha256": new_bundle_hash,
    }


def _active_config_for_write(idx: int) -> dict:
    """Private bootstrap/read path used before an idx convenience CAS write."""
    return ensure_role_config(_active_employee(idx))


def _active_employee(idx: int) -> dict:
    from . import employeeidentity
    employee = employeeidentity.active_employee(idx)
    if not employee:
        raise ValueError("未知或历史员工不可修改当前岗位配置")
    return employee


def set_prompt_for_identity(
    identity_ref: str, template: str, *, expected_revision: int,
):
    _upsert_identity(identity_ref, {
        "prompt_template": (template or "").strip() or None,
    }, expected_revision=expected_revision)


def set_prompt(idx: int, template: str):
    config = _active_config_for_write(idx)
    set_prompt_for_identity(
        config["identity_ref"], template,
        expected_revision=config["config_revision"],
    )


def set_skills_for_identity(
    identity_ref: str, skills: list, *, expected_revision: int,
):
    _upsert_identity(identity_ref, {
        "skills_json": json.dumps(skills, ensure_ascii=False),
    }, expected_revision=expected_revision)


def set_skills(idx: int, skills: list):
    config = _active_config_for_write(idx)
    set_skills_for_identity(
        config["identity_ref"], skills,
        expected_revision=config["config_revision"],
    )


def _store_learning_result(
    identity_ref: str, skills: list, learned_at: float, *,
    expected_revision: int,
) -> None:
    _upsert_identity(identity_ref, {
        "skills_json": json.dumps(skills, ensure_ascii=False),
        "learned_at": learned_at,
    }, expected_revision=expected_revision)


def set_settings_for_identity(
    identity_ref: str, settings: dict, *,
    expected_revision: int,
):
    _upsert_identity(identity_ref, {
        "settings_json": json.dumps(settings or {}, ensure_ascii=False),
    }, expected_revision=expected_revision)


def set_settings(idx: int, settings: dict):
    config = _active_config_for_write(idx)
    set_settings_for_identity(
        config["identity_ref"], settings,
        expected_revision=config["config_revision"],
    )


def set_models_for_identity(
    identity_ref: str, model_text=None, model_image=None, *,
    expected_revision: int,
):
    data = {}
    if model_text is not None:
        data["model_text"] = model_text or None
    if model_image is not None:
        data["model_image"] = model_image or None
    if data:
        _upsert_identity(
            identity_ref, data, expected_revision=expected_revision,
        )


def set_models(idx: int, model_text=None, model_image=None):
    config = _active_config_for_write(idx)
    set_models_for_identity(
        config["identity_ref"], model_text=model_text, model_image=model_image,
        expected_revision=config["config_revision"],
    )


def is_enabled(idx: int) -> bool:
    row = db.one(
        "SELECT s.enabled AS slot_enabled,c.enabled AS legacy_enabled "
        "FROM (SELECT ? AS idx) x "
        "LEFT JOIN employee_slot s ON s.idx=x.idx "
        "LEFT JOIN employee_config c ON c.idx=x.idx",
        (idx,),
    )
    if not row:
        return True
    return not (
        (row.get("slot_enabled") is not None
         and int(row.get("slot_enabled") or 0) == 0)
        or (row.get("legacy_enabled") is not None
            and int(row.get("legacy_enabled") or 0) == 0)
    )


def slot_state(idx: int) -> dict:
    row = db.one(
        "SELECT idx,active_identity_ref,enabled,row_version,updated_at "
        "FROM employee_slot WHERE idx=?",
        (int(idx),),
    )
    if not row:
        return {
            "idx": int(idx), "active_identity_ref": None, "enabled": True,
            "row_version": 0, "updated_at": None,
        }
    return {
        **row,
        "enabled": int(row.get("enabled") or 0) != 0,
        "row_version": int(row.get("row_version") or 0),
    }


def set_enabled(
    idx: int, enabled: bool, *, expected_row_version: int,
) -> dict:
    if (
        isinstance(expected_row_version, bool)
        or not isinstance(expected_row_version, int)
        or expected_row_version < 1
    ):
        raise ValueError("员工在岗状态版本必填且必须有效")
    _active_config_for_write(idx)
    now = time.time()
    with db.atomic() as connection:
        where = "idx=? AND row_version=?"
        params: list = [
            1 if enabled else 0, now, int(idx), expected_row_version,
        ]
        changed = connection.execute(
            "UPDATE employee_slot SET enabled=?,row_version=row_version+1,"
            f"updated_at=? WHERE {where}",
            tuple(params),
        )
        if changed.rowcount != 1:
            raise RuntimeError("员工在岗状态已更新，请刷新后再保存")
        # One-release compatibility mirror for older maintenance tools/tests
        # which still write the former idx-keyed table. Role-specific fields
        # are never read from this table after schema54.
        connection.execute(
            "INSERT INTO employee_config(idx,enabled,created_at,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(idx) DO UPDATE SET "
            "enabled=excluded.enabled,updated_at=excluded.updated_at",
            (int(idx), 1 if enabled else 0, now, now),
        )
    return slot_state(idx)


def set_caps_off_for_identity(
    identity_ref: str, caps_off: list, *,
    expected_revision: int,
):
    _upsert_identity(identity_ref, {
        "caps_off_json": json.dumps(caps_off or [], ensure_ascii=False),
    }, expected_revision=expected_revision)


def set_caps_off(idx: int, caps_off: list):
    config = _active_config_for_write(idx)
    set_caps_off_for_identity(
        config["identity_ref"], caps_off,
        expected_revision=config["config_revision"],
    )


# ---------------- 模板渲染 ----------------
def render(template: str, vars_: dict) -> str:
    """只替换 vars_ 里已知的 {占位符};未知占位符与 JSON 花括号原样保留."""
    return re.sub(r"\{(\w+)\}",
                  lambda m: str(vars_[m.group(1)]) if m.group(1) in vars_ else m.group(0),
                  template)


def skills_block(
    idx: int,
    *,
    identity_ref: str | None = None,
    revision: int | None = None,
    config_sha256: str | None = None,
    config: dict | None = None,
) -> str:
    """启用中的技能卡 → 注入工作提示词的文本块."""
    resolved = config or get_config(
        idx,
        identity_ref=identity_ref,
        revision=revision,
        config_sha256=config_sha256,
    )
    raw_skills = (resolved or {}).get("skills") or []
    if not isinstance(raw_skills, list):
        return ""
    lines, total = [], 0
    for skill in raw_skills:
        if len(lines) >= MAX_SKILLS_IN_PROMPT:
            break
        if not isinstance(skill, dict) or not skill.get("enabled", True):
            continue
        title = skill.get("title")
        detail = skill.get("detail")
        title = title.strip() if isinstance(title, str) else ""
        detail = detail.strip() if isinstance(detail, str) else ""
        if not title and not detail:
            continue
        line = f"- 【{title}】{detail}"
        # 单条异常超长技能不得挤掉其后所有有效技能。
        if len(line) > MAX_SKILLS_CHARS or total + len(line) > MAX_SKILLS_CHARS:
            continue
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return ("\n【你的进修技能库(全网收集的最新打法,本次工作要主动运用)】\n"
            + "\n".join(lines) + "\n")


_PROFILE_CONTEXT_LABELS = (
    ("scope", "职责域"),
    ("knowledge_domains", "专业知识域"),
    ("data_objects", "数据对象"),
    ("skill_tree", "技能树"),
    ("capabilities", "核心能力"),
    ("decisions", "负责决策"),
    ("learning_tracks", "学习路径"),
)


def _joined_items(items, limit: int = 60) -> str:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            item = item.get("name") or item.get("title") or ""
        text = str(item or "").strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return "、".join(out)


def profile_context_text(profile: dict) -> str:
    """岗位专业档案 → 紧凑可读文本（替代原始压缩 JSON 注入）。"""
    if not isinstance(profile, dict) or not profile:
        return ""
    lines = []
    for key, label in _PROFILE_CONTEXT_LABELS:
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}：{value.strip()}")
        elif isinstance(value, list):
            joined = _joined_items(value)
            if joined:
                lines.append(f"{label}：{joined}")
    rhythm = profile.get("operating_rhythm")
    if isinstance(rhythm, dict):
        rhythm_txt = "；".join(
            f"{cn}：{str(rhythm.get(en) or '').strip()}"
            for en, cn in (
                ("daily", "日常"), ("event_driven", "事件触发"), ("review", "复盘"),
            )
            if isinstance(rhythm.get(en), str) and str(rhythm.get(en)).strip()
        )
        if rhythm_txt:
            lines.append(f"工作节奏：{rhythm_txt}")
    for key in ("tool_permissions", "escalation_matrix"):
        value = profile.get(key)
        if isinstance(value, list) and value:
            lines.append(
                ("工具权限：" if key == "tool_permissions" else "升级路径：")
                + json.dumps(
                    value, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )[:2000]
            )
    return "\n".join(lines)


def approved_role_context_text(
    *,
    fingerprint: str = "",
    profile: dict | None = None,
    workflow=None,
    capabilities=None,
    skills: list | None = None,
    outputs: list | None = None,
    decision_contract: dict | None = None,
    profile_rendered: bool = False,
    workflow_rendered: bool = False,
    skills_rendered: bool = True,
    contract_rendered: bool = False,
) -> str:
    """已批准能力包 → 去重的紧凑可读文本。

    旧实现把整个 effective bundle 压缩 JSON 原样注入（实测每单 1k~6k 字符），
    而其中技能明细、能力项、决策合同在同一 system 里已按同一批准配置全文
    渲染过一遍。这里只补齐未渲染的部分并给出版本指纹；已渲染的部分只留
    标题级引用，杜绝同一信息进模型两次。
    """
    lines = []
    if fingerprint:
        lines.append(f"版本指纹：{fingerprint}")
    if isinstance(profile, dict) and profile:
        if profile_rendered:
            lines.append("岗位专业档案：与上文【冻结的岗位专业档案】为同一批准版本。")
        else:
            rendered = profile_context_text(profile)
            if rendered:
                lines.append(rendered)
    if workflow:
        if workflow_rendered:
            lines.append("工作方式：与上文【内部岗位工作方式】为同一批准版本。")
        elif isinstance(workflow, str) and workflow.strip():
            lines.append(f"工作流程：{workflow.strip()[:4000]}")
        elif isinstance(workflow, (list, dict)):
            joined = (
                _joined_items(workflow) if isinstance(workflow, list)
                else json.dumps(
                    workflow, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )[:4000]
            )
            if joined:
                lines.append(f"工作流程：{joined}")
    if capabilities:
        joined = (
            _joined_items(capabilities) if isinstance(capabilities, list)
            else json.dumps(
                capabilities, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )[:2000]
        )
        if joined:
            lines.append(f"批准能力项（启用状态见上文）：{joined}")
    titles = []
    for skill in skills or []:
        if not isinstance(skill, dict):
            continue
        title = str(skill.get("title") or "").strip()
        if title:
            titles.append(
                title if skill.get("enabled", True) else f"{title}（停用）"
            )
    if titles:
        suffix = "（详情已在上文技能库块注入）" if skills_rendered else ""
        lines.append(f"批准技能库{suffix}：{'、'.join(titles[:60])}")
    if isinstance(outputs, list) and outputs:
        joined = _joined_items(outputs)
        if joined:
            lines.append(f"固定交付物：{joined}")
    if isinstance(decision_contract, dict) and decision_contract:
        if contract_rendered:
            lines.append(
                "决策合同：与上文【行业决策合同】为同一批准版本，按上文全文执行。"
            )
        else:
            lines.append(
                "决策合同（批准版本）：" + json.dumps(
                    decision_contract, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )[:6000]
            )
    return "\n".join(lines)[:24000]


# ---------------- 全网进修 ----------------
def learning_prompt_bundle(station: dict, existing: list):
    """进修也走隔离研究：现有技能/岗位档案只给最终模型，不交给 WebSearch。"""
    from . import providers

    known = "、".join(s.get("title", "") for s in existing) or "(暂无)"
    known_detail = json.dumps(existing[:MAX_SKILLS_IN_PROMPT], ensure_ascii=False)
    system = f"""你是数字员工培训师,今天是 {time.strftime('%Y-%m-%d')}。
【学员内部岗位档案】
- 岗位:{station['name']}(部门:{station.get('dept', '')})
- 职责:{station['duty']}
- 对标能力:{station.get('skill', '')}
- 已掌握技能:{known}
- 已掌握技能详情:{known_detail[:MAX_SKILLS_CHARS]}

根据隔离联网代理返回的公开证据，为学员提炼 3-6 条新的技能卡。每条必须具体、
可直接执行，避开已掌握技能。只输出一个合法 JSON 对象:
{{"skills":[{{"title":"技能名,12字内","detail":"怎么做,具体可执行,120字内",
"source":"信息来源(站点/文章名)"}}]}}"""
    user = (
        f"请为「{station['name']}」补充近三个月公开出现的新方法论、平台规则变化、"
        "实用工具玩法和可执行技巧；只整理有来源支持的新内容。"
    )
    research = providers.sanitize_research_brief(
        f"检索「{station['name']}」岗位领域近三个月的公开方法论、平台规则变化、"
        "实用工具玩法和案例，优先权威来源，至少做 3 次针对性搜索。"
    )
    return providers.PromptBundle(
        system=system,
        user=user,
        research=research,
        sensitive=tuple(
            p for p in (
                station.get("duty") or "",
                station.get("skill") or "",
                known_detail,
            ) if str(p).strip()
        ),
    )


async def learn(
    station: dict,
    broadcast=None,
    *,
    claimed: bool = False,
    identity_ref: str,
    expected_revision: int,
    expected_config_sha256: str,
) -> dict:
    """联网收集该岗位的最新技能,合并进技能库.

    broadcast(ev) 可选:实时把进修步骤推给前端(SSE)。
    claimed=True 仅供已在路由入口预占进修槽的后台任务使用。
    """
    identity_ref = str(identity_ref or "").strip()
    expected_config_sha256 = str(expected_config_sha256 or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", identity_ref):
        raise ValueError("员工岗位身份引用必填且必须有效")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise ValueError("员工岗位配置修订号必填且必须有效")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_config_sha256):
        raise ValueError("员工岗位配置摘要必填且必须有效")
    idx = station["idx"]
    if claimed:
        if idx not in LEARNING:
            raise ValueError("该员工进修占位已失效")
    elif not claim_learning(idx):
        raise ValueError("该员工正在进修中")
    broadcast = broadcast or (lambda ev: None)

    def progress(kind, label=""):
        broadcast({"type": "employee_step", "idx": idx,
                   "step": {"k": kind, "l": str(label)[:300], "ts": time.time()}})

    try:
        from . import employeeidentity
        active_employee = employeeidentity.active_employee(idx)
        if not active_employee:
            raise ValueError("未知或历史员工不可进修")
        active_identity_ref = employeeidentity.identity_ref(active_employee)
        if identity_ref != active_identity_ref:
            raise RuntimeError("员工岗位身份已更新，请刷新后重试")
        active_config = await db.arun(
            get_config_by_identity, active_identity_ref,
        )
        if not active_config:
            raise RuntimeError("员工岗位配置档案不存在，请刷新后重试")
        if expected_revision != int(active_config["config_revision"]):
            raise RuntimeError("员工岗位配置已更新，请刷新后重试")
        if expected_config_sha256 != active_config["config_sha256"]:
            raise RuntimeError("员工岗位配置已更新，请刷新后重试")
        existing = active_config["skills"]
        prompt = learning_prompt_bundle(active_employee, existing)
        progress("start", "去全网进修:检索岗位最新方法论与平台规则…")
        from . import providers
        r = await providers.call_text_json(
            idx,
            prompt.user,
            web=True,
            timeout=600,
            progress=progress,
            token=f"learn:{idx}",
            system_prompt=prompt.system,
            research_brief=prompt.research,
            sensitive_texts=prompt.sensitive,
            identity_ref=active_identity_ref,
            config_revision=active_config["config_revision"],
            config_sha256=active_config["config_sha256"],
            bundle_sha256=active_config["bundle_sha256"],
        )
        fresh = []
        seen = {s.get("title") for s in existing}
        for s in r["data"].get("skills", []):
            if s.get("title") and s["title"] not in seen:
                s.update(enabled=True, learned_at=time.time())
                fresh.append(s)
                seen.add(s["title"])
        merged = existing + fresh
        await db.arun(
            _store_learning_result,
            active_config["identity_ref"],
            merged,
            time.time(),
            expected_revision=active_config["config_revision"],
        )
        progress("done", f"进修完成:新学 {len(fresh)} 条技能,技能库共 {len(merged)} 条 "
                         f"· ${r['cost_usd']:.3f}")
        broadcast({"type": "employee_update", "idx": idx})
        return {"new": len(fresh), "total": len(merged), "cost_usd": r["cost_usd"]}
    except llm.LLMError as e:
        from . import providers
        progress(
            "error",
            providers.public_failure_message(
                e,
                "进修任务暂未完成，请稍后重试",
            ),
        )
        broadcast({"type": "employee_update", "idx": idx})
        raise
    finally:
        # A route-level claim covers learning, settlement and notification.
        # Releasing it here would let a second request claim the same employee
        # while the first request is still settling, then the first route's
        # finalizer could erase the second request's claim (ABA race).
        if not claimed:
            LEARNING.discard(idx)
