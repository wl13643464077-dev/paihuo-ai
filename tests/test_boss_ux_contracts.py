"""最近两批老板视角 UI/接口承诺的合同测试.

前端承诺按 tests/test_frontend_navigation.py 的先例做源码扫描(选稳定锚点,
不钉整行);后端承诺按 tests/test_sweep_fixes.py 的临时库模式做行为测试:

- 定时任务表单先亮成本账(schedCost),重新启用时清「已暂停」残留;
- 资产库标题搜索(前端带 q,后端只按标题 LIKE);
- 矩阵账号 cookie 输入框按密码处理,不明文回显;
- 人设语料上传走后端 parse-file,接受 Word/PDF;
- 发布台账自动复盘总开关可查可关;
- 回收站覆盖人设档案与资产两个新类目;
- 员工进修完成/失败都有看得懂的通知文案;
- 企业档案返回 filled/total_fields 进度。
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from app import auth, db, main, notify, pubtrack

ROOT = Path(__file__).resolve().parents[1]


def _js_function(source: str, name: str) -> str:
    """截取一个顶层函数的源码段:从定义处到下一个顶层函数定义."""
    marker = f"function {name}("
    start = source.index(marker)
    candidates = [
        pos for pos in (
            source.find("\nfunction ", start + 1),
            source.find("\nasync function ", start + 1),
        ) if pos >= 0
    ]
    end = min(candidates) if candidates else len(source)
    return source[start:end]


class BossUxFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_schedule_form_shows_cost_estimate_before_committing(self):
        # 频率表单必须有独立的成本预估函数,且切频率/改小时数时会重算。
        self.assertIn("function schedCost(", self.app_js)
        self.assertIn('id="s-cost"', self.app_js)
        kind_ui = _js_function(self.app_js, "schedKindUI")
        self.assertIn("schedCost()", kind_ui, "切换频率后必须重算成本预估")

    def test_asset_library_search_is_server_side_by_title(self):
        # 搜索框存在,且查询参数随列表请求发给服务端(而非前端过滤)。
        self.assertIn('id="as-q"', self.app_js)
        self.assertIn("q:ASSET_Q", self.app_js)

    def test_matrix_cookie_input_is_masked_like_a_password(self):
        tag = re.search(r'<input[^>]*\bid="mx-cookie"[^>]*>', self.app_js)
        self.assertIsNotNone(tag, "找不到矩阵账号 cookie 输入框")
        self.assertIn('type="password"', tag.group(0),
                      "cookie 等同登录密码,输入框必须打码")

    def test_profile_corpus_upload_uses_backend_parse_file(self):
        loader = _js_function(self.app_js, "loadProfileFiles")
        self.assertIn("parseFileInto(input", loader,
                      "人设语料上传必须复用后端 parse-file 抽取文字")
        tag = re.search(
            r'<input[^>]*onchange="loadProfileFiles\(this\)"[^>]*>',
            self.app_js,
        )
        self.assertIsNotNone(tag, "找不到人设语料的文件选择框")
        accept = re.search(r'accept="([^"]*)"', tag.group(0))
        self.assertIsNotNone(accept, "文件选择框必须声明 accept 白名单")
        self.assertIn(".docx", accept.group(1))
        self.assertIn(".pdf", accept.group(1))

    def test_trash_labels_cover_profile_and_asset(self):
        start = self.app_js.index("const TRASH_KIND_LABEL")
        label_src = self.app_js[start:self.app_js.index("}", start) + 1]
        self.assertIn("profile:", label_src)
        self.assertIn("asset:", label_src)


class BossUxBackendStaticContractTests(unittest.TestCase):
    def test_trash_tables_cover_profile_and_asset(self):
        self.assertIn("profile", main._TRASH_TABLES)
        self.assertIn("asset", main._TRASH_TABLES)
        self.assertEqual("account_profile", main._TRASH_TABLES["profile"][0])
        self.assertEqual("asset", main._TRASH_TABLES["asset"][0])


class BossUxBehaviorCase(unittest.TestCase):
    """行为合同:临时库直调 main 的端点函数(参考 test_sweep_fixes.py)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "boss_ux.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "测试企业", "balance": 50})
        auth.set_current({
            "id": 20, "tenant_id": 2, "username": "owner",
            "role": "owner", "modules": ["content", "library"],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    # ---- 定时任务:重新启用清「已暂停」残留 ----

    def _make_schedule(self, note):
        return db.insert("schedule", {
            "tenant_id": 2, "name": "每日选题",
            "brief_json": '{"direction":"本行业新动态"}',
            "kind": "daily", "at_time": "09:00",
            "enabled": 0, "last_note": note,
        })

    def test_reenable_clears_paused_note_with_recovery_message(self):
        sid = self._make_schedule("已暂停:点数不足(需 18 点)")
        main.schedule_update(sid, {"enabled": True})
        row = db.one("SELECT enabled,last_note FROM schedule WHERE id=?", (sid,))
        self.assertEqual(1, row["enabled"])
        self.assertTrue(str(row["last_note"]).startswith("已重新启用"),
                        f"残留文案未被替换: {row['last_note']!r}")
        self.assertNotIn("已暂停", row["last_note"])

    def test_reenable_keeps_ordinary_last_note(self):
        sid = self._make_schedule("上次 09:00 已自动开工")
        main.schedule_update(sid, {"enabled": True})
        row = db.one("SELECT last_note FROM schedule WHERE id=?", (sid,))
        self.assertEqual("上次 09:00 已自动开工", row["last_note"],
                         "非暂停残留的运行记录不应被覆盖")

    # ---- 资产库:q 只按标题 LIKE ----

    def test_assets_q_matches_title_only(self):
        hit = db.insert("asset", {
            "tenant_id": 2, "type": "topic",
            "payload_json": json.dumps(
                {"title": "麻辣烫新店开业选题", "angle": "本地生活"},
                ensure_ascii=False),
        })
        db.insert("asset", {
            "tenant_id": 2, "type": "topic",
            "payload_json": json.dumps(
                {"title": "火锅新品测评", "angle": "顺带聊麻辣烫"},
                ensure_ascii=False),
        })
        page = main.assets(type="topic", limit=20, offset=0, q="麻辣烫")
        self.assertEqual([hit], [row["id"] for row in page["items"]],
                         "q 只应命中标题包含关键词的资产")
        self.assertEqual(1, page["total"])

    # ---- 发布台账:自动复盘总开关 ----

    def test_publog_auto_retro_toggle_flips_tenant_switch(self):
        self.assertEqual({"enabled": True}, main.publog_auto_retro_get(),
                         "自动复盘默认开启")
        res = main.publog_auto_retro_set({"enabled": False})
        self.assertFalse(res["enabled"])
        self.assertFalse(pubtrack.auto_enabled(2))
        self.assertEqual({"enabled": False}, main.publog_auto_retro_get())
        main.publog_auto_retro_set({"enabled": True})
        self.assertTrue(pubtrack.auto_enabled(2))

    # ---- 进修通知文案 ----

    def test_learn_notifications_have_readable_messages(self):
        for kind in ("learn_done", "learn_failed"):
            msg = notify.build_msg(
                kind, {"title": "王牌文案", "new": 2, "total": 7})
            self.assertTrue(msg.strip(), f"{kind} 文案不能为空")
            self.assertIn("进修", msg, f"{kind} 文案要说清是进修事件")
        refund = notify.build_msg(
            "learn_done", {"title": "王牌文案", "new": 0, "total": 7})
        self.assertIn("退回", refund, "新学 0 条时要说明已退点")

    # ---- 企业档案:filled/total_fields ----

    def test_company_get_reports_field_progress(self):
        db.set_setting(
            "company_profile:2",
            json.dumps({"brand": "川小福", "tone": "亲切"}, ensure_ascii=False),
        )
        data = main.company_get()
        self.assertEqual(2, data["filled"])
        self.assertEqual(len(main._COMPANY_FIELDS), data["total_fields"])
        self.assertGreaterEqual(data["total_fields"], data["filled"])
        self.assertTrue(data["injected"])


if __name__ == "__main__":
    unittest.main()
