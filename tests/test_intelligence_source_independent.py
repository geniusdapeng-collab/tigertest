"""Independent negative tests for source authorization and replay boundaries."""
from copy import deepcopy
from dataclasses import replace
from urllib.parse import urlsplit
from datetime import datetime, timezone
import sqlite3
import time

import pytest

from test_intelligence_acquisition import web
from trading_system.intelligence.contracts import IntelligenceError, SearchRequest, SourceError, digest
from trading_system.intelligence.extract import extract_response
from trading_system.intelligence.parse_limits import ParsePolicy, replay_partial
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.transport import HttpTransport
from trading_system.intelligence.planner import build_plan
from trading_system.intelligence.service import IntelligenceService
from trading_system.intelligence.replay import reconstruct_document, verify_bundle


def test_page_attachment_does_not_forward_endpoint_credential_to_another_origin(web):
    base, routes, calls = web
    routes["/registered"] = (200, "text/html", "<article><h1>Company release</h1><p>Original release.</p></article>")
    routes["/attachment"] = (200, "text/plain", "An allowed public attachment")
    spec = SourceSpec("licensed_page", "page", base + "/registered",
        allowed_hosts=("127.0.0.1", "localhost"), credential_env="TIGER_TEST_PAGE_KEY")
    adapter = SourceRegistry([spec]).build_adapters(transport=HttpTransport(allow_local=True),
        environment={"TIGER_TEST_PAGE_KEY": "CANARY_ENDPOINT_ONLY_CREDENTIAL"})[spec.source_id]
    adapter.search(SearchRequest("registered release"))
    other_origin = base.replace("127.0.0.1", "localhost") + "/attachment"
    assert urlsplit(other_origin).netloc != urlsplit(spec.endpoint).netloc
    try:
        adapter.fetch(other_origin)
    except SourceError as error:
        assert error.code in {"FETCH_DENIED", "POLICY_DENIED"}
    assert calls[0]["headers"].get("Authorization") == "Bearer CANARY_ENDPOINT_ONLY_CREDENTIAL"
    assert all("Authorization" not in call["headers"] for call in calls if call["path"] == "/attachment")


def test_registered_feed_declared_credential_cannot_be_reported_available_without_key(web):
    base, routes, calls = web
    routes["/licensed-feed"] = (200, "application/rss+xml", "<rss><channel></channel></rss>")
    spec = SourceSpec("licensed_feed", "feed", base + "/licensed-feed",
        allowed_hosts=("127.0.0.1",), credential_env="TIGER_TEST_FEED_KEY")
    try:
        adapter = SourceRegistry([spec]).build_adapters(transport=HttpTransport(allow_local=True), environment={})[spec.source_id]
    except IntelligenceError as error:
        assert error.code in {"CONTRACT_INVALID", "POLICY_DENIED"}
        return
    assert adapter.available()["available"] is False
    with pytest.raises(SourceError) as denied:
        adapter.search(SearchRequest("authorized feed"))
    assert denied.value.code == "SOURCE_AUTH_REQUIRED" and not calls


@pytest.mark.parametrize("kind", ["feed", "issuer_rss"])
def test_authenticated_feed_sends_key_only_to_registered_origin(web, kind):
    base, routes, calls = web
    routes["/licensed-feed"] = (200, "application/rss+xml", "<rss><channel><item>"
        "<title>Authorized release</title><link>" + base + "/release</link>"
        "<pubDate>Fri, 06 Nov 2026 08:00:00 GMT</pubDate></item></channel></rss>")
    routes["/release"] = (200, "text/html", "<article><h1>Authorized release</h1><p>Original body.</p></article>")
    routes["/public-attachment"] = (200, "text/plain", "Public companion report")
    spec = SourceSpec("licensed_feed", kind, base + "/licensed-feed",
        allowed_hosts=("127.0.0.1", "localhost"), credential_env="TIGER_TEST_FEED_KEY",
        options={"issuer_id": "issuer:0000320193"} if kind == "issuer_rss" else {})
    adapter = SourceRegistry([spec]).build_adapters(transport=HttpTransport(allow_local=True),
        environment={"TIGER_TEST_FEED_KEY": "CANARY_FEED_ENDPOINT_ONLY_CREDENTIAL"})[spec.source_id]
    assert adapter.available()["available"] is True
    found = adapter.search(SearchRequest("authorized release"))
    assert found.hits[0]["url"] == base + "/release"
    assert adapter.fetch(found.hits[0]["url"]).text == "Authorized release\nOriginal body."
    public_url = base.replace("127.0.0.1", "localhost") + "/public-attachment"
    assert adapter.fetch(public_url).text == "Public companion report"
    assert len(calls) == 3
    assert calls[0]["headers"]["Authorization"] == "Bearer CANARY_FEED_ENDPOINT_ONLY_CREDENTIAL"
    assert calls[1]["headers"]["Authorization"] == "Bearer CANARY_FEED_ENDPOINT_ONLY_CREDENTIAL"
    assert "Authorization" not in calls[2]["headers"]
    # A host allowlist does not grant another scheme/port the endpoint secret.
    assert "Authorization" not in adapter._headers(base.replace("http://", "https://") + "/release")
    endpoint = urlsplit(base)
    different_port = endpoint._replace(netloc=endpoint.hostname + ":" + str(endpoint.port + 1)).geturl()
    assert "Authorization" not in adapter._headers(different_port + "/release")


@pytest.mark.parametrize("kind", ["web_fetch", "sec_directory", "sec_submissions", "cboe_csv", "bls_calendar"])
def test_credential_config_is_rejected_for_adapter_without_auth_contract(web, kind):
    spec = SourceSpec("unsupported_auth", kind, web[0] + "/source", allowed_hosts=("127.0.0.1",),
        credential_env="TIGER_TEST_UNSUPPORTED_KEY")
    with pytest.raises(IntelligenceError) as error:
        SourceRegistry([spec]).build_adapters(transport=HttpTransport(allow_local=True),
            environment={"TIGER_TEST_UNSUPPORTED_KEY": "CANARY_UNSUPPORTED_CREDENTIAL"})
    assert error.value.code == "POLICY_DENIED" and not web[2]


def test_credentialed_redirect_rejects_scheme_change_before_second_send():
    class Response:
        status_code = 302
        headers = {"Location": "http://licensed.example/release"}
        def close(self):
            pass
    class Session:
        def __init__(self):
            self.calls = []
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response()
    session = Session()
    transport = HttpTransport(session=session, allow_local=True)
    with pytest.raises(SourceError) as blocked:
        transport.request("GET", "https://licensed.example/release",
            headers={"Authorization": "Bearer CANARY_TLS_ONLY_CREDENTIAL"},
            allowed_hosts=("licensed.example",))
    assert blocked.value.code == "FETCH_DENIED"
    assert len(session.calls) == 1


def test_pdf_parser_shared_deadline_failure_has_a_replayable_partial_receipt(tmp_path, web):
    from test_intelligence_sources import _pdf_bytes
    base, routes, calls = web
    routes["/original.pdf"] = (200, "application/pdf", _pdf_bytes())
    class DeadlineAtEndOfRead(HttpTransport):
        def request(self, *args, **kwargs):
            observed = super().request(*args, **kwargs)
            # The read completed before the hard deadline. The caller was
            # descheduled before extraction began, exhausting the shared time.
            self.deadline = time.monotonic() - 1
            return observed
    source = SourceSpec("fixed_pdf", "page", base + "/original.pdf",
        allowed_hosts=("127.0.0.1",), minimum_interval_seconds=0)
    registry = SourceRegistry([source])
    service = IntelligenceService(tmp_path, registry,
        registry.build_adapters(transport=DeadlineAtEndOfRead(allow_local=True), environment={}))
    plan = build_plan({"run_id": "independent-parser-deadline", "as_of": datetime.now(timezone.utc).isoformat(),
        "universe": {}, "frontier_manifest_hash": "0" * 64, "source_ids": [source.source_id],
        "required_cells": [{"target_id": "topic:test", "question": "original PDF", "required": True}],
        "limits": {"requests": 10, "fetches": 2, "bytes": 100000, "rounds": 1,
                   "concurrency": 1, "deadline_seconds": 10}}, registry)
    job = service.create_job(plan, "independent-parser-deadline")
    acquired = service.collect(job["job_id"])
    assert len(calls) == 1
    assert acquired["documents"][0]["completeness"] == "partial"
    assert acquired["documents"][0]["extraction_metadata"]["extraction_error"] == "PARSER_DEADLINE_EXCEEDED"
    frozen = service.freeze(job["job_id"], expected_plan_hash=plan["plan_hash"],
        expected_revision=service.status(job["job_id"])["revision"], expected_frontier_hash=plan["frontier_manifest_hash"])
    checked = verify_bundle(tmp_path, frozen["bundle_id"])
    assert checked["documents"][0]["text"] == "" and checked["documents"][0]["completeness"] == "partial"
    for mutation in ("text", "completeness"):
        fabricated = deepcopy(checked["documents"][0])
        fabricated[mutation] = "Invented complete PDF fact" if mutation == "text" else "complete"
        with pytest.raises(IntelligenceError) as denied:
            reconstruct_document(fabricated, service.archive.read_blob, checked["source_registry"])
        assert denied.value.code == "HASH_MISMATCH"


@pytest.mark.parametrize("field,value", [("code", "PARSER_MEMORY_EXCEEDED"), ("sha256", "0" * 64),
    ("input_sha256", "0" * 64), ("input_bytes", 0), ("text_sha256", "0" * 64),
    ("completeness", "complete"), ("evidence_scope", "verified_original_fact")])
def test_parser_failure_receipt_rejects_each_tampered_field(field, value):
    from test_intelligence_sources import _pdf_bytes, _response
    observed = _response(_pdf_bytes())
    failed = extract_response(observed, deadline=time.monotonic() - 1)
    receipt = deepcopy(failed.metadata["parse_failure"])
    receipt[field] = value
    with pytest.raises(IntelligenceError) as denied:
        replay_partial(observed, receipt=receipt)
    assert denied.value.code == "HASH_MISMATCH"


@pytest.mark.parametrize("mutation", ["raw", "policy", "invalid_code", "fabricated_fact"])
def test_parser_failure_receipt_binds_bytes_policy_and_empty_partial_scope(mutation):
    from test_intelligence_sources import _pdf_bytes, _response
    observed = _response(_pdf_bytes())
    policy = ParsePolicy(max_pages=1)
    failed = extract_response(observed, deadline=time.monotonic() - 1, policy=policy)
    receipt = deepcopy(failed.metadata["parse_failure"])
    selected = policy
    if mutation == "raw":
        observed = replace(observed, body=observed.body + b"\nchanged bytes")
    elif mutation == "policy":
        selected = replace(policy, max_pages=2)
    elif mutation == "invalid_code":
        receipt["code"] = "SUCCESS_WITH_FULL_FACTS"
    else:
        receipt["text_sha256"] = digest("A fabricated extracted fact")
        receipt["completeness"] = "complete"
    if mutation in {"invalid_code", "fabricated_fact"}:
        # Even a recomputed self-digest cannot enlarge the receipt's scope.
        receipt["sha256"] = digest({key: value for key, value in receipt.items() if key != "sha256"})
    with pytest.raises(IntelligenceError) as denied:
        replay_partial(observed, policy=selected, receipt=receipt)
    assert denied.value.code == "HASH_MISMATCH"


def test_pdf_attachment_parser_failure_remains_an_explicit_source_gap(tmp_path, web):
    from test_intelligence_parse_limits import pdf
    base, routes, calls = web
    routes["/release"] = (200, "text/html", "<article><h1>Results</h1><p>Original release.</p>"
        '<a href="/supplement.pdf">Financial supplement</a></article>')
    routes["/supplement.pdf"] = (200, "application/pdf", pdf(2))
    source = SourceSpec("fixed_release", "page", base + "/release",
        allowed_hosts=("127.0.0.1",), minimum_interval_seconds=0,
        options={"extractor": {"max_pages": 1}})
    registry = SourceRegistry([source])
    service = IntelligenceService(tmp_path, registry,
        registry.build_adapters(transport=HttpTransport(allow_local=True), environment={}))
    plan = build_plan({"run_id": "independent-attachment-gap", "as_of": datetime.now(timezone.utc).isoformat(),
        "universe": {}, "frontier_manifest_hash": "0" * 64, "source_ids": [source.source_id],
        "required_cells": [{"target_id": "topic:test", "question": "results and supplement", "required": True}],
        "limits": {"requests": 10, "fetches": 2, "bytes": 100000, "rounds": 1,
                   "concurrency": 1, "deadline_seconds": 10}}, registry)
    job = service.create_job(plan, "independent-attachment-gap")
    acquired = service.collect(job["job_id"])
    assert len(calls) == 2
    attachment = next(row for row in acquired["documents"] if "parent_document_id" in row)
    assert attachment["extraction_metadata"]["extraction_error"] == "PARSER_PAGE_LIMIT"
    assert attachment["completeness"] == "partial"
    assert acquired["gaps"][0]["status"] == "unverifiable"
    assert "ATTACHMENT" in acquired["gaps"][0]["reason"]


def test_unknown_paid_search_dispatch_blocks_new_retry_ordinal(tmp_path, web):
    base, routes, calls = web
    def first_response_lost(_call):
        if len(calls) == 1:
            time.sleep(0.15)
        return (200, "application/json", {"results": []})
    routes["/search"] = first_response_lost
    source = SourceSpec("deepseek", "search", base + "/search", credential_env="TIGER_TEST_SEARCH_KEY",
        minimum_interval_seconds=0, max_retries=1)
    registry = SourceRegistry([source])
    transport = HttpTransport(allow_local=True, timeout=0.02)
    adapters = registry.build_adapters(transport=transport, environment={"TIGER_TEST_SEARCH_KEY": "fixture-paid-api"})
    now = datetime.now(timezone.utc)
    service = IntelligenceService(tmp_path, registry, adapters, sleeper=lambda _delay: None)
    plan = build_plan({"run_id": "independent-unknown-send", "as_of": now.isoformat(), "universe": {},
        "frontier_manifest_hash": "0" * 64, "source_ids": ["deepseek"],
        "required_cells": [{"target_id": "topic:test", "question": "public primary source", "required": True}],
        "limits": {"requests": 10, "fetches": 2, "bytes": 100000, "rounds": 1,
                   "concurrency": 1, "deadline_seconds": 10}}, registry)
    job = service.create_job(plan, "independent-unknown-send")
    with pytest.raises(IntelligenceError) as blocked:
        service.collect(job["job_id"])
    assert blocked.value.code == "RECONCILIATION_REQUIRED"
    assert len(calls) == 1


def test_research_resume_continues_after_started_marker_before_first_query(tmp_path, monkeypatch):
    source = SourceSpec("deepseek", "search", "https://api.deepseek.com/v1/tools/search",
                        credential_env="TIGER_TEST_SEARCH_KEY")
    registry = SourceRegistry([source])
    service = IntelligenceService(tmp_path, registry, {})
    plan = build_plan({"run_id": "independent-research-resume", "as_of": datetime.now(timezone.utc).isoformat(),
        "universe": {}, "frontier_manifest_hash": "0" * 64, "source_ids": ["deepseek"],
        "required_cells": [{"target_id": "topic:test", "question": "original counterevidence", "required": True}],
        "limits": {"requests": 10, "fetches": 2, "bytes": 100000, "rounds": 2,
                   "concurrency": 1, "deadline_seconds": 10}}, registry)
    job = service.create_job(plan, "independent-research-resume")
    class ProcessDied(BaseException):
        pass
    def die(*_args, **_kwargs):
        raise ProcessDied
    monkeypatch.setattr(service, "_query", die)
    with pytest.raises(ProcessDied):
        service.research(job["job_id"], replay_key="same-research-round")
    calls = []
    monkeypatch.setattr(service, "_query", lambda *args, **kwargs: calls.append(args) or [])
    service.research(job["job_id"], replay_key="same-research-round")
    assert len(calls) == 1
    assert len(service.archive.events(job["job_id"], kind="research_started")) == 1


def test_completed_research_key_cannot_be_rebound_to_another_gap_request(tmp_path, monkeypatch):
    source = SourceSpec("deepseek", "search", "https://api.deepseek.com/v1/tools/search",
                        credential_env="TIGER_TEST_SEARCH_KEY")
    registry = SourceRegistry([source])
    service = IntelligenceService(tmp_path, registry, {})
    plan = build_plan({"run_id": "independent-research-binding", "as_of": datetime.now(timezone.utc).isoformat(),
        "universe": {}, "frontier_manifest_hash": "0" * 64, "source_ids": [source.source_id],
        "required_cells": [{"target_id": "topic:test", "question": "original release", "required": True}],
        "limits": {"requests": 10, "fetches": 2, "bytes": 100000, "rounds": 2,
                   "concurrency": 1, "deadline_seconds": 10}}, registry)
    job = service.create_job(plan, "independent-research-binding")
    monkeypatch.setattr(service, "_query", lambda *args, **kwargs: [])
    service.research(job["job_id"], replay_key="bound-research-round")
    with pytest.raises(IntelligenceError) as conflict:
        service.research(job["job_id"], gap_ids=[], replay_key="bound-research-round")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_host_policy_database_contention_obeys_remaining_http_deadline(tmp_path):
    from trading_system.intelligence.host_limits import HostRequestPolicy
    policy = HostRequestPolicy(tmp_path, minimum_interval_seconds=0)
    lock = sqlite3.connect(policy.path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(IntelligenceError) as denied:
            policy.before_request("https://original.example/release", deadline=started + 0.05)
        assert denied.value.code in {"DEADLINE_EXCEEDED", "ARCHIVE_UNAVAILABLE"}
        assert time.monotonic() - started < 0.4
    finally:
        lock.rollback()
        lock.close()
