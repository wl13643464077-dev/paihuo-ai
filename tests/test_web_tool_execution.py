"""Streaming WebSearch accounting and fail-closed runner behavior."""
import asyncio
import json
import os
import stat
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import llm


class _Input:
    def __init__(self, *, drain_error=None):
        self.drain_error = drain_error

    def write(self, _data):
        return None

    async def drain(self):
        if self.drain_error:
            raise self.drain_error
        return None

    def close(self):
        return None


class _Output:
    def __init__(self, lines=(), *, readline_error=None, read_error=None):
        self._lines = [line if isinstance(line, bytes) else line.encode()
                       for line in lines]
        self.readline_error = readline_error
        self.read_error = read_error

    async def readline(self):
        if self.readline_error:
            raise self.readline_error
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self, _size):
        if self.read_error:
            raise self.read_error
        return b""


class _Proc:
    def __init__(self, events, *, exit_code=0, feed_error=None,
                 stdout_error=None, stderr_error=None, wait_error=None,
                 pid=None):
        self.stdin = _Input(drain_error=feed_error)
        self.stdout = _Output(
            (json.dumps(event).encode() + b"\n" for event in events),
            readline_error=stdout_error,
        )
        self.stderr = _Output(read_error=stderr_error)
        self.returncode = None
        self.exit_code = exit_code
        self.wait_error = wait_error
        self.killed = False
        if pid is not None:
            self.pid = pid

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.wait_error:
            raise self.wait_error
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode


class WebToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _provider_env(self):
        return {
            "ANTHROPIC_BASE_URL": "https://proxy.example",
            "ANTHROPIC_AUTH_TOKEN": "explicit-key",
        }

    async def test_web_call_returns_only_aggregate_tool_usage(self):
        events = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "search-1", "name": "WebSearch",
                "input": {"query": "private query"},
            }]}},
            {"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "search-1",
                "is_error": False,
                "content": "private search result body",
            }]}, "tool_use_result": {
                "query": "private query must not leak",
                "results": [{
                    "tool_use_id": "internal-result-id",
                    "content": [{
                        "title": "Private source title",
                        "url": "https://example.com/private-source",
                        "body": "private indexed body",
                    }],
                }],
            }},
            {"type": "result", "is_error": False, "result": "answer",
             "usage": {"input_tokens": 1, "output_tokens": 2}},
        ]
        proc = _Proc(events)
        with patch.object(llm, "CLAUDE", sys.executable), \
                patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await llm.call(
                "检索任务", web=True, provider_env=self._provider_env()
            )

        self.assertEqual("answer", result["text"])
        self.assertEqual(
            {"WebSearch": {"attempts": 1, "success": 1, "errors": 0}},
            result["tool_usage"],
        )
        self.assertNotIn("private search result body", repr(result))
        self.assertNotIn("web_sources", result)
        self.assertNotIn("private query must not leak", repr(result))
        self.assertNotIn("private indexed body", repr(result))

    async def test_opt_in_capture_returns_only_sanitized_structured_sources(self):
        long_title = "  First\nsource\x00 " + ("长" * 200)
        events = [
            {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "search-1", "name": "WebSearch",
                "input": {"query": "private query"},
            }]}},
            {"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "search-1",
                "is_error": False,
                "content": "https://attacker.invalid/from-tool-result-content",
            }]}, "tool_use_result": {
                "query": "another private query",
                "unknown": "private unknown field",
                "results": [
                    "https://attacker.invalid/string-result",
                    {
                        "tool_use_id": "internal-result-id",
                        "content": [{
                            "title": long_title,
                            "url": "  https://example.com/posts/1  ",
                            "body": "private result body",
                            "snippet": "private snippet",
                        }],
                    },
                ],
            }},
            {"type": "result", "is_error": False,
             "result": "https://attacker.invalid/from-model-text"},
        ]
        proc = _Proc(events)
        with patch.object(llm, "CLAUDE", sys.executable), \
                patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await llm.call(
                "检索任务", web=True, capture_web_sources=True,
                provider_env=self._provider_env(),
            )

        self.assertEqual(1, len(result["web_sources"]))
        self.assertEqual(
            "https://example.com/posts/1",
            result["web_sources"][0]["source_url"],
        )
        title = result["web_sources"][0]["source_title"]
        self.assertTrue(title.startswith("First source "))
        self.assertLessEqual(len(title), 160)
        serialized = repr(result["web_sources"])
        for secret in (
            "private query", "another private query", "internal-result-id",
            "private result body", "private snippet", "private unknown field",
            "from-tool-result-content", "from-model-text", "string-result",
        ):
            self.assertNotIn(secret, serialized)

    async def test_opt_in_cannot_capture_when_web_execution_is_disabled(self):
        events = [
            {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "search-1", "name": "WebSearch",
            }]}},
            {"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "search-1",
                "is_error": False,
            }]}, "tool_use_result": {"results": [{"content": [{
                "title": "Must stay unavailable",
                "url": "https://example.com/disabled-web",
            }]}]}},
            {"type": "result", "is_error": False, "result": "answer"},
        ]
        proc = _Proc(events)
        with patch.object(llm, "CLAUDE", sys.executable), \
                patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await llm.call(
                "普通任务", web=False, capture_web_sources=True,
                provider_env=self._provider_env(),
            )

        self.assertEqual([], result["web_sources"])

    def test_capture_requires_one_explicit_correlated_successful_websearch(self):
        source_payload = {
            "query": "private",
            "results": [{"tool_use_id": "internal", "content": [{
                "title": "Should not escape",
                "url": "https://example.com/not-eligible",
            }]}],
        }
        cases = {
            "mismatched_id": (
                [{"type": "tool_use", "id": "search-1", "name": "WebSearch"}],
                [{"type": "tool_result", "tool_use_id": "search-2",
                  "is_error": False}],
            ),
            "errored_search": (
                [{"type": "tool_use", "id": "search-1", "name": "WebSearch"}],
                [{"type": "tool_result", "tool_use_id": "search-1",
                  "is_error": True}],
            ),
            "unknown_error_marker_fails_closed": (
                [{"type": "tool_use", "id": "search-1", "name": "WebSearch"}],
                [{"type": "tool_result", "tool_use_id": "search-1",
                  "is_error": {"unexpected": "private"}}],
            ),
            "id_alias_is_not_outer_tool_use_id": (
                [{"type": "tool_use", "id": "search-1", "name": "WebSearch"}],
                [{"type": "tool_result", "id": "search-1",
                  "is_error": False}],
            ),
            "webfetch": (
                [{"type": "tool_use", "id": "fetch-1", "name": "WebFetch"}],
                [{"type": "tool_result", "tool_use_id": "fetch-1",
                  "name": "WebFetch", "is_error": False}],
            ),
            "anonymous_ambiguity": (
                [
                    {"type": "tool_use", "name": "WebSearch"},
                    {"type": "tool_use", "name": "WebSearch"},
                ],
                [{"type": "tool_result", "tool_use_id": "unknown",
                  "is_error": False}],
            ),
            "multiple_correlated_results_in_one_event": (
                [
                    {"type": "tool_use", "id": "search-1", "name": "WebSearch"},
                    {"type": "tool_use", "id": "search-2", "name": "WebSearch"},
                ],
                [
                    {"type": "tool_result", "tool_use_id": "search-1",
                     "is_error": False},
                    {"type": "tool_result", "tool_use_id": "search-2",
                     "is_error": False},
                ],
            ),
        }
        for name, (tool_uses, tool_results) in cases.items():
            with self.subTest(name=name):
                state = {
                    "chars": 0,
                    "tool_usage": {},
                    "_capture_web_sources": True,
                }
                llm._step_of(
                    {"type": "assistant", "message": {"content": tool_uses}},
                    state,
                    lambda *_args: None,
                )
                llm._step_of(
                    {
                        "type": "user",
                        "message": {"content": tool_results},
                        "tool_use_result": source_payload,
                    },
                    state,
                    lambda *_args: None,
                )
                self.assertEqual([], llm._public_web_sources(state))
                self.assertNotIn("Should not escape", repr(state))

    def test_capture_deduplicates_bounds_and_rejects_malformed_sources(self):
        valid = [{
            "title": f"Source {index}",
            "url": f"https://example.com/posts/{index}",
        } for index in range(40)]
        content = [
            valid[0],
            {"title": "Duplicate wins later", "url": valid[0]["url"]},
            {"title": "Too long", "url": "https://example.com/" + "x" * 2049},
            {"title": "Too long before trimming", "url": " " + "x" * 2048},
            {"title": "Missing URL"},
            {"url": "https://example.com/missing-title"},
            {"title": 123, "url": "https://example.com/non-string-title"},
            {"title": "Non-string URL", "url": {"href": "https://example.com"}},
            {"title": "Embedded whitespace", "url": "https://example.com/bad path"},
        ] + valid[1:]
        state = {
            "chars": 0,
            "tool_usage": {},
            "_capture_web_sources": True,
        }
        llm._step_of({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": "search-1", "name": "WebSearch",
            }]},
        }, state, lambda *_args: None)
        llm._step_of({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "search-1",
                "is_error": False,
                "content": "do not parse https://attacker.invalid/body",
            }]},
            "tool_use_result": {
                "query": "do not retain",
                "results": [
                    {"tool_use_id": "internal", "content": content},
                    {"content": "not-a-list"},
                    7,
                    "not-a-dict",
                ],
            },
        }, state, lambda *_args: None)

        sources = llm._public_web_sources(state)
        self.assertEqual(32, len(sources))
        self.assertEqual(valid[0]["url"], sources[0]["source_url"])
        self.assertEqual(32, len({item["source_url"] for item in sources}))
        serialized = repr(state)
        self.assertNotIn("do not retain", serialized)
        self.assertNotIn("attacker.invalid", serialized)
        self.assertNotIn("internal", serialized)
        self.assertNotIn("Duplicate wins later", serialized)
        self.assertNotIn("Too long", serialized)
        self.assertNotIn("missing-title", serialized)

    async def test_web_call_rejects_text_without_successful_search(self):
        proc = _Proc([
            {"type": "result", "is_error": False, "result": "ungrounded answer"},
        ])
        with patch.object(llm, "CLAUDE", sys.executable), \
                patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with self.assertRaises(llm.LLMError):
                await llm.call(
                    "检索任务", web=True, provider_env=self._provider_env()
                )

    async def test_system_prompt_uses_private_ephemeral_file_not_argv(self):
        secret = "INTERNAL-SKILLS-AND-CAPABILITIES"
        observed = {}
        proc = _Proc([{
            "type": "result", "is_error": False, "result": "answer",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }])

        async def spawn(*argv, **kwargs):
            observed["argv"] = argv
            observed["kwargs"] = kwargs
            prompt_path = argv[argv.index("--system-prompt-file") + 1]
            observed["prompt_path"] = prompt_path
            with open(prompt_path, encoding="utf-8") as handle:
                observed["prompt"] = handle.read()
            observed["prompt_mode"] = stat.S_IMODE(os.stat(prompt_path).st_mode)
            observed["runtime_mode"] = stat.S_IMODE(
                os.stat(os.path.dirname(prompt_path)).st_mode
            )
            return proc

        with patch.object(llm, "CLAUDE", sys.executable), \
                patch("asyncio.create_subprocess_exec", side_effect=spawn):
            result = await llm.call(
                "user task",
                system_prompt=f"  {secret}  ",
                provider_env=self._provider_env(),
            )

        self.assertEqual("answer", result["text"])
        self.assertIn("--system-prompt-file", observed["argv"])
        self.assertNotIn("--system-prompt", observed["argv"])
        self.assertNotIn(secret, observed["argv"])
        self.assertEqual(secret, observed["prompt"])
        self.assertEqual(0o600, observed["prompt_mode"])
        self.assertEqual(0o700, observed["runtime_mode"])
        self.assertFalse(os.path.exists(observed["prompt_path"]))
        self.assertTrue(observed["kwargs"]["start_new_session"])

    async def test_nonzero_runner_exit_rejects_even_a_result_event(self):
        proc = _Proc([{
            "type": "result", "is_error": False, "result": "must reject",
        }], exit_code=7)
        with patch.object(llm, "CLAUDE", sys.executable), \
                patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with self.assertRaisesRegex(
                llm.LLMError, "^云雾能力网关执行失败，请稍后重试$"
            ):
                await llm.call("task", provider_env=self._provider_env())

    async def test_io_and_wait_failures_terminate_runner_and_fail_stably(self):
        failure_cases = (
            {"feed_error": RuntimeError("private feed failure")},
            {"stdout_error": RuntimeError("private stdout failure")},
            {"stderr_error": RuntimeError("private stderr failure")},
            {"wait_error": RuntimeError("private wait failure")},
        )
        for case in failure_cases:
            with self.subTest(case=next(iter(case))):
                proc = _Proc([], **case)
                with patch.object(llm, "CLAUDE", sys.executable), \
                        patch(
                            "asyncio.create_subprocess_exec",
                            AsyncMock(return_value=proc),
                        ):
                    with self.assertRaisesRegex(
                        llm.LLMError,
                        "^云雾能力网关执行失败，请稍后重试$",
                    ):
                        await llm.call("task", provider_env=self._provider_env())
                self.assertTrue(proc.killed)

    async def test_failure_terminates_independent_process_group_when_available(self):
        proc = _Proc(
            [], stdout_error=RuntimeError("reader failed"), pid=43210
        )

        def mark_group_killed(_pid, _signal):
            proc.returncode = -9

        spawn = AsyncMock(return_value=proc)
        killpg = Mock(side_effect=mark_group_killed)
        with patch.object(llm, "CLAUDE", sys.executable), \
                patch("asyncio.create_subprocess_exec", spawn), \
                patch("os.killpg", killpg):
            with self.assertRaises(llm.LLMError):
                await llm.call("task", provider_env=self._provider_env())

        self.assertTrue(spawn.await_args.kwargs["start_new_session"])
        killpg.assert_called_once()
        self.assertEqual(43210, killpg.call_args.args[0])

    def test_foreign_named_result_cannot_settle_anonymous_websearch(self):
        state = {"chars": 0, "tool_usage": {}}
        progress = lambda *_args: None
        llm._step_of({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "name": "WebSearch",
                "input": {"query": "private"},
            }]},
        }, state, progress)
        for result in (
            {"type": "tool_result", "tool_use_id": "fetch-1",
             "name": "WebFetch", "is_error": False},
            {"type": "tool_result", "tool_use_id": "other-1",
             "tool_name": "UnknownTool", "is_error": False},
        ):
            llm._step_of(result, state, progress)

        self.assertEqual(
            {"attempts": 1, "success": 0, "errors": 0},
            state["tool_usage"]["WebSearch"],
        )

    def test_foreign_name_overrides_even_a_matching_websearch_id(self):
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
            "type": "tool_result", "tool_use_id": "search-1",
            "name": "WebFetch", "is_error": False,
        }, state, progress)

        self.assertEqual(0, state["tool_usage"]["WebSearch"]["success"])

    def test_id_result_without_name_requires_one_anonymous_search(self):
        state = {"chars": 0, "tool_usage": {}}
        progress = lambda *_args: None
        for _ in range(2):
            llm._step_of({
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use", "name": "WebSearch",
                    "input": {"query": "private"},
                }]},
            }, state, progress)
        llm._step_of({
            "type": "tool_result", "tool_use_id": "ambiguous-result",
            "is_error": False,
        }, state, progress)

        self.assertEqual(0, state["tool_usage"]["WebSearch"]["success"])


if __name__ == "__main__":
    unittest.main()
