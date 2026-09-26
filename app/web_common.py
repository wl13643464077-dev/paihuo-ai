"""多个路由域共用的 Web 层辅助(第 3 期从 main.py 机械拆分，函数体未改)。

放这里的都是被 main.py 与 app/routes/ 下两个及以上模块共同使用的东西：
当前用户/权限检查(TEN、_need_*)、分页、DB 线程安全包装、计费启动、
持久上传/免费 AI 限流闸门、公开视图裁剪等。main.py 会把这里的名字原样
重新导入，保持 main.<名字> 可用。本模块不得 import main.py 或 app/routes/。
"""


from fastapi import HTTPException

from . import auth


def _need_admin():
    if not auth.is_admin():
        raise HTTPException(403, "需要主账号权限")


def _need_root():
    if not auth.is_root():
        raise HTTPException(403, "需要平台管理员权限")


def _is_boss() -> bool:
    """员工内部资料仅向唯一超级管理账号 boss 开放。"""
    u = auth.current() or {}
    return u.get("role") == "root" and u.get("username") == "boss"


def TEN() -> int:
    return auth.tenant_id()
