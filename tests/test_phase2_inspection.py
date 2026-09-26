"""第 2 期巡店改进的行为测试：整改派到人、误报作废、单张补拍、
门店级标准版本、确定性评分与回填、照片按内容校验。

全部走临时 SQLite + 真实服务层函数，不依赖 fastapi。
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest

from app import db, inspection, inspectionoverrides, inspectionstandards, notify


def _photo(key: str, digest: str, **extra) -> dict:
    return {
        "storage_key": key,
        "mime_type": "image/jpeg",
        "byte_size": 120_000,
        "sha256": digest,
        "width": 1200,
        "height": 900,
        **extra,
    }


def _review(photo_id: int, *, verdict="clean", confidence=0.95, analyzable=True):
    return {
        "photo_id": int(photo_id),
        "analyzable": analyzable,
        "verdict": verdict,
        "confidence": confidence,
        "visible_facts": ["画面主体、通道与物品状态清晰可见"],
    }


def _issue(photo_id: int, *, title="通道堆放纸箱", severity="high", category="safety"):
    return {
        "title": title,
        "description": f"{title}，照片可见",
        "severity": severity,
        "category": category,
        "confidence": 0.9,
        "evidence": [{"photo_id": int(photo_id)}],
        "action": {"plan": "清理并拍照", "owner": "店长", "due_days": 3},
    }


def _verified(result: dict) -> dict:
    if result["issues"]:
        result["analysis_status"] = "issues_found"
    else:
        result["analysis_status"] = "clean_verified"
        result["verification"] = {
            "primary_model": "gpt-5.5",
            "review_model": "claude-opus-4-8",
            "both_clean": True,
        }
    return result


class _Phase2Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        self._reset_connections()
        db.DB_PATH = os.path.join(self.tmp.name, "phase2-inspection.db")
        db.conn()
        for tenant in (
            {"id": 2, "name": "连锁餐饮", "industries_json": "[]"},
            {"id": 3, "name": "别家企业", "industries_json": "[]"},
        ):
            db.insert("tenants", tenant)
        for tenant_id in (2, 3):
            db.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
                "VALUES(?,'restaurant',1,0)",
                (tenant_id,),
            )
        for uid, tid, role, title, modules in (
            (20, 2, "owner", "staff", "[]"),
            (21, 2, "member", "director", '["restaurant"]'),
            (22, 2, "member", "manager", '["restaurant"]'),   # 朝阳店店长
            (23, 2, "member", "staff", '["restaurant"]'),     # 朝阳店店员
            (25, 2, "member", "staff", '["restaurant"]'),     # 静安店店员
            (26, 2, "member", "manager", '["restaurant"]'),   # 静安店店长
            (27, 2, "member", "manager", '["restaurant"]'),   # 静安店第二个店长
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
        inspection.set_member_branches(20, 22, [self.a["id"]])
        inspection.set_member_branches(20, 23, [self.a["id"]])
        inspection.set_member_branches(20, 25, [self.b["id"]])
        inspection.set_member_branches(20, 26, [self.b["id"]])
        inspection.set_member_branches(20, 27, [self.b["id"]])
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

    def _draft(self, branch: dict, photos: int = 1, *, task_id=None) -> dict:
        self._seq += 1
        return inspection.create_visit_draft(
            2, 20, "restaurant", branch["id"],
            {"request_key": f"phase2-visit-{self._seq:04d}"},
            [
                _photo(
                    f"inspections/2/p2v{self._seq}/{index}.jpg",
                    f"{self._seq:x}{index:x}".rjust(64, "0"),
                )
                for index in range(photos)
            ],
            task_id=task_id,
        )

    def _visit_with_issues(self, branch: dict, issues_spec=(("high", "safety"),)) -> dict:
        draft = self._draft(branch, photos=len(issues_spec) or 1)
        ids = [int(p["id"]) for p in draft["photos"]]
        issues = [
            _issue(ids[index], title=f"问题{index}", severity=sev, category=cat)
            for index, (sev, cat) in enumerate(issues_spec)
        ]
        result = _verified({
            "summary": "巡店结论",
            "score": 42,
            "photo_reviews": [
                _review(pid, verdict="issue" if index < len(issues) else "clean")
                for index, pid in enumerate(ids)
            ],
            "issues": issues,
        })
        return inspection.complete_visit(2, 20, "restaurant", draft["id"], result)

    @staticmethod
    def _user(uid: int) -> dict:
        row = db.one(
            "SELECT id,tenant_id,role,job_title,modules_json,enabled FROM users WHERE id=?",
            (uid,),
        )
        row["modules"] = json.loads(row["modules_json"])
        return row


class AssignActionTests(_Phase2Case):
    def test_single_store_manager_is_default_assignee_and_notified(self):
        visit = self._visit_with_issues(self.a)
        action = visit["issues"][0]["action"]
        self.assertEqual(22, action["assignee_user_id"])
        self.assertEqual("u22", action["owner"])
        self.assertEqual("u22", visit["issues"][0]["owner"])
        rows = db.q(
            "SELECT user_id,kind FROM notification WHERE tenant_id=2 AND kind=?",
            (inspection.ASSIGNED_NOTICE_KIND,),
        )
        self.assertEqual([{"user_id": 22, "kind": inspection.ASSIGNED_NOTICE_KIND}], rows)
        # 静安店有两个店长：不猜，留给老板指派。
        other = self._visit_with_issues(self.b)
        self.assertIsNone(other["issues"][0]["action"]["assignee_user_id"])
        self.assertEqual("店长", other["issues"][0]["action"]["owner"])

    def test_permissions_candidates_audit_and_per_user_notification(self):
        visit = self._visit_with_issues(self.a)
        action_id = visit["issues"][0]["action"]["id"]
        db.execute("DELETE FROM notification")

        # 店员不能指派；别的门店的店长连这条整改都看不到。
        with self.assertRaises(inspection.InspectionForbidden):
            inspection.assign_action(2, 23, action_id, 23)
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.assign_action(2, 26, action_id, 23)
        # 别家企业的老板查不到。
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.assign_action(3, 30, action_id, 30)
        # 被指派人必须绑定该门店，或是老板/总监。
        with self.assertRaises(inspection.InspectionError):
            inspection.assign_action(2, 20, action_id, 25)
        with self.assertRaises(inspection.InspectionError):
            inspection.assign_action(2, 20, action_id, 30)

        # 本店店长可以指派给本店店员，并改期限。
        due_at = time.time() + 5 * 86400
        assigned = inspection.assign_action(2, 22, action_id, 23, due_at=due_at)
        self.assertEqual(23, assigned["assignee_user_id"])
        self.assertEqual("u23", assigned["owner"])
        self.assertAlmostEqual(due_at, assigned["due_at"], places=3)
        issue = db.one(
            "SELECT owner,due_at FROM inspection_issue WHERE id=?",
            (visit["issues"][0]["id"],),
        )
        self.assertEqual("u23", issue["owner"])
        event = db.one(
            "SELECT payload_json,created_by FROM inspection_event WHERE action_id=? "
            "AND kind='action_assigned' ORDER BY id DESC LIMIT 1",
            (action_id,),
        )
        self.assertEqual(22, event["created_by"])
        self.assertEqual(23, json.loads(event["payload_json"])["to_user_id"])

        rows = db.q(
            "SELECT id,user_id,kind,title,body,link FROM notification WHERE tenant_id=2"
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(23, rows[0]["user_id"])
        self.assertIn("朝阳店", rows[0]["body"])
        self.assertTrue(rows[0]["link"].startswith(f"#/inspections/{visit['id']}"))
        staff = self._user(23)
        self.assertTrue(notify.can_view(staff, rows[0]))
        self.assertFalse(notify.can_view(self._user(22), rows[0]))
        self.assertEqual(
            [rows[0]["id"]],
            [item["id"] for item in notify.unread_for_user(2, staff)],
        )
        # 逐人通知没有收件人时绝不广播。
        self.assertIsNone(notify.record(2, inspection.ASSIGNED_NOTICE_KIND, {"title": "x"}))
        self.assertFalse(notify.can_view(
            staff, {"kind": inspection.ASSIGNED_NOTICE_KIND, "user_id": None},
        ))

        # 总监和老板都可以接整改；老板指派给总监。
        again = inspection.assign_action(2, 20, action_id, 21)
        self.assertEqual(21, again["assignee_user_id"])
        detail = inspection.get_visit(2, 20, "restaurant", visit["id"])
        self.assertTrue(detail["can_assign"])
        self.assertEqual(
            {20, 21, 22, 23},
            {item["id"] for item in detail["assignable_users"]},
        )
        self.assertFalse(inspection.get_visit(2, 23, "restaurant", visit["id"])["can_assign"])

    def test_actions_for_assignee_contract_and_scope(self):
        visit = self._visit_with_issues(self.a, (("high", "safety"), ("low", "hygiene")))
        first, second = [issue["action"] for issue in visit["issues"]]
        inspection.assign_action(2, 20, first["id"], 23)
        inspection.assign_action(2, 20, second["id"], 23)
        db.execute(
            "UPDATE inspection_action SET due_at=? WHERE id=?",
            (time.time() - 86400, first["id"]),
        )
        items = inspection.actions_for_assignee(2, 23)
        self.assertEqual([first["id"], second["id"]], [item["id"] for item in items])
        top = items[0]
        for key in (
            "id", "visit_id", "branch_id", "branch_name", "title", "plan",
            "due_at", "status", "next_step", "button", "hint",
        ):
            self.assertIn(key, top)
        self.assertEqual("inspection_action", top["kind"])
        self.assertEqual("朝阳店", top["branch_name"])
        self.assertTrue(top["overdue"])
        self.assertEqual("start", top["next_step"])
        self.assertEqual("开始整改", top["button"])

        current = inspection.transition_action(
            2, 23, "restaurant", first["id"],
            expected_version=top["version"], target_status="in_progress",
        )
        self.assertEqual("in_progress", current["status"])
        self.assertEqual(
            "upload_recheck", inspection.actions_for_assignee(2, 23)[0]["next_step"]
        )
        # 误报作废后不再出现在待办里。
        inspection.dismiss_action(2, 20, second["id"], "照片里是新到的货")
        self.assertEqual(
            [first["id"]], [item["id"] for item in inspection.actions_for_assignee(2, 23)]
        )
        # 解绑门店后看不到；别家企业、没指派的人都是空。
        inspection.set_member_branches(20, 23, [self.b["id"]])
        self.assertEqual([], inspection.actions_for_assignee(2, 23))
        self.assertEqual([], inspection.actions_for_assignee(3, 23))
        self.assertEqual([], inspection.actions_for_assignee(2, 25))


class DismissTests(_Phase2Case):
    def test_false_positive_excluded_from_counts_overdue_and_score_then_reopen(self):
        visit = self._visit_with_issues(self.a, (("high", "safety"), ("medium", "hygiene")))
        self.assertEqual(100 - 15 - 8, visit["score"])
        self.assertEqual(42, visit["ai_reference_score"])
        high, medium = visit["issues"]
        db.execute(
            "UPDATE inspection_action SET due_at=? WHERE tenant_id=2",
            (time.time() - 86400,),
        )
        inspection.transition_action(
            2, 22, "restaurant", high["action"]["id"],
            expected_version=high["action"]["version"], target_status="in_progress",
        )
        before = inspection.aggregate(2, 20, "restaurant")
        self.assertEqual(2, before["open_issues"])
        self.assertEqual(2, before["overdue_actions"])
        self.assertEqual(2, before["total_actions"])

        with self.assertRaises(inspection.InspectionForbidden):
            inspection.dismiss_action(2, 22, high["action"]["id"], "店长觉得不是问题")
        with self.assertRaises(inspection.InspectionError):
            inspection.dismiss_action(2, 20, high["action"]["id"], "  ")

        dismissed = inspection.dismiss_action(
            2, 21, high["action"]["id"], "照片里是正在上架的新货"
        )
        self.assertEqual("closed", dismissed["status"])
        self.assertEqual("false_positive", dismissed["close_reason"])
        self.assertEqual("照片里是正在上架的新货", dismissed["dismiss_note"])
        self.assertEqual(92.0, dismissed["visit_score"])
        after = inspection.aggregate(2, 20, "restaurant")
        self.assertEqual(1, after["open_issues"])
        self.assertEqual(1, after["overdue_actions"])
        self.assertEqual(1, after["total_actions"])
        self.assertEqual(0, after["verified_actions"])
        self.assertEqual(1, after["dismissed_actions"])
        self.assertEqual(92.0, after["average_score"])
        listed = inspection.list_visits(2, 20, "restaurant")["items"][0]
        self.assertEqual(1, listed["issue_count"])
        self.assertEqual(92.0, listed["score"])
        with self.assertRaises(inspection.InspectionConflict):
            inspection.dismiss_action(2, 20, high["action"]["id"], "重复")
        # 作废后不能再推进或指派。
        with self.assertRaises(inspection.InspectionConflict):
            inspection.assign_action(2, 20, high["action"]["id"], 23)

        with self.assertRaises(inspection.InspectionForbidden):
            inspection.restore_dismissed_action(2, 23, high["action"]["id"])
        reopened = inspection.restore_dismissed_action(2, 20, high["action"]["id"])
        self.assertEqual("in_progress", reopened["status"])
        self.assertEqual("", reopened["close_reason"])
        self.assertEqual(77.0, reopened["visit_score"])
        issue = db.one("SELECT status FROM inspection_issue WHERE id=?", (high["id"],))
        self.assertEqual("rectifying", issue["status"])
        again = inspection.aggregate(2, 20, "restaurant")
        self.assertEqual(2, again["open_issues"])
        self.assertEqual(2, again["overdue_actions"])
        kinds = [
            row["kind"] for row in db.q(
                "SELECT kind FROM inspection_event WHERE action_id=? ORDER BY id",
                (high["action"]["id"],),
            )
        ]
        self.assertIn("action_dismissed", kinds)
        self.assertIn("action_dismiss_reverted", kinds)
        with self.assertRaises(inspection.InspectionConflict):
            inspection.restore_dismissed_action(2, 20, medium["action"]["id"])


class RetakeTests(_Phase2Case):
    def _task(self) -> int:
        # 首轮分析后 main 会把任务收口为 done/succeeded（见 _commit_inspection_retake_wait）。
        return int(db.insert("task", {
            "tenant_id": 2,
            "emp_idx": inspection.EMPLOYEE_IDX,
            "brief_json": "{}",
            "status": "done",
            "billing_status": "succeeded",
        }))

    def test_single_blurry_photo_keeps_other_results_and_resumes(self):
        task_id = self._task()
        draft = self._draft(self.b, photos=3, task_id=task_id)
        p1, p2, p3 = [int(p["id"]) for p in draft["photos"]]
        first = _verified({
            "summary": "前两张检查完，第二张看不清",
            "score": 70,
            "photo_reviews": [
                _review(p1, verdict="issue"),
                _review(p2, analyzable=False),
                _review(p3, confidence=0.55),
            ],
            "issues": [
                _issue(p1),
                # 证据只落在看不清的照片上：先不记。
                _issue(p2, title="模糊处疑似积水", severity="medium", category="hygiene"),
            ],
        })
        waiting = inspection.complete_visit(2, 20, "restaurant", draft["id"], first)
        self.assertEqual("needs_retake", waiting["status"])
        self.assertEqual([p2, p3], waiting["retake"]["pending"])
        self.assertEqual([], waiting["issues"])
        flags = {p["id"]: p["needs_retake"] for p in waiting["photos"]}
        self.assertEqual({p1: False, p2: True, p3: True}, flags)
        self.assertTrue(next(p for p in waiting["photos"] if p["id"] == p2)["retake_note"])
        self.assertEqual(p1, waiting["photo_reviews"][0]["photo_id"])
        self.assertEqual("issue", waiting["photo_reviews"][0]["verdict"])

        with self.assertRaises(inspection.InspectionConflict):
            inspection.retake_target(2, 20, "restaurant", draft["id"], p1)
        # 别的门店的店员不能补拍这家店。
        with self.assertRaises(inspection.InspectionNotFound):
            inspection.retake_target(2, 23, "restaurant", draft["id"], p2)

        old_key = db.one("SELECT storage_key FROM inspection_photo WHERE id=?", (p2,))
        step = inspection.replace_retake_photo(
            2, 25, "restaurant", draft["id"], p2,
            _photo(f"inspections/2/{draft['id']}/retake2.jpg", "e" * 64,
                   capture_slot="forged"),
        )
        self.assertFalse(step["ready"])
        self.assertEqual(1, step["remaining"])
        self.assertEqual(old_key["storage_key"], step["old_storage_key"])
        self.assertEqual("needs_retake", db.one(
            "SELECT status FROM inspection_visit WHERE id=?", (draft["id"],))["status"])
        with self.assertRaises(inspection.InspectionConflict):
            inspection.replace_retake_photo(
                2, 25, "restaurant", draft["id"], p2,
                _photo(f"inspections/2/{draft['id']}/again.jpg", "f" * 64),
            )
        done = inspection.replace_retake_photo(
            2, 25, "restaurant", draft["id"], p3,
            _photo(f"inspections/2/{draft['id']}/retake3.jpg", "d" * 64),
        )
        self.assertTrue(done["ready"])
        self.assertEqual(task_id, done["task_id"])
        photo = db.one(
            "SELECT id,sha256,capture_slot,created_by FROM inspection_photo WHERE id=?",
            (p2,),
        )
        self.assertEqual(("e" * 64, "", 25), (photo["sha256"], photo["capture_slot"],
                                               photo["created_by"]))
        self.assertEqual("analyzing", db.one(
            "SELECT status FROM inspection_visit WHERE id=?", (draft["id"],))["status"])
        # 原任务免费重新排队（首轮已扣过点）。
        self.assertEqual(
            {"status": "queued", "billing_status": "included"},
            db.one("SELECT status,billing_status FROM task WHERE id=?", (task_id,)),
        )
        detail = inspection.get_visit(2, 20, "restaurant", draft["id"])
        self.assertEqual({p2, p3}, inspection.analysis_photo_ids(detail))

        # 第二轮只看补拍的两张：一张有问题，另一张干净。
        second = _verified({
            "summary": "补拍后看清了",
            "score": 90,
            "photo_reviews": [_review(p2, verdict="issue"), _review(p3)],
            "issues": [_issue(p2, title="地面积水", severity="medium", category="hygiene")],
        })
        # 第二轮不能引用第一轮已看过的照片。
        with self.assertRaises(inspection.InspectionError):
            inspection.complete_visit(2, 20, "restaurant", draft["id"], {
                **second, "photo_reviews": second["photo_reviews"] + [_review(p1)],
            })
        completed = inspection.complete_visit(2, 20, "restaurant", draft["id"], second)
        self.assertEqual("completed", completed["status"])
        self.assertEqual(
            {"通道堆放纸箱", "地面积水"},
            {issue["title"] for issue in completed["issues"]},
        )
        self.assertEqual([p1, p2, p3], [r["photo_id"] for r in completed["photo_reviews"]])
        self.assertEqual(100 - 15 - 8, completed["score"])
        self.assertEqual(70, completed["ai_reference_score"])
        self.assertIsNone(completed["retake"])
        kinds = [row["kind"] for row in db.q(
            "SELECT kind FROM inspection_event WHERE visit_id=? ORDER BY id",
            (draft["id"],),
        )]
        self.assertEqual(1, kinds.count("retake_requested"))
        self.assertEqual(2, kinds.count("photo_retaken"))

    def test_retake_rounds_are_bounded(self):
        draft = self._draft(self.b, photos=2)
        p1, p2 = [int(p["id"]) for p in draft["photos"]]
        result = _verified({
            "summary": "第一张干净", "score": 100,
            "photo_reviews": [_review(p1), _review(p2, confidence=0.3)],
            "issues": [],
        })
        visit = inspection.complete_visit(2, 20, "restaurant", draft["id"], result)
        for round_no in range(2, inspection.MAX_RETAKE_ROUNDS + 2):
            self.assertEqual("needs_retake", visit["status"])
            inspection.replace_retake_photo(
                2, 20, "restaurant", draft["id"], p2,
                _photo(f"inspections/2/{draft['id']}/r{round_no}.jpg",
                       f"{round_no:x}".rjust(64, "a")),
            )
            visit = inspection.complete_visit(2, 20, "restaurant", draft["id"], _verified({
                "summary": "还是看不清", "score": 100,
                "photo_reviews": [_review(p2, analyzable=False)],
                "issues": [],
            }))
        self.assertEqual("completed", visit["status"])
        self.assertEqual([p2], visit["unresolved_photo_ids"])
        self.assertEqual(100, visit["score"])

    def test_union_retake_flags_marks_photo_either_model_could_not_read(self):
        primary = inspection.normalize_model_result(
            {"summary": "干净", "score": 98, "issues": [],
             "photo_reviews": [_review(11), _review(12)]},
            {11, 12}, allow_clean_candidate=True,
        )
        review = inspection.normalize_model_result(
            {"summary": "干净", "score": 95, "issues": [],
             "photo_reviews": [_review(11), _review(12, analyzable=False)]},
            {11, 12}, allow_clean_candidate=True,
        )
        merged = inspection.union_retake_flags(primary, (primary, review))
        self.assertEqual([12], merged["retake_photo_ids"])
        self.assertTrue(merged["photo_reviews"][1]["needs_retake"])
        self.assertEqual([], primary["retake_photo_ids"])


class BranchVersionTests(_Phase2Case):
    def _raw(self, snapshot: dict, key: str, checklist=()) -> dict:
        return {
            "request_key": key,
            "require_checklist": True,
            "template_key": "restaurant",
            "template_version": snapshot["template_version"],
            "file_slots": [
                slot["slot_code"] for slot in snapshot["capture_slots"] if slot["required"]
            ],
            "observations": {"metrics": [], "checklist": list(checklist)},
        }

    def _put(self, scope_kind, scope_key, patch, expected_version=0):
        return inspectionoverrides.upsert_override(2, 20, "restaurant", {
            "scope_kind": scope_kind,
            "scope_key": scope_key,
            "item_code": self.item["item_code"],
            "patch": patch,
            "expected_version": expected_version,
        })

    def setUp(self):
        super().setUp()
        self.item = next(
            item for item in inspectionstandards.effective_checklist("restaurant")
            if item["tier"] == "operations"
        )

    def test_other_region_override_does_not_change_this_store_version(self):
        base_a = inspectionoverrides.effective_snapshot(2, 22, "restaurant", self.a["id"])
        self.assertEqual(inspectionstandards.CATALOG_VERSION, base_a["template_version"])
        self._put("region", "华东", {"shot_guide": "华东专用拍法"})
        self._put("branch", self.b["id"], {"shot_guide": "静安店专用拍法"})
        after_a = inspectionoverrides.effective_snapshot(2, 22, "restaurant", self.a["id"])
        after_b = inspectionoverrides.effective_snapshot(2, 26, "restaurant", self.b["id"])
        self.assertEqual(base_a["template_version"], after_a["template_version"])
        self.assertNotEqual(base_a["template_version"], after_b["template_version"])
        # 朝阳店店长照旧提交，不受别的区域改标准影响。
        visit = inspection.create_visit_shell(
            2, 22, "restaurant", self.a["id"], self._raw(base_a, "p2-version-a-0001"),
        )
        self.assertEqual(base_a["template_version"], visit["template_version"])

    def test_stale_version_auto_revalidates_unless_filled_item_changed(self):
        before = inspectionoverrides.effective_snapshot(2, 22, "restaurant", self.a["id"])
        filled = [{"item_code": self.item["item_code"], "value": "消防记录已核对"}]
        row = self._put("tenant", None, {"shot_guide": "新拍法"})
        current = inspectionoverrides.effective_snapshot(2, 22, "restaurant", self.a["id"])
        self.assertNotEqual(before["template_version"], current["template_version"])
        visit = inspection.create_visit_shell(
            2, 22, "restaurant", self.a["id"],
            self._raw(before, "p2-version-refresh-1", filled),
        )
        self.assertEqual(current["template_version"], visit["template_version"])
        event = db.one(
            "SELECT payload_json FROM inspection_event WHERE visit_id=? "
            "AND kind='visit_created'",
            (visit["id"],),
        )
        self.assertTrue(json.loads(event["payload_json"])["standard_refreshed"])

        # 老板把本次填写过的检查项关掉了：这才要求重填，并说清是哪一项。
        self._put("tenant", None, {"enabled": False}, expected_version=row["version"])
        with self.assertRaises(inspection.InspectionError) as caught:
            inspection.create_visit_shell(
                2, 22, "restaurant", self.a["id"],
                self._raw(before, "p2-version-refresh-2", filled),
            )
        self.assertIn(self.item["label"], str(caught.exception))
        self.assertIn("重新填写", str(caught.exception))
        # 没填那一项的照样能交。
        inspection.create_visit_shell(
            2, 22, "restaurant", self.a["id"], self._raw(before, "p2-version-refresh-3"),
        )
        # 产品基线整体换代仍要求刷新。
        with self.assertRaisesRegex(inspection.InspectionError, "版本已更新"):
            inspection.create_visit_shell(
                2, 22, "restaurant", self.a["id"],
                {**self._raw(before, "p2-version-refresh-4"),
                 "template_version": "2020.01.0"},
            )


class ScoreTests(_Phase2Case):
    def test_deterministic_score_rules(self):
        score = inspection.deterministic_score
        self.assertEqual(100.0, score([]))
        self.assertEqual(85.0, score([{"severity": "high", "category": "safety"}]))
        # 同类问题同次只扣最重的一次。
        self.assertEqual(85.0, score([
            {"severity": "high", "category": "safety"},
            {"severity": "low", "category": "Safety"},
            {"severity": "medium", "category": "safety"},
        ]))
        self.assertEqual(74.0, score([
            {"severity": "high", "category": "safety"},
            {"severity": "medium", "category": "hygiene"},
            {"severity": "low", "category": "display"},
        ]))
        # 误报不扣；下限 0。
        self.assertEqual(97.0, score([
            {"severity": "high", "category": "safety", "false_positive": True},
            {"severity": "low", "category": "display"},
        ]))
        self.assertEqual(0.0, score([
            {"severity": "critical", "category": f"c{index}"} for index in range(6)
        ]))

    def test_model_score_is_reference_only_and_ranking_uses_rule(self):
        visit = self._visit_with_issues(self.a, (("low", "display"),))
        self.assertEqual(97.0, visit["score"])
        self.assertEqual(42, visit["ai_reference_score"])
        self.assertEqual(inspection.SCORE_RULE, visit["score_rule"])
        metrics = inspection.aggregate(2, 20, "restaurant")
        self.assertEqual(97.0, metrics["average_score"])

    def test_backfill_recomputes_legacy_visits_idempotently(self):
        legacy_ids = []
        for index, (score, model_json) in enumerate((
            (60, json.dumps({"summary": "旧", "score": 60, "issues": []})),
            (55, None),
        )):
            legacy_ids.append(int(db.insert("inspection_visit", {
                "tenant_id": 2, "industry_key": "restaurant", "branch_id": self.a["id"],
                "employee_idx": inspection.EMPLOYEE_IDX,
                "request_key": f"legacy-score-{index:04d}", "status": "completed",
                "score": score, "summary_md": "旧记录", "model_json": model_json,
                "created_by": 20, "version": 1,
            })))
        issue_id = int(db.insert("inspection_issue", {
            "tenant_id": 2, "visit_id": legacy_ids[0], "title": "旧问题",
            "description": "旧问题", "severity": "medium", "category": "hygiene",
            "status": "detected",
        }))
        db.insert("inspection_action", {
            "tenant_id": 2, "visit_id": legacy_ids[0], "issue_id": issue_id,
            "status": "open", "plan": "整改",
        })
        # 未完成的巡店不回填。
        pending_id = int(db.insert("inspection_visit", {
            "tenant_id": 2, "industry_key": "restaurant", "branch_id": self.a["id"],
            "employee_idx": inspection.EMPLOYEE_IDX, "request_key": "legacy-pending-01",
            "status": "analyzing", "created_by": 20, "version": 1,
        }))

        self.assertEqual(1, inspection.backfill_scores(batch_size=1))
        self.assertEqual(1, inspection.backfill_scores(batch_size=50))
        self.assertEqual(0, inspection.backfill_scores(batch_size=50))
        first = db.one("SELECT score,model_json FROM inspection_visit WHERE id=?",
                       (legacy_ids[0],))
        self.assertEqual(92.0, first["score"])
        model = json.loads(first["model_json"])
        self.assertEqual(60, model["ai_reference_score"])
        self.assertEqual(inspection.SCORE_RULE, model["score_rule"])
        self.assertEqual("旧", model["summary"])
        second = db.one("SELECT score,model_json FROM inspection_visit WHERE id=?",
                        (legacy_ids[1],))
        self.assertEqual(100.0, second["score"])
        self.assertEqual(55, json.loads(second["model_json"])["ai_reference_score"])
        self.assertIsNone(db.one(
            "SELECT model_json FROM inspection_visit WHERE id=?", (pending_id,)
        )["model_json"])
        # 再跑一次不改任何东西（ai_reference_score 不会被新分覆盖）。
        self.assertEqual(0, inspection.backfill_scores())
        self.assertEqual(
            60, json.loads(db.one(
                "SELECT model_json FROM inspection_visit WHERE id=?", (legacy_ids[0],)
            )["model_json"])["ai_reference_score"],
        )
        # 新完成的巡店自带标记，不会被回填重复处理。
        self._visit_with_issues(self.b)
        self.assertEqual(0, inspection.backfill_scores())


class PhotoMagicTests(unittest.TestCase):
    @staticmethod
    def _image(fmt: str) -> bytes:
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (2400, 1200), (200, 120, 40)).save(output, fmt)
        return output.getvalue()

    def test_sniff_by_content_not_filename(self):
        self.assertEqual(".jpg", inspection.sniff_photo_ext(self._image("JPEG")))
        self.assertEqual(".png", inspection.sniff_photo_ext(self._image("PNG")))
        self.assertEqual(".webp", inspection.sniff_photo_ext(self._image("WEBP")))
        self.assertIsNone(inspection.sniff_photo_ext(b"GIF89a....."))
        self.assertIsNone(inspection.sniff_photo_ext(b""))

    def test_upload_without_extension_is_accepted_and_reencoded(self):
        for fmt in ("JPEG", "PNG", "WEBP"):
            for filename in ("", "blob", "image", "wx_camera_123"):
                with self.subTest(fmt=fmt, filename=filename):
                    result = inspection.normalize_photo_upload(self._image(fmt), filename)
                    self.assertEqual("image/jpeg", result["mime_type"])
                    self.assertTrue(result["data"].startswith(b"\xff\xd8\xff"))
                    self.assertEqual((2400, 1200), (result["width"], result["height"]))
        # 扩展名撒谎也以内容为准。
        png_named_jpg = inspection.normalize_photo_upload(self._image("PNG"), "a.jpg")
        self.assertEqual("image/jpeg", png_named_jpg["mime_type"])

    def test_non_image_rejected_even_with_image_extension(self):
        for data in (b"not an image at all", b"\xff\xd8\xff" + b"\x00" * 64):
            with self.subTest(data=data[:8]), self.assertRaises(ValueError):
                inspection.normalize_photo_upload(data, "photo.jpg")


if __name__ == "__main__":
    unittest.main()
