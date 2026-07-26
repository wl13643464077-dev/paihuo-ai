#!/bin/bash
set -euo pipefail

APP_ROOT="${CONTENTCREW_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
PYTHON="${CONTENTCREW_PYTHON:-$APP_ROOT/venv/bin/python}"
HOST="${CONTENTCREW_HOST:-127.0.0.1}"
PORT="${CONTENTCREW_PORT:-8899}"

cd "$APP_ROOT"
exec "$PYTHON" -m uvicorn app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --proxy-headers \
  --forwarded-allow-ips "${CONTENTCREW_FORWARDED_ALLOW_IPS:-127.0.0.1}" \
  --timeout-graceful-shutdown 15
