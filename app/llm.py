"""云雾 Claude 工具执行器:通过 claude CLI 无头运行 WebSearch.

业务路由在 app/providers.py。本模块只接受调用方显式传入的云雾兼容
base URL/token,绝不读取本地 Claude 登录态或历史 Anthropic Key 设置。
网页正文由应用自己的逐跳 SSRF 防护网关读取，不把任意 URL 访问权交给 CLI。
"""
import asyncio
import json
import os
import re
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
            try:
                proc.kill()
                n += 1
            except ProcessLookupError:
                pass
    return n


class LLMError(Exception):
    pass


def _step_of(ev, state, progress):
    """把 CLI 流事件翻译成人话步骤,回调 progress(kind, label)."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        progress("boot", "员工已上线,阅读任务简报…")
    elif t == "assistant":
        for blk in (ev.get("message") or {}).get("content") or []:
            if blk.get("type") != "tool_use":
                continue
            name, inp = blk.get("name"), blk.get("input") or {}
            if name == "WebSearch":
                progress("search", f"联网检索:{inp.get('query', '…')}")
            elif name == "WebFetch":
                progress("fetch", f"阅读网页:{inp.get('url', '…')}")
            else:
                progress("tool", f"使用工具 {name}")
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


def _runner_command(executable: str, *, model: str, web: bool,
                    system_prompt: str = None) -> list[str]:
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
    if system_prompt and system_prompt.strip():
        command += ["--system-prompt", system_prompt.strip()]
    if web:
        command += [
            "--tools",
            "WebSearch",
            "--max-budget-usd",
            "5",
        ]
    else:
        command += ["--tools", "", "--max-budget-usd", "2"]
    return command


async def call(prompt: str, model: str = DEFAULT_MODEL, web: bool = False,
               timeout: int = 600, progress=None, token: str = None,
               provider_env: dict = None, system_prompt: str = None) -> dict:
    """调用 claude -p(流式),返回 {text, cost_usd, tokens}.

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
    # 私有岗位资料必须走 CLI 明确提供的 system 边界；stdin 永远只承载不可信
    # 的用户任务/公开检索 brief，不能再把两者拼成一段 prompt。
    cmd = _runner_command(
        CLAUDE,
        model=model,
        web=web,
        system_prompt=system_prompt,
    )
    progress = progress or (lambda *a: None)
    result = {}
    state = {"chars": 0}
    # 每次调用都使用全新的 HOME、配置目录和 cwd，并在进程退出后删除。即使旧版
    # runner-home 或项目祖先里残留 hooks、插件、登录态、CLAUDE.md，也没有加载入口。
    with tempfile.TemporaryDirectory(prefix="paihuo-llm-run-") as runtime_root:
        cli_home, workdir = _prepare_runner_dirs(runtime_root)
        env = _isolated_runner_env(
            provider_env,
            extra_env,
            cli_home=cli_home,
        )
        async with _sem:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=workdir, env=env,
                limit=1 << 24)
            if token:
                RUNNING[token] = proc
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
                """超时或上层取消时收掉 CLI，避免任务已失败但模型进程仍在后台跑。"""
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass

            try:
                await asyncio.wait_for(
                    asyncio.gather(_feed(), _read(), _drain_err()), timeout)
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                await _terminate()
                raise LLMError(f"LLM 调用超时(>{timeout}s)")
            except asyncio.CancelledError:
                await _terminate()
                raise
            finally:
                if token:
                    RUNNING.pop(token, None)
    if not result:
        # stderr 属于不可信供应商/CLI 输出；它可能回显 system prompt、请求体或
        # 凭据，绝不能进入异常文本、数据库、SSE 或 API 响应。
        raise LLMError("云雾能力网关执行失败，请稍后重试")
    if result.get("is_error"):
        # result.error 同样可能包含上游回显，失败面只暴露稳定文案。
        raise LLMError("云雾能力网关执行失败，请稍后重试")
    usage = result.get("usage") or {}
    tokens = (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
              + usage.get("cache_read_input_tokens", 0))
    return {"text": result.get("result") or "", "cost_usd": result.get("total_cost_usd") or 0.0,
            "tokens": tokens}


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
            return {"data": data, "cost_usd": r["cost_usd"], "tokens": r["tokens"]}
        except LLMError as e:
            last = e
            p = prompt + "\n\n⚠️ 你上一次的输出无法解析为 JSON,这次只输出一个合法 JSON 对象,不要任何其他文字。"
    raise last
