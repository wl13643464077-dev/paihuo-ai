"""Build app/scene_roles_v1.json (+ factory profiles) from tools/scene_roles_v1_spec.py.

Usage: python3 tools/build_scene_roles_v1.py
The output is deterministic; re-running with an unchanged spec is a no-op.
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from scene_roles_v1_spec import GROUP, INDUSTRY_OFFSETS, ROLES  # noqa: E402

OUT = os.path.join(ROOT, "app", "scene_roles_v1.json")
PROFILES = os.path.join(ROOT, "app", "factory_profiles_v1.json")


def _md(industry_name: str, r: dict) -> str:
    steps = "\n".join(f"{i}. {s}；" for i, s in enumerate(r["steps"], 1))
    return (
        f"【角色】你是{industry_name}的「{r['name']}」，对门店的实际经营结果负责。\n\n"
        f"【适用任务】{r['duty']}。\n\n"
        f"【边界】{r['scope']}\n\n"
        f"【必须输入】{'、'.join(r['inputs'])}。关键资料缺失时，先输出“资料缺口与获取方式”，"
        "不得用想象数据代填。\n\n"
        f"【执行要求】\n{steps}\n"
        f"{len(r['steps']) + 1}. 每个结论标注“已证实事实 / 数据推断 / 待验证假设”，计算列出公式与口径。\n\n"
        f"【输出要求】{'、'.join(r['deliverables'])}。输出顺序：结论摘要 → 依据 → 正文 → 风险与例外 → 行动清单 → 待确认事项。\n\n"
        f"【禁止】{'；'.join(r['boundary'])}；禁止伪造数据、无来源行业数字和自动执行任何业务写操作。"
    )


def build():
    depts = {}
    profiles = {}
    with open(PROFILES, encoding="utf-8") as handle:
        existing_profiles = json.load(handle)
    for industry, roles in ROLES.items():
        offset = INDUSTRY_OFFSETS[industry]
        with open(os.path.join(ROOT, "data", "departments", f"{industry}.json"), encoding="utf-8") as handle:
            industry_name = json.load(handle).get("industry") or industry
        employees = []
        for n, r in enumerate(roles, 1):
            idx = 3000 + offset * 100 + n
            key = f"scene-{industry}-{r['key']}"
            intro = f"{r['person']}，{r['name']}：{r['duty']}。"
            employees.append({
                "idx": idx, "num": idx, "key": key, "name": r["name"],
                "person": r["person"], "role": f"AI场景专岗·{r['name']}",
                "duty": r["duty"], "desc": r["duty"], "intro": intro,
                "emoji": r["emoji"], "color": r["color"], "group": GROUP,
                "inputs": r["inputs"], "steps": r["steps"],
                "deliverables": r["deliverables"], "md": _md(industry_name, r),
                "scene_case": r["case"], "scene_boundary": r["boundary"],
                "scene_when": r["scenes"],
                "public_guide": {
                    "focus": r["duty"],
                    "materials": "、".join(r["inputs"]),
                    "input_tips": [
                        f"先说清门店/区域和时间范围，以及想解决的事：{r['scenes'][0]}",
                        f"按原样提供{r['inputs'][0]}和{r['inputs'][1]}，不要只给口头汇总",
                        f"说明希望什么时候拿到{r['deliverables'][0]}、由谁拍板",
                    ],
                    "output_hint": f"将得到{'、'.join(r['deliverables'])}；结论由你拍板，不会改动任何业务系统。",
                },
            })
            profiles[key] = {
                "dept_key": industry,
                "professional_profile": {
                    "scope": r["scope"], "decisions": r["decisions"],
                    "knowledge_domains": r["knowledge"], "data_objects": r["data"],
                    "skill_tree": r["skills"], "capabilities": r["caps"],
                    "operating_rhythm": dict(zip(("daily", "event_driven", "review"), r["rhythm"])),
                },
            }
        depts[industry] = {
            "group": {"name": GROUP, "emoji": "✨", "color": "#ff7a00",
                      "members": [e["idx"] for e in employees]},
            "employees": employees,
        }
    payload = {"catalog": "scene.v1", "as_of": "2026-10-28",
               "source": "11 行业 AI 落地场景 220 例（2026-10-27）中无需硬件、现有花名册未覆盖的场景",
               "departments": depts}
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    merged = {k: v for k, v in existing_profiles.items() if not k.startswith("scene-")}
    merged.update(profiles)
    with open(PROFILES, "w", encoding="utf-8") as handle:
        # Keep the sidecar's existing byte format (indent=0, no trailing newline).
        handle.write(json.dumps(merged, ensure_ascii=False, indent=0, sort_keys=True))
    print(f"wrote {sum(len(d['employees']) for d in depts.values())} roles")


if __name__ == "__main__":
    build()
