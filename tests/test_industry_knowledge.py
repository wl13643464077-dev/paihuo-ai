"""行业知识底座:结构约束与注入行为。

这一层存在的意义是让 420 名"行业专家"真的有行业口径,而不是通用顾问换了称呼。
因此最重要的一条纪律必须由测试守住:**参考区间必须带来源、适用范围与时点**——
手册自己就写着"禁止无来源行业基准",知识底座绝不能反过来把没出处的数字喂进去。
"""
import glob
import json
import os
import tempfile
import unittest

from app import industryknowledge

KNOWLEDGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "industry_knowledge",
)


def _files():
    return sorted(glob.glob(os.path.join(KNOWLEDGE_DIR, "*.json")))


class KnowledgeFileContractTests(unittest.TestCase):
    """落库的每份行业资料都要满足同一份契约。"""

    @classmethod
    def setUpClass(cls):
        cls.docs = []
        for path in _files():
            with open(path, encoding="utf-8") as handle:
                cls.docs.append((os.path.basename(path), json.load(handle)))

    def test_every_industry_department_has_knowledge(self):
        """有产业部就该有对应的行业底座,否则那个行业的员工仍是通用顾问。"""
        from app import departments

        dept_keys = {d.get("key") for d in departments.list_depts()}
        known = {doc.get("key") for _name, doc in self.docs}
        missing = sorted(k for k in dept_keys if k and k not in known)
        self.assertEqual([], missing, f"这些行业还没有知识底座: {missing}")

    def test_required_sections_are_present(self):
        for name, doc in self.docs:
            with self.subTest(file=name):
                self.assertTrue(doc.get("key"), "缺少 key")
                self.assertTrue(doc.get("name"), "缺少行业名")
                for section in ("metrics", "glossary", "compliance", "pitfalls"):
                    self.assertIsInstance(
                        doc.get(section), list, f"{section} 必须是数组"
                    )
                    self.assertTrue(doc.get(section), f"{section} 不能为空")

    def test_metrics_carry_formulas(self):
        """指标是确定性知识,必须给出可复算的公式,否则等于没说。"""
        for name, doc in self.docs:
            for metric in doc.get("metrics") or []:
                with self.subTest(file=name, metric=metric.get("name")):
                    self.assertTrue(metric.get("name"))
                    self.assertTrue(
                        metric.get("formula"),
                        "指标必须带计算公式",
                    )

    def test_every_benchmark_is_sourced(self):
        """核心纪律:没有来源/适用范围/时点的参考值,一条都不许进提示词。"""
        offenders = []
        for name, doc in self.docs:
            for bench in doc.get("benchmarks") or []:
                for field in ("metric", "range", "source", "scope", "as_of"):
                    if not str(bench.get(field) or "").strip():
                        offenders.append(
                            f"{name}: {bench.get('metric') or bench} 缺少 {field}"
                        )
        self.assertEqual([], offenders, "存在无出处的行业基准值")

    def test_benchmarks_do_not_claim_false_precision(self):
        """参考值应是区间或带限定的量级,不该是一个赤裸的精确数。"""
        offenders = []
        for name, doc in self.docs:
            for bench in doc.get("benchmarks") or []:
                text = str(bench.get("range") or "")
                looks_like_range = any(
                    mark in text
                    for mark in ("-", "~", "–", "—", "至", "约", "%", "以上",
                                 "以下", "左右", "不等", "区间", "起")
                )
                if not looks_like_range:
                    offenders.append(f"{name}: {bench.get('metric')} = {text}")
        self.assertEqual([], offenders, "参考值应表达为区间/量级而非精确点值")

    def test_compliance_entries_are_dated(self):
        """法规会修订:引用前必须知道资料是什么时候查的。"""
        for name, doc in self.docs:
            for item in doc.get("compliance") or []:
                with self.subTest(file=name, rule=item.get("name")):
                    self.assertTrue(item.get("name"))
                    self.assertTrue(item.get("requirement"))
                    self.assertTrue(item.get("as_of"), "合规条目必须标注时点")


class KnowledgeBlockTests(unittest.TestCase):
    """注入逻辑本身:相关性挑选、体量上限、纪律说明。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_config = industryknowledge.CONFIG_DIR
        self.old_legacy = industryknowledge.LEGACY_DIR
        industryknowledge.CONFIG_DIR = os.path.join(self.tmp.name, "absent")
        industryknowledge.LEGACY_DIR = self.tmp.name
        industryknowledge.reset_cache()

        def _restore():
            industryknowledge.CONFIG_DIR = self.old_config
            industryknowledge.LEGACY_DIR = self.old_legacy
            industryknowledge.reset_cache()

        self.addCleanup(_restore)

    def _write(self, doc):
        path = os.path.join(self.tmp.name, f"{doc['key']}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False)
        industryknowledge.reset_cache()

    def _sample(self):
        return {
            "key": "demo", "name": "样例行业",
            "metrics": [
                {"name": "坪效", "formula": "销售额 ÷ 营业面积",
                 "why": "衡量空间产出", "trap": "面积口径要统一"},
                {"name": "客单价", "formula": "销售额 ÷ 交易笔数",
                 "why": "衡量单次消费", "trap": "含退货要剔除"},
            ],
            "benchmarks": [
                {"metric": "坪效", "range": "1-2 万元/㎡/年",
                 "source": "某某报告", "scope": "一线城市", "as_of": "2025"},
            ],
            "glossary": [
                {"term": "坪效", "meaning": "每平方米产出"},
                {"term": "动线", "meaning": "顾客行走路径"},
            ],
            "practices": ["温层管理是本行业独有的"],
            "compliance": [
                {"name": "食品经营许可证", "requirement": "持证经营",
                 "as_of": "2026-07"},
            ],
            "pitfalls": ["盲目扩张导致现金流断裂"],
        }

    def test_missing_industry_injects_nothing(self):
        self.assertEqual("", industryknowledge.block_for("nope", "任意岗位"))

    def test_block_contains_discipline_and_sections(self):
        self._write(self._sample())
        block = industryknowledge.block_for("demo", "坪效 选址 单店模型")

        self.assertIn("样例行业", block)
        self.assertIn("以老板的数据为准", block)
        self.assertIn("销售额 ÷ 营业面积", block)
        self.assertIn("食品经营许可证", block)

    def test_benchmarks_always_carry_source_and_scope_in_prompt(self):
        """注入到提示词里也必须带着出处,否则模型会当成自己的结论写出去。"""
        self._write(self._sample())
        block = industryknowledge.block_for("demo", "坪效")

        self.assertIn("来源:某某报告", block)
        self.assertIn("适用:一线城市", block)
        self.assertIn("2025", block)

    def test_relevance_selection_prefers_matching_entries(self):
        doc = self._sample()
        doc["metrics"] = [
            {"name": f"无关指标{i}", "formula": f"公式{i}", "why": "", "trap": ""}
            for i in range(20)
        ] + [{"name": "库存周转天数", "formula": "平均库存 ÷ 日均销货成本",
              "why": "", "trap": ""}]
        self._write(doc)

        block = industryknowledge.block_for("demo", "库存周转与补货计划")
        self.assertIn("库存周转天数", block)

    def test_block_is_size_bounded(self):
        doc = self._sample()
        doc["metrics"] = [
            {"name": f"指标{i}", "formula": "公式" * 200, "why": "", "trap": ""}
            for i in range(50)
        ]
        self._write(doc)

        block = industryknowledge.block_for("demo", "指标")
        self.assertLess(
            len(block),
            industryknowledge.MAX_BLOCK_CHARS + 600,
            "知识块不能挤掉岗位手册与老板任务书",
        )

    def test_corrupt_file_does_not_break_other_industries(self):
        self._write(self._sample())
        with open(os.path.join(self.tmp.name, "broken.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{not json")
        industryknowledge.reset_cache()

        self.assertTrue(industryknowledge.block_for("demo", "坪效"))
        self.assertEqual("", industryknowledge.block_for("broken", "坪效"))


class SpecialistPromptInjectionTests(unittest.TestCase):
    """端到端:专家的 system 提示里应当出现本行业的口径。"""

    def test_specialist_system_prompt_carries_industry_knowledge(self):
        from app import departments

        if not _files():
            self.skipTest("尚未落地行业知识资料")

        specialists = departments.specialists()
        target = None
        for idx, employee in sorted(specialists.items()):
            if employee.get("dept_key") and industryknowledge.get(
                employee["dept_key"]
            ):
                target = employee
                break
        self.assertIsNotNone(target, "找不到带知识底座的专家")

        bundle = departments.build_task_prompt(
            target, {"direction": "帮我看看这个月的经营"},
            skills_text="", knowledge_text="", caps=[],
        )
        self.assertIn("行业知识底座", bundle.system)
        # 老板的任务书仍然只在 user 侧,知识底座不得把业务数据提升为 system。
        self.assertIn("帮我看看这个月的经营", bundle.user)
        self.assertNotIn("帮我看看这个月的经营", bundle.system)
        # 缓存契约：知识底座按任务方向召回、随任务变化，必须位于全部稳定
        # 段（身份/手册/交付规则）之后，否则跨任务字节级稳定前缀被拦腰
        # 截断，供应商自动前缀缓存永远无法命中。
        self.assertLess(
            bundle.system.index("【交付规则】"),
            bundle.system.index("行业知识底座"),
        )


if __name__ == "__main__":
    unittest.main()
