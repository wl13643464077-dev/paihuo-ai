"""巡店门店-成员绑定（schema v58）与巡店统计口径修复的行为测试。

全部走临时 SQLite + 真实服务层函数，不依赖 fastapi。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from app import bossdashboard, db, inspection


REPO_ROOT = Path(__file__).resolve().parents[1]


def _photo(key: str, digest: str) -> dict:
    return {
        "storage_key": key,
        "mime_type": "image/jpeg",
        "byte_size": 120_000,
        "sha256": digest,
        "width": 1200,
        "height": 900,
    }


def _analysis(photos: list[dict], *, issue: bool = True, due_days=3) -> dict:
    before = [item for item in photos if item.get("phase", "before") == "before"]
    pid = int(before[0]["id"])
    result = {
        "summary": "巡店结论",
        "score": 70 if issue else 100,
        "photo_reviews": [{
            "photo_id": int(item["id"]),
            "analyzable": True,
            "verdict": "issue" if issue and int(item["id"]) == pid else "clean",
            "confidence": 0.95,
            "visible_facts": ["画面主体、通道与物品状态清晰可见"],
        } for item in before],
        "issues": [],
    }
    if issue:
        result["analysis_status"] = "issues_found"
        result["issues"] = [{
            "title": "通道堆放纸箱",
            "description": "消防通道可见纸箱",
            "severity": "high",
            "category": "safety",
            "confidence": 0.9,
            "evidence": [{"photo_id": pid}],
            "action": {"plan": "清走纸箱并拍照", "owner": "店长", "due_days": due_days},
        }]
    else:
        result["analysis_status"] = "clean_verified"
        result["verification"] = {
            "primary_model": "gpt-5.5",
            "review_model": "claude-opus-4-8",
            "both_clean": True,
        }
    return result


class _ScopeDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        self._reset_connections()
        db.DB_PATH = os.path.join(self.tmp.name, "store-scope.db")
        db.conn()
        for tenant in (
            {"id": 2, "name": "连锁餐饮", "industries_json": "[]"},
            {"id": 3, "name": "别家企业", "industries_json": "[]"},
        ):
            db.insert("tenants", tenant)
        for tenant_id, industry_key in ((2, "restaurant"), (3, "restaurant")):
            db.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                "VALUES(?,?,1,0)",
                (tenant_id, industry_key),
            )
        for uid, tid, role, title, modules in (
            (20, 2, "owner", "staff", "[]"),
            (21, 2, "member", "director", '["restaurant"]'),
            (22, 2, "member", "manager", '["restaurant"]'),
            (23, 2, "member", "staff", '["restaurant"]'),
            (24, 2, "member", "staff", "[]"),
            (30, 3, "owner", "staff", "[]"),
        ):
            db.insert("users", {
                "id": uid, "tenant_id": tid, "username": f"u{uid}",
                "password_hash": "x", "role": role, "job_title": title,
                "modules_json": modules, "enabled": 1,
            })
        self.a = inspection.create_branch(
            2, 20, "restaurant", {"name": "朝阳店", "region": "华北"}
        )
        self.b = inspection.create_branch(
            2, 20, "restaurant", {"name": "静安店", "region": "华东"}
        )
        self.c = inspection.create_branch(
            2, 20, "restaurant", {"name": "新开店", "region": ""}
        )
        self.foreign = inspection.create_branch(
            3, 30, "restaurant", {"name": "别家店", "region": "华北"}
        )
        # 经理 22 只负责朝阳店；员工 23 还没分配任何门店。
        inspection.set_member_branches(20, 22, [self.a["id"]])
        self._seq = 0

    def tearDown(self):
        self._reset_connections()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    @staticmethod
    def _reset_connections():
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

    def _visit(self, branch: dict, *, issue: bool = True, due_days=3,
               task_id: int | None = None) -> dict:
        self._seq += 1
        digest = f"{self._seq:x}".rjust(64, "0")
        draft = inspection.create_visit_draft(
            2, 20, "restaurant", branch["id"],
            {"request_key": f"scope-visit-{self._seq:04d}"},
            [_photo(f"inspections/2/scope{self._seq}/front.jpg", digest)],
            task_id=task_id,
        )
        return inspection.complete_visit(
            2, 20, "restaurant", draft["id"],
            _analysis(draft["photos"], issue=issue, due_days=due_days),
        )

    @staticmethod
    def _make_overdue():
        past = time.time() - 3 * 86400
        db.execute("UPDATE inspection_action SET due_at=? WHERE tenant_id=2", (past,))
        db.execute("UPDATE inspection_issue SET due_at=? WHERE tenant_id=2", (past,))


class BranchScopeTests(_ScopeDbCase):
    def test_owner_and_director_see_all_manager_only_bound_staff_empty(self):
        all_ids = {self.a["id"], self.b["id"], self.c["id"]}
        for uid in (20, 21):
            self.assertEqual(
                all_ids,
                {row["id"] for row in inspection.list_branches(2, uid, "restaurant")},
            )
            found = inspection.search_branches(2, uid, "restaurant", limit=50)
            self.assertEqual(all_ids, {row["id"] for row in found["items"]})
        self.assertEqual(
            [self.a["id"]],
            [row["id"] for row in inspection.list_branches(2, 22, "restaurant")],
        )
        self.assertEqual(
            [self.a["id"]],
            [row["id"] for row in inspection.search_branches(
                2, 22, "restaurant", limit=50)["items"]],
        )
        self.assertEqual([], inspection.list_branches(2, 23, "restaurant"))
        self.assertEqual(
            [], inspection.search_branches(2, 23, "restaurant")["items"]
        )

        # 老板在门店列表里能直接看到负责人；经理看不到别人的分配。
        owner_rows = {
            row["id"]: row
            for row in inspection.search_branches(2, 20, "restaurant", limit=50)["items"]
        }
        self.assertEqual(
            ["u22"], [a["username"] for a in owner_rows[self.a["id"]]["assignees"]]
        )
        self.assertEqual([], owner_rows[self.b["id"]]["assignees"])
        manager_row = inspection.search_branches(2, 22, "restaurant")["items"][0]
        self.assertNotIn("assignees", manager_row)

        staff = inspection.branch_scope_info(2, 23, "restaurant")
        self.assertEqual(0, staff["assigned_branches"])
        self.assertEqual("老板还没给你分配门店，请联系老板", staff["notice"])
        self.assertFalse(staff["can_manage_branches"])
        manager = inspection.branch_scope_info(2, 22, "restaurant")
        self.assertEqual((1, ""), (manager["assigned_branches"], manager["notice"]))
        owner = inspection.branch_scope_info(2, 20, "restaurant")
        self.assertTrue(owner["all_branches"])
        self.assertTrue(owner["can_assign_actions"])
        director = inspection.branch_scope_info(2, 21, "restaurant")
        self.assertTrue(director["all_branches"])
        self.assertTrue(director["can_review"])
        self.assertFalse(director["can_assign_actions"])

    def test_history_detail_photos_and_task_follow_binding(self):
        task_id = db.insert("task", {
            "tenant_id": 2, "emp_idx": inspection.EMPLOYEE_IDX,
            "brief_json": "{}", "status": "done", "billing_status": "included",
            "billing_points": 1, "created_by": 20,
        })
        va = self._visit(self.a)
        vb = self._visit(self.b, task_id=task_id)

        self.assertEqual(
            [va["id"]],
            [row["id"] for row in inspection.list_visits(2, 22, "restaurant")["items"]],
        )
        self.assertEqual([], inspection.list_visits(2, 23, "restaurant")["items"])
        self.assertEqual(
            {va["id"], vb["id"]},
            {row["id"] for row in inspection.list_visits(2, 21, "restaurant")["items"]},
        )
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.list_visits(2, 22, "restaurant", branch_id=self.b["id"])
        # 区域下钻也不能绕过门店绑定。
        self.assertEqual(
            [], inspection.list_visits(2, 22, "restaurant", region="华东")["items"]
        )

        self.assertEqual(va["id"], inspection.get_visit(2, 22, "restaurant", va["id"])["id"])
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.get_visit(2, 22, "restaurant", vb["id"])
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.get_visit(2, 23, "restaurant", va["id"])

        self.assertTrue(inspection.visit_files_visible(2, 22, va["id"]))
        self.assertFalse(inspection.visit_files_visible(2, 22, vb["id"]))
        self.assertFalse(inspection.visit_files_visible(2, 23, va["id"]))
        self.assertTrue(inspection.visit_files_visible(2, 21, vb["id"]))
        self.assertTrue(inspection.visit_files_visible(2, 20, vb["id"]))

        self.assertEqual(vb["id"], inspection.task_scope(2, 20, task_id)["visit_id"])
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.task_scope(2, 22, task_id)

    def test_aggregate_and_risk_ranking_only_count_bound_branches(self):
        self._visit(self.a)
        self._visit(self.b)
        owner = inspection.aggregate(2, 20, "restaurant")
        self.assertEqual(2, owner["visits"])
        self.assertEqual(3, len(owner["branches"]))
        manager = inspection.aggregate(2, 22, "restaurant")
        self.assertEqual(1, manager["visits"])
        self.assertEqual(1, manager["open_issues"])
        self.assertEqual([self.a["id"]], [row["id"] for row in manager["branches"]])
        self.assertEqual(["华北"], [row["region"] for row in manager["regions"]])
        bounded = inspection.aggregate(
            2, 22, "restaurant", branch_limit=20, region_limit=50,
        )
        self.assertEqual(1, bounded["total_branches"])
        self.assertEqual([self.a["id"]], [row["id"] for row in bounded["branches"]])
        staff = inspection.aggregate(2, 23, "restaurant", branch_limit=20, region_limit=50)
        self.assertEqual((0, 0, [], []), (
            staff["visits"], staff["total_branches"], staff["branches"], staff["regions"],
        ))
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.aggregate(2, 22, "restaurant", pinned_branch_id=self.b["id"])

    def test_staff_can_only_progress_actions_of_bound_branch(self):
        va = self._visit(self.a)
        vb = self._visit(self.b)
        action_a = va["issues"][0]["action"]
        action_b = vb["issues"][0]["action"]
        moved = inspection.transition_action(
            2, 22, "restaurant", action_a["id"],
            expected_version=action_a["version"], target_status="in_progress",
        )
        self.assertEqual("in_progress", moved["status"])
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.transition_action(
                2, 22, "restaurant", action_b["id"],
                expected_version=action_b["version"], target_status="in_progress",
            )
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.transition_action(
                2, 23, "restaurant", action_a["id"],
                expected_version=moved["version"], target_status="awaiting_recheck",
            )
        # 整改责任人/期限仍只由老板确认。
        with self.assertRaises(inspection.InspectionForbidden):
            inspection.update_action_assignment(
                2, 21, "restaurant", action_a["id"],
                expected_version=moved["version"], owner="新店长",
                due_at=time.time() + 86400,
            )
        # 员工也不能给别人门店的整改单上传复查照片。
        db.execute(
            "UPDATE inspection_action SET status='awaiting_recheck' WHERE id=?",
            (action_b["id"],),
        )
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.add_recheck_photos(
                2, 22, "restaurant", action_b["id"],
                [_photo("inspections/2/recheck-b/after.jpg", "e" * 64)],
            )

    def test_only_owner_or_director_create_disable_branch_and_review(self):
        for uid in (22, 23):
            with self.assertRaises(inspection.InspectionForbidden):
                inspection.create_branch(2, uid, "restaurant", {"name": f"自建{uid}"})
            with self.assertRaises(inspection.InspectionForbidden):
                inspection.set_branch_active(
                    2, uid, "restaurant", self.a["id"], active=False,
                )
        created = inspection.create_branch(2, 21, "restaurant", {"name": "总监建的店"})
        self.assertEqual(21, created["created_by"])
        disabled = inspection.set_branch_active(
            2, 21, "restaurant", created["id"], active=False,
        )
        self.assertFalse(disabled["active"])
        self.assertNotIn(
            created["id"],
            {row["id"] for row in inspection.list_branches(2, 20, "restaurant")},
        )
        restored = inspection.set_branch_active(
            2, 20, "restaurant", created["id"], active=True,
        )
        self.assertTrue(restored["active"])
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.set_branch_active(
                3, 30, "restaurant", self.a["id"], active=False,
            )

        va = self._visit(self.a)
        action = va["issues"][0]["action"]
        submitted = inspection.transition_action(
            2, 22, "restaurant", action["id"],
            expected_version=action["version"], target_status="awaiting_recheck",
        )
        photos = inspection.add_recheck_photos(
            2, 22, "restaurant", action["id"],
            [_photo("inspections/2/recheck-a/after.jpg", "f" * 64)],
        )
        recheck = inspection.record_recheck(
            2, 22, "restaurant", action["id"],
            {"recommendation": "close", "confidence": 0.9, "note": "已清走",
             "evidence_photo_ids": [photos[0]["id"]]},
        )
        with self.assertRaises(inspection.InspectionForbidden):
            inspection.review_recheck(
                2, 22, "restaurant", recheck["id"], decision="close",
                expected_action_version=submitted["version"], note="经理自己关单",
            )
        reviewed = inspection.review_recheck(
            2, 21, "restaurant", recheck["id"], decision="close",
            expected_action_version=submitted["version"], note="总监确认已整改",
        )
        self.assertEqual("closed", reviewed["action"]["status"])
        self.assertEqual(21, reviewed["action"]["closed_by"])

    def test_owner_assigns_member_branches_with_replace_semantics(self):
        result = inspection.set_member_branches(
            20, 23, [self.b["id"], self.c["id"], self.b["id"]],
        )
        self.assertEqual(sorted([self.b["id"], self.c["id"]]), result["branch_ids"])
        self.assertEqual(
            {22: [self.a["id"]], 23: sorted([self.b["id"], self.c["id"]])},
            inspection.member_branch_assignments(2),
        )
        inspection.set_member_branches(20, 23, [self.c["id"]])
        self.assertEqual(
            [self.c["id"]],
            [row["id"] for row in inspection.list_branches(2, 23, "restaurant")],
        )
        inspection.set_member_branches(20, 23, [])
        self.assertEqual([], inspection.list_branches(2, 23, "restaurant"))

        with self.assertRaises(inspection.InspectionForbidden):
            inspection.set_member_branches(21, 23, [self.a["id"]])
        with self.assertRaises(inspection.InspectionForbidden):
            inspection.set_member_branches(22, 23, [self.a["id"]])
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.set_member_branches(30, 23, [self.a["id"]])
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.set_member_branches(20, 23, [self.foreign["id"]])
        with self.assertRaises(inspection.InspectionError):
            inspection.set_member_branches(20, 20, [self.a["id"]])
        with self.assertRaises(inspection.InspectionError):
            inspection.set_member_branches(20, 24, [self.a["id"]])
        with self.assertRaises(inspection.InspectionError):
            inspection.set_member_branches(20, 23, "1,2")
        inspection.set_branch_active(2, 20, "restaurant", self.b["id"], active=False)
        with self.assertRaises(inspection.InspectionConflict):
            inspection.set_member_branches(20, 23, [self.b["id"]])
        # 失败的整体替换不能留下半截绑定。
        self.assertEqual({22: [self.a["id"]]}, inspection.member_branch_assignments(2))

        catalog = inspection.assignable_branches(2)
        self.assertEqual(
            {self.a["id"], self.c["id"]}, {row["id"] for row in catalog["items"]}
        )
        self.assertFalse(catalog["truncated"])


class InspectionMetricFixTests(_ScopeDbCase):
    def test_disabled_branch_leaves_kpis_everywhere(self):
        self._visit(self.a)
        self._visit(self.b)
        self._make_overdue()
        now = time.time()
        before = inspection.aggregate(2, 20, "restaurant")
        self.assertEqual((2, 2, 2), (
            before["visits"], before["open_issues"], before["overdue_actions"],
        ))
        boss_before, _, _ = bossdashboard._inspection_aggregate(
            2, "restaurant", now - 30 * 86400, now,
        )
        self.assertEqual(2, boss_before["backlog"]["overdue_actions"])

        inspection.set_branch_active(2, 20, "restaurant", self.b["id"], active=False)
        after = inspection.aggregate(2, 20, "restaurant")
        self.assertEqual((1, 1, 1, 1), (
            after["visits"], after["open_issues"], after["overdue_actions"],
            after["total_actions"],
        ))
        bounded = inspection.aggregate(
            2, 20, "restaurant", branch_limit=20, region_limit=50,
        )
        self.assertEqual(1, bounded["overdue_actions"])
        self.assertNotIn(self.b["id"], {row["id"] for row in bounded["branches"]})
        boss_after, _, _ = bossdashboard._inspection_aggregate(
            2, "restaurant", now - 30 * 86400, now,
        )
        self.assertEqual(1, boss_after["open_issues"])
        self.assertEqual(1, boss_after["overdue_actions"])
        self.assertEqual(1, boss_after["backlog"]["open_actions"])
        self.assertEqual(1, boss_after["backlog"]["overdue_issues"])
        # 历史巡店记录仍可查看。
        self.assertEqual(2, len(inspection.list_visits(2, 20, "restaurant")["items"]))

    def test_awaiting_owner_review_is_not_overdue(self):
        va = self._visit(self.a)
        self._make_overdue()
        now = time.time()
        self.assertEqual(1, inspection.aggregate(2, 20, "restaurant")["overdue_actions"])
        action = va["issues"][0]["action"]
        current = db.one(
            "SELECT version FROM inspection_action WHERE id=?", (action["id"],)
        )["version"]
        inspection.transition_action(
            2, 22, "restaurant", action["id"],
            expected_version=current, target_status="awaiting_recheck",
        )
        summary = inspection.aggregate(2, 20, "restaurant")
        self.assertEqual(0, summary["overdue_actions"])
        self.assertEqual(1, summary["open_issues"])
        bounded = inspection.aggregate(
            2, 20, "restaurant", branch_limit=20, region_limit=50,
        )
        self.assertEqual(
            [0], [row["overdue_actions"] for row in bounded["regions"]
                  if row["region"] == "华北"],
        )
        self.assertEqual(
            0, inspection.list_visits(2, 20, "restaurant")["items"][0]["overdue_count"],
        )
        boss, _, _ = bossdashboard._inspection_aggregate(
            2, "restaurant", now - 30 * 86400, now,
        )
        self.assertEqual((0, 0), (boss["overdue_actions"], boss["overdue_issues"]))
        self.assertEqual(
            (0, 0),
            (boss["backlog"]["overdue_actions"], boss["backlog"]["overdue_issues"]),
        )
        self.assertEqual(1, boss["backlog"]["open_actions"])

    def test_unassigned_region_drilldown_matches_summary_label(self):
        vc = self._visit(self.c, issue=False)
        self._visit(self.a, issue=False)
        regions = inspection.aggregate(
            2, 20, "restaurant", branch_limit=20, region_limit=50,
        )["regions"]
        self.assertIn("未分区", [row["region"] for row in regions])
        for label in ("未分区", ""):
            self.assertEqual(
                [vc["id"]],
                [row["id"] for row in inspection.list_visits(
                    2, 20, "restaurant", region=label)["items"]],
            )

    def test_due_days_is_at_least_one_day(self):
        started = time.time()
        visit = self._visit(self.a, due_days=0)
        action = visit["issues"][0]["action"]
        self.assertGreaterEqual(action["due_at"], started + 86400 - 5)
        self.assertEqual(0, inspection.aggregate(2, 20, "restaurant")["overdue_actions"])


class SchemaV58MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        self._reset()
        db.DB_PATH = os.path.join(self.tmp.name, "migrate.db")

    def tearDown(self):
        self._reset()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    @staticmethod
    def _reset():
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None

    def _raw(self, *statements):
        self._reset()
        connection = sqlite3.connect(db.DB_PATH)
        try:
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        finally:
            connection.close()

    def test_fresh_database_is_v58_with_user_branch_contract(self):
        db.conn()
        self.assertEqual(58, db.LATEST_SCHEMA_VERSION)
        self.assertEqual(58, db.one("PRAGMA user_version")["user_version"])
        self.assertEqual(
            "member-branch-scope",
            db.one("SELECT name FROM schema_version WHERE version=58")["name"],
        )
        columns = {row["name"] for row in db.q("PRAGMA table_info(user_branch)")}
        self.assertEqual(
            {"tenant_id", "user_id", "branch_id", "created_by", "created_at"}, columns,
        )
        db.execute(
            "INSERT INTO user_branch(tenant_id,user_id,branch_id,created_at) "
            "VALUES(2,5,7,0)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO user_branch(tenant_id,user_id,branch_id,created_at) "
                "VALUES(2,5,7,1)"
            )

    def test_v57_database_upgrades_and_drops_orphan_bindings(self):
        db.conn()
        db.insert("tenants", {"id": 2, "name": "企业", "industries_json": "[]"})
        db.insert("users", {
            "id": 22, "tenant_id": 2, "username": "u22", "password_hash": "x",
            "role": "member", "modules_json": "[]", "enabled": 1,
        })
        branch_id = db.insert("store_branch", {
            "tenant_id": 2, "industry_key": "restaurant", "name": "店",
            "created_at": 0, "updated_at": 0,
        })
        # 模拟 v57 旧库：没有 user_branch，账本/user_version 停在 57。
        self._raw(
            "DROP TABLE user_branch",
            "DELETE FROM schema_version WHERE version=58",
            "PRAGMA user_version=57",
        )
        db.conn()
        self.assertEqual(58, db.one("PRAGMA user_version")["user_version"])
        self.assertEqual(
            0, db.one("SELECT COUNT(*) n FROM user_branch")["n"],
        )
        # 已有表但带悬空绑定的库，升级时清理掉悬空行、保留有效绑定。
        self._raw(
            f"INSERT INTO user_branch VALUES(2,22,{branch_id},20,0)",
            "INSERT INTO user_branch VALUES(2,999,1,20,0)",
            f"INSERT INTO user_branch VALUES(2,22,{branch_id + 100},20,0)",
            "DELETE FROM schema_version WHERE version=58",
            "PRAGMA user_version=57",
        )
        db.conn()
        self.assertEqual(
            [(22, branch_id)],
            [(row["user_id"], row["branch_id"]) for row in db.q(
                "SELECT user_id,branch_id FROM user_branch"
            )],
        )
        self.assertEqual(
            1, db.one("SELECT COUNT(*) n FROM schema_version WHERE version=58")["n"],
        )

    def test_startup_rejects_user_branch_without_unique_key(self):
        db.conn()
        self._raw(
            "DROP TABLE user_branch",
            "CREATE TABLE user_branch(tenant_id INTEGER NOT NULL,user_id INTEGER NOT NULL,"
            "branch_id INTEGER NOT NULL,created_by INTEGER,created_at REAL NOT NULL)",
            "CREATE INDEX idx_user_branch_branch ON user_branch(tenant_id,branch_id)",
        )
        with self.assertRaises(RuntimeError) as caught:
            db.conn()
        self.assertIn("user_branch", str(caught.exception))


@unittest.skipUnless(shutil.which("node"), "需要 node 执行前端函数")
class InspectionLocalDateTests(unittest.TestCase):
    def test_default_visit_date_uses_beijing_local_day(self):
        source = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
        start = source.index("function inspectionLocalDate(")
        end = source.index("\n", start)
        script = (
            source[start:end]
            + "\nconst early=new Date(2026,8,25,3,15);"
            + "console.log(inspectionLocalDate(early)+'|'+early.toISOString().slice(0,10)"
            + "+'|'+inspectionLocalDate(new Date(2026,0,5,23,59)));"
        )
        output = subprocess.run(
            ["node", "-e", script],
            env={**os.environ, "TZ": "Asia/Shanghai"},
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        local_day, utc_day, late = output.split("|")
        self.assertEqual("2026-09-25", local_day)
        # 旧写法 toISOString 在北京时间凌晨会得到前一天。
        self.assertEqual("2026-09-24", utc_day)
        self.assertEqual("2026-01-05", late)


if __name__ == "__main__":
    unittest.main()


class TaskCenterBranchScopeTests(_ScopeDbCase):
    """任务中心里的巡店任务与巡店页同一口径：经理/员工只看自己门店的。"""

    def _inspection_task(self, branch: dict) -> int:
        task_id = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": inspection.EMPLOYEE_IDX,
            "brief_json": '{"direction": "巡店"}',
            "status": "done",
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        self._visit(branch, task_id=task_id)
        return task_id

    def _ids(self, uid: int) -> set[int]:
        from app import taskcenter
        viewer = db.one("SELECT * FROM users WHERE id=?", (uid,))
        result = taskcenter.list_items(2, {"restaurant"}, viewer=viewer)
        return {
            int(item.get("record_id") or item.get("id") or 0)
            for item in result["items"]
        }

    def test_manager_only_sees_bound_branch_inspection_tasks(self):
        task_a = self._inspection_task(self.a)
        task_b = self._inspection_task(self.b)
        self.assertTrue({task_a, task_b} <= self._ids(20))   # 老板
        self.assertTrue({task_a, task_b} <= self._ids(21))   # 总监
        manager_ids = self._ids(22)
        self.assertIn(task_a, manager_ids)
        self.assertNotIn(task_b, manager_ids)
        self.assertFalse({task_a, task_b} & self._ids(23))   # 未分配门店的员工
