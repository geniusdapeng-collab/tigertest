"""Actual local source/model HTTP with TEST_FIXTURE dates and replies only."""
from datetime import timedelta
import json
import re

import pytest

from test_intelligence_topic_safety_integration import (
    SAFE_QUOTE, _analysis, _claim, _review, _service, local_topic,
)
from trading_system.intelligence import topic as topic_module
from trading_system.intelligence.contracts import aware, timestamp, utc_now


@pytest.mark.parametrize("dimension,horizon,age_days,precision,expected_state,expected_reason", [
    ("news_events", "week", 9, "instant", "partial", "MATERIAL_OUTSIDE_REQUESTED_TIME_WINDOW"),
    ("news_events", "month", 9, "instant", "completed", "MATERIAL_REQUESTED_TIME_WINDOW_QUALIFIED"),
    ("issuer_ir", "current", 9, "instant", "completed", "MATERIAL_REQUESTED_TIME_WINDOW_QUALIFIED"),
    ("issuer_ir", "week", 9, "instant", "partial", "MATERIAL_OUTSIDE_REQUESTED_TIME_WINDOW"),
    ("news_events", "week", 0, "date", "partial", "MATERIAL_PUBLICATION_CUTOFF_UNVERIFIED"),
    ("issuer_ir", "baseline", 66, "naive", "completed", "MATERIAL_BASELINE_WINDOW_QUALIFIED"),
])
def test_fixed_cutoff_coverage_uses_publication_not_new_retrieval_or_today(
        local_topic, monkeypatch, dimension, horizon, age_days, precision, expected_state, expected_reason):
    h = local_topic
    service, _, _unused = _service(h, [("public_original", SAFE_QUOTE,
        [dimension], ["topic_analysis"])])
    policy = {key: {"status": "required" if key == dimension else "not_applicable",
        "reason": "TEST_FIXTURE exact temporal question" if key == dimension else "TEST_FIXTURE unrelated dimension"}
        for key in service.catalog.dimensions()}
    topic = service.create({"question": "TEST_FIXTURE inspect NVDA publication-window evidence",
        "entity_ids": ["us:NVDA"], "material_policy": policy, "horizon": horizon,
        "idempotency_key": "TEST_FIXTURE-freshness-topic"})
    job = service.submit({"topic_id": topic["topic_id"], "version": topic["version"],
        "idempotency_key": "TEST_FIXTURE-freshness-research"})
    published = utc_now() - timedelta(days=age_days)
    raw_date = (published.date().isoformat() if precision == "date" else
        published.replace(tzinfo=None).isoformat(timespec="seconds") if precision == "naive" else timestamp(published))
    h.state["pages"]["/public_original"] = re.sub(
        r'(<meta property="article:published_time" content=")[^"]+(">)',
        lambda match: match[1] + raw_date + match[2], h.state["pages"]["/public_original"])
    h.state["reply"] = lambda wire, payload: (_analysis([_claim(payload["documents"][0], SAFE_QUOTE,
        dimension=dimension)]) if wire["reasoning_effort"] == "high" else _review())
    report = service.run(job["job_id"])
    assert report["state"] == expected_state, report
    cell = next(cell for cell in report["coverage"] if cell["dimension_id"] == dimension)
    checked = cell["freshness_checks"][0]
    assert checked["reason"] == expected_reason and checked["checked_as_of"] == report["cutoff"]
    assert checked["qualified"] is (expected_state == "completed")
    documents = service._service().archive.snapshot_documents(report["snapshot_ids"][0])
    assert len(documents) == 1 and SAFE_QUOTE in documents[0]["text"]
    assert documents[0]["times"]["published"]["raw"] == raw_date
    assert aware(documents[0]["times"]["first_seen"]["value"]) >= aware(job["created_at"])
    if precision == "naive":
        assert documents[0]["times"]["published"]["value"] is None
        assert documents[0]["times"]["published"]["precision"] == "unknown"
        assert "历史背景基线" in report["readable_report"] and "不是当期新增催化" in report["readable_report"]
    if expected_state == "partial":
        assert cell["status"] == "unknown" and cell["reason"] == expected_reason and report["required_gaps"]
        assert expected_reason in report["readable_report"]
    receipt = report["receipt_sha256"]
    monkeypatch.setattr(topic_module, "utc_now", lambda: aware(report["cutoff"]) + timedelta(days=400))
    assert service.report(job["job_id"])["receipt_sha256"] == receipt
    assert len(h.state["model_requests"]) == 2 and len(h.state["source_calls"]) == 1
    (h.root / "freshness-case-evidence.json").write_text(json.dumps({
        "schema": "tiger.topic-freshness-case/v1", "kind": "TEST_FIXTURE_actual_loopback_not_model_quality",
        "preconditions": {"dimension": dimension, "horizon": horizon, "raw_publication": raw_date,
            "retrieval": documents[0]["times"]["first_seen"]},
        "steps": ["Fetch actual HTML", "Seal fixed snapshot", "Run controlled high/max model HTTP", "Reverify under a later wall clock"],
        "expected": {"state": expected_state, "reason": expected_reason, "sealed_cutoff_must_stay_fixed": True},
        "actual": {"state": report["state"], "coverage": cell, "cutoff": report["cutoff"], "receipt_sha256": receipt,
            "snapshot_id": report["snapshot_ids"][0], "original_text_retained": True},
    }, ensure_ascii=False, indent=2) + "\n")
