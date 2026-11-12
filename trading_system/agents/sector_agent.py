"""Sector Agent — SHS 板块热度评分（理论第二道门）。

对每个可交易板块 ETF 计算四因子：
  S_macro  宏观适配（TNX 趋势固定查表）
  S_flow   资金动量 = round(0.45*RS斜率分位 + 0.25*R20 + 0.30*板块广度)
  S_narr   叙事兑现（EPS 上修/指引/IV 压缩；免费源缺失 → 中性 5 + 记录）
  S_micro  微观催化（Call OI/Skew；缺失 → 中性 5 + 记录）

输出主线池（SHS≥7.5 且广度≥60%）、次主线（7.0-7.5），主线池最多 2 条。
"""

from __future__ import annotations

import math

import pandas as pd

from .. import config
from ..data_models import SectorScore
from ..indicators import (
    aggregate, breadth_above_sma, last, percentile_rank, pct_change_n,
    rs_line, score_from_quantile, score_sector_macro, score_sector_r20,
    sma, tnx_trend_label,
)
from .base import BaseAgent


class SectorAgent(BaseAgent):
    name = "Sector-Agent"
    layer = 2

    def execute(self, context: dict) -> list[SectorScore]:
        p = self.provider
        tnx: pd.Series | None = context["market_data"].get("tnx")
        spy: pd.DataFrame = context["market_data"]["spy"]
        sector_data: dict[str, pd.DataFrame] = context["market_data"]["sector_etfs"]
        universe_closes: dict[str, pd.Series] = context["market_data"]["universe_closes"]

        # S4：利率基准缺失（CN/HK 免费源不可得）→ 宏观适配维整体缺失，
        # 逐板块剔除再归一化（D2：宁可缺失不可编造，不钉中性分）
        if tnx is None or len(tnx.dropna()) == 0:
            label = None
        else:
            tnx_clean = tnx.dropna()
            tnx_chg_bp = (tnx_clean.iloc[-1] - tnx_clean.iloc[-21]) * 100 \
                if len(tnx_clean) > 21 else 0.0
            label = tnx_trend_label(tnx_chg_bp)

        results: list[SectorScore] = []
        for etf, df in sector_data.items():
            if df is None or len(df) < 120:
                continue
            results.append(self._score_sector(etf, df, spy["Close"], label,
                                              universe_closes, context))

        results.sort(key=lambda s: s.shs, reverse=True)

        # 主线池规则：SHS≥7.5 且广度≥60% 为主线；7.0-7.5 次主线；主线最多 2 条
        # v5.4 修复：广度【缺失】（NaN，如 IWM/XLRE 无产业链成分映射）旧代码
        # 视同通过——"广度≥60%"硬条件形同虚设。白皮书口径：无广度证据不得进
        # 主线池，降入次主线（观察/轻仓）并在证据中披露。
        from ..activity import assign_pools, enabled
        assign_pools(results, active=enabled(context))

        context["sectors"] = results
        context["sector_map"] = {s.etf: s for s in results}
        self.log.info("SHS 完成: %s", [(s.etf, s.shs, "主线" if s.in_main_pool else "次主线" if s.in_sub_pool else "-") for s in results[:5]])
        return results

    def _score_sector(self, etf: str, df: pd.DataFrame, spy_close: pd.Series,
                      tnx_label: str | None, universe_closes: dict[str, pd.Series],
                      context: dict) -> SectorScore:
        close = df["Close"]
        evidence: list[str] = []

        # 1) 宏观适配（固定查表；利率基准缺失 → 该维剔除再归一化）
        if tnx_label is None:
            s_macro = None
            evidence.append(f"{etf} 宏观适配: 利率基准缺失 → 该维剔除再归一化（D2）")
        else:
            s_macro = score_sector_macro(etf, tnx_label)
            evidence.append(f"{etf} 宏观适配: TNX趋势={tnx_label}→{s_macro}分")

        # 2) 资金动量（最关键）
        rs = rs_line(close, spy_close)
        rs_slope_series = rs.pct_change(20)
        q = percentile_rank(rs_slope_series)
        a_flow = score_from_quantile(q)
        r20 = pct_change_n(close, 20)
        b_flow = score_sector_r20(r20)

        # 板块内部广度代理：股票池中属于该板块/产业链的标的中 50D 上方占比
        # v6.0：无产业链映射的宽基 ETF（IWM/XLRE 等）回退用【全池广度】作代理
        # 并如实披露——v5.4 起广度缺失不得进主线池，若无代理这些板块将永远
        # 被排除在主线之外，属于另一种口径失真。
        from ..chains import SECTOR_TO_CHAINS, CHAINS
        member_closes: dict[str, pd.Series] = {}
        for cid in SECTOR_TO_CHAINS.get(etf, []):
            for link in ("upstream", "midstream", "downstream"):
                for t in CHAINS[cid][link]["tickers"]:
                    if t in universe_closes:
                        member_closes[t] = universe_closes[t]
        if member_closes:
            breadth = breadth_above_sma(member_closes, 50)
            breadth_note = f"内部广度{breadth:.0f}%"
        else:
            breadth = breadth_above_sma(universe_closes, 50)
            breadth_note = f"广度{breadth:.0f}%（全池代理，无板块成分映射）"
        c_flow = score_from_quantile(breadth / 100) if not math.isnan(breadth) else None

        s_flow = aggregate({"A": a_flow, "B": b_flow, "C": c_flow},
                           {"A": 0.45, "B": 0.25, "C": 0.30})
        evidence.append(
            f"{etf} 资金动量: RS斜率分位{q:.2f}→{a_flow}分; R20={r20:+.1%}→{b_flow}分; {breadth_note}→{c_flow}分 ⇒ S_flow={s_flow}"
        )

        # 3) 叙事兑现（v5.0 起 LLM 驱动：NarrativeAgent 注入；
        #    LLM 不可用 → None 剔除再归一化，红线禁止规则估算）
        narr_llm = context.get("narrative_llm") or {}
        s_narr = None
        if etf in narr_llm and narr_llm[etf]["score"] is not None:
            s_narr = round(float(narr_llm[etf]["score"]), 1)
            evidence.append(
                f"{etf} 叙事兑现: LLM 评分 {s_narr}（EPS修正 {narr_llm[etf].get('bias')}, "
                f"指引 {narr_llm[etf].get('tone')}; {narr_llm[etf].get('evidence', '')[:60]}）"
            )
        else:
            status = narr_llm.get(etf, {}).get("status", "unavailable")
            note = narr_llm.get(etf, {}).get("evidence", "")
            evidence.append(f"{etf} 叙事兑现: {status}，{note} → 缺失维剔除后再归一化")

        # 4) 微观催化（缺失 → None）
        s_micro = None
        evidence.append(f"{etf} 微观催化: 期权OI/Skew 数据源缺失 → 剔除后再归一化")

        shs = aggregate(
            {"macro": s_macro, "flow": s_flow, "narr": s_narr, "micro": s_micro},
            config.SHS_WEIGHTS,
        )
        evidence.append(
            f"{etf} SHS = {shs}（macro {s_macro if s_macro is not None else '缺失'}/flow {s_flow}/"
            f"narr {s_narr if s_narr is not None else '缺失'}/micro 缺失，再归一化）")

        return SectorScore(
            etf=etf, shs=shs,
            factors={"macro": s_macro, "flow": s_flow, "narr": s_narr, "micro": s_micro},
            breadth=breadth, rs_slope_q=q, r20=r20, evidence=evidence,
        )
