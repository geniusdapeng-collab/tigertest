"""Owned, bounded preparation before the detached business watchdog exists.

Signals interrupt cooperating POSIX main-thread code. They do not establish a
hard realtime guarantee for an uninterruptible kernel/device operation. Python
thread callers use the same request in a separately owned preparation process,
so their event loop does not silently lose the deadline guarantee.
"""
from __future__ import annotations

if not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from trading_system.agent_evidence import EvidenceInvalid, json_bytes, strict_json

PREPARATION_SECONDS = 45.0
BOOTSTRAP_SECONDS = 45.0
PROCESS_CLEANUP_SECONDS = 3.0
MAX_RESPONSE_BYTES = 65536


class PreparationExpired(BaseException):
    """A deadline cannot be swallowed as a source/library ValueError/OSError."""

    code = "TIMEOUT"

    def __init__(self, stage):
        self.stage = stage
        super().__init__("Controlled deadline elapsed; recover the original request identity.")


class PreparationDeadline:
    """Nestable real-time signal lease; an existing earlier timer wins."""

    def __init__(self, seconds, *, stage="admission.prepare", deadline=None):
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or seconds <= 0):
            raise EvidenceInvalid("RUNNER_FAILED", "Preparation deadline must be positive")
        self.seconds, self.stage, self.requested_deadline = float(seconds), stage, deadline
        self.active = False
        self.outer_fired = False

    def __enter__(self):
        if threading.current_thread() is not threading.main_thread():
            raise EvidenceInvalid("RUNNER_FAILED", "Signal preparation guard requires an owned main thread")
        if not hasattr(signal, "setitimer"):
            raise EvidenceInvalid("RUNNER_FAILED", "POSIX interval timers are unavailable")
        now = time.monotonic()
        self.started = now
        self.previous_handler = signal.getsignal(signal.SIGALRM)
        remaining, self.previous_interval = signal.getitimer(signal.ITIMER_REAL)
        self.previous_deadline = now + remaining if remaining > 0 else None
        self.deadline = min(now + self.seconds, self.requested_deadline) if self.requested_deadline is not None else now + self.seconds
        if self.previous_deadline is not None:
            self.deadline = min(self.deadline, self.previous_deadline)
        def expired(signum, frame):
            if self.previous_deadline is not None and self.previous_deadline <= self.deadline:
                self.outer_fired = True
                handler = self.previous_handler
                if callable(handler):
                    handler(signum, frame)
                elif handler == signal.SIG_DFL:
                    signal.signal(signal.SIGALRM, signal.SIG_DFL)
                    os.kill(os.getpid(), signal.SIGALRM)
            raise PreparationExpired(self.stage)
        signal.signal(signal.SIGALRM, expired)
        self.active = True
        signal.setitimer(signal.ITIMER_REAL, max(0.000001, self.deadline - time.monotonic()))
        return self

    def __exit__(self, *_exc):
        if not self.active:
            return False
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous_handler)
        self.active = False
        if self.previous_deadline is not None:
            now = time.monotonic()
            if not self.outer_fired:
                remaining = max(0.000001, self.previous_deadline - now)
            elif self.previous_interval > 0:
                elapsed_periods = max(0, math.floor((now - self.previous_deadline) / self.previous_interval)) + 1
                remaining = max(0.000001, self.previous_deadline + elapsed_periods * self.previous_interval - now)
            else:
                remaining = 0
            signal.setitimer(signal.ITIMER_REAL, remaining, self.previous_interval)
        return False


def thread_submission(state: Path, request: dict, source: Path, *, seconds=PREPARATION_SECONDS,
                      entry_clock: dict | None = None):
    """Execute trusted admission in an owned process without changing its key.

    The accepted supervisor starts a new session itself. Cleanup here targets
    only the short preparation process group, never that detached supervisor.
    Public request JSON cannot supply this source path or command.
    """
    from trading_system.agent_runner import failure
    process = None
    try:
        process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
            "submit", str(state), str(source), repr(float(seconds))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, start_new_session=True)
        # This wrapper exists only on the private preparation-process pipe.
        # The public API still accepts only its fixed four business fields.
        payload = {"request": request, "entry_clock": entry_clock} if entry_clock is not None else request
        output, _diagnostics = process.communicate(json_bytes(payload), timeout=seconds + PROCESS_CLEANUP_SECONDS)
        if len(output) > MAX_RESPONSE_BYTES:
            raise EvidenceInvalid("RESULT_INVALID", "Preparation response exceeded its contract")
        result = strict_json(output)
        if (not isinstance(result, dict) or result.get("schema_version") != "tiger.agent-result/v1"
                or result.get("operation") != "run" or type(result.get("operation_ok")) is not bool):
            raise EvidenceInvalid("RESULT_INVALID", "Preparation response identity differs")
        return result
    except subprocess.TimeoutExpired:
        return failure("run", "TIMEOUT", "Preparation process exceeded its observation deadline; recover with the same key.",
            data={"identity_recovery": "Repeat the identical request and idempotency key."})
    except (EvidenceInvalid, PreparationExpired) as exc:
        return failure("run", exc.code, str(exc), stage=exc.stage)
    except (OSError, ValueError, TypeError, RecursionError):
        return failure("run", "RUNNER_FAILED", "Controlled preparation process did not return valid evidence.")
    finally:
        if process is not None and process.returncode is None:
            # Retain direct child ownership until the original group is cleaned;
            # do not reap first and then target a possibly reused PID/group.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(0.25)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=PROCESS_CLEANUP_SECONDS)
            except subprocess.TimeoutExpired:
                # An uninterruptible OS task is not claimed as stopped.
                raise EvidenceInvalid("INTERRUPTED", "Preparation cleanup could not confirm process exit") from None
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _main():
    from trading_system import agent_runner as runner
    try:
        if len(sys.argv) != 5 or sys.argv[1] != "submit":
            return 2
        seconds = float(sys.argv[4])
        if not 0 < seconds <= PREPARATION_SECONDS:
            raise EvidenceInvalid("INVALID_REQUEST", "Invalid internal preparation duration")
        raw = sys.stdin.buffer.read(runner.MAX_INPUT_BYTES + 1)
        if len(raw) > runner.MAX_INPUT_BYTES:
            raise EvidenceInvalid("INPUT_TOO_LARGE", "Preparation request exceeded input limit")
        payload = strict_json(raw)
        private = isinstance(payload, dict) and set(payload) == {"request", "entry_clock"}
        request, clock = (payload["request"], payload["entry_clock"]) if private else (payload, None)
        result = runner.submit(Path(sys.argv[2]), request, source=Path(sys.argv[3]),
                               _preparation_seconds=seconds, _entry_clock=clock)
    except EvidenceInvalid as exc:
        result = runner.failure("run", exc.code, str(exc), stage=exc.stage)
    except (ValueError, OSError, TypeError):
        result = runner.failure("run", "RUNNER_FAILED", "Internal preparation arguments were invalid")
    sys.stdout.buffer.write(json_bytes(result))
    sys.stdout.buffer.flush()
    return 0 if result["operation_ok"] else runner.EXIT_CODES.get(result["error"]["code"], 7)


if __name__ == "__main__":
    raise SystemExit(_main())
