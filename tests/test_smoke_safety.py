import unittest
from pathlib import Path


class SmokeSafetyContractCase(unittest.TestCase):
    def test_operator_smoke_defaults_to_read_only_and_has_no_bundled_password(self):
        source = (
            Path(__file__).resolve().parent / "smoke.py"
        ).read_text(encoding="utf-8")
        self.assertIn('WRITE_MODE = "--write"', source)
        self.assertIn("SMOKE_ALLOW_PROD_WRITES", source)
        self.assertIn("SMOKE_USERNAME", source)
        self.assertIn("SMOKE_PASSWORD", source)
        self.assertNotIn('("boss", "123456")', source)
        self.assertNotIn('"smoke123"', source)
        self.assertLess(
            source.index("if not WRITE_MODE:"),
            source.index("# 4. 知识库 CRUD"),
        )

    def test_simple_deploy_smoke_only_reads_fixed_endpoints(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "deploy" / "simple" / "common.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("smoke_check()", source)
        self.assertIn("/healthz", source)
        self.assertIn("/healthz?deep=1", source)
        self.assertIn("/login", source)
        for forbidden in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
            self.assertNotIn(forbidden, source)

    def test_capture_scripts_require_injected_credentials(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("capture_product.py", "capture_product3.py"):
            with self.subTest(name=name):
                source = (root / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn("PAIHUO_CAPTURE_USERNAME", source)
                self.assertIn("PAIHUO_CAPTURE_PASSWORD", source)
                self.assertNotIn('"password": "123456"', source)
                self.assertNotIn('page.type("#p", "123456"', source)


if __name__ == "__main__":
    unittest.main()
