"""线索雷达必须把可点击、可追溯的真实原帖地址贯通到结果。"""
import unittest
from unittest.mock import AsyncMock, patch

from app import growth


class LeadSourceUrlTests(unittest.TestCase):
    def test_public_search_result_parser_restores_original_post_url(self):
        parser = growth._DuckResultParser()
        parser.feed(
            '<div class="result"><a class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.zhihu.com%2Fquestion%2F321">'
            '企业 AI <b>怎么选</b></a>'
            '<a class="result__snippet">正在比较价格和实施效果</a></div>'
        )
        self.assertEqual(parser.results[0]["title"], "企业 AI 怎么选")
        self.assertEqual(parser.results[0]["snippet"], "正在比较价格和实施效果")
        self.assertEqual(
            growth._duck_result_url(parser.results[0]["href"]),
            "https://www.zhihu.com/question/321",
        )

    def test_accepts_public_http_links_and_rejects_unsafe_targets(self):
        self.assertEqual(
            growth.lead_source_url(" https://www.zhihu.com/question/123 "),
            "https://www.zhihu.com/question/123",
        )
        self.assertEqual(
            growth.lead_source_url(
                "知乎原帖：https://www.zhihu.com/question/456 （已核验）", embedded=True
            ),
            "https://www.zhihu.com/question/456",
        )
        for unsafe in (
            "javascript:alert(1)",
            "data:text/html,x",
            "//example.com/post/1",
            "https://user:pass@example.com/post/1",
            "http://localhost/post/1",
            "http://127.0.0.1/post/1",
            "http://192.168.1.8/post/1",
            "http://2130706433/post/1",
            "http://0x7f000001/post/1",
            "https://example.com:bad/post/1",
            "https://example.com/post/1\njavascript:alert(1)",
            "https://example.com/post/1 已核验",
            "https://www.zhihu.com/",
            "https://www.zhihu.com/search?q=装修",
            "https://www.zhihu.com/people/someone",
            "https://www.xiaohongshu.com/user/profile/abc",
        ):
            with self.subTest(url=unsafe):
                self.assertEqual(growth.lead_source_url(unsafe), "")

    def test_normalizer_keeps_only_traceable_leads_and_recomputes_counts(self):
        result = growth.normalize_leads_result({
            "leads": [
                {"category": "求推荐", "source_url": "https://www.zhihu.com/question/1"},
                {"category": "吐槽同行", "where": "https://www.douban.com/group/topic/2"},
                {"category": "吐槽同行", "source_url": "https://www.douban.com/group/topic/2"},
                {"category": "攻略需求", "where": "小红书搜索：装修攻略"},
                {"category": "比价观望", "source_url": "javascript:alert(1)"},
                {"category": "比价观望", "source_url": "javascript:alert(1)",
                 "where": "来源：https://example.com/posts/price-3 已核验"},
                {"category": "攻略需求", "source_url": "data:text/html,x",
                 "url": "https://example.com/posts/guide-4"},
            ],
            "by_category": {"求推荐": 99},
        })
        self.assertEqual(len(result["leads"]), 4)
        self.assertEqual(
            [lead["source_url"] for lead in result["leads"]],
            ["https://www.zhihu.com/question/1", "https://www.douban.com/group/topic/2",
             "https://example.com/posts/price-3", "https://example.com/posts/guide-4"],
        )
        self.assertEqual(result["by_category"], {
            "求推荐": 1, "吐槽同行": 1, "攻略需求": 1, "比价观望": 1,
        })
        self.assertEqual(len(result["followup"]), 4)
        self.assertTrue(all("https://" in item for item in result["followup"]))

    def test_known_platform_must_match_the_original_post_domain(self):
        self.assertTrue(growth.lead_platform_matches(
            "https://www.zhihu.com/question/123", "知乎"))
        self.assertFalse(growth.lead_platform_matches(
            "https://phishing.example/posts/123", "知乎"))
        result = growth.normalize_leads_result({"leads": [
            {"platform": "知乎", "source_url": "https://phishing.example/posts/123"},
            {"platform": "知乎", "source_url": "https://www.zhihu.com/question/456"},
        ]})
        self.assertEqual(
            [item["source_url"] for item in result["leads"]],
            ["https://www.zhihu.com/question/456"],
        )

    def test_normalizer_rejects_an_entire_untraceable_delivery(self):
        with self.assertRaisesRegex(Exception, "没有找到可回到原帖"):
            growth.normalize_leads_result({
                "leads": [{"category": "求推荐", "where": "知乎搜索某个关键词"}],
            })

    def test_gateway_sources_get_stable_ids_and_downstream_cannot_change_urls(self):
        sources = growth.normalize_lead_sources({"sources": [
            {
                "platform": "知乎",
                "source_title": "成都装修怎么选",
                "source_url": "https://www.zhihu.com/question/123",
                "signal": "正在对比三家供应商",
                "discovery_query": "成都 装修 求推荐",
            },
            {
                "platform": "豆瓣",
                "source_title": "装修踩坑记录",
                "source_url": "https://www.douban.com/group/topic/456",
                "signal": "想找靠谱施工方",
            },
        ]})
        self.assertEqual([item["source_id"] for item in sources], ["S1", "S2"])

        merged = growth.merge_lead_enrichment(sources, {"leads": [
            {
                "source_id": "S1",
                "category": "求推荐",
                "intent": "高",
                "source_url": "https://phishing.example/fake",
                "source_title": "被改写的标题",
                "script_comment": "先公开回答问题",
            },
            {
                "source_id": "S2",
                "category": "吐槽同行",
                "intent": "中",
                "script_dm": "先确认对方是否愿意交流",
            },
        ]})
        self.assertEqual(
            [item["source_url"] for item in merged["leads"]],
            ["https://www.zhihu.com/question/123",
             "https://www.douban.com/group/topic/456"],
        )
        self.assertEqual(merged["leads"][0]["source_title"], "成都装修怎么选")
        self.assertEqual(merged["leads"][0]["script_comment"], "先公开回答问题")

    def test_enrichment_without_exact_id_and_actual_copy_is_not_delivered(self):
        sources = growth.normalize_lead_sources({"sources": [{
            "platform": "知乎",
            "source_title": "真实问题",
            "source_url": "https://www.zhihu.com/question/789",
        }]})
        self.assertEqual(growth.merge_lead_enrichment(
            sources, {"leads": [{"source_id": "UNKNOWN", "script_comment": "错配"}]}
        )["leads"], [])
        self.assertEqual(growth.merge_lead_enrichment(
            sources, {"leads": [{"source_id": "S1", "profile": "只有画像"}]}
        )["leads"], [])


class LeadSourceVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_accessible_matching_final_posts_survive(self):
        candidates = [
            {
                "platform": "知乎",
                "source_title": "成都装修怎么选",
                "source_url": "https://www.zhihu.com/question/123",
                "signal": "业主正在比较三家装修公司",
            },
            {
                "platform": "知乎",
                "source_title": "已删除的帖子",
                "source_url": "https://www.zhihu.com/question/404",
            },
            {
                "platform": "知乎",
                "source_title": "跳到平台首页",
                "source_url": "https://www.zhihu.com/question/home",
            },
            {
                "platform": "知乎",
                "source_title": "成都装修避坑经历",
                "source_url": "https://www.zhihu.com/question/mismatch",
            },
            {
                "platform": "知乎",
                "source_title": "成都装修怎么选",
                "source_url": "https://www.zhihu.com/question/generic-overlap",
                "signal": "搜索卡片声称业主正准备购买",
            },
            {
                "platform": "知乎",
                "source_title": "伪装成知乎的外站",
                "source_url": "https://www.zhihu.com/question/offsite",
            },
        ]

        async def fetch(url, **_kwargs):
            if url.endswith("/404"):
                raise ValueError("原帖返回 404")
            if url.endswith("/home"):
                return {
                    "source_url": "https://www.zhihu.com/",
                    "source_title": "知乎首页",
                    "text": "这是平台首页内容。" * 30,
                }
            if url.endswith("/mismatch"):
                return {
                    "source_url": url,
                    "source_title": "完全无关的宠物用品讨论",
                    "text": "这是一篇关于宠物食品和猫砂选择的公开讨论。" * 20,
                }
            if url.endswith("/generic-overlap"):
                return {
                    "source_url": url,
                    "source_title": "成都装修公司招聘信息",
                    "text": (
                        "成都装修公司正在招聘设计师，岗位职责、薪资待遇和"
                        "简历投递方式如下。"
                    ) * 20,
                }
            if url.endswith("/offsite"):
                return {
                    "source_url": "https://attacker.example/posts/1",
                    "source_title": "伪装成知乎的外站",
                    "text": "这是一段足够长但终跳域名已经改变的页面内容。" * 20,
                }
            return {
                "source_url": url,
                "source_title": "成都装修怎么选",
                "text": "成都装修怎么选？业主正在比较三家装修公司的预算和施工方案。" * 12,
            }

        with patch.object(
            growth.linkgrab,
            "fetch_page_evidence",
            new=AsyncMock(side_effect=fetch),
        ):
            verified = await growth.verify_lead_sources(candidates)

        self.assertEqual(len(verified), 1)
        self.assertEqual(
            verified[0]["source_url"],
            "https://www.zhihu.com/question/123",
        )
        self.assertEqual(verified[0]["source_title"], "成都装修怎么选")
        self.assertIn("业主正在比较三家装修公司", verified[0]["signal"])
        self.assertNotIn("搜索卡片", verified[0]["signal"])

    async def test_unknown_platform_cannot_redirect_to_a_different_site(self):
        candidates = [{
            "platform": "公开网页",
            "source_title": "真实原帖标题",
            "source_url": "https://source.example/posts/1",
        }]
        with patch.object(
            growth.linkgrab,
            "fetch_page_evidence",
            new=AsyncMock(return_value={
                "source_url": "https://different.example/posts/1",
                "source_title": "真实原帖标题",
                "text": "真实原帖标题以及足够长的公开中文正文。" * 20,
            }),
        ):
            self.assertEqual([], await growth.verify_lead_sources(candidates))

    async def test_unverified_direct_and_gateway_cards_fail_before_analysis(self):
        direct = [{
            "platform": "知乎",
            "source_title": "搜索卡片但原帖已失效",
            "source_url": "https://www.zhihu.com/question/404",
        }]
        gateway_result = {
            "data": {
                "sources": [{
                    "platform": "知乎",
                    "source_title": "网关也返回失效原帖",
                    "source_url": "https://www.zhihu.com/question/410",
                }]
            },
            "cost_usd": 0.1,
            "tokens": 10,
        }
        with patch.object(
            growth, "direct_lead_sources", new=AsyncMock(return_value=direct)
        ), patch.object(
            growth.linkgrab,
            "fetch_page_evidence",
            new=AsyncMock(side_effect=ValueError("原帖不可访问")),
        ), patch.object(
            growth.providers,
            "call_web_json",
            new=AsyncMock(return_value=gateway_result),
        ) as gateway, patch.object(
            growth.providers,
            "call_text_json",
            new=AsyncMock(),
        ) as analysis:
            with self.assertRaisesRegex(Exception, "没有找到可回到原帖"):
                await growth.leads_radar(2, "企业服务", "成都", "AI赋能")

        gateway.assert_awaited_once()
        analysis.assert_not_awaited()


class LeadsRadarContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_locks_sources_then_selected_model_enriches_them(self):
        research_result = {
            "data": {
                "sources": [{
                    "platform": "知乎",
                    "source_title": "原始问题",
                    "source_url": "https://www.zhihu.com/question/123",
                    "signal": "准备购买，正在比较",
                    "discovery_query": "成都 家装 求推荐",
                }]
            },
            "cost_usd": 0.2,
            "tokens": 80,
        }
        final_result = {
            "data": {
                "leads": [{
                    "source_id": "S1",
                    "category": "求推荐",
                    "profile": "成都业主",
                    "intent": "高",
                    "source_url": "https://phishing.example/fake",
                    "script_comment": "公开回复",
                    "script_dm": "礼貌私信",
                }],
                "strategy": "逐条人工跟进",
            },
            "cost_usd": 0.1,
            "tokens": 8,
        }
        with patch.object(growth, "direct_lead_sources",
                          new=AsyncMock(return_value=[])), \
                patch.object(
                    growth,
                    "verify_lead_sources",
                    new=AsyncMock(side_effect=lambda rows: rows),
                ), \
                patch("app.skills.registry.company_block",
                      return_value="【公司机密】SECRET-COMPANY-CONTEXT"), \
                patch.object(growth.providers, "call_web_json",
                          new=AsyncMock(return_value=research_result)) as research_call, \
                patch.object(growth.providers, "call_text_json",
                             new=AsyncMock(return_value=final_result)) as final_call:
            result = await growth.leads_radar(1, "家装", "成都", "全屋定制")

        research_prompt = research_call.await_args.args[0]
        self.assertIn("source_url", research_prompt)
        self.assertIn("搜索结果页、平台首页、账号主页", research_prompt)
        self.assertNotIn("SECRET-COMPANY-CONTEXT", research_prompt)
        self.assertEqual(research_call.await_args.kwargs["token"], "leads:1:research")
        self.assertFalse(final_call.await_args.kwargs["web"])
        self.assertIn("URL 由服务器冻结保管", final_call.await_args.args[1])
        self.assertIn("SECRET-COMPANY-CONTEXT", final_call.await_args.args[1])
        self.assertNotIn("https://www.zhihu.com/question/123",
                         final_call.await_args.args[1])
        self.assertEqual(
            result["leads"][0]["source_url"],
            "https://www.zhihu.com/question/123",
        )
        self.assertEqual(result["leads"][0]["source_title"], "原始问题")
        self.assertAlmostEqual(result["cost_usd"], 0.3)
        self.assertEqual(result["tokens"], 88)

    async def test_direct_search_sources_bypass_broken_gateway(self):
        direct_sources = [{
            "platform": "知乎",
            "source_title": "企业用 AI 工具值不值",
            "source_url": "https://www.zhihu.com/question/456",
            "signal": "正在比较费用和落地效果",
            "where": "企业 AI 工具 价格",
            "category_hint": "比价观望",
        }]
        final_result = {
            "data": {"leads": [{
                "source_id": "S1", "category": "比价观望", "profile": "企业老板",
                "intent": "高", "script_comment": "先分享一份选型清单",
                "script_dm": "礼貌询问当前阶段",
            }]},
            "cost_usd": 0.1,
            "tokens": 12,
        }
        with patch.object(growth, "direct_lead_sources",
                          new=AsyncMock(return_value=direct_sources)), \
                patch.object(
                    growth,
                    "verify_lead_sources",
                    new=AsyncMock(side_effect=lambda rows: rows),
                ), \
                patch.object(growth.providers, "call_web_json",
                             new=AsyncMock()) as gateway, \
                patch.object(growth.providers, "call_text_json",
                             new=AsyncMock(return_value=final_result)):
            result = await growth.leads_radar(1, "企业服务", "长沙", "AI赋能")

        gateway.assert_not_awaited()
        self.assertEqual(result["leads"][0]["source_url"],
                         "https://www.zhihu.com/question/456")
        self.assertEqual(result["tokens"], 12)


if __name__ == "__main__":
    unittest.main()
