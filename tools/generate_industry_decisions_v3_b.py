#!/usr/bin/env python3
"""DEPRECATED: old schema-54 prototype generator (intentionally inert).

The reviewed catalogs now have two authoritative generators:
``generate_industry_decisions_v3_hotel_fitness.py`` and
``generate_industry_decisions_v3_auto_beauty_pet.py``. This source is retained
only to make old automation fail loudly; it must never overwrite those files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V2_DIR = ROOT / "data" / "industry_decisions"
V1_DIR = ROOT / "data" / "departments"
OUT_DIR = ROOT / "data" / "industry_decisions_v3"
CATALOG_VERSION = "2026.08.v3"
AS_OF = "2026-08-12"
GO_SEMANTICS = "证据足以进入人工审批，不代表允许系统执行任何业务写操作"
DECISION_STATES = ["GO", "HOLD", "ESCALATE", "ADVISE"]

BASELINE_POLICY = (
    "同口径采用本企业最近4个完整统计窗口的中位数；不足4个窗口标记“基线不足”，"
    "不得用行业值或臆测值补齐"
)
TARGET_UP = (
    "由有权负责人依据本企业同口径基线、风险容忍度和适用法定要求书面批准；"
    "目标方向为不低于获批基线，目录不预设行业阈值"
)
INDUSTRY_LABELS = {
    "hotel": "酒店住宿行业痛点数字员工",
    "auto": "汽车维修行业痛点数字员工",
    "fitness": "健身场馆行业痛点数字员工",
    "beauty": "生活美容行业痛点数字员工",
    "pet": "宠物服务行业痛点数字员工",
}

USAGE_CADENCE = {
    "forecast": "每日滚动监测 + 需求事件触发 + 每周复盘",
    "economics": "逐批/逐单核算 + 日结异常触发 + 每月复盘",
    "trace": "逐对象留痕 + 断链实时触发 + 每周抽检",
    "risk": "逐次准入/放行门禁 + 风险事件实时升级 + 每月演练复盘",
    "operations": "逐班次/逐预约调度 + 约束变化实时重算 + 每周复盘",
    "customer": "每日服务队列 + 权益/客诉事件触发 + 每月效果复盘",
    "governance": "每周例行评审 + 重大变更触发 + 每月治理复盘",
}


@dataclass(frozen=True)
class IndustrySpec:
    key: str
    start_idx: int
    tagline: str
    groups: tuple[tuple[str, str, str], ...]
    roster: str


GROUP_SIZES = (5, 5, 5, 5, 5, 4, 4, 3)

SPECS = (
    IndustrySpec(
        "hotel",
        1501,
        "围绕酒店真实需求、房态交付、治安卫生、净收益和资产韧性设置36个可复核决策岗位。",
        (
            ("需求与渠道收益", "📈", "#0369A1"),
            ("产品与客源增长", "🧭", "#0E7490"),
            ("房务与卫生交付", "🛏️", "#047857"),
            ("安全与住客恢复", "🛡️", "#B45309"),
            ("能耗人才与采购", "🏗️", "#7C3AED"),
            ("库存财务与投资", "🧾", "#BE123C"),
            ("品牌合规与连续", "🏨", "#4338CA"),
            ("口碑与经营智能", "🔎", "#334155"),
        ),
        r"""
demand-event-forecast|需求曲线与会展事件预测官|HOTEL-NET-REVENUE
inventory-upgrade-protection|房态库存与房型升级保护官|HOTEL-NET-REVENUE,HOTEL-ROOM-TURN
overbooking-walk-noshow|超售Walk与No-show回收官|HOTEL-NET-REVENUE
ota-net-rate-commission|OTA渠道净价与佣金审计官|HOTEL-NET-REVENUE
corporate-group-profit|企业协议价/团队包盈利官|HOTEL-NET-REVENUE
price-elasticity-calendar|房价弹性与日历收益官|HOTEL-NET-REVENUE
promo-incrementality|促销增量与蚕食实验官|HOTEL-NET-REVENUE
direct-member-repeat|直订会员复住增量官|HOTEL-NET-REVENUE,HOTEL-EXPERIENCE-RCA
trevpar-ancillary-mix|TRevPAR早餐/会议/增值组合官|HOTEL-NET-REVENUE
local-demand-conversion|本地客源与商圈转化官|HOTEL-NET-REVENUE
room-turn-peak-schedule|客房翻房峰值排程官|HOTEL-ROOM-TURN
cleaning-inspection-release|清洁抽检与房态放行官|HOTEL-ROOM-TURN,HOTEL-HYGIENE-WATER
linen-separation-turnover|布草洁污隔离与洗涤周转官|HOTEL-HYGIENE-WATER
amenity-breakfast-batch|客用品/早餐批次效期官|HOTEL-HYGIENE-WATER
maintenance-ooo-priority|设施维修停房优先级官|HOTEL-ROOM-TURN,HOTEL-ASSET-ENERGY
lock-pms-recovery|门锁/PMS故障恢复官|HOTEL-ROOM-TURN,HOTEL-SECURITY-MINOR
realname-minor-five-musts|实名登记与未成年人五必须官|HOTEL-SECURITY-MINOR
foreign-guest-card|外籍登记/外卡受理官|HOTEL-SECURITY-MINOR
guest-incident-recovery|住客事故投诉服务恢复官|HOTEL-EXPERIENCE-RCA
hygiene-water-breakfast-capa|卫生/水质/早餐食品CAPA官|HOTEL-HYGIENE-WATER,HOTEL-EXPERIENCE-RCA
energy-carbon-ledger|水电冷热能耗与碳账官|HOTEL-ASSET-ENERGY
frontdesk-room-skill-schedule|前台客房技能排班官|HOTEL-ROOM-TURN
peak-outsourcing-sla|峰值外包/劳务SLA官|HOTEL-ROOM-TURN
vendor-linen-audit|供应商质量与布草外包审查官|HOTEL-HYGIENE-WATER
procurement-tco-green|采购TCO与绿色替代官|HOTEL-ASSET-ENERGY
breakfast-amenity-fefo|早餐/客用品FEFO与缺货官|HOTEL-HYGIENE-WATER
daily-pos-ota-payment-reconcile|日结POS/OTA/支付对账官|HOTEL-NET-REVENUE
goppar-cashflow-warning|单店GOPPAR/现金流预警官|HOTEL-NET-REVENUE,HOTEL-ASSET-ENERGY
capex-roi-priority|资产翻新/CAPEX回报排序官|HOTEL-ASSET-ENERGY
franchise-brand-audit|加盟店品牌标准与稽核官|HOTEL-EXPERIENCE-RCA
pms-identity-access|PMS/住客身份最小权限官|HOTEL-SECURITY-MINOR
cancellation-prepaid-price|退订/预付合同与价格合规官|HOTEL-NET-REVENUE,HOTEL-SECURITY-MINOR
emergency-continuity|消防停水停电业务连续性官|HOTEL-ASSET-ENERGY,HOTEL-ROOM-TURN
review-authenticity-rca|评价真实性/平台口碑根因官|HOTEL-EXPERIENCE-RCA
compset-anomaly-replication|多店可比组与异常复制官|HOTEL-NET-REVENUE
metric-lineage-alert|酒店指标口径/血缘与预警官|HOTEL-NET-REVENUE,HOTEL-ASSET-ENERGY
""",
    ),
    IndustrySpec(
        "auto",
        1601,
        "围绕车型技术、安全门禁、三包召回、配件工位和返修危废设置36个汽车后市场决策岗位。",
        (
            ("车型技术与安全", "⚡", "#B91C1C"),
            ("召回三包与责任", "🛡️", "#B45309"),
            ("配件工具与供应", "🧩", "#0369A1"),
            ("估价证据与交付", "🧾", "#7C3AED"),
            ("技能工位与产能", "🛠️", "#047857"),
            ("预约返修与客诉", "🚘", "#0E7490"),
            ("经营回收与根因", "📊", "#BE123C"),
            ("危废事件与连续", "♻️", "#334155"),
        ),
        r"""
vin-tech-version|VIN技术资料版本审查官|AUTO-HV-ADAS
hv-poweroff-verification|高压断电验电门禁官|AUTO-HV-ADAS
battery-anomaly-triage|动力电池异常分诊官|AUTO-HV-ADAS
adas-calibration-closure|ADAS标定闭环审查官|AUTO-HV-ADAS
dual-final-release|竣工质量双人放行官|AUTO-HV-ADAS,AUTO-QUOTE-RECORD
recall-hit-disposition|召回命中处置官|AUTO-WARRANTY-RECALL
warranty-eligibility-evidence|三包资格证据官|AUTO-WARRANTY-RECALL
oem-warranty-policy-match|厂家保修政策匹配官|AUTO-WARRANTY-RECALL
repeat-fault-escalation|重复故障升级官|AUTO-WARRANTY-RECALL,AUTO-WASTE-CAPA
customer-pay-boundary|客户自费边界说明官|AUTO-WARRANTY-RECALL,AUTO-QUOTE-RECORD
vin-parts-fitment|VIN配件适配官|AUTO-PARTS-BAY
parts-origin-trace|配件来源追溯官|AUTO-PARTS-BAY
substitute-part-risk|替代件风险评审官|AUTO-PARTS-BAY
special-tool-readiness|专用工具设备就绪官|AUTO-PARTS-BAY,AUTO-SKILL-CAPACITY
shortage-reschedule|缺件预约重排官|AUTO-PARTS-BAY,AUTO-SKILL-CAPACITY
fault-evidence-pack|故障证据整编官|AUTO-QUOTE-RECORD
itemized-labor-estimate|分项工时估价官|AUTO-QUOTE-RECORD
supplemental-repair-authorization|追加维修授权官|AUTO-QUOTE-RECORD
before-after-image-archive|维修前后影像档案官|AUTO-QUOTE-RECORD
settlement-delivery-transparency|结算交付透明官|AUTO-QUOTE-RECORD
technician-skill-match|技师技能匹配官|AUTO-SKILL-CAPACITY
equipment-validity-schedule|设备有效期排产官|AUTO-SKILL-CAPACITY
bay-load-commitment|工位负荷承诺官|AUTO-SKILL-CAPACITY
high-risk-dual-collaboration|高危双人协同官|AUTO-HV-ADAS,AUTO-SKILL-CAPACITY
cross-store-transfer|跨店转单决策官|AUTO-SKILL-CAPACITY
appointment-promise-complete|预约承诺完整官|AUTO-SKILL-CAPACITY
repair-progress-exception|在修进度异常官|AUTO-SKILL-CAPACITY,AUTO-QUOTE-RECORD
same-cause-rework-detect|返修同因识别官|AUTO-WARRANTY-RECALL,AUTO-WASTE-CAPA
complaint-service-recovery|投诉服务恢复官|AUTO-QUOTE-RECORD,AUTO-WASTE-CAPA
warranty-claim-recovery|保修索赔回收官|AUTO-WARRANTY-RECALL
parts-margin-slowmoving|配件毛利慢动官|AUTO-PARTS-BAY
standard-hour-variance|标准工时偏差官|AUTO-QUOTE-RECORD
same-cause-root-capa|同因返修根因官|AUTO-WASTE-CAPA
hazwaste-manifest|危险废物联单官|AUTO-WASTE-CAPA
battery-thermal-sentinel|电池热事件哨兵|AUTO-HV-ADAS,AUTO-WASTE-CAPA
workshop-continuity|车间连续经营官|AUTO-SKILL-CAPACITY,AUTO-WASTE-CAPA
""",
    ),
    IndustrySpec(
        "fitness",
        1701,
        "围绕预付履约、健康筛查、课程产能、器械场地和场馆连续性设置36个健身经营决策岗位。",
        (
            ("预付偿付与权益", "💳", "#7C3AED"),
            ("产品履约与留存", "🔁", "#0369A1"),
            ("健康筛查与急救", "🫀", "#B91C1C"),
            ("许可器械与质量", "🛡️", "#B45309"),
            ("课程容量与场效", "🏋️", "#047857"),
            ("门禁交接与用工", "🗓️", "#0E7490"),
            ("采购营销与隐私", "🔎", "#BE123C"),
            ("对账组合与连续", "📊", "#334155"),
        ),
        r"""
prepaid-solvency-coverage|预付资金偿付覆盖官|FITNESS-PREPAID
package-contract-boundary|课包合同履约边界官|FITNESS-PREPAID
closure-refund-model|闭店停业退款测算官|FITNESS-PREPAID
session-writeoff-evidence|课时核销与消费证据官|FITNESS-PREPAID
freeze-transfer-rights|会员冻结转店权益官|FITNESS-PREPAID
sales-promo-margin-cashflow|售卡促销毛利与现金流官|FITNESS-PREPAID
coach-package-sla|教练课包交付SLA官|FITNESS-PREPAID,FITNESS-ADHERENCE
refund-dispute-evidence|退款/投诉争议证据官|FITNESS-PREPAID
monthly-annual-product-mix|月付年付产品结构官|FITNESS-PREPAID
churn-repeat-prediction|会员流失预警与复购官|FITNESS-ADHERENCE
health-questionnaire-screen|入会健康问询与禁忌筛查官|FITNESS-MEDICAL-RISK
training-risk-substitution|训练风险分级与动作替代官|FITNESS-MEDICAL-RISK
injury-first-aid-response|运动伤害急救响应官|FITNESS-MEDICAL-RISK
highrisk-license-lifeguard|高危项目许可与救生员排班官|FITNESS-HIGH-RISK
pool-water-opening-gate|泳池水质/消毒/开放门禁官|FITNESS-HIGH-RISK
equipment-inspection-lockout|器械安全点检与锁机官|FITNESS-EQUIPMENT
equipment-maintenance-fallback|器械维保与停机替代官|FITNESS-EQUIPMENT
coach-credential-education|教练资质/继续教育审查官|FITNESS-HIGH-RISK,FITNESS-MEDICAL-RISK
course-delivery-safety-capa|课程交付抽检与安全CAPA官|FITNESS-MEDICAL-RISK,FITNESS-CLASS-CAPACITY
complaint-recovery-insurance|会员投诉服务恢复与保险通知官|FITNESS-MEDICAL-RISK,FITNESS-PREPAID
class-seat-waitlist|团课席位/预约候补调度官|FITNESS-CLASS-CAPACITY
pt-slot-coach-load|私教时段与教练负荷官|FITNESS-CLASS-CAPACITY
peak-space-opening-hour|高峰坪效与开放时段官|FITNESS-CLASS-CAPACITY
attendance-burn-noshow|到店/课耗/爽约预测官|FITNESS-ADHERENCE,FITNESS-CLASS-CAPACITY
cleaning-ventilation-peak|设施清洁/通风高峰保障官|FITNESS-HIGH-RISK
access-card-sharing|会员门禁异常与反借卡官|FITNESS-PREPAID
coach-exit-handover|教练离职会员交接官|FITNESS-ADHERENCE,FITNESS-PREPAID
workforce-labor-compliance|场馆排班与劳动合规官|FITNESS-CLASS-CAPACITY
vendor-equipment-tco|供应商/器械采购TCO官|FITNESS-EQUIPMENT
supplement-batch-expiry|蛋白粉/饮品批次效期官|FITNESS-HIGH-RISK
private-traffic-fatigue|私域触达增量/疲劳控制官|FITNESS-ADHERENCE
ad-claim-medical-boundary|广告功效宣称与医健边界官|FITNESS-MEDICAL-RISK
health-face-access-privacy|会员健康/人脸/门禁隐私官|FITNESS-PREPAID,FITNESS-MEDICAL-RISK
daily-fee-burn-refund-reconcile|日结卡费/课耗/退款对账官|FITNESS-PREPAID
multisite-benchmark-class-mix|多店同类对标与课程组合官|FITNESS-CLASS-CAPACITY
emergency-refund-continuity|消防停电/教练缺勤/集中退费连续官|FITNESS-HIGH-RISK,FITNESS-PREPAID
""",
    ),
    IndustrySpec(
        "beauty",
        1801,
        "围绕生活美容边界、服务前门禁、预付履约、不良反应和宣称证据设置36个美业决策岗位。",
        (
            ("项目边界与资质", "🪞", "#7C3AED"),
            ("服务前安全门禁", "🛡️", "#B91C1C"),
            ("产品仪器与批次", "🧴", "#B45309"),
            ("预付权益与退款", "💳", "#0369A1"),
            ("技师产能与疗程", "🗓️", "#047857"),
            ("反应客诉与CAPA", "🩹", "#BE123C"),
            ("证据宣称与口碑", "🔎", "#0E7490"),
            ("隐私对账与连续", "🔐", "#334155"),
        ),
        r"""
life-medical-boundary|生活医美边界识别官|BEAUTY-BOUNDARY
facility-qualification-fit|机构资质适配官|BEAUTY-BOUNDARY
practitioner-scope|从业者执业范围官|BEAUTY-BOUNDARY
device-scope-gate|美容仪器适用门禁官|BEAUTY-BOUNDARY
highrisk-referral|高风险客户转诊官|BEAUTY-BOUNDARY,BEAUTY-PRE-SERVICE
contraindication-completeness|禁忌症问询完整官|BEAUTY-PRE-SERVICE
sensitivity-patch-gate|皮肤敏感试用门禁官|BEAUTY-PRE-SERVICE
informed-consent-scope|知情同意范围官|BEAUTY-PRE-SERVICE
disinfection-crossinfection|消毒交叉感染官|BEAUTY-PRE-SERVICE
store-emergency-readiness|门店应急准备官|BEAUTY-PRE-SERVICE
cosmetics-registration|化妆品注册备案官|BEAUTY-PRE-SERVICE,BEAUTY-CLAIMS
batch-expiry-quarantine|批次效期隔离官|BEAUTY-PRE-SERVICE
storage-deviation|储存条件偏差官|BEAUTY-PRE-SERVICE
device-maintenance-calibration|仪器维保校准官|BEAUTY-PRE-SERVICE
product-service-compatibility|产品项目配伍官|BEAUTY-PRE-SERVICE,BEAUTY-REACTION-CAPA
prepaid-contract-transparency|预付合同透明官|BEAUTY-PREPAID
stored-value-liability|储值履约负债官|BEAUTY-PREPAID
abnormal-topup-deterrence|异常充值劝阻官|BEAUTY-PREPAID
cross-store-rights|跨店权益迁移官|BEAUTY-PREPAID
closure-refund-disposition|闭店退款处置官|BEAUTY-PREPAID
service-technician-match|项目技师匹配官|BEAUTY-CONTINUITY
departing-staff-handover|离职客户交接官|BEAUTY-CONTINUITY
room-device-peak-capacity|房间设备峰值官|BEAUTY-CONTINUITY
treatment-interval-calendar|疗程间隔日历官|BEAUTY-CONTINUITY
noshow-waitlist-reschedule|爽约候补重排官|BEAUTY-CONTINUITY
adverse-reaction-triage|不良反应分级官|BEAUTY-REACTION-CAPA
abnormal-service-suspension|异常项目暂停官|BEAUTY-REACTION-CAPA
complaint-evidence-preserve|客诉证据保全官|BEAUTY-REACTION-CAPA
beauty-incident-capa|美容事故CAPA官|BEAUTY-REACTION-CAPA
before-after-authenticity|前后对比真实性官|BEAUTY-CLAIMS
efficacy-claim-evidence|功效宣称证据官|BEAUTY-CLAIMS
expectation-calibration|客户期望校准官|BEAUTY-CLAIMS,BEAUTY-REACTION-CAPA
reputation-root-remediation|口碑根因整改官|BEAUTY-CLAIMS,BEAUTY-REACTION-CAPA
sensitive-image-privacy|敏感影像隐私官|BEAUTY-CLAIMS
stored-payment-reconcile|储值支付核销对账官|BEAUTY-PREPAID
regulatory-continuity|监管检查连续官|BEAUTY-BOUNDARY,BEAUTY-PRE-SERVICE
""",
    ),
    IndustrySpec(
        "pet",
        1901,
        "围绕宠物诊疗、生物安全、寄养洗美、活体追溯和兽药食品设置36个宠物行业决策岗位。",
        (
            ("诊疗处方与出院", "🩺", "#0369A1"),
            ("感染隔离与医废", "🧫", "#B91C1C"),
            ("寄养洗美与事件", "🐾", "#7C3AED"),
            ("活体来源与交付", "🧬", "#B45309"),
            ("兽药食品与耗材", "📦", "#047857"),
            ("知情随访与争议", "🤝", "#0E7490"),
            ("排程成本与转院", "📊", "#BE123C"),
            ("福利数据与连续", "🛡️", "#334155"),
        ),
        r"""
emergency-triage|宠物急症分诊官|PET-CLINICAL
clinical-evidence-complete|诊疗证据完整官|PET-CLINICAL
veterinary-prescription-review|兽药处方审查官|PET-CLINICAL,PET-INVENTORY-COMPLIANCE
anesthesia-surgery-gate|麻醉手术门禁官|PET-CLINICAL
discharge-followup-plan|出院随访计划官|PET-CLINICAL,PET-TRUST
infection-risk-screen|传染风险筛查官|PET-INFECTION
isolation-flow|隔离区流线官|PET-INFECTION
disinfection-validation|环境消毒验证官|PET-INFECTION
lab-sample-chain|检验样本链路官|PET-INFECTION,PET-CLINICAL
animal-medical-waste|动物医废暴露官|PET-INFECTION
behavior-risk|性格行为风险官|PET-BOARDING-SAFETY
vaccine-deworm-gate|疫苗驱虫准入官|PET-BOARDING-SAFETY
boarding-cage-match|寄养笼位匹配官|PET-BOARDING-SAFETY
grooming-service-safety|洗美项目安全官|PET-BOARDING-SAFETY
loss-injury-incident|丢失受伤事件官|PET-BOARDING-SAFETY
live-animal-origin|活体来源核验官|PET-LIVE-TRACE
immunity-quarantine-trace|免疫检疫追溯官|PET-LIVE-TRACE
live-delivery-contract|活体交付合同官|PET-LIVE-TRACE
breeding-disease-risk|繁育疾病风险官|PET-LIVE-TRACE
rescue-adoption-handover|救助领养交接官|PET-LIVE-TRACE,PET-TRUST
supplier-quality-entry|供应商质量准入官|PET-INVENTORY-COMPLIANCE
vetdrug-coldchain-expiry|兽药冷链效期官|PET-INVENTORY-COMPLIANCE
prescription-drug-inventory-gate|处方药库存门禁官|PET-INVENTORY-COMPLIANCE
pet-food-recall|宠物食品召回官|PET-INVENTORY-COMPLIANCE
medical-consumable-expiry|医疗耗材效期官|PET-INVENTORY-COMPLIANCE
estimate-informed-consent|诊疗估价知情官|PET-CLINICAL,PET-TRUST
chronic-revisit-guardian|慢病复诊守护官|PET-CLINICAL,PET-TRUST
lifecycle-nutrition|生命周期营养官|PET-TRUST
medical-dispute-evidence|医患争议证据官|PET-CLINICAL,PET-TRUST
vet-room-schedule|兽医诊室排程官|PET-CLINICAL
highvalue-consumable-inventory|高值耗材库存官|PET-INVENTORY-COMPLIANCE
clinical-unit-cost|诊疗单项成本官|PET-CLINICAL
emergency-referral|急诊转院协同官|PET-CLINICAL
animal-welfare-inspection|动物福利巡检官|PET-BOARDING-SAFETY,PET-LIVE-TRACE
owner-data-privacy|宠物主人数据官|PET-TRUST
inpatient-continuity|在院动物连续官|PET-CLINICAL,PET-INFECTION
""",
    ),
)


def parse_roster(spec: IndustrySpec) -> list[tuple[str, str, list[str]]]:
    rows: list[tuple[str, str, list[str]]] = []
    for line in spec.roster.strip().splitlines():
        slug, name, pain_text = (part.strip() for part in line.split("|", 2))
        rows.append((slug, name, [part.strip() for part in pain_text.split(",")]))
    if len(rows) != 36:
        raise ValueError(f"{spec.key}: expected 36 roles, got {len(rows)}")
    return rows


def group_index(position: int) -> int:
    seen = 0
    for index, size in enumerate(GROUP_SIZES):
        seen += size
        if position < seen:
            return index
    raise IndexError(position)


def topic_for(name: str) -> str:
    return name[:-1] if name.endswith("官") else name


def decision_kind(name: str) -> str:
    if any(token in name for token in (
        "需求", "预测", "容量", "客源", "复住", "流失", "爽约", "No-show",
    )):
        return "forecast"
    if any(token in name for token in (
        "成本", "净价", "毛利", "现金流", "盈利", "收益", "GOPPAR", "TRevPAR",
        "回报", "TCO", "对账", "偿付", "退款测算", "工时", "估价", "回收",
    )):
        return "economics"
    if any(token in name for token in (
        "追溯", "证据", "档案", "登记", "账", "血缘", "真实性", "合同透明",
        "完整", "批次", "检疫", "影像", "联单", "核验", "知情", "处方审查",
    )):
        return "trace"
    if any(token in name for token in (
        "风险", "门禁", "安全", "许可", "资质", "召回", "急症", "急救", "事故",
        "异常", "隔离", "消毒", "医废", "禁忌", "转诊", "不良反应", "热事件",
        "隐私", "保护", "卫生", "水质", "边界", "锁机", "五必须", "福利",
    )):
        return "risk"
    if any(token in name for token in (
        "排程", "排班", "调度", "重排", "负荷", "工位", "翻房", "周转", "FEFO",
        "交接", "交付", "调拨", "转单", "转院", "恢复", "连续", "峰值", "时段",
        "笼位", "匹配", "间隔", "候补", "在岗", "就绪", "维保", "停机",
    )):
        return "operations"
    if any(token in name for token in (
        "会员", "客户", "住客", "口碑", "投诉", "客诉", "期望", "评价", "私域",
        "服务恢复", "慢病", "营养", "领养", "权益", "复购", "疗程",
    )):
        return "customer"
    return "governance"


def primary_decision(name: str, kind: str) -> str:
    topic = topic_for(name)
    templates = {
        "forecast": f"{topic}应采用哪个可回放情景，以及该情景是否具备进入人工计划审批的企业证据",
        "economics": f"{topic}的全量经济口径、现金影响与敏感性是否完整，哪个方案具备进入人工审批的净价值",
        "trace": f"{topic}涉及的对象、版本、时间、责任与原始凭证能否闭合为可追溯证据链",
        "risk": f"{topic}是否满足适用准入和风险边界，应该GO、HOLD、ESCALATE还是仅ADVISE",
        "operations": f"{topic}在容量、技能、时窗、服务承诺与安全硬约束下应选择哪个人工执行方案",
        "customer": f"{topic}面向哪个已授权对象和服务场景形成可验证恢复或增量价值，并在何种条件下停止",
        "governance": f"{topic}候选方案是否有足够事实、反证、版本和责任边界进入人工审批",
    }
    return templates[kind]


def workflow(name: str, kind: str) -> list[str]:
    topic = topic_for(name)
    middle = {
        "forecast": ["按门店、对象、时段与可验证事件切分历史事实", "回放候选情景并量化误差、偏差来源与不确定性"],
        "economics": ["还原收入、折扣、成本、损耗、履约、税费和资金时间的全量口径", "比较基线与候选方案的净价值、现金影响和关键参数敏感性"],
        "trace": ["按对象、版本、批次、时间和责任人串联原始凭证", "定位断链、冲突、迟录、重复、失效和不可逆变更"],
        "risk": ["逐项核对适用规则、企业制度、资质许可与硬性红线", "对异常分级并验证隔离、暂停、升级、复核和恢复条件"],
        "operations": ["建立容量、技能、时窗、设备、服务承诺和安全约束矩阵", "模拟候选方案对等待、品质、负荷、异常恢复和连续性的影响"],
        "customer": ["核对授权状态、合同权益、事实时间线和目标服务场景", "用对照识别自然发生、转移、蚕食与真实恢复或增量"],
        "governance": ["对齐决策范围、候选项、适用版本和责任边界", "核验事实、对照、依赖、反证、例外与复盘责任"],
    }[kind]
    return [
        f"锁定{topic}的门店、对象、时间窗、版本和授权责任人",
        *middle,
        f"形成{topic}的GO、HOLD、ESCALATE与ADVISE判定依据",
        f"输出带来源索引、缺口责任、审批人和复盘日期的{topic}只读建议",
    ]


def metrics(industry: str, idx: int, topic: str, kind: str, inputs: list[str]) -> list[dict]:
    source = (
        f"{inputs[0]}、{inputs[1]}，以及本企业批准的指标口径字典、原始明细、"
        "审批记录、执行日志和复核日志"
    )
    first_name, first_formula = {
        "forecast": (
            "情景回放覆盖率",
            f"完成同口径历史回放且保留误差明细的“{topic}”候选情景数 ÷ 同期进入评审的“{topic}”候选情景总数 × 100%；分母为0时记N/A",
        ),
        "economics": (
            "全量经济口径完整率",
            f"收入、优惠、成本、损耗、履约和资金时间均齐备的“{topic}”评审对象数 ÷ 同期应评审“{topic}”对象总数 × 100%；分母为0时记N/A",
        ),
        "trace": (
            "关键链路可追溯率",
            f"对象、版本、时间、责任人与原始凭证完整关联的“{topic}”记录数 ÷ 同期应关联“{topic}”记录总数 × 100%；分母为0时记N/A",
        ),
        "risk": (
            "风险证据完整率",
            f"适用规则、原始事实、分级依据和复核责任均齐备的“{topic}”事项数 ÷ 同期应评审“{topic}”事项总数 × 100%；分母为0时记N/A",
        ),
        "operations": (
            "硬约束入模完整率",
            f"容量、技能、时窗、服务和安全约束均进入比较的“{topic}”方案数 ÷ 同期应评审“{topic}”方案总数 × 100%；分母为0时记N/A",
        ),
        "customer": (
            "授权与恢复证据完整率",
            f"授权、权益、事实时间线、目标场景和对照均齐备的“{topic}”方案数 ÷ 同期应评审“{topic}”方案总数 × 100%；分母为0时记N/A",
        ),
        "governance": (
            "决策证据完整率",
            f"事实、版本、反证、责任人和复盘日期均齐备的“{topic}”事项数 ÷ 同期应评审“{topic}”事项总数 × 100%；分母为0时记N/A",
        ),
    }[kind]
    return [
        {
            "key": f"{industry}_v3_{idx}_m1",
            "name": f"{topic}{first_name}",
            "formula": first_formula,
            "window": "每周滚动、每月复盘；按门店和对象类型分层，保留分子分母明细",
            "source": source,
            "baseline_policy": BASELINE_POLICY,
            "target_policy": TARGET_UP,
        },
        {
            "key": f"{industry}_v3_{idx}_m2",
            "name": f"{topic}建议可解释率",
            "formula": (
                f"已关联原始事实、计算口径、反证和人工复核结论的“{topic}”建议数 ÷ 同期输出“{topic}”"
                "建议总数 × 100%；分母为0时记N/A"
            ),
            "window": "每月；按GO、HOLD、ESCALATE、ADVISE状态分层并保留逐项复核结论",
            "source": source,
            "baseline_policy": BASELINE_POLICY,
            "target_policy": TARGET_UP,
        },
        {
            "key": f"{industry}_v3_{idx}_m3",
            "name": f"{topic}到期复盘闭环率",
            "formula": (
                f"在批准期限内验证结果且保留偏差原因的“{topic}”事项数 ÷ 同期到期应复盘的“{topic}”"
                "事项总数 × 100%；分母为0时记N/A"
            ),
            "window": "每月滚动；按复盘到期月归集，保留基线、建议、人工执行结果与偏差原因",
            "source": source,
            "baseline_policy": BASELINE_POLICY,
            "target_policy": TARGET_UP,
        },
    ]


def build_employee(
    spec: IndustrySpec,
    industry_index: int,
    role_index: int,
    role: tuple[str, str, list[str]],
    pains: dict[str, dict],
    people: dict[int, str],
) -> dict:
    slug, name, pain_codes = role
    idx = spec.start_idx + role_index
    topic = topic_for(name)
    primary_pain = pains[pain_codes[0]]
    group_name, emoji, color = spec.groups[group_index(role_index)]
    kind = decision_kind(name)
    decision = primary_decision(name, kind)
    inputs = list(dict.fromkeys([
        *primary_pain["required_data"],
        *(
            [pains[pain_codes[1]]["required_data"][0]]
            if len(pain_codes) > 1 else []
        ),
        f"{topic}当前候选方案、适用对象、时间窗、版本、责任人与批准边界",
        f"{topic}对象级原始明细、异常/例外、变更日志、反证和既往人工结论",
    ]))[:8]
    while len(inputs) < 4:
        inputs.append(f"{topic}经责任人确认的补充事实证据第{len(inputs) + 1}项")
    outputs = [f"{topic}四态决策单", f"{topic}证据索引、缺口责任与人工审批包"]
    return {
        "idx": idx,
        "num": idx,
        "key": f"{spec.key}-v3-{slug}",
        "name": name,
        "person": people[idx],
        "role": f"{INDUSTRY_LABELS[spec.key]}·{name}",
        "duty": f"围绕“{decision}”持续核验企业事实，形成可执行但不自动执行的行业建议。",
        "desc": f"专职解决{primary_pain['title']}中的{topic}问题，不代理审批、不修改业务系统、不用行业均值替代企业证据。",
        "intro": f"{people[idx]}是{topic}的专属数字员工，负责把对象级业务数据整理成可追溯、可复核、可复盘的人工决策材料。",
        "emoji": emoji,
        "color": color,
        "group": group_name,
        "pain_codes": pain_codes,
        "value_grade": "高价值",
        "usage_cadence": USAGE_CADENCE[kind],
        "priority_rank": role_index + 1,
        "primary_decision": decision,
        "decision_contract": {
            "decision": decision,
            "decision_states": DECISION_STATES,
            "triggers": [
                f"进入{topic}计划、变更、准入、放行、承诺到期或复盘节点前",
                f"{primary_pain['signals'][0]}，或关键事实、规则版本、责任人和状态发生变化",
            ],
            "required_inputs": inputs,
            "evidence_required": [
                f"{topic}对象级原始记录、口径版本、规则版本与来源索引",
                f"{topic}候选方案、反证、异常例外、人工审批责任和复盘时间证据",
            ],
            "workflow": workflow(name, kind),
            "outputs": outputs,
            "success_metrics": metrics(spec.key, idx, topic, kind, inputs),
            "approval_boundary": (
                f"仅输出{topic}建议、证据缺口和复盘要求；任何采购、定价、排班、库存、"
                "客户触达、准入放行、资金、许可、诊疗处置或系统写操作均由有权人员审批执行。"
            ),
            "forbidden_actions": [
                f"不得代替有权人员批准或执行{topic}结论",
                f"不得在{topic}任一必需输入缺失、冲突或过期时返回GO",
                f"不得删除、改写或掩盖{topic}的异常、拒绝、复核和审批记录",
                f"不得用行业均值、模型猜测或无来源阈值填补{topic}的本企业事实",
            ],
            "fallback": (
                f"{topic}的对象、原始记录、适用版本或授权边界任一缺失/冲突时返回HOLD并列出补证责任；"
                f"发现人身安全、违法违规、重大资产/资金或连续经营风险时立即返回ESCALATE。"
            ),
            "requires_human_approval": True,
            "allowed_side_effects": [],
            "go_semantics": GO_SEMANTICS,
        },
        "public_guide": {
            "focus": decision,
            "materials": "；".join(inputs),
            "input_tips": [
                f"先说明{topic}的门店、对象、时间窗、当前候选方案和期望决策日期",
                f"原始流水与现场记录保留唯一标识、时间戳、版本和责任人，不要只给汇总截图",
                f"如{topic}存在缺数、冲突、例外或紧急风险，请明确标注而非自行补值",
            ],
            "output_hint": f"输出{outputs[0]}和{outputs[1]}，不直接执行任何业务操作。",
        },
    }


def build_catalog(spec: IndustrySpec, industry_index: int) -> dict:
    source_path = V2_DIR / f"{spec.key}.json"
    base = json.loads(source_path.read_text(encoding="utf-8"))
    v1 = json.loads((V1_DIR / f"{spec.key}.json").read_text(encoding="utf-8"))
    people = {int(employee["idx"]): str(employee["person"]) for employee in v1["employees"]}
    pains = {pain["code"]: pain for pain in base["pain_points"]}
    roster = parse_roster(spec)
    employees = [
        build_employee(spec, industry_index, index, role, pains, people)
        for index, role in enumerate(roster)
    ]
    groups = []
    for index, (name, emoji, color) in enumerate(spec.groups):
        first = sum(GROUP_SIZES[:index])
        groups.append({
            "name": name,
            "emoji": emoji,
            "color": color,
            "members": [spec.start_idx + n for n in range(first, first + GROUP_SIZES[index])],
        })
    result = {
        "key": base["key"],
        "name": base["name"],
        "emoji": base["emoji"],
        "tagline": spec.tagline,
        "catalog_version": CATALOG_VERSION,
        "as_of": AS_OF,
        "identity_policy": "保留V1原始idx与person；V3为当前岗位版本，历史任务继续冻结其原岗位版本。",
        "selection_policy": "仅保留本行业高频、高价值、可获得企业事实且有明确人工决策边界的36个专属岗位。",
        "sources": base["sources"],
        "pain_points": base["pain_points"],
        "groups": groups,
        "employees": employees,
    }
    return result


def validate(catalogs: list[dict]) -> None:
    expected_starts = {spec.key: spec.start_idx for spec in SPECS}
    all_ids: list[int] = []
    all_keys: list[str] = []
    all_names: list[str] = []
    all_people: list[str] = []
    all_decisions: list[str] = []
    all_metrics: list[str] = []
    for catalog in catalogs:
        key = catalog["key"]
        employees = catalog["employees"]
        input_fingerprints: list[tuple[str, ...]] = []
        expected = list(range(expected_starts[key], expected_starts[key] + 36))
        actual = [employee["idx"] for employee in employees]
        if actual != expected:
            raise AssertionError(f"{key}: ids are not the original contiguous range")
        if len(catalog["groups"]) != 8:
            raise AssertionError(f"{key}: expected 8 groups")
        covered = [member for group in catalog["groups"] for member in group["members"]]
        if covered != expected:
            raise AssertionError(f"{key}: group coverage/order mismatch")
        pain_codes = {pain["code"] for pain in catalog["pain_points"]}
        if {code for e in employees for code in e["pain_codes"]} != pain_codes:
            raise AssertionError(f"{key}: pain coverage is not closed")
        for employee in employees:
            contract = employee["decision_contract"]
            if employee.get("primary_decision") != contract.get("decision"):
                raise AssertionError(f"{employee['key']}: primary decision mismatch")
            if contract["decision_states"] != DECISION_STATES:
                raise AssertionError(f"{employee['key']}: bad decision states")
            if not (4 <= len(contract["required_inputs"]) <= 8):
                raise AssertionError(f"{employee['key']}: required_inputs outside 4..8")
            if len(contract["evidence_required"]) < 2 or len(contract["workflow"]) < 4:
                raise AssertionError(f"{employee['key']}: incomplete provenance workflow")
            if len(contract["outputs"]) < 2 or len(contract["success_metrics"]) != 3:
                raise AssertionError(f"{employee['key']}: incomplete outputs/metrics")
            if contract["requires_human_approval"] is not True or contract["allowed_side_effects"] != []:
                raise AssertionError(f"{employee['key']}: unsafe side-effect contract")
            for metric in contract["success_metrics"]:
                if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", metric["key"]):
                    raise AssertionError(f"{metric['key']}: invalid metric key")
                for required in contract["required_inputs"][:2]:
                    if required not in metric["source"]:
                        raise AssertionError(f"{metric['key']}: metric not evidence-bound")
                all_metrics.append(metric["key"])
            all_ids.append(employee["idx"])
            all_keys.append(employee["key"])
            all_names.append(employee["name"])
            all_people.append(employee["person"])
            all_decisions.append(contract["decision"])
            input_fingerprints.append(tuple(contract["required_inputs"]))
        if len(input_fingerprints) != len(set(input_fingerprints)):
            raise AssertionError(f"{key}: duplicate required_inputs fingerprint")
    for label, values in (
        ("idx", all_ids), ("key", all_keys), ("name", all_names),
        ("person", all_people), ("decision", all_decisions), ("metric", all_metrics),
    ):
        if len(values) != len(set(values)):
            duplicates = sorted({value for value in values if values.count(value) > 1})
            raise AssertionError(f"duplicate {label}: {duplicates[:5]}")
    if len(all_ids) != 180 or len(all_metrics) != 540:
        raise AssertionError("unexpected corpus cardinality")


def main() -> None:
    raise SystemExit(
        "DEPRECATED: use generate_industry_decisions_v3_hotel_fitness.py and "
        "generate_industry_decisions_v3_auto_beauty_pet.py; no files changed"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true",
        help="validate that checked-in JSON exactly matches deterministic generation",
    )
    args = parser.parse_args()
    catalogs = [build_catalog(spec, index) for index, spec in enumerate(SPECS)]
    validate(catalogs)
    if not args.check:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
    for catalog in catalogs:
        path = OUT_DIR / f"{catalog['key']}.json"
        payload = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != payload:
                raise SystemExit(f"OUT-OF-DATE {path}")
        else:
            path.write_text(payload, encoding="utf-8")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        print(f"{catalog['key']}: 36 employees sha256={digest}")
    print("validated: 5 industries / 180 active employees / 540 structured metrics")


if __name__ == "__main__":
    main()
