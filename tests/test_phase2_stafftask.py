"""第 2 期「派给店员的任务」服务层：权限、幂等、拍照提交、审核、轨迹、待办合并、
一句话派活、AI 验照片、扣点退款与通知定向。全部走临时 SQLite 真调用。"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from PIL import Image

from app import db, inspection, notify, stafftask, timeutil

NOW = 1790000000.0   # 2026-09-21 22:13 北京时间(周一)


def _jpeg(w=64, h=48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 160, 200)).save(buf, "JPEG")
    return buf.getvalue()


def _cn(y, m, d, hh, mm=0) -> float:
    from datetime import datetime
    return datetime(y, m, d, hh, mm, tzinfo=timeutil.CN_TZ).timestamp()


class StaffTaskBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "staff.db")
        db.conn()
        self.root = os.path.join(self.tmp.name, "assets")
        for tid, name in ((2, "连锁"), (3, "别家")):
            db.insert("tenants", {"id": tid, "name": name, "industries_json": "[]"})
            db.execute("UPDATE tenants SET balance=10 WHERE id=?", (tid,))
            db.execute("INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,"
                       "created_at) VALUES(?,'restaurant',1,0)", (tid,))
        users = (
            (20, 2, "boss", "owner", "staff", 1),
            (21, 2, "zongjian", "member", "director", 1),
            (22, 2, "renmin-dz", "member", "manager", 1),
            (23, 2, "xiaowang", "member", "staff", 1),
            (24, 2, "xiaoli", "member", "staff", 1),
            (25, 2, "zhongshan-dz", "member", "manager", 1),
            (26, 2, "tingyong", "member", "staff", 1),
            (27, 2, "yunying", "member", "staff", 1),
            (30, 3, "other-boss", "owner", "staff", 1),
        )
        for uid, tid, name, role, title, enabled in users:
            db.insert("users", {"id": uid, "tenant_id": tid, "username": name,
                                "password_hash": "x", "role": role, "job_title": title,
                                "modules_json": '["restaurant"]' if uid != 27 else '["content"]',
                                "enabled": enabled})
        self.a = inspection.create_branch(2, 20, "restaurant", {"name": "人民路店"})["id"]
        self.b = inspection.create_branch(2, 20, "restaurant", {"name": "中山路店"})["id"]
        self.other = inspection.create_branch(3, 30, "restaurant", {"name": "外家店"})["id"]
        for uid, branches in ((22, [self.a]), (23, [self.a]), (24, [self.b]),
                              (25, [self.b]), (26, [self.a])):
            inspection.set_member_branches(20, uid, branches)
        db.execute("UPDATE users SET enabled=0 WHERE id=26")

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def u(self, uid):
        return {"id": uid}

    def create(self, actor=20, **kw):
        kw.setdefault("title", "把冷柜清洗一遍")
        kw.setdefault("branch_id", self.a)
        return stafftask.create_task(2, self.u(actor), now=kw.pop("now", NOW), **kw)

    def submit(self, uid, task_id, photos=1, **kw):
        return stafftask.submit_task(2, self.u(uid), task_id,
                                     photos=[_jpeg() for _ in range(photos)],
                                     asset_root=self.root, now=kw.pop("now", NOW + 60), **kw)

    def balance(self, tid=2):
        return float(db.one("SELECT balance FROM tenants WHERE id=?", (tid,))["balance"])


class PermissionTests(StaffTaskBase):
    def test_dispatch_permission_matrix(self):
        # 老板/总监：任何门店
        self.assertEqual(23, self.create(20, assignee_user_id=23)["assignee_user_id"])
        self.assertEqual(24, self.create(21, branch_id=self.b, assignee_user_id=24)["assignee_user_id"])
        # 老板可以派给总监(不用绑店)
        self.assertEqual(21, self.create(20, assignee_user_id=21)["assignee_user_id"])
        # 店长：只能自己的店、只能派给本店的人
        self.assertEqual(23, self.create(22, assignee_user_id=23)["assignee_user_id"])
        with self.assertRaises(stafftask.StaffTaskNotFound):
            self.create(22, branch_id=self.b)
        with self.assertRaises(stafftask.StaffTaskError):
            self.create(22, assignee_user_id=24)          # 别家店的店员
        with self.assertRaises(stafftask.StaffTaskError):
            self.create(22, assignee_user_id=20)          # 店长不能把活派给老板
        # 店员不能派活
        with self.assertRaises(stafftask.StaffTaskForbidden):
            self.create(23, assignee_user_id=23)
        # 被指派人：停用 / 别家企业 / 没开通这个行业的都不行
        for bad in (26, 30, 27, 999):
            with self.assertRaises(stafftask.StaffTaskError):
                self.create(20, assignee_user_id=bad)
        # 别家企业的门店不存在
        with self.assertRaises(stafftask.StaffTaskNotFound):
            self.create(20, branch_id=self.other)
        # 别家企业的老板拿不到这个租户的操作权
        with self.assertRaises(stafftask.StaffTaskForbidden):
            stafftask.create_task(2, {"id": 30}, title="x", branch_id=self.a)

    def test_branch_inferred_from_single_store_assignee_and_required_otherwise(self):
        task = stafftask.create_task(2, self.u(20), title="补货", assignee_user_id=23)
        self.assertEqual(self.a, task["branch_id"])
        with self.assertRaises(stafftask.StaffTaskError):
            stafftask.create_task(2, self.u(20), title="补货")

    def test_system_created_task_has_no_actor(self):
        task = stafftask.create_task(2, None, title="闭店清单漏了一项", branch_id=self.a,
                                     assignee_user_id=23, source="checklist", source_ref="run:5")
        self.assertIsNone(task["created_by"])
        self.assertEqual("checklist", task["source"])

    def test_request_key_is_idempotent(self):
        first = self.create(20, assignee_user_id=23, request_key="req-abcdefgh-1")
        again = self.create(20, assignee_user_id=23, request_key="req-abcdefgh-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(again["replayed"])
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM staff_task")["n"])
        # 别的门店的店长撞上同一个编号，不泄露那条任务
        with self.assertRaises(stafftask.StaffTaskConflict):
            stafftask.create_task(2, self.u(25), title="x", branch_id=self.b,
                                  request_key="req-abcdefgh-1")
        with self.assertRaises(stafftask.StaffTaskError):
            self.create(20, request_key="short")

    def test_reassign_rules(self):
        task = self.create(20, assignee_user_id=23)
        with self.assertRaises(stafftask.StaffTaskForbidden):
            stafftask.assign_task(2, self.u(23), task["id"], 23)   # 店员不能改派
        moved = stafftask.assign_task(2, self.u(22), task["id"], 22)
        self.assertEqual(22, moved["assignee_user_id"])
        self.assertEqual("改派给 renmin-dz", stafftask.list_events(2, task["id"])[-1]["note"])
        with self.assertRaises(stafftask.StaffTaskNotFound):
            stafftask.assign_task(2, self.u(25), task["id"], 24)    # 别店店长看不到
        with self.assertRaises(stafftask.StaffTaskError):
            stafftask.assign_task(2, self.u(20), task["id"], 24)    # 不在这家店


class IndustryScopeTests(StaffTaskBase):
    def test_director_without_branch_industry_module_cannot_see_task(self):
        task = self.create(title="清点库存", assignee_user_id=23)
        ids = lambda uid: [row["id"] for row in stafftask.list_tasks(2, self.u(uid))["items"]]
        self.assertIn(task["id"], ids(21))                     # 开通了餐饮的总监
        db.execute("UPDATE users SET modules_json='[\"content\"]' WHERE id=21")
        self.assertNotIn(task["id"], ids(21))                  # 撤掉餐饮板块后看不到
        with self.assertRaises(stafftask.StaffTaskNotFound):
            stafftask.get_task(2, self.u(21), task["id"])
        self.assertIn(task["id"], ids(20))                     # 老板不受板块限制


class LifecycleTests(StaffTaskBase):
    def test_submit_requires_photo_and_stores_watermarked_evidence(self):
        stafftask.set_ai_check_enabled(2, self.u(20), True)   # 老板明确打开过才会排 AI 验照片
        task = self.create(20, assignee_user_id=23)
        with self.assertRaises(stafftask.StaffTaskError) as caught:
            self.submit(23, task["id"], photos=0)
        self.assertIn("拍照", str(caught.exception))
        with self.assertRaises(stafftask.StaffTaskNotFound):
            self.submit(24, task["id"])                    # 别店店员看不到
        with self.assertRaises(stafftask.StaffTaskError):
            self.submit(23, task["id"], photos=stafftask.MAX_PHOTOS + 1)
        with self.assertRaises(stafftask.StaffTaskError):   # 不是照片
            stafftask.submit_task(2, self.u(23), task["id"], photos=[b"nope"],
                                  asset_root=self.root)
        done = self.submit(23, task["id"], photos=2, note="过期货已下架")
        self.assertEqual("submitted", done["status"])
        self.assertEqual(2, len(done["photos"]))
        self.assertTrue(done["ai_check_pending"])
        for photo in done["photos"]:
            key = photo["url"][len("/files/"):]
            self.assertTrue(os.path.isfile(os.path.join(self.root, key)))
            self.assertIn("人民路店", photo["watermark_text"])
            self.assertIn("xiaowang", photo["watermark_text"])
        # 网络重试：同一个人再交一次，返回原结果，不多存照片
        again = self.submit(23, task["id"], photos=1)
        self.assertTrue(again["replayed"])
        self.assertEqual(2, db.one("SELECT COUNT(*) n FROM staff_task_photo")["n"])
        files = [f for _, _, fs in os.walk(self.root) for f in fs]
        self.assertEqual(2, len(files))

    def test_photo_optional_task_and_unassigned_claim(self):
        task = self.create(20, require_photo=False)
        self.assertIsNone(task["assignee_user_id"])
        with self.assertRaises(stafftask.StaffTaskNotFound):
            self.submit(24, task["id"], photos=0)          # 不是这家店
        done = self.submit(23, task["id"], photos=0)
        self.assertEqual(23, done["assignee_user_id"])
        self.assertFalse(done["ai_check_pending"])

    def test_review_flow_and_event_trail(self):
        task = self.create(22, assignee_user_id=23)
        self.submit(23, task["id"])
        with self.assertRaises(stafftask.StaffTaskForbidden):
            stafftask.review_task(2, self.u(23), task["id"], approve=True)
        with self.assertRaises(stafftask.StaffTaskNotFound):
            stafftask.review_task(2, self.u(25), task["id"], approve=True)
        with self.assertRaises(stafftask.StaffTaskError):
            stafftask.review_task(2, self.u(22), task["id"], approve=False)   # 打回要原因
        back = stafftask.review_task(2, self.u(22), task["id"], approve=False,
                                     note="底层没擦", now=NOW + 120)
        self.assertEqual("todo", back["status"])
        self.assertEqual("底层没擦", back["review_note"])
        self.assertEqual(1, len(back["earlier_photos"]))
        self.assertEqual([], back["photos"])
        with self.assertRaises(stafftask.StaffTaskConflict):
            stafftask.review_task(2, self.u(22), task["id"], approve=True)
        self.submit(23, task["id"], now=NOW + 300)
        ok = stafftask.review_task(2, self.u(20), task["id"], approve=True, now=NOW + 400)
        self.assertEqual("approved", ok["status"])
        self.assertEqual(20, ok["reviewed_by"])
        detail = stafftask.get_task(2, self.u(20), task["id"])
        self.assertEqual(
            ["created", "assigned", "submitted", "rejected", "submitted", "approved"],
            [e["kind"] for e in detail["events"]],
        )
        self.assertEqual(1, len(detail["photos"]))
        self.assertEqual(1, len(detail["earlier_photos"]))

    def test_manager_cannot_review_own_submission(self):
        task = self.create(20, assignee_user_id=22)
        self.submit(22, task["id"])
        with self.assertRaises(stafftask.StaffTaskForbidden):
            stafftask.review_task(2, self.u(22), task["id"], approve=True)
        self.assertEqual("approved", stafftask.review_task(
            2, self.u(21), task["id"], approve=True)["status"])

    def test_cancel(self):
        task = self.create(22, assignee_user_id=23)
        with self.assertRaises(stafftask.StaffTaskForbidden):
            stafftask.cancel_task(2, self.u(23), task["id"])
        done = stafftask.cancel_task(2, self.u(22), task["id"], note="不用了")
        self.assertEqual("cancelled", done["status"])
        with self.assertRaises(stafftask.StaffTaskConflict):
            self.submit(23, task["id"])
        with self.assertRaises(stafftask.StaffTaskConflict):
            stafftask.cancel_task(2, self.u(20), task["id"])
        self.assertEqual("cancelled", stafftask.list_events(2, task["id"])[-1]["kind"])

    def test_list_scoping_and_filters(self):
        t1 = self.create(20, assignee_user_id=23)["id"]
        t2 = self.create(20, branch_id=self.b, assignee_user_id=24)["id"]
        t3 = self.create(20)["id"]                                    # 人民路店未指派
        t4 = self.create(20, assignee_user_id=22)["id"]
        ids = lambda uid, **kw: {i["id"] for i in stafftask.list_tasks(2, self.u(uid), **kw)["items"]}
        self.assertEqual({t1, t2, t3, t4}, ids(20))
        self.assertEqual({t1, t2, t3, t4}, ids(21))
        self.assertEqual({t1, t3, t4}, ids(22))
        self.assertEqual({t1, t3}, ids(23))
        self.assertEqual({t2}, ids(24))
        self.assertEqual({t2}, ids(20, branch_id=self.b))
        self.assertEqual({t1}, ids(20, assignee_user_id=23))
        self.submit(23, t1)
        self.assertEqual({t1}, ids(20, status="submitted"))
        with self.assertRaises(stafftask.StaffTaskError):
            ids(20, status="bogus")
        page = stafftask.list_tasks(2, self.u(20), limit=2)
        self.assertEqual(2, len(page["items"]))
        rest = stafftask.list_tasks(2, self.u(20), limit=2, before_id=page["next_before_id"])
        self.assertEqual(2, len(rest["items"]))
        self.assertIsNone(rest["next_before_id"])
        with self.assertRaises(stafftask.StaffTaskNotFound):
            stafftask.get_task(2, self.u(24), t1)


class TodoTests(StaffTaskBase):
    def test_todo_merges_three_kinds_sorted_by_urgency(self):
        now = _cn(2026, 9, 25, 10, 0)
        overdue = self.create(20, assignee_user_id=23, title="逾期的活",
                              due_at=_cn(2026, 9, 24, 18))["id"]
        later = self.create(20, assignee_user_id=23, title="下周的活",
                            due_at=_cn(2026, 9, 30, 18))["id"]
        today = self.create(20, assignee_user_id=23, title="今天的活",
                            due_at=_cn(2026, 9, 25, 20))["id"]
        nodue = self.create(20, title="门店公共活")["id"]          # 未指派，本店可见
        self.create(20, branch_id=self.b, assignee_user_id=24, title="别店的活")
        calls = {}

        def runs_for_user(tid, uid, date):
            calls["checklist"] = (tid, uid, date)
            return [{"id": 7, "name": "开店清单", "branch_id": self.a, "status": "open",
                     "due_at": _cn(2026, 9, 25, 9, 30),
                     "items": [{"key": "a", "text": "开灯", "done": True},
                               {"key": "b", "text": "拍冷柜", "done": False, "require_photo": True}]},
                    {"id": 8, "name": "闭店清单", "branch_id": self.a, "status": "open",
                     "due_at": _cn(2026, 9, 25, 22)}]

        fake = types.ModuleType("app.checklist")
        fake.runs_for_user = runs_for_user
        actions = [{"id": 5, "visit_id": 1, "issue_title": "地面有水渍", "plan": "拖干",
                    "status": "open", "due_at": _cn(2026, 9, 23, 12)},
                   {"id": 6, "visit_id": 1, "issue_title": "已提交复查",
                    "status": "awaiting_recheck", "due_at": _cn(2026, 9, 20, 12)}]
        with mock.patch.dict(sys.modules, {"app.checklist": fake}), \
                mock.patch.object(inspection, "actions_for_assignee",
                                  create=True, new=lambda tid, uid: actions):
            todo = stafftask.todo_for_user(2, 23, now=now)
        self.assertEqual((2, 23, "2026-09-25"), calls["checklist"])
        self.assertEqual("9 月 25 日", todo["date_text"])
        self.assertEqual(["人民路店"], [b["name"] for b in todo["branches"]])
        order = [(i["kind"], i["id"]) for i in todo["items"]]
        self.assertEqual([
            ("inspection_action", 5), ("task", overdue), ("checklist", 7),     # 逾期
            ("task", today), ("checklist", 8),                                  # 今天
            ("task", later), ("task", nodue),                                   # 其他
            ("inspection_action", 6),                                           # 已交等审核
        ], order)
        self.assertEqual({"done": 1, "total": 2}, todo["items"][2]["progress"])
        self.assertEqual(3, todo["counts"]["overdue"])
        self.assertEqual({overdue, today, later, nodue}, {t["id"] for t in todo["tasks"]})
        self.assertEqual([], todo["reviews"])
        self.assertFalse(todo["user"]["can_dispatch"])

    def test_todo_tolerates_missing_or_broken_partner_modules(self):
        self.create(20, assignee_user_id=23)
        with mock.patch.dict(sys.modules, {"app.checklist": None}):
            todo = stafftask.todo_for_user(2, 23, now=NOW)
        self.assertEqual([], todo["checklists"])
        broken = types.ModuleType("app.checklist")
        broken.runs_for_user = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.dict(sys.modules, {"app.checklist": broken}), \
                mock.patch.object(inspection, "actions_for_assignee", create=True,
                                  new=mock.Mock(side_effect=RuntimeError("boom"))):
            todo = stafftask.todo_for_user(2, 23, now=NOW)
        self.assertEqual(1, len(todo["items"]))
        with self.assertRaises(stafftask.StaffTaskForbidden):
            stafftask.todo_for_user(2, 26)                      # 停用

    def test_manager_todo_includes_reviews_and_submitted_tasks_wait(self):
        task = self.create(22, assignee_user_id=23)
        self.submit(23, task["id"])
        staff = stafftask.todo_for_user(2, 23, now=NOW + 120)
        self.assertEqual("waiting", staff["items"][0]["urgency"])
        manager = stafftask.todo_for_user(2, 22, now=NOW + 120)
        self.assertEqual([task["id"]], [r["id"] for r in manager["reviews"]])
        self.assertTrue(manager["user"]["can_dispatch"])
        self.assertEqual([], stafftask.todo_for_user(2, 25, now=NOW)["reviews"])

    def test_approved_today_stays_on_staff_home_until_next_day(self):
        task = self.create(20, assignee_user_id=23, due_at=_cn(2026, 9, 25, 18))
        self.submit(23, task["id"], now=_cn(2026, 9, 25, 9))
        stafftask.review_task(2, self.u(20), task["id"], approve=True, now=_cn(2026, 9, 25, 10))
        todo = stafftask.todo_for_user(2, 23, now=_cn(2026, 9, 25, 21))
        mine = [t for t in todo["items"] if t["kind"] == "task"]
        self.assertEqual([(task["id"], "approved", "approved")],
                         [(t["id"], t["status"], t["urgency"]) for t in mine])
        self.assertEqual(0, todo["counts"]["todo"])             # 不算在「要做的」里
        # 别的店员看不到别人通过的活；第二天收起
        self.assertEqual([], [t for t in stafftask.todo_for_user(2, 24, now=_cn(2026, 9, 25, 21))["items"]
                              if t["kind"] == "task"])
        tomorrow = stafftask.todo_for_user(2, 23, now=_cn(2026, 9, 26, 8))
        self.assertEqual([], [t for t in tomorrow["items"] if t["kind"] == "task"])

    def test_resubmitted_task_tells_reviewer_last_reason_and_round(self):
        task = self.create(22, assignee_user_id=23)
        self.submit(23, task["id"], now=NOW + 60)
        first = stafftask.todo_for_user(2, 22, now=NOW + 90)["reviews"][0]
        self.assertEqual("", first["review_note"])
        self.assertEqual(1, first["submit_count"])
        stafftask.review_task(2, self.u(22), task["id"], approve=False,
                              note="底层没擦", now=NOW + 120)
        self.submit(23, task["id"], now=NOW + 180)
        again = stafftask.todo_for_user(2, 22, now=NOW + 200)["reviews"][0]
        self.assertEqual("底层没擦", again["review_note"])
        self.assertEqual(2, again["submit_count"])
        listed = stafftask.list_tasks(2, self.u(20), status="submitted")["items"][0]
        self.assertEqual(2, listed["submit_count"])

    def test_todo_has_any_task_tells_never_assigned_from_all_done(self):
        # 从没被派过活：空待办 + has_any_task=False（手机端显示「还没有人给您派活」）
        fresh = stafftask.todo_for_user(2, 24, now=NOW)
        self.assertEqual([], fresh["items"])
        self.assertFalse(fresh["has_any_task"])
        # 有活没做完：True
        task = self.create(20, assignee_user_id=23)
        self.assertTrue(stafftask.todo_for_user(2, 23, now=NOW)["has_any_task"])
        # 交了、过了几天从列表里掉出去：待办空了，但仍然算「派过活」
        self.submit(23, task["id"])
        later = stafftask.todo_for_user(2, 23, now=NOW + 5 * 86400)
        self.assertEqual([], later["items"])
        self.assertTrue(later["has_any_task"])


class StoreTasksTests(StaffTaskBase):
    """店长手机首页「本店在做的活」：只看自己门店，店员拿不到别人的活。"""

    def test_manager_sees_own_store_tasks_only_and_staff_gets_nothing_extra(self):
        mine_a = self.create(20, assignee_user_id=23, title="冷柜清洗", due_at=NOW + 3600)
        other_b = self.create(20, branch_id=self.b, assignee_user_id=24, title="中山路盘点")
        rejected = self.create(22, assignee_user_id=23, title="擦玻璃")
        self.submit(23, rejected["id"], now=NOW + 60)
        stafftask.review_task(2, self.u(22), rejected["id"], approve=False,
                              note="没擦干净", now=NOW + 90)
        waiting = self.create(20, assignee_user_id=23, title="补货")
        self.submit(23, waiting["id"], now=NOW + 100)
        approved = self.create(20, assignee_user_id=23, title="已通过的")
        self.submit(23, approved["id"], now=NOW + 110)
        stafftask.review_task(2, self.u(20), approved["id"], approve=True, now=NOW + 120)
        own = self.create(20, assignee_user_id=22, title="店长自己的活")
        foreign = stafftask.create_task(3, self.u(30), title="外家活", branch_id=self.other,
                                        now=NOW)

        manager = stafftask.todo_for_user(2, 22, now=NOW + 200)
        got = {t["id"]: t for t in manager["store_tasks"]}
        # 等审核排第一，其次被打回，再是在做的
        self.assertEqual([waiting["id"], rejected["id"], mine_a["id"]],
                         [t["id"] for t in manager["store_tasks"]])
        self.assertEqual("submitted", got[waiting["id"]]["store_state"])
        self.assertEqual("rejected", got[rejected["id"]]["store_state"])
        self.assertEqual("xiaowang", got[mine_a["id"]]["assignee_name"])
        self.assertTrue(got[mine_a["id"]]["due_text"])
        for absent in (other_b, approved, own, foreign):
            self.assertNotIn(absent["id"], got)
        # 另一家店的店长只看到自己店
        zhongshan = stafftask.todo_for_user(2, 25, now=NOW + 200)
        self.assertEqual([other_b["id"]], [t["id"] for t in zhongshan["store_tasks"]])
        # 店员一律没有
        for uid in (23, 24):
            self.assertEqual([], stafftask.todo_for_user(2, uid, now=NOW + 200)["store_tasks"])
        self.assertEqual([], stafftask.store_tasks_for_manager(2, stafftask._load_user(2, 23)))
        # 店长能打开只读详情，审核卡能用
        detail = stafftask.get_task(2, self.u(22), waiting["id"])
        self.assertTrue(detail["can_manage"])
        # 被取消门店绑定后立即看不到
        inspection.set_member_branches(20, 22, [self.b])
        moved = stafftask.todo_for_user(2, 22, now=NOW + 200)
        self.assertEqual({other_b["id"]}, {t["id"] for t in moved["store_tasks"]})

    def test_unassigned_store_task_is_not_listed_twice(self):
        open_task = self.create(20, title="谁有空谁做")
        manager = stafftask.todo_for_user(2, 22, now=NOW)
        self.assertIn(open_task["id"], [t["id"] for t in manager["items"] if t["kind"] == "task"])
        self.assertNotIn(open_task["id"], [t["id"] for t in manager["store_tasks"]])

    def test_dispatcher_label_shows_role(self):
        by_boss = self.create(20, assignee_user_id=23)
        by_manager = self.create(22, assignee_user_id=23)
        by_director = self.create(21, assignee_user_id=23)
        self.assertEqual("老板 boss", stafftask.get_task(2, self.u(23), by_boss["id"])["dispatcher_label"])
        self.assertEqual("店长 renmin-dz",
                         stafftask.get_task(2, self.u(23), by_manager["id"])["dispatcher_label"])
        self.assertEqual("总监 zongjian",
                         stafftask.get_task(2, self.u(23), by_director["id"])["dispatcher_label"])
        system = stafftask.create_task(2, None, title="系统派的", branch_id=self.a,
                                       assignee_user_id=23, now=NOW)
        self.assertEqual("", stafftask.get_task(2, self.u(23), system["id"])["dispatcher_label"])


class OneLinerTests(StaffTaskBase):
    def parse(self, uid, text, data=None, exc=None):
        prompts = []

        async def call(prompt, tid):
            prompts.append(prompt)
            if exc:
                raise exc
            return {"data": data}

        result = asyncio.run(stafftask.parse_one_liner(2, self.u(uid), text, call=call,
                                                       now=_cn(2026, 9, 25, 10)))
        return result, prompts

    def test_parse_matches_real_stores_and_people_and_charges(self):
        data = {"tasks": [
            {"title": "把冷柜清一遍", "detail": "", "store": "人民路店", "person": "店长",
             "due": "2026-09-26 12:00", "require_photo": True},
            {"title": "盘点饮料", "store": "火星店", "person": "小王", "due": "明天"},
            {"title": "擦玻璃", "store": "中山路", "person": "xiaoli", "due": "2020-01-01 10:00",
             "require_photo": "yes"},
            {"title": ""}, "垃圾", {"title": 123},
        ]}
        result, prompts = self.parse(20, "人民路店明天中午前把冷柜清一遍拍照给我", data)
        self.assertIn("2026-09-25 10:00", prompts[0])
        drafts = result["drafts"]
        self.assertEqual(3, len(drafts))
        first, second, third = drafts
        self.assertEqual((self.a, 22), (first["branch_id"], first["assignee_user_id"]))
        self.assertEqual(_cn(2026, 9, 26, 12), first["due_at"])
        self.assertTrue(first["require_photo"])
        self.assertEqual([], first["hints"])
        # 匹配不到的门店/人一律留空，不编造
        self.assertIsNone(second["branch_id"])
        self.assertIsNone(second["assignee_user_id"])
        self.assertIsNone(second["due_at"])
        self.assertTrue(second["hints"])
        # 模糊门店名唯一命中；过去的时间丢掉；非布尔的拍照要求按默认 true
        self.assertEqual((self.b, 24), (third["branch_id"], third["assignee_user_id"]))
        self.assertIsNone(third["due_at"])
        self.assertTrue(third["require_photo"])
        self.assertAlmostEqual(9.8, self.balance())
        op = db.one("SELECT status,points FROM billing_operation WHERE action='staff_parse'")
        self.assertEqual(("succeeded", 0.2), (op["status"], op["points"]))
        # 草稿不落库
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM staff_task")["n"])

    def test_manager_only_sees_own_store_and_defaults_to_it(self):
        data = {"tasks": [{"title": "拖地", "store": None, "person": "xiaowang"},
                          {"title": "补货", "store": "中山路店", "person": "xiaoli"}]}
        result, _ = self.parse(22, "拖地，中山路店补货", data)
        own, other = result["drafts"]
        self.assertEqual((self.a, 23), (own["branch_id"], own["assignee_user_id"]))
        self.assertIsNone(other["branch_id"])            # 店长看不到别家店
        self.assertIsNone(other["assignee_user_id"])

    def test_ambiguous_names_are_left_empty(self):
        inspection.create_branch(2, 20, "restaurant", {"name": "人民路二店"})
        branch, why = stafftask.match_branch("人民路", stafftask._dispatch_branches(
            2, stafftask._load_user(2, 20)))
        self.assertIsNone(branch)
        self.assertIn("好几家", why)
        member, why = stafftask.match_member("店长", [
            {"id": 1, "name": "a", "job_title": "manager"},
            {"id": 2, "name": "b", "job_title": "manager"}])
        self.assertIsNone(member)
        self.assertIsNone(stafftask.match_member("小王", [
            {"id": 1, "name": "wang", "job_title": "staff"}])[0])

    def test_invalid_output_or_model_failure_refunds(self):
        for data, exc in (({"oops": 1}, None), ({"tasks": [{"title": ""}]}, None),
                          (None, RuntimeError("down"))):
            with self.assertRaises(stafftask.StaffTaskError) as caught:
                self.parse(20, "随便说一句", data, exc)
            self.assertEqual(502, caught.exception.status)
            self.assertAlmostEqual(10.0, self.balance())
        statuses = [r["status"] for r in db.q("SELECT status FROM billing_operation")]
        self.assertEqual(["refunded"] * 3, statuses)

    def test_staff_cannot_parse_and_insufficient_points_blocks_call(self):
        with self.assertRaises(stafftask.StaffTaskForbidden):
            self.parse(23, "x", {"tasks": []})
        db.execute("UPDATE tenants SET balance=0.1 WHERE id=2")
        with self.assertRaises(stafftask.StaffTaskError) as caught:
            self.parse(20, "x", {"tasks": [{"title": "a"}]})
        self.assertEqual(402, caught.exception.status)
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM billing_operation")["n"])

    def test_price_missing_from_saved_table_is_free(self):
        import json
        db.set_setting("prices", json.dumps({"content_job": {"points": 18, "label": "x"}}))
        result, _ = self.parse(20, "x", {"tasks": [{"title": "a", "store": "人民路店"}]})
        self.assertEqual(0, result["points"])
        self.assertAlmostEqual(10.0, self.balance())


class AiCheckTests(StaffTaskBase):
    def setUp(self):
        super().setUp()
        stafftask.set_ai_check_enabled(2, self.u(20), True)

    def test_switch_defaults_off_until_owner_decides(self):
        # 从没设置过：默认关，且还没问过老板；明确打开过的企业保持打开
        db.execute("DELETE FROM app_setting WHERE key=?", ("staff_ai_check:2",))
        self.assertFalse(stafftask.ai_check_enabled(2))
        self.assertFalse(stafftask.ai_check_asked(2))
        meta = stafftask.dispatch_options(2, self.u(20))
        self.assertFalse(meta["ai_check_enabled"])
        self.assertFalse(meta["ai_check_asked"])
        task_id = self.submitted()
        result, seen = self.run_check(task_id, {"verdict": "pass"})
        self.assertIsNone(result)
        self.assertEqual({}, seen)                       # 没打开就不调模型、不扣点
        self.assertAlmostEqual(10.0, self.balance())
        stafftask.set_ai_check_enabled(2, self.u(20), False)   # 「先不用」也算表过态
        self.assertTrue(stafftask.ai_check_asked(2))
        self.assertFalse(stafftask.ai_check_enabled(2))
        stafftask.set_ai_check_enabled(2, self.u(20), True)
        self.assertTrue(stafftask.ai_check_enabled(2))
        self.assertTrue(stafftask.dispatch_options(2, self.u(20))["ai_check_asked"])

    def test_ai_verdict_only_reaches_reviewers(self):
        task_id = self.submitted()
        self.run_check(task_id, {"verdict": "pass", "reason": "干净", "confidence": 0.9})
        self.assertEqual("pass", stafftask.get_task(2, self.u(20), task_id)["ai_check"]["verdict"])
        self.assertEqual("pass", stafftask.get_task(2, self.u(22), task_id)["ai_check"]["verdict"])
        # 交差的店员自己看不到 AI 建议(详情、列表、待办都不带)
        self.assertIsNone(stafftask.get_task(2, self.u(23), task_id)["ai_check"])
        listed = stafftask.list_tasks(2, self.u(23))["items"]
        self.assertTrue(listed)
        self.assertTrue(all(t["ai_check"] is None for t in listed))
        todo = stafftask.todo_for_user(2, 23, now=NOW + 120)
        self.assertTrue(all(t.get("ai_check") is None for t in todo["tasks"]))
        manager = stafftask.todo_for_user(2, 22, now=NOW + 120)
        self.assertEqual("pass", manager["reviews"][0]["ai_check"]["verdict"])

    def run_check(self, task_id, raw=None, exc=None):
        seen = {}

        async def call(prompt, images, tid):
            seen["prompt"], seen["images"] = prompt, images
            if exc:
                raise exc
            return raw

        result = asyncio.run(stafftask.run_ai_check(2, task_id, call=call, asset_root=self.root))
        return result, seen

    def submitted(self):
        task = self.create(20, assignee_user_id=23)
        self.submit(23, task["id"], photos=2, note="擦干净了")
        return task["id"]

    def test_pass_verdict_is_saved_as_suggestion_and_charged(self):
        task_id = self.submitted()
        result, seen = self.run_check(task_id, {"verdict": "PASS", "reason": "冷柜很干净",
                                                "confidence": 1.7})
        self.assertEqual({"verdict": "pass", "reason": "冷柜很干净", "confidence": 1.0}, result)
        self.assertEqual(2, len(seen["images"]))
        self.assertIn("把冷柜清洗一遍", seen["prompt"])
        self.assertIn("擦干净了", seen["prompt"])
        task = stafftask.get_task(2, self.u(20), task_id)
        self.assertEqual("submitted", task["status"])     # 只是建议，状态不变
        self.assertEqual("pass", task["ai_check"]["verdict"])
        self.assertEqual("ai_checked", task["events"][-1]["kind"])
        self.assertAlmostEqual(9.8, self.balance())

    def test_failure_never_blocks_submission_and_refunds(self):
        task_id = self.submitted()
        for round_no, (raw, exc) in enumerate(((None, RuntimeError("vision down")),
                                               ({"verdict": "maybe"}, None))):
            if round_no:   # 打回后重交，开始新一轮
                stafftask.review_task(2, self.u(20), task_id, approve=False, note="重拍")
                self.submit(23, task_id, now=NOW + 600)
            result, seen = self.run_check(task_id, raw, exc)
            self.assertIsNone(result)
            self.assertTrue(seen["images"])                # 模型确实被调用了
            task = stafftask.get_task(2, self.u(20), task_id)
            self.assertEqual("submitted", task["status"])
            self.assertTrue(task["ai_check"]["error"])
            self.assertAlmostEqual(10.0, self.balance())
        self.assertEqual(["refunded", "refunded"], [r["status"] for r in db.q(
            "SELECT status FROM billing_operation ORDER BY created_at")])
        # 同一轮已经记过结果：再触发不调模型、不扣点
        result, seen = self.run_check(task_id, {"verdict": "pass"})
        self.assertEqual({}, seen)
        self.assertEqual(2, db.one("SELECT COUNT(*) n FROM billing_operation")["n"])

    def test_disabled_switch_or_no_points_skips_model(self):
        task_id = self.submitted()
        stafftask.set_ai_check_enabled(2, self.u(20), False)
        result, seen = self.run_check(task_id, {"verdict": "pass"})
        self.assertIsNone(result)
        self.assertEqual({}, seen)
        with self.assertRaises(stafftask.StaffTaskForbidden):
            stafftask.set_ai_check_enabled(2, self.u(22), True)
        stafftask.set_ai_check_enabled(2, self.u(20), True)
        db.execute("UPDATE tenants SET balance=0 WHERE id=2")
        result, seen = self.run_check(task_id, {"verdict": "pass"})
        self.assertIsNone(result)
        self.assertEqual({}, seen)
        self.assertIn("点数不足", stafftask.get_task(2, self.u(20), task_id)["ai_check"]["reason"])

    def test_stale_result_after_review_is_not_written(self):
        task_id = self.submitted()
        stafftask.review_task(2, self.u(20), task_id, approve=True)
        result, seen = self.run_check(task_id, {"verdict": "fail"})
        self.assertIsNone(result)
        self.assertEqual({}, seen)                        # 已审核就不再看


class NotificationTests(StaffTaskBase):
    def unread(self, uid):
        user = db.one("SELECT id,tenant_id,role,job_title,enabled,modules_json FROM users WHERE id=?", (uid,))
        user["modules"] = db.jloads(user.pop("modules_json"), [])
        return notify.unread_for_user(2, user)

    def test_personal_notifications_reach_only_their_targets(self):
        task = self.create(22, assignee_user_id=23)
        kinds = lambda uid: [n["kind"] for n in self.unread(uid)]
        self.assertEqual(["staff_task_assigned"], kinds(23))
        self.assertEqual([], kinds(24))
        self.assertEqual([], kinds(20))
        self.submit(23, task["id"])
        self.assertEqual(["staff_task_submitted"], kinds(22))   # 派活人
        self.assertEqual([], kinds(20))
        stafftask.review_task(2, self.u(22), task["id"], approve=False, note="重拍")
        self.assertEqual("你交的活被打回了，请重做", self.unread(23)[0]["title"])
        self.assertEqual(f"#/staff-tasks/{task['id']}", self.unread(23)[0]["link"])
        # 个人通知绝不降级成全员广播
        self.assertIsNone(notify.record(2, "staff_task_assigned", {"task_id": 1}))
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM notification WHERE user_id IS NULL")["n"])

    def test_system_task_submission_notifies_bosses(self):
        task = stafftask.create_task(2, None, title="整改复查", branch_id=self.a,
                                     assignee_user_id=23, source="inspection")
        self.submit(23, task["id"])
        self.assertEqual(["staff_task_submitted"], [n["kind"] for n in self.unread(20)])


class TodayReviewTests(NotificationTests):
    """「今天」页：等我审核的交差件数 + 审核后通知自动收掉。"""

    test_personal_notifications_reach_only_their_targets = None
    test_system_task_submission_notifies_bosses = None

    def count(self, uid, tid=2):
        return stafftask.review_pending_count(tid, self.u(uid))

    def test_review_count_follows_submission_and_review_and_respects_scope(self):
        task = self.create(20, assignee_user_id=23)                 # 人民路店
        for uid in (20, 21, 22, 23, 25):
            self.assertEqual(0, self.count(uid))
        self.submit(23, task["id"])
        self.assertEqual(1, self.count(20))    # 老板
        self.assertEqual(1, self.count(21))    # 总监
        self.assertEqual(1, self.count(22))    # 这家店的店长
        self.assertEqual(0, self.count(25))    # 别家店的店长
        self.assertEqual(0, self.count(23))    # 店员不能审
        self.assertEqual(0, self.count(24))
        self.assertEqual(0, self.count(27))    # 没开这个行业板块的成员
        self.assertEqual(0, self.count(30, tid=3))   # 别的企业
        self.assertEqual(0, self.count(30))          # 跨企业的账号
        self.assertEqual(0, stafftask.review_pending_count(2, None))
        # 店长自己交的活不算进自己的待审核，老板那里要算
        own = self.create(20, assignee_user_id=22)
        self.submit(22, own["id"])
        self.assertEqual(1, self.count(22))
        self.assertEqual(2, self.count(20))
        stafftask.review_task(2, self.u(20), task["id"], approve=True)
        self.assertEqual(1, self.count(20))
        self.assertEqual(0, self.count(22))
        stafftask.review_task(2, self.u(20), own["id"], approve=False, note="重拍")
        self.assertEqual(0, self.count(20))
        # 取消一件等审核的活，数量也跟着走
        third = self.create(20, assignee_user_id=23)
        self.submit(23, third["id"])
        self.assertEqual(1, self.count(20))
        stafftask.cancel_task(2, self.u(20), third["id"])
        self.assertEqual(0, self.count(20))

    def test_review_clears_only_that_tasks_submitted_notice(self):
        kinds = lambda uid: [n["kind"] for n in self.unread(uid)]
        first = self.create(20, assignee_user_id=23)
        second = self.create(20, assignee_user_id=23)
        third = self.create(20, assignee_user_id=23)
        for task in (first, second, third):
            self.submit(23, task["id"])
        self.assertEqual(["staff_task_submitted"] * 3, kinds(20))
        # 别的企业里同编号链接的通知不能被顺手标掉
        db.insert("notification", {"tenant_id": 3, "kind": "staff_task_submitted",
                                   "title": "店员交差了，等您审核", "body": "",
                                   "link": f"#/staff-tasks/{first['id']}", "user_id": 30})
        stafftask.review_task(2, self.u(20), first["id"], approve=True)
        left = self.unread(20)
        self.assertEqual([f"#/staff-tasks/{third['id']}", f"#/staff-tasks/{second['id']}"],
                         [n["link"] for n in left])
        stafftask.review_task(2, self.u(20), second["id"], approve=False, note="没擦干净")
        self.assertEqual([f"#/staff-tasks/{third['id']}"], [n["link"] for n in self.unread(20)])
        stafftask.cancel_task(2, self.u(20), third["id"])
        self.assertEqual([], kinds(20))
        # 店员收到的「通过/打回」通知不受影响；别的企业那条还在
        self.assertEqual(2, kinds(23).count("staff_task_reviewed"))
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) n FROM notification WHERE tenant_id=3 AND read_at IS NULL")["n"])
        # 打回后重交，会有一条新的「等您审核」
        self.submit(23, second["id"], now=NOW + 600)
        self.assertEqual([f"#/staff-tasks/{second['id']}"], [n["link"] for n in self.unread(20)])


class MiscTests(StaffTaskBase):
    def test_prefers_staff_home_only_for_store_bound_staff(self):
        load = lambda uid: db.one("SELECT * FROM users WHERE id=?", (uid,))
        self.assertTrue(stafftask.prefers_staff_home(load(23)))
        self.assertTrue(stafftask.prefers_staff_home(load(22)))
        self.assertFalse(stafftask.prefers_staff_home(load(27)))     # 内容运营副账号
        self.assertFalse(stafftask.prefers_staff_home(load(20)))
        self.assertFalse(stafftask.prefers_staff_home(load(21)))
        self.assertFalse(stafftask.prefers_staff_home(None))

    def test_dispatch_options(self):
        boss = stafftask.dispatch_options(2, self.u(20))
        self.assertEqual({"人民路店", "中山路店"}, {b["name"] for b in boss["branches"]})
        renmin = next(b for b in boss["branches"] if b["id"] == self.a)
        self.assertEqual([22, 23], [m["id"] for m in renmin["members"]])   # 店长在前，停用的不在
        self.assertEqual(0.2, boss["prices"]["staff_parse"])
        manager = stafftask.dispatch_options(2, self.u(22))
        self.assertEqual([self.a], [b["id"] for b in manager["branches"]])
        staff = stafftask.dispatch_options(2, self.u(23))
        self.assertFalse(staff["can_dispatch"])
        self.assertEqual([], staff["branches"])

    def test_parse_due(self):
        self.assertEqual(_cn(2026, 9, 26, 12), stafftask.parse_due("2026-09-26 12:00"))
        self.assertEqual(_cn(2026, 9, 26, 18), stafftask.parse_due("2026-09-26"))
        self.assertEqual(NOW, stafftask.parse_due(NOW))
        self.assertIsNone(stafftask.parse_due(""))
        for bad in ("明天", "2026-13-01", True, 5):
            with self.assertRaises(stafftask.StaffTaskError):
                stafftask.parse_due(bad)

    def test_upload_gate_limits_per_user(self):
        gate = stafftask.UploadGate(global_limit=2, per_user_hour=2)
        key = gate.acquire(2, 23, now=0)
        with self.assertRaises(stafftask.StaffTaskError) as caught:
            gate.acquire(2, 23, now=1)
        self.assertEqual(429, caught.exception.status)
        other = gate.acquire(2, 24, now=1)
        with self.assertRaises(stafftask.StaffTaskError):
            gate.acquire(2, 22, now=1)                       # 全站并发满
        gate.release(key)
        gate.release(other)
        gate.release(gate.acquire(2, 23, now=2))
        with self.assertRaises(stafftask.StaffTaskError):
            gate.acquire(2, 23, now=3)                       # 本小时次数用完
        gate.release(gate.acquire(2, 23, now=3700))


if __name__ == "__main__":
    unittest.main()
