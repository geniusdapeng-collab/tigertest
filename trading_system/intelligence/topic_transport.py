"""Admitted topic transport deadlines and closed, value-free observations.

Requests' read timeout is an idle timeout. The one-shot POSIX alarm also bounds
headers and streaming keep-alives, while preserving an earlier worker alarm.
No response body, reasoning, exception text or credential enters this record.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import math
import signal
import threading
import time

from ..llm.json_observation import validate_transport
from ..redline.model_diagnostics import MODEL_FAILURE_CODES, RequiredModelUnavailable, model_failure_diagnostic
from .contracts import IntelligenceError, aware, digest, utc_now


CONTRACT = "tiger.topic-model-transport/v1"
_PHASES = {"awaiting_headers", "response_headers", "response_body", "http_envelope",
    "native_validation", "response_checkpoint", "response_validation"}
_ERRORS = {None, "connect_timeout", "read_timeout", "timeout", "transport_failure"}


class _AlarmElapsed(BaseException):
    """Bypass Requests' broad Exception handlers until our guard translates it."""


def deadline_supported():
    if (threading.current_thread() is not threading.main_thread()
            or not all(hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL", "setitimer", "getitimer"))):
        raise IntelligenceError("INVALID_STARTUP", "Topic transport needs the existing POSIX worker thread")
    delay, interval = signal.getitimer(signal.ITIMER_REAL)
    if interval or delay and not callable(signal.getsignal(signal.SIGALRM)):
        raise IntelligenceError("INVALID_STARTUP", "Unsupported outer watchdog")


class TransportObservation:
    def __init__(self):
        self.started = time.monotonic()
        self.data = {"phase": "awaiting_headers", "transport_error": None,
            "transport": {"http_status": None, "body_sha256": None, "body_bytes": 0,
                "body_whitespace_only": None, "body_complete": False}}
        self.deadline_owner = None
        self.response_seconds = None
        self.effective_seconds = None

    @contextmanager
    def deadline(self, job, response_seconds, stage):
        if type(response_seconds) not in {int, float} or not math.isfinite(response_seconds) or response_seconds <= 0:
            raise IntelligenceError("INVALID_STARTUP")
        self.response_seconds, self.effective_seconds = response_seconds, 0
        deadline_supported()
        analysis = stage == "sector.narrative"
        remaining = ((aware(job["deadline"]) - utc_now()).total_seconds() - job["policy"]["closeout_seconds"]
            - (job["policy"]["review_reserve_seconds"] if analysis else 0))
        if remaining <= 0:
            self.deadline_owner = "analysis_reserve" if analysis else "job_closeout"
            raise IntelligenceError("WALL_DEADLINE")
        old_handler = signal.getsignal(signal.SIGALRM)
        old_delay, old_interval = signal.getitimer(signal.ITIMER_REAL)
        started = time.monotonic()
        seconds, owner = min((response_seconds, "response"),
            (remaining, "analysis_reserve" if analysis else "job_closeout"))
        if old_delay and old_delay <= seconds:
            seconds, owner = old_delay, "parent_watchdog"
        expired, parent_error, parent_fired = False, None, False
        self.response_seconds, self.effective_seconds = response_seconds, seconds

        def alarm(number, frame):
            nonlocal expired, parent_error, parent_fired
            expired = True
            self.deadline_owner = owner
            if owner == "parent_watchdog":
                parent_fired = True
                try:
                    old_handler(number, frame)
                except BaseException as error:
                    parent_error = error
            raise _AlarmElapsed

        signal.signal(signal.SIGALRM, alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
            if expired:
                raise _AlarmElapsed
        except BaseException:
            # A close() failure may mask the private alarm inside Requests.
            # The recorded owner still restores the actual earlier reason.
            if expired:
                if parent_error is not None:
                    raise parent_error
                if owner in {"parent_watchdog", "job_closeout", "analysis_reserve"}:
                    raise IntelligenceError("WALL_DEADLINE") from None
                raise RequiredModelUnavailable("RESPONSE_DEADLINE") from None
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_delay and not parent_fired:
                signal.setitimer(signal.ITIMER_REAL,
                    max(0.000001, old_delay - (time.monotonic() - started)), old_interval)

    def failure(self, job, attempt_id, request_sha256, stage, error):
        diagnostic = model_failure_diagnostic(error)
        code = diagnostic["code"] if diagnostic else (
            "WALL_DEADLINE" if isinstance(error, IntelligenceError) and error.code == "WALL_DEADLINE"
            else "INTERRUPTED" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "INTERNAL_ERROR")
        value = {"schema": CONTRACT, "tenant": job["tenant"], "job_id": job["job_id"],
            "job_request_sha256": job["request_sha256"], "policy_sha256": job["policy_sha256"],
            "source_fingerprint_sha256": job["source_fingerprint"]["sha256"],
            "attempt_id": attempt_id, "request_sha256": request_sha256, "stage": stage,
            "phase": self.data["phase"], "code": code,
            "transport_error": self.data["transport_error"], "deadline_owner": self.deadline_owner,
            "elapsed_seconds": time.monotonic() - self.started,
            "response_deadline_seconds": self.response_seconds, "effective_deadline_seconds": self.effective_seconds,
            "transport": validate_transport(self.data["transport"]),
            "body_scope": "observed_prefix_not_provider_usage", "usage_state": "unknown"}
        value["sha256"] = digest(value)
        return value


def validate_failure(value, job, attempt):
    fields = {"schema", "tenant", "job_id", "job_request_sha256", "policy_sha256",
        "source_fingerprint_sha256", "attempt_id", "request_sha256", "stage", "phase", "code",
        "transport_error", "deadline_owner", "elapsed_seconds", "response_deadline_seconds",
        "effective_deadline_seconds", "transport", "body_scope", "usage_state", "sha256"}
    try:
        if (type(value) is not dict or set(value) != fields or value["schema"] != CONTRACT
                or job["policy"].get("model_transport_contract") != CONTRACT
                or value["tenant"] != job["tenant"] or value["job_id"] != job["job_id"]
                or value["job_request_sha256"] != job["request_sha256"]
                or value["policy_sha256"] != job["policy_sha256"]
                or value["source_fingerprint_sha256"] != job["source_fingerprint"]["sha256"]
                or value["attempt_id"] != attempt["attempt_id"] or value["request_sha256"] != attempt["request_sha"]
                or value["stage"] != attempt["stage"] or value["phase"] not in _PHASES
                or value["code"] not in MODEL_FAILURE_CODES | {"WALL_DEADLINE", "INTERRUPTED", "INTERNAL_ERROR"}
                or value["transport_error"] not in _ERRORS
                or value["deadline_owner"] not in {None, "response", "job_closeout", "analysis_reserve", "parent_watchdog"}
                or type(value["elapsed_seconds"]) not in {int, float}
                or not math.isfinite(value["elapsed_seconds"]) or value["elapsed_seconds"] < 0
                or any(type(value[k]) not in {int, float} or not math.isfinite(value[k]) or value[k] < 0
                    for k in ("response_deadline_seconds", "effective_deadline_seconds"))
                or value["response_deadline_seconds"] <= 0
                or value["effective_deadline_seconds"] == 0 and value["phase"] != "awaiting_headers"
                or value["effective_deadline_seconds"] > value["response_deadline_seconds"]
                or value["body_scope"] != "observed_prefix_not_provider_usage" or value["usage_state"] != "unknown"
                or value["sha256"] != digest({k: v for k, v in value.items() if k != "sha256"})):
            raise ValueError
        validate_transport(value["transport"])
        return deepcopy(value)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise IntelligenceError("HASH_MISMATCH", "Invalid closed topic failure observation") from None
