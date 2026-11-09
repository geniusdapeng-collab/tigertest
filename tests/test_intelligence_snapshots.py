"""Scoped selections exercise real SQLite transactions and immutable raw bytes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
import time

import pytest

from trading_system.intelligence.archive import Archive
from trading_system.intelligence.contracts import IntelligenceError, digest, source_time
from trading_system.intelligence.planner import build_plan
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from test_intelligence_acquisition import web


NOW = datetime(2026, 11, 6, 1, tzinfo=timezone.utc)


def archive_job(tmp_path, *, tenant="public", clock=None):
    archive = Archive(tmp_path, tenant=tenant, clock=clock or (lambda: NOW))
    registry = SourceRegistry([SourceSpec("web_page", "web_fetch", "", capabilities=("fetch",))])
    plan = build_plan({"run_id": "snapshot-test", "as_of": NOW.isoformat(),
        "universe": {}, "frontier_manifest_hash": "0" * 64, "source_ids": ["web_page"],
        "required_cells": [{"target_id": "topic:semiconductors", "question": "Supply and counterevidence", "required": True}]}, registry)
    job = archive.create_job(plan, "snapshot-test")
    return archive, job["job_id"]


def add(archive, job_id, name, revision=1, *, entity="issuer:A", topic="semiconductors",
        material="issuer_financial", extra=None, text=None):
    text = text or f"Original source {name} revision {revision} complete financial context."
    record = {"id": f"version_{name}_{revision}", "doc_id": "document_" + name,
        "version": digest({"name": name, "revision": revision}), "source_id": "web_page", "source_version": "1",
        "raw_blob": archive.put_blob(("<article>" + text + "</article>").encode(), "text/html"),
        "text_blob": archive.put_blob(text.encode(), "text/plain"), "text": text,
        "times": {"first_seen": source_time(archive.clock().isoformat())}, "material_type": material,
        **(extra or {})}
    return archive.put_document(job_id, record, attempt_id=f"attempt_{name}_{revision}",
        targets=("topic:" + topic,) if topic else (), entities=(entity,) if entity else ())


def test_scoped_snapshot_reads_only_target_despite_unrelated_archive_growth(tmp_path, monkeypatch):
    archive, job = archive_job(tmp_path)
    add(archive, job, "target", 1)
    add(archive, job, "target", 2)
    reads = []
    original = archive.read_blob
    monkeypatch.setattr(archive, "read_blob", lambda ref: (reads.append(ref["sha256"]), original(ref))[1])
    first = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:A"]})
    assert first["state"] == "completed"
    assert len(first["members"]) == 2 and len(reads) == 4
    for index in range(150):
        add(archive, job, f"unrelated{index}", entity="issuer:B", topic="energy")
    reads.clear()
    second = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:A"]})
    assert len(second["members"]) == 2 and len(reads) == 4
    assert second["counts"]["scanned_candidates"] == 2
    assert {doc["id"] for doc in archive.snapshot_documents(first["snapshot_id"])} == {"version_target_1", "version_target_2"}


def test_snapshot_stays_immutable_after_writer_and_late_association(tmp_path):
    instant = [NOW]
    archive, job = archive_job(tmp_path, clock=lambda: instant[0])
    original = add(archive, job, "early", entity="issuer:A")
    add(archive, job, "unrelated", entity="issuer:B")
    frozen = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:A"]})
    instant[0] += timedelta(hours=1)
    unrelated = archive.document("version_unrelated_1")
    archive.put_document(job, unrelated, attempt_id="late_tag", entities=("issuer:A",))
    add(archive, job, "newer", entity="issuer:A")
    assert archive.snapshot(frozen["snapshot_id"]) == frozen
    same_cutoff = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:A"]})
    assert [row["id"] for row in same_cutoff["members"]] == [original["id"]]
    later = archive.create_snapshot(instant[0], scope={"entity_ids": ["issuer:A"]})
    assert len(later["members"]) == 3


def test_preserves_revisions_counterevidence_and_distinguishes_requested_subjects(tmp_path):
    archive, job = archive_job(tmp_path)
    add(archive, job, "financial", 1)
    second = add(archive, job, "financial", 2, extra={"supersedes": "version_financial_1"})
    add(archive, job, "counter", material="counterevidence", extra={"claim_type": "refutes"})
    snapshot = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:A"],
        "required_dimensions": ["issuer_financial", "counterevidence"]})
    assert len(snapshot["members"]) == 3 and snapshot["state"] == "completed"
    assert snapshot["revision_policy"] == "all_eligible_observed_revisions"
    assert all(row["requested_entity_ids"] == ["issuer:A"] and row["validated_subjects"] == [] for row in snapshot["members"])
    latest = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:A"], "include_history": False})
    assert {row["id"] for row in latest["members"]} == {second["id"], "version_counter_1"}


def test_validated_subject_can_scope_without_promoting_requested_entity(tmp_path):
    archive, job = archive_job(tmp_path)
    add(archive, job, "subject", entity="issuer:requested", extra={"validated_subjects": ["issuer:actual"]})
    snapshot = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:actual"]})
    assert len(snapshot["members"]) == 1
    assert snapshot["members"][0]["validated_subjects"] == ["issuer:actual"]
    assert snapshot["members"][0]["requested_entity_ids"] == ["issuer:requested"]


def test_latest_revision_uses_observation_sequence_for_same_time(tmp_path):
    archive, job = archive_job(tmp_path)
    add(archive, job, "same", 9)
    add(archive, job, "same", 10)
    snapshot = archive.create_snapshot(NOW, scope={"include_history": False})
    assert [row["id"] for row in snapshot["members"]] == ["version_same_10"]


@pytest.mark.parametrize("limit,value,reason", [("documents", 1, "SNAPSHOT_DOCUMENT_LIMIT"),
    ("scan_candidates", 1, "SNAPSHOT_SCAN_LIMIT"), ("bytes", 1, "SNAPSHOT_BYTE_LIMIT"),
    ("metadata_bytes", 1, "SNAPSHOT_METADATA_LIMIT")])
def test_limits_return_truthful_partial_without_truncating_originals(tmp_path, limit, value, reason):
    archive, job = archive_job(tmp_path)
    add(archive, job, "one")
    add(archive, job, "two")
    snapshot = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:A"]}, limits={limit: value})
    assert snapshot["state"] == "partial"
    assert reason in {gap["reason"] for gap in snapshot["gaps"]}
    assert archive.snapshot(snapshot["snapshot_id"]) == snapshot


def test_material_period_and_source_scope_are_explicit(tmp_path):
    archive, job = archive_job(tmp_path)
    add(archive, job, "report", extra={"period_start": "2026-01-01", "period_end": "2026-03-31"})
    add(archive, job, "unknown", material="news")
    snapshot = archive.create_snapshot(NOW, scope={"material_types": ["issuer_financial"],
        "source_ids": ["web_page"], "period_start": "2026-01-01", "period_end": "2026-06-30"})
    assert [row["id"] for row in snapshot["members"]] == ["version_report_1"]
    missing = archive.create_snapshot(NOW, scope={"topic_ids": ["semiconductors"], "required_dimensions": ["bank_report"]})
    assert missing["state"] == "partial"
    assert any(row["reason"] == "MATERIAL_DIMENSION_MISSING" for row in missing["gaps"])


def test_tenant_boundary_readonly_and_input_validation(tmp_path):
    archive, job = archive_job(tmp_path)
    add(archive, job, "private")
    snapshot = archive.create_snapshot(NOW, scope={})
    other = Archive(tmp_path, tenant="other", readonly=True)
    with pytest.raises(IntelligenceError, match="tenant"):
        other.snapshot(snapshot["snapshot_id"])
    readonly = Archive(tmp_path, readonly=True)
    assert readonly.snapshot(snapshot["snapshot_id"]) == snapshot
    with pytest.raises(IntelligenceError) as denied:
        readonly.create_snapshot(NOW, scope={})
    assert denied.value.code == "ACCESS_DENIED"
    for scope, limits in [({"url": "https://example.com"}, None), ({}, {"documents": True}), ({}, {"other": 1}),
                          ({"period_start": "tomorrow"}, None)]:
        with pytest.raises(IntelligenceError):
            archive.create_snapshot(NOW, scope=scope, limits=limits)


def test_snapshot_artifact_and_selected_blob_tampering_fails(tmp_path):
    archive, job = archive_job(tmp_path)
    record = add(archive, job, "original")
    snapshot = archive.create_snapshot(NOW, scope={})
    path = tmp_path / "snapshots" / (snapshot["snapshot_id"] + ".json")
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises(IntelligenceError) as error:
        archive.snapshot(snapshot["snapshot_id"])
    assert error.value.code == "HASH_MISMATCH"
    path.write_bytes(original)
    (tmp_path / record["raw_blob"]["path"]).write_bytes(b"tampered")
    with pytest.raises(IntelligenceError) as error:
        archive.snapshot_documents(snapshot["snapshot_id"])
    assert error.value.code == "HASH_MISMATCH"


def test_sqlite_snapshot_records_are_append_only(tmp_path):
    archive, job = archive_job(tmp_path)
    add(archive, job, "original")
    snapshot = archive.create_snapshot(NOW, scope={})
    with archive._session() as connection:
        for table in ("topic_snapshots", "snapshot_scope_index", "snapshot_document_index"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM " + table)
    assert archive.snapshot(snapshot["snapshot_id"]) == snapshot


def test_legacy_index_migration_is_bounded_and_does_not_rewrite_first_seen(tmp_path):
    archive, job = archive_job(tmp_path)
    first = add(archive, job, "old1")
    add(archive, job, "old2")
    with archive._session() as connection:
        for table in ("snapshot_scope_index", "snapshot_document_index"):
            connection.execute("DROP TRIGGER no_delete_" + table)
            connection.execute("DELETE FROM " + table)
        connection.execute("UPDATE snapshot_index_migrations SET state='pending'")
    limited = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:A"]}, limits={"scan_candidates": 1})
    assert limited["state"] == "partial"
    assert "SNAPSHOT_INDEX_INCOMPLETE" in {row["reason"] for row in limited["gaps"]}
    for _ in range(5):
        complete = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:A"]})
        if complete["state"] == "completed":
            break
    assert complete["state"] == "completed" and len(complete["members"]) == 2
    assert archive.snapshot(limited["snapshot_id"]) == limited
    assert archive.document(first["id"])["times"]["first_seen"] == first["times"]["first_seen"]


def test_concurrent_writer_cannot_change_sealed_membership(tmp_path, monkeypatch):
    archive, job = archive_job(tmp_path)
    add(archive, job, "before")
    other = Archive(tmp_path, clock=lambda: NOW)
    started = threading.Event()
    failures = []
    writer = None
    read = archive.read_blob
    def append_later():
        try:
            started.set()
            add(other, job, "concurrent")
        except Exception as error:
            failures.append(type(error).__name__)
    def observe(ref):
        nonlocal writer
        if writer is None:
            writer = threading.Thread(target=append_later)
            writer.start()
            assert started.wait(timeout=2)
        return read(ref)
    monkeypatch.setattr(archive, "read_blob", observe)
    snapshot = archive.create_snapshot(NOW, scope={})
    writer.join(timeout=5)
    assert not writer.is_alive() and not failures
    assert [row["id"] for row in snapshot["members"]] == ["version_before_1"]
    assert archive.snapshot(snapshot["snapshot_id"]) == snapshot
    fresh = archive.create_snapshot(NOW, scope={})
    assert len(fresh["members"]) == 2


def test_database_work_guard_rejects_unbounded_query(tmp_path):
    from trading_system.intelligence.snapshots import _sql_budget
    archive, _ = archive_job(tmp_path)
    with archive._session() as connection:
        with pytest.raises(IntelligenceError) as error:
            with _sql_budget(connection, {"scan_candidates": 1}, time.monotonic() + 30):
                connection.execute("WITH RECURSIVE numbers(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM numbers WHERE x<1000000) SELECT SUM(x) FROM numbers").fetchone()
        assert error.value.code == "BUDGET_EXHAUSTED"
        assert error.value.details["reason"] == "SNAPSHOT_SQL_WORK_LIMIT"
        assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_artifact_publication_failure_keeps_retry_possible(tmp_path, monkeypatch):
    import trading_system.intelligence.snapshots as snapshots
    archive, job = archive_job(tmp_path)
    add(archive, job, "original")
    original = snapshots.os.link
    def disk_error(*args):
        raise OSError("fixture disk failure")
    monkeypatch.setattr(snapshots.os, "link", disk_error)
    with pytest.raises(IntelligenceError) as error:
        archive.create_snapshot(NOW, scope={})
    assert error.value.code == "ARCHIVE_UNAVAILABLE"
    assert not list((tmp_path / "snapshots").iterdir())
    monkeypatch.setattr(snapshots.os, "link", original)
    snapshot = archive.create_snapshot(NOW, scope={})
    assert archive.snapshot(snapshot["snapshot_id"]) == snapshot


def trusted_materials(materials):
    return {"material_types": materials, "material_basis": "registered_source_policy",
        "normalization": {"source_contract_sha256": "a" * 64,
            "discovery": {"parameters": {"context": {"material_type": "page"}}}}}


def test_registered_source_all_materials_are_indexed_and_present_without_subject_promotion(tmp_path):
    archive, job = archive_job(tmp_path)
    types = ["news_events", "industry_data", "competition_technology"]
    original = add(archive, job, "policy", material=None, extra=trusted_materials(types))
    for material in types:
        snapshot = archive.create_snapshot(NOW, scope={"material_types": [material], "required_dimensions": types})
        assert snapshot["state"] == "completed"
        assert [row["id"] for row in snapshot["members"]] == [original["id"]]
        member = snapshot["members"][0]
        assert member["material_types"] == sorted(types)
        assert member["material_basis"] == "registered_source_policy"
        assert member["requested_entity_ids"] == ["issuer:A"] and member["validated_subjects"] == []
        assert not any(row["reason"] == "MATERIAL_DIMENSION_MISSING" for row in snapshot["gaps"])
        assert archive.snapshot(snapshot["snapshot_id"]) == snapshot


def test_query_material_labels_cannot_fill_required_material_dimensions(tmp_path):
    archive, job = archive_job(tmp_path)
    add(archive, job, "query_only", material=None, extra={"material_types": ["formal_research"],
        "material_basis": "query_context", "normalization": {"source_contract_sha256": "a" * 64,
            "discovery": {"parameters": {"context": {"material_type": "news_events"}}}}})
    snapshot = archive.create_snapshot(NOW, scope={"entity_ids": ["issuer:A"],
        "required_dimensions": ["formal_research", "news_events"]})
    assert len(snapshot["members"]) == 1
    assert snapshot["state"] == "partial"
    assert {row["material_type"] for row in snapshot["gaps"] if row["reason"] == "MATERIAL_DIMENSION_MISSING"} == {
        "formal_research", "news_events"}
    assert "material_types" not in snapshot["members"][0]
    assert snapshot["members"][0]["validated_subjects"] == []
    scoped = archive.create_snapshot(NOW, scope={"material_types": ["news_events"]})
    assert scoped["members"] == []
    assert scoped["excluded"][0]["reason"] == "QUERY_MATERIAL_NOT_VERIFIED"


@pytest.mark.parametrize("materials", [[], "news_events", [None], [7], [""], ["bad material"]])
def test_registered_material_classification_requires_a_nonempty_typed_identifier_list(tmp_path, materials):
    archive, job = archive_job(tmp_path)
    with pytest.raises(IntelligenceError) as denied:
        add(archive, job, "invalid", material=None, extra=trusted_materials(materials))
    assert denied.value.code == "CONTRACT_INVALID"
    assert archive.documents(job) == []


@pytest.mark.parametrize("contract_hash", [None, "", "not-a-source-hash", "A" * 64])
def test_registered_material_classification_requires_source_contract_binding(tmp_path, contract_hash):
    archive, job = archive_job(tmp_path)
    record = trusted_materials(["news_events"])
    record["normalization"]["source_contract_sha256"] = contract_hash
    with pytest.raises(IntelligenceError) as denied:
        add(archive, job, "unbound", material=None, extra=record)
    assert denied.value.code == "CONTRACT_INVALID"


def test_legacy_metadata_and_old_snapshot_remain_byte_identical_after_multimaterial_indexing(tmp_path):
    from trading_system.intelligence.contracts import canonical
    from trading_system.intelligence.snapshots import _metadata
    archive, job = archive_job(tmp_path)
    original = add(archive, job, "legacy")
    metadata = _metadata(original)
    assert "material_types" not in metadata
    assert metadata["material_type"] == "issuer_financial" and metadata["material_basis"] == "document_metadata"
    frozen = archive.create_snapshot(NOW, scope={"material_types": ["issuer_financial"],
        "required_dimensions": ["issuer_financial"]})
    path = tmp_path / "snapshots" / (frozen["snapshot_id"] + ".json")
    artifact = path.read_bytes()
    metadata_bytes = canonical(metadata)
    with archive._session() as db:
        indexed = db.execute("SELECT metadata_json FROM snapshot_document_index WHERE document_id=?", (original["id"],)).fetchone()[0]
    assert indexed.encode() == metadata_bytes
    add(archive, job, "new_policy", material=None, extra=trusted_materials(["news_events", "industry_data"]))
    archive.create_snapshot(NOW, scope={"material_types": ["news_events"], "required_dimensions": ["industry_data"]})
    assert canonical(_metadata(archive.document(original["id"]))) == metadata_bytes
    assert archive.snapshot(frozen["snapshot_id"]) == frozen
    assert path.read_bytes() == artifact
    assert canonical(Archive(tmp_path, readonly=True).snapshot(frozen["snapshot_id"])) == artifact


@pytest.mark.parametrize("kind", ["page", "issuer_rss"])
def test_registered_source_to_snapshot_preserves_material_policy_and_independent_raw_replay(tmp_path, web, kind):
    from trading_system.intelligence.service import IntelligenceService
    from trading_system.intelligence.transport import HttpTransport
    from trading_system.intelligence.replay import reconstruct_document
    base, routes, calls = web
    routes["/original"] = (200, "text/html", "<article><h1>Original operations</h1><p>Complete original operating release.</p></article>")
    routes["/feed"] = (200, "application/rss+xml", "<rss><channel><item><title>Original operations</title>"
        "<description>Official discovery description</description><pubDate>Thu, 05 Nov 2026 08:00:00 GMT</pubDate>"
        "<link>" + base + "/original</link></item></channel></rss>")
    types = ["issuer_ir", "competition_technology", "news_events"]
    spec = SourceSpec("registered_original", kind, base + ("/original" if kind == "page" else "/feed"),
        allowed_hosts=("127.0.0.1",), minimum_interval_seconds=0, max_retries=0,
        authority_scopes=("issuer_ir",), options={"material_types": types, "issuer_id": "issuer:A"})
    registry = SourceRegistry([spec])
    service = IntelligenceService(tmp_path, registry,
        registry.build_adapters(transport=HttpTransport(allow_local=True), environment={}), clock=lambda: NOW)
    plan = build_plan({"run_id": "material-source-" + kind, "as_of": NOW.isoformat(), "universe": {},
        "frontier_manifest_hash": "0" * 64, "source_ids": [spec.source_id],
        "required_cells": [{"target_id": "topic:semiconductors", "question": "Original operations", "required": True}]}, registry)
    job = service.create_job(plan, "material-source-" + kind)
    acquired = service.collect(job["job_id"])
    assert len(calls) == (1 if kind == "page" else 2)
    assert any(row["completeness"] == "complete" for row in acquired["documents"])
    for document in acquired["documents"]:
        assert document["material_types"] == types and document["material_basis"] == "registered_source_policy"
        assert document["normalization"]["source_contract_sha256"] == digest(spec.public())
        reconstruct_document(document, service.archive.read_blob, registry.public_manifest())
    snapshot = service.archive.create_snapshot(NOW, scope={"topic_ids": ["semiconductors"],
        "material_types": ["competition_technology"], "required_dimensions": types}, registry_hash=registry.public_manifest()["sha256"])
    assert snapshot["state"] == "completed" and len(snapshot["members"]) == len(acquired["documents"])
    assert all(row["material_types"] == sorted(types) and row["validated_subjects"] == [] for row in snapshot["members"])
    for original in service.archive.snapshot_documents(snapshot["snapshot_id"]):
        reconstruct_document(original, service.archive.read_blob, registry.public_manifest())
    tampered = deepcopy(acquired["documents"][0]); tampered["material_types"] = ["formal_research"]
    with pytest.raises(IntelligenceError) as denied:
        reconstruct_document(tampered, service.archive.read_blob, registry.public_manifest())
    assert denied.value.code == "HASH_MISMATCH"
