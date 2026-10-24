"""微型知识库：把老板上传的长资料切段，按当前任务只召回最相关的几段。

旧做法每条知识只取前 800 字，上传一份 1 万字的 SOP，员工永远只看得到开头。
这里移植纳米Work企业版「知识库」的做法（600 字一段、80 字重叠），但不依赖
向量模型：用中文二元词 + BM25 在本地打分，零调用成本、零外部依赖，结果可解释。

纪律：
- 只处理调用方已按租户、未删除过滤好的行；本模块不碰数据库。
- 每份资料最多贡献 2 段，避免一份长文挤掉其他资料。
- 置顶资料至少带上最相关的一段（没有关键词命中就带开头一段）。
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter, OrderedDict

CHUNK_SIZE = 600
CHUNK_OVERLAP = 80
MAX_CHUNKS_PER_DOC = 60
MAX_CHUNKS_PER_HIT_DOC = 2
DEFAULT_TOP_DOCS = 12
DEFAULT_BUDGET_CHARS = 7000

_BM25_K1 = 1.2
_BM25_B = 0.75

_SENTENCE_END = "。！？!?；;\n"
_cache_lock = threading.Lock()
_CACHE_MAX = 4000
_chunk_cache: "OrderedDict[tuple, list[dict]]" = OrderedDict()


def search_terms(value: str) -> set[str]:
    """轻量中文/英文检索词：英文整词 + 中文连续片段 + 中文二元词。"""
    normalized = re.sub(r"\s+", " ", str(value or "").lower())
    terms = set(re.findall(r"[a-z0-9][a-z0-9_-]{1,40}", normalized))
    for chunk in re.findall(r"[一-鿿]{2,}", normalized):
        if len(chunk) <= 8:
            terms.add(chunk)
        terms.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return {term for term in terms if len(term) >= 2}


def _term_counts(value: str) -> Counter:
    normalized = re.sub(r"\s+", " ", str(value or "").lower())
    counts: Counter = Counter(re.findall(r"[a-z0-9][a-z0-9_-]{1,40}", normalized))
    for chunk in re.findall(r"[一-鿿]{2,}", normalized):
        counts.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return counts


def chunk_text(body: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP,
               max_chunks: int = MAX_CHUNKS_PER_DOC) -> list[str]:
    """按句子边界切段：每段不超过 size 字，相邻段重叠 overlap 字保住上下文。"""
    text = re.sub(r"[ \t\r\f\v]+", " ", str(body or ""))
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    size = max(80, int(size))
    overlap = max(0, min(int(overlap), size // 2))
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = min(len(text), start + size)
        if end < len(text):
            # 回退到最近的句末，避免把一句话劈成两半；找不到就硬切。
            window = text[start + size // 2:end]
            cut = max(window.rfind(mark) for mark in _SENTENCE_END)
            if cut >= 0:
                end = start + size // 2 + cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _doc_chunks(row: dict) -> list[dict]:
    """切段并预先统计词频；按 (id, 内容长度, 内容哈希) 缓存，资料改动后自动失效。"""
    content = str(row.get("content") or "")
    key = (row.get("id"), len(content), hash(content))
    with _cache_lock:
        cached = _chunk_cache.get(key)
        if cached is not None:
            _chunk_cache.move_to_end(key)
            return cached
    pieces = chunk_text(content)
    result = []
    for index, piece in enumerate(pieces):
        counts = _term_counts(piece)
        result.append({
            "index": index,
            "text": piece,
            "counts": counts,
            "length": max(1, sum(counts.values())),
        })
    with _cache_lock:
        _chunk_cache[key] = result
        while len(_chunk_cache) > _CACHE_MAX:
            _chunk_cache.popitem(last=False)
    return result


def retrieve(rows: list[dict], query: str, *, tags_of=None,
             top_docs: int = DEFAULT_TOP_DOCS,
             budget_chars: int = DEFAULT_BUDGET_CHARS) -> dict:
    """在已过滤的知识行里召回最相关的段落。

    返回 ``{"hits": [{"id","title","chunk_index","chunk_total","text","score"}],
    "docs": 命中的资料数, "total": 资料总数}``。``tags_of(row)`` 可返回该行标签文本。
    """
    rows = [row for row in (rows or []) if row]
    if not rows:
        return {"hits": [], "docs": 0, "total": 0}
    query_terms = search_terms(query)
    query_counts = Counter({term: 1 for term in query_terms if len(term) == 2
                            or not re.match(r"[一-鿿]", term)})

    corpus = []
    for position, row in enumerate(rows):
        chunks = _doc_chunks(row)
        title = str(row.get("title") or "")
        tags = str(tags_of(row) if tags_of else "")
        corpus.append((position, row, title, tags, chunks))

    all_chunks = [chunk for *_rest, chunks in corpus for chunk in chunks]
    n_chunks = max(1, len(all_chunks))
    avg_len = sum(chunk["length"] for chunk in all_chunks) / n_chunks if all_chunks else 1.0
    doc_freq: Counter = Counter()
    if query_counts:
        for chunk in all_chunks:
            for term in query_counts:
                if term in chunk["counts"]:
                    doc_freq[term] += 1

    def bm25(chunk: dict) -> float:
        score = 0.0
        for term in query_counts:
            tf = chunk["counts"].get(term, 0)
            if not tf:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1 + (n_chunks - df + 0.5) / (df + 0.5))
            norm = tf * (_BM25_K1 + 1) / (
                tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * chunk["length"] / avg_len)
            )
            score += idf * norm
        return score

    doc_hits = []
    for position, row, title, tags, chunks in corpus:
        title_bonus = len(query_terms & search_terms(title)) * 1.5
        tag_bonus = len(query_terms & search_terms(tags)) * 0.8
        scored = sorted(
            ((bm25(chunk) + title_bonus + tag_bonus, chunk) for chunk in chunks),
            key=lambda item: (item[0], -item[1]["index"]),
            reverse=True,
        )
        pinned = bool(row.get("pinned"))
        if not scored:
            if pinned or title_bonus:
                scored = [(title_bonus, {"index": 0, "text": "", "counts": {}, "length": 1})]
            else:
                continue
        best = scored[0][0]
        relevant = [item for item in scored if item[0] > 0.6][:MAX_CHUNKS_PER_HIT_DOC]
        if not relevant and pinned:
            # 置顶资料没有命中词：带开头一段，保证老板置顶的红线永远在场。
            relevant = [min(scored, key=lambda item: item[1]["index"])]
        if not relevant:
            continue
        rank = best + (100 if pinned else 0)
        doc_hits.append((rank, int(pinned), -position, row, chunks, relevant))

    if not doc_hits and not query_counts:
        # 没有任何检索词（例如空任务）：最近三份资料各给开头一段兜底。
        for position, row, _title, _tags, chunks in corpus[:3]:
            if chunks:
                doc_hits.append((0.0, 0, -position, row, chunks, [(0.0, chunks[0])]))

    doc_hits.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    hits, used, docs = [], 0, 0
    for _rank, _pinned, _pos, row, chunks, relevant in doc_hits[:top_docs]:
        added = False
        for score, chunk in sorted(relevant, key=lambda item: item[1]["index"]):
            text = re.sub(r"\s+", " ", chunk["text"]).strip()
            if not text and not added:
                text = ""
            cost = len(text) + len(str(row.get("title") or "")) + 12
            if used + cost > budget_chars and hits:
                break
            hits.append({
                "id": row.get("id"),
                "title": str(row.get("title") or ""),
                "chunk_index": chunk["index"],
                "chunk_total": len(chunks),
                "text": text,
                "score": round(float(score), 3),
            })
            used += cost
            added = True
        if added:
            docs += 1
        if used >= budget_chars:
            break
    return {"hits": hits, "docs": docs, "total": len(rows)}


def format_hits(hits: list[dict]) -> list[str]:
    """把召回段落渲染成提示词行；长资料标出是第几段，方便员工在交付里引用出处。"""
    lines = []
    for hit in hits:
        where = (f"（第{hit['chunk_index'] + 1}/{hit['chunk_total']}段）"
                 if hit.get("chunk_total", 1) > 1 else "")
        body = hit.get("text") or "（见标题）"
        lines.append(f"- 《{hit['title']}》{where}:{body}")
    return lines
