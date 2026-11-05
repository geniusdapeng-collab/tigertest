"""Closed schemas for evidence-bound, model-required semantic stages.

Builders describe allowed output only. They do not infer, repair, clamp,
default or authorize a trading action. Cross-item coverage and action
authority are checked by the execution owner after schema validation.
"""
from __future__ import annotations

import re
import math

from ..search.ontology import (
    ASSESSMENT_STATUSES, CLAIM_KINDS, CLAIM_RELATIONS, EVENT_IDENTIFIER_SCHEMES, EVENT_TYPES, IDENTIFIER_SCHEMES,
    LEGACY_ANNOTATION_EVENT_TYPES, PERIOD_KINDS, SUBJECT_TYPES, TIME_PRECISIONS,
)

PLAIN_TEXT_PATTERN = r"^(?!\s*$)[^<>\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]+$"
PUBLIC_TEXT_PATTERN = r"^(?![\s\ufeff])(?!.*[\s\ufeff]$)" + PLAIN_TEXT_PATTERN[1:]
SYMBOL_PATTERN = r"^[A-Z][A-Z0-9.\-]{0,14}$"
TICKER_PATTERN = r"^[A-Z0-9][A-Z0-9.\-]{0,14}$"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_CHAIN_PATTERN = r"^[A-Za-z][A-Za-z0-9._:-]{0,63}$"
_EVENTS = list(LEGACY_ANNOTATION_EVENT_TYPES)
V2_MAX_DOCUMENTS = 20_000


def _ids(values, *, allow_empty: bool = False, symbols: bool = False,
         chains: bool = False, maximum: int = 1000) -> list[str]:
    if type(maximum) is not int or not 1 <= maximum <= V2_MAX_DOCUMENTS:
        raise ValueError("MODEL_SCHEMA_INVALID_ID_LIMIT")
    if not isinstance(values, (list, tuple)) or len(values) > maximum or not values and not allow_empty:
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
    if not doc_ids:
        return _array({"type": "string", "pattern": r"(?!)"}, 0, 0, unique=True)
    return _array(_enum(doc_ids), 1, 8, unique=True)


def clean_schema(doc_ids, *, structured: bool = False, target_ids=None) -> dict:
    docs = _ids(doc_ids, allow_empty=structured, maximum=V2_MAX_DOCUMENTS if structured else 1000)
    document = _object(
        id=_enum(docs) if docs else {"type": "string", "pattern": r"(?!)"},
        tickers=_array({"type": "string", "pattern": TICKER_PATTERN}, 0, 8, unique=True),
        sentiment=_enum(["bullish", "bearish", "neutral", "mixed"]),
        sentiment_score=_number(-1, 1),
        events=_array(_enum(_EVENTS), 0, 6, unique=True),
        summary_zh=_text(600), relevance=_number(0, 1),
    )
    if structured:
        document["properties"]["events"] = _array(_enum(list(EVENT_TYPES)), 0, 6, unique=True)
        document["properties"].update(_structured_fields(target_ids))
        document["required"] = list(document["properties"])
    return _object(documents=_array(document, len(docs), len(docs), unique=True))


def sentiment_schema(chain_ids, doc_ids, *, allow_empty=False, target_sources=None,
                     target_coverage=None) -> dict:
    chains, docs = _ids(chain_ids, chains=True), _ids(doc_ids, allow_empty=allow_empty,
        maximum=V2_MAX_DOCUMENTS if target_sources is not None else 1000)
    if target_sources is not None:
        return _target_sentiment(chains, docs, target_sources, target_coverage)
    chain = _object(
        chain_id=_enum(chains), heat=_number(0, 10), sentiment_score=_number(-1, 1),
        narrative_change=_enum(["improving", "stable", "deteriorating"]),
        key_drivers=_array(_text(600), 1, 5, unique=True), evidence_ids=_evidence(docs),
    )
    if not docs:
        chain["properties"].update(heat={"const": 5}, sentiment_score={"const": 0},
                                   narrative_change={"const": "stable"})
    return _object(chains=_array(chain, len(chains), len(chains), unique=True))


def risk_schema(chain_ids, doc_ids, *, allow_empty=False, target_sources=None,
                target_coverage=None) -> dict:
    chains, docs = _ids(chain_ids, chains=True), _ids(doc_ids, allow_empty=allow_empty,
        maximum=V2_MAX_DOCUMENTS if target_sources is not None else 1000)
    if target_sources is not None:
        return _target_risk(chains, docs, target_sources, target_coverage)
    allowed = chains if "unknown" in chains else [*chains, "unknown"]
    alert = _object(
        severity=_number(1, 10), chain_id=_enum(allowed),
        link=_enum(["upstream", "midstream", "downstream", "unknown"]),
        type=_enum(["supply_chain", "policy", "patent", "capacity", "other"]),
        headline_zh=_text(400), transmission=_array(_text(300), 1, 6, unique=True),
        evidence_ids=_evidence(docs),
    )
    return _object(assessment=_text(1000), alerts=_array(alert, 0, 20 if docs else 0, unique=True))


def narrative_schema(etfs, doc_ids, *, allow_empty=False, target_sources=None,
                     target_coverage=None) -> dict:
    sectors, docs = _ids(etfs, symbols=True), _ids(doc_ids, allow_empty=allow_empty,
        maximum=V2_MAX_DOCUMENTS if target_sources is not None else 1000)
    if target_sources is not None:
        return _target_narrative(sectors, docs, target_sources, target_coverage)
    sector = _object(
        etf=_enum(sectors), narrative_score=_number(0, 10),
        eps_revision_bias=_enum(["up", "flat", "down", "unknown"]),
        guidance_tone=_enum(["positive", "neutral", "negative", "unknown"]),
        evidence=_array(_text(600), 1, 3, unique=True), evidence_ids=_evidence(docs),
    )
    if not docs:
        sector["properties"].update(narrative_score={"const": 5},
                                    eps_revision_bias={"const": "unknown"},
                                    guidance_tone={"const": "unknown"})
    return _object(sectors=_array(sector, len(sectors), len(sectors), unique=True))


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _references(low: int = 0, high: int = 64) -> dict:
    return _array({"type": "string", "pattern": _ID_PATTERN}, low, high, unique=True)


def _structured_fields(target_ids) -> dict:
    target = ({"type": "string", "pattern": _ID_PATTERN} if target_ids is None else
              _enum(_ids(target_ids, allow_empty=True)))
    if target_ids is not None and not target_ids:
        target = {"type": "string", "pattern": r"(?!)"}
    span = _object(
        id={"type": "string", "pattern": _ID_PATTERN},
        start=_nullable({"type": "integer", "minimum": 0}),
        end=_nullable({"type": "integer", "minimum": 1}),
        quote={"type": "string", "minLength": 1, "maxLength": 8000},
    )
    identifier = _object(scheme=_enum(list(IDENTIFIER_SCHEMES)), value=_text(200),
                         span_ids=_references(1))
    subject = _object(
        id={"type": "string", "pattern": _ID_PATTERN}, type=_enum(list(SUBJECT_TYPES)),
        name=_text(300), identifiers=_array(identifier, 0, 12, unique=True),
        span_ids=_references(1),
    )
    time_value = _object(value=_nullable(_text(80)), precision=_enum(list(TIME_PRECISIONS)),
                         span_ids=_references())
    period = _object(kind=_enum(list(PERIOD_KINDS)), value=_nullable(_text(80)),
                     span_ids=_references())
    event = _object(
        id={"type": "string", "pattern": _ID_PATTERN}, event_type=_enum(list(EVENT_TYPES)),
        subject_ids=_references(1, 16), is_primary={"type": "boolean"},
        event_identifiers=_array(_object(scheme=_enum(list(EVENT_IDENTIFIER_SCHEMES)),
                                         value=_text(200), span_ids=_references(1)),
                                 0, 12, unique=True),
        report_period=period, event_time=time_value, effective_time=time_value,
        source_span_ids=_references(1), claim_kind=_enum(list(CLAIM_KINDS)),
        target_ids=_array(target, 0, 32, unique=True),
    )
    claim = _object(
        id={"type": "string", "pattern": _ID_PATTERN},
        event_id={"type": "string", "pattern": _ID_PATTERN},
        predicate=_text(200), value=_nullable(_text(300)), unit=_nullable(_text(100)),
        currency=_nullable(_text(100)), source_span_ids=_references(1),
        relation=_enum(list(CLAIM_RELATIONS)), conflict_group=_nullable(_text(300)),
        supersedes_claim_id=_nullable(_text(300)),
    )
    return {"subjects": _array(subject, 0, 64, unique=True),
            "evidence_spans": _array(span, 0, 128, unique=True),
            "event_records": _array(event, 0, 64, unique=True),
            "claims": _array(claim, 0, 128, unique=True)}


def _target_sources(targets, docs, source_map) -> dict[str, list[str]]:
    if not isinstance(source_map, dict) or set(source_map) != set(targets):
        raise ValueError("MODEL_SCHEMA_TARGET_COVERAGE_REQUIRED")
    allowed = set(docs)
    sources = {}
    for target in targets:
        values = _ids(source_map[target], allow_empty=True, maximum=V2_MAX_DOCUMENTS)
        if not set(values) <= allowed:
            raise ValueError("MODEL_SCHEMA_TARGET_SOURCE_UNKNOWN")
        sources[target] = values
    return sources


def _target_bindings(key, targets, sources, nullable_fields, target_coverage=None) -> dict:
    if target_coverage is not None and (not isinstance(target_coverage, dict)
                                      or set(target_coverage) != set(targets)):
        raise ValueError("MODEL_SCHEMA_TARGET_COVERAGE_REQUIRED")
    constraints = []
    for target in targets:
        owned = sources[target]
        references = (_enum(owned) if owned else {"type": "string", "pattern": r"(?!)"})
        properties = {key: {"const": target},
                      "evidence_ids": {"items": references, "maxItems": min(8, len(owned))}}
        if not owned:
            properties["status"] = {"const": "insufficient_evidence"}
        elif target_coverage is not None and target_coverage[target].get("status") == "conflicted":
            properties["status"] = {"const": "conflicted"}
        constraints.append({"properties": properties})
    nulls = {field: {"type": "null"} for field in nullable_fields}
    states = [
        {"properties": {"status": {"const": "assessed"}, "evidence_ids": {"minItems": 1},
                         **{field: {"type": ["string", "number"]} for field in nullable_fields}}},
        {"properties": {"status": {"const": "insufficient_evidence"}, **nulls}},
        {"properties": {"status": {"const": "conflicted"},
                         "evidence_ids": {"minItems": 1}, **nulls}},
    ]
    return {"allOf": [{"oneOf": constraints}, {"oneOf": states}]}


def _target_sentiment(chains, docs, source_map, target_coverage) -> dict:
    sources = _target_sources(chains, docs, source_map)
    row = _object(
        chain_id=_enum(chains), status=_enum(list(ASSESSMENT_STATUSES)),
        heat=_nullable(_number(0, 10)), sentiment_score=_nullable(_number(-1, 1)),
        narrative_change=_nullable(_enum(["improving", "stable", "deteriorating"])),
        key_drivers=_array(_text(600), 1, 5, unique=True),
        evidence_ids=_array(_enum(docs) if docs else {"type": "string", "pattern": r"(?!)"},
                            0, 8, unique=True),
    )
    row.update(_target_bindings("chain_id", chains, sources,
                               ("heat", "sentiment_score", "narrative_change"), target_coverage))
    return _object(chains=_array(row, len(chains), len(chains), unique=True))


def _target_narrative(sectors, docs, source_map, target_coverage) -> dict:
    sources = _target_sources(sectors, docs, source_map)
    row = _object(
        etf=_enum(sectors), status=_enum(list(ASSESSMENT_STATUSES)),
        narrative_score=_nullable(_number(0, 10)),
        eps_revision_bias=_enum(["up", "flat", "down", "unknown"]),
        guidance_tone=_enum(["positive", "neutral", "negative", "unknown"]),
        evidence=_array(_text(600), 1, 3, unique=True),
        evidence_ids=_array(_enum(docs) if docs else {"type": "string", "pattern": r"(?!)"},
                            0, 8, unique=True),
    )
    row.update(_target_bindings("etf", sectors, sources, ("narrative_score",), target_coverage))
    row["allOf"].append({"oneOf": [
        {"properties": {"status": {"const": "assessed"}}},
        {"properties": {"status": {"enum": ["insufficient_evidence", "conflicted"]},
                         "eps_revision_bias": {"const": "unknown"},
                         "guidance_tone": {"const": "unknown"}}},
    ]})
    return _object(sectors=_array(row, len(sectors), len(sectors), unique=True))


def _target_risk(chains, docs, source_map, target_coverage) -> dict:
    sources = _target_sources(chains, docs, source_map)
    citation = _enum(docs) if docs else {"type": "string", "pattern": r"(?!)"}
    row = _object(chain_id=_enum(chains), status=_enum(list(ASSESSMENT_STATUSES)),
                  risk_level=_nullable(_number(0, 10)),
                  evidence_ids=_array(citation, 0, 8, unique=True))
    row.update(_target_bindings("chain_id", chains, sources, ("risk_level",), target_coverage))
    alert = _object(
        severity=_number(1, 10), chain_id=_enum(chains),
        link=_enum(["upstream", "midstream", "downstream", "unknown"]),
        type=_enum(["supply_chain", "policy", "patent", "capacity", "other"]),
        headline_zh=_text(400), transmission=_array(_text(300), 1, 6, unique=True),
        evidence_ids=_array(citation, 1, 8, unique=True),
    )
    alert["allOf"] = [{"oneOf": [
        {"properties": {"chain_id": {"const": chain},
                         "evidence_ids": {"items": _enum(owned) if owned else
                                          {"type": "string", "pattern": r"(?!)"}}}}
        for chain, owned in sources.items()
    ]}]
    return _object(assessment=_text(1000),
                   chain_assessments=_array(row, len(chains), len(chains), unique=True),
                   alerts=_array(alert, 0, 20 if docs else 0, unique=True))


def validate_target_assessments(stage: str, output: dict, targets, target_sources, *,
                                target_coverage=None) -> None:
    """Check complete target ownership and cross-row risk consistency.

    Known, evidenced alerts survive incomplete overall coverage. They do not
    authorize a complete risk score or a no-risk conclusion.
    """
    configuration = {
        "tech.sentiment": ("chains", "chain_id", ("heat", "sentiment_score", "narrative_change")),
        "sector.narrative": ("sectors", "etf", ("narrative_score",)),
        "tech.risk": ("chain_assessments", "chain_id", ("risk_level",)),
    }
    if stage not in configuration:
        raise ValueError("TARGET_ASSESSMENT_STAGE_INVALID")
    if not isinstance(target_sources, dict) or set(target_sources) != set(targets):
        raise ValueError("TARGET_ASSESSMENT_COVERAGE_INVALID")
    collection, key, score_fields = configuration[stage]
    try:
        rows = output[collection]
        if (not isinstance(rows, list) or len(rows) != len(targets)
                or {row[key] for row in rows} != set(targets)):
            raise ValueError("TARGET_ASSESSMENT_COVERAGE_INVALID")
        for row in rows:
            status, references = row["status"], row["evidence_ids"]
            owned = target_sources[row[key]]
            if (target_coverage is not None and target_coverage[row[key]].get("status") == "conflicted"
                    and status != "conflicted"):
                raise ValueError("TARGET_ASSESSMENT_CONFLICT_MUST_BE_NULL")
            if (status not in ASSESSMENT_STATUSES or not isinstance(references, list)
                    or len(references) > 8 or len(references) != len(set(references))
                    or not set(references) <= set(owned)):
                raise ValueError("TARGET_ASSESSMENT_SOURCE_INVALID")
            if status == "assessed":
                if not references or any(row[field] is None for field in score_fields):
                    raise ValueError("TARGET_ASSESSMENT_EVIDENCE_REQUIRED")
                for field in score_fields:
                    value = row[field]
                    if field == "narrative_change":
                        if value not in {"improving", "stable", "deteriorating"}:
                            raise ValueError("TARGET_ASSESSMENT_VALUE_INVALID")
                    elif (type(value) not in {int, float} or not math.isfinite(value)
                          or not (-1 if field == "sentiment_score" else 0) <= value <=
                          (1 if field == "sentiment_score" else 10)):
                        raise ValueError("TARGET_ASSESSMENT_VALUE_INVALID")
            elif any(row[field] is not None for field in score_fields):
                raise ValueError("TARGET_ASSESSMENT_UNKNOWN_MUST_BE_NULL")
            if status == "conflicted" and not references:
                raise ValueError("TARGET_ASSESSMENT_EVIDENCE_REQUIRED")
            if stage == "sector.narrative" and status != "assessed" and (
                    row.get("eps_revision_bias") != "unknown" or row.get("guidance_tone") != "unknown"):
                raise ValueError("TARGET_ASSESSMENT_UNKNOWN_MUST_BE_NULL")
            if not owned and status != "insufficient_evidence":
                raise ValueError("TARGET_ASSESSMENT_EVIDENCE_REQUIRED")
        if stage == "tech.risk":
            known = {target: 0.0 for target in targets}
            for alert in output["alerts"]:
                owner = alert["chain_id"]
                references = alert["evidence_ids"]
                severity = alert["severity"]
                if (owner not in known or not references
                        or len(references) > 8 or len(references) != len(set(references))
                        or type(severity) not in {int, float} or not math.isfinite(severity)
                        or not 1 <= severity <= 10
                        or not set(references) <= set(target_sources[owner])):
                    raise ValueError("TARGET_RISK_ALERT_SOURCE_INVALID")
                known[owner] = max(known[owner], alert["severity"])
            if any(row["status"] == "assessed" and row["risk_level"] != known[row[key]]
                   for row in rows):
                raise ValueError("TARGET_RISK_LEVEL_DIFFERS_FROM_ALERTS")
    except (KeyError, TypeError):
        raise ValueError("TARGET_ASSESSMENT_INVALID") from None


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
