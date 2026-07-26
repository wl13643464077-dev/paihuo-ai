"""V18 宣传片3.0/PPT3.0 素材采集:补拍 V17 没覆盖的板块.

新增:权限管理/知识沉淀库/资产库/定时任务/派单向导/流水线工单/拍板/员工能力面板/
积分账单/管理后台/摄影棚照片卡槽。
产物:data/promo/v3/rec/*.webm + data/promo/v3/shot/*.png
坑:登录后页面挂 SSE,goto 千万别用 networkidle,用 domcontentloaded。
"""
import json
import os
import shutil
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("PAIHUO_CAPTURE_BASE", "http://127.0.0.1:8899").rstrip("/")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data/promo/v3")
REC = os.path.join(OUT, "rec")
SHOT = os.path.join(OUT, "shot")
for d in (REC, SHOT):
    os.makedirs(d, exist_ok=True)

W, H = 1920, 1080

CURSOR_JS = """
(() => {
  if (window.__curReady) return; window.__curReady = 1;
  const boot = () => {
    const c = document.createElement('div');
    c.id='__cur';
    c.style.cssText='position:fixed;z-index:2147483647;width:26px;height:26px;border-radius:50%;'+
      'background:rgba(255,209,102,.92);border:3px solid #33291F;box-shadow:0 2px 10px rgba(0,0,0,.45);'+
      'pointer-events:none;transform:translate(-50%,-50%);left:-60px;top:-60px;transition:left .04s linear,top .04s linear';
    document.body.appendChild(c);
    document.addEventListener('mousemove', e => { c.style.left=e.clientX+'px'; c.style.top=e.clientY+'px'; }, true);
    document.addEventListener('mousedown', e => {
      const r = document.createElement('div');
      r.style.cssText='position:fixed;z-index:2147483646;width:26px;height:26px;border-radius:50%;'+
        'border:4px solid #FFD166;pointer-events:none;transform:translate(-50%,-50%);'+
        'left:'+e.clientX+'px;top:'+e.clientY+'px;animation:__rip .5s ease-out forwards';
      document.body.appendChild(r); setTimeout(()=>r.remove(), 550);
    }, true);
    const st = document.createElement('style');
    st.textContent='@keyframes __rip{to{width:90px;height:90px;opacity:0}}';
    document.head.appendChild(st);
  };
  if (document.body) boot(); else document.addEventListener('DOMContentLoaded', boot);
})();
"""


# 无价格演示模式:录制/截图前持续抹掉界面上的金额(按钮标价/点数/余额)
SCRUB_JS = """
(() => {
  if (window.__scrubOn) return; window.__scrubOn = 1;
  const scrub = () => {
    try {
      const it = document.createNodeIterator(document.body, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = it.nextNode())) {
        let s = n.nodeValue;
        if (!s || !/[¥¥]|\\d ?点|积分|余额/.test(s)) continue;
        s = s.replace(/[((][^())]*[¥¥][^())]*[))]/g, "")
             .replace(/[¥¥]\\s?\\d+(\\.\\d+)?(\\s?起)?([/／][^\\s··]+)?/g, "")
             .replace(/\\d+(\\.\\d+)?\\s?点(\\/[人单条次])?/g, "")
             .replace(/(积分|余额)\\s*[::]?\\s*[+-]?\\d+(\\.\\d+)?/g, "$1")
             .replace(/ ?· ?(?=[))]?$)/, "");
        if (s !== n.nodeValue) n.nodeValue = s;
      }
    } catch (e) {}
  };
  const boot = () => { scrub(); setInterval(scrub, 350); };
  if (document.body) boot(); else document.addEventListener('DOMContentLoaded', boot);
})();
"""

# 账单页专用:金额数字列打码(模糊),保留"逐笔记账"的观感
BILL_BLUR_CSS = """
.dimtable td:nth-child(4), .dimtable td:nth-child(5), .stat, .kv b { filter: blur(7px); }
"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def capture_credentials():
    """宣传素材脚本不携带任何默认账号口令。"""
    username = os.environ.get("PAIHUO_CAPTURE_USERNAME", "").strip()
    password = os.environ.get("PAIHUO_CAPTURE_PASSWORD", "")
    if not username or not password:
        raise RuntimeError(
            "请通过 PAIHUO_CAPTURE_USERNAME/PAIHUO_CAPTURE_PASSWORD "
            "注入专用测试账号；脚本不含默认凭据"
        )
    return username, password


def move(page, x, y, steps=28):
    page.mouse.move(x, y, steps=steps)


def hover_el(page, loc, steps=16, pause=800):
    loc.scroll_into_view_if_needed()
    b = loc.bounding_box()
    if b:
        move(page, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=steps)
        page.wait_for_timeout(pause)
    return b


def click_el(page, loc, steps=16):
    b = hover_el(page, loc, steps=steps, pause=350)
    loc.click()
    return b


def wheel(page, total, chunk=130, delay=45):
    done = 0
    while abs(done) < abs(total):
        step = chunk if total > 0 else -chunk
        page.mouse.wheel(0, step)
        done += step
        page.wait_for_timeout(delay)


def new_rec_ctx(browser, name):
    ctx = browser.new_context(viewport={"width": W, "height": H},
                              record_video_dir=REC,
                              record_video_size={"width": W, "height": H})
    ctx.add_init_script(CURSOR_JS)
    ctx.add_init_script(SCRUB_JS)
    ctx._rec_name = name
    return ctx


def close_rec(ctx):
    name = ctx._rec_name
    paths = [p.video.path() for p in ctx.pages if p.video]
    ctx.close()
    if paths:
        dst = os.path.join(REC, name + ".webm")
        shutil.move(paths[0], dst)
        log(f"录屏 → {dst} ({os.path.getsize(dst)//1024}KB)")


def api_login(ctx):
    username, password = capture_credentials()
    r = ctx.request.post(BASE + "/api/auth/login",
                         data=json.dumps({"username": username, "password": password}),
                         headers={"Content-Type": "application/json"})
    assert r.ok, r.status


def open_page(ctx, hash_, sel=None, wait=1800):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(BASE + "/" + hash_, wait_until="domcontentloaded")
    if sel:
        page.wait_for_selector(sel, timeout=25000)
    page.wait_for_timeout(wait)
    return page


# ---------- 录屏 ----------

def rec_team(browser):
    """权限管理:企业/成员/板块授权/租户管理——一人一岗数据隔离."""
    ctx = new_rec_ctx(browser, "team")
    api_login(ctx)
    page = open_page(ctx, "#/team", ".card")
    move(page, 960, 400)
    wheel(page, 900, delay=55)
    # 悬停一个副账号的「板块」按钮
    mod = page.locator("button", has_text="🧩 板块")
    if mod.count():
        hover_el(page, mod.first, pause=1100)
    wheel(page, 1600, delay=50)
    # 租户列表(root)悬停充值
    for label in ("💰 充值", "开套餐"):
        b = page.locator("button", has_text=label)
        if b.count():
            hover_el(page, b.first, pause=900)
            break
    wheel(page, 1200, delay=50)
    page.wait_for_timeout(600)
    close_rec(ctx)


def rec_knowledge(browser):
    """知识沉淀库:11维评估表格 + 置顶注入."""
    ctx = new_rec_ctx(browser, "knowledge")
    api_login(ctx)
    page = open_page(ctx, "#/knowledge", ".dimtable")
    move(page, 960, 420)
    page.wait_for_timeout(700)
    wheel(page, 1100, chunk=110, delay=55)
    pin = page.locator("button", has_text="取消置顶")
    if pin.count():
        hover_el(page, pin.first, pause=1000)
    wheel(page, 900, delay=55)
    page.wait_for_timeout(700)
    close_rec(ctx)


def rec_assets(browser):
    """资产库:选题库/成品库/专家报告三个tab + 11维筛选."""
    ctx = new_rec_ctx(browser, "assets")
    api_login(ctx)
    page = open_page(ctx, "#/assets", ".card")
    move(page, 960, 380)
    wheel(page, 900, chunk=110, delay=50)
    for tab in ("📦 成品库", "📄 专家报告"):
        t = page.locator(".tb", has_text=tab)
        if t.count():
            click_el(page, t.first)
            page.wait_for_timeout(1400)
            wheel(page, 800, delay=50)
    page.wait_for_timeout(600)
    close_rec(ctx)


def rec_schedules(browser):
    """定时任务:到点自动开工."""
    ctx = new_rec_ctx(browser, "schedules")
    api_login(ctx)
    page = open_page(ctx, "#/schedules", ".card")
    move(page, 960, 380)
    page.wait_for_timeout(800)
    run = page.locator("button", has_text="▶️ 立即来一单")
    if run.count():
        hover_el(page, run.first, pause=1200)
    sw = page.locator(".switch")
    if sw.count():
        hover_el(page, sw.first, pause=900)
    page.wait_for_timeout(700)
    close_rec(ctx)


def rec_new(browser):
    """派单向导:填方向、选行业/平台——不真提交."""
    ctx = new_rec_ctx(browser, "new")
    api_login(ctx)
    page = open_page(ctx, "#/new", "#b-dir")
    move(page, 960, 340)
    page.click("#b-dir")
    page.type("#b-dir", "门店三周年,出一整套「老顾客答谢周」传播内容:短视频脚本+图文+海报", delay=30)
    ind = page.locator("#b-ind")
    if ind.count():
        click_el(page, ind.first)
        page.wait_for_timeout(300)
        page.select_option("#b-ind", label="餐饮") if ind.first.evaluate("el=>el.tagName") == "SELECT" else None
    # 平台 chips
    for pf in ("小红书", "抖音"):
        c = page.locator(".chip", has_text=pf)
        if c.count():
            click_el(page, c.first)
            page.wait_for_timeout(350)
    wheel(page, 700, delay=55)
    go = page.locator("button", has_text="流水线")
    if not go.count():
        go = page.locator("button.pri").last
    if go.count():
        hover_el(page, go.first, pause=1400)
    page.wait_for_timeout(500)
    close_rec(ctx)


def rec_jobflow(browser):
    """流水线工单:10工位进度 + 步骤级工作日志."""
    ctx = new_rec_ctx(browser, "jobflow")
    api_login(ctx)
    page = open_page(ctx, "#/job/5", ".card", wait=2200)
    move(page, 960, 420)
    wheel(page, 1500, chunk=110, delay=55)
    page.wait_for_timeout(600)
    wheel(page, 2600, chunk=130, delay=45)
    page.wait_for_timeout(700)
    close_rec(ctx)


def rec_delivery(browser):
    """交付包:全平台发布包 + 一键导出."""
    ctx = new_rec_ctx(browser, "delivery")
    api_login(ctx)
    page = open_page(ctx, "#/delivery/5", ".card", wait=2200)
    move(page, 960, 430)
    wheel(page, 2200, chunk=110, delay=55)
    for label in ("PDF", "pack.zip", "打包"):
        a = page.locator("a.btn", has_text=label)
        if a.count():
            hover_el(page, a.first, pause=900)
            break
    wheel(page, 1400, delay=50)
    page.wait_for_timeout(600)
    close_rec(ctx)


def rec_review(browser):
    """等您拍板:质检+终审,通过/打回都是老板一句话."""
    ctx = new_rec_ctx(browser, "review")
    api_login(ctx)
    page = open_page(ctx, "#/job/6", ".card", wait=2200)
    move(page, 960, 420)
    wheel(page, 1600, chunk=110, delay=55)
    for label in ("通过", "拍板"):
        b = page.locator("button", has_text=label)
        if b.count():
            hover_el(page, b.first, pause=1200)
            break
    page.wait_for_timeout(600)
    close_rec(ctx)


def rec_emp(browser):
    """员工面板:能力矩阵 → 学到的技能 → 工作方式."""
    ctx = new_rec_ctx(browser, "emp")
    api_login(ctx)
    page = open_page(ctx, "", ".room", wait=1500)
    room = page.locator('[data-room="0"]')
    if room.count():
        click_el(page, room.first)
    else:
        page.evaluate("openEmp(0)")
    page.wait_for_selector(".modal, .panel", timeout=15000)
    page.wait_for_timeout(1300)
    wheel(page, 700, delay=55)
    for tab in ("技能", "工作方式"):
        t = page.locator(".tb", has_text=tab)
        if t.count():
            click_el(page, t.first)
            page.wait_for_timeout(1500)
            wheel(page, 900, delay=50)
    page.wait_for_timeout(600)
    close_rec(ctx)


def rec_billing2(browser):
    """积分账户:余额大卡 + 充值/消耗明细(金额打码)."""
    ctx = new_rec_ctx(browser, "billing2")
    api_login(ctx)
    page = open_page(ctx, "#/billing", ".card")
    page.add_style_tag(content=BILL_BLUR_CSS)
    move(page, 960, 360)
    page.wait_for_timeout(900)
    for tab in ("💰 充值记录", "🔻 消耗记录"):
        t = page.locator(".tb", has_text=tab)
        if t.count():
            click_el(page, t.first)
            page.wait_for_timeout(1300)
            wheel(page, 900, delay=50)
    page.wait_for_timeout(600)
    close_rec(ctx)


def rec_avatar2(browser):
    """摄影棚:照片卡槽 + 历史成片(HeyGen/可灵/基础版)."""
    ctx = new_rec_ctx(browser, "avatar2")
    api_login(ctx)
    page = open_page(ctx, "#/avatar", ".card", wait=2200)
    move(page, 960, 420)
    wheel(page, 900, chunk=110, delay=50)
    imgs = page.locator("#av-photo-box img")
    if imgs.count() > 1:
        click_el(page, imgs.nth(imgs.count() - 2))
        page.wait_for_timeout(1200)
    wheel(page, 1600, delay=50)
    vid = page.locator("video").first
    if vid.count():
        vid.scroll_into_view_if_needed()
        vid.evaluate("v => { v.muted = true; v.play(); }")
        page.wait_for_timeout(6500)
    page.wait_for_timeout(500)
    close_rec(ctx)


def rec_admin(browser):
    """管理后台:供应商/模型路由/员工级模型."""
    ctx = new_rec_ctx(browser, "admin")
    api_login(ctx)
    page = open_page(ctx, "#/admin", ".card", wait=2200)
    move(page, 960, 420)
    wheel(page, 2400, chunk=120, delay=50)
    page.wait_for_timeout(700)
    close_rec(ctx)


# ---------- V2 场景重录(带无价格模式) ----------

def rec_login_office(browser):
    ctx = new_rec_ctx(browser, "login_office")
    page = ctx.new_page()
    username, password = capture_credentials()
    page.goto(BASE + "/login", wait_until="domcontentloaded")
    page.wait_for_timeout(900)
    move(page, 960, 430)
    page.click("#u")
    page.type("#u", username, delay=95)
    move(page, 960, 500)
    page.click("#p")
    page.type("#p", password, delay=85)
    page.wait_for_timeout(350)
    page.click("button")
    page.wait_for_url(BASE + "/*", timeout=20000)
    page.wait_for_selector(".room", timeout=30000)
    page.wait_for_timeout(1200)
    move(page, 960, 600)
    wheel(page, 1500, delay=50)
    page.wait_for_timeout(600)
    wheel(page, -1500, delay=25)
    tab = page.locator(".dt", has_text="餐饮产业部")
    tab.scroll_into_view_if_needed()
    b = tab.bounding_box()
    move(page, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    tab.click()
    page.wait_for_timeout(1500)
    wheel(page, 2600, delay=42)
    page.wait_for_timeout(400)
    wheel(page, -2600, delay=20)
    tab2 = page.locator(".dt", has_text="茶饮咖啡产业部")
    if tab2.count():
        tab2.first.scroll_into_view_if_needed()
        b2 = tab2.first.bounding_box()
        move(page, b2["x"] + b2["width"] / 2, b2["y"] + b2["height"] / 2)
        tab2.first.click()
        page.wait_for_timeout(1600)
        wheel(page, 1800, delay=45)
    page.wait_for_timeout(700)
    close_rec(ctx)


def rec_dispatch(browser):
    """真实给超级店长派活(会真跑任务)."""
    ctx = new_rec_ctx(browser, "dispatch")
    api_login(ctx)
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_selector(".room", timeout=30000)
    page.wait_for_timeout(1200)
    page.locator(".dt", has_text="餐饮产业部").click()
    page.wait_for_timeout(1400)
    room = page.locator('[data-room="160"]')
    room.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    rb = room.bounding_box()
    move(page, rb["x"] + rb["width"] / 2, rb["y"] + rb["height"] / 2)
    page.wait_for_timeout(400)
    btn = room.locator("button.pri")
    bb = btn.bounding_box()
    move(page, bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2, steps=15)
    btn.click()
    page.wait_for_selector("#spec-dir", timeout=15000)
    page.wait_for_timeout(700)
    page.click("#spec-dir")
    page.type("#spec-dir",
              "暑假到了,想做一场「学生証免费换饮品」的引流活动,预算8千。"
              "帮我扒全国同类活动的爆点和翻车点,出一套能落地的执行方案。",
              delay=26)
    page.click("#spec-industry")
    page.type("#spec-industry", "川渝火锅", delay=45)
    page.wait_for_timeout(400)
    go = page.locator("button", has_text="🚀 派活")
    gb = go.bounding_box()
    move(page, gb["x"] + gb["width"] / 2, gb["y"] + gb["height"] / 2, steps=12)
    go.click()
    page.wait_for_selector("#spec-steps", timeout=30000)
    log("任务已下发,录制实时步骤…")
    t0 = time.time()
    while time.time() - t0 < 130:
        page.wait_for_timeout(4000)
        move(page, 900 + int(30 * ((time.time() - t0) % 3)), 640, steps=6)
    close_rec(ctx)


def rec_report(browser):
    ctx = new_rec_ctx(browser, "report")
    api_login(ctx)
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_selector(".room", timeout=30000)
    page.wait_for_timeout(1200)
    page.evaluate("openSpec(102)")
    page.wait_for_selector(".panel", timeout=15000)
    page.wait_for_timeout(1000)
    topic = page.locator(".topic", has_text="#10")
    topic.scroll_into_view_if_needed()
    tb = topic.bounding_box()
    move(page, tb["x"] + 200, tb["y"] + tb["height"] / 2)
    topic.click()
    page.wait_for_selector(".panel .md", timeout=15000)
    page.wait_for_timeout(900)
    move(page, 960, 560)
    wheel(page, 5200, chunk=100, delay=55)
    page.wait_for_timeout(500)
    pdf = page.locator('a.btn', has_text="PDF").first
    if pdf.count():
        pdf.scroll_into_view_if_needed()
        pb = pdf.bounding_box()
        if pb:
            move(page, pb["x"] + pb["width"] / 2, pb["y"] + pb["height"] / 2)
            page.wait_for_timeout(900)
            w = page.locator('a.btn', has_text="Word").first
            wb = w.bounding_box()
            if wb:
                move(page, wb["x"] + wb["width"] / 2, wb["y"] + wb["height"] / 2, steps=10)
                page.wait_for_timeout(900)
    close_rec(ctx)


def rec_meeting(browser):
    """真实发起会议(会真跑,3-4分钟)."""
    ctx = new_rec_ctx(browser, "meeting")
    api_login(ctx)
    page = ctx.new_page()
    page.goto(BASE + "/#/meetings", wait_until="domcontentloaded")
    page.wait_for_selector("#mt-q", timeout=20000)
    page.wait_for_timeout(800)
    move(page, 960, 330)
    page.click("#mt-q")
    page.type("#mt-q",
              "外卖平台抽成越来越高,堂食又起不来。要不要自建小程序做私域外卖?怎么起步?",
              delay=30)
    sug = page.locator("button", has_text="自动选人")
    sb = sug.bounding_box()
    move(page, sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2, steps=14)
    sug.click()
    page.wait_for_function("document.querySelectorAll('.mt-chip.on').length >= 2", timeout=90000)
    page.wait_for_timeout(1200)
    start = page.locator("button", has_text="🪑 开会")
    start.scroll_into_view_if_needed()
    stb = start.bounding_box()
    move(page, stb["x"] + stb["width"] / 2, stb["y"] + stb["height"] / 2, steps=14)
    start.click()
    page.wait_for_selector("#mt-msgs", timeout=30000)
    log("会议开始,录发言…")
    t0 = time.time()
    while time.time() - t0 < 210:
        page.wait_for_timeout(5000)
        move(page, 950 + int(25 * ((time.time() - t0) % 4)), 700, steps=5)
        if page.locator("h2", has_text="已散会").count():
            log("会议已散会")
            page.wait_for_timeout(2500)
            wheel(page, 1200, delay=45)
            break
    close_rec(ctx)


def rec_meeting_replay(browser):
    ctx = new_rec_ctx(browser, "meeting_replay")
    api_login(ctx)
    page = ctx.new_page()
    page.goto(BASE + "/#/meetings", wait_until="domcontentloaded")
    page.wait_for_selector(".topic", timeout=20000)
    page.wait_for_timeout(800)
    row = page.locator(".topic").first
    rb = row.bounding_box()
    move(page, rb["x"] + 300, rb["y"] + rb["height"] / 2)
    row.click()
    page.wait_for_selector("#mt-msgs", timeout=20000)
    page.wait_for_timeout(1000)
    card = page.locator("#mt-msgs")
    card.scroll_into_view_if_needed()
    page.wait_for_timeout(600)
    box = card.bounding_box()
    if box:
        move(page, box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 300))
        card.evaluate("el => el.scrollTop = 0")
        page.wait_for_timeout(400)
        for _ in range(26):
            page.mouse.wheel(0, 150)
            page.wait_for_timeout(120)
    page.wait_for_timeout(500)
    act = page.locator("button", has_text="派给TA")
    if act.count():
        act.first.scroll_into_view_if_needed()
        ab = act.first.bounding_box()
        if ab:
            move(page, ab["x"] + ab["width"] / 2, ab["y"] + ab["height"] / 2)
            page.wait_for_timeout(1200)
    page.wait_for_timeout(600)
    close_rec(ctx)


# ---------- 截图(2x,给PPT) ----------

def shots(browser):
    ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=2)
    ctx.add_init_script(SCRUB_JS)
    api_login(ctx)
    page = ctx.new_page()

    def snap(name, full=False):
        page.wait_for_timeout(500)
        page.screenshot(path=os.path.join(SHOT, name + ".png"), full_page=full)
        log(f"截图 {name}")

    def goto(hash_, sel=None, wait=1800):
        page.goto(BASE + "/" + hash_, wait_until="domcontentloaded")
        if sel:
            page.wait_for_selector(sel, timeout=25000)
        page.wait_for_timeout(wait)

    # V2 同名截图重拍(无价格),PPT 的 shot_path 会优先取 v3 版
    goto("", ".room", 1500); snap("office_content")
    page.locator(".dt", has_text="餐饮产业部").click()
    page.wait_for_timeout(1600); snap("office_restaurant")
    page.evaluate("openSpec(160)")
    page.wait_for_selector(".panel", timeout=15000)
    page.wait_for_timeout(900); snap("spec_dispatch")
    page.evaluate("closeSpec(); openSpec(102)")
    page.wait_for_selector(".panel", timeout=15000)
    page.wait_for_timeout(700)
    t = page.locator(".topic", has_text="#10")
    if t.count():
        t.first.click()
        page.wait_for_selector(".panel .md", timeout=15000)
        page.wait_for_timeout(800)
        snap("report")
        page.locator(".panel").first.evaluate("el => el.scrollTop = 600")
        page.wait_for_timeout(500)
        snap("report_body")
    page.evaluate("closeSpec()")
    goto("#/meetings", ".card", 2000)
    row = page.locator(".topic")
    if row.count():
        row.first.click()
        page.wait_for_timeout(2000)
        mbox = page.locator("#mt-msgs")
        if mbox.count():
            mbox.first.scroll_into_view_if_needed()
        snap("meeting")

    goto("#/team", ".card"); snap("team")
    goto("#/knowledge", ".dimtable"); snap("knowledge2")
    goto("#/assets", ".card"); snap("assets_topic")
    t = page.locator(".tb", has_text="📄 专家报告")
    if t.count():
        t.first.click(); page.wait_for_timeout(1400); snap("assets_report")
    goto("#/schedules", ".card"); snap("schedules")
    goto("#/new", "#b-dir")
    page.fill("#b-dir", "门店三周年,出一整套「老顾客答谢周」传播内容")
    page.wait_for_timeout(400); snap("new_wizard")
    goto("#/job/5", ".card", 2200); snap("job_pipeline")
    page.mouse.wheel(0, 2200); page.wait_for_timeout(700); snap("job_log")
    goto("#/job/6", ".card", 2200); snap("review")
    goto("#/delivery/5", ".card", 2200); snap("delivery")
    page.mouse.wheel(0, 1800); page.wait_for_timeout(700); snap("delivery_pack")
    goto("", ".room", 1500)
    page.evaluate("openEmp(0)")
    page.wait_for_timeout(1500); snap("emp_caps")
    tb = page.locator(".tb", has_text="技能")
    if tb.count():
        tb.first.click(); page.wait_for_timeout(1200); snap("emp_skills")
    tb = page.locator(".tb", has_text="工作方式")
    if tb.count():
        tb.first.click(); page.wait_for_timeout(1200); snap("emp_method")
    goto("#/billing", ".card")
    page.add_style_tag(content=BILL_BLUR_CSS)
    snap("billing_all")
    t = page.locator(".tb", has_text="🔻 消耗记录")
    if t.count():
        t.first.click(); page.wait_for_timeout(1200)
        page.add_style_tag(content=BILL_BLUR_CSS)
        snap("billing_out")
    goto("#/admin", ".card", 2200); snap("admin")
    goto("#/avatar", ".card", 2200); snap("avatar_slots")
    goto("#/meetings", ".card", 2000); snap("meetings_list")
    goto("#/profiles", ".card", 2000); snap("profiles")
    ctx.close()


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-device-scale-factor=1",
                                          "--disable-gpu", "--font-render-hinting=none"])
        import sys as _sys
        all_fns = [rec_team, rec_knowledge, rec_assets, rec_schedules, rec_new,
                   rec_jobflow, rec_delivery, rec_review, rec_emp, rec_billing2,
                   rec_avatar2, rec_admin,
                   rec_login_office, rec_dispatch, rec_report, rec_meeting,
                   rec_meeting_replay, shots]
        want = _sys.argv[1:]
        for fn in [f for f in all_fns if not want or f.__name__ in want]:
            log(f"===== {fn.__name__} =====")
            try:
                fn(browser)
            except Exception as e:
                log(f"!! {fn.__name__} 失败: {e}")
        browser.close()
    log("采集完成")


if __name__ == "__main__":
    main()
