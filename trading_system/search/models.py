"""搜索数据模型。"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RawDocument:
    """一个搜索源返回的原始文档（未清洗）。"""
    doc_id: str          # 内容哈希（去重键）
    source: str          # 来源标识：kimi_search / edgar / google_news / reddit / patentsview / federal_register / demo
    title: str
    url: str
    content: str
    published: str       # ISO 日期或 ""
    fetched_at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)   # 源特定字段（如 EDGAR form 类型）

    @staticmethod
    def make_id(source: str, url: str, title: str, content: str) -> str:
        h = hashlib.sha1(f"{source}|{url}|{title}|{content[:256]}".encode("utf-8", "ignore"))
        return h.hexdigest()[:16]


@dataclass
class Evidence:
    """证据元数据（S3 数据层）：清洗管线输出的每条文档必须携带。

    - tier：来源可信度分级（"T0".."T3"，规则映射，见 search/credibility.py）；
    - published_at：Point-in-Time 时间戳（epoch 秒，"当时可知"纪律）；
      源未提供发布时间时为 None，并在交叉验证统计中披露计数；
    - content_hash：正文 sha256 前 16 位（同内容同哈希，用于审计与对账）；
    - fetched_at：抓取时刻（epoch 秒）。
    """
    content: str
    source: str
    tier: str = ""
    published_at: float | None = None
    content_hash: str = ""
    fetched_at: float = 0.0


@dataclass
class CleanDocument:
    """清洗后的文档：规则字段 + LLM 语义标注（degraded=True 表示语义标注缺失）。"""
    raw: RawDocument
    tickers: list[str] = field(default_factory=list)
    sentiment: str | None = None            # bullish/bearish/neutral/mixed
    sentiment_score: float | None = None    # -1..1
    events: list[str] = field(default_factory=list)
    summary_zh: str | None = None
    relevance: float | None = None
    degraded: bool = False                  # True = LLM 语义标注缺失（透传）
    # S3 数据层：证据元数据 + 交叉验证结论（规则层调度，见 search/credibility.py）
    evidence: Evidence | None = None
    corroborated: bool = True               # False = 未通过交叉验证，决策侧仅作背景参考


@dataclass
class StructuredDocument(CleanDocument):
    """V2 annotations and computed per-event uses; v1 snapshots stay unchanged.

    The model supplies subjects/spans/events. Only the deterministic validator
    supplies ``eligibility``. A document-level flag is not sufficient to grant
    a particular target, claim or security scoring/action permission.
    """
    subjects: list[dict] = field(default_factory=list)
    evidence_spans: list[dict] = field(default_factory=list)
    event_records: list[dict] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    eligibility: dict = field(default_factory=dict)
    annotation_contract: str = "typed-evidence/v2"


@dataclass
class SearchBatch:
    """一次搜索任务的产出。"""
    query: str
    docs: list[RawDocument]
    source_stats: dict[str, dict] = field(default_factory=dict)  # source -> {ok, n, ms, err}
