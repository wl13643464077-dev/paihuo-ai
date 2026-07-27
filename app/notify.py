"""微信通知(V25):企业微信群机器人 webhook,把派活的大事推到老板微信里.

傻瓜化配置:企业微信群 → 右上角…→ 添加群机器人 → 复制 Webhook 地址 → 粘到
「发布渠道」页,完事。个人微信也能收:企业微信 App 和微信互通,或把机器人加进
和自己的单人群。事件:等拍板 / 交付完成 / 审查拦截 / 复盘提醒 / 周报出炉。
"""
import asyncio
import json
import logging
import re
import time

import httpx

from . import db

log = logging.getLogger("notify")

_WEBHOOK_RE = re.compile(r"^https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[\w-]+$")


def get_webhook(tid: int) -> str:
    return db.get_setting(f"wechat_webhook:{tid}") or ""


def set_webhook(tid: int, url: str):
    url = (url or "").strip()
    if url and not _WEBHOOK_RE.match(url):
        raise ValueError("这不是企业微信群机器人的 Webhook 地址(应形如 "
                         "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx)")
    db.set_setting(f"wechat_webhook:{tid}", url or None)


def build_msg(kind: str, payload: dict) -> str:
    """事件 → 企微 markdown 消息(老板一眼能看懂,带直达链接)."""
    base = db.get_setting("site_base") or "https://paihuo.ai"
    p = payload or {}
    title = (p.get("title") or "")[:40]
    if kind == "awaiting":
        return (f"**📥 派活 · 等您拍板**\n工单 #{p.get('job_id')} 《{title}》\n"
                f"工位:{p.get('station', '')}\n"
                f"[去拍板]({base}/#/job/{p.get('job_id')})")
    if kind == "done":
        return (f"**📦 派活 · 交付完成**\n工单 #{p.get('job_id')} 《{title}》已出交付包\n"
                f"[看交付包]({base}/#/delivery/{p.get('job_id')})")
    if kind == "gate":
        return (f"**⛔ 派活 · 审查官拦截**\n工单 #{p.get('job_id')} 《{title}》被质检拦下\n"
                f"[去处理]({base}/#/job/{p.get('job_id')})")
    if kind == "retro_due":
        return (f"**📊 派活 · 该复盘了**\n《{title}》({p.get('platform', '')})发布已满 "
                f"{p.get('day', '')} 天\n把后台数据丢给审查官,看看表现和限流风险\n"
                f"[一键复盘]({base}/#/censor)")
    if kind == "schedule_paused":
        return (f"**⏸️ 派活 · 定时任务已暂停**\n「{title}」因点数不足自动暂停,"
                f"内容会断更!\n充值后请到定时任务页重新打开开关\n"
                f"[去充值]({base}/#/billing) · [看定时任务]({base}/#/schedules)")
    if kind == "schedule_failed":
        return (f"**⚠️ 派活 · 定时任务连续失败**\n「{title}」已连续 "
                f"{int(p.get('streak') or 0)} 次到点开工失败(系统会每 10 分钟"
                f"自动重试),日更可能断更,建议点开看看\n"
                f"[看定时任务]({base}/#/schedules)")
    if kind == "learn_done":
        fresh = int(p.get("new") or 0)
        return (f"**🎓 派活 · 员工进修完成**\n「{title}」新学 {fresh} 条技能,"
                f"技能库共 {p.get('total', '?')} 条"
                + ("" if fresh else "\n本次内容与已有技能全部重复,3 点已自动退回,建议隔几天再进修")
                + f"\n[看员工技能]({base}/#/)")
    if kind == "learn_failed":
        return (f"**🎓 派活 · 员工进修失败**\n「{title}」本次进修没成功,"
                f"3 点已自动退回,可稍后重试\n[回办公室]({base}/#/)")
    if kind == "daily_digest":
        # 老板昨日经营简报:完成/花销/风险/余额,一条看全,不用登录翻后台
        lines = [f"**📋 派活 · 昨日经营简报({p.get('date', '')})**",
                 f"✅ 完成:内容工单 {int(p.get('jobs_done') or 0)} 单 · "
                 f"专家任务 {int(p.get('tasks_done') or 0)} 件",
                 f"💎 花销:消耗 {float(p.get('spent') or 0):.0f} 点"
                 + (f" · 失败退回 {int(p.get('refunds') or 0)} 笔" if p.get("refunds") else "")]
        if p.get("paused"):
            lines.append(f"⏸️ 风险:{int(p['paused'])} 个定时任务因点数不足暂停,内容断更中")
        balance_line = f"💰 余额:{float(p.get('balance') or 0):.0f} 点"
        if p.get("days_left") is not None:
            balance_line += f",按近 7 天日均消耗约可再跑 {int(p['days_left'])} 天"
        lines.append(balance_line)
        if p.get("plan_days_left") is not None and int(p["plan_days_left"]) <= 7:
            expire_days = int(p["plan_days_left"])
            lines.append("⏰ 套餐:" + ("已到期,请尽快续费"
                         if expire_days < 0
                         else f"还有 {expire_days} 天到期,记得续费"))
        lines.append(f"[看账单明细]({base}/#/billing)"
                     + (f" · [去处理定时任务]({base}/#/schedules)" if p.get("paused") else ""))
        return "\n".join(lines)
    if kind == "member_reviewed":
        verdict = "通过" if p.get("approved") else "打回"
        return (f"**👤 派活 · 成员代拍板**\n"
                f"👤 {p.get('user', '')} 已代拍板 工单#{p.get('job_id')} "
                f"工位{p.get('station', '')}:{verdict}\n"
                f"[看工单]({base}/#/job/{p.get('job_id')})")
    if kind == "purchase_requested":
        return (
            f"**💼 派活 · 新购买申请**\n{p.get('summary', '')}\n"
            f"[去跟进]({base}/#/billing)"
        )
    if kind in {"purchase_contacted", "purchase_lost", "purchase_paid"}:
        return (
            f"**💼 派活 · 套餐申请进展**\n{p.get('summary', '')}\n"
            f"[查看申请]({base}/#/billing)"
        )
    if kind == "report":
        return (f"**📰 派活 · {p.get('report_name', '报告出炉')}**\n{(p.get('summary') or '')[:180]}\n"
                f"[查看]({base}/{p.get('link') or '#/knowledge'})")
    if kind == "video":
        return (f"**🎬 派活 · 视频成片**\n《{title}》已出片,可下载发布\n"
                f"[查看]({base}{p.get('file', '')})")
    if kind == "pub":
        if p.get("ok"):
            return (f"**🚀 派活 · 矩阵发布成功**\n《{title}》已发到{p.get('platform', '')},"
                    f"台账已登记,T+1/3/7 自动复盘\n[看发布记录]({base}/#/channels)")
        return (f"**🚀 派活 · 矩阵发布失败**\n《{title}》({p.get('platform', '')})\n"
                f"{p.get('why', '')}\n👉 {p.get('fix', '')}\n[去处理]({base}/#/channels)")
    return f"**派活**\n{json.dumps(p, ensure_ascii=False)[:300]}"


def _inbox_item(kind: str, payload: dict) -> tuple[str, str, str]:
    """Build a compact, non-sensitive in-app notification."""
    p = payload or {}
    labels = {
        "awaiting": "有工单等您拍板",
        "done": "内容工单已交付",
        "gate": "审查官拦截了一项内容",
        "retro_due": "发布内容该复盘了",
        "report": p.get("report_name") or "报告已出炉",
        "video": "视频成片已交付",
        "pub": "矩阵发布成功" if p.get("ok") else "矩阵发布失败",
        "learn_done": "员工进修完成",
        "learn_failed": "员工进修失败(已退点)",
        "daily_digest": f"昨日经营简报({p.get('date', '')})",
        "schedule_failed": "定时任务连续失败,可能断更",
        "schedule_paused": "定时任务因点数不足已暂停,内容会断更",
        "purchase_requested": "有新的套餐购买申请",
        "purchase_contacted": "套餐申请已联系",
        "purchase_lost": "套餐申请已结束",
        "purchase_paid": "套餐和点数已开通",
        # 副账号代老板拍板:标题直接说清谁、哪单、哪站、通过还是打回
        "member_reviewed": (
            f"👤 {p.get('user', '')} 已代拍板 工单#{p.get('job_id')} "
            f"工位{p.get('station', '')}:"
            f"{'通过' if p.get('approved') else '打回'}"
        ),
    }
    title = str(labels.get(kind) or "派活有新进展")[:80]
    body = str(
        p.get("summary")
        or p.get("title")
        or p.get("why")
        or ""
    ).strip()[:240]
    if kind in {"awaiting", "gate", "member_reviewed"} and p.get("job_id"):
        link = f"#/job/{int(p['job_id'])}"
    elif kind == "done" and p.get("job_id"):
        link = f"#/delivery/{int(p['job_id'])}"
    elif kind == "retro_due":
        link = "#/censor"
    elif kind == "video":
        link = "#/tasks"
    elif kind == "pub":
        link = "#/channels"
    elif kind == "daily_digest" or kind.startswith("purchase_"):
        link = "#/billing"
    elif kind in {"schedule_paused", "schedule_failed"}:
        link = "#/schedules"
    else:
        link = str(p.get("link") or "#/knowledge")
    if not re.match(r"^#/[A-Za-z0-9_~.%/?=&:+-]*$", link):
        link = "#/"
    return title, body, link


# 通知权限不另加数据库列，保持 schema48 可直接运行。每一种通知先归入
# 「超级管理员」「企业主」「业务板块」三种策略之一；未知类型对 member
# 默认不可见，避免以后新增通知时因为忘记配权限而泄露标题、摘要或链接。
ROOT_ONLY_KINDS = {
    "platform_alert", "purchase_requested",
}

# 钱袋子、经营风险和成员代拍板只发当前租户的 enabled root/owner。
# 每位收件人一行，绝不在没有老板时降级为 user_id=NULL 的全员广播。
BOSS_ONLY_KINDS = {
    "daily_digest", "schedule_paused", "schedule_failed",
    "learn_done", "learn_failed",
    "member_reviewed",
}

# 购买申请的客户状态只精确定向给发起 owner/root。它不属于普通业务板块，
# 也不能广播给企业成员。
PURCHASE_CUSTOMER_KINDS = {
    "purchase_contacted", "purchase_lost", "purchase_paid",
}

# 普通业务通知仍保留 schema48 的租户广播行，但读取和标记已读都必须经过
# 同一份板块白名单。未知 kind 默认只允许 root，避免新增类型自动外泄。
KIND_MODULES = {
    "awaiting": frozenset({"content"}),
    "done": frozenset({"content"}),
    "gate": frozenset({"content"}),
    "retro_due": frozenset({"content"}),
    "report": frozenset({"content"}),
    "pub": frozenset({"content"}),
    # ``video`` 来自 textvideo.tv_job（图文成片），任务中心和详情都归内容部。
    # 数字人摄影棚使用 avatar_job/SSE，不得把图文成片通知误投给 avatar-only 成员。
    "video": frozenset({"content"}),
}

# 保留旧常量名供外部诊断脚本兼容；语义是“需要精确定向的管理通知”。
OWNER_ONLY_KINDS = ROOT_ONLY_KINDS | BOSS_ONLY_KINDS


def _role_uids(tid: int, roles: tuple[str, ...]) -> list[int]:
    marks = ",".join("?" for _ in roles)
    return [int(row["id"]) for row in db.q(
        f"SELECT id FROM users WHERE tenant_id=? AND role IN ({marks}) "
        "AND COALESCE(enabled,1)=1 ORDER BY id",
        (int(tid), *roles),
    )]


def _root_uids(tid: int) -> list[int]:
    return _role_uids(tid, ("root",))


def _boss_uids(tid: int) -> list[int]:
    return _role_uids(tid, ("root", "owner"))


def _enabled_identity(user: dict | None) -> bool:
    """Middleware只会放 enabled 用户；显式 False 也在服务层再挡一次。"""
    return bool(user) and user.get("enabled", 1) not in (0, False)


def _allowed_kinds(user: dict | None) -> tuple[str, ...] | None:
    """None 表示本租户全部；空 tuple 表示没有任何通知类型权限。"""
    if not _enabled_identity(user):
        return ()
    role = str(user.get("role") or "")
    if role == "root":
        return None
    if role == "owner":
        return tuple(sorted(
            set(KIND_MODULES) | BOSS_ONLY_KINDS | PURCHASE_CUSTOMER_KINDS
        ))
    if role != "member":
        return ()
    modules = {
        str(module)
        for module in (user.get("modules") or [])
        if isinstance(module, str)
    }
    return tuple(sorted(
        kind for kind, required in KIND_MODULES.items()
        if required.intersection(modules)
    ))


def can_view(user: dict | None, item: dict) -> bool:
    """单条通知权限判定，读取与 mark-read 共用，默认拒绝未知 member 通知。"""
    if not _enabled_identity(user):
        return False
    try:
        uid = int(user.get("id") or 0)
    except (TypeError, ValueError):
        return False
    if uid <= 0:
        return False
    target = item.get("user_id")
    if target is not None:
        try:
            if int(target) != uid:
                return False
        except (TypeError, ValueError):
            return False
    kinds = _allowed_kinds(user)
    return kinds is None or str(item.get("kind") or "") in kinds


def _kind_sql(user: dict | None) -> tuple[str, tuple]:
    kinds = _allowed_kinds(user)
    if kinds is None:
        return "", ()
    if not kinds:
        return " AND 0", ()
    marks = ",".join("?" for _ in kinds)
    return f" AND kind IN ({marks})", tuple(kinds)


def unread_for_user(tid: int, user: dict | None, limit: int = 30) -> list[dict]:
    """只从 SQL 返回当前账号真正可看的未读通知，避免先取 30 条再过滤漏项。"""
    if not _enabled_identity(user):
        return []
    try:
        uid = int(user.get("id") or 0)
        page_limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        return []
    if uid <= 0:
        return []
    kind_sql, kind_params = _kind_sql(user)
    return db.q(
        "SELECT id,kind,title,body,link,created_at "
        "FROM notification WHERE tenant_id=? AND read_at IS NULL "
        "AND (user_id=? OR (user_id IS NULL "
        "AND instr(COALESCE(read_by,','), ','||?||',')=0))"
        f"{kind_sql} ORDER BY id DESC LIMIT ?",
        (int(tid), uid, str(uid), *kind_params, page_limit),
    )


def history_for_user(
        tid: int,
        user: dict | None,
        *,
        limit: int = 40,
        offset: int = 0,
        unread_only: bool = False) -> dict:
    """Return a paged, permission-filtered notification history."""
    if not _enabled_identity(user):
        return {"items": [], "total": 0}
    try:
        uid = int(user.get("id") or 0)
        page_limit = max(1, min(int(limit), 100))
        page_offset = max(0, int(offset))
    except (TypeError, ValueError):
        return {"items": [], "total": 0}
    if uid <= 0:
        return {"items": [], "total": 0}
    kind_sql, kind_params = _kind_sql(user)
    where = (
        "tenant_id=? AND (user_id=? OR user_id IS NULL)"
        f"{kind_sql}"
    )
    params: tuple = (int(tid), uid, *kind_params)
    if unread_only:
        where += (
            " AND read_at IS NULL AND (user_id=? OR "
            "(user_id IS NULL AND instr(COALESCE(read_by,','),"
            " ','||?||',')=0))"
        )
        params += (uid, str(uid))
    total_row = db.one(
        f"SELECT COUNT(*) n FROM notification WHERE {where}",
        params,
    ) or {}
    rows = db.q(
        "SELECT id,kind,title,body,link,created_at,user_id,read_at,read_by "
        f"FROM notification WHERE {where} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, page_limit, page_offset),
    )
    items = []
    for row in rows:
        targeted = row.get("user_id") is not None
        read_by = str(row.get("read_by") or ",")
        items.append({
            "id": int(row["id"]),
            "kind": row.get("kind") or "",
            "title": row.get("title") or "",
            "body": row.get("body") or "",
            "link": row.get("link") or "#/",
            "created_at": row.get("created_at"),
            "is_read": bool(
                row.get("read_at") is not None
                or (not targeted and f",{uid}," in read_by)
            ),
        })
    return {"items": items, "total": int(total_row.get("n") or 0)}


def _visible_unread_ids(
        tid: int, user: dict | None, ids: list[int]) -> list[int]:
    """校验调用方给出的 id，不能靠猜 id 标记跨模块/跨收件人的通知。"""
    if not ids or not _enabled_identity(user):
        return []
    marks = ",".join("?" for _ in ids)
    rows = db.q(
        "SELECT id,kind,user_id,read_by FROM notification "
        f"WHERE tenant_id=? AND read_at IS NULL AND id IN ({marks})",
        (int(tid), *ids),
    )
    uid = int(user.get("id") or 0)
    visible = []
    for row in rows:
        if not can_view(user, row):
            continue
        if row.get("user_id") is None:
            read_by = row.get("read_by") or ","
            if f",{uid}," in read_by:
                continue
        visible.append(int(row["id"]))
    return visible


def mark_read(tid: int, user: dict | None, ids: list[int]) -> int:
    """按账号标记已读；隐藏通知即使 id 被猜中也不会发生任何写入。"""
    visible_ids = _visible_unread_ids(tid, user, ids)
    if not visible_ids:
        return 0
    uid = int(user.get("id") or 0)
    marks = ",".join("?" for _ in visible_ids)
    now = time.time()
    changed = 0
    with db.atomic() as connection:
        changed += connection.execute(
            f"UPDATE notification SET read_at=?,updated_at=? "
            f"WHERE tenant_id=? AND read_at IS NULL AND user_id=? "
            f"AND id IN ({marks})",
            (now, now, int(tid), uid, *visible_ids),
        ).rowcount
        changed += connection.execute(
            f"UPDATE notification SET "
            f"read_by=COALESCE(read_by,',')||?||',',updated_at=? "
            f"WHERE tenant_id=? AND read_at IS NULL AND user_id IS NULL "
            f"AND instr(COALESCE(read_by,','), ','||?||',')=0 "
            f"AND id IN ({marks})",
            (str(uid), now, int(tid), str(uid), *visible_ids),
        ).rowcount
    return int(changed)


def mark_all_read(tid: int, user: dict | None) -> int:
    """管理账号的一键全读只处理自己的定向行和可见业务广播。"""
    if not _enabled_identity(user) or user.get("role") not in ("root", "owner"):
        return 0
    uid = int(user.get("id") or 0)
    kind_sql, kind_params = _kind_sql(user)
    now = time.time()
    changed = 0
    with db.atomic() as connection:
        changed += connection.execute(
            "UPDATE notification SET read_at=?,updated_at=? "
            "WHERE tenant_id=? AND read_at IS NULL AND user_id=?"
            f"{kind_sql}",
            (now, now, int(tid), uid, *kind_params),
        ).rowcount
        changed += connection.execute(
            "UPDATE notification SET read_at=?,updated_at=? "
            "WHERE tenant_id=? AND read_at IS NULL AND user_id IS NULL"
            f"{kind_sql}",
            (now, now, int(tid), *kind_params),
        ).rowcount
    return int(changed)



def record(
        tid: int,
        kind: str,
        payload: dict,
        *,
        target_user_id: int | None = None) -> int | None:
    """Persist the notification even when no external webhook is configured."""
    try:
        title, body, link = _inbox_item(kind, payload)
        try:
            job_id = int((payload or {}).get("job_id") or 0) or None
        except (TypeError, ValueError):
            job_id = None
        targets = [None]
        if target_user_id is not None:
            target = db.one(
                "SELECT id FROM users WHERE id=? AND tenant_id=? "
                "AND COALESCE(enabled,1)=1",
                (int(target_user_id), int(tid)),
            )
            if not target:
                return None
            targets = [int(target["id"])]
        elif kind in ROOT_ONLY_KINDS:
            targets = _root_uids(tid)
            if not targets:
                return None
        elif kind in BOSS_ONLY_KINDS:
            targets = _boss_uids(tid)
            if not targets:
                return None
        first = None
        for target in targets:
            row_id = db.insert(
                "notification",
                {
                    "tenant_id": int(tid),
                    "job_id": job_id,
                    "kind": str(kind or "event")[:40],
                    "title": title,
                    "body": body,
                    "link": link,
                    "user_id": target,
                },
            )
            first = first if first is not None else row_id
        return first
    except Exception as exc:
        log.error(
            "站内通知落库失败 tid=%s kind=%s error_type=%s",
            tid,
            kind,
            type(exc).__name__,
        )
        return None


def send_sync(tid: int, kind: str, payload: dict) -> bool:
    """同步发送(调度器线程用);没配 webhook 静默跳过."""
    url = get_webhook(tid)
    if not url:
        return False
    try:
        r = httpx.post(url, json={"msgtype": "markdown",
                                  "markdown": {"content": build_msg(kind, payload)}}, timeout=8)
        d = r.json()
        if d.get("errcode") != 0:
            log.warning(
                "企微通知失败 tid=%s errcode=%s",
                tid,
                d.get("errcode"),
            )
            return False
        return True
    except Exception as exc:
        log.warning(
            "企微通知异常 tid=%s error_type=%s",
            tid,
            type(exc).__name__,
        )
        return False


def push(tid: int, kind: str, payload: dict):
    """站内必达；配置企微时再异步发送外部提醒。"""
    record(tid, kind, payload)
    if not get_webhook(tid):
        return
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, send_sync, tid, kind, payload)
    except RuntimeError:
        send_sync(tid, kind, payload)


async def push_async(tid: int, kind: str, payload: dict):
    """Async entrypoint: persist off-loop and keep webhook I/O out of DB workers."""
    await db.arun(record, tid, kind, payload)
    if not await db.arun(get_webhook, tid):
        return
    asyncio.get_running_loop().run_in_executor(
        None,
        send_sync,
        tid,
        kind,
        payload,
    )


async def test_send(tid: int) -> dict:
    ok = await asyncio.to_thread(send_sync, tid, "report",
                                 {"report_name": "通知测试", "summary": "看到这条说明打通了!以后等拍板/交付/复盘提醒都会推到这里。",
                                  "link": "#/"})
    if not ok:
        raise ValueError("发送失败:检查 Webhook 地址是否完整、机器人是否还在群里")
    return {"ok": True, "note": "已发送测试消息,去群里看"}
