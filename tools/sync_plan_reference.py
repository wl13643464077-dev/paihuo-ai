"""把服务端套餐目录写进宣传页和套餐页，作为接口失败时的“参考价”兜底。

用法(在仓库根目录)：
    python tools/sync_plan_reference.py          # 改写 static/promo.html 与 static/app.js
    python tools/sync_plan_reference.py --check  # 只检查是否一致，不一致返回 1

数据来源是 app.purchases.reference_catalog()(只读代码里的默认套餐和价目，
不连数据库)。改了 app/billing.py 的 PLANS / PERIODS / DEFAULT_PRICES 后运行一次。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROMO = ROOT / "static" / "promo.html"
APP_JS = ROOT / "static" / "app.js"
PROMO_RE = re.compile(
    r'(<script type="application/json" id="plan-reference">)(.*?)(</script>)',
    re.S,
)
APP_RE = re.compile(r"^(const PLAN_REFERENCE=)(.*?)(;)$", re.M)


def reference_json() -> str:
    from app import purchases

    text = json.dumps(
        purchases.reference_catalog(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    # 内嵌进 <script> 时不能出现 "</"。
    return text.replace("</", "<\\/")


def _rewrite(path: Path, pattern: re.Pattern, payload: str) -> tuple[str, str]:
    source = path.read_text(encoding="utf-8")
    if len(pattern.findall(source)) != 1:
        raise SystemExit(f"{path.name} 里没找到唯一的参考价标记")
    updated = pattern.sub(lambda m: m.group(1) + payload + m.group(3), source)
    return source, updated


def main(argv: list[str]) -> int:
    payload = reference_json()
    check = "--check" in argv
    stale = []
    for path, pattern in ((PROMO, PROMO_RE), (APP_JS, APP_RE)):
        source, updated = _rewrite(path, pattern, payload)
        if source != updated:
            stale.append(path.name)
            if not check:
                path.write_text(updated, encoding="utf-8")
    if check and stale:
        print("参考价需要同步：" + "、".join(stale))
        return 1
    print("已同步：" + ("、".join(stale) if stale else "无变化"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
