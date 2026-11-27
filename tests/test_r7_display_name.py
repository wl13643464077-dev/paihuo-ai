"""第七轮：账号加姓名(users.display_name，schema v65)。

- v64 旧库升级到 v65：只加一列、保数据、可重复启动；
- 建账号 / 改姓名接口：去空格、限 20 字、可空、权限；
- 显示名回退：填了姓名用姓名，没填用登录名；
- 派活任务、交差通知、催办、清单、驾驶舱、巡店整改输出的人名都走显示名。
"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

from fastapi import HTTPException
from PIL import Image

from app import auth, checklist, cockpit, db, inspection, main, people, stafftask, timeutil


def _reset():
    db._shutdown_async_pool(wait=True)
    db._close_all_connections()
    db._conn = None
    db._conn_path = None


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (90, 160, 200)).save(buf, "JPEG")
    return buf.getvalue()


NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timeutil.CN_TZ).timestamp()


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        _reset()
        db.DB_PATH = os.path.join(self.tmp.name, "names.db")

    def tearDown(self):
        auth.set_current(None)
        _reset()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()


class SchemaV65MigrationTests(_TempDb):
    def _raw(self, *statements):
        _reset()
        connection = sqlite3.connect(db.DB_PATH)
        try:
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _user_columns():
        return {row["name"]: row for row in db.q("PRAGMA table_info(users)")}

    def test_fresh_database_is_v65_with_nullable_display_name(self):
        db.conn()
        self.assertEqual(65, db.LATEST_SCHEMA_VERSION)
        self.assertEqual(65, db.one("PRAGMA user_version")["user_version"])
        self.assertEqual(
            "user-display-name",
            db.one("SELECT name FROM schema_version WHERE version=65")["name"],
        )
        column = self._user_columns()["display_name"]
        self.assertEqual(("TEXT", 0, None),
                         (column["type"], column["notnull"], column["dflt_value"]))
        # 不为这一列建索引
        self.assertFalse([
            row["name"] for row in db.q("PRAGMA index_list(users)")
            if "display" in str(row["name"])
        ])

    def test_v64_database_upgrades_keeps_data_and_is_idempotent(self):
        db.conn()
        db.insert("tenants", {"id": 2, "name": "企业", "industries_json": "[]"})
        db.insert("users", {
            "id": 22, "tenant_id": 2, "username": "laowang", "password_hash": "h",
            "role": "member", "job_title": "manager", "modules_json": "[]",
            "phone": "13800000000", "enabled": 1,
        })
        before = dict(db.one("SELECT * FROM users WHERE id=22"))
        before.pop("display_name")
        # 模拟 v64 旧库：没有 display_name 列，账本和 user_version 停在 64。
        self._raw(
            "ALTER TABLE users DROP COLUMN display_name",
            "DELETE FROM schema_version WHERE version>=65",
            "PRAGMA user_version=64",
        )
        connection = sqlite3.connect(db.DB_PATH)
        try:
            self.assertNotIn("display_name", {
                row[1] for row in connection.execute("PRAGMA table_info(users)")
            })
        finally:
            connection.close()

        db.conn()
        self.assertEqual(65, db.one("PRAGMA user_version")["user_version"])
        self.assertEqual(
            1, db.one("SELECT COUNT(*) n FROM schema_version WHERE version=65")["n"])
        # v64 的账本行原样保留
        self.assertEqual(
            "staff-task-loop",
            db.one("SELECT name FROM schema_version WHERE version=64")["name"],
        )
        after = dict(db.one("SELECT * FROM users WHERE id=22"))
        self.assertIsNone(after.pop("display_name"))    # 不回填
        self.assertEqual(before, after)
        applied_at = db.one(
            "SELECT applied_at FROM schema_version WHERE version=65")["applied_at"]

        # 再启动一次：不重复加列、不改账本
        db.execute("UPDATE users SET display_name='王店长' WHERE id=22")
        _reset()
        db.conn()
        self.assertEqual(65, db.one("PRAGMA user_version")["user_version"])
        self.assertEqual(
            applied_at,
            db.one("SELECT applied_at FROM schema_version WHERE version=65")["applied_at"],
        )
        self.assertEqual(
            "王店长", db.one("SELECT display_name FROM users WHERE id=22")["display_name"])

    def test_ledger_at_65_without_column_is_repaired_not_trusted(self):
        """账本写着 65 但列被人删了：启动时按实体补回来，而不是带着缺列的表服务。"""
        db.conn()
        self._raw("ALTER TABLE users DROP COLUMN display_name")
        db.conn()
        self.assertIn("display_name", self._user_columns())
        db._validate_migrated_database(db.conn())

    def test_post_migration_contract_requires_the_column(self):
        db.conn()
        connection = db.conn()
        connection.execute("ALTER TABLE users DROP COLUMN display_name")
        try:
            with self.assertRaisesRegex(RuntimeError, "display_name"):
                db._validate_migrated_database(connection)
        finally:
            connection.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
            connection.commit()

    def test_v66_database_is_refused_by_this_build(self):
        db.conn()
        self._raw("PRAGMA user_version=66")
        with self.assertRaisesRegex(RuntimeError, "拒绝降级启动"):
            db.conn()


class PeopleHelperTests(unittest.TestCase):
    def test_display_name_falls_back_to_username(self):
        self.assertEqual("小李", people.display_name({"username": "xiaoli", "display_name": "小李"}))
        self.assertEqual("xiaoli", people.display_name({"username": "xiaoli", "display_name": None}))
        self.assertEqual("xiaoli", people.display_name({"username": "xiaoli", "display_name": "  "}))
        self.assertEqual("xiaoli", people.display_name({"username": "xiaoli"}))
        self.assertEqual("", people.display_name(None))

    def test_clean_display_name(self):
        self.assertEqual("小李", people.clean_display_name("  小李  "))
        self.assertEqual("王 店长", people.clean_display_name("王 \t 店长"))
        self.assertIsNone(people.clean_display_name(None))
        self.assertIsNone(people.clean_display_name("   "))
        self.assertEqual("李" * 20, people.clean_display_name("李" * 20))
        for bad in ("李" * 21, "<b>小李</b>", "小'李", '小"李', "小\\李", "小&李", 123):
            with self.assertRaises(ValueError, msg=repr(bad)):
                people.clean_display_name(bad)

    def test_with_role_does_not_repeat_role_already_in_name(self):
        self.assertEqual("小李（店员）", people.with_role("小李", "店员"))
        self.assertEqual("王店长", people.with_role("王店长", "店长"))
        self.assertEqual("小李", people.with_role("小李", ""))
        self.assertEqual("", people.with_role("", "店员"))


class TeamDisplayNameApiTests(_TempDb):
    def setUp(self):
        super().setUp()
        db.conn()
        db.insert("tenants", {"id": 2, "name": "餐饮企业", "balance": 10})
        db.insert("tenants", {"id": 3, "name": "别人家", "balance": 10})
        self.owner = db.insert("users", {
            "tenant_id": 2, "username": "boss2", "password_hash": "x",
            "role": "owner", "modules_json": "[]", "enabled": 1})
        self.foreign = db.insert("users", {
            "tenant_id": 3, "username": "other", "password_hash": "x",
            "role": "member", "modules_json": "[]", "enabled": 1})
        auth.set_current(auth.get_user(self.owner))

    def _name(self, uid):
        return db.one("SELECT display_name FROM users WHERE id=?", (uid,))["display_name"]

    def test_create_with_display_name_trims_and_returns_it(self):
        out = main.team_user_create({"username": "xiaoli", "display_name": "  小李 ",
                                     "job_title": "staff", "modules": [],
                                     "allowed_emp_idxs": []})
        self.assertEqual(("xiaoli", "小李", "小李"),
                         (out["username"], out["display_name"], out["name"]))
        self.assertEqual("小李", self._name(out["id"]))
        listed = {u["username"]: u for u in main.team_get()["users"]}
        self.assertEqual(("小李", "小李"),
                         (listed["xiaoli"]["display_name"], listed["xiaoli"]["name"]))
        # 老板自己没填：display_name 是空串，name 回退登录名
        self.assertEqual(("", "boss2"),
                         (listed["boss2"]["display_name"], listed["boss2"]["name"]))

    def test_create_without_display_name_is_allowed_and_stores_null(self):
        out = main.team_user_create({"username": "noname", "job_title": "staff"})
        self.assertEqual(("", "noname"), (out["display_name"], out["name"]))
        self.assertIsNone(self._name(out["id"]))
        blank = main.team_user_create({"username": "blank", "display_name": "   "})
        self.assertIsNone(self._name(blank["id"]))

    def test_create_rejects_bad_display_name_without_creating_account(self):
        for bad in ("李" * 21, "<script>", 5):
            with self.assertRaises(HTTPException) as caught:
                main.team_user_create({"username": "badname", "display_name": bad})
            self.assertEqual(400, caught.exception.status_code)
            self.assertIn("姓名", caught.exception.detail)
        self.assertIsNone(db.one("SELECT id FROM users WHERE username='badname'"))

    def test_update_sets_changes_and_clears_display_name(self):
        uid = main.team_user_create({"username": "laowang", "job_title": "manager"})["id"]
        main.team_user_update(uid, {"display_name": " 王店长 "})
        self.assertEqual("王店长", self._name(uid))
        main.team_user_update(uid, {"display_name": "老王"})
        self.assertEqual("老王", self._name(uid))
        # 只改别的字段不动姓名
        main.team_user_update(uid, {"enabled": 0})
        self.assertEqual("老王", self._name(uid))
        main.team_user_update(uid, {"display_name": ""})
        self.assertIsNone(self._name(uid))
        with self.assertRaises(HTTPException) as caught:
            main.team_user_update(uid, {"display_name": "王" * 21})
        self.assertEqual(400, caught.exception.status_code)
        self.assertIsNone(self._name(uid))

    def test_owner_can_name_self_and_me_reports_it(self):
        main.team_user_update(self.owner, {"display_name": "王总"})
        auth.set_current(auth.get_user(self.owner))
        me = main.auth_me()
        self.assertEqual(("boss2", "王总", "王总"),
                         (me["username"], me["display_name"], me["name"]))

    def test_me_falls_back_to_username(self):
        me = main.auth_me()
        self.assertEqual(("boss2", "", "boss2"),
                         (me["username"], me["display_name"], me["name"]))

    def test_cannot_rename_other_tenant_and_members_cannot_rename(self):
        with self.assertRaises(HTTPException) as caught:
            main.team_user_update(self.foreign, {"display_name": "越权"})
        self.assertEqual(404, caught.exception.status_code)
        self.assertIsNone(self._name(self.foreign))
        manager = main.team_user_create({
            "username": "mgr", "job_title": "manager", "modules": ["restaurant"]})["id"]
        staff = main.team_user_create({"username": "stf", "job_title": "staff"})["id"]
        auth.set_current(auth.get_user(manager))
        with self.assertRaises(HTTPException) as caught:
            main.team_user_update(staff, {"display_name": "小张"})
        self.assertEqual(403, caught.exception.status_code)
        self.assertIsNone(self._name(staff))

    def test_platform_can_open_tenant_without_owner_name(self):
        if not db.one("SELECT id FROM tenants WHERE id=1"):
            db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        root = db.insert("users", {
            "tenant_id": 1, "username": "boss", "password_hash": "x",
            "role": "root", "modules_json": "[]", "enabled": 1})
        auth.set_current(auth.get_user(root))
        plain = main.team_tenant_create({
            "name": "新客户", "owner": "newowner", "password": "TypedPassword2026"})
        row = db.one("SELECT display_name FROM users WHERE tenant_id=?",
                     (plain["tenant_id"],))
        self.assertIsNone(row["display_name"])
        named = main.team_tenant_create({
            "name": "新客户二", "owner": "newowner2", "password": "TypedPassword2026",
            "owner_display_name": " 李老板 "})
        row = db.one("SELECT display_name FROM users WHERE tenant_id=?",
                     (named["tenant_id"],))
        self.assertEqual("李老板", row["display_name"])


class DisplayNameOutputTests(_TempDb):
    """姓名填了的显示姓名，没填的回退登录名：任务、通知、清单、驾驶舱、巡店同一条规则。"""

    def setUp(self):
        super().setUp()
        db.conn()
        self.root = os.path.join(self.tmp.name, "assets")
        db.insert("tenants", {"id": 2, "name": "连锁", "industries_json": "[]"})
        db.execute("UPDATE tenants SET balance=10 WHERE id=2")
        db.execute("INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,"
                   "created_at) VALUES(2,'restaurant',1,0)")
        users = (
            (20, "wangji", "王总", "owner", "staff"),
            (22, "laowang", "王店长", "member", "manager"),
            (23, "xiaoli", "小李", "member", "staff"),
            (24, "xiaozhang", None, "member", "staff"),      # 没填姓名
        )
        for uid, username, name, role, title in users:
            db.insert("users", {
                "id": uid, "tenant_id": 2, "username": username, "display_name": name,
                "password_hash": "x", "role": role, "job_title": title,
                "modules_json": '["restaurant"]', "enabled": 1})
        self.branch = inspection.create_branch(
            2, 20, "restaurant", {"name": "人民路店"})["id"]
        for uid in (22, 23, 24):
            inspection.set_member_branches(20, uid, [self.branch])

    def _task(self, actor=20, assignee=23, **kw):
        return stafftask.create_task(
            2, {"id": actor}, title=kw.pop("title", "把冷柜清洗一遍"),
            branch_id=self.branch, assignee_user_id=assignee, now=NOW, **kw)

    def test_people_names_lookup_is_tenant_scoped_with_fallback(self):
        db.insert("tenants", {"id": 3, "name": "别家", "industries_json": "[]"})
        db.insert("users", {"id": 30, "tenant_id": 3, "username": "other",
                            "display_name": "外人", "password_hash": "x",
                            "role": "owner", "modules_json": "[]", "enabled": 1})
        self.assertEqual({23: "小李", 24: "xiaozhang"},
                         people.names(2, [23, 24, 30, None, 0, "x"]))
        self.assertEqual("小李", people.name_of(2, 23))
        self.assertIsNone(people.name_of(2, 30))

    def test_task_output_uses_display_names_and_keeps_login_name(self):
        task = self._task()
        self.assertEqual("小李", task["assignee_name"])
        self.assertEqual("xiaoli", task["assignee_username"])
        self.assertEqual("王总", task["created_by_name"])
        self.assertEqual("王总（老板）", task["dispatcher_label"])
        # 姓名里已经带了身份的不重复写
        by_manager = self._task(actor=22)
        self.assertEqual("王店长", by_manager["dispatcher_label"])
        # 没填姓名：回退登录名
        fallback = self._task(assignee=24)
        self.assertEqual(("xiaozhang", "xiaozhang"),
                         (fallback["assignee_name"], fallback["assignee_username"]))

    def test_events_notifications_and_reject_label_use_display_names(self):
        task = self._task()
        events = stafftask.get_task(2, {"id": 20}, task["id"])["events"]
        self.assertEqual(["王总", "王总"], [e["actor_name"] for e in events])
        self.assertIn("指派给 小李", [e["note"] for e in events])

        stafftask.submit_task(2, {"id": 23}, task["id"], photos=[_jpeg()],
                              asset_root=self.root, now=NOW + 60)
        notice = db.one(
            "SELECT title,body FROM notification WHERE tenant_id=2 "
            "AND kind='staff_task_submitted' ORDER BY id DESC LIMIT 1")
        self.assertTrue(notice["body"].startswith("小李 交了「把冷柜清洗一遍」"), notice["body"])
        self.assertNotIn("xiaoli", notice["title"] + notice["body"])

        stafftask.review_task(2, {"id": 22}, task["id"], approve=False,
                              note="底层没擦", now=NOW + 120)
        rejected = stafftask.get_task(2, {"id": 23}, task["id"])
        self.assertEqual("王店长", rejected["reviewer_label"])
        self.assertEqual("王店长", rejected["reviewed_by_name"])

        nudged = stafftask.nudge_task(2, {"id": 20}, task["id"], now=NOW + 180)
        self.assertTrue(nudged["message"].startswith("王总（老板） 催您："))

    def test_reassign_event_and_members_and_todo_header(self):
        task = self._task()
        stafftask.assign_task(2, {"id": 20}, task["id"], 24, now=NOW + 10)
        notes = [e["note"] for e in stafftask.get_task(2, {"id": 20}, task["id"])["events"]]
        self.assertIn("改派给 xiaozhang", notes)       # 没姓名回退登录名

        members = {m["id"]: m for b in stafftask.dispatch_options(2, {"id": 20})["branches"]
                   for m in b["members"]}
        self.assertEqual(("王店长", "laowang"), (members[22]["name"], members[22]["username"]))
        self.assertEqual(("xiaozhang", "xiaozhang"),
                         (members[24]["name"], members[24]["username"]))

        todo = stafftask.todo_for_user(2, 23, now=NOW)
        self.assertEqual(("小李", "xiaoli", "店员"),
                         (todo["user"]["name"], todo["user"]["username"],
                          todo["user"]["role_label"]))
        self.assertEqual("9 月 21 日 周一", todo["date_text"])

    def test_one_liner_matches_member_by_display_name_or_login_name(self):
        members = stafftask.branch_members(2, {"id": self.branch, "industry_key": "restaurant"})
        self.assertEqual(23, stafftask.match_member("小李", members)[0]["id"])
        self.assertEqual(23, stafftask.match_member("xiaoli", members)[0]["id"])
        self.assertEqual(22, stafftask.match_member("店长", members)[0]["id"])

    def test_cockpit_people_and_inspection_assignees_use_display_names(self):
        accounts = cockpit._users(2)
        self.assertEqual(("小李", "xiaoli"), (accounts[23]["name"], accounts[23]["username"]))
        self.assertEqual(("xiaozhang", "xiaozhang"),
                         (accounts[24]["name"], accounts[24]["username"]))

        users = {u["id"]: u for u in inspection.assignable_users(2, self.branch, "restaurant")}
        self.assertEqual(("小李", "xiaoli", "小李（店员）"),
                         (users[23]["name"], users[23]["username"], users[23]["label"]))
        self.assertEqual("王店长", users[22]["label"])
        self.assertEqual("王总（老板）", users[20]["label"])
        self.assertEqual("xiaozhang（店员）", users[24]["label"])

        assignees = inspection._branch_assignees(2, [self.branch])[self.branch]
        self.assertEqual({"王店长", "小李", "xiaozhang"}, {a["name"] for a in assignees})

    def test_checklist_run_shows_who_ticked_by_display_name(self):
        day = timeutil.today_cn(NOW)
        # 门店是「现在」建的；清单只给截止时间之前就有的门店生成，这里把建店时间拨回去
        db.execute("UPDATE store_branch SET created_at=0 WHERE id=?", (self.branch,))
        template = checklist.create_template(2, 20, {
            "name": "周一大扫除", "kind": "custom", "due_time": "18:00",
            "items": [{"text": "开灯"}, {"text": "擦台面"}],
        }, now=NOW - 10)
        checklist.generate_runs(2, date=day, now=NOW)
        run = db.one("SELECT * FROM checklist_run WHERE tenant_id=2 AND template_id=?",
                     (template["id"],))
        key = db.jloads(run["items_json"], [])[0]["key"]
        checklist.complete_item(2, 23, run["id"], key, now=NOW + 5, asset_root=self.root)
        overview = checklist.runs_overview(2, 20, day, now=NOW + 10)
        store = overview["stores"][0]
        store["runs"] = [r for r in store["runs"] if r["template_id"] == template["id"]]
        done = [i for r in store["runs"] for i in r["items"] if i["done"]]
        self.assertEqual(["小李"], [i["done_by_name"] for i in done])
        # 顶部统计按「项」给数，和各店的 x/y 项同一个单位
        summary = overview["summary"]
        self.assertEqual(
            (store["items_total"], store["items_done"], store["items_missed"]),
            (summary["items_total"], summary["items_done"], summary["items_missed"]))
        self.assertEqual(1, summary["items_done"])
        self.assertGreaterEqual(summary["items_total"], 2)
        self.assertEqual(0, summary["items_missed"])
        # 过了点没做完：超时没做的「项」数 = 那些清单里没打勾的项
        checklist.mark_missed(2, now=NOW + 3 * 86400)
        late = checklist.runs_overview(2, 20, day, now=NOW + 3 * 86400)["summary"]
        self.assertEqual(late["items_total"] - late["items_done"], late["items_missed"])
        self.assertGreaterEqual(late["missed"], 1)


class TodayTextTests(unittest.TestCase):
    def test_today_text_is_beijing_date_with_weekday(self):
        # 2026-11-23 16:30 UTC = 北京时间 11 月 24 日 00:30 周二
        ts = datetime(2026, 11, 23, 16, 30, tzinfo=timeutil.timezone.utc).timestamp()
        self.assertEqual("11 月 24 日 周二", timeutil.today_text_cn(ts))
        self.assertEqual("2026-11-24", timeutil.today_cn(ts))
        sunday = datetime(2026, 11, 29, 12, 0, tzinfo=timeutil.CN_TZ).timestamp()
        self.assertEqual("11 月 29 日 周日", timeutil.today_text_cn(sunday))


if __name__ == "__main__":
    unittest.main()
