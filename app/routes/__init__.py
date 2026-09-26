"""按业务域拆出的 HTTP 路由模块(第 3 期起)。

每个模块定义 ``router = APIRouter()``，由 main.py 在原代码位置
``app.include_router(...)`` 挂载，保持路由注册顺序。约定见 docs/ARCHITECTURE.md。
路由模块不得 import main.py；共享辅助从 app/web_common.py 引入。
"""
