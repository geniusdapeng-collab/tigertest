"""Real localhost HTTP fixtures for acquisition, storage and strict replay contracts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from trading_system.intelligence import (IntelligenceError, IntelligenceService, SourceRegistry,
    SourceSpec, build_plan, digest, project_to_rawdocuments, reconstruct_document, source_time, verify_bundle)
from trading_system.intelligence.adapters import (BraveAdapter, DeepSeekAdapter, DeepSeekBridge,
    ExaAdapter, SearchRequest, TavilyAdapter)
from trading_system.intelligence.archive import Archive
from trading_system.intelligence.contracts import SourceError
from trading_system.intelligence.planner import compile_requests
from trading_system.intelligence.transport import HttpTransport
from trading_system.redline import LLMUnavailable


NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


@pytest.fixture
def web():
    routes, calls = {}, []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()
        def do_POST(self):
            self.respond()
        def respond(self):
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            path = self.path.split("?", 1)[0]
            calls.append({"method": self.command, "path": self.path, "body": body,
                          "headers": dict(self.headers)})
            result = routes.get(path, (404, "text/plain", b"missing"))
            if callable(result):
                result = result(calls[-1])
            status, content_type, value, *extra = result
            if isinstance(value, (dict, list)):
                value = json.dumps(value).encode()
            elif isinstance(value, str):
                value = value.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            custom_headers = extra[0] if extra else {}
            for key, val in custom_headers.items():
                if val is not None:
                    self.send_header(key, val)
            if "Content-Length" not in custom_headers:
                self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            try:
                self.wfile.write(value)
            except BrokenPipeError:
                return
        def log_message(self, *_):
            return
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", routes, calls
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def registry(url, *, search=True):
    sources = [SourceSpec("web_page", "web_fetch", "", minimum_interval_seconds=0)]
    if search:
        sources.append(SourceSpec("deepseek", "search", url + "/ds/search", minimum_interval_seconds=0, max_retries=0))
    return SourceRegistry(sources)


def plan(reg, **changes):
    data = {"run_id": "fixture-run", "as_of": "2026-11-05T13:00:00+00:00", "market": "us",
        "universe": {"AAPL": "listed:US:AAPL"}, "frontier": ["AAPL"],
        "frontier_manifest_hash": "a" * 64, "market_snapshot_refs": [],
        "required_cells": [{"cell_id": "cell-a", "target_id": "security:listed:US:AAPL",
            "entity_id": "listed:US:AAPL", "question": "AAPL earnings", "query": "AAPL earnings",
            "required": True, "material_type": "issuer_financial", "window_days": 450}],
        "limits": {"requests": 30, "fetches": 10, "bytes": 1000000, "rounds": 3,
                   "concurrency": 4, "deadline_seconds": 100}}
    data.update(changes)
    return build_plan(data, reg)


def populate(web):
    url, routes, _ = web
    routes["/ds/search"] = (200, "application/json", {"results": [{"title": "Apple quarterly results",
        "url": url + "/article", "summary": "Discovery summary", "content": "Provider body",
        "date": "2026-11-04T15:00:00Z"}]})
    routes["/article"] = (200, "text/html", '<html><head><title>Apple results</title><meta property="article:published_time" content="2026-11-04T15:00:00Z"></head><body><nav>irrelevant navigation</nav><article><h1>Apple results</h1><p>Apple Inc. (AAPL) reports revenue of $10 million for Q3 2026.</p></article><footer>related fake issuer</footer></body></html>')
    return url


def service(tmp_path, web):
    url = populate(web)
    reg = registry(url)
    transport = HttpTransport(allow_local=True)
    adapters = {"deepseek": DeepSeekAdapter(transport, "fixture-secret", endpoint=url + "/ds/search")}
    from trading_system.intelligence.adapters import WebFetch
    adapters["web_page"] = WebFetch(transport)
    observed = []
    def observe(method, inputs, invoke):
        value = invoke()
        observed.append((method, inputs, value))
        return value
    return IntelligenceService(tmp_path, reg, adapters, clock=lambda: NOW, observe_io=observe), reg, observed


def test_real_http_original_fetch_freeze_and_offline_replay(tmp_path, web):
    svc, reg, observed = service(tmp_path, web)
    p = plan(reg)
    job = svc.create_job(p, "replay-a")
    acquired = svc.collect(job["job_id"])
    assert acquired["usage"]["requests"] == 2 and acquired["usage"]["fetches"] == 1
    assert len(acquired["documents"]) == 2
    original = next(x for x in acquired["documents"] if x["text_kind"] == "html_main")
    assert original["completeness"] == "complete"
    assert "irrelevant navigation" not in original["text"] and "related fake issuer" not in original["text"]
    assert original["authority"]["scope"] == "discovery"
    assert original["times"]["published"]["precision"] == "instant"
    raw = project_to_rawdocuments(acquired["documents"])
    assert len(raw) == 2 and len({d.doc_id for d in raw}) == 2
    assert all(d.doc_id.startswith("ds_") and len(d.doc_id) == 27 for d in raw)
    svc.record_assessment(job["job_id"], [{"id": "claim1", "document_ids": [original["id"]],
        "target_ids": ["security:listed:US:AAPL"]}], [{"cell_id": "cell-a", "status": "assessed", "evidence_refs": [original["id"]]}])
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"],
        expected_revision=svc.status(job["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    assert frozen["state"] == "completed" and frozen["missing_required"] == []
    count = len(web[2])
    verified = verify_bundle(tmp_path, frozen["bundle_id"], expected_plan_hash=p["plan_hash"],
                             expected_registry_sha256=reg.public_manifest()["sha256"])
    assert verified["manifest_sha256"] == frozen["manifest_sha256"] and len(web[2]) == count
    assert {x[0] for x in observed} == {"intelligence.source.request"}
    all_files = b"".join(f.read_bytes() for f in tmp_path.rglob("*") if f.is_file())
    assert b"fixture-secret" not in all_files


def test_unknown_date_and_empty_search_are_not_no_event(tmp_path, web):
    svc, reg, _ = service(tmp_path, web)
    web[1]["/ds/search"] = (200, "application/json", {"results": []})
    p = plan(reg)
    job = svc.create_job(p, "empty")
    value = svc.collect(job["job_id"])
    assert value["documents"] == []
    assert value["gaps"][0]["reason"] == "NO_RESULTS_NOT_NO_EVENT"
    assert source_time("2026-11-05")["precision"] == "date"
    assert source_time("2026-11-05T12:00:00")["precision"] == "unknown"
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=value["revision"],
                        expected_frontier_hash=p["frontier_manifest_hash"])
    assert frozen["state"] == "partial" and frozen["missing_required"] == ["cell-a"]


@pytest.mark.parametrize("status,code", [(401, "SOURCE_AUTH_REQUIRED"), (403, "FETCH_DENIED"),
                                        (429, "RATE_LIMITED"), (503, "SOURCE_UNAVAILABLE")])
def test_http_failures_have_receipts_and_missing_coverage(tmp_path, web, status, code):
    svc, reg, _ = service(tmp_path, web)
    web[1]["/ds/search"] = (status, "application/json", {"error": "unavailable"}, {"Retry-After": "120"})
    job = svc.create_job(plan(reg), "http-" + str(status))
    result = svc.collect(job["job_id"])
    assert result["state"] == "partial" and result["gaps"][0]["reason"] == code
    assert result["usage"]["requests"] == 1 and len(result["attempts"]) == 1
    assert result["attempts"][0]["result"]["usage"]["requires_reconciliation"]


def test_missing_key_is_not_sent_and_never_fake_zero_timeout_cost(tmp_path, web):
    url = populate(web); reg = registry(url)
    svc = IntelligenceService(tmp_path, reg, {"deepseek": DeepSeekAdapter(HttpTransport(allow_local=True), None)}, clock=lambda: NOW)
    job = svc.create_job(plan(reg), "missing-key")
    result = svc.collect(job["job_id"])
    assert not web[2] and result["usage"]["requests"] == 0
    assert result["attempts"][0]["result"]["usage"]["state"] == "not_sent"


def test_duplicate_job_and_collect_are_idempotent(tmp_path, web):
    svc, reg, _ = service(tmp_path, web); p = plan(reg)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda _: svc.create_job(p, "same-key"), range(4)))
    assert len({j["job_id"] for j in jobs}) == 1
    job_id = jobs[0]["job_id"]
    svc.collect(job_id); count = len(web[2]); svc.collect(job_id)
    assert len(web[2]) == count
    with pytest.raises(IntelligenceError, match="Replay key"):
        svc.create_job(plan(reg, run_id="different-run"), "same-key")


def test_freeze_cas_inflight_and_late_receipt_do_not_mutate_bundle(tmp_path, web):
    svc, reg, _ = service(tmp_path, web); p = plan(reg)
    job = svc.create_job(p, "freeze-race"); job_id = job["job_id"]
    attempt = svc.archive.begin_attempt(job_id, "deepseek", "search", {"parameters": {}, "reserved_requests": 1})
    with pytest.raises(IntelligenceError) as error:
        svc.freeze(job_id, expected_plan_hash=p["plan_hash"], expected_revision=svc.status(job_id)["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    assert error.value.code == "INFLIGHT_ATTEMPTS"
    svc.archive.settle_attempt(job_id, attempt, "unknown", {"request_count": 1, "bytes_received": 0,
        "usage": {"state": "unknown", "requires_reconciliation": True}})
    stale = svc.status(job_id)["revision"]
    svc.archive.append(job_id, "operator_note", {"reason": "new revision"})
    with pytest.raises(IntelligenceError) as error:
        svc.freeze(job_id, expected_plan_hash=p["plan_hash"], expected_revision=stale, expected_frontier_hash=p["frontier_manifest_hash"])
    assert error.value.code == "JOB_REVISION_CONFLICT"
    bundle = svc.freeze(job_id, expected_plan_hash=p["plan_hash"], expected_revision=svc.status(job_id)["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    before = canonical_json(bundle)
    late = svc.archive.settle_attempt(job_id, attempt, "completed", {"request_count": 1, "bytes_received": 40,
        "usage": {"state": "reported", "requires_reconciliation": False}})
    assert late["late"] and canonical_json(svc.archive.bundle(bundle["bundle_id"])) == before
    assert svc.status(job_id)["usage"]["requests"] == 1
    with pytest.raises(IntelligenceError) as error:
        svc.collect(job_id)
    assert error.value.code == "FROZEN_BUNDLE"


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_blob_and_metadata_tamper_fail_independent_read(tmp_path, web):
    svc, reg, _ = service(tmp_path, web); p = plan(reg)
    job = svc.create_job(p, "tamper"); acquired = svc.collect(job["job_id"])
    bundle = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=acquired["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    ref = bundle["documents"][0]["text_blob"]
    (tmp_path / ref["path"]).write_bytes(b"forged text")
    with pytest.raises(IntelligenceError) as error:
        verify_bundle(tmp_path, bundle["bundle_id"])
    assert error.value.code == "HASH_MISMATCH"


def test_append_only_tables_tenant_isolation_and_readonly(tmp_path, web):
    svc, reg, _ = service(tmp_path, web); p = plan(reg)
    job = svc.create_job(p, "isolation")
    with sqlite3.connect(tmp_path / "intelligence.sqlite3") as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM events")
    foreign = IntelligenceService(tmp_path, reg, {}, tenant="another", readonly=True)
    with pytest.raises(IntelligenceError) as error:
        foreign.status(job["job_id"])
    assert error.value.code == "EVIDENCE_NOT_FOUND"
    read = IntelligenceService(tmp_path, reg, {}, readonly=True)
    with pytest.raises(IntelligenceError) as error:
        read.create_job(p, "denied")
    assert error.value.code == "ACCESS_DENIED"


def test_explicit_baseline_import_retains_first_seen_and_no_semantic_reuse(tmp_path, web):
    original, reg, _ = service(tmp_path / "old", web); p = plan(reg)
    job = original.create_job(p, "old"); acquired = original.collect(job["job_id"])
    baseline = original.archive_versions("2026-11-05T13:00:00Z")
    assert len(baseline["versions"]) == 2
    newer = IntelligenceService(tmp_path / "new", reg, {}, clock=lambda: NOW)
    p2 = plan(reg, run_id="new-run", baseline_refs=[{"manifest_sha256": baseline["manifest_sha256"]}])
    j2 = newer.create_job(p2, "new")
    receipt = newer.import_baseline(j2["job_id"], tmp_path / "old",
        version_ids=[v["id"] for v in baseline["versions"]], expected_manifest_hash=baseline["manifest_sha256"])
    docs = newer.archive.documents(j2["job_id"])
    assert {d["times"]["first_seen"]["value"] for d in docs} == {NOW.isoformat()}
    assert not receipt["reused_semantic_annotations"] and not receipt["reused_search_results"]
    assert newer.status(j2["job_id"])["usage"]["requests"] == 0
    assert newer.archive_versions("2026-11-05T11:59:59Z")["versions"] == []
    original_doc = next(doc for doc in docs if doc["text_kind"] == "html_main")
    provider_doc = next(doc for doc in docs if doc["source_id"] == "deepseek")
    old_fetch = next(ref for ref in original_doc["source_attempt_refs"] if ref not in provider_doc["source_attempt_refs"])
    for refs in (["attempt_unknown"], [old_fetch]):
        with pytest.raises(IntelligenceError) as error:
            newer.record_assessment(j2["job_id"], [], [{"cell_id": "cell-a", "status": "present",
                "document_ids": [provider_doc["id"]], "attempt_refs": refs}])
        assert error.value.code == "EVIDENCE_NOT_FOUND"
    newer.record_assessment(j2["job_id"], [{"id": "fresh-claim", "document_ids": [original_doc["id"]],
        "target_ids": ["security:listed:US:AAPL"]}], [{"cell_id": "cell-a", "status": "present",
            "document_ids": [original_doc["id"]], "attempt_refs": original_doc["source_attempt_refs"]}])
    unbound_plan = plan(reg, run_id="unbound-run", baseline_refs=[])
    unbound = newer.create_job(unbound_plan, "unbound")
    original_record = {key: value for key, value in original_doc.items()
        if key not in {"target_ids", "requested_entity_ids", "query_hit_ids"}}
    newer.archive.import_document(unbound["job_id"], original_record, baseline, ["listed:US:AAPL"])
    for append_unadmitted_receipt in (False, True):
        if append_unadmitted_receipt:
            newer.archive.append(unbound["job_id"], "baseline_import", receipt)
        with pytest.raises(IntelligenceError) as error:
            newer.record_assessment(unbound["job_id"], [], [{"cell_id": "cell-a", "status": "present",
                "document_ids": [original_doc["id"]], "attempt_refs": original_doc["source_attempt_refs"]}])
        assert error.value.code == "EVIDENCE_NOT_FOUND"
    frozen = newer.freeze(j2["job_id"], expected_plan_hash=p2["plan_hash"], expected_revision=newer.status(j2["job_id"])["revision"], expected_frontier_hash=p2["frontier_manifest_hash"])
    assert len(verify_bundle(tmp_path / "new", frozen["bundle_id"])["documents"]) == 2 and frozen["state"] == "completed"


@pytest.mark.parametrize("adapter_cls,endpoint,response", [
    (BraveAdapter, "/brave", {"results": [{"title": "AAPL", "url": "https://example.com/a", "description": "news", "page_age": "2026-11-04"}]}),
    (TavilyAdapter, "/tavily", {"results": [{"title": "AAPL", "url": "https://example.com/a", "content": "summary", "raw_content": "original markdown", "published_date": "2026-11-04T14:00:00Z"}], "usage": {"credits": 2}}),
    (ExaAdapter, "/exa", {"results": [{"title": "AAPL", "url": "https://example.com/a", "text": "original text", "publishedDate": "2026-11-04T14:00:00Z"}], "costDollars": {"total": 0.01}}),
])
def test_three_optional_providers_send_actual_supported_parameters(web, adapter_cls, endpoint, response):
    url, routes, calls = web; routes[endpoint] = (200, "application/json", response)
    adapter = adapter_cls(HttpTransport(allow_local=True), "fixture-key", endpoint=url + endpoint)
    result = adapter.search(SearchRequest("AAPL earnings", count=10, topic="news",
        start_date="2026-11-01", end_date="2026-11-04", domains=("example.com",)))
    assert len(result.hits) == 1 and result.raw_bytes
    if adapter_cls is TavilyAdapter:
        body = json.loads(calls[-1]["body"])
        assert body["include_answer"] is False and body["auto_parameters"] is False
        assert body["include_published_date"] is True and body["include_raw_content"] == "markdown"
    if adapter_cls is ExaAdapter:
        body = json.loads(calls[-1]["body"])
        assert body["contents"] == {"text": True} and "summary" not in body
        assert result.usage["state"] == "reported"


def test_long_query_splits_without_losing_terms_or_constraints():
    request = SearchRequest(" ".join("company" + str(i) for i in range(50)),
        domains=("sec.gov", "example.com"), start_date="2026-11-01", end_date="2026-11-04")
    parts = compile_requests("deepseek", request)
    assert len(parts) > 2
    for domain in request.domains:
        assert " ".join(p.query for p in parts if p.domains == (domain,)) == request.query
    from trading_system.intelligence.adapters import deepseek_query
    assert all(len(deepseek_query(p)) <= 128 for p in parts)
    assert all(p.start_date == request.start_date and p.end_date == request.end_date for p in parts)


def test_root_entity_registry_dictionary_identifiers_are_normalized():
    from trading_system.intelligence import default_registry
    reg = default_registry()
    p = plan(reg, entities={"listed:US:AAPL": {"type": "security", "name": "Apple Inc.",
        "aliases": ["Apple", "AAPL"], "identifiers": {"ticker": ["AAPL"], "cik": ["0000320193"]}}})
    queries = [q for q in p["query_plan"] if q["source_id"] == "sec_submissions"]
    assert len(queries) == 1 and queries[0]["request"]["context"]["cik"] == "0000320193"


def test_production_transport_rejects_loopback_before_network(web):
    with pytest.raises(SourceError) as error:
        HttpTransport().request("GET", web[0] + "/article")
    assert error.value.code == "FETCH_DENIED" and not web[2]


@pytest.mark.parametrize("endpoint", ["https://user:secret@example.com/api", "https://example.com/api?api_key=secret",
    "https://example.com/api?token=secret", "https://example.com/api?ACCESS_TOKEN=secret",
    "https://example.com/api?x-amz-signature=secret", "https://example.com/api#fragment"])
def test_registry_rejects_userinfo_and_query_credentials(endpoint):
    with pytest.raises(IntelligenceError):
        SourceSpec("brave", "search", endpoint)


def test_provider_secret_url_is_rejected_and_never_archived(tmp_path, web):
    svc, reg, _ = service(tmp_path, web)
    web[1]["/ds/search"] = (200, "application/json", {"results": [{"title": "bad",
        "url": "https://example.com/private?api_key=foreign-secret", "content": "unsafe"}]})
    job = svc.create_job(plan(reg), "unsafe-url")
    result = svc.collect(job["job_id"])
    assert result["documents"] == []
    assert b"foreign-secret" not in b"".join(f.read_bytes() for f in tmp_path.rglob("*") if f.is_file())


def test_bridge_ephemeral_key_is_redacted_from_web_echo_and_replays_partial(tmp_path, web):
    from trading_system.intelligence.adapters import WebFetch
    class NativeClient:
        _api_key = "native-ephemeral-fixture-secret"
        search_receipts = []
        def search(self, query, **kwargs):
            return [{"url": web[0] + "/echo", "title": "Release", "summary": "discovery", "content": "provider body", "published": None}]
        def complete(self, *args, **kwargs):
            raise AssertionError("acquisition must not call a text model")
    web[1]["/echo"] = (200, "text/html", "<article>Original body echoes native-ephemeral-fixture-secret.</article>")
    reg = registry(web[0]); client = NativeClient()
    svc = IntelligenceService(tmp_path, reg, {"deepseek": DeepSeekBridge(client),
        "web_page": WebFetch(HttpTransport(allow_local=True))}, clock=lambda: NOW)
    p = plan(reg); job = svc.create_job(p, "bridge-secret"); result = svc.collect(job["job_id"])
    fetched = next(d for d in result["documents"] if d["text_kind"] == "html_main")
    assert fetched["completeness"] == "partial" and "[REDACTED_SECRET]" in fetched["text"]
    assert client._api_key.encode() not in b"".join(f.read_bytes() for f in tmp_path.rglob("*") if f.is_file())
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=svc.status(job["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    verify_bundle(tmp_path, frozen["bundle_id"])


@pytest.mark.parametrize("failure", [False, True])
def test_bridge_native_budget_meter_accounts_actual_wire_bytes_and_failure(tmp_path, web, failure):
    class NativeClient:
        search_receipts = []
        @contextmanager
        def native_io_budget(self, *, max_bytes, deadline):
            assert max_bytes == 1000000 and deadline > 0
            self.meter = {"bytes_received": 0, "request_count": 0, "transport_observed": False, "limit_reason": None}
            yield self.meter
        def search(self, query, **kwargs):
            self.meter.update(bytes_received=317, request_count=2, transport_observed=True,
                              limit_reason="bytes" if failure else None)
            if failure:
                error = LLMUnavailable("bounded fixture")
                error.diagnostic = {"error_type": "RESPONSE_TOO_LARGE"}
                raise error
            return []
    reg = registry(web[0]); svc = IntelligenceService(tmp_path, reg, {"deepseek": DeepSeekBridge(NativeClient())}, clock=lambda: NOW)
    job = svc.create_job(plan(reg), "native-budget"); result = svc.collect(job["job_id"])
    assert result["usage"]["bytes"] == 317 and result["usage"]["requests"] == 2
    if failure:
        assert result["gaps"][0]["reason"] == "BUDGET_EXHAUSTED"
        assert result["attempts"][0]["result"]["error"]["details"]["bytes_received"] == 317
    assert not web[2]


def test_fetch_failure_is_not_overwritten_by_pending_semantics(tmp_path, web):
    svc, reg, _ = service(tmp_path, web); web[1]["/article"] = (403, "text/plain", "denied")
    job = svc.create_job(plan(reg), "original-denied"); acquired = svc.collect(job["job_id"])
    assert acquired["gaps"][0]["status"] == "unverifiable" and acquired["gaps"][0]["reason"] == "FETCH_DENIED"


def test_request_byte_and_fetch_limits_stop_extra_network(tmp_path, web):
    svc, reg, _ = service(tmp_path, web)
    p = plan(reg, limits={"requests": 1, "fetches": 1, "bytes": 1000000, "rounds": 1, "concurrency": 1, "deadline_seconds": 100})
    job = svc.create_job(p, "one-request"); result = svc.collect(job["job_id"])
    assert len(web[2]) == 1 and result["usage"]["requests"] == 1
    assert result["gaps"][0]["reason"] == "BUDGET_EXHAUSTED"
    with pytest.raises(IntelligenceError) as error:
        svc.research(job["job_id"])
    assert error.value.code == "BUDGET_EXHAUSTED"
    with pytest.raises(SourceError) as error:
        HttpTransport(allow_local=True, max_bytes=8).request("GET", web[0] + "/article")
    assert error.value.code == "RESPONSE_TOO_LARGE" and error.value.bytes_received == 0


def test_deadline_prevents_any_network_and_malformed_json_never_becomes_empty(tmp_path, web):
    svc, reg, _ = service(tmp_path / "expired", web)
    p = plan(reg); job = svc.create_job(p, "expired")
    svc.clock = lambda: NOW.replace(hour=13)
    result = svc.collect(job["job_id"])
    assert not web[2] and result["gaps"][0]["reason"] == "BUDGET_EXHAUSTED"
    svc2, reg2, _ = service(tmp_path / "bad-json", web); web[1]["/ds/search"] = (200, "application/json", '{"results":[],"results":[]}')
    job2 = svc2.create_job(plan(reg2), "malformed"); result2 = svc2.collect(job2["job_id"])
    assert result2["documents"] == [] and result2["gaps"][0]["reason"] == "CONTRACT_INVALID"
    assert result2["usage"]["bytes"] == len('{"results":[],"results":[]}') and result2["usage"]["requests"] == 1


def test_http_remaining_body_bytes_bound_known_unknown_and_compressed_responses(web):
    url, routes, _ = web
    routes["/known"] = (200, "text/plain", b"12345678")
    transport = HttpTransport(allow_local=True, max_bytes=8)
    assert transport.request("GET", url + "/known").body == b"12345678"
    assert transport.total_bytes_received == 8
    routes["/oversize"] = (200, "text/plain", b"123456789")
    before = transport.total_bytes_received
    with pytest.raises(SourceError) as error:
        transport.request("GET", url + "/oversize")
    assert error.value.code == "RESPONSE_TOO_LARGE" and error.value.bytes_received == 0
    assert transport.total_bytes_received == before
    routes["/unknown"] = (200, "text/plain", b"123456789", {"Content-Length": None})
    with pytest.raises(SourceError) as error:
        transport.request("GET", url + "/unknown")
    assert error.value.code == "RESPONSE_TOO_LARGE" and error.value.bytes_received == 8
    assert transport.total_bytes_received == before + 8
    routes["/unknown-short"] = (200, "text/plain", b"1234567", {"Content-Length": None})
    assert transport.request("GET", url + "/unknown-short").body == b"1234567"
    compressed = gzip.compress(b"x" * 1000)
    assert len(compressed) < 64
    routes["/compressed"] = (200, "text/plain", compressed, {"Content-Encoding": "gzip"})
    compressed_transport = HttpTransport(allow_local=True, max_bytes=64)
    with pytest.raises(SourceError) as error:
        compressed_transport.request("GET", url + "/compressed")
    assert error.value.code == "RESPONSE_TOO_LARGE" and error.value.bytes_received == 64
    assert compressed_transport.total_bytes_received == 64


def test_redirect_cannot_echo_credential_query_to_another_origin(web):
    url, routes, calls = web
    redirected = url.replace("127.0.0.1", "localhost") + "/destination?api_key=fred-fixture-secret"
    routes["/redirect"] = (302, "text/plain", b"", {"Location": redirected})
    routes["/destination"] = (200, "text/plain", "unexpected credential receiver")
    with pytest.raises(SourceError) as error:
        HttpTransport(allow_local=True).request("GET", url + "/redirect", params={"api_key": "fred-fixture-secret"},
            allowed_hosts=("127.0.0.1", "localhost"))
    assert error.value.code == "FETCH_DENIED" and len(calls) == 1


@pytest.mark.parametrize("suffix", ["/destination/%66%72%65%64-fixture-secret", "/destination?value=fred%252Dfixture-secret", "/destination#fred%2Dfixture-secret"])
def test_redirect_denies_known_credential_echo_in_any_url_component(web, suffix):
    url, routes, calls = web
    routes["/redirect"] = (302, "text/plain", b"", {"Location": url + suffix})
    with pytest.raises(SourceError) as error:
        HttpTransport(allow_local=True).request("GET", url + "/redirect", params={"api_key": "fred-fixture-secret"})
    assert error.value.code == "FETCH_DENIED" and len(calls) == 1


@pytest.mark.parametrize("mode", ["query_without_echo", "authorization", "post_body", "same_origin_auth"])
def test_redirect_query_drop_and_existing_header_post_denials_remain(web, mode):
    url, routes, calls = web
    destination = (url if mode == "same_origin_auth" else url.replace("127.0.0.1", "localhost")) + "/destination"
    routes["/redirect"] = (302, "text/plain", b"", {"Location": destination})
    routes["/destination"] = (200, "text/plain", "safe public body")
    transport = HttpTransport(allow_local=True)
    if mode in {"query_without_echo", "same_origin_auth"}:
        result = transport.request("GET", url + "/redirect",
            params={"api_key": "fred-fixture-secret"} if mode == "query_without_echo" else None,
            headers={"Authorization": "Bearer fixture-secret"} if mode == "same_origin_auth" else {})
        assert result.body == b"safe public body" and len(calls) == 2
        assert calls[-1]["path"] == "/destination" and "fred-fixture-secret" not in calls[-1]["path"]
        if mode == "same_origin_auth":
            assert calls[-1]["headers"]["Authorization"] == "Bearer fixture-secret"
    else:
        with pytest.raises(SourceError) as error:
            transport.request("POST" if mode == "post_body" else "GET", url + "/redirect",
                headers={"Authorization": "Bearer fixture-secret"} if mode == "authorization" else {},
                json_body={"api_key": "fixture-secret"} if mode == "post_body" else None)
        assert error.value.code == "FETCH_DENIED" and len(calls) == 1


@pytest.mark.parametrize("header", ["X-API-Key", "X-Subscription-Token", "Api-Key", "Proxy-Authorization", "Cookie"])
def test_all_known_credential_headers_are_denied_on_cross_origin_redirect(web, header):
    url, routes, calls = web
    routes["/redirect"] = (302, "text/plain", b"", {"Location": url.replace("127.0.0.1", "localhost") + "/destination"})
    routes["/destination"] = (200, "text/plain", "unexpected credential receiver")
    with pytest.raises(SourceError) as error:
        HttpTransport(allow_local=True).request("GET", url + "/redirect", headers={header: "fixture-secret"})
    assert error.value.code == "FETCH_DENIED" and len(calls) == 1


@pytest.mark.parametrize("claims,coverage", [([{ "document_ids": [{}]}], []),
    ([{"document_ids": []}], []), ([], [{"cell_id": "cell-a", "status": "present", "document_ids": []}]),
    ([], [{"cell_id": "cell-a", "status": "missing", "attempt_refs": None}])])
def test_assessment_malformed_or_unbacked_claims_fail_explicitly(tmp_path, web, claims, coverage):
    svc, reg, _ = service(tmp_path, web); job = svc.create_job(plan(reg), "bad-assessment")
    with pytest.raises(IntelligenceError) as error:
        svc.record_assessment(job["job_id"], claims, coverage)
    assert error.value.code == "CONTRACT_INVALID"


@pytest.mark.parametrize("field,value", [("title", "forged"), ("source_id", "deepseek"), ("source_version", "999"),
    ("original_url", "https://example.com/forged"), ("final_url", "https://example.com/forged"),
    ("publisher_id", "forged.test"), ("origin_cluster", "forged.test"), ("version", "f" * 64),
    ("authority", {"scope": "sec_filing", "proof_type": "immutable_accession", "proof_ref": {}}),
    ("source_metadata", {"authority": {"proof_type": "official_release"}})])
def test_offline_reconstruction_rejects_forged_semantic_provenance(tmp_path, web, field, value):
    svc, reg, _ = service(tmp_path, web); job = svc.create_job(plan(reg), "semantic-tamper"); acquired = svc.collect(job["job_id"])
    document = deepcopy(next(d for d in acquired["documents"] if d["text_kind"] == "html_main")); document[field] = value
    with pytest.raises(IntelligenceError) as error:
        reconstruct_document(document, svc.archive.read_blob, reg.public_manifest())
    assert error.value.code == "HASH_MISMATCH"


def test_same_url_changed_body_keeps_old_version_and_freeze_unchanged(tmp_path, web):
    svc, reg, _ = service(tmp_path, web); p = plan(reg)
    first = svc.create_job(p, "version-a"); original = svc.collect(first["job_id"])
    frozen = svc.freeze(first["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=svc.status(first["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    before = canonical_json(frozen)
    web[1]["/article"] = (200, "text/html", "<article><h1>Updated results</h1><p>Revenue revised to 12 million USD.</p></article>")
    second = svc.create_job(plan(reg, run_id="fixture-run-2"), "version-b"); updated = svc.collect(second["job_id"])
    old = next(d for d in original["documents"] if d["text_kind"] == "html_main")
    new = next(d for d in updated["documents"] if d["text_kind"] == "html_main")
    assert old["doc_id"] == new["doc_id"] and old["version"] != new["version"] and old["id"] != new["id"]
    assert svc.archive.document(old["id"])["text"] == old["text"]
    assert canonical_json(verify_bundle(tmp_path, frozen["bundle_id"])) == before


def test_unknown_connected_holding_is_required_gap_without_scope_expansion(tmp_path, web):
    svc, reg, _ = service(tmp_path, web)
    p = plan(reg, holdings=["AAPL", "UNKNOWN"])
    assert p["holdings"] == ["AAPL", "UNKNOWN"] and p["unavailable_holdings"] == ["UNKNOWN"]
    assert p["frontier"] == ["AAPL"] and p["allowed_entities"] == ["listed:US:AAPL"]
    assert not any("UNKNOWN" in row["request"]["query"] for row in p["query_plan"])
    job = svc.create_job(p, "unknown-holding"); result = svc.collect(job["job_id"])
    gap = next(g for g in result["gaps"] if g["target_id"] == "account:holding:UNKNOWN")
    assert gap["required"] and gap["status"] == "unverifiable" and gap["reason"] == "HOLDING_OUTSIDE_REVIEWED_ROSTER"
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=svc.status(job["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    assert gap["cell_id"] in frozen["missing_required"] and frozen["state"] == "partial"
    for change in ({"frontier": ["UNKNOWN"]}, {"watchlist": ["UNKNOWN"]}):
        with pytest.raises(IntelligenceError) as error:
            plan(reg, **change)
        assert error.value.code == "POLICY_DENIED"


def test_pending_scope_is_explicit_and_unknown_orders_are_required_connection_gaps(tmp_path, web):
    svc, reg, _ = service(tmp_path, web)
    p = plan(reg, universe={"AAPL": "listed:US:AAPL", "MSFT": "listed:US:MSFT"},
        pending=["MSFT", "ZZZ"], holdings=["UNKNOWN"])
    assert p["pending"] == ["MSFT", "ZZZ"] and p["unavailable_pending"] == ["ZZZ"]
    assert p["unavailable_holdings"] == ["UNKNOWN"] and p["holdings"] == ["UNKNOWN"]
    assert p["frontier"] == ["AAPL", "MSFT"] and p["allowed_entities"] == ["listed:US:AAPL", "listed:US:MSFT"]
    assert not any("ZZZ" in row["request"]["query"] for row in p["query_plan"])
    job = svc.create_job(p, "pending-outside"); result = svc.collect(job["job_id"])
    gap = next(row for row in result["gaps"] if row["target_id"] == "account:pending:ZZZ")
    assert gap["required"] and gap["status"] == "unverifiable" and gap["reason"] == "PENDING_OUTSIDE_REVIEWED_ROSTER"
    assert "listed:US:ZZZ" not in p["allowed_entities"]
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=svc.status(job["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    assert gap["cell_id"] in frozen["missing_required"] and frozen["state"] == "partial"


def test_mutable_job_projection_cannot_rewrite_frozen_plan(tmp_path, web):
    svc, reg, _ = service(tmp_path, web); p = plan(reg); job = svc.create_job(p, "plan-tamper")
    altered = {**p, "as_of": "2027-01-01T00:00:00Z"}
    with sqlite3.connect(tmp_path / "intelligence.sqlite3") as db:
        db.execute("UPDATE jobs SET plan_json=? WHERE job_id=?", (canonical_json(altered), job["job_id"]))
    with pytest.raises(IntelligenceError) as error:
        svc.status(job["job_id"])
    assert error.value.code == "HASH_MISMATCH"


def test_self_hashed_forged_bundle_still_needs_actual_freeze_event(tmp_path, web):
    svc, reg, _ = service(tmp_path, web); p = plan(reg); job = svc.create_job(p, "freeze-event-binding")
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=svc.status(job["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    forged_manifest = {key: value for key, value in frozen.items() if key not in {"bundle_id", "manifest_sha256", "freeze_event_ref"}}
    forged_manifest.update(state="completed", missing_required=[])
    sha = digest(forged_manifest); forged_id = "bundle_" + sha[:32]
    forged = {**forged_manifest, "bundle_id": forged_id, "manifest_sha256": sha, "freeze_event_ref": frozen["freeze_event_ref"]}
    with sqlite3.connect(tmp_path / "intelligence.sqlite3") as db:
        db.execute("DROP TRIGGER no_update_bundles")
        db.execute("UPDATE bundles SET bundle_id=?,manifest_sha256=?,record_json=? WHERE bundle_id=?",
            (forged_id, sha, canonical_json(forged), frozen["bundle_id"]))
    from trading_system.intelligence.contracts import canonical
    (tmp_path / "bundles" / (forged_id + ".json")).write_bytes(canonical(forged))
    with pytest.raises(IntelligenceError) as error:
        verify_bundle(tmp_path, forged_id)
    assert error.value.code == "HASH_MISMATCH"


def test_blob_shard_symlink_cannot_escape_archive(tmp_path):
    archive = Archive(tmp_path / "archive")
    external = tmp_path / "outside"; external.mkdir()
    raw = b"untrusted source bytes"; sha = hashlib.sha256(raw).hexdigest()
    (archive.root / "blobs" / "sha256" / sha[:2]).symlink_to(external, target_is_directory=True)
    with pytest.raises(IntelligenceError) as error:
        archive.put_blob(raw)
    assert error.value.code == "ACCESS_DENIED" and list(external.iterdir()) == []


def test_official_only_research_is_audited_partial_and_preserves_present_and_connection(tmp_path, web):
    url, routes, calls = web
    routes["/vix"] = (200, "text/csv", "DATE,OPEN,HIGH,LOW,CLOSE\n11/04/2026,20,22,19,21\n")
    reg = SourceRegistry([SourceSpec("cboe_vix", "cboe_csv", url + "/vix", minimum_interval_seconds=0)])
    svc = IntelligenceService(tmp_path, reg, reg.build_adapters(transport=HttpTransport(allow_local=True)), clock=lambda: NOW)
    p = plan(reg, required_cells=[
        {"cell_id": "present", "target_id": "global:macro", "question": "Current volatility", "required": True},
        {"cell_id": "missing", "target_id": "chain:memory", "question": "Memory demand", "required": True},
        {"cell_id": "connection", "target_id": "account:holdings", "question": "Holdings connection", "required": False, "material_type": "connection"}])
    job = svc.create_job(p, "official-only"); acquired = svc.collect(job["job_id"])
    svc.record_assessment(job["job_id"], [], [{"cell_id": "present", "status": "present", "document_ids": [acquired["documents"][0]["id"]]},
        {"cell_id": "missing", "status": "missing"}])
    count = len(calls); result = svc.research(job["job_id"], replay_key="research-once")
    assert len(calls) == count and result["usage"]["requests"] == 1
    gaps = {row["cell_id"]: row for row in result["gaps"]}
    assert gaps["present"]["status"] == "present"
    assert gaps["connection"]["status"] == "not_connected"
    assert gaps["missing"]["status"] == "unverifiable" and gaps["missing"]["reason"] == "NO_REGISTERED_DISCOVERY_PROVIDER"
    assert len(svc.archive.events(job["job_id"], kind="research_unavailable")) == 1
    revision = result["revision"]; svc.research(job["job_id"], replay_key="research-once")
    assert svc.status(job["job_id"])["revision"] == revision
    with pytest.raises(IntelligenceError) as error:
        svc.research(job["job_id"], gap_ids=["not-a-gap"])
    assert error.value.code == "CONTRACT_INVALID"
    svc.record_assessment(job["job_id"], [], [{"cell_id": "missing", "status": "missing",
        "reason": "no_qualified_material_for_this_question"}])
    retained = next(row for row in svc.status(job["job_id"])["gaps"] if row["cell_id"] == "missing")
    assert retained["reason"] == "no_qualified_material_for_this_question"
    assert retained["source_reason"] == "NO_REGISTERED_DISCOVERY_PROVIDER"
    assert retained["source_diagnostics"][-1]["reason"] == "NO_REGISTERED_DISCOVERY_PROVIDER"
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=svc.status(job["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    assert frozen["state"] == "partial" and frozen["missing_required"] == ["missing"]
    with pytest.raises(IntelligenceError) as error:
        svc.research(job["job_id"])
    assert error.value.code == "FROZEN_BUNDLE"
