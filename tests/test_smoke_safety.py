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

    def test_release_smoke_only_logs_in_and_reads_fixed_endpoints(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "deploy" / "smoke_readonly.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("SMOKE_USERNAME", source)
        self.assertIn("SMOKE_PASSWORD", source)
        self.assertNotIn('"boss"', source)
        self.assertNotIn('"123456"', source)
        for forbidden in ('"PUT"', '"PATCH"', '"DELETE"'):
            self.assertNotIn(forbidden, source)
        self.assertEqual(source.count('"POST"'), 1)
        self.assertIn('"/api/auth/login"', source)
        self.assertNotIn("/api/tasks", source)
        self.assertNotIn("/api/jobs", source)

        main_source = (root / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('if not username.startswith("__release_smoke_"):', main_source)

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
