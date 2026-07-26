#!/usr/bin/env python3
"""Rotate the production boss password without printing or storing it in SQLite.

The one-time credential is written to a caller-selected root-only file.  The
password hash participates in every session signature, so changing it
invalidates this root account's previous browser sessions immediately.  Any
legacy database-backed global session secret is deleted, never regenerated.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import sys


PBKDF2_ITERS = 200_000


def _fsync_directory(path: str) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ITERS
    )
    return f"pbkdf2:{PBKDF2_ITERS}:{salt}:{digest.hex()}"


def rotate(database: str, output: str, username: str = "boss") -> dict:
    database = os.path.realpath(database)
    output = os.path.abspath(output)
    parent = os.path.dirname(output)
    if not os.path.isfile(database):
        raise RuntimeError("database does not exist")
    if not os.path.isdir(parent):
        raise RuntimeError("credential directory does not exist")

    password = secrets.token_urlsafe(24)
    created = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    body = (
        f"username={username}\n"
        f"password={password}\n"
        f"created_utc={created}\n"
        "notice=首次登录后请在账号设置中再次修改并妥善保管\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(output, flags, 0o600)
    committed = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        # 凭据必须先在目录项层面持久化，才允许提交对应的新密码。
        _fsync_directory(parent)

        connection = sqlite3.connect(database, timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, role FROM users WHERE username=? AND enabled=1",
                (username,),
            ).fetchone()
            if not row or row[1] != "root":
                raise RuntimeError("enabled root account was not found")
            connection.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (_hash_password(password), row[0]),
            )
            connection.execute(
                "DELETE FROM app_setting "
                "WHERE key IN ('session_secret','bootstrap_pw')"
            )
            connection.commit()
            committed = True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    finally:
        if not committed:
            try:
                os.unlink(output)
                _fsync_directory(parent)
            except FileNotFoundError:
                pass

    return {
        "status": "rotated",
        "username": username,
        "credential_path": output,
        "sessions_invalidated": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rotate the Paihuo root account and write a root-only handoff file."
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--username", default="boss")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        result = rotate(**vars(_parser().parse_args(argv)))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": type(exc).__name__},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
