"""全局防回退审计：业务模块不得直调底层 Claude 工具执行器。"""
import ast
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import llm


ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {ROOT / "app" / "providers.py"}


class NoLocalModelFallbackTests(unittest.TestCase):
    def test_only_provider_gateway_may_call_llm_runner(self):
        offenders = []
        for path in (ROOT / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                owner = node.func.value
                if (isinstance(owner, ast.Name) and owner.id == "llm"
                        and node.func.attr in {"call", "call_json"} and path not in ALLOWED):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} llm.{node.func.attr}")
        self.assertEqual(offenders, [], "发现绕过云雾供应商层的调用：\n" + "\n".join(offenders))

    def test_runner_rejects_missing_explicit_api_credentials(self):
        source = (ROOT / "app" / "llm.py").read_text(encoding="utf-8")
        self.assertNotIn('get_setting("anthropic_api_key")', source)
        self.assertNotIn('"/root/.local/bin/claude"', source)
        self.assertIn("CONTENTCREW_CLAUDE_PATH", source)
        self.assertIn("禁止使用本地 Claude 登录态", source)

    def test_runner_environment_cannot_inherit_local_claude_state_or_hooks(self):
        hostile_parent = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/root",
            "CLAUDE_CONFIG_DIR": "/root/.claude",
            "CLAUDECODE": "1",
            "ANTHROPIC_API_KEY": "host-login-key",
            "BASH_ENV": "/root/host-hook.sh",
            "PYTHONPATH": "/root/local-plugins",
            "HTTPS_PROXY": "http://proxy.example:8080",
        }
        with tempfile.TemporaryDirectory() as runtime_root:
            cli_home, workdir = llm._prepare_runner_dirs(runtime_root)
            with patch.dict(os.environ, hostile_parent, clear=True):
                env = llm._isolated_runner_env(
                    {
                        "ANTHROPIC_BASE_URL": "https://api.example",
                        "ANTHROPIC_AUTH_TOKEN": "explicit-api-token",
                        "BASH_ENV": "/tmp/injected-hook.sh",
                    },
                    {"CLAUDE_CONFIG_DIR": "/tmp/should-not-win"},
                    cli_home=cli_home,
                )

            self.assertEqual(env["HOME"], cli_home)
            self.assertTrue(env["CLAUDE_CONFIG_DIR"].startswith(cli_home))
            self.assertEqual(
                os.path.commonpath((runtime_root, cli_home)), runtime_root
            )
            self.assertEqual(
                os.path.commonpath((runtime_root, workdir)), runtime_root
            )
            self.assertEqual(os.listdir(workdir), [])
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "explicit-api-token")
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("CLAUDECODE", env)
            self.assertNotIn("BASH_ENV", env)
            self.assertNotIn("PYTHONPATH", env)
            self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:8080")

    def test_runner_command_disables_disk_customizations_and_unknown_tools(self):
        command = llm._runner_command(
            "/opt/claude",
            model="claude-opus-4-8",
            web=True,
            system_prompt="isolated system",
        )

        for flag in (
            "--safe-mode",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--no-chrome",
            "--setting-sources",
        ):
            self.assertIn(flag, command)
        source_index = command.index("--setting-sources")
        self.assertEqual("", command[source_index + 1])
        tools_index = command.index("--tools")
        self.assertEqual("WebSearch", command[tools_index + 1])
        self.assertNotIn("WebFetch", command[tools_index + 1])
        budget_index = command.index("--max-budget-usd")
        self.assertEqual("5", command[budget_index + 1])
        self.assertNotIn("--max-turns", command)
        self.assertNotIn("Bash,Read,Write", " ".join(command))

        no_web = llm._runner_command(
            "/opt/claude",
            model="claude-opus-4-8",
            web=False,
            system_prompt=None,
        )
        self.assertEqual("", no_web[no_web.index("--tools") + 1])
        self.assertEqual(
            "2",
            no_web[no_web.index("--max-budget-usd") + 1],
        )

    def test_runner_no_longer_uses_a_persistent_project_workdir(self):
        source = (ROOT / "app" / "llm.py").read_text(encoding="utf-8")
        self.assertNotIn('data", "llmwork"', source)
        self.assertIn("TemporaryDirectory", source)


if __name__ == "__main__":
    unittest.main()
