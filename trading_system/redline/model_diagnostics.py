"""Closed, value-free diagnostics for required model failures.

This module has no transport or provider imports. Only controlled failure
codes, HTTP status and schema locations may cross the error evidence boundary.
It never extracts data from exception messages or arbitrary exception fields.
"""
from __future__ import annotations

import re

from . import LLMUnavailable


MODEL_IO_METHODS = frozenset({
    "model.deepseek.chat", "model.deepseek.search", "model.deepseek.fetch",
})
MODEL_FAILURE_CODES = frozenset({
    "ACTIVE_STAGE_REQUIRED", "API_KEY_REQUIRED", "DUPLICATE_RESPONSE_JSON_KEY",
    "EMPTY_MODEL_OUTPUT", "EMPTY_OR_NONOBJECT_MODEL_OUTPUT", "EMPTY_SEARCH_CONTENT",
    "ENDPOINT_NOT_ALLOWED", "EXECUTION_REQUIRED", "HTTP_ERROR", "HTTP_INVALID",
    "INCOMPLETE_MODEL_RESPONSE", "INCONSISTENT_USAGE", "INVALID_CHAT_REQUEST",
    "INVALID_CHOICES", "INVALID_FETCH_RESPONSE", "INVALID_JSON_OBJECT_KEY",
    "INVALID_JSON_VALUE", "INVALID_RESPONSE_BYTES", "INVALID_RESPONSE_IDENTITY",
    "INVALID_RESPONSE_JSON", "INVALID_SCHEMA_ANNOTATION", "INVALID_SCHEMA_BOUND",
    "INVALID_SCHEMA_COMBINATOR", "INVALID_SCHEMA_ENUM", "INVALID_SCHEMA_LENGTH",
    "INVALID_SCHEMA_PATTERN", "INVALID_SCHEMA_PROPERTIES", "INVALID_SCHEMA_REQUIRED",
    "INVALID_SCHEMA_TYPE", "INVALID_SCHEMA_UNIQUENESS", "INVALID_SEARCH_REQUEST",
    "INVALID_SEARCH_RESPONSE", "INVALID_SEARCH_RESULT", "INVALID_USAGE",
    "INVALID_REASONING_CONTENT", "MODEL_SCENE_POLICY_INCOMPLETE",
    "MODEL_SCENE_POLICY_INVALID", "MODEL_USAGE_EXCEEDS_BUDGET", "MODEL_OUTPUT_TRUNCATED",
    "NETWORK_TIMEOUT", "NON_THINKING_OUTPUT_REQUIRED", "NONFINITE_RESPONSE_JSON", "NO_USABLE_SEARCH_RESULTS",
    "NON_THINKING_REASONING_CONTENT", "UNSUPPORTED_MODEL_STAGE",
    "TOKENIZER_UNAVAILABLE", "TOKENIZER_INTEGRITY_MISMATCH", "TOKENIZER_LOAD_FAILED",
    "TOKENIZER_ENCODE_FAILED", "CALLER_OUTPUT_CAP_FORBIDDEN",
    "INPUT_CONTEXT_EXCEEDED", "TOKEN_BUDGET_DID_NOT_CONVERGE",
    "OUTPUT_SCHEMA_MISMATCH", "REQUEST_TOO_LARGE", "RESPONSE_CLOSE_FAILURE",
    "RESPONSE_DEADLINE", "RESPONSE_DEPTH_LIMIT", "RESPONSE_IDENTITY_REUSED_OR_UNSAFE",
    "RESPONSE_MODEL_MISMATCH", "RESPONSE_NOT_OBJECT", "RESPONSE_OBJECT_MISMATCH",
    "RESPONSE_TOO_LARGE", "SOURCE_TITLE_LIMIT", "SOURCE_TITLE_REQUIRED",
    "TRANSPORT_FAILURE", "TRUSTED_CONFIGURATION_MISMATCH", "UNEXPECTED_CONTENT_ENCODING",
    "UNEXPECTED_CONTENT_TYPE", "UNSAFE_FETCH_URL", "UNSAFE_OUTPUT_SCHEMA",
    "UNSAFE_RESPONSE_FINGERPRINT", "UNSUPPORTED_OUTPUT_SCHEMA", "USAGE_REQUIRED",
})
SCHEMA_FAILURE_KEYWORDS = frozenset({
    "type", "const", "enum", "allOf", "anyOf", "oneOf", "required",
    "additionalProperties", "minItems", "maxItems", "uniqueItems", "items",
    "minLength", "maxLength", "pattern", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "depth",
})
_POINTER = re.compile(r"(?:/(?:[A-Za-z_][A-Za-z0-9_.:-]{0,127}|[0-9]{1,10}|\*))*")


def validate_model_diagnostic(value: object) -> dict:
    """Return a detached closed diagnostic or reject without quoting its data."""
    fields = {"schema", "provider", "code", "http_status", "schema_path", "schema_keyword"}
    versioned = type(value) is dict and value.get("schema") == "tiger.model-failure.v2"
    if versioned:
        fields |= {"json_stage", "transport"}
    if (type(value) is not dict or set(value) - fields
            or not {"schema", "provider", "code"}.issubset(value)
            or type(value["schema"]) is not str
            or value["schema"] not in {"tiger.model-failure.v1", "tiger.model-failure.v2"}
            or value["provider"] != "deepseek"
            or type(value["code"]) is not str or value["code"] not in MODEL_FAILURE_CODES):
        raise ValueError("Invalid model failure diagnostic")
    if versioned:
        if type(value.get("json_stage")) is not str or value["json_stage"] not in {"http_envelope", "assistant_content"}:
            raise ValueError("Invalid model JSON failure stage")
        if "transport" in value:
            from ..llm.json_observation import validate_transport
            if value["json_stage"] != "http_envelope":
                raise ValueError("Unexpected native transport diagnostic")
            validate_transport(value["transport"])
    if value["code"] == "HTTP_ERROR":
        if type(value.get("http_status")) is not int or not 100 <= value["http_status"] <= 599:
            raise ValueError("Invalid model failure HTTP status")
    elif "http_status" in value:
        raise ValueError("Unexpected model failure HTTP status")
    located = {"schema_path", "schema_keyword"}.intersection(value)
    if located:
        path, keyword = value.get("schema_path"), value.get("schema_keyword")
        if (located != {"schema_path", "schema_keyword"}
                or value["code"] != "OUTPUT_SCHEMA_MISMATCH"
                or type(path) is not str or len(path) > 512 or _POINTER.fullmatch(path) is None
                or type(keyword) is not str or keyword not in SCHEMA_FAILURE_KEYWORDS):
            raise ValueError("Invalid model failure schema location")
    if "transport" in value:
        return {**value, "transport": dict(value["transport"])}
    return dict(value)


class RequiredModelUnavailable(LLMUnavailable):
    """A transport-created failure carrying only prevalidated safe metadata."""

    def __init__(self, code: str, *, http_status: int | None = None,
                 schema_path: str | None = None, schema_keyword: str | None = None,
                 json_stage: str | None = None, transport: dict | None = None,
                 observation: dict | None = None):
        diagnostic = {"schema": "tiger.model-failure.v1", "provider": "deepseek", "code": code}
        if http_status is not None:
            diagnostic["http_status"] = http_status
        if schema_path is not None:
            diagnostic["schema_path"] = schema_path
        if schema_keyword is not None:
            diagnostic["schema_keyword"] = schema_keyword
        if json_stage is not None:
            diagnostic.update(schema="tiger.model-failure.v2", json_stage=json_stage)
        if transport is not None:
            diagnostic["transport"] = transport
        self._diagnostic = validate_model_diagnostic(diagnostic)
        self._observation = None
        if observation is not None:
            from ..llm.json_observation import validate_reference_observation
            self._observation = validate_reference_observation(observation)
        label = f"HTTP_{http_status}" if code == "HTTP_ERROR" else code
        super().__init__(f"DeepSeek required: {label}")

    @property
    def diagnostic(self) -> dict:
        return validate_model_diagnostic(self._diagnostic)

    @property
    def observation(self) -> dict | None:
        if self._observation is None:
            return None
        from ..llm.json_observation import validate_reference_observation
        return validate_reference_observation(self._observation)


def model_failure_diagnostic(error: BaseException) -> dict | None:
    """Ignore generic failures, subclasses and malformed metadata entirely."""
    if type(error) is not RequiredModelUnavailable:
        return None
    try:
        return error.diagnostic
    except ValueError:
        # Malformed metadata cannot replace the original failure.
        return None


def model_failure_observation(error: BaseException) -> dict | None:
    """Only an exact trusted transport failure may expose closed native refs."""
    if type(error) is not RequiredModelUnavailable:
        return None
    try:
        return error.observation
    except ValueError:
        return None
