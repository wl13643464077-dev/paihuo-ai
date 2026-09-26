"""老板结论卡：专家任务/会议统一「一句话结论 + 3 条行动 + 要留意」，内容工单默认少打扰。"""
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import bossbrief, db, departments, employeeidentity, engine, meeting, taskrunner
from app.skills import registry


REPORT_MD = """# 门店选址报告
## 核心结论
这个位置**可以租**，但要先谈下免租期。周末客流比工作日高 30%。
## 数据
- 工作日客流 1200 人
- 周末客流 1560 人
| 时段 | 客流 |
| --- | --- |
## 风险提示
- 对面在建商场，明年可能分流
## 下一步建议
1. 店长：本周约房东谈 2 个月免租期
2. 老板：周六实地数一次客流
3. **财务**：把租金占比控制在 15% 以内，超了就不签
4. 第四条不该进卡片
"""


class BossBriefPureTests(unittest.TestCase):
    def test_fallback_extracts_three_actions_from_next_steps_section(self):
        brief = bossbrief.fallback_brief(REPORT_MD)
        self.assertEqual(
            [
                "店长：本周约房东谈 2 个月免租期",
                "老板：周六实地数一次客流",
                "财务：把租金占比控制在 15% 以内，超了就不签",
            ],
            brief["actions"],
        )
        self.assertEqual("这个位置可以租，但要先谈下免租期。", brief["verdict"])
        self.assertEqual("对面在建商场，明年可能分流", brief["watch"])
        self.assertTrue(all(len(a) <= bossbrief.ACTION_MAX for a in brief["actions"]))

    def test_fallback_always_has_an_action_even_without_structure(self):
        brief = bossbrief.fallback_brief("随便写了几句话，没有任何结构。")
        self.assertEqual([bossbrief.DEFAULT_ACTION], brief["actions"])
        self.assertTrue(brief["verdict"])

    def test_model_string_actions_are_split_by_line_not_by_character(self):
        brief, used_model = bossbrief.merge_model_brief(
            {"verdict": "能租", "actions": "店长：谈免租\n老板：数客流"}, REPORT_MD
        )
        self.assertTrue(used_model)
        self.assertEqual("能租", brief["verdict"])
        # 模型只给了 2 条：用正文「下一步建议」补满 3 条。
        self.assertEqual(
            ["店长：谈免租", "老板：数客流", "店长：本周约房东谈 2 个月免租期"],
            brief["actions"],
        )
        self.assertEqual(["一条"], bossbrief.as_items("一条", 3, 40))

    def test_long_action_is_trimmed_to_forty_chars(self):
        long_action = "店长：" + "把门口货架整体换成引流品并且重新摆放价签" * 4
        brief, _ = bossbrief.merge_model_brief(
            {"verdict": "可以", "actions": [long_action]}, REPORT_MD
        )
        self.assertLessEqual(len(brief["actions"][0]), bossbrief.ACTION_MAX)
        self.assertTrue(brief["actions"][0].endswith("…"))

    def test_format_and_parse_round_trip(self):
        text = bossbrief.format_brief(
            "可以租", ["店长：谈免租", "老板：数客流", "财务：控租金"], "商场分流",
            ["决策状态：HOLD"],
        )
        self.assertEqual(
            {
                "verdict": "可以租",
                "actions": ["店长：谈免租", "老板：数客流", "财务：控租金"],
                "watch": "商场分流",
                "extra": ["决策状态：HOLD"],
            },
            bossbrief.parse_brief(text),
        )
        self.assertEqual(
            "可以租｜要做：店长：谈免租；老板：数客流；财务：控租金｜留意：商场分流",
            bossbrief.one_line(text),
        )

    def test_legacy_summary_still_parses(self):
        legacy = "- 客流稳定\n- 租金可谈\n- 👉 **一句话行动建议**:本周约房东"
        self.assertEqual(
            {"verdict": "客流稳定", "actions": ["本周约房东"], "watch": "",
             "extra": ["租金可谈"]},
            bossbrief.parse_brief(legacy),
        )


class MeetingBriefTests(unittest.TestCase):
    def test_meeting_digest_maps_to_same_structure_in_plain_words(self):
        summary = meeting._digest(
            "GO",
            {"id": 2, "title": "社区团购试点", "risk": "团长流失快"},
            "三类验证都通过，毛利够",
            "本周启动 20 户试点",
            [
                {"who": "小王·运营", "task": "做一份团购试点方案和记录表"},
                {"who": "小李·财务", "task": "算清试点期每单毛利"},
            ],
            [{"verdict": "FAIL", "fatal_risk": "不该用到"}],
        )
        brief = bossbrief.parse_brief(summary)
        self.assertEqual("可以干：三类验证都通过，毛利够", brief["verdict"])
        self.assertEqual(
            [
                "小王·运营：做一份团购试点方案和记录表",
                "小李·财务：算清试点期每单毛利",
                "本周启动 20 户试点",
            ],
            brief["actions"],
        )
        self.assertEqual("团长流失快", brief["watch"])
        self.assertEqual(["选定方案：社区团购试点"], brief["extra"])
        for jargon in ("P2｜", "决策：GO", "用户提交覆盖", "门禁原因", "Next Action"):
            self.assertNotIn(jargon, summary)

    def test_no_go_meeting_still_has_an_action_and_uses_validation_risk(self):
        summary = meeting._digest(
            "NO_GO", None, "", "停止当前方向，转向更小的问题", [],
            [
                {"verdict": "PASS", "fatal_risk": ""},
                {"verdict": "FAIL", "fatal_risk": "房租占营收 40%"},
            ],
        )
        brief = bossbrief.parse_brief(summary)
        self.assertEqual("不建议干：关键证据尚未补齐", brief["verdict"])
        self.assertEqual(["停止当前方向，转向更小的问题"], brief["actions"])
        self.assertEqual("房租占营收 40%", brief["watch"])

    def test_delivery_sync_appends_progress_line_that_parser_keeps(self):
        summary = meeting._digest("NEED_INFO", None, "缺客流数据", "去数客流") + "\n执行交付：1/2 已完成"
        brief = bossbrief.parse_brief(summary)
        self.assertEqual("还差关键信息，先补齐再定：缺客流数据", brief["verdict"])
        self.assertIn("执行交付：1/2 已完成", brief["extra"])


class StageProgressTests(unittest.TestCase):
    def test_stage_mapping_hides_internal_step_text(self):
        steps = [
            {"k": "boot", "l": "员工已上线(secret-model-x),阅读任务简报…", "ts": 1},
            {"k": "tool", "l": "已装载批准能力包 r7：启用能力 3 项", "ts": 2},
            {"k": "search", "l": "联网检索中 · 已发起 2 次", "ts": 3},
        ]
        p = bossbrief.task_stage_progress(steps, "running", 1000, now=1120, length="std")
        self.assertEqual(0, p["current"])
        self.assertEqual("正在查资料", p["label"])
        self.assertEqual(120, p["elapsed_seconds"])
        self.assertEqual("预计还要约 8 分钟", p["hint"])
        self.assertNotIn("secret-model-x", json.dumps(p, ensure_ascii=False))

        steps.append({"k": "typing", "l": "正在撰写产出…已写 300 字", "ts": 4})
        p = bossbrief.task_stage_progress(steps, "running", 1000, now=1300)
        self.assertEqual(("正在写方案", 1), (p["label"], p["current"]))
        steps.append({"k": "typing", "l": "正在撰写产出…已写 1900 字", "ts": 5})
        p = bossbrief.task_stage_progress(steps, "running", 1000, now=1400)
        self.assertEqual("正在检查", p["label"])
        steps.append({"k": "done", "l": "交付完成 · $0.012", "ts": 6})
        p = bossbrief.task_stage_progress(steps, "running", 1000, now=1500)
        self.assertEqual("马上好", p["label"])
        self.assertLessEqual(p["eta_seconds"], 60)

    def test_overdue_and_queued_and_terminal(self):
        p = bossbrief.task_stage_progress(
            [{"k": "typing", "l": "已写 10 字"}], "running", 0, now=5000,
            typical_seconds=600,
        )
        self.assertIsNone(p["eta_seconds"])
        self.assertIn("还在认真做", p["hint"])
        queued = bossbrief.task_stage_progress([], "queued", 0, now=10)
        self.assertEqual(-1, queued["current"])
        self.assertEqual("排队中，马上开工", queued["label"])
        self.assertIsNone(bossbrief.task_stage_progress([], "done", 0))


class DefaultJobModeTests(unittest.TestCase):
    def test_new_content_jobs_default_to_only_final_review(self):
        self.assertEqual("autopilot", engine.DEFAULT_JOB_MODE)
        self.assertEqual("autopilot", engine.job_mode_or_default(None))
        self.assertEqual("autopilot", engine.job_mode_or_default(""))
        # 老租户已保存的偏好原样沿用。
        for saved in ("copilot", "manual", "fullauto"):
            self.assertEqual(saved, engine.job_mode_or_default(saved))
        self.assertIsNone(engine.job_mode_or_default("bogus"))

        stops = [
            station["idx"] for station in registry.STATIONS
            if not station.get("solo_only")
            and engine.engine.needs_review(station["idx"], engine.DEFAULT_JOB_MODE)
        ]
        forced = [
            station["idx"] for station in registry.STATIONS
            if not station.get("solo_only")
            and station["approval"] == registry.APPROVAL_FORCE
        ]
        self.assertEqual(forced, stops)
        self.assertEqual(1, len(stops))
        copilot_stops = [
            station["idx"] for station in registry.STATIONS
            if not station.get("solo_only")
            and engine.engine.needs_review(station["idx"], "copilot")
        ]
        self.assertGreater(len(copilot_stops), len(stops))


class TaskSummaryDbTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "boss-brief.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 10})
        db.insert("users", {"tenant_id": 2, "username": "boss2", "password_hash": "x",
                            "role": "owner", "enabled": 1})
        taskrunner.RUNNING.clear()

    def tearDown(self):
        taskrunner.RUNNING.clear()
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _task(self, idx=0, **extra):
        employee = employeeidentity.any_employee(idx)
        row = {
            "tenant_id": 2,
            "emp_idx": idx,
            "brief_json": json.dumps({"direction": "看看这个铺面能不能租"}),
            "status": "queued",
            "billing_status": "charged",
            "billing_points": 3,
            **employeeidentity.task_fields(employee),
        }
        row.update(extra)
        return db.insert("task", row)

    def _summary(self, task_id):
        return db.one("SELECT summary_md FROM task WHERE id=?", (task_id,))["summary_md"]

    async def test_short_delivery_also_gets_boss_brief(self):
        short_md = "# 铺面结论\n可以租。\n## 下一步建议\n- 店长：周五前约房东"
        self.assertLess(len(short_md), taskrunner.DIGEST_MIN_CHARS)
        task_id = self._task(idx=101)
        summary_model = AsyncMock(return_value={
            "data": {
                "verdict": "能租，先谈免租期",
                "actions": ["店长：周五前约房东", "老板：周六数客流", "财务：算租金占比"],
                "watch": "对面商场明年开业",
            },
            "cost_usd": 0.0,
        })
        with patch.object(
            taskrunner.providers, "call_text",
            new=AsyncMock(return_value={"text": short_md, "cost_usd": 0.0, "tokens": 1}),
        ), patch.object(
            taskrunner.providers, "call_text_json", new=summary_model,
        ), patch.object(
            taskrunner.departments, "enforce_decision_output",
            return_value={"is_decision": False},
        ):
            await taskrunner.run_task(task_id, lambda _p: None)
        summary_model.assert_awaited_once()
        brief = bossbrief.parse_brief(self._summary(task_id))
        self.assertEqual("能租，先谈免租期", brief["verdict"])
        self.assertEqual(3, len(brief["actions"]))
        self.assertEqual("对面商场明年开业", brief["watch"])

    async def test_model_failure_falls_back_to_three_actions_from_body(self):
        task_id = self._task(status="done", billing_status="succeeded", output_md=REPORT_MD)
        with patch.object(
            taskrunner.providers, "call_text_json",
            new=AsyncMock(side_effect=RuntimeError("model down")),
        ), self.assertLogs("taskrunner", level="WARNING"):
            await taskrunner._gen_summary(task_id, REPORT_MD, 0, 2, lambda _p: None)
        brief = bossbrief.parse_brief(self._summary(task_id))
        self.assertEqual(
            [
                "店长：本周约房东谈 2 个月免租期",
                "老板：周六实地数一次客流",
                "财务：把租金占比控制在 15% 以内，超了就不签",
            ],
            brief["actions"],
        )
        self.assertEqual("对面在建商场，明年可能分流", brief["watch"])

    async def test_model_garbage_json_still_yields_card(self):
        task_id = self._task(status="done", billing_status="succeeded", output_md=REPORT_MD)
        with patch.object(
            taskrunner.providers, "call_text_json",
            new=AsyncMock(return_value={"data": ["not", "a", "dict"], "cost_usd": 0.01}),
        ):
            await taskrunner._gen_summary(task_id, REPORT_MD, 0, 2, lambda _p: None)
        brief = bossbrief.parse_brief(self._summary(task_id))
        self.assertEqual(3, len(brief["actions"]))

    def test_decision_summary_uses_card_structure_without_jargon(self):
        employee = next(
            e for e in departments.specialists().values()
            if departments.is_decision_employee(e)
        )
        output = (
            "# 决策\n## 决策状态\nGO\n## 数据缺口\n- 待补齐：近7日退款明细\n"
            f"## 审批边界\n{departments.DECISION_APPROVAL_BODY}\n"
            f"## 禁止动作\n{departments.DECISION_FORBIDDEN_BODY}\n"
        )
        gate = departments.enforce_decision_output(employee, output)
        self.assertEqual("HOLD", gate["status"])
        summary = "\n".join(
            taskrunner._decision_summary_lines(gate["output"], employee, decision_gate=gate)
        )
        brief = bossbrief.parse_brief(summary)
        self.assertTrue(brief["verdict"].startswith("先别动（HOLD）"))
        self.assertEqual(3, len(brief["actions"]))
        self.assertIn("复核前不要按报告改价、下单或调整人手", brief["actions"])
        self.assertIn("决策状态：HOLD", brief["extra"])
        self.assertIn("- 决策状态：HOLD", summary)
        for kept in ("人工审批", "数据缺口", "审批边界", "禁止动作", "近7日退款明细"):
            self.assertIn(kept, summary)
        for jargon in ("用户提交覆盖", "门禁原因", "门禁结论"):
            self.assertNotIn(jargon, summary)

    def test_typical_seconds_uses_recent_history(self):
        self.assertEqual(600, bossbrief.typical_task_seconds(2, 0, "std"))
        self.assertEqual(300, bossbrief.typical_task_seconds(2, 0, "lite"))
        for span in (200, 240, 260):
            self._task(status="done", billing_status="succeeded",
                       created_at=1000.0, terminal_at=1000.0 + span)
        self.assertEqual(240, bossbrief.typical_task_seconds(2, 0, "std"))


if __name__ == "__main__":
    unittest.main()
