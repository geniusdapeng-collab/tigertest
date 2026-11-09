"""Versioned complete-prompt DeepSeek Flash admission and historical replay.

Current requests use the pinned V4.1 reference formatter and tokenizer. The
historical HTTP-JSON counter remains explicit and read-only for old evidence;
it cannot silently authorize a new request. Provider usage is still the
accounting authority, distinct from this versioned pre-dispatch calculation.
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
LEGACY_TOKENIZER_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"
TOKENIZER_SHA256 = "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b"
POLICY_VERSION = "workloom.text-primary/v1"
LEGACY_REGISTRY_VERSION = "deepseek-flash/1048576x393216/v1"
REGISTRY_VERSION = "deepseek-flash/1048576x393216/provider-prompt-v2"
LEGACY_COUNTER_ID = "canonical-http-json/v1"
COUNTER_ID = "deepseek-v41-flash1017/provider-prompt-v1"
LEGACY_TOKENIZER_PATH = Path(__file__).with_name("assets") / "deepseek-v4-tokenizer.json"
TOKENIZER_PATH = Path(__file__).with_name("assets") / "deepseek-v41-flash1017-tokenizer.json"
LEGACY_MODEL_BUDGET_SOURCE_HASHES = frozenset({
    "316ebc4abc075655c4f83dff97b13c54a43f0f55ab0ce0361e816a0f6bd5f59f",
    "1f02368a59dde5d1b7cecdfdb71530830f08b783dc21c7d72f0b92327488c21b",
})
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


def budget_contract(*, counter_id: str = COUNTER_ID) -> dict[str, str]:
    if counter_id == COUNTER_ID:
        registry, tokenizer = REGISTRY_VERSION, TOKENIZER_SHA256
    elif counter_id == LEGACY_COUNTER_ID:
        registry, tokenizer = LEGACY_REGISTRY_VERSION, LEGACY_TOKENIZER_SHA256
    else:
        raise TokenBudgetError("COUNTER_UNVERIFIED")
    return {"counter_id": counter_id, "policy_version": POLICY_VERSION,
            "registry_version": registry, "tokenizer_sha256": tokenizer}


def counter_for_registry(registry_version: str) -> str:
    if registry_version == REGISTRY_VERSION:
        return COUNTER_ID
    if registry_version == LEGACY_REGISTRY_VERSION:
        return LEGACY_COUNTER_ID
    raise TokenBudgetError("COUNTER_UNVERIFIED")


def frozen_budget_contract(source: bytes) -> dict[str, str]:
    """Select replay semantics from frozen code, never self-reported metadata."""
    if not isinstance(source, bytes):
        raise TokenBudgetError("COUNTER_UNVERIFIED")
    observed = hashlib.sha256(source).hexdigest()
    if observed in LEGACY_MODEL_BUDGET_SOURCE_HASHES:
        return budget_contract(counter_id=LEGACY_COUNTER_ID)
    try:
        current = Path(__file__).read_bytes()
    except OSError:
        raise TokenBudgetError("COUNTER_UNVERIFIED") from None
    if observed != hashlib.sha256(current).hexdigest():
        raise TokenBudgetError("COUNTER_UNVERIFIED")
    return budget_contract()


@lru_cache(maxsize=2)
def _tokenizer(counter_id: str = COUNTER_ID) -> Tokenizer:
    metadata = budget_contract(counter_id=counter_id)
    path = LEGACY_TOKENIZER_PATH if counter_id == LEGACY_COUNTER_ID else TOKENIZER_PATH
    try:
        raw = path.read_bytes()
    except OSError:
        raise TokenBudgetError("TOKENIZER_UNAVAILABLE") from None
    if hashlib.sha256(raw).hexdigest() != metadata["tokenizer_sha256"]:
        raise TokenBudgetError("TOKENIZER_INTEGRITY_MISMATCH")
    try:
        return Tokenizer.from_str(raw.decode("utf-8"))
    except Exception:
        raise TokenBudgetError("TOKENIZER_LOAD_FAILED") from None


def count_json_tokens(value: object, *, counter_id: str = COUNTER_ID) -> int:
    """Count complete JSON request input using its explicit framing contract.

    The historical public name is retained for the existing dispatcher and
    quota callers. Only the explicit legacy counter tokenizes raw HTTP JSON.
    """
    text = canonical_json(value)
    tokenizer = _tokenizer(counter_id)
    if counter_id == COUNTER_ID:
        from .provider_prompt import render_prompt

        try:
            text = render_prompt(value)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, IndexError,
                AssertionError, RecursionError):
            raise TokenBudgetError("COUNTER_UNVERIFIED") from None
    try:
        return len(tokenizer.encode(text, add_special_tokens=counter_id == LEGACY_COUNTER_ID).ids)
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


def plan_request(request_without_max_tokens: dict[str, Any], *,
                 counter_id: str = COUNTER_ID) -> tuple[dict[str, Any], int, int]:
    """Return complete request, versioned input count, and legal output ceiling."""
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
        input_tokens = count_json_tokens(request, counter_id=counter_id)
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


def verify_request_budget(request: dict[str, Any], *, expected_request_sha256: str,
                          input_tokens: int, output_ceiling: int,
                          registry_version: str, tokenizer_sha256: str) -> None:
    """Reject final-wire rewrites before any network side effect.

    The caller supplies the immutable IO-input admission, not values derived
    again from a possibly changed mutable request.
    """
    counter_id = counter_for_registry(registry_version)
    metadata = budget_contract(counter_id=counter_id)
    if (metadata["tokenizer_sha256"] != tokenizer_sha256
            or not isinstance(request, dict)
            or hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()
                != expected_request_sha256):
        raise TokenBudgetError("REQUEST_BUDGET_CHANGED")
    rebuilt, count, ceiling = plan_request(
        {key: value for key, value in request.items() if key != "max_tokens"},
        counter_id=counter_id)
    if (type(input_tokens) is not int or type(output_ceiling) is not int
            or count != input_tokens or ceiling != output_ceiling or rebuilt != request):
        raise TokenBudgetError("REQUEST_BUDGET_CHANGED")


def tokenizer_receipt(*, counter_id: str = COUNTER_ID) -> dict[str, Any]:
    _tokenizer(counter_id)
    contract = budget_contract(counter_id=counter_id)
    return {
        "sha256": contract["tokenizer_sha256"],
        "context_tokens": CONTEXT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "policy_version": POLICY_VERSION,
        "registry_version": contract["registry_version"],
        "counter_id": counter_id,
    }
