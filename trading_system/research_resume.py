"""Same-run parent attempts under the original admission and account locks.

The original request, source, policy, account before-state and deadline never
change. Failed attempts retain their bytes. Only a complete, cleanly exited
attempt supplies the canonical event stream used by ordinary receipt proof.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re

from .agent_evidence import (EvidenceInvalid, atomic_bytes, atomic_json, checked_root,
    digest, file_ref, fsync_directory, json_bytes, read_bytes, read_json, read_snapshot,
    safe_path, strict_json, verify_events, verify_ref)

ATTEMPT = "tiger.parent-attempt/v1"
POINTER = "tiger.parent-attempt-current/v1"
ARCHIVE_INTENT = "tiger.parent-attempt-archive-intent/v1"
ARCHIVE = "tiger.parent-attempt-archive/v1"
HISTORY = "tiger.parent-history/v1"
PROVIDER_REPLAY = "tiger.parent-provider-replay/v1"
_ROTATED_FILES = ("accepted.json", "engine-result.json", "processes.json", "supervisor-process.json",
    "process-exit.json", "engine.log", "log-summary.json", "model-failure-accounting.json")
_START_FIELDS = {"schema", "run_id", "number", "admission_sha256", "request_sha256", "source_sha256",
    "policy_sha256", "account_parent", "as_of", "deadline_at", "started_at", "previous_attempt_ref", "events_path", "io_path"}


def supported(policy):
    value = policy.get("research_work") if type(policy) is dict else None
    return (policy.get("profile") in {
                "us-public-intelligence-paper-v2",
                "us-tech-intelligence-paper-v3",
            } and type(value) is dict
            and value.get("parent_attempt_contract") == ATTEMPT
            and value.get("provider_replay_contract") == PROVIDER_REPLAY)


def _require(policy):
    if not supported(policy):
        raise EvidenceInvalid("RESUME_UNSUPPORTED", "This frozen workflow does not admit same-run research recovery")


def _now():
    return datetime.now(timezone.utc)


def _binding(run, admission):
    return {"run_id": run.name, "admission_sha256": digest(read_bytes(run, "admission.json")),
        **{name: admission[name] for name in ("request_sha256", "source_sha256", "policy_sha256")},
        "account_parent": admission["parent"], "as_of": admission["clock"]["admitted_at_utc"],
        "deadline_at": admission["clock"]["deadline_at_utc"]}


def _write_equal(run, relative, content):
    try:
        atomic_bytes(run, relative, content, exclusive=True)
    except FileExistsError:
        if read_bytes(run, relative) != content:
            raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Immutable recovery artifact differs") from None


def _deadline(admission):
    if _now() >= datetime.fromisoformat(admission["clock"]["deadline_at_utc"]):
        raise EvidenceInvalid("TIMEOUT", "The original research admission deadline has elapsed", stage="resume.prepare")


def _read_start(run, number, admission):
    if type(number) is not int or not 1 <= number <= 999999999999:
        raise EvidenceInvalid("RESULT_INVALID", "Invalid parent attempt identity")
    relative = f"parent-attempts/{number}/start.json"
    value = read_json(run, relative)
    if (type(value) is not dict or set(value) != _START_FIELDS or value["schema"] != ATTEMPT
            or value["number"] != number or any(value[name] != item for name, item in _binding(run, admission).items())
            or value["events_path"] != f"parent-attempts/{number}/events.jsonl"
            or value["io_path"] != f"parent-attempts/{number}/io.jsonl"):
        raise EvidenceInvalid("RESULT_INVALID", "Parent attempt differs from its frozen admission")
    try:
        started = datetime.fromisoformat(value["started_at"])
        if (started.utcoffset() is None or started < datetime.fromisoformat(value["as_of"])
                or started >= datetime.fromisoformat(value["deadline_at"])):
            raise ValueError
    except (ValueError, TypeError):
        raise EvidenceInvalid("RESULT_INVALID", "Parent attempt time differs from the original deadline") from None
    if number == 1 and value["previous_attempt_ref"] is not None:
        raise EvidenceInvalid("RESULT_INVALID", "Initial parent attempt has an unexpected predecessor")
    return value


def current_parent_attempt(run, admission=None):
    path = safe_path(run, "parent-attempt.json", must_exist=False)
    if not path.exists():
        return None
    admission = read_json(run, "admission.json") if admission is None else admission
    value = read_json(run, "parent-attempt.json")
    if (type(value) is not dict or set(value) != {"schema", "run_id", "number", "start_ref"}
            or value["schema"] != POINTER or value["run_id"] != run.name
            or type(value["number"]) is not int
            or value["start_ref"]["path"] != f"parent-attempts/{value['number']}/start.json"):
        raise EvidenceInvalid("RESULT_INVALID", "Parent attempt pointer is invalid")
    verify_ref(run, value["start_ref"], schema=ATTEMPT)
    return _read_start(run, value["number"], admission)


def assert_attempt_owner(run, attempt):
    current = current_parent_attempt(run)
    if current != attempt:
        raise EvidenceInvalid("PARENT_ATTEMPT_FENCED", "Parent attempt is no longer the admitted writer")


def begin_parent_attempt(run, admission, policy, *, previous_attempt_ref=None):
    _require(policy)
    _deadline(admission)
    current = current_parent_attempt(run, admission)
    if previous_attempt_ref is None and current is not None:
        return current
    number = 1 if current is None else current["number"] + 1
    if previous_attempt_ref is not None:
        if current is None or previous_attempt_ref["path"] != f"parent-attempts/{current['number']}/archive.json":
            raise EvidenceInvalid("RESULT_INVALID", "Parent recovery predecessor differs")
        verify_ref(run, previous_attempt_ref, schema=ARCHIVE)
    relative = f"parent-attempts/{number}/start.json"
    path = safe_path(run, relative, must_exist=False)
    if path.exists():
        value = _read_start(run, number, admission)
        if value["previous_attempt_ref"] != previous_attempt_ref:
            raise EvidenceInvalid("RESULT_INVALID", "Prepared parent attempt predecessor changed")
    else:
        value = {"schema": ATTEMPT, **_binding(run, admission), "number": number,
            "started_at": _now().isoformat(), "previous_attempt_ref": previous_attempt_ref,
            "events_path": f"parent-attempts/{number}/events.jsonl", "io_path": f"parent-attempts/{number}/io.jsonl"}
        atomic_json(run, relative, value, exclusive=True)
    atomic_json(run, "parent-attempt.json", {"schema": POINTER, "run_id": run.name, "number": number,
        "start_ref": file_ref(run, relative, ATTEMPT)})
    return value


def _inventory(root):
    checked_root(root)
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise EvidenceInvalid("RESULT_INVALID", "Recovery work contains a symbolic link")
        if path.is_dir():
            checked_root(path)
        elif path.is_file():
            result.append(file_ref(root, path.relative_to(root).as_posix(), "application/octet-stream"))
        else:
            raise EvidenceInvalid("RESULT_INVALID", "Recovery work contains an unsupported file")
    return result


def _archive_attempt(run, attempt, admission):
    number = attempt["number"]
    prefix = f"parent-attempts/{number}"
    seal_path = safe_path(run, prefix + "/archive.json", must_exist=False)
    if seal_path.exists():
        reference = file_ref(run, prefix + "/archive.json", ARCHIVE)
        _verify_archive(run, attempt, reference)
        return reference
    intent_path = safe_path(run, prefix + "/archive-intent.json", must_exist=False)
    if intent_path.exists():
        intent = read_json(run, prefix + "/archive-intent.json")
    else:
        roots = [file_ref(run, name, "application/octet-stream") for name in (*_ROTATED_FILES, "status.json")
                 if safe_path(run, name, must_exist=False).exists()]
        intent = {"schema": ARCHIVE_INTENT, **_binding(run, admission), "number": number,
            "root_files": roots, "work_files": _inventory(run / "work")}
        atomic_json(run, prefix + "/archive-intent.json", intent, exclusive=True)
    if (intent.get("schema") != ARCHIVE_INTENT or intent.get("number") != number
            or any(intent.get(name) != value for name, value in _binding(run, admission).items())
            or any(ref["path"] not in {*_ROTATED_FILES, "status.json"} for ref in intent["root_files"])):
        raise EvidenceInvalid("RESULT_INVALID", "Parent archive intent is invalid")
    checked_root(run / prefix / "archive", create=True)
    for ref in intent["root_files"]:
        destination = prefix + "/archive/" + ref["path"]
        target = safe_path(run, destination, must_exist=False)
        if target.exists():
            if digest(read_bytes(run, destination)) != ref["sha256"]:
                raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Archived parent artifact differs")
            continue
        raw = verify_ref(run, ref)
        if ref["path"] == "status.json":
            atomic_bytes(run, destination, raw, exclusive=True)
        else:
            os.rename(safe_path(run, ref["path"]), target)
            fsync_directory(target.parent)
            fsync_directory(run)
    old_work = run / prefix / "work"
    if not old_work.exists():
        if _inventory(run / "work") != intent["work_files"]:
            raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Stopped work changed during recovery")
        os.rename(checked_root(run / "work"), old_work)
        fsync_directory(run)
        fsync_directory(old_work.parent)
    if _inventory(old_work) != intent["work_files"]:
        raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Archived work differs from its original bytes")
    files = [{**ref, "path": prefix + "/archive/" + ref["path"]} for ref in intent["root_files"]]
    files += [{**ref, "path": prefix + "/work/" + ref["path"]} for ref in intent["work_files"]]
    for name in ("events.jsonl", "io.jsonl"):
        if safe_path(run, prefix + "/" + name, must_exist=False).exists():
            files.append(file_ref(run, prefix + "/" + name, "application/x-ndjson"))
    value = {"schema": ARCHIVE, **_binding(run, admission), "number": number,
        "start_ref": file_ref(run, prefix + "/start.json", ATTEMPT),
        "intent_ref": file_ref(run, prefix + "/archive-intent.json", ARCHIVE_INTENT),
        "files": sorted(files, key=lambda ref: ref["path"]), "business_completed": False}
    atomic_json(run, prefix + "/archive.json", value, exclusive=True)
    reference = file_ref(run, prefix + "/archive.json", ARCHIVE)
    _verify_archive(run, attempt, reference)
    return reference


def _verify_archive(run, attempt, reference):
    value = strict_json(verify_ref(run, reference, schema=ARCHIVE))
    admission = read_json(run, "admission.json")
    if (type(value) is not dict or value.get("schema") != ARCHIVE or value.get("number") != attempt["number"]
            or value.get("business_completed") is not False
            or any(value.get(name) != item for name, item in _binding(run, admission).items())
            or value.get("start_ref") != file_ref(run, f"parent-attempts/{attempt['number']}/start.json", ATTEMPT)):
        raise EvidenceInvalid("RESULT_INVALID", "Parent archive binding differs")
    verify_ref(run, value["intent_ref"], schema=ARCHIVE_INTENT)
    paths = []
    prefix = f"parent-attempts/{attempt['number']}/"
    for ref in value["files"]:
        if not ref["path"].startswith(prefix):
            raise EvidenceInvalid("RESULT_INVALID", "Parent archive escapes its attempt")
        verify_ref(run, ref)
        paths.append(ref["path"])
    if len(paths) != len(set(paths)):
        raise EvidenceInvalid("RESULT_INVALID", "Parent archive duplicates a file")
    return value


def _restore_work(run):
    before = read_json(run, "account-input.json")
    from .agent_runtime import ACCOUNT, REQUIRED_ACCOUNT_FILES, allowed_account_path, _calendar_input
    if (before.get("schema") != "tiger.account-input.v1" or before.get("account") != ACCOUNT
            or not REQUIRED_ACCOUNT_FILES <= {ref["path"] for ref in before["files"]}):
        raise EvidenceInvalid("RESULT_INVALID", "Original account before-state is invalid")
    checked_root(run / "work" / "reports", create=True)
    for name in ("options_hist", "review_proposals"):
        checked_root(run / "work" / "reports" / name, create=True)
    for ref in before["files"]:
        if not allowed_account_path(ref["path"]):
            raise EvidenceInvalid("RESULT_INVALID", "Original account path is not admitted")
        _write_equal(run, "work/reports/" + ref["path"], verify_ref(run / "account-before", ref))
    content, record = _calendar_input(run)
    if read_json(run, "calendar-input.json") != record:
        raise EvidenceInvalid("RESULT_INVALID", "Original calendar binding differs")
    _write_equal(run, record["work_path"], content)


def source_unknown_attempts(run):
    root = run / "intelligence"
    if not root.exists():
        return []
    checked_root(root)
    if not safe_path(root, "intelligence.sqlite3", must_exist=False).exists():
        return []
    from .intelligence.archive import Archive
    archive = Archive(root, readonly=True)
    archive.verify_chain()
    with archive._session() as connection:
        jobs = [row["job_id"] for row in connection.execute(
            "SELECT job_id FROM jobs WHERE tenant=? AND run_id=?", ("public", run.name))]
    return [{"job_id": job, "attempt_id": row["attempt_id"], "state": row["status"]}
            for job in jobs for row in archive.attempts(job) if row["status"] in {"inflight", "unknown"}]


def _recover_work_store(run, admission, policy, verifier):
    from .research_work import work_manifest
    from .tasks.store import WorkStore
    from .llm.json_observation import replay_attempt
    store = WorkStore.create(run / "tasks", work_manifest(admission))
    status = store.status()
    if not status["units"] and status["plan_sha256"] is None:
        if source_unknown_attempts(run):
            raise EvidenceInvalid("RECONCILIATION_REQUIRED", "An original external attempt remains unknown; retain its reservation")
        return store
    if status["cancel_requested"]:
        raise EvidenceInvalid("RESUME_NOT_RETRYABLE", "An explicitly cancelled research task cannot be resumed")
    for row in store.attempts():
        if row["state"] == "observed" and row["usage"] is not None:
            original = store.read_attempt_request(row["attempt_id"])
            native = store.read_attempt_response(row["attempt_id"])
            verdict = replay_attempt(native, original)
            usage = verdict["usage"]
            if usage is None or {name: usage[name] for name in row["usage"]} != row["usage"]:
                raise EvidenceInvalid("RECONCILIATION_REQUIRED", "Observed model usage did not pass native replay")
            store.settle_attempt(row["attempt_id"])
    current_attempts = store.attempts()
    if (any(row["state"] in {"dispatched", "observed"} for row in current_attempts)
            or source_unknown_attempts(run)):
        raise EvidenceInvalid("RECONCILIATION_REQUIRED", "An original external attempt remains unknown; retain its reservation")
    attempts_by_unit = {}
    for attempt in current_attempts:
        attempts_by_unit.setdefault(attempt["unit_id"], []).append(attempt)
    transient = []
    for row in status["units"]:
        if row["state"] != "failed":
            continue
        unit_attempts = attempts_by_unit.get(row["unit_id"], [])
        settled_only = all(attempt["state"] in {"settled", "not_sent"} for attempt in unit_attempts)
        if (row["error_code"] == "STORE_BUSY"
                or row["error_code"] in {"ATTEMPT_UNKNOWN", "UNIT_HANDLER_FAILED"}
                and settled_only):
            transient.append(row["unit_id"])
    if transient:
        store.requeue_transient(
            transient,
            allowed_codes={"STORE_BUSY", "ATTEMPT_UNKNOWN", "UNIT_HANDLER_FAILED"},
            reason="resume_reclassified_pre_fix_scheduler_contention",
        )
        status = store.status()
    if any(row["state"] == "failed" and not row["retryable"] for row in status["units"]):
        raise EvidenceInvalid("RESUME_NOT_RETRYABLE", "A complete non-retryable business failure remains a failure")
    store.recover_stopped_workers(verifier=verifier)
    store.verify()
    return store


def prepare_resume(run, admission, policy, *, stopped_verifier):
    _require(policy)
    _deadline(admission)
    if not callable(stopped_verifier) or stopped_verifier() is not None:
        raise EvidenceInvalid("RUNNER_FAILED", "Stopped parent ownership must be verified before recovery")
    current = current_parent_attempt(run, admission)
    if current is None:
        # A supervisor may have died before its first durable acceptance, after
        # the frozen admission existed. No old stream is upgraded to resumable.
        if safe_path(run, "events.jsonl", must_exist=False).exists():
            raise EvidenceInvalid("RESUME_UNSUPPORTED", "An existing historical event stream cannot be resumed")
        _restore_work(run)
        current = begin_parent_attempt(run, admission, policy)
        return {"attempt": current, "closeout_only": False}
    _recover_work_store(run, admission, policy, stopped_verifier)
    engine_path = safe_path(run, "engine-result.json", must_exist=False)
    if engine_path.exists():
        engine = read_json(run, "engine-result.json")
        if engine.get("success") is True and safe_path(run, "process-exit.json", must_exist=False).exists():
            process = read_json(run, "process-exit.json")
            if (process.get("engine_exit") == 0 and process.get("guardian_exit") == 0
                    and process.get("group_cleanup_before_reap") is True):
                return {"attempt": current, "closeout_only": True}
    for name in ("events.jsonl", "receipt.json", "account-version.json"):
        if safe_path(run, name, must_exist=False).exists():
            raise EvidenceInvalid("RESUME_NOT_RETRYABLE", "Prepared publication evidence requires its original closeout proof")
    archived = _archive_attempt(run, current, admission)
    _restore_work(run)
    _deadline(admission)
    return {"attempt": begin_parent_attempt(run, admission, policy, previous_attempt_ref=archived), "closeout_only": False}


def execution_context(run, admission, frozen_policy, **kwargs):
    from .redline.execution import ExecutionContext
    from .agent_evidence import EvidenceStore
    if not supported(frozen_policy):
        return ExecutionContext(store=EvidenceStore(run, run.name), **kwargs)
    from .research_replay import ParentEvidenceStore, ParentExecutionContext, provider_cache
    current = current_parent_attempt(run, admission)
    if current is None:
        raise EvidenceInvalid("RESULT_INVALID", "Admitted parent attempt is missing")
    previous = [_read_start(run, number, admission) for number in range(1, current["number"])]
    return ParentExecutionContext(store=ParentEvidenceStore(run, run.name, current), parent_attempt=current,
                                  provider_cache=provider_cache(run, previous), **kwargs)


def _verify_partial_events(run, attempt, policy):
    path = safe_path(run, attempt["events_path"], must_exist=False)
    if not path.exists():
        return
    raw = read_bytes(run, attempt["events_path"])
    if not raw.endswith(b"\n"):
        raw = raw[:raw.rfind(b"\n") + 1]
    previous = None
    fields = {"schema", "run_id", "sequence", "stage_index", "stage", "kind", "status", "input_ref",
        "output_ref", "previous_stage_ref", "reason", "error_type", "previous_event_sha256", "event_sha256"}
    for sequence, line in enumerate(raw.splitlines(), 1):
        row = strict_json(line)
        index = (sequence - 1) // 2
        if (type(row) is not dict or set(row) != fields or index >= len(policy["stages"])
                or row["schema"] != "tiger.execution-event.v1" or row["run_id"] != run.name
                or type(row["sequence"]) is not int or row["sequence"] != sequence
                or row["stage_index"] != index + 1 or row["stage"] != policy["stages"][index]["name"]
                or row["kind"] != ("begin" if sequence % 2 else "end")
                or row["previous_event_sha256"] != previous
                or row["event_sha256"] != digest(json_bytes({key: value for key, value in row.items() if key != "event_sha256"}))):
            raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Archived parent event chain differs")
        read_snapshot(run, row["input_ref"], run_id=run.name, stage=row["stage"], role="input")
        if row["output_ref"] is not None:
            read_snapshot(run, row["output_ref"], run_id=run.name, stage=row["stage"], role="output")
        previous = row["event_sha256"]


def _history_value(run, admission, policy, *, proof=None):
    from .research_replay import read_io_journal
    current = current_parent_attempt(run, admission)
    if current is None:
        raise EvidenceInvalid("RESULT_INVALID", "Parent attempt history is missing")
    starts, archives, prior_io = [], [], {}
    for number in range(1, current["number"] + 1):
        attempt = _read_start(run, number, admission)
        starts.append(file_ref(run, f"parent-attempts/{number}/start.json", ATTEMPT))
        if number > 1 and attempt["previous_attempt_ref"] != archives[-1]:
            raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Parent attempt predecessor chain differs")
        if number < current["number"]:
            reference = file_ref(run, f"parent-attempts/{number}/archive.json", ARCHIVE)
            _verify_archive(run, attempt, reference)
            archives.append(reference)
            _verify_partial_events(run, attempt, policy)
        rows = read_io_journal(run, number, allow_partial=number < current["number"])
        for row in rows:
            source = row["reused_from"]
            if source is not None:
                if (type(source) is not dict or set(source) != {"parent_attempt", "sha256"}
                        or type(source["parent_attempt"]) is not int or not 1 <= source["parent_attempt"] < number):
                    raise EvidenceInvalid("RESULT_INVALID", "Provider replay source identity differs")
                original = prior_io.get((source["parent_attempt"], source["sha256"]))
                if (original is None or original["kind"] != "end"
                        or any(row[name] != original[name] for name in
                               ("stage", "stage_index", "call_index", "method", "input_ref", "output_ref", "error_ref"))):
                    raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Provider replay changed the original observation")
            prior_io[(number, row["sha256"])] = row
    if proof is None:
        proof = verify_events(run, run.name, policy["stages"], event_relative=current["events_path"])
    elif type(proof) is not dict or not {
            "event_count", "stage_count", "last_event_sha256", "degraded", "evidence_refs"} <= set(proof):
        raise EvidenceInvalid("RESULT_INVALID", "Parent event proof is invalid")
    current_io = read_io_journal(run, current["number"])
    by_call = {(row["stage_index"], row["call_index"]): row for row in current_io if row["kind"] == "end"}
    events = [strict_json(line) for line in read_bytes(run, current["events_path"]).splitlines()]
    event_calls = {}
    for row in events[1::2]:
        output = read_snapshot(run, row["output_ref"], run_id=run.name, stage=row["stage"], role="output")
        for call in output["io_calls"]:
            event_calls[(row["stage_index"], call["index"])] = call
    if set(by_call) != set(event_calls):
        raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Parent event and I/O checkpoint inventories differ")
    for key, call in event_calls.items():
        journal = by_call[key]
        if (journal["method"] != call["method"] or journal["input_ref"] != call["input_ref"]
                or journal["output_ref"] != call.get("output_ref") or journal["error_ref"] != call.get("error_ref")):
            raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Parent I/O checkpoint differs from the completed stage")
    from .research_work import work_manifest
    from .tasks.store import WorkStore
    work = WorkStore.open(run / "tasks", work_manifest(admission)).verify()
    return {"schema": HISTORY, **_binding(run, admission), "completed_attempt": current["number"],
        "attempts": starts, "archives": archives,
        "events_ref": file_ref(run, current["events_path"], "application/x-ndjson"),
        "io_ref": file_ref(run, current["io_path"], "application/x-ndjson") if current_io else None,
        "event_tail_sha256": proof["last_event_sha256"], "work_store_event_tail_sha256": work["last_event_sha256"],
        "failed_attempts_remain_failures": True}


def publish_parent_events(run, admission, policy):
    """Copy the current attempt's canonical event stream before full closeout."""
    _require(policy)
    current = current_parent_attempt(run, admission)
    if current is None:
        raise EvidenceInvalid("RESULT_INVALID", "Parent attempt history is missing")
    source = file_ref(run, current["events_path"], "application/x-ndjson")
    _write_equal(run, "events.jsonl", verify_ref(run, source, schema="application/x-ndjson"))
    return file_ref(run, "events.jsonl", "application/x-ndjson")


def publish_parent_completion(run, admission, policy, *, proof=None):
    _require(policy)
    value = _history_value(run, admission, policy, proof=proof)
    _write_equal(run, "research-parent-history.json", json_bytes(value))
    publish_parent_events(run, admission, policy)
    return file_ref(run, "research-parent-history.json", HISTORY), value


def verify_parent_history(run, admission, policy, reference, *, expected=None):
    _require(policy)
    value = strict_json(verify_ref(run, reference, schema=HISTORY))
    expected = _history_value(run, admission, policy) if expected is None else expected
    if value != expected or read_bytes(run, "events.jsonl") != verify_ref(run, value["events_ref"], schema="application/x-ndjson"):
        raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Canonical parent history differs from its completed attempt")
    return value
