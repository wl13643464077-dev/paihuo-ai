"""三人设走查修复批A的合同:报错说人话、500 不吐英文、游客留资出口放行。"""
import asyncio
import os
import types
import unittest

from fastapi import HTTPException

from app import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class WalkthroughFixContracts(unittest.TestCase):
    def test_brief_field_errors_use_chinese_labels(self):
        with self.assertRaises(HTTPException) as ctx:
            main._validated_brief({"direction": "x", "material": "字" * 20001})
        self.assertIn("附加素材", ctx.exception.detail)
        self.assertNotIn("material", ctx.exception.detail)
        with self.assertRaises(HTTPException) as bad_type:
            main._validated_brief({"direction": "x", "ref_link": 123})
        self.assertIn("参考链接", bad_type.exception.detail)

    def test_unhandled_exception_returns_chinese_without_leak(self):
        request = types.SimpleNamespace(
            url=types.SimpleNamespace(path="/api/x"))
        secret = RuntimeError("secret-internal-detail")
        response = asyncio.run(main._unhandled_exception(request, secret))
        self.assertEqual(500, response.status_code)
        body = response.body.decode("utf-8")
        self.assertNotIn("secret-internal-detail", body)
        self.assertNotIn("Internal Server Error", body)
        self.assertIn("系统开小差", body)

    def test_tour_middleware_allows_feedback_lead_capture(self):
        src = _read("app/main.py")
        self.assertIn('path == "/api/feedback" and request.method == "POST"',
                      src, "游客留资出口必须在参观模式拦截前放行")

    def test_render_catch_treats_403_as_permission_not_network(self):
        src = _read("static/app.js")
        self.assertIn("这个页面需要更高权限", src)
        self.assertIn('e?.status===403', src)

    def test_job_detail_delete_button_admin_gated(self):
        src = _read("static/app.js")
        self.assertNotIn('title="彻底删除工单及全部产物"', src,
                         "工单详情页删除钮的文案与回收站语义矛盾且对 member 裸奔")

    def test_meeting_pool_filters_by_member_modules(self):
        src = _read("static/app.js")
        self.assertIn("DEPTS.filter(d=>can(d.key)).flatMap", src,
                      "会议室手工选人池必须按板块过滤,否则 member 勾了专家才 403")


if __name__ == "__main__":
    unittest.main()
