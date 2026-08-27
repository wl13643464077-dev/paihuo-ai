"""schema57 副账号职级与数字员工白名单：老板全权，总监/经理限域分配，后端强制。"""
import json
import os
import tempfile
import unittest

from fastapi import HTTPException

from app import auth, db, departments, main


def _industry_employee(dept_key: str, position: int = 0) -> dict:
    dept = next(
        d for d in departments.list_depts() if d["key"] == dept_key
    )
    e = dept["employees"][position]
    return {**e, "dept_key": dept_key}


class TeamHierarchyCase(unittest.TestCase):
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
        for industry in ("restaurant", "auto"):
            db.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,"
                "is_primary,created_at) VALUES(?,?,?,0)",
                (2, industry, 0),
            )
        self.rest_a = _industry_employee("restaurant", 0)
        self.rest_b = _industry_employee("restaurant", 1)
        self.auto_a = _industry_employee("auto", 0)
        self.uid_owner = self._user("boss2", "owner", [])
        self.uid_director = self._user(
            "dir1", "member", ["restaurant", "auto"], job_title="director",
        )
        self.uid_manager = self._user(
            "mgr1", "member", ["restaurant"], job_title="manager",
        )
        self.uid_staff = self._user(
            "stf1", "member", ["restaurant"], job_title="staff",
        )

    def tearDown(self):
        auth.set_current(None)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    @staticmethod
    def _user(name, role, modules, job_title="staff", whitelist=None):
        return db.insert("users", {
            "tenant_id": 2,
            "username": name,
            "password_hash": "x",
            "role": role,
            "modules_json": json.dumps(modules),
            "job_title": job_title,
            "allowed_emp_idxs_json": (
                json.dumps(whitelist) if whitelist is not None else None
            ),
            "enabled": 1,
        })

    @staticmethod
    def _login(uid):
        user = auth.get_user(uid)
        assert user, uid
        auth.set_current(user)
        return user

    # ---------- schema ----------

    def test_schema57_columns_and_ledger(self):
        cols = {r["name"] for r in db.q("PRAGMA table_info(users)")}
        self.assertIn("job_title", cols)
        self.assertIn("allowed_emp_idxs_json", cols)
        row = db.one("SELECT name FROM schema_version WHERE version=57")
        self.assertEqual(
            "member-hierarchy-employee-allocation", (row or {}).get("name")
        )

    # ---------- auth 语义 ----------

    def test_owner_bypasses_whitelist(self):
        self._login(self.uid_owner)
        self.assertTrue(
            auth.employee_allowed(self.rest_a["idx"], "restaurant")
        )
        self.assertTrue(auth.employee_allowed(self.auto_a["idx"], "auto"))

    def test_member_without_whitelist_gets_full_industry(self):
        self._login(self.uid_staff)
        self.assertTrue(
            auth.employee_allowed(self.rest_a["idx"], "restaurant")
        )
        # 未开通的行业照旧不通(板块边界先于白名单)
        self.assertFalse(auth.employee_allowed(self.auto_a["idx"], "auto"))

    def test_member_whitelist_narrows_to_listed_employees(self):
        db.update("users", self.uid_staff, {
            "allowed_emp_idxs_json": json.dumps([int(self.rest_a["idx"])]),
        })
        self._login(self.uid_staff)
        self.assertTrue(
            auth.employee_allowed(self.rest_a["idx"], "restaurant")
        )
        self.assertFalse(
            auth.employee_allowed(self.rest_b["idx"], "restaurant")
        )

    # ---------- 楼层与员工详情强制 ----------

    def test_depts_list_hides_unassigned_employees(self):
        db.update("users", self.uid_staff, {
            "allowed_emp_idxs_json": json.dumps([int(self.rest_a["idx"])]),
        })
        self._login(self.uid_staff)
        depts = main.depts_list()
        rest = next(d for d in depts if d["key"] == "restaurant")
        idxs = {e["idx"] for e in rest["employees"]}
        self.assertEqual({int(self.rest_a["idx"])}, idxs)

    def test_dept_emp_rejects_unassigned_employee(self):
        db.update("users", self.uid_staff, {
            "allowed_emp_idxs_json": json.dumps([int(self.rest_a["idx"])]),
        })
        self._login(self.uid_staff)
        with self.assertRaises(HTTPException) as ctx:
            main.dept_emp(int(self.rest_b["idx"]))
        self.assertEqual(403, ctx.exception.status_code)
        # 名单内的照常可看
        detail = main.dept_emp(int(self.rest_a["idx"]))
        self.assertEqual(int(self.rest_a["idx"]), detail["idx"])

    # ---------- 团队接口权限矩阵 ----------

    def test_owner_sets_job_title_and_whitelist(self):
        self._login(self.uid_owner)
        main.team_user_update(self.uid_staff, {"job_title": "manager"})
        row = db.one("SELECT job_title FROM users WHERE id=?", (self.uid_staff,))
        self.assertEqual("manager", row["job_title"])
        main.team_user_update(
            self.uid_staff, {"allowed_emp_idxs": [int(self.rest_a["idx"])]},
        )
        row = db.one(
            "SELECT allowed_emp_idxs_json FROM users WHERE id=?",
            (self.uid_staff,),
        )
        self.assertEqual([int(self.rest_a["idx"])], json.loads(row[
            "allowed_emp_idxs_json"]))
        with self.assertRaises(HTTPException) as ctx:
            main.team_user_update(self.uid_staff, {"job_title": "ceo"})
        self.assertEqual(400, ctx.exception.status_code)

    def test_whitelist_must_stay_inside_target_modules(self):
        self._login(self.uid_owner)
        with self.assertRaises(HTTPException) as ctx:
            main.team_user_update(
                self.uid_staff, {"allowed_emp_idxs": [int(self.auto_a["idx"])]},
            )
        self.assertEqual(400, ctx.exception.status_code)

    def test_manager_allocates_to_staff_within_own_industry(self):
        self._login(self.uid_manager)
        main.team_user_update(
            self.uid_staff, {"allowed_emp_idxs": [int(self.rest_a["idx"])]},
        )
        row = db.one(
            "SELECT allowed_emp_idxs_json FROM users WHERE id=?",
            (self.uid_staff,),
        )
        self.assertEqual(
            [int(self.rest_a["idx"])],
            json.loads(row["allowed_emp_idxs_json"]),
        )

    def test_manager_cannot_touch_modules_or_peers_or_self(self):
        self._login(self.uid_manager)
        with self.assertRaises(HTTPException) as ctx:
            main.team_user_update(self.uid_staff, {"modules": ["auto"]})
        self.assertEqual(403, ctx.exception.status_code)
        peer = self._user(
            "mgr2", "member", ["restaurant"], job_title="manager",
        )
        with self.assertRaises(HTTPException) as ctx:
            main.team_user_update(
                peer, {"allowed_emp_idxs": [int(self.rest_a["idx"])]},
            )
        self.assertEqual(403, ctx.exception.status_code)
        with self.assertRaises(HTTPException) as ctx:
            main.team_user_update(
                self.uid_manager,
                {"allowed_emp_idxs": [int(self.rest_a["idx"])]},
            )
        self.assertEqual(403, ctx.exception.status_code)

    def test_restricted_manager_cannot_exceed_own_whitelist(self):
        db.update("users", self.uid_manager, {
            "allowed_emp_idxs_json": json.dumps([int(self.rest_a["idx"])]),
        })
        self._login(self.uid_manager)
        with self.assertRaises(HTTPException) as ctx:
            main.team_user_update(
                self.uid_staff, {"allowed_emp_idxs": [int(self.rest_b["idx"])]},
            )
        self.assertEqual(403, ctx.exception.status_code)
        with self.assertRaises(HTTPException) as ctx:
            main.team_user_update(self.uid_staff, {"allowed_emp_idxs": None})
        self.assertEqual(403, ctx.exception.status_code)

    def test_staff_cannot_manage_anyone(self):
        self._login(self.uid_staff)
        with self.assertRaises(HTTPException) as ctx:
            main.team_user_update(
                self.uid_manager,
                {"allowed_emp_idxs": [int(self.rest_a["idx"])]},
            )
        self.assertEqual(403, ctx.exception.status_code)

    def test_director_manages_manager_and_staff(self):
        self._login(self.uid_director)
        main.team_user_update(
            self.uid_manager, {"allowed_emp_idxs": [int(self.rest_a["idx"])]},
        )
        main.team_user_update(
            self.uid_staff, {"allowed_emp_idxs": [int(self.rest_a["idx"])]},
        )

    def test_team_get_limited_view_for_manager(self):
        self._login(self.uid_manager)
        out = main.team_get()
        self.assertFalse(out["is_admin"])
        self.assertTrue(out["can_allocate"])
        ids = {u["id"] for u in out["users"]}
        self.assertIn(self.uid_manager, ids)
        self.assertIn(self.uid_staff, ids)
        self.assertNotIn(self.uid_owner, ids)
        self.assertNotIn(self.uid_director, ids)
        # 受限视图的企业信息不含余额等敏感字段
        self.assertEqual({"name"}, set(out["tenant"].keys()))
        # 分配器只给自己行业
        keys = {g["key"] for g in out["industry_employees"]}
        self.assertEqual({"restaurant"}, keys)

    def test_team_get_admin_view_keeps_everyone(self):
        self._login(self.uid_owner)
        out = main.team_get()
        self.assertTrue(out["is_admin"])
        ids = {u["id"] for u in out["users"]}
        self.assertEqual(
            {self.uid_owner, self.uid_director, self.uid_manager,
             self.uid_staff},
            ids,
        )
        keys = {g["key"] for g in out["industry_employees"]}
        self.assertEqual({"restaurant", "auto"}, keys)


if __name__ == "__main__":
    unittest.main()
