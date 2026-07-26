"""图文转视频·一键成片(V25).

流水线:正文 →(超长先压成口播稿)→ 分句 → 海螺 TTS 逐句配音 → 每句一张图
(Ken Burns 缓推镜头)+ 大字字幕 + 顶部标题 → ffmpeg 拼装 → 1080×1920 竖版 MP4。
对标剪映「图文成片」/闪剪,但吃的是派活自己的素材:工单真实图/AI图,或按关键词
全网现抓。没有图也能出片(纯色底+字幕)。

任务表 tv_job:排队/进行中/成片/失败;失败自动退点;重启后未完任务标失败退点。
"""
import asyncio
from contextlib import contextmanager
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time


MAX_RENDER_OUTPUT_BYTES = 768 * 1024 * 1024
_BOUNDED_MEDIA_PROFILES = {
    "probe": {
        "cpu": (10, 11),
        "address_space": 512 * 1024 * 1024,
        "file_size": 2 * 1024 * 1024,
        "processes": 32,
    },
    "thumbnail": {
        "cpu": (30, 31),
        "address_space": 768 * 1024 * 1024,
        "file_size": 8 * 1024 * 1024,
        "processes": 64,
    },
    "audio": {
        "cpu": (45, 46),
        "address_space": 768 * 1024 * 1024,
        "file_size": 8 * 1024 * 1024,
        "processes": 64,
    },
    "render": {
        "cpu": (1500, 1501),
        "address_space": 2 * 1024 * 1024 * 1024,
        "file_size": MAX_RENDER_OUTPUT_BYTES,
        "open_files": 128,
        "processes": 128,
    },
}


def _bounded_media_worker(argv: list[str]) -> int:
    """Single-threaded launcher: set rlimits, then exec one fixed media binary."""
    if len(argv) < 2 or argv[0] not in _BOUNDED_MEDIA_PROFILES:
        return 2
    profile = _BOUNDED_MEDIA_PROFILES[argv[0]]
    executable = os.path.realpath(argv[1])
    if (
        not os.path.isabs(executable)
        or not os.path.isfile(executable)
        or not os.access(executable, os.X_OK)
    ):
        return 2
    try:
        import resource
    except ImportError:
        return 2
    limits = (
        ("RLIMIT_CORE", 0, 0),
        ("RLIMIT_CPU", profile["cpu"][0], profile["cpu"][1]),
        (
            "RLIMIT_AS",
            profile["address_space"],
            profile["address_space"],
        ),
        ("RLIMIT_FSIZE", profile["file_size"], profile["file_size"]),
        (
            "RLIMIT_NOFILE",
            profile.get("open_files", 64),
            profile.get("open_files", 64),
        ),
        ("RLIMIT_NPROC", profile["processes"], profile["processes"]),
    )
    try:
        for name, soft, hard in limits:
            resource_key = getattr(resource, name, None)
            if resource_key is not None:
                resource.setrlimit(resource_key, (soft, hard))
    except (OSError, ValueError):
        return 2
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/nonexistent",
    }
    os.execve(executable, [executable, *argv[2:]], environment)
    return 2


# ``preexec_fn`` is unsafe when a threaded web server forks.  The parent starts
# a fresh Python interpreter with no hook; this early branch sets limits in that
# single-threaded process and replaces it with ffprobe/ffmpeg before app imports.
if __name__ == "__main__" and len(sys.argv) >= 2 \
        and sys.argv[1] == "--bounded-media-exec":
    raise SystemExit(_bounded_media_worker(sys.argv[2:]))


from . import avatar, db, netfetch, providers

log = logging.getLogger("textvideo")
_BUILD_SEM = asyncio.Semaphore(2)   # 全服并发合成上限:单机CPU保护,超出的排队
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_COUNTS: dict[int, int] = {}
_DELETE_REQUEST_ERROR = "删除在途任务"

FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
ASSET_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")
FPS = 30
W, H = 1080, 1920
DEFAULT_VOICE = "presenter_female"

# 兜底底色(没图的句子轮换,不至于黑屏)
BG_COLORS = ["0x1f2430", "0x2b3a55", "0x3a2b45", "0x224036", "0x40352b"]


def _plain(md: str) -> str:
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md or "")
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[#>*`|_-]{1,}", " ", t)
    t = re.sub(r"https?://\S+", "", t)
    return re.sub(r"[ \t]+", " ", t).strip()


async def make_script(title: str, body: str) -> str:
    """正文过长时压成 60-90 秒口播稿;短文直接用."""
    plain = _plain(body)
    if len(plain) <= 320:
        return plain
    r = await providers.call_text(
        3,
        f"把下面这篇《{title}》压缩成一段 60-90 秒的短视频口播稿(200-280字):"
        f"口语化、保留最抓人的钩子和核心信息、结尾一句号召;只输出口播稿正文,"
        f"不要标题不要任何说明。\n\n{plain[:3500]}",
        timeout=180)
    s = r["text"].strip().strip("「」\"“”")
    return s if 60 <= len(s) <= 600 else plain[:300]


def split_sentences(script: str) -> list:
    """分句:每句 6~26 字,做一屏字幕+一段配音."""
    parts = re.split(r"(?<=[。!?!?;;])\s*", script.replace("\n", " "))
    out = []
    for p in parts:
        p = p.strip(" 。")
        if not p:
            continue
        while len(p) > 26:          # 超长句按逗号再切
            cut = p.rfind(",", 8, 26)
            cut = cut if cut > 0 else 24
            out.append(p[:cut].strip(",,"))
            p = p[cut:].strip(",,")
        if p:
            out.append(p.strip(",,"))
    merged = []
    for p in out:                    # 太碎的短句并进上一句
        if merged and len(merged[-1]) + len(p) <= 14:
            merged[-1] += "," + p
        else:
            merged.append(p)
    return merged[:40]


def _wrap(text: str, width: int, max_lines: int) -> str:
    lines = [text[i:i + width] for i in range(0, len(text), width)][:max_lines]
    return "\n".join(lines)


def _probe_dur(path: str) -> float:
    try:
        if (
            not path
            or os.path.islink(path)
            or not os.path.isfile(path)
            or not (0 < os.path.getsize(path) <= 512 * 1024 * 1024)
        ):
            return 2.0
        configured = (
            os.environ.get("CONTENTCREW_FFPROBE_PATH")
            or shutil.which("ffprobe")
            or ""
        )
        command = _bounded_media_command(
            "probe",
            configured,
            [
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                path,
            ],
        )
        with tempfile.TemporaryFile(mode="w+b") as output:
            result = subprocess.run(
                command,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                cwd=os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__))
                ),
            )
            if result.returncode != 0:
                return 2.0
            output.seek(0)
            raw = output.read(129)
        if not raw or len(raw) > 128:
            return 2.0
        duration = float(raw.decode("ascii", "strict").strip())
        return max(0.6, duration) if math.isfinite(duration) else 2.0
    except (
        OSError,
        subprocess.TimeoutExpired,
        UnicodeError,
        TypeError,
        ValueError,
    ):
        return 2.0


XFADE = 0.45          # 段间叠化时长(秒):每段视频/音频都补同样的静音尾巴,叠化吃掉它
_TRANSITIONS = ["fade", "smoothleft", "circleopen", "dissolve", "smoothup"]

# 四种运镜轮换,告别"一律居中放大"的生硬感
def _motion(i: int, frames: int) -> str:
    z_in = f"zoompan=z='1+0.10*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    z_out = f"zoompan=z='1.10-0.10*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    pan_r = f"zoompan=z='1.10':x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)'"
    pan_l = f"zoompan=z='1.10':x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)'"
    return [z_in, pan_r, z_out, pan_l][i % 4] + f":d={frames}:s={W}x{H}:fps={FPS}"


def _seg_cmd(img: str, audio: str, dur: float, sub_file: str, title_file: str,
             out: str, seg_i: int = 0):
    total = dur + XFADE                      # 画面比语音多拖一个叠化时长
    frames = max(int(total * FPS) + 2, FPS)
    if img:
        src = ["-protocol_whitelist", "file,pipe", "-i", img]
        vf_head = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                   f"crop={W}:{H},{_motion(seg_i, frames)}")
    else:
        color = BG_COLORS[abs(hash(out)) % len(BG_COLORS)]
        src = [
            "-f",
            "lavfi",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            f"color=c={color}:s={W}x{H}:r={FPS}:d={total + 0.2}",
        ]
        vf_head = "null"
    # 字幕淡入(0.35s),不再硬弹出来
    vf = (f"{vf_head},"
          f"drawtext=fontfile={FONT}:textfile={title_file}:fontsize=54:fontcolor=white:"
          f"borderw=4:bordercolor=black@0.85:x=(w-text_w)/2:y=150:line_spacing=16,"
          f"drawtext=fontfile={FONT}:textfile={sub_file}:fontsize=62:fontcolor=white:"
          f"alpha='if(lt(t,0.35),t/0.35,1)':"
          f"borderw=5:bordercolor=black@0.9:x=(w-text_w)/2:y=h-430:line_spacing=20")
    return _render_command(
        src + [
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            audio,
            "-filter_complex",
            f"[0:v]{vf}[v];[1:a]apad=pad_dur={XFADE}[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            f"{total:.3f}",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            out,
        ]
    )


# ---------------- V25.3 Vlog 混剪:用户自己的视频片段 + 主题/文案 → 智能混剪 ----------------
CLIP_ROOT = os.path.join(ASSET_DIR, "tvclips")
MAX_CLIP_BYTES = 38 * 1024 * 1024
MAX_CLIP_PROBE_OUTPUT_BYTES = 1024 * 1024
_CLIP_NAME_RE = re.compile(
    r"^c_[0-9a-f]{10}(?:[0-9a-f]{22})?\.(?:mp4|mov|webm)$",
    re.I,
)
_CLIP_LIBRARY_LOCK_GUARD = threading.Lock()
_CLIP_LIBRARY_LOCKS: dict[int, threading.RLock] = {}


def clip_dir(tid: int) -> str:
    d = os.path.join(CLIP_ROOT, str(tid))
    os.makedirs(d, exist_ok=True)
    return d


@contextmanager
def clip_library_lock(tid: int):
    """Serialize one tenant's clip references and physical file mutations."""
    tid = int(tid)
    with _CLIP_LIBRARY_LOCK_GUARD:
        lock = _CLIP_LIBRARY_LOCKS.setdefault(tid, threading.RLock())
    with lock:
        yield


def valid_clip_name(name: str) -> bool:
    return (
        isinstance(name, str)
        and name == os.path.basename(name)
        and bool(_CLIP_NAME_RE.fullmatch(name))
    )


def resolve_clip_path(tid: int, name: str) -> str | None:
    """Resolve only managed, regular, non-symlink clips inside this tenant."""
    if not valid_clip_name(name):
        return None
    directory = clip_dir(int(tid))
    candidate = os.path.join(directory, name)
    if os.path.islink(candidate):
        return None
    root = os.path.realpath(directory)
    resolved = os.path.realpath(candidate)
    try:
        inside = os.path.commonpath((root, resolved)) == root
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(resolved):
        return None
    return candidate


def owned_clip_path(tid: int, stored: str) -> str | None:
    """把 params_json 里存下的片段路径重新收敛回本租户素材库。

    入队时已校验过,但执行时不能只信库里的字符串:参数被改写或历史数据被污染,
    都会让成片混进服务器上的任意视频(含他租户素材)。这里按文件名重新解析,
    并要求解析结果与存下来的路径一致。
    """
    if not isinstance(stored, str) or not stored:
        return None
    resolved = resolve_clip_path(tid, os.path.basename(stored))
    if not resolved:
        return None
    if os.path.realpath(resolved) != os.path.realpath(stored):
        return None
    return resolved


def _bounded_media_command(
    profile: str,
    executable: str,
    arguments: list[str],
) -> list[str]:
    if profile not in _BOUNDED_MEDIA_PROFILES:
        raise ValueError("unsupported media worker profile")
    binary = os.path.realpath(executable or "")
    python = os.path.realpath(sys.executable or "")
    for path in (python, binary):
        if (
            not path
            or not os.path.isabs(path)
            or not os.path.isfile(path)
            or not os.access(path, os.X_OK)
        ):
            raise OSError("bounded media executable unavailable")
    return [
        python,
        "-m",
        "app.textvideo",
        "--bounded-media-exec",
        profile,
        binary,
        *arguments,
    ]


def _render_command(arguments: list[str]) -> list[str]:
    """Run every production ffmpeg render through the bounded clean launcher."""
    configured = (
        os.environ.get("CONTENTCREW_FFMPEG_PATH")
        or shutil.which("ffmpeg")
        or ""
    )
    return _bounded_media_command(
        "render",
        configured,
        [
            "-nostdin",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *arguments,
        ],
    )


def _render_output_ok(path: str) -> bool:
    try:
        return (
            bool(path)
            and not os.path.islink(path)
            and os.path.isfile(path)
            and 0 < os.path.getsize(path) <= MAX_RENDER_OUTPUT_BYTES
        )
    except (OSError, TypeError, ValueError):
        return False


def _concat_cmd(list_file: str, final: str, transcode: bool = False) -> list[str]:
    arguments = [
        "-f",
        "concat",
        "-safe",
        "0",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        list_file,
    ]
    if transcode:
        arguments += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
        ]
    else:
        arguments += ["-c", "copy"]
    return _render_command([*arguments, final])


def probe_clip(path: str, expected_ext: str = "") -> dict:
    """Return bounded video metadata; every parser failure is a zero result."""
    invalid = {"dur": 0, "w": 0, "h": 0}
    try:
        if (
            not path
            or os.path.islink(path)
            or not os.path.isfile(path)
            or os.path.getsize(path) <= 0
            or os.path.getsize(path) > MAX_CLIP_BYTES
        ):
            return invalid
        with open(path, "rb") as handle:
            header = handle.read(4096)
        is_webm = header.startswith(b"\x1a\x45\xdf\xa3")
        is_iso_media = b"ftyp" in header[4:4096]
        if not (is_webm or is_iso_media):
            return invalid
        expected_ext = (
            expected_ext or os.path.splitext(path)[1]
        ).lower()
        if (
            (is_webm and expected_ext != ".webm")
            or (
                is_iso_media
                and expected_ext not in {".mp4", ".mov", ".m4v"}
            )
        ):
            return invalid
        configured_probe = (
            os.environ.get("CONTENTCREW_FFPROBE_PATH")
            or shutil.which("ffprobe")
            or ""
        )
        probe = os.path.realpath(configured_probe) if configured_probe else ""
        if (
            not probe
            or not os.path.isabs(probe)
            or not os.path.isfile(probe)
            or not os.access(probe, os.X_OK)
        ):
            return invalid
        arguments = [
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-probesize",
            "5000000",
            "-analyzeduration",
            "5000000",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration,format_name",
            "-of",
            "json",
            path,
        ]
        command = _bounded_media_command("probe", probe, arguments)
        kwargs = {
            "stderr": subprocess.DEVNULL,
            "timeout": 15,
            "check": False,
            "cwd": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        }
        with tempfile.TemporaryFile(mode="w+b") as output:
            result = subprocess.run(command, stdout=output, **kwargs)
            if result.returncode != 0:
                return invalid
            output.seek(0)
            raw = output.read(MAX_CLIP_PROBE_OUTPUT_BYTES + 1)
        if not raw or len(raw) > MAX_CLIP_PROBE_OUTPUT_BYTES:
            return invalid
        payload = json.loads(raw.decode("utf-8", "strict"))
        if not isinstance(payload, dict):
            return invalid
        format_data = payload.get("format")
        if not isinstance(format_data, dict):
            return invalid
        format_names = {
            name.strip().lower()
            for name in str(format_data.get("format_name") or "").split(",")
            if name.strip()
        }
        if (
            (is_webm and not format_names.intersection({"webm", "matroska"}))
            or (
                is_iso_media
                and not format_names.intersection(
                    {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
                )
            )
        ):
            return invalid
        stream = next(
            (
                item for item in (payload.get("streams") or [])
                if isinstance(item, dict)
            ),
            {},
        )
        duration = float(format_data.get("duration") or 0)
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        if not (
            0 < duration <= 600
            and 0 < width <= avatar.MAX_UPLOAD_IMAGE_DIMENSION
            and 0 < height <= avatar.MAX_UPLOAD_IMAGE_DIMENSION
            and width * height <= avatar.MAX_UPLOAD_IMAGE_PIXELS
        ):
            return invalid
        return {"dur": duration, "w": width, "h": height}
    except (
        json.JSONDecodeError,
        OSError,
        subprocess.TimeoutExpired,
        TypeError,
        ValueError,
        OverflowError,
        AttributeError,
    ):
        return invalid


def make_thumb(path: str, out: str, at: float = 1.0):
    """Create one bounded thumbnail without exposing parser output."""
    try:
        if (
            not path
            or os.path.islink(path)
            or not os.path.isfile(path)
            or not out
            or os.path.islink(out)
        ):
            return False
        configured = (
            os.environ.get("CONTENTCREW_FFMPEG_PATH")
            or shutil.which("ffmpeg")
            or ""
        )
        ffmpeg = os.path.realpath(configured) if configured else ""
        arguments = [
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-threads",
            "1",
            "-ss",
            f"{max(0.0, float(at)):.1f}",
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            "scale=320:-2",
            "-threads",
            "1",
            out,
        ]
        command = _bounded_media_command("thumbnail", ffmpeg, arguments)
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        valid = (
            result.returncode == 0
            and not os.path.islink(out)
            and os.path.isfile(out)
            and 0 < os.path.getsize(out) <= 8 * 1024 * 1024
        )
        if not valid:
            try:
                os.remove(out)
            except (FileNotFoundError, OSError):
                pass
        return valid
    except (
        OSError,
        subprocess.TimeoutExpired,
        TypeError,
        ValueError,
        OverflowError,
    ):
        try:
            os.remove(out)
        except (FileNotFoundError, OSError, TypeError):
            pass
        return False


async def _describe_clips(clips: list, progress) -> list:
    """由多媒体师所选 API 模型看中间帧；失败时返回空描述。"""
    import base64
    import tempfile as _tf
    descs = [""] * len(clips)
    try:
        images = []
        with _tf.TemporaryDirectory() as td:
            for i, c in enumerate(clips[:8]):
                fp = os.path.join(td, f"f{i}.jpg")
                await asyncio.to_thread(make_thumb, c["path"], fp, max(0.5, c["dur"] / 2))
                if os.path.isfile(fp):
                    with open(fp, "rb") as handle:
                        images.append((
                            "image/jpeg",
                            base64.b64encode(handle.read()).decode(),
                        ))
        if not images:
            return descs
        result = await providers.call_vision(
            5,
            f"这是 {len(images)} 个视频片段各自的截图,按顺序用一句中文(15字内)"
            '描述每张拍了什么。只输出 JSON:{"descs":["描述1","描述2",...]},'
            "数量与图一致。",
            images,
            timeout=240,
            token="textvideo:clip-vision",
        )
        from . import llm
        got = llm.extract_json(result["text"]).get("descs") or []
        for i, dsc in enumerate(got[:len(descs)]):
            descs[i] = str(dsc)[:30]
        progress("看完素材:" + ";".join(
            f"片段{i + 1}={d}" for i, d in enumerate(descs) if d
        ))
    except Exception as exc:
        log.warning(
            "clip vision failed error_type=%s", type(exc).__name__
        )
        progress("素材视觉识别没成功,改用轮换分配画面")
    return descs


async def _assign_clips(sents: list, descs: list) -> list:
    """每句话配哪个片段:懂画面就按内容配,不懂就轮换."""
    n = len(descs)
    fallback = [i % n for i in range(len(sents))]
    if not any(descs):
        return fallback
    try:
        r = await providers.call_text(
            5,
            "口播稿逐句列表:\n" + "\n".join(f"{i}: {s}" for i, s in enumerate(sents))
            + "\n\n可用画面片段:\n" + "\n".join(f"{i}: {d or '(未知画面)'}" for i, d in enumerate(descs))
            + f"\n\n为每句话选最贴的画面片段(内容相关优先;避免连续多句用同一片段)。"
              f'只输出 JSON:{{"map":[片段序号×{len(sents)}]}}',
            timeout=120)
        from . import llm
        m = llm.extract_json(r["text"]).get("map") or []
        m = [int(x) % n for x in m][:len(sents)]
        if len(m) == len(sents):
            return m
    except Exception as exc:
        log.warning(
            "clip assign failed error_type=%s", type(exc).__name__
        )
    return fallback


def _clip_seg_cmd(clip_path: str, offset: float, dur: float, audio: str,
                  sub_file: str, title_file: str, out: str, first: bool):
    """从用户片段裁一段配上配音字幕:替换原声,轻调色,首段淡入."""
    total = dur + XFADE
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"fps={FPS},setsar=1,eq=saturation=1.12:brightness=0.015,"
          f"tpad=stop_mode=clone:stop_duration={XFADE + 1},"
          + (f"fade=t=in:st=0:d=0.5," if first else "")
          + f"drawtext=fontfile={FONT}:textfile={title_file}:fontsize=54:fontcolor=white:"
          f"borderw=4:bordercolor=black@0.85:x=(w-text_w)/2:y=150:line_spacing=16,"
          f"drawtext=fontfile={FONT}:textfile={sub_file}:fontsize=62:fontcolor=white:"
          f"alpha='if(lt(t,0.35),t/0.35,1)':"
          f"borderw=5:bordercolor=black@0.9:x=(w-text_w)/2:y=h-430:line_spacing=20")
    return _render_command([
        "-ss",
        f"{max(0, offset):.2f}",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        clip_path,
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        audio,
        "-filter_complex",
        f"[0:v]{vf}[v];[1:a]apad=pad_dur={XFADE}[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        f"{total:.3f}",
        "-r",
        str(FPS),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        out,
    ])


async def build_from_clips(tvid: int, tid: int, title: str, script: str, clips: list,
                           voice_id: str, out_rel_dir: str, progress,
                           bgm: str = "warm", end_text: str = "") -> str:
    """混剪主流程:句子×片段智能匹配 → 裁剪+配音+字幕 → 叠化+BGM."""
    bgm = validate_bgm(bgm)
    sents = split_sentences(script)
    if not sents:
        raise ValueError("口播稿是空的")
    for c in clips:
        c.update(probe_clip(c["path"]))
    clips = [c for c in clips if c["dur"] >= 1.5]
    if not clips:
        raise ValueError("片段都太短或无法读取(每段至少1.5秒)")
    progress(f"素材 {len(clips)} 段视频,口播 {len(sents)} 句;先看看每段拍了什么…")
    descs = await _describe_clips(clips, progress)
    mapping = await _assign_clips(sents, descs)
    progress("画面分配好了,逐句配音…")
    with tempfile.TemporaryDirectory() as td:
        sem = asyncio.Semaphore(4)

        async def _one(i, s):
            async with sem:
                url = await avatar.tts(s, voice_id or DEFAULT_VOICE)
                p = os.path.join(td, f"a{i}.mp3")
                await netfetch.download_public_media(
                    url,
                    p,
                    kind="audio",
                    max_bytes=avatar.MAX_REMOTE_AUDIO_BYTES,
                    timeout=120,
                )
                return p
        audios = await asyncio.gather(*(_one(i, s) for i, s in enumerate(sents)))
        title_file = os.path.join(td, "title.txt")
        with open(title_file, "w", encoding="utf-8") as f:
            f.write(_wrap(title.strip()[:28], 14, 2))
        segs, lens, used = [], [], {}
        for i, (s, a) in enumerate(zip(sents, audios)):
            dur = await asyncio.to_thread(_probe_dur, a)
            ci = mapping[i]
            clip = clips[ci]
            # 同一片段第N次使用往后挪起点,别老放同一秒
            k = used.get(ci, 0)
            used[ci] = k + 1
            span = max(clip["dur"] - dur - XFADE - 0.3, 0)
            offset = min(k * (dur + 2.5), span)
            sub = os.path.join(td, f"s{i}.txt")
            with open(sub, "w", encoding="utf-8") as f:
                f.write(_wrap(s, 13, 2))
            out = os.path.join(td, f"seg{i}.mp4")
            cmd = _clip_seg_cmd(clip["path"], offset, dur, a, sub, title_file, out, i == 0)
            r = await asyncio.to_thread(
                subprocess.run,
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
            )
            if r.returncode != 0 or not _render_output_ok(out):
                log.warning(
                    "clip segment failed segment=%s returncode=%s",
                    i + 1,
                    r.returncode,
                )
                raise ValueError(f"第{i + 1}句剪辑失败")
            segs.append(out)
            lens.append(dur + XFADE)
            if (i + 1) % 3 == 0 or i == len(sents) - 1:
                progress(f"剪辑 {i + 1}/{len(sents)}(画面:片段{ci + 1}{('·' + descs[ci]) if descs[ci] else ''})")
        await _append_ending(td, title_file, end_text, segs, lens)   # 结尾关注卡
        final_dir = os.path.join(ASSET_DIR, out_rel_dir)
        os.makedirs(final_dir, exist_ok=True)
        name = f"tv_{tvid}.mp4"
        final = os.path.join(final_dir, name)
        done = False
        if len(segs) > 1:
            progress("拼装:叠化转场 + 轻音乐铺底…")
            try:
                base, fgraph, vlabel, _f, total_len = _xfade_cmd(segs, lens, final, bgm_mood=bgm)
                cmd = base + ["-filter_complex", fgraph,
                              "-map", f"[{vlabel}]", "-map", "[aout]",
                              "-t", f"{total_len:.3f}", "-r", str(FPS),
                              "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                              "-c:a", "aac", "-b:a", "128k", final]
                r = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1200,
                )
                done = r.returncode == 0 and _render_output_ok(final)
                if not done:
                    log.warning("clips xfade failed returncode=%s", r.returncode)
            except Exception as exc:
                log.warning(
                    "clips xfade error_type=%s", type(exc).__name__
                )
        if not done:
            lst = os.path.join(td, "list.txt")
            with open(lst, "w") as f:
                f.writelines(f"file '{x}'\n" for x in segs)
            r = await asyncio.to_thread(
                subprocess.run,
                _concat_cmd(lst, final, transcode=False),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
            )
            if r.returncode != 0 or not _render_output_ok(final):
                log.warning(
                    "clip concat failed returncode=%s output_exists=%s",
                    r.returncode,
                    _render_output_ok(final),
                )
                raise ValueError("拼装失败")
        final_duration = await asyncio.to_thread(_probe_dur, final)
        progress(f"混剪完成:{final_duration:.0f} 秒(转场/字幕/BGM/调色已配)")
        return f"/files/{out_rel_dir}/{name}"


# ---------------- 轻音乐 BGM(numpy 合成柔和铺底,免版权) ----------------
# 三套配乐风格(numpy合成免版权):warm温暖 / up轻快 / calm沉稳;none=不配乐
BGM_MOODS = {"warm": {"label": "温暖", "bar": 4.0, "chords": [
                 (261.63, 329.63, 392.00), (196.00, 246.94, 392.00),
                 (220.00, 261.63, 329.63), (174.61, 220.00, 349.23)]},   # C-G-Am-F
             "up": {"label": "轻快", "bar": 2.6, "chords": [
                 (261.63, 329.63, 392.00), (174.61, 220.00, 349.23),
                 (196.00, 246.94, 392.00), (261.63, 329.63, 392.00)]},   # C-F-G-C
             "calm": {"label": "沉稳", "bar": 5.0, "chords": [
                 (220.00, 261.63, 329.63), (174.61, 220.00, 349.23),
                 (261.63, 329.63, 392.00), (196.00, 246.94, 392.00)]}}   # Am-F-C-G

_BGM_FILENAMES = {
    "warm": "_bgm_warm.wav",
    "up": "_bgm_up.wav",
    "calm": "_bgm_calm.wav",
}
BGM_CHOICES = frozenset((*_BGM_FILENAMES, "none"))
MAX_BGM_BYTES = 4 * 1024 * 1024
_BGM_LOCK_GUARD = threading.Lock()
_BGM_LOCKS: dict[str, threading.Lock] = {}


def validate_bgm(value, default: str = "warm") -> str:
    """Return one exact server-owned BGM enum; never normalize attacker input."""
    mood = default if value is None else value
    if not isinstance(mood, str) or mood not in BGM_CHOICES:
        raise ValueError("unsupported BGM mood")
    return mood


def _bgm_file_is_valid(path: str) -> bool:
    try:
        return (
            not os.path.islink(path)
            and os.path.isfile(path)
            and 44 < os.path.getsize(path) <= MAX_BGM_BYTES
        )
    except OSError:
        return False


def _ensure_bgm(mood: str = "warm") -> str:
    mood = validate_bgm(mood)
    if mood == "none":
        raise ValueError("BGM file is unavailable for the none mood")
    filename = _BGM_FILENAMES[mood]
    directory = os.path.realpath(os.path.join(ASSET_DIR, "tv"))
    path = os.path.join(directory, filename)
    if _bgm_file_is_valid(path):
        return path

    with _BGM_LOCK_GUARD:
        lock = _BGM_LOCKS.setdefault(mood, threading.Lock())
    with lock:
        if _bgm_file_is_valid(path):
            return path
        import wave
        import numpy as np

        spec = BGM_MOODS[mood]
        sr, bar = 44100, spec["bar"]
        t = np.arange(int(sr * bar)) / sr
        env = np.minimum(t / (bar * 0.2), 1.0) * np.minimum(
            (bar - t) / (bar * 0.3),
            1.0,
        )
        parts = []
        for freqs in spec["chords"]:
            wavef = sum(
                np.sin(2 * np.pi * frequency * t) * amplitude
                for frequency, amplitude in zip(
                    freqs,
                    (0.30, 0.24, 0.20),
                )
            )
            wavef += 0.06 * np.sin(
                2 * np.pi * freqs[0] / 2 * t
            )
            parts.append((wavef * env).astype(np.float32))
        loop = np.concatenate(parts)
        loop = (
            loop / (np.max(np.abs(loop)) + 1e-6) * 0.9 * 32767
        ).astype(np.int16)

        os.makedirs(directory, mode=0o750, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{filename}.",
            suffix=".tmp",
            dir=directory,
        )
        os.close(descriptor)
        replaced = False
        try:
            with wave.open(temporary, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(sr)
                output.writeframes(loop.tobytes())
            if not _bgm_file_is_valid(temporary):
                raise OSError("generated BGM failed validation")
            os.chmod(temporary, 0o640)
            with open(temporary, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            replaced = True
            try:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_fd = os.open(directory, flags)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if not replaced:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
        if not _bgm_file_is_valid(path):
            raise OSError("generated BGM is unavailable")
        return path


def _xfade_cmd(segs: list, lens: list, final: str, bgm_mood: str = "warm") -> list:
    """段间叠化拼装(视频 xfade + 音频 acrossfade,叠化吃掉每段的静音尾巴)+ 可选低音量 BGM."""
    bgm_mood = validate_bgm(bgm_mood)
    n = len(segs)
    inputs = []
    for s in segs:
        inputs += ["-protocol_whitelist", "file,pipe", "-i", s]
    with_bgm = bgm_mood != "none"
    if with_bgm:
        inputs += [
            "-stream_loop",
            "-1",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            _ensure_bgm(bgm_mood),
        ]
    fc, cur_v, cur_a = [], "0:v", "0:a"
    cur_len = lens[0]
    for i in range(1, n):
        tr = _TRANSITIONS[(i - 1) % len(_TRANSITIONS)]
        off = max(cur_len - XFADE, 0.1)
        fc.append(f"[{cur_v}][{i}:v]xfade=transition={tr}:duration={XFADE}:offset={off:.3f}[v{i}]")
        fc.append(f"[{cur_a}][{i}:a]acrossfade=d={XFADE}[a{i}]")
        cur_v, cur_a = f"v{i}", f"a{i}"
        cur_len = cur_len + lens[i] - XFADE
    fade_st = max(cur_len - 1.5, 0)
    if with_bgm:
        fc.append(f"[{n}:a]volume=0.10,atrim=0:{cur_len:.3f}[bg]")
        fc.append(f"[{cur_a}][bg]amix=inputs=2:duration=first:normalize=0,"
                  f"afade=t=out:st={fade_st:.3f}:d=1.5[aout]")
    else:
        fc.append(f"[{cur_a}]afade=t=out:st={fade_st:.3f}:d=1.0[aout]")
    return (
        _render_command(inputs),
        ";".join(fc),
        cur_v,
        final,
        cur_len,
    )


async def build(tvid: int, tid: int, title: str, script: str, images: list,
                voice_id: str, out_rel_dir: str, progress,
                bgm: str = "warm", end_text: str = "") -> str:
    """成片主流程,返回 /files 路径."""
    bgm = validate_bgm(bgm)
    sents = split_sentences(script)
    if not sents:
        raise ValueError("口播稿是空的")
    progress(f"口播稿共 {len(sents)} 句,逐句配音…")
    with tempfile.TemporaryDirectory() as td:
        # ① 逐句 TTS(并行4路)
        sem = asyncio.Semaphore(4)

        async def _one(i, s):
            async with sem:
                url = await avatar.tts(s, voice_id or DEFAULT_VOICE)
                p = os.path.join(td, f"a{i}.mp3")
                await netfetch.download_public_media(
                    url,
                    p,
                    kind="audio",
                    max_bytes=avatar.MAX_REMOTE_AUDIO_BYTES,
                    timeout=120,
                )
                return p
        audios = await asyncio.gather(*(_one(i, s) for i, s in enumerate(sents)))
        progress("配音完成,开始逐句合成画面…")
        # ② 每句一段视频
        title_file = os.path.join(td, "title.txt")
        with open(title_file, "w", encoding="utf-8") as f:
            f.write(_wrap(title.strip()[:28], 14, 2))
        segs, lens = [], []
        for i, (s, a) in enumerate(zip(sents, audios)):
            dur = await asyncio.to_thread(_probe_dur, a)
            sub = os.path.join(td, f"s{i}.txt")
            with open(sub, "w", encoding="utf-8") as f:
                f.write(_wrap(s, 13, 2))
            img = images[i * len(images) // len(sents)] if images else None
            out = os.path.join(td, f"seg{i}.mp4")
            cmd = _seg_cmd(img, a, dur, sub, title_file, out, seg_i=i)
            r = await asyncio.to_thread(
                subprocess.run,
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
            )
            if r.returncode != 0 or not _render_output_ok(out):
                log.warning(
                    "video segment failed segment=%s returncode=%s",
                    i + 1,
                    r.returncode,
                )
                raise ValueError(f"第{i + 1}句合成失败")
            segs.append(out)
            lens.append(dur + XFADE)
            if (i + 1) % 3 == 0 or i == len(sents) - 1:
                progress(f"画面合成 {i + 1}/{len(sents)}(运镜:推近/横移/拉远轮换)")
        await _append_ending(td, title_file, end_text, segs, lens)   # 结尾关注卡
        # ③ 拼装:段间叠化转场 + 轻音乐铺底;失败回退硬切拼接
        final_dir = os.path.join(ASSET_DIR, out_rel_dir)
        os.makedirs(final_dir, exist_ok=True)
        name = f"tv_{tvid}.mp4"
        final = os.path.join(final_dir, name)
        done = False
        if len(segs) > 1:
            progress("拼装:叠化转场 + 轻音乐铺底…")
            try:
                base, fgraph, vlabel, _f, total_len = _xfade_cmd(segs, lens, final, bgm_mood=bgm)
                cmd = base + ["-filter_complex", fgraph,
                              "-map", f"[{vlabel}]", "-map", "[aout]",
                              "-t", f"{total_len:.3f}", "-r", str(FPS),
                              "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                              "-c:a", "aac", "-b:a", "128k", final]
                r = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=900,
                )
                done = r.returncode == 0 and _render_output_ok(final)
                if not done:
                    log.warning("xfade assembly failed returncode=%s", r.returncode)
                    progress("叠化拼装失败,回退简单拼接")
            except Exception as exc:
                log.warning(
                    "xfade assembly error_type=%s", type(exc).__name__
                )
        if not done:
            lst = os.path.join(td, "list.txt")
            with open(lst, "w") as f:
                f.writelines(f"file '{s}'\n" for s in segs)
            r = await asyncio.to_thread(
                subprocess.run,
                _concat_cmd(lst, final, transcode=False),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
            )
            if r.returncode != 0 or not _render_output_ok(final):
                r = await asyncio.to_thread(
                    subprocess.run,
                    _concat_cmd(lst, final, transcode=True),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=600,
                )
                if r.returncode != 0 or not _render_output_ok(final):
                    log.warning(
                        "video concat transcode failed returncode=%s",
                        r.returncode,
                    )
                    raise ValueError("拼装失败")
        final_duration = await asyncio.to_thread(_probe_dur, final)
        progress(f"成片完成:{final_duration:.0f} 秒(含转场/运镜/BGM)")
        return f"/files/{out_rel_dir}/{name}"


def _ending_cmd(title_file: str, end_file: str, out: str, dur: float = 2.2):
    """结尾卡:品牌深底+标题+关注引导,静音(BGM 会铺过来)."""
    total = dur + XFADE
    vf = (f"drawtext=fontfile={FONT}:textfile={title_file}:fontsize=64:fontcolor=#ffd166:"
          f"borderw=0:x=(w-text_w)/2:y=h/2-220:line_spacing=20,"
          f"drawtext=fontfile={FONT}:textfile={end_file}:fontsize=48:fontcolor=white:"
          f"alpha='if(lt(t,0.5),t/0.5,1)':x=(w-text_w)/2:y=h/2+40:line_spacing=18,"
          f"fade=t=in:st=0:d=0.4")
    return _render_command([
        "-f",
        "lavfi",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        f"color=c=0x2b2317:s={W}x{H}:r={FPS}:d={total + 0.2}",
        "-f",
        "lavfi",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        f"anullsrc=r=44100:cl=mono:d={total + 0.2}",
        "-filter_complex",
        f"[0:v]{vf}[v]",
        "-map",
        "[v]",
        "-map",
        "1:a",
        "-t",
        f"{total:.3f}",
        "-r",
        str(FPS),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        out,
    ])


async def _append_ending(td: str, title_file: str, end_text: str, segs: list, lens: list):
    end_file = os.path.join(td, "end.txt")
    with open(end_file, "w", encoding="utf-8") as f:
        f.write(_wrap((end_text or "喜欢这条就点赞关注\n下条更精彩")[:40], 12, 3))
    out = os.path.join(td, "seg_end.mp4")
    r = await asyncio.to_thread(
        subprocess.run,
        _ending_cmd(title_file, end_file, out),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    if r.returncode == 0 and _render_output_ok(out):
        segs.append(out)
        lens.append(2.2 + XFADE)


def _steps_append(tvid: int, msg: str):
    ts = time.time()

    def _tx():
        # 读-改-写要在同一事务里做:进池后可能与相邻追加并发,
        # BEGIN IMMEDIATE 保证两次追加都不丢。
        with db.atomic() as c:
            r = c.execute(
                "SELECT steps_json FROM tv_job WHERE id=?", (tvid,)).fetchone()
            if not r:
                return
            steps = db.jloads(r["steps_json"], [])
            steps.append({"t": ts, "msg": msg})
            c.execute("UPDATE tv_job SET steps_json=?,updated_at=? WHERE id=?",
                      (json.dumps(steps, ensure_ascii=False), ts, tvid))

    # 常在事件循环上被 run_job 协程调用;进度落库是写操作,必须进 db 线程池,
    # 否则写锁竞争时会冻结整个循环。丢一条进度无碍。
    db.submit_write(_tx)


def _active_enter(tvid: int):
    with _ACTIVE_LOCK:
        _ACTIVE_COUNTS[tvid] = _ACTIVE_COUNTS.get(tvid, 0) + 1


def _active_leave(tvid: int):
    with _ACTIVE_LOCK:
        left = _ACTIVE_COUNTS.get(tvid, 0) - 1
        if left > 0:
            _ACTIVE_COUNTS[tvid] = left
        else:
            _ACTIVE_COUNTS.pop(tvid, None)


def _is_active(tvid: int) -> bool:
    with _ACTIVE_LOCK:
        return _ACTIVE_COUNTS.get(tvid, 0) > 0


def _safe_broadcast(broadcast, payload: dict):
    if not broadcast:
        return
    try:
        broadcast(payload)
    except Exception as exc:
        # WebSocket/通知属于交付后的旁路，不能反向改变任务或计费终态。
        log.error(
            "tv_job %s broadcast failed error_type=%s",
            payload.get("tv_id"),
            type(exc).__name__,
        )


def _output_folders(row: dict | None) -> list[str]:
    folders = ["tv"]
    if row and row.get("job_id") is not None:
        try:
            folders.append(f"job{int(row['job_id'])}")
        except (TypeError, ValueError):
            pass
    return folders


def _safe_unlink(path: str):
    root = os.path.realpath(ASSET_DIR)
    candidate = os.path.abspath(path)
    parent = os.path.realpath(os.path.dirname(candidate))
    if parent != root and not parent.startswith(root + os.sep):
        return
    try:
        # 对符号链接删除链接本身，不跟随到同一素材根中的其他租户文件。
        os.remove(candidate)
    except OSError:
        pass


def _cleanup_hunt_assets(tvid: int):
    for i in range(12):
        _safe_unlink(os.path.join(ASSET_DIR, "tv", f"hunt_{tvid}_{i}.jpg"))


def cleanup_job_assets(tvid: int, row: dict | None = None,
                       extra_file: str | None = None):
    """删除本任务可预测命名的产物，绝不采信数据库里的任意文件路径。"""
    row = row or db.one("SELECT id,job_id,video_file FROM tv_job WHERE id=?", (tvid,))
    folders = _output_folders(row)
    allowed_urls = {
        f"/files/{folder}/tv_{tvid}.mp4": os.path.join(
            ASSET_DIR, folder, f"tv_{tvid}.mp4")
        for folder in folders
    }
    # 即使数据库写入前进程被取消，也能按确定名称清理刚生成的文件。
    for path in allowed_urls.values():
        _safe_unlink(path)
    if extra_file in allowed_urls:
        _safe_unlink(allowed_urls[extra_file])
    _cleanup_hunt_assets(tvid)


def _finalize_requested_delete(tvid: int):
    if _is_active(tvid):
        return
    row = db.one(
        "SELECT id,job_id,video_file,status,billing_status,error "
        "FROM tv_job WHERE id=?",
        (tvid,),
    )
    if not row or row.get("status") != "cancelled" \
            or row.get("billing_status") != "refunded" \
            or row.get("error") != _DELETE_REQUEST_ERROR:
        return
    cleanup_job_assets(tvid, row)
    db.execute(
        "DELETE FROM tv_job WHERE id=? AND status='cancelled' "
        "AND billing_status='refunded' AND error=?",
        (tvid, _DELETE_REQUEST_ERROR),
    )


def _charged_points(row: dict) -> float:
    stored = row.get("billing_points")
    if stored is not None:
        return float(stored)
    if int(row.get("tenant_id") or 1) == 1:
        return 0.0
    from . import billing
    return float(
        (billing.prices().get("text_video") or {"points": 1})["points"]
    )


def settle_failure(tvid: int, message: str,
                   terminal_status: str = "failed") -> bool:
    """原子收口未交付任务，并按创建时快照金额最多退款一次。"""
    if terminal_status not in {"failed", "cancelled"}:
        raise ValueError("invalid text-video terminal status")
    row = db.one(
        "SELECT id,tenant_id,job_id,video_file,status,billing_status,"
        "billing_points FROM tv_job WHERE id=?",
        (tvid,),
    )
    if not row or row.get("billing_status") != "charged":
        return False
    points = _charged_points(row)
    error = (message or "图文转视频执行失败")[:500]
    from . import billing

    def claim(connection):
        changed = connection.execute(
            "UPDATE tv_job SET status=?,billing_status='refunded',"
            "billing_points=COALESCE(billing_points,?),video_file=NULL,"
            "error=?,updated_at=? WHERE id=? AND billing_status='charged' "
            "AND status IN "
            "('pending_charge','queued','running','failed','cancelled')",
            (terminal_status, points, error, time.time(), tvid),
        )
        return changed.rowcount == 1

    label = (billing.prices().get("text_video") or {}).get(
        "label", "text_video")
    settled = billing.refund_amount_if_claimed(
        int(row.get("tenant_id") or 1),
        points,
        claim,
        f"退回:{label} · {error[:160]}",
    )
    if settled:
        cleanup_job_assets(tvid, row)
    return settled


def delete_job(tvid: int, tenant_id: int) -> bool | None:
    """安全删除；在途任务先取消退款，worker 退出后再物理删除。"""
    row = db.one(
        "SELECT * FROM tv_job WHERE id=? AND tenant_id=?",
        (tvid, tenant_id),
    )
    if not row:
        return None

    if row.get("status") == "pending_charge" \
            and row.get("billing_status") == "pending":
        cleanup_job_assets(tvid, row)
        return db.execute(
            "DELETE FROM tv_job WHERE id=? AND tenant_id=? "
            "AND status='pending_charge' AND billing_status='pending'",
            (tvid, tenant_id),
        ) == 1

    if row.get("status") in {"queued", "running", "pending_charge"} \
            and row.get("billing_status") == "charged":
        if not settle_failure(
                tvid, _DELETE_REQUEST_ERROR, terminal_status="cancelled"):
            current = db.one(
                "SELECT * FROM tv_job WHERE id=? AND tenant_id=?",
                (tvid, tenant_id),
            )
            if not current:
                return True
            if current.get("status") in {"queued", "running", "pending_charge"} \
                    and current.get("billing_status") == "charged":
                return False
            row = current
        else:
            row = db.one("SELECT * FROM tv_job WHERE id=?", (tvid,))

    if row and row.get("status") == "cancelled" \
            and row.get("billing_status") == "refunded":
        if row.get("error") != _DELETE_REQUEST_ERROR:
            db.execute(
                "UPDATE tv_job SET error=?,updated_at=? "
                "WHERE id=? AND status='cancelled' AND billing_status='refunded'",
                (_DELETE_REQUEST_ERROR, time.time(), tvid),
            )
        cleanup_job_assets(tvid, row)
        _finalize_requested_delete(tvid)
        return True

    # 已交付/已失败任务没有活跃 worker，可以直接清理产物和记录。若 worker
    # 刚好抢先改变终态，条件 DELETE 失败会让调用方返回冲突而不是误删。
    cleanup_job_assets(tvid, row)
    return db.execute(
        "DELETE FROM tv_job WHERE id=? AND tenant_id=? AND status=? "
        "AND billing_status=?",
        (tvid, tenant_id, row.get("status"), row.get("billing_status")),
    ) == 1


class _Cancelled(Exception):
    pass


async def run_job(tvid: int, broadcast=None):
    _active_enter(tvid)
    try:
        row = db.one("SELECT * FROM tv_job WHERE id=?", (tvid,))
        if not row or row.get("status") != "queued" \
                or row.get("billing_status") != "charged":
            return
        if _BUILD_SEM.locked():
            _steps_append(tvid, "排队中:前面还有别的成片在合成,轮到自动开工…")
        async with _BUILD_SEM:
            # 只有 queued→running 的 CAS 胜者能执行。重复调度、重启恢复和
            # 两个 worker 同时撞上时，其余调用会在产生供应商成本前退出。
            started = db.execute(
                "UPDATE tv_job SET status='running',updated_at=? "
                "WHERE id=? AND status='queued' AND billing_status='charged'",
                (time.time(), tvid),
            )
            if started != 1:
                return
            row = db.one("SELECT * FROM tv_job WHERE id=?", (tvid,))
            if not row:
                return
            await _run_job_inner(
                tvid,
                row,
                db.jloads(row["params_json"], {}),
                row["tenant_id"],
                broadcast,
            )
    finally:
        _active_leave(tvid)
        _finalize_requested_delete(tvid)


async def _run_job_inner(tvid: int, row: dict, p: dict, tid: int, broadcast):
    def progress(msg):
        state = db.one(
            "SELECT status,billing_status FROM tv_job WHERE id=?", (tvid,))
        if not state or state.get("status") != "running" \
                or state.get("billing_status") != "charged":
            raise _Cancelled()
        _steps_append(tvid, msg)
        _safe_broadcast(
            broadcast,
            {"type": "tv_step", "tv_id": tvid, "tenant_id": tid, "msg": msg},
        )

    produced_file = None
    try:
        title = (p.get("title") or "").strip()[:40] or "派活出品"
        progress("开工:整理口播稿…")
        if p.get("topic") and not p.get("script"):
            r = await providers.call_text(
                3,
                f"围绕主题「{p['topic']}」写一段 60-90 秒的短视频口播稿(200-280字):"
                f"口语化、开头有钩子、结尾一句号召;只输出口播稿正文。",
                timeout=180)
            p["script"] = r["text"].strip().strip("「」\"“”")
        script = p.get("script") or await make_script(title, p.get("body") or "")
        # V25.3:混剪模式——用户自己的 vlog 片段当画面(原图文成片模式原样保留)
        if p.get("mode") == "clips":
            clips = []
            for stored in p.get("clips") or []:
                owned = owned_clip_path(tid, stored)
                if owned:
                    clips.append({"path": owned})
            if not clips:
                raise ValueError("没有可用的视频片段,先在混剪页上传")
            out_dir = "tv"
            file = await build_from_clips(tvid, tid, title, script, clips,
                                          p.get("voice_id") or DEFAULT_VOICE, out_dir, progress,
                                          bgm=p.get("bgm") or "warm",
                                          end_text=p.get("end_text") or "")
            produced_file = file
        else:
            images = []
            for f in p.get("images") or []:      # "/files/..." → 本地路径
                from . import assetfiles
                try:
                    lp = assetfiles.resolve_tenant_asset(
                        f, tid, expected_job_id=row.get("job_id"),
                        allowed_extensions=(".png", ".jpg", ".jpeg", ".webp"),
                    )
                except assetfiles.AssetAccessError:
                    continue
                images.append(lp)
            if not images and p.get("image_query"):
                progress(f"没有现成配图,按「{p['image_query']}」全网抓图…")
                from . import imagehunt
                try:
                    cands = await imagehunt.search(p["image_query"], 12)
                    got = await imagehunt._grab_first_ok(cands, 4)
                    for i, (data, _c) in enumerate(got):
                        lp = os.path.join(ASSET_DIR, "tv", f"hunt_{tvid}_{i}.jpg")
                        os.makedirs(os.path.dirname(lp), exist_ok=True)
                        with open(lp, "wb") as f:
                            f.write(data)
                        images.append(lp)
                    progress(f"抓到 {len(images)} 张真实图")
                except Exception:
                    progress("抓图失败，改用纯色底")
            out_dir = f"job{row['job_id']}" if row.get("job_id") else "tv"
            produced_file = await build(
                tvid,
                tid,
                title,
                script,
                images,
                p.get("voice_id") or DEFAULT_VOICE,
                out_dir,
                progress,
                bgm=p.get("bgm") or "warm",
                end_text=p.get("end_text") or "",
            )

        delivered = db.execute(
            "UPDATE tv_job SET status='done',billing_status='succeeded',"
            "video_file=?,script=?,error=NULL,updated_at=? "
            "WHERE id=? AND status='running' AND billing_status='charged'",
            (produced_file, script, time.time(), tvid),
        )
        if delivered != 1:
            cleanup_job_assets(tvid, row, produced_file)
            raise _Cancelled()
        _cleanup_hunt_assets(tvid)

        # 通知、WebSocket 推送是旁路。数据库中的交付与计费终态已经在上面的
        # 单条条件 UPDATE 中同时提交，旁路故障只能记日志，不能触发退款。
        from . import notify
        try:
            notify.push(tid, "video", {"title": title, "file": produced_file})
        except Exception as exc:
            log.error(
                "tv_job %s notification failed error_type=%s",
                tvid,
                type(exc).__name__,
            )
    except _Cancelled:
        cleanup_job_assets(tvid, row, produced_file)
        log.info("tv_job %s cancelled before delivery commit", tvid)
    except Exception as e:
        log.error(
            "tv_job %s failed error_type=%s",
            tvid,
            type(e).__name__,
        )
        cleanup_job_assets(tvid, row, produced_file)
        public_error = providers.public_failure_message(
            e,
            "成片生成失败，点数已自动退回；可从原任务免费重试",
        )
        try:
            settle_failure(tvid, public_error, terminal_status="failed")
        except Exception as settlement_exc:
            log.error(
                "tv_job %s failure settlement failed error_type=%s",
                tvid,
                type(settlement_exc).__name__,
            )
    finally:
        # 进度是异步落库的;结束前冲刷,保证终态可见时步骤记录已完整。
        await db.adrain()
        _safe_broadcast(
            broadcast,
            {"type": "tv_done", "tv_id": tvid, "tenant_id": tid},
        )


def resume_pending(broadcast):
    """重启恢复：清 pending；queued 重跑；running 原子退款；孤儿文件清理。"""
    for row in db.q(
            "SELECT * FROM tv_job "
            "WHERE status='pending_charge' AND billing_status='pending'"):
        cleanup_job_assets(row["id"], row)
        db.execute(
            "DELETE FROM tv_job WHERE id=? AND status='pending_charge' "
            "AND billing_status='pending'",
            (row["id"],),
        )

    # failed/cancelled 已结算任务可能在文件生成后、清理前宕机；启动时补清理。
    for row in db.q(
            "SELECT * FROM tv_job WHERE status IN ('failed','cancelled') "
            "AND billing_status='refunded'"):
        cleanup_job_assets(row["id"], row)
        if row.get("status") == "cancelled" \
                and row.get("error") == _DELETE_REQUEST_ERROR:
            _finalize_requested_delete(row["id"])

    # 若进程在旧代码“先写失败、再退款”的窗口中退出，新工单保存的扣费快照
    # 能让启动恢复补做一次精确退款；没有快照的历史行不会凭空增发点数。
    for row in db.q(
            "SELECT * FROM tv_job WHERE status IN ('failed','cancelled') "
            "AND billing_status='charged' AND billing_points IS NOT NULL"):
        try:
            settle_failure(
                row["id"],
                row.get("error") or "服务重启补偿未完成结算",
                terminal_status=row["status"],
            )
        except Exception as exc:
            log.error(
                "tv_job %s terminal settlement recovery failed error_type=%s",
                row["id"],
                type(exc).__name__,
            )

    for row in db.q(
            "SELECT * FROM tv_job WHERE status IN ('queued','running') "
            "AND billing_status='charged'"):
        if row["status"] == "queued":
            asyncio.create_task(run_job(row["id"], broadcast))
            continue
        try:
            settle_failure(
                row["id"],
                "服务重启中断,已自动退点,请重新生成",
                terminal_status="failed",
            )
        except Exception as exc:
            log.error(
                "tv_job %s restart settlement failed error_type=%s",
                row["id"],
                type(exc).__name__,
            )
        _safe_broadcast(
            broadcast,
            {"type": "tv_done", "tv_id": row["id"],
             "tenant_id": row["tenant_id"]},
        )
