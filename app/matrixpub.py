"""矩阵真发布中心(V25,beta):多平台账号 Cookie 绑定 + 发布队列 + Playwright 自动发布.

现实边界(对老板诚实):抖音/小红书没有对个人商家开放的发布 API,业内工具(蚁小二等)
全是浏览器自动化路线。本模块同款思路:
- 绑定 = 把浏览器登录态(Cookie)交给派活代持;绑定页有手把手取 Cookie 向导;
- 发布 = 服务器起无头浏览器带 Cookie 打开创作者后台,自动填充并提交;
- 平台改版可能导致流程失效 → 每一步落日志+失败自动截图,提示转人工;
- 平台风控提示:同一账号发布频率建议 ≤ 5条/天,内容先过审查官。

公众号走正规 API(wechat.py),不在此列。
"""
import asyncio
import json
import logging
import os
import time
import uuid

import httpx

from . import db, notify, pubtrack, secureconfig

log = logging.getLogger("matrixpub")
_PUB_SEM = None


def _sem():
    global _PUB_SEM
    if _PUB_SEM is None:
        import asyncio as _a
        _PUB_SEM = _a.Semaphore(1)   # 无头浏览器一次一个,防内存打爆
    return _PUB_SEM

ASSET_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")

PLATFORMS = {
    "xhs": {"name": "小红书", "emoji": "📕", "home": "https://creator.xiaohongshu.com",
            "kind": "imagetext", "cookie_hint": "creator.xiaohongshu.com 登录后的 Cookie"},
    "douyin": {"name": "抖音", "emoji": "🎵", "home": "https://creator.douyin.com",
               "kind": "video", "cookie_hint": "creator.douyin.com 登录后的 Cookie"},
}


# ---------------- 账号管理 ----------------
COOKIE_FIELD = "matrix_cookie"


def accounts(tid: int) -> list:
    """返回可直接使用的账号(Cookie 已解密,仅存在于内存)。

    解不开的 Cookie(密钥轮换或数据损坏)不让它把整个账号列表打挂:该账号按
    「登录态失效」呈现,老板重新绑定即可,发布侧自然走已有的失效分支。
    """
    out = []
    for acc in db.jloads(db.get_setting(f"matrix_accounts:{tid}"), []) or []:
        if acc.get("cookie"):
            try:
                acc = {**acc, "cookie": secureconfig.decrypt_field(
                    COOKIE_FIELD, acc["cookie"])}
            except secureconfig.SecureConfigError:
                log.warning("matrix cookie undecryptable account=%s", acc.get("id"))
                acc = {**acc, "cookie": "", "status": "expired"}
        out.append(acc)
    return out


def _save(tid: int, accs: list):
    # 平台登录态等同于账号本身,泄露即被接管;绝不明文落库。
    stored = [
        {**a, "cookie": secureconfig.encrypt_field(COOKIE_FIELD, a.get("cookie"))}
        if a.get("cookie") else a
        for a in accs
    ]
    db.set_setting(f"matrix_accounts:{tid}", json.dumps(stored, ensure_ascii=False))


def add_account(tid: int, platform: str, name: str, cookie: str) -> dict:
    if platform not in PLATFORMS:
        raise ValueError("暂只支持小红书/抖音(公众号在上面单独配)")
    cookie = (cookie or "").strip().replace("\n", " ")
    if len(cookie) < 40:
        raise ValueError("Cookie 太短,不像有效登录态——按向导重新复制完整 Cookie")
    accs = accounts(tid)
    acc = {"id": uuid.uuid4().hex[:8], "platform": platform,
           "name": (name or PLATFORMS[platform]["name"])[:20],
           "cookie": cookie, "status": "unchecked", "nickname": "", "checked_at": None}
    accs.append(acc)
    _save(tid, accs[:10])
    return {k: v for k, v in acc.items() if k != "cookie"}


def del_account(tid: int, acc_id: str):
    _save(tid, [a for a in accounts(tid) if a["id"] != acc_id])


def pub_list(tid: int) -> list:
    """给前端的账号列表(不吐 cookie)."""
    out = []
    for a in accounts(tid):
        p = PLATFORMS.get(a["platform"], {})
        out.append({**{k: v for k, v in a.items() if k != "cookie"},
                    "platform_name": p.get("name", a["platform"]), "emoji": p.get("emoji", "")})
    return out


async def _probe_login(acc: dict):
    """轻量 HTTP 探登录态(不开浏览器):返回 (alive, nickname).

    alive=True 活着 / False 确认已失效(平台有响应但不认) / None 探测没打通(网络问题,别下死结论)。
    """
    try:
        headers = {"Cookie": acc["cookie"],
                   "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
        async with httpx.AsyncClient(timeout=20, headers=headers) as cli:
            if acc["platform"] == "xhs":
                r = await cli.get("https://edith.xiaohongshu.com/api/sns/web/v2/user/me")
                d = r.json() if r.status_code == 200 else {}
                nickname = ((d.get("data") or {}).get("nickname")) or ""
                return bool(d.get("success") and nickname), nickname
            if acc["platform"] == "douyin":
                r = await cli.get("https://creator.douyin.com/web/api/media/user/info/")
                d = r.json() if r.status_code == 200 else {}
                nickname = ((d.get("user") or {}).get("nickname")) or ""
                return bool(nickname), nickname
    except Exception as exc:
        log.warning(
            "probe_login %s failed error_type=%s",
            acc.get("id"),
            type(exc).__name__,
        )
        return None, ""
    return False, ""


async def check_account(tid: int, acc_id: str) -> dict:
    """验证 Cookie 是否还活着(不发内容,只查登录态)."""
    accs = accounts(tid)
    acc = next((a for a in accs if a["id"] == acc_id), None)
    if not acc:
        raise ValueError("账号不存在")
    alive, nickname = await _probe_login(acc)
    ok = bool(alive)
    acc["status"] = "ok" if ok else "expired"
    acc["nickname"] = nickname
    acc["checked_at"] = time.time()
    _save(tid, accs)
    if not ok:
        raise ValueError("登录态无效或已过期:重新登录该平台后,按向导重新复制 Cookie 绑定")
    return {"ok": True, "nickname": nickname,
            "note": f"绑定成功,账号「{nickname}」登录态有效"}


# ---------------- 发布队列 ----------------
def enqueue(tid: int, platform: str, acc_id: str, payload: dict) -> int:
    # 入队即校验:平台非法或与账号绑定的平台不一致时,发布协程会在 PLATFORMS[...]
    # 抛 KeyError,任务永久卡在 running。这里提前拒掉,报错也更像人话。
    if platform not in PLATFORMS:
        raise ValueError("暂只支持小红书/抖音(公众号在发布渠道页单独配)")
    acc = next((a for a in accounts(tid) if a["id"] == acc_id), None)
    if not acc:
        raise ValueError("先绑定该平台账号")
    if acc.get("platform") != platform:
        raise ValueError("所选账号不属于该平台,请重新选择")
    return db.insert("pub_task", {
        "tenant_id": tid,
        "platform": platform,
        "account": acc_id,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "submission_state": "not_submitted",
    })


def _log(pid: int, msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"

    def _tx():
        # 追加语义的读-改-写放同一事务,进池后与相邻日志并发也不丢行。
        with db.atomic() as c:
            r = c.execute("SELECT log FROM pub_task WHERE id=?", (pid,)).fetchone()
            text = ((r["log"] if r else "") or "") + line
            c.execute("UPDATE pub_task SET log=?,updated_at=? WHERE id=?",
                      (text[-4000:], time.time(), pid))

    # 发布协程在事件循环上跑;日志落库进 db 线程池,防写锁竞争冻结循环。
    db.submit_write(_tx)
    # The persisted progress belongs to the current tenant and is shown in the
    # task UI.  The host journal receives only a stable event: account names,
    # content, screenshots and provider text must never be copied there.
    log.info("pub_task %s progress", pid)


def _mark_submission_uncertain(pid: int):
    """在真实点击发布前先持久化不确定态，宕机也绝不能自动重放。"""
    now = time.time()
    changed = db.execute(
        "UPDATE pub_task SET submission_state='submission_uncertain',"
        "submit_started_at=?,updated_at=? "
        "WHERE id=? AND status='running' "
        "AND submission_state='not_submitted'",
        (now, now, pid),
    )
    if changed != 1:
        raise RuntimeError("发布任务状态已变化，已停止提交")


def _cookie_pairs(cookie: str, domain: str) -> list:
    out = []
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                out.append({"name": k, "value": v, "domain": domain, "path": "/"})
    return out


async def _publish_xhs(pg, pid: int, payload: dict):
    """小红书图文发布流(创作者后台,选择器尽量宽松)."""
    await pg.goto("https://creator.xiaohongshu.com/publish/publish?source=official",
                  wait_until="domcontentloaded", timeout=60000)
    await pg.wait_for_timeout(4000)
    if "login" in pg.url or await pg.locator("text=扫码登录").count():
        raise ValueError("登录态失效:重新绑定 Cookie")
    _log(pid, "已进入发布页,切换到图文…")
    tab = pg.locator("text=上传图文")
    if await tab.count():
        await tab.first.click()
        await pg.wait_for_timeout(1500)
    imgs = [p for p in (payload.get("images") or []) if os.path.isfile(p)][:9]
    if not imgs:
        raise ValueError("没有可上传的图片(需要至少1张本地图)")
    inp = pg.locator("input[type=file]")
    await inp.first.set_input_files(imgs)
    _log(pid, f"已上传 {len(imgs)} 张图,等待处理…")
    await pg.wait_for_timeout(6000)
    title_box = pg.locator("input[placeholder*='标题'], input[placeholder*='填写标题']")
    if await title_box.count():
        await title_box.first.fill((payload.get("title") or "")[:20])
    body_box = pg.locator("div[contenteditable='true'], textarea[placeholder*='正文']")
    if await body_box.count():
        await body_box.first.click()
        await pg.keyboard.insert_text((payload.get("body") or "")[:990])
    _log(pid, "标题正文已填充，准备提交…")
    btn = pg.locator("button:has-text('发布')")
    if not await btn.count():
        raise ValueError("没找到发布按钮(平台可能改版)")
    _mark_submission_uncertain(pid)
    _log(pid, "提交发布…")
    await btn.first.click()
    await pg.wait_for_timeout(6000)
    if await pg.locator("text=发布成功").count() or "success" in pg.url:
        return "发布成功"
    return "已提交(平台未明确回执,去后台确认)"


async def _publish_douyin(pg, pid: int, payload: dict):
    """抖音视频发布流."""
    await pg.goto("https://creator.douyin.com/creator-micro/content/upload",
                  wait_until="domcontentloaded", timeout=60000)
    await pg.wait_for_timeout(4000)
    if "login" in pg.url or await pg.locator("text=扫码登录").count():
        raise ValueError("登录态失效:重新绑定 Cookie")
    video = payload.get("video")
    if not (video and os.path.isfile(video)):
        raise ValueError("没有可上传的视频文件")
    inp = pg.locator("input[type=file]")
    await inp.first.set_input_files(video)
    _log(pid, "视频上传中(等转码)…")
    await pg.wait_for_timeout(20000)
    cap = pg.locator("div[contenteditable='true'], input[placeholder*='标题']")
    if await cap.count():
        await cap.first.click()
        await pg.keyboard.insert_text((payload.get("title") or "")[:55])
    btn = pg.locator("button:has-text('发布')")
    if not await btn.count():
        raise ValueError("没找到发布按钮(平台可能改版)")
    _mark_submission_uncertain(pid)
    _log(pid, "提交发布…")
    await btn.first.click()
    await pg.wait_for_timeout(8000)
    return "已提交发布(去抖音后台确认状态)"


# 失败原因人话化:kind -> (给老板看的原因, 下一步怎么办)。浏览器自动化根治不了平台改版/风控,
# 加固思路是「失败必有出路」:每种失败都指向能立刻走通的动作(重绑Cookie / 半自动发布 / 缓一缓)。
FAIL_GUIDE = {
    "cookie": ("平台登录态已失效(Cookie 过期)",
               "重新登录该平台 → 按向导重新复制 Cookie → 点「验证」通过后再发;赶时间就用「🪄 半自动发布」"),
    "layout": ("平台后台改版,自动流程没找到对应按钮(浏览器自动化的行业通病)",
               "先用「🪄 半自动发布」四步照点发出去,我们会尽快适配新版"),
    "asset": ("素材不齐:该平台此流程需要本地图片/视频",
              "小红书需至少1张配图、抖音需成片视频;补齐素材再发,或用「🪄 半自动发布」"),
    "net": ("网络超时或平台响应太慢",
            "稍等几分钟点「🔁 重试」;还不行就用「🪄 半自动发布」"),
    "risk": ("疑似触发平台风控(操作频繁或环境异常)",
             "隔几小时再试,建议每账号每天 ≤5 条;这条稳妥起见用「🪄 半自动发布」发"),
    "unknown": ("自动发布没走通",
                "点开失败截图看现场;这条可直接用「🪄 半自动发布」四步发出去"),
}


def _classify(e: Exception) -> str:
    s = str(e)
    if "登录态" in s or "扫码登录" in s or "login" in s.lower():
        return "cookie"
    if "没找到" in s or "改版" in s:
        return "layout"
    if "没有可上传" in s:
        return "asset"
    if "频繁" in s or "验证码" in s or "风控" in s:
        return "risk"
    if "Timeout" in type(e).__name__ or "timeout" in s.lower() or "net::" in s:
        return "net"
    return "unknown"


def resume_pending(broadcast=None):
    """服务重启后:排队/进行中的发布没法断点续接(浏览器现场没了),标失败并给出路."""
    why = "服务重启,这条自动发布被中断"
    for r in db.q(
            "SELECT id,submission_state FROM pub_task "
            "WHERE status IN ('queued','running')"):
        safe = r.get("submission_state") == "not_submitted"
        fix = (
            "点「🔁 免费重试」重新排队;或用「🪄 半自动发布」"
            if safe
            else "平台可能已收到提交，请先到平台后台核对；不要直接重发"
        )
        db.update("pub_task", r["id"], {
            "status": "failed",
            "fail_json": json.dumps(
                {
                    "kind": "net",
                    "why": why,
                    "fix": fix,
                    "err": "restart",
                    "shot": "",
                    "home": "",
                },
                ensure_ascii=False,
            ),
        })
        _log(r["id"], f"❌ {why} → {fix}")


async def run_task(pid: int, broadcast=None):
    async with _sem():
        # 必须在 semaphore 内 CAS。否则两个 worker 都可能在排队前读到 queued，
        # 随后依次进入浏览器并真实发布两次。
        started = db.execute(
            "UPDATE pub_task SET status='running',updated_at=? "
            "WHERE id=? AND status='queued' "
            "AND submission_state='not_submitted'",
            (time.time(), pid),
        )
        if started != 1:
            return
        row = db.one("SELECT * FROM pub_task WHERE id=?", (pid,))
        if not row:
            return
        await _run_task_inner(pid, row, broadcast)


async def _run_task_inner(pid: int, row: dict, broadcast=None):
    tid = row["tenant_id"]
    payload = db.jloads(row["payload_json"], {})
    p = PLATFORMS[row["platform"]]
    accs = accounts(tid)
    acc = next((a for a in accs if a["id"] == row["account"]), None)
    shot_url = ""
    try:
        if not acc:
            raise ValueError("绑定账号已被删除:重新绑定后再发")
        # Cookie 预检:起浏览器(慢且吃内存)之前先花2秒探登录态,死 Cookie 快速失败
        alive, _ = await _probe_login(acc)
        if alive is False:
            acc["status"] = "expired"
            _save(tid, accs)
            raise ValueError("登录态失效:重新绑定 Cookie")
        from playwright.async_api import async_playwright
        domain = ".xiaohongshu.com" if row["platform"] == "xhs" else ".douyin.com"
        _log(pid, f"启动无头浏览器,载入「{acc['name']}」登录态…")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 900},
                                            locale="zh-CN")
            await ctx.add_cookies(_cookie_pairs(acc["cookie"], domain))
            pg = await ctx.new_page()
            try:
                note = await (_publish_xhs(pg, pid, payload) if row["platform"] == "xhs"
                              else _publish_douyin(pg, pid, payload))
                _log(pid, f"✅ {note}")
                db.update(
                    "pub_task",
                    pid,
                    {
                        "status": "done",
                        "submission_state": "submitted",
                    },
                )
            except Exception:
                shot_dir = os.path.join(ASSET_DIR, "pub")
                os.makedirs(shot_dir, exist_ok=True)
                try:
                    await pg.screenshot(path=os.path.join(shot_dir, f"fail_{pid}.png"))
                    shot_url = f"/files/pub/fail_{pid}.png"
                    _log(pid, f"失败现场截图:{shot_url}")
                except Exception:
                    pass
                raise
            finally:
                await browser.close()
    except Exception as e:
        kind = _classify(e)
        why, fix = FAIL_GUIDE[kind]
        current = db.one(
            "SELECT submission_state FROM pub_task WHERE id=?", (pid,)) or {}
        if current.get("submission_state") != "not_submitted":
            fix = (
                "平台可能已经收到提交，请先到平台后台核对；"
                "为避免重复发帖，系统不会自动重试"
            )
        fail = {"kind": kind, "why": why, "fix": fix,
                "err": "自动发布没有成功，内容未发出；可重试，或用「一键复制」手动发布",
                "shot": shot_url, "home": p["home"]}
        _log(pid, f"❌ {why} → {fix}")
        db.update("pub_task", pid, {"status": "failed",
                                    "fail_json": json.dumps(fail, ensure_ascii=False)})
        notify.push(tid, "pub", {"ok": False, "platform": p["name"],
                                 "title": payload.get("title") or "", "why": why, "fix": fix})
    else:
        # 发成功自动登记发布台账:T+1/3/7 审查官自动来复盘,不用老板记着
        try:
            pubtrack.add_entry(tid, p["name"], payload.get("title") or "",
                               job_id=payload.get("job_id"), source="matrix")
            _log(pid, "已自动登记发布台账,T+1/3/7 审查官自动复盘")
        except Exception as exc:
            log.debug(
                "发布台账登记跳过 pub_task=%s error_type=%s",
                pid,
                type(exc).__name__,
            )
        notify.push(tid, "pub", {"ok": True, "platform": p["name"],
                                 "title": payload.get("title") or ""})
    finally:
        # 发布日志是异步落库的;结束前冲刷,保证任务终态可见时日志已完整。
        await db.adrain()
        if broadcast:
            broadcast({"type": "pub_update", "task_id": pid})
