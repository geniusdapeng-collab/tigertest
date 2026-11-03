"""Closed schemas for evidence-bound, model-required semantic stages.

Builders describe allowed output only. They do not infer, repair, clamp,
default or authorize a trading action. Cross-item coverage and action
authority are checked by the execution owner after schema validation.
"""
from __future__ import annotations

import re

PLAIN_TEXT_PATTERN = r"^(?!\s*$)[^<>\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]+$"
PUBLIC_TEXT_PATTERN = r"^(?![\s\ufeff])(?!.*[\s\ufeff]$)" + PLAIN_TEXT_PATTERN[1:]
SYMBOL_PATTERN = r"^[A-Z][A-Z0-9.\-]{0,14}$"
TICKER_PATTERN = r"^[A-Z0-9][A-Z0-9.\-]{0,14}$"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_CHAIN_PATTERN = r"^[A-Za-z][A-Za-z0-9._:-]{0,63}$"
_EVENTS = ["earnings", "guidance", "downgrade", "upgrade", "lawsuit",
           "supply_chain", "policy", "patent", "capacity", "macro", "other"]


def _ids(values, *, allow_empty: bool = False, symbols: bool = False,
         chains: bool = False) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > 1000 or not values and not allow_empty:
        raise ValueError("MODEL_SCHEMA_ID_SET_REQUIRED")
    pattern = SYMBOL_PATTERN if symbols else _CHAIN_PATTERN if chains else _ID_PATTERN
    if any(not isinstance(value, str) or re.fullmatch(pattern, value) is None for value in values):
        raise ValueError("MODEL_SCHEMA_INVALID_ID")
    if len(set(values)) != len(values):
        raise ValueError("MODEL_SCHEMA_DUPLICATE_ID")
    return list(values)


def _object(**properties) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _text(maximum: int, *, public: bool = False) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": maximum,
            "pattern": PUBLIC_TEXT_PATTERN if public else PLAIN_TEXT_PATTERN}


def _enum(values: list[str]) -> dict:
    return {"type": "string", "enum": list(values)}


def _number(low: float, high: float) -> dict:
    return {"type": "number", "minimum": low, "maximum": high}


def _array(items: dict, low: int, high: int, *, unique: bool = False) -> dict:
    return {"type": "array", "items": items, "minItems": low, "maxItems": high,
            "uniqueItems": unique}


def _evidence(doc_ids: list[str]) -> dict:
    return _array(_enum(doc_ids), 1, 8, unique=True)


def clean_schema(doc_ids) -> dict:
    docs = _ids(doc_ids)
    document = _object(
        id=_enum(docs),
        tickers=_array({"type": "string", "pattern": TICKER_PATTERN}, 0, 8, unique=True),
        sentiment=_enum(["bullish", "bearish", "neutral", "mixed"]),
        sentiment_score=_number(-1, 1),
        events=_array(_enum(_EVENTS), 0, 6, unique=True),
        summary_zh=_text(600), relevance=_number(0, 1),
    )
    return _object(documents=_array(document, len(docs), len(docs), unique=True))


def sentiment_schema(chain_ids, doc_ids) -> dict:
    chains, docs = _ids(chain_ids, chains=True), _ids(doc_ids)
    chain = _object(
        chain_id=_enum(chains), heat=_number(0, 10), sentiment_score=_number(-1, 1),
        narrative_change=_enum(["improving", "stable", "deteriorating"]),
        key_drivers=_array(_text(600), 1, 5, unique=True), evidence_ids=_evidence(docs),
    )
    return _object(chains=_array(chain, len(chains), len(chains), unique=True))


def risk_schema(chain_ids, doc_ids) -> dict:
    chains, docs = _ids(chain_ids, chains=True), _ids(doc_ids)
    allowed = chains if "unknown" in chains else [*chains, "unknown"]
    alert = _object(
        severity=_number(1, 10), chain_id=_enum(allowed),
        link=_enum(["upstream", "midstream", "downstream", "unknown"]),
        type=_enum(["supply_chain", "policy", "patent", "capacity", "other"]),
        headline_zh=_text(400), transmission=_array(_text(300), 1, 6, unique=True),
        evidence_ids=_evidence(docs),
    )
    return _object(assessment=_text(1000), alerts=_array(alert, 0, 20, unique=True))


def narrative_schema(etfs, doc_ids) -> dict:
    sectors, docs = _ids(etfs, symbols=True), _ids(doc_ids)
    sector = _object(
        etf=_enum(sectors), narrative_score=_number(0, 10),
        eps_revision_bias=_enum(["up", "flat", "down", "unknown"]),
        guidance_tone=_enum(["positive", "neutral", "negative", "unknown"]),
        evidence=_array(_text(600), 1, 3, unique=True), evidence_ids=_evidence(docs),
    )
    return _object(sectors=_array(sector, len(sectors), len(sectors), unique=True))


def decision_schema(symbols, doc_ids, market_ids, *, company_sources=None) -> dict:
    candidates = _ids(symbols, allow_empty=True, symbols=True)
    docs = _ids(doc_ids, allow_empty=company_sources is not None)
    markets = _ids(market_ids, allow_empty=True)
    if (set(markets) & set(docs) or len(markets) != len(candidates)
            or any(re.fullmatch(r"md_[A-Z][A-Z0-9.\-]{0,14}_[0-9]{8}_[0-9a-f]{24}", item) is None
                   for item in markets)
            or any(sum(item.startswith("md_" + symbol + "_") for item in markets) != 1
                   for symbol in candidates)):
        raise ValueError("MODEL_SCHEMA_MARKET_EVIDENCE_REQUIRED")
    symbol = _enum(candidates) if candidates else {"type": "string", "pattern": r"(?!)"}
    citation = _enum([*docs, *markets]) if docs or markets else {"type": "string", "pattern": r"(?!)"}
    decision = _object(
        symbol=symbol, action=_enum(["buy", "watch"]), action_summary=_text(300, public=True),
        why_this=_text(1200, public=True), why_now=_text(1200, public=True),
        entry_condition=_text(1200, public=True), invalidation=_text(1200, public=True),
        risk_note=_text(1200, public=True),
        source_ids=_array(citation, 1, 9, unique=True),
    )
    # A buy must carry at least its own market snapshot and one article. The
    # execution owner additionally verifies the exact citation types and owner.
    decision["allOf"] = [{"oneOf": [
        {"properties": {"action": {"const": "buy"}, "source_ids": {"minItems": 2}}},
        {"properties": {"action": {"const": "watch"}}},
    ]}]
    if company_sources is not None:
        if (set(company_sources) - set(candidates)
                or {doc for owned in company_sources.values() for doc in owned} != set(docs)):
            raise ValueError("MODEL_SCHEMA_COMPANY_EVIDENCE_OWNERSHIP_REQUIRED")
        if candidates:
            company_constraints = []
            for candidate in candidates:
                market = next(item for item in markets if item.startswith("md_" + candidate + "_"))
                owned = _ids(company_sources.get(candidate, []), allow_empty=True)
                company_constraints.append({"properties": {
                    "symbol": {"const": candidate},
                    "source_ids": {"items": _enum([market, *owned])},
                    "action": _enum(["buy", "watch"] if owned else ["watch"]),
                }})
            decision["allOf"].append({"oneOf": company_constraints})
    return _object(action=_enum(["BUY", "WAIT", "AVOID"]), action_summary=_text(1200, public=True),
                   decisions=_array(decision, 0, len(candidates), unique=True))
