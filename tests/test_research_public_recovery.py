"""Public recovery and explicit supplement admission contracts."""
from datetime import datetime, timezone
from datetime import timedelta
import hashlib
import json
import os

import pytest

from test_research_followup import discovery
from agent_workflow_fixtures import fixture_source, wait_terminal
from trading_system import agent_runner as runner
from trading_system.agent_evidence import (EvidenceInvalid, atomic_json, checked_root,
                                           digest, read_bytes, read_json)
from trading_system.agent_runtime import INTELLIGENCE_PROFILE, frozen_clock


def test_public_request_contracts_are_exact_and_bounded():
    run_id = "run-" + "a" * 32
    assert runner.validate_request("resume", {"run_id": run_id}) == {"run_id": run_id}
    assert runner.validate_request("cancel", {"run_id": run_id}) == {"run_id": run_id}
    supplement = {
        "schema_version": "tiger.research-supplement-request/v1",
        "parent_run_id": run_id,
        "symbols": ["NVDA", "BRK.B"],
        "idempotency_key": "supplement-request-0001",
    }
    assert runner.validate_request("supplement", supplement) == supplement
    for value in (
        {**supplement, "symbols": []},
        {**supplement, "symbols": ["NVDA", "NVDA"]},
        {**supplement, "symbols": ["nvda"]},
        {**supplement, "profile": "us-public-intelligence-paper-v2"},
        {**supplement, "parent_run_id": "other"},
    ):
        with pytest.raises(EvidenceInvalid) as caught:
            runner.validate_request("supplement", value)
        assert caught.value.code == "INVALID_REQUEST"


def test_public_cancel_records_scheduler_cancellation_without_stopping_observer(tmp_path):
    from trading_system.research_work import research_work_policy, work_manifest
    from trading_system.tasks.store import WorkStore

    state = checked_root(tmp_path / "state", create=True)
    run_id = "run-" + "d" * 32
    run = checked_root(state / "runs" / run_id, create=True)
    clock = frozen_clock(datetime(2026, 11, 8, 1, tzinfo=timezone.utc))
    admission = {
        "run_id": run_id, "request_sha256": "1" * 64,
        "source_sha256": "2" * 64, "policy_sha256": "3" * 64,
        "parent": None, "clock": clock,
    }
    atomic_json(run, "admission.json", admission)
    atomic_json(run, "policy.json", {"research_work": research_work_policy()})
    atomic_json(run, "status.json", {
        "schema": "tiger.status.v1", "run_id": run_id,
        "status": "running", "error": None,
    })
    descriptor = runner._lock_fd(run, "run.lock", create=True, nonblocking=True)
    try:
        value = runner.submit_cancel(state, {"run_id": run_id})
    finally:
        os.close(descriptor)
    assert value["operation_ok"] is True
    assert value["operation"] == "cancel"
    assert value["status"] == "cancelling"
    assert value["data"]["cancel_requested"] is True
    assert value["data"]["scheduling"]["cancelling_observation_stops_scheduler"] is False
    store = WorkStore.open(run / "tasks", work_manifest(admission))
    assert store.status()["cancel_requested"] is True
    assert store.status()["graph_state"] == "cancelled"


def test_verified_pending_symbols_create_a_new_scope_not_an_old_approval(tmp_path, monkeypatch):
    from trading_system.agent_runtime import ACCOUNT

    state = checked_root(tmp_path / "state", create=True)
    _, _, pending = discovery()
    parent_id = pending["run_id"]
    parent = checked_root(state / "runs" / parent_id, create=True)
    request = {
        "schema_version": "tiger.agent-request/v1",
        "profile": "us-public-intelligence-paper-v2",
        "paper_account": ACCOUNT,
        "idempotency_key": "parent-request-0001",
    }
    atomic_json(parent, "request.json", request)
    monkeypatch.setattr(runner, "verify_run", lambda *_args, **_kwargs: {
        "pointer": {"receipt_sha256": "7" * 64},
        "summary": {"disclosures": {"intelligence": {"pending_candidates": pending}}},
    })
    supplement = {
        "schema_version": "tiger.research-supplement-request/v1",
        "parent_run_id": parent_id,
        "symbols": ["BBB"],
        "idempotency_key": "supplement-request-0002",
    }
    clock = frozen_clock(datetime(2026, 11, 8, 1, tzinfo=timezone.utc))
    internal, scope = runner._supplement_inputs(state, supplement, clock)
    assert internal == {
        "schema_version": "tiger.agent-request/v1",
        "profile": "us-public-intelligence-paper-v2",
        "paper_account": ACCOUNT,
        "idempotency_key": "supplement-request-0002",
    }
    assert scope["parent_run_id"] == parent_id
    assert scope["symbols"] == ["BBB"]
    assert scope["reuse_trade_approval"] is False
    assert scope["parent_receipt_sha256"] == "7" * 64
    assert scope["requested_at"] == clock["admitted_at_utc"]


def test_active_same_run_is_observed_without_second_worker(tmp_path, monkeypatch):
    state = checked_root(tmp_path / "state", create=True)
    run_id = "run-" + "c" * 32
    run = checked_root(state / "runs" / run_id, create=True)
    atomic_json(run, "status.json", {
        "schema": "tiger.status.v1", "run_id": run_id,
        "status": "running", "error": None,
    })
    descriptor = runner._lock_fd(run, "run.lock", create=True, nonblocking=True)
    spawned = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: spawned.append(True))
    try:
        value = runner.submit_resume(state, {"run_id": run_id})
    finally:
        os.close(descriptor)
    assert value["operation_ok"] is True
    assert value["operation"] == "resume" and value["run_id"] == run_id
    assert value["status"] == "running" and value["data"]["idempotent_replay"] is True
    assert spawned == []


def test_stopped_same_run_resumes_exact_identity_in_a_new_process(tmp_path, monkeypatch):
    from trading_system.intelligence import IntelligenceService, build_plan, default_registry
    from trading_system.intelligence.transport import HttpResponse

    cutoff = datetime.now(timezone.utc)
    observed = cutoff - timedelta(days=1)
    registry = default_registry()
    class Transport:
        def request(self, method, url, **_kwargs):
            if method == "POST":
                body = json.dumps({"results": [{
                    "url": "https://www.reuters.com/fixture/recovery-original",
                    "title": "TEST_FIXTURE recovery original",
                    "date": observed.isoformat(),
                    "content": "Discovery only",
                }]}).encode()
                media = "application/json"
            else:
                body = ("<html><head><title>TEST_FIXTURE recovery original</title>"
                        f'<meta property="article:published_time" content="{observed.isoformat()}">'
                        '</head><article><p>' + "Complete original paragraph. " * 300 +
                        "TAIL recovery evidence.</p></article></html>").encode()
                media = "text/html"
            return HttpResponse(url, 200, {"content-type": media}, body,
                                observed.isoformat(), hashlib.sha256(body).hexdigest())
    archive_root = tmp_path / "archive"
    service = IntelligenceService(archive_root, registry,
        registry.build_adapters(transport=Transport(), environment={"LLM_API_KEY": "fixture-only"}),
        clock=lambda: observed)
    plan = build_plan({
        "run_id": "prior-original",
        "as_of": cutoff.isoformat(),
        "universe": {"AAPL": "us:AAPL"},
        "frontier": ["AAPL"],
        "holdings": [],
        "frontier_manifest_hash": "a" * 64,
        "required_cells": [{
            "target_id": "security:us:AAPL",
            "entity_id": "us:AAPL",
            "required": True,
            "question": "Original company evidence",
        }],
    }, registry)
    job = service.create_job(plan, "prior-original")
    service.collect(job["job_id"])
    assert service.archive.documents(job["job_id"])
    config = tmp_path / "snapshot-only.json"
    config.write_text('{"acquisition_mode":"snapshot_only"}')
    monkeypatch.setenv("TIGER_INTEL_REGISTRY_FILE", str(config))
    monkeypatch.setenv("TIGER_INTEL_ARCHIVE_ROOT", str(service.archive.root))

    source = fixture_source(tmp_path / "source")
    state = tmp_path / "state"
    request = {
        "schema_version": "tiger.agent-request/v1",
        "profile": INTELLIGENCE_PROFILE,
        "paper_account": "public-usd",
        "idempotency_key": "recovery-run-0001",
    }
    accepted = runner.submit(state, request, source=source)
    assert accepted["operation_ok"], accepted
    run_id = accepted["run_id"]
    first = wait_terminal(state, run_id, seconds=30)
    assert first["status"] in {"failed", "blocked"}
    run = state / "runs" / run_id
    original = {name: read_bytes(run, name) for name in (
        "request.json", "admission.json", "policy.json",
        "account-input.json", "calendar-input.json")}
    assert read_json(run, "parent-attempt.json")["number"] == 1

    resumed = runner.submit_resume(state, {"run_id": run_id})
    assert resumed["operation_ok"], resumed
    second = wait_terminal(state, run_id, seconds=30)
    assert second["status"] in {"failed", "blocked"}
    assert read_json(run, "parent-attempt.json")["number"] == 2
    assert {name: read_bytes(run, name) for name in original} == original
    assert (run / "parent-attempts/1/archive.json").exists()
    assert not (run / "receipt.json").exists()
