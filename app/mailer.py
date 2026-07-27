"""客资邮件通知(V10.3):访客留资后发到老板邮箱.

SMTP:老板需在邮箱服务商开启「SMTP服务」拿到授权码,连同发件账号(smtp_user)与
收件地址(lead_email)一起填到管理后台。任一项未配置时跳过发信并告警——
收件地址绝不能有内置默认值,否则漏配即把客户客资发往第三方邮箱。
客资本身始终入库,权限管理页可见,不会因发信失败丢失。
"""
import asyncio
import logging
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText

from . import db, secureconfig

log = logging.getLogger("mailer")

DEFAULT_SMTP_HOST = "smtp.qq.com"
DEFAULT_SMTP_PORT = 465


def _send(subject: str, body: str):
    code = secureconfig.get_secret("smtp_authcode")
    if not code:
        log.info("smtp 未配置授权码,跳过邮件(客资已入库)")
        return
    user = (db.get_setting("smtp_user") or "").strip()
    to = (db.get_setting("lead_email") or "").strip()
    if not user or not to:
        # 明确告警而不是回退到某个内置地址:客资是客户的商业数据。
        log.warning(
            "smtp 已配授权码但未配置发件账号或收件地址,跳过邮件(客资已入库);"
            "请在管理后台补齐 smtp_user 与 lead_email"
        )
        return
    host = (db.get_setting("smtp_host") or "").strip() or DEFAULT_SMTP_HOST
    try:
        port = int(db.get_setting("smtp_port") or DEFAULT_SMTP_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_SMTP_PORT
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL(host, port, timeout=20) as s:
        s.login(user, code)
        s.sendmail(user, [to], msg.as_string())
    log.info("lead mail sent")


async def notify_feedback(who: str, text: str, contact: str):
    body = (f"「派活」用户反馈/建议\n\n来自:{who}\n联系方式:{contact or '-'}\n"
            f"时间:{time.strftime('%Y-%m-%d %H:%M')}\n\n内容:\n{text}")
    try:
        await asyncio.to_thread(_send, "💬 派活用户反馈:" + who[:20], body)
    except Exception as exc:
        log.warning(
            "feedback mail failed error_type=%s", type(exc).__name__
        )


async def notify_apply(phone: str, name: str, company: str, note: str):
    body = (f"「派活」新账号开通申请!\n\n手机号:{phone}\n称呼:{name or '-'}\n"
            f"企业/行业:{company or '-'}\n想用来做什么:{note or '-'}\n"
            f"时间:{time.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"去权限管理页开租户/账号,然后联系 TA 交付账号密码。")
    try:
        await asyncio.to_thread(_send, "📝 派活开通申请:" + phone, body)
    except Exception as exc:
        log.warning(
            "apply mail failed error_type=%s", type(exc).__name__
        )


async def notify_lead(phone: str, name: str, company: str):
    body = (f"「派活」新客资!\n\n手机号:{phone}\n称呼:{name or '-'}\n"
            f"企业/行业:{company or '-'}\n时间:{time.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"TA 已进入参观模式逛产品,尽快联系跟进。\n(权限管理页也能看到全部客资)")
    try:
        await asyncio.to_thread(_send, "🎁 派活新客资:" + phone, body)
    except Exception as exc:
        log.warning(
            "lead mail failed error_type=%s", type(exc).__name__
        )
