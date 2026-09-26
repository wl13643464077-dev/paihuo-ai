"""团队权限的 HTTP 路由：成员/企业/租户管理、入驻申请(第 3 期从 main.py 机械拆分，函数体未改)。

不 import main.py。
"""


import json
import re as _re_uname
import time

from fastapi import APIRouter, HTTPException

from .. import auth, billing, db, departments, funnel, inspection, signup
from ..web_common import TEN, _display_dept_name, _industry_scope, _need_admin, _need_root
from .inspection import _raise_inspection_error


router = APIRouter()


def _clean_username(raw: str) -> str:
    """用户名净化:去空白,禁掉引号/尖括号/反斜杠/控制字符(既是登录名也会进前端按钮参数,
    从源头挡住 XSS/注入),限 40 字。"""
    name = (raw or "").strip()
    if not name or len(name) > 40 or _re_uname.search(r"[\s'\"<>&\\\x00-\x1f]", name):
        raise HTTPException(400, "用户名不能含空格/引号/尖括号等特殊字符,且不超过40字")
    return name


# ---------------- V8:权限管理(成员/企业/租户) ----------------
JOB_TITLE_LABELS = {"director": "总监", "manager": "经理", "staff": "员工"}


def _need_team_view():
    """团队页权限：owner/root 全量管理；总监/经理进入受限分配视图。"""
    if auth.is_admin() or auth.can_allocate_members():
        return
    raise HTTPException(403, "需要企业主账号或总监/经理权限")


def _member_job_rank(user_row: dict) -> int:
    title = str(user_row.get("job_title") or "staff")
    return auth.JOB_TITLE_RANK.get(title, 0)


@router.get("/api/team")
def team_get():
    _need_team_view()
    actor = auth.current() or {}
    admin_view = auth.is_admin()
    users = db.q("SELECT id, username, role, modules_json, job_title, "
                 "allowed_emp_idxs_json, enabled, created_at FROM users "
                 "WHERE tenant_id=? ORDER BY id", (TEN(),))
    for x in users:
        x["modules"] = db.jloads(x.pop("modules_json"), [])
        raw_allowed = db.jloads(x.pop("allowed_emp_idxs_json"), None)
        x["allowed_emp_idxs"] = (
            sorted({int(v) for v in raw_allowed if str(v).lstrip("-").isdigit()})
            if isinstance(raw_allowed, list) else None
        )
        if x["role"] != "member":
            x["job_title"] = ""
    if not admin_view:
        # 总监/经理：只看到自己 + 职级低于自己的同租户成员（分配对象）。
        my_rank = auth.JOB_TITLE_RANK.get(auth.job_title(), 0)
        users = [
            x for x in users
            if x["id"] == actor.get("id")
            or (
                x["role"] == "member"
                and auth.JOB_TITLE_RANK.get(
                    str(x.get("job_title") or "staff"), 0
                ) < my_rank
            )
        ]
    # 数字员工分配器：按操作者自己的可用范围给出行业员工名录。
    allocator = []
    for d in departments.list_depts():
        if not auth.dept_visible(d["key"]):
            continue
        if not admin_view and not auth.allowed(d["key"]):
            continue
        allocator_emps = [
            {
                "idx": e["idx"],
                "name": e["name"],
                "person": e.get("person") or "",
                "emoji": e.get("emoji") or "",
            }
            for e in d["employees"]
            if admin_view or auth.employee_allowed(e["idx"], d["key"])
        ]
        if allocator_emps:
            allocator.append({
                "key": d["key"],
                "name": _display_dept_name(d["key"], d["name"]),
                "emoji": d["emoji"],
                "employees": allocator_emps,
            })
    if admin_view:
        t = db.one("SELECT * FROM tenants WHERE id=?", (TEN(),))
    else:
        row = db.one("SELECT name FROM tenants WHERE id=?", (TEN(),)) or {}
        t = {"name": row.get("name") or ""}
    out = {"tenant": t, "users": users, "all_modules": auth.all_modules(),
           "industry_employees": allocator,
           "job_titles": [
               {"key": key, "label": JOB_TITLE_LABELS[key]}
               for key in auth.JOB_TITLES
           ],
           "my_job_title": auth.job_title(),
           "is_admin": admin_view,
           "can_allocate": auth.can_allocate_members()}
    if admin_view:
        # 巡店“负责门店”：老板给经理/员工分配门店（总监本来就看全部）。
        assignments = inspection.member_branch_assignments(TEN())
        for x in users:
            x["branch_ids"] = assignments.get(int(x["id"]), [])
        out["store_branches"] = inspection.assignable_branches(TEN())
    if auth.is_root():
        tenants = db.q("SELECT t.*, (SELECT COUNT(*) FROM users u WHERE u.tenant_id=t.id) n_users "
                       "FROM tenants t ORDER BY t.id")
        for x in tenants:
            x["industries"] = db.jloads(x.get("industries_json"), [])
        out["tenants"] = tenants
        out["guests"] = db.q("SELECT * FROM guests ORDER BY id DESC LIMIT 100")
        out["applies"] = db.q("SELECT * FROM account_apply ORDER BY status, id DESC LIMIT 100")
        out["all_industries"] = auth.all_industries()
    return out


def _open_account_from_apply(a: dict, trial_points: float = 0) -> dict:
    """按申请开企业账号(租户+owner+随机密码);可送体验点."""
    import re as _re
    import secrets as _sec
    base_name = _re.sub(r"\D", "", a.get("phone") or "") or f"user{a['id']}"
    username = base_name
    while db.one("SELECT id FROM users WHERE username=?", (username,)):
        username = base_name + str(_sec.randbelow(90) + 10)
    # 系统生成 16 位随机强密码:不强制首登改密,首页上手卡片里温和提示改成好记的。
    password = auth.generate_initial_password()
    tname = (a.get("company") or "").strip() or f"{(a.get('name') or a.get('phone') or '客户')}的企业"
    # 按申请单上的行业绑定 1 个行业;拿不准就留空,老板登录后首页会让他自己选。
    valid_keys, dept_names, _ = _industry_scope()
    industry_key = signup.industry_key_for_apply(a, valid_keys, dept_names)
    with db.atomic() as connection:
        tid = db.insert(
            "tenants",
            {"name": tname[:30], "industries_json": "[]"},
        )
        if industry_key:
            signup.write_tenant_industries(connection, tid, [industry_key])
        auth.create_owner_account(
            tid, username, password, system_generated=True
        )
        if trial_points > 0:
            billing.grant(tid, trial_points, "开户体验点(自动赠送)")
        db.update(
            "account_apply",
            a["id"],
            {"status": 1, "tenant_id": tid, "username": username},
        )
    funnel.record_safe(
        "registration_complete",
        "application",
        tenant_id=tid,
        actor_key=f"lead:{a.get('phone') or a['id']}",
        unique_only=True,
    )
    return {"tenant_id": tid, "tenant_name": tname[:30], "username": username,
            "password": password, "industry": industry_key or "",
            "notice": (f"【派活 PaiHuo】您的企业账号已开通\n"
                       f"网址:https://paihuo.ai\n账号:{username}\n初始密码:{password}\n"
                       + (f"已赠送 {trial_points:.0f} 点体验点数,登录就能派活。\n" if trial_points > 0 else "")
                       + "登录后可在首页把密码改成您好记的。有任何问题随时联系我们,祝生意兴隆!")}


@router.post("/api/team/applies/{aid}/approve")
def team_apply_approve(aid: int):
    """root 一键开通:申请 → 自动建企业租户+主账号+随机密码,密码只回显这一次."""
    _need_root()
    a = db.one("SELECT * FROM account_apply WHERE id=?", (aid,))
    if not a:
        raise HTTPException(404)
    if a.get("username"):
        raise HTTPException(400, f"这条申请已开通过,账号「{a['username']}」;"
                                 f"忘了密码就去该企业的成员列表重置")
    return _open_account_from_apply(a)


@router.get("/api/team/apply-config")
def apply_config_get():
    _need_root()
    return {"auto": db.get_setting("auto_approve_apply") == "1",
            "trial_points": float(db.get_setting("trial_points") or 20),
            "daily_cap": int(float(db.get_setting("auto_approve_daily_cap") or 20))}


@router.put("/api/team/apply-config")
def apply_config_put(body: dict):
    _need_root()
    db.set_setting("auto_approve_apply", "1" if body.get("auto") else "0")
    db.set_setting("trial_points", str(min(max(float(body.get("trial_points") or 20), 0), 200)))
    db.set_setting("auto_approve_daily_cap",
                   str(int(min(max(float(body.get("daily_cap") or 20), 1), 1000))))
    return {"ok": True}


@router.post("/api/team/applies/{aid}/done")
def team_apply_done(aid: int):
    _need_root()
    if not db.one("SELECT id FROM account_apply WHERE id=?", (aid,)):
        raise HTTPException(404)
    db.update("account_apply", aid, {"status": 1})
    return {"ok": True}


@router.post("/api/team/users")
def team_user_create(body: dict):
    _need_admin()
    name = _clean_username(body.get("username"))
    pw = body.get("password") or ""
    policy_error = auth.password_policy_error(pw)
    if policy_error:
        raise HTTPException(400, policy_error)
    if db.one("SELECT id FROM users WHERE username=?", (name,)):
        raise HTTPException(400, "用户名已存在")
    tid = TEN()
    if auth.is_root() and body.get("tenant_id"):
        tid = int(body["tenant_id"])
    role = "owner" if (auth.is_root() and body.get("role") == "owner") else "member"
    job_title = str(body.get("job_title") or "staff")
    if job_title not in auth.JOB_TITLES:
        raise HTTPException(400, "职级只能是总监、经理或员工")
    uid = db.insert("users", {"tenant_id": tid, "username": name,
                              "password_hash": auth.hash_pw(pw), "role": role,
                              "modules_json": json.dumps(body.get("modules") or []),
                              "job_title": job_title,
                              "enabled": 1, "must_change_password": 1})
    return {"id": uid}


def _clean_emp_whitelist(raw) -> str | None:
    """白名单输入规整：None=不限定；数组=去重排序的行业员工 idx 名单。"""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise HTTPException(400, "数字员工名单格式无效")
    idxs = sorted({
        int(v) for v in raw
        if isinstance(v, (int, str)) and str(v).lstrip("-").isdigit()
    })
    if len(idxs) > 500:
        raise HTTPException(400, "数字员工名单过长")
    for emp_idx in idxs:
        emp = departments.get_active(emp_idx)
        if not emp:
            raise HTTPException(400, f"名单包含不存在的数字员工 #{emp_idx}")
    return json.dumps(idxs)


@router.put("/api/team/users/{uid}")
def team_user_update(uid: int, body: dict):
    actor = auth.current() or {}
    actor_is_admin = auth.is_admin()
    if not actor_is_admin:
        # 总监/经理只有一项权力：给职级低于自己的成员分配数字员工。
        if not auth.can_allocate_members():
            raise HTTPException(403, "需要企业主账号权限")
        if set(body) - {"allowed_emp_idxs"}:
            raise HTTPException(403, "板块、职级、密码与启停只能由企业主账号管理")
        if "allowed_emp_idxs" not in body:
            raise HTTPException(400, "缺少要分配的数字员工名单")
    actor_is_root = auth.is_root()
    actor_tenant_id = TEN()
    data = {}
    if "modules" in body:
        data["modules_json"] = json.dumps(body["modules"] or [])
    if "job_title" in body:
        title = str(body["job_title"] or "staff")
        if title not in auth.JOB_TITLES:
            raise HTTPException(400, "职级只能是总监、经理或员工")
        data["job_title"] = title
    if "allowed_emp_idxs" in body:
        data["allowed_emp_idxs_json"] = _clean_emp_whitelist(
            body["allowed_emp_idxs"]
        )
    if "enabled" in body:
        data["enabled"] = 1 if body["enabled"] else 0
    if body.get("password"):
        policy_error = auth.password_policy_error(body["password"])
        if policy_error:
            raise HTTPException(400, policy_error)
        data["password_hash"] = auth.hash_pw(body["password"])
        data["must_change_password"] = 1
    with db.atomic() as connection:
        current_row = connection.execute(
            "SELECT * FROM users WHERE id=?", (uid,)
        ).fetchone()
        if not current_row:
            raise HTTPException(404)
        u = dict(current_row)
        if not actor_is_root and int(u["tenant_id"]) != int(actor_tenant_id):
            raise HTTPException(404)
        if u["role"] == "root" and not actor_is_root:
            raise HTTPException(403)
        if (
            u["role"] != "member"
            and ("job_title" in data or "allowed_emp_idxs_json" in data)
        ):
            raise HTTPException(400, "职级与数字员工分配只适用于副账号成员")
        if "allowed_emp_idxs_json" in data:
            target_modules = set(db.jloads(u.get("modules_json"), []))
            target_list = (
                json.loads(data["allowed_emp_idxs_json"])
                if data["allowed_emp_idxs_json"] is not None else None
            )
            if target_list is not None:
                for emp_idx in target_list:
                    emp = departments.get_active(emp_idx)
                    dept_key = str((emp or {}).get("dept_key") or "")
                    if dept_key not in target_modules:
                        raise HTTPException(
                            400,
                            f"数字员工 #{emp_idx} 所在行业未对该成员开通，"
                            "请先在板块里开通对应行业",
                        )
            if not actor_is_admin:
                # 经理/总监的分配边界：目标职级低于自己、行业和员工都在
                # 自己的可用范围内；自己被限定名单时不得放开为“全部”。
                if u["id"] == actor.get("id"):
                    raise HTTPException(403, "不能给自己调整数字员工名单")
                actor_rank = auth.JOB_TITLE_RANK.get(auth.job_title(), 0)
                if _member_job_rank(u) >= actor_rank:
                    raise HTTPException(403, "只能给职级低于自己的成员分配数字员工")
                if target_list is None:
                    if (actor.get("allowed_emp_idxs") is not None):
                        raise HTTPException(
                            403, "您自己是受限名单，只能分配名单内的数字员工"
                        )
                    for dept_key in target_modules:
                        if dept_key not in auth.BASE_MODULES and not auth.allowed(dept_key):
                            raise HTTPException(
                                403, "成员开通的行业超出您的权限范围，无法放开为全部"
                            )
                else:
                    for emp_idx in target_list:
                        emp = departments.get_active(emp_idx)
                        dept_key = str((emp or {}).get("dept_key") or "")
                        if not auth.employee_allowed(emp_idx, dept_key):
                            raise HTTPException(
                                403,
                                f"数字员工 #{emp_idx} 不在您的可分配范围内",
                            )
        if data:
            connection.execute(
                "UPDATE users SET "
                + ",".join(f"{key}=?" for key in data)
                + ",updated_at=? WHERE id=?",
                (*data.values(), time.time(), uid),
            )
            # 停用成员等同强制下线；会话撤销与 enabled 更新必须同事务。
            # 否则账号重新启用时，停用前 Cookie 会重新变成有效。
            if (
                int(u.get("enabled") or 0) == 1
                and data.get("enabled") == 0
            ):
                auth.revoke_sessions(uid)
    return {"ok": True}


@router.delete("/api/team/users/{uid}")
def team_user_delete(uid: int):
    _need_admin()
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not u or (not auth.is_root() and u["tenant_id"] != TEN()):
        raise HTTPException(404)
    if u["role"] == "root":
        raise HTTPException(403, "root 账号不可删除")
    if u["id"] == auth.current()["id"]:
        raise HTTPException(400, "不能删除自己")
    with db.atomic() as connection:
        connection.execute("DELETE FROM users WHERE id=?", (uid,))
        # 成员删掉后不留悬空的门店绑定。
        connection.execute("DELETE FROM user_branch WHERE user_id=?", (uid,))
    return {"ok": True}


@router.put("/api/team/users/{uid}/branches")
def team_user_branches(uid: int, body: dict):
    """老板给成员分配负责门店（整体替换）。"""
    _need_admin()
    try:
        return inspection.set_member_branches(
            int((auth.current() or {}).get("id") or 0),
            uid,
            body.get("branch_ids"),
        )
    except inspection.InspectionError as exc:
        _raise_inspection_error(exc)


@router.put("/api/team/tenant")
def team_tenant_update(body: dict):
    _need_admin()
    name = (body.get("name") or "").strip()
    if name:
        db.update("tenants", TEN(), {"name": name})
    return {"ok": True}


@router.post("/api/team/tenants")
def team_tenant_create(body: dict):
    """root:开新企业租户 + 其主账号."""
    _need_root()
    name = (body.get("name") or "").strip()
    owner = _clean_username(body.get("owner"))
    pw = body.get("password") or ""
    if not name:
        raise HTTPException(400, "企业名必填")
    policy_error = auth.password_policy_error(pw)
    if policy_error:
        raise HTTPException(400, policy_error)
    if db.one("SELECT id FROM users WHERE username=?", (owner,)):
        raise HTTPException(400, "主账号用户名已存在")
    valid_ind = {d["key"] for d in auth.all_industries()}
    inds = [x for x in (body.get("industries") or []) if x in valid_ind]
    with db.atomic() as connection:
        tid = db.insert("tenants", {
            "name": name,
            "industries_json": json.dumps(inds, ensure_ascii=False),
        })
        for position, industry_key in enumerate(dict.fromkeys(inds)):
            connection.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,"
                "is_primary,created_at) VALUES(?,?,?,?)",
                (
                    tid,
                    industry_key,
                    1 if position == 0 else 0,
                    time.time(),
                ),
            )
        db.insert("users", {
            "tenant_id": tid,
            "username": owner,
            "password_hash": auth.hash_pw(pw),
            "role": "owner",
            "modules_json": "[]",
            "enabled": 1,
            "must_change_password": 1,
        })
    funnel.record_safe(
        "registration_complete",
        "direct_admin",
        tenant_id=tid,
        actor_key=f"tenant:{tid}",
        unique_only=True,
    )
    return {"tenant_id": tid}


@router.put("/api/team/tenants/{tid}/industries")
def team_tenant_industries(tid: int, body: dict):
    _need_root()
    valid = {d["key"] for d in auth.all_industries()}
    inds = [x for x in (body.get("industries") or []) if x in valid]
    if not db.one("SELECT id FROM tenants WHERE id=?", (tid,)):
        raise HTTPException(404)
    with db.atomic() as connection:
        connection.execute(
            "UPDATE tenants SET industries_json=?,updated_at=? WHERE id=?",
            (json.dumps(inds, ensure_ascii=False), time.time(), tid),
        )
        connection.execute("DELETE FROM tenant_industry WHERE tenant_id=?", (tid,))
        for position, industry_key in enumerate(dict.fromkeys(inds)):
            connection.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                "VALUES(?,?,?,?)",
                (tid, industry_key, 1 if position == 0 else 0, time.time()),
            )
    return {"ok": True}


@router.put("/api/team/tenants/{tid}")
def team_tenant_toggle(tid: int, body: dict):
    _need_root()
    with db.atomic() as connection:
        tenant_row = connection.execute(
            "SELECT id,enabled FROM tenants WHERE id=?", (tid,)
        ).fetchone()
        if not tenant_row:
            raise HTTPException(404)
        tenant = dict(tenant_row)
        if "enabled" in body:
            target_enabled = 1 if body["enabled"] else 0
            connection.execute(
                "UPDATE tenants SET enabled=?,updated_at=? WHERE id=?",
                (target_enabled, time.time(), tid),
            )
            # 停用企业必须同时永久撤销全部已签发会话。否则企业重新启用后，
            # 停用前的旧 Cookie 会复活，绕过管理员的下线意图。
            if int(tenant.get("enabled") or 0) == 1 and target_enabled == 0:
                for row in connection.execute(
                    "SELECT id FROM users WHERE tenant_id=?", (tid,)
                ):
                    auth.revoke_sessions(int(row["id"]))
        if body.get("name"):
            connection.execute(
                "UPDATE tenants SET name=?,updated_at=? WHERE id=?",
                (body["name"], time.time(), tid),
            )
    return {"ok": True}
