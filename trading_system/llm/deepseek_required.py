"""Fail-closed DeepSeek Flash transport with keyless, stage-bound evidence.

``deepseek-flash`` is the API ID currently documented as V4.1-Flash-1017;
it is not an immutable snapshot ID. Search is the provider's native Search
API, not the Responses ``web_search`` tool (which Flash ignores).
No raw response body, authorization header or unsafe source URL is persisted.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import html
import ipaddress
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

import requests

from ..redline import LLMUnavailable
from ..redline.model_diagnostics import RequiredModelUnavailable, validate_model_diagnostic
from .model_budget import (
    CONTEXT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MODEL_ID as MODEL,
    POLICY_VERSION,
    REGISTRY_VERSION,
    TOKENIZER_SHA256,
    TokenBudgetError,
    chat_request_core,
    plan_request,
    tokenizer_receipt,
)
from .scene_policy import (
    KERNEL_SCENE_MAP,
    ScenePolicyError,
    scene_config,
    validate_coverage,
)

BASE_URL = "https://api.deepseek.com"
RELEASE = "DeepSeek-V4.1-Flash-1017"
SEARCH_MODE = "provider_native_search"
THINKING_MODE = "scene-policy"
SEARCH_FETCH_MAX_BYTES = 1024 * 1024
CHAT_RESPONSE_MAX_BYTES = 64 * 1024 * 1024  # Transport memory guard, never a model token cap.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
RESPONSE_DEADLINE = 180.0
CHAT_READ_TIMEOUT = 180.0
CHAT_RESPONSE_DEADLINE = 480.0
_URL = re.compile(r"https?://[^\s<>\"'`\[\]{}]+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_CREDENTIAL = re.compile(
    r"(?i)\b(access[_-]?token|api[_-]?key|authorization|password|secret|session(?:id)?)"
    r"\s*[:=]\s*(?:Bearer\s+)?[^\s,;<>\"']+")
_SENSITIVE_QUERY = frozenset({
    "accesstoken", "token", "key", "apikey", "signature", "password", "passwd",
    "session", "sessionid", "secret", "authorization", "auth", "credential",
    "credentials", "refreshtoken", "idtoken", "authtoken", "oauthtoken",
    "sessiontoken", "clientsecret", "accesskey", "accesskeyid", "securitytoken", "jwt",
})
_SCHEMA_KEYS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "const", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minItems", "maxItems", "minLength", "maxLength", "pattern", "uniqueItems",
    "anyOf", "oneOf", "allOf", "title", "description", "$schema",
})
_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


def _fail(code: str, *, schema_path: str | None = None,
          schema_keyword: str | None = None) -> None:
    if re.fullmatch(r"HTTP_[1-5][0-9]{2}", code):
        raise RequiredModelUnavailable("HTTP_ERROR", http_status=int(code[5:])) from None
    raise RequiredModelUnavailable(code, schema_path=schema_path,
                                   schema_keyword=schema_keyword) from None


def retryable_search_failure(diagnostic: dict) -> bool:
    """DeepSeek's documented retryable HTTP responses for native Search."""
    diagnostic = validate_model_diagnostic(diagnostic)
    status = diagnostic.get("http_status")
    return status is not None and (status in (408, 429) or 500 <= status <= 599)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        _fail("INVALID_JSON_VALUE")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _finite_number(value: object) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and (not isinstance(value, float) or math.isfinite(value)))


def _json_equal(left: object, right: object) -> bool:
    if _finite_number(left) and _finite_number(right):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_equal(left[k], right[k]) for k in left)
    return left == right


def _check_schema(schema: object, depth: int = 0) -> None:
    """Validate the supported JSON Schema subset before accepting any instance."""
    if depth > 32 or not isinstance(schema, dict) or not schema or set(schema) - _SCHEMA_KEYS:
        _fail("UNSUPPORTED_OUTPUT_SCHEMA")
    _canonical(schema)
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if (not kinds or any(not isinstance(x, str) or x not in _TYPES for x in kinds)
                or len(set(kinds)) != len(kinds)):
            _fail("INVALID_SCHEMA_TYPE")
    for annotation in ("title", "description", "$schema"):
        if annotation in schema and not isinstance(schema[annotation], str):
            _fail("INVALID_SCHEMA_ANNOTATION")
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict) or any(not isinstance(k, str) for k in props):
            _fail("INVALID_SCHEMA_PROPERTIES")
        for child in props.values():
            _check_schema(child, depth + 1)
    if "required" in schema:
        required = schema["required"]
        if (not isinstance(required, list) or any(not isinstance(x, str) for x in required)
                or len(set(required)) != len(required)):
            _fail("INVALID_SCHEMA_REQUIRED")
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if not isinstance(extra, bool):
            _check_schema(extra, depth + 1)
    if "items" in schema:
        _check_schema(schema["items"], depth + 1)
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and not _finite_number(schema[key]):
            _fail("INVALID_SCHEMA_BOUND")
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            _fail("INVALID_SCHEMA_LENGTH")
    if "pattern" in schema:
        try:
            if not isinstance(schema["pattern"], str) or len(schema["pattern"]) > 1000:
                _fail("INVALID_SCHEMA_PATTERN")
            re.compile(schema["pattern"])
        except re.error:
            _fail("INVALID_SCHEMA_PATTERN")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        _fail("INVALID_SCHEMA_UNIQUENESS")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        _fail("INVALID_SCHEMA_ENUM")
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                _fail("INVALID_SCHEMA_COMBINATOR")
            for child in schema[key]:
                _check_schema(child, depth + 1)


def _matches_type(value: object, kind: str) -> bool:
    return {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "number": lambda: _finite_number(value),
        "integer": lambda: _finite_number(value) and int(value) == value,
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }[kind]()


def _matches(value: object, schema: dict, depth: int = 0) -> bool:
    if depth > 64:
        return False
    kind = schema.get("type")
    if kind is not None and not any(_matches_type(value, k) for k in
                                    (kind if isinstance(kind, list) else [kind])):
        return False
    if "const" in schema and not _json_equal(value, schema["const"]):
        return False
    if "enum" in schema and not any(_json_equal(value, v) for v in schema["enum"]):
        return False
    for key, accept in (("allOf", all), ("anyOf", any)):
        if key in schema and not accept(_matches(value, s, depth + 1) for s in schema[key]):
            return False
    if "oneOf" in schema and sum(_matches(value, s, depth + 1) for s in schema["oneOf"]) != 1:
        return False
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            return False
        props = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                if not _matches(item, props[key], depth + 1):
                    return False
            elif extra is False or (isinstance(extra, dict) and not _matches(item, extra, depth + 1)):
                return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return False
        if schema.get("uniqueItems") and any(_json_equal(v, x) for i, v in enumerate(value)
                                             for x in value[i + 1:]):
            return False
        if "items" in schema and any(not _matches(v, schema["items"], depth + 1) for v in value):
            return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
    if _finite_number(value):
        for key, invalid in (("minimum", lambda bound: value < bound),
                             ("maximum", lambda bound: value > bound),
                             ("exclusiveMinimum", lambda bound: value <= bound),
                             ("exclusiveMaximum", lambda bound: value >= bound)):
            if key in schema and invalid(schema[key]):
                return False
    return True


def _schema_child_path(path: str, segment: str | int) -> str | None:
    # Property names come only from the trusted schema. Never expose an extra
    # model-generated key, and mask schema names outside the fixed safe grammar.
    if type(segment) is int:
        component = str(segment)
    else:
        component = segment if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", segment) else "*"
    child = path + "/" + component
    return child if len(child) <= 512 else None


def _schema_mismatch(value: object, schema: dict, depth: int = 0,
                     path: str = "") -> tuple[str, str]:
    """Locate a rejected instance without returning its keys, values or text.

    This runs only after the unchanged acceptance validator rejects the value.
    Combinator and extra-property failures stay at the parent instance path.
    """
    if depth > 64:
        return path, "depth"
    kind = schema.get("type")
    if kind is not None and not any(_matches_type(value, k) for k in
                                    (kind if isinstance(kind, list) else [kind])):
        return path, "type"
    if "const" in schema and not _json_equal(value, schema["const"]):
        return path, "const"
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        return path, "enum"
    for key, accept in (("allOf", all), ("anyOf", any)):
        if key in schema and not accept(_matches(value, child, depth + 1) for child in schema[key]):
            return path, key
    if "oneOf" in schema and sum(_matches(value, child, depth + 1) for child in schema["oneOf"]) != 1:
        return path, "oneOf"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            return path, "type"
        props = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return path, "required"
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                if not _matches(item, props[key], depth + 1):
                    child = _schema_child_path(path, key)
                    return ((path, "depth") if child is None else
                            _schema_mismatch(item, props[key], depth + 1, child))
            elif extra is False or (isinstance(extra, dict) and not _matches(item, extra, depth + 1)):
                return path, "additionalProperties"
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            return path, "minItems"
        if len(value) > schema.get("maxItems", len(value)):
            return path, "maxItems"
        if schema.get("uniqueItems") and any(_json_equal(item, other)
                for index, item in enumerate(value) for other in value[index + 1:]):
            return path, "uniqueItems"
        if "items" in schema:
            for index, item in enumerate(value):
                if not _matches(item, schema["items"], depth + 1):
                    child = _schema_child_path(path, index)
                    return ((path, "depth") if child is None else
                            _schema_mismatch(item, schema["items"], depth + 1, child))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return path, "minLength"
        if len(value) > schema.get("maxLength", len(value)):
            return path, "maxLength"
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return path, "pattern"
    if _finite_number(value):
        for key, invalid in (("minimum", lambda bound: value < bound),
                             ("maximum", lambda bound: value > bound),
                             ("exclusiveMinimum", lambda bound: value <= bound),
                             ("exclusiveMaximum", lambda bound: value >= bound)):
            if key in schema and invalid(schema[key]):
                return path, key
    # A conservative fixed fallback cannot expose an untrusted value even if a
    # future acceptance rule has not yet received a more specific diagnostic.
    return path, "type"


def validate_schema(value: object, schema: dict) -> None:
    """Validate the complete supported schema; unknown keywords never pass."""
    _check_schema(schema)
    _canonical(value)  # Reject non-JSON values, nonfinite numbers and deep cycles.
    if not _matches(value, schema):
        path, keyword = _schema_mismatch(value, schema)
        _fail("OUTPUT_SCHEMA_MISMATCH", schema_path=path, schema_keyword=keyword)


def _decoded_url_component(value: str) -> str | None:
    """Resolve nested percent escapes completely or reject ambiguous input."""
    for _ in range(8):
        decoded = unquote(value)
        if decoded == value:
            return value
        value = decoded
    return value if unquote(value) == value else None


def sanitize_url(value: object, *, secret: str | None = None, _depth: int = 0) -> str | None:
    """Return a credential-free public HTTP(S) URL, otherwise reject it entirely."""
    if _depth > 4 or not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if secret and secret in value:
        return None
    value = html.unescape(value)
    decoded_url = _decoded_url_component(value)
    if decoded_url is None or secret and secret in decoded_url or _JWT.search(decoded_url):
        return None
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value or _JWT.search(value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 80, 443)):
            return None
        host = parts.hostname.lower().rstrip(".")
        if (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
                or "." not in host and ":" not in host):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if re.fullmatch(r"[0-9.]+", host) or host.startswith("0x"):
                return None
            encoded_host = host.encode("idna").decode("ascii")
            if len(encoded_host) > 253 or any(not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in encoded_host.split(".")):
                return None
        else:
            if not address.is_global:
                return None
        for key, item in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100):
            key, item = _decoded_url_component(key), _decoded_url_component(item)
            if key is None or item is None:
                return None
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if (normalized in _SENSITIVE_QUERY or normalized.startswith(("xamz", "xgoog"))
                    or _CREDENTIAL.search(item) or _JWT.search(item) or secret and secret in item):
                return None
            if any(sanitize_url(match.group(0), secret=secret, _depth=_depth + 1) is None
                   for match in _URL.finditer(item)):
                return None
        return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))
    except (ValueError, UnicodeError):
        return None


def _reject_constant(_value: str):
    _fail("NONFINITE_RESPONSE_JSON")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_RESPONSE_JSON_KEY")
        result[key] = value
    return result


def _json_loads(value: bytes | str) -> object:
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "strict")
        return json.loads(value, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        _fail("INVALID_RESPONSE_JSON")


def sanitize_model_text(value: str, secret: str | None = None) -> str:
    def replace_url(match):
        raw = match.group(0)
        clean = raw.rstrip(".,;:!?)")
        suffix = raw[len(clean):]
        return (sanitize_url(clean, secret=secret) or "[REDACTED_URL]") + suffix

    value = _URL.sub(replace_url, value)
    if secret:
        value = value.replace(secret, "[REDACTED_SECRET]")
    value = _JWT.sub("[REDACTED_TOKEN]", value)
    return _CREDENTIAL.sub(lambda m: m.group(1) + "=[REDACTED_SECRET]", value)


def sanitize_model_value(value: object, secret: str | None = None, depth: int = 0) -> object:
    if depth > 64:
        _fail("RESPONSE_DEPTH_LIMIT")
    if isinstance(value, str):
        return sanitize_model_text(value, secret)
    if isinstance(value, list):
        return [sanitize_model_value(v, secret, depth + 1) for v in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("INVALID_JSON_OBJECT_KEY")
            key = sanitize_model_text(key, secret)
            if key in out:
                _fail("INVALID_JSON_OBJECT_KEY")
            out[key] = sanitize_model_value(item, secret, depth + 1)
        return out
    return value


class DeepSeekRequiredClient:
    required = True

    def __init__(self, execution):
        if execution is None or not callable(getattr(execution, "call_io", None)):
            _fail("EXECUTION_REQUIRED")
        if (os.environ.get("LLM_BACKEND") != "api"
                or os.environ.get("LLM_BASE_URL") != BASE_URL
                or os.environ.get("LLM_MODEL") != MODEL):
            _fail("TRUSTED_CONFIGURATION_MISMATCH")
        key = os.environ.get("LLM_API_KEY", "")
        if (not key or len(key) > 4096 or key.strip() != key
                or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            _fail("API_KEY_REQUIRED")
        self.execution = execution
        self._api_key = key
        try:
            if validate_coverage():
                _fail("MODEL_SCENE_POLICY_INCOMPLETE")
            self._scene_policies = {
                stage: scene_config(scene) for stage, scene in KERNEL_SCENE_MAP.items()
            }
            self._tokenizer = tokenizer_receipt()
        except (ScenePolicyError, TokenBudgetError):
            _fail("MODEL_SCENE_POLICY_INVALID")
        self.receipts: list[dict] = []
        self.search_receipts: list[dict] = []
        self.fetch_receipts: list[dict] = []
        self._remote_requests = 0
        self._chat_schema_retry_used = False
        self._observed_response_ids: set[str] = set()
        self._native_io_limits = None
        self._response_observation_contract = getattr(execution, "response_observation_contract", "")
        if self._response_observation_contract:
            from .json_observation import CONTRACT
            if (self._response_observation_contract != CONTRACT
                    or not callable(getattr(execution, "model_attempt_snapshot", None))
                    or not callable(getattr(execution, "claim_model_format_retry", None))):
                _fail("TRUSTED_CONFIGURATION_MISMATCH")
        self._last_chat_transport = None

    @contextmanager
    def native_io_budget(self, *, max_bytes: int, deadline: float):
        """Trusted acquisition limits for Search/Fetch only, shared across retry.

        Chat planning and its legal model window are deliberately independent.
        The mutable meter survives failures so the acquisition ledger can record
        bytes/requests that were actually observed instead of reporting zero.
        """
        if (type(max_bytes) is not int or max_bytes <= 0
                or type(deadline) not in {int, float} or not math.isfinite(deadline)
                or self._native_io_limits is not None):
            raise ValueError("Invalid or nested native acquisition budget")
        meter = {"bytes_received": 0, "request_count": 0, "limit_reason": None,
                 "transport_observed": False}
        self._native_io_limits = {"max_bytes": max_bytes, "deadline": deadline, "meter": meter}
        try:
            yield meter
        finally:
            self._native_io_limits = None

    def describe(self) -> dict:
        return {"mode": "required", "enabled": True, "provider": "deepseek",
                "model": MODEL, "version": RELEASE, "release": RELEASE,
                "version_binding": "official-current-alias-mapping",
                "thinking_mode": THINKING_MODE,
                "scene_policy": {
                    stage: {
                        "scene": KERNEL_SCENE_MAP[stage],
                        "thinking": cfg["thinking"],
                    }
                    for stage, cfg in self._scene_policies.items()
                },
                "budget": self._tokenizer,
                "search": SEARCH_MODE, "remote_requests": self._remote_requests,
                "chat_calls": len(self.receipts), "search_calls": len(self.search_receipts),
                "fetch_calls": len(self.fetch_receipts)}

    def _stage(self) -> str:
        scope = getattr(self.execution, "_active", None)
        name = getattr(getattr(scope, "spec", None), "name", None)
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", name)
                or self._api_key in name):
            _fail("ACTIVE_STAGE_REQUIRED")
        return name

    def _text(self, value: str) -> str:
        return sanitize_model_text(value, self._api_key)

    def _clean(self, value: object, depth: int = 0) -> object:
        return sanitize_model_value(value, self._api_key, depth)

    def _post_json(self, path: str, request: dict, *, _transport_observation=None,
                   _chat_read_timeout=None) -> tuple[dict, str]:
        if path not in ("/chat/completions", "/v1/tools/search", "/v1/tools/fetch"):
            _fail("ENDPOINT_NOT_ALLOWED")
        chat = path == "/chat/completions"
        if (_transport_observation is not None and (not chat or type(_transport_observation) is not dict)
                or _chat_read_timeout is not None and (not chat or type(_chat_read_timeout) not in {int, float}
                    or not math.isfinite(_chat_read_timeout) or not 0 < _chat_read_timeout <= CHAT_RESPONSE_DEADLINE)):
            _fail("INVALID_CHAT_REQUEST")
        observed_chat = chat and (bool(self._response_observation_contract) or _transport_observation is not None)
        observed_digest = hashlib.sha256()
        if observed_chat:
            self._last_chat_transport = {"http_status": None, "body_sha256": None,
                "body_bytes": 0, "body_whitespace_only": True, "body_complete": False}
        if _transport_observation is not None:
            _transport_observation.update(phase="awaiting_headers", transport=self._last_chat_transport)
        wire = _canonical(request)
        # Chat admission is governed by the pinned tokenizer and model window.
        # The Search/Fetch byte envelope is a separate endpoint constraint.
        if not chat and len(wire) > SEARCH_FETCH_MAX_BYTES:
            _fail("REQUEST_TOO_LARGE")
        response_limit = CHAT_RESPONSE_MAX_BYTES if chat else SEARCH_FETCH_MAX_BYTES
        response = None
        deadline = time.monotonic() + (CHAT_RESPONSE_DEADLINE if chat else RESPONSE_DEADLINE)
        tool_limits = self._native_io_limits if not chat else None
        if tool_limits is not None:
            meter = tool_limits["meter"]
            meter["transport_observed"] = True
            remaining_bytes = tool_limits["max_bytes"] - meter["bytes_received"]
            if remaining_bytes <= 0:
                meter["limit_reason"] = "bytes"
                _fail("RESPONSE_TOO_LARGE")
            response_limit = min(response_limit, remaining_bytes)
            deadline = min(deadline, tool_limits["deadline"])
            if time.monotonic() >= deadline:
                meter["limit_reason"] = "deadline"
                _fail("RESPONSE_DEADLINE")
        self._remote_requests += 1
        try:
            remaining = max(0.001, deadline - time.monotonic()) if tool_limits is not None else None
            if tool_limits is not None:
                meter["request_count"] += 1
            response = requests.post(
                BASE_URL + path, data=wire,
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         "Accept-Encoding": "identity", "Authorization": "Bearer " + self._api_key},
                timeout=((min(CONNECT_TIMEOUT, remaining / 2), min(READ_TIMEOUT, remaining / 2))
                         if tool_limits is not None else
                         (CONNECT_TIMEOUT, (_chat_read_timeout if _chat_read_timeout is not None else CHAT_READ_TIMEOUT)
                            if chat else READ_TIMEOUT)),
                allow_redirects=False, verify=True, stream=True)
            status = response.status_code
            if _transport_observation is not None:
                _transport_observation["phase"] = "response_headers"
            if observed_chat and type(status) is int and 100 <= status <= 599:
                self._last_chat_transport["http_status"] = status
            if type(status) is not int or status != 200:
                _fail(f"HTTP_{status}" if type(status) is int and 100 <= status <= 599 else "HTTP_INVALID")
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            if encoding not in ("", "identity"):
                _fail("UNEXPECTED_CONTENT_ENCODING")
            if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                _fail("UNEXPECTED_CONTENT_TYPE")
            declared = response.headers.get("Content-Length")
            if declared is not None and (not declared.isdecimal() or int(declared) > response_limit):
                if tool_limits is not None and declared.isdecimal() and int(declared) > remaining_bytes:
                    meter["limit_reason"] = "bytes"
                _fail("RESPONSE_TOO_LARGE")
            chunks: list[bytes] = []
            size = 0
            if _transport_observation is not None:
                _transport_observation["phase"] = "response_body"
            while True:
                if time.monotonic() > deadline:
                    if tool_limits is not None:
                        meter["limit_reason"] = "deadline"
                    _fail("RESPONSE_DEADLINE")
                if tool_limits is not None:
                    room = min(response_limit - size, tool_limits["max_bytes"] - meter["bytes_received"])
                    if room <= 0:
                        if meter["bytes_received"] >= tool_limits["max_bytes"]:
                            meter["limit_reason"] = "bytes"
                        _fail("RESPONSE_TOO_LARGE")
                    chunk = response.raw.read1(min(16384, room))
                    if isinstance(chunk, bytes):
                        meter["bytes_received"] += len(chunk)
                else:
                    chunk = response.raw.read1(16384)
                if time.monotonic() > deadline:
                    if tool_limits is not None:
                        meter["limit_reason"] = "deadline"
                    _fail("RESPONSE_DEADLINE")
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    _fail("INVALID_RESPONSE_BYTES")
                size += len(chunk)
                if observed_chat:
                    observed_digest.update(chunk)
                    self._last_chat_transport.update(body_bytes=size,
                        body_sha256=observed_digest.hexdigest(),
                        body_whitespace_only=self._last_chat_transport["body_whitespace_only"] and not bool(chunk.strip()))
                if size > response_limit:
                    _fail("RESPONSE_TOO_LARGE")
                chunks.append(chunk)
                if tool_limits is not None and declared is not None and size == int(declared):
                    break
            raw = b"".join(chunks)
            if observed_chat:
                self._last_chat_transport.update(body_complete=True, body_bytes=len(raw),
                    body_sha256=hashlib.sha256(raw).hexdigest(), body_whitespace_only=not bool(raw.strip()))
            if _transport_observation is not None:
                _transport_observation["phase"] = "http_envelope"
            obj = _json_loads(raw)
            if not isinstance(obj, dict):
                _fail("RESPONSE_NOT_OBJECT")
            return obj, hashlib.sha256(raw).hexdigest()
        except LLMUnavailable:
            raise
        except requests.Timeout as error:
            if _transport_observation is not None:
                _transport_observation["transport_error"] = (
                    "connect_timeout" if isinstance(error, requests.ConnectTimeout) else
                    "read_timeout" if isinstance(error, requests.ReadTimeout) else "timeout")
            _fail("NETWORK_TIMEOUT")
        except Exception:
            if _transport_observation is not None:
                _transport_observation["transport_error"] = "transport_failure"
            _fail("TRANSPORT_FAILURE")
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    _fail("RESPONSE_CLOSE_FAILURE")

    def _native_transport_usage(self):
        limits = self._native_io_limits
        if limits is None or not limits["meter"]["transport_observed"]:
            return {}
        meter = limits["meter"]
        return {"transport_usage": {"bytes_received": meter["bytes_received"],
                                    "request_count": meter["request_count"]}}

    @staticmethod
    def _identifier(value: object) -> str:
        if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value)
                or _JWT.search(value)):
            _fail("INVALID_RESPONSE_IDENTITY")
        return value

    @staticmethod
    def _usage(value: object, thinking_mode: str = "disabled") -> dict:
        if not isinstance(value, dict):
            _fail("USAGE_REQUIRED")
        result = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            n = value.get(key)
            if type(n) is not int or n <= 0:
                _fail("INVALID_USAGE")
            result[key] = n
        if result["prompt_tokens"] + result["completion_tokens"] != result["total_tokens"]:
            _fail("INCONSISTENT_USAGE")
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            if key in value:
                if type(value[key]) is not int or not 0 <= value[key] <= result["prompt_tokens"]:
                    _fail("INVALID_USAGE")
                result[key] = value[key]
        for parent, child, limit in (("prompt_tokens_details", "cached_tokens", "prompt_tokens"),
                                     ("completion_tokens_details", "reasoning_tokens", "completion_tokens")):
            details = value.get(parent)
            if details is not None:
                if not isinstance(details, dict):
                    _fail("INVALID_USAGE")
                if child in details:
                    n = details[child]
                    if type(n) is not int or not 0 <= n <= result[limit]:
                        _fail("INVALID_USAGE")
                    if (parent == "completion_tokens_details" and child == "reasoning_tokens"
                            and thinking_mode == "disabled" and n != 0):
                        _fail("NON_THINKING_OUTPUT_REQUIRED")
                    result[parent] = {child: n}
        return result

    def plan_chat(self, *, system: str, user: str, schema_hint: dict, schema: dict,
                  temperature: float = 0.2, public_payload: dict | None = None) -> tuple[dict, int, int]:
        """Plan the exact sanitized wire without transport or acceptance-budget side effects."""
        stage = self._stage()
        if stage not in self._scene_policies:
            _fail("UNSUPPORTED_MODEL_STAGE")
        _check_schema(schema)
        if _canonical(self._clean(schema)) != _canonical(schema):
            _fail("UNSAFE_OUTPUT_SCHEMA")
        if (not isinstance(system, str) or not system.strip() or not isinstance(user, str) or not user.strip()
                or not isinstance(schema_hint, dict)
                or (public_payload is not None and not isinstance(public_payload, dict))
                or not _finite_number(temperature)
                or not 0 <= temperature <= 2):
            _fail("INVALID_CHAT_REQUEST")
        thinking = self._scene_policies[stage]["thinking"]
        user_text = self._text(user)
        if public_payload is not None:
            # Redact data values before JSON escaping: escaped delimiters must
            # never become part of a URL/credential match in the whole prompt.
            user_text += "\n" + _canonical(self._clean(public_payload)).decode("utf-8")
        return plan_request(chat_request_core(system=self._text(system), user=user_text,
            schema=schema, schema_hint=self._clean(schema_hint), thinking_mode=thinking["type"],
            effort=thinking.get("effort"), temperature=temperature))

    def complete_json(self, *, system: str, user: str, schema_hint: dict,
                      max_tokens: int | None = None, temperature: float = 0.2, schema: dict,
                      public_payload: dict | None = None) -> dict:
        if self._response_observation_contract:
            return self._observed_complete_json(system=system, user=user, schema_hint=schema_hint,
                max_tokens=max_tokens, temperature=temperature, schema=schema, public_payload=public_payload)
        if max_tokens is not None:
            _fail("CALLER_OUTPUT_CAP_FORBIDDEN")
        stage = self._stage()
        try:
            request, input_token_estimate, output_ceiling = self.plan_chat(system=system, user=user,
                schema_hint=schema_hint, schema=schema, temperature=temperature, public_payload=public_payload)
        except TokenBudgetError as error:
            _fail(error.code)
        thinking_mode = request["thinking"]["type"]
        effort = request.get("reasoning_effort")
        call_id = uuid.uuid4().hex
        inputs = {"schema": "tiger.deepseek-request.v1", "call_id": call_id, "stage": stage,
                  "provider": "deepseek", "method": "POST", "endpoint": BASE_URL + "/chat/completions",
                  "request": request, "request_sha256": _hash(request), "output_schema": schema,
                  "schema_sha256": _hash(schema), "release": RELEASE,
                  "scene": KERNEL_SCENE_MAP[stage], "thinking_mode": thinking_mode,
                  "reasoning_effort": effort, "input_token_estimate": input_token_estimate,
                  "output_ceiling": output_ceiling, "policy_version": POLICY_VERSION,
                  "registry_version": REGISTRY_VERSION, "tokenizer_sha256": TOKENIZER_SHA256}

        observed_attempt = None

        def invoke_once():
            nonlocal observed_attempt
            observed_attempt = None
            response, body_hash = self._post_json("/chat/completions", request)
            if response.get("model") != MODEL:
                _fail("RESPONSE_MODEL_MISMATCH")
            if response.get("object") != "chat.completion":
                _fail("RESPONSE_OBJECT_MISMATCH")
            response_id = self._identifier(response.get("id"))
            if self._api_key in response_id or response_id in self._observed_response_ids:
                _fail("RESPONSE_IDENTITY_REUSED_OR_UNSAFE")
            self._observed_response_ids.add(response_id)
            choices = response.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                _fail("INVALID_CHOICES")
            choice = choices[0]
            message = choice.get("message")
            if choice.get("finish_reason") == "length":
                _fail("MODEL_OUTPUT_TRUNCATED")
            if (type(choice.get("index")) is not int or choice["index"] != 0
                    or choice.get("finish_reason") != "stop" or not isinstance(message, dict)
                    or message.get("role") != "assistant" or message.get("tool_calls")
                    or message.get("function_call")):
                _fail("INCOMPLETE_MODEL_RESPONSE")
            reasoning_content = message.get("reasoning_content")
            if reasoning_content is not None and not isinstance(reasoning_content, str):
                _fail("INVALID_REASONING_CONTENT")
            if thinking_mode == "disabled" and (reasoning_content or "").strip():
                _fail("NON_THINKING_REASONING_CONTENT")
            usage = self._usage(response.get("usage"), thinking_mode)
            if (usage["completion_tokens"] > output_ceiling
                    or usage["total_tokens"] > CONTEXT_TOKENS):
                _fail("MODEL_USAGE_EXCEEDS_BUDGET")
            observed_attempt = {"response_id_sha256": hashlib.sha256(response_id.encode()).hexdigest(),
                "transport_response_sha256": body_hash, "request_sha256": inputs["request_sha256"],
                "finish_reason": "stop", "model": MODEL, "usage": usage}
            text = message.get("content")
            if not isinstance(text, str) or not text.strip():
                _fail("EMPTY_MODEL_OUTPUT")
            parsed = _json_loads(text)
            if not isinstance(parsed, dict) or not parsed:
                _fail("EMPTY_OR_NONOBJECT_MODEL_OUTPUT")
            parsed = self._clean(parsed)
            validate_schema(parsed, schema)
            fingerprint = response.get("system_fingerprint")
            if fingerprint is not None:
                fingerprint = self._identifier(fingerprint)
                if self._api_key in fingerprint:
                    _fail("UNSAFE_RESPONSE_FINGERPRINT")
            safe_response = {
                "id": response_id, "model": MODEL, "system_fingerprint": fingerprint,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": _canonical(parsed).decode("utf-8")}}],
                "usage": usage,
            }
            return {"schema": "tiger.deepseek-call.v1", "call_id": call_id, "stage": stage,
                    "provider": "deepseek", "requested_model": MODEL, "response_id": response_id,
                    "model": MODEL, "release": RELEASE, "usage": usage,
                    "scene": KERNEL_SCENE_MAP[stage], "thinking_mode": thinking_mode,
                    "reasoning_effort": effort, "input_token_estimate": input_token_estimate,
                    "output_ceiling": output_ceiling, "policy_version": POLICY_VERSION,
                    "registry_version": REGISTRY_VERSION, "tokenizer_sha256": TOKENIZER_SHA256,
                    "finish_reason": "stop", "request_sha256": inputs["request_sha256"],
                    "response_sha256": _hash(safe_response), "transport_response_sha256": body_hash,
                    "schema_sha256": inputs["schema_sha256"], "response": safe_response,
                    "system_fingerprint": fingerprint, "search_mode": SEARCH_MODE, "parsed": parsed}

        def invoke():
            try:
                return invoke_once()
            except RequiredModelUnavailable as error:
                diagnostic = error.diagnostic
                if (self._chat_schema_retry_used or diagnostic["code"] != "OUTPUT_SCHEMA_MISMATCH"
                        or "schema_keyword" not in diagnostic or observed_attempt is None):
                    raise
                self._chat_schema_retry_used = True
                first_attempt = observed_attempt
                return {**invoke_once(), "retry_diagnostics": [diagnostic], "retry_attempt": first_attempt}

        proof = self.execution.call_io("model.deepseek.chat", inputs, invoke)
        self.receipts.append({k: v for k, v in proof.items() if k not in ("schema", "parsed", "response")})
        return proof["parsed"]

    def _safe_native_content(self, text):
        if not isinstance(text, str):
            return None, False
        safe = self._text(text)
        # A malformed reply cannot be parsed before its first durable record.
        # Inspect bounded decoded variants for encoded credentials instead of
        # persisting them or changing malformed JSON into an accepted answer.
        decoded = text
        for _ in range(8):
            following = html.unescape(unquote(re.sub(r"\\u([0-9a-fA-F]{4})",
                lambda match: chr(int(match.group(1), 16)), decoded)))
            if following == decoded:
                break
            decoded = following
        else:
            return "[REDACTED_MODEL_CONTENT]", True
        if decoded != text and self._text(decoded) != decoded:
            return "[REDACTED_MODEL_CONTENT]", True
        return safe, safe != text

    @staticmethod
    def _observation_failure(diagnostic, observation):
        return RequiredModelUnavailable(diagnostic["code"],
            http_status=diagnostic.get("http_status"), schema_path=diagnostic.get("schema_path"),
            schema_keyword=diagnostic.get("schema_keyword"), json_stage=diagnostic["json_stage"],
            transport=diagnostic.get("transport"), observation=observation)

    def _observed_complete_json(self, *, system, user, schema_hint, max_tokens,
                               temperature, schema, public_payload):
        from .json_observation import (ATTEMPT_SCHEMA, CALL_SCHEMA, CONTRACT, REQUEST_SCHEMA,
            replay_attempt, staged_diagnostic, validate_reference_observation, validate_transport)
        if max_tokens is not None:
            _fail("CALLER_OUTPUT_CAP_FORBIDDEN")
        stage = self._stage()
        try:
            request, input_tokens, ceiling = self.plan_chat(system=system, user=user,
                schema_hint=schema_hint, schema=schema, temperature=temperature, public_payload=public_payload)
        except TokenBudgetError as error:
            _fail(error.code)
        call_id = uuid.uuid4().hex
        mode, effort = request["thinking"]["type"], request.get("reasoning_effort")
        inputs = {"schema": REQUEST_SCHEMA, "response_observation_contract": CONTRACT,
            "call_id": call_id, "stage": stage, "provider": "deepseek", "method": "POST",
            "endpoint": BASE_URL + "/chat/completions", "request": request, "request_sha256": _hash(request),
            "output_schema": schema, "schema_sha256": _hash(schema), "release": RELEASE,
            "scene": KERNEL_SCENE_MAP[stage], "thinking_mode": mode, "reasoning_effort": effort,
            "input_token_estimate": input_tokens, "output_ceiling": ceiling,
            "policy_version": POLICY_VERSION, "registry_version": REGISTRY_VERSION,
            "tokenizer_sha256": TOKENIZER_SHA256}
        observation = {"schema": CONTRACT, "call_id": call_id, "request_sha256": inputs["request_sha256"],
                       "attempts": [], "retry_used": False}

        def record(ordinal, *, native=None, body_hash=None, error=None):
            transport = self._last_chat_transport
            if native is not None and transport is None:
                # Test adapters may provide a complete decoded native object;
                # an unknown byte count remains unknown, never a fabricated 0.
                transport = {"http_status": 200, "body_sha256": body_hash, "body_bytes": None,
                    "body_whitespace_only": None, "body_complete": True}
            if transport is None:
                transport = {"http_status": None, "body_sha256": None, "body_bytes": None,
                    "body_whitespace_only": None, "body_complete": False}
            transport = validate_transport(transport)
            diagnostic = staged_diagnostic(error, "http_envelope", transport) if error is not None else None
            attempt = {"schema": ATTEMPT_SCHEMA, "call_id": call_id, "ordinal": ordinal,
                "request_sha256": inputs["request_sha256"], "transport": transport,
                "response": None, "response_sha256": None, "content_redacted": None,
                "reasoning_state": None, "fingerprint_valid": None, "transport_diagnostic": diagnostic}
            if native is not None:
                content = native["choices"][0]["message"].get("content")
                safe_content, redacted = self._safe_native_content(content)
                reasoning = native["choices"][0]["message"].get("reasoning_content")
                reasoning_state = ("absent" if reasoning is None else "invalid" if not isinstance(reasoning, str)
                                   else "present" if reasoning.strip() else "empty")
                fingerprint, fingerprint_valid = native.get("system_fingerprint"), True
                if fingerprint is not None:
                    try:
                        fingerprint = self._identifier(fingerprint)
                        if self._api_key in fingerprint:
                            _fail("UNSAFE_RESPONSE_FINGERPRINT")
                    except RequiredModelUnavailable:
                        fingerprint, fingerprint_valid = None, False
                safe_response = {"id": native["id"], "object": "chat.completion", "model": MODEL,
                    "system_fingerprint": fingerprint, "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": safe_content}}],
                    "usage": self._usage(native.get("usage"), "enabled")}
                attempt.update(response=safe_response, response_sha256=_hash(safe_response),
                    content_redacted=redacted, reasoning_state=reasoning_state, fingerprint_valid=fingerprint_valid)
            # Fail closed on an evidence-write error, before parsing or another
            # provider send. The final call/error links this exact immutable ref.
            ref = self.execution.model_attempt_snapshot(attempt)
            entry = {"ordinal": ordinal, "observation_ref": ref, "diagnostic": diagnostic}
            observation["attempts"].append(entry)
            observation["retry_used"] = len(observation["attempts"]) == 2
            return attempt, entry

        def invoke():
            for ordinal in (1, 2):
                self._last_chat_transport = None
                try:
                    native, body_hash = self._post_json("/chat/completions", request)
                    if native.get("model") != MODEL:
                        _fail("RESPONSE_MODEL_MISMATCH")
                    if native.get("object") != "chat.completion":
                        _fail("RESPONSE_OBJECT_MISMATCH")
                    response_id = self._identifier(native.get("id"))
                    if self._api_key in response_id or response_id in self._observed_response_ids:
                        _fail("RESPONSE_IDENTITY_REUSED_OR_UNSAFE")
                    choices = native.get("choices")
                    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                        _fail("INVALID_CHOICES")
                    choice, message = choices[0], choices[0].get("message")
                    if choice.get("finish_reason") == "length":
                        _fail("MODEL_OUTPUT_TRUNCATED")
                    if (type(choice.get("index")) is not int or choice["index"] != 0
                            or choice.get("finish_reason") != "stop" or not isinstance(message, dict)
                            or message.get("role") != "assistant" or message.get("tool_calls") or message.get("function_call")):
                        _fail("INCOMPLETE_MODEL_RESPONSE")
                    self._usage(native.get("usage"), "enabled")
                    self._observed_response_ids.add(response_id)
                except RequiredModelUnavailable as error:
                    _, entry = record(ordinal, error=error)
                    raise self._observation_failure(entry["diagnostic"], observation) from None
                attempt, entry = record(ordinal, native=native, body_hash=body_hash)
                verdict = replay_attempt(attempt, inputs)
                # Redaction must never turn invalid original JSON into an
                # accepted result or authorize a second billable request.
                if attempt["content_redacted"]:
                    try:
                        original = _json_loads(message["content"])
                        if not isinstance(original, dict) or not original:
                            _fail("EMPTY_OR_NONOBJECT_MODEL_OUTPUT")
                        original = self._clean(original)
                        validate_schema(original, schema)
                        if verdict["diagnostic"] is not None or _hash(original) != _hash(verdict["parsed"]):
                            _fail("RESPONSE_OBJECT_MISMATCH")
                    except RequiredModelUnavailable as error:
                        verdict.update(parsed=None, diagnostic=staged_diagnostic(error, "assistant_content"), retry_eligible=False)
                entry["diagnostic"] = verdict["diagnostic"]
                if verdict["diagnostic"] is not None:
                    if ordinal == 1 and verdict["retry_eligible"] and self.execution.claim_model_format_retry():
                        continue
                    raise self._observation_failure(verdict["diagnostic"], observation) from None
                parsed = verdict["parsed"]
                safe_response = {**attempt["response"], "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": _canonical(parsed).decode("utf-8")}}]}
                return {"schema": CALL_SCHEMA, "response_observation_contract": CONTRACT, "call_id": call_id,
                    "stage": stage, "provider": "deepseek", "requested_model": MODEL, "response_id": response_id,
                    "model": MODEL, "release": RELEASE, "usage": verdict["usage"], "scene": KERNEL_SCENE_MAP[stage],
                    "thinking_mode": mode, "reasoning_effort": effort, "input_token_estimate": input_tokens,
                    "output_ceiling": ceiling, "policy_version": POLICY_VERSION, "registry_version": REGISTRY_VERSION,
                    "tokenizer_sha256": TOKENIZER_SHA256, "finish_reason": "stop", "request_sha256": inputs["request_sha256"],
                    "response_sha256": _hash(safe_response), "transport_response_sha256": body_hash,
                    "schema_sha256": inputs["schema_sha256"], "response": safe_response,
                    "system_fingerprint": safe_response["system_fingerprint"], "search_mode": SEARCH_MODE,
                    "parsed": parsed, "response_observation": validate_reference_observation(observation)}
            raise AssertionError("Native format retry exceeded its closed bound")

        proof = self.execution.call_io("model.deepseek.chat", inputs, invoke)
        self.receipts.append({key: value for key, value in proof.items() if key not in ("schema", "parsed", "response")})
        return proof["parsed"]

    def _source_title(self, title: str) -> str:
        # Source formatting only: preserve the supplied words without markup,
        # control characters or repeated whitespace. Never invent or truncate.
        return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f<>]", " ", self._text(title)).split())

    def _document(self, *, url: str, title: str, content: str, published: object = None,
                  source: str) -> dict:
        title, content = self._source_title(title), self._text(content)
        if not title:
            _fail("SOURCE_TITLE_REQUIRED")
        if len(title) > 500:
            _fail("SOURCE_TITLE_LIMIT")
        if not content.strip():
            _fail("EMPTY_SEARCH_CONTENT")
        when = None
        if isinstance(published, str):
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    when = published
            except ValueError:
                when = None
        identity = {"url": url, "title": title, "content": content, "published": when}
        return {"id": "ds_" + _hash(identity)[:24], **identity, "source": source,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "content_hash": hashlib.sha256(content.encode()).hexdigest()}

    def search(self, query: str, count: int = 4, need_content: bool = True) -> list[dict]:
        stage = self._stage()
        if (not isinstance(query, str) or not query.strip() or not 1 <= len(query) <= 128
                or type(count) is not int or not 1 <= count <= 50 or not isinstance(need_content, bool)):
            _fail("INVALID_SEARCH_REQUEST")
        query = self._text(query)
        if len(query) > 128:
            _fail("INVALID_SEARCH_REQUEST")
        request = {"query": query, "count": count, "need_content": need_content}
        call_id = uuid.uuid4().hex
        inputs = {"schema": "tiger.deepseek-search-request.v1", "call_id": call_id, "stage": stage,
                  "provider": "deepseek", "method": "POST", "endpoint": BASE_URL + "/v1/tools/search",
                  "request": request, "request_sha256": _hash(request),
                  "query_sha256": hashlib.sha256(query.encode()).hexdigest(), "search_mode": SEARCH_MODE}

        def invoke():
            retry_diagnostics = []
            try:
                response, body_hash = self._post_json("/v1/tools/search", request)
            except RequiredModelUnavailable as error:
                if not retryable_search_failure(error.diagnostic):
                    raise
                retry_diagnostics.append(error.diagnostic)
                limits = self._native_io_limits
                if limits is not None and time.monotonic() + 1 >= limits["deadline"]:
                    limits["meter"]["limit_reason"] = "deadline"
                    _fail("RESPONSE_DEADLINE")
                time.sleep(1)
                response, body_hash = self._post_json("/v1/tools/search", request)
            original = response.get("results")
            if not isinstance(original, list) or len(original) > count:
                _fail("INVALID_SEARCH_RESPONSE")
            results = []
            for item in original:
                if not isinstance(item, dict):
                    _fail("INVALID_SEARCH_RESULT")
                url = sanitize_url(item.get("url"), secret=self._api_key)
                if url is None:
                    continue
                title = item.get("title", "")
                title = self._source_title(title) if isinstance(title, str) else None
                content = item.get("content")
                if stage != "decision.debate":
                    content = content or item.get("summary") or item.get("snippet")
                if (not title or len(title) > 500
                        or not isinstance(content, str) or not content.strip()):
                    continue
                results.append(self._document(url=url, title=title, content=content,
                               published=item.get("date"), source="deepseek_search"))
            if original and not results:
                _fail("NO_USABLE_SEARCH_RESULTS")
            return {"schema": "tiger.deepseek-search.v1", "call_id": call_id, "stage": stage,
                    "provider": "deepseek", "method": "POST", "search_mode": SEARCH_MODE,
                    "query_sha256": inputs["query_sha256"], "request_sha256": inputs["request_sha256"],
                    "result_count": len(original), "usable_result_count": len(results),
                    "retry_diagnostics": retry_diagnostics,
                    "response_sha256": _hash({"results": results}),
                    "transport_response_sha256": body_hash, "results": results,
                    **self._native_transport_usage()}

        proof = self.execution.call_io("model.deepseek.search", inputs, invoke)
        self.search_receipts.append({k: v for k, v in proof.items() if k not in ("schema", "results")})
        return proof["results"]

    def fetch(self, url: str) -> dict:
        stage = self._stage()
        safe_url = sanitize_url(url, secret=self._api_key)
        if safe_url is None:
            _fail("UNSAFE_FETCH_URL")
        request = {"url": safe_url}
        call_id = uuid.uuid4().hex
        inputs = {"schema": "tiger.deepseek-fetch-request.v1", "call_id": call_id, "stage": stage,
                  "provider": "deepseek", "method": "POST", "endpoint": BASE_URL + "/v1/tools/fetch",
                  "request": request, "request_sha256": _hash(request), "search_mode": SEARCH_MODE}

        def invoke():
            response, body_hash = self._post_json("/v1/tools/fetch", request)
            if not isinstance(response.get("title"), str) or not isinstance(response.get("content"), str):
                _fail("INVALID_FETCH_RESPONSE")
            page = self._document(url=safe_url, title=response["title"], content=response["content"],
                                  source="deepseek_fetch")
            return {"schema": "tiger.deepseek-fetch.v1", "call_id": call_id, "stage": stage,
                    "provider": "deepseek", "method": "POST", "search_mode": SEARCH_MODE,
                    "request_sha256": inputs["request_sha256"], "response_sha256": _hash({"page": page}),
                    "transport_response_sha256": body_hash, "page": page,
                    **self._native_transport_usage()}

        proof = self.execution.call_io("model.deepseek.fetch", inputs, invoke)
        self.fetch_receipts.append({k: v for k, v in proof.items() if k not in ("schema", "page")})
        return proof["page"]
