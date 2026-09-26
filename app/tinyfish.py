"""TinyFish 免费联网情报通道（Search + Fetch，真浏览器渲染）.

TinyFish 的 Search / Fetch API 免费（https://docs.tinyfish.ai/），
返回结构化搜索结果与干净的页面 Markdown，动态/JS 页面也能抓到。

铁规:本模块只是「更好的情报来源」,永远降级可用——没配 key、限流、
超时或任何异常都返回空结果,由调用方回退云雾 Claude WebSearch 网关,
绝不因情报通道故障拦死业务。
"""
import asyncio
import logging
import os

import httpx

from . import secureconfig

log = logging.getLogger("tinyfish")

SEARCH_URL = "https://api.search.tinyfish.ai"
FETCH_URL = "https://api.fetch.tinyfish.ai"
SEARCH_TIMEOUT = 20
FETCH_TIMEOUT = 60


def api_key() -> str:
    """环境变量优先,其次管理后台保存的密文配置。"""
    return (
        os.environ.get("TINYFISH_API_KEY") or ""
    ).strip() or (secureconfig.get_secret("tinyfish_key") or "").strip()


def available() -> bool:
    return bool(api_key())


async def search(query: str, *, purpose: str = "", domain_type: str = "web",
                 recency_minutes: int = 0, limit: int = 6) -> list:
    """搜索并返回 [{title,url,snippet,domain,date}]。异常/未配置返回 []."""
    key = api_key()
    query = str(query or "").strip()
    if not key or not query:
        return []
    params = {"query": query[:300]}
    if purpose:
        params["purpose"] = str(purpose)[:1000]
    if domain_type in ("news", "research_paper"):
        params["domain_type"] = domain_type
    if recency_minutes:
        params["recency_minutes"] = int(recency_minutes)
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as cli:
            r = await cli.get(SEARCH_URL, params=params,
                              headers={"X-API-Key": key})
            if r.status_code != 200:
                log.warning("tinyfish search 非200 status=%s", r.status_code)
                return []
            rows = (r.json() or {}).get("results") or []
    except Exception as exc:                    # noqa: BLE001 —— 降级
        log.warning("tinyfish search 降级 error_type=%s", type(exc).__name__)
        return []
    out = []
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        out.append({
            "title": str(row.get("title") or "")[:200],
            "url": url[:600],
            "snippet": str(row.get("snippet") or "")[:400],
            "domain": str(row.get("domain") or "")[:120],
            "date": str(row.get("date") or "")[:40],
        })
        if len(out) >= limit:
            break
    return out


async def fetch(urls: list, *, purpose: str = "") -> list:
    """真浏览器渲染抓取,返回 [{url,title,text}](markdown)。异常返回 []."""
    key = api_key()
    urls = [str(u or "").strip() for u in (urls or [])
            if str(u or "").strip().startswith(("http://", "https://"))][:6]
    if not key or not urls:
        return []
    body = {
        "urls": urls,
        "format": "markdown",
        "per_url_timeout_ms": 45000,
    }
    if purpose:
        body["purpose"] = str(purpose)[:1000]
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as cli:
            r = await cli.post(FETCH_URL, json=body,
                               headers={"X-API-Key": key})
            if r.status_code != 200:
                log.warning("tinyfish fetch 非200 status=%s", r.status_code)
                return []
            payload = r.json() or {}
    except Exception as exc:                    # noqa: BLE001 —— 降级
        log.warning("tinyfish fetch 降级 error_type=%s", type(exc).__name__)
        return []
    out = []
    for row in payload.get("results") or []:
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        out.append({
            "url": str(row.get("url") or "")[:600],
            "title": str(row.get("title") or "")[:200],
            "text": text.strip()[:9000],
        })
    return out


async def research_bundle(queries: list, *, purpose: str = "",
                          fetch_top: int = 4) -> dict:
    """多词检索 + 抓正文,拼成给写作模型的证据材料。

    返回 {"material": str, "sources": [{"title","url"}]};失败返回空 material。
    """
    seen: dict = {}
    for q in [str(x or "").strip() for x in (queries or []) if str(x or "").strip()][:3]:
        for row in await search(q, purpose=purpose):
            if row["url"] not in seen:
                seen[row["url"]] = row
    ranked = list(seen.values())
    if not ranked:
        return {"material": "", "sources": []}
    pages = await fetch([row["url"] for row in ranked[:fetch_top]], purpose=purpose)
    blocks = []
    for row in ranked:
        blocks.append(
            f"### 搜索结果:{row['title']}\n- URL:{row['url']}\n"
            f"- 摘要:{row['snippet']}" + (f"\n- 日期:{row['date']}" if row["date"] else "")
        )
    for page in pages:
        blocks.append(
            f"### 页面正文:{page['title']}\n- URL:{page['url']}\n{page['text']}"
        )
    material = "\n\n".join(blocks)[:36000]
    sources = [{"title": row["title"] or row["domain"], "url": row["url"]}
               for row in ranked[:10]]
    return {"material": material, "sources": sources}


def usage_note() -> str:
    return "TinyFish Search/Fetch（免费真浏览器情报通道）"


async def smoke() -> dict:
    """管理后台连通性自检:1 次轻量搜索。"""
    rows = await search("餐饮行业 最新动态", recency_minutes=60 * 24 * 7, limit=2)
    return {"ok": bool(rows), "results": len(rows)}


def _run_sync(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
