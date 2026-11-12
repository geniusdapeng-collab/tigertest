"""Frozen catalog contracts keep old TEST_FIXTURE research historically stable.

Source/model integration uses actual loopback HTTP. These tests do not call a
paid provider or measure model quality. Current authorization remains separate
from the historical material, author, identity and role qualification contract.
"""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from test_intelligence_topic_safety_integration import (
    SAFE_QUOTE, _analysis, _claim, _deny_read_side_effects, _review, _service, local_topic,
)
from trading_system.intelligence.catalog import SourceCatalog
from trading_system.intelligence.contracts import IntelligenceError, canonical, digest
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.topic import TopicResearch, render_report, topic_coverage
from trading_system.intelligence.topic_cli import execute


def _readonly(service, *, registry=None):
    return TopicResearch(service.root, service.archive_root, mode="readonly",
        registry=registry or service.registry, policy=service.policy)


def _new_config(harness, monkeypatch, value):
    path = harness.root / "TEST_FIXTURE-current-catalog.json"
    path.write_bytes(canonical(value) + b"\n")
    monkeypatch.setenv("TIGER_INTEL_CATALOG_FILE", str(path))


def _reply(harness, *, formal=False, person=None, quote=SAFE_QUOTE):
    def answer(wire, payload):
        if wire["reasoning_effort"] != "high":
            return _review()
        claim = _claim(payload["documents"][0], quote,
            dimension="person_statement" if person else "news_events")
        if person:
            claim["entity_id"] = person
        claims = [claim]
        if formal:
            claims.append(_claim(payload["documents"][0], quote,
                claim_id="TEST_FIXTURE-formal", dimension="formal_research"))
        return _analysis(claims)
    harness.state["reply"] = answer


def _reseal_current_projection(current, job, original):
    documents = current._service(readonly=True).archive.snapshot_documents(original["snapshot_ids"][-1])
    manifest = current._service(readonly=True).archive.snapshot(original["snapshot_ids"][-1])
    changed = deepcopy(original)
    changed["coverage"] = topic_coverage(job["topic"], changed["analysis"], documents,
        current.catalog, as_of=manifest["cutoff"])
    changed["required_gaps"] = list(manifest["gaps"]) + [
        cell for cell in changed["coverage"] if cell["required"] and cell["status"] != "covered"]
    changed["state"] = "partial" if changed["required_gaps"] or not changed["analysis"]["conclusions"] else "completed"
    changed["readable_report"] = render_report(changed, documents)
    changed["receipt_sha256"] = digest({key: value for key, value in changed.items() if key != "receipt_sha256"})
    directory = current.store.directory(job["job_id"])
    (directory / "report.json").write_bytes(canonical(changed) + b"\n")
    # The fixture mutates the ordinary progress projection through its writer;
    # it never changes the immutable admission, original model or archive.
    with current.store.connection() as connection:
        row = connection.execute("SELECT record FROM research_jobs WHERE tenant=? AND job_id=?",
            (current.tenant, job["job_id"])).fetchone()
    projected = json.loads(row[0])
    projected.update(state=changed["state"], report_sha256=changed["receipt_sha256"])
    import sqlite3
    with sqlite3.connect(current.store.database) as connection:
        connection.execute("UPDATE research_jobs SET record=? WHERE tenant=? AND job_id=?",
            (canonical(projected).decode(), current.tenant, job["job_id"]))
    return changed


def _person_catalog(through="2021-01-01T00:00:00Z"):
    return {"entities": {
        "institution:fixture": {"type": "institution", "name": "TEST_FIXTURE Institution"},
        "person:fixture": {"type": "person", "name": "Ada Specialist", "role_history": [{
            "institution_id": "institution:fixture", "role": "TEST_FIXTURE Analyst",
            "valid_from": "2020-01-01T00:00:00Z", "valid_until": through}]}},
        "sources": {"public_original": {"author_ids": ["person:fixture"],
            "person_statement_basis": "original_statement"}}}


@pytest.mark.parametrize("change", ["material", "material_with_full_snapshot", "person_role", "entity_identity"])
def test_new_catalog_cannot_reinterpret_an_old_verified_report(local_topic, monkeypatch, change):
    h = local_topic
    if change == "person_role":
        quote = "TEST_FIXTURE Ada Specialist says NVDA demand remains conditional."
        service, _, _ = _service(h, [("public_original", quote, ["person_statement"], ["topic_analysis"])])
        original_config = _person_catalog()
        service.catalog = SourceCatalog(service.registry, original_config)
        service.monitor.catalog = service.catalog
        policy = {key: {"status": "required" if key == "person_statement" else "not_applicable",
            "reason": "TEST_FIXTURE historical person scope"} for key in service.catalog.dimensions()}
        topic = service.create({"question": "TEST_FIXTURE original person role qualification",
            "entity_ids": ["person:fixture"], "material_policy": policy,
            "idempotency_key": "TEST_FIXTURE-person-topic"})
        job = service.submit({"topic_id": topic["topic_id"], "version": topic["version"],
            "idempotency_key": "TEST_FIXTURE-person-research"})
        _reply(h, person="person:fixture", quote=quote)
        changed_config = _person_catalog("2099-01-01T00:00:00Z")
        dimension = "person_statement"
    else:
        service, topic, job = _service(h, [("public_original", SAFE_QUOTE,
            ["news_events", "formal_research"] if change == "material_with_full_snapshot" else ["news_events"],
            ["topic_analysis"])], formal_required=change in {"material", "material_with_full_snapshot"})
        if change == "material_with_full_snapshot":
            service.catalog = SourceCatalog(service.registry, {"sources": {
                "public_original": {"material_types": ["news_events"]}}})
            service.monitor.catalog = service.catalog
            topic = service.create({"question": topic["question"], "entity_ids": topic["entity_ids"],
                "material_policy": topic["material_policy"], "idempotency_key": "TEST_FIXTURE-restricted-catalog-topic"})
            job = service.submit({"topic_id": topic["topic_id"], "version": topic["version"],
                "idempotency_key": "TEST_FIXTURE-restricted-catalog-job"})
        _reply(h, formal=change in {"material", "material_with_full_snapshot"})
        changed_config = ({"sources": {"public_original": {"material_types": ["news_events", "formal_research"]}}}
            if change in {"material", "material_with_full_snapshot"} else {"entities": {"us:NVDA": {
                "type": "security", "name": "TEST_FIXTURE different company", "identifiers": {"ticker": []}}}})
        dimension = "formal_research" if change in {"material", "material_with_full_snapshot"} else "news_events"
    original = service.run(job["job_id"])
    assert original["state"] in {"partial", "completed"}, original
    before = next(cell for cell in original["coverage"] if cell["dimension_id"] == dimension)
    _new_config(h, monkeypatch, changed_config)
    current = _readonly(service)
    assert current.catalog.manifest()["sha256"] != topic["catalog_sha256"]
    assert current.registry.public_manifest() == service.registry.public_manifest()
    source_calls, model_calls = len(h.state["source_calls"]), len(h.state["model_requests"])
    _deny_read_side_effects(monkeypatch)
    view = current.report_view(job["job_id"])
    assert view["report"] == original
    assert view["current_source_status"]["state"] == "needs_review"
    assert view["current_source_status"]["warnings"][0]["reason"] == "SOURCE_POLICY_CHANGED"
    changed = _reseal_current_projection(current, job, original)
    after = next(cell for cell in changed["coverage"] if cell["dimension_id"] == dimension)
    assert before["status"] != after["status"]  # The new contract would actually reinterpret this evidence.
    if change == "material_with_full_snapshot":
        assert original["state"] == "partial" and changed["state"] == "completed"
    with pytest.raises(IntelligenceError) as error:
        current.report(job["job_id"])
    assert error.value.code == "HASH_MISMATCH"
    assert len(h.state["source_calls"]) == source_calls and len(h.state["model_requests"]) == model_calls


@pytest.mark.parametrize("revocation", ["disabled", "removed", "allowed_uses", "unavailable", "expired"])
def test_frozen_permission_never_overrides_current_source_revocation(local_topic, monkeypatch, revocation):
    h = local_topic
    service, _, job = _service(h, [("public_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"])])
    _reply(h)
    sealed = service.run(job["job_id"])
    assert sealed["state"] == "completed", sealed
    registry = service.registry
    if revocation == "disabled":
        registry = SourceRegistry([replace(registry.get("public_original"), enabled=False)])
    elif revocation == "removed":
        registry = SourceRegistry([SourceSpec("TEST_FIXTURE-different", "page", h.base + "/different",
            allowed_hosts=("127.0.0.1",), minimum_interval_seconds=0)])
    else:
        override = ({"allowed_uses": ["discovery"]} if revocation == "allowed_uses" else
            {"entitlement": {"kind": "unavailable"}} if revocation == "unavailable" else
            {"entitlement": {"kind": "licensed", "reference": "TEST_FIXTURE-expired",
                "valid_until": "2020-01-01T00:00:00Z"}})
        _new_config(h, monkeypatch, {"sources": {"public_original": override}})
    current = _readonly(service, registry=registry)
    _deny_read_side_effects(monkeypatch)
    with pytest.raises(IntelligenceError) as error:
        current.report(job["job_id"])
    assert error.value.code == "POLICY_DENIED"
    assert len(h.state["model_requests"]) == 2 and len(h.state["source_calls"]) == 1


def test_new_catalog_requires_a_new_logical_topic_and_job_without_rebinding_old_admission(local_topic):
    h = local_topic
    service, topic, job = _service(h, [("public_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"])])
    original_admission = (service.store.directory(job["job_id"]) / "admission.json").read_bytes()
    assert job["catalog_manifest"] == service.catalog.manifest()
    assert job["registry_manifest"] == service.registry.public_manifest()
    service.catalog = SourceCatalog(service.registry, {"sources": {"public_original": {
        "material_types": ["news_events", "formal_research"]}}})
    service.monitor.catalog = service.catalog
    with pytest.raises(IntelligenceError) as error:
        service.submit(job["request"])
    assert error.value.code == "PLAN_CONFLICT"
    outcome = service.run(job["job_id"])
    assert outcome["state"] == "failed" and outcome["error"]["code"] == "PLAN_CONFLICT"
    assert not h.state["model_requests"] and not h.state["source_calls"]
    new_topic = service.create({"question": topic["question"], "entity_ids": topic["entity_ids"],
        "material_policy": topic["material_policy"], "horizon": topic["horizon"],
        "idempotency_key": "TEST_FIXTURE-new-catalog-topic"})
    new_job = service.submit({"topic_id": new_topic["topic_id"], "version": new_topic["version"],
        "idempotency_key": "TEST_FIXTURE-new-catalog-job"})
    assert new_topic["topic_id"] != topic["topic_id"] and new_job["job_id"] != job["job_id"]
    assert new_job["catalog_manifest"]["sha256"] == new_topic["catalog_sha256"] == service.catalog.manifest()["sha256"]
    assert service.store.job(job["job_id"])["catalog_manifest"] == job["catalog_manifest"]
    assert (service.store.directory(job["job_id"]) / "admission.json").read_bytes() == original_admission


@pytest.mark.parametrize("field", ["catalog_manifest", "registry_manifest"])
def test_legacy_job_without_frozen_contract_requires_original_release_for_status_and_report(local_topic, monkeypatch, field):
    h = local_topic
    service, _, job = _service(h, [("public_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"])])
    _reply(h)
    assert service.run(job["job_id"])["state"] == "completed"
    current = _readonly(service)
    status = execute("status", {"job_id": job["job_id"]}, state_root=service.root,
        archive_root=service.archive_root, service_factory=lambda *_args, **_kwargs: current)
    assert status["operation_ok"] and status["data"]["state"] == "completed"
    changed = service.store.job(job["job_id"])
    changed.pop(field)
    with service.store.connection() as connection:
        connection.execute("UPDATE research_jobs SET record=? WHERE tenant=? AND job_id=?",
            (canonical(changed).decode(), service.tenant, job["job_id"]))
    _deny_read_side_effects(monkeypatch)
    for action in (lambda: current.report(job["job_id"]), lambda: execute("status",
            {"job_id": job["job_id"]}, state_root=service.root, archive_root=service.archive_root,
            service_factory=lambda *_args, **_kwargs: current)):
        with pytest.raises(IntelligenceError) as error:
            action()
        assert error.value.code == "LEGACY_JOB_REQUIRES_MATCHING_RELEASE"
    assert len(h.state["model_requests"]) == 2


def test_mutable_job_cannot_change_the_admission_catalog_and_every_snapshot_binds_its_registry(local_topic):
    h = local_topic
    service, _, job = _service(h, [("public_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"])])
    changed = deepcopy(job["catalog_manifest"])
    changed["sources"]["public_original"]["material_types"].append("formal_research")
    changed["sha256"] = digest({key: value for key, value in changed.items() if key != "sha256"})
    with pytest.raises(IntelligenceError) as error:
        # Ordinary progress writes cannot replace immutable admission content.
        service.store.update(job["job_id"], catalog_manifest=changed)
        service.store.job(job["job_id"])
    assert error.value.code == "HASH_MISMATCH"
    # Restore only the fixture's mutable row to test an independently sealed
    # snapshot under a different registry, while retaining the original model.
    with service.store.connection() as connection:
        connection.execute("UPDATE research_jobs SET record=? WHERE tenant=? AND job_id=?",
            (canonical(job).decode(), service.tenant, job["job_id"]))
    _reply(h)
    original = service.run(job["job_id"])
    assert original["state"] == "completed", original
    archive = service._service().archive
    sealed = archive.snapshot(original["snapshot_ids"][-1])
    other = archive.create_snapshot(sealed["cutoff"], scope=sealed["scope"], limits=sealed["limits"],
        topic_ref=sealed["topic_ref"], registry_hash="0" * 64)
    directory = service.store.directory(job["job_id"])
    (directory / "snapshot-0.json").write_bytes(canonical(other) + b"\n")
    changed_report = deepcopy(original)
    changed_report["snapshot_ids"] = [other["snapshot_id"]]
    changed_report["receipt_sha256"] = digest({key: value for key, value in changed_report.items() if key != "receipt_sha256"})
    (directory / "report.json").write_bytes(canonical(changed_report) + b"\n")
    service.store.update(job["job_id"], snapshot_ids=changed_report["snapshot_ids"],
        report_sha256=changed_report["receipt_sha256"])
    with pytest.raises(IntelligenceError) as error:
        service.report(job["job_id"])
    assert error.value.code == "HASH_MISMATCH"
    assert len(h.state["model_requests"]) == 2


@pytest.mark.parametrize("fault", ["catalog_topic", "registry_catalog"])
def test_individually_valid_frozen_manifests_must_keep_the_original_cross_bindings(local_topic, fault):
    h = local_topic
    service, _, job = _service(h, [("public_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"])])
    changed = deepcopy(job)
    if fault == "catalog_topic":
        manifest = changed["catalog_manifest"]
        manifest["sources"]["public_original"]["material_types"].append("formal_research")
    else:
        manifest = changed["registry_manifest"]
        manifest["policy_version"] = "TEST_FIXTURE-other-policy-version"
    manifest["sha256"] = digest({key: value for key, value in manifest.items() if key != "sha256"})
    # Even matching local projections of individually valid manifests cannot
    # sever catalog->admitted topic or catalog->original registry linkage.
    (service.store.directory(job["job_id"]) / "admission.json").write_bytes(canonical(changed) + b"\n")
    with service.store.connection() as connection:
        connection.execute("UPDATE research_jobs SET record=? WHERE tenant=? AND job_id=?",
            (canonical(changed).decode(), service.tenant, job["job_id"]))
    with pytest.raises(IntelligenceError) as error:
        service.store.job(job["job_id"])
    assert error.value.code == "HASH_MISMATCH"
    assert not h.state["source_calls"] and not h.state["model_requests"]
