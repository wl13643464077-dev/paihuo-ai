"""发布后数据体检（内容流水线 v3 复盘口径，免费、纯规则）。

每个平台只先看两个最关键的指标，及格线来自流水线三平台真实数据诊断：
- 抖音：2 秒跳出率 <40%、完播率 >8%  → 不过线先改开头 3 秒
- 视频号：转发 ≥5、平均播放时长 >25 秒 → 不过线先改「转发理由」和第一人称经验
- 小红书：收藏 ≥10、封面点击率 ≥8%   → 不过线先改封面大字和标题关键词
同平台连续 3 条不过线 → 换选题方向。

数据由老板从各平台后台抄过来（24h / 72h 各填一次），存在 app_setting，不改表结构。
"""
from __future__ import annotations

import json
import time

from . import db

# 平台: [(字段, 名称, 单位, 方向, 及格线)]；方向 "lt" 越低越好，"ge"/"gt" 越高越好。
METRICS = {
    "抖音": [
        ("bounce_2s", "2秒跳出率", "%", "lt", 40.0),
        ("completion", "完播率", "%", "gt", 8.0),
    ],
    "视频号": [
        ("shares", "转发数", "次", "ge", 5.0),
        ("avg_watch_s", "平均播放时长", "秒", "gt", 25.0),
    ],
    "小红书": [
        ("saves", "收藏数", "次", "ge", 10.0),
        ("ctr", "封面点击率", "%", "ge", 8.0),
    ],
}
FIX = {
    "抖音": "先改开头 3 秒：第一句换成≤15字的冲突/结论，第 0 帧就要有冲突画面，别用数字人静止正脸开场。",
    "视频号": "先改「转发理由」和第一人称经验：说清楚这条该转给谁，加一段「我见过的店里……」。",
    "小红书": "先改封面大字和标题关键词：封面≤12字直接给结论，标题放 1 个用户会搜的词。",
}
STREAK = 3
_KEY = "pub_metrics:{tid}:{pid}"


def fields_for(platform: str) -> list[dict]:
    return [{"key": k, "label": label, "unit": unit, "target": _target_text(op, line, unit)}
            for k, label, unit, op, line in METRICS.get(platform, [])]


def _target_text(op, line, unit):
    sign = {"lt": "<", "gt": ">", "ge": "≥"}[op]
    return f"{sign}{line:g}{unit}"


def _passes(op, value, line):
    return value < line if op == "lt" else value > line if op == "gt" else value >= line


def _clean(platform: str, metrics: dict) -> dict:
    clean = {}
    for key, label, _unit, _op, _line in METRICS.get(platform, []):
        raw = (metrics or {}).get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(str(raw).replace("%", "").strip())
        except ValueError as exc:
            raise ValueError(f"「{label}」要填数字") from exc
        if value < 0 or value > 10_000_000:
            raise ValueError(f"「{label}」数值不合理")
        clean[key] = value
    return clean


def diagnose(platform: str, metrics: dict) -> dict:
    """返回每个指标是否过线和整体结论；不支持的平台返回 supported=False。"""
    if platform not in METRICS:
        return {"supported": False, "items": [], "passed": None,
                "advice": "这个平台暂未设及格线，可交给审查官人工复盘。"}
    values = _clean(platform, metrics)
    items = []
    for key, label, unit, op, line in METRICS[platform]:
        if key not in values:
            continue
        ok = _passes(op, values[key], line)
        items.append({"key": key, "label": label, "value": values[key], "unit": unit,
                      "target": _target_text(op, line, unit), "pass": ok})
    if not items:
        raise ValueError("至少填一个指标")
    passed = all(item["pass"] for item in items)
    return {"supported": True, "items": items, "passed": passed,
            "advice": "两项都过线，保持这个打法，下一条只改一个变量做对比。" if passed
            else FIX[platform]}


def save(tid: int, pid: int, stage: str, metrics: dict) -> dict:
    """保存一次数据（stage=24h/72h）并给出诊断 + 同平台连续不过线提醒。"""
    row = db.one("SELECT id,platform,title FROM publish_log WHERE id=? AND tenant_id=?",
                 (pid, tid))
    if not row:
        raise LookupError("发布记录不存在")
    stage = stage if stage in ("24h", "72h") else "24h"
    platform = row["platform"] or ""
    result = diagnose(platform, metrics)
    if not result["supported"]:
        return {**result, "platform": platform}
    key = _KEY.format(tid=tid, pid=pid)
    stored = db.jloads(db.get_setting(key), {}) or {}
    stored[stage] = {"metrics": {i["key"]: i["value"] for i in result["items"]},
                     "passed": result["passed"], "at": time.time()}
    db.set_setting(key, json.dumps(stored, ensure_ascii=False))
    result["streak"] = fail_streak(tid, platform)
    if result["streak"] >= STREAK:
        result["advice"] += f" 同平台已连续 {result['streak']} 条不过线：建议换选题方向。"
    return {**result, "platform": platform, "stage": stage}


def load(tid: int, pid: int) -> dict:
    return db.jloads(db.get_setting(_KEY.format(tid=tid, pid=pid)), {}) or {}


def fail_streak(tid: int, platform: str) -> int:
    """同平台最近几条（按发布时间倒序）连续不过线的条数；最新一次数据为准。"""
    rows = db.q("SELECT id FROM publish_log WHERE tenant_id=? AND platform=? "
                "ORDER BY published_at DESC, id DESC LIMIT 20", (tid, platform))
    streak = 0
    for row in rows:
        data = load(tid, row["id"])
        latest = data.get("72h") or data.get("24h")
        if not latest:
            continue
        if latest.get("passed"):
            break
        streak += 1
    return streak
