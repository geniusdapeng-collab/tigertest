"""Complete-wire DeepSeek Flash budgeting with the pinned official tokenizer.

The model window is shared by fully serialized input and generated output.
Callers therefore never choose a small arbitrary completion cap: this module
assigns the complete legal remaining output window after tokenizing the exact
canonical request bytes. This local estimate is distinct from provider usage
and does not claim to reproduce the provider's private chat framing.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from tokenizers import Tokenizer

MODEL_ID = "deepseek-flash"
CONTEXT_TOKENS = 1_048_576
MAX_OUTPUT_TOKENS = 393_216
TOKENIZER_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"
POLICY_VERSION = "workloom.text-primary/v1"
REGISTRY_VERSION = "deepseek-flash/1048576x393216/v1"
TOKENIZER_PATH = Path(__file__).with_name("assets") / "deepseek-v4-tokenizer.json"
CLEAN_TOOL_FUNCTION = "submit_clean_document_rows"


class TokenBudgetError(RuntimeError):
    """A request cannot fit or the pinned tokenizer is unavailable or altered."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise TokenBudgetError("INVALID_JSON_VALUE") from None


@lru_cache(maxsize=1)
def _tokenizer() -> Tokenizer:
    try:
        raw = TOKENIZER_PATH.read_bytes()
    except OSError:
        raise TokenBudgetError("TOKENIZER_UNAVAILABLE") from None
    if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise TokenBudgetError("TOKENIZER_INTEGRITY_MISMATCH")
    try:
        return Tokenizer.from_file(str(TOKENIZER_PATH))
    except Exception:
        raise TokenBudgetError("TOKENIZER_LOAD_FAILED") from None


def count_json_tokens(value: object) -> int:
    """Tokenize the exact canonical JSON bytes used for the HTTP request."""
    text = canonical_json(value)
    try:
        return len(_tokenizer().encode(text, add_special_tokens=True).ids)
    except Exception:
        raise TokenBudgetError("TOKENIZER_ENCODE_FAILED") from None


def chat_request_core(*, system: str, user: str, schema: dict, schema_hint: dict,
                      thinking_mode: str, effort: str | None, temperature: float,
                      clean_protocol: dict | None = None) -> dict:
    """Build the fixed Tiger text-only wire request before planning its window."""
    if clean_protocol is not None:
        return clean_tool_request_core(system=system, user=user, schema=schema,
            schema_hint=schema_hint, thinking_mode=thinking_mode, effort=effort,
            temperature=temperature, clean_protocol=clean_protocol)
    request = {"model": MODEL_ID, "messages": [
        {"role": "system", "content": system + "\nReturn exactly one JSON object. JSON only."},
        {"role": "user", "content": user + "\nJSON schema:\n" + canonical_json(schema)
         + "\nJSON field guidance:\n" + canonical_json(schema_hint)}],
        "stream": False, "response_format": {"type": "json_object"},
        "thinking": {"type": thinking_mode}}
    if thinking_mode == "enabled":
        request["reasoning_effort"] = effort
    else:
        request["temperature"] = temperature
    return request


def provider_clean_schema(schema: dict) -> dict:
    """Remove only strict-provider unsupported grammar keywords.

    The complete keyed grammar and original final business schema remain local
    acceptance authorities. Nullable branches, patterns, enums and numeric
    bounds are copied unchanged. References are never introduced or accepted.
    """
    omitted = {"minLength", "maxLength", "minItems", "maxItems", "uniqueItems"}
    supported = {"type", "properties", "required", "additionalProperties", "items",
                 "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                 "pattern", "anyOf"}

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        if set(value) - supported - omitted:
            raise TokenBudgetError("UNSUPPORTED_OUTPUT_SCHEMA")
        result = {}
        for key, item in value.items():
            if key in omitted:
                continue
            result[key] = ({name: visit(child) for name, child in item.items()}
                           if key == "properties" else visit(item))
        if result.get("type") == "object" and (
                set(result.get("required", [])) != set(result.get("properties", {}))
                or result.get("additionalProperties") is not False):
            raise TokenBudgetError("UNSUPPORTED_OUTPUT_SCHEMA")
        return result

    return visit(schema)


def clean_tool_request_core(*, system: str, user: str, schema: dict, schema_hint: dict,
                            thinking_mode: str, effort: str | None, temperature: float,
                            clean_protocol: dict) -> dict:
    """Build the inline-only, non-executable clean data function wire."""
    from .clean_keyed_codec import CONTRACT, build_inline_schema

    if (clean_protocol != {"contract": CONTRACT} or thinking_mode != "disabled"
            or effort is not None):
        raise TokenBudgetError("INVALID_CHAT_REQUEST")
    try:
        grammar = provider_clean_schema(build_inline_schema(schema))
    except (TypeError, ValueError, KeyError, RecursionError):
        raise TokenBudgetError("UNSUPPORTED_OUTPUT_SCHEMA") from None
    return {"model": MODEL_ID, "messages": [
        {"role": "system", "content": system +
         "\nReturn the complete keyed document data only through the fixed function " +
         CLEAN_TOOL_FUNCTION + ". Do not execute tools or return a tool execution result."},
        {"role": "user", "content": user +
         "\nOriginal final acceptance JSON schema (after lossless flatten):\n" + canonical_json(schema) +
         "\nJSON field guidance:\n" + canonical_json(schema_hint)}],
        "stream": False, "thinking": {"type": "disabled"}, "temperature": temperature,
        "tools": [{"type": "function", "function": {"name": CLEAN_TOOL_FUNCTION,
            "description": "Return every supplied document's complete annotations as structured data. This function has no executable side effect.",
            "strict": True, "parameters": grammar}}],
        "tool_choice": {"type": "function", "function": {"name": CLEAN_TOOL_FUNCTION}}}


def plan_request(request_without_max_tokens: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    """Return the request, exact input-token count, and legal output ceiling."""
    if not isinstance(request_without_max_tokens, dict):
        raise TokenBudgetError("INVALID_REQUEST")
    if "max_tokens" in request_without_max_tokens:
        raise TokenBudgetError("CALLER_OUTPUT_CAP_FORBIDDEN")
    candidate = MAX_OUTPUT_TOKENS
    visited: set[int] = set()
    feasible: dict[int, tuple[dict[str, Any], int, int]] = {}
    for _ in range(16):
        if candidate in visited:
            # The numeric output field itself is present in the counted JSON.
            # At a digit-token boundary (e.g. 999 -> 1000), increasing by one
            # can add an input token. Use the largest proven feasible wire in
            # that cycle instead of rejecting a request that actually fits.
            if feasible:
                return feasible[max(feasible)]
            break
        visited.add(candidate)
        request = {**request_without_max_tokens, "max_tokens": candidate}
        input_tokens = count_json_tokens(request)
        remaining = CONTEXT_TOKENS - input_tokens
        if remaining < 1:
            if candidate == 1:
                raise TokenBudgetError("INPUT_CONTEXT_EXCEEDED")
            candidate = 1  # A shorter wire field may leave one valid output token.
            continue
        if candidate <= remaining:
            feasible[candidate] = (request, input_tokens, candidate)
        ceiling = min(MAX_OUTPUT_TOKENS, remaining)
        if ceiling == candidate:
            return request, input_tokens, ceiling
        candidate = ceiling
    raise TokenBudgetError("TOKEN_BUDGET_DID_NOT_CONVERGE")


def tokenizer_receipt() -> dict[str, Any]:
    _tokenizer()
    return {
        "sha256": TOKENIZER_SHA256,
        "context_tokens": CONTEXT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "policy_version": POLICY_VERSION,
        "registry_version": REGISTRY_VERSION,
    }
