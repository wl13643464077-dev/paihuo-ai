import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from contextlib import contextmanager
from unittest import mock

from fastapi import HTTPException
from PIL import Image
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request
from starlette.responses import Response

from app import auth, avatar, db, textvideo


class PersistentUploadSecurityCase(unittest.TestCase):
    def setUp(self):
        from app import main

        self.main = main
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        self.old_public_dir = avatar.PUBLIC_DIR
        self.old_clip_root = textvideo.CLIP_ROOT
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "persistent-upload.db")
        avatar.PUBLIC_DIR = os.path.join(self.tmp.name, "public")
        textvideo.CLIP_ROOT = os.path.join(self.tmp.name, "clips")
        os.makedirs(avatar.PUBLIC_DIR, exist_ok=True)
        os.makedirs(textvideo.CLIP_ROOT, exist_ok=True)
        db.conn()
        db.insert("tenants", {"id": 1, "name": "平台"})
        db.insert("tenants", {"id": 2, "name": "租户甲"})
        db.insert("tenants", {"id": 3, "name": "租户乙"})
        self._as_tenant(2)
        self.main._persistent_upload_hits.clear()
        self.main._persistent_upload_active_tenants.clear()

    def tearDown(self):
        self.main._persistent_upload_hits.clear()
        self.main._persistent_upload_active_tenants.clear()
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        avatar.PUBLIC_DIR = self.old_public_dir
        textvideo.CLIP_ROOT = self.old_clip_root
        self.tmp.cleanup()

    @staticmethod
    def _jpeg_bytes() -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (24, 24), (120, 80, 40)).save(output, "JPEG")
        return output.getvalue()

    @staticmethod
    def _wav_bytes() -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 8000)
        return output.getvalue()

    @staticmethod
    def _upload(name: str, data: bytes, content_type: str) -> UploadFile:
        return UploadFile(
            io.BytesIO(data),
            size=len(data),
            filename=name,
            headers=Headers({"content-type": content_type}),
        )

    @staticmethod
    def _public_tree_inodes() -> set[tuple[int, int]]:
        inodes = set()
        for root, _directories, files in os.walk(avatar.PUBLIC_DIR):
            for name in files:
                path = os.path.join(root, name)
                metadata = os.stat(path, follow_symlinks=False)
                if not os.path.islink(path):
                    inodes.add((metadata.st_dev, metadata.st_ino))
        return inodes

    @staticmethod
    def _request(path: str, *, content_length: int) -> Request:
        async def unread_body():
            raise AssertionError(
                "test call_next must not consume the request body"
            )

        return Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "headers": [
                    (b"cookie", b"cc_sess=test-session"),
                    (
                        b"content-length",
                        str(content_length).encode("ascii"),
                    ),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("paihuo.ai", 443),
            },
            receive=unread_body,
        )

    @staticmethod
    def _as_tenant(tenant_id: int):
        auth.set_current({
            "id": tenant_id * 10,
            "tenant_id": tenant_id,
            "username": f"owner-{tenant_id}",
            "role": "owner",
            "modules": ["avatar", "content"],
        })

    def test_media_parser_rejects_disguised_or_mismatched_uploads(self):
        avatar.validate_upload_media(self._jpeg_bytes(), ".jpg", "photo")
        avatar.validate_upload_media(self._wav_bytes(), ".wav", "voice")

        for data, ext, kind in (
            (b"<html>not an image</html>", ".jpg", "photo"),
            (self._jpeg_bytes(), ".png", "photo"),
            (b"ID3-not-a-real-audio-stream", ".mp3", "voice"),
            (b"\x00\x00\x00\x18ftypmp42-not-a-video", ".mp4", "video"),
        ):
            with self.subTest(ext=ext, kind=kind), self.assertRaises(
                avatar.InvalidAvatarMedia
            ):
                avatar.validate_upload_media(data, ext, kind)

    def test_avatar_media_probe_uses_bounded_launcher_without_preexec(self):
        probe = os.path.join(self.tmp.name, "ffprobe-avatar")
        with open(probe, "wb") as handle:
            handle.write(b"test executable")
        os.chmod(probe, 0o700)

        def bounded_probe(command, **kwargs):
            self.assertEqual(os.path.realpath(sys.executable), command[0])
            self.assertEqual(
                ["-m", "app.textvideo", "--bounded-media-exec", "probe"],
                command[1:5],
            )
            self.assertEqual(os.path.realpath(probe), command[5])
            self.assertNotIn("preexec_fn", kwargs)
            self.assertNotIn("capture_output", kwargs)
            self.assertEqual(subprocess.DEVNULL, kwargs["stderr"])
            self.assertLessEqual(kwargs["timeout"], 15)
            kwargs["stdout"].write(json.dumps({
                "streams": [{"codec_type": "audio"}],
                "format": {
                    "duration": "2.0",
                    "format_name": "mp3",
                },
            }).encode("utf-8"))
            return mock.Mock(returncode=0)

        with mock.patch.dict(
            os.environ,
            {"CONTENTCREW_FFPROBE_PATH": probe},
        ), mock.patch.object(
            avatar.subprocess,
            "run",
            side_effect=bounded_probe,
        ):
            payload = avatar._probe_media_bytes(b"ID3-audio-fixture")

        self.assertEqual(
            "audio", payload["streams"][0]["codec_type"]
        )

    def test_audio_cap_is_unique_bounded_and_cleans_partial_failure(self):
        source = os.path.join(avatar.PUBLIC_DIR, "source.mp3")
        with open(source, "wb") as handle:
            handle.write(b"ID3-source")
        ffmpeg = os.path.join(self.tmp.name, "ffmpeg-avatar")
        with open(ffmpeg, "wb") as handle:
            handle.write(b"test executable")
        os.chmod(ffmpeg, 0o700)
        invocations = 0

        def bounded_audio(command, **kwargs):
            nonlocal invocations
            invocations += 1
            self.assertEqual(os.path.realpath(sys.executable), command[0])
            self.assertEqual(
                ["-m", "app.textvideo", "--bounded-media-exec", "audio"],
                command[1:5],
            )
            self.assertEqual(os.path.realpath(ffmpeg), command[5])
            self.assertNotIn("preexec_fn", kwargs)
            self.assertNotIn("capture_output", kwargs)
            self.assertEqual(subprocess.DEVNULL, kwargs["stdout"])
            self.assertEqual(subprocess.DEVNULL, kwargs["stderr"])
            output = command[-1]
            with open(output, "wb") as handle:
                handle.write(b"ID3-capped")
            return mock.Mock(returncode=0 if invocations == 1 else 1)

        with mock.patch.dict(
            os.environ,
            {"CONTENTCREW_FFMPEG_PATH": ffmpeg},
        ), mock.patch.object(
            avatar.subprocess,
            "run",
            side_effect=bounded_audio,
        ):
            capped = avatar._cap_audio(source, 30)
            self.assertNotEqual(
                os.path.realpath(source), os.path.realpath(capped)
            )
            self.assertTrue(os.path.isfile(capped))
            before_failure = set(os.listdir(avatar.PUBLIC_DIR))
            with self.assertRaises(avatar.providers.ProviderError):
                avatar._cap_audio(source, 30)

        self.assertEqual(
            before_failure,
            set(os.listdir(avatar.PUBLIC_DIR)),
        )
        self.assertFalse(any(
            name.startswith(".audio-cap-")
            for name in os.listdir(avatar.PUBLIC_DIR)
        ))

    def test_invalid_avatar_media_is_rejected_before_public_write(self):
        before = set(os.listdir(avatar.PUBLIC_DIR))
        upload = self._upload(
            "face.jpg", b"<script>not an image</script>", "image/jpeg"
        )
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(self.main.avatar_upload(upload, "photo"))
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual(before, set(os.listdir(avatar.PUBLIC_DIR)))
        self.assertEqual([], avatar.saved_assets(2))

    def test_avatar_rate_limit_rejects_before_read_or_write(self):
        first = self._upload("face.jpg", self._jpeg_bytes(), "image/jpeg")
        with mock.patch.object(
            self.main, "_PERSISTENT_UPLOAD_USER_LIMIT", 1
        ), mock.patch.object(
            self.main, "_PERSISTENT_UPLOAD_TENANT_LIMIT", 1
        ):
            result = asyncio.run(self.main.avatar_upload(first, "photo"))
            self.assertTrue(os.path.isfile(
                os.path.join(avatar.PUBLIC_DIR, result["name"])
            ))
            second = self._upload(
                "second.jpg", self._jpeg_bytes(), "image/jpeg"
            )
            with mock.patch.object(
                self.main,
                "_read_limited",
                new=mock.AsyncMock(
                    side_effect=AssertionError("rate limit must run before read")
                ),
            ) as read_limited, self.assertRaises(HTTPException) as caught:
                asyncio.run(self.main.avatar_upload(second, "photo"))
            self.assertEqual(429, caught.exception.status_code)
            read_limited.assert_not_awaited()

    def test_upload_middleware_reserves_slot_before_multipart_parsing(self):
        request = self._request(
            "/api/avatar/upload",
            content_length=1024,
        )
        observed = {}
        user = {
            "id": 20,
            "tenant_id": 2,
            "username": "owner-2",
            "role": "owner",
            "modules": ["avatar"],
            "must_change_password": 0,
        }

        async def call_next(_request):
            observed["reserved"] = (
                self.main._PERSISTENT_UPLOAD_RESERVED.get()
            )
            # The endpoint's existing guard must recognize the middleware
            # reservation instead of charging a second rate-limit hit.
            async with self.main._persistent_upload_slot("avatar"):
                observed["nested"] = True
            return Response(status_code=204)

        with mock.patch.object(
            self.main.auth, "parse_session", return_value=20
        ), mock.patch.object(
            self.main.auth, "get_user", return_value=user
        ):
            response = asyncio.run(
                self.main._auth_mw(request, call_next)
            )

        self.assertEqual(204, response.status_code)
        self.assertEqual(
            {"reserved": True, "nested": True},
            observed,
        )
        self.assertNotIn(2, self.main._persistent_upload_active_tenants)
        self.assertEqual(
            [1, 1],
            sorted(
                len(stamps)
                for stamps in self.main._persistent_upload_hits.values()
            ),
        )

    def test_upload_middleware_rejects_oversize_before_call_next(self):
        request = self._request(
            "/api/tv/clips",
            content_length=(
                self.main._CLIP_UPLOAD_MAX_BYTES
                + self.main._UPLOAD_MULTIPART_OVERHEAD_BYTES
                + 1
            ),
        )
        called = False
        user = {
            "id": 20,
            "tenant_id": 2,
            "username": "owner-2",
            "role": "owner",
            "modules": ["content"],
            "must_change_password": 0,
        }

        async def call_next(_request):
            nonlocal called
            called = True
            return Response(status_code=204)

        with mock.patch.object(
            self.main.auth, "parse_session", return_value=20
        ), mock.patch.object(
            self.main.auth, "get_user", return_value=user
        ):
            response = asyncio.run(
                self.main._auth_mw(request, call_next)
            )

        self.assertEqual(413, response.status_code)
        self.assertFalse(called)
        self.assertEqual({}, self.main._persistent_upload_hits)
        self.assertEqual(
            set(), self.main._persistent_upload_active_tenants
        )

    def test_asset_eviction_deletes_only_unreferenced_current_tenant_file(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 2), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(b"a" * 10, ".mp3", "voice", 2)
            db.insert("avatar_job", {
                "tenant_id": 2,
                "params_json": json.dumps(
                    {"own_audio_name": first["name"]}, ensure_ascii=False
                ),
                "status": "done",
                "audio_file": f"/files/avatar-public/{first['name']}",
            })
            second = avatar.store_uploaded_asset(
                b"b" * 10, ".mp3", "voice", 2
            )

            self._as_tenant(3)
            foreign = avatar.store_uploaded_asset(
                b"x" * 10, ".mp3", "voice", 3
            )
            self._as_tenant(2)
            third = avatar.store_uploaded_asset(
                b"c" * 10, ".mp3", "voice", 2
            )

        self.assertTrue(os.path.isfile(
            os.path.join(avatar.PUBLIC_DIR, first["name"])
        ))
        self.assertFalse(os.path.exists(
            os.path.join(avatar.PUBLIC_DIR, second["name"])
        ))
        self.assertTrue(os.path.isfile(
            os.path.join(avatar.PUBLIC_DIR, third["name"])
        ))
        self.assertTrue(os.path.isfile(
            os.path.join(avatar.PUBLIC_DIR, foreign["name"])
        ))
        self.assertTrue(avatar.asset_belongs(first["name"], {"voice"}, 2))
        self.assertFalse(avatar.asset_belongs(second["name"], {"voice"}, 2))
        self.assertTrue(avatar.asset_belongs(foreign["name"], {"voice"}, 3))

    def test_failed_eviction_rejects_upload_and_repeated_attempts_do_not_grow(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(
                b"a" * 10, ".mp3", "voice", 2
            )
            first_path = os.path.join(avatar.PUBLIC_DIR, first["name"])
            before = set(os.listdir(avatar.PUBLIC_DIR))
            real_link = os.link

            def fail_first_quarantine(source, target, *args, **kwargs):
                if (
                    os.path.realpath(source)
                    == os.path.realpath(first_path)
                    and ".avatar-upload-txn-" in target
                ):
                    raise OSError("injected quarantine failure")
                return real_link(source, target, *args, **kwargs)

            with mock.patch.object(
                avatar.os, "link", side_effect=fail_first_quarantine
            ):
                for payload in (b"b" * 10, b"c" * 10, b"d" * 10):
                    with self.assertRaises(avatar.AssetQuotaExceeded):
                        avatar.store_uploaded_asset(
                            payload, ".mp3", "voice", 2
                        )

        registered = {
            item["name"] for item in avatar.saved_assets(2)
        }
        self.assertEqual({first["name"]}, registered)
        self.assertEqual(before, set(os.listdir(avatar.PUBLIC_DIR)))
        self.assertTrue(os.path.isfile(first_path))
        usage = avatar.tenant_asset_usage(2)
        self.assertEqual(1, usage["files"])
        self.assertEqual(10, usage["bytes"])

    def test_publish_failure_after_eviction_restores_old_file_and_registry(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(
                b"original-bytes", ".mp3", "voice", 2
            )
            first_path = os.path.join(avatar.PUBLIC_DIR, first["name"])
            before = set(os.listdir(avatar.PUBLIC_DIR))

            with mock.patch.object(
                avatar,
                "_publish_staged_upload",
                create=True,
                side_effect=OSError("injected publish failure"),
            ), self.assertRaises(OSError):
                avatar.store_uploaded_asset(
                    b"replacement", ".mp3", "voice", 2
                )

        self.assertEqual(before, set(os.listdir(avatar.PUBLIC_DIR)))
        with open(first_path, "rb") as handle:
            self.assertEqual(b"original-bytes", handle.read())
        self.assertEqual(
            {first["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )
        self.assertEqual(
            {"files": 1, "bytes": len(b"original-bytes"),
             "names": {first["name"]}},
            avatar.tenant_asset_usage(2),
        )

    def test_second_eviction_backup_failure_keeps_every_old_public_file(self):
        first = avatar.store_uploaded_asset(
            b"first-old", ".mp3", "voice", 2
        )
        second = avatar.store_uploaded_asset(
            b"second-old", ".mp3", "voice", 2
        )
        first_path = os.path.join(avatar.PUBLIC_DIR, first["name"])
        second_path = os.path.join(avatar.PUBLIC_DIR, second["name"])
        before = set(os.listdir(avatar.PUBLIC_DIR))
        real_link = os.link

        def fail_second_quarantine(source, target, *args, **kwargs):
            if (
                os.path.realpath(source) == os.path.realpath(second_path)
                and ".avatar-upload-txn-" in target
            ):
                raise OSError("injected second quarantine failure")
            return real_link(source, target, *args, **kwargs)

        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024), \
                mock.patch.object(
                    avatar.os,
                    "link",
                    side_effect=fail_second_quarantine,
                ), self.assertRaises(avatar.AssetQuotaExceeded):
            avatar.store_uploaded_asset(
                b"replacement", ".mp3", "voice", 2
            )

        self.assertEqual(before, set(os.listdir(avatar.PUBLIC_DIR)))
        with open(first_path, "rb") as handle:
            self.assertEqual(b"first-old", handle.read())
        with open(second_path, "rb") as handle:
            self.assertEqual(b"second-old", handle.read())
        self.assertEqual(
            {first["name"], second["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )
        self.assertEqual(2, avatar.tenant_asset_usage(2)["files"])

    def test_registry_reservation_failure_never_creates_an_unregistered_file(self):
        before = set(os.listdir(avatar.PUBLIC_DIR))
        with mock.patch.object(
            avatar,
            "_save_asset_lists",
            side_effect=RuntimeError("injected registry failure"),
        ), mock.patch.object(
            avatar.os,
            "remove",
            side_effect=OSError("injected cleanup failure"),
        ), self.assertRaises(RuntimeError):
            avatar.store_uploaded_asset(
                b"a" * 10, ".mp3", "voice", 2
            )

        self.assertEqual(before, set(os.listdir(avatar.PUBLIC_DIR)))
        self.assertEqual([], avatar.saved_assets(2))
        self.assertEqual(
            {"files": 0, "bytes": 0, "names": set()},
            avatar.tenant_asset_usage(2),
        )

    def test_partial_publication_failure_removes_new_link_and_registration(self):
        materialized = {}

        def fail_after_materializing(staged_path, reserved_name):
            path = os.path.join(avatar.PUBLIC_DIR, reserved_name)
            os.link(staged_path, path, follow_symlinks=False)
            os.chmod(path, 0o640)
            materialized["name"] = reserved_name
            raise RuntimeError("injected partial publication failure")

        with mock.patch.object(
            avatar,
            "_publish_staged_upload",
            side_effect=fail_after_materializing,
        ), self.assertRaises(RuntimeError):
            avatar.store_uploaded_asset(
                b"a" * 10, ".mp3", "voice", 2
            )

        self.assertTrue(materialized.get("name"))
        self.assertFalse(os.path.exists(os.path.join(
            avatar.PUBLIC_DIR,
            materialized["name"],
        )))
        self.assertEqual([], avatar.saved_assets(2))
        self.assertEqual(0, avatar.tenant_asset_usage(2)["files"])
        self.assertEqual(set(), set(os.listdir(avatar.PUBLIC_DIR)))

    def test_registry_switch_failure_restores_old_file_and_registration(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(
                b"a" * 10, ".mp3", "voice", 2
            )
            real_save = avatar._save_asset_lists
            save_calls = 0

            def fail_final_save(tid, assets, photos):
                nonlocal save_calls
                save_calls += 1
                if save_calls == 1:
                    raise RuntimeError("injected registry finalize failure")
                return real_save(tid, assets, photos)

            with mock.patch.object(
                avatar,
                "_save_asset_lists",
                side_effect=fail_final_save,
            ), self.assertRaises(RuntimeError):
                avatar.store_uploaded_asset(
                    b"b" * 10, ".mp3", "voice", 2
                )

        registered = {
            item["name"] for item in avatar.saved_assets(2)
        }
        durable = {
            name
            for name in os.listdir(avatar.PUBLIC_DIR)
            if avatar._public_file_path(name)
        }
        self.assertEqual({first["name"]}, durable)
        self.assertEqual({first["name"]}, registered)
        self.assertEqual(1, avatar.tenant_asset_usage(2)["files"])

    def test_database_commit_failure_unpublishes_new_and_restores_old(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(
                b"durable-old", ".mp3", "voice", 2
            )
            first_path = os.path.join(avatar.PUBLIC_DIR, first["name"])
            before = set(os.listdir(avatar.PUBLIC_DIR))
            real_atomic = db.atomic

            @contextmanager
            def fail_at_commit():
                with real_atomic():
                    yield
                    raise RuntimeError("injected database commit failure")

            with mock.patch.object(
                avatar.db,
                "atomic",
                fail_at_commit,
            ), self.assertRaises(RuntimeError):
                avatar.store_uploaded_asset(
                    b"replacement", ".mp3", "voice", 2
                )

        self.assertEqual(before, set(os.listdir(avatar.PUBLIC_DIR)))
        with open(first_path, "rb") as handle:
            self.assertEqual(b"durable-old", handle.read())
        self.assertEqual(
            {first["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )

    def test_reserved_name_collision_never_overwrites_existing_user_file(self):
        first = avatar.store_uploaded_asset(
            b"user-owned", ".mp3", "voice", 2
        )
        first_path = os.path.join(avatar.PUBLIC_DIR, first["name"])
        before = set(os.listdir(avatar.PUBLIC_DIR))

        with mock.patch.object(
            avatar,
            "_reserved_public_name",
            return_value=first["name"],
        ), self.assertRaises(FileExistsError):
            avatar.store_uploaded_asset(
                b"must-not-overwrite", ".mp3", "voice", 2
            )

        self.assertEqual(before, set(os.listdir(avatar.PUBLIC_DIR)))
        with open(first_path, "rb") as handle:
            self.assertEqual(b"user-owned", handle.read())
        self.assertEqual(
            [first["name"]],
            [item["name"] for item in avatar.saved_assets(2)],
        )

    def test_incoming_bytes_are_private_and_same_disk_before_eviction(self):
        observed = {}
        real_quarantine = avatar._quarantine_uploaded_asset_evictions

        def inspect_stage(tid, removed, transaction_dir):
            staged_path = os.path.join(
                transaction_dir,
                avatar._ASSET_TRANSACTION_PAYLOAD,
                ".incoming",
            )
            observed["same_device"] = (
                os.stat(transaction_dir).st_dev
                == os.stat(avatar.PUBLIC_DIR).st_dev
            )
            observed["dir_mode"] = (
                os.stat(transaction_dir).st_mode & 0o777
            )
            observed["file_mode"] = (
                os.stat(staged_path).st_mode & 0o777
            )
            return real_quarantine(tid, removed, transaction_dir)

        with mock.patch.object(
            avatar,
            "_quarantine_uploaded_asset_evictions",
            side_effect=inspect_stage,
        ):
            uploaded = avatar.store_uploaded_asset(
                b"private-stage", ".mp3", "voice", 2
            )

        self.assertEqual({
            "same_device": True,
            "dir_mode": 0o700,
            "file_mode": 0o000,
        }, observed)
        self.assertEqual(
            {uploaded["name"]},
            set(os.listdir(avatar.PUBLIC_DIR)),
        )

    def test_precommit_journal_recovery_keeps_original_public_asset(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(
                b"precommit-old", ".mp3", "voice", 2
            )
            first_path = os.path.join(avatar.PUBLIC_DIR, first["name"])
            with mock.patch.object(
                avatar.db,
                "atomic",
                side_effect=RuntimeError("simulated crash before commit"),
            ), mock.patch.object(
                avatar,
                "_cleanup_asset_transaction",
                return_value=False,
            ), self.assertRaises(RuntimeError):
                avatar.store_uploaded_asset(
                    b"never-committed", ".mp3", "voice", 2
                )

        self.assertEqual(1, len(
            avatar._tenant_asset_transaction_dirs(2)
        ))
        self.assertTrue(os.path.isfile(first_path))
        self.assertGreater(
            avatar.tenant_asset_usage(2)["files"],
            1,
        )

        recovered = avatar.recover_asset_transactions(
            2,
            raise_on_blocked=True,
        )
        self.assertEqual({"recovered": 1, "blocked": 0}, recovered)
        self.assertEqual([], avatar._tenant_asset_transaction_dirs(2))
        with open(first_path, "rb") as handle:
            self.assertEqual(b"precommit-old", handle.read())
        self.assertEqual(
            {first["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )

    def test_pre_manifest_crash_state_is_strictly_cleaned_on_recovery(self):
        before = set(os.listdir(avatar.PUBLIC_DIR))
        with mock.patch.object(
            avatar,
            "_write_asset_transaction_manifest",
            side_effect=RuntimeError("simulated pre-manifest crash"),
        ), mock.patch.object(
            avatar,
            "_cleanup_asset_transaction",
            return_value=False,
        ), self.assertRaises(RuntimeError):
            avatar.store_uploaded_asset(
                b"pre-manifest", ".mp3", "voice", 2
            )

        transactions = avatar._tenant_asset_transaction_dirs(2)
        self.assertEqual(1, len(transactions))
        self.assertFalse(os.path.exists(os.path.join(
            transactions[0],
            avatar._ASSET_TRANSACTION_MANIFEST,
        )))
        self.assertEqual([], avatar.saved_assets(2))

        self.assertEqual(
            {"recovered": 1, "blocked": 0},
            avatar.recover_asset_transactions(
                2,
                raise_on_blocked=True,
            ),
        )
        self.assertEqual(before, set(os.listdir(avatar.PUBLIC_DIR)))
        self.assertEqual([], avatar._tenant_asset_transaction_dirs(2))

    def test_postcommit_journal_recovery_finishes_eviction(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(
                b"postcommit-old", ".mp3", "voice", 2
            )
            with mock.patch.object(
                avatar,
                "_finalize_committed_asset_evictions",
                side_effect=RuntimeError(
                    "simulated crash after commit"
                ),
            ):
                second = avatar.store_uploaded_asset(
                    b"postcommit-new", ".mp3", "voice", 2
                )

        self.assertEqual(1, len(
            avatar._tenant_asset_transaction_dirs(2)
        ))
        self.assertTrue(os.path.isfile(os.path.join(
            avatar.PUBLIC_DIR,
            first["name"],
        )))
        self.assertTrue(os.path.isfile(os.path.join(
            avatar.PUBLIC_DIR,
            second["name"],
        )))
        self.assertEqual(
            {second["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )

        recovered = avatar.recover_asset_transactions(
            2,
            raise_on_blocked=True,
        )
        self.assertEqual({"recovered": 1, "blocked": 0}, recovered)
        self.assertFalse(os.path.exists(os.path.join(
            avatar.PUBLIC_DIR,
            first["name"],
        )))
        self.assertTrue(os.path.isfile(os.path.join(
            avatar.PUBLIC_DIR,
            second["name"],
        )))
        self.assertEqual([], avatar._tenant_asset_transaction_dirs(2))

    def test_failed_transaction_rmtree_is_counted_and_blocks_growth(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            avatar.store_uploaded_asset(
                b"old-rmtree", ".mp3", "voice", 2
            )
            with mock.patch.object(
                avatar.shutil,
                "rmtree",
                side_effect=OSError("injected rmtree failure"),
            ):
                second = avatar.store_uploaded_asset(
                    b"new-rmtree", ".mp3", "voice", 2
                )
                before = self._public_tree_inodes()
                usage = avatar.tenant_asset_usage(2)
                self.assertGreaterEqual(usage["files"], 3)
                self.assertGreaterEqual(
                    usage["bytes"],
                    len(b"old-rmtree") + len(b"new-rmtree"),
                )
                for payload in (b"third", b"fourth"):
                    with self.assertRaises(avatar.AssetQuotaExceeded):
                        avatar.store_uploaded_asset(
                            payload, ".mp3", "voice", 2
                        )
                    self.assertEqual(
                        before,
                        self._public_tree_inodes(),
                    )

        self.assertEqual(
            {second["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )
        self.assertEqual(
            {"recovered": 1, "blocked": 0},
            avatar.recover_asset_transactions(
                2,
                raise_on_blocked=True,
            ),
        )

    def test_partial_payload_cleanup_preserves_manifest_until_recovery(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            avatar.store_uploaded_asset(
                b"manifest-last-old", ".mp3", "voice", 2
            )
            with mock.patch.object(
                avatar,
                "_finalize_committed_asset_evictions",
                side_effect=RuntimeError("defer committed cleanup"),
            ):
                second = avatar.store_uploaded_asset(
                    b"manifest-last-new", ".mp3", "voice", 2
                )
            transaction = avatar._tenant_asset_transaction_dirs(2)[0]
            manifest_path = os.path.join(
                transaction,
                avatar._ASSET_TRANSACTION_MANIFEST,
            )
            real_unlink = os.unlink

            def remove_incoming_then_fail(payload):
                incoming = os.path.join(payload, ".incoming")
                if os.path.exists(incoming):
                    real_unlink(incoming)
                raise OSError("injected partial payload cleanup")

            with mock.patch.object(
                avatar.shutil,
                "rmtree",
                side_effect=remove_incoming_then_fail,
            ):
                self.assertEqual(
                    {"recovered": 0, "blocked": 1},
                    avatar.recover_asset_transactions(2),
                )
                self.assertTrue(os.path.isfile(manifest_path))
                before = self._public_tree_inodes()
                with self.assertRaises(avatar.AssetQuotaExceeded):
                    avatar.store_uploaded_asset(
                        b"must-not-grow", ".mp3", "voice", 2
                    )
                self.assertEqual(before, self._public_tree_inodes())
                self.assertTrue(os.path.isfile(manifest_path))

        self.assertEqual(
            {second["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )
        self.assertEqual(
            {"recovered": 1, "blocked": 0},
            avatar.recover_asset_transactions(
                2,
                raise_on_blocked=True,
            ),
        )

    def test_pending_transaction_blocks_photo_registry_mutation(self):
        photo_bytes = self._jpeg_bytes()
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(
                    avatar,
                    "MAX_TENANT_ASSET_BYTES",
                    len(photo_bytes) * 2,
                ):
            avatar.store_uploaded_asset(
                photo_bytes, ".jpg", "photo", 2
            )
            with mock.patch.object(
                avatar,
                "_finalize_committed_asset_evictions",
                side_effect=RuntimeError("defer committed cleanup"),
            ):
                second = avatar.store_uploaded_asset(
                    photo_bytes, ".jpg", "photo", 2
                )
            with mock.patch.object(
                avatar.shutil,
                "rmtree",
                side_effect=OSError("cleanup remains unavailable"),
            ), self.assertRaises(avatar.AssetQuotaExceeded):
                avatar.photos_remove(second["name"])

        self.assertIn(
            second["name"],
            {item["name"] for item in avatar.saved_assets(2)},
        )
        self.assertIn(
            second["name"],
            {item["name"] for item in avatar.saved_photos()},
        )
        self.assertEqual(
            {"recovered": 1, "blocked": 0},
            avatar.recover_asset_transactions(
                2,
                raise_on_blocked=True,
            ),
        )
        self.assertTrue(avatar.photos_remove(second["name"]))

    def test_legacy_registration_never_auto_evicts_to_accept_overquota(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(
                b"registered", ".mp3", "voice", 2
            )
            unregistered = avatar._write_public_bytes(
                b"legacy-file",
                ".mp3",
            )
            with self.assertRaises(avatar.AssetQuotaExceeded):
                avatar.remember_asset(
                    unregistered["name"],
                    "voice",
                    2,
                )

        self.assertTrue(os.path.isfile(os.path.join(
            avatar.PUBLIC_DIR,
            first["name"],
        )))
        self.assertTrue(os.path.isfile(os.path.join(
            avatar.PUBLIC_DIR,
            unregistered["name"],
        )))
        self.assertEqual(
            {first["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )

    def test_startup_recovers_even_in_validation_and_propagates_blocker(self):
        old_validation = self.main.VALIDATION
        self.main.VALIDATION = True
        try:
            with mock.patch.object(
                self.main.db,
                "conn",
            ), mock.patch.object(
                self.main.secureconfig,
                "migrate_legacy_secrets",
            ) as migrate_secrets, mock.patch.object(
                self.main.auth,
                "bootstrap",
            ), mock.patch.object(
                self.main.avatar,
                "recover_asset_transactions",
                return_value={"recovered": 1, "blocked": 0},
            ) as recover:
                asyncio.run(self.main._startup())
            recover.assert_called_once_with(raise_on_blocked=True)
            migrate_secrets.assert_called_once_with()

            with mock.patch.object(
                self.main.db,
                "conn",
            ), mock.patch.object(
                self.main.secureconfig,
                "migrate_legacy_secrets",
            ), mock.patch.object(
                self.main.auth,
                "bootstrap",
            ), mock.patch.object(
                self.main.avatar,
                "recover_asset_transactions",
                side_effect=avatar.AssetQuotaExceeded(
                    "blocked fixture"
                ),
            ), self.assertRaises(avatar.AssetQuotaExceeded):
                asyncio.run(self.main._startup())
        finally:
            self.main.VALIDATION = old_validation

    def test_committed_unlink_failure_retains_backup_and_blocks_growth(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(
                b"old-unlink", ".mp3", "voice", 2
            )
            first_path = os.path.join(avatar.PUBLIC_DIR, first["name"])
            real_unlink = os.unlink

            def fail_old_public_unlink(path, *args, **kwargs):
                if os.path.realpath(path) == os.path.realpath(first_path):
                    raise OSError("injected old unlink failure")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                avatar.os,
                "unlink",
                side_effect=fail_old_public_unlink,
            ):
                second = avatar.store_uploaded_asset(
                    b"new-unlink", ".mp3", "voice", 2
                )
                before = self._public_tree_inodes()
                with self.assertRaises(avatar.AssetQuotaExceeded):
                    avatar.store_uploaded_asset(
                        b"blocked", ".mp3", "voice", 2
                    )
                self.assertEqual(before, self._public_tree_inodes())

        self.assertTrue(os.path.isfile(first_path))
        self.assertEqual(
            {second["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )
        self.assertEqual(
            {"recovered": 1, "blocked": 0},
            avatar.recover_asset_transactions(
                2,
                raise_on_blocked=True,
            ),
        )
        self.assertFalse(os.path.exists(first_path))

    def test_postcommit_directory_fsync_failure_preserves_recovery_inode(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(
                b"old-fsync", ".mp3", "voice", 2
            )
            first_path = os.path.join(avatar.PUBLIC_DIR, first["name"])
            real_fsync_directory = avatar._fsync_directory

            def fail_after_old_unlink(path):
                if (
                    os.path.realpath(path)
                    == os.path.realpath(avatar.PUBLIC_DIR)
                    and not os.path.exists(first_path)
                ):
                    raise OSError("injected postcommit fsync failure")
                return real_fsync_directory(path)

            with mock.patch.object(
                avatar,
                "_fsync_directory",
                side_effect=fail_after_old_unlink,
            ):
                second = avatar.store_uploaded_asset(
                    b"new-fsync", ".mp3", "voice", 2
                )
                before = self._public_tree_inodes()
                with self.assertRaises(avatar.AssetQuotaExceeded):
                    avatar.store_uploaded_asset(
                        b"blocked", ".mp3", "voice", 2
                    )
                self.assertEqual(before, self._public_tree_inodes())

        self.assertFalse(os.path.exists(first_path))
        self.assertEqual(
            {second["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )
        self.assertGreater(
            avatar.tenant_asset_usage(2)["files"],
            1,
        )
        self.assertEqual(
            {"recovered": 1, "blocked": 0},
            avatar.recover_asset_transactions(
                2,
                raise_on_blocked=True,
            ),
        )

    def test_failed_publish_rollback_retains_journal_and_never_grows(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 1), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 1024):
            first = avatar.store_uploaded_asset(
                b"rollback-old", ".mp3", "voice", 2
            )
            real_unlink = os.unlink
            failed_target = {}

            def publish_then_fail(staged_path, reserved_name):
                target = os.path.join(avatar.PUBLIC_DIR, reserved_name)
                os.link(staged_path, target, follow_symlinks=False)
                os.chmod(target, 0o640)
                failed_target["path"] = target
                raise RuntimeError("injected publish failure")

            def fail_new_public_unlink(path, *args, **kwargs):
                if (
                    failed_target.get("path")
                    and os.path.realpath(path)
                    == os.path.realpath(failed_target["path"])
                ):
                    raise OSError("injected rollback unlink failure")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                avatar,
                "_publish_staged_upload",
                side_effect=publish_then_fail,
            ), mock.patch.object(
                avatar.os,
                "unlink",
                side_effect=fail_new_public_unlink,
            ), self.assertRaises(RuntimeError):
                avatar.store_uploaded_asset(
                    b"rollback-new", ".mp3", "voice", 2
                )

            before = self._public_tree_inodes()
            with mock.patch.object(
                avatar.os,
                "unlink",
                side_effect=fail_new_public_unlink,
            ), self.assertRaises(avatar.AssetQuotaExceeded):
                avatar.store_uploaded_asset(
                    b"must-block", ".mp3", "voice", 2
                )
            self.assertEqual(before, self._public_tree_inodes())

        self.assertTrue(os.path.isfile(os.path.join(
            avatar.PUBLIC_DIR,
            first["name"],
        )))
        self.assertEqual(
            {first["name"]},
            {item["name"] for item in avatar.saved_assets(2)},
        )
        self.assertEqual(
            {"recovered": 1, "blocked": 0},
            avatar.recover_asset_transactions(
                2,
                raise_on_blocked=True,
            ),
        )
        self.assertEqual(
            {first["name"]},
            set(os.listdir(avatar.PUBLIC_DIR)),
        )

    def test_photo_delete_and_upload_share_one_registry_lock(self):
        first = avatar.store_uploaded_asset(
            self._jpeg_bytes(), ".jpg", "photo", 2
        )
        delete_reached_save = threading.Event()
        allow_delete_save = threading.Event()
        upload_started = threading.Event()
        errors = []
        uploaded = {}
        real_save_lists = avatar._save_asset_lists
        real_write_public = avatar._write_public_bytes
        gated = False

        def gated_save_lists(tid, assets, photos):
            nonlocal gated
            if (
                threading.current_thread().name == "asset-delete"
                and not gated
            ):
                gated = True
                delete_reached_save.set()
                if not allow_delete_save.wait(5):
                    raise RuntimeError("delete test barrier timed out")
            return real_save_lists(tid, assets, photos)

        def observed_write(data, ext, **kwargs):
            upload_started.set()
            return real_write_public(data, ext, **kwargs)

        def delete_photo():
            try:
                self._as_tenant(2)
                avatar.photos_remove(first["name"])
            except BaseException as exc:
                errors.append(exc)

        def upload_photo():
            try:
                uploaded.update(avatar.store_uploaded_asset(
                    self._jpeg_bytes(), ".jpg", "photo", 2
                ))
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(
            avatar, "_save_asset_lists", side_effect=gated_save_lists
        ), mock.patch.object(
            avatar, "_write_public_bytes", side_effect=observed_write
        ):
            deleting = threading.Thread(
                target=delete_photo, name="asset-delete"
            )
            deleting.start()
            self.assertTrue(delete_reached_save.wait(5))
            uploading = threading.Thread(
                target=upload_photo, name="asset-upload"
            )
            uploading.start()
            # The upload has started at the caller but must not reach its
            # durable file write while deletion holds the registry RLock.
            time.sleep(0.05)
            self.assertFalse(upload_started.is_set())
            allow_delete_save.set()
            deleting.join(5)
            uploading.join(5)

        self.assertFalse(deleting.is_alive())
        self.assertFalse(uploading.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(uploaded.get("name"))
        registered = {
            item["name"] for item in avatar.saved_assets(2)
        }
        self.assertEqual({uploaded["name"]}, registered)
        self.assertFalse(os.path.exists(
            os.path.join(avatar.PUBLIC_DIR, first["name"])
        ))
        self.assertTrue(os.path.isfile(
            os.path.join(avatar.PUBLIC_DIR, uploaded["name"])
        ))

    def test_avatar_job_create_and_photo_delete_share_reference_lock(self):
        photo = avatar.store_uploaded_asset(
            self._jpeg_bytes(), ".jpg", "photo", 2
        )
        path = os.path.join(avatar.PUBLIC_DIR, photo["name"])
        create_entered = threading.Event()
        allow_insert = threading.Event()
        delete_done = threading.Event()
        create_errors = []
        delete_outcome = {}

        def delayed_create(params, tid=None):
            create_entered.set()
            if not allow_insert.wait(5):
                raise AssertionError("avatar insert barrier timed out")
            return db.insert("avatar_job", {
                "tenant_id": int(tid or 2),
                "params_json": json.dumps(params, ensure_ascii=False),
                "status": "queued",
                "billing_status": "charged",
                "billing_points": 1,
            })

        def discard_task(coroutine):
            coroutine.close()
            return mock.Mock()

        def create_job():
            self._as_tenant(2)
            try:
                asyncio.run(self.main.avatar_job_create({
                    "photo_name": photo["name"],
                    "script": "这是一段用于验证素材引用互斥的数字人口播稿。",
                    "duration": 15,
                }))
            except BaseException as exc:
                create_errors.append(exc)

        def delete_photo():
            self._as_tenant(2)
            try:
                delete_outcome["removed"] = avatar.photos_remove(
                    photo["name"]
                )
            except BaseException as exc:
                delete_outcome["error"] = exc
            finally:
                delete_done.set()

        with mock.patch.object(
            self.main,
            "_create_charged_avatar_job",
            side_effect=delayed_create,
        ), mock.patch.object(
            self.main,
            "_start_avatar_job_worker",
        ):
            creator = threading.Thread(
                target=create_job,
                name="avatar-create",
            )
            creator.start()
            self.assertTrue(create_entered.wait(5))
            deleter = threading.Thread(
                target=delete_photo,
                name="avatar-delete",
            )
            deleter.start()
            time.sleep(0.05)
            self.assertFalse(
                delete_done.is_set(),
                "delete must wait until the avatar job reference is durable",
            )
            allow_insert.set()
            creator.join(5)
            deleter.join(5)

        self.assertFalse(creator.is_alive())
        self.assertFalse(deleter.is_alive())
        self.assertEqual([], create_errors)
        self.assertNotIn("error", delete_outcome)
        self.assertTrue(delete_outcome.get("removed"))
        self.assertTrue(os.path.isfile(path))
        row = db.one(
            "SELECT params_json FROM avatar_job WHERE tenant_id=2"
        )
        self.assertEqual(
            photo["name"],
            json.loads(row["params_json"])["photo_name"],
        )
        self.assertTrue(avatar.asset_belongs(
            photo["name"], {"photo"}, 2
        ))

    def test_referenced_assets_make_byte_quota_fail_before_new_file_write(self):
        with mock.patch.object(avatar, "MAX_TENANT_ASSET_FILES", 10), \
                mock.patch.object(avatar, "MAX_TENANT_ASSET_BYTES", 15):
            first = avatar.store_uploaded_asset(b"a" * 10, ".mp3", "voice", 2)
            db.insert("avatar_job", {
                "tenant_id": 2,
                "params_json": json.dumps(
                    {"own_audio_name": first["name"]}, ensure_ascii=False
                ),
                "status": "done",
                "audio_file": f"/files/avatar-public/{first['name']}",
            })
            before = set(os.listdir(avatar.PUBLIC_DIR))
            with self.assertRaises(avatar.AssetQuotaExceeded):
                avatar.store_uploaded_asset(
                    b"b" * 10, ".mp3", "voice", 2
                )
        self.assertEqual(before, set(os.listdir(avatar.PUBLIC_DIR)))

    def test_clip_shared_quota_is_checked_before_upload_read(self):
        payload = b"x" * 32
        upload = self._upload("clip.mp4", payload, "video/mp4")
        with mock.patch.object(
            self.main, "_PERSISTENT_UPLOAD_TENANT_BYTES", 16
        ), mock.patch.object(
            self.main,
            "_read_limited",
            new=mock.AsyncMock(
                side_effect=AssertionError("quota must run before read")
            ),
            create=True,
        ) as read_limited, self.assertRaises(HTTPException) as caught:
            asyncio.run(self.main.tv_clip_upload(upload))
        self.assertEqual(413, caught.exception.status_code)
        read_limited.assert_not_awaited()
        clip_root = os.path.join(textvideo.CLIP_ROOT, "2")
        self.assertFalse(os.path.exists(clip_root))

    def test_clip_parse_or_thumbnail_failure_leaves_no_persistent_file(self):
        upload = self._upload("clip.mp4", b"video-fixture", "video/mp4")
        with mock.patch.object(
            textvideo,
            "probe_clip",
            return_value={"dur": 12.0, "w": 1280, "h": 720},
        ), mock.patch.object(
            textvideo, "make_thumb", return_value=None
        ), self.assertRaises(HTTPException) as caught:
            asyncio.run(self.main.tv_clip_upload(upload))
        self.assertEqual(400, caught.exception.status_code)
        clip_root = os.path.join(textvideo.CLIP_ROOT, "2")
        self.assertEqual(
            [],
            os.listdir(clip_root) if os.path.isdir(clip_root) else [],
        )

    def test_clip_probe_uses_bounded_parser_and_limited_output(self):
        clip_root = os.path.join(textvideo.CLIP_ROOT, "2")
        os.makedirs(clip_root, exist_ok=True)
        path = os.path.join(
            clip_root,
            "c_0123456789abcdef0123456789abcdef.mp4",
        )
        with open(path, "wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypmp42video-fixture")
        probe = os.path.join(self.tmp.name, "ffprobe")
        with open(probe, "wb") as handle:
            handle.write(b"test executable")
        os.chmod(probe, 0o700)

        def bounded_probe(command, **kwargs):
            self.assertEqual(os.path.realpath(sys.executable), command[0])
            self.assertEqual("-m", command[1])
            self.assertEqual("app.textvideo", command[2])
            self.assertEqual("--bounded-media-exec", command[3])
            self.assertEqual("probe", command[4])
            self.assertEqual(os.path.realpath(probe), command[5])
            self.assertIn("-probesize", command)
            self.assertIn("-analyzeduration", command)
            self.assertLessEqual(kwargs["timeout"], 15)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("capture_output", kwargs)
            self.assertNotIn("preexec_fn", kwargs)
            kwargs["stdout"].write(json.dumps({
                "streams": [{"width": 1280, "height": 720}],
                "format": {
                    "duration": "12.5",
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                },
            }).encode("utf-8"))
            return mock.Mock(returncode=0)

        with mock.patch.dict(
            os.environ,
            {"CONTENTCREW_FFPROBE_PATH": probe},
        ), mock.patch.object(
            textvideo.subprocess,
            "run",
            side_effect=bounded_probe,
        ):
            meta = textvideo.probe_clip(path)
        self.assertEqual({"dur": 12.5, "w": 1280, "h": 720}, meta)

    def test_clip_thumbnail_uses_same_bounded_launcher_without_capture(self):
        clip_root = os.path.join(textvideo.CLIP_ROOT, "2")
        os.makedirs(clip_root, exist_ok=True)
        path = os.path.join(
            clip_root,
            "c_0123456789abcdef0123456789abcdef.mp4",
        )
        thumb = path + ".jpg"
        with open(path, "wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypmp42video-fixture")
        ffmpeg = os.path.join(self.tmp.name, "ffmpeg")
        with open(ffmpeg, "wb") as handle:
            handle.write(b"test executable")
        os.chmod(ffmpeg, 0o700)

        def bounded_thumbnail(command, **kwargs):
            self.assertEqual(os.path.realpath(sys.executable), command[0])
            self.assertEqual(
                [
                    "-m",
                    "app.textvideo",
                    "--bounded-media-exec",
                    "thumbnail",
                    os.path.realpath(ffmpeg),
                ],
                command[1:6],
            )
            self.assertIn("-nostdin", command)
            self.assertNotIn("capture_output", kwargs)
            self.assertNotIn("preexec_fn", kwargs)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertLessEqual(kwargs["timeout"], 60)
            with open(thumb, "wb") as handle:
                handle.write(b"thumbnail")
            return mock.Mock(returncode=0)

        with mock.patch.dict(
            os.environ,
            {"CONTENTCREW_FFMPEG_PATH": ffmpeg},
        ), mock.patch.object(
            textvideo.subprocess,
            "run",
            side_effect=bounded_thumbnail,
        ):
            self.assertTrue(textvideo.make_thumb(path, thumb, 2.0))
        self.assertTrue(os.path.isfile(thumb))

    def test_media_worker_sets_limits_and_drops_parent_secrets_before_exec(self):
        binary = os.path.join(self.tmp.name, "media-binary")
        with open(binary, "wb") as handle:
            handle.write(b"test executable")
        os.chmod(binary, 0o700)
        with mock.patch(
            "resource.setrlimit",
        ) as set_limit, mock.patch.object(
            textvideo.os,
            "execve",
            side_effect=RuntimeError("exec intercepted"),
        ) as execute, mock.patch.dict(
            os.environ,
            {"SHOULD_NOT_REACH_MEDIA_PARSER": "sensitive"},
        ):
            with self.assertRaisesRegex(RuntimeError, "exec intercepted"):
                textvideo._bounded_media_worker([
                    "probe",
                    binary,
                    "-v",
                    "error",
                ])
        self.assertGreaterEqual(set_limit.call_count, 5)
        executable, arguments, environment = execute.call_args.args
        self.assertEqual(os.path.realpath(binary), executable)
        self.assertEqual(os.path.realpath(binary), arguments[0])
        self.assertNotIn("SHOULD_NOT_REACH_MEDIA_PARSER", environment)
        self.assertEqual(
            {"PATH", "LANG", "LC_ALL", "HOME"},
            set(environment),
        )

    def test_clip_parser_timeout_cleans_files_and_releases_upload_slot(self):
        upload = self._upload(
            "clip.mp4",
            b"\x00\x00\x00\x18ftypmp42video-fixture",
            "video/mp4",
        )
        with mock.patch.object(
            textvideo,
            "probe_clip",
            side_effect=subprocess.TimeoutExpired("ffprobe", 15),
        ), self.assertRaises(HTTPException) as caught:
            asyncio.run(self.main.tv_clip_upload(upload))
        self.assertEqual(400, caught.exception.status_code)
        self.assertNotIn(2, self.main._persistent_upload_active_tenants)
        clip_root = os.path.join(textvideo.CLIP_ROOT, "2")
        self.assertEqual(
            [],
            os.listdir(clip_root) if os.path.isdir(clip_root) else [],
        )

    def test_clip_list_ignores_hidden_temporary_and_unmanaged_files(self):
        clip_root = os.path.join(textvideo.CLIP_ROOT, "2")
        os.makedirs(clip_root, exist_ok=True)
        valid = "c_0123456789abcdef0123456789abcdef.mp4"
        for name in (valid, ".clip-orphan.part", "notes.txt"):
            with open(os.path.join(clip_root, name), "wb") as handle:
                handle.write(b"fixture")
        with mock.patch.object(
            textvideo,
            "probe_clip",
            return_value={"dur": 8.0, "w": 720, "h": 1280},
        ) as probe:
            rows = self.main.tv_clips_list()
        self.assertEqual([valid], [row["name"] for row in rows])
        probe.assert_called_once_with(os.path.join(clip_root, valid))

    def test_clip_delete_rejects_active_same_tenant_reference(self):
        clip_root = os.path.join(textvideo.CLIP_ROOT, "2")
        os.makedirs(clip_root, exist_ok=True)
        name = "c_0123456789abcdef0123456789abcdef.mp4"
        path = os.path.join(clip_root, name)
        with open(path, "wb") as handle:
            handle.write(b"fixture")
        db.insert("tv_job", {
            "tenant_id": 2,
            "params_json": json.dumps(
                {"mode": "clips", "clips": [path]},
                ensure_ascii=False,
            ),
            "status": "queued",
            "billing_status": "charged",
            "billing_points": 3,
        })

        with self.assertRaises(HTTPException) as caught:
            self.main.tv_clip_delete(name)
        self.assertEqual(409, caught.exception.status_code)
        self.assertTrue(os.path.isfile(path))

    def test_clip_create_and_delete_share_reference_lock(self):
        clip_root = os.path.join(textvideo.CLIP_ROOT, "2")
        os.makedirs(clip_root, exist_ok=True)
        name = "c_0123456789abcdef0123456789abcdef.mp4"
        path = os.path.join(clip_root, name)
        with open(path, "wb") as handle:
            handle.write(b"fixture")
        create_entered = threading.Event()
        allow_insert = threading.Event()
        delete_done = threading.Event()
        create_errors = []
        delete_outcome = {}

        def delayed_create(params, tenant_id=None, job_id=None, note=""):
            create_entered.set()
            if not allow_insert.wait(2):
                raise AssertionError("test insert barrier timed out")
            return db.insert("tv_job", {
                "tenant_id": int(tenant_id or 2),
                "job_id": job_id,
                "params_json": json.dumps(params, ensure_ascii=False),
                "status": "queued",
                "billing_status": "charged",
                "billing_points": 3,
            })

        def discard_task(coroutine):
            coroutine.close()
            return mock.Mock()

        def create_job():
            self._as_tenant(2)
            try:
                asyncio.run(self.main.text_video_create({
                    "mode": "clips",
                    "clips": [name],
                    "script": "这是一段用于验证素材引用互斥的完整口播文案。",
                    "title": "并发验收",
                }))
            except BaseException as exc:
                create_errors.append(exc)

        def delete_clip():
            self._as_tenant(2)
            try:
                delete_outcome["result"] = self.main.tv_clip_delete(name)
            except HTTPException as exc:
                delete_outcome["status"] = exc.status_code
            finally:
                delete_done.set()

        with mock.patch.object(
            self.main,
            "_create_charged_tv_job",
            side_effect=delayed_create,
        ), mock.patch.object(
            self.main,
            "_start_text_video_worker",
        ):
            creator = threading.Thread(target=create_job)
            creator.start()
            self.assertTrue(create_entered.wait(2))
            deleter = threading.Thread(target=delete_clip)
            deleter.start()
            time.sleep(0.05)
            self.assertFalse(
                delete_done.is_set(),
                "delete must wait until clip references are persisted",
            )
            allow_insert.set()
            creator.join(2)
            deleter.join(2)

        self.assertFalse(creator.is_alive())
        self.assertFalse(deleter.is_alive())
        self.assertEqual([], create_errors)
        self.assertEqual(409, delete_outcome.get("status"))
        self.assertTrue(os.path.isfile(path))


if __name__ == "__main__":
    unittest.main()
