"""AgentGW 数据供应商 — 服务端行情通道（限流根修）。

根因背景：直连 yfinance 是从本地出口 IP 抓 Yahoo 网页接口，共享出口 IP
极易触发 YFRateLimitError；stooq 走本地出口 HTTP，受限网络下大面积超时。
本 provider 经 agent-gw 服务端调用 yahoo_finance 数据源的
get_historical_stock_prices——请求在服务端发出，不经过本地出口 IP，
从根本上不受本地限流/出口超时影响（已实测 SPY/^TNX/^VIX 全通）。

定位：降级链第三环（yahoo → stooq → agentgw），也可 --provider agentgw 指定。
约束：单次最长 2 年日线（服务端限制），覆盖本系统 420 个交易日需求。

启用条件：agent_gw SDK 可导入且凭证可用（KIMI_API_KEY 或 ~/.kimi/agent-gw.json）。
"""

from __future__ import annotations

import io
import json
import logging
import time

import pandas as pd

from .base import DataProvider

log = logging.getLogger(__name__)

_API = "get_historical_stock_prices"
_DS = "yahoo_finance"


class AgentGwProvider(DataProvider):
    name = "agentgw"

    def __init__(self, per_call_timeout: float = 40.0,
                 batch_budget_s: float = 240.0, pause_s: float = 0.4):
        from agent_gw import AgentGwClient, ToolsAPI   # 延迟导入：无 SDK 环境不炸
        self._api = ToolsAPI(AgentGwClient())
        self.per_call_timeout = per_call_timeout
        self.batch_budget_s = batch_budget_s
        self.pause_s = pause_s
        self._cache: dict[str, pd.DataFrame] = {}       # 本轮内复用（零基线：不落盘）

    # ---------------------------------------------------------------- 可用性
    @staticmethod
    def available() -> bool:
        try:
            from agent_gw import AgentGwClient  # noqa: F401
            AgentGwClient()
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- 底层抓取
    def _fetch(self, ticker: str, days: int) -> pd.DataFrame:
        # 缓存充足性按【本次请求的 days】判定：full 模式两级拉取下，若预筛先以
        # 短历史入缓存，重量级拉取必须重新取数——否则 252 日窗口指标（RS 分位、
        # SMA200、广度）在短历史上失真，且血缘仍记为正常（v5.4 修复）。
        if ticker in self._cache and len(self._cache[ticker]) >= days:
            return self._cache[ticker]
        period = "2y" if days > 252 else "1y"
        resp = self._api.call_data_source_tool({
            "data_source_name": _DS,
            "api_name": _API,
            "params": {"ticker": ticker, "period": period, "interval": "1d",
                       "file_path": f"/tmp/_agentgw_{ticker.strip('^')}.csv"},
        })
        if not getattr(resp, "is_success", False):
            raise RuntimeError(f"agent-gw 拉取 {ticker} 失败: {getattr(resp, 'text', '')[:160]}")
        payload = json.loads(resp.text)
        csv_text = payload.get("data_preview") or ""
        if not csv_text.strip():
            raise RuntimeError(f"agent-gw 返回空数据: {ticker}")
        df = pd.read_csv(io.StringIO(csv_text))
        df["Date"] = pd.to_datetime(df["Date"], utc=True).dt.tz_localize(None)
        df = df.set_index("Date")
        if "Volume" not in df.columns:
            df["Volume"] = 0.0
        df = self._normalize_ohlcv(df[["Open", "High", "Low", "Close", "Volume"]])
        self._cache[ticker] = df
        return df

    # ---------------------------------------------------------------- 接口实现
    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        df = self._fetch(ticker, days)
        if len(df) < max(30, days // 3):
            raise RuntimeError(f"agent-gw {ticker} 历史不足: {len(df)} 行")
        return df

    def ohlcv_batch(self, tickers: list[str], days: int = 400, *,
                    hard_deadline: float | None = None,
                    progress_callback=None) -> dict[str, pd.DataFrame]:
        """并发批量（服务端通道逐只一次调用，4 路并发 + 总墙钟预算）。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        out: dict[str, pd.DataFrame] = {}
        started = time.time()
        pool = ThreadPoolExecutor(max_workers=4)
        futs = {pool.submit(self._fetch, t, days): t for t in tickers}
        try:
            timeout = self.batch_budget_s
            if hard_deadline is not None:
                timeout = min(timeout, max(0.0, hard_deadline - time.monotonic()))
            for fut in as_completed(futs, timeout=timeout):
                t = futs[fut]
                try:
                    df = fut.result()
                    if df is not None and len(df):
                        out[t] = df
                except Exception as e:
                    log.info("agentgw.ohlcv_batch 拉取 %s 失败（%s）", t, type(e).__name__)
                if time.time() - started > self.batch_budget_s:
                    break
        except Exception:
            pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if len(out) < len(tickers):
            log.warning("agentgw 批量完成 %d/%d（墙钟 %.0fs）",
                        len(out), len(tickers), time.time() - started)
        return out

    def tnx_yield(self, days: int = 400) -> pd.Series:
        s = self._fetch("^TNX", days)["Close"].dropna()
        # 保留网关历史口径兼容：部分通道曾返回收益率×10（42.5=4.25%）；
        # Yahoo/yfinance 直接返回百分数。此处仅对网关 >20 的旧口径还原。
        if len(s) and float(s.median()) > 20.0:
            s = s / 10.0
        return s

    def vix(self, days: int = 400) -> pd.Series:
        return self._fetch("^VIX", days)["Close"].dropna()

    def vix9d(self, days: int = 400) -> pd.Series | None:
        try:
            return self._fetch("^VIX9D", days)["Close"].dropna()
        except Exception:
            return None
