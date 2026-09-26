#!/usr/bin/env bash
# 派活 简易部署：手动回滚到上一个（或指定的）release，可同时恢复数据库备份。
#
#   sudo bash /srv/paihuo/current/deploy/simple/rollback.sh --list
#   sudo bash /srv/paihuo/current/deploy/simple/rollback.sh --restore-deploy-snapshot
#   sudo bash /srv/paihuo/current/deploy/simple/rollback.sh --to 20260926-101500-ab12cd34 \
#        --restore-backup /var/backups/paihuo/db-2026-09-26T021455Z.db
#   bash rollback.sh --dry-run ...     # 只演练，不改任何东西
#
# 数据库迁移不可逆：新代码把库升到新版本后，旧代码会拒绝启动。所以如果数据库版本
# 比目标 release 支持的高，必须同时恢复一份旧备份（会丢掉备份之后的新数据），
# 顺序固定为：停服 → 恢复数据库 → 切换 current → 启动 → 冒烟。
set -euo pipefail
umask 022

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/simple/common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'EOF'
用法: rollback.sh [选项]
  --list                     列出服务器上的所有版本和它们的发布记录
  --to <release-id>          回滚到指定版本（默认：当前版本记录的上一个版本）
  --restore-deploy-snapshot  同时恢复“当前版本发布前的停服快照”（最常用）
  --restore-backup <文件>    同时恢复指定的数据库备份（/var/backups/paihuo/db-*.db）
  --dry-run                  只演练，打印计划，不改任何东西
  -h, --help                 显示本帮助
EOF
}

TARGET=""
RESTORE=""
USE_SNAPSHOT=0
LIST=0
while (($#)); do
  case "$1" in
    --to) TARGET="${2:?--to 需要一个值}"; shift 2 ;;
    --restore-backup) RESTORE="${2:?--restore-backup 需要一个值}"; shift 2 ;;
    --restore-deploy-snapshot) USE_SNAPSHOT=1; shift ;;
    --list) LIST=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "不认识的参数: $1" ;;
  esac
done
[[ -n "$RESTORE" && "$USE_SNAPSHOT" -eq 1 ]] && die "--restore-backup 和 --restore-deploy-snapshot 只能选一个"

CURRENT="$(current_release || true)"

if [[ "$LIST" -eq 1 ]]; then
  say "服务器上的版本（新的在前，* 为线上版本）："
  while IFS= read -r id; do
    mark=" "; [[ "$id" == "$CURRENT" ]] && mark="*"
    dir="$RELEASES_DIR/$id"
    printf '%s %s  支持数据库 v%s  发布于 %s  上一个 %s\n' "$mark" "$id" \
      "$(code_schema_version "$dir/app/db.py")" \
      "$(meta_get "$dir" DEPLOYED_AT)" "$(meta_get "$dir" PREVIOUS_RELEASE)"
    snap="$(meta_get "$dir" PRE_SWITCH_BACKUP)"
    if [[ -n "$snap" ]]; then printf '    发布前快照: %s\n' "$snap"; fi
  done < <(ls -1t -- "$RELEASES_DIR" 2>/dev/null)
  say "数据库当前版本: v$(db_schema_version "$DB_PATH")"
  exit 0
fi

say "派活 回滚 $( [[ "$DRY_RUN" -eq 1 ]] && echo '（演练模式：不会改动任何东西）')"
require_root
acquire_lock

step "1/4" "确定回滚目标"
[[ -n "$CURRENT" ]] || die "没有 current 软链接，无从回滚"
info "线上版本: $CURRENT"
if [[ -z "$TARGET" ]]; then
  TARGET="$(meta_get "$RELEASES_DIR/$CURRENT" PREVIOUS_RELEASE)"
  if [[ -z "$TARGET" ]]; then
    # 没有发布记录（比如旧体系发布的版本）：取比当前版本旧的最近一个目录
    TARGET="$(ls -1t -- "$RELEASES_DIR" | awk -v cur="$CURRENT" 'found {print; exit} $0 == cur {found=1}')"
  fi
fi
[[ -n "$TARGET" ]] || die "找不到上一个版本，请用 --to 指定（--list 查看）"
[[ "$TARGET" != "$CURRENT" ]] || die "目标版本就是线上版本: $TARGET"
TARGET_DIR="$RELEASES_DIR/$TARGET"
[[ -d "$TARGET_DIR" ]] || die "版本不存在: $TARGET_DIR"
[[ -x "$TARGET_DIR/venv/bin/python" ]] || die "目标版本没有虚拟环境: $TARGET_DIR/venv"
[[ -f "$TARGET_DIR/run.sh" ]] || die "目标版本缺少 run.sh: $TARGET_DIR"
info "回滚到: $TARGET"

if [[ "$USE_SNAPSHOT" -eq 1 ]]; then
  RESTORE="$(meta_get "$RELEASES_DIR/$CURRENT" PRE_SWITCH_BACKUP)"
  [[ -n "$RESTORE" ]] || die "当前版本没有记录发布前快照，请用 --restore-backup 指定备份文件"
fi

step "2/4" "检查数据库版本是否兼容"
TARGET_SCHEMA="$(code_schema_version "$TARGET_DIR/app/db.py")"
DB_SCHEMA="$(db_schema_version "$DB_PATH")"
[[ "$TARGET_SCHEMA" =~ ^[0-9]+$ ]] || die "读不到目标版本支持的数据库版本"
if [[ -n "$RESTORE" ]]; then
  [[ -f "$RESTORE" ]] || die "备份文件不存在: $RESTORE"
  RESTORE_SCHEMA="$(db_schema_version "$RESTORE")"
  info "将恢复备份: $RESTORE（v$RESTORE_SCHEMA）"
  if (( RESTORE_SCHEMA > TARGET_SCHEMA )); then
    die "备份是 v$RESTORE_SCHEMA，目标版本只支持到 v$TARGET_SCHEMA，请换一份更早的备份"
  fi
  warn "恢复后，备份时间点之后产生的数据（新任务、上传、充值等）都会回到备份那一刻；换下来的库会保留在 $DATA_DIR/rollback-quarantine-*"
elif [[ "$DB_SCHEMA" =~ ^[0-9]+$ ]] && (( DB_SCHEMA > TARGET_SCHEMA )); then
  die "数据库已经是 v$DB_SCHEMA，目标版本只支持到 v$TARGET_SCHEMA，旧代码会拒绝启动。
  请加 --restore-deploy-snapshot（恢复当前版本发布前的快照）或 --restore-backup <文件>。"
else
  info "数据库 v$DB_SCHEMA，目标版本支持到 v$TARGET_SCHEMA，不需要恢复数据库"
fi

step "3/4" "停服 → 恢复数据库 → 切换版本 → 启动"
service_stop
[[ "$DRY_RUN" -eq 1 ]] || info "服务已停止"
if [[ -n "$RESTORE" ]]; then
  restore_db "$RESTORE"
fi
switch_current "$TARGET"
service_start
if [[ "$DRY_RUN" -eq 0 ]]; then
  info "current -> releases/$TARGET，已执行 systemctl start $SERVICE"
fi

step "4/4" "冒烟检查"
if smoke_check 0; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say ""
    say "演练结束：以上是回滚计划，没有做任何改动。"
    exit 0
  fi
  history_log "rollback-ok $CURRENT -> $TARGET restore=${RESTORE:-none}"
  say ""
  say "回滚成功：线上版本现在是 $TARGET"
  [[ -n "$RESTORE" ]] && say "被换下来的数据库在 ${RESTORED_QUARANTINE:-$DATA_DIR/rollback-quarantine-*}，确认无误后再删。"
  exit 0
fi
show_recent_logs
history_log "rollback-smoke-failed $CURRENT -> $TARGET restore=${RESTORE:-none}"
die "回滚后的版本没有通过冒烟检查，请看上面的日志，按 deploy/simple/README.md「常见故障」排查"
