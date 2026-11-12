"""Snapshot-only paper research uses original bytes and all five model stages.

Synthetic model/provider boundaries are explicitly offline. These tests verify
the production execution/proof paths, not live source quality or profitability.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import main
from trading_system.agent_evidence import EvidenceInvalid, atomic_json, read_json
from trading_system.agent_runtime import policy_manifest, frozen_clock
from trading_system.intelligence import IntelligenceService, build_plan, default_registry
from trading_system.intelligence.archive import Archive
from trading_system.intelligence.contracts import IntelligenceError
from trading_system.intelligence.registry import SourceRegistry, acquisition_mode
from trading_system.intelligence.transport import HttpResponse, HttpTransport
from trading_system.intelligence_workflow import (PAPER_SNAPSHOT_LIMITS, SNAPSHOT_ONLY_METADATA_BYTES,
    TOOL_LIMITS, baseline_manifest_hash, stage_input_packet, validate_input_packet)
from trading_system.llm import intelligence_required as semantic
from trading_system.llm import intelligence_proof as proof
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.redline.execution import INTELLIGENCE_PROFILE, REQUIRED_MODEL_STAGES, RequiredModelFailed
from test_intelligence_workflow_v2 import intelligence_fixture
from test_intelligence_snapshots import NOW, archive_job, add


def original_archive(root, registry, cutoff):
    observed = cutoff - timedelta(days=1)
    class Transport:
        def request(self, method, url, **kwargs):
            if method == "POST":
                body = json.dumps({"results": [{"url": "https://www.reuters.com/fixture/snapshot-only-original",
                    "title": "TEST_FIXTURE original", "date": observed.isoformat(), "content": "Discovery only"}]}).encode()
                media = "application/json"
            else:
                body = ('<html><head><title>TEST_FIXTURE original</title>'
                    '<meta property="article:published_time" content="' + observed.isoformat() + '"></head>'
                    '<article><h1>TEST_FIXTURE original</h1><p>' + "Complete original paragraph. " * 300 +
                    'TAIL original evidence.</p></article></html>').encode()
                media = "text/html"
            return HttpResponse(url, 200, {"content-type": media}, body, observed.isoformat(), hashlib.sha256(body).hexdigest())
    service = IntelligenceService(root, registry,
        registry.build_adapters(transport=Transport(), environment={"LLM_API_KEY": "fixture-only"}), clock=lambda: observed)
    plan = build_plan({"run_id": "prior-original", "as_of": cutoff.isoformat(),
        "universe": {"AAPL": "us:AAPL"}, "frontier": ["AAPL"], "holdings": [],
        "frontier_manifest_hash": "a" * 64,
        "required_cells": [{"target_id": "security:us:AAPL", "entity_id": "us:AAPL",
            "required": True, "question": "Original company evidence"}]}, registry)
    job = service.create_job(plan, "prior-original")
    service.collect(job["job_id"])
    assert service.archive.documents(job["job_id"])
    return service.archive


def snapshot_fixture(tmp_path, patch, *, failure_stage=None, authoritative=False):
    execution, root, args, provider, denied, calls = intelligence_fixture(
        tmp_path, patch, failure_stage=failure_stage, authoritative=authoritative)
    old_packet = read_json(root, "intelligence-input.json")
    prior = original_archive(tmp_path / "prior-original", default_registry(old_packet["config"]), execution.admitted_at)
    config = {**old_packet["config"], "acquisition_mode": "snapshot_only"}
    configured = tmp_path / "snapshot-only-config.json"
    configured.write_text(json.dumps(config))
    patch.setenv("TIGER_INTEL_REGISTRY_FILE", str(configured))
    patch.setenv("TIGER_INTEL_ARCHIVE_ROOT", str(prior.root))
    (root / "intelligence-input.json").unlink()
    packet = stage_input_packet(root, as_of=execution.admitted_at)
    if authoritative:
        atomic_json(root, "policy.json", policy_manifest(INTELLIGENCE_PROFILE, intelligence_input=packet,
            response_observation_contract=execution.response_observation_contract))
    build_adapters = SourceRegistry.build_adapters
    def frozen_adapters(registry, *args, **kwargs):
        from trading_system.intelligence.replay import _FrozenTransport
        assert isinstance(kwargs.get("transport"), _FrozenTransport), "Snapshot mode constructed a live source adapter"
        return build_adapters(registry, *args, **kwargs)
    patch.setattr(SourceRegistry, "build_adapters", frozen_adapters)
    patch.setattr(HttpTransport, "request", lambda *a, **k: pytest.fail("Snapshot mode attempted source transport"))
    patch.setattr(DeepSeekRequiredClient, "search", lambda *a, **k: pytest.fail("Snapshot mode called native Search"))
    patch.setattr(DeepSeekRequiredClient, "fetch", lambda *a, **k: pytest.fail("Snapshot mode called native Fetch"))
    return execution, root, args, prior, packet, denied, calls


@pytest.mark.parametrize("mode", ["unknown", "", None, True, 1, [], {}])
def test_unknown_trusted_modes_fail_before_source_configuration(mode):
    with pytest.raises(IntelligenceError):
        default_registry({"acquisition_mode": mode})


def test_mode_does_not_change_source_permissions_or_default_policy_bytes():
    absent = default_registry().public_manifest()
    assert default_registry({"acquisition_mode": "live"}).public_manifest() == absent
    assert default_registry({"acquisition_mode": "snapshot_only"}).public_manifest() == absent
    assert acquisition_mode({}) == acquisition_mode(None) == "live"
    assert PAPER_SNAPSHOT_LIMITS["metadata_bytes"] == 8 * 1024 * 1024
    assert SNAPSHOT_ONLY_METADATA_BYTES == 32 * 1024 * 1024
    assert TOOL_LIMITS == {"requests": 320, "fetches": 256, "bytes": 64 * 1024 * 1024,
        "rounds": 2, "concurrency": 4, "deadline_seconds": 600}


def test_snapshot_startup_requires_nonempty_explicit_originals_before_packet_write(tmp_path, monkeypatch):
    configured = tmp_path / "snapshot.json"
    configured.write_text('{"acquisition_mode":"snapshot_only"}')
    monkeypatch.setenv("TIGER_INTEL_REGISTRY_FILE", str(configured))
    monkeypatch.delenv("TIGER_INTEL_ARCHIVE_ROOT", raising=False)
    run = tmp_path / "run"; run.mkdir()
    with pytest.raises(EvidenceInvalid) as denied:
        stage_input_packet(run, as_of=NOW)
    assert denied.value.code == "INVALID_REQUEST"
    assert not (run / "intelligence-input.json").exists()
    archive, _ = archive_job(tmp_path / "empty")
    monkeypatch.setenv("TIGER_INTEL_ARCHIVE_ROOT", str(archive.root))
    with pytest.raises(EvidenceInvalid): stage_input_packet(run, as_of=NOW)
    assert not (run / "intelligence-input.json").exists()


def test_snapshot_metadata_budget_is_mode_bound_and_cannot_be_raised_in_saved_packet(tmp_path, monkeypatch):
    archive, job = archive_job(tmp_path / "archive")
    add(archive, job, "one", entity="us:NVDA")
    monkeypatch.setenv("TIGER_INTEL_ARCHIVE_ROOT", str(archive.root))
    configured = tmp_path / "config.json"
    configured.write_text('{"acquisition_mode":"snapshot_only"}')
    monkeypatch.setenv("TIGER_INTEL_REGISTRY_FILE", str(configured))
    run = tmp_path / "run"; run.mkdir()
    packet = stage_input_packet(run, as_of=NOW)
    manifest = packet["baseline"]["manifest"]
    assert manifest["limits"] == {**PAPER_SNAPSHOT_LIMITS, "metadata_bytes": 32 * 1024 * 1024}
    assert validate_input_packet(packet) == packet
    for budget in (8 * 1024 * 1024, 64 * 1024 * 1024):
        broken = deepcopy(packet)
        value = broken["baseline"]["manifest"]
        value["limits"]["metadata_bytes"] = budget
        value["manifest_sha256"] = baseline_manifest_hash(value)
        value["snapshot_id"] = "snapshot_" + value["manifest_sha256"][:32]
        with pytest.raises(EvidenceInvalid): validate_input_packet(broken)
    unscoped = deepcopy(packet)
    unscoped["baseline"]["manifest"]["schema"] = "tiger.intelligence-baseline/v1"
    unscoped["baseline"]["manifest"]["manifest_sha256"] = baseline_manifest_hash(unscoped["baseline"]["manifest"])
    with pytest.raises(EvidenceInvalid): validate_input_packet(unscoped)
    configured.write_text('{}')
    live = tmp_path / "live"; live.mkdir()
    assert stage_input_packet(live, as_of=NOW)["baseline"]["manifest"]["limits"] == PAPER_SNAPSHOT_LIMITS


def test_snapshot_41stage_full_originals_five_semantic_stages_and_offline_proof(tmp_path, monkeypatch):
    execution, root, args, prior, packet, denied, calls = snapshot_fixture(tmp_path, monkeypatch)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    result = asdict(output.result)
    model = result["raw"]["model"]
    assert model["required_stages"] == list(REQUIRED_MODEL_STAGES)
    assert model["stage_counts"] == {stage: 1 for stage in REQUIRED_MODEL_STAGES}
    assert {stage for stage, path in calls} == set(REQUIRED_MODEL_STAGES)
    assert all(path == "/chat/completions" for stage, path in calls)
    assert model["source_requests"] == model["search_count"] == model["fetch_count"] == 0
    disclosed = result["raw"]["intelligence"]
    assert disclosed["acquisition_mode"] == "snapshot_only" and disclosed["state"] == "partial"
    assert result["picks"] == [] and result["action"] in {"WAIT", "AVOID"}
    archive = Archive(root / "intelligence", readonly=True)
    bundle = archive.bundle(disclosed["bundle"]["bundle_id"])
    plan = archive.plan(bundle["job_id"])
    assert plan["query_plan"] == [] and plan["limits"] == TOOL_LIMITS
    assert set(plan["universe"]) == {"AAPL", "MU", "NVDA"}
    assert {cell["target_id"] for cell in plan["coverage_cells"]} >= {"global:macro", "security:us:AAPL", "security:us:MU", "security:us:NVDA"}
    assert bundle["attempts"] == [] and bundle["documents"]
    assert any("TAIL original evidence." in doc["text"] and len(doc["text"]) > 4000 for doc in bundle["documents"])
    assert bundle["baseline_refs"][0]["reused_semantic_annotations"] is False
    assert any(row.get("source_reason") == "SNAPSHOT_ONLY_NO_LIVE_ACQUISITION" for row in bundle["gaps"])
    assert len(archive.events(bundle["job_id"], kind="research_started")) == 1
    assert len(archive.events(bundle["job_id"], kind="research_completed")) == 1
    shutil.rmtree(prior.root)
    admission = {"clock": frozen_clock(execution.admitted_at)}
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", lambda *a, **k: pytest.fail("Proof called model"))
    assert proof.verify_intelligence_model(root, result, admission) == model
    replay = proof._Replay(root, admission, None)
    _, checked_archive, checked_bundle, _, checked_plan, snapshots, phases, _ = proof._verify_acquisition(replay)
    original_events = checked_archive.events(checked_bundle["job_id"])
    for corruption in ("checkpoint_hash", "collection_io", "research_query", "research_mode", "missing_completion", "extra_document", "attempt"):
        events, corrupt_bundle, corrupt_phases = deepcopy(original_events), deepcopy(checked_bundle), deepcopy(phases)
        if corruption == "checkpoint_hash":
            next(event for event in events if event["kind"] == "snapshot_collection_completed")["payload"]["documents_sha256"] = "a" * 64
        elif corruption == "collection_io":
            next(event for event in events if event["kind"] == "snapshot_collection_completed")["payload"]["request_count"] = 1
        elif corruption == "research_query":
            next(event for event in events if event["kind"] == "research_started")["payload"]["query_plan"] = [{"source_id": "deepseek"}]
        elif corruption == "research_mode":
            next(event for event in events if event["kind"] == "research_completed")["payload"]["mode"] = "live"
        elif corruption == "missing_completion":
            events = [event for event in events if event["kind"] != "research_completed"]
        elif corruption == "extra_document":
            corrupt_phases[0].append({"id": "not-an-imported-original"})
        else:
            corrupt_bundle["attempts"].append({"status": "failed", "result": {"request_count": 0}})
        fake_archive = SimpleNamespace(events=lambda *a, **k: events)
        with pytest.raises(EvidenceInvalid):
            proof._verify_snapshot_only(fake_archive, corrupt_bundle, checked_plan, snapshots, corrupt_phases)
    wrong_mode = deepcopy(packet); wrong_mode["config"].pop("acquisition_mode")
    atomic_json(root, "intelligence-input.json", wrong_mode)
    try:
        with pytest.raises(EvidenceInvalid): proof.verify_intelligence_model(root, result, admission)
    finally:
        atomic_json(root, "intelligence-input.json", packet)
    changed = deepcopy(result); changed["raw"]["intelligence"].pop("acquisition_mode")
    with pytest.raises(EvidenceInvalid): proof.verify_intelligence_model(root, changed, admission)
    assert denied == []


@pytest.mark.parametrize("stage", REQUIRED_MODEL_STAGES)
def test_snapshot_mode_never_replaces_a_failed_required_model_stage(tmp_path, monkeypatch, stage):
    execution, root, args, _, _, _, calls = snapshot_fixture(tmp_path, monkeypatch, failure_stage=stage)
    with pytest.raises(RequiredModelFailed): main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["stage"] == stage and execution.summary()[-1]["status"] == "failed"
    assert not (root / "receipt.json").exists()
    assert all(path == "/chat/completions" for _, path in calls)


def test_snapshot_authoritative_paper_replays_original_ledger_and_model_proofs(tmp_path, monkeypatch):
    from trading_system.paper_proof import verify_paper_execution
    execution, root, args, _, _, denied, _ = snapshot_fixture(tmp_path, monkeypatch, authoritative=True)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    result = asdict(output.result)
    admission = {"clock": frozen_clock(execution.admitted_at), "parent": None}
    assert proof.verify_intelligence_model(root, result, admission) == result["raw"]["model"]
    assert verify_paper_execution(root, admission, result, output.sim_state)["contract"] == "tiger.paper-execution.v1"
    assert denied == []


def test_known_previous_summary_prompt_source_is_recognized_without_executing_it(tmp_path, monkeypatch):
    content = b'raise AssertionError("Historical source must not execute")\n'
    path = tmp_path / "source/trading_system/llm/intelligence_required.py"
    path.parent.mkdir(parents=True); path.write_bytes(content)
    monkeypatch.setattr(proof, "SUMMARY_CLEAN_SOURCE_SHA256", hashlib.sha256(content).hexdigest())
    assert proof.frozen_clean_format_contract(tmp_path) == semantic.SUMMARY_CLEAN_FORMAT_CONTRACT
    path.write_bytes(content + b"# unknown\n")
    with pytest.raises(EvidenceInvalid): proof.frozen_clean_format_contract(tmp_path)
