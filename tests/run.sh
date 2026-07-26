#!/bin/bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${CONTENTCREW_PYTHON:-$ROOT/venv/bin/python}"
TARGET="${1:-http://127.0.0.1:8899}"

case "$TARGET" in
  https://paihuo.ai*|https://www.paihuo.ai*)
    if [[ "${SMOKE_CONFIRM_PRODUCTION:-}" != "YES" ]]; then
      echo "拒绝默认连接生产：请显式设置 SMOKE_CONFIRM_PRODUCTION=YES" >&2
      exit 2
    fi
    ;;
esac

cd "$ROOT"
"$PYTHON" -m py_compile app/*.py app/skills/*.py
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m unittest discover -s tests -p 'test_*.py'
"$PYTHON" tests/smoke.py "$TARGET"
