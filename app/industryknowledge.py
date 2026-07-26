"""行业知识底座:给每个产业部员工注入真实的行业口径、基准与合规要点。

为什么需要这一层:
岗位手册(md)是一套通用的连锁总部方法论,10 个模板行业之间只替换了行业名、
业务从句、关键口径清单和合规领域清单四处——方法论本身完全相同。这让"行业专家"
实际只是"通用顾问换了个称呼",手册里也没有任何一条真实的行业基准值。

这一层把可核查的行业知识独立出来按需注入:
- metrics   指标定义与计算公式(确定性行业知识,不是估计值)
- benchmarks参考区间,**每条必须带来源、适用范围与时点**;没有来源的一律不收
- glossary  行内术语,让员工说行话而不是通用商业词
- practices 该行业区别于通用零售/服务业的经营动作
- compliance中国大陆合规要点(法规名 + 主要要求 + 检索时点)
- pitfalls  真实的经营失败原因

注入纪律(见 discipline_note):参考区间只是校准起点,与老板提供的实际数据冲突时
以实际数据为准,超出适用范围不得套用。这与手册自身"禁止无来源行业基准"的要求一致。
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("industryknowledge")

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(_ROOT, "config", "industry_knowledge")
LEGACY_DIR = os.path.join(_ROOT, "data", "industry_knowledge")

# 单个员工注入的上限:知识底座是背景资料,不能挤掉岗位手册与老板的任务书。
MAX_METRICS = 10
MAX_BENCHMARKS = 8
MAX_GLOSSARY = 14
MAX_PRACTICES = 6
MAX_COMPLIANCE = 6
MAX_PITFALLS = 6
MAX_BLOCK_CHARS = 4200

_cache: dict | None = None


def _dir() -> str:
    return CONFIG_DIR if os.path.isdir(CONFIG_DIR) else LEGACY_DIR


def _load() -> dict:
    """读取全部行业知识;单个文件损坏不拖垮其它行业(缺失即不注入)。"""
    global _cache
    if _cache is not None:
        return _cache
    out: dict = {}
    directory = _dir()
    if os.path.isdir(directory):
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(directory, filename)
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    value = json.load(handle)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                log.error("行业知识无法读取 file=%s error_type=%s",
                          filename, type(exc).__name__)
                continue
            key = value.get("key") if isinstance(value, dict) else None
            if key:
                out[str(key)] = value
    _cache = out
    return out


def reset_cache() -> None:
    global _cache
    _cache = None


def get(industry_key: str) -> dict:
    return _load().get(str(industry_key or "")) or {}


def available() -> list:
    return sorted(_load())


def _terms(text: str) -> set:
    """轻量中英文取词,用于把知识条目与当前岗位做相关性匹配。"""
    normalized = re.sub(r"\s+", " ", str(text or "")).lower()
    terms = set(re.findall(r"[a-z][a-z0-9_/&-]{1,30}", normalized))
    for chunk in re.findall(r"[一-鿿]{2,}", normalized):
        terms.add(chunk)
        terms.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return {t for t in terms if len(t) >= 2}


def _rank(items: list, query: set, fields: tuple, limit: int) -> list:
    """按与岗位的词面重合度排序;完全不相关时保留原始顺序的前几条兜底。"""
    if not items:
        return []
    scored = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get(f) or "") for f in fields)
        score = len(query & _terms(text))
        scored.append((score, -position, item))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    picked = [item for score, _pos, item in scored if score > 0][:limit]
    if not picked:
        picked = [item for _s, _p, item in scored[:min(3, limit)]]
    return picked


def discipline_note() -> str:
    return (
        "以下行业资料用于校准口径与常识,不替代老板给的实际数据:"
        "①参考区间附来源与适用范围,只作为量级参照,必须结合本店实际重新测算;"
        "②与老板提供的数据冲突时,一律以老板的数据为准并指出差异;"
        "③适用范围不匹配(城市线级/业态/店型不同)时不得套用,应说明为何不适用;"
        "④引用区间时要连来源一起写出,不得把参考值写成本店结论。"
    )


def block_for(industry_key: str, role_text: str = "") -> str:
    """按岗位相关性组装该行业的知识块;没有资料时返回空串。"""
    data = get(industry_key)
    if not data:
        return ""
    query = _terms(role_text)
    parts: list = []

    metrics = _rank(data.get("metrics") or [], query,
                    ("name", "formula", "why", "trap"), MAX_METRICS)
    if metrics:
        lines = []
        for m in metrics:
            line = f"- {m.get('name', '')}:{m.get('formula', '')}"
            if m.get("trap"):
                line += f"(口径注意:{m['trap']})"
            lines.append(line)
        parts.append("【本行业核心指标与计算口径】\n" + "\n".join(lines))

    benchmarks = _rank(data.get("benchmarks") or [], query,
                       ("metric", "scope", "note"), MAX_BENCHMARKS)
    if not benchmarks:
        # 没有获得授权数据源时,基准区间是空的。必须让员工知道这件事,
        # 否则它会"凭印象"编一个行业均值——那正是手册明令禁止的。
        parts.append(
            "【行业参考区间:本系统暂未接入授权数据源】\n"
            "不得凭记忆或印象给出任何行业均值、基准线或"
            "「行业一般为 X%」式表述。需要基准时,只能:"
            "①用老板提供的历史数据自行测算;②明确列为「待补数据」并说明"
            "获取方式(如向行业协会/数据服务商采购、同行访谈、自有多店对比)。"
        )
    if benchmarks:
        lines = []
        for b in benchmarks:
            # 来源与适用范围是这条数据能不能用的前提,必须一起给出。
            lines.append(
                f"- {b.get('metric', '')}:{b.get('range', '')}"
                f" | 适用:{b.get('scope', '未标注')}"
                f" | 来源:{b.get('source', '未标注')}"
                f"{'(' + str(b['as_of']) + ')' if b.get('as_of') else ''}"
            )
        parts.append(
            "【行业参考区间(仅作量级参照,须按本店实际校准并注明来源)】\n"
            + "\n".join(lines)
        )

    glossary = _rank(data.get("glossary") or [], query,
                     ("term", "meaning"), MAX_GLOSSARY)
    if glossary:
        parts.append(
            "【行内术语(产出中优先使用,不要用通用商业词代替)】\n"
            + "、".join(
                f"{g.get('term', '')}({g.get('meaning', '')})" for g in glossary
            )
        )

    practices = data.get("practices") or []
    practices = [p for p in practices if str(p).strip()][:MAX_PRACTICES]
    if practices:
        parts.append(
            "【本行业区别于通用零售/服务业的经营要点】\n"
            + "\n".join(f"- {p}" for p in practices)
        )

    compliance = _rank(data.get("compliance") or [], query,
                       ("name", "requirement"), MAX_COMPLIANCE)
    if compliance:
        lines = []
        for c in compliance:
            lines.append(
                f"- {c.get('name', '')}:{c.get('requirement', '')}"
                f"{'(资料时点 ' + str(c['as_of']) + ',引用前需复核现行有效性)' if c.get('as_of') else ''}"
            )
        parts.append("【中国大陆合规要点】\n" + "\n".join(lines))

    pitfalls = data.get("pitfalls") or []
    pitfalls = [p for p in pitfalls if str(p).strip()][:MAX_PITFALLS]
    if pitfalls:
        parts.append(
            "【本行业常见的经营陷阱(方案里要主动规避并说明如何规避)】\n"
            + "\n".join(f"- {p}" for p in pitfalls)
        )

    if not parts:
        return ""
    body = "\n\n".join(parts)
    if len(body) > MAX_BLOCK_CHARS:
        body = body[:MAX_BLOCK_CHARS].rsplit("\n", 1)[0]
    name = data.get("name") or industry_key
    return (
        f"\n【{name}·行业知识底座】\n"
        f"{discipline_note()}\n\n{body}\n"
    )
