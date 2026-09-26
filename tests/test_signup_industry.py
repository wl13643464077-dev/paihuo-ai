"""自助开户行业绑定、访客名额与充值校验的行为测试（不依赖 fastapi）。"""
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app import auth, db, departments, signup


def _valid_keys():
    return [d["key"] for d in departments.list_depts()]


class IndustryMatchCase(unittest.TestCase):
    def test_free_text_maps_to_expected_industry(self):
        keys = _valid_keys()
        cases = {
            "奶茶店": "tea_coffee",
            "咖啡馆": "tea_coffee",
            "火锅": "restaurant",
            "张记火锅店": "restaurant",
            "美甲": "beauty",
            "美容院": "beauty",
            "美甲/美容院": "beauty",
            "健身房": "fitness",
            "瑜伽馆": "fitness",
            "汽车美容": "auto",
            "4S店": "auto",
            "宠物医院": "pet",
            "社区超市": "grocery",
            "药店": "pharmacy",
            "零食量贩": "snack",
            "便利店": "convenience",
            "民宿": "hotel",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, signup.resolve_industry(text, keys))

    def test_exact_key_label_and_department_name_match(self):
        keys = _valid_keys()
        names = {d["key"]: d["name"] for d in departments.list_depts()}
        self.assertEqual("tea_coffee", signup.resolve_industry("tea_coffee", keys))
        self.assertEqual("tea_coffee", signup.resolve_industry("茶饮咖啡", keys))
        self.assertEqual(
            "convenience",
            signup.resolve_industry(names["convenience"], keys, names),
        )

    def test_unknown_ambiguous_and_other_return_none(self):
        keys = _valid_keys()
        for text in ("", "其他", "other", "企业服务", "软件外包", "奶茶火锅店"):
            with self.subTest(text=text):
                self.assertIsNone(signup.resolve_industry(text, keys))

    def test_match_is_restricted_to_existing_departments(self):
        self.assertIsNone(signup.resolve_industry("奶茶店", ["restaurant"]))
        self.assertEqual("restaurant", signup.resolve_industry("火锅", ["restaurant"]))

    def test_public_options_only_expose_key_and_name_of_real_departments(self):
        options = signup.industry_options([
            {"key": "restaurant", "name": "餐饮产业部", "employees": [{"idx": 1}]},
            {"key": "newbiz", "name": "新行业部", "employees": []},
        ])
        self.assertEqual(
            [{"key": "restaurant", "name": "餐饮"}, {"key": "newbiz", "name": "新行业部"}],
            options,
        )
        real = signup.industry_options(departments.list_depts())
        self.assertEqual(set(_valid_keys()), {x["key"] for x in real})
        for item in real:
            self.assertEqual({"key", "name"}, set(item))

    def test_apply_note_round_trip_resolves_industry(self):
        keys = _valid_keys()
        line = signup.normalize_apply_industry("tea_coffee", "", keys)
        note = signup.compose_apply_note(line, "想做新品推广")
        self.assertEqual("行业：茶饮咖啡\n想做新品推广", note)
        self.assertEqual(
            "tea_coffee",
            signup.industry_key_for_apply({"note": note, "company": "王记"}, keys),
        )
        # 选「其他」写自由文本 → 开户时模糊匹配
        other = signup.compose_apply_note(
            signup.normalize_apply_industry("other", "健身房", keys), "")
        self.assertEqual(
            "fitness", signup.industry_key_for_apply({"note": other}, keys))
        # 伪造的行业 key 不会被当成真实行业写入
        self.assertEqual("", signup.normalize_apply_industry("root_all", "", keys))
        # 老申请单没有行业行 → 用企业名兜底
        self.assertEqual(
            "restaurant",
            signup.industry_key_for_apply({"note": "", "company": "小龙坎火锅"}, keys),
        )
        self.assertIsNone(
            signup.industry_key_for_apply({"note": "随便看看", "company": "某某科技"}, keys))

    def test_clip_strips_control_chars_and_truncates(self):
        self.assertEqual("ab c", signup.clip("  ab\nc  ", 10))
        self.assertEqual(30, len(signup.clip("王" * 500, 30)))
        self.assertEqual("", signup.clip(None, 30))


class GrantPointsCase(unittest.TestCase):
    def test_rejects_non_positive_non_numeric_and_huge(self):
        for bad in (None, "", "abc", 0, -5, "-1", float("nan"), float("inf"), True,
                    signup.GRANT_POINTS_MAX + 1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    signup.parse_grant_points(bad)

    def test_accepts_positive_numbers(self):
        self.assertEqual(100.0, signup.parse_grant_points("100"))
        self.assertEqual(0.5, signup.parse_grant_points(0.5))


class QuotaCase(unittest.TestCase):
    def test_daily_quota_reserve_is_atomic_under_concurrency(self):
        quota = signup.DailyQuota()
        barrier = threading.Barrier(16)

        def attempt(_):
            barrier.wait()
            return quota.try_reserve(5)

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(attempt, range(16)))
        self.assertEqual(5, sum(results))
        self.assertEqual(5, quota.used())

    def test_daily_quota_release_and_day_rollover(self):
        quota = signup.DailyQuota()
        day1 = 100 * 86400 + 10
        self.assertTrue(quota.try_reserve(1, now=day1))
        self.assertFalse(quota.try_reserve(1, now=day1))
        quota.release(now=day1)
        self.assertTrue(quota.try_reserve(1, now=day1))
        # 跨天名额清零;昨天的回滚不会把今天的计数减成负数
        self.assertTrue(quota.try_reserve(1, now=day1 + 86400))
        quota.release(now=day1 + 2 * 86400)
        self.assertEqual(1, quota.used(now=day1 + 86400))

    def test_daily_counter_limits_per_key_and_stays_bounded(self):
        counter = signup.DailyCounter(limit=2, max_keys=3)
        now = 200 * 86400
        self.assertFalse(counter.hit("a", now))
        self.assertFalse(counter.hit("a", now))
        self.assertTrue(counter.hit("a", now))
        self.assertFalse(counter.hit("a", now + 86400))   # 第二天重新计
        for i in range(10):
            counter.hit(f"noise-{i}", now + 86400)
        self.assertLessEqual(len(counter), 3)


class TenantIndustryDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "signup.db")
        db.conn()
        if not db.one("SELECT id FROM tenants WHERE id=1"):
            db.insert("tenants", {"name": "平台总部"})   # id=1 是平台方

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _tenant(self, name="新客"):
        return db.insert("tenants", {"name": name, "industries_json": "[]"})

    def _as_owner(self, tid):
        auth.set_current({"id": 99, "tenant_id": tid, "username": "o",
                          "role": "owner", "modules": []})

    def test_empty_tenant_sees_no_expert_until_owner_picks_one(self):
        tid = self._tenant()
        self._as_owner(tid)
        self.assertTrue(signup.tenant_needs_industry(tid))
        self.assertFalse(auth.dept_visible("tea_coffee"))

        signup.claim_first_industry(tid, "tea_coffee", _valid_keys())

        self.assertFalse(signup.tenant_needs_industry(tid))
        self.assertTrue(auth.dept_visible("tea_coffee"))
        self.assertTrue(auth.allowed("tea_coffee"))
        self.assertFalse(auth.dept_visible("restaurant"))
        row = db.one("SELECT industries_json FROM tenants WHERE id=?", (tid,))
        self.assertEqual(["tea_coffee"], db.jloads(row["industries_json"], []))
        self.assertIn("tea_coffee", [m["key"] for m in auth.all_modules()])

    def test_self_claim_is_one_time_even_after_platform_clears_industries(self):
        tid = self._tenant()
        signup.claim_first_industry(tid, "fitness", _valid_keys())
        # 平台后来把行业清空(例如停权)：老板不能再自己免费选回来
        db.execute("DELETE FROM tenant_industry WHERE tenant_id=?", (tid,))
        db.execute("UPDATE tenants SET industries_json='[]' WHERE id=?", (tid,))
        self.assertFalse(signup.tenant_needs_industry(tid))
        with self.assertRaises(signup.IndustryChoiceError) as caught:
            signup.claim_first_industry(tid, "restaurant", _valid_keys())
        self.assertEqual(409, caught.exception.status)

    def test_owner_cannot_add_second_industry_or_invalid_key(self):
        tid = self._tenant()
        with self.assertRaises(signup.IndustryChoiceError) as caught:
            signup.claim_first_industry(tid, "not-a-dept", _valid_keys())
        self.assertEqual(400, caught.exception.status)
        signup.claim_first_industry(tid, "fitness", _valid_keys())
        with self.assertRaises(signup.IndustryChoiceError) as caught:
            signup.claim_first_industry(tid, "restaurant", _valid_keys())
        self.assertEqual(409, caught.exception.status)
        keys = [r["industry_key"] for r in db.q(
            "SELECT industry_key FROM tenant_industry WHERE tenant_id=?", (tid,))]
        self.assertEqual(["fitness"], keys)

    def test_concurrent_first_pick_binds_exactly_one_industry(self):
        tid = self._tenant()
        keys = _valid_keys()
        barrier = threading.Barrier(2)

        def pick(key):
            # 每个线程用自己的 SQLite 连接，模拟两个请求同时点
            barrier.wait()
            try:
                signup.claim_first_industry(tid, key, keys)
                return True
            except signup.IndustryChoiceError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(pick, ["restaurant", "beauty"]))
        self.assertEqual(1, sum(results))
        rows = db.q("SELECT industry_key FROM tenant_industry WHERE tenant_id=?", (tid,))
        self.assertEqual(1, len(rows))

    def test_platform_and_missing_tenant_are_rejected(self):
        with self.assertRaises(signup.IndustryChoiceError):
            signup.claim_first_industry(1, "restaurant", _valid_keys())
        with self.assertRaises(signup.IndustryChoiceError) as caught:
            signup.claim_first_industry(987654, "restaurant", _valid_keys())
        self.assertEqual(404, caught.exception.status)
        self.assertFalse(signup.tenant_needs_industry(1))

    def test_write_tenant_industries_replaces_both_stores(self):
        tid = self._tenant()
        with db.atomic() as connection:
            signup.write_tenant_industries(connection, tid, ["pet", "pet", "hotel"])
        rows = db.q("SELECT industry_key,is_primary FROM tenant_industry "
                    "WHERE tenant_id=? ORDER BY is_primary DESC", (tid,))
        self.assertEqual([("pet", 1), ("hotel", 0)],
                         [(r["industry_key"], r["is_primary"]) for r in rows])


if __name__ == "__main__":
    unittest.main()
