"""Bind independently verified reports to actual sealed TEST_FIXTURE snapshots.

The source and model replies use actual local HTTP. Tampering changes only a
report and mutable job projection, leaving the original snapshot, source bytes,
model proofs and attempt receipts intact. No paid model is called here.
"""
from copy import deepcopy
from datetime import timedelta
import json
import re

import pytest

from test_intelligence_topic_safety_integration import (
    SAFE_QUOTE, _analysis, _claim, _review, _service, local_topic,
)
from trading_system.intelligence.contracts import IntelligenceError, aware, canonical, digest, timestamp, utc_now
from trading_system.intelligence.topic import render_report, topic_coverage


def _sealed(harness, *, two_rounds=False):
    service, _, _unused = _service(harness, [("public_original", SAFE_QUOTE,
        ["news_events"], ["topic_analysis"])])
    if two_rounds:
        service.policy["rounds"] = 2
    policy = {key: {"status": "required" if key == "news_events" else "not_applicable",
        "reason": "TEST_FIXTURE sealed report binding"} for key in service.catalog.dimensions()}
    topic = service.create({"question": "TEST_FIXTURE old news and sealed cutoff integrity",
        "entity_ids": ["us:NVDA"], "material_policy": policy, "horizon": "week",
        "idempotency_key": "TEST_FIXTURE-integrity-topic"})
    job = service.submit({"topic_id": topic["topic_id"], "version": topic["version"],
        "idempotency_key": "TEST_FIXTURE-integrity-research"})
    published = timestamp(utc_now() - timedelta(days=9))
    harness.state["pages"]["/public_original"] = re.sub(
        r'(<meta property="article:published_time" content=")[^"]+(">)',
        lambda match: match[1] + published + match[2], harness.state["pages"]["/public_original"])
    generations = []
    def reply(wire, payload):
        if wire["reasoning_effort"] != "high":
            return _review()
        generations.append(payload)
        analysis = _analysis([_claim(payload["documents"][0], SAFE_QUOTE)])
        if two_rounds and len(generations) == 1:
            analysis["followup_questions"] = ["TEST_FIXTURE verify the contrary publication"]
        return analysis
    harness.state["reply"] = reply
    report = service.run(job["job_id"])
    assert report["state"] == "partial", report
    assert len(report["snapshot_ids"]) == (2 if two_rounds else 1)
    archive = service._service(readonly=True).archive
    documents = archive.snapshot_documents(report["snapshot_ids"][-1])
    return service, job, report, documents, archive.snapshot(report["snapshot_ids"][-1])


def _reseal_mutable_projection(service, job, report, documents, *, change_job_snapshots=False):
    report["readable_report"] = render_report(report, documents)
    report["receipt_sha256"] = digest({key: value for key, value in report.items() if key != "receipt_sha256"})
    (service.store.directory(job["job_id"]) / "report.json").write_bytes(canonical(report) + b"\n")
    changes = {"state": report["state"], "report_sha256": report["receipt_sha256"]}
    if change_job_snapshots:
        changes["snapshot_ids"] = report["snapshot_ids"]
    service.store.update(job["job_id"], **changes)


@pytest.mark.parametrize("fault", [
    "cutoff", "state", "gaps", "completion_projection", "topic", "job_id",
    "snapshot_ids", "coherent_duplicate_snapshots", "model_label", "account_label",
])
def test_rehashed_report_and_mutable_job_cannot_override_sealed_report_bindings(local_topic, fault):
    h = local_topic
    service, job, original, documents, manifest = _sealed(h)
    original_attempts = service.store.attempts(job["job_id"])
    report = deepcopy(original)
    if fault == "cutoff":
        report["cutoff"] = timestamp(aware(manifest["cutoff"]) - timedelta(days=7))
        report["coverage"] = topic_coverage(job["topic"], report["analysis"], documents,
            service.catalog, as_of=report["cutoff"])
        report["required_gaps"] = list(manifest.get("gaps", [])) + [
            cell for cell in report["coverage"] if cell["required"] and cell["status"] != "covered"]
        report["state"] = "partial" if report["required_gaps"] else "completed"
        assert report["state"] == "completed"  # Demonstrates the actual backdating attempt.
    elif fault == "state":
        report["state"] = "completed"
    elif fault == "gaps":
        report["required_gaps"] = []
    elif fault == "completion_projection":
        report.update(state="completed", required_gaps=[])
    elif fault == "topic":
        report["topic"]["question"] = "TEST_FIXTURE an unrelated admitted question"
    elif fault == "job_id":
        report["job_id"] = "topicrun_" + "0" * 32
    elif fault in {"snapshot_ids", "coherent_duplicate_snapshots"}:
        report["snapshot_ids"].append(report["snapshot_ids"][-1])
    elif fault == "model_label":
        report["model"] = "TEST_FIXTURE-alternate-model"
    else:
        report["account_connected"] = True
    _reseal_mutable_projection(service, job, report, documents,
        change_job_snapshots=fault == "coherent_duplicate_snapshots")
    with pytest.raises(IntelligenceError) as error:
        service.report(job["job_id"])
    assert error.value.code == "HASH_MISMATCH"
    assert service.store.attempts(job["job_id"]) == original_attempts
    assert len(h.state["model_requests"]) == 2 and len(h.state["source_calls"]) == 1
    (h.root / "report-binding-case-evidence.json").write_text(json.dumps({
        "schema": "tiger.topic-report-binding-case/v1", "evidence_scope": "TEST_FIXTURE actual local HTTP then mutable projection tamper",
        "fault": fault, "preconditions": "A genuinely verified partial report; immutable originals/model/snapshot proofs unchanged",
        "steps": ["Change report metadata/derived state", "Recompute its hash and mutable job report hash", "Independently read report"],
        "expected": "Reject without any model/source retry", "actual": {"error": error.value.code,
            "sealed_cutoff": manifest["cutoff"], "original_state": original["state"], "attempts_unchanged": True,
            "model_posts": len(h.state["model_requests"]), "source_gets": len(h.state["source_calls"])},
    }, ensure_ascii=False, indent=2) + "\n")


def test_local_snapshot_checkpoint_must_equal_the_archive_sealed_manifest(local_topic):
    h = local_topic
    service, job, original, _documents, _manifest = _sealed(h)
    path = service.store.directory(job["job_id"]) / "snapshot-0.json"
    changed = json.loads(path.read_text())
    changed["topic_ref"]["topic_id"] = "TEST_FIXTURE-unrelated-topic"
    path.write_bytes(canonical(changed) + b"\n")
    with pytest.raises(IntelligenceError) as error:
        service.report(job["job_id"])
    assert error.value.code == "HASH_MISMATCH"
    assert len(h.state["model_requests"]) == 2


def test_two_round_chain_keeps_original_order_and_rejects_coherent_reordering(local_topic):
    h = local_topic
    service, job, original, documents, _manifest = _sealed(h, two_rounds=True)
    assert service.report(job["job_id"])["receipt_sha256"] == original["receipt_sha256"]
    assert len(h.state["model_requests"]) == 3
    report = deepcopy(original)
    report["snapshot_ids"].reverse()
    _reseal_mutable_projection(service, job, report, documents, change_job_snapshots=True)
    with pytest.raises(IntelligenceError) as error:
        service.report(job["job_id"])
    assert error.value.code == "HASH_MISMATCH"
    assert len(h.state["model_requests"]) == 3
