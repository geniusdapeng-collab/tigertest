"""Bounded raw snapshot admission, frontier selection and offline proof replay."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import json
import shutil

import pytest

import main
from trading_system.agent_evidence import EvidenceInvalid, atomic_json, read_json
from trading_system.intelligence.archive import Archive
from trading_system.intelligence.contracts import digest
from trading_system.intelligence_workflow import (baseline_version_ids, stage_input_packet,
    baseline_manifest_hash, validate_baseline_manifest, validate_input_packet)
from trading_system.llm.intelligence_proof import _source_parameters, _verify_baseline, verify_intelligence_model
from test_intelligence_acquisition import web
from test_intelligence_snapshots import NOW, archive_job, add


def test_admission_selects_trusted_roster_and_shared_topics_before_reading(tmp_path, monkeypatch):
    archive, job = archive_job(tmp_path / "archive")
    add(archive, job, "company", entity="us:NVDA", topic="unused")
    add(archive, job, "macro", entity=None, topic="global:macro")
    for index in range(80): add(archive, job, "unrelated" + str(index), entity="outside:roster", topic="unrelated")
    monkeypatch.setenv("TIGER_INTEL_ARCHIVE_ROOT", str(archive.root))
    monkeypatch.delenv("TIGER_INTEL_REGISTRY_FILE", raising=False)
    monkeypatch.setattr(Archive, "archive_versions", lambda *args, **kwargs: pytest.fail("Admission must not export the live full archive"))
    reads = []
    original = Archive.read_blob
    def read(self, ref):
        reads.append(ref["sha256"])
        return original(self, ref)
    monkeypatch.setattr(Archive, "read_blob", read)
    run = tmp_path / "run"; run.mkdir()
    packet = stage_input_packet(run, as_of=NOW)
    manifest = packet["baseline"]["manifest"]
    assert manifest["schema"] == "topic_snapshot/v1"
    assert manifest["counts"]["selected"] == manifest["counts"]["scanned_candidates"] == 2
    assert len(reads) == 4
    assert {row["id"] for row in manifest["versions"]} == {"version_company_1", "version_macro_1"}
    assert manifest["limits"]["documents"] == 256 and manifest["limits"]["deadline_seconds"] == 30
    assert validate_input_packet(packet) == packet


def test_frontier_derivation_uses_only_fixed_members_and_retains_shared_counterevidence(tmp_path):
    archive, job = archive_job(tmp_path)
    a = add(archive, job, "nvda", entity="us:NVDA", topic="unrelated")
    add(archive, job, "aapl", entity="us:AAPL", topic="unrelated")
    macro = add(archive, job, "macro", entity=None, topic="global:macro")
    counter = add(archive, job, "counter", entity=None, topic="memory", material="counterevidence")
    manifest = archive.create_snapshot(NOW, scope={"entity_ids": ["us:NVDA", "us:AAPL"],
        "topic_ids": ["global:macro", "memory"]})
    subset = baseline_version_ids(manifest, entity_ids=["us:NVDA"], target_ids=["global:macro", "chain:memory"])
    assert set(subset) == {a["id"], macro["id"], counter["id"]}
    assert "version_aapl_1" not in subset
    altered = deepcopy(manifest); altered["members"][0]["requested_entity_ids"].append("us:NVDA")
    with pytest.raises(EvidenceInvalid): validate_baseline_manifest(altered)


def test_partial_snapshot_is_frozen_with_explicit_gaps(tmp_path, monkeypatch):
    import trading_system.intelligence_workflow as workflow
    archive, job = archive_job(tmp_path / "archive")
    add(archive, job, "one", entity="us:NVDA")
    add(archive, job, "two", entity="us:NVDA")
    monkeypatch.setenv("TIGER_INTEL_ARCHIVE_ROOT", str(archive.root))
    monkeypatch.delenv("TIGER_INTEL_REGISTRY_FILE", raising=False)
    monkeypatch.setitem(workflow.PAPER_SNAPSHOT_LIMITS, "documents", 1)
    run = tmp_path / "run"; run.mkdir()
    manifest = stage_input_packet(run, as_of=NOW)["baseline"]["manifest"]
    assert manifest["state"] == "partial" and manifest["gaps"]
    assert manifest["counts"]["selected"] == 1


@pytest.mark.parametrize("change", ["version", "duplicate", "frontier", "bytes", "naive_cutoff"])
def test_rehashed_snapshot_envelope_still_requires_coherent_members_and_bounds(tmp_path, change):
    archive, job = archive_job(tmp_path)
    add(archive, job, "one", entity="us:NVDA")
    manifest = deepcopy(archive.create_snapshot(NOW, scope={"entity_ids": ["us:NVDA"]}))
    if change == "version": manifest["versions"][0]["doc_id"] = "changed_document"
    elif change == "duplicate":
        manifest["members"].append(deepcopy(manifest["members"][0]))
        manifest["versions"].append(deepcopy(manifest["versions"][0]))
        manifest["counts"]["selected"] += 1
    elif change == "frontier": manifest["event_head"] = {"sequence": 0, "sha256": None}
    elif change == "bytes": manifest["counts"]["selected_raw_text_bytes"] += 1
    else: manifest["as_of"] = manifest["cutoff"] = "2026-11-06T01:00:00"
    manifest["manifest_sha256"] = baseline_manifest_hash(manifest)
    manifest["snapshot_id"] = "snapshot_" + manifest["manifest_sha256"][:32]
    with pytest.raises(EvidenceInvalid): validate_baseline_manifest(manifest)


def test_offline_proof_recomputes_subset_and_rejects_relabelled_provenance(tmp_path):
    archive, job = archive_job(tmp_path)
    selected = add(archive, job, "nvda", entity="us:NVDA", topic="unrelated")
    add(archive, job, "aapl", entity="us:AAPL", topic="unrelated")
    manifest = archive.create_snapshot(NOW, scope={"entity_ids": ["us:NVDA", "us:AAPL"]})
    receipt = {"archive_id": manifest["archive_id"], "manifest_sha256": manifest["manifest_sha256"],
        "manifest": manifest, "event_head": manifest["fixed_event_frontier"],
        "versions": [row for row in manifest["versions"] if row["id"] == selected["id"]],
        "snapshot_id": manifest["snapshot_id"], "imported_at": NOW.isoformat(),
        "reused_semantic_annotations": False, "reused_search_results": False}
    packet = {"baseline": {"manifest": manifest}, "registry": {"sha256": "a" * 64}}
    bundle = {"as_of": NOW.isoformat(), "allowed_entities": ["us:NVDA"], "gaps": [],
        "baseline_refs": [receipt], "documents": [selected]}
    _verify_baseline(packet, bundle)
    omitted = deepcopy(bundle); omitted["baseline_refs"][0]["versions"] = []
    with pytest.raises(EvidenceInvalid): _verify_baseline(packet, omitted)
    relabelled = deepcopy(manifest)
    member = next(row for row in relabelled["members"] if row["id"] == selected["id"])
    member["entity_ids"] = member["requested_entity_ids"] = ["us:MSFT"]
    next(row for row in relabelled["versions"] if row["id"] == selected["id"])["entity_ids"] = ["us:MSFT"]
    relabelled["manifest_sha256"] = baseline_manifest_hash(relabelled)
    relabelled["snapshot_id"] = "snapshot_" + relabelled["manifest_sha256"][:32]
    changed = deepcopy(bundle)
    changed["baseline_refs"][0].update(manifest=relabelled,
        manifest_sha256=relabelled["manifest_sha256"], snapshot_id=relabelled["snapshot_id"], versions=[])
    with pytest.raises(EvidenceInvalid):
        _verify_baseline({**packet, "baseline": {"manifest": relabelled}}, changed)


def test_proof_strips_only_trusted_internal_parser_policy_and_preserves_other_identity_fields():
    from trading_system.intelligence.parse_limits import ParsePolicy
    from trading_system.intelligence.registry import SourceSpec
    spec = SourceSpec("reviewed", "page", "https://example.com/original", options={"extractor": {"html_engine": "scrapling"}})
    parser = ParsePolicy(html_engine="scrapling").public()
    assert _source_parameters(spec, "fetch", {"url": spec.endpoint, "_parse_policy": parser,
        "unexpected_field": "still-rejected-by-plan"}) == {"url": spec.endpoint, "unexpected_field": "still-rejected-by-plan"}
    assert _source_parameters(spec, "search", {"context": {"ticker": "NVDA", "_parse_policy": parser}}) == {"context": {"ticker": "NVDA"}}
    with pytest.raises(EvidenceInvalid):
        _source_parameters(spec, "fetch", {"url": spec.endpoint, "_parse_policy": ParsePolicy().public()})


def test_registered_page_parser_attachment_and_failure_receipt_replay_in_complete_paper_run(tmp_path, monkeypatch, web):
    from trading_system.intelligence import default_registry
    from trading_system.intelligence.registry import SourceSpec
    from trading_system.intelligence.transport import HttpTransport
    from trading_system.intelligence_workflow import TOOL_LIMITS
    from test_intelligence_workflow_v2 import intelligence_fixture
    import socket
    base, routes, calls = web
    original_request, original_init = HttpTransport.request, HttpTransport.__init__
    original_connect = socket.socket.connect
    execution, root, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch)
    packet = read_json(root, "intelligence-input.json")
    for source in packet["config"]["sources"].values(): source["enabled"] = False
    spec = SourceSpec("reviewed_page", "page", base + "/original", allowed_hosts=("127.0.0.1",),
        minimum_interval_seconds=0, max_retries=0, options={"extractor": {"html_engine": "scrapling", "max_input_bytes": 512}})
    packet["config"]["registered_sources"] = [spec.public()]
    packet["registry"] = default_registry(packet["config"]).public_manifest()
    atomic_json(root, "intelligence-input.json", packet)
    monkeypatch.setitem(TOOL_LIMITS, "requests", 6)
    monkeypatch.setitem(TOOL_LIMITS, "fetches", 4)
    monkeypatch.setitem(TOOL_LIMITS, "rounds", 1)
    routes["/robots.txt"] = (200, "text/plain", "User-agent: *\nAllow: /\n")
    routes["/original"] = (200, "text/html", b'<article><h1>Original measured release</h1><p>Complete original release text.</p>'
        b'<a href="/appendix.pdf">Original appendix</a></article>')
    routes["/appendix.pdf"] = (200, "application/pdf", b"%PDF-1.7\n" + b"TEST_FIXTURE bounded oversized attachment " * 32)
    def initialize(transport, *args, **kwargs):
        kwargs["allow_local"] = True
        original_init(transport, *args, **kwargs)
    monkeypatch.setattr(HttpTransport, "__init__", initialize)
    monkeypatch.setattr(HttpTransport, "request", original_request)
    def connect(transport_socket, address):
        assert address == ("127.0.0.1", int(base.rsplit(":", 1)[1])), "Paper fixture may contact only its explicit loopback source"
        return original_connect(transport_socket, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    bundle = Archive(root / "intelligence", readonly=True).bundle(result["raw"]["intelligence"]["bundle"]["bundle_id"])
    assert any(doc["normalization"].get("parse_failure") for doc in bundle["documents"])
    assert any(doc["normalization"].get("attachment", {}).get("parent_normalization", {}).get("operation") == "search"
        for doc in bundle["documents"])
    assert sum(call["path"] == "/appendix.pdf" for call in calls) == 1 and len(calls) == 3
    assert calls[0]["path"] == "/robots.txt" and bundle["usage"]["requests"] == 3
    monkeypatch.setattr(HttpTransport, "request", lambda *args, **kwargs: pytest.fail("Offline proof attempted source I/O"))
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York", "admitted_at_utc": execution.admitted_at.isoformat()}}
    assert verify_intelligence_model(root, result, admission) == result["raw"]["model"]


def test_new_snapshot_full_v2_run_replays_after_origin_archive_is_removed(tmp_path, monkeypatch):
    from trading_system.intelligence import IntelligenceService, build_plan, default_registry
    from trading_system.intelligence.transport import HttpResponse
    from test_intelligence_workflow_v2 import intelligence_fixture
    import hashlib
    execution, root, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch)
    packet = read_json(root, "intelligence-input.json")
    registry = default_registry(packet["config"])
    observed = execution.admitted_at - timedelta(days=1)
    class Transport:
        def request(self, method, url, **kwargs):
            if method == "POST":
                body = json.dumps({"results": [{"url": "https://www.reuters.com/fixture/scoped-prior", "title": "Prior source",
                    "date": "2026-10-19T14:00:00Z", "content": "Discovery only"}]}).encode()
                media = "application/json"
            else:
                body = b'<article><h1>Original prior source</h1><p>Original prior complete source retained for new research.</p></article>'
                media = "text/html"
            return HttpResponse(url, 200, {"content-type": media}, body, observed.isoformat(), hashlib.sha256(body).hexdigest())
    service = IntelligenceService(tmp_path / "prior", registry,
        registry.build_adapters(transport=Transport(), environment={"LLM_API_KEY": "fixture-only"}), clock=lambda: observed)
    plan = build_plan({"run_id": "prior-scoped", "as_of": execution.admitted_at.isoformat(),
        "universe": {"AAPL": "us:AAPL"}, "frontier": ["AAPL"], "holdings": [], "frontier_manifest_hash": "a" * 64,
        "required_cells": [{"target_id": "security:us:AAPL", "entity_id": "us:AAPL", "required": True,
            "question": "Prior original company baseline"}]}, registry)
    job = service.create_job(plan, "prior-scoped")
    service.collect(job["job_id"])
    manifest = service.archive.create_snapshot(execution.admitted_at, scope={"entity_ids": ["us:AAPL"]}, registry_hash=registry.public_manifest()["sha256"])
    packet["baseline"] = {"archive_root": str(service.archive.root), "manifest": manifest}
    atomic_json(root, "intelligence-input.json", packet)
    # A live writer appends after admission; this must not change the artifact
    # imported by the new paper run or its independently verified identity.
    service.archive.clock = lambda: execution.admitted_at + timedelta(minutes=1)
    service.archive.append(job["job_id"], "late_public_observation", {"not_part_of_snapshot": True})
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    assert result["raw"]["intelligence"]["baseline_snapshot"]["snapshot_id"] == manifest["snapshot_id"]
    frozen = Archive(root / "intelligence", readonly=True).bundle(result["raw"]["intelligence"]["bundle"]["bundle_id"])
    assert frozen["baseline_refs"][0]["snapshot_id"] == manifest["snapshot_id"]
    assert frozen["baseline_refs"][0]["reused_semantic_annotations"] is False
    shutil.rmtree(service.archive.root)
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York", "admitted_at_utc": execution.admitted_at.isoformat()}}
    assert verify_intelligence_model(root, result, admission) == result["raw"]["model"]
    broken = deepcopy(packet); broken["baseline"]["manifest"]["cutoff"] = "2099-01-01T00:00:00+00:00"
    with pytest.raises(EvidenceInvalid): _verify_baseline(broken, frozen)
