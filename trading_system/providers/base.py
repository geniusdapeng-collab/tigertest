"""数据供应商接口。

所有 provider 必须实现以下方法，返回统一的 pandas.DataFrame（OHLCV）。
索引为日期，列名为 Open/High/Low/Close/Volume。

yield 类数据（TNX）统一约定为【百分数收益率】（如 4.25 表示 4.25%），
由 provider 按上游实际单位归一化；Yahoo ^TNX Close 已是百分数。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import time

import pandas as pd


class DataProvider(ABC):
    name: str = "base"
    # 产出是否为【合成/演示】数据（demo 及其子类）。生产台账（公开验证台账、
    # 期权历史库、治理事件账）一律禁止写入合成数据；判定只看本标记，
    # 不看 name 字符串——测试用的 DemoProvider 子类常改 name（如 "onestale-demo"），
    # 用 name 比对会被绕过（v6.5 修复）。
    synthetic: bool = False

    @abstractmethod
    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        """单标的 OHLCV，至少 days 个交易日的历史。"""

    def ohlcv_batch(self, tickers: list[str], days: int = 400, *,
                    hard_deadline: float | None = None,
                    progress_callback=None) -> dict[str, pd.DataFrame]:
        """批量拉取（默认逐个调用，子类可优化为并发）。"""
        out: dict[str, pd.DataFrame] = {}
        for t in tickers:
            if hard_deadline is not None and time.monotonic() >= hard_deadline:
                break
            try:
                df = self.ohlcv(t, days=days)
                if df is not None and len(df) > 0:
                    out[t] = df
            except Exception:
                continue
            if callable(progress_callback):
                progress_callback({"attempted": len(out), "completed": len(out),
                                   "failed": 0, "timed_out": 0})
        return out

    @abstractmethod
    def tnx_yield(self, days: int = 400) -> pd.Series:
        """10 年期美债收益率序列（百分数，如 4.25）。"""

    @abstractmethod
    def vix(self, days: int = 400) -> pd.Series:
        """VIX 收盘序列。"""

    # ---- S4 多市场基准组通用接口（市场规格 benchmarks 的消费口）----
    # 默认实现：US 符号（TNX/VIX/VIX9D）走既有专用方法（行为零变化）；
    # 其他市场符号尝试按 OHLCV 收盘价序列取（免费源不可得时抛错/None，
    # 由调用方按市场策略记缺失走"剔除再归一化"）。
    def rate_yield_for(self, symbol: str, days: int = 400) -> pd.Series:
        """10Y 利率序列（百分数）。symbol="TNX" 等同 tnx_yield()。"""
        if symbol.upper() == "TNX":
            return self.tnx_yield(days)
        df = self.ohlcv(symbol, days=days)
        if df is None or not len(df):
            raise RuntimeError(f"{self.name} 无法提供利率序列 {symbol}")
        return df["Close"]

    def vol_index_for(self, symbol: str, days: int = 400) -> pd.Series | None:
        """波动率指数序列。symbol="VIX"/"VIX9D" 等同 vix()/vix9d()。"""
        if symbol.upper() == "VIX":
            return self.vix(days)
        if symbol.upper() == "VIX9D":
            return self.vix9d(days)
        df = self.ohlcv(symbol, days=days)
        if df is None or not len(df):
            raise RuntimeError(f"{self.name} 无法提供波动率序列 {symbol}")
        return df["Close"]

    def vix9d(self, days: int = 400) -> pd.Series | None:
        """VIX9D（可选，缺失时返回 None → 期限结构子项中性 5 分）。"""
        return None

    # ---- 期权/微观结构（免费源通常缺失，返回 None → 中性 5 分 + evidence）----
    def gex_billions(self) -> float | None:
        return None

    def zero_dte_share(self) -> float | None:
        return None

    def options_chain_snapshot(self, ticker: str) -> dict | None:
        """最近到期期权链快照（真实数据，Yahoo 可实现）。

        返回 dict：
          pcr_oi   — Put/Call 持仓量比
          pcr_vol  — Put/Call 成交量比
          atm_iv   — 平值隐含波动率（小数，如 0.35）
          call_oi  — Call 总持仓
          put_oi   — Put 总持仓
          expiry   — 使用的到期日
        不支持/失败返回 None（期权维度走中性降级 + 缺失披露）。
        """
        return None

    def quote(self, ticker: str) -> dict | None:
        """最新报价（盘中触发器用）。

        返回 dict：
          price — 价格
          ts    — 报价时间戳
          kind  — "realtime"（盘中实时/延时报价）或 "eod_close"（日线收盘价）。
                  基类默认实现只能拿到【最近一根日线收盘价】，可能是上一交易日的
                  陈旧价格，盘中触发器必须按 kind 披露，不得冒充实时价。
        """
        try:
            df = self.ohlcv(ticker, days=5)
            if df is not None and len(df):
                return {"price": float(df["Close"].iloc[-1]),
                        "ts": str(df.index[-1].date()),
                        "kind": "eod_close"}
        except Exception:
            pass
        return None

    # ---- 基本面修正（免费源通常缺失）----
    def eps_revision_up_pct(self, sector_etf: str) -> float | None:
        return None

    def guidance_up_pct(self, sector_etf: str) -> float | None:
        return None

    @staticmethod
    def _adjust_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
        """未复权美股的拆股/并股检测与前复权（v6.2）。

        新浪/腾讯的美股日 K 是【未复权】原始价——拆股在序列里就是一根假暴跌
        （AAPL 2020-08-31 收 499→129，1:4 拆股 = 表面 -74%）。本方法反向扫描，
        日价格比命中常见拆股比例（2/3/4/5/6/8/10 或其倒数，容差 6%）时，
        将历史段 OHLC 按比例复权、Volume 反向调整。
        容差取 6%：拆股生效当天本身有 ±3-5% 真实波动（实测 AAPL 1:4 拆股日
        比值 3.87）；而相邻整数倍的间距 ≥10%（1:3 vs 1:4），6% 不会混淆。

        返回 (复权后 df, 调整事件列表——调用方应记日志披露)。
        误判防护：仅当比值落在整数比例 ±2.5% 内才调整；大盘股真实单日恰好
        整数倍波动的概率极低，且事件全量披露可审计。
        """
        import logging
        log = logging.getLogger(__name__)
        events: list[str] = []
        if df is None or len(df) < 2:
            return df, events
        df = df.copy()
        close = df["Close"].values
        factors: list[tuple[int, float]] = []        # (切分点 i, 历史段乘数)
        for i in range(len(close) - 1, 0, -1):
            prev, cur = close[i - 1], close[i]
            if prev <= 0 or cur <= 0:
                continue
            ratio = prev / cur
            for n in (2, 3, 4, 5, 6, 8, 10):
                if abs(ratio - n) / n <= 0.06:                # 正向拆股 1:N
                    factors.append((i, 1.0 / n))
                    events.append(f"{df.index[i].date()} 疑似 1:{n} 拆股（{prev:.2f}→{cur:.2f}），已前复权")
                    break
                if abs(ratio - 1.0 / n) * n <= 0.06:          # 并股 N:1
                    factors.append((i, float(n)))
                    events.append(f"{df.index[i].date()} 疑似 {n}:1 并股（{prev:.2f}→{cur:.2f}），已前复权")
                    break
        for i, f in factors:
            df.iloc[:i, df.columns.get_indexer(["Open", "High", "Low", "Close"])] *= f
            df.iloc[:i, df.columns.get_loc("Volume")] /= f
        for e in events:
            log.info("split-adjust: %s", e)
        return df, events

    @staticmethod
    def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
        """统一列名与索引。"""
        df = df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        rename = {c: c.capitalize() for c in df.columns}
        df = df.rename(columns=rename)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        return df


# ============================================================
# 数据来源判定（v6.5）：合成/演示数据不得写入任何生产台账
# ============================================================

def is_synthetic_name(name: object) -> bool:
    """按 provider 名判定合成数据（历史口径兜底："demo" / "*-demo"）。"""
    lowered = str(name or "").lower()
    return lowered == "demo" or lowered.endswith("-demo")


def is_synthetic(provider: object) -> bool:
    """统一判定：该 provider 的产出是否属于合成/演示数据（台账写入的唯一准入判据）。

    优先级：显式 `synthetic` 标记 > 历史 name 兜底（"demo" / "*-demo"）。
    """
    flag = getattr(provider, "synthetic", None)
    if isinstance(flag, bool):
        return flag
    return is_synthetic_name(getattr(provider, "name", ""))


def source_label(name: object) -> str:
    """台账里记录的数据来源标签：合成数据统一归一为 "demo"（下游闸门据此识别）。"""
    return "demo" if is_synthetic_name(name) else str(name or "")
