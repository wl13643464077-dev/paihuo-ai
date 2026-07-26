#!/usr/bin/env python3
"""Boot/start guard for crash-safe Paihuo cutovers.

This helper is installed in the fixed root-owned ops directory. It prevents
systemd from starting an unverified candidate (and Caddy from exposing it)
after a reboot clears the transient systemd manager environment.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys


UTC = timezone.utc
SAFE_TERMINAL = {
    "succeeded",
    "rolled_back",
    "recovered_rollback",
    "failed_before_cutover",
    "failed_before_cutover_recovered",
    "checkpoint_ready",
}
PROXY_COMMITTED = {
    "cutover_committed",
    "cutover_committed_proxy_failed",
    "cutover_committed_activation_failed",
    "rollback_committed",
    "rollback_committed_activation_failed",
}


class GuardError(RuntimeError):
    pass


def _owner_uid() -> int:
    return 0 if os.geteuid() == 0 else os.geteuid()


def _private_json(path: Path) -> dict:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != _owner_uid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise GuardError(f"unsafe control file: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise GuardError(f"invalid control JSON: {path}")
    return value


def check_start(
    *,
    control_dir: str | os.PathLike[str],
    permit: str | os.PathLike[str],
    mode: str,
    now: datetime | None = None,
) -> dict:
    control = Path(control_dir)
    metadata = control.lstat()
    if (
        control.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != _owner_uid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise GuardError("upgrade control directory is not root-owned 0700")
    incomplete: list[tuple[Path, dict]] = []
    for receipt_path in sorted(control.glob("upgrade-*.json")):
        receipt = _private_json(receipt_path)
        if receipt.get("status") not in SAFE_TERMINAL:
            incomplete.append((receipt_path, receipt))
    if not incomplete:
        return {"ok": True, "reason": "no_incomplete_upgrade"}
    if len(incomplete) != 1:
        raise GuardError("multiple incomplete upgrade receipts")
    receipt_path, receipt = incomplete[0]
    status = str(receipt.get("status") or "")
    if mode == "backup":
        if status in PROXY_COMMITTED:
            return {"ok": True, "reason": "accepted_cutover_backup"}
        raise GuardError(f"backup blocked by upgrade status {status!r}")
    if mode == "proxy":
        if status not in PROXY_COMMITTED:
            raise GuardError(f"proxy blocked by upgrade status {status!r}")
    if status.startswith("rollback_failed"):
        raise GuardError("application blocked by failed rollback")
    permit_path = Path(permit)
    if not permit_path.exists():
        raise GuardError("application start lacks a transient root permit")
    token = _private_json(permit_path)
    if Path(str(token.get("receipt_path") or "")).absolute() != receipt_path.absolute():
        raise GuardError("start permit does not match the active receipt")
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    try:
        issued = datetime.fromisoformat(
            str(token["issued_at_utc"]).replace("Z", "+00:00")
        ).astimezone(UTC)
    except (KeyError, ValueError) as exc:
        raise GuardError("start permit has an invalid timestamp") from exc
    if current - issued > timedelta(minutes=5) or issued - current > timedelta(minutes=1):
        raise GuardError("start permit expired")
    return {"ok": True, "reason": "transient_upgrade_permit"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-dir", default="/var/lib/paihuo-upgrade")
    parser.add_argument(
        "--permit", default="/run/paihuo-upgrade/start.permit"
    )
    parser.add_argument(
        "--mode", choices=("application", "proxy", "backup"), required=True
    )
    args = parser.parse_args()
    try:
        check_start(
            control_dir=args.control_dir,
            permit=args.permit,
            mode=args.mode,
        )
        return 0
    except (GuardError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"paihuo start blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
