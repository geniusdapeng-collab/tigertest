"""Independent v2 publication proof; all upstream I/O is explicitly offline."""
from __future__ import annotations

from dataclasses import asdict
import copy
import hashlib
import json
import shutil

import pytest

from trading_system.agent_evidence import (EvidenceInvalid, atomic_json, atomic_bytes,
    read_json, read_bytes, file_ref, digest, json_bytes, SNAPSHOT_SCHEMA, verify_events)
from trading_system.llm.intelligence_proof import verify_intelligence_model, _hash
from trading_system.redline.execution import daily_stage_contract
from trading_system.intelligence import default_registry
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from test_intelligence_workflow_v2 import intelligence_fixture
import main


@pytest.fixture(scope="module")
def completed(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        execution, root, args, _, _, _ = intelligence_fixture(tmp_path_factory.mktemp("v2-proof"), patch)
        output = main._daily(args, "yahoo", execution=execution)
        admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                               "admitted_at_utc": execution.admitted_at.isoformat()}}
        yield root, asdict(output.result), admission


@pytest.fixture
def isolated(completed, tmp_path):
    root, result, admission = completed
    target = tmp_path / root.name
    shutil.copytree(root, target)
    return target, copy.deepcopy(result), copy.deepcopy(admission)


def _events_rebind(root, old, new):
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    previous = None
    for row in events:
        for name in ("input_ref", "output_ref", "previous_stage_ref"):
            if row[name] == old:
                row[name] = new
        row["previous_event_sha256"] = previous
        row["event_sha256"] = digest(json_bytes({key: value for key, value in row.items() if key != "event_sha256"}))
        previous = row["event_sha256"]
    atomic_bytes(root, "events.jsonl", b"".join(json_bytes(row) for row in events))


def _mutate(root, stage, transform):
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    end = next(row for row in events if row["stage"] == stage and row["kind"] == "end")
    old = end["output_ref"]
    snapshot = read_json(root, old["path"])
    transform(snapshot["value"])
    atomic_json(root, old["path"], snapshot)
    _events_rebind(root, old, file_ref(root, old["path"], SNAPSHOT_SCHEMA))
    # All deliberately altered journal envelopes are valid. Only a semantic or
    # source reconstruction check is allowed to reject them in these tests.
    verify_events(root, root.name, daily_stage_contract(True, intelligence=True))


def _mutate_io(root, stage, method, role, transform):
    def change(output):
        call = next(row for row in output["io_calls"] if row["method"] == method)
        ref = call[role + "_ref"]
        blob = read_json(root, ref["path"])
        transform(blob["value"])
        atomic_json(root, ref["path"], blob)
        call[role + "_ref"] = file_ref(root, ref["path"], SNAPSHOT_SCHEMA)
    _mutate(root, stage, change)


def test_full_v2_receipt_replays_source_parsers_models_null_targets_and_final_binding(completed, monkeypatch):
    root, result, admission = completed
    # Even a verifier regression must not be able to call a real source/model.
    monkeypatch.setattr("trading_system.intelligence.transport.HttpTransport.request",
                        lambda *args, **kwargs: pytest.fail("Proof attempted external source I/O"))
    monkeypatch.setattr("trading_system.llm.deepseek_required.DeepSeekRequiredClient._post_json",
                        lambda *args, **kwargs: pytest.fail("Proof attempted model I/O"))
    assert verify_intelligence_model(root, result, admission) == result["raw"]["model"]


def test_copied_same_name_run_replays_without_access_to_mutable_original_paths(isolated, monkeypatch):
    root, result, admission = isolated
    monkeypatch.setattr("trading_system.providers.get_provider",
                        lambda *args, **kwargs: pytest.fail("Proof constructed a provider"))
    monkeypatch.setattr("trading_system.options_metrics.write_json_atomic",
                        lambda *args, **kwargs: pytest.fail("Proof invoked an ambient options writer"))
    assert verify_intelligence_model(root, result, admission) == result["raw"]["model"]


@pytest.mark.parametrize("field", ["model", "summary", "action", "company", "market", "gaps", "frontier"])
def test_public_result_mutations_never_authorize_themselves(completed, field):
    root, result, admission = completed
    changed = copy.deepcopy(result)
    if field == "model":
        changed["raw"]["model"]["call_count"] += 1
    elif field == "summary":
        changed["raw"]["action_summary"] += "伪造结论"
    elif field == "action":
        changed["action"] = "BUY"
    elif field == "company":
        changed["raw"]["debate"]["company_evidence"]["manifest_sha256"] = "f" * 64
    elif field == "market":
        changed["raw"]["execution_decisions"][0]["market_evidence"][0]["symbol"] = "OTHER"
    elif field == "gaps":
        changed["raw"]["intelligence"]["gaps"][0]["status"] = "present"
    else:
        changed["raw"]["intelligence"]["frontier"]["symbols"].append("UNRESEARCHED")
    with pytest.raises(EvidenceInvalid):
        verify_intelligence_model(root, changed, admission)


@pytest.mark.parametrize("stage,variant", [
    ("clean.llm_semantic", "eligibility"), ("clean.llm_semantic", "assessment"),
    ("tech.sentiment", "score"), ("tech.risk", "alert"), ("tech.fusion", "risk"),
    ("sector.narrative", "score"), ("intelligence.frontier", "rank"),
    ("intelligence.freeze", "coverage"), ("decision.debate", "company"),
    ("layer4.risk", "approved"),
])
def test_rehashed_stage_forgery_is_rejected_beyond_journal_hashes(isolated, stage, variant):
    root, result, admission = isolated
    def change(output):
        value = output["value"]
        if variant == "eligibility":
            value["documents"][0]["fields"]["eligibility"]["allowed_uses"] = ["action"]
        elif variant == "assessment":
            value["acquisition"]["rounds"][0]["assessment"]["coverage"][0]["status"] = "present"
        elif variant == "score" and stage == "tech.sentiment":
            value[next(iter(value))]["fields"]["heat"] = 10
        elif variant == "alert":
            value.append({"severity": 10, "headline_zh": "伪造风险"})
        elif variant == "risk":
            value[0]["fields"]["risk_level"] = 0
        elif variant == "score":
            value["narrative"][next(iter(value["narrative"]))]["score"] = 10
        elif variant == "rank":
            value["symbols"] = []
        elif variant == "coverage":
            value["missing_required"] = []
        elif variant == "company":
            value["company_evidence"]["bundle_id"] = "bundle_" + "f" * 32
        else:
            value["action"] = "BUY"
    _mutate(root, stage, change)
    with pytest.raises(EvidenceInvalid):
        verify_intelligence_model(root, result, admission)


@pytest.mark.parametrize("kind", ["source_normalization", "native_identity", "model_usage", "model_scene", "model_fingerprint", "target_schema"])
def test_rehashed_external_receipt_forgery_is_rejected(isolated, kind):
    root, result, admission = isolated
    if kind == "source_normalization":
        _mutate_io(root, "search.collect", "intelligence.source.request", "output",
                   lambda value: value["normalized"]["hits"][0].update(text="已篡改规范化正文"))
    elif kind == "native_identity":
        _mutate_io(root, "search.collect", "model.deepseek.search", "output",
                   lambda value: value["results"][0].update(id="ds_" + "f" * 24))
    elif kind == "model_usage":
        _mutate_io(root, "tech.risk", "model.deepseek.chat", "output",
                   lambda value: value["usage"].update(total_tokens=201))
    elif kind == "model_scene":
        _mutate_io(root, "tech.risk", "model.deepseek.chat", "output",
                   lambda value: value.update(scene="different-scene"))
    elif kind == "model_fingerprint":
        def malformed(value):
            value["system_fingerprint"] = value["response"]["system_fingerprint"] = {"not": "an identifier"}
            value["response_sha256"] = _hash(value["response"])
        _mutate_io(root, "tech.risk", "model.deepseek.chat", "output", malformed)
    else:
        _mutate_io(root, "tech.sentiment", "model.deepseek.chat", "input",
                   lambda value: value["output_schema"]["properties"]["chains"].update(maxItems=100))
    with pytest.raises(EvidenceInvalid):
        verify_intelligence_model(root, result, admission)


def test_source_blob_bytes_are_checked_before_using_annotations(isolated):
    root, result, admission = isolated
    from trading_system.intelligence.archive import Archive
    bundle_id = result["raw"]["intelligence"]["bundle"]["bundle_id"]
    archive = Archive(root / "intelligence", readonly=True)
    bundle = archive.bundle(bundle_id)
    path = archive.root / bundle["documents"][0]["raw_blob"]["path"]
    path.write_bytes(path.read_bytes() + b" altered")
    with pytest.raises(EvidenceInvalid):
        verify_intelligence_model(root, result, admission)


def test_policy_order_cannot_be_replaced_by_a_public_result_flag(isolated):
    root, result, admission = isolated
    atomic_json(root, "policy.json", {"paper_ledger_contract": "tiger.paper-execution.v1",
        "stages": daily_stage_contract(True, intelligence=True, authoritative_paper=True)})
    result["raw"]["paper_execution_version"] = "tiger.paper-execution.v1"
    with pytest.raises(EvidenceInvalid):
        verify_intelligence_model(root, result, admission)


def test_deepseek_fetch_compatibility_projection_remains_replayable_partial_evidence(tmp_path, monkeypatch):
    execution, root, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch)
    packet = read_json(root, "intelligence-input.json")
    packet["config"]["sources"]["web_page"]["enabled"] = False
    packet["registry"] = default_registry(packet["config"]).public_manifest()
    atomic_json(root, "intelligence-input.json", packet)
    original = DeepSeekRequiredClient._post_json
    def post(client, path, request):
        if path != "/v1/tools/fetch":
            return original(client, path, request)
        payload = {"title": "TEST_FIXTURE provider Fetch", "content": "Complete available provider projection " + request["url"]}
        return payload, _hash(payload)
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                           "admitted_at_utc": execution.admitted_at.isoformat()}}
    summary = verify_intelligence_model(root, result, admission)
    assert summary["fetch_count"] > 0 and result["raw"]["intelligence"]["state"] == "partial"
    assert result["picks"] == []


def test_a_rehashed_prompt_cannot_omit_the_frozen_documents(isolated):
    root, result, admission = isolated
    def change(output):
        call = output["io_calls"][0]
        request_ref = call["input_ref"]
        snapshot = read_json(root, request_ref["path"])
        request = snapshot["value"]
        request["request"]["messages"][1]["content"] = request["request"]["messages"][1]["content"].replace(
            '"documents":', '"documents_omitted":', 1)
        request["request_sha256"] = _hash(request["request"])
        atomic_json(root, request_ref["path"], snapshot)
        call["input_ref"] = file_ref(root, request_ref["path"], SNAPSHOT_SCHEMA)
        proof_ref = call["output_ref"]
        proof = read_json(root, proof_ref["path"])
        proof["value"]["request_sha256"] = request["request_sha256"]
        atomic_json(root, proof_ref["path"], proof)
        call["output_ref"] = file_ref(root, proof_ref["path"], SNAPSHOT_SCHEMA)
    _mutate(root, "tech.sentiment", change)
    with pytest.raises(EvidenceInvalid):
        verify_intelligence_model(root, result, admission)


@pytest.mark.parametrize("representation", ["html", "json"])
def test_source_secret_redaction_is_a_replayable_partial_version_without_leaking_key(tmp_path, monkeypatch, representation):
    from trading_system.intelligence.transport import HttpTransport
    execution, root, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch)
    original = HttpTransport.request
    def fetch(transport, method, url, **kwargs):
        response = original(transport, method, url, **kwargs)
        from dataclasses import replace
        if representation == "json":
            body = b'{ "public": "fixture-secret-only", "value": 1 }\n'
            headers = {"content-type": "application/json"}
        else:
            body = response.body.replace(b"Original source article.", b"Original source article fixture-secret-only.")
            headers = response.headers
        return replace(response, headers=headers, body=body, transport_sha256=hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(HttpTransport, "request", fetch)
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                           "admitted_at_utc": execution.admitted_at.isoformat()}}
    assert verify_intelligence_model(root, result, admission) == result["raw"]["model"]
    from trading_system.intelligence.archive import Archive
    archive = Archive(root / "intelligence", readonly=True)
    bundle = archive.bundle(result["raw"]["intelligence"]["bundle"]["bundle_id"])
    redacted = [doc for doc in bundle["documents"] if doc["normalization"].get("raw_redacted")]
    assert redacted and all(doc["completeness"] == "partial" for doc in redacted)
    assert all(b"fixture-secret-only" not in archive.read_blob(doc["raw_blob"]) for doc in bundle["documents"])
    assert all("fixture-secret-only" not in doc["text"] for doc in bundle["documents"])
    assert result["picks"] == []


def test_actual_authoritative_policy_binds_paper_order_and_independent_paper_ledger(tmp_path, monkeypatch):
    from trading_system.agent_runtime import policy_manifest
    from trading_system.redline.execution import INTELLIGENCE_PROFILE
    from trading_system.paper_proof import verify_paper_execution
    execution, root, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch, authoritative=True)
    packet = read_json(root, "intelligence-input.json")
    atomic_json(root, "policy.json", policy_manifest(INTELLIGENCE_PROFILE, intelligence_input=packet,
        response_observation_contract=execution.response_observation_contract))
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                           "admitted_at_utc": execution.admitted_at.isoformat()}}
    assert result["raw"]["paper_execution_version"] == "tiger.paper-execution.v1"
    assert verify_intelligence_model(root, result, admission) == result["raw"]["model"]
    admission["parent"] = None
    paper = verify_paper_execution(root, admission, result, output.sim_state)
    assert paper["contract"] == "tiger.paper-execution.v1"


def test_qualified_originals_keep_assessed_target_separate_from_unknown_targets(tmp_path, monkeypatch):
    from datetime import timedelta
    from urllib.parse import urlsplit
    from trading_system.intelligence.service import IntelligenceService
    from trading_system.intelligence.transport import HttpResponse, HttpTransport
    from test_structured_evidence import _doc

    execution, root, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch)
    observed_at = execution.admitted_at - timedelta(minutes=1)
    original_init = IntelligenceService.__init__
    def initialize(service, *args, **kwargs):
        kwargs["clock"] = lambda: observed_at
        original_init(service, *args, **kwargs)
    monkeypatch.setattr(IntelligenceService, "__init__", initialize)

    def article(url):
        words = "first" if urlsplit(url).hostname == "www.reuters.com" else "second"
        doc = _doc(name="AAPL", symbol="AAPL", words=words)
        doc.sentiment, doc.sentiment_score = "neutral", 0
        doc.event_records[0]["target_ids"] = ["memory", "XLK"]
        return doc

    def fetch(transport, method, url, **kwargs):
        text = article(url).raw.content
        body = ('<html><head><title>Independent source fixture</title>'
                '<meta property="article:published_time" content="2026-10-19T14:00:00Z">'
                '</head><body><article><pre>' + text + '</pre></article></body></html>').encode()
        return HttpResponse(url, 200, {"content-type": "text/html"}, body,
                            observed_at.isoformat(), hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(HttpTransport, "request", fetch)
    original_post = DeepSeekRequiredClient._post_json
    def post(client, path, request):
        response, _ = original_post(client, path, request)
        if path == "/v1/tools/search":
            for row in response["results"]:
                if int(row["url"][-1], 16) % 2:
                    row["url"] = row["url"].replace("www.reuters.com", "www.bloomberg.com")
            return response, _hash(response)
        content = request["messages"][1]["content"]
        payload = json.loads(content.split("\n", 1)[1].split("\nJSON schema:\n")[0])
        result = json.loads(response["choices"][0]["message"]["content"])
        stage = client.execution._active.spec.name
        if stage == "clean.llm_semantic":
            annotated = []
            for document in payload["documents"]:
                if "AAPL (AAPL)" not in document.get("content", ""):
                    annotated.append(next(row for row in result["documents"] if row["id"] == document["id"]))
                    continue
                template = article(document["url"])
                value = asdict(template)
                annotated.append({"id": document["id"], **{key: value[key] for key in (
                    "tickers", "sentiment", "sentiment_score", "events", "summary_zh", "relevance",
                    "subjects", "evidence_spans", "event_records", "claims")}})
            result["documents"] = annotated
        elif stage in {"tech.sentiment", "tech.risk", "sector.narrative"}:
            key = "sectors" if stage == "sector.narrative" else "chain_assessments" if stage == "tech.risk" else "chains"
            name = "etf" if stage == "sector.narrative" else "chain_id"
            target = "XLK" if stage == "sector.narrative" else "memory"
            refs = payload["target_sources"][target]
            assert refs, "The offline independent originals must establish this target's evidence"
            row = next(row for row in result[key] if row[name] == target)
            row.update(status="assessed", evidence_ids=refs[:2])
            if stage == "tech.sentiment":
                row.update(heat=6, sentiment_score=.2, narrative_change="improving")
            elif stage == "tech.risk":
                row.update(risk_level=0)
            else:
                row.update(narrative_score=6, eps_revision_bias="up", guidance_tone="positive")
        response["choices"][0]["message"]["content"] = json.dumps(result)
        return response, _hash(response)
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                           "admitted_at_utc": execution.admitted_at.isoformat()}}
    assert verify_intelligence_model(root, result, admission) == result["raw"]["model"]
    signals = {signal["chain_id"]: signal for signal in result["raw"]["tech_signals"]}
    assert signals["memory"]["risk_level"] == 0 and signals["memory"]["risk_assessment_status"] == "assessed"
    assert signals["logic"]["risk_level"] is None
    assert signals["logic"]["risk_assessment_status"] == "insufficient_evidence"


def test_explicit_raw_baseline_is_copied_reannotated_and_replayed_without_ambient_archive(tmp_path, monkeypatch):
    from datetime import timedelta
    from urllib.parse import urlsplit
    from trading_system.intelligence import IntelligenceService, build_plan
    from trading_system.intelligence.transport import HttpResponse
    from test_structured_evidence import _doc
    execution, root, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch)
    packet = read_json(root, "intelligence-input.json")
    registry = default_registry(packet["config"])
    observed_at = execution.admitted_at - timedelta(days=1)
    def article(url):
        doc = _doc(name="AAPL", symbol="AAPL", words="first" if urlsplit(url).hostname == "www.reuters.com" else "second")
        doc.sentiment, doc.sentiment_score = "neutral", 0
        doc.event_records[0]["target_ids"] = ["memory", "XLK"]
        return doc
    class FrozenBaselineTransport:
        def request(self, method, url, **kwargs):
            if method == "POST":
                value = {"results": [{"url": "https://" + host + "/fixture/prior-archive",
                    "title": "Explicit prior raw source", "content": "Prior provider discovery only",
                    "date": "2026-10-19T14:00:00Z"} for host in ("www.reuters.com", "www.bloomberg.com")]}
                body, media = json.dumps(value).encode(), "application/json"
            else:
                body = ('<html><head><title>Prior raw article</title>'
                    '<meta property="article:published_time" content="2026-10-19T14:00:00Z">'
                    '</head><body><article><pre>' + article(url).raw.content + '</pre></article></body></html>').encode()
                media = "text/html"
            return HttpResponse(url, 200, {"content-type": media}, body,
                                observed_at.isoformat(), hashlib.sha256(body).hexdigest())
    baseline = IntelligenceService(tmp_path / "prior-archive", registry,
        registry.build_adapters(transport=FrozenBaselineTransport(), environment={"LLM_API_KEY": "baseline-fixture-only"}),
        clock=lambda: observed_at)
    plan = build_plan({"run_id": "explicit-baseline-fixture", "as_of": execution.admitted_at.isoformat(),
        "market": "us", "universe": {"AAPL": "us:AAPL"}, "frontier": ["AAPL"], "holdings": [],
        "frontier_manifest_hash": "a" * 64, "required_cells": [{"cell_id": "prior-cell", "target_id": "security:us:AAPL",
            "question": "Prior original baseline", "query": "prior original baseline", "required": True}]}, registry)
    job = baseline.create_job(plan, "prior-raw-only")
    prior = baseline.collect(job["job_id"])
    assert prior["documents"] and baseline.archive.assessment(job["job_id"])["claims"] == []
    manifest = baseline.archive_versions(execution.admitted_at.isoformat())
    packet["baseline"] = {"archive_root": str(baseline.archive.root), "manifest": manifest}
    atomic_json(root, "intelligence-input.json", packet)
    original_post = DeepSeekRequiredClient._post_json
    def post(client, path, request):
        response, wire_hash = original_post(client, path, request)
        if path != "/chat/completions" or client.execution._active.spec.name != "clean.llm_semantic":
            return response, wire_hash
        content = request["messages"][1]["content"]
        payload = json.loads(content.split("\n", 1)[1].split("\nJSON schema:\n")[0])
        output = json.loads(response["choices"][0]["message"]["content"])
        for index, document in enumerate(payload["documents"]):
            if document["url"].endswith("/prior-archive") and "AAPL (AAPL)" in document.get("content", ""):
                annotation = asdict(article(document["url"]))
                output["documents"][index] = {"id": document["id"], **{key: annotation[key] for key in (
                    "tickers", "sentiment", "sentiment_score", "events", "summary_zh", "relevance",
                    "subjects", "evidence_spans", "event_records", "claims")}}
        response["choices"][0]["message"]["content"] = json.dumps(output)
        return response, _hash(response)
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    # All required bytes/manifest commitments are now inside this run. Replay
    # never consults a changed or unavailable historical archive location.
    shutil.rmtree(baseline.archive.root)
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                           "admitted_at_utc": execution.admitted_at.isoformat()}}
    assert verify_intelligence_model(root, result, admission) == result["raw"]["model"]
    from trading_system.intelligence.archive import Archive
    frozen = Archive(root / "intelligence", readonly=True).bundle(result["raw"]["intelligence"]["bundle"]["bundle_id"])
    assert frozen["baseline_refs"][0]["reused_semantic_annotations"] is False
    assert {row["id"] for row in manifest["versions"]} <= {row["id"] for row in frozen["documents"]}
    assert result["raw"]["cross_validation"]["corroborated"] == 2
    assert any(row["status"] == "present" and row["target_id"] == "chain:memory" for row in frozen["gaps"])


@pytest.mark.parametrize("http_status", [503, None])
def test_failed_discovery_calls_preserve_billing_certainty_and_never_become_clean_risk(tmp_path, monkeypatch, http_status):
    from trading_system.redline.model_diagnostics import RequiredModelUnavailable
    execution, root, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch)
    original = DeepSeekRequiredClient._post_json
    def post(client, path, request):
        if path == "/v1/tools/search":
            raise (RequiredModelUnavailable("HTTP_ERROR", http_status=http_status) if http_status is not None
                   else RequiredModelUnavailable("NETWORK_TIMEOUT"))
        return original(client, path, request)
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
    monkeypatch.setattr("trading_system.llm.deepseek_required.time.sleep", lambda seconds: None)
    if http_status is None:
        from trading_system.intelligence.archive import Archive
        from trading_system.intelligence.contracts import IntelligenceError
        with pytest.raises(IntelligenceError) as error:
            main._daily(args, "yahoo", execution=execution)
        assert error.value.code == "RECONCILIATION_REQUIRED"
        archive = Archive(root / "intelligence", readonly=True)
        with archive._session() as connection:
            job_id = connection.execute("SELECT job_id FROM jobs").fetchone()[0]
        attempts = archive.attempts(job_id)
        assert len(attempts) == 1 and attempts[0]["status"] == "unknown"
        assert attempts[0]["result"]["usage"]["requires_reconciliation"] is True
        assert not (root / "result.json").exists()
        return
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                           "admitted_at_utc": execution.admitted_at.isoformat()}}
    summary = verify_intelligence_model(root, result, admission)
    assert summary["call_count"] == 5 and summary["search_count"] == 0
    assert result["raw"]["docs_collected"] == 0 and result["raw"]["intelligence"]["state"] == "partial"
    assert (result["raw"]["intelligence"]["usage"]["unreconciled_attempts"] > 0) == (http_status is None)
    assert all(row["risk_level"] is None for row in result["raw"]["tech_signals"])
    assert result["picks"] == []
    source_gaps = [row for row in result["raw"]["intelligence"]["gaps"] if row.get("source_reason")]
    assert source_gaps and all(row["source_diagnostics"] for row in source_gaps)
    changed = copy.deepcopy(result)
    next(row for row in changed["raw"]["intelligence"]["gaps"] if row.get("source_reason"))["source_reason"] = "CLEARED_BY_SAVED_FLAG"
    with pytest.raises(EvidenceInvalid):
        verify_intelligence_model(root, changed, admission)


def test_connected_holding_outside_reviewed_roster_has_a_required_labeled_gap_in_full_run(tmp_path, monkeypatch):
    from trading_system.simulator import SimEngine
    execution, root, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch)
    books = SimEngine(str(execution.paths.sim_path))
    books.state["cash"] = 99_900.0
    books.state["positions"] = [{"ticker": "ZZZ", "shares": 1, "entry_price": 100.0,
        "stop": 90.0, "entry_date": "2026-10-19", "chain": "semis", "sector": "XLK",
        "risk_usd": 10.0, "time_stop_days": 7, "peak_price": 100.0,
        "bars_held": 0, "source": "yahoo", "note": "Existing admitted holding fixture"}]
    books.save()
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                           "admitted_at_utc": execution.admitted_at.isoformat()}}
    assert verify_intelligence_model(root, result, admission) == result["raw"]["model"]
    holding = next(row for row in result["raw"]["intelligence"]["gaps"] if row["target_id"] == "account:holding:ZZZ")
    assert holding["required"] is True and holding["status"] == "unverifiable"
    assert holding["target_label"] == "ZZZ 持仓（不在审定池）"
    assert holding["reason"] == "HOLDING_OUTSIDE_REVIEWED_ROSTER"
    assert holding["cell_id"] in result["raw"]["intelligence"]["bundle"]["missing_required"]
    assert result["picks"] == []


def test_pending_outside_reviewed_roster_is_separate_and_blocks_new_entries_in_full_run(tmp_path, monkeypatch):
    from trading_system.simulator import SimEngine
    execution, root, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch)
    books = SimEngine(str(execution.paths.sim_path))
    books.state["pending"] = [{"ticker": "ZZZ", "shares": 1, "entry_ref": 100.0,
        "stop_price": 90.0, "risk_usd": 10.0, "chain": "semis", "sector": "XLK",
        "signal_date": execution.run_date.isoformat(), "time_stop_days": 7,
        "source": "yahoo", "note": "Existing admitted pending fixture"}]
    books.save()
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
                           "admitted_at_utc": execution.admitted_at.isoformat()}}
    assert verify_intelligence_model(root, result, admission) == result["raw"]["model"]
    pending = next(row for row in result["raw"]["intelligence"]["gaps"] if row["target_id"] == "account:pending:ZZZ")
    assert pending["required"] is True and pending["status"] == "unverifiable"
    assert pending["target_label"] == "ZZZ 待成交（不在审定池）"
    assert pending["reason"] == "PENDING_OUTSIDE_REVIEWED_ROSTER"
    assert pending["cell_id"] in result["raw"]["intelligence"]["bundle"]["missing_required"]
    assert not any(row["target_id"] == "account:holding:ZZZ" for row in result["raw"]["intelligence"]["gaps"])
    assert result["picks"] == []
