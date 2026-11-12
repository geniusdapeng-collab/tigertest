from datetime import datetime

import pandas as pd

from trading_system.calibration import CalibrationLayer
from trading_system.journal import Journal
from trading_system.simulator import SimEngine

from test_activity_profile import signal


def execution(engine, iso, frame=None, *, picks=False):
    from trading_system.paper_ledger import PaperObservations

    now = datetime.fromisoformat(iso)
    result = signal(now.date().isoformat())
    if not picks:
        result.picks.clear()
    observations = PaperObservations(now, lambda _: frame)
    return engine.step(result.trade_date, result, None, paper=observations,
                       run_id="fixture-" + iso,
                       references={"market_ref": "market", "approval_ref": "approved",
                                   "decision_ref": "decision"})


def observations(day, *, low=99, open_price=100):
    return pd.DataFrame({"Open": [open_price], "High": [102], "Low": [low],
                         "Close": [100], "Volume": [100000]},
                        index=pd.DatetimeIndex([day]))


def test_gap_and_missed_open_are_terminal_and_never_feed_calibration(tmp_path):
    engine = SimEngine(str(tmp_path / "paper.json"))
    journal = Journal(tmp_path / "journal.json")
    journal.log_picks(signal("2026-10-21"))
    journal.records[0].update(status="closed", r=1.0)
    journal.save()

    execution(engine, "2026-10-22T17:00:00-04:00", picks=True)
    execution(engine, "2026-10-23T10:00:00-04:00")
    assert engine.state["pending"]
    execution(engine, "2026-10-26T10:00:00-04:00", observations("2026-10-23", low=89))
    assert not engine.state["pending"] and not engine.state["positions"]
    journal.project_paper(engine.state)
    assert journal.records[0]["accounting_basis"] == "legacy_hypothesis"
    assert journal.records[1]["status"] == "void"
    assert journal.records[1]["outcome_reason"] == "SIGNAL_EXPIRED"
    assert CalibrationLayer().collect_samples(journal.records, paper_only=True) == []
    assert journal.stats(paper_only=True)["closed"] == 0


def test_intraday_bar_never_exits_and_early_close_and_restart_are_idempotent(tmp_path):
    engine = SimEngine(str(tmp_path / "paper.json"))
    journal = Journal(tmp_path / "journal.json")
    execution(engine, "2026-11-25T17:00:00-05:00", picks=True)
    sid = engine.state["pending"][0]["signal_id"]
    bar = observations("2026-11-27", low=92)
    execution(engine, "2026-11-27T09:20:00-05:00", bar)
    assert engine.state["pending"] and not engine.state["positions"]
    execution(engine, "2026-11-27T12:59:00-05:00", bar)
    assert len(engine.state["positions"]) == 1 and not engine.state["closed"]
    assert not engine.state["positions"][0].get("last_bar_date")
    journal.project_paper(engine.state)
    assert journal.records[0]["entry_date"] == "2026-11-27"
    assert journal.records[0]["status"] == "open"

    engine = SimEngine(engine.path)
    execution(engine, "2026-11-27T13:01:00-05:00", bar)
    assert engine.state["closed"][0]["signal_id"] == sid
    event_ids = [event["event_id"] for event in engine.state["paper_events"]]
    execution(engine, "2026-11-27T13:02:00-05:00", bar)
    assert [event["event_id"] for event in engine.state["paper_events"]] == event_ids
    journal.project_paper(engine.state)
    assert journal.records[0]["status"] == "closed"
    assert journal.records[0]["r"] == engine.state["closed"][0]["r_multiple"]
    assert len(CalibrationLayer().collect_samples(journal.records, paper_only=True)) == 1


def test_gap_below_stop_is_rejected_once_without_a_fill(tmp_path):
    engine = SimEngine(str(tmp_path / "paper.json"))
    execution(engine, "2026-10-22T17:00:00-04:00", picks=True)
    execution(engine, "2026-10-23T10:00:00-04:00",
              observations("2026-10-23", open_price=92, low=90))
    execution(engine, "2026-10-23T11:00:00-04:00", observations("2026-10-23"))
    assert [event["event_type"] for event in engine.state["paper_events"]] == ["registered", "rejected"]
    assert engine.state["paper_events"][-1]["reason"] == "opening_gap_through_stop"
    assert not engine.state["positions"] and not engine.state["closed"]


def test_missed_open_cannot_be_backfilled_after_closing_book(tmp_path):
    engine = SimEngine(str(tmp_path / "paper.json"))
    execution(engine, "2026-10-22T17:00:00-04:00", picks=True)
    execution(engine, "2026-10-23T17:00:00-04:00")
    assert engine.state["pending"]
    execution(engine, "2026-10-23T17:10:00-04:00", observations("2026-10-23"))
    assert engine.state["pending"] and not engine.state["positions"]
    execution(engine, "2026-10-26T09:00:00-04:00", observations("2026-10-23"))
    assert engine.state["paper_events"][-1]["event_type"] == "expired"


def test_paper_transition_is_reproduced_from_stage_and_vendor_evidence(tmp_path, monkeypatch):
    from copy import deepcopy
    from pathlib import Path

    import pytest

    import main
    from test_model_required_workflow import required_fixture
    from trading_system.agent_evidence import EvidenceInvalid, read_json, verify_events
    from trading_system.agent_runtime import frozen_clock
    from trading_system.paper_proof import verify_paper_execution
    from trading_system.redline.execution import ExecutionContext, daily_stage_contract

    original, root, args, provider, denied, _ = required_fixture(tmp_path, monkeypatch)
    admitted = datetime.fromisoformat("2026-10-20T17:00:00-04:00")
    context = ExecutionContext(run_id=original.run_id, run_date=original.run_date,
        paths=original.paths, policy=original.policy, store=original.store,
        admitted_at=admitted, authoritative_paper=True)
    sim_file = context.paths.sim_path
    engine = SimEngine(str(sim_file))
    execution(engine, "2026-10-19T17:00:00-04:00", picks=True)
    before = root / "account-before"
    before.mkdir()
    (before / sim_file.name).write_bytes(sim_file.read_bytes())
    (before / "journal.json").write_text("[]")
    real_ohlcv = provider.ohlcv

    def one_position(ticker, days=400):
        if ticker == "CDW":
            return observations("2026-10-20", low=92)
        return real_ohlcv(ticker, days)

    monkeypatch.setattr(provider, "ohlcv", one_position)
    result = main._daily(args, "yahoo", execution=context)
    verified = verify_events(root, context.run_id, daily_stage_contract(True, authoritative_paper=True))
    assert verified["stage_count"] == 39 and not denied
    journal = read_json(root, "work/reports/journal.json")
    assert journal[0]["status"] == "closed" and {"filled", "closed"} <= journal[0]["paper_event_ids"].keys()
    admission = {"clock": frozen_clock(admitted), "parent": None}
    report = read_json(root, str(Path(result.report_paths["json"]).relative_to(root)))
    disclosure = verify_paper_execution(root, admission, report, result.sim_state)
    assert disclosure["contract"] == "tiger.paper-execution.v1"
    assert [event["event_type"] for event in disclosure["events"]] == ["filled", "closed"]
    assert report["raw"]["calibration"]["n"] == 1
    tampered = deepcopy(report)
    tampered["raw"]["calibration"]["n"] = 2
    with pytest.raises(EvidenceInvalid, match="cannot be reproduced"):
        verify_paper_execution(root, admission, tampered, result.sim_state)


def test_before_close_missing_open_can_recover_but_cash_rejection_is_terminal(tmp_path):
    engine = SimEngine(str(tmp_path / "paper.json"))
    execution(engine, "2026-10-22T17:00:00-04:00", picks=True)
    first = execution(engine, "2026-10-23T09:31:00-04:00")
    assert [event["reason"] for event in first["observation_status"]["missing"]] == ["BAR_UNAVAILABLE"]
    second = execution(engine, "2026-10-23T09:32:00-04:00")
    assert second["observation_status"]["missing"] == first["observation_status"]["missing"]
    assert not second["events"]

    engine.state["cash"] = 0.5
    third = execution(engine, "2026-10-23T09:33:00-04:00", observations("2026-10-23"))
    assert third["events"][0]["event_type"] == "rejected"
    assert third["events"][0]["reason"] == "opening_cash_budget_exhausted"
    assert not engine.state["pending"] and not engine.state["positions"]
