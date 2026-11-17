"""Tiingo 数据供应商（官方 API，需 TIINGO_API_KEY 环境变量）—— 可选强化环。

官方 REST API（非抓取），请求额度与限流以当前账户套餐为准。
未配置 key 时 available()=False，自动从降级链剔除，不影响任何现有流程。

EOD 使用源端完整的复权 OHLCV（拆股与分红），与策略的复权历史输入一致。
字段契约：https://www.tiingo.com/documentation/end-of-day
配置方式：export TIINGO_API_KEY=xxxx
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.request
from datetime import datetime, timedelta
from numbers import Real

import pandas as pd

from .base import DataProvider

log = logging.getLogger(__name__)

_BASE = "https://api.tiingo.com/tiingo/daily/{t}/prices"
_ADJUSTED_COLUMNS = {"adjOpen": "Open", "adjHigh": "High", "adjLow": "Low",
                     "adjClose": "Close", "adjVolume": "Volume"}


class TiingoProvider(DataProvider):
    name = "tiingo"

    def __init__(self, batch_budget_s: float = 240.0):
        self.key = os.environ.get("TIINGO_API_KEY", "")
        if not self.key:
            raise RuntimeError("未配置 TIINGO_API_KEY")
        self.batch_budget_s = batch_budget_s
        self._cache: dict[str, pd.DataFrame] = {}

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("TIINGO_API_KEY"))

    @classmethod
    def _adjusted_frame(cls, rows: object, ticker: str) -> pd.DataFrame:
        """只接纳完整、有效的源端复权序列；坏行使整标的失败，不能静默删行。"""
        if not isinstance(rows, list):
            raise RuntimeError(f"tiingo 响应格式无效: {ticker}")
        if not rows:
            raise RuntimeError(f"tiingo 空数据: {ticker}")
        dates: list[str] = []
        bars: list[dict] = []
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                raise RuntimeError(f"tiingo 行格式无效: {ticker} 第 {i + 1} 行")
            date = row.get("date")
            if not isinstance(date, str) or not date.strip():
                raise RuntimeError(f"tiingo 日期缺失或无效: {ticker} 第 {i + 1} 行")
            dates.append(date)
            bar = {}
            for source, column in _ADJUSTED_COLUMNS.items():
                value = row.get(source)
                try:
                    valid = (not isinstance(value, bool) and isinstance(value, Real)
                             and math.isfinite(value)
                             and (value >= 0 if column == "Volume" else value > 0))
                except (TypeError, ValueError, OverflowError):
                    valid = False
                if not valid:
                    raise RuntimeError(
                        f"tiingo 复权字段 {source} 缺失或无效: {ticker} 第 {i + 1} 行")
                bar[column] = value
            if not (bar["Low"] <= bar["Open"] <= bar["High"]
                    and bar["Low"] <= bar["Close"] <= bar["High"]):
                raise RuntimeError(f"tiingo 复权 OHLC 范围无效: {ticker} 第 {i + 1} 行")
            bars.append(bar)
        index = pd.to_datetime(dates, utc=True, errors="coerce").tz_localize(None)
        if index.isna().any() or index.has_duplicates:
            raise RuntimeError(f"tiingo 日期无效或重复: {ticker}")
        # adj* 已由 Tiingo 统一复权，不能用 raw 字段补洞，也不二次猜测拆股。
        return cls._normalize_ohlcv(pd.DataFrame(bars, index=index.rename("Date")))

    def _fetch(self, ticker: str, days: int) -> pd.DataFrame:
        # 缓存充足性按【本次请求的 days】判定（v5.4 修复：旧代码不看长度，
        # full 模式预筛的 30 日短历史会被重量级拉取直接复用，指标全面失真）。
        if ticker in self._cache and len(self._cache[ticker]) >= days:
            return self._cache[ticker].copy()
        start = (datetime.now() - timedelta(days=min(int(days * 1.6) + 30, 1000))
                 ).strftime("%Y-%m-%d")
        url = f"{_BASE.format(t=ticker)}?startDate={start}&token={self.key}"
        req = urllib.request.Request(url, headers={"User-Agent": "caishen-ai/5.4"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        out = self._adjusted_frame(rows, ticker)
        # 整批验证成功后才替换缓存；调用方修改返回值也不能污染已验证的序列。
        self._cache[ticker] = out
        return out.copy()

    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        df = self._fetch(ticker, days)
        if len(df) < max(30, days // 3):
            raise RuntimeError(f"tiingo {ticker} 历史不足: {len(df)} 行")
        return df

    def ohlcv_batch(self, tickers: list[str], days: int = 400, *,
                    hard_deadline: float | None = None,
                    progress_callback=None) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        started = time.time()
        for t in tickers:
            if time.time() - started > self.batch_budget_s:
                log.warning("tiingo 批量墙钟耗尽 %d/%d", len(out), len(tickers))
                break
            if hard_deadline is not None and time.monotonic() >= hard_deadline:
                break
            try:
                df = self._fetch(t, days)
                if len(df):
                    out[t] = df
            except Exception as e:
                log.info("tiingo 拉取 %s 失败: %s", t, e)
            time.sleep(0.15)   # 请求间隔；不承诺覆盖账户的全部频率/额度限制
        return out

    def tnx_yield(self, days: int = 400) -> pd.Series:
        raise RuntimeError("tiingo 不提供利率序列，走 agentgw 通道")

    def vix(self, days: int = 400) -> pd.Series:
        raise RuntimeError("tiingo 不提供 VIX，走 agentgw 通道")
