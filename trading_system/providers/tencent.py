"""腾讯行情数据供应商（v6.2 多源扩展 · 中国免费源，无需 key）。

接口（已实测全通）：
  日 K  web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=us{代码}{后缀},day,,,{n},qfq
        后缀：纳斯达克 .OQ ｜ 纽交所 .N ｜ ETF 可无后缀（自动探测并本轮缓存）
        返回 bar = [date, open, close, high, low, volume]（注意开收顺序）
  实时  qt.gtimg.cn/q=us{代码}（GBK，~ 分隔，第 4 字段为现价）

重要：腾讯美股 K 线【未复权】（实测 AAPL 2020 拆股为硬跳变）——本 provider
对每段历史调用 _adjust_splits 做拆股检测+前复权，调整事件记日志披露。

定位：降级链中国免费环（yahoo → stooq → tencent → sina → eastmoney →
agentgw → ifind_gw → tiingo）。TNX/VIX 本通道不承接（腾讯 VIX 数据陈旧，
实测时间戳停滞），显式抛错走降级。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import re
from threading import Lock
import time

import pandas as pd
from curl_cffi import requests as curl_requests

from .base import DataProvider
from .cn_native import cn_native, hk_native

logger = logging.getLogger(__name__)

# v3.3：新端点为主（proxy.finance.qq.com 全符号可达，含 HK/CN）；
# 旧端点（web.ifzq.gtimg.cn）保留兜底——2026-08-30 实测旧端点对 HK 符号整体 501。
_KLINE_NEW = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
              "?param={native},day,,,{n},qfq")
_KLINE_OLD = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
              "?param={native},day,,,{n},qfq")
_QUOTE = "https://qt.gtimg.cn/q={native}"
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
_SUFFIXES = (".OQ", ".N", ".AM", "")   # 探测：纳斯达克 → 纽交所 → 美交所/ARCA(ETF) → 无后缀
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_SYMBOL_DIRECTORY_URLS = (
    ("https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt", "nasdaq"),
    ("https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt", "other"),
)
_EXCHANGE_SUFFIX = {"N": ".N", "A": ".AM", "P": ".AM", "Z": ".AM", "V": ".AM"}
_exchange_cache: tuple[float, dict[str, str]] | None = None
_exchange_lock = Lock()


class TencentProvider(DataProvider):
    name = "tencent"
    BATCH_WORKERS = 4

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self._suffix: dict[str, str] = {}
        self._exchange: dict[str, str] | None = None

    @staticmethod
    def available() -> bool:
        return True            # 无需 key；调用失败由降级链处理

    # ---------------------------------------------------------------- 内部
    def _get(self, url: str, encoding: str = "utf-8", referer: str | None = None) -> str:
        headers = dict(_UA)
        if referer:
            headers["Referer"] = referer
        response = curl_requests.get(url, headers=headers, timeout=self.timeout,
                                     impersonate="chrome")
        response.raise_for_status()
        return response.content.decode(encoding, errors="replace")

    def _load_exchange_map(self) -> dict[str, str]:
        global _exchange_cache
        if self._exchange is not None:
            return self._exchange
        with _exchange_lock:
            if _exchange_cache is None or time.time() - _exchange_cache[0] >= 7 * 86400:
                symbols: dict[str, str] = {}
                try:
                    for url, kind in _SYMBOL_DIRECTORY_URLS:
                        response = curl_requests.get(
                            url,
                            headers={**_UA, "Referer": "https://www.nasdaqtrader.com/"},
                            timeout=max(self.timeout, 30), impersonate="chrome")
                        response.raise_for_status()
                        text = response.content.decode("utf-8", errors="replace")
                        for line in text.splitlines()[1:]:
                            if line.startswith("File Creation Time") or "|" not in line:
                                continue
                            fields = line.split("|")
                            if kind == "nasdaq":
                                if len(fields) < 4 or fields[3].strip() != "N":
                                    continue
                                symbol, suffix = fields[0].strip(), ".OQ"
                            else:
                                if len(fields) < 8 or fields[6].strip() != "N":
                                    continue
                                suffix = _EXCHANGE_SUFFIX.get(fields[2].strip().upper())
                                if suffix is None:
                                    continue
                                symbol = fields[0].strip()
                                alternate = fields[7].strip()
                                if alternate and alternate != symbol:
                                    symbols[alternate] = suffix
                            if symbol:
                                symbols[symbol] = suffix
                except Exception as exc:
                    logger.warning("腾讯交易所映射加载失败，回退后缀探测: %s", exc)
                _exchange_cache = (time.time(), symbols)
            self._exchange = _exchange_cache[1]
        return self._exchange

    def _fetch_kline(self, sym: str, days: int, native: str | None = None) -> pd.DataFrame:
        """native 为完整源符号（usSPY.OQ / sh000300 / hk00700）；
        CN/HK 必须带 Referer（gu.qq.com），否则反爬跳转（v3.3 实测）。"""
        native = native or f"us{sym}"
        referer = f"https://gu.qq.com/{native}"
        try:
            text = self._get(_KLINE_NEW.format(native=native, n=min(days + 20, 800)),
                             referer=referer)
        except Exception as e:
            logger.warning("腾讯新端点失败（%s），尝试旧端点: %s", native, e)
            text = self._get(_KLINE_OLD.format(native=native, n=min(days + 20, 800)),
                             referer=referer)
        blob = json.loads(text)
        if blob.get("code") != 0:
            raise RuntimeError(f"腾讯 K 线错误: {blob.get('msg', '')[:80]}")
        data = blob.get("data") or {}
        node = data.get(native) or (list(data.values())[0] if data else None)
        if not node:
            raise RuntimeError(f"腾讯无数据: {native}")
        bars = node.get("qfqday") or node.get("day") or []
        if not bars:
            raise RuntimeError(f"腾讯 K 线为空: us{sym}")
        # bar = [date, open, close, high, low, volume]（腾讯开/收顺序与常规相反）
        df = pd.DataFrame({
            "Date": [b[0] for b in bars],
            "Open": [float(b[1]) for b in bars],
            "Close": [float(b[2]) for b in bars],
            "High": [float(b[3]) for b in bars],
            "Low": [float(b[4]) for b in bars],
            "Volume": [float(b[5]) if len(b) > 5 else 0.0 for b in bars],
        })
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        df, events = self._adjust_splits(df)     # 未复权源：拆股硬跳变必须处理
        if events:
            logger.warning("腾讯 %s 拆股复权: %s", sym, events)
        return self._normalize_ohlcv(df).tail(days)

    def _fetch(self, ticker: str, days: int) -> pd.DataFrame:
        # CN/HK 原生符号直达（v3.3）：不做美股后缀探测
        native = cn_native(ticker) or hk_native(ticker)
        if native:
            df = self._fetch_kline(ticker, days, native=native)
            if len(df) < min(days, 50):
                raise RuntimeError(f"腾讯 {native} 数据不足（{len(df)} 行）")
            return df
        suffix = self._suffix.get(ticker)
        if suffix is None:
            try:
                suffix = self._load_exchange_map().get(ticker)
            except Exception as exc:
                logger.warning("腾讯交易所映射加载失败，回退后缀探测: %s", exc)
        if suffix:
            cands = [f"{ticker}{suffix}",
                     *(f"{ticker}{s}" for s in _SUFFIXES if s != suffix)]
        else:
            cands = [f"{ticker}{s}" for s in _SUFFIXES]
        last_err: Exception | None = None
        for cand in cands:
            try:
                df = self._fetch_kline(cand, days)
                # 腾讯对【错误后缀】不报错而是返回仅当天 1 行的占位数据
                # （实测 usCAT.OQ 如此）——探测必须要求行数充足，
                # 否则会把 1 行占位当成功并缓存错误后缀
                if len(df) < min(days, 50):
                    raise RuntimeError(f"占位数据（{len(df)} 行），后缀错误")
                self._suffix[ticker] = cand
                return df
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f"腾讯全后缀探测失败 {ticker}: {last_err}")

    # ---------------------------------------------------------------- 接口
    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        df = self._fetch(ticker, days)
        if len(df) < max(30, days // 3):
            raise RuntimeError(f"腾讯 {ticker} 历史不足: {len(df)} 行")
        return df

    def ohlcv_batch(self, tickers: list[str], days: int = 400) -> dict[str, pd.DataFrame]:
        """Fetch independent symbols concurrently while preserving input order."""
        requested = list(dict.fromkeys(tickers))
        if not requested:
            return {}
        # Resolve the official directory once on the owner thread. Doing this
        # inside every worker would serialize retries behind the module lock
        # and turn one metadata failure into a long tail.
        self._load_exchange_map()
        result: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=min(self.BATCH_WORKERS, len(requested)),
                                thread_name_prefix="tencent-batch") as pool:
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

    def quote(self, ticker: str) -> dict | None:
        """腾讯实时行情（美股/沪深/港股，kind=realtime）。"""
        try:
            native = cn_native(ticker) or hk_native(ticker) or f"us{ticker}"
            text = self._get(_QUOTE.format(native=native), encoding="gbk")
            m = re.search(r'="(.*)"', text)
            if not m:
                raise ValueError("empty")
            f = m.group(1).split("~")
            if len(f) < 6 or f[0] == "pv_none_match":
                raise ValueError(f"腾讯无报价: {ticker}")
            price = float(f[3])
            if price <= 0:
                raise ValueError("price=0")
            ts_m = _TS_RE.search(m.group(1))
            return {"price": price,
                    "ts": ts_m.group(0) if ts_m else "",
                    "kind": "realtime"}
        except Exception as exc:
            logger.debug("腾讯报价失败 %s: %s", ticker, exc)
            return super().quote(ticker)

    # 利率/波动率本通道不承接（腾讯 VIX 数据陈旧，实测时间戳停滞）——显式抛错走降级
    def tnx_yield(self, days: int = 400) -> pd.Series:
        raise RuntimeError("tencent 不承接利率序列（^TNX），请走 yahoo/stooq/agentgw")

    def vix(self, days: int = 400) -> pd.Series:
        raise RuntimeError("tencent 的 VIX 数据陈旧不可用（实测时间戳停滞），请走降级链")
