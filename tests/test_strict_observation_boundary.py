"""Strict observation cutoffs and terminal evidence failures; all sources are fixtures."""
from __future__ import annotations

import copy
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

import main
from trading_system import pipeline
from trading_system.journal import Journal
from trading_system.ledger_io import write_json_atomic
from trading_system.redline.execution import (
    EvidenceWriteError, StrictDataUnavailable, record_provider,
)
from test_strict_daily_core import daily_fixture
from test_strict_execution_core import advance_to, make_execution


def _frame(index):
    return pd.DataFrame({"Open": 100., "High": 102., "Low": 99., "Close": 101.,
                         "Volume": 2_000_000}, index=pd.DatetimeIndex(index))


@pytest.mark.parametrize("method,shape", [("ohlcv", "frame"), ("ohlcv_batch", "batch"),
    ("tnx_yield", "series"), ("vix", "series"), ("vix9d", "series"),
    ("rate_yield_for", "series"), ("vol_index_for", "series")])
@pytest.mark.parametrize("index", [["2026-10-20", "2026-10-27"],
                                    ["2026-10-27", "2026-10-20"]])
def test_future_observation_is_rejected_after_raw_io_is_persisted(tmp_path, method, shape, index):
    execution, root = make_execution(tmp_path)
    raw = _frame(index)
    if shape == "series":
        raw = raw["Close"]
    if shape == "batch":
        raw = {"AAA": raw}
    source = SimpleNamespace(name="yahoo", synthetic=False, **{method: lambda *a, **k: raw})
    provider = record_provider(source, execution)
    with pytest.raises(StrictDataUnavailable):
        with execution.step("daily.preflight", {}) as stage:
            getattr(provider, method)("AAA")
            stage.output({})
    assert execution.summary()[-1]["status"] == "failed"
    snapshots = [p.read_text() for p in (root / "evidence").glob("*.json")]
    assert any("2026-10-27" in text and '"role":"output"' in text for text in snapshots)
    assert not (root / "receipt.json").exists()


@pytest.mark.parametrize("stamp,accepted,market_date", [
    ("2026-10-19", True, "2026-10-19"), ("2026-10-20", True, "2026-10-20"),
    ("2026-10-21", False, None), ("2026-10-21T01:00:00+00:00", True, "2026-10-20"),
    ("2026-10-21T04:00:00+00:00", False, None),
    ("2026-10-21T09:00:00+09:00", True, "2026-10-20"),
])
def test_observation_dates_use_frozen_new_york_boundary(tmp_path, stamp, accepted, market_date):
    execution, root = make_execution(tmp_path)
    raw = _frame([stamp])
    original_index = raw.index.copy()
    provider = record_provider(SimpleNamespace(name="yahoo", synthetic=False,
                                                ohlcv=lambda *a, **k: raw), execution)
    if not accepted:
        with pytest.raises(StrictDataUnavailable):
            with execution.step("daily.preflight", {}) as stage:
                provider.ohlcv("AAA")
                stage.output({})
    else:
        with execution.step("daily.preflight", {}) as stage:
            observed = provider.ohlcv("AAA")
            assert str(observed.index[-1].date()) == market_date
            assert observed["Close"].iloc[-1] == raw["Close"].iloc[-1]
            stage.output(observed)
    assert raw.index.equals(original_index), "Normalization must not mutate the source observation"


@pytest.mark.parametrize("target,expected_stage", [
    ("benchmark", "data.prepare"), ("macro", "data.prepare"),
    ("short_vol", "data.prepare"), ("etf", "data.prepare"),
    ("stock", "data.prepare"), ("monitor", "tech.monitor"),
    ("linkage", "tech.cycle_linkage"), ("journal", "journal.settle"),
    ("paper", "paper.step"), ("html", "report.html"),
])
def test_future_observation_stops_each_consumer_before_stage_success(tmp_path, monkeypatch, target, expected_stage):
    execution, root, args, fixture, network = daily_fixture(tmp_path, monkeypatch)
    original_ohlcv, original_batch = fixture.ohlcv, fixture.ohlcv_batch
    def future(value):
        value = value.copy()
        value.index = value.index + pd.Timedelta(days=7)
        return value
    def ohlcv(ticker, days=400):
        frame = original_ohlcv(ticker, days)
        active = execution._active.spec.name
        if ((target == "benchmark" and ticker == "SPY" and active == "data.prepare")
                or (target == "linkage" and active == "tech.cycle_linkage")
                or (target == "journal" and active == "journal.settle")
                or (target == "paper" and active == "paper.step")
                or (target == "html" and active == "report.html")):
            return future(frame)
        return frame
    def batch(tickers, days=400, **kwargs):
        actual = original_batch(tickers, days, **kwargs)
        active = execution._active.spec.name
        chosen = (next(iter(actual), None) if target == "etf" and "XLK" in tickers else
                  "AAA" if target == "stock" and "AAA" in actual else None)
        if active == "data.prepare" and chosen:
            actual[chosen] = future(actual[chosen])
        if target == "monitor" and active == "tech.monitor":
            actual[tickers[0]] = future(original_ohlcv(tickers[0], days))
        return actual
    monkeypatch.setattr(fixture, "ohlcv", ohlcv)
    monkeypatch.setattr(fixture, "ohlcv_batch", batch)
    if target in ("macro", "short_vol"):
        method = "tnx_yield" if target == "macro" else "vix9d"
        original = getattr(fixture, method)
        monkeypatch.setattr(fixture, method, lambda days=400: future(original(days)))
    if target == "journal":
        write_json_atomic(execution.paths.journal_path, [{"date": "2026-10-19", "ticker": "AAA",
            "status": "open", "stop": 50., "entry_ref": 100.,
            "account_currency": "USD", "quote_currency": "USD"}])
    if target == "paper":
        from trading_system.simulator import SimEngine
        original_step = SimEngine.step
        def step(sim, trade_date, result, get_bar):
            get_bar("AAA")
            return original_step(sim, trade_date, result, get_bar)
        monkeypatch.setattr(SimEngine, "step", step)
    with pytest.raises(StrictDataUnavailable):
        main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["stage"] == expected_stage
    assert execution.summary()[-1]["status"] == "failed"
    assert not any(row["stage"] == "daily.result" for row in execution.summary())
    assert not (root / "receipt.json").exists() and network == []


def test_future_benchmark_freshness_defense_catches_unwrapped_test_source(tmp_path, monkeypatch):
    execution, root, args, fixture, _ = daily_fixture(tmp_path, monkeypatch)
    fixture.run_date = date(2026, 10, 27)
    fixture.execution = execution  # Bypass only the wrapper, retain strict no-fallback policy.
    monkeypatch.setattr(pipeline, "get_provider", lambda *a, **k: fixture)
    with pytest.raises(StrictDataUnavailable):
        main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["stage"] == "data.prepare"


def test_legacy_provider_data_and_future_option_expiry_are_unchanged(tmp_path):
    execution, _ = make_execution(tmp_path)
    raw = _frame(["2026-10-27"])
    source = SimpleNamespace(name="yahoo", synthetic=False, ohlcv=lambda *a, **k: raw,
                             options_chain_snapshot=lambda *a: {"expiry": "2026-11-20", "atm_iv": .2})
    assert record_provider(source, None).ohlcv("AAA") is raw
    with execution.step("daily.preflight", {}) as stage:
        assert record_provider(source, execution).options_chain_snapshot("AAA")["expiry"] == "2026-11-20"
        stage.output({})


def test_evidence_failure_stops_journal_before_next_provider_or_write(tmp_path, monkeypatch):
    execution, root = make_execution(tmp_path)
    journal = Journal(execution.paths.journal_path)
    journal.records = [{"date": "2026-10-19", "ticker": ticker, "status": "open",
        "stop": 90., "entry_ref": 100., "account_currency": "USD", "quote_currency": "USD"}
        for ticker in ("AAA", "BBB")]
    journal.save()
    before = journal.path.read_bytes()
    records_before = copy.deepcopy(journal.records)
    calls = []
    frame = _frame(["2026-10-19", "2026-10-20"])
    def ohlcv(ticker, **kwargs):
        calls.append(ticker)
        return frame
    provider = record_provider(SimpleNamespace(name="yahoo", synthetic=False, ohlcv=ohlcv), execution)
    original_snapshot = execution.store.snapshot
    def snapshot(**kwargs):
        if isinstance(kwargs["value"], pd.DataFrame) and calls == ["AAA"]:
            raise OSError("injected raw observation snapshot failure")
        return original_snapshot(**kwargs)
    monkeypatch.setattr(execution.store, "snapshot", snapshot)
    advance_to(execution, "journal.settle")
    with pytest.raises(EvidenceWriteError):
        with execution.step("journal.settle", {}) as stage:
            journal.settle(provider, as_of_date=execution.run_date)
            stage.output({"records": journal.records})
    assert calls == ["AAA"]
    assert journal.path.read_bytes() == before and journal.records == records_before
    assert execution.summary()[-1]["status"] == "failed"


@pytest.mark.parametrize("operation", ["begin_io", "call_io", "finish_io", "stage_output"])
def test_evidence_failure_poison_prevents_same_stage_followup(tmp_path, monkeypatch, operation):
    execution, _ = make_execution(tmp_path)
    original_snapshot = execution.store.snapshot
    calls = []
    def snapshot(**kwargs):
        if kwargs["value"] == "fail-output":
            raise OSError("injected snapshot failure")
        return original_snapshot(**kwargs)
    monkeypatch.setattr(execution.store, "snapshot", snapshot)
    with pytest.raises(EvidenceWriteError):
        with execution.step("daily.preflight", {}) as stage:
            pending = execution.begin_io("test.pending", {}) if operation == "finish_io" else None
            with pytest.raises(EvidenceWriteError):
                execution.call_io("test.first", {}, lambda: "fail-output")
            with pytest.raises(EvidenceWriteError):
                if operation == "begin_io":
                    execution.begin_io("test.followup", {})
                elif operation == "call_io":
                    execution.call_io("test.followup", {}, lambda: calls.append("unexpected"))
                elif operation == "finish_io":
                    execution.finish_io(pending, output="must not persist")
                else:
                    stage.output({})
            raise EvidenceWriteError("leave failed stage")
    assert calls == []


@pytest.mark.parametrize("bad_index", [pd.DatetimeIndex([pd.NaT]), pd.RangeIndex(1), pd.Index(["2026-10-27"])])
def test_future_boundary_rejects_unverifiable_observation_dates(tmp_path, bad_index):
    execution, _ = make_execution(tmp_path)
    frame = _frame(["2026-10-20"])
    frame.index = bad_index
    provider = record_provider(SimpleNamespace(name="yahoo", synthetic=False,
                                                ohlcv=lambda *a, **k: frame), execution)
    with pytest.raises(StrictDataUnavailable):
        with execution.step("daily.preflight", {}) as stage:
            provider.ohlcv("AAA")
            stage.output({})


def test_future_violation_cannot_be_caught_to_continue_provider_or_output(tmp_path):
    execution, _ = make_execution(tmp_path)
    calls = []
    def ohlcv(ticker):
        calls.append(ticker)
        return _frame(["2026-10-27"])
    provider = record_provider(SimpleNamespace(name="yahoo", synthetic=False, ohlcv=ohlcv), execution)
    with pytest.raises(StrictDataUnavailable):
        with execution.step("daily.preflight", {}) as stage:
            with pytest.raises(StrictDataUnavailable):
                provider.ohlcv("AAA")
            with pytest.raises(StrictDataUnavailable):
                provider.ohlcv("BBB")
            stage.output({})
    assert calls == ["AAA"]


def test_empty_observation_retains_existing_missing_data_semantics(tmp_path):
    execution, _ = make_execution(tmp_path)
    empty = pd.DataFrame()
    provider = record_provider(SimpleNamespace(name="yahoo", synthetic=False,
        ohlcv=lambda *a, **k: empty, vix9d=lambda: None), execution)
    with execution.step("daily.preflight", {}) as stage:
        assert provider.ohlcv("AAA") is empty
        assert provider.vix9d() is None
        stage.output({"missing": True})


def test_timezone_aware_observations_complete_real_daily_with_market_dates(tmp_path, monkeypatch):
    execution, root, args, fixture, network = daily_fixture(tmp_path, monkeypatch)
    original = fixture.ohlcv
    def ohlcv(ticker, days=400):
        frame = original(ticker, days)
        # 01:00 UTC on the next day is 21:00 on the original NY market date.
        frame.index = (frame.index + pd.Timedelta(days=1, hours=1)).tz_localize("UTC")
        return frame
    monkeypatch.setattr(fixture, "ohlcv", ohlcv)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    assert output.result.raw["freshness"]["benchmark_last_bar"] == "2026-10-20"
    assert output.result.raw["freshness"]["benchmark_lag_days"] == 0
    assert len(execution.summary()) == 39 and network == []
    assert output.sim_state["equity_curve"][-1]["date"] == "2026-10-20"


def test_journal_ordinary_provider_failure_still_discloses_and_continues(tmp_path):
    journal = Journal(tmp_path / "journal.json")
    journal.records = [{"date": "2026-10-19", "ticker": ticker, "status": "open",
        "stop": 90., "entry_ref": 100., "account_currency": "USD", "quote_currency": "USD"}
        for ticker in ("AAA", "BBB")]
    journal.save()
    def ohlcv(ticker, **kwargs):
        if ticker == "AAA":
            raise OSError("ordinary provider unavailable")
        return _frame(["2026-10-19", "2026-10-20"])
    journal.settle(SimpleNamespace(ohlcv=ohlcv), as_of_date=date(2026, 10, 20))
    assert journal.last_failed == ["AAA"]
    assert journal.records[1]["entry_date"] == "2026-10-20"
