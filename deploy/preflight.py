"""Read-only production layout and database checks before Paihuo cutover."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import unicodedata
import uuid
from urllib.parse import quote

from deploy import verify_release
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
    "--system-prompt-file",
    "--allowedTools",
    "--permission-mode",
    "--tools",
    "--max-budget-usd",
)

API_TOOL_RUNNER_MAX_BUDGET_USD = 10.0


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
            system_prompt_file="/private/preflight-system-prompt",
        )
    except Exception as exc:
        raise PreflightError(
            "API tool runner command contract could not be loaded"
        ) from exc
    _require(
        isinstance(command, (list, tuple))
        and all(isinstance(token, str) for token in command),
        "application tool runner command must be a string argument list",
    )
    dangerous_tokens = [
        token
        for token in command
        if "bypasspermissions" in token.lower()
        or token.lower().startswith("--dangerously-skip-permissions")
    ]
    _require(
        not dangerous_tokens,
        "application tool runner contains a dangerous permission token",
    )
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

    def option_value(flag: str) -> str:
        positions = [
            index for index, token in enumerate(command) if token == flag
        ]
        _require(
            len(positions) == 1,
            f"application tool runner {flag} must appear exactly once",
        )
        position = positions[0]
        _require(
            position + 1 < len(command),
            f"application tool runner {flag} requires a value",
        )
        return command[position + 1]

    expected_values = {
        "--tools": ("WebSearch", "WebSearch"),
        "--allowedTools": ("WebSearch", "WebSearch"),
        "--permission-mode": ("dontAsk", "dontAsk"),
        "--setting-sources": ("", "an empty string"),
    }
    for flag, (expected, description) in expected_values.items():
        _require(
            option_value(flag) == expected,
            f"application tool runner {flag} must be {description}",
        )

    prompt_file = option_value("--system-prompt-file")
    _require(
        bool(prompt_file) and os.path.isabs(prompt_file),
        "application tool runner --system-prompt-file must be an absolute path",
    )
    _require(
        "--system-prompt" not in command,
        "application tool runner must not pass system prompt text in argv",
    )

    budget_text = option_value("--max-budget-usd")
    try:
        budget = float(budget_text)
    except (TypeError, ValueError):
        budget = math.nan
    _require(
        math.isfinite(budget)
        and 0 < budget <= API_TOOL_RUNNER_MAX_BUDGET_USD,
        "application tool runner --max-budget-usd must be a finite number "
        "greater than 0 and at most 10",
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
    # Claude Code 2.1.220 documents paired aliases in compact form, for
    # example ``--system-prompt[-file]``.  The ordinary flag regex above sees
    # only ``--system-prompt`` even though the installed parser accepts the
    # file variant.  Expand only this explicit bracketed CLI-help grammar;
    # never infer arbitrary capabilities from a version number.
    for base, suffix in re.findall(
        r"(?<![\w-])(--[A-Za-z0-9][A-Za-z0-9-]*)\[-([A-Za-z0-9-]+)\]",
        help_text,
    ):
        available_flags.add(f"{base}-{suffix}")
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


_LEARNING_EVIDENCE_SCHEMA = "learning-evidence-gate-v1"
_LEARNING_EVIDENCE_CATALOG_VERSION = "2026.08.v4"
_LEARNING_EVIDENCE_TOP_KEYS = {
    "schema", "catalog_version", "source_catalog_sha256",
    "authority_policy_sha256", "industry_aliases", "employees",
    "authority_registry",
}
_LEARNING_EVIDENCE_EMPLOYEE_KEYS = {
    "employee_key", "industry_key", "source_public_contract_sha256",
    "job_label_en", "topics",
}
_LEARNING_EVIDENCE_TOPIC_KEYS = {
    "topic_id", "canonical_topic", "canonical_topic_sha256", "label_en",
    "object_aliases_en", "method_aliases_en",
}
_LEARNING_EVIDENCE_AUTHORITY_KINDS = {
    "regulator", "standard", "official", "association", "research",
    "industry",
}
_LEARNING_EVIDENCE_GENERIC_ENGLISH = {
    "analysis", "basic", "business", "businesses", "case", "cases",
    "common", "company", "companies", "control", "data", "digital",
    "general", "generic", "information", "industry", "industries", "local",
    "management", "method", "methods", "model", "models", "online",
    "operation", "operations", "platform", "private", "process",
    "processes", "public", "record", "records", "report", "reports",
    "research", "service", "services", "solution", "solutions", "standard",
    "system", "systems", "tool", "tools", "workflow", "workflows",
}
_LEARNING_EVIDENCE_UNSAFE_SUFFIXES = {
    "ac.cn", "co", "co.uk", "com", "com.cn", "edu", "edu.cn", "gov",
    "gov.cn", "int", "mil", "net", "net.cn", "org", "org.cn", "uk",
    "cn",
}
_LEARNING_EVIDENCE_HEX64 = re.compile(r"(?i)[0-9a-f]{64}")
_LEARNING_EVIDENCE_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_LEARNING_EVIDENCE_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


def _learning_evidence_canonical_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _learning_evidence_sha256(value) -> str:
    return hashlib.sha256(_learning_evidence_canonical_json(value)).hexdigest()


def _read_learning_evidence_json(path: Path, *, label: str) -> tuple[dict, bytes]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PreflightError(f"{label} missing: {path}") from exc
    _require(
        stat.S_ISREG(metadata.st_mode) and not path.is_symlink(),
        f"{label} must be a regular file: {path}",
    )
    _require(
        0 < metadata.st_size <= 16 * 1024 * 1024,
        f"{label} exceeds size limit: {path}",
    )
    try:
        body = path.read_bytes()
        value = json.loads(body.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"{label} is not valid JSON: {path}") from exc
    _require(isinstance(value, dict), f"{label} must be a JSON object: {path}")
    return value, body


def _learning_evidence_public_contract(
    directory: Path,
    *,
    expected_industries: int = 10,
    expected_roles: int = 360,
    expected_topics: int = 1800,
) -> dict:
    """Extract only the immutable V4 public research surface.

    The digest deliberately excludes person, idx and every private identity or
    configuration hash.  Sidecar translations are joined solely through the
    public employee key and the frozen topic/anchor contract.
    """
    _require(
        directory.is_dir() and not directory.is_symlink(),
        f"V4 public research catalog missing: {directory}",
    )
    files = sorted(directory.iterdir())
    _require(
        len(files) == expected_industries,
        f"V4 public research catalog must contain {expected_industries} files",
    )
    industry_keys: set[str] = set()
    roles_by_key: dict[str, dict] = {}
    catalog_contract: list[dict] = []
    topic_count = 0
    for path in files:
        _require(
            path.is_file() and not path.is_symlink() and path.suffix == ".json",
            "V4 public research catalog contains an unexpected entry",
        )
        catalog = _read_release_json(path, label="V4 public research catalog")
        industry_key = str(catalog.get("key") or "").strip()
        _require(
            industry_key and industry_key not in industry_keys,
            "V4 public research industry key is missing or duplicate",
        )
        industry_keys.add(industry_key)
        _require(
            catalog.get("catalog_version")
            == _LEARNING_EVIDENCE_CATALOG_VERSION,
            f"V4 public research catalog version is invalid: {industry_key}",
        )
        employees = catalog.get("employees")
        _require(
            isinstance(employees, list) and employees,
            f"V4 public research employees are missing: {industry_key}",
        )
        for employee in employees:
            _require(
                isinstance(employee, dict),
                f"V4 public research employee is invalid: {industry_key}",
            )
            employee_key = str(employee.get("key") or "").strip()
            role_name = str(employee.get("name") or "").strip()
            topics = employee.get("public_research_topics")
            groups = employee.get("public_research_anchor_groups")
            _require(
                employee_key and employee_key not in roles_by_key and role_name,
                f"V4 public research employee key is missing or duplicate: {industry_key}",
            )
            _require(
                isinstance(topics, list)
                and all(isinstance(topic, str) and topic.strip() == topic for topic in topics)
                and role_name in topics,
                f"V4 public research topics are invalid: {employee_key}",
            )
            _require(
                isinstance(groups, list) and groups,
                f"V4 public research anchor groups are invalid: {employee_key}",
            )
            group_by_topic: dict[str, dict] = {}
            for group in groups:
                _require(
                    isinstance(group, dict)
                    and set(group) == {"topic", "object_anchors", "method_anchors"},
                    f"V4 public research anchor group shape is invalid: {employee_key}",
                )
                topic = str(group.get("topic") or "").strip()
                objects = group.get("object_anchors")
                methods = group.get("method_anchors")
                _require(
                    topic and topic not in group_by_topic
                    and isinstance(objects, list) and objects
                    and isinstance(methods, list) and methods
                    and all(
                        isinstance(anchor, str) and anchor.strip() == anchor
                        and anchor
                        for anchor in [*objects, *methods]
                    ),
                    f"V4 public research anchor group is invalid: {employee_key}",
                )
                group_by_topic[topic] = group
            _require(
                set(group_by_topic) == set(topics) - {role_name}
                and len(groups) == len(topics) - 1,
                f"V4 public research topic coverage is invalid: {employee_key}",
            )
            role_contract = {
                "public_research_topics": topics,
                "public_research_anchor_groups": groups,
            }
            try:
                employee_idx = int(employee.get("idx"))
            except (TypeError, ValueError) as exc:
                raise PreflightError(
                    f"V4 public research employee id is invalid: {employee_key}"
                ) from exc
            person = str(employee.get("person") or "").strip()
            _require(
                employee_idx > 0 and person,
                f"V4 public research identity guard is incomplete: {employee_key}",
            )
            roles_by_key[employee_key] = {
                "employee_key": employee_key,
                "industry_key": industry_key,
                "role_name": role_name,
                "person": person,
                "idx": employee_idx,
                "role_digest": _learning_evidence_sha256(role_contract),
                "groups": group_by_topic,
                "ordered_group_topics": [group["topic"] for group in groups],
            }
            catalog_contract.append({
                "employee_key": employee_key,
                "industry_key": industry_key,
                "role_name": role_name,
                **role_contract,
            })
            topic_count += len(groups)
    _require(
        len(roles_by_key) == expected_roles,
        f"V4 public research contract must contain {expected_roles} roles",
    )
    _require(
        topic_count == expected_topics,
        f"V4 public research contract must contain {expected_topics} topics",
    )
    catalog_contract.sort(
        key=lambda row: (row["industry_key"], row["employee_key"])
    )
    return {
        "catalog_digest": _learning_evidence_sha256(catalog_contract),
        "industry_keys": industry_keys,
        "roles_by_key": roles_by_key,
        "industries": len(industry_keys),
        "roles": len(roles_by_key),
        "topics": topic_count,
    }


def _learning_evidence_identity_safe_text(
    value,
    *,
    label: str,
    people: set[str],
    employee_id_pattern: re.Pattern[str],
    english: bool,
) -> str:
    _require(isinstance(value, str), f"{label} must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    _require(
        value == normalized and value == value.strip() and 2 <= len(value) <= 96,
        f"{label} must be trimmed NFKC text between 2 and 96 characters",
    )
    _require(
        _LEARNING_EVIDENCE_CONTROL.search(value) is None
        and _LEARNING_EVIDENCE_HEX64.search(value) is None,
        f"{label} contains forbidden control or digest material",
    )
    folded = value.casefold()
    _require(
        all(person.casefold() not in folded for person in people),
        f"{label} contains forbidden person material",
    )
    _require(
        employee_id_pattern.search(value) is None,
        f"{label} contains forbidden employee id material",
    )
    if english:
        _require(
            value.isascii() and re.search(r"[A-Za-z]", value) is not None,
            f"{label} must be printable English ASCII text",
        )
        words = re.findall(r"[a-z0-9]+", folded)
        _require(
            words and any(
                word not in _LEARNING_EVIDENCE_GENERIC_ENGLISH
                and not word.isdigit()
                for word in words
            ),
            f"{label} must not be generic-only",
        )
    return value


def _learning_evidence_alias_list(
    raw,
    *,
    label: str,
    source_anchors: set[str],
    people: set[str],
    employee_id_pattern: re.Pattern[str],
) -> list[str]:
    _require(
        isinstance(raw, list) and 3 <= len(raw) <= 12,
        f"{label} must contain between 3 and 12 aliases",
    )
    aliases: list[str] = []
    seen: set[str] = set()
    for row in raw:
        _require(
            isinstance(row, dict) and set(row) == {"alias", "source_anchor"},
            f"{label} alias entry shape is invalid",
        )
        alias = _learning_evidence_identity_safe_text(
            row.get("alias"),
            label=f"{label} alias",
            people=people,
            employee_id_pattern=employee_id_pattern,
            english=True,
        )
        source_anchor = row.get("source_anchor")
        _require(
            isinstance(source_anchor, str) and source_anchor in source_anchors,
            f"{label} source_anchor does not reference its frozen V4 anchor",
        )
        folded = unicodedata.normalize("NFKC", alias).casefold()
        _require(folded not in seen, f"{label} contains a duplicate alias")
        seen.add(folded)
        aliases.append(folded)
    return aliases


def _validate_learning_evidence_authorities(registry) -> str:
    _require(
        isinstance(registry, list) and 1 <= len(registry) <= 512,
        "learning evidence authority registry must be a bounded non-empty list",
    )
    seen: set[tuple[str, str]] = set()
    for row in registry:
        _require(
            isinstance(row, dict) and set(row) == {"host", "match", "kind"},
            "learning evidence authority entry shape is invalid",
        )
        host = row.get("host")
        match = row.get("match")
        kind = row.get("kind")
        _require(
            isinstance(host, str) and host == host.strip()
            and host == host.lower() and host.isascii()
            and _LEARNING_EVIDENCE_HOST.fullmatch(host) is not None,
            "learning evidence authority host must be lowercase bare ASCII DNS",
        )
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise PreflightError("learning evidence authority host must not be an IP address")
        _require(
            match in {"exact", "suffix"}
            and kind in _LEARNING_EVIDENCE_AUTHORITY_KINDS,
            "learning evidence authority match or kind is invalid",
        )
        if match == "suffix":
            _require(
                host.count(".") >= 2
                and host not in _LEARNING_EVIDENCE_UNSAFE_SUFFIXES,
                "learning evidence authority suffix is not explicitly safe",
            )
        key = (host, match)
        _require(key not in seen, "learning evidence authority entry is duplicated")
        seen.add(key)
    return _learning_evidence_sha256(registry)


def _validate_learning_evidence_gate(
    sidecar_path: Path,
    v4_directory: Path,
    *,
    expected_industries: int = 10,
    expected_roles: int = 360,
    expected_topics: int = 1800,
) -> dict:
    """Fail closed on multilingual aliases before release build or startup."""
    contract = _learning_evidence_public_contract(
        v4_directory,
        expected_industries=expected_industries,
        expected_roles=expected_roles,
        expected_topics=expected_topics,
    )
    sidecar, actual_sidecar = _read_learning_evidence_json(
        sidecar_path,
        label="learning evidence sidecar",
    )
    canonical_sidecar = (
        json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    _require(
        actual_sidecar == canonical_sidecar,
        "learning evidence sidecar bytes are not canonical",
    )
    _require(
        set(sidecar) == _LEARNING_EVIDENCE_TOP_KEYS,
        "learning evidence sidecar top-level shape is invalid",
    )
    _require(
        sidecar.get("schema") == _LEARNING_EVIDENCE_SCHEMA
        and sidecar.get("catalog_version")
        == _LEARNING_EVIDENCE_CATALOG_VERSION,
        "learning evidence sidecar schema or catalog version is invalid",
    )
    _require(
        sidecar.get("source_catalog_sha256") == contract["catalog_digest"],
        "learning evidence sidecar source catalog digest drifted",
    )
    roles_by_key = contract["roles_by_key"]
    people = {row["person"] for row in roles_by_key.values()}
    employee_ids = {row["idx"] for row in roles_by_key.values()}
    employee_id_pattern = re.compile(
        r"(?<![0-9])(?:"
        + "|".join(str(value) for value in sorted(employee_ids))
        + r")(?![0-9])"
    )

    registry = sidecar.get("authority_registry")
    authority_digest = _validate_learning_evidence_authorities(registry)
    _require(
        sidecar.get("authority_policy_sha256") == authority_digest,
        "learning evidence sidecar authority policy digest drifted",
    )

    industry_aliases = sidecar.get("industry_aliases")
    _require(
        isinstance(industry_aliases, list)
        and len(industry_aliases) == expected_industries,
        "learning evidence sidecar industry aliases have incomplete coverage",
    )
    seen_industries: set[str] = set()
    for row in industry_aliases:
        _require(
            isinstance(row, dict)
            and set(row) == {"industry_key", "aliases_zh", "aliases_en"},
            "learning evidence industry alias shape is invalid",
        )
        industry_key = str(row.get("industry_key") or "").strip()
        _require(
            industry_key in contract["industry_keys"]
            and industry_key not in seen_industries,
            "learning evidence industry alias key is invalid or duplicate",
        )
        seen_industries.add(industry_key)
        for language, english in (("aliases_zh", False), ("aliases_en", True)):
            values = row.get(language)
            _require(
                isinstance(values, list) and 1 <= len(values) <= 16,
                f"learning evidence {language} must be a bounded non-empty list",
            )
            normalized = [
                _learning_evidence_identity_safe_text(
                    value,
                    label=f"learning evidence {language}",
                    people=people,
                    employee_id_pattern=employee_id_pattern,
                    english=english,
                )
                for value in values
            ]
            _require(
                len({value.casefold() for value in normalized}) == len(normalized),
                f"learning evidence {language} contains NFKC/casefold duplicates",
            )
    _require(
        seen_industries == contract["industry_keys"],
        "learning evidence industry alias coverage is incomplete",
    )

    employees = sidecar.get("employees")
    _require(
        isinstance(employees, list) and len(employees) == expected_roles,
        f"learning evidence sidecar must contain {expected_roles} roles",
    )
    seen_roles: set[str] = set()
    rendered_topics = 0
    for employee in employees:
        _require(
            isinstance(employee, dict)
            and set(employee) == _LEARNING_EVIDENCE_EMPLOYEE_KEYS,
            "learning evidence employee shape is invalid",
        )
        employee_key = str(employee.get("employee_key") or "").strip()
        source = roles_by_key.get(employee_key)
        _require(
            source is not None and employee_key not in seen_roles,
            "learning evidence employee key is unknown or duplicate",
        )
        seen_roles.add(employee_key)
        _require(
            employee.get("industry_key") == source["industry_key"],
            f"learning evidence employee industry drifted: {employee_key}",
        )
        _require(
            employee.get("source_public_contract_sha256")
            == source["role_digest"],
            f"learning evidence public contract digest drifted: {employee_key}",
        )
        _learning_evidence_identity_safe_text(
            employee.get("job_label_en"),
            label=f"learning evidence job label: {employee_key}",
            people=people,
            employee_id_pattern=employee_id_pattern,
            english=True,
        )
        topics = employee.get("topics")
        _require(
            isinstance(topics, list)
            and len(topics) == len(source["ordered_group_topics"]),
            f"learning evidence topic coverage is incomplete: {employee_key}",
        )
        seen_topic_ids: set[str] = set()
        seen_canonical_topics: set[str] = set()
        for topic in topics:
            _require(
                isinstance(topic, dict)
                and set(topic) == _LEARNING_EVIDENCE_TOPIC_KEYS,
                f"learning evidence topic shape is invalid: {employee_key}",
            )
            topic_id = topic.get("topic_id")
            canonical_topic = topic.get("canonical_topic")
            _require(
                isinstance(topic_id, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", topic_id)
                and topic_id not in seen_topic_ids,
                f"learning evidence topic id is invalid or duplicate: {employee_key}",
            )
            seen_topic_ids.add(topic_id)
            _require(
                isinstance(canonical_topic, str)
                and canonical_topic in source["groups"]
                and canonical_topic not in seen_canonical_topics,
                f"learning evidence canonical topic is unknown or duplicate: {employee_key}",
            )
            seen_canonical_topics.add(canonical_topic)
            _require(
                topic.get("canonical_topic_sha256")
                == _learning_evidence_sha256(canonical_topic),
                f"learning evidence canonical topic digest drifted: {employee_key}",
            )
            _learning_evidence_identity_safe_text(
                topic.get("label_en"),
                label=f"learning evidence topic label: {employee_key}",
                people=people,
                employee_id_pattern=employee_id_pattern,
                english=True,
            )
            group = source["groups"][canonical_topic]
            object_aliases = _learning_evidence_alias_list(
                topic.get("object_aliases_en"),
                label=f"learning evidence object aliases: {employee_key}/{topic_id}",
                source_anchors=set(group["object_anchors"]),
                people=people,
                employee_id_pattern=employee_id_pattern,
            )
            method_aliases = _learning_evidence_alias_list(
                topic.get("method_aliases_en"),
                label=f"learning evidence method aliases: {employee_key}/{topic_id}",
                source_anchors=set(group["method_anchors"]),
                people=people,
                employee_id_pattern=employee_id_pattern,
            )
            _require(
                not any(
                    obj in method or method in obj
                    for obj in object_aliases for method in method_aliases
                ),
                f"learning evidence object/method aliases overlap: {employee_key}/{topic_id}",
            )
            rendered_topics += 1
        _require(
            seen_canonical_topics == set(source["groups"]),
            f"learning evidence topic coverage drifted: {employee_key}",
        )
    _require(
        seen_roles == set(roles_by_key),
        "learning evidence role coverage is incomplete",
    )
    _require(
        rendered_topics == expected_topics,
        f"learning evidence sidecar must contain {expected_topics} topics",
    )
    return {
        "industries": len(seen_industries),
        "roles": len(seen_roles),
        "topics": rendered_topics,
        "source_catalog_sha256": contract["catalog_digest"],
        "authority_policy_sha256": authority_digest,
    }


def _verify_signed_release_payload(
    root: Path,
    state: Path,
    venv: Path,
    manifest: dict,
) -> dict:
    """Recompute the immutable materialized tree from the signed manifest.

    Archive extraction already performs this proof, but application preflight
    runs later and must detect post-extraction mutation too.  Reuse the
    standalone verifier's canonical manifest and materialized-tree contracts:
    it validates entry structure/order/path/mode/size/digest/tree digest and
    treats only the exact ``data`` state symlink and real release ``venv`` as
    unsigned runtime entries.
    """
    expected_venv = root / "venv"
    try:
        _require(
            venv == expected_venv.resolve(strict=True),
            f"release venv must be the real runtime directory: {expected_venv}",
        )
        validated, entries = verify_release._validate_manifest(manifest)
        verify_release._verify_materialized_payload(
            root,
            manifest=validated,
            entries=entries,
            allow_runtime=True,
            expected_data_target=state,
        )
    except (verify_release.ReleaseVerifyError, OSError) as exc:
        raise PreflightError(
            f"release manifest or payload verification failed: {exc}"
        ) from exc
    return validated


_UNSAFE_APPROVAL_BOUNDARY_PHRASES = (
    "无需人工",
    "无须人工",
    "不需人工",
    "免于人工",
    "系统自动放行",
    "系统自动执行",
    "系统直接放行",
    "系统直接执行",
    "系统自行放行",
    "系统自行执行",
    "可自动放行",
    "可自动执行",
)


def _validate_decision_prose_boundary(contract: dict, employee_key: str) -> None:
    """Reject prose that contradicts the machine execution boundary."""
    approval = re.sub(
        r"\s+", "", str(contract.get("approval_boundary") or "")
    ).lower()
    fallback = re.sub(
        r"\s+", "", str(contract.get("fallback") or "")
    ).upper()
    _require(
        not any(
            phrase.lower() in approval
            for phrase in _UNSAFE_APPROVAL_BOUNDARY_PHRASES
        ),
        f"decision contract approval boundary permits automatic execution: "
        f"{employee_key}",
    )
    automated_actions = (
        "放行", "执行", "审批", "下单", "写入", "发布", "触达", "改价",
        "退款", "调度", "排班", "停店", "上下架", "采购", "结算",
    )
    for occurrence in re.finditer("自动", approval):
        prefix = approval[max(0, occurrence.start() - 10):occurrence.start()]
        suffix = approval[occurrence.start():occurrence.start() + 16]
        explicitly_prohibited = any(
            token in prefix
            for token in ("不得", "禁止", "不能", "不可", "不允许", "严禁", "无权")
        )
        _require(
            explicitly_prohibited
            or not any(action in suffix for action in automated_actions),
            f"decision contract approval boundary permits automatic execution: "
            f"{employee_key}",
        )
    # A fallback is entered because required evidence is absent or conflicting.
    # GO is therefore contradictory regardless of surrounding prose; only
    # HOLD/ESCALATE/ADVISE are safe missing-data states.
    _require(
        "GO" not in fallback,
        f"decision contract fallback permits GO with missing data: {employee_key}",
    )
    _require(
        any(state in fallback for state in ("HOLD", "ESCALATE", "ADVISE")),
        f"decision contract fallback lacks a safe state: {employee_key}",
    )


def _validate_industry_decision_configs(
    directory: Path,
    *,
    legacy_employee_ids: set[int],
    catalog_version: str = "2026.08.v2",
    expected_count: int = 6,
    allow_original_ids: bool = False,
) -> dict:
    """Validate one immutable decision roster before the service can start."""
    expected = {
        "auto", "beauty", "convenience", "fitness", "grocery", "hotel",
        "pet", "pharmacy", "snack", "tea_coffee",
    }
    _require(
        directory.is_dir() and not directory.is_symlink(),
        f"immutable industry decision configuration missing: {directory}",
    )
    files = sorted(directory.iterdir())
    _require(
        len(files) == len(expected),
        "immutable industry decision configuration must contain ten files",
    )
    keys: set[str] = set()
    employee_ids: set[int] = set()
    employee_keys: set[str] = set()
    employee_names: set[str] = set()
    employee_persons: set[str] = set()
    employee_decisions: set[str] = set()
    v3_metric_values: dict[str, set[str]] = {
        "key": set(), "name": set(), "formula": set(),
    }
    v3_exact_fingerprints: dict[str, set[str]] = {
        "workflow": set(), "outputs": set(), "tools": set(),
        "skills": set(), "capabilities": set(), "escalation": set(),
        "learning": set(),
    }
    employee_count = 0
    for path in files:
        _require(
            path.is_file() and not path.is_symlink() and path.suffix == ".json",
            "immutable industry decision configuration contains an unexpected entry",
        )
        catalog = _read_release_json(path, label="industry decision configuration")
        industry = str(catalog.get("key") or "").strip()
        _require(industry in expected and industry not in keys, "invalid decision industry key")
        keys.add(industry)
        _require(
            catalog.get("catalog_version") == catalog_version,
            f"industry decision catalog version is invalid: {industry}",
        )
        sources = catalog.get("sources")
        pains = catalog.get("pain_points")
        groups = catalog.get("groups")
        employees = catalog.get("employees")
        _require(isinstance(sources, list) and sources, f"decision sources missing: {industry}")
        _require(isinstance(pains, list) and pains, f"decision pain points missing: {industry}")
        _require(isinstance(groups, list) and groups, f"decision groups missing: {industry}")
        _require(
            isinstance(employees, list) and len(employees) == expected_count,
            f"industry decision roster must contain {expected_count} employees: {industry}",
        )
        if allow_original_ids:
            _require(
                len(pains) == 8 and len(groups) == 8
                and [len(group.get("members") or []) for group in groups]
                == [5, 5, 5, 5, 5, 4, 4, 3],
                f"V3 decision catalog must contain eight pain clusters and "
                f"the fixed 36-person group shape: {industry}",
            )
        source_ids = {
            str(source.get("id") or "").strip()
            for source in sources if isinstance(source, dict)
        }
        _require(
            len(source_ids) == len(sources) and all(source_ids),
            f"decision source ids are invalid: {industry}",
        )
        _require(
            all(
                str(source.get("url") or "").startswith("https://")
                and source.get("source_type") in {
                    "official", "standard", "association", "annual_report",
                }
                for source in sources
            ),
            f"decision sources are not authoritative HTTPS records: {industry}",
        )
        pain_codes = {
            str(pain.get("code") or "").strip()
            for pain in pains if isinstance(pain, dict)
        }
        _require(
            len(pain_codes) == len(pains) and all(pain_codes),
            f"decision pain codes are invalid: {industry}",
        )
        _require(
            all(
                isinstance(pain.get("source_ids"), list)
                and pain["source_ids"]
                and set(pain["source_ids"]) <= source_ids
                for pain in pains
            ),
            f"decision pain source references are invalid: {industry}",
        )
        covered: set[str] = set()
        for employee in employees:
            _require(isinstance(employee, dict), f"decision employee is invalid: {industry}")
            try:
                employee_id = int(employee.get("idx"))
            except (TypeError, ValueError) as exc:
                raise PreflightError(f"decision employee id is invalid: {industry}") from exc
            employee_key = str(employee.get("key") or "").strip()
            employee_name = str(employee.get("name") or "").strip()
            employee_person = str(employee.get("person") or "").strip()
            if allow_original_ids:
                ranges = {
                    "tea_coffee": (1001, 1036), "convenience": (1101, 1136),
                    "snack": (1201, 1236), "grocery": (1301, 1336),
                    "pharmacy": (1401, 1436), "hotel": (1501, 1536),
                    "auto": (1601, 1636), "fitness": (1701, 1736),
                    "beauty": (1801, 1836), "pet": (1901, 1936),
                }
                low, high = ranges[industry]
                _require(
                    low <= employee_id <= high and employee_id in legacy_employee_ids
                    and employee_id not in employee_ids,
                    f"V3 decision employee did not preserve original id: {employee_id}",
                )
            else:
                _require(
                    20000 <= employee_id <= 29999
                    and employee_id not in legacy_employee_ids
                    and employee_id not in employee_ids,
                    f"decision employee id is reused or invalid: {employee_id}",
                )
            _require(
                employee_key and employee_key not in employee_keys,
                f"decision employee key is missing or duplicate: {industry}",
            )
            _require(
                employee_name and employee_name not in employee_names,
                f"decision employee name is missing or duplicate: {industry}",
            )
            if catalog_version == "2026.08.v4":
                raw_person_snapshot = str(
                    employee.get("person_snapshot", employee_person) or ""
                ).strip()
                _require(
                    re.fullmatch(r"[\u4e00-\u9fff]{2,4}", employee_person) is not None,
                    f"V4 decision employee person is invalid: {employee_id}",
                )
                _require(
                    raw_person_snapshot == employee_person,
                    f"V4 decision employee person snapshot is invalid: {employee_id}",
                )
                _require(
                    employee.get("identity_scheme") in (None, "v2-person"),
                    f"V4 decision employee identity scheme is invalid: {employee_id}",
                )
                _require(
                    employee_person not in employee_persons,
                    f"V4 decision employee person is duplicated: {employee_person}",
                )
                employee_persons.add(employee_person)
                topics = employee.get("public_research_topics")
                clean_topics = [
                    topic.strip() for topic in topics
                ] if isinstance(topics, list) and all(
                    isinstance(topic, str) for topic in topics
                ) else []
                forbidden_topic_values = {
                    str(employee.get(field) or "").strip()
                    for field in (
                        "identity_ref", "config_sha256", "bundle_sha256",
                        "employee_spec_sha256", "spec_sha256",
                    )
                    if str(employee.get(field) or "").strip()
                }
                _require(
                    isinstance(topics, list) and 3 <= len(topics) <= 6
                    and len(clean_topics) == len(topics)
                    and all(
                        2 <= len(topic) <= 120
                        and re.search(r"[\x00-\x1f\x7f]", topic) is None
                        and re.search(
                            r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])",
                            topic,
                        ) is None
                        for topic in clean_topics
                    )
                    and len(set(clean_topics)) == len(topics)
                    and employee_name in set(clean_topics)
                    and all(employee_person not in topic for topic in clean_topics)
                    and all(str(employee_id) not in topic for topic in clean_topics)
                    and all(
                        secret not in topic
                        for secret in forbidden_topic_values
                        for topic in clean_topics
                    ),
                    f"V4 public research topics are invalid: {employee_id}",
                )
                anchor_groups = employee.get("public_research_anchor_groups")
                expected_topics = set(clean_topics) - {employee_name}
                actual_topics = set()
                anchor_groups_valid = isinstance(anchor_groups, list)
                if anchor_groups_valid:
                    for group in anchor_groups:
                        if not isinstance(group, dict):
                            anchor_groups_valid = False
                            break
                        topic = str(group.get("topic") or "").strip()
                        objects = group.get("object_anchors")
                        methods = group.get("method_anchors")
                        if (
                            topic not in expected_topics or topic in actual_topics
                            or not isinstance(objects, list) or not 2 <= len(objects) <= 24
                            or not isinstance(methods, list) or not 1 <= len(methods) <= 12
                            or any(
                                not isinstance(value, str)
                                or not 3 <= len(value.strip()) <= 120
                                or re.search(
                                    r"[\x00-\x1f\x7f]", value.strip(),
                                ) is not None
                                or employee_person in value
                                or str(employee_id) in value
                                or re.search(
                                    r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])",
                                    value,
                                ) is not None
                                or any(
                                    secret in value
                                    for secret in forbidden_topic_values
                                )
                                for value in [*objects, *methods]
                            )
                            or len({value.strip() for value in objects}) != len(objects)
                            or len({value.strip() for value in methods}) != len(methods)
                            or any(
                                str(obj).strip().lower() in str(method).strip().lower()
                                or str(method).strip().lower() in str(obj).strip().lower()
                                for obj in objects for method in methods
                            )
                        ):
                            anchor_groups_valid = False
                            break
                        actual_topics.add(topic)
                _require(
                    anchor_groups_valid and actual_topics == expected_topics,
                    f"V4 public research anchor groups are invalid: {employee_id}",
                )
            employee_ids.add(employee_id)
            employee_keys.add(employee_key)
            employee_names.add(employee_name)
            employee_count += 1
            employee_pains = employee.get("pain_codes")
            _require(
                isinstance(employee_pains, list) and employee_pains
                and set(employee_pains) <= pain_codes,
                f"decision employee pain coverage is invalid: {employee_key}",
            )
            covered.update(employee_pains)
            contract = employee.get("decision_contract")
            _require(isinstance(contract, dict), f"decision contract missing: {employee_key}")
            if allow_original_ids:
                primary_decision = str(employee.get("primary_decision") or "").strip()
                _require(
                    primary_decision
                    and primary_decision == str(contract.get("decision") or "").strip(),
                    f"V3 primary decision is missing or mismatched: {employee_key}",
                )
                _require(
                    primary_decision not in employee_decisions,
                    f"V3 primary decision is duplicated across industries: {primary_decision}",
                )
                employee_decisions.add(primary_decision)
                score = employee.get("priority_score")
                score_fields = (
                    "pain_severity", "usage_frequency", "economic_value",
                    "data_availability",
                )
                _require(
                    isinstance(score, dict)
                    and set(score) == {*score_fields, "total"}
                    and all(type(score.get(field)) is int and 1 <= score[field] <= 5
                            for field in score_fields)
                    and score.get("total") == sum(score[field] for field in score_fields),
                    f"V3 priority score is invalid: {employee_key}",
                )
                _require(
                    isinstance(employee.get("priority_rank"), int)
                    and 1 <= employee["priority_rank"] <= 36
                    and bool(str(employee.get("usage_cadence") or "").strip())
                    and bool(str(employee.get("selection_rationale") or "").strip()),
                    f"V3 priority metadata is invalid: {employee_key}",
                )
                profile = employee.get("professional_profile")
                profile_keys = {
                    "scope", "decisions", "knowledge_domains", "data_objects",
                    "tool_permissions", "skill_tree", "capabilities",
                    "operating_rhythm", "escalation_matrix", "learning_tracks",
                }
                _require(
                    isinstance(profile, dict) and set(profile) == profile_keys
                    and bool(str(profile.get("scope") or "").strip())
                    and isinstance(profile.get("decisions"), list)
                    and len(profile["decisions"]) >= 2
                    and primary_decision in profile["decisions"]
                    and isinstance(profile.get("knowledge_domains"), list)
                    and len(profile["knowledge_domains"]) >= 3
                    and isinstance(profile.get("data_objects"), list)
                    and len(profile["data_objects"]) >= 3
                    and isinstance(profile.get("skill_tree"), list)
                    and len(profile["skill_tree"]) >= 5
                    and isinstance(profile.get("capabilities"), list)
                    and len(profile["capabilities"]) >= 4
                    and isinstance(profile.get("learning_tracks"), list)
                    and len(profile["learning_tracks"]) >= 3,
                    f"V3 professional profile is incomplete: {employee_key}",
                )
                _require(
                    isinstance(profile.get("tool_permissions"), list)
                    and len(profile["tool_permissions"]) >= 2
                    and all(
                        isinstance(row, dict)
                        and set(row) == {"tool", "access", "scope"}
                        and row.get("access") == "read_only"
                        and bool(str(row.get("tool") or "").strip())
                        and bool(str(row.get("scope") or "").strip())
                        for row in profile["tool_permissions"]
                    ),
                    f"V3 tool permissions are unsafe or incomplete: {employee_key}",
                )
                _require(
                    isinstance(profile.get("operating_rhythm"), dict)
                    and set(profile["operating_rhythm"])
                    == {"daily", "event_driven", "review"}
                    and all(str(value or "").strip()
                            for value in profile["operating_rhythm"].values())
                    and isinstance(profile.get("escalation_matrix"), list)
                    and len(profile["escalation_matrix"]) >= 2
                    and all(
                        isinstance(row, dict)
                        and set(row) == {"level", "condition", "owner", "action"}
                        and all(str(value or "").strip() for value in row.values())
                        for row in profile["escalation_matrix"]
                    ),
                    f"V3 rhythm/escalation profile is incomplete: {employee_key}",
                )
                exact_groups = {
                    "workflow": contract.get("workflow"),
                    "outputs": contract.get("outputs"),
                    "tools": profile.get("tool_permissions"),
                    "skills": profile.get("skill_tree"),
                    "capabilities": profile.get("capabilities"),
                    "escalation": profile.get("escalation_matrix"),
                    "learning": profile.get("learning_tracks"),
                }
                for label, value in exact_groups.items():
                    fingerprint = json.dumps(
                        value, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    )
                    _require(
                        fingerprint not in v3_exact_fingerprints[label],
                        f"V3 {label} is copied across industries: {employee_key}",
                    )
                    v3_exact_fingerprints[label].add(fingerprint)
            _require(
                contract.get("decision_states") == ["GO", "HOLD", "ESCALATE", "ADVISE"],
                f"decision states are invalid: {employee_key}",
            )
            for field, minimum in (
                ("triggers", 2), ("required_inputs", 4),
                ("evidence_required", 2), ("workflow", 4),
                ("outputs", 2), ("forbidden_actions", 3),
            ):
                values = contract.get(field)
                _require(
                    isinstance(values, list) and len(values) >= minimum
                    and all(isinstance(value, str) and value.strip() for value in values),
                    f"decision contract {field} is invalid: {employee_key}",
                )
            metrics = contract.get("success_metrics")
            metric_fields = {
                "key", "name", "formula", "window", "source",
                "baseline_policy", "target_policy",
            }
            _require(
                isinstance(metrics, list) and len(metrics) >= 3
                and all(
                    isinstance(metric, dict)
                    and metric_fields <= set(metric)
                    and all(
                        isinstance(metric[field], str) and metric[field].strip()
                        for field in metric_fields
                    )
                    and bool(re.fullmatch(
                        r"[a-z][a-z0-9_]{2,63}", str(metric["key"])
                    ))
                    for metric in metrics
                )
                and len({str(metric["key"]) for metric in metrics}) == len(metrics),
                f"decision contract success_metrics are invalid: {employee_key}",
            )
            if allow_original_ids:
                for metric in metrics:
                    for field in ("key", "name", "formula"):
                        value = str(metric[field]).strip()
                        _require(
                            value not in v3_metric_values[field],
                            f"V3 metric {field} is duplicated across industries: {value}",
                        )
                        v3_metric_values[field].add(value)
            _require(
                contract.get("requires_human_approval") is True
                and contract.get("allowed_side_effects") == []
                and contract.get("go_semantics")
                == "证据足以进入人工审批，不代表允许系统执行任何业务写操作",
                f"decision contract execution boundary is invalid: {employee_key}",
            )
            _require(
                bool(str(contract.get("approval_boundary") or "").strip())
                and bool(str(contract.get("fallback") or "").strip()),
                f"decision contract boundary/fallback is invalid: {employee_key}",
            )
            _validate_decision_prose_boundary(contract, employee_key)
        _require(covered == pain_codes, f"decision pain points are uncovered: {industry}")
        if allow_original_ids:
            ranks = [employee["priority_rank"] for employee in employees]
            _require(
                sorted(ranks) == list(range(1, 37)),
                f"V3 priority ranks must cover 1..36: {industry}",
            )
            ranked = sorted(
                employees,
                key=lambda employee: (
                    -employee["priority_score"]["total"],
                    -employee["priority_score"]["usage_frequency"],
                    -employee["priority_score"]["economic_value"],
                    -employee["priority_score"]["pain_severity"],
                    -employee["priority_score"]["data_availability"],
                    employee["idx"],
                ),
            )
            _require(
                all(employee["priority_rank"] == rank
                    for rank, employee in enumerate(ranked, 1)),
                f"V3 priority ranks do not follow the value score: {industry}",
            )
            _require(
                len({employee["usage_cadence"] for employee in employees}) >= 3,
                f"V3 usage cadence is overly uniform: {industry}",
            )
            generic_workflow_markers = (
                "冻结判断对象、门店范围、数据时点、批准责任与",
                "标明GO/HOLD/ESCALATE/ADVISE依据",
            )
            generic_metric_markers = (
                "能回链", "反证及人工结论", "人工复核可解释率",
                "批准截止前补齐证据并关闭", "到期关闭率",
            )
            generic_metric_patterns = (
                re.compile(r"^无需返工即通过.+的.+记录数\s*÷\s*同期进入.+的.+记录总数"),
                re.compile(r"^在.+后完成.+且.+无未决冲突的事项数\s*÷\s*同期应完成.+的事项总数"),
            )
            for employee in employees:
                profile = employee["professional_profile"]
                _require(
                    all(row["tool"] not in {
                        "企业事实查询器", "证据版本追溯器", "通用数据平台", "业务系统",
                    } for row in profile["tool_permissions"]),
                    f"V3 role uses a generic placeholder tool: {employee['key']}",
                )
                _require(
                    all(not track.startswith((
                        "方法进修：", "案例进修：", "结果校准：",
                    )) for track in profile["learning_tracks"]),
                    f"V3 learning tracks use a copied three-part template: {employee['key']}",
                )
                _require(
                    all(not any(marker in row["condition"] for marker in (
                        "时发现不可由岗位消除的重大影响",
                        "触及企业书面红线",
                        "仍无法对齐同一对象与时点，则停止当前判断",
                        "停售、停产、资金、隐私或恢复红线",
                        "授权内隔离影响",
                    )) for row in profile["escalation_matrix"]),
                    f"V3 escalation uses generic business signals: {employee['key']}",
                )
                professional_steps = [
                    step for step in employee["decision_contract"]["workflow"]
                    if not any(marker in step for marker in generic_workflow_markers)
                ]
                _require(
                    len(professional_steps) >= 4,
                    f"V3 role has fewer than four professional steps: {employee['key']}",
                )
                native_metrics = 0
                for metric in employee["decision_contract"]["success_metrics"]:
                    metric_text = f"{metric['name']}\n{metric['formula']}"
                    if not any(marker in metric_text for marker in generic_metric_markers):
                        native_metrics += 1
                    _require(
                        not any(pattern.search(metric["formula"].strip())
                                for pattern in generic_metric_patterns),
                        f"V3 metric uses a skill/object substitution template: {employee['key']}",
                    )
                _require(
                    native_metrics == 3,
                    f"V3 role does not have three native business metrics: {employee['key']}",
                )
            for label, values in (
                ("primary decision", [e["primary_decision"] for e in employees]),
                ("workflow", [json.dumps(e["decision_contract"]["workflow"], ensure_ascii=False, sort_keys=True) for e in employees]),
                ("outputs", [json.dumps(e["decision_contract"]["outputs"], ensure_ascii=False, sort_keys=True) for e in employees]),
                ("skills", [json.dumps(e["professional_profile"]["skill_tree"], ensure_ascii=False, sort_keys=True) for e in employees]),
                ("capabilities", [json.dumps(e["professional_profile"]["capabilities"], ensure_ascii=False, sort_keys=True) for e in employees]),
            ):
                _require(
                    len(values) == len(set(values)),
                    f"V3 {label} contains copied role groups: {industry}",
                )
    _require(
        keys == expected and employee_count == len(expected) * expected_count,
        "decision roster coverage is incomplete",
    )
    if allow_original_ids:
        _require(
            all(len(values) == 1080 for values in v3_metric_values.values()),
            "V3 must contain 1080 globally unique metric keys, names and formulas",
        )
    return {"files": len(files), "employees": employee_count}


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
    manifest = _verify_signed_release_payload(
        root_path,
        state_path,
        venv_path,
        manifest,
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
    department_keys: set[str] = set()
    legacy_employee_ids: set[int] = set()
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
        for employee in department["employees"]:
            _require(
                isinstance(employee, dict),
                f"department employee must be an object: {department_file}",
            )
            try:
                employee_id = int(employee.get("idx"))
            except (TypeError, ValueError) as exc:
                raise PreflightError(
                    f"department employee id is invalid: {department_file}"
                ) from exc
            _require(
                employee_id not in legacy_employee_ids,
                f"duplicate department employee id: {employee_id}",
            )
            legacy_employee_ids.add(employee_id)
        department_key = str(department.get("key") or "").strip()
        _require(
            bool(department_key),
            f"department configuration key is missing: {department_file}",
        )
        _require(
            department_key not in department_keys,
            f"duplicate department configuration key: {department_key}",
        )
        department_keys.add(department_key)
    try:
        candidate_schema_version = int(manifest.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise PreflightError("release manifest schema version is invalid") from exc
    decision_report = {"files": 0, "employees": 0}
    learning_evidence_report = {"industries": 0, "roles": 0, "topics": 0}
    if candidate_schema_version >= 53:
        decision_report = _validate_industry_decision_configs(
            config_root / "industry_decisions",
            legacy_employee_ids=legacy_employee_ids,
        )
    if candidate_schema_version >= 54:
        v3_report = _validate_industry_decision_configs(
            config_root / "industry_decisions_v3",
            legacy_employee_ids=legacy_employee_ids,
            catalog_version="2026.08.v3",
            expected_count=36,
            allow_original_ids=True,
        )
        decision_report = {
            "files": decision_report["files"] + v3_report["files"],
            "employees": v3_report["employees"],
            "historical_employees": decision_report["employees"],
        }
    if candidate_schema_version >= 55:
        # V3 remains immutable historical material; V4 is the only current
        # 360-person decision roster in schema55.
        v4_report = _validate_industry_decision_configs(
            config_root / "industry_decisions_v4",
            legacy_employee_ids=legacy_employee_ids,
            catalog_version="2026.08.v4",
            expected_count=36,
            allow_original_ids=True,
        )
        decision_report = {
            "files": decision_report["files"] + v4_report["files"],
            "employees": v4_report["employees"],
            "historical_employees": (
                int(decision_report.get("historical_employees") or 0)
                + int(decision_report.get("employees") or 0)
            ),
        }
        learning_evidence_report = _validate_learning_evidence_gate(
            root_path / "app" / "learning_evidence_gate_v1.json",
            config_root / "industry_decisions_v4",
        )
    industry_knowledge = config_root / "industry_knowledge"
    _require(
        industry_knowledge.is_dir() and not industry_knowledge.is_symlink(),
        f"immutable industry knowledge missing: {industry_knowledge}",
    )
    industry_knowledge_files = sorted(industry_knowledge.iterdir())
    _require(
        industry_knowledge_files,
        f"immutable industry knowledge is empty: {industry_knowledge}",
    )
    industry_keys: set[str] = set()
    for knowledge_file in industry_knowledge_files:
        _require(
            knowledge_file.suffix == ".json",
            "immutable industry knowledge contains an unexpected entry",
        )
        knowledge = _read_release_json(
            knowledge_file,
            label="industry knowledge",
        )
        knowledge_key = str(knowledge.get("key") or "").strip()
        _require(
            bool(knowledge_key) and bool(str(knowledge.get("name") or "").strip()),
            f"industry knowledge identity is invalid: {knowledge_file}",
        )
        _require(
            knowledge_key not in industry_keys,
            f"duplicate industry knowledge key: {knowledge_key}",
        )
        for section in (
            "metrics", "benchmarks", "glossary", "practices",
            "compliance", "pitfalls",
        ):
            _require(
                isinstance(knowledge.get(section), list),
                f"industry knowledge {section} must be a list: {knowledge_file}",
            )
        _require(
            all(
                isinstance(metric, dict)
                and bool(str(metric.get("name") or "").strip())
                and bool(str(metric.get("formula") or "").strip())
                for metric in knowledge["metrics"]
            ),
            f"industry knowledge metrics are invalid: {knowledge_file}",
        )
        _require(
            all(
                isinstance(benchmark, dict)
                and all(
                    bool(str(benchmark.get(field) or "").strip())
                    for field in ("metric", "range", "source", "scope", "as_of")
                )
                for benchmark in knowledge["benchmarks"]
            ),
            f"industry knowledge benchmarks are invalid: {knowledge_file}",
        )
        industry_keys.add(knowledge_key)
    _require(
        industry_keys == department_keys,
        "industry knowledge keys do not match department configuration",
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
        "industry_decision_files": decision_report["files"],
        "industry_decision_employees": decision_report["employees"],
        "learning_evidence_industries": learning_evidence_report["industries"],
        "learning_evidence_roles": learning_evidence_report["roles"],
        "learning_evidence_topics": learning_evidence_report["topics"],
        "industry_knowledge_files": len(industry_knowledge_files),
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
