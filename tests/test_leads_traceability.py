"""线索雷达必须把可点击、可追溯的真实原帖地址贯通到结果。"""
import asyncio
from collections import Counter
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import growth, llm, providers


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
        self.assertEqual(
            growth.lead_source_url(
                "https://www.zhihu.com/question/456#comments"
            ),
            "https://www.zhihu.com/question/456",
        )
        for detail in (
            "https://www.douban.com/group/topic/456",
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://weibo.com/123456/N8abcD3Ef",
            "https://x.com/alice/status/123",
            "https://tieba.baidu.com/p/123",
            "https://mp.weixin.qq.com/s/abc",
        ):
            with self.subTest(detail=detail):
                self.assertEqual(growth.lead_source_url(detail), detail)
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
            "https://www.zhihu.com/people/someone/answers",
            "https://www.xiaohongshu.com/user/profile/abc",
            "https://www.xiaohongshu.com/user/profile/abc/notes",
            "https://space.bilibili.com/123/video",
            "https://x.com/alice/with_replies",
            "https://twitter.com/alice/media",
            "https://www.bing.com/search?q=装修",
            "https://www.bing.com/ck/a?u=https%3A%2F%2Fexample.com%2Fpost%2F1",
            "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fpost%2F1",
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpost%2F1",
            "https://www.baidu.com/link?url=opaque",
            "https://www.bing.com/%73earch?q=ai",
            "https://www.google.com/%75rl?q=https%3A%2F%2Fexample.com%2Fpost%2F1",
            "https://duckduckgo.com/%6c/?uddg=https%3A%2F%2Fexample.com%2Fpost%2F1",
            "https://www.baidu.com/%6cink?url=opaque",
            "https://www.zhihu.com/%75ser/%70rofile/abc",
            "https://www.zhihu.com/foo/../search?q=ai",
            "https://www.zhihu.com/foo/%2e%2e/search?q=ai",
            "https://www.zhihu.com/foo%2f..%2fsearch?q=ai",
            "https://www.bilibili.com/v/popular/all",
            "https://www.douban.com/group/explore",
            "https://weibo.com/hot/search",
            "https://www.zhihu.com/topic/19550517/hot",
            "https://www.xiaohongshu.com/explore",
            "https://www.dianping.com/shop/123/review_all",
            "https://weibo.com/friendships/friends",
            "https://weibo.com/123456/follow",
            "https://www.zhihu.com/question/waiting",
            "https://www.douban.com/note/list",
            "https://www.bilibili.com/read/mobile",
            "https://www.bilibili.com/read/mobile?id=",
            "https://tieba.baidu.com/p/index",
            "https://mp.weixin.qq.com/s?mid=",
            "https://weibo.com/123456/about",
            "https://weibo.com/123456/album",
            "https://weibo.com/123456/groups",
            "https://weibo.com/123456/collections",
        ):
            with self.subTest(url=unsafe):
                self.assertEqual(growth.lead_source_url(unsafe), "")

    def test_bing_rss_parser_keeps_direct_posts_not_search_or_home_pages(self):
        xml = """<?xml version="1.0" encoding="utf-8"?>
        <rss><channel>
          <item><title>企业 AI 怎么选</title>
            <link>https://www.zhihu.com/question/321</link>
            <description>正在&lt;b&gt;比较&lt;/b&gt;价格和实施效果</description></item>
          <item><title>搜索页</title>
            <link>https://www.bing.com/search?q=ai</link><description>非原帖</description></item>
          <item><title>平台首页</title>
            <link>https://www.zhihu.com/</link><description>非原帖</description></item>
          <item><title>恶意协议</title>
            <link>javascript:alert(1)</link><description>非原帖</description></item>
        </channel></rss>"""

        self.assertEqual(growth._parse_bing_rss(xml), [{
            "source_title": "企业 AI 怎么选",
            "source_url": "https://www.zhihu.com/question/321",
            "signal": "正在比较价格和实施效果",
        }])

    def test_search_metadata_is_removed_without_breaking_a_real_product_name(self):
        self.assertEqual(
            growth._lead_search_term(
                "AI 赋能（系统验收/勿跟进）", fallback="企业服务", limit=160
            ),
            "AI 赋能",
        )
        self.assertEqual(
            growth._lead_search_term(
                "系统验收咨询服务", fallback="企业服务", limit=160
            ),
            "系统验收咨询服务",
        )

    def test_gateway_parser_accepts_common_structured_result_names_only(self):
        rows = growth._gateway_lead_candidates({"results": [
            {
                "title": "真实问题",
                "link": "https://www.zhihu.com/question/789",
                "description": "正在比较方案",
            },
            {
                "title": "搜索页",
                "link": "https://www.google.com/search?q=ai",
            },
            {
                "link": "https://www.douban.com/group/topic/999",
                "description": "没有标题不能证明与原帖对应",
            },
            "https://attacker.example/not-a-structured-item",
        ]})
        self.assertEqual(rows, [{
            "title": "真实问题",
            "link": "https://www.zhihu.com/question/789",
            "description": "正在比较方案",
            "source_url": "https://www.zhihu.com/question/789",
            "source_title": "真实问题",
            "signal": "正在比较方案",
        }])

    def test_gateway_merge_round_robins_tool_sources_and_model_candidates(self):
        rows = growth._merge_gateway_lead_candidates(
            [
                {
                    "source_title": "工具真实来源 A",
                    "source_url": "https://www.douban.com/group/topic/101",
                },
                {
                    "source_title": "工具真实来源 B",
                    "source_url": "https://www.bilibili.com/video/BV1xx411c7mD",
                },
            ],
            {"sources": [
                {
                    "platform": "知乎",
                    "source_title": "模型结构化候选",
                    "source_url": "https://www.zhihu.com/question/202",
                },
                {
                    "platform": "豆瓣",
                    "source_title": "与工具来源重复",
                    "source_url": "https://www.douban.com/group/topic/101#comments",
                },
            ]},
        )

        self.assertEqual(
            [item["source_url"] for item in rows],
            [
                "https://www.douban.com/group/topic/101",
                "https://www.zhihu.com/question/202",
                "https://www.bilibili.com/video/BV1xx411c7mD",
            ],
        )
        self.assertEqual(rows[0]["platform"], "豆瓣")

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
    async def test_direct_search_round_robins_before_verifying_the_first_ten(self):
        expected_queries = [
            "site:zhihu.com/question 成都 火锅 推荐 哪家好",
            "site:zhihu.com/question 火锅 避雷 踩坑 翻车",
            "site:zhihu.com/question 火锅 怎么选 攻略 选型",
            "site:zhihu.com/question 火锅 价格 贵不贵 值不值",
            "成都 火锅 求推荐 经验",
            "site:douban.com/group/topic 餐饮 吐槽 踩雷",
            "火锅 选型 使用经验 教程",
            "成都 火锅 收费 价格 对比",
        ]

        async def search(query):
            query_index = expected_queries.index(query)
            return [
                {
                    "source_title": f"第{query_index}个查询第{row_index}条原帖",
                    "source_url": (
                        f"https://source{query_index}.example/posts/{row_index}"
                    ),
                    "signal": "公开原帖中的真实需求信号",
                }
                for row_index in range(2)
            ]

        async def fetch(url, **_kwargs):
            if url != "https://source6.example/posts/0":
                raise ValueError("原帖不可用")
            return {
                "source_url": url,
                "source_title": "第6个查询第0条原帖",
                "text": "第6个查询第0条原帖正在对比服务和价格。" * 12,
            }

        with patch.object(growth, "_public_lead_search", side_effect=search), \
                patch.object(
                    growth.linkgrab,
                    "fetch_page_evidence",
                    new=AsyncMock(side_effect=fetch),
                ):
            candidates = await growth.direct_lead_sources("餐饮", "成都", "火锅")
            verified = await growth.verify_lead_sources(candidates)

        self.assertEqual(len(candidates), 12)
        self.assertEqual(
            [item["source_url"] for item in candidates[:10]],
            [
                *(f"https://source{i}.example/posts/0" for i in range(8)),
                "https://source0.example/posts/1",
                "https://source1.example/posts/1",
            ],
        )
        self.assertEqual(
            [item["source_url"] for item in verified],
            ["https://source6.example/posts/0"],
        )

    async def test_direct_search_keeps_global_dedup_limit_and_two_per_query(self):
        shared = "https://shared.example/posts/1"
        query_indexes = {}

        async def search(query):
            query_index = query_indexes.setdefault(query, len(query_indexes))
            return [
                {
                    "source_title": "全局重复原帖",
                    "source_url": shared,
                    "signal": "重复候选",
                },
                {
                    "source_title": f"查询{query_index}第一条",
                    "source_url": f"https://unique{query_index}.example/posts/1",
                    "signal": "第一条独立候选",
                },
                {
                    "source_title": f"查询{query_index}第二条",
                    "source_url": f"https://unique{query_index}.example/posts/2",
                    "signal": "第二条独立候选",
                },
                {
                    "source_title": f"查询{query_index}第三条",
                    "source_url": f"https://unique{query_index}.example/posts/3",
                    "signal": "不应被第三次取用",
                },
            ]

        search_mock = AsyncMock(side_effect=search)
        with patch.object(growth, "_public_lead_search", new=search_mock):
            candidates = await growth.direct_lead_sources("餐饮", "成都", "火锅")

        urls = [item["source_url"] for item in candidates]
        per_query = Counter(item["where"] for item in candidates)
        self.assertEqual(len(candidates), 12)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertTrue(all(count <= 2 for count in per_query.values()))
        for query in per_query:
            rows = [item["source_url"] for item in candidates if item["where"] == query]
            query_index = query_indexes[query]
            expected = [f"https://unique{query_index}.example/posts/1"]
            if query_index == 0:
                expected.insert(0, shared)
            elif len(rows) == 2:
                expected.append(f"https://unique{query_index}.example/posts/2")
            self.assertEqual(rows, expected)
        self.assertFalse(any(url.endswith("/posts/3") for url in urls))

    async def test_public_search_merges_bounded_independent_sources(self):
        duck = [{
            "source_title": "原帖 A",
            "source_url": "https://www.zhihu.com/question/1",
            "signal": "求推荐",
        }]
        bing = [
            dict(duck[0]),
            {
                "source_title": "原帖 B",
                "source_url": "https://www.douban.com/group/topic/2",
                "signal": "避坑",
            },
        ]
        with patch.object(growth, "_duck_search",
                          new=AsyncMock(return_value=duck)) as duck_call, \
                patch.object(growth, "_bing_rss_search",
                             new=AsyncMock(return_value=bing)) as bing_call:
            rows = await growth._public_lead_search("企业 AI 怎么选", timeout=0.2)

        duck_call.assert_awaited_once()
        bing_call.assert_awaited_once()
        self.assertEqual(
            [row["source_url"] for row in rows],
            ["https://www.zhihu.com/question/1",
             "https://www.douban.com/group/topic/2"],
        )

    async def test_public_search_has_a_wall_clock_deadline(self):
        async def hangs(*_args, **_kwargs):
            await asyncio.Future()

        with patch.object(growth, "_duck_search", side_effect=hangs), \
                patch.object(growth, "_bing_rss_search", side_effect=hangs):
            rows = await asyncio.wait_for(
                growth._public_lead_search("企业 AI 怎么选", timeout=0.02),
                timeout=0.25,
            )

        self.assertEqual(rows, [])

    async def test_public_search_enforces_the_queries_site_scope(self):
        rows = [
            {
                "source_title": "知乎原始问题",
                "source_url": "https://www.zhihu.com/question/123",
                "signal": "正在比较方案",
            },
            {
                "source_title": "搜索引擎忽略 site 后混入的外站",
                "source_url": "https://vendor.example/posts/advertorial",
                "signal": "厂商宣传",
            },
            {
                "source_title": "同域但不是 question 路径",
                "source_url": "https://zhuanlan.zhihu.com/p/456",
                "signal": "文章",
            },
        ]
        with patch.object(growth, "_duck_search",
                          new=AsyncMock(return_value=[])), patch.object(
            growth, "_bing_rss_search", new=AsyncMock(return_value=rows)
        ):
            found = await growth._public_lead_search(
                "site:zhihu.com/question 企业 AI 怎么选", timeout=0.2
            )

        self.assertEqual(
            [item["source_url"] for item in found],
            ["https://www.zhihu.com/question/123"],
        )

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
            {
                "platform": "知乎",
                "source_url": "https://www.zhihu.com/question/no-title",
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
    @staticmethod
    def _verified_source():
        return [{
            "platform": "知乎",
            "source_title": "企业怎么选 AI 服务",
            "source_url": "https://www.zhihu.com/question/456",
            "signal": "正在比较服务方",
            "where": "企业 AI 服务 求推荐",
            "category_hint": "求推荐",
        }]

    @staticmethod
    def _provider_timeout():
        error = providers.ProviderError("云雾模型服务响应超时，请稍后重试")
        error.__cause__ = asyncio.TimeoutError()
        return error

    @staticmethod
    def _analysis_data(*, source_id="S1", include_copy=True, forged_url=None):
        lead = {
            "source_id": source_id,
            "category": "求推荐",
            "profile": "企业决策者",
            "intent": "高",
        }
        if include_copy:
            lead["script_comment"] = "先公开回答对方的具体问题"
        if forged_url:
            lead["source_url"] = forged_url
        return {"leads": [lead], "strategy": "逐条人工跟进"}

    def _lead_patches(self, final_call):
        return (
            patch.object(
                growth, "direct_lead_sources",
                new=AsyncMock(return_value=self._verified_source()),
            ),
            patch.object(
                growth, "verify_lead_sources",
                new=AsyncMock(side_effect=lambda rows: rows),
            ),
            patch.object(growth.providers, "call_text_json", new=final_call),
        )

    async def test_primary_success_does_not_resolve_or_call_fallback(self):
        primary = AsyncMock(return_value={
            "data": self._analysis_data(), "cost_usd": 0.1, "tokens": 7,
        })
        direct_patch, verify_patch, primary_patch = self._lead_patches(primary)
        with direct_patch, verify_patch, primary_patch, patch.object(
            growth.providers,
            "text_model_for",
            return_value="deepseek-v4-flash",
        ) as model_lookup:
            result = await growth.leads_radar(1, "企业服务", "成都", "AI赋能")

        primary.assert_awaited_once()
        model_lookup.assert_called_once_with(1)
        self.assertEqual(
            primary.await_args.kwargs["resolved_model"], "deepseek-v4-flash"
        )
        self.assertEqual(result["analysis_status"], "complete")

    async def test_exact_provider_timeout_uses_one_different_listed_api_model(self):
        gateway = AsyncMock(side_effect=[
            self._provider_timeout(), {
            "data": self._analysis_data(),
            "cost_usd": 0.2,
            "tokens": 11,
        }])
        direct_patch, verify_patch, primary_patch = self._lead_patches(gateway)
        with direct_patch, verify_patch, primary_patch, patch.object(
            growth.providers, "text_model_for", return_value="gpt-5.5"
        ) as model_lookup:
            result = await growth.leads_radar(1, "企业服务", "成都", "AI赋能")

        self.assertEqual(gateway.await_count, 2)
        model_lookup.assert_called_once_with(1)
        self.assertEqual(
            gateway.await_args_list[0].kwargs["resolved_model"], "gpt-5.5"
        )
        fallback_call = gateway.await_args_list[1]
        self.assertEqual(
            fallback_call.kwargs["model_override"], "claude-opus-4-8"
        )
        self.assertGreaterEqual(fallback_call.kwargs["timeout"], 45)
        self.assertLessEqual(fallback_call.kwargs["timeout"], 60)
        self.assertEqual(result["analysis_status"], "complete")

    async def test_primary_outer_deadline_bounds_rewrite_and_still_falls_back(self):
        cancelled = asyncio.Event()
        calls = 0

        async def gateway(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                try:
                    # 模拟 call_text 第一版命中泄露门后，第二次保密重写卡住。
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return {
                "data": self._analysis_data(),
                "cost_usd": 0.2,
                "tokens": 8,
            }

        gateway_mock = AsyncMock(side_effect=gateway)
        direct_patch, verify_patch, primary_patch = self._lead_patches(gateway_mock)
        started = time.monotonic()
        with direct_patch, verify_patch, primary_patch, patch.object(
            growth.providers, "text_model_for", return_value="deepseek-v4-flash"
        ), patch.object(
            growth, "_LEADS_ANALYSIS_PRIMARY_TIMEOUT", 0.02
        ), patch.object(
            growth, "_LEADS_ANALYSIS_TOTAL_TIMEOUT", 60
        ):
            result = await growth.leads_radar(1, "企业服务", "成都", "AI赋能")

        self.assertTrue(cancelled.is_set())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(gateway_mock.await_count, 2)
        self.assertEqual(
            gateway_mock.await_args_list[1].kwargs["model_override"], "gpt-5.5"
        )
        self.assertEqual(result["analysis_status"], "complete")

    async def test_primary_snapshot_prevents_config_race_between_main_and_fallback(self):
        gateway = AsyncMock(side_effect=[
            self._provider_timeout(), {
                "data": self._analysis_data(), "cost_usd": 0.1, "tokens": 4,
            },
        ])
        route = unittest.mock.Mock(side_effect=[
            "gpt-5.5",
            # 如果超时后重读，会导致错误地再选 gpt-5.5。
            "deepseek-v4-flash",
        ])
        direct_patch, verify_patch, primary_patch = self._lead_patches(gateway)
        with direct_patch, verify_patch, primary_patch, patch.object(
            growth.providers, "text_model_for", new=route
        ):
            result = await growth.leads_radar(1, "企业服务", "成都", "AI赋能")

        route.assert_called_once_with(1)
        self.assertEqual(gateway.await_args_list[0].kwargs["resolved_model"], "gpt-5.5")
        self.assertEqual(
            gateway.await_args_list[1].kwargs["model_override"],
            "claude-opus-4-8",
        )
        self.assertEqual(result["analysis_status"], "complete")

    async def test_local_snapshot_stays_local_when_config_changes_then_falls_back_to_api(self):
        gateway = AsyncMock(side_effect=[
            self._provider_timeout(), {
                "data": self._analysis_data(), "cost_usd": 0.1, "tokens": 4,
            },
        ])
        route = unittest.mock.Mock(side_effect=[
            providers.CLAUDE_LOCAL,
            "gpt-5.5",
        ])
        direct_patch, verify_patch, primary_patch = self._lead_patches(gateway)
        with direct_patch, verify_patch, primary_patch, patch.object(
            growth.providers, "text_model_for", new=route
        ):
            result = await growth.leads_radar(1, "企业服务", "成都", "AI赋能")

        route.assert_called_once_with(1)
        primary_call, fallback_call = gateway.await_args_list
        self.assertEqual(
            primary_call.kwargs["resolved_model"], providers.CLAUDE_LOCAL
        )
        self.assertIsNone(primary_call.kwargs.get("model_override"))
        self.assertEqual(fallback_call.kwargs["model_override"], "gpt-5.5")
        self.assertIsNone(fallback_call.kwargs.get("resolved_model"))
        self.assertEqual(result["analysis_status"], "complete")

    async def test_non_timeout_leak_and_cancellation_never_call_fallback(self):
        cases = (
            providers.ProviderError("云雾模型服务连接失败，请稍后重试"),
            providers.ProviderError("云雾模型服务响应超时，请稍后重试"),
            providers.PrivatePromptLeak("模型输出包含数字员工内部资料"),
        )
        for error in cases:
            with self.subTest(error=type(error).__name__):
                primary = AsyncMock(side_effect=error)
                direct_patch, verify_patch, primary_patch = self._lead_patches(primary)
                with direct_patch, verify_patch, primary_patch:
                    result = await growth.leads_radar(
                        1, "企业服务", "成都", "AI赋能"
                    )
                primary.assert_awaited_once()
                self.assertEqual(result["analysis_status"], "degraded")

        primary = AsyncMock(side_effect=asyncio.CancelledError())
        direct_patch, verify_patch, primary_patch = self._lead_patches(primary)
        with direct_patch, verify_patch, primary_patch:
            with self.assertRaises(asyncio.CancelledError):
                await growth.leads_radar(1, "企业服务", "成都", "AI赋能")
        primary.assert_awaited_once()

    async def test_timeout_fallback_invalid_json_or_leak_delivers_degraded_sources(self):
        fallback_failures = (
            llm.LLMError("模型输出中未找到合法 JSON"),
            providers.PrivatePromptLeak("备用输出泄露"),
        )
        for failure in fallback_failures:
            with self.subTest(failure=type(failure).__name__):
                gateway = AsyncMock(side_effect=[
                    self._provider_timeout(), failure,
                ])
                direct_patch, verify_patch, primary_patch = self._lead_patches(gateway)
                with direct_patch, verify_patch, primary_patch, patch.object(
                    growth.providers, "text_model_for",
                    return_value="deepseek-v4-flash",
                ):
                    result = await growth.leads_radar(
                        1, "企业服务", "成都", "AI赋能"
                    )

                self.assertEqual(gateway.await_count, 2)
                self.assertEqual(
                    gateway.await_args_list[1].kwargs["model_override"], "gpt-5.5"
                )
                self.assertEqual(result["analysis_status"], "degraded")
                self.assertEqual(
                    result["leads"][0]["source_url"],
                    "https://www.zhihu.com/question/456",
                )
                self.assertEqual(result["leads"][0]["intent"], "低")
                self.assertEqual(result["leads"][0]["profile"], "待人工核验原帖需求")

    async def test_fallback_user_has_no_source_url_and_cannot_rewrite_frozen_url(self):
        gateway = AsyncMock(side_effect=[
            self._provider_timeout(), {
            "data": self._analysis_data(
                forged_url="https://attacker.example/fake"
            ),
            "cost_usd": 0.2,
            "tokens": 9,
        }])
        direct_patch, verify_patch, primary_patch = self._lead_patches(gateway)
        with direct_patch, verify_patch, primary_patch, patch.object(
            growth.providers, "text_model_for", return_value="deepseek-v4-flash"
        ):
            result = await growth.leads_radar(1, "企业服务", "成都", "AI赋能")

        self.assertEqual(gateway.await_count, 2)
        fallback_prompt = gateway.await_args_list[1].args[1]
        self.assertNotIn("https://www.zhihu.com/question/456", fallback_prompt)
        self.assertNotIn("source_url", fallback_prompt)
        self.assertEqual(
            result["leads"][0]["source_url"],
            "https://www.zhihu.com/question/456",
        )

    async def test_analysis_packet_strips_embedded_urls_and_controls_without_mutating_sources(self):
        frozen = [{
            "platform": "知\x00乎 https://platform.example/path",
            "source_title": (
                "真实问题 https://www.zhihu.com/question/456 "
                "http://other.example/post\x07"
            ),
            "source_url": "https://www.zhihu.com/question/456",
            "signal": "正在比较 https://vendor.example/offer\x1f 三个方案",
            "time": "近\x00期 https://clock.example/now",
            "where": "公开搜索",
            "category_hint": "求推荐\x00 https://category.example/x",
        }]
        gateway = AsyncMock(side_effect=[
            self._provider_timeout(), {
                "data": self._analysis_data(), "cost_usd": 0.1, "tokens": 4,
            },
        ])
        with patch.object(
            growth, "direct_lead_sources", new=AsyncMock(return_value=frozen)
        ), patch.object(
            growth, "verify_lead_sources", new=AsyncMock(side_effect=lambda rows: rows)
        ), patch.object(
            growth.providers, "call_text_json", new=gateway
        ), patch.object(
            growth.providers, "text_model_for", return_value="deepseek-v4-flash"
        ):
            result = await growth.leads_radar(1, "企业服务", "成都", "AI赋能")

        for call in gateway.await_args_list:
            prompt = call.args[1]
            self.assertNotIn("http://", prompt)
            self.assertNotIn("https://", prompt)
            for injected_control in ("\x00", "\x07", "\x1f"):
                self.assertNotIn(injected_control, prompt)
        self.assertIn(
            "https://www.zhihu.com/question/456",
            result["leads"][0]["source_title"],
        )
        self.assertIn(
            "https://vendor.example/offer", result["leads"][0]["signal"]
        )
        self.assertEqual(
            result["leads"][0]["source_url"],
            "https://www.zhihu.com/question/456",
        )

    async def test_invalid_or_unusable_primary_enrichment_is_degraded_not_failed(self):
        cases = (
            AsyncMock(side_effect=llm.LLMError("模型输出中未找到合法 JSON")),
            AsyncMock(return_value={
                "data": self._analysis_data(source_id="UNKNOWN"),
                "cost_usd": 0,
                "tokens": 1,
            }),
            AsyncMock(return_value={
                "data": self._analysis_data(include_copy=False),
                "cost_usd": 0,
                "tokens": 1,
            }),
        )
        for primary in cases:
            with self.subTest(primary=primary):
                direct_patch, verify_patch, primary_patch = self._lead_patches(primary)
                with direct_patch, verify_patch, primary_patch:
                    result = await growth.leads_radar(
                        1, "企业服务", "成都", "AI赋能"
                    )
                self.assertEqual(result["analysis_status"], "degraded")
                self.assertEqual(len(result["leads"]), 1)
                self.assertEqual(result["leads"][0]["category"], "求推荐")

    async def test_programming_errors_still_fail_instead_of_becoming_degraded(self):
        primary = AsyncMock(side_effect=TypeError("internal bug"))
        direct_patch, verify_patch, primary_patch = self._lead_patches(primary)
        with direct_patch, verify_patch, primary_patch:
            with self.assertRaisesRegex(TypeError, "internal bug"):
                await growth.leads_radar(1, "企业服务", "成都", "AI赋能")

    async def test_gateway_prefers_correlated_websearch_sources_before_model_urls(self):
        research_result = {
            "data": {"sources": [{
                "platform": "知乎",
                "source_title": "模型声明的候选",
                "source_url": "https://www.zhihu.com/question/999",
            }]},
            "web_sources": [{
                "source_title": "工具实际返回的原帖",
                "source_url": "https://www.douban.com/group/topic/123",
            }],
            "cost_usd": 0.2,
            "tokens": 80,
        }
        final_result = {
            "data": {"leads": [{
                "source_id": "S1", "category": "求推荐", "intent": "高",
                "script_comment": "先回复对方的具体问题",
            }]},
            "cost_usd": 0.1,
            "tokens": 8,
        }
        verified_batches = []

        async def verify(rows):
            verified_batches.append(list(rows))
            return list(rows)

        with patch.object(
            growth, "direct_lead_sources", new=AsyncMock(return_value=[])
        ), patch.object(
            growth, "verify_lead_sources", new=AsyncMock(side_effect=verify)
        ), patch.object(
            growth.providers, "call_web_json",
            new=AsyncMock(return_value=research_result),
        ), patch.object(
            growth.providers, "call_text_json",
            new=AsyncMock(return_value=final_result),
        ) as final_call:
            result = await growth.leads_radar(1, "餐饮", "成都", "火锅")

        self.assertEqual(verified_batches[0], [])
        self.assertEqual(
            [item["source_url"] for item in verified_batches[1]],
            [
                "https://www.douban.com/group/topic/123",
                "https://www.zhihu.com/question/999",
            ],
        )
        self.assertEqual(
            result["leads"][0]["source_url"],
            "https://www.douban.com/group/topic/123",
        )
        self.assertNotIn(
            "https://www.douban.com/group/topic/123",
            final_call.await_args.args[1],
        )

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
        self.assertIn("必须实际使用 WebSearch", research_prompt)
        self.assertIn("豆瓣 group/topic", research_prompt)
        self.assertIn("哔哩哔哩 video", research_prompt)
        self.assertIn("总共最多 8 次", research_prompt)
        self.assertNotIn("WebFetch", research_prompt)
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

    async def test_search_brief_strips_smoke_markers_before_any_public_lookup(self):
        direct_sources = [{
            "platform": "知乎",
            "source_title": "企业怎么选 AI 服务",
            "source_url": "https://www.zhihu.com/question/456",
            "signal": "正在比较服务方",
        }]
        final_result = {
            "data": {"leads": [{
                "source_id": "S1", "category": "比价观望", "intent": "高",
                "script_comment": "先分享选型清单",
            }]},
            "cost_usd": 0,
            "tokens": 1,
        }
        with patch.object(
            growth, "direct_lead_sources", new=AsyncMock(return_value=direct_sources)
        ) as direct_call, patch.object(
            growth, "verify_lead_sources", new=AsyncMock(side_effect=lambda rows: rows)
        ), patch.object(
            growth.providers, "call_text_json", new=AsyncMock(return_value=final_result)
        ):
            await growth.leads_radar(
                1, "企业服务", "成都", "AI 赋能（系统验收/勿跟进）"
            )

        self.assertEqual(
            direct_call.await_args.args,
            ("企业服务", "成都", "AI 赋能"),
        )


if __name__ == "__main__":
    unittest.main()
