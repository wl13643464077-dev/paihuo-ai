"""Read-only production layout and database checks before Paihuo cutover."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from urllib.parse import quote

from app.session_secret import (
    CONFIG_KEY_ENV,
    REQUIRE_CONFIG_KEY_ENV,
    REQUIRE_SESSION_SECRET_ENV,
    SESSION_SECRET_ENV,
    validate_secret_token,
)


class PreflightError(RuntimeError):
    pass


API_TOOL_RUNNER_REQUIRED_ISOLATION_FLAGS = (
    "--safe-mode",
    "--setting-sources",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--no-chrome",
    "--tools",
    "--max-budget-usd",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PreflightError(message)


def _check_secret_environment(variable: str, requirement_variable: str) -> dict:
    """Validate one production secret without returning any secret material."""
    requirement = os.environ.get(requirement_variable)
    _require(
        requirement in (None, "", "0", "1"),
        f"{requirement_variable} must be 0 or 1",
    )
    required = requirement == "1"
    configured = os.environ.get(variable)
    if configured is None:
        _require(
            not required,
            f"{variable} is required for production",
        )
        return {"required": False, "configured": False, "strong": False}
    try:
        validate_secret_token(configured, variable=variable)
    except ValueError as exc:
        # The validator's message names only the variable and violated policy.
        raise PreflightError(str(exc)) from exc
    return {"required": required, "configured": True, "strong": True}


def _check_session_secret_environment() -> dict:
    return _check_secret_environment(
        SESSION_SECRET_ENV, REQUIRE_SESSION_SECRET_ENV
    )


def _check_config_key_environment() -> dict:
    return _check_secret_environment(CONFIG_KEY_ENV, REQUIRE_CONFIG_KEY_ENV)


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _api_tool_runner_required_flags() -> tuple[str, ...]:
    """Derive CLI capabilities from the exact command used by app.llm."""
    try:
        from app.llm import _runner_command

        command = _runner_command(
            "claude",
            model="preflight-model",
            web=True,
            system_prompt="preflight",
        )
    except Exception as exc:
        raise PreflightError(
            "API tool runner command contract could not be loaded"
        ) from exc
    command_flags = {
        token
        for token in command
        if isinstance(token, str) and token.startswith("--")
    }
    missing_isolation = sorted(
        set(API_TOOL_RUNNER_REQUIRED_ISOLATION_FLAGS) - command_flags
    )
    _require(
        not missing_isolation,
        "application tool runner is missing mandatory isolation flags: "
        + ",".join(missing_isolation),
    )
    return tuple(sorted(command_flags))


def _check_api_tool_runner(path: Path) -> dict:
    """Fail closed unless the installed Claude CLI supports the app contract."""
    _require(
        _executable(path),
        f"API tool runner executable missing: {path}",
    )

    probes = {}
    for argument in ("--version", "--help"):
        try:
            result = subprocess.run(
                [str(path), argument],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PreflightError(
                f"API tool runner {argument} check failed"
            ) from exc
        _require(
            result.returncode == 0,
            f"API tool runner {argument} failed",
        )
        probes[argument] = result

    required_flags = _api_tool_runner_required_flags()
    help_text = (
        (probes["--help"].stdout or "")
        + "\n"
        + (probes["--help"].stderr or "")
    )
    available_flags = set(
        re.findall(
            r"(?<![\w-])--[A-Za-z0-9][A-Za-z0-9-]*(?![\w-])",
            help_text,
        )
    )
    missing = sorted(set(required_flags) - available_flags)
    _require(
        not missing,
        "API tool runner missing required capabilities: " + ",".join(missing),
    )
    return {
        "ok": True,
        "required_isolation_flags": list(
            API_TOOL_RUNNER_REQUIRED_ISOLATION_FLAGS
        ),
        "validated_flags": list(required_flags),
    }


def _writable_file(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return (
        path.is_file()
        and bool(mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        and os.access(path, os.R_OK | os.W_OK)
    )


def _read_release_json(path: Path, *, label: str) -> dict:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PreflightError(f"{label} missing: {path}") from exc
    _require(
        stat.S_ISREG(metadata.st_mode) and not path.is_symlink(),
        f"{label} must be a regular file: {path}",
    )
    _require(
        metadata.st_size <= 4 * 1024 * 1024,
        f"{label} exceeds size limit: {path}",
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"{label} is not valid JSON: {path}") from exc
    _require(isinstance(value, dict), f"{label} must be a JSON object: {path}")
    return value


def _probe_directory(path: Path) -> None:
    """以当前（systemd 服务）用户实际创建并删除一个探针文件。"""
    _require(path.is_dir(), f"runtime directory missing: {path}")
    _require(
        os.access(path, os.R_OK | os.W_OK | os.X_OK),
        f"runtime directory is not writable: {path}",
    )
    probe = path / f".paihuo-preflight-{uuid.uuid4().hex}"
    descriptor = None
    try:
        descriptor = os.open(
            probe,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        os.write(descriptor, b"ok")
    except OSError as exc:
        raise PreflightError(
            f"runtime directory write probe failed: {path}: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PreflightError(
                f"runtime directory cleanup probe failed: {path}: {exc}"
            ) from exc


def _check_runtime_tree(path: Path) -> None:
    """旧部署切换非 root 用户时，已有子目录/文件也必须可继续写。"""
    _probe_directory(path)

    def walk_error(exc: OSError):
        raise PreflightError(
            f"runtime tree cannot be traversed: {path}: {exc}"
        ) from exc

    for current, directories, files in os.walk(path, onerror=walk_error):
        current_path = Path(current)
        _require(
            os.access(current_path, os.R_OK | os.W_OK | os.X_OK),
            f"runtime subdirectory is not writable: {current_path}",
        )
        for name in directories:
            child = current_path / name
            _require(
                not child.is_symlink(),
                f"runtime directory must not be a symlink: {child}",
            )
            _require(
                os.access(child, os.R_OK | os.W_OK | os.X_OK),
                f"runtime subdirectory is not writable: {child}",
            )
        for name in files:
            child = current_path / name
            _require(
                not child.is_symlink() and _writable_file(child),
                f"runtime file is not writable by service user: {child}",
            )


def check_layout(
    root: str | os.PathLike[str],
    state: str | os.PathLike[str],
    venv: str | os.PathLike[str],
    *,
    check_runtime: bool = True,
) -> dict:
    """Validate immutable release and writable state using short-lived probes."""
    session_secret_report = _check_session_secret_environment()
    config_key_report = _check_config_key_environment()
    root_path = Path(root).resolve(strict=True)
    state_path = Path(state).resolve(strict=True)
    venv_path = Path(venv).resolve(strict=True)
    data_link = root_path / "data"

    _require(data_link.is_symlink(), f"{data_link} must be a symlink")
    _require(
        data_link.resolve(strict=True) == state_path,
        f"{data_link} must point exactly to {state_path}",
    )
    _probe_directory(state_path)
    public_dir = Path(
        os.environ.get("CONTENTCREW_PUBLIC_DIR", "/srv/paihuo-pub")
    ).resolve(strict=True)
    _probe_directory(public_dir)

    manifest = _read_release_json(
        root_path / "RELEASE-MANIFEST.json",
        label="release manifest",
    )
    _require(
        manifest.get("format") == "paihuo-release-manifest/v1",
        "release manifest format is unsupported",
    )
    config_root = root_path / "config"
    departments = config_root / "departments"
    _require(
        departments.is_dir() and not departments.is_symlink(),
        f"immutable department configuration missing: {departments}",
    )
    department_files = sorted(departments.iterdir())
    _require(
        department_files,
        f"immutable department configuration is empty: {departments}",
    )
    for department_file in department_files:
        _require(
            department_file.suffix == ".json",
            "immutable department configuration contains an unexpected entry",
        )
        department = _read_release_json(
            department_file,
            label="department configuration",
        )
        _require(
            isinstance(department.get("employees"), list),
            f"department configuration employees must be a list: {department_file}",
        )
    gate_seed = _read_release_json(
        config_root / "gate_rules.default.json",
        label="immutable gate seed",
    )
    _require(
        isinstance(gate_seed.get("sensitive_words"), list)
        and all(
            isinstance(word, str) and word.strip()
            for word in gate_seed.get("sensitive_words", [])
        ),
        "immutable gate seed sensitive_words is invalid",
    )

    for required in (
        root_path / "app" / "main.py",
        root_path / "static" / "index.html",
    ):
        _require(required.is_file(), f"required file missing: {required}")
    state_gate = state_path / "gate_rules.json"
    if os.path.lexists(state_gate):
        state_gate_rules = _read_release_json(
            state_gate,
            label="runtime gate rules",
        )
        _require(
            isinstance(state_gate_rules.get("sensitive_words"), list)
            and all(
                isinstance(word, str) and word.strip()
                for word in state_gate_rules.get("sensitive_words", [])
            ),
            "runtime gate rules sensitive_words is invalid",
        )
    _require(
        _executable(root_path / "run.sh"),
        f"service entrypoint is not executable: {root_path / 'run.sh'}",
    )
    for runtime_dir in (state_path / "assets", state_path / "llmwork"):
        _check_runtime_tree(runtime_dir)
    _require(
        _executable(venv_path / "bin" / "python"),
        f"python executable missing: {venv_path / 'bin' / 'python'}",
    )
    _require(
        _executable(venv_path / "bin" / "yt-dlp"),
        f"yt-dlp executable missing: {venv_path / 'bin' / 'yt-dlp'}",
    )
    ffmpeg = Path(
        os.environ.get("CONTENTCREW_FFMPEG_PATH")
        or shutil.which("ffmpeg")
        or "/usr/bin/ffmpeg"
    )
    _require(_executable(ffmpeg), f"ffmpeg executable missing: {ffmpeg}")
    claude = Path(
        os.environ.get("CONTENTCREW_CLAUDE_PATH")
        or "/srv/paihuo/bin/claude"
    )
    api_tool_runner_report = None
    if check_runtime:
        api_tool_runner_report = _check_api_tool_runner(claude)

        browser_root = Path(
            os.environ.get(
                "PLAYWRIGHT_BROWSERS_PATH",
                str(state_path.parent / "ms-playwright"),
            )
        )
        _require(browser_root.is_dir(), f"Playwright browsers missing: {browser_root}")
        chromium = [
            path
            for path in browser_root.glob("chromium-*")
            if any(
                candidate.is_file() and os.access(candidate, os.X_OK)
                for candidate in (
                    path / "chrome-linux" / "chrome",
                    path / "chrome-linux64" / "chrome",
                    path / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
                )
            )
        ]
        _require(chromium, f"Playwright Chromium executable missing: {browser_root}")
        try:
            fonts = subprocess.run(
                ["fc-list", ":lang=zh", "family"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PreflightError(f"Chinese font check failed: {exc}") from exc
        _require(
            fonts.returncode == 0 and fonts.stdout.strip(),
            "no Chinese font is visible to the service user",
        )
        pdf_env = dict(os.environ)
        pdf_env["PYTHONPATH"] = str(root_path)
        try:
            pdf_check = subprocess.run(
                [
                    str(venv_path / "bin" / "python"),
                    "-c",
                    (
                        "from app.export import md_to_pdf;"
                        "data=md_to_pdf('# 发布预检\\n\\n中文导出正常');"
                        "assert data[:4]==b'%PDF' and len(data)>100"
                    ),
                ],
                cwd=root_path,
                env=pdf_env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PreflightError(f"PDF runtime check failed: {exc}") from exc
        _require(
            pdf_check.returncode == 0,
            f"PDF runtime check failed: {pdf_check.stderr[-300:]}",
        )

    database = state_path / "contentcrew.db"
    configured_database = Path(
        os.environ.get("CONTENTCREW_DB_PATH", str(database))
    ).resolve()
    _require(
        configured_database == database,
        "CONTENTCREW_DB_PATH does not match the shared state database",
    )
    _require(database.is_file(), f"database missing: {database}")
    _require(
        _writable_file(database),
        f"database is not writable by service user: {database}",
    )
    try:
        descriptor = os.open(database, os.O_RDWR)
        os.close(descriptor)
    except OSError as exc:
        raise PreflightError(
            f"database read/write open failed: {database}: {exc}"
        ) from exc
    for sidecar in (
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ):
        if sidecar.exists():
            _require(
                _writable_file(sidecar),
                f"SQLite sidecar is not writable by service user: {sidecar}",
            )

    uri = f"file:{quote(str(database))}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            legacy_contract = {
                "tenants": {"id", "name"},
                "users": {
                    "id", "tenant_id", "username", "password_hash", "role"
                },
                "job": {
                    "id", "brief_json", "mode", "status", "current_idx",
                    "created_at", "updated_at",
                },
            }
            for table, required_columns in legacy_contract.items():
                _require(
                    table in tables,
                    f"database missing core table: {table}",
                )
                actual_columns = {
                    str(column[1])
                    for column in connection.execute(
                        f"PRAGMA table_info({table})"
                    )
                }
                missing = sorted(required_columns - actual_columns)
                _require(
                    not missing,
                    f"database core table {table} is missing columns: "
                    + ",".join(missing),
                )
            ledger_version = 0
            if "schema_version" in tables:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version),0) FROM schema_version"
                ).fetchone()
                ledger_version = int((row or [0])[0] or 0)
            else:
                # 通过上面的核心表/列签名才视为可迁移的 legacy v0。
                ledger_version = 0
            user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0] or 0
            )
            schema_version = max(ledger_version, user_version)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PreflightError(f"database read-only check failed: {exc}") from exc
    _require(integrity == "ok", f"database quick_check failed: {integrity}")

    from app.db import LATEST_SCHEMA_VERSION

    _require(
        schema_version <= LATEST_SCHEMA_VERSION,
        f"database schema v{schema_version} is newer than app "
        f"v{LATEST_SCHEMA_VERSION}",
    )
    free_bytes = shutil.disk_usage(state_path).free
    _require(free_bytes >= 1_000_000_000, "less than 1 GB free on state volume")

    return {
        "ok": True,
        "root": str(root_path),
        "state": str(state_path),
        "public_dir": str(public_dir),
        "database": str(database),
        "integrity": integrity,
        "schema_version": schema_version,
        "schema_ledger_version": ledger_version,
        "sqlite_user_version": user_version,
        "app_schema_version": LATEST_SCHEMA_VERSION,
        "department_files": len(department_files),
        "ffmpeg": str(ffmpeg),
        "api_tool_runner": str(claude),
        "api_tool_runner_capabilities": (
            api_tool_runner_report["validated_flags"]
            if api_tool_runner_report else []
        ),
        "session_secret": session_secret_report,
        "config_key": config_key_report,
        "free_bytes": free_bytes,
        "uid": os.geteuid(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Paihuo release/state layout before service start."
    )
    parser.add_argument("--root", default="/srv/paihuo/current")
    parser.add_argument("--state", default="/var/lib/paihuo/data")
    parser.add_argument("--venv", default="/srv/paihuo/current/venv")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        print(
            json.dumps(
                check_layout(args.root, args.state, args.venv),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (PreflightError, OSError, ValueError) as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
