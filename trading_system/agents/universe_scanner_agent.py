"""全市场扫描 Agent（Layer 0）— v4.0 核心修复。

v3 根因：engine 内置硬编码 watchlist（AAPL/MSFT/GOOGL/NVDA/TSLA/AMD/META/
AMZN/SPY/QQQ），每次运行都分析同样的 10 只。本 Agent 彻底替换该机制：

  1. 载入股票池（core / extended / 自定义文件）
  2. 硬过滤（流动性与可交易性）：
       收盘价 ≥ $5；20 日平均成交额 ≥ $20M；历史 ≥ 260 日；ATR14/价 ≤ 12%
  3. 量化初排（真实数据计算，非搜索关键词）：
       RS63 分位 30% + RS20 分位 25% + 均线结构 20% + 波动收缩 15% + 接近新高 10%
  4. 输出 Top N 与主线补扫；严格研究另补评所有已出现入场形态的标的

扫描结果每次运行随市场数据变化而变化 —— “总是同样几只票”问题在此终结。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .. import config
from ..chains import chain_of
from ..data_models import StockCandidate
from ..indicators import atr, last, percentile_rank, rs_line, sma
from .base import BaseAgent
from .tss_agent import TSSAgent


def rank_universe(universe: list[str], market_data: dict, scan_filters=None, *, capture_decisions=False):
    """Pure market-only filtering/ranking shared by research frontier and final scan.

    No sectors, SHS, TSS entry setup or semantic evidence is read here.
    Final mainline/shape supplements stay in execute(), after final scoring.
    """
    spy_close: pd.Series = market_data["spy"]["Close"]
    stock_data: dict[str, pd.DataFrame] = market_data["stock_ohlcv"]
    # S4 多市场：硬过滤阈值按市场微调（config.MARKET_SCAN_FILTERS）；
    # US 缺省回退全局 SCAN_* 常量（现状行为不变）
    filt = scan_filters or {}
    min_price = filt.get("min_price", config.SCAN_MIN_PRICE)
    min_adv = filt.get("min_adv", config.SCAN_MIN_ADV_USD)

    passed: list[dict] = []
    rejected = {"price": 0, "adv": 0, "history": 0, "atr": 0, "nodata": 0}
    audit = {} if capture_decisions else None

    for ticker in universe:
        df = stock_data.get(ticker)
        if df is None or len(df) < config.SCAN_MIN_HISTORY_DAYS * 0.6:
            rejected["nodata"] += 1
            if audit is not None:
                audit[ticker] = {"hard_filter": "no_data" if df is None else "short_history"}
            continue
        close = df["Close"]
        price = last(close)
        if price < min_price:
            rejected["price"] += 1
            if audit is not None:
                audit[ticker] = {"hard_filter": "price"}
            continue
        adv = float((close * df["Volume"]).tail(20).mean())
        if adv < min_adv:
            rejected["adv"] += 1
            if audit is not None:
                audit[ticker] = {"hard_filter": "adv"}
            continue
        a14 = last(atr(df, 14))
        atr_pct = a14 / price if price > 0 else 9.9
        if atr_pct > config.SCAN_MAX_ATR_PCT:
            rejected["atr"] += 1
            if audit is not None:
                audit[ticker] = {"hard_filter": "atr"}
            continue
        if audit is not None:
            audit[ticker] = {"hard_filter": "pass"}

        # ---- 量化初排 ----
        rs = rs_line(close, spy_close)
        q63 = percentile_rank(rs.pct_change(63))
        q20 = percentile_rank(rs.pct_change(20))

        sma10, sma20, sma50 = last(sma(close, 10)), last(sma(close, 20)), last(sma(close, 50))
        ma_score = 10.0 if price > sma10 > sma20 > sma50 else \
                   8.0 if price > sma20 > sma50 else \
                   5.0 if price > sma50 else 2.0

        a50 = last(atr(df, 50))
        vc = a14 / a50 if a50 and not math.isnan(a50) else 1.0
        contraction = 10.0 if vc <= 0.7 else 8.0 if vc <= 0.85 else \
                      6.0 if vc <= 1.0 else 4.0 if vc <= 1.15 else 2.0

        hhv = float(df["High"].tail(252).max())
        near_high = min(10.0, max(0.0, (price / hhv - 0.75) / 0.25 * 10))

        q63n = 5.0 if math.isnan(q63) else q63 * 10
        q20n = 5.0 if math.isnan(q20) else q20 * 10
        w = config.SCAN_RANK_WEIGHTS
        rank = (q63n * w["rs_63"] + q20n * w["rs_20"] + ma_score * w["ma_align"]
                + contraction * w["contraction"] + near_high * w["near_high"])

        passed.append({
            "ticker": ticker, "price": price, "adv": adv,
            "atr_pct": atr_pct, "rank": round(rank, 3),
            "rs63_q": round(q63, 3) if not math.isnan(q63) else None,
        })

    passed.sort(key=lambda x: x["rank"], reverse=True)
    return passed, rejected, audit


class UniverseScannerAgent(BaseAgent):
    name = "Universe-Scanner"
    layer = 0

    def __init__(self, provider, universe: list[str], top_n: int = config.SCAN_TOP_N,
                 *, include_entry_setups: bool = False):
        super().__init__(provider)
        self.universe = universe
        self.top_n = top_n
        self.include_entry_setups = include_entry_setups

    def execute(self, context: dict) -> list[StockCandidate]:
        stock_data = context["market_data"]["stock_ohlcv"]
        passed, rejected, audit = rank_universe(
            self.universe, context["market_data"], context.get("scan_filters"),
            capture_decisions=bool(context.get("capture_scan_decisions")))
        top = passed[: self.top_n]

        # ---- 主线定向补扫（理论方向聚焦）：主线/准主线板块内的强结构票
        # 即使未进全局 Top N，也带入 TSS 精评（最多 SCAN_MAINLINE_BOOST 只）----
        boost = 0
        if config.SCAN_MAINLINE_BOOST > 0:
            from ..chains import mainline_tickers
            sectors = context.get("sectors", [])
            # v5.4：准主线口径与 sector_agent 主线闸门对齐——广度缺失（NaN）
            # 不再视同通过（白皮书：补扫资格需"SHS≥7.0 且广度≥60"证据）。
            hot_etfs = [s.etf for s in sectors
                        if s.in_main_pool
                        or (s.shs >= config.SHS_SUB_POOL
                            and not math.isnan(s.breadth)
                            and s.breadth >= config.BREADTH_HEALTHY)]
            ml = mainline_tickers(hot_etfs) if hot_etfs else set()
            chosen = {r["ticker"] for r in top}
            for row in passed:
                if boost >= config.SCAN_MAINLINE_BOOST:
                    break
                if row["ticker"] in ml and row["ticker"] not in chosen:
                    top.append(row)
                    chosen.add(row["ticker"])
                    boost += 1

        # Keep the existing shortlist for observation and options history.
        # An entry setup outside it earns the same full TSS and risk checks.
        baseline_selected = len(top)
        if audit is not None:
            for row in top:
                audit[row["ticker"]]["selection"] = "baseline"
        checked = unavailable = 0
        if self.include_entry_setups:
            chosen = {row["ticker"] for row in top}
            for row in passed:
                ticker = row["ticker"]
                if ticker in chosen:
                    continue
                checked += 1
                template = TSSAgent.entry_template_for(stock_data[ticker])
                if template is None:
                    unavailable += 1
                    if audit is not None:
                        audit[ticker]["selection"] = "shape_missing"
                elif template:
                    top.append(row)
                    chosen.add(ticker)
                    if audit is not None:
                        audit[ticker]["selection"] = "supplement"
                elif audit is not None:
                    audit[ticker]["selection"] = "no_entry_setup"

        candidates: list[StockCandidate] = []
        for row in top:
            cid, link = chain_of(row["ticker"])
            from ..activity import enabled, sector_evidence
            sector = sector_evidence(row["ticker"], cid or "", active=enabled(context))
            candidates.append(StockCandidate(
                ticker=row["ticker"], rank_score=row["rank"],
                price=row["price"], adv_usd=row["adv"], atr_pct=row["atr_pct"],
                chain_id=cid or "", chain_link=link or "",
                sector_etf=sector["etf"],
            ))

        context["scan_stats"] = {
            "universe_size": len(self.universe),
            "data_ok": len(passed) + sum(rejected.values()) - rejected["nodata"],
            "rejected": rejected,
            "passed": len(passed),
            "selected": len(candidates),
            "mainline_boost": boost,
            # v6.3：候选链映射率披露（无映射候选在 L4 被隐性降权，必须显性化）
            "chain_mapped": f"{sum(1 for c in candidates if c.chain_id)}/{len(candidates)}",
            "top10": [(c.ticker, c.rank_score) for c in candidates[:10]],
        }
        if self.include_entry_setups:
            context["scan_stats"]["entry_setup_supplement"] = {
                "checked": checked, "added": len(candidates) - baseline_selected,
                "baseline_selected": baseline_selected, "unavailable": unavailable,
            }
        context["watchlist"] = candidates
        if audit is not None:
            context["scan_decisions"] = audit
        self.log.info("扫描完成: 池 %d → 通过过滤 %d → 候选 %d；Top10=%s",
                      len(self.universe), len(passed), len(candidates),
                      context["scan_stats"]["top10"])
        return candidates
