"""Real signed-session requests must recheck current checklist permissions."""
from __future__ import annotations

import json
import os
from unittest import mock

from fastapi.testclient import TestClient

from app import assetfiles, auth, checklist, db, inspection, main, photoproof, timeutil
from tests.test_phase2_stafftask import StaffTaskBase, _jpeg
from tests.test_phase2_inspection import _Phase2Case


class ChecklistPermissionRevocationTests(StaffTaskBase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.root, exist_ok=True)
        self.asset_patch = mock.patch.object(assetfiles, "ASSET_ROOT", self.root)
        self.asset_patch.start()
        files = next(route.app for route in main.app.routes
                     if getattr(route, "path", None) == "/files")
        self.file_roots = mock.patch.object(files, "all_directories", [self.root])
        self.file_roots.start()
        self.client = TestClient(main.app)
        items = [{"key": "check_a", "text": "确认冷柜清洁", "require_photo": False},
                 {"key": "check_b", "text": "确认台面清洁", "require_photo": False}]
        self.template = db.insert("checklist_template", {
            "tenant_id": 2, "industry_key": "restaurant", "kind": "custom",
            "name": "门店现场清单", "items_json": json.dumps(items),
        })
        self.run_id = db.insert("checklist_run", {
            "tenant_id": 2, "branch_id": self.a, "template_id": self.template,
            "run_date": timeutil.today_cn(), "kind": "custom", "status": "open",
            "assignee_user_id": 23, "items_json": json.dumps(items),
        })
        self._as(23)

    def tearDown(self):
        self.client.close()
        self.file_roots.stop()
        self.asset_patch.stop()
        auth.set_current(None)
        super().tearDown()

    def _as(self, uid):
        self.client.cookies.set("cc_sess", auth.make_session(uid))

    def _post(self, key="check_a", **kwargs):
        return self.client.post(f"/api/checklist/runs/{self.run_id}/items/{key}", **kwargs)

    def _assert_visible(self):
        mine = self.client.get("/api/checklist/runs?mine=1")
        self.assertEqual(200, mine.status_code, mine.text)
        self.assertIn(self.run_id, [row["id"] for row in mine.json()["items"]])
        todo = self.client.get("/api/staff/todo")
        self.assertIn(self.run_id, [row["id"] for row in todo.json()["checklists"]])

    def _assert_revoked(self):
        old = db.one("SELECT items_json,status FROM checklist_run WHERE id=?", (self.run_id,))
        for url, field in (("/api/checklist/runs?mine=1", "items"),
                           ("/api/staff/todo", "checklists")):
            response = self.client.get(url)
            self.assertEqual(200, response.status_code, response.text)
            self.assertNotIn(self.run_id, [row["id"] for row in response.json()[field]])
        overview = self.client.get("/api/checklist/runs")
        self.assertEqual(200, overview.status_code, overview.text)
        self.assertNotIn(self.a, [row["branch_id"] for row in overview.json()["stores"]])
        ranking = self.client.get("/api/stores/ranking")
        self.assertEqual(200, ranking.status_code, ranking.text)
        self.assertNotIn(self.a, [row["branch_id"] for row in ranking.json()["stores"]])
        result = self._post(data={"done": "1", "note": "旧指派不得授权"})
        self.assertEqual(404, result.status_code, result.text)
        self.assertEqual(old, db.one("SELECT items_json,status FROM checklist_run WHERE id=?", (self.run_id,)))

    def test_branch_unbinding_hides_historical_assignment(self):
        self._assert_visible()
        db.execute("DELETE FROM user_branch WHERE tenant_id=2 AND user_id=23")
        self._assert_revoked()

    def test_member_industry_revocation_hides_and_prevents_completion(self):
        self._assert_visible()
        db.execute("UPDATE users SET modules_json='[\"content\"]' WHERE id=23")
        self._assert_revoked()

    def test_tenant_industry_revocation_cannot_be_restored_by_old_branch(self):
        self._as(20)
        db.execute("UPDATE checklist_run SET assignee_user_id=20 WHERE id=?", (self.run_id,))
        self._assert_visible()
        db.execute("DELETE FROM tenant_industry WHERE tenant_id=2 AND industry_key='restaurant'")
        self._assert_revoked()

    def test_cross_tenant_session_cannot_use_old_assignment(self):
        self._assert_visible()
        db.execute("UPDATE users SET tenant_id=3 WHERE id=23")
        self._assert_revoked()

    def test_repeated_completion_and_old_photo_are_denied_after_revocation(self):
        result = self._post(data={"done": "1"}, files={"photo": ("proof.jpg", _jpeg(), "image/jpeg")})
        self.assertEqual(200, result.status_code, result.text)
        photo = result.json()["items"][0]["photo_url"]
        self.assertEqual(200, self.client.get(photo).status_code)
        db.execute("DELETE FROM user_branch WHERE tenant_id=2 AND user_id=23")
        self._assert_revoked()
        self.assertEqual(403, self.client.get(photo).status_code)

    def test_photo_processing_revocation_cleans_new_file_and_keeps_run_unchanged(self):
        original = photoproof.store_photo

        def save_then_revoke(*args, **kwargs):
            meta = original(*args, **kwargs)
            db.execute("UPDATE users SET modules_json='[\"content\"]' WHERE id=23")
            return meta

        before = db.one("SELECT items_json,status FROM checklist_run WHERE id=?", (self.run_id,))
        with mock.patch.object(photoproof, "store_photo", side_effect=save_then_revoke):
            result = self._post(data={"done": "1"}, files={"photo": ("proof.jpg", _jpeg(), "image/jpeg")})
        self.assertEqual(404, result.status_code, result.text)
        self.assertEqual(before, db.one("SELECT items_json,status FROM checklist_run WHERE id=?", (self.run_id,)))
        self.assertEqual([], [name for _root, _dirs, names in os.walk(self.root) for name in names])

    def test_owner_director_and_current_staff_can_complete(self):
        for uid in (20, 21, 23):
            with self.subTest(uid=uid):
                self._as(uid)
                db.execute("UPDATE checklist_run SET assignee_user_id=? WHERE id=?", (uid, self.run_id))
                self._assert_visible()
                result = self._post(data={"done": "1"})
                self.assertEqual(200, result.status_code, result.text)
                self.assertTrue(result.json()["items"][0]["done"])

    def test_director_without_industry_cannot_read_or_edit_old_template(self):
        self._as(21)
        db.execute("UPDATE users SET modules_json='[\"content\"]' WHERE id=21")
        response = self.client.get("/api/checklist/templates")
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn(self.template, [row["id"] for row in response.json()["items"]])
        result = self.client.put(f"/api/checklist/templates/{self.template}", json={"name": "越权修改"})
        self.assertEqual(404, result.status_code, result.text)
        result = self.client.post("/api/checklist/templates", json={
            "industry_key": "restaurant", "items": ["越权新增"],
        })
        self.assertEqual(403, result.status_code, result.text)
        result = self.client.put("/api/checklist/duty", json={"branch_id": self.a, "user_id": 23})
        self.assertEqual(404, result.status_code, result.text)


class InspectionTodoPermissionRevocationTests(_Phase2Case):
    def test_tenant_industry_revocation_hides_assigned_inspection_action(self):
        visit = self._visit_with_issues(self.a)
        action_id = visit["issues"][0]["action"]["id"]
        inspection.assign_action(2, 20, action_id, 23)
        client = TestClient(main.app)
        try:
            client.cookies.set("cc_sess", auth.make_session(23))
            before = client.get("/api/staff/todo")
            self.assertEqual(200, before.status_code, before.text)
            self.assertIn(action_id, [row["id"] for row in before.json()["actions"]])
            db.execute("DELETE FROM tenant_industry WHERE tenant_id=2 AND industry_key='restaurant'")
            after = client.get("/api/staff/todo")
            self.assertEqual(200, after.status_code, after.text)
            self.assertNotIn(action_id, [row["id"] for row in after.json()["actions"]])
            self.assertNotIn(action_id, [row["id"] for row in after.json()["items"]
                                        if row["kind"] == "inspection_action"])
        finally:
            client.close()
            auth.set_current(None)
