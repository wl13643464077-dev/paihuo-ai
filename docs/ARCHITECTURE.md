# 派活 PaiHuo 后端架构说明

> 第 3 期整理。目的是让后来的人知道：代码现在是怎么分层的、新代码该放哪、
> main.py 还剩哪些没拆、以及七套"后台任务"以后怎么收成一套。
> 本文只描述现状和方案，本期只做了第 1 节里"已拆分"的那部分机械搬家，业务行为没有任何改动。

## 1. 现状分层

```
浏览器单页 static/app.js ── HTTP/SSE ──┐
                                        ▼
┌──────────────────────── app/main.py（FastAPI 应用，约 1.43 万行）────────────────────────┐
│ 全局：app 实例、异常翻译、_metrics_mw / _auth_mw 中间件、上传白名单表                      │
│       (_PERSISTENT_UPLOAD_ROUTES / _TRANSIENT_UPLOAD_ROUTES / _BOUNDED_UPLOAD_PATTERNS)、  │
│       启动段 _startup(恢复中断任务、拉起调度/看门狗/清理循环)                              │
│ 仍在 main 里的路由域：登录/会话、设置中心、平台后台、内容工单、专家任务、任务中心、        │
│       数字员工与学习、知识库、回收站、企业档案、老板看板、定时任务、文件解析、圆桌会议、   │
│       访客体验、交付包、人设/资产、SSE、公众号/审查官/图库、成片、发布台账、企微通知、矩阵发布 │
│ 在原位置 app.include_router(...) 挂载 ↓                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────┘
        │ include_router（保持注册顺序）             │ 重新导出（main.<名字> 仍可用）
        ▼                                              ▼
app/routes/  HTTP 层（按业务域）          app/web_common.py  共享 Web 辅助
  billing.py     套餐与支付                 TEN / _need_admin / _need_root / _need_module / _is_boss
  team.py        团队权限                   分页 _pagination / _page_result
  inspection.py  巡店(+巡店分析作业)        _run_db_safely / _run_db_then_start_worker_safely
  avatar.py      数字人                     计费启动 _start_billed_operation / _start_billing_operation_safely
  tools.py       工具箱(+工具作业/看门狗)   持久上传闸门 / 免费 AI 限流 / _read_limited
app/api_staff.py、app/api_checklist.py      公开视图裁剪 _public_station / _steps_for_view …
  （第 2 期新写的路由模块，同一写法）       _create_charged_expert_task
        │                                              │
        ▼                                              ▼
服务/领域模块（可单测、不依赖 FastAPI）：billing、purchases、wxpay、inspection*、avatar、
  textvideo、meeting、taskrunner、engine、matrixpub、growth、stafftask、checklist、employees …
        │
        ▼
app/db.py（SQLite + 线程池门面 db.arun / db.atomic / submit_write）、auth.py（请求上下文里的当前用户）
```

依赖方向只允许自上而下：`main.py → routes/* → web_common → 服务模块 → db/auth`。
`routes/*` 和 `web_common` 都**不得** `import main`；`web_common` 也不得 import `routes/*`。
路由模块之间允许少量单向引用（目前只有 `routes/team.py` 引用 `routes/inspection.py` 的
`_raise_inspection_error`），不允许形成环。

## 2. 目录约定

| 位置 | 放什么 | 不放什么 |
|---|---|---|
| `app/routes/<域>.py` | 该域的 HTTP 路由（`router = APIRouter()`）、请求参数解析、把业务异常翻成 HTTP 状态码、该域专属的后台作业入口（worker/恢复/看门狗） | 跨域共用的辅助；中间件；上传白名单表 |
| 服务模块 `app/<域>.py` | 业务规则、SQL、状态机、计费结算；纯函数优先，方便真实 SQLite 单测 | `HTTPException`、`Request` 等 Web 概念（旧代码里有的先不动） |
| `app/web_common.py` | 被 main 与两个及以上路由模块共用的 Web 层辅助（当前用户、权限、分页、DB 线程包装、上传/限流闸门） | 只被一个域用的东西（跟着那个域走） |
| `app/main.py` | FastAPI 实例、中间件、异常处理、上传白名单表、启动/关闭段、`include_router` 挂载点、尚未拆分的路由 | 新业务路由（新功能请直接写成 `routes/` 模块或 `api_*.py`） |

写新路由模块的要点（照 `app/api_staff.py`、`app/api_checklist.py`、`app/routes/*.py` 的写法）：

1. 当前用户用 `auth.current()` / `auth.tenant_id()`（或 `web_common.TEN()`），不要从 main 拿。
2. 日志沿用 `logging.getLogger("main")` 或模块自己的名字；拆分搬家的模块统一用 `"main"`，保证线上日志检索口径不变。
3. 需要上传的路由，白名单仍登记在 main.py 的 `_PERSISTENT_UPLOAD_ROUTES` / `_TRANSIENT_UPLOAD_ROUTES`，
   或像 `api_staff.upload_policy` 那样提供按正则匹配的策略函数。
4. 在 main.py 里挂载：`app.include_router(routes.xxx.router)`。**挂载位置决定注册顺序**：
   `/api/x/{id}` 与 `/api/x/export` 这类会互相匹配的路由，必须保持"具体路径在前"。

### 继续拆分时的操作守则（本期就是这样做的）

- 整段搬家，函数体不改；函数体内的相对导入 `from . import x` 要改成 `from .. import x`（包层级多了一层）。
- 被搬走的每个名字在 main.py 重新导出：`from .routes.xxx import a, b  # noqa: E402,F401`。
  例外：被 `global` 重新绑定的模块变量（如 `_TOOL_WATCHDOG_TASK`）不导出，导出只会得到过期快照。
- 块外的辅助：只被该域用的跟着搬；被多处用的搬到 `web_common.py`，main 从那里导回原名。
- 测试里 `patch.object(main, "helper")`：只对 main 里剩下的代码生效。被测函数若已搬到
  `routes/xxx.py`，要改成 `patch.object(app.routes.xxx, "helper")`（名字在哪个模块里被**查找**就 patch 哪里）。
  `main.ROOT = ...` 这类直接赋值同理。
- 读源码文本/AST 的测试（`test_async_db_boundary`、`test_async_p1_route_boundaries` 等）要把
  `app/routes/*.py`、`app/web_common.py` 一起纳入扫描，不能因为搬家而漏检。
- 验收：拆分前后各导出一次路由表，(method, path) 多重集合一致、注册全序一致；
  `ruff check --select F821,F811,F823 app` 与 pyright 的未定义名/属性/调用类问题不新增；全量测试结果与拆分前一致。

## 3. 第 3 期已拆分的域

| 域 | 文件 | 路由数 | 说明 |
|---|---|---|---|
| 套餐与支付 | `app/routes/billing.py` | 13 | `/api/billing`、`/api/purchases*`、`/api/pay/wxpay/*`、`/api/admin/purchases*`、`/api/admin/wxpay/config`、`/api/admin/pay-orders` |
| 巡店 | `app/routes/inspection.py` | 23 | `/api/inspections/*` 全部 + 巡店分析作业（`_run_inspection_task`、`_resume_inspection_tasks`、`_backfill_inspection_scores` 等，启动段仍由 main 调用） |
| 团队权限 | `app/routes/team.py` | 13 | `/api/team`、`/api/team/users*`、`/api/team/tenants*`（不含下面"未拆"里的 3 条零散路由）、`/api/team/applies*`、`/api/team/apply-config` |
| 数字人 | `app/routes/avatar.py` | 12 | `/api/avatar/*`（`/api/avatar/consents` 属于第 3 期合规段，仍在 main） |
| 工具箱 | `app/routes/tools.py` | 18 | `/api/tools/*` + 工具作业 worker/看门狗（`_recover_interrupted_tool_jobs`、`_ensure_tool_running_index`、`_start_tool_watchdog` 仍由 main 启动段调用） |
| 共享 | `app/web_common.py` | 0 | 54 个共享辅助，main 原名导回 |

main.py：拆分前 19979 行 → 拆分后 14269 行。

## 4. 还没拆的部分与建议顺序

按"收益 ÷ 风险"排序。行数为拆分后 main.py 里的大致规模。

1. **数字员工与学习**（`/api/employees/*`、`/api/employee-learning/*`，约 3900 行）——main 里最大的一块，
   但测试大量 `patch.object(main, "_employee_public_contract" / "_LEARNING_BATCH_COORDINATORS" …)`，
   拆时要先把学习批次协调器（模块级可变状态）整理进服务模块 `employeelearning.py`，再搬路由。
2. **回收站**（`/api/trash/*`，约 1100 行）——块外依赖很少（依赖分析显示只需要几行共享辅助），
   但 `trash_purge` / `_PURGED_CONTENT` / `_purge_local_files` 被测试直接引用和 patch，需要同步改测试。
3. **公众号草稿分发 + 审查官 + 图库 + 发布渠道**（V24 段，约 1400 行）——内聚度高；
   `_recover_wechat_deliveries` 在启动段调用，按工具箱的方式保持调用点不变即可。
4. **圆桌会议**（`/api/meetings/*`，约 600 行）——依赖员工公开视图辅助，最好排在第 1 项之后。
5. **成片 / 素材库 / 发布台账 / 矩阵发布 / 企微通知**（V25 除工具箱外的部分，约 1000 行）。
6. **内容工单 + 交付包 + 专家任务 + 任务中心**（`/api/jobs/*`、`/api/tasks/*`、`/api/task-center/*`）——
   与引擎、计费、七套任务系统耦合最深，建议放到第 5 节的任务生命周期统一之后再拆。
7. **平台后台**（`/api/admin/*`）——目前散落在 main 的 6 处（短信、模型通道、概览/漏斗/员工、
   功能开关、公众号告警等），与各业务域交叉；建议各域拆分时把自己的 admin 路由一起带走，最后剩下的再归到 `routes/admin.py`。
8. 零散的团队/套餐路由：`/api/team/tenants/{tid}/grant`、`/api/team/support-contact`、
   `/api/team/tenants/{tid}/subscribe`（夹在飞书/客服路由中间，为保持注册顺序本期没动），
   以及登录/会话、设置中心、访客体验、SSE、静态页。登录与中间件强相关，建议最后处理。

## 5. 七套任务系统 → 一套通用任务生命周期（目标设计，本期未实施）

### 5.1 现状

| 系统 | 表 | 执行者 / 进程内登记 | 重启恢复 | 超时收口 | 计费 |
|---|---|---|---|---|---|
| 内容工单 job | `job` + `station_run` | `engine`（队列 + `engine.locks`） | `engine.start()` | `watchdog.stale_jobs` | `billing_status` + `billing_operation` |
| 专家任务 task（含巡店分析） | `task` | `taskrunner`（`RUNNING`） | `taskrunner.resume_pending` / `_resume_inspection_tasks` | `watchdog.stale_tasks` | 同上 |
| 工具作业 tool_job | `tool_job` | `routes/tools.py`（`_TOOL_TASKS`） | `_recover_interrupted_tool_jobs` | 自带 `_tool_watchdog_loop` | 同上 |
| 图文成片 tv_job | `tv_job` | `textvideo.run_job` | `textvideo.resume_pending` | 无统一看门狗 | 同上 |
| 数字人 avatar_job | `avatar_job` | `avatar.run_job` | `avatar.resume_pending` | 无统一看门狗 | 同上 |
| 矩阵发布 pub_task | `pub_task` | `matrixpub` | `matrixpub.resume_pending`（中断标失败） | 无 | 不计费 |
| 圆桌会议 meeting | `meeting` | `meeting._run`（`ACTIVE`） | `meeting.resume_pending` / `recover_interventions` | `watchdog.stale_meetings` | 同上 |

七套系统各自实现了同一组动作：落库(常见 `pending_charge`) → 原子扣点 → 排队 → 执行并写进度 →
成功/失败结算(失败幂等退点) → 重启恢复 → 超时收口 → 任务中心展示与重试（`taskcenter.retry_meta`、
`/api/task-center/{kind}/{rid}/retry` 里按 kind 分支）。状态值、进度字段（`steps_json` / `progress` / `log`）、
软删除字段、重试计数的写法各不相同，看门狗只覆盖了其中三套。

### 5.2 目标：一张任务头表 + 各业务明细表

```
work_item（新表，所有后台任务共用的"任务头"）
  id, tenant_id, kind(content/expert/inspection/tool/video/avatar/publish/meeting),
  ref_id(指向原业务表主键), status, phase, progress_json, error_public, error_type,
  billing_op_key, billing_status, billing_points, attempt, max_attempts,
  lease_owner, lease_until, heartbeat_at, deadline_at,
  created_by, created_at, updated_at, terminal_at, deleted_at
```

统一状态机（所有 kind 共用，业务细分放 `phase`）：

```
pending_charge ─扣点成功─► queued ─被领取─► running ─┬─► done
      │                      ▲                        ├─► failed ──(可重试)──► queued（attempt+1，不重复扣点）
      └─扣点失败/放弃─► cancelled                     ├─► waiting_input（如巡店补拍、会议待追问）
                                                       └─► cancelled（用户取消，退点）
```

通用运行时 `app/workitems.py`（新服务模块）提供：

- `create(kind, ref_id, *, op_key, points)`：与业务明细在同一事务内写入任务头并扣点；
- `claim(kind)` / `heartbeat(id)` / `finish(id, result)` / `fail(id, error, refund=True)`：
  用租约（`lease_owner` + `lease_until`）取代各模块的进程内集合（`RUNNING`、`ACTIVE`、`_TOOL_TASKS`、`engine.locks`），
  结算走 CAS 条件更新，失败退点统一调用 `billing.fail_operation`（幂等）；
- `recover_on_startup()`：租约过期的 running 按 kind 的策略"重新排队"或"失败退点"，取代 7 个 `resume_pending`；
- 一个看门狗循环：按 `deadline_at` / `heartbeat_at` 收口，取代 `watchdog.py` 的三类扫描和工具箱自带看门狗；
- 执行器注册表：`register(kind, run=..., on_fail=..., resume_policy=..., public_view=...)`，
  业务模块只负责"怎么干活"和"怎么展示"。

任务中心 `taskcenter.list_items` 改为查 `work_item` 一张表（按需 join 明细取标题），
`/api/task-center/{kind}/{rid}/retry` 变成通用的 `workitems.retry(id)`。

### 5.3 分步迁移（每一步都可单独上线、可回滚）

1. **只加不改**：新建 `work_item` 表和 `workitems.py`（纯函数 + 真实 SQLite 单测），不接任何业务。
2. **影子写入**：七套系统在现有状态变化处同步写任务头（同一事务），读路径不变；
   加一个对账脚本比对任务头与各业务表状态，跑一段时间确认一致。
3. **任务中心切读**：`taskcenter` 改读 `work_item`，保留旧查询做灰度对比开关。
4. **逐个 kind 切执行**：按风险从低到高——矩阵发布 → 工具作业 → 图文成片 → 数字人 → 会议 → 专家任务/巡店 → 内容工单。
   每切一个：该 kind 的 `resume_pending` / 看门狗分支改为调用通用运行时，旧代码保留一个版本后删除。
5. **统一看门狗与重启恢复**：全部 kind 切完后，删掉 `watchdog.py` 中按表扫描的分支和工具箱自带看门狗，
   启动段只剩 `workitems.recover_on_startup()` 与一个循环。
6. **收尾**：业务表里重复的生命周期字段（`status`/`billing_status`/`retry_count`/软删除）改为只读兼容视图，
   经过一个数据保留周期后再清理（需要单独的 schema 版本升级，不和上面的步骤混在一起）。

风险与约束：单进程（实例锁）前提下租约主要用于崩溃恢复，不要引入多 worker；
所有迁移步骤都不能改变"失败必退点、退点幂等"的现有保证，每一步都要有真实 SQLite 的结算测试。
