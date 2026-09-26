"""现场照片证据：店员拍照 → 服务端统一压缩、打水印、落盘。

第 2 期"真人派活闭环"里，派给店员的任务(staff_task)和开闭店清单(checklist_run)
都要求"拍照为证"。照片的可信度来自服务端，而不是手机：

- 拍摄时间以服务器收到的时间(北京时间)为准，写进水印和 ``received_at``；
  手机相册里的旧照片、EXIF 里伪造的时间都不采信(EXIF 整体丢弃)。
- 统一重编码成 JPEG，长边不超过 1600 像素，去掉一切元数据。
- 水印：底部一条半透明黑底白字：「门店名 · 2026-09-25 14:32 · 姓名」。

存储路径：``data/assets/staff/{tid}/{branch_id}/{32hex}.jpg``，对外 URL 为
``/files/staff/{tid}/{branch_id}/{32hex}.jpg``。访问控制见 ``file_access_scope``：
按门店所属行业要求板块权限，并且经理/员工只能看自己负责门店的照片。

本模块不写数据库；调用方(stafftask / checklist)把返回的字典存进自己的表。
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import time
from typing import Any

from . import assetfiles, db, timeutil

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_EDGE = 1600
JPEG_QUALITY = 82
_CJK_FONT_CANDIDATES = (
    os.environ.get("PAIHUO_WATERMARK_FONT") or "",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)
STAFF_FILE_RE = re.compile(
    r"^/files/staff/(\d+)/(\d+)/([a-f0-9]{32}\.jpg)$"
)


class PhotoError(ValueError):
    """照片不合格(格式/大小/内容)，文案直接给店员看。"""


def _font(size: int):
    from PIL import ImageFont
    for path in _CJK_FONT_CANDIDATES:
        if path and os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size), True
            except OSError:
                continue
    return ImageFont.load_default(), False


def watermark_text(branch_name: str, person_name: str, ts: float) -> str:
    stamp = timeutil.now_cn(ts).strftime("%Y-%m-%d %H:%M")
    parts = [str(branch_name or "").strip()[:20], stamp,
             str(person_name or "").strip()[:12]]
    return " · ".join(p for p in parts if p)


def _render(data: bytes, text: str) -> tuple[bytes, int, int]:
    from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
    if not data:
        raise PhotoError("没有收到照片，请重新拍一张")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PhotoError("照片太大了(超过 12MB)，请重新拍一张")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise PhotoError("这不是能识别的照片，请用相机重新拍一张") from exc
    if img.width * img.height > 64_000_000:
        raise PhotoError("照片像素太高，请重新拍一张")
    # 按 EXIF 方向摆正后，丢弃全部元数据
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB",):
        img = img.convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    w, h = img.size
    size = max(14, int(min(w, h) * 0.035))
    font, cjk = _font(size)
    label = text if cjk else text.encode("ascii", "ignore").decode() or text
    draw = ImageDraw.Draw(img, "RGBA")
    pad = max(6, size // 2)
    try:
        box = draw.textbbox((0, 0), label, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
    except Exception:  # 极老版本 PIL 没有 textbbox
        tw, th = len(label) * size // 2, size
    band = th + pad * 2
    draw.rectangle([0, h - band, w, h], fill=(0, 0, 0, 150))
    draw.text((pad, h - band + pad - 1), label, font=font, fill=(255, 255, 255, 235))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), w, h


def _write_new(path: str, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o640)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def store_photo(
    tid: int,
    branch_id: int,
    data: bytes,
    *,
    branch_name: str = "",
    person_name: str = "",
    now: float | None = None,
    asset_root: str | None = None,
) -> dict[str, Any]:
    """压缩 + 水印 + 落盘，返回可直接存库的元数据。

    返回：storage_key, url, sha256, mime_type, byte_size, width, height,
    received_at, watermark_text。
    """
    tid, branch_id = int(tid), int(branch_id)
    if tid < 1 or branch_id < 1:
        raise PhotoError("照片要对应到具体门店")
    ts = time.time() if now is None else float(now)
    text = watermark_text(branch_name, person_name, ts)
    payload, width, height = _render(data, text)
    root = os.path.realpath(asset_root or assetfiles.ASSET_ROOT)
    directory = os.path.join(root, "staff", str(tid), str(branch_id))
    os.makedirs(directory, mode=0o750, exist_ok=True)
    if os.path.realpath(directory) != directory:
        raise PhotoError("照片目录不安全")
    name = os.urandom(16).hex() + ".jpg"
    path = os.path.join(directory, name)
    _write_new(path, payload)
    key = f"staff/{tid}/{branch_id}/{name}"
    return {
        "storage_key": key,
        "url": "/files/" + key,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mime_type": "image/jpeg",
        "byte_size": len(payload),
        "width": width,
        "height": height,
        "received_at": ts,
        "watermark_text": text,
    }


def remove_photo(storage_key: str, *, asset_root: str | None = None) -> None:
    """清掉一张未被引用的照片(调用方负责确认没有引用)。"""
    key = str(storage_key or "")
    if not STAFF_FILE_RE.match("/files/" + key):
        return
    root = os.path.realpath(asset_root or assetfiles.ASSET_ROOT)
    path = os.path.abspath(os.path.join(root, key))
    if os.path.commonpath((root, path)) != root or os.path.islink(path):
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def file_scope(path: str) -> dict | None:
    """``/files/staff/...`` 的归属：租户 + 门店所属行业(作为必需板块)。

    不是 staff 照片路径返回 None；是但门店不存在时返回租户 0(拒绝)。
    """
    match = STAFF_FILE_RE.match(path)
    if not match:
        return None
    tenant_id, branch_id = int(match.group(1)), int(match.group(2))
    row = db.one(
        "SELECT tenant_id,industry_key FROM store_branch WHERE id=? AND tenant_id=?",
        (branch_id, tenant_id),
    )
    industry = str((row or {}).get("industry_key") or "").strip()
    return {
        "tenant_id": int((row or {}).get("tenant_id") or 0),
        "industry_key": industry or None,
        "required_module": industry,
    }


def branch_visible(tid: int, uid: int, branch_id: int) -> bool:
    """经理/员工只能看自己负责门店的照片；老板/总监看全部。"""
    from . import inspection
    user = db.one(
        "SELECT id,tenant_id,role,job_title,enabled FROM users WHERE id=?",
        (int(uid),),
    )
    if not user or not int(user.get("enabled") or 0) \
            or int(user.get("tenant_id") or 0) != int(tid):
        return False
    if inspection.sees_all_branches(user):
        return True
    return bool(db.one(
        "SELECT 1 AS ok FROM user_branch WHERE tenant_id=? AND user_id=? AND branch_id=?",
        (int(tid), int(uid), int(branch_id)),
    ))
