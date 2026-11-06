"""Topic work uses actual SQLite, immutable snapshots and the real model adapter.

Only HTTP responses and prefetch inputs are deterministic fixtures in this file.
No test claims production model quality or source throughput.
"""
from copy import deepcopy
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
import requests

from trading_system.agent_evidence import atomic_json
from trading_system.intelligence.archive import Archive
from trading_system.intelligence.catalog import SourceCatalog
from trading_system.intelligence.contracts import IntelligenceError, digest, source_time, timestamp, utc_now
from trading_system.intelligence.planner import build_plan
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.service import IntelligenceService
from trading_system.intelligence.topic import (DEFAULT_POLICY, TopicExecution, TopicModelClient, TopicResearch,
    TopicStore, topic_coverage, trusted_policy_value, validate_analysis)
from trading_system.intelligence.topic_cli import execute, validate_request
from trading_system.llm.model_budget import TokenBudgetError


TEXT = "NVDA published a documented forecast with important uncertainty and contrary observations."
DOCUMENT = "topic_fixture_document"
ANALYSIS = {"summary": "The supplied forecast is conditional.", "conclusions": [
    {"id": "claim-1", "text": "NVDA published a forecast.", "kind": "opinion", "dimension_id": "news_events",
     "entity_id": "us:NVDA", "supports": [{"document_id": DOCUMENT, "quote": TEXT}], "refutes": [],
     "conditions": "Forecast and not a realized result."}], "unknowns": ["Entitled reports are unavailable."],
    "changes": [], "next_review": "Review on a new company disclosure.", "followup_questions": []}
REVIEW = {"accepted": True, "issues": [], "summary": "Exact citation and conditional wording are supported."}


class Response:
    def __init__(self, value):
        self.raw = io.BytesIO(json.dumps(value).encode())
        self.status_code = 200
        self.headers = {"Content-Type": "application/json"}
    def close(self):
        self.raw.close()


class FixtureResearch(TopicResearch):
    def _acquire(self, job, questions, round_index):
        directory = self.store.directory(job["job_id"])
        name = f"acquisition-{round_index}.json"
        if (directory / name).exists():
            return json.loads((directory / name).read_text())
        service = self._service()
        now = utc_now()
        plan = build_plan({"run_id": "fixture_" + str(round_index), "as_of": timestamp(now),
            "universe": {}, "frontier_manifest_hash": "0" * 64,
            "source_ids": ["web_page"], "required_cells": [{"target_id": "topic:" + job["topic"]["topic_id"],
            "question": "Documented evidence", "required": True}]}, self.registry)
        acquired = service.create_job(plan, job["job_id"] + ":fixture:" + str(round_index))
        record = {"id": DOCUMENT, "doc_id": "logical_topic_fixture", "version": digest(TEXT),
            "source_id": "web_page", "source_version": "1", "title": "Original forecast",
            "final_url": "https://example.com/research", "text": TEXT, "text_kind": "main_text",
            "completeness": "complete", "material_type": "news_events",
            "raw_blob": service.archive.put_blob(TEXT.encode(), "text/plain"),
            "text_blob": service.archive.put_blob(TEXT.encode(), "text/plain"),
            "times": {"first_seen": source_time(timestamp(now)),
                "published": source_time(timestamp(now), evidence={"kind": "TEST_FIXTURE_declared_publication"})}}
        service.archive.put_document(acquired["job_id"], record, attempt_id="fixture-attempt",
            targets=("topic:" + job["topic"]["topic_id"],), entities=("us:NVDA",))
        result = {"state": "completed", "usage": {"requests": 0, "fetches": 0, "bytes": len(TEXT)}}
        atomic_json(directory, name, result, exclusive=True)
        return result


@pytest.fixture
def research(tmp_path, monkeypatch):
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "test-only-topic-credential"}.items():
        monkeypatch.setenv(key, value)
    sent = []
    def post(url, **kwargs):
        wire = json.loads(kwargs["data"])
        sent.append(wire)
        parsed = deepcopy(ANALYSIS if wire.get("reasoning_effort") == "high" else REVIEW)
        return Response({"id": "topic-response-" + str(len(sent)), "model": "deepseek-flash",
            "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(parsed)}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}})
    monkeypatch.setattr(requests, "post", post)
    registry = SourceRegistry([SourceSpec("web_page", "web_fetch", "", capabilities=("fetch",), options={"material_types": ["news_events"]})])
    service = FixtureResearch(tmp_path / "state", tmp_path / "archive", mode="research", registry=registry,
        service_factory=lambda root, r: IntelligenceService(root, r, {}))
    topic = service.create({"question": "Research NVDA with evidence and counterevidence", "entity_ids": ["us:NVDA"],
        "material_policy": {"issuer_disclosure": "supplemental"}, "idempotency_key": "topic-scope"})
    job = service.submit({"topic_id": topic["topic_id"], "version": 1, "idempotency_key": "topic-research"})
    return service, topic, job, sent


def test_full_topic_flow_exact_sources_same_model_review_and_independent_report(research):
    service, topic, job, sent = research
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    assert report["account_connected"] is False
    assert len(sent) == 2 and [wire["reasoning_effort"] for wire in sent] == ["high", "max"]
    assert all(TEXT in wire["messages"][1]["content"] and wire["max_tokens"] > 300000 for wire in sent)
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]
    assert len(service.store.attempts(job["job_id"])) == 2
    assert service.run(job["job_id"])["receipt_sha256"] == report["receipt_sha256"] and len(sent) == 2
    assert "仍不知道" in report["readable_report"]


def test_idempotency_conflict_and_same_identity(research):
    service, topic, job, _ = research
    assert service.submit(job["request"])["job_id"] == job["job_id"]
    with pytest.raises(IntelligenceError) as error:
        service.submit({**job["request"], "version": 2})
    assert error.value.code == "EVIDENCE_NOT_FOUND"
    other = service.create({"question": "Other scope", "idempotency_key": "other-topic"})
    with pytest.raises(IntelligenceError) as error:
        service.submit({**job["request"], "topic_id": other["topic_id"]})
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_unknown_dispatch_holds_and_never_repeats_paid_request(research, monkeypatch):
    service, topic, job, sent = research
    calls = []
    def broken(*args, **kwargs):
        calls.append(1)
        raise requests.Timeout()
    monkeypatch.setattr(requests, "post", broken)
    first = service.run(job["job_id"])
    assert first["state"] == "failed"
    assert service.store.attempts(job["job_id"])[0]["state"] == "unknown"
    second = service.run(job["job_id"])
    assert second["state"] == "blocked" and second["error"]["code"] == "USAGE_RECONCILIATION_REQUIRED"
    assert len(calls) == 1


def test_model_attempt_admission_denies_before_network(research):
    service, topic, job, sent = research
    request = {"model": "deepseek-flash", "max_tokens": 393216}
    attempt = service.store.reserve(job["job_id"], "sector.narrative", request)
    with pytest.raises(IntelligenceError) as error:
        service.store.reserve(job["job_id"], "sector.narrative", request)
    assert error.value.code == "USAGE_RECONCILIATION_REQUIRED" and not sent
    service.store.settle(job["job_id"], attempt, {"usage": {"total_tokens": 9999999}})
    assert service.store.attempts(job["job_id"])[0]["state"] == "overrun"


def test_required_missing_dimension_is_partial_not_completed(research):
    service, topic, job, sent = research
    missing = service.create({"question": "Need licensed report", "entity_ids": ["us:NVDA"],
        "material_policy": {"issuer_disclosure": "supplemental", "formal_research": "required"}, "idempotency_key": "missing-source-topic"})
    job = service.submit({"topic_id": missing["topic_id"], "version": 1, "idempotency_key": "missing-research"})
    result = service.run(job["job_id"])
    assert result["state"] == "partial" and result["required_gaps"]


def test_report_and_raw_tampering_fails(research):
    service, _, job, _ = research
    report = service.run(job["job_id"])
    path = service.store.directory(job["job_id"]) / "report.json"
    original = path.read_bytes()
    changed = json.loads(original)
    changed["analysis"]["summary"] = "tampered"
    path.write_text(json.dumps(changed))
    with pytest.raises(IntelligenceError):
        service.report(job["job_id"])
    path.write_bytes(original)
    doc = service._service().archive.document(DOCUMENT)
    (service.archive_root / doc["raw_blob"]["path"]).write_text("tampered")
    with pytest.raises(IntelligenceError):
        service.report(job["job_id"])


def test_checkpoint_resumes_same_job_without_reacquiring_or_regenerating(research, monkeypatch):
    service, _, job, sent = research
    original = TopicModelClient.complete_json
    def fail_review(self, **kwargs):
        if self.execution._active.spec.name == "decision.debate":
            raise IntelligenceError("WALL_DEADLINE")
        return original(self, **kwargs)
    monkeypatch.setattr(TopicModelClient, "complete_json", fail_review)
    assert service.run(job["job_id"])["state"] == "failed"
    assert len(sent) == 1
    monkeypatch.setattr(TopicModelClient, "complete_json", original)
    result = service.run(job["job_id"])
    assert result["state"] == "completed" and len(sent) == 2
    assert len(list(service.store.directory(job["job_id"]).glob("acquisition-*.json"))) == 1


@pytest.mark.parametrize("operation,input_value", [("create", {"question": "Q", "idempotency_key": "k", "url": "https://example.com"}),
    ("research", {"topic_id": "topic", "version": True, "idempotency_key": "k"}), ("status", {}),
    ("status", {"job_id": "../escape"}), ("report", {"job_id": "run-1"}), ("health", {"key": "secret"})])
def test_typed_inputs_reject_extra_fields_and_path_identity(operation, input_value):
    with pytest.raises(IntelligenceError):
        validate_request(operation, input_value)


def test_readonly_rejects_before_initialization(tmp_path):
    for operation in ("create", "research", "watch", "tick"):
        with pytest.raises(IntelligenceError) as error:
            execute(operation, {"question": "Q", "idempotency_key": "k"} if operation == "create" else
                {"topic_id": "topic", "version": 1, "idempotency_key": "k"} if operation == "research" else
                {"topic_id": "topic", "version": 1, "source_ids": ["fed_policy"], "cadence_preset": "core", "idempotency_key": "k"} if operation == "watch" else {},
                state_root=tmp_path / "state", archive_root=tmp_path / "archive")
        assert error.value.code == "MODE_DENIED"
    assert not (tmp_path / "state").exists()


def test_health_never_creates_state(tmp_path):
    result = execute("health", {}, state_root=tmp_path / "state", archive_root=tmp_path / "archive")
    assert result["operation_ok"] and not (tmp_path / "state").exists()


def test_wrong_citation_entity_and_incomplete_fact_rejected():
    doc = {"id": DOCUMENT, "text": TEXT, "completeness": "partial"}
    for field, value in (("entity_id", "us:OTHER"), ("dimension_id", "invented"), ("kind", "fact")):
        analysis = deepcopy(ANALYSIS); analysis["conclusions"][0][field] = value
        with pytest.raises(IntelligenceError):
            validate_analysis(analysis, [doc], {"news_events": "required"}, ["us:NVDA"])
    analysis = deepcopy(ANALYSIS); analysis["conclusions"][0]["supports"][0]["quote"] = "invented quotation"
    with pytest.raises(IntelligenceError):
        validate_analysis(analysis, [doc], {"news_events": "required"}, ["us:NVDA"])


def test_policy_is_distinct_from_model_window_and_queue_bound(research):
    service, topic, job, sent = research
    assert job["policy"]["model_total_tokens"] > job["policy"]["model_attempts"] * 300000
    for changes in ({"rounds": 3}, {"model_concurrency": 2}, {"wall_seconds": 1}):
        with pytest.raises(IntelligenceError):
            trusted_policy_value({**DEFAULT_POLICY, **changes})


def test_response_checkpoint_survives_crash_before_stage_checkpoint(research, monkeypatch):
    service, _, job, sent = research
    from trading_system.intelligence import topic as module
    original = module.atomic_json
    def crash(root, name, value, **kwargs):
        if name.startswith("io-"):
            raise KeyboardInterrupt
        return original(root, name, value, **kwargs)
    monkeypatch.setattr(module, "atomic_json", crash)
    with pytest.raises(KeyboardInterrupt):
        service.run(job["job_id"])
    assert len(sent) == 1
    assert service.store.attempts(job["job_id"])[0]["state"] == "settled"
    monkeypatch.setattr(module, "atomic_json", original)
    result = service.run(job["job_id"])
    assert result["state"] == "completed", result
    assert len(sent) == 2  # Same original generation response, only review is new.


def test_queue_and_tenant_boundaries(research):
    service, topic, job, _ = research
    service.policy = {**service.policy, "max_jobs": 1}
    with pytest.raises(IntelligenceError) as error:
        service.submit({**job["request"], "idempotency_key": "another-request"})
    assert error.value.code == "QUEUE_FULL"
    other = TopicStore(service.root, tenant="other", readonly=True)
    with pytest.raises(IntelligenceError) as error:
        other.job(job["job_id"])
    assert error.value.code == "EVIDENCE_NOT_FOUND"


def test_elapsed_admission_deadline_denies_before_any_model_io(research, monkeypatch):
    service, _, job, sent = research
    from trading_system.intelligence import topic as module
    monkeypatch.setattr(module, "utc_now", lambda: __import__('datetime').datetime.fromisoformat(job["deadline"]) + timedelta(seconds=1))
    result = service.run(job["job_id"])
    assert result["state"] == "failed" and result["error"]["code"] == "WALL_DEADLINE"
    assert not sent and not service.store.attempts(job["job_id"])


def test_actual_window_overflow_partitions_originals_and_reviews_every_source(research, monkeypatch):
    service, _, job, sent = research
    original_acquire = service._acquire
    def acquire(job, questions, index):
        result = original_acquire(job, questions, index)
        archive = service._service().archive
        extra_plan = build_plan({"run_id": "extra-source", "as_of": timestamp(utc_now()), "universe": {},
            "frontier_manifest_hash": "0" * 64, "source_ids": ["web_page"],
            "required_cells": [{"target_id": "topic:" + job["topic"]["topic_id"], "question": "Counterevidence", "required": True}]}, service.registry)
        extra_job = archive.create_job(extra_plan, "extra-source-fixture")
        doc = archive.document(DOCUMENT)
        doc = {**doc, "id": "extra_document", "doc_id": "extra_logical", "version": digest("extra"),
               "text": TEXT + " Different source.", "text_blob": archive.put_blob((TEXT + " Different source.").encode(), "text/plain")}
        archive.put_document(extra_job["job_id"], doc, attempt_id="extra-attempt", targets=("topic:" + job["topic"]["topic_id"],), entities=("us:NVDA",))
        return result
    monkeypatch.setattr(service, "_acquire", acquire)
    original_plan = TopicModelClient.plan_chat
    def window(self, **kwargs):
        if len(kwargs.get("public_payload", {}).get("documents", [])) > 1:
            raise TokenBudgetError("INPUT_CONTEXT_EXCEEDED")
        return original_plan(self, **kwargs)
    monkeypatch.setattr(TopicModelClient, "plan_chat", window)
    def post(url, **kwargs):
        wire = json.loads(kwargs["data"]); sent.append(wire)
        prefix = wire["messages"][1]["content"].split("\nJSON schema:\n")[0]
        payload = json.loads(prefix.rsplit("\n", 1)[1])
        if wire.get("reasoning_effort") == "high":
            parsed = deepcopy(ANALYSIS)
            if "source_analyses" in payload:
                parsed["conclusions"] = [c for part in payload["source_analyses"] for c in part["conclusions"]]
            else:
                doc = payload["documents"][0]
                parsed["conclusions"][0]["id"] = "claim-" + doc["id"]
                parsed["conclusions"][0]["supports"] = [{"document_id": doc["id"], "quote": doc["text"]}]
        else:
            parsed = deepcopy(REVIEW)
        return Response({"id": "batch-response-" + str(len(sent)), "model": "deepseek-flash", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(parsed)}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}})
    monkeypatch.setattr(requests, "post", post)
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    assert len(sent) == 6  # Two source batches + synthesis, then independent equivalents.
    assert {r["document_id"] for c in report["analysis"]["conclusions"] for r in c["supports"]} == {DOCUMENT, "extra_document"}
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]


def test_snapshot_excludes_historical_source_without_current_topic_permission(research):
    service, _, job, sent = research
    service._acquire(job, [job["topic"]["question"]], 0)
    archive = service._service().archive
    plan = build_plan({"run_id": "old-private", "as_of": timestamp(utc_now()), "universe": {},
        "frontier_manifest_hash": "0" * 64, "source_ids": ["web_page"],
        "required_cells": [{"target_id": "topic:" + job["topic"]["topic_id"], "question": "Old private archive", "required": True}]}, service.registry)
    old_job = archive.create_job(plan, "old-private")
    private = {**archive.document(DOCUMENT), "id": "revoked_document", "doc_id": "old-private", "source_id": "revoked_private_source", "version": digest("private")}
    archive.put_document(old_job["job_id"], private, attempt_id="old-private-attempt", targets=("topic:" + job["topic"]["topic_id"],), entities=("us:NVDA",))
    snapshot = service._snapshot(job, 0)
    assert [member["id"] for member in snapshot["members"]] == [DOCUMENT]
    assert snapshot["scope"]["source_ids"] == ["web_page"] and not sent


def test_discovery_only_and_expired_rights_cannot_enter_topic_snapshot(research):
    service, _, job, sent = research
    service.catalog.sources["web_page"]["allowed_uses"] = ["discovery"]
    assert service._eligible_sources() == []
    with pytest.raises(IntelligenceError) as error:
        service._snapshot(job, 0)
    assert error.value.code == "SOURCE_NOT_CONNECTED"
    service.catalog.sources["web_page"]["allowed_uses"] = ["topic_analysis"]
    service.catalog.sources["web_page"]["entitlement"] = {"kind": "licensed", "reference": "fixture", "valid_until": "2020-01-01T00:00:00+00:00"}
    assert service._eligible_sources() == [] and not sent


def test_requested_entity_or_material_label_does_not_establish_coverage(research):
    service, topic, _, _ = research
    wrong_subject = {"id": DOCUMENT, "source_id": "web_page", "text": "A different business announced expansion.", "completeness": "complete", "requested_entity_ids": ["us:NVDA"]}
    analysis = deepcopy(ANALYSIS)
    analysis["conclusions"][0]["supports"][0]["quote"] = wrong_subject["text"]
    coverage = topic_coverage(topic, analysis, [wrong_subject], service.catalog)
    cell = next(x for x in coverage if x["dimension_id"] == "news_events")
    assert cell["status"] == "unknown" and cell["reason"] == "MATERIAL_OR_SUBJECT_UNVERIFIED"
    analysis["conclusions"][0]["dimension_id"] = "formal_research"
    unrelated_material = {**wrong_subject, "text": TEXT}
    analysis["conclusions"][0]["supports"][0]["quote"] = TEXT
    coverage = topic_coverage(topic, analysis, [unrelated_material], service.catalog)
    assert next(x for x in coverage if x["dimension_id"] == "formal_research")["status"] == "unknown"


def test_source_upgrade_cannot_reinterpret_original_job_or_checkpoints(research, monkeypatch):
    service, _, job, sent = research
    from trading_system.intelligence import topic as module
    original = module.implementation_fingerprint()
    monkeypatch.setattr(module, "implementation_fingerprint", lambda: {**original, "sha256": "0" * 64})
    result = service.run(job["job_id"])
    assert result["state"] == "failed" and result["error"]["code"] == "SOURCE_CHANGED"
    assert not sent


def test_legacy_job_missing_source_binding_is_explicitly_rejected(research):
    service, _, job, _ = research
    with service.store.connection() as connection:
        changed = deepcopy(job); changed.pop("source_fingerprint")
        connection.execute("UPDATE research_jobs SET record=? WHERE tenant=? AND job_id=?", (json.dumps(changed), service.tenant, job["job_id"]))
    with pytest.raises(IntelligenceError) as error:
        service.store.job(job["job_id"])
    assert error.value.code == "LEGACY_JOB_REQUIRES_MATCHING_RELEASE"


@pytest.fixture
def loopback_topic_model(monkeypatch):
    state = {"requests": [], "responses": []}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            wire = json.loads(self.rfile.read(length))
            state["requests"].append(wire)
            parsed = deepcopy(ANALYSIS if wire.get("reasoning_effort") == "high" else REVIEW)
            response = {"id": "http-topic-response-" + str(len(state["requests"])),
                "model": "deepseek-flash", "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(parsed),
                    "reasoning_content": "private thought test-only-topic-credential"}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}}
            body = json.dumps(response, separators=(",", ":")).encode()
            state["responses"].append(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/chat/completions"
    def route(url, **kwargs):
        assert url == "https://api.deepseek.com/chat/completions"
        return requests.api.post(endpoint, **kwargs)
    monkeypatch.setattr(requests, "post", route)
    yield endpoint, state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _hardkill_after_response_file(service, job, endpoint):
    """A separate real process dies after fsync/publication, before SQLite settle."""
    script = r'''
import os, signal, sys
from pathlib import Path
import requests
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_intelligence_topic import FixtureResearch
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.service import IntelligenceService
from trading_system.intelligence import topic as topic_module
endpoint = sys.argv[4]
def route(url, **kwargs):
    assert url == "https://api.deepseek.com/chat/completions"
    return requests.api.post(endpoint, **kwargs)
requests.post = route
original = topic_module.atomic_json
def kill_after_publication(root, name, value, **kwargs):
    result = original(root, name, value, **kwargs)
    if name.startswith("response-"):
        os.kill(os.getpid(), signal.SIGKILL)
    return result
topic_module.atomic_json = kill_after_publication
registry = SourceRegistry([SourceSpec("web_page", "web_fetch", "", capabilities=("fetch",),
    options={"material_types": ["news_events"]})])
service = FixtureResearch(sys.argv[1], sys.argv[2], mode="research", registry=registry,
    service_factory=lambda root, r: IntelligenceService(root, r, {}))
service.run(sys.argv[3])
'''
    observed = subprocess.run([sys.executable, "-B", "-c", script, str(service.root),
        str(service.archive_root), job["job_id"], endpoint], cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    assert observed.returncode == -signal.SIGKILL, observed.stderr.decode(errors="replace")
    attempts = service.store.attempts(job["job_id"])
    assert len(attempts) == 1 and attempts[0]["state"] == "dispatched"
    response_file = service.store.directory(job["job_id"]) / ("response-" + attempts[0]["attempt_id"] + ".json")
    assert response_file.is_file()
    return attempts[0], response_file


def test_real_http_hardkill_between_response_and_settle_recovers_original_attempt(research, loopback_topic_model):
    service, _, job, _ = research
    endpoint, observed = loopback_topic_model
    original, response_file = _hardkill_after_response_file(service, job, endpoint)
    checkpoint = json.loads(response_file.read_text())
    assert checkpoint["receipt"]["response_sha256"] == hashlib.sha256(observed["responses"][0]).hexdigest()
    assert checkpoint["request_sha256"] == original["request_sha"]
    assert checkpoint["receipt"]["saved_response_sha256"] == digest(checkpoint["response"])
    assert "private thought" not in response_file.read_text()
    assert "test-only-topic-credential" not in response_file.read_text()
    result = service.run(job["job_id"])
    assert result["state"] == "completed", result
    attempts = service.store.attempts(job["job_id"])
    assert len(attempts) == len(observed["requests"]) == 2
    assert attempts[0]["attempt_id"] == original["attempt_id"] and attempts[0]["state"] == "settled"
    assert attempts[0]["receipt"]["usage"]["total_tokens"] == 50
    assert [request["reasoning_effort"] for request in observed["requests"]] == ["high", "max"]
    assert service.report(job["job_id"])["receipt_sha256"] == result["receipt_sha256"]
    assert service.run(job["job_id"])["receipt_sha256"] == result["receipt_sha256"]
    assert len(observed["requests"]) == 2


@pytest.mark.parametrize("damage", ["missing", "response", "body_hash", "request", "tenant", "usage", "checkpoint_hash"])
def test_real_http_hardkill_missing_or_tampered_response_keeps_hold_and_blocks(research, loopback_topic_model, damage):
    service, _, job, _ = research
    endpoint, observed = loopback_topic_model
    attempt, response_file = _hardkill_after_response_file(service, job, endpoint)
    value = json.loads(response_file.read_text())
    if damage == "missing":
        response_file.unlink()
    else:
        if damage == "response":
            value["response"]["id"] = "invented-response"
        elif damage == "body_hash":
            value["receipt"]["response_sha256"] = "0" * 64
        elif damage == "request":
            value["request_sha256"] = value["receipt"]["request_sha256"] = "0" * 64
            # Even a coherent checkpoint checksum cannot change the original
            # dispatched database request identity.
            value["checkpoint_sha256"] = digest({key: child for key, child in value.items() if key != "checkpoint_sha256"})
        elif damage == "tenant":
            value["tenant"] = "other-tenant"
        elif damage == "usage":
            value["response"]["usage"]["total_tokens"] = 9999
        else:
            value["checkpoint_sha256"] = "0" * 64
        response_file.write_text(json.dumps(value))
    result = service.run(job["job_id"])
    assert result["state"] == "blocked" and result["error"]["code"] == "USAGE_RECONCILIATION_REQUIRED"
    unchanged = service.store.attempts(job["job_id"])
    assert len(unchanged) == 1 and unchanged[0]["attempt_id"] == attempt["attempt_id"]
    assert unchanged[0]["state"] == "dispatched" and unchanged[0]["hold"] == attempt["hold"]
    assert unchanged[0]["receipt"] is None and len(observed["requests"]) == 1


def test_local_original_response_reconciliation_requires_no_provider_key_or_network(research, loopback_topic_model, monkeypatch):
    service, _, job, _ = research
    endpoint, observed = loopback_topic_model
    attempt, _ = _hardkill_after_response_file(service, job, endpoint)
    monkeypatch.delenv("LLM_API_KEY")
    assert TopicModelClient.reconcile_pending(TopicExecution(service.store, job["job_id"])) == [attempt["attempt_id"]]
    assert TopicModelClient.reconcile_pending(TopicExecution(service.store, job["job_id"])) == []
    assert len(observed["requests"]) == 1
    assert service.store.attempts(job["job_id"])[0]["state"] == "settled"


def test_expired_job_still_settles_original_received_usage_without_more_http(research, loopback_topic_model, monkeypatch):
    service, _, job, _ = research
    endpoint, observed = loopback_topic_model
    attempt, _ = _hardkill_after_response_file(service, job, endpoint)
    from trading_system.intelligence import topic as module
    from trading_system.intelligence.contracts import aware
    monkeypatch.setattr(module, "utc_now", lambda: aware(job["deadline"]) + timedelta(seconds=1))
    result = service.run(job["job_id"])
    assert result["state"] == "failed" and result["error"]["code"] == "WALL_DEADLINE"
    settled = service.store.attempts(job["job_id"])
    assert settled[0]["attempt_id"] == attempt["attempt_id"] and settled[0]["state"] == "settled"
    assert settled[0]["receipt"]["usage"]["total_tokens"] == 50
    assert len(observed["requests"]) == 1


def test_soft_crash_after_durable_response_also_reconciles_unknown_without_repeat(research, loopback_topic_model, monkeypatch):
    service, _, job, _ = research
    _, observed = loopback_topic_model
    from trading_system.intelligence import topic as module
    original = module.atomic_json
    def interrupt_after_write(root, name, value, **kwargs):
        result = original(root, name, value, **kwargs)
        if name.startswith("response-"):
            raise KeyboardInterrupt
        return result
    monkeypatch.setattr(module, "atomic_json", interrupt_after_write)
    with pytest.raises(KeyboardInterrupt):
        service.run(job["job_id"])
    assert service.store.attempts(job["job_id"])[0]["state"] == "unknown"
    monkeypatch.setattr(module, "atomic_json", original)
    assert service.run(job["job_id"])["state"] == "completed"
    assert len(observed["requests"]) == 2


def test_omitted_reasoning_metadata_cannot_make_invalid_response_valid_on_resume(research, loopback_topic_model):
    service, _, job, _ = research
    _, observed = loopback_topic_model
    execution = TopicExecution(service.store, job["job_id"])
    execution._active.spec.name = "clean.llm_semantic"
    arguments = {"system": "Read supplied evidence", "user": "Return the fixed result",
        "schema_hint": {}, "schema": {"type": "object", "properties": {"accepted": {"type": "boolean"}},
            "required": ["accepted"], "additionalProperties": True}}
    for _ in range(2):
        with pytest.raises(Exception) as error:
            TopicModelClient(execution).complete_json(**arguments)
        assert getattr(error.value, "diagnostic", {}).get("code") == "NON_THINKING_REASONING_CONTENT"
    assert len(observed["requests"]) == 1
    assert service.store.attempts(job["job_id"])[0]["state"] == "settled"


def test_constructor_catalog_preparation_time_is_in_the_original_wall_clock(tmp_path, monkeypatch):
    from trading_system.intelligence import catalog
    original = catalog.SourceCatalog.__init__
    entered = []
    def slow(self, *args, **kwargs):
        entered.append(utc_now())
        time.sleep(0.08)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(catalog.SourceCatalog, "__init__", slow)
    service = TopicResearch(tmp_path / "state", tmp_path / "archive", mode="research")
    topic = service.create({"question": "Preparation clock", "idempotency_key": "prepare-topic"})
    job = service.submit({"topic_id": topic["topic_id"], "version": 1, "idempotency_key": "prepare-job"})
    from trading_system.intelligence.contracts import aware
    assert aware(job["created_at"]) <= entered[0]
    assert (utc_now() - aware(job["created_at"])).total_seconds() >= 0.08
    assert aware(job["deadline"]) - aware(job["created_at"]) == timedelta(seconds=2400)


def test_actual_database_lock_wait_is_charged_to_admitted_deadline(research):
    service, topic, _, _ = research
    admitted = utc_now()
    request = {"topic_id": topic["topic_id"], "version": 1, "idempotency_key": "locked-job"}
    lock = sqlite3.connect(service.store.database)
    lock.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(service.store.create, request, topic, service.policy, catalog=service.catalog, admitted_at=admitted)
            time.sleep(0.15)
            lock.commit()
            job = pending.result(timeout=5)
    finally:
        lock.close()
    from trading_system.intelligence.contracts import aware
    assert job["created_at"] == timestamp(admitted)
    assert job["deadline"] == timestamp(admitted + timedelta(seconds=2400))
    assert (utc_now() - aware(job["created_at"])).total_seconds() >= 0.15


def test_preparation_that_exhausts_deadline_cannot_admit_a_new_job(research):
    service, topic, _, _ = research
    admitted = utc_now() - timedelta(seconds=2399.9)
    request = {"topic_id": topic["topic_id"], "version": 1, "idempotency_key": "expired-preparation"}
    lock = sqlite3.connect(service.store.database)
    lock.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(service.store.create, request, topic, service.policy, catalog=service.catalog, admitted_at=admitted)
            time.sleep(0.2)
            lock.commit()
            with pytest.raises(IntelligenceError) as error:
                pending.result(timeout=5)
    finally:
        lock.close()
    assert error.value.code == "WALL_DEADLINE"
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM research_jobs WHERE replay_key=?", (request["idempotency_key"],)).fetchone()[0] == 0


@pytest.mark.parametrize("slow_part", ["fingerprint", "durable_publication"])
def test_admission_tail_preparation_rechecks_wall_before_database_commit(tmp_path, monkeypatch, slow_part):
    from trading_system.intelligence import topic as module
    store = TopicStore(tmp_path / "state")
    policy = trusted_policy_value({**DEFAULT_POLICY, "wall_seconds": 3,
        "review_reserve_seconds": 1, "closeout_seconds": 1})
    request = {"idempotency_key": "slow-admission-tail"}
    catalog = SourceCatalog()
    topic = {"schema": "TEST_FIXTURE", "catalog_sha256": catalog.manifest()["sha256"]}
    if slow_part == "fingerprint":
        original = module.implementation_fingerprint
        def slow_fingerprint():
            time.sleep(0.2)
            return original()
        monkeypatch.setattr(module, "implementation_fingerprint", slow_fingerprint)
    else:
        original = module.atomic_json
        def slow_publication(root, name, value, **kwargs):
            result = original(root, name, value, **kwargs)
            if name == "admission.json":
                time.sleep(0.2)
            return result
        monkeypatch.setattr(module, "atomic_json", slow_publication)
    with pytest.raises(IntelligenceError) as error:
        store.create(request, topic, policy, catalog=catalog, admitted_at=utc_now() - timedelta(seconds=2.9))
    assert error.value.code == "WALL_DEADLINE"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM research_jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM topic_events").fetchone()[0] == 0
    if slow_part == "fingerprint":
        assert not list((store.root / "topic-runs").iterdir())
    else:
        # The immutable prepared artifact may remain, but no admission, replay
        # key or accepted event is committed and it cannot enter the queue.
        assert len(list((store.root / "topic-runs").glob("*/admission.json"))) == 1


def test_cli_trusted_admission_anchor_and_idempotent_deadline_are_never_reset(research):
    service, _, job, _ = research
    later = utc_now()
    restored = FixtureResearch(service.root, service.archive_root, mode="research", registry=service.registry,
        admitted_at=later, service_factory=service.service_factory)
    same = restored.submit(job["request"])
    assert same["job_id"] == job["job_id"]
    assert same["created_at"] == job["created_at"] and same["deadline"] == job["deadline"]
    new = restored.submit({**job["request"], "idempotency_key": "new-trusted-admission"})
    assert new["created_at"] == timestamp(later)
    assert new["deadline"] == timestamp(later + timedelta(seconds=2400))
    with pytest.raises(IntelligenceError):
        validate_request("research", {**job["request"], "admitted_at": timestamp(later)})


def _fingerprint_tree(tmp_path, monkeypatch):
    from trading_system.intelligence import topic as module
    fixed = ["scripts/tiger-topic.mjs", "scripts/tiger-topic-mcp.mjs", "bundles/trading/capabilities/workflow-client.mjs",
        "governance/bundles/trading/model-policy.yml", "requirements.txt", "trading_system/llm/assets/deepseek-v4-tokenizer.json",
        "trading_system/intelligence/topic.py", "trading_system/agent_evidence.py"]
    root = tmp_path / "release"
    for relative in fixed:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture source " + relative)
    monkeypatch.setattr(module, "__file__", str(root / "trading_system/intelligence/topic.py"))
    return root, module


def test_fingerprint_covers_lower_dependencies_and_fixed_assets_and_ignores_cache(tmp_path, monkeypatch):
    root, module = _fingerprint_tree(tmp_path, monkeypatch)
    first = module.implementation_fingerprint()
    assert "trading_system/agent_evidence.py" in first["files"]
    assert "bundles/trading/capabilities/workflow-client.mjs" in first["files"]
    assert "requirements.txt" in first["files"]
    (root / "trading_system/agent_evidence.py").write_text("changed lower-level source")
    second = module.implementation_fingerprint()
    assert first["sha256"] != second["sha256"]
    cache = root / "trading_system/__pycache__/ignored.py"
    cache.parent.mkdir()
    cache.write_text("ignored bytecode-cache fixture")
    assert module.implementation_fingerprint() == second


@pytest.mark.parametrize("kind", ["python_file", "python_directory", "fixed_entry_asset"])
def test_fingerprint_rejects_symlinked_source_and_fixed_assets(tmp_path, monkeypatch, kind):
    root, module = _fingerprint_tree(tmp_path, monkeypatch)
    if kind == "python_file":
        (root / "trading_system/linked.py").symlink_to(tmp_path / "outside.py")
    elif kind == "python_directory":
        (root / "trading_system/linked-package").symlink_to(tmp_path, target_is_directory=True)
    else:
        path = root / "requirements.txt"
        path.unlink()
        path.symlink_to(tmp_path / "outside.txt")
    with pytest.raises(IntelligenceError) as error:
        module.implementation_fingerprint()
    assert error.value.code == "SOURCE_UNAVAILABLE"
