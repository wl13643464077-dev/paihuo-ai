"""V17 宣传片/PPT 素材采集:Playwright 真实操作录屏 + 2x 高清截图.

产物:data/promo/v2/rec/*.webm(录屏) + data/promo/v2/shot/*.png(截图)
"""
import json
import os
import shutil
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("PAIHUO_CAPTURE_BASE", "http://127.0.0.1:8899").rstrip("/")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data/promo/v2")
REC = os.path.join(OUT, "rec")
SHOT = os.path.join(OUT, "shot")
for d in (REC, SHOT):
    os.makedirs(d, exist_ok=True)

W, H = 1920, 1080

# 假光标:跟随鼠标 + 点击涟漪,让录屏看得见操作
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
    ctx._rec_name = name
    return ctx


def close_rec(ctx):
    """关 context 并把随机名视频改成语义名。"""
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


def wait_dash(page):
    page.wait_for_selector(".room", timeout=30000)
    page.wait_for_timeout(1200)


# ---------- 录屏各流程 ----------

def rec_promo(browser):
    ctx = new_rec_ctx(browser, "promo")
    page = ctx.new_page()
    page.goto(BASE + "/promo", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    move(page, 960, 520)
    wheel(page, 5200, chunk=110, delay=40)
    page.wait_for_timeout(800)
    close_rec(ctx)


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
    wait_dash(page)
    # 内容部楼层
    move(page, 960, 600)
    wheel(page, 1500, delay=50)
    page.wait_for_timeout(600)
    wheel(page, -1500, delay=25)
    # 切餐饮产业部(60人)
    tab = page.locator(".dt", has_text="餐饮产业部")
    tab.scroll_into_view_if_needed()
    box = tab.bounding_box()
    move(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    tab.click()
    page.wait_for_timeout(1500)
    wheel(page, 2600, delay=42)
    page.wait_for_timeout(400)
    wheel(page, -2600, delay=20)
    # 切茶饮咖啡部
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
    """给超级店长(160)真实派活,录实时步骤。"""
    ctx = new_rec_ctx(browser, "dispatch")
    api_login(ctx)
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="domcontentloaded")
    wait_dash(page)
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
              "下周门店三周年,想搞一场「老顾客答谢周」活动,预算1万。"
              "帮我扒一扒全国同类活动里做得最火的玩法,出一套能引爆同城的落地方案。",
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
        # 轻微动一下鼠标,画面不死
        move(page, 900 + int(30 * ((time.time() - t0) % 3)), 640, steps=6)
    close_rec(ctx)


def rec_report(browser):
    """打开历史任务#10 的完整交付方案,滚动展示 + 导出按钮。"""
    ctx = new_rec_ctx(browser, "report")
    api_login(ctx)
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="domcontentloaded")
    wait_dash(page)
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
    # 悬停导出按钮
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
    """真实发起会议:自动选人 → 开会 → 录专家陆续发言。"""
    ctx = new_rec_ctx(browser, "meeting")
    api_login(ctx)
    page = ctx.new_page()
    page.goto(BASE + "/#/meetings", wait_until="domcontentloaded")
    page.wait_for_selector("#mt-q", timeout=20000)
    page.wait_for_timeout(800)
    move(page, 960, 330)
    page.click("#mt-q")
    page.type("#mt-q",
              "我的火锅店周末排队爆满,可工作日冷清得可怕,房租人工照付。怎么把工作日的客流拉起来?",
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
        done = page.locator("h2", has_text="已散会").count()
        if done:
            log("会议已散会")
            page.wait_for_timeout(2500)
            wheel(page, 1200, delay=45)
            break
    close_rec(ctx)


def rec_avatar(browser):
    ctx = new_rec_ctx(browser, "avatar")
    api_login(ctx)
    page = ctx.new_page()
    page.goto(BASE + "/#/avatar", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    move(page, 960, 500)
    wheel(page, 1400, delay=50)
    vid = page.locator("video").first
    if vid.count():
        vid.scroll_into_view_if_needed()
        vb = vid.bounding_box()
        if vb:
            move(page, vb["x"] + vb["width"] / 2, vb["y"] + vb["height"] / 2)
            vid.evaluate("v => { v.muted = true; v.play(); }")
            page.wait_for_timeout(6000)
    page.wait_for_timeout(600)
    close_rec(ctx)


def rec_billing(browser):
    ctx = new_rec_ctx(browser, "billing")
    api_login(ctx)
    page = ctx.new_page()
    page.goto(BASE + "/#/billing", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    move(page, 960, 480)
    wheel(page, 2000, delay=55)
    page.wait_for_timeout(700)
    close_rec(ctx)


# ---------- 截图(2x 高清,给 PPT) ----------

def shots(browser):
    ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=2)
    api_login(ctx)
    page = ctx.new_page()

    def snap(name, full=False):
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(SHOT, name + ".png"), full_page=full)
        log(f"截图 {name}")

    page.goto(BASE + "/promo", wait_until="domcontentloaded")
    page.wait_for_timeout(1600)
    snap("promo_hero")
    page.goto(BASE + "/login", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    snap("login")
    page.goto(BASE + "/", wait_until="domcontentloaded")
    wait_dash(page)
    snap("office_content")
    page.locator(".dt", has_text="餐饮产业部").click()
    page.wait_for_timeout(1600)
    snap("office_restaurant")
    page.mouse.wheel(0, 1400)
    page.wait_for_timeout(800)
    snap("office_restaurant_floor")
    page.mouse.wheel(0, -1400)
    tab2 = page.locator(".dt", has_text="茶饮咖啡产业部")
    if tab2.count():
        tab2.first.click()
        page.wait_for_timeout(1600)
        snap("office_tea")
    # 超级店长面板(派活tab)
    page.locator(".dt", has_text="餐饮产业部").click()
    page.wait_for_timeout(1300)
    page.evaluate("openSpec(160)")
    page.wait_for_selector(".panel", timeout=15000)
    page.wait_for_timeout(900)
    snap("spec_dispatch")
    # 交付报告
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
    # 会议
    page.goto(BASE + "/#/meetings", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    # 打开最近一场已散会的会议记录
    row = page.locator(".topic", has_text="#")
    if row.count():
        row.first.click()
        page.wait_for_timeout(2000)
        mbox = page.locator("#mt-msgs")
        if mbox.count():
            mbox.first.scroll_into_view_if_needed()
        snap("meeting")
    page.goto(BASE + "/#/avatar", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    snap("avatar")
    page.goto(BASE + "/#/billing", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    snap("billing")
    page.goto(BASE + "/#/knowledge", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    snap("knowledge")
    ctx.close()



def rec_meeting_replay(browser):
    """回放已散会会议:滚动看专家互辩气泡 + 行动计划派给TA。"""
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
    # 把会议卡滚进视口
    card = page.locator("#mt-msgs")
    card.scroll_into_view_if_needed()
    page.wait_for_timeout(600)
    box = card.bounding_box()
    if box:
        move(page, box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 300))
        # 气泡区内部滚动
        card.evaluate("el => el.scrollTop = 0")
        page.wait_for_timeout(400)
        for _ in range(26):
            page.mouse.wheel(0, 150)
            page.wait_for_timeout(120)
    page.wait_for_timeout(500)
    # 行动计划
    act = page.locator("button", has_text="派给TA")
    if act.count():
        act.first.scroll_into_view_if_needed()
        ab = act.first.bounding_box()
        if ab:
            move(page, ab["x"] + ab["width"] / 2, ab["y"] + ab["height"] / 2)
            page.wait_for_timeout(1200)
    page.wait_for_timeout(600)
    close_rec(ctx)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-device-scale-factor=1",
                                          "--disable-gpu", "--font-render-hinting=none"])
        import sys as _sys
        all_fns = [rec_promo, rec_login_office, rec_dispatch, rec_report,
                   rec_meeting, rec_avatar, rec_billing, rec_meeting_replay, shots]
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
