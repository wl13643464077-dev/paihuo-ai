"""资产/沉淀多维评估器(V5):轻量模型自动打标 ≥10 个维度.

维度:类别/平台/行业/主题/关键词/质量评分/人设匹配度/可复用度/时效性/情绪倾向/一句话摘要/建议用法。
后台循环自动补齐没有 meta 的行;也可手动触发单条重评。
"""
import asyncio
import json
import logging
import time

from . import db, providers

log = logging.getLogger("analyzer")

DIM_SCHEMA = """{
 "category": "类别,如 选题/成品文案/专家报告/经验方法/数据事实/账号人设",
 "platform": "最适配平台,如 小红书/公众号/抖音/视频号/B站/微博/通用",
 "industry": "所属行业,如 餐饮/科技数码/美妆/通用",
 "theme": "主题,8字内",
 "keywords": ["关键词3-6个"],
 "quality": 85,
 "match": 80,
 "reuse": 75,
 "timeliness": "长期有效/季节性/时效性强",
 "sentiment": "正面/中性/争议",
 "summary": "一句话摘要,30字内",
 "usage": "建议用法,20字内"
}"""


def _clean_text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _score(value) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number != number or number in (float("inf"), float("-inf")):
        return 0
    return max(0, min(100, round(number)))


def normalize_meta(raw) -> dict:
    """模型输出是不可信输入，只持久化 UI 真正需要的有界标量。"""
    data = raw if isinstance(raw, dict) else {}
    keywords = data.get("keywords")
    if not isinstance(keywords, list):
        keywords = []
    return {
        "category": _clean_text(data.get("category"), 30),
        "platform": _clean_text(data.get("platform"), 30),
        "industry": _clean_text(data.get("industry"), 40),
        "theme": _clean_text(data.get("theme"), 40),
        "keywords": [_clean_text(item, 30) for item in keywords[:6]
                     if _clean_text(item, 30)],
        "quality": _score(data.get("quality")),
        "match": _score(data.get("match")),
        "reuse": _score(data.get("reuse")),
        "timeliness": _clean_text(data.get("timeliness"), 30),
        "sentiment": _clean_text(data.get("sentiment"), 30),
        "summary": _clean_text(data.get("summary"), 160),
        "usage": _clean_text(data.get("usage"), 120),
    }


def _text_of(kind: str, row: dict) -> str:
    if kind == "knowledge":
        return f"标题:{row['title']}\n内容:{(row['content'] or '')[:1500]}"
    p = db.jloads(row["payload_json"], {})
    return "\n".join(f"{k}:{str(v)[:400]}" for k, v in p.items())


async def analyze(kind: str, row_id: int) -> dict:
    table = "knowledge" if kind == "knowledge" else "asset"
    row = db.one(
        f"SELECT * FROM {table} WHERE id=? AND deleted_at IS NULL", (row_id,)
    )
    if not row:
        raise ValueError("不存在")
    prompt = (f"你是内容资产评估师。对下面这份「{kind}」资产做多维评估打标。\n"
              f"quality=内容质量(0-100),match=对老板账号/生意的可用匹配度(0-100),"
              f"reuse=可复用度(0-100)。评分要敢拉开差距,不要都给80。\n\n"
              f"【资产内容】\n{_text_of(kind, row)}\n\n"
              f"只输出一个合法 JSON 对象:\n{DIM_SCHEMA}")
    r = await providers.call_text_json(9, prompt, timeout=180,
                                       token=f"analyze:{kind}:{row_id}")
    meta = normalize_meta(r.get("data"))
    if kind == "knowledge":
        changed = db.execute(
            "UPDATE knowledge SET meta_json=?,updated_at=? "
            "WHERE id=? AND deleted_at IS NULL",
            (json.dumps(meta, ensure_ascii=False), time.time(), row_id),
        )
        if changed != 1:
            raise ValueError("沉淀已删除")
    else:
        changed = db.execute(
            "UPDATE asset SET meta_json=?,updated_at=? "
            "WHERE id=? AND deleted_at IS NULL",
            (json.dumps(meta, ensure_ascii=False), time.time(), row_id),
        )
        if changed != 1:
            raise ValueError("资产已删除")
    return meta


async def loop():
    """后台自动补评:每 45s 找 3 条没有 meta 的行处理."""
    log.info("analyzer started")
    while True:
        try:
            todo = ([("knowledge", r["id"]) for r in db.q(
                        "SELECT id FROM knowledge WHERE meta_json IS NULL "
                        "AND deleted_at IS NULL ORDER BY id DESC LIMIT 2")]
                    + [("asset", r["id"]) for r in db.q(
                        "SELECT id FROM asset WHERE meta_json IS NULL "
                        "AND deleted_at IS NULL ORDER BY id DESC LIMIT 2")])
            for kind, rid in todo[:3]:
                try:
                    await analyze(kind, rid)
                    log.info("analyzed %s %s", kind, rid)
                except Exception:
                    # 失败别死循环:写一个占位 meta,可手动重评
                    payload = json.dumps(
                        {"summary": "自动评估失败，可手动重试"},
                        ensure_ascii=False,
                    )
                    if kind == "knowledge":
                        db.execute(
                            "UPDATE knowledge SET meta_json=?,updated_at=? "
                            "WHERE id=? AND deleted_at IS NULL",
                            (payload, time.time(), rid),
                        )
                    else:
                        db.execute(
                            "UPDATE asset SET meta_json=?,updated_at=? "
                            "WHERE id=? AND deleted_at IS NULL",
                            (payload, time.time(), rid),
                        )
        except Exception as exc:
            log.error(
                "analyzer tick failed error_type=%s",
                type(exc).__name__,
            )
        await asyncio.sleep(45)
