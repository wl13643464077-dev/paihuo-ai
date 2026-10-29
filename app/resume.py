"""数字员工「人设履历」：把岗位档案翻译成招聘网站式的简历。

全部内容由员工自己的岗位档案（技能树、核心能力、负责决策、工作流程、
必须输入、交付物）确定性生成，过往雇主一律匿名；页面固定展示「人设履历」
说明，绝不冒充真人经历，也不编造业绩数字。手写样稿放在
``resume_overrides_v1.json``（按员工 key），存在时整体优先。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

log = logging.getLogger("resume")

OVERRIDES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "resume_overrides_v1.json"
)
_overrides_cache = None

FOOTNOTE = (
    "人设履历：经历与公司为岗位能力的拟人化示意（公司已匿名），不代表真人经历；"
    "能力以岗位档案与实际交付为准，所有结论都需要你拍板。"
)

# 每个行业三类匿名雇主：近期 → 早期
_EMPLOYERS = {
    "restaurant": ("某全国连锁餐饮品牌", "某中央厨房团餐公司", "某五星酒店餐饮部"),
    "tea_coffee": ("某全国连锁茶饮品牌", "某精品咖啡连锁", "某区域烘焙茶饮店"),
    "convenience": ("某全国连锁便利店品牌", "某区域便利店连锁", "某商超集团便利业态"),
    "snack": ("某头部零食量贩品牌", "某区域零食连锁", "某休闲食品供应商"),
    "grocery": ("某全国连锁生鲜商超", "某社区生鲜连锁", "某即时零售平台前置仓"),
    "pharmacy": ("某上市连锁药房", "某区域医药连锁", "某医药流通企业"),
    "hotel": ("某国际连锁酒店集团", "某中高端连锁酒店", "某精品民宿品牌"),
    "auto": ("某全国汽车后市场连锁", "某品牌授权 4S 集团", "某区域快修连锁"),
    "fitness": ("某全国连锁健身品牌", "某精品私教工作室连锁", "某综合体育场馆"),
    "beauty": ("某全国连锁美容机构", "某高端美业会所", "某美妆零售连锁"),
    "pet": ("某全国连锁宠物医院集团", "某区域宠物洗护连锁", "某宠物用品零售企业"),
    "content": ("某头部 MCN 机构", "某品牌内容营销公司", "某本地生活服务平台"),
}
_MAJORS = {
    "restaurant": ("烹饪与营养教育", "财务管理", "食品质量与安全"),
    "tea_coffee": ("食品科学与工程", "市场营销", "连锁经营管理"),
    "convenience": ("连锁经营管理", "市场营销", "物流管理"),
    "snack": ("市场营销", "供应链管理", "食品质量与安全"),
    "grocery": ("食品质量与安全", "物流管理", "市场营销"),
    "pharmacy": ("药学", "医药营销", "临床药学"),
    "hotel": ("酒店管理", "旅游管理", "市场营销"),
    "auto": ("汽车服务工程", "车辆工程", "市场营销"),
    "fitness": ("运动训练", "体育教育", "运动康复"),
    "beauty": ("医学美容技术", "化妆品技术", "市场营销"),
    "pet": ("动物医学", "动物科学", "市场营销"),
    "content": ("新闻传播学", "广告学", "数字媒体艺术"),
}
_SCHOOL_TIER = ("本科", "本科", "硕士", "本科")


def _overrides() -> dict:
    global _overrides_cache
    if _overrides_cache is None:
        data = {}
        try:
            with open(OVERRIDES_PATH, encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                data = raw
        except FileNotFoundError:
            pass
        except ValueError:
            log.warning("resume overrides unreadable; using generated resumes")
        _overrides_cache = data
    return _overrides_cache


PLAIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume_plain")
_plain_cache = None


def _plain() -> dict:
    """人工逐个写的大白话介绍（按员工 key），存在时覆盖模板生成的同名字段。"""
    global _plain_cache
    if _plain_cache is None:
        data = {}
        try:
            names = sorted(n for n in os.listdir(PLAIN_DIR) if n.endswith(".json"))
        except OSError:
            names = []
        for name in names:
            try:
                with open(os.path.join(PLAIN_DIR, name), encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, ValueError):
                log.warning("plain resume file unreadable: %s", name)
                continue
            if isinstance(raw, dict):
                data.update({k: v for k, v in raw.items() if isinstance(v, dict)})
        _plain_cache = data
    return _plain_cache


def _apply_plain(result: dict, plain: dict, job: str) -> dict:
    one = str(plain.get("one") or "").strip()
    what = str(plain.get("what") or "").strip()
    how = str(plain.get("how") or "").strip()
    when = [w for w in plain.get("when") or [] if isinstance(w, dict) and w.get("when")]
    value = [str(v).strip() for v in plain.get("value") or [] if str(v).strip()]
    past = [p for p in plain.get("past") or [] if isinstance(p, dict) and p.get("story")]
    periods = [job_row.get("period") for job_row in result.get("experiences") or []]
    out = dict(result)
    out["plain"] = {"one": one, "what": what, "how": how, "when": when, "value": value}
    if one:
        out["one"] = one
        out["headline"] = f"{job}｜{one}"
    if what:
        out["summary"] = what
    if when:
        out["scenes"] = when
    if value:
        out["why"] = value
    first = (result.get("cautions") or [""])[0]
    out["cautions"] = [c for c in (
        str(plain.get("note") or "").strip() or first,
        "TA 只出建议和清单，不会动你的系统和钱，最后怎么做由你拍板。",
    ) if c]
    if plain.get("tags"):
        out["tags"] = [str(t) for t in plain["tags"]][:4]
    if past:
        out["experiences"] = [
            {
                "company": str(p.get("company") or "某企业"),
                "title": str(p.get("title") or ""),
                "period": periods[i] if i < len(periods) else "",
                "duties": [],
                "wins": [str(p["story"])],
            }
            for i, p in enumerate(past[:3])
        ]
        out["projects"] = []
        out["highlights"] = []
    return out


def _seed(key: str) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


def _strings(value, limit: int | None = None) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
    return out[:limit] if limit else out


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    cut = re.split(r"[，。；;,]", text[:limit])[0]
    return (cut if len(cut) >= limit // 2 else text[:limit]).rstrip("，。；;, ") + "…"


def _titles(job: str) -> tuple[str, str, str]:
    job = job.strip()
    if re.search(r"(官|专家|师|员)$", job):
        domain = re.sub(r"(官|专家|师|员)$", "", job) or job
        return f"{domain}负责人", f"{domain}主管", f"{domain}专员"
    return f"{job} · 负责人", f"{job} · 主管", f"{job} · 专员"


def _clean_rationale(text: str) -> str:
    return re.sub(r"^(业务)?入选点在于(负责)?", "", str(text or "").strip())


def _step_head(step: str) -> str:
    return re.split(r"[：:，,；;。]", str(step or "").strip(), maxsplit=1)[0].strip()


def build(employee: dict, profile: dict | None = None,
          capability_details: dict | None = None) -> dict:
    """Return a résumé dict for one active or historical employee."""
    if not isinstance(employee, dict):
        return {}
    key = str(employee.get("key") or employee.get("idx") or "")
    override = _overrides().get(key)
    if isinstance(override, dict) and override:
        return {**override, "footnote": override.get("footnote") or FOOTNOTE,
                "source": "override",
                "one": override.get("one") or str(override.get("headline") or "").split("｜")[-1]}

    profile = profile if isinstance(profile, dict) else {}
    details = capability_details if isinstance(capability_details, dict) else {}
    contract = employee.get("decision_contract") or {}
    dept_key = str(employee.get("dept_key") or "content")
    job = str(employee.get("name") or "数字员工").strip()
    titles = _titles(job)
    seed = _seed(key)

    skills = _strings(profile.get("skill_tree")) or [
        _step_head(step) for step in _strings(employee.get("steps"))
    ]
    caps = _strings(profile.get("capabilities"))
    knowledge = _strings(profile.get("knowledge_domains"))
    data_objects = _strings(profile.get("data_objects"))
    decisions = _strings(profile.get("decisions"))
    workflow = _strings(contract.get("workflow")) or [
        _step_head(step) for step in _strings(employee.get("steps"))
    ]
    outputs = _strings(contract.get("outputs")) or _strings(employee.get("deliverables"))
    inputs = _strings(contract.get("required_inputs")) or _strings(employee.get("inputs"))
    learning = _strings(profile.get("learning_tracks"))
    metrics = [
        str(m.get("name") or "").split("·")[-1].strip()
        for m in contract.get("success_metrics") or [] if isinstance(m, dict)
    ]
    metrics = [m for m in metrics if m]
    scope = str(profile.get("scope") or employee.get("desc") or employee.get("duty") or "").strip()
    rationale = _clean_rationale(employee.get("selection_rationale"))
    primary = str(contract.get("decision") or employee.get("primary_decision") or "").strip()

    years = 8 + seed % 5
    end_year = 2026
    employers = _EMPLOYERS.get(dept_key, _EMPLOYERS["content"])
    major = _MAJORS.get(dept_key, _MAJORS["content"])[seed % 3]
    degree = _SCHOOL_TIER[seed % len(_SCHOOL_TIER)]
    grad_year = end_year - years - 1

    split1 = end_year - 1 - (seed % 3) - 4
    split2 = grad_year + 1 + (seed >> 3) % 2 + 1
    split2 = min(split2, split1 - 2)
    periods = [
        (f"{split1}.0{1 + seed % 9}", f"{end_year}.0{1 + (seed >> 4) % 7}"),
        (f"{split2}.0{1 + (seed >> 2) % 9}", f"{split1}.0{1 + seed % 9}"),
        (f"{grad_year + 1}.07", f"{split2}.0{1 + (seed >> 2) % 9}"),
    ]

    def duty_lines(chunk: list[str], verb: str) -> list[str]:
        return [f"{verb}{item}" for item in chunk if item]

    win_pool = (
        [f"建立「{cap}」工作机制，形成可复核的交付标准" for cap in caps]
        + [f"把「{m}」纳入月度复盘口径并持续跟踪" for m in metrics]
    )
    experiences = [
        {
            "company": employers[0], "title": titles[0],
            "period": f"{periods[0][0]} – {periods[0][1]}",
            "duties": duty_lines(skills[:3], "负责"),
            "wins": win_pool[:2] or duty_lines(outputs[:2], "交付"),
        },
        {
            "company": employers[1], "title": titles[1],
            "period": f"{periods[1][0]} – {periods[1][1]}",
            "duties": duty_lines(skills[3:6] or skills[:2], "主导"),
            "wins": win_pool[2:4] or duty_lines(outputs[:2], "牵头交付"),
        },
        {
            "company": employers[2], "title": titles[2],
            "period": f"{periods[2][0]} – {periods[2][1]}",
            "duties": duty_lines(data_objects[:3] or inputs[:3], "整理与核对") or ["基础数据整理与台账维护"],
            "wins": [f"打下{knowledge[0]}基本功" if knowledge else "打下岗位基本功"],
        },
    ]

    project_goals = decisions or [employee.get("duty") or job]
    projects = []
    for n, goal in enumerate(project_goals[:3]):
        how = workflow[n:n + 3] if len(workflow) > 3 else workflow
        projects.append({
            "name": _short(goal, 26),
            "goal": goal,
            "role": "牵头人" if n == 0 else "核心成员",
            "how": "、".join(how),
            "result": (
                f"交付{('、'.join(outputs[:2]))}，结论交负责人拍板"
                if outputs else "形成可复核的结论与行动清单，交负责人拍板"
            ),
        })

    cap_details = details.get("capabilities") or {}
    highlights = [
        {"title": cap, "detail": _short(cap_details.get(cap) or "", 70)}
        for cap in (caps or skills)[:4]
    ]

    why = []
    if rationale:
        why.append(_short(rationale, 90))
    if employee.get("scene_case"):
        why.append(f"行业对标：{employee['scene_case']}")
    if caps:
        why.append(f"核心能力：{'、'.join(caps[:4])}")
    if employee.get("value_grade"):
        why.append(f"岗位价值评级：{employee['value_grade']}")
    if not why:
        why.append(_short(scope or employee.get("duty") or "", 90))

    cautions = []
    if scope:
        cautions.append(_short(scope, 80))
    cautions += _strings(employee.get("scene_boundary"))
    if contract.get("approval_boundary"):
        cautions.append(_short(contract["approval_boundary"], 80))
    if not cautions:
        cautions.append("结论都需要你拍板，TA 不会直接改你的系统数据。")

    scenes = _strings(employee.get("scene_when"))
    scenes += [
        f"要判断：{_short(d, 34)}" for d in decisions[:3]
        if "证据不足" not in d and "是否由" not in d
    ]
    if not scenes:
        scenes = [f"需要一份「{o}」时" for o in outputs[:4]]
    cadence = str(employee.get("usage_cadence") or "").strip()
    rhythm = profile.get("operating_rhythm") if isinstance(profile.get("operating_rhythm"), dict) else {}

    skill_bars = [
        {"name": s, "level": 78 + (seed >> (i * 3)) % 18}
        for i, s in enumerate(skills[:6])
    ]
    duty = str(employee.get("duty") or "").strip()
    if primary:
        headline_tail = "帮你判断" + _short(primary, 32)
    elif duty and duty != job:
        headline_tail = _short(duty, 36)
    else:
        headline_tail = _short(scope, 36)
    result = {
        "source": "generated",
        "headline": f"{job}｜{headline_tail}",
        "years": years,
        "degree": degree,
        "major": major,
        "industry": str(employee.get("dept_name") or ""),
        "cadence": cadence or str(rhythm.get("daily") or ""),
        "tags": (knowledge or skills)[:4],
        "summary": _short(
            rationale
            or (employee.get("duty") if str(employee.get("duty") or "").strip() != job else "")
            or scope, 200),
        "highlights": highlights,
        "experiences": experiences,
        "projects": projects,
        "education": {
            "school": f"某{'综合' if seed % 2 else '理工'}类大学",
            "major": major, "degree": degree,
            "period": f"{grad_year - (3 if degree == '本科' else 6)}.09 – {grad_year}.06",
        },
        "certificates": knowledge[:4],
        "learning": learning[:4],
        "why": why[:4],
        "cautions": cautions[:4],
        "scenes": scenes[:6],
        "prepare": (inputs or [f"相关的{d}" for d in data_objects]
                    or ["要解决的问题与目标", "已有数据和材料", "期限与限制条件"])[:6],
        "skill_bars": skill_bars,
        "deliverables": outputs[:6],
        "footnote": FOOTNOTE,
    }
    plain = _plain().get(key)
    if isinstance(plain, dict):
        result = _apply_plain(result, plain, job)
        result["source"] = "plain"
    return result


def card(resume: dict) -> dict:
    """The small subset shown on roster cards."""
    if not isinstance(resume, dict):
        return {}
    return {
        "one": resume.get("one") or "",
        "headline": resume.get("headline") or "",
        "years": resume.get("years"),
        "tags": (resume.get("tags") or [])[:3],
    }


_built_cache: dict = {}


def for_employee(employee: dict) -> dict:
    """Build (and memoize) a résumé using the role's frozen display profile."""
    from . import departments

    if not isinstance(employee, dict):
        return {}
    role_key = str(employee.get("key") or "")
    cache_key = (role_key, str(employee.get("employee_spec_sha256") or ""),
                 str(employee.get("dept_name") or ""))
    cached = _built_cache.get(cache_key)
    if cached is not None:
        return cached
    profile = employee.get("professional_profile") or {}
    details = departments.capability_details_for(role_key)
    if not profile:
        sidecar = departments.factory_profile_for(role_key)
        if sidecar:
            profile = sidecar.get("professional_profile") or {}
            details = sidecar.get("capability_details") or details
    result = build(employee, profile, details)
    _built_cache[cache_key] = result
    return result
