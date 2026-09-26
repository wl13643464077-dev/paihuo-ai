"""Route-level contracts for the brand, team-run and activity-image upgrade.

These tests deliberately use signed sessions and real tenant-scoped rows.  A
service-layer tenant check alone cannot catch an endpoint that forgets to
authorize the current task, draft, or reviewer before calling that service.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

from app import (
    assetfiles, auth, brand_media, brand_package, db, employeeidentity,
    growth, main, teamrun,
)
from app.engine import LAST_IDX
from app.skills import registry


TEAM = {
    "teamName": "周年庆协作小队",
    "summary": "先拆解，再并行交付",
    "members": [
        {"idx": 0, "name": "队长", "role": "内容策划", "roleInTeam": "队长",
         "task": "拆解活动目标", "dependsOn": []},
        {"idx": 160, "name": "超级店长", "role": "活动策划", "roleInTeam": "协同",
         "task": "制作活动物料", "dependsOn": [0]},
    ],
}


class UpgradeRouteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        self.old_root = main.ROOT
        main.ROOT = self.temp.name
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.temp.name, "routes.db")
        self.assets_root = Path(self.temp.name) / "data" / "assets"
        self.assets_root.mkdir(parents=True)
        self.asset_patch = mock.patch.object(
            assetfiles, "ASSET_ROOT", str(self.assets_root),
        )
        self.asset_patch.start()
        db.conn()
        for tenant_id, name in ((2, "甲公司"), (3, "乙公司")):
            db.insert("tenants", {
                "id": tenant_id, "name": name, "balance": 100,
            })
            db.execute(
                "INSERT INTO tenant_industry(tenant_id,industry_key) VALUES(?,?)",
                (tenant_id, "restaurant"),
            )
        self.owner = self._user(2, "route-owner", "owner")
        self.member = self._user(
            2, "route-member", "member", modules='["content","restaurant"]',
        )
        self.other_member = self._user(
            2, "route-other-member", "member", modules='["content"]',
        )
        self.foreign_owner = self._user(3, "route-foreign", "owner")
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        auth.set_current(None)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_db_path
        main.ROOT = self.old_root
        self.asset_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _user(tenant_id: int, name: str, role: str, modules: str = "[]") -> int:
        return db.insert("users", {
            "tenant_id": tenant_id, "username": name,
            "password_hash": "fixture", "role": role,
            "modules_json": modules,
        })

    def _as(self, user_id: int):
        self.client.cookies.set("cc_sess", auth.make_session(user_id))

    @staticmethod
    def _png() -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (48, 48), (30, 100, 160)).save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def _draft(tenant_id: int) -> dict:
        async def no_sources(_prompt):
            return {"sources": []}

        return asyncio.run(brand_package.collect(
            tenant_id, "青禾餐饮", research=no_sources,
        ))

    @staticmethod
    def _team_run(tenant_id: int, actor_id: int) -> dict:
        return teamrun.create_run(
            tenant_id, actor_id, "做一场门店周年庆", TEAM,
            mode="semi", depth="comprehensive",
            request_key=f"route-team-{tenant_id}-{actor_id}",
        )

    @classmethod
    def _confirmed_brand(cls, tenant_id: int) -> dict:
        draft = cls._draft(tenant_id)
        brand_package.add_fact(
            tenant_id, draft["id"], "brand_name", "青禾餐饮",
        )
        brand_package.add_fact(
            tenant_id, draft["id"], "store_name", "青禾小馆",
        )
        return brand_package.confirm(tenant_id, draft["id"])

    @staticmethod
    def _completed_report_job(tenant_id: int) -> int:
        job_id = db.insert("job", {
            "tenant_id": tenant_id,
            "brief_json": '{"direction":"门店活动复盘"}',
            "status": "done", "current_idx": LAST_IDX,
            "mode": "fullauto", "billing_status": "charged",
            "billing_points": 18,
        })
        db.insert("station_run", {
            "job_id": job_id, "station_idx": LAST_IDX,
            "skill_id": registry.BY_IDX[LAST_IDX]["skill"],
            "version": 1, "status": "done",
            "output_json": '{"report":"第一版复盘报告","next_topics":[]}',
        })
        return job_id

    @staticmethod
    def _task(tenant_id: int, emp_idx: int = 160,
              status: str = "done") -> int:
        employee = employeeidentity.active_employee(emp_idx)
        return db.insert("task", {
            "tenant_id": tenant_id,
            "emp_idx": emp_idx,
            "brief_json": '{}',
            "status": status,
            **employeeidentity.task_fields(employee),
        })

    def _artwork(self, tenant_id: int, task_id: int) -> int:
        result = brand_media.save_task_artwork(
            tenant_id, task_id, "周年庆主视觉",
            {
                "image_bytes": self._png(),
                "status": "needs_manual_review",
                "quality": {"status": "needs_manual_review", "reasons": ["待人工核对"]},
                "required_text": {
                    "store_name": "青禾小馆",
                    "activity_title": "周年庆",
                    "activity_content": "到店体验新品",
                    "authorized_texts": [],
                },
                "brand_package_id": 1,
                "brand_version": 1,
            },
        )
        return result["id"]

    def test_brand_draft_is_owner_only_and_confirmed_package_is_shared(self):
        draft = self._draft(2)
        path = f"/api/brand-packages/{draft['id']}"

        self._as(self.member)
        self.assertEqual(403, self.client.get(path).status_code)
        self.assertEqual(403, self.client.post(
            f"{path}/facts", json={"key": "store_name", "value": "青禾小馆"},
        ).status_code)
        self.assertEqual(403, self.client.post(f"{path}/confirm").status_code)

        self._as(self.foreign_owner)
        self.assertEqual(404, self.client.get(path).status_code)
        self.assertEqual([], self.client.get("/api/brand-packages").json()["items"])

        self._as(self.owner)
        self.assertEqual(200, self.client.get(path).status_code)
        added = self.client.post(
            f"{path}/facts", json={"key": "store_name", "value": "青禾小馆"},
        )
        self.assertEqual(200, added.status_code, added.text)
        confirmed = self.client.post(f"{path}/confirm")
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        self.assertEqual("confirmed", confirmed.json()["status"])

        self._as(self.member)
        self.assertEqual("青禾小馆", self.client.get(path).json()["store_name"])
        listed = self.client.get("/api/brand-packages").json()
        self.assertEqual([draft["id"]], [item["id"] for item in listed["items"]])

    def test_brand_logo_upload_is_owner_only_and_rejects_invalid_image(self):
        draft = self._draft(2)
        path = f"/api/brand-packages/{draft['id']}/logo"
        image = self._png()

        self._as(self.member)
        self.assertEqual(403, self.client.post(
            path, files={"file": ("logo.png", image, "image/png")},
        ).status_code)

        self._as(self.foreign_owner)
        self.assertEqual(404, self.client.post(
            path, files={"file": ("logo.png", image, "image/png")},
        ).status_code)

        self._as(self.owner)
        invalid = self.client.post(
            path, files={"file": ("logo.png", b"not an image", "image/png")},
        )
        self.assertEqual(400, invalid.status_code, invalid.text)
        self.assertFalse(brand_package.get_package(2, draft["id"])["facts"])
        uploaded = self.client.post(
            path, files={"file": ("logo.png", image, "image/png")},
        )
        self.assertEqual(200, uploaded.status_code, uploaded.text)
        package = brand_package.get_package(2, draft["id"])
        self.assertTrue(package["logo_url"].startswith("/files/tools/2/"))

    def test_team_run_routes_scope_creator_tenant_and_admin(self):
        run = self._team_run(2, self.member)
        path = f"/api/team-runs/{run['id']}"

        self._as(self.other_member)
        self.assertEqual(403, self.client.get(path).status_code)
        self.assertEqual(403, self.client.post(f"{path}/summary/retry").status_code)
        self.assertEqual([], self.client.get("/api/team-runs").json()["items"])

        self._as(self.foreign_owner)
        self.assertEqual(404, self.client.get(path).status_code)
        self.assertEqual(404, self.client.post(f"{path}/summary/retry").status_code)

        for actor in (self.member, self.owner):
            self._as(actor)
            response = self.client.get(path)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(run["id"], response.json()["id"])
            self.assertEqual([run["id"]], [
                item["id"] for item in self.client.get("/api/team-runs").json()["items"]
            ])

    def test_invalid_team_creation_does_not_create_or_charge(self):
        self._as(self.owner)
        response = self.client.post("/api/team-runs", json={
            "query": "做一场门店周年庆", "team": {"members": TEAM["members"][:1]},
            "mode": "auto", "depth": "comprehensive",
            "request_key": "invalid-team-request-001",
        })
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual(0, db.one("SELECT COUNT(*) AS n FROM team_run")["n"])
        self.assertEqual(100, db.one("SELECT balance FROM tenants WHERE id=2")["balance"])

    def test_artwork_list_file_and_review_stay_within_task_scope(self):
        task_id = self._task(2)
        image_id = self._artwork(2, task_id)
        base = f"/api/tasks/{task_id}/activity-images"

        self._as(self.foreign_owner)
        self.assertEqual(404, self.client.get(base).status_code)
        self.assertEqual(404, self.client.get(f"{base}/{image_id}/file").status_code)
        self.assertEqual(404, self.client.post(
            f"{base}/{image_id}/review", json={"decision": "reject"},
        ).status_code)

        self._as(self.other_member)
        self.assertEqual(404, self.client.get(base).status_code)
        self.assertEqual(404, self.client.get(f"{base}/{image_id}/file").status_code)

        self._as(self.member)
        listing = self.client.get(base)
        self.assertEqual(200, listing.status_code, listing.text)
        self.assertEqual(image_id, listing.json()["items"][0]["id"])
        self.assertEqual(200, self.client.get(f"{base}/{image_id}/file").status_code)
        self.assertEqual(403, self.client.post(
            f"{base}/{image_id}/review", json={
                "decision": "approve", "observed_text": "青禾小馆 周年庆 到店体验新品",
                "logo_match": True, "no_extra_claims": True,
            },
        ).status_code)

        self._as(self.owner)
        reviewed = self.client.post(
            f"{base}/{image_id}/review", json={
                "decision": "approve", "observed_text": "青禾小馆 周年庆 到店体验新品",
                "logo_match": True, "no_extra_claims": True,
                "note": "人工核对画面及品牌标志",
            },
        )
        self.assertEqual(200, reviewed.status_code, reviewed.text)
        self.assertEqual("passed", reviewed.json()["status"])
        self.assertEqual("passed", self.client.get(base).json()["items"][0]["status"])

    def test_invalid_activity_generation_does_not_charge(self):
        task_id = self._task(2)
        self._as(self.owner)
        response = self.client.post(
            f"/api/tasks/{task_id}/activity-images",
            json={"title": "", "content": "到店体验新品", "group_key": "周年庆"},
        )
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual(100, db.one("SELECT balance FROM tenants WHERE id=2")["balance"])
        self.assertEqual(0, db.one("SELECT COUNT(*) AS n FROM task_activity_image")["n"])

        missing_branch = self.client.post(
            f"/api/tasks/{task_id}/activity-images",
            json={"title": "周年庆", "content": "到店体验新品",
                  "group_key": "周年庆", "branch_id": -1},
        )
        self.assertEqual(400, missing_branch.status_code, missing_branch.text)
        self.assertEqual(100, db.one("SELECT balance FROM tenants WHERE id=2")["balance"])

        self._as(self.foreign_owner)
        cross_tenant = self.client.post(
            f"/api/tasks/{task_id}/activity-images",
            json={"title": "周年庆", "content": "到店体验新品"},
        )
        self.assertEqual(404, cross_tenant.status_code, cross_tenant.text)
        self.assertEqual(100, db.one("SELECT balance FROM tenants WHERE id=3")["balance"])

    def test_generated_artwork_is_billed_once_and_stays_a_candidate(self):
        task_id = self._task(2)
        self._as(self.owner)

        async def generate(_tenant_id, activity, **_kwargs):
            return {
                "image_bytes": self._png(),
                "status": "passed",  # a generator cannot self-approve delivery
                "quality": {"status": "passed", "reasons": []},
                "required_text": {
                    "store_name": "青禾小馆",
                    "activity_title": activity["title"],
                    "activity_content": activity["content"],
                    "authorized_texts": [],
                },
                "brand_package_id": 17,
                "brand_version": 2,
            }

        with mock.patch.object(brand_media, "generate_activity_image", generate):
            response = self.client.post(
                f"/api/tasks/{task_id}/activity-images",
                json={"title": "周年庆", "content": "到店体验新品",
                      "group_key": "周年庆主视觉"},
            )
        self.assertEqual(200, response.status_code, response.text)
        image = response.json()["image"]
        self.assertEqual("needs_manual_review", image["status"])
        self.assertEqual("周年庆主视觉", image["group_key"])
        self.assertEqual(2, response.json()["charged_points"])
        self.assertEqual(98, db.one("SELECT balance FROM tenants WHERE id=2")["balance"])
        self.assertEqual(1, db.one("SELECT COUNT(*) AS n FROM task_activity_image")["n"])
        self.assertEqual(200, self.client.get(image["file_url"]).status_code)

    def test_completed_report_revision_route_scopes_job_and_keeps_prior_delivery(self):
        job_id = self._completed_report_job(2)
        path = f"/api/jobs/{job_id}/report/revise"
        comment = "把下周行动拆成三步"

        self._as(self.foreign_owner)
        self.assertEqual(404, self.client.post(
            path, json={"comment": comment},
        ).status_code)

        self._as(self.owner)
        missing = self.client.post(path, json={"comment": "  "})
        self.assertEqual(400, missing.status_code, missing.text)
        self.assertEqual("done", db.one(
            "SELECT status FROM job WHERE id=?", (job_id,),
        )["status"])

        response = self.client.post(path, json={"comment": comment})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({"ok": True, "version": 2}, response.json())
        runs = db.q(
            "SELECT version,status,review_comment,output_json "
            "FROM station_run WHERE job_id=? AND station_idx=? ORDER BY version",
            (job_id, LAST_IDX),
        )
        self.assertEqual([(1, "done"), (2, "queued")], [
            (row["version"], row["status"]) for row in runs
        ])
        self.assertIn("第一版复盘报告", runs[0]["output_json"])
        self.assertEqual(comment, runs[1]["review_comment"])
        self.assertIsNone(runs[1]["output_json"])
        self.assertEqual(100, db.one(
            "SELECT balance FROM tenants WHERE id=2",
        )["balance"])
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_log WHERE job_id=?",
            (job_id,),
        )["n"])
        self.assertTrue(self.client.get(f"/api/jobs/{job_id}").json()[
            "report_revision_running"
        ])
        self.assertEqual(400, self.client.post(
            path, json={"comment": "同时再改一次"},
        ).status_code)

    def test_direct_video_brand_preflight_blocks_conflict_before_charge(self):
        brand = self._confirmed_brand(2)
        self._as(self.owner)
        conflicting = "品牌：别家餐饮\n这是一段用于门店推广的口播稿，讲清楚进店体验与服务价值。"
        response = self.client.post("/api/text-video", json={
            "title": "门店介绍", "script": conflicting,
        })
        self.assertEqual(400, response.status_code, response.text)
        self.assertIn("品牌", response.json()["detail"])
        self.assertEqual(0, db.one("SELECT COUNT(*) AS n FROM tv_job")["n"])
        self.assertEqual(100, db.one(
            "SELECT balance FROM tenants WHERE id=2",
        )["balance"])

        generic = "这是一段用于门店推广的口播稿，讲清楚进店体验与服务价值。"
        with mock.patch.object(main, "_start_text_video_worker") as start_worker:
            accepted = self.client.post("/api/text-video", json={
                "title": "门店介绍", "script": generic,
            })
        self.assertEqual(200, accepted.status_code, accepted.text)
        self.assertEqual(brand["version"], accepted.json()["brand_version"])
        self.assertTrue(accepted.json()["brand_warnings"])
        start_worker.assert_called_once_with(accepted.json()["tv_id"])
        saved = db.one(
            "SELECT status,billing_status FROM tv_job WHERE id=?",
            (accepted.json()["tv_id"],),
        )
        self.assertEqual(("queued", "charged"), (
            saved["status"], saved["billing_status"],
        ))
        self.assertEqual(97, db.one(
            "SELECT balance FROM tenants WHERE id=2",
        )["balance"])

    def test_variants_brand_preflight_rejects_conflict_and_returns_review(self):
        brand = self._confirmed_brand(2)
        self._as(self.owner)
        conflicting = (
            "店名：别家小馆\n这是一个完整的门店视频口播稿，"
            "请介绍服务流程、顾客体验和行动建议。"
        )
        rejected = self.client.post("/api/tools/variants", json={
            "script": conflicting, "n": 3,
        })
        self.assertEqual(400, rejected.status_code, rejected.text)
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_operation",
        )["n"])
        self.assertEqual(100, db.one(
            "SELECT balance FROM tenants WHERE id=2",
        )["balance"])

        generic = "这是一篇完整的门店口播稿，说明进店前如何预约、到店后如何体验以及最后如何反馈。"
        result = {
            "variants": [{"style": "温和", "hook": "先看服务",
                          "script": "青禾小馆的服务从预约开始。"}],
            "brand_warnings": ["口播稿未提及已确认的品牌或门店名"],
            "brand_version": brand["version"],
            "cost_usd": 0.01, "tokens": 10,
        }
        with mock.patch.object(growth, "script_variants", return_value=result) as generate:
            accepted = self.client.post("/api/tools/variants", json={
                "script": generic, "n": 3,
            })
        self.assertEqual(200, accepted.status_code, accepted.text)
        self.assertEqual(result, accepted.json())
        generate.assert_awaited_once_with(2, generic, 3, "")
        self.assertEqual(99, db.one(
            "SELECT balance FROM tenants WHERE id=2",
        )["balance"])
        self.assertEqual("succeeded", db.one(
            "SELECT status FROM billing_operation WHERE tenant_id=2 "
            "AND action='matrix_variants'",
        )["status"])


if __name__ == "__main__":
    unittest.main()
