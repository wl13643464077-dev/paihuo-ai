# 派活AI：巡店、连续协作与行业老板看板升级

更新时间：2026-08-12
通道：完整
当前阶段：schema53 行业决策员工 V2 实现完成，QA 与独立终审进行中
在岗角色：总指挥、产品经理、架构师、交互设计师、工程师、测试负责人、安全评审、
发布负责人
模式：自动

## 阶段账本

| 阶段 | 状态 | 产物 | 证据/剩余项 |
|---|---|---|---|
| 定向 | 已过关口 | `00-brief.md` | 三条业务闭环与不做项已锁定 |
| 压力测试 | 已过关口 | `01-pressure-test.md` | 照片证据、版本分叉、权限越界和假经营数据风险已确认 |
| 验证 | 已过关口 | `02-validation.md` | 早期用户场景和官方看板/巡店参考已取证 |
| PRD | 已过关口 | `03-prd.md` | 主流程、权限、状态、计费和移动端验收已冻结 |
| 决策/设计 | 已过关口 | `04-decisions.md`、`05-design.md` | D-009～D-012 与证据优先交互已锁定 |
| 架构 | 已过关口 | `06-architecture.md` | schema51、任务线程、巡店领域和只读看板边界已落地 |
| 执行 | 已完成 | `08-implementation.md`、`app/`、`static/` | 三大功能、schema51 与横向安全/并发修复已实现 |
| QA | 已通过 | `09-qa.md` | 最终全量 1199/1199，静态、依赖与补丁检查全绿 |
| 独立终审 | 已通过 | `10-review.md` | PASS，最新候选树无剩余确定性 P0/P1 |
| 不可变发布 | 已完成 | `11-release.md` | schema52 RID `20260811T064409Z-schema52-r7`，唯一回执 succeeded/complete |
| 复盘 | 已完成 | `12-retro.md` | 视觉能力、运行时合同与真实业务验收规则已固化 |

## 当前已证明

- schema51 本地迁移合同完整；新表、字段、唯一/部分唯一索引、迁移账本和
  `PRAGMA user_version=51` 均通过回归。
- 员工总数为 431：11 名核心员工 + 420 名行业员工；新增巡店经理
  使用照片证据、整改和复查专用流。
- 430 名文本直接派活员工可保留版本持续第 2/3/4 轮；巡店经理通过
  可审计 CAPA 与复查链继续，两类都可持续到老板满意。
- 行业老板看板已按 tenant + explicit industry 实施，仅展示平台能证明的任务、
  效率、风险和巡店指标，未接 POS/ERP 经营数据不生成假数值。
- 用户/租户停用会话、并发会话撤销、任务/图片跨行业、巡店幂等/CAS、
  看板口径和跨租户链接等终审发现均已修复并回归通过。
- 最终全量 `1199/1199` 与 compileall、Node 语法、shell `bash -n`、
  `pip check`、`git diff --check` 全绿；独立安全终审 PASS。
- Chrome 端到端已实际点通 431 人员口径、巡店经理专用入口、门店/
  照片/问题/整改/汇总、第 1～4 轮与满意收口、行业老板看板；
  浏览器控制台 warning/error 为 0。

## schema51 发布基线（schema52 切换前）

- 当前不可变版本：`20260810T033419Z-schema51-r7-hotfix9`；数据库
  `PRAGMA user_version=51`，升级回执 `succeeded/complete`。
- 巡店旧失败单只做 1 次免费重试并复用原照片：完成逐图审查，识别 4 个问题，
  形成 4 条证据和 4 个整改动作，计费 0 点，活动与待结算均归零。
- 撰稿人真实任务完成第 1 轮与第 2 轮，线程最终为 `satisfied`，第二轮被验收，
  两轮计费 0 点，未创建重复任务。
- contentcrew、Caddy、备份定时器、正式域名 HTTPS、固定 artifact / materialized /
  wheelhouse / bootstrap 证据链均通过独立只读验收。

## 后续观察

1. 观察巡店合同重做率、零问题异模复核率和人工驳回率，只记录聚合码与数量。
2. 新增视觉模型前先登记 `supports_vision` 并通过真实像素 canary；文本模型不得
   静默承接视觉任务。
3. 每次发布继续保留真实业务 smoke；健康 200、HTTP 成功或 JSON 可解析均不能
   替代业务证据链验收。

## 禁止事项

- 不输出或提交用户口令、Key、Cookie、环境变量、业务正文、巡店照片
  或供应商原始响应。
- 不绕过 tenant/industry 权限，不让 AI 无证据写发现或自动关闭整改项。
- 在发布回执、生产健康和真实业务冒烟前，不声明 schema51 已上线。

---

## schema52：全国门店主数据与行业巡店标准深化

更新时间：2026-08-10
通道：完整
当前阶段：功能与留存 v2 已实现，进入最终独审和发布门禁
生产状态：已切换至 `20260811T064409Z-schema52-r7`；schema52 与正式域名只读验收通过

### 本轮新增目标

1. 企业可下载规范 Excel，一次导入数百至数万门店；上传后先预览、逐行校验，再按
   门店编号原子提交，绝不按店名静默覆盖。
2. 巡店从“任意上传 1～8 张照片”升级为“按行业标准逐项采集”：门头、收银、
   环境、后场/后厨、仓储/设备、人员/服务等拍摄槽位有框选建议和必填规则。
3. 销售、客流、员工、库存/损耗等作为结构化经营数据导入与同店环比/同比依据；
   没有可信来源时显示待接入，不让模型制造数值。
4. 首批完整覆盖现有 11 个行业，标准分为强制合规、推荐标准、企业运营三层，
   每条保留来源、适用范围、生效时间和版本；巡店记录冻结当次标准快照。

### schema52 阶段账本

| 阶段 | 状态 | 证据/产物 |
|---|---|---|
| 权威研究 | 已完成 | 国家市监、卫健、交通、体育、农业农村、应急/消防等官方来源 |
| 产品/架构 | 已冻结 | 03-prd.md 至 07-plan.md 的 schema52 增量合同 |
| Excel 模板 | 已实现 | 四工作表：门店主表、经营数据、填写说明、示例；受限下载与解析 |
| 后端 TDD | 已实现 | schema52、结构化 XLSX、preview/commit、标准快照、override 与经营数据 |
| 前端 TDD | 已实现 | 巡店工作台顶部导入向导、门店搜索、7 槽位、经营指标与对比 |
| 留存加固 | 已完成 | 24h startup+periodic sweep、secure-delete + WAL truncate、内容寻址 zlib 审计归档 |
| 独立评审 | 已通过 | 原始 P1、备份/WAL、归档篡改、回滚、满量并发复验均通过，无确定 P0/P1 |
| 不可变发布 | 已完成 | 双构建一致、固定四证据链、升级回执与正式域名验收全部通过 |

### schema52 当前已证明

- 批量导入支持 20,000 门店 + 40,000 经营数据行；preview 无业务副作用，commit
  原子、幂等并使用 batch CAS。暂存无手机号/员工/来源/备注/数值明文，跨租户、
  跨行业、跨请求、跨行或密钥变化均 fail-closed。
- 11 行业标准目录、企业→区域→门店覆盖、强制项保护、巡店快照、每行业 7 个采集
  槽位、结构化经营指标和前端导入主路径均已进入候选树与合同测试。
- v1 独立审计发现两个 P1：24h 清理只在租户请求时触发；committed staging 永久
  保留 60,000 行并导致重复满量导入线性增长。两项均阻断了直接发布。
- v2 已增加启动和周期性的跨租户有界 sweep、partial retention index，以及包含
  完整 action + 脱敏数据的 SHA-256 内容寻址 zlib 归档；验证归档后在同一事务删除
  working rows，权威门店、经营数据和巡店记录不动。
- 最终 WAL 修复后的不重复分组证据为 `85/85` 导入/留存/迁移/升级、
  `50/50` schema52 集成，以及 `178/178` async DB + 发布链；合计 `313/313`。
  Node 语法、Python 编译和 tracked/untracked whitespace 检查全绿。

### schema52 发布结果

1. 最终 release ID 为 `20260811T064409Z-schema52-r7`；两次不可变构建字节一致，
   artifact/materialized/wheelhouse/bootstrap 四条信任链验证通过。
2. 固定升级回执唯一且 `succeeded/complete`；current/cwd/exe、schema52 ledger、服务、
   备份、正式 HTTPS、模板下载、导入入口、门店搜索与检查清单均通过。

### schema52 生产现场

- 当前生产精确为 `20260811T064409Z-schema52-r7` 与 schema52；服务、Caddy、备份、
  数据库 quick check、正式 HTTPS、磁盘、升级回执、待结算和固定信任根均通过最终
  只读 postdeploy 验收。
- 历史 3 条零点 avatar failed+charged 属于已知平台语义债；1 条
  `inspection_action.in_progress` 是人工整改状态，关联执行已终态且无待结算。它们不
  需要、也不允许为本次发布篡改业务数据。
- 最终制品哈希、升级回执、模板与正式域名聚合证据已写入 `11-release.md`；验收未创建
  生产导入、任务或模型调用。

---

## schema53：行业决策员工 V2

更新时间：2026-08-12
通道：完整
生产状态：schema52 保持在线；schema53 尚未构建、未授权、未部署

### 当前结论

- 本地审计证明现有 10 个非餐饮行业 360 名员工中，35 个岗位名被 10 行业共同复用；
  这是目录结构趋同，不是单纯提示词问题。
- 十行业权威研究已完成，已锁定每行业 6 个专属决策员工；餐饮 60 人保持不动。
- 10 份行业 JSON 已落地 60 个痛点、64 条来源、60 名专属员工和 180 项结构化指标。
- 新 active 口径为 131：11 名核心员工 + 60 名餐饮员工 + 60 名非餐饮 V2 员工。
  历史文档中的 431/420 是 schema52 发布事实，不篡改；schema53 上线后不再用于当前宣传。
- 旧 360 名模板员工进入 legacy，只能解释和完成既有任务/线程/会议；新任务、会议、进修、
  专家推荐均只允许 active。
- 为避免历史串账，本轮版本提升为 schema53：task/thread/meeting 在数字 `idx` 之外冻结
  key、目录版本、名称、部门和规格 SHA-256 五字段身份；同 `idx` 的不完整、不一致或
  跨目录快照一律 fail-closed，legacy 仅可只读解释并完成既有协作链。
- public task guide 与 private employee contract 已隔离；V2 通过固定
  `evidence_requirements` 收集材料，由服务端生成 `user_submitted_unverified` manifest，
  单项正文最多 4,000 字符、全部正文与来源名合计最多 20,000 字符。
- create、followup、完整 manifest 幂等比较和用户/系统提示隔离已接通；严格决策门禁在
  缺项、未知引用、结构不完整或冲突时输出 HOLD。GO 仅表示可进入人工审批，不代表材料
  真实性已核验，也不允许自动外部副作用；餐饮及其他 V1 任务合同保持不变。
- 行业 JSON 已映射进不可变 `config/industry_decisions`；发布前置检查重验 manifest
  结构、目录树、SHA-256、磁盘内容和签名 payload，一致性失败即拒绝发布。

### 阶段账本

| 阶段 | 状态 | 证据/产物 |
|---|---|---|
| 趋同审计 | 已完成 | 360 人、35 个共同岗位名、idx 引用范围与风险清单 |
| 十行业研究 | 已完成 | 官方法规/标准、协会/上市公司事实、痛点/决策/数据/员工映射 |
| 产品/架构 | 已冻结 | 03-prd、D-021～D-025、schema53 双目录与身份快照 |
| 决策目录 TDD | 已实现 | 10 JSON × 6 员工、严格合同与发布校验 |
| 运行时/迁移 | 已实现 | active/legacy resolver、schema53、冻结身份、证据链与任务/会议/看板接线 |
| QA/独立终审 | 进行中 | 历史不串账、权限/计费/prompt/发布/前端全矩阵正在终验 |
| 不可变发布 | 未授权 | 必须使用全新 RID，并再次取得用户明确生产授权 |

### 发布约束

- `20260811T082804Z-schema52-r7-hotfix` 只属于先前 UI 工作且未部署，现已失去候选资格；
  不得把旧制品与行业目录 V2 混发。
- schema53 只有在全量测试、独立 P0/P1 终审、双构建和固定证据链全部通过后才可请求
  生产授权；研究完成或本地页面可见均不代表上线。

---

## schema54：360 名行业员工全部在岗纠偏

更新时间：2026-08-12
通道：完整
当前阶段：十行业研究与 360 岗产品设计完成，V3 目录及版本化身份实现中
生产状态：生产继续运行 schema52；schema53 未部署；schema54 明确禁止部署

### 当前权威口径

- schema53 的“非餐饮每行业 6 名、旧 360 名 legacy、active=131”已被用户明确否决，
  不再作为当前产品口径；其代码只作为未部署候选基础继续演进。
- 原非餐饮 360 名员工全部保持在岗，按原工号重建为 360 个行业痛点专属 V3 岗位；
  餐饮 60 与核心 11 保持不动，最终 active=431、行业 active=420。
- 旧 schema52 岗位和 schema53 20xxx 岗位只作为旧任务的 historical identity version，
  不计人数、不称降级员工；同一员工的 current V3 identity 可接新活。
- 十行业联网研究和 360 岗逐槽设计已完成；零售四行业、酒店/汽车/健身均完成四维
  痛点评分，药房/美业/宠物完成监管与行业资料补强。目录实现必须接受语义去重独审。
- 用户已授权修改 `/Users/wanglei/Documents/派活AI-R7`，同时明确禁止部署；本轮不得
  创建生产候选、上传制品、切换服务或声称已上线。

### schema54 阶段账本

| 阶段 | 状态 | 证据/产物 |
|---|---|---|
| 需求纠偏 | 已完成 | 360 全员在岗、原工号、431 总现役口径 |
| 联网研究 | 已完成 | 十行业权威来源、痛点评分与高价值岗位排序 |
| 360 岗设计 | 已完成 | 10×36、连续号段、8组占位、岗位名全局唯一 |
| 产品/架构 | 已冻结 | D-026～D-031、schema54 身份/配置/历史连续性合同 |
| V3 目录 TDD | 进行中 | 360 决策合同、来源、指标与语义去重 |
| 迁移/运行时 | 进行中 | identity_ref、slot/role-config、task/thread/meeting revision 已进入聚焦回归 |
| 前端/文案 | 进行中 | current/historical 两轴、岗位档案和“不降级”展示 RED 用例已建立 |
| QA/独审 | 待开始 | 同 idx 串岗、迁移回滚、计费、浏览器与 360 语义复核 |
| 部署 | 禁止 | 用户明确要求“不部署” |

### 最新验收纠偏

- V3 必须重建每个员工的岗位档案、行业知识、数据对象、技能树、能力、专属流程、
  只读工具权限、升级矩阵和学习路径；“换名字+换提示词”不属于完成。
- 旧的模板目录已在新 TDD 门禁下确认 RED：缺失 professional profile、仍只有 6 个伞形痛点、
  缺少真实价值排名，且流程相似度超限。不允许降低门禁使其通过。
- 茶咖 36 人的真实数据工具、技能树和能力样板已达到岗位级颗粒度，但首轮合同暴露了通用首尾步骤、
  通用治理指标、三段式学习路径和空泛升级条件；已退回重写并新增运行时/测试/预检三层 RED 门禁。
- schema54 身份与岗位配置已实现 full identity ref、slot/role config、config revision/hash，任务、线程和会议按冻结
  岗位档案精确路由；全量回归等待 360 份 V3 professional profile 全部落盘后执行。

---

## schema54 最新状态：360 份 V3 岗位已落盘

更新时间：2026-08-13
通道：完整
生产状态：生产继续运行既有 schema52；schema54 未构建正式 release、未授权、未部署

### 当前唯一有效的现役口径

- 非餐饮原 360 名员工全部是 current/在岗员工，十行业各 36 名并沿用原 `idx`；餐饮
  60 名与核心 11 名保持在岗。行业 active=420，总现役=431。
- V1 同 `idx` 360 份和 V2 `20xxx` 60 份只是 historical role versions，用于精确复现
  旧任务、线程和会议；它们不是“历史员工”“降级员工”或额外员工人数。
- 360 份 current 岗位均以真实高价值、高频行业痛点为中心，完整包含专业档案、行业
  知识、数据对象、inputs、workflow、outputs、只读 tools、skills、capabilities、
  escalation、learning、3 个行业原生 KPI 和四维优先级，不以换名字/提示词冒充专属化。

### 最新实现与证据

| 项目 | 当前状态 | 当前证据 |
|---|---|---|
| V3 目录 | 已落盘 | 10×36=360，原 idx；auditor `errors=0` |
| 岗位 KPI | 已通过结构门禁 | 360×3=1,080；key/name/formula 三字段分别全局唯一 |
| 来源语义 | 已完成 | 十行业×8=80 痛点簇逐簇核对 source→pain→role，无阻断项 |
| 运行时花名册/版本 | 已接通 | current/historical/all-specialists/all-identities = `420/420/480/840` |
| 身份与配置 | 已接通 | slot/role-config、full identity、revision/hash、CAS |
| 连续执行 | 已接通 | task/thread/meeting 精确冻结 identity + config revision |
| 生成器权威 | 已收敛 | A、hotel+fitness、auto+beauty+pet；旧 B 退役且不可写 |
| 全仓回归 | 已通过 | 干净副本、Python 3.12、完整 requirements、真实 Chromium；`1456/1456 OK` |
| schema54 专项 | 已通过 | `219/219`，包含在全仓或与其重叠，不与 1,456 相加 |
| 最终独审 | 已通过 | 目录/迁移/身份/配置/浏览器复验；`P0=0 / P1=0 / P2=0` |
| 正式构建与部署 | 禁止 | 未构建正式 release、未授权、未部署 |

### 不可越过的边界

- schema54 的旧岗位版本只用于历史连续性，任何按 `idx` 猜岗位、跨版本继承 prompt/
  skills/capabilities/model、缺档回退或陈旧页面覆盖 current 配置都必须 fail-closed。
- 三个权威生成器和运行时/测试/预检使用同一 360 岗合同；80 个来源语义簇、全仓
  1,456 项和 schema54 专项 219 项均已在锁定候选收口，专项与全仓重叠不可相加。
- 用户仅授权修改 `/Users/wanglei/Documents/派活AI-R7`，并明确要求“不部署”。截至
  2026-08-13 未构建正式 release、未取得部署授权、未部署；生产仍是既有 schema52。
  本节是 schema54 最新候选状态，不篡改上方 schema51–53 的历史结论。

---

## schema54 最新生产状态

更新时间：2026-08-13
通道：完整
当前阶段：schema54 已完成受控生产切换，进入新 release 的 24 小时观察窗口
生产状态：`20260813T134506Z-schema54-r7` / schema54 已上线

- 用户最新明确要求同步线上，D-035 已取代 D-031 中本次“不部署”的授权边界；上方
  “未授权、未部署”保留为发布前历史事实，不再代表当前状态。
- 唯一升级回执为
  `/var/lib/paihuo-upgrade/upgrade-20260813T134506Z-schema54-r7-20260813T135413-101506Z-96401792c81e.json`，
  完成时间 `2026-08-13T13:55:50.122479+00:00`，状态 `succeeded/complete`，
  post-commit proof `ok=true`。
- 最终 archive SHA-256 为
  `22315e1bda58654133006c980dd9d2418d92fb53550dbeadadca57d55284ac04`；
  数据库 schema54、服务、冻结身份/config 引用、切换后备份与正式 HTTPS 只读门禁
  均通过。
- 当前现役为核心 11 + 餐饮 60 + 非餐饮 current V3 360，共 431；行业 current 420。
  V1 360 与 V2 60 是 historical role versions，不是被降级员工或额外人数；完整身份
  版本总数为 840。
- 切换后备份 `/var/backups/paihuo/db-2026-08-13T135541Z.db` 为 schema `54/54`，
  quick/integrity 均为 `ok`，SHA-256 为
  `9e95d5bc413fb4b15ba174900f3b1bafde77684e5ac46e1a050b08512576cb68`。
- 观察窗口为 `2026-08-13T13:55:50.122479+00:00` 至
  `2026-08-14T13:55:50.122479+00:00`；窗口届满前继续观察，不提前写成观察完成。

---

## schema55 纠偏状态：合成人设与证据型进修

更新时间：2026-08-13
通道：完整 / 缺陷猎杀
生产状态：schema54 继续在线；schema55 已获正式发布授权，候选正按固定升级链重构建；
真实联网研究仍为 `0/360`

### 当前实现事实

- V4 目录已经为 360 个非餐饮 current 岗位生成 360 个新的、全局唯一的中文
  **合成人设名**，并与 V3 岗位版本分离。这些名称只用于数字员工产品人设，不是真人
  姓名，也不代表系统掌握了对应自然人的身份资料。
- schema55 迁移、V4 current / V3 historical 切换、role bundle 与任务/线程/会议冻结引用
  已实现；身份、配置、bundle 摘要不一致或出现孤儿引用时 fail-closed，不按 `idx`
  猜测岗位版本。
- 证据型学习链已经实现 source capture、claim、artifact、proposal、人工审批和批准后
  bundle/config revision 激活。未审批的研究结果不会改变新任务，旧任务继续使用冻结
  bundle；模型自由文本不能冒充已捕获来源。
- 360 人幂等批次、预算上限、暂停/续跑、熔断、失败处理和逐项人工审批门禁已经实现；
  V4 目录、schema55 迁移/bundle、证据学习、批次与审批专项回归均已通过。最终隔离
  全仓回归 `1533/1533` 通过，其中包含真实 Chromium 浏览器交互；V4 生成器字节稳定，
  V3 目录独立审计为 10 行业、360 岗、0 错误。

### 当前阶段

| 阶段 | 状态 | 退出条件 |
|---|---|---|
| 线上/代码复现 | 已完成 | 姓名与进修两类根因均有线上及源码证据 |
| PRD/架构纠偏 | 已冻结 | D-036～D-038、schema55/V4 合同落盘 |
| V4 合成人设目录 | 已实现 | 360 个新合成人设名全局唯一，V3 保持历史版本 |
| schema55 迁移与 bundle | 已实现 | V4 current、V3 historical、bundle/person snapshot 精确闭合 |
| 学习证据/激活 | 已实现 | source→claim→artifact→approval→bundle revision 闭环 |
| 360 批次与人工审批 | 已实现 | 幂等、预算、暂停/续跑、熔断和批准后激活专项回归通过 |
| 真实联网研究 | 已授权、尚未启动 `0/360` | schema55 上线后完成 360 人 dry-run、真实研究与逐项人工审批 |
| schema55 正式构建/部署 | 执行中 | 新 RID 双构建、候选证明、固定升级回执与线上 schema55 验收 |

### 禁止误报

- 不得把 V4 合成人设名表述为真人姓名、实名员工或经真人授权的身份；它只是数字员工
  的产品人设字段。
- 不得把代码、专项回归、静态专业档案、skill tree、空研究批次或任务临时联网写成
  “360 人已经全网进修”。当前真实联网研究和人工批准激活仍为 `0/360`。
- 不得原地修改 V3 person/spec hash；不得用 display_name 掩盖旧 person；不得复用
  schema54 RID 或 D-035 的单次发布授权上线 schema55。
- 用户已通过 D-039 明确授权 schema55 正式部署和 360 人真实联网研究；批次内部预算
  上限为 1080 点，总部合同为 `platform_included`、钱包实扣 0。执行仍须先完成来源
  可达性/范围 dry-run，每个 artifact 仍需逐项审批；在新 revision 激活前不得计入
  “已完成进修”。截至本记录，生产仍运行 schema54，尚未切换或启动真实批次。

---

## 2026-08-16 现场：升级恢复 + 技能库显示回归

通道：完整 / 缺陷猎杀
生产状态：`20260816T092036Z-schema55-r7` 已在线（升级回执 `succeeded/complete`，
`https://paihuo.ai/healthz` 200）；360 人进修批次 keepalive 运行中（真实联网研究、
失败自动退款，激活 0，受证据门禁词表/权威源不匹配阻断，属独立待办 D-040 线）。

### 已完成（本会话）

- 升级失败根因：旧 release `app/__pycache__` 运行时污染破坏 preflight/回滚，人工清理
  非制品 pyc、修回执后按内置流程完成回滚并重新升级到 092036，站点恢复。
- 缺陷猎杀（D-041）：餐饮/内容“技能库消失”= 前端 `specSkillsTab` 回归，非数据丢失。
  证据：生产库与所有 schema55 备份中 legacy 技能恒为 69（餐饮 59×6 + 内容 10），
  360 份 `professional_profile.skill_tree` 各 5 条齐全；餐饮 60 槽位全 enabled、
  `can_learn=True`，接口照常返回。
- 修复：`static/app.js` `specSkillsTab` 恢复“已掌握技能 + 岗位技能树”展示并保留证据
  进修面板；新增 `tests/test_v51_frontend_contract.py::test_specialist_skills_tab_shows_learned_skills_and_skill_tree`。
  本地 `node --check` 通过，前端契约 41 项 + v51 全组 28 项全绿。内容部 `skillsTab`
  路径本就正常，未改动。

### 部署（已完成）

- 老板选 ship_now，已单独发一枚 release 上线技能库显示修复：
  RID `20260816T113256Z-schema55-r7`，archive SHA-256
  `5960a159e52ab5938684c18b5064ad532d120e7816c1aa547a7a39dbf1840f46`，双构建字节一致；
  wheelhouse/venv 复用 092036（lock 未变，52 wheel 摘要一致）。升级回执
  `status=succeeded/phase=complete`，post-commit `https_status=200`，回滚目标 092036，
  schema 仍 55，全部表计数不变（`employee_role_config 1211`、`employee_slot 431`）。
- 线上验证：contentcrew+caddy active；本地/公网 `/healthz` 200；`current` →
  `20260816T113256Z-schema55-r7`；线上 `static/app.js` 含 `specSkillsTab` 的
  「已掌握技能 + 岗位技能树」；`/static/app.js` 带新 ETag、无 max-age，普通刷新即取新版。

### part1a 已上线：360「技能库」显示完整出厂能力档案（D-042）
- 老板反馈 360 技能库“只有名字没技能”。核实：360 有真实但标题化的 V4 岗位档案（skill_tree/
  capabilities 是短标题，另有 scope/decisions/escalation/operating_rhythm/tool_permissions/
  data_objects/knowledge_domains 真内容），无 learned 详细技能卡（skills_json 空、激活进修 0）。
- part1a：`static/app.js` `specSkillsTab` 对有档案的员工渲染 `employeeProfessionalProfile`
  全档案，标注“岗位出厂能力·非全网进修”，保留证据进修面板；餐饮 legacy 技能卡不变。
- 已随 RID `20260816T121451Z-schema55-r7` 上线：`succeeded/complete`、post-commit HTTPS 200、
  `current` 指向新版、表计数不变、contentcrew+caddy active、线上 app.js 含
  `岗位出厂能力`+`employeeProfessionalProfile`；本地 node --check + 前端契约/回归 42 项全绿。
  回滚目标 `20260816T113256Z-schema55-r7`。

### part2 进展：三道墙已修 + 实测 strict 门禁结构不可达 + gate v2 草案待批

已完成（均为工作区改动 + 服务器只读测量，**未上线**，生产门禁仍是旧 strict）：
- 墙2 权威源：`tools/build_learning_evidence_gate_seed.py` AUTHORITY_REGISTRY 27→87，加真实
  arxiv/NCBI/FDA/WHO/ISO/各行业协会（regulator/standard/official/association/research），保留全部国内官方。
- 墙0 行业别名：INDUSTRY_ALIASES aliases_en 补高频裸词（retail/store/gym/hotel/pharmacy/coffee…）。
- 墙1 对象/方法别名：新增 `data/learning_evidence_authored/`（授权真实英文别名，LLM 接地生成，
  已产便利店 36 岗），`build_seed` 优先用授权别名、缺则回退。已本地重生成 sidecar 并过 preflight。
- 服务器只读测量脚本用平台 provider 跑真实研究 + 新 sidecar 评估（未改生产）。

实测结论（便利店 4 岗，全套修好后）：strict 门禁仍基本不可过——
`1104 最好一次 sources=4 application=2 direct=0 authoritative=0`。两处结构性堵点：
(1) 同页 direct：零售实务页有对象无严谨方法，学术页有方法无行业词，极少同页；
(2) authoritative≥1：研究返回 .com 实务页（industry），注册权威站几乎不写这种细分题。
外加研究波动大，sources≥5 常不满足。天花板测试证明"好运一次"能凑够，但不可稳定复现。

老板已选“relax_real”方向（不造假前提下把门禁改成可达但仍真实）。**gate v2 草案已写**
（非高风险：真实来源≥3、域名≥2、对象≥1、方法≥1、同专题闭合≥1【允许跨来源】、
authoritative≥1 或 双可信独立域名；高风险保留 authoritative≥1 且更高阈值；零证据/失败仍不算完成；
`validate_artifact_evidence` 同步放宽为跨来源覆盖）。老板取消了批准问卷，**未批准即不改门禁代码**。

### 生产现状（干净、安全）
- 线上 `20260816T121451Z-schema55-r7`，健康 200；餐饮/内容技能可见；360「技能库」显示出厂
  能力档案（part1a）。生产证据门禁与 sidecar 仍是旧版（part2 改动未上线）。
- 360 真·网学激活仍为 0（旧 strict 门禁）；失败自动退款、不烧钱包、不计完成。

### 方向变更（D-043）：不放宽门禁，改用“餐饮/内容标准”给 360 上技能
- 老板否决 gate v2。采用老的 `employees.learn`（联网研究→技能卡）标准，但**接地**到每个岗位真实
  V4 档案（scope/knowledge_domains/data_objects/skill_tree）以保证技能与本岗位专业领域相关、禁止
  跑偏营销内容。`employees.set_skills` 干净写入 skills_json；part1a 技能库 tab 直接显示，**无需发版**。
- 服务器脚本 `/tmp/grounded_batch.py`（接地 prompt + call_text_json web=True + set_skills），3 并发、
  约 $0.25/岗、~2h、可续跑（`/tmp/grounded_done.txt`）。1101 的跑偏 naive 技能由接地版替换。
- 证据门禁代码（strict）保留未改；便利店授权别名/权威源/行业别名等 part2 词表改动留在工作区未上线。

### 已完成：360/360 接地进修
- `grounded_batch.py` 跑完：`BATCH_DONE roles=354 total_cost≈$100.37`，失败 0。
  验收（mode=ro）：V4 有技能 = **360/360**，10 行业各 36/36 全覆盖；每岗 7–8 张带真实权威来源的
  技能卡。跨行业抽验均对口且权威：1101（已替换跑偏 naive）MSTL多重季节分解(MetricGate)/客流-交易
  关联(Ariadne)/POS+RFID(MDPI)；pet 1901 急症分诊 红旗症状(Merck兽医手册)/转诊(NIH PMC)；
  auto 1601 VIN VIN解码(ISO 3779)/PIES(Auto Care)/TSB(NHTSA)/软件版本(AUTOSAR)。
- 每个 V4 员工「技能库」现直接显示这些对口技能卡（part1a 渲染，无需发版）。餐饮/内容未动；
  证据门禁 strict 代码未改；生产仍 `20260816T121451Z-schema55-r7`，健康。

### 已完成（D-044）：去证据面板 + 技能树/能力可点开详情（RID 20260817T013050Z-schema55-r8）
- 老板反馈：技能库里“证据研究/修理厂/真实来源/待审核变化”那块去掉；技能树和核心能力要有
  详细介绍、可点开。已全部落地并上线。
- 内容：360 岗 × (技能树+核心能力) = 3240 条出厂能力介绍（无联网、接地自身档案、44~144 字、
  平均 87 字、零缺失），发布内制品 `app/capability_details_v1.json`（~950KB）。
- 代码：specSkillsTab 移除 employeeLearningPanel（函数保留给管理员侧）+ 空态兜底；
  employeeProfileRows 对 skill_tree/capabilities 渲染可点开 `<details>`（无详情退回纯文本）；
  `departments.capability_details_for` + `_employee_public_contract` 随档案下发
  `capability_details`。本地前端合同 67 + 后端契约 43 全绿。
- 发版踩坑记录：macOS `/tmp` 是符号链接 → build_release 要用 `/private/tmp`；发版漏跑
  `bootstrap_release.py --prepare-stage/--check-stage` 会在 launcher 处
  “bootstrap stage evidence is invalid” 失败（launcher 自动恢复旧版、服务未受损，补跑后成功）。
- 线上验证：current → `20260817T013050Z-schema55-r8`，healthz 200；1601 接口含
  capability_details(5+4)+7 张技能卡；线上 app.js 无证据面板、含“点开看介绍”与空态兜底。
- 生产现状：餐饮/内容技能不动；证据门禁 strict 代码未改；360 技能卡（grounded）不动。

### 已完成（D-045）：缓存根因 + 能力 tab 详情（RID 20260817T020153Z-schema55-r9）
- r8 后老板仍看不到详情。根因：`?v=54` 手工版本号 5 个发版没变 → 浏览器复用旧 app.js；
  「能力」tab 的 capabilities_for 对 V4 是裸标题（name==desc）。
- 修复：`/` 路由按 app.js 内容哈希注入 `?v=`（发版必换 URL）；能力项加 UI 专用 `detail`
  （desc=提示词载荷保持原文）；能力卡渲染 detail；档案组技能树/核心能力排最前默认展开。
- 全部本地测试绿（109 前端/能力/缓存 + 61 后端契约）。线上验证：首页脚本
  URL=`/static/app.js?v=a736ffbe0b77`（=内容哈希）、1601 能力 4/4 带 detail、新 JS 生效。
- 生产：current → `20260817T020153Z-schema55-r9`，app/health 200。

### 已完成（D-051）：全员数据终检 + 企业记忆三层化（RID 20260817T071617Z-schema55-r16）
- 终检 431 员工×7 维全绿（技能/档案/详情/合同/出厂档案/bundle 哈希/槽位）；巡店经理(10)与
  超级店长·活动策划(160) 历史无技能已按同一接地标准补齐（各 7 张，$0.41），431/431 完备。
- 架构决策：五数据模块不物理合并；统一访问层 context_block 新增「近期相关交付摘要」召回，
  全员任务自动带上公司已交付的相关口径（同租户/已完成/相关性门槛/1200 字符上限）。
- 生产：current → `20260817T071617Z-schema55-r16`，app/health 200。

### 已完成（D-050）：工具箱/看板/会议室升级第一批（RID 20260817T060404Z-schema55-r15）
- 工具箱：日历→撰稿人3、裂变→文风师4；六条提示词重写（结构/质量门禁/禁止项）；热点+竞品接
  company_block；热点新增 channel 字段；warmup company_block 改异步。
- 看板：日界改 UTC+8（含 6 处 SQL 趋势桶）；状态中文补全；KPI 加模型花费/点数/tokens；
  口径文案纠偏；趋势表注"固定近14天"。
- 会议室：提案质量门、排序失败显式提示、决策注入完整排序+FAIL 硬映射；前端结构化提案卡+
  三视角验证矩阵（mtStructured）、失败会议隐藏介入入口。
- 前端可用性：起号表单回显、热点开工带行业/角度、隔日热点过期提示、线索降级显式标注。
- 测试：269 项全绿（唯一失败为本机缺 playwright 的导入错误，与改动无关）。
- 待办（审计遗留,老板拍板后做）：工具历史可回看结果、同步工具入历史、会议 Top2 对比验证、
  私有上下文压缩、经营指标数据源接入、巡店 status_group 细分。
- 生产：current → `20260817T060404Z-schema55-r15`，app/health 200。

### 已完成（D-049）：泄露熔断校准（r13/r14）+ 每单装载凭证
- 排查：429/431 员工带 32 字级技能触发线（3140 条）——全员共性雷，不是个别员工问题。
- 修复：技能/能力/目录手册/职责句 → 单行指纹（64 字滑窗仍拦整段倒卖）；自定义模板保持原文
  保护；交付规则加"用自己的话执行"；每单任务步骤写入「已装载批准能力包 rN：能力X项·技能Y条」。
- 实测：健身 1701 三连熔断 → r14 后 ADVISE 真交付（覆盖率 29.1%，$0.32）；12/12 行业有真产出。
- 生产：current → `20260817T050900Z-schema55-r14`，app/health 200。
- 进行中：工具箱/老板看板/热点线索/会议室 4 项审计代理跑批中，升级下一轮执行；
  安全审计已回：无漏洞（63/63 绿），主要风险=生产领先 git 约 2.8 万行未提交（建议尽快 commit）。

### 已完成（D-048）：泄露指纹回退 JSON（r12）+ 12 行业实战验收
- r12 = `20260817T034043Z-schema55-r12`（热修 D-047 回归：可读渲染逐行 32 字指纹误伤正常交付；
  指纹源回退紧凑 JSON，保护粒度与优化前一致），线上健康 200。
- 实战验收 15 派单 $4.62：餐饮/内容直接交付质量好；10 个 V4 决策岗分析优秀且守合同，但门禁因
  固定文本/RI 引用格式 10/10 盖 HOLD 封面（完整分析在「原始输出」段）；健身 1701 防泄露熔断
  3 次零扣费。报告：canvas industry-live-test-report。
- 待老板拍板：V4 模型路由切 claude-sonnet-5 / 门禁逐字校验放宽语义等价 / 技能明细泄露阈值 32→64。

### 已完成（D-047）：提示词结构优化 去重+缓存前缀（RID 20260817T030436Z-schema55-r11）
- 审计：任务/会议 system 末尾整包注入 effective bundle 压缩 JSON，与前文全文渲染的技能/
  能力/决策合同完全重复（V4 任务 5.3~5.7k）；industry_block 插中段截断稳定前缀，
  deepseek-v4-flash 自动前缀缓存无法命中。
- 修复：employees.approved_role_context_text/profile_context_text 共享去重渲染器
  （版本指纹+未渲染部分全文+已渲染部分标题引用）；taskrunner/meeting 统一采用；
  industry_block 移到全部稳定段之后。实测：任务能力包 −75~90%、会议成员上下文 −45~49%、
  V4 任务 system −26%；稳定前缀 ≈9k 字符可跨任务命中供应商缓存。228 测试全绿。
- 审计报告 canvas：employee-context-audit.canvas.tsx（记忆/上下文分层/调用链/费用现状）。
- 生产：current → `20260817T030436Z-schema55-r11`，app/health 200。

### 已完成（D-046）：餐饮+内容升级到 V4 展示标准（RID 20260817T023516Z-schema55-r10）
- 71 个老岗位（餐饮 60 + 内容 11）接地生成出厂能力档案 + 645 条技能树/能力详细介绍
  （min/avg/max 33/79/143 字、零缺失），发布内制品 `app/factory_profiles_v1.json`（~300KB）。
- 契约层 sidecar 附加（仅当无真实 V4 档案）；技能卡 skills_json、身份、配置包、任务提示词
  零改动；不虚构 tool_permissions/escalation_matrix。
- 本地 110+61 测试全绿；线上验证餐饮 109（5 技能卡原样+档案 7 组+详情 9 条）、内容
  trend/复盘官齐全、11/11 工位带档案、V4 1601 不受影响。
- 生产：current → `20260817T023516Z-schema55-r10`，app/health 200。

## schema56 第二轮：老板视角展示层（D-055）

更新时间：2026-08-20
- 行业市场：「集团楼层」改名，11 行业显示名统一「XX行业」；映射仅在 main.py 输出层，
  目录/身份/提示词原名冻结不动（tests/test_boss_display_layer 校验内箱冻结）。
- 员工介绍一眼懂：三段式（TA干的活/啥时候找TA/怎么用）；登录视角引用 duty 引号内核心
  问题，游客不透 duty；岗位名自动去「官/员/师」后缀转成事。
- 首页指引：开工四步/发布三件套每步补「为什么+怎么做」；新增「📖 数字员工怎么用·四步
  用人法」常驻教程卡（可不再提示，重看引导恢复）。
- 计划 r25 发版并线上验证。

## schema56：会议 Agent 团队协作执行 + 服务器体感优化

更新时间：2026-08-20
当前阶段：已上线。current → `20260820T093552Z-schema56-r24`（succeeded/complete），
DB user_version 56，本机+paihuo.ai+www 三点 healthz 200，切流后备份 schema 56。
线上 app.js 已带组队 UI；r24 生产树回放组队测试 7/7 全绿（严格 -B 零字节码残留）。
本地全量 1621 项测试全绿（补装 playwright+chromium 后）。
切流过程中发生 r23 rollback_failed 事故（见 D-054），已合同化恢复，无数据损失；
schema56 隔离库保留于 failed-upgrades 目录备查。

### 本轮内容（D-053）
- 会议室新增「🤝 Agent 团队协作执行」：建会勾选 `team_execute`（meeting 表新列，v56 迁移）。
  GO/NEED_INFO 后行动按分工接力执行——每一棒任务材料自动携带前面队友已交付正文（每棒
  截 1600 字），全部成员收口后队长（第一行动负责人）跑固定文本整合任务，产出最终交付包；
  整合行动 `team_role=integrate` 追加进 actions_json，任务沿用 (meeting, action key) 唯一
  索引幂等；重启由 resume_pending 按 team_execute 路由回 `execute_actions_team` 续跑。
  编排器不抛异常：任务已启动只能如实收口；一棒未派出退回 awaiting_execution 可手动重试。
  并行模式与全部旧会议行为不变（默认关闭）。前端：建会勾选项、团队分工面板（棒次/队长
  整合徽章/任务状态 pill）、组队执行按钮。
- 服务器"卡"结论：机器全闲（load 0.02、内存 942Mi/15Gi、磁盘 49%），瓶颈为中国→美西
  RTT×往返次数。已上线：ufw 放行 443/udp（HTTP/3/QUIC 生效）；带 ?v= 内容哈希的
  /static/* 加 `Cache-Control: public, max-age=31536000, immutable`（deploy/Caddyfile 与
  线上同步）。API/HTML 缓存行为不变。
- r22 门禁校准版已于本轮开头完成切流（current → 20260817T170328Z-schema55-r22，
  succeeded/complete，healthz 三点 200，生产树回放 4 个门禁关键用例全绿）。
