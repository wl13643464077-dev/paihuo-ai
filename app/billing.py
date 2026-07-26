"""计费与套餐(V8):点数钱包 + 按次消耗 + 月/季/年订阅.

定价原则:各动作的真实 API 成本(云雾/HeyGen 扣费估算)× 3,取整为「点」(1点=¥1)。
成本依据(2026-07 实测/牌价):
- 内容整单:检索3工位走云雾联网能力网关(~$0.6)+ 云雾文本模型其余工位(~$0.1)+ AI生图4-7张(~$0.4)
  ≈ $1.1 ≈ ¥8 → 定价 24 点
- 专家任务/单独派活:DeepSeek 一次 ≈ ¥0.3 → 1 点
- 数字人视频:HeyGen ~$1/分钟 + 海螺TTS ≈ 30秒成本 ¥4 → 12 点/条
- 声音克隆:海螺克隆 ≈ ¥3 → 9 点/次
- 全网进修：云雾联网能力网关 ≈ ¥1.1 → 3 点/次
- 爆款链接提取：云雾联网能力网关 ≈ ¥0.4 → 1 点/次
平台自有租户(id=1,老板本人)不扣点。
"""
import json
import logging
import time
import uuid

from . import auth, db

log = logging.getLogger("billing")

DEFAULT_PRICES = {
    "content_job": {"points": 18, "label": "内容流水线整单", "cost": "成本≈¥6(检索¥3.6+撰写¥0.3+生图¥2)"},
    "expert_task": {"points": 1, "label": "行业专家任务/单独派活", "cost": "成本≈¥0.3"},
    "avatar_basic": {"points": 6, "label": "数字人视频·基础版(RunningHub)", "cost": "成本≈¥2"},
    "avatar_video": {"points": 12, "label": "数字人视频(≤30秒)", "cost": "成本≈¥4(HeyGen+配音)"},
    "avatar_video_60": {"points": 20, "label": "数字人视频(31-60秒)", "cost": "成本≈¥7"},
    "voice_clone": {"points": 9, "label": "声音克隆", "cost": "成本≈¥3"},
    "learn": {"points": 3, "label": "员工全网进修", "cost": "成本≈¥1.1"},
    "link_extract": {"points": 1, "label": "爆款链接提取口播稿", "cost": "成本≈¥0.4"},
    "censor_check": {"points": 1, "label": "审查官·发前深度审查", "cost": "成本≈¥0.3"},
    "censor_retro": {"points": 1, "label": "审查官·发后数据复盘", "cost": "成本≈¥0.3"},
    "wechat_draft": {"points": 1, "label": "一键发公众号草稿箱(含终审)", "cost": "成本≈¥0.3"},
    "text_video": {"points": 3, "label": "图文转视频·一键成片", "cost": "成本≈¥1(配音+合成)"},
    "matrix_variants": {"points": 1, "label": "口播矩阵·一稿裂变", "cost": "成本≈¥0.3"},
    "pcal": {"points": 2, "label": "私域内容日历(整月)", "cost": "成本≈¥0.6"},
    "hot_pick": {"points": 1, "label": "今日必发·热点扫描", "cost": "成本≈¥0.4(联网)"},
    "warmup": {"points": 3, "label": "起号军师·30天冷启动计划", "cost": "成本≈¥1.1(联网调研)"},
    "leads": {"points": 3, "label": "获客线索雷达", "cost": "成本≈¥1.1(联网侦察)"},
    "bench_watch": {"points": 3, "label": "竞品盯梢周报", "cost": "成本≈¥1.1(联网)"},
    "menu_copy": {"points": 1, "label": "看图写卖点/菜单文案", "cost": "成本≈¥0.3"},
    "product_shot": {"points": 2, "label": "产品图美化(图生图)", "cost": "成本≈¥0.6"},
}

PLANS = [
    {"key": "trial", "name": "体验版", "points": 150, "price": 99, "sale": 69,
     "desc": "适合先试试水:每月约 5 单内容 + 3 条数字人"},
    {"key": "startup", "name": "创业版", "points": 500, "price": 299, "sale": 199,
     "desc": "一人公司日更:含 500 点，专家任务按 1 点/次计费"},
    {"key": "biz", "name": "企业版", "points": 1800, "price": 899, "sale": 599,
     "desc": "小团队多账号:内容矩阵 + 数字人矩阵"},
    {"key": "flagship", "name": "旗舰版", "points": 7000, "price": 2999, "sale": 1999,
     "desc": "MCN/多门店连锁:全板块放开跑"},
]
PERIODS = [{"key": "month", "label": "月付", "months": 1, "discount": 1.0},
           {"key": "quarter", "label": "季付(9折)", "months": 3, "discount": 0.9},
           {"key": "year", "label": "年付(8折)", "months": 12, "discount": 0.8}]


def prices() -> dict:
    saved = db.jloads(db.get_setting("prices"), None)
    return saved if saved else DEFAULT_PRICES


def balance(tid: int = None) -> float:
    tid = tid or auth.tenant_id()
    r = db.one("SELECT balance FROM tenants WHERE id=?", (tid,))
    return (r or {}).get("balance") or 0


class InsufficientPoints(Exception):
    pass


class _LinkedClaimRejected(Exception):
    """让计费操作与业务状态一起回滚的内部控制流。"""


def _spend(tid: int, pts: float, reason: str) -> float:
    """原子扣点:条件 UPDATE(防并发扣穿)+ 流水,同一事务(防余额与流水脱节)."""
    now = time.time()
    with db.atomic() as c:
        cur = c.execute("UPDATE tenants SET balance=balance-?, updated_at=? "
                        "WHERE id=? AND balance>=?", (pts, now, tid, pts))
        if cur.rowcount == 0:
            r = c.execute("SELECT balance FROM tenants WHERE id=?", (tid,)).fetchone()
            bal = (r[0] if r else 0) or 0
            raise InsufficientPoints(f"点数不足:本次需 {pts:.0f} 点,余额 {bal:.0f} 点。请充值或升级套餐")
        newbal = c.execute("SELECT balance FROM tenants WHERE id=?", (tid,)).fetchone()[0]
        c.execute("INSERT INTO billing_log(tenant_id,delta,balance,reason,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?)", (tid, -pts, newbal, reason, now, now))
    return newbal


def charge_if_claimed(action: str, tid: int, claim, note: str = "",
                      n: int = 1, points: float = None) -> bool:
    """原子抢占待开工记录并扣点，杜绝“先扣点、任务却没创建”。"""
    p = prices().get(action) or {"points": 1}
    amount = float(points if points is not None else p["points"] * max(1, n))
    reason = f"{p.get('label', action)}{(' · ' + note) if note else ''}"
    now = time.time()
    with db.atomic() as c:
        if not claim(c):
            return False
        if not tid or tid == 1:
            return True
        cur = c.execute(
            "UPDATE tenants SET balance=balance-?, updated_at=? "
            "WHERE id=? AND balance>=?",
            (amount, now, tid, amount),
        )
        if cur.rowcount == 0:
            row = c.execute("SELECT balance FROM tenants WHERE id=?", (tid,)).fetchone()
            bal = (row[0] if row else 0) or 0
            raise InsufficientPoints(
                f"点数不足:本次需 {amount:.0f} 点,余额 {bal:.0f} 点。请充值或升级套餐")
        newbal = c.execute("SELECT balance FROM tenants WHERE id=?", (tid,)).fetchone()[0]
        c.execute(
            "INSERT INTO billing_log(tenant_id,delta,balance,reason,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (tid, -amount, newbal, reason, now, now),
        )
    return True


def start_operation(action: str, tid: int = None, note: str = "", n: int = 1,
                    op_key: str = None) -> str:
    """建立一笔可恢复的后台/长请求计费操作并原子扣点。

    ``op_key`` 可由业务传入以防重复提交；省略时生成不可预测的唯一键。记录先以
    ``pending`` 落库，再由 ``charge_if_claimed`` 在同一事务中切为 ``charged``
    并扣款。进程若在两步之间退出，只会留下未扣费的 pending 记录。
    """
    tid = tid or auth.tenant_id()
    units = max(1, int(n or 1))
    key = (op_key or uuid.uuid4().hex).strip()
    if not key or len(key) > 160:
        raise ValueError("无效的计费操作编号")
    price = prices().get(action) or {"points": 1}
    points = 0.0 if tid == 1 else float(price["points"] * units)
    now = time.time()
    try:
        with db.atomic() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO billing_operation"
                "(op_key,tenant_id,action,units,points,note,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'pending',?,?)",
                (key, tid, action, units, points, note[:200], now, now),
            )
            if cur.rowcount != 1:
                raise ValueError("重复的计费操作")
    except Exception:
        raise

    def claim(c):
        cur = c.execute(
            "UPDATE billing_operation SET status='charged',updated_at=? "
            "WHERE op_key=? AND status='pending'",
            (time.time(), key),
        )
        return cur.rowcount == 1

    try:
        charged = charge_if_claimed(
            action, tid, claim, note=note, n=units, points=points)
    except Exception:
        db.q("DELETE FROM billing_operation WHERE op_key=? AND status='pending'", (key,))
        raise
    if not charged:
        raise RuntimeError("计费操作状态冲突")
    return key


def start_operation_if_claimed(action: str, tid: int, claim, note: str = "",
                               n: int = 1, op_key: str = None) -> str | None:
    """在同一事务中占用业务状态、建立操作记录并扣点。

    用于“先把业务改成 running，再另开计费记录”会留下崩溃窗口的入口。
    ``claim(connection)`` 返回 False 时什么都不写；余额不足或插入失败时，
    业务 claim、操作记录、余额和流水也会整体回滚。
    """
    units = max(1, int(n or 1))
    key = (op_key or uuid.uuid4().hex).strip()
    if not key or len(key) > 160:
        raise ValueError("无效的计费操作编号")
    price = prices().get(action) or {"points": 1}
    points = 0.0 if tid == 1 else float(price["points"] * units)
    reason = f"{price.get('label', action)}{(' · ' + note) if note else ''}"
    now = time.time()
    with db.atomic() as c:
        if not claim(c):
            return None
        c.execute(
            "INSERT INTO billing_operation"
            "(op_key,tenant_id,action,units,points,note,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'charged',?,?)",
            (key, tid, action, units, points, note[:200], now, now),
        )
        if tid != 1 and points > 0:
            cur = c.execute(
                "UPDATE tenants SET balance=balance-?, updated_at=? "
                "WHERE id=? AND balance>=?",
                (points, now, tid, points),
            )
            if cur.rowcount == 0:
                row = c.execute(
                    "SELECT balance FROM tenants WHERE id=?", (tid,)
                ).fetchone()
                balance_now = (row[0] if row else 0) or 0
                raise InsufficientPoints(
                    f"点数不足:本次需 {points:.0f} 点,余额 "
                    f"{balance_now:.0f} 点。请充值或升级套餐"
                )
            new_balance = c.execute(
                "SELECT balance FROM tenants WHERE id=?", (tid,)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO billing_log"
                "(tenant_id,delta,balance,reason,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (tid, -points, new_balance, reason, now, now),
            )
    return key


def complete_operation_if_claimed(op_key: str, claim) -> bool:
    """业务终态和计费成功必须在同一次提交中可见。"""
    try:
        with db.atomic() as c:
            changed = c.execute(
                "UPDATE billing_operation SET status='succeeded',updated_at=? "
                "WHERE op_key=? AND status='charged'",
                (time.time(), op_key),
            )
            if changed.rowcount != 1:
                return False
            if not claim(c):
                raise _LinkedClaimRejected
        return True
    except _LinkedClaimRejected:
        return False


def fail_operation_if_claimed(op_key: str, reason: str, claim) -> bool:
    """业务回滚、操作失败和实际退款在同一次提交中完成。"""
    error = (reason or "执行失败")[:500]
    now = time.time()
    operation = db.one(
        "SELECT action FROM billing_operation WHERE op_key=?", (op_key,)
    )
    if not operation:
        return False
    label = (prices().get(operation["action"]) or {}).get(
        "label", operation["action"]
    )
    try:
        with db.atomic() as c:
            row = c.execute(
                "SELECT tenant_id,points,action FROM billing_operation "
                "WHERE op_key=? AND status='charged'",
                (op_key,),
            ).fetchone()
            if not row:
                return False
            changed = c.execute(
                "UPDATE billing_operation SET status='refunded',error=?,updated_at=? "
                "WHERE op_key=? AND status='charged'",
                (error, now, op_key),
            )
            if changed.rowcount != 1:
                return False
            if not claim(c):
                raise _LinkedClaimRejected
            tid = int(row["tenant_id"] or 0)
            points = float(row["points"] or 0)
            if tid and tid != 1 and points > 0:
                tenant = c.execute(
                    "UPDATE tenants SET balance=COALESCE(balance,0)+?, updated_at=? "
                    "WHERE id=?",
                    (points, now, tid),
                )
                if tenant.rowcount != 1:
                    raise RuntimeError(f"退款租户不存在:{tid}")
                new_balance = c.execute(
                    "SELECT balance FROM tenants WHERE id=?", (tid,)
                ).fetchone()[0]
                c.execute(
                    "INSERT INTO billing_log"
                    "(tenant_id,delta,balance,reason,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        tid,
                        points,
                        new_balance,
                        f"退回:{label} · {error[:160]}",
                        now,
                        now,
                    ),
                )
        return True
    except _LinkedClaimRejected:
        return False


def complete_operation(op_key: str) -> bool:
    """把已交付操作标为完成；重复完成是安全的。"""
    changed = db.execute(
        "UPDATE billing_operation SET status='succeeded',updated_at=? "
        "WHERE op_key=? AND status='charged'",
        (time.time(), op_key),
    )
    if changed:
        return True
    row = db.one("SELECT status FROM billing_operation WHERE op_key=?", (op_key,))
    return bool(row and row["status"] == "succeeded")


def fail_operation(op_key: str, reason: str = "执行失败自动退回") -> bool:
    """失败结算并按实际扣款退款；并发/重复调用最多成功一次。"""
    row = db.one(
        "SELECT tenant_id,points,action FROM billing_operation WHERE op_key=?",
        (op_key,),
    )
    if not row:
        return False

    def claim(c):
        cur = c.execute(
            "UPDATE billing_operation SET status='refunded',error=?,updated_at=? "
            "WHERE op_key=? AND status='charged'",
            ((reason or "执行失败")[:500], time.time(), op_key),
        )
        return cur.rowcount == 1

    label = (prices().get(row["action"]) or {}).get("label", row["action"])
    return refund_amount_if_claimed(
        row["tenant_id"], row["points"], claim,
        f"退回:{label} · {(reason or '执行失败')[:160]}",
    )


def recover_interrupted_operations(reason: str = "服务重启，中断任务自动退回",
                                   exclude_op_keys=None) -> int:
    """启动时退回上个进程仍为 charged 的操作，并清理无扣款的 pending 记录。"""
    excluded = {str(key) for key in (exclude_op_keys or []) if key}
    rows = db.q("SELECT op_key FROM billing_operation WHERE status='charged'")
    rows = [row for row in rows if row["op_key"] not in excluded]
    refunded = sum(1 for row in rows if fail_operation(row["op_key"], reason))
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        db.q(
            f"DELETE FROM billing_operation WHERE status='pending' "
            f"AND op_key NOT IN ({placeholders})",
            tuple(excluded),
        )
    else:
        db.q("DELETE FROM billing_operation WHERE status='pending'")
    return refunded


def charge(action: str, tid: int = None, note: str = "") -> float:
    """扣点。平台自有租户(1)免扣。返回扣后余额;不足抛 InsufficientPoints."""
    tid = tid or auth.tenant_id()
    if tid == 1:
        return -1  # 平台自有,不计费
    p = prices().get(action) or {"points": 1}
    return _spend(tid, p["points"],
                  f"{p.get('label', action)}{(' · ' + note) if note else ''}")


def charge_n(action: str, n: int, tid: int = None, note: str = "") -> float:
    """按份数一次性扣点(圆桌会议按人头):要么全扣要么不扣,杜绝扣一半报 402."""
    tid = tid or auth.tenant_id()
    if tid == 1:
        return -1
    p = prices().get(action) or {"points": 1}
    return _spend(tid, p["points"] * max(1, n),
                  f"{p.get('label', action)} ×{n}{(' · ' + note) if note else ''}")


def refund(action: str, tid: int, note: str = "", n: int = 1):
    """任务失败退点(与 charge 对称;租户1本来免扣,不退)."""
    if not tid or tid == 1:
        return
    p = prices().get(action) or {"points": 1}
    grant(tid, p["points"] * max(1, n),
          f"退回:{p.get('label', action)}{(' · ' + note) if note else ''}")


def refund_amount_if_claimed(tid: int, points: float, claim, reason: str) -> bool:
    """原子抢占失败结算并按任务实际扣点金额退款。"""
    amount = float(points or 0)
    now = time.time()
    with db.atomic() as c:
        if not claim(c):
            return False
        if not tid or tid == 1 or amount <= 0:
            return True
        cur = c.execute(
            "UPDATE tenants SET balance=COALESCE(balance,0)+?, updated_at=? WHERE id=?",
            (amount, now, tid),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"退款租户不存在:{tid}")
        newbal = c.execute("SELECT balance FROM tenants WHERE id=?", (tid,)).fetchone()[0]
        c.execute(
            "INSERT INTO billing_log(tenant_id,delta,balance,reason,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (tid, amount, newbal, reason, now, now),
        )
    return True


def refund_if_claimed(action: str, tid: int, claim, note: str = "", n: int = 1) -> bool:
    """先原子抢占一条待失败任务，再在同一事务内退点。

    ``claim(connection)`` 只有在本调用把任务从 running 改成 failed 时才返回 True。
    状态变更与余额/流水同生共死，worker、看门狗和重启恢复即使同时撞上也只会退一次。
    """
    p = prices().get(action) or {"points": 1}
    points = p["points"] * max(1, n)
    reason = f"退回:{p.get('label', action)}{(' · ' + note) if note else ''}"
    return refund_amount_if_claimed(tid, points, claim, reason)


def grant(tid: int, points: float, reason: str):
    now = time.time()
    with db.atomic() as c:
        c.execute("UPDATE tenants SET balance=COALESCE(balance,0)+?, updated_at=? WHERE id=?",
                  (points, now, tid))
        newbal = c.execute("SELECT balance FROM tenants WHERE id=?", (tid,)).fetchone()[0]
        c.execute("INSERT INTO billing_log(tenant_id,delta,balance,reason,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?)", (tid, points, newbal, reason, now, now))
    return newbal


def subscribe(tid: int, plan_key: str, period_key: str):
    plan = next((p for p in PLANS if p["key"] == plan_key), None)
    period = next((p for p in PERIODS if p["key"] == period_key), None)
    if not plan or not period:
        raise ValueError("未知套餐或周期")
    pts = plan["points"] * period["months"]
    price = round(plan["sale"] * period["months"] * period["discount"])
    expires = time.time() + period["months"] * 31 * 86400
    db.update("tenants", tid, {"plan": f"{plan['name']}·{period['label']}",
                               "plan_expires": expires})
    grant(tid, pts, f"开通{plan['name']}({period['label']},实收¥{price}),含 {pts} 点")
    return {"points": pts, "price": price, "expires": expires}
