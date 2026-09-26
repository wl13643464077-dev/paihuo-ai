"""Signed-session HTTP regression for permissions revoked after assignment."""
from __future__ import annotations

import os
from unittest import mock

from fastapi.testclient import TestClient

from app import auth, db, main, photoproof, stafftask
from tests.test_phase2_stafftask import StaffTaskBase, _jpeg


class StaffPermissionRevocationTests(StaffTaskBase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.root, exist_ok=True)
        files = next(route.app for route in main.app.routes
                     if getattr(route, "path", None) == "/files")
        self.file_roots = mock.patch.object(files, "all_directories", [self.root])
        self.file_roots.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.file_roots.stop()
        auth.set_current(None)
        super().tearDown()

    def _as(self, uid):
        self.client.cookies.set("cc_sess", auth.make_session(uid))

    def _task_with_photo(self, *, creator=20, assignee=23):
        task = self.create(creator, assignee_user_id=assignee, require_photo=False,
                           detail="仅本店员工可见的整改要求")
        submitted = self.submit(assignee, task["id"])
        photo = submitted["photos"][0]["url"]
        stafftask.review_task(2, self.u(20), task["id"], approve=False,
                              note="补充说明后重交")
        return task["id"], photo

    def _assert_current_access(self, task_id, photo):
        self.assertEqual(200, self.client.get(photo).status_code)
        result = self.client.get(f"/api/staff/tasks/{task_id}")
        self.assertEqual(200, result.status_code, result.text)
        self.assertTrue(result.json()["can_submit"])
        self.assertIn(task_id, [r["id"] for r in self.client.get("/api/staff/tasks").json()["items"]])
        self.assertIn(task_id, [r["id"] for r in self.client.get("/api/staff/todo").json()["tasks"]])

    def _assert_revoked(self, task_id, photo):
        events = db.one("SELECT COUNT(*) n FROM staff_task_event WHERE task_id=?", (task_id,))["n"]
        for endpoint, field in (("/api/staff/tasks", "items"), ("/api/staff/todo", "tasks")):
            response = self.client.get(endpoint)
            self.assertEqual(200, response.status_code, response.text)
            self.assertNotIn(task_id, [r["id"] for r in response.json()[field]])
        self.assertEqual(404, self.client.get(f"/api/staff/tasks/{task_id}").status_code)
        self.assertEqual(404, self.client.post(f"/api/staff/tasks/{task_id}/submit", data={"note": "旧指派不能授权"}).status_code)
        self.assertEqual(403, self.client.get(photo).status_code)
        self.assertEqual("todo", db.one("SELECT status FROM staff_task WHERE id=?", (task_id,))["status"])
        self.assertEqual(events, db.one("SELECT COUNT(*) n FROM staff_task_event WHERE task_id=?", (task_id,))["n"])

    def test_assignee_loses_access_when_industry_module_is_revoked(self):
        task_id, photo = self._task_with_photo()
        self._as(23)
        self._assert_current_access(task_id, photo)
        db.execute("UPDATE users SET modules_json='[\"content\"]' WHERE id=23")
        self._assert_revoked(task_id, photo)

    def test_assignee_loses_access_when_branch_binding_is_removed(self):
        task_id, photo = self._task_with_photo()
        self._as(23)
        self._assert_current_access(task_id, photo)
        db.execute("DELETE FROM user_branch WHERE tenant_id=2 AND user_id=23")
        self._assert_revoked(task_id, photo)

    def test_creator_assignment_does_not_bypass_revoked_manager_branch(self):
        task_id, photo = self._task_with_photo(creator=22, assignee=22)
        self._as(22)
        self._assert_current_access(task_id, photo)
        db.execute("DELETE FROM user_branch WHERE tenant_id=2 AND user_id=22")
        self._assert_revoked(task_id, photo)

    def test_signed_session_cannot_use_assignment_from_previous_tenant(self):
        task_id, photo = self._task_with_photo()
        self._as(23)
        self._assert_current_access(task_id, photo)
        db.execute("UPDATE users SET tenant_id=3 WHERE id=23")
        self._assert_revoked(task_id, photo)

    def test_submitted_replay_does_not_reveal_photos_after_revocation(self):
        task_id = self.create(assignee_user_id=23, require_photo=False)["id"]
        submitted = self.submit(23, task_id)
        self._as(23)
        db.execute("DELETE FROM user_branch WHERE tenant_id=2 AND user_id=23")
        self.assertEqual(404, self.client.get(f"/api/staff/tasks/{task_id}").status_code)
        self.assertEqual(404, self.client.post(f"/api/staff/tasks/{task_id}/submit", data={"note": "重放"}).status_code)
        self.assertEqual(403, self.client.get(submitted["photos"][0]["url"]).status_code)
        self.assertEqual("submitted", db.one("SELECT status FROM staff_task WHERE id=?", (task_id,))["status"])

    def test_owner_and_director_keep_authorized_access_without_branch_binding(self):
        for uid in (20, 21):
            with self.subTest(uid=uid):
                task_id, photo = self._task_with_photo(assignee=uid)
                self._as(uid)
                self._assert_current_access(task_id, photo)
                result = self.client.post(f"/api/staff/tasks/{task_id}/submit", data={"note": "交付"})
                self.assertEqual(200, result.status_code, result.text)
                self.assertEqual("submitted", result.json()["status"])

    def test_tenant_industry_revocation_also_hides_existing_assigned_tasks(self):
        task_id, photo = self._task_with_photo(assignee=20)
        self._as(20)
        self._assert_current_access(task_id, photo)
        db.execute("DELETE FROM tenant_industry WHERE tenant_id=2 AND industry_key='restaurant'")
        self._assert_revoked(task_id, photo)

    def test_revocation_during_photo_save_prevents_submission(self):
        task_id = self.create(assignee_user_id=23)["id"]

        def revoke_after_save(*args, **kwargs):
            saved = photoproof.store_photo(*args, **kwargs)
            db.execute("DELETE FROM user_branch WHERE tenant_id=2 AND user_id=23")
            return saved

        with self.assertRaises(stafftask.StaffTaskNotFound):
            stafftask.submit_task(2, self.u(23), task_id, photos=[_jpeg()],
                                  store=revoke_after_save, asset_root=self.root)
        self.assertEqual("todo", db.one("SELECT status FROM staff_task WHERE id=?", (task_id,))["status"])
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM staff_task_photo WHERE task_id=?", (task_id,))["n"])
        self.assertEqual([], [name for _root, _dirs, names in os.walk(self.root) for name in names])
