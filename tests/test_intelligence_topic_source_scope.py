"""Trusted source convergence with real local collection, never model calls."""
from html import escape
import json

import pytest

from test_intelligence_topic_safety_integration import SAFE_QUOTE, _service, local_topic
from trading_system.intelligence.contracts import timestamp, utc_now
from trading_system.intelligence.planner import build_plan
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.topic import TopicResearch


def _scope(service, dimension, key):
    policy = {material: {"status": "required" if material == dimension else "not_applicable",
        "reason": "TEST_FIXTURE source scope" if material == dimension else "TEST_FIXTURE unrelated source type"}
        for material in service.catalog.dimensions()}
    topic = service.create({"question": "TEST_FIXTURE NVDA narrow " + dimension,
        "entity_ids": ["us:NVDA"], "material_policy": policy, "idempotency_key": key + "-topic"})
    return topic, service.submit({"topic_id": topic["topic_id"], "version": topic["version"],
        "idempotency_key": key + "-research"})


def _archive_all(service, topic):
    source_service = service._service()
    plan = build_plan({"run_id": "TEST_FIXTURE-curated-prior", "as_of": timestamp(utc_now()),
        "universe": {"NVDA": "us:NVDA"}, "frontier": ["NVDA"], "frontier_manifest_hash": "0" * 64,
        "source_ids": service.registry.source_ids(), "required_cells": [{
            "target_id": "topic:" + topic["topic_id"], "question": "TEST_FIXTURE existing broad archive",
            "entity_id": "us:NVDA", "required": True}]}, service.registry)
    prior = source_service.create_job(plan, "TEST_FIXTURE-curated-prior")
    result = source_service.collect(prior["job_id"])
    assert len(result["documents"]) == 3


def _curated(h):
    return _service(h, [
        ("curated_nvidia_ir", SAFE_QUOTE + " TEST_FIXTURE issuer IR original.", ["issuer_ir"], ["topic_analysis"]),
        ("curated_fed_data", SAFE_QUOTE + " TEST_FIXTURE industry data original.", ["industry_data"], ["topic_analysis"]),
        ("curated_gs_statement", SAFE_QUOTE + " TEST_FIXTURE original named statement.", ["person_statement"], ["topic_analysis"]),
    ])[0]


@pytest.mark.parametrize("dimension,expected_source", [
    ("issuer_ir", "curated_nvidia_ir"), ("person_statement", "curated_gs_statement"),
])
def test_prefetch_and_snapshot_share_narrow_trusted_source_scope(local_topic, dimension, expected_source):
    h = local_topic
    service = _curated(h)
    topic, job = _scope(service, dimension, "TEST_FIXTURE-selected")
    _archive_all(service, topic)
    before = len(h.state["source_calls"])
    assert service._topic_sources(topic) == [expected_source]
    service._acquire(job, [topic["question"]], 0)
    assert [row["path"] for row in h.state["source_calls"][before:]] == ["/" + expected_source]
    snapshot = service._snapshot(job, 0)
    documents = service._service().archive.snapshot_documents(snapshot["snapshot_id"])
    assert snapshot["scope"]["source_ids"] == [expected_source]
    assert {document["source_id"] for document in documents} == {expected_source}
    assert len(h.state["source_calls"]) == before + 1 and h.state["model_requests"] == []
    (h.root / "source-scope-case-evidence.json").write_text(json.dumps({
        "schema": "tiger.topic-source-scope-case/v1", "kind": "TEST_FIXTURE_actual_HTTP_collection_only",
        "preconditions": "Three curated material sources already exist in the same entity archive",
        "steps": ["Declare only " + dimension + " active", "Collect", "Seal scoped snapshot"],
        "expected_source_ids": [expected_source], "actual_scope": snapshot["scope"],
        "actual_document_source_ids": sorted({document["source_id"] for document in documents}),
        "actual_model_calls": 0, "not_claimed": "No core-direction/radar selector or production model-quality proof",
    }, ensure_ascii=False, indent=2) + "\n")


def test_default_search_and_fetch_identity_dependencies_are_not_dropped(local_topic):
    h = local_topic
    specs = [
        SourceSpec("curated_nvidia_ir", "page", h.base + "/ir", allowed_hosts=("127.0.0.1",),
            options={"material_types": ["issuer_ir"]}),
        SourceSpec("deepseek", "search", "https://api.deepseek.com/v1/tools/search", credential_env="LLM_API_KEY"),
        SourceSpec("web_page", "web_fetch", "", capabilities=("fetch",)),
        SourceSpec("sec_directory", "sec_directory", "https://www.sec.gov/files/company_tickers.json", allowed_hosts=("www.sec.gov",)),
    ]
    service = TopicResearch(h.root / "selector-state", h.root / "selector-archive",
        mode="research", registry=SourceRegistry(specs))
    topic, _job = _scope(service, "issuer_ir", "TEST_FIXTURE-dependency")
    assert set(service._topic_sources(topic)) == {"curated_nvidia_ir", "deepseek", "web_page", "sec_directory"}
    assert h.state["source_calls"] == h.state["model_requests"] == []


def test_new_trusted_source_can_enter_next_snapshot_without_extending_old_cutoff(local_topic):
    h = local_topic
    original = _curated(h)
    old_topic, old_job = _scope(original, "issuer_ir", "TEST_FIXTURE-old")
    _archive_all(original, old_topic)
    original._acquire(old_job, [old_topic["question"]], 0)
    old = original._snapshot(old_job, 0)
    old_documents = original._service().archive.snapshot_documents(old["snapshot_id"])
    new_source = SourceSpec("curated_new_ir", "page", h.base + "/curated_new_ir",
        allowed_hosts=("127.0.0.1",), minimum_interval_seconds=0, max_retries=0,
        options={"material_types": ["issuer_ir"], "allowed_uses": ["topic_analysis"], "extractor": {"html_engine": "builtin"}})
    new_text = "TEST_FIXTURE NVDA new independently registered IR original after the first cutoff."
    h.state["pages"]["/curated_new_ir"] = '<html><head><meta property="article:published_time" content="' + timestamp(utc_now()) + '"></head><body><article><h1>TEST_FIXTURE new IR</h1><p>' + escape(new_text) + '</p></article></body></html>'
    registry = SourceRegistry([*original.registry.sources.values(), new_source])
    current = TopicResearch(original.root, original.archive_root, mode="research", registry=registry,
        policy=original.policy, service_factory=original.service_factory)
    topic, job = _scope(current, "issuer_ir", "TEST_FIXTURE-next")
    current._acquire(job, [topic["question"]], 0)
    new = current._snapshot(job, 0)
    documents = current._service().archive.snapshot_documents(new["snapshot_id"])
    assert set(new["scope"]["source_ids"]) == {"curated_nvidia_ir", "curated_new_ir"}
    assert {document["source_id"] for document in documents} == {"curated_nvidia_ir", "curated_new_ir"}
    assert any(new_text in document["text"] for document in documents)
    assert original._snapshot(old_job, 0) == old
    assert original._service().archive.snapshot_documents(old["snapshot_id"]) == old_documents
    assert old["cutoff"] != new["cutoff"] and h.state["model_requests"] == []
