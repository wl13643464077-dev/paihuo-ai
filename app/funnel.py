"""Privacy-safe, self-hosted product funnel observability.

Only a small allow-list of product events and dimensions can be persisted.
Actors are HMACed before storage; raw IP addresses, phone numbers, user agents,
form contents and credentials never enter the analytics table.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import auth, db

log = logging.getLogger("funnel")

EVENT_DIMENSIONS = {
    "landing_view": {"promo"},
    "page_view": {
        # Keep this aligned with static/app.js `routes`: the first hash segment
        # is emitted verbatim by trackPageView().
        "office", "new", "job", "profiles", "assets", "delivery",
        "knowledge", "schedules", "settings", "avatar", "admin", "team",
        "billing", "meetings", "tasks", "company", "production", "censor",
        "channels", "tools", "trash", "guide", "notifications",
        # Legacy dimensions remain readable while current routes no longer
        # collapse into "other".
        "library", "schedule", "publish", "matrix", "other",
    },
    "pricing_view": {"promo", "billing"},
    "lead_submitted": {"account_apply", "guest_trial"},
    "registration_complete": {"application", "direct_admin"},
    "login_success": {"password"},
    "first_job_submitted": {"content", "expert"},
    "purchase_requested": {"promo", "login", "billing"},
    "purchase_paid": {"subscription"},
}
PUBLIC_EVENTS = {"landing_view", "pricing_view"}
CLIENT_EVENTS = {"page_view", "pricing_view"}
LABELS = {
    "landing_view": "落地页访问",
    "lead_submitted": "留资提交",
    "registration_complete": "账号开通",
    "login_success": "登录成功",
    "first_job_submitted": "首次派活",
    "purchase_requested": "提交购买申请",
    "purchase_paid": "确认购买到账",
    "pricing_view": "定价页到达",
    "page_view": "产品页访问",
}

_retention_lock = threading.Lock()
_last_retention_day = ""


def day_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))


def normalize(event: str, dimension: str) -> tuple[str, str]:
    event = str(event or "").strip().lower()
    if event not in EVENT_DIMENSIONS:
        raise ValueError("未知漏斗事件")
    dimension = str(dimension or "").strip().lower()
    if event == "page_view" and dimension not in EVENT_DIMENSIONS[event]:
        dimension = "other"
    if dimension not in EVENT_DIMENSIONS[event]:
        raise ValueError("漏斗事件维度无效")
    return event, dimension


def _actor_hash(actor_key: str) -> str:
    material = ("funnel:v1:" + str(actor_key or "")).encode("utf-8", "replace")
    return hmac.new(auth._secret(), material, hashlib.sha256).hexdigest()


def _trim_retention(today: str) -> None:
    global _last_retention_day
    if _last_retention_day == today:
        return
    with _retention_lock:
        if _last_retention_day == today:
            return
        cutoff = (
            datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            - timedelta(days=400)
        ).strftime("%Y-%m-%d")
        db.execute("DELETE FROM funnel_event WHERE day<?", (cutoff,))
        _last_retention_day = today


def record(
    event: str,
    dimension: str,
    *,
    tenant_id: int,
    actor_key: str,
    now: float | None = None,
    unique_only: bool = False,
) -> bool:
    """Persist one allow-listed event without retaining its raw actor identity."""
    event, dimension = normalize(event, dimension)
    tenant_id = max(0, int(tenant_id or 0))
    if not actor_key:
        raise ValueError("漏斗事件缺少匿名主体")
    stamp = float(now or time.time())
    day = day_key(stamp)
    actor = _actor_hash(actor_key)
    connection = db.conn()
    connection.execute(
        """
        INSERT INTO funnel_event(
          day,event,dimension,tenant_id,actor_hash,hits,first_at,last_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(day,event,dimension,tenant_id,actor_hash) DO UPDATE SET
          hits=CASE WHEN ? THEN funnel_event.hits ELSE funnel_event.hits+1 END,
          last_at=excluded.last_at
        """,
        (
            day, event, dimension, tenant_id, actor, 1, stamp, stamp,
            1 if unique_only else 0,
        ),
    )
    if not getattr(db._thread, "atomic_depth", 0):
        connection.commit()
    _trim_retention(day)
    return True


def record_safe(event: str, dimension: str, **kwargs) -> bool:
    """Analytics must never make a successful customer operation fail."""
    try:
        return record(event, dimension, **kwargs)
    except Exception as exc:  # noqa: BLE001 - intentionally isolates telemetry failures
        log.error(
            "funnel event dropped event=%s error_type=%s",
            event,
            type(exc).__name__,
        )
        return False


def record_first_work(tenant_id: int, dimension: str) -> bool:
    """Record exactly one first assignment per tenant across all calendar days."""
    event, dimension = normalize("first_job_submitted", dimension)
    tenant_id = max(1, int(tenant_id))
    stamp = time.time()
    day = day_key(stamp)
    # Accounts opened from a lead keep the same irreversible journey identity
    # from lead -> registration -> first assignment.  Directly-created tenants
    # use their tenant id.  The phone is read only to HMAC it and is never copied
    # into the analytics table or logs.
    application = db.one(
        "SELECT phone FROM account_apply WHERE tenant_id=? AND phone IS NOT NULL "
        "ORDER BY id LIMIT 1",
        (tenant_id,),
    )
    actor_key = (
        f"lead:{application['phone']}"
        if application and application.get("phone")
        else f"tenant:{tenant_id}"
    )
    actor = _actor_hash(actor_key)
    try:
        changed = db.execute(
            """
            INSERT OR IGNORE INTO funnel_event(
              day,event,dimension,tenant_id,actor_hash,hits,first_at,last_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (day, event, dimension, tenant_id, actor, 1, stamp, stamp),
        )
        _trim_retention(day)
        return changed == 1
    except Exception as exc:  # noqa: BLE001 - telemetry is deliberately non-blocking
        log.error(
            "first-work funnel event dropped tenant=%s error_type=%s",
            tenant_id,
            type(exc).__name__,
        )
        return False


def report(days: int = 30) -> dict:
    days = min(180, max(1, int(days or 30)))
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days - 1)).isoformat()
    rows = db.q(
        """
        SELECT event, SUM(hits) AS events, COUNT(DISTINCT actor_hash) AS actors
        FROM funnel_event WHERE day>=? GROUP BY event
        """,
        (since,),
    )
    by_event = {
        row["event"]: {
            "label": LABELS.get(row["event"], row["event"]),
            "events": int(row.get("events") or 0),
            "actors": int(row.get("actors") or 0),
        }
        for row in rows
    }
    summary = {
        event: by_event.get(event, {"label": LABELS[event], "events": 0, "actors": 0})
        for event in LABELS
    }
    daily_rows = db.q(
        """
        SELECT day,event,SUM(hits) AS events,COUNT(DISTINCT actor_hash) AS actors
        FROM funnel_event WHERE day>=? GROUP BY day,event ORDER BY day
        """,
        (since,),
    )
    daily: dict[str, dict] = {}
    for row in daily_rows:
        item = daily.setdefault(row["day"], {"day": row["day"]})
        item[row["event"]] = {
            "events": int(row.get("events") or 0),
            "actors": int(row.get("actors") or 0),
        }

    def rate(numerator: str, denominator: str) -> float:
        top = summary[numerator]["actors"]
        bottom = summary[denominator]["actors"]
        return round(top * 100 / bottom, 1) if bottom else 0.0

    def cohort_rate(numerator: str, denominator: str) -> float:
        bottom = summary[denominator]["actors"]
        if not bottom:
            return 0.0
        row = db.one(
            """
            SELECT COUNT(DISTINCT numerator.actor_hash) AS actors
            FROM funnel_event numerator
            JOIN funnel_event denominator
              ON denominator.actor_hash=numerator.actor_hash
            WHERE numerator.day>=? AND denominator.day>=?
              AND numerator.event=? AND denominator.event=?
            """,
            (since, since, numerator, denominator),
        ) or {}
        return round(int(row.get("actors") or 0) * 100 / bottom, 1)

    return {
        "days": days,
        "since": since,
        "through": today.isoformat(),
        "summary": summary,
        "conversion": {
            "landing_to_lead_pct": rate("lead_submitted", "landing_view"),
            "lead_to_registration_pct": cohort_rate(
                "registration_complete", "lead_submitted"
            ),
            "registration_to_first_job_pct": cohort_rate(
                "first_job_submitted", "registration_complete"
            ),
            "landing_to_pricing_pct": rate("pricing_view", "landing_view"),
            "purchase_to_paid_pct": cohort_rate(
                "purchase_paid", "purchase_requested"
            ),
        },
        "daily": list(daily.values()),
    }
