"""老板数据驾驶舱 HTTP 路由：汇总、导出 Excel、AI 经营分析。

下发与追踪复用「派给店员」接口（POST /api/staff/tasks，source_ref=cockpit:…），
这里不重复造派活入口。业务逻辑在 app/cockpit.py。不 import main.py。
"""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import cockpit, timeutil
from ..web_common import TEN, _need_admin

router = APIRouter()


def _raise(exc: cockpit.CockpitError):
    raise HTTPException(getattr(exc, "status", 400), str(exc))


@router.get("/api/cockpit/summary")
def cockpit_summary(days: int = cockpit.DEFAULT_DAYS):
    _need_admin()
    try:
        return cockpit.summary(TEN(), days)
    except cockpit.CockpitError as exc:
        _raise(exc)


@router.get("/api/cockpit/export.xlsx")
def cockpit_export(days: int = cockpit.DEFAULT_DAYS):
    _need_admin()
    try:
        data = cockpit.summary(TEN(), days)
    except cockpit.CockpitError as exc:
        _raise(exc)
    name = f"派活数据驾驶舱-近{data['period']['days']}天-{timeutil.today_cn()}.xlsx"
    return Response(
        cockpit.export_xlsx(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=cockpit.xlsx; filename*=UTF-8''{quote(name)}"},
    )


@router.post("/api/cockpit/ai")
async def cockpit_ai(body: dict | None = None):
    _need_admin()
    try:
        return await cockpit.analyze(TEN(), (body or {}).get("days", cockpit.DEFAULT_DAYS))
    except cockpit.CockpitError as exc:
        _raise(exc)
