"""Daily macro labels, immutable source I/O and independent replay agree."""
from dataclasses import asdict
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

import main
from trading_system.agent_evidence import normalize, read_snapshot
from trading_system.llm.required_proof import verify_required_model
from trading_system.market_close import _snapshot_window, close_metadata, close_observation, verify_close_evidence
from trading_system.redline.execution import (
    DAILY_MACRO_OBSERVATION_CONTRACT, ExecutionContext, ExecutionPolicy,
    REQUIRED_POLICY_ID, StrictObservationError, daily_macro_observation,
    market_observation, record_provider,
)
from test_model_required_workflow import required_fixture
from test_strict_execution_core import make_execution


@pytest.mark.parametrize("zone", [None, "America/Chicago", "America/New_York", "UTC"])
@pytest.mark.parametrize("start", ["2026-01-12", "2026-07-13", "2026-03-06", "2026-10-30"])
def test_source_midnight_labels_keep_calendar_dates_prices_and_raw_input(zone, start):
    index = pd.date_range(start, periods=5, tz=zone)
    raw = pd.Series([4.1, 4.2, 4.3, 4.4, 4.5], index=index, name="Close")
    original = raw.copy(deep=True)
    expected_index = index.tz_localize(None) if zone else index
    cutoff = expected_index[-1].date()
    projected = daily_macro_observation(raw, run_date=cutoff, market_timezone="America/New_York")
    pd.testing.assert_index_equal(projected.index, expected_index)
    assert projected.tolist() == original.tolist()
    pd.testing.assert_series_equal(raw, original)
    accepted = close_observation(projected, cutoff, exact=False)
    snapshot = normalize(raw)
    assert _snapshot_window(snapshot, cutoff, cutoff, exact=False,
        macro_daily_labels=DAILY_MACRO_OBSERVATION_CONTRACT) == normalize(accepted)
    assert snapshot == normalize(original)


@pytest.mark.parametrize("zone", [None, "America/Chicago", "America/New_York", "UTC"])
@pytest.mark.parametrize("kind", ["one_am", "intraday", "nat", "duplicate", "reversed", "future"])
def test_nonmidnight_invalid_and_future_source_labels_fail_runtime_and_replay(zone, kind):
    index = pd.date_range("2026-11-02", periods=2, tz=zone)
    if kind == "one_am":
        index += pd.Timedelta(hours=1)
    elif kind == "intraday":
        index += pd.Timedelta(hours=13)
    elif kind == "nat":
        index = pd.DatetimeIndex([index[0], pd.NaT])
    elif kind == "duplicate":
        index = pd.DatetimeIndex([index[0], index[0]])
    elif kind == "reversed":
        index = index[::-1]
    cutoff = date(2026, 11, 1) if kind == "future" else date(2026, 11, 3)
    raw = pd.Series([4.1, 4.2], index=index, name="Close")
    with pytest.raises(StrictObservationError):
        daily_macro_observation(raw, run_date=cutoff, market_timezone="America/New_York")
    with pytest.raises((ValueError, TypeError, KeyError)):
        _snapshot_window(normalize(raw), cutoff, cutoff, exact=False,
                         macro_daily_labels=DAILY_MACRO_OBSERVATION_CONTRACT)


def test_future_utc_source_day_cannot_borrow_previous_new_york_day():
    raw = pd.Series([4.2], index=pd.date_range("2026-11-04", periods=1, tz="UTC"))
    ordinary = market_observation(raw, run_date=date(2026, 11, 3), market_timezone="America/New_York")
    assert ordinary.index[0].date() == date(2026, 11, 3)
    with pytest.raises(StrictObservationError):
        daily_macro_observation(raw, run_date=date(2026, 11, 3), market_timezone="America/New_York")


def test_legacy_replay_keeps_original_instant_rule_and_ohlcv_stays_strict():
    raw = pd.Series([4.2], index=pd.date_range("2026-11-03", periods=1, tz="America/Chicago"))
    ordinary = market_observation(raw, run_date=date(2026, 11, 3), market_timezone="America/New_York")
    assert ordinary.index[0].hour == 1
    with pytest.raises(ValueError):
        _snapshot_window(normalize(raw), date(2026, 11, 3), date(2026, 11, 3), exact=False)
    intraday = pd.DataFrame({field: [10.] for field in ("Open", "High", "Low", "Close", "Volume")}, index=ordinary.index)
    with pytest.raises(StrictObservationError):
        close_observation(intraday, date(2026, 11, 3), exact=True)
    with pytest.raises(ValueError):
        _snapshot_window(normalize(intraday), date(2026, 11, 3), date(2026, 11, 3), exact=True,
                         macro_daily_labels=DAILY_MACRO_OBSERVATION_CONTRACT)
    close = datetime.fromisoformat("2026-11-03T16:00:00-05:00")
    assert "macro_daily_labels" not in close_metadata(close, macro_daily_labels=None)
    assert close_metadata(close)["macro_daily_labels"] == DAILY_MACRO_OBSERVATION_CONTRACT


@pytest.mark.parametrize("method,projected_hour", [("rate_yield_for", 0), ("vol_index_for", 0), ("tnx_yield", 1), ("ohlcv", 1)])
def test_recording_provider_applies_only_two_admitted_macro_methods(tmp_path, method, projected_hour):
    original, root = make_execution(tmp_path)
    execution = ExecutionContext(run_id=original.run_id, run_date=original.run_date,
        paths=original.paths, policy=ExecutionPolicy(REQUIRED_POLICY_ID, model_mode="enabled", search_source_names=("deepseek_search",)),
        store=original.store, admitted_at=datetime.fromisoformat("2026-10-20T21:30:00+00:00"))
    raw = pd.Series([4.2], index=pd.date_range("2026-10-20", periods=1, tz="America/Chicago"))
    source = SimpleNamespace(name="yahoo", synthetic=False, **{method: lambda *a, **k: raw})
    with execution.step("daily.preflight", {}) as stage:
        projected = getattr(record_provider(source, execution), method)("TNX")
        assert projected.index[0].hour == projected_hour
        stage.output(projected)
    output = read_snapshot(root, execution.summary()[0]["output_ref"], run_id=execution.run_id,
                           stage="daily.preflight", role="output")
    recorded = read_snapshot(root, output["io_calls"][0]["output_ref"], run_id=execution.run_id,
                             stage="daily.preflight", role="output")
    assert recorded == normalize(raw)
    assert recorded["index"][0]["value"].endswith("-05:00")


def test_complete_workflow_and_independent_model_proof_bind_source_label_contract(tmp_path, monkeypatch):
    original, root, args, provider, denied, _ = required_fixture(tmp_path, monkeypatch)
    admitted_at = datetime.fromisoformat("2026-10-20T21:30:00+00:00")
    execution = ExecutionContext(run_id=original.run_id, run_date=original.run_date,
        paths=original.paths, policy=original.policy, store=original.store, admitted_at=admitted_at)
    for method in ("tnx_yield", "vix", "vix9d"):
        original_method = getattr(provider, method)

        def source_daily(*args, _original=original_method, **kwargs):
            series = _original(*args, **kwargs)
            if series is None:
                return None
            series = series.copy()
            series.index = series.index.tz_localize("America/Chicago")
            return series

        monkeypatch.setattr(provider, method, source_daily)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    result = asdict(output.result)
    admission = {"clock": {"run_date": execution.run_date.isoformat(),
        "market_timezone": "America/New_York", "admitted_at_utc": admitted_at.isoformat()}}
    verify_close_evidence(root, admission, result["raw"]["freshness"])
    assert verify_required_model(root, result, admission) == result["raw"]["model"]
    assert len(execution.summary()) == 39 and denied == []
    assert result["raw"]["freshness"]["market_close"]["macro_daily_labels"] == DAILY_MACRO_OBSERVATION_CONTRACT


def test_legacy_metadata_without_new_contract_remains_replayable(tmp_path, monkeypatch):
    original, root, args, _, denied, _ = required_fixture(tmp_path, monkeypatch)
    admitted_at = datetime.fromisoformat("2026-10-20T21:30:00+00:00")
    execution = ExecutionContext(run_id=original.run_id, run_date=original.run_date,
        paths=original.paths, policy=original.policy, store=original.store, admitted_at=admitted_at)
    monkeypatch.setattr("trading_system.pipeline.close_metadata", lambda close: close_metadata(close, macro_daily_labels=None))
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                            "admitted_at_utc": admitted_at.isoformat()}}
    assert "macro_daily_labels" not in result["raw"]["freshness"]["market_close"]
    verify_close_evidence(root, admission, result["raw"]["freshness"])
    assert verify_required_model(root, result, admission) == result["raw"]["model"]
    assert denied == []
