"""A missing serial source must yield to trusted fallbacks within a bounded budget."""
from __future__ import annotations

import logging

import pandas as pd
import pytest

from trading_system import pipeline
from trading_system.agent_evidence import read_snapshot
from trading_system.providers.stooq import StooqProvider
from trading_system.redline.execution import (
    EvidenceWriteError, ExecutionContractError, StrictObservationError, record_provider,
)
from test_strict_execution_core import advance_to, make_execution


def frame():
    return pd.DataFrame({"Open": [100., 100., 100.], "High": [101., 101., 101.],
        "Low": [99., 99., 99.], "Close": [100., 100., 100.], "Volume": [1000, 1000, 1000]},
        index=pd.date_range("2026-10-18", periods=3))


def test_serial_source_exhausts_three_no_outputs_without_logging_error_bodies(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    provider = StooqProvider(allow_challenge=False, max_consecutive_no_output=3)
    calls = []
    def unavailable(ticker, days=400):
        calls.append((ticker, days))
        raise TimeoutError("TEST_SECRET_SENTINEL never serialize the body")
    monkeypatch.setattr(provider, "ohlcv", unavailable)
    assert provider.ohlcv_batch(list("ABCDEF"), 420) == {}
    assert calls == [(t, 420) for t in "ABC"]
    status = provider.last_batch_status
    assert status["source_exhausted"] is True
    assert status["reason"] == "availability_budget_exhausted"
    assert status["unattempted_symbols"] == list("DEF")
    assert status["errors"] == {"TimeoutError": 3}
    assert "TEST_SECRET_SENTINEL" not in caplog.text


def test_valid_output_resets_budget_and_is_preserved_byte_for_byte(monkeypatch):
    provider = StooqProvider(allow_challenge=False, max_consecutive_no_output=3)
    observed = frame()
    answers = {"C": observed}
    calls = []
    def fetch(ticker, days=400):
        calls.append(ticker)
        return answers.get(ticker)
    monkeypatch.setattr(provider, "ohlcv", fetch)
    result = provider.ohlcv_batch(list("ABCDEFGH"), 420)
    assert result["C"] is observed
    assert calls == list("ABCDEF")  # AB misses, C resets, DEF misses, GH left for next source.
    assert provider.last_batch_status["empty_observations"] == 5
    assert provider.last_batch_status["unattempted_symbols"] == list("GH")
    assert provider.last_batch_status["errors"] == {}


@pytest.mark.parametrize("symbols", [[], ["", " ", None]])
def test_empty_input_is_not_source_exhaustion(monkeypatch, symbols):
    provider = StooqProvider(allow_challenge=False, max_consecutive_no_output=3)
    monkeypatch.setattr(provider, "ohlcv", lambda *_a, **_k: pytest.fail("No request expected"))
    assert provider.ohlcv_batch(symbols, 420) == {}
    assert not provider.last_batch_status["source_exhausted"]
    assert provider.last_batch_status["attempted"] == 0
    assert provider.last_batch_status["errors"] == {}


def test_ordinary_missing_symbol_and_fully_observed_empty_batch_do_not_claim_source_failure(monkeypatch):
    provider = StooqProvider(allow_challenge=False, max_consecutive_no_output=3)
    monkeypatch.setattr(provider, "ohlcv", lambda *_a, **_k: None)
    for symbols in [["MISSING"], list("ABC")]:
        assert provider.ohlcv_batch(symbols, 420) == {}
        assert not provider.last_batch_status["source_exhausted"]
        assert provider.last_batch_status["empty_observations"] == len(symbols)
        assert provider.last_batch_status["errors"] == {}


@pytest.mark.parametrize("invalid", [None, pd.DataFrame(),
    pd.DataFrame({"Close": [float("nan")]}), [100]])
def test_invalid_observation_does_not_reset_availability_budget(monkeypatch, invalid):
    provider = StooqProvider(allow_challenge=False, max_consecutive_no_output=3)
    monkeypatch.setattr(provider, "ohlcv", lambda *_a, **_k: invalid)
    assert provider.ohlcv_batch(list("ABCDE"), 420) == {}
    assert provider.last_batch_status["attempted"] == 3
    assert provider.last_batch_status["source_exhausted"]


@pytest.mark.parametrize("error_type", [EvidenceWriteError, ExecutionContractError, StrictObservationError])
def test_contract_and_evidence_faults_never_become_source_exhaustion(monkeypatch, error_type):
    provider = StooqProvider(allow_challenge=False, max_consecutive_no_output=3)
    def fatal(*_a, **_k):
        raise error_type("TEST_FIXTURE fatal execution boundary")
    monkeypatch.setattr(provider, "ohlcv", fatal)
    with pytest.raises(error_type):
        provider.ohlcv_batch(list("ABCDE"), 420)
    assert not provider.last_batch_status["source_exhausted"]


@pytest.mark.parametrize("invalid", [0, -1, True, "3"])
def test_invalid_budget_rejected_before_any_network(invalid):
    with pytest.raises(ValueError):
        StooqProvider(allow_challenge=False, max_consecutive_no_output=invalid)


def test_legacy_optional_unbounded_source_behavior_is_preserved(monkeypatch):
    provider = StooqProvider()
    calls = []
    def empty(ticker, days=400):
        calls.append(ticker)
        return None
    monkeypatch.setattr(provider, "ohlcv", empty)
    assert provider.ohlcv_batch(list("ABCDE"), 420) == {}
    assert calls == list("ABCDE")
    assert not provider.last_batch_status["source_exhausted"]


def test_strict_fallback_records_exhaustion_and_reaches_existing_trusted_source(tmp_path, monkeypatch):
    execution, root = make_execution(tmp_path)
    advance_to(execution, "data.prepare")
    monkeypatch.setattr(pipeline, "_LINEAGE", [])
    monkeypatch.setattr(pipeline, "_DATA_ISSUES", [])
    constructors, source_calls = [], []
    class Primary:
        name, synthetic = "yahoo", False
        def ohlcv_batch(self, tickers, days=400):
            return {}
    class Backup:
        name, synthetic = "sina", False
        def ohlcv_batch(self, tickers, days=400):
            source_calls.append(list(tickers))
            return {ticker: frame() for ticker in tickers}
    def stooq_factory(**kwargs):
        constructors.append(kwargs)
        provider = StooqProvider(**kwargs)
        provider.ohlcv = lambda *_a, **_k: None
        return provider
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", stooq_factory)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda _execution: [record_provider(Backup(), execution)])
    with execution.step("data.prepare", {"fixture": "TEST_FIXTURE"}) as stage:
        result = pipeline._batch_with_fallback(record_provider(Primary(), execution), list("ABCDEF"), 420)
        stage.output({"data": result, "issues": pipeline._DATA_ISSUES})
    assert constructors == [{"allow_challenge": False, "max_consecutive_no_output": 3}]
    assert set(result) == set("ABCDEF") and source_calls == [list("ABCDEF")]
    issue = next(i for i in pipeline._DATA_ISSUES if i["issue"] == "source_exhausted")
    assert issue["provider"] == "stooq" and issue["symbols"] == list("DEF")
    assert issue["availability"]["attempted"] == 3
    assert issue["availability"]["service_failure_asserted"] is False
    recorded = read_snapshot(root, execution.summary()[-1]["output_ref"],
        run_id=execution.run_id, stage="data.prepare", role="output")
    assert issue in recorded["value"]["issues"]
