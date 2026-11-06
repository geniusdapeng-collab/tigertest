"""Exact AM03/AM28 local HTTP evidence; never invokes a real model/provider.

The current repository implementation and shared safety helpers are used.
The HTTP fixture emits each invalid response on the wire;
no client-return monkeypatch converts a valid response into the fault.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests

SOURCE = Path(__file__).resolve().parents[1]
import test_intelligence_topic_safety_integration as safety
from trading_system.intelligence.contracts import IntelligenceError, timestamp, utc_now
from trading_system.intelligence.monitoring import MonitoringWorker
from trading_system.intelligence.topic import implementation_fingerprint


@pytest.fixture
def exact_local_topic(tmp_path, monkeypatch):
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
            "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "TEST_FIXTURE_MODEL_AUTH_CANARY",
            "NO_PROXY": "127.0.0.1,localhost"}.items():
        monkeypatch.setenv(key, value)
    for key in ("TIGER_INTEL_CATALOG_FILE", "TIGER_INTEL_REGISTRY_FILE"):
        monkeypatch.delenv(key, raising=False)
    state = {"pages": {"/robots.txt": (200, "text/plain", "User-agent: *\nAllow: /\n")}, "source_calls": [], "model_requests": [], "network_calls": [],
        "handler_errors": [], "raw_model_responses": [], "reply": safety._safe_reply,
        "response_fault": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["source_calls"].append({"path": self.path,
                "authorization_present": bool(self.headers.get("Authorization"))})
            page = state["pages"].get(self.path)
            code, media, body = page if isinstance(page, tuple) else (200 if page else 404,
                "text/html; charset=utf-8", page or "TEST_FIXTURE unknown page")
            self._send(code, media, body.encode())

        def do_POST(self):
            wire = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["model_requests"].append(wire)
            try:
                assert self.path == "/chat/completions"
                assert self.headers.get("Authorization") == "Bearer TEST_FIXTURE_MODEL_AUTH_CANARY"
                parsed = state["reply"](wire, safety._payload(wire))
                response = {"id": "TEST_FIXTURE-exact-" + str(len(state["model_requests"])),
                    "model": "deepseek-flash", "object": "chat.completion",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {
                        "role": "assistant", "content": json.dumps(parsed)}}],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}}
                fault = state["response_fault"]
                if fault == "empty_content":
                    response["choices"][0]["message"]["content"] = ""
                elif fault == "missing_choices":
                    response.pop("choices")
                elif fault == "empty_choices":
                    response["choices"] = []
                elif fault == "null_choices":
                    response["choices"] = None
                elif fault == "mapping_choices_with_private_reasoning":
                    response["choices"] = {"message": {
                        "reasoning_content": "TEST_FIXTURE_PRIVATE_REASONING_CANARY",
                        "content": "TEST_FIXTURE invalid choices mapping"}}
                elif fault == "null_first_choice":
                    response["choices"] = [None]
                elif fault == "missing_response_id":
                    response.pop("id")
                elif fault == "missing_usage":
                    response.pop("usage")
                state["raw_model_responses"].append(response)
                self._send(200, "application/json", json.dumps(response, separators=(",", ":")).encode())
            except Exception as error:
                state["handler_errors"].append(type(error).__name__)
                self._send(500, "application/json", b'{"error":"TEST_FIXTURE handler failure"}')

        def _send(self, code, media, body):
            self.send_response(code)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_port)
    original_request = requests.sessions.Session.request

    def local_only(session, method, url, **kwargs):
        assert urlsplit(url).netloc == urlsplit(base).netloc, "TEST_FIXTURE nonlocal HTTP refused"
        state["network_calls"].append({"method": method, "path": urlsplit(url).path})
        return original_request(session, method, url, **kwargs)

    def model_route(url, **kwargs):
        assert url == "https://api.deepseek.com/chat/completions"
        return requests.api.post(base + "/chat/completions", **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", local_only)
    monkeypatch.setattr(requests, "post", model_route)
    harness = SimpleNamespace(root=tmp_path, base=base, state=state)
    try:
        yield harness
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive() and not state["handler_errors"]
        (tmp_path / "wire-observation.json").write_text(json.dumps({key: value
            for key, value in state.items() if key not in {"reply", "pages"}}, indent=2) + "\n")


def _record(harness, case, actual):
    (harness.root / "case-evidence.json").write_text(json.dumps({
        "schema": "tiger.exact-acceptance-case/v1", "case": case,
        "kind": "TEST_FIXTURE_actual_loopback_HTTP_and_persistence",
        "source_root": str(SOURCE), "source_fingerprint": implementation_fingerprint(),
        "actual": actual, "paid_or_external_calls": 0,
    }, ensure_ascii=False, indent=2) + "\n")


def test_am03_same_url_revision_warns_without_rewriting_sealed_report(exact_local_topic):
    h = exact_local_topic
    service, topic, job = safety._service(h, [("public_original", safety.SAFE_QUOTE,
        ["news_events"], ["topic_analysis"])])
    watch = service.monitor.create_watch(topic["topic_id"], ["public_original"],
        version=topic["version"], cadence_preset="core", idempotency_key="TEST_FIXTURE-am03-watch")
    worker = MonitoringWorker(service.monitor, lambda: service._service(), owner="TEST_FIXTURE-am03-monitor")
    assert worker.tick()["state"] == "completed"
    sealed = service.run(job["job_id"])
    assert sealed["state"] == "completed", sealed
    path = service.store.directory(job["job_id"]) / "report.json"
    before = path.read_bytes()
    old_documents = service._service().archive.snapshot_documents(sealed["snapshot_ids"][-1])
    old = next(d for d in old_documents if d["source_id"] == "public_original")
    original_page = h.state["pages"]["/public_original"]
    revised_text = "TEST_FIXTURE NVDA source corrected its measured demand and retained documented uncertainty."
    h.state["pages"]["/public_original"] = original_page.replace(safety.SAFE_QUOTE, revised_text)
    with service.monitor._connection(write=True) as connection:
        connection.execute("UPDATE monitor_units SET next_due=0 WHERE tenant=? AND watch_id=?",
            (service.tenant, watch["watch_id"]))
    changed = worker.tick()
    view = safety._readonly(service).report_view(job["job_id"])
    new = next(d for d in service._service().archive.documents(changed["acquisition_job_id"])
        if d["source_id"] == "public_original")
    _record(h, "AM03", {"preconditions": "Completed independently verified report and same source URL",
        "steps": "Change actual HTTP source body; due tick; readonly report_view",
        "expected": "Old report bytes/cutoff/hash unchanged; current source needs review",
        "monitor_result": changed, "view": view["current_source_status"],
        "old_version": {k: old.get(k) for k in ("id", "doc_id", "version", "supersedes")},
        "new_version": {k: new.get(k) for k in ("id", "doc_id", "version", "supersedes")},
        "sealed_file_sha256_before": hashlib.sha256(before).hexdigest(),
        "sealed_file_sha256_after": hashlib.sha256(path.read_bytes()).hexdigest(),
        "model_posts": len(h.state["model_requests"])})
    assert changed["state"] == "completed" and changed["changed"] is True
    assert new["doc_id"] == old["doc_id"] and new["id"] != old["id"]
    assert new["version"] != old["version"] and new["canonical_url"] == old["canonical_url"]
    assert new["supersedes"] == old["id"]
    assert new["supersession"]["basis"] == "observed_source_version"
    assert new["supersession"]["period_basis"] == "unknown"
    assert revised_text in new["text"] and service._service().archive.document(old["id"])["text"] == old["text"]
    assert view["current_source_status"]["state"] == "needs_review"
    warning = next(w for w in view["current_source_status"]["warnings"]
        if w["reason"] == "MONITORED_SOURCE_CHANGED_AFTER_CUTOFF")
    assert warning["source_id"] == "public_original" and warning["claim_ids"] == ["claim-safe"]
    assert view["report"] == sealed and view["sealed_receipt_sha256"] == sealed["receipt_sha256"]
    assert path.read_bytes() == before and len(h.state["model_requests"]) == 2


@pytest.mark.parametrize("fault", ["empty_content", "missing_choices", "empty_choices",
    "null_choices", "mapping_choices_with_private_reasoning", "null_first_choice"])
def test_am28_actual_incomplete_model_response_fails_with_original_attempt_accounting(exact_local_topic, fault):
    h = exact_local_topic
    service, _, job = safety._service(h, [("public_original", safety.SAFE_QUOTE,
        ["news_events"], ["topic_analysis"])])
    h.state["response_fault"] = fault
    result = service.run(job["job_id"])
    attempts = service.store.attempts(job["job_id"])
    count = len(h.state["model_requests"])
    replay = service.run(job["job_id"])
    _record(h, "AM28-" + fault, {"preconditions": "Actual HTTP200 model response with valid usage50 and fault " + fault,
        "steps": "Run topic; inspect persisted original attempt and response; replay terminal job",
        "expected": "Semantic failure without report/fallback; one attempt; valid identity and usage50 stay settled; replay preserves the same semantic error",
        "result": {k: result.get(k) for k in ("state", "phase", "error")},
        "replay": {k: replay.get(k) for k in ("state", "phase", "error")},
        "attempts": attempts, "raw_responses": h.state["raw_model_responses"],
        "model_posts_before_replay": count, "model_posts_after_replay": len(h.state["model_requests"]),
        "checkpoint_files": sorted(p.name for p in service.store.directory(job["job_id"]).glob("response-*.json"))})
    assert result["state"] in {"failed", "blocked"}, result
    assert not (service.store.directory(job["job_id"]) / "report.json").exists()
    assert len(attempts) == 1 and count == len(h.state["model_requests"]) == 1
    # The actual HTTP200 body has a validated response identity and usage50.
    # Incomplete semantic choices must not erase that known billed usage or
    # turn its own immutable checkpoint into a HASH_MISMATCH on replay.
    assert attempts[0]["state"] == "settled"
    assert attempts[0]["receipt"]["usage"]["total_tokens"] == 50
    assert result["error"]["code"] == replay["error"]["code"] == "MODEL_REQUIRED_FAILED"
    assert replay["state"] == result["state"] == "failed"
    checkpoint = service.store.directory(job["job_id"]) / attempts[0]["receipt"]["response_ref"]
    saved = checkpoint.read_text()
    assert "reasoning_content" not in saved
    assert "TEST_FIXTURE_PRIVATE_REASONING_CANARY" not in saved
    with pytest.raises(IntelligenceError) as error:
        service.report(job["job_id"])
    assert error.value.code == "RESEARCH_INCOMPLETE"


@pytest.mark.parametrize("fault", ["missing_response_id", "missing_usage"])
def test_am28_missing_billing_identity_or_usage_keeps_original_unknown_hold(exact_local_topic, fault):
    h = exact_local_topic
    service, _, job = safety._service(h, [("public_original", safety.SAFE_QUOTE,
        ["news_events"], ["topic_analysis"])])
    h.state["response_fault"] = fault
    first = service.run(job["job_id"])
    attempts = service.store.attempts(job["job_id"])
    replay = service.run(job["job_id"])
    _record(h, "AM28-" + fault, {"preconditions": "Actual HTTP200 body has missing billing field " + fault,
        "steps": "Run topic; inspect accounting; replay original job",
        "expected": "Unknown original attempt retains hold; replay blocks without any second HTTP; no report",
        "first": {k: first.get(k) for k in ("state", "phase", "error")},
        "replay": {k: replay.get(k) for k in ("state", "phase", "error")},
        "attempts": attempts, "raw_responses": h.state["raw_model_responses"],
        "model_posts": len(h.state["model_requests"])})
    assert first["state"] == "failed"
    assert len(attempts) == 1 and attempts[0]["state"] == "unknown"
    assert attempts[0]["receipt"] is None and attempts[0]["hold"] > 0
    assert replay["state"] == "blocked" and replay["error"]["code"] == "USAGE_RECONCILIATION_REQUIRED"
    assert len(h.state["model_requests"]) == 1
    assert not (service.store.directory(job["job_id"]) / "report.json").exists()
