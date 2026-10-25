# 派活 AI 备份与恢复手册（simple 部署）

本手册适用于当前 `paihuo.service` 与 `paihuo-backup-simple.service/.timer`。
安装和发布见 [simple 部署手册](simple/README.md)。旧不可变发布链已退役，旧
`contentcrew.service`、ops 目录、start guard 和升级控制面不是现用恢复前提；不要
为了执行本手册重新启用它们。

**生产恢复会回退备份之后的业务数据，须先获得维护窗口和数据回退授权。** 运行中的
主库绝不能覆盖。先停止真实应用、Caddy、simple 备份 timer 和正在执行的 backup
service，确认没有主库持有者，再替换 DB/env；失败保持这些服务停止。
`rollback.sh` 只恢复 DB 和代码，不自动恢复 env，也不自动停止备份 timer。

## 不可破坏的边界

- 主库是 `/var/lib/paihuo/data/contentcrew.db`，素材是该目录的 `assets/` 和
  `/srv/paihuo-pub`。代码 `/srv/paihuo/current` 与运行数据分离。
- 在线 SQLite 只能通过 `sqlite3.Connection.backup()` 备份，不能 `cp` 主库：已提交
  数据可能仍在 WAL。`deploy.backup_db` 实现在线一致性备份；`deploy.verify_backup`
  校验完整性、schema、所有业务表行数，并可在临时目录做真实恢复演练。
- `/var/backups/paihuo` 必须 `root:root 0700`，DB 备份 `root:root 0600`。应用账号
  `paihuo` 不能写备份目录。现用 root unit 从 root 管理的 `/srv/paihuo/current`
  执行 shared deploy 模块，应用账号不能修改代码树。
- simple unit 每小时备份 DB，保留近 14 天且至少 24 份；素材约每天一份。journal
  JSON 记录精确 `backup_path`、`sha256`、`schema_digest`、`table_counts` 和
  `restore_drill`。它**没有 `--success-attestation` 和独立 backup-health timer**；
  旧控制面的 attestation 只是历史证据，不能代替当前 timer、journal 和实物复验。
- DB 与含 `CONTENTCREW_CONFIG_KEY` 的 `/etc/paihuo/paihuo.env` 必须按受控发布/恢复
  记录配对，不能各自选“最新文件”。会话签名密钥与配置包装密钥独立，禁止为了启动
  生成替代包装密钥。恢复 env 可能使现有登录会话失效，须在维护窗口告知。
- 本机副本不等于异地灾备。异地 DB 重验 SHA，env 须另行授权、加密、配对保存。
  自动异地未配置时报告 `offsite.configured=false`，不能宣称已有自动灾备。

## 安装备份与首次验收

以下只用于获准的安装/维护，不用于只读观察期触发备份。按 simple 手册安装应用和
备份 units 后，在维护窗口做首次备份：

```bash
set -euo pipefail
sudo test "$(sudo stat -c '%U:%G %a' /var/backups/paihuo)" = 'root:root 700'
sudo systemctl start paihuo-backup-simple.service
sudo systemctl show paihuo-backup-simple.service -p Result -p ExecMainStatus --no-pager
sudo journalctl -u paihuo-backup-simple.service -n 30 -o cat --no-pager
sudo systemctl enable --now paihuo-backup-simple.timer
sudo systemctl is-active --quiet paihuo-backup-simple.timer
```

`ExecMainStatus=0` 才表示本轮 DB 和已配置附加步骤均成功。`ExecMainStatus=75` 被
unit 的 `SuccessExitStatus=75` 接受，只说明 DB 成功但素材或异地失败，不能当作完整
成功。模块会另行触发 `paihuo-failure-alert@paihuo-backup-assets.service` 或
`paihuo-failure-alert@paihuo-backup-offsite.service`；告警须有已安装 failure-alert
unit 和已配置 webhook 才能送达，不能假定已有收件人。历史备份 timer/raw-copy cron
不得与 simple 并发；历史服务器迁移见 simple 手册第 五 节。

## 日常只读检查

```bash
sudo systemctl status paihuo-backup-simple.timer --no-pager
sudo systemctl show paihuo-backup-simple.service -p Result -p ExecMainStatus --no-pager
sudo systemctl list-timers paihuo-backup-simple.timer --no-pager
sudo journalctl -u paihuo-backup-simple.service --since '2 days ago' -o cat --no-pager
sudo find /var/backups/paihuo -maxdepth 1 -type f -name 'db-*.db' \
  -printf '%TY-%Tm-%Td %TH:%TM %s %p\n' | sort
```

从**同一次成功执行的 journal JSON**选精确备份路径/SHA，不只按 mtime 猜。记录
执行时间、Result/退出码、`assets.ok`、`offsite.configured/ok` 和恢复演练结果。
下面变量须填写该次已核验记录的值，再独立复验：

```bash
set -euo pipefail
backup=/var/backups/paihuo/db-YYYY-MM-DDTHHMMSSZ.db
expected_sha=该次journal记录中的64位sha256
sudo test -f "$backup"; sudo test ! -L "$backup"
sudo test "$(sudo stat -c '%U:%G %a' "$backup")" = 'root:root 600'
test "$(sudo sha256sum "$backup" | cut -d' ' -f1)" = "$expected_sha"
sudo env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.verify_backup "$backup" --restore-drill
```

复验须 `integrity_check=ok`、`restore_drill.ok=true`，schema digest、user_version
和每张表行数与该次记录相符。`schema_objects` 是对象数，不是表数。演练只在临时
目录恢复，自动移除临时文件，不触碰主库。24 小时无成功记录、SHA/行数不符、演练
失败、磁盘不足、timer 未运行或附加步骤失败，都应记录并告警。simple 没有独立
健康定时器，外部监控须显式配置，不能由本手册假定存在。

## DB/env 配对保存

正式发布/恢复前，用确定的 DB 备份身份和 release id 建立 root-only env 副本与
配对记录；simple 发布脚本不会代办。以下在备份成功、密钥未轮换时执行，变量取自
已核验发布记录，不得盲选最新备份：

```bash
set -euo pipefail
release_id=已核验的release-id
backup=/var/backups/paihuo/db-YYYY-MM-DDTHHMMSSZ.db
pair_dir=/var/backups/paihuo/recovery-pairs
key_backup="$pair_dir/paihuo-env-$release_id.backup"
sudo install -d -o root -g root -m 0700 "$pair_dir"
sudo env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.session_secret_env --path /etc/paihuo/paihuo.env \
  --backup-to "$key_backup"
sudo env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.session_secret_env --path /etc/paihuo/paihuo.env \
  --check-backup "$key_backup"
sudo test "$(sudo stat -c '%U:%G %a' "$key_backup")" = 'root:root 600'
sudo sha256sum "$backup"
```

`--backup-to` 不覆盖不同的已有文件，`--check-backup` 只读校验受管密钥、文件内容和
权限，只输出状态/计数。配对记录须含 DB 精确路径/SHA、release id、schema、env
副本路径及受控系统配置包身份。禁止输出 env 内容/哈希或环境转储；密钥不可上传
Git、PR、聊天或未获准目标。包装密钥轮换须另有原子解密—重包方案。

## 素材文件快照

- 快照在 `/var/backups/paihuo/assets/assets-YYYY-MM-DDTHHMMSSZ/{assets,pub}/`，
  `.snapshot.json` 记录来源、计数与跳过的特殊文件。
- 未变化文件硬链接到上次快照，新增/修改文件复制。不要就地修改备份文件，可能
  影响其他硬链接快照。保留近 14 天且至少 3 份，约每 23 小时一份。
- unit 的 `--asset-source 标签=绝对路径` 定义源；模块逐级 `O_NOFOLLOW` 打开，
  符号链接/管道等特殊文件被跳过，不代表已经备份。

素材恢复先进入下文维护窗口，停止应用及备份 timer/service，保存修改前素材副本
和 owner/mode。选精确快照，确认 manifest 成功且文件可读，再复制：

```bash
set -euo pipefail
snap=/var/backups/paihuo/assets/assets-YYYY-MM-DDTHHMMSSZ
sudo test -d "$snap/assets"; sudo test -d "$snap/pub"
sudo cat "$snap/.snapshot.json"
sudo rsync -a "$snap/assets/" /var/lib/paihuo/data/assets/
sudo chown -R paihuo:paihuo /var/lib/paihuo/data/assets
sudo rsync -a "$snap/pub/" /srv/paihuo-pub/
sudo chown -R paihuo:paihuo /srv/paihuo-pub
```

不要带 `--delete`，除非获准删除线上多余文件；不带删除也不等于精确时间点回滚。
素材可能比 DB 快照旧，须另验 DB 引用的文件完整性；撤回使用修改前的精确副本。

## 异地备份（须另行配置与授权）

备份 unit 读取可选 `/etc/paihuo/backup.env`（`root:root 0600`）。以下是配置示例，
不表示生产已启用；目标、凭据及保留策略先获准：

```ini
PAIHUO_BACKUP_REMOTE=rclone:paihuo-oss:paihuo-backup/prod
# 或 PAIHUO_BACKUP_REMOTE=rsync:backup@10.0.0.8:/srv/paihuo-offsite
RCLONE_CONFIG=/etc/paihuo/rclone.conf
# ProtectHome=yes 下不能使用 /root/.ssh：
# PAIHUO_BACKUP_SSH_KEY=/etc/paihuo/backup_ed25519
# PAIHUO_BACKUP_SSH_KNOWN_HOSTS=/etc/paihuo/backup_known_hosts
```

上传 `<目标>/db/db-….db` 与 `.sha256`，有新素材快照才复制到
`<目标>/assets/current/`，只增不删。远端素材 current 不是不可变时间点归档，
时间点恢复需另配版本策略。DB/env 配对包不会被该同步自动上传，须另有获准的加密
保管渠道。同步失败/超时退出 75，不能仅看 unit Result=success。

### 阿里云 OSS 配置示例

获准目标须为私有桶，凭据限制上传/读取/列举范围，删除权限与远端版本/保留策略另审。
安装 rclone 后用编辑器保存配置，不把密钥放到命令行：

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/paihuo/rclone.conf
sudoedit /etc/paihuo/rclone.conf
```

```ini
[paihuo-oss]
type = s3
provider = Alibaba
access_key_id = <受控AccessKey ID>
secret_access_key = <受控AccessKey Secret>
endpoint = oss-cn-shanghai.aliyuncs.com
acl = private
```

### 腾讯云 COS 配置示例

```ini
[paihuo-cos]
type = s3
provider = TencentCOS
access_key_id = <受控SecretId>
secret_access_key = <受控SecretKey>
endpoint = cos.ap-guangzhou.myqcloud.com
acl = private
```

按实际获准桶名配置，如 `PAIHUO_BACKUP_REMOTE=rclone:paihuo-cos:paihuo-backup-1250000000/prod`。
endpoint 仅示例，须与桶地域一致；本手册不授权创建付费资源。

### 首次异地验收

```bash
sudo env RCLONE_CONFIG=/etc/paihuo/rclone.conf rclone lsd paihuo-oss:
sudo systemctl start paihuo-backup-simple.service
sudo systemctl show paihuo-backup-simple.service -p Result -p ExecMainStatus --no-pager
sudo journalctl -u paihuo-backup-simple.service -n 30 -o cat --no-pager
sudo cat /var/backups/paihuo/.offsite-status.json
```

须 `ExecMainStatus=0`、`offsite.ok=true`，并在隔离目录从远端取回 DB/SHA 复验和
真实恢复演练；能列目录或上传返回成功不足以证明可恢复。

## 从异地取回恢复材料

整机损坏时在新机器按 simple 手册准备 root 管理的代码、venv 和 units，但保持
应用、Caddy、simple timer/service 停止。另从获准密钥渠道取回配对 env/系统配置包；
没有匹配 env 就不能启动或生成替代包装密钥。

```bash
set -euo pipefail
remote=paihuo-oss:paihuo-backup/prod
name=db-YYYY-MM-DDTHHMMSSZ.db
work=/var/backups/paihuo/offsite-restore
sudo install -d -o root -g root -m 0700 "$work"
sudo env RCLONE_CONFIG=/etc/paihuo/rclone.conf \
  rclone copyto "$remote/db/$name" "$work/$name"
sudo env RCLONE_CONFIG=/etc/paihuo/rclone.conf \
  rclone copyto "$remote/db/$name.sha256" "$work/$name.sha256"
sudo chmod 0600 "$work/$name" "$work/$name.sha256"
sudo sh -c 'cd "$1" && sha256sum -c "$2"' sh "$work" "$name.sha256"
sudo env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.verify_backup "$work/$name" --restore-drill
```

SHA 还须与独立受控配对记录一致，不能只信同一远端可修改的 `.sha256`。rsync 同理，
按精确文件名取 DB/SHA，不用未核验通配符。素材先取到隔离目录校验，不直接写 live；
确认 DB 引用可恢复后，在维护窗口复制。

## 生产恢复

下面须在**同一个 root Bash 会话**顺序执行，路径为默认 simple 布局，定制布局先
核对 unit 实际 DB/EnvironmentFile。变量来自受控配对记录。确认当前代码支持所选
schema；跨版本另核验目标 release/venv 并显式计划 current 切换。本节不自动切代码，
也不假定有 previous 链接。

### 1. 锁定维护并停止真实服务

```bash
set -euo pipefail
umask 077
test "$(id -u)" = 0
command -v flock; command -v lsof; command -v ss
backup=/var/backups/paihuo/db-YYYY-MM-DDTHHMMSSZ.db
expected_sha=该受控配对记录中的64位sha256
key_backup=/var/backups/paihuo/recovery-pairs/paihuo-env-所选release-id.backup
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
prepared="/var/lib/paihuo/data/.manual-restore-$stamp.db"
quarantine="/var/lib/paihuo/data/manual-restore-old-$stamp"
saved_env="/etc/paihuo/.paihuo.env.before-restore-$stamp"
restore_env="/etc/paihuo/.paihuo.env.restore-$stamp"
# 与 deploy.sh / rollback.sh 共用；整个会话保持锁。
exec 9>/srv/paihuo/.deploy.lock
flock -n 9
systemctl stop paihuo-backup-simple.timer
systemctl stop paihuo-backup-simple.service
systemctl stop caddy.service
systemctl stop paihuo.service
for unit in paihuo.service caddy.service paihuo-backup-simple.timer paihuo-backup-simple.service; do
  test "$(systemctl show "$unit" -p ActiveState --value)" = inactive
done
test "$(systemctl show paihuo.service -p MainPID --value)" = 0
# 恢复/撤回使用同一 fail-closed 检查；不抑制 lsof warning。
assert_restore_quiescent() {
  local database="$1" suffix source listeners holders errors error_file rc=0
  test -f "$database" && test ! -L "$database" || return 1
  local files=("$database")
  for suffix in -wal -shm -journal; do
    source="$database$suffix"
    if test -e "$source" || test -L "$source"; then
      test -f "$source" && test ! -L "$source" || return 1
      files+=("$source")
    fi
  done
  listeners="$(ss -H -ltnp 'sport = :8899')" || return 1
  test -z "$listeners" || return 1
  error_file="$(mktemp)" || return 1
  holders="$(lsof -nP -- "${files[@]}" 2>"$error_file")" || rc=$?
  errors="$(cat -- "$error_file")" || { rm -- "$error_file"; return 1; }
  rm -- "$error_file" || return 1
  test "$rc" = 1 && test -z "$holders" && test -z "$errors"
}
assert_restore_quiescent /var/lib/paihuo/data/contentcrew.db
test -f "$backup"; test ! -L "$backup"
test "$(stat -c '%U:%G %a' "$backup")" = 'root:root 600'
test "$(sha256sum "$backup" | cut -d' ' -f1)" = "$expected_sha"
test ! -e "$prepared"; test ! -L "$prepared"
test ! -e "$quarantine"; test ! -L "$quarantine"
env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.verify_backup "$backup" --restore-to "$prepared"
env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.session_secret_env --check --path "$key_backup"
# 旧环境文件与旧 DB 一起保留，撤回须一起接回。
env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.session_secret_env --path /etc/paihuo/paihuo.env \
  --backup-to "$saved_env"
```

还须核对进程清单不存在手工 uvicorn/其他应用进程；`lsof` 检查异常不能按“无占用”
放行。格式检查不等于 DB/env 配对认证。以下在已验证、独立的 `$prepared` 上使用
当前 release 的加密实现，只读认证所有独立配置和嵌入的公众号 secret/matrix cookie，
不启动应用、不运行迁移、不调用会修改 live 的 `get_secret`。该段只输出聚合计数；
必须 `plaintext=0`、`failed=0`，且 `authenticated=encrypted`，再进入步骤 2：

```bash
env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /srv/paihuo/current/venv/bin/python - "$prepared" "$key_backup" <<'PY'
import json, os, sqlite3, sys
from pathlib import Path
from deploy import session_secret_env

counts = dict(encrypted=0, authenticated=0, plaintext=0, failed=0)
try:
    body, _ = session_secret_env._read_validated_secret_file(
        sys.argv[2], keys=session_secret_env.DEFAULT_SECRET_KEYS,
        owner_uid=0, owner_gid=0, label="recovery environment")
    for line in body.decode("utf-8").splitlines():
        key, marker, value = line.partition("=")
        if marker and key.strip() == "CONTENTCREW_CONFIG_KEY":
            os.environ["CONTENTCREW_CONFIG_KEY"] = value
    os.environ["CONTENTCREW_REQUIRE_CONFIG_KEY"] = "1"
    from app import secureconfig
    cipher = secureconfig._configured_fernet()

    def authenticate(domain, stored):
        text = str(stored or "")
        if not text:
            return
        if not secureconfig.is_encrypted(text):
            counts["plaintext"] += 1
            return
        counts["encrypted"] += 1
        try:
            secureconfig._decrypt(domain, text, cipher)
            counts["authenticated"] += 1
        except Exception:
            counts["failed"] += 1

    uri = Path(sys.argv[1]).resolve().as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        for name, stored in connection.execute("SELECT key,value FROM app_setting"):
            if name in secureconfig.SECRET_SETTING_KEYS:
                authenticate(name, stored)
            elif name.startswith("wechat_mp:"):
                payload = json.loads(str(stored or ""))
                if not isinstance(payload, dict):
                    raise ValueError("invalid credential shape")
                authenticate("wechat_mp_secret", payload.get("secret"))
            elif name.startswith("matrix_accounts:"):
                payload = json.loads(str(stored or ""))
                if not isinstance(payload, list) or any(not isinstance(x, dict) for x in payload):
                    raise ValueError("invalid credential shape")
                for account in payload:
                    authenticate("matrix_cookie", account.get("cookie"))
except Exception:
    counts["failed"] += 1
print(json.dumps(counts, sort_keys=True))
sys.exit(1 if counts["failed"] or counts["plaintext"] else 0)
PY
```

把此结果与所选 DB SHA、schema 和 env 身份记入私有恢复记录。缺失证据或任一失败
就保持公网/应用/timer 关闭；不要用新包装密钥替代，也不要输出失败配置名或密文。

### 2. 保留原 DB/env 并原子替换

恢复文件在数据目录，与主库同文件系统；不依赖归档控制面。`quarantine` 与
`saved_env` 是同一次撤回单元，不可单独删除。

```bash
# 延续步骤 1 的 root Bash 会话和变量，配对认证已通过。
test "$(systemctl show paihuo.service -p ActiveState --value)" = inactive
test "$(systemctl show paihuo-backup-simple.service -p ActiveState --value)" = inactive
assert_restore_quiescent /var/lib/paihuo/data/contentcrew.db
install -d -o root -g root -m 0700 "$quarantine"
test -f /var/lib/paihuo/data/contentcrew.db
test ! -L /var/lib/paihuo/data/contentcrew.db
ln /var/lib/paihuo/data/contentcrew.db "$quarantine/contentcrew.db"
for suffix in -wal -shm -journal; do
  source="/var/lib/paihuo/data/contentcrew.db$suffix"
  test ! -e "$source" || mv -- "$source" "$quarantine/"
done
test ! -e "$restore_env"; test ! -L "$restore_env"
install -o root -g root -m 0600 "$key_backup" "$restore_env"
env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.session_secret_env --check --path "$restore_env"
mv -T -- "$restore_env" /etc/paihuo/paihuo.env
chown paihuo:paihuo "$prepared"
chmod 0600 "$prepared"
mv -T -- "$prepared" /var/lib/paihuo/data/contentcrew.db
sync
env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.session_secret_env --path /etc/paihuo/paihuo.env \
  --check-backup "$key_backup"
env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.verify_backup /var/lib/paihuo/data/contentcrew.db --restore-drill
```

私有恢复记录关联 DB/quarantine、原 env 副本、所选 DB/env 身份与 current 实际目标/SHA。
若另切代码，记录原目标；撤回要接回与原 DB/env 兼容的原代码，不能在应用运行中
切换，也不能让不兼容代码启动。

### 3. 本机验收后才重开公网和定时备份

```bash
systemctl start paihuo.service
systemctl is-active --quiet paihuo.service
curl -fsS http://127.0.0.1:8899/healthz
curl -fsS 'http://127.0.0.1:8899/healthz?deep=1'
curl -fsS -o /dev/null http://127.0.0.1:8899/login
# 确认恢复的业务数据与正常登录态任务/老板看板后：
systemctl start paihuo-backup-simple.service
test "$(systemctl show paihuo-backup-simple.service -p ExecMainStatus --value)" = 0
journalctl -u paihuo-backup-simple.service -n 30 -o cat --no-pager
# 先复核本次精确 JSON、SHA、演练、素材与已配置异地步骤，再继续：
systemctl start paihuo-backup-simple.timer
systemctl start caddy.service
systemctl is-active --quiet paihuo.service caddy.service paihuo-backup-simple.timer
```

healthz=200 不代表完整可恢复；须配对认证、正常登录业务验收及新备份实物复验。
失败保持公网/timer 关闭，按下节撤回。成功后重新算连续 24 小时稳定观察；至少
一个业务周期后决定 quarantine、原 env 和素材原副本归档期限，不自动删除。
恢复不授权修改 UFW、Tailscale、账号密码或发行版。

## 恢复失败时撤回

按同一次受控恢复记录确认 `quarantine` 与 `saved_env`，不能混入其他时间点文件。
新会话先取得同一 deploy 锁；若曾切代码，先接回与原 DB/env 兼容的原 current，
保持应用停止。以下仍在 root Bash 执行：

```bash
set -euo pipefail
umask 077
test "$(id -u)" = 0
command -v flock; command -v lsof; command -v ss
quarantine=/var/lib/paihuo/data/manual-restore-old-YYYYMMDDTHHMMSSZ
saved_env=/etc/paihuo/.paihuo.env.before-restore-YYYYMMDDTHHMMSSZ
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
failed="/var/lib/paihuo/data/manual-restore-rejected-$stamp"
failed_env="/etc/paihuo/.paihuo.env.rejected-$stamp"
restore_env="/etc/paihuo/.paihuo.env.withdraw-$stamp"
exec 9>/srv/paihuo/.deploy.lock
flock -n 9
systemctl stop paihuo-backup-simple.timer
systemctl stop paihuo-backup-simple.service
systemctl stop caddy.service
systemctl stop paihuo.service
for unit in paihuo.service caddy.service paihuo-backup-simple.timer paihuo-backup-simple.service; do
  test "$(systemctl show "$unit" -p ActiveState --value)" = inactive
done
test "$(systemctl show paihuo.service -p MainPID --value)" = 0
# 新会话重建同一检查；不能把工具异常或 warning 当作“无占用”。
assert_restore_quiescent() {
  local database="$1" suffix source listeners holders errors error_file rc=0
  test -f "$database" && test ! -L "$database" || return 1
  local files=("$database")
  for suffix in -wal -shm -journal; do
    source="$database$suffix"
    if test -e "$source" || test -L "$source"; then
      test -f "$source" && test ! -L "$source" || return 1
      files+=("$source")
    fi
  done
  listeners="$(ss -H -ltnp 'sport = :8899')" || return 1
  test -z "$listeners" || return 1
  error_file="$(mktemp)" || return 1
  holders="$(lsof -nP -- "${files[@]}" 2>"$error_file")" || rc=$?
  errors="$(cat -- "$error_file")" || { rm -- "$error_file"; return 1; }
  rm -- "$error_file" || return 1
  test "$rc" = 1 && test -z "$holders" && test -z "$errors"
}
assert_restore_quiescent /var/lib/paihuo/data/contentcrew.db
test -f "$quarantine/contentcrew.db"; test ! -L "$quarantine/contentcrew.db"
test ! -e "$failed"; test ! -L "$failed"
env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.session_secret_env --check --path "$saved_env"
env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.session_secret_env --path /etc/paihuo/paihuo.env \
  --backup-to "$failed_env"
install -d -o root -g root -m 0700 "$failed"
# DB swap 前失败时，两条路径仍是旧 DB 的同一 inode；不能把未移动的旧 sidecar
# 当成新失败 DB 的 sidecar 搬走。只有 live 已变为另一 DB 才隔离它们。
# 不变量：恢复期间应用始终停止、无写者；sidecar 逐个 mv 任一步失败都会在 DB
# swap 前因 set -e 停止，same inode 下 live 剩余 sidecar 仍属于旧 DB。
same_old_db=0
if test "$quarantine/contentcrew.db" -ef /var/lib/paihuo/data/contentcrew.db; then
  same_old_db=1
fi
if test "$same_old_db" = 0; then
  ln /var/lib/paihuo/data/contentcrew.db "$failed/contentcrew.db"
  for suffix in -wal -shm -journal; do
    source="/var/lib/paihuo/data/contentcrew.db$suffix"
    test ! -e "$source" || mv -- "$source" "$failed/"
  done
fi
test ! -e "$restore_env"; test ! -L "$restore_env"
install -o root -g root -m 0600 "$saved_env" "$restore_env"
mv -T -- "$restore_env" /etc/paihuo/paihuo.env
chown paihuo:paihuo "$quarantine/contentcrew.db"
chmod 0600 "$quarantine/contentcrew.db"
restore_quarantined_db() {
  local saved="$1" live="$2" suffix source target
  test -f "$saved" && test ! -L "$saved" || return 1
  test -f "$live" && test ! -L "$live" || return 1
  if ! test "$saved" -ef "$live"; then
    mv -T -- "$saved" "$live" || return 1
  fi
  for suffix in -wal -shm -journal; do
    source="$saved$suffix"
    if test -e "$source" || test -L "$source"; then
      test -f "$source" && test ! -L "$source" || return 1
      target="$live$suffix"
      test ! -e "$target" && test ! -L "$target" || return 1
      mv -- "$source" "$target" || return 1
    fi
  done
}
restore_quarantined_db "$quarantine/contentcrew.db" /var/lib/paihuo/data/contentcrew.db
sync
env PYTHONPATH=/srv/paihuo/current PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m deploy.session_secret_env --path /etc/paihuo/paihuo.env \
  --check-backup "$saved_env"
```

旧 DB 的 sidecar 一起接回，不能用 immutable 忽略 WAL 后声称完整。如素材也改过，
先接回修改前的精确副本，再按步骤 3 验收、新备份复验和恢复公网/timer。
DB swap 前失败时，不执行同 inode 的 `mv`，继续恢复旧 env 和已搬走的旧 sidecar；
仍在 live 的旧 sidecar 不搬到 rejected。此时 quarantine 的 DB 硬链接不是独立不变
快照，应在恢复一致性后另做 SQLite 在线备份再归档。保留 rejected DB/env 与恢复
记录，不能盲删。上述 same-inode 分支仅在应用始终停止、无写者且没有额外人工变更
这个不变量成立时有效；任一不成立或 live/quarantine sidecar 碰撞都停止，不覆盖。

## 实现依据与历史边界

| 实际依据 | 安全结论 | 操作入口 |
| --- | --- | --- |
| `simple/paihuo.service` | 当前单进程应用、DB/env 外置 | 停/启 paihuo.service |
| `simple/paihuo-backup-simple.service/.timer` | current shared 模块、小时触发、75 部分失败 | timer/service/journal 实物复验 |
| `backup_db.py` / `verify_backup.py` | SQLite 在线备份、逐表复验、隔离恢复 | 当前 release 的模块命令 |
| `session_secret_env.py` / `app/secureconfig.py` | 受控 env、独立包装密钥、密文绑定 | DB/env 配对与隔离认证 |
| `simple/common.sh` / `rollback.sh` | 共用锁、回滚只处理 DB/代码 | 人工 env/备份停止须补全 |

`backup_health.py` 仍保留供共享模块与已核验工具使用，但默认旧 attestation 路径不是
simple unit 的现用成功记录。已归档的控制面/ops/旧 units 仅按精确清单用于审计和
受控恢复，不能因此重新启用旧发布链或提前删除保留期内控制面。
