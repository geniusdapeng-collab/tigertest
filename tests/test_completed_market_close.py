"""Offline checks of the strict close-data admission and replay path."""
from dataclasses import asdict
from datetime import date, datetime
import json

import pandas as pd
import pytest

import main
from trading_system.agent_evidence import EvidenceInvalid, read_bytes, read_json
from trading_system.data_models import StockCandidate
from trading_system.llm.required_proof import verify_required_model
from trading_system.market_close import completed_session, macro_limits, next_scheduled_close, verify_close_evidence
from trading_system.pipeline import ProviderDataError, UniverseScannerAgent, _single_with_fallback
from trading_system.redline.execution import (ExecutionContext, StrictDataStale,
                                               StrictObservationError)
from test_model_required_workflow import required_fixture
from test_model_required_temporal import _stage, _rewrite_events, _save_snapshot


@pytest.mark.parametrize(("admission", "expected"), [
    ("2026-11-03T20:59:59+00:00", "2026-11-02T16:00:00-05:00"),
    ("2026-11-03T21:00:00+00:00", "2026-11-02T16:00:00-05:00"),
    ("2026-11-03T21:00:01+00:00", "2026-11-03T16:00:00-05:00"),
    ("2026-07-06T20:01:00+00:00", "2026-07-06T16:00:00-04:00"),
    ("2026-11-27T18:00:00+00:00", "2026-11-25T16:00:00-05:00"),
    ("2026-11-27T18:01:00+00:00", "2026-11-27T13:00:00-05:00"),
    ("2026-11-29T23:00:00+00:00", "2026-11-27T13:00:00-05:00"),
    ("2025-01-09T23:00:00+00:00", "2025-01-08T16:00:00-05:00"),
    ("2028-01-01T02:00:00+00:00", "2027-12-31T16:00:00-05:00"),
])
def test_close_calendar_includes_exact_close_dst_and_special_holidays(admission, expected):
    assert completed_session(datetime.fromisoformat(admission)).isoformat() == expected


@pytest.mark.parametrize("admission", ["2026-11-03T16:00:00", "2029-01-03T21:00:00+00:00"])
def test_close_calendar_rejects_unverifiable_admission(admission):
    with pytest.raises(StrictObservationError):
        completed_session(datetime.fromisoformat(admission))


def test_same_day_macro_requires_source_publication_windows():
    admission = datetime.fromisoformat("2026-11-27T18:10:00+00:00")
    cash_close = completed_session(admission)
    day = cash_close.date()
    previous = day.replace(day=26)
    assert macro_limits(cash_close, admission) == {"tnx": previous, "vix": previous, "vix9d": previous}
    after_volatility = datetime.fromisoformat("2026-11-27T18:15:01+00:00")
    assert macro_limits(cash_close, after_volatility) == {"tnx": previous, "vix": day, "vix9d": day}
    at_rate_release = datetime.fromisoformat("2026-11-27T21:15:00+00:00")
    assert macro_limits(cash_close, at_rate_release)["tnx"] == previous
    after_rate_release = datetime.fromisoformat("2026-11-27T21:15:01+00:00")
    assert set(macro_limits(cash_close, after_rate_release).values()) == {day}


@pytest.mark.parametrize(("research_day", "expiry"), [
    ("2026-11-25", "2026-11-27T13:00:00-05:00"),
    ("2026-11-27", "2026-11-30T16:00:00-05:00"),
    ("2025-01-08", "2025-01-10T16:00:00-05:00"),
])
def test_next_session_expiry_uses_same_calendar(research_day, expiry):
    assert next_scheduled_close(date.fromisoformat(research_day)).isoformat() == expiry


def _with_clock(execution, stamp, *, run_date=None):
    admitted = datetime.fromisoformat(stamp)
    return ExecutionContext(run_id=execution.run_id, run_date=run_date or execution.run_date,
        paths=execution.paths, policy=execution.policy, store=execution.store, admitted_at=admitted)


def test_model_daily_uses_one_completed_session_and_same_spy_chart(tmp_path, monkeypatch):
    original, root, args, provider, denied, _ = required_fixture(tmp_path, monkeypatch)
    research_date = date(2026, 11, 25)
    provider.run_date = research_date
    execution = _with_clock(original, "2026-11-25T20:59:59+00:00", run_date=research_date)
    admission = {"clock": {"run_date": research_date.isoformat(), "market_timezone": "America/New_York",
                           "admitted_at_utc": execution.admitted_at.isoformat()}}
    batch = provider.ohlcv_batch
    def mixed_dates(symbols, days=400):
        if execution._active.spec.name == "tech.monitor":
            assert not any(symbol.endswith((".KS", ".TW")) for symbol in symbols)
        frames = batch(symbols, days)
        if "AAA" in frames:
            frames["AAA"] = frames["AAA"].loc[:"2026-11-24"]
        if "BBB" in frames:
            frames["BBB"] = frames["BBB"].loc[:"2026-11-23"]
        return frames
    monkeypatch.setattr(provider, "ohlcv_batch", mixed_dates)
    from trading_system.tech_chain.agents import CycleLinkageAgent
    monkeypatch.setattr(CycleLinkageAgent, "LEADERS", ["AAA", "CCC", "DDD", "005930.KS"])
    original_ohlcv = provider.ohlcv
    linkage_fetches = []

    def ohlcv(symbol, days=400):
        if execution._active.spec.name == "tech.cycle_linkage":
            linkage_fetches.append(symbol)
            if symbol == "DDD":
                raise TimeoutError("offline linkage source unavailable")
        return original_ohlcv(symbol, days)

    monkeypatch.setattr(provider, "ohlcv", ohlcv)
    scan = UniverseScannerAgent.execute

    def observed_watch(agent, context):
        scan(agent, context)
        frame = context["market_data"]["stock_ohlcv"]["AAA"]
        context["watchlist"] = [StockCandidate("AAA", price=float(frame["Close"].iloc[-1]))]
        context["scan_stats"]["selected"] = 1
        return context["watchlist"]

    monkeypatch.setattr(UniverseScannerAgent, "execute", observed_watch)
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    _, stage = _stage(execution, root, "data.prepare")
    data = stage["value"]["market_data"]
    freshness = result["raw"]["freshness"]
    assert freshness["benchmark_last_bar"] == "2026-11-24"
    assert freshness["benchmark_lag_days"] == 0
    assert freshness["market_close"]["close_at_utc"] == "2026-11-24T21:00:00+00:00"
    assert "BBB" in freshness["market_close"]["stock_missing"]
    assert "BBB" not in data["stock_ohlcv"]
    linkage_input, linkage_stage = _stage(execution, root, "tech.cycle_linkage")
    assert linkage_input["prefetched"] == ["AAA", "CCC", "SPY"]
    assert _stage(execution, root, "tech.monitor")[0]["excluded"] == ["000660.KS", "005930.KS", "2330.TW"]
    assert linkage_fetches == ["DDD"]
    assert [row["fields"]["ticker"] for row in linkage_stage["value"]] == ["AAA", "CCC"]
    assert any(issue["issue"] == "completed_session_linkage_missing" and issue["symbols"] == ["DDD", "005930.KS"]
               for issue in result["raw"]["data_issues"])
    assert all(frame["index"][-1]["value"].startswith("2026-11-24")
               for frame in [data["spy"], *data["sector_etfs"].values(), *data["stock_ohlcv"].values()])
    assert _stage(execution, root, "report.html")[1]["value"]["benchmarks"]["SPY"][-1][0] == "2026-11-24"
    chart = _stage(execution, root, "report.html")[1]
    assert list(chart["value"]["benchmarks"]) == ["SPY"]
    assert [call["method"] for call in chart["io_calls"]] == ["file.write_text"]
    assert "已完成收盘交易日" in chart["value"]["html"]["text"]
    verify_close_evidence(root, admission, freshness)
    plans = result["raw"]["execution_decisions"]
    assert [plan["valid_for"] for plan in plans] == ["2026-11-27T13:00:00-05:00"]
    assert verify_required_model(root, result, admission) == result["raw"]["model"]
    assert denied == []

    # Rehash a price change and keep the self-reported dates. Raw provider IO
    # still shows the original value, so even coherent stage evidence fails.
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    event = next(row for row in events if row["stage"] == "data.prepare" and row["kind"] == "end")
    output_ref = event["output_ref"]
    blob = read_json(root, output_ref["path"])
    spy = blob["value"]["value"]["market_data"]["spy"]
    spy["values"][-1][spy["columns"].index("Close")] += 1
    _rewrite_events(root, events, [(output_ref, _save_snapshot(root, blob))])
    with pytest.raises(EvidenceInvalid, match="Frozen completed-session"):
        verify_close_evidence(root, admission, freshness)


@pytest.mark.parametrize("issue", ["stale", "empty", "provider_error"])
def test_completed_benchmark_failures_remain_explicit(tmp_path, monkeypatch, issue):
    original, _, _, provider, denied, _ = required_fixture(tmp_path, monkeypatch)
    execution = _with_clock(original, "2026-10-20T19:59:59+00:00")
    from trading_system.redline.execution import daily_stage_specs, record_provider

    history = provider.ohlcv("SPY", 300)
    def unavailable(*args, **kwargs):
        if issue == "provider_error":
            raise TimeoutError("offline vendor timeout https://user:secret@invalid")
        return history.loc[:"2026-10-16"] if issue == "stale" else pd.DataFrame()
    monkeypatch.setattr(provider, "ohlcv", unavailable)
    monkeypatch.setattr("trading_system.pipeline._channel_chain", lambda _: [])
    for spec in daily_stage_specs(True):
        if spec.name == "data.prepare":
            break
        with execution.step(spec.name, {}) as stage:
            stage.output({})
    with pytest.raises(StrictDataStale if issue == "stale" else ProviderDataError) as error:
        with execution.step("data.prepare", {}) as stage:
            _single_with_fallback(record_provider(provider, execution), "ohlcv", "SPY",
                                  days=300, close_cutoff=execution.completed_session_close.date())
            stage.output({})
    assert "secret" not in str(error.value)
    assert denied == []
