"""Closed, value-free diagnostics for RESULT_REJECTED failures."""
from __future__ import annotations

import re

SCHEMA = "tiger.validation-diagnostic/v1"
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")
_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
_PATH = re.compile(r"(?:/(?:[A-Za-z_][A-Za-z0-9_.:-]{0,127}|[0-9]{1,10}|\*))*\Z")
_KEYWORDS = frozenset({
    "type", "const", "enum", "allOf", "anyOf", "oneOf", "required",
    "additionalProperties", "minItems", "maxItems", "uniqueItems", "items",
    "minLength", "maxLength", "pattern", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "depth",
})


def validate(value: object) -> dict:
    fields = {"schema", "validator", "reason", "schema_path", "schema_keyword"}
    if (type(value) is not dict or set(value) != fields or value["schema"] != SCHEMA
            or type(value["validator"]) is not str or _IDENTITY.fullmatch(value["validator"]) is None
            or type(value["reason"]) is not str or _REASON.fullmatch(value["reason"]) is None
            or type(value["schema_path"]) is not str or len(value["schema_path"]) > 512
            or _PATH.fullmatch(value["schema_path"]) is None
            or value["schema_keyword"] is not None
            and value["schema_keyword"] not in _KEYWORDS):
        raise ValueError("INVALID_VALIDATION_DIAGNOSTIC")
    return dict(value)


def from_exception(error: BaseException, validator: object) -> dict:
    validator_name = validator if isinstance(validator, str) and _IDENTITY.fullmatch(validator) else "unknown-validator"
    reason = getattr(error, "code", None)
    if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
        reason = "RESULT_REJECTED"
    diagnostic = getattr(error, "diagnostic", None)
    path, keyword = "", None
    if isinstance(diagnostic, dict):
        candidate_path = diagnostic.get("schema_path")
        candidate_keyword = diagnostic.get("schema_keyword")
        candidate_code = diagnostic.get("code")
        if isinstance(candidate_path, str) and len(candidate_path) <= 512 and _PATH.fullmatch(candidate_path):
            path = candidate_path
        if candidate_keyword in _KEYWORDS:
            keyword = candidate_keyword
        if isinstance(candidate_code, str) and _REASON.fullmatch(candidate_code):
            reason = candidate_code
    return validate({
        "schema": SCHEMA,
        "validator": validator_name,
        "reason": reason,
        "schema_path": path,
        "schema_keyword": keyword,
    })
