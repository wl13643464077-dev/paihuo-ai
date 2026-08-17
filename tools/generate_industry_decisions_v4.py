#!/usr/bin/env python3
"""Generate the immutable V4 industry decision catalogs.

V4 is intentionally derived from the reviewed V3 contracts instead of editing
V3 in place.  The only catalog-level identity changes here are the versioned
employee key and a synthetic, globally unique person display name.  The
professional profile and decision contract are carried byte-for-byte at the
JSON value level so downstream migration code can compare V3/V4 contracts.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V3_DIR = ROOT / "data" / "industry_decisions_v3"
V4_DIR = ROOT / "data" / "industry_decisions_v4"
CATALOG_VERSION = "2026.08.v4"
SOURCE_VERSION = CATALOG_VERSION

# This is a guardrail, not a generated value: the V3 release is a historical
# artifact and must remain byte-stable while V4 is generated.
V3_SHA256 = {
    "auto.json": "9bf03098b786b8dcaac82117a888d9d75fb6efb433cc10774d21abc9c57b70f6",
    "beauty.json": "51ec95d564df7865bfc43caf852415bb9b83444f9c3247b306aad88077c71c69",
    "convenience.json": "4a5b10cd89549d072cc600e3fda8853d0d401ad65d9c69ae25bb4bfd645d6802",
    "fitness.json": "4fc22e99c04590aa81a6ca2401e7d2f9ba08ce13717f2dea5b50eea25569b215",
    "grocery.json": "9f45022e57f6cd8660615922b8e02a67bfff9d2a3c81c2c22700539bab5d7266",
    "hotel.json": "1537fdce7b00ec6796807317d438f74480e26a415a9dbc41f7e2a16393d3e02b",
    "pet.json": "687ded5855298d6055da13327c29bcc935a48698467d293a71f28c4ccbee443a",
    "pharmacy.json": "db7d7b6bebcb93295d2509447c93fb7c70621490074c342bec1af547cc31bf93",
    "snack.json": "b8ea41241de1702791ccf8e2c1cbfdca9c04be47dd665154934b1400b53ba16a",
    "tea_coffee.json": "c3604e168ebb0ccf6bf5b2263ced418de89093f23c5cc478d653159a7835e7a5",
}


def _public_research_topics(employee: dict) -> list[str]:
    """Return bounded, explicitly public role topics for external search.

    The research gateway must never infer its outbound brief from a private
    config, employee name, tenant data, prompt or workflow.  V4 therefore
    freezes a small public taxonomy in the catalog itself: the public job
    title plus reviewed learning/knowledge headings already intended for the
    employee card.  Runtime code may send only these values to WebSearch.
    """
    profile = employee.get("professional_profile")
    profile = profile if isinstance(profile, dict) else {}
    candidates = [
        employee.get("name"),
        *(profile.get("learning_tracks") or []),
        *(profile.get("knowledge_domains") or []),
    ]
    result = []
    for raw in candidates:
        value = re.sub(
            r"[^0-9A-Za-z+./\-\u4e00-\u9fff ]", " ", str(raw or "")
        )
        value = re.sub(r"\s+", " ", value).strip()[:80]
        if len(value) < 2 or value in result:
            continue
        result.append(value)
        if len(result) >= 6:
            break
    if len(result) < 3:
        raise ValueError(f"employee {employee.get('idx')} lacks public research topics")
    return result


_ANCHOR_STOP = {
    "行业", "业务", "服务", "方法", "流程", "分析", "管理", "识别",
    "执行", "核验", "审查", "确认", "学习", "案例", "规则", "版本",
    "岗位", "数据", "系统", "门店", "对象", "记录", "清单", "输出",
}


def _anchor_candidates(values, *, minimum: int, limit: int) -> list[str]:
    """Freeze bounded public semantic anchors, not runtime-invented n-grams.

    The generator turns already-reviewed public topics and business objects
    into an explicit catalog contract. Runtime may only consume these frozen
    values and must require an object+method pair.
    """
    scored: dict[str, int] = {}

    def add(value: str, score: int) -> None:
        value = value.strip().lower()
        if (
            len(value) < minimum or len(value) > 40
            or value in _ANCHOR_STOP
            or re.fullmatch(r"[0-9]+", value)
        ):
            return
        scored[value] = max(scored.get(value, -1), score)

    for raw in values or []:
        clean = re.sub(
            r"[^0-9A-Za-z+./\-一-鿿 ]", " ", str(raw or "")
        )
        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean:
            continue
        add(clean, 100 + min(len(clean), 30))
        for ascii_word in re.findall(r"[A-Za-z][A-Za-z0-9+./-]{1,}", clean):
            add(ascii_word, 95 + len(ascii_word))
        for segment in re.split(r"[与和及或的、/ ]+", clean):
            segment = segment.strip()
            if len(segment) < minimum:
                continue
            add(segment, 90 + min(len(segment), 20))
            # Remove only reviewed generic wrappers at word boundaries. Never
            # emit arbitrary internal n-grams such as “训练支持”.
            for suffix in (
                "场景应用", "应用", "实训", "训练", "演练", "案例复盘",
                "案例精读", "案例", "规范", "要求", "实务", "时间戳",
                "逐小时预报", "起止", "日志", "记录", "状态", "清单",
                "台账", "看板", "队列", "快照", "映射", "计划基线",
            ):
                if segment.endswith(suffix):
                    add(segment[:-len(suffix)], 88 + len(segment))
    return [
        value for value, _score in sorted(
            scored.items(), key=lambda item: (-item[1], -len(item[0]), item[0])
        )[:limit]
    ]


def _public_research_anchor_groups(employee: dict, topics: list[str]) -> list[dict]:
    """Pair public decision objects with each professional research method."""
    profile = employee.get("professional_profile")
    profile = profile if isinstance(profile, dict) else {}
    contract = employee.get("decision_contract")
    contract = contract if isinstance(contract, dict) else {}
    object_values = [
        *(profile.get("data_objects") or []),
        *[
            value for value in (contract.get("required_inputs") or [])
            if "候选规则" not in str(value) and "批准边界" not in str(value)
        ][:5],
    ]
    object_anchors = [
        value for value in _anchor_candidates(object_values, minimum=2, limit=30)
        if len(value) >= 3
    ][:24]
    groups = []
    job_title = str(employee.get("name") or "").strip()
    for topic in topics:
        if topic == job_title:
            continue
        method_anchors = _anchor_candidates([topic], minimum=3, limit=10)
        # An anchor present in both sets cannot prove the business object and
        # the method independently (e.g. “恢复” inside “延误恢复优先级”).
        object_only = [
            value for value in object_anchors
            if value not in method_anchors
            and all(value not in method for method in method_anchors)
            and all(method not in value for method in method_anchors)
        ]
        if not object_only or not method_anchors:
            raise ValueError(
                f"employee {employee.get('idx')} lacks public research anchor group"
            )
        groups.append({
            "topic": topic,
            "object_anchors": object_only,
            "method_anchors": method_anchors,
        })
    if len(groups) < 2:
        raise ValueError(f"employee {employee.get('idx')} lacks anchor group diversity")
    return groups


# 120 real Chinese surname characters.  They are used only as a deterministic
# synthetic roster seed; no industry term is embedded in a person name.
SURNAMES = list(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞"
)

# Common given names.  The generator uses a stable permutation and selects
# three different names per surname before moving to the next surname.
GIVEN_NAMES = """
知遥 予安 清妍 沐川 若宁 书瑶 言蹊 景澄 星野 嘉树 以南 怀瑾 栩然 思远 明澈 云舒 乐言 昭阳 屿舟 闻溪
向晚 新月 安澜 亦航 清越 书宁 知夏 景行 予墨 念慈 砚秋 澄意 清和 若初 言希 星河 嘉禾 以宁 怀川 栩嘉
思齐 明远 云起 乐知 昭宁 屿安 闻舟 向野 新晴 安歌 亦辰 清嘉 书衡 知微 景铄 予川 念安 砚宁 澄远 清妤
若岚 言序 星遥 嘉言 以恒 怀序 栩宁 思言 明礼 云帆 乐成 昭文 屿森 闻韬 向晨 新知 安禾 亦宁 清朗 书昀
知衡 景尧 予澄 念初 砚舟 澄怀 清韵 若溪 言澈 星宁 嘉瑞 以安 怀宁 栩川 思源 明川 云峥 乐游 昭远 屿辰
闻远 向荣 新安 安予 亦清 清言 书远 知行 景昀 予珩 念云 砚清 澄宁 清允 若嘉 言舟 星澜 嘉实 以澄 怀远
栩墨 思衡 明知 云宁 乐谦 昭晖 屿川 闻川 向宁 新尧 安和 亦舒 清宁 书言 知澜 景宁 予昕 念舟 砚阳
澄秋 清野 若恒 言安 星昀 嘉宁 以晨 怀谦 栩清 思宁 明轩 云知 乐川 昭辰 屿宁 闻昕 向安 新远 安晴 亦澄
""".split()

# A compact pronunciation table covers the common characters used above.  A
# rare surname/given character falls back to ``zi`` and remains machine-review
# pending in name_registry instead of pretending it was human-reviewed.
PINYIN_BY_CHAR = {
    "赵": "zhao", "钱": "qian", "孙": "sun", "李": "li", "周": "zhou", "吴": "wu", "郑": "zheng", "王": "wang", "冯": "feng", "陈": "chen", "褚": "chu", "卫": "wei", "蒋": "jiang", "沈": "shen", "韩": "han", "杨": "yang", "朱": "zhu", "秦": "qin", "尤": "you", "许": "xu", "何": "he", "吕": "lv", "施": "shi", "张": "zhang", "孔": "kong", "曹": "cao", "严": "yan", "华": "hua", "金": "jin", "魏": "wei", "陶": "tao", "姜": "jiang", "戚": "qi", "谢": "xie", "邹": "zou", "喻": "yu", "柏": "bai", "水": "shui", "窦": "dou", "章": "zhang", "云": "yun", "苏": "su", "潘": "pan", "葛": "ge", "奚": "xi", "范": "fan", "彭": "peng", "郎": "lang", "鲁": "lu", "韦": "wei", "昌": "chang", "马": "ma", "苗": "miao", "凤": "feng", "花": "hua", "方": "fang", "俞": "yu", "任": "ren", "袁": "yuan", "柳": "liu", "酆": "feng", "鲍": "bao", "史": "shi", "唐": "tang", "费": "fei", "廉": "lian", "岑": "cen", "薛": "xue", "雷": "lei", "贺": "he", "倪": "ni", "汤": "tang", "滕": "teng", "殷": "yin", "罗": "luo", "毕": "bi", "郝": "hao", "邬": "wu", "安": "an", "常": "chang", "乐": "le", "于": "yu", "时": "shi", "傅": "fu", "皮": "pi", "卞": "bian", "齐": "qi", "康": "kang", "伍": "wu", "余": "yu", "元": "yuan", "卜": "bu", "顾": "gu", "孟": "meng", "平": "ping", "黄": "huang", "和": "he", "穆": "mu", "萧": "xiao", "尹": "yin", "姚": "yao", "邵": "shao", "湛": "zhan", "汪": "wang", "祁": "qi", "毛": "mao", "禹": "yu", "狄": "di", "米": "mi", "贝": "bei", "明": "ming", "臧": "zang", "计": "ji", "伏": "fu", "成": "cheng", "戴": "dai", "谈": "tan", "宋": "song", "茅": "mao", "庞": "pang",
    "知": "zhi", "遥": "yao", "予": "yu", "清": "qing", "妍": "yan", "沐": "mu", "川": "chuan", "若": "ruo", "宁": "ning", "书": "shu", "瑶": "yao", "言": "yan", "蹊": "qi", "景": "jing", "澄": "cheng", "星": "xing", "野": "ye", "嘉": "jia", "树": "shu", "以": "yi", "南": "nan", "怀": "huai", "瑾": "jin", "栩": "xu", "然": "ran", "思": "si", "远": "yuan", "明": "ming", "澈": "che", "云": "yun", "舒": "shu", "昭": "zhao", "阳": "yang", "屿": "yu", "舟": "zhou", "闻": "wen", "溪": "xi", "向": "xiang", "晚": "wan", "新": "xin", "月": "yue", "澜": "lan", "安": "an", "亦": "yi", "航": "hang", "越": "yue", "夏": "xia", "行": "xing", "墨": "mo", "念": "nian", "慈": "ci", "砚": "yan", "秋": "qiu", "意": "yi", "和": "he", "初": "chu", "希": "xi", "河": "he", "禾": "he", "齐": "qi", "起": "qi", "歌": "ge", "辰": "chen", "衡": "heng", "微": "wei", "铄": "shuo", "序": "xu", "恒": "heng", "宁": "ning", "姝": "shu", "岚": "lan", "妤": "yu", "文": "wen", "昕": "xin", "朗": "lang", "礼": "li", "谦": "qian", "韵": "yun", "嘉": "jia", "言": "yan", "实": "shi", "帆": "fan", "森": "sen", "韬": "tao", "晨": "chen", "允": "yun", "瑞": "rui", "源": "yuan", "峥": "zheng", "游": "you", "荣": "rong", "晴": "qing", "珩": "heng", "昀": "yun", "晖": "hui", "尧": "yao", "秋": "qiu", "轩": "xuan",
}


def _canonical_pinyin(person: str) -> str:
    return "-".join(PINYIN_BY_CHAR.get(char, "zi") for char in person)


def _version_key(value: str) -> str:
    value = value.replace("-v3-", "-v4-", 1)
    return re.sub(r"(?<![a-z0-9])v3(?![a-z0-9])", "v4", value, count=1)


def _all_employees(catalogs: list[tuple[Path, dict]]) -> list[dict]:
    return sorted(
        [employee for _, catalog in catalogs for employee in catalog["employees"]],
        key=lambda employee: int(employee["idx"]),
    )


def _assign_people(catalogs: list[tuple[Path, dict]]) -> dict[int, dict[str, str | int | bool]]:
    employees = _all_employees(catalogs)
    if len(employees) != 360 or len(SURNAMES) != 120:
        raise ValueError(f"expected 360 employees and 120 surnames; got {len(employees)} / {len(SURNAMES)}")
    given_order = list(GIVEN_NAMES)
    random.Random(20260813).shuffle(given_order)
    registry: dict[int, dict[str, str | int | bool]] = {}
    used: set[str] = set()
    for ordinal, employee in enumerate(employees):
        surname = SURNAMES[ordinal % len(SURNAMES)]
        for offset in range(len(given_order)):
            given = given_order[(ordinal * 7 + offset) % len(given_order)]
            person = surname + given
            if person not in used:
                used.add(person)
                break
        else:  # pragma: no cover - guarded by the large given-name pool
            raise ValueError(f"unable to allocate a unique person for idx={employee['idx']}")
        registry[int(employee["idx"])] = {
            "idx": int(employee["idx"]),
            "person": person,
            "canonical_pinyin": _canonical_pinyin(person),
            "synthetic": True,
            "source_version": SOURCE_VERSION,
            "reviewed": "machine_review_pending",
        }
    if len(registry) != len(employees) or len({entry["person"] for entry in registry.values()}) != len(employees):
        raise AssertionError("V4 person registry is not globally unique")
    return registry


def _version_contract_keys(value):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key == "key" and isinstance(child, str) and "v3" in child:
                result[key] = _version_key(child)
            else:
                result[key] = _version_contract_keys(child)
        return result
    if isinstance(value, list):
        return [_version_contract_keys(item) for item in value]
    return value


def _build_catalogs() -> list[tuple[str, dict]]:
    sources = sorted(V3_DIR.glob("*.json"))
    if len(sources) != 10:
        raise ValueError(f"expected 10 V3 catalogs, found {len(sources)}")
    catalogs = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in sources]
    registry = _assign_people(catalogs)
    result: list[tuple[str, dict]] = []
    for path, original in catalogs:
        catalog = copy.deepcopy(original)
        catalog["catalog_version"] = CATALOG_VERSION
        catalog["identity_policy"] = (
            "V4使用全局唯一的合成中文姓名承载当前员工显示身份；原idx、岗位合同和V3历史版本保持可回放，"
            "姓名登记表标记为machine_review_pending，人工复核前不得冒充真人经历。"
        )
        catalog["selection_policy"] = (
            "沿用V3按痛点严重度、使用频率、经济价值和企业数据可得性筛选的36个专属岗位；V4只升级显示身份与版本化键。"
        )
        catalog["employees"] = _version_contract_keys(catalog["employees"])
        for employee in catalog["employees"]:
            entry = registry[int(employee["idx"])]
            employee["person"] = entry["person"]
            topics = _public_research_topics(employee)
            employee["public_research_topics"] = topics
            employee["public_research_anchor_groups"] = (
                _public_research_anchor_groups(employee, topics)
            )
        # Keep the registry ordered exactly like the employee list so reviewers
        # can diff one file without an index lookup.
        catalog["name_registry"] = [registry[int(employee["idx"])] for employee in catalog["employees"]]
        result.append((path.name, catalog))
    return result


def _render(catalog: dict) -> bytes:
    return (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _check_v3() -> list[str]:
    errors = []
    for name, expected in V3_SHA256.items():
        path = V3_DIR / name
        if not path.exists():
            errors.append(f"missing V3 file: {path}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"V3 byte drift: {name} expected {expected}, got {actual}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify generated bytes and V3 immutability without writing")
    args = parser.parse_args(argv)
    errors = _check_v3()
    generated = _build_catalogs()
    if args.check:
        for name, catalog in generated:
            path = V4_DIR / name
            expected = _render(catalog)
            if not path.exists():
                errors.append(f"missing V4 file: {path}")
            elif path.read_bytes() != expected:
                errors.append(f"V4 byte drift: {name}; run generator to regenerate")
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(f"OK: {len(generated)} V4 catalogs byte-stable; V3 SHA256 unchanged")
        return 0
    V4_DIR.mkdir(parents=True, exist_ok=True)
    for name, catalog in generated:
        (V4_DIR / name).write_bytes(_render(catalog))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"WROTE: {len(generated)} V4 catalogs to {V4_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
