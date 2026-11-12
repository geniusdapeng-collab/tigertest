"""Pinned DeepSeek V4.1 text-request framing, without provider dispatch.

The unmodified MIT reference encoder is distributed beside the tokenizer.
Strict tool schemas follow the typed normalization of deepseek-recipe
935817c66f5d6e64838f419692545f06787e485f. Neither HTTP JSON control fields nor
an incident-specific token adjustment are prompt text. Unsupported protocol
shapes fail closed instead of reverting to a whole-HTTP-JSON estimate.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from types import ModuleType

ENCODER_PATH = Path(__file__).with_name("assets") / "deepseek-v41-flash1017-encoding.py"
ENCODER_SHA256 = "13231c592e958fb39e2af6001dae69d90e7a36b265702b9b390236fb387599e2"
SOURCE_REVISION = "1bae0e1088993b9b82de08f59de5be5a34e42170"
SOURCE_URL = ("https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/resolve/"
              + SOURCE_REVISION + "/encoding/encoding.py")
SCHEMA_SOURCE_URL = ("https://github.com/deepseek-ai/deepseek-recipe/blob/"
                    "935817c66f5d6e64838f419692545f06787e485f/"
                    "deepseek-recipe/src/protocol/client_tools.rs")
_CONTROL_SPELLING = re.compile(r"<[|｜]|</?think>")
_FIELDS = frozenset({"model", "messages", "stream", "response_format", "thinking",
    "reasoning_effort", "temperature", "max_tokens", "tools", "tool_choice"})


@lru_cache(maxsize=1)
def _encoder():
    raw = ENCODER_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ENCODER_SHA256:
        raise ValueError("Unverified provider formatter")
    module = ModuleType("tiger_pinned_deepseek_v41_encoder")
    exec(compile(raw, str(ENCODER_PATH), "exec"), module.__dict__)
    return module


def _plain_strings(value, depth=0):
    if depth > 64:
        raise ValueError("Unsupported provider value depth")
    if isinstance(value, str):
        value.encode("utf-8", "strict")
        # The reference string encoder recognizes added-token spellings.
        # Arbitrary source text containing them needs special-range-aware
        # encoding, which this admitted text-only protocol does not support.
        if _CONTROL_SPELLING.search(value):
            raise ValueError("Unsupported literal provider control token")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Unsupported provider object key")
            _plain_strings(key, depth + 1)
            _plain_strings(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _plain_strings(item, depth + 1)


def _normalize_schema(value, depth=0):
    """Render the documented typed, closed strict-schema subset in order.

    This is only the provider's prompt projection. The request and the complete
    local acceptance schema remain unchanged and retain all of their checks.
    """
    if depth > 48 or not isinstance(value, dict) or not value:
        raise ValueError("Unsupported strict schema")
    description = {"description": value["description"]} if "description" in value else {}
    if description and not isinstance(description["description"], str):
        raise ValueError("Unsupported strict schema description")
    if "anyOf" in value:
        if (set(value) - {"anyOf", "description"} or not isinstance(value["anyOf"], list)
                or not value["anyOf"]):
            raise ValueError("Unsupported strict schema union")
        return {"anyOf": [_normalize_schema(v, depth + 1) for v in value["anyOf"]],
                **description}
    kind = value.get("type")
    if isinstance(kind, list):
        if (set(value) - {"type", "description"} or len(kind) != 2 or "null" not in kind
                or any(t not in {"string", "number", "integer", "boolean", "null"} for t in kind)
                or len(set(kind)) != len(kind)):
            raise ValueError("Unsupported strict schema nullable type")
        return {"type": kind, **description}
    if kind == "object":
        properties = value.get("properties")
        required = value.get("required")
        if (set(value) - {"type", "properties", "additionalProperties", "required", "description"}
                or not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(v, str) for v in required)
                or len(set(required)) != len(required) or set(required) != set(properties)
                or value.get("additionalProperties") is not False):
            raise ValueError("Unsupported strict schema object")
        props = {key: _normalize_schema(child, depth + 1) for key, child in properties.items()}
        return {"type": kind, "properties": props, "additionalProperties": False,
                "required": list(props), **description}
    if kind == "array":
        if set(value) - {"type", "items", "description"} or "items" not in value:
            raise ValueError("Unsupported strict schema array")
        return {"type": kind, "items": _normalize_schema(value["items"], depth + 1),
                **description}
    fields = {"string": ("description", "pattern", "format", "enum"),
        "number": ("description", "const", "default", "minimum", "maximum",
                   "exclusiveMinimum", "exclusiveMaximum", "multipleOf"),
        "integer": ("description", "const", "default", "minimum", "maximum",
                    "exclusiveMinimum", "exclusiveMaximum", "multipleOf"),
        "boolean": ("description",), "null": ("description",)}
    if not isinstance(kind, str) or kind not in fields or set(value) - {"type", *fields[kind]}:
        raise ValueError("Unsupported strict schema type")
    return {"type": kind, **{key: value[key] for key in fields[kind] if key in value}}


def _messages(value):
    if not isinstance(value, list) or not value:
        raise ValueError("Unsupported provider messages")
    result = deepcopy(value)
    fields = {"system": {"role", "content"}, "user": {"role", "content"},
        "assistant": {"role", "content", "reasoning_content", "tool_calls"},
        "tool": {"role", "content", "tool_call_id"}}
    for msg in result:
        if (not isinstance(msg, dict) or not isinstance(msg.get("role"), str)
                or msg["role"] not in fields or set(msg) - fields[msg["role"]]
                or not isinstance(msg.get("content"), str)
                    and not (msg["role"] == "assistant" and msg.get("content") is None)):
            raise ValueError("Unsupported provider message shape")
        if "reasoning_content" in msg and not isinstance(msg["reasoning_content"], (str, type(None))):
            raise ValueError("Unsupported reasoning history")
        if msg["role"] == "tool" and not isinstance(msg.get("tool_call_id"), str):
            raise ValueError("Unsupported tool result identity")
        if "tool_calls" in msg:
            if not isinstance(msg["tool_calls"], list):
                raise ValueError("Unsupported tool-call history")
            for call in msg["tool_calls"]:
                if (not isinstance(call, dict) or set(call) != {"id", "type", "function"}
                        or call["type"] != "function" or not isinstance(call["id"], str)
                        or not isinstance(call["function"], dict)
                        or set(call["function"]) != {"name", "arguments"}
                        or any(not isinstance(v, str) for v in call["function"].values())):
                    raise ValueError("Unsupported tool-call history")
    # The pinned string reference inserts assistant headers at user/system
    # transitions. Prefix continuations and adjacent assistant messages need
    # the recipe's special-range renderer, so do not claim a count for them.
    if (result[0]["role"] not in {"system", "user"}
            or result[-1]["role"] == "assistant"
            or not any(msg["role"] == "user" for msg in result)):
        raise ValueError("Unsupported provider conversation boundary")
    for index, msg in enumerate(result):
        if msg["role"] == "assistant" and (
                index == 0 or result[index - 1]["role"] == "assistant"
                or index == 1 and result[0]["role"] == "system"):
            raise ValueError("Unsupported assistant transition")
    return result


def render_prompt(request):
    """Return the complete supported provider prompt, never an HTTP body."""
    if (not isinstance(request, dict) or set(request) - _FIELDS
            or request.get("model") != "deepseek-flash" or request.get("stream", False) is not False
            or request.get("thinking") not in ({"type": "disabled"}, {"type": "enabled"})):
        raise ValueError("Unsupported provider request")
    _plain_strings(request)
    # Reflect the object order decoded from Tiger's final canonical HTTP wire.
    request = json.loads(json.dumps(request, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False))
    messages = _messages(request.get("messages"))
    thinking = request["thinking"]["type"] == "enabled"
    effort = request.get("reasoning_effort")
    if (thinking and effort not in (None, "low", "high", "max")
            or not thinking and effort is not None):
        raise ValueError("Unsupported provider reasoning policy")
    tools = request.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("Unsupported provider tools")
    normalized = []
    for tool in tools:
        if (not isinstance(tool, dict) or set(tool) != {"type", "function"}
                or tool["type"] != "function" or not isinstance(tool["function"], dict)):
            raise ValueError("Unsupported provider tool")
        fn = tool["function"]
        if (set(fn) - {"name", "description", "parameters", "strict"}
                or not isinstance(fn.get("name"), str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", fn["name"]) is None
                or not isinstance(fn.get("description", ""), str) or fn.get("strict") is not True):
            raise ValueError("Unsupported provider tool schema mode")
        normalized.append({"type": "function", "function": {
            "name": fn["name"], "description": fn.get("description", ""),
            "parameters": _normalize_schema(fn.get("parameters"))}})
    names = [tool["function"]["name"] for tool in normalized]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate provider tool names")
    choice = request.get("tool_choice", "auto" if normalized else "none")
    required = choice == "required"
    if isinstance(choice, dict):
        if (set(choice) != {"type", "function"} or choice["type"] != "function"
                or not isinstance(choice["function"], dict) or set(choice["function"]) != {"name"}
                or choice["function"]["name"] not in names):
            raise ValueError("Unsupported named provider tool")
        normalized = [tool for tool in normalized
                      if tool["function"]["name"] == choice["function"]["name"]]
        required = True
    elif choice not in ("none", "auto", "required"):
        raise ValueError("Unsupported provider tool choice")
    if thinking and required or required and not normalized:
        raise ValueError("Unsupported required tool policy")
    if choice == "none":
        normalized = []
    response_format = request.get("response_format")
    if response_format not in (None, {"type": "text"}, {"type": "json_object"}):
        raise ValueError("Unsupported provider response format")
    if normalized or response_format == {"type": "json_object"}:
        if messages[0]["role"] != "system":
            messages.insert(0, {"role": "system", "content": ""})
        if normalized:
            messages[0]["tools"] = normalized
        if response_format == {"type": "json_object"}:
            messages[0]["response_format"] = response_format
    prompt = _encoder().encode_messages(messages,
        thinking_mode="thinking" if thinking else "chat", reasoning_effort=effort)
    if required:
        prompt += "\n\n<｜DSML｜ calls>\n"
    return prompt
