"""科技股产业链专项 Agent 集群（用户指令三）。

五个专项 Agent：
  1. TechChainMonitorAgent（rules）：全产业链实时监测——各环节代表标的的
     行情动能（20/60 日动量、相对强度、环节内广度）；
  2. CycleLinkageAgent（rules）：产业周期与全球化联动——三星/海力士/台积电
     等全球龙头的动量映射产能/库存周期，跨市场背离检测；
  3. ChainSentimentAgent（llm）：舆情热度/情感聚合——全部经 LLM 推理；
  4. ChainRiskAgent（llm）：风险预警——供应链扰动/政策微调/专利动态，
     全部经 LLM 推理；
  5. TechChainFusionAgent 在 fusion.py（rules）：把 1-4 的产出按固定权重
     合成 TechChainSignal（缺失维度剔除再归一化，与全系统校准原则一致）。

红线对应：3/4 是 LLM 驱动（llm_guard，不可用时透传文档，无规则回退）；
1/2/5 是确定性量化计算（属"阈值触发/格式转换"同级的规则保留区）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..llm.client import LLMClient
from ..llm.prompts import (RISK_SCHEMA, RISK_USER_TMPL, SENTIMENT_SCHEMA,
                           SENTIMENT_USER_TMPL, SYSTEM_ANALYST)
from ..redline import ExecutionTracer, Passthrough, llm_guard
from ..redline.execution import EvidenceWriteError, ExecutionContractError, StrictObservationError
from ..search.models import CleanDocument
from .universe import TECH_SUBCHAINS

log = logging.getLogger("tech_chain.agents")


# ---------------------------------------------------------------- 产出结构
@dataclass
class LinkMomentum:
    link: str                    # upstream/midstream/downstream
    tickers_ok: int
    mom20: float | None          # 环节 20 日平均动量（小数）
    mom60: float | None
    breadth: float | None        # 环节内 >20SMA 占比 0..1


@dataclass
class ChainMonitorRow:
    chain_id: str
    links: list[LinkMomentum] = field(default_factory=list)
    leading_link: str | None = None       # 当前领涨环节
    chain_mom20: float | None = None      # 链整体 20 日动量（各环节等权）


@dataclass
class GlobalLinkageRow:
    ticker: str                  # 005930.KS / 000660.KS / 2330.TW / TSM / MU
    mom20: float | None
    mom60: float | None
    rs_vs_spy: float | None      # 20 日相对强度（小数差）


@dataclass
class SentimentRow:
    chain_id: str
    heat: float | None = None            # 0..10
    sentiment_score: float | None = None  # -1..1
    narrative_change: str | None = None   # improving/stable/deteriorating
    key_drivers: list[str] = field(default_factory=list)
    degraded: bool = True


@dataclass
class RiskAlert:
    severity: float              # 1..10
    chain_id: str
    link: str
    type: str                    # supply_chain/policy/patent/capacity/other
    headline_zh: str
    transmission: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 1. 监测
class TechChainMonitorAgent:
    """tech.monitor（rules）：六条子链 × 三环节的行情动能监测。"""

    RETRY_BUDGET_S = 45.0     # 逐只重试的墙钟预算：超时就停，空表透传（红线 3）

    def __init__(self, provider):
        self.provider = provider

    @staticmethod
    def _mom(df: pd.DataFrame, win: int) -> float | None:
        if df is None or len(df) < win + 1:
            return None
        c = df["Close"].to_numpy(dtype=float)
        if c[-win - 1] <= 0:
            return None
        return float(c[-1] / c[-win - 1] - 1.0)

    def execute(self, tracer: ExecutionTracer | None = None,
                prefetched: dict[str, pd.DataFrame] | None = None, *,
                excluded: frozenset[str] = frozenset()) -> list[ChainMonitorRow]:
        import time
        tickers = sorted({t for c in TECH_SUBCHAINS.values()
                          for l in c["links"].values() for t in l} - excluded)
        # 优先复用 pipeline 数据准备阶段已下载的数据（避免重复拉取卡墙钟）
        data: dict[str, pd.DataFrame] = {}
        missing: list[str] = []
        for t in tickers:
            df = (prefetched or {}).get(t)
            if df is not None and len(df) >= 25:
                data[t] = df
            else:
                missing.append(t)
        if missing:
            deadline = time.time() + self.RETRY_BUDGET_S
            try:
                extra = self.provider.ohlcv_batch(missing, days=90)
                data.update(extra)
            except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                raise
            except Exception as e:
                log.info("tech.monitor 批量补充失败，退化为逐只（预算 %.0fs）: %s",
                         self.RETRY_BUDGET_S, e)
                for t in missing:
                    if time.time() > deadline:
                        log.warning("tech.monitor 重试预算耗尽，%d/%d 透传",
                                    len(data), len(tickers))
                        break
                    try:
                        data[t] = self.provider.ohlcv(t, days=90)
                    except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                        raise
                    except Exception:
                        continue
        rows: list[ChainMonitorRow] = []
        for cid, spec in TECH_SUBCHAINS.items():
            row = ChainMonitorRow(chain_id=cid)
            for link, ts in spec["links"].items():
                moms20, moms60, above = [], [], []
                for t in ts:
                    df = data.get(t)
                    if df is None or len(df) < 25:
                        continue
                    m20 = self._mom(df, 20)
                    m60 = self._mom(df, 60)
                    c = df["Close"].to_numpy(dtype=float)
                    sma20 = float(np.mean(c[-20:]))
                    if m20 is not None:
                        moms20.append(m20)
                    if m60 is not None:
                        moms60.append(m60)
                    above.append(1.0 if c[-1] > sma20 else 0.0)
                if not above:
                    continue
                row.links.append(LinkMomentum(
                    link=link, tickers_ok=len(above),
                    mom20=float(np.mean(moms20)) if moms20 else None,
                    mom60=float(np.mean(moms60)) if moms60 else None,
                    breadth=float(np.mean(above)),
                ))
            if row.links:
                scored = [(l.link, l.mom20) for l in row.links if l.mom20 is not None]
                if scored:
                    row.leading_link = max(scored, key=lambda x: x[1])[0]
                    row.chain_mom20 = float(np.mean([s for _, s in scored]))
            rows.append(row)
        return rows


# ---------------------------------------------------------------- 2. 全球联动
class CycleLinkageAgent:
    """tech.cycle_linkage（rules）：三星/海力士/台积电/美光的周期映射与
    跨市场背离（2330.TW vs TSM ADR 同公司双市场印证）。"""

    LEADERS = ["005930.KS", "000660.KS", "2330.TW", "TSM", "MU"]

    def __init__(self, provider):
        self.provider = provider

    def execute(self, tracer: ExecutionTracer | None = None, *,
                prefetched: dict[str, pd.DataFrame] | None = None,
                fetch=None) -> list[GlobalLinkageRow]:
        import time
        rows: list[GlobalLinkageRow] = []
        deadline = time.time() + 45.0
        try:
            spy = prefetched["SPY"] if prefetched is not None else self.provider.ohlcv("SPY", days=90)
        except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
            raise
        except Exception:
            spy = None
        spy_m20 = TechChainMonitorAgent._mom(spy, 20) if spy is not None else None
        for t in self.LEADERS:
            df = prefetched.get(t) if prefetched is not None else None
            if df is None:
                if time.time() > deadline:
                    log.warning("tech.cycle_linkage 拉取预算耗尽，%d/%d 尚有可用",
                                len(rows), len(self.LEADERS))
                    if prefetched is None:
                        break
                    continue
                try:
                    df = fetch(t) if fetch is not None else self.provider.ohlcv(t, days=90)
                except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                    raise
                except Exception as e:
                    log.info("联动标的 %s 拉取失败: %s", t, e)
                    continue
            if prefetched is not None and df is None:
                continue
            m20 = TechChainMonitorAgent._mom(df, 20)
            m60 = TechChainMonitorAgent._mom(df, 60)
            rs = (m20 - spy_m20) if (m20 is not None and spy_m20 is not None) else None
            rows.append(GlobalLinkageRow(ticker=t, mom20=m20, mom60=m60, rs_vs_spy=rs))
        return rows


# ---------------------------------------------------------------- 3. 舆情
class ChainSentimentAgent:
    """tech.sentiment（llm）：六链舆情热度/情感/叙事变化 —— 全部 LLM 推理。"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def execute(self, docs: list[CleanDocument],
                tracer: ExecutionTracer | None = None
                ) -> dict[str, SentimentRow] | Passthrough:
        def _run() -> dict[str, SentimentRow]:
            payload = "\n\n".join(
                f"<doc id=\"{c.raw.doc_id}\" tickers=\"{','.join(c.tickers)}\">"
                f"{c.raw.content[:800]}</doc>" for c in docs[:24])
            out = self.llm.complete_json(
                system=SYSTEM_ANALYST,
                user=SENTIMENT_USER_TMPL.format(docs=payload),
                schema_hint=SENTIMENT_SCHEMA)
            rows: dict[str, SentimentRow] = {}
            for item in out.get("chains", []):
                cid = str(item.get("chain_id") or "")
                if cid not in TECH_SUBCHAINS:
                    continue
                rows[cid] = SentimentRow(
                    chain_id=cid,
                    heat=_clamp(item.get("heat"), 0, 10),
                    sentiment_score=_clamp(item.get("sentiment_score"), -1, 1),
                    narrative_change=item.get("narrative_change"),
                    key_drivers=[str(x) for x in (item.get("key_drivers") or [])][:5],
                    degraded=False)
            return rows or None  # 空 → llm_guard 走透传

        return llm_guard("tech.sentiment", _run, fallback_payload=docs, tracer=tracer)


# ---------------------------------------------------------------- 4. 风险
class ChainRiskAgent:
    """tech.risk（llm）：供应链/政策/专利风险预警 —— 全部 LLM 推理。"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def execute(self, docs: list[CleanDocument],
                tracer: ExecutionTracer | None = None
                ) -> list[RiskAlert] | Passthrough:
        def _run() -> list[RiskAlert]:
            payload = "\n\n".join(
                f"<doc id=\"{c.raw.doc_id}\" source=\"{c.raw.source}\">"
                f"{c.raw.content[:900]}</doc>" for c in docs[:24])
            out = self.llm.complete_json(
                system=SYSTEM_ANALYST,
                user=RISK_USER_TMPL.format(docs=payload),
                schema_hint=RISK_SCHEMA)
            alerts: list[RiskAlert] = []
            for a in out.get("alerts", []):
                sev = _clamp(a.get("severity"), 1, 10)
                if sev is None:
                    continue
                alerts.append(RiskAlert(
                    severity=sev,
                    chain_id=str(a.get("chain_id") or "unknown"),
                    link=str(a.get("link") or "unknown"),
                    type=str(a.get("type") or "other"),
                    headline_zh=str(a.get("headline_zh") or ""),
                    transmission=[str(x) for x in (a.get("transmission") or [])][:6],
                    evidence_ids=[str(x) for x in (a.get("evidence_ids") or [])][:6],
                ))
            return alerts if alerts else None

        return llm_guard("tech.risk", _run, fallback_payload=docs, tracer=tracer)


def _clamp(v, lo, hi) -> float | None:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return None
