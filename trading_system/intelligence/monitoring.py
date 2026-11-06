"""Finite durable collection scheduling, fenced cursors and local change outbox.

The worker uses IntelligenceService for every network operation. A watch never
starts a model or changes a paper account. Queue records survive a restart;
uncertain external attempts are held for reconciliation rather than resent.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

from .catalog import DIMENSIONS, SourceCatalog, material_policy as normalize_material_policy
from .contracts import IntelligenceError, aware, canonical, digest, identifier, safe_value, timestamp, utc_now


SCHEMA = "tiger.intelligence-monitor/v1"
CADENCES = {"core": 300, "normal": 900, "low": 3600, "daily": 86400}
UNIT_LIMITS = {"requests": 48, "fetches": 24, "bytes": 8 * 1024 * 1024,
    "rounds": 1, "concurrency": 1, "deadline_seconds": 240}
_PAUSED = {"SOURCE_DISABLED": "paused_disabled", "ENTITLEMENT_REQUIRED": "paused_entitlement",
    "SOURCE_AUTH_REQUIRED": "paused_credentials", "SOURCE_USE_DENIED": "paused_allowed_uses",
    "SOURCE_NOT_CONNECTED": "paused_connection", "POLICY_DENIED": "paused_policy"}
_PAUSE_REASONS = {health: reason for reason, health in _PAUSED.items()}
_DDL = """
CREATE TABLE IF NOT EXISTS monitor_topics (
 tenant TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, scope_hash TEXT NOT NULL,
 record TEXT NOT NULL, PRIMARY KEY(tenant,id,version));
CREATE TABLE IF NOT EXISTS monitor_keys (
 tenant TEXT NOT NULL, kind TEXT NOT NULL, replay_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 record TEXT NOT NULL, PRIMARY KEY(tenant,kind,replay_key));
CREATE TABLE IF NOT EXISTS monitor_watches (
 tenant TEXT NOT NULL, id TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(tenant,id));
CREATE TABLE IF NOT EXISTS monitor_units (
 tenant TEXT NOT NULL, id TEXT NOT NULL, watch_id TEXT NOT NULL, source_id TEXT NOT NULL,
 domain TEXT NOT NULL, topic_id TEXT NOT NULL, topic_version INTEGER NOT NULL,
 cadence INTEGER NOT NULL, priority INTEGER NOT NULL, next_due REAL NOT NULL,
 last_attempt REAL, last_success REAL, last_change REAL, fingerprint TEXT,
 cursor_json TEXT NOT NULL DEFAULT '{}', health TEXT NOT NULL DEFAULT 'never_observed',
 failures INTEGER NOT NULL DEFAULT 0, source_contract_hash TEXT NOT NULL DEFAULT '', PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS monitor_due ON monitor_units(tenant,next_due,priority,id);
CREATE INDEX IF NOT EXISTS monitor_source ON monitor_units(tenant,source_id,last_attempt);
CREATE TABLE IF NOT EXISTS monitor_jobs (
 tenant TEXT NOT NULL, id TEXT NOT NULL, unit_id TEXT NOT NULL, state TEXT NOT NULL,
 created REAL NOT NULL, due REAL NOT NULL, priority INTEGER NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0, owner TEXT, lease_until REAL,
 deadline REAL NOT NULL, acquisition_id TEXT, plan_json TEXT, reservation INTEGER NOT NULL,
 byte_reservation INTEGER NOT NULL DEFAULT 0,
 quota_day TEXT NOT NULL, result TEXT, PRIMARY KEY(tenant,id));
CREATE UNIQUE INDEX IF NOT EXISTS monitor_active_unit ON monitor_jobs(tenant,unit_id)
 WHERE state IN ('queued','running','reconciliation');
CREATE INDEX IF NOT EXISTS monitor_queue ON monitor_jobs(tenant,state,due,priority,created);
CREATE TABLE IF NOT EXISTS monitor_rate (
 tenant TEXT NOT NULL, domain TEXT NOT NULL, next_allowed REAL NOT NULL,
 PRIMARY KEY(tenant,domain));
CREATE TABLE IF NOT EXISTS monitor_budget (
 tenant TEXT NOT NULL, day TEXT NOT NULL, source_id TEXT NOT NULL,
 spent INTEGER NOT NULL DEFAULT 0, held INTEGER NOT NULL DEFAULT 0,
 bytes_spent INTEGER NOT NULL DEFAULT 0, bytes_held INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,day,source_id));
CREATE TABLE IF NOT EXISTS monitor_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, kind TEXT NOT NULL,
 event_at REAL NOT NULL, record TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS monitor_tenant_events ON monitor_events(tenant,sequence);
CREATE TABLE IF NOT EXISTS monitor_outbox (
 tenant TEXT NOT NULL, topic_id TEXT NOT NULL, version INTEGER NOT NULL,
 state TEXT NOT NULL, revision INTEGER NOT NULL, record TEXT NOT NULL,
 PRIMARY KEY(tenant,topic_id,version));
"""


def _json(value):
    if safe_value(value) != value:
        raise IntelligenceError("POLICY_DENIED", "Unsafe monitor metadata")
    result = canonical(value).decode()
    if re.search(r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9]{20,}", result):
        raise IntelligenceError("POLICY_DENIED", "Credential-like content cannot be monitor metadata")
    return result


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise IntelligenceError("CONTRACT_INVALID", "Invalid " + label)
    return value


class MonitoringStore:
    def __init__(self, root, *, tenant="public", clock=utc_now, catalog=None,
                 max_queue=128, daily_requests=20000, daily_bytes=2 * 1024**3,
                 minimum_free_bytes=256 * 1024 * 1024, readonly=False):
        self.tenant = identifier(tenant, "tenant")
        self.clock = clock
        self.readonly = readonly
        self.catalog = catalog or SourceCatalog()
        self.max_queue = _integer(max_queue, 1, 10000, "queue capacity")
        self.daily_requests = _integer(daily_requests, 1, 1000000, "daily request quota")
        self.daily_bytes = _integer(daily_bytes, 1, 1024**4, "daily byte quota")
        self.minimum_free_bytes = _integer(minimum_free_bytes, 0, 1024**4, "disk high water mark")
        self.root = Path(root).absolute()
        for path in [self.root, *self.root.parents]:
            if path.is_symlink():
                raise IntelligenceError("POLICY_DENIED", "Monitor roots cannot contain symlinks")
        self.database = self.root / "monitoring.sqlite"
        if self.database.is_symlink() or self.database.exists() and self.database.stat().st_nlink != 1:
            raise IntelligenceError("POLICY_DENIED", "Unsafe monitor database")
        if readonly:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with self._connection() as connection:
                connection.executescript(_DDL)
                columns = {row[1] for row in connection.execute("PRAGMA table_info(monitor_units)")}
                if "source_contract_hash" not in columns:
                    connection.execute("ALTER TABLE monitor_units ADD COLUMN source_contract_hash TEXT NOT NULL DEFAULT ''")
                for table, names in (("monitor_jobs", ["byte_reservation"]), ("monitor_budget", ["bytes_spent", "bytes_held"])):
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}
                    for name in names:
                        if name not in columns:
                            connection.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " INTEGER NOT NULL DEFAULT 0")
                if "plan_json" not in {row[1] for row in connection.execute("PRAGMA table_info(monitor_jobs)")}:
                    connection.execute("ALTER TABLE monitor_jobs ADD COLUMN plan_json TEXT")
            os.chmod(self.database, 0o600)
        except OSError:
            raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Monitor storage is unavailable") from None

    @contextmanager
    def _connection(self, *, write=False):
        connection = None
        if write and self.readonly:
            raise IntelligenceError("MODE_DENIED", "Monitor storage is read only")
        try:
            connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True,
                timeout=5, isolation_level=None) if self.readonly else sqlite3.connect(self.database, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            if not self.readonly:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except sqlite3.Error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Monitor transaction failed") from None
        except BaseException:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _now(self):
        return aware(self.clock()).timestamp()

    def _event(self, connection, kind, payload):
        previous = connection.execute("SELECT event_hash FROM monitor_events WHERE tenant=? ORDER BY sequence DESC LIMIT 1", (self.tenant,)).fetchone()
        previous = previous[0] if previous else "0" * 64
        when = self._now()
        record = {"tenant": self.tenant, "kind": kind, "at": when, "payload": payload, "previous_hash": previous}
        connection.execute("INSERT INTO monitor_events(tenant,kind,event_at,record,previous_hash,event_hash) VALUES(?,?,?,?,?,?)",
            (self.tenant, kind, when, _json(record), previous, digest(record)))

    def _replay(self, connection, kind, key, request):
        identifier(key, "idempotency key")
        row = connection.execute("SELECT request_hash,record FROM monitor_keys WHERE tenant=? AND kind=? AND replay_key=?", (self.tenant, kind, key)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != digest(request):
            raise IntelligenceError("IDEMPOTENCY_CONFLICT", "Key belongs to another monitor request")
        return json.loads(row["record"])

    def _key(self, connection, kind, key, request, record):
        connection.execute("INSERT INTO monitor_keys VALUES(?,?,?,?,?)", (self.tenant, kind, key, digest(request), _json(record)))

    def create_topic(self, question, entity_ids=(), material_policy=None, horizon="current", *, idempotency_key):
        if not isinstance(question, str) or not question.strip() or len(question) > 10000:
            raise IntelligenceError("CONTRACT_INVALID", "A bounded explicit research question is required")
        if not isinstance(horizon, str) or horizon not in {"current", "week", "month", "baseline"}:
            raise IntelligenceError("CONTRACT_INVALID", "Unknown research horizon")
        entities = self.catalog.validate_entities(entity_ids)
        if material_policy is None:
            entity_types = {self.catalog.entities[x]["type"] for x in entities}
            material_policy = {key: "required" for kind, key in (("person", "person_statement"),
                ("institution", "institutional_commentary"), ("industry", "industry_data")) if kind in entity_types}
        policy = normalize_material_policy(material_policy, has_entities=any(self.catalog.entities[x]["type"] in {"security", "issuer"} for x in entities))
        scope = {"question": question.strip(), "entity_ids": entities, "horizon": horizon, "material_policy": policy}
        _json(scope)
        catalog_hash = self.catalog.manifest()["sha256"]
        identity = "topic_" + digest({"tenant": self.tenant, "scope": scope, "catalog_sha256": catalog_hash})[:32]
        with self._connection(write=True) as connection:
            prior = self._replay(connection, "topic", idempotency_key, scope)
            if prior:
                checked = self._topic(connection, prior["topic_id"], prior["version"])
                if checked != prior:
                    raise IntelligenceError("HASH_MISMATCH", "Topic replay record differs")
                return checked
            value = {"schema": "tiger.intelligence-topic/v1", "tenant": self.tenant, "topic_id": identity,
                "version": 1, **scope, "scope_hash": digest(scope), "catalog_sha256": catalog_hash,
                "created_at": timestamp(self.clock())}
            connection.execute("INSERT OR IGNORE INTO monitor_topics VALUES(?,?,?,?,?)", (self.tenant, identity, 1, value["scope_hash"], _json(value)))
            stored = self._topic(connection, identity, 1)
            self._key(connection, "topic", idempotency_key, scope, stored)
            self._event(connection, "topic_created", {"topic_id": identity, "scope_hash": value["scope_hash"]})
            return stored

    def _topic(self, connection, topic_id, version):
        identifier(topic_id, "topic")
        if version is None:
            row = connection.execute("SELECT record,version FROM monitor_topics WHERE tenant=? AND id=? ORDER BY version DESC LIMIT 1", (self.tenant, topic_id)).fetchone()
        else:
            _integer(version, 1, 1000000, "topic version")
            row = connection.execute("SELECT record,version FROM monitor_topics WHERE tenant=? AND id=? AND version=?", (self.tenant, topic_id, version)).fetchone()
        if row is None:
            raise IntelligenceError("EVIDENCE_NOT_FOUND", "Topic is unavailable to this tenant")
        value = json.loads(row[0])
        try:
            scope = {key: value[key] for key in ("question", "entity_ids", "horizon", "material_policy")}
            legacy_id = "topic_" + digest({"tenant": self.tenant, "scope": scope})[:32]
            expected_id = "topic_" + digest({"tenant": self.tenant, "scope": scope,
                "catalog_sha256": value["catalog_sha256"]})[:32]
            valid = value["tenant"] == self.tenant and value["topic_id"] == topic_id and topic_id in {legacy_id, expected_id} and value["scope_hash"] == digest(scope) and value["version"] == row["version"]
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise IntelligenceError("HASH_MISMATCH", "Topic immutable scope differs")
        return value

    def topic(self, topic_id, version=None):
        if not self.database.exists():
            raise IntelligenceError("EVIDENCE_NOT_FOUND", "Topic is unavailable to this tenant")
        with self._connection() as connection:
            return self._topic(connection, topic_id, version)

    def create_watch(self, topic_id, source_ids, cadence_preset="normal", idempotency_key=None, topic_version=None, *, version=None):
        if (not isinstance(cadence_preset, str) or cadence_preset not in CADENCES
                or not isinstance(source_ids, (list, tuple)) or not 1 <= len(source_ids) <= 150
                or any(not isinstance(source, str) for source in source_ids)
                or len(set(source_ids)) != len(source_ids)):
            raise IntelligenceError("CONTRACT_INVALID", "Watch needs bounded reviewed sources and cadence")
        for source_id in source_ids:
            spec = self.catalog.registry.get(source_id)
            if spec.kind == "web_fetch":
                raise IntelligenceError("POLICY_DENIED", "A fetch-only source cannot be polled without registered discovery")
        topic_version = version if version is not None else topic_version
        with self._connection(write=True) as connection:
            topic = self._topic(connection, topic_id, topic_version)
            catalog_hash = self.catalog.manifest()["sha256"]
            if topic["catalog_sha256"] != catalog_hash:
                raise IntelligenceError("PLAN_CONFLICT", "Topic belongs to an earlier reviewed catalog; create its new contract identity")
            request = {"topic_id": topic_id, "version": topic["version"], "source_ids": sorted(source_ids),
                "cadence_preset": cadence_preset, "catalog_sha256": catalog_hash}
            prior = self._replay(connection, "watch", idempotency_key, request)
            if prior:
                return prior
            watch_id = "watch_" + digest({"tenant": self.tenant, **request})[:32]
            result = {"schema": SCHEMA, "watch_id": watch_id, **request, "created_at": timestamp(self.clock()), "units": []}
            now = self._now()
            for source_id in sorted(source_ids):
                spec = self.catalog.registry.get(source_id)
                row = self.catalog.sources[source_id]
                cadence = max(CADENCES[cadence_preset], row["cadence_seconds"], math.ceil(spec.minimum_interval_seconds))
                unit_id = "unit_" + digest({"watch": watch_id, "source": source_id})[:32]
                domain = urlsplit(spec.endpoint).hostname or source_id
                connection.execute("INSERT OR IGNORE INTO monitor_units(tenant,id,watch_id,source_id,domain,topic_id,topic_version,cadence,priority,next_due,source_contract_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (self.tenant, unit_id, watch_id, source_id, domain, topic_id, topic["version"], cadence, row["priority"], now,
                        digest({"spec": spec.public(), "catalog": row})))
                result["units"].append({"unit_id": unit_id, "source_id": source_id, "effective_cadence_seconds": cadence, "next_due": timestamp(self.clock())})
            connection.execute("INSERT OR IGNORE INTO monitor_watches VALUES(?,?,?)", (self.tenant, watch_id, _json(result)))
            stored = json.loads(connection.execute("SELECT record FROM monitor_watches WHERE tenant=? AND id=?", (self.tenant, watch_id)).fetchone()[0])
            self._key(connection, "watch", idempotency_key, request, stored)
            self._event(connection, "watch_created", {"watch_id": watch_id, "source_ids": sorted(source_ids)})
            return stored

    def enqueue_due(self, *, limit=32):
        _integer(limit, 1, self.max_queue, "enqueue limit")
        now = self._now()
        if self.minimum_free_bytes:
            try:
                stat = os.statvfs(self.root)
            except OSError:
                raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Monitor disk watermark cannot be checked") from None
            if stat.f_bavail * stat.f_frsize < self.minimum_free_bytes:
                return {"enqueued": [], "deferred": 0, "reason": "DISK_HIGH_WATER"}
        with self._connection(write=True) as connection:
            active = connection.execute("SELECT COUNT(*) FROM monitor_jobs WHERE tenant=? AND state IN ('queued','running','reconciliation')", (self.tenant,)).fetchone()[0]
            room = max(0, min(limit, self.max_queue - active))
            rows = connection.execute("SELECT u.* FROM monitor_units u WHERE u.tenant=? AND u.next_due<=? AND NOT EXISTS(SELECT 1 FROM monitor_jobs j WHERE j.tenant=u.tenant AND j.unit_id=u.id AND j.state IN ('queued','running','reconciliation')) ORDER BY u.next_due,u.priority,u.id LIMIT ?", (self.tenant, now, limit + 1)).fetchall()
            created = []
            for row in rows[:room]:
                key = "monitor_" + uuid.uuid4().hex
                connection.execute("INSERT INTO monitor_jobs(tenant,id,unit_id,state,created,due,priority,deadline,reservation,quota_day) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (self.tenant, key, row["id"], "queued", now, now, row["priority"], now + 1800, 0, ""))
                created.append(key)
                self._event(connection, "monitor_enqueued", {"job_id": key, "unit_id": row["id"]})
            return {"enqueued": created, "deferred": max(0, len(rows) - room),
                "deferred_count_is_lower_bound": len(rows) > limit,
                "reason": "QUEUE_FULL" if len(rows) > room else None}

    def claim(self, owner, *, lease_seconds=180, request_reservation=24, byte_reservation=8 * 1024 * 1024):
        identifier(owner, "worker owner")
        _integer(lease_seconds, 1, 900, "lease seconds")
        _integer(request_reservation, 1, 320, "request reservation")
        _integer(byte_reservation, 1, 64 * 1024 * 1024, "byte reservation")
        now, day = self._now(), aware(self.clock()).date().isoformat()
        with self._connection(write=True) as connection:
            # An expired queue entry is terminal; a running record retains its
            # acquisition identity and reservation for safe recovery.
            connection.execute("UPDATE monitor_jobs SET state='expired' WHERE tenant=? AND state='queued' AND deadline<=?", (self.tenant, now))
            rows = connection.execute("SELECT j.*,u.source_id,u.domain,u.cadence,u.watch_id,u.topic_id,u.topic_version,u.cursor_json,u.source_contract_hash FROM monitor_jobs j JOIN monitor_units u ON u.tenant=j.tenant AND u.id=j.unit_id WHERE j.tenant=? AND u.health!='quota_bound_breached' AND ((j.state='queued' AND j.due<=?) OR (j.state='running' AND j.lease_until<=?)) ORDER BY j.created,j.priority,j.id LIMIT ?", (self.tenant, now, now, self.max_queue)).fetchall()
            for row in rows:
                current = digest({"spec": self.catalog.registry.get(row["source_id"], require_enabled=False).public(), "catalog": self.catalog.sources[row["source_id"]]})
                if current != row["source_contract_hash"] or not self.catalog.registry.get(row["source_id"], require_enabled=False).enabled:
                    connection.execute("UPDATE monitor_units SET health='source_policy_changed' WHERE tenant=? AND id=?", (self.tenant, row["unit_id"]))
                    continue
                readiness = self.catalog.connection(row["source_id"], now=self.clock())
                paused = _PAUSED.get(readiness["reason"])
                if paused:
                    connection.execute("UPDATE monitor_units SET health=? WHERE tenant=? AND id=?", (paused, self.tenant, row["unit_id"]))
                    continue
                limit = connection.execute("SELECT next_allowed FROM monitor_rate WHERE tenant=? AND domain=?", (self.tenant, row["domain"])).fetchone()
                if limit is not None and limit[0] > now:
                    continue
                if connection.execute("SELECT 1 FROM monitor_jobs j JOIN monitor_units u ON u.tenant=j.tenant AND u.id=j.unit_id WHERE j.tenant=? AND j.state='running' AND j.lease_until>? AND u.domain=? LIMIT 1", (self.tenant, now, row["domain"])).fetchone():
                    continue
                connection.execute("INSERT OR IGNORE INTO monitor_budget(tenant,day,source_id) VALUES(?,?,?)", (self.tenant, day, row["source_id"]))
                old_hold = row["reservation"] if row["state"] == "running" else 0
                source_budget = connection.execute("SELECT spent,held FROM monitor_budget WHERE tenant=? AND day=? AND source_id=?", (self.tenant, day, row["source_id"])).fetchone()
                global_budget = connection.execute("SELECT COALESCE(SUM(spent+held),0) FROM monitor_budget WHERE tenant=? AND day=?", (self.tenant, day)).fetchone()[0]
                global_bytes = connection.execute("SELECT COALESCE(SUM(bytes_spent+bytes_held),0) FROM monitor_budget WHERE tenant=? AND day=?", (self.tenant, day)).fetchone()[0]
                if not old_hold and (global_budget + request_reservation > self.daily_requests or global_bytes + byte_reservation > self.daily_bytes or sum(source_budget) + request_reservation > self.catalog.sources[row["source_id"]]["maximum_requests_per_day"]):
                    connection.execute("UPDATE monitor_units SET health='quota_waiting' WHERE tenant=? AND id=?", (self.tenant, row["unit_id"]))
                    continue
                if not old_hold:
                    connection.execute("UPDATE monitor_budget SET held=held+?,bytes_held=bytes_held+? WHERE tenant=? AND day=? AND source_id=?", (request_reservation, byte_reservation, self.tenant, day, row["source_id"]))
                generation = row["generation"] + 1
                reservation = old_hold or request_reservation
                quota_day = row["quota_day"] if old_hold else day
                reserved_bytes = row["byte_reservation"] if old_hold else byte_reservation
                connection.execute("UPDATE monitor_jobs SET state='running',owner=?,generation=?,lease_until=?,reservation=?,byte_reservation=?,quota_day=? WHERE tenant=? AND id=?", (owner, generation, now + lease_seconds, reservation, reserved_bytes, quota_day, self.tenant, row["id"]))
                connection.execute("UPDATE monitor_units SET last_attempt=? WHERE tenant=? AND id=?", (now, self.tenant, row["unit_id"]))
                minimum = max(0.0, self.catalog.registry.get(row["source_id"]).minimum_interval_seconds)
                connection.execute("INSERT INTO monitor_rate VALUES(?,?,?) ON CONFLICT(tenant,domain) DO UPDATE SET next_allowed=MAX(next_allowed,excluded.next_allowed)", (self.tenant, row["domain"], now + minimum))
                self._event(connection, "monitor_claimed", {"job_id": row["id"], "owner": owner, "generation": generation})
                value = dict(row)
                value.update(state="running", owner=owner, generation=generation, lease_until=now + lease_seconds, reservation=reservation, byte_reservation=reserved_bytes, quota_day=quota_day, cursor=json.loads(row["cursor_json"]))
                value["topic"] = self._topic(connection, row["topic_id"], row["topic_version"])
                return value
            return None

    def _lease(self, connection, job_id, owner, generation):
        row = connection.execute("SELECT j.*,u.source_id,u.domain,u.cadence,u.watch_id,u.topic_id,u.topic_version,u.fingerprint,u.failures,u.health FROM monitor_jobs j JOIN monitor_units u ON j.tenant=u.tenant AND j.unit_id=u.id WHERE j.tenant=? AND j.id=?", (self.tenant, identifier(job_id))).fetchone()
        if row is None:
            raise IntelligenceError("EVIDENCE_NOT_FOUND", "Monitor job unavailable")
        if row["state"] != "running" or row["owner"] != owner or row["generation"] != generation or row["lease_until"] <= self._now():
            raise IntelligenceError("LEASE_CONFLICT", "Monitor worker lease is stale")
        return row

    def bind_acquisition(self, job_id, owner, generation, acquisition_id):
        identifier(acquisition_id)
        with self._connection(write=True) as connection:
            row = self._lease(connection, job_id, owner, generation)
            if row["acquisition_id"] and row["acquisition_id"] != acquisition_id:
                raise IntelligenceError("IDEMPOTENCY_CONFLICT", "Monitor job already bound to an acquisition")
            connection.execute("UPDATE monitor_jobs SET acquisition_id=? WHERE tenant=? AND id=?", (acquisition_id, self.tenant, job_id))
            self._event(connection, "acquisition_bound", {"job_id": job_id, "acquisition_id": acquisition_id})

    def bind_plan(self, job_id, owner, generation, plan):
        value = _json(plan)
        with self._connection(write=True) as connection:
            row = self._lease(connection, job_id, owner, generation)
            if row["plan_json"] is not None:
                if row["plan_json"] != value:
                    raise IntelligenceError("PLAN_CONFLICT", "Monitor acquisition plan is already frozen")
                return json.loads(row["plan_json"])
            connection.execute("UPDATE monitor_jobs SET plan_json=? WHERE tenant=? AND id=?", (value, self.tenant, job_id))
            self._event(connection, "monitor_plan_frozen", {"job_id": job_id, "plan_hash": plan["plan_hash"]})
            return plan

    def complete(self, job_id, owner, generation, outcome):
        if not isinstance(outcome, dict):
            raise IntelligenceError("CONTRACT_INVALID", "Monitor outcome must be an object")
        _json(outcome)
        documents = outcome.get("document_versions", [])
        if (not isinstance(documents, list) or len(documents) > 1024
                or any(not isinstance(doc, (str, dict)) or isinstance(doc, dict) and not isinstance(doc.get("id"), str) for doc in documents)):
            raise IntelligenceError("CONTRACT_INVALID", "Monitor outcome exceeds document limit")
        observed = outcome.get("observations", [])
        if not isinstance(observed, list) or len(observed) > 1024 or any(not isinstance(row, dict) for row in observed):
            raise IntelligenceError("CONTRACT_INVALID", "Monitor observation limit exceeded")
        if not isinstance(outcome.get("usage", {}), dict):
            raise IntelligenceError("CONTRACT_INVALID", "Usage must be typed counters")
        requests = outcome.get("usage", {}).get("requests", 0)
        _integer(requests, 0, 1000000, "actual source requests")
        received = outcome.get("usage", {}).get("bytes", 0)
        _integer(received, 0, 1024**4, "actual received bytes")
        uncertain = outcome.get("requires_reconciliation", False) or any(o.get("delivery") == "unknown" or o.get("requires_reconciliation") for o in observed)
        errors = [o.get("error") for o in observed if o.get("error")]
        try:
            retry_after = max([float(o.get("retry_after") or 0) for o in observed] + [0.0])
        except (TypeError, ValueError, OverflowError):
            raise IntelligenceError("CONTRACT_INVALID", "Invalid source retry interval") from None
        if not math.isfinite(retry_after) or retry_after < 0:
            raise IntelligenceError("CONTRACT_INVALID", "Invalid source retry interval")
        success = outcome.get("acquisition_complete", False) and not errors and not uncertain
        fingerprint = digest(sorted([{key: d.get(key) for key in ("id", "version", "raw_sha256", "text_sha256", "extractor_config_sha256")} if isinstance(d, dict)
            else {"id": d} for d in documents], key=lambda x: x["id"])) if documents else None
        now = self._now()
        with self._connection(write=True) as connection:
            row = self._lease(connection, job_id, owner, generation)
            breached = requests > row["reservation"] or received > row["byte_reservation"]
            if breached:
                success = False
            unavailable = bool(row["fingerprint"] and any(o.get("http_status") in {404, 410} for o in observed))
            changed = bool(success and fingerprint and row["fingerprint"] != fingerprint
                or unavailable and row["health"] != "source_unavailable")
            unchanged = success and (outcome.get("not_modified") is True or fingerprint == row["fingerprint"] or not fingerprint)
            if uncertain:
                state, health = "reconciliation", "usage_or_delivery_unknown"
            else:
                state, health = ("completed", "healthy") if success else ("failed", "quota_bound_breached" if breached else "backoff")
                if not breached and errors:
                    health = next((_PAUSED[code] for code in errors if code in _PAUSED), health)
                if unavailable and not breached:
                    health = "source_unavailable"
                connection.execute("UPDATE monitor_budget SET held=MAX(0,held-?),spent=spent+?,bytes_held=MAX(0,bytes_held-?),bytes_spent=bytes_spent+? WHERE tenant=? AND day=? AND source_id=?", (row["reservation"], requests, row["byte_reservation"], received, self.tenant, row["quota_day"], row["source_id"]))
            failures = 0 if success else row["failures"] + 1
            delay = max(row["cadence"], retry_after, min(86400, 2 ** min(16, failures)))
            cursor = outcome.get("cursor", {}) if success else None
            if cursor is not None and (not isinstance(cursor, dict) or len(canonical(cursor)) > 65536):
                raise IntelligenceError("CONTRACT_INVALID", "Invalid bounded source cursor")
            connection.execute("UPDATE monitor_jobs SET state=?,result=?,owner=NULL,lease_until=NULL WHERE tenant=? AND id=?", (state, _json({**outcome, "changed": changed, "unchanged": unchanged}), self.tenant, job_id))
            connection.execute("UPDATE monitor_units SET next_due=?,health=?,failures=?,last_success=CASE WHEN ? THEN ? ELSE last_success END,last_change=CASE WHEN ? THEN ? ELSE last_change END,fingerprint=CASE WHEN ? AND ? IS NOT NULL THEN ? ELSE fingerprint END,cursor_json=CASE WHEN ? THEN ? ELSE cursor_json END WHERE tenant=? AND id=?", (now + delay, health, failures, success, now, changed, now, success, fingerprint, fingerprint, success, _json(cursor or {}), self.tenant, row["unit_id"]))
            if retry_after:
                connection.execute("INSERT INTO monitor_rate VALUES(?,?,?) ON CONFLICT(tenant,domain) DO UPDATE SET next_allowed=MAX(next_allowed,excluded.next_allowed)", (self.tenant, row["domain"], now + retry_after))
            self._event(connection, "monitor_settled", {"job_id": job_id, "state": state, "changed": changed, "requests": requests, "retry_after": retry_after})
            if changed:
                prior = connection.execute("SELECT revision,record FROM monitor_outbox WHERE tenant=? AND topic_id=? AND version=?", (self.tenant, row["topic_id"], row["topic_version"])).fetchone()
                revision = prior["revision"] + 1 if prior else 1
                previous = json.loads(prior["record"]) if prior else {}
                source_ids = sorted(set(previous.get("source_ids", [])) | {row["source_id"]})
                value = {"topic_id": row["topic_id"], "version": row["topic_version"], "revision": revision,
                    "latest_job_id": job_id, "source_id": row["source_id"], "changed_at": timestamp(self.clock()),
                    "source_ids": source_ids,
                    "change_reason": "source_withdrawal_or_unavailable" if unavailable else "original_or_extractor_version_changed",
                    "state": "needs_review", "automatic_model_started": False, "external_notification_sent": False}
                connection.execute("INSERT INTO monitor_outbox VALUES(?,?,?,?,?,?) ON CONFLICT(tenant,topic_id,version) DO UPDATE SET state=excluded.state,revision=excluded.revision,record=excluded.record", (self.tenant, row["topic_id"], row["topic_version"], "needs_review", revision, _json(value)))
            return {"job_id": job_id, "state": state, "changed": changed, "next_due": timestamp(aware(self.clock()) + timedelta(seconds=delay))}

    def fail(self, job_id, owner, generation, code, *, retry_after=0, requires_reconciliation=False):
        identifier(code, "failure code")
        return self.complete(job_id, owner, generation, {"acquisition_complete": False,
            "requires_reconciliation": requires_reconciliation, "document_versions": [],
            "observations": [{"error": code, "retry_after": retry_after,
                "delivery": "unknown" if requires_reconciliation else "not_sent"}], "usage": {"requests": 0}})

    def status(self, watch_id=None, *, limit=100):
        _integer(limit, 1, 1000, "status limit")
        if not self.database.exists():
            if watch_id is not None:
                raise IntelligenceError("EVIDENCE_NOT_FOUND", "Watch unavailable to tenant")
            return {"schema": SCHEMA, "tenant": self.tenant, "sources": [], "queue": {},
                "queue_capacity": self.max_queue, "oldest_active_age_seconds": None,
                "pending_changes": [], "budget": [], "automatic_model_started": False}
        with self._connection() as connection:
            predicate, params = "tenant=?", [self.tenant]
            if watch_id is not None:
                identifier(watch_id)
                predicate += " AND watch_id=?"
                params.append(watch_id)
                if not connection.execute("SELECT 1 FROM monitor_watches WHERE tenant=? AND id=?", (self.tenant, watch_id)).fetchone():
                    raise IntelligenceError("EVIDENCE_NOT_FOUND", "Watch unavailable to tenant")
            rows = connection.execute("SELECT * FROM monitor_units WHERE " + predicate + " ORDER BY next_due,id LIMIT ?", (*params, limit + 1)).fetchall()
            now = self._now()
            sources = []
            for row in rows[:limit]:
                value = dict(row)
                value["cursor"] = json.loads(value.pop("cursor_json"))
                value["source_stale"] = row["last_success"] is not None and now - row["last_success"] > row["cadence"] * 2
                value["connection"] = self.catalog.connection(row["source_id"], now=self.clock())
                value["connection"].update(connected=row["last_success"] is not None, last_success=row["last_success"])
                value["health"] = _PAUSED.get(value["connection"]["reason"], value["health"])
                value["health_reason"] = _PAUSE_REASONS.get(value["health"])
                sources.append(value)
            queue = dict(connection.execute("SELECT state,COUNT(*) FROM monitor_jobs WHERE tenant=? GROUP BY state", (self.tenant,)).fetchall())
            oldest = connection.execute("SELECT MIN(created) FROM monitor_jobs WHERE tenant=? AND state IN ('queued','running','reconciliation')", (self.tenant,)).fetchone()[0]
            outbox = [json.loads(row[0]) for row in connection.execute("SELECT record FROM monitor_outbox WHERE tenant=? AND state='needs_review' ORDER BY topic_id LIMIT ?", (self.tenant, limit)).fetchall()]
            budget = [dict(row) for row in connection.execute("SELECT day,source_id,spent,held,bytes_spent,bytes_held FROM monitor_budget WHERE tenant=? ORDER BY day DESC,source_id LIMIT ?", (self.tenant, limit)).fetchall()]
            return {"schema": SCHEMA, "tenant": self.tenant, "sources": sources, "queue": queue,
                "queue_capacity": self.max_queue, "oldest_active_age_seconds": None if oldest is None else max(0, now - oldest),
                "pending_changes": outbox, "budget": budget, "source_rows_limited": len(rows) > limit,
                "automatic_model_started": False}

    def source_health(self):
        aggregated = {}
        if self.database.exists():
            with self._connection() as connection:
                rows = connection.execute("SELECT u.source_id,MAX(u.last_success) last_success,COUNT(*) watch_count,MAX(CASE WHEN u.last_success IS NOT NULL AND ?-u.last_success>u.cadence*2 THEN 1 ELSE 0 END) source_stale,(SELECT v.health FROM monitor_units v WHERE v.tenant=u.tenant AND v.source_id=u.source_id ORDER BY v.last_attempt DESC LIMIT 1) health FROM monitor_units u WHERE u.tenant=? GROUP BY u.source_id", (self._now(), self.tenant)).fetchall()
                aggregated = {row["source_id"]: dict(row) for row in rows}
        result = []
        for source_id in self.catalog.sources:
            value = self.catalog.connection(source_id, now=self.clock())
            observed = aggregated.get(source_id, {})
            successful = observed.get("last_success") is not None
            value.update(last_success=observed.get("last_success"),
                connected=successful, source_stale=bool(observed.get("source_stale")),
                material_types=self.catalog.sources[source_id]["material_types"],
                watch_count=observed.get("watch_count", 0), health=observed.get("health", "never_observed"))
            value["health"] = _PAUSED.get(value["reason"], value["health"])
            value["health_reason"] = _PAUSE_REASONS.get(value["health"])
            if successful and value["reason"] == "NOT_YET_OBSERVED":
                value["reason"] = None
            result.append(value)
        return {"schema": "tiger.intelligence-source-health/v1", "sources": result}

    def acknowledge_changes(self, topic_id, version, revision, report_reference):
        """CAS a reviewed local marker; the caller separately verifies its report."""
        identifier(topic_id, "topic")
        _integer(version, 1, 1000000, "topic version")
        _integer(revision, 1, 1000000000, "change revision")
        if (not isinstance(report_reference, dict) or not isinstance(report_reference.get("report_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", report_reference["report_sha256"]) is None
                or not isinstance(report_reference.get("job_id"), str)):
            raise IntelligenceError("CONTRACT_INVALID", "A verified report identity reference is required")
        _json(report_reference)
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT revision,state FROM monitor_outbox WHERE tenant=? AND topic_id=? AND version=?", (self.tenant, topic_id, version)).fetchone()
            if row is None:
                return {"acknowledged": False, "reason": "NO_PENDING_CHANGES"}
            if row["revision"] != revision:
                raise IntelligenceError("REVISION_CONFLICT", "New changes arrived after the reviewed version")
            connection.execute("UPDATE monitor_outbox SET state='reviewed' WHERE tenant=? AND topic_id=? AND version=?", (self.tenant, topic_id, version))
            self._event(connection, "changes_acknowledged", {"topic_id": topic_id, "version": version,
                "revision": revision, "report_reference": report_reference})
            return {"acknowledged": True, "duplicate": row["state"] == "reviewed", "revision": revision}

    def job(self, job_id):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM monitor_jobs WHERE tenant=? AND id=?", (self.tenant, identifier(job_id))).fetchone()
            if row is None:
                raise IntelligenceError("EVIDENCE_NOT_FOUND", "Monitor job unavailable")
            value = dict(row)
            value["result"] = json.loads(value["result"]) if value["result"] else None
            value["plan"] = json.loads(value.pop("plan_json")) if value["plan_json"] else None
            return value

    def begin_reconciliation(self, job_id, owner, *, lease_seconds=180):
        """Claim an existing unresolved identity; this does not send or release quota."""
        identifier(owner, "reconciliation owner")
        _integer(lease_seconds, 1, 900, "reconciliation lease")
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM monitor_jobs WHERE tenant=? AND id=?", (self.tenant, identifier(job_id))).fetchone()
            if row is None:
                raise IntelligenceError("EVIDENCE_NOT_FOUND", "Monitor job unavailable")
            if row["state"] != "reconciliation" or not row["acquisition_id"]:
                raise IntelligenceError("PLAN_CONFLICT", "Job has no unresolved acquisition identity")
            generation = row["generation"] + 1
            connection.execute("UPDATE monitor_jobs SET state='running',owner=?,generation=?,lease_until=? WHERE tenant=? AND id=?", (owner, generation, self._now() + lease_seconds, self.tenant, job_id))
            self._event(connection, "reconciliation_started", {"job_id": job_id, "acquisition_id": row["acquisition_id"], "generation": generation})
            return {**dict(row), "generation": generation, "owner": owner}

    def verify_events(self):
        """Explicit full audit, separate from routine status/worker progress."""
        previous, count = "0" * 64, 0
        with self._connection() as connection:
            cursor = connection.execute("SELECT * FROM monitor_events WHERE tenant=? ORDER BY sequence", (self.tenant,))
            for row in cursor:
                value = json.loads(row["record"])
                if row["previous_hash"] != previous or value.get("previous_hash") != previous or digest(value) != row["event_hash"]:
                    raise IntelligenceError("HASH_MISMATCH", "Monitor event chain differs")
                previous, count = row["event_hash"], count + 1
        return {"events": count, "event_head": previous}


class MonitoringWorker:
    """One leased bounded unit per tick; hosting may repeat ticks until deadline."""

    def __init__(self, store, service_factory, *, owner=None):
        self.store, self.service_factory = store, service_factory
        self.owner = identifier(owner or "worker_" + uuid.uuid4().hex)

    def _plan(self, job, service):
        from .planner import build_plan
        topic = job["topic"]
        catalog = self.store.catalog
        registry = service.registry
        universe = {}
        entities = {}
        for identity in topic["entity_ids"]:
            row = catalog.entities[identity]
            entities[identity] = row
            tickers = row.get("identifiers", {}).get("ticker", [])
            if row["type"] == "security" and tickers:
                universe[tickers[0]] = identity
        materials = [key for key, value in topic["material_policy"].items()
            if value["status"] != "not_applicable" and key in catalog.sources[job["source_id"]]["material_types"]]
        cells = []
        for material in materials or ["news_events"]:
            for identity in topic["entity_ids"] or [None]:
                target = identity or "topic:" + topic["topic_id"]
                cells.append({"target_id": target, "entity_id": identity, "topic": topic["topic_id"],
                    "question": topic["question"] + " " + DIMENSIONS[material]["label"],
                    "query": topic["question"] + " " + (catalog.entities[identity]["name"] if identity else "") + " " + DIMENSIONS[material]["label"],
                    "required": topic["material_policy"][material]["status"] == "required",
                    "material_type": material, "window_days": DIMENSIONS[material]["window_days"]})
        sources = [job["source_id"]]
        spec = registry.get(job["source_id"])
        if spec.kind == "search" and "web_page" in registry.source_ids():
            sources.append("web_page")
        if spec.kind == "sec_submissions" and "sec_directory" in registry.source_ids():
            sources.insert(0, "sec_directory")
        limits = dict(UNIT_LIMITS)
        limits["requests"] = job["reservation"]
        limits["bytes"] = min(limits["bytes"], job["byte_reservation"])
        limits["deadline_seconds"] = max(1, min(UNIT_LIMITS["deadline_seconds"], int(job["deadline"] - self.store._now())))
        request = {"run_id": job["id"], "as_of": timestamp(self.store.clock()),
            "universe": universe, "frontier": list(universe), "entities": entities, "holdings": [],
            "frontier_manifest_hash": topic["scope_hash"], "required_cells": cells,
            "source_ids": sources, "limits": limits}
        plan = build_plan(request, registry)
        for query in plan["query_plan"]:
            query["request"]["context"].update(monitor_watch_id=job["watch_id"],
                monitor_cursor=job["cursor"], conditional={key: job["cursor"][key] for key in ("etag", "last_modified") if key in job["cursor"]},
                monitor_topic_id=topic["topic_id"])
        value = {key: val for key, val in plan.items() if key != "plan_hash"}
        return {**value, "plan_hash": digest(value)}

    def tick(self):
        enqueued = self.store.enqueue_due(limit=min(32, self.store.max_queue))
        job = self.store.claim(self.owner, request_reservation=UNIT_LIMITS["requests"],
            lease_seconds=UNIT_LIMITS["deadline_seconds"] + 60)
        if job is None:
            return {"schema": SCHEMA, "state": "idle", "schedule": enqueued}
        try:
            if job["deadline"] <= self.store._now():
                return self.store.fail(job["id"], self.owner, job["generation"], "DEADLINE_EXCEEDED",
                    requires_reconciliation=bool(job["acquisition_id"]))
            service = self.service_factory()
            if service.archive.tenant != self.store.tenant:
                raise IntelligenceError("POLICY_DENIED", "Monitor service tenant differs")
            if service.registry.public_manifest()["sha256"] != self.store.catalog.registry.public_manifest()["sha256"]:
                raise IntelligenceError("POLICY_DENIED", "Monitor service source policy differs")
            connection = self.store.catalog.connection(job["source_id"], now=self.store.clock())
            adapter = service.adapters.get(job["source_id"])
            availability = adapter.available() if adapter is not None and callable(getattr(adapter, "available", None)) else {"available": adapter is not None}
            if connection["reason"] in _PAUSED or not availability["available"]:
                return self.store.fail(job["id"], self.owner, job["generation"],
                    connection["reason"] if connection["reason"] in _PAUSED else availability.get("reason") or "SOURCE_NOT_CONNECTED")
            if hasattr(service, "use_host_limits"):
                service.use_host_limits(self.store.root)
            if job["acquisition_id"]:
                acquisition_id = job["acquisition_id"]
                attempts = service.archive.attempts(acquisition_id)
                if any(row["status"] in {"inflight", "unknown"} for row in attempts):
                    return self.store.fail(job["id"], self.owner, job["generation"], "SOURCE_RECONCILIATION_REQUIRED", requires_reconciliation=True)
            else:
                plan = json.loads(job["plan_json"]) if job["plan_json"] else self._plan(job, service)
                plan = self.store.bind_plan(job["id"], self.owner, job["generation"], plan)
                acquisition = service.create_job(plan, "watch-cycle:" + job["id"])
                acquisition_id = acquisition["job_id"]
                self.store.bind_acquisition(job["id"], self.owner, job["generation"], acquisition_id)
                job["acquisition_id"] = acquisition_id
            acquired = service.collect(acquisition_id)
            if hasattr(service, "monitor_outcome"):
                outcome = service.monitor_outcome(acquisition_id)
            else:
                outcome = self._outcome(service, acquisition_id, acquired)
            result = self.store.complete(job["id"], self.owner, job["generation"], outcome)
            return {"schema": SCHEMA, **result, "acquisition_job_id": acquisition_id, "schedule": enqueued}
        except IntelligenceError as error:
            if error.code == "LEASE_CONFLICT":
                raise
            return self.store.fail(job["id"], self.owner, job["generation"], error.code,
                requires_reconciliation=bool(job["acquisition_id"]))
        except Exception:
            return self.store.fail(job["id"], self.owner, job["generation"], "SOURCE_WORKER_FAILED",
                requires_reconciliation=bool(job["acquisition_id"]))

    def reconcile(self, job_id):
        """Use the original persisted source receipts, never a guessed no-send."""
        record = self.store.job(job_id)
        if record["state"] != "reconciliation" or not record["acquisition_id"]:
            raise IntelligenceError("PLAN_CONFLICT", "Monitor job is not awaiting reconciliation")
        service = self.service_factory()
        if service.archive.tenant != self.store.tenant:
            raise IntelligenceError("POLICY_DENIED", "Reconciliation service tenant differs")
        if service.registry.public_manifest()["sha256"] != self.store.catalog.registry.public_manifest()["sha256"]:
            raise IntelligenceError("POLICY_DENIED", "Reconciliation source policy differs")
        attempts = service.archive.attempts(record["acquisition_id"])
        if any(row["status"] in {"inflight", "unknown"} for row in attempts):
            return {"job_id": job_id, "state": "reconciliation", "reason": "SOURCE_RECONCILIATION_REQUIRED"}
        acquired = service.evidence(job_id=record["acquisition_id"])
        outcome = service.monitor_outcome(record["acquisition_id"]) if hasattr(service, "monitor_outcome") else self._outcome(service, record["acquisition_id"], acquired)
        if outcome.get("requires_reconciliation"):
            return {"job_id": job_id, "state": "reconciliation", "reason": "USAGE_RECONCILIATION_REQUIRED"}
        lease = self.store.begin_reconciliation(job_id, self.owner)
        return self.store.complete(job_id, self.owner, lease["generation"], outcome)

    def run(self, *, max_ticks=12, max_seconds=3600, interval_seconds=1, sleeper=time.sleep, stop=None):
        _integer(max_ticks, 1, 10000, "monitor tick count")
        _integer(max_seconds, 1, 86400, "monitor run duration")
        _integer(interval_seconds, 1, 3600, "monitor poll interval")
        deadline, outcomes = time.monotonic() + max_seconds, []
        for index in range(max_ticks):
            if time.monotonic() >= deadline or stop is not None and stop():
                break
            outcomes.append(self.tick())
            if index + 1 < max_ticks:
                wait = min(interval_seconds, max(0, deadline - time.monotonic()))
                if wait:
                    sleeper(wait)
        return {"schema": SCHEMA, "state": "stopped", "ticks": outcomes,
            "automatic_model_started": False, "status": self.store.status()}

    @staticmethod
    def _outcome(service, acquisition_id, acquired):
        observations, unknown = [], False
        for attempt in service.archive.attempts(acquisition_id):
            result = attempt.get("result") or {}
            observation = result.get("observation") or {}
            error = result.get("error") or {}
            details = error.get("details") or {}
            usage = result.get("usage") or {}
            requires = attempt["status"] in {"inflight", "unknown"} or (
                service.registry.get(attempt["source_id"]).kind == "search" and usage.get("requires_reconciliation", False))
            unknown = unknown or requires
            observations.append({"source_id": attempt["source_id"], "http_status": observation.get("http_headers", {}).get(":status") or details.get("http_status"),
                "retry_after": details.get("retry_after"), "error": error.get("code"),
                "delivery": details.get("delivery", "returned"), "requires_reconciliation": requires,
                "received_at": observation.get("received_at")})
        docs = acquired.get("documents", [])
        return {"acquisition_complete": bool(observations) and not any(o["error"] for o in observations),
            "document_versions": [{"id": d["id"], "version": d["version"]} for d in docs],
            "observations": observations, "requires_reconciliation": unknown,
            "usage": acquired.get("usage", {}), "cursor": {}, "gaps": acquired.get("gaps", [])}
