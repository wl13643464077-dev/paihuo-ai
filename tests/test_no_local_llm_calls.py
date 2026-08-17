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
            system_prompt_file="/private/runtime/system-prompt.txt",
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
        self.assertIn("--system-prompt-file", command)
        self.assertEqual(
            "/private/runtime/system-prompt.txt",
            command[command.index("--system-prompt-file") + 1],
        )
        self.assertNotIn("--system-prompt", command)

        no_web = llm._runner_command(
            "/opt/claude",
            model="claude-opus-4-8",
            web=False,
            system_prompt_file=None,
        )
        self.assertEqual("", no_web[no_web.index("--tools") + 1])
        self.assertEqual(
            "2",
            no_web[no_web.index("--max-budget-usd") + 1],
        )

    def test_web_runner_uses_explicit_allowlist_and_noninteractive_permissions(self):
        command = llm._runner_command(
            "/opt/claude",
            model="claude-opus-4-8",
            web=True,
        )

        allowed_index = command.index("--allowedTools")
        self.assertEqual("WebSearch", command[allowed_index + 1])
        permission_index = command.index("--permission-mode")
        self.assertEqual("dontAsk", command[permission_index + 1])
        self.assertNotIn("bypassPermissions", command)

    def test_tool_usage_aggregates_only_websearch_metadata(self):
        state = {"chars": 0, "tool_usage": {}}
        progress = []
        llm._step_of(
            {
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "name": "WebSearch",
                        "input": {"query": "secret query must not be retained"},
                    }],
                },
            },
            state,
            lambda kind, label: progress.append((kind, label)),
        )
        llm._step_of(
            {
                "type": "user",
                "message": {
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "is_error": False,
                        "content": "sensitive result body must not be retained",
                    }],
                },
            },
            state,
            lambda kind, label: progress.append((kind, label)),
        )

        self.assertEqual(
            {"WebSearch": {"attempts": 1, "success": 1, "errors": 0}},
            state["tool_usage"],
        )
        self.assertTrue(any(kind == "search" for kind, _ in progress))
        self.assertTrue(any("有效检索 1" in label for _, label in progress))
        self.assertNotIn("sensitive result body", repr(state))

    def test_web_runner_fails_closed_without_successful_websearch(self):
        self.assertTrue(hasattr(llm, "_require_successful_websearch"))
        with self.assertRaises(llm.LLMError):
            llm._require_successful_websearch(
                {"WebSearch": {"attempts": 1, "success": 0, "errors": 1}}
            )

    def test_tool_usage_accepts_single_block_content_and_deduplicates_results(self):
        state = {"chars": 0, "tool_usage": {}}
        progress = lambda *_args: None
        llm._step_of({
            "type": "assistant",
            "message": {"content": {
                "type": "tool_use", "id": "search-1", "name": "WebSearch",
                "input": {"query": "private"},
            }},
        }, state, progress)
        result = {
            "type": "user",
            "message": {"content": {
                "type": "tool_result", "tool_use_id": "search-1",
                "is_error": False, "content": "private result",
            }},
        }
        llm._step_of(result, state, progress)
        llm._step_of(result, state, progress)

        self.assertEqual(
            {"attempts": 1, "success": 1, "errors": 0},
            state["tool_usage"]["WebSearch"],
        )

    def test_anonymous_or_foreign_tool_results_cannot_count_as_websearch_success(self):
        state = {"chars": 0, "tool_usage": {}}
        progress = lambda *_args: None
        llm._step_of({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": "search-1", "name": "WebSearch",
                "input": {"query": "private"},
            }]},
        }, state, progress)
        for block in (
            {"type": "tool_result", "is_error": False},
            {"type": "tool_result", "tool_use_id": "other-1", "is_error": False},
            {"type": "tool_result", "name": "OtherTool", "is_error": False},
        ):
            llm._step_of(
                {"type": "user", "message": {"content": [block]}},
                state,
                progress,
            )

        self.assertEqual(0, state["tool_usage"]["WebSearch"]["success"])

    def test_string_error_marker_is_not_counted_as_success(self):
        state = {"chars": 0, "tool_usage": {}}
        progress = lambda *_args: None
        llm._step_of({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": "search-1", "name": "WebSearch",
                "input": {"query": "private"},
            }]},
        }, state, progress)
        llm._step_of({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "search-1",
                "is_error": "true", "content": "private error",
            }]},
        }, state, progress)

        self.assertEqual(
            {"attempts": 1, "success": 0, "errors": 1},
            state["tool_usage"]["WebSearch"],
        )

    def test_runner_no_longer_uses_a_persistent_project_workdir(self):
        source = (ROOT / "app" / "llm.py").read_text(encoding="utf-8")
        self.assertNotIn('data", "llmwork"', source)
        self.assertIn("TemporaryDirectory", source)


if __name__ == "__main__":
    unittest.main()
