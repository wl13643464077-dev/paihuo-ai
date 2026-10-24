"""口播稿/正文「AI 味」检测：本地 AI 腔体检 + 腾讯朱雀 AI 检测官方 API（选配）。

- 本地体检免费、随时可用：查排比三连、书面腔连接词、句长过于均匀、英文缩写过多。
- 朱雀走腾讯云 EdgeOne 官方 API（管理后台配置 zhuque_key；每月有免费额度）。
  只用官方 API，不自动化网页版、不绕验证码。单次请求最多 1000 字符，长文按段切。

结论只是外部信号：分数低不是目标，真人经历和判断才是（见流水线 b1 去AI味研究）。
"""
from __future__ import annotations

import re
import statistics

import httpx

ZHUQUE_URL = "https://ai-gateway.edgeone.link/v1/providers/zhuque-text/classify"
SEGMENT_CHARS = 1000
MAX_CHARS = 6000
MIN_ZHUQUE_CHARS = 100

BOOKISH = ("首先", "其次", "最后", "综上所述", "总而言之", "值得注意的是", "不难发现",
           "众所周知", "在当今", "随着.{1,8}的发展", "不仅.{1,12}而且", "此外", "与此同时",
           "毋庸置疑", "赋能", "抓手", "闭环", "底层逻辑", "颗粒度")


class ZhuqueError(RuntimeError):
    pass


def local_lint(text: str) -> dict:
    """免费的本地 AI 腔体检，返回问题清单和 0-100 的「AI 腔指数」（越高越像模板文）。"""
    plain = re.sub(r"\s+", "", str(text or ""))
    issues = []
    hits = []
    for pattern in BOOKISH:
        found = re.findall(pattern, plain)
        if found:
            hits.append(f"{found[0]}×{len(found)}" if len(found) > 1 else found[0])
    if hits:
        issues.append("书面腔/套话：" + "、".join(hits[:8]))
    sentences = [s for s in re.split(r"[。！？!?；;\n]", str(text or "")) if s.strip()]
    lengths = [len(re.sub(r"\s", "", s)) for s in sentences]
    uniform = False
    if len(lengths) >= 5:
        mean = statistics.mean(lengths)
        spread = statistics.pstdev(lengths) / mean if mean else 0
        if spread < 0.28:
            uniform = True
            issues.append("句子长短太均匀，读起来像模板：长短句交错一下")
    parallel = 0
    for i in range(len(sentences) - 2):
        heads = {re.sub(r"\s", "", s)[:2] for s in sentences[i:i + 3]}
        if len(heads) == 1 and all(len(s.strip()) > 4 for s in sentences[i:i + 3]):
            parallel += 1
    if parallel:
        issues.append(f"排比三连 {parallel} 处：同一开头连用三句，删一句或打散")
    acronyms = re.findall(r"\b[A-Z]{2,6}\b", str(text or ""))
    if len(acronyms) > 3:
        issues.append(f"英文缩写 {len(acronyms)} 个，口播建议 ≤3 个")
    numbers = [s for s in sentences if len(re.findall(r"\d+(?:\.\d+)?", s)) >= 3]
    if numbers:
        issues.append(f"{len(numbers)} 句里塞了 3 个以上数字：一句只放一个主数字")
    score = min(100, len(hits) * 12 + parallel * 15 + (20 if uniform else 0)
                + max(0, len(acronyms) - 3) * 5 + len(numbers) * 8)
    level = "低" if score < 25 else "中" if score < 55 else "高"
    return {"score": score, "level": level, "issues": issues, "chars": len(plain)}


def _segments(text: str) -> list[str]:
    plain = str(text or "").strip()[:MAX_CHARS]
    out, current = [], ""
    for para in re.split(r"(?<=[。！？!?\n])", plain):
        if len(current) + len(para) > SEGMENT_CHARS and current:
            out.append(current)
            current = ""
        while len(para) > SEGMENT_CHARS:
            out.append(para[:SEGMENT_CHARS])
            para = para[SEGMENT_CHARS:]
        current += para
    if current.strip():
        out.append(current)
    return out


def _find_ratio(payload):
    """官方返回字段 labels_ratio: {"0": 人工, "1": AI, "2": 疑似AI}；兼容外层包裹。"""
    if isinstance(payload, dict):
        ratio = payload.get("labels_ratio")
        if isinstance(ratio, dict):
            return ratio
        for value in payload.values():
            found = _find_ratio(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_ratio(value)
            if found:
                return found
    return None


async def zhuque_classify(text: str, api_key: str, *, client=None, timeout: float = 30) -> dict:
    """调用朱雀官方 API，按段长度加权汇总人工/AI/疑似占比。"""
    if not api_key:
        raise ZhuqueError("未配置朱雀 API Key")
    segments = _segments(text)
    if sum(len(s) for s in segments) < MIN_ZHUQUE_CHARS:
        raise ZhuqueError(f"文字太短（少于 {MIN_ZHUQUE_CHARS} 字），朱雀检测不准")
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)
    totals = {"human": 0.0, "ai": 0.0, "suspect": 0.0}
    weight = 0
    try:
        for segment in segments:
            try:
                response = await client.post(
                    ZHUQUE_URL,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"text": segment, "is_merge": False},
                )
            except httpx.HTTPError as exc:
                raise ZhuqueError("朱雀服务暂时连不上，请稍后再试") from exc
            if response.status_code in (401, 403):
                raise ZhuqueError("朱雀 API Key 无效或没有权限")
            if response.status_code == 429:
                raise ZhuqueError("朱雀免费额度或频率已用尽，请稍后再试")
            if response.status_code >= 400:
                raise ZhuqueError(f"朱雀检测失败（HTTP {response.status_code}）")
            try:
                ratio = _find_ratio(response.json())
            except ValueError as exc:
                raise ZhuqueError("朱雀返回了无法识别的结果") from exc
            if not ratio:
                raise ZhuqueError("朱雀返回了无法识别的结果")
            size = len(segment)
            for key, name in (("0", "human"), ("1", "ai"), ("2", "suspect")):
                try:
                    totals[name] += float(ratio.get(key) or 0) * size
                except (TypeError, ValueError):
                    pass
            weight += size
    finally:
        if owns:
            await client.aclose()
    result = {name: round(value / weight, 3) for name, value in totals.items()}
    ai_like = result["ai"] + result["suspect"]
    result["verdict"] = ("不太可能是AI生成" if ai_like < 0.3
                         else "疑似AI辅助" if ai_like < 0.6 else "易被判为AI生成")
    result["segments"] = len(segments)
    return result
