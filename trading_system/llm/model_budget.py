"""Exact DeepSeek Flash request budgeting with the official tokenizer.

The model window is shared by fully serialized input and generated output.
Callers therefore never choose a small arbitrary completion cap: this module
assigns the complete legal remaining output window after tokenizing the exact
request bytes that will be sent over HTTP.
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
                      thinking_mode: str, effort: str | None, temperature: float) -> dict:
    """Build the fixed Tiger text-only wire request before planning its window."""
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
