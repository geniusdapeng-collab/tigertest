"""MC025: provider failures disclose source/type, never untrusted error bodies.

All strings below are non-secret sentinels. Providers, search and LLM calls are
local fixtures; sockets are blocked and no SDK credentials are loaded.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
import socket
import traceback
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system import pipeline
from trading_system.providers.agentgw import AgentGwProvider
from trading_system.providers.demo import DemoProvider
from trading_system.redline import LLMUnavailable


MARKER = "MC025_NON_SECRET_SENTINEL"
ERROR_TEXTS = [
    f"token={MARKER}",
    f"https://fixture.invalid/data?api_key={MARKER}",
    f'{{"remote_response": "{MARKER}"}}',
    f"Authorization: Bearer {MARKER}\nFORGED LOG ENTRY",
    f"上游异常：{MARKER}\x1b[31m",
    MARKER + "x" * 20_000,
]


def _frame(value=100.0, days=3):
    return pd.DataFrame({
        "Open": [value] * days, "High": [value + 1] * days,
        "Low": [value - 1] * days, "Close": [value] * days,
        "Volume": [1000.0] * days,
    }, index=pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days))


class _Provider:
    synthetic = False

    def __init__(self, name, *, result=None, error=None, calls=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = calls if calls is not None else []

    def _call(self, method, target, days):
        self.calls.append((self.name, method, target, days))
        if self.error is not None:
            raise self.error
        return self.result

    def ohlcv(self, ticker, days=400):
        return self._call("ohlcv", ticker, days)

    def rate_yield_for(self, symbol, days=400):
        return self._call("rate_yield_for", symbol, days)

    def vol_index_for(self, symbol, days=400):
        return self._call("vol_index_for", symbol, days)

    def ohlcv_batch(self, tickers, days=400, *, hard_deadline=None, progress_callback=None):
        return self._call("ohlcv_batch", list(tickers), days)

    def quote(self, ticker):
        return self._call("quote", ticker, None)


class _NoStringError(RuntimeError):
    def __str__(self):
        raise AssertionError("Untrusted exception must never be formatted")


@pytest.fixture(autouse=True)
def _offline_boundaries(monkeypatch, tmp_path, caplog):
    # Other collected suites may configure the root logger above WARNING.
    # Make these disclosure assertions independent of collection order.
    caplog.set_level(logging.WARNING)

    def reject_network(*args, **kwargs):
        pytest.fail("MC025 fixtures must not access a network")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "getaddrinfo", reject_network)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline.config, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(pipeline, "_LINEAGE", [])
    monkeypatch.setattr(pipeline, "_HEALTH", {})
    monkeypatch.setattr(pipeline, "_DATA_ISSUES", [])
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [])
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider",
                        lambda: _Provider("stooq", result={}))


def _assert_no_body(caplog, error=None):
    surfaces = [caplog.text, json.dumps(pipeline._DATA_ISSUES)]
    if error is not None:
        surfaces.extend([str(error), repr(error), repr(vars(error)),
                         "".join(traceback.format_exception(error))])
    assert all(MARKER not in surface for surface in surfaces)
    assert "FORGED LOG ENTRY" not in caplog.text
    for record in caplog.records:
        assert record.exc_info is None
        assert not any(isinstance(arg, BaseException) for arg in record.args)


@pytest.mark.parametrize("method", ["ohlcv", "rate_yield_for", "vol_index_for"])
@pytest.mark.parametrize("text", ERROR_TEXTS,
                         ids=["token", "url", "json", "multiline", "unicode", "long"])
def test_single_fallback_logs_only_error_type(monkeypatch, caplog, method, text):
    frame = _frame()
    calls = []
    primary = _Provider("primary", error=TimeoutError(text), calls=calls)
    fallback = _Provider("fallback", result=frame, calls=calls)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [fallback])

    assert pipeline._single_with_fallback(primary, method, "A", days=30) is frame

    _assert_no_body(caplog)
    assert "primary" in caplog.text and method in caplog.text
    assert "TimeoutError" in caplog.text and "fallback" in caplog.text
    assert calls == [("primary", method, "A", 30), ("fallback", method, "A", 30)]
    assert pipeline._LINEAGE == [(f"{method}(A)", "fallback")]
    assert pipeline._HEALTH["primary"]["fail"] == 1
    assert pipeline._HEALTH["fallback"]["ok"] == 1
    assert pipeline._DATA_ISSUES == [{
        "provider": "primary", "synthetic": False, "method": method,
        "symbols": ["A"], "issue": "provider_error", "error_type": "TimeoutError",
    }]


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_exception_string_conversion_is_never_invoked(monkeypatch, caplog, batch):
    frame = _frame()
    primary = _Provider("stooq", error=_NoStringError(MARKER))
    fallback = _Provider("fallback", result={"A": frame} if batch else frame)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [fallback])
    out = (pipeline._batch_with_fallback(primary, ["A"], 30)["A"] if batch else
           pipeline._single_with_fallback(primary, "ohlcv", "A", days=30))
    assert out is frame
    _assert_no_body(caplog)
    assert "_NoStringError" in caplog.text


def test_single_multiple_failures_identify_actual_source(monkeypatch, caplog):
    calls = []
    primary = _Provider("yahoo", error=TimeoutError(MARKER), calls=calls)
    middle = _Provider("stooq", error=ValueError(MARKER), calls=calls)
    frame = _frame()
    final = _Provider("last", result=frame, calls=calls)
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", lambda: middle)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [final])

    assert pipeline._single_with_fallback(primary, "ohlcv", "A", days=30) is frame

    _assert_no_body(caplog)
    messages = [record.getMessage() for record in caplog.records]
    assert any("yahoo.ohlcv" in m and "TimeoutError" in m for m in messages)
    assert any("stooq.ohlcv" in m and "ValueError" in m for m in messages)
    assert [call[0] for call in calls] == ["yahoo", "stooq", "last"]
    assert [(i["provider"], i["error_type"]) for i in pipeline._DATA_ISSUES] == [
        ("yahoo", "TimeoutError"), ("stooq", "ValueError")]
    assert {name: (h["ok"], h["fail"]) for name, h in pipeline._HEALTH.items()} == {
        "yahoo": (0, 1), "stooq": (0, 1), "last": (1, 0)}


@pytest.mark.parametrize("error_class", [RuntimeError, ValueError, PermissionError,
                                        TimeoutError, _NoStringError])
@pytest.mark.parametrize("fallback", [False, True], ids=["primary-only", "all-failed"])
def test_total_single_failure_has_no_original_exception_chain(
        monkeypatch, caplog, error_class, fallback):
    error = error_class(MARKER)
    error.__cause__ = ValueError(MARKER + " cause")
    error.add_note(MARKER + " note")
    primary = _Provider("primary", error=error)
    last = _Provider("last", error=error)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [last] if fallback else [])

    with pytest.raises(Exception) as caught:
        pipeline._single_with_fallback(primary, "ohlcv", "A", days=30)

    safe = caught.value
    assert type(safe).__name__ == "ProviderDataError"
    assert isinstance(safe, RuntimeError)
    assert safe.provider == ("last" if fallback else "primary")
    assert safe.method == "ohlcv" and safe.error_type == error_class.__name__
    assert safe.__cause__ is None and safe.__context__ is None
    _assert_no_body(caplog, safe)
    assert pipeline._LINEAGE == []
    assert len(pipeline._DATA_ISSUES) == (2 if fallback else 1)
    assert all(issue["error_type"] == error_class.__name__
               for issue in pipeline._DATA_ISSUES)


@pytest.mark.parametrize("text", ERROR_TEXTS,
                         ids=["token", "url", "json", "multiline", "unicode", "long"])
def test_batch_failure_logs_only_error_type(monkeypatch, caplog, text):
    primary = _Provider("stooq", error=PermissionError(text))
    frame = _frame()
    fallback = _Provider("last", result={"A": frame})
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [fallback])

    assert pipeline._batch_with_fallback(primary, ["A"], 30)["A"] is frame

    _assert_no_body(caplog)
    assert "stooq" in caplog.text and "ohlcv_batch" in caplog.text
    assert "PermissionError" in caplog.text
    assert pipeline._DATA_ISSUES[0]["error_type"] == "PermissionError"


def test_batch_middle_failure_preserves_partial_data_order_and_missing(monkeypatch, caplog):
    calls = []
    a, b, c = _frame(101), _frame(102), _frame(103)
    primary = _Provider("yahoo", result={"A": a}, calls=calls)
    stooq = _Provider("stooq", error=TimeoutError(MARKER), calls=calls)
    middle = _Provider("middle", result={"A": _frame(999), "B": b}, calls=calls)
    last = _Provider("last", result={"C": c, "UNREQUESTED": _frame()}, calls=calls)
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", lambda: stooq)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [middle, last])

    result = pipeline._batch_with_fallback(primary, list("ABCD"), 30, "stocks")

    assert result == {"A": a, "B": b, "C": c}
    assert result["A"] is a and result["B"] is b and result["C"] is c
    assert calls == [("yahoo", "ohlcv_batch", list("ABCD"), 30),
                     ("stooq", "ohlcv_batch", list("BCD"), 30),
                     ("middle", "ohlcv_batch", list("BCD"), 30),
                     ("last", "ohlcv_batch", list("CD"), 30)]
    assert pipeline._LINEAGE == [("stocks[1/4]", "yahoo"),
                                 ("stocks+补1只", "middle"), ("stocks+补1只", "last")]
    assert pipeline._HEALTH == {}  # Batch health accounting is unchanged.
    assert pipeline._DATA_ISSUES[-1]["issue"] == "batch_missing"
    assert pipeline._DATA_ISSUES[-1]["symbols"] == ["D"]
    _assert_no_body(caplog)


def test_batch_total_failure_keeps_explicit_unavailable_disclosure(monkeypatch, caplog):
    primary = _Provider("stooq", error=RuntimeError(MARKER))
    last = _Provider("last", error=ValueError(MARKER))
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [last])
    assert pipeline._batch_with_fallback(primary, ["A", "B"], 30, "stocks") == {}
    assert pipeline._DATA_ISSUES[-1] == {
        "provider": "stooq", "synthetic": False, "method": "ohlcv_batch",
        "symbols": ["A", "B"], "issue": "batch_unavailable", "label": "stocks",
    }
    assert [(i["provider"], i["error_type"]) for i in pipeline._DATA_ISSUES
            if i["issue"] == "provider_error"] == [
        ("stooq", "RuntimeError"), ("last", "ValueError")]
    _assert_no_body(caplog)


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_normal_primary_success_preserves_values_and_does_not_probe(monkeypatch, batch):
    def unexpected_chain():
        pytest.fail("Successful primary data must keep the existing short circuit")

    monkeypatch.setattr(pipeline, "_channel_chain", unexpected_chain)
    frame = _frame()
    value = {t: frame for t in "ABC"} if batch else frame
    primary = _Provider("yahoo", result=value)
    out = (pipeline._batch_with_fallback(primary, list("ABC"), 30) if batch else
           pipeline._single_with_fallback(primary, "ohlcv", "A", days=30))
    assert (all(out[t] is frame for t in "ABC") if batch else out is frame)
    assert len(primary.calls) == 1 and pipeline._DATA_ISSUES == []
    assert pipeline._LINEAGE == ([("batch[3/3]", "yahoo")] if batch else
                                  [("ohlcv(A)", "yahoo")])


def test_quote_exception_remains_safe_and_falls_back(monkeypatch, caplog):
    primary = _Provider("stooq", error=RuntimeError(MARKER))
    quote = {"price": 100.0, "ts": pd.Timestamp.now().isoformat(), "kind": "realtime"}
    monkeypatch.setattr(pipeline, "_channel_chain",
                        lambda: [_Provider("last", result=quote)])
    assert pipeline.quote_with_fallback(primary, "A") is quote
    assert pipeline._DATA_ISSUES[0]["error_type"] == "RuntimeError"
    _assert_no_body(caplog)


class _OfflineLLM:
    def complete_json(self, **kwargs):
        raise LLMUnavailable("MC025 offline fixture")


class _EmptySearch:
    def __init__(self, *, demo):
        self.demo = demo

    def register_topic_set(self, *args, **kwargs):
        return None

    def gather_topic_sets(self, **kwargs):
        return []

    def active_topics(self):
        return {}


def _pipeline_fixtures(monkeypatch, provider):
    monkeypatch.setattr(pipeline, "get_provider", lambda name=None: provider)
    monkeypatch.setattr(pipeline, "default_client", _OfflineLLM)
    monkeypatch.setattr(pipeline, "SearchHub", _EmptySearch)
    monkeypatch.setattr(pipeline, "load_universe", lambda *args: ["A"])
    monkeypatch.setattr("trading_system.universe.load_market_universe",
                        lambda *args: (["A"], "fixture"))


@pytest.mark.parametrize("failed_method", ["ohlcv", "rate_yield_for", "vol_index_for"])
def test_hard_benchmark_failure_wrapper_has_only_safe_cause(monkeypatch, caplog, failed_method):
    class BenchmarkProvider(_Provider):
        def _call(self, method, target, days):
            if method == failed_method:
                raise RuntimeError(MARKER)
            return _frame(days=days)

    provider = BenchmarkProvider("primary")
    _pipeline_fixtures(monkeypatch, provider)

    with pytest.raises(RuntimeError, match="MRS 无法计算") as caught:
        pipeline.run_pipeline(universe_mode="core")

    _assert_no_body(caplog, caught.value)
    safe = caught.value.__cause__
    assert type(safe).__name__ == "ProviderDataError"
    assert safe.provider == "primary" and safe.method == failed_method
    assert safe.error_type == "RuntimeError"
    assert safe.__cause__ is None and safe.__context__ is None


@pytest.mark.parametrize("market", ["cn", "hk"])
def test_soft_macro_failures_still_reach_next_stage_without_body(monkeypatch, caplog, market):
    class MacroUnavailable(DemoProvider):
        name = "primary"

        def rate_yield_for(self, symbol, days=400):
            raise TimeoutError(MARKER)

        def vol_index_for(self, symbol, days=400):
            raise PermissionError(MARKER)

    class DataPrepared(Exception):
        pass

    def stop_after_data(*args, **kwargs):
        raise DataPrepared

    _pipeline_fixtures(monkeypatch, MacroUnavailable())
    monkeypatch.setattr(pipeline, "TechChainMonitorAgent",
                        lambda provider: SimpleNamespace(execute=stop_after_data))
    with pytest.raises(DataPrepared):
        pipeline.run_pipeline(universe_mode="core", market=market)
    _assert_no_body(caplog)
    assert "宏观维缺失再归一化" in caplog.text
    assert "情绪维缺失再归一化" in caplog.text
    assert {i["method"] for i in pipeline._DATA_ISSUES} >= {
        "rate_yield_for", "vol_index_for"}


def test_completed_report_keeps_lineage_health_and_safe_data_issues(monkeypatch, caplog):
    class FailingDemo(DemoProvider):
        name = "primary"

        def __init__(self):
            super().__init__()
            self.single_failed = self.batch_failed = False

        def ohlcv(self, ticker, days=400):
            if ticker == "SPY" and not self.single_failed:
                self.single_failed = True
                raise TimeoutError(MARKER)
            return super().ohlcv(ticker, days)

        def ohlcv_batch(self, tickers, days=400, *, hard_deadline=None, progress_callback=None):
            if not self.batch_failed:
                self.batch_failed = True
                raise PermissionError(MARKER)
            return super().ohlcv_batch(tickers, days, hard_deadline=hard_deadline,
                                       progress_callback=progress_callback)

    _pipeline_fixtures(monkeypatch, FailingDemo())
    backup = DemoProvider()
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [backup])
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", lambda: backup)
    result = pipeline.run_pipeline(universe_mode="core", top_n=5, max_picks=2)

    _assert_no_body(caplog)
    assert MARKER not in json.dumps(asdict(result), default=str)
    assert result.provider == "demo" and result.raw["synthetic"] is True
    assert all(source == "demo" for _, source in result.raw["source_lineage"])
    assert result.raw["provider_health"]["primary"]["fail"] == 1
    assert result.raw["provider_health"]["demo"]["ok"] == 1
    assert {(i["method"], i["error_type"]) for i in result.raw["data_issues"]} == {
        ("ohlcv", "TimeoutError"), ("ohlcv_batch", "PermissionError")}
    assert len(result.raw["redline"]) == 21 and result.mrs is not None


def _agentgw(response=None, error=None):
    provider = AgentGwProvider.__new__(AgentGwProvider)  # Never construct an SDK client.
    provider._cache = {}
    provider.per_call_timeout, provider.batch_budget_s, provider.pause_s = 40.0, 240.0, 0.0
    calls = []

    def call(params):
        calls.append(params["params"]["ticker"])
        if error is not None:
            raise error
        return response

    provider._api = SimpleNamespace(call_data_source_tool=call)
    return provider, calls


@pytest.mark.parametrize("sdk_error", [False, True], ids=["response-body", "sdk-exception"])
def test_agentgw_internal_batch_log_is_safe_before_pipeline_fallback(
        monkeypatch, caplog, sdk_error):
    caplog.set_level(logging.INFO)
    provider, calls = _agentgw(
        SimpleNamespace(is_success=False, text=MARKER),
        TimeoutError(MARKER) if sdk_error else None)
    frame = _frame()
    monkeypatch.setattr(pipeline, "_channel_chain",
                        lambda: [_Provider("last", result={"A": frame})])

    assert pipeline._batch_with_fallback(provider, ["A"], 30)["A"] is frame

    assert calls == ["A"]
    _assert_no_body(caplog)
    assert "agentgw.ohlcv_batch" in caplog.text
    assert ("TimeoutError" if sdk_error else "RuntimeError") in caplog.text


def test_agentgw_batch_failure_keeps_valid_cached_frames(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    provider, calls = _agentgw(SimpleNamespace(is_success=False, text=MARKER))
    cached = _frame(101, days=60)
    provider._cache["A"] = cached
    b, c = _frame(102), _frame(103)
    monkeypatch.setattr(pipeline, "_channel_chain",
                        lambda: [_Provider("last", result={"A": _frame(999), "B": b, "C": c})])

    out = pipeline._batch_with_fallback(provider, list("ABC"), 30)

    assert out["A"] is cached and out["B"] is b and out["C"] is c
    assert sorted(calls) == ["B", "C"]
    assert provider._cache["A"] is cached
    assert pipeline._LINEAGE == [("batch[1/3]", "agentgw"), ("batch+补2只", "last")]
    _assert_no_body(caplog)
