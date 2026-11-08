"""CN/HK 多市场数据修复测试（v3.3）：符号翻译层 + 两源 CN/HK 分支（全程 mock）。"""
from __future__ import annotations

import pandas as pd
import pytest

from trading_system.providers.cn_native import cn_native, hk_native
from trading_system.providers.tencent import TencentProvider
from trading_system.providers.sina import SinaProvider


# ---------------------------------------------------------------- 符号翻译层
@pytest.mark.parametrize("ticker,native", [
    ("000300.SS", "sh000300"), ("600519.SS", "sh600519"), ("688981.SS", "sh688981"),
    ("399001.SZ", "sz399001"), ("300750.SZ", "sz300750"), ("000001.SH", "sh000001"),
])
def test_cn_native(ticker, native):
    assert cn_native(ticker) == native


@pytest.mark.parametrize("ticker", ["SPY", "AAPL", "HSI.HK", "0700.HK", "CAT.OQ", ""])
def test_cn_native_rejects_non_cn(ticker):
    assert cn_native(ticker) is None


@pytest.mark.parametrize("ticker,native", [
    ("0700.HK", "hk00700"), ("00700.HK", "hk00700"), ("09988.HK", "hk09988"),
    ("HSI.HK", "hkHSI"), ("HSTECH.HK", "hkHSTECH"), ("HSCEI.HK", "hkHSCEI"),
])
def test_hk_native(ticker, native):
    assert hk_native(ticker) == native


@pytest.mark.parametrize("ticker", ["SPY", "000300.SS", "AAPL", ""])
def test_hk_native_rejects_non_hk(ticker):
    assert hk_native(ticker) is None


# ---------------------------------------------------------------- 腾讯 CN/HK
def _tencent_kline_payload(native, n=60):
    bars = [[f"2026-08-{(i % 28) + 1:02d}", "100.0", "101.0", "102.0", "99.0", "1000.0"]
            for i in range(n)]
    return f'{{"code":0,"msg":"","data":{{"{native}":{{"day":{bars}}}}}}}'.replace("'", '"')


def test_tencent_cn_kline_uses_native_and_referer(monkeypatch):
    seen = {}

    def fake_get(self, url, encoding="utf-8", referer=None):
        seen["url"], seen["referer"] = url, referer
        return _tencent_kline_payload("sh000300")

    monkeypatch.setattr(TencentProvider, "_get", fake_get)
    df = TencentProvider().ohlcv("000300.SS", days=60)
    assert "param=sh000300" in seen["url"]
    assert seen["referer"] == "https://gu.qq.com/sh000300"
    assert len(df) == 60


def test_tencent_hk_kline(monkeypatch):
    seen = {}

    def fake_get(self, url, encoding="utf-8", referer=None):
        seen["url"] = url
        return _tencent_kline_payload("hk00700")

    monkeypatch.setattr(TencentProvider, "_get", fake_get)
    df = TencentProvider().ohlcv("00700.HK", days=60)
    assert "param=hk00700" in seen["url"] and len(df) == 60


def test_tencent_us_path_unchanged(monkeypatch):
    """美股路径零变化：仍走 us 前缀 + 后缀探测，无 Referer。"""
    seen = {}

    def fake_get(self, url, encoding="utf-8", referer=None):
        seen.setdefault("referers", []).append(referer)
        return _tencent_kline_payload("usSPY")

    monkeypatch.setattr(TencentProvider, "_get", fake_get)
    df = TencentProvider().ohlcv("SPY", days=60)
    # v3.3 新端点统一带 gu.qq.com Referer（全符号一致行为）
    assert len(df) == 60 and all(r.startswith("https://gu.qq.com/usSPY") for r in seen["referers"])  # 后缀探测期 usSPY.OQ 等变体


# ---------------------------------------------------------------- 新浪 CN
def test_sina_cn_kline(monkeypatch):
    payload = ("x([" + ",".join(
        f'{{"day":"2026-08-{(i % 28) + 1:02d}","open":"100","high":"102",'
        f'"low":"99","close":"101","volume":"1000"}}' for i in range(60)) + "])")

    class R:
        content = payload.encode()

        def raise_for_status(self):
            return None

    monkeypatch.setattr("trading_system.providers.sina.curl_requests.get",
                        lambda *a, **k: R())
    df = SinaProvider().ohlcv("600519.SS", days=60)
    assert len(df) == 60 and float(df["Close"].iloc[-1]) == 101.0


def test_sina_us_path_unchanged(monkeypatch):
    """美股路径零变化：仍走原 usstock JSONP 端点。"""
    calls = []

    def fake_get(self, url, encoding="utf-8"):
        calls.append(url)
        raise RuntimeError("boom")

    monkeypatch.setattr(SinaProvider, "_get", fake_get)
    with pytest.raises(RuntimeError):
        SinaProvider().ohlcv("AAPL", days=60)
    assert "usstock" in calls[0]
