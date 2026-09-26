"""第 2 期开闭店清单：默认模板、首次复制、每日生成幂等/跨天、指派、照片必填、完成/错过。"""
from __future__ import annotations

import io
import os
import re
import tempfile
import unittest

from PIL import Image

from app import checklist, db, signup

DAY = "2026-09-21"
D0 = checklist.cn_date_start_ts(DAY)          # 2026-09-21 00:00 北京时间
H = 3600


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (30, 160, 90)).save(buf, "JPEG")
    return buf.getvalue()


class Phase2Base(unittest.TestCase):
    """两家茶饮店：A 店有店长 22、店员 23；B 店只有店员 24。老板 20，别家老板 30。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "checklist.db")
        db.conn()
        self.assets = os.path.join(self.tmp.name, "assets")
        for tid in (2, 3):
            db.insert("tenants", {"id": tid, "name": f"企业{tid}", "industries_json": "[]"})
            db.execute("INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                       "VALUES(?,'tea_coffee',1,0)", (tid,))
        for uid, tid, role, title, phone in (
                (20, 2, "owner", "staff", "13800000020"),
                (21, 2, "member", "director", None),
                (22, 2, "member", "manager", "13800000022"),
                (23, 2, "member", "staff", "13800000023"),
                (24, 2, "member", "staff", ""),
                (30, 3, "owner", "staff", None)):
            db.insert("users", {"id": uid, "tenant_id": tid, "username": f"u{uid}",
                                "password_hash": "x", "role": role, "job_title": title,
                                "modules_json": '["tea_coffee"]', "enabled": 1,
                                "phone": phone})
        self.a = db.insert("store_branch", {"tenant_id": 2, "industry_key": "tea_coffee",
                                            "name": "朝阳店", "active": 1, "created_at": 0,
                                            "area_sqm": 200})
        self.b = db.insert("store_branch", {"tenant_id": 2, "industry_key": "tea_coffee",
                                            "name": "静安店", "active": 1, "created_at": 0})
        for uid, bid in ((22, self.a), (23, self.a), (24, self.b)):
            db.execute("INSERT INTO user_branch(tenant_id,user_id,branch_id,created_at) "
                       "VALUES(2,?,?,0)", (uid, bid))

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    # 小工具
    def seed_templates(self, ts=D0 - 86400):
        return checklist.ensure_templates(2, now=ts)

    def runs(self, branch_id=None, kind=None, day=DAY):
        sql = "SELECT * FROM checklist_run WHERE tenant_id=2 AND run_date=?"
        args = [day]
        if branch_id:
            sql += " AND branch_id=?"
            args.append(branch_id)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return db.q(sql + " ORDER BY id", tuple(args))


class DefaultTemplateTests(unittest.TestCase):
    def test_every_catalog_industry_has_three_realistic_checklists(self):
        catalog = set(signup.INDUSTRY_CATALOG)
        self.assertEqual(11, len(catalog))
        self.assertEqual(catalog, set(checklist.default_industries()))
        for industry in sorted(catalog) + [checklist.GENERIC_INDUSTRY]:
            templates = checklist.default_templates(industry)
            self.assertEqual(set(checklist.DEFAULT_KINDS), set(templates), industry)
            for kind, tpl in templates.items():
                where = f"{industry}/{kind}"
                self.assertTrue(6 <= len(tpl["items"]) <= 10, where)
                self.assertRegex(tpl["due_time"], r"^([01]\d|2[0-3]):[0-5]\d$", where)
                self.assertTrue(tpl["name"], where)
                keys = [item["key"] for item in tpl["items"]]
                self.assertEqual(len(keys), len(set(keys)), where)
                for item in tpl["items"]:
                    self.assertRegex(item["key"], r"^[a-z0-9_]{1,32}$", where)
                    self.assertTrue(4 <= len(item["text"]) <= checklist.ITEM_TEXT_MAX, where)
                    # 需要拍照的项文案里要让店员知道拍什么
                    if item["require_photo"]:
                        self.assertIn("拍", item["text"], where)
                self.assertTrue(any(i["require_photo"] for i in tpl["items"]), where)

    def test_tea_coffee_open_checklist_has_concrete_photo_items(self):
        items = checklist.default_templates("tea_coffee")["open"]["items"]
        texts = " ".join(i["text"] for i in items)
        self.assertIn("冰桶", texts)
        self.assertIn("效期标签", texts)
        self.assertIn("零钱", texts)

    def test_unknown_industry_falls_back_to_generic(self):
        self.assertEqual(checklist.default_templates(checklist.GENERIC_INDUSTRY),
                         checklist.default_templates("other"))

    def test_normalize_items_keeps_keys_and_generates_new_ones(self):
        items = checklist.normalize_items([
            {"key": "o01", "text": " 制冰机  正常 ", "require_photo": True},
            {"text": "新加的一项"},
            "字符串也行",
            {"key": "o01", "text": "重复 key 会换新 key"},
        ])
        self.assertEqual("o01", items[0]["key"])
        self.assertEqual("制冰机 正常", items[0]["text"])
        keys = [i["key"] for i in items]
        self.assertEqual(len(keys), len(set(keys)))
        for bad in ([], "x", [{"text": ""}], [{"text": "x" * 81}],
                    [{"text": "a"}] * (checklist.MAX_ITEMS + 1)):
            with self.assertRaises(checklist.ChecklistError):
                checklist.normalize_items(bad)


class TemplateTests(Phase2Base):
    def test_first_use_copies_defaults_once_per_industry(self):
        self.assertEqual(3, self.seed_templates())
        self.assertEqual(0, self.seed_templates())
        items = checklist.list_templates(2, 20)
        self.assertEqual({"open", "close", "handover"}, {t["kind"] for t in items})
        self.assertTrue(all(t["industry_key"] == "tea_coffee" for t in items))
        # 别家企业互不影响，第一次用时才复制
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM checklist_template "
                                   "WHERE tenant_id=3")["n"])
        # 总监可以管理，店长/店员不行
        self.assertEqual(3, len(checklist.list_templates(2, 21)))
        with self.assertRaises(checklist.ChecklistForbidden):
            checklist.list_templates(2, 22)
        with self.assertRaises(checklist.ChecklistForbidden):
            checklist.list_templates(2, 30)

    def test_owner_edits_items_due_time_and_can_deactivate(self):
        self.seed_templates()
        tpl = next(t for t in checklist.list_templates(2, 20) if t["kind"] == "open")
        kept = tpl["items"][0]
        updated = checklist.update_template(2, 20, tpl["id"], {
            "items": [kept, {"text": "门口地垫摆正", "require_photo": False}],
            "due_time": "09:15",
            "name": "早班开店",
        })
        self.assertEqual(kept["key"], updated["items"][0]["key"])
        self.assertEqual(2, len(updated["items"]))
        self.assertEqual("09:15", updated["due_time"])
        self.assertEqual("早班开店", updated["name"])
        for bad in ({"due_time": "25:00"}, {"items": []}, {"active": "no"}):
            with self.assertRaises(checklist.ChecklistError):
                checklist.update_template(2, 20, tpl["id"], bad)
        with self.assertRaises(checklist.ChecklistNotFound):
            checklist.update_template(3, 30, tpl["id"], {"due_time": "09:00"})
        checklist.update_template(2, 20, tpl["id"], {"active": False})
        checklist.generate_runs(2, date=DAY, now=D0 + 60)
        self.assertEqual([], self.runs(kind="open"))
        run = self.runs(self.a, "handover")[0]
        self.assertEqual(checklist.due_ts(DAY, "15:00"), run["due_at"])

    def test_create_custom_template_applies_to_all_branches(self):
        self.seed_templates()
        tpl = checklist.create_template(2, 20, {
            "kind": "custom", "name": "周一大扫除", "due_time": "18:00",
            "items": [{"text": "擦玻璃，拍一张", "require_photo": True}]}, now=D0 - 10)
        self.assertEqual("", tpl["industry_key"])
        checklist.generate_runs(2, date=DAY, now=D0 + 60)
        self.assertEqual(2, len(self.runs(kind="custom")))
        with self.assertRaises(checklist.ChecklistError):
            checklist.create_template(2, 20, {"kind": "weird", "items": ["a"]})
        with self.assertRaises(checklist.ChecklistError):
            checklist.create_template(2, 20, {"industry_key": "hotel", "items": ["a"]})


class GenerateTests(Phase2Base):
    def test_generation_is_idempotent_and_assigns_manager_or_blank(self):
        self.seed_templates()
        self.assertEqual(6, checklist.generate_runs(date=DAY, now=D0 + 60))
        self.assertEqual(0, checklist.generate_runs(date=DAY, now=D0 + 120))
        self.assertEqual(0, checklist.generate_runs(2, date=DAY, now=D0 + 180))
        self.assertEqual(6, len(self.runs()))
        for run in self.runs(self.a):
            self.assertEqual(22, run["assignee_user_id"])     # A 店店长
        for run in self.runs(self.b):
            self.assertIsNone(run["assignee_user_id"])        # B 店没店长，留空
        opened = self.runs(self.a, "open")[0]
        self.assertEqual(D0 + 10 * H, opened["due_at"])       # 茶饮开店 10:00
        self.assertEqual("open", opened["status"])
        items = db.jloads(opened["items_json"], [])
        self.assertTrue(items and not any(i["done"] for i in items))

    def test_next_day_generates_fresh_runs(self):
        self.seed_templates()
        checklist.generate_runs(date=DAY, now=D0 + 60)
        tomorrow = "2026-09-22"
        self.assertEqual(6, checklist.generate_runs(now=D0 + 86400 + 60))
        self.assertEqual(6, len(self.runs(day=tomorrow)))
        self.assertEqual(6, len(self.runs(day=DAY)))
        run = self.runs(self.a, "open", day=tomorrow)[0]
        self.assertEqual(D0 + 86400 + 10 * H, run["due_at"])

    def test_duty_person_overrides_manager_and_must_be_bound(self):
        self.seed_templates()
        with self.assertRaises(checklist.ChecklistError):
            checklist.set_duty_user(2, 20, self.a, 24)       # 24 没绑定 A 店
        with self.assertRaises(checklist.ChecklistForbidden):
            checklist.set_duty_user(2, 22, self.a, 23)       # 店长不能指定
        checklist.set_duty_user(2, 20, self.a, 23)
        checklist.set_duty_user(2, 20, self.b, 24)
        checklist.generate_runs(date=DAY, now=D0 + 60)
        self.assertEqual({23}, {r["assignee_user_id"] for r in self.runs(self.a)})
        self.assertEqual({24}, {r["assignee_user_id"] for r in self.runs(self.b)})
        checklist.set_duty_user(2, 20, self.a, None)
        self.assertEqual(22, checklist.default_assignee(2, self.a))

    def test_brand_new_setup_skips_checklists_already_past_due_today(self):
        # 中午 12 点才第一次用：今天 10:00 截止的开店清单不生成，交班/闭店照常
        checklist.generate_runs(date=DAY, now=D0 + 12 * H)
        self.assertEqual({"handover", "close"}, {r["kind"] for r in self.runs()})

    def test_inactive_branch_and_other_industry_are_skipped(self):
        db.insert("store_branch", {"tenant_id": 2, "industry_key": "hotel",
                                   "name": "停用店", "active": 0, "created_at": 0})
        self.seed_templates()
        checklist.generate_runs(date=DAY, now=D0 + 60)
        self.assertEqual({self.a, self.b}, {r["branch_id"] for r in self.runs()})


class CompleteTests(Phase2Base):
    def setUp(self):
        super().setUp()
        self.seed_templates()
        checklist.generate_runs(date=DAY, now=D0 + 60)
        self.run = self.runs(self.a, "open")[0]
        self.items = db.jloads(self.run["items_json"], [])
        self.photo_item = next(i for i in self.items if i["require_photo"])
        self.plain_item = next(i for i in self.items if not i["require_photo"])

    def complete(self, uid, key, *, photo=None, now=D0 + 8 * H, **kw):
        return checklist.complete_item(2, uid, self.run["id"], key, photo=photo,
                                       now=now, asset_root=self.assets, **kw)

    def test_photo_item_requires_photo_and_stores_metadata(self):
        with self.assertRaisesRegex(checklist.ChecklistError, "拍照"):
            self.complete(23, self.photo_item["key"])
        out = self.complete(23, self.photo_item["key"], photo={"data": _jpeg()},
                            note="冰量足")
        item = next(i for i in out["items"] if i["key"] == self.photo_item["key"])
        self.assertTrue(item["done"])
        self.assertEqual(23, item["done_by"])
        self.assertEqual("冰量足", item["note"])
        self.assertTrue(re.match(r"^/files/staff/2/%d/[a-f0-9]{32}\.jpg$" % self.a,
                                 item["photo_url"]))
        stored = next(i for i in db.jloads(db.one(
            "SELECT items_json FROM checklist_run WHERE id=?", (self.run["id"],))["items_json"])
            if i["key"] == self.photo_item["key"])
        self.assertEqual(D0 + 8 * H, stored["photo"]["received_at"])
        self.assertTrue(os.path.isfile(os.path.join(self.assets, stored["photo"]["storage_key"])))
        self.assertIn("朝阳店", stored["photo"]["watermark_text"])
        # 取消再打勾：之前的照片还在，不用重拍
        self.complete(23, self.photo_item["key"], done=False)
        again = self.complete(23, self.photo_item["key"])
        self.assertTrue(next(i for i in again["items"]
                             if i["key"] == self.photo_item["key"])["done"])
        # 不是照片 / 别家门店的照片元数据都不收
        with self.assertRaises(ValueError):
            self.complete(23, self.plain_item["key"], photo={"data": b"not image"})
        with self.assertRaisesRegex(checklist.ChecklistError, "不属于"):
            self.complete(23, self.plain_item["key"], photo={
                "storage_key": f"staff/2/{self.b}/{'a' * 32}.jpg"})

    def test_retake_replaces_old_photo_file(self):
        first = self.complete(23, self.photo_item["key"], photo={"data": _jpeg()})
        old_url = next(i for i in first["items"] if i["key"] == self.photo_item["key"])["photo_url"]
        self.complete(23, self.photo_item["key"], photo={"data": _jpeg()})
        self.assertFalse(os.path.exists(os.path.join(self.assets, old_url[len("/files/"):])))

    def test_all_items_before_due_marks_done_then_locked(self):
        for item in self.items:
            photo = {"data": _jpeg()} if item["require_photo"] else None
            out = self.complete(22 if item["key"] < "o04" else 23, item["key"], photo=photo)
        self.assertEqual("done", out["status"])
        self.assertEqual(out["total"], out["done_count"])
        row = db.one("SELECT * FROM checklist_run WHERE id=?", (self.run["id"],))
        self.assertEqual(D0 + 8 * H, row["completed_at"])
        self.assertEqual(23, row["completed_by"])
        with self.assertRaises(checklist.ChecklistConflict):
            self.complete(23, self.plain_item["key"], done=False)
        # 截止时间一过，已完成的清单不会被标 missed
        self.assertEqual([], [r for r in checklist.mark_missed(2, now=D0 + 11 * H)
                              if r["id"] == self.run["id"]])

    def test_missed_after_due_and_late_completion_stays_missed(self):
        self.complete(23, self.plain_item["key"])
        missed = checklist.mark_missed(now=D0 + 10 * H)
        self.assertIn(self.run["id"], [r["id"] for r in missed])
        self.assertEqual([], checklist.mark_missed(now=D0 + 10 * H + 300))
        for item in self.items:
            photo = {"data": _jpeg()} if item["require_photo"] else None
            out = self.complete(23, item["key"], photo=photo, now=D0 + 11 * H)
        self.assertEqual("missed", out["status"])
        self.assertEqual(D0 + 11 * H, out["completed_at"])

    def test_finishing_after_due_before_loop_marks_counts_as_missed(self):
        for item in self.items:
            photo = {"data": _jpeg()} if item["require_photo"] else None
            out = self.complete(23, item["key"], photo=photo, now=D0 + 10 * H + 60)
        self.assertEqual("missed", out["status"])

    def test_only_bound_staff_or_boss_can_tick(self):
        with self.assertRaises(checklist.ChecklistNotFound):
            self.complete(24, self.plain_item["key"])        # B 店店员
        with self.assertRaises(checklist.ChecklistNotFound):
            checklist.complete_item(3, 30, self.run["id"], self.plain_item["key"],
                                    photo=None, now=D0)       # 别家老板
        with self.assertRaises(checklist.ChecklistNotFound):
            self.complete(23, "nope")
        out = self.complete(20, self.plain_item["key"])      # 老板看全部门店
        self.assertEqual(1, out["done_count"])

    def test_runs_for_user_and_overview_follow_branch_binding(self):
        mine = checklist.runs_for_user(2, 23, DAY, now=D0 + 8 * H)
        self.assertEqual({self.a}, {r["branch_id"] for r in mine})
        self.assertEqual(3, len(mine))
        self.assertTrue(all(not r["assigned_to_me"] for r in mine))
        manager = checklist.runs_for_user(2, 22, DAY, now=D0 + 8 * H)
        self.assertTrue(all(r["assigned_to_me"] for r in manager))
        self.assertEqual({self.b}, {r["branch_id"] for r in
                                    checklist.runs_for_user(2, 24, DAY, now=D0)})
        # 老板不绑定门店：我的清单为空，总览看全部
        self.assertEqual([], checklist.runs_for_user(2, 20, DAY, now=D0))
        overview = checklist.runs_overview(2, 20, DAY, now=D0 + 8 * H)
        self.assertEqual(["朝阳店", "静安店"], [s["branch_name"] for s in overview["stores"]])
        self.assertEqual(6, overview["summary"]["total"])
        scoped = checklist.runs_overview(2, 22, DAY, now=D0 + 8 * H)
        self.assertEqual([self.a], [s["branch_id"] for s in scoped["stores"]])
        with self.assertRaises(checklist.ChecklistError):
            checklist.runs_for_user(2, 23, "2026-13-01")
        card = mine[0]
        for field in ("id", "branch_name", "name", "kind_label", "due_text", "items",
                      "done_count", "total", "status_label"):
            self.assertIn(field, card)


if __name__ == "__main__":
    unittest.main()
