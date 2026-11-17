"""MC-003：单源故障不得中断真实源回退，缺失与合成来源必须可辨认。"""

from __future__ import annotations

import pandas as pd
import pytest

from trading_system import pipeline
from trading_system.providers.demo import DemoProvider
from trading_system.redline import LLMUnavailable


def _frame(value=100.0):
    return pd.DataFrame({
        "Open": [value] * 3, "High": [value + 1] * 3,
        "Low": [value - 1] * 3, "Close": [value] * 3,
        "Volume": [1000.0] * 3,
    }, index=pd.date_range(end=pd.Timestamp.today().normalize(), periods=3))


class _Provider:
    synthetic = False

    def __init__(self, name, result=None, error=None, capabilities=None, calls=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = calls if calls is not None else []
        if capabilities is not None:
            self.CAPABILITIES = frozenset(capabilities)

    def _call(self, method, target, days):
        self.calls.append((self.name, method, target, days))
        if self.error is not None:
            raise self.error
        return self.result

    def ohlcv(self, ticker, days=400):
        return self._call("ohlcv", ticker, days)

    def vol_index_for(self, symbol, days=400):
        return self._call("vol_index_for", symbol, days)

    def ohlcv_batch(self, tickers, days=400, *, hard_deadline=None, progress_callback=None):
        return self._call("ohlcv_batch", list(tickers), days)


@pytest.fixture(autouse=True)
def _isolated_fallbacks(monkeypatch):
    monkeypatch.setattr(pipeline, "_LINEAGE", [])
    monkeypatch.setattr(pipeline, "_HEALTH", {})
    monkeypatch.setattr(pipeline, "_DATA_ISSUES", [], raising=False)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [])
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider",
                        lambda: _Provider("stooq", result={}))


def test_initial_batch_exception_reaches_existing_fallback(monkeypatch):
    calls = []
    primary = _Provider("yahoo", error=TimeoutError("primary timeout"), calls=calls)
    frames = {t: _frame(i + 1) for i, t in enumerate(["A", "B", "C"])}
    stooq = _Provider("stooq", result=frames, calls=calls)
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", lambda: stooq)

    result = pipeline._batch_with_fallback(primary, ["A", "B", "C"], 420)

    assert result == frames
    assert [c[0] for c in calls] == ["yahoo", "stooq"]
    assert any(i["provider"] == "yahoo" and i["issue"] == "provider_error"
               for i in pipeline._DATA_ISSUES)
    assert ("batch+补3只", "stooq") in pipeline._LINEAGE


def test_middle_batch_exception_preserves_order_and_missing_only(monkeypatch):
    calls = []
    a, b, c = _frame(101), _frame(102), _frame(103)
    primary = _Provider("yahoo", result={"A": a}, calls=calls)
    stooq = _Provider("stooq", result={"A": _frame(999), "B": b}, calls=calls)
    broken = _Provider("tencent", error=ConnectionError("middle source down"), calls=calls)
    last = _Provider("sina", result={"A": _frame(888), "C": c,
                                     "UNREQUESTED": _frame()}, calls=calls)
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", lambda: stooq)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [broken, last])

    result = pipeline._batch_with_fallback(primary, ["A", "B", "C", "D"], 420)

    assert list(result) == ["A", "B", "C"]
    assert result["A"] is a and result["B"] is b and result["C"] is c
    assert calls == [
        ("yahoo", "ohlcv_batch", ["A", "B", "C", "D"], 420),
        ("stooq", "ohlcv_batch", ["B", "C", "D"], 420),
        ("tencent", "ohlcv_batch", ["C", "D"], 420),
        ("sina", "ohlcv_batch", ["C", "D"], 420),
    ]
    assert pipeline._LINEAGE == [("batch[1/4]", "yahoo"),
                                  ("batch+补1只", "stooq"),
                                  ("batch+补1只", "sina")]
    assert any(i["issue"] == "batch_missing" and i["symbols"] == ["D"]
               for i in pipeline._DATA_ISSUES)


def test_batch_skips_declared_non_market_data_capability(monkeypatch):
    primary = _Provider("stooq", result={})
    macro_only = _Provider("official", error=AssertionError("must not call"),
                           capabilities={"rate_yield_for", "vol_index_for"})
    frame = _frame()
    equity = _Provider("tencent", result={"A": frame}, capabilities={"ohlcv"})
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [macro_only, equity])

    result = pipeline._batch_with_fallback(primary, ["A"], 30)

    assert result["A"] is frame
    assert macro_only.calls == []
    assert len(equity.calls) == 1
    assert any(i["provider"] == "official" and i["issue"] == "unsupported"
               for i in pipeline._DATA_ISSUES)


@pytest.mark.parametrize("invalid", [None, pd.DataFrame(),
                                    pd.DataFrame({"Close": [float("nan")]}),
                                    pd.Series([100.0])],
                         ids=["none", "empty", "all_nan", "wrong_type"])
def test_invalid_batch_values_do_not_block_replacement(monkeypatch, invalid):
    primary = _Provider("stooq", result={"A": invalid})
    frame = _frame()
    fallback = _Provider("tencent", result={"A": frame})
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [fallback])

    result = pipeline._batch_with_fallback(primary, ["A"], 30)

    assert result["A"] is frame
    assert fallback.calls == [("tencent", "ohlcv_batch", ["A"], 30)]
    assert any(i["provider"] == "stooq" and i["issue"] == "invalid_data"
               for i in pipeline._DATA_ISSUES)


@pytest.mark.parametrize("payload", [None, []], ids=["none", "wrong_type"])
def test_invalid_batch_container_continues_fallback(monkeypatch, payload):
    primary = _Provider("stooq", result=payload)
    frame = _frame()
    fallback = _Provider("tencent", result={"A": frame})
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [fallback])
    assert pipeline._batch_with_fallback(primary, ["A"], 30)["A"] is frame


def test_batch_total_outage_retains_explicit_missing_evidence(monkeypatch, caplog):
    caplog.set_level("WARNING")
    primary = _Provider("stooq", result={})
    broken = _Provider("tencent", error=TimeoutError("down"))
    empty = _Provider("sina", result={"A": pd.DataFrame()})
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [broken, empty])

    assert pipeline._batch_with_fallback(primary, ["A", "B"], 30, "stocks") == {}
    final = pipeline._DATA_ISSUES[-1]
    assert final["issue"] == "batch_unavailable"
    assert final["label"] == "stocks" and final["symbols"] == ["A", "B"]
    assert any(i["provider"] == "tencent" and i["issue"] == "provider_error"
               for i in pipeline._DATA_ISSUES)
    assert "stocks" in caplog.text and "0/2" in caplog.text


def test_empty_batch_never_calls_providers():
    primary = _Provider("primary", error=AssertionError("empty request"))
    assert pipeline._batch_with_fallback(primary, [], 30) == {}
    assert primary.calls == [] and pipeline._DATA_ISSUES == []


@pytest.mark.parametrize("success_at", ["primary", "stooq"])
def test_existing_coverage_threshold_does_not_force_extra_sources(monkeypatch, success_at):
    frames = {t: _frame() for t in ["A", "B", "C"]}
    primary = _Provider("yahoo", result=frames if success_at == "primary" else {})
    stooq = _Provider("stooq", result=frames)
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", lambda: stooq)

    def unexpected_chain():
        pytest.fail("达到既有覆盖率阈值后不得继续请求后续通道")

    monkeypatch.setattr(pipeline, "_channel_chain", unexpected_chain)
    result = pipeline._batch_with_fallback(primary, list("ABCDEF"), 30)
    assert result == frames
    assert len(stooq.calls) == (0 if success_at == "primary" else 1)


def test_batch_passes_one_hard_deadline_to_every_real_provider(monkeypatch, tmp_path):
    calls = []
    frame = _frame()
    class DeadlineProvider(_Provider):
        def ohlcv_batch(self, tickers, days=400, *, hard_deadline=None, progress_callback=None):
            calls.append((self.name, hard_deadline, tickers))
            return {ticker: frame for ticker in tickers}
    primary = DeadlineProvider("yahoo")
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", lambda: _Provider("stooq", result={}))
    result = pipeline._batch_with_fallback(primary, ["A", "B"], 30, deadline_seconds=2)
    assert set(result) == {"A", "B"}
    assert calls and calls[0][0] == "yahoo" and calls[0][1] is not None


def test_progress_projection_updates_while_batch_is_running(monkeypatch, tmp_path):
    frame = _frame()
    class ProgressProvider(_Provider):
        def ohlcv_batch(self, tickers, days=400, *, hard_deadline=None, progress_callback=None):
            for index, ticker in enumerate(tickers, 1):
                progress_callback({
                    "attempted": index, "completed": index,
                    "failed": 0, "timed_out": 0,
                })
            return {ticker: frame for ticker in tickers}
    execution = type("Execution", (), {
        "paths": type("Paths", (), {"work_dir": tmp_path})(),
        "_active": type("Active", (), {"spec": type("Spec", (), {"name": "data.prepare"})()})(),
    })()
    provider = ProgressProvider("yahoo")
    provider.execution = execution
    result = pipeline._batch_with_fallback(provider, list("ABCDE"), 30, deadline_seconds=2)
    assert len(result) == 5
    progress = __import__("json").loads((tmp_path / "progress.json").read_text())
    assert progress["schema"] == "tiger.run-progress/v1"
    assert progress["provider"] == "yahoo"
    assert progress["completed"] == 5 and progress["total"] == 5
    assert progress["progress_is_not_receipt"] is True


@pytest.mark.parametrize("invalid", [None, pd.DataFrame(),
                                    pd.DataFrame({"Close": [float("nan")]}),
                                    pd.Series(dtype=float),
                                    pd.Series([float("nan")])],
                         ids=["none", "empty_frame", "nan_frame", "empty_series", "nan_series"])
def test_single_empty_or_nan_result_tries_next_source(monkeypatch, invalid):
    primary = _Provider("primary", result=invalid)
    frame = _frame()
    fallback = _Provider("tencent", result=frame)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [fallback])

    assert pipeline._single_with_fallback(primary, "ohlcv", "A", days=30) is frame
    assert pipeline._HEALTH["primary"]["fail"] == 1
    assert pipeline._HEALTH["tencent"]["ok"] == 1
    assert pipeline._LINEAGE == [("ohlcv(A)", "tencent")]


def test_empty_intermediate_single_result_reaches_last_source(monkeypatch):
    primary = _Provider("primary", error=RuntimeError("down"))
    empty = _Provider("tencent", result=pd.DataFrame())
    frame = _frame()
    last = _Provider("sina", result=frame)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [empty, last])
    assert pipeline._single_with_fallback(primary, "ohlcv", "A", days=30) is frame
    assert pipeline._HEALTH["tencent"]["fail"] == 1


def test_single_total_outage_preserves_error_and_failure_evidence(monkeypatch):
    primary = _Provider("primary", result=pd.DataFrame())
    last = _Provider("tencent", error=ValueError("last source unavailable"))
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [last])
    with pytest.raises(pipeline.ProviderDataError) as caught:
        pipeline._single_with_fallback(primary, "ohlcv", "A", days=30)
    assert caught.value.provider == "tencent" and caught.value.method == "ohlcv"
    assert caught.value.error_type == "ValueError"
    assert [i["provider"] for i in pipeline._DATA_ISSUES] == ["primary", "tencent"]
    assert pipeline._LINEAGE == []


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_real_source_cannot_fallback_to_renamed_synthetic(monkeypatch, batch):
    primary = _Provider("stooq", result={} if batch else None)
    fake = _Provider("renamed", result={"A": _frame()} if batch else _frame())
    fake.synthetic = True
    real_frame = _frame(200)
    real = _Provider("tencent", result={"A": real_frame} if batch else real_frame)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [fake, real])

    result = (pipeline._batch_with_fallback(primary, ["A"], 30)["A"] if batch
              else pipeline._single_with_fallback(primary, "ohlcv", "A", days=30))
    assert result is real_frame and fake.calls == []
    assert any(i["provider"] == "renamed" and i["issue"] == "synthetic_fallback_rejected"
               for i in pipeline._DATA_ISSUES)


def test_renamed_synthetic_pipeline_stays_demo_and_labels_sources(monkeypatch, tmp_path):
    class RenamedDemo(DemoProvider):
        name = "fixture-provider"

    class OfflineLLM:
        def complete_json(self, **kwargs):
            raise LLMUnavailable("offline fault injection")

    class SearchProbe:
        def __init__(self, *, demo):
            seen.append(demo)
            assert demo is True, "合成源即使改名，也必须选择 demo 搜索"

        def register_topic_set(self, *args, **kwargs):
            return None

        def gather_topic_sets(self, **kwargs):
            return []

        def active_topics(self):
            return {}

    seen = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline.config, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(pipeline, "get_provider", lambda name=None: RenamedDemo())
    monkeypatch.setattr(pipeline, "default_client", lambda: OfflineLLM())
    monkeypatch.setattr(pipeline, "SearchHub", SearchProbe)
    # 本轮必须清掉之前运行的失败记录。
    pipeline._DATA_ISSUES.append({"issue": "old_run"})

    result = pipeline.run_pipeline(provider_name="fixture-provider", universe_mode="core",
                                   top_n=5, max_picks=2)

    assert seen == [True]
    assert result.provider == "demo"
    assert result.raw["synthetic"] is True
    assert result.raw["provider_name"] == "fixture-provider"
    assert result.raw["data_issues"] == []
    assert all(source == "demo" for _, source in result.raw["source_lineage"])
    assert result.mrs is not None
    assert len(result.raw["redline"]) == 21
