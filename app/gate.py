"""质检关卡(Gate):挂在工位9(分发官)之前的引擎内置关卡.

PRD 5.1:敏感词、平台规则、事实抽检不通过则阻断发布并强制人工处理。
不可变 release 提供 config/gate_rules.default.json seed；运行时
data/gate_rules.json 优先且可热更新，缺失时从 seed 安全初始化。
"""
import json
import logging
import os
import stat
import threading
import time
import uuid

from . import llm, providers

log = logging.getLogger("gate")

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SEED_RULES_PATH = os.path.join(_ROOT, "config", "gate_rules.default.json")
STATE_RULES_PATH = os.path.join(_ROOT, "data", "gate_rules.json")
MANIFEST_PATH = os.path.join(_ROOT, "RELEASE-MANIFEST.json")
RULES_PATH = STATE_RULES_PATH
_MAX_RULES_BYTES = 1 * 1024 * 1024
_RULES_INIT_LOCK = threading.Lock()

DEFAULT_RULES = {
    "sensitive_words": [
        "全网最低价", "史上最全", "绝对有效", "包治", "根治", "秒杀一切", "第一名",
        "国家级", "最高级", "100%有效", "无副作用", "稳赚", "保本", "躺赚",
    ],
    "notes": "敏感词命中为中风险;平台规则与事实抽检由 LLM 质检承担。可直接编辑本文件热更新。",
}


def _validate_rules(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("gate rules must be a JSON object")
    words = value.get("sensitive_words")
    if (
        not isinstance(words, list)
        or any(not isinstance(word, str) or not word.strip() for word in words)
    ):
        raise ValueError("gate sensitive_words must be a list of non-empty strings")
    notes = value.get("notes", "")
    if not isinstance(notes, str):
        raise ValueError("gate notes must be a string")
    return value


def _read_rules(path: str) -> dict:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("gate rules file must be a regular file")
    if metadata.st_size > _MAX_RULES_BYTES:
        raise ValueError("gate rules file exceeds size limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        actual = os.fstat(descriptor)
        if (
            actual.st_dev != metadata.st_dev
            or actual.st_ino != metadata.st_ino
            or not stat.S_ISREG(actual.st_mode)
            or actual.st_size > _MAX_RULES_BYTES
        ):
            raise ValueError("gate rules file changed during open")
        chunks = []
        remaining = _MAX_RULES_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0 and os.read(descriptor, 1):
            raise ValueError("gate rules file exceeds size limit")
    finally:
        os.close(descriptor)
    return _validate_rules(json.loads(b"".join(chunks).decode("utf-8")))


def _write_all(descriptor: int, body: bytes) -> None:
    offset = 0
    while offset < len(body):
        written = os.write(descriptor, body[offset:])
        if written <= 0:
            raise OSError("short write while initializing gate rules")
        offset += written


def _initial_rules() -> dict:
    if os.path.exists(SEED_RULES_PATH):
        return _read_rules(SEED_RULES_PATH)
    if os.path.lexists(MANIFEST_PATH):
        raise ValueError(
            "formal release is missing immutable gate rules seed"
        )
    # Source checkouts have no generated config directory until release build.
    return _validate_rules(json.loads(json.dumps(DEFAULT_RULES)))


def _initialize_state_rules() -> None:
    with _RULES_INIT_LOCK:
        if os.path.lexists(STATE_RULES_PATH):
            return
        rules = _initial_rules()
        parent = os.path.dirname(STATE_RULES_PATH)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        body = (
            json.dumps(
                rules,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        temporary = os.path.join(
            parent,
            f".gate_rules.init-{uuid.uuid4().hex}",
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, body)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            try:
                os.link(
                    temporary,
                    STATE_RULES_PATH,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            directory_descriptor = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _rules():
    if not os.path.lexists(STATE_RULES_PATH):
        _initialize_state_rules()
    return _read_rules(STATE_RULES_PATH)


async def check(title: str, body: str, platforms: list,
                research: dict = None, progress=None, token: str = None) -> dict:
    progress = progress or (lambda *a: None)
    issues = []
    if not (body or "").strip():
        progress("error", "正文为空,直接阻断")
        issues.append({"type": "内容缺失", "severity": "高",
                       "detail": "正文为空——上游初稿未产出或产出丢失,不能发布空内容"})
        return {"passed": False, "issues": issues, "cost_usd": 0.0, "tokens": 0,
                "report": "⛔ 正文为空,已阻断发布"}
    progress("gate", "第 1 步:审查官词库敏感词扫描…")
    rules = _rules()
    text = f"{title}\n{body}"
    # V24:接入审查官大词库(广告法/医疗金融/导流/违禁),gate_rules.json 里的自定义词继续生效
    from . import censor
    lex_hits = censor.scan(text)
    for h in lex_hits:
        issues.append({"type": h["type"], "severity": h["severity"],
                       "detail": h["detail"] + f";建议:{h.get('suggest', '')}"})
    hits = len(lex_hits)
    for w in rules.get("sensitive_words", []):
        if w in text and not any(i.get("word") == w for i in lex_hits):
            hits += 1
            issues.append({"type": "敏感词", "severity": "中",
                           "detail": f"命中规则库敏感词「{w}」(广告法/平台风控风险)"})
    progress("gate", f"审查官词库扫描完成:命中 {hits} 条")
    cost, tokens = 0.0, 0
    # 事实基准:上游情报员联网检索到的素材(质检员自己的知识可能过时,不能当事实标准)
    ref = ""
    if research:
        facts = (research.get("facts") or []) + (research.get("data_points") or [])
        srcs = [s.get("title") or s.get("url") or "" for s in research.get("sources") or []]
        if facts or srcs:
            ref = ("\n【情报员联网检索到的事实基准(判断事实问题以此为准)】\n"
                   + "\n".join(f"- {f}" for f in facts[:20])
                   + ("\n来源:" + ";".join(srcs[:10]) if srcs else ""))
    progress("gate", "第 2 步:AI 质检(广告法/平台规则/事实抽检)…")
    gate_error = False
    try:
        r = await providers.call_text_json(8, f"""你是内容质检员,今天是 {time.strftime('%Y-%m-%d')}。检查以下待发布内容,目标平台:{'、'.join(platforms)}。
检查维度:①违反广告法的绝对化/夸大用语 ②平台高危内容(医疗/金融承诺、导流、违禁品)
③事实错误:与下方"事实基准"矛盾、或正文内部自相矛盾的内容才算;你的训练知识可能已过时,
**严禁**仅因为你不认识某个新产品/新模型/新事件就判为"虚构/不存在" ④诱导互动违规话术。
宁缺勿滥,只报真问题。
{ref}
标题:{title}
正文:
{body[:4000]}

只输出 JSON:{{"issues":[{{"type":"类别","severity":"高/中/低","detail":"具体位置与问题"}}]}},没有问题输出 {{"issues":[]}}""",
                                timeout=180, progress=progress, token=token)
        issues += r["data"].get("issues", [])
        cost, tokens = r["cost_usd"], r["tokens"]
    except (llm.LLMError, providers.ProviderError) as e:
        # fail-closed:发布前质检是对客户的合规承诺,供应商故障时绝不能"仅凭词库放行"。
        # 标记为高危拦下,老板可在工单上重检或知情放行。
        log.warning("gate ai check failed error_type=%s", type(e).__name__)
        gate_error = True
        issues.append({
            "type": "质检未完成",
            "severity": "高",
            "detail": "AI 深度质检未能完成(模型服务暂时不可用),仅完成规则库检查。"
                      "为避免未经审查的内容发出,已暂停发布;请稍后重检,或确认无误后手动放行。",
        })
    passed = not any(i.get("severity") == "高" for i in issues)
    if passed:
        report = "✅ 质检通过" + (f",共 {len(issues)} 条提示" if issues else ",未发现问题")
    elif gate_error:
        report = f"⏸ 质检未完成,已暂停发布(共 {len(issues)} 条提示),可重检或手动放行"
    else:
        report = f"⛔ 存在高风险问题,已阻断发布,共 {len(issues)} 条提示"
    progress("done" if passed else "error", report)
    return {"passed": passed, "issues": issues, "cost_usd": cost, "tokens": tokens,
            "report": report}
