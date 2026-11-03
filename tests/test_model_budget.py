"""Official tokenizer budgeting and no-small-cap contracts."""
from __future__ import annotations

import pytest

import trading_system.llm.model_budget as budget


def _request(text: str = "public evidence") -> dict:
    return {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": text}],
        "stream": False,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
    }


def test_short_request_gets_the_complete_legal_output_window():
    request, input_tokens, ceiling = budget.plan_request(_request())
    assert request["max_tokens"] == ceiling == budget.MAX_OUTPUT_TOKENS
    assert input_tokens == budget.count_json_tokens(request)
    assert input_tokens + ceiling <= budget.CONTEXT_TOKENS
    assert ceiling > 16_384


def test_budget_shrinks_to_exact_remaining_window(monkeypatch):
    monkeypatch.setattr(budget, "CONTEXT_TOKENS", 512)
    monkeypatch.setattr(budget, "MAX_OUTPUT_TOKENS", 256)
    request, input_tokens, ceiling = budget.plan_request(_request())
    assert ceiling == min(256, 512 - input_tokens)
    assert request["max_tokens"] == ceiling
    assert input_tokens + ceiling <= 512


def test_legal_window_does_not_fail_when_serialized_limit_changes_token_width(monkeypatch):
    core = _request()
    short = budget.count_json_tokens({**core, "max_tokens": 999})
    long = budget.count_json_tokens({**core, "max_tokens": 1000})
    assert long == short + 1
    monkeypatch.setattr(budget, "CONTEXT_TOKENS", short + 1000)

    wire, actual_input, output = budget.plan_request(core)
    assert wire["max_tokens"] == output == 999
    assert actual_input == short and actual_input + output <= budget.CONTEXT_TOKENS
    assert long + 1000 > budget.CONTEXT_TOKENS


def test_shorter_output_field_can_leave_one_legal_output_token(monkeypatch):
    core = _request()
    short = budget.count_json_tokens({**core, "max_tokens": 1})
    assert budget.count_json_tokens({**core, "max_tokens": budget.MAX_OUTPUT_TOKENS}) == short + 1
    monkeypatch.setattr(budget, "CONTEXT_TOKENS", short + 1)
    wire, input_tokens, output = budget.plan_request(core)
    assert (wire["max_tokens"], input_tokens, output) == (1, short, 1)


def test_oversized_input_and_caller_caps_fail_closed(monkeypatch):
    monkeypatch.setattr(budget, "CONTEXT_TOKENS", 64)
    monkeypatch.setattr(budget, "MAX_OUTPUT_TOKENS", 32)
    with pytest.raises(budget.TokenBudgetError, match="INPUT_CONTEXT_EXCEEDED"):
        budget.plan_request(_request("public evidence " * 100))
    monkeypatch.setattr(budget, "CONTEXT_TOKENS", 1_048_576)
    monkeypatch.setattr(budget, "MAX_OUTPUT_TOKENS", 393_216)
    with pytest.raises(budget.TokenBudgetError, match="CALLER_OUTPUT_CAP_FORBIDDEN"):
        budget.plan_request({**_request(), "max_tokens": 2000})


def test_pinned_tokenizer_receipt_is_exact():
    receipt = budget.tokenizer_receipt()
    assert receipt == {
        "sha256": "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614",
        "context_tokens": 1_048_576,
        "max_output_tokens": 393_216,
        "policy_version": "workloom.text-primary/v1",
        "registry_version": "deepseek-flash/1048576x393216/v1",
    }
