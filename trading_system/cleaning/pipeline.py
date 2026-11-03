"""数据清洗与处理管线 — 严格执行用户指令二·2：

  · 规则引擎【只允许】：基础格式校验（空文档/超长截断/日期规整）、
    去重（doc_id 哈希 + 近似去重的归一化指纹）——确定性操作；
  · 其余一切（语义歧义消解、情感分析、模糊实体消歧、事件抽取、关联推断）
    【一律由 LLM 完成】——本模块不存在任何关键词打分/正则情感等规则语义路径。

红线 2 落点：clean_llm_semantic 环节通过 llm_guard 执行；
LLM 不可用时透传"规则清洗后的文档"（degraded 标注），绝不用规则补算语义。
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata

from ..llm.client import LLMClient
from ..llm.prompts import CLEAN_SCHEMA, CLEAN_USER_TMPL, SYSTEM_ANALYST
from ..redline import ExecutionTracer, Passthrough, llm_guard
from ..search.credibility import evidence_for
from ..search.models import CleanDocument, RawDocument

log = logging.getLogger("cleaning")

MAX_CONTENT = 4000          # 格式校验：超长截断
MAX_DOCS_PER_LLM_CALL = 12  # 单次 LLM 调用的文档批量


# ---------------------------------------------------------------- 规则层
def _normalize_text(t: str) -> str:
    """格式校验：Unicode 归一 + 空白规整。确定性操作。"""
    t = unicodedata.normalize("NFKC", t or "")
    return re.sub(r"\s+", " ", t).strip()


def _fingerprint(doc: RawDocument) -> str:
    """近似去重指纹：标题+正文前 200 字符的归一化哈希。确定性操作。"""
    import hashlib
    base = _normalize_text(doc.title)[:120] + "|" + _normalize_text(doc.content)[:200]
    return hashlib.sha1(base.lower().encode("utf-8", "ignore")).hexdigest()[:16]


def rule_base_clean(docs: list[RawDocument], *, preserve_source: bool = False) -> list[RawDocument]:
    """clean.rule_base 环节：格式校验 + 去重。仅此两项，规则不再多做一分。"""
    out: list[RawDocument] = []
    seen_id: set[str] = set()
    seen_fp: set[str] = set()
    for d in docs:
        if preserve_source:
            # The native provider adapter already sanitized these values. Dedup
            # over the whole body, so a common prefix cannot hide distinct evidence.
            content, title = d.content, d.title
            fp = hashlib.sha256(f"{d.url}\0{title}\0{content}".encode("utf-8")).hexdigest()
            invalid_content = not content.strip()
        else:
            content = _normalize_text(d.content)[:MAX_CONTENT]
            title = _normalize_text(d.title)[:200]
            fp = _fingerprint(d)
            invalid_content = len(content) < 20
        if invalid_content:
            continue
        if d.doc_id in seen_id or fp in seen_fp:  # 去重
            continue
        seen_id.add(d.doc_id)
        seen_fp.add(fp)
        out.append(d if preserve_source else RawDocument(
            doc_id=d.doc_id, source=d.source, title=title, url=d.url,
            content=content, published=d.published, fetched_at=d.fetched_at, meta=d.meta))
    return out


# ---------------------------------------------------------------- LLM 层
def _docs_payload(docs: list[RawDocument]) -> str:
    parts = []
    for d in docs:
        parts.append(f"<doc id=\"{d.doc_id}\" source=\"{d.source}\" "
                     f"published=\"{d.published}\">\n{d.content[:1200]}\n</doc>")
    return "\n\n".join(parts)


def _apply_llm_annotations(docs: list[RawDocument],
                           llm_out: dict) -> list[CleanDocument]:
    by_id = {str(x.get("id")): x for x in llm_out.get("documents", []) if isinstance(x, dict)}
    cleaned: list[CleanDocument] = []
    for d in docs:
        ann = by_id.get(d.doc_id)
        if not ann:
            cleaned.append(_with_evidence(CleanDocument(raw=d, degraded=True)))
            continue
        cleaned.append(_with_evidence(CleanDocument(
            raw=d,
            tickers=[str(t).upper() for t in (ann.get("tickers") or [])][:8],
            sentiment=ann.get("sentiment"),
            sentiment_score=_safe_float(ann.get("sentiment_score")),
            events=[str(e) for e in (ann.get("events") or [])][:6],
            summary_zh=ann.get("summary_zh"),
            relevance=_safe_float(ann.get("relevance")),
            degraded=False,
        )))
    return cleaned


def _with_evidence(doc: CleanDocument) -> CleanDocument:
    """S3 数据层：清洗管线输出的每条文档必须携带证据元数据
    （tier/Point-in-Time 时间戳/content_hash/fetched_at，规则映射非语义）。"""
    doc.evidence = evidence_for(doc.raw)
    return doc


def _safe_float(v) -> float | None:
    try:
        return max(-1.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


def llm_semantic_clean(docs: list[RawDocument], llm: LLMClient,
                       tracer: ExecutionTracer | None = None
                       ) -> list[CleanDocument] | Passthrough:
    """clean.llm_semantic 环节：实体消歧/情感/事件/关联 —— 全部 LLM。

    返回 CleanDocument 列表；LLM 不可用时 llm_guard 返回 Passthrough
    （payload = 规则清洗后的文档列表），调用方 unwrap 并保留 degraded 语义。
    """
    def _run() -> list[CleanDocument]:
        cleaned_all: list[CleanDocument] = []
        for i in range(0, len(docs), MAX_DOCS_PER_LLM_CALL):
            batch = docs[i:i + MAX_DOCS_PER_LLM_CALL]
            out = llm.complete_json(
                system=SYSTEM_ANALYST,
                user=CLEAN_USER_TMPL.format(n=len(batch), docs=_docs_payload(batch)),
                schema_hint=CLEAN_SCHEMA)
            cleaned_all.extend(_apply_llm_annotations(batch, out))
        return cleaned_all

    return llm_guard("clean.llm_semantic", _run, fallback_payload=docs, tracer=tracer)


def unwrap_cleaned(result: list[CleanDocument] | Passthrough) -> list[CleanDocument]:
    """统一出口：无论 LLM 成功还是透传，都产出 CleanDocument 列表；
    透传时逐篇标注 degraded=True（语义标注缺失），供下游维度按
    '缺失因子再归一化'处理。"""
    if isinstance(result, Passthrough):
        return [_with_evidence(CleanDocument(raw=d, degraded=True)) for d in result.payload]
    return result
