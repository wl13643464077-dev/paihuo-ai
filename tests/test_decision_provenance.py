"""Schema53 V2 decision evidence provenance is server-owned and fail-closed."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import auth, billing, db, departments, employeeidentity, employees, taskthreads


def _employee() -> dict:
    return next(
        employee for employee in departments.specialists().values()
        if departments.is_decision_employee(employee)
        and len(employee["decision_contract"]["required_inputs"]) == 5
    )


def _raw_items(employee: dict, *, sentinel: str = "") -> list[dict]:
    return [
        {
            "input_id": f"RI-{index:02d}",
            "content": (
                f"{label}：2026-08-01 用户提供的原始记录 {sentinel}"
            ).strip(),
            "source_name": f"用户材料-{index}",
        }
        for index, label in enumerate(
            employee["decision_contract"]["required_inputs"], 1
        )
    ]


def _manifest(employee: dict, tenant_id: int = 2, *, count: int | None = None) -> dict:
    items = _raw_items(employee)
    if count is not None:
        items = items[:count]
    return departments.normalize_decision_evidence(employee, tenant_id, items)


def _output(manifest: dict, status: str = "GO", *, include: int | None = None) -> str:
    items = manifest["items"]
    if include is not None:
        items = items[:include]
    refs = "\n".join(
        f"- [{item['input_id']}][{item['evidence_id']}] "
        f"事实：{item['label']}原始记录值已提供；记录日期：2026-08-01；"
        "来源索引：用户提交"
        for item in items
    )
    return (
        "# 决策\n"
        f"## 决策状态\n{status}\n"
        f"## 事实证据/数据源\n{refs}\n"
        "## 数据缺口\n无数据缺口\n"
        f"## 审批边界\n{departments.DECISION_APPROVAL_BODY}\n"
        f"## 禁止动作\n{departments.DECISION_FORBIDDEN_BODY}\n"
    )


class DecisionProvenanceUnitTests(unittest.TestCase):
    def test_public_guide_adds_only_allowlisted_ri_labels(self):
        employee = _employee()
        guide = departments.public_task_guide(employee)
        expected = [
            {"input_id": f"RI-{index:02d}", "label": label}
            for index, label in enumerate(
                employee["decision_contract"]["required_inputs"], 1
            )
        ]
        self.assertEqual(expected, guide["evidence_requirements"])
        serialized = json.dumps(guide, ensure_ascii=False)
        for secret in ("workflow", "forbidden_actions", "approval_boundary"):
            self.assertNotIn(secret, serialized)

        legacy = next(iter(departments.legacy_specialists().values()))
        self.assertNotIn(
            "evidence_requirements", departments.public_task_guide(legacy)
        )

    def test_manifest_is_canonical_bounded_and_bound_to_tenant_spec_input_content(self):
        employee = _employee()
        manifest = _manifest(employee)
        self.assertEqual(departments.DECISION_EVIDENCE_SCHEMA, manifest["schema"])
        self.assertEqual(2, manifest["tenant_id"])
        self.assertEqual(
            employee["employee_spec_sha256"], manifest["employee_spec_sha256"]
        )
        self.assertRegex(manifest["items"][0]["evidence_id"], r"^U:[0-9a-f]{64}$")
        self.assertEqual(
            "user_submitted_unverified", manifest["items"][0]["kind"]
        )
        self.assertEqual(
            manifest,
            departments.validate_decision_evidence(employee, 2, manifest),
        )

        for tenant_id, spec in ((3, None), (2, "0" * 64)):
            copied = json.loads(json.dumps(manifest))
            if spec is None:
                with self.assertRaises(ValueError):
                    departments.validate_decision_evidence(employee, tenant_id, copied)
            else:
                copied["employee_spec_sha256"] = spec
                with self.assertRaises(ValueError):
                    departments.validate_decision_evidence(employee, 2, copied)

    def test_rejects_unknown_duplicate_and_forged_item_fields(self):
        employee = _employee()
        first = _raw_items(employee)[0]
        cases = (
            [{**first, "evidence_id": "U:" + "0" * 64}],
            [first, dict(first)],
            [{**first, "input_id": "RI-99"}],
            [{"input_id": "RI-01", "content": "x" * 4001}],
        )
        for value in cases:
            with self.subTest(value=value[0].get("input_id")):
                with self.assertRaises(ValueError):
                    departments.normalize_decision_evidence(employee, 2, value)

    def test_merge_rechecks_total_bound_before_followup_can_persist(self):
        employee = _employee()
        requirements = departments.decision_evidence_requirements(employee)
        base = departments.normalize_decision_evidence(
            employee,
            2,
            [
                {
                    "input_id": requirements[index]["input_id"],
                    "content": requirements[index]["label"] + (
                        "a" * (4000 - len(requirements[index]["label"]))
                    ),
                    "source_name": "s" * 160,
                }
                for index in range(4)
            ],
        )
        with self.assertRaisesRegex(ValueError, "合并后.*总量超限"):
            departments.normalize_decision_evidence(
                employee,
                2,
                [{
                    "input_id": requirements[4]["input_id"],
                    "content": requirements[4]["label"] + (
                        "b" * (4000 - len(requirements[4]["label"]))
                    ),
                    "source_name": "t" * 160,
                }],
                base_manifest=base,
            )

    def test_prompt_keeps_evidence_and_free_material_out_of_system_and_research(self):
        employee = _employee()
        manifest = _manifest(employee)
        sentinel = "PRIVATE-EVIDENCE-SENTINEL"
        manifest = departments.normalize_decision_evidence(
            employee, 2, _raw_items(employee, sentinel=sentinel)
        )
        brief = {
            "direction": "决策任务",
            "industry": "汽车",
            "material": "PRIVATE-FREE-MATERIAL",
            "revision_material": "PRIVATE-REVISION-MATERIAL",
            "decision_evidence": manifest,
        }
        bundle = departments.build_task_prompt(employee, brief, "", "", [])
        first = manifest["items"][0]
        pair = f"[{first['input_id']}][{first['evidence_id']}]"
        self.assertIn(sentinel, bundle.user)
        self.assertIn(pair, bundle.user)
        self.assertIn("逐项引用", bundle.user)
        self.assertIn("内容未核验", bundle.user)
        self.assertIn("不证明内容相关或真实", bundle.user)
        for private in (sentinel, "PRIVATE-FREE-MATERIAL", "PRIVATE-REVISION-MATERIAL"):
            self.assertNotIn(private, bundle.system)
            self.assertNotIn(private, bundle.research)

    def test_only_exact_all_ri_pairs_can_pass_go(self):
        employee = _employee()
        manifest = _manifest(employee)
        passed = departments.enforce_decision_output(
            employee, _output(manifest), provenance=manifest
        )
        self.assertEqual("GO", passed["status"])
        self.assertTrue(passed["passed"])

        partial = departments.enforce_decision_output(
            employee, _output(manifest, include=len(manifest["items"]) - 1),
            provenance=manifest,
        )
        self.assertEqual("HOLD", partial["status"])
        self.assertFalse(partial["passed"])
        self.assertIn(manifest["items"][-1]["input_id"], " ".join(partial["reasons"]))

        forged = _output(manifest).replace(
            manifest["items"][0]["evidence_id"], "U:" + "f" * 64
        )
        rejected = departments.enforce_decision_output(
            employee, forged, provenance=manifest
        )
        self.assertEqual("HOLD", rejected["status"])
        self.assertFalse(rejected["passed"])

    def test_missing_manifest_unrelated_input_and_free_url_are_hold(self):
        employee = _employee()
        manifest = _manifest(employee)
        missing = departments.enforce_decision_output(employee, _output(manifest))
        self.assertEqual("HOLD", missing["status"])
        self.assertFalse(missing["passed"])

        unrelated_items = _raw_items(employee)
        unrelated_items[0]["content"] = "门店 POS 订单与 ERP_EXPORT_999 收银流水"
        unrelated = departments.normalize_decision_evidence(
            employee, 2, unrelated_items
        )
        rejected = departments.enforce_decision_output(
            employee, _output(unrelated), provenance=unrelated
        )
        self.assertEqual("HOLD", rejected["status"])
        self.assertFalse(rejected["passed"])

        with_url = _output(manifest).replace(
            "## 审批边界", "- https://fabricated.invalid/fact\n## 审批边界"
        )
        rejected_url = departments.enforce_decision_output(
            employee, with_url, provenance=manifest
        )
        self.assertEqual("HOLD", rejected_url["status"])

        unknown = _output(manifest) + "\n其他章节自造 U:" + "a" * 64
        rejected_unknown = departments.enforce_decision_output(
            employee, unknown, provenance=manifest
        )
        self.assertEqual("HOLD", rejected_unknown["status"])

    def test_copied_labels_unknown_ri_and_chinese_system_suffix_are_hold(self):
        employee = _employee()
        disguised = departments.normalize_decision_evidence(
            employee,
            2,
            [
                {
                    "input_id": row["input_id"],
                    "content": f"{row['label']}：咖啡店 POS 订单，与该标签无实际关系",
                }
                for row in departments.decision_evidence_requirements(employee)
            ],
        )
        self.assertEqual(
            "HOLD",
            departments.enforce_decision_output(
                employee, _output(disguised), provenance=disguised
            )["status"],
        )

        manifest = _manifest(employee)
        unknown_ri = _output(manifest).replace(
            "## 数据缺口", "- 额外索引 [RI-99]\n## 数据缺口"
        )
        self.assertEqual(
            "HOLD",
            departments.enforce_decision_output(
                employee, unknown_ri, provenance=manifest
            )["status"],
        )
        invented_erp = _output(manifest).replace(
            "## 数据缺口", "- 据 ERP系统导出\n## 数据缺口"
        )
        self.assertEqual(
            "HOLD",
            departments.enforce_decision_output(
                employee, invented_erp, provenance=manifest
            )["status"],
        )

    def test_malformed_case_variant_and_duplicate_ri_tokens_are_hold(self):
        employee = _employee()
        manifest = _manifest(employee)
        original = _output(manifest)
        for token in ("[RI-999]", "[RI-9]", "[ri-99]", "[RI-AB]"):
            with self.subTest(token=token):
                attacked = original.replace(
                    "## 数据缺口", f"- 额外索引 {token}\n## 数据缺口"
                )
                rejected = departments.enforce_decision_output(
                    employee, attacked, provenance=manifest
                )
                self.assertEqual("HOLD", rejected["status"])
                self.assertFalse(rejected["passed"])

        # 正常分析里复述同一 RI 编号（引用行一次 + 说明再提一次）不再视为
        # 伪造：防伪由未知/畸形 token 全拒与"每个证据对只允许一条可复核
        # 事实行"共同保证（老板拍板 D-052：机器摩擦不算违规）。
        duplicate_mention = original.replace(
            "## 数据缺口", "- 补充说明：RI-01 的记录口径见上一行\n## 数据缺口"
        )
        allowed_mention = departments.enforce_decision_output(
            employee, duplicate_mention, provenance=manifest
        )
        self.assertEqual("GO", allowed_mention["status"])
        self.assertTrue(allowed_mention["passed"])

        # 但复制整条精确证据对行仍然失败：GO 严格文法要求每对唯一。
        pair_line = next(
            line for line in original.splitlines()
            if line.startswith(f"- [{manifest['items'][0]['input_id']}]")
        )
        duplicated_pair = original.replace(
            pair_line, pair_line + "\n" + pair_line, 1
        )
        rejected_duplicate = departments.enforce_decision_output(
            employee, duplicated_pair, provenance=manifest
        )
        self.assertEqual("HOLD", rejected_duplicate["status"])
        self.assertFalse(rejected_duplicate["passed"])

        harmless = original.replace(
            "## 数据缺口", "- 普通业务文字 [RISK-99]\n## 数据缺口"
        )
        allowed = departments.enforce_decision_output(
            employee, harmless, provenance=manifest
        )
        self.assertEqual("GO", allowed["status"])
        self.assertTrue(allowed["passed"])

        known_id = manifest["items"][0]["evidence_id"]
        for token in (
            known_id.lower(),
            "U-FAKE",
            "U\u200b:" + known_id[2:],
            "U\\:" + known_id[2:],
            "Ｕ：" + known_id[2:],
        ):
            with self.subTest(provenance_token=token):
                attacked = original.replace(
                    "## 数据缺口", f"- 额外溯源 {token}\n## 数据缺口"
                )
                rejected = departments.enforce_decision_output(
                    employee, attacked, provenance=manifest
                )
                self.assertEqual("HOLD", rejected["status"])

        ordinary_u = original.replace(
            "## 数据缺口", "- SKU 字段与 UUID 是普通业务文字\n## 数据缺口"
        )
        self.assertEqual(
            "GO",
            departments.enforce_decision_output(
                employee, ordinary_u, provenance=manifest
            )["status"],
        )
        for character in (
            "\u061c", "\u180e", "\u202a", "\u202e",
            "\u2061", "\u2069", "\u034f",
        ):
            attacked = original.replace(
                "## 数据缺口",
                f"- 额外索引 [RI{character}-99]\n## 数据缺口",
            )
            self.assertEqual(
                "HOLD",
                departments.enforce_decision_output(
                    employee, attacked, provenance=manifest
                )["status"],
            )

    def test_go_requires_one_exact_pair_per_independent_fact_line(self):
        employee = _employee()
        manifest = _manifest(employee)
        pairs = " ".join(
            f"[{item['input_id']}][{item['evidence_id']}]"
            for item in manifest["items"]
        )
        joined = (
            "# 决策\n"
            "## 决策状态\nGO\n"
            "## 事实证据/数据源\n"
            f"- 事实：综合原始记录值已提供；记录日期：2026-08-01；来源索引 {pairs}\n"
            "## 数据缺口\n无数据缺口\n"
            f"## 审批边界\n{departments.DECISION_APPROVAL_BODY}\n"
            f"## 禁止动作\n{departments.DECISION_FORBIDDEN_BODY}\n"
        )
        rejected = departments.enforce_decision_output(
            employee, joined, provenance=manifest
        )
        self.assertEqual("HOLD", rejected["status"])
        self.assertFalse(rejected["passed"])

    def test_status_ambiguity_and_execution_claims_are_hold(self):
        employee = _employee()
        manifest = _manifest(employee)
        original = _output(manifest)
        for value in (
            "GO\nHOLD", "GO / HOLD", "GO、HOLD",
            "GO；但建议HOLD", "GO (HOLD)", "GO\n- HOLD",
        ):
            with self.subTest(status=value):
                attacked = original.replace(
                    "## 决策状态\nGO", f"## 决策状态\n{value}"
                )
                self.assertEqual(
                    "HOLD",
                    departments.enforce_decision_output(
                        employee, attacked, provenance=manifest
                    )["status"],
                )

        attacks = (
            original.replace(
                departments.DECISION_APPROVAL_BODY,
                "无需人工审批，系统可自动执行改价、采购和车辆放行。",
            ),
            original.replace(
                departments.DECISION_FORBIDDEN_BODY,
                "允许系统自动执行业务写操作，包括改价和采购。",
            ),
            original + "\n系统已自动完成改价并下发采购单。",
            original + "\n已执行车辆放行。",
            original + "\n已删除故障码并关闭工单。",
            original.replace(
                departments.DECISION_APPROVAL_BODY,
                "人工审批可省略，系统可直接处理。",
            ),
            original.replace(
                departments.DECISION_FORBIDDEN_BODY,
                "不得限制系统自动改价。",
            ),
            original.replace(
                departments.DECISION_FORBIDDEN_BODY,
                "禁止人工干预，系统自动执行采购。",
            ),
            original + "\n平台已经自动完成改价。",
            original + "\n系统自动完成采购并生成订单。",
            original + "\n车辆已经放行。",
            original + "\n故障码已经删除。",
            original + "\n采购单已经下发。",
            original + "\n已完成价格写入。",
            original + "\n完成了改价并创建采购单。",
        )
        for attacked in attacks:
            rejected = departments.enforce_decision_output(
                employee, attacked, provenance=manifest
            )
            self.assertEqual("HOLD", rejected["status"])
            self.assertFalse(rejected["passed"])

        allowed = departments.enforce_decision_output(
            employee,
            original.replace("# 决策\n", "# 决策\n建议人工审批后再改价。\n", 1),
            provenance=manifest,
        )
        self.assertEqual("GO", allowed["status"])

    def test_partial_hold_coverage_wording_is_exact(self):
        employee = _employee()
        manifest = _manifest(employee, count=1)
        item = manifest["items"][0]
        missing = ["RI-02", "RI-03", "RI-04", "RI-05"]
        output = (
            "# 补证\n## 决策状态\nHOLD\n"
            "## 事实证据/数据源\n"
            f"- [{item['input_id']}][{item['evidence_id']}] "
            "事实：原始记录值已提供；记录日期：2026-08-01；来源索引：用户提交\n"
            "## 数据缺口\n待补齐：" + "、".join(missing) + "\n"
            f"## 审批边界\n{departments.DECISION_APPROVAL_BODY}\n"
            f"## 禁止动作\n{departments.DECISION_FORBIDDEN_BODY}\n"
        )
        gate = departments.enforce_decision_output(
            employee, output, provenance=manifest
        )
        self.assertEqual("HOLD", gate["status"])
        self.assertTrue(gate["passed"])
        self.assertIn("已提交 1/5", gate["output"])
        self.assertIn("缺 RI-02、RI-03、RI-04、RI-05", gate["output"])
        self.assertNotIn("必需 RI 提交与引用结构完整", gate["output"])
        from app import taskrunner
        summary = "\n".join(
            taskrunner._decision_summary_lines(
                gate["output"], employee, decision_gate=gate
            )
        )
        self.assertIn("已提交 1/5", summary)
        self.assertIn("缺 RI-02、RI-03、RI-04、RI-05", summary)
        self.assertNotIn("必需 RI 提交与引用结构完整", summary)

    def test_hidden_or_nonprose_contract_and_pair_tokens_are_hold(self):
        employee = _employee()
        manifest = _manifest(employee)
        original = _output(manifest)
        attacks = {}
        for name, transform in (
            ("pair_comment", lambda pair: f"<!-- {pair} -->"),
            ("pair_link_destination", lambda pair: f"[来源](x{pair})"),
            ("pair_link_title", lambda pair: f"[来源](x \"{pair}\")"),
            ("pair_hidden_span", lambda pair: f"<span hidden>{pair}</span>"),
            ("pair_image_alt", lambda pair: f"![{pair}](x)"),
            ("pair_image_title", lambda pair: f"![来源](x \"{pair}\")"),
        ):
            attacked = original
            for item in manifest["items"]:
                pair = f"[{item['input_id']}][{item['evidence_id']}]"
                attacked = attacked.replace(pair, transform(pair))
            attacks[name] = attacked
        attacked_reference = original
        for item in manifest["items"]:
            pair = f"[{item['input_id']}][{item['evidence_id']}]"
            attacked_reference = attacked_reference.replace(
                pair,
                f"[来源{item['input_id']}][src{item['input_id']}]",
            )
            attacked_reference += (
                f"\n[src{item['input_id']}]: x{pair} "
                f'"事实：订单金额：123；记录日期：2026-08-01"\n'
            )
        attacks["reference_definition"] = attacked_reference

        fact_syntax = (
            ("fact_image_alt", lambda fact: f"![{fact}](x)"),
            ("fact_image_title", lambda fact: f"![x](x \"{fact}\")"),
            ("fact_comment", lambda fact: f"<!-- {fact} -->"),
            ("fact_link_destination", lambda fact: f"[x](x{fact})"),
        )
        fact_text = "事实：订单金额值123；记录日期：2026-08-01；来源索引：用户提交"
        for name, transform in fact_syntax:
            attacked = original
            for item in manifest["items"]:
                pair = f"[{item['input_id']}][{item['evidence_id']}]"
                line = next(line for line in attacked.splitlines() if pair in line)
                attacked = attacked.replace(line, f"- {pair} {transform(fact_text)}")
            attacks[name] = attacked

        for name, attacked in attacks.items():
            with self.subTest(name=name):
                rejected = departments.enforce_decision_output(
                    employee, attacked, provenance=manifest
                )
                self.assertEqual("HOLD", rejected["status"])
                self.assertFalse(rejected["passed"])

        visible_inline_code = original.replace(
            f"[{manifest['items'][0]['input_id']}]"
            f"[{manifest['items'][0]['evidence_id']}]",
            f"`[{manifest['items'][0]['input_id']}]"
            f"[{manifest['items'][0]['evidence_id']}]`",
        )
        allowed = departments.enforce_decision_output(
            employee, visible_inline_code, provenance=manifest
        )
        self.assertEqual("GO", allowed["status"])
        self.assertTrue(allowed["passed"])
        from app import export
        rendered = export._md_html(allowed["output"])
        for item in manifest["items"]:
            self.assertIn(item["input_id"], rendered)
            self.assertIn(item["evidence_id"], rendered)
            self.assertIn(item["label"], rendered)
        self.assertGreaterEqual(rendered.count("2026-08-01"), len(manifest["items"]))

    def test_v1_ignores_provenance_and_remains_byte_for_byte_unchanged(self):
        legacy = next(iter(departments.legacy_specialists().values()))
        original = "# V1\n普通交付"
        result = departments.enforce_decision_output(
            legacy, original, provenance={"forged": True}
        )
        self.assertEqual(original, result["output"])
        self.assertTrue(result["passed"])


class DecisionFollowupProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "decision-followup.db")
        db.conn()
        for tenant_id in (1, 2, 3):
            db.insert("tenants", {
                "id": tenant_id,
                "name": f"t{tenant_id}",
                "balance": 10,
            })

    def tearDown(self):
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _creator(self):
        def create(task_data, _note):
            return db.insert("task", {
                **task_data,
                "status": "queued",
                "billing_status": "included",
            })
        return create

    def _charged_creator(self):
        def create(task_data, note):
            task_id = db.insert("task", {
                **task_data,
                "status": "pending_charge",
                "billing_status": "pending",
                "billing_points": 1,
            })

            def claim(connection):
                return connection.execute(
                    "UPDATE task SET status='queued',billing_status='charged' "
                    "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
                    (task_id,),
                ).rowcount == 1

            if not billing.charge_if_claimed(
                "expert_task", 2, claim, note=note, points=1
            ):
                raise RuntimeError("charge claim lost")
            return task_id
        return create

    def test_followup_merges_by_input_id_and_replay_compares_manifest(self):
        employee = _employee()
        initial = departments.normalize_decision_evidence(
            employee, 2, _raw_items(employee)[:1]
        )
        root = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": employee["idx"],
            **employeeidentity.task_fields(employee),
            "brief_json": json.dumps({
                "direction": "决策任务",
                "decision_evidence": initial,
            }, ensure_ascii=False),
            "status": "done",
            "output_md": "# 首版",
            "billing_status": "succeeded",
        })
        supplemental = _raw_items(employee)[1:]
        result = taskthreads.create_followup(
            root, 2, "decision-followup-0001", "补齐证据", self._creator(),
            evidence_items=supplemental,
            expected_emp_idx=employee["idx"],
        )
        brief = db.jloads(db.one(
            "SELECT brief_json FROM task WHERE id=?", (result["task_id"],)
        )["brief_json"])
        self.assertEqual(
            len(employee["decision_contract"]["required_inputs"]),
            len(brief["decision_evidence"]["items"]),
        )

        replay = taskthreads.create_followup(
            root, 2, "decision-followup-0001", "补齐证据", self._creator(),
            evidence_items=supplemental,
            expected_emp_idx=employee["idx"],
        )
        self.assertFalse(replay["created"])

        changed = json.loads(json.dumps(supplemental))
        changed[0]["content"] += "被篡改"
        with self.assertRaises(taskthreads.IdempotencyConflict):
            taskthreads.create_followup(
                root, 2, "decision-followup-0001", "补齐证据", self._creator(),
                evidence_items=changed,
                expected_emp_idx=employee["idx"],
            )

    def test_over_budget_merge_rejects_before_thread_task_or_charge(self):
        employee = _employee()
        requirements = departments.decision_evidence_requirements(employee)

        def full_content(row, fill):
            return row["label"] + fill * (4000 - len(row["label"]))

        base = departments.normalize_decision_evidence(
            employee,
            2,
            [{
                "input_id": row["input_id"],
                "content": full_content(row, "a"),
                "source_name": "s" * 160,
            } for row in requirements[:4]],
        )
        root = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": employee["idx"],
            **employeeidentity.task_fields(employee),
            "brief_json": json.dumps({
                "direction": "超限合并",
                "decision_evidence": base,
            }, ensure_ascii=False),
            "status": "done",
            "output_md": "# 首版",
            "billing_status": "succeeded",
        })
        fifth = requirements[4]
        with self.assertRaises(taskthreads.InvalidFollowup) as rejected:
            taskthreads.create_followup(
                root,
                2,
                "decision-over-budget-0001",
                "补齐最后一项",
                self._charged_creator(),
                evidence_items=[{
                    "input_id": fifth["input_id"],
                    "content": full_content(fifth, "b"),
                    "source_name": "t" * 160,
                }],
                expected_emp_idx=employee["idx"],
            )
        self.assertEqual("invalid_evidence_items", rejected.exception.code)
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM task")["n"])
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM task_thread")["n"])
        self.assertEqual(0, db.one("SELECT COUNT(*) n FROM billing_log")["n"])
        self.assertEqual(10, billing.balance(2))

    def test_shorter_override_within_budget_can_create_and_charge_once(self):
        employee = _employee()
        requirements = departments.decision_evidence_requirements(employee)
        items = []
        for row in requirements:
            items.append({
                "input_id": row["input_id"],
                "content": row["label"] + "a" * (3800 - len(row["label"])),
                "source_name": "s" * 140,
            })
        base = departments.normalize_decision_evidence(employee, 2, items)
        root = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": employee["idx"],
            **employeeidentity.task_fields(employee),
            "brief_json": json.dumps({
                "direction": "缩短覆盖",
                "decision_evidence": base,
            }, ensure_ascii=False),
            "status": "done",
            "output_md": "# 首版",
            "billing_status": "succeeded",
        })
        first = requirements[0]
        created = taskthreads.create_followup(
            root,
            2,
            "decision-shorter-0001",
            "用更短原始记录替换",
            self._charged_creator(),
            evidence_items=[{
                "input_id": first["input_id"],
                "content": first["label"] + "：简短原始记录",
            }],
            expected_emp_idx=employee["idx"],
        )
        self.assertTrue(created["created"])
        self.assertEqual(2, db.one("SELECT COUNT(*) n FROM task")["n"])
        self.assertEqual(1, db.one("SELECT COUNT(*) n FROM billing_log")["n"])
        self.assertEqual(9, billing.balance(2))


class DecisionTaskHTTPProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "decision-http.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "platform", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "company", "balance": 10})
        self.employee = _employee()
        db.q(
            "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
            "VALUES(?,?,1,0)",
            (2, self.employee["dept_key"]),
        )
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner",
            "role": "owner",
            "modules": [self.employee["dept_key"]],
        })

    def _binding(self):
        config = employees.get_config(self.employee["idx"])
        return {
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }

    def tearDown(self):
        auth.set_current(None)
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_initial_create_persists_manifest_and_replay_compares_it(self):
        import asyncio
        from app import main

        body = {
            "emp_idx": self.employee["idx"],
            **self._binding(),
            "brief": {
                "direction": "请判断是否可进入人工审批",
                "evidence_items": _raw_items(self.employee),
            },
            "request_key": "decision-initial-0001",
            "force": True,
        }
        with patch.object(main, "_start_expert_task_worker", return_value=None) as start:
            first = asyncio.run(main.task_create(body))
            replay = asyncio.run(main.task_create(body))
        self.assertTrue(first["created"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["task_id"], replay["task_id"])
        self.assertEqual(1, start.call_count)
        stored = db.jloads(db.one(
            "SELECT brief_json FROM task WHERE id=?", (first["task_id"],)
        )["brief_json"])
        self.assertNotIn("evidence_items", stored)
        departments.validate_decision_evidence(
            self.employee, 2, stored["decision_evidence"]
        )

        changed = json.loads(json.dumps(body))
        changed["brief"]["evidence_items"][0]["content"] += "篡改"
        with self.assertRaises(HTTPException) as conflict:
            asyncio.run(main.task_create(changed))
        self.assertEqual(409, conflict.exception.status_code)

    def test_client_cannot_inject_manifest_and_missing_items_still_creates(self):
        import asyncio
        from app import main

        with self.assertRaises(HTTPException) as forged:
            asyncio.run(main.task_create({
                "emp_idx": self.employee["idx"],
                **self._binding(),
                "brief": {
                    "direction": "伪造",
                    "decision_evidence": _manifest(self.employee),
                },
                "request_key": "decision-forged-0001",
                "force": True,
            }))
        self.assertEqual(400, forged.exception.status_code)

        with patch.object(main, "_start_expert_task_worker", return_value=None):
            created = asyncio.run(main.task_create({
                "emp_idx": self.employee["idx"],
                **self._binding(),
                "brief": {"direction": "先建任务，后续补证据"},
                "request_key": "decision-empty-0001",
                "force": True,
            }))
        stored = db.jloads(db.one(
            "SELECT brief_json FROM task WHERE id=?", (created["task_id"],)
        )["brief_json"])
        self.assertNotIn("decision_evidence", stored)

    def test_task_detail_uses_resolved_frozen_employee_and_hides_cross_tenant(self):
        from app import main

        manifest = _manifest(self.employee)
        task_id = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": self.employee["idx"],
            **employeeidentity.task_fields(self.employee),
            "brief_json": json.dumps({
                "direction": "详情",
                "decision_evidence": manifest,
            }, ensure_ascii=False),
            "status": "done",
            "billing_status": "succeeded",
        })
        detail = main.task_get(task_id)
        self.assertEqual(
            departments.decision_evidence_requirements(self.employee),
            detail["task_guide"]["evidence_requirements"],
        )
        self.assertEqual(manifest, detail["brief"]["decision_evidence"])

        auth.set_current({
            "id": 30, "tenant_id": 3, "username": "other", "role": "owner",
            "modules": [self.employee["dept_key"]],
        })
        with self.assertRaises(HTTPException) as hidden:
            main.task_get(task_id)
        self.assertEqual(404, hidden.exception.status_code)

    def test_manual_output_edit_cannot_bypass_v2_gate(self):
        from app import main

        manifest = _manifest(self.employee)
        task_id = db.insert("task", {
            "tenant_id": 2,
            "emp_idx": self.employee["idx"],
            **employeeidentity.task_fields(self.employee),
            "brief_json": json.dumps({
                "direction": "编辑门禁",
            }, ensure_ascii=False),
            "status": "done",
            "output_md": _output(manifest),
            "summary_md": "- 决策状态：GO",
            "billing_status": "succeeded",
        })
        self.assertEqual(
            {"ok": True},
            main.task_output_edit(
                task_id,
                {"md": _output(manifest)},
            ),
        )
        row = db.one(
            "SELECT output_md,summary_md FROM task WHERE id=?", (task_id,)
        )
        self.assertIn("- 决策状态：HOLD", row["output_md"])
        self.assertIn("- 决策状态：HOLD", row["summary_md"])


if __name__ == "__main__":
    unittest.main()
