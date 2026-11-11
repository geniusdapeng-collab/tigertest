"""Durable, opt-in work graph with fenced leases and append-only observations.

The SQLite tables are projections. Every mutation records their changed rows in
the same short transaction, and verify replays the hash chain independently.
No provider callback or semantic validator runs while a write lock is held.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import stat
import time
from typing import Callable

from ..agent_evidence import (EvidenceInvalid, atomic_bytes, checked_root, digest,
                              json_bytes, read_bytes, strict_json, verify_ref)
from .contracts import (CONTRACT, DISPOSITIONS, EVENT_SCHEMA, OBSERVATION_SCHEMA,
                        PLAN_SCHEMA, REQUEST_SCHEMA, RESULT_SCHEMA, AttemptHandle,
                        UnitLease, UnitSpec, WorkUnitError, detached, hash_value,
                        identifier, identifiers, instant, known_usage, plain_label,
                        timestamp_number, validate_manifest, value_hash)

APPLICATION_ID = 0x54574B31
SCHEMA_VERSION = 1
DEFAULT_WRITE_BUSY_RETRY_MS = 10000
WRITE_LOCK_ATTEMPT_MS = 250
TABLES = {
    "meta": ("key", ("key", "value")),
    "units": ("unit_id", ("unit_id", "spec", "state", "owner", "epoch", "lease_until",
                            "result_ref", "error_code", "retryable")),
    "attempts": ("attempt_id", ("attempt_id", "unit_id", "ordinal", "operation",
                               "request_ref", "reservation", "state", "epoch", "owner",
                               "observation_id", "usage")),
    "observations": ("observation_id", ("observation_id", "attempt_id", "ref", "usage", "late")),
    "retry_claims": ("slot", ("slot", "unit_id", "attempt_id")),
}
JSON_COLUMNS = frozenset({"value", "spec", "result_ref", "request_ref", "reservation", "usage", "ref"})


class WorkStore:
    def __init__(self, root: Path, manifest: dict, *, clock=None, busy_timeout_ms=15000,
                 write_busy_retry_ms=DEFAULT_WRITE_BUSY_RETRY_MS):
        self.root = Path(root)
        self.database = self.root / "work-units.sqlite3"
        self.manifest = validate_manifest(manifest)
        self.manifest_sha256 = value_hash(self.manifest)
        self.clock = time.time if clock is None else clock
        if (not callable(self.clock) or type(busy_timeout_ms) is not int
                or not 0 <= busy_timeout_ms <= 60000
                or type(write_busy_retry_ms) is not int
                or not 0 <= write_busy_retry_ms <= 60000):
            raise WorkUnitError("CONTRACT_INVALID", "Invalid task store clock or timeout")
        self.busy_timeout_ms = busy_timeout_ms
        self.write_busy_retry_ms = write_busy_retry_ms

    @classmethod
    def create(cls, root, manifest, **kwargs):
        result = cls(Path(root), manifest, **kwargs)
        try:
            checked_root(result.root, create=True)
            result._safe_database()
            try:
                descriptor = os.open(result.database, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                # Initialization can die after the SQLite commit and before
                # publishing its redundant immutable manifest. Rebuild only
                # from a fully replay-verified matching new-contract DB.
                if not (result.root / "work-manifest.json").exists():
                    with result._tx() as connection:
                        if result._meta(connection, "manifest") != result.manifest:
                            raise WorkUnitError("MANIFEST_MISMATCH", "Work-unit manifest differs")
                    result.verify()
                    try:
                        atomic_bytes(result.root, "work-manifest.json", json_bytes(result.manifest), exclusive=True)
                    except FileExistsError:
                        if read_bytes(result.root, "work-manifest.json") != json_bytes(result.manifest):
                            raise WorkUnitError("MANIFEST_MISMATCH", "Work-unit manifest differs") from None
                with result._tx(write=True) as connection:
                    result._ensure_indexes(connection)
                return cls.open(root, manifest, **kwargs)
            else:
                os.close(descriptor)
            connection = sqlite3.connect(result.database, isolation_level=None)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                for statement in (
                    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                    "CREATE TABLE units(unit_id TEXT PRIMARY KEY,spec TEXT NOT NULL,state TEXT NOT NULL,owner TEXT,epoch INTEGER NOT NULL,lease_until REAL,result_ref TEXT,error_code TEXT,retryable INTEGER NOT NULL)",
                    "CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY,unit_id TEXT NOT NULL REFERENCES units(unit_id),ordinal INTEGER NOT NULL,operation TEXT NOT NULL,request_ref TEXT NOT NULL,reservation TEXT NOT NULL,state TEXT NOT NULL,epoch INTEGER NOT NULL,owner TEXT NOT NULL,observation_id TEXT,usage TEXT,UNIQUE(unit_id,ordinal))",
                    "CREATE TABLE observations(observation_id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),ref TEXT NOT NULL,usage TEXT,late INTEGER NOT NULL)",
                    "CREATE TABLE retry_claims(slot INTEGER PRIMARY KEY CHECK(slot=1),unit_id TEXT NOT NULL REFERENCES units(unit_id),attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id))",
                    "CREATE TABLE events(sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL,event_sha256 TEXT NOT NULL UNIQUE)",
                    "CREATE INDEX attempts_unit_state ON attempts(unit_id,state)",
                    "CREATE INDEX units_state ON units(state)",
                    "CREATE INDEX attempts_state_unit ON attempts(state,unit_id)",
                ):
                    connection.execute(statement)
                for table in ("observations", "retry_claims", "events"):
                    for operation in ("UPDATE", "DELETE"):
                        connection.execute(f"CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable evidence'); END")
                changes = {"meta": [{"key": "manifest", "value": result.manifest},
                                     {"key": "plan", "value": None},
                                     {"key": "cancel", "value": False}]}
                result._apply(connection, changes)
                result._record(connection, "created", changes)
                connection.commit()
            finally:
                connection.close()
            atomic_bytes(result.root, "work-manifest.json", json_bytes(result.manifest), exclusive=True)
            return result
        except WorkUnitError:
            raise
        except (OSError, sqlite3.Error, EvidenceInvalid) as exc:
            raise result._error(exc) from None

    @classmethod
    def open(cls, root, manifest, *, verify_checks=True, **kwargs):
        result = cls(Path(root), manifest, **kwargs)
        try:
            checked_root(result.root)
            if read_bytes(result.root, "work-manifest.json") != json_bytes(result.manifest):
                raise WorkUnitError("MANIFEST_MISMATCH", "Work-unit manifest differs")
            with result._tx() as connection:
                if result._meta(connection, "manifest") != result.manifest:
                    raise WorkUnitError("MANIFEST_MISMATCH", "Work-unit manifest differs")
            if verify_checks:
                result.verify()
            return result
        except WorkUnitError:
            raise
        except (OSError, sqlite3.Error, EvidenceInvalid, ValueError) as exc:
            raise result._error(exc) from None

    def _error(self, exc):
        busy = isinstance(exc, sqlite3.OperationalError) and getattr(exc, "sqlite_errorcode", None) in {
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        return WorkUnitError("STORE_BUSY" if busy else "STORE_INVALID",
                             "Work-unit store is busy" if busy else "Work-unit persistence is invalid",
                             cause_type=type(exc).__name__)

    @staticmethod
    def _ensure_indexes(connection):
        for statement in (
                "CREATE INDEX IF NOT EXISTS attempts_unit_state ON attempts(unit_id,state)",
                "CREATE INDEX IF NOT EXISTS units_state ON units(state)",
                "CREATE INDEX IF NOT EXISTS attempts_state_unit ON attempts(state,unit_id)"):
            connection.execute(statement)

    @staticmethod
    def _busy_error(exc):
        return (isinstance(exc, sqlite3.OperationalError)
                and getattr(exc, "sqlite_errorcode", None) in {
                    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})

    def _safe_database(self):
        checked_root(self.root)
        for name in ("work-units.sqlite3", "work-units.sqlite3-wal", "work-units.sqlite3-shm", "work-units.sqlite3-journal"):
            target = self.root / name
            try:
                info = target.lstat()
            except FileNotFoundError:
                # SQLite removes WAL/SHM when its last connection closes. A
                # missing sidecar is normal; the main DB is checked by _tx.
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 0 and name != "work-units.sqlite3":
                # A concurrent unlink can be observed as a zero-link regular
                # sidecar. It has no path to follow or overwrite. Symlinks,
                # other file types and hardlinks remain rejected below.
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkUnitError("STORE_INVALID", "Unsafe work-unit database", cause_type="UnsafeFileType")

    @contextmanager
    def _tx(self, write=False):
        connection = None
        try:
            self._safe_database()
            if not self.database.exists():
                raise WorkUnitError("STORE_INVALID", "Work-unit database is missing")
            connect_timeout = (min(self.busy_timeout_ms, WRITE_LOCK_ATTEMPT_MS) / 1000
                               if write else self.busy_timeout_ms / 1000)
            connection = sqlite3.connect(self.database, timeout=connect_timeout, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                f"PRAGMA busy_timeout="
                f"{min(self.busy_timeout_ms, WRITE_LOCK_ATTEMPT_MS) if write else self.busy_timeout_ms}")
            if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                    or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
                raise WorkUnitError("STORE_VERSION_INVALID", "Unrecognized work-unit database contract")
            deadline = time.monotonic() + self.write_busy_retry_ms / 1000
            while True:
                try:
                    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    break
                except sqlite3.OperationalError as exc:
                    if not write or not self._busy_error(exc) or time.monotonic() >= deadline:
                        raise
                    time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            yield connection
            if write:
                while True:
                    try:
                        connection.commit()
                        break
                    except sqlite3.OperationalError as exc:
                        if not self._busy_error(exc) or time.monotonic() >= deadline:
                            raise
                        time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            else:
                connection.commit()
        except (sqlite3.Error, OSError, EvidenceInvalid, ValueError) as exc:
            if connection is not None:
                connection.rollback()
            if isinstance(exc, WorkUnitError):
                raise
            raise self._error(exc) from None
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _now(self):
        return timestamp_number(self.clock())

    def _admission(self, connection):
        if self._meta(connection, "cancel"):
            raise WorkUnitError("TASK_CANCELLED", "Work-unit task was cancelled")
        if self._now() >= instant(self.manifest["deadline_at"]):
            raise WorkUnitError("TASK_DEADLINE", "Work-unit deadline has expired")

    def _rows(self, connection, table):
        key, columns = TABLES[table]
        rows = []
        for raw in connection.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {key}"):
            rows.append({name: strict_json(raw[name]) if name in JSON_COLUMNS and raw[name] is not None else raw[name]
                         for name in columns})
        return rows

    def _row(self, connection, table, identity):
        key, columns = TABLES[table]
        raw = connection.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {key}=?", (identity,)).fetchone()
        if raw is None:
            return None
        return {name: strict_json(raw[name]) if name in JSON_COLUMNS and raw[name] is not None else raw[name]
                for name in columns}

    def _meta(self, connection, key):
        row = self._row(connection, "meta", key)
        if row is None:
            raise WorkUnitError("STORE_INVALID", "Missing work-unit metadata")
        return row["value"]

    def _apply(self, connection, changes):
        for table, rows in changes.items():
            key, columns = TABLES[table]
            for row in rows:
                values = [json_bytes(row[name]).decode() if name in JSON_COLUMNS and (row[name] is not None or table == "meta") else row[name]
                          for name in columns]
                exists = connection.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (row[key],)).fetchone()
                if exists:
                    connection.execute(f"UPDATE {table} SET {','.join(name+'=?' for name in columns if name != key)} WHERE {key}=?",
                                       [values[i] for i, name in enumerate(columns) if name != key] + [row[key]])
                else:
                    connection.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)

    def _record(self, connection, kind, changes):
        last = connection.execute("SELECT sequence,event_sha256 FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
        payload = {"schema": EVENT_SCHEMA, "manifest_sha256": self.manifest_sha256,
                   "sequence": 1 if last is None else last[0] + 1,
                   "previous_event_sha256": None if last is None else last[1],
                   "kind": kind, "at": self._now(), "changes": changes}
        encoded = json_bytes(payload)
        connection.execute("INSERT INTO events(sequence,payload,event_sha256) VALUES(?,?,?)",
                           (payload["sequence"], encoded.decode(), digest(encoded)))

    def _change(self, connection, kind, changes):
        self._apply(connection, changes)
        self._record(connection, kind, changes)

    def _blob(self, value, schema):
        content = json_bytes(detached(value))
        relative = "artifacts/" + digest(content) + ".json"
        try:
            atomic_bytes(self.root, relative, content, exclusive=True)
        except FileExistsError:
            if read_bytes(self.root, relative) != content:
                raise WorkUnitError("EVIDENCE_HASH_MISMATCH", "Work-unit artifact collision") from None
        except (OSError, EvidenceInvalid) as exc:
            raise self._error(exc) from None
        return {"path": relative, "sha256": digest(content), "bytes": len(content), "schema": schema}

    def _read_blob(self, ref, schema):
        try:
            return strict_json(verify_ref(self.root, ref, schema=schema))
        except (OSError, ValueError, EvidenceInvalid) as exc:
            if isinstance(exc, WorkUnitError):
                raise
            raise WorkUnitError("EVIDENCE_HASH_MISMATCH", "Work-unit artifact is invalid") from None

    def read_artifact(self, ref, *, schema):
        return self._read_blob(ref, schema)

    def read_attempt_request(self, attempt_id):
        return self._read_blob(self.attempt(attempt_id)["request_ref"], REQUEST_SCHEMA)["request"]

    def _plan_value(self, specs, originals, *, revision=1, previous_plan_sha256=None):
        if not isinstance(specs, (list, tuple)) or not specs or not all(isinstance(spec, UnitSpec) for spec in specs):
            raise WorkUnitError("PLAN_INVALID", "An explicit nonempty unit graph is required")
        ordered = [spec.to_dict() for spec in specs]
        names = [spec["unit_id"] for spec in ordered]
        if len(names) != len(set(names)):
            raise WorkUnitError("PLAN_INVALID", "Duplicate unit identity")
        by_name = {spec["unit_id"]: spec for spec in ordered}
        visited, visiting = set(), set()
        def visit(name):
            if name in visiting or name not in by_name:
                raise WorkUnitError("PLAN_INVALID", "Missing or cyclic unit dependency")
            if name in visited:
                return
            visiting.add(name)
            for dependency in by_name[name]["dependencies"]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
        for name in names:
            visit(name)
        originals = detached(originals)
        if type(originals) is not list:
            raise WorkUnitError("PLAN_INVALID", "Original document manifest is required")
        original_ids = []
        for row in originals:
            if type(row) is not dict or set(row) != {"id", "sha256"}:
                raise WorkUnitError("PLAN_INVALID", "Invalid original document identity")
            original_ids.append(identifier(row["id"], "document"))
            hash_value(row["sha256"])
        terminal = [name for spec in ordered for name in spec["terminal_documents"]]
        if (len(original_ids) != len(set(original_ids)) or sorted(terminal) != sorted(original_ids)
                or any(not set(spec["document_ids"]) <= set(original_ids) for spec in ordered)):
            raise WorkUnitError("PLAN_INVALID", "Every original requires exactly one terminal unit")
        value = {"schema": PLAN_SCHEMA, "manifest_sha256": self.manifest_sha256,
                 "revision": revision, "previous_plan_sha256": previous_plan_sha256,
                 "units": sorted(ordered, key=lambda spec: spec["unit_id"]),
                 "originals": sorted(originals, key=lambda row: row["id"])}
        return value

    @staticmethod
    def _new_unit(spec):
        return {"unit_id": spec["unit_id"], "spec": spec,
                "state": "blocked" if spec["dependencies"] else "ready", "owner": None,
                "epoch": 0, "lease_until": None, "result_ref": None,
                "error_code": None, "retryable": 0}

    def plan(self, specs, *, originals):
        value = self._plan_value(specs, originals)
        with self._tx(write=True) as connection:
            existing = self._meta(connection, "plan")
            if existing is not None:
                if existing != value:
                    raise WorkUnitError("PLAN_IMMUTABLE", "Work-unit graph cannot replace its scope")
                return value_hash(value)
            self._admission(connection)
            units = [self._new_unit(spec) for spec in value["units"]]
            self._change(connection, "planned", {"meta": [{"key": "plan", "value": value}], "units": units})
        return value_hash(value)

    def extend_plan(self, specs, *, originals):
        """Append a discovered wave without replacing a frozen earlier unit.

        Identical repeated identities are idempotent. Existing originals may
        be referenced by new analysis, while their terminal owner remains the
        original unit. One ledger and one format-reanswer CAS span all waves.
        """
        if not isinstance(specs, (list, tuple)) or not all(isinstance(spec, UnitSpec) for spec in specs):
            raise WorkUnitError("PLAN_INVALID", "An explicit unit extension is required")
        incoming = [spec.to_dict() for spec in specs]
        if len({spec["unit_id"] for spec in incoming}) != len(incoming):
            raise WorkUnitError("PLAN_INVALID", "Duplicate extended unit identity")
        originals = detached(originals)
        if type(originals) is not list:
            raise WorkUnitError("PLAN_INVALID", "An explicit original extension is required")
        for row in originals:
            if type(row) is not dict or set(row) != {"id", "sha256"}:
                raise WorkUnitError("PLAN_INVALID", "Invalid extended original identity")
            identifier(row["id"], "document")
            hash_value(row["sha256"])
        if len({row["id"] for row in originals}) != len(originals):
            raise WorkUnitError("PLAN_INVALID", "Duplicate extended original identity")
        with self._tx() as connection:
            before = self._meta(connection, "plan")
        if before is None:
            raise WorkUnitError("PLAN_MISSING", "Initial work-unit graph is required before extension")
        merged_units = {spec["unit_id"]: spec for spec in before["units"]}
        merged_originals = {row["id"]: row for row in before["originals"]}
        added = []
        for spec in incoming:
            previous = merged_units.get(spec["unit_id"])
            if previous is not None and previous != spec:
                raise WorkUnitError("PLAN_IMMUTABLE", "Existing work-unit inputs and contract cannot change")
            if previous is None:
                added.append(spec)
                merged_units[spec["unit_id"]] = spec
        for row in originals:
            previous = merged_originals.get(row["id"])
            if previous is not None and previous != row:
                raise WorkUnitError("PLAN_IMMUTABLE", "Existing original version cannot change")
            merged_originals[row["id"]] = row
        if not added and len(merged_originals) == len(before["originals"]):
            return value_hash(before)
        value = self._plan_value([UnitSpec.from_dict(spec) for spec in merged_units.values()],
                                 list(merged_originals.values()), revision=before["revision"] + 1,
                                 previous_plan_sha256=value_hash(before))
        with self._tx(write=True) as connection:
            self._admission(connection)
            if self._meta(connection, "plan") != before:
                raise WorkUnitError("PLAN_CHANGED", "Work-unit plan changed during extension; read and retry")
            self._change(connection, "plan_extended", {"meta": [{"key": "plan", "value": value}],
                                                       "units": [self._new_unit(spec) for spec in added]})
        return value_hash(value)

    def unit(self, unit_id):
        identifier(unit_id, "unit")
        with self._tx() as connection:
            row = self._row(connection, "units", unit_id)
        if row is None:
            raise WorkUnitError("UNIT_MISSING", "Work-unit does not exist")
        return row

    def _lease(self, connection, lease, *, live=True):
        if not isinstance(lease, UnitLease) or (lease.tenant_id, lease.run_id, lease.manifest_sha256) != (
                self.manifest["tenant_id"], self.manifest["run_id"], self.manifest_sha256):
            raise WorkUnitError("LEASE_INVALID", "Work-unit lease identity differs")
        row = self._row(connection, "units", lease.unit_id)
        if (row is None or row["owner"] != lease.owner or row["epoch"] != lease.epoch
                or row["state"] != "running" or (live and row["lease_until"] <= self._now())):
            raise WorkUnitError("LEASE_FENCED", "Work-unit lease is expired or fenced")
        return row

    def check_lease(self, lease):
        with self._tx() as connection:
            self._lease(connection, lease)
            self._admission(connection)

    def claim_ready(self, owner, *, lease_seconds=60):
        return self._claim(None, owner, lease_seconds)

    def validated_unit_ids(self):
        """Read the verified-state projection without rehashing every result.

        Callers use this only after WorkStore.open() has verified the complete
        chain once, or while the current coordinator owns every mutation.
        """
        with self._tx() as connection:
            return [row[0] for row in connection.execute(
                "SELECT unit_id FROM units WHERE state='validated' ORDER BY rowid")]

    def _claimable_rows(self, connection, *, name=None, after_rowid=0, limit=64):
        columns = TABLES["units"][1]
        selected = ",".join("u." + column for column in columns)
        dependency_ready = """
            NOT EXISTS (
                SELECT 1
                FROM json_each(u.spec, '$.dependencies') AS dependency
                LEFT JOIN units AS prerequisite
                  ON prerequisite.unit_id = dependency.value
                WHERE prerequisite.unit_id IS NULL
                   OR prerequisite.state != 'validated'
            )
        """
        if name is None:
            raw_rows = connection.execute(
                f"SELECT u.rowid AS _rowid,{selected} FROM units AS u "
                f"WHERE u.state IN ('ready','blocked') AND u.rowid>? "
                f"AND {dependency_ready} ORDER BY u.rowid LIMIT ?",
                (after_rowid, limit),
            )
        else:
            raw_rows = connection.execute(
                f"SELECT u.rowid AS _rowid,{selected} FROM units AS u "
                f"WHERE u.unit_id=? AND u.state IN ('ready','blocked') "
                f"AND {dependency_ready} LIMIT 1",
                (name,),
            )
        rows = []
        for raw in raw_rows:
            row = {"_rowid": raw["_rowid"]}
            row.update({column: strict_json(raw[column]) if column in JSON_COLUMNS and raw[column] is not None
                        else raw[column] for column in columns})
            rows.append(row)
        return rows

    def ready_candidates(self, *, after_rowid=0, limit=64):
        if type(after_rowid) is not int or after_rowid < 0:
            raise WorkUnitError("CONTRACT_INVALID", "Invalid ready-row cursor")
        if type(limit) is not int or not 1 <= limit <= 512:
            raise WorkUnitError("CONTRACT_INVALID", "Ready-row limit must be bounded")
        with self._tx() as connection:
            return self._claimable_rows(
                connection, after_rowid=after_rowid, limit=limit)

    def scope_states(self, unit_ids):
        names = identifiers(unit_ids, "coordinator scope")
        with self._tx() as connection:
            states = {}
            for offset in range(0, len(names), 512):
                batch = names[offset:offset + 512]
                placeholders = ",".join("?" for _ in batch)
                states.update({row[0]: row[1] for row in connection.execute(
                    f"SELECT unit_id,state FROM units WHERE unit_id IN ({placeholders})",
                    batch,
                )})
        if set(states) != set(names):
            raise WorkUnitError("UNIT_MISSING", "Coordinator scope contains a missing unit")
        return states

    def claim(self, unit_id, owner, *, lease_seconds=60):
        identifier(unit_id, "unit")
        return self._claim(unit_id, owner, lease_seconds)

    def _claim(self, name, owner, lease_seconds):
        identifier(owner, "worker")
        seconds = timestamp_number(lease_seconds)
        if seconds == 0:
            raise WorkUnitError("CONTRACT_INVALID", "Lease duration must be positive")
        with self._tx(write=True) as connection:
            if self._meta(connection, "cancel"):
                return None
            self._admission(connection)
            rows = self._claimable_rows(connection, name=name, limit=1)
            if rows:
                row = rows[0]
                row.pop("_rowid")
                until = min(self._now() + seconds, instant(self.manifest["deadline_at"]))
                row.update(state="running", owner=owner, epoch=row["epoch"] + 1,
                           lease_until=until, error_code=None, retryable=0)
                self._change(connection, "claimed", {"units": [row]})
                return UnitLease(self.manifest["tenant_id"], self.manifest["run_id"],
                                 self.manifest_sha256, row["unit_id"], owner, row["epoch"], until)
        return None

    def heartbeat(self, lease, *, lease_seconds=60):
        seconds = timestamp_number(lease_seconds)
        if seconds == 0:
            raise WorkUnitError("CONTRACT_INVALID", "Lease duration must be positive")
        with self._tx(write=True) as connection:
            row = self._lease(connection, lease)
            self._admission(connection)
            until = min(self._now() + seconds, instant(self.manifest["deadline_at"]))
            row["lease_until"] = until
            self._change(connection, "heartbeat", {"units": [row]})
        return UnitLease(lease.tenant_id, lease.run_id, lease.manifest_sha256,
                         lease.unit_id, lease.owner, lease.epoch, until)

    def _unresolved(self, connection, *, except_id=None):
        columns = TABLES["attempts"][1]
        raw_rows = connection.execute(
            f"SELECT {','.join(columns)} FROM attempts "
            f"WHERE state IN ('dispatched','observed') ORDER BY unit_id,ordinal")
        unresolved = []
        for raw in raw_rows:
            row = {name: strict_json(raw[name]) if name in JSON_COLUMNS and raw[name] is not None
                   else raw[name] for name in columns}
            if row["attempt_id"] == except_id:
                continue
            unit = self._row(connection, "units", row["unit_id"])
            if (row["state"] == "observed" and row["usage"] is None
                    or unit["state"] != "running"
                    or unit["epoch"] != row["epoch"]
                    or unit["lease_until"] <= self._now()):
                unresolved.append(row)
        return unresolved

    def _attempts_for_unit_rows(self, connection, unit_id):
        columns = TABLES["attempts"][1]
        raw_rows = connection.execute(
            f"SELECT {','.join(columns)} FROM attempts WHERE unit_id=? ORDER BY ordinal",
            (unit_id,),
        )
        return [{name: strict_json(raw[name]) if name in JSON_COLUMNS and raw[name] is not None
                 else raw[name] for name in columns} for raw in raw_rows]

    def begin_attempt(self, lease, request, *, operation, ordinal=1, reservation=None):
        if not isinstance(lease, UnitLease):
            raise WorkUnitError("LEASE_INVALID", "Attempt requires a work-unit lease")
        operation = identifier(operation, "operation")
        if type(ordinal) is not int or ordinal not in {1, 2}:
            raise WorkUnitError("ATTEMPT_INVALID", "Only the first call and admitted format reanswer exist")
        request = detached(request)
        reservation = detached({} if reservation is None else reservation)
        if type(reservation) is not dict or any(type(value) is not int or value < 0 for value in reservation.values()):
            raise WorkUnitError("RESERVATION_INVALID", "Reservations require nonnegative integer amounts")
        envelope = {"schema": REQUEST_SCHEMA, "manifest_sha256": self.manifest_sha256,
                    "unit_id": lease.unit_id, "operation": operation, "ordinal": ordinal, "request": request}
        ref = self._blob(envelope, REQUEST_SCHEMA)
        first_request = None
        if ordinal == 2:
            with self._tx() as connection:
                first_before = next((row for row in self._attempts_for_unit_rows(
                    connection, lease.unit_id) if row["ordinal"] == 1), None)
            if first_before is not None:
                first_request = self._read_blob(first_before["request_ref"], REQUEST_SCHEMA)
        with self._tx(write=True) as connection:
            self._lease(connection, lease)
            self._admission(connection)
            existing = next((row for row in self._attempts_for_unit_rows(
                connection, lease.unit_id) if row["ordinal"] == ordinal), None)
            if existing is not None:
                if existing["request_ref"] != ref or existing["operation"] != operation or existing["reservation"] != reservation:
                    raise WorkUnitError("ATTEMPT_MISMATCH", "Attempt cannot replace its exact request")
                if existing["state"] == "settled":
                    return AttemptHandle(existing["attempt_id"], lease.unit_id, ordinal, value_hash(request), ref, True)
                if existing["state"] == "prepared" and (existing["epoch"], existing["owner"]) == (lease.epoch, lease.owner):
                    return AttemptHandle(existing["attempt_id"], lease.unit_id, ordinal, value_hash(request), ref)
                if existing["state"] != "not_sent":
                    raise WorkUnitError("ATTEMPT_UNKNOWN", "Attempt requires reconciliation before another send")
                existing.update(state="prepared", epoch=lease.epoch, owner=lease.owner)
                self._change(connection, "intent_resumed", {"attempts": [existing]})
                return AttemptHandle(existing["attempt_id"], lease.unit_id, ordinal, value_hash(request), ref)
            if self._unresolved(connection):
                raise WorkUnitError("ATTEMPT_UNKNOWN", "Unresolved attempt requires reconciliation before another send")
            if ordinal == 2:
                claim = self._row(connection, "retry_claims", 1)
                first = next((row for row in self._attempts_for_unit_rows(
                    connection, lease.unit_id) if row["ordinal"] == 1), None)
                if claim is None or first is None or claim["attempt_id"] != first["attempt_id"]:
                    raise WorkUnitError("RETRY_NOT_ADMITTED", "Format reanswer has not been admitted")
                if (first != first_before or first_request is None
                        or first_request["request"] != request or first["operation"] != operation):
                    raise WorkUnitError("RETRY_REQUEST_MISMATCH", "Format reanswer must preserve the exact request")
            attempt_id = "attempt-" + digest(json_bytes(envelope))
            row = {"attempt_id": attempt_id, "unit_id": lease.unit_id, "ordinal": ordinal,
                   "operation": operation, "request_ref": ref, "reservation": reservation,
                   "state": "prepared", "epoch": lease.epoch, "owner": lease.owner,
                   "observation_id": None, "usage": None}
            self._change(connection, "intent_prepared", {"attempts": [row]})
        return AttemptHandle(attempt_id, lease.unit_id, ordinal, value_hash(request), ref)

    def _attempt_lease(self, connection, lease, attempt_id):
        self._lease(connection, lease)
        row = self._row(connection, "attempts", identifier(attempt_id, "attempt"))
        if row is None or (row["unit_id"], row["epoch"], row["owner"]) != (lease.unit_id, lease.epoch, lease.owner):
            raise WorkUnitError("ATTEMPT_MISMATCH", "Attempt belongs to another lease")
        return row

    def mark_dispatch(self, lease, attempt_id):
        with self._tx(write=True) as connection:
            row = self._attempt_lease(connection, lease, attempt_id)
            self._admission(connection)
            if row["state"] != "prepared" or self._unresolved(connection, except_id=attempt_id):
                raise WorkUnitError("ATTEMPT_UNKNOWN", "Attempt requires reconciliation before dispatch")
            row["state"] = "dispatched"
            self._change(connection, "dispatch_started", {"attempts": [row]})

    def attempt(self, attempt_id):
        with self._tx() as connection:
            row = self._row(connection, "attempts", identifier(attempt_id, "attempt"))
        if row is None:
            raise WorkUnitError("ATTEMPT_MISSING", "Attempt does not exist")
        return row

    def attempts_for_unit(self, unit_id):
        self.unit(unit_id)
        with self._tx() as connection:
            columns = TABLES["attempts"][1]
            raw = connection.execute(f"SELECT {','.join(columns)} FROM attempts WHERE unit_id=? ORDER BY ordinal", (unit_id,))
            return [{name: strict_json(row[name]) if name in JSON_COLUMNS and row[name] is not None else row[name]
                     for name in columns} for row in raw]

    def attempts(self):
        with self._tx() as connection:
            return self._rows(connection, "attempts")

    def format_retry_claim(self):
        with self._tx() as connection:
            return self._row(connection, "retry_claims", 1)

    def record_response(self, lease, attempt_id, response, *, usage):
        if not isinstance(lease, UnitLease):
            raise WorkUnitError("LEASE_INVALID", "Response requires a work-unit lease identity")
        usage = known_usage(usage)
        row = self.attempt(attempt_id)
        if ((lease.tenant_id, lease.run_id, lease.manifest_sha256) !=
                (self.manifest["tenant_id"], self.manifest["run_id"], self.manifest_sha256)
                or row["unit_id"] != lease.unit_id or row["epoch"] != lease.epoch or row["owner"] != lease.owner
                or row["state"] not in {"dispatched", "observed", "settled"}):
            raise WorkUnitError("ATTEMPT_MISMATCH", "Response belongs to another dispatched lease")
        with self._tx() as connection:
            unit = self._row(connection, "units", lease.unit_id)
            late = unit["state"] != "running" or unit["epoch"] != lease.epoch or unit["owner"] != lease.owner or unit["lease_until"] <= self._now()
        envelope = {"schema": OBSERVATION_SCHEMA, "manifest_sha256": self.manifest_sha256,
                    "attempt_id": attempt_id, "request_sha256": row["request_ref"]["sha256"],
                    "response": detached(response), "usage": usage}
        ref = self._blob(envelope, OBSERVATION_SCHEMA)
        observation_id = "observation-" + ref["sha256"]
        with self._tx(write=True) as connection:
            existing = self._row(connection, "observations", observation_id)
            if existing is not None:
                return {**existing, "late": bool(existing["late"])}
            row = self._row(connection, "attempts", attempt_id)
            unit = self._row(connection, "units", lease.unit_id)
            current = unit["state"] == "running" and unit["epoch"] == lease.epoch and unit["owner"] == lease.owner and unit["lease_until"] > self._now()
            observation = {"observation_id": observation_id, "attempt_id": attempt_id,
                           "ref": ref, "usage": usage, "late": int(late or not current)}
            changes = {"observations": [observation]}
            if current and not late and row["state"] == "dispatched":
                row.update(state="observed", observation_id=observation_id, usage=usage)
                changes["attempts"] = [row]
            self._change(connection, "response_observed", changes)
        return {**observation, "late": bool(observation["late"])}

    def _observation(self, observation_id):
        with self._tx() as connection:
            row = self._row(connection, "observations", observation_id)
        if row is None:
            raise WorkUnitError("OBSERVATION_MISSING", "Native observation does not exist")
        envelope = self._read_blob(row["ref"], OBSERVATION_SCHEMA)
        if (envelope["manifest_sha256"] != self.manifest_sha256 or envelope["attempt_id"] != row["attempt_id"]
                or envelope["usage"] != row["usage"]):
            raise WorkUnitError("OBSERVATION_INVALID", "Native observation binding differs")
        return row, envelope

    def read_attempt_response(self, attempt_id):
        row = self.attempt(attempt_id)
        if row["observation_id"] is None:
            raise WorkUnitError("OBSERVATION_MISSING", "Attempt has no attached native observation")
        return self._observation(row["observation_id"])[1]["response"]

    def settle_attempt(self, attempt_id):
        before = self.attempt(attempt_id)
        if before["observation_id"] is None or before["usage"] is None:
            raise WorkUnitError("USAGE_UNKNOWN", "Unknown usage requires verified reconciliation")
        self._observation(before["observation_id"])
        with self._tx(write=True) as connection:
            row = self._row(connection, "attempts", attempt_id)
            if row["state"] == "settled":
                return row["usage"]
            if row["state"] != "observed" or row != before:
                raise WorkUnitError("ATTEMPT_MISMATCH", "Attempt settlement changed")
            row["state"] = "settled"
            self._change(connection, "usage_settled", {"attempts": [row]})
        return row["usage"]

    def reconcile_attempt(self, attempt_id, observation_id, *, verifier):
        if not callable(verifier):
            raise WorkUnitError("VERIFIER_REQUIRED", "Reconciliation requires a trusted verifier")
        attempt = self.attempt(attempt_id)
        observation, envelope = self._observation(observation_id)
        request = self._read_blob(attempt["request_ref"], REQUEST_SCHEMA)
        if (observation["attempt_id"] != attempt_id or envelope["request_sha256"] != attempt["request_ref"]["sha256"]
                or envelope["usage"] is None):
            raise WorkUnitError("USAGE_UNKNOWN", "Reconciliation requires bound native known usage")
        try:
            if verifier(detached(request["request"]), detached(envelope["response"]), detached(envelope["usage"])) is not None:
                raise ValueError
        except Exception:
            raise WorkUnitError("RECONCILIATION_REJECTED", "Native receipt did not pass reconciliation") from None
        with self._tx(write=True) as connection:
            row = self._row(connection, "attempts", attempt_id)
            if row["state"] == "settled":
                if row["observation_id"] != observation_id:
                    raise WorkUnitError("ATTEMPT_MISMATCH", "Settled receipt cannot be replaced")
                return row["usage"]
            if row != attempt or row["state"] not in {"dispatched", "observed"}:
                raise WorkUnitError("ATTEMPT_MISMATCH", "Attempt reconciliation changed")
            row.update(state="settled", observation_id=observation_id, usage=envelope["usage"])
            self._change(connection, "native_reconciled", {"attempts": [row]})
        return row["usage"]

    def claim_format_retry(self, lease, attempt_id, *, eligibility_verifier):
        if not callable(eligibility_verifier):
            raise WorkUnitError("VERIFIER_REQUIRED", "Retry eligibility requires native replay")
        row = self.attempt(attempt_id)
        if row["unit_id"] != lease.unit_id or row["ordinal"] != 1 or row["state"] != "settled" or row["usage"] is None:
            raise WorkUnitError("RETRY_NOT_ADMITTED", "Only a complete known first reply can be reanswered")
        request = self._read_blob(row["request_ref"], REQUEST_SCHEMA)["request"]
        response = self.read_attempt_response(attempt_id)
        try:
            eligible = eligibility_verifier(detached(request), detached(response))
            if type(eligible) is not bool:
                raise ValueError
        except Exception:
            raise WorkUnitError("RETRY_REPLAY_REJECTED", "Native reply eligibility replay failed") from None
        if not eligible:
            return False
        with self._tx(write=True) as connection:
            self._lease(connection, lease)
            self._admission(connection)
            if self._row(connection, "attempts", attempt_id) != row:
                raise WorkUnitError("ATTEMPT_MISMATCH", "Retry eligibility changed")
            if self._row(connection, "retry_claims", 1) is not None:
                return False
            if self._unresolved(connection):
                raise WorkUnitError("ATTEMPT_UNKNOWN", "Unresolved attempt requires reconciliation")
            self._change(connection, "format_reanswer_admitted", {"retry_claims": [
                {"slot": 1, "unit_id": lease.unit_id, "attempt_id": attempt_id}]})
        return True

    def publish_result(self, lease, value, *, validator, dispositions=None):
        if not callable(validator):
            raise WorkUnitError("VALIDATOR_REQUIRED", "Unit result requires its trusted validator")
        value = detached(value)
        try:
            if validator(detached(value)) is not None:
                raise ValueError
        except Exception:
            raise WorkUnitError("RESULT_REJECTED", "Work-unit validator rejected the result") from None
        snapshot = self._result_validation_snapshot(lease.unit_id)
        unit = snapshot["units"].get(lease.unit_id)
        if unit is None:
            raise WorkUnitError("UNIT_MISSING", "Work-unit does not exist")
        deps = {}
        for name in unit["spec"]["dependencies"]:
            self._read_result_from(name, snapshot)
            deps[name] = snapshot["units"][name]["result_ref"]["sha256"]
        dispositions = detached({} if dispositions is None else dispositions)
        if type(dispositions) is not dict or set(dispositions) != set(unit["spec"]["terminal_documents"]):
            raise WorkUnitError("DISPOSITION_INVALID", "Every terminal document requires a bound disposition")
        for row in dispositions.values():
            if type(row) is not dict or set(row) != {"state", "reason"} or row["state"] not in DISPOSITIONS:
                raise WorkUnitError("DISPOSITION_INVALID", "Invalid document disposition")
            plain_label(row["reason"], "disposition reason")
        original_hashes = {name: snapshot["originals"][name] for name in dispositions}
        envelope = {"schema": RESULT_SCHEMA, "tenant_id": lease.tenant_id, "run_id": lease.run_id,
                    "manifest_sha256": self.manifest_sha256, "unit_id": lease.unit_id,
                    "input_sha256": value_hash(unit["spec"]["inputs"]),
                    "contract_sha256": value_hash(unit["spec"]["contract"]), "epoch": lease.epoch,
                    "dependency_results": deps, "document_sha256": original_hashes,
                    "value": value, "dispositions": dispositions}
        ref = self._blob(envelope, RESULT_SCHEMA)
        self._commit_result(lease, ref, deps)
        return ref

    def _result_validation_snapshot(self, unit_id):
        """Load only one result's dependency closure and terminal originals."""
        with self._tx() as connection:
            target = self._row(connection, "units", unit_id)
            if target is None:
                raise WorkUnitError("UNIT_MISSING", "Work-unit does not exist")
            units = {unit_id: target}
            pending = list(target["spec"]["dependencies"])
            while pending:
                name = pending.pop()
                if name in units:
                    continue
                row = self._row(connection, "units", name)
                if row is None:
                    raise WorkUnitError("UNIT_MISSING", "Work-unit dependency does not exist")
                units[name] = row
                pending.extend(row["spec"]["dependencies"])
            documents = sorted({name for row in units.values()
                                for name in row["spec"]["terminal_documents"]})
            originals = {}
            if documents:
                for offset in range(0, len(documents), 512):
                    batch = documents[offset:offset + 512]
                    placeholders = ",".join("?" for _ in batch)
                    rows = connection.execute(
                        "SELECT json_extract(item.value,'$.id'),"
                        "json_extract(item.value,'$.sha256') "
                        "FROM meta,json_each(meta.value,'$.originals') AS item "
                        f"WHERE meta.key='plan' AND json_extract(item.value,'$.id') IN ({placeholders})",
                        batch,
                    ).fetchall()
                    originals.update({row[0]: row[1] for row in rows})
                if set(originals) != set(documents):
                    raise WorkUnitError("PLAN_INVALID", "Terminal originals are missing from the plan")
        return {"units": units, "originals": originals, "memo": {}, "visiting": set()}

    def _commit_result(self, lease, ref, dependency_results):
        with self._tx(write=True) as connection:
            row = self._lease(connection, lease)
            self._admission(connection)
            for name, expected in dependency_results.items():
                dependency = self._row(connection, "units", name)
                if dependency["state"] != "validated" or dependency["result_ref"]["sha256"] != expected:
                    raise WorkUnitError("DEPENDENCY_INVALID", "Unit dependency changed before publication")
            if any(attempt["state"] not in {"settled", "not_sent"}
                   for attempt in self._attempts_for_unit_rows(connection, lease.unit_id)):
                raise WorkUnitError("ATTEMPT_UNKNOWN", "Unsettled external attempt blocks result publication")
            row.update(state="validated", owner=None, lease_until=None, result_ref=ref, error_code=None, retryable=0)
            self._change(connection, "result_validated", {"units": [row]})

    def _result_snapshot(self, projections=None):
        """Read one coherent graph; memoization lives only in this operation.

        Each reused artifact is still read and rehashed. Shared dependencies
        and the complete plan are decoded once per recovery pass rather than
        once for every path through a DAG.
        """
        if projections is None:
            with self._tx() as connection:
                units = self._rows(connection, "units")
                plan = self._meta(connection, "plan")
        else:
            units = projections["units"]
            plan = next(row["value"] for row in projections["meta"] if row["key"] == "plan")
        return {"units": {row["unit_id"]: row for row in units}, "plan": plan,
                "originals": {} if plan is None else {row["id"]: row["sha256"] for row in plan["originals"]},
                "memo": {}, "visiting": set()}

    def _read_result_from(self, unit_id, snapshot):
        if unit_id in snapshot["memo"]:
            return snapshot["memo"][unit_id]
        unit = snapshot["units"].get(unit_id)
        if unit is None:
            raise WorkUnitError("UNIT_MISSING", "Work-unit does not exist")
        if unit["state"] != "validated" or unit["result_ref"] is None:
            raise WorkUnitError("RESULT_MISSING", "Unit has no validated result")
        if unit_id in snapshot["visiting"]:
            raise WorkUnitError("DEPENDENCY_INVALID", "Saved result dependency graph is cyclic")
        snapshot["visiting"].add(unit_id)
        value = self._read_blob(unit["result_ref"], RESULT_SCHEMA)
        deps = {}
        for name in unit["spec"]["dependencies"]:
            self._read_result_from(name, snapshot)
            deps[name] = snapshot["units"][name]["result_ref"]["sha256"]
        documents = {name: snapshot["originals"][name] for name in unit["spec"]["terminal_documents"]}
        expected = {"schema": RESULT_SCHEMA, "tenant_id": self.manifest["tenant_id"], "run_id": self.manifest["run_id"],
                    "manifest_sha256": self.manifest_sha256, "unit_id": unit_id,
                    "input_sha256": value_hash(unit["spec"]["inputs"]), "contract_sha256": value_hash(unit["spec"]["contract"]),
                    "epoch": unit["epoch"], "dependency_results": deps, "document_sha256": documents}
        if type(value) is not dict or set(value) != set(expected) | {"value", "dispositions"} or any(value[key] != item for key, item in expected.items()):
            raise WorkUnitError("RESULT_BINDING_INVALID", "Work-unit result binding differs")
        if type(value["dispositions"]) is not dict or set(value["dispositions"]) != set(documents):
            raise WorkUnitError("DISPOSITION_INVALID", "Saved dispositions differ from original scope")
        for disposition in value["dispositions"].values():
            if (type(disposition) is not dict or set(disposition) != {"state", "reason"}
                    or disposition["state"] not in DISPOSITIONS):
                raise WorkUnitError("DISPOSITION_INVALID", "Saved document disposition differs")
            plain_label(disposition["reason"], "disposition reason")
        snapshot["visiting"].remove(unit_id)
        snapshot["memo"][unit_id] = value
        return value

    def read_result(self, unit_id):
        identifier(unit_id, "unit")
        return self._read_result_from(unit_id, self._result_validation_snapshot(unit_id))

    def fail(self, lease, error_code, *, retryable=False):
        identifier(error_code, "error code")
        if type(retryable) is not bool:
            raise WorkUnitError("CONTRACT_INVALID", "Retryability must be explicit")
        with self._tx(write=True) as connection:
            row = self._lease(connection, lease)
            row.update(state="failed", error_code=error_code, retryable=int(retryable), owner=None, lease_until=None)
            not_sent = []
            for attempt in self._attempts_for_unit_rows(connection, lease.unit_id):
                if attempt["state"] == "prepared":
                    attempt["state"] = "not_sent"
                    not_sent.append(attempt)
            self._change(connection, "unit_failed", {"units": [row], "attempts": not_sent})

    def recover_expired(self):
        ready, unknown = [], []
        now = self._now()
        with self._tx() as connection:
            if connection.execute(
                    "SELECT 1 FROM units WHERE state='running' AND lease_until<=? LIMIT 1",
                    (now,)).fetchone() is None:
                return {"ready": ready, "unknown": unknown}
        with self._tx(write=True) as connection:
            changed_units, changed_attempts = [], []
            columns = TABLES["units"][1]
            raw_rows = connection.execute(
                f"SELECT {','.join(columns)} FROM units "
                "WHERE state='running' AND lease_until<=? ORDER BY rowid",
                (now,),
            )
            for raw in raw_rows:
                row = {name: strict_json(raw[name]) if name in JSON_COLUMNS and raw[name] is not None
                       else raw[name] for name in columns}
                owned = self._attempts_for_unit_rows(connection, row["unit_id"])
                for attempt in owned:
                    if attempt["state"] == "prepared":
                        attempt["state"] = "not_sent"
                        changed_attempts.append(attempt)
                unresolved = any(a["state"] in {"dispatched", "observed"} for a in owned)
                state = "unknown" if unresolved else "cancelled" if self._meta(connection, "cancel") else "ready"
                row.update(state=state, owner=None, lease_until=None)
                changed_units.append(row)
                (unknown if unresolved else ready).append(row["unit_id"])
            if changed_units or changed_attempts:
                self._change(connection, "leases_recovered", {"units": changed_units, "attempts": changed_attempts})
        return {"ready": ready, "unknown": unknown}

    def recover_stopped_workers(self, *, verifier):
        """Fence workers proven stopped by the owning parent process locks.

        This explicit host-only transition avoids waiting out a live-looking
        lease after its entire process group has stopped. It never clears a
        dispatched unknown, cancels an accepted task or revives a permanent
        semantic failure. The trusted verifier runs before the write lock.
        """
        if not callable(verifier):
            raise WorkUnitError("VERIFIER_REQUIRED", "Stopped worker recovery requires its owner verifier")
        try:
            if verifier() is not None:
                raise ValueError
        except Exception:
            raise WorkUnitError("RECOVERY_REJECTED", "Stopped worker ownership was not verified") from None
        with self._tx(write=True) as connection:
            self._admission(connection)
            attempts = self._rows(connection, "attempts")
            if any(row["state"] in {"dispatched", "observed"} for row in attempts):
                raise WorkUnitError("ATTEMPT_UNKNOWN", "Unresolved external attempt requires native reconciliation")
            units = self._rows(connection, "units")
            if any(row["state"] == "failed" and not row["retryable"] for row in units):
                raise WorkUnitError("RETRY_NOT_ADMITTED", "Permanent business failures cannot be revived")
            changed_units, changed_attempts = [], []
            for row in units:
                if row["state"] == "running" or row["state"] == "failed" and row["retryable"]:
                    row.update(state="ready", owner=None, lease_until=None, error_code=None, retryable=0)
                    changed_units.append(row)
            for row in attempts:
                if row["state"] == "prepared":
                    row["state"] = "not_sent"
                    changed_attempts.append(row)
            if changed_units or changed_attempts:
                self._change(connection, "stopped_workers_recovered", {"units": changed_units, "attempts": changed_attempts})
        return {"ready": [row["unit_id"] for row in changed_units]}

    def retry_units(self, unit_ids):
        if not isinstance(unit_ids, (list, tuple)) or not unit_ids or len(set(unit_ids)) != len(unit_ids):
            raise WorkUnitError("CONTRACT_INVALID", "Explicit distinct failed unit identities are required")
        for name in unit_ids:
            identifier(name, "unit")
        with self._tx(write=True) as connection:
            self._admission(connection)
            if self._unresolved(connection):
                raise WorkUnitError("ATTEMPT_UNKNOWN", "Retry requires native reconciliation first")
            rows = []
            for name in unit_ids:
                row = self._row(connection, "units", name)
                if row is None or row["state"] not in {"unknown", "failed"}:
                    raise WorkUnitError("RETRY_NOT_ADMITTED", "Only failed or reconciled unknown units can resume")
                if row["state"] == "failed" and not row["retryable"]:
                    raise WorkUnitError("RETRY_NOT_ADMITTED", "Failure is not retryable")
                row.update(state="ready", error_code=None, retryable=0)
                rows.append(row)
            self._change(connection, "units_retried", {"units": rows})

    def requeue_transient(self, unit_ids, *, allowed_codes, reason):
        """Reclassify only explicitly trusted transient failures.

        Used when an older checkpoint stored infrastructure contention as a
        permanent business failure. Unknown external attempts must already be
        reconciled; this method cannot revive dispatched/observed work.
        """
        if not isinstance(unit_ids, (list, tuple)) or not unit_ids or len(set(unit_ids)) != len(unit_ids):
            raise WorkUnitError("CONTRACT_INVALID", "Distinct transient unit identities are required")
        if not isinstance(allowed_codes, (list, tuple, set, frozenset)) or not allowed_codes:
            raise WorkUnitError("CONTRACT_INVALID", "Explicit transient error codes are required")
        plain_label(reason, "transient retry reason")
        allowed = {identifier(code, "transient error code") for code in allowed_codes}
        for name in unit_ids:
            identifier(name, "unit")
        with self._tx(write=True) as connection:
            self._admission(connection)
            if self._unresolved(connection):
                raise WorkUnitError("ATTEMPT_UNKNOWN", "Transient retry requires native reconciliation first")
            rows = []
            for name in unit_ids:
                row = self._row(connection, "units", name)
                unit_attempts = self._attempts_for_unit_rows(connection, name)
                # ATTEMPT_UNKNOWN/UNIT_HANDLER_FAILED with no dispatched work is
                # a scheduler-side failure, not a permanent semantic failure.
                no_unsettled = all(attempt["state"] in {"settled", "not_sent"} for attempt in unit_attempts)
                if (row is None or row["state"] != "failed" or row["error_code"] not in allowed
                        or not no_unsettled):
                    raise WorkUnitError("RETRY_NOT_ADMITTED", "Failure is outside the transient retry contract")
                row.update(state="ready", owner=None, lease_until=None, error_code=None, retryable=0)
                rows.append(row)
            self._change(connection, "transient_units_retried", {
                "meta": [{"key": "transient_retry_reason", "value": reason}],
                "units": rows,
            })
        return [row["unit_id"] for row in rows]

    def cancel(self, *, reason):
        plain_label(reason, "cancellation reason")
        with self._tx(write=True) as connection:
            if not self._meta(connection, "cancel"):
                units, attempts = [], []
                for row in self._rows(connection, "units"):
                    if row["state"] not in {"validated", "cancelled"}:
                        row.update(state="cancelled", owner=None, lease_until=None, epoch=row["epoch"] + 1)
                        units.append(row)
                for row in self._rows(connection, "attempts"):
                    if row["state"] == "prepared":
                        row["state"] = "not_sent"
                        attempts.append(row)
                self._change(connection, "cancel_requested", {"meta": [{"key": "cancel", "value": True}],
                                                               "units": units, "attempts": attempts})
        return self.status()

    def resume_plan(self):
        snapshot = self._result_snapshot()
        units = list(snapshot["units"].values())
        reusable = []
        for row in units:
            if row["state"] == "validated":
                self._read_result_from(row["unit_id"], snapshot)
                reusable.append(row["unit_id"])
        return {"reusable": reusable, "pending": [row["unit_id"] for row in units if row["state"] != "validated"]}

    def document_dispositions(self):
        return self._document_dispositions_from(self._result_snapshot())

    def _document_dispositions_from(self, snapshot):
        plan, units = snapshot["plan"], list(snapshot["units"].values())
        values = {}
        for row in units:
            if row["state"] == "validated":
                values.update(self._read_result_from(row["unit_id"], snapshot)["dispositions"])
        originals = [] if plan is None else [row["id"] for row in plan["originals"]]
        return {"admitted": len(originals), "terminal": len(values),
                **{state: sum(value["state"] == state for value in values.values()) for state in sorted(DISPOSITIONS)},
                "pending": sorted(set(originals) - set(values)), "documents": values}

    def compact_status(self, *, include_units=False):
        """Cheap projection for scheduling and default status reads."""
        if type(include_units) is not bool:
            raise WorkUnitError("CONTRACT_INVALID", "Compact status unit flag must be boolean")
        with self._tx() as connection:
            unit_counts = {state: count for state, count in connection.execute(
                "SELECT state,COUNT(*) FROM units GROUP BY state")}
            attempt_counts = {state: count for state, count in connection.execute(
                "SELECT state,COUNT(*) FROM attempts GROUP BY state")}
            usage = tuple(connection.execute(
                "SELECT COALESCE(SUM(json_extract(usage,'$.prompt_tokens')),0),"
                "COALESCE(SUM(json_extract(usage,'$.completion_tokens')),0),"
                "COALESCE(SUM(json_extract(usage,'$.total_tokens')),0) "
                "FROM attempts WHERE state='settled'").fetchone())
            cancelled = self._meta(connection, "cancel")
            retry_count = connection.execute("SELECT COUNT(*) FROM retry_claims").fetchone()[0]
            revision = connection.execute(
                "SELECT CASE WHEN value IS NULL OR value='null' THEN 0 "
                "ELSE json_extract(value,'$.revision') END "
                "FROM meta WHERE key='plan'").fetchone()[0]
            units = self._rows(connection, "units") if include_units else []
        total = sum(unit_counts.values())
        unknown = attempt_counts.get("dispatched", 0) + attempt_counts.get("observed", 0)
        held = sum(count for state, count in attempt_counts.items()
                   if state not in {"settled", "not_sent"})
        states = {state for state, count in unit_counts.items() if count}
        graph_state = ("cancelling" if unknown else "cancelled") if cancelled else (
            "completed" if total and states == {"validated"} else
            "failed" if states & {"failed", "unknown"} else
            "running" if "running" in states else "ready")
        observed_usage = {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                          "total_tokens": usage[2]}
        return {"schema": CONTRACT, "manifest_sha256": self.manifest_sha256,
                "plan_sha256": None, "graph_state": graph_state,
                "plan_revision": revision, "cancel_requested": cancelled,
                "units": units, "unit_count": total,
                "unit_counts": {state: unit_counts.get(state, 0) for state in
                                ("ready", "blocked", "running", "validated", "failed", "unknown", "cancelled")},
                "unknown_attempts": unknown,
                "settled_attempts": attempt_counts.get("settled", 0),
                "known_usage": None if unknown else observed_usage,
                "known_usage_observed": observed_usage,
                "format_reanswers": retry_count,
                "held_reservation_count": held, "held_reservations": []}

    def status(self):
        with self._tx() as connection:
            units = self._rows(connection, "units")
            attempts = self._rows(connection, "attempts")
            plan, cancelled = self._meta(connection, "plan"), self._meta(connection, "cancel")
            retry = self._rows(connection, "retry_claims")
        unknown = [row for row in attempts if row["state"] in {"dispatched", "observed"}]
        settled = [row for row in attempts if row["state"] == "settled"]
        usage = {key: sum(row["usage"][key] for row in settled) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        states = {row["state"] for row in units}
        graph_state = ("cancelling" if unknown else "cancelled") if cancelled else (
            "completed" if units and states == {"validated"} else "failed" if states & {"failed", "unknown"}
            else "running" if "running" in states else "ready")
        return {"schema": CONTRACT, "manifest_sha256": self.manifest_sha256,
                "plan_sha256": None if plan is None else value_hash(plan), "graph_state": graph_state,
                "plan_revision": 0 if plan is None else plan["revision"],
                "cancel_requested": cancelled, "units": units, "unknown_attempts": len(unknown),
                "settled_attempts": len(settled), "known_usage": None if unknown else usage,
                "known_usage_observed": usage, "format_reanswers": len(retry),
                "held_reservations": [{"attempt_id": row["attempt_id"], **row["reservation"]}
                                      for row in attempts if row["state"] not in {"settled", "not_sent"}]}

    def verify(self):
        with self._tx() as connection:
            projections = {table: self._rows(connection, table) for table in TABLES}
            events = list(connection.execute("SELECT sequence,payload,event_sha256 FROM events ORDER BY sequence"))
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise WorkUnitError("STORE_INVALID", "SQLite integrity check failed")
        replay = {table: {} for table in TABLES}
        previous = None
        previous_plan = None
        for expected_sequence, (sequence, raw, checksum) in enumerate(events, 1):
            payload = strict_json(raw)
            if (type(payload) is not dict or set(payload) != {"schema", "manifest_sha256", "sequence",
                    "previous_event_sha256", "kind", "at", "changes"}
                    or type(payload["changes"]) is not dict
                    or sequence != expected_sequence or payload.get("sequence") != sequence
                    or payload.get("schema") != EVENT_SCHEMA or payload.get("manifest_sha256") != self.manifest_sha256
                    or payload.get("previous_event_sha256") != previous or digest(json_bytes(payload)) != checksum):
                raise WorkUnitError("EVENT_CHAIN_INVALID", "Work-unit event chain differs")
            for table, rows in payload["changes"].items():
                if table not in TABLES:
                    raise WorkUnitError("EVENT_CHAIN_INVALID", "Unrecognized work-unit projection")
                key, columns = TABLES[table]
                for row in rows:
                    if set(row) != set(columns):
                        raise WorkUnitError("EVENT_CHAIN_INVALID", "Work-unit projection schema differs")
                    if table == "meta" and row["key"] == "plan" and row["value"] is not None:
                        new_plan = row["value"]
                        expected_revision = 1 if previous_plan is None else previous_plan["revision"] + 1
                        expected_previous = None if previous_plan is None else value_hash(previous_plan)
                        if (new_plan["revision"] != expected_revision
                                or new_plan["previous_plan_sha256"] != expected_previous):
                            raise WorkUnitError("PLAN_INVALID", "Work-unit plan revision chain differs")
                        if previous_plan is not None:
                            new_units = {spec["unit_id"]: spec for spec in new_plan["units"]}
                            new_originals = {item["id"]: item for item in new_plan["originals"]}
                            if (any(new_units.get(spec["unit_id"]) != spec for spec in previous_plan["units"])
                                    or any(new_originals.get(item["id"]) != item for item in previous_plan["originals"])):
                                raise WorkUnitError("PLAN_IMMUTABLE", "A work-unit plan revision replaced earlier scope")
                        self._plan_value([UnitSpec.from_dict(spec) for spec in new_plan["units"]],
                                         new_plan["originals"], revision=new_plan["revision"],
                                         previous_plan_sha256=new_plan["previous_plan_sha256"])
                        previous_plan = new_plan
                    replay[table][row[key]] = row
            previous = checksum
        if not events or any(sorted(replay[table].values(), key=lambda row: row[TABLES[table][0]]) != projections[table] for table in TABLES):
            raise WorkUnitError("PROJECTION_INVALID", "Work-unit projection differs from its event chain")
        if next(row["value"] for row in projections["meta"] if row["key"] == "manifest") != self.manifest:
            raise WorkUnitError("MANIFEST_MISMATCH", "Work-unit manifest differs")
        result_snapshot = self._result_snapshot(projections)
        for row in projections["units"]:
            if row["state"] == "validated":
                self._read_result_from(row["unit_id"], result_snapshot)
        plan = next(row["value"] for row in projections["meta"] if row["key"] == "plan")
        if plan is None:
            if projections["units"] or projections["attempts"] or projections["retry_claims"]:
                raise WorkUnitError("PLAN_INVALID", "Unplanned task has work projections")
        elif ([row["spec"] for row in projections["units"]] != plan["units"]
              or plan["schema"] != PLAN_SCHEMA or plan["manifest_sha256"] != self.manifest_sha256):
            raise WorkUnitError("PLAN_INVALID", "Saved task graph differs from its plan")
        attempts = {row["attempt_id"]: row for row in projections["attempts"]}
        for row in projections["attempts"]:
            envelope = self._read_blob(row["request_ref"], REQUEST_SCHEMA)
            if (envelope["manifest_sha256"] != self.manifest_sha256 or envelope["unit_id"] != row["unit_id"]
                    or envelope["operation"] != row["operation"] or envelope["ordinal"] != row["ordinal"]):
                raise WorkUnitError("ATTEMPT_MISMATCH", "Work-unit intent binding differs")
            if row["state"] == "settled":
                if row["observation_id"] is None or known_usage(row["usage"]) is None:
                    raise WorkUnitError("USAGE_UNKNOWN", "Settled task attempt lacks known native usage")
                observation, native = self._observation(row["observation_id"])
                if (observation["attempt_id"] != row["attempt_id"] or native["usage"] != row["usage"]
                        or native["request_sha256"] != row["request_ref"]["sha256"]):
                    raise WorkUnitError("ATTEMPT_MISMATCH", "Settled task receipt binding differs")
        for row in projections["observations"]:
            _, native = self._observation(row["observation_id"])
            if (row["attempt_id"] not in attempts
                    or native["request_sha256"] != attempts[row["attempt_id"]]["request_ref"]["sha256"]):
                raise WorkUnitError("ATTEMPT_MISMATCH", "Native task receipt belongs to another intent")
        documents = self._document_dispositions_from(result_snapshot)
        return {"schema": CONTRACT, "manifest_sha256": self.manifest_sha256, "event_count": len(events),
                "last_event_sha256": previous, "unit_count": len(projections["units"]),
                "document_count": documents["admitted"]}
