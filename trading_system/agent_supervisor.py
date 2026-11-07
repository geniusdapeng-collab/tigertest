"""Independent supervisor, parent-death guardian and isolated business engine.

The guardian leads the business process group. The supervisor retains its PID
until it has cleaned that group, then reaps its direct child. Engine processes
inherit no account locks, client pipes, or supervisor-liveness descriptors.
"""

from __future__ import annotations

if not __package__:  # Absolute frozen-file entry, invoked with Python -I -B.
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
from datetime import date, datetime, timezone
import importlib.util
import logging
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time

from trading_system.agent_evidence import (
    EvidenceInvalid, EvidenceStore, atomic_json, checked_root, digest, file_ref,
    read_bytes, read_json, safe_path, strict_json,
)
from trading_system.agent_runtime import (
    POLICY_ID, DEADLINE_SECONDS, dependency_identity, run_environment, seal_and_publish,
    validate_admission, verify_source,
)
from trading_system.admission_guards import BOOTSTRAP_SECONDS, PreparationDeadline, PreparationExpired
from trading_system.redline.execution import (
    EvidenceWriteError, ExecutionContext, ExecutionContractError,
    ExecutionPolicy, RuntimePaths,
)

LOG_LIMIT_BYTES = 8 * 1024 * 1024
CONTROL_LIMIT_BYTES = 65536
MAX_LOG_LINE_BYTES = 16384


class SupervisorInterrupted(RuntimeError):
    """Signal interruption that select() cannot swallow as EINTR."""


def error_record(exc: BaseException, *, run_id: str, stage: str | None = None) -> dict:
    name = type(exc).__name__
    if isinstance(exc, (EvidenceInvalid, PreparationExpired)):
        code = exc.code
        stage = exc.stage or stage
    elif (name, getattr(exc, "code", None)) in {
            ("StrictDataUnavailable", "DATA_UNAVAILABLE"),
            ("StrictObservationError", "DATA_UNAVAILABLE"),
            ("StrictDataStale", "DATA_STALE"),
            ("StrictSourceSynthetic", "SOURCE_SYNTHETIC"),
            ("RequiredModelFailed", "MODEL_REQUIRED_FAILED")}:
        code = exc.code
    elif isinstance(exc, TimeoutError):
        code = "TIMEOUT"
    elif isinstance(exc, (InterruptedError, SupervisorInterrupted)):
        code = "INTERRUPTED"
    elif isinstance(exc, EvidenceWriteError) or isinstance(exc, OSError):
        code = "ARTIFACT_WRITE_FAILED"
    elif isinstance(exc, ExecutionContractError):
        text = str(exc)
        code = ("STEP_ORDER_VIOLATION" if text.startswith(("Expected stage", "Stages cannot"))
                else "RUN_INCOMPLETE" if "complete" in text or "output" in text
                else "STEP_FAILED")
    elif name == "CurrencyBoundaryError":
        code = "CURRENCY_BLOCKED"
    else:
        code = "RUNNER_FAILED"
    return {"code": code, "message": f"Workflow stopped ({name}); inspect the bound run evidence.",
            "run_id": run_id, "stage": stage, "retryable": False,
            "details_ref": "engine-result.json"}


def _safe_log_line(data: bytes) -> bytes:
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r'''(?i)(["']?authorization["']?\s*[:=]\s*)[^\r\n,;}]+''',
                  r"\1[REDACTED]", text)
    text = re.sub(r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|password|secret)["']?'''
                  r'''\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                  r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]*", r"\1?[REDACTED_QUERY]", text)
    return text.encode("utf-8")


class BoundedLog:
    def __init__(self, run: Path, limit: int = LOG_LIMIT_BYTES):
        path = safe_path(run, "engine.log", must_exist=False)
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.limit = limit
        self.retained = 0
        self.received = 0
        self.budget_truncated = False
        self.oversized_lines = 0
        self.buffers: dict[str, bytearray] = {}
        self.discard_line: set[str] = set()

    def _write(self, label: str, content: bytes) -> None:
        key = os.environ.get("LLM_API_KEY", "")
        if key:
            content = content.replace(key.encode(), b"[REDACTED]")
        safe = label.encode() + b": " + _safe_log_line(content) + b"\n"
        room = max(0, self.limit - self.retained)
        if len(safe) > room:
            self.budget_truncated = True
        if room:
            data = safe[:room]
            view = memoryview(data)
            while view:
                written = os.write(self.fd, view)
                view = view[written:]
            self.retained += len(data)

    def consume(self, label: str, data: bytes) -> None:
        self.received += len(data)
        pending = self.buffers.setdefault(label, bytearray())
        # Keep reading even after the retained budget is exhausted.
        if self.retained >= self.limit:
            if data:
                self.budget_truncated = True
            pending.clear()
            return
        for piece in data.splitlines(keepends=True):
            ended = piece.endswith((b"\n", b"\r"))
            if label in self.discard_line:
                if ended:
                    self.discard_line.remove(label)
                continue
            if len(pending) + len(piece) > MAX_LOG_LINE_BYTES:
                pending.clear()
                self.oversized_lines += 1
                self._write(label, b"[oversized diagnostic line omitted]")
                if not ended:
                    self.discard_line.add(label)
                continue
            pending.extend(piece)
            if ended:
                self._write(label, bytes(pending).rstrip(b"\r\n"))
                pending.clear()

    def finish(self) -> dict:
        try:
            for label, content in self.buffers.items():
                if content:
                    self._write(label, bytes(content))
            os.fsync(self.fd)
        finally:
            os.close(self.fd)
        return {"received_bytes": self.received, "retained_bytes": self.retained,
                "limit_bytes": self.limit,
                "truncated": self.budget_truncated or self.oversized_lines > 0,
                "budget_truncated": self.budget_truncated,
                "oversized_lines": self.oversized_lines,
                "diagnostics_are_not_business_receipts": True}


def _send_control(fd: int, value: dict) -> None:
    from trading_system.agent_evidence import json_bytes
    content = json_bytes(value)
    if len(content) > CONTROL_LIMIT_BYTES:
        raise ValueError("Oversized guardian control record")
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _kill_group(pgid: int, *, grace_seconds: float = 0.25) -> None:
    if pgid <= 1 or pgid == os.getpgrp():
        raise RuntimeError("Refusing an unowned process group")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        if _darwin_zombie_group(pgid):
            return
        raise
    time.sleep(grace_seconds)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        # Group can disappear between the two bounded cleanup attempts.
        return
    except PermissionError:
        if _darwin_zombie_group(pgid):
            return
        raise


def _darwin_zombie_group(pgid: int) -> bool:
    """Darwin returns EPERM for a group containing only unreaped zombies.

    Only accept that case after an OS table read proves the retained group
    leader is still that zombie and every listed member is also dead. A live
    member, missing leader, malformed ps output, or any other platform fails.
    The caller has deliberately not reaped the leader, preventing PID reuse.
    """
    if sys.platform != "darwin":
        return False
    result = subprocess.run(
        ["/bin/ps", "-ax", "-o", "pid=,pgid=,stat="],
        capture_output=True, text=True, timeout=3, check=True,
        env={"PATH": os.defpath, "LC_ALL": "C"},
    )
    members = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            return False
        if int(parts[1]) == pgid:
            members.append((int(parts[0]), parts[2]))
    return (any(pid == pgid for pid, _ in members)
            and all(status.startswith("Z") for _, status in members))


def _guardian_abort(control_fd: int, reason: str) -> None:
    try:
        _send_control(control_fd, {"kind": "failed", "reason": reason})
    except (OSError, ValueError):
        # A dead supervisor cannot receive a control frame. Liveness cleanup
        # remains mandatory and does not depend on persisting that frame.
        pass
    group = os.getpgrp()
    if group != os.getpid():
        os._exit(71)
    os.killpg(group, signal.SIGTERM)
    time.sleep(0.25)
    os.killpg(group, signal.SIGKILL)
    os._exit(72)  # Unreachable when group KILL succeeds.


def guardian(run: Path, *, parent_fd: int, control_fd: int, deadline: float,
             command: list[str] | None = None, log_limit: int = LOG_LIMIT_BYTES) -> int:
    """Run only a trusted internal command; no public request accepts argv."""
    if os.getpgrp() != os.getpid():
        raise RuntimeError("Guardian must own a new process group")
    # A caught disposition is reset by exec in the engine. Guardian survives its
    # own group TERM long enough to perform the bounded group KILL.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
    signal.signal(signal.SIGINT, lambda _signum, _frame: None)
    entry = run / "source" / "trading_system" / "agent_supervisor.py"
    command = command or [sys.executable, "-I", "-B", str(entry), "engine", str(run)]
    logger = BoundedLog(run, log_limit)
    child = None
    normal = False
    try:
        child = subprocess.Popen(
            command, cwd=run / "work", env=run_environment(run / "work", model_required=(run / "policy.json").exists() and read_json(run, "policy.json")["execution"]["model_mode"] == "enabled"),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, pass_fds=(), start_new_session=False,
        )
        process_info = read_json(run, "processes.json")
        process_info.update(guardian_pid=os.getpid(), engine_pid=child.pid,
                            engine_process_group=os.getpgrp())
        atomic_json(run, "processes.json", process_info)
        _send_control(control_fd, {"kind": "engine_started", "pid": child.pid,
                                  "pgid": os.getpgrp()})
        with selectors.DefaultSelector() as selector:
            os.set_blocking(parent_fd, False)
            selector.register(parent_fd, selectors.EVENT_READ, "parent")
            streams = {child.stdout.fileno(): "stdout", child.stderr.fileno(): "stderr"}
            for fd, label in streams.items():
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ, label)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _guardian_abort(control_fd, "deadline")
                events = selector.select(min(0.2, remaining))
                for key, _mask in events:
                    try:
                        chunk = os.read(key.fd, 8192)
                    except BlockingIOError:
                        continue
                    if key.data == "parent":
                        if not chunk:
                            _guardian_abort(control_fd, "supervisor_liveness_eof")
                        # The only legal liveness signal is an open, silent pipe.
                        _guardian_abort(control_fd, "unexpected_liveness_data")
                    if chunk:
                        logger.consume(key.data, chunk)
                    else:
                        selector.unregister(key.fd)
                        streams.pop(key.fd, None)
                # Child polling is safe here: guardian, not engine, remains the
                # un-reaped process-group leader under supervisor ownership.
                if not streams and child.poll() is not None:
                    code = child.wait()
                    log_summary = logger.finish()
                    logger = None
                    atomic_json(run, "log-summary.json", log_summary, exclusive=True)
                    _send_control(control_fd, {"kind": "engine_finished", "exit_code": code})
                    normal = True
                    return 0 if code == 0 else 1
    except BaseException:
        if child is not None:
            _guardian_abort(control_fd, "guardian_failure")
        raise
    finally:
        if logger is not None:
            logger.finish()
        if child is not None:
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        os.close(parent_fd)
        os.close(control_fd)


def _read_control_until_eof(fd: int, deadline: float) -> list[dict]:
    data = bytearray()
    os.set_blocking(fd, False)
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Business deadline elapsed")
            if not selector.select(min(0.2, remaining)):
                continue
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                continue
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > CONTROL_LIMIT_BYTES:
                raise EvidenceInvalid("RESULT_INVALID", "Guardian control exceeded limit")
    if not data or not data.endswith(b"\n"):
        raise EvidenceInvalid("RUN_INCOMPLETE", "Guardian control ended without a complete record")
    try:
        rows = [strict_json(line) for line in data.splitlines()]
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Invalid guardian control JSON") from exc
    return rows


def _wait_for_owned_exit(pid: int, deadline: float) -> None:
    """Observe termination without reaping the retained process-group leader."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Business deadline elapsed")
        if sys.platform == "linux":
            try:
                result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError as exc:
                raise EvidenceInvalid("RUNNER_FAILED", "Guardian ownership was lost") from exc
            exited = result is not None
            if exited and (result.si_pid != pid or result.si_code not in {
                    os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED}):
                raise EvidenceInvalid("RUNNER_FAILED", "Unexpected guardian wait state")
        elif sys.platform == "darwin":
            try:
                result = subprocess.run(
                    ["/bin/ps", "-p", str(pid), "-o", "pid=,ppid=,pgid=,stat="],
                    capture_output=True, text=True, timeout=min(3, remaining), check=True,
                    env={"PATH": os.defpath, "LC_ALL": "C"},
                )
            except subprocess.TimeoutExpired as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Business deadline elapsed") from exc
                raise EvidenceInvalid("RUNNER_FAILED", "Guardian state query timed out") from exc
            except TimeoutError:
                # The supervisor's business alarm may interrupt the query.
                # Its TimeoutError is an OSError, but retains TIMEOUT semantics.
                raise
            except (OSError, subprocess.CalledProcessError) as exc:
                raise EvidenceInvalid("RUNNER_FAILED", "Guardian state query failed") from exc
            rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
            if (len(rows) != 1 or len(rows[0]) != 4
                    or not all(value.isdigit() for value in rows[0][:3])
                    or tuple(map(int, rows[0][:3])) != (pid, os.getpid(), pid)):
                raise EvidenceInvalid("RUNNER_FAILED", "Guardian process identity differs")
            exited = rows[0][3].startswith("Z")
        else:
            raise EvidenceInvalid("RUNNER_FAILED", "Unsupported guardian wait platform")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Business deadline elapsed")
        if exited:
            return
        time.sleep(min(0.05, remaining))


def supervise_group(run: Path, *, lock_fds: tuple[int, int], deadline: float) -> dict:
    """Validate control, observe exit, then clean the group before reaping."""
    parent_read, parent_write = os.pipe()
    control_read, control_write = os.pipe()
    leader = None
    rows = None
    entry = run / "source" / "trading_system" / "agent_supervisor.py"
    try:
        leader = subprocess.Popen(
            [sys.executable, "-I", "-B", str(entry), "guardian", str(run),
             str(parent_read), str(control_write), repr(deadline)],
            cwd=run / "work", env=run_environment(run / "work", model_required=(run / "policy.json").exists() and read_json(run, "policy.json")["execution"]["model_mode"] == "enabled"),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, pass_fds=(*lock_fds, parent_read, control_write),
            start_new_session=True,
        )
        os.close(parent_read)
        parent_read = -1
        os.close(control_write)
        control_write = -1
        info = read_json(run, "processes.json")
        info.update(guardian_pid=leader.pid, guardian_process_group=leader.pid)
        # Guardian also writes engine PID, so write the parent's record before
        # spawn would be preferable. Its complete identity is also carried by
        # the authenticated dedicated control pipe, not inferred from this file.
        atomic_json(run, "supervisor-process.json", info, exclusive=True)
        try:
            rows = _read_control_until_eof(control_read, deadline)
            if any(isinstance(row, dict) and row.get("kind") == "failed"
                   and row.get("reason") == "deadline" for row in rows):
                raise TimeoutError("Business deadline elapsed")
            if (len(rows) != 2 or any(type(row) is not dict for row in rows)
                    or set(rows[0]) != {"kind", "pid", "pgid"}
                    or rows[0]["kind"] != "engine_started"
                    or type(rows[0]["pid"]) is not int or rows[0]["pid"] <= 1
                    or type(rows[0]["pgid"]) is not int or rows[0]["pgid"] != leader.pid
                    or set(rows[1]) != {"kind", "exit_code"}
                    or rows[1]["kind"] != "engine_finished"
                    or type(rows[1]["exit_code"]) is not int or rows[1]["exit_code"] != 0):
                raise EvidenceInvalid("RUNNER_FAILED", "Business process did not complete cleanly")
            # EOF precedes Python interpreter exit. Wait for actual termination
            # while retaining the child's identity and the original deadline.
            _wait_for_owned_exit(leader.pid, deadline)
        finally:
            # Do not poll/wait before this. Even a dead leader retains its PID
            # until reap, so the known group identifier cannot be reused.
            _kill_group(leader.pid)
        code = leader.wait(timeout=3)
        if code != 0:
            raise EvidenceInvalid("RUNNER_FAILED", "Business process did not complete cleanly")
        return {"schema": "tiger.process-exit.v1", "run_id": run.name,
                "guardian_pid": leader.pid, "guardian_exit": code,
                "engine_pid": rows[0]["pid"], "engine_exit": 0,
                "group_cleanup_before_reap": True}
    finally:
        if leader is not None and leader.returncode is None:
            _kill_group(leader.pid)
            try:
                leader.wait(timeout=3)
            except subprocess.TimeoutExpired as exc:
                raise EvidenceInvalid("INTERRUPTED", "Guardian could not be reaped") from exc
        for fd in (parent_read, parent_write, control_read, control_write):
            if fd >= 0:
                os.close(fd)


def _check_lock_descriptor(fd: int, path: Path) -> None:
    import stat
    actual, expected = os.fstat(fd), path.lstat()
    if (not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1
            or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)):
        raise EvidenceInvalid("RESULT_INVALID", "Transferred lock identity differs")


def supervisor(state: Path, run_id: str, *, account_fd: int, run_fd: int,
               ack_fd: int) -> int:
    run = state / "runs" / run_id
    acknowledged = False
    bootstrap_guard = None
    business_guard = None
    try:
        bootstrap_guard = PreparationDeadline(BOOTSTRAP_SECONDS, stage="supervisor.bootstrap")
        bootstrap_guard.__enter__()
        _check_lock_descriptor(account_fd, state / "account.lock")
        _check_lock_descriptor(run_fd, run / "run.lock")
        admission, _policy, _source = validate_admission(run)
        remaining = (datetime.fromisoformat(admission["clock"]["deadline_at_utc"])
                     - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("Admission deadline elapsed")
        # validate_admission re-derived this upper bound from the frozen
        # runtime input; it never accepts a saved policy flag as authority.
        business_remaining = min(remaining, _policy.get("business_deadline_seconds", DEADLINE_SECONDS))
        deadline = time.monotonic() + business_remaining
        bootstrap_guard.__exit__(None, None, None)
        business_guard = PreparationDeadline(business_remaining, stage="supervisor.business", deadline=deadline)
        business_guard.__enter__()
        atomic_json(run, "processes.json", {"supervisor_pid": os.getpid(),
                                          "supervisor_process_group": os.getpgrp()},
                    exclusive=True)
        atomic_json(run, "accepted.json", {"schema": "tiger.accepted.v1",
                                          "run_id": run_id, "supervisor_pid": os.getpid(),
                                          "admission_sha256": digest(read_bytes(run, "admission.json"))},
                    exclusive=True)
        atomic_json(run, "status.json", {"schema": "tiger.status.v1", "run_id": run_id,
                                        "status": "running", "error": None})
        try:
            _send_control(ack_fd, {"accepted": True, "run_id": run_id})
        except BrokenPipeError:
            # Durable acceptance survives a lost client response.
            pass
        os.close(ack_fd)
        ack_fd = -1
        acknowledged = True

        def interrupted(_signum, _frame):
            raise SupervisorInterrupted("Supervisor was interrupted")
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        process_receipt = supervise_group(run, lock_fds=(account_fd, run_fd), deadline=deadline)
        atomic_json(run, "process-exit.json", process_receipt, exclusive=True)
        verified = seal_and_publish(state, run, deadline=deadline)
        atomic_json(run, "status.json", {"schema": "tiger.status.v1", "run_id": run_id,
                                        "status": verified["status"], "error": None})
        return 0
    except BaseException as exc:
        # If publication succeeded before a later status write failed, readers
        # prove commitment from the pointer/receipt chain, regardless of this.
        error = error_record(exc, run_id=run_id)
        engine_path = run / "engine-result.json"
        if not engine_path.exists():
            error["details_ref"] = None
        if engine_path.exists():
            try:
                engine = read_json(run, "engine-result.json")
                if engine.get("success") is False and isinstance(engine.get("error"), dict):
                    error = engine["error"]
            except (OSError, ValueError):
                # Keep the original safe error if the partial engine evidence
                # itself is unreadable; never promote it to success.
                error["details_ref"] = None
        try:
            atomic_json(run, "status.json", {
                "schema": "tiger.status.v1", "run_id": run_id,
                "status": "blocked" if error["code"] in {
                    "DATA_UNAVAILABLE", "DATA_STALE", "SOURCE_SYNTHETIC", "CURRENCY_BLOCKED"
                } else "interrupted" if isinstance(exc, (InterruptedError, SupervisorInterrupted)) else "failed",
                "error": error, "acceptance_was_durable": acknowledged,
            })
        except (OSError, ValueError):
            # No writable status is not evidence of success. Read-only queries
            # use the liveness lock and return interrupted after process death.
            sys.stderr.write("Unable to persist terminal status; inspect run evidence.\n")
        return 1
    finally:
        if business_guard is not None:
            business_guard.__exit__(None, None, None)
        if bootstrap_guard is not None:
            bootstrap_guard.__exit__(None, None, None)
        for fd in (account_fd, run_fd, ack_fd):
            if fd >= 0:
                os.close(fd)  # Never LOCK_UN an inherited open-file description.


def engine(run: Path) -> int:
    execution = None
    try:
        admission, policy, source = validate_admission(run)
        if dependency_identity() != source["dependencies"]:
            raise EvidenceInvalid("RESULT_INVALID", "Runtime dependency identity differs")
        work = checked_root(run / "work")
        os.chdir(work)
        cache = checked_root(work / "cache" / "yfinance", create=True)
        # Call the documented setter before any Ticker/provider/cache use.
        import yfinance as yf
        yf.set_tz_cache_location(str(cache))
        paths = RuntimePaths(
            work_dir=work, reports_dir=work / "reports",
            journal_path=work / "reports" / "journal.json",
            sim_path=work / "reports" / "sim_portfolio.json",
            calibration_path=work / "reports" / "calibration_samples.json",
            options_history_dir=work / "reports" / "options_hist",
            governance_path=work / "reports" / "governance_events.jsonl",
            proposals_dir=work / "reports" / "review_proposals",
            calendar_override_path=work / "events_calendar.json",
        )
        policy_args = dict(policy["execution"])
        policy_args["provider_names"] = tuple(policy_args["provider_names"])
        policy_args["search_source_names"] = tuple(policy_args["search_source_names"])
        execution = ExecutionContext(
            run_id=run.name, run_date=date.fromisoformat(admission["clock"]["run_date"]),
            admitted_at=datetime.fromisoformat(admission["clock"]["admitted_at_utc"]),
            paths=paths, policy=ExecutionPolicy(**policy_args),
            store=EvidenceStore(run, run.name),
            authoritative_paper=bool(policy.get("paper_ledger_contract")),
            account_version_before=(admission["parent"] or {}).get("version_sha256"),
            response_observation_contract=policy.get("model_contract", {}).get("response_observation_contract", ""),
        )
        main_file = run / "source" / "main.py"
        spec = importlib.util.spec_from_file_location("tiger_frozen_main", main_file)
        if spec is None or spec.loader is None:
            raise EvidenceInvalid("RESULT_INVALID", "Frozen daily entry missing")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        from trading_system import config
        args = argparse.Namespace(
            mode="daily", market="us", universe="extended", universe_file=None,
            top=config.SCAN_TOP_N, picks=config.MAX_PICKS_DEFAULT,
            account=100000.0, use_tuned=False, html=True, quiet=True,
            demo=False, out=str(paths.reports_dir),
        )
        logging.basicConfig(level=logging.WARNING,
                            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        result = module._daily(args, "yahoo", execution=execution)
        execution.assert_complete()
        if result is None:
            raise EvidenceInvalid("RUN_INCOMPLETE", "Daily entry returned no result")
        verify_source(run)
        origins = {}
        source_root = (run / "source").resolve()
        supervisor_origin = Path(__file__).resolve()
        if not supervisor_origin.is_relative_to(source_root):
            raise EvidenceInvalid("RESULT_INVALID", "Supervisor imported outside frozen source")
        origins["trading_system.agent_supervisor"] = supervisor_origin.relative_to(source_root).as_posix()
        for name, loaded in tuple(sys.modules.items()):
            if name == "tiger_frozen_main" or name == "trading_system" or name.startswith("trading_system."):
                origin = getattr(loaded, "__file__", None)
                if origin is None or not Path(origin).resolve().is_relative_to(source_root):
                    raise EvidenceInvalid("RESULT_INVALID", "Application imported outside frozen source")
                origins[name] = Path(origin).resolve().relative_to(source_root).as_posix()
        artifacts = []
        for file in sorted(paths.reports_dir.rglob("*")):
            if file.is_symlink():
                raise EvidenceInvalid("RESULT_INVALID", "Report symlink refused")
            if not file.is_file() or file.name.endswith(".lock"):
                continue
            relative = file.relative_to(run).as_posix()
            schemas = {".json": "application/json", ".jsonl": "application/x-ndjson",
                       ".md": "text/markdown", ".html": "text/html"}
            if file.suffix not in schemas:
                raise EvidenceInvalid("RESULT_INVALID", "Unexpected report artifact")
            if file.suffix == ".json":
                read_json(run, relative)
            artifacts.append(file_ref(run, relative, schemas[file.suffix]))
        atomic_json(run, "engine-result.json", {
            "schema": "tiger.engine-result.v1", "run_id": run.name, "success": True,
            "artifacts": artifacts, "module_origins": origins,
            "source_tree_sha256": source["tree_sha256"],
            "stage_count": len(execution.summary()), "error": None,
        }, exclusive=True)
        return 0
    except BaseException as exc:
        stage = None
        if execution is not None and execution.summary():
            stage = execution.summary()[-1]["stage"]
        error = error_record(exc, run_id=run.name, stage=stage)
        failure = {"schema": "tiger.engine-result.v1", "run_id": run.name,
                   "success": False, "error": error}
        if execution is not None and execution.response_observation_contract:
            try:
                from trading_system.llm.intelligence_proof import verify_partial_intelligence_model
                accounting = verify_partial_intelligence_model(run, admission,
                    scene_policy_path=run / "source" / "governance/bundles/trading/model-policy.yml")
                accounting["runtime_network_diagnostic"] = {
                    "fixed_provider_host": "api.deepseek.com",
                    "trusted_proxy_bypass_configured": "api.deepseek.com" in os.environ.get("NO_PROXY", "").split(","),
                    "actual_egress_route_verified": False,
                }
                atomic_json(run, "model-failure-accounting.json", accounting, exclusive=True)
                failure["model_failure_accounting"] = file_ref(run, "model-failure-accounting.json", "application/json")
            except Exception:
                # Preserve the primary business failure. A rejected or unwritable
                # accounting summary is explicit unknown evidence, never zero cost.
                failure["model_failure_accounting_verification"] = {
                    "status": "rejected", "code": "MODEL_FAILURE_ACCOUNTING_UNVERIFIED"}
                sys.stderr.write("Partial model accounting could not be independently verified or persisted.\n")
        try:
            atomic_json(run, "engine-result.json", failure, exclusive=True)
        except (OSError, ValueError):
            sys.stderr.write("Unable to persist engine failure evidence.\n")
        sys.stderr.write(error["message"] + "\n")
        return 1


def _main() -> int:
    args = sys.argv[1:]
    if not args:
        return 2
    role = args[0]
    if role == "engine" and len(args) == 2:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        code = engine(Path(args[1]))
        sys.stdout.flush()
        sys.stderr.flush()
        # Only this process-entry wrapper hard-exits. Core success already
        # requires settled search workers; failure must not wait forever for a
        # broken executor during Python interpreter shutdown.
        os._exit(code)
    if role == "supervisor" and len(args) == 6:
        return supervisor(Path(args[1]), args[2], account_fd=int(args[3]),
                          run_fd=int(args[4]), ack_fd=int(args[5]))
    if role == "guardian" and len(args) == 5:
        return guardian(Path(args[1]), parent_fd=int(args[2]), control_fd=int(args[3]),
                        deadline=float(args[4]))
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
