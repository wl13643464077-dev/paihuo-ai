#!/usr/bin/env python3
"""把最新的已校验本机备份加密后推送到老板授权的异地位置。

背景(审核 P0-7):本机备份链(backup_db → verify_backup → backup_health)已经
完整,但单机全损(火灾/盗抢/磁盘全坏/勒索)仍等于全量数据丢失。异地备份此前
只存在于 BACKUP_RECOVERY.md 的一句话里。

设计边界(与文档的授权要求一致):
- **未配置目的地即拒绝运行。** 远端主机/挂载路径由老板明确指定,配置本身就是
  授权动作;本工具绝不自选云、绝不擅自外传。
- **只传密文。** 用独立的异地备份密钥(PAIHUO_OFFSITE_KEY,与会话/配置密钥
  互相独立)做 Fernet 加密;远端拿到的是密文+清单,不含任何明文业务数据。
  密钥本身绝不进入推送内容、子进程 argv 或远端;丢了密钥密文即废,所以
  BACKUP_RECOVERY.md 要求把该密钥抄写进离线恢复信封。
- **推完必须验回。** 本地路径(挂载的 NAS/对象存储)重读比对 SHA-256;
  ssh 目的地经由远端 sha256sum 比对。验不上就报失败,不留"以为传好了"。
- **留痕供监控。** 成功后写本地回执(HMAC 防篡改,复用 backup_health 的
  attestation 机制),backup_health --check-offsite 据此判新鲜度,异地断流
  会像本地备份过期一样被告警出来。

目的地形式:
  PAIHUO_OFFSITE_DEST=/mnt/offsite/paihuo          # 已挂载的异地存储
  PAIHUO_OFFSITE_DEST=ssh://backup@host:/srv/paihuo # rsync over ssh
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.session_secret import validate_secret_token          # noqa: E402
from deploy.backup_db import MANAGED_BACKUP_RE                # noqa: E402
from deploy.verify_backup import VerificationError, verify_database  # noqa: E402

UTC = timezone.utc

DEST_ENV = "PAIHUO_OFFSITE_DEST"
KEY_ENV = "PAIHUO_OFFSITE_KEY"
DEFAULT_RECEIPT = "/var/lib/paihuo-upgrade/latest-offsite-backup.json"

# Fernet 是整体加密:超过上限就拒绝并提示分库/换流式方案,而不是悄悄吃掉内存。
MAX_PLAINTEXT_BYTES = 2 * 1024 * 1024 * 1024

_SSH_DEST_RE = re.compile(
    r"^ssh://(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9._-]+):(?P<path>/.+)$"
)
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class OffsiteError(RuntimeError):
    """异地备份没有完成;调用方必须把它当作失败告警。"""


def _utcnow(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise OffsiteError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _fernet(key_material: str):
    """从高熵 token 派生异地专用 Fernet 密钥;域分隔,不与其他密钥互推。"""
    from cryptography.fernet import Fernet

    material = validate_secret_token(key_material, variable=KEY_ENV)
    derived = hashlib.sha256(b"paihuo-offsite-v1\0" + material).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def latest_verified_backup(backup_dir: Path) -> Path:
    """选出本机最新的受管备份;没有可用备份时明确失败。"""
    if not backup_dir.is_dir():
        raise OffsiteError(f"backup directory does not exist: {backup_dir}")
    candidates = sorted(
        (
            entry
            for entry in backup_dir.iterdir()
            if entry.is_file()
            and not entry.is_symlink()
            and MANAGED_BACKUP_RE.match(entry.name)
        ),
        key=lambda entry: entry.name,
    )
    if not candidates:
        raise OffsiteError(f"no managed backups found in {backup_dir}")
    return candidates[-1]


def encrypt_backup(
    source: Path,
    staging_dir: Path,
    key_material: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """加密 + 双哈希清单。返回 {payload_path, manifest_path, ...}。"""
    verification = verify_database(source)
    size = int(verification["size_bytes"])
    if size > MAX_PLAINTEXT_BYTES:
        raise OffsiteError(
            "database exceeds offsite single-file limit; "
            "split archives or a streaming pipeline is required"
        )
    plaintext = source.read_bytes()
    plain_sha = hashlib.sha256(plaintext).hexdigest()
    if plain_sha != verification["sha256"]:
        raise OffsiteError("backup changed while being read; retry")

    token = _fernet(key_material).encrypt(plaintext)
    del plaintext
    cipher_sha = hashlib.sha256(token).hexdigest()

    current = _utcnow(now)
    payload_name = f"{source.name}.enc"
    manifest_name = f"{source.name}.manifest.json"
    payload_path = staging_dir / payload_name
    manifest_path = staging_dir / manifest_name
    with open(payload_path, "wb") as handle:
        handle.write(token)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(payload_path, 0o600)

    manifest = {
        "format": "paihuo-offsite-v1",
        "source_backup": source.name,
        "created_at_utc": current.isoformat().replace("+00:00", "Z"),
        "plaintext_sha256": verification["sha256"],
        "plaintext_bytes": size,
        "ciphertext_sha256": cipher_sha,
        "ciphertext_bytes": len(token),
        "integrity_check": verification["integrity_check"],
        "schema_digest": verification["schema_digest"],
    }
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(manifest_path, 0o600)
    return {
        "payload_path": payload_path,
        "manifest_path": manifest_path,
        "manifest": manifest,
    }


def decrypt_backup(payload: Path, key_material: str) -> bytes:
    """恢复演练/取回用:解开密文,返回明文字节。"""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet(key_material).decrypt(payload.read_bytes())
    except InvalidToken as exc:
        raise OffsiteError(
            "ciphertext does not match the offsite key (wrong key or corrupt file)"
        ) from exc


def parse_destination(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise OffsiteError(
            f"offsite destination is not configured; set {DEST_ENV} "
            "(configuring it is the boss's authorization step)"
        )
    match = _SSH_DEST_RE.match(text)
    if match:
        return {"kind": "ssh", **match.groupdict()}
    if text.startswith("ssh://"):
        raise OffsiteError(
            "ssh destination must look like ssh://user@host:/absolute/path"
        )
    path = Path(text)
    if not path.is_absolute():
        raise OffsiteError("local offsite destination must be an absolute path")
    return {"kind": "local", "path": path}


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _remote_sha256(dest: dict[str, Any], name: str, *, timeout: int) -> str:
    if not _REMOTE_NAME_RE.match(name):
        raise OffsiteError(f"unsafe remote file name: {name}")
    quoted = shlex.quote(f"{dest['path'].rstrip('/')}/{name}")
    proc = _run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            f"{dest['user']}@{dest['host']}",
            f"sha256sum {quoted}",
        ],
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise OffsiteError(
            f"remote hash verification failed for {name}: rc={proc.returncode}"
        )
    fields = (proc.stdout or "").split()
    if not fields or len(fields[0]) != 64:
        raise OffsiteError(f"remote hash output is malformed for {name}")
    return fields[0]


def _push_ssh(
    dest: dict[str, Any], files: list[Path], *, timeout: int
) -> None:
    target = f"{dest['user']}@{dest['host']}:{dest['path'].rstrip('/')}/"
    command = [
        "rsync",
        "--times",
        "--chmod=F600,D700",
        "-e", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
        *[str(path) for path in files],
        target,
    ]
    proc = _run(command, timeout=timeout)
    if proc.returncode != 0:
        raise OffsiteError(f"rsync push failed: rc={proc.returncode}")


def _push_local(dest_path: Path, files: list[Path]) -> None:
    dest_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = stat.S_IMODE(dest_path.stat().st_mode)
    if mode & 0o077:
        raise OffsiteError(
            f"offsite directory {dest_path} must not be group/world accessible"
        )
    for source in files:
        target = dest_path / source.name
        staged = dest_path / f".partial-{source.name}"
        staged.write_bytes(source.read_bytes())
        os.chmod(staged, 0o600)
        os.replace(staged, target)
    directory_fd = os.open(dest_path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _verify_pushed(
    dest: dict[str, Any], staged: dict[str, Any], *, timeout: int
) -> None:
    manifest = staged["manifest"]
    expected = {
        staged["payload_path"].name: manifest["ciphertext_sha256"],
        staged["manifest_path"].name: hashlib.sha256(
            staged["manifest_path"].read_bytes()
        ).hexdigest(),
    }
    for name, digest in expected.items():
        if dest["kind"] == "local":
            actual = hashlib.sha256(
                (dest["path"] / name).read_bytes()
            ).hexdigest()
        else:
            actual = _remote_sha256(dest, name, timeout=timeout)
        if not hmac.compare_digest(actual, digest):
            raise OffsiteError(
                f"pushed file failed verification: {name} "
                "(remote copy does not match what we sent)"
            )


def _prune_remote(
    dest: dict[str, Any],
    *,
    keep: int,
    timeout: int,
) -> list[str]:
    """远端保留最近 keep 份(按文件名时间序);删不动只告警不失败。"""
    removed: list[str] = []
    if keep < 1:
        raise OffsiteError("keep must be at least 1")
    if dest["kind"] == "local":
        payloads = sorted(
            entry
            for entry in dest["path"].iterdir()
            if entry.name.endswith(".enc") and entry.is_file()
        )
        for stale in payloads[:-keep]:
            manifest = dest["path"] / (
                stale.name[: -len(".enc")] + ".manifest.json"
            )
            for path in (stale, manifest):
                if path.exists():
                    path.unlink()
                    removed.append(path.name)
        return removed
    listing = _run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            f"{dest['user']}@{dest['host']}",
            f"ls -1 {shlex.quote(dest['path'].rstrip('/'))}",
        ],
        timeout=timeout,
    )
    if listing.returncode != 0:
        return removed          # 列不动远端就不做清理;推送与校验已成功
    payload_names = sorted(
        line.strip()
        for line in (listing.stdout or "").splitlines()
        if line.strip().endswith(".enc") and _REMOTE_NAME_RE.match(line.strip())
    )
    for stale in payload_names[:-keep]:
        manifest_name = stale[: -len(".enc")] + ".manifest.json"
        for name in (stale, manifest_name):
            quoted = shlex.quote(f"{dest['path'].rstrip('/')}/{name}")
            proc = _run(
                [
                    "ssh",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=accept-new",
                    f"{dest['user']}@{dest['host']}",
                    f"rm -f {quoted}",
                ],
                timeout=timeout,
            )
            if proc.returncode == 0:
                removed.append(name)
    return removed


def _write_receipt(path: Path, report: dict[str, Any], key_material: str) -> None:
    """HMAC 回执:backup_health 据此判断异地链新鲜度,且不可被普通用户伪造。"""
    material = validate_secret_token(key_material, variable=KEY_ENV)
    mac_key = hashlib.sha256(b"paihuo-offsite-receipt-v1\0" + material).digest()
    body = dict(report)
    serialized = json.dumps(body, ensure_ascii=False, sort_keys=True)
    body["hmac"] = hmac.new(
        mac_key, serialized.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, staged = tempfile.mkstemp(
        prefix=".offsite-receipt-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    except OSError:
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass
        raise


def read_receipt(path: Path, key_material: str) -> dict[str, Any]:
    """校验回执 HMAC 后返回;验不过按「没有可信回执」处理。"""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OffsiteError(f"offsite receipt unreadable: {exc}") from exc
    supplied = body.pop("hmac", "")
    material = validate_secret_token(key_material, variable=KEY_ENV)
    mac_key = hashlib.sha256(b"paihuo-offsite-receipt-v1\0" + material).digest()
    expected = hmac.new(
        mac_key,
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise OffsiteError("offsite receipt failed authentication")
    return body


def push_offsite(
    backup_dir: str | os.PathLike[str],
    *,
    destination: str | None = None,
    key_material: str | None = None,
    receipt_path: str | os.PathLike[str] = DEFAULT_RECEIPT,
    keep: int = 14,
    timeout: int = 600,
    now: datetime | None = None,
) -> dict[str, Any]:
    """主流程:选最新已验备份 → 加密 → 推送 → 验回 → 远端保留 → 写回执。"""
    destination = destination if destination is not None else os.environ.get(DEST_ENV, "")
    key_material = key_material if key_material is not None else os.environ.get(KEY_ENV, "")
    if not key_material:
        raise OffsiteError(
            f"offsite encryption key is not configured; set {KEY_ENV} "
            "(and copy it into the offline recovery envelope)"
        )
    dest = parse_destination(destination)
    source = latest_verified_backup(Path(backup_dir))
    current = _utcnow(now)

    with tempfile.TemporaryDirectory(prefix="paihuo-offsite-") as staging:
        staged = encrypt_backup(
            source, Path(staging), key_material, now=current
        )
        files = [staged["payload_path"], staged["manifest_path"]]
        if dest["kind"] == "local":
            _push_local(dest["path"], files)
        else:
            _push_ssh(dest, files, timeout=timeout)
        _verify_pushed(dest, staged, timeout=timeout)
        pruned = _prune_remote(dest, keep=keep, timeout=timeout)

    report = {
        "ok": True,
        "pushed_at_utc": current.isoformat().replace("+00:00", "Z"),
        "source_backup": source.name,
        "destination_kind": dest["kind"],
        "plaintext_sha256": staged["manifest"]["plaintext_sha256"],
        "ciphertext_sha256": staged["manifest"]["ciphertext_sha256"],
        "ciphertext_bytes": staged["manifest"]["ciphertext_bytes"],
        "pruned_remote": pruned,
    }
    _write_receipt(Path(receipt_path), report, key_material)
    return report


def check_offsite_freshness(
    receipt_path: str | os.PathLike[str],
    *,
    key_material: str | None = None,
    max_age_hours: float = 26.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """给监控用:回执缺失/伪造/过期都算异地链断,fail closed。"""
    key_material = key_material if key_material is not None else os.environ.get(KEY_ENV, "")
    if not key_material:
        raise OffsiteError(f"offsite key is not configured; set {KEY_ENV}")
    body = read_receipt(Path(receipt_path), key_material)
    pushed_raw = str(body.get("pushed_at_utc") or "")
    try:
        pushed = datetime.fromisoformat(pushed_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OffsiteError("offsite receipt timestamp is invalid") from exc
    age = (_utcnow(now) - pushed).total_seconds() / 3600.0
    if age > max_age_hours:
        raise OffsiteError(
            f"offsite backup is stale: {age:.1f}h old (limit {max_age_hours}h)"
        )
    return {"ok": True, "age_hours": age, **body}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--receipt", default=DEFAULT_RECEIPT)
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--check-freshness",
        action="store_true",
        help="only verify the receipt freshness (for monitoring)",
    )
    parser.add_argument("--max-age-hours", type=float, default=26.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.check_freshness:
            report = check_offsite_freshness(
                args.receipt, max_age_hours=args.max_age_hours
            )
        else:
            report = push_offsite(
                args.backup_dir,
                receipt_path=args.receipt,
                keep=args.keep,
                timeout=args.timeout,
            )
    except (OffsiteError, VerificationError, ValueError) as exc:
        print(f"offsite backup failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
