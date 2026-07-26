"""数字人摄影棚(V6):口播稿 → 海螺TTS配音 → 可灵驱动照片对口型 → 数字人视频.

全链路走云雾API:
- 配音: POST {yunwu}/minimax/v1/t2a_v2 (MiniMax speech-2.8-hd,系统音色;声音克隆待云雾上传接口开通)
- 数字人: POST {yunwu}/kling/v1/videos/avatar/image2video {image, sound_file, mode}
  轮询 GET .../{task_id} 直到 succeed,下载成片。
- 可灵需要公网可访问的图片/音频 URL:素材存 data/public/,由 contentcrew-public
  服务(0.0.0.0:8901,随机文件名即访问凭证)对外提供。
"""
import asyncio
import io
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import warnings
import wave

import httpx

from . import db, netfetch, providers, secureconfig

log = logging.getLogger("avatar")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.environ.get(
    "CONTENTCREW_PUBLIC_DIR",
    os.path.join(ROOT, "data", "public"),
)
OUT_DIR = os.path.join(ROOT, "data", "assets", "avatar")
os.makedirs(PUBLIC_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

MAX_REMOTE_AUDIO_BYTES = 25 * 1024 * 1024
MAX_REMOTE_VIDEO_BYTES = 512 * 1024 * 1024
MAX_FREE_RETRIES = 3
MAX_TENANT_ASSET_BYTES = 512 * 1024 * 1024
MAX_TENANT_ASSET_FILES = 120
MAX_UPLOAD_IMAGE_PIXELS = 20_000_000
MAX_UPLOAD_IMAGE_DIMENSION = 8192
_ASSET_STORE_LOCK = threading.RLock()


class InvalidAvatarMedia(ValueError):
    """The uploaded bytes do not match a supported, parseable media type."""


class AssetQuotaExceeded(ValueError):
    """The tenant has no safely reclaimable public-asset capacity."""


def asset_library_lock(_tid: int | None = None):
    """Return the process-wide registry lock for an atomic asset→job handoff."""
    return _ASSET_STORE_LOCK

VOICES = [
    {"id": "male-qn-qingse", "label": "青涩男声(阳光少年)"},
    {"id": "male-qn-jingying", "label": "精英男声(干练商务)"},
    {"id": "presenter_male", "label": "主持男声(播音腔)"},
    {"id": "female-shaonv", "label": "少女音(活泼)"},
    {"id": "female-yujie", "label": "御姐音(沉稳)"},
    {"id": "presenter_female", "label": "主持女声(播音腔)"},
    {"id": "audiobook_male_1", "label": "有声书男声(讲述感)"},
    {"id": "audiobook_female_1", "label": "有声书女声(温柔)"},
]


def public_base() -> str:
    return (db.get_setting("public_base") or "https://paihuo.ai/pub").rstrip("/")


def rh_ready() -> bool:
    from . import runninghub
    return runninghub.ready()


def _cap_audio(path: str, max_sec: int) -> str:
    """在提交任一供应商前把音频硬截到已计费时长档（最长 60 秒）。"""
    cap = max(1, min(int(max_sec), 60))
    ff = os.environ.get("CONTENTCREW_FFMPEG_PATH") or shutil.which("ffmpeg")
    if not ff or not os.path.isfile(ff) or not os.access(ff, os.X_OK):
        raise providers.ProviderError("服务器缺少 ffmpeg，无法安全限制数字人音频时长")
    os.makedirs(PUBLIC_DIR, mode=0o750, exist_ok=True)
    name = f"{uuid.uuid4().hex}.mp3"
    out = os.path.join(PUBLIC_DIR, name)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".audio-cap-",
        suffix=".mp3",
        dir=PUBLIC_DIR,
    )
    os.close(descriptor)
    try:
        from . import textvideo

        command = textvideo._bounded_media_command(
            "audio",
            ff,
            [
                "-nostdin",
                "-y",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-threads",
                "1",
                "-i",
                path,
                "-t",
                str(cap),
                "-threads",
                "1",
                temporary,
            ],
        )
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
            cwd=ROOT,
        )
        if (
            result.returncode != 0
            or not os.path.exists(temporary)
            or os.path.getsize(temporary) <= 0
            or os.path.getsize(temporary) > 8 * 1024 * 1024
        ):
            raise providers.ProviderError("数字人音频时长校验失败")
        os.chmod(temporary, 0o640)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, out)
        temporary = ""
        return out
    except providers.ProviderError:
        raise
    except Exception as exc:
        raise providers.ProviderError("数字人音频时长校验失败") from exc
    finally:
        if temporary:
            try:
                os.remove(temporary)
            except OSError:
                pass


def engine_name() -> str:
    """数字人引擎:配了 HeyGen key 且额度未耗尽走 HeyGen(会动),否则可灵."""
    forced = db.get_setting("avatar_engine")
    hg_ok = bool(secureconfig.get_secret("heygen_key")) and not db.get_setting(
        "heygen_exhausted"
    )
    if forced in ("heygen", "kling"):
        if forced == "heygen" and not hg_ok:
            return "kling"
        return forced
    return "heygen" if hg_ok else "kling"


class Cancelled(Exception):
    pass


def cancelled(job_id: int) -> bool:
    r = db.one(
        "SELECT status,deleted_at FROM avatar_job WHERE id=?", (job_id,)
    )
    return (
        not r
        or r.get("deleted_at") is not None
        or r["status"] in {"cancelled", "failed"}
    )


# ---------------- HeyGen 引擎(v2 API,2026-10-31 前有效) ----------------
HEYGEN_API = "https://api.heygen.com"
HEYGEN_UPLOAD = "https://upload.heygen.com"


def _hg_headers():
    return {"X-Api-Key": secureconfig.get_secret("heygen_key")}


HG_PHOTO_REGISTRY = "heygen_photo_registry"
HG_REGISTRY_MAX = 60


def _hg_registry() -> list:
    return db.jloads(db.get_setting(HG_PHOTO_REGISTRY), []) or []


def hg_register_photo(photo_id: str, tenant_id, job_id) -> None:
    """记下我们上传的照片数字人属于哪个租户的哪个任务。

    HeyGen 的 API key 是平台级的,所有租户共用同一批照片槽位。没有这份归属登记,
    清理时就分不清哪个组是谁的——那正是此前"清理即删光全平台数字人"的根因。
    """
    if not photo_id:
        return
    entries = [e for e in _hg_registry() if e.get("id") != photo_id]
    entries.append({
        "id": str(photo_id),
        "tenant_id": tenant_id,
        "job_id": job_id,
        "ts": time.time(),
    })
    db.set_setting(
        HG_PHOTO_REGISTRY,
        json.dumps(entries[-HG_REGISTRY_MAX:], ensure_ascii=False),
    )


def _hg_active_job_ids() -> set:
    rows = db.q(
        "SELECT id FROM avatar_job WHERE status IN ('queued','running')"
    )
    return {r["id"] for r in rows}


async def hg_cleanup_photos() -> int:
    """释放照片槽位:只删我们登记过、且其任务已结束的旧组。

    绝不碰未登记的组——它们可能属于其他租户正在使用的数字人,或是老板在 HeyGen
    后台手工创建的形象。没有可安全删除的对象时返回 0,由调用方给出明确提示,
    而不是靠删别人的东西来腾位置。
    """
    registry = _hg_registry()
    if not registry:
        log.info("heygen cleanup skipped: no owned photo avatars recorded")
        return 0
    active = _hg_active_job_ids()
    # 任务仍在排队/执行中的照片不能删,否则会打断在途成片。
    candidates = [e for e in registry if e.get("job_id") not in active]
    if not candidates:
        log.info("heygen cleanup skipped: all owned photo avatars are in use")
        return 0
    candidates.sort(key=lambda e: e.get("ts") or 0)     # 最旧的先释放
    removed: set = set()
    try:
        async with httpx.AsyncClient(timeout=60) as cli:
            for entry in candidates:
                gid = entry.get("id")
                if not gid:
                    continue
                for path in (f"{HEYGEN_API}/v2/photo_avatar/{gid}",
                             f"{HEYGEN_API}/v2/avatar_group/{gid}"):
                    try:
                        rr = await cli.delete(path, headers=_hg_headers())
                        if rr.status_code == 200 and (rr.json().get("code") == 100):
                            removed.add(gid)
                            break
                    except Exception:
                        pass
                if removed:
                    break                    # 腾出一个槽位就够本次使用
    except Exception as exc:
        log.warning("heygen cleanup failed error_type=%s", type(exc).__name__)
    if removed:
        db.set_setting(
            HG_PHOTO_REGISTRY,
            json.dumps([e for e in _hg_registry() if e.get("id") not in removed],
                       ensure_ascii=False),
        )
    log.info("heygen released %d owned photo avatar slot(s)", len(removed))
    return len(removed)


async def hg_upload_talking_photo(photo_path: str, tenant_id=None, job_id=None,
                                  _retry: bool = True) -> str:
    ext = os.path.splitext(photo_path)[1].lower()
    ctype = "image/png" if ext == ".png" else "image/jpeg"
    with open(photo_path, "rb") as f:
        data = f.read()
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(f"{HEYGEN_UPLOAD}/v1/talking_photo",
                           headers={**_hg_headers(), "Content-Type": ctype}, content=data)
        d = r.json()
        tid = ((d.get("data") or {}).get("talking_photo_id")) or ((d.get("data") or {}).get("id"))
        if tid:
            hg_register_photo(tid, tenant_id, job_id)
            return tid
        blob = json.dumps(d).lower()
        if "exceeded" in blob or "limit" in blob or "401028" in blob:
            # 照片槽位满(HeyGen 只允许3个照片数字人)——只回收我们自己且已结束任务的组。
            if _retry and await hg_cleanup_photos():
                return await hg_upload_talking_photo(
                    photo_path, tenant_id, job_id, _retry=False
                )
            raise providers.ProviderError(
                "HeyGen 照片槽位已满,且没有可安全回收的旧数字人"
                "(其余槽位仍被进行中的任务占用)。请稍后重试,"
                "或到 app.heygen.com 手动删除不再需要的照片数字人"
            )
        if "credit" in blob or "insufficient" in blob:
            # 真·余额不足才标记耗尽,走可灵兜底
            db.set_setting("heygen_exhausted", "1")
            raise providers.ProviderError("HeyGen 余额不足,请充值")
        raise providers.ProviderError("HeyGen 照片上传失败，请稍后重试")


async def hg_upload_audio(audio_path: str) -> str:
    ext = os.path.splitext(audio_path)[1].lower()
    ctype = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav"}.get(ext.lstrip("."), "audio/mpeg")
    with open(audio_path, "rb") as f:
        data = f.read()
    async with httpx.AsyncClient(timeout=180) as cli:
        r = await cli.post(f"{HEYGEN_UPLOAD}/v1/asset",
                           headers={**_hg_headers(), "Content-Type": ctype}, content=data)
        d = r.json()
        aid = ((d.get("data") or {}).get("id")) or ((d.get("data") or {}).get("asset_id"))
        if not aid:
            raise providers.ProviderError("HeyGen 音频上传失败，请稍后重试")
        return aid


async def hg_voice_id() -> str:
    """默认中文音色:老板可在设置 heygen_voice_id 覆盖;否则取声音列表里第一个中文音色."""
    vid = db.get_setting("heygen_voice_id")
    if vid:
        return vid
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.get(f"{HEYGEN_API}/v2/voices", headers=_hg_headers())
        for v in ((r.json().get("data") or {}).get("voices")) or []:
            lang = (v.get("language") or "").lower()
            if "chinese" in lang or "zh" in lang or "mandarin" in lang:
                return v.get("voice_id")
    raise providers.ProviderError("HeyGen 没找到中文音色,请在管理后台设置 heygen_voice_id")


async def hg_generate(talking_photo_id: str, script: str, audio_asset_id: str = None,
                      portrait: bool = True) -> str:
    """提交 HeyGen 生成(Avatar IV 动作引擎),返回 video_id."""
    voice = ({"type": "audio", "audio_asset_id": audio_asset_id} if audio_asset_id
             else {"type": "text", "input_text": script[:1500], "voice_id": await hg_voice_id()})
    body = {"video_inputs": [{
                "character": {"type": "talking_photo", "talking_photo_id": talking_photo_id},
                "voice": voice}],
            "dimension": {"width": 720, "height": 1280} if portrait
            else {"width": 1280, "height": 720},
            "use_avatar_iv_model": True, "title": "BossAI 数字人"}
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(f"{HEYGEN_API}/v2/video/generate",
                           headers={**_hg_headers(), "Content-Type": "application/json"},
                           json=body)
        d = r.json()
        vid = (d.get("data") or {}).get("video_id")
        if not vid:
            raise providers.ProviderError("HeyGen 任务提交失败，请稍后重试")
        return vid


async def hg_poll(video_id: str, progress, timeout: int = 1800, job_id: int = None) -> str:
    t0 = time.time()
    async with httpx.AsyncClient(timeout=60) as cli:
        while time.time() - t0 < timeout:
            await asyncio.sleep(15)
            if job_id and cancelled(job_id):
                raise Cancelled()
            r = await cli.get(f"{HEYGEN_API}/v1/video_status.get",
                              headers=_hg_headers(), params={"video_id": video_id})
            d = (r.json().get("data") or {})
            st = d.get("status", "")
            if st == "completed":
                return d.get("video_url")
            if st == "failed":
                raise providers.ProviderError("HeyGen 生成失败，请稍后重试")
            safe_state = st if st in {"pending", "waiting", "processing"} else "排队"
            progress("tool", f"HeyGen 渲染中({safe_state})…已等 {int(time.time() - t0)}s")
    raise providers.ProviderError("HeyGen 渲染超时(30分钟)")


def _probe_media_bytes(data: bytes) -> dict:
    probe = (
        os.environ.get("CONTENTCREW_FFPROBE_PATH")
        or shutil.which("ffprobe")
        or ""
    )
    if not probe or not os.path.isfile(probe) or not os.access(probe, os.X_OK):
        raise InvalidAvatarMedia("服务器缺少媒体解析器，暂时无法安全接收此格式")
    from . import textvideo

    command = textvideo._bounded_media_command(
        "probe",
        probe,
        [
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-probesize",
            "5000000",
            "-analyzeduration",
            "5000000",
            "-show_entries",
            "stream=codec_type,width,height,duration:"
            "format=duration,format_name",
            "-of",
            "json",
            "pipe:0",
        ],
    )
    try:
        with tempfile.TemporaryFile(mode="w+b") as output:
            result = subprocess.run(
                command,
                input=data,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                cwd=ROOT,
            )
            output.seek(0)
            raw = output.read(1024 * 1024 + 1)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidAvatarMedia("媒体文件解析失败或超时") from exc
    if result.returncode != 0 or not raw or len(raw) > 1024 * 1024:
        raise InvalidAvatarMedia("媒体文件无法解析")
    try:
        payload = json.loads(raw.decode("utf-8", "strict"))
    except (TypeError, ValueError) as exc:
        raise InvalidAvatarMedia("媒体文件解析结果无效") from exc
    return payload if isinstance(payload, dict) else {}


def validate_upload_media(data: bytes, ext: str, kind: str) -> None:
    """Validate actual media bytes, not the client filename or Content-Type."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise InvalidAvatarMedia("上传文件为空")
    data = bytes(data)
    ext = str(ext or "").lower()

    if kind == "photo":
        expected = {
            ".jpg": "JPEG",
            ".jpeg": "JPEG",
            ".png": "PNG",
            ".webp": "WEBP",
        }.get(ext)
        if not expected:
            raise InvalidAvatarMedia("图片格式不受支持")
        from PIL import Image
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as source:
                    width, height = source.size
                    frames = int(getattr(source, "n_frames", 1) or 1)
                    if (source.format or "").upper() != expected:
                        raise InvalidAvatarMedia("图片内容与文件扩展名不一致")
                    if (
                        width <= 0
                        or height <= 0
                        or width > MAX_UPLOAD_IMAGE_DIMENSION
                        or height > MAX_UPLOAD_IMAGE_DIMENSION
                        or width * height > MAX_UPLOAD_IMAGE_PIXELS
                        or frames != 1
                    ):
                        raise InvalidAvatarMedia("图片尺寸、像素量或帧数超过安全限制")
                    source.load()
        except InvalidAvatarMedia:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            ValueError,
        ) as exc:
            raise InvalidAvatarMedia("图片文件无法解析") from exc
        return

    if kind == "voice" and ext == ".wav":
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise InvalidAvatarMedia("录音内容与 WAV 扩展名不一致")
        try:
            with wave.open(io.BytesIO(data), "rb") as source:
                frames = source.getnframes()
                rate = source.getframerate()
                channels = source.getnchannels()
                width = source.getsampwidth()
                if (
                    frames <= 0
                    or rate <= 0
                    or channels not in (1, 2)
                    or width not in (1, 2, 3, 4)
                    or frames / rate > 600
                ):
                    raise InvalidAvatarMedia("录音参数或时长超过安全限制")
                source.readframes(frames)
        except InvalidAvatarMedia:
            raise
        except (EOFError, wave.Error) as exc:
            raise InvalidAvatarMedia("WAV 录音无法解析") from exc
        return

    if kind == "voice":
        if ext == ".mp3":
            if not (
                data.startswith(b"ID3")
                or (
                    len(data) >= 2
                    and data[0] == 0xFF
                    and data[1] & 0xE0 == 0xE0
                )
            ):
                raise InvalidAvatarMedia("录音内容与 MP3 扩展名不一致")
        elif ext == ".m4a":
            if len(data) < 12 or data[4:8] != b"ftyp":
                raise InvalidAvatarMedia("录音内容与 M4A 扩展名不一致")
        else:
            raise InvalidAvatarMedia("录音格式不受支持")
        probe = _probe_media_bytes(data)
        if not any(
            stream.get("codec_type") == "audio"
            for stream in (probe.get("streams") or [])
            if isinstance(stream, dict)
        ):
            raise InvalidAvatarMedia("录音文件没有可解析的音轨")
        return

    if kind == "video":
        if ext not in {".mp4", ".mov"}:
            raise InvalidAvatarMedia("视频格式不受支持")
        if len(data) < 12 or data[4:8] != b"ftyp":
            raise InvalidAvatarMedia("视频内容与文件扩展名不一致")
        probe = _probe_media_bytes(data)
        videos = [
            stream
            for stream in (probe.get("streams") or [])
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ]
        if not videos:
            raise InvalidAvatarMedia("视频文件没有可解析的画面轨")
        width = int(videos[0].get("width") or 0)
        height = int(videos[0].get("height") or 0)
        if (
            width <= 0
            or height <= 0
            or width > MAX_UPLOAD_IMAGE_DIMENSION
            or height > MAX_UPLOAD_IMAGE_DIMENSION
        ):
            raise InvalidAvatarMedia("视频尺寸超过安全限制")
        return

    raise InvalidAvatarMedia("素材类型不受支持")


_PUBLIC_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".mp3", ".m4a", ".wav", ".mp4", ".mov",
}


def _reserved_public_name(ext: str) -> str:
    ext = str(ext or "").lower()
    if ext not in _PUBLIC_MEDIA_EXTENSIONS:
        raise ValueError("invalid public media extension")
    return f"{uuid.uuid4().hex}{ext}"


def _write_public_bytes(
    data: bytes,
    ext: str,
    *,
    reserved_name: str = "",
) -> dict:
    ext = str(ext or "").lower()
    if ext not in _PUBLIC_MEDIA_EXTENSIONS:
        raise ValueError("invalid public media extension")
    if reserved_name:
        expected = re.compile(
            rf"^[0-9a-f]{{32}}{re.escape(ext)}$",
            re.I,
        )
        if (
            reserved_name != os.path.basename(reserved_name)
            or not expected.fullmatch(reserved_name)
        ):
            raise ValueError("invalid reserved public media name")
        name = reserved_name
    else:
        name = _reserved_public_name(ext)
    base = public_base()
    os.makedirs(PUBLIC_DIR, mode=0o750, exist_ok=True)
    target = os.path.join(PUBLIC_DIR, name)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".upload-", suffix=".part", dir=PUBLIC_DIR
    )
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
        raise
    return {"name": name, "url": f"{base}/{name}"}


def save_public(data: bytes, ext: str) -> dict:
    """Atomically publish provider-generated media; uploads use quota wrapper."""
    return _write_public_bytes(data, ext)


async def _download_supplier_audio(url: str) -> bytes:
    try:
        return await netfetch.fetch_public_media(
            url,
            kind="audio",
            max_bytes=MAX_REMOTE_AUDIO_BYTES,
            timeout=120,
        )
    except (ValueError, httpx.HTTPError) as exc:
        raise providers.ProviderError("供应商音频下载失败，请稍后重试") from exc


_ASSET_NAME_RE = re.compile(
    r"^[0-9a-f]{32}\.(?:jpg|jpeg|png|webp|mp3|m4a|wav|mp4|mov)$",
    re.I,
)
_ASSET_KINDS = {"photo", "voice", "video", "generated"}


def _asset_key(tid: int) -> str:
    return f"avatar_assets:{int(tid)}"


def saved_assets(tid: int = None) -> list:
    from . import auth
    tid = int(tid or auth.tenant_id())
    items = db.jloads(db.get_setting(_asset_key(tid)), []) or []
    return [
        x for x in items
        if isinstance(x, dict) and _ASSET_NAME_RE.fullmatch(x.get("name") or "")
    ]


def _saved_photos_for_tenant(tid: int) -> list:
    items = db.jloads(db.get_setting(f"avatar_photos:{int(tid)}"), []) or []
    return [
        item for item in items
        if isinstance(item, dict)
        and _ASSET_NAME_RE.fullmatch(item.get("name") or "")
    ]


def _job_reference_names(tid: int) -> set[str]:
    names: set[str] = set()
    for row in db.q(
        "SELECT params_json,audio_file FROM avatar_job WHERE tenant_id=?",
        (int(tid),),
    ):
        params = db.jloads(row.get("params_json"), {}) or {}
        if isinstance(params, dict):
            for key in ("photo_name", "own_audio_name"):
                name = os.path.basename(str(params.get(key) or ""))
                if name:
                    names.add(name)
        audio_name = os.path.basename(str(row.get("audio_file") or ""))
        if audio_name:
            names.add(audio_name)
    return names


def _name_referenced_by_any_job(name: str) -> bool:
    if not name or name != os.path.basename(name):
        return True
    suffix = "%/" + name
    row = db.one(
        "SELECT id FROM avatar_job WHERE "
        "(CASE WHEN json_valid(params_json) "
        "THEN json_extract(params_json,'$.photo_name') END)=? OR "
        "(CASE WHEN json_valid(params_json) "
        "THEN json_extract(params_json,'$.own_audio_name') END)=? OR "
        "audio_file LIKE ? ESCAPE '\\' LIMIT 1",
        (name, name, suffix),
    )
    return bool(row)


def _registered_tenants(name: str) -> set[int]:
    owners: set[int] = set()
    for row in db.q(
        "SELECT key,value FROM app_setting "
        "WHERE key LIKE 'avatar_assets:%' OR key LIKE 'avatar_photos:%'"
    ):
        try:
            tid = int(row["key"].rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
        items = db.jloads(row.get("value"), []) or []
        if any(
            isinstance(item, dict) and item.get("name") == name
            for item in items
        ):
            owners.add(tid)
    return owners


def _public_file_path(name: str) -> str | None:
    if not name or name != os.path.basename(name):
        return None
    candidate = os.path.join(PUBLIC_DIR, name)
    if os.path.islink(candidate):
        return None
    root = os.path.realpath(PUBLIC_DIR)
    resolved = os.path.realpath(candidate)
    try:
        inside = os.path.commonpath((root, resolved)) == root
    except ValueError:
        inside = False
    return resolved if inside and os.path.isfile(resolved) else None


def tenant_asset_usage(tid: int) -> dict:
    """Return only public files attributable to one tenant."""
    tid = int(tid)
    # Use the same lock as every registry read/modify/write operation.  Without
    # it a quota snapshot can race an upload/delete and under-count a file that
    # is already durable on disk.
    with _ASSET_STORE_LOCK:
        names = {
            item["name"] for item in saved_assets(tid)
        } | {
            item["name"] for item in _saved_photos_for_tenant(tid)
        } | _job_reference_names(tid)
        inode_sizes = _private_transaction_inode_sizes(tid)
        for path in (
            path for path in (_public_file_path(name) for name in names) if path
        ):
            metadata = os.stat(path, follow_symlinks=False)
            inode_sizes[(metadata.st_dev, metadata.st_ino)] = max(
                inode_sizes.get((metadata.st_dev, metadata.st_ino), 0),
                int(metadata.st_size),
            )
        return {
            "files": len(inode_sizes),
            "bytes": sum(inode_sizes.values()),
            "names": names,
        }


def _can_reclaim(name: str, tid: int) -> bool:
    if _name_referenced_by_any_job(name):
        return False
    owners = _registered_tenants(name)
    return bool(owners) and owners <= {int(tid)}


def _delete_public_if_unreferenced(name: str, tid: int) -> bool:
    """Delete only a sole-tenant, unreferenced regular public file."""
    if _name_referenced_by_any_job(name):
        return False
    if _registered_tenants(name) - {int(tid)}:
        return False
    path = _public_file_path(name)
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _plan_asset_capacity(
    tid: int,
    incoming_bytes: int,
    *,
    exclude_name: str = "",
) -> tuple[list, list, list[str]]:
    """Evict oldest safe assets until one new file fits, without mutating state."""
    tid = int(tid)
    incoming_bytes = max(0, int(incoming_bytes))
    assets = [
        item for item in saved_assets(tid)
        if item.get("name") != exclude_name
    ]
    photos = [
        item for item in _saved_photos_for_tenant(tid)
        if item.get("name") != exclude_name
    ]
    references = _job_reference_names(tid)
    names = {
        item["name"] for item in assets
    } | {
        item["name"] for item in photos
    } | references

    def usage():
        inode_sizes = _private_transaction_inode_sizes(tid)
        for path in (
            path
            for path in (_public_file_path(name) for name in names)
            if path
        ):
            metadata = os.stat(path, follow_symlinks=False)
            inode_sizes[(metadata.st_dev, metadata.st_ino)] = max(
                inode_sizes.get((metadata.st_dev, metadata.st_ino), 0),
                int(metadata.st_size),
            )
        return len(inode_sizes), sum(inode_sizes.values())

    candidates = {}
    for item in assets + photos:
        name = item.get("name")
        if not name or name in references:
            continue
        stamp = float(item.get("ts") or 0)
        candidates[name] = min(stamp, candidates.get(name, stamp))
    ordered = sorted(candidates, key=lambda name: (candidates[name], name))
    removed: list[str] = []
    while True:
        file_count, byte_count = usage()
        if (
            file_count + 1 <= int(MAX_TENANT_ASSET_FILES)
            and byte_count + incoming_bytes <= int(MAX_TENANT_ASSET_BYTES)
        ):
            break
        reclaim = next(
            (
                name for name in ordered
                if name in names and _can_reclaim(name, tid)
            ),
            None,
        )
        if not reclaim:
            raise AssetQuotaExceeded(
                "数字人素材空间已满；请删除不再使用的素材后重试"
            )
        names.discard(reclaim)
        removed.append(reclaim)
        assets = [item for item in assets if item.get("name") != reclaim]
        photos = [item for item in photos if item.get("name") != reclaim]
    return assets, photos, removed


def _save_asset_lists(tid: int, assets: list, photos: list) -> None:
    db.set_setting(
        _asset_key(tid),
        json.dumps(assets, ensure_ascii=False),
    )
    db.set_setting(
        f"avatar_photos:{int(tid)}",
        json.dumps(photos, ensure_ascii=False),
    )


def _restore_named_items(target: list, source: list, names: set[str]) -> list:
    """Restore failed-to-delete registry rows without duplicating names."""
    restored = list(target)
    present = {
        item.get("name")
        for item in restored
        if isinstance(item, dict)
    }
    for item in source:
        name = item.get("name") if isinstance(item, dict) else ""
        if name in names and name not in present:
            restored.append(item)
            present.add(name)
    return restored


def _commit_asset_reclamation(
    tid: int,
    *,
    provisional_assets: list,
    provisional_photos: list,
    target_assets: list,
    target_photos: list,
    removed: list[str],
) -> tuple[list, list]:
    """Keep every durable file attributed until its physical delete succeeds.

    The provisional registry is committed first.  Failed physical deletions
    are restored into the final registry, so a filesystem error can consume
    quota but can never create an unaccounted public orphan.
    """
    tid = int(tid)
    with db.atomic():
        _save_asset_lists(tid, provisional_assets, provisional_photos)

    removed_names = list(dict.fromkeys(
        name for name in removed if _ASSET_NAME_RE.fullmatch(name or "")
    ))
    deleted = {
        name
        for name in removed_names
        if _delete_public_if_unreferenced(name, tid)
    }
    failed = set(removed_names) - deleted
    final_assets = _restore_named_items(
        target_assets,
        provisional_assets,
        failed,
    )
    final_photos = _restore_named_items(
        target_photos,
        provisional_photos,
        failed,
    )
    try:
        with db.atomic():
            _save_asset_lists(tid, final_assets, final_photos)
        return final_assets, final_photos
    except BaseException:
        # The already-committed provisional state remains authoritative.  It
        # may contain a stale row for a successfully deleted file, but it never
        # loses attribution for a file that is still present.
        log.error(
            "avatar asset registry cleanup failed tenant=%s",
            tid,
        )
        return provisional_assets, provisional_photos


def _fsync_directory(path: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_ASSET_TRANSACTION_RE = re.compile(
    r"^\.avatar-upload-txn-([1-9][0-9]*)-[A-Za-z0-9_-]{6,}$"
)
_ASSET_TRANSACTION_MANIFEST = ".manifest.json"
_ASSET_TRANSACTION_PAYLOAD = ".payload"
_ASSET_TRANSACTION_MAX_MANIFEST_BYTES = 1024 * 1024


def _tenant_asset_transaction_dirs(tid: int) -> list[str]:
    tid = int(tid)
    root = os.path.realpath(PUBLIC_DIR)
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return []
    found = []
    for entry in entries:
        match = _ASSET_TRANSACTION_RE.fullmatch(entry.name)
        if match and int(match.group(1)) == tid:
            found.append(os.path.join(root, entry.name))
    return sorted(found)


def _private_transaction_inode_sizes(tid: int) -> dict[object, int]:
    """Account every regular private inode, pessimistically on scan errors."""
    sizes: dict[object, int] = {}
    for transaction_dir in _tenant_asset_transaction_dirs(tid):
        try:
            if os.path.islink(transaction_dir):
                raise OSError("transaction directory is a symlink")
            for root, directories, files in os.walk(
                transaction_dir,
                followlinks=False,
            ):
                if any(
                    os.path.islink(os.path.join(root, name))
                    for name in directories
                ):
                    raise OSError("transaction payload contains symlink")
                for name in files:
                    path = os.path.join(root, name)
                    metadata = os.stat(path, follow_symlinks=False)
                    if stat.S_ISREG(metadata.st_mode):
                        sizes[(metadata.st_dev, metadata.st_ino)] = max(
                            sizes.get(
                                (metadata.st_dev, metadata.st_ino),
                                0,
                            ),
                            int(metadata.st_size),
                        )
        except OSError:
            # An unreadable/ambiguous transaction must fill the logical quota
            # rather than disappear from accounting.
            sizes[("unknown", transaction_dir)] = int(
                MAX_TENANT_ASSET_BYTES
            )
    return sizes


def _cleanup_asset_transaction(transaction_dir: str) -> bool:
    """Remove one private upload transaction without following an alias."""
    if not transaction_dir:
        return False
    root = os.path.realpath(PUBLIC_DIR)
    candidate = os.path.realpath(transaction_dir)
    try:
        inside = os.path.commonpath((root, candidate)) == root
    except ValueError:
        inside = False
    if (
        not inside
        or os.path.islink(transaction_dir)
        or not os.path.isdir(transaction_dir)
        or os.path.dirname(candidate) != root
        or candidate != os.path.join(
            root,
            os.path.basename(transaction_dir),
        )
        or not os.path.basename(candidate).startswith(
            ".avatar-upload-txn-"
        )
    ):
        log.error("refused unsafe avatar transaction cleanup")
        return False
    try:
        allowed_root = {
            _ASSET_TRANSACTION_PAYLOAD,
            ".manifest.part",
            _ASSET_TRANSACTION_MANIFEST,
        }
        root_entries = set(os.listdir(candidate))
        if not root_entries <= allowed_root:
            raise OSError("unexpected avatar transaction member")
        payload = os.path.join(
            candidate,
            _ASSET_TRANSACTION_PAYLOAD,
        )
        if os.path.lexists(payload):
            if os.path.islink(payload) or not os.path.isdir(payload):
                raise OSError("unsafe avatar transaction payload")
            allowed_payload = {".incoming"}
            allowed_payload.update(
                item
                for item in os.listdir(payload)
                if re.fullmatch(
                    r"old-[0-9]{4}-"
                    + _ASSET_NAME_RE.pattern[1:-1],
                    item,
                    re.I,
                )
            )
            payload_entries = set(os.listdir(payload))
            if payload_entries != allowed_payload & payload_entries:
                raise OSError("unexpected avatar transaction payload")
            for name in payload_entries:
                path = os.path.join(payload, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    raise OSError("unsafe avatar transaction payload")
            # The manifest intentionally remains durable if this recursive
            # cleanup fails or is interrupted at any point.
            shutil.rmtree(payload)
            _fsync_directory(candidate)
        part = os.path.join(candidate, ".manifest.part")
        if os.path.lexists(part):
            if os.path.islink(part) or not os.path.isfile(part):
                raise OSError("unsafe avatar transaction metadata")
            os.unlink(part)
            _fsync_directory(candidate)
        manifest = os.path.join(
            candidate,
            _ASSET_TRANSACTION_MANIFEST,
        )
        if os.path.lexists(manifest):
            if os.path.islink(manifest) or not os.path.isfile(manifest):
                raise OSError("unsafe avatar transaction metadata")
            # Manifest is removed strictly after every payload member.
            os.unlink(manifest)
        _fsync_directory(candidate)
        os.rmdir(candidate)
        _fsync_directory(root)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        log.error(
            "avatar private transaction cleanup failed",
        )
        return False


def _stage_uploaded_asset(
    data: bytes,
    ext: str,
    reserved_name: str,
    manifest: dict,
) -> tuple[str, str]:
    """Durably stage incoming bytes on the public filesystem, but unreadable.

    The random 0700 directory and 000 incoming file live under ``PUBLIC_DIR``
    only to guarantee same-filesystem link semantics.  The path is never
    returned as a public capability; production Caddy runs as a separate user
    and cannot traverse the private directory.
    """
    ext = str(ext or "").lower()
    if (
        ext not in _PUBLIC_MEDIA_EXTENSIONS
        or not re.fullmatch(
            rf"[0-9a-f]{{32}}{re.escape(ext)}",
            reserved_name or "",
            re.I,
        )
    ):
        raise ValueError("invalid staged public media name")
    os.makedirs(PUBLIC_DIR, mode=0o750, exist_ok=True)
    root = os.path.realpath(PUBLIC_DIR)
    tid = int(manifest.get("tenant_id") or 0)
    if tid <= 0:
        raise ValueError("invalid avatar transaction tenant")
    transaction_dir = tempfile.mkdtemp(
        prefix=f".avatar-upload-txn-{tid}-",
        dir=root,
    )
    payload_dir = os.path.join(
        transaction_dir,
        _ASSET_TRANSACTION_PAYLOAD,
    )
    staged_path = os.path.join(payload_dir, ".incoming")
    descriptor = -1
    try:
        os.chmod(transaction_dir, 0o700)
        os.mkdir(payload_dir, 0o700)
        descriptor = os.open(
            staged_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        incoming_metadata = os.stat(staged_path, follow_symlinks=False)
        manifest = dict(manifest)
        manifest["version"] = 1
        manifest["incoming"] = {
            "dev": int(incoming_metadata.st_dev),
            "ino": int(incoming_metadata.st_ino),
            "size": int(incoming_metadata.st_size),
        }
        _write_asset_transaction_manifest(transaction_dir, manifest)
        os.chmod(staged_path, 0o000)
        _fsync_directory(payload_dir)
        _fsync_directory(root)
        return transaction_dir, staged_path
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        _cleanup_asset_transaction(transaction_dir)
        raise


def _write_asset_transaction_manifest(
    transaction_dir: str,
    manifest: dict,
) -> None:
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _ASSET_TRANSACTION_MAX_MANIFEST_BYTES:
        raise ValueError("avatar transaction manifest is too large")
    target = os.path.join(
        transaction_dir,
        _ASSET_TRANSACTION_MANIFEST,
    )
    temporary = os.path.join(transaction_dir, ".manifest.part")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(transaction_dir)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _prepare_uploaded_asset_evictions(
    tid: int,
    removed: list[str],
) -> list[dict]:
    tid = int(tid)
    root = os.path.realpath(PUBLIC_DIR)
    prepared = []
    removed_names = list(dict.fromkeys(removed))
    if any(
        not _ASSET_NAME_RE.fullmatch(name or "")
        for name in removed_names
    ):
        raise AssetQuotaExceeded("数字人素材空间状态异常，请稍后重试")
    for index, name in enumerate(removed_names):
        if not _can_reclaim(name, tid):
            raise AssetQuotaExceeded(
                "数字人素材空间已满；素材已被使用，请稍后重试"
            )
        expected = os.path.join(root, name)
        path = _public_file_path(name)
        if path != expected or os.path.islink(expected):
            raise AssetQuotaExceeded(
                "数字人素材空间已满；素材路径异常，请稍后重试"
            )
        metadata = os.lstat(expected)
        if not stat.S_ISREG(metadata.st_mode):
            raise AssetQuotaExceeded(
                "数字人素材空间已满；素材路径异常，请稍后重试"
            )
        prepared.append({
            "name": name,
            "backup": f"old-{index:04d}-{name}",
            "mode": stat.S_IMODE(metadata.st_mode),
            "dev": int(metadata.st_dev),
            "ino": int(metadata.st_ino),
            "size": int(metadata.st_size),
        })
    return prepared


def _quarantine_uploaded_asset_evictions(
    tid: int,
    evictions: list[dict],
    transaction_dir: str,
) -> list[dict]:
    """Create durable private hardlinks while every old public name stays live."""
    tid = int(tid)
    root = os.path.realpath(PUBLIC_DIR)
    backed_up = []
    try:
        for item in evictions:
            name = item["name"]
            if not _can_reclaim(name, tid):
                raise OSError("avatar asset is no longer reclaimable")
            expected = os.path.join(root, name)
            path = _public_file_path(name)
            if path != expected or os.path.islink(expected):
                raise OSError("avatar asset path is unsafe")
            metadata = os.lstat(expected)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_dev != int(item["dev"])
                or metadata.st_ino != int(item["ino"])
            ):
                raise OSError("avatar asset inode changed")
            backup_path = os.path.join(
                transaction_dir,
                _ASSET_TRANSACTION_PAYLOAD,
                item["backup"],
            )
            if os.path.lexists(backup_path):
                raise OSError("avatar quarantine target exists")
            os.link(expected, backup_path, follow_symlinks=False)
            backed_up.append(item)
        _fsync_directory(os.path.join(
            transaction_dir,
            _ASSET_TRANSACTION_PAYLOAD,
        ))
        return backed_up
    except BaseException as exc:
        raise AssetQuotaExceeded(
            "数字人素材空间已满；旧素材隔离失败，请稍后重试"
        ) from exc


def _same_manifest_inode(path: str, item: dict) -> bool:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_dev == int(item["dev"])
        and metadata.st_ino == int(item["ino"])
    )


def _finalize_committed_asset_evictions(
    transaction_dir: str,
    evictions: list[dict],
) -> None:
    """After DB commit, unlink only old public names proven by backup inode."""
    root = os.path.realpath(PUBLIC_DIR)
    for item in evictions:
        public_path = os.path.join(root, item["name"])
        backup_path = os.path.join(
            transaction_dir,
            _ASSET_TRANSACTION_PAYLOAD,
            item["backup"],
        )
        if not os.path.lexists(public_path):
            continue
        if (
            not _same_manifest_inode(public_path, item)
            or not _same_manifest_inode(backup_path, item)
        ):
            raise RuntimeError("avatar committed eviction identity mismatch")
        os.unlink(public_path)
    _fsync_directory(root)


def _publish_staged_upload(staged_path: str, reserved_name: str) -> None:
    """Publish the staged inode under a fresh UUID without overwrite."""
    if not _ASSET_NAME_RE.fullmatch(reserved_name or ""):
        raise ValueError("invalid staged public media name")
    root = os.path.realpath(PUBLIC_DIR)
    target = os.path.join(root, reserved_name)
    if os.path.lexists(target):
        raise FileExistsError("public upload target already exists")
    metadata = os.lstat(staged_path)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise OSError("staged upload is not a regular file")
    # link(2) is the portable no-replace publication primitive.  The inode is
    # unreadable until the public name exists, then both links receive 0640.
    os.link(staged_path, target, follow_symlinks=False)
    try:
        os.chmod(target, 0o640)
        _fsync_directory(root)
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


def _unpublish_staged_upload(
    staged_path: str,
    reserved_name: str,
) -> bool:
    """Remove only the public link that still names our staged inode."""
    root = os.path.realpath(PUBLIC_DIR)
    target = os.path.join(root, reserved_name)
    if not os.path.lexists(target):
        return True
    if os.path.islink(target):
        return False
    if not os.path.isfile(staged_path):
        return False
    staged = os.stat(staged_path, follow_symlinks=False)
    published = os.stat(target, follow_symlinks=False)
    if (
        staged.st_dev != published.st_dev
        or staged.st_ino != published.st_ino
    ):
        return False
    os.unlink(target)
    _fsync_directory(root)
    return True


def _load_asset_transaction_manifest(
    transaction_dir: str,
) -> dict:
    root = os.path.realpath(PUBLIC_DIR)
    candidate = os.path.realpath(transaction_dir)
    if (
        os.path.islink(transaction_dir)
        or not os.path.isdir(transaction_dir)
        or os.path.dirname(candidate) != root
        or candidate != os.path.join(
            root,
            os.path.basename(transaction_dir),
        )
    ):
        raise ValueError("invalid avatar transaction directory")
    path = os.path.join(
        transaction_dir,
        _ASSET_TRANSACTION_MANIFEST,
    )
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size
            > _ASSET_TRANSACTION_MAX_MANIFEST_BYTES
        ):
            raise ValueError("invalid avatar transaction manifest")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            manifest = json.loads(handle.read().decode("utf-8"))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    match = _ASSET_TRANSACTION_RE.fullmatch(
        os.path.basename(transaction_dir)
    )
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != 1
        or not match
        or int(manifest.get("tenant_id") or 0)
        != int(match.group(1))
        or not _ASSET_NAME_RE.fullmatch(
            str(manifest.get("new_name") or "")
        )
    ):
        raise ValueError("invalid avatar transaction manifest")
    for key in (
        "original_assets",
        "original_photos",
        "target_assets",
        "target_photos",
    ):
        items = manifest.get(key)
        if (
            not isinstance(items, list)
            or len(items) > 512
            or any(
                not isinstance(item, dict)
                or not _ASSET_NAME_RE.fullmatch(
                    str(item.get("name") or "")
                )
                for item in items
            )
        ):
            raise ValueError("invalid avatar transaction registry")
    incoming = manifest.get("incoming")
    if (
        not isinstance(incoming, dict)
        or any(
            not isinstance(incoming.get(key), int)
            or int(incoming[key]) < 0
            for key in ("dev", "ino", "size")
        )
    ):
        raise ValueError("invalid avatar transaction inode")
    evictions = manifest.get("evictions")
    if not isinstance(evictions, list) or len(evictions) > 512:
        raise ValueError("invalid avatar transaction evictions")
    seen = set()
    for index, item in enumerate(evictions):
        name = item.get("name") if isinstance(item, dict) else ""
        expected_backup = f"old-{index:04d}-{name}"
        if (
            not _ASSET_NAME_RE.fullmatch(str(name or ""))
            or name in seen
            or item.get("backup") != expected_backup
            or any(
                not isinstance(item.get(key), int)
                or int(item[key]) < 0
                for key in ("dev", "ino", "size", "mode")
            )
            or int(item["mode"]) > 0o777
        ):
            raise ValueError("invalid avatar transaction eviction")
        seen.add(name)
    return manifest


def _recover_precommit_asset_transaction(
    transaction_dir: str,
    manifest: dict,
) -> bool:
    """Abort a transaction while preserving every original public inode."""
    root = os.path.realpath(PUBLIC_DIR)
    for item in manifest["evictions"]:
        public_path = os.path.join(root, item["name"])
        backup_path = os.path.join(
            transaction_dir,
            _ASSET_TRANSACTION_PAYLOAD,
            item["backup"],
        )
        public_exists = os.path.lexists(public_path)
        backup_exists = os.path.lexists(backup_path)
        if public_exists:
            if not _same_manifest_inode(public_path, item):
                return False
            if backup_exists and not _same_manifest_inode(
                backup_path, item
            ):
                return False
        elif backup_exists and _same_manifest_inode(backup_path, item):
            os.link(backup_path, public_path, follow_symlinks=False)
            os.chmod(public_path, int(item["mode"]))
        else:
            return False
    staged_path = os.path.join(
        transaction_dir,
        _ASSET_TRANSACTION_PAYLOAD,
        ".incoming",
    )
    target = os.path.join(root, manifest["new_name"])
    if os.path.lexists(target):
        if (
            not _same_manifest_inode(target, manifest["incoming"])
            or not _same_manifest_inode(
                staged_path, manifest["incoming"]
            )
        ):
            return False
        os.unlink(target)
    _fsync_directory(root)
    return _cleanup_asset_transaction(transaction_dir)


def _recover_postcommit_asset_transaction(
    transaction_dir: str,
    manifest: dict,
) -> bool:
    """Finish a committed publication using only manifest-proven inodes."""
    root = os.path.realpath(PUBLIC_DIR)
    staged_path = os.path.join(
        transaction_dir,
        _ASSET_TRANSACTION_PAYLOAD,
        ".incoming",
    )
    target = os.path.join(root, manifest["new_name"])
    if os.path.lexists(target):
        if not _same_manifest_inode(target, manifest["incoming"]):
            return False
        if os.path.lexists(staged_path) and not _same_manifest_inode(
            staged_path, manifest["incoming"]
        ):
            return False
    else:
        if not _same_manifest_inode(
            staged_path, manifest["incoming"]
        ):
            return False
        _publish_staged_upload(staged_path, manifest["new_name"])
    _finalize_committed_asset_evictions(
        transaction_dir,
        manifest["evictions"],
    )
    return _cleanup_asset_transaction(transaction_dir)


def _safe_unjournaled_asset_staging(
    transaction_dir: str,
) -> bool:
    """Recognize only the pre-manifest state, before any old-file backup."""
    root = os.path.realpath(PUBLIC_DIR)
    candidate = os.path.realpath(transaction_dir)
    if (
        os.path.islink(transaction_dir)
        or not os.path.isdir(transaction_dir)
        or os.path.dirname(candidate) != root
        or candidate != os.path.join(
            root,
            os.path.basename(transaction_dir),
        )
    ):
        return False
    try:
        root_entries = set(os.listdir(candidate))
        if not root_entries <= {
            _ASSET_TRANSACTION_PAYLOAD,
            ".manifest.part",
        }:
            return False
        part = os.path.join(candidate, ".manifest.part")
        if os.path.lexists(part) and (
            os.path.islink(part) or not os.path.isfile(part)
        ):
            return False
        payload = os.path.join(
            candidate,
            _ASSET_TRANSACTION_PAYLOAD,
        )
        if not os.path.lexists(payload):
            return True
        if os.path.islink(payload) or not os.path.isdir(payload):
            return False
        entries = os.listdir(payload)
        return (
            set(entries) <= {".incoming"}
            and all(
                not os.path.islink(os.path.join(payload, name))
                and os.path.isfile(os.path.join(payload, name))
                for name in entries
            )
        )
    except OSError:
        return False


def recover_asset_transactions(
    tid: int | None = None,
    *,
    raise_on_blocked: bool = False,
) -> dict:
    """Resolve durable upload journals before accepting another write."""
    with _ASSET_STORE_LOCK:
        if tid is None:
            tids = set()
            root = os.path.realpath(PUBLIC_DIR)
            try:
                entries = list(os.scandir(root))
            except FileNotFoundError:
                entries = []
            for entry in entries:
                match = _ASSET_TRANSACTION_RE.fullmatch(entry.name)
                if match:
                    tids.add(int(match.group(1)))
        else:
            tids = {int(tid)}
        recovered = 0
        blocked = 0
        for tenant_id in sorted(tids):
            for transaction_dir in _tenant_asset_transaction_dirs(
                tenant_id
            ):
                resolved = False
                try:
                    manifest_path = os.path.join(
                        transaction_dir,
                        _ASSET_TRANSACTION_MANIFEST,
                    )
                    if not os.path.lexists(manifest_path):
                        if _safe_unjournaled_asset_staging(
                            transaction_dir
                        ):
                            resolved = _cleanup_asset_transaction(
                                transaction_dir
                            )
                        manifest = None
                    else:
                        manifest = _load_asset_transaction_manifest(
                            transaction_dir
                        )
                    if manifest is None:
                        if resolved:
                            recovered += 1
                        else:
                            blocked += 1
                        continue
                    current_assets = saved_assets(tenant_id)
                    current_photos = _saved_photos_for_tenant(
                        tenant_id
                    )
                    if (
                        current_assets
                        == manifest["original_assets"]
                        and current_photos
                        == manifest["original_photos"]
                    ):
                        resolved = _recover_precommit_asset_transaction(
                            transaction_dir,
                            manifest,
                        )
                    elif (
                        current_assets == manifest["target_assets"]
                        and current_photos
                        == manifest["target_photos"]
                    ):
                        resolved = _recover_postcommit_asset_transaction(
                            transaction_dir,
                            manifest,
                        )
                except BaseException:
                    log.error(
                        "avatar asset transaction recovery failed "
                        "tenant=%s",
                        tenant_id,
                    )
                if resolved:
                    recovered += 1
                else:
                    blocked += 1
        if blocked and raise_on_blocked:
            raise AssetQuotaExceeded(
                "数字人素材正在安全恢复；请稍后重试"
            )
        return {"recovered": recovered, "blocked": blocked}


def store_uploaded_asset(
    data: bytes,
    ext: str,
    kind: str,
    tid: int,
) -> dict:
    """Quota-check, atomically write and register one already-validated upload."""
    tid = int(tid)
    if kind not in {"photo", "voice", "video"}:
        raise ValueError("invalid avatar asset kind")
    with _ASSET_STORE_LOCK:
        recover_asset_transactions(tid, raise_on_blocked=True)
        original_assets = saved_assets(tid)
        original_photos = _saved_photos_for_tenant(tid)
        assets, photos, removed = _plan_asset_capacity(tid, len(data))
        reserved_name = _reserved_public_name(ext)
        if os.path.lexists(os.path.join(
            os.path.realpath(PUBLIC_DIR),
            reserved_name,
        )):
            raise FileExistsError("public upload target already exists")
        base = public_base()
        now = time.time()
        new_asset = {"name": reserved_name, "kind": kind, "ts": now}
        assets.append(new_asset)
        if kind == "photo":
            new_photo = {"name": reserved_name, "ts": now}
            photos.append(new_photo)
            if len(photos) > 24:
                dropped = photos[:-24]
                photos = photos[-24:]
                for item in dropped:
                    name = item.get("name")
                    if name and _can_reclaim(name, tid):
                        assets = [
                            asset for asset in assets
                            if asset.get("name") != name
                        ]
                        if name not in removed:
                            removed.append(name)

        evictions = _prepare_uploaded_asset_evictions(tid, removed)
        manifest = {
            "tenant_id": tid,
            "new_name": reserved_name,
            "original_assets": original_assets,
            "original_photos": original_photos,
            "target_assets": assets,
            "target_photos": photos,
            "evictions": evictions,
        }
        transaction_dir, staged_path = _stage_uploaded_asset(
            data,
            ext,
            reserved_name,
            manifest,
        )
        cleanup_allowed = False
        try:
            try:
                _quarantine_uploaded_asset_evictions(
                    tid,
                    evictions,
                    transaction_dir,
                )
            except BaseException:
                # Old public names were never removed, so partial backup links
                # can be discarded.  A failed discard remains journaled and
                # blocks this tenant on the next write.
                cleanup_allowed = True
                raise
            try:
                # Old public names remain live throughout the SQLite commit.
                # Thus a crash before commit can never strand an old registry
                # row.  Publication is a no-clobber hardlink to staged bytes.
                with db.atomic():
                    _save_asset_lists(tid, assets, photos)
                    _publish_staged_upload(staged_path, reserved_name)
            except BaseException:
                try:
                    unpublished = _unpublish_staged_upload(
                        staged_path,
                        reserved_name,
                    )
                except BaseException:
                    unpublished = False
                if not unpublished:
                    # Never erase the journal while the staged inode may still
                    # have an unregistered public link.  Startup/pre-upload
                    # recovery owns the next attempt.
                    raise RuntimeError(
                        "avatar upload transaction rollback failed"
                    )
                cleanup_allowed = True
                raise
            try:
                _finalize_committed_asset_evictions(
                    transaction_dir,
                    evictions,
                )
            except BaseException:
                # The upload is committed and usable.  Preserve all private
                # backup inodes, account them to this tenant, and block further
                # writes until deterministic recovery finishes the eviction.
                log.error(
                    "avatar committed eviction cleanup deferred "
                    "tenant=%s",
                    tid,
                )
            else:
                cleanup_allowed = True
            return {
                "name": reserved_name,
                "url": f"{base}/{reserved_name}",
            }
        finally:
            if cleanup_allowed:
                _cleanup_asset_transaction(transaction_dir)


def remember_asset(name: str, kind: str, tid: int = None):
    """登记租户上传的公开素材；UUID 不是租户权限，登记关系才是。"""
    from . import auth
    tid = int(tid or auth.tenant_id())
    name = (name or "").strip()
    if not _ASSET_NAME_RE.fullmatch(name) or kind not in _ASSET_KINDS:
        raise ValueError("invalid avatar asset")
    path = _public_file_path(name)
    if not path:
        raise ValueError("avatar asset file missing")
    with _ASSET_STORE_LOCK:
        recover_asset_transactions(tid, raise_on_blocked=True)
        if _registered_tenants(name) - {tid}:
            raise ValueError("avatar asset belongs to another tenant")
        existing = next(
            (item for item in saved_assets(tid) if item.get("name") == name),
            None,
        )
        if existing:
            items = [
                item for item in saved_assets(tid)
                if item.get("name") != name
            ]
            items.append({"name": name, "kind": kind, "ts": time.time()})
            db.set_setting(_asset_key(tid), json.dumps(items, ensure_ascii=False))
            return
        assets, photos, removed = _plan_asset_capacity(
            tid,
            os.path.getsize(path),
            exclude_name=name,
        )
        if removed:
            # This legacy entry registers a file that already exists.  It must
            # never evict durable user assets as a side effect or accept an
            # over-quota conservative state when physical deletion fails.
            raise AssetQuotaExceeded(
                "数字人素材空间已满；请先删除不再使用的素材"
            )
        new_asset = {"name": name, "kind": kind, "ts": time.time()}
        assets.append(new_asset)
        with db.atomic():
            _save_asset_lists(tid, assets, photos)


def forget_asset(name: str, tid: int = None):
    from . import auth
    tid = int(tid or auth.tenant_id())
    with _ASSET_STORE_LOCK:
        recover_asset_transactions(tid, raise_on_blocked=True)
        original_assets = saved_assets(tid)
        original_photos = _saved_photos_for_tenant(tid)
        items = [
            item for item in original_assets if item.get("name") != name
        ]
        photos = [
            item for item in original_photos if item.get("name") != name
        ]
        removed = [name] if _can_reclaim(name, tid) else []
        final_assets, final_photos = _commit_asset_reclamation(
            tid,
            provisional_assets=original_assets,
            provisional_photos=original_photos,
            target_assets=items,
            target_photos=photos,
            removed=removed,
        )
        return not any(
            item.get("name") == name
            for item in final_assets + final_photos
            if isinstance(item, dict)
        )


def asset_belongs(name: str, kinds: set[str], tid: int = None) -> bool:
    """兼容既有照片清单与历史工单，同时要求当前租户归属。"""
    from . import auth
    tid = int(tid or auth.tenant_id())
    if not _ASSET_NAME_RE.fullmatch(name or ""):
        return False
    if any(x.get("name") == name and x.get("kind") in kinds for x in saved_assets(tid)):
        return True
    if "photo" in kinds:
        legacy = db.jloads(db.get_setting(f"avatar_photos:{tid}"), []) or []
        if any(x.get("name") == name for x in legacy if isinstance(x, dict)):
            return True
    # 兼容升级前已经用于任务、但尚未写入素材登记清单的录音/照片。
    for row in db.q("SELECT params_json, audio_file FROM avatar_job WHERE tenant_id=?", (tid,)):
        params = db.jloads(row.get("params_json"), {}) or {}
        if params.get("photo_name") == name and "photo" in kinds:
            return True
        if params.get("own_audio_name") == name and "voice" in kinds:
            return True
        if (row.get("audio_file") or "").endswith("/" + name) and "generated" in kinds:
            return True
    return False


def asset_owner(name: str) -> int:
    """解析登录态预览文件的唯一租户归属；未知或冲突一律返回 0。"""
    if not _ASSET_NAME_RE.fullmatch(name or ""):
        return 0
    owners = set()
    for row in db.q("SELECT key,value FROM app_setting "
                    "WHERE key LIKE 'avatar_assets:%' OR key LIKE 'avatar_photos:%'"):
        try:
            tid = int(row["key"].rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
        items = db.jloads(row.get("value"), []) or []
        if any(isinstance(x, dict) and x.get("name") == name for x in items):
            owners.add(tid)
    for row in db.q("SELECT tenant_id,params_json,audio_file FROM avatar_job"):
        params = db.jloads(row.get("params_json"), {}) or {}
        if (params.get("photo_name") == name or params.get("own_audio_name") == name
                or (row.get("audio_file") or "").endswith("/" + name)):
            owners.add(int(row.get("tenant_id") or 1))
    return next(iter(owners)) if len(owners) == 1 else 0


def asset_path(name: str, kinds: set[str], tid: int = None) -> str:
    """把已登记素材名解析到公开目录内；同时防 UUID 软链逃逸。"""
    if not asset_belongs(name, kinds, tid):
        raise providers.ProviderError("数字人素材不存在或不属于当前企业")
    root = os.path.realpath(PUBLIC_DIR)
    path = os.path.realpath(os.path.join(root, name))
    try:
        inside = os.path.commonpath((root, path)) == root
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(path):
        raise providers.ProviderError("数字人素材不存在或路径无效")
    return path


def cloned_voices() -> list:
    from . import auth
    return db.jloads(db.get_setting(f"cloned_voices:{auth.tenant_id()}"), []) or []


def save_cloned_voices(voices: list):
    from . import auth
    db.set_setting(f"cloned_voices:{auth.tenant_id()}",
                   json.dumps(voices[:10], ensure_ascii=False))


# ---------- 照片卡槽(按租户存,用户可随意增删) ----------

def saved_photos() -> list:
    from . import auth
    return _saved_photos_for_tenant(auth.tenant_id())


def _save_photos(photos: list):
    from . import auth
    tid = auth.tenant_id()
    with _ASSET_STORE_LOCK:
        recover_asset_transactions(tid, raise_on_blocked=True)
        provisional_assets = saved_assets(tid)
        provisional_photos = list(photos)
        keep = photos[-24:]
        dropped = photos[:-24]
        assets = list(provisional_assets)
        removed = []
        for item in dropped:
            name = item.get("name") if isinstance(item, dict) else ""
            if name and _can_reclaim(name, tid):
                assets = [
                    asset
                    for asset in assets
                    if asset.get("name") != name
                ]
                removed.append(name)
        _commit_asset_reclamation(
            tid,
            provisional_assets=provisional_assets,
            provisional_photos=provisional_photos,
            target_assets=assets,
            target_photos=keep,
            removed=removed,
        )


def photos_add(name: str):
    with _ASSET_STORE_LOCK:
        remember_asset(name, "photo")
        photos = [p for p in saved_photos() if p.get("name") != name]
        photos.append({"name": name, "ts": time.time()})
        _save_photos(photos)


def photos_remove(name: str) -> bool:
    from . import auth
    tid = auth.tenant_id()
    with _ASSET_STORE_LOCK:
        recover_asset_transactions(tid, raise_on_blocked=True)
        photos = saved_photos()
        keep = [p for p in photos if p.get("name") != name]
        if len(keep) == len(photos):
            return False
        original_assets = saved_assets(tid)
        assets = [
            item for item in original_assets if item.get("name") != name
        ]
        removed = [name] if _can_reclaim(name, tid) else []
        final_assets, final_photos = _commit_asset_reclamation(
            tid,
            provisional_assets=original_assets,
            provisional_photos=photos,
            target_assets=assets,
            target_photos=keep,
            removed=removed,
        )
        return not any(
            item.get("name") == name
            for item in final_assets + final_photos
            if isinstance(item, dict)
        )


async def clone_voice(sample_path: str, label: str, save: bool = True) -> dict:
    """海螺声音克隆。

    ``save=False`` 供付费入口使用：调用方可把声音列表与计费成功放进同一个
    数据库事务，避免外部成功后本地只完成一半。
    """
    import re
    import uuid as _uuid
    base, key = providers.yunwu_conf()
    async with httpx.AsyncClient(timeout=180) as cli:
        with open(sample_path, "rb") as f:
            r = await cli.post(f"{base}/minimax/v1/files",
                               headers={"Authorization": f"Bearer {key}"},
                               files={"file": (os.path.basename(sample_path), f, "audio/mpeg")},
                               data={"purpose": "voice_clone"})
        d = r.json()
        fid = ((d.get("file") or {}).get("file_id"))
        if not fid:
            raise providers.ProviderError("声音样本上传失败，请稍后重试")
        vid = "boss" + _uuid.uuid4().hex[:12]
        r2 = await cli.post(f"{base}/minimax/v1/voice_clone",
                            headers={"Authorization": f"Bearer {key}"},
                            json={"file_id": fid, "voice_id": vid})
        d2 = r2.json()
        if (d2.get("base_resp") or {}).get("status_code") != 0:
            msg = (d2.get("base_resp") or {}).get("status_msg", "")
            if "duration too short" in msg:
                raise providers.ProviderError("录音太短:克隆需要至少 10 秒(建议 30 秒-1 分钟)")
            raise providers.ProviderError("声音克隆失败，请稍后重试")
    label = re.sub(r"\s+", "", label)[:12] or "我的声音"
    voice = {
        "id": vid,
        "label": f"🧬 {label}",
        "cloned": True,
        "created_at": time.time(),
    }
    if save:
        voices = cloned_voices()
        voices.insert(0, voice)
        save_cloned_voices(voices)
    return voice


async def tts(text: str, voice_id: str) -> str:
    """海螺配音,返回 https 音频 URL(可灵只认 https 音频)."""
    base, key = providers.yunwu_conf()
    async with httpx.AsyncClient(timeout=300) as cli:
        r = await cli.post(f"{base}/minimax/v1/t2a_v2",
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": "speech-2.8-hd", "text": text[:2000],
                                 "stream": False, "output_format": "url",
                                 "voice_setting": {"voice_id": voice_id, "speed": 1.0},
                                 "audio_setting": {"sample_rate": 32000, "format": "mp3"}})
        d = r.json()
        if (d.get("base_resp") or {}).get("status_code") != 0:
            raise providers.ProviderError("配音服务暂时不可用，请稍后重试")
        return d["data"]["audio"]


async def kling_avatar(image_url: str, audio_url: str, prompt: str = "") -> str:
    """提交可灵数字人任务,返回 task_id。pro 模式口型/画面同步更准."""
    base, key = providers.yunwu_conf()
    body = {"image": image_url, "sound_file": audio_url,
            "mode": db.get_setting("kling_mode") or "pro"}
    if prompt:
        body["prompt"] = prompt[:200]
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(f"{base}/kling/v1/videos/avatar/image2video",
                           headers={"Authorization": f"Bearer {key}"}, json=body)
        d = r.json()
        tid = ((d.get("data") or {}).get("task_id")) or ""
        if not tid:
            raise providers.ProviderError("数字人任务提交失败，请稍后重试")
        return tid


async def kling_poll(task_id: str, progress, timeout: int = 1500, job_id: int = None) -> str:
    """轮询直到成片,返回视频 URL."""
    base, key = providers.yunwu_conf()
    t0 = time.time()
    async with httpx.AsyncClient(timeout=60) as cli:
        while time.time() - t0 < timeout:
            await asyncio.sleep(12)
            if job_id and cancelled(job_id):
                raise Cancelled()
            r = await cli.get(f"{base}/kling/v1/videos/avatar/image2video/{task_id}",
                              headers={"Authorization": f"Bearer {key}"})
            d = (r.json().get("data") or {})
            st = d.get("task_status", "")
            if st == "succeed":
                vids = ((d.get("task_result") or {}).get("videos")) or []
                if vids and vids[0].get("url"):
                    return vids[0]["url"]
                raise providers.ProviderError("成片状态成功但没有视频地址")
            if st in ("failed", "error"):
                raise providers.ProviderError("数字人生成失败，请稍后重试")
            safe_state = st if st in {"submitted", "processing"} else "排队"
            progress("tool", f"数字人渲染中({safe_state})…已等 {int(time.time() - t0)}s")
    raise providers.ProviderError("数字人渲染超时(25分钟)")


def _charged_action(p: dict) -> str:
    """还原 avatar_job_create 当初扣的动作(按原始请求引擎+时长,不受运行中引擎回退影响)."""
    if (p.get("engine") or "") == "basic":
        return "avatar_basic"
    return "avatar_video" if int(p.get("duration") or 30) <= 30 else "avatar_video_60"


def settle_failure(job_id: int, message: str,
                   terminal_status: str = "failed") -> bool:
    """将未交付数字人任务原子收口，并且最多退回一次实际扣点。

    新工单把扣款金额固定在 ``billing_points``，所以之后即使管理员调整价格，
    退款也不会多退或少退。升级前的在途工单没有该字段值，只能按它原始参数对应
    的动作价兼容结算；结算时会把推导出的金额回填，后续重试仍然幂等。
    """
    if terminal_status not in {"failed", "cancelled"}:
        raise ValueError("invalid avatar terminal status")
    row = db.one(
        "SELECT id,tenant_id,params_json,status,billing_status,billing_points,"
        "deleted_at "
        "FROM avatar_job WHERE id=?",
        (job_id,),
    )
    if not row or row.get("deleted_at") is not None:
        return False
    text = (message or "数字人任务执行失败")[:500]
    if row.get("billing_status") == "included":
        return db.execute(
            "UPDATE avatar_job SET status=?,error=?,updated_at=? "
            "WHERE id=? AND billing_status='included' "
            "AND status IN ('queued','running') AND deleted_at IS NULL",
            (terminal_status, text, time.time(), job_id),
        ) == 1
    if row.get("billing_status") != "charged":
        return False
    params = db.jloads(row.get("params_json"), {}) or {}
    action = _charged_action(params)
    from . import billing
    stored = row.get("billing_points")
    points = (
        float(stored)
        if stored is not None
        else (0.0 if (row.get("tenant_id") or 1) == 1
              else float((billing.prices().get(action) or {"points": 1})["points"]))
    )
    def claim(connection):
        changed = connection.execute(
            "UPDATE avatar_job SET status=?,billing_status='refunded',"
            "billing_points=COALESCE(billing_points,?),error=?,updated_at=? "
            "WHERE id=? AND billing_status='charged' "
            "AND status IN ('pending_charge','queued','running') "
            "AND deleted_at IS NULL",
            (terminal_status, points, text, time.time(), job_id),
        )
        return changed.rowcount == 1

    label = (billing.prices().get(action) or {}).get("label", action)
    return billing.refund_amount_if_claimed(
        row.get("tenant_id") or 1,
        points,
        claim,
        f"退回:{label} · {text[:160]}",
    )


def prepare_retry(job_id: int, tenant_id: int) -> bool:
    """Requeue one failed digital-human job without charging a second time."""
    with db.atomic() as connection:
        changed = connection.execute(
            "UPDATE avatar_job SET status='queued',billing_status='included',"
            "steps_json='[]',audio_file=NULL,video_file=NULL,error=NULL,"
            "retry_count=COALESCE(retry_count,0)+1,updated_at=? "
            "WHERE id=? AND status='failed' "
            "AND tenant_id=? AND COALESCE(retry_count,0)<? "
            "AND billing_status IN ('refunded','included') "
            "AND deleted_at IS NULL",
            (time.time(), job_id, tenant_id, MAX_FREE_RETRIES),
        )
        return changed.rowcount == 1


async def run_job(job_id: int, broadcast):
    j = db.one(
        "SELECT * FROM avatar_job WHERE id=? AND deleted_at IS NULL", (job_id,)
    )
    if not j:
        return
    # 只有 queued→running 的 CAS 胜者可以开工。取消接口与 worker 同时撞上时，
    # cancelled 不会再被普通 UPDATE 覆盖回 running。
    started = db.execute(
        "UPDATE avatar_job SET status='running',updated_at=? "
        "WHERE id=? AND status='queued' "
        "AND billing_status IN ('charged','included') AND deleted_at IS NULL",
        (time.time(), job_id),
    )
    if started != 1:
        return
    j = db.one(
        "SELECT * FROM avatar_job WHERE id=? AND deleted_at IS NULL", (job_id,)
    )
    if not j:
        return
    p = db.jloads(j["params_json"])
    steps = []
    temporary_audio_paths: list[str] = []

    def cap_for_job(source: str) -> str:
        capped = _cap_audio(source, int(p.get("duration") or 30))
        if os.path.realpath(capped) != os.path.realpath(source):
            temporary_audio_paths.append(capped)
        return capped

    def progress(kind, label=""):
        steps.append({"k": kind, "l": str(label)[:300], "ts": time.time()})
        # 在事件循环上被调用;写锁竞争时同步写库会冻结全部协程,故进 db 线程池。
        # 快照先序列化(steps 之后还会被改),整体重写语义下丢一帧无碍。
        snapshot = json.dumps(steps, ensure_ascii=False)
        db.submit_write(db.update, "avatar_job", job_id, {"steps_json": snapshot})
        broadcast({"type": "avatar_step", "job_id": job_id, "step": steps[-1], "n": len(steps)})

    broadcast({"type": "avatar_update", "job_id": job_id})
    try:
        if cancelled(job_id):
            raise Cancelled()
        picked = p.get("engine") if p.get("engine") in ("heygen", "kling", "basic") else ""
        eng = picked or engine_name()
        if eng == "heygen" and not secureconfig.get_secret("heygen_key"):
            eng = "kling"
        elif eng == "heygen" and db.get_setting("heygen_exhausted"):
            if picked == "heygen":
                db.set_setting("heygen_exhausted", None)  # 明确点了就再试一次(可能已充值)
                progress("start", "尝试 HeyGen(若仍提示额度用完,请充值或改基础版/可灵)…")
            else:
                eng = "kling"
        photo_path = asset_path(p["photo_name"], {"photo"}, j.get("tenant_id") or 1)
        own_audio = p.get("own_audio_name")
        if eng == "basic":
            from . import runninghub
            if own_audio:
                audio_path = asset_path(own_audio, {"voice"}, j.get("tenant_id") or 1)
                db.update("avatar_job", job_id, {"audio_file": f"/files/avatar-public/{own_audio}"})
            else:
                progress("start", f"配音中(音色:{p.get('voice_label', '')})…")
                audio_url = await tts(p["script"], p["voice_id"])
                pub = save_public(
                    await _download_supplier_audio(audio_url), ".mp3"
                )
                audio_path = os.path.join(PUBLIC_DIR, pub["name"])
                db.update("avatar_job", job_id, {"audio_file": f"/files/avatar-public/{pub['name']}"})
            audio_path = cap_for_job(audio_path)
            url = await runninghub.synth(photo_path, audio_path, progress)
        elif eng == "heygen":
            try:
                progress("start", "HeyGen 引擎:上传数字人照片…")
                tp_id = await hg_upload_talking_photo(
                    photo_path, tenant_id=j.get("tenant_id"), job_id=job_id
                )
                audio_asset = None
                if own_audio:
                    progress("start", "上传您的原声录音(成片就是您本人的声音)…")
                    audio_path = cap_for_job(
                        asset_path(
                            own_audio, {"voice"}, j.get("tenant_id") or 1
                        )
                    )
                    audio_asset = await hg_upload_audio(audio_path)
                    db.update("avatar_job", job_id,
                              {"audio_file": f"/files/avatar-public/{own_audio}"})
                else:
                    progress("start", f"海螺配音中(音色:{p.get('voice_label', '')})…")
                    audio_url = await tts(p["script"], p["voice_id"])
                    pub = save_public(
                        await _download_supplier_audio(audio_url), ".mp3"
                    )
                    db.update("avatar_job", job_id,
                              {"audio_file": f"/files/avatar-public/{pub['name']}"})
                    progress("start", "配音上传 HeyGen…")
                    audio_path = cap_for_job(
                        os.path.join(PUBLIC_DIR, pub["name"])
                    )
                    audio_asset = await hg_upload_audio(audio_path)
                progress("start", "提交 HeyGen 数字人(Avatar IV 动作引擎,会动会说)…")
                vid = await hg_generate(tp_id, p["script"], audio_asset_id=audio_asset)
                progress("tool", f"任务已受理 video_id={vid[:16]}…,开始渲染")
                url = await hg_poll(vid, progress, job_id=job_id)
            except providers.ProviderError as e:
                if own_audio:
                    raise
                progress("retry", "HeyGen 暂不可用，自动改用可灵引擎出片…")
                eng = "kling"
        if eng == "kling":
            if own_audio:
                # 本人原声:直接用上传的录音作为可灵的音源(公网URL,可灵会驱动口型)
                progress("start", "用您上传的原声驱动口型…")
                audio_path = cap_for_job(
                    asset_path(
                        own_audio, {"voice"}, j.get("tenant_id") or 1
                    )
                )
                audio_url = f"{public_base()}/{os.path.basename(audio_path)}"
                db.update("avatar_job", job_id,
                          {"audio_file": f"/files/avatar-public/{own_audio}"})
            else:
                # 系统/克隆音色配音(海螺返回 https URL)
                progress("start", f"海螺配音中(音色:{p.get('voice_label', p.get('voice_id'))})…")
                audio_url = await tts(p["script"], p["voice_id"])
                pub = save_public(
                    await _download_supplier_audio(audio_url), ".mp3"
                )
                audio_path = cap_for_job(
                    os.path.join(PUBLIC_DIR, pub["name"])
                )
                audio_url = f"{public_base()}/{os.path.basename(audio_path)}"
                db.update("avatar_job", job_id,
                          {"audio_file": f"/files/avatar-public/{pub['name']}"})
                progress("done", "配音完成")
            photo_url = f"{public_base()}/{p['photo_name']}"
            # 2) 提交可灵数字人
            progress("start", "提交可灵数字人任务(照片+配音→对口型)…")
            tid = await kling_avatar(photo_url, audio_url, p.get("prompt", ""))
            progress("tool", f"任务已受理 task_id={tid[:18]}…,开始渲染")
            url = await kling_poll(tid, progress, job_id=job_id)
        # 3) 下载成片
        progress("fetch", "渲染完成,回收成片…")
        out = os.path.join(OUT_DIR, f"avatar_{job_id}.mp4")
        try:
            await netfetch.download_public_media(
                url,
                out,
                kind="video",
                max_bytes=MAX_REMOTE_VIDEO_BYTES,
                timeout=300,
            )
        except (ValueError, httpx.HTTPError) as exc:
            raise providers.ProviderError("供应商成片下载失败，请稍后重试") from exc
        delivered = db.execute(
            "UPDATE avatar_job SET status='done',"
            "billing_status=CASE WHEN billing_status='charged' "
            "THEN 'succeeded' ELSE 'included' END,"
            "video_file=?,updated_at=? "
            "WHERE id=? AND status='running' "
            "AND billing_status IN ('charged','included') "
            "AND deleted_at IS NULL",
            (f"/files/avatar/avatar_{job_id}.mp4", time.time(), job_id),
        )
        if delivered != 1:
            try:
                os.remove(out)
            except OSError:
                pass
            raise Cancelled()
        db.insert("asset", {"type": "avatar_video", "tenant_id": j.get("tenant_id") or 1,
                            "payload_json": json.dumps(
            {"title": (p.get("script") or "")[:30], "job_id": job_id,
             "file": f"/files/avatar/avatar_{job_id}.mp4"}, ensure_ascii=False)})
        progress("done", "🎬 数字人视频交付!")
    except Cancelled:
        log.info("avatar job %s cancelled by user", job_id)
        try:
            settle_failure(job_id, "老板已取消", terminal_status="cancelled")
        except Exception as exc:
            log.error(
                "avatar job %s cancellation settlement failed error_type=%s",
                job_id,
                type(exc).__name__,
            )
    except Exception as e:
        log.error(
            "avatar job %s failed error_type=%s",
            job_id,
            type(e).__name__,
        )
        public_error = providers.public_failure_message(
            e,
            "数字人任务处理失败，已安全收口，可从原任务免费重试",
        )
        try:
            settle_failure(job_id, public_error, terminal_status="failed")
        except Exception as exc:
            log.error(
                "avatar job %s failure settlement failed error_type=%s",
                job_id,
                type(exc).__name__,
            )
        try:
            progress("error", public_error)
        except Exception as exc:
            log.error(
                "avatar job %s error progress write failed error_type=%s",
                job_id,
                type(exc).__name__,
            )
    finally:
        for temporary_audio in temporary_audio_paths:
            try:
                os.remove(temporary_audio)
            except FileNotFoundError:
                pass
            except OSError:
                log.warning(
                    "avatar temporary audio cleanup failed job=%s",
                    job_id,
                )
        # 进度是异步落库的;结束前冲刷,保证终态可见时步骤记录已完整。
        await db.adrain()
        broadcast({"type": "avatar_update", "job_id": job_id})


def resume_pending(broadcast):
    """服务重启后:queued 的重新拉起;running 的外部渲染已断,标失败并退点(别让老板白扣)."""
    # 创建请求若在“工单落库、原子扣点”之前中断，只会留下未扣款 pending，
    # 可直接清理；已扣款工单与 queued 状态在同一个事务提交，不会落在这里。
    db.q(
        "DELETE FROM avatar_job "
        "WHERE status='pending_charge' AND billing_status='pending' "
        "AND deleted_at IS NULL"
    )
    for r in db.q(
            "SELECT * FROM avatar_job "
            "WHERE status IN ('queued','running') "
            "AND billing_status IN ('charged','included') "
            "AND deleted_at IS NULL"):
        if r["status"] == "queued":
            asyncio.create_task(run_job(r["id"], broadcast))
        else:
            try:
                settle_failure(
                    r["id"],
                    "服务重启中断,已自动退点,请重新生成",
                    terminal_status="failed",
                )
            except Exception as exc:
                log.error(
                    "avatar job %s restart settlement failed error_type=%s",
                    r["id"],
                    type(exc).__name__,
                )
            broadcast({"type": "avatar_update", "job_id": r["id"]})
