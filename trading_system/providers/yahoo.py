"""Yahoo Finance 数据供应商（生产默认）。

依赖 yfinance（免费、无需 key）。
Yahoo / yfinance 的 ^TNX Close 已按百分数收益率发布
（5.136 表示 5.136%），与 DataProvider 契约一致，不再缩放。
"""

from __future__ import annotations

import logging

import pandas as pd

from .base import DataProvider

logger = logging.getLogger(__name__)


class YahooProvider(DataProvider):
    name = "yahoo"

    def __init__(self, *, keep_exchange_timezone: bool = False):
        # Strict execution records aware source instants before projecting them
        # to its admission market. Legacy callers retain yfinance's defaults.
        self.keep_exchange_timezone = keep_exchange_timezone

    def _download(self, ticker: str, days: int) -> pd.DataFrame:
        import yfinance as yf
        period = f"{max(days + 40, 300)}d"
        # v6.0 复权口径统一：auto_adjust=True（前复权 OHLC）。旧代码用未复权
        # 原始价——拆股在序列里就是一根假暴跌（1:4 拆股 = -75%），
        # 止损/指标/回测全坏，且与 stooq（复权）混用时同一面板口径不一致。
        timezone_options = {"ignore_tz": False} if self.keep_exchange_timezone else {}
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True,
                         **timezone_options)
        if df is None or len(df) == 0:
            raise ValueError(f"no data for {ticker}")
        df = self._normalize_ohlcv(df)
        return df.tail(days)

    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        return self._download(ticker, days)

    def ohlcv_batch(self, tickers: list[str], days: int = 400, *,
                    hard_deadline: float | None = None,
                    progress_callback=None) -> dict[str, pd.DataFrame]:
        import time
        import yfinance as yf
        out: dict[str, pd.DataFrame] = {}
        period = f"{max(days + 40, 300)}d"
        # yfinance 支持空格分隔批量下载，显著减少请求次数
        chunks = [tickers[i:i + 50] for i in range(0, len(tickers), 50)]
        consecutive_empty = 0
        t_start = time.time()
        timezone_options = {"ignore_tz": False} if self.keep_exchange_timezone else {}
        for chunk in chunks:
            if hard_deadline is not None and time.monotonic() >= hard_deadline:
                break
            # 墙钟保护：总预算 = 8s × chunk 数 + 120s，防止限流环境卡死整层
            if time.time() - t_start > 8 * len(chunks) + 120:
                logger.warning("yahoo 批量墙钟耗尽（%d/%d 只），提前返回部分结果",
                               len(out), len(tickers))
                break
            try:
                raw = yf.download(" ".join(chunk), period=period, progress=False,
                                  auto_adjust=True, group_by="ticker", threads=True,
                                  timeout=20, **timezone_options)
            except Exception as exc:
                logger.warning("batch download failed for chunk: %s", exc)
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    logger.warning("yahoo 连续 %d 个 chunk 失败，判定源级故障，"
                                   "提前退出让降级链接管", consecutive_empty)
                    break
                continue
            got = 0
            for t in chunk:
                try:
                    df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                    df = df.dropna()
                    if len(df) >= 60:
                        out[t] = self._normalize_ohlcv(df).tail(days)
                        got += 1
                except Exception:
                    continue
            consecutive_empty = 0 if got > 0 else consecutive_empty + 1
            if consecutive_empty >= 2:
                logger.warning("yahoo 连续 chunk 零产出，判定源级故障，提前退出")
                break
        return out

    def tnx_yield(self, days: int = 400) -> pd.Series:
        df = self._download("^TNX", days)
        return df["Close"].dropna()   # Yahoo 已是百分数收益率

    def vix(self, days: int = 400) -> pd.Series:
        return self._download("^VIX", days)["Close"].dropna()

    def vix9d(self, days: int = 400) -> pd.Series | None:
        try:
            return self._download("^VIX9D", days)["Close"].dropna()
        except Exception:
            return None

    def options_chain_snapshot(self, ticker: str) -> dict | None:
        """真实期权链（yfinance）：最近两个到期日合并计算 PCR / 平值 IV。"""
        import yfinance as yf
        try:
            tk = yf.Ticker(ticker)
            expirations = tk.options
            if not expirations:
                return None
            calls_l, puts_l = [], []
            for exp in expirations[:2]:          # 最近两个到期，兼顾流动性
                try:
                    chain = tk.option_chain(exp)
                    calls_l.append(chain.calls)
                    puts_l.append(chain.puts)
                except Exception:
                    continue
            if not calls_l:
                return None
            calls = pd.concat(calls_l, ignore_index=True)
            puts = pd.concat(puts_l, ignore_index=True)
            call_oi = float(calls["openInterest"].fillna(0).sum())
            put_oi = float(puts["openInterest"].fillna(0).sum())
            call_vol = float(calls["volume"].fillna(0).sum())
            put_vol = float(puts["volume"].fillna(0).sum())
            # 平值 IV：现价 ±5% 内合约的 IV 加权均值
            try:
                px = float(tk.fast_info.get("lastPrice"))
            except Exception:
                px = float(self.ohlcv(ticker, days=5)["Close"].iloc[-1])
            near = calls[(calls["strike"] - px).abs() <= px * 0.05]
            atm_iv = float(near["impliedVolatility"].mean()) if len(near) else float("nan")
            return {
                "pcr_oi": put_oi / call_oi if call_oi > 0 else float("nan"),
                "pcr_vol": put_vol / call_vol if call_vol > 0 else float("nan"),
                "atm_iv": atm_iv,
                "call_oi": call_oi, "put_oi": put_oi,
                "expiry": str(expirations[0]),
            }
        except Exception as exc:
            logger.debug("options snapshot failed for %s: %s", ticker, exc)
            return None

    def quote(self, ticker: str) -> dict | None:
        """盘中报价保留源端市场时间；无可验证时间时回退日线收盘价。

        fast_info 与 get_history_metadata 使用同一 Ticker 的行情缓存；
        regularMarketTime 是源端成交时间，不能用本机取数时间代替。
        回退路径 kind="eod_close" 如实披露，不把旧价重新盖章成实时价。
        """
        import math
        from datetime import datetime
        from numbers import Integral, Real

        import yfinance as yf
        try:
            tk = yf.Ticker(ticker)
            px = tk.fast_info.get("lastPrice", None)
            if (isinstance(px, bool) or not isinstance(px, Real)
                    or not math.isfinite(px) or px <= 0):
                raise ValueError("fast_info.lastPrice 缺失或无效")
            source_time = tk.get_history_metadata().get("regularMarketTime")
            # yfinance 元数据格式化后为带时区 Timestamp；未格式化时为 epoch 秒。
            if isinstance(source_time, Integral) and not isinstance(source_time, bool):
                if source_time <= 0:
                    raise ValueError("regularMarketTime 无效")
                quoted_at = pd.Timestamp(int(source_time), unit="s", tz="UTC")
            elif isinstance(source_time, datetime):
                quoted_at = pd.Timestamp(source_time)
            else:
                raise ValueError("regularMarketTime 缺失或格式不可验证")
            if pd.isna(quoted_at) or quoted_at.tzinfo is None:
                raise ValueError("regularMarketTime 缺失时区或无效")
            return {"price": float(px),
                    "ts": quoted_at.isoformat(),
                    "kind": "realtime"}
        except Exception as exc:
            logger.debug("Yahoo 报价缺有效价格或源时间 %s（%s），回退日线收盘价",
                         ticker, type(exc).__name__)
            return super().quote(ticker)
