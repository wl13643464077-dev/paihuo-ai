"""Manual purchase-intent workflow for subscription sales.

This module records a customer's request and lets a platform root confirm an
offline payment.  It is deliberately not a payment gateway: no endpoint claims
that money moved, and only the root-only paid transition can call the existing
idempotent subscription ledger.
"""
from __future__ import annotations

import json
import logging
import re
import time

from . import billing, db, funnel, notify


log = logging.getLogger("purchases")
STATUSES = ("requested", "contacted", "lost", "paid")
MANAGED_TARGETS = {"contacted", "lost", "paid"}
SOURCES = {"promo", "login", "billing"}
_REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,159}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class PurchaseError(ValueError):
    pass


class PurchaseConflict(PurchaseError):
    pass


class PurchaseNotFound(PurchaseError):
    pass


class PurchaseForbidden(PurchaseError):
    pass


def catalog() -> dict:
    return billing.subscription_catalog()


def _request_key(value: str) -> str:
    key = str(value or "").strip()
    if not _REQUEST_KEY_RE.fullmatch(key):
        raise PurchaseError("缺少有效的购买申请号，请刷新页面后重试")
    return key


def _text(value, *, field: str, limit: int, required: bool = False) -> str:
    if value is not None and not isinstance(value, str):
        raise PurchaseError(f"{field}格式不对")
    clean = str(value or "").strip()
    if required and not clean:
        raise PurchaseError(f"{field}必填")
    if len(clean) > limit:
        raise PurchaseError(f"{field}不能超过 {limit} 个字")
    if _CONTROL_RE.search(clean):
        raise PurchaseError(f"{field}不能包含控制字符")
    return clean


def _source(value: str | None) -> str:
    clean = str(value or "billing").strip().lower()
    if clean not in SOURCES:
        raise PurchaseError("购买来源无效，请从套餐页面重新提交")
    return clean


def _actor(uid: int, tid: int) -> dict:
    row = db.one(
        "SELECT id,tenant_id,role,enabled FROM users WHERE id=?",
        (int(uid),),
    )
    if (
        not row
        or int(row["tenant_id"]) != int(tid)
        or not int(row.get("enabled") or 0)
    ):
        raise PurchaseForbidden("账号无权提交该企业的购买申请")
    if row["role"] not in {"root", "owner"}:
        raise PurchaseForbidden("仅企业主账号可以提交购买申请")
    tenant = db.one(
        "SELECT id FROM tenants WHERE id=? AND COALESCE(enabled,1)=1",
        (int(tid),),
    )
    if not tenant:
        raise PurchaseForbidden("企业账号已停用")
    return row


def _serialize(row: dict, *, admin: bool = False) -> dict:
    receipt = db.jloads(row.get("receipt_json"), {}) or {}
    public_receipt = {
        key: receipt[key]
        for key in ("points", "price", "expires")
        if key in receipt
    }
    status_messages = {
        "requested": "申请已提交，平台将尽快与您联系。",
        "contacted": "平台已联系您，请留意沟通消息。",
        "lost": "本次购买申请已结束，如仍有需要可重新提交。",
        "paid": "线下款项已确认，套餐和点数已经开通。",
    }
    item = {
        "id": int(row["id"]),
        "tenant_id": int(row["tenant_id"]),
        "created_by": int(row["created_by"]),
        "request_id": row["request_key"],
        "plan": row["plan_key"],
        "period": row["period_key"],
        "plan_name": row["plan_name"],
        "period_label": row["period_label"],
        "price": row["quoted_price"],
        "points": row["quoted_points"],
        "contact": row["contact"],
        "note": row.get("customer_note") or "",
        "status": row["status"],
        "status_message": status_messages.get(row["status"], "申请状态已更新。"),
        "contacted_at": row.get("contacted_at"),
        "lost_at": row.get("lost_at"),
        "paid_at": row.get("paid_at"),
        "receipt": public_receipt,
        "source": receipt.get("source") or "billing",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "payment_mode": "offline_confirmation",
    }
    if admin:
        item.update({
            "handler_note": row.get("handler_note") or "",
            "handled_by": row.get("handled_by"),
        })
    return item


def _same_request(
    row: dict,
    uid: int,
    quote: dict,
    contact: str,
    note: str,
    source: str,
) -> bool:
    receipt = db.jloads(row.get("receipt_json"), {}) or {}
    return bool(
        int(row["created_by"]) == int(uid)
        and row["plan_key"] == quote["plan"]
        and row["period_key"] == quote["period"]
        and row["contact"] == contact
        and (row.get("customer_note") or "") == note
        and (receipt.get("source") or "billing") == source
    )


def _notify_platform_roots(payload: dict) -> None:
    """Best-effort targeted notice; a notification outage cannot undo a lead."""
    try:
        roots = db.q(
            "SELECT id,tenant_id FROM users WHERE role='root' "
            "AND COALESCE(enabled,1)=1 ORDER BY id"
        )
    except Exception as exc:
        log.error(
            "purchase root notification lookup failed error_type=%s",
            type(exc).__name__,
        )
        return
    for root in roots:
        notify.record(
            int(root["tenant_id"]),
            "purchase_requested",
            payload,
            target_user_id=int(root["id"]),
        )


def create_intent(
    tid: int,
    uid: int,
    *,
    request_key: str,
    plan_key: str,
    period_key: str,
    contact: str,
    note: str = "",
    source: str = "billing",
) -> dict:
    """Create or replay one customer-owned purchase request."""
    _actor(uid, tid)
    key = _request_key(request_key)
    contact = _text(contact, field="联系方式", limit=80, required=True)
    note = _text(note, field="购买备注", limit=300)
    source = _source(source)
    quote = billing.subscription_quote(
        str(plan_key or "").strip(),
        str(period_key or "").strip(),
    )
    now = time.time()
    initial_receipt = json.dumps(
        {"source": source},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    created = False
    with db.atomic() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO purchase_intent(
              tenant_id,created_by,request_key,plan_key,period_key,
              plan_name,period_label,quoted_price,quoted_points,
              contact,customer_note,receipt_json,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'requested',?,?)
            """,
            (
                int(tid),
                int(uid),
                key,
                quote["plan"],
                quote["period"],
                quote["plan_name"],
                quote["period_label"],
                quote["price"],
                quote["points"],
                contact,
                note,
                initial_receipt,
                now,
                now,
            ),
        )
        created = cursor.rowcount == 1
        row = connection.execute(
            "SELECT * FROM purchase_intent WHERE tenant_id=? AND request_key=?",
            (int(tid), key),
        ).fetchone()
        if not row:
            raise PurchaseConflict("购买申请号冲突，请刷新页面后重试")
        item = dict(row)
        if not created and not _same_request(
            item, uid, quote, contact, note, source
        ):
            raise PurchaseConflict("购买申请号已用于其他申请，请刷新页面后重试")

    if created:
        funnel.record_safe(
            "purchase_requested",
            source,
            tenant_id=int(tid),
            actor_key=f"purchase-intent:{item['id']}",
            unique_only=True,
        )
        summary = (
            f"企业 #{int(tid)} 申请{quote['plan_name']}·"
            f"{quote['period_label']}，请线下联系确认。"
        )
        _notify_platform_roots({
            "intent_id": int(item["id"]),
            "title": f"{quote['plan_name']}·{quote['period_label']}",
            "summary": summary,
        })
    return {"created": created, "item": _serialize(item)}


def _status(value: str, *, optional: bool = False) -> str | None:
    clean = str(value or "").strip().lower()
    if optional and not clean:
        return None
    if clean not in STATUSES:
        raise PurchaseError("购买申请状态无效")
    return clean


def _plan_filter(value: str | None) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    valid = {plan["key"] for plan in billing.PLANS}
    if clean not in valid:
        raise PurchaseError("套餐筛选条件无效")
    return clean


def _period_filter(value: str | None) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    valid = {period["key"] for period in billing.PERIODS}
    if clean not in valid:
        raise PurchaseError("周期筛选条件无效")
    return clean


def _page(limit: int, offset: int) -> tuple[int, int]:
    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise PurchaseError("分页参数无效") from exc
    if not 1 <= limit <= 100:
        raise PurchaseError("limit 必须在 1 到 100 之间")
    if not 0 <= offset <= 1_000_000:
        raise PurchaseError("offset 必须在 0 到 1000000 之间")
    return limit, offset


def list_own(
    tid: int,
    uid: int,
    *,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    _actor(uid, tid)
    wanted = _status(status, optional=True)
    limit, offset = _page(limit, offset)
    where = ["tenant_id=?", "created_by=?"]
    args: list = [int(tid), int(uid)]
    if wanted:
        where.append("status=?")
        args.append(wanted)
    clause = " AND ".join(where)
    total = db.one(
        f"SELECT COUNT(*) n FROM purchase_intent WHERE {clause}",
        tuple(args),
    )["n"]
    rows = db.q(
        f"SELECT * FROM purchase_intent WHERE {clause} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(args + [limit, offset]),
    )
    return {
        "items": [_serialize(row) for row in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


def _admin_where(
    *,
    scope_tid: int | None,
    tenant_id: int | None,
    status: str | None,
    plan: str | None,
    period: str | None,
) -> tuple[str, list]:
    where = ["1=1"]
    args: list = []
    try:
        requested_tid = int(tenant_id) if tenant_id not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise PurchaseError("企业筛选条件无效") from exc
    if scope_tid is not None:
        if requested_tid is not None and requested_tid != int(scope_tid):
            raise PurchaseForbidden("企业主只能查看本企业购买申请")
        where.append("tenant_id=?")
        args.append(int(scope_tid))
    elif requested_tid is not None:
        if requested_tid < 1:
            raise PurchaseError("企业筛选条件无效")
        where.append("tenant_id=?")
        args.append(requested_tid)
    wanted = _status(status, optional=True)
    wanted_plan = _plan_filter(plan)
    wanted_period = _period_filter(period)
    if wanted:
        where.append("status=?")
        args.append(wanted)
    if wanted_plan:
        where.append("plan_key=?")
        args.append(wanted_plan)
    if wanted_period:
        where.append("period_key=?")
        args.append(wanted_period)
    return " AND ".join(where), args


def list_admin(
    *,
    scope_tid: int | None,
    tenant_id: int | None = None,
    status: str | None = None,
    plan: str | None = None,
    period: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    limit, offset = _page(limit, offset)
    clause, args = _admin_where(
        scope_tid=scope_tid,
        tenant_id=tenant_id,
        status=status,
        plan=plan,
        period=period,
    )
    total = db.one(
        f"SELECT COUNT(*) n FROM purchase_intent WHERE {clause}",
        tuple(args),
    )["n"]
    rows = db.q(
        f"SELECT * FROM purchase_intent WHERE {clause} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(args + [limit, offset]),
    )
    return {
        "items": [_serialize(row, admin=True) for row in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


def stats(
    *,
    scope_tid: int | None,
    tenant_id: int | None = None,
    status: str | None = None,
    plan: str | None = None,
    period: str | None = None,
) -> dict:
    clause, args = _admin_where(
        scope_tid=scope_tid,
        tenant_id=tenant_id,
        status=status,
        plan=plan,
        period=period,
    )
    rows = db.q(
        "SELECT status,COUNT(*) n,COALESCE(SUM(quoted_price),0) amount "
        f"FROM purchase_intent WHERE {clause} GROUP BY status",
        tuple(args),
    )
    by_status = {
        key: {"count": 0, "amount": 0}
        for key in STATUSES
    }
    for row in rows:
        by_status[row["status"]] = {
            "count": int(row.get("n") or 0),
            "amount": row.get("amount") or 0,
        }
    return {
        "total": sum(item["count"] for item in by_status.values()),
        "quoted_amount": sum(item["amount"] for item in by_status.values()),
        "paid_amount": by_status["paid"]["amount"],
        "by_status": by_status,
        "payment_mode": "offline_confirmation",
    }


def _root_actor(uid: int) -> dict:
    row = db.one(
        "SELECT id,tenant_id,role,enabled FROM users WHERE id=?",
        (int(uid),),
    )
    if not row or row["role"] != "root" or not int(row.get("enabled") or 0):
        raise PurchaseForbidden("只有平台 root 可以更新购买申请")
    return row


def _quote_still_current(row: dict) -> dict:
    try:
        quote = billing.subscription_quote(row["plan_key"], row["period_key"])
    except ValueError as exc:
        raise PurchaseConflict("套餐已下架，请客户重新提交购买申请") from exc
    if (
        float(row["quoted_price"]) != float(quote["price"])
        or float(row["quoted_points"]) != float(quote["points"])
        or row["plan_name"] != quote["plan_name"]
        or row["period_label"] != quote["period_label"]
    ):
        raise PurchaseConflict("套餐价格或权益已更新，请客户重新提交购买申请")
    return quote


def transition(
    intent_id: int,
    *,
    expected_status: str,
    target_status: str,
    actor_id: int,
    note: str = "",
) -> dict:
    """CAS one state change; paid also atomically opens the subscription."""
    _root_actor(actor_id)
    expected = _status(expected_status)
    target = _status(target_status)
    if target not in MANAGED_TARGETS:
        raise PurchaseError("管理端只能标记已联系、已流失或已到账")
    note = _text(note, field="跟进备注", limit=300)
    if target == "lost" and not note:
        raise PurchaseError("标记流失时请填写原因")
    now = time.time()
    changed = False
    with db.atomic() as connection:
        stored = connection.execute(
            "SELECT * FROM purchase_intent WHERE id=?",
            (int(intent_id),),
        ).fetchone()
        if not stored:
            raise PurchaseNotFound("没有找到这条购买申请")
        row = dict(stored)
        current = row["status"]
        if current == target:
            return {"changed": False, "item": _serialize(row, admin=True)}
        if current in {"lost", "paid"}:
            raise PurchaseConflict("这条购买申请已经结束，不能再次变更")
        if current != expected:
            raise PurchaseConflict(
                f"申请状态已从 {expected} 变为 {current}，请刷新后重试"
            )
        if target == "contacted" and current != "requested":
            raise PurchaseConflict("只有待联系申请可以标记为已联系")

        updates = {
            "status": target,
            "handler_note": note or None,
            "handled_by": int(actor_id),
            "updated_at": now,
        }
        if target == "contacted":
            updates["contacted_at"] = now
        elif target == "lost":
            updates["lost_at"] = now
        else:
            quote = _quote_still_current(row)
            operation_key = f"purchase-intent:{int(intent_id)}"
            try:
                receipt = billing.subscribe(
                    int(row["tenant_id"]),
                    quote["plan"],
                    quote["period"],
                    op_key=operation_key,
                )
            except ValueError as exc:
                raise PurchaseConflict(
                    "套餐开通单发生冲突，请核对后重试"
                ) from exc
            if (
                float(receipt["price"]) != float(row["quoted_price"])
                or float(receipt["points"]) != float(row["quoted_points"])
            ):
                raise PurchaseConflict("套餐开通回执与申请报价不一致")
            existing_receipt = db.jloads(row.get("receipt_json"), {}) or {}
            updates.update({
                "paid_at": now,
                "subscription_op_key": operation_key,
                "receipt_json": json.dumps(
                    {**existing_receipt, **receipt},
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            })

        sets = ",".join(f"{key}=?" for key in updates)
        cursor = connection.execute(
            f"UPDATE purchase_intent SET {sets} "
            "WHERE id=? AND status=?",
            tuple(updates.values()) + (int(intent_id), expected),
        )
        if cursor.rowcount != 1:
            raise PurchaseConflict("申请状态已变化，请刷新后重试")
        fresh = connection.execute(
            "SELECT * FROM purchase_intent WHERE id=?",
            (int(intent_id),),
        ).fetchone()
        row = dict(fresh)
        changed = True

    if changed:
        labels = {
            "contacted": "平台已联系您，请留意沟通消息。",
            "lost": "本次购买申请已结束，如仍有需要可重新提交。",
            "paid": "线下款项已确认，套餐和点数已经开通。",
        }
        notify.record(
            int(row["tenant_id"]),
            f"purchase_{target}",
            {
                "intent_id": int(row["id"]),
                "title": f"{row['plan_name']}·{row['period_label']}",
                "summary": labels[target],
            },
            target_user_id=int(row["created_by"]),
        )
        if target == "paid":
            funnel.record_safe(
                "purchase_paid",
                "subscription",
                tenant_id=int(row["tenant_id"]),
                actor_key=f"purchase-intent:{row['id']}",
                unique_only=True,
            )
    return {"changed": changed, "item": _serialize(row, admin=True)}
