"""行业老板看板纯服务层：权限、真实聚合与敏感正文边界。"""
from __future__ import annotations

import datetime as _datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app import (
    bossdashboard,
    db,
    departments,
    employeeidentity,
    taskrunner,
    taskthreads,
)


NOW = 1_800_000_000.0


class BossDashboardServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "boss-dashboard.db")
        db.conn()
        connection = db.conn()
        for tid, name, enabled in (
            (1, "平台", 1),
            (2, "企业甲", 1),
            (3, "企业乙", 1),
            (4, "已停用企业", 0),
            (5, "未配行业企业", 1),
        ):
            db.insert(
                "tenants",
                {"id": tid, "name": name, "enabled": enabled, "balance": 0},
            )
        connection.executemany(
            "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
            "VALUES(?,?,?,?)",
            (
                (1, "restaurant", 1, NOW - 1000),
                (1, "auto", 0, NOW - 1000),
                (2, "restaurant", 1, NOW - 1000),
                (3, "auto", 1, NOW - 1000),
                (4, "restaurant", 1, NOW - 1000),
            ),
        )
        connection.commit()

        specialists = departments.specialists()
        self.restaurant_employee = next(
            employee
            for employee in specialists.values()
            if employee["dept_key"] == "restaurant"
        )
        self.auto_employee = next(
            employee
            for employee in specialists.values()
            if employee["dept_key"] == "auto"
        )
        self.auto_legacy_employee = next(
            employee
            for employee in departments.legacy_specialists().values()
            if employee["dept_key"] == "auto"
            and employee["idx"] == self.auto_employee["idx"]
        )
        self.owner = {
            "id": 20,
            "tenant_id": 2,
            "username": "owner-a",
            "role": "owner",
            "enabled": 1,
        }
        self.root = {
            "id": 1,
            "tenant_id": 1,
            "username": "boss",
            "role": "root",
            "enabled": 1,
        }
        self.member = {
            "id": 21,
            "tenant_id": 2,
            "username": "member-a",
            "role": "member",
            "enabled": 1,
        }

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _task(
        self,
        *,
        tenant_id=2,
        employee=None,
        status="done",
        created_at=None,
        updated_at=None,
        body="不得被看板读取的业务正文-CANARY",
        **extra,
    ):
        employee = employee or self.restaurant_employee
        created_at = created_at if created_at is not None else NOW - 3600
        updated_at = updated_at if updated_at is not None else NOW - 1800
        terminal_at = extra.pop(
            "terminal_at",
            updated_at if status in {"done", "failed", "cancelled"} else None,
        )
        refunded_at = extra.pop(
            "refunded_at",
            updated_at if extra.get("billing_status") == "refunded" else None,
        )
        identity_employee = (
            employee
            if all(
                employee.get(field) not in (None, "")
                for field in (
                    "idx", "key", "name", "dept_key", "catalog_version",
                    "employee_spec_sha256",
                )
            )
            else None
        )
        identity_fields = (
            employeeidentity.task_fields(identity_employee)
            if identity_employee is not None
            else {}
        )
        return db.insert(
            "task",
            {
                "tenant_id": tenant_id,
                "emp_idx": employee["idx"],
                **identity_fields,
                "brief_json": json.dumps({"body": body}, ensure_ascii=False),
                "output_md": body,
                "summary_md": body,
                "steps_json": json.dumps([body], ensure_ascii=False),
                "status": status,
                "tokens": 100,
                "cost_usd": 0.25,
                "billing_points": 2,
                "billing_status": "included",
                "created_at": created_at,
                "updated_at": updated_at,
                "terminal_at": terminal_at,
                "refunded_at": refunded_at,
                **extra,
            },
        )

    def _visit(self, employee_idx=10):
        connection = db.conn()
        connection.execute(
            "INSERT INTO store_branch(id,tenant_id,industry_key,name,created_at,updated_at) "
            "VALUES(1,2,'restaurant','内部门店名',?,?)",
            (NOW - 10_000, NOW - 10_000),
        )
        connection.execute(
            "INSERT INTO inspection_visit("
            "id,tenant_id,industry_key,branch_id,status,score,summary_md,employee_idx,"
            "task_id,request_key,visit_at,created_at,updated_at,completed_at,terminal_at"
            ") VALUES(1,2,'restaurant',1,'completed',88,'巡店正文-CANARY',?,?,?,?,?,?,?,?)",
            (
                employee_idx,
                None,
                "visit-request-1",
                NOW - 5000,
                NOW - 5000,
                NOW - 4000,
                NOW - 4000,
                NOW - 4000,
            ),
        )
        connection.executemany(
            "INSERT INTO inspection_issue("
            "id,tenant_id,visit_id,title,description,severity,status,due_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    1, 2, 1, "问题标题", "问题正文-CANARY", "critical",
                    "detected", NOW - 10, NOW - 4000, NOW - 4000,
                ),
                (
                    2, 2, 1, "已解决标题", "已解决正文-CANARY", "low",
                    "closed", NOW - 100, NOW - 4000, NOW - 1000,
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO inspection_action("
            "id,tenant_id,visit_id,issue_id,status,plan,due_at,closed_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                (1, 2, 1, 1, "open", "整改正文-CANARY", NOW - 5, None, NOW - 3000, NOW - 3000),
                (
                    2, 2, 1, 2, "closed", "已完成正文-CANARY",
                    NOW - 50, NOW - 20, NOW - 3000, NOW - 20,
                ),
            ),
        )
        connection.commit()

    def test_members_disabled_users_and_unknown_roles_are_denied_before_queries(self):
        actors = (
            self.member,
            {**self.owner, "enabled": 0},
            {**self.owner, "role": "auditor"},
            {},
        )
        for actor in actors:
            with self.subTest(actor=actor), patch.object(db, "q") as query:
                with self.assertRaises(bossdashboard.DashboardAccessDenied):
                    bossdashboard.scopes(actor, is_boss=False)
                query.assert_not_called()

    def test_owner_is_fixed_to_own_enabled_tenant_and_explicit_industry(self):
        result = bossdashboard.scopes(self.owner)
        self.assertFalse(result["can_cross_tenant"])
        self.assertEqual([2], [tenant["id"] for tenant in result["tenants"]])
        self.assertEqual(
            ["restaurant"],
            [industry["key"] for industry in result["tenants"][0]["industries"]],
        )

        with self.assertRaises(bossdashboard.DashboardAccessDenied):
            bossdashboard.summary(
                self.owner,
                tenant_id=3,
                industry_key="auto",
                now=NOW,
            )
        with self.assertRaises(bossdashboard.DashboardAccessDenied):
            bossdashboard.summary(
                self.owner,
                tenant_id=2,
                industry_key="auto",
                now=NOW,
            )

        # 即便调用方误把 is_boss=True 传给 owner，也不得升权。
        with self.assertRaises(bossdashboard.DashboardAccessDenied):
            bossdashboard.summary(
                self.owner,
                is_boss=True,
                tenant_id=3,
                industry_key="auto",
                now=NOW,
            )

    def test_only_named_root_boss_can_cross_tenants_and_industries(self):
        local_root = bossdashboard.scopes(self.root, is_boss=False)
        self.assertFalse(local_root["can_cross_tenant"])
        self.assertEqual([1], [tenant["id"] for tenant in local_root["tenants"]])

        # 命名 Boss 的可选范围来自真实租户的显式映射，
        # 不要求平台 tenant=1 自己拥有“全行业”魔法映射。
        db.execute("DELETE FROM tenant_industry WHERE tenant_id=1")
        global_scopes = bossdashboard.scopes(self.root, is_boss=True)
        self.assertTrue(global_scopes["can_cross_tenant"])
        by_tenant = {tenant["id"]: tenant for tenant in global_scopes["tenants"]}
        self.assertEqual({2, 3}, set(by_tenant))
        self.assertNotIn(4, by_tenant)  # 已停用
        self.assertNotIn(5, by_tenant)  # 空行业映射

        self._task(tenant_id=3, employee=self.auto_employee)
        result = bossdashboard.summary(
            self.root,
            is_boss=True,
            tenant_id=3,
            industry_key="auto",
            now=NOW,
        )
        self.assertEqual(3, result["scope"]["tenant_id"])
        self.assertEqual("auto", result["scope"]["industry_key"])
        self.assertFalse(result["can_open_records"])
        self.assertEqual(1, len(result["recent_activity"]))
        self.assertTrue(
            all(item["target_route"] is None for item in result["recent_activity"])
        )
        cross_detail = bossdashboard.employee_detail(
            self.root,
            is_boss=True,
            tenant_id=3,
            industry_key="auto",
            employee_idx=self.auto_employee["idx"],
            now=NOW,
        )
        self.assertFalse(cross_detail["can_open_records"])
        self.assertTrue(cross_detail["tasks"]["items"])
        self.assertTrue(
            all(
                item["target_route"] is None
                for item in cross_detail["tasks"]["items"]
            )
        )

    def test_frozen_legacy_identity_is_visible_but_unknown_fails_closed(self):
        self._task(
            tenant_id=3,
            employee=self.auto_employee,
            status="done",
            tokens=120,
            cost_usd=1.25,
        )
        legacy_task_id = self._task(
            tenant_id=3,
            employee=self.auto_legacy_employee,
            status="done",
            tokens=340,
            cost_usd=2.75,
        )
        self._task(
            tenant_id=3,
            employee={"idx": 99_999},
            status="done",
            tokens=999,
            cost_usd=99.0,
            employee_key="legacy.idx.99999",
            employee_catalog_version="legacy-unknown",
            employee_name_snapshot="未知历史员工",
            employee_dept_key="unknown",
            employee_spec_sha256="0" * 64,
        )
        self._task(
            tenant_id=3,
            employee=self.auto_employee,
            status="done",
            body="PADDED-CANARY",
            employee_key=f" {self.auto_employee['key']} ",
        )

        result = bossdashboard.summary(
            self.root,
            is_boss=True,
            tenant_id=3,
            industry_key="auto",
            now=NOW,
        )
        self.assertEqual(2, result["task_metrics"]["total"])
        self.assertEqual(2, result["task_metrics"]["completed"])
        self.assertEqual(1.0, result["task_metrics"]["completion_rate"])
        self.assertEqual(460, result["efficiency_metrics"]["tokens"])
        self.assertEqual(4.0, result["efficiency_metrics"]["cost_usd"])

        specialists = [
            item
            for item in result["employees"]
            if item["employee_kind"] == "industry_specialist"
        ]
        active = [item for item in specialists if item["roster_status"] == "active"]
        legacy = [item for item in specialists if item["roster_status"] == "legacy"]
        self.assertEqual(36, len(active))
        self.assertTrue(all(item["can_assign"] for item in active))
        self.assertEqual(1, len(legacy))
        legacy_card = legacy[0]
        self.assertEqual(self.auto_legacy_employee["idx"], legacy_card["idx"])
        self.assertEqual(self.auto_legacy_employee["name"], legacy_card["name"])
        self.assertFalse(legacy_card["can_assign"])
        self.assertEqual(1, legacy_card["tasks"])
        self.assertEqual(1.0, legacy_card["completion_rate"])
        self.assertEqual(340, legacy_card["tokens"])
        self.assertEqual(2.75, legacy_card["cost_usd"])
        self.assertNotIn(99_999, {item["idx"] for item in specialists})

        detail = bossdashboard.employee_detail(
            self.root,
            is_boss=True,
            tenant_id=3,
            industry_key="auto",
            employee_idx=self.auto_legacy_employee["idx"],
            identity_ref=legacy_card["identity_ref"],
            now=NOW,
        )
        self.assertEqual("legacy", detail["employee"]["roster_status"])
        self.assertFalse(detail["employee"]["can_assign"])
        self.assertEqual(self.auto_legacy_employee["name"], detail["employee"]["name"])
        self.assertEqual(1, detail["tasks"]["total"])
        self.assertEqual(legacy_task_id, detail["tasks"]["items"][0]["id"])
        self.assertTrue(
            any(
                item["employee_name"] == self.auto_legacy_employee["name"]
                for item in result["recent_activity"]
            )
        )
        self.assertFalse(
            any(item["employee_idx"] == 99_999 for item in result["recent_activity"])
        )
        with self.assertRaises(bossdashboard.DashboardAccessDenied):
            bossdashboard.employee_detail(
                self.root,
                is_boss=True,
                tenant_id=3,
                industry_key="auto",
                employee_idx=99_999,
                now=NOW,
            )

    def test_legacy_production_api_separates_active_legacy_and_unknown(self):
        from app import main

        self._task(
            tenant_id=3,
            employee=self.auto_employee,
            status="done",
            tokens=100,
            cost_usd=1.0,
        )
        legacy_id = self._task(
            tenant_id=3,
            employee=self.auto_legacy_employee,
            status="done",
            tokens=200,
            cost_usd=2.0,
        )
        self._task(
            tenant_id=3,
            employee={"idx": 99_999},
            status="done",
            tokens=300,
            cost_usd=3.0,
            employee_key="legacy.idx.99999",
            employee_catalog_version="legacy-unknown",
            employee_name_snapshot="不可信历史名称",
            employee_dept_key="unknown",
            employee_spec_sha256="0" * 64,
        )
        with patch.object(main, "_need_admin"), patch.object(
            main, "TEN", return_value=3,
        ), patch.object(main.auth, "allowed", return_value=True):
            result = main.boss_production()
            legacy_identity = next(
                item for item in result["employees"]
                if item["roster_status"] == "legacy"
            )
            detail = main.boss_production_detail(
                self.auto_legacy_employee["idx"],
                legacy_identity["identity_ref"],
            )

        self.assertEqual(2, result["total"]["employees"])
        self.assertEqual(2, result["total"]["runs"])
        self.assertEqual(300, result["total"]["tokens"])
        self.assertEqual(3.0, result["total"]["cost_usd"])
        by_status = {item["roster_status"]: item for item in result["employees"]}
        self.assertEqual({"active", "legacy"}, set(by_status))
        self.assertTrue(by_status["active"]["can_assign"])
        self.assertFalse(by_status["legacy"]["can_assign"])
        legacy_display_name = (
            f"{self.auto_legacy_employee.get('person', '')}·"
            f"{self.auto_legacy_employee['name']}"
        ).strip("·")
        self.assertEqual(legacy_display_name, by_status["legacy"]["name"])

        self.assertEqual(legacy_display_name, detail["name"])
        self.assertEqual("legacy", detail["roster_status"])
        self.assertFalse(detail["can_assign"])
        task_item = next(item for item in detail["items"] if item["id"] == legacy_id)
        self.assertEqual("legacy", task_item["employee"]["roster_status"])

    def test_legacy_production_groups_same_idx_by_complete_frozen_identity(self):
        from app import main

        self._task(
            tenant_id=3,
            employee=self.auto_employee,
            status="done",
            tokens=100,
            cost_usd=1.0,
        )
        self._task(
            tenant_id=3,
            employee=self.auto_legacy_employee,
            status="done",
            tokens=200,
            cost_usd=2.0,
        )
        with patch.object(main, "_need_admin"), patch.object(
            main, "TEN", return_value=3,
        ), patch.object(main.auth, "allowed", return_value=True):
            result = main.boss_production()
            detail = main.boss_production_detail(self.auto_employee["idx"])

        same_idx = [
            item
            for item in result["employees"]
            if item["idx"] == self.auto_employee["idx"]
        ]
        self.assertEqual(2, len(same_idx))
        self.assertEqual({"active", "legacy"}, {
            item["roster_status"] for item in same_idx
        })
        self.assertEqual({1.0, 2.0}, {item["cost_usd"] for item in same_idx})
        self.assertEqual(2, result["total"]["employees"])
        self.assertEqual(300, result["total"]["tokens"])
        self.assertEqual("mixed", detail["roster_status"])
        self.assertFalse(detail["can_assign"])
        self.assertEqual({"active", "legacy"}, {
            item["roster_status"] for item in detail["identities"]
        })

        by_status = {
            item["roster_status"]: item for item in same_idx
        }
        with patch.object(main, "_need_admin"), patch.object(
            main, "TEN", return_value=3,
        ), patch.object(main.auth, "allowed", return_value=True):
            active_detail = main.boss_production_detail(
                self.auto_employee["idx"], by_status["active"]["identity_ref"]
            )
            legacy_detail = main.boss_production_detail(
                self.auto_employee["idx"], by_status["legacy"]["identity_ref"]
            )
        self.assertEqual("active", active_detail["roster_status"])
        self.assertEqual({100}, {item["tokens"] for item in active_detail["items"]})
        self.assertEqual("legacy", legacy_detail["roster_status"])
        self.assertEqual({200}, {item["tokens"] for item in legacy_detail["items"]})

    def test_dashboard_same_idx_requires_and_filters_complete_identity_ref(self):
        active_id = self._task(
            tenant_id=3,
            employee=self.auto_employee,
            status="done",
            tokens=100,
        )
        legacy_id = self._task(
            tenant_id=3,
            employee=self.auto_legacy_employee,
            status="done",
            tokens=200,
        )
        summary = bossdashboard.summary(
            self.root,
            is_boss=True,
            tenant_id=3,
            industry_key="auto",
            now=NOW,
        )
        same_idx = [
            item for item in summary["employees"]
            if item["employee_kind"] == "industry_specialist"
            and item["idx"] == self.auto_employee["idx"]
        ]
        self.assertEqual(2, len(same_idx))
        self.assertEqual(2, len({item["identity_ref"] for item in same_idx}))
        with self.assertRaises(bossdashboard.DashboardAccessDenied):
            bossdashboard.employee_detail(
                self.root,
                is_boss=True,
                tenant_id=3,
                industry_key="auto",
                employee_idx=self.auto_employee["idx"],
                now=NOW,
            )
        by_status = {item["roster_status"]: item for item in same_idx}
        active = bossdashboard.employee_detail(
            self.root,
            is_boss=True,
            tenant_id=3,
            industry_key="auto",
            employee_idx=self.auto_employee["idx"],
            identity_ref=by_status["active"]["identity_ref"],
            now=NOW,
        )
        legacy = bossdashboard.employee_detail(
            self.root,
            is_boss=True,
            tenant_id=3,
            industry_key="auto",
            employee_idx=self.auto_employee["idx"],
            identity_ref=by_status["legacy"]["identity_ref"],
            now=NOW,
        )
        self.assertEqual(active_id, active["tasks"]["items"][0]["id"])
        self.assertEqual(legacy_id, legacy["tasks"]["items"][0]["id"])
        self.assertEqual(
            self.auto_legacy_employee["name"], legacy["employee"]["name"]
        )

    def test_legacy_production_obeys_current_industry_scope_and_denies_unknown(self):
        from app import auth, main

        visible_id = self._task(
            tenant_id=3,
            employee=self.auto_employee,
            status="done",
            body="REVOKED-CANARY",
        )
        self._task(
            tenant_id=3,
            employee={"idx": 99_999},
            status="done",
            body="UNKNOWN-CANARY",
            employee_key="legacy.idx.99999",
            employee_catalog_version="legacy-unknown",
            employee_name_snapshot="未知历史员工",
            employee_dept_key="unknown",
            employee_spec_sha256="0" * 64,
        )
        auth.set_current({
            "id": 30, "tenant_id": 3, "role": "owner", "enabled": 1,
            "modules": [],
        })
        allowed = main.boss_production()
        visible = next(
            item for item in allowed["employees"]
            if item["idx"] == self.auto_employee["idx"]
        )
        detail = main.boss_production_detail(
            self.auto_employee["idx"], visible["identity_ref"]
        )
        self.assertEqual(visible_id, detail["items"][0]["id"])
        self.assertNotIn(99_999, {item["idx"] for item in allowed["employees"]})
        self.assertEqual(
            1,
            len([
                item for item in allowed["employees"]
                if item["idx"] == self.auto_employee["idx"]
            ]),
        )

        db.execute("DELETE FROM tenant_industry WHERE tenant_id=3 AND industry_key='auto'")
        revoked = main.boss_production()
        self.assertEqual([], revoked["employees"])
        with self.assertRaises(main.HTTPException) as caught:
            main.boss_production_detail(
                self.auto_employee["idx"], visible["identity_ref"]
            )
        self.assertEqual(404, caught.exception.status_code)

    def test_unknown_empty_disabled_and_missing_mapping_fail_closed(self):
        cases = (
            (999, "restaurant"),
            (4, "restaurant"),
            (5, "restaurant"),
            (2, "not-a-real-industry"),
        )
        for tenant_id, industry_key in cases:
            with self.subTest(tenant_id=tenant_id, industry_key=industry_key):
                with self.assertRaises(bossdashboard.DashboardScopeUnavailable):
                    bossdashboard.summary(
                        self.root,
                        is_boss=True,
                        tenant_id=tenant_id,
                        industry_key=industry_key,
                        now=NOW,
                    )

        db.execute("DROP TABLE tenant_industry")
        with self.assertRaises(bossdashboard.DashboardScopeUnavailable):
            bossdashboard.scopes(self.owner)

    def test_summary_aggregates_only_scoped_structural_fields_without_reading_body(self):
        self._task(status="done", created_at=NOW - 7200, updated_at=NOW - 3600)
        self._task(status="running", created_at=NOW - 200_000, updated_at=NOW - 100_000)
        self._task(status="awaiting_review", created_at=NOW - 5000, updated_at=NOW - 4500)
        self._task(status="failed", created_at=NOW - 4000, updated_at=NOW - 3500)
        self._task(status="cancelled", created_at=NOW - 3000, updated_at=NOW - 2800)
        self._task(tenant_id=3, employee=self.auto_employee, status="done")
        self._task(tenant_id=2, employee=self.auto_employee, status="done")
        self._task(
            tenant_id=2,
            employee={"idx": 10},
            status="done",
            body="普通 task idx10 不得跨行业-CANARY",
        )
        self._visit()

        statements = []
        real_q = db.q

        def traced_q(sql, args=()):
            statements.append(" ".join(sql.lower().split()))
            return real_q(sql, args)

        with patch.object(db, "q", side_effect=traced_q):
            result = bossdashboard.summary(
                self.owner,
                tenant_id=2,
                industry_key="restaurant",
                days=30,
                now=NOW,
            )

        task_metrics = result["task_metrics"]
        self.assertEqual(5, task_metrics["total"])
        self.assertEqual(1, task_metrics["active"])
        self.assertEqual(1, task_metrics["waiting"])
        self.assertEqual(1, task_metrics["completed"])
        self.assertEqual(1, task_metrics["failed"])
        self.assertEqual(1, task_metrics["cancelled"])
        self.assertAlmostEqual(0.2, task_metrics["completion_rate"])
        self.assertEqual(3600.0, result["efficiency_metrics"]["average_cycle_seconds"])
        self.assertEqual(1, result["risk_metrics"]["stale_active_tasks"])
        self.assertEqual(1, result["risk_metrics"]["waiting_for_decision"])

        inspection = result["inspection_metrics"]
        self.assertTrue(inspection["availability"])
        self.assertEqual(1, inspection["visits"])
        self.assertEqual(1, inspection["completed_visits"])
        self.assertEqual(88.0, inspection["average_score"])
        self.assertEqual(2, inspection["issues"])
        self.assertEqual(1, inspection["open_issues"])
        self.assertEqual(1, inspection["critical_issues"])
        self.assertEqual(1, inspection["overdue_issues"])
        self.assertEqual(2, inspection["actions"])
        self.assertEqual(1, inspection["overdue_actions"])
        self.assertEqual("all_open_records", inspection["backlog"]["scope"])
        self.assertEqual(1, inspection["backlog"]["critical_issues"])
        self.assertEqual(1, inspection["backlog"]["overdue_actions"])
        self.assertFalse(result["business_metrics"]["availability"])
        self.assertIsNone(result["business_metrics"]["revenue"])
        self.assertIsNone(result["business_metrics"]["profit"])
        self.assertIsNone(result["business_metrics"]["roi"])
        inspection_employee = next(
            employee
            for employee in result["employees"]
            if employee["idx"] == 10
        )
        self.assertEqual("巡店经理", inspection_employee["name"])
        self.assertEqual("inspection", inspection_employee["employee_kind"])
        self.assertEqual(1, inspection_employee["tasks"])
        self.assertEqual(1, inspection_employee["completed"])
        self.assertEqual(1.0, inspection_employee["completion_rate"])
        self.assertEqual(1, inspection_employee["inspection_visits"])
        recent = result["recent_activity"]
        self.assertTrue(recent)
        self.assertTrue(all("target_route" in item for item in recent))
        self.assertTrue(
            any(item["kind"] == "inspection" for item in recent)
        )
        self.assertTrue(
            any(
                item["kind"] == "inspection"
                and item["target_route"] == "#/inspections/1/restaurant"
                for item in recent
            )
        )
        self.assertTrue(any(item["kind"] == "task" for item in recent))

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("CANARY", serialized)
        forbidden_columns = (
            "brief_json",
            "output_md",
            "summary_md",
            "steps_json",
            "result_json",
            "params_json",
        )
        for column in forbidden_columns:
            self.assertFalse(
                any(column in statement for statement in statements),
                f"看板查询不得读取 {column}",
            )

    def test_inspection_source_absence_is_explicit_not_a_fake_zero(self):
        db.execute("DROP TABLE inspection_action")
        result = bossdashboard.summary(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            now=NOW,
        )
        self.assertFalse(result["inspection_metrics"]["availability"])
        self.assertIsNone(result["inspection_metrics"]["visits"])
        self.assertIsNone(result["risk_metrics"]["overdue_inspection_issues"])

    def test_inspection_period_and_all_open_backlog_are_not_mixed(self):
        self._visit()
        db.execute(
            "UPDATE inspection_visit SET created_at=?,visit_at=? WHERE id=1",
            (NOW - 100 * 86400, NOW - 100 * 86400),
        )
        result = bossdashboard.summary(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            days=1,
            now=NOW,
        )
        metrics = result["inspection_metrics"]
        self.assertEqual(0, metrics["visits"])
        self.assertEqual(0, metrics["issues"])
        self.assertEqual(0, metrics["actions"])
        self.assertEqual(1, metrics["backlog"]["open_issues"])
        self.assertEqual(1, metrics["backlog"]["critical_issues"])
        self.assertEqual(1, metrics["backlog"]["overdue_actions"])
        self.assertEqual(1, result["risk_metrics"]["critical_inspection_issues"])

    def test_old_inspection_completed_this_period_is_in_card_recent_and_detail(self):
        self._visit()
        db.execute(
            "UPDATE inspection_visit SET created_at=?,visit_at=?,completed_at=?,"
            "terminal_at=?,updated_at=? WHERE id=1",
            (
                NOW - 40 * 86400,
                NOW - 40 * 86400,
                NOW - 100,
                NOW - 100,
                NOW - 1,
            ),
        )
        result = bossdashboard.summary(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            days=7,
            now=NOW,
        )
        self.assertEqual(0, result["inspection_metrics"]["visits"])
        self.assertEqual(1, result["inspection_metrics"]["completed_visits"])
        inspector = next(item for item in result["employees"] if item["idx"] == 10)
        self.assertEqual(0, inspector["tasks"])
        self.assertEqual(1, inspector["completed"])
        self.assertIsNone(inspector["completion_rate"])
        recent = next(
            item for item in result["recent_activity"]
            if item["kind"] == "inspection"
        )
        self.assertEqual(NOW - 100, recent["occurred_at"])
        self.assertEqual("#/inspections/1/restaurant", recent["target_route"])

        detail = bossdashboard.employee_detail(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            employee_idx=10,
            days=7,
            now=NOW,
        )
        self.assertEqual(1, detail["inspection_visits"]["total"])
        item = detail["inspection_visits"]["items"][0]
        self.assertEqual("completed", item["period_event_kind"])
        self.assertEqual(NOW - 100, item["period_event_at"])

    def test_old_inspection_failed_this_period_uses_stable_terminal_clock(self):
        self._visit()
        db.execute(
            "UPDATE inspection_visit SET status='failed',score=NULL,created_at=?,"
            "visit_at=?,completed_at=NULL,terminal_at=?,updated_at=? WHERE id=1",
            (NOW - 40 * 86400, NOW - 40 * 86400, NOW - 80, NOW - 1),
        )
        result = bossdashboard.summary(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            days=7,
            now=NOW,
        )
        self.assertEqual(0, result["inspection_metrics"]["visits"])
        self.assertEqual(1, result["inspection_metrics"]["failed_visits"])
        inspector = next(item for item in result["employees"] if item["idx"] == 10)
        self.assertEqual(1, inspector["failed"])
        recent = next(
            item for item in result["recent_activity"]
            if item["kind"] == "inspection"
        )
        self.assertEqual(NOW - 80, recent["occurred_at"])
        detail = bossdashboard.employee_detail(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            employee_idx=10,
            days=7,
            now=NOW,
        )
        item = detail["inspection_visits"]["items"][0]
        self.assertEqual("failed", item["period_event_kind"])
        self.assertEqual(NOW - 80, item["period_event_at"])

    def test_inspection_linked_task_finance_is_real_and_industry_scoped(self):
        self._visit()
        restaurant_task = self._task(
            employee={"idx": 10},
            status="failed",
            billing_status="refunded",
            billing_points=1,
            tokens=321,
            cost_usd=0.75,
            created_at=NOW - 500,
            updated_at=NOW - 100,
            terminal_at=NOW - 100,
            refunded_at=NOW - 90,
        )
        db.execute(
            "UPDATE inspection_visit SET task_id=?,status='failed',completed_at=NULL,"
            "terminal_at=?,updated_at=? WHERE id=1",
            (restaurant_task, NOW - 100, NOW - 1),
        )

        auto_task = self._task(
            employee={"idx": 10},
            status="failed",
            billing_status="refunded",
            billing_points=9,
            tokens=999,
            cost_usd=9.99,
            terminal_at=NOW - 70,
            refunded_at=NOW - 60,
        )
        connection = db.conn()
        connection.execute(
            "INSERT INTO store_branch(id,tenant_id,industry_key,name,created_at,updated_at) "
            "VALUES(2,2,'auto','跨行业门店',?,?)",
            (NOW - 1000, NOW - 1000),
        )
        connection.execute(
            "INSERT INTO inspection_visit(id,tenant_id,industry_key,branch_id,"
            "employee_idx,task_id,status,visit_at,created_at,updated_at,terminal_at) "
            "VALUES(2,2,'auto',2,10,?,'failed',?,?,?,?)",
            (auto_task, NOW - 500, NOW - 500, NOW - 1, NOW - 70),
        )
        connection.commit()

        result = bossdashboard.summary(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            days=7,
            now=NOW,
        )
        self.assertEqual(1, result["task_metrics"]["total"])
        self.assertEqual(1, result["task_metrics"]["failed"])
        self.assertEqual(1, result["task_metrics"]["refunded"])
        self.assertEqual(1.0, result["task_metrics"]["refunded_points"])
        self.assertEqual(321, result["efficiency_metrics"]["tokens"])
        self.assertEqual(0.75, result["efficiency_metrics"]["cost_usd"])
        self.assertEqual(0.0, result["efficiency_metrics"]["billing_points"])
        self.assertEqual(1, result["risk_metrics"]["failed_tasks"])
        today = _datetime.datetime.fromtimestamp(
            NOW - 100, _datetime.timezone(_datetime.timedelta(hours=8)),
        ).date().isoformat()
        trend = {item["day"]: item for item in result["trend"]}
        self.assertEqual(1, trend[today]["tasks_created"])
        self.assertEqual(1, trend[today]["tasks_failed"])
        inspector = next(item for item in result["employees"] if item["idx"] == 10)
        self.assertEqual(1, inspector["tasks"])
        self.assertEqual(1, inspector["failed"])
        self.assertEqual(1, inspector["refunded"])
        self.assertEqual(1.0, inspector["refunded_points"])
        self.assertEqual(321, inspector["tokens"])
        self.assertEqual(0.75, inspector["cost_usd"])

    def test_employee_detail_is_scoped_structural_and_page_limited(self):
        own_ids = [
            self._task(status="done", created_at=NOW - 100 - offset, updated_at=NOW - offset)
            for offset in (1, 2, 3)
        ]
        self._task(tenant_id=3, employee=self.restaurant_employee, status="done")
        self._task(tenant_id=2, employee=self.auto_employee, status="done")
        self._visit()

        detail = bossdashboard.employee_detail(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            employee_idx=self.restaurant_employee["idx"],
            limit=2,
            offset=0,
            now=NOW,
        )
        self.assertEqual(3, detail["tasks"]["total"])
        self.assertEqual(2, len(detail["tasks"]["items"]))
        self.assertTrue(detail["can_open_records"])
        self.assertTrue(
            all(item["target_route"] for item in detail["tasks"]["items"])
        )
        self.assertEqual(set(own_ids[:2]), {item["id"] for item in detail["tasks"]["items"]})
        self.assertEqual(0, detail["inspection_visits"]["total"])
        self.assertEqual([], detail["inspection_visits"]["items"])
        self.assertNotIn("CANARY", json.dumps(detail, ensure_ascii=False))

        # 巡店经理通过 visit.industry_key 归属当前行业；普通
        # task.emp_idx=10 没有这个信任锺，不得混入。
        self._task(
            tenant_id=2,
            employee={"idx": 10},
            status="done",
            body="不得归属行业的普通 task-CANARY",
        )
        inspection_detail = bossdashboard.employee_detail(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            employee_idx=10,
            limit=20,
            now=NOW,
        )
        self.assertEqual("inspection", inspection_detail["employee"]["employee_kind"])
        self.assertEqual(0, inspection_detail["tasks"]["total"])
        self.assertEqual([], inspection_detail["tasks"]["items"])
        self.assertEqual(1, inspection_detail["inspection_visits"]["total"])
        self.assertEqual(
            "#/inspections/1/restaurant",
            inspection_detail["inspection_visits"]["items"][0]["target_route"],
        )

        with self.assertRaises(bossdashboard.DashboardAccessDenied):
            bossdashboard.employee_detail(
                self.owner,
                tenant_id=2,
                industry_key="restaurant",
                employee_idx=self.auto_employee["idx"],
            )
        for limit, offset in ((0, 0), (101, 0), (20, -1), (20, 100_001)):
            with self.subTest(limit=limit, offset=offset):
                with self.assertRaises(bossdashboard.DashboardValidationError):
                    bossdashboard.employee_detail(
                        self.owner,
                        tenant_id=2,
                        industry_key="restaurant",
                        employee_idx=self.restaurant_employee["idx"],
                        limit=limit,
                        offset=offset,
                    )

    def test_days_boundary_is_bounded(self):
        for days in (0, 91, "30", True):
            with self.subTest(days=days):
                with self.assertRaises(bossdashboard.DashboardValidationError):
                    bossdashboard.summary(
                        self.owner,
                        tenant_id=2,
                        industry_key="restaurant",
                        days=days,
                        now=NOW,
                    )

    def test_employee_detail_inherits_period_and_business_metrics_are_industry_specific(self):
        recent_id = self._task(
            status="done", created_at=NOW - 2 * 86400, updated_at=NOW - 100,
        )
        self._task(
            status="done", created_at=NOW - 20 * 86400, updated_at=NOW - 19 * 86400,
        )
        detail = bossdashboard.employee_detail(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            employee_idx=self.restaurant_employee["idx"],
            days=7,
            now=NOW,
        )
        self.assertEqual(7, detail["period"]["days"])
        self.assertEqual(1, detail["tasks"]["total"])
        self.assertEqual([recent_id], [item["id"] for item in detail["tasks"]["items"]])

        restaurant = bossdashboard.summary(
            self.owner, tenant_id=2, industry_key="restaurant", now=NOW,
        )["business_metrics"]
        auto = bossdashboard.summary(
            self.root, is_boss=True, tenant_id=3, industry_key="auto", now=NOW,
        )["business_metrics"]
        restaurant_labels = {item["label"] for item in restaurant["metrics"]}
        auto_labels = {item["label"] for item in auto["metrics"]}
        self.assertIn("人工成本率", restaurant_labels)
        self.assertIn("翻台率", restaurant_labels)
        self.assertIn("工位产能", auto_labels)
        self.assertNotEqual(restaurant_labels, auto_labels)
        self.assertTrue(
            all(
                metric["value"] is None
                and metric["availability"] is False
                and metric["source_required"]
                for metric in restaurant["metrics"] + auto["metrics"]
            )
        )

        with self.assertRaises(bossdashboard.DashboardValidationError):
            bossdashboard.employee_detail(
                self.owner,
                tenant_id=2,
                industry_key="restaurant",
                employee_idx=self.restaurant_employee["idx"],
                days=0,
                now=NOW,
            )

    def test_refunded_points_are_not_spend_and_refunds_are_reported(self):
        self._task(status="done", billing_status="succeeded", billing_points=3)
        self._task(status="running", billing_status="charged", billing_points=2)
        self._task(status="failed", billing_status="refunded", billing_points=7)
        self._task(status="done", billing_status="included", billing_points=11)
        result = bossdashboard.summary(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            days=30,
            now=NOW,
        )
        self.assertEqual(1, result["task_metrics"]["refunded"])
        self.assertEqual(7.0, result["task_metrics"]["refunded_points"])
        self.assertEqual(5.0, result["efficiency_metrics"]["billing_points"])
        employee = next(
            item for item in result["employees"]
            if item["idx"] == self.restaurant_employee["idx"]
        )
        self.assertEqual(1, employee["refunded"])
        self.assertEqual(7.0, employee["refunded_points"])
        self.assertEqual(5.0, employee["billing_points"])

    def test_zero_denominator_completion_rates_are_unknown_not_fake_zero(self):
        result = bossdashboard.summary(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            days=30,
            now=NOW,
        )
        self.assertIsNone(result["task_metrics"]["completion_rate"])
        self.assertIsNone(result["inspection_metrics"]["completion_rate"])
        normal_employee = next(
            item for item in result["employees"]
            if item["idx"] == self.restaurant_employee["idx"]
        )
        inspection_employee = next(
            item for item in result["employees"] if item["idx"] == 10
        )
        self.assertIsNone(normal_employee["completion_rate"])
        self.assertIsNone(inspection_employee["completion_rate"])

    def test_terminal_at_survives_adoption_edit_delete_and_restore_without_new_delivery(self):
        old_terminal = NOW - 60 * 86400
        adopted_id = self._task(
            status="done",
            created_at=old_terminal - 300,
            updated_at=old_terminal,
            terminal_at=old_terminal,
        )
        restored_id = self._task(
            status="done",
            created_at=old_terminal - 200,
            updated_at=old_terminal,
            terminal_at=old_terminal,
        )

        # Lazy thread adoption, a legacy standalone edit, and trash restore all
        # update generic mutation time.  None may rewrite the delivery clock.
        taskthreads.ensure_thread(adopted_id, 2, self.owner["id"], now=NOW - 20)
        db.execute(
            "UPDATE task SET output_md='edited',updated_at=? WHERE id=?",
            (NOW - 15, adopted_id),
        )
        taskthreads.soft_delete_task(
            restored_id, 2, actor_id=self.owner["id"], now=NOW - 10,
        )
        taskthreads.restore_task(restored_id, 2, now=NOW - 5)

        rows = db.q(
            "SELECT id,terminal_at,updated_at FROM task WHERE id IN (?,?) ORDER BY id",
            (adopted_id, restored_id),
        )
        self.assertEqual(
            [old_terminal, old_terminal], [row["terminal_at"] for row in rows]
        )
        self.assertTrue(all(row["updated_at"] > old_terminal for row in rows))

        result = bossdashboard.summary(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            days=7,
            now=NOW,
        )
        self.assertEqual(0, result["task_metrics"]["completed"])
        self.assertEqual([], [
            item for item in result["recent_activity"] if item["kind"] == "task"
        ])
        detail = bossdashboard.employee_detail(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            employee_idx=self.restaurant_employee["idx"],
            days=7,
            now=NOW,
        )
        self.assertEqual(0, detail["tasks"]["total"])

    def test_schema51_backfills_legacy_terminal_and_refund_clocks_exactly_once(self):
        old_terminal = NOW - 70 * 86400
        task_id = self._task(
            status="done",
            created_at=old_terminal - 500,
            updated_at=old_terminal,
            terminal_at=old_terminal,
        )
        refund_id = self._task(
            status="failed",
            billing_status="refunded",
            billing_points=7,
            created_at=old_terminal - 400,
            updated_at=old_terminal + 10,
            terminal_at=old_terminal,
            refunded_at=old_terminal + 10,
        )
        self._visit()
        connection = db.conn()
        connection.execute("DROP INDEX IF EXISTS idx_task_dashboard_terminal")
        connection.execute("DROP INDEX IF EXISTS idx_task_dashboard_refunded")
        connection.execute("DROP INDEX IF EXISTS idx_inspection_visit_terminal")
        connection.execute("ALTER TABLE task DROP COLUMN terminal_at")
        connection.execute("ALTER TABLE task DROP COLUMN refunded_at")
        connection.execute("ALTER TABLE inspection_visit DROP COLUMN terminal_at")
        connection.commit()

        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.conn()
        migrated = db.one(
            "SELECT terminal_at,refunded_at,updated_at FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual(old_terminal, migrated["terminal_at"])
        migrated_refund = db.one(
            "SELECT terminal_at,refunded_at,updated_at FROM task WHERE id=?",
            (refund_id,),
        )
        self.assertEqual(old_terminal + 10, migrated_refund["terminal_at"])
        self.assertEqual(old_terminal + 10, migrated_refund["refunded_at"])
        migrated_visit = db.one(
            "SELECT terminal_at,completed_at,updated_at FROM inspection_visit WHERE id=1"
        )
        self.assertEqual(NOW - 4000, migrated_visit["terminal_at"])

        db.execute(
            "UPDATE task SET updated_at=? WHERE id IN (?,?)",
            (NOW - 1, task_id, refund_id),
        )
        db.execute("UPDATE inspection_visit SET updated_at=? WHERE id=1", (NOW - 1,))
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.conn()
        reopened = db.one(
            "SELECT terminal_at,refunded_at,updated_at FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual(old_terminal, reopened["terminal_at"])
        self.assertEqual(NOW - 1, reopened["updated_at"])
        reopened_refund = db.one(
            "SELECT terminal_at,refunded_at,updated_at FROM task WHERE id=?",
            (refund_id,),
        )
        self.assertEqual(old_terminal + 10, reopened_refund["terminal_at"])
        self.assertEqual(old_terminal + 10, reopened_refund["refunded_at"])
        self.assertEqual(NOW - 1, reopened_refund["updated_at"])
        reopened_visit = db.one(
            "SELECT terminal_at,completed_at,updated_at FROM inspection_visit WHERE id=1"
        )
        self.assertEqual(NOW - 4000, reopened_visit["terminal_at"])
        self.assertEqual(NOW - 1, reopened_visit["updated_at"])

    def test_free_retry_clears_terminal_clock_and_failure_sets_a_new_one(self):
        task_id = self._task(
            status="failed",
            billing_status="included",
            created_at=NOW - 200,
            updated_at=NOW - 100,
            terminal_at=NOW - 100,
        )
        with patch.object(taskrunner.time, "time", return_value=NOW - 50):
            self.assertTrue(taskrunner.prepare_retry(task_id, 2))
        queued = db.one(
            "SELECT status,terminal_at FROM task WHERE id=?", (task_id,)
        )
        self.assertEqual("queued", queued["status"])
        self.assertIsNone(queued["terminal_at"])

        with patch.object(taskrunner.time, "time", return_value=NOW - 25):
            self.assertTrue(taskrunner.settle_failure(task_id, "再次失败"))
        failed = db.one(
            "SELECT status,terminal_at,updated_at FROM task WHERE id=?", (task_id,)
        )
        self.assertEqual("failed", failed["status"])
        self.assertEqual(NOW - 25, failed["terminal_at"])
        self.assertEqual(NOW - 25, failed["updated_at"])

    def test_late_refund_keeps_its_own_period_after_included_retry_succeeds(self):
        old_terminal = NOW - 40 * 86400
        task_id = self._task(
            status="failed",
            billing_status="charged",
            billing_points=7,
            created_at=old_terminal - 100,
            updated_at=old_terminal,
            terminal_at=old_terminal,
            refunded_at=None,
        )
        with patch.object(taskrunner.time, "time", return_value=NOW - 20):
            self.assertTrue(taskrunner.settle_failure(task_id, "延迟退款"))
        settled = db.one(
            "SELECT status,billing_status,terminal_at,refunded_at FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual("failed", settled["status"])
        self.assertEqual("refunded", settled["billing_status"])
        self.assertEqual(old_terminal, settled["terminal_at"])
        self.assertEqual(NOW - 20, settled["refunded_at"])

        with patch.object(taskrunner.time, "time", return_value=NOW - 10):
            self.assertTrue(taskrunner.prepare_retry(task_id, 2))
        retried = db.one(
            "SELECT billing_status,terminal_at,refunded_at FROM task WHERE id=?",
            (task_id,),
        )
        self.assertEqual("included", retried["billing_status"])
        self.assertIsNone(retried["terminal_at"])
        self.assertEqual(NOW - 20, retried["refunded_at"])
        db.execute(
            "UPDATE task SET status='done',terminal_at=?,updated_at=? WHERE id=?",
            (NOW - 5, NOW - 5, task_id),
        )

        result = bossdashboard.summary(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            days=7,
            now=NOW,
        )
        self.assertEqual(1, result["task_metrics"]["completed"])
        self.assertEqual(1, result["task_metrics"]["refunded"])
        self.assertEqual(7.0, result["task_metrics"]["refunded_points"])
        employee = next(
            item for item in result["employees"]
            if item["idx"] == self.restaurant_employee["idx"]
        )
        self.assertEqual(1, employee["refunded"])
        self.assertEqual(7.0, employee["refunded_points"])

    def test_terminal_throughput_uses_terminal_time_and_old_open_work_stays_in_risk_backlog(self):
        terminal_day = _datetime.datetime.fromtimestamp(
            NOW - 100, _datetime.timezone(_datetime.timedelta(hours=8)),
        ).date().isoformat()
        completed_id = self._task(
            status="done",
            created_at=NOW - 40 * 86400,
            updated_at=NOW - 100,
        )
        refunded_id = self._task(
            status="failed",
            billing_status="refunded",
            billing_points=7,
            created_at=NOW - 45 * 86400,
            updated_at=NOW - 80,
        )
        self._task(
            status="running",
            created_at=NOW - 60 * 86400,
            updated_at=NOW - 50 * 86400,
        )
        self._task(
            status="awaiting_review",
            created_at=NOW - 50 * 86400,
            updated_at=NOW - 49 * 86400,
        )
        result = bossdashboard.summary(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            days=7,
            now=NOW,
        )
        self.assertEqual(0, result["task_metrics"]["total"])
        self.assertEqual(1, result["task_metrics"]["completed"])
        self.assertEqual(1, result["task_metrics"]["failed"])
        self.assertEqual(1, result["task_metrics"]["refunded"])
        self.assertEqual(7.0, result["task_metrics"]["refunded_points"])
        self.assertEqual(0, result["task_metrics"]["cohort_completed"])
        self.assertEqual(1, result["risk_metrics"]["stale_active_tasks"])
        self.assertEqual(1, result["risk_metrics"]["waiting_for_decision"])
        trend = {item["day"]: item for item in result["trend"]}
        self.assertEqual(1, trend[terminal_day]["tasks_completed"])
        self.assertEqual(0, trend[terminal_day]["tasks_created"])
        self.assertTrue(
            any(
                item["kind"] == "task" and item["status_group"] == "completed"
                for item in result["recent_activity"]
            )
        )
        detail = bossdashboard.employee_detail(
            self.owner,
            tenant_id=2,
            industry_key="restaurant",
            employee_idx=self.restaurant_employee["idx"],
            days=7,
            now=NOW,
        )
        self.assertEqual(2, detail["tasks"]["total"])
        self.assertEqual(
            {completed_id, refunded_id},
            {item["id"] for item in detail["tasks"]["items"]},
        )
        self.assertTrue(
            all(item["period_event_kind"] in {"completed", "failed"}
                for item in detail["tasks"]["items"])
        )

    def test_summary_query_count_is_constant_at_scale(self):
        connection = db.conn()
        identity = employeeidentity.task_fields(self.restaurant_employee)
        connection.executemany(
            "INSERT INTO task("
            "tenant_id,emp_idx,employee_key,employee_catalog_version,"
            "employee_name_snapshot,employee_dept_key,employee_spec_sha256,"
            "brief_json,status,tokens,cost_usd,"
            "created_at,updated_at,terminal_at"
            ") VALUES(2,?,?,?,?,?,?,'{}',?,10,0.01,?,?,?)",
            (
                (
                    self.restaurant_employee["idx"],
                    identity["employee_key"],
                    identity["employee_catalog_version"],
                    identity["employee_name_snapshot"],
                    identity["employee_dept_key"],
                    identity["employee_spec_sha256"],
                    "done" if index % 2 else "running",
                    NOW - 10_000 + index,
                    NOW - 9_000 + index,
                    NOW - 9_000 + index if index % 2 else None,
                )
                for index in range(2_000)
            ),
        )
        connection.commit()

        query_count = 0
        real_q = db.q

        def counted_q(sql, args=()):
            nonlocal query_count
            query_count += 1
            return real_q(sql, args)

        with patch.object(db, "q", side_effect=counted_q):
            result = bossdashboard.summary(
                self.owner,
                tenant_id=2,
                industry_key="restaurant",
                now=NOW,
            )

        self.assertEqual(2_000, result["task_metrics"]["total"])
        self.assertEqual(
            len(
                [
                    employee
                    for employee in departments.specialists().values()
                    if employee["dept_key"] == "restaurant"
                ]
            ),
            len(result["employees"]) - 1,
        )
        # 历史冻结身份目录占用 1 条固定查询；仍与任务量、
        # active/legacy 员工数无关，不得退化成 N+1。
        self.assertLessEqual(query_count, 21, "看板不得按任务或员工 N+1 查询")


if __name__ == "__main__":
    unittest.main()
