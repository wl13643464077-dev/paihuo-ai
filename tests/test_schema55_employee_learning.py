"""Schema 55 learning service contract (RED first, no real web or billing).

The service is deliberately tested against a tiny local schema.  The production
migration may add tenant/audit columns, but these behavioural gates must remain
true: captured web evidence is typed, proposals are inert until CAS approval,
and a batch can be resumed without duplicating a run or exceeding its budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from app import db
from app import employeelearning as learning
from app import learningevidence


class Schema55LearningCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "learning.db")
        db.conn()
        self._create_schema()

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _create_schema(self):
        # Keep this fixture intentionally small.  The implementation must use
        # db.atomic/one/q (and tolerate migration-added columns), not a private
        # connection or an ORM transaction.
        with db.atomic() as conn:
            # db.conn() applies the repository's latest migration on startup.
            # Replace only these four temp-fixture tables so this contract can
            # exercise the richer service columns without touching production
            # migration code.
            conn.executescript(
                """
                DROP TABLE IF EXISTS employee_learning_artifact;
                DROP TABLE IF EXISTS employee_learning_source;
                DROP TABLE IF EXISTS employee_learning_run;
                DROP TABLE IF EXISTS employee_learning_batch;
                """
            )
            conn.executescript(
                """
                CREATE TABLE employee_learning_batch(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id INTEGER NOT NULL DEFAULT 1,
                  idempotency_key TEXT NOT NULL,
                  status TEXT NOT NULL,
                  budget_cap_points REAL NOT NULL,
                  spent_points REAL NOT NULL DEFAULT 0,
                  checkpoint_json TEXT NOT NULL DEFAULT '{}',
                  total_runs INTEGER NOT NULL DEFAULT 0,
                  completed_runs INTEGER NOT NULL DEFAULT 0,
                  paused_reason TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  UNIQUE(tenant_id, idempotency_key)
                );
                CREATE TABLE employee_learning_run(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  batch_id INTEGER NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  employee_idx INTEGER NOT NULL,
                  identity_ref TEXT NOT NULL,
                  base_config_revision INTEGER NOT NULL,
                  base_config_sha256 TEXT NOT NULL,
                  industry_key TEXT,
                  high_risk INTEGER NOT NULL DEFAULT 0,
                  budget_points REAL NOT NULL,
                  spent_points REAL NOT NULL DEFAULT 0,
                  status TEXT NOT NULL,
                  checkpoint_json TEXT NOT NULL DEFAULT '{}',
                  proposal_json TEXT,
                  result_json TEXT NOT NULL DEFAULT '{}',
                  error_code TEXT,
                  expires_at REAL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  UNIQUE(batch_id, employee_idx),
                  UNIQUE(batch_id, idempotency_key)
                );
                CREATE TABLE employee_learning_source(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id INTEGER NOT NULL,
                  canonical_url TEXT NOT NULL,
                  title TEXT NOT NULL,
                  publisher TEXT NOT NULL,
                  authority_level TEXT NOT NULL,
                  published_at TEXT,
                  fetched_at REAL NOT NULL,
                  http_status INTEGER NOT NULL,
                  tls_valid INTEGER NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  excerpt TEXT NOT NULL,
                  capture_event_id TEXT NOT NULL,
                  domain TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  UNIQUE(run_id, canonical_url)
                );
                CREATE TABLE employee_learning_artifact(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  title TEXT NOT NULL,
                  statement TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  source_ids_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  reviewer_id INTEGER,
                  reviewed_at REAL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE learning_activation_marker(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id INTEGER NOT NULL
                );
                """
            )

    @staticmethod
    def _source(i: int, *, authority="official", domain=None):
        domain = domain or f"source{i}.example"
        url = f"https://{domain}/guides/{i}?utm_source=test"
        return {
            "url": url,
            "title": f"行业证据 {i}",
            "publisher": f"发布机构 {i}",
            "authority_level": authority,
            "published_at": "2026-07-01",
            "fetched_at": 1720000000 + i,
            "http_status": 200,
            "tls_valid": True,
            "content_sha256": hashlib.sha256(f"content-{i}".encode()).hexdigest(),
            "excerpt": f"可核查证据摘要 {i}",
            "capture_event_id": f"websearch-event-{i}",
            "capture_provider": "websearch",
            "domain": domain,
        }

    def _sources(self, count=5, *, high_risk=False):
        rows = []
        for i in range(count):
            rows.append(self._source(
                i + 1,
                authority="regulator" if i == 0 or (high_risk and i == 1) else "industry",
                domain=f"evidence{(i % 3) + 1}.example",
            ))
        return rows

    def _research_result(self, *, high_risk=False, count=5):
        return {
            "sources": self._sources(count, high_risk=high_risk),
            "artifacts": [{
                "kind": "capability",
                "title": "可验证能力增量",
                "statement": "依据来源校准阈值并输出升级建议",
                "payload": {"delta": {"capabilities": ["阈值校准"]}},
                "source_indexes": [1, 2],
            }],
        }

    def test_url_and_capture_provenance_are_strictly_typed(self):
        valid = learning.validate_source(self._source(1))
        self.assertEqual("https://source1.example/guides/1", valid["canonical_url"])
        self.assertEqual("source1.example", valid["domain"])
        with self.assertRaises(learning.LearningValidationError):
            learning.validate_source({**self._source(2), "capture_provider": "model"})
        with self.assertRaises(learning.LearningValidationError):
            learning.validate_source({k: v for k, v in self._source(3).items()
                                      if k != "capture_event_id"})
        with self.assertRaises(learning.LearningValidationError):
            learning.validate_source({**self._source(4), "url": "javascript:alert(1)"})

    def test_source_gate_is_five_three_domains_one_authority_and_high_risk_six_two(self):
        self.assertEqual(
            {"sources": 5, "domains": 3, "authoritative": 1},
            learning.source_gate(self._sources(5)),
        )
        with self.assertRaises(learning.LearningValidationError):
            learning.enforce_source_gate(self._sources(4))
        with self.assertRaises(learning.LearningValidationError):
            learning.enforce_source_gate(self._sources(5, high_risk=True), high_risk=True)
        self.assertEqual(
            {"sources": 6, "domains": 3, "authoritative": 2},
            learning.enforce_source_gate(self._sources(6, high_risk=True), high_risk=True),
        )

    def test_batch_run_are_idempotent_budgeted_and_resumable(self):
        batch = learning.create_batch("batch-demo", budget_cap_points=6)
        self.assertEqual(batch["id"], learning.create_batch(
            "batch-demo", budget_cap_points=6)["id"])
        run = learning.create_run(
            batch["id"], "run-1001", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        self.assertEqual(run["id"], learning.create_run(
            batch["id"], "run-1001", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )["id"])
        learning.start_run(run["id"])
        learning.checkpoint(run["id"], {"page": 2, "cursor": "abc"})
        learning.pause_batch(batch["id"], "老板暂停")
        self.assertEqual("paused", learning.get_batch(batch["id"])["status"])
        learning.resume_batch(batch["id"])
        self.assertEqual("queued", learning.get_run(run["id"])["status"])
        # A third reservation would exceed the six-point batch cap once two
        # three-point runs are reserved; repeat reservation is idempotent.
        run2 = learning.create_run(
            batch["id"], "run-1002", employee_idx=1002,
            identity_ref="b" * 64, base_config_revision=3,
            base_config_sha256="d" * 64, industry_key="grocery",
            budget_points=3,
        )
        self.assertEqual(3, learning.reserve_budget(run2["id"]))
        self.assertEqual(3, learning.reserve_budget(run["id"]))
        run3 = learning.create_run(
            batch["id"], "run-1003", employee_idx=1003,
            identity_ref="c" * 64, base_config_revision=3,
            base_config_sha256="f" * 64, industry_key="grocery",
            budget_points=3,
        )
        with self.assertRaises(learning.BudgetExceededError):
            learning.reserve_budget(run3["id"])

    def test_identity_is_exclusive_across_batches_until_terminal(self):
        first_batch = learning.create_batch(
            "tenant-one-batch", budget_cap_points=3, tenant_id=1,
        )
        second_batch = learning.create_batch(
            "tenant-two-batch", budget_cap_points=3, tenant_id=2,
        )
        first = learning.create_run(
            first_batch["id"], "same-browser-request", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        with self.assertRaisesRegex(
            learning.InvalidTransitionError, "未结束",
        ):
            learning.create_run(
                second_batch["id"], "same-browser-request", employee_idx=1001,
                identity_ref="a" * 64, base_config_revision=3,
                base_config_sha256="c" * 64, industry_key="grocery",
                budget_points=3,
            )
        learning.cancel_run(first["id"], "跨批次终态释放")
        second = learning.create_run(
            second_batch["id"], "same-browser-request", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(1, first["batch_id"])
        self.assertEqual(2, second["batch_id"])

    def test_concurrent_cross_batch_create_has_one_atomic_identity_owner(self):
        batches = [
            learning.create_batch(
                f"concurrent-batch-{number}", budget_cap_points=3,
                tenant_id=number,
            )
            for number in (1, 2)
        ]

        def attempt(batch):
            try:
                run = learning.create_run(
                    batch["id"],
                    f"concurrent-run-{batch['id']}",
                    employee_idx=1001,
                    identity_ref="a" * 64,
                    base_config_revision=3,
                    base_config_sha256="c" * 64,
                    industry_key="grocery",
                    budget_points=3,
                )
                return "created", run
            except learning.InvalidTransitionError as exc:
                return "blocked", str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, batches))

        self.assertEqual(1, sum(kind == "created" for kind, _ in results))
        self.assertEqual(1, sum(kind == "blocked" for kind, _ in results))
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_run "
            "WHERE identity_ref=? AND status IN (?,?,?)",
            (
                "a" * 64,
                learning.RUN_QUEUED,
                learning.RUN_RESEARCHING,
                learning.RUN_AWAITING_APPROVAL,
            ),
        )["n"])

        winner = next(value for kind, value in results if kind == "created")
        learning.cancel_run(winner["id"], "终态后释放")
        losing_batch = next(
            batch for batch in batches if int(batch["id"]) != int(winner["batch_id"])
        )
        retry = learning.create_run(
            losing_batch["id"],
            "concurrent-terminal-retry",
            employee_idx=1001,
            identity_ref="a" * 64,
            base_config_revision=3,
            base_config_sha256="c" * 64,
            industry_key="grocery",
            budget_points=3,
        )
        self.assertEqual(learning.RUN_QUEUED, retry["status"])

    def test_research_creates_inert_proposal_and_artifacts_only_link_real_sources(self):
        batch = learning.create_batch("batch-research", budget_cap_points=10)
        run = learning.create_run(
            batch["id"], "run-research", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        learning.start_run(run["id"])
        researcher = Mock(return_value=self._research_result())
        bundle_count_before = db.one(
            "SELECT COUNT(*) AS n FROM employee_role_bundle_revision"
        )["n"]
        result = learning.research_run(run["id"], researcher)
        self.assertEqual("awaiting_approval", result["status"])
        self.assertEqual(5, result["source_count"])
        self.assertEqual(1, result["artifact_count"])
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_artifact"
        )["n"])
        # The service never mutates employee_role_bundle_revision; the parent
        # integration must do so only through approve_run's CAS callback.
        self.assertEqual(bundle_count_before, db.one(
            "SELECT COUNT(*) AS n FROM employee_role_bundle_revision"
        )["n"])
        artifact = db.one("SELECT * FROM employee_learning_artifact")
        self.assertTrue(db.jloads(artifact["source_ids_json"], []))

        with self.assertRaises(learning.LearningValidationError):
            learning.validate_artifact({
                "kind": "skill", "title": "伪来源", "statement": "x",
                "payload": {}, "source_ids": ["model-said-this"],
            }, {1})

    def test_research_terminal_failures_refresh_batch_progress_atomically(self):
        insufficient_batch = learning.create_batch(
            "research-insufficient-progress", budget_cap_points=3,
        )
        insufficient = learning.create_run(
            insufficient_batch["id"], "research-insufficient-run",
            employee_idx=1001, identity_ref="a" * 64,
            base_config_revision=3, base_config_sha256="c" * 64,
            industry_key="grocery", budget_points=3,
        )
        learning.start_run(insufficient["id"])
        with self.assertRaises(learning.LearningValidationError):
            learning.research_run(
                insufficient["id"],
                lambda _: {"sources": [], "artifacts": []},
            )
        self.assertEqual(
            learning.RUN_EVIDENCE_INSUFFICIENT,
            learning.get_run(insufficient["id"])["status"],
        )
        progress = learning.get_batch(insufficient_batch["id"])
        self.assertEqual(1, progress["completed_runs"])
        self.assertEqual(learning.BATCH_COMPLETED, progress["status"])

    def test_typed_evidence_gate_error_code_is_persisted(self):
        batch = learning.create_batch(
            "typed-evidence-error", budget_cap_points=3,
        )
        run = learning.create_run(
            batch["id"], "typed-evidence-error-run", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        learning.start_run(run["id"])

        def no_direct(_context):
            raise learningevidence.EvidenceGateError(
                "EVIDENCE_ZERO_DIRECT", "没有直接证据",
                counts={"direct": 0},
            )

        with self.assertRaises(learningevidence.EvidenceGateError):
            learning.research_run(run["id"], no_direct)
        persisted = learning.get_run(run["id"])
        self.assertEqual(learning.RUN_EVIDENCE_INSUFFICIENT, persisted["status"])
        self.assertEqual("EVIDENCE_ZERO_DIRECT", persisted["error_code"])

        failed_batch = learning.create_batch(
            "research-provider-failed-progress", budget_cap_points=3,
        )
        failed = learning.create_run(
            failed_batch["id"], "research-provider-failed-run",
            employee_idx=1002, identity_ref="b" * 64,
            base_config_revision=3, base_config_sha256="d" * 64,
            industry_key="grocery", budget_points=3,
        )
        learning.start_run(failed["id"])

        def provider_failure(_context):
            raise RuntimeError("provider failed")

        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            learning.research_run(failed["id"], provider_failure)
        self.assertEqual(
            learning.RUN_FAILED, learning.get_run(failed["id"])["status"],
        )
        progress = learning.get_batch(failed_batch["id"])
        self.assertEqual(1, progress["completed_runs"])
        self.assertEqual(learning.BATCH_COMPLETED, progress["status"])

    def test_research_terminal_and_batch_progress_roll_back_together(self):
        batch = learning.create_batch(
            "research-progress-atomic", budget_cap_points=3,
        )
        run = learning.create_run(
            batch["id"], "research-progress-atomic-run", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        learning.start_run(run["id"])
        with patch.object(
            learning, "_refresh_batch_progress",
            side_effect=RuntimeError("progress write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "progress write failed"):
                learning.research_run(
                    run["id"], lambda _: {"sources": [], "artifacts": []},
                )
        self.assertEqual(
            learning.RUN_RESEARCHING, learning.get_run(run["id"])["status"],
        )
        progress = learning.get_batch(batch["id"])
        self.assertEqual(0, progress["completed_runs"])
        self.assertEqual(learning.BATCH_RUNNING, progress["status"])

    def test_approve_requires_external_cas_and_generates_effective_delta(self):
        batch = learning.create_batch("batch-approve", budget_cap_points=10)
        run = learning.create_run(
            batch["id"], "run-approve", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        learning.start_run(run["id"])
        learning.research_run(run["id"], lambda _: self._research_result())
        with self.assertRaises(learning.ApprovalRequiredError):
            learning.approve_run(run["id"], None, reviewer_id=17)
        with self.assertRaises(learning.LearningValidationError):
            learning.approve_run(run["id"], Mock(), reviewer_id=0)
        self.assertEqual(
            learning.RUN_AWAITING_APPROVAL,
            learning.get_run(run["id"])["status"],
        )
        seen = {}

        def cas(**kwargs):
            seen.update(kwargs)
            return {
                "status": "activated",
                "new_config_revision": 4,
                "new_config_sha256": "e" * 64,
                "bundle_sha256": "b" * 64,
            }

        reviewed_after = time.time()
        approved = learning.approve_run(run["id"], cas, reviewer_id=17)
        self.assertEqual("activated", approved["status"])
        self.assertEqual(4, approved["new_config_revision"])
        self.assertEqual("a" * 64, seen["expected_identity_ref"])
        self.assertEqual(3, seen["expected_config_revision"])
        self.assertTrue(seen["effective_role_bundle_delta"]["artifact_ids"])
        self.assertEqual("activated", learning.get_run(run["id"])["status"])
        artifact = learning.get_run(run["id"])["artifacts"][0]
        self.assertEqual(17, artifact["reviewer_id"])
        self.assertGreaterEqual(float(artifact["reviewed_at"]), reviewed_after)

    def test_approval_is_atomic_and_awaiting_proposal_is_immutable(self):
        batch = learning.create_batch("batch-atomic", budget_cap_points=10)
        run = learning.create_run(
            batch["id"], "run-atomic", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        learning.start_run(run["id"])
        learning.research_run(run["id"], lambda _: self._research_result())

        source_id = int(db.one(
            "SELECT id FROM employee_learning_source WHERE run_id=? ORDER BY id LIMIT 1",
            (run["id"],),
        )["id"])
        with self.assertRaises(learning.InvalidTransitionError):
            learning.draft_artifacts(run["id"], [{
                "kind": "skill",
                "title": "待审批后偷加的技能",
                "statement": "这条不得进入已冻结提案",
                "payload": {"delta": {"skills": ["禁止追加"]}},
                "source_ids": [source_id],
            }])

        def activation(**kwargs):
            db.execute(
                "INSERT INTO learning_activation_marker(run_id) VALUES(?)",
                (kwargs["run_id"],),
            )
            return {
                "status": "activated",
                "new_config_revision": 4,
                "new_config_sha256": "e" * 64,
                "bundle_sha256": "b" * 64,
            }

        with patch.object(
            learning, "_set_artifact_status", side_effect=RuntimeError("fault")
        ):
            with self.assertRaises(RuntimeError):
                learning.approve_run(run["id"], activation, reviewer_id=19)

        self.assertEqual(0, db.one(
            "SELECT COUNT(*) AS n FROM learning_activation_marker"
        )["n"])
        self.assertEqual(
            "awaiting_approval", learning.get_run(run["id"])["status"]
        )
        self.assertEqual("proposed", db.one(
            "SELECT status FROM employee_learning_artifact WHERE run_id=?",
            (run["id"],),
        )["status"])
        self.assertIsNone(db.one(
            "SELECT reviewer_id FROM employee_learning_artifact WHERE run_id=?",
            (run["id"],),
        )["reviewer_id"])

    def test_restart_fails_inflight_run_and_releases_internal_budget(self):
        batch = learning.create_batch("batch-restart", budget_cap_points=6)
        inflight = learning.create_run(
            batch["id"], "run-restart", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        queued = learning.create_run(
            batch["id"], "run-queued", employee_idx=1002,
            identity_ref="b" * 64, base_config_revision=3,
            base_config_sha256="d" * 64, industry_key="grocery",
            budget_points=3,
        )
        learning.reserve_budget(inflight["id"])
        learning.start_run(inflight["id"])

        self.assertEqual(1, learning.recover_interrupted_runs())
        self.assertEqual("failed", learning.get_run(inflight["id"])["status"])
        self.assertEqual(0, learning.get_run(inflight["id"])["spent_points"])
        progress = learning.get_batch(batch["id"])
        self.assertEqual(1, progress["completed_runs"])
        self.assertEqual(learning.BATCH_RUNNING, progress["status"])

    def test_restart_terminal_and_batch_progress_roll_back_together(self):
        batch = learning.create_batch(
            "restart-progress-atomic", budget_cap_points=3,
        )
        run = learning.create_run(
            batch["id"], "restart-progress-atomic-run", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        learning.reserve_budget(run["id"])
        learning.start_run(run["id"])
        with patch.object(
            learning, "_refresh_batch_progress",
            side_effect=RuntimeError("progress write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "progress write failed"):
                learning.recover_interrupted_runs()
        current = learning.get_run(run["id"])
        self.assertEqual(learning.RUN_RESEARCHING, current["status"])
        self.assertEqual(3, current["spent_points"])
        progress = learning.get_batch(batch["id"])
        self.assertEqual(0, progress["completed_runs"])
        self.assertEqual(3, progress["spent_points"])
        self.assertEqual(learning.BATCH_RUNNING, progress["status"])

    def test_terminal_progress_does_not_resume_a_paused_batch(self):
        batch = learning.create_batch(
            "paused-progress", budget_cap_points=6, tenant_id=1,
        )
        first = learning.create_run(
            batch["id"], "paused-first", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=1,
            base_config_sha256="b" * 64, industry_key="tea_coffee",
            budget_points=3,
        )
        queued = learning.create_run(
            batch["id"], "paused-second", employee_idx=1002,
            identity_ref="c" * 64, base_config_revision=1,
            base_config_sha256="d" * 64, industry_key="tea_coffee",
            budget_points=3,
        )
        learning.start_run(first["id"])
        learning.pause_batch(batch["id"], "人工暂停")
        learning.cancel_run(first["id"], "测试终态")
        self.assertEqual(
            learning.BATCH_PAUSED, learning.get_batch(batch["id"])["status"]
        )
        self.assertEqual("queued", learning.get_run(queued["id"])["status"])
        self.assertEqual(0, learning.get_batch(batch["id"])["spent_points"])

    def test_reject_stale_and_expire_are_terminal_and_cannot_activate(self):
        batch = learning.create_batch("batch-terminal", budget_cap_points=20)
        run = learning.create_run(
            batch["id"], "run-reject", employee_idx=1001,
            identity_ref="a" * 64, base_config_revision=3,
            base_config_sha256="c" * 64, industry_key="grocery",
            budget_points=3,
        )
        learning.start_run(run["id"])
        learning.research_run(run["id"], lambda _: self._research_result())
        with self.assertRaises(learning.LearningValidationError):
            learning.reject_run(run["id"], "证据不适用", reviewer_id=False)
        reviewed_after = time.time()
        learning.reject_run(run["id"], "证据不适用", reviewer_id=23)
        rejected = learning.get_run(run["id"])
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual(23, rejected["reviewer_id"])
        self.assertGreaterEqual(float(rejected["reviewed_at"]), reviewed_after)
        self.assertEqual(23, rejected["artifacts"][0]["reviewer_id"])
        self.assertEqual(
            rejected["reviewed_at"], rejected["artifacts"][0]["reviewed_at"]
        )
        with self.assertRaises(learning.InvalidTransitionError):
            learning.approve_run(run["id"], lambda **_: {}, reviewer_id=23)

        expiring = learning.create_run(
            batch["id"], "run-expire", employee_idx=1002,
            identity_ref="b" * 64, base_config_revision=3,
            base_config_sha256="d" * 64, industry_key="grocery",
            budget_points=3, expires_at=time.time() - 1,
        )
        learning.start_run(expiring["id"])
        learning.research_run(expiring["id"], lambda _: self._research_result())
        self.assertEqual("expired", learning.expire_run(expiring["id"])["status"])

    def test_expire_run_honors_effective_now_refreshes_and_releases_owner(self):
        expiry = 2_000_000_000.0
        first_batch = learning.create_batch(
            "expiry-owner-first", budget_cap_points=3, tenant_id=1,
        )
        run = learning.create_run(
            first_batch["id"], "expiry-owner-run", employee_idx=1001,
            identity_ref="e" * 64, base_config_revision=3,
            base_config_sha256="f" * 64, industry_key="grocery",
            budget_points=3, expires_at=expiry,
        )
        second_batch = learning.create_batch(
            "expiry-owner-second", budget_cap_points=3, tenant_id=2,
        )

        with self.assertRaisesRegex(
            learning.InvalidTransitionError, "\u5c1a\u672a\u5230\u671f",
        ):
            learning.expire_run(run["id"], now=expiry - 1)
        self.assertEqual(learning.RUN_QUEUED, learning.get_run(run["id"])["status"])
        self.assertEqual(0, learning.get_batch(first_batch["id"])["completed_runs"])
        with self.assertRaisesRegex(
            learning.InvalidTransitionError, "\u672a\u7ed3\u675f",
        ):
            learning.create_run(
                second_batch["id"], "owner-still-held", employee_idx=1001,
                identity_ref="e" * 64, base_config_revision=3,
                base_config_sha256="f" * 64, industry_key="grocery",
                budget_points=3,
            )

        with patch.object(learning, "_now", return_value=expiry + 10):
            expired = learning.expire_run(run["id"], now=expiry)
        self.assertEqual(learning.RUN_EXPIRED, expired["status"])
        first_progress = learning.get_batch(first_batch["id"])
        self.assertEqual(1, first_progress["completed_runs"])
        self.assertEqual(learning.BATCH_COMPLETED, first_progress["status"])

        with patch.object(learning, "_now", return_value=expiry + 20):
            repeated = learning.expire_run(run["id"], now=expiry + 1)
        self.assertEqual(learning.RUN_EXPIRED, repeated["status"])
        self.assertEqual(expired["updated_at"], repeated["updated_at"])
        self.assertEqual(
            first_progress["updated_at"],
            learning.get_batch(first_batch["id"])["updated_at"],
        )

        successor = learning.create_run(
            second_batch["id"], "owner-released", employee_idx=1001,
            identity_ref="e" * 64, base_config_revision=3,
            base_config_sha256="f" * 64, industry_key="grocery",
            budget_points=3,
        )
        self.assertEqual(learning.RUN_QUEUED, successor["status"])


if __name__ == "__main__":
    unittest.main()
