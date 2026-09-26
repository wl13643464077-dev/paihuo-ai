"""套餐与支付的 HTTP 路由：套餐/点数余额、人工购买意向单、微信支付扫码下单与回调、
平台后台的购买单/支付单/微信支付配置。

机械拆分自 main.py(第 3 期)，函数体未改；业务逻辑在 app/billing.py、
app/purchases.py、app/wxpay.py。不 import main.py。
"""


import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import auth, billing, db, purchases, wxpay
from ..web_common import TEN, _is_boss, _need_admin, _need_root


router = APIRouter()


@router.get("/api/billing")
def billing_get():
    t = db.one("SELECT * FROM tenants WHERE id=?", (TEN(),))
    price_rows = billing.prices()
    if not _is_boss():
        price_rows = {
            action: {k: row.get(k) for k in ("points", "label")}
            for action, row in price_rows.items()
        }
    log_rows = db.q("SELECT delta, balance, reason, created_at FROM billing_log "
                    "WHERE tenant_id=? ORDER BY id DESC LIMIT 300", (TEN(),))
    # 退点也是正向流水,但把它计成「充值」会让累计充值虚高、月度两列同抬:
    # 失败一单先计消耗再计充值。按 reason「退回」前缀单列。
    agg = db.one(
        "SELECT COALESCE(SUM(CASE WHEN delta>0 AND reason NOT LIKE '退回%' "
        "THEN delta END),0) recharged, "
        "COALESCE(SUM(CASE WHEN delta>0 AND reason LIKE '退回%' "
        "THEN delta END),0) refunded, "
        "COALESCE(-SUM(CASE WHEN delta<0 THEN delta END),0) spent, COUNT(*) n "
        "FROM billing_log WHERE tenant_id=?", (TEN(),)) or {}
    # 近30天按动作聚合消耗:必须从 billing_log 流水算——核心扣点路径
    # (内容工单/专家任务/会议/成片/工具/定时)走 charge_if_claimed,
    # 只写流水不写 billing_operation;此前从后者聚合会把大头全部漏掉。
    # 口径:扣款按 reason 的价目 label 归类,「退回:」流水按 label 冲抵。
    label_to_action = {
        (row.get("label") or act): act
        for act, row in billing.prices().items()
    }
    spend_map: dict = {}
    for flow in db.q(
            "SELECT delta, reason FROM billing_log "
            "WHERE tenant_id=? AND created_at>?",
            (TEN(), time.time() - 30 * 86400)):
        reason = flow.get("reason") or ""
        delta = float(flow.get("delta") or 0)
        is_refund = reason.startswith("退回:")
        core = reason[3:] if is_refund else reason
        action = label_to_action.get(core.split(" · ", 1)[0].strip())
        if not action:
            continue
        entry = spend_map.setdefault(
            action, {"action": action, "n": 0, "points": 0.0})
        if delta < 0 and not is_refund:
            entry["n"] += 1
            entry["points"] += -delta
        elif delta > 0 and is_refund:
            entry["n"] -= 1
            entry["points"] -= delta
    spend_by_action = sorted(
        ({**e, "n": max(1, e["n"]), "points": round(e["points"], 1)}
         for e in spend_map.values() if e["points"] > 0.01),
        key=lambda e: -e["points"])
    # 按月对账(北京时区自然月,近6个月):老板问"这个月花了多少"要有答案
    monthly = db.q(
        "SELECT strftime('%Y-%m', created_at, 'unixepoch', '+8 hours') AS ym, "
        "COALESCE(SUM(CASE WHEN delta>0 AND reason NOT LIKE '退回%' "
        "THEN delta END),0) AS recharged, "
        "COALESCE(SUM(CASE WHEN delta>0 AND reason LIKE '退回%' "
        "THEN delta END),0) AS refunded, "
        "COALESCE(-SUM(CASE WHEN delta<0 THEN delta END),0) AS spent "
        "FROM billing_log WHERE tenant_id=? AND created_at>? "
        "GROUP BY ym ORDER BY ym DESC LIMIT 6",
        (TEN(), time.time() - 200 * 86400))
    return {"balance": (t or {}).get("balance") or 0,
            "plan": (t or {}).get("plan") or "",
            "plan_expires": (t or {}).get("plan_expires"),
            "is_platform": TEN() == 1,
            "recharged": agg.get("recharged") or 0, "spent": agg.get("spent") or 0,
            "refunded_total": agg.get("refunded") or 0,
            "txn_n": agg.get("n") or 0,
            "prices": price_rows, "plans": billing.PLANS,
            "periods": billing.PERIODS, "log": log_rows,
            "log_limit": 300,
            "log_truncated": int(agg.get("n") or 0) > len(log_rows),
            "spend_by_action": spend_by_action,
            "point_examples": billing.point_examples(billing.prices()),
            "monthly": monthly}


def _raise_purchase_error(exc: Exception):
    if isinstance(exc, purchases.PurchaseNotFound):
        status_code = 404
    elif isinstance(exc, purchases.PaymentGatewayError):
        status_code = 502
    elif isinstance(exc, purchases.PaymentUnavailable):
        status_code = 409
    elif isinstance(exc, purchases.PurchaseForbidden):
        status_code = 403
    elif isinstance(exc, purchases.PurchaseConflict):
        status_code = 409
    else:
        status_code = 400
    raise HTTPException(status_code, str(exc)) from None


@router.get("/api/purchases/catalog")
def purchase_catalog():
    """Authoritative catalogue; this is an offline application, not checkout."""
    return purchases.catalog()


@router.post("/api/purchases")
def purchase_create(body: dict):
    user = auth.current() or {}
    if user.get("role") not in {"root", "owner"}:
        raise HTTPException(403, "仅企业主账号可以提交购买申请")
    if any(
        key in body
        for key in ("price", "points", "amount", "quoted_price", "quoted_points")
    ):
        raise HTTPException(400, "价格和点数由服务器计算，请勿自行传入")
    try:
        return purchases.create_intent(
            int(user["tenant_id"]),
            int(user["id"]),
            request_key=body.get("request_id"),
            plan_key=body.get("plan"),
            period_key=body.get("period"),
            contact=body.get("contact"),
            note=body.get("note") or "",
            source=body.get("source") or "billing",
        )
    except (purchases.PurchaseError, ValueError) as exc:
        _raise_purchase_error(exc)


@router.get("/api/purchases")
def purchase_list(
        status: str | None = None,
        limit: int = 20,
        offset: int = 0):
    user = auth.current() or {}
    if user.get("role") not in {"root", "owner"}:
        raise HTTPException(403, "仅企业主账号可以查看购买申请")
    try:
        return purchases.list_own(
            int(user["tenant_id"]),
            int(user["id"]),
            status=status,
            limit=limit,
            offset=offset,
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)


def _purchase_admin_scope() -> int | None:
    _need_admin()
    return None if auth.is_root() else TEN()


@router.get("/api/admin/purchases")
def purchase_admin_list(
        tenant_id: int | None = None,
        status: str | None = None,
        plan: str | None = None,
        period: str | None = None,
        limit: int = 50,
        offset: int = 0):
    try:
        return purchases.list_admin(
            scope_tid=_purchase_admin_scope(),
            tenant_id=tenant_id,
            status=status,
            plan=plan,
            period=period,
            limit=limit,
            offset=offset,
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)


@router.get("/api/admin/purchases/stats")
def purchase_admin_stats(
        tenant_id: int | None = None,
        status: str | None = None,
        plan: str | None = None,
        period: str | None = None):
    try:
        return purchases.stats(
            scope_tid=_purchase_admin_scope(),
            tenant_id=tenant_id,
            status=status,
            plan=plan,
            period=period,
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)


@router.patch("/api/admin/purchases/{intent_id}")
def purchase_admin_transition(intent_id: int, body: dict):
    _need_root()
    user = auth.current() or {}
    try:
        return purchases.transition(
            intent_id,
            expected_status=body.get("expected_status"),
            target_status=body.get("status"),
            actor_id=int(user["id"]),
            note=body.get("note") or "",
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)


# ---------------- 微信支付 Native 扫码付款 ----------------
_WXPAY_NOTIFY_MAX_BYTES = 64 * 1024


@router.post("/api/pay/wxpay/orders")
def wxpay_order_create(body: dict):
    user = auth.current() or {}
    if user.get("role") not in {"root", "owner"}:
        raise HTTPException(403, "仅企业主账号可以付款开通套餐")
    if any(
        key in body
        for key in ("price", "points", "amount", "amount_fen", "total",
                    "quoted_price", "quoted_points")
    ):
        raise HTTPException(400, "价格和点数由服务器计算，请勿自行传入")
    try:
        return purchases.create_wxpay_order(
            int(user["tenant_id"]),
            int(user["id"]),
            plan_key=body.get("plan"),
            period_key=body.get("period"),
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)


@router.get("/api/pay/wxpay/orders/{order_no}")
def wxpay_order_status(order_no: str):
    user = auth.current() or {}
    if user.get("role") not in {"root", "owner"}:
        raise HTTPException(403, "仅企业主账号可以查看付款订单")
    try:
        return purchases.pay_order_status(
            int(user["tenant_id"]), int(user["id"]), order_no
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)


@router.post("/api/pay/wxpay/notify")
async def wxpay_notify(request: Request):
    """微信支付结果通知：验签失败 401 且不改状态；重复通知幂等返回成功。"""
    declared = request.headers.get("content-length")
    try:
        if declared is not None and int(declared) > _WXPAY_NOTIFY_MAX_BYTES:
            return JSONResponse({"code": "FAIL", "message": "报文过大"}, status_code=413)
    except ValueError:
        return JSONResponse({"code": "FAIL", "message": "报文长度无效"}, status_code=400)
    # 边读边计数：没带 Content-Length 的分块请求也不能先整段读进内存
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _WXPAY_NOTIFY_MAX_BYTES:
            return JSONResponse({"code": "FAIL", "message": "报文过大"}, status_code=413)
        chunks.append(chunk)
    raw = b"".join(chunks)
    status_code, reply = await db.arun(
        purchases.handle_wxpay_notify, dict(request.headers), raw
    )
    return JSONResponse(reply, status_code=status_code)


@router.get("/api/admin/wxpay/config")
def wxpay_config_get():
    _need_root()
    return wxpay.public_view()


@router.put("/api/admin/wxpay/config")
def wxpay_config_put(body: dict):
    _need_root()
    try:
        return wxpay.save_config(body)
    except wxpay.WxPayConfigError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/api/admin/pay-orders")
def wxpay_admin_orders(status: str | None = None, limit: int = 50, offset: int = 0):
    _need_root()
    try:
        return purchases.list_pay_orders_admin(
            status=status, limit=limit, offset=offset
        )
    except purchases.PurchaseError as exc:
        _raise_purchase_error(exc)
