"""Historical default HTML version identities remain exact after link redaction.

Fixtures explicitly model the older no-parser-hash normalization contract.
The source text, dates, partial status and every nonmetadata identity remain
independently reconstructed from raw bytes; parser policy is never a waiver.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib

import pytest

from trading_system.intelligence.contracts import IntelligenceError, digest
from trading_system.intelligence.extract import extract_response
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.replay import reconstruct_document
from trading_system.intelligence.service import IntelligenceService
from trading_system.intelligence.transport import HttpResponse


NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)


def sample(tmp_path, *, explicit_policy=False, redacted=True):
    source = SourceSpec("web_page", "web_fetch", "", minimum_interval_seconds=0)
    registry = SourceRegistry([source, SourceSpec("deepseek", "search",
        "https://api.deepseek.com/v1/tools/search", minimum_interval_seconds=0)])
    service = IntelligenceService(tmp_path, registry, adapters={}, clock=lambda: NOW)
    # A redacted navigation URL has no attachment semantics in either parser.
    link = "[REDACTED_URL]" if redacted else "https://test-fixture.invalid/reference"
    raw = ('<html><head><title>TEST_FIXTURE original</title>'
        '<meta property="article:published_time" content="2026-11-06T12:00:00Z">'
        '</head><nav><a href="' + link + '">Reference</a></nav>'
        '<article><h1>TEST_FIXTURE original</h1>'
        '<p>TEST_FIXTURE reports conditional demand of 10 million.</p>'
        '</article></html>').encode()
    response = HttpResponse("https://test-fixture.invalid/original", 200,
        {"content-type": "text/html"}, raw, NOW.isoformat(), hashlib.sha256(raw).hexdigest())
    parsed = extract_response(response)
    normalized = parsed.__dict__.copy()
    normalized.pop("raw_bytes")
    normalized.pop("_parser_runtime", None)
    ref = service.archive.put_blob(raw, "text/html")
    policy = {"html_engine": "builtin"}
    parameters = {"url": response.url, **({"_parse_policy": policy} if explicit_policy else {})}
    observation = {"source_id": source.source_id, "normalization_kind": "registered_adapter",
        "raw_response_ref": ref, "raw_redacted": redacted,
        "transport_sha256": response.transport_sha256, "response_url": response.url,
        "http_headers": response.headers, "received_at": response.received_at,
        "actual_request": parameters, "request_count": 1, "parameters": parameters,
        "normalized": normalized}
    hit = {"url": response.url, "title": parsed.title, "published": parsed.published,
        "updated": parsed.updated, "text": parsed.text, "text_kind": parsed.text_kind,
        "completeness": parsed.completeness, "metadata": parsed.metadata,
        "raw_metadata": {}}
    # A separate registered search response reproduces this discovery hit.
    from trading_system.intelligence.contracts import canonical
    discovery_raw = canonical({"hits": [hit], "request": {}, "response_metadata": {}})
    discovery_ref = service.archive.put_blob(discovery_raw, "application/json")
    discovery = {**observation, "source_id": "deepseek", "normalization_kind": "deepseek_bridge",
        "raw_response_ref": discovery_ref, "raw_redacted": False,
        "transport_sha256": hashlib.sha256(discovery_raw).hexdigest(),
        "parameters": {}, "actual_request": {}}
    document = service._document("web_page", hit, discovery, fetch=observation)
    return service, registry, document


def legacy(document):
    value = deepcopy(document)
    metadata = value["extraction_metadata"]
    metadata.pop("extractor_config_sha256", None)
    metadata.pop("redaction_markup_policy", None)
    value["version"] = digest({"canonical_url": value["canonical_url"],
        "source_id": value["source_id"], "text_sha256": value["text_blob"]["sha256"],
        "text_kind": value["text_kind"], "published": value["times"]["published"],
        "updated": value["times"]["updated"], "completeness": value["completeness"]})
    value["id"] = "docver_" + digest({"doc_id": value["doc_id"], "version": value["version"]})[:32]
    return value


def test_old_default_redacted_navigation_retains_original_exact_version(tmp_path):
    service, registry, current = sample(tmp_path)
    old = legacy(current)
    assert old["version"] != current["version"]
    assert "version_contract" not in reconstruct_document(current, service.archive.read_blob,
                                                           registry.public_manifest())
    before = deepcopy(old)
    result = reconstruct_document(old, service.archive.read_blob, registry.public_manifest())
    assert old == before
    assert result["version_contract"] == "legacy-default-redacted-html-without-parser-hash/v1"
    assert result["completeness"] == "partial"
    assert result["text_sha256"] == current["text_blob"]["sha256"]


@pytest.mark.parametrize("damage", ["text", "date", "complete", "id", "version", "hash_present", "policy", "no_redaction"])
def test_legacy_metadata_compatibility_does_not_waive_original_evidence(tmp_path, damage):
    service, registry, current = sample(tmp_path)
    old = legacy(current)
    if damage == "text":
        old["text"] = old["text"].replace("10 million", "12 million")
    elif damage == "date":
        old["times"]["published"]["value"] = "2026-11-07T12:00:00+00:00"
    elif damage == "complete":
        old["completeness"] = "complete"
    elif damage == "id":
        old["id"] = "docver_" + "a" * 32
    elif damage == "version":
        old["version"] = "b" * 64
    elif damage == "hash_present":
        old["extraction_metadata"]["extractor_config_sha256"] = "c" * 64
    elif damage == "policy":
        old["normalization"]["parameters"]["_parse_policy"] = {"html_engine": "builtin"}
    else:
        old["normalization"]["raw_redacted"] = False
    with pytest.raises(IntelligenceError) as rejected:
        reconstruct_document(old, service.archive.read_blob, registry.public_manifest())
    assert rejected.value.code == "HASH_MISMATCH"


def test_current_recorded_parser_hash_cannot_be_changed_or_removed_with_original_identity(tmp_path):
    service, registry, current = sample(tmp_path)
    for replacement in ({"extractor_config_sha256": "d" * 64}, {}):
        changed = deepcopy(current)
        changed["extraction_metadata"] = replacement
        with pytest.raises(IntelligenceError) as rejected:
            reconstruct_document(changed, service.archive.read_blob, registry.public_manifest())
        assert rejected.value.code == "HASH_MISMATCH"
