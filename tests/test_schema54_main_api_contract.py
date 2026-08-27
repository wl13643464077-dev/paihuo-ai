"""Schema 55 public API contract for person and immutable role identity."""

import inspect
import unittest
from unittest import mock

from fastapi import HTTPException

from app import main


class Schema54MainApiSourceContractTests(unittest.TestCase):
    def test_public_identity_contract_has_two_axes_and_permissions(self):
        source = inspect.getsource(main._employee_public_contract)
        fields = (
            "person_status",
            "identity_status",
            "identity_ref",
            "config_revision",
            "can_assign_new",
            "can_continue",
            "can_learn",
        )
        for field in fields:
            self.assertIn(field, main._EMPLOYEE_IDENTITY_PUBLIC_FIELDS)
        self.assertIn("employeeidentity.identity_view", source)
        self.assertIn("_EMPLOYEE_IDENTITY_PUBLIC_FIELDS", source)

    def test_old_idx_keyed_employee_config_is_not_read_by_main(self):
        for endpoint in (main.settings_get, main.admin_overview):
            source = inspect.getsource(endpoint)
            self.assertNotIn("FROM employee_config", source)
            self.assertNotIn("JOIN employee_config", source)

    def test_employee_task_meeting_dashboard_and_production_publish_contract(self):
        blocks = (
            main._public_expert,
            main.depts_list,
            main.dept_emp,
            main.task_get,
            main._meeting_member_view,
            main._meeting_history_member_views,
            main.boss_dashboard_summary,
            main.boss_dashboard_employee,
            main.boss_production,
            main.boss_production_detail,
        )
        joined = "\n".join(inspect.getsource(block) for block in blocks)
        for field in (
            "person_status",
            "identity_status",
            "identity_ref",
            "config_revision",
            "can_assign_new",
            "can_continue",
            "can_learn",
        ):
            self.assertIn(field, joined)

    def test_task_detail_exposes_frozen_professional_profile(self):
        source = inspect.getsource(main.task_get)
        self.assertIn("professional_profile", source)
        self.assertIn("employee_identity_ref", source)
        self.assertIn("employee_config_revision", source)
        self.assertNotIn("employees.get_config(int(t.get(\"emp_idx\")", source)

    def test_current_role_detail_exposes_profile_to_authorized_non_tour_users(self):
        source = inspect.getsource(main.dept_emp)
        self.assertIn('show_profile = (auth.current() or {}).get("role") != "tour"', source)
        self.assertIn("include_profile=show_profile", source)

    def test_same_idx_aggregates_use_identity_ref_not_idx_alone(self):
        depts = inspect.getsource(main.depts_list)
        where = inspect.getsource(main._employee_task_where)
        production = inspect.getsource(main.boss_production)
        production_detail = inspect.getsource(main.boss_production_detail)
        self.assertIn("employee_identity_ref", depts)
        self.assertIn("employee_identity_ref", where)
        self.assertNotIn("employee_config_revision", where)
        self.assertIn("employee_identity_ref", production)
        self.assertIn("employee_config_revision", production)
        self.assertIn('task_sql += " AND employee_identity_ref=?"', production_detail)
        self.assertNotIn('AND employee_config_revision=?', production_detail)

    def test_public_contract_keeps_person_and_role_axes_independent(self):
        view = {
            "person_status": "active",
            "identity_status": "historical",
            "identity_ref": "a" * 64,
            "config_revision": 3,
            "config_sha256": "b" * 64,
            "can_assign_new": False,
            "can_continue": True,
            "can_learn": False,
            "slot_row_version": 8,
            "role_profile_summary": {"has_profile": True},
        }
        with mock.patch.object(main.employeeidentity, "identity_view", return_value=view):
            result = main._employee_public_contract({"idx": 1001})
        self.assertEqual("active", result["person_status"])
        self.assertEqual("historical", result["identity_status"])
        self.assertFalse(result["can_assign_new"])
        self.assertTrue(result["can_continue"])
        self.assertFalse(result["can_learn"])

    def test_mutation_binding_rejects_reused_idx_with_a_different_identity(self):
        employee = {"idx": 1001}
        config = {
            "identity_ref": "a" * 64,
            "config_revision": 4,
            "config_sha256": "b" * 64,
            "bundle_sha256": "d" * 64,
        }
        identity = {
            "person_status": "active", "identity_status": "current",
            "identity_ref": "a" * 64, "config_revision": 4,
            "config_sha256": "b" * 64, "can_assign_new": True,
            "can_continue": True, "can_learn": True,
        }
        with (
            mock.patch.object(main.employeeidentity, "active_employee", return_value=employee),
            mock.patch.object(main.employees, "get_config", return_value=config),
            mock.patch.object(main, "_employee_public_contract", return_value=identity),
        ):
            with self.assertRaises(HTTPException) as caught:
                main._employee_current_write_binding(
                    1001,
                    {
                        "identity_ref": "c" * 64,
                        "config_revision": 4,
                        "config_sha256": "b" * 64,
                        "bundle_sha256": "d" * 64,
                    },
                )
        self.assertEqual(409, caught.exception.status_code)

    def test_mutation_binding_requires_complete_role_bundle_quadruple(self):
        employee = {"idx": 1001}
        config = {
            "identity_ref": "a" * 64,
            "config_revision": 4,
            "config_sha256": "b" * 64,
            "bundle_sha256": "d" * 64,
        }
        identity = {
            "identity_ref": "a" * 64, "config_revision": 4,
            "config_sha256": "b" * 64, "can_assign_new": True,
            "bundle_sha256": "d" * 64,
        }
        with (
            mock.patch.object(main.employeeidentity, "active_employee", return_value=employee),
            mock.patch.object(main.employees, "get_config", return_value=config),
            mock.patch.object(main, "_employee_public_contract", return_value=identity),
        ):
            for body in (
                {},
                {"identity_ref": "a" * 64},
                {"config_revision": 4, "config_sha256": "b" * 64},
            ):
                with self.subTest(body=body):
                    with self.assertRaises(HTTPException) as caught:
                        main._employee_current_write_binding(1001, body)
                    self.assertEqual(400, caught.exception.status_code)
            accepted = main._employee_current_write_binding(1001, config)
        self.assertEqual(config, accepted["config"])

    def test_meeting_member_bindings_are_exact_one_to_one(self):
        def binding(idx, _body):
            return {
                "employee": {"idx": idx},
                "config": {"idx": idx},
                "identity": {"can_assign_new": True},
            }

        good = [
                {"idx": 0, "identity_ref": "a" * 64,
             "config_revision": 1, "config_sha256": "b" * 64,
             "bundle_sha256": "e" * 64},
            {"idx": 1, "identity_ref": "c" * 64,
             "config_revision": 2, "config_sha256": "d" * 64,
             "bundle_sha256": "f" * 64},
        ]
        with mock.patch.object(
            main, "_employee_current_write_binding", side_effect=binding,
        ):
            ordered = main._meeting_current_write_bindings([0, 1], good)
            self.assertEqual([0, 1], [item["employee"]["idx"] for item in ordered])
            malformed = (
                ([0, 1], good[:1]),
                ([0, 1], good + [{**good[0], "idx": 2}]),
                ([0, 1], [good[0], {**good[1], "idx": 0}]),
                ([0, 0], good),
                ([0, 1], [{**good[0], "config_sha256": ""}, good[1]]),
            )
            for idxs, bindings in malformed:
                with self.subTest(idxs=idxs, bindings=bindings):
                    with self.assertRaises(HTTPException) as caught:
                        main._meeting_current_write_bindings(idxs, bindings)
                    self.assertIn(caught.exception.status_code, {400, 409})

    def test_enabled_mutation_requires_slot_row_version(self):
        body = {
            "enabled": False,
            "identity_ref": "a" * 64,
            "config_revision": 1,
            "config_sha256": "b" * 64,
        }
        binding = {
            "employee": {"idx": 1001},
            "config": body,
            "identity": {"identity_ref": "a" * 64},
        }
        with (
            mock.patch.object(main, "_need_boss"),
            mock.patch.object(main, "_is_emp", return_value=True),
            mock.patch.object(main, "_employee_current_write_binding", return_value=binding),
            mock.patch.object(main.employees, "set_enabled") as changed,
        ):
            with self.assertRaises(HTTPException) as caught:
                main.admin_emp_enabled(1001, body)
        self.assertEqual(400, caught.exception.status_code)
        changed.assert_not_called()

    def test_followup_binding_requires_complete_exact_frozen_quadruple(self):
        task = {
            "employee_identity_ref": "a" * 64,
            "employee_config_revision": 7,
            "employee_config_sha256": "b" * 64,
            "bundle_sha256": "d" * 64,
        }
        for body in (
            {},
            {"identity_ref": "a" * 64},
            {"identity_ref": "a" * 64, "config_revision": 7},
        ):
            with self.subTest(body=body):
                with self.assertRaises(HTTPException) as caught:
                    main._require_frozen_task_write_binding(body, task)
                self.assertEqual(400, caught.exception.status_code)
        with self.assertRaises(HTTPException) as caught:
            main._require_frozen_task_write_binding({
                "identity_ref": "a" * 64,
                "config_revision": 7,
                "config_sha256": "c" * 64,
                "bundle_sha256": "d" * 64,
            }, task)
        self.assertEqual(409, caught.exception.status_code)
        main._require_frozen_task_write_binding({
            "identity_ref": "a" * 64,
            "config_revision": 7,
            "config_sha256": "b" * 64,
            "bundle_sha256": "d" * 64,
        }, task)

    def test_followup_creation_keeps_the_tasks_exact_frozen_config_revision(self):
        employee = {"idx": 1001}
        config = {
            "identity_ref": "a" * 64,
            "config_revision": 2,
            "config_sha256": "b" * 64,
            "bundle_sha256": "d" * 64,
        }
        frozen = {
            "employee_key": "auto.role.v1",
            "employee_catalog_version": "2026.05.v1",
            "employee_name_snapshot": "冻结岗位",
            "employee_dept_key": "auto",
            "employee_spec_sha256": "c" * 64,
            "employee_identity_ref": "a" * 64,
            "employee_config_revision": 2,
            "employee_config_sha256": "b" * 64,
            "bundle_sha256": "d" * 64,
            "identity_scheme": "legacy-six",
        }
        task_data = {
            "emp_idx": 1001,
            **frozen,
            "tenant_id": 1,
            "brief_json": "{}",
        }
        with (
            mock.patch.object(
                main.employeeidentity,
                "resolve_task_binding",
                return_value={"employee": employee, "config": config},
            ),
            mock.patch.object(
                main.employeeidentity, "task_fields", return_value=frozen,
            ) as task_fields,
            mock.patch.object(main.db, "insert", return_value=41) as insert,
            mock.patch.object(main.billing, "charge_if_claimed", return_value=True),
        ):
            task_id = main._create_charged_expert_task(task_data)

        self.assertEqual(41, task_id)
        task_fields.assert_called_once_with(employee, config=config)
        inserted = insert.call_args.args[1]
        self.assertEqual(2, inserted["employee_config_revision"])
        self.assertEqual("b" * 64, inserted["employee_config_sha256"])

    def test_production_card_merges_config_revisions_of_one_role_identity(self):
        rows = [
            {
                "idx": 1001,
                "employee_config_revision": revision,
                "n": revision,
                "tk": 10 * revision,
                "cost": float(revision),
                "last": float(revision),
            }
            for revision in (1, 2)
        ]
        public_base = {
            "idx": 1001,
            "name": "当前岗位",
            "dept": "汽车行业部",
            "dept_key": "auto",
            "catalog_version": "2026.08.v3",
            "person_status": "active",
            "identity_status": "current",
            "identity_ref": "a" * 64,
            "can_assign_new": True,
            "can_continue": True,
            "can_learn": True,
            "role_profile_summary": {"has_profile": True},
            "roster_status": "active",
            "can_assign": True,
        }

        def identity(row):
            revision = int(row["employee_config_revision"])
            return (
                (1001, "a" * 64, revision),
                {
                    **public_base,
                    "config_revision": revision,
                    "config_sha256": str(revision) * 64,
                },
            )

        with (
            mock.patch.object(main, "_need_admin"),
            mock.patch.object(main, "TEN", return_value=1),
            mock.patch.object(main.db, "q", side_effect=[[], rows]),
            mock.patch.object(main, "_production_identity", side_effect=identity),
            mock.patch.object(main, "_production_identity_visible", return_value=True),
        ):
            result = main.boss_production()

        self.assertEqual(1, len(result["employees"]))
        self.assertEqual(3, result["employees"][0]["runs"])
        self.assertEqual(2, result["employees"][0]["config_revision"])


if __name__ == "__main__":
    unittest.main()
