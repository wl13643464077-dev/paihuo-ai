"""Purchase workflow for subscription sales.

Two ways to pay, one way to open a plan:

* 购买意向(线下)：客户提交申请，平台 root 核实线下到账后标记“已到账”；
* 微信支付 Native 扫码(可选，root 配好商户号后才出现)：服务端按报价下单，
  验签过的回调或查单确认已付后自动开通。

两条路最终都走 ``activate_paid_subscription``：按服务端报价核对后调用
billing.subscribe 的幂等开通单，同一笔订单/申请无论重放多少次只开通一次。
前端传来的价格一律不信。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import re
import secrets
import time

from . import billing, db, funnel, notify, qrsvg, wxpay


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
    result = billing.subscription_catalog()
    result["point_examples"] = billing.point_examples()
    online = wxpay.is_enabled()
    result["online_pay"] = {"wxpay": online}
    if online:
        result["online_payment_notice"] = (
            "可以微信扫码付款，付款成功立即开通；也可以提交购买意向，由平台联系。"
        )
    return result


def reference_catalog() -> dict:
    """写进宣传页/套餐页的兜底参考价：接口慢或失败时照样能看到价格。

    只取代码里的默认套餐与默认价目，不读数据库；tests 会核对页面里内置的
    那份与这里完全一致，改价后记得运行 tools/sync_plan_reference.py。
    """
    return {
        "plans": [
            {key: plan[key] for key in ("key", "name", "points", "price", "sale", "desc")}
            for plan in billing.PLANS
        ],
        "periods": [
            {key: period[key] for key in ("key", "label", "months", "discount")}
            for period in billing.PERIODS
        ],
        "quotes": [
            billing.subscription_quote(plan["key"], period["key"])
            for plan in billing.PLANS
            for period in billing.PERIODS
        ],
        "point_examples": billing.point_examples(billing.DEFAULT_PRICES),
    }


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
        "status_message": (
            "微信付款已到账，套餐和点数已经开通。"
            if row["status"] == "paid" and receipt.get("paid_via") == "wxpay"
            else status_messages.get(row["status"], "申请状态已更新。")
        ),
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


def activate_paid_subscription(row: dict, *, op_key: str) -> dict:
    """确认钱已到账后开通套餐并发放点数(人工确认到账与在线支付共用)。

    ``row`` 需要 tenant_id/plan_key/period_key/plan_name/period_label/
    quoted_price/quoted_points：先核对仍是当前服务端报价，再用 ``op_key``
    调 billing.subscribe；同一个 op_key 重放只会拿回原回执，不会重复发点。
    调用方应在自己的 db.atomic() 里调用，使开通与业务状态同生共死。
    """
    quote = _quote_still_current(row)
    try:
        receipt = billing.subscribe(
            int(row["tenant_id"]),
            quote["plan"],
            quote["period"],
            op_key=op_key,
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
    return receipt


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
            operation_key = f"purchase-intent:{int(intent_id)}"
            receipt = activate_paid_subscription(row, op_key=operation_key)
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


# ================================================================
# 微信支付 Native 扫码订单
# ================================================================

PAY_ORDER_TTL_SECONDS = 2 * 3600
PAY_QUERY_MIN_INTERVAL = 5
PAY_ORDER_HOURLY_LIMIT = 20
PAY_SWEEP_INTERVAL_SECONDS = 300
PAY_CLOSE_REASON_EXPIRED = "超过 2 小时未付款，已自动关闭"
_BEIJING = _dt.timezone(_dt.timedelta(hours=8))
_ORDER_NO_RE = re.compile(r"^PH[0-9A-Z]{26}$")
_ALERTED_MISMATCH: set[str] = set()


class PaymentUnavailable(PurchaseError):
    """在线支付没开通：前端应回到“提交购买意向”。"""


class PaymentGatewayError(PurchaseError):
    """微信支付接口暂时不可用。"""


class PayMismatch(PurchaseConflict):
    """回调/查单的金额、商户或订单号与本地订单对不上，拒绝入账。"""


def _new_out_trade_no(now: float) -> str:
    stamp = _dt.datetime.fromtimestamp(now, _BEIJING).strftime("%Y%m%d%H%M%S")
    return f"PH{stamp}{secrets.token_hex(6).upper()}"


def _order_no(value) -> str:
    clean = str(value or "").strip()
    if not _ORDER_NO_RE.fullmatch(clean):
        raise PurchaseNotFound("没有找到这笔付款订单")
    return clean


def _order_quote_row(row: dict) -> dict:
    """把支付订单换成 activate_paid_subscription 需要的报价行(分→元)。"""
    return {
        "tenant_id": row["tenant_id"],
        "plan_key": row["plan_key"],
        "period_key": row["period_key"],
        "plan_name": row["plan_name"],
        "period_label": row["period_label"],
        "quoted_price": int(row["amount_fen"]) / 100,
        "quoted_points": row["quoted_points"],
    }


def _serialize_pay_order(
    row: dict,
    *,
    admin: bool = False,
    with_qr: bool = False,
    now: float | None = None,
) -> dict:
    current = time.time() if now is None else float(now)
    status = row["status"]
    activated = bool(row.get("subscription_op_key"))
    if status == "paid":
        message = (
            "付款成功，套餐和点数已经开通。"
            if activated
            else "已收到付款，开通时遇到问题，平台会尽快人工处理，无需重复付款。"
        )
    else:
        message = {
            "created": "请用微信“扫一扫”付款，付完自动开通，不用再找人确认。",
            "closed": "这笔订单已关闭，没有扣款。如需购买请重新下单。",
            "refunded": "这笔订单已退款。",
        }.get(status, "订单状态已更新。")
    payable = bool(
        status == "created"
        and row.get("code_url")
        and float(row["expires_at"]) > current
    )
    amount_fen = int(row["amount_fen"])
    item = {
        "order_no": row["out_trade_no"],
        "channel": row.get("channel") or "wxpay_native",
        "plan": row["plan_key"],
        "period": row["period_key"],
        "plan_name": row["plan_name"],
        "period_label": row["period_label"],
        "amount_fen": amount_fen,
        "amount": amount_fen / 100,
        "points": row["quoted_points"],
        "status": status,
        "status_message": message,
        "activated": activated,
        "expires_at": row["expires_at"],
        "paid_at": row.get("paid_at"),
        "closed_at": row.get("closed_at"),
        "created_at": row.get("created_at"),
        "code_url": row["code_url"] if payable else "",
    }
    if with_qr and payable:
        item["qr_svg"] = qrsvg.svg(row["code_url"])
    if admin:
        item.update({
            "id": int(row["id"]),
            "tenant_id": int(row["tenant_id"]),
            "created_by": int(row["created_by"]),
            "transaction_id": row.get("transaction_id") or "",
            "activation_error": row.get("activation_error") or "",
            "close_reason": row.get("close_reason") or "",
        })
    return item


def _load_pay_order(out_trade_no: str) -> dict | None:
    return db.one("SELECT * FROM pay_order WHERE out_trade_no=?", (out_trade_no,))


def _validate_transaction(config: dict, row: dict, transaction: dict) -> str:
    """核对一笔微信交易确实是这张订单的全额付款，返回微信订单号。"""
    if not isinstance(transaction, dict):
        raise PayMismatch("交易内容无效")
    if str(transaction.get("out_trade_no") or "") != row["out_trade_no"]:
        raise PayMismatch("商户订单号不一致")
    if str(transaction.get("mchid") or "") != str(config.get("mchid") or ""):
        raise PayMismatch("商户号不一致")
    if str(transaction.get("appid") or "") != str(config.get("appid") or ""):
        raise PayMismatch("AppID 不一致")
    if transaction.get("trade_state") != "SUCCESS":
        raise PayMismatch("交易尚未成功")
    amount = transaction.get("amount") if isinstance(transaction.get("amount"), dict) else {}
    try:
        total = int(amount.get("total"))
    except (TypeError, ValueError) as exc:
        raise PayMismatch("交易金额无效") from exc
    if isinstance(amount.get("total"), bool) or total != int(row["amount_fen"]):
        raise PayMismatch("付款金额与订单金额不一致")
    currency = str(amount.get("currency") or "CNY")
    if currency != "CNY":
        raise PayMismatch("付款币种不对")
    transaction_id = str(transaction.get("transaction_id") or "").strip()
    if not transaction_id or len(transaction_id) > 64:
        raise PayMismatch("缺少微信支付订单号")
    return transaction_id


def _alert_platform(summary: str) -> None:
    try:
        roots = db.q(
            "SELECT id,tenant_id FROM users WHERE role='root' "
            "AND COALESCE(enabled,1)=1 ORDER BY id"
        )
        for root in roots:
            notify.record(
                int(root["tenant_id"]),
                "platform_alert",
                {"title": "微信支付需要人工处理", "summary": summary[:230]},
                target_user_id=int(root["id"]),
            )
    except Exception as exc:  # noqa: BLE001 - 提醒失败不能影响入账结果
        log.error("pay alert failed error_type=%s", type(exc).__name__)


def settle_paid_order(
    out_trade_no: str,
    *,
    transaction: dict,
    config: dict,
    source: str,
    notify_digest: dict | None = None,
    now: float | None = None,
) -> dict:
    """把一笔已核实的微信付款落账：订单→paid，并幂等开通套餐。

    同一订单无论回调几次、查单几次，只有第一次会开通；之后直接返回。
    金额/商户/订单号对不上抛 PayMismatch，且不改任何状态。
    """
    current = time.time() if now is None else float(now)
    digest_json = (
        json.dumps(notify_digest, ensure_ascii=True, separators=(",", ":"))
        if notify_digest
        else None
    )
    changed = False
    with db.atomic() as connection:
        stored = connection.execute(
            "SELECT * FROM pay_order WHERE out_trade_no=?",
            (str(out_trade_no or ""),),
        ).fetchone()
        if not stored:
            raise PurchaseNotFound("没有找到这笔付款订单")
        row = dict(stored)
        transaction_id = _validate_transaction(config, row, transaction)
        if row["status"] == "paid":
            if row.get("transaction_id") != transaction_id:
                log.error("pay order paid twice order=%s", row["out_trade_no"])
            return {"changed": False, "item": row}
        if row["status"] not in {"created", "closed"}:
            raise PurchaseConflict("这笔订单已退款，不能再次入账")
        # 已关闭的订单若收到真实的成功付款(关单前最后一刻付的)，钱已到账，照样开通。
        operation_key = f"wxpay:{row['out_trade_no']}"
        receipt = None
        activation_error = None
        try:
            with db.atomic():
                receipt = activate_paid_subscription(
                    _order_quote_row(row), op_key=operation_key
                )
        except PurchaseConflict as exc:
            activation_error = str(exc)[:200]
        receipt_json = (
            json.dumps(
                {**receipt, "source": source, "channel": "wxpay"},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            if receipt
            else None
        )
        cursor = connection.execute(
            "UPDATE pay_order SET status='paid',transaction_id=?,paid_at=?,"
            "subscription_op_key=?,activation_error=?,receipt_json=?,"
            "notify_digest=COALESCE(?,notify_digest),updated_at=? "
            "WHERE id=? AND status=?",
            (
                transaction_id,
                current,
                operation_key if receipt else None,
                activation_error,
                receipt_json,
                digest_json,
                current,
                int(row["id"]),
                row["status"],
            ),
        )
        if cursor.rowcount != 1:
            raise PurchaseConflict("订单状态已变化，请刷新后重试")
        if receipt and row.get("intent_id"):
            # 客户之前提交过同套餐的购买意向：一并标记已到账，平台不用再跟进。
            intent = connection.execute(
                "SELECT receipt_json FROM purchase_intent WHERE id=? AND tenant_id=? "
                "AND status IN ('requested','contacted') AND subscription_op_key IS NULL",
                (int(row["intent_id"]), int(row["tenant_id"])),
            ).fetchone()
            if intent:
                merged = {
                    **(db.jloads(intent["receipt_json"], {}) or {}),
                    **receipt,
                    "paid_via": "wxpay",
                }
                connection.execute(
                    "UPDATE purchase_intent SET status='paid',paid_at=?,"
                    "subscription_op_key=?,receipt_json=?,handler_note=?,updated_at=? "
                    "WHERE id=? AND status IN ('requested','contacted') "
                    "AND subscription_op_key IS NULL",
                    (
                        current,
                        operation_key,
                        json.dumps(merged, ensure_ascii=True, separators=(",", ":")),
                        f"微信支付自动到账(订单 {row['out_trade_no']})",
                        current,
                        int(row["intent_id"]),
                    ),
                )
        fresh = connection.execute(
            "SELECT * FROM pay_order WHERE id=?", (int(row["id"]),)
        ).fetchone()
        row = dict(fresh)
        changed = True

    if changed:
        amount = int(row["amount_fen"]) / 100
        title = f"{row['plan_name']}·{row['period_label']}"
        if row.get("subscription_op_key"):
            summary = (
                f"微信付款 ¥{amount:g} 已到账，{title}和 "
                f"{row['quoted_points']:g} 点已开通。"
            )
        else:
            summary = f"微信付款 ¥{amount:g} 已到账，开通遇到问题，平台会尽快人工处理。"
            _alert_platform(
                f"企业 #{int(row['tenant_id'])} 订单 {row['out_trade_no']} "
                f"已付款但自动开通失败：{row.get('activation_error') or ''}"
            )
        notify.record(
            int(row["tenant_id"]),
            "purchase_paid",
            {"title": title, "summary": summary},
            target_user_id=int(row["created_by"]),
        )
        funnel.record_safe(
            "purchase_paid",
            "subscription",
            tenant_id=int(row["tenant_id"]),
            actor_key=f"pay-order:{row['id']}",
            unique_only=True,
        )
    return {"changed": changed, "item": row}


def close_pay_order(order_id: int, *, reason: str, now: float | None = None) -> bool:
    """只关“待付款”的订单；已付款的绝不会被关。"""
    current = time.time() if now is None else float(now)
    return db.execute(
        "UPDATE pay_order SET status='closed',closed_at=?,close_reason=?,"
        "updated_at=? WHERE id=? AND status='created'",
        (current, reason[:120], current, int(order_id)),
    ) == 1


def _sync_with_remote(row: dict, config: dict, *, now: float, expired: bool) -> None:
    """查单并按结果落账或关单；网络失败时什么都不改。"""
    try:
        transaction = wxpay.query_order(config, row["out_trade_no"])
    except wxpay.WxPayError as exc:
        log.warning(
            "wxpay query failed order=%s error=%s", row["out_trade_no"], str(exc)[:120]
        )
        if expired:
            # 下单时已把 time_expire 交给微信，过期后微信不再收款；
            # 就算之后才收到当时的成功回调，settle_paid_order 也会照常开通。
            close_pay_order(row["id"], reason=PAY_CLOSE_REASON_EXPIRED, now=now)
        return
    state = str(transaction.get("trade_state") or "")
    if state == "SUCCESS":
        try:
            settle_paid_order(
                row["out_trade_no"],
                transaction=transaction,
                config=config,
                source="query",
                now=now,
            )
        except PayMismatch as exc:
            _report_mismatch(row["out_trade_no"], str(exc))
        return
    if state in {"CLOSED", "REVOKED", "PAYERROR"}:
        close_pay_order(row["id"], reason="微信侧订单已关闭或支付失败", now=now)
        return
    if expired:
        try:
            wxpay.close_order(config, row["out_trade_no"])
        except wxpay.WxPayError as exc:
            log.warning(
                "wxpay close failed order=%s error=%s", row["out_trade_no"], str(exc)[:120]
            )
        close_pay_order(row["id"], reason=PAY_CLOSE_REASON_EXPIRED, now=now)


def _report_mismatch(out_trade_no: str, reason: str) -> None:
    log.error("wxpay transaction mismatch order=%s reason=%s", out_trade_no, reason)
    key = f"{out_trade_no}:{reason}"
    if key in _ALERTED_MISMATCH:
        return
    _ALERTED_MISMATCH.add(key)
    _alert_platform(f"订单 {out_trade_no} 的付款信息与订单不一致({reason})，已拒绝自动开通，请核对。")


def sweep_expired_pay_orders(
    now: float | None = None,
    *,
    config: dict | None = None,
    remote: bool = True,
    limit: int = 50,
) -> dict:
    """关掉超过 2 小时未付的订单；关之前先查一次单，防止漏掉刚付的钱。"""
    current = time.time() if now is None else float(now)
    rows = db.q(
        "SELECT * FROM pay_order WHERE status='created' AND expires_at<=? "
        "ORDER BY expires_at LIMIT ?",
        (current, int(limit)),
    )
    if not rows:
        return {"checked": 0, "closed": 0, "paid": 0}
    if remote:
        config = config if config is not None else wxpay.load_config()
    can_query = bool(remote and config and not wxpay.missing_fields(config))
    for row in rows:
        if can_query:
            _sync_with_remote(row, config, now=current, expired=True)
        else:
            close_pay_order(row["id"], reason=PAY_CLOSE_REASON_EXPIRED, now=current)
    ids = tuple(int(row["id"]) for row in rows)
    marks = ",".join("?" for _ in ids)
    after = db.q(f"SELECT status FROM pay_order WHERE id IN ({marks})", ids)
    return {
        "checked": len(rows),
        "closed": sum(1 for row in after if row["status"] == "closed"),
        "paid": sum(1 for row in after if row["status"] == "paid"),
    }


async def pay_order_loop(interval: float = PAY_SWEEP_INTERVAL_SECONDS) -> None:
    """常驻循环：定期关掉过期未付订单；任何异常只记日志，循环不退出。"""
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(sweep_expired_pay_orders)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("pay order sweep failed error_type=%s", type(exc).__name__)


def _linked_intent_id(connection, tid: int, uid: int, quote: dict) -> int | None:
    row = connection.execute(
        "SELECT id FROM purchase_intent WHERE tenant_id=? AND created_by=? "
        "AND plan_key=? AND period_key=? AND status IN ('requested','contacted') "
        "AND quoted_price=? AND quoted_points=? ORDER BY id DESC LIMIT 1",
        (
            int(tid), int(uid), quote["plan"], quote["period"],
            quote["price"], quote["points"],
        ),
    ).fetchone()
    return int(row["id"]) if row else None


def create_wxpay_order(
    tid: int,
    uid: int,
    *,
    plan_key: str,
    period_key: str,
    now: float | None = None,
) -> dict:
    """老板选好套餐和周期后下单，返回付款二维码。金额只按服务端报价计算。"""
    _actor(uid, tid)
    if int(tid) == 1:
        raise PurchaseForbidden("平台自有账号不需要购买套餐")
    config = wxpay.load_config()
    if not wxpay.config_ready(config):
        raise PaymentUnavailable("在线支付暂未开通，请提交购买意向，平台会联系您")
    try:
        quote = billing.subscription_quote(
            str(plan_key or "").strip(), str(period_key or "").strip()
        )
    except ValueError as exc:
        raise PurchaseError("套餐或周期无效，请刷新页面后重试") from exc
    amount_fen = int(round(float(quote["price"]) * 100))
    if amount_fen <= 0:
        raise PurchaseError("该套餐无需付款")
    current = time.time() if now is None else float(now)
    expires_at = current + PAY_ORDER_TTL_SECONDS
    with db.atomic() as connection:
        existing = connection.execute(
            "SELECT * FROM pay_order WHERE tenant_id=? AND created_by=? "
            "AND plan_key=? AND period_key=? AND amount_fen=? AND status='created' "
            "AND expires_at>? ORDER BY id DESC LIMIT 1",
            (
                int(tid), int(uid), quote["plan"], quote["period"], amount_fen,
                current + 10 * 60,
            ),
        ).fetchone()
        if existing:
            row = dict(existing)
            if row.get("code_url"):
                # 同一套餐还有没过期的付款码：直接复用，避免重复下单。
                return {
                    "created": False,
                    "item": _serialize_pay_order(row, with_qr=True, now=current),
                }
            if float(row.get("created_at") or 0) > current - 30:
                raise PurchaseConflict("付款码正在生成，请稍等几秒再点")
            connection.execute(
                "UPDATE pay_order SET status='closed',closed_at=?,close_reason=?,"
                "updated_at=? WHERE id=? AND status='created'",
                (current, "付款码生成失败", current, int(row["id"])),
            )
        recent = connection.execute(
            "SELECT COUNT(*) FROM pay_order WHERE tenant_id=? AND created_at>?",
            (int(tid), current - 3600),
        ).fetchone()[0]
        if int(recent or 0) >= PAY_ORDER_HOURLY_LIMIT:
            raise PurchaseConflict("下单太频繁了，请稍后再试")
        out_trade_no = _new_out_trade_no(current)
        cursor = connection.execute(
            "INSERT INTO pay_order(tenant_id,created_by,channel,intent_id,plan_key,"
            "period_key,plan_name,period_label,quoted_points,amount_fen,"
            "out_trade_no,status,expires_at,created_at,updated_at) "
            "VALUES(?,?,'wxpay_native',?,?,?,?,?,?,?,?,'created',?,?,?)",
            (
                int(tid),
                int(uid),
                _linked_intent_id(connection, tid, uid, quote),
                quote["plan"],
                quote["period"],
                quote["plan_name"],
                quote["period_label"],
                quote["points"],
                amount_fen,
                out_trade_no,
                expires_at,
                current,
                current,
            ),
        )
        order_id = int(cursor.lastrowid)
    try:
        code_url = wxpay.native_order(
            config,
            out_trade_no=out_trade_no,
            description=f"派活{quote['plan_name']}-{quote['period_label']}",
            amount_fen=amount_fen,
            time_expire=expires_at,
            attach=f"t{int(tid)}",
        )
    except wxpay.WxPayError as exc:
        log.error("wxpay native order failed order=%s error=%s", out_trade_no, str(exc)[:160])
        close_pay_order(order_id, reason="微信下单失败", now=current)
        raise PaymentGatewayError(
            "微信支付暂时连不上，请稍后再试，或先提交购买意向"
        ) from exc
    db.execute(
        "UPDATE pay_order SET code_url=?,updated_at=? WHERE id=? AND status='created'",
        (code_url, time.time(), order_id),
    )
    row = db.one("SELECT * FROM pay_order WHERE id=?", (order_id,))
    return {"created": True, "item": _serialize_pay_order(row, with_qr=True, now=current)}


def pay_order_status(
    tid: int,
    uid: int,
    order_no: str,
    *,
    now: float | None = None,
) -> dict:
    """前端轮询用：必要时查一次单(至多 5 秒一次)，过期的顺手关掉。"""
    _actor(uid, tid)
    number = _order_no(order_no)
    row = db.one(
        "SELECT * FROM pay_order WHERE out_trade_no=? AND tenant_id=?",
        (number, int(tid)),
    )
    if not row:
        raise PurchaseNotFound("没有找到这笔付款订单")
    current = time.time() if now is None else float(now)
    if row["status"] == "created":
        expired = float(row["expires_at"]) <= current
        claimed = db.execute(
            "UPDATE pay_order SET last_query_at=? WHERE id=? AND status='created' "
            "AND (last_query_at IS NULL OR last_query_at<=?)",
            (current, int(row["id"]), current - PAY_QUERY_MIN_INTERVAL),
        ) == 1
        if claimed and (expired or row.get("code_url")):
            config = wxpay.load_config()
            if not wxpay.missing_fields(config):
                _sync_with_remote(row, config, now=current, expired=expired)
            elif expired:
                close_pay_order(row["id"], reason=PAY_CLOSE_REASON_EXPIRED, now=current)
        elif expired and not row.get("code_url"):
            close_pay_order(row["id"], reason=PAY_CLOSE_REASON_EXPIRED, now=current)
        row = db.one("SELECT * FROM pay_order WHERE id=?", (int(row["id"]),))
    return {"item": _serialize_pay_order(row, now=current)}


def list_pay_orders_admin(
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    limit, offset = _page(limit, offset)
    where, args = "1=1", []
    wanted = str(status or "").strip()
    if wanted:
        if wanted not in {"created", "paid", "closed", "refunded"}:
            raise PurchaseError("订单状态筛选无效")
        where, args = "status=?", [wanted]
    total = db.one(
        f"SELECT COUNT(*) n FROM pay_order WHERE {where}", tuple(args)
    )["n"]
    rows = db.q(
        f"SELECT * FROM pay_order WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(args + [limit, offset]),
    )
    paid = db.one(
        "SELECT COUNT(*) n,COALESCE(SUM(amount_fen),0) fen FROM pay_order "
        "WHERE status='paid'"
    )
    return {
        "items": [_serialize_pay_order(row, admin=True) for row in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "paid_count": int(paid["n"] or 0),
        "paid_amount": int(paid["fen"] or 0) / 100,
    }


def _notify_reply(status: int, ok: bool, message: str) -> tuple[int, dict]:
    return status, {"code": "SUCCESS" if ok else "FAIL", "message": message}


def handle_wxpay_notify(headers, body: bytes, *, now: float | None = None) -> tuple[int, dict]:
    """处理微信支付结果通知，返回 (HTTP 状态码, 应答 JSON)。

    - 验签失败：401，不改任何状态；
    - 金额/商户/订单号不一致：400 拒绝，不改状态；
    - 重复通知：幂等，直接 200 成功。
    """
    raw = body if isinstance(body, bytes) else str(body or "").encode("utf-8")
    config = wxpay.load_config()
    if wxpay.missing_fields(config):
        return _notify_reply(503, False, "支付配置未就绪")
    try:
        wxpay.verify_headers(config, headers, raw, now=now)
    except wxpay.WxPaySignatureError as exc:
        log.warning("wxpay notify signature rejected reason=%s", str(exc)[:80])
        return _notify_reply(401, False, "签名验证失败")
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _notify_reply(400, False, "报文格式不对")
    if not isinstance(event, dict):
        return _notify_reply(400, False, "报文格式不对")
    if event.get("event_type") != "TRANSACTION.SUCCESS":
        # 退款等其他通知暂不自动处理：确认收到即可，由平台人工核对。
        return _notify_reply(200, True, "成功")
    try:
        transaction = wxpay.decrypt_resource(config["apiv3_key"], event.get("resource"))
    except wxpay.WxPaySignatureError as exc:
        log.error("wxpay notify decrypt failed reason=%s", str(exc)[:80])
        return _notify_reply(400, False, "解密失败")
    amount = transaction.get("amount") if isinstance(transaction.get("amount"), dict) else {}
    digest = {
        "event_id": str(event.get("id") or "")[:64],
        "event_type": "TRANSACTION.SUCCESS",
        "create_time": str(event.get("create_time") or "")[:40],
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "trade_state": str(transaction.get("trade_state") or "")[:20],
        "transaction_id": str(transaction.get("transaction_id") or "")[:64],
        "success_time": str(transaction.get("success_time") or "")[:40],
        "amount_total": amount.get("total"),
    }
    out_trade_no = str(transaction.get("out_trade_no") or "")
    try:
        settle_paid_order(
            out_trade_no,
            transaction=transaction,
            config=config,
            source="notify",
            notify_digest=digest,
            now=now,
        )
    except PurchaseNotFound:
        log.error("wxpay notify for unknown order=%s", out_trade_no[:40])
        return _notify_reply(404, False, "订单不存在")
    except PayMismatch as exc:
        _report_mismatch(out_trade_no[:40], str(exc))
        return _notify_reply(400, False, "付款信息与订单不一致")
    except PurchaseConflict as exc:
        log.error("wxpay notify conflict order=%s reason=%s", out_trade_no[:40], str(exc)[:80])
        return _notify_reply(409, False, "订单状态冲突")
    return _notify_reply(200, True, "成功")
