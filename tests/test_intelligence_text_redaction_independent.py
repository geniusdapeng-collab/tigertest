"""Independent TEST_FIXTURE regression prepared outside the frozen source tree."""
from datetime import datetime, timezone
import json

import pytest

from test_intelligence_acquisition import web
from trading_system.intelligence.contracts import credential_free_url, safe_value
from trading_system.intelligence.planner import build_plan
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.replay import reconstruct_document
from trading_system.intelligence.service import IntelligenceService
from trading_system.intelligence.transport import HttpTransport

CANARY = "TEST_FIXTURE_CANARY"
PUBLIC = [
    "https://public.example/portal?topic=AI&lang=en",
    r"https://public.example/portal?topic=AI\u0026lang=en",
    r"https://public.example/research.html\u0026nbsp;\u0026nbsp;\u003ca",
]
SENSITIVE = [
    "https://public.example/?token=" + CANARY,
    "https://public.example/?access_token=" + CANARY,
    "https://" + CANARY + "@public.example/page",
    r"https://public.example/?\u0074oken=" + CANARY,
    r"https://public.example/?topic=AI\u0026token=" + CANARY,
    "https://public.example/?redirect=https%3A%2F%2Finner.example%2F%3Ftoken%3D" + CANARY,
    r"https://public.example/?topic=AI\u0026nbsp;\u0026token=" + CANARY,
    "https://public.example/?topic=AI&nbsp;&token=" + CANARY,
    r"https://public.example/?topic=AI\u0020\u0026token=" + CANARY,
    r"https://public.example/?topic=AI\u0009\u0026token=" + CANARY,
    r"https://" + CANARY + r"\u0026nbsp;@public.example/page",
    r"https://public.example/?topic=AI\u0026nbsp;\u0026redirect=https%3A%2F%2Finner.example%2F%3Ftoken%3D" + CANARY,
    r"https://public.example/page\u003ca\u0020href=\u0022https://" + CANARY + "@private.example/page",
    r"https://public.example/page\u0020https://" + CANARY + "@private.example/page",
]


@pytest.mark.parametrize("url", PUBLIC)
def test_independent_public_navigation_keeps_exact_raw_text(url):
    source = json.dumps({"html": '<a href="' + url + '">Portal</a>'})
    assert safe_value(source) == source


@pytest.mark.parametrize("url", SENSITIVE)
def test_independent_complete_decoded_url_cannot_hide_credential_after_boundary(url):
    source = json.dumps({"html": '<a href="' + url + '">Portal</a>'})
    redacted = safe_value(source)
    assert "[REDACTED_URL]" in redacted
    assert CANARY not in redacted


@pytest.mark.parametrize("url", [
    "https://public.example/?topic=AI&nbsp;&token=" + CANARY,
    r"https://" + CANARY + r"\u0026nbsp;@public.example/page",
    r"https://public.example/?topic=AI\u0026nbsp;\u0026redirect=https%3A%2F%2Finner.example%2F%3Ftoken%3D" + CANARY,
    r"https://public.example/page\u003ca\u0020href=\u0022https://" + CANARY + "@private.example/page",
    PUBLIC[-1],
])
def test_independent_actual_source_archive_redacts_query_userinfo_nested_or_second_url(tmp_path, web, url):
    base, routes, calls = web
    sensitive = CANARY in url
    body = ('<!doctype html><html><head><title>TEST_FIXTURE original</title></head><body><article>'
        '<h1>TEST_FIXTURE original</h1><p>Complete controlled operating evidence with explicit conditions and '
        'uncertainty. This test makes no financial statement and never calls an external source.</p></article>'
        '<script type="application/json">' + json.dumps({"navigation": '<a href="' + url + '">Portal</a>'})
        + "</script></body></html>")
    routes["/original"] = 200, "text/html", body
    source = SourceSpec("TEST_FIXTURE_source", "page", base + "/original",
        allowed_hosts=("127.0.0.1",), minimum_interval_seconds=0, max_retries=0,
        options={"material_types": ["news_events"]})
    registry = SourceRegistry([source])
    service = IntelligenceService(tmp_path / "archive", registry,
        registry.build_adapters(transport=HttpTransport(allow_local=True), environment={},
            parser_policy={"html_engine": "scrapling"}))
    plan = build_plan({"run_id": "TEST_FIXTURE-redaction", "as_of": datetime.now(timezone.utc).isoformat(),
        "universe": {}, "frontier_manifest_hash": "0" * 64, "source_ids": [source.source_id],
        "required_cells": [{"target_id": "topic:TEST_FIXTURE", "question": "Original fidelity", "required": True}],
        "limits": {"requests": 6, "fetches": 2, "bytes": 100000, "rounds": 1, "concurrency": 1, "deadline_seconds": 30}}, registry)
    job = service.create_job(plan, "TEST_FIXTURE-redaction")
    acquired = service.collect(job["job_id"])
    assert len(calls) == 1 and len(acquired["documents"]) == 1
    document = acquired["documents"][0]
    raw = service.archive.read_blob(document["raw_blob"])
    assert document["normalization"]["raw_redacted"] is sensitive
    assert (CANARY.encode() not in raw and document["completeness"] == "partial") if sensitive else raw == body.encode()
    replay = reconstruct_document(document, service.archive.read_blob, registry.public_manifest())
    assert replay["completeness"] == document["completeness"]


def test_independent_network_policy_still_refuses_literal_backslash_and_credential_urls():
    assert not credential_free_url("https://public.example/path\\")
    assert not credential_free_url("https://public.example/?token=" + CANARY)
    assert not credential_free_url("https://" + CANARY + "@public.example/page")


def test_independent_pending_candidate_scan_is_bounded_at_url_and_embedded_start_limits():
    prefix = "https://public.example/"
    at_limit = prefix + "a" * (8192 - len(prefix))
    assert safe_value(at_limit) == at_limit
    assert safe_value(at_limit + "a") == "[REDACTED_URL]"
    starts = "https://public.example/path" * 32
    assert safe_value(starts) == starts
    assert safe_value(starts + "https://public.example/path") == "[REDACTED_URL]"
