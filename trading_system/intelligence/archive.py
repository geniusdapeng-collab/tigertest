"""SQLite projections plus immutable content-addressed bytes and hash-chained events."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .contracts import (BUNDLE_SCHEMA, IntelligenceError, aware, canonical, digest,
                        identifier, timestamp, utc_now)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 job_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, run_id TEXT NOT NULL,
 replay_key TEXT NOT NULL, plan_hash TEXT NOT NULL, plan_json TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(tenant,replay_key));
CREATE TABLE IF NOT EXISTS events(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, job_id TEXT NOT NULL,
 kind TEXT NOT NULL, at TEXT NOT NULL, payload_json TEXT NOT NULL,
 previous_sha256 TEXT, event_sha256 TEXT NOT NULL UNIQUE);
CREATE INDEX IF NOT EXISTS events_job ON events(tenant,job_id,sequence);
CREATE TABLE IF NOT EXISTS attempts(
 attempt_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, job_id TEXT NOT NULL,
 source_id TEXT NOT NULL, operation TEXT NOT NULL, request_sha256 TEXT NOT NULL,
 request_json TEXT NOT NULL, started_at TEXT NOT NULL, ordinal INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_job ON attempts(tenant,job_id,source_id);
CREATE TABLE IF NOT EXISTS attempt_results(
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), status TEXT NOT NULL,
 completed_at TEXT NOT NULL, result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
 tenant TEXT NOT NULL, id TEXT NOT NULL, doc_id TEXT NOT NULL, version TEXT NOT NULL,
 record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,doc_id,version));
CREATE INDEX IF NOT EXISTS documents_first_seen ON documents(tenant,first_seen_at);
CREATE TABLE IF NOT EXISTS hits(
 hit_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 document_id TEXT NOT NULL, rank INTEGER NOT NULL, targets_json TEXT NOT NULL,
 entities_json TEXT NOT NULL, hit_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS hits_job ON hits(tenant,job_id,document_id);
CREATE TABLE IF NOT EXISTS assessments(
 assessment_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, job_id TEXT NOT NULL,
 at TEXT NOT NULL, claims_json TEXT NOT NULL, coverage_json TEXT NOT NULL,
 event_sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bundles(
 bundle_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, job_id TEXT NOT NULL,
 manifest_sha256 TEXT NOT NULL, record_json TEXT NOT NULL, UNIQUE(tenant,job_id));
"""
_IMMUTABLE = ("events", "attempts", "attempt_results", "documents", "hits", "assessments", "bundles")

# New records carry this small projection in the existing scoped metadata index.
# Old projections and their sealed snapshot hashes remain byte-identical. A
# legacy predecessor may be read once under an explicit metadata size/work cap;
# there is no startup migration or full-record expression index.
_REVISION_PATHS = (
    "canonical_url", "source_id", "source_version", "completeness", "text_kind", "raw_blob.media_type",
    "normalization.kind", "normalization.operation", "normalization.source_contract_sha256",
    "extraction_metadata.extractor", "extraction_metadata.extractor_config_sha256",
    "extraction_metadata.parser", "extraction_metadata.parse_policy",
)
_REVISION_POLICY = "tiger.observed-source-version/2026-11-06.1"
_REVISION_METADATA_LIMIT = 1024 * 1024


def _revision_metadata(record):
    """Comparable complete representations; unknown periods stay explicitly so."""
    kind = record.get("text_kind")
    if (record.get("completeness") != "complete" or not isinstance(kind, str) or not kind
            or kind.startswith("feed") or "snippet" in kind or kind in {"filing_index", "provider_markdown", "attachment"}
            or not all(isinstance(record.get(key), str) and record[key]
                       for key in ("canonical_url", "source_id", "source_version"))):
        return None
    values = []
    for path in _REVISION_PATHS:
        value = record
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        values.append(canonical(value).decode() if isinstance(value, (dict, list)) else value)
    metadata = record.get("source_metadata", {})
    periods = [record.get(field) or metadata.get(field) for field in ("period_start", "period_end")]
    if all(value is None or value == "" for value in periods):
        return values + [None, None], "unknown"
    try:
        normalized = [date.fromisoformat(value).isoformat() for value in periods]
    except (TypeError, ValueError):
        return None
    if normalized != periods or normalized[0] > normalized[1]:
        return None
    return values + normalized, "known"


class Archive:
    def __init__(self, root, *, tenant="public", clock=utc_now, readonly=False):
        self.root = Path(root).resolve()
        self.tenant = identifier(tenant, "tenant")
        self.clock, self.readonly = clock, readonly
        self.database = self.root / "intelligence.sqlite3"
        if readonly:
            if not self.database.is_file():
                raise IntelligenceError("EVIDENCE_NOT_FOUND", "Acquisition archive does not exist")
        else:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                (self.root / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
                (self.root / "bundles").mkdir(exist_ok=True)
                with self._session() as connection:
                    connection.executescript(_SCHEMA)
                    from .snapshots import initialize_schema
                    initialize_schema(connection, self.tenant)
                    for table in _IMMUTABLE:
                        for verb in ("UPDATE", "DELETE"):
                            connection.execute(f"CREATE TRIGGER IF NOT EXISTS no_{verb.lower()}_{table} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'immutable intelligence evidence'); END")
                    connection.execute("INSERT OR IGNORE INTO settings VALUES('archive_id',?)", ("archive_" + uuid.uuid4().hex,))
                    connection.execute("INSERT OR IGNORE INTO settings VALUES('schema','tiger.intelligence-archive/v1')")
            except (OSError, sqlite3.Error):
                raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Cannot initialize intelligence archive") from None

    def _connect(self):
        try:
            if self.readonly:
                connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=20)
            else:
                connection = sqlite3.connect(self.database, timeout=20)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
            connection.row_factory = sqlite3.Row
            return connection
        except sqlite3.Error:
            raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Cannot open intelligence archive") from None

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        if self.readonly:
            raise IntelligenceError("ACCESS_DENIED", "Archive is read only")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except IntelligenceError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Intelligence transaction failed") from None
        finally:
            connection.close()

    def _job(self, connection, job_id):
        row = connection.execute("SELECT * FROM jobs WHERE job_id=? AND tenant=?", (job_id, self.tenant)).fetchone()
        if row is None:
            raise IntelligenceError("EVIDENCE_NOT_FOUND", "Job is not available to this tenant")
        try:
            plan = json.loads(row["plan_json"])
            if (plan.get("plan_hash") != row["plan_hash"] or plan.get("run_id") != row["run_id"]
                    or digest({key: value for key, value in plan.items() if key != "plan_hash"}) != row["plan_hash"]):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise IntelligenceError("HASH_MISMATCH", "Frozen acquisition plan differs from its recorded hash") from None
        return dict(row)

    def _head(self, connection):
        row = connection.execute("SELECT sequence,event_sha256 FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
        return {"sequence": row["sequence"], "sha256": row["event_sha256"]} if row else {"sequence": 0, "sha256": None}

    def _event(self, connection, job_id, kind, payload):
        head = self._head(connection)
        value = {"tenant": self.tenant, "job_id": job_id, "kind": kind,
                 "at": timestamp(self.clock()), "payload": payload, "previous_sha256": head["sha256"]}
        event_hash = digest(value)
        cursor = connection.execute("INSERT INTO events(tenant,job_id,kind,at,payload_json,previous_sha256,event_sha256) VALUES(?,?,?,?,?,?,?)",
            (self.tenant, job_id, kind, value["at"], canonical(payload).decode(), head["sha256"], event_hash))
        connection.execute("UPDATE jobs SET revision=revision+1 WHERE job_id=? AND tenant=?", (job_id, self.tenant))
        return {"sequence": cursor.lastrowid, "sha256": event_hash}

    def append(self, job_id, kind, payload):
        with self.transaction() as connection:
            self._job(connection, job_id)
            return self._event(connection, job_id, kind, payload)

    def events(self, job_id, *, kind=None):
        with self._session() as connection:
            self._job(connection, job_id)
            rows = connection.execute("SELECT * FROM events WHERE tenant=? AND job_id=? ORDER BY sequence",
                                      (self.tenant, job_id)).fetchall()
        return [{"sequence": r["sequence"], "kind": r["kind"], "at": r["at"],
                 "payload": json.loads(r["payload_json"]), "sha256": r["event_sha256"]}
                for r in rows if kind is None or r["kind"] == kind]

    def _ensure_open(self, connection, job_id):
        job = self._job(connection, job_id)
        if connection.execute("SELECT 1 FROM bundles WHERE tenant=? AND job_id=?", (self.tenant, job_id)).fetchone():
            raise IntelligenceError("FROZEN_BUNDLE", "Frozen jobs cannot acquire or alter assessments")
        return job

    def create_job(self, plan, replay_key):
        identifier(replay_key, "replay key")
        if plan.get("plan_hash") != digest({k: v for k, v in plan.items() if k != "plan_hash"}):
            raise IntelligenceError("PLAN_CONFLICT", "Plan hash does not match")
        with self.transaction() as connection:
            previous = connection.execute("SELECT * FROM jobs WHERE tenant=? AND replay_key=?", (self.tenant, replay_key)).fetchone()
            if previous:
                if previous["plan_hash"] != plan["plan_hash"]:
                    raise IntelligenceError("PLAN_CONFLICT", "Replay key is already bound to a different plan")
                return self._status(connection, previous["job_id"])
            job_id = "intel_" + digest({"tenant": self.tenant, "replay_key": replay_key, "plan_hash": plan["plan_hash"]})[:32]
            connection.execute("INSERT INTO jobs(job_id,tenant,run_id,replay_key,plan_hash,plan_json,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, self.tenant, plan["run_id"], replay_key, plan["plan_hash"], canonical(plan).decode(), "accepted", timestamp(self.clock())))
            self._event(connection, job_id, "job_created", {"plan_hash": plan["plan_hash"], "replay_key": replay_key})
            for cell in plan["coverage_cells"]:
                self._event(connection, job_id, "gap", {"cell_id": cell["cell_id"], "target_id": cell["target_id"],
                    "required": cell["required"], "status": "missing", "reason": "NOT_YET_ACQUIRED", "attempt_refs": []})
            return self._status(connection, job_id)

    def plan(self, job_id):
        with self._session() as connection:
            return json.loads(self._job(connection, job_id)["plan_json"])

    def _status(self, connection, job_id):
        job = self._job(connection, job_id)
        attempts = self._attempts(connection, job_id)
        gaps = self._gaps(connection, job_id)
        requests = sum(row.get("result", {}).get("request_count", row["request"].get("reserved_requests", 1)) for row in attempts)
        fetches = sum(row["operation"] == "fetch" for row in attempts)
        received = sum(row.get("result", {}).get("bytes_received", 0) for row in attempts)
        unknown = sum(row.get("result", {}).get("usage", {}).get("requires_reconciliation", False) for row in attempts)
        bundle = connection.execute("SELECT bundle_id,manifest_sha256 FROM bundles WHERE job_id=? AND tenant=?", (job_id, self.tenant)).fetchone()
        return {"job_id": job_id, "run_id": job["run_id"], "state": job["state"], "revision": job["revision"], "created_at": job["created_at"],
                "plan_hash": job["plan_hash"], "gaps": gaps, "attempts": attempts,
                "inflight_attempts": [r["attempt_id"] for r in attempts if r["status"] == "inflight"],
                "usage": {"requests": requests, "fetches": fetches, "bytes": received,
                          "unreconciled_attempts": unknown}, "effective_concurrency": 1,
                "bundle": dict(bundle) if bundle else None}

    def status(self, job_id):
        with self._session() as connection:
            return self._status(connection, job_id)

    def set_state(self, job_id, state):
        if state not in {"accepted", "running", "completed", "partial", "failed"}:
            raise IntelligenceError("CONTRACT_INVALID", "Invalid job state")
        with self.transaction() as connection:
            self._ensure_open(connection, job_id)
            connection.execute("UPDATE jobs SET state=? WHERE job_id=? AND tenant=?", (state, job_id, self.tenant))
            self._event(connection, job_id, "job_state", {"state": state})

    def begin_attempt(self, job_id, source_id, operation, request, *, ordinal=0):
        if operation not in {"search", "fetch"}:
            raise IntelligenceError("CONTRACT_INVALID", "Unsupported acquisition operation")
        with self.transaction() as connection:
            job = self._ensure_open(connection, job_id)
            status = self._status(connection, job_id)
            limits = json.loads(job["plan_json"])["limits"]
            if (status["usage"]["requests"] + request.get("reserved_requests", 1) > limits["requests"] or operation == "fetch" and status["usage"]["fetches"] >= limits["fetches"]
                    or status["usage"]["bytes"] >= limits["bytes"]):
                raise IntelligenceError("BUDGET_EXHAUSTED", "Acquisition tool quota exhausted")
            attempt_id = "attempt_" + uuid.uuid4().hex
            now = timestamp(self.clock())
            connection.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?)", (attempt_id, self.tenant, job_id,
                source_id, operation, digest(request), canonical(request).decode(), now, ordinal))
            self._event(connection, job_id, "attempt_started", {"attempt_id": attempt_id, "source_id": source_id,
                "operation": operation, "request_sha256": digest(request), "request": request, "ordinal": ordinal})
            return attempt_id

    def settle_attempt(self, job_id, attempt_id, status, result):
        if status not in {"completed", "empty", "failed", "unknown"}:
            raise IntelligenceError("CONTRACT_INVALID", "Invalid attempt settlement")
        with self.transaction() as connection:
            self._job(connection, job_id)
            attempt = connection.execute("SELECT * FROM attempts WHERE attempt_id=? AND job_id=? AND tenant=?", (attempt_id, job_id, self.tenant)).fetchone()
            if attempt is None:
                raise IntelligenceError("EVIDENCE_NOT_FOUND", "Attempt is not available")
            existing = connection.execute("SELECT * FROM attempt_results WHERE attempt_id=?", (attempt_id,)).fetchone()
            if existing:
                if existing["result_json"] == canonical(result).decode() and existing["status"] == status:
                    return {"settled": False, "duplicate": True, "late": False}
                self._event(connection, job_id, "late_attempt_receipt", {"attempt_id": attempt_id,
                    "status": status, "result": result, "excluded_from_frozen_bundle": True})
                return {"settled": False, "duplicate": False, "late": True}
            connection.execute("INSERT INTO attempt_results VALUES(?,?,?,?)", (attempt_id, status, timestamp(self.clock()), canonical(result).decode()))
            self._event(connection, job_id, "attempt_settled", {"attempt_id": attempt_id, "status": status, "result": result})
            return {"settled": True, "duplicate": False, "late": False}

    def _attempts(self, connection, job_id):
        rows = connection.execute("SELECT a.*,r.status,r.result_json,r.completed_at FROM attempts a LEFT JOIN attempt_results r USING(attempt_id) WHERE a.tenant=? AND a.job_id=? ORDER BY a.started_at,a.attempt_id", (self.tenant, job_id)).fetchall()
        return [{**{k: row[k] for k in row.keys() if k not in {"request_json", "result_json", "tenant"}},
                 "request": json.loads(row["request_json"]), "status": row["status"] or "inflight",
                 **({"result": json.loads(row["result_json"])} if row["result_json"] else {})} for row in rows]

    def attempts(self, job_id):
        with self._session() as connection:
            self._job(connection, job_id)
            return self._attempts(connection, job_id)

    def put_blob(self, data: bytes, media_type="application/octet-stream"):
        if self.readonly:
            raise IntelligenceError("ACCESS_DENIED", "Archive is read only")
        if not isinstance(data, bytes):
            raise IntelligenceError("CONTRACT_INVALID", "Blob must be bytes")
        sha = hashlib.sha256(data).hexdigest()
        relative = "blobs/sha256/" + sha[:2] + "/" + sha
        path = self.root / relative
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.parent.resolve().is_relative_to(self.root):
                raise IntelligenceError("ACCESS_DENIED", "Blob directory escapes archive")
            if path.exists():
                if path.is_symlink() or path.read_bytes() != data:
                    raise IntelligenceError("HASH_MISMATCH", "Existing content-addressed blob differs")
            else:
                try:
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    if path.read_bytes() != data:
                        raise IntelligenceError("HASH_MISMATCH", "Concurrent blob write differs")
                else:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
        except OSError:
            raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Cannot persist source bytes") from None
        return {"sha256": sha, "bytes": len(data), "path": relative, "media_type": media_type}

    def read_blob(self, ref):
        sha = ref.get("sha256", "")
        relative = "blobs/sha256/" + sha[:2] + "/" + sha
        if re.fullmatch(r"[0-9a-f]{64}", sha) is None or ref.get("path") != relative:
            raise IntelligenceError("HASH_MISMATCH", "Invalid blob reference")
        path = self.root / relative
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise IntelligenceError("ACCESS_DENIED", "Blob path escapes archive")
            expected = ref.get("bytes")
            if type(expected) is not int or expected < 0 or path.stat().st_size != expected:
                raise IntelligenceError("HASH_MISMATCH", "Source blob size differs from its reference")
            with path.open("rb") as stream:
                data = stream.read(expected + 1)
        except OSError:
            raise IntelligenceError("EVIDENCE_NOT_FOUND", "Source blob is missing") from None
        if len(data) != ref.get("bytes") or hashlib.sha256(data).hexdigest() != sha:
            raise IntelligenceError("HASH_MISMATCH", "Source bytes differ from frozen hash")
        return data

    def _observed_replacement(self, connection, record):
        comparable = _revision_metadata(record)
        if comparable is None:
            return record
        values, period_basis = comparable
        projection = {"policy": _REVISION_POLICY, "representation_sha256": digest(values),
                      "period_basis": period_basis, "period_start": values[-2], "period_end": values[-1]}
        stored = {**record, "source_revision": projection}
        if record.get("supersedes"):
            return stored
        # The existing doc_id index bounds lookup to this one rolling URL. SQL
        # work and elapsed time are bounded even when that URL has many versions.
        budget = {"steps": 0, "exhausted": False}
        deadline = time.monotonic() + .1
        def guard():
            budget["steps"] += 1000
            budget["exhausted"] = budget["steps"] > 100000 or time.monotonic() >= deadline
            return int(budget["exhausted"])
        connection.set_progress_handler(guard, 1000)
        try:
            previous = connection.execute(
                "SELECT document_id,event_sequence,event_at,CASE WHEN length(CAST(metadata_json AS BLOB))<=65536 THEN metadata_json END AS metadata_json,metadata_sha256 FROM snapshot_document_index "
                "WHERE tenant=? AND doc_id=? AND first_seen_at<=? "
                "AND json_extract(metadata_json,'$.source_id')=? AND json_extract(metadata_json,'$.source_version')=? "
                "AND json_extract(metadata_json,'$.period_start') IS ? AND json_extract(metadata_json,'$.period_end') IS ? "
                "ORDER BY first_seen_at DESC,event_sequence DESC LIMIT 1",
                (self.tenant, record["doc_id"], record["times"]["first_seen"]["value"],
                 record["source_id"], record["source_version"], values[-2], values[-1])).fetchone()
            incomplete_index = previous is None and connection.execute(
                "SELECT 1 FROM documents d WHERE d.tenant=? AND d.doc_id=? AND NOT EXISTS(SELECT 1 "
                "FROM snapshot_document_index i WHERE i.tenant=d.tenant AND i.document_id=d.id) LIMIT 1",
                (self.tenant, record["doc_id"])).fetchone() is not None
        except sqlite3.OperationalError:
            if budget["exhausted"]:
                projection.update(status="unverifiable", reason="SOURCE_REVISION_LOOKUP_WORK_LIMIT")
                return stored
            raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Cannot inspect previous source-version metadata") from None
        finally:
            connection.set_progress_handler(None, 0)
        if previous is None:
            if incomplete_index:
                projection.update(status="unverifiable", reason="SOURCE_REVISION_INDEX_INCOMPLETE")
            return stored
        if previous["metadata_json"] is None:
            projection.update(status="unverifiable", reason="SOURCE_REVISION_METADATA_LIMIT")
            return stored
        metadata = json.loads(previous["metadata_json"])
        if digest(metadata) != previous["metadata_sha256"]:
            raise IntelligenceError("HASH_MISMATCH", "Previous source-version metadata differs")
        from .snapshots import _event
        receipt = _event(connection, previous["event_sequence"], self.tenant, kind="document_version")
        if (receipt["at"] != previous["event_at"] or receipt["payload"].get("document_id") != previous["document_id"]
                or receipt["payload"].get("document_sha256") != metadata["document_sha256"]):
            raise IntelligenceError("HASH_MISMATCH", "Previous source-version projection is not bound to its receipt")
        prior_projection = metadata.get("source_revision")
        projection_hash = receipt["payload"].get("source_revision_sha256")
        if projection_hash is not None and (prior_projection is None or projection_hash != digest(prior_projection)):
            raise IntelligenceError("HASH_MISMATCH", "Previous source-version representation differs from its receipt")
        if prior_projection is None or projection_hash is None:
            # A pre-policy archive need not be reindexed. Read at most one small
            # predecessor record, never any source blob or unrelated history.
            if metadata["text_blob"]["bytes"] > _REVISION_METADATA_LIMIT:
                projection.update(status="unverifiable", reason="SOURCE_REVISION_METADATA_LIMIT")
                return stored
            legacy = connection.execute("SELECT record_sha256,CASE WHEN length(CAST(record_json AS BLOB))<=? THEN record_json END AS record_json "
                "FROM documents WHERE tenant=? AND id=?", (_REVISION_METADATA_LIMIT, self.tenant, previous["document_id"])).fetchone()
            if legacy is None or legacy["record_json"] is None:
                projection.update(status="unverifiable", reason="SOURCE_REVISION_METADATA_LIMIT")
                return stored
            prior_record = json.loads(legacy["record_json"])
            if digest(prior_record) != legacy["record_sha256"] or legacy["record_sha256"] != metadata["document_sha256"]:
                raise IntelligenceError("HASH_MISMATCH", "Previous source-version original differs")
            prior = _revision_metadata(prior_record)
            prior_projection = {"policy": _REVISION_POLICY, "representation_sha256": digest(prior[0])} if prior else {}
        if (prior_projection.get("policy") != _REVISION_POLICY
                or prior_projection.get("representation_sha256") != projection["representation_sha256"]):
            return stored
        return {**stored, "supersedes": previous["document_id"], "supersession": {
            "basis": "observed_source_version", "period_basis": period_basis,
            "previous_document_sha256": metadata["document_sha256"], "previous_version": metadata["version"],
            "representation_sha256": digest(values)}}

    def put_document(self, job_id, record, *, attempt_id, rank=0, targets=(), entities=(), hit=None,
                     preserve_record=False):
        with self.transaction() as connection:
            self._ensure_open(connection, job_id)
            existing = connection.execute("SELECT * FROM documents WHERE tenant=? AND id=?", (self.tenant, record["id"])).fetchone()
            if existing:
                stored = json.loads(existing["record_json"])
                if stored["version"] != record["version"] or stored["text_blob"]["sha256"] != record["text_blob"]["sha256"]:
                    raise IntelligenceError("HASH_MISMATCH", "Existing document version differs")
            else:
                stored = record if preserve_record else self._observed_replacement(connection, record)
                connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?)", (self.tenant, stored["id"], stored["doc_id"],
                    stored["version"], canonical(stored).decode(), digest(stored), stored["times"]["first_seen"]["value"]))
                payload = {"document_id": stored["id"],
                    "document_sha256": digest(stored), "raw_blob": stored["raw_blob"], "text_blob": stored["text_blob"],
                    "first_seen_at": stored["times"]["first_seen"]["value"]}
                if "source_revision" in stored:
                    payload["source_revision_sha256"] = digest(stored["source_revision"])
                event = self._event(connection, job_id, "document_version", payload)
                from .snapshots import index_document
                index_document(connection, self.tenant, stored, event["sequence"])
            hit_id = "hit_" + digest({"attempt": attempt_id, "rank": rank, "document": stored["id"],
                                      "targets": list(targets), "entities": list(entities)})[:32]
            connection.execute("INSERT OR IGNORE INTO hits VALUES(?,?,?,?,?,?,?,?,?)", (hit_id, self.tenant, job_id, attempt_id,
                stored["id"], rank, canonical(list(targets)).decode(), canonical(list(entities)).decode(), canonical(hit or {}).decode()))
            event = self._event(connection, job_id, "query_hit", {"hit_id": hit_id, "attempt_id": attempt_id,
                "document_id": stored["id"], "rank": rank, "targets": list(targets), "entities": list(entities)})
            from .snapshots import index_hit
            index_hit(connection, self.tenant, stored["id"], event["sequence"], targets, entities)
            return stored

    def document(self, document_id):
        with self._session() as connection:
            row = connection.execute("SELECT * FROM documents WHERE tenant=? AND id=?", (self.tenant, document_id)).fetchone()
        if row is None:
            raise IntelligenceError("EVIDENCE_NOT_FOUND", "Document is not available to this tenant")
        record = json.loads(row["record_json"])
        if digest(record) != row["record_sha256"]:
            raise IntelligenceError("HASH_MISMATCH", "Document metadata differs")
        self.read_blob(record["raw_blob"])
        text = self.read_blob(record["text_blob"]).decode("utf-8")
        if record.get("text") != text:
            raise IntelligenceError("HASH_MISMATCH", "Document text differs")
        return record

    def import_document(self, job_id, record, manifest, entities):
        plan = self.plan(job_id)
        targets = [cell["target_id"] for cell in plan["coverage_cells"] if cell.get("entity_id") in entities
                   or cell["target_id"].startswith(("chain:", "sector:", "global:"))]
        return self.put_document(job_id, record, attempt_id="baseline:" + manifest["manifest_sha256"],
            targets=targets, entities=entities, preserve_record=True,
            hit={"baseline_archive_id": manifest["archive_id"], "baseline_manifest_sha256": manifest["manifest_sha256"]})

    def documents(self, job_id):
        with self._session() as connection:
            self._job(connection, job_id)
            rows = connection.execute("SELECT * FROM hits WHERE tenant=? AND job_id=? ORDER BY rowid", (self.tenant, job_id)).fetchall()
        linked = {}
        for row in rows:
            item = linked.setdefault(row["document_id"], {"targets": set(), "entities": set(), "hits": []})
            item["targets"].update(json.loads(row["targets_json"]))
            item["entities"].update(json.loads(row["entities_json"]))
            item["hits"].append(row["hit_id"])
        return [{**self.document(key), "target_ids": sorted(value["targets"]),
                 "requested_entity_ids": sorted(value["entities"]), "query_hit_ids": value["hits"]}
                for key, value in linked.items()]

    def gap(self, job_id, row):
        if row.get("status") not in {"present", "missing", "conflicting", "unverifiable", "not_connected", "pending"}:
            raise IntelligenceError("CONTRACT_INVALID", "Invalid coverage status")
        plan = self.plan(job_id)
        cells = {c["cell_id"]: c for c in plan["coverage_cells"]}
        cell = cells.get(row.get("cell_id"))
        if not cell:
            raise IntelligenceError("CONTRACT_INVALID", "Unknown coverage cell")
        with self.transaction() as connection:
            self._ensure_open(connection, job_id)
            return self._event(connection, job_id, "gap", {**row, "target_id": cell["target_id"], "required": cell["required"]})

    def _gaps(self, connection, job_id):
        rows = connection.execute("SELECT sequence,payload_json FROM events WHERE tenant=? AND job_id=? AND kind='gap' ORDER BY sequence", (self.tenant, job_id)).fetchall()
        result, diagnostics, seen = {}, {}, {}
        for row in rows:
            value = json.loads(row["payload_json"])
            value.pop("source_diagnostics", None)
            value.pop("source_reason", None)
            cell_id = value["cell_id"]
            result[cell_id] = value
            if value.get("diagnostic_kind") == "source":
                detail = {"reason": value["reason"], "status": value["status"],
                    "attempt_refs": value.get("attempt_refs", []), "document_ids": value.get("document_ids", [])}
                identity = digest(detail)
                if identity not in seen.setdefault(cell_id, set()):
                    seen[cell_id].add(identity)
                    diagnostics.setdefault(cell_id, []).append({**detail, "event_sequence": row["sequence"]})
        for cell_id, details in diagnostics.items():
            result[cell_id] = {**result[cell_id], "source_diagnostics": details,
                               "source_reason": details[-1]["reason"]}
        return list(result.values())

    def record_assessment(self, job_id, claims, coverage):
        with self.transaction() as connection:
            self._ensure_open(connection, job_id)
            ref = self._event(connection, job_id, "assessment", {"claims_sha256": digest(claims), "coverage_sha256": digest(coverage)})
            assessment_id = "assessment_" + ref["sha256"][:24]
            connection.execute("INSERT INTO assessments VALUES(?,?,?,?,?,?,?)", (assessment_id, self.tenant, job_id,
                timestamp(self.clock()), canonical(claims).decode(), canonical(coverage).decode(), ref["sequence"]))
            return assessment_id

    def assessment(self, job_id):
        with self._session() as connection:
            self._job(connection, job_id)
            row = connection.execute("SELECT * FROM assessments WHERE tenant=? AND job_id=? ORDER BY event_sequence DESC LIMIT 1", (self.tenant, job_id)).fetchone()
        return {"claims": json.loads(row["claims_json"]), "coverage": json.loads(row["coverage_json"]),
                "assessment_id": row["assessment_id"]} if row else {"claims": [], "coverage": [], "assessment_id": None}

    def verify_chain(self, *, through=None):
        with self._session() as connection:
            rows = connection.execute("SELECT * FROM events WHERE sequence<=? ORDER BY sequence", (through if through is not None else 2**63 - 1,)).fetchall()
        previous = None
        last = {"sequence": 0, "sha256": None}
        for row in rows:
            value = {"tenant": row["tenant"], "job_id": row["job_id"], "kind": row["kind"], "at": row["at"],
                     "payload": json.loads(row["payload_json"]), "previous_sha256": previous}
            if row["previous_sha256"] != previous or digest(value) != row["event_sha256"]:
                raise IntelligenceError("HASH_MISMATCH", "Acquisition event chain differs")
            previous = row["event_sha256"]
            last = {"sequence": row["sequence"], "sha256": previous}
        return last

    def freeze(self, job_id, *, expected_plan_hash, expected_revision, expected_frontier_hash,
               source_manifest, baseline_refs=None):
        documents = self.documents(job_id)
        assessment = self.assessment(job_id)
        with self.transaction() as connection:
            job = self._job(connection, job_id)
            plan = json.loads(job["plan_json"])
            if job["plan_hash"] != expected_plan_hash or plan["frontier_manifest_hash"] != expected_frontier_hash:
                raise IntelligenceError("PLAN_CONFLICT", "Freeze plan/frontier does not match")
            existing = connection.execute("SELECT record_json FROM bundles WHERE tenant=? AND job_id=?", (self.tenant, job_id)).fetchone()
            if existing:
                return json.loads(existing["record_json"])
            if job["revision"] != expected_revision:
                raise IntelligenceError("JOB_REVISION_CONFLICT", "Collection changed before freeze")
            status = self._status(connection, job_id)
            if status["inflight_attempts"]:
                raise IntelligenceError("INFLIGHT_ATTEMPTS", "All started attempts must settle before freeze")
            missing = [row["cell_id"] for row in status["gaps"] if row["required"] and row["status"] != "present"]
            head = self._head(connection)
            manifest = {"schema": BUNDLE_SCHEMA, "job_id": job_id, "run_id": job["run_id"], "tenant": self.tenant,
                "as_of": plan["as_of"], "as_of_basis": plan["as_of_basis"], "plan_hash": expected_plan_hash,
                "frontier_manifest_hash": expected_frontier_hash, "allowed_entities": plan["allowed_entities"],
                "market_snapshot_refs": plan["market_snapshot_refs"], "source_registry": source_manifest,
                "documents": documents, "claims": assessment["claims"], "coverage": assessment["coverage"],
                "gaps": status["gaps"], "missing_required": missing,
                "state": "partial" if missing else "completed", "attempts": status["attempts"],
                "usage": status["usage"], "baseline_refs": baseline_refs or [], "event_head": head,
                "freeze_event_sequence": head["sequence"] + 1, "frozen_at": timestamp(self.clock())}
            sha = digest(manifest)
            bundle_id = "bundle_" + sha[:32]
            event = self._event(connection, job_id, "bundle_frozen", {"bundle_id": bundle_id, "manifest_sha256": sha,
                "state": manifest["state"], "missing_required": missing})
            record = {**manifest, "bundle_id": bundle_id, "manifest_sha256": sha, "freeze_event_ref": event}
            path = self.root / "bundles" / (bundle_id + ".json")
            data = canonical(record)
            try:
                with path.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                if path.read_bytes() != data:
                    raise IntelligenceError("HASH_MISMATCH", "Existing frozen manifest differs")
            except OSError:
                raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Cannot persist frozen manifest") from None
            connection.execute("INSERT INTO bundles VALUES(?,?,?,?,?)", (bundle_id, self.tenant, job_id, sha, data.decode()))
            connection.execute("UPDATE jobs SET state=? WHERE job_id=? AND tenant=?", (record["state"], job_id, self.tenant))
            return record

    def bundle(self, bundle_id):
        identifier(bundle_id, "bundle id")
        with self._session() as connection:
            row = connection.execute("SELECT record_json FROM bundles WHERE tenant=? AND bundle_id=?", (self.tenant, bundle_id)).fetchone()
        if row is None:
            raise IntelligenceError("EVIDENCE_NOT_FOUND", "Bundle is not available to this tenant")
        record = json.loads(row["record_json"])
        manifest = {k: v for k, v in record.items() if k not in {"bundle_id", "manifest_sha256", "freeze_event_ref"}}
        if digest(manifest) != record["manifest_sha256"] or bundle_id != "bundle_" + digest(manifest)[:32]:
            raise IntelligenceError("HASH_MISMATCH", "Frozen manifest differs")
        try:
            path = self.root / "bundles" / (bundle_id + ".json")
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise IntelligenceError("ACCESS_DENIED", "Frozen manifest path escapes archive")
            saved = path.read_bytes()
        except OSError:
            raise IntelligenceError("EVIDENCE_NOT_FOUND", "Frozen manifest file is missing") from None
        if saved != canonical(record):
            raise IntelligenceError("HASH_MISMATCH", "Frozen manifest file differs")
        if self.verify_chain(through=record["event_head"]["sequence"]) != record["event_head"]:
            raise IntelligenceError("HASH_MISMATCH", "Frozen event frontier differs")
        freeze_head = self.verify_chain(through=record["freeze_event_ref"]["sequence"])
        if freeze_head != record["freeze_event_ref"]:
            raise IntelligenceError("HASH_MISMATCH", "Freeze event differs")
        with self._session() as connection:
            event = connection.execute("SELECT * FROM events WHERE sequence=?", (record["freeze_event_ref"]["sequence"],)).fetchone()
        expected_freeze = {"bundle_id": bundle_id, "manifest_sha256": record["manifest_sha256"],
            "state": record["state"], "missing_required": record["missing_required"]}
        if (record["freeze_event_sequence"] != record["event_head"]["sequence"] + 1
                or record["freeze_event_ref"]["sequence"] != record["freeze_event_sequence"]
                or event is None or event["tenant"] != self.tenant or event["job_id"] != record["job_id"]
                or event["kind"] != "bundle_frozen" or json.loads(event["payload_json"]) != expected_freeze):
            raise IntelligenceError("HASH_MISMATCH", "Bundle manifest is not bound to its actual freeze event")
        for doc in record["documents"]:
            actual = self.document(doc["id"])
            comparable = {k: v for k, v in doc.items() if k not in {"target_ids", "requested_entity_ids", "query_hit_ids"}}
            if actual != comparable:
                raise IntelligenceError("HASH_MISMATCH", "Frozen document differs")
        return record

    def archive_versions(self, as_of, entities=()):
        cutoff = timestamp(as_of)
        chain = self.verify_chain()
        with self._session() as connection:
            archive_id = connection.execute("SELECT value FROM settings WHERE key='archive_id'").fetchone()[0]
            rows = connection.execute("SELECT id FROM documents WHERE tenant=? AND first_seen_at<=? ORDER BY first_seen_at,id", (self.tenant, cutoff)).fetchall()
            links = connection.execute("SELECT document_id,entities_json FROM hits WHERE tenant=?", (self.tenant,)).fetchall()
        linked = {}
        for row in links:
            linked.setdefault(row["document_id"], set()).update(json.loads(row["entities_json"]))
        versions = []
        requested = set(entities)
        for row in rows:
            doc = self.document(row["id"])
            if requested and not requested.intersection(linked.get(doc["id"], ())):
                continue
            versions.append({"id": doc["id"], "doc_id": doc["doc_id"], "version": doc["version"],
                "text_sha256": doc["text_blob"]["sha256"], "raw_sha256": doc["raw_blob"]["sha256"],
                "first_seen_at": doc["times"]["first_seen"]["value"], "source_id": doc["source_id"],
                "entity_ids": sorted(linked.get(doc["id"], ())), "document_sha256": digest(doc)})
        result = {"schema": "tiger.intelligence-baseline/v1", "tenant": self.tenant, "archive_id": archive_id,
                  "as_of": cutoff, "versions": versions, "event_head": chain}
        return {**result, "manifest_sha256": digest(result)}

    def create_snapshot(self, as_of, *, scope, limits=None, topic_ref=None, registry_hash=""):
        """Seal a bounded selection without reading unrelated source bytes."""
        from .snapshots import create_snapshot
        return create_snapshot(self, as_of, scope=scope, limits=limits,
                               topic_ref=topic_ref, registry_hash=registry_hash)

    def snapshot(self, snapshot_id):
        from .snapshots import read_snapshot
        return read_snapshot(self, snapshot_id)

    def snapshot_documents(self, snapshot_id):
        from .snapshots import read_snapshot
        return read_snapshot(self, snapshot_id, documents=True)[1]


def verify_bundle(root, bundle_id, *, tenant="public"):
    return Archive(root, tenant=tenant, readonly=True).bundle(bundle_id)
