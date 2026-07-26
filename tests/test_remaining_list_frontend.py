"""剩余大列表必须在真实页面消费服务端分页，而不是只返回元数据。"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RemainingListFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def _function_source(self, name: str, next_name: str) -> str:
        start = self.app_js.index(f"async function {name}(")
        end = self.app_js.index(f"\nasync function {next_name}(", start)
        return self.app_js[start:end]

    def test_dashboard_jobs_use_state_pagination_and_show_total(self):
        source = self._function_source("dashboard", "notificationReadAll")
        self.assertIn('listPath("/state","jobs")', source)
        self.assertIn("normalizeListContract(", source)
        self.assertIn('listContractNotice(jobsContract,"内容工单")', source)
        self.assertIn('listPager(jobsContract,"jobs")', source)

    def test_avatar_jobs_use_server_pagination_and_show_total(self):
        source = self._function_source("avatarView", "avUploadVoice")
        self.assertIn('listPath("/avatar/jobs","avatar")', source)
        self.assertIn('listContractNotice(jobsContract,"数字人任务")', source)
        self.assertIn('listPager(jobsContract,"avatar")', source)

    def test_meetings_use_server_pagination_and_show_total(self):
        source = self._function_source("meetingsView", "mtSuggest")
        self.assertIn('listPath("/meetings","meetings")', source)
        self.assertIn("normalizeListContract(", source)
        self.assertIn('listContractNotice(meetingsContract,"历史会议")', source)
        self.assertIn('listPager(meetingsContract,"meetings")', source)

    def test_all_three_offsets_are_stateful_and_page_size_is_bounded(self):
        self.assertIn(
            "jobs:0,avatar:0,meetings:0",
            self.app_js.replace(" ", ""),
        )
        self.assertIn("const LIST_PAGE_SIZE=40", self.app_js)


if __name__ == "__main__":
    unittest.main()
