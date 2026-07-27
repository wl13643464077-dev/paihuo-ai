# 派活 AI SQLite 备份与恢复手册

线上主库是 `/var/lib/paihuo/data/contentcrew.db`。周期备份固定写入
`/var/backups/paihuo`，目录和文件均由 root 控制；应用账号 `paihuo` 只能读写
主库和运行数据，不能伪造“最近一次成功备份”。

## 不可破坏的边界

- 运行中的 SQLite 只能通过 `sqlite3.Connection.backup()` 备份。禁止用
  `cp contentcrew.db`，否则可能漏掉 WAL 中已经提交的数据。
- root timer 只执行 `/usr/local/lib/paihuo-ops` 中固定安装的代码，不能执行
  `/srv/paihuo/current` 或候选 release 中的备份脚本。
- 每份成功备份必须同时通过完整性检查、恢复演练，并把**精确路径、SHA、schema
  digest、核心表行数**写入 root 0600 attestation：
  `/var/lib/paihuo-upgrade/latest-periodic-backup.json`。
- 备份目录必须是 `root:root 0700`；控制目录必须是 `root:root 0700`。
- 发布切换期间 `paihuo-backup.timer` 会停止，backup start guard 会拒绝为尚未
  commit 的候选留快照。接受候选或恢复旧版后，升级器先生成新备份，再重开公网。
- 本机备份不能替代异地备份。复制到对象存储或另一台主机后必须重新核对 SHA。
- schema47-r6 的数据库只保存供应商认证密文；数据库备份与对应的配置包装密钥
  必须作为一对恢复。只拿到其中一个不能证明供应商能力可恢复。

## 环境密钥备份与恢复单元

生产 `/etc/paihuo/paihuo.env` 同时保存相互独立的会话签名密钥和配置包装密钥，
必须是 `root:root 0600`。每次 schema47-r6 发布在
`/var/lib/paihuo-upgrade/credentials` 建立本次 release 的不覆盖副本：

```bash
set -euo pipefail
release_id=<本次release-id>
key_backup="/var/lib/paihuo-upgrade/credentials/paihuo-env-$release_id.backup"
sudo env PYTHONPATH=/usr/local/lib/paihuo-ops \
  /usr/bin/python3 -m deploy.session_secret_env \
  --path /etc/paihuo/paihuo.env \
  --backup-to "$key_backup"
sudo env PYTHONPATH=/usr/local/lib/paihuo-ops \
  /usr/bin/python3 -m deploy.session_secret_env \
  --path /etc/paihuo/paihuo.env \
  --check-backup "$key_backup"
sudo test "$(stat -c '%U:%G %a' "$key_backup")" = "root:root 600"
```

安全契约：

- `--backup-to` 只在目标不存在时原子创建；存在且相同时幂等成功，不相同时
  拒绝覆盖。
- `--check-backup` 只读比较当前 EnvironmentFile 与副本；任何一侧的链接、
  硬链接、owner、mode、大小、内容或并发变化异常都失败。
- 命令只输出状态和受管键数量，不输出值或哈希。不要用 `sed`、`cat`、shell
  tracing 或环境转储检查该文件。
- 会话签名密钥可单独受控轮换；配置包装密钥在没有认证的解密—重包事务前
  绝不能直接轮换。
- 发布回执/发布记录必须把数据库快照身份、release id 与密钥副本路径关联。
  恢复时按该关联选取一对材料，不能按“最新文件”猜测。

本机 root-only 副本只处理误删和版本回退，不能替代异地灾备。Git/远端以及
数据库与密钥的异地主机或对象存储位置仍需用户指定并明确授权；授权前不得擅自
上传任何密钥。

## 安装与首次验收

固定运维代码和 systemd unit 的完整安装步骤见 `DEPLOYMENT.md`。安装后执行：

```bash
set -euo pipefail
sudo test "$(stat -c '%U:%G %a' /var/backups/paihuo)" = "root:root 700"
sudo test "$(stat -c '%U:%G %a' /var/lib/paihuo-upgrade)" = "root:root 700"
sudo systemctl daemon-reload
sudo systemctl start paihuo-backup.service
sudo systemctl start paihuo-backup-health.service
sudo systemctl enable --now \
  paihuo-backup.timer paihuo-backup-health.timer
sudo systemctl is-active --quiet \
  paihuo-backup.timer paihuo-backup-health.timer
sudo systemctl show paihuo-backup.service \
  -p Result -p ExecMainStatus --no-pager
```

再用固定模块路径直接验证：

```bash
sudo env PYTHONPATH=/usr/local/lib/paihuo-ops \
  /usr/bin/python3 -m deploy.backup_health \
  --database /var/lib/paihuo/data/contentcrew.db \
  --backup-dir /var/backups/paihuo \
  --max-age-hours 24 \
  --attestation /var/lib/paihuo-upgrade/latest-periodic-backup.json
```

如果曾使用 raw-copy cron，先禁用它；不要让两个备份器并发：

```bash
sudo test ! -e /etc/cron.daily/paihuo-backup || \
  sudo chmod -x /etc/cron.daily/paihuo-backup
```

## 日常检查

```bash
sudo systemctl status \
  paihuo-backup.timer paihuo-backup-health.timer \
  paihuo-backup.service paihuo-backup-health.service --no-pager
sudo journalctl -u paihuo-backup.service --since "2 days ago" --no-pager
sudo find /var/backups/paihuo -maxdepth 1 -type f \
  -name 'db-*.db' -printf '%TY-%Tm-%Td %TH:%TM %s %p\n' | sort
sudo env PYTHONPATH=/usr/local/lib/paihuo-ops \
  /usr/bin/python3 -m deploy.backup_health \
  --database /var/lib/paihuo/data/contentcrew.db \
  --backup-dir /var/backups/paihuo \
  --max-age-hours 24 \
  --attestation /var/lib/paihuo-upgrade/latest-periodic-backup.json
```

任何一个条件都必须告警：24 小时内没有成功 attestation、SHA/schema/行数不匹配、
SQLite 完整性失败、恢复演练失败、磁盘不足、timer 未运行或 unit 非零退出。

## 无停机恢复演练

选择 attestation 指向的文件，不要只按 mtime 猜“最新”：

```bash
sudo sed -n '1,160p' \
  /var/lib/paihuo-upgrade/latest-periodic-backup.json
backup=/var/backups/paihuo/db-YYYY-MM-DDTHHMMSSZ.db
sudo env PYTHONPATH=/usr/local/lib/paihuo-ops \
  /usr/bin/python3 -m deploy.verify_backup \
  "$backup" --restore-drill
```

命令只在临时目录恢复和复验，不触碰主库。

schema47-r6 的完整恢复演练还要选择发布记录绑定的 `key_backup`，先独立验证
其格式与权限，再在隔离候选环境中以该密钥认证临时恢复数据库里的全部受管
配置密文。验证输出只能包含受管键总数、密文数、明文数和成功/失败；不能输出
配置名、值、密文或供应商响应。只有 `plaintext=0` 且所有现存密文认证成功，
才证明这一对数据库/密钥恢复材料完整。

## 生产恢复

恢复前必须先处理所有非终态 `upgrade-*.json`；start guard 会阻止绕过失败升级
直接启动应用或 Caddy。以下流程要求
`/var/lib/paihuo-upgrade` 与 `/var/lib/paihuo/data` 位于同一文件系统，从而用
`mv -T` 原子替换主库：

schema47-r6 还必须先从升级回执/发布记录选定与数据库备份匹配的
`key_backup`。如果当前 `/etc/paihuo/paihuo.env` 与它不一致，不要启动应用或
尝试生成新配置包装密钥；保持公网关闭，先恢复这份环境文件，再恢复数据库。
恢复环境文件时必须使用同目录临时文件、`root:root 0600` 和原子 rename，
随后用 `deploy.session_secret_env --check` 复验；禁止把内容输出到终端或日志。

```bash
set -euo pipefail
backup=/var/backups/paihuo/db-YYYY-MM-DDTHHMMSSZ.db
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
prepared="/var/lib/paihuo-upgrade/manual-restore-$stamp.db"
quarantine="/var/lib/paihuo/data/manual-restore-old-$stamp"

sudo test "$(stat -c %d /var/lib/paihuo-upgrade)" = \
  "$(stat -c %d /var/lib/paihuo/data)"
sudo test -f "$backup"
sudo test ! -e "$prepared"
sudo test ! -e "$quarantine"
sudo env PYTHONPATH=/usr/local/lib/paihuo-ops \
  /usr/bin/python3 -m deploy.verify_backup \
  "$backup" --restore-to "$prepared"
sudo env PYTHONPATH=/usr/local/lib/paihuo-ops \
  /usr/bin/python3 -m deploy.verify_backup "$prepared" --restore-drill
sudo chmod 0600 "$prepared"

sudo systemctl stop paihuo-backup.timer
sudo systemctl stop paihuo-backup.service
sudo systemctl stop caddy.service
sudo systemctl stop contentcrew.service
! sudo systemctl is-active --quiet paihuo-backup.service
! sudo systemctl is-active --quiet caddy.service
! sudo systemctl is-active --quiet contentcrew.service

sudo install -d -o root -g root -m 0700 "$quarantine"
# hardlink 保留失败前主文件，同时线上主库路径始终存在。
sudo ln /var/lib/paihuo/data/contentcrew.db \
  "$quarantine/contentcrew.db"
for suffix in -wal -shm -journal; do
  source="/var/lib/paihuo/data/contentcrew.db$suffix"
  sudo test ! -e "$source" || sudo mv "$source" "$quarantine/"
done
sudo chown paihuo:paihuo "$prepared"
sudo chmod 0600 "$prepared"
sudo mv -T "$prepared" /var/lib/paihuo/data/contentcrew.db
sync
```

若本次恢复要求接回所选 `key_backup`，在服务与 Caddy 都保持停止时原子替换
EnvironmentFile。以下命令不会打印文件内容；`key_backup` 必须来自发布记录，
不能由操作者猜测：

```bash
set -euo pipefail
restore_env="/etc/paihuo/.paihuo.env.restore-$stamp"
sudo test ! -e "$restore_env"
sudo env PYTHONPATH=/usr/local/lib/paihuo-ops \
  /usr/bin/python3 -m deploy.session_secret_env \
  --check --path "$key_backup"
sudo install -o root -g root -m 0600 "$key_backup" "$restore_env"
sudo env PYTHONPATH=/usr/local/lib/paihuo-ops \
  /usr/bin/python3 -m deploy.session_secret_env \
  --check --path "$restore_env"
sudo mv -T "$restore_env" /etc/paihuo/paihuo.env
sudo sync -f /etc/paihuo
sudo env PYTHONPATH=/usr/local/lib/paihuo-ops \
  /usr/bin/python3 -m deploy.session_secret_env \
  --path /etc/paihuo/paihuo.env \
  --check-backup "$key_backup"
```

此时先启动应用、检查本机健康、登录、任务列表、线索雷达和最近数据；确认后才
刷新备份并重开公网：

```bash
sudo systemctl start contentcrew.service
sudo systemctl is-active --quiet contentcrew.service
curl -fsS http://127.0.0.1:8899/healthz
sudo systemctl start paihuo-backup.service
sudo systemctl start paihuo-backup.timer
sudo systemctl start paihuo-backup-health.service
sudo systemctl start caddy.service
sudo systemctl is-active --quiet \
  contentcrew.service caddy.service paihuo-backup.timer
```

至少观察一个业务周期后再归档或清理 quarantine。在确认前不要删除它。
schema47-r6 启动还必须确认 schema47、业务库会话密钥残留 0、受管供应商
配置明文 0 且密文认证全部成功；只输出聚合。任一认证失败都保持公网关闭并
执行撤回，不能用新的包装密钥覆盖。

## 恢复失败时撤回

下面把 quarantine 中的旧主库与 sidecar 原子接回。执行期间保持公网、应用和
周期备份停止：

```bash
set -euo pipefail
quarantine=/var/lib/paihuo/data/manual-restore-old-YYYYMMDDTHHMMSSZ
failed=/var/lib/paihuo/data/manual-restore-rejected-"$(date -u +%Y%m%dT%H%M%SZ)"
sudo systemctl stop paihuo-backup.timer
sudo systemctl stop paihuo-backup.service
sudo systemctl stop caddy.service
sudo systemctl stop contentcrew.service
! sudo systemctl is-active --quiet contentcrew.service
sudo test -f "$quarantine/contentcrew.db"
sudo install -d -o root -g root -m 0700 "$failed"
sudo ln /var/lib/paihuo/data/contentcrew.db "$failed/contentcrew.db"
for suffix in -wal -shm -journal; do
  source="/var/lib/paihuo/data/contentcrew.db$suffix"
  sudo test ! -e "$source" || sudo mv "$source" "$failed/"
done
sudo chown paihuo:paihuo "$quarantine/contentcrew.db"
sudo chmod 0600 "$quarantine/contentcrew.db"
sudo mv -T "$quarantine/contentcrew.db" \
  /var/lib/paihuo/data/contentcrew.db
for suffix in -wal -shm -journal; do
  source="$quarantine/contentcrew.db$suffix"
  sudo test ! -e "$source" || sudo mv "$source" /var/lib/paihuo/data/
done
sync
sudo systemctl start contentcrew.service
curl -fsS http://127.0.0.1:8899/healthz
sudo systemctl start paihuo-backup.service
sudo systemctl start paihuo-backup.timer
sudo systemctl start caddy.service
```

不要在应用运行时覆盖主库，不要删除未确认的 quarantine，也不要用应用账号修改
`/var/backups/paihuo` 或 root attestation。

每次生产恢复或新 release 成功后，都从完成时间重新开始 24 小时观察；旧 r5
窗口不能替代 schema47-r6。生产全局 `P1-OBS-1` 仍需用户明确授权后处置或
明确接受，恢复流程本身不得修改 VNC、Tailscale 或 UFW。
