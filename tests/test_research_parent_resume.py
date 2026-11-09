"""Real local filesystem/SQLite recovery; no provider or business-proof claims.

The low-level fixture intentionally has no frozen executable source. Public
admission validation is tested separately; these cases exercise immutable
attempts, original account restoration, I/O replay and fail-closed holds.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest
import requests

from trading_system import agent_runtime as runtime
from trading_system.agent_evidence import (EvidenceInvalid, atomic_json, checked_root, digest,
    json_bytes, normalize, read_bytes, read_json)
from trading_system.redline.execution import (EvidenceWriteError, ExecutionPolicy, RuntimePaths,
    INTELLIGENCE_POLICY_ID, INTELLIGENCE_SEARCH_SOURCES)
from trading_system.research_work import research_work_policy, attach_research_work
from trading_system import research_resume as resume
from trading_system.research_replay import restore_provider_value, read_io_journal
from trading_system.tasks import UnitSpec


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    for method in ("get", "post", "request"):
        monkeypatch.setattr(requests, method, lambda *args, **kwargs: pytest.fail("Offline recovery fixture attempted a provider call"))


def prepared_parent(tmp_path):
    state = checked_root(tmp_path / "state", create=True)
    run = checked_root(state / "runs" / ("run-" + "d" * 32), create=True)
    checked_root(run / "source", create=True)
    clock = runtime.frozen_clock()
    policy = runtime.policy_manifest(runtime.INTELLIGENCE_PROFILE, research_work=research_work_policy())
    request = {"schema_version": "tiger.agent-request/v1", "profile": runtime.INTELLIGENCE_PROFILE,
               "paper_account": "public-usd", "idempotency_key": "TEST_FIXTURE_parent_0001"}
    atomic_json(run, "request.json", request, exclusive=True)
    atomic_json(run, "policy.json", policy, exclusive=True)
    runtime.stage_account(state, run, None)
    runtime.stage_calendar(run)
    admission = {"schema": "tiger.admission.v1", "run_id": run.name, "clock": clock,
        "parent": None, "request_sha256": digest(json_bytes(request)),
        "policy_sha256": digest(json_bytes(policy)), "source_sha256": "3" * 64}
    atomic_json(run, "admission.json", admission, exclusive=True)
    atomic_json(run, "status.json", {"schema": "tiger.status.v1", "run_id": run.name,
                                      "status": "running", "error": None})
    resume.begin_parent_attempt(run, admission, policy)
    return state, run, admission, policy


def parent_context(run, admission, policy):
    work = run / "work"
    paths = RuntimePaths(work_dir=work, reports_dir=work / "reports", journal_path=work / "reports/journal.json",
        sim_path=work / "reports/sim_portfolio.json", calibration_path=work / "reports/calibration_samples.json",
        options_history_dir=work / "reports/options_hist", governance_path=work / "reports/governance_events.jsonl",
        proposals_dir=work / "reports/review_proposals", calendar_override_path=work / "events_calendar.json")
    execution = resume.execution_context(run, admission, policy, run_id=run.name,
        run_date=datetime.fromisoformat(admission["clock"]["market_timestamp"]).date(),
        admitted_at=datetime.fromisoformat(admission["clock"]["admitted_at_utc"]), paths=paths,
        policy=ExecutionPolicy(INTELLIGENCE_POLICY_ID, model_mode="enabled", search_source_names=INTELLIGENCE_SEARCH_SOURCES),
        authoritative_paper=True, response_observation_contract=policy["model_contract"]["response_observation_contract"])
    attach_research_work(execution, admission, policy)
    return execution


def frame():
    return pd.DataFrame({"Open": [1., 2.], "High": [2., 3.], "Low": [.5, 1.], "Close": [1.5, 2.5],
                         "Volume": pd.Series([100, 200], dtype="int64").array},
        index=pd.date_range("2026-10-20", periods=2, tz="UTC"))


@pytest.mark.parametrize("value", [None, {"x": ["完整原文🙂", 1, True, None]},
    pd.Series([1., float("nan")], index=pd.date_range("2026-10-20", periods=2), name="TEST_FIXTURE"),
    np.array([[1, 2], [3, 4]], dtype=np.int64), datetime(2026, 10, 20, tzinfo=timezone.utc)])
def test_provider_replay_is_a_lossless_typed_observation(value):
    frozen = normalize(value)
    assert normalize(restore_provider_value(frozen)) == frozen


def test_provider_frame_keeps_every_numeric_cell_dtype_and_aware_index():
    frozen = normalize(frame())
    restored = restore_provider_value(frozen)
    pd.testing.assert_frame_equal(restored, frame(), check_freq=False)
    assert normalize(restored) == frozen


@pytest.mark.parametrize("value", [{"__type__": "dataclass", "class": "os.system", "fields": {}},
    {"__type__": "path", "value": "/outside"}, {"__type__": "unknown", "value": []}])
def test_provider_decoder_never_imports_arbitrary_types_or_restores_paths(value):
    with pytest.raises(EvidenceInvalid):
        restore_provider_value(value)


def test_new_parent_attempt_reuses_original_provider_and_original_account_before_state(tmp_path):
    state, run, admission, policy = prepared_parent(tmp_path)
    execution = parent_context(run, admission, policy)
    with pytest.raises(RuntimeError, match="TEST_FIXTURE interruption"):
        with execution.step("daily.preflight", {"TEST_FIXTURE": "original"}):
            result = execution.call_io("provider.yahoo.ohlcv", {"args": ["NVDA"], "kwargs": {}}, frame)
            pd.testing.assert_frame_equal(result, frame())
            sim = read_json(run, "work/reports/sim_portfolio.json")
            sim["cash"] = 123.
            atomic_json(run, "work/reports/sim_portfolio.json", sim)
            raise RuntimeError("TEST_FIXTURE interruption")
    original = {name: read_bytes(run, name) for name in ("request.json", "admission.json", "policy.json", "account-input.json", "calendar-input.json")}
    old_events = read_bytes(run, "parent-attempts/1/events.jsonl")
    first = resume.prepare_resume(run, admission, policy, stopped_verifier=lambda: None)
    assert first["attempt"]["number"] == 2 and not first["closeout_only"]
    assert {name: read_bytes(run, name) for name in original} == original
    assert read_bytes(run, "parent-attempts/1/events.jsonl") == old_events
    assert read_json(run, "parent-attempts/1/work/reports/sim_portfolio.json")["cash"] == 123.
    assert read_json(run, "work/reports/sim_portfolio.json")["cash"] == 100000.
    restored = parent_context(run, admission, policy)
    with restored.step("daily.preflight", {"TEST_FIXTURE": "original"}) as scope:
        value = restored.call_io("provider.yahoo.ohlcv", {"args": ["NVDA"], "kwargs": {}},
                                lambda: pytest.fail("Already observed provider was fetched twice"))
        pd.testing.assert_frame_equal(value, frame(), check_freq=False)
        scope.output({"TEST_FIXTURE": "restored provider, no business completion"})
    rows = read_io_journal(run, 2)
    assert rows[-1]["reused_from"]["parent_attempt"] == 1
    assert read_bytes(run, "parent-attempts/1/events.jsonl") == old_events
    assert not (run / "events.jsonl").exists() and runtime.pointer_read(state) is None
    with pytest.raises(EvidenceWriteError):
        execution.store.append_event({"run_id": run.name, "sequence": execution.store.sequence + 1})


def test_crash_after_work_rotation_recovers_without_rearchiving_or_resetting_admission(tmp_path, monkeypatch):
    state, run, admission, policy = prepared_parent(tmp_path)
    parent_context(run, admission, policy)
    original = resume._write_equal
    failed = []
    def crash(root, relative, content):
        if relative == "work/events_calendar.json" and not failed:
            failed.append(True)
            raise OSError("TEST_FIXTURE seed publication failed")
        return original(root, relative, content)
    monkeypatch.setattr(resume, "_write_equal", crash)
    before_admission = read_bytes(run, "admission.json")
    with pytest.raises(OSError):
        resume.prepare_resume(run, admission, policy, stopped_verifier=lambda: None)
    archive = read_bytes(run, "parent-attempts/1/archive.json")
    assert resume.current_parent_attempt(run)["number"] == 1
    result = resume.prepare_resume(run, admission, policy, stopped_verifier=lambda: None)
    assert result["attempt"]["number"] == 2
    assert read_bytes(run, "admission.json") == before_admission
    assert read_bytes(run, "parent-attempts/1/archive.json") == archive
    assert read_bytes(run, "work/events_calendar.json") == json_bytes({"macro": {}, "earnings": {}})


def test_unknown_model_dispatch_blocks_before_any_old_work_is_moved(tmp_path):
    _state, run, admission, policy = prepared_parent(tmp_path)
    execution = parent_context(run, admission, policy)
    store = execution.research_work_store
    store.plan([UnitSpec("u", "TEST_FIXTURE", {}, {"schema": "TEST_FIXTURE/v1", "validator": "TEST_FIXTURE/v1"})], originals=[])
    lease = store.claim_ready("stopped-worker")
    attempt = store.begin_attempt(lease, {"TEST_FIXTURE": "unknown"}, operation="model.deepseek.chat", reservation={"output_tokens": 20})
    store.mark_dispatch(lease, attempt.attempt_id)
    before = read_bytes(run, "work/reports/sim_portfolio.json")
    with pytest.raises(EvidenceInvalid) as caught:
        resume.prepare_resume(run, admission, policy, stopped_verifier=lambda: None)
    assert caught.value.code == "RECONCILIATION_REQUIRED"
    assert store.status()["unknown_attempts"] == 1 and store.status()["held_reservations"]
    assert read_bytes(run, "work/reports/sim_portfolio.json") == before
    assert not (run / "parent-attempts/1/archive.json").exists()


def test_inflight_original_source_attempt_blocks_even_before_a_model_unit(tmp_path):
    from trading_system.intelligence.archive import Archive
    from trading_system.intelligence.contracts import digest as source_digest
    _state, run, admission, policy = prepared_parent(tmp_path)
    parent_context(run, admission, policy)
    archive = Archive(run / "intelligence")
    plan = {"run_id": run.name, "coverage_cells": [], "limits": {"requests": 3, "fetches": 2, "bytes": 1000}}
    plan["plan_hash"] = source_digest(plan)
    job = archive.create_job(plan, "TEST_FIXTURE_source_job")
    identity = archive.begin_attempt(job["job_id"], "deepseek_search", "search", {"query": "TEST_FIXTURE"})
    assert resume.source_unknown_attempts(run) == [{"job_id": job["job_id"], "attempt_id": identity, "state": "inflight"}]
    with pytest.raises(EvidenceInvalid) as caught:
        resume.prepare_resume(run, admission, policy, stopped_verifier=lambda: None)
    assert caught.value.code == "RECONCILIATION_REQUIRED"
    assert archive.attempts(job["job_id"])[0]["status"] == "inflight"


def test_original_deadline_is_never_renewed_by_a_resume_request(tmp_path, monkeypatch):
    _state, run, admission, policy = prepared_parent(tmp_path)
    original = read_bytes(run, "admission.json")
    monkeypatch.setattr(resume, "_now", lambda: datetime.fromisoformat(admission["clock"]["deadline_at_utc"]) + timedelta(seconds=1))
    with pytest.raises(EvidenceInvalid) as caught:
        resume.prepare_resume(run, admission, policy, stopped_verifier=lambda: None)
    assert caught.value.code == "TIMEOUT" and read_bytes(run, "admission.json") == original
    assert not (run / "parent-attempts/1/archive.json").exists()


def test_old_policy_cannot_upgrade_its_event_stream_to_resumable(tmp_path):
    _state, run, admission, policy = prepared_parent(tmp_path)
    old = dict(policy)
    old.pop("research_work")
    with pytest.raises(EvidenceInvalid) as caught:
        resume.prepare_resume(run, admission, old, stopped_verifier=lambda: None)
    assert caught.value.code == "RESUME_UNSUPPORTED"
