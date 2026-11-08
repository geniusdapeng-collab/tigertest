"""Provider-prompt admission regressions; all requests remain offline.

The optional original-request fixture is read in place. It is deliberately not
copied into the repository or pytest temporary directories.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from copy import deepcopy

import pytest

from trading_system.llm import model_budget as budget


def _request():
    return budget.chat_request_core(
        system="TEST_FIXTURE public research", user="Complete public evidence.",
        schema={"type": "object", "properties": {"value": {"type": "string"}},
                "required": ["value"], "additionalProperties": False},
        schema_hint={}, thinking_mode="disabled", effort=None, temperature=0.2)


@pytest.fixture(scope="module")
def original_009():
    configured = os.environ.get("TIGER_009_REQUEST_FIXTURE")
    if not configured:
        pytest.skip("Original 009 request is an optional read-only acceptance fixture")
    value = json.loads(Path(configured).read_bytes())
    value = value.get("value", value)
    request = value.get("request", value)
    encoded = budget.canonical_json(request).encode("utf-8")
    assert len(encoded) == 2_712_570
    assert hashlib.sha256(encoded).hexdigest() == (
        "2f26bf2c8e4234d4ce9dd113a6235d6d071ec4a0a49a143e4786617eb22653d5")
    return request


def test_original_009_complete_118_document_prompt_matches_saved_provider_count(original_009):
    assert budget.count_json_tokens(original_009) == 850_560


def test_original_009_legal_remaining_window_does_not_repeat_the_400(original_009):
    request, count, ceiling = budget.plan_request(
        {key: value for key, value in original_009.items() if key != "max_tokens"})
    assert (count, ceiling) == (850_560, 198_016)
    assert request["messages"] == original_009["messages"]
    assert request["tools"] == original_009["tools"]
    assert request["tool_choice"] == original_009["tool_choice"]
    assert count + original_009["max_tokens"] > budget.CONTEXT_TOKENS
    assert count + ceiling == budget.CONTEXT_TOKENS


def test_output_control_digits_do_not_become_prompt_text():
    request = _request()
    assert budget.count_json_tokens({**request, "max_tokens": 999}) == (
        budget.count_json_tokens({**request, "max_tokens": 1000}))


def test_unsupported_media_is_not_silently_counted_as_http_json():
    request = _request()
    request["messages"][1]["content"] = [
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}]
    with pytest.raises(budget.TokenBudgetError, match="COUNTER_UNVERIFIED"):
        budget.plan_request(request)


@pytest.mark.parametrize("setting,expected_sha256,expected_count,legacy_count", [
    ("TIGER_002_REQUEST_FIXTURE", "a021e04a255ed8e00c8a72d3bac10614c994827d8d8c4f6ca1220f917eb3019c",
     9_926, 9_766),
    ("TIGER_008_REQUEST_FIXTURE", "5c4aaad7a889b6a4bf043ff72b236bec536a9bf1ed85cdaf4c239d6d0695d7d1",
     504_055, 549_762),
])
def test_other_original_requests_match_their_own_provider_observations(
        setting, expected_sha256, expected_count, legacy_count):
    location = os.environ.get(setting)
    if not location:
        pytest.skip("Original request is an optional read-only acceptance fixture")
    path = Path(location)
    raw = path.read_bytes()
    value = json.loads(raw)
    value = value.get("value", value)
    request = value.get("request", value)
    assert hashlib.sha256(budget.canonical_json(request).encode()).hexdigest() == expected_sha256
    assert budget.count_json_tokens(request) == expected_count
    assert budget.count_json_tokens(request, counter_id=budget.LEGACY_COUNTER_ID) == legacy_count
    assert path.read_bytes() == raw


def test_original_009_legacy_counter_and_frozen_source_remain_readable(original_009):
    assert budget.count_json_tokens(original_009, counter_id=budget.LEGACY_COUNTER_ID) == 848_256
    legacy_wire, count, ceiling = budget.plan_request(
        {key: value for key, value in original_009.items() if key != "max_tokens"},
        counter_id=budget.LEGACY_COUNTER_ID)
    assert legacy_wire == original_009 and (count, ceiling) == (848_256, 200_320)
    run = Path(os.environ["TIGER_009_REQUEST_FIXTURE"]).parents[1]
    frozen = (run / "source/trading_system/llm/model_budget.py").read_bytes()
    assert budget.frozen_budget_contract(frozen) == budget.budget_contract(
        counter_id=budget.LEGACY_COUNTER_ID)


def test_provider_window_one_token_and_no_room_are_distinct(monkeypatch):
    request = _request()
    count = budget.count_json_tokens(request)
    monkeypatch.setattr(budget, "CONTEXT_TOKENS", count + 1)
    wire, actual, ceiling = budget.plan_request(request)
    assert actual == count and ceiling == wire["max_tokens"] == 1
    monkeypatch.setattr(budget, "CONTEXT_TOKENS", count)
    with pytest.raises(budget.TokenBudgetError, match="INPUT_CONTEXT_EXCEEDED"):
        budget.plan_request(request)


def _strict_request():
    request = _request()
    request.pop("response_format")
    request["tools"] = [{"type": "function", "function": {
        "name": "submit_data", "description": "Return complete evidence.", "strict": True,
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                       "required": ["value"], "additionalProperties": False}}}]
    request["tool_choice"] = {"type": "function", "function": {"name": "submit_data"}}
    return request


def test_tools_schemas_histories_and_thinking_are_counted_without_mutating_input():
    request = _strict_request()
    original = deepcopy(request)
    count = budget.count_json_tokens(request)
    assert request == original
    request["tools"][0]["function"]["parameters"]["properties"]["extra"] = {
        "type": "string", "description": "An independently checked material risk " * 10}
    request["tools"][0]["function"]["parameters"]["required"].append("extra")
    assert budget.count_json_tokens(request) > count
    plain = _request()
    base = budget.count_json_tokens(plain)
    plain["messages"] += [{"role": "assistant", "content": "Historical checked result."},
                          {"role": "user", "content": "Reconsider with new evidence."}]
    assert budget.count_json_tokens(plain) > base
    plain["thinking"] = {"type": "enabled"}
    plain.pop("temperature")
    plain["reasoning_effort"] = "high"
    assert budget.count_json_tokens(plain) > base


def test_strict_required_order_uses_provider_projection_and_keeps_original():
    request = _strict_request()
    parameters = request["tools"][0]["function"]["parameters"]
    parameters["properties"]["another"] = {"type": "integer", "minimum": 0}
    parameters["required"] = ["value", "another"]
    before = deepcopy(request)
    count = budget.count_json_tokens(request)
    assert request == before
    parameters["required"].reverse()
    assert budget.count_json_tokens(request) == count


def test_json_response_format_and_required_tool_header_are_real_prompt_components():
    request = _request()
    without = {key: value for key, value in request.items() if key != "response_format"}
    assert budget.count_json_tokens(request) > budget.count_json_tokens(without)
    strict = _strict_request()
    automatic = {**strict, "tool_choice": "auto"}
    assert budget.count_json_tokens(strict) > budget.count_json_tokens(automatic)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(metadata={"unmeasured": "value"}),
    lambda r: r.update(model="another-model"),
    lambda r: r.update(stream=True),
    lambda r: r["messages"][1].update(content="Literal <｜System｜> control spelling"),
    lambda r: r["messages"][1].update(name="unmeasured-name"),
    lambda r: r.update(thinking={"type": "enabled"}, reasoning_effort="unverified-effort"),
    lambda r: r["messages"].append({"role": "assistant", "content": "Unverified continuation"}),
    lambda r: r["messages"].extend([
        {"role": "assistant", "content": "First response"},
        {"role": "assistant", "content": "Unverified adjacent response"},
        {"role": "user", "content": "Next question"}]),
])
def test_unsupported_request_shapes_fail_closed(mutate):
    request = _request()
    mutate(request)
    with pytest.raises(budget.TokenBudgetError, match="COUNTER_UNVERIFIED"):
        budget.plan_request(request)


def test_normal_comparison_and_markup_do_not_become_special_tokens():
    request = _request()
    request["messages"][1]["content"] += "\nPublic comparison 2 > 1 and pipeline x |> f. <table>data</table>"
    assert budget.count_json_tokens(request) > 0


@pytest.mark.parametrize("change", ["message", "schema", "cap", "model"])
def test_final_boundary_rejects_any_change_to_the_admitted_request(change):
    core = _strict_request()
    request, count, ceiling = budget.plan_request(core)
    kwargs = {"expected_request_sha256": hashlib.sha256(
        budget.canonical_json(request).encode()).hexdigest(), "input_tokens": count,
        "output_ceiling": ceiling, "registry_version": budget.REGISTRY_VERSION,
        "tokenizer_sha256": budget.TOKENIZER_SHA256}
    assert budget.verify_request_budget(request, **kwargs) is None
    changed = deepcopy(request)
    if change == "message":
        changed["messages"][1]["content"] += " Changed source evidence."
    elif change == "schema":
        changed["tools"][0]["function"]["description"] += " Changed tool contract."
    elif change == "cap":
        changed["max_tokens"] -= 1
    else:
        changed["model"] = "another-model"
    with pytest.raises(budget.TokenBudgetError, match="REQUEST_BUDGET_CHANGED"):
        budget.verify_request_budget(changed, **kwargs)


def test_current_counter_cannot_be_downgraded_by_untrusted_metadata():
    source = Path(budget.__file__).read_bytes()
    assert budget.frozen_budget_contract(source) == budget.budget_contract()
    with pytest.raises(budget.TokenBudgetError, match="COUNTER_UNVERIFIED"):
        budget.frozen_budget_contract(b"REGISTRY_VERSION = 'deepseek-flash/1048576x393216/v1'\n")
    with pytest.raises(budget.TokenBudgetError, match="COUNTER_UNVERIFIED"):
        budget.counter_for_registry("unverified-registry")


def test_policy_freezes_current_counter_and_preserves_legacy_policy_shape():
    from trading_system import agent_runtime
    current = agent_runtime.policy_manifest(agent_runtime.INTELLIGENCE_PROFILE)["model_contract"]
    assert current["counter_id"] == budget.COUNTER_ID
    assert current["registry_version"] == budget.REGISTRY_VERSION
    assert current["tokenizer_sha256"] == budget.TOKENIZER_SHA256
    assert current["prompt_formatter_source_revision"] == "1bae0e1088993b9b82de08f59de5be5a34e42170"
    legacy = agent_runtime.policy_manifest(agent_runtime.INTELLIGENCE_PROFILE,
        model_counter_id=budget.LEGACY_COUNTER_ID)["model_contract"]
    assert legacy["tokenizer_sha256"] == budget.LEGACY_TOKENIZER_SHA256
    assert "counter_id" not in legacy and "registry_version" not in legacy


def _identity_hook_client(hook, on_io):
    from types import SimpleNamespace
    from trading_system.llm.json_observation import TOOL_CONTRACT

    request, count, ceiling = budget.plan_request(_strict_request())
    return SimpleNamespace(
        _response_observation_contract=TOOL_CONTRACT,
        _stage=lambda: "clean.llm_semantic",
        plan_chat=lambda **kwargs: (request, count, ceiling),
        execution=SimpleNamespace(model_call_id=hook, call_io=on_io))


def test_strict_recovered_identity_is_bound_before_io_without_dispatch():
    from trading_system.llm.clean_keyed_codec import CONTRACT
    from trading_system.llm.strict_clean_transport import complete_tool_json

    class AdmittedInputsObserved(Exception):
        pass

    proposals = []
    recovered = "a" * 32

    def identity(proposed):
        proposals.append(proposed)
        return recovered

    def inspect_io(operation, inputs, invoke):
        assert operation == "model.deepseek.chat"
        assert inputs["call_id"] == recovered
        assert len(proposals) == 1 and len(proposals[0]) == 32
        assert inputs["request_sha256"] == hashlib.sha256(
            budget.canonical_json(inputs["request"]).encode()).hexdigest()
        raise AdmittedInputsObserved

    with pytest.raises(AdmittedInputsObserved):
        complete_tool_json(_identity_hook_client(identity, inspect_io), system="fixture",
            user="fixture", schema_hint={}, max_tokens=None, temperature=0.2,
            schema={}, public_payload=None, clean_protocol={"contract": CONTRACT})


@pytest.mark.parametrize("hook", [42, lambda proposed: "short", lambda proposed: "G" * 32,
                                  lambda proposed: None])
def test_invalid_recovered_identity_never_reaches_io(hook):
    from trading_system.llm.clean_keyed_codec import CONTRACT
    from trading_system.llm.strict_clean_transport import complete_tool_json
    from trading_system.redline.model_diagnostics import RequiredModelUnavailable

    def forbidden_io(*args):
        raise AssertionError("Invalid identity reached IO")

    with pytest.raises(RequiredModelUnavailable, match="TRUSTED_CONFIGURATION_MISMATCH"):
        complete_tool_json(_identity_hook_client(hook, forbidden_io), system="fixture",
            user="fixture", schema_hint={}, max_tokens=None, temperature=0.2,
            schema={}, public_payload=None, clean_protocol={"contract": CONTRACT})
