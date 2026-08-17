# 联网工具与员工能力热修发布

日期：2026-08-09

## 发布前门禁

- schema：50（不变）。
- 全量单元/集成/浏览器测试：1031/1031 通过。
- compileall、Node 语法、shell 语法、pip check、diff check：全部通过。
- 独立代码与需求审查：PASS，无确定性 P0/P1。

## 候选版本

- `20260809T083151Z-schema50-r7-hotfix1`：制品验证通过，但候选 venv 因目录
  mode 不合同被固定安全工具拒绝；未进入升级状态机，未切换 `current`。
- `20260809T083438Z-schema50-r7-hotfix2`：确认新机 `sudo -u paihuo-build` 会把调用者
  umask 从 `0022` 重置为 `0002`；同样在 venv 收养门前停止，未切换生产。
- 已修正发布手册与契约测试：低权限子进程内显式 `umask 0022`，制品无
  `data` 路径时改为“同时断言非实体且非符号链接”后再创建受控链接。
- 最终版本：`20260809T083804Z-schema50-r7-hotfix3`。
- artifact SHA-256：`970a570a15a56c74e50d10d6b7597d1da43f4059d0f3087fd57c5ee216199fe9`。
- manifest SHA-256：`655d0fd3a42959d7ee86168700d472303f1cbad2a09024511d3c623346688e0f`。
- payload tree SHA-256：`cfb2f9668384b9ad11a745cb18b2bcf3d27b10f25ddd7d45fcef82e05282931f`。
- source tree SHA-256：`eb12844ad858961ca492b5be5f921d94d03e30d23446c27065896c514a4e6b4a`。
- 固定升级入口执行成功；升级回执 `status=succeeded`、`phase=complete`，
  schema 仍为 50，`current` 已原子切换至最终版本。

## 上线后验收

- `contentcrew` 与 Caddy 均为 `active/running`、`Result=success`、
  `ExecMainStatus=0`、`NRestarts=0`；应用进程来自最终 release/venv。
- 本机 `/healthz`：HTTP 200、约 0.0037 秒；正式域名 HTTPS：HTTP 200、
  约 0.2275 秒。
- 应用只监听 `127.0.0.1:8899`，公网业务入口仅由 Caddy 暴露 80/443。
- 备份与备份健康 timer 均 enabled、active/waiting；只读校验
  `ok=true`、`fresh=true`、`integrity=ok`、`disk_ok=true`。
- 生产服务账号执行低敏真实 WebSearch：attempts=1、success=1、errors=0；
  未保存查询、回答或供应商响应。
- 正式域名 Boss 登录、会话签发、`/api/auth/me` 的 root 身份校验和退出均为
  HTTP 200；凭据与 Cookie 只在内存使用。
- 官网与登录页已在真实浏览器完成加载、DOM 与视觉渲染检查。
- hotfix3 发布当时，今日必发、线索雷达的生产业务任务验收仍在进行；最终结果
  只记录状态、数量、追溯性与计费聚合，不记录业务正文。

## 生产冒烟发现与 hotfix4

- hotfix3 首轮生产业务冒烟没有被当作通过：热点任务在约 135 秒后
  `failed/refunded`，稳定分类为下游 HTTP 200 空交付；线索雷达约 39 秒后
  因无可核验原帖 `failed/refunded`。两者均未长期运行、未产生点数或待结算项。
- 首轮验收输入曾把“系统验收/勿跟进”写进实际行业/产品检索词，污染了搜索，
  因此该次零召回不能作为放宽原帖门禁的依据。
- 修复下游空交付的有界重试和非 SSE JSON 兼容；全部尝试共享同一墙钟截止，
  取消正常传播，成本/令牌按实际尝试汇总。
- 线索雷达新增有界 Bing RSS 公开搜索入口；WebSearch 提示只要求实际获准的
  `WebSearch`。搜索页、跳转页、首页、账号页、编码绕过和点号路径均拒绝，
  候选仍必须实际打开、同站终跳并与标题/正文对应。
- 最终回归：1044/1044 通过；compileall、Node、shell、pip check、diff check
  全部通过；两项独立复审均 PASS，无剩余确定性 P0/P1。
- 最终替代版本：`20260809T090800Z-schema50-r7-hotfix4`。
- artifact SHA-256：`7574f89888420cc8ec3dd4ae277170be0fad677737a40e7d794502eeca7089f2`。
- manifest SHA-256：`316027b88c131aa40381a9b3f524edaa59696bdf59bc88947e849c133a9b4946`。
- payload tree SHA-256：`eb59baadf877a67d7bddfa7480e0dc69857dd8d4331366b9f0da7b0353374f4e`。
- source tree SHA-256：`3edaa31844203513490d33891ffd1ed4af5e6869e618f5687937f73af7b9988c`。
- 固定升级状态机执行成功；`status=succeeded`、`phase=complete`，schema 50；
  切换前在线/停机快照、恢复演练、回滚准备和切换后新备份均通过。
- hotfix4 正式域名健康与正常业务词的热点/线索雷达终态由下方 hotfix5/6
  生产验收记录接续。

## hotfix5：结构化来源接入与生产发现

- 版本：`20260809T095429Z-schema50-r7-hotfix5`，升级回执成功，schema 50。
- 将 Claude Code WebSearch 的结构化 `title/url` 来源从匹配成功的
  `tool_use_result` 安全提取，模型自由文本不能制造可信 URL；候选继续经过
  详情路径、逐跳 SSRF、同站终跳、正文和标题对应门禁。
- 正常参数生产任务没有卡死且正确退款，但最终为 `failed`。只读归因证明：
  WebSearch 合并后至少已有 1 条严格核验来源，随后可选的岗位分析模型在
  75 秒超时，原子结果因此被整体丢弃。这不是“搜索没有结果”，而是可选增强
  错误地成为核心来源交付的单点门槛。

## hotfix6：已核验来源优先交付

- 最终版本：`20260809T103251Z-schema50-r7-hotfix6`。
- artifact SHA-256：`308f31dad7029ede7d94e9bd486d5fe531ddeb6a0db6f92d2b5e412bc1d84146`。
- manifest SHA-256：`349517b7f42ec6ae57adf6b155c9a64abea828a77ec82da22f74ea0021481393`。
- payload tree SHA-256：`a0ca0f75f808dae40ba5c59e16a2951e7783009f3c41d59ed14fcca1aac8333b`。
- source tree SHA-256：`264da2ec1c2b8a6cda3df1dcc1fd5620d2a18124b5bc8e9fcc58cd5fb9ac86bd`。
- 最终全量测试 1070/1070 通过；compileall、Node、shell、pip check、
  `git diff --check` 全部通过；独立终审 PASS，无剩余确定性 P0/P1。
- 已核验原帖成为核心成功资产；岗位画像/话术分析改为 best-effort。主模型
  路由在任务开始时冻结，精确超时才允许一次不同的已上架 API 模型兜底；
  主阶段 75 秒绝对截止、主备共享 130 秒。若分析仍失败，可在不编造内容、
  不改变 URL 的前提下交付保守降级卡；没有核验来源仍必须失败退款。
- 主/备分析提示中的标题和摘要会先去 URL、控制字符并限长；冻结的来源 URL
  不进入模型，模型也不能新增或改写原帖地址。

## hotfix6 上线与正式验收

- 固定升级状态机完成：`status=succeeded`、`phase=complete`；切换前后备份、
  恢复演练、回滚准备、切换后新备份和 HTTPS 证明均通过。
- `current`、contentcrew 进程 cwd/exe 均精确绑定 hotfix6；contentcrew 与
  Caddy `active/running`、`Result=success`、`ExecMainStatus=0`、`NRestarts=0`。
- manifest、数据库 ledger、`PRAGMA user_version` 均为 schema 50；备份健康、
  完整性、新鲜度、磁盘和两个 timer 均通过。
- `/healthz`：本机 HTTP 200（约 0.011 秒），正式 HTTPS 200（约 0.062 秒）；
  应用 8899 仍仅监听 `127.0.0.1`。
- 正式域名 Boss 登录 HTTP 200，root 身份校验通过。只创建 1 条正常参数
  线索雷达任务：104.7 秒后 `done`，2 条线索、2 个有效且实际可访问的原帖、
  2 个平台匹配，`analysis_status=complete`。计费成功、0 点、余额变化 0；
  终点活动热点/线索任务为 0，待结算操作为 0。
- 验收全过程未记录凭据、Cookie、业务正文、原帖 URL、原始日志或供应商响应。

## schema51：巡店、连续协作与行业老板看板

- 首个 schema51 版本：`20260809T142216Z-schema51-r7-hotfix7`。完成数据库迁移、
  431 名员工登记（430 名文本员工 + 1 名巡店经理）、连续版本线程、巡店证据/
  整改/复查状态机和行业老板只读看板。
- 固定升级器回执为 `succeeded/complete`；schema ledger、
  `PRAGMA user_version` 均为 51，服务、备份、正式 HTTPS 和只读 Boss API 通过。
- hotfix7 首次真实巡店虽然 HTTP 与模型调用成功，却错误返回 100 分、0 问题。
  该结果被明确判为业务 FAIL：线上默认 `deepseek-v4-flash` 是文本模型，旧
  `call_vision` 没有视觉能力门禁，空问题也会被当作完成。

## hotfix8：视觉路由修复，但合同验收仍失败

- 版本：`20260810T023451Z-schema51-r7-hotfix8`。
- 新增显式 `supports_vision`、独立视觉模型路由、逐图标签、逐图覆盖与零问题
  异模复核，文本模型会在联网前失败关闭。
- 正式巡店只创建 1 条记录，约 29 秒后安全失败并退款；没有问题/证据/整改半写，
  也没有活动任务或待结算遗留。失败原因是视觉模型返回与严格结构化合同存在
  非确定性格式漂移，因此 hotfix8 仍未被记为业务通过。
- 独立真实像素 canary 证明 `gpt-5.5` 与 `claude-opus-4-8` 各 1 次均能准确读取
  图片内容；问题从“模型看不见”收敛为“运行时 JSON 合同不够抗漂移”。

## hotfix9：最终不可变版本

- 最终版本：`20260810T033419Z-schema51-r7-hotfix9`。
- archive SHA-256：`1f5f67cedd6f7b4d0725d87d06bd18e95214b2162d738a67c5b60938bd017c0e`。
- build receipt SHA-256：`d86be344465bbd254b4f91749d007e4ff41c8e74fe12f04a2ebb445fa7848135`。
- manifest SHA-256：`ccea7227665c6887c827e46404c61e2b7679f76cee5f85eb7f7acac0c8447867`。
- payload tree SHA-256：`1fd0e8dd2d3d98d1d90f17f35541182bc1b4f4b71100a38a4add028a76adab28`。
- source tree SHA-256：`50f4ee99fc0c04f89bf3dc2338558e923ce6e3bb0657089dbdea66d775299bd1`。
- 升级回执：
  `/var/lib/paihuo-upgrade/upgrade-20260810T033419Z-schema51-r7-hotfix9-20260810T035149-258913Z-45eceed883b1.json`；
  唯一匹配、root-only、`status=succeeded`、`phase=complete`。
- 运行时最终 JSON 合同位于所有可编辑员工模板之后，并绑定本次真实照片 ID 与
  数量；同一视觉模型只允许 1 次合同格式重做，首轮原文不回传、不落日志/数据库。
- 主调用、格式重做和零问题异模复核共享 300 秒绝对截止；取消、泄露、供应商错误、
  不可分析或显式低置信不会被格式重做掩盖。
- 只规范表现层差异，绝不补造照片 ID、逐图审查、证据或 action；负责人、方案、
  期限缺失/null/空值统一触发可控重做，不能伪造“待店长指派”或默认期限。
- 最终全量测试 `1199/1199`；compileall、Node、shell、`pip check`、
  `git diff --check` 全绿；独立终审无剩余确定性 P0/P1。

## hotfix9 上线与真实业务验收

- `current`、contentcrew 进程 cwd/exe 均精确绑定 hotfix9；contentcrew、Caddy
  active/running、退出码 0、零重启。数据库 quick check、schema51、备份与两个
  timer 均通过；8899 只监听 `127.0.0.1`，本机与正式 HTTPS 多次返回 200。
- 固定 artifact、materialized、wheelhouse、bootstrap 四条证据链均独立只读
  复验通过；release manifest 精确 218/218，无缺失、额外条目或 release 外 pyc。
- 巡店业务门：仅对原失败 visit #2 / task #8 发 1 次免费重试，复用唯一原照片，
  没有新建巡店。最终 `completed/issues_found`，评分 45；逐图审查 1，问题 4，
  证据 4，整改动作 4，时间线事件 7。任务 `done/included`、retry_count=1、0 点，
  活动任务与待结算均为 0。
- 连续协作业务门：撰稿人 idx=3 只创建首轮 task #9 和第二轮 task #10；thread #1
  共有 2 个完成版本，最终 `satisfied`，accepted/current 均为 #10。两轮共 0 点，
  未重复建单，活动任务、待结算任务和计费操作均为 0。
- 老板看板业务门：正式生产的汽车行业总览、巡店经理卡和员工下钻均 HTTP 200，
  能看到本期完成巡店；4 项行业经营指标保持 `availability=false/value=null`，
  明确“待接入”，没有用任务量伪造经营数据。
- 验收脚本只输出 ID、状态和数量；未输出 Cookie、凭据、巡店照片、任务正文、
  问题描述、证据内容或供应商响应，也未调用 logout 影响现有会话。

---

## schema52 生产发布

日期：2026-08-11

状态：**已上线；固定升级回执 `succeeded/complete`，正式域名只读验收通过**

### 授权与当前生产事实

- 用户已明确要求“部署到线上去”，本轮具备执行 schema52 受控生产升级的授权。
- 发布前生产为 `20260810T033419Z-schema51-r7-hotfix9` / schema51；最终树独审和测试
  通过后，现场生成并冻结 `20260811T064409Z-schema52-r7`，创建前 release、incoming、
  wheelhouse、bootstrap 四个精确路径均不存在。
- 当前 `current`、contentcrew 进程 cwd/exe 均精确指向 schema52 release；数据库
  `PRAGMA user_version=52`、ledger 最大版本 52、`quick_check=ok`。

### 生产只读 preflight

- 当前 release、进程 cwd/exe、升级回执、contentcrew/Caddy、正式域名健康、数据库
  quick check、schema51 账本、备份新鲜度/校验/恢复证据、磁盘、升级锁和固定信任根
  均通过只读检查。
- 待结算计费与活动执行任务为 0；历史 3 条 avatar failed+charged 已确认属于平台
  租户旧语义债且点数为 0，不构成本次切换账务阻塞。1 条
  `inspection_action.status=in_progress` 是人工整改业务状态，不是运行中的 worker；
  关联巡店/任务已终态且无待结算，不更改该业务状态。
- 可复用的锁定 wheel 集与当前 `requirements.lock.txt` 一致，容量和架构满足新建
  schema52 隔离 wheelhouse/venv；复用仅限复制已验证 wheel 字节，禁止链接旧 sealed
  目录、旧 venv 或旧 attestation。
- 只读 preflight 结论为 GO，但 GO 仅表示现场可进入最终发布流程；最终候选独审、
  回归或不可变构建任一失败仍必须停止。

### 已完成发布门禁

1. 留存 v2 最终独立终审 PASS，schema52 最终分组 `313/313`、静态/依赖/diff 检查全绿。
2. 使用同一最终 release ID 与 source date 在 `/private/tmp` 双构建，archive 与 receipt
   字节一致；清单包含导入、标准、覆盖、巡店实现及 XLSX 模板，不含数据、缓存、
   密钥或运行时文件。
3. 上传到全新 incoming，固定工具验证并物化；新建并 seal wheelhouse、离线安装全新
   venv，创建并只读复验 materialized attestation。
4. 即时 live gates 通过后，仅运行固定 `/usr/local/sbin/paihuo-upgrade`；唯一
   `succeeded/complete` 回执、`current`/cwd/exe、新 schema52 ledger/user_version、
   健康、备份与只读 smoke 为切换成功依据。
5. 正式域名核验入口：办公室 → 巡店经理 → 打开巡店工作台 → 页面顶部
   “批量导入门店”。root 只读会话核验 11 个行业、门店搜索、版本化检查清单、
   7 个拍摄槽位、7 个经营指标及模板下载全部通过；未创建任务、导入或模型调用。

### 最终证据

- release ID：`20260811T064409Z-schema52-r7`。
- archive SHA-256：`f651a332806b84bf6fd294129223f56ce9f6a87e38eaa40f056c9db3ea6871e8`；
  build receipt：`87c556d81833ecc2509235c341dba1e225f29177b6e6cdac22eb042c2fa0fd14`；
  manifest：`be0f1c31f1a24e235bb6fca410f7fb7df12a89f588f9f4e8684a18c56c3410b6`；
  payload tree：`21f59ba039cd7003934d42c186653cb3c0498f0336e3076a86b83f8d24eacf74`；
  source tree：`bbf51cb31f845dd372b6d4a7b44eb52f4098bb62858c7a58cbcb2ca2b7a2118d`。
- 升级回执：
  `/var/lib/paihuo-upgrade/upgrade-20260811T064409Z-schema52-r7-20260811T070746-586842Z-ae3adbd8aaaa.json`；
  `status=succeeded`、`phase=complete`、post-commit proof `ok=true`、备份 schema52、
  正式 HTTPS 200。
- 切换后 contentcrew/Caddy/备份 timers 全部 active，`NRestarts=0`；活动任务、巡店分析、
  导入预览、待结算 task 与 billing operation 均为 0。最新周期备份
  `db-2026-08-11T070918Z.db`，`integrity_check=ok`。
- 正式域名模板返回 200、`Cache-Control: no-store`、29,831 bytes，SHA-256 与候选中的
  四工作表模板一致；门店主表包含门店编号、店名、区域、详细地址、店长姓名/工号/
  手机号等字段，经营数据表承载真实指标、期间、数值、单位与来源。
- postdeploy 独立审计脚本曾因漏加 `-B` 在 current release 生成 4 个 manifest 外 pyc；
  已将整棵 `deploy/__pycache__` 单次同盘 rename 至 root-only 隔离目录，保留原 inode/
  SHA，不删除。随后 materialized 与 bootstrap 固定复验恢复全绿，current release 除
  venv 外 pycache/pyc 为 0；所有后续 Python 验收均强制 `PYTHONDONTWRITEBYTECODE=1`
  与 `-B/-I`。

---

## schema54 生产发布

日期：2026-08-13

状态：**已上线；固定升级回执 `succeeded/complete`，schema54、切换后备份和正式域名
只读验收全部通过**

### 授权、候选与不可变制品

- 用户最新明确要求“现在把完成的更新项目同步到线上”；D-035 仅取代 D-031 的本次
  “不部署”边界，360 人全部在岗、岗位独立专业化、历史身份隔离和 fail-closed 合同
  保持不变。
- release ID：`20260813T134506Z-schema54-r7`；切换提交时间
  `2026-08-13T13:55:28.694253+00:00`，升级完成时间
  `2026-08-13T13:55:50.122479+00:00`。
- 同一 RID 与 `SOURCE_DATE_EPOCH=1786628706` 在两个独立 `/private/tmp` 目录双构建；
  archive、receipt 和物化树字节一致。发布前干净副本全仓回归 `1456/1456 OK`，三套
  权威 V3 生成器 `--check`、360 人 auditor、Node、Python 编译和 diff 检查均通过。
- archive SHA-256：
  `22315e1bda58654133006c980dd9d2418d92fb53550dbeadadca57d55284ac04`；
  build receipt SHA-256：
  `d25eb7cd530bc32c559a0fc37d2a2b01a5666a59669d4a3566e4d4b17bc0bbb9`；
  manifest SHA-256：
  `caacae2f6c487353313874d53cfb18757d4999300d0e163c65a84bd40eed0b17`；
  payload tree SHA-256：
  `e1d30bdf8bd3d930d6f53691bdeaaf2e75532af1dbc4b6ba81f70eb62cb956ed`；
  source tree SHA-256：
  `1d171e69b3ceb2b0c1618174e89d3d94b078d32da34b89bec8d6ddee08c2a82e`。
- runtime 制品按正式 allowlist 收录 app、deploy、static、tests 及 V2/V3 immutable config
  seeds；`docs/team` 与目录生成/审计工具作为开发证据留在源码工作树，不冒充已进入
  runtime archive。

### 生产切换与恢复证据

- 发布前生产为 `20260811T064409Z-schema52-r7` / schema52；数据库 quick/integrity
  check、最新备份恢复演练、固定信任根、磁盘、服务、timers 和无执行中任务门禁通过。
  1 条 `awaiting_review` 只是一条等待人工拍板的终态业务记录，切换时原样保留。
- 候选通过固定 artifact、materialized、52-wheel 离线 wheelhouse、低权限 venv、
  bootstrap stage 和 control-plane 证明；只执行唯一固定入口
  `/usr/local/sbin/paihuo-upgrade`，没有手工改 `current` 或直接运行候选脚本。
- 唯一升级回执：
  `/var/lib/paihuo-upgrade/upgrade-20260813T134506Z-schema54-r7-20260813T135413-101506Z-96401792c81e.json`；
  owner/mode 为 `root:root 0600`，`status=succeeded`、`phase=complete`，
  post-commit proof `ok=true`、backup schema 54、backup integrity `ok`、HTTPS 200。
- 状态机在停服后生成并验证 final-stopped/rollback-ready，完成迁移和认证只读 smoke
  后才持久化 cutover commit。Caddy 在停服阶段一次优雅 SIGTERM 超时后被 fail-closed
  强制停止，随后正常恢复；发布完成后服务 `Result=success`、退出码 0、重启数 0，
  无持续告警。

### 上线后独立只读验收

- `current`、contentcrew 进程 cwd/exe 精确绑定新 release/venv；contentcrew、Caddy
  active/running，两个备份 timer enabled/active；8899 仅监听 `127.0.0.1`。
- 在线数据库 `PRAGMA user_version=54`、schema ledger 最大版本 54，quick/integrity
  均为 `ok`。`employee_slot`、`employee_role_config`、配置历史表均存在；20 个 task 与
  1 个 task thread 的 frozen identity/config 三元组完整，按 current+history 并集核验
  无孤儿引用。
- 切换后备份 `/var/backups/paihuo/db-2026-08-13T135541Z.db` 晚于 commit 创建，
  SHA-256 为
  `9e95d5bc413fb4b15ba174900f3b1bafde77684e5ac46e1a050b08512576cb68`；
  备份数据库同样为 schema `54/54`，quick/integrity 均为 `ok`。
- 本机 `127.0.0.1:8899`、`https://paihuo.ai` 与 `https://www.paihuo.ai` 的
  `/healthz` 均为 GET 200；固定状态机认证只读 smoke 已通过，验收未创建新的业务任务、
  模型调用或计费操作。
- 生产现役口径为核心 11 + 餐饮 60 + 非餐饮 current V3 360，总现役 431、行业
  current 420。historical role versions 为 V1 360 + V2 60；运行时
  current/historical/all-specialists/all-identities 为 `420/420/480/840`，后 420/480/840
  均不是额外员工人数，也不表示员工被降级。

### 观察窗口

- 本 release 的 24 小时观察窗口从完成时间重新计算：
  `2026-08-13T13:55:50.122479+00:00` 至
  `2026-08-14T13:55:50.122479+00:00`。上线已经完成；窗口届满前保持观察中，
  不提前写成观察完成。
