"""Bounded, point-in-time raw-evidence selections, separate from live archives.

The immutable artifact is the trust boundary supplied to a research run. Selected
document and association receipts are verified individually against that fixed
artifact. Full archive-chain audit remains available through verify_chain(); it
is deliberately not repeated for every scoped research read.
"""
from __future__ import annotations

from datetime import date
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import time
import uuid

from .contracts import IntelligenceError, canonical, digest, identifier, safe_value, timestamp

SCHEMA = "topic_snapshot/v1"
POLICY = "tiger.scoped-original-selection/2026-11-06.3"
DEFAULT_LIMITS = {"documents": 256, "scan_candidates": 2048, "bytes": 64 * 1024 * 1024,
                  "metadata_bytes": 8 * 1024 * 1024, "deadline_seconds": 30,
                  "associations": 8192}
_DDL = """
CREATE TABLE IF NOT EXISTS snapshot_document_index(
 tenant TEXT NOT NULL, document_id TEXT NOT NULL, doc_id TEXT NOT NULL,
 first_seen_at TEXT NOT NULL, event_sequence INTEGER NOT NULL, event_at TEXT NOT NULL,
 metadata_json TEXT NOT NULL, metadata_sha256 TEXT NOT NULL,
 PRIMARY KEY(tenant,document_id));
CREATE INDEX IF NOT EXISTS snapshot_documents_observed
 ON snapshot_document_index(tenant,first_seen_at,document_id);
CREATE INDEX IF NOT EXISTS snapshot_documents_logical
 ON snapshot_document_index(tenant,doc_id,first_seen_at,document_id);
CREATE TABLE IF NOT EXISTS snapshot_scope_index(
 tenant TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, document_id TEXT NOT NULL,
 event_sequence INTEGER NOT NULL, event_at TEXT NOT NULL,
 PRIMARY KEY(tenant,kind,value,document_id,event_sequence));
CREATE INDEX IF NOT EXISTS snapshot_scope_document
 ON snapshot_scope_index(tenant,document_id,event_sequence,kind,value);
CREATE TABLE IF NOT EXISTS topic_snapshots(
 tenant TEXT NOT NULL, snapshot_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 record_json TEXT NOT NULL, PRIMARY KEY(tenant,snapshot_id));
CREATE TABLE IF NOT EXISTS snapshot_index_migrations(
 tenant TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS snapshot_document_events
 ON events(tenant,kind,json_extract(payload_json,'$.document_id'),sequence);
"""


def initialize_schema(connection, tenant):
    connection.executescript(_DDL)
    existing = connection.execute("SELECT 1 FROM documents WHERE tenant=? LIMIT 1", (tenant,)).fetchone()
    connection.execute("INSERT OR IGNORE INTO snapshot_index_migrations VALUES(?,?)",
                       (tenant, "pending" if existing else "ready"))
    for table in ("snapshot_document_index", "snapshot_scope_index", "topic_snapshots"):
        for verb in ("UPDATE", "DELETE"):
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS no_{verb.lower()}_{table} "
                f"BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'immutable intelligence evidence'); END")


def _event(connection, sequence, tenant, *, kind=None):
    row = connection.execute("SELECT * FROM events WHERE sequence=?", (sequence,)).fetchone()
    if row is None or row["tenant"] != tenant or kind is not None and row["kind"] != kind:
        raise IntelligenceError("HASH_MISMATCH", "Snapshot source event is unavailable")
    value = {"tenant": row["tenant"], "job_id": row["job_id"], "kind": row["kind"],
             "at": row["at"], "payload": json.loads(row["payload_json"]),
             "previous_sha256": row["previous_sha256"]}
    if digest(value) != row["event_sha256"]:
        raise IntelligenceError("HASH_MISMATCH", "Snapshot source event hash differs")
    return {"sequence": sequence, "sha256": row["event_sha256"], **value}


def _strings(value, label):
    if not isinstance(value, (list, tuple)) or len(value) > 1000:
        raise IntelligenceError("CONTRACT_INVALID", "Invalid snapshot " + label)
    return sorted({identifier(x, label) for x in value})


def _metadata(record):
    metadata = record.get("source_metadata", {})
    discovery = record.get("normalization", {}).get("discovery", {})
    context = discovery.get("parameters", {}).get("context", {})
    material = record.get("material_type") or metadata.get("material_type")
    material_basis = "document_metadata" if material else "query_context"
    material = material or context.get("material_type")
    if not isinstance(material, str) or not material:
        material = None
    else:
        try:
            identifier(material, "material type")
        except IntelligenceError:
            material = None
    # Requested query labels organize a search. They never certify the subjects
    # discussed in its returned text or establish eligibility for trade action.
    subjects = record.get("validated_subjects", [])
    if not isinstance(subjects, list):
        subjects = []
    periods = {}
    for field in ("period_start", "period_end"):
        raw = record.get(field) or metadata.get(field)
        try:
            periods[field] = date.fromisoformat(raw).isoformat() if raw else None
        except (TypeError, ValueError):
            periods[field] = None
    result = {"id": record["id"], "doc_id": record["doc_id"], "version": record["version"],
        "document_sha256": digest(record), "raw_blob": record["raw_blob"], "text_blob": record["text_blob"],
        "first_seen_at": timestamp(record["times"]["first_seen"]["value"]),
        "source_id": record.get("source_id", "unknown"), "source_version": record.get("source_version"),
        "material_type": material, "material_basis": material_basis if material else "unknown",
        "validated_subjects": safe_value(subjects),
        **periods,
        "supersedes": record.get("supersedes") or metadata.get("supersedes"),
        "withdraws": record.get("withdraws") or metadata.get("withdraws"),
        "claim_type": record.get("claim_type") or metadata.get("claim_type"),
        "extractor": record.get("extraction_metadata", {}).get("extractor")}
    if "material_types" in record and record.get("material_basis") == "registered_source_policy":
        materials = record.get("material_types")
        contract_hash = record.get("normalization", {}).get("source_contract_sha256")
        if (not isinstance(materials, list) or not materials
                or not isinstance(contract_hash, str) or len(contract_hash) != 64
                or any(character not in "0123456789abcdef" for character in contract_hash)):
            raise IntelligenceError("CONTRACT_INVALID", "Registered materials require a typed list and source contract binding")
        materials = _strings(materials, "registered material types")
        # This is a source-policy classification, not a semantic assessment of
        # the document or a verification of requested entity/query labels.
        result.update(material_type=materials[0], material_types=materials,
            material_basis="registered_source_policy")
    # Omit the optional list for old records. Their exact metadata projection
    # and sealed snapshot bytes must retain the original representation.
    if "source_revision" in record:
        from .archive import _REVISION_POLICY, _revision_metadata
        projection = record["source_revision"]
        comparable = _revision_metadata(record)
        if (not isinstance(projection, dict) or comparable is None
                or projection.get("policy") != _REVISION_POLICY
                or projection.get("representation_sha256") != digest(comparable[0])
                or projection.get("period_basis") != comparable[1]
                or projection.get("period_start") != comparable[0][-2]
                or projection.get("period_end") != comparable[0][-1]):
            raise IntelligenceError("HASH_MISMATCH", "Source-version representation projection differs from its original")
        result["source_revision"] = projection
    return result


def _scope_entry(connection, tenant, kind, value, document_id, event):
    if isinstance(value, str) and value:
        identifier(value, "scope value")
        connection.execute("INSERT OR IGNORE INTO snapshot_scope_index VALUES(?,?,?,?,?,?)",
            (tenant, kind, value, document_id, event["sequence"], event["at"]))


def index_document(connection, tenant, record, event_sequence):
    event = _event(connection, event_sequence, tenant, kind="document_version")
    metadata = _metadata(record)
    if (event["payload"].get("document_id") != record["id"]
            or event["payload"].get("document_sha256") != metadata["document_sha256"]):
        raise IntelligenceError("HASH_MISMATCH", "Document index is not bound to source receipt")
    data = canonical(metadata)
    connection.execute("INSERT OR IGNORE INTO snapshot_document_index VALUES(?,?,?,?,?,?,?,?)",
        (tenant, record["id"], record["doc_id"], metadata["first_seen_at"], event_sequence,
         event["at"], data.decode(), hashlib.sha256(data).hexdigest()))
    _scope_entry(connection, tenant, "source", metadata["source_id"], record["id"], event)
    for material in metadata.get("material_types", [metadata["material_type"]]):
        _scope_entry(connection, tenant, "material", material, record["id"], event)
    for subject in metadata["validated_subjects"]:
        if isinstance(subject, str):
            value = subject
        elif isinstance(subject, dict):
            value = subject.get("entity_id") or subject.get("id")
        else:
            value = None
        _scope_entry(connection, tenant, "validated_subject", value, record["id"], event)


def index_hit(connection, tenant, document_id, event_sequence, targets, entities):
    event = _event(connection, event_sequence, tenant, kind="query_hit")
    if (event["payload"].get("document_id") != document_id
            or event["payload"].get("entities") != list(entities)
            or event["payload"].get("targets") != list(targets)):
        raise IntelligenceError("HASH_MISMATCH", "Association index is not bound to source receipt")
    _scope_entry(connection, tenant, "receipt", str(event_sequence), document_id, event)
    for value in entities:
        _scope_entry(connection, tenant, "requested_entity", value, document_id, event)
    for value in targets:
        _scope_entry(connection, tenant, "target", value, document_id, event)
        if value.startswith(("topic:", "chain:", "sector:")):
            _scope_entry(connection, tenant, "topic", value.split(":", 1)[1], document_id, event)


def _normalize(scope, limits):
    if not isinstance(scope, dict) or safe_value(scope) != scope:
        raise IntelligenceError("CONTRACT_INVALID", "Snapshot scope must be safe metadata")
    allowed = {"entity_ids", "topic_ids", "material_types", "source_ids", "observed_since",
               "period_start", "period_end", "include_history", "required_dimensions"}
    if set(scope) - allowed:
        raise IntelligenceError("CONTRACT_INVALID", "Unknown snapshot scope fields")
    normalized = {key: _strings(scope.get(key, []), key)
        for key in ("entity_ids", "topic_ids", "material_types", "source_ids", "required_dimensions")}
    normalized["observed_since"] = timestamp(scope["observed_since"]) if scope.get("observed_since") else None
    normalized["include_history"] = scope.get("include_history", True)
    if type(normalized["include_history"]) is not bool:
        raise IntelligenceError("CONTRACT_INVALID", "Snapshot history choice must be boolean")
    for key in ("period_start", "period_end"):
        raw = scope.get(key)
        try:
            normalized[key] = date.fromisoformat(raw).isoformat() if raw is not None else None
        except (TypeError, ValueError):
            raise IntelligenceError("CONTRACT_INVALID", "Snapshot period must be an ISO date") from None
    if normalized["period_start"] and normalized["period_end"] and normalized["period_start"] > normalized["period_end"]:
        raise IntelligenceError("CONTRACT_INVALID", "Snapshot period is inverted")
    if limits is not None and not isinstance(limits, dict):
        raise IntelligenceError("CONTRACT_INVALID", "Snapshot limits must be an object")
    aliases = {"max_documents": "documents", "max_candidates": "scan_candidates", "max_bytes": "bytes"}
    supplied = {}
    for key, value in (limits or {}).items():
        key = aliases.get(key, key)
        if key in supplied or key not in DEFAULT_LIMITS:
            raise IntelligenceError("CONTRACT_INVALID", "Unknown or duplicate snapshot limit")
        supplied[key] = value
    limits = {**DEFAULT_LIMITS, **supplied}
    if any(type(value) is not int or value <= 0 for value in limits.values()):
        raise IntelligenceError("CONTRACT_INVALID", "Snapshot limits must be positive integers")
    if (limits["documents"] > 10000 or limits["scan_candidates"] > 100000
            or limits["bytes"] > 1024 ** 3 or limits["metadata_bytes"] > 128 * 1024 ** 2
            or limits["deadline_seconds"] > 3600 or limits["associations"] > 100000):
        raise IntelligenceError("CONTRACT_INVALID", "Snapshot limits exceed trusted implementation bounds")
    return normalized, limits


def _backfill(connection, tenant, limits, deadline):
    """Bounded additive migration; missing projections remain an explicit gap."""
    if connection.execute("SELECT state FROM snapshot_index_migrations WHERE tenant=?", (tenant,)).fetchone()[0] == "ready":
        return False, 0, 0
    used, scanned = 0, 0
    rows = connection.execute("SELECT d.id,d.record_sha256,length(d.record_json) AS record_bytes, "
        "CASE WHEN length(d.record_json)<=? THEN d.record_json ELSE NULL END AS record_json "
        "FROM documents d LEFT JOIN snapshot_document_index i "
        "ON i.tenant=d.tenant AND i.document_id=d.id WHERE d.tenant=? AND i.document_id IS NULL "
        "ORDER BY d.id LIMIT ?", (limits["metadata_bytes"], tenant, limits["scan_candidates"] + 1))
    pending = False
    for row in rows:
        if scanned >= limits["scan_candidates"] or time.monotonic() >= deadline:
            pending = True
            break
        if row["record_json"] is None:
            pending = True
            break
        used += len(row["record_json"].encode())
        if used > limits["metadata_bytes"]:
            pending = True
            break
        scanned += 1
        record = json.loads(row["record_json"])
        if digest(record) != row["record_sha256"]:
            raise IntelligenceError("HASH_MISMATCH", "Legacy document metadata differs")
        event = connection.execute("SELECT sequence FROM events WHERE tenant=? AND kind='document_version' "
            "AND json_extract(payload_json,'$.document_id')=? ORDER BY sequence LIMIT 1", (tenant, row["id"])).fetchone()
        if event is None:
            raise IntelligenceError("HASH_MISMATCH", "Legacy document has no creation receipt")
        index_document(connection, tenant, record, event["sequence"])
    # Legacy associations are projected from their immutable event receipts,
    # never the current aggregation of all hits. Cutoff tests use event time.
    legacy = connection.execute("SELECT e.* FROM events e WHERE e.tenant=? AND e.kind='query_hit' "
        "AND NOT EXISTS(SELECT 1 FROM snapshot_scope_index s WHERE s.tenant=e.tenant "
        "AND s.event_sequence=e.sequence) ORDER BY e.sequence LIMIT ?", (tenant, limits["scan_candidates"] + 1))
    count = 0
    for row in legacy:
        if count >= limits["scan_candidates"] or time.monotonic() >= deadline:
            pending = True
            break
        used += len(row["payload_json"].encode())
        if used > limits["metadata_bytes"]:
            pending = True
            break
        count += 1
        payload = json.loads(row["payload_json"])
        index_hit(connection, tenant, payload["document_id"], row["sequence"],
                  payload.get("targets", []), payload.get("entities", []))
    if not pending:
        connection.execute("UPDATE snapshot_index_migrations SET state='ready' WHERE tenant=?", (tenant,))
    return pending, scanned, count


def _candidate_query(tenant, cutoff, scope, frontier, index_frontier=None):
    args = [tenant, cutoff, cutoff, frontier]
    where = ["d.tenant=?", "d.first_seen_at<=?", "d.event_at<=?", "d.event_sequence<=?"]
    if index_frontier:
        where.append("d.rowid<=?")
        args.append(index_frontier["documents"])
    conditions, condition_args = [], []
    for key, kinds in (("entity_ids", ("requested_entity", "validated_subject")), ("topic_ids", ("topic", "target"))):
        if scope[key]:
            values = scope[key]
            clause = "(kind IN (" + ",".join("?" for _ in kinds) + ") AND value IN (" + ",".join("?" for _ in values) + "))"
            conditions.append(clause)
            condition_args.extend([*kinds, *values])
    if conditions:
        where.append("d.document_id IN (SELECT document_id FROM snapshot_scope_index WHERE tenant=? "
            "AND event_at<=? AND event_sequence<=? AND (" + " OR ".join(conditions) + "))")
        args.extend([tenant, cutoff, frontier, *condition_args])
    for key, kind in (("material_types", "material"), ("source_ids", "source")):
        if scope[key]:
            where.append("d.document_id IN (SELECT document_id FROM snapshot_scope_index WHERE tenant=? "
                "AND kind=? AND value IN (" + ",".join("?" for _ in scope[key]) + ") AND event_at<=? AND event_sequence<=?)")
            args.extend([tenant, kind, *scope[key], cutoff, frontier])
    if scope["observed_since"]:
        where.append("d.first_seen_at>=?")
        args.append(scope["observed_since"])
    return "SELECT d.* FROM snapshot_document_index d WHERE " + " AND ".join(where), args


def _members(connection, archive, cutoff, scope, limits, frontier, deadline, *, load=True, index_frontier=None):
    base, args = _candidate_query(archive.tenant, cutoff, scope, frontier, index_frontier)
    selected, excluded, gaps, scanned, metadata_bytes, raw_bytes, cursor = [], [], [], 0, 0, 0, ""
    association_count = 0
    exhausted = False
    while not exhausted:
        if time.monotonic() >= deadline:
            gaps.append({"reason": "SNAPSHOT_DEADLINE", "required": True})
            break
        page = connection.execute(base + " AND d.document_id>? ORDER BY d.document_id LIMIT ?",
            (*args, cursor, min(128, limits["scan_candidates"] - scanned + 1))).fetchall()
        if not page:
            break
        for row in page:
            if scanned >= limits["scan_candidates"]:
                gaps.append({"reason": "SNAPSHOT_SCAN_LIMIT", "required": True, "remaining_count": "unknown"})
                exhausted = True
                break
            scanned += 1
            cursor = row["document_id"]
            metadata_bytes += len(row["metadata_json"].encode())
            if metadata_bytes > limits["metadata_bytes"] or time.monotonic() >= deadline:
                gaps.append({"reason": "SNAPSHOT_METADATA_LIMIT" if metadata_bytes > limits["metadata_bytes"] else "SNAPSHOT_DEADLINE", "required": True})
                exhausted = True
                break
            metadata = json.loads(row["metadata_json"])
            if digest(metadata) != row["metadata_sha256"]:
                raise IntelligenceError("HASH_MISMATCH", "Snapshot selection index differs")
            receipt = _event(connection, row["event_sequence"], archive.tenant, kind="document_version")
            if (receipt["at"] != row["event_at"] or receipt["payload"].get("document_id") != metadata["id"]
                    or receipt["payload"].get("document_sha256") != metadata["document_sha256"]):
                raise IntelligenceError("HASH_MISMATCH", "Selection metadata is not bound to creation event")
            if scope["material_types"] and metadata["material_basis"] == "query_context":
                excluded.append({"document_id": row["document_id"], "reason": "QUERY_MATERIAL_NOT_VERIFIED"})
                continue
            if not scope["include_history"]:
                # A rolling URL can carry distinct quarterly/annual originals.
                # Latest-only excludes revisions within a known reporting
                # period, never another period's still-current original.
                partition, partition_args = "", []
                if metadata.get("period_start") and metadata.get("period_end"):
                    partition = (" AND json_extract(metadata_json,'$.period_start')=?"
                                 " AND json_extract(metadata_json,'$.period_end')=?")
                    partition_args = [metadata["period_start"], metadata["period_end"]]
                newer = connection.execute("SELECT 1 FROM snapshot_document_index WHERE tenant=? AND doc_id=? "
                    "AND event_at<=? AND first_seen_at<=? AND event_sequence<=? "
                    "AND (first_seen_at>? OR (first_seen_at=? AND event_sequence>?))" + partition + " LIMIT 1",
                    (archive.tenant, row["doc_id"], cutoff, cutoff, frontier, row["first_seen_at"], row["first_seen_at"], row["event_sequence"], *partition_args)).fetchone()
                if newer:
                    excluded.append({"document_id": row["document_id"], "reason": "PRIOR_REVISION_EXPLICITLY_EXCLUDED"})
                    continue
            unknown_period = False
            for field, cmp in (("period_start", "end"), ("period_end", "start")):
                bound = scope.get("period_" + cmp)
                if bound:
                    actual = metadata.get(field)
                    if not actual:
                        unknown_period = True
                    elif field == "period_start" and actual > bound or field == "period_end" and actual < bound:
                        excluded.append({"document_id": row["document_id"], "reason": "OUTSIDE_REPORTING_PERIOD"})
                        break
            else:
                size = metadata["raw_blob"]["bytes"] + metadata["text_blob"]["bytes"]
                if len(selected) >= limits["documents"] or raw_bytes + size > limits["bytes"]:
                    gaps.append({"reason": "SNAPSHOT_DOCUMENT_LIMIT" if len(selected) >= limits["documents"] else "SNAPSHOT_BYTE_LIMIT", "required": True, "next_document_id": row["document_id"], "remaining_count": "unknown"})
                    exhausted = True
                    break
                remaining_associations = limits["associations"] - association_count
                links = connection.execute("SELECT * FROM snapshot_scope_index WHERE tenant=? AND document_id=? "
                    "AND event_at<=? AND event_sequence<=? AND rowid<=? ORDER BY kind,value,event_sequence LIMIT ?",
                    (archive.tenant, row["document_id"], cutoff, frontier,
                     index_frontier["associations"] if index_frontier else 2**63 - 1,
                     remaining_associations + 1)).fetchall()
                if len(links) > remaining_associations:
                    gaps.append({"reason": "SNAPSHOT_ASSOCIATION_LIMIT", "required": True, "document_id": row["document_id"]})
                    exhausted = True
                    break
                requested, topics, associations = set(), set(), []
                for link in links:
                    event = _event(connection, link["event_sequence"], archive.tenant)
                    if event["at"] != link["event_at"]:
                        raise IntelligenceError("HASH_MISMATCH", "Association observation time differs")
                    if event["kind"] == "query_hit":
                        payload = event["payload"]
                        if payload.get("document_id") != row["document_id"]:
                            raise IntelligenceError("HASH_MISMATCH", "Association points at another document")
                        if link["kind"] == "requested_entity":
                            if link["value"] not in payload.get("entities", []):
                                raise IntelligenceError("HASH_MISMATCH", "Requested entity receipt differs")
                            requested.add(link["value"])
                        if link["kind"] in {"topic", "target"}:
                            expected = payload.get("targets", [])
                            if link["value"] not in expected and not any(x.split(":", 1)[-1] == link["value"] for x in expected):
                                raise IntelligenceError("HASH_MISMATCH", "Topic receipt differs")
                            topics.add(link["value"])
                    associations.append({"kind": link["kind"], "value": link["value"], "event": event})
                association_count += len(links)
                metadata_bytes += len(canonical(associations))
                if metadata_bytes > limits["metadata_bytes"]:
                    gaps.append({"reason": "SNAPSHOT_METADATA_LIMIT", "required": True})
                    exhausted = True
                    break
                if load:
                    _load_original(connection, archive, metadata)
                raw_bytes += size
                selected.append({**metadata, "entity_ids": sorted(requested), "requested_entity_ids": sorted(requested),
                    "topic_ids": sorted(topics), "creation_event": receipt, "membership_provenance": associations,
                    "selection_reason": "eligible_observed_revision" if scope["include_history"] else "latest_observed_revision",
                    "period_status": "unknown" if unknown_period else "matched_or_unrestricted"})
                if unknown_period:
                    gaps.append({"reason": "REPORTING_PERIOD_UNKNOWN", "required": True, "document_id": row["document_id"]})
                revision = metadata.get("source_revision", {})
                if revision.get("status") == "unverifiable":
                    gaps.append({"reason": revision["reason"], "required": False, "document_id": row["document_id"]})
        if len(page) < 128:
            exhausted = True
    dimensions = set(scope["required_dimensions"])
    present = {material for row in selected if row["material_basis"] != "query_context"
        for material in row.get("material_types", [row["material_type"]]) if material}
    gaps.extend({"reason": "MATERIAL_DIMENSION_MISSING", "material_type": key, "required": True} for key in sorted(dimensions - present))
    if not selected:
        gaps.append({"reason": "NO_SCOPED_ORIGINALS", "required": True})
    return selected, excluded, gaps, {"selected": len(selected), "scanned_candidates": scanned,
        "selected_raw_text_bytes": raw_bytes, "selection_metadata_bytes": metadata_bytes,
        "excluded_observed": len(excluded), "remaining_candidates": "unknown" if gaps else 0}


def _load_original(connection, archive, metadata):
    row = connection.execute("SELECT record_sha256,length(record_json) AS size FROM documents WHERE tenant=? AND id=?",
                             (archive.tenant, metadata["id"])).fetchone()
    # Check the small hash/size projection before loading or parsing any full
    # record. A malicious enlarged file/record cannot bypass snapshot byte caps.
    maximum = metadata["text_blob"]["bytes"] * 6 + 1024 * 1024
    if row is None or row["record_sha256"] != metadata["document_sha256"] or row["size"] > maximum:
        raise IntelligenceError("HASH_MISMATCH", "Original metadata differs from sealed selection")
    record = archive.document(metadata["id"])
    if _metadata(record) != {key: metadata[key] for key in _metadata(record)}:
        raise IntelligenceError("HASH_MISMATCH", "Original record differs from sealed selection")
    return record


def _write_artifact(archive, snapshot_id, data):
    directory = archive.root / "snapshots"
    temporary = None
    try:
        directory.mkdir(exist_ok=True)
        if directory.is_symlink() or not directory.resolve().is_relative_to(archive.root):
            raise IntelligenceError("ACCESS_DENIED", "Snapshot directory escapes archive")
        path = directory / (snapshot_id + ".json")
        temporary = directory / ("." + snapshot_id + "." + uuid.uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise IntelligenceError("HASH_MISMATCH", "Existing snapshot artifact differs")
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Cannot persist snapshot artifact") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # An orphan temporary name is never a readable snapshot; its
                # complete content cannot replace or poison the final artifact.
                raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Cannot clean snapshot staging artifact") from None


@contextmanager
def _sql_budget(connection, limits, deadline):
    """Bound SQLite VM work as well as returned rows and raw-byte admission.

    LIMIT alone does not bound a temporary sort or an IN subquery. A progress
    handler interrupts that work before it can scan an arbitrary active archive.
    The caller receives an explicit pre-result quota failure, never a silent
    truncation presented as a complete selection.
    """
    progress = {"steps": 0, "reason": None}
    maximum = max(100000, limits["scan_candidates"] * 4000)
    def guard():
        progress["steps"] += 1000
        if time.monotonic() >= deadline:
            progress["reason"] = "SNAPSHOT_DEADLINE"
        elif progress["steps"] > maximum:
            progress["reason"] = "SNAPSHOT_SQL_WORK_LIMIT"
        return 1 if progress["reason"] else 0
    connection.set_progress_handler(guard, 1000)
    try:
        yield
    except sqlite3.OperationalError:
        if progress["reason"]:
            raise IntelligenceError("BUDGET_EXHAUSTED", "Scoped selection exhausted its database work budget",
                details={"reason": progress["reason"], "sql_vm_steps": progress["steps"],
                         "maximum_sql_vm_steps": maximum}) from None
        raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Scoped database selection failed") from None
    finally:
        connection.set_progress_handler(None, 0)


def create_snapshot(archive, as_of, *, scope, limits=None, topic_ref=None, registry_hash=""):
    cutoff = timestamp(as_of)
    scope, limits = _normalize(scope, limits)
    if topic_ref is not None and (not isinstance(topic_ref, dict) or safe_value(topic_ref) != topic_ref):
        raise IntelligenceError("CONTRACT_INVALID", "Invalid topic identity")
    if not isinstance(registry_hash, str) or registry_hash and (len(registry_hash) != 64 or any(c not in "0123456789abcdef" for c in registry_hash)):
        raise IntelligenceError("CONTRACT_INVALID", "Invalid source policy hash")
    deadline = time.monotonic() + limits["deadline_seconds"]
    with archive.transaction() as connection:
        # One short write transaction fixes source frontier, selection and
        # immutable manifest atomically; network/semantic work is never inside.
        with _sql_budget(connection, limits, deadline):
            pending, migrated, migrated_hits = _backfill(connection, archive.tenant, limits, deadline)
            head = archive._head(connection)
            index_frontier = {"documents": connection.execute("SELECT COALESCE(MAX(rowid),0) FROM snapshot_document_index").fetchone()[0],
                              "associations": connection.execute("SELECT COALESCE(MAX(rowid),0) FROM snapshot_scope_index").fetchone()[0]}
            archive_id = connection.execute("SELECT value FROM settings WHERE key='archive_id'").fetchone()[0]
            selected, excluded, gaps, counts = _members(connection, archive, cutoff, scope, limits, head["sequence"], deadline,
                                                       index_frontier=index_frontier)
        if pending:
            gaps.append({"reason": "SNAPSHOT_INDEX_INCOMPLETE", "required": True,
                         "next_action": "resume_bounded_metadata_migration"})
        manifest = {"schema": SCHEMA, "tenant": archive.tenant, "archive_id": archive_id,
            "origin_archive_id": archive_id, "as_of": cutoff, "cutoff": cutoff,
            "topic_ref": topic_ref, "scope": scope, "limits": limits, "registry_hash": registry_hash,
            "fixed_event_frontier": head, "event_head": head, "selection_policy": POLICY,
            "fixed_index_frontier": index_frontier,
            "revision_policy": "all_eligible_observed_revisions" if scope["include_history"] else "explicit_latest_only",
            "integrity_scope": "selected_originals_and_membership_receipts_at_fixed_frontier",
            "members": selected, "excluded": excluded, "gaps": gaps, "counts": counts,
            "state": "partial" if gaps else "completed"}
        manifest["versions"] = [{"id": row["id"], "doc_id": row["doc_id"], "version": row["version"],
            "text_sha256": row["text_blob"]["sha256"], "raw_sha256": row["raw_blob"]["sha256"],
            "first_seen_at": row["first_seen_at"], "source_id": row["source_id"],
            "entity_ids": row["requested_entity_ids"], "document_sha256": row["document_sha256"]} for row in selected]
        sha = digest(manifest)
        snapshot_id = "snapshot_" + sha[:32]
        record = {**manifest, "snapshot_id": snapshot_id, "manifest_sha256": sha}
        data = canonical(record)
        _write_artifact(archive, snapshot_id, data)
        connection.execute("INSERT OR IGNORE INTO topic_snapshots VALUES(?,?,?,?)",
            (archive.tenant, snapshot_id, sha, data.decode()))
    return record


def read_snapshot(archive, snapshot_id, *, documents=False):
    identifier(snapshot_id, "snapshot id")
    with archive._session() as connection:
        try:
            row = connection.execute("SELECT * FROM topic_snapshots WHERE tenant=? AND snapshot_id=?",
                (archive.tenant, snapshot_id)).fetchone()
        except sqlite3.Error:
            raise IntelligenceError("EVIDENCE_NOT_FOUND", "Snapshot namespace is not initialized") from None
        if row is None:
            raise IntelligenceError("EVIDENCE_NOT_FOUND", "Snapshot is not available to this tenant")
        try:
            record = json.loads(row["record_json"])
            manifest = {key: value for key, value in record.items() if key not in {"snapshot_id", "manifest_sha256"}}
            sha = digest(manifest)
            if (record["schema"] != SCHEMA or record["tenant"] != archive.tenant
                    or record["snapshot_id"] != snapshot_id or row["manifest_sha256"] != sha
                    or record["manifest_sha256"] != sha or snapshot_id != "snapshot_" + sha[:32]):
                raise ValueError
            scope, limits = _normalize(record["scope"], record["limits"])
            if scope != record["scope"] or limits != record["limits"]:
                raise ValueError
            path = archive.root / "snapshots" / (snapshot_id + ".json")
            if path.is_symlink() or not path.resolve().is_relative_to(archive.root):
                raise IntelligenceError("ACCESS_DENIED", "Snapshot artifact path escapes archive")
            if path.stat().st_size > 128 * 1024 ** 2 or path.read_bytes() != canonical(record):
                raise ValueError
            archive_id = connection.execute("SELECT value FROM settings WHERE key='archive_id'").fetchone()[0]
            if record["archive_id"] != archive_id or record["origin_archive_id"] != archive_id:
                raise ValueError
            head = record["fixed_event_frontier"]
            if head["sequence"]:
                event = _event(connection, head["sequence"], connection.execute("SELECT tenant FROM events WHERE sequence=?", (head["sequence"],)).fetchone()[0])
                if event["sha256"] != head["sha256"]:
                    raise ValueError
            elif head["sha256"] is not None:
                raise ValueError
            selected = record["members"]
            if (len(selected) > limits["documents"] or len(selected) != record["counts"]["selected"]
                    or len({item["id"] for item in selected}) != len(selected)
                    or record["state"] != ("partial" if record["gaps"] else "completed")):
                raise ValueError
            result = []
            used, associations = 0, 0
            base, args = _candidate_query(archive.tenant, record["cutoff"], scope, head["sequence"], record["fixed_index_frontier"])
            for member in selected:
                used += member["raw_blob"]["bytes"] + member["text_blob"]["bytes"]
                if used > limits["bytes"]:
                    raise ValueError
                associations += len(member["membership_provenance"])
                if associations > limits["associations"]:
                    raise ValueError
                eligible = connection.execute(base + " AND d.document_id=?", (*args, member["id"])).fetchone()
                if eligible is None or digest(json.loads(eligible["metadata_json"])) != eligible["metadata_sha256"]:
                    raise ValueError
                creation = _event(connection, member["creation_event"]["sequence"], archive.tenant, kind="document_version")
                if creation != member["creation_event"] or creation["payload"].get("document_sha256") != member["document_sha256"]:
                    raise ValueError
                for association in member["membership_provenance"]:
                    original = association["event"]
                    if (_event(connection, original["sequence"], archive.tenant) != original
                            or original["sequence"] > head["sequence"] or original["at"] > record["cutoff"]):
                        raise ValueError
                actual = _load_original(connection, archive, member)
                result.append(actual)
        except (KeyError, TypeError, ValueError, OSError, UnicodeError, AttributeError, sqlite3.Error):
            raise IntelligenceError("HASH_MISMATCH", "Snapshot selection or original evidence differs") from None
    return (record, result) if documents else record
