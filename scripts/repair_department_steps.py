#!/usr/bin/env python3
"""从岗位手册(md)重新生成 steps / deliverables。

背景:两批部门配置的 steps 字段都不可信——
- 10 个模板行业(360 人):steps 被写成了「输出要求」的切分结果,即交付物名词,
  而不是工作流步骤;deliverables 还带着按顿号硬切句子留下的残片
  (如「关键假设。输出顺序固定为：结论摘要」)。
- 餐饮(60 人):解析器只认「## 工作流」,而手册里还用「## 流程」「## 分步工作流」,
  导致 46 人 steps 为空,前端一个能力项都开不出来。

steps 是 departments.capabilities_for() 的数据源,直接决定老板在员工面板上
能开关什么、以及注入提示词的「本次启用的工作流步骤」。修好它才谈得上
「员工能力可配置」。

用法:
    python3 scripts/repair_department_steps.py --check   # 只报告,不改文件
    python3 scripts/repair_department_steps.py           # 就地修复
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEPT_GLOB = os.path.join(ROOT, "data", "departments", "*.json")

# 餐饮手册用 markdown 小节;工作流小节存在这几种写法。
WORKFLOW_HEADINGS = ("工作流", "流程", "分步工作流", "步骤")
DELIVERABLE_HEADINGS = ("交付物", "输出", "产出")
MAX_STEPS = 12
MAX_DELIVERABLES = 12


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    # 去掉 markdown 强调标记:这些文本会直接作为能力项名字显示给老板,
    # 也会注入提示词,带着 ** 既难看也没意义。
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = text.replace("**", "")
    return text.strip(" ；;，,。.、").strip()


def _md_section(md: str, names) -> str:
    """取出 markdown 手册里某个 ## 小节的正文。"""
    for name in names:
        match = re.search(
            rf"^##\s*{re.escape(name)}\s*$\n(.*?)(?=^##\s|\Z)",
            md, re.M | re.S,
        )
        if match:
            return match.group(1)
    return ""


def _numbered_items(block: str) -> list:
    """抓「1. xxx」式编号条目。"""
    items = []
    for line in block.splitlines():
        match = re.match(r"^\s*(\d{1,2})[.、)]\s*(.+)$", line)
        if match:
            value = _clean(match.group(2))
            if value:
                items.append(value)
    return items


def _bullet_items(block: str) -> list:
    """抓「- xxx」式条目。"""
    items = []
    for line in block.splitlines():
        match = re.match(r"^\s*[-*]\s+(.+)$", line)
        if match:
            value = _clean(match.group(1))
            if value:
                items.append(value)
    return items


def _bracket_block(md: str, name: str) -> str:
    """取出模板手册里【某某】开头、到下一个【】或文末为止的正文。"""
    match = re.search(
        rf"【{re.escape(name)}】(.*?)(?=【|\Z)", md, re.S
    )
    return match.group(1) if match else ""


def steps_from_md(md: str) -> list:
    """工作流步骤:模板行业取【执行要求】,餐饮取 ## 流程/工作流/分步工作流。"""
    block = _bracket_block(md, "执行要求")
    if block:
        items = _numbered_items(block)
        if items:
            return items[:MAX_STEPS]
    block = _md_section(md, WORKFLOW_HEADINGS)
    if block:
        items = _numbered_items(block) or _bullet_items(block)
        if items:
            return items[:MAX_STEPS]
    # 少数手册(如餐饮 160)用「一、二、三」小节承载流程。
    items = [
        _clean(m) for m in re.findall(r"^##\s*[一二三四五六七八九十]+、\s*(.+)$", md, re.M)
    ]
    return [i for i in items if i][:MAX_STEPS]


def deliverables_from_md(md: str) -> list:
    """交付物:模板行业取【输出要求】首句,餐饮取 ## 交付物 的条目。"""
    block = _bracket_block(md, "输出要求")
    if block:
        # 首句即交付物清单;其后的「输出顺序固定为：…」是排版要求,不是交付物。
        head = re.split(r"[。\n]", block.strip(), maxsplit=1)[0]
        items = [_clean(part) for part in head.split("、")]
        items = [i for i in items if i]
        if items:
            return items[:MAX_DELIVERABLES]
    block = _md_section(md, DELIVERABLE_HEADINGS)
    if block:
        items = _bullet_items(block) or _numbered_items(block)
        if items:
            return items[:MAX_DELIVERABLES]
    return []


def repair(check_only: bool = False) -> int:
    files = sorted(glob.glob(DEPT_GLOB))
    if not files:
        print(f"找不到部门配置: {DEPT_GLOB}", file=sys.stderr)
        return 2
    total = changed = still_empty = 0
    for path in files:
        with open(path, encoding="utf-8") as handle:
            dept = json.load(handle)
        dirty = False
        for employee in dept.get("employees") or []:
            total += 1
            md = employee.get("md") or ""
            new_steps = steps_from_md(md)
            new_delivs = deliverables_from_md(md)
            if not new_steps:
                still_empty += 1
                print(f"  ! 无法解析步骤: {os.path.basename(path)} "
                      f"num={employee.get('num')} {employee.get('name')}")
            if new_steps and new_steps != employee.get("steps"):
                employee["steps"] = new_steps
                dirty = True
            if new_delivs and new_delivs != employee.get("deliverables"):
                employee["deliverables"] = new_delivs
                dirty = True
        if dirty:
            changed += 1
            if not check_only:
                # 保持仓库既有的 indent=1 排版:改动只应体现在 steps/deliverables 上,
                # 重排全文会让 diff 无法评审。
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(dept, handle, ensure_ascii=False, indent=1)
                    handle.write("\n")
    verb = "需要修复" if check_only else "已修复"
    print(f"{verb}: {changed}/{len(files)} 个部门文件, 共 {total} 名员工, "
          f"仍无法解析步骤 {still_empty} 人")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="只报告需要修复的文件,不写盘")
    args = parser.parse_args()
    return repair(check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
