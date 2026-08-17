# 技术架构

## 总体边界

- 继续使用 FastAPI + SQLite WAL + 原子任务结算 + 单体静态前端。
- 新业务表全部 tenant-scoped；所有查询先锁定 tenant，再锁定行业。
- AI 视觉与文本调用继续走统一 providers 网关，私有员工上下文仅进入 system。
- 图片以租户隔离文件保存，数据库只存相对路径、摘要、类型与大小。

## 连续协作

- 新表 `task_thread` 只保存租户、员工、首版/当前版指针、轮次数与
  `active|satisfied` 状态，不复制业务正文。
- `task` 增加 `thread_id`、`revision_no`、`phase`、`request_key`；
  `source_task_id` 仍为直接上一版。
- 首次继续时惰性收养旧任务，不冒险全库回填历史链。下一轮从服务端
  读取同租户、同员工、已完成的当前版产出，不信任客户端伪造上下文。
- `request_key` 防双击/超时重放；事务 CAS 和部分唯一索引共同保证
  每个 thread 最多一条 `pending_charge|queued|running` 版本。

## 巡店领域

- `store_branch`：租户、行业、区域、门店名称/地址与启用状态。
- `inspection_visit` / `inspection_photo`：巡店任务、评分、摘要以及初检/复检照片
  的租户相对路径、SHA-256 与媒体元数据。
- `inspection_issue` / `inspection_evidence`：可见问题、严重度、置信度与当次照片证据。
- `inspection_action` / `inspection_recheck`：整改计划、负责人、期限、CAS 版本、
  复检建议与企业主人工通过/驳回。
- `inspection_event`：只记录结构化状态事件，支持断点恢复和审计。
- 图片先验魔数/像素/大小并重编码，再落租户目录、创建收费任务、提交后
  异步视觉分析；问题必须引用当次照片，模型无权自动关单。

## 老板看板

- 新增 `bossdashboard.py` 作为只读聚合层，不把 SQL 散在路由与前端。
- API：`/api/boss/dashboard/scopes`、`/summary`、`/employees/{idx}`。
- owner 的 tenant/industry 参数由服务端覆盖；只有命名 Boss 可选其他范围。
- 聚合复用任务中心的 header/status 口径；按员工/状态/日期 GROUP BY，员工元数据批量补齐。
- 巡店聚合独立按 store/region/severity/status 汇总；不读取照片或报告正文。

## 数据迁移与发布

- schema 50 → 51；迁移只加表、列与索引，不破坏旧任务和当前发布。
- 历史 task 不在迁移时强行追链；首次继续时由服务安全收养，异常/跨租户
  parent fail-closed。
- 旧 `industries_json` 只在 schema50→51 时把明确值迁移到 `tenant_industry`；
  运行期不以空列表恢复“全行业”旧语义。
- 按既有不可变 release、wheelhouse、备份与回滚状态机发布。

---

## schema52 架构增量

### 门店主数据与导入审计

- store_branch 增加 legacy-compatible nullable store_code 及省市区、店长、门店类型、
  开业日、面积、座位/房间/工位、经纬度、备注、版本；仅新导入行强制 store_code。
- partial unique (tenant_id, industry_key, store_code)；历史行不按名称猜编号。
- inspection_branch_import 保存作用域、request_key、source_hash、状态、计数、版本和
  稳定错误码；inspection_branch_import_row 保存规范化行、动作和错误码。
- 隔离 worker 只输出有界 JSON，不保留原 Excel；preview 无业务写，commit 在一个
  BEGIN IMMEDIATE 中复核版本与作用域并整体提交。

### 标准、槽位与观察值

- 内置标准库提供平台通用和 11 行业 overlay；企业覆盖存版本化模板记录。
- inspection_template / inspection_template_item 保存企业版本；
  inspection_visit 保存 template key/version/snapshot。
- inspection_photo.capture_slot 和 item_code 锁定拍摄区域；
  inspection_observation 保存 number/boolean/select/text/document/metric 结构化值。
- 视觉模型只接收照片与槽位标签；经营值、个人信息和原始表格不进入模型。

### 经营数据

- inspection_business_value 按 tenant/industry/store_code/metric/period 唯一，
  保存数值、单位、来源、导入批次和版本。
- 比较服务不填充缺失值，只返回 availability 和 reason_code。

### 性能、安全与恢复

- 门店查询强制 limit/cursor/search/region；导入同时限制文件、ZIP 展开、sheet、
  行、单元格和文本长度。
- xlsx 拒绝宏、外链、公式和异常 ZIP；临时文件 0600，子进程禁用户 site 与 pyc。
- request_key + source hash 防重放；preview/commit CAS 防双击与并发覆盖。
- PII 只在 tenant+industry 权限内使用，API 默认脱敏；导入审计不保存完整原文件。

---

## schema53 架构增量：行业决策员工 V2

### 不可变目录与双解析器

- `data/departments` / release `config/departments` 保留 V1 原始目录，作为餐饮 active
  与 10 个非餐饮行业 legacy 的不可变历史证据。
- 新增 `data/industry_decisions` / release `config/industry_decisions`，只承载 10 个
  非餐饮行业 active V2；发布包必须同时封装两层目录。
- `departments.list_depts()`、`specialists()` 和专家匹配只返回 active；
  `get_active(idx)` 用于新任务/新会议/进修；`get(idx)` 可解析 active + legacy，供历史
  展示、恢复和合法跟进。
- loader 对目录版本、来源、痛点覆盖、全局 idx/key/name 唯一、分组成员、合同字段和
  最小数量 fail-closed，并从结构化合同确定性生成兼容字段 `inputs/steps/deliverables/md`。

### schema53 身份快照

- `task` 新增 `employee_key`、`employee_catalog_version`、`employee_name_snapshot`、
  `employee_dept_key`、`employee_spec_sha256`。
- `task_thread` 新增 `employee_key`、`employee_catalog_version`；线程后续轮次必须与冻结
  身份一致。
- `meeting` 新增 `member_snapshot_json`；V2 行动幂等键包含 key + catalog_version，
  旧行动键继续兼容读取。
- schema52→53 从冻结 V1 目录回填现有任务和会议；无法唯一映射时保留明确未知状态，
  不回落到内容部。新写入必须同时保存 idx 与完整身份快照。
- `employee_spec_sha256` 是规范化员工合同的 SHA-256，用于检测同 key/version 的目录
  被意外改写；发布预检和运行时恢复均 fail-closed。

### 提示词与权限边界

- private system 注入员工身份、痛点、决策合同和证据规则；用户业务输入保持独立，
  外部检索不接收内部手册。
- 输出前验证状态、证据引用、缺失输入、审批边界和禁止动作；不能因模型文字承诺而获得
  新的采购、价格、账务、医疗、安全或监管写权限。
- 历史 V1 任务继续使用冻结 V1 手册；V2 目录不能成为旧任务重试的动态替代品。

### 看板与兼容口径

- 当前办公室显示 131 名 active；历史列表和任务下钻仍能显示 legacy，但明确标注
  “历史岗位/不可新派活”。
- 历史成本、成功率和产出按任务身份快照归因，不合并到主观指定的“继任员工”。
- 宣传页不再硬编码 431 或“每行业 36 人”；数量由构建期验证的 active 目录生成或采用
  不承诺人数的产品表述。

---

## schema54 架构增量：稳定员工与版本化岗位身份

本节取代 schema53 的 active=131 / legacy=360 运行口径，但保留 schema53 冻结五字段、
可信材料清单和严格决策门禁的安全成果。

### 目录与身份注册表

- V1 非餐饮 360 岗和 V2 20xxx 60 岗作为不可变历史岗位目录打包；V3 以原工号
  `1001–1936` 提供 360 个 current identity。不得覆盖旧 JSON 后重新计算旧 spec hash。
- `idx` 只表示稳定员工/工位号。岗位主键为完整 64 位
  `identity_ref = sha256(canonical_json(idx,key,catalog_version,name,dept_key,spec_sha256))`。
- 运行时分别维护 `active_by_idx`、`by_identity_ref`、`versions_by_idx`；不得用单个
  `{idx: employee}` 字典表示全部版本。

### 配置、任务和迁移

- 新增 `employee_slot(idx PK, active_identity_ref, enabled, row_version, updated_at)`；
  `enabled` 是唯一跨岗位版本共享的人员级运营状态。
- 新增 `employee_role_config(identity_ref PK, frozen identity fields, prompt_template,
  skills_json, settings_json, caps_off_json, model_text, model_image, config_revision,
  archived_at, ...)`；所有岗位方法和学习结果按身份版本隔离。
- task、task_thread、meeting 成员快照保存 full `identity_ref` 与 `config_revision`；执行、
  retry、followup、meeting、provider routing 全程传递该二元组，精确解析后不得再按 idx
  读取 prompt/skills/caps/model。
- schema53→54 在单事务中先归档 V1/V2 身份和旧配置，再建 V3 默认配置、回填任务/
  线程/会议引用并验证完整性，最后切换 active identity；未知或歧义映射导致整体回滚。
- 配置 API 保留 idx 路由时，body/header 必须带页面加载的 `active_identity_ref` 与
  `slot.row_version`，事务内 CAS 不一致返回 409，防止旧页面写入新岗位。

### 展示与计数

- API 分开返回 `person_status` 与 `identity_status`，并独立返回 `can_assign_new`、
  `can_continue`；historical identity 不能新派活或进修，但合法原线程可继续。
- active 行业员工严格 420、加核心为 431；历史岗位版本不计入员工人数，也不得被文案
  描述为降级员工。

---

## schema55 架构增量：V4 人设身份与版本化学习能力包

### 四代目录与身份解析

- 新增 `industry_decisions_v4` 作为 current 360；V3、V2、V1 均为不可变 historical
  identity。注册表按 identity_ref 保存全版本，同 idx 不得用字典覆盖。
- V4 身份新增 `person_snapshot` 与 `identity_scheme`；旧六字段摘要算法继续只用于 V1–V3，
  V4 使用显式包含人名快照的版本化算法，禁止重算旧 identity_ref。
- `employee_slot.active_identity_ref` 在单事务内由 V3 切到 V4并递增 row_version；enabled
  继续属于人员槽位，V3 的 prompt/skills/profile/model 不自动复制给 V4。

### Role bundle

- `employee_role_bundle_revision` 按 identity_ref + config_revision 保存基线档案、有效档案、
  decision contract、workflow、outputs、data objects、tools、capabilities、skills、escalation、
  learning tracks 与 bundle_sha256；历史 revision 只读。
- task、task_thread、meeting 成员快照增加 person_snapshot 和 bundle_sha256；执行链必须用
  identity_ref + config_revision + config_sha256 + bundle_sha256 精确解析，缺失或不一致时
  在模型调用和扣费前 fail-closed。

### 学习证据与审批状态机

- `employee_learning_batch` 管批量预算、幂等、进度与熔断；`employee_learning_run` 绑定
  员工身份、基准 revision/hash、成本和状态；`employee_learning_source` 保存真实抓取证据；
  `employee_learning_artifact` 保存有来源回链的知识/技能/能力/流程差异。
- 研究阶段使用完整岗位档案做私有训练上下文，外部搜索只接收净化后的岗位领域；来源必须
  来自启用 source capture 的 WebSearch 事件，不采信模型在正文中自行写出的 URL/来源名。
- run 从 queued → researching → awaiting_approval；证据通过后 worker 立即走同一套
  CAS 激活到 activated（D-040）。显式审批接口保留给未自动激活的遗留提案。stale、无证据
  或预算耗尽均不得部分激活。
- taskrunner、meeting、followup 读取任务冻结 bundle；未审批 artifact 不进入提示词，
  当前员工后续进修不改变旧线程人格和工作流。
