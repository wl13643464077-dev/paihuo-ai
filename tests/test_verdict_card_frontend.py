"""前端结论卡纯函数：用 node 直接执行 static/app.js 里抽出的函数（无 DOM）。"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


def function(source: str, name: str) -> str:
    start = source.find(f"function {name}(")
    if start < 0:
        raise AssertionError(f"missing JavaScript function: {name}")
    ends = [
        position for position in (
            source.find("\nfunction ", start + 1),
            source.find("\nasync function ", start + 1),
            source.find("\nconst ", start + 1),
        ) if position >= 0
    ]
    return source[start:min(ends) if ends else len(source)]


def const_line(source: str, name: str) -> str:
    match = re.search(rf"^const {name} = .*$", source, re.M)
    if not match:
        raise AssertionError(f"missing JavaScript const: {name}")
    return match.group(0)


@unittest.skipUnless(shutil.which("node"), "node 不可用")
class VerdictCardFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def _run(self, body: str):
        prelude = "\n".join([
            const_line(self.source, "esc"),
            const_line(self.source, "DEFAULT_JOB_MODE"),
            const_line(self.source, "JOB_MODE_OPTIONS"),
            *(function(self.source, name) for name in (
                "vcClean", "vcShort", "parseBossBrief", "briefFromMarkdown",
                "renderVerdictCard", "renderStageProgress", "deliveryBrief",
                "jobModeOptions",
            )),
        ])
        script = prelude + "\nconst out = (()=>{" + body + "})();\nprocess.stdout.write(JSON.stringify(out));"
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, text=True,
            timeout=20, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_card_renders_verdict_three_actions_with_dispatch_buttons_and_watch(self):
        out = self._run("""
          const data = parseBossBrief("**一句话结论**：能租，先谈免租期\\n\\n**今天/本周就做这 3 件事**\\n1. 店长：周五前约房东\\n2. 老板：周六数客流\\n3. 财务：算\\"租金\\"<占比>\\n\\n**⚠️ 要留意**：对面商场明年开业");
          return {data, html: renderVerdictCard(data)};
        """)
        self.assertEqual("能租，先谈免租期", out["data"]["verdict"])
        self.assertEqual(3, len(out["data"]["actions"]))
        html = out["html"]
        self.assertIn("能租，先谈免租期", html)
        self.assertEqual(3, html.count("派给店员</button>"))
        self.assertEqual(3, html.count('onclick="verdictDispatch(this)"'))
        self.assertIn('data-text="店长：周五前约房东"', html)
        self.assertIn("对面商场明年开业", html)
        # 行动文字进属性和正文都要转义，不能破坏 HTML。
        self.assertIn("&quot;租金&quot;&lt;占比&gt;", html)
        self.assertNotIn("<占比>", html)

    def test_empty_data_renders_nothing(self):
        out = self._run("return [renderVerdictCard(null), renderVerdictCard({}), parseBossBrief('')];")
        self.assertEqual(["", "", None], out)

    def test_legacy_summary_and_markdown_fallback(self):
        out = self._run("""
          const legacy = parseBossBrief("- 客流稳定\\n- 租金可谈\\n- 👉 **一句话行动建议**:本周约房东");
          const fallback = briefFromMarkdown("# 报告\\n## 结论\\n可以租。理由很多。\\n## 下一步建议\\n1. 店长：约房东\\n2. 老板：数客流\\n3. 财务：算租金\\n4. 多余");
          const bare = briefFromMarkdown("就一句话");
          return {legacy, fallback, bare};
        """)
        self.assertEqual("客流稳定", out["legacy"]["verdict"])
        self.assertEqual(["本周约房东"], out["legacy"]["actions"])
        self.assertEqual("可以租。", out["fallback"]["verdict"])
        self.assertEqual(["店长：约房东", "老板：数客流", "财务：算租金"], out["fallback"]["actions"])
        self.assertEqual(1, len(out["bare"]["actions"]))

    def test_stage_progress_shows_plain_stages_and_time(self):
        out = self._run("""
          return renderStageProgress({stages:["正在查资料","正在写方案","正在检查","马上好"],
            current:1,label:"正在写方案",elapsed_seconds:185,eta_seconds:300,hint:"预计还要约 5 分钟"});
        """)
        for text in ("正在查资料", "✓ 正在查资料", "正在写方案", "马上好", "已用 3 分钟", "预计还要约 5 分钟"):
            self.assertIn(text, out)
        self.assertIn('aria-current="step"', out)

    def test_delivery_brief_has_three_actions_and_gate_warning(self):
        out = self._run("""
          const ok = deliveryBrief({title:"火锅店周末引流", packs:[
            {platform:"小红书", best_time:"周五 19:00"}, {platform:"抖音"}], gate:{passed:true}});
          const bad = deliveryBrief({title:"x", packs:[], gate:{passed:false, issues:[{detail:"出现绝对化用语「最便宜」"}]}});
          return {ok, bad};
        """)
        self.assertIn("2 个平台", out["ok"]["verdict"])
        self.assertEqual(3, len(out["ok"]["actions"]))
        self.assertEqual("店员：复制「小红书」版本，周五 19:00 发出去", out["ok"]["actions"][0])
        self.assertTrue(all(len(a) <= 40 for a in out["ok"]["actions"]))
        self.assertIn("质检", out["bad"]["verdict"])
        self.assertEqual("出现绝对化用语「最便宜」", out["bad"]["watch"])

    def test_new_content_form_defaults_to_final_review_only(self):
        out = self._run("""
          return {fresh: jobModeOptions(DEFAULT_JOB_MODE), saved: jobModeOptions("copilot"),
                  legacy: jobModeOptions("fullauto")};
        """)
        self.assertIn('<option value="autopilot" selected>全交给 AI，发之前我看一眼（推荐）</option>', out["fresh"])
        self.assertIn("关键几步我把关", out["fresh"])
        self.assertIn("每一步我都看", out["fresh"])
        self.assertNotIn("fullauto", out["fresh"])
        self.assertIn('<option value="copilot" selected>', out["saved"])
        self.assertIn('<option value="fullauto" selected>', out["legacy"])


if __name__ == "__main__":
    unittest.main()
