#!/usr/bin/env bash
# 派活 简易部署：在一台 Ubuntu 服务器上，从一个 git 提交（或一个解压好的代码目录）发布新版本。
#
#   ① 预检（Python 版本、磁盘、密钥文件、端口、单进程）
#   ② 在线备份数据库 + 素材（调用 deploy/backup_db.py）
#   ③ 在 releases/<时间-提交号>/ 放代码、建虚拟环境、按 requirements.lock.txt 装依赖
#   ④ 停服 → 停服快照 → 用新代码只做数据库迁移、不启动
#   ⑤ 切换 current 软链接 → systemctl restart
#   ⑥ 冒烟：/healthz、/healthz?deep=1、登录页
#   任何一步失败都自动回滚，顺序见 rollback_after_failure 的注释。
#
# 用法（在服务器上，root）：
#   sudo bash deploy/simple/deploy.sh --ref origin/main          # 从本仓库的某个提交发布
#   sudo bash deploy/simple/deploy.sh --repo /srv/paihuo/src --ref v1.2.0
#   sudo bash deploy/simple/deploy.sh --source-dir /tmp/paihuo-code   # 从解压好的目录发布
#   bash deploy/simple/deploy.sh --dry-run --ref HEAD            # 只演练：做预检、打印计划，不改任何东西
#
# 详细说明见 deploy/simple/README.md。
set -euo pipefail
umask 022   # release 目录要让应用账号能读；数据库/备份相关步骤会单独收紧到 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/simple/common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'EOF'
用法: deploy.sh [选项]
  --ref <提交/分支/标签>   从 git 仓库发布的版本（默认 HEAD）
  --repo <路径>            git 仓库位置（默认：本脚本所在的仓库）
  --source-dir <路径>      改为从一个解压好的代码目录发布（不用 git）
  --dry-run                只演练：做全部预检并打印每一步计划，不改任何东西，不需要 root
  --deep-optional          /healthz?deep=1 不通过时只警告、不回滚
  --skip-backup-online     跳过第②步的在线备份（停服快照照做，仍可自动回滚）
  -h, --help               显示本帮助
可用环境变量覆盖路径：PAIHUO_BASE PAIHUO_DATA_DIR PAIHUO_DB_PATH PAIHUO_BACKUP_DIR
PAIHUO_ENV_FILE PAIHUO_PUB_DIR PAIHUO_SERVICE PAIHUO_PORT PAIHUO_APP_USER PAIHUO_PYTHON
PAIHUO_PIP_INDEX_URL（国内镜像，如 https://mirrors.aliyun.com/pypi/simple/）
EOF
}

REPO=""
REF="HEAD"
SOURCE_DIR=""
DEEP_REQUIRED=1
ONLINE_BACKUP=1
while (($#)); do
  case "$1" in
    --ref) REF="${2:?--ref 需要一个值}"; shift 2 ;;
    --repo) REPO="${2:?--repo 需要一个值}"; shift 2 ;;
    --source-dir) SOURCE_DIR="${2:?--source-dir 需要一个值}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --deep-optional) DEEP_REQUIRED=0; shift ;;
    --skip-backup-online) ONLINE_BACKUP=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "不认识的参数: $1" ;;
  esac
done
if [[ -n "$SOURCE_DIR" && -n "$REPO" ]]; then
  die "--repo 和 --source-dir 只能选一个"
fi
if [[ -z "$SOURCE_DIR" && -z "$REPO" ]]; then
  REPO="$OPS_ROOT"
fi

# ---------------- 失败回滚需要的状态 ----------------
PHASE="preflight"
RELEASE_ID=""
RELEASE_DIR=""
CREATED_RELEASE=0
PREV_RELEASE=""
SERVICE_WAS_ACTIVE=0
STOPPED=0
DB_EXISTED=0
SNAPSHOT=""
ONLINE_BACKUP_PATH=""
MIGRATION_STARTED=0
SWITCHED=0
FAIL_REASON=""

fail() { FAIL_REASON="$*"; die "$*"; }

# 失败后的回滚顺序（数据库迁移不可逆，所以一定是“先恢复数据库，再切回旧代码”）：
#   1. 停掉服务（此时可能是新代码在跑，或根本没起来）
#   2. 用第④步的停服快照恢复数据库——旧代码不认新版本的数据库，会拒绝启动；
#      反过来如果先切旧代码再恢复，旧代码会在新库上启动失败或写坏数据
#   3. 把 current 切回上一个 release
#   4. 启动服务并冒烟
# 如果还没停服就失败了（预检/备份/装依赖），线上服务一直没动，只清理半成品目录。
rollback_after_failure() {
  local status="$1"
  set +e
  trap - EXIT
  say ""
  say "!!!! 部署失败（阶段: $PHASE）${FAIL_REASON:+：$FAIL_REASON}"
  if [[ "$SWITCHED" -eq 1 || "$MIGRATION_STARTED" -eq 1 ]]; then
    show_recent_logs
    say "开始自动回滚：先恢复数据库，再切回旧代码"
    ( service_stop ) || warn "停服失败，请手动 systemctl stop $SERVICE 后再按 README 手动回滚"
    if [[ -n "$SNAPSHOT" ]]; then
      if ( restore_db "$SNAPSHOT" ); then
        info "数据库已恢复到停服快照 $SNAPSHOT"
      else
        warn "数据库恢复失败！服务保持停止。请按 deploy/simple/README.md「手动回滚」处理，快照: $SNAPSHOT"
        history_log "deploy-rollback-failed $RELEASE_ID db-restore"
        exit "$status"
      fi
    elif [[ "$DB_EXISTED" -eq 0 && -e "$DB_PATH" ]]; then
      local q
      q="$DATA_DIR/rollback-quarantine-$(date -u +%Y%m%dT%H%M%SZ)"
      mkdir -p -m 0700 -- "$q"
      mv -- "$DB_PATH"* "$q/" 2>/dev/null
      info "首次部署失败：新建的数据库已挪到 $q"
    fi
    if [[ "$SWITCHED" -eq 1 ]]; then
      if [[ -n "$PREV_RELEASE" ]]; then
        ( switch_current "$PREV_RELEASE" ) && info "current 已切回 $PREV_RELEASE"
      else
        rm -f -- "$CURRENT_LINK"
        info "首次部署失败：已去掉 current 软链接"
      fi
    fi
  fi
  if [[ ( "$STOPPED" -eq 1 || "$SWITCHED" -eq 1 ) && -n "$PREV_RELEASE" && -L "$CURRENT_LINK" ]]; then
    say "启动旧版本 $PREV_RELEASE ..."
    ( service_restart )
    if smoke_check 0; then
      say "旧版本已恢复服务。"
      history_log "deploy-failed $RELEASE_ID phase=$PHASE rolled-back-to=$PREV_RELEASE"
    else
      show_recent_logs
      warn "旧版本也没能通过冒烟！请立刻按 deploy/simple/README.md「常见故障」排查"
      history_log "deploy-failed $RELEASE_ID phase=$PHASE rollback-smoke-failed"
    fi
  else
    history_log "deploy-failed $RELEASE_ID phase=$PHASE service-untouched"
  fi
  if [[ "$CREATED_RELEASE" -eq 1 && -n "$RELEASE_DIR" && "$(current_release)" != "$RELEASE_ID" ]]; then
    rm -rf -- "$RELEASE_DIR"
    info "已删除没用上的新版本目录 $RELEASE_DIR"
  fi
  exit "$status"
}

on_exit() {
  local status=$?
  if [[ "$status" -ne 0 && "$DRY_RUN" -eq 0 ]]; then
    rollback_after_failure "$status"
  fi
}
trap on_exit EXIT
trap 'exit 130' INT TERM HUP

say "派活 简易部署 $( [[ "$DRY_RUN" -eq 1 ]] && echo '（演练模式：不会改动任何东西）')"
require_root
acquire_lock

# =====================================================================
step "1/6" "预检"
# =====================================================================
PROBLEMS=0
problem() { printf '  [不通过] %s\n' "$*" >&2; PROBLEMS=$((PROBLEMS + 1)); }

for tool in curl flock tar grep; do
  command -v "$tool" >/dev/null 2>&1 || problem "缺少命令 $tool（apt install $tool）"
done
if [[ "$DRY_RUN" -eq 0 ]] && ! have_systemctl; then
  problem "缺少 systemctl，这台机器不是 systemd 系统"
fi

# Python 版本与 venv 模块
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  problem "找不到 Python（$PYTHON_BIN）"
else
  PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  if "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    info "Python $PY_VERSION"
  else
    problem "Python 版本 $PY_VERSION 太低，至少要 3.11（推荐 Ubuntu 24.04 自带的 3.12）"
  fi
  "$PYTHON_BIN" -c 'import venv, ensurepip' 2>/dev/null \
    || problem "Python 缺少 venv 模块（apt install python3-venv）"
fi

# 应用账号
if id -u "$APP_USER" >/dev/null 2>&1; then
  info "应用账号 $APP_USER 存在"
else
  problem "应用账号 $APP_USER 不存在（useradd --system --user-group --home-dir /var/lib/paihuo --shell /usr/sbin/nologin $APP_USER）"
fi

# 代码来源
if [[ -n "$SOURCE_DIR" ]]; then
  SOURCE_DIR="$(cd -- "$SOURCE_DIR" 2>/dev/null && pwd)" || die "代码目录不存在: $SOURCE_DIR"
  for f in app/db.py app/main.py run.sh requirements.lock.txt; do
    [[ -f "$SOURCE_DIR/$f" ]] || problem "代码目录缺少 $f: $SOURCE_DIR"
  done
  COMMIT_SHORT="dir"
  if git -C "$SOURCE_DIR" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    COMMIT_SHORT="$(git -C "$SOURCE_DIR" rev-parse --short=8 HEAD)"
  fi
  COMMIT_FULL="$COMMIT_SHORT"
  NEW_SCHEMA="$(code_schema_version "$SOURCE_DIR/app/db.py")"
  SOURCE_DESC="目录 $SOURCE_DIR"
else
  command -v git >/dev/null 2>&1 || die "缺少 git（apt install git）"
  GIT=(git -c "safe.directory=$REPO" -C "$REPO")
  COMMIT_FULL="$("${GIT[@]}" rev-parse --verify -q "$REF^{commit}" 2>/dev/null)" \
    || die "在 $REPO 里找不到提交 $REF（先 git fetch？）"
  COMMIT_SHORT="${COMMIT_FULL:0:8}"
  NEW_SCHEMA="$("${GIT[@]}" show "$COMMIT_FULL:app/db.py" | code_schema_version -)"
  SOURCE_DESC="git $REPO @ $REF ($COMMIT_SHORT) $("${GIT[@]}" log -1 --format=%s "$COMMIT_FULL" | cut -c1-60)"
fi
[[ -n "$NEW_SCHEMA" ]] || problem "读不到新代码支持的数据库版本（app/db.py 的 LATEST_SCHEMA_VERSION）"
RELEASE_ID="$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S)-$COMMIT_SHORT"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
info "要发布: $SOURCE_DESC"
info "新版本目录: $RELEASE_DIR"
[[ -e "$RELEASE_DIR" ]] && problem "目录已存在: $RELEASE_DIR"

# 当前版本与数据库
PREV_RELEASE="$(current_release || true)"
if [[ -n "$PREV_RELEASE" ]]; then
  [[ -d "$RELEASES_DIR/$PREV_RELEASE" ]] || problem "current 指向的目录不存在: $PREV_RELEASE"
  info "线上版本: $PREV_RELEASE"
else
  info "线上版本: 无（首次部署）"
fi
DB_SCHEMA="$(db_schema_version "$DB_PATH")" || problem "读不了数据库版本: $DB_PATH"
if [[ "$DB_SCHEMA" == "none" ]]; then
  info "数据库: 还没有（首次部署会新建 $DB_PATH）"
else
  DB_EXISTED=1
  info "数据库: $DB_PATH（v$DB_SCHEMA）→ 新代码支持 v${NEW_SCHEMA:-?}"
  if [[ -n "$NEW_SCHEMA" && "$DB_SCHEMA" =~ ^[0-9]+$ ]]; then
    if (( DB_SCHEMA > NEW_SCHEMA )); then
      problem "数据库 v$DB_SCHEMA 比新代码支持的 v$NEW_SCHEMA 还新，不能发布旧代码（要回退请用 rollback.sh 并恢复备份）"
    elif (( DB_SCHEMA < NEW_SCHEMA )); then
      info "本次会把数据库从 v$DB_SCHEMA 升到 v$NEW_SCHEMA，升级后旧代码不能再直接用这个库（失败会自动用停服快照恢复）"
    fi
  fi
fi

# 磁盘空间
free_mb() { df -Pm -- "$1" 2>/dev/null | awk 'NR==2 {print $4}'; }
existing_parent() { local p="$1"; while [[ ! -e "$p" ]]; do p="$(dirname -- "$p")"; done; echo "$p"; }
BASE_FREE="$(free_mb "$(existing_parent "$PAIHUO_BASE")")"
if [[ -n "$BASE_FREE" ]] && (( BASE_FREE < MIN_FREE_MB )); then
  problem "代码目录所在磁盘只剩 ${BASE_FREE}MB，至少要 ${MIN_FREE_MB}MB（新虚拟环境约 1GB）"
else
  info "代码目录磁盘剩余 ${BASE_FREE:-?}MB"
fi
if [[ "$DB_EXISTED" -eq 1 ]]; then
  DB_MB=$(( $(stat -c %s -- "$DB_PATH") / 1048576 + 1 ))
  NEED_MB=$(( DB_MB * 2 + 512 ))
  BACKUP_FREE="$(free_mb "$(existing_parent "$BACKUP_DIR")")"
  if [[ -n "$BACKUP_FREE" ]] && (( BACKUP_FREE < NEED_MB )); then
    problem "备份目录所在磁盘只剩 ${BACKUP_FREE}MB，本次两份备份至少要 ${NEED_MB}MB"
  else
    info "备份目录磁盘剩余 ${BACKUP_FREE:-?}MB（数据库约 ${DB_MB}MB）"
  fi
fi

# 密钥文件（只检查存在和长度，不打印内容）
if [[ ! -f "$ENV_FILE" ]]; then
  problem "缺少环境文件 $ENV_FILE（首次用 README 里的命令生成）"
elif [[ ! -r "$ENV_FILE" ]]; then
  problem "当前账号读不了 $ENV_FILE，请用 sudo 运行（演练也一样）"
else
  mode="$(stat -c %a -- "$ENV_FILE")"
  if [[ "${mode: -2}" != "00" ]]; then
    problem "$ENV_FILE 权限是 $mode，别人也能读到密钥；请 chmod 600"
  fi
  for key in CONTENTCREW_SESSION_SECRET CONTENTCREW_CONFIG_KEY; do
    len="$(env_value_length "$key")"
    if (( len < 32 )); then
      problem "$ENV_FILE 里缺少 $key 或长度不够 32 位"
    fi
  done
  if [[ "$DB_EXISTED" -eq 0 ]]; then
    len="$(env_value_length CONTENTCREW_BOOTSTRAP_PASSWORD)"
    if (( len < 12 )); then
      problem "首次部署需要在 $ENV_FILE 里设置 CONTENTCREW_BOOTSTRAP_PASSWORD（至少 12 位，含字母和数字），用来建 root 账号 boss"
    fi
  fi
  if env_has_key CONTENTCREW_DB_PATH; then
    warn "$ENV_FILE 里设置了 CONTENTCREW_DB_PATH；本脚本按 $DB_PATH 备份/迁移，请确认两者一致"
  fi
  info "密钥文件 $ENV_FILE 检查通过"
fi

# systemd 单元
if [[ ! -f "$UNIT_FILE" ]]; then
  problem "没有安装服务单元 $UNIT_FILE（install -m 644 deploy/simple/paihuo.service $UNIT_FILE && systemctl daemon-reload）"
elif ! grep -q "CONTENTCREW_DB_PATH=$DB_PATH" "$UNIT_FILE"; then
  warn "$UNIT_FILE 里的 CONTENTCREW_DB_PATH 不是 $DB_PATH，备份/迁移的库和服务用的库可能不是同一个"
fi

# 单进程约束：派活只能跑一个进程
if service_active; then
  SERVICE_WAS_ACTIVE=1
  info "服务 $SERVICE 正在运行"
fi
if env_has_key WEB_CONCURRENCY; then
  problem "$ENV_FILE 里有 WEB_CONCURRENCY，派活只能单进程运行，请删掉"
fi
if env_has_key CONTENTCREW_WORKERS && ! grep -Eq '^[[:space:]]*CONTENTCREW_WORKERS=1[[:space:]]*$' "$ENV_FILE"; then
  problem "$ENV_FILE 里 CONTENTCREW_WORKERS 不是 1，派活只能单进程运行"
fi
if [[ "$SERVICE" != "contentcrew" ]] && have_systemctl && systemctl is-active --quiet contentcrew.service 2>/dev/null; then
  problem "旧的 contentcrew.service 还在运行；两套服务不能同时跑同一个数据库（systemctl disable --now contentcrew.service）"
fi
if command -v pgrep >/dev/null 2>&1; then
  MAIN_PID=0
  if [[ "$SERVICE_WAS_ACTIVE" -eq 1 ]]; then
    MAIN_PID="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || echo 0)"
  fi
  for pid in $(pgrep -f 'uvicorn app.main:app' || true); do
    if [[ "$pid" != "$MAIN_PID" ]]; then
      problem "发现不归 $SERVICE 管的派活进程 PID $pid（手动起的 uvicorn？），请先停掉"
    fi
  done
fi

# 端口
if port_in_use; then
  if [[ "$SERVICE_WAS_ACTIVE" -eq 1 ]]; then
    info "端口 $PORT 由正在运行的 $SERVICE 占用（发布时会重启）"
  else
    problem "端口 $PORT 被别的程序占用，而 $SERVICE 没在运行"
  fi
else
  info "端口 $PORT 空闲"
fi

if (( PROBLEMS > 0 )); then
  die "预检有 $PROBLEMS 项不通过，没有做任何改动"
fi
say "预检通过。"

# =====================================================================
step "2/6" "在线备份数据库和素材"
# =====================================================================
PHASE="backup"

# backup_now <是否带素材 1/0>：调用现有 deploy/backup_db.py，打印生成的备份路径
backup_now() {
  local with_assets="$1" out args code
  out="$BACKUP_DIR/db-$(date -u +%Y-%m-%dT%H%M%SZ).db"
  while [[ -e "$out" ]]; do
    sleep 1
    out="$BACKUP_DIR/db-$(date -u +%Y-%m-%dT%H%M%SZ).db"
  done
  # 保留策略与每小时备份一致；本地备份就够回滚，异地同步交给定时备份，不拖慢发布
  args=(--database "$DB_PATH" --backup-dir "$BACKUP_DIR" --output "$out"
        --keep-days 14 --keep-minimum 24 --remote=)
  if [[ "$with_assets" == "1" ]]; then
    args+=(--restore-drill --assets-interval-hours 0)
    if [[ -d "$DATA_DIR/assets" ]]; then args+=(--asset-source "assets=$DATA_DIR/assets"); fi
    if [[ -d "$PUB_DIR" ]]; then args+=(--asset-source "pub=$PUB_DIR"); fi
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '    (演练，不执行) python -m deploy.backup_db %s\n' "${args[*]}" >&2
    echo "$out"
    return 0
  fi
  code=0
  (umask 077; cd "$OPS_ROOT" && env -u PAIHUO_BACKUP_ASSET_SOURCES -u PAIHUO_BACKUP_REMOTE \
     PYTHONPATH="$OPS_ROOT" PYTHONDONTWRITEBYTECODE=1 \
     "$PYTHON_BIN" -m deploy.backup_db "${args[@]}" >/dev/null) 2> >(grep -v 'offsite backup is not configured' >&2) || code=$?
  if [[ "$code" -eq 75 ]]; then
    warn "数据库已备份，但素材快照失败（见上面的 asset backup failed），继续发布"
  elif [[ "$code" -ne 0 ]]; then
    return 1
  fi
  [[ -f "$out" ]] || return 1
  echo "$out"
}

if [[ "$DB_EXISTED" -eq 0 ]]; then
  info "首次部署，还没有数据库可备份"
elif [[ "$ONLINE_BACKUP" -eq 0 ]]; then
  info "按参数跳过在线备份（停服后仍会做快照）"
else
  ONLINE_BACKUP_PATH="$(backup_now 1)" || fail "在线备份失败，发布中止（线上服务没有动）"
  info "数据库备份: $ONLINE_BACKUP_PATH"
  info "素材快照在: $BACKUP_DIR/assets/"
fi

# =====================================================================
step "3/6" "准备新版本代码和虚拟环境"
# =====================================================================
PHASE="build"
if [[ "$DRY_RUN" -eq 1 ]]; then
  info "(演练) 将把代码放到 $RELEASE_DIR，data/ 换成指向 $DATA_DIR 的软链接"
  info "(演练) 将执行 $PYTHON_BIN -m venv $RELEASE_DIR/venv 并按 requirements.lock.txt 安装依赖"
else
  mkdir -p -- "$RELEASES_DIR"
  mkdir -- "$RELEASE_DIR"
  CREATED_RELEASE=1
  if [[ -n "$SOURCE_DIR" ]]; then
    info "复制代码目录（不带 .git、venv、数据库等运行数据）"
    tar -C "$SOURCE_DIR" \
      --exclude=./.git --exclude=./venv --exclude=./.venv --exclude='__pycache__' \
      --exclude=./.env --exclude='./.env.*' \
      --exclude=./data/assets --exclude=./data/backups --exclude=./data/llmwork \
      --exclude=./data/promo --exclude=./data/public --exclude='./data/contentcrew.db*' \
      -cf - . | tar -C "$RELEASE_DIR" -xf -
  else
    info "导出提交 $COMMIT_SHORT 的代码"
    "${GIT[@]}" archive --format=tar "$COMMIT_FULL" | tar -C "$RELEASE_DIR" -xf -
  fi
  [[ -x "$RELEASE_DIR/run.sh" ]] || chmod 0755 "$RELEASE_DIR/run.sh"

  # 仓库里 data/ 下的行业配置是只读种子：按正式制品的规则挪到 config/，
  # 然后 data/ 整个换成指向运行数据目录的软链接（素材、公开文件、规则都写在那里）。
  info "整理只读配置，data/ 指向 $DATA_DIR"
  "$PYTHON_BIN" - "$RELEASE_DIR" <<'PY'
import os, shutil, sys
root = sys.argv[1]
sys.path.insert(0, root)
from deploy.build_release import _IMMUTABLE_SEED_PATHS
for src, dst in _IMMUTABLE_SEED_PATHS:
    s, d = os.path.join(root, src), os.path.join(root, dst)
    if os.path.lexists(s):
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.move(s, d)
PY
  rm -rf -- "$RELEASE_DIR/data"
  ln -s -- "$DATA_DIR" "$RELEASE_DIR/data"

  info "建虚拟环境并安装依赖（几分钟，线上服务照常运行）"
  venv_args=()
  [[ "${PAIHUO_VENV_SYSTEM_SITE:-0}" == "1" ]] && venv_args+=(--system-site-packages)
  "$PYTHON_BIN" -m venv "${venv_args[@]}" "$RELEASE_DIR/venv"
  pip_args=(--no-input --disable-pip-version-check --no-cache-dir)
  [[ -n "${PAIHUO_PIP_INDEX_URL:-}" ]] && pip_args+=(--index-url "$PAIHUO_PIP_INDEX_URL")
  "$RELEASE_DIR/venv/bin/python" -m pip install "${pip_args[@]}" -r "$RELEASE_DIR/requirements.lock.txt" \
    || fail "安装依赖失败（网络不好可设 PAIHUO_PIP_INDEX_URL 用国内镜像）"
  "$RELEASE_DIR/venv/bin/python" -m pip check >/dev/null || fail "依赖版本冲突（pip check 不通过）"
  "$RELEASE_DIR/venv/bin/python" -m compileall -q "$RELEASE_DIR/app" "$RELEASE_DIR/deploy" \
    || fail "新代码有语法错误（compileall 不通过）"
  info "虚拟环境就绪"
fi

# =====================================================================
step "4/6" "停服 → 停服快照 → 只迁移数据库（不启动）"
# =====================================================================
PHASE="migrate"
if [[ "$SERVICE_WAS_ACTIVE" -eq 1 ]]; then
  info "停止服务 $SERVICE（从这里开始用户会短暂打不开）"
  STOPPED=1
  service_stop
fi
if [[ "$DB_EXISTED" -eq 1 ]]; then
  # 在线备份之后到停服之间可能又有新数据，回滚要用停服这一刻的快照，一条都不丢
  SNAPSHOT="$(backup_now 0)" || fail "停服快照失败"
  info "停服快照: $SNAPSHOT（失败回滚会用它恢复）"
fi
if [[ "$DRY_RUN" -eq 1 ]]; then
  info "(演练) 将以 $APP_USER 身份执行: python -c 'from app import instancelock, db; instancelock.acquire(db.DB_PATH); db.conn()'"
  if [[ "$DB_EXISTED" -eq 1 ]]; then
    info "(演练) 数据库 v$DB_SCHEMA → v$NEW_SCHEMA"
  else
    info "(演练) 新建数据库 v$NEW_SCHEMA"
  fi
else
  if [[ ! -d "$DATA_DIR" ]]; then
    install -d -m 0750 -o "$APP_USER" -g "$(id -gn "$APP_USER")" -- "$DATA_DIR"
  fi
  MIGRATION_STARTED=1
  info "以 $APP_USER 身份用新代码连接数据库（连接即迁移），不启动网站"
  # 先拿单进程实例锁：拿不到说明还有派活进程在跑，此时绝不迁移。
  # 迁移锁、实例锁文件必须归应用账号且权限 600，所以 umask 077 并以应用账号执行。
  (umask 077; cd "$RELEASE_DIR" && as_app_user env -i \
      PATH=/usr/bin:/bin HOME="$DATA_DIR" TZ=Asia/Shanghai PYTHONDONTWRITEBYTECODE=1 \
      CONTENTCREW_DB_PATH="$DB_PATH" \
      "$RELEASE_DIR/venv/bin/python" -c '
from app import instancelock, db
instancelock.acquire(db.DB_PATH)
db.conn()
') || fail "数据库迁移失败"
  AFTER_SCHEMA="$(db_schema_version "$DB_PATH")"
  [[ "$AFTER_SCHEMA" == "$NEW_SCHEMA" ]] || fail "迁移后数据库是 v$AFTER_SCHEMA，不是预期的 v$NEW_SCHEMA"
  if [[ "$DB_EXISTED" -eq 1 ]]; then
    info "数据库迁移完成: v$DB_SCHEMA → v$AFTER_SCHEMA"
  else
    info "已新建数据库 v$AFTER_SCHEMA（root 账号 boss 会在服务第一次启动时创建）"
  fi
  cat >"$RELEASE_DIR/$META_NAME" <<EOF
RELEASE_ID=$RELEASE_ID
COMMIT=$COMMIT_FULL
SOURCE=$SOURCE_DESC
DEPLOYED_AT=$(now_local)
PREVIOUS_RELEASE=$PREV_RELEASE
SCHEMA_BEFORE=$DB_SCHEMA
SCHEMA_AFTER=$AFTER_SCHEMA
PRE_SWITCH_BACKUP=$SNAPSHOT
ONLINE_BACKUP=$ONLINE_BACKUP_PATH
EOF
fi

# =====================================================================
step "5/6" "切换到新版本并重启"
# =====================================================================
PHASE="switch"
switch_current "$RELEASE_ID"
if [[ "$DRY_RUN" -eq 0 ]]; then
  SWITCHED=1
  info "current -> releases/$RELEASE_ID"
fi
service_restart
[[ "$DRY_RUN" -eq 1 ]] || info "已执行 systemctl restart $SERVICE"

# =====================================================================
step "6/6" "冒烟检查"
# =====================================================================
PHASE="smoke"
smoke_check "$DEEP_REQUIRED" || fail "冒烟检查不通过"

PHASE="done"
if [[ "$DRY_RUN" -eq 1 ]]; then
  say ""
  say "演练结束：预检通过，以上是发布计划，没有做任何改动。"
  exit 0
fi
history_log "deploy-ok $RELEASE_ID from=${PREV_RELEASE:-none} schema=$DB_SCHEMA->$AFTER_SCHEMA snapshot=${SNAPSHOT:-none}"

# 清理旧版本：保留最近 $KEEP_RELEASES 个，current 和上一个永远不删
mapfile -t old_releases < <(ls -1t -- "$RELEASES_DIR" 2>/dev/null | tail -n +"$((KEEP_RELEASES + 1))")
for old in "${old_releases[@]}"; do
  [[ "$old" == "$RELEASE_ID" || "$old" == "$PREV_RELEASE" ]] && continue
  rm -rf -- "${RELEASES_DIR:?}/$old" && info "清理旧版本 $old"
done

say ""
say "发布成功: $RELEASE_ID"
say "  上一个版本: ${PREV_RELEASE:-无}"
[[ -n "$SNAPSHOT" ]] && say "  发布前数据库快照: $SNAPSHOT"
say "  出问题回滚: sudo bash $CURRENT_LINK/deploy/simple/rollback.sh --restore-deploy-snapshot"
