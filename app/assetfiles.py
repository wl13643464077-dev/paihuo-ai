"""Tenant-aware resolution for files stored below ``data/assets``.

Any code that opens a client-supplied ``/files/...`` value must use this
module.  A path being inside the global assets directory is not sufficient:
the database owner and, for job exports, the exact originating job must also
match.
"""

from __future__ import annotations

import os
import posixpath
import re
from urllib.parse import unquote, urlsplit

from . import db


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET_ROOT = os.path.join(ROOT, "data", "assets")

_JOB_RE = re.compile(r"^/files/job(\d+)(?:/|$)")
_AVATAR_RE = re.compile(r"^/files/avatar/avatar_(\d+)\.mp4$")
_TVCLIP_RE = re.compile(r"^/files/tvclips/(\d+)(?:/|$)")
_TV_RE = re.compile(r"^/files/tv/tv_(\d+)\.mp4$")
_PUB_RE = re.compile(r"^/files/pub/fail_(\d+)\.png$")
_TOOLS_RE = re.compile(r"^/files/tools/(\d+)(?:/|$)")
_INSPECTION_RE = re.compile(
    r"^/files/inspections/(\d+)/(\d+)/([a-f0-9]{32}\.(?:jpg|png|webp))$"
)
_DANGEROUS_SCHEME_RE = re.compile(r"^\s*(?:javascript|data|vbscript|file):", re.I)
_ASSET_VALUE_FIELDS = {
    "file", "src", "thumb", "preview", "shot", "deck",
    "video", "video_file", "audio_file", "output_url",
}
_EXTERNAL_URL_FIELDS = {
    "url", "href", "source_url", "page_url", "upload_url",
}


class AssetAccessError(ValueError):
    pass


def canonical_file_url(value: str) -> str:
    if not isinstance(value, str):
        raise AssetAccessError("素材路径无效")
    decoded = unquote(value.strip())
    if not decoded.startswith("/files/") or decoded.startswith("/files/avatar-public/"):
        raise AssetAccessError("素材路径无效")
    if (
        "\x00" in decoded
        or "\\" in decoded
        or "//" in decoded
        or any(part in (".", "..") for part in decoded.split("/"))
    ):
        raise AssetAccessError("素材路径无效")
    normalized = posixpath.normpath(decoded)
    if normalized != decoded.rstrip("/") or not normalized.startswith("/files/"):
        raise AssetAccessError("素材路径无效")
    return normalized


def file_access_scope(value: str) -> dict:
    """Return the authoritative tenant and narrowest module for an asset.

    Inspection evidence is narrower than ordinary tenant-owned output: a
    member may only read photos for an industry present in their module scope.
    Resolve all values together so deleting the visit cannot race separate
    ownership and permission lookups.  Unknown families deliberately resolve
    to tenant ``0`` and no module so callers fail closed.
    """
    try:
        path = canonical_file_url(value)
    except AssetAccessError:
        return {
            "tenant_id": 0,
            "industry_key": None,
            "required_module": "",
        }
    match = _JOB_RE.match(path)
    if match:
        row = db.one("SELECT tenant_id FROM job WHERE id=?", (int(match.group(1)),))
        return {
            "tenant_id": int((row or {}).get("tenant_id") or 0),
            "industry_key": None,
            "required_module": "content",
        }
    match = _AVATAR_RE.match(path)
    if match:
        row = db.one(
            "SELECT tenant_id FROM avatar_job WHERE id=?", (int(match.group(1)),)
        )
        return {
            "tenant_id": int((row or {}).get("tenant_id") or 0),
            "industry_key": None,
            "required_module": "avatar",
        }
    match = _TVCLIP_RE.match(path)
    if match:
        return {
            "tenant_id": int(match.group(1)),
            "industry_key": None,
            "required_module": "content",
        }
    match = _TV_RE.match(path)
    if match:
        row = db.one("SELECT tenant_id FROM tv_job WHERE id=?", (int(match.group(1)),))
        return {
            "tenant_id": int((row or {}).get("tenant_id") or 0),
            "industry_key": None,
            "required_module": "content",
        }
    match = _PUB_RE.match(path)
    if match:
        row = db.one("SELECT tenant_id FROM pub_task WHERE id=?", (int(match.group(1)),))
        return {
            "tenant_id": int((row or {}).get("tenant_id") or 0),
            "industry_key": None,
            # Matrix publish creation, retries and history are all guarded by
            # the content module; its failure screenshot carries that scope.
            "required_module": "content",
        }
    match = _TOOLS_RE.match(path)
    if match:
        return {
            "tenant_id": int(match.group(1)),
            "industry_key": None,
            # The only writers for this namespace are product-shot and the
            # image leg of photo-factory, both content-module operations.
            "required_module": "content",
        }
    match = _INSPECTION_RE.match(path)
    if match:
        tenant_id, visit_id = int(match.group(1)), int(match.group(2))
        storage_key = path.removeprefix("/files/")
        row = db.one(
            "SELECT p.tenant_id,v.industry_key FROM inspection_photo p "
            "JOIN inspection_visit v ON v.id=p.visit_id "
            "AND v.tenant_id=p.tenant_id WHERE p.tenant_id=? AND p.visit_id=? "
            "AND p.storage_key=? AND v.deleted_at IS NULL "
            "AND v.industry_key!='' LIMIT 1",
            (tenant_id, visit_id, storage_key),
        )
        return {
            "tenant_id": int((row or {}).get("tenant_id") or 0),
            "industry_key": str((row or {}).get("industry_key") or "") or None,
            "required_module": str(
                (row or {}).get("industry_key") or ""
            ).strip(),
        }
    return {
        "tenant_id": 0,
        "industry_key": None,
        "required_module": "",
    }


def file_owner_tid(value: str) -> int:
    """Return the single database-backed tenant owner, or ``0`` if unknown."""
    return int(file_access_scope(value).get("tenant_id") or 0)


def file_required_module(value: str) -> str:
    """Return the narrowest module/industry required to read an asset.

    Kept as a compatibility helper for callers that only need this field.  The
    HTTP middleware consumes :func:`file_access_scope` directly so ownership
    and module authorization are based on one authoritative resolution.
    """
    return str(file_access_scope(value).get("required_module") or "").strip()


def file_job_id(value: str) -> int:
    """Return the originating content job when that relation is provable."""
    try:
        path = canonical_file_url(value)
    except AssetAccessError:
        return 0
    match = _JOB_RE.match(path)
    if match:
        return int(match.group(1))
    match = _TV_RE.match(path)
    if match:
        row = db.one("SELECT job_id FROM tv_job WHERE id=?", (int(match.group(1)),))
        return int((row or {}).get("job_id") or 0)
    return 0


def resolve_tenant_asset(
    value: str,
    tenant_id: int,
    *,
    expected_job_id: int | None = None,
    allowed_extensions: tuple[str, ...] | None = None,
) -> str:
    """Resolve a URL to a real local file after tenant and job checks."""
    path = canonical_file_url(value)
    tenant_id = int(tenant_id)
    owner = file_owner_tid(path)
    if not owner or owner != tenant_id:
        raise AssetAccessError("素材不存在或不属于当前企业")
    if expected_job_id is not None:
        if file_job_id(path) != int(expected_job_id):
            raise AssetAccessError("素材不属于当前工单")

    root = os.path.realpath(ASSET_ROOT)
    candidate = os.path.realpath(
        os.path.join(root, path.removeprefix("/files/"))
    )
    try:
        inside = os.path.commonpath((root, candidate)) == root
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(candidate):
        raise AssetAccessError("素材文件不存在或路径无效")
    if allowed_extensions:
        allowed = tuple(ext.lower() for ext in allowed_extensions)
        if os.path.splitext(candidate)[1].lower() not in allowed:
            raise AssetAccessError("素材文件类型不受支持")
    return candidate


def validate_embedded_file_urls(
    value,
    tenant_id: int,
    *,
    expected_job_id: int | None = None,
    _field: str = "",
):
    """Validate asset and external URL fields in recursively edited output."""
    if isinstance(value, dict):
        for key, child in value.items():
            validate_embedded_file_urls(
                child,
                tenant_id,
                expected_job_id=expected_job_id,
                _field=str(key).lower(),
            )
    elif isinstance(value, (list, tuple)):
        for child in value:
            validate_embedded_file_urls(
                child,
                tenant_id,
                expected_job_id=expected_job_id,
                _field=_field,
            )
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return
        if _DANGEROUS_SCHEME_RE.match(text):
            raise AssetAccessError("不允许的链接协议")
        if _field in _ASSET_VALUE_FIELDS or text.startswith("/files/"):
            resolve_tenant_asset(
                text, tenant_id, expected_job_id=expected_job_id
            )
            return
        if _field in _EXTERNAL_URL_FIELDS:
            parsed = urlsplit(text)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise AssetAccessError("外部来源链接只允许 http/https")
