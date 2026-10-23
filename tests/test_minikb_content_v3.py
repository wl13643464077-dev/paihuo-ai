"""微型知识库 + 24 维品牌画像 + 内容流水线 v3（一核三版/图文卡/红线体检）。"""
import asyncio
import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import auth, brand_package, db, main, minikb, platformcheck, xhscards
from app.skills import registry


def _fresh_db(name):
    tmp = tempfile.TemporaryDirectory()
    db._shutdown_async_pool(wait=True)
    db._close_all_connections()
    db._conn = None
    db._conn_path = None
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp.name, name)
    db.conn()
    return tmp, old


def _restore_db(tmp, old):
    db._shutdown_async_pool(wait=True)
    db._close_all_connections()
    db._conn = None
    db._conn_path = None
    db.DB_PATH = old
    tmp.cleanup()


LONG_SOP = (
    "门店开档流程。" + "员工到岗后先换工服、洗手消毒、检查仪容仪表。" * 40
    + "冷库温度必须保持在零下十八度以下，每两小时记录一次温度台账。"
    + "收档流程。" + "关闭燃气总阀、清洁灶台、倒垃圾、锁门。" * 40
)


class MiniKbRetrievalTests(unittest.TestCase):
    def test_chunks_cover_whole_document_with_overlap(self):
        chunks = minikb.chunk_text(LONG_SOP)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= minikb.CHUNK_SIZE for chunk in chunks))
        self.assertIn("冷库温度", "".join(chunks))

    def test_deep_passage_is_found_and_each_doc_contributes_at_most_two_chunks(self):
        rows = [
            {"id": 1, "title": "门店SOP", "content": LONG_SOP, "pinned": 0},
            {"id": 2, "title": "菜单", "content": "招牌水煮牛肉 68 元，麻婆豆腐 28 元。", "pinned": 0},
        ]
        result = minikb.retrieve(rows, "冷库温度怎么记录")
        self.assertTrue(result["hits"])
        self.assertEqual(1, result["hits"][0]["id"])
        self.assertIn("零下十八度", result["hits"][0]["text"])
        self.assertGreater(result["hits"][0]["chunk_index"], 0)
        self.assertLessEqual(sum(1 for hit in result["hits"] if hit["id"] == 1), 2)
        self.assertNotIn(2, [hit["id"] for hit in result["hits"]])

    def test_pinned_doc_without_keyword_hit_still_comes_along(self):
        rows = [
            {"id": 1, "title": "品牌红线", "content": "禁止使用绝对化承诺。", "pinned": 1},
            {"id": 2, "title": "门店SOP", "content": LONG_SOP, "pinned": 0},
        ]
        result = minikb.retrieve(rows, "冷库温度")
        ids = [hit["id"] for hit in result["hits"]]
        self.assertIn(1, ids)
        self.assertIn(2, ids)


class MiniKbContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.old = _fresh_db("kb.db")
        db.insert("tenants", {"id": 1, "name": "甲"})
        db.insert("tenants", {"id": 2, "name": "乙"})

    def tearDown(self):
        _restore_db(self.tmp, self.old)

    def test_context_block_injects_passage_beyond_old_800_char_cut(self):
        db.insert("knowledge", {"tenant_id": 1, "title": "门店SOP", "content": LONG_SOP,
                                "tags_json": "[]", "source": "upload"})
        db.insert("knowledge", {"tenant_id": 2, "title": "别家SOP", "content": LONG_SOP,
                                "tags_json": "[]", "source": "upload"})
        self.assertGreater(LONG_SOP.index("冷库温度"), 800)
        block, meta = registry.context_block(1, "冷库温度记录要求", with_meta=True)
        self.assertIn("零下十八度", block)
        self.assertIn("《门店SOP》", block)
        self.assertNotIn("别家SOP", block)
        self.assertEqual(1, meta["selected"])
        self.assertGreaterEqual(meta["chunks"], 1)


class KnowledgeUploadEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.old = _fresh_db("upload.db")
        db.insert("tenants", {"id": 2, "name": "租户甲", "balance": 100})
        cls.uid = db.insert("users", {"tenant_id": 2, "username": "kb-owner",
                                      "password_hash": "x", "role": "owner"})
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        _restore_db(cls.tmp, cls.old)

    def setUp(self):
        self.client.cookies.set("cc_sess", auth.make_session(self.uid))

    def test_text_upload_is_stored_in_full_and_chunked(self):
        response = self.client.post(
            "/api/knowledge/upload",
            files={"file": ("门店SOP.txt", LONG_SOP.encode("utf-8"), "text/plain")},
            data={"category": "SOP"},
        )
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("门店SOP", body["title"])
        self.assertEqual(len(LONG_SOP), body["chars"])
        self.assertGreater(body["chunks"], 3)
        row = db.one("SELECT * FROM knowledge WHERE id=?", (body["id"],))
        self.assertEqual(2, row["tenant_id"])
        self.assertEqual("upload", row["source"])
        self.assertIn("SOP", json.loads(row["tags_json"]))
        self.assertIn("冷库温度", row["content"])
        stats = self.client.get("/api/knowledge-stats").json()
        self.assertGreaterEqual(stats["uploads"], 1)
        self.assertGreater(stats["chunks"], 3)

    def test_gbk_text_is_decoded(self):
        text = "价格表：招牌水煮牛肉六十八元，麻婆豆腐二十八元，米饭两元。"
        response = self.client.post(
            "/api/knowledge/upload",
            files={"file": ("价格表.txt", text.encode("gb18030"), "text/plain")},
        )
        self.assertEqual(200, response.status_code, response.text)
        row = db.one("SELECT content FROM knowledge WHERE id=?", (response.json()["id"],))
        self.assertIn("水煮牛肉", row["content"])

    def test_unsupported_or_empty_files_are_rejected(self):
        bad = self.client.post(
            "/api/knowledge/upload",
            files={"file": ("a.exe", b"MZ" * 40, "application/octet-stream")},
        )
        self.assertEqual(400, bad.status_code)
        empty = self.client.post(
            "/api/knowledge/upload",
            files={"file": ("a.txt", b"   ", "text/plain")},
        )
        self.assertEqual(400, empty.status_code)


class BrandCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.old = _fresh_db("brand.db")

    def tearDown(self):
        _restore_db(self.tmp, self.old)

    @staticmethod
    def _source(text):
        return {"url": "https://qinghe.example.com/about", "title": "青禾餐饮官网",
                "excerpt": text, "fetched_at": 1_790_000_000.0,
                "content_sha256": hashlib.sha256(text.encode()).hexdigest()}

    def test_24_dimensions_and_missing_dimension_can_be_filled_with_evidence(self):
        self.assertEqual(29, len(brand_package.FACT_KEYS))
        for key in ("story", "price", "campaigns", "reviews", "franchise", "content"):
            self.assertIn(key, brand_package.FACT_KEYS)

        text = "青禾餐饮官网：青禾餐饮的品牌口号是‘好饭，慢慢吃’。青禾餐饮创立于2012年杭州。"

        async def research(_prompt, **_kwargs):
            return {"sources": [self._source(text)]}

        async def extract(_name, _sources, key=None):
            if key == "story":
                return {"facts": [{"key": "story", "value": "创立于2012年杭州",
                                   "source_index": 0, "quote": "青禾餐饮创立于2012年杭州"}]}
            return {"facts": [{"key": "slogan", "value": "好饭，慢慢吃", "source_index": 0,
                               "quote": "青禾餐饮的品牌口号是‘好饭，慢慢吃’"}]}

        draft = asyncio.run(brand_package.collect(1, "青禾餐饮", research=research, extract=extract))
        cov = brand_package.coverage(draft)
        self.assertEqual(29, cov["total"])
        self.assertIn("story", [item["key"] for item in cov["missing"]])

        filled = asyncio.run(brand_package.recrawl_fact(
            1, draft["id"], "story", "补抓缺失维度「品牌故事与创立背景」",
            research=research, extract=extract,
        ))
        self.assertEqual("创立于2012年杭州", filled["fields"]["story"])
        self.assertNotIn("story", [m["key"] for m in brand_package.coverage(filled)["missing"]])

        brand_package.confirm(1, draft["id"])
        block = registry.company_block(1)
        self.assertIn("品牌故事与创立背景: 创立于2012年杭州", block)
        self.assertIn("品牌口号: 好饭，慢慢吃", block)


class PlatformCheckTests(unittest.TestCase):
    def test_red_lines_are_flagged(self):
        issues = platformcheck.check_version({
            "platform": "抖音",
            "title": "加盟前必问的三件事",
            "body": "据界面新闻报道，加盟店倒闭潮来了。加微信领资料 https://x.cn",
            "tags": ["加盟", "创业"],
            "script": "大家好，今天聊聊加盟。",
        })
        reds = [item["msg"] for item in issues if item["level"] == "red"]
        self.assertTrue(any("网址" in msg for msg in reds))
        self.assertTrue(any("联系方式" in msg for msg in reds))
        self.assertTrue(any("描述第一句" in msg for msg in reds))
        self.assertTrue(any("口播第一句" in msg for msg in reds))

    def test_xhs_other_platform_names_are_red_and_clean_version_passes(self):
        bad = platformcheck.check_version({
            "platform": "小红书", "title": "闲鱼卖货避坑", "body": "去闲鱼看看", "tags": [],
        })
        self.assertTrue(any("其他平台" in item["msg"] for item in bad if item["level"] == "red"))
        good = platformcheck.check_versions([{
            "platform": "小红书",
            "title": "加盟签约前必问的3件事",
            "body": ("结论先说：签加盟合同前，把商标、物料、退出三件事问清楚。\n" * 12)
                    + "收藏起来，签约前对一遍。\n来源：界面新闻10月报道",
            "tags": ["加盟", "创业", "开店", "加盟避坑", "加盟合同", "商标注册", "餐饮创业", "老板必看"],
            "cards": [{"type": "cover", "title": ["加盟前先问3件事"]}] + [
                {"type": "step", "no": str(i), "title": f"第{i}问", "body": "说明"} for i in range(1, 6)
            ],
        }])[0]
        self.assertEqual(0, good["check_summary"]["red"], good["checks"])


class XhsCardsTests(unittest.TestCase):
    SPEC = {"theme": "不存在的主题", "cards": [
        {"type": "step", "no": "1", "title": "商标归谁", "body": "查注册人 https://evil.example"},
        {"type": "cover", "title": ["加盟签约前", "先问这3件事"], "sub": "别等钱交了才后悔"},
        {"type": "pitfall", "items": ["只看样板店", "口头承诺不写进合同"]},
        {"type": "summary", "title": "收藏这3问", "items": ["商标", "物料", "退出"], "ending": "收藏起来"},
        {"type": "bogus"},
    ]}

    def test_normalize_puts_cover_first_strips_urls_and_falls_back_theme(self):
        spec = xhscards.normalize_spec(self.SPEC)
        self.assertEqual("cover", spec["cards"][0]["type"])
        self.assertEqual(xhscards.DEFAULT_THEME, spec["theme"])
        self.assertNotIn("evil.example", json.dumps(spec, ensure_ascii=False))
        self.assertEqual(4, len(spec["cards"]))

    def test_render_produces_3_by_4_pngs(self):
        try:
            xhscards._font_path()
        except xhscards.CardRenderError:
            self.skipTest("本机没有中文字体")
        from PIL import Image
        images = xhscards.render_cards(self.SPEC, signature="派活AI")
        self.assertEqual(4, len(images))
        for data in images:
            self.assertEqual((1080, 1440), Image.open(io.BytesIO(data)).size)

    def test_too_few_cards_raise(self):
        with self.assertRaises(xhscards.CardRenderError):
            xhscards.render_cards({"cards": [{"type": "cover", "title": ["只有封面"]}]})


class PublishStationPostProcessTests(unittest.TestCase):
    def test_publish_output_gets_checks_and_card_files(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        output = {"data": {"core_card": {"judgement": "签约前先问清三件事"}, "versions": [
            {"platform": "小红书", "title": "加盟签约前必问的3件事", "body": "结论：先问清。收藏起来。",
             "tags": ["加盟"], "cards": XhsCardsTests.SPEC["cards"]},
            {"platform": "抖音", "title": "加盟前必看", "body": "大家好 https://a.cn", "tags": []},
        ]}, "cost_usd": 0, "tokens": 0}

        async def fake_call(_idx, _bundle, **_kwargs):
            return json.loads(json.dumps(output))

        ctx = {"job_id": 77, "version": 1, "tenant_id": None,
               "brief": {"platforms": ["小红书", "抖音"], "direction": "加盟避坑"},
               "profile": {}, "outputs": {3: {"body": "正文", "title_candidates": ["标题"]}}}
        try:
            xhscards._font_path()
            has_font = True
        except xhscards.CardRenderError:
            has_font = False
        with mock.patch.object(registry, "_call_bundle_json", fake_call), \
                mock.patch.object(registry, "ASSET_DIR", tmp.name), \
                mock.patch.object(registry, "build_prompt", lambda *a, **k: None):
            result = asyncio.run(registry.run_publish(ctx))
        versions = result["data"]["versions"]
        self.assertTrue(all("check_summary" in v for v in versions))
        douyin = next(v for v in versions if v["platform"] == "抖音")
        self.assertGreater(douyin["check_summary"]["red"], 0)
        xhs = next(v for v in versions if v["platform"] == "小红书")
        if has_font:
            self.assertEqual(4, len(xhs["card_files"]))
            self.assertTrue(all(f.startswith("/files/job77/xhs_v1_0_") for f in xhs["card_files"]))
            self.assertTrue(os.path.isfile(os.path.join(tmp.name, "job77", "xhs_v1_0_01.png")))


if __name__ == "__main__":
    unittest.main()
