"""Stooq 数据供应商（yahoo 不可用时的降级，免费 CSV 接口）。

stooq 代码规则：美股为小写 + .us（spy.us）；^TNX 无，用 10USY.B 美债收益率；
VIX 用 vix 指数（可能不稳定，缺失时抛错由上层降级处理）。

v5.4 修复：
1. stooq 已上线 JS 反爬墙（SHA-256 PoW 质询）：直接 urlopen 拿到的是
   质询 HTML 而非 CSV，旧代码 pd.read_csv 后以 KeyError: 'Date' 崩溃。
   本 provider 现在：自动解 PoW 质询（成功环境下 24h cookie 复用），
   并校验响应确为 CSV——质询页/"Access denied" 一律抛清晰错误，
   让降级链立即接管，绝不把 HTML 当行情解析。
2. 新增 quote()：stooq 延时报价端点（盘中 ~15 分钟延时），
   拿不到时回退基类日线收盘价（kind="eod_close" 如实披露）。
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
from collections import Counter

import pandas as pd

from .base import DataProvider

logger = logging.getLogger(__name__)

_BASE = "https://stooq.com/q/d/l/?s={symbol}&i=d"
_LIVE = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
_VERIFY = "https://stooq.com/__verify"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_CHALLENGE_RE = re.compile(r'const c="([A-Za-z0-9+/=]+)",d=(\d+)')


class _StooqSession:
    """带反爬质询求解的 HTTP 会话（无 requests 依赖时退化为 urllib）。"""

    def __init__(self, timeout: int = 20, pow_max: int = 8_000_000,
                 *, allow_challenge: bool = True):
        self.timeout = timeout
        self.pow_max = pow_max
        self.allow_challenge = allow_challenge
        self._cookies: dict[str, str] = {}
        self._verified_until = 0.0

    # ------------------------------------------------------------
    def _solve_pow(self, challenge: str, difficulty: int) -> int | None:
        target = "0" * difficulty
        for n in range(self.pow_max):
            if hashlib.sha256(f"{challenge}{n}".encode()).hexdigest().startswith(target):
                return n
        return None

    def _verify(self, html: str) -> bool:
        m = _CHALLENGE_RE.search(html)
        if not m:
            return False
        c, d = m.group(1), int(m.group(2))
        n = self._solve_pow(c, d)
        if n is None:
            logger.warning("stooq PoW 质询超出求解上限（d=%d）", d)
            return False
        text = self._request("POST", _VERIFY, data={"c": c, "n": n})
        if text is not None and "auth" in self._cookies:
            self._verified_until = time.time() + 23 * 3600
            logger.info("stooq 反爬质询已通过（cookie 24h 内复用）")
            return True
        return False

    # ------------------------------------------------------------
    def _request(self, method: str, url: str, data: dict | None = None) -> str | None:
        """底层请求（优先 requests，缺失时 urllib），带回 cookie。"""
        try:
            import requests
            sess = requests.Session()
            sess.headers["User-Agent"] = _UA
            for k, v in self._cookies.items():
                sess.cookies.set(k, v, domain="stooq.com")
            resp = sess.request(method, url, data=data, timeout=self.timeout)
            self._cookies.update(sess.cookies.get_dict())
            return resp.text
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("stooq 请求异常 %s: %s", url, exc)
            return None
        # urllib 退化路径（无 cookie 持久化，质询环境下会失败并清晰抛错）
        import urllib.request
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA},
            data=(None if data is None
                  else "&".join(f"{k}={v}" for k, v in data.items()).encode()),
            method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("stooq 请求异常 %s: %s", url, exc)
            return None

    def get_text(self, url: str) -> str:
        """GET 并保证返回的是数据而非反爬质询页。"""
        text = self._request("GET", url)
        if text is None:
            raise RuntimeError(f"stooq 网络请求失败: {url}")
        if "Date," in text[:200] or "T,D,O,H,L,C,V" in text[:200]:
            return text
        if _CHALLENGE_RE.search(text):
            if not self.allow_challenge:
                raise RuntimeError("Stooq requires access verification; strict public policy refuses the challenge")
            if time.time() < self._verified_until or self._verify(text):
                text = self._request("GET", url) or ""
                if "Date," in text[:200] or "T,D,O,H,L,C,V" in text[:200]:
                    return text
            raise RuntimeError(
                "stooq 反爬质询未能解除（数据中心 IP 可能被拒），请走降级链下一环")
        raise RuntimeError(f"stooq 返回非 CSV 内容（可能被封禁）: {text[:60]!r}")


class StooqProvider(DataProvider):
    name = "stooq"

    def __init__(self, *, allow_challenge: bool = True,
                 max_consecutive_no_output: int | None = None):
        if (max_consecutive_no_output is not None and
                (type(max_consecutive_no_output) is not int or max_consecutive_no_output < 1)):
            raise ValueError("Stooq batch availability budget must be a positive integer or None")
        self._http = _StooqSession(allow_challenge=allow_challenge)
        self.max_consecutive_no_output = max_consecutive_no_output
        self.last_batch_status: dict | None = None

    def _fetch(self, symbol: str, days: int) -> pd.DataFrame:
        text = self._http.get_text(_BASE.format(symbol=symbol))
        df = pd.read_csv(io.StringIO(text))
        if df.empty or "Date" not in df.columns:
            raise ValueError(f"stooq empty: {symbol}")
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        return self._normalize_ohlcv(df).tail(days)

    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        return self._fetch(ticker.lower() + ".us", days)

    def ohlcv_batch(self, tickers: list[str], days: int = 400) -> dict[str, pd.DataFrame]:
        """Bound strict serial no-output probes, then let the existing chain fill gaps.

        Exhaustion means this batch's availability budget was consumed. Ordinary
        missing symbols are not proof that the remote service failed. No price
        is synthesized and already observed frames remain unchanged.
        """
        from ..redline.execution import EvidenceWriteError, ExecutionContractError, StrictObservationError
        symbols = [ticker for ticker in tickers if isinstance(ticker, str) and ticker.strip()]
        status = {"schema": "tiger.source-availability.v1", "provider": self.name,
                  "requested": len(tickers), "skipped_empty_symbols": len(tickers) - len(symbols),
                  "attempted": 0, "returned": 0, "empty_observations": 0,
                  "consecutive_no_output": 0, "limit": self.max_consecutive_no_output,
                  "source_exhausted": False, "service_failure_asserted": False,
                  "reason": "request_completed" if symbols else "empty_request",
                  "unattempted_symbols": [], "errors": {}}
        self.last_batch_status = status
        out, errors = {}, Counter()
        for index, ticker in enumerate(symbols):
            status["attempted"] += 1
            try:
                frame = self.ohlcv(ticker, days=days)
            except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                # Execution/observation/evidence faults must poison the strict
                # stage rather than masquerade as a public-source outage.
                raise
            except Exception as exc:
                errors[type(exc).__name__] += 1
                frame = None
            valid = (isinstance(frame, pd.DataFrame) and not frame.empty
                     and not frame.dropna(how="all").empty)
            if valid:
                out[ticker] = frame
                status["consecutive_no_output"] = 0
            else:
                status["empty_observations"] += 1
                status["consecutive_no_output"] += 1
            status.update(returned=len(out), errors=dict(errors))
            if (self.max_consecutive_no_output is not None
                    and status["consecutive_no_output"] >= self.max_consecutive_no_output
                    and index + 1 < len(symbols)):
                status.update(source_exhausted=True, reason="availability_budget_exhausted",
                              unattempted_symbols=symbols[index + 1:])
                logger.warning("stooq 批次连续 %d 次无有效观测，耗尽本源可用性探测预算；"
                               "已尝试 %d/%d，其余交原可信回退链（不判定远端服务失败）",
                               status["consecutive_no_output"], status["attempted"], len(symbols))
                break
        return out

    def tnx_yield(self, days: int = 400) -> pd.Series:
        # stooq 10 年期美债收益率（百分数）
        df = self._fetch("10usy.b", days)
        return df["Close"].dropna()

    def vix(self, days: int = 400) -> pd.Series:
        try:
            return self._fetch("vix", days)["Close"].dropna()
        except Exception:
            # 备用符号
            return self._fetch("^vix", days)["Close"].dropna()

    def quote(self, ticker: str) -> dict | None:
        """stooq 延时报价（盘中约 15 分钟延时，kind="realtime" 标注为延时报价）。

        端点不可用时回退基类日线收盘价（kind="eod_close"）。
        """
        try:
            text = self._http.get_text(_LIVE.format(symbol=ticker.lower() + ".us"))
            df = pd.read_csv(io.StringIO(text))
            row = df.iloc[0]
            close = float(row["Close"])
            if close != close or close <= 0:      # NaN / N/D
                raise ValueError("stooq 延时报价无效")
            date, tm = str(row.get("Date", "")), str(row.get("Time", ""))
            return {"price": close, "ts": f"{date} {tm}".strip(),
                    "kind": "realtime_delayed"}
        except Exception as exc:
            logger.debug("stooq 延时报价失败 %s: %s", ticker, exc)
            return super().quote(ticker)
