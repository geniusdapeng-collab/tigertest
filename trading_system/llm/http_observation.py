"""Bounded, value-free observations of unsuccessful HTTP bodies.

The digest describes only bytes actually read. No free-form provider text,
headers, credentials, or invented billing usage cross this boundary. This is
an error summary, never a native response or permission to send again.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import re
import time

CONTRACT = "tiger.http-error-observation/v1"
MAX_ERROR_BYTES = 65536
_STOPS = frozenset({"eof", "content_length", "byte_limit", "deadline", "read_failure",
                    "encoding_denied", "invalid_length", "invalid_bytes", "incomplete_body"})
_CLASSES = frozenset({"invalid_request", "authentication", "balance", "permission",
                      "rate_limit", "transient_server", "http_failure"})


def classify_http_status(status):
    if status in {400, 422}:
        return "invalid_request"
    if status == 401:
        return "authentication"
    if status == 402:
        return "balance"
    if status == 403:
        return "permission"
    if status == 429:
        return "rate_limit"
    return "transient_server" if status in {408, 500, 502, 503, 504} else "http_failure"


def validate_http_error(value):
    fields = {"schema", "http_status", "body_bytes", "body_sha256", "body_complete",
              "body_whitespace_only", "body_scope", "stop_reason", "classification", "usage_state"}
    if (type(value) is not dict or set(value) != fields or value["schema"] != CONTRACT
            or type(value["http_status"]) is not int or not 100 <= value["http_status"] <= 599
            or value["http_status"] == 200 or type(value["body_bytes"]) is not int
            or not 0 <= value["body_bytes"] <= MAX_ERROR_BYTES
            or type(value["body_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["body_sha256"]) is None
            or type(value["body_complete"]) is not bool or type(value["body_whitespace_only"]) is not bool
            or value["body_scope"] != ("complete_http_body" if value["body_complete"] else "observed_prefix")
            or type(value["stop_reason"]) is not str or value["stop_reason"] not in _STOPS
            or value["body_complete"] != (value["stop_reason"] in {"eof", "content_length"})
            or value["classification"] != classify_http_status(value["http_status"])
            or value["usage_state"] != "unknown"):
        raise ValueError("Invalid bounded HTTP error observation")
    return deepcopy(value)


def observe_http_error(response, *, status, deadline, max_bytes=MAX_ERROR_BYTES):
    """Consume at most the caller's remaining allowance, with no hidden retry.

    The transport must also configure a bounded socket read timeout; a monotonic
    deadline cannot interrupt a read already in progress. We record read failure
    and retain the original HTTP failure instead of losing that evidence.
    """
    if (type(status) is not int or not 100 <= status <= 599 or status == 200
            or type(max_bytes) is not int or max_bytes < 0
            or type(deadline) not in {float, int} or not math.isfinite(deadline)):
        raise ValueError("Invalid HTTP error observation boundary")
    limit = min(max_bytes, MAX_ERROR_BYTES)
    size, whitespace, complete, reason = 0, True, False, "byte_limit"
    digest = hashlib.sha256()
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    declared = response.headers.get("Content-Length")
    if encoding not in {"", "identity"}:
        reason = "encoding_denied"
    elif declared is not None and (not isinstance(declared, str) or not declared.isdecimal()):
        reason = "invalid_length"
    else:
        length = int(declared) if declared is not None and not response.headers.get("Transfer-Encoding") else None
        while True:
            if time.monotonic() >= deadline:
                reason = "deadline"
                break
            if length is not None and size == length:
                complete, reason = True, "content_length"
                break
            if size >= limit:
                break
            room = min(16384, limit - size, length - size if length is not None else limit)
            try:
                chunk = response.raw.read1(room)
            except Exception:
                # Deliberately retain a closed reason, never arbitrary exception
                # data (which may contain URLs or authentication headers).
                reason = "read_failure"
                break
            if not isinstance(chunk, bytes) or len(chunk) > room:
                reason = "invalid_bytes"
                break
            if not chunk:
                complete = length is None or size == length
                reason = "eof" if complete else "incomplete_body"
                break
            size += len(chunk)
            digest.update(chunk)
            whitespace = whitespace and not bool(chunk.strip())
    return validate_http_error({"schema": CONTRACT, "http_status": status,
        "body_bytes": size, "body_sha256": digest.hexdigest(), "body_complete": complete,
        "body_whitespace_only": whitespace,
        "body_scope": "complete_http_body" if complete else "observed_prefix",
        "stop_reason": reason, "classification": classify_http_status(status), "usage_state": "unknown"})
