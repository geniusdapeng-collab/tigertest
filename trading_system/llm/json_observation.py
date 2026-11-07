"""Versioned, keyless native JSON observations and replay eligibility.

This module has no transport import at module load. Frozen source membership
activates the contract only for newly admitted intelligence-paper runs. Native
snapshots are written before content parsing; references, never exception text,
connect them to either the successful call or the failed-stage event.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

CONTRACT = "tiger.deepseek-json-observation/v2"
SOURCE_PATH = "trading_system/llm/json_observation.py"
ATTEMPT_SCHEMA = "tiger.deepseek-observed-attempt/v2"
REQUEST_SCHEMA = "tiger.deepseek-request.v2"
CALL_SCHEMA = "tiger.deepseek-call.v2"
FAILED_STAGE_SCHEMA = "tiger.failed-stage-output/v2"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CALL = re.compile(r"[0-9a-f]{32}\Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_transport(value):
    fields = {"http_status", "body_sha256", "body_bytes", "body_whitespace_only", "body_complete"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Invalid native transport observation")
    if (value["http_status"] is not None and (type(value["http_status"]) is not int
            or not 100 <= value["http_status"] <= 599)
            or value["body_sha256"] is not None and (not isinstance(value["body_sha256"], str)
                or _HASH.fullmatch(value["body_sha256"]) is None)
            or value["body_bytes"] is not None and (type(value["body_bytes"]) is not int or value["body_bytes"] < 0)
            or value["body_whitespace_only"] is not None and type(value["body_whitespace_only"]) is not bool
            or type(value["body_complete"]) is not bool):
        raise ValueError("Invalid native transport observation")
    return deepcopy(value)


def validate_reference_observation(value):
    """Accept only closed standard refs; never arbitrary exception attributes."""
    from ..redline.model_diagnostics import validate_model_diagnostic
    if (not isinstance(value, dict) or set(value) != {
            "schema", "call_id", "request_sha256", "attempts", "retry_used"}
            or value["schema"] != CONTRACT or not isinstance(value["call_id"], str)
            or _CALL.fullmatch(value["call_id"]) is None
            or not isinstance(value["request_sha256"], str) or _HASH.fullmatch(value["request_sha256"]) is None
            or not isinstance(value["attempts"], list) or not 1 <= len(value["attempts"]) <= 2
            or type(value["retry_used"]) is not bool or value["retry_used"] != (len(value["attempts"]) == 2)):
        raise ValueError("Invalid native observation references")
    for ordinal, item in enumerate(value["attempts"], 1):
        if (not isinstance(item, dict) or set(item) != {"ordinal", "observation_ref", "diagnostic"}
                or type(item["ordinal"]) is not int or item["ordinal"] != ordinal):
            raise ValueError("Invalid native observation ordinal")
        ref = item["observation_ref"]
        if (not isinstance(ref, dict) or set(ref) != {"path", "sha256", "bytes", "schema"}
                or not isinstance(ref["path"], str)
                or re.fullmatch(r"evidence/[0-9a-f]{64}\.json", ref["path"]) is None
                or not isinstance(ref["sha256"], str) or _HASH.fullmatch(ref["sha256"]) is None
                or type(ref["bytes"]) is not int or ref["bytes"] <= 0 or ref["schema"] != "tiger.snapshot.v1"):
            raise ValueError("Invalid native observation reference")
        if item["diagnostic"] is not None:
            validate_model_diagnostic(item["diagnostic"])
        if ordinal < len(value["attempts"]) and item["diagnostic"] is None:
            raise ValueError("A retry requires an original failed response")
    return deepcopy(value)


def staged_diagnostic(value, stage, transport=None):
    """Version the known closed code without introducing provider values."""
    from ..redline.model_diagnostics import RequiredModelUnavailable, validate_model_diagnostic
    if type(value) is not dict and type(value) is not RequiredModelUnavailable:
        raise ValueError("Untrusted model failure diagnostic")
    original = value.diagnostic if type(value) is RequiredModelUnavailable else value
    original = validate_model_diagnostic(original)
    result = {key: item for key, item in original.items() if key not in {"json_stage", "transport"}}
    result.update(schema="tiger.model-failure.v2", json_stage=stage)
    if transport is not None:
        result["transport"] = validate_transport(transport)
    return validate_model_diagnostic(result)


def validate_attempt(value, inputs):
    fields = {"schema", "call_id", "ordinal", "request_sha256", "transport", "response",
        "response_sha256", "content_redacted", "reasoning_state", "fingerprint_valid", "transport_diagnostic"}
    if (not isinstance(value, dict) or set(value) != fields or value["schema"] != ATTEMPT_SCHEMA
            or value["call_id"] != inputs["call_id"] or value["request_sha256"] != inputs["request_sha256"]
            or type(value["ordinal"]) is not int or value["ordinal"] not in {1, 2}):
        raise ValueError("Native observation differs from its request")
    validate_transport(value["transport"])
    if value["response"] is None:
        if (any(value[name] is not None for name in ("response_sha256", "content_redacted", "reasoning_state", "fingerprint_valid"))
                or value["transport_diagnostic"] is None):
            raise ValueError("Unknown native usage cannot have a known response")
        from ..redline.model_diagnostics import validate_model_diagnostic
        diagnostic = validate_model_diagnostic(value["transport_diagnostic"])
        if (diagnostic.get("json_stage") != "http_envelope"
                or diagnostic.get("transport") != value["transport"]):
            raise ValueError("Unknown native usage requires an envelope diagnostic")
    elif (not isinstance(value["response"], dict) or value["response_sha256"] != digest(value["response"])
            or type(value["content_redacted"]) is not bool or type(value["fingerprint_valid"]) is not bool
            or type(value["reasoning_state"]) is not str
            or value["reasoning_state"] not in {"absent", "empty", "present", "invalid"}
            or value["transport_diagnostic"] is not None
            or value["transport"]["http_status"] != 200 or value["transport"]["body_complete"] is not True
            or not isinstance(value["transport"]["body_sha256"], str)
            or value["transport"]["body_whitespace_only"] not in {False, None}
            or value["transport"]["body_bytes"] is not None and value["transport"]["body_bytes"] <= 0):
        raise ValueError("Invalid known native response observation")
    return deepcopy(value)


def replay_attempt(value, inputs):
    """Recompute content acceptance and retry eligibility from a saved reply.

The original failed code is never used to authorize retry. Unknown envelopes,
redacted content and ancillary-policy failures cannot authorize another send.
Generic valid usage is retained even if a later budget/mode check rejects it.
"""
    from .deepseek_required import (DeepSeekRequiredClient, MODEL, CONTEXT_TOKENS,
        _json_loads, sanitize_model_value, validate_schema)
    from ..redline.model_diagnostics import RequiredModelUnavailable
    value = validate_attempt(value, inputs)
    if value["response"] is None:
        return {"parsed": None, "diagnostic": value["transport_diagnostic"],
                "usage": None, "retry_eligible": False}
    response = value["response"]
    if set(response) != {"id", "object", "model", "system_fingerprint", "choices", "usage"}:
        raise ValueError("Known native envelope fields differ")
    DeepSeekRequiredClient._identifier(response["id"])
    choices = response["choices"]
    if (response["model"] != MODEL or response["object"] != "chat.completion"
            or not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)
            or set(choices[0]) != {"index", "finish_reason", "message"}
            or type(choices[0]["index"]) is not int or choices[0]["index"] != 0
            or choices[0]["finish_reason"] != "stop" or not isinstance(choices[0]["message"], dict)
            or set(choices[0]["message"]) != {"role", "content"}
            or choices[0]["message"]["role"] != "assistant"):
        raise ValueError("Known native envelope is not a complete canonical reply")
    usage = DeepSeekRequiredClient._usage(response["usage"], "enabled")
    if usage != response["usage"]:
        raise ValueError("Known native usage is not normalized")
    content = choices[0]["message"]["content"]
    if content is not None and not isinstance(content, str):
        raise ValueError("Known native content projection differs")
    parsed, diagnostic = None, None
    try:
        if value["reasoning_state"] == "invalid":
            raise RequiredModelUnavailable("INVALID_REASONING_CONTENT")
        if inputs["thinking_mode"] == "disabled" and value["reasoning_state"] == "present":
            raise RequiredModelUnavailable("NON_THINKING_REASONING_CONTENT")
        DeepSeekRequiredClient._usage(usage, inputs["thinking_mode"])
        if usage["completion_tokens"] > inputs["output_ceiling"] or usage["total_tokens"] > CONTEXT_TOKENS:
            raise RequiredModelUnavailable("MODEL_USAGE_EXCEEDS_BUDGET")
        if not value["fingerprint_valid"]:
            raise RequiredModelUnavailable("UNSAFE_RESPONSE_FINGERPRINT")
        fingerprint = response["system_fingerprint"]
        if fingerprint is not None:
            DeepSeekRequiredClient._identifier(fingerprint)
        if not isinstance(content, str) or not content.strip():
            raise RequiredModelUnavailable("EMPTY_MODEL_OUTPUT")
        parsed = _json_loads(content)
        if not isinstance(parsed, dict) or not parsed:
            raise RequiredModelUnavailable("EMPTY_OR_NONOBJECT_MODEL_OUTPUT")
        parsed = sanitize_model_value(parsed)
        validate_schema(parsed, inputs["output_schema"])
    except RequiredModelUnavailable as error:
        diagnostic = staged_diagnostic(error, "assistant_content")
    eligible = (diagnostic is not None and diagnostic["code"] in {
        "INVALID_RESPONSE_JSON", "EMPTY_MODEL_OUTPUT", "OUTPUT_SCHEMA_MISMATCH"}
        and isinstance(content, str) and value["content_redacted"] is False)
    return {"parsed": parsed if diagnostic is None else None, "diagnostic": diagnostic,
            "usage": usage, "retry_eligible": eligible}
