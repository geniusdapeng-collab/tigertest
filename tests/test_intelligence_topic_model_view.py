"""Versioned model context with real loopback originals, transport and receipts."""
from copy import deepcopy
import json

import pytest

from trading_system.intelligence import topic as module
from trading_system.intelligence.catalog import DIMENSIONS
from trading_system.intelligence.contracts import IntelligenceError, canonical, digest
from trading_system.intelligence.model_snapshot_view import CONTRACT, model_snapshot_view
from trading_system.llm.deepseek_required import sanitize_model_value
from trading_system.llm.model_budget import count_json_tokens, plan_request
from test_intelligence_topic_safety_integration import (
    SAFE_QUOTE, _analysis, _claim, _payload, _review, _service, local_topic,
)


SECOND = "TEST_FIXTURE NVDA has a separate complete original with contrary observations."
NBSP = "TEST_FIXTURE NVDA conference call begins at 2 p.m.\u00a0PT."


def _ordinary(h):
    service, _, job = _service(h, [
        ("first_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"]),
        ("second_original", SECOND, ["news_events"], ["topic_analysis"]),
    ])
    h.state["reply"] = lambda wire, payload: (_analysis([_claim(
        next(d for d in payload["documents"] if d["source_id"] == "first_original"), SAFE_QUOTE)])
        if wire["reasoning_effort"] == "high" else _review())
    return service, job


def _combined(h):
    service, _, job = _service(h, [
        ("first_original", NBSP, ["news_events"], ["topic_analysis"]),
        ("second_original", SECOND, ["news_events"], ["topic_analysis"]),
    ])

    def reply(wire, payload):
        if wire["reasoning_effort"] != "high":
            assert payload["report"]["conclusions"][0]["supports"][0]["quote"] == NBSP
            assert payload["citation_resolutions"]
            return _review()
        documents = {d["source_id"]: d for d in payload["documents"]}
        claim = _claim(documents["first_original"], NBSP.replace("\u00a0", " "))
        claim["supports"].append({"document_id": documents[
            "second_original" if "previous_analysis" in payload else "first_original"]["id"], "quote": SECOND})
        return _analysis([claim])

    h.state["reply"] = reply
    return service, job


def test_full_original_objects_and_twelve_dimensions_keep_complete_archive_proof(local_topic):
    h = local_topic
    service, first = _ordinary(h)
    topic = service.create({"question": "TEST_FIXTURE all twelve dimensions retain their gaps",
        "entity_ids": ["us:NVDA"], "material_policy": {key: "required" for key in DIMENSIONS},
        "idempotency_key": "all-twelve-view-topic"})
    job = service.submit({"topic_id": topic["topic_id"], "version": topic["version"],
        "idempotency_key": "all-twelve-view-job"})
    report = service.run(job["job_id"])
    assert report["state"] == "partial" and report["required_gaps"], report
    manifest = service._service(readonly=True).archive.snapshot(report["snapshot_ids"][0])
    documents = service._service(readonly=True).archive.snapshot_documents(manifest["snapshot_id"])
    before = canonical(manifest)
    view = model_snapshot_view(manifest, CONTRACT)
    assert canonical(manifest) == before and view["full_manifest_sha256"] == digest(manifest)
    assert view["manifest_sha256"] == manifest["manifest_sha256"]
    assert not {"members", "versions", "excluded", "event_head", "fixed_event_frontier", "fixed_index_frontier"}.intersection(view)
    assert manifest["members"] and all(member["creation_event"] and member["membership_provenance"] for member in manifest["members"])
    assert len(topic["material_policy"]) == len(DIMENSIONS) == 12
    assert report["topic"]["material_policy"] == topic["material_policy"]
    assert job["policy"]["model_snapshot_view_contract"] == CONTRACT
    for wire in h.state["model_requests"]:
        payload = _payload(wire)
        assert payload["snapshot"] == sanitize_model_value(view)
        assert canonical(payload["documents"]) == canonical(sanitize_model_value(documents))
        assert payload["snapshot"]["gaps"] == manifest["gaps"]
        assert payload["snapshot"]["counts"] == manifest["counts"]
        assert wire["max_tokens"] == 393216 and wire["model"] == "deepseek-flash"
    assert [w["reasoning_effort"] for w in h.state["model_requests"]] == ["high", "max"]
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]
    assert canonical(service._service(readonly=True).archive.snapshot(manifest["snapshot_id"])) == before
    assert service.store.job(first["job_id"])["state"] == "accepted"


@pytest.mark.parametrize("contract", ["unknown", "", True, 1, [], {}])
def test_unknown_model_view_contract_cannot_admit_a_job(local_topic, contract):
    with pytest.raises(IntelligenceError) as denied:
        module.trusted_policy_value({**module.DEFAULT_POLICY, "model_snapshot_view_contract": contract})
    assert denied.value.code == "INVALID_STARTUP"
    assert not local_topic.state["source_calls"] and not local_topic.state["model_requests"]


def test_8111_policy_hash_and_legacy_snapshot_object_are_byte_exact():
    legacy = {k: v for k, v in module.DEFAULT_POLICY.items()
        if k not in {"model_snapshot_view_contract", "model_transport_contract"}}
    legacy["version"] = "2026-11-07.2"
    parsed = module.trusted_policy_value(legacy)
    assert "model_snapshot_view_contract" not in parsed
    assert digest(parsed) == "470c534ce99984ac461f79ee8aaf14dd6aab4ebe4abf1f7549d87113a0385350"
    original = {"schema": "TEST_FIXTURE legacy complete manifest", "members": [{"preserve": "all bytes"}]}
    assert model_snapshot_view(original) is original
    assert model_snapshot_view(original, None) is original
    assert canonical(model_snapshot_view(original)) == canonical(original)


@pytest.mark.parametrize("legacy", ["missing", None])
def test_actual_legacy_admission_keeps_full_snapshot_requests_and_response_replay(local_topic, monkeypatch, legacy):
    with monkeypatch.context() as old:
        if legacy == "missing":
            old.delitem(module.DEFAULT_POLICY, "model_snapshot_view_contract")
        else:
            old.setitem(module.DEFAULT_POLICY, "model_snapshot_view_contract", None)
        service, job = _ordinary(local_topic)
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    manifest = service._service(readonly=True).archive.snapshot(report["snapshot_ids"][0])
    assert manifest["members"] and "versions" in manifest
    for wire in local_topic.state["model_requests"]:
        assert canonical(_payload(wire)["snapshot"]) == canonical(sanitize_model_value(manifest))
    directory = service.store.directory(job["job_id"])
    before = {p.name: p.read_bytes() for p in directory.glob("response-*.json")}
    assert service.run(job["job_id"]) == report and len(local_topic.state["model_requests"]) == 2
    assert before == {p.name: p.read_bytes() for p in directory.glob("response-*.json")}


def test_v2_feedback_and_nbsp_derivative_bind_same_view_and_full_manifest(local_topic):
    service, job = _combined(local_topic)
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    payloads = [_payload(w) for w in local_topic.state["model_requests"]]
    assert len(payloads) == 3 and payloads[0]["documents"] == payloads[1]["documents"] == payloads[2]["documents"]
    assert payloads[0]["snapshot"] == payloads[1]["snapshot"] == payloads[2]["snapshot"]
    directory = service.store.directory(job["job_id"])
    marker = json.loads((directory / "citation-repair.json").read_text())
    manifest = service._service(readonly=True).archive.snapshot(report["snapshot_ids"][0])
    assert marker["schema"] == "tiger.topic-citation-repair/v2"
    assert marker["model_snapshot_view_contract"] == CONTRACT
    assert marker["model_snapshot_view_sha256"] == digest(model_snapshot_view(manifest, CONTRACT))
    assert marker["snapshot_sha256"] == digest(manifest)
    resolution = report["citation_resolutions"][0]
    assert resolution["snapshot_sha256"] == digest(manifest)
    assert resolution["request_sha256"] == service.store.attempts(job["job_id"])[1]["request_sha"]
    assert payloads[1]["previous_analysis"]["conclusions"][0]["supports"][1]["document_id"] != report["analysis"]["conclusions"][0]["supports"][1]["document_id"]
    assert report["analysis"]["conclusions"][0]["supports"][0]["quote"] == NBSP
    assert service.report(job["job_id"]) == report
    marker["model_snapshot_view_sha256"] = "0" * 64
    marker["binding_sha256"] = digest({k: v for k, v in marker.items() if k != "binding_sha256"})
    (directory / "citation-repair.json").write_bytes(canonical(marker) + b"\n")
    with pytest.raises(IntelligenceError) as denied:
        service.report(job["job_id"])
    assert denied.value.code == "HASH_MISMATCH" and len(local_topic.state["model_requests"]) == 3


def test_compact_view_feedback_recovery_reuses_original_requests_and_checkpoints(local_topic, monkeypatch):
    service, job = _combined(local_topic)
    write = module.atomic_json

    def interrupt(root, name, value, **kwargs):
        if name.startswith("io-") and "previous_analysis" in value["inputs"]["request"]["messages"][1]["content"]:
            raise KeyboardInterrupt
        return write(root, name, value, **kwargs)

    monkeypatch.setattr(module, "atomic_json", interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.run(job["job_id"])
    original = service.store.attempts(job["job_id"])
    assert len(original) == len(local_topic.state["model_requests"]) == 2
    directory = service.store.directory(job["job_id"])
    before = {p.name: p.read_bytes() for p in directory.glob("response-*.json")}
    monkeypatch.setattr(module, "atomic_json", write)
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    assert len(local_topic.state["model_requests"]) == 3
    assert service.store.attempts(job["job_id"])[:2] == original
    assert all((directory / name).read_bytes() == data for name, data in before.items())
    assert service.report(job["job_id"]) == report


def _rewrite_mutable_request_chain(service, job, wire, mutate):
    """Coherently rehash all mutable request/checkpoint/report layers in a fixture.

    Real model responses, immutable source evidence and original event rows
    remain untouched. A saved view or a matching checksum must not suffice.
    """
    directory = service.store.directory(job["job_id"])
    old_sha = digest(wire)
    path, saved = next((p, json.loads(p.read_text())) for p in directory.glob("io-*.json")
        if json.loads(p.read_text())["result"]["request_sha256"] == old_sha)
    payload = _payload(wire)
    modified = deepcopy(payload)
    mutate(modified)
    request = deepcopy(wire)
    request["messages"][1]["content"] = request["messages"][1]["content"].replace(
        canonical(payload).decode(), canonical(modified).decode(), 1)
    request, tokens, ceiling = plan_request({k: v for k, v in request.items() if k != "max_tokens"})
    new_sha = digest(request)
    assert new_sha != old_sha and count_json_tokens(request) == tokens
    inputs, proof = saved["inputs"], saved["result"]
    inputs.update(request=request, request_sha256=new_sha, input_token_estimate=tokens, output_ceiling=ceiling)
    proof.update(request_sha256=new_sha, input_token_estimate=tokens, output_ceiling=ceiling)
    attempt = next(a for a in service.store.attempts(job["job_id"]) if a["request_sha"] == old_sha)
    checkpoint_path = directory / attempt["receipt"]["response_ref"]
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["request_sha256"] = new_sha
    checkpoint["request_contract"].update(input_tokens=tokens, output_ceiling=ceiling)
    checkpoint["receipt"].update(request_sha256=new_sha, input_tokens=tokens, output_ceiling=ceiling)
    checkpoint["checkpoint_sha256"] = digest({k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"})
    checkpoint_path.write_bytes(canonical(checkpoint) + b"\n")
    receipt = {**checkpoint["receipt"], "checkpoint_sha256": checkpoint["checkpoint_sha256"]}
    with service.store.connection() as db:
        db.execute("UPDATE topic_attempts SET request_sha=?,hold=?,receipt=? WHERE attempt_id=?",
            (new_sha, tokens + ceiling, canonical(receipt).decode(), attempt["attempt_id"]))
    saved["identity"] = digest({"name": "model.deepseek.chat", "stage": proof["stage"],
        "request_sha256": new_sha, "query_sha256": None})
    saved["result_sha256"] = digest(proof)
    path.unlink()
    (directory / ("io-" + saved["identity"] + ".json")).write_bytes(canonical(saved) + b"\n")
    report = json.loads((directory / "report.json").read_text())
    report["model_attempts"] = service.store.attempts(job["job_id"])
    report["receipt_sha256"] = digest({k: v for k, v in report.items() if k != "receipt_sha256"})
    (directory / "report.json").write_bytes(canonical(report) + b"\n")
    service.store.update(job["job_id"], report_sha256=report["receipt_sha256"])


@pytest.mark.parametrize("stage", ["high", "max"])
@pytest.mark.parametrize("fault", ["manifest_digest", "record_digest", "scope", "counts", "full_manifest_instead",
    "added_proof_flag", "dropped_document", "hidden_document", "document_metadata"])
def test_coherent_request_and_checkpoint_rehash_cannot_fake_view_or_drop_originals(local_topic, stage, fault):
    service, job = _ordinary(local_topic)
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    wire = next(w for w in local_topic.state["model_requests"] if w["reasoning_effort"] == stage)
    manifest = service._service(readonly=True).archive.snapshot(report["snapshot_ids"][0])

    def mutate(payload):
        if fault == "manifest_digest": payload["snapshot"]["manifest_sha256"] = "0" * 64
        elif fault == "record_digest": payload["snapshot"]["full_manifest_sha256"] = "0" * 64
        elif fault == "scope": payload["snapshot"]["scope"]["required_dimensions"] = []
        elif fault == "counts": payload["snapshot"]["counts"]["selected"] -= 1
        elif fault == "full_manifest_instead": payload["snapshot"] = sanitize_model_value(manifest)
        elif fault == "added_proof_flag": payload["snapshot"]["verified"] = True
        elif fault in {"dropped_document", "hidden_document"}:
            removed = next(d for d in payload["documents"] if d["source_id"] == "second_original")
            payload["documents"].remove(removed)
            if fault == "hidden_document": payload["not_original_evidence"] = removed
        else:
            payload["documents"][0]["extraction_metadata"]["TEST_FIXTURE_forged"] = True

    _rewrite_mutable_request_chain(service, job, wire, mutate)
    with pytest.raises(IntelligenceError) as denied:
        service.report(job["job_id"])
    assert denied.value.code in {"HASH_MISMATCH", "MODEL_RECEIPT_REQUIRED"}
    assert len(local_topic.state["model_requests"]) == 2


def test_model_view_never_substitutes_for_full_archived_original_verification(local_topic):
    service, job = _ordinary(local_topic)
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    archive = service._service(readonly=True).archive
    manifest = archive.snapshot(report["snapshot_ids"][0])
    view = model_snapshot_view(manifest, CONTRACT)
    document = archive.snapshot_documents(manifest["snapshot_id"])[0]
    (service.archive_root / document["raw_blob"]["path"]).write_bytes(b"TEST_FIXTURE changed original bytes")
    assert model_snapshot_view(manifest, CONTRACT) == view
    with pytest.raises(IntelligenceError) as denied:
        service.report(job["job_id"])
    assert denied.value.code == "HASH_MISMATCH" and len(local_topic.state["model_requests"]) == 2
