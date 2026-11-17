"""iFinD 服务端通道（经 agent-gw 转发同花顺 iFinD 数据源）—— 数据命脉第四环。

与 AgentGwProvider（yahoo_finance 服务端）互为独立数据源：一个挂了另一个顶上，
请求均在服务端发出，不受本地出口限流影响。

定位：降级链第四环（yahoo → stooq → agentgw → ifind_gw），主补【个股】历史日线。
约束（服务端规则）：
  - 单次最多 3 只、区间最长 3 年（覆盖 420 交易日需求）；
  - 美股代码需交易所后缀：纳斯达克 .O ｜ 纽交所 .N ｜ 美交所 .A.N ｜ BATS .B.N
    （后缀自动探测并本轮缓存）；
  - 宏观指数（^TNX/^VIX）与 ETF 覆盖不稳定 → 本通道只承接个股，
    指数/ETF 仍由 agentgw(yahoo_finance) 通道兜底。
"""

from __future__ import annotations

import io
import json
import logging
import time
from datetime import datetime, timedelta

import pandas as pd

from .base import DataProvider

log = logging.getLogger(__name__)

_API = "ifind_get_price"
_DS = "ifind"
_SUFFIXES = (".O", ".N", ".A.N", ".B.N")     # 探测顺序：纳斯达克→纽交所→美交所→BATS


class IfindGwProvider(DataProvider):
    name = "ifind_gw"

    def __init__(self, batch_budget_s: float = 240.0):
        from agent_gw import AgentGwClient, ToolsAPI
        self._api = ToolsAPI(AgentGwClient())
        self.batch_budget_s = batch_budget_s
        self._cache: dict[str, pd.DataFrame] = {}
        self._suffix: dict[str, str] = {}           # 本轮交易所后缀缓存

    @staticmethod
    def available() -> bool:
        try:
            from agent_gw import AgentGwClient
            AgentGwClient()
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- 内部
    def _query(self, ifind_ticker: str, start: str, end: str) -> pd.DataFrame:
        resp = self._api.call_data_source_tool({
            "data_source_name": _DS,
            "api_name": _API,
            "params": {"ticker": ifind_ticker, "start_date": start, "end_date": end,
                       "interval": "D", "adjust": "qfq",  # v6.0 前复权，口径与 yahoo/stooq 统一
                       "file_path": f"/tmp/_ifind_{ifind_ticker}.csv"},
        })
        if not getattr(resp, "is_success", False):
            raise RuntimeError(f"ifind 拉取 {ifind_ticker} 失败: "
                               f"{str(getattr(resp, 'error', ''))[:120]}")
        payload = json.loads(resp.text)
        csv_text = payload.get("data_preview") or ""
        if not csv_text.strip():
            raise RuntimeError(f"ifind 返回空数据: {ifind_ticker}")
        df = pd.read_csv(io.StringIO(csv_text))
        df["Date"] = pd.to_datetime(df["time"].astype(str), format="mixed")
        df = df.set_index("Date")
        df = df.rename(columns={c: c.capitalize() for c in df.columns})
        return self._normalize_ohlcv(df[["Open", "High", "Low", "Close", "Volume"]])

    def _fetch(self, ticker: str, days: int) -> pd.DataFrame:
        # 缓存充足性按【本次请求的 days】判定（v5.4 修复：旧判定 min(days,240)
        # 会把 240 行短缓存冒充 460 日历史返回，252 日窗口指标全部失真）。
        if ticker in self._cache and len(self._cache[ticker]) >= days:
            return self._cache[ticker]
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=min(int(days * 1.6) + 30, 1000))).strftime("%Y-%m-%d")
        candidates = ([self._suffix[ticker]] if ticker in self._suffix
                      else [f"{ticker}{s}" for s in _SUFFIXES])
        last_err: Exception | None = None
        for cand in candidates:
            try:
                df = self._query(cand, start, end)
                self._suffix[ticker] = cand
                self._cache[ticker] = df
                return df
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f"ifind 全后缀探测失败 {ticker}: {last_err}")

    # ---------------------------------------------------------------- 接口
    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        df = self._fetch(ticker, days)
        if len(df) < max(30, days // 3):
            raise RuntimeError(f"ifind {ticker} 历史不足: {len(df)} 行")
        return df

    def ohlcv_batch(self, tickers: list[str], days: int = 400, *,
                    hard_deadline: float | None = None,
                    progress_callback=None) -> dict[str, pd.DataFrame]:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        out: dict[str, pd.DataFrame] = {}
        started = time.time()
        pool = ThreadPoolExecutor(max_workers=3)   # 服务端单请求≤3只，并发取 3 路
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
                    log.info("ifind 拉取 %s 失败: %s", t, e)
                if time.time() - started > self.batch_budget_s:
                    break
        except Exception:
            pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if len(out) < len(tickers):
            log.warning("ifind 批量完成 %d/%d", len(out), len(tickers))
        return out

    # 指数/利率本通道不承接（由 agentgw yahoo_finance 通道兜底）——显式抛错走降级
    def tnx_yield(self, days: int = 400) -> pd.Series:
        raise RuntimeError("ifind_gw 不承接利率序列（^TNX），请走 agentgw 通道")

    def vix(self, days: int = 400) -> pd.Series:
        raise RuntimeError("ifind_gw 不承接波动率指数（^VIX），请走 agentgw 通道")
