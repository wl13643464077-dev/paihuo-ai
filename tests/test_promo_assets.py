"""Static contracts for the promotional page's high-impact image payloads."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMO = ROOT / "static" / "promo.html"


def _asset_size(relative_path: str) -> int:
    path = ROOT / relative_path.lstrip("/")
    assert path.is_file(), f"missing promo asset: {relative_path}"
    return path.stat().st_size


class PromoAssetContractCase(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
