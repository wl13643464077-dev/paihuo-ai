"""多个路由域共用的 Web 层辅助(第 3 期从 main.py 机械拆分，函数体未改)。

放这里的都是被 main.py 与 app/routes/ 下两个及以上模块共同使用的东西：
当前用户/权限检查(TEN、_need_*)、分页、DB 线程安全包装、计费启动、
持久上传/免费 AI 限流闸门、公开视图裁剪等。main.py 会把这里的名字原样
重新导入，保持 main.<名字> 可用。本模块不得 import main.py 或 app/routes/。
"""


import asyncio
import contextvars
import os
import re
import threading
import time
from contextlib import asynccontextmanager as _asynccontextmanager

from fastapi import HTTPException

from . import assetfiles, auth, billing, db, departments, employeeidentity, employees, timeutil


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_INSPECTION_UPLOAD_MAX_BYTES = 38 * 1024 * 1024


_PERSISTENT_UPLOAD_RESERVED = contextvars.ContextVar(
    "persistent_upload_reserved",
    default=False,
)


def _need_admin():
    if not auth.is_admin():
        raise HTTPException(403, "需要主账号权限")


def _need_root():
    if not auth.is_root():
        raise HTTPException(403, "需要平台管理员权限")


def _is_boss() -> bool:
    """员工内部资料仅向唯一超级管理账号 boss 开放。"""
    u = auth.current() or {}
    return u.get("role") == "root" and u.get("username") == "boss"


_PUBLIC_STATION_TASK_GUIDES = {
    "trend": {
        "task_placeholder": "例如：追踪[行业/品牌]最近[时间范围]的市场变化，筛出适合我们跟进的内容机会。",
        "material_placeholder": "可补充品牌定位、目标客群、近期活动和重点关注的平台；没有现成材料也可直接说明业务目标。",
        "input_tips": ["关注的行业、品牌或人群", "希望覆盖的平台和时间范围", "本次选题要服务的业务目标"],
        "output_hint": "得到经过筛选的趋势判断、选题优先级和建议跟进时点",
    },
    "research": {
        "task_placeholder": "例如：围绕[具体主题]核实关键事实、数据与案例，为后续内容准备可靠素材。",
        "material_placeholder": "可粘贴待核实的说法、已有链接、数据口径和优先来源；请标明哪些信息仍不确定。",
        "input_tips": ["要核实的主题和核心问题", "优先关注的地区、时间或来源", "已有链接、说法或待验证数据"],
        "output_hint": "得到带来源的事实摘要、证据强弱和仍待核验的问题",
    },
    "benchmark": {
        "task_placeholder": "例如：拆解[主题/账号/作品]为什么有效，并提炼适合我们借鉴的表达方式。",
        "material_placeholder": "可粘贴对标账号、帖子链接、截图文字和希望重点拆解的维度。",
        "input_tips": ["要研究的主题或对标对象", "目标平台与目标受众", "最关心的内容、结构或转化问题"],
        "output_hint": "得到对标差异、可借鉴做法、不可照搬风险和验证建议",
    },
    "draft": {
        "task_placeholder": "例如：为[目标人群]撰写一篇关于[主题]的[平台/文体]初稿，重点传达[核心观点]。",
        "material_placeholder": "可粘贴产品卖点、事实素材、活动规则、品牌口吻和参考文章。",
        "input_tips": ["主题、核心观点和目标读者", "发布平台与内容形式", "必须包含或不能出现的信息"],
        "output_hint": "得到结构完整、可继续修改或直接评审的内容初稿",
    },
    "style": {
        "task_placeholder": "例如：把这份内容调整为[品牌/个人]的表达风格，保持观点不变并提升辨识度。",
        "material_placeholder": "请粘贴待改原文，并补充品牌语气、常用表达、禁用词和必须保留的事实。",
        "input_tips": ["需要改写的原文", "希望接近的语气与风格", "品牌常用词、禁用词或参考作品"],
        "output_hint": "得到语气统一、自然且符合账号人设的定稿建议",
    },
    "media": {
        "task_placeholder": "例如：为[主题内容]规划适合[目标平台]的视觉素材和画面表达。",
        "material_placeholder": "可粘贴正文、视觉参考、品牌色、图片尺寸、已有素材和版权限制。",
        "input_tips": ["正文、主题或重点信息", "目标平台和画面尺寸", "品牌视觉、素材来源与版权限制"],
        "output_hint": "得到与正文对应的视觉方案、素材需求和使用位置",
    },
    "cover": {
        "task_placeholder": "例如：为[内容主题]设计适合[目标平台]的封面方向，突出[第一眼卖点]。",
        "material_placeholder": "可粘贴标题、品牌色、参考风格、封面尺寸，以及必须出现的文字或图片说明。",
        "input_tips": ["标题、主题和核心卖点", "目标平台与目标人群", "品牌视觉或必须保留的元素"],
        "output_hint": "得到可比较的封面方向、关键信息层级和视觉建议",
    },
    "deck": {
        "task_placeholder": "例如：把[现有内容]整理成面向[听众/场景]的演示结构，突出[核心结论]。",
        "material_placeholder": "可粘贴原始正文、关键数据、汇报对象、演示时长和已有页面结构。",
        "input_tips": ["现有正文、报告或要点", "听众、使用场景和演示时长", "必须讲清的结论与行动要求"],
        "output_hint": "得到清晰的演示结构、页面重点和讲解顺序",
    },
    "publish": {
        "task_placeholder": "例如：把这份成品适配到[目标平台]，整理发布文案、标签和发布节奏。",
        "material_placeholder": "可粘贴已确认的定稿、账号信息、发布时间限制和各平台审核注意事项。",
        "input_tips": ["已经确认的内容成品", "目标平台与发布时间要求", "账号限制、审核要求和运营节奏"],
        "output_hint": "得到各平台可直接审核的发布包和发布检查项",
    },
    "retro": {
        "task_placeholder": "例如：复盘[内容/活动]在[时间范围]的表现，找出有效做法和下一轮调整重点。",
        "material_placeholder": "可粘贴曝光、点击、互动、转化等汇总数据，以及评论摘要、发布时间和异常事件。",
        "input_tips": ["要复盘的内容与发布时间", "曝光、互动、转化等可用数据", "原定目标、异常事件与用户反馈"],
        "output_hint": "得到表现诊断、原因假设、复用项和下一轮改进动作",
    },
    "inspection": {
        "task_placeholder": "例如：检查[门店/区域]本次现场照片，找出可见问题并给出整改与复查计划。",
        "material_placeholder": "请优先从巡店工作台上传现场照片；可补充门店、区域、检查范围、责任人和整改期限要求。",
        "input_tips": ["门店、区域和巡检日期", "1～8张覆盖不同区域的现场照片", "本次重点、负责人和期限要求"],
        "output_hint": "得到带照片证据的问题分级、整改责任与期限、复查标准和门店记录",
    },
}


def _public_station_task_guide(s: dict) -> dict:
    """内容部的公开派活提示；与内部模板、能力和模型配置完全隔离。"""
    guide = _PUBLIC_STATION_TASK_GUIDES.get(s.get("key")) or {
        "task_placeholder": f"例如：请「{s.get('name') or '数字员工'}」围绕[具体目标]完成[具体任务]。",
        "material_placeholder": "可粘贴与当前任务直接相关的资料、数据和参考链接。",
        "input_tips": ["具体目标和使用场景", "已有材料与限制条件", "期望完成时间"],
        "output_hint": "得到一份围绕当前目标的可执行结果",
    }
    return {
        **guide,
        "industry_placeholder": "例如：所属行业、产品类别、目标人群或具体业务场景",
    }


_EMPLOYEE_IDENTITY_PUBLIC_FIELDS = (
    "person_status", "identity_status", "identity_ref", "config_revision",
    "config_sha256", "bundle_sha256", "can_assign_new", "can_continue", "can_learn",
    "slot_row_version", "role_profile_summary",
)


def _employee_public_contract(
    employee: dict,
    *,
    config: dict | None = None,
    include_profile: bool = False,
) -> dict:
    """Expose the two independent schema-54 identity axes.

    A person slot may remain active while an old task or meeting keeps using a
    historical role identity.  Callers handling frozen work may pass its exact
    config revision; we never recover that revision from ``idx``.
    """
    # registry.STATIONS is intentionally a lightweight execution registry and
    # predates the frozen schema-54 identity fields.  Public API callers still
    # pass those core rows in several places, so normalize only an exact core
    # key/idx match to its canonical current identity before building the
    # public contract.  Industry/history rows must already be exact and are
    # never active-first substituted here.
    if not employee.get("dept_key"):
        try:
            active = employeeidentity.active_employee(int(employee.get("idx")))
        except (TypeError, ValueError):
            active = None
        if (
            active
            and active.get("dept_key") == "content"
            and str(active.get("key") or "") == str(employee.get("key") or "")
        ):
            employee = active
    view = employeeidentity.identity_view(
        employee, include_profile=include_profile,
    )
    if config is not None:
        if str(config.get("identity_ref") or "") != str(view["identity_ref"]):
            raise RuntimeError("员工岗位与配置身份不一致")
        view["config_revision"] = int(config.get("config_revision") or 0)
        view["config_sha256"] = str(config.get("config_sha256") or "")
        view["bundle_sha256"] = str(config.get("bundle_sha256") or "")
        if include_profile:
            view["professional_profile"] = (
                config.get("effective_profile")
                or config.get("professional_profile")
                or {}
            )
    result = {
        field: view.get(field) for field in _EMPLOYEE_IDENTITY_PUBLIC_FIELDS
    }
    if include_profile:
        result["professional_profile"] = view.get("professional_profile") or {}
        role_key = str(view.get("key") or employee.get("key") or "")
        cap_details = departments.capability_details_for(role_key)
        if not result["professional_profile"]:
            # 餐饮/内容部老岗位没有 V4 档案：附加发布内出厂能力档案，仅
            # 用于展示层；身份、配置包与任务提示词永远不读这份 sidecar。
            sidecar = departments.factory_profile_for(role_key)
            if sidecar:
                result["professional_profile"] = sidecar["professional_profile"]
                cap_details = sidecar.get("capability_details") or {}
        if cap_details:
            result["capability_details"] = cap_details
    # One-release aliases keep old clients readable. New UI decisions use only
    # person_status + identity_status and the explicit capability booleans.
    result["roster_status"] = (
        "active" if result["identity_status"] == "current" else "legacy"
    )
    result["can_assign"] = bool(result["can_assign_new"])
    return result


def _public_station(
    s: dict, *, include_task_guide: bool = False,
    config: dict | None = None,
) -> dict:
    """内容部员工的对外名片：只含展示信息，不含岗位实现与模型配置。"""
    public = {
        k: s[k]
        for k in ("idx", "key", "name", "dept", "emoji", "color", "intro")
    } | _employee_public_contract(s, config=config)
    if include_task_guide:
        public["task_guide"] = _public_station_task_guide(s)
    return public


def TEN() -> int:
    return auth.tenant_id()


async def _drain_task_despite_cancellation(task: asyncio.Task):
    """Wait for an already-submitted task through any outer cancellations.

    Executors cannot revoke a SQLite write that has already started.  Repeated
    request cancellation therefore must not cancel the child task or let the
    caller guess whether it committed.  Child failures are deliberately read
    from ``task.result()`` and propagated unchanged.
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _run_db_safely(fn, *args, **kwargs):
    """Linearize an already-submitted DB mutation through request cancellation.

    Once a billing/status commit reaches the executor, its caller must observe
    the real result.  Otherwise the worker can commit after cancellation while
    the handler concurrently refunds or deletes the committed artifact.
    """
    operation = asyncio.create_task(db.arun(fn, *args, **kwargs))
    return await _drain_task_despite_cancellation(operation)


async def _run_db_then_start_worker_safely(
    fn,
    *args,
    start_worker,
    should_start=None,
    settle_unstarted=None,
    **kwargs,
):
    """Linearize a durable queue mutation with its in-process worker start.

    ``db.arun`` runs work in an executor, so cancelling the request cannot
    reliably cancel a SQLite transaction that has already begun.  The caller
    must therefore observe the final DB result and, when it committed a queued
    record, schedule its worker before propagating cancellation.  If scheduling
    itself fails, the optional settlement callback closes/refunds the durable
    record instead of leaving a charged orphan.
    """
    operation = asyncio.create_task(db.arun(fn, *args, **kwargs))
    cancellation = None
    try:
        result = await asyncio.shield(operation)
    except asyncio.CancelledError as exc:
        cancellation = exc
        result = await _drain_task_despite_cancellation(operation)

    must_start = should_start(result) if should_start else True
    if must_start:
        try:
            start_worker(result)
        except Exception:
            if settle_unstarted:
                await _run_db_safely(settle_unstarted, result)
            raise

    if cancellation is not None:
        raise cancellation
    return result


def _client_log_label(value, default: str, limit: int = 40) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.:-]", "", str(value or ""))[:limit]
    return clean or default


def _create_charged_expert_task(task_data: dict, note: str = "") -> int:
    """先落 pending 任务，再用同一事务抢占并扣点，避免扣费后没有任务记录。"""
    snapshot_names = {
        "employee_key", "employee_catalog_version", "employee_name_snapshot",
        "employee_dept_key", "employee_spec_sha256", "person_snapshot",
        "identity_scheme",
    }
    required_snapshot_names = snapshot_names - {"person_snapshot"}
    config_names = {
        "employee_identity_ref", "employee_config_revision",
        "employee_config_sha256", "bundle_sha256",
    }
    identity_scheme = str(task_data.get("identity_scheme") or "").strip()
    has_frozen_snapshot = bool(
        all(
            str(task_data.get(field) or "").strip()
            for field in required_snapshot_names
        )
        and (
            identity_scheme != "v2-person"
            or bool(str(task_data.get("person_snapshot") or "").strip())
        )
    )
    supplied_config = {
        field for field in config_names
        if task_data.get(field) not in (None, "")
    }
    if supplied_config and (
        not has_frozen_snapshot or supplied_config != config_names
    ):
        raise RuntimeError("任务员工配置身份字段不完整")
    binding = (
        employeeidentity.resolve_task_binding(task_data)
        if has_frozen_snapshot and supplied_config == config_names else None
    )
    if has_frozen_snapshot and supplied_config == config_names and not binding:
        raise RuntimeError("任务员工配置版本无法验证")
    employee = (
        binding["employee"] if binding
        else employeeidentity.resolve_task(task_data) if has_frozen_snapshot
        else employeeidentity.active_employee(task_data.get("emp_idx"))
    )
    if not employee:
        raise RuntimeError("不允许向未知员工创建任务")
    config = binding["config"] if binding else employees.ensure_role_config(employee)
    identity_fields = employeeidentity.task_fields(employee, config=config)
    compared_fields = snapshot_names | supplied_config
    if has_frozen_snapshot and any(
        str(task_data.get(field) or "") != str(value)
        for field, value in identity_fields.items() if field in compared_fields
    ):
        raise RuntimeError("任务员工身份与冻结目录不一致")
    task_data = {**task_data, **identity_fields, "emp_idx": int(employee["idx"])}
    tid = int(task_data.get("tenant_id") or TEN())
    points = 0.0 if tid == 1 else float(
        (billing.prices().get("expert_task") or {"points": 1})["points"])
    task_id = db.insert("task", {
        **task_data,
        "status": "pending_charge",
        "billing_status": "pending",
        "billing_points": points,
        # 发起人:记录是哪个账号派的活;无会话的内部路径留空
        "created_by": task_data.get("created_by", (auth.current() or {}).get("id")),
    })

    def claim(connection):
        derived_frozen_work = bool(
            task_data.get("source_task_id") or task_data.get("source_meeting_id")
        )
        if not _role_binding_matches(
            connection, task_data, require_current=not derived_frozen_work,
        ):
            raise RuntimeError("员工岗位配置已更新，请刷新后重试")
        changed = connection.execute(
            "UPDATE task SET status='queued',billing_status='charged',updated_at=? "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (time.time(), task_id),
        )
        return changed.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            "expert_task", tid, claim,
            note=f"任务#{task_id}·{note}"[:160], points=points)
    except Exception:
        db.q(
            "DELETE FROM task WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (task_id,),
        )
        raise
    if not charged:
        raise RuntimeError("专家任务计费状态冲突")
    return task_id


def _role_binding_matches(
    connection, frozen: dict, *, require_current: bool,
) -> bool:
    """Check an exact role triple inside the same transaction as charging."""
    identity_ref = str(
        frozen.get("employee_identity_ref", frozen.get("identity_ref")) or ""
    ).strip()
    config_sha256 = str(
        frozen.get("employee_config_sha256", frozen.get("config_sha256")) or ""
    ).strip()
    bundle_sha256 = str(frozen.get("bundle_sha256") or "").strip()
    raw_revision = frozen.get(
        "employee_config_revision", frozen.get("config_revision")
    )
    raw_idx = frozen.get("emp_idx", frozen.get("idx"))
    try:
        revision = int(raw_revision)
        idx = int(raw_idx)
    except (TypeError, ValueError):
        return False
    if (
        re.fullmatch(r"[0-9a-f]{64}", identity_ref) is None
        or re.fullmatch(r"[0-9a-f]{64}", config_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", bundle_sha256) is None
        or revision < 1
    ):
        return False
    row = connection.execute(
        "SELECT * FROM employee_role_config WHERE identity_ref=? "
        "AND config_revision=?",
        (identity_ref, revision),
    ).fetchone()
    if row is None and not require_current:
        row = connection.execute(
            "SELECT * FROM employee_role_config_history WHERE identity_ref=? "
            "AND config_revision=?",
            (identity_ref, revision),
        ).fetchone()
    exact = bool(
        row
        and db.employee_role_config_row_valid(row)
        and int(row["idx"]) == idx
        and int(row["config_revision"]) == revision
        and str(row["config_sha256"]) == config_sha256
    )
    bundle = connection.execute(
        "SELECT * FROM employee_role_bundle_revision WHERE identity_ref=? "
        "AND config_revision=? AND config_sha256=? AND bundle_sha256=?",
        (identity_ref, revision, config_sha256, bundle_sha256),
    ).fetchone()
    exact = bool(exact and bundle and db.employee_role_bundle_row_valid(bundle))
    if not exact or not require_current:
        return exact
    slot = connection.execute(
        "SELECT active_identity_ref,enabled FROM employee_slot WHERE idx=?",
        (idx,),
    ).fetchone()
    return bool(
        slot
        and str(slot["active_identity_ref"] or "") == identity_ref
        and int(slot["enabled"] or 0) == 1
    )


_PERSISTENT_UPLOAD_WINDOW = 3600
_PERSISTENT_UPLOAD_USER_LIMIT = 20
_PERSISTENT_UPLOAD_TENANT_LIMIT = 30
_PERSISTENT_UPLOAD_TENANT_BYTES = 1024 * 1024 * 1024
_PERSISTENT_UPLOAD_TENANT_FILES = 180
_PERSISTENT_UPLOAD_GLOBAL_SEM = asyncio.Semaphore(2)
_PERSISTENT_UPLOAD_GUARD = threading.Lock()
_persistent_upload_hits: dict[tuple, list[float]] = {}
_persistent_upload_active_tenants: set[int] = set()


def _persistent_upload_usage(tid: int) -> dict:
    """Aggregate all tenant-owned persistent media without crossing tenants."""
    from . import avatar as _avatar
    from . import textvideo as _textvideo

    avatar_usage = _avatar.tenant_asset_usage(int(tid))
    files = int(avatar_usage["files"])
    used_bytes = int(avatar_usage["bytes"])
    clip_root = os.path.join(_textvideo.CLIP_ROOT, str(int(tid)))
    if os.path.isdir(clip_root):
        with os.scandir(clip_root) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        files += 1
                        used_bytes += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    asset_root = os.path.realpath(assetfiles.ASSET_ROOT)
    inspection_root = os.path.abspath(
        os.path.join(asset_root, "inspections", str(int(tid)))
    )
    try:
        inspection_inside = (
            os.path.commonpath((asset_root, inspection_root)) == asset_root
        )
    except ValueError:
        inspection_inside = False
    if (
        inspection_inside
        and os.path.isdir(inspection_root)
        and not os.path.islink(inspection_root)
        and os.path.realpath(inspection_root) == inspection_root
    ):
        for current_root, directories, filenames in os.walk(
            inspection_root, followlinks=False
        ):
            directories[:] = [
                name
                for name in directories
                if not os.path.islink(os.path.join(current_root, name))
            ]
            for name in filenames:
                path = os.path.join(current_root, name)
                try:
                    if os.path.isfile(path) and not os.path.islink(path):
                        files += 1
                        used_bytes += os.stat(path, follow_symlinks=False).st_size
                except OSError:
                    continue
    return {"files": files, "bytes": used_bytes}


def _assert_persistent_upload_capacity(
    tid: int,
    incoming_bytes: int,
    *,
    incoming_files: int = 1,
) -> dict:
    incoming_bytes = max(0, int(incoming_bytes))
    incoming_files = max(1, int(incoming_files))
    usage = _persistent_upload_usage(tid)
    if (
        usage["bytes"] + incoming_bytes
        > int(_PERSISTENT_UPLOAD_TENANT_BYTES)
        or usage["files"] + incoming_files
        > int(_PERSISTENT_UPLOAD_TENANT_FILES)
    ):
        raise HTTPException(
            413,
            "本企业的上传素材空间已满，请删除不再使用的素材后重试",
        )
    return usage


@_asynccontextmanager
async def _persistent_upload_slot(action: str):
    """Fail fast before reading request bodies; one writer per tenant."""
    if _PERSISTENT_UPLOAD_RESERVED.get():
        # The authentication middleware already owns the reservation while
        # Starlette parses this request's multipart body.
        yield
        return
    current = auth.current() or {}
    tid = TEN()
    uid = int(current.get("id") or 0)
    now = time.time()
    tenant_key = ("tenant", tid)
    user_key = ("user", tid, uid)
    reserved = False
    acquired = False
    with _PERSISTENT_UPLOAD_GUARD:
        if tid in _persistent_upload_active_tenants:
            raise HTTPException(429, "本企业已有素材正在上传，请稍后再试")
        if len(_persistent_upload_active_tenants) >= 2:
            raise HTTPException(429, "上传服务繁忙，请稍后再试")
        tenant_hits = [
            stamp for stamp in _persistent_upload_hits.get(tenant_key, [])
            if now - stamp < _PERSISTENT_UPLOAD_WINDOW
        ]
        user_hits = [
            stamp for stamp in _persistent_upload_hits.get(user_key, [])
            if now - stamp < _PERSISTENT_UPLOAD_WINDOW
        ]
        if len(tenant_hits) >= int(_PERSISTENT_UPLOAD_TENANT_LIMIT):
            raise HTTPException(429, "本企业本小时上传次数已达上限")
        if len(user_hits) >= int(_PERSISTENT_UPLOAD_USER_LIMIT):
            raise HTTPException(429, "您本小时上传次数已达上限")
        tenant_hits.append(now)
        user_hits.append(now)
        _persistent_upload_hits[tenant_key] = tenant_hits
        _persistent_upload_hits[user_key] = user_hits
        _persistent_upload_active_tenants.add(tid)
        reserved = True
        if len(_persistent_upload_hits) > 5000:
            active = {
                key: [
                    stamp for stamp in stamps
                    if now - stamp < _PERSISTENT_UPLOAD_WINDOW
                ]
                for key, stamps in _persistent_upload_hits.items()
            }
            _persistent_upload_hits.clear()
            _persistent_upload_hits.update({
                key: stamps for key, stamps in active.items() if stamps
            })
    try:
        await _PERSISTENT_UPLOAD_GLOBAL_SEM.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            _PERSISTENT_UPLOAD_GLOBAL_SEM.release()
        if reserved:
            with _PERSISTENT_UPLOAD_GUARD:
                _persistent_upload_active_tenants.discard(tid)


_FREE_AI_GLOBAL_SEM = asyncio.Semaphore(2)
_FREE_AI_COUNTER_GUARD = threading.Lock()
_FREE_AI_TENANT_DAILY = 180
_FREE_AI_USER_DAILY = 60
_FREE_AI_ACTION_DAILY = {
    "company-distill": 10,
    "parse-image": 20,
    "meeting-suggest": 30,
    "profile-distill": 10,
    "expert-match": 30,
    "task-preflight": 40,
}
_free_ai_usage: dict[tuple, int] = {}
_free_ai_active_tenants: set[int] = set()


@_asynccontextmanager
async def _free_ai_slot(action: str):
    """Bound no-charge supplier calls by tenant, user, day, and concurrency."""
    action = _client_log_label(action, "helper", 48)
    current = auth.current() or {}
    tid = TEN()
    uid = int(current.get("id") or 0)
    day = timeutil.cn_day_index()       # 免费额度按北京时间零点换日
    tenant_key = ("tenant", day, tid)
    user_key = ("user", day, tid, uid)
    action_key = ("action", day, tid, uid, action)
    reserved = False
    acquired = False
    with _FREE_AI_COUNTER_GUARD:
        if tid in _free_ai_active_tenants:
            raise HTTPException(429, "当前账号已有辅助 AI 请求在处理，请稍后再试")
        if _free_ai_usage.get(tenant_key, 0) >= _FREE_AI_TENANT_DAILY:
            raise HTTPException(429, "本租户今日辅助 AI 配额已用完")
        if _free_ai_usage.get(user_key, 0) >= _FREE_AI_USER_DAILY:
            raise HTTPException(429, "您今日的辅助 AI 配额已用完")
        if _free_ai_usage.get(action_key, 0) >= _FREE_AI_ACTION_DAILY.get(action, 20):
            raise HTTPException(429, "此辅助能力今日配额已用完")
        if _FREE_AI_GLOBAL_SEM.locked():
            raise HTTPException(429, "辅助 AI 服务繁忙，请稍后再试")
        _free_ai_active_tenants.add(tid)
        reserved = True
    try:
        await _FREE_AI_GLOBAL_SEM.acquire()
        acquired = True
        with _FREE_AI_COUNTER_GUARD:
            _free_ai_usage[tenant_key] = _free_ai_usage.get(tenant_key, 0) + 1
            _free_ai_usage[user_key] = _free_ai_usage.get(user_key, 0) + 1
            _free_ai_usage[action_key] = _free_ai_usage.get(action_key, 0) + 1
            if len(_free_ai_usage) > 10_000:
                stale = [
                    key for key in _free_ai_usage
                    if len(key) > 1 and key[1] != day
                ]
                for key in stale:
                    _free_ai_usage.pop(key, None)
        yield
    finally:
        if acquired:
            _FREE_AI_GLOBAL_SEM.release()
        if reserved:
            with _FREE_AI_COUNTER_GUARD:
                _free_ai_active_tenants.discard(tid)


async def _read_limited(file, max_bytes: int, message: str) -> bytes:
    """分块读取上传内容，达到上限立即停止，避免先把超大请求完整装入内存。"""
    data = bytearray()
    chunk_size = min(1024 * 1024, max_bytes + 1)
    while len(data) <= max_bytes:
        chunk = await file.read(min(chunk_size, max_bytes + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise HTTPException(400, message)
