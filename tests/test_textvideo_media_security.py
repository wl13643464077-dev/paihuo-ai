import asyncio
from concurrent.futures import ThreadPoolExecutor
import inspect
import os
import resource
import sys
import tempfile
import unittest
import wave
from unittest import mock

from fastapi import HTTPException

from app import auth, db, main, textvideo


class TextVideoBgmSecurityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        self.old_asset_dir = textvideo.ASSET_DIR
        self.old_clip_root = textvideo.CLIP_ROOT
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "textvideo-security.db")
        textvideo.ASSET_DIR = os.path.join(self.tmp.name, "assets")
        textvideo.CLIP_ROOT = os.path.join(
            textvideo.ASSET_DIR,
            "tvclips",
        )
        os.makedirs(textvideo.ASSET_DIR, exist_ok=True)
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台", "balance": 0})
        db.insert("tenants", {"id": 2, "name": "企业", "balance": 30})
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner-2",
            "role": "owner",
            "modules": ["content"],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        textvideo.ASSET_DIR = self.old_asset_dir
        textvideo.CLIP_ROOT = self.old_clip_root
        self.tmp.cleanup()

    @staticmethod
    def _discard_task(coroutine):
        coroutine.close()
        return mock.Mock()

    def test_invalid_bgm_is_rejected_before_standalone_job_creation(self):
        invalid_values = (
            "",
            "../../outside",
            "warm/../up",
            "WARM",
            0,
            False,
            {"mood": "warm"},
            ["warm"],
        )
        with mock.patch.object(
            main,
            "_create_charged_tv_job",
            side_effect=AssertionError("invalid BGM reached billing"),
        ) as create:
            for invalid in invalid_values:
                with self.subTest(invalid=invalid):
                    with self.assertRaises(HTTPException) as caught:
                        asyncio.run(main.text_video_create({
                            "script": "这是一段足够长的口播文案，用于验证配乐参数必须先通过服务端枚举。",
                            "bgm": invalid,
                        }))
                    self.assertEqual(400, caught.exception.status_code)
                    self.assertEqual("配乐风格无效", caught.exception.detail)
        create.assert_not_called()
        self.assertEqual(30, db.one(
            "SELECT balance FROM tenants WHERE id=2"
        )["balance"])
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) AS n FROM tv_job")["n"],
        )

    def test_invalid_bgm_is_rejected_before_job_delivery_billing(self):
        with mock.patch.object(
            main,
            "_job_or_404",
            return_value={},
        ), mock.patch.object(
            main,
            "build_delivery",
            return_value={
                "title": "成片验收",
                "body": "已经定稿的正文",
                "versions": [],
                "images": [],
            },
        ), mock.patch.object(
            main,
            "_create_charged_tv_job",
            side_effect=AssertionError("invalid BGM reached billing"),
        ) as create:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.job_text_video(
                    99,
                    {"bgm": "../../../tmp/escape"},
                ))
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual("配乐风格无效", caught.exception.detail)
        create.assert_not_called()

    def test_invalid_bgm_wins_before_clip_selection_or_billing(self):
        with mock.patch.object(
            main,
            "_create_charged_tv_job",
            side_effect=AssertionError("invalid BGM reached billing"),
        ) as create:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.text_video_create({
                    "mode": "clips",
                    "clips": [],
                    "topic": "混剪主题",
                    "bgm": "../warm",
                }))
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual("配乐风格无效", caught.exception.detail)
        create.assert_not_called()

    def test_internal_job_creator_revalidates_bgm_before_row_or_charge(self):
        with mock.patch.object(
            main.billing,
            "charge_if_claimed",
            side_effect=AssertionError("invalid BGM reached charge"),
        ) as charge:
            with self.assertRaises(HTTPException) as caught:
                main._create_charged_tv_job(
                    {"title": "内部调用", "bgm": "../../escape"},
                    tenant_id=2,
                )
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual("配乐风格无效", caught.exception.detail)
        charge.assert_not_called()
        self.assertEqual(
            0,
            db.one("SELECT COUNT(*) AS n FROM tv_job")["n"],
        )

    def test_only_server_enumerated_bgm_values_are_persisted(self):
        captured = []

        def create(params, **_kwargs):
            captured.append(params["bgm"])
            return len(captured)

        with mock.patch.object(
            main,
            "_create_charged_tv_job",
            side_effect=create,
        ), mock.patch.object(
            main,
            "_start_text_video_worker",
        ):
            for mood in ("warm", "up", "calm", "none"):
                result = asyncio.run(main.text_video_create({
                    "script": "这是一段足够长的口播文案，用于验证四种合法配乐都可以创建任务。",
                    "bgm": mood,
                }))
                self.assertEqual(len(captured), result["tv_id"])
        self.assertEqual(["warm", "up", "calm", "none"], captured)

    def test_bgm_uses_fixed_paths_and_rejects_path_escape(self):
        outside = os.path.join(self.tmp.name, "outside.wav")
        with open(outside, "wb") as handle:
            handle.write(b"outside-sentinel")
        for invalid in ("none", "../outside", "../../outside", "warm/../up"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    textvideo._ensure_bgm(invalid)
        with open(outside, "rb") as handle:
            self.assertEqual(b"outside-sentinel", handle.read())

    def test_bgm_generation_is_locked_atomic_and_replaces_symlink(self):
        tv_dir = os.path.join(textvideo.ASSET_DIR, "tv")
        os.makedirs(tv_dir, exist_ok=True)
        outside = os.path.join(self.tmp.name, "outside.wav")
        with open(outside, "wb") as handle:
            handle.write(b"outside-sentinel")
        target = os.path.join(tv_dir, "_bgm_warm.wav")
        os.symlink(outside, target)

        with mock.patch("wave.open", wraps=wave.open) as open_wave:
            with ThreadPoolExecutor(max_workers=6) as pool:
                paths = list(pool.map(
                    textvideo._ensure_bgm,
                    ["warm"] * 6,
                ))

        self.assertEqual([os.path.realpath(target)] * 6, paths)
        self.assertFalse(os.path.islink(target))
        self.assertTrue(os.path.isfile(target))
        self.assertGreater(os.path.getsize(target), 44)
        self.assertEqual(1, open_wave.call_count)
        with open(outside, "rb") as handle:
            self.assertEqual(b"outside-sentinel", handle.read())
        self.assertEqual(
            ["_bgm_warm.wav"],
            sorted(os.listdir(tv_dir)),
        )


class TextVideoRenderBoundaryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ffmpeg = os.path.join(self.tmp.name, "ffmpeg-fixed")
        with open(self.ffmpeg, "wb") as handle:
            handle.write(b"test executable")
        os.chmod(self.ffmpeg, 0o700)
        self.environment = mock.patch.dict(
            os.environ,
            {"CONTENTCREW_FFMPEG_PATH": self.ffmpeg},
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def assert_bounded_render(self, command):
        self.assertEqual(os.path.realpath(sys.executable), command[0])
        self.assertEqual(
            [
                "-m",
                "app.textvideo",
                "--bounded-media-exec",
                "render",
                os.path.realpath(self.ffmpeg),
            ],
            command[1:6],
        )
        self.assertIn("-nostdin", command)
        whitelist = command.index("-protocol_whitelist")
        self.assertEqual("file,pipe", command[whitelist + 1])
        self.assertNotEqual("ffmpeg", command[0])

    def test_every_render_command_builder_uses_bounded_launcher(self):
        commands = [
            textvideo._seg_cmd(
                "image.jpg",
                "voice.mp3",
                2.0,
                "subtitle.txt",
                "title.txt",
                "segment.mp4",
            ),
            textvideo._clip_seg_cmd(
                "clip.mp4",
                1.0,
                2.0,
                "voice.mp3",
                "subtitle.txt",
                "title.txt",
                "segment.mp4",
                True,
            ),
            textvideo._ending_cmd(
                "title.txt",
                "ending.txt",
                "ending.mp4",
            ),
            textvideo._xfade_cmd(
                ["one.mp4", "two.mp4"],
                [2.5, 2.5],
                "final.mp4",
                bgm_mood="none",
            )[0],
            textvideo._concat_cmd(
                "list.txt",
                "final.mp4",
                transcode=False,
            ),
            textvideo._concat_cmd(
                "list.txt",
                "final.mp4",
                transcode=True,
            ),
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assert_bounded_render(command)

    def test_render_profile_sets_hard_output_limit_and_clean_environment(self):
        with mock.patch(
            "resource.setrlimit",
        ) as set_limit, mock.patch.object(
            textvideo.os,
            "execve",
            side_effect=RuntimeError("exec intercepted"),
        ) as execute, mock.patch.dict(
            os.environ,
            {"SHOULD_NOT_REACH_RENDERER": "sensitive"},
        ):
            with self.assertRaisesRegex(RuntimeError, "exec intercepted"):
                textvideo._bounded_media_worker([
                    "render",
                    self.ffmpeg,
                    "-nostdin",
                ])

        set_limit.assert_any_call(
            resource.RLIMIT_FSIZE,
            (
                textvideo.MAX_RENDER_OUTPUT_BYTES,
                textvideo.MAX_RENDER_OUTPUT_BYTES,
            ),
        )
        set_limit.assert_any_call(
            resource.RLIMIT_NOFILE,
            (128, 128),
        )
        executable, arguments, environment = execute.call_args.args
        self.assertEqual(os.path.realpath(self.ffmpeg), executable)
        self.assertEqual(os.path.realpath(self.ffmpeg), arguments[0])
        self.assertNotIn("SHOULD_NOT_REACH_RENDERER", environment)
        self.assertEqual(
            {"PATH", "LANG", "LC_ALL", "HOME"},
            set(environment),
        )

    def test_render_output_guard_rejects_symlink_empty_and_oversize(self):
        empty = os.path.join(self.tmp.name, "empty.mp4")
        with open(empty, "wb"):
            pass
        self.assertFalse(textvideo._render_output_ok(empty))

        oversized = os.path.join(self.tmp.name, "oversized.mp4")
        with open(oversized, "wb") as handle:
            handle.truncate(textvideo.MAX_RENDER_OUTPUT_BYTES + 1)
        self.assertFalse(textvideo._render_output_ok(oversized))

        outside = os.path.join(self.tmp.name, "outside.mp4")
        with open(outside, "wb") as handle:
            handle.write(b"video")
        linked = os.path.join(self.tmp.name, "linked.mp4")
        os.symlink(outside, linked)
        self.assertFalse(textvideo._render_output_ok(linked))

    def test_runtime_source_has_no_direct_ffmpeg_command_array(self):
        source = inspect.getsource(textvideo)
        self.assertNotRegex(
            source,
            r"\[\s*[\"']ffmpeg[\"']",
        )


if __name__ == "__main__":
    unittest.main()
