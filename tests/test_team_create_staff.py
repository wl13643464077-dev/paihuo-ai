"""团队页建店员/店长账号：系统生成初始密码只回一次、不开 AI 专家、建号时直接分门店。"""
import json
import os
import tempfile
import unittest

from fastapi import HTTPException

from app import auth, db, main, stafftask


class TeamCreateStaffCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "fresh.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "餐饮企业", "balance": 10})
        db.insert("tenants", {"id": 3, "name": "别人家", "balance": 10})
        for tid in (2, 3):
            db.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                "VALUES(?,?,1,0)", (tid, "restaurant"))
        self.owner = db.insert("users", {
            "tenant_id": 2, "username": "boss2", "password_hash": "x",
            "role": "owner", "modules_json": "[]", "enabled": 1})
        self.b1 = db.insert("store_branch", {"tenant_id": 2, "industry_key": "restaurant",
                                             "name": "人民路店", "active": 1, "created_at": 0})
        self.b2 = db.insert("store_branch", {"tenant_id": 2, "industry_key": "restaurant",
                                             "name": "中山路店", "active": 1, "created_at": 0})
        self.closed = db.insert("store_branch", {"tenant_id": 2, "industry_key": "restaurant",
                                                 "name": "已关店", "active": 0, "created_at": 0})
        self.foreign = db.insert("store_branch", {"tenant_id": 3, "industry_key": "restaurant",
                                                  "name": "别家店", "active": 1, "created_at": 0})
        auth.set_current(auth.get_user(self.owner))

    def tearDown(self):
        auth.set_current(None)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _row(self, uid):
        return db.one("SELECT * FROM users WHERE id=?", (uid,))

    def test_empty_password_generates_one_returned_once_and_stores_only_hash(self):
        out = main.team_user_create({"username": "xiaoli", "job_title": "staff",
                                     "modules": [], "allowed_emp_idxs": []})
        pw = out["initial_password"]
        self.assertEqual(16, len(pw))
        self.assertFalse(auth.password_policy_error(pw))
        row = self._row(out["id"])
        self.assertNotEqual(pw, row["password_hash"])
        self.assertNotIn(pw, json.dumps(row, ensure_ascii=False, default=str))
        self.assertTrue(auth.check_pw(pw, row["password_hash"]))
        # 别人知道这个密码(老板、聊天记录)，首次登录必须换。
        self.assertEqual(1, row["must_change_password"])
        # 不当作老板自助开户那种「系统密码只温和提示」的账号。
        self.assertFalse(auth.password_hint_pending(out["id"]))
        # 团队列表等后续接口都拿不到明文
        listing = json.dumps(main.team_get(), ensure_ascii=False, default=str)
        self.assertNotIn(pw, listing)

    def test_owner_typed_password_is_not_echoed_and_weak_one_rejected(self):
        out = main.team_user_create({"username": "xiaozhang", "password": "TypedPassword2026"})
        self.assertNotIn("initial_password", out)
        self.assertTrue(auth.check_pw("TypedPassword2026", self._row(out["id"])["password_hash"]))
        with self.assertRaises(HTTPException) as caught:
            main.team_user_create({"username": "weak", "password": "123456"})
        self.assertEqual(400, caught.exception.status_code)

    def test_store_staff_created_with_no_modules_and_no_experts(self):
        out = main.team_user_create({"username": "nostore", "job_title": "staff",
                                     "modules": [], "allowed_emp_idxs": []})
        row = self._row(out["id"])
        self.assertEqual([], json.loads(row["modules_json"]))
        self.assertEqual([], json.loads(row["allowed_emp_idxs_json"]))
        self.assertEqual("staff", row["job_title"])

    def test_store_assignment_in_create_opens_store_industry_but_not_experts(self):
        out = main.team_user_create({"username": "dz", "job_title": "manager", "modules": [],
                                     "allowed_emp_idxs": [], "branch_ids": [self.b1, self.b2]})
        self.assertEqual(sorted([self.b1, self.b2]), out["branch_ids"])
        row = self._row(out["id"])
        self.assertEqual(["restaurant"], json.loads(row["modules_json"]))
        self.assertEqual([], json.loads(row["allowed_emp_idxs_json"]))
        bound = {r["branch_id"] for r in db.q(
            "SELECT branch_id FROM user_branch WHERE user_id=?", (out["id"],))}
        self.assertEqual({self.b1, self.b2}, bound)
        # 店长登录后能派这两家店的活，但一个行业专家都用不了。
        member = auth.get_user(out["id"])
        self.assertTrue(stafftask.prefers_staff_home(member))
        opts = stafftask.dispatch_options(2, member)
        self.assertEqual({self.b1, self.b2}, {b["id"] for b in opts["branches"]})
        auth.set_current(member)
        from app import departments
        dept = next(d for d in departments.list_depts() if d["key"] == "restaurant")
        self.assertFalse(auth.employee_allowed(dept["employees"][0]["idx"], "restaurant"))

    def test_bad_store_in_create_creates_nothing(self):
        for bad in (self.closed, self.foreign, 999999):
            with self.subTest(branch=bad):
                with self.assertRaises(HTTPException):
                    main.team_user_create({"username": f"bad{bad}", "job_title": "staff",
                                           "branch_ids": [bad]})
                self.assertIsNone(db.one("SELECT id FROM users WHERE username=?", (f"bad{bad}",)))

    def test_branch_form_can_open_store_industry_for_staff(self):
        out = main.team_user_create({"username": "later", "job_title": "staff",
                                     "modules": [], "allowed_emp_idxs": []})
        main.team_user_branches(out["id"], {"branch_ids": [self.b1], "open_industries": True})
        row = self._row(out["id"])
        self.assertEqual(["restaurant"], json.loads(row["modules_json"]))
        self.assertEqual([], json.loads(row["allowed_emp_idxs_json"]))
        # 不带 open_industries 时维持原来的严格校验。
        other = main.team_user_create({"username": "strict", "job_title": "staff",
                                       "modules": [], "allowed_emp_idxs": []})
        with self.assertRaises(HTTPException):
            main.team_user_branches(other["id"], {"branch_ids": [self.b1]})

    def test_member_cannot_create_accounts(self):
        member = db.insert("users", {"tenant_id": 2, "username": "mgr", "password_hash": "x",
                                     "role": "member", "job_title": "director",
                                     "modules_json": "[]", "enabled": 1})
        auth.set_current(auth.get_user(member))
        with self.assertRaises(HTTPException) as caught:
            main.team_user_create({"username": "sneaky"})
        self.assertEqual(403, caught.exception.status_code)
        with self.assertRaises(HTTPException):
            main.team_user_branches(self.owner, {"branch_ids": [self.b1], "open_industries": True})

    def test_staff_home_full_version_link_only_for_owner_and_director(self):
        staff = main.team_user_create({"username": "s1", "job_title": "staff", "modules": [],
                                       "allowed_emp_idxs": [], "branch_ids": [self.b1]})
        self.assertFalse(stafftask.todo_for_user(2, staff["id"])["user"]["can_open_full"])
        self.assertTrue(stafftask.todo_for_user(2, self.owner)["user"]["can_open_full"])

    def test_team_module_labels_use_short_industry_names(self):
        labels = {m["key"]: m["label"] for m in main.team_get()["all_modules"]}
        self.assertTrue(labels["restaurant"].endswith("餐饮"), labels["restaurant"])


if __name__ == "__main__":
    unittest.main()
