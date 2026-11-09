"""Observed source replacements preserve periods, raw history and import hashes."""
from copy import deepcopy
from datetime import timedelta

import pytest

from trading_system.intelligence.archive import Archive
from trading_system.intelligence.contracts import IntelligenceError, canonical, digest
from test_intelligence_snapshots import NOW, add, archive_job


Q1 = {"period_start": "2026-01-01", "period_end": "2026-03-31"}
Q3 = {"period_start": "2026-07-01", "period_end": "2026-09-30"}
ORIGINAL = {"canonical_url": "https://test-fixture.invalid/rolling", "text_kind": "html_main",
    "completeness": "complete", "normalization": {"kind": "registered_adapter", "operation": "fetch",
        "source_contract_sha256": "a" * 64}, "extraction_metadata": {"extractor": "tiger-main-text/v1"}}


def original(archive, job, revision, *, changes=None, period=None):
    extra = deepcopy(ORIGINAL)
    extra.update(period or {})
    extra.update(changes or {})
    return add(archive, job, "rolling", revision, extra=extra)


def test_latest_only_partitions_rolling_url_by_known_reporting_period(tmp_path):
    clock = [NOW]
    archive, job = archive_job(tmp_path, clock=lambda: clock[0])
    early = original(archive, job, 1, period=Q1)
    clock[0] += timedelta(seconds=1)
    q3 = original(archive, job, 2, period=Q3)
    clock[0] += timedelta(seconds=1)
    q1 = original(archive, job, 3, period=Q1)
    latest = archive.create_snapshot(clock[0], scope={"include_history": False})
    assert {row["id"] for row in latest["members"]} == {q1["id"], q3["id"]}
    q1_only = archive.create_snapshot(clock[0], scope={"include_history": False, **Q1})
    assert [row["id"] for row in q1_only["members"]] == [q1["id"]]
    assert q1_only["state"] == "completed"
    assert q1["supersedes"] == early["id"] and "supersedes" not in q3
    assert q1["supersession"]["period_basis"] == "known"
    assert archive.snapshot_documents(latest["snapshot_id"])


def test_unknown_period_uses_conservative_latest_rule_and_explicit_period_gap(tmp_path):
    archive, job = archive_job(tmp_path)
    first = original(archive, job, 1)
    frozen = archive.create_snapshot(NOW, scope={"include_history": False, **Q1})
    artifact = (tmp_path / "snapshots" / (frozen["snapshot_id"] + ".json")).read_bytes()
    new = original(archive, job, 2)
    latest = archive.create_snapshot(NOW, scope={"include_history": False, **Q1})
    assert [row["id"] for row in latest["members"]] == [new["id"]]
    assert latest["state"] == "partial"
    assert {row["reason"] for row in latest["gaps"]} == {"REPORTING_PERIOD_UNKNOWN"}
    assert new["supersedes"] == first["id"]
    assert new["supersession"]["basis"] == "observed_source_version"
    assert new["supersession"]["period_basis"] == "unknown"
    assert archive.snapshot(frozen["snapshot_id"]) == frozen
    assert (tmp_path / "snapshots" / (frozen["snapshot_id"] + ".json")).read_bytes() == artifact


@pytest.mark.parametrize("before,after,first_period,next_period", [
    ({}, {}, Q1, Q3), ({}, {}, Q1, None), ({}, {}, None, Q1),
    ({}, {"source_version": "2"}, Q1, Q1), ({}, {"source_id": "other"}, Q1, Q1),
    ({}, {"canonical_url": "https://test-fixture.invalid/other"}, Q1, Q1),
    ({}, {"text_kind": "pdf_text"}, Q1, Q1),
    ({"text_kind": "feed", "completeness": "complete"}, {}, None, None),
    ({"text_kind": "snippet", "completeness": "complete"}, {}, None, None),
    ({"completeness": "partial"}, {}, Q1, Q1), ({}, {"completeness": "partial"}, Q1, Q1),
    ({}, {"extraction_metadata": {"extractor": "tiger-scrapling/v1"}}, Q1, Q1),
    ({}, {"extraction_metadata": {"extractor": "tiger-main-text/v1", "extractor_config_sha256": "b" * 64}}, Q1, Q1),
    ({}, {"normalization": {"kind": "registered_adapter", "operation": "fetch", "source_contract_sha256": "b" * 64}}, Q1, Q1),
    ({}, {}, {"period_start": "2026-01-01"}, {"period_start": "2026-01-01"}),
])
def test_different_period_policy_or_representation_never_claims_upstream_replacement(
        tmp_path, before, after, first_period, next_period):
    archive, job = archive_job(tmp_path)
    original(archive, job, 1, changes=before, period=first_period)
    new = original(archive, job, 2, changes=after, period=next_period)
    assert "supersedes" not in new and "supersession" not in new


def test_pre_policy_predecessor_reads_one_small_record_without_any_history_blob(tmp_path, monkeypatch):
    archive, job = archive_job(tmp_path)
    first = original(archive, job, 1)
    legacy = deepcopy(first)
    legacy.pop("source_revision")
    second_archive, second_job = archive_job(tmp_path / "legacy")
    for name in ("raw_blob", "text_blob"):
        second_archive.put_blob(archive.read_blob(legacy[name]), legacy[name]["media_type"])
    second_archive.put_document(second_job, legacy, attempt_id="legacy", preserve_record=True)
    old_bytes = canonical(legacy)
    monkeypatch.setattr(second_archive, "read_blob", lambda _ref: pytest.fail("Historical blobs must not be read"))
    monkeypatch.setattr(second_archive, "document", lambda _id: pytest.fail("Historical document API must not be called"))
    new = original(second_archive, second_job, 2)
    assert new["supersedes"] == legacy["id"]
    with second_archive._session() as db:
        saved = db.execute("SELECT record_json FROM documents WHERE id=?", (legacy["id"],)).fetchone()[0]
    assert saved.encode() == old_bytes


def test_baseline_import_preserves_external_supersedes_and_exact_original_hash(tmp_path):
    origin, job = archive_job(tmp_path / "origin")
    previous = original(origin, job, 1)
    revised = original(origin, job, 2)
    target, target_job = archive_job(tmp_path / "target")
    for name in ("raw_blob", "text_blob"):
        target.put_blob(origin.read_blob(revised[name]), revised[name]["media_type"])
    imported = target.import_document(target_job, revised,
        {"manifest_sha256": "a" * 64, "archive_id": "origin_archive"}, ["issuer:A"])
    assert canonical(imported) == canonical(revised) and digest(imported) == digest(revised)
    assert imported["supersedes"] == previous["id"]
    with pytest.raises(IntelligenceError) as missing:
        target.document(previous["id"])
    assert missing.value.code == "EVIDENCE_NOT_FOUND"
    snapshot = target.create_snapshot(NOW, scope={})
    assert target.snapshot_documents(snapshot["snapshot_id"]) == [revised]
    assert snapshot["members"][0]["document_sha256"] == digest(revised)


def test_oversize_legacy_projection_emits_gap_instead_of_reading_original(tmp_path, monkeypatch):
    source, source_job = archive_job(tmp_path / "source")
    legacy = add(source, source_job, "rolling", 1, text="x" * (1024 * 1024 + 1), extra=deepcopy(ORIGINAL))
    legacy.pop("source_revision")
    archive, job = archive_job(tmp_path / "legacy")
    for name in ("raw_blob", "text_blob"):
        archive.put_blob(source.read_blob(legacy[name]), legacy[name]["media_type"])
    archive.put_document(job, legacy, attempt_id="pre-policy", preserve_record=True)
    # This old record has the same URL/source, but no stored representation
    # projection. Its text byte size is enough to reject a legacy-body load.
    monkeypatch.setattr(archive, "read_blob", lambda _ref: pytest.fail("No historical body read"))
    new = original(archive, job, 2)
    assert "supersedes" not in new
    assert new["source_revision"]["reason"] == "SOURCE_REVISION_METADATA_LIMIT"


def test_new_lineage_lookup_uses_only_small_metadata_and_never_reopens_prior_record(tmp_path, monkeypatch):
    archive, job = archive_job(tmp_path)
    first = original(archive, job, 1)
    statements = []
    connect = archive._connect
    def traced():
        db = connect()
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(archive, "_connect", traced)
    monkeypatch.setattr(archive, "read_blob", lambda _ref: pytest.fail("No historical blob read"))
    second = original(archive, job, 2)
    assert second["supersedes"] == first["id"]
    assert not any("SELECT record_sha256,CASE" in sql for sql in statements)
    assert any("FROM snapshot_document_index" in sql and "LIMIT 1" in sql for sql in statements)


def test_self_hashed_predecessor_projection_cannot_forge_a_matching_representation(tmp_path):
    import json
    from trading_system.intelligence.archive import _revision_metadata
    archive, job = archive_job(tmp_path)
    first = original(archive, job, 1)
    changed = deepcopy(first)
    changed["extraction_metadata"] = {"extractor": "changed-parser"}
    with archive._session() as db:
        row = db.execute("SELECT metadata_json FROM snapshot_document_index WHERE document_id=?", (first["id"],)).fetchone()
        metadata = json.loads(row[0])
        metadata["source_revision"]["representation_sha256"] = digest(_revision_metadata(changed)[0])
        db.execute("DROP TRIGGER no_update_snapshot_document_index")
        db.execute("UPDATE snapshot_document_index SET metadata_json=?,metadata_sha256=? WHERE document_id=?",
            (canonical(metadata).decode(), digest(metadata), first["id"]))
    with pytest.raises(IntelligenceError) as tampered:
        original(archive, job, 2, changes={"extraction_metadata": {"extractor": "changed-parser"}})
    assert tampered.value.code == "HASH_MISMATCH"
    with archive._session() as db:
        assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
