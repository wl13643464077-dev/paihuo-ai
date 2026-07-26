import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendMobileAccessibilityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def test_tables_are_enhanced_with_mobile_labels(self):
        self.assertIn("function enhanceResponsiveTables(", self.app_js)
        self.assertIn("cell.dataset.label=", self.app_js)
        self.assertIn("enhanceResponsiveTables();", self.app_js)

    def test_mobile_tables_render_as_cards(self):
        self.assertIn(".dimtable thead{display:none}", self.index_html)
        self.assertIn(".dimtable tbody,.dimtable tr,.dimtable td{display:block", self.index_html)
        self.assertIn("content:attr(data-label)", self.index_html)
        self.assertIn(".dimtable{min-width:0", self.index_html)

    def test_mobile_interactive_targets_are_at_least_44px(self):
        self.assertIn("min-height:44px", self.index_html)
        self.assertIn("min-width:44px", self.index_html)
        self.assertIn(".switch", self.index_html)


if __name__ == "__main__":
    unittest.main()
