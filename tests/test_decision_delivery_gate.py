"""V2 决策员工交付机器门禁：只降级，不自动执行。"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import db, departments, taskrunner


def _v2_employee() -> dict:
    return next(
        employee
        for employee in departments.specialists().values()
        if departments.is_decision_employee(employee)
    )


def _complete_output(status: str = "GO") -> str:
    manifest = _provenance()
    evidence = "\n".join(
        f"- [{item['input_id']}][{item['evidence_id']}] "
        f"事实：{item['label']}原始记录值已提供；时间：2026-08-01；"
        "来源索引：用户提交"
        for item in manifest["items"]
    )
    return (
        "# 决策测试\n"
        f"## 决策状态\n{status}\n"
        "## 事实证据/数据源\n"
        f"{evidence}\n"
        "## 数据缺口\n无数据缺口\n"
        f"## 审批边界\n{departments.DECISION_APPROVAL_BODY}\n"
        f"## 禁止动作\n{departments.DECISION_FORBIDDEN_BODY}\n"
    )


def _provenance() -> dict:
    employee = _v2_employee()
    return departments.normalize_decision_evidence(
        employee,
        2,
        [
            {
                "input_id": f"RI-{index:02d}",
                "content": f"{label}：2026-08-01 用户原始记录",
                "source_name": f"用户材料-{index}",
            }
            for index, label in enumerate(
                employee["decision_contract"]["required_inputs"], 1
            )
        ],
    )


class DecisionDeliveryGateTests(unittest.TestCase):
    def test_complete_go_keeps_go_and_explicitly_requires_human_approval(self):
        original = _complete_output("GO")
        result = departments.enforce_decision_output(
            _v2_employee(), original, provenance=_provenance()
        )

        self.assertTrue(result["is_decision"])
        self.assertEqual("GO", result["status"])
        self.assertTrue(result["passed"])
        self.assertEqual([], result["reasons"])
        self.assertIn("GO 仅表示可进入人工审批", result["output"])
        self.assertIn(original.strip(), result["output"])

    def test_go_without_evidence_fails_safe_to_hold_and_preserves_original(self):
        original = _complete_output("GO").replace(
            _complete_output("GO").split("## 事实证据/数据源\n", 1)[1].split(
                "## 数据缺口", 1
            )[0].strip(),
            "- 暂无可核验证据。",
        )
        result = departments.enforce_decision_output(_v2_employee(), original)

        self.assertEqual("HOLD", result["status"])
        self.assertIn("缺少事实证据", "；".join(result["reasons"]))
        self.assertIn("GO", result["original_output"])
        self.assertIn("原始输出（人工复核）", result["output"])

    def test_generic_system_source_is_not_reproducible_evidence(self):
        original = _complete_output("GO").replace(
            _complete_output("GO").split("## 事实证据/数据源\n", 1)[1].split(
                "## 数据缺口", 1
            )[0].strip(),
            "- 事实：有一条业务数据；数据源：系统。",
        )
        result = departments.enforce_decision_output(_v2_employee(), original)

        self.assertEqual("HOLD", result["status"])
        self.assertIn("事实证据", "；".join(result["reasons"]))

    def test_evidence_needs_concrete_record_time_and_source_or_index(self):
        cases = (
            "- 事实：系统显示数据；数据源：ERP。",
            "- 事实：订单数量 123；数据源：ERP。",
            "- 事实：订单数量 123；统计期：2026-08-01 至 2026-08-07。",
        )
        for evidence in cases:
            with self.subTest(evidence=evidence):
                result = departments.enforce_decision_output(
                    _v2_employee(),
                    _complete_output("GO").split("## 事实证据/数据源", 1)[0]
                    + "## 事实证据/数据源\n" + evidence
                    + "\n## 数据缺口\n无数据缺口\n"
                    f"## 审批边界\n{departments.DECISION_APPROVAL_BODY}\n"
                    f"## 禁止动作\n{departments.DECISION_FORBIDDEN_BODY}\n",
                    provenance=_provenance(),
                )
                self.assertEqual("HOLD", result["status"])

    def test_concrete_evidence_source_index_and_window_can_pass(self):
        original = _complete_output("GO")
        result = departments.enforce_decision_output(
            _v2_employee(), original, provenance=_provenance()
        )

        self.assertEqual("GO", result["status"])
        self.assertTrue(result["passed"])

    def test_invalid_status_fails_safe_to_hold(self):
        original = _complete_output("MAYBE")
        result = departments.enforce_decision_output(
            _v2_employee(), original, provenance=_provenance()
        )

        self.assertEqual("HOLD", result["status"])
        self.assertIn("决策状态非法", result["reasons"])

    def test_hold_is_never_upgraded(self):
        original = _complete_output("HOLD").replace("无数据缺口", "- 待补齐：近7日退款明细")
        result = departments.enforce_decision_output(
            _v2_employee(), original, provenance=_provenance()
        )

        self.assertEqual("HOLD", result["status"])
        self.assertTrue(result["passed"])
        self.assertFalse(result["downgraded"])

    def test_escalate_with_specific_gap_keeps_escalate(self):
        original = _complete_output("ESCALATE").replace(
            "无数据缺口", "- 待补齐：重大安全事件的现场复核记录"
        )
        result = departments.enforce_decision_output(
            _v2_employee(), original, provenance=_provenance()
        )

        self.assertEqual("ESCALATE", result["status"])
        self.assertTrue(result["passed"])

    def test_go_with_specific_gap_is_hold(self):
        original = _complete_output("GO").replace(
            "无数据缺口", "- 待补齐：近7日退款明细"
        )
        result = departments.enforce_decision_output(
            _v2_employee(), original, provenance=_provenance()
        )

        self.assertEqual("HOLD", result["status"])
        self.assertIn("未闭合数据缺口", "；".join(result["reasons"]))

    def test_contradictory_no_gap_then_pending_gap_is_hold(self):
        for gap in (
            "并非无数据缺口；近7日退款明细待补齐",
            "无数据缺口；但近7日退款明细仍待补齐",
        ):
            with self.subTest(gap=gap):
                result = departments.enforce_decision_output(
                    _v2_employee(), _complete_output("GO").replace("无数据缺口", gap),
                    provenance=_provenance(),
                )
                self.assertEqual("HOLD", result["status"])
                self.assertIn("未闭合数据缺口", "；".join(result["reasons"]))

    def test_explicit_no_gap_synonyms_do_not_match_positive_gap_substrings(self):
        for gap in ("没有数据缺口", "不存在数据缺口", "暂无任何数据缺口"):
            with self.subTest(gap=gap):
                result = departments.enforce_decision_output(
                    _v2_employee(), _complete_output("GO").replace("无数据缺口", gap),
                    provenance=_provenance(),
                )
                self.assertEqual("GO", result["status"])
                self.assertTrue(result["passed"])

    def test_v1_output_is_unchanged(self):
        legacy = next(iter(departments.legacy_specialists().values()))
        original = "# V1 交付\n普通员工输出，不应被决策门禁包装。"
        result = departments.enforce_decision_output(legacy, original)

        self.assertFalse(result["is_decision"])
        self.assertIsNone(result["status"])
        self.assertEqual(original, result["output"])

    def test_v2_prompt_forbids_assumptions_but_v1_rule_remains(self):
        employee = _v2_employee()
        bundle = departments.build_task_prompt(
            employee,
            {"direction": "测试决策", "industry": "测试行业"},
            "",
            "",
            [],
        )
        self.assertIn("不得合理假设", bundle.system)
        self.assertIn("## 决策状态", bundle.system)

        legacy = next(iter(departments.legacy_specialists().values()))
        legacy = {**legacy, "dept_name": "旧版部门"}
        legacy_bundle = departments.build_task_prompt(
            legacy,
            {"direction": "测试普通员工", "industry": "餐饮"},
            "",
            "",
            [],
        )
        self.assertIn("合理假设并显著标注", legacy_bundle.system)
        self.assertNotIn("不得合理假设", legacy_bundle.system)


class DecisionSummaryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "decision-summary.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 5})

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def test_v2_long_summary_inherits_hold_and_never_accepts_invalid_llm_summary(self):
        employee = _v2_employee()
        original = _complete_output("GO").replace(
            "无数据缺口", "- 待补齐：近7日退款明细"
        ) + "\n" + ("长正文。" * 600)
        gate = departments.enforce_decision_output(employee, original)
        self.assertEqual("HOLD", gate["status"])
        self.assertGreater(len(gate["output"]), taskrunner.DIGEST_MIN_CHARS)
        task_id = db.insert(
            "task",
            {
                "emp_idx": employee["idx"],
                "tenant_id": 2,
                "brief_json": "{}",
                "status": "done",
                "billing_status": "succeeded",
                "output_md": gate["output"],
                "cost_usd": 0.0,
            },
        )

        # 即使二次模型恶意返回 GO，V2 摘要路径也不应调用它或采纳其结果。
        with patch.object(
            taskrunner.providers,
            "call_text_json",
            new=AsyncMock(return_value={"data": {"points": ["GO"], "action": "自动执行"}}),
        ) as summary_model:
            await taskrunner._gen_summary(
                task_id,
                gate["output"],
                employee["idx"],
                2,
                lambda _payload: None,
                employee=employee,
                decision_gate=gate,
            )

        summary_model.assert_not_awaited()
        summary = db.one("SELECT summary_md FROM task WHERE id=?", (task_id,))["summary_md"]
        self.assertIn("决策状态：HOLD", summary)
        self.assertIn("人工审批", summary)
        self.assertIn("内容未核验", summary)
        self.assertIn("数据缺口", summary)
        self.assertIn("近7日退款明细", summary)
        self.assertIn("审批边界", summary)
        self.assertIn("禁止动作", summary)
        self.assertNotIn("决策状态：GO", summary)


if __name__ == "__main__":
    unittest.main()
