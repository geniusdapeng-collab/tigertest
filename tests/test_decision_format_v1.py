"""MC-007 final decision citation contract."""
from __future__ import annotations

from copy import deepcopy

import pytest

from trading_system.llm.deepseek_required import validate_schema
from trading_system.llm.intelligence_required import (
    DECISION_FORMAT_CONTRACT, chat_arguments,
)
from trading_system.llm.required_schemas import decision_schema
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


MARKET_NVDA = "md_NVDA_20261020_" + "a" * 24
MARKET_AMD = "md_AMD_20261020_" + "b" * 24


def schema():
    return decision_schema(
        ["NVDA", "AMD"], ["doc-nvda", "doc-amd"], [MARKET_NVDA, MARKET_AMD],
        company_sources={"NVDA": ["doc-nvda"], "AMD": ["doc-amd"]},
        format_contract=DECISION_FORMAT_CONTRACT,
    )


def decision(symbol, action, source_ids, **extra):
    return {
        "symbol": symbol,
        "action": action,
        "action_summary": "证据约束下继续观察。",
        "why_this": "主题与流动性候选。",
        "why_now": "等待证据和风控同时满足。",
        "entry_condition": "仅在完整证据和数值风控通过后重新研究。",
        "invalidation": "证据失效或风控条件变化。",
        "risk_note": "不构成真实交易指令。",
        "source_ids": source_ids,
        **extra,
    }


def output(*rows):
    return {
        "action": "WAIT",
        "action_summary": "等待，不以缺失证据替代买入条件。",
        "decisions": list(rows),
    }


def test_decision_format_v1_prompt_is_versioned_and_explicit():
    target_schema = schema()
    arguments = chat_arguments(
        "decision.debate",
        {
            "approved_picks": [],
            "watch_candidates": [{"symbol": "NVDA"}, {"symbol": "AMD"}],
            "numeric_action": "WAIT",
            "mrs": {}, "sectors": [], "rejections": [], "risk_rationale": {},
            "market_evidence": [], "company_evidence": {},
            "market_asof": {},
        },
        target_schema,
        {
            "run_date": "2026-10-20", "market_timezone": "America/New_York",
            "decision_format_contract": DECISION_FORMAT_CONTRACT,
        },
    )
    assert DECISION_FORMAT_CONTRACT in arguments["user"]
    assert arguments["public_payload"]["decision_format_contract"] == DECISION_FORMAT_CONTRACT
    for phrase in (
        "恰好一次引用该symbol自己的market evidence ID",
        "watch可以只含该市场ID",
        "buy还必须含至少一个同一证券的frozen company evidence ID",
        "不得引用其他证券",
    ):
        assert phrase in arguments["user"]


def test_v3_decision_prompt_bytes_do_not_receive_v4_decision_contract():
    target_schema = schema()
    arguments = chat_arguments(
        "decision.debate",
        {
            "approved_picks": [], "watch_candidates": [], "numeric_action": "WAIT",
            "mrs": {}, "sectors": [], "rejections": [], "risk_rationale": {},
            "market_evidence": [], "company_evidence": {}, "market_asof": {},
        },
        target_schema,
        {"run_date": "2026-10-20", "market_timezone": "America/New_York"},
    )
    assert DECISION_FORMAT_CONTRACT not in arguments["user"]
    assert "decision_format_contract" not in arguments["public_payload"]
    legacy_schema = decision_schema(
        ["NVDA", "AMD"], ["doc-nvda", "doc-amd"], [MARKET_NVDA, MARKET_AMD],
        company_sources={"NVDA": ["doc-nvda"], "AMD": ["doc-amd"]},
    )
    validate_schema(
        output(decision("NVDA", "watch", [MARKET_NVDA, "doc-nvda"])),
        legacy_schema,
    )


def test_watch_uses_exactly_its_own_market_id_and_no_allocation_fields():
    target = output(decision("NVDA", "watch", [MARKET_NVDA]))
    validate_schema(target, schema())
    with pytest.raises(RequiredModelUnavailable):
        validate_schema(output(decision("NVDA", "watch", [MARKET_NVDA], entry_price=100)), schema())
    with pytest.raises(RequiredModelUnavailable):
        validate_schema(output(decision("NVDA", "watch", [MARKET_NVDA, "doc-nvda"])), schema())


def test_cross_symbol_market_or_company_citation_is_rejected():
    with pytest.raises(RequiredModelUnavailable):
        validate_schema(output(decision("AMD", "watch", [MARKET_NVDA])), schema())
    with pytest.raises(RequiredModelUnavailable):
        validate_schema(output(decision("AMD", "watch", [MARKET_AMD, "doc-nvda"])), schema())


def test_buy_requires_owned_company_original_and_its_market_id():
    valid = decision("NVDA", "buy", [MARKET_NVDA, "doc-nvda"])
    validate_schema(output(valid), schema())
    with pytest.raises(RequiredModelUnavailable):
        validate_schema(output(decision("NVDA", "buy", [MARKET_NVDA])), schema())
    with pytest.raises(RequiredModelUnavailable):
        validate_schema(output(decision("NVDA", "buy", [MARKET_AMD, "doc-nvda"])), schema())
