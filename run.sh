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

cd "$APP_ROOT"
exec "$PYTHON" -m uvicorn app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --proxy-headers \
  --forwarded-allow-ips "${CONTENTCREW_FORWARDED_ALLOW_IPS:-127.0.0.1}" \
  --timeout-graceful-shutdown 15
