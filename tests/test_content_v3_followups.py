"""内容流水线 v3 后续五项：平台成片、AI 味检测、发布后数据体检、图文卡改字重出图、AI 带货视频。"""
import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from app import auth, avatar, billing, db, main, retrometrics, salesvideo, xhscards, zhuque
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


def _png(color=(200, 80, 40), size=(600, 800)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class ZhuqueTests(unittest.TestCase):
    def test_local_lint_flags_template_writing(self):
        text = ("首先，我们要明白。其次，我们要行动。最后，我们要坚持。"
                "综上所述，值得注意的是，赋能闭环很重要。")
        result = zhuque.local_lint(text)
        self.assertGreater(result["score"], 25)
        self.assertTrue(any("书面腔" in issue for issue in result["issues"]))

    def test_natural_speech_scores_low(self):
        text = "我跟你讲，上个月有个老板找我。他店开了三年，第一次亏钱。为啥？外卖平台抽成涨了，他一直没算账。"
        self.assertLess(zhuque.local_lint(text)["score"], 25)

    def test_zhuque_api_segments_and_weights_ratios(self):
        calls = []

        def handler(request):
            calls.append(json.loads(request.content))
            self.assertEqual("Bearer k", request.headers["Authorization"])
            return httpx.Response(200, json={"data": {"labels_ratio": {"0": 0.8, "1": 0.1, "2": 0.1}}})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await zhuque.zhuque_classify("这是一段测试文字。" * 300, "k", client=client)

        result = asyncio.run(run())
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(len(c["text"]) <= zhuque.SEGMENT_CHARS for c in calls))
        self.assertFalse(calls[0]["is_merge"])
        self.assertAlmostEqual(0.8, result["human"], places=2)
        self.assertEqual("不太可能是AI生成", result["verdict"])

    def test_zhuque_bad_key_raises_clear_error(self):
        async def run():
            transport = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
            async with httpx.AsyncClient(transport=transport) as client:
                await zhuque.zhuque_classify("测试" * 200, "bad", client=client)

        with self.assertRaises(zhuque.ZhuqueError):
            asyncio.run(run())


class RetroMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.old = _fresh_db("retro.db")
        db.insert("tenants", {"id": 1, "name": "甲"})

    def tearDown(self):
        _restore_db(self.tmp, self.old)

    def test_diagnose_uses_v3_pass_lines(self):
        bad = retrometrics.diagnose("抖音", {"bounce_2s": "55%", "completion": 2})
        self.assertFalse(bad["passed"])
        self.assertIn("开头 3 秒", bad["advice"])
        good = retrometrics.diagnose("小红书", {"saves": 12, "ctr": 9})
        self.assertTrue(good["passed"])
        self.assertFalse(retrometrics.diagnose("公众号", {})["supported"])
        with self.assertRaises(ValueError):
            retrometrics.diagnose("视频号", {"shares": "很多"})

    def test_three_failures_in_a_row_suggest_changing_topic(self):
        from app import pubtrack
        ids = [pubtrack.add_entry(1, "视频号", f"第{i}条", published_at=1000 + i) for i in range(3)]
        result = None
        for pid in ids:
            result = retrometrics.save(1, pid, "24h", {"shares": 1, "avg_watch_s": 10})
        self.assertEqual(3, result["streak"])
        self.assertIn("换选题方向", result["advice"])
        latest = pubtrack.add_entry(1, "视频号", "第4条", published_at=2000)
        result = retrometrics.save(1, latest, "72h", {"shares": 8, "avg_watch_s": 30})
        self.assertTrue(result["passed"])
        self.assertEqual(0, result["streak"])


class SalesVideoTests(unittest.TestCase):
    def test_requests_follow_provider_contracts(self):
        uri = salesvideo.image_data_uri(salesvideo.prepare_image(_png()))
        self.assertTrue(uri.startswith("data:image/jpeg;base64,"))
        prompt = salesvideo.build_prompt({"product": "水煮牛肉", "selling_points": "现切"}, "红油一浇真香")
        self.assertIn("红油一浇真香", prompt)
        seed = salesvideo.seedance_request(prompt, uri)
        self.assertEqual(salesvideo.SEEDANCE_MODEL, seed["model"])
        self.assertTrue(seed["generate_audio"])
        self.assertEqual(15, seed["duration"])
        kling = salesvideo.kling_request(prompt, uri)
        self.assertEqual("kling-v3", kling["model_name"])
        base64.b64decode(kling["image"])
        self.assertEqual("on", kling["sound"])

    def test_real_person_rejection_switches_to_kling_once(self):
        posts = []

        async def fake_post(base, key, path, body, timeout=120):
            posts.append(path)
            if path == salesvideo.SEEDANCE_TASKS:
                raise salesvideo.RealPersonRejected("真人")
            return {"data": {"task_id": "k-1"}}

        events = []
        with mock.patch.object(salesvideo, "_post", fake_post), \
                mock.patch.object(salesvideo.providers, "yunwu_conf", lambda: ("https://gw", "k")):
            channel, task = asyncio.run(salesvideo.submit("p", "data:image/jpeg;base64,AA==",
                                                          lambda *a: events.append(a)))
        self.assertEqual(("kling", "k-1"), (channel, task))
        self.assertEqual([salesvideo.SEEDANCE_TASKS, salesvideo.KLING_TASKS], posts)
        self.assertTrue(any("可灵" in str(e) for e in events))

    def test_given_line_is_used_without_model_call(self):
        line = asyncio.run(salesvideo.write_line({"script": "红油 一浇，牛肉滑嫩"}))
        self.assertEqual("红油一浇，牛肉滑嫩", line)

    def test_sales_engine_has_its_own_price(self):
        self.assertEqual("avatar_sales", avatar._charged_action({"engine": "sales"}))
        self.assertIn("avatar_sales", billing.DEFAULT_PRICES)


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.old = _fresh_db("followups.db")
        cls.assets = tempfile.TemporaryDirectory()
        db.insert("tenants", {"id": 2, "name": "租户甲", "balance": 100})
        cls.uid = db.insert("users", {"tenant_id": 2, "username": "v3-owner",
                                      "password_hash": "x", "role": "owner"})
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        _restore_db(cls.tmp, cls.old)
        cls.assets.cleanup()

    def setUp(self):
        self.client.cookies.set("cc_sess", auth.make_session(self.uid))

    def _job_with_publish(self, versions):
        job_id = db.insert("job", {"tenant_id": 2, "brief_json": "{}", "status": "done"})
        db.insert("station_run", {"job_id": job_id, "station_idx": 3, "status": "done",
                                  "output_json": json.dumps({"body": "正文" * 30,
                                                             "title_candidates": ["标题"]})})
        db.insert("station_run", {"job_id": job_id, "station_idx": 8, "status": "done",
                                  "output_json": json.dumps({"versions": versions},
                                                            ensure_ascii=False)})
        return job_id

    def test_ai_smell_without_key_returns_local_lint(self):
        with mock.patch.object(main.secureconfig, "get_secret", return_value=""):
            r = self.client.post("/api/tools/ai-smell", json={"text": "首先其次最后，综上所述，赋能闭环。" * 3})
        self.assertEqual(200, r.status_code, r.text)
        self.assertFalse(r.json()["configured"])
        self.assertGreater(r.json()["local"]["score"], 0)

    def test_publog_metrics_roundtrip(self):
        from app import pubtrack
        pid = pubtrack.add_entry(2, "小红书", "加盟避坑")
        fields = self.client.get(f"/api/publog/{pid}/metrics").json()
        self.assertEqual(["saves", "ctr"], [f["key"] for f in fields["fields"]])
        r = self.client.post(f"/api/publog/{pid}/metrics",
                             json={"stage": "24h", "metrics": {"saves": 3, "ctr": 4}})
        self.assertEqual(200, r.status_code, r.text)
        self.assertFalse(r.json()["passed"])
        self.assertIn("封面", r.json()["advice"])
        self.assertIn("24h", self.client.get(f"/api/publog/{pid}/metrics").json()["saved"])

    def test_platform_video_uses_platform_script(self):
        job_id = self._job_with_publish([
            {"platform": "抖音", "title": "t", "body": "描述", "script": "抖音口播" * 20},
            {"platform": "视频号", "title": "t2", "body": "描述2", "script": "视频号口播" * 30},
        ])
        captured = {}

        def fake_create(params, **kwargs):
            captured.update(params=params, **kwargs)
            return 99

        async def fake_run(create, **_kwargs):
            return create()

        with mock.patch.object(main, "_create_charged_tv_job", fake_create), \
                mock.patch.object(main, "_run_db_then_start_worker_safely", fake_run):
            r = self.client.post(f"/api/jobs/{job_id}/text-video", json={"platform": "视频号"})
            self.assertEqual(200, r.status_code, r.text)
            self.assertEqual("视频号", captured["params"]["platform"])
            self.assertTrue(captured["params"]["script"].startswith("视频号口播"))
            self.assertTrue(captured["note"].startswith("视频号"))
            bad = self.client.post(f"/api/jobs/{job_id}/text-video", json={"platform": "B站"})
            self.assertEqual(400, bad.status_code)

    def test_xhs_cards_rerender_updates_publish_output(self):
        try:
            xhscards._font_path()
        except xhscards.CardRenderError:
            self.skipTest("本机没有中文字体")
        job_id = self._job_with_publish([{"platform": "小红书", "title": "加盟签约前必问的3件事",
                                          "body": "结论", "tags": [], "cards": []}])
        cards = [{"type": "cover", "title": ["改过的", "封面大字"]},
                 {"type": "step", "no": "1", "title": "第一步", "body": "说明"},
                 {"type": "summary", "items": ["要点"], "ending": "收藏起来"}]
        with mock.patch.object(registry, "ASSET_DIR", self.assets.name):
            r = self.client.post(f"/api/jobs/{job_id}/xhs-cards",
                                 json={"cards": cards, "theme": "蓝白"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(3, len(r.json()["card_files"]))
        out = json.loads(db.one("SELECT output_json FROM station_run WHERE job_id=? AND station_idx=8",
                                (job_id,))["output_json"])
        xhs = out["versions"][0]
        self.assertEqual("蓝白", xhs["card_theme"])
        self.assertEqual(["改过的", "封面大字"], xhs["cards"][0]["title"])
        self.assertIn("check_summary", xhs)
        bad = self.client.post(f"/api/jobs/{job_id}/xhs-cards",
                               json={"cards": cards, "cover_image": "/files/job999999/x.png"})
        self.assertEqual(400, bad.status_code)

    def test_photo_cover_renders(self):
        try:
            xhscards._font_path()
        except xhscards.CardRenderError:
            self.skipTest("本机没有中文字体")
        from PIL import Image
        images = xhscards.render_cards(
            {"cards": [{"type": "cover", "title": ["照片封面"]},
                       {"type": "fact", "title": "a", "body": "b"},
                       {"type": "summary", "items": ["x"]}]},
            cover_image=_png())
        self.assertEqual((1080, 1440), Image.open(io.BytesIO(images[0])).size)

    def test_sales_job_requires_photo_and_product(self):
        r = self.client.post("/api/avatar/sales-jobs", json={"product": "牛肉"})
        self.assertIn(r.status_code, (400, 403))
        r = self.client.post("/api/avatar/sales-jobs",
                             json={"photo_name": "x.jpg", "product": "牛肉", "script": "长" * 61})
        self.assertIn(r.status_code, (400, 403))


if __name__ == "__main__":
    unittest.main()
