"""Auditable two-phase XLSX import for inspection branch master data.

``preview_import`` parses in a resource-contained child process and persists only
an import ledger plus staged rows. ``commit_import`` rechecks current branch state
under ``BEGIN IMMEDIATE`` and applies the whole workbook atomically.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping
import zipfile
import zlib
import xml.etree.ElementTree as ET

from cryptography.fernet import Fernet, InvalidToken

from . import auth, db, inspection, inspectionstandards


MAX_FILE_MIB = 16
MAX_FILE_BYTES = MAX_FILE_MIB * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 512
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_ENTRY_BYTES = 96 * 1024 * 1024
MAX_RELATIONSHIP_XML_BYTES = 2 * 1024 * 1024
MAX_CONTENT_TYPES_XML_BYTES = 1024 * 1024
MAX_SHARED_STRINGS_XML_BYTES = 32 * 1024 * 1024
MAX_STYLES_XML_BYTES = 4 * 1024 * 1024
MAX_WORKBOOK_XML_BYTES = 2 * 1024 * 1024
MAX_THEME_XML_BYTES = 4 * 1024 * 1024
MAX_OTHER_XML_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_XML_ELEMENTS = 4_096
MAX_PACKAGE_XML_DEPTH = 24
MAX_XML_ATTRIBUTES_PER_ELEMENT = 32
MAX_XML_ATTRIBUTE_CHARS = 512 * 1024
WORKSHEET_SCAN_CHUNK_BYTES = 64 * 1024
WORKSHEET_SCAN_OVERLAP_BYTES = 512
MAX_WORKER_STDOUT_BYTES = 96 * 1024 * 1024
MAX_WORKER_STDERR_BYTES = 64 * 1024
PARSER_TIMEOUT_SECONDS = 75
PREVIEW_TTL_SECONDS = 24 * 60 * 60
EXPIRED_CLEANUP_BATCH = 8
# Lifecycle cleanup is intentionally bounded per invocation.  Startup and the
# scheduler both call the cross-tenant sweep; a slow/large tenant can therefore
# never monopolize the request worker or turn one tick into an unbounded scan.
RETENTION_CLEANUP_BATCH = 32
ARCHIVE_FORMAT_VERSION = 2
# The isolated parser already caps stdout at 96 MiB.  The archive adds only
# bounded row/action keys, so 128 MiB leaves headroom without permitting a
# corrupted database blob to expand into a half-gigabyte allocation.
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_ACTIVE_PREVIEWS_PER_TENANT = 4
MAX_ACTIVE_PREVIEW_ROWS_PER_TENANT = 120_000
MAX_ACTIVE_PREVIEW_BYTES_PER_TENANT = 128 * 1024 * 1024
MAX_RETIRED_PREVIEWS_PER_TENANT = 8
MAX_RETIRED_PREVIEW_ROWS_PER_TENANT = 240_000
MAX_RETIRED_PREVIEW_BYTES_PER_TENANT = 256 * 1024 * 1024
DEFAULT_IMPORT_PAGE_LIMIT = 50
MAX_IMPORT_PAGE_LIMIT = 200
_BUSINESS_ROW_OFFSET = 100_000
_SQL_IN_CHUNK = 400
_REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_STORE_CODE_RE = re.compile(r"^[^\s/\\]{1,40}$")
_METRIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STAGING_ENCRYPTED_PREFIX = "inspection-import:v1:"
_FORMULA_TAG_RE = re.compile(
    rb"<(?:[A-Za-z_][A-Za-z0-9_.-]*:)?f(?=[\x20\t\r\n/>])", re.I,
)

BRANCH_MAP = {
    "门店编号*": "store_code", "店名*": "name", "区域*": "region",
    "省": "province", "市": "city", "区/县": "district", "详细地址*": "address",
    "店长姓名": "manager_name", "店长工号": "manager_employee_no",
    "店长手机号": "manager_phone", "门店类型": "store_type",
    "开业日期": "opened_on", "营业面积㎡": "area_sqm",
    "座位数/房间数/工位数": "seat_count",
    "经度": "longitude", "纬度": "latitude", "启用状态*": "active",
    "备注": "remark",
}
STORE_FIELDS = tuple(BRANCH_MAP.values())
BUSINESS_PUBLIC_FIELDS = (
    "store_code", "metric_key", "period_start", "period_end", "value",
    "unit", "source_ref", "remark",
)


class ImportContractError(ValueError):
    """Stable failure safe to translate into a 4xx response."""

    def __init__(self, code: str, message: str = "门店导入失败") -> None:
        self.code = str(code)
        self.safe_message = str(message)
        super().__init__(f"{self.code}: {self.safe_message}")


def _fail(code: str, message: str = "门店导入失败") -> None:
    raise ImportContractError(code, message)


class _StagingCipherError(ValueError):
    pass


def _staging_cipher() -> Fernet:
    material = hashlib.sha256(
        b"paihuo-inspection-import-v1\0" + auth._secret()
    ).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def _staging_binding(
    tenant_id: int, industry: str, request_key: str, purpose: str,
) -> bytes:
    return (
        f"{int(tenant_id)}\0{industry}\0{request_key}\0{purpose}"
    ).encode("utf-8")


def _encrypt_staging(
    plaintext: str, *, tenant_id: int, industry: str, request_key: str,
    purpose: str,
) -> str:
    binding = _staging_binding(tenant_id, industry, request_key, purpose)
    token = _staging_cipher().encrypt(
        binding + b"\0" + plaintext.encode("utf-8")
    ).decode("ascii")
    return _STAGING_ENCRYPTED_PREFIX + token


def _decrypt_staging(
    stored: str, *, tenant_id: int, industry: str, request_key: str,
    purpose: str,
) -> str:
    if not isinstance(stored, str) or not stored.startswith(
        _STAGING_ENCRYPTED_PREFIX
    ):
        raise _StagingCipherError("staging payload is not encrypted")
    binding = _staging_binding(tenant_id, industry, request_key, purpose)
    try:
        payload = _staging_cipher().decrypt(
            stored[len(_STAGING_ENCRYPTED_PREFIX):].encode("ascii")
        )
        stored_binding, plaintext = payload.rsplit(b"\0", 1)
        if stored_binding != binding:
            raise _StagingCipherError("staging binding mismatch")
        return plaintext.decode("utf-8")
    except (
        InvalidToken, UnicodeDecodeError, UnicodeEncodeError, ValueError,
    ) as exc:
        if isinstance(exc, _StagingCipherError):
            raise
        raise _StagingCipherError("staging payload cannot be authenticated") from exc


def _text(value: Any, limit: int, *, required: bool = False) -> str:
    if value is None:
        clean = ""
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        clean = str(value).strip()
    else:
        _fail("ROW_INVALID", "字段格式无效")
    if required and not clean:
        _fail("ROW_INVALID", "必填字段缺失")
    if len(clean) > limit or _CONTROL_RE.search(clean):
        _fail("ROW_INVALID", "字段长度或字符无效")
    return clean


def _date(value: Any, *, required: bool = False) -> str | None:
    text = _text(value, 32, required=required)
    if not text:
        return None
    try:
        # The worker serializes Excel date cells with ISO format.
        parsed = dt.date.fromisoformat(text[:10])
    except ValueError:
        _fail("ROW_INVALID", "日期必须为 YYYY-MM-DD")
    return parsed.isoformat()


def _number(
    value: Any, *, minimum: float, maximum: float, integer: bool = False,
    required: bool = False,
) -> int | float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            _fail("ROW_INVALID", "数值必填")
        return None
    if isinstance(value, bool):
        _fail("ROW_INVALID", "数值格式无效")
    try:
        number = float(value)
    except (TypeError, ValueError):
        _fail("ROW_INVALID", "数值格式无效")
    if not math.isfinite(number) or not minimum <= number <= maximum:
        _fail("ROW_INVALID", "数值超出范围")
    if integer:
        if not number.is_integer():
            _fail("ROW_INVALID", "数值必须为整数")
        return int(number)
    return number


def _normalize_branch(raw: Mapping[str, Any]) -> dict:
    translated = {target: raw.get(source) for source, target in BRANCH_MAP.items()}
    code = _text(translated["store_code"], 40, required=True)
    if not _STORE_CODE_RE.fullmatch(code):
        _fail("ROW_INVALID", "门店编号格式无效")
    status = _text(translated["active"], 16, required=True).lower()
    active_values = {
        "1": 1, "true": 1, "yes": 1, "y": 1, "启用": 1, "是": 1,
        "0": 0, "false": 0, "no": 0, "n": 0, "停用": 0, "否": 0,
    }
    if status not in active_values:
        _fail("ROW_INVALID", "启用状态无效")
    phone = _text(translated["manager_phone"], 32)
    if phone:
        phone_shape = re.fullmatch(r"\+?[0-9][0-9() -]{5,30}[0-9]", phone)
        digits = re.sub(r"\D", "", phone)
        if not phone_shape or len(digits) < 7 or len(digits) > 15:
            _fail("ROW_INVALID", "店长手机号格式无效")
    return {
        "store_code": code,
        "name": _text(translated["name"], 80, required=True),
        "region": _text(translated["region"], 60, required=True),
        "province": _text(translated["province"], 60),
        "city": _text(translated["city"], 60),
        "district": _text(translated["district"], 60),
        "address": _text(translated["address"], 240, required=True),
        "manager_name": _text(translated["manager_name"], 40),
        "manager_employee_no": _text(translated["manager_employee_no"], 40),
        "manager_phone": phone,
        "store_type": _text(translated["store_type"], 40),
        "opened_on": _date(translated["opened_on"]),
        "area_sqm": _number(translated["area_sqm"], minimum=0, maximum=10_000_000),
        "seat_count": _number(translated["seat_count"], minimum=0, maximum=1_000_000, integer=True),
        "longitude": _number(translated["longitude"], minimum=-180, maximum=180),
        "latitude": _number(translated["latitude"], minimum=-90, maximum=90),
        "active": active_values[status],
        "remark": _text(translated["remark"], 500),
    }


def _normalize_business(
    raw: Mapping[str, Any], metric_rules: Mapping[str, set[str]],
) -> dict:
    translated = {
        "store_code": raw.get("门店编号*"),
        "metric_key": raw.get("指标编码*"),
        "period_start": raw.get("期间开始*"),
        "period_end": raw.get("期间结束*"),
        "value": raw.get("数值*"),
        "unit": raw.get("单位*"),
        "source_ref": raw.get("数据来源*"),
        "remark": raw.get("备注"),
    }
    try:
        code = _text(translated["store_code"], 40, required=True)
    except ImportContractError:
        _fail("BUSINESS_VALUE_INVALID", "经营数据门店编号无效")
    if not _STORE_CODE_RE.fullmatch(code):
        _fail("BUSINESS_VALUE_INVALID", "经营数据门店编号无效")
    metric = _text(translated["metric_key"], 80, required=True)
    if not _METRIC_RE.fullmatch(metric) or metric not in metric_rules:
        _fail("BUSINESS_VALUE_INVALID", "指标编码格式无效")
    start = _date(translated["period_start"], required=True)
    end = _date(translated["period_end"], required=True)
    if start > end:
        _fail("BUSINESS_VALUE_INVALID", "经营数据周期无效")
    unit = _text(translated["unit"], 40, required=True)
    if unit not in metric_rules[metric]:
        _fail("BUSINESS_UNIT_INVALID", "单位与指标口径不一致")
    return {
        "store_code": code,
        "metric_key": metric,
        "period_start": start,
        "period_end": end,
        "value": _number(translated["value"], minimum=-1e15, maximum=1e15, required=True),
        "unit": unit,
        "source_ref": _text(translated["source_ref"], 160, required=True),
        "remark": _text(translated["remark"], 300),
    }


def _mask_middle(value: str, *, phone: bool = False) -> str:
    if not value:
        return ""
    if phone:
        digits = re.sub(r"\D", "", value)
        if len(digits) <= 7:
            # Seven digits is the accepted minimum.  Keeping 3+4 would expose
            # every digit once the mask symbols are removed, so short numbers
            # deliberately retain only two digits at each edge.
            return digits[:2] + "***" + digits[-2:]
        return digits[:3] + "****" + digits[-4:]
    if len(value) == 1:
        return "*"
    if len(value) == 2:
        return value[0] + "*"
    return value[0] + "***" + value[-1]


def _masked(payload: Mapping[str, Any]) -> dict:
    result = dict(payload)
    result["manager_name"] = _mask_middle(str(result.get("manager_name") or ""))
    result["manager_employee_no"] = _mask_middle(str(result.get("manager_employee_no") or ""))
    result["manager_phone"] = _mask_middle(str(result.get("manager_phone") or ""), phone=True)
    return result


def _safe_error_store_code(value: Any) -> str:
    """Produce a non-throwing, bounded identifier for an invalid business row."""
    if value is None or isinstance(value, bool):
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    try:
        clean = _CONTROL_RE.sub("", str(value)).strip()
    except (TypeError, ValueError):
        return ""
    if len(clean) <= 40:
        return clean
    # Preserve enough context to find the spreadsheet row without echoing an
    # arbitrarily long attacker-controlled identifier into responses/ledgers.
    return clean[:24] + "…" + clean[-8:]


def _clean_filename(filename: str) -> str:
    if not isinstance(filename, str) or Path(filename).name != filename:
        _fail("FILE_TYPE_UNSUPPORTED", "只支持 .xlsx 文件")
    if Path(filename).suffix.lower() != ".xlsx" or len(filename) > 180:
        _fail("FILE_TYPE_UNSUPPORTED", "只支持 .xlsx 文件")
    return filename


def _catalog_contract(industry: str) -> dict[str, str]:
    """Return the exact standards identity frozen by a preview ledger."""
    try:
        summary = inspectionstandards.version_summary(industry)
    except inspectionstandards.InspectionStandardError:
        _fail("METRIC_CATALOG_UNAVAILABLE", "该行业暂无经营指标口径")
    version = _text(summary.get("catalog_version"), 40, required=True)
    digest = _text(summary.get("sha256"), 64, required=True).lower()
    if not _SHA256_RE.fullmatch(digest):
        _fail("METRIC_CATALOG_UNAVAILABLE", "该行业经营指标口径无效")
    return {"catalog_version": version, "catalog_sha256": digest}


def _scan_package_xml(stream, *, relationships: bool) -> None:
    """Incrementally validate small OOXML metadata parts.

    Relationship attributes need semantic XML decoding (for whitespace and
    character references), but parsing an unrestricted relationship tree in the
    web process is itself a denial-of-service primitive.  iterparse plus explicit
    node/depth/attribute budgets keeps that semantic check bounded.
    """
    depth = 0
    elements = 0
    attribute_chars = 0
    try:
        events = ET.iterparse(stream, events=("start", "end"))
        for event, element in events:
            if event == "start":
                depth += 1
                elements += 1
                if (
                    depth > MAX_PACKAGE_XML_DEPTH
                    or elements > MAX_PACKAGE_XML_ELEMENTS
                    or len(element.attrib) > MAX_XML_ATTRIBUTES_PER_ELEMENT
                ):
                    _fail(
                        "XLSX_XML_LIMIT_EXCEEDED",
                        "XLSX XML 元数据规模异常",
                    )
                attributes = {
                    str(key).rsplit("}", 1)[-1].lower(): str(value).strip()
                    for key, value in element.attrib.items()
                }
                attribute_chars += sum(
                    len(str(key)) + len(str(value))
                    for key, value in element.attrib.items()
                )
                if attribute_chars > MAX_XML_ATTRIBUTE_CHARS or any(
                    len(value) > 4_096 for value in attributes.values()
                ):
                    _fail(
                        "XLSX_XML_LIMIT_EXCEEDED",
                        "XLSX XML 元数据规模异常",
                    )
                if relationships:
                    target_mode = attributes.get("targetmode", "").lower()
                    target = attributes.get("target", "").lower()
                    if (
                        target_mode == "external"
                        or target.startswith((
                            "http://", "https://", "ftp://", "file:",
                            "mailto:", "//", "\\\\",
                        ))
                    ):
                        _fail(
                            "XLSX_EXTERNAL_LINK_FORBIDDEN",
                            "XLSX 不允许外部链接",
                        )
                elif any(
                    "vbaproject" in value.lower()
                    or "macroenabled" in value.lower()
                    for value in attributes.values()
                ):
                    _fail("XLSX_MACRO_FORBIDDEN", "XLSX 不允许宏")
                continue

            if len(element.text or "") > 4_096 or len(element.tail or "") > 4_096:
                _fail(
                    "XLSX_XML_LIMIT_EXCEEDED",
                    "XLSX XML 元数据规模异常",
                )
            element.clear()
            depth -= 1
        if depth != 0:
            _fail("XLSX_INVALID", "XLSX XML 元数据无效")
    except ImportContractError:
        raise
    except ET.ParseError:
        _fail("XLSX_INVALID", "XLSX XML 元数据无效")


def _scan_worksheet_formulas(stream) -> None:
    """Scan worksheet XML with constant memory instead of archive.read()."""
    overlap = b""
    while True:
        chunk = stream.read(WORKSHEET_SCAN_CHUNK_BYTES)
        if not chunk:
            break
        window = overlap + chunk
        if _FORMULA_TAG_RE.search(window):
            _fail("XLSX_FORMULA_FORBIDDEN", "XLSX 不允许公式")
        overlap = window[-WORKSHEET_SCAN_OVERLAP_BYTES:]


def _xml_component_limit(lower_name: str) -> int | None:
    if lower_name.endswith(".rels"):
        return MAX_RELATIONSHIP_XML_BYTES
    if lower_name == "[content_types].xml":
        return MAX_CONTENT_TYPES_XML_BYTES
    if lower_name == "xl/sharedstrings.xml":
        return MAX_SHARED_STRINGS_XML_BYTES
    if lower_name == "xl/styles.xml":
        return MAX_STYLES_XML_BYTES
    if lower_name == "xl/workbook.xml":
        return MAX_WORKBOOK_XML_BYTES
    if lower_name.startswith("xl/theme/") and lower_name.endswith(".xml"):
        return MAX_THEME_XML_BYTES
    if lower_name.endswith(".xml") and not lower_name.startswith(
        "xl/worksheets/"
    ):
        return MAX_OTHER_XML_BYTES
    return None


def _validate_archive(data: bytes) -> None:
    if not isinstance(data, bytes) or not data:
        _fail("XLSX_INVALID", "XLSX 文件无效")
    if len(data) > MAX_FILE_BYTES:
        _fail("FILE_TOO_LARGE", f"XLSX 文件超过 {MAX_FILE_MIB}MB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ARCHIVE_ENTRIES:
                _fail("XLSX_ZIP_BOMB", "XLSX 解压规模异常")
            total = 0
            names = {item.filename.lower() for item in entries}
            if any(
                name.startswith("xl/externallinks/")
                for name in names
            ):
                _fail("XLSX_EXTERNAL_LINK_FORBIDDEN", "XLSX 不允许外部链接")
            if any(name.endswith("vbaproject.bin") for name in names):
                _fail("XLSX_MACRO_FORBIDDEN", "XLSX 不允许宏")
            normalized_names: set[str] = set()
            for item in entries:
                name = item.filename.replace("\\", "/")
                if item.flag_bits & 0x1 or name.startswith("/") or ".." in name.split("/"):
                    _fail("XLSX_INVALID", "XLSX 压缩结构无效")
                if item.file_size < 0 or item.file_size > MAX_ENTRY_BYTES:
                    _fail("XLSX_ZIP_BOMB", "XLSX 解压规模异常")
                component_limit = _xml_component_limit(name.lower())
                if component_limit is not None and item.file_size > component_limit:
                    _fail(
                        "XLSX_XML_LIMIT_EXCEEDED",
                        "XLSX XML 元数据规模异常",
                    )
                total += item.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    _fail("XLSX_ZIP_BOMB", "XLSX 解压规模异常")
                if item.file_size > 1024 * 1024 and item.file_size / max(1, item.compress_size) > 100:
                    _fail("XLSX_ZIP_BOMB", "XLSX 压缩比异常")
                lower = name.lower()
                if lower in normalized_names:
                    _fail("XLSX_INVALID", "XLSX 包含重复条目")
                normalized_names.add(lower)
                if lower.startswith("xl/externallinks/"):
                    _fail("XLSX_EXTERNAL_LINK_FORBIDDEN", "XLSX 不允许外部链接")
                if lower.endswith("vbaproject.bin"):
                    _fail("XLSX_MACRO_FORBIDDEN", "XLSX 不允许宏")
                if lower.endswith((".rels", "[content_types].xml")):
                    with archive.open(item) as stream:
                        _scan_package_xml(
                            stream, relationships=lower.endswith(".rels"),
                        )
                elif lower.startswith("xl/worksheets/") and lower.endswith(".xml"):
                    with archive.open(item) as stream:
                        _scan_worksheet_formulas(stream)
    except ImportContractError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        _fail("XLSX_INVALID", "XLSX 文件无效")


def _stop_worker(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_worker_bounded(
    command: list[str], *, cwd: str, env: Mapping[str, str],
) -> subprocess.CompletedProcess:
    """Read both child pipes concurrently and enforce caps while streaming."""
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except OSError:
        _fail("XLSX_INVALID", "XLSX 无法安全解析")
    if process.stdout is None or process.stderr is None:
        _stop_worker(process)
        _fail("XLSX_INVALID", "XLSX 无法安全解析")

    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {
        "stdout": MAX_WORKER_STDOUT_BYTES,
        "stderr": MAX_WORKER_STDERR_BYTES,
    }
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    deadline = time.monotonic() + PARSER_TIMEOUT_SECONDS
    try:
        for name, stream in streams.items():
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("PARSER_TIMEOUT", "XLSX 解析超时")
            events = selector.select(remaining)
            if not events:
                _fail("PARSER_TIMEOUT", "XLSX 解析超时")
            for key, _ in events:
                name = str(key.data)
                buffer = buffers[name]
                limit = limits[name]
                read_size = min(64 * 1024, limit - len(buffer) + 1)
                try:
                    chunk = os.read(key.fd, max(1, read_size))
                except OSError:
                    _fail("XLSX_INVALID", "XLSX 无法安全解析")
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffer.extend(chunk)
                if len(buffer) > limit:
                    # Fail while the child is still writing; never let capture
                    # grow to attacker-selected size before checking it.
                    _fail("XLSX_INVALID", "XLSX 无法安全解析")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail("PARSER_TIMEOUT", "XLSX 解析超时")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _fail("PARSER_TIMEOUT", "XLSX 解析超时")
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
        )
    finally:
        selector.close()
        for stream in streams.values():
            try:
                stream.close()
            except OSError:
                pass
        if process.poll() is None:
            _stop_worker(process)


def _parse_isolated(data: bytes) -> dict:
    descriptor, path = tempfile.mkstemp(prefix="paihuo-branch-import-", suffix=".xlsx")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.chmod(path, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        worker = str(Path(__file__).with_name("inspection_import_worker.py"))
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
        }
        result = _run_worker_bounded(
            [sys.executable, "-I", worker, path],
            cwd=tempfile.gettempdir(), env=env,
        )
        if result.returncode != 0:
            _fail("XLSX_INVALID", "XLSX 无法安全解析")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError):
            _fail("XLSX_INVALID", "XLSX 解析器返回无效")
        if not isinstance(payload, dict) or not payload.get("ok"):
            code = payload.get("error_code") if isinstance(payload, dict) else None
            allowed = {
                "XLSX_INVALID", "XLSX_FORMULA_FORBIDDEN", "SHEET_LIMIT_EXCEEDED",
                "ROW_LIMIT_EXCEEDED", "CELL_LIMIT_EXCEEDED", "HEADER_INVALID",
                "CELL_TEXT_LIMIT_EXCEEDED", "TEXT_BUDGET_EXCEEDED",
            }
            _fail(code if code in allowed else "XLSX_INVALID", "XLSX 结构不符合导入模板")
        if not isinstance(payload.get("branches"), list) or not isinstance(payload.get("business_values"), list):
            _fail("XLSX_INVALID", "XLSX 解析器返回无效")
        return payload
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _scope(tenant_id: int, industry_key: str) -> tuple[int, str]:
    try:
        tid = int(tenant_id)
    except (TypeError, ValueError):
        _fail("SCOPE_FORBIDDEN", "租户作用域无效")
    industry = _text(industry_key, 80, required=True)
    if not db.one(
        "SELECT 1 ok FROM tenant_industry WHERE tenant_id=? AND industry_key=?",
        (tid, industry),
    ):
        _fail("SCOPE_FORBIDDEN", "当前租户未开通该行业")
    return tid, industry


def _authorize(tenant_id: int, actor_id: int, industry_key: str) -> tuple[int, int, str]:
    tid, industry = _scope(tenant_id, industry_key)
    try:
        uid = int(actor_id)
        # 批量导入会批量更新/停用门店，并写入店长联系方式与经营数据。
        # 它属于企业主数据治理，不等同于区域经理日常新建单店；必须
        # 由 owner/root 执行，避免普通行业成员一次改动整家公司门店。
        inspection._actor(tid, uid, industry, manager=True)
    except (TypeError, ValueError, inspection.InspectionForbidden):
        _fail("SCOPE_FORBIDDEN", "当前账号无权操作该行业")
    return tid, uid, industry


_EXPIRED_BUSINESS_AUDIT_JSON = '{"redacted":true}'


def _archive_payload_from_rows(raw_rows) -> tuple[bytes, str, bytes, int, str]:
    """Build and self-verify a compressed, PII-masked audit archive.

    The working row table is a decryptable staging surface.  Once a commit has
    succeeded it is redundant: the authority lives in ``store_branch`` and
    ``inspection_business_value``.  We retain the bounded public audit shape in
    a content-addressed zlib archive instead.  Only ``masked_payload_json`` is
    ever copied, so manager contact fields and operating source notes cannot
    leak into the archive.  Row actions are authenticated in the same payload;
    otherwise a 60k-row action list would grow once per import and could be
    altered without invalidating the archive digest.
    """
    records = []
    for row in raw_rows:
        try:
            masked = json.loads(str(row["masked_payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            _fail("IMPORT_ARCHIVE_INVALID", "导入审计暂存数据无效")
            raise AssertionError from exc  # _fail always raises
        if not isinstance(masked, dict):
            _fail("IMPORT_ARCHIVE_INVALID", "导入审计暂存数据无效")
        stored_row_number = int(row["row_number"])
        logical_row = (
            stored_row_number - _BUSINESS_ROW_OFFSET
            if stored_row_number >= _BUSINESS_ROW_OFFSET
            else stored_row_number
        )
        row_kind = (
            "business" if stored_row_number >= _BUSINESS_ROW_OFFSET
            else "branch"
        )
        records.append({
            "row_number": logical_row,
            "row_kind": row_kind,
            "store_code": row["store_code"],
            "action": row["action"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "data": masked,
        })
    canonical = _compact_json({
        "version": ARCHIVE_FORMAT_VERSION,
        "rows": records,
    }).encode("utf-8")
    if len(canonical) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档超过安全上限")
    digest = hashlib.sha256(canonical).hexdigest()
    compressed = zlib.compress(canonical, level=9)
    # Verify the exact bytes that will be committed before the row table is
    # removed.  A failed compression/digest check aborts the surrounding
    # BEGIN IMMEDIATE transaction and leaves all working rows recoverable.
    try:
        unpacked = zlib.decompress(compressed)
    except zlib.error as exc:
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档校验失败")
        raise AssertionError from exc
    if unpacked != canonical or hashlib.sha256(unpacked).hexdigest() != digest:
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档校验失败")
    # Kept for schema/API compatibility. Version 2 stores the complete action
    # truth in the compressed archive, so no per-ledger row list is needed.
    return canonical, digest, compressed, len(records), "[]"


def _load_audit_archive(connection, row: Mapping[str, Any]) -> list[dict]:
    """Read one archive with hash/size/shape verification (fail closed)."""
    row_map = dict(row) if hasattr(row, "keys") else row
    archive_sha256 = str(
        row_map.get("audit_archive_sha256")
        or row_map.get("archive_sha256")
        or ""
    ).lower()
    if not _SHA256_RE.fullmatch(archive_sha256):
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档标识无效")
    archive = connection.execute(
        "SELECT archive_sha256,payload_zlib,uncompressed_bytes,row_count "
        "FROM inspection_branch_import_archive WHERE archive_sha256=?",
        (archive_sha256,),
    ).fetchone()
    if archive is None:
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档不存在")
    try:
        expected_bytes = int(archive["uncompressed_bytes"] or 0)
        expected_rows = int(archive["row_count"] or 0)
    except (TypeError, ValueError):
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档元数据无效")
    if (
        expected_bytes < 0
        or expected_bytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES
        or expected_rows < 0
    ):
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档元数据无效")
    try:
        packed = bytes(archive["payload_zlib"] or b"")
        inflater = zlib.decompressobj()
        canonical = inflater.decompress(packed, expected_bytes + 1)
        valid_stream = (
            inflater.eof
            and not inflater.unconsumed_tail
            and not inflater.unused_data
            and len(canonical) <= expected_bytes
        )
    except (TypeError, ValueError, zlib.error):
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档校验失败")
    if (
        not valid_stream
        or len(canonical) != expected_bytes
        or hashlib.sha256(canonical).hexdigest() != archive_sha256
    ):
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档校验失败")
    try:
        payload = json.loads(canonical)
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档格式无效")
    if (
        not isinstance(payload, dict)
        or int(payload.get("version") or 0) != ARCHIVE_FORMAT_VERSION
        or not isinstance(payload.get("rows"), list)
        or len(payload["rows"]) != expected_rows
    ):
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档格式无效")
    return payload["rows"]


def _archive_committed_rows(connection, ledger: Mapping[str, Any], raw_rows) -> dict:
    """Persist one verified deduplicated archive and return its coordinates."""
    canonical, digest, compressed, row_count, actions_json = _archive_payload_from_rows(raw_rows)
    now = time.time()
    connection.execute(
        "INSERT OR IGNORE INTO inspection_branch_import_archive("
        "archive_sha256,payload_zlib,uncompressed_bytes,row_count,created_at) "
        "VALUES(?,?,?,?,?)",
        (digest, compressed, len(canonical), row_count, now),
    )
    saved = connection.execute(
        "SELECT archive_sha256,payload_zlib,uncompressed_bytes,row_count "
        "FROM inspection_branch_import_archive WHERE archive_sha256=?",
        (digest,),
    ).fetchone()
    # ``INSERT OR IGNORE`` may have selected a pre-existing content-addressed
    # blob.  Verify that blob as well; never delete working rows against a
    # corrupt archive supplied by a previous transaction.
    if saved is None:
        _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档写入失败")
    _load_audit_archive(
        connection,
        {"audit_archive_sha256": digest},
    )
    connection.execute(
        "UPDATE inspection_branch_import SET audit_archive_sha256=?,"
        "audit_archive_bytes=?,audit_archive_rows=?,audit_archived_at=?,"
        "archive_sha256=?,archive_size=?,archive_row_count=?,archived_at=?,"
        "audit_actions_json=? "
        "WHERE id=? AND tenant_id=?",
        (
            digest, len(compressed), row_count, now,
            digest, len(compressed), row_count, now,
            actions_json,
            int(ledger["id"]), int(ledger["tenant_id"]),
        ),
    )
    # This is the only destructive operation in the retention path: it removes
    # duplicate working audit rows, never authoritative branch/value/visit data.
    connection.execute(
        "DELETE FROM inspection_branch_import_row WHERE import_id=? "
        "AND tenant_id=?",
        (int(ledger["id"]), int(ledger["tenant_id"])),
    )
    return {
        "archive_sha256": digest,
        "archive_size": len(compressed),
        "archive_row_count": row_count,
    }


def _scrub_preview_staging(
    connection, import_id: int, tenant_id: int, purged_at: float,
) -> None:
    """Remove raw staging while retaining row/action/masked audit coordinates."""
    connection.execute(
        "UPDATE inspection_branch_import_row SET "
        "payload_json=CASE WHEN row_number>=? THEN ? ELSE masked_payload_json END,"
        "masked_payload_json=CASE WHEN row_number>=? THEN ? "
        "ELSE masked_payload_json END "
        "WHERE import_id=? AND tenant_id=?",
        (
            _BUSINESS_ROW_OFFSET, _EXPIRED_BUSINESS_AUDIT_JSON,
            _BUSINESS_ROW_OFFSET, _EXPIRED_BUSINESS_AUDIT_JSON,
            int(import_id), int(tenant_id),
        ),
    )
    connection.execute(
        "UPDATE inspection_branch_import SET business_values_json='[]',"
        "staging_purged_at=? "
        "WHERE id=? AND tenant_id=?",
        (float(purged_at), int(import_id), int(tenant_id)),
    )


def _expire_preview(connection, import_id: int, tenant_id: int, now: float) -> bool:
    """Scrub one uncommitted preview and retain only its masked audit summary."""
    current = connection.execute(
        "SELECT status FROM inspection_branch_import "
        "WHERE id=? AND tenant_id=?", (int(import_id), int(tenant_id)),
    ).fetchone()
    if current is None or str(current["status"]) != "previewed":
        return False
    _scrub_preview_staging(connection, import_id, tenant_id, now)
    changed = connection.execute(
        "UPDATE inspection_branch_import SET status='expired',"
        "updated_at=? "
        "WHERE id=? AND tenant_id=? AND status='previewed'",
        (float(now), int(import_id), int(tenant_id)),
    )
    return changed.rowcount == 1


def _expire_stale_previews(connection, tenant_id: int, now: float) -> int:
    cutoff = float(now) - PREVIEW_TTL_SECONDS
    stale = connection.execute(
        "SELECT id FROM inspection_branch_import "
        "WHERE tenant_id=? AND status IN ('previewed','expired') "
        "AND staging_purged_at IS NULL "
        "AND updated_at<? "
        "ORDER BY updated_at,id LIMIT ?",
        (int(tenant_id), cutoff, EXPIRED_CLEANUP_BATCH),
    ).fetchall()
    expired = 0
    for row in stale:
        # Preserve the old timestamp: after lifecycle cleanup this ledger no
        # longer consumes the rolling active-staging quota.
        _scrub_preview_staging(connection, row["id"], tenant_id, now)
        changed = connection.execute(
            "UPDATE inspection_branch_import SET status='expired' "
            "WHERE id=? AND tenant_id=? AND status IN ('previewed','expired')",
            (int(row["id"]), int(tenant_id)),
        )
        expired += int(changed.rowcount == 1)
    return expired


def cleanup_expired_previews(
    *, now: float | None = None, batch_size: int = RETENTION_CLEANUP_BATCH,
) -> dict[str, int]:
    """Bounded cross-tenant TTL sweep used at startup and on periodic ticks.

    The request paths still perform a tenant-local lazy sweep for fast quota
    recovery.  This process-wide entry point closes the lifecycle gap when a
    tenant is idle: at most ``batch_size`` stale ledgers are scrubbed in one
    transaction, and the next startup/tick continues from the indexed cursor.
    It never touches authoritative ``store_branch``, ``inspection_business_value``
    or ``inspection_visit`` rows.
    """
    try:
        limit = int(batch_size)
    except (TypeError, ValueError):
        limit = RETENTION_CLEANUP_BATCH
    limit = max(1, min(limit, 256))
    stamp = time.time() if now is None else float(now)
    cutoff = stamp - PREVIEW_TTL_SECONDS
    expired = 0
    scanned = 0
    with db.atomic() as connection:
        stale = connection.execute(
            "SELECT id,tenant_id,status FROM inspection_branch_import "
            "WHERE status IN ('previewed','expired') "
            "AND staging_purged_at IS NULL "
            "AND updated_at<? "
            "ORDER BY updated_at,tenant_id,id LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        scanned = len(stale)
        for row in stale:
            tenant_id = int(row["tenant_id"])
            import_id = int(row["id"])
            _scrub_preview_staging(connection, import_id, tenant_id, stamp)
            changed = connection.execute(
                "UPDATE inspection_branch_import SET status='expired',"
                "updated_at=? WHERE id=? AND tenant_id=? "
                "AND status IN ('previewed','expired')",
                (stamp, import_id, tenant_id),
            )
            expired += int(changed.rowcount == 1)
    # secure_delete overwrites the live page cells, but obsolete WAL frames can
    # still contain the previous ciphertext.  A successful truncate checkpoint
    # removes those frames before the next official backup/forensic snapshot.
    # A long reader may temporarily report busy; startup and every periodic tick
    # call this function even when no new rows expire, so cleanup is retried
    # without rolling back the already-safe logical retention transaction.
    wal_busy = -1
    wal_checkpointed = 0
    try:
        checkpoint = db.conn().execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        wal_busy = int(checkpoint[0]) if checkpoint is not None else -1
        wal_checkpointed = int(wal_busy == 0)
    except (sqlite3.Error, TypeError, ValueError):
        wal_busy = -1
    return {
        "scanned": scanned,
        "expired": expired,
        "compacted": 0,
        "remaining_bounded": int(scanned == limit),
        "wal_checkpointed": wal_checkpointed,
        "wal_busy": wal_busy,
    }


def _catalog_matches(row: Mapping[str, Any], contract: Mapping[str, str]) -> bool:
    return (
        str(row["catalog_version"] or "") == contract["catalog_version"]
        and str(row["catalog_sha256"] or "").lower()
        == contract["catalog_sha256"]
    )


def _preview_cipher_valid(row: Mapping[str, Any]) -> bool:
    if int(row["error_count"] or 0) or int(row["business_error_count"] or 0):
        # Uncommittable previews deliberately contain only masked plaintext.
        return True
    try:
        raw = _decrypt_staging(
            str(row["business_values_json"] or ""),
            tenant_id=int(row["tenant_id"]),
            industry=str(row["industry_key"]),
            request_key=str(row["request_key"]),
            purpose="business-values",
        )
        return isinstance(json.loads(raw), list)
    except (
        _StagingCipherError, TypeError, ValueError, json.JSONDecodeError,
    ):
        return False


def _request_replay(
    connection, tenant_id: int, industry: str, request_key: str,
    digest: str, catalog_contract: Mapping[str, str], now: float,
) -> Mapping[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM inspection_branch_import WHERE tenant_id=? "
        "AND industry_key=? AND request_key=?",
        (tenant_id, industry, request_key),
    ).fetchone()
    if row is None:
        return None
    if row["source_sha256"] != digest:
        _fail("REQUEST_KEY_CONFLICT", "同一请求号对应不同文件")
    if row["status"] == "previewed" and not _catalog_matches(
        row, catalog_contract,
    ):
        _expire_preview(connection, row["id"], tenant_id, now)
        row = connection.execute(
            "SELECT * FROM inspection_branch_import WHERE id=?", (row["id"],),
        ).fetchone()
    elif row["status"] == "previewed" and not _preview_cipher_valid(row):
        _expire_preview(connection, int(row["id"]), int(tenant_id), now)
        row = connection.execute(
            "SELECT * FROM inspection_branch_import WHERE id=?", (row["id"],),
        ).fetchone()
    return row


def _active_source(
    connection, tenant_id: int, industry: str, digest: str,
    catalog_contract: Mapping[str, str], now: float,
) -> Mapping[str, Any] | None:
    rows = connection.execute(
        "SELECT * FROM inspection_branch_import WHERE tenant_id=? "
        "AND industry_key=? AND source_sha256=? AND status='previewed' "
        "ORDER BY id",
        (tenant_id, industry, digest),
    ).fetchall()
    for row in rows:
        if not _catalog_matches(row, catalog_contract):
            _expire_preview(connection, row["id"], tenant_id, now)
            continue
        if not _preview_cipher_valid(row):
            _expire_preview(connection, int(row["id"]), int(tenant_id), now)
            continue
        return row
    return None


def _same_branch(existing: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    for field in STORE_FIELDS:
        old = existing.get(field)
        new = candidate.get(field)
        if field in {"area_sqm", "longitude", "latitude"}:
            if old is None or new is None:
                if old is not new:
                    return False
            elif not math.isclose(float(old), float(new), rel_tol=0, abs_tol=1e-9):
                return False
        elif old != new:
            return False
    return True


def _same_business_value(
    existing: Mapping[str, Any], candidate: Mapping[str, Any],
) -> bool:
    old_value = existing.get("value")
    new_value = candidate.get("value")
    if old_value is None or new_value is None or not math.isclose(
        float(old_value), float(new_value), rel_tol=0, abs_tol=1e-9,
    ):
        return False
    return all(
        existing.get(field) == candidate.get(field)
        for field in ("unit", "source_ref", "remark")
    )


def _business_public_payload(value: Mapping[str, Any], *, redact: bool) -> dict:
    payload = {
        field: value.get(field) for field in BUSINESS_PUBLIC_FIELDS
        if field in value
    }
    if redact:
        # An errored preview can never commit.  Keep only the natural-key
        # coordinates needed to locate the bad line; operating values and
        # internal source notes must never enter SQLite for that ledger.
        for field in ("value", "source_ref", "remark"):
            payload.pop(field, None)
    return payload


def _chunks(values: list, size: int = _SQL_IN_CHUNK):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _branches_by_code(
    connection,
    tid: int,
    industry: str,
    codes,
) -> dict[str, dict]:
    """Fetch only candidate branches without crossing SQLite bind limits."""
    unique = sorted({
        str(code) for code in codes
        if code is not None and str(code).strip()
    })
    output: dict[str, dict] = {}
    for batch in _chunks(unique):
        marks = ",".join("?" for _ in batch)
        rows = connection.execute(
            "SELECT * FROM store_branch WHERE tenant_id=? AND industry_key=? "
            f"AND store_code IN ({marks})",
            (tid, industry, *batch),
        ).fetchall()
        for row in rows:
            item = dict(row)
            output[str(item["store_code"])] = item
    return output


def _compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _stage_serialized_payloads(
    item: Mapping[str, Any], *, uncommittable: bool, tenant_id: int,
    industry: str, request_key: str,
) -> tuple[dict, int, str, str]:
    is_business = item["row_kind"] == "business"
    payload = (
        _business_public_payload(item, redact=uncommittable)
        if is_business else item.get("payload") or {}
    )
    masked_payload = (
        _business_public_payload(item, redact=True)
        if is_business else _masked(payload)
    )
    stored_payload = (
        masked_payload if uncommittable and not is_business else payload
    )
    stored_row_number = int(item["row_number"])
    if is_business:
        stored_row_number += _BUSINESS_ROW_OFFSET
    raw_json = _compact_json(stored_payload)
    payload_json = (
        raw_json
        if uncommittable
        else _encrypt_staging(
            raw_json,
            tenant_id=tenant_id,
            industry=industry,
            request_key=request_key,
            purpose=f"row:{stored_row_number}",
        )
    )
    return payload, stored_row_number, payload_json, _compact_json(masked_payload)


def _prepare_staged_items(
    staged_items: list[Mapping[str, Any]], *, uncommittable: bool,
    tenant_id: int, industry: str, request_key: str,
) -> tuple[list[tuple], int]:
    prepared: list[tuple] = []
    total = 0
    for item in staged_items:
        values = _stage_serialized_payloads(
            item,
            uncommittable=uncommittable,
            tenant_id=tenant_id,
            industry=industry,
            request_key=request_key,
        )
        _, _, payload_json, masked_payload_json = values
        total += len(payload_json.encode("utf-8"))
        total += len(masked_payload_json.encode("utf-8"))
        prepared.append((item, *values))
    return prepared, total


def _staging_usage(connection, tenant_id: int, status: str) -> tuple[int, int, int]:
    usage = connection.execute(
        "SELECT COUNT(*) preview_count,"
        "COALESCE(SUM(total_rows+business_create_count+business_update_count+"
        "business_skip_count+business_error_count),0) staged_rows,"
        "COALESCE(SUM(length(CAST(business_values_json AS BLOB))),0) ledger_bytes "
        "FROM inspection_branch_import WHERE tenant_id=? AND status=? "
        "AND staging_purged_at IS NULL",
        (int(tenant_id), status),
    ).fetchone()
    row_bytes = connection.execute(
        "SELECT COALESCE(SUM(length(CAST(r.payload_json AS BLOB))+"
        "length(CAST(r.masked_payload_json AS BLOB))),0) staged_bytes "
        "FROM inspection_branch_import_row r "
        "JOIN inspection_branch_import i ON i.id=r.import_id "
        "AND i.tenant_id=r.tenant_id "
        "WHERE i.tenant_id=? AND i.status=? "
        "AND i.staging_purged_at IS NULL",
        (int(tenant_id), status),
    ).fetchone()
    return (
        int(usage["preview_count"] or 0),
        int(usage["staged_rows"] or 0),
        int(usage["ledger_bytes"] or 0)
        + int(row_bytes["staged_bytes"] or 0),
    )


def _enforce_preview_quota(
    connection, tenant_id: int, *, incoming_rows: int, incoming_bytes: int,
) -> None:
    active_count, active_rows, active_bytes = _staging_usage(
        connection, tenant_id, "previewed",
    )
    if (
        active_count + 1 > MAX_ACTIVE_PREVIEWS_PER_TENANT
        or active_rows + int(incoming_rows)
        > MAX_ACTIVE_PREVIEW_ROWS_PER_TENANT
        or active_bytes + int(incoming_bytes)
        > MAX_ACTIVE_PREVIEW_BYTES_PER_TENANT
    ):
        _fail(
            "IMPORT_PREVIEW_QUOTA_EXCEEDED",
            "待提交导入已达租户安全限额，请先提交或等待过期",
        )

    # CAS-conflicted/undecryptable generations no longer consume an active
    # slot, so an owner can recover immediately even when the active cap is
    # one.  They remain separately bounded until the ordinary 24-hour lazy
    # lifecycle scrub runs; repeated conflicts therefore cannot amplify the
    # staging tables without limit.
    retired_count, retired_rows, retired_bytes = _staging_usage(
        connection, tenant_id, "expired",
    )
    if (
        retired_count >= MAX_RETIRED_PREVIEWS_PER_TENANT
        or retired_rows >= MAX_RETIRED_PREVIEW_ROWS_PER_TENANT
        or retired_bytes >= MAX_RETIRED_PREVIEW_BYTES_PER_TENANT
    ):
        _fail(
            "IMPORT_PREVIEW_QUOTA_EXCEEDED",
            "近期失效导入已达租户安全限额，请等待生命周期清理",
        )


def _business_values_by_natural_key(
    connection,
    tid: int,
    industry: str,
    keys,
) -> dict[tuple, dict]:
    """Fetch exact business natural keys without a long OR-query write lock.

    Small previews use one bounded OR batch.  Large 40k-row previews stage the
    exact keys in a connection-local TEMP table and join through the natural
    index.  This avoids hundreds of complex OR plans while retaining exact-key
    CAS semantics and never writes the authoritative database.
    """
    unique = list(dict.fromkeys(keys))
    output: dict[tuple, dict] = {}
    if not unique:
        return output
    if len(unique) > 400:
        table = "_inspection_import_business_keys"
        connection.execute(
            f"CREATE TEMP TABLE IF NOT EXISTS {table}("
            "branch_id INTEGER NOT NULL,metric_key TEXT NOT NULL,"
            "period_start TEXT NOT NULL,period_end TEXT NOT NULL,"
            "PRIMARY KEY(branch_id,metric_key,period_start,period_end)"
            ") WITHOUT ROWID"
        )
        connection.execute(f"DELETE FROM {table}")
        connection.executemany(
            f"INSERT INTO {table}(branch_id,metric_key,period_start,period_end) "
            "VALUES(?,?,?,?)",
            unique,
        )
        try:
            rows = connection.execute(
                f"SELECT v.* FROM {table} k JOIN inspection_business_value v "
                "INDEXED BY idx_inspection_business_value_natural "
                "ON v.branch_id=k.branch_id AND v.metric_key=k.metric_key "
                "AND v.period_start=k.period_start AND v.period_end=k.period_end "
                "WHERE v.tenant_id=? AND v.industry_key=?",
                (tid, industry),
            ).fetchall()
        finally:
            connection.execute(f"DELETE FROM {table}")
        for row in rows:
            item = dict(row)
            natural = (
                int(item["branch_id"]), str(item["metric_key"]),
                str(item["period_start"]), str(item["period_end"]),
            )
            output[natural] = item
        return output
    for batch in _chunks(unique, 200):
        conditions = " OR ".join(
            "(branch_id=? AND metric_key=? AND period_start=? AND period_end=?)"
            for _ in batch
        )
        params: list[Any] = [tid, industry]
        for key in batch:
            params.extend(key)
        rows = connection.execute(
            "SELECT * FROM inspection_business_value WHERE tenant_id=? "
            f"AND industry_key=? AND ({conditions})",
            params,
        ).fetchall()
        for row in rows:
            item = dict(row)
            natural = (
                int(item["branch_id"]), str(item["metric_key"]),
                str(item["period_start"]), str(item["period_end"]),
            )
            output[natural] = item
    return output


def _classify_business_values(
    connection,
    tid: int,
    industry: str,
    branches: list[dict],
    values: list[dict],
) -> list[dict]:
    """Freeze branch and business natural-key baselines for a preview."""
    branch_items = {
        str(item.get("payload", {}).get("store_code") or ""): item
        for item in branches if isinstance(item.get("payload"), dict)
    }
    external_codes = {
        str(value.get("store_code") or "")
        for value in values
        if str(value.get("store_code") or "") not in branch_items
    }
    external_branches = _branches_by_code(
        connection, tid, industry, external_codes,
    )
    prepared = []
    for value in values:
        code = str(value.get("store_code") or "")
        branch_item = branch_items.get(code)
        if branch_item is not None:
            if branch_item.get("action") == "error":
                prepared.append({
                    **value,
                    "action": "error",
                    "error_code": "BUSINESS_STORE_NOT_FOUND",
                    "existing_branch_id": 0,
                    "existing_row_version": 0,
                    "existing_business_value_id": 0,
                    "existing_business_row_version": 0,
                })
                continue
            branch_id = int(branch_item.get("existing_branch_id") or 0)
            branch_version = int(
                branch_item.get("existing_row_version") or 0
            )
        else:
            branch = external_branches.get(code)
            if branch is None:
                prepared.append({
                    **value,
                    "action": "error",
                    "error_code": "BUSINESS_STORE_NOT_FOUND",
                    "existing_branch_id": 0,
                    "existing_row_version": 0,
                    "existing_business_value_id": 0,
                    "existing_business_row_version": 0,
                })
                continue
            branch_id = int(branch["id"])
            branch_version = int(branch.get("row_version") or 0)
        prepared.append({
            **value,
            "existing_branch_id": branch_id,
            "existing_row_version": branch_version,
        })

    natural_keys = [
        (
            int(value["existing_branch_id"]), value["metric_key"],
            value["period_start"], value["period_end"],
        )
        for value in prepared
        if not value.get("error_code")
        and int(value.get("existing_branch_id") or 0) > 0
    ]
    existing_by_key = _business_values_by_natural_key(
        connection, tid, industry, natural_keys,
    )
    output = []
    for value in prepared:
        if value.get("error_code"):
            output.append(value)
            continue
        branch_id = int(value.get("existing_branch_id") or 0)
        natural = (
            branch_id, value["metric_key"], value["period_start"],
            value["period_end"],
        )
        existing = existing_by_key.get(natural) if branch_id > 0 else None
        if existing is None:
            action = "create"
            value_id = value_version = 0
        else:
            action = (
                "skip" if _same_business_value(existing, value) else "update"
            )
            value_id = int(existing["id"])
            value_version = int(existing.get("row_version") or 0)
        output.append({
            **value,
            "action": action,
            "error_code": None,
            "existing_branch_id": branch_id,
            "existing_row_version": int(
                value.get("existing_row_version") or 0
            ),
            "existing_business_value_id": value_id,
            "existing_business_row_version": value_version,
        })
    return output


def _classify(
    connection, tid: int, industry: str, branches: list[dict],
) -> list[dict]:
    by_code = _branches_by_code(
        connection,
        tid,
        industry,
        (
            item.get("payload", {}).get("store_code")
            for item in branches
            if isinstance(item.get("payload"), dict)
        ),
    )
    output = []
    for item in branches:
        payload = item.get("payload")
        if item.get("error_code") or not isinstance(payload, dict):
            output.append({
                **item,
                "action": "error",
                "error_code": item.get("error_code") or "ROW_INVALID",
                "existing_branch_id": 0,
                "existing_row_version": 0,
            })
            continue
        existing = by_code.get(payload["store_code"])
        if existing is None:
            output.append({
                **item,
                "action": "create",
                "error_code": None,
                "existing_branch_id": 0,
                "existing_row_version": 0,
            })
            continue
        baseline = {
            "existing_branch_id": int(existing["id"]),
            "existing_row_version": int(existing.get("row_version") or 0),
        }
        output.append({
            **item,
            "action": "skip" if _same_branch(existing, payload) else "update",
            "error_code": None,
            **baseline,
        })
    return output


def _import_page_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_IMPORT_PAGE_LIMIT
    if isinstance(value, bool):
        _fail("IMPORT_PAGE_INVALID", "导入分页条数无效")
    try:
        limit = int(value)
    except (TypeError, ValueError):
        _fail("IMPORT_PAGE_INVALID", "导入分页条数无效")
    if not 1 <= limit <= MAX_IMPORT_PAGE_LIMIT:
        _fail(
            "IMPORT_PAGE_INVALID",
            f"导入分页条数必须在 1-{MAX_IMPORT_PAGE_LIMIT} 之间",
        )
    return limit


def _import_page_cursor(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        _fail("IMPORT_PAGE_INVALID", "导入分页游标无效")
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 9:
        _fail("IMPORT_PAGE_INVALID", "导入分页游标无效")
    cursor = int(text)
    if cursor < 0 or cursor >= 1_000_000:
        _fail("IMPORT_PAGE_INVALID", "导入分页游标无效")
    return cursor


def _import_row_kind(value: Any) -> str:
    if value in (None, "", "all"):
        return "all"
    if not isinstance(value, str) or value not in {"branch", "business"}:
        _fail("IMPORT_PAGE_INVALID", "导入行类型无效")
    return value


def _filtered_import_total(
    row: Mapping[str, Any], *, row_kind: str, errors_only: bool,
) -> int:
    branch = (
        int(row["error_count"])
        if errors_only else int(row["total_rows"])
    )
    business = (
        int(row["business_error_count"])
        if errors_only else sum(
            int(row[field]) for field in (
                "business_create_count", "business_update_count",
                "business_skip_count", "business_error_count",
            )
        )
    )
    if row_kind == "branch":
        return branch
    if row_kind == "business":
        return business
    return branch + business


def _public_import(
    connection,
    row: Mapping[str, Any],
    *,
    limit: Any = None,
    cursor: Any = None,
    errors_only: bool = False,
    row_kind: Any = None,
    request_key_override: str | None = None,
    source_reused: bool = False,
) -> dict:
    page_limit = _import_page_limit(limit)
    page_cursor = _import_page_cursor(cursor)
    selected_kind = _import_row_kind(row_kind)
    if not isinstance(errors_only, bool):
        _fail("IMPORT_PAGE_INVALID", "导入错误筛选无效")
    conditions = ["import_id=?", "tenant_id=?", "row_number>?"]
    params: list[Any] = [
        int(row["id"]), int(row["tenant_id"]), page_cursor,
    ]
    if selected_kind == "branch":
        conditions.append("row_number<?")
        params.append(_BUSINESS_ROW_OFFSET)
    elif selected_kind == "business":
        conditions.append("row_number>=?")
        params.append(_BUSINESS_ROW_OFFSET)
    if errors_only:
        conditions.append("error_code IS NOT NULL")
    rows = connection.execute(
        "SELECT row_number,store_code,action,error_code,error_message,"
        "payload_json,masked_payload_json FROM inspection_branch_import_row "
        "WHERE " + " AND ".join(conditions) + " ORDER BY row_number LIMIT ?",
        (*params, page_limit + 1),
    ).fetchall()
    # Committed imports no longer retain duplicate working rows.  Rehydrate a
    # bounded page from the verified compressed audit archive so existing API
    # pagination remains compatible without allowing the staging table to grow
    # one row per historical import forever.
    if not rows and str(row["status"] or "") == "committed":
        archive_rows = _load_audit_archive(connection, row)
        # Compatibility fallback for local development databases created by
        # the pre-release v1 archive prototype.  Production schema 52 uses v2,
        # where actions and errors live inside the authenticated archive.
        actions_by_key = {}
        try:
            action_payload = json.loads(str(row["audit_actions_json"] or "[]"))
            if isinstance(action_payload, list):
                actions_by_key = {
                    (
                        int(item.get("row_number") or 0),
                        str(item.get("row_kind") or "branch"),
                    ): item
                    for item in action_payload if isinstance(item, dict)
                }
        except (TypeError, ValueError, json.JSONDecodeError):
            _fail("IMPORT_ARCHIVE_INVALID", "导入审计动作摘要无效")
        filtered_archive = []
        for item in archive_rows:
            if not isinstance(item, dict):
                _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档格式无效")
            logical = int(item.get("row_number") or 0)
            stored = (
                logical + _BUSINESS_ROW_OFFSET
                if str(item.get("row_kind") or "") == "business"
                else logical
            )
            if stored <= page_cursor:
                continue
            if selected_kind == "branch" and stored >= _BUSINESS_ROW_OFFSET:
                continue
            if selected_kind == "business" and stored < _BUSINESS_ROW_OFFSET:
                continue
            masked = item.get("data")
            if not isinstance(masked, dict):
                _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档格式无效")
            action_meta = actions_by_key.get(
                (logical, str(item.get("row_kind") or "branch")),
                {},
            )
            action = item.get("action") or action_meta.get("action") or "committed"
            error_code = item.get("error_code") or action_meta.get("error_code")
            error_message = (
                item.get("error_message") or action_meta.get("error_message")
            )
            if action not in {"create", "update", "skip", "error", "committed"}:
                _fail("IMPORT_ARCHIVE_INVALID", "导入审计归档格式无效")
            if errors_only and not error_code:
                continue
            filtered_archive.append({
                "row_number": stored,
                "store_code": item.get("store_code"),
                "action": action,
                "error_code": error_code,
                "error_message": error_message,
                "payload_json": _compact_json(masked),
                "masked_payload_json": _compact_json(masked),
            })
            if len(filtered_archive) >= page_limit + 1:
                break
        rows = filtered_archive
    has_more = len(rows) > page_limit
    rows = rows[:page_limit]
    rendered_rows = []
    for item in rows:
        stored_row_number = int(item["row_number"])
        is_business = stored_row_number >= _BUSINESS_ROW_OFFSET
        data = json.loads(item["masked_payload_json"] or "{}")
        if (
            is_business
            and row["status"] == "previewed"
            and not int(row["error_count"] or 0)
            and not int(row["business_error_count"] or 0)
        ):
            try:
                raw = _decrypt_staging(
                    str(item["payload_json"] or ""),
                    tenant_id=int(row["tenant_id"]),
                    industry=str(row["industry_key"]),
                    request_key=str(row["request_key"]),
                    purpose=f"row:{stored_row_number}",
                )
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    data = decoded
            except (
                _StagingCipherError, TypeError, ValueError,
                json.JSONDecodeError,
            ):
                pass
        rendered_rows.append({
            "row_kind": "business" if is_business else "branch",
            "row_number": (
                stored_row_number - _BUSINESS_ROW_OFFSET
                if is_business else stored_row_number
            ),
            "store_code": item["store_code"],
            "action": item["action"],
            "error_code": item["error_code"],
            "error_message": item["error_message"],
            "data": data,
        })
    return {
        "import_id": int(row["id"]),
        "industry_key": row["industry_key"],
        "request_key": (
            request_key_override
            if request_key_override is not None else row["request_key"]
        ),
        "source_reused": bool(source_reused),
        "source_sha256": row["source_sha256"],
        "filename": row["filename"],
        "catalog_version": row["catalog_version"] or "",
        "catalog_sha256": row["catalog_sha256"] or "",
        "status": row["status"],
        "counts": {
            "create": int(row["create_count"]),
            "update": int(row["update_count"]),
            "skip": int(row["skip_count"]),
            "error": int(row["error_count"]),
        },
        "business_counts": {
            "create": int(row["business_create_count"]),
            "update": int(row["business_update_count"]),
            "skip": int(row["business_skip_count"]),
            "error": int(row["business_error_count"]),
        },
        "total_rows": int(row["total_rows"]),
        "business_total_rows": sum(
            int(row[field]) for field in (
                "business_create_count", "business_update_count",
                "business_skip_count", "business_error_count",
            )
        ),
        "limit": page_limit,
        "cursor": str(page_cursor) if page_cursor else None,
        "next_cursor": (
            str(int(rows[-1]["row_number"]))
            if has_more and rows else None
        ),
        "has_more": has_more,
        "row_kind": selected_kind,
        "errors_only": errors_only,
        "filtered_total_rows": _filtered_import_total(
            row, row_kind=selected_kind, errors_only=errors_only,
        ),
        "rows": rendered_rows,
    }


def preview_import(
    tenant_id: int, actor_id: int, industry_key: str, request_key: str,
    filename: str, data: bytes,
) -> dict:
    """Validate and stage an XLSX without touching branch/business tables."""
    tid, uid, industry = _authorize(tenant_id, actor_id, industry_key)
    request = _text(request_key, 160, required=True)
    if not _REQUEST_RE.fullmatch(request):
        _fail("REQUEST_KEY_INVALID", "请求幂等号格式无效")
    clean_filename = _clean_filename(filename)
    _validate_archive(data)
    digest = hashlib.sha256(data).hexdigest()
    catalog_contract = _catalog_contract(industry)
    now = time.time()
    # Cleanup has its own transaction so a later, intentional 409 cannot roll
    # back scrubbing of unrelated stale previews.
    with db.atomic() as connection:
        _expire_stale_previews(connection, tid, now)
    # The fast-path replay/source check still serializes with concurrent inserts.
    with db.atomic() as connection:
        existing = _request_replay(
            connection, tid, industry, request, digest, catalog_contract, now,
        )
        if existing is not None:
            existing_public = _public_import(connection, existing)
        else:
            source = _active_source(
                connection, tid, industry, digest, catalog_contract, now,
            )
            existing_public = (
                _public_import(
                    connection,
                    source,
                    request_key_override=request,
                    source_reused=True,
                )
                if source is not None else None
            )
    if existing_public is not None:
        if existing_public["status"] == "expired":
            _fail(
                "IMPORT_PREVIEW_EXPIRED",
                "导入预览已过期，请使用新请求号重新上传",
            )
        return existing_public

    parsed = _parse_isolated(data)
    if not parsed["branches"]:
        _fail("EMPTY_BRANCH_SHEET", "门店表至少需要一行数据")
    staged = []
    code_occurrences: dict[str, int] = {}
    for item in parsed["branches"]:
        row_number = int(item.get("row_number") or 0)
        try:
            payload = _normalize_branch(item.get("data") or {})
            code_occurrences[payload["store_code"]] = code_occurrences.get(payload["store_code"], 0) + 1
            staged.append({"row_number": row_number, "payload": payload, "error_code": None})
        except ImportContractError as exc:
            staged.append({"row_number": row_number, "payload": None, "error_code": exc.code})
    for item in staged:
        payload = item.get("payload")
        if payload and code_occurrences.get(payload["store_code"], 0) > 1:
            item["error_code"] = "DUPLICATE_STORE_CODE"

    try:
        metric_rules = {
            metric["metric_code"]: set(metric.get("allowed_units") or (metric["unit"],))
            for metric in inspectionstandards.metric_catalog(industry)
        }
    except inspectionstandards.InspectionStandardError:
        _fail("METRIC_CATALOG_UNAVAILABLE", "该行业暂无经营指标口径")
    business_values = []
    business_errors = []
    business_keys: set[tuple] = set()
    for item in parsed["business_values"]:
        row_number = int(item.get("row_number") or 0)
        try:
            value = _normalize_business(item.get("data") or {}, metric_rules)
            natural = tuple(value[key] for key in (
                "store_code", "metric_key", "period_start", "period_end"
            ))
            if natural in business_keys:
                _fail("BUSINESS_VALUE_DUPLICATE", "经营数据重复")
            business_keys.add(natural)
            business_values.append({**value, "row_number": row_number})
        except ImportContractError as exc:
            business_errors.append({
                "row_number": row_number,
                "store_code": _safe_error_store_code(
                    (item.get("data") or {}).get("门店编号*")
                ),
                "action": "error",
                "error_code": exc.code,
                "existing_branch_id": 0,
                "existing_row_version": 0,
                "existing_business_value_id": 0,
                "existing_business_row_version": 0,
            })

    now = time.time()
    with db.atomic() as connection:
        _expire_stale_previews(connection, tid, now)
    with db.atomic() as connection:
        replay = _request_replay(
            connection, tid, industry, request, digest, catalog_contract, now,
        )
        if replay is not None:
            if replay["status"] == "expired":
                _fail(
                    "IMPORT_PREVIEW_EXPIRED",
                    "导入预览已过期，请使用新请求号重新上传",
                )
            return _public_import(connection, replay)
        source = _active_source(
            connection, tid, industry, digest, catalog_contract, now,
        )
        if source is not None:
            return _public_import(
                connection,
                source,
                request_key_override=request,
                source_reused=True,
            )
        classified = _classify(connection, tid, industry, staged)
        counts = {kind: 0 for kind in ("create", "update", "skip", "error")}
        for item in classified:
            item["row_kind"] = "branch"
            if item.get("error_code"):
                item["action"] = "error"
            counts[item["action"]] += 1
        business_classified = _classify_business_values(
            connection, tid, industry, classified, business_values
        )
        business_classified.extend(business_errors)
        business_counts = {
            kind: 0 for kind in ("create", "update", "skip", "error")
        }
        for item in business_classified:
            item["row_kind"] = "business"
            if item.get("error_code"):
                item["action"] = "error"
            business_counts[item["action"]] += 1
        # Any row error makes this ledger permanently uncommittable.  Store
        # only its public masked projection from the first INSERT and discard
        # operating values entirely; an update-after-insert would still leave
        # recoverable private content in SQLite WAL/freelist pages.
        uncommittable = counts["error"] > 0 or business_counts["error"] > 0
        stored_business_values = (
            [] if uncommittable else business_classified
        )
        raw_business_values_json = _compact_json(stored_business_values)
        stored_business_values_json = (
            raw_business_values_json
            if uncommittable
            else _encrypt_staging(
                raw_business_values_json,
                tenant_id=tid,
                industry=industry,
                request_key=request,
                purpose="business-values",
            )
        )
        staged_items = [*classified, *business_classified]
        prepared_staged_items, staged_bytes = _prepare_staged_items(
            staged_items,
            uncommittable=uncommittable,
            tenant_id=tid,
            industry=industry,
            request_key=request,
        )
        incoming_bytes = (
            len(stored_business_values_json.encode("utf-8")) + staged_bytes
        )
        _enforce_preview_quota(
            connection,
            tid,
            incoming_rows=len(staged_items),
            incoming_bytes=incoming_bytes,
        )
        ledger_columns = (
            "tenant_id", "industry_key", "request_key", "source_sha256",
            "filename", "catalog_version", "catalog_sha256",
            "business_values_json", "status", "total_rows", "create_count",
            "update_count", "skip_count", "error_count",
            "business_create_count", "business_update_count",
            "business_skip_count", "business_error_count", "created_by",
            "created_at", "updated_at",
        )
        ledger_values = (
            tid, industry, request, digest, clean_filename,
            catalog_contract["catalog_version"],
            catalog_contract["catalog_sha256"],
            stored_business_values_json,
            "previewed", len(classified), counts["create"], counts["update"],
            counts["skip"], counts["error"], business_counts["create"],
            business_counts["update"], business_counts["skip"],
            business_counts["error"], uid, now, now,
        )
        cursor = connection.execute(
            f"INSERT INTO inspection_branch_import({','.join(ledger_columns)}) "
            f"VALUES({','.join('?' for _ in ledger_values)})",
            ledger_values,
        )
        import_id = int(cursor.lastrowid)
        for (
            item, payload, stored_row_number, payload_json,
            masked_payload_json,
        ) in prepared_staged_items:
            error_code = item.get("error_code")
            connection.execute(
                "INSERT INTO inspection_branch_import_row(import_id,tenant_id,row_number,"
                "store_code,action,error_code,error_message,payload_json,"
                "masked_payload_json,existing_branch_id,existing_row_version,"
                "existing_business_value_id,existing_business_row_version,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    import_id, tid, stored_row_number, payload.get("store_code"),
                    item["action"], error_code,
                    "该行未通过导入校验" if error_code else None,
                    payload_json,
                    masked_payload_json,
                    int(item.get("existing_branch_id") or 0),
                    int(item.get("existing_row_version") or 0),
                    int(item.get("existing_business_value_id") or 0),
                    int(item.get("existing_business_row_version") or 0),
                    now,
                ),
            )
        saved = connection.execute(
            "SELECT * FROM inspection_branch_import WHERE id=?", (import_id,)
        ).fetchone()
        return _public_import(connection, saved)


def _commit_import_once(
    tenant_id: int, actor_id: int, import_id: int, industry_key: str,
) -> dict:
    """Atomically apply a clean preview; committed replays are read-only."""
    try:
        tid, uid, iid = int(tenant_id), int(actor_id), int(import_id)
    except (TypeError, ValueError):
        _fail("IMPORT_NOT_FOUND", "导入记录不存在")
    _, uid, industry = _authorize(tid, uid, industry_key)
    now = time.time()
    with db.atomic() as connection:
        _expire_stale_previews(connection, tid, now)
    with db.atomic() as connection:
        ledger = connection.execute(
            "SELECT i.* FROM inspection_branch_import i "
            "JOIN tenant_industry ti ON ti.tenant_id=i.tenant_id "
            "AND ti.industry_key=i.industry_key "
            "WHERE i.id=? AND i.tenant_id=? AND i.industry_key=?",
            (iid, tid, industry),
        ).fetchone()
        if not ledger:
            _fail("IMPORT_NOT_FOUND", "导入记录不存在")
        if ledger["status"] == "committed":
            return _public_import(connection, ledger)
        if ledger["status"] == "expired":
            _fail("IMPORT_PREVIEW_EXPIRED", "导入预览已过期，请重新上传")
        current_catalog = _catalog_contract(industry)
        if (
            str(ledger["catalog_version"] or "")
            != current_catalog["catalog_version"]
            or str(ledger["catalog_sha256"] or "").lower()
            != current_catalog["catalog_sha256"]
        ):
            _fail(
                "IMPORT_STATE_CONFLICT",
                "巡店标准已更新，请重新上传并预览",
            )
        if int(ledger["error_count"]) or int(ledger["business_error_count"]):
            _fail("IMPORT_HAS_ERRORS", "预览存在错误，不能提交")
        raw_rows = connection.execute(
            "SELECT * FROM inspection_branch_import_row WHERE import_id=? "
            "ORDER BY row_number",
            (iid,),
        ).fetchall()
        classified = []
        branch_candidates = []
        business_stage_rows: dict[int, Mapping[str, Any]] = {}
        seen_codes: set[str] = set()
        for row in raw_rows:
            row_number = int(row["row_number"])
            if row_number >= 100_000:
                logical_row = row_number - 100_000
                if logical_row in business_stage_rows:
                    _fail("IMPORT_STATE_CONFLICT", "导入暂存数据无效")
                business_stage_rows[logical_row] = row
                continue
            try:
                raw_payload = _decrypt_staging(
                    str(row["payload_json"] or ""),
                    tenant_id=tid,
                    industry=industry,
                    request_key=str(ledger["request_key"]),
                    purpose=f"row:{row_number}",
                )
                payload = json.loads(raw_payload)
            except (
                _StagingCipherError, TypeError, ValueError,
                json.JSONDecodeError,
            ):
                _fail(
                    "IMPORT_PREVIEW_EXPIRED",
                    "导入预览密钥已失效，请重新上传",
                )
            action = str(row["action"] or "")
            if not isinstance(payload, dict) or action not in {
                "create", "update", "skip",
            }:
                _fail("IMPORT_STATE_CONFLICT", "导入暂存数据无效")
            code = str(payload.get("store_code") or "")
            if not code or code in seen_codes:
                _fail("IMPORT_STATE_CONFLICT", "导入暂存数据无效")
            seen_codes.add(code)
            baseline_id = int(row["existing_branch_id"] or 0)
            baseline_version = int(row["existing_row_version"] or 0)
            branch_candidates.append({
                "row_number": row_number,
                "payload": payload,
                "action": action,
                "existing_branch_id": baseline_id,
                "existing_row_version": baseline_version,
            })
        current_branches = _branches_by_code(
            connection,
            tid,
            ledger["industry_key"],
            [item["payload"]["store_code"] for item in branch_candidates],
        )
        for item in branch_candidates:
            payload = item["payload"]
            action = item["action"]
            code = str(payload["store_code"])
            baseline_id = int(item["existing_branch_id"])
            baseline_version = int(item["existing_row_version"])
            current = current_branches.get(code)
            if action == "create":
                if baseline_id != 0 or baseline_version != 0 or current is not None:
                    _fail("IMPORT_STATE_CONFLICT", "门店数据已变更，请重新预览")
            else:
                if (
                    baseline_id <= 0
                    or baseline_version <= 0
                    or current is None
                    or int(current["id"]) != baseline_id
                    or int(current.get("row_version") or 0) != baseline_version
                ):
                    _fail("IMPORT_STATE_CONFLICT", "门店数据已变更，请重新预览")
                current_action = (
                    "skip" if _same_branch(current, payload) else "update"
                )
                if current_action != action:
                    _fail("IMPORT_STATE_CONFLICT", "门店数据已变更，请重新预览")
            classified.append(item)
        try:
            raw_business_values = _decrypt_staging(
                str(ledger["business_values_json"] or ""),
                tenant_id=tid,
                industry=industry,
                request_key=str(ledger["request_key"]),
                purpose="business-values",
            )
            business_values = json.loads(raw_business_values)
        except (
            _StagingCipherError, TypeError, ValueError,
            json.JSONDecodeError,
        ):
            _fail(
                "IMPORT_PREVIEW_EXPIRED",
                "导入预览密钥已失效，请重新上传",
            )
        if not isinstance(business_values, list):
            _fail("IMPORT_STATE_CONFLICT", "导入暂存数据无效")
        branch_by_code = {
            str(item["payload"]["store_code"]): item for item in classified
        }
        validated_business = []
        business_candidates = []
        seen_business_rows: set[int] = set()
        seen_natural_keys: set[tuple] = set()
        external_branch_codes = {
            str(value.get("store_code") or "")
            for value in business_values if isinstance(value, dict)
            and str(value.get("store_code") or "") not in branch_by_code
        }
        external_branches = _branches_by_code(
            connection, tid, ledger["industry_key"], external_branch_codes,
        )
        for value in business_values:
            if not isinstance(value, dict):
                _fail("IMPORT_STATE_CONFLICT", "导入暂存数据无效")
            try:
                logical_row = int(value["row_number"])
                code = str(value["store_code"])
                metric = str(value["metric_key"])
                period_start = str(value["period_start"])
                period_end = str(value["period_end"])
                action = str(value["action"])
                branch_baseline_id = int(value["existing_branch_id"] or 0)
                branch_baseline_version = int(
                    value["existing_row_version"] or 0
                )
                value_baseline_id = int(
                    value["existing_business_value_id"] or 0
                )
                value_baseline_version = int(
                    value["existing_business_row_version"] or 0
                )
            except (KeyError, TypeError, ValueError):
                _fail("IMPORT_STATE_CONFLICT", "导入暂存数据无效")
            natural = (code, metric, period_start, period_end)
            stage = business_stage_rows.get(logical_row)
            if (
                logical_row in seen_business_rows
                or natural in seen_natural_keys
                or stage is None
                or action not in {"create", "update", "skip"}
                or str(stage["action"] or "") != action
                or int(stage["existing_branch_id"] or 0)
                != branch_baseline_id
                or int(stage["existing_row_version"] or 0)
                != branch_baseline_version
                or int(stage["existing_business_value_id"] or 0)
                != value_baseline_id
                or int(stage["existing_business_row_version"] or 0)
                != value_baseline_version
            ):
                _fail("IMPORT_STATE_CONFLICT", "导入暂存数据无效")
            try:
                staged_raw = _decrypt_staging(
                    str(stage["payload_json"] or ""),
                    tenant_id=tid,
                    industry=industry,
                    request_key=str(ledger["request_key"]),
                    purpose=f"row:{int(stage['row_number'])}",
                )
                staged_payload = json.loads(staged_raw)
            except (
                _StagingCipherError, TypeError, ValueError,
                json.JSONDecodeError,
            ):
                _fail(
                    "IMPORT_PREVIEW_EXPIRED",
                    "导入预览密钥已失效，请重新上传",
                )
            if staged_payload != _business_public_payload(value, redact=False):
                _fail("IMPORT_STATE_CONFLICT", "导入暂存数据无效")
            seen_business_rows.add(logical_row)
            seen_natural_keys.add(natural)

            branch_item = branch_by_code.get(code)
            if branch_item is not None:
                if branch_item["action"] == "create":
                    if branch_baseline_id != 0 or branch_baseline_version != 0:
                        _fail(
                            "IMPORT_STATE_CONFLICT",
                            "经营数据门店已变更，请重新预览",
                        )
                    current_branch_id = 0
                else:
                    current_branch_id = int(
                        branch_item["existing_branch_id"]
                    )
                    if (
                        branch_baseline_id != current_branch_id
                        or branch_baseline_version
                        != int(branch_item["existing_row_version"])
                    ):
                        _fail(
                            "IMPORT_STATE_CONFLICT",
                            "经营数据门店已变更，请重新预览",
                        )
            else:
                branch = external_branches.get(code)
                if (
                    branch is None
                    or int(branch["id"]) != branch_baseline_id
                    or int(branch["row_version"] or 0)
                    != branch_baseline_version
                ):
                    _fail(
                        "IMPORT_STATE_CONFLICT",
                        "经营数据门店已变更，请重新预览",
                    )
                current_branch_id = int(branch["id"])
            business_candidates.append({
                "value": value,
                "action": action,
                "metric": metric,
                "period_start": period_start,
                "period_end": period_end,
                "current_branch_id": current_branch_id,
                "value_baseline_id": value_baseline_id,
                "value_baseline_version": value_baseline_version,
            })

        if set(business_stage_rows) != seen_business_rows:
            _fail("IMPORT_STATE_CONFLICT", "导入暂存数据无效")
        current_values = _business_values_by_natural_key(
            connection,
            tid,
            ledger["industry_key"],
            [
                (
                    item["current_branch_id"], item["metric"],
                    item["period_start"], item["period_end"],
                )
                for item in business_candidates
                if int(item["current_branch_id"]) > 0
            ],
        )
        for item in business_candidates:
            value = item["value"]
            action = item["action"]
            current_branch_id = int(item["current_branch_id"])
            value_baseline_id = int(item["value_baseline_id"])
            value_baseline_version = int(item["value_baseline_version"])
            current_value = current_values.get((
                current_branch_id,
                item["metric"],
                item["period_start"],
                item["period_end"],
            )) if current_branch_id > 0 else None
            if value_baseline_id == 0 and value_baseline_version == 0:
                if current_value is not None or action != "create":
                    _fail(
                        "IMPORT_STATE_CONFLICT",
                        "经营数据已变更，请重新预览",
                    )
            elif (
                value_baseline_id <= 0
                or value_baseline_version <= 0
                or current_value is None
                or int(current_value["id"]) != value_baseline_id
                or int(current_value.get("row_version") or 0)
                != value_baseline_version
            ):
                _fail(
                    "IMPORT_STATE_CONFLICT",
                    "经营数据已变更，请重新预览",
                )
            else:
                current_action = (
                    "skip"
                    if _same_business_value(current_value, value)
                    else "update"
                )
                if current_action != action:
                    _fail(
                        "IMPORT_STATE_CONFLICT",
                        "经营数据已变更，请重新预览",
                    )
            validated_business.append(value)
        counts = {kind: 0 for kind in ("create", "update", "skip", "error")}
        branch_ids: dict[str, int] = {}
        for item in classified:
            payload = item["payload"]
            action = item["action"]
            counts[action] += 1
            if action == "create":
                columns = ["tenant_id", "industry_key", *STORE_FIELDS, "created_by", "created_at", "updated_at"]
                values = [tid, ledger["industry_key"], *[payload[key] for key in STORE_FIELDS], uid, now, now]
                marks = ",".join("?" for _ in columns)
                cursor = connection.execute(
                    f"INSERT INTO store_branch({','.join(columns)}) VALUES({marks})", values
                )
                branch_ids[payload["store_code"]] = int(cursor.lastrowid)
            else:
                branch_id = int(item["existing_branch_id"])
                branch_ids[payload["store_code"]] = branch_id
                if action == "update":
                    assignments = ",".join(f"{field}=?" for field in STORE_FIELDS)
                    updated = connection.execute(
                        f"UPDATE store_branch SET {assignments},updated_at=?,"
                        "row_version=row_version+1 WHERE tenant_id=? "
                        "AND industry_key=? AND id=? AND row_version=?",
                        (
                            *[payload[key] for key in STORE_FIELDS], now,
                            tid, ledger["industry_key"], branch_id,
                            int(item["existing_row_version"]),
                        ),
                    )
                    if updated.rowcount != 1:
                        _fail(
                            "IMPORT_STATE_CONFLICT",
                            "门店数据已变更，请重新预览",
                        )
            connection.execute(
                "UPDATE inspection_branch_import_row SET action=? WHERE import_id=? AND row_number=?",
                (action, iid, int(item["row_number"])),
            )
        business_counts = {
            kind: 0 for kind in ("create", "update", "skip", "error")
        }
        for value in validated_business:
            action = str(value["action"])
            business_counts[action] += 1
            branch_id = branch_ids.get(value["store_code"])
            if branch_id is None:
                branch_id = int(value["existing_branch_id"] or 0)
                if branch_id <= 0:
                    _fail("IMPORT_STATE_CONFLICT", "经营数据门店已不存在")
            if action == "create":
                connection.execute(
                    "INSERT INTO inspection_business_value(tenant_id,industry_key,"
                    "branch_id,import_id,metric_key,period_start,period_end,value,"
                    "unit,source_ref,remark,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tid, ledger["industry_key"], branch_id, iid,
                        value["metric_key"], value["period_start"],
                        value["period_end"], value["value"], value["unit"],
                        value["source_ref"], value["remark"], now, now,
                    ),
                )
            elif action == "update":
                updated = connection.execute(
                    "UPDATE inspection_business_value SET value=?,unit=?,"
                    "source_ref=?,remark=?,import_id=?,updated_at=?,"
                    "row_version=row_version+1 WHERE tenant_id=? "
                    "AND industry_key=? AND branch_id=? AND id=? "
                    "AND metric_key=? AND period_start=? AND period_end=? "
                    "AND row_version=?",
                    (
                        value["value"], value["unit"], value["source_ref"],
                        value["remark"], iid, now, tid,
                        ledger["industry_key"], branch_id,
                        int(value["existing_business_value_id"]),
                        value["metric_key"], value["period_start"],
                        value["period_end"],
                        int(value["existing_business_row_version"]),
                    ),
                )
                if updated.rowcount != 1:
                    _fail(
                        "IMPORT_STATE_CONFLICT",
                        "经营数据已变更，请重新预览",
                    )
        # 提交后权威数据已经分别进入 store_branch 与
        # inspection_business_value。先将脱敏行坐标写入可验证的
        # content-addressed 压缩归档，再在同一事务中移除重复 working rows；
        # 归档失败会让整个 BEGIN IMMEDIATE 回滚，绝不留下半套提交。
        _archive_committed_rows(connection, ledger, raw_rows)
        connection.execute(
            "UPDATE inspection_branch_import SET status='committed',create_count=?,"
            "update_count=?,skip_count=?,error_count=0,business_create_count=?,"
            "business_update_count=?,business_skip_count=?,business_error_count=0,"
            "committed_by=?,committed_at=?,"
            "business_values_json='[]',staging_purged_at=?,updated_at=? "
            "WHERE id=? AND tenant_id=? AND status='previewed'",
            (
                counts["create"], counts["update"], counts["skip"],
                business_counts["create"], business_counts["update"],
                business_counts["skip"], uid, now, now, now, iid, tid,
            ),
        )
        saved = connection.execute(
            "SELECT * FROM inspection_branch_import WHERE id=?", (iid,)
        ).fetchone()
        return _public_import(connection, saved)


def commit_import(
    tenant_id: int, actor_id: int, import_id: int, industry_key: str,
) -> dict:
    """Commit once; retire a CAS-conflicted snapshot without deleting staging."""
    try:
        return _commit_import_once(
            tenant_id, actor_id, import_id, industry_key,
        )
    except ImportContractError as exc:
        if exc.code not in {"IMPORT_STATE_CONFLICT", "IMPORT_PREVIEW_EXPIRED"}:
            raise
        try:
            tid, uid, iid = int(tenant_id), int(actor_id), int(import_id)
            _, _, industry = _authorize(tid, uid, industry_key)
        except (TypeError, ValueError, ImportContractError):
            raise exc
        # The failed transaction has rolled back.  Only retire the stale CAS or
        # undecryptable snapshot here; encrypted staging remains available for
        # authorized audit until the ordinary 24-hour lifecycle scrub runs.
        with db.atomic() as connection:
            connection.execute(
                "UPDATE inspection_branch_import SET status='expired',updated_at=? "
                "WHERE id=? AND tenant_id=? AND industry_key=? "
                "AND status='previewed'",
                (time.time(), iid, tid, industry),
            )
        raise


def get_import(
    tenant_id: int, actor_id: int, import_id: int, industry_key: str,
    *,
    limit: Any = None,
    cursor: Any = None,
    errors_only: bool = False,
    row_kind: Any = None,
) -> dict:
    """Return one bounded, PII-masked page of an import ledger."""
    try:
        tid, uid, iid = int(tenant_id), int(actor_id), int(import_id)
    except (TypeError, ValueError):
        _fail("IMPORT_NOT_FOUND", "导入记录不存在")
    _, _, industry = _authorize(tid, uid, industry_key)
    with db.atomic() as connection:
        _expire_stale_previews(connection, tid, time.time())
        row = connection.execute(
            "SELECT i.* FROM inspection_branch_import i "
            "JOIN tenant_industry ti ON ti.tenant_id=i.tenant_id "
            "AND ti.industry_key=i.industry_key "
            "WHERE i.id=? AND i.tenant_id=? AND i.industry_key=?",
            (iid, tid, industry),
        ).fetchone()
        if not row:
            _fail("IMPORT_NOT_FOUND", "导入记录不存在")
        if row["status"] == "previewed" and not _preview_cipher_valid(row):
            _expire_preview(connection, iid, tid, time.time())
            row = connection.execute(
                "SELECT * FROM inspection_branch_import WHERE id=?", (iid,),
            ).fetchone()
        return _public_import(
            connection, row, limit=limit, cursor=cursor,
            errors_only=errors_only, row_kind=row_kind,
        )


def _previous_year(iso_date: str) -> str:
    value = dt.date.fromisoformat(iso_date)
    try:
        return value.replace(year=value.year - 1).isoformat()
    except ValueError:  # 2月29日对比上年的 2月28日。
        return value.replace(year=value.year - 1, day=28).isoformat()


def business_comparison(
    tenant_id: int, industry_key: str, branch_id: int,
) -> dict:
    """Return real imported values only; missing comparisons remain ``None``."""
    tid, industry = _scope(tenant_id, industry_key)
    try:
        bid = int(branch_id)
    except (TypeError, ValueError):
        _fail("BRANCH_NOT_FOUND", "门店不存在")
    branch = db.one(
        "SELECT id FROM store_branch WHERE id=? AND tenant_id=? AND industry_key=?",
        (bid, tid, industry),
    )
    if not branch:
        _fail("BRANCH_NOT_FOUND", "门店不存在")
    try:
        catalog = inspectionstandards.metric_catalog(industry)
    except inspectionstandards.InspectionStandardError:
        _fail("METRIC_CATALOG_UNAVAILABLE", "该行业暂无经营指标口径")
    stored = db.q(
        "SELECT metric_key,period_start,period_end,value,unit,source_ref "
        "FROM inspection_business_value WHERE tenant_id=? AND industry_key=? "
        "AND branch_id=? ORDER BY metric_key,period_end DESC,period_start DESC,id DESC",
        (tid, industry, bid),
    )
    by_metric: dict[str, list[dict]] = {}
    for row in stored:
        by_metric.setdefault(str(row["metric_key"]), []).append(row)
    metrics = []
    for definition in catalog:
        code = str(definition["metric_code"])
        rows = by_metric.get(code, [])
        actual_row = rows[0] if rows else None
        previous_row = rows[1] if len(rows) > 1 else None
        same_year_row = None
        if actual_row:
            wanted_start = _previous_year(str(actual_row["period_start"]))
            wanted_end = _previous_year(str(actual_row["period_end"]))
            same_year_row = next((
                row for row in rows[1:]
                if row["period_start"] == wanted_start
                and row["period_end"] == wanted_end
            ), None)
        available = actual_row is not None
        reason_codes = {
            "actual": None if actual_row else "metric_data_unavailable",
            "previous_period": (
                None if previous_row else "previous_period_unavailable"
            ),
            "same_period_last_year": (
                None
                if same_year_row
                else "same_period_last_year_unavailable"
            ),
            # Targets and benchmarks are deliberately not synthesized from
            # observations or formulas.  Until an authoritative value is
            # imported/configured, callers receive a stable absence reason.
            "target": "target_not_configured",
            "benchmark": "benchmark_not_configured",
        }
        metrics.append({
            "metric_code": code,
            "label": definition["label"],
            "formula": definition["formula"],
            "required_inputs": list(definition["required_inputs"]),
            "actual": actual_row["value"] if actual_row else None,
            "previous_period": previous_row["value"] if previous_row else None,
            "same_period_last_year": (
                same_year_row["value"] if same_year_row else None
            ),
            "target": None,
            "benchmark": None,
            "unit": (
                actual_row["unit"] if actual_row else definition["unit"]
            ),
            "source_ref": actual_row["source_ref"] if actual_row else None,
            "period": ({
                "start": actual_row["period_start"],
                "end": actual_row["period_end"],
            } if actual_row else None),
            "availability": available,
            "reason_code": None if available else "metric_data_unavailable",
            "actual_reason_code": reason_codes["actual"],
            "previous_period_reason_code": reason_codes["previous_period"],
            "same_period_last_year_reason_code": (
                reason_codes["same_period_last_year"]
            ),
            "target_reason_code": reason_codes["target"],
            "benchmark_reason_code": reason_codes["benchmark"],
            "reason_codes": reason_codes,
        })
    availability = any(item["availability"] for item in metrics)
    return {
        "tenant_id": tid,
        "industry_key": industry,
        "branch_id": bid,
        "availability": availability,
        "reason_code": None if availability else "business_data_unavailable",
        "metrics": metrics,
    }
