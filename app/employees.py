"""数字员工层(V2):可编辑提示词模板 + 全网进修技能库.

- 提示词:每个工位的职责提示词抽成模板存 employee_config,老板可改可恢复默认;
  模板里 {占位符} 由工位在运行时注入(未知占位符原样保留,不会崩)。
- 技能库:员工可"全网进修"——联网检索岗位最新方法论/平台规则/爆款技巧,
  沉淀为技能卡(可停用/删除),启用中的技能自动注入该员工的工作提示词。
"""
import asyncio
import json
import logging
import re
import time

from . import db, llm

log = logging.getLogger("employees")

LEARNING: set = set()          # 正在进修中的工位 idx
MAX_SKILLS_IN_PROMPT = 12      # 注入提示词的技能条数上限
MAX_SKILLS_CHARS = 2400        # 注入提示词的技能总字数上限


def claim_learning(idx: int) -> bool:
    """Atomically reserve one employee's learning slot on the event loop."""
    if idx in LEARNING:
        return False
    LEARNING.add(idx)
    return True


# ---------------- 配置读写 ----------------
def _row_to_config(idx: int, r) -> dict:
    if not r:
        return {"idx": idx, "prompt_template": None, "skills": [], "learned_at": None,
                "settings": {}, "caps_off": [], "enabled": True}
    return {"idx": idx, "prompt_template": r["prompt_template"],
            "skills": db.jloads(r["skills_json"], []), "learned_at": r["learned_at"],
            "settings": db.jloads(r.get("settings_json"), {}),
            "caps_off": db.jloads(r.get("caps_off_json"), []),
            "enabled": (r.get("enabled", 1) or 0) != 0}


def get_config(idx: int) -> dict:
    return _row_to_config(idx, db.one("SELECT * FROM employee_config WHERE idx=?", (idx,)))


def get_configs(idxs) -> dict:
    """批量取多员工配置(一条 SQL),供 /api/depts 等一次渲染几百员工时避免 N+1 查询.
    返回 {idx: config};库里没有的 idx 不在结果里(调用方用 get_config 的默认兜底)."""
    idxs = list(idxs)
    if not idxs:
        return {}
    ph = ",".join("?" * len(idxs))
    rows = db.q(f"SELECT * FROM employee_config WHERE idx IN ({ph})", idxs)
    return {r["idx"]: _row_to_config(r["idx"], r) for r in rows}


def _upsert(idx: int, data: dict):
    data = dict(data)
    data["updated_at"] = time.time()
    if db.one("SELECT idx FROM employee_config WHERE idx=?", (idx,)):
        sets = ",".join(f"{k}=?" for k in data)
        db.q(f"UPDATE employee_config SET {sets} WHERE idx=?", list(data.values()) + [idx])
    else:
        data.setdefault("created_at", time.time())
        data["idx"] = idx
        cols = ",".join(data)
        db.q(f"INSERT INTO employee_config({cols}) VALUES({','.join('?' * len(data))})",
             list(data.values()))


def set_prompt(idx: int, template: str):
    """保存自定义提示词模板;传空串/None 表示恢复默认."""
    _upsert(idx, {"prompt_template": (template or "").strip() or None})


def set_skills(idx: int, skills: list):
    _upsert(idx, {"skills_json": json.dumps(skills, ensure_ascii=False)})


def _store_learning_result(idx: int, skills: list, learned_at: float) -> None:
    """Persist the learned skill set and timestamp as one transaction."""
    with db.atomic():
        _upsert(
            idx,
            {
                "skills_json": json.dumps(skills, ensure_ascii=False),
                "learned_at": learned_at,
            },
        )


def set_settings(idx: int, settings: dict):
    """员工工作配置(V3):趋势官/情报员的检索渠道、拆解师的对标与维度等."""
    _upsert(idx, {"settings_json": json.dumps(settings or {}, ensure_ascii=False)})


def set_models(idx: int, model_text=None, model_image=None):
    """员工级模型路由(V6):传 None 不动,传空串清除(回全局默认)."""
    data = {}
    if model_text is not None:
        data["model_text"] = model_text or None
    if model_image is not None:
        data["model_image"] = model_image or None
    if data:
        _upsert(idx, data)


def is_enabled(idx: int) -> bool:
    r = db.one("SELECT enabled FROM employee_config WHERE idx=?", (idx,))
    return (r or {}).get("enabled", 1) != 0


def set_enabled(idx: int, enabled: bool):
    _upsert(idx, {"enabled": 1 if enabled else 0})


def set_caps_off(idx: int, caps_off: list):
    """员工能力开关(V4):记录被老板停用的出厂能力名."""
    _upsert(idx, {"caps_off_json": json.dumps(caps_off or [], ensure_ascii=False)})


# ---------------- 模板渲染 ----------------
def render(template: str, vars_: dict) -> str:
    """只替换 vars_ 里已知的 {占位符};未知占位符与 JSON 花括号原样保留."""
    return re.sub(r"\{(\w+)\}",
                  lambda m: str(vars_[m.group(1)]) if m.group(1) in vars_ else m.group(0),
                  template)


def skills_block(idx: int) -> str:
    """启用中的技能卡 → 注入工作提示词的文本块."""
    skills = [s for s in get_config(idx)["skills"] if s.get("enabled", True)]
    if not skills:
        return ""
    lines, total = [], 0
    for s in skills[:MAX_SKILLS_IN_PROMPT]:
        line = f"- 【{s.get('title', '')}】{s.get('detail', '')}"
        total += len(line)
        if total > MAX_SKILLS_CHARS:
            break
        lines.append(line)
    return ("\n【你的进修技能库(全网收集的最新打法,本次工作要主动运用)】\n"
            + "\n".join(lines) + "\n")


# ---------------- 全网进修 ----------------
def learning_prompt_bundle(station: dict, existing: list):
    """进修也走隔离研究：现有技能/岗位档案只给最终模型，不交给 WebSearch。"""
    from . import providers

    known = "、".join(s.get("title", "") for s in existing) or "(暂无)"
    known_detail = json.dumps(existing[:MAX_SKILLS_IN_PROMPT], ensure_ascii=False)
    system = f"""你是数字员工培训师,今天是 {time.strftime('%Y-%m-%d')}。
【学员内部岗位档案】
- 岗位:{station['name']}(部门:{station.get('dept', '')})
- 职责:{station['duty']}
- 对标能力:{station.get('skill', '')}
- 已掌握技能:{known}
- 已掌握技能详情:{known_detail[:MAX_SKILLS_CHARS]}

根据隔离联网代理返回的公开证据，为学员提炼 3-6 条新的技能卡。每条必须具体、
可直接执行，避开已掌握技能。只输出一个合法 JSON 对象:
{{"skills":[{{"title":"技能名,12字内","detail":"怎么做,具体可执行,120字内",
"source":"信息来源(站点/文章名)"}}]}}"""
    user = (
        f"请为「{station['name']}」补充近三个月公开出现的新方法论、平台规则变化、"
        "实用工具玩法和可执行技巧；只整理有来源支持的新内容。"
    )
    research = providers.sanitize_research_brief(
        f"检索「{station['name']}」岗位领域近三个月的公开方法论、平台规则变化、"
        "实用工具玩法和案例，优先权威来源，至少做 3 次针对性搜索。"
    )
    return providers.PromptBundle(
        system=system,
        user=user,
        research=research,
        sensitive=tuple(
            p for p in (
                station.get("duty") or "",
                station.get("skill") or "",
                known_detail,
            ) if str(p).strip()
        ),
    )


async def learn(station: dict, broadcast=None, *, claimed: bool = False) -> dict:
    """联网收集该岗位的最新技能,合并进技能库.

    broadcast(ev) 可选:实时把进修步骤推给前端(SSE)。
    claimed=True 仅供已在路由入口预占进修槽的后台任务使用。
    """
    idx = station["idx"]
    if claimed:
        if idx not in LEARNING:
            raise ValueError("该员工进修占位已失效")
    elif not claim_learning(idx):
        raise ValueError("该员工正在进修中")
    broadcast = broadcast or (lambda ev: None)

    def progress(kind, label=""):
        broadcast({"type": "employee_step", "idx": idx,
                   "step": {"k": kind, "l": str(label)[:300], "ts": time.time()}})

    try:
        existing = (await db.arun(get_config, idx))["skills"]
        prompt = learning_prompt_bundle(station, existing)
        progress("start", "去全网进修:检索岗位最新方法论与平台规则…")
        from . import providers
        r = await providers.call_text_json(
            idx,
            prompt.user,
            web=True,
            timeout=600,
            progress=progress,
            token=f"learn:{idx}",
            system_prompt=prompt.system,
            research_brief=prompt.research,
            sensitive_texts=prompt.sensitive,
        )
        fresh = []
        seen = {s.get("title") for s in existing}
        for s in r["data"].get("skills", []):
            if s.get("title") and s["title"] not in seen:
                s.update(enabled=True, learned_at=time.time())
                fresh.append(s)
                seen.add(s["title"])
        merged = existing + fresh
        await db.arun(_store_learning_result, idx, merged, time.time())
        progress("done", f"进修完成:新学 {len(fresh)} 条技能,技能库共 {len(merged)} 条 "
                         f"· ${r['cost_usd']:.3f}")
        broadcast({"type": "employee_update", "idx": idx})
        return {"new": len(fresh), "total": len(merged), "cost_usd": r["cost_usd"]}
    except llm.LLMError as e:
        from . import providers
        progress(
            "error",
            providers.public_failure_message(
                e,
                "进修任务暂未完成，请稍后重试",
            ),
        )
        broadcast({"type": "employee_update", "idx": idx})
        raise
    finally:
        LEARNING.discard(idx)
