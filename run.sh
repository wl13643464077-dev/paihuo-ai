#!/bin/bash
set -euo pipefail

APP_ROOT="${CONTENTCREW_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
# 生产制品用 venv/；README 教本地开发建 .venv/。两者都认，否则照 README 操作必然启动失败。
if [ -n "${CONTENTCREW_PYTHON:-}" ]; then
  PYTHON="$CONTENTCREW_PYTHON"
elif [ -x "$APP_ROOT/venv/bin/python" ]; then
  PYTHON="$APP_ROOT/venv/bin/python"
elif [ -x "$APP_ROOT/.venv/bin/python" ]; then
  PYTHON="$APP_ROOT/.venv/bin/python"
else
  echo "找不到虚拟环境:请先建 venv/ 或 .venv/,或设 CONTENTCREW_PYTHON" >&2
  exit 1
fi
HOST="${CONTENTCREW_HOST:-127.0.0.1}"
PORT="${CONTENTCREW_PORT:-8899}"

# 硬约束:只能跑 1 个 worker。引擎队列、任务锁、实时推送都在进程内存里,
# 多 worker 会重复认领任务、重复扣点。应用启动时还会对数据目录加独占实例锁,
# 第二个进程会直接拒绝启动。这里显式 --workers 1,防止 WEB_CONCURRENCY 等
# 环境变量把 uvicorn 悄悄变成多进程。
if [ -n "${CONTENTCREW_WORKERS:-}" ] && [ "${CONTENTCREW_WORKERS}" != "1" ]; then
  echo "派活只能以单进程运行(CONTENTCREW_WORKERS 必须为 1 或不设)" >&2
  exit 1
fi

cd "$APP_ROOT"
exec "$PYTHON" -m uvicorn app.main:app \
  --workers 1 \
  --host "$HOST" \
  --port "$PORT" \
  --proxy-headers \
  --forwarded-allow-ips "${CONTENTCREW_FORWARDED_ALLOW_IPS:-127.0.0.1}" \
  --timeout-graceful-shutdown 15
