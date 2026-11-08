"""V3 inline clean data protocol, native checkpoint and lossless replay.

The fixed function is an output container, never an executable tool. Native
arguments are durably recorded before JSON parsing. Original final acceptance
and keyed coverage are both checked; no value, ID, default or null is repaired.
Historical content-only v1/v2 transport and replay remain separate.
"""
from __future__ import annotations

from copy import deepcopy
import math
import uuid


def _safe_native_value(client, value, depth=0):
    """Apply opaque-string credential protection to every key and value.

    Native tool metadata is untrusted too. Encoded secrets in an invalid tool
    name, ID, extra key or nested value must not reach disk before rejection.
    Any changed projection is explicitly redacted and never accepted/retried.
    """
    from .deepseek_required import _fail

    if depth > 64:
        # Preserve known usage and its durable rejected native reference even
        # when invalid metadata is too deep to inspect safely. The opaque
        # marker is explicitly redacted and cannot authorize success/retry.
        return "[REDACTED_MODEL_CONTENT]", True
    if isinstance(value, str):
        try:
            value.encode("utf-8", "strict")
        except UnicodeError:
            return "[REDACTED_MODEL_CONTENT]", True
        return client._safe_native_content(value)
    if isinstance(value, float) and not math.isfinite(value):
        return "[REDACTED_MODEL_CONTENT]", True
    if isinstance(value, list):
        result, redacted = [], False
        for item in value:
            safe, changed = _safe_native_value(client, item, depth + 1)
            result.append(safe)
            redacted = redacted or changed
        return result, redacted
    if isinstance(value, dict):
        result, redacted = {}, False
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("INVALID_JSON_OBJECT_KEY")
            safe_key, key_changed = _safe_native_value(client, key, depth + 1)
            safe_value, changed = _safe_native_value(client, item, depth + 1)
            if safe_key in result:
                # A redaction collision is rejected opaque evidence, never an
                # overwritten native key that could become a valid response.
                return "[REDACTED_MODEL_CONTENT]", True
            result[safe_key] = safe_value
            redacted = redacted or key_changed or changed
        return result, redacted
    return value, False


def replay_tool_attempt(value, inputs):
    """Recompute the business projection from the saved original arguments."""
    from ..redline.model_diagnostics import RequiredModelUnavailable
    from .clean_keyed_codec import CONTRACT as CLEAN_CONTRACT, build_inline_schema, flatten_strict_output
    from .deepseek_required import (DeepSeekRequiredClient, MODEL, CONTEXT_TOKENS,
                                   _json_loads, validate_schema)
    from .json_observation import TOOL_CONTRACT, staged_diagnostic, validate_attempt
    from .model_budget import CLEAN_TOOL_FUNCTION

    if (inputs.get("response_observation_contract") != TOOL_CONTRACT
            or inputs.get("clean_protocol") != {"contract": CLEAN_CONTRACT}
            or inputs.get("stage") != "clean.llm_semantic"
            or inputs.get("thinking_mode") != "disabled"
            or inputs.get("endpoint") != "https://api.deepseek.com/beta/chat/completions"):
        raise ValueError("Strict clean protocol differs from its admitted request")
    value = validate_attempt(value, inputs)
    if value["response"] is None:
        return {"parsed": None, "native_parsed": None, "diagnostic": value["transport_diagnostic"],
                "usage": None, "retry_eligible": False}
    response = value["response"]
    if (set(response) != {"id", "object", "model", "system_fingerprint", "choices", "usage"}
            or response["model"] != MODEL or response["object"] != "chat.completion"):
        raise ValueError("Strict clean native envelope differs")
    DeepSeekRequiredClient._identifier(response["id"])
    usage = DeepSeekRequiredClient._usage(response["usage"], "enabled")
    if usage != response["usage"]:
        raise ValueError("Strict clean usage was not normalized")
    parsed = native_parsed = diagnostic = None
    arguments = None
    try:
        if value["reasoning_state"] == "invalid":
            raise RequiredModelUnavailable("INVALID_REASONING_CONTENT")
        if value["reasoning_state"] == "present":
            raise RequiredModelUnavailable("NON_THINKING_REASONING_CONTENT")
        DeepSeekRequiredClient._usage(usage, "disabled")
        if usage["completion_tokens"] > inputs["output_ceiling"] or usage["total_tokens"] > CONTEXT_TOKENS:
            raise RequiredModelUnavailable("MODEL_USAGE_EXCEEDS_BUDGET")
        if not value["fingerprint_valid"]:
            raise RequiredModelUnavailable("UNSAFE_RESPONSE_FINGERPRINT")
        if response["system_fingerprint"] is not None:
            DeepSeekRequiredClient._identifier(response["system_fingerprint"])
        choices = response["choices"]
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise RequiredModelUnavailable("INVALID_CHOICES")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise RequiredModelUnavailable("MODEL_OUTPUT_TRUNCATED")
        message = choice.get("message")
        if (set(choice) != {"index", "finish_reason", "message"}
                or type(choice["index"]) is not int or choice["index"] != 0
                or choice["finish_reason"] != "tool_calls" or not isinstance(message, dict)
                or set(message) != {"role", "content", "tool_calls"}
                or message["role"] != "assistant" or message["content"] not in (None, "")
                or not isinstance(message["tool_calls"], list) or len(message["tool_calls"]) != 1):
            raise RequiredModelUnavailable("INCOMPLETE_MODEL_RESPONSE")
        tool = message["tool_calls"][0]
        if (not isinstance(tool, dict) or set(tool) not in (
                {"id", "type", "function"}, {"index", "id", "type", "function"})
                or "index" in tool and (type(tool["index"]) is not int or tool["index"] != 0)
                or tool["type"] != "function" or not isinstance(tool["function"], dict)
                or set(tool["function"]) != {"name", "arguments"}
                or tool["function"]["name"] != CLEAN_TOOL_FUNCTION):
            raise RequiredModelUnavailable("INCOMPLETE_MODEL_RESPONSE")
        DeepSeekRequiredClient._identifier(tool["id"])
        arguments = tool["function"]["arguments"]
        if value["content_redacted"]:
            # A redacted native string is retained only as rejected evidence.
            # Parsing it could otherwise transform an unsafe reply into success.
            raise RequiredModelUnavailable("RESPONSE_OBJECT_MISMATCH")
        if not isinstance(arguments, str) or not arguments.strip():
            raise RequiredModelUnavailable("EMPTY_MODEL_OUTPUT")
        native_parsed = _json_loads(arguments)
        if not isinstance(native_parsed, dict) or not native_parsed:
            raise RequiredModelUnavailable("EMPTY_OR_NONOBJECT_MODEL_OUTPUT")
        validate_schema(native_parsed, build_inline_schema(inputs["output_schema"]))
        parsed = flatten_strict_output(native_parsed, inputs["output_schema"], validate=validate_schema)
    except RequiredModelUnavailable as error:
        diagnostic = staged_diagnostic(error, "assistant_content")
    eligible = (diagnostic is not None and diagnostic["code"] in {
        "INVALID_RESPONSE_JSON", "EMPTY_MODEL_OUTPUT", "OUTPUT_SCHEMA_MISMATCH"}
        and isinstance(arguments, str) and value["content_redacted"] is False)
    return {"parsed": parsed if diagnostic is None else None,
            "native_parsed": native_parsed if diagnostic is None else None,
            "diagnostic": diagnostic, "usage": usage, "retry_eligible": eligible}


def complete_tool_json(client, *, system, user, schema_hint, max_tokens,
                       temperature, schema, public_payload, clean_protocol):
    """Dispatch a fixed same-wire request with at most one global reanswer."""
    from ..redline.model_diagnostics import RequiredModelUnavailable
    from .clean_keyed_codec import CONTRACT as CLEAN_CONTRACT
    from .deepseek_required import (BASE_URL, MODEL, RELEASE, SEARCH_MODE, _fail, _hash)
    from .json_observation import (TOOL_CONTRACT, contract_schemas, staged_diagnostic,
                                   validate_reference_observation, validate_transport)
    from .model_budget import (POLICY_VERSION, REGISTRY_VERSION, TOKENIZER_SHA256,
                               TokenBudgetError, verify_request_budget)
    from .scene_policy import KERNEL_SCENE_MAP

    if (client._response_observation_contract != TOOL_CONTRACT or client._stage() != "clean.llm_semantic"
            or clean_protocol != {"contract": CLEAN_CONTRACT}):
        _fail("TRUSTED_CONFIGURATION_MISMATCH")
    if max_tokens is not None:
        _fail("CALLER_OUTPUT_CAP_FORBIDDEN")
    try:
        request, input_tokens, ceiling = client.plan_chat(system=system, user=user,
            schema_hint=schema_hint, schema=schema, temperature=temperature,
            public_payload=public_payload, clean_protocol=clean_protocol)
    except TokenBudgetError as error:
        _fail(error.code)
    schemas = contract_schemas(TOOL_CONTRACT)
    stage, call_id = client._stage(), uuid.uuid4().hex
    identity_hook = getattr(client.execution, "model_call_id", None)
    if identity_hook is not None:
        if not callable(identity_hook):
            _fail("TRUSTED_CONFIGURATION_MISMATCH")
        call_id = identity_hook(call_id)
        if (not isinstance(call_id, str) or len(call_id) != 32
                or any(character not in "0123456789abcdef" for character in call_id)):
            _fail("TRUSTED_CONFIGURATION_MISMATCH")
    inputs = {"schema": schemas["request"], "response_observation_contract": TOOL_CONTRACT,
        "clean_protocol": deepcopy(clean_protocol), "call_id": call_id, "stage": stage,
        "provider": "deepseek", "method": "POST", "endpoint": BASE_URL + "/beta/chat/completions",
        "request": request, "request_sha256": _hash(request), "output_schema": schema,
        "schema_sha256": _hash(schema), "release": RELEASE, "scene": KERNEL_SCENE_MAP[stage],
        "thinking_mode": "disabled", "reasoning_effort": None,
        "input_token_estimate": input_tokens, "output_ceiling": ceiling,
        "policy_version": POLICY_VERSION, "registry_version": REGISTRY_VERSION,
        "tokenizer_sha256": TOKENIZER_SHA256}
    observation = {"schema": TOOL_CONTRACT, "call_id": call_id,
                   "request_sha256": inputs["request_sha256"], "attempts": [], "retry_used": False}

    def record(ordinal, *, native=None, error=None):
        transport = client._last_chat_transport
        if transport is None:
            transport = {"http_status": None, "body_sha256": None, "body_bytes": None,
                "body_whitespace_only": None, "body_complete": False,
                "final_response_url": None, "redirected": None}
        transport = validate_transport(transport)
        diagnostic = staged_diagnostic(error, "http_envelope", transport) if error is not None else None
        attempt = {"schema": schemas["attempt"], "call_id": call_id, "ordinal": ordinal,
            "request_sha256": inputs["request_sha256"], "transport": transport,
            "response": None, "response_sha256": None, "content_redacted": None,
            "reasoning_state": None, "fingerprint_valid": None, "transport_diagnostic": diagnostic}
        if native is not None:
            choice = native["choices"][0]
            message = choice["message"]
            safe_choice, redacted = _safe_native_value(client, {
                "index": choice.get("index"), "finish_reason": choice.get("finish_reason"),
                "message": {key: message.get(key) for key in ("role", "content", "tool_calls")}})
            reasoning = message.get("reasoning_content")
            reasoning_state = ("absent" if reasoning is None else "invalid" if not isinstance(reasoning, str)
                               else "present" if reasoning.strip() else "empty")
            fingerprint, fingerprint_valid = native.get("system_fingerprint"), True
            if fingerprint is not None:
                try:
                    fingerprint = client._identifier(fingerprint)
                    if client._api_key in fingerprint:
                        _fail("UNSAFE_RESPONSE_FINGERPRINT")
                except RequiredModelUnavailable:
                    fingerprint, fingerprint_valid = None, False
            response = {"id": native["id"], "object": native["object"], "model": native["model"],
                "system_fingerprint": fingerprint, "choices": [safe_choice],
                "usage": client._usage(native.get("usage"), "enabled")}
            attempt.update(response=response, response_sha256=_hash(response), content_redacted=redacted,
                           reasoning_state=reasoning_state, fingerprint_valid=fingerprint_valid)
        reference = client.execution.model_attempt_snapshot(attempt)
        entry = {"ordinal": ordinal, "observation_ref": reference, "diagnostic": diagnostic}
        observation["attempts"].append(entry)
        observation["retry_used"] = len(observation["attempts"]) == 2
        return attempt, entry

    def invoke():
        for ordinal in (1, 2):
            from .resume_native import cached_attempt
            cached = cached_attempt(client.execution, ordinal, inputs, observation)
            if cached is None:
                client._last_chat_transport = None
                try:
                    try:
                        verify_request_budget(request, expected_request_sha256=inputs["request_sha256"],
                            input_tokens=input_tokens, output_ceiling=ceiling,
                            registry_version=inputs["registry_version"], tokenizer_sha256=inputs["tokenizer_sha256"])
                    except TokenBudgetError as error:
                        _fail(error.code)
                    native, body_hash = client._post_json("/beta/chat/completions", request)
                    if native.get("model") != MODEL:
                        _fail("RESPONSE_MODEL_MISMATCH")
                    if native.get("object") != "chat.completion":
                        _fail("RESPONSE_OBJECT_MISMATCH")
                    response_id = client._identifier(native.get("id"))
                    if client._api_key in response_id or response_id in client._observed_response_ids:
                        _fail("RESPONSE_IDENTITY_REUSED_OR_UNSAFE")
                    choices = native.get("choices")
                    if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)
                            or not isinstance(choices[0].get("message"), dict)
                            or choices[0]["message"].get("role") != "assistant"
                            or choices[0]["message"].get("function_call")):
                        _fail("INCOMPLETE_MODEL_RESPONSE")
                    client._usage(native.get("usage"), "enabled")
                    client._observed_response_ids.add(response_id)
                except RequiredModelUnavailable as error:
                    _, entry = record(ordinal, error=error)
                    raise client._observation_failure(entry["diagnostic"], observation) from None
                attempt, entry = record(ordinal, native=native)
            else:
                attempt, entry = cached
                response = attempt["response"]
                response_id = None if response is None else response["id"]
                body_hash = attempt["transport"]["body_sha256"]
            verdict = replay_tool_attempt(attempt, inputs)
            entry["diagnostic"] = verdict["diagnostic"]
            if verdict["diagnostic"] is not None:
                if ordinal == 1 and verdict["retry_eligible"] and client.execution.claim_model_format_retry():
                    continue
                raise client._observation_failure(verdict["diagnostic"], observation) from None
            response = attempt["response"]
            return {"schema": schemas["call"], "response_observation_contract": TOOL_CONTRACT,
                "clean_protocol": deepcopy(clean_protocol), "call_id": call_id, "stage": stage,
                "provider": "deepseek", "requested_model": MODEL, "response_id": response_id,
                "model": MODEL, "release": RELEASE, "usage": verdict["usage"], "scene": KERNEL_SCENE_MAP[stage],
                "thinking_mode": "disabled", "reasoning_effort": None,
                "input_token_estimate": input_tokens, "output_ceiling": ceiling,
                "policy_version": POLICY_VERSION, "registry_version": REGISTRY_VERSION,
                "tokenizer_sha256": TOKENIZER_SHA256, "finish_reason": "tool_calls",
                "request_sha256": inputs["request_sha256"], "response_sha256": _hash(response),
                "transport_response_sha256": body_hash, "schema_sha256": inputs["schema_sha256"],
                "response": response, "system_fingerprint": response["system_fingerprint"],
                "search_mode": SEARCH_MODE, "native_parsed": verdict["native_parsed"],
                "parsed": verdict["parsed"], "response_observation": validate_reference_observation(observation)}
        raise AssertionError("Strict clean reanswer exceeded its fixed bound")

    proof = client.execution.call_io("model.deepseek.chat", inputs, invoke)
    client.receipts.append({key: value for key, value in proof.items()
                           if key not in {"schema", "parsed", "native_parsed", "response"}})
    return proof["parsed"]
