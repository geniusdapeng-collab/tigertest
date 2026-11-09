"""Native accounting for complete envelopes rejected before content parsing.

The versioned observation retains the actual finish/shape and valid usage.
It never fabricates a stop response, parses truncated content, grants a retry,
or retroactively fills an older observation whose usage remains unknown.
"""
from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import re

CONTRACT = "tiger.rejected-native/v1"
_FINISH = {"stop", "length", "tool_calls", "content_filter", "insufficient_system_resource"}
_CODES = {"MODEL_OUTPUT_TRUNCATED", "INCOMPLETE_MODEL_RESPONSE", "INVALID_CHOICES"}


def _code(value):
    if value["choice_count"] != 1 or not value["choice_mapping"]:
        return "INVALID_CHOICES"
    if value["finish_reason"] == "length":
        return "MODEL_OUTPUT_TRUNCATED"
    if (value["index_state"] != "zero" or value["finish_reason"] != "stop"
            or not value["message_mapping"] or value["role_state"] != "assistant"
            or value["tool_calls_present"] or value["function_call_present"]):
        return "INCOMPLETE_MODEL_RESPONSE"
    raise ValueError("REJECTED_NATIVE_IS_NOT_A_REJECTION")


def capture(native, inputs, *, ordinal, body_hash, code, secret, sanitize_identifier):
    """Return only a checked accounting observation, or preserve unknown usage."""
    from .deepseek_required import DeepSeekRequiredClient, MODEL
    from .json_observation import digest
    from ..redline.model_diagnostics import RequiredModelUnavailable
    if inputs.get("rejected_native_contract") != CONTRACT or code not in _CODES or not isinstance(native, dict):
        return None
    try:
        if native.get("model") != MODEL or native.get("object") != "chat.completion":
            return None
        response_id = DeepSeekRequiredClient._identifier(native.get("id"))
        encoded_secrets = () if not secret else (
            secret, base64.b64encode(secret.encode()).decode().rstrip("="),
            base64.urlsafe_b64encode(secret.encode()).decode().rstrip("="))
        if any(value in response_id for value in encoded_secrets):
            return None
        safe_identity, redacted = sanitize_identifier(response_id)
        if redacted or safe_identity != response_id:
            return None
        usage = DeepSeekRequiredClient._usage(native.get("usage"), "enabled")
    except RequiredModelUnavailable:
        return None
    choices = native.get("choices")
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) else None
    message = choice.get("message") if choice is not None else None
    index = choice.get("index") if choice is not None else None
    finish = choice.get("finish_reason") if choice is not None else None
    content = message.get("content") if isinstance(message, dict) else None
    role = message.get("role") if isinstance(message, dict) else None
    value = {"schema": CONTRACT, "call_id": inputs["call_id"], "ordinal": ordinal,
        "request_sha256": inputs["request_sha256"], "transport_response_sha256": body_hash,
        "provider": "deepseek", "response_id": response_id, "model": MODEL,
        "object": "chat.completion", "usage": usage,
        "choice_count": len(choices) if isinstance(choices, list) else None,
        "choice_mapping": choice is not None,
        "index_state": "zero" if type(index) is int and index == 0 else "other" if index is not None else "missing",
        "finish_reason": finish if type(finish) is str and finish in _FINISH else "missing" if finish is None else "other",
        "finish_reason_sha256": digest(finish),
        "message_mapping": isinstance(message, dict),
        "role_state": "assistant" if role == "assistant" else "other" if role is not None else "missing",
        "tool_calls_present": bool(message.get("tool_calls")) if isinstance(message, dict) else False,
        "function_call_present": bool(message.get("function_call")) if isinstance(message, dict) else False,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest() if isinstance(content, str) else None,
        "content_bytes": len(content.encode()) if isinstance(content, str) else None}
    value["observation_sha256"] = digest(value)
    if _code(value) != code:
        return None
    return validate(value, inputs, ordinal=ordinal, transport_hash=body_hash)


def validate(value, inputs, *, ordinal, transport_hash):
    from .deepseek_required import DeepSeekRequiredClient, MODEL
    from .json_observation import digest
    fields = {"schema", "call_id", "ordinal", "request_sha256", "transport_response_sha256", "provider",
        "response_id", "model", "object", "usage", "choice_count", "choice_mapping", "index_state",
        "finish_reason", "finish_reason_sha256", "message_mapping", "role_state", "tool_calls_present",
        "function_call_present", "content_sha256", "content_bytes", "observation_sha256"}
    if (inputs.get("rejected_native_contract") != CONTRACT or type(value) is not dict or set(value) != fields
            or value["schema"] != CONTRACT or value["call_id"] != inputs["call_id"]
            or type(value["ordinal"]) is not int or value["ordinal"] != ordinal
            or value["request_sha256"] != inputs["request_sha256"]
            or value["transport_response_sha256"] != transport_hash
            or type(transport_hash) is not str or re.fullmatch(r"[0-9a-f]{64}", transport_hash) is None
            or value["provider"] != "deepseek" or value["model"] != MODEL or value["object"] != "chat.completion"
            or value["observation_sha256"] != digest({key: item for key, item in value.items() if key != "observation_sha256"})):
        raise ValueError("REJECTED_NATIVE_BINDING_MISMATCH")
    DeepSeekRequiredClient._identifier(value["response_id"])
    if DeepSeekRequiredClient._usage(value["usage"], "enabled") != value["usage"]:
        raise ValueError("REJECTED_NATIVE_USAGE_INVALID")
    if (any(type(value[key]) is not bool for key in ("choice_mapping", "message_mapping", "tool_calls_present", "function_call_present"))
            or value["choice_count"] is not None and (type(value["choice_count"]) is not int or value["choice_count"] < 0)
            or value["index_state"] not in {"zero", "other", "missing"}
            or value["role_state"] not in {"assistant", "other", "missing"}
            or value["finish_reason"] not in _FINISH | {"missing", "other"}
            or type(value["finish_reason_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["finish_reason_sha256"]) is None
            or value["finish_reason"] in _FINISH and value["finish_reason_sha256"] != digest(value["finish_reason"])
            or value["finish_reason"] == "missing" and value["finish_reason_sha256"] != digest(None)
            or value["choice_mapping"] and value["choice_count"] != 1
            or value["message_mapping"] and not value["choice_mapping"]
            or (value["content_sha256"] is None) != (value["content_bytes"] is None)
            or value["content_bytes"] is not None and (type(value["content_bytes"]) is not int or value["content_bytes"] < 0)
            or value["content_sha256"] is not None and (type(value["content_sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", value["content_sha256"]) is None)):
        raise ValueError("REJECTED_NATIVE_SHAPE_INVALID")
    _code(value)
    return deepcopy(value)


def replay(value, inputs, *, ordinal, transport):
    from .json_observation import staged_diagnostic
    from ..redline.model_diagnostics import RequiredModelUnavailable
    value = validate(value, inputs, ordinal=ordinal, transport_hash=transport["body_sha256"])
    if transport["http_status"] != 200 or transport["body_complete"] is not True:
        raise ValueError("REJECTED_NATIVE_ENVELOPE_INCOMPLETE")
    return {"parsed": None, "diagnostic": staged_diagnostic(RequiredModelUnavailable(_code(value)), "http_envelope", transport),
            "usage": value["usage"], "retry_eligible": False}


def response_identity(attempt):
    if attempt["response"] is not None:
        return attempt["response"]["id"], attempt["response_sha256"]
    value = attempt.get("rejected_native")
    return (None, None) if value is None else (value["response_id"], value["observation_sha256"])
