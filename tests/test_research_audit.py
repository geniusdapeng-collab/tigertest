from trading_system.agent_evidence import atomic_bytes, atomic_json, file_ref
from trading_system.research_audit import _current_event_relative, candidate_funnel
from trading_system.review.attribution import attribute_journal


def _candidate(ticker, shape):
    return {"fields": {"ticker": ticker, "entry_template": shape, "s_options": None}}


def test_candidate_funnel_separates_missing_shape_sources_and_model_veto():
    scan = {
        "A": {"hard_filter": "pass", "selection": "supplement"},
        "B": {"hard_filter": "pass", "selection": "baseline"},
        "C": {"hard_filter": "no_data"},
        "D": {"hard_filter": "pass", "selection": "shape_missing"},
    }
    risk = {"candidates": [{"ticker": ticker, "status": "accepted", "reasons": []}
                           for ticker in ("A", "B")]}
    debate = {
        "company_evidence": {"candidates": {"A": {"status": "no_results", "source_ids": []},
                                           "B": {"status": "filing_verified", "source_ids": ["filing"]}}},
        "execution_decisions": [{"symbol": ticker, "action": "watch"} for ticker in ("A", "B")],
    }
    value = candidate_funnel(list(scan), scan, [_candidate("A", "C"), _candidate("B", "A")], risk, debate)
    a, b, c, d = value["rows"]
    assert a["gates"]["company"]["status"] == "unknown"
    assert a["gates"]["model"]["status"] == "unknown"
    assert b["gates"]["model"] == {"status": "rejected", "reasons": ["model_watch"]}
    assert c["gates"]["market_data"]["status"] == "unknown"
    assert d["gates"]["entry_setup"]["status"] == "unknown"
    assert value["gates"]["entry_setup"] == {
        "denominator": 3, "passed": 2, "rejected": 0, "unknown": 1,
        "not_applicable": 1, "overlapping_reasons": {"shape_missing": 1},
    }


def test_review_missing_snapshot_has_no_zero_violation_claim_and_keeps_evidence_ids():
    entered = {"date": "2026-11-03", "ticker": "A", "signal_id": "sig-a", "status": "open",
               "entry": 12, "mrs_star": 7, "mode": "标准做多", "protected": True}
    without = attribute_journal([entered])["violation_coverage"]
    assert without["V3"]["status"] == "insufficient_evidence"
    assert without["V3"]["checked"] == 0
    assert without["V3"]["violations"] is None
    with_snapshot = attribute_journal([entered], {"2026-11-03": {"no_chase": False}})["violation_coverage"]
    assert with_snapshot["V3"]["status"] == "checked"
    assert with_snapshot["V3"]["violations"] == 0
    assert with_snapshot["V3"]["evidence"] == [{"signal_id": "sig-a", "date": "2026-11-03", "ticker": "A",
                                               "status": "checked", "fields": ["no_chase"]}]
    assert with_snapshot["V4"]["status"] == "insufficient_evidence"
    assert attribute_journal([])["violation_coverage"]["V3"]["status"] == "not_applicable"


def test_authoritative_review_compares_actual_open_and_applied_earnings_reduction():
    row = {"date": "2026-11-03", "ticker": "A", "signal_id": "signal",
           "status": "open", "entry": 12.2, "entry_raw": 12, "entry_date": "2026-11-04",
           "mode": "标准做多", "accounting_basis": "paper_execution",
           "paper_event_ids": {"filled": "event"}, "review_snapshot": {
               "no_chase": True, "entry_ceiling": 12.1, "mainline_eligible": True,
               "earnings": ["A"], "earnings_size_factor": .5, "earnings_size_limit": .5,
           }}
    coverage = attribute_journal([row])["violation_coverage"]
    assert coverage["V3"]["status"] == coverage["V4"]["status"] == "checked"
    assert coverage["V3"]["violations"] == coverage["V4"]["violations"] == 0
    row["entry_raw"] = 12.2
    row["review_snapshot"]["earnings_size_factor"] = .75
    coverage = attribute_journal([row])["violation_coverage"]
    assert coverage["V3"]["violations"] == coverage["V4"]["violations"] == 1
    row["accounting_basis"] = "legacy_hypothesis"
    assert attribute_journal([row])["violation_coverage"]["V3"]["status"] == "not_applicable"


def test_research_audit_resolves_the_verified_current_parent_attempt(tmp_path):
    relative = "parent-attempts/1/events.jsonl"
    atomic_bytes(tmp_path, relative, b"\n")
    start_path = "parent-attempts/1/start.json"
    atomic_json(tmp_path, start_path, {
        "schema": "tiger.parent-attempt/v1",
        "events_path": relative,
    })
    atomic_json(tmp_path, "parent-attempt.json", {
        "schema": "tiger.parent-attempt-current/v1",
        "start_ref": file_ref(tmp_path, start_path, "tiger.parent-attempt/v1"),
    })
    assert _current_event_relative(tmp_path) == relative
