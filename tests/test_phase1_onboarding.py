"""第 1 期：新老板首次上手 + 自助开户不强制改密的行为测试（不依赖 fastapi）。"""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app import auth, db, departments, onboarding, signup
from app.skills import registry


def _valid_keys():
    return [d["key"] for d in departments.list_depts()]


GOOD_POSTS = {"posts": [
    {"platform": "朋友圈", "text": "周末来吃碗热汤面～", "tip": "配一张出锅图"},
    {"platform": "小红书", "text": "成都春熙路藏着一家牛肉面\n#成都美食", "tip": "中午 11 点发"},
    {"platform": "大众点评", "text": "汤底每天熬 8 小时，欢迎到店", "tip": ""},
]}


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "onboarding.db")
        db.conn()
        if not db.one("SELECT id FROM tenants WHERE id=1"):
            db.insert("tenants", {"name": "平台总部"})

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _tenant(self, name="新客"):
        return db.insert("tenants", {"name": name, "industries_json": "[]"})

    def _owner(self, tid, system_generated=True):
        uid = auth.create_owner_account(
            tid, f"1380000{tid:04d}", auth.generate_initial_password(),
            system_generated=system_generated,
        )
        return auth.get_user(uid)


class InitialPasswordPolicyCase(_DbCase):
    def test_generated_password_is_strong_and_random(self):
        seen = set()
        for _ in range(50):
            password = auth.generate_initial_password()
            self.assertEqual(16, len(password))
            self.assertEqual("", auth.password_policy_error(password))
            self.assertTrue(any(c.isdigit() for c in password))
            self.assertTrue(any(c.isalpha() for c in password))
            seen.add(password)
        self.assertEqual(50, len(seen))

    def test_self_service_owner_is_not_forced_to_change_password(self):
        tid = self._tenant()
        owner = self._owner(tid, system_generated=True)
        self.assertEqual(0, owner["must_change_password"])
        self.assertEqual("owner", owner["role"])
        self.assertTrue(auth.password_hint_pending(owner["id"]))
        # 会话可正常签发和解析：登录后直接进首页，不会被 428 拦住。
        self.assertEqual(owner["id"], auth.parse_session(auth.make_session(owner["id"])))

    def test_admin_set_password_still_forces_change(self):
        tid = self._tenant()
        uid = auth.create_owner_account(
            tid, "manual-owner", "Manual-Pass-2026", system_generated=False
        )
        row = db.one("SELECT must_change_password FROM users WHERE id=?", (uid,))
        self.assertEqual(1, row["must_change_password"])
        self.assertFalse(auth.password_hint_pending(uid))

    def test_hint_cleared_after_password_change(self):
        tid = self._tenant()
        owner = self._owner(tid)
        auth.clear_password_hint(owner["id"])
        self.assertFalse(auth.password_hint_pending(owner["id"]))
        state = onboarding.get_state(tid, auth.get_user(owner["id"]))
        self.assertFalse(state["password_hint"])

    def test_create_owner_rolls_back_with_outer_transaction(self):
        tid = self._tenant()
        with self.assertRaises(RuntimeError):
            with db.atomic():
                uid = auth.create_owner_account(
                    tid, "rollback-owner", "x" * 4 + "Pass-2026",
                    system_generated=True,
                )
                raise RuntimeError("开户中途失败")
        self.assertIsNone(db.one("SELECT id FROM users WHERE username='rollback-owner'"))
        self.assertFalse(auth.password_hint_pending(uid))


class OnboardingStateCase(_DbCase):
    def test_only_owner_of_real_tenant_sees_card(self):
        tid = self._tenant()
        owner = self._owner(tid)
        self.assertTrue(onboarding.get_state(tid, owner)["show"])
        member = {"id": 5, "tenant_id": tid, "role": "member"}
        self.assertEqual({"show": False}, onboarding.get_state(tid, member))
        root = {"id": 1, "tenant_id": 1, "role": "root"}
        self.assertEqual({"show": False}, onboarding.get_state(1, root))
        self.assertEqual({"show": False}, onboarding.get_state(-1, {"role": "tour", "tenant_id": -1}))

    def test_industry_step_skipped_once_chosen(self):
        tid = self._tenant()
        owner = self._owner(tid)
        state = onboarding.get_state(tid, owner)
        self.assertFalse(state["steps"]["industry"])
        signup.claim_first_industry(tid, "restaurant", _valid_keys())
        state = onboarding.get_state(tid, owner)
        self.assertTrue(state["steps"]["industry"])
        self.assertEqual("restaurant", state["industry"]["key"])
        self.assertEqual("餐饮", state["industry"]["name"])
        self.assertEqual(3, state["gen_left"])
        self.assertTrue(state["password_hint"])

    def test_dismiss_is_persisted_server_side(self):
        tid = self._tenant()
        owner = self._owner(tid)
        onboarding.dismiss(tid)
        self.assertFalse(onboarding.get_state(tid, owner)["show"])
        other = self._tenant("隔壁店")
        self.assertTrue(onboarding.get_state(other, self._owner(other))["show"])


class ProgressFlagsCase(_DbCase):
    """R-4 / O-4:上手卡「接下来」的进度每次从服务端重取，只看本企业，清单自动生成的活不算派过活。"""

    def test_progress_flags_are_tenant_scoped(self):
        tid = self._tenant()
        owner = self._owner(tid)
        other = self._tenant("隔壁店")
        self.assertEqual({"store": False, "staff": False, "staff_task": False},
                         onboarding.get_state(tid, owner)["progress"])
        # 隔壁企业的门店、店员、派活都不能算到这家头上
        db.insert("store_branch", {"tenant_id": other, "industry_key": "restaurant", "name": "隔壁店"})
        db.insert("users", {"tenant_id": other, "username": "other-staff", "password_hash": "x",
                            "role": "member"})
        db.insert("staff_task", {"tenant_id": other, "title": "擦桌子", "source": "boss"})
        self.assertEqual({"store": False, "staff": False, "staff_task": False},
                         onboarding.progress_flags(tid))
        # 开闭店清单是系统生成的，不算老板派过第一件活
        db.insert("staff_task", {"tenant_id": tid, "title": "开店检查", "source": "checklist"})
        self.assertFalse(onboarding.progress_flags(tid)["staff_task"])
        db.insert("store_branch", {"tenant_id": tid, "industry_key": "restaurant", "name": "人民路店"})
        db.insert("users", {"tenant_id": tid, "username": "xiaoli", "password_hash": "x",
                            "role": "member"})
        db.insert("staff_task", {"tenant_id": tid, "title": "拍门头", "source": "boss"})
        self.assertEqual({"store": True, "staff": True, "staff_task": True},
                         onboarding.get_state(tid, owner)["progress"])


class StoreInfoCase(_DbCase):
    def test_validation_messages(self):
        tid = self._tenant()
        cases = [
            ({"product": "牛肉面"}, "店名"),
            ({"name": "王记"}, "卖什么"),
            ({"name": "王记", "product": "面", "price": "三十"}, "数字"),
            ({"name": "王记", "product": "面", "price": "-5"}, "之间"),
            ({"name": "王记", "product": "面", "price": "999999"}, "之间"),
        ]
        for body, words in cases:
            with self.subTest(body=body):
                with self.assertRaises(onboarding.OnboardingError) as ctx:
                    onboarding.save_store(tid, body)
                self.assertEqual(400, ctx.exception.status)
                self.assertIn(words, str(ctx.exception))

    def test_store_info_merges_into_existing_company_profile(self):
        tid = self._tenant()
        old = {"brand": "旧名", "business": "旧业务", "tone": "亲切接地气", "taboo": "不说最便宜"}
        db.set_setting(f"company_profile:{tid}", json.dumps(old, ensure_ascii=False))
        onboarding.save_store(tid, {
            "name": " 王记牛肉面 ", "city": "成都 春熙路", "product": "现熬牛骨汤面",
            "feature": "汤底每天熬 8 小时", "price": "35",
        })
        prof = json.loads(db.get_setting(f"company_profile:{tid}"))
        self.assertEqual("王记牛肉面", prof["brand"])
        self.assertEqual("现熬牛骨汤面（门店在成都 春熙路，客单价约35元）", prof["business"])
        self.assertEqual("汤底每天熬 8 小时", prof["selling_points"])
        # 老板以前调好的其他字段原样保留
        self.assertEqual("亲切接地气", prof["tone"])
        self.assertEqual("不说最便宜", prof["taboo"])
        # 覆盖前的版本进 prev，企业档案页「撤销」可以换回
        self.assertEqual(old, json.loads(db.get_setting(f"company_profile_prev:{tid}")))
        # 所有数字员工读到的企业档案块里已经有店铺信息
        block = registry.company_block(tid)
        self.assertIn("王记牛肉面", block)
        self.assertIn("春熙路", block)
        self.assertIn("汤底每天熬 8 小时", block)
        basics = onboarding.store_basics(tid)
        self.assertEqual({"name": "王记牛肉面", "city": "成都 春熙路",
                          "product": "现熬牛骨汤面", "feature": "汤底每天熬 8 小时",
                          "price": 35}, basics)

    def test_existing_profile_prefills_and_counts_as_done(self):
        tid = self._tenant()
        owner = self._owner(tid)
        db.set_setting(f"company_profile:{tid}", json.dumps(
            {"brand": "小美美甲", "business": "日式美甲", "selling_points": "不伤甲"},
            ensure_ascii=False))
        state = onboarding.get_state(tid, owner)
        self.assertTrue(state["steps"]["store"])
        self.assertEqual("小美美甲", state["store"]["name"])
        self.assertEqual("日式美甲", state["store"]["product"])

    def test_optional_fields_can_be_empty(self):
        tid = self._tenant()
        onboarding.save_store(tid, {"name": "阿强汽修", "product": "保养换油"})
        prof = json.loads(db.get_setting(f"company_profile:{tid}"))
        self.assertEqual("保养换油", prof["business"])
        self.assertNotIn("selling_points", prof)
        self.assertIsNone(db.get_setting(f"company_profile_prev:{tid}"))


class PromptAndParseCase(unittest.TestCase):
    def test_prompt_contains_store_industry_and_beijing_date(self):
        prompt = onboarding.build_prompt(
            {"name": "王记", "city": "成都", "product": "牛肉面", "feature": "现拉面", "price": 35},
            "餐饮", "2026-09-26", "六",
        )
        for words in ("王记", "成都", "牛肉面", "现拉面", "约35元", "餐饮",
                      "2026-09-26", "星期六", "朋友圈", "小红书", "大众点评"):
            self.assertIn(words, prompt)

    def test_prompt_omits_empty_optional_fields(self):
        prompt = onboarding.build_prompt({"name": "王记", "product": "面"}, "", "2026-01-01", "四")
        self.assertNotIn("客单价：", prompt)
        self.assertNotIn("城市/商圈：", prompt)

    def test_normalize_orders_platforms_and_accepts_aliases(self):
        posts = onboarding.normalize_posts({"posts": [
            {"platform": "点评", "text": " 到店体验 "},
            {"platform": "微信朋友圈", "text": "朋友圈正文", "tip": "x" * 200},
            {"platform": "小红书笔记", "body": "红书正文"},
            {"platform": "小红书", "text": "重复的被忽略"},
            "垃圾项",
        ]})
        self.assertEqual(["朋友圈", "小红书", "大众点评"], [p["platform"] for p in posts])
        self.assertEqual("到店体验", posts[2]["text"])
        self.assertEqual("红书正文", posts[1]["text"])
        self.assertEqual(onboarding.POST_TIP_MAX, len(posts[0]["tip"]))

    def test_normalize_rejects_incomplete_output(self):
        for bad in (None, {}, {"posts": "x"},
                    {"posts": [{"platform": "朋友圈", "text": "a"},
                               {"platform": "小红书", "text": ""},
                               {"platform": "大众点评", "text": "c"}]}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    onboarding.normalize_posts(bad)


class GeneratePostsCase(_DbCase):
    def _ready_tenant(self):
        tid = self._tenant()
        signup.claim_first_industry(tid, "restaurant", _valid_keys())
        onboarding.save_store(tid, {"name": "王记牛肉面", "city": "成都",
                                    "product": "牛肉面", "price": 35})
        return tid

    def test_success_saves_posts_marks_done_and_uses_beijing_date(self):
        tid = self._ready_tenant()
        owner = self._owner(tid)
        seen = []

        async def fake(prompt, tenant):
            seen.append((prompt, tenant))
            return {"data": GOOD_POSTS, "cost_usd": 0.001}

        # UTC 9/25 17:00 已经是北京时间 9/26 周六凌晨
        ts = datetime(2026, 9, 25, 17, 0, tzinfo=timezone.utc).timestamp()
        result = asyncio.run(onboarding.generate_posts(tid, call=fake, now=ts))
        self.assertEqual(3, len(result["posts"]))
        self.assertEqual("2026-09-26", result["posts_date"])
        self.assertEqual(2, result["gen_left"])
        self.assertIn("2026-09-26", seen[0][0])
        self.assertIn("星期六", seen[0][0])
        self.assertIn("餐饮", seen[0][0])
        self.assertEqual(tid, seen[0][1])
        state = onboarding.get_state(tid, owner)
        self.assertTrue(state["done"])
        self.assertTrue(state["steps"]["posts"])
        self.assertEqual(result["posts"], state["posts"])
        self.assertEqual(1, state["gen_used"])

    def test_generation_is_free_of_points(self):
        tid = self._ready_tenant()
        before = db.one("SELECT balance FROM tenants WHERE id=?", (tid,))["balance"]

        async def fake(prompt, tenant):
            return {"data": GOOD_POSTS}

        asyncio.run(onboarding.generate_posts(tid, call=fake))
        after = db.one("SELECT balance FROM tenants WHERE id=?", (tid,))["balance"]
        self.assertEqual(before, after)
        self.assertIsNone(db.one("SELECT id FROM billing_log WHERE tenant_id=?", (tid,)))

    def test_failure_refunds_the_attempt(self):
        tid = self._ready_tenant()

        async def broken(prompt, tenant):
            return {"data": {"posts": [{"platform": "朋友圈", "text": "只有一条"}]}}

        async def boom(prompt, tenant):
            raise RuntimeError("供应商超时")

        for call in (broken, boom):
            with self.subTest(call=call.__name__):
                with self.assertRaises(onboarding.OnboardingError) as ctx:
                    asyncio.run(onboarding.generate_posts(tid, call=call))
                self.assertEqual(502, ctx.exception.status)
                self.assertIn("不算次数", str(ctx.exception))
        state = onboarding.get_state(tid, self._owner(tid))
        self.assertEqual(0, state["gen_used"])
        self.assertFalse(state["done"])

    def test_three_free_uses_per_tenant(self):
        tid = self._ready_tenant()
        calls = []

        async def fake(prompt, tenant):
            calls.append(tenant)
            return {"data": GOOD_POSTS}

        for _ in range(3):
            asyncio.run(onboarding.generate_posts(tid, call=fake))
        with self.assertRaises(onboarding.OnboardingError) as ctx:
            asyncio.run(onboarding.generate_posts(tid, call=fake))
        self.assertEqual(429, ctx.exception.status)
        self.assertEqual(3, len(calls))   # 第 4 次根本不调模型
        # 别的企业不受影响
        other = self._ready_tenant()
        asyncio.run(onboarding.generate_posts(other, call=fake))
        self.assertEqual(4, len(calls))

    def test_reserve_is_atomic_under_concurrency(self):
        tid = self._ready_tenant()
        barrier = threading.Barrier(8)

        def attempt(_):
            barrier.wait()
            try:
                onboarding.reserve_generation(tid)
                return True
            except onboarding.OnboardingError:
                return False
            finally:
                db._close_thread_connection()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(onboarding.GEN_LIMIT, sum(results))

    def test_requires_store_info_first(self):
        tid = self._tenant()
        called = []

        async def fake(prompt, tenant):
            called.append(1)
            return {"data": GOOD_POSTS}

        with self.assertRaises(onboarding.OnboardingError) as ctx:
            asyncio.run(onboarding.generate_posts(tid, call=fake))
        self.assertEqual(400, ctx.exception.status)
        self.assertEqual([], called)
        self.assertEqual(0, onboarding.get_state(tid, self._owner(tid))["gen_used"])


class OnboardingCardBrowserCase(unittest.IsolatedAsyncioTestCase):
    """真实浏览器里跑 static/onboarding.js：手机宽度下走完 填店铺 → 生成 → 复制 → 收起 → 关闭。"""

    @classmethod
    def setUpClass(cls):
        from functools import partial
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                return

        root = Path(__file__).resolve().parents[1]
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         partial(Quiet, directory=str(root)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    async def test_owner_walks_through_card_on_phone(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("playwright 不可用")
        from urllib.parse import urlparse

        state = {
            "show": True, "done": False,
            "industry": {"key": "restaurant", "name": "餐饮"},
            "steps": {"industry": True, "store": False, "posts": False},
            "store": {"name": "", "city": "", "product": "", "feature": "", "price": ""},
            "gen_used": 0, "gen_left": 3, "gen_limit": 3,
            "posts": [], "posts_date": "", "password_hint": True,
        }
        calls = []
        payloads = {
            "/api/auth/me": {"id": 7, "username": "13800000000", "role": "owner",
                             "tenant": "王记", "modules": ["content", "avatar", "library"],
                             "all_modules": []},
            "/api/meta": {}, "/api/employees": [],
            "/api/state": {"jobs": [], "inbox": [], "notifications": []},
            "/api/billing": {"balance": 20, "is_platform": False, "recharged": 20,
                             "spent": 0, "txn_n": 0, "log": [], "prices": {},
                             "plans": [], "plan": "", "plan_expires": None},
        }

        async def api_route(route):
            request = route.request
            path = urlparse(request.url).path
            body = request.post_data_json if request.post_data else None
            calls.append((request.method, path, body))
            if path == "/api/onboarding" and request.method == "GET":
                payload = state
            elif path == "/api/onboarding/store":
                state["steps"]["store"] = True
                state["store"] = {**body, "price": 35}
                payload = {"ok": True, "store": state["store"]}
            elif path == "/api/onboarding/posts":
                payload = {"posts": GOOD_POSTS["posts"], "posts_date": "2026-09-26",
                           "gen_used": 1, "gen_left": 2, "done": True}
            elif path == "/api/onboarding/dismiss":
                payload = {"ok": True}
            else:
                payload = payloads.get(path, {})
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        async with async_playwright() as playwright:
            executable = next((p for p in (
                os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "",
                "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
            ) if p and os.path.isfile(p)), "")
            options = {"headless": True}
            if executable:
                options["executable_path"] = executable
            browser = await playwright.chromium.launch(**options)
            page = await browser.new_page(viewport={"width": 375, "height": 800})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.add_init_script(
                "window.EventSource=class{constructor(){} close(){}};")
            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/billing")
            await page.wait_for_function(
                "document.querySelector('#main')?.textContent.includes('套餐与点数')")
            self.assertTrue(await page.evaluate("typeof window.PH_ONBOARDING.card==='function'"))
            # 第一次没有缓存时 card() 返回 Promise:数据到了再整体插入,首页不会晚一拍跳动
            await page.evaluate(
                "Promise.resolve(PH_ONBOARDING.card()).then(html => document.querySelector('#main').insertAdjacentHTML('afterbegin', html))")
            await page.wait_for_selector("#ph-s-name")
            text = await page.locator("#ph-onb").inner_text()
            self.assertIn("您的行业:餐饮", text.replace(" ", ""))
            self.assertIn("建议把密码改成您好记的", text)
            self.assertTrue(await page.locator(".ph-go").is_disabled())

            await page.fill("#ph-s-name", "王记牛肉面")
            await page.fill("#ph-s-city", "成都 春熙路")
            await page.fill("#ph-s-product", "现熬牛骨汤面")
            await page.fill("#ph-s-price", "35")
            await page.click("text=保存,下一步")
            await page.wait_for_selector(".ph-go:not([disabled])")
            put = [c for c in calls if c[1] == "/api/onboarding/store"]
            self.assertEqual("PUT", put[0][0])
            self.assertEqual({"name": "王记牛肉面", "city": "成都 春熙路",
                              "product": "现熬牛骨汤面", "feature": "", "price": "35"}, put[0][2])

            await page.click(".ph-go")
            await page.wait_for_function("document.querySelectorAll('#ph-onb .ph-post').length===3")
            posts_text = await page.locator("#ph-onb").inner_text()
            for platform in ("朋友圈", "小红书", "大众点评"):
                self.assertIn(platform, posts_text)
            self.assertIn("还能用 2 次", posts_text)
            await page.locator("#ph-onb .ph-post button").first.click()
            await page.wait_for_function(
                "document.querySelector('#toast-stack')?.textContent.includes('已复制')")

            # 手机宽度下卡片不撑出横向滚动
            overflow = await page.evaluate(
                "document.querySelector('#ph-onb .ph-onb').getBoundingClientRect().right - innerWidth")
            self.assertLessEqual(overflow, 1)

            await page.click("button[aria-label='收起']")
            await page.wait_for_selector(".ph-onb-done")
            done_text = await page.locator("#ph-onb").inner_text()
            self.assertIn("接下来 3 件事", done_text)
            for item in ("建门店", "给店员开账号", "绑定微信"):
                self.assertIn(item, done_text)
            self.assertEqual(
                ["#/store", "#/team", "#/channels"],
                await page.evaluate(
                    "[...document.querySelectorAll('#ph-onb .ph-next-i')].map(a => a.getAttribute('href'))"),
            )
            await page.click("button[aria-label='关闭上手提示']")
            await page.wait_for_function("document.querySelector('#ph-onb').innerHTML.trim()===''")
            self.assertIn(("POST", "/api/onboarding/dismiss", {}), calls)
            self.assertEqual([], errors)
            await browser.close()

    async def test_store_info_offers_to_create_the_first_store(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("playwright 不可用")
        from urllib.parse import urlparse

        state = {
            "show": True, "done": False,
            "industry": {"key": "restaurant", "name": "餐饮"},
            "steps": {"industry": True, "store": False, "posts": False},
            "store": {"name": "", "city": "", "product": "", "feature": "", "price": ""},
            "gen_used": 0, "gen_left": 3, "gen_limit": 3,
            "posts": [], "posts_date": "", "password_hint": False,
        }
        calls = []
        branches = []

        async def api_route(route):
            request = route.request
            path = urlparse(request.url).path
            body = request.post_data_json if request.post_data else None
            calls.append((request.method, path, body))
            if path == "/api/onboarding" and request.method == "GET":
                payload = state
            elif path == "/api/onboarding/store":
                state["steps"]["store"] = True
                state["store"] = {**body}
                payload = {"ok": True, "store": state["store"]}
            elif path == "/api/onboarding/posts":
                state["done"] = True
                state["steps"]["posts"] = True
                payload = {"posts": GOOD_POSTS["posts"], "posts_date": "2026-09-26",
                           "gen_used": 1, "gen_left": 2, "done": True}
            elif path == "/api/inspections/branches" and request.method == "POST":
                branches.append(body)
                payload = {"id": 5, "name": body["name"], "region": body["region"], "active": True}
            elif path == "/api/inspections":
                payload = {"items": [], "summary": {"total_branches": len(branches)}}
            elif path == "/api/onboarding/dismiss":
                payload = {"ok": True}
            elif path == "/api/auth/me":
                payload = {"id": 7, "username": "13800000000", "role": "owner", "tenant": "王记",
                           "modules": ["content", "avatar", "library"], "all_modules": []}
            elif path == "/api/state":
                payload = {"jobs": [], "inbox": [], "notifications": [], "balance": 20,
                           "setup": {"store": bool(branches)}}
            else:
                payload = {}
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        async with async_playwright() as playwright:
            executable = next((p for p in (
                os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "",
                "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
            ) if p and os.path.isfile(p)), "")
            options = {"headless": True}
            if executable:
                options["executable_path"] = executable
            browser = await playwright.chromium.launch(**options)
            page = await browser.new_page(viewport={"width": 375, "height": 800})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.add_init_script(
                "window.EventSource=class{constructor(){} close(){}};")
            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/")
            await page.wait_for_selector("#ph-s-name")

            await page.fill("#ph-s-name", "王记牛肉面")
            await page.fill("#ph-s-city", "成都 春熙路")
            await page.fill("#ph-s-product", "现熬牛骨汤面")
            await page.click("text=保存,下一步")
            # 还没有门店:问一句「用这个名字建第一家门店？」
            await page.wait_for_selector("#ui-dialog")
            dialog = await page.locator("#ui-dialog").inner_text()
            self.assertIn("用这个名字建第一家门店？", dialog)
            # O6-2:第一家店叫「企业名·商圈店」,不和企业重名
            self.assertIn("王记牛肉面·春熙路店 · 成都 春熙路", dialog)
            await page.locator("#ui-dialog [data-dialog-ok]").click()
            await page.wait_for_function(
                "document.querySelector('#toast-stack')?.textContent.includes('第一家门店')")
            self.assertEqual([{"name": "王记牛肉面·春熙路店", "region": "成都 春熙路"}], branches)
            # 今天页的门店卡跟着刷新,不再是「还没录入门店」
            await page.wait_for_function(
                "document.querySelector('#main .today-store:not(.today-store-empty)')")

            # N-05:有门店以后上手卡收成一行,开账号/派活/写文案三个去处
            await page.wait_for_selector("#ph-onb .ph-onb-line")
            line = await page.locator("#ph-onb").inner_text()
            self.assertIn("基本信息好了", line)
            self.assertEqual("#/team", await page.locator(
                "#ph-onb .ph-onb-line a", has_text="开店员账号").get_attribute("href"))
            self.assertEqual(0, await page.locator("#ph-s-name").count())
            card_h = await page.evaluate(
                "document.querySelector('#ph-onb .ph-onb').getBoundingClientRect().height")
            self.assertLess(card_h, 140)
            # 点「写今天的文案」再展开 3 步
            await page.click("#ph-onb .ph-onb-line >> text=写今天的文案")

            # 再改一次店铺信息:已经有门店了,不再问
            await page.click("text=改一下")
            await page.click("text=保存,下一步")
            await page.wait_for_selector(".ph-go:not([disabled])")
            await page.wait_for_timeout(300)
            self.assertEqual(0, await page.locator("#ui-dialog").count())
            self.assertEqual(1, len(branches))

            # 3 步都做完:展开的卡片里也有「接下来 3 件事」,建门店已完成
            await page.click(".ph-go")
            await page.wait_for_function("document.querySelectorAll('#ph-onb .ph-post').length===3")
            text = await page.locator("#ph-onb").inner_text()
            self.assertIn("接下来 3 件事", text)
            first = page.locator("#ph-onb .ph-next-i").first
            self.assertIn("已完成", await first.inner_text())
            self.assertIn("ph-ok", await first.get_attribute("class"))

            # 关掉时告诉老板去哪找回来
            await page.click("button[aria-label='收起']")
            await page.click("button[aria-label='关闭上手提示']")
            await page.wait_for_function(
                "document.querySelector('#toast-stack')?.textContent.includes('新手引导重看')")
            self.assertEqual([], errors)
            await browser.close()

    async def test_declining_first_store_creates_nothing(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("playwright 不可用")
        from urllib.parse import urlparse

        state = {
            "show": True, "done": False,
            "industry": {"key": "restaurant", "name": "餐饮"},
            "steps": {"industry": True, "store": False, "posts": False},
            "store": {"name": "", "city": "", "product": "", "feature": "", "price": ""},
            "gen_used": 0, "gen_left": 3, "gen_limit": 3,
            "posts": [], "posts_date": "", "password_hint": False,
        }
        posts = []

        async def api_route(route):
            request = route.request
            path = urlparse(request.url).path
            body = request.post_data_json if request.post_data else None
            if request.method == "POST" and path == "/api/inspections/branches":
                posts.append(body)
            if path == "/api/onboarding" and request.method == "GET":
                payload = state
            elif path == "/api/onboarding/store":
                state["steps"]["store"] = True
                state["store"] = {**body}
                payload = {"ok": True}
            elif path == "/api/inspections":
                payload = {"items": [], "summary": {"total_branches": 0}}
            elif path == "/api/auth/me":
                payload = {"id": 7, "username": "13800000000", "role": "owner", "tenant": "王记",
                           "modules": ["content"], "all_modules": []}
            elif path == "/api/state":
                payload = {"jobs": [], "inbox": [], "notifications": []}
            else:
                payload = {}
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        async with async_playwright() as playwright:
            executable = next((p for p in (
                os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "",
                "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
            ) if p and os.path.isfile(p)), "")
            options = {"headless": True}
            if executable:
                options["executable_path"] = executable
            browser = await playwright.chromium.launch(**options)
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.add_init_script(
                "window.EventSource=class{constructor(){} close(){}};")
            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/")
            await page.wait_for_selector("#ph-s-name")
            await page.fill("#ph-s-name", "王记牛肉面")
            await page.fill("#ph-s-product", "现熬牛骨汤面")
            await page.click("text=保存,下一步")
            await page.wait_for_selector("#ui-dialog")
            await page.locator("#ui-dialog [data-dialog-cancel]").click()
            await page.wait_for_function("!document.querySelector('#ui-dialog')")
            await page.wait_for_timeout(200)
            self.assertEqual([], posts)
            # O-2:「先不建」后卡片上留一行入口，随时能建
            self.assertIn("还没建门店", await page.locator("#ph-onb .ph-nostore").inner_text())
            self.assertEqual([], errors)
            await browser.close()

    async def test_store_form_collapses_card_without_reload(self):
        """O-2/O-3/O-4/R-4:误点弹窗外面后留入口;表单预填店名城市;建好门店不刷新就收成一行;
        「接下来」随进度打勾,全做完只剩「上手完成」。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("playwright 不可用")
        from urllib.parse import urlparse

        state = {
            "show": True, "done": False,
            "industry": {"key": "restaurant", "name": "餐饮"},
            "steps": {"industry": True, "store": False, "posts": False},
            "store": {"name": "", "city": "", "product": "", "feature": "", "price": ""},
            "gen_used": 0, "gen_left": 3, "gen_limit": 3,
            "posts": [], "posts_date": "", "password_hint": False,
            "progress": {"store": False, "staff": False, "staff_task": False},
        }
        branches = []

        async def api_route(route):
            request = route.request
            path = urlparse(request.url).path
            body = request.post_data_json if request.post_data else None
            if path == "/api/onboarding" and request.method == "GET":
                # 故意不跟着门店变:验证建店后 app.js 自己刷新 STATE.setup,不靠这份兜底
                payload = state
            elif path == "/api/onboarding/store":
                state["steps"]["store"] = True
                state["store"] = {**body}
                payload = {"ok": True}
            elif path == "/api/inspections/branches" and request.method == "POST":
                branches.append(body)
                payload = {"id": 5, "name": body["name"], "region": body["region"], "active": True}
            elif path == "/api/inspections":
                payload = {"items": [], "summary": {"total_branches": len(branches)}}
            elif path == "/api/auth/me":
                payload = {"id": 7, "username": "13800000000", "role": "owner", "tenant": "王记",
                           "modules": ["content"], "all_modules": []}
            elif path == "/api/state":
                payload = {"jobs": [], "inbox": [], "notifications": [], "balance": 20,
                           "setup": {"store": bool(branches)}}
            else:
                payload = {}
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        async with async_playwright() as playwright:
            executable = next((p for p in (
                os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "",
                "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
            ) if p and os.path.isfile(p)), "")
            options = {"headless": True}
            if executable:
                options["executable_path"] = executable
            browser = await playwright.chromium.launch(**options)
            page = await browser.new_page(viewport={"width": 375, "height": 800})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.add_init_script(
                "window.EventSource=class{constructor(){} close(){}};")
            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/")
            await page.wait_for_selector("#ph-s-name")
            await page.evaluate("window.__noReload = 1")
            await page.fill("#ph-s-name", "王记火锅")
            await page.fill("#ph-s-city", "成都 春熙路")
            await page.fill("#ph-s-product", "牛油火锅")
            await page.click("text=保存,下一步")
            # O-2:误点到弹窗外面 = 先不建,但卡片上留一行入口
            await page.wait_for_selector("#ui-dialog")
            await page.mouse.click(5, 5)
            await page.wait_for_function("!document.querySelector('#ui-dialog')")
            await page.wait_for_selector("#ph-onb .ph-nostore")
            self.assertIn("还没建门店", await page.locator("#ph-onb .ph-nostore").inner_text())
            self.assertEqual([], branches)
            # O-3 / O6-2:点入口打开建店表单,城市已经填好,店名预填成「企业名·商圈店」(不和企业重名)
            await page.click("#ph-onb .ph-nostore >> text=建第一家店")
            await page.wait_for_function(
                "document.querySelector('#store-form-name')?.value==='王记火锅·春熙路店'")
            self.assertEqual("成都 春熙路", await page.input_value("#store-form-region"))
            await page.click("#store-form [data-store-form-cancel]")
            await page.wait_for_function("!document.querySelector('#store-form')")
            # 从今天页门店空状态的「新建门店」进来也一样预填
            await page.click("#main .today-store-empty [data-empty-action]")
            await page.wait_for_function(
                "document.querySelector('#store-form-name')?.value==='王记火锅·春熙路店'")
            self.assertEqual(3, await page.locator("#ph-onb .ph-step").count())
            await page.click("#store-form [data-store-form-ok]")
            await page.wait_for_function("!document.querySelector('#store-form')")
            self.assertEqual([{"name": "王记火锅·春熙路店", "region": "成都 春熙路", "address": ""}], branches)
            # O-4:不刷新页面,上手卡收成一行,门店卡也换成有门店的样子
            await page.wait_for_selector("#ph-onb .ph-onb-line")
            await page.wait_for_function(
                "document.querySelector('#main .today-store:not(.today-store-empty)')")
            self.assertEqual(1, await page.evaluate("window.__noReload"))
            self.assertEqual(0, await page.locator("#ph-onb .ph-step").count())
            line = await page.locator("#ph-onb").inner_text()
            self.assertIn("开店员账号", line)
            self.assertIn("派第一件活", line)

            # R-4:开了店员账号、派了活之后,这两件打勾,不再当成「接下来」
            state["progress"].update({"staff": True, "staff_task": True})
            await page.evaluate("PH_ONBOARDING.refresh()")
            await page.wait_for_function(
                "!document.querySelector('#ph-onb .ph-onb-line a[href=\"#/team\"]')")
            line = await page.locator("#ph-onb").inner_text()
            self.assertIn("✓ 开店员账号", line)
            self.assertIn("✓ 派第一件活", line)
            self.assertEqual(0, await page.locator("#ph-onb .ph-onb-line a[href='#/staff-tasks']").count())
            self.assertIn("写今天的文案", line)
            # 文案也写过了:只剩一句「上手完成」和关闭按钮
            state["done"] = True
            state["steps"]["posts"] = True
            await page.evaluate("PH_ONBOARDING.refresh()")
            await page.wait_for_selector("#ph-onb .ph-onb-all")
            line = await page.locator("#ph-onb").inner_text()
            self.assertIn("上手完成", line)
            self.assertNotIn("接下来", line)
            self.assertEqual(1, await page.locator("#ph-onb button[aria-label='关闭上手提示']").count())
            self.assertEqual(1, await page.evaluate("window.__noReload"))
            self.assertEqual([], errors)
            await browser.close()

    async def test_first_store_name_never_copies_company_name(self):
        """O6-2:城市里没有商圈时店名留空、企业名只放进占位提示;老板已经动手输的不覆盖。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("playwright 不可用")
        from urllib.parse import urlparse

        async def api_route(route):
            path = urlparse(route.request.url).path
            payload = {}
            if path == "/api/auth/me":
                payload = {"id": 7, "username": "13800000000", "role": "owner", "tenant": "王记",
                           "modules": ["content"], "all_modules": []}
            elif path == "/api/state":
                payload = {"jobs": [], "inbox": [], "notifications": [], "balance": 20,
                           "setup": {"store": False}}
            elif path == "/api/onboarding":
                payload = {"show": False, "done": True}
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(payload, ensure_ascii=False))

        async with async_playwright() as playwright:
            executable = next((p for p in (
                os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "",
                "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
            ) if p and os.path.isfile(p)), "")
            options = {"headless": True}
            if executable:
                options["executable_path"] = executable
            browser = await playwright.chromium.launch(**options)
            page = await browser.new_page(viewport={"width": 375, "height": 800})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.add_init_script("window.EventSource=class{constructor(){} close(){}};")
            await page.route("**/api/**", api_route)
            await page.goto(f"{self.base}/static/index.html#/")
            await page.wait_for_function("typeof firstStoreNameSuggestion==='function' && !!ME")
            self.assertEqual(
                ["王记火锅·春熙路店", "王记火锅·静安店", "", "", "王记火锅·万达店"],
                await page.evaluate("""[
                    firstStoreNameSuggestion('王记火锅','成都 春熙路'),
                    firstStoreNameSuggestion('王记火锅','上海·静安'),
                    firstStoreNameSuggestion('王记火锅','成都'),
                    firstStoreNameSuggestion('','成都 春熙路'),
                    firstStoreNameSuggestion('王记火锅','成都 万达店')]"""))
            # 只写了城市:店名留空,占位提示带企业名
            await page.evaluate("""window.PH_ONBOARDING.storeBasics=async()=>({name:'王记火锅',city:'成都'});
                openStoreForm()""")
            await page.wait_for_function(
                "document.querySelector('#store-form-region')?.value==='成都'")
            self.assertEqual("", await page.input_value("#store-form-name"))
            self.assertIn("王记火锅", await page.get_attribute("#store-form-name", "placeholder"))
            await page.click("#store-form [data-store-form-cancel]")
            # 预填数据回来之前老板已经输了店名:不覆盖
            await page.evaluate("""window.PH_ONBOARDING.storeBasics=()=>new Promise(r=>setTimeout(
                ()=>r({name:'王记火锅',city:'成都 春熙路'}),300)); openStoreForm()""")
            await page.wait_for_selector("#store-form-name")
            await page.fill("#store-form-name", "老店")
            await page.wait_for_timeout(500)
            self.assertEqual("老店", await page.input_value("#store-form-name"))
            self.assertEqual([], errors)
            await browser.close()

    async def test_card_is_empty_for_members(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("playwright 不可用")
        async with async_playwright() as playwright:
            executable = next((p for p in (
                os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "",
                "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
            ) if p and os.path.isfile(p)), "")
            options = {"headless": True}
            if executable:
                options["executable_path"] = executable
            browser = await playwright.chromium.launch(**options)
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                await page.route("**/api/**", lambda route: route.fulfill(
                    status=200, content_type="application/json", body="{}"))
                # Navigating to .js creates a JavaScript text document;
                # set_content() does not change its MIME type into HTML.
                # Use a same-origin HTML fixture without app.js or its ME state.
                fixture_url = f"{self.base}/__test_fixtures__/onboarding-member.html"
                await page.route(fixture_url, lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8",
                    body="<!doctype html><meta charset='utf-8'><div id='main'></div>"))
                await page.goto(fixture_url)
                self.assertEqual("text/html", await page.evaluate("document.contentType"))
                await page.add_script_tag(url=f"{self.base}/static/onboarding.js")
                self.assertTrue(await page.evaluate(
                    "typeof window.PH_ONBOARDING?.card==='function'"))
                html = await page.evaluate(
                    "(()=>{window.ME={role:'member'};return PH_ONBOARDING.card();})()")
                self.assertEqual("", html)
                self.assertEqual([], errors)
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
