"""账号的「人名」：老板认的是「小李」「王店长」，不是登录名 xiaoli、laowang。

users.display_name(v65)可空；凡是把人名给人看的地方都走这里——
填了姓名用姓名，没填回退登录名。登录名只在登录、建账号、账号管理处单独显示。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from . import db

MAX_DISPLAY_NAME = 20
# 写进 SQL 的同一条回退规则(列带表别名时用 name_sql("u"))
_BAD_CHARS = re.compile(r"[<>&\"'\\\x00-\x1f\x7f]")


def name_sql(alias: str = "") -> str:
    """SELECT 里直接取显示名：COALESCE(NULLIF(TRIM(display_name),''),username)。"""
    prefix = f"{alias}." if alias else ""
    if alias and not alias.isalnum():
        raise ValueError("invalid alias")
    return (f"COALESCE(NULLIF(TRIM({prefix}display_name),''),"
            f"{prefix}username)")


def clean_display_name(raw: Any) -> str | None:
    """姓名入库前规整：去首尾空格、压掉多余空白；空 = 不填(None)。

    不合规抛 ValueError(大白话)，由路由转成 400。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("姓名格式不对")
    name = " ".join(raw.split())
    if not name:
        return None
    if len(name) > MAX_DISPLAY_NAME:
        raise ValueError(f"姓名最多 {MAX_DISPLAY_NAME} 个字")
    if _BAD_CHARS.search(name):
        raise ValueError("姓名不能含引号、尖括号等特殊符号")
    return name


def display_name(user: Mapping[str, Any] | None) -> str:
    """有姓名用姓名，否则回退登录名；都没有返回空串。"""
    if not user:
        return ""
    name = str(user.get("display_name") or "").strip()
    return name or str(user.get("username") or "")


def with_role(name: Any, role_label: Any) -> str:
    """带角色的写法「小李（店员）」；姓名里已含角色(「王店长」)就不重复。"""
    name = str(name or "").strip()
    role = str(role_label or "").strip()
    if not name:
        return ""
    if not role or role in name:
        return name
    return f"{name}（{role}）"


def names(tid: int, ids: Iterable[Any]) -> dict[int, str]:
    """本企业一批账号 id -> 显示名。跨企业/已删除的账号不在结果里。"""
    wanted: set[int] = set()
    for value in ids:
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            wanted.add(number)
    result: dict[int, str] = {}
    ordered = sorted(wanted)
    for start in range(0, len(ordered), 500):
        chunk = ordered[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        for row in db.q(
            f"SELECT id,{name_sql()} AS label FROM users "
            f"WHERE tenant_id=? AND id IN ({marks})",
            (int(tid), *chunk),
        ):
            result[int(row["id"])] = str(row.get("label") or "")
    return result


def names_and_logins(tid: int, ids: Iterable[Any]) -> tuple[dict[int, str], dict[int, str]]:
    """一次查询同时拿 (显示名, 登录名)；列表接口用它，不为登录名多查一遍。"""
    wanted = sorted({int(i) for i in ids if i})
    shown: dict[int, str] = {}
    logins: dict[int, str] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        for row in db.q(
            f"SELECT id,username,display_name FROM users "
            f"WHERE tenant_id=? AND id IN ({marks})",
            (int(tid), *chunk),
        ):
            shown[int(row["id"])] = display_name(row)
            logins[int(row["id"])] = str(row.get("username") or "")
    return shown, logins


def name_of(tid: int, uid: Any) -> str | None:
    """单个账号的显示名；查不到(跨企业/已删除)返回 None。"""
    try:
        number = int(uid or 0)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return names(tid, [number]).get(number)
