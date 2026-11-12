"""Semantic contracts, boundary cases and hostile-output rejection; no model IO."""
from __future__ import annotations

import copy

import pytest

from trading_system.llm.deepseek_required import validate_schema
from trading_system.llm.required_schemas import (
    clean_schema, decision_schema, narrative_schema, risk_schema, sentiment_schema,
)
from trading_system.redline import LLMUnavailable


DOCS = ["doc-1", "doc-2"]
CHAINS = ["memory", "logic"]
ETFS = ["XLK", "SMH"]
SYMBOLS = ["AAPL", "MSFT"]
MARKET_IDS = ["md_" + symbol + "_20261023_" + "a" * 24 for symbol in SYMBOLS]


def examples():
    clean = {"documents": [{"id": doc, "tickers": ["AAPL"], "sentiment": "bullish",
                            "sentiment_score": 0.4, "events": ["earnings"],
                            "summary_zh": "财报显示收入增长。", "relevance": 0.8} for doc in DOCS]}
    sentiment = {"chains": [{"chain_id": chain, "heat": 5, "sentiment_score": 0.2,
                              "narrative_change": "stable", "key_drivers": ["Public filing"],
                              "evidence_ids": [DOCS[0]]} for chain in CHAINS]}
    risk = {"assessment": "No additional evidenced alerts.", "alerts": [{
        "severity": 3, "chain_id": "memory", "link": "upstream", "type": "capacity",
        "headline_zh": "产能预期仍有不确定性。", "transmission": ["Supplier revenue"],
        "evidence_ids": [DOCS[0]]}]}
    narrative = {"sectors": [{"etf": etf, "narrative_score": 6,
                              "eps_revision_bias": "up", "guidance_tone": "positive",
                              "evidence": ["Issuer guidance improved."],
                              "evidence_ids": [DOCS[1]]} for etf in ETFS]}
    decision = {"action": "BUY", "action_summary": "Public evidence supports a bounded paper candidate.",
                "decisions": [{"symbol": "AAPL", "action": "buy", "action_summary": "Candidate review",
                               "why_this": "Issuer evidence", "why_now": "Recent release",
                               "entry_condition": "Wait for the approved setup",
                               "invalidation": "Issuer retracts guidance",
                               "risk_note": "Liquidity and gap risk", "source_ids": [MARKET_IDS[0], DOCS[0]]}]}
    return {"clean": (clean_schema(DOCS), clean),
            "sentiment": (sentiment_schema(CHAINS, DOCS), sentiment),
            "risk": (risk_schema(CHAINS, DOCS), risk),
            "narrative": (narrative_schema(ETFS, DOCS), narrative),
            "decision": (decision_schema(SYMBOLS, DOCS, MARKET_IDS), decision)}


ROWS = {"clean": "documents", "sentiment": "chains", "risk": "alerts",
        "narrative": "sectors", "decision": "decisions"}


@pytest.mark.parametrize("name", list(ROWS))
def test_valid_stage_output_and_exact_closed_keys(name):
    schema, value = examples()[name]
    validate_schema(value, schema)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    row = schema["properties"][ROWS[name]]["items"]
    assert row["additionalProperties"] is False
    assert set(row["required"]) == set(row["properties"])


@pytest.mark.parametrize("name", list(ROWS))
@pytest.mark.parametrize("location", ["top", "row"])
def test_extra_output_fields_fail(name, location):
    schema, value = examples()[name]
    target = value if location == "top" else value[ROWS[name]][0]
    target["invented"] = "not allowed"
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)


MISSING = [
    ("clean", field) for field in ("id", "tickers", "sentiment", "sentiment_score", "events", "summary_zh", "relevance")
] + [
    ("sentiment", field) for field in ("chain_id", "heat", "sentiment_score", "narrative_change", "key_drivers", "evidence_ids")
] + [
    ("risk", field) for field in ("severity", "chain_id", "link", "type", "headline_zh", "transmission", "evidence_ids")
] + [
    ("narrative", field) for field in ("etf", "narrative_score", "eps_revision_bias", "guidance_tone", "evidence", "evidence_ids")
] + [
    ("decision", field) for field in ("symbol", "action", "action_summary", "why_this", "why_now", "entry_condition", "invalidation", "risk_note", "source_ids")
]


@pytest.mark.parametrize("name,field", MISSING)
def test_missing_semantic_field_fails(name, field):
    schema, value = examples()[name]
    del value[ROWS[name]][0][field]
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)


@pytest.mark.parametrize("name,field,bad", [
    ("clean", "id", "unseen-doc"), ("clean", "sentiment", "positive"),
    ("clean", "events", ["invented-event"]), ("clean", "tickers", ["<script>"]),
    ("sentiment", "chain_id", "invented"), ("sentiment", "narrative_change", "up"),
    ("risk", "chain_id", "invented"), ("risk", "link", "middle"),
    ("risk", "type", "rumor"), ("narrative", "etf", "SPY"),
    ("narrative", "eps_revision_bias", "positive"), ("narrative", "guidance_tone", "up"),
    ("decision", "symbol", "UNSEEN"), ("decision", "action", "sell"),
    ("sentiment", "evidence_ids", ["unseen-doc"]),
    ("risk", "evidence_ids", ["unseen-doc"]),
    ("narrative", "evidence_ids", ["unseen-doc"]),
    ("decision", "source_ids", ["unseen-doc"]),
])
def test_unregistered_entities_and_enums_fail(name, field, bad):
    schema, value = examples()[name]
    value[ROWS[name]][0][field] = bad
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)


@pytest.mark.parametrize("name,field,bad", [
    ("clean", "sentiment_score", -1.01), ("clean", "sentiment_score", 1.01),
    ("clean", "relevance", -0.01), ("clean", "relevance", 1.01),
    ("sentiment", "heat", 10.01), ("sentiment", "heat", -0.01),
    ("sentiment", "sentiment_score", -1.01),
    ("risk", "severity", 0.99), ("risk", "severity", 10.01),
    ("narrative", "narrative_score", -0.01), ("narrative", "narrative_score", 10.01),
    ("clean", "sentiment_score", float("nan")),
    ("sentiment", "heat", float("inf")), ("risk", "severity", float("nan")),
    ("narrative", "narrative_score", float("-inf")),
    ("clean", "relevance", True), ("sentiment", "heat", True),
    ("risk", "severity", True), ("narrative", "narrative_score", True),
])
def test_no_numeric_defaulting_or_clamping(name, field, bad):
    schema, value = examples()[name]
    value[ROWS[name]][0][field] = bad
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)


@pytest.mark.parametrize("bad", ["", " ", "<script>text</script>", "hidden\x00text", "line\nnext",
                                 "hidden\x85text", "spoof\u202etext"])
@pytest.mark.parametrize("name,field", [("clean", "summary_zh"), ("risk", "headline_zh"),
                                       ("decision", "why_this")])
def test_plain_text_required_without_html_or_control_characters(name, field, bad):
    schema, value = examples()[name]
    value[ROWS[name]][0][field] = bad
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)


@pytest.mark.parametrize("name", ["clean", "sentiment", "narrative"])
def test_complete_declared_array_length_required(name):
    schema, value = examples()[name]
    value[ROWS[name]].pop()
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)


def test_legitimate_model_no_alert_result_is_valid():
    validate_schema({"assessment": "No public evidence of a qualifying risk.", "alerts": []},
                    risk_schema(CHAINS, DOCS))


def test_global_chain_tickers_are_annotations_without_expanding_us_trade_authority():
    schema, value = examples()["clean"]
    value["documents"][0]["tickers"] = ["005930.KS", "000660.KS", "2330.TW", "TSM"]
    validate_schema(value, schema)
    with pytest.raises(ValueError):
        decision_schema(["005930.KS"], DOCS, MARKET_IDS[:1])


def test_no_candidates_allows_only_empty_decisions_with_real_explanation():
    schema = decision_schema([], DOCS, [])
    validate_schema({"action": "WAIT", "action_summary": "No supported candidate in this evidence set.",
                     "decisions": []}, schema)
    value = examples()["decision"][1]
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)


@pytest.mark.parametrize("text", [" Waiting", "Waiting ", "等待　", "\ufeff等待"])
def test_decision_text_must_satisfy_the_website_plain_text_contract(text):
    schema, value = examples()["decision"]
    value["decisions"][0]["why_now"] = text
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)


@pytest.mark.parametrize("action", ["HOLD", "WATCH", "SELL"])
def test_top_action_has_no_legacy_or_publisher_alias(action):
    schema, value = examples()["decision"]
    value["action"] = action
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)


@pytest.mark.parametrize("factory,args", [
    (clean_schema, ([],)), (sentiment_schema, (CHAINS, [])), (risk_schema, (CHAINS, [])),
    (narrative_schema, (ETFS, [])), (decision_schema, (SYMBOLS, [], MARKET_IDS)),
    (clean_schema, (["doc-1", "doc-1"],)), (clean_schema, (["<unsafe>"],)),
    (sentiment_schema, ([], DOCS)), (risk_schema, ([], DOCS)),
    (narrative_schema, ([], DOCS)), (decision_schema, (["<unsafe>"], DOCS, MARKET_IDS)),
    (decision_schema, (SYMBOLS, DOCS, [])),
    (decision_schema, (SYMBOLS, DOCS, MARKET_IDS[:1])),
    (decision_schema, (SYMBOLS, DOCS, [MARKET_IDS[0], MARKET_IDS[0]])),
    (decision_schema, (SYMBOLS, DOCS, [MARKET_IDS[0], MARKET_IDS[1].replace("MSFT", "QQQ")])),
    (decision_schema, (SYMBOLS, DOCS, [MARKET_IDS[0], "invented-market-id"])),
    (decision_schema, (SYMBOLS, DOCS + MARKET_IDS[:1], MARKET_IDS)),
])
def test_invalid_input_identity_sets_do_not_create_fake_schema(factory, args):
    with pytest.raises(ValueError):
        factory(*args)


def test_builders_are_pure_and_do_not_mutate_input_sets():
    arguments = [DOCS[:], CHAINS[:], ETFS[:], SYMBOLS[:], MARKET_IDS[:]]
    before = copy.deepcopy(arguments)
    clean_schema(arguments[0]); sentiment_schema(arguments[1], arguments[0])
    risk_schema(arguments[1], arguments[0]); narrative_schema(arguments[2], arguments[0])
    decision_schema(arguments[3], arguments[0], arguments[4])
    assert arguments == before


def test_market_only_watch_is_valid_but_buy_needs_at_least_two_citations():
    schema, value = examples()["decision"]
    row = value["decisions"][0]
    row["action"], row["source_ids"] = "watch", MARKET_IDS[:1]
    value["action"] = "WAIT"
    validate_schema(value, schema)
    row["action"] = "buy"
    value["action"] = "BUY"
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)


@pytest.mark.parametrize("references", [[], [MARKET_IDS[0]] * 2, [MARKET_IDS[0]] + DOCS * 5])
def test_decision_citation_array_is_nonempty_unique_and_bounded(references):
    schema, value = examples()["decision"]
    value["decisions"][0]["source_ids"] = references
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)
