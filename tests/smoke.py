"""派活生产冒烟测试。

默认只执行健康、认证和只读端点。写入型 E2E 必须显式传 ``--write``，且生产域名
还需 ``SMOKE_ALLOW_PROD_WRITES=YES``。账号只从环境变量读取，仓库不保存口令。
"""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

POSITIONAL = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
BASE = (POSITIONAL[0] if POSITIONAL else "https://paihuo.ai").rstrip("/")
WRITE_MODE = "--write" in sys.argv[1:]
BOSS = (os.environ.get("SMOKE_USERNAME", ""), os.environ.get("SMOKE_PASSWORD", ""))
_pass, _fail = [], []


def _req(method, path, body=None, cookie=None, raw=False):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        ck = r.headers.get("Set-Cookie", "")
        payload = r.read()
        return r.status, (payload if raw else json.loads(payload or "{}")), ck
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode()[:200]), ""


def ok(name, cond, detail=""):
    (_pass if cond else _fail).append(name)
    print(f"  {'✓' if cond else '✗ FAIL'} {name}{'' if cond else ' — ' + str(detail)[:120]}")
    return cond


def login(user, pw):
    st, d, ck = _req("POST", "/api/auth/login", {"username": user, "password": pw})
    return ck.split(";")[0] if st == 200 and ck else None


def main():
    print(f"== 派活冒烟测试 @ {BASE} ({'隔离写入' if WRITE_MODE else '只读'}) ==")

    st, health, _ = _req("GET", "/healthz")
    ok("公开健康检查", st == 200 and health == {"status": "ok"}, health)
    if not all(BOSS):
        ok("环境变量提供冒烟账号", False,
           "请设置 SMOKE_USERNAME 与 SMOKE_PASSWORD；仓库不含默认口令")
        return finish()

    # 1. 认证
    st, d, _ = _req("GET", "/api/state")
    ok("未登录被拦(401)", st == 401)
    ck = login(*BOSS)
    ok("boss 登录", bool(ck), "登录失败")
    if not ck:
        return finish()
    st, me, _ = _req("GET", "/api/auth/me", cookie=ck)
    ok("auth/me 返回root", st == 200 and me.get("role") == "root", me)

    # 2. 只读端点
    for path, key in [("/api/state", "jobs"), ("/api/employees", None),
                      ("/api/depts", None), ("/api/meta", "app"),
                      ("/api/billing", "prices"), ("/api/knowledge", None),
                      ("/api/schedules", None), ("/api/avatar/meta", "engines"),
                      ("/api/meetings", None), ("/api/task-center", "items"),
                      ("/api/team", "users")]:
        st, d, _ = _req("GET", path, cookie=ck)
        good = st == 200 and (key is None or (isinstance(d, dict) and key in d)
                              or isinstance(d, list))
        ok(f"GET {path}", good, d)

    # 2.2 员工内部资料：只有 boss 可见
    st, root_emps, _ = _req("GET", "/api/employees", cookie=ck)
    ok("boss可见内容部内部档案", st == 200 and root_emps
       and {"capabilities", "skills", "default_template", "model"} <= set(root_emps[0]), root_emps[:1])
    st, root_meta, _ = _req("GET", "/api/meta", cookie=ck)
    ok("boss可见工位岗位档案", st == 200 and root_meta.get("stations")
       and {"duty", "skill", "approval"} <= set(root_meta["stations"][0]),
       root_meta.get("stations", [])[:1])
    st, root_depts, _ = _req("GET", "/api/depts", cookie=ck)
    root_expert = next((e for dep in root_depts for e in dep.get("employees", [])), {})
    if root_expert.get("idx"):
        st, root_detail, _ = _req("GET", f"/api/depts/emp/{root_expert['idx']}", cookie=ck)
        ok("boss可见专家内部档案", st == 200
           and {"duty", "desc", "inputs", "steps", "deliverables",
                "capabilities", "skills"} <= set(root_detail), root_detail)

    # 2.5 V27.2:三件套向导进度 + 会议速览列
    st, d, _ = _req("GET", "/api/state", cookie=ck)
    ok("state带三件套进度trio", st == 200
       and {"video", "censor", "publish"} <= set((d.get("trio") or {}).keys()), d.get("trio"))
    st, rows, _ = _req("GET", "/api/meetings", cookie=ck)
    if rows:
        st, m1, _ = _req("GET", f"/api/meetings/{rows[0]['id']}", cookie=ck)
        ok("会议详情带速览字段", st == 200 and "summary_md" in m1,
           list(m1.keys()) if st == 200 else m1)
    st, tc, _ = _req("GET", "/api/task-center", cookie=ck)
    ok("任务中心有状态总账和来源追踪", st == 200 and "counts" in tc
       and all({"kind", "status_group", "source_label", "target_route"} <= set(x)
               for x in tc.get("items", [])), tc)

    # 3. 数字人引擎(基础版应在列表)
    st, meta, _ = _req("GET", "/api/avatar/meta", cookie=ck)
    engs = [e["key"] for e in meta.get("engines", [])]
    ok("基础版引擎已上线", "basic" in engs, engs)
    ok("原声可用", meta.get("own_voice_ready") is True)

    if not WRITE_MODE:
        print("  · 只读模式完成；未创建、扣费、修改或删除任何业务数据")
        return finish()
    if BASE in ("https://paihuo.ai", "https://www.paihuo.ai") \
            and os.environ.get("SMOKE_ALLOW_PROD_WRITES") != "YES":
        ok("生产写入已获显式授权", False,
           "若确需生产写入，请额外设置 SMOKE_ALLOW_PROD_WRITES=YES")
        return finish()

    # 4. 知识库 CRUD
    st, d, _ = _req("POST", "/api/knowledge", {"title": "冒烟测试", "content": "x"}, ck)
    kid = d.get("id") if st == 200 else None
    ok("知识库新增", bool(kid), d)
    if kid:
        st, _, _ = _req("PUT", f"/api/knowledge/{kid}", {"content": "y"}, ck)
        ok("知识库更新", st == 200)
        st, _, _ = _req("DELETE", f"/api/knowledge/{kid}", cookie=ck)
        ok("知识库删除", st == 200)

    # 5. 专家任务(派活)一条,验证扣费+产出
    bal0 = _req("GET", "/api/billing", cookie=ck)[1].get("balance", 0)
    task_employee = root_expert or (root_emps[0] if root_emps else {})
    if not task_employee:
        ok("schema54", False, "")
        return finish()
    st, d, _ = _req("POST", "/api/tasks",
                    {
                        "emp_idx": task_employee.get("idx"),
                        "identity_ref": task_employee.get("identity_ref"),
                        "config_revision": task_employee.get("config_revision"),
                        "config_sha256": task_employee.get("config_sha256"),
                        "brief": {"direction": "smoke schema54 binding"},
                    }, ck)
    tid = d.get("task_id") if st == 200 else None
    ok("派活专家任务", bool(tid), d)
    if tid:
        done = False
        for _ in range(30):
            time.sleep(6)
            st, t, _ = _req("GET", f"/api/tasks/{tid}", cookie=ck)
            if t.get("status") in ("done", "failed"):
                done = t["status"] == "done" and bool(t.get("output_md"))
                break
        ok("专家任务出交付物", done)
        # 导出
        st, _, _ = _req("GET", f"/api/tasks/{tid}/export.pdf", cookie=ck, raw=True)
        ok("任务导出PDF", st == 200)
        _req("DELETE", f"/api/tasks/{tid}", cookie=ck)

    # 6. 圆桌会议自动选人
    st, d, _ = _req("POST", "/api/meetings/suggest",
                    {"question": "餐厅工作日客流怎么拉"}, ck)
    ok("会议自动选人", st == 200 and len(d.get("idxs", [])) >= 2, d)

    # 7. 反馈
    st, _, _ = _req("POST", "/api/feedback", {"text": "冒烟测试反馈", "contact": "-"}, ck)
    ok("反馈提交", st == 200)

    # 7.5 V24:公众号排版 / 审查官 / 小红书风格 / 发布渠道
    st, d, _ = _req("GET", "/api/mp/themes", cookie=ck)
    ok("公众号排版12主题", st == 200 and len(d.get("themes", [])) == 12, d)
    st, d, _ = _req("GET", "/api/xhs/styles", cookie=ck)
    ok("小红书风格预设", st == 200 and len(d.get("presets", [])) >= 6, d)
    st, d, _ = _req("POST", "/api/censor/scan",
                    {"title": "全网最低价", "body": "根治失眠,加微信领取"}, ck)
    ok("审查官极速扫描命中", st == 200 and d.get("verdict") in ("warn", "block")
       and len(d.get("issues", [])) >= 3, d)
    st, d, _ = _req("POST", "/api/censor/scan",
                    {"title": "今天天气不错", "body": "适合出门走走,拍拍照片"}, ck)
    ok("审查官干净内容放行", st == 200 and d.get("verdict") == "pass", d)
    st, d, _ = _req("GET", "/api/censor/logs", cookie=ck)
    ok("审查记录可读", st == 200 and isinstance(d, list), d)
    st, d, _ = _req("GET", "/api/channels/wechat", cookie=ck)
    ok("公众号渠道配置可读", st == 200 and "server_ip" in d, d)

    # 7.6 V25:工具箱 / 台账 / 矩阵 / 成片 / 通知
    st, d, _ = _req("GET", "/api/tools/meta", cookie=ck)
    ok("工具箱meta(节日+音色)", st == 200 and d.get("festivals") is not None
       and len(d.get("voices", [])) >= 8, d)
    st, d, _ = _req("POST", "/api/publog", {"platform": "微博", "title": "冒烟台账", "days_ago": 0}, ck)
    plid = d.get("id") if st == 200 else None
    ok("发布台账登记", bool(plid), d)
    if plid:
        st, rows, _ = _req("GET", "/api/publog", cookie=ck)
        ok("台账列表含T+1/3/7", st == 200 and any(
            r["id"] == plid and set((r.get("retro") or {}).keys()) == {"1", "3", "7"} for r in rows), "")
        _req("DELETE", f"/api/publog/{plid}", cookie=ck)
    st, d, _ = _req("GET", "/api/matrix/accounts", cookie=ck)
    ok("矩阵发布平台就绪", st == 200 and len(d.get("platforms", [])) >= 2, d)
    st, d, _ = _req("POST", "/api/matrix/publish",
                    {"platform": "xhs", "account": "nonexist", "title": "x", "body": "x"}, ck)
    ok("矩阵发未绑定账号被拒(400)", st == 400, d)
    st, d, _ = _req("POST", "/api/matrix/tasks/999999/retry", cookie=ck)
    ok("矩阵重试不存在任务404", st == 404, d)
    st, d, _ = _req("GET", "/api/text-video", cookie=ck)
    ok("成片任务列表", st == 200 and isinstance(d, list), d)
    st, d, _ = _req("GET", "/api/tv/clips", cookie=ck)
    ok("混剪素材库可读", st == 200 and isinstance(d, list), d)
    st, d, _ = _req("GET", "/api/tools/jobs", cookie=ck)
    ok("工具箱后台作业可读", st == 200 and isinstance(d, list), d)
    st, d, _ = _req("PUT", "/api/channels/webhook", {"url": "https://bad.example/x"}, ck)
    ok("坏webhook被拒(400)", st == 400, d)

    # 8. 多租户数据隔离
    tname = f"冒烟企业{int(time.time())}"
    uname = f"smoke{int(time.time())}"
    temporary_password = secrets.token_urlsafe(20)
    st, d, _ = _req("POST", "/api/team/tenants",
                    {"name": tname, "owner": uname,
                     "password": temporary_password}, ck)
    ntid = d.get("tenant_id") if st == 200 else None
    ok("root开新租户", bool(ntid), d)
    if ntid:
        ck2 = login(uname, temporary_password)
        ok("新租户主账号登录", bool(ck2))
        if ck2:
            st, s2, _ = _req("GET", "/api/state", cookie=ck2)
            ok("新租户数据隔离(0工单)", st == 200 and len(s2.get("jobs", [])) == 0, s2)
            forbidden_core = {"capabilities", "skills", "default_template", "placeholders",
                              "prompt_template", "settings", "stats", "model", "model_id",
                              "duty", "skill", "approval", "channel_catalog",
                              "default_channels", "default_dimensions"}
            st, pub_emps, _ = _req("GET", "/api/employees", cookie=ck2)
            ok("客户只收到内容员工文字介绍", st == 200 and pub_emps
               and pub_emps[0].get("intro")
               and not (forbidden_core & set(pub_emps[0])), pub_emps[:1])
            st, pub_meta, _ = _req("GET", "/api/meta", cookie=ck2)
            first_station = (pub_meta.get("stations") or [{}])[0]
            ok("客户meta不泄露岗位档案", st == 200 and first_station.get("intro")
               and not (forbidden_core & set(first_station))
               and not ({"channel_catalog", "default_channels", "default_dimensions"} & set(pub_meta)),
               first_station)
            st, pub_depts, _ = _req("GET", "/api/depts", cookie=ck2)
            first_expert = next((e for dep in pub_depts for e in dep.get("employees", [])), {})
            forbidden_expert = {"duty", "desc", "inputs", "steps", "deliverables",
                                "capabilities", "skills", "learned_at", "learning",
                                "prompt_template", "is_custom", "skills_n", "stats"}
            ok("客户只收到专家文字介绍", st == 200 and first_expert.get("intro")
               and not (forbidden_expert & set(first_expert)), first_expert)
            if first_expert.get("idx"):
                st, pub_detail, _ = _req(
                    "GET", f"/api/depts/emp/{first_expert['idx']}", cookie=ck2)
                ok("客户专家详情不泄露内部资料", st == 200 and pub_detail.get("intro")
                   and not (forbidden_expert & set(pub_detail)), pub_detail)
            st, _, _ = _req("GET", "/api/admin/overview", cookie=ck2)
            ok("新租户禁访平台后台(403)", st == 403)
            st, _, _ = _req("POST", "/api/jobs",
                            {"brief": {"direction": "x"}}, ck2)
            ok("新租户余额0下单被拒(402)", st == 402)
        # 收尾:停用测试租户,不污染列表
        _req("PUT", f"/api/team/tenants/{ntid}", {"enabled": False}, ck)

    # 9. 游客也只能查看文字介绍，且可以点开介绍详情
    st, _, tour_set_cookie = _req("POST", "/api/guest/tour")
    tour_ck = tour_set_cookie.split(";")[0] if st == 200 and tour_set_cookie else None
    ok("游客参观凭证", bool(tour_ck))
    if tour_ck:
        st, tour_depts, _ = _req("GET", "/api/depts", cookie=tour_ck)
        tour_expert = next((e for dep in tour_depts for e in dep.get("employees", [])), {})
        if tour_expert.get("idx"):
            st, tour_detail, _ = _req(
                "GET", f"/api/depts/emp/{tour_expert['idx']}", cookie=tour_ck)
            ok("游客可看员工介绍且无内部档案", st == 200 and tour_detail.get("intro")
               and not ({"duty", "desc", "inputs", "steps", "deliverables",
                         "capabilities", "skills", "prompt_template"} & set(tour_detail)),
               tour_detail)

    finish()


def finish():
    print(f"\n== 结果: {len(_pass)} 通过 / {len(_fail)} 失败 ==")
    if _fail:
        print("失败项:", ", ".join(_fail))
        sys.exit(1)
    print("✅ 全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
