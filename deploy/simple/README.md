# 派活 简易部署（1 台 Ubuntu 服务器，1–2 人就能维护）

这是当前唯一维护的生产部署通道：一条命令发布、一条命令回滚，出问题自动回滚。
旧的不可变发布链已退役；`deploy/DEPLOYMENT.md` 只保留历史迁移说明，不能作为现用
安装、发布或恢复手册。数据库、素材和配对密钥的恢复见 [备份与恢复手册](../BACKUP_RECOVERY.md)。

| 文件 | 作用 |
| --- | --- |
| `deploy.sh` | 发布：预检 → 备份 → 装新版本 → 停服快照+迁移 → 切换重启 → 冒烟，失败自动回滚 |
| `rollback.sh` | 手动回滚到上一个/指定版本，可同时恢复数据库备份 |
| `common.sh` | 上面两个脚本共用的函数（不要单独执行） |
| `paihuo.service` | systemd 服务单元（单进程、北京时间、内存上限、日志进 journald、安全沙箱） |
| `paihuo-backup-simple.service` / `.timer` | 每小时备份数据库、每天快照素材、可选异地同步 |

> **同一个数据库只能由一个应用进程使用**：当前服务是 `paihuo.service`。迁移尚未完成的
> 旧服务器须先停用历史 `contentcrew.service` 和旧备份 units，不能与 simple 同时运行。
> 仓库不再提供旧发布链；归档材料仅用于审计，不应重新启用。

## 目录布局

```
/srv/paihuo/src/                     git 仓库（运维在这里 git fetch）
/srv/paihuo/releases/<时间-提交号>/   每次发布一个目录：代码 + venv/，data -> /var/lib/paihuo/data
/srv/paihuo/current -> releases/...  线上版本（软链接，发布/回滚就是改它）
/srv/paihuo/deploy-history.log       发布/回滚记录
/var/lib/paihuo/data/                数据库 contentcrew.db、素材 assets/（归 paihuo 账号）
/srv/paihuo-pub/                     数字人照片/声音的临时公开文件
/var/backups/paihuo/                 数据库备份 db-*.db、素材快照 assets/（root 700）
/etc/paihuo/paihuo.env               密钥与配置（root 600）
/etc/paihuo/backup.env               异地备份配置（root 600，可选）
```

---

## 一、从零到上线

### 1. 准备服务器

- 推荐 **Ubuntu 24.04**（自带 Python 3.12；最低 Python 3.11），2 核 4G 内存、40G 以上磁盘。
- 服务器在中国大陆的，域名必须先完成 **ICP 备案**，否则 80/443 端口会被云厂商拦截。
- 安全组只开 22、80、443。应用只听本机 `127.0.0.1:8899`，公网一律走 Caddy。

```bash
sudo apt update
sudo apt install -y python3 python3-venv git curl ffmpeg fonts-noto-cjk
# Caddy（自动申请/续期 HTTPS 证书），按官网说明添加软件源后安装：
#   https://caddyserver.com/docs/install#debian-ubuntu-raspbian
sudo apt install -y caddy
```

`ffmpeg` 用于视频合成，`fonts-noto-cjk` 用于店员照片上的中文时间水印。

### 2. 建账号和目录

```bash
getent passwd paihuo >/dev/null || sudo useradd --system --user-group \
  --home-dir /var/lib/paihuo --shell /usr/sbin/nologin paihuo
sudo install -d -o root   -g root   -m 0755 /srv/paihuo /srv/paihuo/releases
sudo install -d -o paihuo -g paihuo -m 0750 /var/lib/paihuo /var/lib/paihuo/data
sudo install -d -o paihuo -g paihuo -m 0750 /srv/paihuo-pub
sudo install -d -o root   -g root   -m 0700 /var/backups/paihuo
sudo install -d -o root   -g root   -m 0755 /etc/paihuo
```

### 3. 拉代码

```bash
sudo git clone <仓库地址> /srv/paihuo/src
```

### 4. 生成密钥文件

下面这条命令会在 `/etc/paihuo/paihuo.env` 里生成两把互相独立的强随机密钥（会话签名、
配置加密），文件权限自动是 root 600，**不会把密钥打印出来**：

```bash
cd /srv/paihuo/src
sudo env PYTHONPATH=/srv/paihuo/src python3 -m deploy.session_secret_env --path /etc/paihuo/paihuo.env
```

然后用编辑器补上首次启动要用的 root 密码（至少 12 位，含字母和数字）：

```bash
sudo -e /etc/paihuo/paihuo.env
```

```ini
# 首次启动时用它建平台管理员账号 boss；登录改密后可以删掉这一行
CONTENTCREW_BOOTSTRAP_PASSWORD=<你自己想的强密码>
# 可选：服务异常时发企业微信群告警（群机器人地址）
# PAIHUO_ALERT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<机器人key>
# 可选：服务器出口公网 IP，老板绑定公众号时页面会提示把它加进 IP 白名单
# PAIHUO_PUBLIC_EGRESS_IP=<服务器公网IP>
```

> **配置加密密钥 `CONTENTCREW_CONFIG_KEY` 一旦用上就不能换、不能丢**：后台填的模型
> Key、短信/支付密钥都是用它加密后存进数据库的。请把 `/etc/paihuo/paihuo.env`
> 另外离线保存一份（比如加密 U 盘），和数据库备份配对保管。

### 5. 安装服务单元

```bash
cd /srv/paihuo/src
sudo install -m 644 deploy/simple/paihuo.service /etc/systemd/system/paihuo.service
# 可选：服务崩溃时发企业微信告警（需要上面配了 PAIHUO_ALERT_WEBHOOK）
sudo install -m 644 deploy/paihuo-failure-alert@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable paihuo
```

机器内存不是 4G 的，改一下单元里的 `MemoryHigh`/`MemoryMax`（大约给到总内存的 60%/75%）。

### 6. 第一次发布

先演练（只做预检、打印计划，不改任何东西），再正式发布：

```bash
cd /srv/paihuo/src
sudo bash deploy/simple/deploy.sh --dry-run --ref origin/main
sudo bash deploy/simple/deploy.sh --ref origin/main
```

服务器访问 PyPI 慢的，加国内镜像：

```bash
sudo PAIHUO_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
  bash deploy/simple/deploy.sh --ref origin/main
```

看到"发布成功"就说明本机 `http://127.0.0.1:8899` 已经能用了。

### 7. 域名和 HTTPS（用仓库里的 Caddyfile）

1. 域名解析：把 `你的域名` 和 `www.你的域名` 的 A 记录指到服务器公网 IP。
2. 复制配置并把里面的 `paihuo.ai, www.paihuo.ai` 换成你的域名：

```bash
sudo cp /srv/paihuo/src/deploy/Caddyfile /etc/caddy/Caddyfile
sudo -e /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

- 不要接回旧 Caddy 启动闸门；迁移遗留 drop-in 的核对见第 五 节。
- Caddyfile 里 `/pub/` 只直接放行 `paihuo-promo-*` 宣传片；数字人照片/声音走应用生成的
  **带过期时间的签名链接** `/pub/s/...`（反代到应用）。如果你以前的 Caddyfile 里有
  整目录公开的 `handle_path /pub/*`，一定要换成新版。
- 证书由 Caddy 自动申请和续期，首次访问 `https://你的域名` 可能要等几十秒。

### 8. 首次登录与初始化

1. 打开 `https://你的域名/login`，用户名 `boss`，密码是第 4 步填的
   `CONTENTCREW_BOOTSTRAP_PASSWORD`。登录后立刻在「我的」里改密码。
2. 改完密码后，把 `CONTENTCREW_BOOTSTRAP_PASSWORD` 这一行从 `/etc/paihuo/paihuo.env` 删掉
   （它只在数据库里还没有任何账号时才会用到）。
3. 忘了 boss 密码：见下文"常见故障"。

### 9. 配置各项服务（都在网页后台里填，密钥加密存库）

用 boss 登录后，「我的 → 后台」是平台管理员设置，「获客 → 发布渠道」「我的 → 套餐」
里有对应卡片。每一项都是**可选**的，不配就用不了对应功能，不影响其他功能。

| 功能 | 在哪里配 | 需要准备什么 |
| --- | --- | --- |
| 大模型（必配其一） | 后台 →「模型供应商 · 国内直连」或「旧通道 · OpenAI 兼容中转网关」 | 国内直连：DeepSeek / 通义千问（百炼）/ 智谱 / Kimi / 火山方舟 任一家的 API Key；在该厂商完成实名和**生成式 AI 服务备案/登记**要求。填好后点"测试连接"，再把"默认模型通道"切过去 |
| 联网查资料 | 同一张卡片的"联网查资料" | 博查搜索 API Key（或选用通义自带联网） |
| 短信验证码登录 | 后台 → 短信登录 卡片（默认关闭） | 阿里云短信：AccessKey（建议 RAM 子账号只给短信权限）、已审核的**短信签名**和**验证码模板**（模板变量 `code`） |
| 微信扫码支付 | 「我的 → 套餐」页，平台管理员可见"填写 / 修改商户配置"（默认关闭） | 微信支付**商户号**（需营业执照）、关联的公众号/小程序 AppID、商户 API 证书序列号和私钥、APIv3 密钥、微信支付公钥 ID 和公钥；回调地址填 `https://你的域名/api/pay/wxpay/notify` |
| 企业微信群通知 | 老板自己在「获客 → 发布渠道」填群机器人 Webhook | 企业微信里建群 → 添加群机器人 → 复制 Webhook 地址 |
| 公众号草稿箱 | 同上「发布渠道」 | 已认证公众号的 AppID/AppSecret，并把服务器出口 IP 加进公众号 IP 白名单 |
| 高风险功能 | 后台 →「高风险功能开关」 | 默认全部关闭，见 `docs/UPGRADE_NOTES.md`，打开前先评估合规风险 |

### 10. 配置备份（必须做）

```bash
cd /srv/paihuo/src
sudo install -m 644 deploy/simple/paihuo-backup-simple.service /etc/systemd/system/
sudo install -m 644 deploy/simple/paihuo-backup-simple.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start paihuo-backup-simple.service     # 先手动跑一次
sudo systemctl show paihuo-backup-simple.service -p ExecMainStatus --no-pager
sudo ls -l /var/backups/paihuo/
sudo systemctl enable --now paihuo-backup-simple.timer
```

- 每小时一份数据库备份（在线一致性快照 + 完整性校验 + 恢复演练），保留 14 天且至少 24 份；
  素材（`/var/lib/paihuo/data/assets`、`/srv/paihuo-pub`）约每天一份硬链接增量快照。
- `ExecMainStatus=75` 表示数据库备份成功、但素材快照或异地同步失败，看
  `journalctl -u paihuo-backup-simple`。
- 如果这台机器以前装过旧备份 units，先按第 五 节停用它们，再启用 simple timer；
  别让两个备份同时跑。simple unit 的精确备份/恢复演练报告写入 journal，未安装旧
  backup-health timer，也不会自动生成旧控制面的 attestation。

**异地备份（强烈建议）**：服务器整机坏了，本机备份也就没了。在阿里云 OSS 或腾讯云 COS
建一个私有桶、一个只有上传/读取/列举权限的子账号，然后：

```bash
sudo apt install -y rclone
sudo install -o root -g root -m 0600 /dev/null /etc/paihuo/rclone.conf
sudo -e /etc/paihuo/rclone.conf        # 写法见 deploy/BACKUP_RECOVERY.md「阿里云 OSS」
sudo install -o root -g root -m 0600 /dev/null /etc/paihuo/backup.env
sudo -e /etc/paihuo/backup.env
```

```ini
PAIHUO_BACKUP_REMOTE=rclone:paihuo-oss:paihuo-backup/prod
RCLONE_CONFIG=/etc/paihuo/rclone.conf
```

再手动跑一次 `sudo systemctl start paihuo-backup-simple.service`，确认 OSS/COS 里出现了
`db/` 和 `assets/current/`。异地恢复步骤见 `deploy/BACKUP_RECOVERY.md「从异地恢复」`。
`/etc/paihuo/paihuo.env` 不会被自动上传，请按第 4 步的提醒单独离线保管。

---

## 二、日常发布

```bash
cd /srv/paihuo/src
sudo git fetch
sudo bash deploy/simple/deploy.sh --dry-run --ref origin/main   # 先看计划
sudo bash deploy/simple/deploy.sh --ref origin/main
```

每一步都会打印中文进度。关键行为：

1. **预检**：Python ≥ 3.11、磁盘够、密钥文件在且权限 600、服务单元已安装、端口没被别人占、
   没有第二个派活进程（`WEB_CONCURRENCY`、手动起的 uvicorn、旧的 `contentcrew.service`
   都会被拦下）、数据库版本不比新代码新。任何一项不过就**什么都不做**直接退出。
2. **在线备份**：调用 `deploy/backup_db.py`，数据库 + 素材快照，服务照常运行。
3. **装新版本**：`releases/<时间-提交号>/` 放代码、建 venv、装 `requirements.lock.txt`，
   服务照常运行（这一步最慢，几分钟）。
4. **停服 → 停服快照 → 只迁移不启动**：从这里开始用户会短暂打不开（通常十几秒到一分钟）。
   停服这一刻再做一份快照（回滚用它，一条数据都不丢），然后以 `paihuo` 账号用新代码
   `from app import instancelock, db; instancelock.acquire(db.DB_PATH); db.conn()`
   ——先拿单进程锁（拿不到说明还有进程在跑，绝不迁移），再"连接即迁移"。
5. **切换 current 并 `systemctl restart paihuo`**。
6. **冒烟**：本机 `/healthz`、`/healthz?deep=1`（所有后台循环在跑）、`/login` 都要 200。

**失败自动回滚的顺序**（数据库迁移不可逆，必须先恢复数据库再切回旧代码）：

```
停服 → 用第 4 步的停服快照恢复数据库（换下来的库挪到 data/rollback-quarantine-*）
     → current 切回上一个版本 → 启动 → 冒烟
```

为什么不能反过来：旧代码看到新版本的数据库会拒绝启动（"已拒绝降级启动"）。
如果失败发生在第 1–3 步，线上服务根本没动过，只会删掉没用上的新版本目录。

发布成功后会自动清理旧版本，保留最近 5 个（`PAIHUO_KEEP_RELEASES` 可调）。

其他用法：

```bash
# 从一个解压好的代码目录发布（服务器不方便用 git 时）
sudo bash deploy/simple/deploy.sh --source-dir /tmp/paihuo-code
# 后台循环刚好在重启的机器上起得慢，deep 检查只警告不回滚
sudo bash deploy/simple/deploy.sh --ref origin/main --deep-optional
```

## 三、手动回滚

```bash
R=/srv/paihuo/current/deploy/simple/rollback.sh
sudo bash $R --list                        # 看有哪些版本、各自支持的数据库版本、发布前快照
sudo bash $R --dry-run                     # 演练回滚到上一个版本
sudo bash $R                               # 回滚到上一个版本（数据库版本兼容时）
sudo bash $R --restore-deploy-snapshot     # 回滚并恢复"当前版本发布前"的停服快照（最常用）
sudo bash $R --to <release-id> --restore-backup /var/backups/paihuo/db-2026-09-26T021455Z.db
```

- 数据库版本比目标版本新时，`rollback.sh` 会拒绝，必须带上 `--restore-deploy-snapshot`
  或 `--restore-backup`。**恢复备份会丢掉备份之后的新数据**（新任务、上传、充值），
  换下来的库保存在 `/var/lib/paihuo/data/rollback-quarantine-*`，需要时可以从里面捞数据，
  确认没问题前不要删。
- 顺序固定为：停服 → 恢复数据库 → 切换 current → 启动 → 冒烟。
- `rollback.sh` 不会自动恢复 `/etc/paihuo/paihuo.env`，也不会替你停备份 timer。
  涉及密钥配对变化或人工 DB 恢复时，须先按 [生产恢复](../BACKUP_RECOVERY.md#生产恢复)
  完成维护锁、simple timer/service 停止及 DB/env 配对校验，不能只加 `--restore-backup`。
- 只想恢复素材文件：见 `deploy/BACKUP_RECOVERY.md「素材文件快照」`（先停服，再 rsync，不带 `--delete`）。

## 四、常见故障排查

先看日志：`sudo journalctl -u paihuo -n 200 --no-pager`，实时跟踪加 `-f`。

| 现象 | 原因和处理 |
| --- | --- |
| 预检报"读不了 paihuo.env" | 没用 sudo。演练也要 `sudo bash ... --dry-run` |
| 预检报"旧的 contentcrew.service 还在运行" | 从旧体系切过来：`sudo systemctl disable --now contentcrew.service`，确认 `systemctl is-active contentcrew` 不是 active |
| 安装依赖很慢或失败 | 设 `PAIHUO_PIP_INDEX_URL` 用国内镜像；磁盘不够就清理 `/var/backups/paihuo` 里过旧的手工文件 |
| 日志里"已有另一个派活进程……拒绝启动" | 同一个数据库上有第二个进程：`pgrep -af 'uvicorn app.main:app'` 找出来停掉；不要设 `WEB_CONCURRENCY` |
| 日志里"unsafe migration lock" | 数据目录里的 `contentcrew.db.migration.lock` 不归 paihuo 或权限不是 600（通常是有人用 root 手动跑过程序）：停服后 `sudo chown paihuo:paihuo /var/lib/paihuo/data/contentcrew.db*` 并 `sudo chmod 600 /var/lib/paihuo/data/contentcrew.db.*lock` |
| 日志里"数据库 schema vX 高于当前程序支持的 vY，已拒绝降级启动" | 旧代码配了新数据库：用 `rollback.sh --restore-deploy-snapshot` 或切回新版本 |
| 日志里"CONTENTCREW_SESSION_SECRET 未配置，生产启动已拒绝" | 密钥文件缺项，重跑第 4 步的生成命令（已有的密钥不会被改） |
| 日志里"空数据库启动前必须设置 CONTENTCREW_BOOTSTRAP_PASSWORD" | 首次启动没填 root 密码，见第 4 步 |
| 服务反复重启后停住 | 5 分钟内崩溃 10 次会停止拉起；修好后 `sudo systemctl reset-failed paihuo && sudo systemctl start paihuo` |
| 被系统杀掉（日志有 `oom-kill` / `memory.max`） | 内存超了 `MemoryMax`：换大内存机器或调高单元里的上限 |
| 浏览器 502 | 应用没起来或还在启动：`curl -s http://127.0.0.1:8899/healthz`；Caddy 日志 `journalctl -u caddy` |
| `/healthz?deep=1` 返回 503 | 返回内容里 `loops` 会列出哪个后台循环 `stale`/`stopped`，按名字在日志里搜；重启服务通常能恢复 |
| HTTPS 证书申请失败 | 域名没解析到本机、80/443 没开、或大陆服务器域名未备案 |
| 忘了 boss 密码 | 服务运行中执行 `sudo env PYTHONPATH=/srv/paihuo/current python3 -m deploy.rotate_boss_password --database /var/lib/paihuo/data/contentcrew.db --output /root/boss-password.txt`，新密码写在只有 root 能读的这个文件里（文件已存在会拒绝），`sudo cat` 看完就 `sudo rm` |
| 需要看某次发布/回滚做了什么 | `cat /srv/paihuo/deploy-history.log`，各版本目录下的 `.paihuo-release` 记录了上一个版本和发布前快照 |

## 五、历史服务器迁移说明（旧发布链已退役）

仅供尚未迁移的历史服务器核对。已完成 simple 迁移和归档的服务器不要重复执行。
旧目录布局（`/srv/paihuo/releases`、`current`、`/var/lib/paihuo/data`）可保留，
迁移不删除业务数据。先核对旧 units 是否实际存在，再停用存在的项：

```bash
for unit in contentcrew.service paihuo-backup.timer paihuo-backup.service \
  paihuo-backup-health.timer paihuo-backup-health.service; do
  if systemctl cat "$unit" >/dev/null 2>&1; then
    sudo systemctl disable --now "$unit"
  fi
done
sudo install -m 644 /srv/paihuo/src/deploy/simple/paihuo.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable paihuo
sudo bash /srv/paihuo/src/deploy/simple/deploy.sh --ref origin/main
```

保留的历史 release 只有在 `run.sh`、`venv/bin/python`、schema 与所选 DB/env 配对都核验
兼容后，才能作为 `rollback.sh --to` 的目标；不能只按目录时间猜上一版，也不存在固定的
`previous` 链接保证。先 `--list`，再显式 `--to` 演练。Caddy 曾使用旧启动闸门时，先用
`systemctl cat caddy.service` 找出精确 drop-in，备份和核对引用后再移出该文件并
`daemon-reload`；不要批量删除 `/etc/systemd/system/caddy.service.d/`。
