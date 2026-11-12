"""Lossless public-provider replay and eagerly bound parent I/O checkpoints.

Only explicit normalized numeric/container/date values are reconstructed. A
saved class name never imports or instantiates a Python class. Every restored
value must normalize to the exact original observation before it is reused.
"""
from __future__ import annotations

from datetime import date, datetime, time
import os
from pathlib import Path
import stat

from .agent_evidence import (EvidenceInvalid, EvidenceStore, checked_root, digest,
    fsync_directory, json_bytes, normalize, read_bytes, read_snapshot, safe_path, strict_json)
from .redline.execution import (ExecutionContext, EvidenceWriteError,
    StrictDataStale, StrictDataUnavailable, StrictObservationError)

IO_SCHEMA = "tiger.parent-provider-replay/v1"
IO_FIELDS = {"schema", "run_id", "parent_attempt", "sequence", "previous_sha256", "sha256",
    "kind", "stage", "stage_index", "call_index", "method", "input_ref", "output_ref", "error_ref", "reused_from"}


def _invalid(message):
    return EvidenceInvalid("RESULT_INVALID", message)


def _index(values, names):
    import pandas as pd
    if len(names) > 1:
        return pd.MultiIndex.from_tuples([tuple(value) for value in values], names=names)
    result = pd.Index([tuple(value) if isinstance(value, list) else value for value in values], tupleize_cols=False)
    result.names = names
    return result


def restore_provider_value(value):
    """Decode the closed provider subset and require an exact evidence roundtrip."""
    def restore(item, depth=0):
        if depth > 100:
            raise _invalid("Provider replay nesting is excessive")
        descend = lambda child: restore(child, depth + 1)
        if item is None or type(item) in {str, bool, int, float}:
            return item
        if type(item) is list:
            return [descend(child) for child in item]
        if type(item) is not dict or any(type(key) is not str for key in item):
            raise _invalid("Provider replay contains an unsupported value")
        kind = item.get("__type__")
        if kind is None:
            return {key: descend(child) for key, child in item.items()}
        if kind in {"date", "datetime", "time", "Timestamp"} and set(item) == {"__type__", "value"}:
            if kind == "Timestamp":
                import pandas as pd
                return pd.Timestamp(item["value"])
            return {"date": date, "datetime": datetime, "time": time}[kind].fromisoformat(item["value"])
        if kind == "missing" and set(item) == {"__type__", "reason"}:
            if item["reason"] == "pandas_na":
                import pandas as pd
                return pd.NA
            if item["reason"] in {"nan", "positive_infinity", "negative_infinity"}:
                return float({"nan": "nan", "positive_infinity": "inf", "negative_infinity": "-inf"}[item["reason"]])
        if kind == "numpy_datetime64" and set(item) == {"__type__", "dtype", "value"}:
            import numpy as np
            dtype = np.dtype(item["dtype"])
            if dtype.kind != "M":
                raise _invalid("Provider datetime dtype differs")
            return np.array("NaT" if item["value"] is None else item["value"], dtype=dtype)[()]
        if kind == "ndarray" and set(item) == {"__type__", "dtype", "shape", "values"}:
            import numpy as np
            dtype = np.dtype(item["dtype"])
            if dtype.fields is not None or dtype.kind not in "biufcMmOUSV":
                raise _invalid("Provider array dtype is unsupported")
            result = np.asarray(descend(item["values"]), dtype=dtype)
            if list(result.shape) != item["shape"]:
                raise _invalid("Provider array shape differs")
            return result
        if kind == "set" and set(item) == {"__type__", "values"}:
            return set(descend(item["values"]))
        if kind == "index" and set(item) == {"__type__", "names", "values"}:
            return _index(descend(item["values"]), descend(item["names"]))
        if kind == "series" and set(item) == {"__type__", "name", "index", "index_names", "dtype", "values"}:
            import pandas as pd
            return pd.Series(descend(item["values"]), index=_index(descend(item["index"]), descend(item["index_names"])),
                             name=descend(item["name"]), dtype=item["dtype"])
        fields = {"__type__", "index", "index_names", "columns", "column_names", "dtypes", "values"}
        if kind == "dataframe" and set(item) == fields:
            import pandas as pd
            index = _index(descend(item["index"]), descend(item["index_names"]))
            columns, values = descend(item["columns"]), descend(item["values"])
            if (len(item["dtypes"]) != len(columns) or len(values) != len(index)
                    or any(type(row) is not list or len(row) != len(columns) for row in values)):
                raise _invalid("Provider frame dimensions differ")
            parts = [pd.Series([row[number] for row in values], index=index, dtype=dtype)
                     for number, dtype in enumerate(item["dtypes"])]
            result = pd.concat(parts, axis=1) if parts else pd.DataFrame(index=index)
            result.columns = _index(columns, descend(item["column_names"]))
            return result
        raise _invalid("Provider replay type is outside its closed contract")
    try:
        result = restore(value)
        if normalize(result) != value:
            raise _invalid("Provider observation failed lossless replay")
        return result
    except EvidenceInvalid:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise _invalid("Provider observation could not be replayed losslessly") from None


def _append(root, relative, content):
    path = safe_path(root, relative, must_exist=False)
    checked_root(path.parent, create=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _invalid("Unsafe parent evidence stream")
        with os.fdopen(descriptor, "ab", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def read_io_journal(run, number, *, allow_partial=False):
    relative = f"parent-attempts/{number}/io.jsonl"
    path = safe_path(run, relative, must_exist=False)
    if not path.exists():
        return []
    raw = read_bytes(run, relative)
    if not raw.endswith(b"\n"):
        if not allow_partial:
            raise _invalid("Parent I/O journal is unfinished")
        raw = raw[:raw.rfind(b"\n") + 1]
    previous, rows, pending = None, [], {}
    try:
        for sequence, line in enumerate(raw.splitlines(), 1):
            row = strict_json(line)
            if (type(row) is not dict or set(row) != IO_FIELDS or row["schema"] != IO_SCHEMA
                    or row["run_id"] != run.name or type(row["parent_attempt"]) is not int
                    or row["parent_attempt"] != number or row["sequence"] != sequence
                    or row["previous_sha256"] != previous
                    or row["sha256"] != digest(json_bytes({key: value for key, value in row.items() if key != "sha256"}))
                    or row["kind"] not in {"begin", "end"}
                    or type(row["stage_index"]) is not int or row["stage_index"] < 1
                    or type(row["call_index"]) is not int or row["call_index"] < 1
                    or type(row["method"]) is not str or not row["method"]):
                raise _invalid("Parent I/O journal identity differs")
            key = (row["stage_index"], row["call_index"])
            read_snapshot(run, row["input_ref"], run_id=run.name, stage=row["stage"], role="input")
            if row["kind"] == "begin":
                if key in pending or row["output_ref"] is not None or row["error_ref"] is not None or row["reused_from"] is not None:
                    raise _invalid("Parent I/O begin state differs")
                pending[key] = row
            else:
                begin = pending.pop(key, None)
                if (begin is None or any(row[name] != begin[name] for name in ("stage", "method", "input_ref"))
                        or (row["output_ref"] is None) == (row["error_ref"] is None)):
                    raise _invalid("Parent I/O end state differs")
                read_snapshot(run, row["output_ref"] or row["error_ref"], run_id=run.name, stage=row["stage"], role="output")
            previous = row["sha256"]
            rows.append(row)
        if pending and not allow_partial:
            raise _invalid("Parent I/O journal has unfinished calls")
        return rows
    except (KeyError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        if isinstance(error, EvidenceInvalid):
            raise
        raise _invalid("Parent I/O journal is invalid") from None


class ParentEvidenceStore(EvidenceStore):
    def __init__(self, root, run_id, attempt):
        self.root, self.run_id = checked_root(root), run_id
        self.attempt = attempt
        self.event_relative = attempt["events_path"]
        self.sequence, self.previous_hash = 0, None
        if safe_path(self.root, self.event_relative, must_exist=False).exists():
            raise EvidenceWriteError("A parent attempt stream cannot be resumed")

    def append_event(self, event):
        from .research_resume import assert_attempt_owner
        try:
            assert_attempt_owner(self.root, self.attempt)
            row = dict(event)
            sequence = self.sequence + 1
            if row.get("sequence", sequence) != sequence or row.get("run_id", self.run_id) != self.run_id:
                raise _invalid("Parent stage event identity differs")
            row.update(schema="tiger.execution-event.v1", run_id=self.run_id, sequence=sequence,
                       previous_event_sha256=self.previous_hash)
            row.pop("event_sha256", None)
            row["event_sha256"] = digest(json_bytes(row))
            _append(self.root, self.event_relative, json_bytes(row))
            self.sequence, self.previous_hash = sequence, row["event_sha256"]
            return row
        except Exception as error:
            raise EvidenceWriteError("Unable to persist parent attempt event") from error


class ParentExecutionContext(ExecutionContext):
    def __init__(self, *, parent_attempt, provider_cache, **kwargs):
        super().__init__(**kwargs)
        self.parent_attempt, self._provider_cache = parent_attempt, provider_cache
        self._io_sequence, self._io_previous = 0, None
        self._reuse_sources = {}

    def _check_thread(self):
        super()._check_thread()
        from .research_resume import assert_attempt_owner
        assert_attempt_owner(self.store.root, self.parent_attempt)

    def _io_event(self, handle, kind, *, call=None, reused_from=None):
        self._check_thread()
        scope = handle.scope
        row = {"schema": IO_SCHEMA, "run_id": self.run_id, "parent_attempt": self.parent_attempt["number"],
            "sequence": self._io_sequence + 1, "previous_sha256": self._io_previous,
            "kind": kind, "stage": scope.spec.name, "stage_index": scope.index,
            "call_index": handle.index, "method": handle.method, "input_ref": handle.input_ref,
            "output_ref": None if call is None else call.get("output_ref"),
            "error_ref": None if call is None else call.get("error_ref"), "reused_from": reused_from}
        row["sha256"] = digest(json_bytes(row))
        _append(self.store.root, f"parent-attempts/{self.parent_attempt['number']}/io.jsonl", json_bytes(row))
        self._io_sequence, self._io_previous = row["sequence"], row["sha256"]

    def begin_io(self, method, inputs):
        handle = super().begin_io(method, inputs)
        self._io_event(handle, "begin")
        return handle

    def finish_io(self, handle, **kwargs):
        super().finish_io(handle, **kwargs)
        call = next(row for row in handle.scope.io_calls if row["index"] == handle.index)
        self._io_event(handle, "end", call=call, reused_from=self._reuse_sources.pop(handle.index, None))

    def call_io(self, method, inputs, fn):
        if not method.startswith("provider."):
            return super().call_io(method, inputs, fn)
        handle = self.begin_io(method, inputs)
        key = (handle.scope.index, handle.index, method, handle.input_ref["sha256"])
        cached = self._provider_cache.get(key)
        if cached is None:
            try:
                result = fn()
            except BaseException as original:
                self.finish_io(handle, error=original)
                raise
            self.finish_io(handle, output=result)
            return result
        self._reuse_sources[handle.index] = {"parent_attempt": cached["parent_attempt"], "sha256": cached["sha256"]}
        if cached["error_ref"] is not None:
            saved = read_snapshot(self.store.root, cached["error_ref"], run_id=self.run_id,
                                  stage=handle.scope.spec.name, role="output")
            ref = self._snapshot(handle.scope.spec.name, "output", saved)
            call = {"index": handle.index, "method": method, "input_ref": handle.input_ref, "error_ref": ref}
            handle.scope.io_calls.append(call)
            handle.completed = True
            self._io_event(handle, "end", call=call, reused_from=self._reuse_sources.pop(handle.index))
            error_type = {"StrictDataUnavailable": StrictDataUnavailable,
                          "StrictObservationError": StrictObservationError,
                          "StrictDataStale": StrictDataStale}.get(saved["error_type"], RuntimeError)
            raise error_type("Previously observed public provider failure")
        value = read_snapshot(self.store.root, cached["output_ref"], run_id=self.run_id,
                              stage=handle.scope.spec.name, role="output")
        result = restore_provider_value(value)
        self.finish_io(handle, output=result)
        return result


def provider_cache(run, attempts):
    cache = {}
    for attempt in attempts:
        for row in read_io_journal(run, attempt["number"], allow_partial=True):
            if row["kind"] != "end" or not row["method"].startswith("provider."):
                continue
            key = (row["stage_index"], row["call_index"], row["method"], row["input_ref"]["sha256"])
            previous = cache.get(key)
            if previous is not None and (previous["output_ref"], previous["error_ref"]) != (row["output_ref"], row["error_ref"]):
                raise _invalid("Public provider observations conflict across parent attempts")
            cache[key] = row
    return cache
