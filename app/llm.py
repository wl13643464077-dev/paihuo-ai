"""云雾 Claude 工具执行器:通过 claude CLI 无头运行 WebSearch.

业务路由在 app/providers.py。本模块只接受调用方显式传入的云雾兼容
base URL/token,绝不读取本地 Claude 登录态或历史 Anthropic Key 设置。
网页正文由应用自己的逐跳 SSRF 防护网关读取，不把任意 URL 访问权交给 CLI。
"""
import asyncio
import json
import os
import re
import signal
import shutil
import tempfile

CLAUDE = (
    os.environ.get("CONTENTCREW_CLAUDE_PATH")
    or shutil.which("claude")
    or ""
)
_PARENT_ENV_ALLOWLIST = {
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
}
_PROVIDER_ENV_ALLOWLIST = {
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
}

DEFAULT_MODEL = "claude-opus-4-8"

# 并发闸门:全局最多同时 3 个 LLM 调用,防止把服务器打满
_sem = asyncio.Semaphore(3)

# 运行中的 CLI 子进程注册表:token -> proc(供"老板打断"随时终止)
RUNNING: dict = {}


def kill(token_prefix: str) -> int:
    """终止 token 以指定前缀开头的所有运行中调用,返回杀掉的进程数."""
    n = 0
    for t, proc in list(RUNNING.items()):
        if t.startswith(token_prefix):
            if _kill_process_tree(proc):
                n += 1
    return n


class LLMError(Exception):
    pass


_STABLE_RUNNER_ERROR = "云雾能力网关执行失败，请稍后重试"


def _kill_process_tree(proc) -> bool:
    """Best-effort kill of the isolated CLI process group, then the process."""
    if proc is None:
        return False
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
            return True
        except Exception:
            # Test doubles and unusual platforms may not expose a usable
            # process group. Fall back to the subprocess API below.
            pass
    if getattr(proc, "returncode", None) is not None:
        return False
    try:
        proc.kill()
        return True
    except Exception:
        return False


def _event_blocks(value) -> list[dict]:
    """Normalize stream-json content without retaining tool payloads."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _tool_result_is_error(block: dict) -> bool:
    marker = block.get("is_error")
    if isinstance(marker, str):
        marker = marker.strip().lower() in {"1", "true", "yes", "y", "on"}
    elif isinstance(marker, (int, float)) and not isinstance(marker, bool):
        marker = marker != 0
    elif marker is not None and not isinstance(marker, bool):
        # Unknown structured markers are untrusted; fail closed at the source
        # provenance boundary instead of treating them as a successful result.
        marker = True
    error = block.get("error")
    return marker is True or bool(error)


_MAX_WEB_SOURCES = 32
_MAX_WEB_SOURCE_TITLE = 160
_MAX_WEB_SOURCE_URL = 2048


def _clean_web_source_title(value) -> str:
    """Return bounded display metadata without retaining hidden controls."""
    if not isinstance(value, str):
        return ""
    printable = "".join(ch if ch.isprintable() else " " for ch in value)
    return re.sub(r"\s+", " ", printable).strip()[:_MAX_WEB_SOURCE_TITLE]


def _clean_web_source_url(value) -> str:
    """Keep a complete URL or reject it; source URLs are never truncated."""
    if not isinstance(value, str):
        return ""
    if len(value) > _MAX_WEB_SOURCE_URL:
        return ""
    url = value.strip()
    if not url:
        return ""
    # Whitespace/control characters inside a URL are not safe link metadata.
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        return ""
    return url


def _capture_web_sources(ev: dict, state: dict) -> None:
    """Capture only the documented, top-level WebSearch source metadata.

    The caller must first establish that this *same user event* contains one
    successful outer tool_result correlated to a known WebSearch tool-use id.
    Query text, tool-result content, snippets, bodies and internal ids are
    intentionally never inspected or retained here.
    """
    if not state.get("_capture_web_sources"):
        return
    tool_use_result = ev.get("tool_use_result")
    if not isinstance(tool_use_result, dict):
        return
    results = tool_use_result.get("results")
    if not isinstance(results, list):
        return

    sources = state.setdefault("_web_sources", [])
    seen = state.setdefault("_web_source_urls", set())
    for result in results:
        if len(sources) >= _MAX_WEB_SOURCES:
            return
        # Real stream-json may interleave status strings. Only structured
        # result objects with a list of structured content items are eligible.
        if not isinstance(result, dict):
            continue
        content = result.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if len(sources) >= _MAX_WEB_SOURCES:
                return
            if not isinstance(item, dict):
                continue
            title = _clean_web_source_title(item.get("title"))
            url = _clean_web_source_url(item.get("url"))
            if not title or not url or url in seen:
                continue
            seen.add(url)
            sources.append({"source_title": title, "source_url": url})


def _public_web_sources(state: dict) -> list[dict]:
    """Return a defensive, bounded copy of captured public source metadata."""
    raw = (state or {}).get("_web_sources") or []
    if not isinstance(raw, list):
        return []
    result = []
    seen = set()
    for item in raw:
        if len(result) >= _MAX_WEB_SOURCES:
            break
        if not isinstance(item, dict):
            continue
        title = _clean_web_source_title(item.get("source_title"))
        url = _clean_web_source_url(item.get("source_url"))
        if not title or not url or url in seen:
            continue
        seen.add(url)
        result.append({"source_title": title, "source_url": url})
    return result


def _step_of(ev, state, progress):
    """把 CLI 流事件翻译成人话步骤,回调 progress(kind, label)."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        progress("boot", "员工已上线,阅读任务简报…")
    elif t == "assistant":
        for blk in _event_blocks((ev.get("message") or {}).get("content")):
            if blk.get("type") != "tool_use":
                continue
            name = blk.get("name")
            if name == "WebSearch":
                # 只保留计数与 tool_use id。查询本身可能包含业务敏感信息，不能
                # 写进返回值/日志；id 只在本次进程内用于把 tool_result 归属到
                # WebSearch，进程结束后随 state 一起丢弃。
                usage = state.setdefault("tool_usage", {}).setdefault(
                    "WebSearch", {"attempts": 0, "success": 0, "errors": 0}
                )
                tool_id = blk.get("id") or blk.get("tool_use_id")
                if tool_id:
                    tool_id = str(tool_id)
                    known = state.setdefault("_websearch_ids", set())
                    completed = state.setdefault("_websearch_result_ids", set())
                    if tool_id in known or tool_id in completed:
                        continue
                    known.add(tool_id)
                else:
                    state["_websearch_anonymous_pending"] = (
                        int(state.get("_websearch_anonymous_pending") or 0) + 1
                    )
                usage["attempts"] += 1
                progress("search", f"联网检索中 · 已发起 {usage['attempts']} 次")
            elif name == "WebFetch":
                progress("fetch", "正在读取公开网页…")
            else:
                progress("tool", f"使用工具 {name}")
    elif t in {"user", "tool_result"}:
        # Claude stream-json 通常把工具返回包装在 user.message.content 中，
        # 不同 CLI 版本也可能直接发出 tool_result；两种形态都只统计
        # WebSearch 的成功/失败，不把 result content 写入 state。
        blocks = []
        if t == "tool_result":
            blocks = [ev]
        else:
            message = ev.get("message") or {}
            blocks = _event_blocks(message.get("content"))
        websearch_ids = state.get("_websearch_ids") or set()
        tool_result_blocks = [
            blk for blk in blocks
            if isinstance(blk, dict) and blk.get("type") == "tool_result"
        ]
        capture_eligible = 0
        for blk in tool_result_blocks:
            outer_tool_use_id = blk.get("tool_use_id")
            tool_id = outer_tool_use_id or blk.get("id")
            name = blk.get("name") or blk.get("tool_name")
            # An explicit foreign tool name always wins over any pending
            # anonymous search attempt. It must never be used to manufacture
            # WebSearch success.
            if name is not None and str(name) != "WebSearch":
                continue
            completed = state.setdefault("_websearch_result_ids", set())
            matched = False
            matched_known_id = False
            if tool_id:
                tool_id = str(tool_id)
                if tool_id in completed:
                    continue
                if websearch_ids:
                    if tool_id not in websearch_ids:
                        continue
                    websearch_ids.remove(tool_id)
                    # Source provenance is stricter than legacy aggregate
                    # accounting: only the real outer tool_use_id field can
                    # authorize top-level tool_use_result metadata.
                    matched_known_id = bool(outer_tool_use_id)
                else:
                    pending = int(state.get("_websearch_anonymous_pending") or 0)
                    # With no tool name and no known tool-use id, an id-bearing
                    # result is attributable only when exactly one anonymous
                    # WebSearch attempt is pending.
                    if name is None and pending != 1:
                        continue
                    if name == "WebSearch" and pending < 1:
                        continue
                    state["_websearch_anonymous_pending"] = pending - 1
                completed.add(tool_id)
                matched = True
            elif name == "WebSearch":
                pending = int(state.get("_websearch_anonymous_pending") or 0)
                if pending > 0:
                    state["_websearch_anonymous_pending"] = pending - 1
                    matched = True
                elif len(websearch_ids) == 1:
                    completed_id = next(iter(websearch_ids))
                    websearch_ids.remove(completed_id)
                    completed.add(completed_id)
                    matched = True
            if not matched:
                continue
            usage = state.setdefault("tool_usage", {}).setdefault(
                "WebSearch", {"attempts": 0, "success": 0, "errors": 0}
            )
            if _tool_result_is_error(blk):
                usage["errors"] += 1
                progress("error", f"联网检索失败 · 有效检索 {usage['success']}")
            else:
                usage["success"] += 1
                progress("search", f"联网检索完成 · 有效检索 {usage['success']}")
                if t == "user" and matched_known_id:
                    capture_eligible += 1
        # Top-level tool_use_result has no per-block owner. Capture only for
        # the unambiguous real shape: one outer result in this user event, its
        # explicit id correlated to WebSearch, and that result succeeded.
        if (t == "user" and len(tool_result_blocks) == 1
                and capture_eligible == 1):
            _capture_web_sources(ev, state)
    elif t == "stream_event":
        e = ev.get("event") or {}
        d = e.get("delta") or {}
        if e.get("type") == "content_block_delta" and d.get("type") == "text_delta":
            state["chars"] += len(d.get("text", ""))
            progress("typing", f"正在撰写产出…已写 {state['chars']} 字")


def _resolve_model(model: str):
    """历史兼容钩子;不再从本地设置替换模型或凭据。"""
    return model, {}


def _isolated_runner_env(
        provider_env: dict = None, extra_env: dict = None, *,
        cli_home: str) -> dict:
    """Build a minimal CLI environment with no local login/config inheritance."""
    env = {
        key: value for key, value in os.environ.items()
        if key in _PARENT_ENV_ALLOWLIST
    }
    env.update({
        "HOME": cli_home,
        "XDG_CONFIG_HOME": os.path.join(cli_home, "config"),
        "XDG_CACHE_HOME": os.path.join(cli_home, "cache"),
        "XDG_DATA_HOME": os.path.join(cli_home, "data"),
        "TMPDIR": os.path.join(cli_home, "tmp"),
        "CLAUDE_CONFIG_DIR": os.path.join(cli_home, "claude"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_SAFE_MODE": "1",
        "IS_SANDBOX": "1",
    })
    for supplied in (extra_env or {}, provider_env or {}):
        env.update({
            key: value for key, value in supplied.items()
            if key in _PROVIDER_ENV_ALLOWLIST and value is not None
        })
    if (provider_env or {}).get("ANTHROPIC_AUTH_TOKEN"):
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def _prepare_runner_dirs(runtime_root: str) -> tuple[str, str]:
    """Create a fresh per-call HOME/config and an empty non-project cwd."""
    runtime_root = os.path.abspath(runtime_root)
    os.chmod(runtime_root, 0o700)
    cli_home = os.path.join(runtime_root, "home")
    workdir = os.path.join(runtime_root, "work")
    for path in (
            cli_home,
            workdir,
            os.path.join(cli_home, "config"),
            os.path.join(cli_home, "cache"),
            os.path.join(cli_home, "data"),
            os.path.join(cli_home, "tmp"),
            os.path.join(cli_home, "claude")):
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    return cli_home, workdir


def _write_system_prompt(runtime_root: str, system_prompt: str = None):
    """Write private system context to a one-call 0600 file, never argv."""
    prompt = str(system_prompt or "").strip()
    if not prompt:
        return None
    path = os.path.join(os.path.abspath(runtime_root), "system-prompt.txt")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
            handle.flush()
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return path


def _runner_command(executable: str, *, model: str, web: bool,
                    system_prompt_file: str = None) -> list[str]:
    """Build a Claude CLI command that opts out of every disk customization."""
    command = [
        executable,
        "-p",
        "--safe-mode",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        model,
    ]
    if system_prompt_file:
        command += ["--system-prompt-file", os.path.abspath(system_prompt_file)]
    if web:
        command += [
            "--tools",
            "WebSearch",
            # `dontAsk` is the official non-interactive permission mode. The
            # explicit allowlist keeps the runner limited to WebSearch; never
            # use bypassPermissions, which would grant arbitrary tool access.
            "--allowedTools",
            "WebSearch",
            "--permission-mode",
            "dontAsk",
            "--max-budget-usd",
            "5",
        ]
    else:
        command += ["--tools", "", "--max-budget-usd", "2"]
    return command


def _public_tool_usage(state: dict) -> dict:
    """Return a non-sensitive WebSearch usage aggregate.

    The stream state also contains ephemeral tool ids used to correlate
    results.  Never expose those ids (or tool inputs/results); callers only get
    bounded integer counters suitable for progress/telemetry.
    """
    raw = (state or {}).get("tool_usage") or {}
    web = raw.get("WebSearch") if isinstance(raw, dict) else None
    if not isinstance(web, dict):
        return {}
    usage = {}
    for key in ("attempts", "success", "errors"):
        try:
            usage[key] = max(0, int(web.get(key) or 0))
        except (TypeError, ValueError):
            usage[key] = 0
    return {"WebSearch": usage}


def _require_successful_websearch(tool_usage: dict):
    """Fail closed when a web-enabled call produced no usable search result."""
    web = (tool_usage or {}).get("WebSearch")
    try:
        success = int(web.get("success") or 0) if isinstance(web, dict) else 0
    except (TypeError, ValueError):
        success = 0
    if success < 1:
        raise LLMError("联网检索未返回有效结果，任务已阻断")


async def call(prompt: str, model: str = DEFAULT_MODEL, web: bool = False,
               timeout: int = 600, progress=None, token: str = None,
               provider_env: dict = None, system_prompt: str = None,
               capture_web_sources: bool = False) -> dict:
    """调用 claude -p(流式),返回文本、计量与聚合工具使用数据。

    ``capture_web_sources`` 默认关闭。仅显式开启时，返回值才增加
    ``web_sources``，且来源只取自已与 WebSearch id 关联成功的结构化
    ``tool_use_result`` 元数据；不会解析模型文本或 tool_result.content。

    progress(kind, label) 可选回调:实时上报员工每一步动作(检索/阅读/撰写),
    供引擎广播到前端做工位步骤可视化。
    """
    if not provider_env or not provider_env.get("ANTHROPIC_BASE_URL") or not (
            provider_env.get("ANTHROPIC_AUTH_TOKEN") or provider_env.get("ANTHROPIC_API_KEY")):
        raise LLMError("工具执行器必须显式传入云雾 API 地址与凭据,禁止使用本地 Claude 登录态")
    if not CLAUDE or not os.path.isfile(CLAUDE) or not os.access(CLAUDE, os.X_OK):
        raise LLMError(
            "云雾能力网关执行器未安装或不可执行；"
            "请检查 CONTENTCREW_CLAUDE_PATH"
        )
    model, extra_env = _resolve_model(model)
    progress = progress or (lambda *a: None)
    result = {}
    state = {"chars": 0, "tool_usage": {}}
    if capture_web_sources and web:
        state["_capture_web_sources"] = True
    # 每次调用都使用全新的 HOME、配置目录和 cwd，并在进程退出后删除。即使旧版
    # runner-home 或项目祖先里残留 hooks、插件、登录态、CLAUDE.md，也没有加载入口。
    with tempfile.TemporaryDirectory(prefix="paihuo-llm-run-") as runtime_root:
        cli_home, workdir = _prepare_runner_dirs(runtime_root)
        # 私有岗位资料通过临时文件进入 CLI system 边界。文件只在
        # 本次 0700 runtime 内存活，权限为 0600；既不进 argv，也不与
        # stdin 里的不可信用户任务/公开检索 brief 混合。
        system_prompt_file = _write_system_prompt(runtime_root, system_prompt)
        cmd = _runner_command(
            CLAUDE,
            model=model,
            web=web,
            system_prompt_file=system_prompt_file,
        )
        env = _isolated_runner_env(
            provider_env,
            extra_env,
            cli_home=cli_home,
        )
        async with _sem:
            proc = None
            io_tasks = []
            err_buf = bytearray()

            async def _feed():
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()

            async def _drain_err():
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        return
                    if len(err_buf) < 8192:
                        err_buf.extend(chunk)

            async def _read():
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") == "result":
                        result.update(ev)
                    else:
                        try:
                            _step_of(ev, state, progress)
                        except Exception:
                            pass  # 步骤上报绝不影响主流程

            async def _terminate():
                """超时、取消或 I/O 异常时收掉 CLI 整个独立进程组。"""
                if proc is None:
                    return
                try:
                    _kill_process_tree(proc)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=workdir, env=env,
                    limit=1 << 24, start_new_session=True)
                if token:
                    RUNNING[token] = proc
                io_tasks = [
                    asyncio.create_task(_feed()),
                    asyncio.create_task(_read()),
                    asyncio.create_task(_drain_err()),
                ]
                await asyncio.wait_for(
                    asyncio.gather(*io_tasks), timeout)
                exit_code = await asyncio.wait_for(proc.wait(), 10)
                if exit_code != 0 or getattr(proc, "returncode", exit_code) != 0:
                    raise LLMError(_STABLE_RUNNER_ERROR)
            except asyncio.TimeoutError:
                await _terminate()
                raise LLMError(f"LLM 调用超时(>{timeout}s)")
            except asyncio.CancelledError:
                await _terminate()
                raise
            except Exception:
                await _terminate()
                raise LLMError(_STABLE_RUNNER_ERROR) from None
            finally:
                if token:
                    RUNNING.pop(token, None)
                for task in io_tasks:
                    if not task.done():
                        task.cancel()
                if io_tasks:
                    await asyncio.gather(*io_tasks, return_exceptions=True)
    tool_usage = _public_tool_usage(state)
    if web:
        # A text result without an actual WebSearch tool_result is not a valid
        # network-backed answer.  Enforce this before accepting the result,
        # including when the CLI exits cleanly but skipped the tool.
        _require_successful_websearch(tool_usage)
    if not result:
        # stderr 属于不可信供应商/CLI 输出；它可能回显 system prompt、请求体或
        # 凭据，绝不能进入异常文本、数据库、SSE 或 API 响应。
        raise LLMError(_STABLE_RUNNER_ERROR)
    if result.get("is_error"):
        # result.error 同样可能包含上游回显，失败面只暴露稳定文案。
        raise LLMError(_STABLE_RUNNER_ERROR)
    usage = result.get("usage") or {}
    tokens = (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
              + usage.get("cache_read_input_tokens", 0))
    response = {
        "text": result.get("result") or "",
        "cost_usd": result.get("total_cost_usd") or 0.0,
        "tokens": tokens,
        "tool_usage": tool_usage,
    }
    if capture_web_sources:
        response["web_sources"] = _public_web_sources(state)
    return response


def extract_json(text: str):
    """从模型输出里抠出第一个完整的顶层 JSON 对象.

    容忍 ```json 围栏、前后废话、字符串值里的裸换行/控制字符(strict=False)。
    顶层候选解析失败时跳过整个花括号块,绝不回退到其内部的嵌套子对象——
    否则会把 image_plan 之类的小对象当成工位产出,静默丢掉正文。
    """
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1), strict=False)
        except ValueError:
            text = m.group(1)
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        end = None
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if in_str:
                if c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            break  # 花括号未配平(输出被截断),没有完整候选了
        try:
            return json.loads(text[start:end + 1], strict=False)
        except ValueError:
            start = text.find("{", end + 1)
    raise LLMError("模型输出中未找到合法 JSON")


async def call_json(prompt: str, model: str = DEFAULT_MODEL, web: bool = False,
                    timeout: int = 600, retries: int = 1, progress=None, token: str = None,
                    system_prompt: str = None) -> dict:
    """调用并解析 JSON;解析失败自动带错误重问一次."""
    last = None
    p = prompt
    for i in range(retries + 1):
        if i and progress:
            progress("retry", "上一次输出不是合法 JSON,要求员工重写…")
        r = await call(
            p, model=model, web=web, timeout=timeout, progress=progress, token=token,
            system_prompt=system_prompt,
        )
        try:
            data = extract_json(r["text"])
            return {
                "data": data,
                "cost_usd": r["cost_usd"],
                "tokens": r["tokens"],
                "tool_usage": r.get("tool_usage") or {},
            }
        except LLMError as e:
            last = e
            p = prompt + "\n\n⚠️ 你上一次的输出无法解析为 JSON,这次只输出一个合法 JSON 对象,不要任何其他文字。"
    raise last
