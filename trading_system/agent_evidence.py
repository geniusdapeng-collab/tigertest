"""Typed, content-addressed evidence for the fixed public paper workflow.

This is a local cooperative-process integrity boundary, not a signature service.
The reader never trusts a stored ``verified`` flag, follows symbolic links, or
interprets strings in downloaded documents as executable instructions.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import Any, Callable

from .redline.execution import EvidenceWriteError
from .redline.model_diagnostics import MODEL_IO_METHODS, validate_model_diagnostic

MAX_FILE_BYTES = 256 * 1024 * 1024
SNAPSHOT_SCHEMA = "tiger.snapshot.v1"
EVENT_SCHEMA = "tiger.execution-event.v1"


class EvidenceInvalid(ValueError):
    def __init__(self, code: str, message: str, *, stage: str | None = None):
        super().__init__(message)
        self.code = code
        self.stage = stage


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def strict_json(data: str | bytes) -> Any:
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    def finite_float(text: str) -> float:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return value

    def invalid_constant(_text: str):
        raise ValueError("Non-finite JSON constant")

    return json.loads(data, object_pairs_hook=_unique_object,
                      parse_constant=invalid_constant, parse_float=finite_float)


def normalize(value: object, _active: set[int] | None = None,
              _depth: int = 0) -> Any:
    """Snapshot explicit supported data, retaining complete table values.

    Non-finite/missing values have an explicit type and reason. Arbitrary
    objects, repr fallbacks, non-string mapping keys and cyclic containers fail.
    """
    if _depth > 100:
        raise TypeError("Evidence nesting exceeds 100")
    active = set() if _active is None else _active
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"__type__": "missing", "reason":
                "nan" if math.isnan(value) else "positive_infinity" if value > 0
                else "negative_infinity"}
    if type(value).__module__.startswith("pandas.") and type(value).__name__ in {
            "NaTType", "NAType"}:
        return {"__type__": "missing", "reason": "pandas_na"}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return {"__type__": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, Path):
        # Paths are explicit trusted runtime metadata, never a serialization
        # escape hatch for arbitrary runtime objects.
        return {"__type__": "path", "value": str(value)}
    identity = id(value)
    if identity in active:
        raise TypeError("Cyclic evidence value")
    active.add(identity)
    try:
        def descend(item):
            return normalize(item, active, _depth + 1)
        if isinstance(value, dict):
            if not all(isinstance(key, str) for key in value):
                raise TypeError("Evidence mapping keys must be strings")
            return {key: descend(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [descend(item) for item in value]
        if isinstance(value, (set, frozenset)):
            items = [descend(item) for item in value]
            return {"__type__": "set", "values": sorted(items, key=json_bytes)}
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"__type__": "dataclass",
                    "class": f"{type(value).__module__}.{type(value).__qualname__}",
                    "fields": {field.name: descend(getattr(value, field.name))
                               for field in dataclasses.fields(value)}}
        # Lazy imports keep doctor/describe/readonly startup free of market
        # client imports and avoid initializing any provider cache.
        import numpy as np
        import pandas as pd
        if value is pd.NA or value is pd.NaT:
            return {"__type__": "missing", "reason": "pandas_na"}
        if isinstance(value, np.generic):
            if isinstance(value, np.datetime64):
                return {"__type__": "numpy_datetime64", "dtype": str(value.dtype),
                        "value": None if np.isnat(value) else str(value)}
            return descend(value.item())
        if isinstance(value, np.ndarray):
            return {"__type__": "ndarray", "dtype": str(value.dtype),
                    "shape": list(value.shape), "values": descend(value.tolist())}
        if isinstance(value, pd.DataFrame):
            return {"__type__": "dataframe", "index": descend(value.index.tolist()),
                    "index_names": descend(list(value.index.names)),
                    "columns": descend(value.columns.tolist()),
                    "column_names": descend(list(value.columns.names)),
                    "dtypes": [str(dtype) for dtype in value.dtypes],
                    "values": descend(value.to_numpy(dtype=object).tolist())}
        if isinstance(value, pd.Series):
            return {"__type__": "series", "name": descend(value.name),
                    "index": descend(value.index.tolist()),
                    "index_names": descend(list(value.index.names)),
                    "dtype": str(value.dtype), "values": descend(value.tolist())}
        if isinstance(value, pd.Index):
            return {"__type__": "index", "names": descend(list(value.names)),
                    "values": descend(value.tolist())}
        raise TypeError(f"Unsupported evidence type: {type(value).__name__}")
    finally:
        active.remove(identity)


def checked_root(root: Path, *, create: bool = False) -> Path:
    root = Path(root)
    if not root.is_absolute():
        raise EvidenceInvalid("INVALID_REQUEST", "State root must be absolute")
    # Check lexical ancestors before resolve: resolve alone would hide symlinks.
    chain = list(reversed(root.parents)) + [root]
    for part in chain:
        if part.exists() or part.is_symlink():
            mode = part.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise EvidenceInvalid("RESULT_INVALID", "Unsafe state directory")
        elif create:
            part.mkdir(mode=0o700, exist_ok=True)
            if not stat.S_ISDIR(part.lstat().st_mode):
                raise EvidenceInvalid("RESULT_INVALID", "Unsafe newly created state directory")
        else:
            break
    return root


def safe_path(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise EvidenceInvalid("RESULT_INVALID", "Invalid evidence path")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or any(p in ("", ".", "..") for p in relative.split("/")):
        raise EvidenceInvalid("RESULT_INVALID", "Evidence path escapes run")
    checked_root(root)
    target = root.joinpath(*posix.parts)
    for part in list(reversed(target.parents)):
        if part == root or root in part.parents:
            if part.exists() or part.is_symlink():
                if not stat.S_ISDIR(part.lstat().st_mode):
                    raise EvidenceInvalid("RESULT_INVALID", "Unsafe evidence parent")
            elif must_exist:
                raise EvidenceInvalid("EVIDENCE_MISSING", "Evidence directory missing")
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise EvidenceInvalid("RESULT_INVALID", "Evidence must be an unlinked regular file")
    elif must_exist:
        raise EvidenceInvalid("EVIDENCE_MISSING", "Evidence file missing")
    return target


def read_bytes(root: Path, relative: str, *, limit: int = MAX_FILE_BYTES) -> bytes:
    path = safe_path(root, relative)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise EvidenceInvalid("RESULT_INVALID", "Invalid evidence size or file type")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise EvidenceInvalid("RESULT_INVALID", "Evidence exceeds size limit")
        return data
    finally:
        os.close(fd)


def read_json(root: Path, relative: str) -> Any:
    try:
        return strict_json(read_bytes(root, relative))
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, EvidenceInvalid):
            raise
        raise EvidenceInvalid("RESULT_INVALID", "Invalid evidence JSON") from exc


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_bytes(root: Path, relative: str, data: bytes, *,
                 exclusive: bool = False, before_publish: Callable[[], None] | None = None) -> Path:
    """Durable local replacement; exclusive files are immutable after creation."""
    target = safe_path(root, relative, must_exist=False)
    checked_root(target.parent, create=True)
    target = safe_path(root, relative, must_exist=False)
    if exclusive and target.exists():
        raise FileExistsError("Immutable evidence already exists")
    temp_path = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".write-", dir=target.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if before_publish is not None:
            before_publish()
        if exclusive:
            # link() is an atomic no-overwrite publication. Remove the temporary
            # name before readers see the final record through another pointer.
            os.link(temp_path, target, follow_symlinks=False)
            temp_path.unlink()
            temp_path = None
        else:
            os.replace(temp_path, target)
            temp_path = None
        fsync_directory(target.parent)
        return target
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def atomic_json(root: Path, relative: str, value: object, *,
                exclusive: bool = False, before_publish: Callable[[], None] | None = None) -> Path:
    return atomic_bytes(root, relative, json_bytes(value), exclusive=exclusive,
                        before_publish=before_publish)


def file_ref(root: Path, relative: str, schema: str) -> dict:
    content = read_bytes(root, relative)
    return {"path": relative, "sha256": digest(content), "bytes": len(content),
            "schema": schema}


def verify_ref(root: Path, ref: object, *, schema: str | None = None) -> bytes:
    if (not isinstance(ref, dict) or set(ref) != {"path", "sha256", "bytes", "schema"}
            or not isinstance(ref["bytes"], int) or isinstance(ref["bytes"], bool)
            or ref["bytes"] < 0 or not isinstance(ref["sha256"], str)
            or len(ref["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in ref["sha256"])
            or not isinstance(ref["schema"], str) or not ref["schema"]
            or (schema is not None and ref["schema"] != schema)):
        raise EvidenceInvalid("RESULT_INVALID", "Malformed evidence reference")
    content = read_bytes(root, ref["path"])
    if len(content) != ref["bytes"] or digest(content) != ref["sha256"]:
        raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Evidence content hash mismatch")
    return content


class EvidenceStore:
    def __init__(self, root: Path, run_id: str):
        self.root = checked_root(root)
        self.run_id = run_id
        self.sequence = 0
        self.previous_hash = None
        if (root / "events.jsonl").exists():
            raise EvidenceWriteError("An evidence stream cannot be resumed")

    def snapshot(self, *, stage: str, role: str, value: object) -> dict:
        try:
            if role not in {"input", "output"} or not isinstance(stage, str) or not stage:
                raise ValueError("Invalid snapshot identity")
            payload = {"schema": SNAPSHOT_SCHEMA, "run_id": self.run_id,
                       "stage": stage, "role": role, "value": normalize(value)}
            content = json_bytes(payload)
            relative = f"evidence/{digest(content)}.json"
            target = safe_path(self.root, relative, must_exist=False)
            if target.exists():
                if read_bytes(self.root, relative) != content:
                    raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Blob collision")
            else:
                atomic_bytes(self.root, relative, content, exclusive=True)
            return {"path": relative, "sha256": digest(content),
                    "bytes": len(content), "schema": SNAPSHOT_SCHEMA}
        except Exception as exc:
            raise EvidenceWriteError("Unable to persist stage snapshot") from exc

    def append_event(self, event: dict) -> dict:
        try:
            row = dict(event)
            sequence = self.sequence + 1
            if "sequence" in row and row["sequence"] != sequence:
                raise ValueError("Unexpected event sequence")
            if row.get("run_id", self.run_id) != self.run_id:
                raise ValueError("Cross-run event")
            row.update(schema=EVENT_SCHEMA, run_id=self.run_id, sequence=sequence,
                       previous_event_sha256=self.previous_hash)
            row.pop("event_sha256", None)
            row["event_sha256"] = digest(json_bytes(row))
            content = json_bytes(row)
            path = safe_path(self.root, "events.jsonl", must_exist=False)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Unsafe event stream")
                with os.fdopen(fd, "ab", closefd=False) as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                os.close(fd)
            fsync_directory(self.root)
            self.sequence = sequence
            self.previous_hash = row["event_sha256"]
            return row
        except Exception as exc:
            raise EvidenceWriteError("Unable to append execution event") from exc


def read_snapshot(root: Path, ref: dict, *, run_id: str, stage: str,
                  role: str) -> Any:
    content = verify_ref(root, ref, schema=SNAPSHOT_SCHEMA)
    try:
        blob = strict_json(content)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Invalid snapshot JSON", stage=stage) from exc
    if (not isinstance(blob, dict)
            or set(blob) != {"schema", "run_id", "stage", "role", "value"}
            or blob["schema"] != SNAPSHOT_SCHEMA or blob["run_id"] != run_id
            or blob["stage"] != stage or blob["role"] != role):
        raise EvidenceInvalid("RESULT_INVALID", "Snapshot identity mismatch", stage=stage)
    return blob["value"]


def verify_events(root: Path, run_id: str, specs: list[dict], *, event_relative: str = "events.jsonl") -> dict:
    """Recompute the ordered execution stream and all declared child I/O refs."""
    raw = read_bytes(root, event_relative, limit=16 * 1024 * 1024)
    if not raw.endswith(b"\n"):
        raise EvidenceInvalid("RUN_INCOMPLETE", "Unfinished execution stream")
    try:
        events = [strict_json(line) for line in raw.splitlines()]
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Invalid event JSON") from exc
    if len(events) != 2 * len(specs):
        raise EvidenceInvalid("RUN_INCOMPLETE", "Required stage event count differs")
    previous = None
    previous_output = None
    refs: dict[str, dict] = {}
    degraded = []
    for index, spec in enumerate(specs):
        begin, end = events[index * 2:index * 2 + 2]
        stage = spec["name"]
        for offset, row in enumerate((begin, end)):
            fields = {"schema", "run_id", "sequence", "stage_index", "stage",
                      "kind", "status", "input_ref", "output_ref",
                      "previous_stage_ref", "reason", "error_type",
                      "previous_event_sha256", "event_sha256"}
            if not isinstance(row, dict) or set(row) != fields:
                raise EvidenceInvalid("RESULT_INVALID", "Invalid event record", stage=stage)
            unsigned = {key: value for key, value in row.items() if key != "event_sha256"}
            actual_hash = digest(json_bytes(unsigned))
            if (row.get("schema") != EVENT_SCHEMA or row.get("run_id") != run_id
                    or row.get("sequence") != 2 * index + offset + 1
                    or row.get("previous_event_sha256") != previous
                    or row.get("event_sha256") != actual_hash):
                raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Event chain differs", stage=stage)
            if (row.get("stage") != stage or row.get("stage_index") != index + 1
                    or not isinstance(row.get("stage_index"), int)
                    or isinstance(row.get("stage_index"), bool)
                    or not isinstance(row.get("sequence"), int)
                    or isinstance(row.get("sequence"), bool)
                    or row.get("kind") != ("begin" if offset == 0 else "end")):
                raise EvidenceInvalid("STEP_ORDER_VIOLATION", "Stage order differs", stage=stage)
            if row["error_type"] is not None:
                raise EvidenceInvalid("STEP_FAILED", "Stage contains a failure", stage=stage)
            if offset == 0 and (row["status"] != "running"
                                or row["output_ref"] is not None or row["reason"] is not None):
                raise EvidenceInvalid("RESULT_INVALID", "Invalid stage begin state", stage=stage)
            if row.get("previous_stage_ref") != previous_output:
                raise EvidenceInvalid("STEP_ORDER_VIOLATION", "Stage predecessor differs", stage=stage)
            previous = actual_hash
        if begin.get("input_ref") != end.get("input_ref"):
            raise EvidenceInvalid("RESULT_INVALID", "Stage input binding differs", stage=stage)
        read_snapshot(root, begin["input_ref"], run_id=run_id, stage=stage, role="input")
        refs[begin["input_ref"]["path"]] = begin["input_ref"]
        state = end.get("status")
        if state == "passthrough":
            if (spec["driver"] != "llm" or spec.get("allow_passthrough") is not True
                    or not isinstance(end.get("reason"), str) or not end["reason"].strip()):
                raise EvidenceInvalid("STEP_FAILED", "Illegal stage passthrough", stage=stage)
            degraded.append({"stage": stage, "reason": end["reason"]})
        elif state != "completed":
            raise EvidenceInvalid("STEP_FAILED", "Stage did not complete", stage=stage)
        elif end["reason"] is not None:
            raise EvidenceInvalid("RESULT_INVALID", "Completed stage has a passthrough reason", stage=stage)
        output = read_snapshot(root, end.get("output_ref"), run_id=run_id,
                               stage=stage, role="output")
        refs[end["output_ref"]["path"]] = end["output_ref"]
        if (not isinstance(output, dict) or output.get("schema") != "tiger.stage-output.v1"
                or set(output) != {"schema", "value", "io_calls"}
                or not isinstance(output["io_calls"], list)):
            raise EvidenceInvalid("RESULT_INVALID", "Stage output schema differs", stage=stage)
        for number, call in enumerate(output["io_calls"], 1):
            if (not isinstance(call, dict) or call.get("index") != number
                    or not isinstance(call.get("index"), int)
                    or isinstance(call.get("index"), bool)
                    or not isinstance(call.get("method"), str) or not call["method"]
                    or any(c in call["method"] for c in "\r\n")
                    or "input_ref" not in call
                    or ("output_ref" in call) == ("error_ref" in call)
                    or set(call) != {"index", "method", "input_ref",
                                     "output_ref" if "output_ref" in call else "error_ref"}):
                raise EvidenceInvalid("RESULT_INVALID", "I/O evidence schema differs", stage=stage)
            for name in ("input_ref", "output_ref", "error_ref"):
                if name in call:
                    value = read_snapshot(root, call[name], run_id=run_id, stage=stage,
                                          role="input" if name == "input_ref" else "output")
                    if name == "error_ref":
                        base_fields = {"error_type", "message", "outcome_observed"}
                        if (not isinstance(value, dict)
                                or set(value) not in (base_fields, base_fields | {"diagnostic"})
                                or not isinstance(value["error_type"], str) or not value["error_type"]
                                or value["message"] != "External call failed"
                                or not isinstance(value["outcome_observed"], bool)):
                            raise EvidenceInvalid("RESULT_INVALID", "I/O error payload differs",
                                                  stage=stage)
                        if "diagnostic" in value:
                            try:
                                if (call["method"] not in MODEL_IO_METHODS
                                        or value["error_type"] != "RequiredModelUnavailable"):
                                    raise ValueError("Untrusted model failure metadata")
                                validate_model_diagnostic(value["diagnostic"])
                            except ValueError as exc:
                                raise EvidenceInvalid("RESULT_INVALID", "I/O error diagnostic differs",
                                                      stage=stage) from exc
                    refs[call[name]["path"]] = call[name]
        previous_output = end["output_ref"]
    return {"event_count": len(events), "stage_count": len(specs),
            "last_event_sha256": previous, "degraded": degraded,
            "evidence_refs": list(refs.values())}
