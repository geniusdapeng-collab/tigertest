"""Offline fixtures exercise the real 39-stage engine, without claiming live market evidence."""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import main
from trading_system import pipeline, providers
from trading_system.agent_evidence import read_snapshot, verify_events
from trading_system.data_models import PipelineResult
from trading_system.ledger_io import read_json_strict, write_json_atomic
from trading_system.providers.base import DataProvider
from trading_system.redline.execution import (
    EvidenceWriteError, ExecutionContractError, ExecutionPolicy, StrictDataStale,
    StrictDataUnavailable, daily_stage_contract, record_provider,
)
from test_strict_execution_core import make_execution


class OfflinePublicBoundaryFixture(DataProvider):
    """Test replacement for the public-provider boundary; never a production source."""
    name = "yahoo"
    synthetic = False

    def __init__(self, run_date):
        self.run_date = run_date
        self.calls = []

    def ohlcv(self, ticker, days=400):
        self.calls.append(("ohlcv", ticker, days))
        index = pd.bdate_range(end=self.run_date, periods=min(days, 280))
        slope = 0.08 + sum(map(ord, ticker)) % 7 * 0.005
        close = 70 + np.arange(len(index)) * slope + np.sin(np.arange(len(index)) / 9) * 0.2
        return pd.DataFrame({"Open": close * .999, "High": close * 1.01,
                             "Low": close * .99, "Close": close,
                             "Volume": np.full(len(index), 2_000_000)}, index=index)

    def ohlcv_batch(self, tickers, days=400, *, hard_deadline=None, progress_callback=None):
        # A small observed subset deliberately tests honest missing-stock disclosure.
        from trading_system.markets import get_market
        allowed = set(get_market("us").sector_symbols) | {"AAA", "BBB", "CCC"}
        return {ticker: self.ohlcv(ticker, days) for ticker in tickers if ticker in allowed}

    def tnx_yield(self, days=400):
        return pd.Series(4.1, index=pd.bdate_range(end=self.run_date, periods=min(days, 280)))

    def vix(self, days=400):
        return pd.Series(19.0, index=pd.bdate_range(end=self.run_date, periods=min(days, 280)))

    def vix9d(self, days=400):
        return pd.Series(18.0, index=pd.bdate_range(end=self.run_date, periods=min(days, 280)))

    def options_chain_snapshot(self, ticker):
        return {"pcr_oi": float("nan"), "atm_iv": .21, "call_oi": 100.0}


class OfflineSearchFixture:
    name = "google_news"

    def search(self, query, limit=4):
        from trading_system.search.models import RawDocument
        return [RawDocument("fixed-document", self.name, "Quarterly technology results",
                            "https://example.test/results", "A public company reports quarterly revenue and supply chain results. " * 3,
                            "2026-10-20")]


def daily_fixture(tmp_path, monkeypatch):
    from trading_system.search.hub import SearchHub
    from trading_system.simulator import SimEngine
    execution, root = make_execution(tmp_path)
    execution.policy = ExecutionPolicy("offline-test-public-boundary", provider_names=("yahoo",),
                                       search_source_names=("google_news",))
    paths = execution.paths
    write_json_atomic(paths.journal_path, [])
    write_json_atomic(paths.calibration_path, [])
    SimEngine(str(paths.sim_path)).save()
    paths.governance_path.write_text("")
    paths.calendar_override_path.write_text('{"macro":{},"earnings":{}}')
    universe = paths.work_dir / "universe.txt"
    universe.write_text("AAA\nBBB\nCCC\n")
    fixture = OfflinePublicBoundaryFixture(execution.run_date)
    factory = lambda name=None, *, execution=None: record_provider(fixture, execution)
    monkeypatch.setattr(pipeline, "get_provider", factory)
    monkeypatch.setattr(providers, "get_provider", factory)
    monkeypatch.setattr(pipeline, "default_client", lambda: pytest.fail("strict path constructed a default LLM"))
    monkeypatch.setattr(pipeline, "SearchHub", lambda *, demo=False, execution=None:
                        SearchHub([OfflineSearchFixture()], demo=False, execution=execution,
                                  budget=.2, cleanup_budget=.2))
    network_attempts = []
    def denied_network(*args, **kwargs):
        network_attempts.append("unexpected network")
        raise AssertionError("Offline fixture must never contact a network")
    monkeypatch.setattr("socket.socket.connect", denied_network)
    monkeypatch.chdir(paths.work_dir)
    monkeypatch.setenv("TS_OPTIONS_HIST_DIR", str(tmp_path / "forbidden-options"))
    monkeypatch.setenv("TS_EVENTS_CALENDAR", str(tmp_path / "forbidden-calendar.json"))
    args = SimpleNamespace(mode="daily", market="us", universe="file", universe_file=str(universe),
                           top=3, picks=2, account=100_000.0, out=str(paths.reports_dir),
                           demo=False, use_tuned=False, html=True, quiet=True)
    return execution, root, args, fixture, network_attempts


def stage_value(execution, root, name):
    record = next(row for row in execution.summary() if row["stage"] == name)
    return read_snapshot(root, record["output_ref"], run_id=execution.run_id,
                         stage=name, role="output")["value"]


def test_real_daily_39_stages_and_post_review_reports_agree(tmp_path, monkeypatch):
    execution, root, args, fixture, network_attempts = daily_fixture(tmp_path, monkeypatch)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    verified = verify_events(root, execution.run_id, daily_stage_contract())
    assert len(execution.summary()) == 39
    assert len((root / "events.jsonl").read_text().splitlines()) == 78
    assert output.result.trade_date == "2026-10-20"
    assert output.sim_state["equity_curve"][-1]["date"] == "2026-10-20"
    assert output.accounting["status"] == "updated"
    assert output.governance["ok"] is True
    assert output.review["governance_event"]["payload"]["decision"]["action"] == "review.daily"
    report = read_json_strict(output.report_paths["json"])
    review = next(row for row in report["raw"]["redline"] if row["step"] == "review.daily")
    assert review["status"] == "executed" and review["note"] == ""
    markdown = Path(output.report_paths["markdown"]).read_text()
    assert "| review.daily | ✅ |" in markdown
    assert "复盘纪要由 main.daily 尾部" not in markdown
    assert markdown == main.render_markdown(output.result) + "\n" + main._journal_section(output.journal_stats)
    html = Path(output.report_paths["html"]).read_text()
    assert "review.daily" in html
    assert "复盘纪要由 main.daily 尾部" not in html
    assert all(Path(path).is_relative_to(execution.paths.work_dir) for path in output.report_paths.values())
    assert not (tmp_path / "forbidden-options").exists()
    histories = list(execution.paths.options_history_dir.glob("*.json"))
    assert histories
    assert all(read_json_strict(path)[-1]["date"] == "2026-10-20" for path in histories)
    assert all(read_json_strict(path)[-1]["pcr_oi"] is None for path in histories)
    assert stage_value(execution, root, "report.plan")["enabled"] is False
    assert stage_value(execution, root, "search.collect")["cleanup"]["settled"] is True
    assert network_attempts == [] and fixture.calls


def test_pipeline_uses_frozen_market_date_when_host_has_crossed_midnight(tmp_path, monkeypatch):
    execution, root, args, _, _ = daily_fixture(tmp_path, monkeypatch)
    class HostTomorrow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 21, 5, 0, tzinfo=tz)
    monkeypatch.setattr(pipeline, "datetime", HostTomorrow)
    monkeypatch.setattr("trading_system.journal.datetime", HostTomorrow)
    monkeypatch.setattr("trading_system.options_metrics.datetime", HostTomorrow)
    monkeypatch.setattr("trading_system.review.chief.datetime", HostTomorrow)
    output = main._daily(args, "yahoo", execution=execution)
    assert output.result.trade_date == "2026-10-20"
    assert output.sim_state["started"] == "2026-10-20"
    assert "20261020" in output.report_paths["review"]
    assert all(record["date"] == "2026-10-20"
               for path in execution.paths.options_history_dir.glob("*.json")
               for record in read_json_strict(path))


@pytest.mark.parametrize("corruption", ["journal", "sim", "calendar", "calibration"])
def test_corrupt_staged_input_stops_before_success_or_account_publication(tmp_path, monkeypatch, corruption):
    execution, root, args, _, _ = daily_fixture(tmp_path, monkeypatch)
    path = {"journal": execution.paths.journal_path, "sim": execution.paths.sim_path,
            "calendar": execution.paths.calendar_override_path,
            "calibration": execution.paths.calibration_path}[corruption]
    path.write_text('{"duplicate":1,"duplicate":2}')
    with pytest.raises((ValueError, ExecutionContractError)):
        main._daily(args, "yahoo", execution=execution)
    assert not any(row["stage"] == "daily.result" for row in execution.summary())
    assert not (root / "receipt.json").exists()
    assert path.read_text() == '{"duplicate":1,"duplicate":2}'


@pytest.mark.parametrize("failure", ["governance", "review", "final_markdown"])
def test_tail_failure_never_becomes_daily_success(tmp_path, monkeypatch, failure):
    execution, root, args, _, _ = daily_fixture(tmp_path, monkeypatch)
    if failure == "governance":
        execution.paths.governance_path.write_text('{"invalid":true}\n')
    elif failure == "review":
        from trading_system.review.chief import ReviewChief
        monkeypatch.setattr(ReviewChief, "daily", lambda *a, **k: (_ for _ in ()).throw(OSError("memo disk full")))
    else:
        import trading_system.ledger_io as ledger
        original = ledger.write_text_atomic
        def fail_markdown(path, value):
            if Path(path).name.startswith("日报_") and "胜率追踪" in value:
                raise OSError("final markdown disk full")
            return original(path, value)
        monkeypatch.setattr(ledger, "write_text_atomic", fail_markdown)
    with pytest.raises((ValueError, OSError)):
        main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["status"] == "failed"
    assert not any(row["stage"] == "daily.result" for row in execution.summary())


def test_evidence_failure_inside_provider_fallback_is_never_swallowed(tmp_path, monkeypatch):
    execution, root, args, _, _ = daily_fixture(tmp_path, monkeypatch)
    original = execution.store.snapshot
    def fail_provider_output(**kwargs):
        if kwargs["stage"] == "data.prepare" and isinstance(kwargs["value"], pd.DataFrame):
            raise OSError("evidence volume full")
        return original(**kwargs)
    monkeypatch.setattr(execution.store, "snapshot", fail_provider_output)
    with pytest.raises(EvidenceWriteError):
        main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["stage"] == "data.prepare"
    assert execution.summary()[-1]["status"] == "failed"


def test_strict_report_projection_discloses_nan_without_mutating_strategy(tmp_path):
    from trading_system.report import to_json
    result = PipelineResult("2026-10-20", "yahoo", raw={"missing": float("nan"), "ratio": float("inf")})
    path = to_json(result, str(tmp_path), strict=True)
    actual = read_json_strict(path)
    assert actual["raw"]["missing"] is None and actual["raw"]["ratio"] is None
    reasons = actual["raw"]["serialization"]["nonfinite_values"]
    assert {item["path"] for item in reasons} == {"/raw/missing", "/raw/ratio"}
    assert math.isnan(result.raw["missing"]) and math.isinf(result.raw["ratio"])
    result.raw["unsupported"] = object()
    with pytest.raises(TypeError):
        to_json(result, str(tmp_path), strict=True)
    assert read_json_strict(path) == actual


def test_strict_options_history_rejects_bad_prior_data_and_write_failure(tmp_path, monkeypatch):
    from trading_system.options_metrics import OptionsHistoryStore
    store = OptionsHistoryStore(tmp_path, strict=True, as_of_date=date(2026, 10, 20))
    path = tmp_path / "AAA.json"
    path.write_text('[{"date":"2026-10-19","pcr_oi":NaN,"atm_iv":0.2,"call_oi":2}]')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        store.append("AAA", {"pcr_oi": 1, "atm_iv": .2, "call_oi": 10})
    assert path.read_bytes() == before
    path.write_text("[]")
    monkeypatch.setattr("trading_system.options_metrics.write_json_atomic",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("storage failure")))
    with pytest.raises(OSError):
        store.append("AAA", {"pcr_oi": 1, "atm_iv": .2, "call_oi": 10})
    assert path.read_text() == "[]"
    with pytest.raises(ValueError):
        store.load("../escape")


def test_strict_cache_reset_rejects_outside_symlink_and_retains_accounting(tmp_path):
    from trading_system.state import purge_run_state
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.txt"
    protected.write_text("must remain")
    work = tmp_path / "work"
    (work / "cache").mkdir(parents=True)
    (work / "cache" / "search").symlink_to(outside, target_is_directory=True)
    journal = work / "journal.json"
    journal.write_text("[]")
    with pytest.raises(ValueError):
        purge_run_state(str(work), strict=True)
    assert protected.read_text() == "must remain" and journal.read_text() == "[]"


def test_strict_review_rejects_corrupt_proposal_instead_of_hiding_it(tmp_path):
    from trading_system.review.monthly import list_proposals
    path = tmp_path / "PROP-invalid.json"
    path.write_text('{"status":"pending_review"}')
    with pytest.raises(TypeError):
        list_proposals(str(tmp_path), strict=True)


@pytest.mark.parametrize("data_issue", ["stale", "short", "empty"])
def test_data_failures_have_the_public_failure_codes(tmp_path, monkeypatch, data_issue):
    execution, root, args, fixture, _ = daily_fixture(tmp_path, monkeypatch)
    original = fixture.ohlcv
    def bad_benchmark(ticker, days=400):
        frame = original(ticker, days)
        if ticker == "SPY":
            if data_issue == "stale":
                frame.index = frame.index - pd.Timedelta(days=30)
            elif data_issue == "short":
                frame = frame.tail(20)
            else:
                frame = frame.iloc[:0]
        return frame
    monkeypatch.setattr(fixture, "ohlcv", bad_benchmark)
    expected = StrictDataStale if data_issue == "stale" else StrictDataUnavailable
    with pytest.raises(expected) as error:
        main._daily(args, "yahoo", execution=execution)
    assert error.value.code == ("DATA_STALE" if data_issue == "stale" else "DATA_UNAVAILABLE")
    assert execution.summary()[-1]["stage"] == "data.prepare"


def test_final_json_is_rechecked_against_actual_result(tmp_path, monkeypatch):
    execution, root, args, _, _ = daily_fixture(tmp_path, monkeypatch)
    import trading_system.report_html as html
    original = html.render_html
    def tampering_render(*args, **kwargs):
        json_path = execution.paths.reports_dir / "result_20261020.json"
        value = read_json_strict(json_path)
        value["action"] = "FORGED"
        write_json_atomic(json_path, value)
        return original(*args, **kwargs)
    monkeypatch.setattr(html, "render_html", tampering_render)
    with pytest.raises(OSError, match="Final JSON differs"):
        main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["stage"] == "daily.result"
    assert execution.summary()[-1]["status"] == "failed"


def test_journal_settlement_uses_admission_date_not_host_date(tmp_path):
    from trading_system.journal import Journal
    journal = Journal(tmp_path / "journal.json")
    journal.records = [{"date": "2026-10-19", "ticker": "AAA", "status": "open",
                        "stop": 90.0, "entry_ref": 100.0,
                        "account_currency": "USD", "quote_currency": "USD"}]
    index = pd.to_datetime(["2026-10-19", "2026-10-20", "2026-10-21"])
    frame = pd.DataFrame({"Open": [100., 100., 95.], "High": [102., 102., 99.],
                          "Low": [99., 99., 80.], "Close": [100., 101., 89.]}, index=index)
    source = SimpleNamespace(ohlcv=lambda *args, **kwargs: frame)
    assert journal.settle(source, as_of_date=date(2026, 10, 20)) == 0
    assert journal.records[0]["status"] == "open"
    assert journal.records[0]["entry_date"] == "2026-10-20"
    assert journal.settle(source, as_of_date=date(2026, 10, 21)) == 1
    assert journal.records[0]["exit_date"] == "2026-10-21"
