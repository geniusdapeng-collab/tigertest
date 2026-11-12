"""来源可信度分级与交叉验证 — S3 数据层（Tiger Data Fabric）核心。

红线定位（D9）：本模块是【规则层调度逻辑】——可信度分级是"来源名/域名 → 档位"
的确定映射，交叉验证是"实体+事件类型的哈希匹配 + 源数/档位计数"。
本模块不存在、也不允许存在任何 LLM 调用；语义标注（实体/事件类型）由
clean.llm_semantic 环节的 LLM 产出，本模块只消费其结构化字段。

分级（config.SOURCE_TIERS / DOMAIN_TIERS，可覆盖）：
  T0 交易所/监管原文（SEC EDGAR / Federal Register / 港交所披露易 / 巨潮）
  T1 主流财经媒体（agent-gw 全网 / Google News 中的白名单域）
  T2 聚合门户
  T3 社媒（Reddit / 雪球类）

交叉验证（≥2 源才进决策）：
  关键事件类文档（财报/并购/政策/供应链扰动等，由 LLM 事件标注驱动）要求
  ≥2 个不同源佐证，且至少一个源 ≤ T2。未达标标记 corroborated=False：
  不删除（透传纪律），但叙事/舆情打分输入只使用 corroborated=True 的集合；
  LLM 语义标注缺失（degraded）的文档全部记为未验证并透传披露，不阻塞管线。
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import IntEnum
from urllib.parse import urlsplit

from .. import config
from .models import CleanDocument, Evidence, RawDocument
from .ontology import legacy_event_type

log = logging.getLogger("search.credibility")


class SourceTier(IntEnum):
    """来源可信度档位（值越小越权威）。"""
    T0 = 0   # 交易所/监管原文
    T1 = 1   # 主流财经媒体
    T2 = 2   # 聚合门户
    T3 = 3   # 社媒

    @staticmethod
    def of(value: "SourceTier | str | int") -> "SourceTier":
        if isinstance(value, SourceTier):
            return value
        if isinstance(value, str):
            return SourceTier[value.strip().upper()]
        return SourceTier(int(value))


# ---------------------------------------------------------------- 分级
def _domain_of(url: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/]+)", url or "")
    return (m.group(1).lower() if m else "").split(":")[0]


def tier_for_source(source: str, url: str = "") -> SourceTier:
    """来源 → 可信度档位。域名覆盖表优先（同一源不同落地域再分级），
    其次源默认表（config.SOURCE_TIERS，可覆盖），最后保守回退 T2。"""
    domain = _domain_of(url)
    if domain:
        for suffix, tier in config.DOMAIN_TIERS.items():
            if domain == suffix or domain.endswith("." + suffix):
                return SourceTier.of(tier)
    return SourceTier.of(config.SOURCE_TIERS.get(source, config.SOURCE_TIER_DEFAULT))


# ---------------------------------------------------------------- Point-in-Time 时间戳
def parse_published(published: str) -> float | None:
    """把各源的 published 字符串规整为 epoch 秒（UTC）。无法解析返回 None
    （调用方在统计中披露计数——"当时可知"纪律不容忍编造时间）。"""
    s = (published or "").strip()
    if not s:
        return None
    try:                                   # ISO 日期 / 日期时间（EDGAR、demo 等）
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    try:                                   # RFC 2822（Google News pubDate）
        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except (TypeError, ValueError):
        pass
    try:                                   # epoch 秒（Reddit created_utc）
        return float(s)
    except ValueError:
        return None


def content_hash(content: str) -> str:
    """正文内容哈希（sha256 前 16 位）。确定性：同内容同哈希。"""
    return hashlib.sha256((content or "").encode("utf-8", "ignore")).hexdigest()[:16]


def evidence_for(raw: RawDocument) -> Evidence:
    """从原始文档构建证据元数据（清洗管线输出必须携带）。"""
    return Evidence(
        content=raw.content,
        source=raw.source,
        tier=tier_for_source(raw.source, raw.url).name,
        published_at=parse_published(raw.published),
        content_hash=content_hash(raw.content),
        fetched_at=raw.fetched_at,
    )


# ---------------------------------------------------------------- 交叉验证
def _normalize(t: str) -> str:
    t = unicodedata.normalize("NFKC", t or "")
    return re.sub(r"\s+", " ", t).strip().lower()


def _canonical_event(events: list[str], meta_hint: str = "") -> str | None:
    """把 LLM 事件标注/源 meta 提示归一到 config.KEY_EVENT_TYPES 规范型。
    纯规则：别名子串匹配（别名表在 config，可维护）。"""
    return legacy_event_type(events, meta_hint, event_types=config.KEY_EVENT_TYPES,
                             aliases=config.KEY_EVENT_ALIASES)


def _entity_key(doc: CleanDocument) -> str:
    """实体键：LLM 标注的 ticker 集合优先；无标注时用归一化标题指纹
    （跨源同事件标题几乎不可能逐字相同 → 保守不配对，宁缺毋滥）。"""
    if doc.tickers:
        return "T:" + ",".join(sorted({t.upper() for t in doc.tickers}))
    base = _normalize(doc.raw.title)[:80]
    return "H:" + hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:12]


def corroboration_source(doc: CleanDocument) -> str:
    """Native search is a retriever; corroborate the original publisher hosts.

    Preserve every legacy source identity. For the new provider, domains already
    present in the reviewed tier table also consolidate their subdomains. An
    unknown publisher is counted by its complete host, never by a model label.
    """
    if doc.raw.source != "deepseek_search":
        return doc.raw.source
    host = (urlsplit(doc.raw.url).hostname or "").lower().rstrip(".")
    for suffix in sorted(config.DOMAIN_TIERS, key=len, reverse=True):
        if host == suffix or host.endswith("." + suffix):
            return "publisher:" + suffix
    return "publisher:" + host.removeprefix("www.")


@dataclass
class CrossValidationStats:
    total: int = 0                  # 进入验证的文档总数
    key_event_docs: int = 0         # 其中关键事件类文档数
    corroborated: int = 0           # 关键事件中通过交叉验证数
    downgraded: int = 0             # 被降级数（关键事件未达标 + LLM 标注缺失）
    llm_missing: int = 0            # LLM 语义标注缺失（degraded）全量记未验证
    missing_published_at: int = 0   # 缺 Point-in-Time 时间戳（披露计数）

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "key_event_docs": self.key_event_docs,
            "corroborated": self.corroborated,
            "downgraded": self.downgraded,
            "llm_missing": self.llm_missing,
            "missing_published_at": self.missing_published_at,
        }


class CrossValidator:
    """关键事件交叉验证器（纯规则，无 LLM）。

    规则：同（实体, 事件类型）分组内，≥min_sources 个不同源 且至少一个源
    档位 ≤ max_tier（默认 T2，即 T0/T1/T2 均可，社媒 T3 单独不算数）。
    副作用：就地更新每个 CleanDocument.corroborated；返回统计供日报披露。
    """

    def __init__(self, min_sources: int | None = None,
                 max_tier: "SourceTier | str | None" = None):
        self.min_sources = min_sources or config.CORROBORATION_MIN_SOURCES
        self.max_tier = SourceTier.of(max_tier or config.CORROBORATION_MAX_TIER)

    def validate(self, docs: list[CleanDocument]) -> dict:
        stats = CrossValidationStats(total=len(docs))
        groups: dict[tuple[str, str], list[CleanDocument]] = {}
        for d in docs:
            if d.evidence is None:                       # 防御：证据必须已挂载
                d.evidence = evidence_for(d.raw)
            if d.evidence.published_at is None:
                stats.missing_published_at += 1
            if d.degraded:
                # LLM 语义标注缺失：全部记为未验证（透传披露，不阻塞管线）
                d.corroborated = False
                stats.llm_missing += 1
                stats.downgraded += 1
                continue
            canon = _canonical_event(d.events, str(d.raw.meta.get("event_hint", "")))
            if canon is None:
                continue                                 # 非关键事件：不需交叉验证
            stats.key_event_docs += 1
            groups.setdefault((_entity_key(d), canon), []).append(d)

        for (entity, canon), members in groups.items():
            sources = {corroboration_source(m) for m in members}
            best_tier = min(SourceTier.of(m.evidence.tier) for m in members)
            ok = len(sources) >= self.min_sources and best_tier <= self.max_tier
            for m in members:
                m.corroborated = ok
            if ok:
                stats.corroborated += len(members)
            else:
                stats.downgraded += len(members)
                log.info("[交叉验证] 降级 %d 篇（实体=%s 事件=%s 源=%s 最优档=%s）",
                         len(members), entity[:24], canon, sorted(sources), best_tier.name)
        return stats.to_dict()


def scoring_docs(docs: list[CleanDocument]) -> list[CleanDocument]:
    """叙事/舆情打分输入集合：只用 corroborated=True 的文档；
    被降级文档不删除（透传纪律），决策侧仅作背景参考。"""
    return [d for d in docs if d.corroborated]
