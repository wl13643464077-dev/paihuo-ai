# 派活 PaiHuo · 自主开发会话交接文档

> 交接对象:本地开发团队。本文覆盖 Claude 自主开发会话在分支
> `claude/business-project-code-review-cjag2y` 上的全部工作、必须遵守的
> 工程不变量、验收方法与遗留队列。

## 一、链接

| 项 | 地址 |
|---|---|
| 仓库 | https://github.com/wl13643464077-dev/paihuo-ai |
| 开发分支 | https://github.com/wl13643464077-dev/paihuo-ai/tree/claude/business-project-code-review-cjag2y |
| PR | https://github.com/wl13643464077-dev/paihuo-ai/pull/2 |

分支顶端(交接时):`dda28c4`。中途已与主支线的 schema48 系列
(`c113265` 合入 origin/main + 一批异步加固)rebase 合流一次,无冲突遗留。

## 二、本会话做了什么(按主题,含关键 commit)

**1. 审计 P0/P1 与基础设施**(早期批次)
- 8 项 P0、P1 安全批、异步 DB 边界重构(segfault 根因修复:dbio 线程
  永不代际切换,见 app/db.py 注释)、异地备份(deploy/offsite_backup.py,
  后被主支线 `16fded7` 暂缓 rollout)、观测层(app/obs.py + /api/ops/*)。
- 420+ 数字员工步骤修复;11 行业知识库(81 条带来源基准,
  data/industry_knowledge/,禁止裸精确数,合同测试锁死)。

**2. 老板视角 UX(三人设走查驱动,批A-D)**
- `c37de36` 批A:403 人话卡片、member 出路指引、全局 500 兜底、报错中文化
- `4b4d51b` 批B:台账分页、定时失败告警(fail_streak)、套餐临期红条+简报
- `e88be56` 批C:按月对账、四类租户级导出(/api/records/export.xlsx)、
  成片进发布包
- `155092d` 批D:扣费流水带单号、账单明细链接化(billReasonHtml)
- `fbd8b95` 任务中心真·服务端全局搜索(taskcenter 八臂 LIKE,计数同源)

**3. 协作可见(多人协作审计链)**
- `613b6df`/`81df609`/`808c41b`:job/task/avatar/meeting/tv/tool 六类全部
  落 created_by;station_run 落 reviewed_by(与状态 CAS 同事务,在 engine);
  详情与任务中心显示发起人/拍板人;member 代拍板(含 gate 放行)推
  member_reviewed 通知给老板(HTTP 层事务提交后发)。

**4. 通知定向**
- `af63002`/`dda28c4`:notification.user_id(空=广播)+ read_by(按人已读,
  逗号包裹串 instr 判含);财务类 kind(daily_digest/schedule_paused/
  schedule_failed/learn_*/member_reviewed)只发企业主,多 owner 各一份,
  无 enabled owner 时跳过站内绝不降级广播;广播单条已读互不吞未读;
  一键全读仅 admin。**新写测试注意:测试租户必须插入真实 owner 用户行,
  否则财务类通知不会落库。**

**5. 钱务诚实(多轮)**
- 「近30天花在哪」从 billing_log 流水按价目 label 归类(charge_if_claimed
  不写 billing_operation,从后者聚合会漏大头——已有合同测试锁死口径);
  「退回:」前缀冲抵。
- 退点不计充值:累计/月度/导出/明细四处「退回」单列。
- 进修 0 新技能退点、深审失败降级返回免费规则扫描、拍照工厂按腿退款。

**6. 回收站与合规**
- 六类软删(job/task/knowledge/avatar/profile/asset)+ 彻底删除
  (trash_purge,三阶段 marker 认领,连带 job 目录/tv 成片文件,
  billing 锚点永不动)。

**7. 其他**
- 昨日经营简报(scheduler._run_daily_digest,幂等标记先落后发,临期窗口
  [-3,7] 天);SSE 按板块过滤(engine.EVENT_MODULE);企业档案提炼可撤销
  (company_profile_prev 两版互换);promo/login 转化断链修复;
  新手引导(楼层默认展开/重看引导/点数标注)。

## 三、必须遵守的工程不变量(违反会被合同测试拦下)

1. **零回归纪律**:任何提交前跑全量
   `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
   把 `ERROR|FAIL` 行排序后与环境基线比对,必须逐条一致。
   本容器基线 74 条,全部是环境性失败(playwright/numpy 未装、
   paihuo-build 用户缺失、/tmp 非 owner 受控);你们本地基线可能不同,
   先跑一次干净 HEAD 建立自己的基线再开发。
2. **异步边界**:app 协程内禁止内联同步 db 调用,必须走
   db.arun/aq/aone/aget_setting/aset_setting/submit_write 门面
   (test_async_db_boundary 源码扫描强制)。dbio 线程永不做连接代际切换。
3. **保密口径**:运行时日志只记稳定上下文+error_type,禁止 .exception()
   打原始堆栈(test_error_log_confidentiality);对外错误文案不回显
   str(exc);providers.public_failure_message 是唯一失败文案出口。
4. **计费模式**:扣点用 charge_if_claimed(claim 与扣点同事务)或
   start_operation 家族;失败必退;扣费 note 带单号(工单#N/任务#N/
   会议#N/工具单#N),前端 billReasonHtml 依赖这些格式。
5. **租户隔离**:一切查询带 tenant_id=TEN();用户名解析等辅助查询同样
   强制租户过滤。
6. **前端**:单文件 static/app.js;动态文本一律 esc()/cp();删除类操作
   uiConfirm;提交前 `node --check static/app.js`。
7. **文案**:对老板说人话,涉及钱与不可逆操作必须如实披露
   (截断/上限/退点/保留策略)。

## 四、如何跑验收

```bash
# 后端全量(约 30-60s)
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
# 前端语法
node --check static/app.js
# 单套件示例
.venv/bin/python -m unittest tests.test_notification_targeting -v
```

关键合同测试地图:
- 钱:test_billing_spend_by_action / test_billing_operations /
  test_photo_factory / test_*_settlement
- 协作:test_collaboration_visibility / test_notification_targeting /
  test_notifications_read_permissions / test_sse_module_filter
- 回收站:test_recoverable_lifecycle / test_trash_purge
- 走查承诺:test_walkthrough_fixes / test_boss_ux_contracts /
  test_sweep_fixes
- 异步/保密:test_async_db_boundary / test_error_log_confidentiality /
  test_failure_confidentiality_api

## 五、遗留队列(建议优先级)

1. SSE 细粒度:task_update/meeting_update/employee_update 未按部门板块
   过滤(需按记录 dept_key 解析,engine.EVENT_MODULE 目前只做粗粒度)。
   影响仅为无关 member 页面多刷 /state,可见性本身 fail-closed 安全。
2. 审查记录(censor_log)与发布台账残留:job 彻底删除后 censor_log/
   publish_log/pub_task.payload 仍留标题/正文摘要;如要合规闭环需级联
   清理策略(注意 pub_task 有防重发语义,不能裸删)。
3. promo/login 移动端断点全面核查(已修过 720px 导航,未做全页走查)。
4. 游客反馈限频是进程内存态(_feedback_ips),多 worker/重启即清;
   如上多实例需换持久层。
5. 通知 read_by 无清理策略(行数≤30 查询无压力,长期可加归档)。
6. 在线支付未接(套餐开通仍走顾问/root 手工),转化链路的最后一环。

## 六、与主支线协作

- 主支线会向本分支推提交(已发生过一次 schema48 合流)。推送被拒时:
  `git fetch` → `git rebase origin/claude/business-project-code-review-cjag2y`
  → 全量测试重比基线 → push。不要 force,不要丢弃对方提交。
- schema 迁移全部走 db.py 的 `_add_column` 幂等模式,新增列同时更新
  `_validate_migrated_database` 契约(主支线已把 created_by/reviewed_by/
  fail_streak 纳入 schema48)。
