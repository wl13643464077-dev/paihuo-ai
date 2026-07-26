"""飞书集成(V8):资产库/沉淀库一键同步到飞书多维表格.

模式:平台级「企业自建应用」凭据(root 在管理后台配 app_id/app_secret),
每个租户自己粘一个多维表格链接;同步时自动建「知识沉淀」「资产库」数据表并写入。
说明:扫码 OAuth 登录需要企业认证域名+https 回调,当前采用自建应用直连模式,
效果一样(数据进多维表格),配置更简单。
"""
import json
import logging
import re
import time

import httpx

from . import auth, db, secureconfig

log = logging.getLogger("feishu")
BASE = "https://open.feishu.cn/open-apis"
SYNC_READ_PAGE_SIZE = 200
FEISHU_WRITE_BATCH_SIZE = 100

KNOW_FIELDS = ["标题", "内容", "类别", "平台", "行业", "主题", "关键词", "质量分",
               "匹配度", "复用度", "时效性", "情绪", "摘要", "来源", "创建时间"]
ASSET_FIELDS = ["标题", "类型", "类别", "平台", "行业", "主题", "关键词", "质量分",
                "匹配度", "复用度", "时效性", "摘要", "创建时间"]


class FeishuProviderError(RuntimeError):
    """飞书远端失败。

    异常文本只能使用平台维护的固定文案；远端 ``msg``、响应正文和网络异常
    不得跨越异常、HTTP 或日志边界。
    """


def _provider_error(action: str) -> FeishuProviderError:
    return FeishuProviderError(f"{action}暂时不可用，请稍后重试")


async def _request_json(cli, method: str, url: str, *, action: str, **kwargs) -> dict:
    try:
        response = await getattr(cli, method)(url, **kwargs)
    except Exception as exc:
        log.warning(
            "飞书远端请求失败 action=%s error_type=%s",
            action,
            type(exc).__name__,
        )
        raise _provider_error(action) from None

    status = getattr(response, "status_code", None)
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    if not 200 <= status_code < 300:
        log.warning(
            "飞书远端响应失败 action=%s status=%s",
            action,
            status_code or "unknown",
        )
        raise _provider_error(action)

    try:
        data = response.json()
    except Exception as exc:
        log.warning(
            "飞书远端响应解析失败 action=%s error_type=%s",
            action,
            type(exc).__name__,
        )
        raise _provider_error(action) from None
    if not isinstance(data, dict) or data.get("code") != 0:
        log.warning("飞书远端业务响应失败 action=%s", action)
        raise _provider_error(action)
    return data


def conf():
    return (
        db.get_setting("feishu_app_id") or "",
        secureconfig.get_secret("feishu_app_secret"),
    )


def tenant_bitable() -> str:
    return db.get_setting(f"feishu_bitable:{auth.tenant_id()}") or ""


def parse_app_token(url: str) -> str:
    m = re.search(r"/(?:base|wiki)/([A-Za-z0-9]{15,})", url or "")
    return m.group(1) if m else ""


async def _token(cli) -> str:
    app_id, secret = conf()
    if not app_id or not secret:
        raise ValueError("平台未配置飞书应用(管理后台→飞书集成)")
    data = await _request_json(
        cli,
        "post",
        f"{BASE}/auth/v3/tenant_access_token/internal",
        action="飞书鉴权",
        json={"app_id": app_id, "app_secret": secret},
    )
    token = data.get("tenant_access_token")
    if not isinstance(token, str) or not token:
        raise _provider_error("飞书鉴权")
    return token


async def _ensure_table(cli, tok, app_token, name, fields) -> str:
    data = await _request_json(
        cli,
        "get",
        f"{BASE}/bitable/v1/apps/{app_token}/tables?page_size=100",
        action="读取飞书多维表格",
        headers={"Authorization": f"Bearer {tok}"},
    )
    items = (data.get("data") or {}).get("items") or []
    if not isinstance(items, list):
        raise _provider_error("读取飞书多维表格")
    for table in items:
        if not isinstance(table, dict) or table.get("name") != name:
            continue
        table_id = table.get("table_id")
        if isinstance(table_id, str) and table_id:
            return table_id
        raise _provider_error("读取飞书多维表格")
    data = await _request_json(
        cli,
        "post",
        f"{BASE}/bitable/v1/apps/{app_token}/tables",
        action="创建飞书数据表",
        headers={"Authorization": f"Bearer {tok}"},
        json={"table": {"name": name, "fields": [
            {"field_name": f, "type": 1} for f in fields]}},
    )
    table_id = (data.get("data") or {}).get("table_id")
    if not isinstance(table_id, str) or not table_id:
        raise _provider_error("创建飞书数据表")
    return table_id


async def _batch_create(cli, tok: str, app_token: str, table_id: str,
                        records: list) -> None:
    await _request_json(
        cli,
        "post",
        f"{BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
        action="写入飞书多维表格",
        headers={"Authorization": f"Bearer {tok}"},
        json={"records": [{"fields": item} for item in records]},
    )


async def _write_batches(cli, tok: str, app_token: str, table_id: str,
                         records: list) -> int:
    written = 0
    for index in range(0, len(records), FEISHU_WRITE_BATCH_SIZE):
        batch = records[index:index + FEISHU_WRITE_BATCH_SIZE]
        await _batch_create(cli, tok, app_token, table_id, batch)
        written += len(batch)
    return written


def _know_record(r):
    m = r.get("meta") or {}
    return {"标题": r.get("title") or "", "内容": (r.get("content") or "")[:800],
            "类别": m.get("category") or "", "平台": m.get("platform") or "",
            "行业": m.get("industry") or "", "主题": m.get("theme") or "",
            "关键词": "、".join(m.get("keywords") or []),
            "质量分": str(m.get("quality") or ""), "匹配度": str(m.get("match") or ""),
            "复用度": str(m.get("reuse") or ""), "时效性": m.get("timeliness") or "",
            "情绪": m.get("sentiment") or "", "摘要": m.get("summary") or "",
            "来源": "自动沉淀" if r.get("source") == "auto" else "老板手记",
            "创建时间": time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("created_at") or 0))}


def _asset_record(r):
    m = r.get("meta") or {}
    p = r.get("payload") or {}
    return {"标题": p.get("title") or "", "类型": r.get("type") or "",
            "类别": m.get("category") or "", "平台": m.get("platform") or "",
            "行业": m.get("industry") or "", "主题": m.get("theme") or "",
            "关键词": "、".join(m.get("keywords") or []),
            "质量分": str(m.get("quality") or ""), "匹配度": str(m.get("match") or ""),
            "复用度": str(m.get("reuse") or ""), "时效性": m.get("timeliness") or "",
            "摘要": m.get("summary") or "",
            "创建时间": time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("created_at") or 0))}


async def sync_rows(name: str, fields: list, recs: list) -> dict:
    """通用:把一批记录写进租户多维表格的指定数据表(没有就建)."""
    url = tenant_bitable()
    app_token = parse_app_token(url)
    if not app_token:
        raise ValueError("先在「👥 权限管理」页粘贴您的飞书多维表格链接并保存")
    if not recs:
        raise ValueError("没有可同步的数据")
    async with httpx.AsyncClient(timeout=60) as cli:
        tok = await _token(cli)
        table_id = await _ensure_table(cli, tok, app_token, name, fields)
        n = await _write_batches(cli, tok, app_token, table_id, recs)
    return {"synced": n, "table": name}


def _sync_page(kind: str, tid: int, before_id: int | None) -> tuple:
    cursor_sql = " AND id<?" if before_id is not None else ""
    args = (
        (tid, before_id, SYNC_READ_PAGE_SIZE)
        if before_id is not None
        else (tid, SYNC_READ_PAGE_SIZE)
    )
    if kind == "knowledge":
        rows = db.q(
            "SELECT * FROM knowledge WHERE tenant_id=? "
            f"AND deleted_at IS NULL{cursor_sql} "
            "ORDER BY id DESC LIMIT ?",
            args,
        )
        for row in rows:
            row["meta"] = db.jloads(row.get("meta_json"), {})
        records = [_know_record(row) for row in rows]
    else:
        rows = db.q(
            "SELECT * FROM asset WHERE tenant_id=?"
            f"{cursor_sql} ORDER BY id DESC LIMIT ?",
            args,
        )
        for row in rows:
            row["meta"] = db.jloads(row.get("meta_json"), {})
            row["payload"] = db.jloads(row.get("payload_json"), {})
        records = [_asset_record(row) for row in rows]
    next_cursor = int(rows[-1]["id"]) if rows else None
    return records, next_cursor


async def sync(kind: str) -> dict:
    url = tenant_bitable()
    app_token = parse_app_token(url)
    if not app_token:
        raise ValueError("先在下方粘贴您的飞书多维表格链接并保存")
    tid = auth.tenant_id()
    if kind == "knowledge":
        name, fields = "知识沉淀", KNOW_FIELDS
    else:
        name, fields = "资产库", ASSET_FIELDS
    records, before_id = _sync_page(kind, tid, None)
    if not records:
        raise ValueError("没有可同步的数据")
    async with httpx.AsyncClient(timeout=60) as cli:
        tok = await _token(cli)
        table_id = await _ensure_table(cli, tok, app_token, name, fields)
        n = 0
        while records:
            n += await _write_batches(
                cli,
                tok,
                app_token,
                table_id,
                records,
            )
            if len(records) < SYNC_READ_PAGE_SIZE:
                break
            records, before_id = _sync_page(kind, tid, before_id)
    return {"synced": n, "table": name}
