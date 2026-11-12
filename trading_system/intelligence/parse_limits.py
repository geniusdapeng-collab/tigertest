"""Trusted parser policy and a disposable, resource-bounded extraction process.

Only bytes already acquired by Tiger enter this process. No browser, fetcher,
cookie store, network request or provider credential is available to it.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
import hashlib
import errno
import json
import math
import os
from pathlib import Path
import re
import subprocess
import shutil
import threading
import sys
import tempfile
import time

from .contracts import FetchResult, IntelligenceError, canonical, digest, source_time


@dataclass(frozen=True)
class ParsePolicy:
    html_engine: str = "builtin"
    selector: str = "article, main"
    expected_terms: tuple[str, ...] = ()
    timeout_seconds: float = 60.0
    max_pages: int = 1000
    max_input_bytes: int = 32 * 1024 * 1024
    max_text_bytes: int = 16 * 1024 * 1024
    memory_bytes: int = 768 * 1024 * 1024
    manifest_version: str | None = None

    def __post_init__(self):
        if (self.manifest_version is not None and self.manifest_version != "v1"
                or self.html_engine not in {"builtin", "scrapling"}
                or not isinstance(self.selector, str) or not 1 <= len(self.selector) <= 512
                or not isinstance(self.expected_terms, tuple)
                or any(not isinstance(x, str) or not 1 <= len(x) <= 256 for x in self.expected_terms)
                or len(self.expected_terms) > 32
                or isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 300):
            raise IntelligenceError("CONTRACT_INVALID", "Invalid trusted parser policy")
        for name, ceiling in (("max_pages", 10000), ("max_input_bytes", 128 * 1024 * 1024),
                              ("max_text_bytes", 64 * 1024 * 1024), ("memory_bytes", 2 * 1024**3)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise IntelligenceError("CONTRACT_INVALID", "Invalid parser resource boundary")
        if self.memory_bytes < 128 * 1024 * 1024:
            raise IntelligenceError("CONTRACT_INVALID", "Parser memory boundary is too small")

    def public(self):
        value = {**asdict(self), "expected_terms": list(self.expected_terms)}
        if self.manifest_version is None:
            value.pop("manifest_version")
        return value

    @classmethod
    def from_value(cls, value=None):
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) - cls.__dataclass_fields__.keys():
            raise IntelligenceError("CONTRACT_INVALID", "Unknown parser policy field")
        values = dict(value)
        if "expected_terms" in values:
            if not isinstance(values["expected_terms"], (tuple, list)):
                raise IntelligenceError("CONTRACT_INVALID", "Parser terms must be a list")
            values["expected_terms"] = tuple(values["expected_terms"])
        return cls(**values)


class ParserResourcePool:
    """Atomic process-local reservations shared by one trusted coordinator.

    This does not claim cross-process admission. Multi-process coordinators
    must reserve through their persistent scheduler before using this pool.
    A rejected reservation has not spawned a worker or sent any external IO.
    """
    def __init__(self, *, max_workers, memory_bytes, temporary_bytes, wait_for_capacity=False):
        if any(type(value) is not int or value <= 0
               for value in (max_workers, memory_bytes, temporary_bytes)) or type(wait_for_capacity) is not bool:
            raise IntelligenceError("CONTRACT_INVALID", "Invalid parser pool capacity")
        self.max_workers, self.memory_bytes, self.temporary_bytes = max_workers, memory_bytes, temporary_bytes
        self._workers = self._memory = self._temporary = 0
        self._lock = threading.Lock()
        self._available = threading.Condition(self._lock)
        self.wait_for_capacity = wait_for_capacity

    def snapshot(self):
        with self._lock:
            return {"scope": "coordinator_process", "active_workers": self._workers,
                "reserved_memory_bytes": self._memory, "reserved_temporary_bytes": self._temporary,
                "max_workers": self.max_workers, "memory_capacity_bytes": self.memory_bytes,
                "temporary_capacity_bytes": self.temporary_bytes}

    @contextmanager
    def reserve(self, *, memory_bytes, temporary_bytes, timeout=None):
        if any(type(value) is not int or value < 0 for value in (memory_bytes, temporary_bytes)):
            raise IntelligenceError("CONTRACT_INVALID", "Invalid parser reservation")
        if timeout is not None and (type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0):
            raise IntelligenceError("CONTRACT_INVALID", "Invalid parser reservation deadline")
        if self.wait_for_capacity and timeout is None:
            raise IntelligenceError("CONTRACT_INVALID", "Queued parsing requires an original deadline")
        cutoff = None if timeout is None else time.monotonic() + timeout
        with self._available:
            if memory_bytes > self.memory_bytes or temporary_bytes > self.temporary_bytes:
                raise IntelligenceError("PARSER_CAPACITY_UNAVAILABLE", "Parser admission capacity exhausted")
            while (self._workers >= self.max_workers or self._memory + memory_bytes > self.memory_bytes
                    or self._temporary + temporary_bytes > self.temporary_bytes):
                if not self.wait_for_capacity:
                    raise IntelligenceError("PARSER_CAPACITY_UNAVAILABLE", "Parser admission capacity exhausted")
                remaining = cutoff - time.monotonic()
                if remaining <= 0:
                    raise IntelligenceError("PARSER_DEADLINE_EXCEEDED", "Original parser deadline expired in capacity queue")
                self._available.wait(timeout=remaining)
            self._workers += 1
            self._memory += memory_bytes
            self._temporary += temporary_bytes
        try:
            yield
        finally:
            with self._available:
                self._workers -= 1
                self._memory -= memory_bytes
                self._temporary -= temporary_bytes
                self._available.notify_all()


FAILURE_CODES = frozenset({"PARSER_INPUT_TOO_LARGE", "PARSER_DEADLINE_EXCEEDED", "PARSER_MEMORY_EXCEEDED",
    "PARSER_RESOURCE_EXCEEDED", "PARSER_RESOURCE_UNAVAILABLE", "PARSER_PAGE_LIMIT", "PARSER_TEXT_LIMIT",
    "PARSER_SELECTOR_UNVERIFIED", "PARSER_DEPENDENCY_UNAVAILABLE", "PARSER_FAILED", "PARSER_DISK_EXHAUSTED", "PARSER_STRUCTURE_LIMIT", "PARSER_CAPACITY_UNAVAILABLE"})
_WORKER_PHASES = frozenset({"started", "policy_ready", "limits_ready", "input_read", "builtin_complete",
    "scrapling_started", "scrapling_complete", "output_ready"})
_RUNTIME_SINK = ContextVar("tiger_parser_runtime", default=None)
_RESOURCE_POOL = ContextVar("tiger_parser_resources", default=None)


@contextmanager
def trusted_parser_resources(pool):
    """Bind one admitted coordinator pool on the current source worker only."""
    if not isinstance(pool, ParserResourcePool):
        raise IntelligenceError("CONTRACT_INVALID", "Trusted parser pool required")
    token = _RESOURCE_POOL.set(pool)
    try:
        yield
    finally:
        _RESOURCE_POOL.reset(token)


@contextmanager
def capture_parse_runtime():
    """Capture bounded execution measurements outside normalized evidence.

    A trusted operation owner may retain this separate trace in its receipt.
    Source parsers/replayers never use it to attest facts or completeness.
    """
    captured = {"schema": "tiger.parser-runtime-trace/v1", "records": [], "omitted_records": 0}
    token = _RUNTIME_SINK.set(captured)
    try:
        yield captured
    finally:
        _RUNTIME_SINK.reset(token)


def _measured(result, runtime):
    # FetchResult's dataclass fields are its normalization contract. A private
    # diagnostic attribute and optional operation-local trace stay out of
    # asdict(), document identities and deterministic source_io replay.
    result._parser_runtime = runtime
    captured = _RUNTIME_SINK.get()
    if captured is not None:
        if len(captured["records"]) < 16:
            captured["records"].append(runtime)
        else:
            captured["omitted_records"] += 1
    return result


def _worker_phase(data):
    if not isinstance(data, bytes):
        return None
    # Library diagnostics are intentionally discarded. Never archive a child
    # stderr body; a parser warning can contain untrusted source text.
    found = None
    for line in data[-4096:].splitlines():
        if line.startswith(b"tiger-parser-phase:"):
            name = line.removeprefix(b"tiger-parser-phase:").decode("ascii", errors="replace")
            if name in _WORKER_PHASES:
                found = name
    return found


def _partial(response, original_url, code, policy):
    media = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
    code = code if code in FAILURE_CODES else "PARSER_FAILED"
    receipt = {"schema": "tiger.parser-failure/v1", "code": code,
        "input_sha256": hashlib.sha256(response.body).hexdigest(), "input_bytes": len(response.body),
        "policy": ParsePolicy.from_value(policy).public(), "text_sha256": hashlib.sha256(b"").hexdigest(),
        "completeness": "partial", "evidence_scope": "resource_failure_no_extracted_content"}
    receipt["sha256"] = digest(receipt)
    result = FetchResult(original_url or response.url, response.url, "", "", response.body,
        media, "attachment", "partial", source_time(None), source_time(None),
        {"http_status": response.status, "http_headers": response.headers,
         "transport_sha256": response.transport_sha256, "request_count": response.request_count,
         "retrieved_at": response.received_at, "extractor": "tiger-main-text/v1",
         "extraction_error": code, "parse_failure": receipt}, usage={"state": "public_unmetered",
        "requests": response.request_count, "bytes": len(response.body), "requires_reconciliation": False})
    from .parse_manifest import attach_manifest
    return attach_manifest(result, ParsePolicy.from_value(policy))


def replay_partial(response, *, original_url=None, policy=None, receipt):
    """Verify a sealed failed operation without pretending it extracted text.

    Wall/RSS failures depend on the original execution, so repeating parsing on
    another host would reclassify an old partial observation as new evidence.
    This receipt can reproduce only empty, partial content, never a fact source.
    """
    if not isinstance(receipt, dict) or receipt.get("code") not in FAILURE_CODES:
        raise IntelligenceError("HASH_MISMATCH", "Invalid parser failure receipt")
    expected = _partial(response, original_url, receipt["code"], policy)
    original = receipt.get("redacted_original")
    if original is not None:
        # The original confidential wire cannot be retained. Its actual empty
        # parser failure remains bound to the observed transport hash, while
        # the outer receipt binds the sanitized bytes available for replay.
        fields = {"schema", "code", "input_sha256", "input_bytes", "policy", "text_sha256",
            "completeness", "evidence_scope", "sha256"}
        try:
            baseline = expected.metadata["parse_failure"]
            if (not isinstance(original, dict) or set(original) != fields
                    or digest({key: value for key, value in original.items() if key != "sha256"}) != original["sha256"]
                    or any(original[key] != baseline[key] for key in
                        ("schema", "code", "policy", "text_sha256", "completeness", "evidence_scope"))
                    or type(original["input_bytes"]) is not int or original["input_bytes"] < 0
                    or not isinstance(original["input_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", original["input_sha256"]) is None
                    or original["input_sha256"] != response.transport_sha256):
                raise ValueError
            baseline["redacted_original"] = original
            baseline["sha256"] = digest({key: value for key, value in baseline.items() if key != "sha256"})
        except (ValueError, TypeError, KeyError):
            raise IntelligenceError("HASH_MISMATCH", "Redacted parser failure differs from its original observation") from None
    if expected.metadata["parse_failure"] != receipt:
        raise IntelligenceError("HASH_MISMATCH", "Parser failure input or policy differs")
    return expected


def redact_partial(response, *, original, original_url=None, policy=None):
    """Bind sanitized bytes without rerunning or improving an observed failure."""
    receipt = original.metadata.get("parse_failure")
    if original.text != "" or original.text_kind != "attachment" or original.completeness != "partial":
        raise IntelligenceError("HASH_MISMATCH", "Only an empty observed parser failure can be rebound")
    # Validate against the original bytes while they are still in memory. The
    # saved receipt contains hashes and policy only, never those private bytes.
    replay_partial(replace(response, body=original.raw_bytes, parse_failure_receipt=None),
        original_url=original_url, policy=policy, receipt=receipt)
    if receipt.get("redacted_original") is not None:
        raise IntelligenceError("HASH_MISMATCH", "A parser failure cannot be redacted recursively")
    result = _partial(response, original_url, receipt["code"], policy)
    bound = result.metadata["parse_failure"]
    bound["redacted_original"] = receipt
    bound["sha256"] = digest({key: value for key, value in bound.items() if key != "sha256"})
    # Use the same independent replay verifier before publishing the receipt.
    return replay_partial(response, original_url=original_url, policy=policy, receipt=bound)


def _resident_bytes(pid, remaining):
    if remaining <= 0:
        raise IntelligenceError("PARSER_DEADLINE_EXCEEDED", "Parser deadline exhausted before memory observation")
    if sys.platform.startswith("linux"):
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except FileNotFoundError:
            return None
        except (OSError, ValueError, IndexError):
            raise IntelligenceError("PARSER_RESOURCE_UNAVAILABLE", "Cannot observe parser memory",
                details={"observer_reason": "PROC_STATUS_UNAVAILABLE"}) from None
    elif sys.platform == "darwin":
        try:
            observed = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "rss="],
                capture_output=True, timeout=min(0.25, max(0.001, remaining)),
                env={"PATH": os.defpath, "LC_ALL": "C"}, check=False)
            text = observed.stdout.strip()
            if observed.returncode != 0 and text:
                raise IntelligenceError("PARSER_RESOURCE_UNAVAILABLE", "Cannot observe parser memory",
                    details={"observer_reason": "PS_FAILED"})
            resident = int(text) * 1024 if text else None
            if resident is not None and resident < 0:
                raise ValueError
            return resident
        except subprocess.TimeoutExpired:
            raise IntelligenceError("PARSER_RESOURCE_UNAVAILABLE", "Cannot observe parser memory",
                details={"observer_reason": "PS_TIMEOUT"}) from None
        except (OSError, ValueError):
            raise IntelligenceError("PARSER_RESOURCE_UNAVAILABLE", "Cannot observe parser memory",
                details={"observer_reason": "PS_UNAVAILABLE_OR_INVALID"}) from None
    else:
        raise IntelligenceError("PARSER_RESOURCE_UNAVAILABLE", "Parser resource isolation requires POSIX")
    return None


def isolated_extract(response, *, original_url=None, trusted_filing=False, policy=None, deadline=None, resource_pool=None):
    resource_pool = resource_pool if resource_pool is not None else _RESOURCE_POOL.get()
    policy = ParsePolicy.from_value(policy)
    started = time.monotonic()
    cutoff = min(started + policy.timeout_seconds, deadline) if deadline is not None else started + policy.timeout_seconds
    child = None
    input_file = None
    reservation = None
    reserved = False
    estimate = None
    samples, peak_resident, observer_reason, worker_phase = 0, None, None, None
    observer = "linux_proc_status" if sys.platform.startswith("linux") else "darwin_ps" if sys.platform == "darwin" else "unsupported"
    def runtime():
        # These are execution diagnostics, outside the deterministic failure
        # receipt. They never attest content or change replay completeness.
        return {"schema": "tiger.parser-runtime/v1", "elapsed_seconds": max(0, time.monotonic() - started),
            "resident_samples": samples, "peak_resident_bytes": peak_resident,
            "memory_limit_bytes": policy.memory_bytes, "observer": observer,
            "observer_reason": observer_reason, "worker_phase": worker_phase,
            "worker_returncode": child.poll() if child is not None else None,
            "resource_estimate": estimate, "reservation_scope": "coordinator_process" if resource_pool is not None else "single_parser_only"}
    def partial(code):
        result = _partial(response, original_url, code, policy)
        return _measured(result, runtime())
    if len(response.body) > policy.max_input_bytes:
        return partial("PARSER_INPUT_TOO_LARGE")
    if time.monotonic() >= cutoff:
        return partial("PARSER_DEADLINE_EXCEEDED")
    header = {"url": response.url, "status": response.status, "headers": response.headers,
        "received_at": response.received_at, "transport_sha256": response.transport_sha256,
        "request_count": response.request_count, "original_url": original_url,
        "trusted_filing": bool(trusted_filing), "policy": policy.public(), "body_bytes": len(response.body)}
    payload = canonical(header) + b"\n" + response.body
    worker = Path(__file__).with_name("parse_worker.py")
    try:
        estimate = {"memory_bytes": policy.memory_bytes + len(payload) + policy.max_text_bytes * 8 + 4 * 1024 * 1024,
                    "temporary_bytes": len(payload)}
        if resource_pool is not None and not isinstance(resource_pool, ParserResourcePool):
            raise IntelligenceError("CONTRACT_INVALID", "Trusted parser resource pool required")
        reservation = (resource_pool.reserve(**estimate, timeout=max(0, cutoff - time.monotonic()))
                       if resource_pool is not None else nullcontext())
        reservation.__enter__()
        reserved = True
        if policy.manifest_version is not None and shutil.disk_usage(tempfile.gettempdir()).free < len(payload):
            return partial("PARSER_DISK_EXHAUSTED")
        # A regular private descriptor carries the already bounded input.
        # Repeated short communicate() observations must never abandon an
        # unfinished stdin pipe write when a cold worker starts after a slice.
        input_file = tempfile.TemporaryFile(mode="w+b")
        input_file.write(payload)
        input_file.seek(0)
        if time.monotonic() >= cutoff:
            return partial("PARSER_DEADLINE_EXCEEDED")
        child = subprocess.Popen([sys.executable, "-I", "-B", str(worker)],
            stdin=input_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": os.defpath, "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"},
            close_fds=True)
        while True:
            wait = cutoff - time.monotonic()
            if wait <= 0:
                return partial("PARSER_DEADLINE_EXCEEDED")
            try:
                output, diagnostics = child.communicate(timeout=min(0.1, wait))
                worker_phase = _worker_phase(diagnostics) or worker_phase
                break
            except subprocess.TimeoutExpired as exc:
                worker_phase = _worker_phase(exc.stderr) or worker_phase
                remaining = cutoff - time.monotonic()
                if remaining <= 0:
                    return partial("PARSER_DEADLINE_EXCEEDED")
                observer_error = None
                try:
                    resident = _resident_bytes(child.pid, remaining)
                except IntelligenceError as exc:
                    if exc.code != "PARSER_RESOURCE_UNAVAILABLE":
                        raise
                    resident, observer_error = None, exc
                if time.monotonic() >= cutoff:
                    return partial("PARSER_DEADLINE_EXCEEDED")
                if resident is None and observer_error is None:
                    observer_error = IntelligenceError("PARSER_RESOURCE_UNAVAILABLE", "Cannot observe live parser memory",
                        details={"observer_reason": "LIVE_PROCESS_NOT_OBSERVED"})
                if observer_error is not None:
                    # A disappearing RSS record can race with worker exit.
                    # Only actual completion of its pipes and process permits
                    # its result; a still-live unobservable child fails closed.
                    remaining = cutoff - time.monotonic()
                    if remaining <= 0:
                        return partial("PARSER_DEADLINE_EXCEEDED")
                    try:
                        output, diagnostics = child.communicate(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired as exc:
                        worker_phase = _worker_phase(exc.stderr) or worker_phase
                        raise observer_error
                    worker_phase = _worker_phase(diagnostics) or worker_phase
                    observer_reason = "EXIT_CONFIRMED_AFTER_" + observer_error.details.get("observer_reason", "UNAVAILABLE")
                    break
                if resident is not None:
                    samples += 1
                    peak_resident = max(peak_resident or 0, resident)
                if resident is not None and resident > policy.memory_bytes:
                    return partial("PARSER_MEMORY_EXCEEDED")
        if time.monotonic() >= cutoff:
            return partial("PARSER_DEADLINE_EXCEEDED")
        if child.returncode != 0 or len(output) > policy.max_text_bytes * 8 + 4 * 1024 * 1024:
            return partial("PARSER_RESOURCE_EXCEEDED")
        value = json.loads(output)
        if not isinstance(value, dict) or value.get("error"):
            code = value.get("error", "PARSER_FAILED") if isinstance(value, dict) else "PARSER_FAILED"
            if code not in {"PARSER_PAGE_LIMIT", "PARSER_TEXT_LIMIT", "PARSER_MEMORY_EXCEEDED",
                            "PARSER_RESOURCE_UNAVAILABLE", "PARSER_SELECTOR_UNVERIFIED", "PARSER_DEPENDENCY_UNAVAILABLE", "PARSER_STRUCTURE_LIMIT"}:
                code = "PARSER_FAILED"
            return partial(code)
        if not isinstance(value.get("text"), str) or len(value["text"].encode()) > policy.max_text_bytes:
            return partial("PARSER_TEXT_LIMIT")
        if hashlib.sha256(response.body).hexdigest() != header["transport_sha256"]:
            # A sanitized response can retain the original transport hash. The
            # input byte identity remains independently bound by the archive.
            value.setdefault("metadata", {})["transport_sha256"] = header["transport_sha256"]
        return _measured(FetchResult(**value, raw_bytes=response.body), runtime())
    except (OSError, ValueError, TypeError, IntelligenceError) as exc:
        if isinstance(exc, IntelligenceError):
            observer_reason = exc.details.get("observer_reason")
        code = ("PARSER_DEADLINE_EXCEEDED" if time.monotonic() >= cutoff else
                exc.code if isinstance(exc, IntelligenceError) else
                "PARSER_DISK_EXHAUSTED" if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT} else "PARSER_FAILED")
        return partial(code)
    finally:
        try:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate()
        finally:
            try:
                if input_file is not None:
                    input_file.close()
            finally:
                if reserved:
                    reservation.__exit__(None, None, None)
