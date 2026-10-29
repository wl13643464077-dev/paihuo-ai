"""门店数字人一条龙：选题 72 小时门槛、红线体检、剪辑时间轴与剪辑包计费/退款。"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from app import assetfiles, auth, avatar, billing, db, storestudio


class StudioPureTests(unittest.TestCase):
    def test_topics_keep_only_sourced_items_within_72_hours(self):
        now = datetime(2026, 10, 29, 12, 0, tzinfo=storestudio.TZ)
        data = {"topics": [
            {"title": "新规：外卖后厨要公示", "heat": "央视财经报道，微博热搜", "source": "央视财经 10-28",
             "published": "2026-10-28", "score": 88, "keywords": ["外卖后厨"],
             "angle": "看 https://x.com 我们店怎么做"},
            {"title": "旧闻", "heat": "有数据", "source": "某报 10-20", "published": "2026-10-20", "score": 99},
            {"title": "没出处", "heat": "很火", "source": "", "published": "2026-10-29", "score": 90},
            {"title": "国庆客流", "heat": "美团数据", "source": "美团 10-27", "published": "2026-10-27",
             "score": 70},
        ]}
        topics = storestudio.normalize_topics(data, now)
        self.assertEqual(["新规：外卖后厨要公示", "国庆客流"], [t["title"] for t in topics])
        self.assertNotIn("http", topics[0]["angle"])
        self.assertEqual(["国庆客流"[:12]], topics[1]["keywords"])
        self.assertLessEqual(topics[0]["hours_ago"], 72)

    def test_topic_dates_accept_common_chinese_formats(self):
        now = datetime(2026, 10, 29, 17, 0, tzinfo=storestudio.TZ)
        for text in ("2026-10-28", "2026年10月28日", "10月28日", "10-27", "昨天", "5小时前",
                     "2天前", "央视财经 10-28", "2026/10/27 09:30"):
            hours = storestudio._hours_ago(text, now)
            self.assertIsNotNone(hours, text)
            self.assertLessEqual(hours, 72, text)
        self.assertGreater(storestudio._hours_ago("2026-10-25", now), 72)
        self.assertIsNone(storestudio._hours_ago("最近", now))
        kept = storestudio.normalize_topics(
            [{"title": "新规", "why": "热搜", "channel": "央视财经 10月28日"}], now)
        self.assertEqual(1, len(kept))

    def test_redlines_flag_contacts_urls_and_absolute_claims_but_not_list_ordinals(self):
        self.assertEqual([], storestudio.redline_issues("第一，晚六点前到店送酸梅汤；第二，套餐降价"))
        issues = storestudio.redline_issues("全网最低，加V领券，详见 www.a.com，电话13800138000")
        self.assertIn("含网址", issues)
        self.assertIn("含电话号码", issues)
        self.assertIn("含联系方式/导流", issues)
        self.assertIn("含广告法绝对化用语", issues)

    def test_subtitles_cover_duration_and_cards_skip_hook_and_ending(self):
        script = "国庆一过客流掉了三成。别慌。美团称晚市团购反而涨了。我们店做了三件事。你店里怎么样？"
        timeline = storestudio.subtitle_timeline(script, 20.0)
        self.assertAlmostEqual(20.0, timeline[-1][1], places=1)
        self.assertEqual(0.0, timeline[0][0])
        windows = storestudio.broll_windows(timeline, 3, 20.0)
        self.assertTrue(windows)
        for start, end in windows:
            self.assertGreaterEqual(start, 2.0)
            self.assertLessEqual(end - start, 2.8 + 1e-6)
            self.assertLessEqual(end, 20.0 - 1.5)

    def test_studio_files_resolve_to_the_job_owner(self):
        for name in ("avatar_5_studio.mp4", "avatar_5_studio-pack.zip",
                     "avatar_5_studio-cover34.jpg", "avatar_5_studio-cover916.jpg"):
            self.assertTrue(assetfiles._AVATAR_RE.match("/files/avatar/" + name), name)
        for name in ("avatar_5_studio.zip", "avatar_5_x.mp4", "avatar_5_studio-pack.zip/x"):
            self.assertFalse(assetfiles._AVATAR_RE.match("/files/avatar/" + name), name)

    def test_scene_prompt_keeps_identity_and_store(self):
        prompt = storestudio.scene_prompt("door", "餐饮", "穿厨师服 https://evil.example")
        self.assertIn("first reference photo is the shop owner", prompt)
        self.assertIn("second reference photo", prompt)
        self.assertNotIn("https://", prompt)


class StudioBillingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, avatar.PUBLIC_DIR, avatar.OUT_DIR)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "studio.db")
        avatar.PUBLIC_DIR = os.path.join(self.tmp.name, "public")
        avatar.OUT_DIR = os.path.join(self.tmp.name, "out")
        os.makedirs(avatar.PUBLIC_DIR, exist_ok=True)
        os.makedirs(avatar.OUT_DIR, exist_ok=True)
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 30})
        auth.set_current({"id": 20, "tenant_id": 2, "username": "owner", "role": "owner",
                          "modules": ["avatar"]})

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH, avatar.PUBLIC_DIR, avatar.OUT_DIR = self.old
        self.tmp.cleanup()

    def _job(self):
        from app import main
        op = billing.start_operation("studio_pack", tid=2, note="test")
        params = {"photo_name": "x.png", "script": "口播稿内容", "voice_id": "v", "duration": 30,
                  "studio": {"title": "标题", "topic": {"title": "选题", "keywords": ["词"]},
                             "billing_op": op}}
        return main._create_charged_avatar_job(params, 2), params

    def test_failed_avatar_refunds_video_and_prepaid_pack(self):
        job_id, _ = self._job()
        self.assertEqual(30 - 12 - 3, billing.balance(2))
        self.assertTrue(avatar.settle_failure(job_id, "供应商失败"))
        self.assertEqual(30, billing.balance(2))

    def test_failed_edit_refunds_only_the_pack(self):
        job_id, params = self._job()
        with mock.patch.object(storestudio, "compose", side_effect=storestudio.StudioError("x")):
            asyncio.run(storestudio.run_after_delivery(job_id, 2, params, "v.mp4", "p.png",
                                                       lambda *_: None))
        self.assertEqual(30 - 12, billing.balance(2))
        self.assertEqual("failed", storestudio.studio_state(job_id)["status"])

    def test_restart_marks_running_edit_failed_and_refunds(self):
        job_id, _ = self._job()
        storestudio._save_state(job_id, {"status": "running"})
        self.assertEqual(1, storestudio.recover_interrupted())
        self.assertEqual("failed", storestudio.studio_state(job_id)["status"])
        self.assertEqual(30 - 12, billing.balance(2))

    def test_store_photos_are_a_separate_asset_kind(self):
        name = f"{2:032x}.png"
        with open(os.path.join(avatar.PUBLIC_DIR, name), "wb") as fh:
            fh.write(b"img")
        avatar.remember_asset(name, "store")
        self.assertTrue(avatar.asset_belongs(name, {"store"}, 2))
        self.assertFalse(avatar.asset_belongs(name, {"photo"}, 2))


if __name__ == "__main__":
    unittest.main()
