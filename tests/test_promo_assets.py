"""Static contracts for the promotional page's high-impact image payloads."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMO = ROOT / "static" / "promo.html"
LOGIN = ROOT / "static" / "login.html"


def _asset_size(relative_path: str) -> int:
    path = ROOT / relative_path.lstrip("/")
    assert path.is_file(), f"missing promo asset: {relative_path}"
    return path.stat().st_size


class PromoAssetContractCase(unittest.TestCase):
    def test_public_copy_does_not_advertise_retired_roster_counts(self) -> None:
        public_copy = (
            PROMO.read_text(encoding="utf-8")
            + LOGIN.read_text(encoding="utf-8")
        )
        self.assertNotIn("431 位", public_copy)
        self.assertNotIn("420 名", public_copy)
        self.assertNotIn("其他申请转人工审核并明确反馈", public_copy)
        self.assertNotIn("其他申请进入人工审核", public_copy)
        self.assertIn("专属决策员工", public_copy)

    def test_promo_hero_images_use_small_webp_with_png_fallbacks(self) -> None:
        page = PROMO.read_text(encoding="utf-8")

        hero = re.search(
            r'<picture>\s*<source srcset="(/static/img/hero-1280\.webp)" type="image/webp">\s*'
            r'<img src="(/static/img/hero\.png)" alt="[^"]+" width="1280" height="853" '
            r'loading="eager" fetchpriority="high" decoding="async"',
            page,
        )
        self.assertIsNotNone(
            hero,
            "hero image must keep a PNG fallback and prioritize the compact WebP",
        )

        avatar = re.search(
            r'<picture class="heroimg">\s*'
            r'<source srcset="(/static/img/avatar-1152\.webp)" type="image/webp">\s*'
            r'<img src="(/static/img/avatar\.png)" alt="[^"]+" width="1152" height="768" '
            r'loading="lazy" decoding="async"',
            page,
        )
        self.assertIsNotNone(
            avatar,
            "below-the-fold avatar image must lazy-load with a PNG fallback",
        )

        self.assertLessEqual(_asset_size(hero.group(1)), 180_000)
        self.assertLessEqual(_asset_size(avatar.group(1)), 120_000)
        self.assertGreater(_asset_size(hero.group(2)), _asset_size(hero.group(1)))
        self.assertGreater(_asset_size(avatar.group(2)), _asset_size(avatar.group(1)))

    def test_promo_3d_hero_is_self_hosted_and_optional(self) -> None:
        page = PROMO.read_text(encoding="utf-8")
        scene = (ROOT / "static" / "promo-3d.js").read_text(encoding="utf-8")
        # 国内访问不依赖外网 CDN：three.js 随站点发布
        self.assertIn('from "./vendor/three.module.min.js"', scene)
        self.assertGreater(_asset_size("/static/vendor/three.module.min.js"), 100_000)
        self.assertTrue((ROOT / "static" / "vendor" / "three.LICENSE.txt").is_file())
        for cdn in ("cdn.jsdelivr", "unpkg.com", "cdnjs.cloudflare"):
            self.assertNotIn(cdn, page + scene)
        # 3D 是渐进增强：WebGL/省流量/加载失败都退回 CSS 背景，减少动效用户只渲染静帧
        self.assertIn('import("/static/promo-3d.js', page)
        self.assertIn("webglOK()", page)
        self.assertIn("saveData", page)
        self.assertIn("prefers-reduced-motion: reduce", page)
        self.assertIn('class="fallback"', page)

    def test_promo_entry_is_not_cached_and_api_docs_are_private(self) -> None:
        main_py = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        promo_route = main_py[main_py.index('@app.get("/promo")'):]
        promo_route = promo_route[:promo_route.index("\n\n")]
        self.assertIn("_HTML_ENTRY_NO_CACHE_HEADERS", promo_route)
        self.assertIn('os.environ.get("CONTENTCREW_PUBLIC_API_DOCS") == "1"', main_py)
        self.assertIn('openapi_url="/openapi.json" if _PUBLIC_API_DOCS else None', main_py)


if __name__ == "__main__":
    unittest.main()
