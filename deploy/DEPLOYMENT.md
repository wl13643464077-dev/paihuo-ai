# 派活 AI schema51-r7 生产发布与恢复

本文只描述 r7 的正式发布路径。核心边界是：root 只运行服务器上预先固定并验明
身份的控制工具，不运行候选 release 中的脚本、Python 模块、virtualenv、pip 或
console script。候选代码只有在完整证明、停服最终快照和低权限预检之后，才由
`paihuo` 服务账号运行。

## 1. 目录、身份与信任边界

- 不可变版本：`/srv/paihuo/releases/<release-id>`，最终为 `root:root`。
- 当前版本：`/srv/paihuo/current`，只允许升级状态机原子切换。
- 运行数据：`/var/lib/paihuo/data`，归 `paihuo:paihuo`。
- 周期备份：`/var/backups/paihuo`，`root:root 0700`。
- 升级控制面：`/var/lib/paihuo-upgrade`，`root:root 0700`。
- 入站制品：`/var/lib/paihuo-upgrade/incoming/<release-id>`。
- 离线 wheelhouse：`/var/cache/paihuo-wheelhouse/<release-id>`；cache 父目录
  `root:root 0755`，每版封印目录和 `wheels/` 为 `0555`、文件为 `0444`。
- 首次升级引导区：
  `/var/lib/paihuo-upgrade/bootstrap/releases/<release-id>`，root-only。
- 固定运维代码：`/usr/local/lib/paihuo-ops`。
- 固定升级入口：`/usr/local/sbin/paihuo-upgrade`。
- 低权限依赖构建账号：`paihuo-build`，无登录 shell、无生产 state 权限。
- 应用账号：`paihuo`；只读 smoke 账号：`paihuo-smoke`。
- 环境密钥：`/etc/paihuo/paihuo.env`，`root:root 0600`。

初始化受控目录和低权限账号：

```bash
sudo install -d -o root -g root -m 0700 /var/lib/paihuo-upgrade
sudo install -d -o root -g root -m 0755 /srv/paihuo/releases
sudo install -d -o root -g root -m 0700 /srv/paihuo/.venv-quarantine
sudo install -d -o root -g root -m 0700 /var/backups/paihuo
sudo install -d -o root -g root -m 0755 /var/cache/paihuo-wheelhouse
getent passwd paihuo-build >/dev/null || \
  sudo useradd --system --user-group --home-dir /nonexistent \
    --shell /usr/sbin/nologin paihuo-build
getent passwd paihuo-smoke >/dev/null || \
  sudo useradd --system --user-group --home-dir /nonexistent \
    --shell /usr/sbin/nologin paihuo-smoke

build_uid="$(id -u paihuo-build)"
build_gid="$(id -g paihuo-build)"
app_uid="$(id -u paihuo)"
app_gid="$(id -g paihuo)"
test "$build_uid" -ne 0
test "$build_gid" -ne 0
test "$build_uid" != "$app_uid"
test "$build_gid" != "$app_gid"
test "$(id -G paihuo-build)" = "$build_gid"
sudo -u paihuo-build test ! -r /var/lib/paihuo/data
sudo -u paihuo-build test ! -r /etc/paihuo/paihuo.env
sudo -u paihuo-build test ! -r /var/lib/paihuo-upgrade
```

以上验证对新建和已有账号一视同仁。任一断言失败都必须立即停止发布；不得自动
修改已有账号的 uid、gid、附加组或生产目录权限来迎合检查。

禁止覆盖 `current`、复制运行中的 WAL 数据库、让两个 release 共用 virtualenv、
从应用可写目录加载 root Python 模块，或用候选版入口替代固定升级入口。

## 2. 构建 release 制品

正式候选必须由 allowlist 确定性构建器产生。不得归档整个工作树或临时手写包含
清单：

```bash
set -euo pipefail
release_id=<从未使用过的UTC时间-schema51-r7>
source_date_epoch=<冻结时UTC-epoch>
output=/private/tmp/paihuo-release-build
venv/bin/python -m deploy.build_release \
  --source . \
  --output-dir "$output" \
  --release-id "$release_id" \
  --source-date-epoch "$source_date_epoch"
```

构建器拒绝数据库、素材、环境文件、凭据、符号链接、硬链接、特殊文件、
setuid/setgid 和未在 allowlist 中的路径；并规范化 tar 的 mode、owner、顺序和
mtime。外部 receipt 必须绑定 archive、manifest、payload/source tree SHA、
成员数及 `self_verified=true`。相同 source、release id 和
`SOURCE_DATE_EPOCH` 必须得到相同 archive SHA。

冻结候选仍须在构建环境完成全量测试、静态检查、依赖检查和独立 P0/P1 复审。
这些检查不能替代服务器上的独立制品验证。

## 3. 固定 out-of-band 可信工具

`verify_release.py` 与 `bootstrap_release.py` 是首次 r5→r6 的信任根，必须脱离
候选 archive 单独传输。wheelhouse 封印工具也按同一规则固定。操作者先在本地
记录 SHA256，再传到服务器临时入站目录：

```bash
sha256sum \
  deploy/verify_release.py \
  deploy/bootstrap_release.py \
  deploy/wheelhouse.py
```

把三条本地摘要保存到变更单。上传后，在服务器上先复算临时文件摘要；任何一项
与本地记录不同都立即停止。只有比对成功后，才安装到 root-only 目录：

```bash
sudo install -d -o root -g root -m 0700 \
  /var/lib/paihuo-upgrade/bootstrap

test "$(sha256sum /tmp/verify_release.py | cut -d' ' -f1)" = \
  "<本地-verify_release.py-SHA256>"
test "$(sha256sum /tmp/bootstrap_release.py | cut -d' ' -f1)" = \
  "<本地-bootstrap_release.py-SHA256>"
test "$(sha256sum /tmp/wheelhouse.py | cut -d' ' -f1)" = \
  "<本地-wheelhouse.py-SHA256>"

sudo install -o root -g root -m 0600 /tmp/verify_release.py \
  /var/lib/paihuo-upgrade/bootstrap/verify_release.py
sudo install -o root -g root -m 0600 /tmp/bootstrap_release.py \
  /var/lib/paihuo-upgrade/bootstrap/bootstrap_release.py
sudo install -o root -g root -m 0600 /tmp/wheelhouse.py \
  /var/lib/paihuo-upgrade/bootstrap/wheelhouse.py

sudo test "$(stat -c '%U:%G:%a:%h:%F' \
  /var/lib/paihuo-upgrade/bootstrap/verify_release.py)" = \
  "root:root:600:1:regular file"
sudo test "$(stat -c '%U:%G:%a:%h:%F' \
  /var/lib/paihuo-upgrade/bootstrap/bootstrap_release.py)" = \
  "root:root:600:1:regular file"
sudo test "$(stat -c '%U:%G:%a:%h:%F' \
  /var/lib/paihuo-upgrade/bootstrap/wheelhouse.py)" = \
  "root:root:600:1:regular file"
sudo test "$(sha256sum \
  /var/lib/paihuo-upgrade/bootstrap/verify_release.py | cut -d' ' -f1)" = \
  "<本地-verify_release.py-SHA256>"
sudo test "$(sha256sum \
  /var/lib/paihuo-upgrade/bootstrap/bootstrap_release.py | cut -d' ' -f1)" = \
  "<本地-bootstrap_release.py-SHA256>"
sudo test "$(sha256sum \
  /var/lib/paihuo-upgrade/bootstrap/wheelhouse.py | cut -d' ' -f1)" = \
  "<本地-wheelhouse.py-SHA256>"
```

再次对固定目标计算 SHA，并与本地记录逐项相等。固定工具的所有祖先目录都必须
是 root 所有且不可被 group/other 写。后续 root 调用统一使用：

```bash
sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin \
  PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I <固定工具绝对路径> <参数>
```

不得通过 `PYTHONPATH`、当前工作目录或候选 release 补齐这些工具的 import。
`bootstrap_release.py` 还会验证自身 `__file__`、inode、euid、祖先目录和固定
`verify_release.py`；任何不一致都拒绝进入 root 执行链。

## 4. 入站制品、独立验证与安全 materialize

为一个从未使用过的新 release id 创建 root-only 入站目录，安装 archive 和
receipt。不要直接解包，也不要让 root 执行 archive 内的任何文件：

```bash
release_id=<从未使用过的UTC时间-schema51-r7>
incoming="/var/lib/paihuo-upgrade/incoming/$release_id"
release="/srv/paihuo/releases/$release_id"
wheelhouse="/var/cache/paihuo-wheelhouse/$release_id"

sudo install -d -o root -g root -m 0700 "$incoming"
sudo install -o root -g root -m 0600 \
  "/tmp/$release_id.tar.gz" "$incoming/$release_id.tar.gz"
sudo install -o root -g root -m 0600 \
  "/tmp/$release_id.receipt.json" "$incoming/$release_id.receipt.json"
sudo test ! -e "$release"
```

由固定 verifier 在打开的文件描述符上验证 archive、receipt、manifest、tar
成员、路径、类型、mode、owner、mtime 和 tree SHA；验证通过才原子 materialize
到最终 release 绝对路径，并生成 root-only artifact attestation：

```bash
sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I \
  /var/lib/paihuo-upgrade/bootstrap/verify_release.py \
  --archive "$incoming/$release_id.tar.gz" \
  --receipt "$incoming/$release_id.receipt.json" \
  --extract-dir "$release" \
  --attestation "$incoming/release-artifact-attestation.json"

sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I \
  /var/lib/paihuo-upgrade/bootstrap/verify_release.py \
  --archive "$incoming/$release_id.tar.gz" \
  --receipt "$incoming/$release_id.receipt.json" \
  --artifact-attestation "$incoming/release-artifact-attestation.json"
```

第二条命令是独立只读复核。它把保留的 archive、receipt 和 artifact
attestation 重新交叉绑定；不得因第一条命令成功而省略。

## 5. root 封印离线 hash wheelhouse

依赖 wheel 必须在隔离构建环境下载并上传，生产服务器不访问包索引。root-only
incoming 中的 `source-wheels/` 只能是未封印输入；root 不从中执行代码。固定
wheelhouse 工具验证精确版本、wheel
METADATA、目标平台和 Python ABI，拒绝缺失、额外、重复、符号链接、硬链接或
特殊文件，然后原子生成：

- `/var/cache/paihuo-wheelhouse/<release-id>/wheels/`
- `/var/cache/paihuo-wheelhouse/<release-id>/requirements.hashed.lock`
- `/var/cache/paihuo-wheelhouse/<release-id>/wheelhouse-attestation.json`

cache 父目录为 `root:root 0755`，允许 `paihuo-build` 只读遍历但不可写；
每版封印目录和 `wheels/` 为 `0555`，文件为 `0444`。不得开放
`/var/lib/paihuo-upgrade` 或 incoming 的 `0700` 权限给构建账号。示例：

```bash
wheelhouse="/var/cache/paihuo-wheelhouse/$release_id"
source_wheels="/var/lib/paihuo-upgrade/incoming/$release_id/source-wheels"
sudo install -d -o paihuo-build -g paihuo-build -m 0700 "$source_wheels"
# 将低权限下载区中的 wheel 以 paihuo-build:paihuo-build 0600 安装到
# $source_wheels；root 只设定离线上传数据的受检身份，不取得文件所有权。
# root-only 0700 祖先使构建账号不能自行进入或在封印期间再次篡改。

sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I \
  /var/lib/paihuo-upgrade/bootstrap/wheelhouse.py \
  --seal \
  --source-lock "$release/requirements.lock.txt" \
  --source-wheels "$source_wheels" \
  --output "$wheelhouse" \
  --target-platform linux_x86_64 \
  --target-python cp312

sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I \
  /var/lib/paihuo-upgrade/bootstrap/wheelhouse.py \
  --verify \
  --output "$wheelhouse"
```

attestation 绑定 source lock、hashed lock、wheel set、平台、Python ABI、数量、
总字节数以及每个 wheel 的名称、版本、文件名、SHA256 和大小。安装前、引导
封印前和升级完成前均须复核；不能只相信目录只读位。

## 6. 低权限构建最终绝对 virtualenv

virtualenv 必须直接创建在最终绝对路径。root 只创建一个专供
`paihuo-build` 写入的空目录；创建 venv、安装依赖和运行 pip 的进程 euid 必须是
`paihuo-build`。该账号不能读取生产 state、升级凭据或 root 控制回执：

```bash
wheelhouse="/var/cache/paihuo-wheelhouse/$release_id"
umask 0022
sudo install -d -o paihuo-build -g paihuo-build -m 0755 "$release/venv"

# 部分 sudo 策略会把调用者的 0022 重置为 0002，因此必须在
# paihuo-build 子进程内再次固定 umask。`exec "$@"` 只执行已分隔的参数，
# 不把 release id 或路径拼成可解析的 shell 文本。
sudo -u paihuo-build /bin/sh -c '
  umask 0022
  exec "$@"
' sh /usr/bin/env -i \
  PATH=/usr/bin:/bin HOME=/nonexistent \
  PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m venv --copies "$release/venv"

# Ubuntu 的 venv 会额外建立冗余兼容链接 lib64 -> lib。仅允许构建账号在精确
# 证明链接名和相对目标后移除它；不得跟随、复制或泛化删除其他链接。
if test -L "$release/venv/lib64"; then
  test "$(/usr/bin/readlink "$release/venv/lib64")" = "lib"
  sudo -u paihuo-build /usr/bin/env -i \
    PATH=/usr/bin:/bin /usr/bin/unlink "$release/venv/lib64"
fi
test ! -e "$release/venv/lib64"

sudo -u paihuo-build /bin/sh -c '
  umask 0022
  exec "$@"
' sh /usr/bin/env -i \
  PATH="$release/venv/bin:/usr/bin:/bin" HOME=/nonexistent \
  PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 \
  PIP_NO_INPUT=1 PYTHONDONTWRITEBYTECODE=1 \
  "$release/venv/bin/python" -m pip install \
  --require-hashes \
  --no-index \
  --find-links "$wheelhouse/wheels" \
  --no-deps \
  -r "$wheelhouse/requirements.hashed.lock"
```

生产构建不允许联网、不允许 sdist、不允许依赖解析，也不允许从普通
`requirements.lock.txt` 安装。除上面经过精确证明并由构建账号移除的冗余
`lib64 -> lib` 外，若 `venv --copies` 仍产生任何内部符号链接，本次候选失败；
不得用递归解引用复制或泛化删除来“修复”它。修正构建工具后以新 release id
重新 materialize。

安装结束后，root 不使用递归 shell 命令接管候选树，也不运行候选 Python、pip
或 console script。固定 root-only wheelhouse 工具按 release id 锁定最终 venv，
拒绝符号链接、硬链接、跨设备和特殊文件，逐项以不跟随链接的方式接管并生成
venv tree 摘要：

```bash
sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I \
  /var/lib/paihuo-upgrade/bootstrap/wheelhouse.py \
  --adopt-venv \
  --release-id "$release_id"

# 当前 allowlist 制品不包含 data 路径。同时检查 `-e` 与 `-L`，
# 避免把断链符号链接当成“不存在”后覆盖。
sudo test ! -e "$release/data"
sudo test ! -L "$release/data"
sudo ln -s /var/lib/paihuo/data "$release/data"
sudo chown -h root:root "$release/data"
sudo test "$(readlink -f "$release/data")" = "/var/lib/paihuo/data"
```

`--adopt-venv` 必须先于 `data` 链接执行；它只允许触碰
`/srv/paihuo/releases/<release-id>/venv`，不得遍历 release 的 `data` 或生产
state。接管完成后 venv 根必须是 `root:root 0755`：`paihuo` 服务账号可只读
遍历并执行已验证 runtime，但不能写入。接管证明随后由 bootstrap stage和升级
回执再次绑定。

`data` 必须是 release 内唯一符号链接。若 archive materialize 了空的 `data`
目录，必须在创建链接前通过受控 materialize 流程处理；不得覆盖已存在路径。

创建并立即只读复核 materialized attestation：

```bash
sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I \
  /var/lib/paihuo-upgrade/bootstrap/verify_release.py \
  --materialized-release "$release" \
  --state /var/lib/paihuo/data \
  --artifact-attestation "$incoming/release-artifact-attestation.json" \
  --attestation "$incoming/materialized-release-attestation.json"

sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I \
  /var/lib/paihuo-upgrade/bootstrap/verify_release.py \
  --materialized-release "$release" \
  --state /var/lib/paihuo/data \
  --artifact-attestation "$incoming/release-artifact-attestation.json" \
  --materialized-attestation \
    "$incoming/materialized-release-attestation.json"
```

## 7. r5→r6 首次升级入口

首次升级不能从候选 wrapper 起步。固定
`/var/lib/paihuo-upgrade/bootstrap/bootstrap_release.py` 先重新验证 artifact、
materialized release、venv 树、固定 ops 闭包和 launcher，随后把它们复制进
root-only 引导 stage 并生成 bootstrap attestation：

```bash
sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I \
  /var/lib/paihuo-upgrade/bootstrap/bootstrap_release.py \
  --prepare-stage \
  --release-id "$release_id"
```

`--prepare-stage` 成功后，唯一允许的首次入口仍是同一个固定 helper 的
`--launch`。helper 会再次执行只读 `check_stage`，再用最小环境 `execve` 已封印
launcher；操作者不直接执行 stage 中的脚本：

```bash
sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I \
  /var/lib/paihuo-upgrade/bootstrap/bootstrap_release.py \
  --launch \
  --release-id "$release_id"
```

stage launcher 必须收到 helper 注入的内部标记，否则拒绝运行。它在任何停服前
依次执行：

1. 获取 root-only wrapper lock；
2. 验证当前加载的 ops、launcher、bootstrap attestation 和 release evidence；
3. 建立 durable wrapper transaction marker；
4. 快照旧的固定运维代码、systemd、Caddy 配置和固定 launcher；
5. 安装并验证新固定控制面；
6. 进入升级状态机。

被封印并由控制面事务安装的固定对象包括
`deploy/start_guard.py`、`deploy/smoke_readonly.py`、
`deploy/session_secret_env.py`、`deploy/contentcrew.service` 和
`deploy/caddy-paihuo-guard.conf`；Caddy guard 的固定目标是
`/etc/systemd/system/caddy.service.d/10-paihuo-guard.conf`。密钥检查由固定
ops 以 `-m deploy.session_secret_env` 执行。候选 release 中的同名文件只作为
经过摘要验证的安装材料，不能被 root 原地运行。

在 `prepare` 成功前不得替换 `/usr/local/lib/paihuo-ops` 或固定 launcher，否则
会丢失 r5 控制面的可恢复快照。

## 8. r6后唯一入口（常规升级唯一入口）

首次升级成功后，后续发布只允许使用 root-owned 固定 launcher：

```bash
sudo /usr/bin/env -i \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -I \
  /var/lib/paihuo-upgrade/bootstrap/bootstrap_release.py \
  --prepare-stage \
  --release-id "$release_id"

sudo /usr/local/sbin/paihuo-upgrade \
  --release-id "$release_id"
```

可选的 `--health-timeout` 必须是数值。正式入口不接受 release 路径、Python
路径、venv 路径、ops 路径或任意额外参数，避免操作者把候选路径注入 root
控制面。每个新候选仍须先完成第 3～6 节的制品、wheelhouse、低权限 venv 和
attestation 流程，并由固定 bootstrap helper 执行一次 `--prepare-stage`；常规
升级只是不再使用 bootstrap helper 的 `--launch`，而改由固定 launcher 启动。

固定 launcher 清空继承环境，只保留固定 `PATH`、
`PYTHONDONTWRITEBYTECODE=1` 和已验证 ops 的 `PYTHONPATH`；工作目录固定为
`/`。它不从候选 release import 控制模块。认证只读 smoke 只能由已随固定
ops 验明摘要的 `deploy/smoke_readonly.py` 执行，不能调用候选版 smoke 脚本。

## 9. 升级状态机、收据与证明

升级状态机按以下顺序收敛：

1. 在停服前验证固定控制面、环境密钥副本、artifact/materialized/bootstrap/
   wheelhouse/venv 证明和候选、旧版摘要。
2. 检查备份 timer、最近 24 小时备份新鲜度、完整性和磁盘余量。
3. 暂停周期备份，等待备份进程退出，先关闭 Caddy，再停止旧服务。
4. 创建停服最终 SQLite 快照，并在 root 控制目录预恢复
   `rollback-ready`；空间不足则不启动候选。
5. 以 `paihuo` 身份验证旧版和候选 runtime、精确锁版本、`pip check`、
   `yt-dlp` 和 preflight。root 不执行这些候选命令。
6. 原子切换 `current`，以 quiescent 模式启动候选；此时不恢复 worker 或外联。
7. 切换 validation 模式，由固定 `paihuo-smoke` 客户端执行认证只读 smoke。
8. smoke 成功后持久化 `cutover_committed`，删除 rollback-ready，才正常启动
   worker。
9. 生成并 root-attest 新备份，恢复 timer，最后开放 Caddy。
10. 再次验证 control、artifact、materialized、bootstrap、wheelhouse 和 venv
    收据绑定，写入 `status=succeeded, phase=complete`。

升级回执位于 `/var/lib/paihuo-upgrade/upgrade-*.json`，至少绑定：

- 候选、旧版、数据库和最终快照摘要；
- artifact archive、build receipt、manifest、payload/source tree；
- materialized attestation；
- bootstrap stage attestation、ops、launcher 和 venv tree；
- wheelhouse attestation、hashed lock、wheel set 和目标 ABI；
- 固定控制面 attestation 与密钥恢复副本；
- post-commit 备份、schema 和 HTTPS 健康证明。

恢复时只使用原回执中的固定路径和摘要，忽略新命令行对 archive、receipt、
attestation 或 venv 的替换。没有 `status=succeeded` 且 `phase=complete` 的回执，
不得宣称发布完成。

## 10. 自动恢复与回滚

SIGINT、SIGTERM、普通异常和 wrapper trap 都进入同一恢复协议。断电或 SIGKILL
后，下次固定入口先扫描未完成回执；它只处理旧回执绑定的 release 和证明，完成
恢复后退出，要求操作者复核再发起新升级。

cutover commit 之前失败时：

1. 关闭 Caddy 并证明候选服务已停止；
2. 重新验证最终快照及 rollback-ready 的源/目标双摘要；
3. 隔离候选写入后的数据库和 sidecar；
4. 从 root-only rollback-ready 原子恢复数据库；
5. 校验旧版摘要并原子切回；
6. 旧版先以无 worker 验证态完成健康检查和只读 smoke；
7. 写入 `rolled_back` 或 `recovered_rollback`；
8. 刷新 root-attested 备份、恢复 timer，最后开放 Caddy；
9. 根据 wrapper transaction attestation 原子恢复旧控制面。

schema48 会把历史 JSON 凭据字段迁移为绑定字段身份的密文，r6 无法读取迁移后的
数据库。cutover commit 之前的失败必须同时恢复停服前最终数据库快照和旧代码，
不得只把代码切回 r6 后继续使用 schema48 数据库；cutover commit 之后则沿用
本节的前向恢复协议，不自动降级代码或数据库。

一旦 `cutover_committed` 已持久化，不再自动回滚数据库，因为 worker 可能已经
产生不可逆外部效果。激活失败时保持应用和公网入口关闭，写入
`cutover_committed_activation_failed`，只允许按原回执继续恢复。

快照、旧版、固定控制面或任何 receipt-bound 证明不匹配时，状态写为
`rollback_failed`；无法证明服务已停止时写为
`rollback_failed_uncontained`。两者都阻止应用、代理和备份绕过启动闸，必须
人工审计，不得通过删除回执强行继续。

## 11. 发布后只读验证

```bash
curl -fsS https://paihuo.ai/healthz
sudo systemctl is-active \
  contentcrew.service caddy paihuo-backup.timer paihuo-backup-health.timer
sudo systemctl status \
  contentcrew.service paihuo-backup.service paihuo-backup-health.service \
  --no-pager
sudo journalctl -u contentcrew.service --since '-15 minutes' --no-pager
sudo ss -ltnp
```

应用只能监听 `127.0.0.1:8899`，公网只开放 Caddy 80/443。schema51-r7 还必须
证明数据库 schema ledger/user_version、历史 JSON 凭据已完成密文迁移、配置
密文、环境密钥副本和新备份一致；
只输出聚合，不输出业务正文、密钥、Cookie、配置值或供应商响应。

每个新 release 都从升级回执完成时刻重新计算 24 小时观察窗口。旧 release 的
观察不能让新版本提前通过最终阶段。`P1-OBS-1`（root TigerVNC 公网 5902）仍须
单独关闭或由用户明确接受；未获授权不得修改 VNC、Tailscale 或 UFW。
