"""Schema55 batch learning API and orchestration contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import httpx
from fastapi import HTTPException

from app import auth, billing, db, departments, employeelearning, employees, main
from tests.test_schema55_main_learning_api import _gate_config_for_employees


class Schema55LearningBatchApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "schema55-learning-batch.db")
        departments.reset_cache()
        main._LEARNING_BATCH_COORDINATORS.clear()
        main._LEARNING_BATCH_ACTIVE_RUNS.clear()
        db.conn()
        auth.set_current({
            "id": 1,
            "tenant_id": 1,
            "role": "root",
            "username": "boss",
            "modules": ["*"],
        })
        gate = _gate_config_for_employees(main._learning_batch_v4_employees())
        self.gate_patch = mock.patch.object(
            main, "_learning_evidence_config", return_value=gate,
        )
        self.gate_patch.start()

    def tearDown(self):
        self.gate_patch.stop()
        auth.set_current(None)
        main._LEARNING_BATCH_COORDINATORS.clear()
        main._LEARNING_BATCH_ACTIVE_RUNS.clear()
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        departments.reset_cache()
        self.tmp.cleanup()

    @staticmethod
    def _verified_sources() -> list[dict]:
        rows = []
        for index in range(1, 6):
            rows.append({
                "url": f"https://evidence{((index - 1) % 3) + 1}.example/rule/{index}",
                "title": f"十五分钟需求预测与置信区间 {index}",
                "publisher": f"公开机构 {index}",
                "authority_level": "official" if index == 1 else "industry",
                "published_at": "2026-08-01",
                "fetched_at": time.time(),
                "http_status": 200,
                "tls_valid": True,
                "content_sha256": hashlib.sha256(
                    f"batch-source-{index}".encode()
                ).hexdigest(),
                "excerpt": (
                    "茶咖现制饮品POS订单到达的下一营业日各十五分钟需求分位情景、置信区间、"
                    f"节假日天气事件的可核验方法 {index}"
                ),
                "capture_event_id": f"batch-websearch-{index}",
                "capture_provider": "websearch",
            })
        return rows

    def test_dry_run_defaults_to_exact_current_v4_360_without_creating_batch(self):
        body = {
            "request_key": "schema55-batch-all-v4-001",
            "max_concurrency": 4,
        }
        result = asyncio.run(main.employee_learning_batch_dry_run(body))
        preview = result["preview"]
        self.assertEqual(360, preview["target_count"])
        self.assertEqual(1080.0, preview["budget_cap_points"])
        self.assertEqual("platform_included", preview["billing_mode"])
        self.assertEqual(0.0, preview["wallet_charge_points"])
        self.assertEqual(3.0, preview["points_per_employee"])
        self.assertEqual(4, preview["max_concurrency"])
        self.assertEqual(10, len(preview["industry_counts"]))
        self.assertEqual(360, sum(preview["industry_counts"].values()))
        self.assertFalse(preview["auto_approve"])
        self.assertEqual(64, len(preview["target_digest"]))
        self.assertEqual(64, len(preview["preview_token"]))
        internal = main._learning_batch_preview_contract(body, tenant_id=1)
        sample_fields = (
            "idx", "person", "name", "industry_key", "config_revision",
            "identity_ref", "config_sha256", "bundle_sha256",
        )
        self.assertEqual(
            {
                key: internal["_targets"][0][key]
                for key in sample_fields
            },
            preview["target_sample"][0],
        )
        self.assertEqual(
            preview["target_digest"],
            hashlib.sha256(json.dumps(
                [
                    {
                        key: row[key]
                        for key in (
                            "idx", "industry_key", "identity_ref",
                            "config_revision", "config_sha256",
                            "bundle_sha256", "high_risk",
                        )
                    }
                    for row in internal["_targets"]
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_batch"
        )["n"])

    def test_execute_requires_preview_confirmation_and_freezes_every_run_tuple(self):
        body = {
            "request_key": "schema55-batch-two-001",
            "idxs": [1001, 1002],
            "max_concurrency": 2,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        with self.assertRaises(HTTPException) as missing_confirmation:
            asyncio.run(main.employee_learning_batch_create({
                **body,
                "preview_token": preview["preview_token"],
                "budget_cap_points": 6,
            }))
        self.assertEqual(400, missing_confirmation.exception.status_code)

        with mock.patch.object(
            main, "_schedule_employee_learning_batch", return_value=True,
        ) as schedule:
            created = asyncio.run(main.employee_learning_batch_create({
                **body,
                "preview_token": preview["preview_token"],
                "budget_cap_points": 6,
                "confirm_execute": True,
                "auto_approve": False,
            }))
        batch = created["batch"]
        self.assertTrue(created["started"])
        self.assertEqual(2, batch["target_count"])
        self.assertEqual(6.0, batch["budget_cap_points"])
        self.assertEqual(2, batch["counts"]["queued"])
        self.assertEqual(0, batch["counts"]["pending_review"])
        self.assertFalse(batch["auto_approve"])
        schedule.assert_called_once_with(batch["id"])

        runs = employeelearning.list_batch_runs(batch["id"])
        self.assertEqual(2, len(runs))
        for run in runs:
            checkpoint = main._learning_run_checkpoint(run)
            self.assertEqual(run["identity_ref"], checkpoint["identity_ref"])
            self.assertEqual(
                run["base_config_revision"], checkpoint["config_revision"]
            )
            self.assertEqual(
                run["base_config_sha256"], checkpoint["config_sha256"]
            )
            self.assertRegex(checkpoint["expected_bundle_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual("queued", checkpoint["stage"])
            self.assertEqual(3.0, run["budget_points"])
            self.assertEqual(0.0, run["spent_points"])
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_operation"
        )["n"])

    def test_create_list_and_detail_persist_wallet_audit_fields_across_restart(self):
        body = {
            "request_key": "schema55-batch-wallet-audit-001",
            "idxs": [1001, 1002],
            "max_concurrency": 2,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        with mock.patch.object(
            main, "_schedule_employee_learning_batch", return_value=True,
        ):
            created = asyncio.run(main.employee_learning_batch_create({
                **body,
                "preview_token": preview["preview_token"],
                "budget_cap_points": 6,
                "confirm_execute": True,
            }))

        expected = {
            "billing_mode": "platform_included",
            "wallet_charge_points": 0.0,
            "points_per_employee": 3.0,
            "target_digest": preview["target_digest"],
        }
        self.assertEqual(expected, {
            key: created["batch"][key] for key in expected
        })
        self.assertEqual("verified", created["batch"]["billing_proof_status"])
        self.assertNotIn("preview_token", created["batch"])
        metadata = db.jloads(db.one(
            "SELECT metadata_json FROM employee_learning_batch WHERE id=?",
            (created["batch"]["id"],),
        )["metadata_json"], {})
        self.assertEqual(expected, {
            key: metadata[key] for key in expected
        })

        # Coordinators are process-local. Clearing them models a restart while
        # the durable public audit contract must continue to come from DB.
        main._LEARNING_BATCH_COORDINATORS.clear()
        listed = main.employee_learning_batches_list()["batches"][0]
        detailed = main.employee_learning_batch_get(
            created["batch"]["id"]
        )["batch"]
        for public in (listed, detailed):
            self.assertEqual(expected, {key: public[key] for key in expected})
            self.assertEqual("verified", public["billing_proof_status"])
            self.assertNotIn("preview_token", public)

    def test_legacy_batch_without_frozen_billing_metadata_is_proof_missing(self):
        legacy = employeelearning.create_batch(
            "schema55-legacy-billing-proof-missing-001",
            budget_cap_points=3,
            tenant_id=1,
        )
        public = main._learning_batch_public(legacy)
        self.assertEqual("proof_missing", public["billing_proof_status"])
        for field in (
            "billing_mode", "wallet_charge_points", "points_per_employee",
            "target_digest",
        ):
            self.assertIsNone(public[field])
        self.assertNotIn("preview_token", public)

    def test_batch_detail_exposes_exact_frozen_person_job_and_employee_idx_for_review(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-review-list-001",
            "idxs": [1001, 1002],
            "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        runs = employeelearning.list_batch_runs(batch["id"])
        employeelearning._set_run_status(
            runs[0], employeelearning.RUN_AWAITING_APPROVAL,
        )

        public = main.employee_learning_batch_get(batch["id"])["batch"]
        expected = {row["idx"]: row for row in preview["_targets"]}
        self.assertEqual(1, public["counts"]["pending_review"])
        self.assertEqual(2, len(public["runs"]))
        for run in public["runs"]:
            frozen = expected[run["employee_idx"]]
            self.assertEqual(frozen["person"], run["person"])
            self.assertEqual(frozen["name"], run["name"])
            self.assertEqual(frozen["identity_ref"], run["identity_ref"])
            self.assertEqual(frozen["config_revision"], run["config_revision"])
            self.assertEqual(frozen["config_sha256"], run["config_sha256"])
            self.assertEqual(frozen["bundle_sha256"], run["bundle_sha256"])

    def test_execute_is_idempotent_and_rejects_budget_or_scope_drift(self):
        body = {
            "request_key": "schema55-batch-idempotent-001",
            "idxs": [1001, 1002],
            "max_concurrency": 1,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        execute = {
            **body,
            "preview_token": preview["preview_token"],
            "budget_cap_points": 6,
            "confirm_execute": True,
        }
        with mock.patch.object(
            main, "_schedule_employee_learning_batch", return_value=True,
        ):
            first = asyncio.run(main.employee_learning_batch_create(execute))
            second = asyncio.run(main.employee_learning_batch_create(execute))
        self.assertEqual(first["batch"]["id"], second["batch"]["id"])
        self.assertEqual(2, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_run"
        )["n"])

        with self.assertRaises(HTTPException) as too_much:
            asyncio.run(main.employee_learning_batch_dry_run({
                **body, "budget_cap_points": 7,
            }))
        self.assertEqual(400, too_much.exception.status_code)
        with self.assertRaises(HTTPException) as empty_scope:
            asyncio.run(main.employee_learning_batch_dry_run({
                "request_key": "schema55-batch-empty-scope-001",
                "idxs": [],
            }))
        self.assertEqual(400, empty_scope.exception.status_code)
        with self.assertRaises(HTTPException) as forged_token:
            asyncio.run(main.employee_learning_batch_create({
                **execute, "preview_token": "f" * 64,
            }))
        self.assertEqual(409, forged_token.exception.status_code)

    def test_execute_live_identity_conflict_is_409_and_leaves_no_partial_manifest(self):
        blocker_preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-live-owner-blocker-001",
            "idxs": [1002],
            "max_concurrency": 1,
        }, tenant_id=1)
        main._create_learning_batch_manifest(blocker_preview, actor_id=1)
        before_batches = db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_batch"
        )["n"]
        before_runs = db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_run"
        )["n"]

        body = {
            "request_key": "schema55-batch-live-owner-candidate-001",
            "idxs": [1001, 1002],
            "max_concurrency": 1,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        with self.assertRaises(HTTPException) as conflict:
            asyncio.run(main.employee_learning_batch_create({
                **body,
                "preview_token": preview["preview_token"],
                "budget_cap_points": 6,
                "confirm_execute": True,
            }))
        self.assertEqual(409, conflict.exception.status_code)
        self.assertEqual(before_batches, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_batch"
        )["n"])
        self.assertEqual(before_runs, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_run"
        )["n"])

    def test_execute_revalidates_every_frozen_target_inside_manifest_transaction(self):
        body = {
            "request_key": "schema55-batch-manifest-cas-barrier-001",
            "idxs": [1001],
            "max_concurrency": 1,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        execute = {
            **body,
            "preview_token": preview["preview_token"],
            "budget_cap_points": 3,
            "confirm_execute": True,
        }
        config = employees.get_config(1001)
        manifest_entered = threading.Event()
        release_manifest = threading.Event()
        real_create = main._create_learning_batch_manifest_for_schedule

        def blocked_create(*args, **kwargs):
            manifest_entered.set()
            if not release_manifest.wait(timeout=3):
                raise RuntimeError("manifest CAS barrier timed out")
            return real_create(*args, **kwargs)

        async def scenario():
            with mock.patch.object(
                main,
                "_create_learning_batch_manifest_for_schedule",
                side_effect=blocked_create,
            ):
                request = asyncio.create_task(
                    main.employee_learning_batch_create(execute)
                )
                entered = await asyncio.to_thread(manifest_entered.wait, 2)
                self.assertTrue(entered)
                await db.arun(
                    employees.set_settings_for_identity,
                    config["identity_ref"],
                    {"manifest_cas_probe": True},
                    expected_revision=config["config_revision"],
                )
                release_manifest.set()
                with self.assertRaises(HTTPException) as stale:
                    await request
                self.assertEqual(409, stale.exception.status_code)

        asyncio.run(scenario())
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_batch"
        )["n"])
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_run"
        )["n"])
        self.assertIsNone(
            employeelearning._identity_run_owner(config["identity_ref"])
        )

    def test_execute_scheduler_failure_terminalizes_only_the_new_manifest(self):
        body = {
            "request_key": "schema55-batch-create-start-failure-001",
            "idxs": [1001, 1002],
            "max_concurrency": 1,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        with mock.patch.object(
            main,
            "_schedule_employee_learning_batch",
            side_effect=RuntimeError("scheduler unavailable"),
        ), self.assertRaises(RuntimeError):
            asyncio.run(main.employee_learning_batch_create({
                **body,
                "preview_token": preview["preview_token"],
                "budget_cap_points": 6,
                "confirm_execute": True,
            }))

        batch = db.one(
            "SELECT * FROM employee_learning_batch WHERE request_key=?",
            (body["request_key"],),
        )
        self.assertIsNotNone(batch)
        self.assertEqual(employeelearning.BATCH_CANCELLED, batch["status"])
        runs = employeelearning.list_batch_runs(batch["id"])
        self.assertEqual(2, len(runs))
        self.assertTrue(all(
            run["status"] == employeelearning.RUN_CANCELLED
            and float(run["spent_points"] or 0) == 0
            for run in runs
        ))
        self.assertTrue(all(
            employeelearning._identity_run_owner(run["identity_ref"]) is None
            for run in runs
        ))

    def test_execute_scheduler_failure_pauses_but_never_deletes_idempotent_replay(self):
        body = {
            "request_key": "schema55-batch-create-replay-failure-001",
            "idxs": [1001],
            "max_concurrency": 1,
        }
        internal = main._learning_batch_preview_contract(body, tenant_id=1)
        existing = main._create_learning_batch_manifest(internal, actor_id=1)
        before_batch_count = db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_batch"
        )["n"]
        before_run_ids = [
            run["id"] for run in employeelearning.list_batch_runs(existing["id"])
        ]
        public = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]

        with mock.patch.object(
            main,
            "_schedule_employee_learning_batch",
            side_effect=RuntimeError("scheduler unavailable"),
        ), self.assertRaises(RuntimeError):
            asyncio.run(main.employee_learning_batch_create({
                **body,
                "preview_token": public["preview_token"],
                "budget_cap_points": 3,
                "confirm_execute": True,
            }))

        replay = employeelearning.get_batch(existing["id"])
        self.assertEqual(employeelearning.BATCH_PAUSED, replay["status"])
        self.assertEqual(before_batch_count, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_batch"
        )["n"])
        self.assertEqual(before_run_ids, [
            run["id"] for run in employeelearning.list_batch_runs(existing["id"])
        ])
        self.assertTrue(all(
            run["status"] == employeelearning.RUN_QUEUED
            for run in employeelearning.list_batch_runs(existing["id"])
        ))

    def test_execute_replay_generation_fences_older_scheduler_compensation(self):
        body = {
            "request_key": "schema55-batch-execute-compensation-race-001",
            "idxs": [1001], "max_concurrency": 1,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        execute = {
            **body,
            "preview_token": preview["preview_token"],
            "budget_cap_points": 3,
            "confirm_execute": True,
        }
        settle_entered = threading.Event()
        release_settle = threading.Event()
        real_settle = main._settle_unstarted_learning_batch_manifest
        schedule_calls = 0

        def schedule_once_fails(_batch_id):
            nonlocal schedule_calls
            schedule_calls += 1
            if schedule_calls == 1:
                raise RuntimeError("first scheduler unavailable")
            return True

        def blocked_settle(result):
            settle_entered.set()
            if not release_settle.wait(timeout=3):
                raise RuntimeError("manifest settlement barrier timed out")
            return real_settle(result)

        async def scenario():
            with (
                mock.patch.object(
                    main,
                    "_schedule_employee_learning_batch",
                    side_effect=schedule_once_fails,
                ),
                mock.patch.object(
                    main,
                    "_settle_unstarted_learning_batch_manifest",
                    side_effect=blocked_settle,
                ),
            ):
                first = asyncio.create_task(
                    main.employee_learning_batch_create(execute)
                )
                entered = await asyncio.to_thread(settle_entered.wait, 2)
                self.assertTrue(entered)
                second = await main.employee_learning_batch_create(execute)
                self.assertTrue(second["started"])
                release_settle.set()
                with self.assertRaises(RuntimeError):
                    await first
                return second

        second = asyncio.run(scenario())
        batch = employeelearning.get_batch(second["batch"]["id"])
        self.assertEqual(employeelearning.BATCH_QUEUED, batch["status"])
        self.assertEqual(2, main._learning_batch_coordinator_generation(batch))
        self.assertTrue(all(
            run["status"] == employeelearning.RUN_QUEUED
            for run in employeelearning.list_batch_runs(batch["id"])
        ))
        self.assertEqual(2, schedule_calls)

    def test_manual_pause_generation_fences_older_create_compensation(self):
        body = {
            "request_key": "schema55-batch-pause-compensation-race-001",
            "idxs": [1001], "max_concurrency": 1,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        execute = {
            **body,
            "preview_token": preview["preview_token"],
            "budget_cap_points": 3,
            "confirm_execute": True,
        }
        settle_entered = threading.Event()
        release_settle = threading.Event()
        real_settle = main._settle_unstarted_learning_batch_manifest

        def blocked_settle(result):
            settle_entered.set()
            if not release_settle.wait(timeout=3):
                raise RuntimeError("pause takeover settlement barrier timed out")
            return real_settle(result)

        async def scenario():
            with (
                mock.patch.object(
                    main,
                    "_schedule_employee_learning_batch",
                    side_effect=RuntimeError("scheduler unavailable"),
                ),
                mock.patch.object(
                    main,
                    "_settle_unstarted_learning_batch_manifest",
                    side_effect=blocked_settle,
                ),
            ):
                first = asyncio.create_task(
                    main.employee_learning_batch_create(execute)
                )
                entered = await asyncio.to_thread(settle_entered.wait, 2)
                self.assertTrue(entered)
                durable = db.one(
                    "SELECT id FROM employee_learning_batch WHERE request_key=?",
                    (body["request_key"],),
                )
                paused = main.employee_learning_batch_pause(
                    int(durable["id"]), {"reason": "boss takeover pause"},
                )["batch"]
                self.assertEqual("paused", paused["status"])
                release_settle.set()
                with self.assertRaises(RuntimeError):
                    await first
                return int(durable["id"])

        batch_id = asyncio.run(scenario())
        batch = employeelearning.get_batch(batch_id)
        self.assertEqual(employeelearning.BATCH_PAUSED, batch["status"])
        self.assertEqual("boss takeover pause", batch["paused_reason"])
        self.assertEqual(2, main._learning_batch_coordinator_generation(batch))
        self.assertTrue(all(
            run["status"] == employeelearning.RUN_QUEUED
            for run in employeelearning.list_batch_runs(batch_id)
        ))

    def test_execute_request_cancellation_drains_manifest_and_starts_real_scheduler(self):
        body = {
            "request_key": "schema55-batch-create-cancel-barrier-001",
            "idxs": [1001],
            "max_concurrency": 1,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        execute = {
            **body,
            "preview_token": preview["preview_token"],
            "budget_cap_points": 3,
            "confirm_execute": True,
        }
        db_entered = threading.Event()
        release_db = threading.Event()
        real_create = main._create_learning_batch_manifest

        def blocked_create(*args, **kwargs):
            db_entered.set()
            if not release_db.wait(timeout=3):
                raise RuntimeError("test DB barrier timed out")
            return real_create(*args, **kwargs)

        async def exercise():
            worker_started = asyncio.Event()
            release_worker = asyncio.Event()

            async def held_worker(run_id, _tenant_id):
                worker_started.set()
                await release_worker.wait()
                employeelearning.cancel_run(run_id, reason="TEST_DONE")

            with (
                mock.patch.object(
                    main, "_create_learning_batch_manifest", new=blocked_create,
                ),
                mock.patch.object(
                    main, "_execute_learning_batch_run", new=held_worker,
                ),
            ):
                request = asyncio.create_task(
                    main.employee_learning_batch_create(execute)
                )
                entered = await asyncio.to_thread(db_entered.wait, 2)
                self.assertTrue(entered)
                request.cancel()
                await asyncio.sleep(0)
                release_db.set()
                with self.assertRaises(asyncio.CancelledError):
                    await request

                await asyncio.wait_for(worker_started.wait(), timeout=2)
                batch = db.one(
                    "SELECT * FROM employee_learning_batch WHERE request_key=?",
                    (body["request_key"],),
                )
                self.assertIsNotNone(batch)
                coordinator = main._LEARNING_BATCH_COORDINATORS.get(batch["id"])
                self.assertIsNotNone(coordinator)
                self.assertFalse(coordinator.done())
                release_worker.set()
                await asyncio.wait_for(coordinator, timeout=2)

        asyncio.run(exercise())

    def test_pause_stops_new_launches_without_rewinding_active_run_and_resume_is_explicit(self):
        body = {
            "request_key": "schema55-batch-pause-001",
            "idxs": [1001, 1002],
            "max_concurrency": 1,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        with mock.patch.object(
            main, "_schedule_employee_learning_batch", return_value=True,
        ):
            created = asyncio.run(main.employee_learning_batch_create({
                **body,
                "preview_token": preview["preview_token"],
                "budget_cap_points": 6,
                "confirm_execute": True,
            }))
        batch_id = created["batch"]["id"]
        first_run = employeelearning.list_batch_runs(batch_id)[0]
        employeelearning.reserve_budget(first_run["id"])
        employeelearning.start_run(first_run["id"])

        paused = main.employee_learning_batch_pause(
            batch_id, {"reason": "人工核对来源"},
        )["batch"]
        self.assertEqual("paused", paused["status"])
        self.assertEqual("人工核对来源", paused["paused_reason"])
        self.assertEqual(
            employeelearning.RUN_RESEARCHING,
            employeelearning.get_run(first_run["id"])["status"],
        )
        self.assertEqual(1, paused["counts"]["queued"])
        second_run = employeelearning.list_batch_runs(batch_id)[1]
        asyncio.run(main._execute_learning_batch_run(second_run["id"], 1))
        self.assertEqual(
            employeelearning.RUN_QUEUED,
            employeelearning.get_run(second_run["id"])["status"],
        )
        self.assertEqual(0.0, employeelearning.get_run(second_run["id"])["spent_points"])

        with mock.patch.object(
            main, "_schedule_employee_learning_batch", return_value=True,
        ) as schedule:
            resumed = asyncio.run(main.employee_learning_batch_resume(batch_id))
        self.assertTrue(resumed["started"])
        # The mocked scheduler acknowledges ownership but intentionally never
        # runs a launch claim.  Resume therefore commits the truthful durable
        # ``queued`` state; the real-scheduler test below observes ``running``
        # only after its worker actually starts.
        self.assertEqual("queued", resumed["batch"]["status"])
        schedule.assert_called_once_with(batch_id)

    def test_resume_http_runs_real_scheduler_on_event_loop_and_is_idempotent(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,created_at,updated_at) "
            "VALUES(1,'平台总部',1,?,?)",
            (now, now),
        )
        user_id = db.insert("users", {
            "tenant_id": 1,
            "username": "boss",
            "password_hash": "schema55-http-fixture",
            "role": "root",
            "modules_json": "[]",
            "enabled": 1,
        })
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-resume-http-001",
            "idxs": [1001],
            "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=user_id)
        main._pause_learning_batch_without_interrupting(
            batch["id"], "HTTP 恢复测试",
        )
        worker_started = asyncio.Event()
        release_worker = asyncio.Event()
        launches = 0

        async def held_worker(run_id, _tenant_id):
            nonlocal launches
            launches += 1
            worker_started.set()
            await release_worker.wait()
            employeelearning.cancel_run(run_id, reason="TEST_DONE")

        async def exercise():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://paihuo.test",
                cookies={"cc_sess": auth.make_session(user_id)},
            ) as client:
                first = await client.post(
                    f"/api/employee-learning/batches/{batch['id']}/resume"
                )
                self.assertEqual(200, first.status_code, first.text)
                self.assertTrue(first.json()["started"])
                await asyncio.wait_for(worker_started.wait(), timeout=2)
                coordinator = main._LEARNING_BATCH_COORDINATORS[batch["id"]]

                second = await client.post(
                    f"/api/employee-learning/batches/{batch['id']}/resume"
                )
                self.assertEqual(200, second.status_code, second.text)
                self.assertFalse(second.json()["started"])
                self.assertIs(
                    coordinator,
                    main._LEARNING_BATCH_COORDINATORS[batch["id"]],
                )
                release_worker.set()
                await asyncio.wait_for(coordinator, timeout=2)

        with mock.patch.object(
            main, "_execute_learning_batch_run", new=held_worker,
        ):
            asyncio.run(exercise())
        self.assertEqual(1, launches)

    def test_resume_scheduler_failure_restores_paused_state(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-resume-start-failure-001",
            "idxs": [1001],
            "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        main._pause_learning_batch_without_interrupting(
            batch["id"], "人工审查中",
        )

        with mock.patch.object(
            main,
            "_schedule_employee_learning_batch",
            side_effect=RuntimeError("no running event loop"),
        ), self.assertRaises(RuntimeError):
            asyncio.run(main.employee_learning_batch_resume(batch["id"]))

        restored = employeelearning.get_batch(batch["id"])
        self.assertEqual(employeelearning.BATCH_PAUSED, restored["status"])
        self.assertEqual("人工审查中", restored["paused_reason"])

    def test_resume_generation_fences_older_scheduler_compensation(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-resume-compensation-race-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        main._pause_learning_batch_without_interrupting(batch["id"], "人工审查中")
        settle_entered = threading.Event()
        release_settle = threading.Event()
        real_settle = main._settle_unstarted_employee_learning_batch
        schedule_calls = 0

        def schedule_once_fails(_batch_id):
            nonlocal schedule_calls
            schedule_calls += 1
            if schedule_calls == 1:
                raise RuntimeError("first resume scheduler unavailable")
            return True

        def blocked_settle(state):
            settle_entered.set()
            if not release_settle.wait(timeout=3):
                raise RuntimeError("resume settlement barrier timed out")
            return real_settle(state)

        async def scenario():
            with (
                mock.patch.object(
                    main,
                    "_schedule_employee_learning_batch",
                    side_effect=schedule_once_fails,
                ),
                mock.patch.object(
                    main,
                    "_settle_unstarted_employee_learning_batch",
                    side_effect=blocked_settle,
                ),
            ):
                first = asyncio.create_task(
                    main.employee_learning_batch_resume(batch["id"])
                )
                entered = await asyncio.to_thread(settle_entered.wait, 2)
                self.assertTrue(entered)
                second = await main.employee_learning_batch_resume(batch["id"])
                self.assertTrue(second["started"])
                release_settle.set()
                with self.assertRaises(RuntimeError):
                    await first
                return second

        second = asyncio.run(scenario())
        durable = employeelearning.get_batch(batch["id"])
        self.assertEqual(employeelearning.BATCH_QUEUED, durable["status"])
        # Initial explicit pause owns generation 1; the two accepted resumes
        # then take generations 2 and 3.
        self.assertEqual(3, main._learning_batch_coordinator_generation(durable))
        self.assertEqual("queued", second["batch"]["status"])
        self.assertTrue(all(
            run["status"] == employeelearning.RUN_QUEUED
            for run in employeelearning.list_batch_runs(batch["id"])
        ))
        self.assertEqual(2, schedule_calls)

    def test_explicit_resume_reconciles_refunded_terminal_reservation_in_scope(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'显式恢复结算租户',1,10,?,?)",
            (now, now),
        )
        auth.set_current({
            "id": 2, "tenant_id": 2, "role": "root",
            "username": "boss", "modules": ["*"],
        })
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-resume-scoped-reconcile-001",
            "idxs": [1001, 1002], "max_concurrency": 1,
        }, tenant_id=2)
        batch = main._create_learning_batch_manifest(preview, actor_id=2)
        failed, queued = employeelearning.list_batch_runs(batch["id"])
        employeelearning.reserve_budget(failed["id"])
        employeelearning.start_run(failed["id"])
        op_key = billing.start_operation(
            "learn",
            tid=2,
            op_key=main._learning_billing_op_key(2, failed["id"]),
        )
        employeelearning.checkpoint(failed["id"], {
            **main._learning_run_checkpoint(employeelearning.get_run(failed["id"])),
            "stage": "researching",
            "billing_op_key": op_key,
        })
        employeelearning.cancel_run(failed["id"], reason="CRASH_WINDOW")
        billing.fail_operation(op_key, "CRASH_AFTER_REFUND_BEFORE_RELEASE")
        main._pause_learning_batch_without_interrupting(
            batch["id"], "重启后显式恢复",
        )
        self.assertEqual(3.0, float(employeelearning.get_run(
            failed["id"]
        )["spent_points"] or 0))
        self.assertEqual(3.0, float(employeelearning.get_batch(
            batch["id"]
        )["spent_points"] or 0))

        with mock.patch.object(
            main, "_schedule_employee_learning_batch", return_value=True,
        ) as schedule:
            resumed = asyncio.run(main.employee_learning_batch_resume(batch["id"]))
        self.assertTrue(resumed["started"])
        schedule.assert_called_once_with(batch["id"])
        self.assertEqual(0.0, float(employeelearning.get_run(
            failed["id"]
        )["spent_points"] or 0))
        self.assertEqual(0.0, float(employeelearning.get_batch(
            batch["id"]
        )["spent_points"] or 0))
        self.assertEqual(employeelearning.RUN_QUEUED,
                         employeelearning.get_run(queued["id"])["status"])
        self.assertEqual(10.0, billing.balance(2))

    def test_batch_api_is_named_boss_only_and_never_accepts_auto_approval(self):
        auth.set_current({
            "id": 2,
            "tenant_id": 1,
            "role": "root",
            "username": "another-root",
            "modules": ["*"],
        })
        with self.assertRaises(HTTPException) as denied:
            asyncio.run(main.employee_learning_batch_dry_run({
                "request_key": "schema55-batch-denied-001",
            }))
        self.assertEqual(403, denied.exception.status_code)

        auth.set_current({
            "id": 1,
            "tenant_id": 1,
            "role": "root",
            "username": "boss",
            "modules": ["*"],
        })
        body = {
            "request_key": "schema55-batch-no-auto-approve-001",
            "idxs": [1001],
            "max_concurrency": 1,
        }
        preview = asyncio.run(main.employee_learning_batch_dry_run(body))["preview"]
        with self.assertRaises(HTTPException) as rejected:
            asyncio.run(main.employee_learning_batch_create({
                **body,
                "preview_token": preview["preview_token"],
                "budget_cap_points": 3,
                "confirm_execute": True,
                "auto_approve": True,
            }))
        self.assertEqual(400, rejected.exception.status_code)

    def test_each_run_charges_tenant_once_and_replay_does_not_charge_again(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'批次测试租户',1,20,?,?)",
            (now, now),
        )
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-billing-001",
            "idxs": [1001],
            "max_concurrency": 1,
        }, tenant_id=2)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        run = employeelearning.list_batch_runs(batch["id"])[0]

        async def verified_research(*_args, **_kwargs):
            return {"sources": self._verified_sources()}

        with mock.patch.object(
            main.providers,
            "call_verified_learning_research",
            new=verified_research,
        ):
            asyncio.run(main._execute_learning_batch_run(run["id"], 2))
            asyncio.run(main._execute_learning_batch_run(run["id"], 2))

        finished = employeelearning.get_run(run["id"])
        self.assertEqual(
            employeelearning.RUN_ACTIVATED, finished["status"]
        )
        op_key = main._learning_billing_op_key(2, run["id"])
        operation = db.one(
            "SELECT status,points,units FROM billing_operation WHERE op_key=?",
            (op_key,),
        )
        self.assertEqual("succeeded", operation["status"])
        self.assertEqual(3.0, operation["points"])
        self.assertEqual(1, operation["units"])
        self.assertEqual(17.0, db.one(
            "SELECT balance FROM tenants WHERE id=2"
        )["balance"])
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_log WHERE tenant_id=2 AND delta=-3"
        )["n"])

    def test_post_charge_checkpoint_failure_refunds_wallet_and_platform_operation(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'计费故障租户',1,10,?,?)",
            (now, now),
        )
        real_checkpoint = employeelearning.checkpoint

        def fail_researching_checkpoint(run_id, payload):
            if isinstance(payload, dict) and payload.get("stage") == "researching":
                raise RuntimeError("post-charge checkpoint failure")
            return real_checkpoint(run_id, payload)

        for tenant_id, request_key in (
            (2, "schema55-batch-post-charge-refund-tenant-001"),
            (1, "schema55-batch-post-charge-refund-platform-001"),
        ):
            with self.subTest(tenant_id=tenant_id):
                preview = main._learning_batch_preview_contract({
                    "request_key": request_key,
                    "idxs": [1001],
                    "max_concurrency": 1,
                }, tenant_id=tenant_id)
                batch = main._create_learning_batch_manifest(preview, actor_id=1)
                run = employeelearning.list_batch_runs(batch["id"])[0]
                with mock.patch.object(
                    main.employeelearning,
                    "checkpoint",
                    side_effect=fail_researching_checkpoint,
                ):
                    asyncio.run(
                        main._execute_learning_batch_run(run["id"], tenant_id)
                    )

                finished = employeelearning.get_run(run["id"])
                operation = db.one(
                    "SELECT status,points FROM billing_operation WHERE op_key=?",
                    (main._learning_billing_op_key(tenant_id, run["id"]),),
                )
                self.assertEqual(employeelearning.RUN_CANCELLED, finished["status"])
                self.assertEqual(0.0, float(finished["spent_points"] or 0))
                self.assertEqual("refunded", operation["status"])
                self.assertEqual(0.0 if tenant_id == 1 else 3.0,
                                 float(operation["points"]))
                self.assertIsNone(
                    employeelearning._identity_run_owner(run["identity_ref"])
                )
                if tenant_id == 2:
                    self.assertEqual(10.0, db.one(
                        "SELECT balance FROM tenants WHERE id=2"
                    )["balance"])

    def test_insufficient_points_pauses_without_dropping_frozen_target(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'余额等待租户',1,0,?,?)",
            (now, now),
        )
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-billing-wait-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=2)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        run = employeelearning.list_batch_runs(batch["id"])[0]
        asyncio.run(main._execute_learning_batch_run(run["id"], 2))

        waiting = employeelearning.get_run(run["id"])
        paused = employeelearning.get_batch(batch["id"])
        self.assertEqual(employeelearning.RUN_QUEUED, waiting["status"])
        self.assertEqual(0.0, waiting["spent_points"])
        self.assertEqual(employeelearning.BATCH_PAUSED, paused["status"])
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_operation WHERE tenant_id=2"
        )["n"])

        db.execute("UPDATE tenants SET balance=6 WHERE id=2")
        employeelearning.resume_batch(batch["id"])

        async def verified_research(*_args, **_kwargs):
            return {"sources": self._verified_sources()}

        with mock.patch.object(
            main.providers, "call_verified_learning_research",
            new=verified_research,
        ):
            asyncio.run(main._execute_learning_batch_run(run["id"], 2))
        self.assertEqual(
            employeelearning.RUN_ACTIVATED,
            employeelearning.get_run(run["id"])["status"],
        )

    def test_coordinator_honors_concurrency_and_restart_requires_explicit_resume(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'恢复测试租户',1,50,?,?)",
            (now, now),
        )
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-coordinator-001",
            "idxs": [1001, 1002, 1003, 1004],
            "max_concurrency": 3,
        }, tenant_id=2)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        active = 0
        maximum = 0

        async def bounded_fake(run_id, _tenant_id):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            employeelearning.cancel_run(run_id, reason="TEST_DONE")
            active -= 1

        with mock.patch.object(
            main, "_execute_learning_batch_run", new=bounded_fake,
        ):
            asyncio.run(asyncio.wait_for(
                main._employee_learning_batch_coordinator(batch["id"]),
                timeout=2,
            ))
        self.assertEqual(3, maximum)
        self.assertTrue(all(
            run["status"] == employeelearning.RUN_CANCELLED
            for run in employeelearning.list_batch_runs(batch["id"])
        ))
        self.assertFalse(main._LEARNING_BATCH_ACTIVE_RUNS)

        recovery_preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-recovery-001",
            "idxs": [1005, 1006],
            "max_concurrency": 1,
        }, tenant_id=2)
        recovery_batch = main._create_learning_batch_manifest(
            recovery_preview, actor_id=1,
        )
        first, second = employeelearning.list_batch_runs(recovery_batch["id"])
        employeelearning.reserve_budget(first["id"])
        employeelearning.start_run(first["id"])
        op_key = billing.start_operation(
            "learn",
            tid=2,
            note="重启中断测试",
            op_key=main._learning_billing_op_key(2, first["id"]),
        )
        checkpoint = main._learning_run_checkpoint(employeelearning.get_run(first["id"]))
        employeelearning.checkpoint(
            first["id"],
            {**checkpoint, "stage": "researching", "billing_op_key": op_key},
        )
        self.assertEqual(1, employeelearning.recover_interrupted_runs())
        _settled, protected = main._recover_employee_learning_billing()
        self.assertNotIn(op_key, protected)
        self.assertEqual(
            1,
            billing.recover_interrupted_operations(exclude_op_keys=protected),
        )
        self.assertEqual("refunded", db.one(
            "SELECT status FROM billing_operation WHERE op_key=?", (op_key,)
        )["status"])
        self.assertEqual(employeelearning.RUN_FAILED,
                         employeelearning.get_run(first["id"])["status"])
        self.assertEqual(employeelearning.RUN_QUEUED,
                         employeelearning.get_run(second["id"])["status"])

        auth.set_current({
            "id": 1,
            "tenant_id": 2,
            "role": "root",
            "username": "boss",
            "modules": ["*"],
        })
        public = main.employee_learning_batch_get(recovery_batch["id"])["batch"]
        self.assertTrue(public["can_resume"])
        with mock.patch.object(
            main, "_schedule_employee_learning_batch", return_value=True,
        ) as schedule:
            resumed = asyncio.run(
                main.employee_learning_batch_resume(recovery_batch["id"])
            )
        self.assertTrue(resumed["started"])
        schedule.assert_called_once_with(recovery_batch["id"])

    def test_expired_queued_run_stops_before_reserve_billing_and_provider(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'过期配额租户',1,10,?,?)",
            (now, now),
        )
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-expired-before-launch-001",
            "idxs": [1001],
            "max_concurrency": 1,
        }, tenant_id=2)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        run = employeelearning.list_batch_runs(batch["id"])[0]
        db.execute(
            "UPDATE employee_learning_run SET expires_at=? WHERE id=?",
            (time.time() - 5, int(run["id"])),
        )
        provider = mock.AsyncMock(side_effect=AssertionError("provider called"))
        with mock.patch.object(
            main.providers, "call_verified_learning_research", new=provider,
        ):
            asyncio.run(main._execute_learning_batch_run(run["id"], 2))

        closed = employeelearning.get_run(run["id"])
        self.assertEqual(employeelearning.RUN_EXPIRED, closed["status"])
        self.assertEqual(0.0, float(closed["spent_points"] or 0))
        self.assertEqual(0, provider.await_count)
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_operation WHERE tenant_id=2"
        )["n"])
        self.assertEqual(10.0, billing.balance(2))
        self.assertIsNone(
            employeelearning._identity_run_owner(run["identity_ref"])
        )
        self.assertEqual(
            employeelearning.BATCH_COMPLETED,
            employeelearning.get_batch(batch["id"])["status"],
        )

    def test_batch_reads_and_new_manifest_lazily_release_expired_owners(self):
        old_preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-expired-owner-old-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        old_batch = main._create_learning_batch_manifest(old_preview, actor_id=1)
        old_run = employeelearning.list_batch_runs(old_batch["id"])[0]
        employeelearning._set_run_status(
            old_run, employeelearning.RUN_AWAITING_APPROVAL,
        )
        db.execute(
            "UPDATE employee_learning_run SET expires_at=? WHERE id=?",
            (time.time() - 5, int(old_run["id"])),
        )

        # No GET/list sweep occurs first: manifest owner preflight itself must
        # expire the old proposal in the same BEGIN IMMEDIATE transaction.
        new_preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-after-expired-owner-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        new_batch = main._create_learning_batch_manifest(new_preview, actor_id=1)
        self.assertEqual(
            employeelearning.RUN_EXPIRED,
            employeelearning.get_run(old_run["id"])["status"],
        )
        self.assertEqual(
            employeelearning.RUN_QUEUED,
            employeelearning.list_batch_runs(new_batch["id"])[0]["status"],
        )

        read_preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-expired-read-sweep-001",
            "idxs": [1002], "max_concurrency": 1,
        }, tenant_id=1)
        read_batch = main._create_learning_batch_manifest(read_preview, actor_id=1)
        read_run = employeelearning.list_batch_runs(read_batch["id"])[0]
        employeelearning._set_run_status(
            read_run, employeelearning.RUN_AWAITING_APPROVAL,
        )
        db.execute(
            "UPDATE employee_learning_run SET expires_at=? WHERE id=?",
            (time.time() - 5, int(read_run["id"])),
        )
        main._LEARNING_BATCH_COORDINATORS.clear()
        listed = main.employee_learning_batches_list(limit=10)["batches"]
        row = next(item for item in listed if item["id"] == read_batch["id"])
        self.assertEqual(1, row["counts"]["failed"])
        self.assertEqual(
            employeelearning.RUN_EXPIRED,
            employeelearning.get_run(read_run["id"])["status"],
        )

    def test_launch_claim_rechecks_disabled_slot_inside_reservation_transaction(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'启动门禁租户',1,10,?,?)",
            (now, now),
        )
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-slot-disabled-at-claim-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=2)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        run = employeelearning.list_batch_runs(batch["id"])[0]
        real_reserve = employeelearning.reserve_budget

        def disable_then_reserve(run_id):
            slot = employees.slot_state(1001)
            employees.set_enabled(
                1001, False, expected_row_version=slot["row_version"],
            )
            return real_reserve(run_id)

        provider = mock.AsyncMock(side_effect=AssertionError("provider called"))
        with (
            mock.patch.object(
                main.employeelearning,
                "reserve_budget",
                side_effect=disable_then_reserve,
            ),
            mock.patch.object(
                main.providers,
                "call_verified_learning_research",
                new=provider,
            ),
        ):
            asyncio.run(main._execute_learning_batch_run(run["id"], 2))

        closed = employeelearning.get_run(run["id"])
        self.assertEqual(employeelearning.RUN_STALE, closed["status"])
        self.assertEqual(0.0, float(closed["spent_points"] or 0))
        self.assertEqual(0, provider.await_count)
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_operation WHERE tenant_id=2"
        )["n"])
        self.assertIsNone(
            employeelearning._identity_run_owner(run["identity_ref"])
        )

    def test_single_and_bulk_request_keys_are_permanently_mode_namespaced(self):
        bulk_key = "schema55-mode-namespace-bulk-first-001"
        bulk_preview = main._learning_batch_preview_contract({
            "request_key": bulk_key,
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        bulk_batch = main._create_learning_batch_manifest(
            bulk_preview, actor_id=1,
        )
        bulk_run = employeelearning.list_batch_runs(bulk_batch["id"])[0]
        employeelearning.cancel_run(bulk_run["id"], reason="TEST_TERMINAL")
        config2 = employees.get_config(1002)
        with self.assertRaises(HTTPException) as single_conflict:
            asyncio.run(main.employee_learning_run_create(1002, {
                "request_key": bulk_key,
                "identity_ref": config2["identity_ref"],
                "config_revision": config2["config_revision"],
                "config_sha256": config2["config_sha256"],
                "bundle_sha256": config2["bundle_sha256"],
            }))
        self.assertEqual(409, single_conflict.exception.status_code)
        self.assertEqual(1, len(employeelearning.list_batch_runs(bulk_batch["id"])))

        single_key = "schema55-mode-namespace-single-first-001"
        config2 = employees.get_config(1002)

        async def no_network_worker(_run_id, _binding):
            return None

        with mock.patch.object(
            main, "_employee_learning_research_worker", new=no_network_worker,
        ):
            single = asyncio.run(main.employee_learning_run_create(1002, {
                "request_key": single_key,
                "identity_ref": config2["identity_ref"],
                "config_revision": config2["config_revision"],
                "config_sha256": config2["config_sha256"],
                "bundle_sha256": config2["bundle_sha256"],
            }))
        single_run_id = int(single["run"]["id"])
        billing.fail_operation(
            main._learning_billing_op_key(1, single_run_id), "TEST_TERMINAL",
        )
        employeelearning.cancel_run(single_run_id, reason="TEST_TERMINAL")
        employeelearning.release_budget(single_run_id)
        candidate = {
            "request_key": single_key,
            "idxs": [1001], "max_concurrency": 1,
        }
        public_preview = asyncio.run(
            main.employee_learning_batch_dry_run(candidate)
        )["preview"]
        with self.assertRaises(HTTPException) as bulk_conflict:
            asyncio.run(main.employee_learning_batch_create({
                **candidate,
                "preview_token": public_preview["preview_token"],
                "budget_cap_points": 3,
                "confirm_execute": True,
            }))
        self.assertEqual(409, bulk_conflict.exception.status_code)

    def test_bulk_list_filters_single_rows_before_limit_and_detail_rejects_them(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-bulk-not-hidden-by-single-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        bulk = main._create_learning_batch_manifest(preview, actor_id=1)
        single_ids = []
        for index in range(9):
            single = employeelearning.create_batch(
                f"schema55-single-list-noise-{index:02d}",
                budget_cap_points=3,
                tenant_id=1,
            )
            single_ids.append(int(single["id"]))
            db.execute(
                "UPDATE employee_learning_batch SET metadata_json=? WHERE id=?",
                (
                    json.dumps({
                        "schema": "schema55-learning-single-v1",
                        "employee_idx": 1001 + index,
                    }, ensure_ascii=False),
                    int(single["id"]),
                ),
            )

        listed = main.employee_learning_batches_list(limit=8)["batches"]
        self.assertEqual([int(bulk["id"])], [row["id"] for row in listed])
        with self.assertRaises(HTTPException) as hidden_detail:
            main.employee_learning_batch_get(single_ids[-1])
        self.assertEqual(404, hidden_detail.exception.status_code)

    def test_public_separates_planned_cap_from_actual_wallet_debit(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'净扣款证明租户',1,10,?,?)",
            (now, now),
        )
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-actual-wallet-proof-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=2)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        queued = main._learning_batch_public(batch)
        self.assertEqual(3.0, queued["wallet_charge_points"])
        self.assertEqual(3.0, queued["planned_wallet_charge_points"])
        self.assertEqual(0.0, queued["actual_wallet_debit_points"])
        self.assertEqual("verified", queued["billing_proof_status"])
        self.assertEqual("verified", queued["actual_wallet_debit_proof_status"])

        async def verified_research(*_args, **_kwargs):
            return {"sources": self._verified_sources()}

        run = employeelearning.list_batch_runs(batch["id"])[0]
        with mock.patch.object(
            main.providers,
            "call_verified_learning_research",
            new=verified_research,
        ):
            asyncio.run(main._execute_learning_batch_run(run["id"], 2))
        charged = main._learning_batch_public(
            employeelearning.get_batch(batch["id"])
        )
        self.assertEqual(3.0, charged["wallet_charge_points"])
        self.assertEqual(3.0, charged["actual_wallet_debit_points"])

        metadata = main._learning_batch_metadata(batch)
        metadata["target_count"] = 2
        db.execute(
            "UPDATE employee_learning_batch SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), int(batch["id"])),
        )
        tampered = main._learning_batch_public(
            employeelearning.get_batch(batch["id"])
        )
        self.assertEqual("proof_missing", tampered["billing_proof_status"])
        self.assertIsNone(tampered["wallet_charge_points"])

    def test_final_broadcast_failure_cannot_refund_a_delivered_proposal(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'通知故障租户',1,10,?,?)",
            (now, now),
        )
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-final-broadcast-failure-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=2)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        run = employeelearning.list_batch_runs(batch["id"])[0]

        async def verified_research(*_args, **_kwargs):
            return {"sources": self._verified_sources()}

        broadcasts = 0

        def fail_final_broadcast(_payload):
            nonlocal broadcasts
            broadcasts += 1
            if broadcasts == 2:
                raise RuntimeError("injected websocket failure")

        with (
            mock.patch.object(
                main.providers,
                "call_verified_learning_research",
                new=verified_research,
            ),
            mock.patch.object(main.engine, "broadcast", new=fail_final_broadcast),
        ):
            asyncio.run(main._execute_learning_batch_run(run["id"], 2))

        delivered = employeelearning.get_run(run["id"])
        self.assertEqual(employeelearning.RUN_ACTIVATED, delivered["status"])
        self.assertEqual(3.0, float(delivered["spent_points"] or 0))
        self.assertEqual(5, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_source WHERE run_id=?",
            (run["id"],),
        )["n"])
        self.assertEqual(4, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_artifact WHERE run_id=?",
            (run["id"],),
        )["n"])
        op = db.one(
            "SELECT status FROM billing_operation WHERE op_key=?",
            (main._learning_billing_op_key(2, run["id"]),),
        )
        self.assertEqual("succeeded", op["status"])
        self.assertEqual(7.0, billing.balance(2))

    def test_restart_billing_recovery_distinguishes_expired_delivery(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'过期交付恢复租户',1,20,?,?)",
            (now, now),
        )

        def charged_run(idx: int, request_key: str):
            preview = main._learning_batch_preview_contract({
                "request_key": request_key,
                "idxs": [idx], "max_concurrency": 1,
            }, tenant_id=2)
            batch = main._create_learning_batch_manifest(preview, actor_id=1)
            queued = employeelearning.list_batch_runs(batch["id"])[0]
            prepared = main._claim_learning_batch_run_for_launch(queued["id"])
            self.assertTrue(prepared["claimed"])
            run = prepared["run"]
            op_key = main._start_learning_billing_at_frozen_price(
                run["id"], 2, "过期交付恢复测试",
            )
            employeelearning.checkpoint(run["id"], {
                **main._learning_run_checkpoint(run),
                "stage": "researching",
                "billing_op_key": op_key,
            })
            return prepared, op_key

        delivered, delivered_op = charged_run(
            1001, "schema55-expired-delivered-recovery-001",
        )
        sources = self._verified_sources()
        artifacts = main._evidence_backed_learning_artifacts(
            delivered["binding"]["employee"], sources,
        )
        employeelearning.research_run(
            delivered["run"]["id"],
            lambda _context: {"sources": sources, "artifacts": artifacts},
        )
        undelivered, undelivered_op = charged_run(
            1002, "schema55-expired-undelivered-recovery-001",
        )
        for run_id in (delivered["run"]["id"], undelivered["run"]["id"]):
            db.execute(
                "UPDATE employee_learning_run SET expires_at=? WHERE id=?",
                (time.time() - 1, int(run_id)),
            )
            employeelearning.expire_run(int(run_id))

        self.assertEqual(14.0, billing.balance(2))
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.conn()

        settled, protected = main._recover_employee_learning_billing()
        billing.recover_interrupted_operations(exclude_op_keys=protected)
        self.assertEqual(1, settled)
        self.assertIn(delivered_op, protected)
        self.assertNotIn(undelivered_op, protected)
        self.assertEqual("succeeded", db.one(
            "SELECT status FROM billing_operation WHERE op_key=?",
            (delivered_op,),
        )["status"])
        self.assertEqual("refunded", db.one(
            "SELECT status FROM billing_operation WHERE op_key=?",
            (undelivered_op,),
        )["status"])
        self.assertEqual(3.0, float(employeelearning.get_run(
            delivered["run"]["id"]
        )["spent_points"] or 0))
        self.assertEqual(0.0, float(employeelearning.get_run(
            undelivered["run"]["id"]
        )["spent_points"] or 0))
        self.assertEqual(17.0, billing.balance(2))

    def test_frozen_three_point_price_drift_blocks_batch_and_single_before_web(self):
        now = time.time()
        db.execute(
            "INSERT INTO tenants(id,name,enabled,balance,created_at,updated_at) "
            "VALUES(2,'价格漂移租户',1,20,?,?)",
            (now, now),
        )
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-price-drift-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=2)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        run = employeelearning.list_batch_runs(batch["id"])[0]
        changed_prices = {key: dict(value) for key, value in billing.prices().items()}
        changed_prices["learn"]["points"] = 5
        db.set_setting("prices", json.dumps(changed_prices, ensure_ascii=False))
        provider = mock.AsyncMock(side_effect=AssertionError("provider called"))
        with mock.patch.object(
            main.providers, "call_verified_learning_research", new=provider,
        ):
            asyncio.run(main._execute_learning_batch_run(run["id"], 2))
        blocked = employeelearning.get_run(run["id"])
        self.assertEqual(employeelearning.RUN_CANCELLED, blocked["status"])
        self.assertEqual(0.0, float(blocked["spent_points"] or 0))
        self.assertEqual(0, provider.await_count)
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_operation WHERE tenant_id=2"
        )["n"])
        self.assertEqual(20.0, billing.balance(2))

        auth.set_current({
            "id": 2, "tenant_id": 2, "role": "root",
            "username": "boss", "modules": ["*"],
        })
        config2 = employees.get_config(1002)
        with (
            mock.patch.object(
                main.providers,
                "call_verified_learning_research",
                new=provider,
            ),
            self.assertRaises(HTTPException) as single_blocked,
        ):
            asyncio.run(main.employee_learning_run_create(1002, {
                "request_key": "schema55-single-price-drift-001",
                "identity_ref": config2["identity_ref"],
                "config_revision": config2["config_revision"],
                "config_sha256": config2["config_sha256"],
                "bundle_sha256": config2["bundle_sha256"],
            }))
        self.assertEqual(409, single_blocked.exception.status_code)
        single_run = db.one(
            "SELECT r.id FROM employee_learning_run r "
            "JOIN employee_learning_batch b ON b.id=r.batch_id "
            "WHERE b.request_key=?",
            ("schema55-single-price-drift-001",),
        )
        self.assertIsNotNone(single_run)
        self.assertEqual(
            employeelearning.RUN_CANCELLED,
            employeelearning.get_run(single_run["id"])["status"],
        )
        self.assertEqual(0, provider.await_count)
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM billing_operation WHERE tenant_id=2"
        )["n"])
        self.assertEqual(20.0, billing.balance(2))

    def test_preview_rejects_nonfinite_or_nonexact_learning_prices(self):
        original = {key: dict(value) for key, value in billing.prices().items()}
        for index, invalid in enumerate(
            (float("nan"), float("inf"), float("-inf"), 2.999, 5, True),
        ):
            with self.subTest(invalid=invalid):
                changed = {key: dict(value) for key, value in original.items()}
                changed["learn"]["points"] = invalid
                db.set_setting(
                    "prices", json.dumps(changed, ensure_ascii=False),
                )
                with self.assertRaises(HTTPException) as blocked:
                    asyncio.run(main.employee_learning_batch_dry_run({
                        "request_key": f"schema55-invalid-price-{index:02d}",
                        "idxs": [1001], "max_concurrency": 1,
                    }))
                self.assertEqual(503, blocked.exception.status_code)
                self.assertEqual(0, db.one(
                    "SELECT COUNT(*) AS n FROM employee_learning_batch"
                )["n"])

    def test_resume_wakes_existing_coordinator_before_stale_paused_exit(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-resume-existing-coordinator-race-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        main._pause_learning_batch_without_interrupting(
            batch["id"], "race barrier",
        )

        async def scenario():
            list_entered = asyncio.Event()
            release_list = asyncio.Event()
            worker_started = asyncio.Event()
            real_arun = main.db.arun

            async def blocked_arun(fn, *args, **kwargs):
                if fn is employeelearning.list_batch_runs and not list_entered.is_set():
                    list_entered.set()
                    await release_list.wait()
                return await real_arun(fn, *args, **kwargs)

            async def held_worker(run_id, _tenant_id):
                worker_started.set()
                employeelearning.cancel_run(run_id, reason="TEST_DONE")

            with (
                mock.patch.object(main.db, "arun", new=blocked_arun),
                mock.patch.object(
                    main, "_execute_learning_batch_run", new=held_worker,
                ),
            ):
                self.assertTrue(main._schedule_employee_learning_batch(batch["id"]))
                coordinator = main._LEARNING_BATCH_COORDINATORS[batch["id"]]
                await asyncio.wait_for(list_entered.wait(), timeout=2)
                resumed = await main.employee_learning_batch_resume(batch["id"])
                self.assertFalse(resumed["started"])
                self.assertIs(
                    coordinator,
                    main._LEARNING_BATCH_COORDINATORS[batch["id"]],
                )
                release_list.set()
                await asyncio.wait_for(worker_started.wait(), timeout=2)
                await asyncio.wait_for(coordinator, timeout=2)

        asyncio.run(scenario())
        self.assertEqual(
            employeelearning.RUN_CANCELLED,
            employeelearning.list_batch_runs(batch["id"])[0]["status"],
        )

    def test_resume_final_paused_read_hands_off_to_successor_coordinator(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-resume-final-read-race-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        main._pause_learning_batch_without_interrupting(
            batch["id"], "final read barrier",
        )
        second_read_entered = threading.Event()
        release_second_read = threading.Event()
        real_get_batch = employeelearning.get_batch
        get_calls = 0
        get_lock = threading.Lock()

        def captured_second_get(batch_id):
            nonlocal get_calls
            result = real_get_batch(batch_id)
            with get_lock:
                get_calls += 1
                call_number = get_calls
            if call_number == 2:
                second_read_entered.set()
                if not release_second_read.wait(timeout=3):
                    raise RuntimeError("final paused read barrier timed out")
            return result

        async def scenario():
            worker_started = asyncio.Event()

            async def held_worker(run_id, _tenant_id):
                worker_started.set()
                employeelearning.cancel_run(run_id, reason="TEST_DONE")

            with (
                mock.patch.object(
                    main.employeelearning,
                    "get_batch",
                    side_effect=captured_second_get,
                ),
                mock.patch.object(
                    main, "_execute_learning_batch_run", new=held_worker,
                ),
            ):
                self.assertTrue(main._schedule_employee_learning_batch(batch["id"]))
                old = main._LEARNING_BATCH_COORDINATORS[batch["id"]]
                entered = await asyncio.to_thread(
                    second_read_entered.wait, 2,
                )
                self.assertTrue(entered)
                resumed = await main.employee_learning_batch_resume(batch["id"])
                self.assertFalse(resumed["started"])
                self.assertIs(old, main._LEARNING_BATCH_COORDINATORS[batch["id"]])
                release_second_read.set()
                await asyncio.wait_for(worker_started.wait(), timeout=2)
                await asyncio.wait_for(old, timeout=2)

        asyncio.run(scenario())
        self.assertEqual(
            employeelearning.RUN_CANCELLED,
            employeelearning.list_batch_runs(batch["id"])[0]["status"],
        )

    def test_coordinator_db_exception_pauses_queued_work_instead_of_orphaning(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-coordinator-db-failure-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        real_arun = main.db.arun
        injected = False

        async def fail_one_list(fn, *args, **kwargs):
            nonlocal injected
            if fn is employeelearning.list_batch_runs and not injected:
                injected = True
                raise RuntimeError("transient coordinator DB failure")
            return await real_arun(fn, *args, **kwargs)

        async def scenario():
            with mock.patch.object(main.db, "arun", new=fail_one_list):
                self.assertTrue(main._schedule_employee_learning_batch(batch["id"]))
                coordinator = main._LEARNING_BATCH_COORDINATORS[batch["id"]]
                with self.assertRaises(RuntimeError):
                    await coordinator

        asyncio.run(scenario())
        settled = employeelearning.get_batch(batch["id"])
        self.assertEqual(employeelearning.BATCH_PAUSED, settled["status"])
        self.assertIn("异常中断", settled["paused_reason"])
        self.assertEqual(
            employeelearning.RUN_QUEUED,
            employeelearning.list_batch_runs(batch["id"])[0]["status"],
        )
        self.assertNotIn(batch["id"], main._LEARNING_BATCH_COORDINATORS)

    def test_coordinator_failure_keeps_registry_through_persistent_handoff_faults(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-persistent-handoff-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)
        real_arun = main.db.arun

        async def scenario():
            failed_loop = False
            settlement_entered = asyncio.Event()
            release_settlement = asyncio.Event()
            fifth_handoff_fault = asyncio.Event()
            release_handoff = asyncio.Event()
            handoff_faults = 0
            launches = []

            async def fake_execute(run_id, _tenant_id):
                launches.append(run_id)
                employeelearning.cancel_run(run_id, reason="TEST_COMPLETE")

            async def faulted_arun(fn, *args, **kwargs):
                nonlocal failed_loop, handoff_faults
                if fn is employeelearning.list_batch_runs and not failed_loop:
                    failed_loop = True
                    raise RuntimeError("main loop fault")
                if fn is main._settle_learning_batch_coordinator_failure:
                    settlement_entered.set()
                    await release_settlement.wait()
                    return await real_arun(fn, *args, **kwargs)
                if (
                    fn is employeelearning.get_batch
                    and release_settlement.is_set()
                    and handoff_faults < 5
                ):
                    handoff_faults += 1
                    if handoff_faults == 5:
                        fifth_handoff_fault.set()
                        await release_handoff.wait()
                    raise RuntimeError("persistent handoff DB fault")
                return await real_arun(fn, *args, **kwargs)

            with (
                mock.patch.object(main.db, "arun", new=faulted_arun),
                mock.patch.object(
                    main, "_execute_learning_batch_run", new=fake_execute,
                ),
            ):
                self.assertTrue(main._schedule_employee_learning_batch(batch["id"]))
                old = main._LEARNING_BATCH_COORDINATORS[batch["id"]]
                await asyncio.wait_for(settlement_entered.wait(), timeout=2)
                state = main._resume_employee_learning_batch_state(batch["id"], 1)
                self.assertEqual(1, state["coordinator_generation"])
                self.assertFalse(main._schedule_employee_learning_batch(batch["id"]))
                release_settlement.set()
                await asyncio.wait_for(fifth_handoff_fault.wait(), timeout=2)
                self.assertIs(old, main._LEARNING_BATCH_COORDINATORS[batch["id"]])
                release_handoff.set()
                with self.assertRaises(RuntimeError):
                    await asyncio.wait_for(old, timeout=3)
                successor = main._LEARNING_BATCH_COORDINATORS.get(batch["id"])
                if successor is not None:
                    await asyncio.wait_for(successor, timeout=3)
                return handoff_faults, launches

        handoff_faults, launches = asyncio.run(scenario())
        self.assertEqual(5, handoff_faults)
        self.assertEqual(1, len(launches))
        self.assertNotIn(batch["id"], main._LEARNING_BATCH_COORDINATORS)
        self.assertNotIn(
            employeelearning.RUN_QUEUED,
            {run["status"] for run in employeelearning.list_batch_runs(batch["id"])},
        )

    def test_child_scheduler_failure_releases_active_marker_and_resume_runs(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-child-scheduler-fault-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)

        async def scenario():
            real_create_task = asyncio.create_task
            failed = False
            launches = []

            async def fake_execute(run_id, _tenant_id):
                launches.append(run_id)
                employeelearning.cancel_run(run_id, reason="TEST_COMPLETE")

            def fail_first_child(coro):
                nonlocal failed
                if not failed:
                    failed = True
                    raise RuntimeError("child scheduler unavailable")
                return real_create_task(coro)

            with mock.patch.object(
                main, "_execute_learning_batch_run", new=fake_execute,
            ):
                self.assertTrue(main._schedule_employee_learning_batch(batch["id"]))
                first = main._LEARNING_BATCH_COORDINATORS[batch["id"]]
                with mock.patch.object(
                    main.asyncio, "create_task", side_effect=fail_first_child,
                ):
                    with self.assertRaises(RuntimeError):
                        await asyncio.wait_for(first, timeout=2)
                self.assertFalse(main._LEARNING_BATCH_ACTIVE_RUNS)
                self.assertEqual(
                    employeelearning.BATCH_PAUSED,
                    employeelearning.get_batch(batch["id"])["status"],
                )
                resumed = await main.employee_learning_batch_resume(batch["id"])
                self.assertTrue(resumed["started"])
                successor = main._LEARNING_BATCH_COORDINATORS[batch["id"]]
                await asyncio.wait_for(successor, timeout=2)
                return launches

        launches = asyncio.run(scenario())
        self.assertEqual(1, len(launches))
        self.assertFalse(main._LEARNING_BATCH_ACTIVE_RUNS)

    def test_child_cancelled_before_first_step_cannot_leak_active_marker(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-child-prestart-cancel-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)

        async def scenario():
            real_create_task = asyncio.create_task
            cancelled_first = False
            launches = []

            async def fake_execute(run_id, _tenant_id):
                launches.append(run_id)
                employeelearning.cancel_run(run_id, reason="TEST_COMPLETE")

            def cancel_first_child(coro):
                nonlocal cancelled_first
                task = real_create_task(coro)
                if not cancelled_first:
                    cancelled_first = True
                    task.cancel()
                return task

            with mock.patch.object(
                main, "_execute_learning_batch_run", new=fake_execute,
            ):
                self.assertTrue(main._schedule_employee_learning_batch(batch["id"]))
                coordinator = main._LEARNING_BATCH_COORDINATORS[batch["id"]]
                with mock.patch.object(
                    main.asyncio, "create_task", side_effect=cancel_first_child,
                ):
                    await asyncio.wait_for(coordinator, timeout=2)
                return launches

        launches = asyncio.run(scenario())
        self.assertEqual(1, len(launches))
        self.assertFalse(main._LEARNING_BATCH_ACTIVE_RUNS)
        self.assertNotIn(batch["id"], main._LEARNING_BATCH_COORDINATORS)

    def test_cancelled_coordinator_pauses_remaining_queue_after_active_child(self):
        preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-coordinator-cancel-001",
            "idxs": [1001, 1002], "max_concurrency": 1,
        }, tenant_id=1)
        batch = main._create_learning_batch_manifest(preview, actor_id=1)

        async def scenario():
            worker_started = asyncio.Event()
            release_worker = asyncio.Event()

            async def held_worker(run_id, _tenant_id):
                worker_started.set()
                await release_worker.wait()
                employeelearning.cancel_run(run_id, reason="TEST_DONE")

            with mock.patch.object(
                main, "_execute_learning_batch_run", new=held_worker,
            ):
                self.assertTrue(main._schedule_employee_learning_batch(batch["id"]))
                coordinator = main._LEARNING_BATCH_COORDINATORS[batch["id"]]
                await asyncio.wait_for(worker_started.wait(), timeout=2)
                coordinator.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await coordinator
                release_worker.set()
                await asyncio.sleep(0)

        asyncio.run(scenario())
        settled = employeelearning.get_batch(batch["id"])
        self.assertEqual(employeelearning.BATCH_PAUSED, settled["status"])
        self.assertEqual(
            1,
            sum(
                run["status"] == employeelearning.RUN_QUEUED
                for run in employeelearning.list_batch_runs(batch["id"])
            ),
        )
        self.assertNotIn(batch["id"], main._LEARNING_BATCH_COORDINATORS)

    def test_startup_detector_is_read_only_for_bulk_single_and_paused_rows(self):
        runnable_preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-startup-orphan-001",
            "idxs": [1001], "max_concurrency": 1,
        }, tenant_id=1)
        runnable = main._create_learning_batch_manifest(
            runnable_preview, actor_id=1,
        )
        already_preview = main._learning_batch_preview_contract({
            "request_key": "schema55-batch-startup-already-paused-001",
            "idxs": [1002], "max_concurrency": 1,
        }, tenant_id=1)
        already = main._create_learning_batch_manifest(
            already_preview, actor_id=1,
        )
        main._pause_learning_batch_without_interrupting(
            already["id"], "manual pause",
        )
        single = employeelearning.create_batch(
            "schema55-single-startup-not-bulk-001",
            budget_cap_points=3,
            tenant_id=1,
        )
        db.execute(
            "UPDATE employee_learning_batch SET metadata_json=? WHERE id=?",
            (
                json.dumps({"schema": "schema55-learning-single-v1"}),
                int(single["id"]),
            ),
        )

        before = [
            employeelearning.get_batch(batch_id)
            for batch_id in (runnable["id"], already["id"], single["id"])
        ]
        self.assertEqual(1, main._detect_orphaned_learning_batches_for_restart())
        after = [
            employeelearning.get_batch(batch_id)
            for batch_id in (runnable["id"], already["id"], single["id"])
        ]
        self.assertEqual(
            [row["status"] for row in before],
            [row["status"] for row in after],
        )
        self.assertEqual(
            [row.get("paused_reason") for row in before],
            [row.get("paused_reason") for row in after],
        )
        recovered = employeelearning.get_batch(runnable["id"])
        self.assertEqual(employeelearning.BATCH_QUEUED, recovered["status"])
        self.assertIsNone(recovered.get("paused_reason"))
        self.assertEqual(
            "manual pause", employeelearning.get_batch(already["id"])["paused_reason"]
        )
        self.assertEqual(
            employeelearning.BATCH_QUEUED,
            employeelearning.get_batch(single["id"])["status"],
        )


if __name__ == "__main__":
    unittest.main()
