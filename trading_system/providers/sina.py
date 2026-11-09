"""新浪财经数据供应商（v6.2 多源扩展 · 中国免费源，无需 key）。

接口（已实测全通）：
  日 K  stock.finance.sina.com.cn/usstock/api/jsonp.php/x/
        US_MinKService.getDailyK?symbol={小写代码}
        JSONP 包裹，全历史（1984 起），字段 d/o/h/l/c/v
  实时  hq.sinajs.cn/list=gb_{小写代码}（GBK，需 Referer 头）

重要：新浪美股 K 线【未复权】（实测 AAPL 2020 拆股 499→129 硬跳变）——
本 provider 对每段历史调用 _adjust_splits 做拆股检测+前复权，
调整事件记日志披露。另外新浪末根可能滞后 1 个交易日（T+1 更新），
新鲜度由 pipeline 的 v6.1 闸门统一校验披露。

定位：降级链中国免费环（yahoo → stooq → tencent → sina → eastmoney →
agentgw → ifind_gw → tiingo）。TNX/VIX 不承接，显式抛错走降级。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import re

import pandas as pd
from curl_cffi import requests as curl_requests

from .base import DataProvider
from .cn_native import cn_native

logger = logging.getLogger(__name__)

_KLINE = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/x/"
          "US_MinKService.getDailyK?symbol={sym}")
_QUOTE = "https://hq.sinajs.cn/list=gb_{sym}"
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
       "Referer": "https://finance.sina.com.cn"}


class SinaProvider(DataProvider):
    name = "sina"
    BATCH_WORKERS = 4

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    @staticmethod
    def available() -> bool:
        return True            # 无需 key；调用失败由降级链处理

    def _get(self, url: str, encoding: str = "utf-8") -> str:
        response = curl_requests.get(url, headers=_UA, timeout=self.timeout,
                                     impersonate="chrome")
        response.raise_for_status()
        return response.content.decode(encoding, errors="replace")

    # ---------------------------------------------------------------- 接口
    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        # A股分支（v3.3）：CN_MarketDataService（日 K scale=240，真实实测可达）
        native = cn_native(ticker)
        if native:
            return self._ohlcv_cn(native, days)
        text = self._get(_KLINE.format(sym=ticker.lower()))
        m = re.search(r"\((\[.*\])\)", text, re.S)
        if not m:
            raise RuntimeError(f"新浪 K 线解析失败: {ticker}（返回非 JSONP）")
        rows = json.loads(m.group(1))
        if not rows:
            raise RuntimeError(f"新浪无数据: {ticker}")
        df = pd.DataFrame({
            "Date": [r["d"] for r in rows],
            "Open": [float(r["o"]) for r in rows],
            "High": [float(r["h"]) for r in rows],
            "Low": [float(r["l"]) for r in rows],
            "Close": [float(r["c"]) for r in rows],
            "Volume": [float(r.get("v") or 0) for r in rows],
        })
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        df, events = self._adjust_splits(df)     # 未复权源：拆股硬跳变必须处理
        if events:
            logger.warning("新浪 %s 拆股复权: %s", ticker, events)
        df = self._normalize_ohlcv(df).tail(days)
        if len(df) < max(30, days // 3):
            raise RuntimeError(f"新浪 {ticker} 历史不足: {len(df)} 行")
        return df

    def ohlcv_batch(self, tickers: list[str], days: int = 400) -> dict[str, pd.DataFrame]:
        """Fill the capability fallback with a bounded, deterministic batch."""
        requested = list(dict.fromkeys(tickers))
        if not requested:
            return {}
        result: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=min(self.BATCH_WORKERS, len(requested)),
                                thread_name_prefix="sina-batch") as pool:
            futures = {pool.submit(self.ohlcv, ticker, days): ticker for ticker in requested}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    frame = future.result()
                except Exception:
                    continue
                if frame is not None and not frame.empty:
                    result[ticker] = frame
        return {ticker: result[ticker] for ticker in requested if ticker in result}

    _CN_KLINE = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/x/"
                 "CN_MarketDataService.getKLineData"
                 "?symbol={native}&scale=240&ma=no&datalen={n}")
    _CN_QUOTE = "https://hq.sinajs.cn/list={native}"

    def _ohlcv_cn(self, native: str, days: int) -> pd.DataFrame:
        url = self._CN_KLINE.format(native=native, n=min(days + 20, 1023))
        response = curl_requests.get(
            url, headers={**_UA, "Referer": "https://finance.sina.com.cn"},
            timeout=self.timeout, impersonate="chrome")
        response.raise_for_status()
        text = response.content.decode("utf-8", errors="replace")
        m = re.search(r"\((\[.*\])\)", text, re.S)
        if not m:
            raise RuntimeError(f"新浪 A股 K 线解析失败: {native}（返回非 JSONP）")
        rows = json.loads(m.group(1))
        if not rows:
            raise RuntimeError(f"新浪 A股无数据: {native}")
        df = pd.DataFrame({
            "Date": [r["day"] for r in rows],
            "Open": [float(r["open"]) for r in rows],
            "High": [float(r["high"]) for r in rows],
            "Low": [float(r["low"]) for r in rows],
            "Close": [float(r["close"]) for r in rows],
            "Volume": [float(r.get("volume") or 0) for r in rows],
        })
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        df = self._normalize_ohlcv(df).tail(days)
        if len(df) < max(30, days // 3):
            raise RuntimeError(f"新浪 A股 {native} 历史不足: {len(df)} 行")
        return df

    def quote(self, ticker: str) -> dict | None:
        """新浪实时行情（美股/A股，kind=realtime）。"""
        # A股分支：f[3]=最新价（指数/个股一致）
        native = cn_native(ticker)
        if native:
            try:
                response = curl_requests.get(
                    self._CN_QUOTE.format(native=native),
                    headers={**_UA, "Referer": "https://finance.sina.com.cn"},
                    timeout=self.timeout, impersonate="chrome")
                response.raise_for_status()
                text = response.content.decode("gbk", errors="replace")
                m = re.search(r'="(.*)"', text)
                if not m or not m.group(1).strip():
                    raise ValueError(f"新浪 A股无报价: {ticker}")
                f = m.group(1).split(",")
                price = float(f[3])
                if price <= 0:
                    raise ValueError("price=0")
                return {"price": price, "ts": "", "kind": "realtime"}
            except Exception as exc:
                logger.debug("新浪 A股报价失败 %s: %s", ticker, exc)
                return super().quote(ticker)
        try:
            text = self._get(_QUOTE.format(sym=ticker.lower()), encoding="gbk")
            m = re.search(r'="(.*)"', text)
            if not m or not m.group(1).strip():
                raise ValueError(f"新浪无报价: {ticker}")
            f = m.group(1).split(",")
            # [0]名称 [1]现价 [2]涨幅% [3]时间 [4]涨跌 [5]开 [6]高 [7]低
            price = float(f[1])
            if price <= 0:
                raise ValueError("price=0")
            return {"price": price, "ts": f[3] if len(f) > 3 else "",
                    "kind": "realtime"}
        except Exception as exc:
            logger.debug("新浪报价失败 %s: %s", ticker, exc)
            return super().quote(ticker)

    # 利率/波动率本通道不承接——显式抛错走降级
    def tnx_yield(self, days: int = 400) -> pd.Series:
        raise RuntimeError("sina 不承接利率序列（^TNX），请走 yahoo/stooq/agentgw")

    def vix(self, days: int = 400) -> pd.Series:
        raise RuntimeError("sina 不承接波动率指数（^VIX），请走 yahoo/stooq/agentgw")
