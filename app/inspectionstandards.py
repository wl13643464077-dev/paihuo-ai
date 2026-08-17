"""巡店标准库：通用基线、行业覆盖、采集位和经营指标口径。

这里保存的是版本化的产品数据，不是租户业务数据，因此不依赖数据库。
法定要求与经营建议分层：``mandatory`` 不允许租户关闭或降级，
``recommended`` 和 ``operations`` 可在受限字段内覆盖。

经营指标目录只定义可复算口径；``value``、``target`` 和 ``benchmark``
始终默认为 ``None``，未接入门店真实数据时绝不生成数字。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


CATALOG_VERSION = "2026.08.2"
CATALOG_AS_OF = "2026-08-10"

INDUSTRIES = (
    "auto", "beauty", "convenience", "fitness", "grocery", "hotel",
    "pet", "pharmacy", "restaurant", "snack", "tea_coffee",
)
TIERS = ("mandatory", "recommended", "operations")
SEVERITIES = ("low", "medium", "high", "critical")
OVERRIDE_LAYERS = ("tenant", "region", "branch")


class InspectionStandardError(ValueError):
    """巡店标准输入不符合合同。"""


class UnknownIndustryError(InspectionStandardError):
    """行业不在当前版本的标准库中。"""


class StandardOverrideError(InspectionStandardError):
    """租户覆盖试图越过允许边界。"""


# 注册表只收录可从政府官方站点核验的全国层面来源。属地执法口径
# 可能更严，所以检查项的 as_of 字段必须随版本保留，不宣称永久有效。
_SOURCES: dict[str, dict[str, str]] = {
    "SAMR-PRICE-2022": {
        "title": "明码标价和禁止价格欺诈规定",
        "authority": "国家市场监督管理总局",
        "effective": "2022-07-01",
        "url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/jls/art/2023/art_72865dc294a14432aff931038b02c210.html",
        "as_of": CATALOG_AS_OF,
    },
    "FIRE-LAW-2021": {
        "title": "中华人民共和国消防法",
        "authority": "全国人大常委会",
        "effective": "2021-04-29",
        "url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/bgt/art/2023/art_e9e34f0731c249a891ea9c925e17f237.html",
        "as_of": CATALOG_AS_OF,
    },
    "SAMR-FOOD-PERMIT-2023": {
        "title": "食品经营许可和备案管理办法",
        "authority": "国家市场监督管理总局",
        "effective": "2023-12-01",
        "url": "https://www.samr.gov.cn/cms_files/filemanager/1647978232/attach/20236/11c003f92242446e9be9f1ca600f7444.pdf",
        "as_of": CATALOG_AS_OF,
    },
    "SAMR-FOOD-LAW-2021": {
        "title": "中华人民共和国食品安全法（2021修正）",
        "authority": "全国人大常委会",
        "effective": "2021-04-29",
        "url": "https://www.samr.gov.cn/spxts/gzdt/art/2023/art_a54cbedacfd44ff5bd2b40504ea98649.html",
        "as_of": CATALOG_AS_OF,
    },
    "SAMR-FOOD-RESP-97": {
        "title": "食品生产经营企业落实食品安全主体责任监督管理规定（第97号令修改）",
        "authority": "国家市场监督管理总局",
        "effective": "2025-04-15",
        "url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2025/art_6892dba109e54ea894aec4340e8f7427.html",
        "as_of": CATALOG_AS_OF,
    },
    "SAMR-CATERING-CHAIN-104": {
        "title": "餐饮服务连锁企业落实食品安全主体责任监督管理规定（第104号令）",
        "authority": "国家市场监督管理总局",
        "effective": "2025-12-01",
        "url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2025/art_09105249f5984a2fb507e88cd346f20d.html",
        "as_of": CATALOG_AS_OF,
    },
    "SAMR-FOOD-SALES-CHAIN-114": {
        "title": "食品销售连锁企业落实食品安全主体责任监督管理规定（第114号令）",
        "authority": "国家市场监督管理总局",
        "effective": "2026-03-20",
        "url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2025/art_0bd44100b044475ea1404c8ab2fe3557.html",
        "as_of": CATALOG_AS_OF,
    },
    "SAMR-FOOD-OPS-2018": {
        "title": "餐饮服务食品安全操作规范",
        "authority": "国家市场监督管理总局",
        "effective": "2018-10-01",
        "url": "https://www.samr.gov.cn/zw/zfxxgk/zc/xzgfxwj/art/2023/art_2f6fadd3be844359be6a0e7fece23bcf.html",
        "as_of": CATALOG_AS_OF,
    },
    "NHC-GB31654-2021": {
        "title": "GB 31654-2021 食品安全国家标准 餐饮服务通用卫生规范",
        "authority": "国家卫生健康委员会",
        "effective": "2022-02-22",
        "url": "https://www.nhc.gov.cn/sps/c100087/202109/621ad0d6bc2f4b96a8627014c8e8713a.shtml",
        "as_of": CATALOG_AS_OF,
    },
    "NHC-PUBLIC-HEALTH-2024": {
        "title": "公共场所卫生管理条例（2024修订）",
        "authority": "国务院",
        "effective": "2025-01-20",
        "url": "https://www.nhc.gov.cn/fzs/c100048/201808/685a2df0bfa446c5ade0b89938f1fbec.shtml",
        "as_of": CATALOG_AS_OF,
    },
    "NHC-PUBLIC-RULES-2017": {
        "title": "公共场所卫生管理条例实施细则",
        "authority": "国家卫生健康委员会",
        "effective": "2017-12-26",
        "url": "https://www.nhc.gov.cn/wjw/c100221/202201/5f363fcea6f24665a79fce9fd6a0991e.shtml",
        "as_of": CATALOG_AS_OF,
    },
    "NHC-HOTEL-BEAUTY-2007": {
        "title": "住宿业卫生规范、美容美发场所卫生规范",
        "authority": "卫生部、商务部",
        "effective": "2007-06-25",
        "url": "https://www.nhc.gov.cn/zhjcj/s5853/200804/16ea3a5bbfcd43e6929fac45338f3949.shtml",
        "as_of": CATALOG_AS_OF,
    },
    "NHC-GB37487-2019": {
        "title": "GB 37487-2019 公共场所卫生管理规范",
        "authority": "国家卫生健康委员会",
        "effective": "2019-11-01",
        "url": "https://www.nhc.gov.cn/wjw/pgw/202003/0f8d6aa7f8c6402195bd1f07756863f8.shtml",
        "as_of": CATALOG_AS_OF,
    },
    "NHC-GB37488-2019": {
        "title": "GB 37488-2019 公共场所卫生指标及限值要求",
        "authority": "国家卫生健康委员会",
        "effective": "2019-11-01",
        "url": "https://www.nhc.gov.cn/wjw/pgw/202003/682fcc9c3d3c406cac782451eca02e23.shtml",
        "as_of": CATALOG_AS_OF,
    },
    "HOTEL-SECURITY-1987": {
        "title": "旅馆业治安管理办法",
        "authority": "国务院",
        "effective": "1987-11-10",
        "url": "https://xzfg.moj.gov.cn/front/law/detail?LawID=793",
        "as_of": CATALOG_AS_OF,
    },
    "SAMR-SPECIAL-EQUIPMENT-2014": {
        "title": "中华人民共和国特种设备安全法",
        "authority": "全国人大常委会",
        "effective": "2014-01-01",
        "url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_ad5e293574484b48b45047ee0ede6099.html",
        "as_of": CATALOG_AS_OF,
    },
    "MOT-AUTO-2023": {
        "title": "机动车维修管理规定（2023年第五次修正）",
        "authority": "交通运输部",
        "effective": "2023-11-10",
        "url": "https://xxgk.mot.gov.cn/jigou/fgs/202312/t20231204_3961931.html",
        "as_of": CATALOG_AS_OF,
    },
    "MEE-AUTO-HAZWASTE-2011": {
        "title": "关于机动车维修企业产生的废弃机油桶是否属于危险废物以及相关法律适用问题的复函",
        "authority": "生态环境部",
        "effective": "2011-04-07",
        "url": "https://www.mee.gov.cn/gkml/hbb/bh/201104/t20110412_209095.htm",
        "as_of": CATALOG_AS_OF,
    },
    "NMPA-DRUG-LAW-2019": {
        "title": "中华人民共和国药品管理法（2019修订）",
        "authority": "全国人大常委会",
        "effective": "2019-12-01",
        "url": "https://english.nmpa.gov.cn/2019-09/26/c_773012.htm",
        "as_of": CATALOG_AS_OF,
    },
    "NMPA-GSP-2016": {
        "title": "药品经营质量管理规范（GSP）",
        "authority": "国家药品监督管理局",
        "effective": "2016-07-20",
        "url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/bgt/art/2023/art_bc07ffdb7a1c4e46be371ac5a4a65f9c.html",
        "as_of": CATALOG_AS_OF,
    },
    "SAMR-COSMETICS-2021": {
        "title": "化妆品监督管理条例",
        "authority": "国务院",
        "effective": "2021-01-01",
        "url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/bgt/art/2023/art_9f8b70e79a2242df96c6c290a0ac425b.html",
        "as_of": CATALOG_AS_OF,
    },
    "NHC-MEDICAL-BEAUTY-2002": {
        "title": "医疗美容服务管理办法",
        "authority": "国家卫生健康委员会",
        "effective": "2002-05-01",
        "url": "https://www.nhc.gov.cn/wjw/c100221/202201/d7e8fa33a26b425da98d69fb04191699.shtml",
        "as_of": CATALOG_AS_OF,
    },
    "SPORT-HIGH-RISK-2018": {
        "title": "经营高危险性体育项目许可管理办法（2018修改）",
        "authority": "国家体育总局",
        "effective": "2018-11-30",
        "url": "https://www.sport.gov.cn/n315/n9041/n25319615/n25319619/c25523179/content.html",
        "as_of": CATALOG_AS_OF,
    },
    "SPORT-LIFEGUARD-2020": {
        "title": "游泳救生员国家职业技能标准（2020年版）",
        "authority": "人力资源和社会保障部、国家体育总局",
        "effective": "2020-03-19",
        "url": "https://www.mohrss.gov.cn/xxgk2020/fdzdgknr/rcrs_4225/jnrc/202112/t20211227_431391.html",
        "as_of": CATALOG_AS_OF,
    },
    "MOA-ANIMAL-CLINIC-2022": {
        "title": "动物诊疗机构管理办法",
        "authority": "农业农村部",
        "effective": "2022-10-07",
        "url": "https://xmsyj.moa.gov.cn/gzdt/202209/t20220909_6408940.htm",
        "as_of": CATALOG_AS_OF,
    },
    "MOA-ANIMAL-RECORDS-2024": {
        "title": "动物诊疗病历管理规范和兽医处方格式及应用规范",
        "authority": "农业农村部",
        "effective": "2024-05-01",
        "url": "https://xmsyj.moa.gov.cn/gzdt/202312/t20231214_6442774.htm",
        "as_of": CATALOG_AS_OF,
    },
    "MOA-ANIMAL-EPIDEMIC-2021": {
        "title": "中华人民共和国动物防疫法（2021修订）",
        "authority": "全国人大常委会",
        "effective": "2021-05-01",
        "url": "https://fgs.moa.gov.cn/flfg/202002/t20200217_6337192.htm",
        "as_of": CATALOG_AS_OF,
    },
}

for _source_no, _source in _SOURCES.items():
    # 映射 key 便于 O(1) 查询，条目内同时带 source_no，便于直接导出为数组。
    _source["source_no"] = _source_no


def _item(
    item_code: str,
    area_code: str,
    label: str,
    tier: str,
    source_no: str,
    *,
    input_type: str = "boolean",
    evidence: str = "photo",
    shot_guide: str,
    weight: int,
    severity: str,
    condition: str = "all_stores",
    jurisdiction: str = "CN",
) -> dict[str, Any]:
    source = _SOURCES[source_no]
    return {
        "item_code": item_code,
        "area_code": area_code,
        "label": label,
        "tier": tier,
        "input_type": input_type,
        "required": tier == "mandatory",
        "evidence": evidence,
        "shot_guide": shot_guide,
        "weight": weight,
        "severity": severity,
        "condition": condition,
        "jurisdiction": jurisdiction,
        "source_no": source_no,
        "source_url": source["url"],
        "effective": source["effective"],
        "as_of": CATALOG_AS_OF,
    }


_COMMON_ITEMS = (
    _item(
        "common.fire_exit", "fire_safety", "疏散通道和安全出口保持畅通",
        "mandatory", "FIRE-LAW-2021", shot_guide="从通道起点向安全出口拍摄，同时展示地面、通道和出口门",
        weight=20, severity="critical",
    ),
    _item(
        "common.price_display", "customer_area", "商品或服务价格清晰公示且与结算一致",
        "mandatory", "SAMR-PRICE-2022", shot_guide="同框拍到价签/价目表与对应商品或服务名称",
        weight=10, severity="high",
    ),
    _item(
        "common.fire_equipment", "fire_safety", "消防器材未被遮挡且外观状态可检查",
        "recommended", "FIRE-LAW-2021", shot_guide="拍全貌及压力表/检查标识，不遮挡设备周边",
        weight=8, severity="high",
    ),
    _item(
        "common.fire_inspection_log", "records", "防火检查和隐患整改记录可追溯",
        "operations", "FIRE-LAW-2021", input_type="document", evidence="record",
        shot_guide="拍记录首页、最近一次检查页和对应设备，个人信息可遮盖",
        weight=5, severity="medium",
    ),
)


def _overlay(prefix: str, rows: Sequence[tuple]) -> tuple[dict[str, Any], ...]:
    return tuple(
        _item(
            f"{prefix}.{row[0]}", row[1], row[2], row[3], row[4],
            input_type=row[5], evidence=row[6], shot_guide=row[7],
            weight=row[8], severity=row[9],
            condition=row[10] if len(row) > 10 else "all_stores",
            jurisdiction=row[11] if len(row) > 11 else "CN",
        )
        for row in rows
    )


_INDUSTRY_ITEMS: dict[str, tuple[dict[str, Any], ...]] = {
    "restaurant": _overlay("restaurant", (
        ("food_license", "license", "食品经营许可/备案信息与实际经营项目一致", "mandatory", "SAMR-FOOD-PERMIT-2023", "document", "document", "拍完整证照及经营项目，同框体现门店名称", 18, "critical"),
        ("raw_cooked_separation", "back_of_house", "原料、半成品与成品分区，工器具不交叉混用", "recommended", "NHC-GB31654-2021", "boolean", "photo", "广角拍操作台、刀板和容器的分区/标识", 12, "high"),
        ("pass_log", "pass", "出品、留样或清洗消毒记录与当日运营可对应", "operations", "SAMR-FOOD-OPS-2018", "document", "record", "拍当日记录和出餐口全景，遮盖顾客信息", 6, "medium"),
        ("food_safety_staff", "personnel", "适用主体配备食品安全总监或食品安全员，并能提供任命与履职记录", "mandatory", "SAMR-FOOD-RESP-97", "document", "record", "拍任命文件与最近履职记录，遮盖身份证号等个人信息", 14, "high", "when_covered_by_order_97_or_order_104"),
        ("supplier_traceability", "receiving", "采购食品及原料时查验供货者许可和合格证明，进货记录可追溯", "mandatory", "SAMR-FOOD-LAW-2021", "document", "record", "抽一项当日原料，同框拍包装标识、供货凭证及进货记录", 16, "high"),
        ("temperature_control", "storage", "需温控的原料、半成品和成品按标示条件贮存，监测记录可核验", "mandatory", "SAMR-FOOD-LAW-2021", "boolean", "photo", "拍设备显示、产品贮存条件标识和最近监测记录，不自行设定阈值", 15, "high", "when_products_require_temperature_control"),
        ("expiry_management", "storage", "定期检查库存，及时隔离和清理变质或超过保质期的食品及原料", "mandatory", "SAMR-FOOD-LAW-2021", "boolean", "photo", "抽拍日期标识及临期/不合格品隔离区，不推断未展示库存", 15, "critical"),
        ("cleaning_disinfection", "cleaning", "餐饮具与直接接触食品的工器具完成清洗消毒并保持清洁", "mandatory", "NHC-GB31654-2021", "boolean", "photo", "拍清洗、消毒、保洁动线及当班记录，避免只拍设备外壳", 14, "high"),
        ("risk_governance", "records", "适用主体留存日管控、周排查、月调度风险治理记录", "mandatory", "SAMR-CATERING-CHAIN-104", "document", "record", "分别拍最近日、周、月记录的日期与结论，经营信息可遮盖", 12, "high", "when_covered_by_order_97_or_order_104"),
    )),
    "tea_coffee": _overlay("tea_coffee", (
        ("food_license", "license", "食品经营许可/备案项目覆盖现制饮品实际业务", "mandatory", "SAMR-FOOD-PERMIT-2023", "document", "document", "拍完整证照、门店名称和经营项目", 18, "critical"),
        ("ingredient_storage", "bar", "开封原料有标识并按产品要求贮存", "recommended", "NHC-GB31654-2021", "boolean", "photo", "拍开封原料标识、容器和贮存设备全景", 12, "high"),
        ("handoff_log", "pickup", "制作、封口与交付环节可追溯且无混单", "operations", "SAMR-FOOD-OPS-2018", "boolean", "observation", "拍取餐台、订单标识和封口状态，遮盖联系方式", 6, "medium"),
        ("food_safety_staff", "personnel", "适用主体配备食品安全总监或食品安全员，并能提供任命与履职记录", "mandatory", "SAMR-FOOD-RESP-97", "document", "record", "拍任命文件与最近履职记录，遮盖身份证号等个人信息", 14, "high", "when_covered_by_order_97_or_order_104"),
        ("supplier_traceability", "receiving", "茶叶、乳品、水果及其他原料的供货资质和进货记录可追溯", "mandatory", "SAMR-FOOD-LAW-2021", "document", "record", "抽一项原料，同框拍产品标识、供货凭证和进货记录", 16, "high"),
        ("temperature_control", "cold_chain", "需冷藏冷冻的乳品、水果和半成品按标示条件贮存并留存监测记录", "mandatory", "SAMR-FOOD-LAW-2021", "boolean", "photo", "拍设备显示、产品贮存条件标识和最近监测记录", 15, "high", "when_products_require_temperature_control"),
        ("expiry_management", "bar", "开封日期、使用期限和原包装保质期可核验，过期原料已隔离清理", "mandatory", "SAMR-FOOD-LAW-2021", "boolean", "photo", "抽拍开封标识、原包装日期和不合格品隔离区", 15, "critical"),
        ("cleaning_disinfection", "cleaning", "饮具与直接接触饮品的器具完成清洗消毒并保持清洁", "mandatory", "NHC-GB31654-2021", "boolean", "photo", "拍清洗、消毒、保洁分区与当班记录", 14, "high"),
        ("risk_governance", "records", "适用主体留存日管控、周排查、月调度风险治理记录", "mandatory", "SAMR-CATERING-CHAIN-104", "document", "record", "分别拍最近日、周、月记录的日期与结论，经营信息可遮盖", 12, "high", "when_covered_by_order_97_or_order_104"),
    )),
    "convenience": _overlay("convenience", (
        ("food_scope", "license", "食品销售及现场制售在许可/备案范围内", "mandatory", "SAMR-FOOD-PERMIT-2023", "document", "document", "拍证照经营项目和现场制售区全景", 16, "critical"),
        ("expiry_rotation", "shelves", "商品保质期标识可见，临期与常规商品可区分管理", "recommended", "SAMR-FOOD-OPS-2018", "boolean", "photo", "拍货架全景及抽样商品日期，不需要推断未展示商品", 10, "high"),
        ("shift_handover", "cashier", "交接班的现金、报损和异常记录可追溯", "operations", "SAMR-PRICE-2022", "document", "record", "拍交接记录与收银台全景，遮盖账号与个人信息", 5, "medium"),
        ("food_safety_staff", "personnel", "适用主体配备食品安全总监或食品安全员，并能提供任命与履职记录", "mandatory", "SAMR-FOOD-RESP-97", "document", "record", "拍任命文件与最近履职记录，遮盖身份证号等个人信息", 13, "high", "when_covered_by_order_97_or_order_114"),
        ("supplier_traceability", "receiving", "食品供货者许可、合格证明和进货查验记录可相互对应", "mandatory", "SAMR-FOOD-LAW-2021", "document", "record", "抽一项在售食品，同框拍包装标识、供货凭证和进货记录", 15, "high"),
        ("temperature_control", "cold_chain", "需温控食品按标示条件陈列贮存，设备监测记录可核验", "mandatory", "SAMR-FOOD-LAW-2021", "boolean", "photo", "拍设备显示、商品贮存条件标识和最近监测记录", 14, "high", "when_products_require_temperature_control"),
        ("cleaning_disinfection", "hot_food", "现场制售区直接接触食品的设备和器具完成清洗消毒", "mandatory", "NHC-GB31654-2021", "boolean", "photo", "拍清洗、消毒、保洁分区及当班记录", 13, "high", "when_store_prepares_or_serves_ready_to_eat_food"),
        ("risk_governance", "records", "适用主体留存日管控、周排查、月调度风险治理记录", "mandatory", "SAMR-FOOD-SALES-CHAIN-114", "document", "record", "分别拍最近日、周、月记录的日期与结论，经营信息可遮盖", 12, "high", "when_food_sales_chain_store_or_covered_by_order_97"),
    )),
    "grocery": _overlay("grocery", (
        ("food_scope", "license", "食品和食用农产品销售资质/备案与实际业务一致", "mandatory", "SAMR-FOOD-PERMIT-2023", "document", "document", "拍证照、门店名称和主要销售区全景", 16, "critical"),
        ("cold_chain_display", "cold_chain", "冷藏冷冻商品与设备运行记录可对应", "recommended", "NHC-GB31654-2021", "boolean", "photo", "同框拍陈列柜、设备显示与商品状态，不自行设定温度阈值", 12, "high"),
        ("loss_record", "stockroom", "报损、退货与临期处理记录可追溯", "operations", "SAMR-FOOD-OPS-2018", "document", "record", "拍最近记录和对应隔离区，遮盖个人信息", 6, "medium"),
        ("food_safety_staff", "personnel", "适用主体配备食品安全总监或食品安全员，并能提供任命与履职记录", "mandatory", "SAMR-FOOD-RESP-97", "document", "record", "拍任命文件与最近履职记录，遮盖身份证号等个人信息", 13, "high", "when_covered_by_order_97_or_order_114"),
        ("supplier_traceability", "receiving", "食品及食用农产品供货信息、合格证明和进货记录可追溯", "mandatory", "SAMR-FOOD-LAW-2021", "document", "record", "抽一项在售商品，同框拍标识、供货凭证和进货记录", 15, "high"),
        ("expiry_management", "shelves", "库存定期检查，超过保质期或感官异常食品已隔离清理", "mandatory", "SAMR-FOOD-LAW-2021", "boolean", "photo", "抽拍商品日期及临期/不合格品隔离区，不推断未展示库存", 15, "critical"),
        ("cleaning_disinfection", "fresh_area", "生鲜接触面、称量器具和周转容器按制度清洁消毒", "mandatory", "SAMR-FOOD-LAW-2021", "boolean", "photo", "拍清洁工具分区、作业现场和最近清洁记录", 13, "high", "when_selling_unpacked_fresh_or_ready_to_eat_food"),
        ("risk_governance", "records", "适用主体留存日管控、周排查、月调度风险治理记录", "mandatory", "SAMR-FOOD-SALES-CHAIN-114", "document", "record", "分别拍最近日、周、月记录的日期与结论，经营信息可遮盖", 12, "high", "when_food_sales_chain_store_or_covered_by_order_97"),
    )),
    "snack": _overlay("snack", (
        ("food_scope", "license", "预包装、散装食品销售在许可/备案范围内", "mandatory", "SAMR-FOOD-PERMIT-2023", "document", "document", "拍证照经营项目与散称区全景", 16, "critical"),
        ("bulk_label", "bulk_goods", "散装食品的名称、日期等标识与货品对应", "recommended", "SAMR-FOOD-OPS-2018", "boolean", "photo", "同框拍散装容器、商品和完整标识", 11, "high"),
        ("replenishment_log", "shelves", "补货、临期与报损记录能对应货架异常", "operations", "SAMR-FOOD-OPS-2018", "document", "record", "拍抽样货架和最近处理记录", 5, "medium"),
        ("food_safety_staff", "personnel", "适用主体配备食品安全总监或食品安全员，并能提供任命与履职记录", "mandatory", "SAMR-FOOD-RESP-97", "document", "record", "拍任命文件与最近履职记录，遮盖身份证号等个人信息", 13, "high", "when_covered_by_order_97_or_order_114"),
        ("supplier_traceability", "receiving", "预包装和散装食品的供货资质、合格证明及进货记录可追溯", "mandatory", "SAMR-FOOD-LAW-2021", "document", "record", "抽一项在售食品，同框拍产品标识、供货凭证和进货记录", 15, "high"),
        ("temperature_control", "storage", "需温控食品按标示条件贮存，设备监测记录可核验", "mandatory", "SAMR-FOOD-LAW-2021", "boolean", "photo", "拍设备显示、产品贮存条件标识和最近监测记录", 14, "high", "when_products_require_temperature_control"),
        ("expiry_management", "shelves", "库存定期检查，超过保质期或感官异常食品已隔离清理", "mandatory", "SAMR-FOOD-LAW-2021", "boolean", "photo", "抽拍商品日期及临期/不合格品隔离区，不推断未展示库存", 15, "critical"),
        ("cleaning_disinfection", "bulk_goods", "散装食品容器、取用工具和接触面按制度清洁消毒", "mandatory", "SAMR-FOOD-LAW-2021", "boolean", "photo", "拍容器防护、专用工具、清洁存放点和最近记录", 13, "high", "when_selling_bulk_food"),
        ("risk_governance", "records", "适用主体留存日管控、周排查、月调度风险治理记录", "mandatory", "SAMR-FOOD-SALES-CHAIN-114", "document", "record", "分别拍最近日、周、月记录的日期与结论，经营信息可遮盖", 12, "high", "when_food_sales_chain_store_or_covered_by_order_97"),
    )),
    "hotel": _overlay("hotel", (
        ("health_license", "license", "住宿场所卫生许可与实际经营主体一致", "mandatory", "NHC-PUBLIC-HEALTH-2024", "document", "document", "拍完整卫生许可证及大堂门店名称", 18, "critical"),
        ("linen_separation", "linen_room", "清洁布草、污染布草和客用品分区存放", "recommended", "NHC-HOTEL-BEAUTY-2007", "boolean", "photo", "广角拍布草间分区、标识与离地存放状态", 12, "high"),
        ("room_cleaning_log", "guest_room", "客房清洁、用品更换与异常记录可追溯", "operations", "NHC-PUBLIC-RULES-2017", "document", "record", "拍房号脱敏后的清洁记录和客房全景", 6, "medium"),
        ("personnel_health", "personnel", "直接为顾客服务的人员按公共场所卫生要求持有效健康合格证明", "mandatory", "NHC-PUBLIC-RULES-2017", "document", "record", "拍岗位名册与有效证明，遮盖身份证号和住址", 13, "high", "when_role_requires_health_certificate"),
        ("guest_registration", "front_desk", "住宿旅客按规定查验身份证件并如实登记", "mandatory", "HOTEL-SECURITY-1987", "document", "record", "仅核验登记流程和脱敏抽样记录，不拍摄或导出证件号码", 18, "critical"),
        ("water_hygiene", "water_system", "生活饮用水、二次供水或泳池水的卫生管理与检测记录可核验", "mandatory", "NHC-GB37488-2019", "document", "record", "拍供水设施标识和最近检测记录，按实际供水类型核验", 15, "high", "when_operating_secondary_water_supply_or_pool"),
        ("gas_safety", "plant_room", "使用燃气的厨房或热水设备宜建立阀门、软管、报警和通风巡查清单", "recommended", "FIRE-LAW-2021", "boolean", "photo", "拍燃气总阀、报警装置、通风和最近巡查记录，不拆卸设备", 10, "high", "when_store_uses_gas"),
        ("special_equipment", "plant_room", "电梯、锅炉或压力设备依法登记、检验并留存安全检查记录", "mandatory", "SAMR-SPECIAL-EQUIPMENT-2014", "document", "record", "按实际设备拍使用标志、检验信息和最近检查记录", 16, "critical", "when_store_uses_regulated_special_equipment"),
    )),
    "beauty": _overlay("beauty", (
        ("health_license", "license", "生活美容场所卫生许可与经营主体一致", "mandatory", "NHC-PUBLIC-HEALTH-2024", "document", "document", "拍完整卫生许可证和门店名称", 18, "critical"),
        ("tool_disinfection", "service_room", "顾客用品与工器具清洗、消毒、保洁分区明确", "recommended", "NHC-HOTEL-BEAUTY-2007", "boolean", "photo", "拍工器具流转区、消毒设备和清洁存放区", 12, "high"),
        ("disinfection_log", "records", "用品消毒和卫生自查记录可追溯", "operations", "NHC-PUBLIC-RULES-2017", "document", "record", "拍最近消毒记录与对应设备，遮盖顾客信息", 6, "medium"),
        ("personnel_health", "personnel", "直接为顾客服务的人员按公共场所卫生要求持有效健康合格证明", "mandatory", "NHC-PUBLIC-RULES-2017", "document", "record", "拍岗位名册与有效证明，遮盖身份证号和住址", 13, "high", "when_role_requires_health_certificate"),
        ("cosmetic_traceability", "product_storage", "经营使用的化妆品来源合法，产品标签和进货查验记录可追溯", "mandatory", "SAMR-COSMETICS-2021", "document", "record", "抽一项在用产品，拍完整标签、供货凭证和进货记录", 15, "high"),
        ("cosmetic_expiry", "product_storage", "超过使用期限、变质或标签缺失的化妆品已停止使用并隔离", "mandatory", "SAMR-COSMETICS-2021", "boolean", "photo", "抽拍产品期限、开封标识和隔离区，不推断未展示产品", 15, "critical"),
        ("medical_beauty_boundary", "service_room", "未取得医疗机构资质时，不开展创伤性、侵入性或使用药物器械的医疗美容项目", "mandatory", "NHC-MEDICAL-BEAUTY-2002", "boolean", "observation", "核对项目单、宣传页与现场设备；仅生活美容门店记录不适用医疗项目", 20, "critical", "when_offering_or_promoting_invasive_or_medical_beauty_services"),
        ("cosmetic_stop_use", "records", "接到缺陷、召回或不良反应风险信息时，相关化妆品停止使用并留存处置记录", "mandatory", "SAMR-COSMETICS-2021", "document", "record", "拍脱敏通知、隔离产品和处置记录", 13, "high", "when_recall_defect_or_adverse_event_notice_exists"),
    )),
    "auto": _overlay("auto", (
        ("repair_filing", "license", "机动车维修经营备案与公示的维修范围一致", "mandatory", "MOT-AUTO-2023", "document", "document", "拍备案/公示信息、门店名称与主作业区", 18, "critical"),
        ("parts_traceability", "parts_room", "配件来源、包装与待用/旧件分区可追溯", "recommended", "MOT-AUTO-2023", "boolean", "photo", "拍配件标识、存放区和旧件交付/隔离区", 10, "high"),
        ("repair_order", "workshop", "维修项目、配件、工时与质量检验记录可对应", "operations", "MOT-AUTO-2023", "document", "record", "拍脱敏维修工单和对应工位，不拍车主联系方式", 7, "medium"),
        ("technical_personnel", "personnel", "技术负责人、质量检验员等关键岗位人员与备案业务和岗位要求相匹配", "mandatory", "MOT-AUTO-2023", "document", "record", "拍岗位名册、资格或培训记录，遮盖身份证号和联系方式", 14, "high"),
        ("quality_inspection", "quality_control", "维修作业执行进厂、过程和竣工质量检验，记录与车辆工单对应", "mandatory", "MOT-AUTO-2023", "document", "record", "抽一辆已完工车辆，拍脱敏工单和三阶段检验记录", 16, "critical"),
        ("emission_tamper_redline", "workshop", "不得擅自改装污染控制装置或承修报废机动车", "mandatory", "MOT-AUTO-2023", "boolean", "observation", "核对工单项目、拆装件和作业现场，不记录车主个人信息", 20, "critical", "when_repair_scope_involves_emission_control_or_vehicle_status"),
        ("hazardous_waste", "hazardous_waste", "废矿物油、受污染容器等危险废物分类收集、规范暂存并可追溯去向", "mandatory", "MEE-AUTO-HAZWASTE-2011", "document", "record", "拍容器标识、防渗区域和脱敏转移/交接记录", 17, "critical", "when_generating_hazardous_repair_waste"),
        ("high_voltage_workflow", "workshop", "维修新能源汽车时宜实施高压隔离、断电验电、专用防护和双人复核", "recommended", "MOT-AUTO-2023", "boolean", "observation", "拍高压警戒区、绝缘防护、断电标识和复核记录，不接触带电部件", 10, "high", "when_servicing_new_energy_vehicles"),
    )),
    "pharmacy": _overlay("pharmacy", (
        ("drug_license", "license", "药品经营许可证的主体、地址和经营范围与现场一致", "mandatory", "NMPA-DRUG-LAW-2019", "document", "document", "拍完整许可证、门店名称和地址信息", 20, "critical"),
        ("drug_storage", "drug_storage", "药品按质量状态与储存要求分区，异常品有隔离标识", "recommended", "NMPA-GSP-2016", "boolean", "photo", "拍药品储存区、分区标识和监测记录，不自行设定温湿度阈值", 14, "high"),
        ("prescription_audit", "dispensing", "处方审核、调配复核与销售记录可追溯", "operations", "NMPA-GSP-2016", "document", "record", "拍脱敏记录、调配台和处方药分区，遮盖患者信息", 8, "high"),
        ("pharmacist_on_duty", "personnel", "经营处方药时，依法配备并由执业药师或其他药学技术人员履行审方职责", "mandatory", "NMPA-DRUG-LAW-2019", "document", "record", "拍在岗公示、注册信息和排班记录，遮盖身份证号", 18, "critical", "when_selling_prescription_drugs"),
        ("gsp_storage", "drug_storage", "药品按标签和GSP要求分类储存，温湿度监测及异常处置记录完整", "mandatory", "NMPA-GSP-2016", "document", "record", "拍储存分区、产品条件标识、设备显示和最近异常处置记录", 16, "high"),
        ("cold_chain", "cold_chain", "冷藏药品收货、储存和销售交接的温度记录连续可追溯", "mandatory", "NMPA-GSP-2016", "document", "record", "抽一项冷藏药品，拍标签条件、设备显示和收货至销售记录", 17, "critical", "when_handling_cold_chain_drugs"),
        ("expiry_quarantine", "drug_storage", "近效期药品有标识和跟踪，过期或质量异常药品已隔离并停止销售", "mandatory", "NMPA-GSP-2016", "boolean", "photo", "抽拍药品日期、近效期提示和不合格品隔离区", 17, "critical"),
        ("recall_stop_sale", "records", "召回、停售或质量风险通知对应药品已隔离，处置和上报记录可追溯", "mandatory", "NMPA-GSP-2016", "document", "record", "拍脱敏通知、隔离状态和处置记录", 16, "critical", "when_recall_stop_sale_or_quality_notice_exists"),
    )),
    "fitness": _overlay("fitness", (
        ("high_risk_license", "license", "实际经营高危险性体育项目时，许可证及人员名录按规定公示", "mandatory", "SPORT-HIGH-RISK-2018", "document", "document", "如门店有游泳、攀岩等高危项目，拍许可证、人员名录和项目区；不适用时录入不适用依据", 20, "critical", "when_operating_high_risk_sports"),
        ("equipment_check", "training_floor", "健身器械状态、使用说明和日常检查信息可核验", "recommended", "SPORT-HIGH-RISK-2018", "boolean", "photo", "拍器械全貌、关键连接部位和检查标识", 12, "high"),
        ("incident_log", "records", "设备异常、伤情与应急处置记录可追溯", "operations", "SPORT-HIGH-RISK-2018", "document", "record", "拍脱敏记录与应急设备存放位置", 7, "high"),
        ("qualified_staff", "personnel", "高危险性体育项目配备符合数量和资质要求的社会体育指导、救助等人员", "mandatory", "SPORT-HIGH-RISK-2018", "document", "record", "拍人员名录、资格证明与当班排班，遮盖身份证号", 17, "critical", "when_operating_high_risk_sports"),
        ("lifeguard_positions", "pool", "游泳场所救生员在岗位置、观察范围和交接记录与开放时段对应", "mandatory", "SPORT-LIFEGUARD-2020", "boolean", "observation", "拍开放时段救生岗位全景和脱敏交接记录", 17, "critical", "when_operating_a_swimming_pool"),
        ("pool_water_quality", "pool", "游泳池水质达到适用卫生指标，现场检测与送检记录可核验", "mandatory", "NHC-GB37488-2019", "document", "record", "拍现场公示、当日检测和最近送检记录，不自行改写限值", 16, "high", "when_operating_a_swimming_pool"),
        ("public_sanitation", "locker_room", "更衣、淋浴、通风和公共用品卫生管理符合公共场所要求", "mandatory", "NHC-GB37487-2019", "boolean", "photo", "拍更衣淋浴区、通风设施、清洁工具分区及当日记录", 14, "high", "when_venue_is_subject_to_public_place_hygiene_rules"),
        ("aed_readiness", "emergency", "经营安全建议：配置可用AED和急救包，并让当班人员熟悉位置与基本响应流程", "recommended", "SPORT-LIFEGUARD-2020", "boolean", "observation", "拍设备位置、状态标识和最近培训记录；此项不宣称全国统一强制配置", 8, "medium"),
    )),
    "pet": _overlay("pet", (
        ("clinic_license", "license", "实际开展动物诊疗时，动物诊疗许可和执业人员信息符合规定", "mandatory", "MOA-ANIMAL-CLINIC-2022", "document", "document", "如开展诊疗，拍许可证、执业信息和诊疗区；不开展时录入不适用依据", 20, "critical", "when_providing_animal_diagnosis_or_treatment"),
        ("clean_dirty_flow", "clinic_or_grooming", "清洁区、污染区与动物暂留区分开，避免交叉流转", "recommended", "MOA-ANIMAL-CLINIC-2022", "boolean", "photo", "广角拍诊疗/洗护动线、分区标识和废弃物存放点", 12, "high"),
        ("medical_record", "records", "诊疗病历、处方和诊疗废弃物处理记录可追溯", "operations", "MOA-ANIMAL-RECORDS-2024", "document", "record", "拍脱敏病历/处方和废弃物交接记录，遮盖客户信息", 8, "high"),
        ("registered_veterinarian", "personnel", "开展动物诊疗时配备符合机构类别要求的执业兽医师并依法备案执业", "mandatory", "MOA-ANIMAL-CLINIC-2022", "document", "record", "拍执业人员公示、备案信息和当班排班，遮盖身份证号", 18, "critical", "when_providing_animal_diagnosis_or_treatment"),
        ("animal_isolation", "animal_holding", "疑似传染病动物有隔离场所、标识和消毒防护流程", "mandatory", "MOA-ANIMAL-EPIDEMIC-2021", "boolean", "photo", "拍隔离区、动线标识、防护用品和最近消毒记录", 17, "critical", "when_receiving_animals_for_diagnosis_or_treatment"),
        ("medicine_management", "drug_storage", "兽药和生物制品分类储存，购进、使用、效期与异常品隔离记录可追溯", "mandatory", "MOA-ANIMAL-CLINIC-2022", "document", "record", "抽一项在用药品，拍标签、储存条件、购进使用及隔离记录", 16, "high", "when_storing_or_using_veterinary_medicines"),
        ("clinical_waste", "clinical_waste", "诊疗废弃物分类收集、暂存和无害化处理记录可追溯", "mandatory", "MOA-ANIMAL-CLINIC-2022", "document", "record", "拍分类容器、暂存标识和脱敏交接记录", 17, "critical", "when_generating_animal_clinical_waste"),
        ("disinfection_log", "records", "诊疗、隔离和动物暂留区域的清洁消毒计划及执行记录完整", "mandatory", "MOA-ANIMAL-EPIDEMIC-2021", "document", "record", "拍清洁消毒分区、用品和最近执行记录", 14, "high", "when_receiving_animals_on_site"),
    )),
}


def _slot(code: str, area: str, label: str, guide: str, *, required: bool = True) -> dict[str, Any]:
    return {
        "slot_code": code,
        "area_code": area,
        "label": label,
        "required": required,
        "shot_guide": guide,
        "min_photos": 1 if required else 0,
        "max_photos": 1,
    }


_COMMON_SLOTS = (
    _slot("common.facade", "facade", "门头与入口", "正面广角，完整包含门头、入口和周边通行区"),
    _slot("common.cashier", "cashier", "收银/接待区", "拍服务台全景、价格公示和顾客动线，遮盖个人信息"),
    _slot("common.environment", "environment", "顾客区环境", "从主通道拍广角，展示地面、墙面、照明与陈列"),
    _slot("common.fire_route", "fire_safety", "疏散通道", "从通道起点拍至安全出口，确保遮挡物和指示标志可见"),
)

_INDUSTRY_SLOTS = {
    "restaurant": (
        _slot("restaurant.kitchen", "back_of_house", "后厨全景", "广角展示备餐、烹饪、清洗动线"),
        _slot("restaurant.storage", "storage", "库房/冷藏", "拍货架、标识、离地存放和冷藏设备"),
        _slot("restaurant.pass", "pass", "出餐口", "拍出餐台、餐具和待交付食品防护状态"),
    ),
    "tea_coffee": (
        _slot("tea_coffee.bar", "bar", "制作吧台", "拍原料、器具、制作和清洗区"),
        _slot("tea_coffee.cold_storage", "storage", "冷藏与原料区", "拍设备显示、原料标识和容器状态"),
        _slot("tea_coffee.pickup", "pickup", "取餐区", "拍订单标识、封口和交付动线"),
    ),
    "convenience": (
        _slot("convenience.shelves", "shelves", "食品货架", "拍陈列、价签和保质期抽样"),
        _slot("convenience.hot_food", "hot_food", "现制现售区", "拍设备、食品防护和工器具"),
        _slot("convenience.stockroom", "stockroom", "库房", "拍分区、离地存放和报损隔离区"),
    ),
    "grocery": (
        _slot("grocery.fresh", "fresh_area", "生鲜区", "拍商品、标识、称重和清洁状态"),
        _slot("grocery.cold_chain", "cold_chain", "冷链陈列", "同框拍陈列柜、设备显示和商品"),
        _slot("grocery.stockroom", "stockroom", "后场库房", "拍分区、货架和报损/退货区"),
    ),
    "snack": (
        _slot("snack.bulk", "bulk_goods", "散称区", "拍容器防护、取用工具和完整标识"),
        _slot("snack.shelves", "shelves", "预包装货架", "拍价签、陈列和保质期抽样"),
        _slot("snack.stockroom", "stockroom", "后场库房", "拍离地存放、分区和临期/报损隔离区"),
    ),
    "hotel": (
        _slot("hotel.guest_room", "guest_room", "客房", "广角拍床品、卫生间和客用品"),
        _slot("hotel.linen", "linen_room", "布草间", "拍清洁/污染布草分区与存放状态"),
        _slot("hotel.back_office", "back_of_house", "客房后场", "拍清洁车、用品库和废弃物动线"),
    ),
    "beauty": (
        _slot("beauty.service_room", "service_room", "服务间", "拍操作位、顾客用品和清洁防护"),
        _slot("beauty.disinfection", "disinfection", "消毒区", "拍清洗、消毒、保洁分区和设备"),
        _slot("beauty.products", "product_storage", "产品存放区", "拍产品标识、开封状态和存放环境"),
    ),
    "auto": (
        _slot("auto.workshop", "workshop", "维修车间", "广角拍工位、举升设备和人车动线"),
        _slot("auto.parts", "parts_room", "配件库", "拍新件、旧件、待用件分区和标识"),
        _slot("auto.waste", "hazardous_waste", "废油/废件区", "拍容器、防渗、标识和存放区全景"),
    ),
    "pharmacy": (
        _slot("pharmacy.dispensing", "dispensing", "处方药调配区", "拍处方药分区、审方位和调配台，不拍患者信息"),
        _slot("pharmacy.storage", "drug_storage", "药品储存区", "拍分区标识、设备显示和异常品隔离区"),
        _slot("pharmacy.cold_chain", "cold_chain", "药品冷链区", "拍设备、监测显示和应急措施"),
    ),
    "fitness": (
        _slot("fitness.training", "training_floor", "训练区", "广角拍器械间距、通道和器械状态"),
        _slot("fitness.high_risk", "high_risk_area", "高危项目区", "如适用，拍项目区、安全公示、救助人员与设备", required=False),
        _slot("fitness.locker", "locker_room", "更衣/淋浴区", "拍地面防滑、通风、储物与清洁状态"),
    ),
    "pet": (
        _slot("pet.clinic", "clinic_or_grooming", "诊疗/洗护区", "广角拍操作位、清污分区和动物暂留位"),
        _slot("pet.holding", "animal_holding", "动物暂留区", "拍笼位、分隔、通风和清洁状态"),
        _slot("pet.waste", "clinical_waste", "诊疗废弃物区", "如开展诊疗，拍分类容器、标识与交接记录", required=False),
    ),
}


def _metric(
    code: str,
    label: str,
    unit: str,
    formula: str,
    inputs: Sequence[str],
    *,
    allowed_units: Sequence[str] | None = None,
) -> dict[str, Any]:
    units = list(allowed_units or (unit,))
    if not units or any(not isinstance(value, str) or not value for value in units):
        raise InspectionStandardError(f"指标 {code} 缺少合法单位")
    return {
        "metric_code": code,
        "label": label,
        "unit": unit,
        "allowed_units": units,
        "formula": formula,
        "required_inputs": list(inputs),
        "source_required": True,
        "value": None,
        "target": None,
        "benchmark": None,
        "as_of": CATALOG_AS_OF,
    }


_COMMON_METRICS = (
    _metric("common.net_revenue", "净营业额", "CNY", "有效实收金额合计 - 有效退款金额合计", ("paid_amount", "refunded_amount", "order_status"), allowed_units=("CNY", "元")),
    _metric("common.transactions", "有效交易笔数", "count", "统计周期内已完成且未全额退款的交易数", ("order_id", "order_status", "refunded_amount"), allowed_units=("count", "笔")),
    _metric("common.average_ticket", "客单价", "CNY/transaction", "净营业额 ÷ 有效交易笔数", ("net_revenue", "transactions"), allowed_units=("CNY/transaction", "元/笔")),
    _metric("common.employee_count", "员工数", "人", "统计时点内符合导入口径的在岗员工去重人数", ("employee_id", "employment_status", "scope_at"), allowed_units=("人", "person")),
    _metric("common.labor_hours", "实际出勤工时", "小时", "统计周期内符合导入口径的实际出勤时长合计", ("employee_id", "clock_in", "clock_out", "excluded_break_hours"), allowed_units=("小时", "hour")),
)

_INDUSTRY_METRICS = {
    "restaurant": (
        _metric("restaurant.table_turnover", "翻台率", "times", "完成就餐桌次 ÷ 同口径可用桌数", ("served_table_sessions", "available_tables")),
        _metric("restaurant.void_rate", "退菜/作废率", "ratio", "退菜或作废品项数 ÷ 已下单品项数", ("voided_line_items", "ordered_line_items")),
    ),
    "tea_coffee": (
        _metric("tea_coffee.cups_per_labor_hour", "人效杯数", "cups/labor_hour", "完成饮品杯数 ÷ 实际出勤工时", ("completed_cups", "labor_hours")),
        _metric("tea_coffee.remake_rate", "重做率", "ratio", "重做饮品杯数 ÷ 完成饮品杯数", ("remade_cups", "completed_cups")),
    ),
    "convenience": (
        _metric("convenience.stockout_rate", "缺货率", "ratio", "缺货 SKU 数 ÷ 应在售 SKU 数", ("out_of_stock_skus", "listed_skus")),
        _metric("convenience.loss_rate", "报损率", "ratio", "报损成本 ÷ 同口径销售成本", ("loss_cost", "cost_of_goods_sold")),
    ),
    "grocery": (
        _metric("grocery.shrink_rate", "损耗率", "ratio", "盘点损耗金额 ÷ 同口径销售成本", ("inventory_shrink_value", "cost_of_goods_sold")),
        _metric("grocery.fresh_sell_through", "生鲜售罄率", "ratio", "生鲜销售数量 ÷ 生鲜可售数量", ("fresh_units_sold", "fresh_units_available")),
    ),
    "snack": (
        _metric("snack.inventory_days", "库存周转天数", "days", "平均库存成本 ÷ 同口径日均销售成本", ("average_inventory_cost", "daily_cost_of_goods_sold")),
        _metric("snack.bulk_loss_rate", "散称损耗率", "ratio", "散称报损重量 ÷ 散称入库重量", ("bulk_loss_weight", "bulk_received_weight")),
    ),
    "hotel": (
        _metric("hotel.occupancy", "入住率", "ratio", "已售房晚 ÷ 可售房晚", ("sold_room_nights", "available_room_nights")),
        _metric("hotel.revpar", "RevPAR", "CNY/available_room", "客房收入 ÷ 可售房晚", ("room_revenue", "available_room_nights")),
    ),
    "beauty": (
        _metric("beauty.room_utilization", "房间/工位利用率", "ratio", "已占用服务时长 ÷ 可用服务时长", ("occupied_service_minutes", "available_service_minutes")),
        _metric("beauty.rebooking_rate", "再预约率", "ratio", "离店前完成下次预约的客次 ÷ 完成服务客次", ("rebooked_visits", "completed_visits")),
    ),
    "auto": (
        _metric("auto.bay_utilization", "工位利用率", "ratio", "工位实际作业时长 ÷ 工位可用时长", ("occupied_bay_hours", "available_bay_hours")),
        _metric("auto.first_time_fix_rate", "一次修复率", "ratio", "质保观察期内未因同一故障返修的完工单数 ÷ 可评价完工单数", ("non_repeat_repair_orders", "eligible_repair_orders", "observation_window")),
    ),
    "pharmacy": (
        _metric("pharmacy.expiry_loss_rate", "近效期/过期损失率", "ratio", "近效期与过期报损成本 ÷ 同口径销售成本", ("expiry_loss_cost", "cost_of_goods_sold")),
        _metric("pharmacy.prescription_audit_rate", "处方审核记录完整率", "ratio", "有完整审核记录的处方数 ÷ 应审核处方数", ("prescriptions_with_complete_audit", "prescriptions_requiring_audit")),
    ),
    "fitness": (
        _metric("fitness.visit_rate", "会员到店率", "ratio", "统计期内有到店记录的有效会员数 ÷ 期初有效会员数", ("visiting_active_members", "opening_active_members")),
        _metric("fitness.class_utilization", "团课上座率", "ratio", "实际签到人次 ÷ 已开课可预约名额", ("class_checkins", "offered_class_capacity")),
    ),
    "pet": (
        _metric("pet.appointment_fulfillment", "预约履约率", "ratio", "完成服务的预约数 ÷ 到期预约数", ("completed_appointments", "due_appointments")),
        _metric("pet.record_completeness", "诊疗记录完整率", "ratio", "字段完整的诊疗记录数 ÷ 应留诊疗记录数", ("complete_clinical_records", "required_clinical_records")),
    ),
}


def _industry(industry: Any) -> str:
    key = str(industry or "").strip()
    if key not in INDUSTRIES:
        raise UnknownIndustryError(f"未知行业: {key or '<empty>'}")
    return key


def _patch_map(raw: Any, layer: str) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        mapped: dict[str, dict[str, Any]] = {}
        for position, record in enumerate(raw):
            if not isinstance(record, Mapping):
                raise StandardOverrideError(f"{layer}[{position}] 覆盖必须是对象")
            code = record.get("item_code")
            if not isinstance(code, str) or not code.strip():
                raise StandardOverrideError(f"{layer}[{position}] 缺少 item_code")
            if code in mapped:
                raise StandardOverrideError(f"{layer}覆盖包含重复检查项: {code}")
            mapped[code] = {
                key: value for key, value in record.items() if key != "item_code"
            }
        return mapped
    raise StandardOverrideError(f"{layer} 覆盖必须是对象或对象数组")


def _override_layers(raw: Any) -> list[tuple[str, Mapping[str, Any]]]:
    if raw is None:
        return []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return [("tenant", _patch_map(raw, "tenant"))]
    if not isinstance(raw, Mapping):
        raise StandardOverrideError("覆盖配置必须是对象或对象数组")
    keys = set(raw)
    if keys & set(OVERRIDE_LAYERS):
        unknown = keys - set(OVERRIDE_LAYERS)
        if unknown:
            raise StandardOverrideError(f"未知覆盖层: {sorted(unknown)}")
        layers = []
        for layer in OVERRIDE_LAYERS:
            value = raw.get(layer, {})
            layers.append((layer, _patch_map(value, layer)))
        return layers
    return [("tenant", raw)]


def _apply_patch(item: dict[str, Any], patch: Any, layer: str) -> None:
    if not isinstance(patch, Mapping):
        raise StandardOverrideError(f"{layer}:{item['item_code']} 覆盖必须是对象")
    allowed = {"enabled", "required", "weight", "severity", "shot_guide"}
    unknown = set(patch) - allowed
    if unknown:
        raise StandardOverrideError(
            f"{layer}:{item['item_code']} 不允许覆盖字段 {sorted(unknown)}"
        )
    mandatory = item["tier"] == "mandatory"
    for field, value in patch.items():
        if field in {"enabled", "required"}:
            if not isinstance(value, bool):
                raise StandardOverrideError(f"{layer}:{item['item_code']} {field}必须是布尔值")
            if mandatory and value is False:
                raise StandardOverrideError(f"强制项 {item['item_code']} 不能关闭或取消必填")
            item[field] = value
        elif field == "weight":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StandardOverrideError(f"{layer}:{item['item_code']} weight必须是数字")
            number = float(value)
            if not math.isfinite(number) or not 0 <= number <= 100:
                raise StandardOverrideError(f"{layer}:{item['item_code']} weight必须在0-100之间")
            if mandatory and number < float(item["weight"]):
                raise StandardOverrideError(f"强制项 {item['item_code']} 不能降低权重")
            item[field] = int(number) if number.is_integer() else number
        elif field == "severity":
            if value not in SEVERITIES:
                raise StandardOverrideError(f"{layer}:{item['item_code']} severity无效")
            if mandatory and SEVERITIES.index(value) < SEVERITIES.index(item["severity"]):
                raise StandardOverrideError(f"强制项 {item['item_code']} 不能降低严重度")
            item[field] = value
        else:
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 300:
                raise StandardOverrideError(f"{layer}:{item['item_code']} shot_guide无效")
            item[field] = value.strip()


def effective_checklist(
    industry: str,
    tenant_overrides: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """返回通用基线 + 行业 overlay + 租户/区域/门店覆盖后的清单。

    覆盖可写为 ``{item_code: patch}``，也可按 ``tenant``、``region``、
    ``branch`` 三层组织，后一层在安全边界内覆盖前一层。
    未知项、未知字段和强制项降级均整个请求失败（fail closed）。
    """
    key = _industry(industry)
    items = [copy.deepcopy(item) for item in (*_COMMON_ITEMS, *_INDUSTRY_ITEMS[key])]
    by_code = {item["item_code"]: item for item in items}
    for layer, patches in _override_layers(tenant_overrides):
        for code, patch in patches.items():
            if not isinstance(code, str) or code not in by_code:
                raise StandardOverrideError(f"{layer}覆盖包含未知检查项: {code}")
            _apply_patch(by_code[code], patch, layer)
    result = []
    for item in items:
        item.setdefault("enabled", True)
        if item["enabled"]:
            result.append(item)
    return result


def capture_slots(industry: str) -> list[dict[str, Any]]:
    """返回行业动态采集位，固定不超过八个。"""
    key = _industry(industry)
    slots = [copy.deepcopy(slot) for slot in (*_COMMON_SLOTS, *_INDUSTRY_SLOTS[key])]
    if len(slots) > 8:  # 数据守卫，防止后续编辑破坏上传合同。
        raise InspectionStandardError("行业采集位超过 8 个")
    return slots


def metric_catalog(industry: str) -> list[dict[str, Any]]:
    """返回指标口径，不计算、不推测、不附会行业基准。"""
    key = _industry(industry)
    return [copy.deepcopy(metric) for metric in (*_COMMON_METRICS, *_INDUSTRY_METRICS[key])]


def source_registry() -> dict[str, dict[str, str]]:
    """返回官方标准/法规来源注册表的防污染副本。"""
    return copy.deepcopy(_SOURCES)


def catalog_items() -> list[dict[str, Any]]:
    """返回去重后的底层项定义，用于导出和合同验证。"""
    values = list(_COMMON_ITEMS)
    for industry in INDUSTRIES:
        values.extend(_INDUSTRY_ITEMS[industry])
    return copy.deepcopy(values)


def catalog_slots() -> list[dict[str, Any]]:
    """返回去重后的底层采集位定义。"""
    values = list(_COMMON_SLOTS)
    for industry in INDUSTRIES:
        values.extend(_INDUSTRY_SLOTS[industry])
    return copy.deepcopy(values)


def version_summary(industry: str | None = None) -> dict[str, Any]:
    """生成可比对的稳定版本摘要和 SHA-256 快照。"""
    if industry is None:
        industries = list(INDUSTRIES)
        payload: dict[str, Any] = {
            "items": catalog_items(),
            "slots": catalog_slots(),
            "metrics": {key: metric_catalog(key) for key in INDUSTRIES},
            "sources": source_registry(),
        }
    else:
        key = _industry(industry)
        industries = [key]
        payload = {
            "items": effective_checklist(key),
            "slots": capture_slots(key),
            "metrics": metric_catalog(key),
            "sources": source_registry(),
        }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "catalog_version": CATALOG_VERSION,
        "as_of": CATALOG_AS_OF,
        "industry": industry,
        "industries": industries,
        "item_count": len(payload["items"]),
        "slot_count": len(payload["slots"]),
        "metric_count": (
            sum(len(items) for items in payload["metrics"].values())
            if industry is None else len(payload["metrics"])
        ),
        "source_count": len(payload["sources"]),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


# 直观别名，便于 HTTP 层或后续导出器使用，核心合同仍只有上述函数。
get_capture_slots = capture_slots
operating_metrics = metric_catalog
standards_version = version_summary


__all__ = [
    "CATALOG_AS_OF", "CATALOG_VERSION", "INDUSTRIES", "OVERRIDE_LAYERS",
    "TIERS", "InspectionStandardError", "StandardOverrideError",
    "UnknownIndustryError", "catalog_items", "catalog_slots",
    "capture_slots", "effective_checklist", "get_capture_slots",
    "metric_catalog", "operating_metrics", "source_registry",
    "standards_version", "version_summary",
]
