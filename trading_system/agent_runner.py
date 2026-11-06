"""Authoritative local JSON entry for one fixed, ordered public paper workflow.

No request supplies paths, commands, stage lists, model keys, tuning, or orders.
Submission identity is durable before worker launch; only a verified published
receipt can return business results.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import threading
import time
import uuid

from .agent_evidence import (
    EvidenceInvalid, atomic_json, checked_root, digest, json_bytes,
    read_bytes, read_json, safe_path, strict_json,
)
from .agent_runtime import (
    ACCOUNT, DEPENDENCIES, KEY_RE, PROFILE, SUPPORTED_PROFILES, RUN_RE, SOURCE_ROOT, SCENE_POLICY_RELATIVE,
    committed_pointer, freeze_source, frozen_clock, pointer_read, policy_manifest,
    run_environment, stage_account, stage_calendar, startup_runtime_input, validate_clock, verify_run,
    DEADLINE_SECONDS, INTELLIGENCE_PROFILE, RUNTIME_POLICY_ENV,
)
from .admission_guards import (PREPARATION_SECONDS, PreparationDeadline,
                               PreparationExpired, thread_submission)

OPERATIONS = frozenset({"doctor", "describe", "run", "status", "receipt", "verify"})
MAX_INPUT_BYTES = 65536
TERMINAL_FAILURES = frozenset({"failed", "blocked", "interrupted"})
EXIT_CODES = {
    "INVALID_REQUEST": 2, "INPUT_TOO_LARGE": 2,
    "READ_ONLY": 3, "LIVE_FORBIDDEN": 3, "UNAUTHORIZED": 3,
    "IDEMPOTENCY_CONFLICT": 5, "ACCOUNT_BUSY": 5, "ACCOUNT_VERSION_CONFLICT": 5,
    "DATA_UNAVAILABLE": 6, "DATA_STALE": 6, "SOURCE_SYNTHETIC": 6, "CURRENCY_BLOCKED": 6,
    "RUNNER_FAILED": 7, "RESULT_INVALID": 7, "EVIDENCE_MISSING": 7,
    "EVIDENCE_HASH_MISMATCH": 7, "ARTIFACT_WRITE_FAILED": 7, "RUN_NOT_FOUND": 7,
    "STEP_ORDER_VIOLATION": 8, "STEP_FAILED": 8, "RUN_INCOMPLETE": 8, "MODEL_REQUIRED_FAILED": 8,
    "TIMEOUT": 9, "INTERRUPTED": 9, "CANCELLED": 130,
}


def envelope(operation: str | None, *, run_id: str | None = None,
             status: str = "ready", data: dict | None = None) -> dict:
    return {
        "schema_version": "tiger.agent-result/v1", "operation": operation,
        "operation_ok": True, "run_id": run_id, "status": status,
        "execution_mode": "paper" if run_id else None,
        "data": data or {}, "summary": None, "verification": None,
        "limitations": [], "evidence_refs": [], "error": None,
    }


def failure(operation: str | None, code: str, message: str, *,
            run_id: str | None = None, status: str = "invalid",
            stage: str | None = None, data: dict | None = None,
            details_ref: str | None = None) -> dict:
    value = envelope(operation, run_id=run_id, status=status, data=data)
    value["operation_ok"] = False
    value["error"] = {
        "code": code, "message": message, "run_id": run_id, "stage": stage,
        "retryable": code == "ACCOUNT_BUSY", "details_ref": details_ref,
    }
    return value


def validate_request(operation: str, request: object) -> dict:
    if operation not in OPERATIONS or not isinstance(request, dict):
        raise EvidenceInvalid("INVALID_REQUEST", "Unknown operation or non-object request")
    fields = {
        "doctor": set(), "describe": {"profile"},
        "run": {"schema_version", "profile", "paper_account", "idempotency_key"},
        "status": {"run_id"}, "receipt": {"run_id"}, "verify": {"run_id"},
    }[operation]
    if set(request) != fields:
        raise EvidenceInvalid("INVALID_REQUEST", "Request fields differ from the fixed contract")
    if operation in {"describe", "run"} and (not isinstance(request["profile"], str)
                                            or request["profile"] not in SUPPORTED_PROFILES):
        raise EvidenceInvalid("INVALID_REQUEST", "Unsupported workflow profile")
    if operation == "run":
        if (request["schema_version"] != "tiger.agent-request/v1"
                or request["paper_account"] != ACCOUNT
                or not isinstance(request["idempotency_key"], str)
                or not KEY_RE.fullmatch(request["idempotency_key"])):
            raise EvidenceInvalid("INVALID_REQUEST", "Invalid fixed-profile submission")
    if operation in {"status", "receipt", "verify"}:
        if not isinstance(request["run_id"], str) or not RUN_RE.fullmatch(request["run_id"]):
            raise EvidenceInvalid("INVALID_REQUEST", "Invalid run identity")
    return request


def _platform() -> None:
    if sys.version_info < (3, 11):
        raise EvidenceInvalid("RUNNER_FAILED", "This runner requires Python 3.11 or newer")
    if os.name != "posix" or not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY")):
        raise EvidenceInvalid("RUNNER_FAILED", "This runner requires POSIX flock and durable directory writes")
    try:
        import fcntl
    except ImportError as exc:
        raise EvidenceInvalid("RUNNER_FAILED", "POSIX file locks are unavailable") from exc


def _lock_fd(root: Path, name: str, *, create: bool, nonblocking: bool) -> int:
    import fcntl
    path = safe_path(root, name, must_exist=not create)
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise EvidenceInvalid("RESULT_INVALID", "Unsafe workflow lock")
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def admission_lock(state: Path, *, deadline: float | None = None):
    # Account workers never inherit this short submission lock. Polling makes
    # contention bounded even when flock would restart an interrupted syscall.
    deadline = deadline if deadline is not None else time.monotonic() + PREPARATION_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise EvidenceInvalid("TIMEOUT", "Preparation lock deadline elapsed", stage="admission.lock")
        try:
            descriptor = _lock_fd(state, "submission.lock", create=True, nonblocking=True)
            break
        except BlockingIOError:
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        os.close(descriptor)


def _is_active(run: Path) -> bool:
    if not (run / "run.lock").exists():
        return False
    try:
        descriptor = _lock_fd(run, "run.lock", create=False, nonblocking=True)
    except BlockingIOError:
        return True
    os.close(descriptor)
    return False


def observe_run(state: Path, run_id: str) -> dict:
    run = state / "runs" / run_id
    if not run.exists() and not run.is_symlink():
        raise EvidenceInvalid("RUN_NOT_FOUND", "Run identity is not present")
    checked_root(run)
    # Sample liveness before mutable status and commitment. A run's writers
    # retain this lock through their final writes and never restart that run.
    # A held sample may conservatively stay running for one poll; an unlocked
    # sample permits terminal reads. Sampling after older file reads could
    # invent an interruption when the worker finishes between those reads.
    active = _is_active(run)
    persisted = read_json(run, "status.json") if (run / "status.json").exists() else {}
    if (not isinstance(persisted, dict) or (persisted and (
            persisted.get("schema") != "tiger.status.v1"
            or persisted.get("run_id") != run_id
            or persisted.get("status") not in {
                "accepted", "running", "completed", "completed_degraded", *TERMINAL_FAILURES}))):
        raise EvidenceInvalid("RESULT_INVALID", "Invalid persisted run status")
    observed = persisted.get("status")
    # Status never returns the summary. Commitment precedence handles a crash
    # after publication but before the mutable status write.
    pointer = committed_pointer(state, run_id)
    if pointer is not None:
        receipt = read_json(run, "receipt.json")
        status = receipt.get("status")
        if status not in {"completed", "completed_degraded"}:
            raise EvidenceInvalid("RESULT_INVALID", "Committed receipt has invalid status")
        return {"status": status, "observed_status": observed,
                "commitment_present": True, "active": active, "error": None}
    if observed in TERMINAL_FAILURES and not active:
        error = persisted.get("error")
        if (not isinstance(error, dict) or error.get("code") not in EXIT_CODES
                or error.get("run_id") != run_id
                or set(error) != {"code", "message", "run_id", "stage", "retryable", "details_ref"}):
            raise EvidenceInvalid("RESULT_INVALID", "Invalid terminal failure record")
        return {"status": observed, "observed_status": observed,
                "commitment_present": False, "active": False, "error": error}
    if active:
        accepted = None
        if (run / "accepted.json").exists():
            accepted = read_json(run, "accepted.json")
            if (not isinstance(accepted, dict) or accepted.get("schema") != "tiger.accepted.v1"
                    or accepted.get("run_id") != run_id
                    or accepted.get("admission_sha256") != digest(read_bytes(run, "admission.json"))):
                raise EvidenceInvalid("RESULT_INVALID", "Acceptance binding differs")
        return {"status": "running" if accepted else "accepted", "observed_status": observed,
                "commitment_present": False, "active": True, "error": None}
    error = failure("run", "INTERRUPTED", "Worker is no longer active and no account commit exists.",
                    run_id=run_id, status="interrupted")["error"]
    return {"status": "interrupted", "observed_status": observed,
            "commitment_present": False, "active": False, "error": error}


def _replay(state: Path, run_id: str) -> dict:
    observed = observe_run(state, run_id)
    status = observed["status"]
    value = envelope("run", run_id=run_id,
                     status="accepted" if status in {"completed", "completed_degraded"} else status,
                     data={"idempotent_replay": True, "observed_status": status})
    if status in TERMINAL_FAILURES:
        value["operation_ok"] = False
        value["error"] = observed["error"]
    return value


def _acknowledgement(fd: int, run_id: str, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    content = bytearray()
    os.set_blocking(fd, False)
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EvidenceInvalid("TIMEOUT", "Acceptance response timed out; recover with the same key.")
            if not selector.select(min(remaining, 0.2)):
                continue
            try:
                block = os.read(fd, 4096)
            except BlockingIOError:
                continue
            if not block:
                break
            content.extend(block)
            if len(content) > 4096:
                raise EvidenceInvalid("RESULT_INVALID", "Acceptance response exceeded its limit")
    try:
        value = strict_json(bytes(content))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceInvalid("RUNNER_FAILED", "Worker exited before durable acceptance") from exc
    if value != {"accepted": True, "run_id": run_id}:
        raise EvidenceInvalid("RESULT_INVALID", "Worker acceptance identity differs")


def submit(state: Path, request: dict, *, source: Path = SOURCE_ROOT,
           _preparation_seconds: float = PREPARATION_SECONDS,
           _entry_clock: dict | None = None) -> dict:
    """Bound all preparation without cancelling an accepted detached worker."""
    try:
        clock = frozen_clock() if _entry_clock is None else _entry_clock
        validate_clock(clock)
        validate_request("run", request)
        if threading.current_thread() is not threading.main_thread():
            return thread_submission(state, request, source, seconds=_preparation_seconds, entry_clock=clock)
        with PreparationDeadline(_preparation_seconds) as guard:
            return _submit(state, request, source=source, preparation_deadline=guard.deadline,
                           entry_clock=clock)
    except (EvidenceInvalid, PreparationExpired) as exc:
        return failure("run", exc.code, str(exc), stage=exc.stage)


def _submit(state: Path, request: dict, *, source: Path,
            preparation_deadline: float, entry_clock: dict) -> dict:
    run_id = None
    created = False
    account_fd = run_fd = ack_read = ack_write = -1
    worker = None
    try:
        clock = entry_clock
        checked_root(state, create=True)
        with admission_lock(state, deadline=preparation_deadline):
            mapping_path = "keys/" + digest(request["idempotency_key"].encode("ascii")) + ".json"
            map_file = safe_path(state, mapping_path, must_exist=False)
            request_hash = digest(json_bytes(request))
            if map_file.exists():
                mapping = read_json(state, mapping_path)
                if (not isinstance(mapping, dict) or set(mapping) != {
                        "schema", "run_id", "request_sha256"}
                        or mapping.get("schema") != "tiger.idempotency.v1"
                        or not isinstance(mapping.get("run_id"), str)
                        or not RUN_RE.fullmatch(mapping["run_id"])):
                    raise EvidenceInvalid("RESULT_INVALID", "Invalid idempotency mapping")
                run_id = mapping["run_id"]
                if mapping["request_sha256"] != request_hash:
                    raise EvidenceInvalid("IDEMPOTENCY_CONFLICT", "Key is bound to a different request")
                if read_json(state / "runs" / run_id, "request.json") != request:
                    raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Stored request binding differs")
                return _replay(state, run_id)
            # Replays above use their original frozen contract, regardless of
            # later changes to the trusted startup file or its availability.
            runtime_input = startup_runtime_input(request["profile"])
            if runtime_input is not None:
                from .agent_runtime import frozen_clock as derive_clock
                clock = derive_clock(datetime.fromisoformat(clock["host_timestamp"]),
                                     seconds=runtime_input["business_deadline_seconds"])
            try:
                account_fd = _lock_fd(state, "account.lock", create=True, nonblocking=True)
            except BlockingIOError as exc:
                raise EvidenceInvalid("ACCOUNT_BUSY", "The paper account already has an active run; key was not consumed.") from exc
            run_id = "run-" + uuid.uuid4().hex
            run = state / "runs" / run_id
            checked_root(run, create=True)
            created = True
            run_fd = _lock_fd(run, "run.lock", create=True, nonblocking=True)
            atomic_json(run, "request.json", request, exclusive=True)
            # Identity survives incomplete preparation too; a replay never
            # silently performs the operation again with the same key.
            atomic_json(state, mapping_path, {
                "schema": "tiger.idempotency.v1", "run_id": run_id,
                "request_sha256": request_hash,
            }, exclusive=True)
            atomic_json(run, "status.json", {
                "schema": "tiger.status.v1", "run_id": run_id,
                "status": "accepted", "error": None,
            })
            parent = pointer_read(state)
            freeze_source(run, source)
            if runtime_input is not None:
                atomic_json(run, "runtime-input.json", runtime_input, exclusive=True)
            intelligence_input = None
            from .redline.execution import INTELLIGENCE_PROFILE
            if request["profile"] == INTELLIGENCE_PROFILE:
                from .intelligence_workflow import stage_input_packet
                intelligence_input = stage_input_packet(run,
                    as_of=datetime.fromisoformat(clock["admitted_at_utc"]))
            policy = policy_manifest(request["profile"],
                                     scene_policy_path=run / "source" / SCENE_POLICY_RELATIVE,
                                     intelligence_input=intelligence_input, runtime_input=runtime_input)
            atomic_json(run, "policy.json", policy, exclusive=True)
            stage_account(state, run, parent)
            stage_calendar(run)
            admission = {
                "schema": "tiger.admission.v1", "run_id": run_id, "clock": clock,
                "parent": parent, "request_sha256": request_hash,
                "policy_sha256": digest(json_bytes(policy)),
                "source_sha256": digest(read_bytes(run, "source-manifest.json")),
            }
            if runtime_input is not None:
                admission["runtime_input_sha256"] = digest(json_bytes(runtime_input))
            if datetime.now(timezone.utc) >= datetime.fromisoformat(clock["deadline_at_utc"]):
                raise EvidenceInvalid("TIMEOUT", "Business deadline elapsed during preparation", stage="admission.prepare")
            atomic_json(run, "admission.json", admission, exclusive=True)
            ack_read, ack_write = os.pipe()
            entry = run / "source" / "trading_system" / "agent_supervisor.py"
            worker = subprocess.Popen(
                [sys.executable, "-I", "-B", str(entry), "supervisor", str(state), run_id,
                 str(account_fd), str(run_fd), str(ack_write)],
                cwd=run / "work", env=run_environment(run / "work", model_required=policy["execution"]["model_mode"] == "enabled"),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
                pass_fds=(account_fd, run_fd, ack_write),
            )
            os.close(ack_write)
            ack_write = -1
            # Close our duplicate references; never LOCK_UN shared inherited
            # descriptions because the independent worker owns the same locks.
            os.close(account_fd)
            account_fd = -1
            os.close(run_fd)
            run_fd = -1
            _acknowledgement(ack_read, run_id)
            return envelope("run", run_id=run_id, status="accepted",
                            data={"idempotent_replay": False, "observed_status": "running"})
    except (OSError, EvidenceInvalid, ValueError, PreparationExpired) as exc:
        controlled = isinstance(exc, (EvidenceInvalid, PreparationExpired))
        code = exc.code if controlled else "ARTIFACT_WRITE_FAILED" if isinstance(exc, OSError) else "RUNNER_FAILED"
        message = str(exc) if controlled else "Unable to prepare the controlled run."
        value = failure("run", code, message, run_id=run_id,
                        status="blocked" if code == "ACCOUNT_BUSY" else "invalid",
                        stage=exc.stage if controlled else None,
                        data={"identity_recovery": "Repeat the identical request and idempotency key."}
                        if run_id else {})
        if created and run_id is not None and worker is None:
            try:
                atomic_json(state / "runs" / run_id, "status.json", {
                    "schema": "tiger.status.v1", "run_id": run_id,
                    "status": "failed", "error": value["error"],
                })
            except (OSError, ValueError):
                sys.stderr.write("Admission failure could not be persisted; liveness readback remains authoritative.\n")
        return value
    finally:
        for descriptor in (account_fd, run_fd, ack_read, ack_write):
            if descriptor >= 0:
                os.close(descriptor)


def doctor(state: Path, mode: str) -> dict:
    runtime_input = startup_runtime_input()
    packages = {}
    for name in DEPENDENCIES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    python_supported = sys.version_info >= (3, 11)
    ready = python_supported and all(packages.values())
    value = envelope("doctor", status="ready" if ready else "not_ready", data={
        "profile": PROFILE, "access_mode": mode, "python_version": sys.version.split()[0],
        "dependencies": packages, "state_root_exists": state.exists(),
        "platform": sys.platform, "state_writes": False, "network_requests": False,
        "business_workers_started": False,
        "business_runtime": {"profile": INTELLIGENCE_PROFILE,
            "startup_environment": RUNTIME_POLICY_ENV, "configured": runtime_input is not None,
            "business_deadline_seconds": runtime_input["business_deadline_seconds"] if runtime_input else DEADLINE_SECONDS,
            "input_sha256": digest(json_bytes(runtime_input)) if runtime_input else None,
            "values_are_frozen_on_new_submission": True},
    })
    value["limitations"] = policy_manifest()["limitations"]
    if not ready:
        value["operation_ok"] = False
        message = ("This runner requires Python 3.11 or newer."
                   if not python_supported else "Required Python dependencies are missing.")
        value["error"] = failure("doctor", "RUNNER_FAILED", message)["error"]
    return value


def execute(operation: str, request: object, *, mode: str, state: Path,
            _entry_clock: dict | None = None) -> dict:
    run_id = None
    observed = None
    try:
        # Direct Python callers use the same entry boundary as the CLI. The
        # private clock never appears among the public request fields.
        entry_clock = (frozen_clock() if _entry_clock is None else _entry_clock) if operation == "run" else None
        request = validate_request(operation, request)
        run_id = request.get("run_id")
        if mode not in {"readonly", "paper"}:
            raise EvidenceInvalid("INVALID_REQUEST", "Invalid startup access mode")
        if operation == "run" and mode != "paper":
            raise EvidenceInvalid("READ_ONLY", "Paper submission requires explicit paper startup mode")
        _platform()
        if operation == "run":
            # Root/path validation belongs to the bounded preparation too.
            return submit(state, request, _entry_clock=entry_clock)
        checked_root(state)
        if operation == "doctor":
            return doctor(state, mode)
        if operation == "describe":
            selected = policy_manifest(request["profile"], runtime_input=startup_runtime_input(request["profile"]))
            value = envelope(operation, data={"profile": selected, "access_mode": mode})
            value["limitations"] = selected["limitations"]
            return value
        observation = observe_run(state, run_id)
        observed = observation["status"]
        if operation == "status":
            return envelope(operation, run_id=run_id, status=observed, data=observation)
        if observed not in {"completed", "completed_degraded"}:
            return failure(operation, "RUN_INCOMPLETE", "Run has no verified published completion.",
                           run_id=run_id, status=observed, data={"observed_status": observed})
        result = verify_run(state, run_id)
        value = envelope(operation, run_id=run_id, status=result["status"], data={
            "source": result["source"], "current_source_matches": result["current_source_matches"],
            "degraded": result["degraded"], "observed_status": observed,
        })
        for key in ("summary", "verification", "limitations", "evidence_refs"):
            value[key] = result[key]
        if operation == "verify":
            value["summary"] = None
        return value
    except EvidenceInvalid as exc:
        return failure(operation if operation in OPERATIONS else None, exc.code, str(exc),
                       run_id=run_id, stage=exc.stage,
                       status="not_found" if exc.code == "RUN_NOT_FOUND" else "invalid",
                       data={"observed_status": observed} if observed else {})
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return failure(operation if operation in OPERATIONS else None, "RESULT_INVALID",
                       "Stored workflow evidence or runtime configuration is invalid.",
                       run_id=run_id, data={"observed_status": observed} if observed else {})


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise EvidenceInvalid("INVALID_REQUEST", "Invalid startup arguments")


def main(argv: list[str] | None = None) -> int:
    operation = None
    try:
        # Input reading, JSON validation and every preparation step consume
        # this original wall. A trusted file may extend its duration later;
        # it cannot reset the admitted instant.
        entry_clock = frozen_clock()
        parser = _Parser(add_help=False)
        parser.add_argument("--mode", default="readonly")
        parser.add_argument("--state-root", required=True)
        parser.add_argument("operation")
        arguments = parser.parse_args(argv)
        operation = arguments.operation if arguments.operation in OPERATIONS else None
        def read_and_execute():
            data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if len(data) > MAX_INPUT_BYTES:
                raise EvidenceInvalid("INPUT_TOO_LARGE", "Request exceeds 65536 bytes")
            try:
                request = strict_json(data)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise EvidenceInvalid("INVALID_REQUEST", "Request must be one strict UTF-8 JSON object") from exc
            return execute(arguments.operation, request, mode=arguments.mode,
                           state=Path(arguments.state_root), _entry_clock=entry_clock)
        if arguments.operation == "run":
            # The existing preparation lease also owns a blocked stdin read;
            # nested submit guards preserve this earlier timer.
            with PreparationDeadline(PREPARATION_SECONDS):
                value = read_and_execute()
        else:
            value = read_and_execute()
    except (EvidenceInvalid, PreparationExpired) as exc:
        value = failure(operation, exc.code, str(exc), stage=exc.stage)
    except KeyboardInterrupt:
        value = failure(operation, "CANCELLED", "Client stopped observing; recover unknown identity with the same key.")
    except (OSError, ValueError):
        value = failure(operation, "RUNNER_FAILED", "Runner configuration or request input is unavailable.")
    try:
        sys.stdout.buffer.write(json_bytes(value))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        return 130
    return 0 if value["operation_ok"] else EXIT_CODES.get(value["error"]["code"], 7)


if __name__ == "__main__":
    raise SystemExit(main())
