#!/usr/bin/env python3
"""systemd OnFailure 通知；仅接受企业微信机器人地址，不输出 webhook。"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request


WEBHOOK_RE = re.compile(
    r"^https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9_-]+$"
)


def send_failure_alert(unit: str) -> bool:
    webhook = (os.environ.get("PAIHUO_ALERT_WEBHOOK") or "").strip()
    if not webhook:
        print("paihuo failure alert skipped: webhook is not configured", file=sys.stderr)
        return False
    if not WEBHOOK_RE.fullmatch(webhook):
        raise ValueError("PAIHUO_ALERT_WEBHOOK is not an approved WeCom webhook")
    content = (
        "**🚨 派活 AI 服务异常**\n"
        f"systemd 单元 `{(unit or 'contentcrew.service')[:120]}` 启动失败或异常退出。\n"
        "请立即检查服务器上的 `systemctl status` 与 `journalctl`。"
    )
    request = urllib.request.Request(
        webhook,
        data=json.dumps(
            {"msgtype": "markdown", "markdown": {"content": content}},
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read(4096))
    if payload.get("errcode") != 0:
        raise RuntimeError(f"WeCom rejected alert: errcode={payload.get('errcode')}")
    return True


def main() -> int:
    try:
        send_failure_alert(sys.argv[1] if len(sys.argv) > 1 else "contentcrew.service")
        return 0
    except Exception as exc:
        print(f"paihuo failure alert failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
