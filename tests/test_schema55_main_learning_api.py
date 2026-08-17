"""Schema55 main API: frozen names and evidence learning activation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from fastapi import HTTPException

from app import (
    auth, billing, db, departments, employeeidentity, employeelearning,
    employees, learningevidence, main,
)


def _evidence_gate_fixture() -> dict:
    authority_registry = [
        {"host": "regulator.example", "match": "exact", "kind": "regulator"},
        {"host": "standards.example", "match": "suffix", "kind": "standard"},
        {"host": "official.example", "match": "exact", "kind": "official"},
    ]
    contract = {
        "public_research_topics": ["需求校准官", "订单需求预测", "漂移诊断"],
        "public_research_anchor_groups": [{
            "topic": "订单需求预测",
            "object_anchors": ["POS订单到达"],
            "method_anchors": ["分位数预测"],
        }, {
            "topic": "漂移诊断",
            "object_anchors": ["样本外偏移"],
            "method_anchors": ["MAPE诊断"],
        }],
    }
    return {
        "schema": "learning-evidence-gate-v1",
        "catalog_version": "2026.08.v4",
        "source_catalog_sha256": "1" * 64,
        "authority_policy_sha256": learningevidence.canonical_sha256(
            authority_registry
        ),
        "industry_aliases": [{
            "industry_key": "tea_coffee",
            "aliases_zh": ["茶咖", "现制饮品"],
            "aliases_en": ["tea shop", "coffee shop"],
        }],
        "employees": [{
            "employee_key": "tea-coffee-v4-test",
            "industry_key": "tea_coffee",
            "source_public_contract_sha256": learningevidence.canonical_sha256(
                contract
            ),
            "job_label_en": "Demand Calibration Specialist",
            "topics": [{
                "topic_id": "order-demand",
                "canonical_topic": "订单需求预测",
                "canonical_topic_sha256": learningevidence.canonical_sha256(
                    "订单需求预测"
                ),
                "label_en": "Order demand forecasting",
                "object_aliases_en": [{
                    "alias": "order arrivals", "source_anchor": "POS订单到达",
                }, {
                    "alias": "pos order arrivals", "source_anchor": "POS订单到达",
                }, {
                    "alias": "point of sale arrivals", "source_anchor": "POS订单到达",
                }],
                "method_aliases_en": [{
                    "alias": "quantile forecast", "source_anchor": "分位数预测",
                }, {
                    "alias": "quantile demand forecast", "source_anchor": "分位数预测",
                }, {
                    "alias": "quantile forecasting method", "source_anchor": "分位数预测",
                }],
            }, {
                "topic_id": "drift-diagnosis",
                "canonical_topic": "漂移诊断",
                "canonical_topic_sha256": learningevidence.canonical_sha256(
                    "漂移诊断"
                ),
                "label_en": "Drift diagnosis",
                "object_aliases_en": [{
                    "alias": "out of sample drift", "source_anchor": "样本外偏移",
                }, {
                    "alias": "out-of-sample drift", "source_anchor": "样本外偏移",
                }, {
                    "alias": "holdout drift", "source_anchor": "样本外偏移",
                }],
                "method_aliases_en": [{
                    "alias": "MAPE diagnosis", "source_anchor": "MAPE诊断",
                }, {
                    "alias": "mean absolute percentage error diagnosis", "source_anchor": "MAPE诊断",
                }, {
                    "alias": "mape diagnostic check", "source_anchor": "MAPE诊断",
                }],
            }],
        }],
        "authority_registry": authority_registry,
    }


def _evidence_gate_employee() -> dict:
    return {
        "key": "tea-coffee-v4-test",
        "name": "需求校准官",
        "dept_key": "tea_coffee",
        "catalog_version": "2026.08.v4",
        "public_research_topics": ["需求校准官", "订单需求预测", "漂移诊断"],
        "public_research_anchor_groups": [{
            "topic": "订单需求预测",
            "object_anchors": ["POS订单到达"],
            "method_anchors": ["分位数预测"],
        }, {
            "topic": "漂移诊断",
            "object_anchors": ["样本外偏移"],
            "method_anchors": ["MAPE诊断"],
        }],
    }


def _padded_public_alias_rows(prefix: str, anchors) -> list[dict]:
    values = [str(anchor) for anchor in list(anchors or [])[:16] if str(anchor).strip()]
    if not values:
        values = [prefix]
    rows = []
    index = 1
    while len(rows) < max(3, len(values)) and index <= 12:
        rows.append({
            "alias": f"{prefix} {index}",
            "source_anchor": values[(index - 1) % len(values)],
        })
        index += 1
    return rows


def _gate_config_for_employees(employee_rows: list[dict]):
    authority_registry = [
        {"host": "evidence1.example", "match": "exact", "kind": "regulator"},
        {"host": "evidence2.example", "match": "exact", "kind": "official"},
        {"host": "evidence3.example", "match": "exact", "kind": "industry"},
    ]
    industry_rows = []
    employee_contracts = []
    seen_industries = set()
    for employee in employee_rows:
        industry_key = str(employee["dept_key"])
        if industry_key not in seen_industries:
            seen_industries.add(industry_key)
            industry_rows.append({
                "industry_key": industry_key,
                "aliases_zh": sorted(
                    main._LEARNING_INDUSTRY_ANCHORS.get(industry_key)
                    or {industry_key}
                ),
                "aliases_en": [f"{industry_key.replace('_', ' ')} industry"],
            })
        topics = []
        for topic_index, group in enumerate(
            employee.get("public_research_anchor_groups") or [], start=1,
        ):
            canonical_topic = str(group["topic"])
            topics.append({
                "topic_id": f"topic-{topic_index}",
                "canonical_topic": canonical_topic,
                "canonical_topic_sha256": learningevidence.canonical_sha256(
                    canonical_topic
                ),
                "label_en": f"Public role topic {topic_index}",
                "object_aliases_en": _padded_public_alias_rows(
                    f"public object {topic_index}", group["object_anchors"],
                ),
                "method_aliases_en": _padded_public_alias_rows(
                    f"public method {topic_index}", group["method_anchors"],
                ),
            })
        contract = learningevidence.employee_public_contract(employee)
        employee_contracts.append({
            "employee_key": str(employee["key"]),
            "industry_key": industry_key,
            "source_public_contract_sha256": learningevidence.canonical_sha256(
                contract
            ),
            "job_label_en": f"Public role {len(employee_contracts) + 1}",
            "topics": topics,
        })
    raw = {
        "schema": "learning-evidence-gate-v1",
        "catalog_version": departments.DECISION_V4_CATALOG_VERSION,
        "source_catalog_sha256": "2" * 64,
        "authority_policy_sha256": learningevidence.canonical_sha256(
            authority_registry
        ),
        "industry_aliases": industry_rows,
        "employees": employee_contracts,
        "authority_registry": authority_registry,
    }
    return learningevidence.load_config_data(raw)


class LearningEvidenceGateContractTests(unittest.TestCase):
    def test_loader_is_strict_and_digest_binds_canonical_sidecar(self):
        config = learningevidence.load_config_data(_evidence_gate_fixture())
        self.assertRegex(config.digest, r"^[0-9a-f]{64}$")
        bad = _evidence_gate_fixture()
        bad["unexpected"] = True
        with self.assertRaisesRegex(
            learningevidence.EvidenceConfigError, "字段|field",
        ):
            learningevidence.load_config_data(bad)
        bad = _evidence_gate_fixture()
        bad["authority_registry"][0]["host"] = "*.example"
        with self.assertRaises(learningevidence.EvidenceConfigError):
            learningevidence.load_config_data(bad)

    def test_nfkc_casefold_ascii_boundaries_and_cjk_phrases(self):
        self.assertTrue(learningevidence.alias_matches(
            "ＣＯＦＦＥＥ SHOP 茶咖门店", "coffee shop",
        ))
        self.assertTrue(learningevidence.alias_matches(
            "这是茶咖门店方法", "茶咖",
        ))
        self.assertFalse(learningevidence.alias_matches(
            "coffeeshop 茶饮", "coffee",
        ))

    def test_graph_requires_direct_application_method_authority_and_domains(self):
        config = learningevidence.load_config_data(_evidence_gate_fixture())
        employee = _evidence_gate_employee()
        sources = [
            {
                "url": "https://regulator.example/a",
                "title": "Tea shop order arrivals quantile forecast",
            },
            {
                "url": "https://case-one.example/b",
                "title": "茶咖 POS订单到达 operational case",
            },
            {
                "url": "https://case-two.example/c",
                "title": "coffee shop order arrivals implementation",
            },
            {
                "url": "https://lab.standards.example/d",
                "title": "现制饮品 quantile forecast methodology",
            },
            {
                "url": "https://case-three.example/e",
                "title": "茶咖 POS订单到达 分位数预测 review",
            },
        ]
        graph = learningevidence.evaluate_evidence(
            employee, sources, config=config,
        )
        self.assertEqual(5, graph["counts"]["sources"])
        self.assertGreaterEqual(graph["counts"]["direct"], 1)
        self.assertGreaterEqual(graph["counts"]["application"], 2)
        self.assertGreaterEqual(graph["counts"]["method_authority"], 1)
        self.assertEqual("regulator", graph["sources"][0]["authority_kind"])
        self.assertEqual("standard", graph["sources"][3]["authority_kind"])

    def test_model_authority_is_ignored_and_high_risk_is_stric(self):
        config = learningevidence.load_config_data(_evidence_gate_fixture())
        employee = _evidence_gate_employee()
        sources = [{
            "url": f"https://untrusted-{i}.example/a",
            "title": "tea shop order arrivals quantile forecast",
            "authority_level": "regulator",
        } for i in range(1, 7)]
        with self.assertRaises(learningevidence.EvidenceGateError) as caught:
            learningevidence.evaluate_evidence(
                employee, sources, config=config, high_risk=True,
            )
        self.assertEqual("EVIDENCE_AUTHORITY_INSUFFICIENT", caught.exception.code)
        self.assertEqual(0, caught.exception.counts["authoritative"])

    def test_artifact_needs_same_topic_direct_and_complementary_source(self):
        config = learningevidence.load_config_data(_evidence_gate_fixture())
        graph = learningevidence.evaluate_evidence(
            _evidence_gate_employee(), [
                {
                    "url": "https://regulator.example/a",
                    "title": "tea shop order arrivals quantile forecast",
                },
                {
                    "url": "https://case-one.example/b",
                    "title": "茶咖 POS订单到达 operational case",
                },
                {
                    "url": "https://case-two.example/c",
                    "title": "coffee shop order arrivals implementation",
                },
                {
                    "url": "https://lab.standards.example/d",
                    "title": "现制饮品 quantile forecast methodology",
                },
                {
                    "url": "https://case-three.example/e",
                    "title": "茶咖 POS订单到达 分位数预测 review",
                },
            ], config=config,
        )
        learningevidence.validate_artifact_evidence([1, 2], graph["sources"])
        with self.assertRaisesRegex(
            learningevidence.EvidenceGateError, "互补|complement",
        ):
            learningevidence.validate_artifact_evidence([1], graph["sources"])


class Schema55MainPureContractTests(unittest.TestCase):
    def test_external_search_brief_contains_no_employee_identity_or_profile(self):
        employee = {
            "idx": 1001,
            "person": "绝密姓名",
            "name": "公开岗位标题",
            "dept_key": "grocery",
            "catalog_version": departments.DECISION_V4_CATALOG_VERSION,
            "identity_ref": "a" * 64,
            "professional_profile": {"scope": "绝密岗位档案"},
            "decision_contract": {"decision": "绝密决策合同"},
            "workflow": ["绝密工作流程"],
            "public_research_topics": [
                "公开岗位标题", "公开行业标准核验", "公开专业协会方法",
            ],
            "public_research_anchor_groups": [
                {
                    "topic": "公开行业标准核验",
                    "object_anchors": ["公开业务对象甲", "公开业务对象乙"],
                    "method_anchors": ["公开标准核验法"],
                },
                {
                    "topic": "公开专业协会方法",
                    "object_anchors": ["公开业务对象甲", "公开业务对象乙"],
                    "method_anchors": ["公开协会方法论"],
                },
            ],
        }
        employee["key"] = "tea-coffee-v4-test"
        employee["dept_key"] = "tea_coffee"
        employee["name"] = "需求校准官"
        employee["public_research_topics"] = [
            "需求校准官", "订单需求预测", "漂移诊断",
        ]
        employee["public_research_anchor_groups"] = [{
            "topic": "订单需求预测",
            "object_anchors": ["POS订单到达"],
            "method_anchors": ["分位数预测"],
        }, {
            "topic": "漂移诊断",
            "object_anchors": ["样本外偏移"],
            "method_anchors": ["MAPE诊断"],
        }]
        config = learningevidence.load_config_data(_evidence_gate_fixture())
        with mock.patch.object(
            main, "_learning_evidence_config", return_value=config,
        ):
            brief = main._learning_public_search_brief(employee)
        self.assertIn("茶咖现制", brief)
        self.assertIn("需求校准官", brief)
        self.assertIn("订单需求预测", brief)
        self.assertIn("pos订单到达", brief)
        self.assertIn("Demand Calibration Specialist", brief)
        for secret in (
            "1001", "绝密姓名", "绝密岗位档案", "绝密决策合同",
            "绝密工作流程", "a" * 64,
        ):
            self.assertNotIn(secret, brief)

    def test_runtime_rejects_malformed_or_overlapping_public_anchor_groups(self):
        base = {
            "idx": 1001,
            "person": "测试姓名",
            "public_research_anchor_groups": [{
                "topic": "专业专题",
                "object_anchors": ["业务对象甲", "业务对象乙"],
                "method_anchors": ["专业方法甲"],
            }, {
                "topic": "第二专业专题",
                "object_anchors": ["第二业务对象甲", "第二业务对象乙"],
                "method_anchors": ["第二专业方法甲"],
            }],
        }
        valid_tail = base["public_research_anchor_groups"][1]
        bad_groups = (
            [{
                "topic": "专业专题", "object_anchors": "业务对象甲",
                "method_anchors": ["专业方法甲"],
            }, valid_tail],
            [{
                "topic": "专业专题", "object_anchors": ["对象专业方法甲扩展"],
                "method_anchors": ["专业方法甲"],
            }, valid_tail],
            [{
                "topic": "专业专题", "object_anchors": ["业务对象甲"],
                "method_anchors": ["围绕业务对象甲的专业方法"],
            }, valid_tail],
        )
        self.assertEqual(2, len(main._learning_role_anchor_groups(base)))
        for groups in bad_groups:
            employee = dict(base, public_research_anchor_groups=groups)
            with self.assertRaisesRegex(
                employeelearning.LearningValidationError, "无效|相互重叠",
            ):
                main._learning_role_anchor_groups(employee)

    def test_local_projection_creates_four_distinct_evidence_backed_dimensions(self):
        sources = [
            {"title": f"公开来源{i}", "excerpt": f"第{i}条公开规则与方法摘要"}
            for i in range(1, 6)
        ]
        rows = main._evidence_backed_learning_artifacts(
            {"name": "需求校准官"}, sources,
        )
        self.assertEqual(
            ["knowledge", "skill", "capability", "workflow"],
            [row["kind"] for row in rows],
        )
        self.assertEqual(4, len({row["statement"] for row in rows}))
        self.assertTrue(all(len(row["source_indexes"]) >= 2 for row in rows))
        self.assertTrue(rows[-1]["payload"]["step"])

    def test_local_capability_and_workflow_deltas_are_role_specific(self):
        sources = [
            {"title": f"公开来源{i}", "excerpt": f"公开规则摘要 {i}"}
            for i in range(1, 6)
        ]
        demand = {
            "name": "需求官",
            "decision_contract": {
                "decision": "是否发布十五分钟需求分位",
                "workflow": ["构造需求特征窗", "回放需求误差"],
                "outputs": ["需求分位表"],
            },
            "professional_profile": {
                "learning_tracks": ["分位数预测"],
                "capabilities": ["识别需求断点"],
            },
        }
        bottleneck = {
            "name": "吧台瓶颈官",
            "decision_contract": {
                "decision": "是否调整吧台工位组合",
                "workflow": ["还原逐杯扫描", "定位瓶颈工位"],
                "outputs": ["瓶颈证据图"],
            },
            "professional_profile": {
                "learning_tracks": ["流程瓶颈分析"],
                "capabilities": ["识别在制品堆积"],
            },
        }
        first = main._evidence_backed_learning_artifacts(demand, sources)
        second = main._evidence_backed_learning_artifacts(bottleneck, sources)
        self.assertNotEqual(first[2]["statement"], second[2]["statement"])
        self.assertNotEqual(first[3]["payload"]["step"], second[3]["payload"]["step"])

    def test_meeting_snapshot_freezes_person_scheme_and_bundle(self):
        frozen = {
            "idx": 1001, "key": "role", "name": "岗位名",
            "dept_key": "grocery", "catalog_version": "2026.08.v4",
            "spec_sha256": "a" * 64, "person_snapshot": "赵若恒",
            "identity_scheme": "v2-person",
        }
        binding = {
            "employee": {"idx": 1001},
            "config": {
                "identity_ref": "b" * 64, "config_revision": 2,
                "config_sha256": "c" * 64, "bundle_sha256": "d" * 64,
                "person_snapshot": "赵若恒", "identity_scheme": "v2-person",
            },
        }
        with mock.patch.object(main.employeeidentity, "snapshot", return_value=frozen):
            result = main._meeting_binding_snapshot(binding)
        self.assertEqual("赵若恒", result["person_snapshot"])
        self.assertEqual("v2-person", result["identity_scheme"])
        self.assertEqual("d" * 64, result["bundle_sha256"])

    def test_production_display_uses_frozen_person_not_current_person(self):
        row = {
            "idx": 1001,
            "employee_key": "role", "employee_catalog_version": "2026.08.v4",
            "employee_name_snapshot": "冻结岗位", "employee_dept_key": "grocery",
            "employee_spec_sha256": "a" * 64,
            "employee_identity_ref": "b" * 64, "employee_config_revision": 2,
            "employee_config_sha256": "c" * 64,
            "person_snapshot": "冻结姓名", "identity_scheme": "v2-person",
            "bundle_sha256": "d" * 64,
        }
        binding = {
            "employee": {
                "idx": 1001, "name": "当前岗位", "person": "当前姓名",
                "dept_key": "grocery", "dept_name": "商超零售",
            },
            "config": {}, "role_bundle": {},
        }
        identity = {
            "identity_ref": "b" * 64, "config_revision": 2,
            "config_sha256": "c" * 64, "bundle_sha256": "d" * 64,
        }
        with (
            mock.patch.object(main.employeeidentity, "resolve_task_binding", return_value=binding),
            mock.patch.object(main, "_employee_public_contract", return_value=identity),
        ):
            _signature, public = main._production_identity(row)
        self.assertEqual("冻结姓名·冻结岗位", public["name"])
        self.assertNotIn("当前姓名", public["name"])


class Schema55MainLearningApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = os.path.join(self.tmp.name, "schema55-main-learning.db")
        departments.reset_cache()
        db.conn()
        auth.set_current({
            "id": 1, "tenant_id": 1, "role": "root", "username": "boss",
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
        db._shutdown_async_pool(wait=True)
        db._close_all_connections()
        db._conn = None
        db._conn_path = None
        db.DB_PATH = self.old_path
        departments.reset_cache()
        self.tmp.cleanup()

    @staticmethod
    def _source(index: int) -> dict:
        domain = f"evidence{((index - 1) % 3) + 1}.example"
        return {
            "url": f"https://{domain}/method/{index}",
            "title": f"行业方法 {index}",
            "publisher": f"发布机构 {index}",
            "authority_level": "official" if index == 1 else "industry",
            "published_at": "2026-08-01",
            "fetched_at": time.time(),
            "http_status": 200,
            "tls_valid": True,
            "content_sha256": hashlib.sha256(f"source-{index}".encode()).hexdigest(),
            "excerpt": (
                f"茶咖现制饮品的POS订单到达分位数需求预测与置信区间、天气事件与渠道截尾"
                f"的可核验方法和工作步骤 {index}"
            ),
            "capture_event_id": f"websearch-{index}",
            "capture_provider": "websearch",
        }

    @staticmethod
    def _unrelated_source(index: int) -> dict:
        domain = f"space{((index - 1) % 3) + 1}.example"
        return {
            "url": f"https://{domain}/mars/{index}",
            "title": f"火星岩石光谱研究 {index}",
            "publisher": f"深空探测机构 {index}",
            "authority_level": "official" if index == 1 else "research",
            "published_at": "2026-08-01",
            "fetched_at": time.time(), "http_status": 200, "tls_valid": True,
            "content_sha256": hashlib.sha256(f"mars-{index}".encode()).hexdigest(),
            "excerpt": f"火星矿物光谱、轨道探测器与深空通信研究结论 {index}",
            "capture_event_id": f"mars-search-{index}",
            "capture_provider": "websearch",
        }

    def _pending_run(self, request_key: str) -> tuple[dict, dict, dict, dict]:
        employee = employeeidentity.active_employee(1001)
        config = employees.get_config(1001)
        batch = employeelearning.create_batch(
            f"{request_key}-batch", budget_cap_points=3, tenant_id=1,
        )
        run = employeelearning.create_run(
            batch["id"],
            f"{request_key}-run",
            employee_idx=1001,
            identity_ref=config["identity_ref"],
            base_config_revision=config["config_revision"],
            base_config_sha256=config["config_sha256"],
            industry_key="tea_coffee",
            budget_points=3,
        )
        employeelearning.reserve_budget(run["id"])
        employeelearning.start_run(run["id"])
        employeelearning.checkpoint(run["id"], {
            "expected_bundle_sha256": config["bundle_sha256"],
            "evidence_gate_digest": main._learning_evidence_config().digest,
        })
        sources = [self._source(index) for index in range(1, 6)]
        artifacts = main._evidence_backed_learning_artifacts(employee, sources)
        employeelearning.research_run(
            run["id"],
            lambda _context: {"sources": sources, "artifacts": artifacts},
        )
        body = {
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }
        return employee, config, employeelearning.get_run(run["id"]), body

    def test_approval_fails_closed_when_frozen_gate_digest_drifts(self):
        _employee, config, run, body = self._pending_run("gate-drift")
        current_gate = main._learning_evidence_config()
        changed = dict(current_gate.canonical_data)
        changed["source_catalog_sha256"] = "f" * 64
        drifted_gate = learningevidence.load_config_data(changed)
        self.assertNotEqual(current_gate.digest, drifted_gate.digest)
        with (
            mock.patch.object(
                main, "_learning_evidence_config", return_value=drifted_gate,
            ),
            self.assertRaises(HTTPException) as caught,
        ):
            main.employee_learning_run_approve(run["id"], body)
        self.assertEqual(409, caught.exception.status_code)
        self.assertIn("门禁版本", str(caught.exception.detail))
        persisted = employeelearning.get_run(run["id"])
        self.assertEqual(
            employeelearning.RUN_AWAITING_APPROVAL, persisted["status"],
        )
        self.assertEqual(config["config_revision"], employees.get_config(1001)["config_revision"])

    def test_worker_freezes_gate_digest_before_accepting_evidence(self):
        employee = employeeidentity.active_employee(1001)
        config = employees.get_config(1001)
        batch = employeelearning.create_batch(
            "gate-freeze-batch", budget_cap_points=3, tenant_id=1,
        )
        run = employeelearning.create_run(
            batch["id"], "gate-freeze-run", employee_idx=1001,
            identity_ref=config["identity_ref"],
            base_config_revision=config["config_revision"],
            base_config_sha256=config["config_sha256"],
            industry_key="tea_coffee", budget_points=3,
        )
        employeelearning.reserve_budget(run["id"])
        employeelearning.start_run(run["id"])
        employeelearning.checkpoint(run["id"], {
            "expected_bundle_sha256": config["bundle_sha256"],
        })
        with mock.patch.object(
            main.providers, "call_verified_learning_research",
            new=mock.AsyncMock(return_value={
                "sources": [self._source(index) for index in range(1, 6)],
            }),
        ):
            asyncio.run(main._employee_learning_research_worker(
                run["id"], {"employee": employee, "config": config},
            ))
        persisted = employeelearning.get_run(run["id"])
        self.assertEqual(
            main._learning_evidence_config().digest,
            main._learning_run_checkpoint(persisted)["evidence_gate_digest"],
        )
        self.assertEqual(employeelearning.RUN_ACTIVATED, persisted["status"])
        self.assertEqual(
            config["config_revision"] + 1,
            employees.get_config(1001)["config_revision"],
        )

    def test_three_consecutive_zero_direct_gates_pause_batch(self):
        batch = employeelearning.create_batch(
            "zero-direct-batch", budget_cap_points=12, tenant_id=1,
        )
        runs = []
        for index in range(4):
            run = employeelearning.create_run(
                batch["id"], f"zero-direct-run-{index}",
                employee_idx=9000 + index,
                identity_ref=hashlib.sha256(f"identity-{index}".encode()).hexdigest(),
                base_config_revision=1,
                base_config_sha256=hashlib.sha256(
                    f"config-{index}".encode()
                ).hexdigest(),
                industry_key="tea_coffee", budget_points=3,
            )
            runs.append(run)
        for index, run in enumerate(runs[:3], start=1):
            state = main._record_learning_evidence_gate_outcome(
                run["id"], "EVIDENCE_ZERO_DIRECT",
            )
            checkpoint = main._learning_batch_json(state["checkpoint_json"], {})
            self.assertEqual(index, checkpoint["evidence_zero_direct_streak"])
        self.assertEqual(employeelearning.BATCH_PAUSED, state["status"])
        self.assertIn("连续3个", str(state["paused_reason"]))

    def test_create_is_inert_then_exact_approval_advances_effective_bundle(self):
        employee = employeeidentity.active_employee(1001)
        config = employees.get_config(1001)
        body = {
            "request_key": "learning-api-case-001",
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }

        async def no_network_worker(_run_id, _binding):
            return None

        with mock.patch.object(
            main, "_employee_learning_research_worker", new=no_network_worker,
        ):
            created = asyncio.run(main.employee_learning_run_create(1001, body))
        run = created["run"]
        self.assertEqual("researching", run["status"])
        self.assertEqual([], run["artifacts"])
        self.assertEqual(config["bundle_sha256"], run["bundle_sha256"])
        self.assertEqual(
            config["config_revision"], employees.get_config(1001)["config_revision"]
        )

        sources = [self._source(index) for index in range(1, 6)]
        artifacts = main._evidence_backed_learning_artifacts(employee, sources)
        employeelearning.checkpoint(run["id"], {
            **main._learning_run_checkpoint(employeelearning.get_run(run["id"])),
            "evidence_gate_digest": main._learning_evidence_config().digest,
        })
        employeelearning.research_run(
            run["id"], lambda _context: {"sources": sources, "artifacts": artifacts},
        )
        pending = main.employee_learning_run_get(run["id"])["run"]
        self.assertEqual("awaiting_approval", pending["status"])
        self.assertEqual(5, len(pending["sources"]))
        self.assertEqual(4, len(pending["artifacts"]))
        self.assertEqual(
            config["config_revision"], employees.get_config(1001)["config_revision"]
        )
        detail = main.dept_emp(1001)
        self.assertEqual(run["id"], detail["learning_run"]["id"])
        self.assertEqual(1, len(detail["learning_runs"]))
        self.assertEqual(
            detail["learning_run"]["sources"][0]["source_url"],
            detail["learning_run"]["sources"][0]["canonical_url"],
        )

        approved = main.employee_learning_run_approve(run["id"], body)["run"]
        self.assertEqual("activated", approved["status"])
        self.assertTrue(approved["reviewed_at"])
        self.assertEqual(1, approved["reviewer_id"])
        self.assertTrue(approved["artifacts"])
        self.assertTrue(all(
            artifact["reviewer_id"] == 1 and artifact["reviewed_at"]
            for artifact in approved["artifacts"]
        ))
        after = employees.get_config(1001)
        self.assertEqual(config["config_revision"] + 1, after["config_revision"])
        self.assertNotEqual(config["bundle_sha256"], after["bundle_sha256"])
        self.assertTrue(after["learning_evidence"])
        self.assertGreater(
            len(after["effective_profile"].get("capabilities") or []),
            len(config["effective_profile"].get("capabilities") or []),
        )
        self.assertGreater(
            len(after["effective_workflow"]), len(config["effective_workflow"]),
        )

    def test_approval_rejects_bundle_cas_drift(self):
        run = {
            "employee_idx": 1001,
            "identity_ref": "a" * 64,
            "base_config_revision": 2,
            "base_config_sha256": "b" * 64,
            "checkpoint_json": {"expected_bundle_sha256": "c" * 64},
        }
        with self.assertRaises(Exception) as caught:
            main._learning_request_binding(run, {
                "identity_ref": "a" * 64, "config_revision": 2,
                "config_sha256": "b" * 64, "bundle_sha256": "d" * 64,
            })
        self.assertEqual(409, caught.exception.status_code)

    def test_wrong_client_binding_never_marks_pending_proposal_stale(self):
        _employee, _config, run, body = self._pending_run(
            "learning-client-binding-mismatch-001"
        )
        wrong = {**body, "bundle_sha256": "f" * 64}
        if wrong["bundle_sha256"] == body["bundle_sha256"]:
            wrong["bundle_sha256"] = "e" * 64

        for action in (
            main.employee_learning_run_approve,
            main.employee_learning_run_reject,
        ):
            with self.subTest(action=action.__name__), self.assertRaises(
                HTTPException
            ) as conflict:
                action(run["id"], wrong)
            self.assertEqual(409, conflict.exception.status_code)
            latest = employeelearning.get_run(run["id"])
            self.assertEqual(
                employeelearning.RUN_AWAITING_APPROVAL, latest["status"]
            )
            self.assertEqual(
                run["id"],
                employeelearning._identity_run_owner(run["identity_ref"])["id"],
            )
            self.assertTrue(all(
                artifact["status"] == "proposed"
                for artifact in latest["artifacts"]
            ))

        rejected = main.employee_learning_run_reject(
            run["id"], {**body, "reason": "客户端绑定核对后人工驳回"},
        )["run"]
        self.assertEqual(employeelearning.RUN_REJECTED, rejected["status"])
        self.assertIsNone(
            employeelearning._identity_run_owner(run["identity_ref"])
        )

    def test_true_server_binding_drift_marks_approval_stale_and_releases_owner(self):
        _employee, config, run, body = self._pending_run(
            "learning-server-binding-drift-approve-001"
        )
        employees.set_settings_for_identity(
            config["identity_ref"],
            {"server_drift_probe": True},
            expected_revision=config["config_revision"],
        )

        with self.assertRaises(HTTPException) as stale:
            main.employee_learning_run_approve(run["id"], body)
        self.assertEqual(409, stale.exception.status_code)
        latest = employeelearning.get_run(run["id"])
        self.assertEqual(employeelearning.RUN_STALE, latest["status"])
        self.assertIsNone(
            employeelearning._identity_run_owner(run["identity_ref"])
        )
        self.assertTrue(all(
            artifact["status"] == "stale"
            for artifact in latest["artifacts"]
        ))

    def test_reject_exact_frozen_binding_can_close_after_server_drift(self):
        _employee, config, run, body = self._pending_run(
            "learning-server-binding-drift-reject-001"
        )
        employees.set_settings_for_identity(
            config["identity_ref"],
            {"server_drift_probe": True},
            expected_revision=config["config_revision"],
        )

        rejected = main.employee_learning_run_reject(
            run["id"], {**body, "reason": "当前岗位已更新，终结旧提案"},
        )["run"]
        self.assertEqual(employeelearning.RUN_REJECTED, rejected["status"])
        self.assertEqual(1, rejected["reviewer_id"])
        self.assertTrue(rejected["reviewed_at"])
        self.assertIsNone(
            employeelearning._identity_run_owner(run["identity_ref"])
        )
        self.assertTrue(all(
            artifact["status"] == "rejected"
            and artifact["reviewer_id"] == 1
            and artifact["reviewed_at"] == rejected["reviewed_at"]
            for artifact in rejected["artifacts"]
        ))

    def test_reject_returns_and_persists_boss_review_audit(self):
        employee = employeeidentity.active_employee(1001)
        config = employees.get_config(1001)
        batch = employeelearning.create_batch(
            "learning-reject-audit-batch", budget_cap_points=3, tenant_id=1,
        )
        run = employeelearning.create_run(
            batch["id"],
            "learning-reject-audit-run",
            employee_idx=1001,
            identity_ref=config["identity_ref"],
            base_config_revision=config["config_revision"],
            base_config_sha256=config["config_sha256"],
            industry_key="tea_coffee",
            budget_points=3,
        )
        employeelearning.start_run(run["id"])
        employeelearning.checkpoint(run["id"], {
            "expected_bundle_sha256": config["bundle_sha256"],
        })
        sources = [self._source(index) for index in range(1, 6)]
        artifacts = main._evidence_backed_learning_artifacts(employee, sources)
        employeelearning.research_run(
            run["id"],
            lambda _context: {"sources": sources, "artifacts": artifacts},
        )
        body = {
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
            "reason": "证据与本次岗位目标不匹配",
        }
        rejected = main.employee_learning_run_reject(run["id"], body)["run"]
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual(1, rejected["reviewer_id"])
        self.assertTrue(rejected["reviewed_at"])
        self.assertTrue(rejected["artifacts"])
        self.assertTrue(all(
            artifact["status"] == "rejected"
            and artifact["reviewer_id"] == 1
            and artifact["reviewed_at"] == rejected["reviewed_at"]
            for artifact in rejected["artifacts"]
        ))
        self.assertEqual(
            config["config_revision"], employees.get_config(1001)["config_revision"]
        )

    def test_employee_learning_history_is_tenant_scoped(self):
        config = employees.get_config(1001)

        def make(tenant_id: int, key: str) -> int:
            batch = employeelearning.create_batch(
                key, budget_cap_points=3, tenant_id=tenant_id,
            )
            run = employeelearning.create_run(
                batch["id"], f"run-{key}", employee_idx=1001,
                identity_ref=config["identity_ref"],
                base_config_revision=config["config_revision"],
                base_config_sha256=config["config_sha256"],
                industry_key="tea_coffee", budget_points=3,
            )
            employeelearning.reserve_budget(run["id"])
            employeelearning.start_run(run["id"])
            employeelearning.checkpoint(run["id"], {
                "expected_bundle_sha256": config["bundle_sha256"],
            })
            return int(run["id"])

        own_id = make(1, "tenant-one-learning")
        # 身份锁是全局岗位版本锁；先将租户1运行收口，
        # 再验证租户2的后续运行不会被租户1历史读到。
        employeelearning.cancel_run(own_id, "TENANT_SCOPE_FIXTURE_TERMINAL")
        other_id = make(2, "tenant-two-learning")
        history = main._employee_learning_history(config["identity_ref"])
        ids = [run["id"] for run in history["runs"]]
        self.assertIn(own_id, ids)
        self.assertNotIn(other_id, ids)

    def test_unrelated_web_evidence_cannot_activate_a_role(self):
        employee = employeeidentity.active_employee(1001)
        config = employees.get_config(1001)
        body = {
            "request_key": "learning-mars-evidence-001",
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }

        async def no_network_worker(_run_id, _binding):
            return None

        with mock.patch.object(
            main, "_employee_learning_research_worker", new=no_network_worker,
        ):
            created = asyncio.run(main.employee_learning_run_create(1001, body))
        run_id = created["run"]["id"]
        sources = [self._unrelated_source(index) for index in range(1, 6)]
        # A tampered researcher may pair unrelated captured pages with a
        # superficially valid role proposal. Approval must recompute both
        # semantic relevance and the frozen source digests before activation.
        artifacts = main._evidence_backed_learning_artifacts(
            employee, [self._source(index) for index in range(1, 6)],
        )
        employeelearning.research_run(
            run_id, lambda _context: {"sources": sources, "artifacts": artifacts},
        )
        with self.assertRaises(HTTPException) as caught:
            main.employee_learning_run_approve(run_id, body)
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(
            config["config_revision"], employees.get_config(1001)["config_revision"]
        )

    def test_same_industry_branding_pages_do_not_train_demand_role(self):
        employee = employeeidentity.active_employee(1001)
        sources = []
        for index in range(1, 6):
            source = self._source(index)
            source.update({
                "title": f"茶咖品牌视觉与社媒投放案例 {index}",
                "excerpt": (
                    "围绕门店空间软装、品牌色彩、社交媒体内容和活动操作的"
                    f"传播复盘 {index}"
                ),
            })
            sources.append(source)
        with self.assertRaises(employeelearning.LearningValidationError):
            main._learning_relevant_sources(employee, sources)

    def test_other_industry_demand_forecasts_do_not_train_tea_role(self):
        employee = employeeidentity.active_employee(1001)
        sources = []
        for index in range(1, 6):
            source = self._source(index)
            source.update({
                "title": f"电力系统需求预测研究 {index}",
                "excerpt": (
                    "电力系统负荷需求预测、输电调度、发电备用和电价出清的"
                    f"分位数方法 {index}"
                ),
            })
            sources.append(source)
        with self.assertRaises(employeelearning.LearningValidationError):
            main._learning_relevant_sources(employee, sources)

    def test_role_name_fragment_and_training_support_do_not_authorize_learning(self):
        employee = employeeidentity.active_employee(1001)
        for theme in (
            "十五分钟配送服务承诺、到店取餐体验与骑手履约案例",
            "门店员工训练支持计划、服务礼仪培训与新员工带教",
        ):
            sources = []
            for index in range(1, 6):
                source = self._source(index)
                source.update({
                    "title": f"茶咖现制饮品{theme} {index}",
                    "excerpt": f"茶咖行业{theme}的通用经验 {index}",
                    "content_sha256": hashlib.sha256(
                        f"generic-{theme}-{index}".encode()
                    ).hexdigest(),
                })
                sources.append(source)
            with self.assertRaises(employeelearning.LearningValidationError):
                main._learning_relevant_sources(employee, sources)

    def test_cross_role_generic_case_review_does_not_train_hotel_inventory_role(self):
        employee = employeeidentity.active_employee(1502)
        sources = []
        for index in range(1, 6):
            source = self._source(index)
            source.update({
                "title": f"酒店行业通用服务案例复盘 {index}",
                "excerpt": (
                    "酒店行业通用服务案例复盘与员工培训经验，"
                    f"聚焦微笑礼仪和通用沟通技巧 {index}"
                ),
                "content_sha256": hashlib.sha256(
                    f"hotel-generic-{index}".encode()
                ).hexdigest(),
            })
            sources.append(source)
        with self.assertRaises(employeelearning.LearningValidationError):
            main._learning_relevant_sources(employee, sources)

    def test_short_generic_topic_does_not_train_convenience_planogram_role(self):
        employee = employeeidentity.active_employee(1117)
        sources = []
        for index in range(1, 6):
            source = self._source(index)
            source.update({
                "title": f"便利店网红优惠券广告预算例外审批 {index}",
                "excerpt": f"便利店营销投放的例外审批经验 {index}",
                "content_sha256": hashlib.sha256(
                    f"approval-generic-{index}".encode()
                ).hexdigest(),
            })
            sources.append(source)
        with self.assertRaises(employeelearning.LearningValidationError):
            main._learning_relevant_sources(employee, sources)

    def test_professional_paraphrase_can_pass_without_copying_catalog_sentence(self):
        employee = employeeidentity.active_employee(1001)
        sources = []
        for index in range(1, 6):
            source = self._source(index)
            source.update({
                "title": f"茶饮门店分位回归和预测区间实务 {index}",
                "excerpt": (
                    "茶饮门店用分位回归估算每十五分钟订单需求，"
                    f"并用预测区间校准节庆与气温不确定性 {index}"
                ),
                "content_sha256": hashlib.sha256(
                    f"paraphrase-{index}".encode()
                ).hexdigest(),
            })
            sources.append(source)
        accepted = main._learning_relevant_sources(employee, sources)
        self.assertEqual(5, len(accepted))
        self.assertTrue(all(
            "分位数需求预测与置信区间" in row["semantic_topics"]
            for row in accepted
        ))

    def test_same_industry_unrelated_critical_path_does_not_train_repair_progress(self):
        employee = employeeidentity.active_employee(1627)
        sources = []
        for index in range(1, 7):
            source = self._source(index)
            source.update({
                "title": f"汽车维修品牌官网改版项目 {index}",
                "excerpt": f"汽车维修品牌官网改版的关键路径分析与页面上线排期 {index}",
                "authority_level": "official" if index <= 2 else "industry",
                "content_sha256": hashlib.sha256(
                    f"auto-site-{index}".encode()
                ).hexdigest(),
            })
            sources.append(source)
        with self.assertRaises(employeelearning.LearningValidationError):
            main._learning_relevant_sources(employee, sources)

    def test_hotel_inventory_paraphrase_maps_to_multiple_professional_concepts(self):
        employee = employeeidentity.active_employee(1502)
        sources = []
        for index in range(1, 6):
            source = self._source(index)
            source.update({
                "title": f"酒店房类对应和升等路径实务 {index}",
                "excerpt": (
                    "酒店实际客房与线上售卖房类建立对应关系，"
                    f"升等路径在连续入住订单中断裂时清理过时关系 {index}"
                ),
                "content_sha256": hashlib.sha256(
                    f"hotel-paraphrase-{index}".encode()
                ).hexdigest(),
            })
            sources.append(source)
        accepted = main._learning_relevant_sources(employee, sources)
        self.assertEqual(5, len(accepted))
        topics = {topic for row in accepted for topic in row["semantic_topics"]}
        self.assertIn("物理房与虚拟房型映射", topics)
        self.assertIn("升级链与连住截断", topics)

    def test_different_verified_topics_create_different_frozen_deltas(self):
        employee = employeeidentity.active_employee(1001)

        def evidence(theme: str, key: str) -> list[dict]:
            rows = []
            for index in range(1, 6):
                source = self._source(index)
                source.update({
                    "title": f"茶咖现制饮品{theme} {index}",
                    "excerpt": f"茶咖门店十五分钟订单与{theme}的可核验方法 {index}",
                    "content_sha256": hashlib.sha256(
                        f"{key}-{index}".encode()
                    ).hexdigest(),
                })
                rows.append(source)
            return rows

        quantile = main._evidence_backed_learning_artifacts(
            employee, evidence("分位数需求预测与置信区间", "quantile"),
        )
        drift = main._evidence_backed_learning_artifacts(
            employee, evidence("MAPE与样本外漂移诊断", "drift"),
        )
        self.assertNotEqual(
            [row["statement"] for row in quantile],
            [row["statement"] for row in drift],
        )
        self.assertNotEqual(
            quantile[0]["payload"]["evidence_topics"],
            drift[0]["payload"]["evidence_topics"],
        )
        self.assertNotEqual(
            quantile[0]["payload"]["evidence_sources"],
            drift[0]["payload"]["evidence_sources"],
        )

    def test_failed_research_refunds_wallet_and_batch_reservation(self):
        db.insert("tenants", {"id": 2, "name": "测试企业", "balance": 6})
        auth.set_current({
            "id": 20, "tenant_id": 2, "role": "root", "username": "boss",
            "modules": ["*"],
        })
        config = employees.get_config(1001)
        body = {
            "request_key": "learning-refund-case-001",
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }

        async def scenario():
            with mock.patch.object(
                main.providers,
                "call_verified_learning_research",
                new=mock.AsyncMock(side_effect=RuntimeError("provider down")),
            ):
                result = await main.employee_learning_run_create(1001, body)
                await asyncio.sleep(0.1)
                return result

        created = asyncio.run(scenario())
        run = employeelearning.get_run(created["run"]["id"])
        self.assertEqual(employeelearning.RUN_FAILED, run["status"])
        self.assertEqual(0, float(run["spent_points"]))
        self.assertEqual(6, billing.balance(2))
        operation = db.one(
            "SELECT status FROM billing_operation WHERE action='learn' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        self.assertEqual("refunded", operation["status"])

    def test_single_run_insufficient_points_retries_same_idempotency_key(self):
        db.insert("tenants", {"id": 2, "name": "补款企业", "balance": 0})
        auth.set_current({
            "id": 20, "tenant_id": 2, "role": "root",
            "username": "boss", "modules": ["*"],
        })
        config = employees.get_config(1001)
        body = {
            "request_key": "learning-top-up-retry-001",
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }
        with self.assertRaises(HTTPException) as insufficient:
            asyncio.run(main.employee_learning_run_create(1001, body))
        self.assertEqual(402, insufficient.exception.status_code)
        run = employeelearning.list_batch_runs(1)[0]
        self.assertEqual(employeelearning.RUN_QUEUED, run["status"])
        self.assertEqual(0.0, run["spent_points"])

        db.execute("UPDATE tenants SET balance=6 WHERE id=2")

        async def no_network_worker(_run_id, _binding):
            return None

        with mock.patch.object(
            main, "_employee_learning_research_worker", new=no_network_worker,
        ):
            retried = asyncio.run(main.employee_learning_run_create(1001, body))
        self.assertTrue(retried["started"])
        self.assertEqual(
            employeelearning.RUN_RESEARCHING, retried["run"]["status"]
        )
        self.assertEqual(3.0, billing.balance(2))

    def test_single_run_cancel_during_prepare_still_launches_and_replays_safely(self):
        config = employees.get_config(1001)
        body = {
            "request_key": "learning-single-cancel-barrier-001",
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }
        checkpoint_entered = threading.Event()
        release_checkpoint = threading.Event()
        real_checkpoint = employeelearning.checkpoint

        def blocked_checkpoint(run_id, payload):
            if isinstance(payload, dict) and payload.get("stage") == "billing_pending":
                checkpoint_entered.set()
                if not release_checkpoint.wait(timeout=3):
                    raise RuntimeError("single-run checkpoint barrier timed out")
            return real_checkpoint(run_id, payload)

        async def scenario():
            worker_started = asyncio.Event()
            release_worker = asyncio.Event()
            worker_finished = asyncio.Event()

            async def held_worker(run_id, _binding):
                worker_started.set()
                await release_worker.wait()
                await main._run_db_safely(
                    billing.fail_operation,
                    main._learning_billing_op_key(1, run_id),
                    "TEST_DONE",
                )
                await db.arun(
                    employeelearning.cancel_run, run_id, reason="TEST_DONE",
                )
                await db.arun(employeelearning.release_budget, run_id)
                worker_finished.set()

            with (
                mock.patch.object(
                    main.employeelearning,
                    "checkpoint",
                    side_effect=blocked_checkpoint,
                ),
                mock.patch.object(
                    main, "_employee_learning_research_worker", new=held_worker,
                ),
            ):
                request = asyncio.create_task(
                    main.employee_learning_run_create(1001, body)
                )
                entered = await asyncio.to_thread(checkpoint_entered.wait, 2)
                self.assertTrue(entered)
                request.cancel()
                await asyncio.sleep(0)
                release_checkpoint.set()
                with self.assertRaises(asyncio.CancelledError):
                    await request

                await asyncio.wait_for(worker_started.wait(), timeout=2)
                batch = employeelearning.get_batch(1)
                run = employeelearning.list_batch_runs(batch["id"])[0]
                checkpoint = main._learning_run_checkpoint(run)
                self.assertEqual(employeelearning.RUN_RESEARCHING, run["status"])
                self.assertEqual("researching", checkpoint["stage"])
                self.assertEqual(
                    main._learning_billing_op_key(1, run["id"]),
                    checkpoint["billing_op_key"],
                )

                replay = await main.employee_learning_run_create(1001, body)
                self.assertFalse(replay["started"])
                self.assertEqual(run["id"], replay["run"]["id"])
                self.assertEqual(
                    employeelearning.RUN_RESEARCHING, replay["run"]["status"]
                )
                release_worker.set()
                await asyncio.wait_for(worker_finished.wait(), timeout=2)

        asyncio.run(scenario())

    def test_expired_pending_is_lazily_closed_before_approval_and_new_run(self):
        _employee, config, run, body = self._pending_run(
            "learning-expired-owner-release-001"
        )
        db.execute(
            "UPDATE employee_learning_run SET expires_at=? WHERE id=?",
            (time.time() - 5, int(run["id"])),
        )

        with self.assertRaises(HTTPException) as expired:
            main.employee_learning_run_approve(int(run["id"]), body)
        self.assertEqual(409, expired.exception.status_code)
        closed = employeelearning.get_run(int(run["id"]))
        self.assertEqual(employeelearning.RUN_EXPIRED, closed["status"])
        self.assertIsNone(closed.get("reviewer_id"))
        self.assertIsNone(closed.get("reviewed_at"))
        self.assertIsNone(
            employeelearning._identity_run_owner(config["identity_ref"])
        )
        self.assertEqual(
            employeelearning.BATCH_COMPLETED,
            employeelearning.get_batch(int(run["batch_id"]))["status"],
        )

        async def no_network_worker(_run_id, _binding):
            return None

        with mock.patch.object(
            main, "_employee_learning_research_worker", new=no_network_worker,
        ):
            restarted = asyncio.run(main.employee_learning_run_create(1001, {
                **body,
                "request_key": "learning-after-expired-owner-001",
            }))
        self.assertTrue(restarted["started"])
        self.assertNotEqual(run["id"], restarted["run"]["id"])

    def test_future_pending_is_not_expired_by_lazy_read(self):
        _employee, _config, run, _body = self._pending_run(
            "learning-future-expiry-safe-001"
        )
        db.execute(
            "UPDATE employee_learning_run SET expires_at=? WHERE id=?",
            (time.time() + 3600, int(run["id"])),
        )
        public = main.employee_learning_run_get(int(run["id"]))["run"]
        self.assertEqual(employeelearning.RUN_AWAITING_APPROVAL, public["status"])
        self.assertIsNotNone(
            employeelearning._identity_run_owner(run["identity_ref"])
        )

    def test_current_ineligible_approval_stales_but_exact_reject_still_audits(self):
        _employee, config, run, body = self._pending_run(
            "learning-ineligible-approve-001"
        )
        slot = employees.slot_state(1001)
        employees.set_enabled(
            1001, False, expected_row_version=slot["row_version"],
        )
        with self.assertRaises(HTTPException) as ineligible:
            main.employee_learning_run_approve(int(run["id"]), body)
        self.assertEqual(409, ineligible.exception.status_code)
        self.assertEqual(
            employeelearning.RUN_STALE,
            employeelearning.get_run(int(run["id"]))["status"],
        )
        self.assertIsNone(
            employeelearning._identity_run_owner(config["identity_ref"])
        )

        disabled = employees.slot_state(1001)
        employees.set_enabled(
            1001, True, expected_row_version=disabled["row_version"],
        )
        _employee2, _config2, reject_run, reject_body = self._pending_run(
            "learning-ineligible-reject-001"
        )
        enabled = employees.slot_state(1001)
        employees.set_enabled(
            1001, False, expected_row_version=enabled["row_version"],
        )
        rejected = main.employee_learning_run_reject(
            int(reject_run["id"]), reject_body,
        )["run"]
        self.assertEqual(employeelearning.RUN_REJECTED, rejected["status"])
        self.assertEqual(1, rejected["reviewer_id"])
        self.assertTrue(rejected["reviewed_at"])

    def test_activation_transaction_rechecks_eligibility_after_preflight(self):
        _employee, _config, run, body = self._pending_run(
            "learning-activation-slot-cas-001"
        )
        real_current_binding = main._learning_current_run_binding

        def read_then_disable(candidate):
            binding = real_current_binding(candidate)
            slot = employees.slot_state(1001)
            employees.set_enabled(
                1001, False, expected_row_version=slot["row_version"],
            )
            return binding

        with (
            mock.patch.object(
                main,
                "_learning_current_run_binding",
                side_effect=read_then_disable,
            ),
            self.assertRaises(HTTPException) as stale,
        ):
            main.employee_learning_run_approve(int(run["id"]), body)
        self.assertEqual(409, stale.exception.status_code)
        closed = employeelearning.get_run(int(run["id"]))
        self.assertEqual(employeelearning.RUN_STALE, closed["status"])
        self.assertIsNone(
            employeelearning._identity_run_owner(run["identity_ref"])
        )
        self.assertEqual(
            int(run["base_config_revision"]),
            employees.get_config(1001)["config_revision"],
        )

    def test_activation_internal_runtime_failure_keeps_proposal_awaiting(self):
        _employee, _config, run, body = self._pending_run(
            "learning-activation-internal-runtime-001"
        )
        with (
            mock.patch.object(
                employees,
                "activate_learning_bundle",
                side_effect=RuntimeError("injected storage failure"),
            ),
            self.assertRaises(HTTPException) as unavailable,
        ):
            main.employee_learning_run_approve(int(run["id"]), body)
        self.assertEqual(503, unavailable.exception.status_code)
        self.assertNotIn("storage", str(unavailable.exception.detail).lower())
        latest = employeelearning.get_run(int(run["id"]))
        self.assertEqual(employeelearning.RUN_AWAITING_APPROVAL, latest["status"])
        self.assertEqual(
            int(run["id"]),
            employeelearning._identity_run_owner(run["identity_ref"])["id"],
        )
        self.assertTrue(all(
            artifact["status"] == "proposed" for artifact in latest["artifacts"]
        ))

    def test_single_idempotency_key_cannot_rebind_after_terminal_config_drift(self):
        config = employees.get_config(1001)
        body = {
            "request_key": "learning-single-permanent-frozen-key-001",
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }

        async def no_network_worker(_run_id, _binding):
            return None

        with mock.patch.object(
            main, "_employee_learning_research_worker", new=no_network_worker,
        ):
            first = asyncio.run(main.employee_learning_run_create(1001, body))
        run_id = int(first["run"]["id"])
        billing.fail_operation(
            main._learning_billing_op_key(1, run_id), "TEST_TERMINAL",
        )
        employeelearning.cancel_run(run_id, reason="TEST_TERMINAL")
        employeelearning.release_budget(run_id)
        employees.set_settings_for_identity(
            config["identity_ref"],
            {"permanent_key_probe": True},
            expected_revision=config["config_revision"],
        )
        current = employees.get_config(1001)
        with self.assertRaises(HTTPException) as rebound:
            asyncio.run(main.employee_learning_run_create(1001, {
                "request_key": body["request_key"],
                "identity_ref": current["identity_ref"],
                "config_revision": current["config_revision"],
                "config_sha256": current["config_sha256"],
                "bundle_sha256": current["bundle_sha256"],
            }))
        self.assertEqual(409, rebound.exception.status_code)
        self.assertEqual(run_id, employeelearning.list_batch_runs(1)[0]["id"])

    def test_cancelled_provider_terminalizes_run_and_releases_charge_and_owner(self):
        db.insert("tenants", {"id": 2, "name": "双取消租户", "balance": 10})
        auth.set_current({
            "id": 20, "tenant_id": 2, "role": "root",
            "username": "boss", "modules": ["*"],
        })
        employee = employeeidentity.active_employee(1001)
        config = employees.get_config(1001)
        binding = main._employee_current_write_binding(1001, {
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        })
        batch = employeelearning.create_batch(
            "learning-provider-cancel-terminal-001",
            budget_cap_points=3,
            tenant_id=2,
        )
        run = employeelearning.create_run(
            batch["id"],
            "learning-provider-cancel-terminal-run-001",
            employee_idx=1001,
            identity_ref=config["identity_ref"],
            base_config_revision=config["config_revision"],
            base_config_sha256=config["config_sha256"],
            industry_key=str(employee.get("dept_key") or ""),
            budget_points=3,
            expires_at=time.time() + 3600,
        )
        employeelearning.reserve_budget(run["id"])
        employeelearning.start_run(run["id"])
        op_key = billing.start_operation(
            "learn",
            tid=2,
            op_key=main._learning_billing_op_key(2, run["id"]),
        )
        employeelearning.checkpoint(run["id"], {
            "stage": "researching",
            "expected_bundle_sha256": config["bundle_sha256"],
            "billing_op_key": op_key,
        })
        real_cancel_run = employeelearning.cancel_run
        cancel_calls = 0

        def fail_cancel_once(run_id, reason="CANCELLED"):
            nonlocal cancel_calls
            cancel_calls += 1
            if cancel_calls == 1:
                raise RuntimeError("injected cancel transition fault")
            return real_cancel_run(run_id, reason=reason)

        async def scenario():
            entered = asyncio.Event()

            async def blocked_provider(*_args, **_kwargs):
                entered.set()
                await asyncio.Event().wait()

            with (
                mock.patch.object(
                    main.providers,
                    "call_verified_learning_research",
                    new=blocked_provider,
                ),
                mock.patch.object(
                    main.employeelearning,
                    "cancel_run",
                    side_effect=fail_cancel_once,
                ),
            ):
                task = asyncio.create_task(
                    main._employee_learning_research_worker(run["id"], binding)
                )
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())
        closed = employeelearning.get_run(run["id"])
        self.assertEqual(employeelearning.RUN_CANCELLED, closed["status"])
        self.assertEqual(0.0, float(closed["spent_points"] or 0))
        self.assertEqual("refunded", db.one(
            "SELECT status FROM billing_operation WHERE op_key=?", (op_key,),
        )["status"])
        self.assertEqual(10.0, billing.balance(2))
        self.assertGreaterEqual(cancel_calls, 2)
        self.assertIsNone(
            employeelearning._identity_run_owner(config["identity_ref"])
        )

    def test_single_worker_scheduler_failure_refunds_and_releases_owner(self):
        config = employees.get_config(1001)
        body = {
            "request_key": "learning-single-worker-schedule-failure-001",
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }
        real_create_task = asyncio.create_task

        def fail_only_research_worker(coro):
            name = getattr(getattr(coro, "cr_code", None), "co_name", "")
            if name == "_employee_learning_research_worker":
                raise RuntimeError("worker scheduler unavailable")
            return real_create_task(coro)

        with (
            mock.patch.object(
                main.asyncio,
                "create_task",
                side_effect=fail_only_research_worker,
            ),
            self.assertRaises(RuntimeError),
        ):
            asyncio.run(main.employee_learning_run_create(1001, body))

        run = employeelearning.list_batch_runs(1)[0]
        self.assertEqual(employeelearning.RUN_CANCELLED, run["status"])
        self.assertEqual(0.0, float(run["spent_points"] or 0))
        self.assertEqual("refunded", db.one(
            "SELECT status FROM billing_operation WHERE op_key=?",
            (main._learning_billing_op_key(1, run["id"]),),
        )["status"])
        self.assertIsNone(
            employeelearning._identity_run_owner(config["identity_ref"])
        )

    def test_concurrent_different_requests_for_same_identity_charge_once(self):
        db.insert("tenants", {"id": 2, "name": "并发进修企业", "balance": 10})
        auth.set_current({
            "id": 21, "tenant_id": 2, "role": "root",
            "username": "boss", "modules": ["*"],
        })
        config = employees.get_config(1001)

        def body(request_key):
            return {
                "request_key": request_key,
                "identity_ref": config["identity_ref"],
                "config_revision": config["config_revision"],
                "config_sha256": config["config_sha256"],
                "bundle_sha256": config["bundle_sha256"],
            }

        async def no_network_worker(_run_id, _binding):
            return None

        async def scenario():
            with mock.patch.object(
                main, "_employee_learning_research_worker", new=no_network_worker,
            ):
                results = await asyncio.gather(
                    main.employee_learning_run_create(
                        1001, body("concurrent-single-a")
                    ),
                    main.employee_learning_run_create(
                        1001, body("concurrent-single-b")
                    ),
                    return_exceptions=True,
                )
                await asyncio.sleep(0)
                return results

        results = asyncio.run(scenario())
        successes = [row for row in results if isinstance(row, dict)]
        conflicts = [row for row in results if isinstance(row, HTTPException)]
        self.assertEqual(1, len(successes))
        self.assertEqual(1, len(conflicts))
        self.assertEqual(409, conflicts[0].status_code)
        self.assertEqual(7.0, billing.balance(2))
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_operation "
            "WHERE tenant_id=2 AND action='learn'",
        )["n"])
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_log "
            "WHERE tenant_id=2 AND delta=-3",
        )["n"])
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_run "
            "WHERE identity_ref=? AND status IN (?,?,?)",
            (
                config["identity_ref"],
                employeelearning.RUN_QUEUED,
                employeelearning.RUN_RESEARCHING,
                employeelearning.RUN_AWAITING_APPROVAL,
            ),
        )["n"])

    def test_concurrent_same_request_is_one_idempotent_owner(self):
        db.insert("tenants", {"id": 2, "name": "并发幂等企业", "balance": 10})
        auth.set_current({
            "id": 22, "tenant_id": 2, "role": "root",
            "username": "boss", "modules": ["*"],
        })
        config = employees.get_config(1001)
        body = {
            "request_key": "concurrent-same-idempotency",
            "identity_ref": config["identity_ref"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "bundle_sha256": config["bundle_sha256"],
        }

        async def no_network_worker(_run_id, _binding):
            return None

        async def scenario():
            with mock.patch.object(
                main, "_employee_learning_research_worker", new=no_network_worker,
            ):
                rows = await asyncio.gather(
                    main.employee_learning_run_create(1001, dict(body)),
                    main.employee_learning_run_create(1001, dict(body)),
                )
                await asyncio.sleep(0)
                return rows

        first, second = asyncio.run(scenario())
        self.assertEqual(first["run"]["id"], second["run"]["id"])
        self.assertEqual([False, True], sorted([
            bool(first["started"]), bool(second["started"]),
        ]))
        self.assertEqual(7.0, billing.balance(2))
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM billing_operation "
            "WHERE tenant_id=2 AND action='learn'",
        )["n"])
        self.assertEqual(1, db.one(
            "SELECT COUNT(*) AS n FROM employee_learning_run "
            "WHERE identity_ref=?",
            (config["identity_ref"],),
        )["n"])


if __name__ == "__main__":
    unittest.main()
