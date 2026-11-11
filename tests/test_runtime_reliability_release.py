"""Focused regressions for the 2026-11-11 default-v2 reliability release."""
from copy import deepcopy
import sqlite3
import threading
import time

import pandas as pd

from agent_workflow_fixtures import REQUEST, fixture_source, wait_terminal
from test_repair_research_status import project, raw_fixture, verified
from test_task_work_units import create, unit
from trading_system import agent_runner as runner
from trading_system import pipeline
from trading_system.providers.base import DataProvider
from trading_system.research_work import research_work_policy, validate_research_work_policy
from trading_system.tasks import Coordinator, WorkUnitError
from trading_system.universe import load_universe


def _valid(_value):
    return None


def _frame():
    index = pd.bdate_range("2026-01-02", periods=80)
    return pd.DataFrame({
        "Open": 100.0, "High": 101.0, "Low": 99.0,
        "Close": 100.0, "Volume": 2_000_000.0,
    }, index=index)


def test_indexed_claim_does_not_decode_full_units_projection(tmp_path, monkeypatch):
    specs = [unit(f"u{number:04d}") for number in range(2_800)]
    store = create(tmp_path, specs)
    reads = 0
    original = store._rows

    def traced(connection, table):
        nonlocal reads
        if table == "units":
            reads += 1
        return original(connection, table)

    monkeypatch.setattr(store, "_rows", traced)
    for number in range(64):
        lease = store.claim_ready(f"worker-{number}")
        assert lease is not None and lease.unit_id == f"u{number:04d}"
    assert reads == 0


def test_transient_busy_store_error_is_retried_by_coordinator(tmp_path, monkeypatch):
    store = create(tmp_path)
    original = store.claim_ready
    failures = {"remaining": 2}

    def flaky_claim(*args, **kwargs):
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise WorkUnitError("STORE_BUSY", "TEST_FIXTURE transient write lock")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "claim_ready", flaky_claim)
    coordinator = Coordinator(store)
    try:
        result = coordinator.run(
            {"extract": lambda _context: {"ok": True}},
            validators={"extract": _valid},
            timeout=3,
        )
    finally:
        coordinator.close()
    assert failures["remaining"] == 0
    assert result["graph_state"] == "completed"


def test_transient_busy_write_lock_is_retried_before_transaction(tmp_path):
    store = create(tmp_path)
    store.busy_timeout_ms = 25
    store.write_busy_retry_ms = 2_000
    ready = threading.Event()
    release = threading.Event()

    def hold_lock():
        holder = sqlite3.connect(store.database, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        ready.set()
        release.wait(2)
        holder.commit()
        holder.close()

    thread = threading.Thread(target=hold_lock, daemon=True)
    thread.start()
    assert ready.wait(2)
    threading.Timer(0.35, release.set).start()
    try:
        lease = store.claim_ready("retry-worker")
    finally:
        release.set()
        thread.join(2)
    assert lease is not None and lease.unit_id == "a"


def test_large_workload_capacity_preserves_frozen_policy_compatibility(tmp_path):
    policy = research_work_policy()
    assert policy["document_workers"] >= 16
    previous = deepcopy(policy)
    previous["parser_workers"] = 2
    previous["document_workers"] = 4
    assert validate_research_work_policy(previous) == previous

    store = create(tmp_path, [unit(f"capacity-{number:03d}") for number in range(64)])
    active, peak, lock = 0, 0, threading.Lock()

    def handler(_context):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with lock:
            active -= 1
        return {"ok": True}

    coordinator = Coordinator(store, max_workers=policy["document_workers"])
    try:
        result = coordinator.run(
            {"extract": handler}, validators={"extract": _valid}, timeout=10)
    finally:
        coordinator.close()
    assert result["graph_state"] == "completed"
    assert peak >= 16


def test_fast_status_uses_sealed_projection_without_full_verify(tmp_path, monkeypatch):
    source = fixture_source(tmp_path / "source")
    state = tmp_path / "state"
    accepted = runner.submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    run_id = accepted["run_id"]
    assert wait_terminal(state, run_id)["status"] == "completed_degraded"
    run = state / "runs" / run_id
    assert (run / "research-status-summary.json").is_file()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("status must not run the complete verify proof")

    with monkeypatch.context() as patch:
        patch.setattr(runner, "verify_run", forbidden)
        status = runner.execute("status", {"run_id": run_id}, mode="readonly", state=state)
    assert status["operation_ok"], status
    assert status["status"] == "completed_degraded"
    assert status["data"]["research_status"]["basis"] in {"fast_projection", "report_snapshot"}

    cards = runner.execute(
        "status", {"run_id": run_id, "detail": "cards", "limit": 1},
        mode="readonly", state=state,
    )
    assert cards["operation_ok"], cards
    assert cards["data"]["research_status"]["detail"] == "cards"
    assert "securities" in cards["data"]["research_status"]


class _BatchProvider(DataProvider):
    CAPABILITIES = frozenset({"ohlcv_batch"})

    def __init__(self, name, returned):
        self.name = name
        self.returned = tuple(returned)
        self.calls = []
        self.frame = _frame()

    def ohlcv(self, ticker, days=400):
        if ticker in self.returned:
            return self.frame.tail(days).copy()
        raise ValueError("TEST_FIXTURE missing")

    def ohlcv_batch(self, tickers, days=400):
        self.calls.append(tuple(tickers))
        return {ticker: self.frame.tail(days).copy() for ticker in tickers
                if ticker in self.returned}

    def tnx_yield(self, days=400):
        return self.frame["Close"].tail(days)

    def vix(self, days=400):
        return self.frame["Close"].tail(days)


def test_market_gap_fill_recovers_small_active_gap_and_prunes_retired_symbols(monkeypatch):
    from trading_system.universe import RETIRED_UNIVERSE

    assert {"ALTM", "FSR", "MAXN", "SAVE", "SMAR", "SQ", "WBA"}.issubset(RETIRED_UNIVERSE)
    active = load_universe("extended")
    assert not set(active) & set(RETIRED_UNIVERSE)

    primary = _BatchProvider("primary", ["AAA", "BBB"])
    fill = _BatchProvider("fill", ["CCC"])
    monkeypatch.setattr(pipeline, "_ordered_batch_chain", lambda _execution=None: [fill])
    result = pipeline._batch_with_fallback(
        primary, ["AAA", "BBB", "CCC"], days=60, minimum=1,
        label="TEST_FIXTURE", gap_fill=True)
    assert fill.calls == [("CCC",)]
    assert set(result) == {"AAA", "BBB", "CCC"}


def test_completed_wait_projection_is_not_unknown():
    raw = raw_fixture()
    raw["action"] = "WAIT"
    raw["execution_decisions"] = []
    raw["intelligence"]["state"] = "completed"
    raw["intelligence"]["bundle"]["state"] = "completed"
    raw["intelligence"]["bundle"]["missing_required"] = []
    raw["intelligence"]["pending_candidates"]["candidates"] = []
    raw["research_audit"]["funnel"]["rows"] = []
    raw["research_audit"]["funnel"]["universe_size"] = 0
    for row in raw["intelligence"]["gaps"]:
        row["status"] = "present"
    value = project(raw, verification=verified(raw), detail="summary")
    assert value["axes"]["evidence"]["state"] == "sufficient"
    assert value["axes"]["decision"]["state"] == "WAIT"
    assert value["axes"]["decision"]["recorded_action"] == "WAIT"
