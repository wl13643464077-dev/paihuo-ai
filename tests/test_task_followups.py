"""数字员工连续协作：会话收养、幂等追问、单活跃版本与满意收口。"""
import asyncio
import ast
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import billing, db, departments, employeeidentity, taskthreads
from app.skills import registry


class TaskThreadCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "threads.db")
        db.conn()
        self._install_schema_contract()
        for tid, name, balance in (
            (1, "平台", 0),
            (2, "企业甲", 10),
            (3, "企业乙", 10),
        ):
            db.insert("tenants", {
                "id": tid,
                "name": name,
                "balance": balance,
            })

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    @staticmethod
    def _install_schema_contract():
        columns = {row["name"] for row in db.q("PRAGMA table_info(task)")}
        for name, declaration in (
            ("thread_id", "INTEGER"),
            ("revision_no", "INTEGER NOT NULL DEFAULT 1"),
            ("phase", "TEXT NOT NULL DEFAULT 'delivery'"),
            ("request_key", "TEXT"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE task ADD COLUMN {name} {declaration}")
        db.q("""
            CREATE TABLE IF NOT EXISTS task_thread(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id INTEGER NOT NULL,
              emp_idx INTEGER NOT NULL,
              root_task_id INTEGER NOT NULL,
              current_task_id INTEGER NOT NULL,
              accepted_task_id INTEGER,
              status TEXT NOT NULL DEFAULT 'active',
              revision_count INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER,
              satisfied_at REAL,
              created_at REAL,
              updated_at REAL
            )
        """)
        db.q(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_thread_root "
            "ON task_thread(tenant_id,root_task_id)"
        )
        db.q(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_request_key "
            "ON task(tenant_id,request_key) WHERE request_key IS NOT NULL"
        )
        db.q(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_thread_revision "
            "ON task(thread_id,revision_no) WHERE thread_id IS NOT NULL"
        )

    def _task(
        self,
        direction="生成本周经营复盘",
        *,
        tenant=2,
        emp=0,
        status="done",
        source_task_id=None,
        output="第一版交付",
        created_by=20,
        material="",
    ):
        employee = employeeidentity.active_employee(emp)
        self.assertIsNotNone(employee)
        return db.insert("task", {
            "tenant_id": tenant,
            "emp_idx": emp,
            **employeeidentity.task_fields(employee),
            "brief_json": json.dumps({
                "direction": direction,
                "industry": "餐饮",
                "material": material,
            }, ensure_ascii=False),
            "status": status,
            "output_md": output,
            "summary_md": "简短速览",
            "source_task_id": source_task_id,
            "billing_status": "charged",
            "billing_points": 1,
            "created_by": created_by,
        })

    def _charged_creator(self, tenant=2, calls=None):
        def create(task_data, note):
            if calls is not None:
                calls.append((dict(task_data), note))
            task_id = db.insert("task", {
                **task_data,
                "status": "pending_charge",
                "billing_status": "pending",
                "billing_points": 1,
                "created_by": 20,
            })

            def claim(connection):
                return connection.execute(
                    "UPDATE task SET status='queued',billing_status='charged' "
                    "WHERE id=? AND status='pending_charge' "
                    "AND billing_status='pending'",
                    (task_id,),
                ).rowcount == 1

            charged = billing.charge_if_claimed(
                "expert_task",
                tenant,
                claim,
                note=note,
                points=1,
            )
            if not charged:
                raise RuntimeError("charge claim lost")
            return task_id

        return create

    def test_done_legacy_task_is_adopted_lazily(self):
        root = self._task()
        self.assertIsNone(db.one(
            "SELECT thread_id FROM task WHERE id=?", (root,)
        )["thread_id"])

        summary = taskthreads.ensure_thread(root, 2, actor_id=20, now=100)

        self.assertEqual("active", summary["status"])
        self.assertEqual(root, summary["root_task_id"])
        self.assertEqual(root, summary["current_task_id"])
        self.assertEqual(1, summary["revision_count"])
        stored = db.one(
            "SELECT thread_id,revision_no,phase FROM task WHERE id=?", (root,)
        )
        self.assertEqual(summary["thread_id"], stored["thread_id"])
        self.assertEqual(1, stored["revision_no"])
        self.assertEqual("delivery", stored["phase"])

        replay = taskthreads.ensure_thread(root, 2, actor_id=20, now=101)
        self.assertEqual(summary["thread_id"], replay["thread_id"])
        self.assertEqual(
            1,
            db.one("SELECT COUNT(*) n FROM task_thread")["n"],
        )

    def test_legacy_linear_redo_chain_is_adopted_without_branching(self):
        first = self._task(direction="原始任务")
        second = self._task(direction="原始任务", source_task_id=first)
        third = self._task(direction="原始任务", source_task_id=second)

        summary = taskthreads.ensure_thread(second, 2, actor_id=20)

        self.assertEqual(first, summary["root_task_id"])
        self.assertEqual(third, summary["current_task_id"])
        self.assertEqual(3, summary["revision_count"])
        rows = db.q(
            "SELECT id,revision_no,phase,thread_id FROM task "
            "WHERE id IN (?,?,?) ORDER BY revision_no",
            (first, second, third),
        )
        self.assertEqual([1, 2, 3], [row["revision_no"] for row in rows])
        self.assertEqual(
            ["delivery", "revision", "revision"],
            [row["phase"] for row in rows],
        )
        self.assertEqual(1, len({row["thread_id"] for row in rows}))

    def test_legacy_branch_is_rejected_instead_of_silently_merged(self):
        root = self._task()
        self._task(source_task_id=root)
        self._task(source_task_id=root)

        with self.assertRaises(taskthreads.ThreadConflict) as caught:
            taskthreads.ensure_thread(root, 2)
        self.assertEqual("legacy_branch", caught.exception.code)
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM task_thread")["n"])

    def test_followup_charges_once_and_advances_thread_in_same_transaction(self):
        root = self._task(output="A" * 13000)
        calls = []

        result = taskthreads.create_followup(
            root,
            2,
            "followup-00000001",
            "把整改责任人和时限补齐",
            self._charged_creator(calls=calls),
            actor_id=20,
            expected_emp_idx=0,
            now=200,
        )

        self.assertTrue(result["created"])
        self.assertEqual(1, len(calls))
        followup = db.one("SELECT * FROM task WHERE id=?", (result["task_id"],))
        self.assertEqual(root, followup["source_task_id"])
        self.assertEqual(result["thread"]["thread_id"], followup["thread_id"])
        self.assertEqual(2, followup["revision_no"])
        self.assertEqual("revision", followup["phase"])
        self.assertEqual("queued", followup["status"])
        brief = db.jloads(followup["brief_json"])
        self.assertEqual(12000, len(brief["prev_excerpt"]))
        self.assertEqual(
            "把整改责任人和时限补齐",
            brief["feedback"],
        )
        self.assertEqual(
            9,
            db.one("SELECT balance FROM tenants WHERE id=2")["balance"],
        )

        replay = taskthreads.create_followup(
            root,
            2,
            "followup-00000001",
            "  把整改责任人和时限补齐  ",
            self._charged_creator(calls=calls),
            actor_id=20,
            expected_emp_idx=0,
            now=201,
        )
        self.assertFalse(replay["created"])
        self.assertEqual(result["task_id"], replay["task_id"])
        self.assertEqual(1, len(calls))
        self.assertEqual(
            9,
            db.one("SELECT balance FROM tenants WHERE id=2")["balance"],
        )

    def test_request_key_cannot_be_reused_for_changed_feedback_or_actor(self):
        root = self._task()
        creator = self._charged_creator()
        taskthreads.create_followup(
            root, 2, "followup-00000002", "第一条意见", creator,
            actor_id=20,
        )

        with self.assertRaises(taskthreads.IdempotencyConflict):
            taskthreads.create_followup(
                root, 2, "followup-00000002", "换一条意见", creator,
                actor_id=20,
            )
        with self.assertRaises(taskthreads.IdempotencyConflict):
            taskthreads.create_followup(
                root, 2, "followup-00000002", "第一条意见", creator,
                actor_id=21,
            )

    def test_stale_revision_or_active_revision_cannot_open_another_followup(self):
        root = self._task()
        creator = self._charged_creator()
        first = taskthreads.create_followup(
            root, 2, "followup-00000003", "继续优化", creator,
            actor_id=20,
        )

        with self.assertRaises(taskthreads.ThreadConflict) as stale:
            taskthreads.create_followup(
                root, 2, "followup-00000004", "并发另一版", creator,
                actor_id=20,
            )
        self.assertEqual("stale_revision", stale.exception.code)
        with self.assertRaises(taskthreads.ThreadConflict) as active:
            taskthreads.create_followup(
                first["task_id"],
                2,
                "followup-00000005",
                "上一版还没完成",
                creator,
                actor_id=20,
            )
        self.assertEqual("task_not_done", active.exception.code)
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM task WHERE request_key IS NOT NULL"
            )["n"],
        )

    def test_concurrent_different_keys_create_only_one_next_revision(self):
        root = self._task()
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def submit(suffix):
            barrier.wait()
            try:
                results.append(taskthreads.create_followup(
                    root,
                    2,
                    f"followup-concurrent-{suffix}",
                    f"意见{suffix}",
                    self._charged_creator(),
                    actor_id=20,
                ))
            except taskthreads.ThreadConflict as exc:
                errors.append(exc.code)

        workers = [
            threading.Thread(target=submit, args=(suffix,))
            for suffix in ("a", "b")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual(1, len(results))
        self.assertEqual(["stale_revision"], errors)
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM task WHERE request_key IS NOT NULL"
            )["n"],
        )
        self.assertEqual(
            9,
            db.one("SELECT balance FROM tenants WHERE id=2")["balance"],
        )

    def test_concurrent_same_key_is_one_creation_plus_one_replay(self):
        root = self._task()
        barrier = threading.Barrier(2)
        results = []

        def submit():
            barrier.wait()
            results.append(taskthreads.create_followup(
                root,
                2,
                "followup-same-key-0001",
                "用同一条修改意见",
                self._charged_creator(),
                actor_id=20,
            ))

        workers = [threading.Thread(target=submit) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual([False, True], sorted(
            result["created"] for result in results
        ))
        self.assertEqual(1, len({result["task_id"] for result in results}))
        self.assertEqual(
            1,
            db.one(
                "SELECT COUNT(*) n FROM task WHERE request_key=?",
                ("followup-same-key-0001",),
            )["n"],
        )
        self.assertEqual(
            9,
            db.one("SELECT balance FROM tenants WHERE id=2")["balance"],
        )

    def test_callback_failure_rolls_back_adoption_task_and_charge(self):
        root = self._task()

        def broken_creator(task_data, note):
            self._charged_creator()(task_data, note)
            raise RuntimeError("worker handoff preparation failed")

        with self.assertRaises(RuntimeError):
            taskthreads.create_followup(
                root,
                2,
                "followup-rollback-0001",
                "会回滚",
                broken_creator,
                actor_id=20,
            )

        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM task_thread")["n"])
        self.assertIsNone(db.one(
            "SELECT thread_id FROM task WHERE id=?", (root,)
        )["thread_id"])
        self.assertEqual(
            0,
            db.one(
                "SELECT COUNT(*) n FROM task WHERE request_key IS NOT NULL"
            )["n"],
        )
        self.assertEqual(
            10,
            db.one("SELECT balance FROM tenants WHERE id=2")["balance"],
        )

    def test_tenant_employee_and_created_row_contract_are_rechecked(self):
        root = self._task()
        with self.assertRaises(taskthreads.TaskThreadNotFound):
            taskthreads.ensure_thread(root, 3)
        with self.assertRaises(taskthreads.ThreadConflict) as employee:
            taskthreads.create_followup(
                root,
                2,
                "followup-employee-0001",
                "继续",
                self._charged_creator(),
                expected_emp_idx=999,
            )
        self.assertEqual("employee_mismatch", employee.exception.code)

        def corrupt_creator(task_data, _note):
            return db.insert("task", {
                **task_data,
                "tenant_id": 3,
                "status": "queued",
                "billing_status": "charged",
            })

        with self.assertRaises(taskthreads.ThreadConflict) as corrupt:
            taskthreads.create_followup(
                root,
                2,
                "followup-corrupt-0001",
                "继续",
                corrupt_creator,
            )
        self.assertEqual("created_task_mismatch", corrupt.exception.code)
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM task_thread")["n"])

    def test_core_employee_zero_can_continue(self):
        root = self._task(emp=0)
        result = taskthreads.create_followup(
            root,
            2,
            "followup-core-zero-0001",
            "再加一组可执行选题",
            self._charged_creator(),
            expected_emp_idx=0,
        )
        self.assertTrue(result["created"])
        self.assertEqual(0, result["thread"]["emp_idx"])

    def test_satisfied_closes_current_done_revision_idempotently(self):
        root = self._task()
        followup = taskthreads.create_followup(
            root,
            2,
            "followup-satisfied-0001",
            "补上截止日期",
            self._charged_creator(),
            actor_id=20,
        )
        db.execute(
            "UPDATE task SET status='done',output_md='第二版交付' WHERE id=?",
            (followup["task_id"],),
        )

        closed = taskthreads.mark_satisfied(
            followup["task_id"], 2, actor_id=20, now=300
        )
        self.assertEqual("satisfied", closed["status"])
        self.assertEqual(followup["task_id"], closed["accepted_task_id"])
        replay = taskthreads.mark_satisfied(
            followup["task_id"], 2, actor_id=20, now=301
        )
        self.assertEqual(closed["accepted_task_id"], replay["accepted_task_id"])

        with self.assertRaises(taskthreads.ThreadConflict) as satisfied:
            taskthreads.create_followup(
                followup["task_id"],
                2,
                "followup-after-satisfied",
                "还要改",
                self._charged_creator(),
                actor_id=20,
            )
        self.assertEqual("thread_satisfied", satisfied.exception.code)

    def test_exhausted_failed_leaf_continues_from_latest_delivered_revision(self):
        root = self._task(output="第一版可用交付")
        failed = taskthreads.create_followup(
            root,
            2,
            "followup-failed-leaf-0001",
            "先生成第二版",
            self._charged_creator(),
            actor_id=20,
        )
        db.execute(
            "UPDATE task SET status='failed',billing_status='refunded',"
            "retry_count=3,output_md='稳定失败文案' WHERE id=?",
            (failed["task_id"],),
        )

        summary = taskthreads.thread_summary_for_task(failed["task_id"], 2)
        self.assertTrue(summary["can_continue"])
        self.assertTrue(summary["can_accept"])
        self.assertEqual(root, summary["resume_task_id"])
        self.assertEqual(failed["task_id"], summary["failed_current_task_id"])

        resumed = taskthreads.create_followup(
            failed["task_id"],
            2,
            "followup-failed-leaf-0002",
            "从第一版继续，补充负责人",
            self._charged_creator(),
            actor_id=20,
        )
        third = db.one("SELECT * FROM task WHERE id=?", (resumed["task_id"],))
        third_brief = db.jloads(third["brief_json"], {})
        self.assertEqual(3, third["revision_no"])
        self.assertEqual(failed["task_id"], third["source_task_id"])
        self.assertEqual("第一版可用交付", third_brief["prev_excerpt"])

    def test_exhausted_failed_leaf_can_accept_latest_delivered_revision(self):
        root = self._task(output="已可用的第一版")
        failed = taskthreads.create_followup(
            root,
            2,
            "followup-failed-accept-0001",
            "尝试第二版",
            self._charged_creator(),
            actor_id=20,
        )
        db.execute(
            "UPDATE task SET status='failed',billing_status='refunded',"
            "retry_count=3 WHERE id=?",
            (failed["task_id"],),
        )

        closed = taskthreads.mark_satisfied(root, 2, actor_id=20)
        self.assertEqual("satisfied", closed["status"])
        self.assertEqual(root, closed["accepted_task_id"])
        self.assertEqual(failed["task_id"], closed["current_task_id"])
        replay = taskthreads.mark_satisfied(root, 2, actor_id=20)
        self.assertEqual(root, replay["accepted_task_id"])

    def test_failed_leaf_with_free_retry_does_not_offer_paid_followup(self):
        root = self._task()
        failed = taskthreads.create_followup(
            root,
            2,
            "followup-free-retry-0001",
            "尝试第二版",
            self._charged_creator(),
        )
        db.execute(
            "UPDATE task SET status='failed',billing_status='refunded',"
            "retry_count=2 WHERE id=?",
            (failed["task_id"],),
        )
        summary = taskthreads.thread_summary_for_task(failed["task_id"], 2)
        self.assertFalse(summary["can_continue"])
        self.assertFalse(summary["can_accept"])
        self.assertEqual("free_retry_available", summary["continue_blocked_by"])
        with self.assertRaises(taskthreads.ThreadConflict) as caught:
            taskthreads.create_followup(
                failed["task_id"],
                2,
                "followup-free-retry-0002",
                "不绕过免费重试",
                self._charged_creator(),
            )
        self.assertEqual("free_retry_available", caught.exception.code)

    def test_summary_is_bounded_metadata_and_never_returns_historical_body(self):
        root = self._task(output="业务秘密正文")
        first = taskthreads.create_followup(
            root,
            2,
            "followup-summary-0001",
            "修改意见",
            self._charged_creator(),
        )
        summary = taskthreads.thread_summary_for_task(
            first["task_id"], 2, limit=1
        )

        self.assertEqual(2, summary["revision_count"])
        self.assertEqual(1, len(summary["revisions"]))
        self.assertTrue(summary["revisions_truncated"])
        forbidden = {
            "brief_json", "output_md", "summary_md", "feedback",
            "prev_excerpt", "direction",
        }
        self.assertFalse(forbidden & set(summary))
        self.assertFalse(forbidden & set(summary["revisions"][0]))
        self.assertNotIn(
            "业务秘密正文",
            json.dumps(summary, ensure_ascii=False),
        )

    def test_delete_guards_preserve_thread_anchors_and_legacy_children(self):
        legacy = self._task()
        self._task(source_task_id=legacy)
        legacy_guard = taskthreads.task_deletion_guard(legacy, 2)
        self.assertFalse(legacy_guard["allowed"])
        self.assertEqual("legacy_parent", legacy_guard["code"])

        root = self._task(direction="新会话")
        followup = taskthreads.create_followup(
            root,
            2,
            "followup-delete-0001",
            "继续",
            self._charged_creator(),
        )
        self.assertEqual(
            "thread_root",
            taskthreads.task_deletion_guard(root, 2)["code"],
        )
        self.assertEqual(
            "thread_current",
            taskthreads.task_deletion_guard(followup["task_id"], 2)["code"],
        )
        self.assertFalse(
            taskthreads.task_hard_delete_guard(root, 2)["allowed"]
        )

        standalone = self._task(direction="独立历史")
        self.assertTrue(
            taskthreads.task_deletion_guard(standalone, 2)["allowed"]
        )
        self.assertTrue(
            taskthreads.task_hard_delete_guard(standalone, 2)["allowed"]
        )

    def test_disabled_employee_cannot_open_a_new_revision(self):
        root = self._task(emp=0)
        db.insert("employee_config", {"idx": 0, "enabled": 0})
        calls = []

        summary = taskthreads.thread_summary_for_task(root, 2)
        self.assertFalse(summary["can_continue"])
        self.assertEqual("employee_disabled", summary["continue_blocked_by"])
        with self.assertRaises(taskthreads.ThreadConflict) as caught:
            taskthreads.create_followup(
                root,
                2,
                "followup-disabled-0001",
                "继续修改",
                self._charged_creator(calls=calls),
                actor_id=20,
            )

        self.assertEqual("employee_disabled", caught.exception.code)
        self.assertEqual([], calls)
        self.assertEqual(10, db.one(
            "SELECT balance FROM tenants WHERE id=2"
        )["balance"])

    def test_request_key_compares_full_revision_material_not_only_truncated_merge(self):
        root = self._task(material="O" * taskthreads.MAX_MATERIAL_CHARS)
        shared = "S" * (taskthreads.MAX_MATERIAL_CHARS // 2)
        first_material = shared + "A" * (taskthreads.MAX_MATERIAL_CHARS // 2)
        changed_tail = shared + "B" * (taskthreads.MAX_MATERIAL_CHARS // 2)
        key = "followup-material-tail-0001"
        taskthreads.create_followup(
            root,
            2,
            key,
            "保持同一修改意见",
            self._charged_creator(),
            material=first_material,
            actor_id=20,
        )
        with self.assertRaises(taskthreads.IdempotencyConflict) as caught:
            taskthreads.create_followup(
                root,
                2,
                key,
                "保持同一修改意见",
                self._charged_creator(),
                material=changed_tail,
                actor_id=20,
            )
        self.assertEqual("request_key_reused", caught.exception.code)

    def test_inspection_employee_only_uses_inspection_workbench(self):
        inspection_task = self._task(emp=10)
        summary = taskthreads.thread_summary_for_task(inspection_task, 2)
        self.assertFalse(summary["can_continue"])
        self.assertFalse(summary["can_accept"])
        self.assertEqual(
            "inspection_workbench_required",
            summary["continue_blocked_by"],
        )

        with self.assertRaises(taskthreads.ThreadConflict) as followup:
            taskthreads.create_followup(
                inspection_task,
                2,
                "inspection-followup-0001",
                "继续修改",
                self._charged_creator(),
            )
        self.assertEqual(
            "inspection_workbench_required", followup.exception.code
        )
        with self.assertRaises(taskthreads.ThreadConflict) as accepted:
            taskthreads.mark_satisfied(inspection_task, 2)
        self.assertEqual(
            "inspection_workbench_required", accepted.exception.code
        )
        self.assertEqual(
            "inspection_audit_record",
            taskthreads.task_deletion_guard(inspection_task, 2)["code"],
        )
        self.assertFalse(
            taskthreads.task_hard_delete_guard(inspection_task, 2)["allowed"]
        )
        self.assertEqual(0, db.one(
            "SELECT COUNT(*) n FROM task_thread"
        )["n"])

    def test_each_revision_inherits_material_and_exact_previous_delivery(self):
        root = self._task(material="首版现场资料", output="第一版完整交付")
        second = taskthreads.create_followup(
            root,
            2,
            "followup-material-0001",
            "先补责任人",
            self._charged_creator(),
            material="第二轮新增资料",
        )
        db.execute(
            "UPDATE task SET status='done',output_md=? WHERE id=?",
            ("第二版真实交付\n含门店整改负责人", second["task_id"]),
        )
        third = taskthreads.create_followup(
            second["task_id"],
            2,
            "followup-material-0002",
            "再补截止时间",
            self._charged_creator(),
            material="第三轮新增资料",
        )
        brief = db.jloads(db.one(
            "SELECT brief_json FROM task WHERE id=?", (third["task_id"],)
        )["brief_json"])

        self.assertIn("首版现场资料", brief["material"])
        self.assertIn("第二轮新增资料", brief["material"])
        self.assertIn("第三轮新增资料", brief["material"])
        self.assertEqual(
            "第二版真实交付\n含门店整改负责人",
            brief["prev_excerpt"],
        )

        core_bundle = registry.solo_prompt(
            0, brief, "内部技能秘密", "企业知识秘密"
        )
        industry_bundle = departments.build_task_prompt(
            departments.get(101),
            brief,
            "内部技能秘密",
            "企业知识秘密",
            [],
        )
        for bundle in (core_bundle, industry_bundle):
            with self.subTest(bundle=type(bundle).__name__):
                self.assertIn(brief["prev_excerpt"], bundle.user)
                self.assertIn("不可信业务数据", bundle.user)
                self.assertNotIn(brief["prev_excerpt"], bundle.system)
                self.assertNotIn(brief["prev_excerpt"], bundle.research)
                self.assertNotIn(
                    brief["prev_excerpt"], "\n".join(bundle.sensitive)
                )

    def test_long_first_material_cannot_push_current_revision_material_out_of_prompt(self):
        root = self._task(material="旧材料" * 1600, output="首版交付")
        current_material = "本轮新数据" + ("新" * 4200) + "CURRENT-REVISION-SENTINEL"
        result = taskthreads.create_followup(
            root,
            2,
            "followup-current-material-0001",
            "必须根据本轮新数据修订",
            self._charged_creator(),
            material=current_material,
        )
        brief = db.jloads(db.one(
            "SELECT brief_json FROM task WHERE id=?", (result["task_id"],)
        )["brief_json"])
        self.assertEqual(current_material, brief["revision_material"])
        for bundle in (
            registry.solo_prompt(0, brief, "内部技能", "企业知识"),
            departments.build_task_prompt(
                departments.get(101), brief, "内部技能", "企业知识", [],
            ),
        ):
            self.assertIn("CURRENT-REVISION-SENTINEL", bundle.user)
            self.assertIn("本轮新增材料（优先落实）", bundle.user)
            self.assertNotIn("CURRENT-REVISION-SENTINEL", bundle.system)

    def test_soft_delete_race_cannot_orphan_a_new_thread(self):
        root = self._task()
        barrier = threading.Barrier(2)
        outcomes = []
        unexpected = []

        def followup():
            barrier.wait()
            try:
                result = taskthreads.create_followup(
                    root,
                    2,
                    "followup-delete-race-0001",
                    "并发继续",
                    self._charged_creator(),
                    actor_id=20,
                )
                outcomes.append(("followup", result["task_id"]))
            except taskthreads.TaskThreadError as exc:
                outcomes.append(("followup_error", exc.code))
            except BaseException as exc:  # pragma: no cover - asserted below
                unexpected.append(exc)

        def delete():
            barrier.wait()
            try:
                taskthreads.soft_delete_task(
                    root, 2, actor_id=20, now=500
                )
                outcomes.append(("deleted", root))
            except taskthreads.TaskThreadError as exc:
                outcomes.append(("delete_error", exc.code))
            except BaseException as exc:  # pragma: no cover - asserted below
                unexpected.append(exc)

        workers = [
            threading.Thread(target=followup),
            threading.Thread(target=delete),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual([], unexpected)
        winning = {name for name, _value in outcomes} & {"followup", "deleted"}
        self.assertEqual(1, len(winning), outcomes)
        row = db.one("SELECT deleted_at,thread_id FROM task WHERE id=?", (root,))
        if "followup" in winning:
            self.assertIsNone(row["deleted_at"])
            self.assertIsNotNone(row["thread_id"])
            self.assertEqual(1, db.one(
                "SELECT COUNT(*) n FROM task_thread"
            )["n"])
        else:
            self.assertIsNotNone(row["deleted_at"])
            self.assertIsNone(row["thread_id"])
            self.assertEqual(0, db.one(
                "SELECT COUNT(*) n FROM task_thread"
            )["n"])

    def test_historical_revision_can_restore_but_can_never_be_purged(self):
        root = self._task()
        second = taskthreads.create_followup(
            root,
            2,
            "followup-restore-0001",
            "第二版",
            self._charged_creator(),
        )
        db.execute(
            "UPDATE task SET status='done',output_md='第二版交付' WHERE id=?",
            (second["task_id"],),
        )
        third = taskthreads.create_followup(
            second["task_id"],
            2,
            "followup-restore-0002",
            "第三版",
            self._charged_creator(),
        )

        taskthreads.soft_delete_task(
            second["task_id"], 2, actor_id=20, now=600
        )
        self.assertFalse(taskthreads.task_hard_delete_guard(
            second["task_id"], 2, include_deleted=True
        )["allowed"])
        restored = taskthreads.restore_task(
            second["task_id"], 2, now=601
        )

        self.assertTrue(restored["restored"])
        self.assertIsNone(db.one(
            "SELECT deleted_at FROM task WHERE id=?", (second["task_id"],)
        )["deleted_at"])
        summary = taskthreads.thread_summary_for_task(third["task_id"], 2)
        self.assertEqual(3, summary["revision_count"])
        self.assertEqual(third["task_id"], summary["current_task_id"])

    def test_http_delete_restore_and_purge_keep_thread_history_intact(self):
        from app import auth, main

        root = self._task()
        second = taskthreads.create_followup(
            root,
            2,
            "followup-http-restore-0001",
            "第二版",
            self._charged_creator(),
        )
        db.execute(
            "UPDATE task SET status='done',output_md='第二版交付' WHERE id=?",
            (second["task_id"],),
        )
        third = taskthreads.create_followup(
            second["task_id"],
            2,
            "followup-http-restore-0002",
            "第三版",
            self._charged_creator(),
        )
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner",
            "role": "owner",
            "modules": ["content"],
        })
        try:
            with patch.object(main.llm, "kill"):
                deleted = main.task_delete(second["task_id"])
            self.assertTrue(deleted["soft_deleted"])
            self.assertTrue(
                main.trash_restore("task", second["task_id"])["restored"]
            )
            with patch.object(main.llm, "kill"):
                main.task_delete(second["task_id"])
            with self.assertRaises(HTTPException) as purge:
                main.trash_purge("task", second["task_id"])
            self.assertEqual(409, purge.exception.status_code)
        finally:
            auth.set_current(None)

        summary = taskthreads.thread_summary_for_task(third["task_id"], 2)
        self.assertEqual(3, summary["revision_count"])
        self.assertEqual(third["task_id"], summary["current_task_id"])

    def test_restore_fails_closed_for_hidden_orphan_thread_anchor(self):
        task_id = self._task()
        taskthreads.soft_delete_task(task_id, 2, actor_id=20, now=700)
        db.insert("task_thread", {
            "tenant_id": 2,
            "emp_idx": 0,
            "root_task_id": task_id,
            "current_task_id": task_id,
            "status": "active",
            "revision_count": 1,
            "created_by": 20,
        })

        with self.assertRaises(taskthreads.ThreadConflict) as caught:
            taskthreads.restore_task(task_id, 2, now=701)
        self.assertEqual("restore_thread_mismatch", caught.exception.code)
        self.assertIsNotNone(db.one(
            "SELECT deleted_at FROM task WHERE id=?", (task_id,)
        )["deleted_at"])

    def test_legacy_redo_requires_explicit_key_and_forwards_material(self):
        from app import main

        with self.assertRaises(HTTPException) as missing:
            asyncio.run(main.task_redo(42, {"feedback": "修改"}))
        self.assertEqual(400, missing.exception.status_code)

        forward = AsyncMock(return_value={"task_id": 43})
        binding = {
            "identity_ref": "a" * 64,
            "config_revision": 7,
            "config_sha256": "b" * 64,
            "bundle_sha256": "c" * 64,
        }
        with patch.object(main, "_create_task_followup", forward):
            result = asyncio.run(main.task_redo(42, {
                "feedback": "修改",
                "material": "旧入口新增材料",
                "request_key": "legacy-redo-contract-0001",
                **binding,
            }))
        self.assertEqual(43, result["task_id"])
        forward.assert_awaited_once_with(42, {
            "feedback": "修改",
            "material": "旧入口新增材料",
            "request_key": "legacy-redo-contract-0001",
            **binding,
        })

    def test_followup_async_routes_have_no_inline_sync_db_calls(self):
        source = Path(taskthreads.__file__).with_name("main.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        targets = {
            "_create_task_followup",
            "task_followup",
            "task_redo",
            "task_thread_accept",
        }
        blocking = {
            "one", "q", "execute", "insert", "update", "conn", "atomic"
        }
        found = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.name not in targets:
                continue
            calls = []
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "db"
                    and child.func.attr in blocking
                ):
                    calls.append(child.func.attr)
            if calls:
                found[node.name] = calls
        self.assertEqual({}, found)

    def test_invalid_inputs_and_non_done_adoption_fail_before_callback(self):
        running = self._task(status="running")
        calls = []
        with self.assertRaises(taskthreads.ThreadConflict) as active:
            taskthreads.create_followup(
                running,
                2,
                "followup-running-0001",
                "不能现在建",
                self._charged_creator(calls=calls),
            )
        self.assertEqual("task_not_done", active.exception.code)
        for request_key, feedback in (
            ("short", "有意见"),
            ("followup-valid-0001", ""),
            ("followup valid 0002", "有意见"),
        ):
            with self.subTest(request_key=request_key, feedback=feedback):
                with self.assertRaises(taskthreads.InvalidFollowup):
                    taskthreads.create_followup(
                        running,
                        2,
                        request_key,
                        feedback,
                        self._charged_creator(calls=calls),
                    )
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
