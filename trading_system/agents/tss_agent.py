"""TSS Agent（Layer 3）— 个股建仓评分 + 入场模板检测（理论第三道门）。

对扫描器产出的每只候选，用真实 OHLCV 计算：
  S_structure = 0.40*距关键位 + 0.40*突破回踩质量 + 0.20*流动性真空
  S_momentum  = 0.40*均线结构 + 0.35*波动收缩(ATR14/ATR50) + 0.25*ADX14
  S_options   = 期权数据缺失 → 中性 5 分 + evidence 记录
  TSS         = 0.40*structure + 0.40*momentum + 0.20*options

入场模板（理论 §4.2）：
  模板A 突破后回踩确认：放量突破 + 缩量回踩不破 + 再收高
  模板B 收缩后放量启动：ATR 收缩(≤0.85) + 当日放量(≥1.5)突破 HHV20
  模板C 趋势回撤到均线：均线多头 + 回撤至 10/20D 附近缩量企稳

产业链周期加成：热区链（复苏/扩张）内标的 TSS ×≤1.15；衰退链 ×0.95。
"""

from __future__ import annotations

import math

import pandas as pd

from .. import config
from ..data_models import ChainState, StockCandidate
from ..indicators import (
    aggregate, extract_structure_features, last, score_adx,
    score_breakout_quality, score_dist_to_key, score_ma_alignment_tss,
    score_resistance_touches, score_vol_contraction, sma,
)
from ..options_metrics import score_options
from .base import BaseAgent
from .chain_cycle_agent import chain_bonus


class TSSAgent(BaseAgent):
    name = "TSS-Agent"
    layer = 3
    MIN_HISTORY = 130

    def execute(self, context: dict) -> list[StockCandidate]:
        stock_data: dict[str, pd.DataFrame] = context["market_data"]["stock_ohlcv"]
        candidates: list[StockCandidate] = context["watchlist"]
        chain_map: dict[str, ChainState] = context.get("chain_map", {})
        # 科技股产业链子集群的标准化汇入（核心因子之一）：主链 → 乘性加成
        tech_bonus_map: dict[str, float] = context.get("tech_bonus_map", {})
        tech_signals: dict = context.get("tech_signal_map", {})

        for c in candidates:
            df = stock_data.get(c.ticker)
            if df is None or len(df) < self.MIN_HISTORY:
                continue
            if context.get("options_store") is not None:
                self._score(c, df, options_store=context["options_store"])
            else:
                self._score(c, df)
            bonus = chain_bonus(chain_map.get(c.chain_id))
            tech_hint = tech_bonus_map.get(c.chain_id, 1.0)
            if tech_hint != 1.0:
                bonus = round(max(0.85, min(config.CHAIN_BONUS_MAX, bonus * tech_hint)), 4)
                sig = tech_signals.get(c.chain_id)
                note = (f"科技链景气 {sig.prosperity}/10" if sig and sig.prosperity is not None
                        else "科技链加成")
                c.evidence.append(f"{note} → 额外加成 ×{tech_hint:.3f}")
            c.tss_final = round(min(10.0, c.tss * bonus), 2)
            if bonus != 1.0:
                from ..chains import chain_name_zh
                c.evidence.append(
                    f"产业链加成 ×{bonus:.3f}（{chain_name_zh(c.chain_id)}）→ TSS_final={c.tss_final}")

        candidates.sort(key=lambda x: x.tss_final, reverse=True)
        context["watchlist"] = candidates
        self.log.info("TSS 完成: %s", [(c.ticker, c.tss_final, c.entry_template) for c in candidates[:10]])
        return candidates

    # ------------------------------------------------------------

    def _score(self, c: StockCandidate, df: pd.DataFrame, *, options_store=None) -> None:
        f = extract_structure_features(df)
        close = df["Close"]
        vol = df["Volume"]

        # ---- S_structure ----
        a = score_dist_to_key(f["d_atr"])
        b = score_breakout_quality(f["flags"])
        d = score_resistance_touches(f["touch"])
        c.s_structure = aggregate({"A": a, "B": b, "C": d}, config.TSS_STRUCTURE_AGG)

        # ---- S_momentum ----
        ma = score_ma_alignment_tss(f["close"], f["sma10"], f["sma20"], f["sma50"],
                                    f["sma50_rising"])
        vc_score = score_vol_contraction(f["vc"]) if not math.isnan(f["vc"]) else None
        adx_score = score_adx(f["adx14"]) if not math.isnan(f["adx14"]) else None
        c.s_momentum = aggregate({"A": ma, "B": vc_score, "C": adx_score}, config.TSS_MOMENTUM_AGG)

        # ---- S_options（真实期权链 + 历史分位；缺失/样本不足 → None 再归一化）----
        opt = (score_options(c.ticker, self.provider, store=options_store)
               if options_store is not None else score_options(c.ticker, self.provider))
        c.s_options = opt["s_options"]              # None 表示缺失，聚合时剔除
        opt_evidence = opt["evidence"]
        for m in opt.get("missing", []):
            c.evidence.append(f"[缺失→剔除归一化] {m}")

        c.tss = aggregate(
            {"structure": c.s_structure, "momentum": c.s_momentum, "options": c.s_options},
            config.TSS_WEIGHTS)

        # ---- 流动性系数 C_liq（0.5-1.1，理论 §6.2）----
        c.c_liq = self._c_liq(c.adv_usd)

        # ---- 入场模板检测 ----
        c.key_level = f["key_level"]
        c.entry_template = self._detect_template(f, df)
        c.stop_plan = self._stop_plan(c, f)
        c.stop_price = self._stop_price(c, f)     # v6.0：结构化止损价（消灭正则解析）

        vc_txt = f"{vc_score}分" if vc_score is not None else "缺失"
        adx_txt = f"{adx_score}分" if adx_score is not None else "缺失"
        c.evidence.extend([
            f"结构: 距关键位{f['d_atr']:.2f}ATR→{a}分; 突破回踩质量→{b}分; 上方触点{f['touch']}→{d}分 ⇒ S_structure={c.s_structure}",
            f"动能: 均线→{ma}分; ATR收缩{f['vc']:.2f}→{vc_txt}; ADX{f['adx14']:.0f}→{adx_txt} ⇒ S_momentum={c.s_momentum}",
            opt_evidence,
            f"TSS={c.tss}（0.4/0.4/0.2）模板={c.entry_template or '无'} 关键位={f['key_level']:.2f}",
        ])

    # ------------------------------------------------------------

    @staticmethod
    def _c_liq(adv_usd: float) -> float:
        """流动性系数：20 日平均成交额映射 0.5-1.1（理论 §6.2）。"""
        if adv_usd >= 500_000_000: return 1.1
        if adv_usd >= 200_000_000: return 1.05
        if adv_usd >= 100_000_000: return 1.0
        if adv_usd >= 50_000_000: return 0.9
        if adv_usd >= 20_000_000: return 0.8
        return 0.5

    @classmethod
    def entry_template_for(cls, df: pd.DataFrame) -> str | None:
        """Check the TSS entry gate from OHLCV, or report unavailable features."""
        complete = df.dropna()
        if len(df) < cls.MIN_HISTORY or complete.empty:
            return None
        return cls._detect_template(extract_structure_features(complete), df)

    @staticmethod
    def _detect_template(f: dict, df: pd.DataFrame) -> str:
        flags = f["flags"]
        close = df["Close"]
        vol = df["Volume"]
        vol20 = vol.rolling(20).mean()
        c = f["close"]

        # 模板A：放量突破 + 缩量回踩不破 + 再收高
        if flags.get("volume_burst") and (flags.get("retest_ok") or flags.get("retest_shabby")):
            if flags.get("reclose_high") or c > f["key_level"]:
                return "A"
        # 模板B：波动收缩 + 当日放量突破 HHV20
        vc = f["vc"]
        today_burst = False
        if len(df) > 21:
            prev_hhv = float(df["High"].iloc[-21:-1].max())
            vr = float(vol.iloc[-1] / vol20.iloc[-1]) if vol20.iloc[-1] > 0 else 1.0
            today_burst = c > prev_hhv and vr >= 1.5
        if not math.isnan(vc) and vc <= 0.85 and (today_burst or flags.get("volume_burst")):
            return "B"
        # 模板C：均线多头 + 回撤至 10/20D 附近缩量企稳
        if c > f["sma20"] > 0 and f["sma50_rising"]:
            near_ma = min(abs(c / f["sma10"] - 1), abs(c / f["sma20"] - 1)) if f["sma10"] else 9
            recent_vol_shrink = float(vol.tail(5).mean() / vol20.iloc[-1]) <= 0.95 if len(vol20) else False
            if near_ma <= 0.03 and recent_vol_shrink:
                return "C"
        return ""

    @staticmethod
    def _stop_price(c: StockCandidate, f: dict) -> float:
        """结构化止损价（v6.0）——与 _stop_plan 文案同一来源，数值不再靠
        下游从中文文本正则解析（v4.0 '零文本解析'原则的补齐）。"""
        atr14 = f["atr14"] if not math.isnan(f["atr14"]) else f["close"] * 0.02
        if c.entry_template == "A":
            return round(f["key_level"] - 0.5 * atr14, 4)
        if c.entry_template == "B":
            return round(f["key_level"] - 1.0 * atr14, 4)
        if c.entry_template == "C":
            return round(min(f["sma20"], f["close"] - 1.5 * atr14), 4)
        return round(f["close"] - 2.0 * atr14, 4)

    @staticmethod
    def _stop_plan(c: StockCandidate, f: dict) -> str:
        atr14 = f["atr14"] if not math.isnan(f["atr14"]) else f["close"] * 0.02
        if c.entry_template == "A":
            stop = f["key_level"] - 0.5 * atr14
            return f"跌破关键位 {f['key_level']:.2f}（止损参考 {stop:.2f}）离场"
        if c.entry_template == "B":
            stop = f["key_level"] - 1.0 * atr14
            return f"收盘跌回区间（{f['key_level']:.2f}）下方且无法站回，止损参考 {stop:.2f}"
        if c.entry_template == "C":
            stop = min(f["sma20"], f["close"] - 1.5 * atr14)
            return f"有效跌破回撤低点/20D（止损参考 {stop:.2f}）离场"
        stop = f["close"] - 2.0 * atr14
        return f"结构不明，仅观察；若参与以 2ATR 止损（参考 {stop:.2f}）"
