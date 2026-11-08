"""Scope expansion is a new research admission, never an old trade approval."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from test_task_work_units import manifest
from trading_system.research_followup import pending_candidates, create_scope, apply_scope, candidate_outcomes


def discovery():
    frontier = {"symbols": ["AAA"], "universe": ["AAA", "BBB"],
        "security_ids": {"AAA": "us:AAA", "BBB": "us:BBB"},
        "as_of": "2026-11-08T00:00:00+00:00", "manifest_sha256": "4" * 64,
        "selection_reasons": {"AAA": ["market_rank"]}, "missing": [],
        "market_snapshot_refs": {"stage_output": {"sha256": "5" * 64}, "stock_sha256": {"AAA": "6" * 64}},
        "not_a_trade_approval": True}
    scans = {"AAA": {"hard_filter": "pass", "selection": "baseline"},
        "BBB": {"hard_filter": "pass", "selection": "research_missing", "market_selection": "supplement",
                "rank_score": 87}}
    return frontier, scans, pending_candidates(frontier, scans, manifest())


def test_scope_preserves_original_discovery_and_requires_full_research():
    frontier, _, pending = discovery()
    original = deepcopy(frontier)
    scope = create_scope(pending, ["BBB"], parent_receipt_sha256="7" * 64,
        requested_at=datetime(2026, 11, 8, 0, 30, tzinfo=timezone.utc))
    next_frontier = apply_scope({**frontier, "as_of": "2026-11-08T01:00:00+00:00"}, scope,
        {"BBB": {"frozen": "new market observation"}})
    assert next_frontier["symbols"] == ["AAA", "BBB"]
    assert next_frontier["selection_reasons"]["BBB"] == ["authorized_pending_research"]
    assert next_frontier["manifest_sha256"] != frontier["manifest_sha256"]
    assert frontier == original and scope["reuse_trade_approval"] is False
    assert scope["required_work"] == ["new_market_observation", "required_evidence", "all_semantic_analyses", "numeric_risk", "account_compare_and_swap"]


@pytest.mark.parametrize("symbols", [["CCC"], ["AAA"], ["BBB", "BBB"], []])
def test_unreviewed_or_unrequested_scope_cannot_expand(symbols):
    _, _, pending = discovery()
    with pytest.raises(ValueError):
        create_scope(pending, symbols, parent_receipt_sha256="7" * 64,
            requested_at=datetime(2026, 11, 8, 0, 30, tzinfo=timezone.utc))


def test_expired_candidate_is_a_new_question_and_never_an_old_order():
    _, _, pending = discovery()
    scope = create_scope(pending, ["BBB"], parent_receipt_sha256="7" * 64,
        requested_at=datetime(2026, 11, 9, tzinfo=timezone.utc))
    assert scope["original_signal_expired"] is True and scope["mode"] == "new_research_admission"
    assert scope["reuse_trade_approval"] is False


def test_missing_or_changed_security_remains_pending():
    frontier, _, pending = discovery()
    scope = create_scope(pending, ["BBB"], parent_receipt_sha256="7" * 64,
        requested_at=datetime(2026, 11, 8, 0, 30, tzinfo=timezone.utc))
    frontier["as_of"] = "2026-11-08T01:00:00+00:00"
    with pytest.raises(ValueError, match="SCOPE_SECURITY"):
        apply_scope({**frontier, "security_ids": {"AAA": "us:AAA"}}, scope, {})
    next_frontier = apply_scope(frontier, scope, {})
    assert next_frontier["symbols"] == ["AAA"]
    assert next_frontier["missing"][-1]["reason"] == "market_observation_missing"


def test_candidate_state_distinguishes_expiry_and_completed_partial_research():
    _, _, pending = discovery()
    state = candidate_outcomes(pending, None, observed_at=datetime(2026, 11, 9, tzinfo=timezone.utc))
    assert state["rows"][0]["status"] == "expired_pending_research"
    from trading_system.research_followup import _outcome_rows
    successor = {"run_id": "run-" + "b" * 32,
        "research_sufficient": False, "selected_symbols": [], "researched_symbols": ["BBB"]}
    state = _outcome_rows(pending, successor=successor, observed_at=datetime(2026, 11, 9, tzinfo=timezone.utc))
    assert state["rows"][0]["status"] == "pending_evidence"


def test_tampered_discovery_and_unverified_successor_fail_closed():
    _, _, pending = discovery()
    altered = deepcopy(pending); altered["candidates"][0]["symbol"] = "CCC"
    with pytest.raises(ValueError):
        create_scope(altered, ["CCC"], parent_receipt_sha256="7" * 64,
            requested_at=datetime(2026, 11, 8, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        candidate_outcomes(pending, {"verified": False}, observed_at=datetime(2026, 11, 9, tzinfo=timezone.utc))


def test_new_frontier_replays_real_market_frames_with_bound_scope():
    from test_intelligence_workflow_v2 import market
    from trading_system.intelligence_workflow import build_frontier, verify_frontier, value_hash
    from trading_system.redline.execution import ExecutionContractError
    data = market()
    universe = ["MU", "NVDA", "AAPL"]
    options = {"as_of": datetime(2026, 11, 8, tzinfo=timezone.utc),
               "market_snapshot_ref": {"sha256": "5" * 64}, "top_n": 1}
    ids = {symbol: "us:" + symbol for symbol in universe}
    parent = build_frontier(data, universe, ids, **options)
    pending_symbol = next(symbol for symbol in universe if symbol not in parent["symbols"])
    decisions = {symbol: {"hard_filter": "pass", "selection": "baseline"} for symbol in universe}
    decisions[pending_symbol].update(selection="research_missing", market_selection="supplement")
    pending = pending_candidates(parent, decisions, manifest())
    scope = create_scope(pending, [pending_symbol], parent_receipt_sha256="7" * 64,
                         requested_at=datetime(2026, 11, 8, 0, 30, tzinfo=timezone.utc))
    options["as_of"] = datetime(2026, 11, 8, 1, tzinfo=timezone.utc)
    actual = build_frontier(data, universe, ids, research_scope=scope, **options)
    assert actual["symbols"] == [*parent["symbols"], pending_symbol]
    assert actual["market_snapshot_refs"]["stock_sha256"][pending_symbol] == value_hash(data["stock_ohlcv"][pending_symbol])
    assert actual["supplement_scope_sha256"] == scope["manifest_sha256"]
    assert pending_symbol not in parent["symbols"]
    verify_frontier(actual, data, universe, research_scope=scope, **options)
    with pytest.raises(ExecutionContractError):
        verify_frontier(actual, data, universe, **options)
    wrong_market = deepcopy(data)
    wrong_market["stock_ohlcv"][pending_symbol].iloc[-1, 0] += 1
    with pytest.raises(ExecutionContractError):
        verify_frontier(actual, wrong_market, universe, research_scope=scope, **options)


def test_admitted_scope_is_loaded_from_bound_bytes_and_cannot_be_injected(tmp_path):
    from types import SimpleNamespace
    from trading_system.agent_evidence import atomic_json
    from trading_system.research_work import attach_research_work, research_work_policy
    execution = SimpleNamespace(intelligence=True, run_id=manifest()["run_id"], store=SimpleNamespace(root=tmp_path))
    admission = {"run_id": execution.run_id, "request_sha256": "3" * 64,
        "source_sha256": "1" * 64, "policy_sha256": "2" * 64, "parent": None,
        "clock": {"admitted_at_utc": "2026-11-08T01:00:00+00:00",
                  "deadline_at_utc": "2026-11-08T02:00:00+00:00"}}
    _, _, pending = discovery()
    scope = create_scope(pending, ["BBB"], parent_receipt_sha256="7" * 64,
                         requested_at=datetime(2026, 11, 8, 0, 30, tzinfo=timezone.utc))
    policy = {"research_work": research_work_policy(), "research_scope_sha256": scope["manifest_sha256"]}
    admission["research_scope_sha256"] = scope["manifest_sha256"]
    atomic_json(execution.store.root, "research-scope.json", scope, exclusive=True)
    attach_research_work(execution, admission, policy)
    assert execution.research_scope == scope
    broken = {**admission, "research_scope_sha256": "b" * 64}
    with pytest.raises(ValueError, match="SCOPE"):
        attach_research_work(execution, broken, policy)
