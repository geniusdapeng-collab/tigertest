"""Independent numeric replay rejects rehashed score/approval substitutions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import copy
import shutil
from dataclasses import asdict
from datetime import date

import pytest
import numpy as np
import pandas as pd

import main
from trading_system.agent_evidence import (EvidenceInvalid, atomic_bytes, atomic_json,
    normalize, read_bytes, read_json, verify_events, verify_ref)
from trading_system.agent_runtime import account_files
from trading_system.data_models import ChainState, TradePick
from trading_system.intelligence_scoring_proof import CONTRACT, STAGES, _Trace, _linkage, verify_intelligence_scoring
from trading_system.markets import get_market
from trading_system.llm.intelligence_proof import verify_intelligence_model
from trading_system.redline.execution import ExecutionContractError, daily_stage_contract
from trading_system.tech_chain.agents import CycleLinkageAgent, TechChainMonitorAgent
from test_intelligence_workflow_v2 import intelligence_fixture, market
from test_model_required_temporal import _rewrite_events, _save_snapshot


def _stages(root):
    return {row["stage"]: row for line in read_bytes(root, "events.jsonl").splitlines()
            if (row := json.loads(line))["kind"] == "end"}


def _admission(execution):
    return {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                       "admitted_at_utc": execution.admitted_at.isoformat()}}


def _freeze_before(execution, root):
    refs = account_files(execution.paths.reports_dir)
    atomic_json(root, "account-input.json", {"schema": "tiger.account-input.v1", "account": "public-usd",
                                           "parent": None, "files": refs})
    for ref in refs:
        atomic_bytes(root, "account-before/" + ref["path"], verify_ref(execution.paths.reports_dir, ref))


def _run(tmp_path, monkeypatch, *, account_bound=True, options_failure=False, numeric_buys=False,
         monitor_failure=False):
    execution, root, args, provider, denied, _ = intelligence_fixture(tmp_path, monkeypatch)
    if monitor_failure:
        original_batch = provider.ohlcv_batch
        def batch(symbols, days=400):
            if execution._active.spec.name == "tech.monitor":
                raise TimeoutError("Offline observed monitor batch failure")
            return original_batch(symbols, days)
        monkeypatch.setattr(provider, "ohlcv_batch", batch)
    if numeric_buys:
        original = provider.ohlcv
        sectors = set(get_market("us").sector_symbols)
        def strong_market(symbol, days=400):
            if symbol == "SPY":
                return original(symbol, days)
            index = pd.bdate_range(end=execution.run_date, periods=min(days, 280))
            step = np.arange(len(index), dtype=float)
            close = 70 * np.exp(.0008 * step + .000006 * step ** 2)
            high, low = close * 1.04, close * .96
            volume = np.full(len(index), 2_000_000.)
            if symbol not in sectors:
                high[-14:], low[-14:] = close[-14:] * 1.002, close[-14:] * .998
                close[-1] = max(high[-21:-1]) * 1.005
                high[-1], low[-1], volume[-1] = close[-1] * 1.003, close[-1] * .995, 8_000_000.
            return pd.DataFrame({"Open": close * .999, "High": high, "Low": low,
                                 "Close": close, "Volume": volume}, index=index)
        monkeypatch.setattr(provider, "ohlcv", strong_market)
    if options_failure:
        def fail(ticker):
            raise TimeoutError("Offline observed source failure")
        monkeypatch.setattr(provider, "options_chain_snapshot", fail)
    if account_bound:
        _freeze_before(execution, root)
    result = main._daily(args, "yahoo", execution=execution)
    assert denied == []
    return execution, root, result


def _hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture(scope="module")
def scoring_baseline(tmp_path_factory):
    """One real complete run; every mutation still receives all sealed bytes.

    The final directory component remains ``run`` in each copy because snapshot
    identities are bound to that run ID. Absolute deployment paths within the
    input/I/O tape remain original metadata and are never opened during replay.
    Source documents, model receipts, raw market observations and account inputs
    are copied together rather than replaced by mocked proof return values.
    """
    workspace = tmp_path_factory.mktemp("numeric_scoring_baseline")
    with pytest.MonkeyPatch.context() as patch:
        execution, root, result = _run(workspace, patch)
        execution.assert_complete()
        verified = verify_events(root, execution.run_id, daily_stage_contract(True, intelligence=True))
        assert verified["stage_count"] == 41 and not verified["degraded"]
        admission = _admission(execution)
        proof = verify_intelligence_scoring(root, _stages(root), admission)
        assert proof["stages"] == list(STAGES) and proof["account_input_bound"] is True
        return root, admission, normalize(result.result)


@pytest.fixture
def scoring_run(tmp_path, scoring_baseline):
    baseline, admission, result = scoring_baseline
    root = tmp_path / baseline.name
    shutil.copytree(baseline, root)
    assert root.name == baseline.name == "run"
    assert _hashes(root) == _hashes(baseline)
    return root, copy.deepcopy(admission), copy.deepcopy(result)


def test_all_eight_numeric_stages_replay_without_network_or_ledger_writes(scoring_run, monkeypatch):
    root, admission, _ = scoring_run
    before = _hashes(root)
    monkeypatch.setattr("trading_system.options_metrics.write_json_atomic", lambda *a, **k: pytest.fail("Replay invoked ledger writer"))
    monkeypatch.setattr("trading_system.providers.get_provider", lambda *a, **k: pytest.fail("Replay constructed a provider"))
    actual = verify_intelligence_scoring(root, _stages(root), admission)
    assert actual["contract"] == CONTRACT and actual["stages"] == list(STAGES)
    assert actual["account_input_bound"] is True
    assert actual["ledger_writes"] == actual["network_requests"] == 0
    assert actual["researched_candidates"] == 3
    assert _hashes(root) == before


def test_fixture_without_account_commitment_is_explicit_not_silently_certified(scoring_run):
    root, admission, _ = scoring_run
    (root / "account-input.json").unlink()
    actual = verify_intelligence_scoring(root, _stages(root), admission)
    assert actual["account_input_bound"] is False


def test_observed_options_failure_replays_missing_dimension_without_retry(tmp_path, monkeypatch):
    execution, root, _ = _run(tmp_path, monkeypatch, options_failure=True)
    actual = verify_intelligence_scoring(root, _stages(root), _admission(execution))
    assert actual["external_inputs_replayed"] is True


def test_numeric_approved_entries_recompute_risk_sizing_and_caps_from_raw_market(tmp_path, monkeypatch):
    execution, root, _ = _run(tmp_path, monkeypatch, numeric_buys=True)
    stage = _stages(root)["layer4.risk"]
    risk = read_json(root, stage["output_ref"]["path"])["value"]["value"]
    assert risk["picks"], "Fixture must exercise approved numeric risk/sizing, before the semantic veto"
    assert all(row["fields"]["entry_template"] == "B" for row in risk["picks"])
    assert all(0 < row["fields"]["position_pct"] <= .2 for row in risk["picks"])
    actual = verify_intelligence_scoring(root, _stages(root), _admission(execution))
    assert actual["numeric_approved"] == [row["fields"]["ticker"] for row in risk["picks"]]


@pytest.mark.parametrize("stage", list(STAGES))
def test_rehashed_score_shortlist_and_approval_output_still_fail_formula_replay(scoring_run, stage):
    root, admission, _ = scoring_run
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    row = next(row for row in events if row["stage"] == stage and row["kind"] == "end")
    blob = read_json(root, row["output_ref"]["path"])
    value = blob["value"]["value"]
    if stage == "tech.monitor":
        next(row for row in value if row["fields"]["chain_mom20"] is not None)["fields"]["chain_mom20"] += .1
    elif stage == "tech.cycle_linkage":
        value[0]["fields"]["rs_vs_spy"] += .1
    elif stage == "layer1.mrs":
        value["fields"]["mrs_star"] += 1
    elif stage == "layer2.sector":
        value["sectors"][0]["fields"]["shs"] += 1
    elif stage == "layer2b.chain":
        value["chains"].append(normalize(ChainState("forged", "Forged", 9, "expansion")))
    elif stage == "layer0.scan":
        value["candidates"][0]["fields"]["rank_score"] += 1
    elif stage == "layer3.tss":
        value["watchlist"][0]["fields"]["tss_final"] += 1
    else:
        value["picks"] = [normalize(TradePick("AAPL", 9, 9, "A", 100, 90, 999, .999, 9990))]
        value["action"] = "BUY"
    _rewrite_events(root, events, [(row["output_ref"], _save_snapshot(root, blob))])
    # Generic hashes, snapshot references and the full event chain remain valid.
    verify_events(root, root.name, daily_stage_contract(True, intelligence=True))
    with pytest.raises(EvidenceInvalid, match="deterministic") as error:
        verify_intelligence_scoring(root, _stages(root), admission)
    assert error.value.stage == stage


def test_options_read_cannot_override_committed_before_history_even_when_rehashed(scoring_run):
    root, admission, _ = scoring_run
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    row = next(row for row in events if row["stage"] == "layer3.tss" and row["kind"] == "end")
    blob = read_json(root, row["output_ref"]["path"])
    call = next(call for call in blob["value"]["io_calls"] if call["method"] == "options.history.read")
    history = read_json(root, call["output_ref"]["path"])
    history["value"] = [{"date": "2026-10-19", "pcr_oi": .8, "atm_iv": .2, "call_oi": 1000}]
    call["output_ref"] = _save_snapshot(root, history)
    _rewrite_events(root, events, [(row["output_ref"], _save_snapshot(root, blob))])
    verify_events(root, root.name, daily_stage_contract(True, intelligence=True))
    with pytest.raises(EvidenceInvalid) as error:
        verify_intelligence_scoring(root, _stages(root), admission)
    assert error.value.stage == "layer3.tss"


def test_unexpected_or_unused_provider_receipt_is_not_ignored(scoring_run):
    root, admission, _ = scoring_run
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    row = next(row for row in events if row["stage"] == "layer1.mrs" and row["kind"] == "end")
    blob = read_json(root, row["output_ref"]["path"])
    blob["value"]["io_calls"][0]["method"] = "provider.yahoo.ohlcv"
    _rewrite_events(root, events, [(row["output_ref"], _save_snapshot(root, blob))])
    verify_events(root, root.name, daily_stage_contract(True, intelligence=True))
    with pytest.raises(EvidenceInvalid) as error:
        verify_intelligence_scoring(root, _stages(root), admission)
    assert error.value.stage == "layer1.mrs"


def test_caller_cannot_supply_a_different_end_event_mapping(scoring_run):
    root, admission, _ = scoring_run
    stages = _stages(root)
    stages.pop("layer4.risk")
    with pytest.raises(EvidenceInvalid):
        verify_intelligence_scoring(root, stages, admission)


@pytest.mark.parametrize("stage", ["tech.monitor", "tech.cycle_linkage"])
def test_coherent_downstream_rehash_cannot_promote_invented_momentum(tmp_path, monkeypatch, stage):
    """Compute all downstream formulas from a false upstream value, then seal.

    This models a coherent rewritten event chain rather than changing only one
    output hash. Market/provider observations and all model responses remain
    genuine fixture inputs. The original rule implementation is restored before
    the independent full source/model/fusion/numeric verifier runs.
    """
    cls = TechChainMonitorAgent if stage == "tech.monitor" else CycleLinkageAgent
    original = cls.execute
    substitutions = []

    def forged(self, *args, **kwargs):
        rows = original(self, *args, **kwargs)
        if stage == "tech.monitor":
            row = next(row for row in rows if row.chain_mom20 is not None)
            substitutions.append(row.chain_mom20)
            row.chain_mom20 = .99
        else:
            row = next(row for row in rows if row.rs_vs_spy is not None)
            substitutions.append(row.rs_vs_spy)
            row.rs_vs_spy = .99
        return rows

    with pytest.MonkeyPatch.context() as substitution:
        substitution.setattr(cls, "execute", forged)
        execution, root, result = _run(tmp_path, monkeypatch)
    assert substitutions and cls.execute is original
    verify_events(root, execution.run_id, daily_stage_contract(True, intelligence=True))
    with pytest.raises(EvidenceInvalid, match="deterministic") as error:
        verify_intelligence_model(root, asdict(result.result), _admission(execution))
    assert error.value.stage == stage


def test_monitor_observed_failed_batch_and_individual_retry_frames_recompute(tmp_path, monkeypatch):
    execution, root, _ = _run(tmp_path, monkeypatch, monitor_failure=True)
    stage = _stages(root)["tech.monitor"]
    calls = read_json(root, stage["output_ref"]["path"])["value"]["io_calls"]
    assert "error_ref" in calls[0] and calls[0]["method"] == "provider.yahoo.ohlcv_batch"
    assert any(row["method"] == "provider.yahoo.ohlcv" for row in calls[1:])
    assert verify_intelligence_scoring(root, _stages(root), _admission(execution))["network_requests"] == 0


def test_linkage_observed_fallback_reconstructs_closes_and_rejects_unknown_source():
    data = market()
    frame = data["spy"]
    calls = [({"method": "provider.yahoo.ohlcv", "error_ref": {}},
              {"provider": "yahoo", "method": "ohlcv", "args": ["TSM"], "kwargs": {"days": 90}}, {}),
             ({"method": "provider.official.available"}, {"provider": "official"}, False),
             ({"method": "provider.stooq.ohlcv"},
              {"provider": "stooq", "method": "ohlcv", "args": ["TSM"], "kwargs": {"days": 90}}, normalize(frame))]
    rows = _linkage(data, _Trace(calls), "yahoo", ["yahoo", "official", "stooq"],
                    date(2026, 10, 20), date(2026, 10, 20))
    tsm = next(row for row in rows if row.ticker == "TSM")
    assert tsm.mom20 == float(frame["Close"].iloc[-1] / frame["Close"].iloc[-21] - 1)
    assert tsm.rs_vs_spy == 0
    altered = copy.deepcopy(calls)
    altered[-1][0]["method"] = "provider.unreviewed.ohlcv"
    with pytest.raises(ExecutionContractError):
        _linkage(data, _Trace(altered), "yahoo", ["yahoo", "official", "stooq"],
                 date(2026, 10, 20), date(2026, 10, 20))


@pytest.mark.parametrize("source,availability,accepted", [
    ("tencent", True, True), ("tencent", False, False),
    ("tencent", None, False), ("official", True, False),
])
def test_linkage_fallback_requires_a_true_capable_source_probe(source, availability, accepted):
    data = market()
    calls = [({"method": "provider.yahoo.ohlcv", "error_ref": {}},
              {"provider": "yahoo", "method": "ohlcv", "args": ["TSM"], "kwargs": {"days": 90}}, {})]
    if availability is not None:
        calls.append(({"method": "provider." + source + ".available"}, {"provider": source}, availability))
    calls.append(({"method": "provider." + source + ".ohlcv"},
        {"provider": source, "method": "ohlcv", "args": ["TSM"], "kwargs": {"days": 90}}, normalize(data["spy"])))
    if accepted:
        rows = _linkage(data, _Trace(calls), "yahoo", ["yahoo", source],
                        date(2026, 10, 20), date(2026, 10, 20))
        assert any(row.ticker == "TSM" for row in rows)
    else:
        with pytest.raises(ExecutionContractError, match="availability"):
            _linkage(data, _Trace(calls), "yahoo", ["yahoo", source],
                     date(2026, 10, 20), date(2026, 10, 20))
