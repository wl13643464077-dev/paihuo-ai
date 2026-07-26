#!/usr/bin/env python3
"""Transactional installer for Paihuo's root-owned deployment control plane.

The application release symlink and SQLite database are handled by
``deploy.upgrade_release``.  This module owns the other half of the deployment
transaction: fixed operators, systemd units and Caddy configuration.

Snapshots are immutable, root-only directories.  A snapshot is never replaced;
rerunning the same release validates and reuses the original snapshot.  Every
published attestation contains only paths, metadata and digests--never file
contents or environment values.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Sequence
import uuid


UTC = timezone.utc
ATTESTATION_FORMAT = "paihuo-control-plane-attestation/v1"
TERMINAL_UPGRADE_STATUSES = {
    "succeeded",
    "rolled_back",
    "recovered_rollback",
    "failed_before_cutover",
    "failed_before_cutover_recovered",
    "checkpoint_ready",
}
RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
UNIT_FILES = (
    "contentcrew.service",
    "paihuo-backup.service",
    "paihuo-backup.timer",
    "paihuo-backup-health.service",
    "paihuo-backup-health.timer",
    "paihuo-failure-alert@.service",
)
RUNTIME_UNITS = tuple(
    unit for unit in UNIT_FILES if "@" not in unit
)
OPS_FILES = (
    "app/__init__.py",
    "app/session_secret.py",
    "deploy/__init__.py",
    "deploy/backup_db.py",
    "deploy/backup_health.py",
    "deploy/bootstrap_release.py",
    "deploy/control_plane.py",
    "deploy/failure_alert.py",
    "deploy/session_secret_env.py",
    "deploy/smoke_readonly.py",
    "deploy/start_guard.py",
    "deploy/upgrade.sh",
    "deploy/upgrade_release.py",
    "deploy/verify_backup.py",
    "deploy/verify_release.py",
    "deploy/wheelhouse.py",
)
CONTENTCREW_REQUIRED_LINES = (
    "EnvironmentFile=/etc/paihuo/paihuo.env",
    "Environment=CONTENTCREW_REQUIRE_SESSION_SECRET=1",
    "Environment=CONTENTCREW_REQUIRE_CONFIG_KEY=1",
    "ExecStartPre=+/usr/bin/env PYTHONPATH=/usr/local/lib/paihuo-ops "
    "/usr/bin/python3 -m deploy.session_secret_env --check "
    "--path /etc/paihuo/paihuo.env",
)


class ControlPlaneError(RuntimeError):
    """The control plane could not be proven installed or restored."""


@dataclass(frozen=True)
class Layout:
    ops_link: Path = Path("/usr/local/lib/paihuo-ops")
    launcher: Path = Path("/usr/local/sbin/paihuo-upgrade")
    systemd_dir: Path = Path("/etc/systemd/system")
    caddy_dropin: Path = Path(
        "/etc/systemd/system/caddy.service.d/10-paihuo-guard.conf"
    )
    caddyfile: Path = Path("/etc/caddy/Caddyfile")
    caddy_bin: Path = Path("/usr/bin/caddy")
    start_permit: Path = Path("/run/paihuo-upgrade/start.permit")


def _owner_uid() -> int:
    return 0 if os.geteuid() == 0 else os.geteuid()


def _owner_gid() -> int:
    return 0 if os.geteuid() == 0 else os.getegid()


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ControlPlaneError(f"not a regular file: {path}")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)
    finally:
        os.close(descriptor)


def _metadata(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"type": "missing"}
    common = {
        "gid": info.st_gid,
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
        "uid": info.st_uid,
    }
    if stat.S_ISLNK(info.st_mode):
        return {**common, "target": os.readlink(path), "type": "symlink"}
    if stat.S_ISREG(info.st_mode):
        return {**common, "sha256": _sha256(path), "type": "file"}
    if stat.S_ISDIR(info.st_mode):
        return {**common, "type": "directory"}
    raise ControlPlaneError(f"unsupported control-plane object: {path}")


def _assert_root_only_dir(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ControlPlaneError(f"unsafe control directory: {path}")
    info = path.lstat()
    if (
        info.st_uid != _owner_uid()
        or info.st_gid != _owner_gid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ControlPlaneError("control directory must be owner-controlled 0700")
    return path.resolve(strict=True)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _assert_root_only_dir(path.parent)
    if path.is_symlink():
        raise ControlPlaneError("attestation path must not be a symlink")
    staged = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    descriptor = os.open(
        staged,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, _owner_uid(), _owner_gid())
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        _fsync_dir(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _read_attestation(path: Path) -> dict[str, Any]:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != _owner_uid()
            or info.st_gid != _owner_gid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ControlPlaneError("control-plane attestation is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            report = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ControlPlaneError("cannot read control-plane attestation") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(report, dict) or report.get("format") != ATTESTATION_FORMAT:
        raise ControlPlaneError("invalid control-plane attestation")
    return report


def attestation_digest(path: str | os.PathLike[str]) -> str:
    target = Path(path).absolute()
    _read_attestation(target)
    return _sha256(target)


def _safe_candidate_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ControlPlaneError(f"candidate control-plane file missing: {path.name}")
    info = path.lstat()
    if info.st_uid != _owner_uid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ControlPlaneError(
            f"candidate control-plane file is not owner-controlled: {path.name}"
        )


def _candidate_records(release: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    paths = [release / item for item in OPS_FILES]
    paths += [release / "deploy" / item for item in UNIT_FILES]
    paths += [
        release / "deploy" / "caddy-paihuo-guard.conf",
        release / "deploy" / "Caddyfile",
    ]
    for path in paths:
        _safe_candidate_file(path)
        relative = path.relative_to(release).as_posix()
        result[relative] = _metadata(path)
    return result


def _tree_digest(records: dict[str, dict[str, Any]]) -> str:
    body = (
        json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _copy_snapshot_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ControlPlaneError(f"snapshot source is not regular: {source}")
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    os.chmod(destination, 0o600)
    os.chown(destination, _owner_uid(), _owner_gid())


def _snapshot_entry(
    *,
    source: Path,
    snapshot: Path,
    key: str,
    records: dict[str, dict[str, Any]],
) -> None:
    record = _metadata(source)
    records[key] = {"path": str(source), **record}
    if record["type"] == "file":
        info = source.lstat()
        if (
            info.st_uid != _owner_uid()
            or info.st_gid != _owner_gid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_nlink != 1
        ):
            raise ControlPlaneError("original control-plane file is unsafe")
        copy = snapshot / "objects" / key
        _copy_snapshot_file(source, copy)
        records[key]["snapshot"] = str(copy.relative_to(snapshot))


def _verify_snapshot(snapshot: Path, report: dict[str, Any]) -> None:
    records = report.get("original")
    if (
        not isinstance(records, dict)
        or _tree_digest(records) != report.get("snapshot_digest")
    ):
        raise ControlPlaneError("control-plane snapshot manifest mismatch")
    link = records.get("ops_link")
    if (
        not isinstance(link, dict)
        or link.get("type") != "symlink"
        or not link.get("resolved_target")
    ):
        raise ControlPlaneError("control-plane snapshot lacks original ops link")
    for key, record in records.items():
        if key == "ops_link":
            continue
        if not isinstance(record, dict) or record.get("type") not in {
            "file",
            "missing",
        }:
            raise ControlPlaneError("control-plane snapshot object is invalid")
        if record["type"] == "missing":
            if "snapshot" in record:
                raise ControlPlaneError("missing snapshot object has file payload")
            continue
        relative = PurePosixPath(str(record.get("snapshot") or ""))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in ("", ".", "..") for part in relative.parts)
        ):
            raise ControlPlaneError("snapshot object path is unsafe")
        source = snapshot / relative
        info = source.lstat()
        if (
            source.is_symlink()
            or not source.is_file()
            or info.st_uid != _owner_uid()
            or info.st_gid != _owner_gid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or _sha256(source) != record.get("sha256")
        ):
            raise ControlPlaneError("snapshot object integrity mismatch")
    runtime = report.get("runtime")
    if not isinstance(runtime, dict):
        raise ControlPlaneError("control-plane snapshot runtime state is missing")
    for unit in (*RUNTIME_UNITS, "caddy.service"):
        state = runtime.get(unit)
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("active"), bool)
            or not isinstance(state.get("enabled"), bool)
        ):
            raise ControlPlaneError("control-plane snapshot runtime state is invalid")


def _snapshot_control_plane(
    *,
    release: Path,
    release_id: str,
    snapshot: Path,
    candidate_digest: str,
    layout: Layout,
    runner: Callable[[Sequence[str]], Any],
) -> dict[str, Any]:
    parent = _assert_root_only_dir(snapshot.parent, create=True)
    if snapshot.exists() or snapshot.is_symlink():
        report = _read_attestation(snapshot / "attestation.json")
        if (
            report.get("release_id") != release_id
            or report.get("candidate_digest") != candidate_digest
        ):
            raise ControlPlaneError(
                "existing control-plane snapshot does not match candidate"
            )
        _verify_snapshot(snapshot, report)
        return report

    staged = parent / f".{release_id}.snapshot-{uuid.uuid4().hex}"
    staged.mkdir(mode=0o700)
    os.chown(staged, _owner_uid(), _owner_gid())
    try:
        records: dict[str, dict[str, Any]] = {}
        link_record = _metadata(layout.ops_link)
        if link_record["type"] != "symlink":
            raise ControlPlaneError("fixed ops path must be an existing symlink")
        ops_target = layout.ops_link.resolve(strict=True)
        ops_target_info = ops_target.lstat()
        if (
            not ops_target.is_dir()
            or ops_target.is_symlink()
            or ops_target_info.st_uid != _owner_uid()
            or ops_target_info.st_gid != _owner_gid()
            or stat.S_IMODE(ops_target_info.st_mode) & 0o022
        ):
            raise ControlPlaneError("fixed ops target must be a real directory")
        records["ops_link"] = {
            "path": str(layout.ops_link),
            **link_record,
            "resolved_target": str(ops_target),
        }
        for relative in OPS_FILES:
            _snapshot_entry(
                source=ops_target / PurePosixPath(relative),
                snapshot=staged,
                key=f"ops/{relative}",
                records=records,
            )
        for name in UNIT_FILES:
            _snapshot_entry(
                source=layout.systemd_dir / name,
                snapshot=staged,
                key=f"units/{name}",
                records=records,
            )
        _snapshot_entry(
            source=layout.caddy_dropin,
            snapshot=staged,
            key="caddy/dropin",
            records=records,
        )
        _snapshot_entry(
            source=layout.caddyfile,
            snapshot=staged,
            key="caddy/Caddyfile",
            records=records,
        )
        _snapshot_entry(
            source=layout.launcher,
            snapshot=staged,
            key="launcher",
            records=records,
        )
        runtime: dict[str, dict[str, bool]] = {}
        for unit in (*RUNTIME_UNITS, "caddy.service"):
            runtime[unit] = {
                "active": _systemd_boolean_state(
                    runner, unit=unit, state="active"
                ),
                "enabled": _systemd_boolean_state(
                    runner, unit=unit, state="enabled"
                ),
            }
        report = {
            "candidate_digest": candidate_digest,
            "created_at_utc": datetime.now(tz=UTC).isoformat(),
            "format": ATTESTATION_FORMAT,
            "original": records,
            "release": str(release),
            "release_id": release_id,
            "runtime": runtime,
            "snapshot_digest": _tree_digest(records),
            "status": "snapshotted",
        }
        _atomic_json(staged / "attestation.json", report)
        os.replace(staged, snapshot)
        _fsync_dir(parent)
        _verify_snapshot(snapshot, report)
        return report
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def _atomic_install(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if destination.is_symlink():
        raise ControlPlaneError(f"installation target is a symlink: {destination}")
    staged = destination.parent / f".{destination.name}.next-{uuid.uuid4().hex}"
    try:
        with source.open("rb") as incoming, staged.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.chmod(staged, mode)
        os.chown(staged, _owner_uid(), _owner_gid())
        os.replace(staged, destination)
        _fsync_dir(destination.parent)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _atomic_link(target: Path, link: Path) -> None:
    staged = link.parent / f".{link.name}.next-{uuid.uuid4().hex}"
    try:
        staged.symlink_to(target, target_is_directory=True)
        os.replace(staged, link)
        _fsync_dir(link.parent)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    )
    return result


def _run(
    runner: Callable[[Sequence[str]], Any],
    command: Sequence[str],
) -> Any:
    result = runner(list(command))
    if isinstance(result, int) and result != 0:
        raise ControlPlaneError("control-plane validation command failed")
    if hasattr(result, "returncode") and result.returncode != 0:
        raise ControlPlaneError("control-plane validation command failed")
    return result


def _probe(
    runner: Callable[[Sequence[str]], Any],
    command: Sequence[str],
) -> tuple[int, str]:
    try:
        result = runner(list(command))
    except ControlPlaneError:
        return 1, ""
    if isinstance(result, int):
        return result, ""
    return (
        int(getattr(result, "returncode", 0)),
        str(getattr(result, "stdout", "") or "").strip(),
    )


def _systemd_property(
    runner: Callable[[Sequence[str]], Any],
    property_name: str,
    *,
    unit: str = "contentcrew.service",
) -> str:
    result = _run(
        runner,
        [
            "systemctl",
            "show",
            unit,
            "-p",
            property_name,
            "--value",
        ],
    )
    return str(getattr(result, "stdout", "") or "").strip()


def _systemd_boolean_state(
    runner: Callable[[Sequence[str]], Any],
    *,
    unit: str,
    state: str,
) -> bool:
    if state == "active":
        rc, _ = _probe(runner, ["systemctl", "is-active", "--quiet", unit])
        if rc not in (0, 3):
            raise ControlPlaneError("systemd active state could not be proven")
        return rc == 0
    if state == "enabled":
        rc, _ = _probe(runner, ["systemctl", "is-enabled", "--quiet", unit])
        if rc not in (0, 1):
            raise ControlPlaneError("systemd enabled state could not be proven")
        return rc == 0
    raise ControlPlaneError("unsupported systemd state probe")


def _verify_contentcrew_unit(candidate: Path, installed: Path) -> None:
    if _sha256(candidate) != _sha256(installed):
        raise ControlPlaneError("effective contentcrew unit differs from candidate")
    body = installed.read_text(encoding="utf-8")
    for required in CONTENTCREW_REQUIRED_LINES:
        if required not in body:
            raise ControlPlaneError("contentcrew unit lacks a mandatory secret gate")


def _verify_installed(
    *,
    release: Path,
    versioned_ops: Path,
    layout: Layout,
    runner: Callable[[Sequence[str]], Any],
) -> dict[str, Any]:
    if layout.ops_link.resolve(strict=True) != versioned_ops.resolve(strict=True):
        raise ControlPlaneError("fixed ops link does not select candidate")
    installed: dict[str, dict[str, Any]] = {}
    for relative in OPS_FILES:
        source = release / PurePosixPath(relative)
        target = versioned_ops / PurePosixPath(relative)
        if _sha256(source) != _sha256(target):
            raise ControlPlaneError("installed fixed ops differ from candidate")
        target_info = target.lstat()
        expected_mode = 0o755 if relative == "deploy/upgrade.sh" else 0o644
        if (
            target.is_symlink()
            or not stat.S_ISREG(target_info.st_mode)
            or target_info.st_uid != _owner_uid()
            or target_info.st_gid != _owner_gid()
            or stat.S_IMODE(target_info.st_mode) != expected_mode
            or target_info.st_nlink != 1
        ):
            raise ControlPlaneError("installed fixed ops metadata is unsafe")
        installed[f"ops/{relative}"] = _metadata(target)
    launcher_source = release / "deploy" / "upgrade.sh"
    if _sha256(launcher_source) != _sha256(layout.launcher):
        raise ControlPlaneError("fixed launcher differs from candidate")
    launcher_metadata = _metadata(layout.launcher)
    if (
        launcher_metadata.get("type") != "file"
        or launcher_metadata.get("uid") != _owner_uid()
        or launcher_metadata.get("gid") != _owner_gid()
        or launcher_metadata.get("mode") != "0755"
    ):
        raise ControlPlaneError("fixed launcher metadata is unsafe")
    installed["launcher"] = launcher_metadata
    for name in UNIT_FILES:
        source = release / "deploy" / name
        target = layout.systemd_dir / name
        if _sha256(source) != _sha256(target):
            raise ControlPlaneError("installed systemd unit differs from candidate")
        installed[f"units/{name}"] = _metadata(target)
    _verify_contentcrew_unit(
        release / "deploy" / "contentcrew.service",
        layout.systemd_dir / "contentcrew.service",
    )
    for source, target, key in (
        (
            release / "deploy" / "caddy-paihuo-guard.conf",
            layout.caddy_dropin,
            "caddy/dropin",
        ),
        (release / "deploy" / "Caddyfile", layout.caddyfile, "caddy/Caddyfile"),
    ):
        if _sha256(source) != _sha256(target):
            raise ControlPlaneError("installed Caddy configuration differs from candidate")
        installed[key] = _metadata(target)

    unit_paths = [str(layout.systemd_dir / name) for name in UNIT_FILES]
    _run(runner, ["systemctl", "daemon-reload"])
    _run(runner, ["systemd-analyze", "verify", *unit_paths])
    _run(runner, [str(layout.caddy_bin), "validate", "--config", str(layout.caddyfile)])
    output = _systemd_property(runner, "FragmentPath")
    if not output:
        raise ControlPlaneError(
            "systemd effective contentcrew FragmentPath was not reported"
        )
    try:
        effective = Path(output).resolve(strict=True)
    except OSError as exc:
        raise ControlPlaneError(
            "systemd effective contentcrew FragmentPath is unsafe"
        ) from exc
    if effective != (
        layout.systemd_dir / "contentcrew.service"
    ).resolve(strict=True):
        raise ControlPlaneError(
            "systemd effective contentcrew FragmentPath is unexpected"
        )
    dropins = _systemd_property(runner, "DropInPaths")
    if dropins:
        raise ControlPlaneError(
            "contentcrew has unexpected effective systemd drop-ins"
        )
    environment_files = _systemd_property(runner, "EnvironmentFiles")
    if environment_files != "/etc/paihuo/paihuo.env (ignore_errors=no)":
        raise ControlPlaneError(
            "contentcrew effective EnvironmentFile is unexpected"
        )
    environment = _systemd_property(runner, "Environment")
    try:
        environment_tokens = set(shlex.split(environment))
    except ValueError as exc:
        raise ControlPlaneError(
            "contentcrew effective Environment is malformed"
        ) from exc
    for required in (
        "CONTENTCREW_REQUIRE_SESSION_SECRET=1",
        "CONTENTCREW_REQUIRE_CONFIG_KEY=1",
    ):
        if required not in environment_tokens:
            raise ControlPlaneError(
                "contentcrew effective mandatory secret flags are missing"
            )
    exec_start_pre = _systemd_property(runner, "ExecStartPre")
    secret_gate = (
        "/usr/bin/env PYTHONPATH=/usr/local/lib/paihuo-ops "
        "/usr/bin/python3 -m deploy.session_secret_env --check "
        "--path /etc/paihuo/paihuo.env"
    )
    if exec_start_pre.count(secret_gate) != 1:
        raise ControlPlaneError(
            "contentcrew effective fixed secret ExecStartPre is missing/ambiguous"
        )
    caddy_dropins = _systemd_property(
        runner, "DropInPaths", unit="caddy.service"
    )
    try:
        caddy_dropin_paths = set(shlex.split(caddy_dropins))
    except ValueError as exc:
        raise ControlPlaneError("Caddy effective drop-ins are malformed") from exc
    if caddy_dropin_paths != {str(layout.caddy_dropin)}:
        raise ControlPlaneError("Caddy effective drop-in differs from candidate")
    caddy_exec_start_pre = _systemd_property(
        runner, "ExecStartPre", unit="caddy.service"
    )
    caddy_guard = (
        "/usr/bin/python3 /usr/local/lib/paihuo-ops/deploy/start_guard.py "
        "--mode proxy --control-dir /var/lib/paihuo-upgrade "
        "--permit /run/paihuo-upgrade/start.permit"
    )
    if caddy_exec_start_pre.count(caddy_guard) != 1:
        raise ControlPlaneError(
            "Caddy effective fixed proxy start guard is missing/ambiguous"
        )
    caddy_groups = _systemd_property(
        runner, "SupplementaryGroups", unit="caddy.service"
    )
    try:
        group_tokens = set(shlex.split(caddy_groups))
    except ValueError as exc:
        raise ControlPlaneError(
            "Caddy effective supplementary groups are malformed"
        ) from exc
    if "paihuo-public" not in group_tokens:
        raise ControlPlaneError(
            "Caddy effective paihuo-public supplementary group is missing"
        )
    return {
        "digest": _tree_digest(installed),
        "objects": installed,
        "validation": {
            "caddy_validate": True,
            "contentcrew_fragment": str(layout.systemd_dir / "contentcrew.service"),
            "systemd_analyze_verify": True,
        },
    }


def _materialize_ops(release: Path, versioned: Path) -> None:
    if versioned.exists() or versioned.is_symlink():
        versioned_info = versioned.lstat()
        if (
            versioned.is_symlink()
            or not stat.S_ISDIR(versioned_info.st_mode)
            or versioned_info.st_uid != _owner_uid()
            or versioned_info.st_gid != _owner_gid()
            or stat.S_IMODE(versioned_info.st_mode) != 0o755
        ):
            raise ControlPlaneError("existing versioned ops root is unsafe")
        for relative in OPS_FILES:
            target = versioned / PurePosixPath(relative)
            info = target.lstat()
            expected_mode = (
                0o755 if relative == "deploy/upgrade.sh" else 0o644
            )
            if (
                _sha256(release / PurePosixPath(relative)) != _sha256(target)
                or target.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != _owner_uid()
                or info.st_gid != _owner_gid()
                or stat.S_IMODE(info.st_mode) != expected_mode
                or info.st_nlink != 1
            ):
                raise ControlPlaneError("existing versioned ops do not match candidate")
        return
    staged = versioned.parent / f".{versioned.name}.next-{uuid.uuid4().hex}"
    try:
        staged.mkdir(mode=0o700)
        os.chown(staged, _owner_uid(), _owner_gid())
        for relative in OPS_FILES:
            source = release / PurePosixPath(relative)
            target = staged / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            _atomic_install(
                source,
                target,
                0o755 if relative == "deploy/upgrade.sh" else 0o644,
            )
        os.chmod(staged, 0o755)
        os.chown(staged, _owner_uid(), _owner_gid())
        os.replace(staged, versioned)
        _fsync_dir(versioned.parent)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def prepare_control_plane(
    *,
    release: str | os.PathLike[str],
    control_dir: str | os.PathLike[str],
    layout: Layout = Layout(),
    runner: Callable[[Sequence[str]], Any] = _default_runner,
) -> dict[str, Any]:
    release_path = Path(release).resolve(strict=True)
    if release_path.is_symlink() or not release_path.is_dir():
        raise ControlPlaneError("candidate release is unsafe")
    release_id = release_path.name
    if not RELEASE_ID_RE.fullmatch(release_id):
        raise ControlPlaneError("unsafe release id")
    records = _candidate_records(release_path)
    candidate_digest = _tree_digest(records)
    root = _assert_root_only_dir(Path(control_dir).absolute(), create=True)
    snapshots = _assert_root_only_dir(root / "control-plane", create=True)
    snapshot = snapshots / release_id
    report = _snapshot_control_plane(
        release=release_path,
        release_id=release_id,
        snapshot=snapshot,
        candidate_digest=candidate_digest,
        layout=layout,
        runner=runner,
    )
    versioned = layout.ops_link.parent / f"{layout.ops_link.name}-{release_id}"
    try:
        _materialize_ops(release_path, versioned)
        _atomic_link(versioned, layout.ops_link)
        _atomic_install(
            release_path / "deploy" / "upgrade.sh",
            layout.launcher,
            0o755,
        )
        for name in UNIT_FILES:
            _atomic_install(
                release_path / "deploy" / name,
                layout.systemd_dir / name,
                0o644,
            )
        _atomic_install(
            release_path / "deploy" / "caddy-paihuo-guard.conf",
            layout.caddy_dropin,
            0o644,
        )
        _atomic_install(
            release_path / "deploy" / "Caddyfile",
            layout.caddyfile,
            0o644,
        )
        installed = _verify_installed(
            release=release_path,
            versioned_ops=versioned,
            layout=layout,
            runner=runner,
        )
        installed_at = (
            report.get("installed_at_utc")
            if report.get("status") == "installed"
            else None
        ) or datetime.now(tz=UTC).isoformat()
        report.update(
            {
                "installed": installed,
                "installed_at_utc": installed_at,
                "status": "installed",
                "versioned_ops": str(versioned),
            }
        )
        _atomic_json(snapshot / "attestation.json", report)
    except BaseException:
        try:
            unsafe_receipt = False
            for receipt_path in sorted(root.glob("upgrade-*.json")):
                receipt = _private_upgrade_receipt(receipt_path)
                if (
                    Path(str(receipt.get("release") or "")).absolute()
                    == release_path.absolute()
                    and str(receipt.get("status") or "")
                    not in TERMINAL_UPGRADE_STATUSES
                ):
                    unsafe_receipt = True
                    break
            if unsafe_receipt:
                reconcile_failed_upgrade(
                    control_dir=control_dir,
                    release_id=release_id,
                    layout=layout,
                    runner=runner,
                )
            else:
                restore_control_plane(
                    control_dir=control_dir,
                    release_id=release_id,
                    layout=layout,
                    runner=runner,
                )
        except BaseException as restore_error:
            raise ControlPlaneError(
                "control-plane install/attestation failed and restoration "
                "was not proven"
            ) from restore_error
        raise
    return {
        "attestation_path": str(snapshot / "attestation.json"),
        "attestation_sha256": attestation_digest(snapshot / "attestation.json"),
        "candidate_digest": candidate_digest,
        "status": "installed",
    }


def verify_installed_attestation(
    path: str | os.PathLike[str],
    *,
    layout: Layout = Layout(),
    runner: Callable[[Sequence[str]], Any] = _default_runner,
) -> dict[str, Any]:
    attestation = Path(path).absolute()
    report = _read_attestation(attestation)
    if report.get("status") != "installed":
        raise ControlPlaneError("control plane is not attested as installed")
    release = Path(str(report.get("release") or "")).resolve(strict=True)
    if release.name != report.get("release_id"):
        raise ControlPlaneError("control-plane release identity mismatch")
    candidate_digest = _tree_digest(_candidate_records(release))
    if candidate_digest != report.get("candidate_digest"):
        raise ControlPlaneError("candidate control-plane digest changed")
    versioned = Path(str(report.get("versioned_ops") or "")).resolve(strict=True)
    installed = _verify_installed(
        release=release,
        versioned_ops=versioned,
        layout=layout,
        runner=runner,
    )
    expected_installed = report.get("installed")
    if (
        not isinstance(expected_installed, dict)
        or installed["digest"] != expected_installed.get("digest")
    ):
        raise ControlPlaneError("effective control plane differs from attestation")
    return {
        "attestation_path": str(attestation),
        "attestation_sha256": attestation_digest(attestation),
        "candidate_digest": candidate_digest,
        "effective_digest": installed["digest"],
        "status": "installed",
    }


def _restore_record(snapshot: Path, record: dict[str, Any]) -> None:
    destination = Path(str(record["path"]))
    kind = record.get("type")
    if kind == "missing":
        if destination.is_dir() and not destination.is_symlink():
            raise ControlPlaneError("refusing to remove unexpected directory")
        try:
            destination.unlink()
            _fsync_dir(destination.parent)
        except FileNotFoundError:
            pass
        return
    if kind != "file":
        raise ControlPlaneError("snapshot object has unsupported restore type")
    source = snapshot / str(record["snapshot"])
    if _sha256(source) != record.get("sha256"):
        raise ControlPlaneError("snapshot object digest mismatch")
    _atomic_install(source, destination, int(str(record["mode"]), 8))
    os.chown(destination, int(record["uid"]), int(record["gid"]))


def _verify_restored(snapshot: Path, records: dict[str, dict[str, Any]], layout: Layout) -> None:
    link = records["ops_link"]
    if _metadata(layout.ops_link) != {
        key: link[key] for key in ("gid", "mode", "target", "type", "uid")
    }:
        raise ControlPlaneError("fixed ops link restoration mismatch")
    for key, record in records.items():
        if key == "ops_link":
            continue
        actual = _metadata(Path(str(record["path"])))
        expected = {
            name: record[name]
            for name in ("gid", "mode", "sha256", "type", "uid")
            if name in record
        }
        if actual != expected:
            raise ControlPlaneError("restored control-plane object mismatch")


def _private_upgrade_receipt(path: Path) -> dict[str, Any]:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != _owner_uid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ControlPlaneError("upgrade receipt is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ControlPlaneError("upgrade receipt is invalid")
    return value


def _private_upgrade_status(path: Path) -> str:
    return str(_private_upgrade_receipt(path).get("status") or "")


def gate_existing_upgrade(
    *,
    control_dir: str | os.PathLike[str],
    release: str | os.PathLike[str],
) -> dict[str, Any]:
    root = _assert_root_only_dir(Path(control_dir).absolute())
    release_path = Path(release).resolve(strict=True)
    transaction = root / "wrapper-transaction.json"
    resumable = False
    if transaction.exists() or transaction.is_symlink():
        marker = _private_upgrade_receipt(transaction)
        if marker.get("format") != "paihuo-wrapper-transaction/v1":
            raise ControlPlaneError("wrapper transaction marker is invalid")
        if marker.get("release") != str(release_path):
            raise ControlPlaneError(
                "unfinished wrapper transaction belongs to a different release"
            )
        resumable = True
    snapshot = root / "control-plane" / release_path.name
    if (snapshot.exists() or snapshot.is_symlink()) and not resumable:
        raise ControlPlaneError(
            "release id already has a closed control-plane snapshot"
        )
    incomplete: list[tuple[Path, dict[str, Any]]] = []
    for receipt_path in sorted(root.glob("upgrade-*.json")):
        receipt = _private_upgrade_receipt(receipt_path)
        if str(receipt.get("status") or "") not in TERMINAL_UPGRADE_STATUSES:
            incomplete.append((receipt_path, receipt))
    if len(incomplete) > 1:
        raise ControlPlaneError("multiple incomplete upgrades require manual review")
    if incomplete and Path(
        str(incomplete[0][1].get("release") or "")
    ).absolute() != release_path.absolute():
        raise ControlPlaneError(
            "incomplete upgrade belongs to a different release"
        )
    return {
        "incomplete_count": len(incomplete),
        "same_release": bool(incomplete),
        "status": "ready",
    }


def begin_wrapper_transaction(
    *,
    control_dir: str | os.PathLike[str],
    release: str | os.PathLike[str],
) -> dict[str, Any]:
    root = _assert_root_only_dir(Path(control_dir).absolute())
    release_path = Path(release).resolve(strict=True)
    marker_path = root / "wrapper-transaction.json"
    if marker_path.exists() or marker_path.is_symlink():
        marker = _private_upgrade_receipt(marker_path)
        if (
            marker.get("format") != "paihuo-wrapper-transaction/v1"
            or marker.get("release") != str(release_path)
        ):
            raise ControlPlaneError("another wrapper transaction is unfinished")
        return {"status": "resumed", "transaction_path": str(marker_path)}
    _atomic_json(
        marker_path,
        {
            "format": "paihuo-wrapper-transaction/v1",
            "release": str(release_path),
            "release_id": release_path.name,
            "started_at_utc": datetime.now(tz=UTC).isoformat(),
            "status": "active",
        },
    )
    return {"status": "started", "transaction_path": str(marker_path)}


def finish_wrapper_transaction(
    *,
    control_dir: str | os.PathLike[str],
    release_id: str,
) -> dict[str, Any]:
    if not RELEASE_ID_RE.fullmatch(release_id):
        raise ControlPlaneError("unsafe release id")
    root = _assert_root_only_dir(Path(control_dir).absolute())
    marker_path = root / "wrapper-transaction.json"
    marker = _private_upgrade_receipt(marker_path)
    if (
        marker.get("format") != "paihuo-wrapper-transaction/v1"
        or marker.get("release_id") != release_id
    ):
        raise ControlPlaneError("wrapper transaction marker mismatch")
    marker_release = Path(str(marker.get("release") or "")).absolute()
    for receipt_path in sorted(root.glob("upgrade-*.json")):
        receipt = _private_upgrade_receipt(receipt_path)
        if (
            Path(str(receipt.get("release") or "")).absolute()
            == marker_release
            and str(receipt.get("status") or "")
            not in TERMINAL_UPGRADE_STATUSES
        ):
            return {
                "status": "retained_incomplete",
                "transaction_path": str(marker_path),
            }
    marker_path.unlink()
    _fsync_dir(root)
    return {"status": "finished"}


@contextmanager
def _restoration_start_permit(
    control_root: Path, permit: Path
) -> Iterable[None]:
    incomplete = []
    for receipt in sorted(control_root.glob("upgrade-*.json")):
        if _private_upgrade_status(receipt) not in TERMINAL_UPGRADE_STATUSES:
            incomplete.append(receipt)
    if len(incomplete) > 1:
        raise ControlPlaneError("multiple incomplete upgrades block restoration")
    if not incomplete:
        yield
        return
    permit.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(permit.parent, 0o700)
    os.chown(permit.parent, _owner_uid(), _owner_gid())
    _atomic_json(
        permit,
        {
            "issued_at_utc": datetime.now(tz=UTC).isoformat(),
            "nonce": uuid.uuid4().hex,
            "receipt_path": str(incomplete[0]),
        },
    )
    try:
        yield
    finally:
        try:
            permit.unlink()
            _fsync_dir(permit.parent)
        except FileNotFoundError:
            pass


def _restore_runtime_state(
    *,
    runtime: dict[str, Any],
    layout: Layout,
    runner: Callable[[Sequence[str]], Any],
) -> None:
    for unit in RUNTIME_UNITS:
        expected = runtime.get(unit)
        if not isinstance(expected, dict):
            raise ControlPlaneError("snapshot runtime state is incomplete")
        if bool(expected.get("active")):
            if unit == "contentcrew.service":
                _run(runner, ["systemctl", "restart", unit])
                _run(runner, ["systemctl", "is-active", "--quiet", unit])
                _run(
                    runner,
                    [
                        "/usr/bin/curl",
                        "-fsS",
                        "--max-time",
                        "10",
                        "http://127.0.0.1:8899/healthz",
                    ],
                )
            elif unit.endswith(".timer"):
                _run(runner, ["systemctl", "restart", unit])
                _run(runner, ["systemctl", "is-active", "--quiet", unit])
            else:
                _run(runner, ["systemctl", "start", unit])
        else:
            _run(runner, ["systemctl", "stop", unit])
        enabled = _systemd_boolean_state(
            runner, unit=unit, state="enabled"
        )
        if enabled != bool(expected.get("enabled")):
            raise ControlPlaneError("restored systemd enabled state differs")
    caddy_runtime = runtime.get("caddy.service")
    if not isinstance(caddy_runtime, dict):
        raise ControlPlaneError("snapshot lacks Caddy runtime state")
    if bool(caddy_runtime.get("active")):
        _run(runner, ["systemctl", "restart", "caddy.service"])
        _run(runner, ["systemctl", "is-active", "--quiet", "caddy.service"])
        _run(
            runner,
            [
                "/usr/bin/curl",
                "-fsS",
                "--max-time",
                "15",
                "--resolve",
                "paihuo.ai:443:127.0.0.1",
                "https://paihuo.ai/healthz",
            ],
        )
    else:
        _run(runner, ["systemctl", "stop", "caddy.service"])
    caddy_enabled = _systemd_boolean_state(
        runner, unit="caddy.service", state="enabled"
    )
    if caddy_enabled != bool(caddy_runtime.get("enabled")):
        raise ControlPlaneError("restored Caddy enabled state differs")


def restore_control_plane(
    *,
    control_dir: str | os.PathLike[str],
    release_id: str,
    layout: Layout = Layout(),
    runner: Callable[[Sequence[str]], Any] = _default_runner,
) -> dict[str, Any]:
    if not RELEASE_ID_RE.fullmatch(release_id):
        raise ControlPlaneError("unsafe release id")
    root = _assert_root_only_dir(Path(control_dir).absolute())
    snapshot = root / "control-plane" / release_id
    _assert_root_only_dir(snapshot)
    report = _read_attestation(snapshot / "attestation.json")
    if report.get("release_id") != release_id:
        raise ControlPlaneError("snapshot release id mismatch")
    records = report.get("original")
    if not isinstance(records, dict) or _tree_digest(records) != report.get(
        "snapshot_digest"
    ):
        raise ControlPlaneError("control-plane snapshot manifest mismatch")
    link = records.get("ops_link")
    if not isinstance(link, dict) or link.get("type") != "symlink":
        raise ControlPlaneError("snapshot lacks original fixed ops link")
    for key, record in records.items():
        if key == "ops_link":
            continue
        if not isinstance(record, dict):
            raise ControlPlaneError("invalid snapshot object")
        _restore_record(snapshot, record)
    _atomic_link(Path(str(link["target"])), layout.ops_link)
    _run(runner, ["systemctl", "daemon-reload"])
    _run(
        runner,
        [
            "systemd-analyze",
            "verify",
            *[str(layout.systemd_dir / name) for name in UNIT_FILES],
        ],
    )
    _run(runner, [str(layout.caddy_bin), "validate", "--config", str(layout.caddyfile)])
    _verify_restored(snapshot, records, layout)
    runtime = report.get("runtime")
    if not isinstance(runtime, dict):
        raise ControlPlaneError("snapshot lacks original runtime state")
    with _restoration_start_permit(root, layout.start_permit):
        _restore_runtime_state(runtime=runtime, layout=layout, runner=runner)
    report.update(
        {
            "restored_at_utc": datetime.now(tz=UTC).isoformat(),
            "runtime_restored": True,
            "status": "restored",
        }
    )
    _atomic_json(snapshot / "attestation.json", report)
    return {
        "attestation_path": str(snapshot / "attestation.json"),
        "attestation_sha256": attestation_digest(snapshot / "attestation.json"),
        "status": "restored",
    }


def reconcile_failed_upgrade(
    *,
    control_dir: str | os.PathLike[str],
    release_id: str,
    layout: Layout = Layout(),
    runner: Callable[[Sequence[str]], Any] = _default_runner,
) -> dict[str, Any]:
    if not RELEASE_ID_RE.fullmatch(release_id):
        raise ControlPlaneError("unsafe release id")
    root = _assert_root_only_dir(Path(control_dir).absolute())
    attestation = root / "control-plane" / release_id / "attestation.json"
    control_report = _read_attestation(attestation)
    release = Path(str(control_report.get("release") or "")).resolve(strict=True)
    installed_at: datetime | None = None
    if control_report.get("installed_at_utc"):
        try:
            installed_at = datetime.fromisoformat(
                str(control_report["installed_at_utc"]).replace("Z", "+00:00")
            ).astimezone(UTC)
        except ValueError as exc:
            raise ControlPlaneError(
                "control-plane installation timestamp is invalid"
            ) from exc
    relevant: list[dict[str, Any]] = []
    for receipt_path in sorted(root.glob("upgrade-*.json")):
        receipt = _private_upgrade_receipt(receipt_path)
        supplied = Path(str(receipt.get("release") or "")).absolute()
        status = str(receipt.get("status") or "")
        if (
            supplied == release.absolute()
            and status not in TERMINAL_UPGRADE_STATUSES
        ):
            relevant.append(receipt)
            continue
        try:
            created = datetime.fromisoformat(
                str(receipt["created_at_utc"]).replace("Z", "+00:00")
            ).astimezone(UTC)
        except (KeyError, ValueError):
            continue
        if supplied == release.absolute() and (
            installed_at is not None and created >= installed_at
        ):
            relevant.append(receipt)
    safe_restore_statuses = TERMINAL_UPGRADE_STATUSES - {"succeeded"}
    unsafe_incomplete = [
        receipt
        for receipt in relevant
        if str(receipt.get("status") or "") not in safe_restore_statuses
        and str(receipt.get("status") or "") != "succeeded"
    ]
    committed = [
        receipt for receipt in unsafe_incomplete
        if str(receipt.get("status") or "").startswith("cutover_committed")
    ]
    current = Path("/srv/paihuo/current")
    succeeded_selected = any(
        receipt.get("status") == "succeeded"
        and receipt.get("phase") == "complete"
        for receipt in relevant
    ) and current.is_symlink() and current.resolve(strict=True) == release
    if unsafe_incomplete or succeeded_selected:
        if unsafe_incomplete:
            for unit in ("caddy.service", "contentcrew.service"):
                _run(runner, ["systemctl", "stop", unit])
                active_rc, _ = _probe(
                    runner, ["systemctl", "is-active", "--quiet", unit]
                )
                if active_rc != 3:
                    raise ControlPlaneError(
                        "incomplete upgrade containment was not proven"
                    )
            verified = verify_installed_attestation(
                attestation, layout=layout, runner=runner
            )
            if committed:
                status = "retained_committed_failure"
            else:
                status = "retained_incomplete"
        else:
            verified = verify_installed_attestation(
                attestation, layout=layout, runner=runner
            )
            status = "retained_succeeded"
        return {
            "attestation_path": str(attestation),
            "attestation_sha256": verified["attestation_sha256"],
            "status": status,
        }
    return restore_control_plane(
        control_dir=root,
        release_id=release_id,
        layout=layout,
        runner=runner,
    )


def attest_release_evidence(
    *,
    release: str | os.PathLike[str],
    state: str | os.PathLike[str],
    control_dir: str | os.PathLike[str],
    archive: str | os.PathLike[str],
    receipt: str | os.PathLike[str],
    artifact_attestation: str | os.PathLike[str],
    materialized_attestation: str | os.PathLike[str],
) -> dict[str, Any]:
    """Recheck retained build evidence and attest the exact runtime tree.

    The archive is extracted by the standalone verifier before this wrapper is
    ever entered.  This fixed-ops check never extracts again.  On a resumed
    wrapper transaction an existing materialized attestation is revalidated
    against both the live tree and artifact proof instead of being replaced.
    """
    from deploy.verify_release import (
        ReleaseVerifyError,
        verify_materialized_attestation,
        verify_materialized_release,
        verify_release_artifact,
    )

    root = _assert_root_only_dir(Path(control_dir).absolute())
    release_path = Path(release).resolve(strict=True)
    state_path = Path(state).resolve(strict=True)
    release_id = release_path.name
    if not RELEASE_ID_RE.fullmatch(release_id):
        raise ControlPlaneError("release id is unsafe")
    incoming = _assert_root_only_dir(root / "incoming")
    evidence_dir = _assert_root_only_dir(incoming / release_id)
    expected = {
        "archive": evidence_dir / f"{release_id}.tar.gz",
        "receipt": evidence_dir / f"{release_id}.receipt.json",
        "artifact_attestation": (
            evidence_dir / "release-artifact-attestation.json"
        ),
        "materialized_attestation": (
            evidence_dir / "materialized-release-attestation.json"
        ),
    }
    supplied = {
        "archive": Path(archive).absolute(),
        "receipt": Path(receipt).absolute(),
        "artifact_attestation": Path(artifact_attestation).absolute(),
        "materialized_attestation": Path(materialized_attestation).absolute(),
    }
    if supplied != expected:
        raise ControlPlaneError("release evidence paths are not fixed")
    try:
        artifact = verify_release_artifact(
            archive=supplied["archive"],
            receipt=supplied["receipt"],
            artifact_attestation=supplied["artifact_attestation"],
        )
        materialized_path = supplied["materialized_attestation"]
        if materialized_path.exists() or materialized_path.is_symlink():
            materialized = verify_materialized_attestation(
                release_dir=release_path,
                state_dir=state_path,
                artifact_attestation=supplied["artifact_attestation"],
                attestation=materialized_path,
            )
            status = "verified_existing"
        else:
            verify_materialized_release(
                release_dir=release_path,
                expected_data_target=state_path,
                artifact_attestation=supplied["artifact_attestation"],
                attestation=materialized_path,
            )
            materialized = verify_materialized_attestation(
                release_dir=release_path,
                state_dir=state_path,
                artifact_attestation=supplied["artifact_attestation"],
                attestation=materialized_path,
            )
            status = "created"
    except (OSError, ReleaseVerifyError, ValueError) as exc:
        raise ControlPlaneError("release artifact evidence is invalid") from exc
    for key in (
        "manifest_sha256",
        "payload_tree_sha256",
        "release_id",
        "schema_version",
        "source_tree_sha256",
    ):
        if materialized.get(key) != artifact.get(key):
            raise ControlPlaneError("release evidence identity is inconsistent")
    if artifact.get("release_id") != release_id:
        raise ControlPlaneError("release evidence belongs to another release")
    return {
        "archive_sha256": str(artifact["archive_sha256"]),
        "artifact_attestation_sha256": _sha256(
            supplied["artifact_attestation"]
        ),
        "materialized_attestation_sha256": _sha256(
            supplied["materialized_attestation"]
        ),
        "payload_tree_sha256": str(artifact["payload_tree_sha256"]),
        "release_id": release_id,
        "schema_version": int(artifact["schema_version"]),
        "source_tree_sha256": str(artifact["source_tree_sha256"]),
        "status": status,
    }


def verify_bootstrap_runtime(
    *,
    release_id: str,
    ops_root: str | os.PathLike[str],
    launcher: str | os.PathLike[str],
) -> dict[str, Any]:
    """Prove the interpreter is loading only the root-owned launcher bundle."""
    if not RELEASE_ID_RE.fullmatch(release_id):
        raise ControlPlaneError("release id is unsafe")
    supplied_root = Path(ops_root).absolute()
    if supplied_root.is_symlink():
        raise ControlPlaneError("trusted ops root must be a real directory")
    root = supplied_root.resolve(strict=True)
    root_info = root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != _owner_uid()
        or root_info.st_gid != _owner_gid()
        or stat.S_IMODE(root_info.st_mode) not in {0o700, 0o755}
        or stat.S_IMODE(root_info.st_mode) & 0o022
    ):
        raise ControlPlaneError("trusted ops root metadata is unsafe")
    records: dict[str, dict[str, Any]] = {}
    for relative in OPS_FILES:
        path = root / PurePosixPath(relative)
        info = path.lstat()
        expected_mode = 0o755 if relative == "deploy/upgrade.sh" else 0o644
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != _owner_uid()
            or info.st_gid != _owner_gid()
            or stat.S_IMODE(info.st_mode) != expected_mode
            or info.st_nlink != 1
        ):
            raise ControlPlaneError("trusted ops file metadata is unsafe")
        records[relative] = _metadata(path)
    loaded = Path(__file__).resolve(strict=True)
    if loaded != (root / "deploy" / "control_plane.py").resolve(strict=True):
        raise ControlPlaneError("control plane was not loaded from trusted ops")
    launcher_path = Path(launcher).absolute()
    launcher_info = launcher_path.lstat()
    if (
        launcher_path.is_symlink()
        or not stat.S_ISREG(launcher_info.st_mode)
        or launcher_info.st_uid != _owner_uid()
        or launcher_info.st_gid != _owner_gid()
        or stat.S_IMODE(launcher_info.st_mode) != 0o755
        or launcher_info.st_nlink != 1
        or _sha256(launcher_path)
        != _sha256(root / "deploy" / "upgrade.sh")
    ):
        raise ControlPlaneError("fixed launcher is not bound to trusted ops")
    return {
        "launcher_sha256": _sha256(launcher_path),
        "ops_digest": _tree_digest(records),
        "release_id": release_id,
        "status": "trusted",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Snapshot, install or restore Paihuo's fixed control plane."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--release", required=True)
    prepare.add_argument("--control-dir", default="/var/lib/paihuo-upgrade")
    gate = subparsers.add_parser("gate")
    gate.add_argument("--release", required=True)
    gate.add_argument("--control-dir", default="/var/lib/paihuo-upgrade")
    begin = subparsers.add_parser("begin")
    begin.add_argument("--release", required=True)
    begin.add_argument("--control-dir", default="/var/lib/paihuo-upgrade")
    finish = subparsers.add_parser("finish")
    finish.add_argument("--release-id", required=True)
    finish.add_argument("--control-dir", default="/var/lib/paihuo-upgrade")
    restore = subparsers.add_parser("restore")
    restore.add_argument("--release-id", required=True)
    restore.add_argument("--control-dir", default="/var/lib/paihuo-upgrade")
    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--release-id", required=True)
    reconcile.add_argument("--control-dir", default="/var/lib/paihuo-upgrade")
    evidence = subparsers.add_parser("release-evidence")
    evidence.add_argument("--release", required=True)
    evidence.add_argument("--state", required=True)
    evidence.add_argument("--control-dir", default="/var/lib/paihuo-upgrade")
    evidence.add_argument("--archive", required=True)
    evidence.add_argument("--receipt", required=True)
    evidence.add_argument("--artifact-attestation", required=True)
    evidence.add_argument("--materialized-attestation", required=True)
    bootstrap = subparsers.add_parser("bootstrap-check")
    bootstrap.add_argument("--release-id", required=True)
    bootstrap.add_argument("--ops-root", required=True)
    bootstrap.add_argument("--launcher", required=True)
    digest = subparsers.add_parser("digest")
    digest.add_argument("--attestation", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.geteuid() != 0:
        print("control-plane operation failed: command must run as root", file=sys.stderr)
        return 1
    try:
        if args.action == "prepare":
            report = prepare_control_plane(
                release=args.release, control_dir=args.control_dir
            )
        elif args.action == "gate":
            report = gate_existing_upgrade(
                release=args.release, control_dir=args.control_dir
            )
        elif args.action == "begin":
            report = begin_wrapper_transaction(
                release=args.release, control_dir=args.control_dir
            )
        elif args.action == "finish":
            report = finish_wrapper_transaction(
                control_dir=args.control_dir, release_id=args.release_id
            )
        elif args.action == "restore":
            report = restore_control_plane(
                control_dir=args.control_dir, release_id=args.release_id
            )
        elif args.action == "reconcile":
            report = reconcile_failed_upgrade(
                control_dir=args.control_dir, release_id=args.release_id
            )
        elif args.action == "release-evidence":
            report = attest_release_evidence(
                release=args.release,
                state=args.state,
                control_dir=args.control_dir,
                archive=args.archive,
                receipt=args.receipt,
                artifact_attestation=args.artifact_attestation,
                materialized_attestation=args.materialized_attestation,
            )
        elif args.action == "bootstrap-check":
            report = verify_bootstrap_runtime(
                release_id=args.release_id,
                ops_root=args.ops_root,
                launcher=args.launcher,
            )
        else:
            report = {
                "attestation_sha256": attestation_digest(args.attestation),
                "status": "verified",
            }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (ControlPlaneError, OSError, ValueError) as exc:
        print(
            f"control-plane operation failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
