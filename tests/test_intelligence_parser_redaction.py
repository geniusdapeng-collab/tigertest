"""A redacted original cannot bypass a frozen parser failure or its policy."""
from copy import deepcopy
import hashlib

import pytest

from test_intelligence_acquisition import web
from trading_system.intelligence.contracts import FetchResult, IntelligenceError, digest, timestamp, utc_now
from trading_system.intelligence.parse_limits import ParsePolicy, _partial, redact_partial, replay_partial
from trading_system.intelligence.planner import build_plan
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.replay import reconstruct_document
from trading_system.intelligence.service import IntelligenceService
from trading_system.intelligence.transport import HttpResponse, HttpTransport


@pytest.mark.parametrize("max_input_bytes", [32 * 1024 * 1024, 64])
@pytest.mark.parametrize("attachment", [False, True])
def test_actual_rss_fetch_redaction_retains_parser_boundary_and_replays(tmp_path, web, max_input_bytes, attachment):
    base, routes, calls = web
    now = utc_now()
    policy = {"html_engine": "scrapling", "selector": ".article-body",
        "expected_terms": ["TEST_FIXTURE"], "max_input_bytes": max_input_bytes}
    link = ('<a href="https://test-fixture.invalid/report.pdf?token=TEST_FIXTURE_URL_VALUE">Earnings release</a>'
        if attachment else '<a href="https://test-fixture.invalid/reference?token=TEST_FIXTURE_URL_VALUE">Source reference</a>')
    body = ('<html><head><title>TEST_FIXTURE NVDA Original</title>'
        '<meta property="article:published_time" content="' + timestamp(now) + '"></head>'
        '<body><div class="article-body"><h1>TEST_FIXTURE NVDA Original</h1>'
        '<p>TEST_FIXTURE NVDA documented conditional demand and complete operating context.</p>'
        + link + '</div></body></html>')
    routes["/body"] = 200, "text/html", body
    routes["/feed"] = (200, "application/rss+xml", '<rss><channel><item>'
        '<title>TEST_FIXTURE NVDA Original</title><link>' + base + '/body</link>'
        '<description>TEST_FIXTURE original discovery</description></item></channel></rss>')
    spec = SourceSpec("TEST_FIXTURE_issuer_feed", "issuer_rss", base + "/feed",
        version="TEST_FIXTURE-v1", allowed_hosts=("127.0.0.1",), minimum_interval_seconds=0,
        max_retries=0, authority_scopes=("issuer_ir",), options={"issuer_id": "issuer:TEST_FIXTURE",
            "material_types": ["issuer_ir"], "extractor": policy})
    registry = SourceRegistry([spec])
    service = IntelligenceService(tmp_path, registry,
        registry.build_adapters(transport=HttpTransport(allow_local=True), environment={}), clock=lambda: now)
    observed = []
    original_observe = service._observe_result
    def observe(source_id, operation, parameters, result):
        if isinstance(result, FetchResult):
            observed.append(result)
        return original_observe(source_id, operation, parameters, result)
    service._observe_result = observe
    plan = build_plan({"run_id": "TEST_FIXTURE-parser-redaction", "as_of": timestamp(now),
        "universe": {}, "frontier_manifest_hash": "0" * 64, "source_ids": [spec.source_id],
        "required_cells": [{"target_id": "topic:TEST_FIXTURE", "question": "TEST_FIXTURE raw replay",
            "required": True}]}, registry)
    job = service.create_job(plan, "TEST_FIXTURE-parser-redaction")
    acquired = service.collect(job["job_id"])
    document = next(row for row in acquired["documents"] if row["normalization"]["operation"] == "fetch")
    assert len(calls) == 2 and len(observed) == 1
    assert document["normalization"]["raw_redacted"] is True
    assert b"TEST_FIXTURE_URL_VALUE" not in service.archive.read_blob(document["raw_blob"])
    replay = reconstruct_document(document, service.archive.read_blob, registry.public_manifest())
    assert replay["completeness"] == document["completeness"] == "partial"
    assert document["attachments"] == []
    if max_input_bytes == 64:
        failure = document["extraction_metadata"]["parse_failure"]
        assert document["text"] == observed[0].text == "" and document["text_kind"] == "attachment"
        assert failure["code"] == "PARSER_INPUT_TOO_LARGE"
        assert failure["redacted_original"] == observed[0].metadata["parse_failure"]
        assert failure["input_sha256"] == document["raw_blob"]["sha256"]
        assert failure["redacted_original"]["input_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    else:
        assert observed[0].metadata["extractor"] == document["extraction_metadata"]["extractor"] == "tiger-scrapling/v1"
        assert document["text"] and "parse_failure" not in document["extraction_metadata"]
        assert document["extraction_metadata"]["extractor_config_sha256"] != observed[0].metadata["extractor_config_sha256"]


def sealed_failure(code="PARSER_DEADLINE_EXCEEDED"):
    original_body = b"TEST_FIXTURE confidential original wire"
    transport_sha = hashlib.sha256(original_body).hexdigest()
    response = HttpResponse("https://test-fixture.invalid/original", 200, {"content-type": "text/html"},
        original_body, "2026-11-06T00:00:00+00:00", transport_sha)
    policy = ParsePolicy(html_engine="scrapling", max_input_bytes=64)
    observed = _partial(response, None, code, policy)
    sanitized = HttpResponse(response.url, response.status, response.headers, b"[REDACTED_SECRET]",
        response.received_at, transport_sha)
    return policy, observed, sanitized


@pytest.mark.parametrize("code", ["PARSER_INPUT_TOO_LARGE", "PARSER_DEADLINE_EXCEEDED", "PARSER_MEMORY_EXCEEDED"])
def test_rebinding_observed_failure_does_not_run_or_improve_parser(monkeypatch, code):
    from trading_system.intelligence import parse_limits
    policy, observed, sanitized = sealed_failure(code)
    monkeypatch.setattr(parse_limits, "isolated_extract", lambda *_args, **_kwargs: pytest.fail("No parser rerun"))
    result = redact_partial(sanitized, original=observed, policy=policy)
    assert result.text == "" and result.completeness == "partial" and result.text_kind == "attachment"
    assert result.metadata["extraction_error"] == code
    assert result.metadata["parse_failure"]["redacted_original"] == observed.metadata["parse_failure"]
    assert replay_partial(sanitized, policy=policy, receipt=result.metadata["parse_failure"]) == result
    with pytest.raises(IntelligenceError) as recursive:
        redact_partial(sanitized, original=result, policy=policy)
    assert recursive.value.code == "HASH_MISMATCH"


@pytest.mark.parametrize("damage", ["transport_hash", "original_code", "original_policy", "extra_field", "stored_hash"])
def test_redacted_failure_replay_rejects_rehashed_forged_bindings(damage):
    policy, observed, sanitized = sealed_failure()
    result = redact_partial(sanitized, original=observed, policy=policy)
    receipt = deepcopy(result.metadata["parse_failure"])
    prior = receipt["redacted_original"]
    if damage == "transport_hash":
        prior["input_sha256"] = "a" * 64
    elif damage == "original_code":
        prior["code"] = "PARSER_MEMORY_EXCEEDED"
    elif damage == "original_policy":
        prior["policy"]["max_input_bytes"] = 128
    elif damage == "extra_field":
        prior["invented"] = True
    else:
        receipt["input_sha256"] = "b" * 64
    prior["sha256"] = digest({key: value for key, value in prior.items() if key != "sha256"})
    receipt["sha256"] = digest({key: value for key, value in receipt.items() if key != "sha256"})
    with pytest.raises(IntelligenceError) as rejected:
        replay_partial(sanitized, policy=policy, receipt=receipt)
    assert rejected.value.code == "HASH_MISMATCH"


@pytest.mark.parametrize("engine", ["builtin", "scrapling"])
def test_literal_redaction_marker_is_not_a_relative_attachment_or_an_unchanged_parser_version(engine):
    from trading_system.intelligence.extract import extract_response
    raw = (b'<html><title>TEST_FIXTURE release</title><article><h1>TEST_FIXTURE release</h1>'
        b'<p>TEST_FIXTURE operating evidence.</p><a href="[REDACTED_URL]">Earnings release</a></article></html>')
    response = HttpResponse("https://test-fixture.invalid/release", 200, {"content-type": "text/html"},
        raw, "2026-11-06T00:00:00+00:00", hashlib.sha256(raw).hexdigest())
    result = extract_response(response, policy=ParsePolicy(html_engine=engine))
    assert result.attachments == []
    assert result.metadata["redaction_markup_policy"] == "tiger.redacted-html-links/v1"
    assert result.metadata["extractor_config_sha256"]
