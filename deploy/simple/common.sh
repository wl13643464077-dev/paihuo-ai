# shellcheck shell=bash
# 派活 简易部署通道：deploy.sh / rollback.sh 共用的配置与函数。
# 只被 source，不单独执行。所有路径都可以用环境变量覆盖（测试/演练用临时目录）。

# ---------------- 配置（默认值就是生产服务器上的推荐布局） ----------------
PAIHUO_BASE="${PAIHUO_BASE:-/srv/paihuo}"                 # 代码：releases/ 和 current
RELEASES_DIR="$PAIHUO_BASE/releases"
CURRENT_LINK="$PAIHUO_BASE/current"
HISTORY_LOG="$PAIHUO_BASE/deploy-history.log"
LOCK_FILE="$PAIHUO_BASE/.deploy.lock"
DATA_DIR="${PAIHUO_DATA_DIR:-/var/lib/paihuo/data}"      # 运行数据（数据库、素材）
DB_PATH="${PAIHUO_DB_PATH:-$DATA_DIR/contentcrew.db}"
BACKUP_DIR="${PAIHUO_BACKUP_DIR:-/var/backups/paihuo}"
ENV_FILE="${PAIHUO_ENV_FILE:-/etc/paihuo/paihuo.env}"
PUB_DIR="${PAIHUO_PUB_DIR:-/srv/paihuo-pub}"
SERVICE="${PAIHUO_SERVICE:-paihuo}"
UNIT_FILE="${PAIHUO_UNIT_FILE:-/etc/systemd/system/$SERVICE.service}"
PORT="${PAIHUO_PORT:-8899}"
APP_USER="${PAIHUO_APP_USER:-paihuo}"
PYTHON_BIN="${PAIHUO_PYTHON:-python3}"                     # 建虚拟环境、跑备份用的 Python
KEEP_RELEASES="${PAIHUO_KEEP_RELEASES:-5}"
MIN_FREE_MB="${PAIHUO_MIN_FREE_MB:-2048}"
SMOKE_TIMEOUT="${PAIHUO_SMOKE_TIMEOUT:-120}"
META_NAME=".paihuo-release"                                 # 每个 release 目录里的发布记录

# 本脚本所在仓库的根目录：备份/恢复用这里的 deploy/backup_db.py、deploy/verify_backup.py。
OPS_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

DRY_RUN=0

# ---------------- 输出 ----------------
say()  { printf '%s\n' "$*"; }
info() { printf '  - %s\n' "$*"; }
warn() { printf '  [警告] %s\n' "$*" >&2; }
die()  { printf '[失败] %s\n' "$*" >&2; exit 1; }
step() { printf '\n==== [%s] %s ====\n' "$1" "$2"; }

# 演练模式下只打印将要执行的命令
run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '    (演练，不执行) %s\n' "$*"
  else
    "$@"
  fi
}

now_local() { TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S'; }

history_log() {
  [[ "$DRY_RUN" -eq 1 ]] && return 0
  mkdir -p -- "$PAIHUO_BASE"
  printf '%s\t%s\n' "$(now_local)" "$*" >>"$HISTORY_LOG" || true
}

require_root() {
  if [[ "$DRY_RUN" -eq 1 || "${PAIHUO_ALLOW_NONROOT:-0}" == "1" ]]; then
    return 0
  fi
  [[ "$EUID" -eq 0 ]] || die "请用 root 运行（sudo bash $0 ...），或先加 --dry-run 演练"
}

acquire_lock() {
  [[ "$DRY_RUN" -eq 1 ]] && return 0
  mkdir -p -- "$PAIHUO_BASE"
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "另一个发布/回滚正在进行（锁文件 $LOCK_FILE），请等它结束"
}

# 以应用账号身份执行（数据库和锁文件必须归应用账号所有，否则服务起不来）
as_app_user() {
  if [[ "$(id -un)" == "$APP_USER" ]]; then
    "$@"
  else
    runuser -u "$APP_USER" -- "$@"
  fi
}

# ---------------- 环境文件（只看有没有、长度够不够，绝不打印值） ----------------
env_value_length() {
  local key="$1" line
  [[ -r "$ENV_FILE" ]] || { echo 0; return 0; }
  line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  echo "${#line}"
}

env_has_key() {
  [[ -r "$ENV_FILE" ]] && grep -Eq "^[[:space:]]*$1=" "$ENV_FILE"
}

# ---------------- 版本与数据库 ----------------
current_release() {
  if [[ -L "$CURRENT_LINK" ]]; then
    basename -- "$(readlink -- "$CURRENT_LINK")"
  fi
}

meta_get() {  # meta_get <release目录> <键>
  local file="$1/$META_NAME"
  [[ -f "$file" ]] || return 0
  grep -E "^$2=" "$file" | tail -n 1 | cut -d= -f2- || true
}

# 某个 release 的代码最高支持的数据库版本（读 app/db.py 里的 LATEST_SCHEMA_VERSION）
code_schema_version() {  # code_schema_version <db.py 路径或 ->
  grep -Eo '^LATEST_SCHEMA_VERSION[[:space:]]*=[[:space:]]*[0-9]+' "$1" | grep -Eo '[0-9]+$' | head -n 1 || true
}

# 数据库文件当前的版本号；文件不存在输出 none
db_schema_version() {  # db_schema_version <数据库路径>
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo none
    return 0
  fi
  "$PYTHON_BIN" - "$path" <<'PY'
import sqlite3, sys
from pathlib import Path
uri = Path(sys.argv[1]).resolve().as_uri() + "?mode=ro"
with sqlite3.connect(uri, uri=True, timeout=30) as c:
    print(c.execute("PRAGMA user_version").fetchone()[0])
PY
}

# ---------------- 服务 ----------------
have_systemctl() { command -v systemctl >/dev/null 2>&1; }

service_active() {
  have_systemctl && systemctl is-active --quiet "$SERVICE" 2>/dev/null
}

service_stop() {
  run systemctl stop "$SERVICE"
  [[ "$DRY_RUN" -eq 1 ]] && return 0
  local i
  for i in $(seq 1 30); do
    service_active || return 0
    sleep 1
  done
  die "服务 $SERVICE 30 秒内没有停下来"
}

service_start() { run systemctl start "$SERVICE"; }
service_restart() { run systemctl restart "$SERVICE"; }

show_recent_logs() {
  if command -v journalctl >/dev/null 2>&1; then
    say "---- 最近 40 行服务日志（journalctl -u $SERVICE）----"
    journalctl -u "$SERVICE" -n 40 --no-pager 2>/dev/null || true
    say "----"
  fi
}

# 端口上有没有程序在监听（不依赖 ss/netstat）
port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null
}

# ---------------- 切换 current 软链接（原子） ----------------
switch_current() {  # switch_current <release-id>
  local id="$1" tmp="$PAIHUO_BASE/.current.tmp.$$"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '    (演练，不执行) current -> releases/%s\n' "$id"
    return 0
  fi
  [[ -d "$RELEASES_DIR/$id" ]] || die "release 不存在: $RELEASES_DIR/$id"
  ln -sfn -- "releases/$id" "$tmp"
  mv -Tf -- "$tmp" "$CURRENT_LINK"
}

# ---------------- 冒烟检查 ----------------
http_code() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1" 2>/dev/null || true
}

# wait_http <url> <秒>：等到返回 200，成功返回 0
wait_http() {
  local url="$1" deadline code
  deadline=$(( $(date +%s) + $2 ))
  while :; do
    code="$(http_code "$url")"
    [[ "$code" == "200" ]] && return 0
    if (( $(date +%s) >= deadline )); then
      LAST_HTTP_CODE="$code"
      return 1
    fi
    sleep 2
  done
}

# smoke_check <deep 是否必须通过 1/0>
smoke_check() {
  local deep_required="$1" base="http://127.0.0.1:$PORT"
  LAST_HTTP_CODE=""
  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "(演练) 将检查 $base/healthz、$base/healthz?deep=1、$base/login 都返回 200"
    return 0
  fi
  if ! wait_http "$base/healthz" "$SMOKE_TIMEOUT"; then
    warn "/healthz 在 ${SMOKE_TIMEOUT} 秒内没有返回 200（最后一次: ${LAST_HTTP_CODE:-无响应}）"
    return 1
  fi
  info "/healthz 正常"
  # 后台循环刚启动时可能还没登记心跳，给 60 秒
  if wait_http "$base/healthz?deep=1" 60; then
    info "/healthz?deep=1 正常（后台循环都在跑）"
  elif [[ "$deep_required" == "1" ]]; then
    warn "/healthz?deep=1 没有返回 200（最后一次: ${LAST_HTTP_CODE:-无响应}），有后台循环没跑起来"
    return 1
  else
    warn "/healthz?deep=1 没有返回 200（最后一次: ${LAST_HTTP_CODE:-无响应}），先放行，请尽快查看日志"
  fi
  if ! wait_http "$base/login" 20; then
    warn "登录页 /login 没有返回 200（最后一次: ${LAST_HTTP_CODE:-无响应}）"
    return 1
  fi
  info "登录页 /login 正常"
  return 0
}

# ---------------- 数据库恢复 ----------------
# restore_db <备份文件>：服务必须已停。
# 先把备份校验并复制成数据目录里的临时文件，再把现在的库(含 -wal/-shm)挪进隔离目录，
# 最后原子改名替换。被换下来的库留在隔离目录里，确认没问题前不要删。
restore_db() {
  local backup="$1" stamp prepared quarantine suffix group
  [[ -f "$backup" ]] || die "备份文件不存在: $backup"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  prepared="$DATA_DIR/.restore-$stamp.db"
  quarantine="$DATA_DIR/rollback-quarantine-$stamp"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "(演练) 将校验 $backup 并替换 $DB_PATH，原库挪到 $quarantine"
    return 0
  fi
  if service_active; then
    die "服务 $SERVICE 还在运行，不能恢复数据库"
  fi
  info "校验备份并准备恢复: $backup"
  (umask 077; cd "$OPS_ROOT" && PYTHONPATH="$OPS_ROOT" PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON_BIN" -m deploy.verify_backup "$backup" --restore-to "$prepared" >/dev/null) \
    || die "备份校验失败，没有动线上数据库: $backup"
  install -d -m 0700 -- "$quarantine"
  for suffix in "" -wal -shm -journal; do
    if [[ -e "$DB_PATH$suffix" ]]; then
      mv -- "$DB_PATH$suffix" "$quarantine/"
    fi
  done
  group="$(id -gn "$APP_USER")"
  chown "$APP_USER:$group" "$prepared"
  chmod 0600 "$prepared"
  mv -T -- "$prepared" "$DB_PATH"
  sync
  RESTORED_QUARANTINE="$quarantine"
  info "数据库已恢复；换下来的库保存在 $quarantine"
}
