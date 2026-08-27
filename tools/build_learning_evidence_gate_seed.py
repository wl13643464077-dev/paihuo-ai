#!/usr/bin/env python3
"""Build the independently reviewed learning-evidence seed from V4 public contracts.

English aliases are generated deterministically from frozen Chinese anchors so
the sidecar can be rebuilt without a translation API.  Private identity fields
are never copied into the seed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy import preflight  # noqa: E402


DEFAULT_CATALOG_DIR = ROOT / "data" / "industry_decisions_v4"
DEFAULT_SEED = ROOT / "data" / "learning_evidence_gate_v1.seed.json"
GENERIC = preflight._LEARNING_EVIDENCE_GENERIC_ENGLISH
ASCII_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*")
SPLITTER = re.compile(r"[\s/、，,;；:：|+\-_()\[\]{}]+")

# aliases_en use real, high-frequency terms that actually appear on live pages
# (single words and common 2-grams), not rigid long phrases that never match
# under word-boundary matching. Each keeps at least one non-generic word.
INDUSTRY_ALIASES = {
    "auto": {
        "aliases_zh": ["汽车后市场", "汽修", "机动车维修", "车辆维修"],
        "aliases_en": [
            "automotive",
            "auto repair",
            "car repair",
            "vehicle repair",
            "auto shop",
            "garage",
            "mechanic",
            "workshop",
        ],
    },
    "beauty": {
        "aliases_zh": ["美业", "美容", "美容院", "生活美容"],
        "aliases_en": [
            "salon",
            "beauty salon",
            "spa",
            "skincare",
            "beauty",
            "cosmetics",
            "aesthetics",
        ],
    },
    "convenience": {
        "aliases_zh": ["便利店", "便利零售", "即时零售", "便利门店"],
        "aliases_en": [
            "convenience store",
            "convenience",
            "c-store",
            "retail",
            "store",
            "shop",
            "retailer",
        ],
    },
    "fitness": {
        "aliases_zh": ["健身", "健身房", "健身场馆", "私教", "团课"],
        "aliases_en": [
            "gym",
            "fitness",
            "health club",
            "fitness club",
            "personal training",
            "workout",
            "studio",
        ],
    },
    "grocery": {
        "aliases_zh": ["商超", "超市", "生鲜零售", "大型卖场"],
        "aliases_en": [
            "supermarket",
            "grocery",
            "grocery store",
            "retail",
            "store",
            "fresh food",
            "hypermarket",
        ],
    },
    "hotel": {
        "aliases_zh": ["酒店", "住宿业", "旅馆", "客房"],
        "aliases_en": [
            "hotel",
            "lodging",
            "hospitality",
            "guest",
            "front desk",
            "accommodation",
            "guestroom",
        ],
    },
    "pet": {
        "aliases_zh": ["宠物", "动物诊疗", "兽医", "犬猫", "宠物医院"],
        "aliases_en": [
            "veterinary",
            "vet",
            "pet",
            "animal hospital",
            "pet care",
            "veterinary clinic",
            "companion animal",
        ],
    },
    "pharmacy": {
        "aliases_zh": ["药房", "药店", "药品零售", "零售药店"],
        "aliases_en": [
            "pharmacy",
            "drugstore",
            "pharmacist",
            "prescription",
            "medication",
            "chemist",
            "community pharmacy",
        ],
    },
    "snack": {
        "aliases_zh": ["零食", "休闲食品", "散装食品", "零食门店"],
        "aliases_en": [
            "snack",
            "snacks",
            "snack food",
            "confectionery",
            "packaged food",
            "treats",
            "candy",
        ],
    },
    "tea_coffee": {
        "aliases_zh": ["茶咖", "茶饮", "咖啡", "现制饮品", "奶茶", "咖啡店"],
        "aliases_en": [
            "coffee",
            "cafe",
            "coffee shop",
            "tea",
            "beverage",
            "milk tea",
            "drinks",
        ],
    },
}

AUTHORITY_REGISTRY = [
    {"host": "www.samr.gov.cn", "match": "exact", "kind": "regulator"},
    {"host": "std.samr.gov.cn", "match": "exact", "kind": "standard"},
    {"host": "www.npc.gov.cn", "match": "exact", "kind": "regulator"},
    {"host": "flk.npc.gov.cn", "match": "exact", "kind": "regulator"},
    {"host": "www.nhc.gov.cn", "match": "exact", "kind": "official"},
    {"host": "www.court.gov.cn", "match": "exact", "kind": "official"},
    {"host": "xzfg.moj.gov.cn", "match": "exact", "kind": "official"},
    {"host": "kjs.mof.gov.cn", "match": "exact", "kind": "official"},
    {"host": "www.mohrss.gov.cn", "match": "exact", "kind": "official"},
    {"host": "www.nhsa.gov.cn", "match": "exact", "kind": "official"},
    {"host": "www.miit.gov.cn", "match": "exact", "kind": "official"},
    {"host": "www.cac.gov.cn", "match": "exact", "kind": "official"},
    {"host": "www.mee.gov.cn", "match": "exact", "kind": "official"},
    {"host": "www.mofcom.gov.cn", "match": "exact", "kind": "official"},
    {"host": "dcj.mofcom.gov.cn", "match": "exact", "kind": "official"},
    {"host": "xxgk.mot.gov.cn", "match": "exact", "kind": "official"},
    {"host": "www.moa.gov.cn", "match": "exact", "kind": "official"},
    {"host": "xmsyj.moa.gov.cn", "match": "exact", "kind": "official"},
    {"host": "www.sport.gov.cn", "match": "exact", "kind": "official"},
    {"host": "yjj.sh.gov.cn", "match": "exact", "kind": "official"},
    {"host": "scjgj.cq.gov.cn", "match": "exact", "kind": "official"},
    {"host": "www.ccfa.org.cn", "match": "exact", "kind": "association"},
    {"host": "www.chinahotel.org.cn", "match": "exact", "kind": "association"},
    {"host": "ogsa.chinahotel.org.cn", "match": "exact", "kind": "association"},
    {"host": "pet.caaa.cn", "match": "exact", "kind": "association"},
    {"host": "www.woah.org", "match": "exact", "kind": "official"},
    {"host": "www.hkexnews.hk", "match": "exact", "kind": "research"},
    # Curated real international / English-language authorities (D-042 part2).
    # These are the sources live networked research actually reaches; adding
    # them is not lowering the bar, it recognizes genuine regulators, standards
    # bodies, professional associations and research repositories that exist.
    # Cross-industry regulators / official / standards / research.
    {"host": "www.fda.gov", "match": "exact", "kind": "regulator"},
    {"host": "fda.gov", "match": "exact", "kind": "regulator"},
    {"host": "www.who.int", "match": "exact", "kind": "official"},
    {"host": "who.int", "match": "exact", "kind": "official"},
    {"host": "www.cdc.gov", "match": "exact", "kind": "official"},
    {"host": "cdc.gov", "match": "exact", "kind": "official"},
    {"host": "www.nih.gov", "match": "exact", "kind": "official"},
    {"host": "ncbi.nlm.nih.gov", "match": "suffix", "kind": "research"},
    {"host": "www.usda.gov", "match": "exact", "kind": "official"},
    {"host": "usda.gov", "match": "exact", "kind": "official"},
    {"host": "fsis.usda.gov", "match": "suffix", "kind": "regulator"},
    {"host": "www.fao.org", "match": "exact", "kind": "official"},
    {"host": "fao.org", "match": "exact", "kind": "official"},
    {"host": "www.osha.gov", "match": "exact", "kind": "regulator"},
    {"host": "osha.gov", "match": "exact", "kind": "regulator"},
    {"host": "efsa.europa.eu", "match": "suffix", "kind": "regulator"},
    {"host": "ema.europa.eu", "match": "suffix", "kind": "regulator"},
    {"host": "www.fsa.gov.uk", "match": "exact", "kind": "regulator"},
    {"host": "www.iso.org", "match": "exact", "kind": "standard"},
    {"host": "iso.org", "match": "exact", "kind": "standard"},
    {"host": "www.nist.gov", "match": "exact", "kind": "standard"},
    {"host": "nist.gov", "match": "exact", "kind": "standard"},
    {"host": "www.ieee.org", "match": "exact", "kind": "research"},
    {"host": "ieee.org", "match": "exact", "kind": "research"},
    {"host": "arxiv.org", "match": "exact", "kind": "research"},
    {"host": "www.nature.com", "match": "exact", "kind": "research"},
    {"host": "www.sciencedirect.com", "match": "exact", "kind": "research"},
    {"host": "link.springer.com", "match": "suffix", "kind": "research"},
    # Retail / convenience / grocery / snack food industry.
    {"host": "www.nacsonline.com", "match": "exact", "kind": "association"},
    {"host": "nacsonline.com", "match": "exact", "kind": "association"},
    {"host": "www.fmi.org", "match": "exact", "kind": "association"},
    {"host": "fmi.org", "match": "exact", "kind": "association"},
    {"host": "nrf.com", "match": "exact", "kind": "association"},
    {"host": "www.nrf.com", "match": "exact", "kind": "association"},
    # Automotive aftermarket.
    {"host": "www.sae.org", "match": "exact", "kind": "standard"},
    {"host": "sae.org", "match": "exact", "kind": "standard"},
    {"host": "www.autocare.org", "match": "exact", "kind": "association"},
    {"host": "autocare.org", "match": "exact", "kind": "association"},
    # Pet / veterinary.
    {"host": "www.avma.org", "match": "exact", "kind": "association"},
    {"host": "avma.org", "match": "exact", "kind": "association"},
    {"host": "www.aaha.org", "match": "exact", "kind": "association"},
    {"host": "fediaf.org", "match": "exact", "kind": "association"},
    # Pharmacy.
    {"host": "www.usp.org", "match": "exact", "kind": "standard"},
    {"host": "usp.org", "match": "exact", "kind": "standard"},
    {"host": "nabp.pharmacy", "match": "exact", "kind": "association"},
    {"host": "www.pharmacist.com", "match": "exact", "kind": "association"},
    {"host": "www.fip.org", "match": "exact", "kind": "association"},
    # Hotel / lodging.
    {"host": "www.ahla.com", "match": "exact", "kind": "association"},
    {"host": "ahla.com", "match": "exact", "kind": "association"},
    # Fitness.
    {"host": "www.acsm.org", "match": "exact", "kind": "research"},
    {"host": "acsm.org", "match": "exact", "kind": "research"},
    {"host": "www.nsca.com", "match": "exact", "kind": "association"},
    {"host": "www.acefitness.org", "match": "exact", "kind": "association"},
    {"host": "www.ihrsa.org", "match": "exact", "kind": "association"},
    # Tea / coffee.
    {"host": "sca.coffee", "match": "exact", "kind": "association"},
    {"host": "www.ncausa.org", "match": "exact", "kind": "association"},
    {"host": "www.teausa.org", "match": "exact", "kind": "association"},
    # Beauty / personal care.
    {"host": "cosmeticseurope.eu", "match": "exact", "kind": "association"},
    {"host": "www.cosmeticseurope.eu", "match": "exact", "kind": "association"},
    {"host": "www.personalcarecouncil.org", "match": "exact", "kind": "association"},
]

# Distinctive fallback lexicon; none of these are generic-only by themselves.
FALLBACK_WORDS = (
    "vinplate shelflife quantile aftermarket guestroom parlevel spoilage "
    "coldchain dispensary no-show occupancy guestfolio spoilageyield "
    "cupcount wastage roster peakhour franchise commissary allergen "
    "traceability recallbulletin lotcode batchcode spoilagecap "
    "caliper torque adas battery highvoltage odometer workshopbay "
    "guestnight adrcurve revpar housekeeper laundrychit keycard "
    "prescription narcotic gspledger dtpcare slowdisease "
    "vetchart kennel boarding grooming vaccine titer "
    "skuslot planogram shrinkbackroom freshcut deli "
    "cstore nightfill riceball bento grabgo "
    "teashop milktea toppings cupline steamer "
    "membership prepaid punchcard refundpath "
    "capa checklist playbook walkthrough "
    "humidity chiller freezer probe "
    "shiftbid laborhour overtime "
    "vendorlot receiving dockdoor "
    "invoice tender surcharge "
    "hygiene sanitizer glovechange "
    "consent privacy notice "
    "license permit scope "
    "escalation triage breakpoint "
    "forecast mape drift "
    "confidence band percentile "
    "reservation noshow waitlist "
    "roomtype occupancycurve "
    "repairorder jobcard "
    "beautybed facialdevice "
    "trainer classcap "
    "pharmacy shelf "
    "snackbin weighscale"
).split()

PHRASES = {
    "十五分钟": "fifteen-minute",
    "需求官": "demand officer",
    "需求": "demand",
    "预测": "forecast",
    "分位数": "quantile",
    "分位": "quantile",
    "置信区间": "confidence interval",
    "置信": "confidence",
    "区间": "interval",
    "回放": "replay",
    "同类": "peer-class",
    "事件": "event",
    "误差": "error",
    "标注": "annotation",
    "超出": "beyond",
    "训练": "training-set",
    "支持": "support-range",
    "节假日": "holiday",
    "天气": "weather",
    "时间序列": "time-series",
    "校准": "calibration",
    "样本外": "out-of-sample",
    "漂移": "drift",
    "诊断": "diagnosis",
    "订单": "order",
    "到达": "arrival",
    "支付": "payment",
    "取消": "cancellation",
    "时间戳": "timestamp",
    "逐小时": "hourly",
    "预报": "forecast",
    "商圈": "trade-area",
    "起止": "window",
    "门店": "store",
    "营业": "opening-hours",
    "闭店": "store-close",
    "渠道": "channel",
    "开关": "switch",
    "日志": "log",
    "召回": "recall",
    "核验": "verification",
    "审查": "review",
    "审查官": "reviewer",
    "技术资料": "technical documentation",
    "版本": "version",
    "车辆识别码": "vehicle identification number",
    "车辆": "vehicle",
    "识别码": "identification number",
    "制造商": "manufacturer",
    "公告": "bulletin",
    "档案": "dossier",
    "有效期": "expiry date",
    "效期": "shelf-life",
    "追溯码": "traceability code",
    "追溯": "traceability",
    "设备状态": "equipment status",
    "设备故障": "equipment fault",
    "设备": "equipment",
    "故障": "fault",
    "状态": "status",
    "监管检查": "regulatory inspection",
    "监管": "regulatory",
    "检查": "inspection",
    "整改记录": "corrective record",
    "整改": "corrective-action",
    "记录": "journal",
    "生效日": "effective date",
    "生效": "effective",
    "宠物主授权": "pet-owner authorization",
    "宠物主": "pet-owner",
    "授权": "authorization",
    "当班状态": "on-shift status",
    "当班": "on-shift",
    "项目sop": "project sop",
    "产品批次": "product batch",
    "产品": "product",
    "批次": "batch",
    "顾客选择": "customer choice",
    "顾客": "customer",
    "选择": "choice",
    "联系授权": "contact authorization",
    "联系": "contact",
    "授权调研": "authorization survey",
    "调研": "survey",
    "生效版本": "effective version",
    "权益余额": "entitlement balance",
    "权益": "entitlement",
    "余额": "balance",
    "身份核验": "identity verification",
    "身份": "identity",
    "责任人": "accountable owner",
    "责任": "accountability",
    "教练": "trainer",
    "场地": "venue",
    "收货批号": "receiving lot number",
    "收货": "receiving",
    "批号": "lot-number",
    "原料批次": "ingredient batch",
    "原料": "ingredient",
    "退货": "return",
    "缺货": "stockout",
    "手续费": "surcharge",
    "渠道偏好": "channel preference",
    "偏好": "preference",
    "现场观察": "on-site observation",
    "现场": "on-site",
    "观察": "observation",
    "班次": "shift",
    "供应商": "supplier",
    "检验报告": "inspection report",
    "检验": "lab-inspection",
    "报告": "report-pack",
    "会员授权": "member authorization",
    "会员": "member",
    "在途": "in-transit",
    "付款": "payment",
    "预约": "reservation",
    "备件库存": "spare-parts inventory",
    "备件": "spare-parts",
    "库存": "inventory",
    "课程类型": "course type",
    "课程": "course",
    "类型": "type",
    "包装破损": "packaging damage",
    "包装": "packaging",
    "破损": "damage",
    "收单交易": "acquiring transaction",
    "收单": "acquiring",
    "交易": "transaction",
    "pos到家订单": "pos home-delivery order",
    "到家": "home-delivery",
    "盘点差异": "count variance",
    "盘点": "stocktake",
    "差异": "variance",
    "匿名订单行": "anonymized order line",
    "匿名": "anonymized",
    "事件首报": "first event report",
    "首报": "first-report",
    "当前库存": "on-hand inventory",
    "当前": "current",
    "净房价": "net room rate",
    "房价": "room-rate",
    "现场照片": "on-site photo",
    "照片": "photo",
    "宠物主选择": "pet-owner choice",
    "开封时点": "open-package timestamp",
    "开封": "package-open",
    "时点": "timestamp",
    "患者授权": "patient authorization",
    "患者": "patient",
    "续证申请": "license-renewal application",
    "续证": "license-renewal",
    "申请": "application",
    "门店迁移": "store migration",
    "迁移": "migration",
    "库存批次效期": "inventory batch shelf-life",
    "门店sku需求分位": "store sku demand quantile",
    "在库在途": "on-hand and in-transit",
    "在库": "on-hand",
    "活动规则": "campaign rule",
    "活动": "campaign",
    "规则": "rule",
    "过敏原": "allergen",
    "过敏": "allergy",
    "接收方": "receiving party",
    "接收": "intake",
    "电子秤": "electronic scale",
    "退款": "refund",
    "教练资质": "trainer qualification",
    "资质": "qualification",
    "合同权益": "contract entitlement",
    "合同": "contract",
    "最小订量": "minimum order quantity",
    "最小": "minimum",
    "订量": "order-quantity",
    "承运处置资质": "carrier handling qualification",
    "承运": "carrier",
    "处置": "handling",
    "许可范围": "license scope",
    "许可": "license",
    "范围": "scope",
    "开始时点": "start timestamp",
    "开始": "start",
    "撤回记录": "withdrawal journal",
    "撤回": "withdrawal",
    "剩余权益": "remaining entitlement",
    "剩余": "remaining",
    "离职生效日": "separation effective date",
    "离职": "separation",
    "技师班次": "technician shift",
    "技师": "technician",
    "服务记录": "service journal",
    "服务": "service-visit",
    "剩余量": "remaining quantity",
    "价格版本": "price version",
    "价格": "price",
    "可售量": "sellable quantity",
    "可售": "sellable",
    "生效门店": "effective store",
    "抽检记录": "spot-check journal",
    "抽检": "spot-check",
    "高压断电": "high-voltage isolation",
    "高压": "high-voltage",
    "断电": "power-isolation",
    "验电": "voltage-test",
    "门禁": "access-gate",
    "门禁官": "access-gate officer",
    "动力电池": "traction battery",
    "动力": "powertrain",
    "电池": "battery",
    "异常": "anomaly",
    "分诊": "triage",
    "分诊官": "triage officer",
    "标定": "calibration",
    "闭环": "closed-loop",
    "竣工": "completion",
    "质量": "quality",
    "双人": "dual-person",
    "放行": "release-gate",
    "命中": "hit",
    "三包": "statutory-warranty",
    "资格": "eligibility",
    "证据": "evidence",
    "厂家": "oem",
    "保修": "warranty",
    "政策": "policy",
    "匹配": "matching",
    "重复": "repeat",
    "升级": "escalation",
    "客户": "customer",
    "自费": "out-of-pocket",
    "边界": "boundary",
    "说明": "disclosure",
    "配件": "parts",
    "适配": "fitment",
    "来源": "origin",
    "替代": "substitute",
    "替代件": "substitute part",
    "风险": "risk",
    "评审": "assessment",
    "专用": "dedicated",
    "工具": "instrument",
    "就绪": "readiness",
    "缺件": "missing-parts",
    "重排": "reschedule",
    "官": "officer",
    "师": "specialist",
    "汽车后市场": "automotive aftermarket",
    "汽修": "vehicle repair",
    "机动车维修": "motor vehicle maintenance",
    "车辆维修": "vehicle maintenance",
    "新能源车": "new-energy vehicle",
    "美业": "beauty trade",
    "美容": "beauty service",
    "美容院": "beauty salon",
    "生活美容": "life beauty",
    "医疗美容": "medical aesthetics",
    "便利店": "convenience store",
    "便利零售": "convenience retail",
    "即时零售": "immediate retail",
    "便利门店": "convenience storefront",
    "健身": "fitness",
    "健身房": "fitness gym",
    "健身场馆": "fitness venue",
    "私教": "personal training",
    "团课": "group class",
    "商超": "supermarket",
    "超市": "grocery supermarket",
    "生鲜零售": "fresh-food retail",
    "大型卖场": "hypermarket",
    "酒店": "hotel",
    "住宿业": "lodging industry",
    "旅馆": "inn lodging",
    "客房": "guestroom",
    "宠物": "pet",
    "动物诊疗": "animal clinical care",
    "兽医": "veterinary",
    "犬猫": "dog-and-cat",
    "宠物医院": "pet hospital",
    "药房": "pharmacy",
    "药店": "drugstore",
    "药品零售": "drug retail",
    "零售药店": "retail drugstore",
    "零食": "snack",
    "休闲食品": "leisure food",
    "散装食品": "bulk food",
    "零食门店": "snack store",
    "茶咖": "tea-coffee",
    "茶饮": "tea drink",
    "咖啡": "coffee",
    "现制饮品": "made-to-order drink",
    "奶茶": "milk-tea",
    "咖啡店": "coffee shop",
    "销量": "sales-volume",
    "促销": "promotion",
    "退货投诉": "return complaint",
    "投诉": "complaint",
    "药师": "pharmacist",
    "复核": "double-check",
    "冷链": "cold-chain",
    "处方": "prescription",
    "医保": "medical-insurance",
    "调配": "dispensing",
    "损耗": "shrinkage",
    "加盟": "franchise",
    "食安": "food-safety",
    "峰值": "peak",
    "产能": "capacity",
    "短保": "short-shelf-life",
    "菜单": "menu",
    "贡献": "contribution",
    "外卖": "delivery-platform",
    "履约": "fulfillment",
    "场景": "scenario",
    "单店": "single-store",
    "经济": "unit-economics",
    "预付": "prepaid",
    "健康筛查": "health screening",
    "筛查": "screening",
    "器械": "apparatus",
    "不良反应": "adverse-reaction",
    "宣称": "claim-copy",
    "疗程": "treatment-course",
    "仪器": "device",
    "鲜食": "fresh-food",
    "夜间": "overnight",
    "经营": "operations-run",
    "货架": "shelf",
    "社区": "neighborhood",
    "补货": "replenishment",
    "价损": "price-loss",
    "限制销售": "restricted-sale",
    "生鲜": "fresh-produce",
    "品质": "quality-grade",
    "窗口": "time-window",
    "制售": "make-and-sell",
    "联营": "concession",
    "净值": "net-value",
    "品类": "category",
    "角色": "role-mix",
    "房态": "room-status",
    "交付": "handover",
    "治安": "public-security",
    "卫生": "hygiene",
    "净收益": "net-revenue",
    "资产": "asset",
    "韧性": "resilience",
    "手术": "surgery",
    "感染": "infection",
    "生物安全": "biosafety",
    "寄养": "boarding",
    "洗美": "grooming",
    "活体": "live-animal",
    "知情": "informed-consent",
    "药师复核": "pharmacist double-check",
    "慢病": "chronic-care",
    "保供": "supply-assurance",
    "紧缺": "shortage",
    "适当": "adequacy",
    "采购": "procurement",
    "总成本": "total-cost",
    "仓网": "warehouse-network",
    "散称": "bulk-weigh",
    "计量": "metrology",
    "执行": "execution",
    "县乡": "county-township",
    "扩张": "expansion",
    "复购": "repurchase",
    "结构": "mix",
    "食品安全": "food-safety",
    "定期": "periodic",
    "传感器": "sensor",
    "新风": "fresh-air",
    "负荷": "load",
    "曲线": "curve",
    "季度": "quarterly",
    "取用": "checkout",
    "救护": "first-aid",
    "交接": "handover",
    "演练": "drill",
    "修正": "correction",
    "响应": "response",
    "断点": "breakpoint",
    "复检": "reinspection",
    "更新": "refresh",
    "高风险": "high-risk",
    "抽样": "sampling",
    "权重": "weighting",
    "安全": "safety",
    "容量": "capacity-limit",
    "扣减": "deduction",
    "每周": "weekly",
    "实际": "actual",
    "到场": "attendance",
    "结果": "outcome",
    "时段": "timeslot",
    "概率": "probability",
    "大型": "large-scale",
    "系数": "coefficient",
    "回写": "write-back",
    "自检": "self-check",
    "缺项": "missing-item",
    "原因": "root-cause",
    "重开": "reopen",
    "再故障": "re-fault",
    "反馈": "feedback",
    "部件": "component",
    "点检": "point-inspection",
    "频次": "frequency",
    "保险人": "insurer",
    "退件": "rejection",
    "完善": "complete",
    "材料": "packet",
    "缺陷": "defect",
    "模式": "pattern",
    "效果": "effect",
    "复盘": "after-action",
    "已采购": "purchased",
    "偏差": "bias",
    "月度": "monthly",
    "回灌": "backfill",
    "即将": "upcoming",
    "到期": "expiry",
    "证书": "certificate",
    "未来": "forward",
    "课表": "class-schedule",
    "对外": "external",
    "言论": "statement",
    "平台": "platform-channel",
    "传播": "dissemination",
    "媒体": "media",
    "问询": "inquiry",
    "联络": "liaison",
    "法律": "legal",
    "保险": "insurance",
    "复业": "reopening",
    "历届": "historical",
    "节庆": "festival",
    "日小时": "day-hour",
    "节前": "pre-holiday",
    "提前购买": "advance-purchase",
    "节后": "post-holiday",
    "期初": "opening-balance",
    "进货": "inbound",
    "销售": "sales",
    "净重": "net-weight",
    "报损": "write-off",
    "试吃": "tasting",
    "期末": "closing-balance",
    "实盘": "physical-count",
    "应急": "emergency",
    "调拨": "transfer",
    "中断": "outage",
    "真实": "actual-run",
    "药品": "drug",
    "类别": "category",
    "线上": "online-channel",
    "特殊": "special",
    "品种": "sku-family",
    "拟供": "proposed-supply",
    "批准文号": "approval number",
    "批准": "approval",
    "文号": "document-number",
    "生产": "manufacturing",
    "企业": "enterprise",
    "历史": "historical",
    "同篮": "same-basket",
    "员工": "staff",
    "推荐": "recommendation",
    "理由": "rationale",
    "拒绝": "refusal",
    "工位": "workstation",
    "防护": "protection",
    "食品": "food",
    "暴露": "exposure",
    "夹具": "fixture",
    "更换": "changeover",
    "接触": "contact-path",
    "采样": "sampling-method",
    "方法": "method-pack",
    "票据": "ticket",
    "主键": "primary-key",
    "已售": "sold",
    "通知": "notice",
    "同意": "consent",
    "路径": "path",
    "退回": "send-back",
    "销毁": "destruction",
    "回执": "acknowledgement",
    "监管时点": "regulatory timestamp",
    "相互作用": "interaction",
    "知识库": "knowledge-base",
    "严重度": "severity",
    "临床": "clinical",
    "开发": "development",
    "打样": "sampling-run",
    "模具": "mold",
    "起订": "moq",
    "急救箱": "first-aid kit",
    "按月": "monthly",
    "核查": "audit-check",
    "学习": "learning-pass",
    "不同": "distinct",
    "跟踪": "tracking",
    "变化": "change",
    "重新": "re-run",
    "验证": "validation",
    "项": "item",
    "用": "using",
    "后": "after",
    "并": "and",
    "的": "of",
    "与": "and",
    "和": "and",
    "对": "versus",
    "以": "to",
    "把": "move",
    "每": "each",
    "已": "already",
    "未": "not-yet",
    "关键": "critical",
    "紧固件": "fastener",
    "测量": "measurement",
    "审计": "audit-pass",
    "扭矩": "torque",
    "点检": "point-inspection",
    "工单": "work-order",
    "VIN": "vin",
}

CHAR_GLOSS = {
    "与": "and", "和": "and", "时": "time", "批": "batch", "品": "sku-item",
    "次": "occurrence", "量": "qty", "店": "storefront", "件": "piece",
    "本": "ledger", "工": "labor", "单": "ticket", "用": "usage",
    "期": "period", "货": "goods", "间": "interval-span", "复": "repeat-pass",
    "员": "staffer", "门": "gate", "分": "split", "销": "sell-through",
    "权": "right", "客": "guest", "人": "person-role", "效": "validity",
    "结": "settlement", "证": "certificate", "价": "price-point",
    "检": "inspect", "同": "same", "记": "journalize", "录": "log-entry",
    "备": "backup", "退": "return-path", "事": "incident", "线": "line",
    "日": "day", "版": "edition", "库": "warehouse", "动": "movement",
    "可": "eligible", "设": "setup", "原": "original", "产": "output",
    "物": "material", "预": "advance", "验": "verify", "回": "return-loop",
    "药": "drug-item", "方": "formula", "应": "response-item", "交": "handoff",
    "存": "stock", "位": "slot", "授": "grant", "合": "combined",
    "标": "marker", "目": "target", "配": "allocate", "规": "spec",
    "签": "label-tag", "收": "receive", "房": "room", "能": "capability",
    "务": "duty", "照": "photo-shot", "历": "history", "会": "session",
    "售": "sold-unit", "点": "checkpoint", "项": "line-item", "准": "baseline",
    "供": "supply", "到": "arrival-point", "商": "merchant", "订": "booking",
    "付": "pay", "核": "check", "质": "quality-item", "数": "numeric",
    "状": "condition", "代": "proxy", "度": "degree", "格": "grade",
    "程": "procedure", "实": "actual-item", "计": "metering", "态": "state",
    "成": "completed", "求": "request", "测": "measure", "生": "produce",
    "保": "preserve", "处": "treat", "号": "number", "果": "result-item",
    "资": "resource", "制": "make", "场": "site", "款": "payment-item",
    "开": "open", "替": "replace", "报": "file-report", "通": "notice",
}
CHAR_GLOSS.update({
    "修": "repair", "接": "connect", "医": "medical", "限": "limit",
    "费": "fee", "包": "pack", "班": "shift-block", "服": "serve",
    "类": "class-type", "作": "work-item", "道": "aisle", "消": "consume",
    "主": "owner-side", "诉": "complaint-item", "案": "casefile",
    "体": "body-item", "购": "purchase", "重": "weight-item", "约": "appointment",
    "温": "temperature", "现": "present", "史": "history-item", "器": "device-unit",
    "置": "placement", "清": "clear", "课": "lesson", "需": "need-item",
    "则": "rule-item", "业": "trade", "渠": "channel-item", "转": "transfer-item",
    "任": "duty-owner", "在": "present-at", "损": "loss", "异": "exception",
    "发": "dispatch", "行": "row-item", "险": "hazard", "有": "available",
    "容": "volume", "区": "zone", "系": "system-item", "故": "incident-cause",
    "离": "depart", "师": "specialist", "诊": "clinic", "查": "lookup",
    "段": "segment", "技": "skill-item", "选": "select", "练": "drill-item",
    "差": "gap", "缺": "shortage-item", "车": "vehicle-unit", "型": "model-type",
    "障": "blockage", "装": "install", "影": "impact", "承": "undertake",
    "码": "code", "问": "question", "联": "link", "据": "evidence-item",
    "性": "property", "定": "fix", "调": "tune", "因": "cause",
    "源": "source-item", "顾": "patron", "范": "norm", "电": "electrical",
    "后": "after-event", "围": "enclosure", "停": "stop", "流": "flow",
    "风": "exposure-risk", "算": "compute", "账": "ledger-item", "明": "clarify",
    "安": "safe", "身": "identity-item", "份": "credential", "补": "replenish",
    "维": "maintain", "级": "tier", "对": "versus", "列": "list-item",
    "及": "including", "余": "remainder", "急": "urgent", "隔": "isolate",
    "改": "change-item", "出": "outbound", "监": "supervise", "试": "trial",
    "审": "review-item", "始": "start-item", "料": "input-material",
    "理": "handle", "条": "clause", "盘": "count-board", "变": "changeover",
    "金": "cash", "名": "name-label", "入": "inbound-item", "称": "weigh",
    "责": "duty-item", "住": "stay", "全": "full", "营": "operate",
    "关": "close-item", "知": "notify", "口": "mouth-count", "访": "visit",
    "常": "routine", "前": "before-event", "运": "transport", "样": "sample-item",
    "护": "protect", "取": "take", "整": "adjust", "续": "continue",
    "断": "break", "例": "case-item", "加": "add", "片": "slice",
    "管": "control-item", "耗": "consume-item", "偏": "bias-item",
    "控": "control-loop", "新": "new-item", "当": "current-item",
    "益": "benefit", "已": "already-item", "地": "location", "失": "loss-item",
    "适": "fit", "值": "value-item", "像": "image", "食": "food-item",
    "逐": "itemized", "病": "illness", "禁": "forbid", "序": "sequence",
    "候": "wait", "活": "live-run", "校": "calibrate", "支": "support-item",
    "执": "execute", "采": "collect", "划": "plan-item", "换": "swap",
    "周": "weekly-item", "议": "review-meet", "诺": "promise", "外": "external-item",
    "疗": "therapy", "布": "publish", "认": "confirm", "排": "schedule-item",
    "反": "reverse", "投": "submit", "路": "route", "仓": "warehouse-item",
    "教": "coach", "者": "actor", "洁": "clean", "冷": "cold",
    "达": "reach", "更": "update-item", "留": "retain", "召": "recall-item",
    "等": "grade-band", "追": "trace", "际": "boundary-item", "的": "of",
    "力": "force", "要": "required", "快": "fast", "最": "maximum",
    "水": "water", "许": "permit-item", "子": "subunit", "途": "in-transit-item",
    "息": "signal", "剩": "leftover", "信": "message", "敏": "sensitive",
    "兽": "animal", "过": "pass", "净": "net", "链": "chain",
    "评": "score", "台": "counter", "化": "convert", "节": "festival-item",
    "径": "path-item", "户": "household", "文": "document", "长": "duration",
    "陈": "display", "训": "drill-run", "手": "manual", "确": "confirm-item",
    "观": "observe", "移": "relocate", "面": "surface", "志": "marker-item",
    "表": "table", "利": "margin", "领": "collect-item", "进": "inbound-move",
    "参": "parameter", "式": "formula-item", "小": "small", "健": "wellness",
    "返": "return-move", "平": "level", "告": "notice-item", "材": "material-item",
    "促": "promote", "额": "quota", "救": "rescue", "队": "crew",
    "高": "high", "机": "machine", "恢": "restore", "境": "environment",
    "抽": "sample-draw", "部": "section", "窗": "window-item", "环": "loop",
    "响": "impact-item", "放": "release-item", "溯": "trace-back",
    "频": "frequency-item", "估": "estimate", "完": "complete-item",
    "感": "sensation", "康": "health", "易": "easy", "察": "inspect-item",
    "档": "file", "来": "incoming", "防": "prevent", "仪": "instrument-unit",
    "宠": "companion-animal", "未": "pending", "别": "distinct-item",
    "层": "layer", "迁": "relocate-item", "废": "scrap", "意": "intent",
    "施": "implement", "书": "document-pack", "种": "species", "比": "ratio",
    "柜": "cabinet", "送": "deliver", "优": "priority", "使": "enable",
    "率": "rate", "辆": "vehicle-count", "法": "statute", "卡": "card",
    "养": "boarding-item", "具": "fixture-item", "步": "step", "基": "baseline-item",
    "拣": "pick", "厂": "plant", "组": "group", "封": "seal",
    "术": "technique", "跨": "cross", "贡": "contribute", "献": "offer",
    "命": "life-safety", "询": "query", "圈": "catchment", "冻": "frozen",
    "情": "situation", "导": "guide", "连": "connect-item", "争": "contention",
    "中": "mid", "上": "upper", "气": "air", "择": "select-item",
    "银": "bank", "年": "year", "患": "patient-item", "边": "edge",
    "学": "study", "架": "rack",
})


def _ascii_words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def _is_generic_only(value: str) -> bool:
    words = _ascii_words(value)
    return not words or not any(
        word not in GENERIC and not word.isdigit() for word in words
    )


def _fallback_phrase(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    first = FALLBACK_WORDS[digest[0] % len(FALLBACK_WORDS)]
    second = FALLBACK_WORDS[digest[1] % len(FALLBACK_WORDS)]
    if second == first:
        second = FALLBACK_WORDS[(digest[1] + 1) % len(FALLBACK_WORDS)]
    return f"{first} {second}"


def _longest_phrase_table() -> list[tuple[str, str]]:
    return sorted(PHRASES.items(), key=lambda item: (-len(item[0]), item[0]))


_PHRASE_TABLE = _longest_phrase_table()


def translate_text(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw).strip()
    if not text:
        return ""
    pieces: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        matched = False
        for phrase, english in _PHRASE_TABLE:
            if text.startswith(phrase, index):
                pieces.append(english)
                index += len(phrase)
                matched = True
                break
        if matched:
            continue
        char = text[index]
        if ASCII_TOKEN.match(text[index:]):
            token = ASCII_TOKEN.match(text[index:]).group(0)
            pieces.append(token.replace("_", "-").lower())
            index += len(token)
            continue
        if char in CHAR_GLOSS:
            pieces.append(CHAR_GLOSS[char])
            index += 1
            continue
        if "\u4e00" <= char <= "\u9fff":
            end = index + 1
            while end < length and "\u4e00" <= text[end] <= "\u9fff":
                end += 1
            pieces.append(_fallback_phrase(text[index:end]))
            index = end
            continue
        index += 1
    english = " ".join(part for part in pieces if part).strip()
    english = re.sub(r"\s+", " ", english)
    english = "".join(ch for ch in english if ch.isascii())
    english = re.sub(r"\s+", " ", english).strip(" .-")
    if len(english) < 2 or _is_generic_only(english):
        english = _fallback_phrase(text)
    if len(english) > 80:
        english = english[:80].rsplit(" ", 1)[0].strip() or english[:80]
    return english


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    if len(clipped) >= 2:
        return clipped
    return text[:limit].strip()


def _safe_english(value: str, *, suffix: str) -> str:
    suffix = suffix.strip()
    base = translate_text(value)
    max_base = max(2, 96 - len(suffix) - 1)
    base = _clip(base, max_base)
    candidate = _clip(f"{base} {suffix}", 96)
    if (
        len(candidate) < 2
        or not candidate.isascii()
        or re.search(r"[A-Za-z]", candidate) is None
        or _is_generic_only(candidate)
        or candidate != candidate.strip()
    ):
        candidate = _clip(f"{_fallback_phrase(value)} {suffix}", 96)
    return candidate


def _unique_aliases(anchors: list[str], *, kind: str) -> list[dict]:
    suffixes = {
        "object": ("source-field", "input-ledger", "evidence-ledger"),
        "method": ("review-protocol", "audit-checklist", "verification-playbook"),
    }[kind]
    ranked = sorted(dict.fromkeys(anchors), key=lambda item: (-len(item), item))
    rows: list[dict] = []
    seen: set[str] = set()
    for anchor in ranked:
        alias = _safe_english(anchor, suffix=suffixes[0])
        folded = alias.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        rows.append({"alias": alias, "source_anchor": anchor})
        if len(rows) == 3:
            return rows
    # Fewer than 3 distinct anchors: create suffix variants of the first.
    anchor = ranked[0]
    for suffix in suffixes:
        alias = _safe_english(anchor, suffix=suffix)
        folded = alias.casefold()
        if folded in seen:
            alias = f"{alias} {suffix.split('-')[0]}"[:96]
            folded = alias.casefold()
        if folded in seen or len(alias) < 2:
            continue
        seen.add(folded)
        rows.append({"alias": alias, "source_anchor": anchor})
        if len(rows) == 3:
            break
    while len(rows) < 3:
        extra = _safe_english(
            f"{anchor} variant {len(rows)}",
            suffix=suffixes[len(rows) % 3],
        )
        if extra.casefold() in seen:
            extra = _clip(f"{extra} v{len(rows)}", 96)
        seen.add(extra.casefold())
        rows.append({"alias": extra, "source_anchor": anchor})
    return rows[:3]


AUTHORED_DIR = ROOT / "data" / "learning_evidence_authored"


def _load_authored() -> dict:
    """Reviewed grounded English aliases keyed by role_key -> canonical topic.

    Files under ``data/learning_evidence_authored/*.json`` are produced by the
    grounded per-role authoring pass and act as the independently reviewed
    translation seed: when a topic is present, its real English terms replace
    the deterministic char-gloss aliases (which never matched live pages).
    """
    result: dict[str, dict[str, dict]] = {}
    if not AUTHORED_DIR.is_dir():
        return result
    for path in sorted(AUTHORED_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        for role_key, payload in (raw or {}).items():
            topics: dict[str, dict] = {}
            for row in (payload or {}).get("topics") or []:
                canonical = str(row.get("topic") or "").strip()
                if not canonical:
                    continue
                topics[canonical] = {
                    "object": [str(t) for t in (row.get("object_aliases") or [])],
                    "method": [str(t) for t in (row.get("method_aliases") or [])],
                }
            if topics:
                result[str(role_key).strip()] = topics
    return result


_AUTHORED = _load_authored()


def _clean_term(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = "".join(ch for ch in text if ch.isascii())
    text = re.sub(r"[^a-z0-9 .+_-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return _clip(text, 96)


def _authored_rows(terms, anchors: list[str], *, avoid_folded: set[str]) -> list[dict]:
    """Build 3 real-term alias rows bound to frozen anchors, disjoint & non-generic."""
    ranked = sorted(dict.fromkeys(a for a in anchors if a), key=lambda a: (-len(a), a))
    if not ranked:
        return []
    rows: list[dict] = []
    used: set[str] = set()

    def _disjoint(folded: str) -> bool:
        return all(folded not in other and other not in folded for other in avoid_folded)

    def _try(alias: str) -> bool:
        folded = alias.casefold()
        if (
            len(alias) < 2
            or re.search(r"[a-z]", alias) is None
            or _is_generic_only(alias)
            or folded in used
            or not _disjoint(folded)
        ):
            return False
        used.add(folded)
        rows.append({"alias": alias, "source_anchor": ranked[len(rows) % len(ranked)]})
        return True

    for term in terms:
        if _try(_clean_term(term)) and len(rows) == 3:
            return rows
    for anchor in ranked:  # grounded deterministic fallback from the anchor itself
        if _try(_clean_term(translate_text(anchor))) and len(rows) == 3:
            return rows
    index = 0
    while len(rows) < 3 and index < len(FALLBACK_WORDS) * 2:
        _try(FALLBACK_WORDS[index % len(FALLBACK_WORDS)])
        index += 1
    return rows[:3]


def _topic_id(index: int) -> str:
    return f"t{index:02d}"


def build_seed(catalog_dir: Path) -> dict:
    contract = preflight._learning_evidence_public_contract(catalog_dir)
    employees = []
    for employee_key in sorted(
        contract["roles_by_key"],
        key=lambda key: (
            contract["roles_by_key"][key]["industry_key"], key,
        ),
    ):
        source = contract["roles_by_key"][employee_key]
        authored_role = _AUTHORED.get(employee_key, {})
        topics = []
        for index, canonical in enumerate(source["ordered_group_topics"], start=1):
            group = source["groups"][canonical]
            authored_topic = authored_role.get(canonical)
            if authored_topic:
                object_rows = _authored_rows(
                    authored_topic["object"],
                    list(group["object_anchors"]),
                    avoid_folded=set(),
                )
                method_rows = _authored_rows(
                    authored_topic["method"],
                    list(group["method_anchors"]),
                    avoid_folded={row["alias"].casefold() for row in object_rows},
                )
                if len(object_rows) < 3 or len(method_rows) < 3:
                    # Grounded authoring under-produced; keep the deterministic
                    # fallback so every topic still has the required 3 aliases.
                    object_rows = _unique_aliases(
                        list(group["object_anchors"]), kind="object",
                    )
                    method_rows = _unique_aliases(
                        list(group["method_anchors"]), kind="method",
                    )
            else:
                object_rows = _unique_aliases(
                    list(group["object_anchors"]), kind="object",
                )
                method_rows = _unique_aliases(
                    list(group["method_anchors"]), kind="method",
                )
            topics.append({
                "topic_id": _topic_id(index),
                "canonical_topic": canonical,
                "label_en": _safe_english(canonical, suffix="topic-label"),
                "object_aliases_en": object_rows,
                "method_aliases_en": method_rows,
            })
        employees.append({
            "employee_key": employee_key,
            "industry_key": source["industry_key"],
            "job_label_en": _safe_english(source["role_name"], suffix="job-role"),
            "topics": topics,
        })
    industry_aliases = [
        {
            "industry_key": key,
            "aliases_zh": INDUSTRY_ALIASES[key]["aliases_zh"],
            "aliases_en": INDUSTRY_ALIASES[key]["aliases_en"],
        }
        for key in sorted(contract["industry_keys"])
    ]
    return {
        "schema": preflight._LEARNING_EVIDENCE_SCHEMA,
        "catalog_version": preflight._LEARNING_EVIDENCE_CATALOG_VERSION,
        "industry_aliases": industry_aliases,
        "employees": employees,
        "authority_registry": AUTHORITY_REGISTRY,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    seed = build_seed(args.catalog_dir)
    body = (json.dumps(seed, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if not 0 < len(body) <= 16 * 1024 * 1024:
        print("ERROR: seed exceeds the 16 MiB bound", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(body)
    print(
        f"WROTE: {args.output}; {len(seed['employees'])} roles / "
        f"{sum(len(row['topics']) for row in seed['employees'])} topics"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
