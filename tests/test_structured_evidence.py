"""Typed source, target ownership and risk regressions; no network/model I/O."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json

import pytest

from trading_system.llm.deepseek_required import validate_schema
from trading_system.llm.required_schemas import (
    clean_schema, narrative_schema, risk_schema, sentiment_schema,
    validate_target_assessments,
)
from trading_system.redline import LLMUnavailable
from trading_system.search.event_identity import (
    EVENT_CONTRACT, STRUCTURED_EVENT_CONTRACT, target_evidence_sources,
    target_evidence_coverage, validate_events, validate_structured_events,
)
from trading_system.search.models import CleanDocument, RawDocument, StructuredDocument
from trading_system.search.ontology import EVENT_TYPES, canonical_event_type
from trading_system.tech_chain.agents import (
    ChainMonitorRow, RiskAlert, StructuredSentimentRow,
)
from trading_system.tech_chain.fusion import (
    StructuredTechChainSignal, TechChainFusionAgent, signals_to_bonus_map,
)
from trading_system.tech_chain.risk_evidence import TARGET_CONTRACT, risk_coverage


CUTOFF = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)
REGISTRY = {
    "securities": {"AAA": "listed:alpha"},
    "entities": {"issuer:alpha": {
        "type": "issuer", "name": "Alpha Corporation", "aliases": ["Alpha"],
        "identifiers": {"ticker": ["AAA"], "cik": ["0000000123"]},
        "security_ids": ["listed:alpha"],
    }},
}


def _time():
    return {"value": None, "precision": "unknown", "span_ids": []}


def _doc(key="a", host="reuters.com", *, name="Alpha Corporation", symbol="AAA",
         period="2026-09-30", words="first", target="memory"):
    heading = name + (f" ({symbol})" if symbol else "")
    report = f"Reporting period ended {period}."
    tail = ("Customers described an improvement in orders after reviewing capacity at independent plants."
            if words == "first" else
            "Market analysts separately studied invoiced sales and cash receipts from regional buyers.")
    body = heading + "\n" + report + "\n" + tail
    raw = RawDocument(key, "deepseek_search", heading + " earnings", f"https://{host}/{key}",
                      body, "2026-11-02T14:30:00Z", CUTOFF.timestamp() - 60,
                      {"text_kind": "html_main", "completeness": "complete",
                       "intelligence_document_id": "document:" + key,
                       "intelligence_version": hashlib.sha256(body.encode()).hexdigest(),
                       "source_times": {"published": {"value": "2026-11-02T14:30:00Z",
                                                       "precision": "instant"}}})
    spans = [{"id": "s1", "start": None, "end": None, "quote": heading},
             {"id": "s2", "start": None, "end": None, "quote": report},
             {"id": "fact", "start": None, "end": None, "quote": tail}]
    subjects = [{"id": "subject", "type": "issuer", "name": name,
                 "identifiers": ([{"scheme": "ticker", "value": symbol, "span_ids": ["s1"]}]
                                 if symbol else []), "span_ids": ["s1"]}]
    events = [{"id": "event", "event_type": "earnings", "subject_ids": ["subject"],
               "is_primary": True, "event_identifiers": [],
               "report_period": {"kind": "period_end", "value": period, "span_ids": ["s2"]},
               "event_time": _time(), "effective_time": _time(),
               "source_span_ids": ["s1", "s2", "fact"], "claim_kind": "reported_fact",
               "target_ids": [target]}]
    claims = [{"id": "claim", "event_id": "event", "predicate": "operating_conditions",
               "value": None, "unit": None, "currency": None, "source_span_ids": ["fact"],
               "relation": "supports", "conflict_group": None, "supersedes_claim_id": None}]
    return StructuredDocument(raw, tickers=[symbol] if symbol else [], events=["earnings"],
                              summary_zh="已获取原始报道。", relevance=.8,
                              subjects=subjects, evidence_spans=spans, event_records=events, claims=claims)


def _pair(**kwargs):
    return [_doc(**kwargs), _doc("b", "bloomberg.com", words="second", **kwargs)]


def _validate(docs, **kwargs):
    return validate_structured_events(docs, CUTOFF, entity_registry=REGISTRY, **kwargs)


def _event(doc):
    return doc.eligibility["events"][0]


def _policy(source_id, host, scope):
    return {"schema": "tiger.source-registry/v1", "policy_version": "intelligence/v1",
            "sources": {source_id: {"source_id": source_id, "version": "1", "enabled": True,
                       "kind": source_id, "allowed_hosts": [host], "authority_scopes": [scope]}}}


def _authority(doc, source_id, scope, *, issuer=None, series=None, proof_type="official_release",
               **proof_fields):
    doc.raw.meta["source_policy_id"] = source_id
    doc.raw.meta["authority"] = {
        "scope": scope, "issuer_id": issuer, "series_ids": series or [], "proof_type": proof_type,
        "proof_ref": {"source_id": source_id, "source_url": doc.raw.url,
                      "response_sha256": "a" * 64,
                      "content_sha256": hashlib.sha256(doc.raw.content.encode()).hexdigest(),
                      **proof_fields},
    }


def test_old_dataclass_and_default_schema_shapes_stay_frozen():
    raw = RawDocument("old", "demo", "Old", "https://example.com", "Old body", "")
    assert set(asdict(CleanDocument(raw))) == {
        "raw", "tickers", "sentiment", "sentiment_score", "events", "summary_zh",
        "relevance", "degraded", "evidence", "corroborated",
    }
    old = clean_schema(["old"])["properties"]["documents"]["items"]["properties"]
    assert "subjects" not in old and "mna" not in old["events"]["items"]["enum"]
    new = clean_schema(["old"], structured=True)["properties"]["documents"]["items"]["properties"]
    assert set(new) - set(old) == {"subjects", "evidence_spans", "event_records", "claims"}
    assert "mna" in new["events"]["items"]["enum"] == list(EVENT_TYPES)
    assert canonical_event_type("lawsuit") == "litigation"
    assert canonical_event_type("merger") == "mna"
    assert risk_coverage(0) == {"contract": "tiger.tech-risk-evidence.v1",
                                "scorable_documents": 0, "status": "insufficient_evidence"}


def test_full_text_span_bound_independent_event_is_target_scoped():
    docs = _pair()
    stats = _validate(docs)
    assert stats["corroborated"] == 2 and stats["scorable_events"] == 2
    assert stats["event_identity"]["contract"] == STRUCTURED_EVENT_CONTRACT
    assert target_evidence_sources(docs, ["memory", "energy"]) == {"memory": ["a", "b"], "energy": []}
    assert _event(docs[0])["subject_ids"] == ["issuer:alpha"]
    assert _event(docs[0])["allowed_uses"] == ["background", "analysis", "scoring"]
    assert docs[0].eligibility["resolved_spans"]["s1"]["start"] == 0


def test_opposite_claim_relations_conflict_without_a_model_declared_group():
    docs = _pair()
    docs[1].claims[0]["relation"] = "refutes"
    assert all(doc.claims[0]["conflict_group"] is None for doc in docs)
    _validate(docs)
    claims = [doc.eligibility["claims"][0] for doc in docs]
    assert all(claim["status"] == "conflicted" and claim["conflict_ids"] for claim in claims)
    assert claims[0]["conflict_ids"] == claims[1]["conflict_ids"]
    coverage = target_evidence_coverage(docs, ["memory"])
    assert coverage["memory"]["status"] == "conflicted"
    assert coverage["memory"]["source_ids"] == ["a", "b"]
    sources = target_evidence_sources(docs, ["memory"])
    output = {"chains": [{"chain_id": "memory", "status": "assessed", "heat": 6,
        "sentiment_score": .2, "narrative_change": "improving", "key_drivers": ["Unresolved contrary report"],
        "evidence_ids": ["a", "b"]}]}
    with pytest.raises(ValueError, match="CONFLICT_MUST_BE_NULL"):
        validate_target_assessments("tech.sentiment", output, ["memory"], sources, target_coverage=coverage)


def test_explicit_v2_dispatch_never_replaces_default_v1_contract():
    docs = _pair()
    result = validate_events(docs, CUTOFF, contract=STRUCTURED_EVENT_CONTRACT, entity_registry=REGISTRY)
    assert result["event_identity"]["contract"] == STRUCTURED_EVENT_CONTRACT
    old = CleanDocument(RawDocument("old", "demo", "Background", "https://example.com",
                                     "Not a typed annotation", "2026-11-02T14:30:00Z"))
    assert validate_events([old], CUTOFF)["event_identity"]["contract"] == EVENT_CONTRACT
    with pytest.raises(ValueError, match="Structured"):
        _validate([old])


@pytest.mark.parametrize("case,status", [
    ("missing_quote", "unverified_event"), ("wrong_offsets", "unverified_event"),
    ("duplicate_quote", "unverified_event"), ("unknown_subject", "unverified_event"),
    ("unquoted_period", "unverified_event"), ("missing_scope", "unverified_event"),
    ("late_version", "version_unverified_at_cutoff"), ("unknown_publication", "unknown_publication"),
    ("future_publication", "future_publication"), ("date_only_today", "unknown_publication"),
    ("snippet", "original_body_unverified"), ("partial_body", "original_body_unverified"),
    ("other", "background_claim"), ("rumor", "background_claim"),
    ("unrelated_target", "corroborated"),
])
def test_missing_or_unproven_evidence_never_becomes_eligible(case, status):
    docs = _pair()
    doc = docs[0]
    if case == "missing_quote":
        doc.evidence_spans[0]["quote"] = "Model invented a company name"
    elif case == "wrong_offsets":
        doc.evidence_spans[0].update(start=1, end=len(doc.evidence_spans[0]["quote"]) + 1)
    elif case == "duplicate_quote":
        doc.raw = replace(doc.raw, content=doc.raw.content + "\n" + doc.evidence_spans[0]["quote"])
    elif case == "unknown_subject":
        doc.event_records[0]["subject_ids"] = ["not-supplied"]
    elif case == "unquoted_period":
        doc.event_records[0]["report_period"]["value"] = "2026-06-30"
    elif case == "missing_scope":
        doc.event_records[0]["source_span_ids"] = ["s1"]
    elif case == "late_version":
        doc.raw = replace(doc.raw, fetched_at=CUTOFF.timestamp() + 1)
    elif case == "unknown_publication":
        doc.raw.meta["source_times"]["published"].update(value=None, precision="unknown")
        doc.raw = replace(doc.raw, published="")
    elif case == "future_publication":
        doc.raw.meta["source_times"]["published"]["value"] = "2026-11-04T14:00:01Z"
    elif case == "date_only_today":
        doc.raw.meta["source_times"]["published"].update(value="2026-11-04T00:00:00Z", precision="date")
    elif case == "snippet":
        doc.raw.meta["text_kind"] = "snippet"
    elif case == "partial_body":
        doc.raw.meta["completeness"] = "partial"
    elif case == "other":
        for item in docs:
            item.event_records[0]["event_type"] = "other"
    elif case == "rumor":
        for item in docs:
            item.event_records[0]["claim_kind"] = "rumor"
    elif case == "unrelated_target":
        for item in docs:
            item.event_records[0]["target_ids"] = ["energy"]
    stats = _validate(docs)
    assert stats["event_identity"]["documents"][doc.raw.doc_id]["status"] == (
        "eligible" if case == "unrelated_target" else status)
    assert target_evidence_sources(docs, ["memory"])["memory"] == []
    if status == "unverified_event":
        assert _event(doc)["reason"]


def test_exact_duplicate_and_syndicated_origins_never_supply_two_slots():
    docs = _pair()
    docs[1].raw = replace(docs[1].raw, content=docs[0].raw.content)
    assert _validate(docs)["corroborated"] == 0
    docs = _pair()
    docs[1].raw = replace(docs[1].raw, content="Reporting by Reuters.\n" + docs[1].raw.content)
    assert _validate(docs)["corroborated"] == 0
    assert _event(docs[1])["status"] == "duplicate_origin"


def test_controlled_alias_resolves_without_a_literal_ticker_but_unknown_name_does_not():
    docs = [_doc(name="Alpha", symbol=""), _doc("b", "bloomberg.com", words="second")]
    assert _validate(docs)["corroborated"] == 2
    assert _event(docs[0])["tradable_security_ids"] == ["listed:alpha"]
    unknown = _doc(name="Unregistered Supplier", symbol="")
    _validate([unknown])
    assert _event(unknown)["tradable_security_ids"] == []
    assert _event(unknown)["subject_ids"][0].startswith("external:issuer:")
    assert "action" not in _event(unknown)["allowed_uses"]


def test_two_known_but_conflicting_identifiers_cannot_be_resolved_by_convenient_name():
    doc = _doc(name="Alpha Corporation", symbol="BBB")
    registry = copy.deepcopy(REGISTRY)
    registry["securities"]["BBB"] = "listed:beta"
    registry["entities"]["issuer:beta"] = {
        "type": "issuer", "name": "Beta Company", "aliases": [],
        "identifiers": {"ticker": ["BBB"]}, "security_ids": ["listed:beta"],
    }
    stats = validate_structured_events([doc], CUTOFF, entity_registry=registry)
    assert stats["event_identity"]["documents"]["a"]["status"] == "unverified_event"
    assert stats["event_identity"]["documents"]["a"]["subject_errors"]["subject"] == "subject_identity_conflict"


def test_foreign_research_subject_can_support_analysis_without_us_trade_authority():
    docs = _pair(name="Samsung Electronics", symbol="")
    assert _validate(docs)["corroborated"] == 2
    assert target_evidence_sources(docs, ["memory"])["memory"] == ["a", "b"]
    assert all(not _event(doc)["tradable_security_ids"] and "action" not in _event(doc)["allowed_uses"]
               for doc in docs)


def _add_period_event(doc, period, *, primary=False):
    quote = f"Comparison period ended {period}."
    doc.raw = replace(doc.raw, content=doc.raw.content + "\n" + quote)
    doc.evidence_spans.append({"id": "s3", "start": None, "end": None, "quote": quote})
    event = copy.deepcopy(doc.event_records[0])
    event.update(id="comparison", is_primary=primary, source_span_ids=["s1", "s3"])
    event["report_period"].update(value=period, span_ids=["s3"])
    doc.event_records.append(event)


def test_primary_report_period_is_bound_per_record_not_all_dates_in_entire_article():
    docs = _pair()
    _add_period_event(docs[0], "2026-06-30")
    assert _validate(docs)["corroborated"] == 2
    assert docs[0].eligibility["events"][1]["status"] == "context_event"
    assert "scoring" not in docs[0].eligibility["events"][1]["allowed_uses"]


def test_ambiguous_two_primary_periods_are_not_silently_chosen():
    docs = _pair()
    _add_period_event(docs[0], "2026-06-30", primary=True)
    assert _validate(docs)["corroborated"] == 0
    assert {_event["status"] for _event in docs[0].eligibility["events"]} == {"ambiguous_primary_period"}


def test_mna_has_a_shared_canonical_type_and_requires_its_own_event_identity():
    docs = _pair()
    for doc in docs:
        doc.event_records[0]["event_type"] = "mna"
        doc.event_records[0]["report_period"] = {"kind": "unknown", "value": None, "span_ids": []}
    assert _validate(docs)["corroborated"] == 0
    for doc in docs:
        quote = "Merger contract identifier ABC-2026-0042."
        doc.raw = replace(doc.raw, content=doc.raw.content + "\n" + quote)
        doc.evidence_spans.append({"id": "mna", "start": None, "end": None, "quote": quote})
        doc.event_records[0]["event_identifiers"] = [
            {"scheme": "contract_id", "value": "ABC-2026-0042", "span_ids": ["mna"]}]
        doc.event_records[0]["source_span_ids"].append("mna")
    assert _validate(docs)["corroborated"] == 2
    assert "/mna/contract_id:abc-2026-0042" in _event(docs[0])["event_id"]


def test_authoritative_macro_series_does_not_require_ticker_or_gain_trade_permission():
    doc = _doc(host="www.bls.gov", name="Consumer Price Index", symbol="", target="macro")
    doc.subjects[0].update(type="macro_series", identifiers=[])
    doc.event_records[0].update(event_type="macro", claim_kind="official_statistic")
    original = doc.evidence_spans[2]["quote"]
    fact = "Consumer Price Index was 300.2 index points."
    doc.raw = replace(doc.raw, content=doc.raw.content.replace(original, fact))
    doc.evidence_spans[2]["quote"] = fact
    doc.evidence_spans[0].update(start=0, end=len("Consumer Price Index"))
    doc.claims[0].update(predicate="price_index", value="300.2", unit="index points")
    registry = {"entities": {"series:CPI": {"type": "macro_series", "name": "Consumer Price Index",
                "aliases": [], "identifiers": {}, "security_ids": []}}}
    policy = _policy("bls", "www.bls.gov", "official_statistics")
    _authority(doc, "bls", "official_statistics", series=["series:CPI"])
    result = validate_structured_events([doc], CUTOFF, entity_registry=registry, source_policies=policy)
    assert result["corroborated"] == 1 and _event(doc)["status"] == "verified_original"
    assert _event(doc)["tradable_security_ids"] == [] and "action" not in _event(doc)["allowed_uses"]
    doc.raw.meta["source_policy_id"] = "not-registered"
    assert validate_structured_events([doc], CUTOFF, entity_registry=registry, source_policies=policy)["corroborated"] == 0


def test_authority_label_from_web_metadata_cannot_override_registry_or_late_mutable_version():
    doc = _doc(host="attacker.test")
    doc.event_records[0]["claim_kind"] = "issuer_disclosure"
    _authority(doc, "sec", "sec_filing", issuer="0000000123")
    assert _validate([doc], source_policies=_policy("sec", "www.sec.gov", "sec_filing"))["corroborated"] == 0
    doc = _doc(host="investor.example.com")
    doc.raw = replace(doc.raw, fetched_at=CUTOFF.timestamp() + 1)
    doc.event_records[0]["claim_kind"] = "issuer_disclosure"
    _authority(doc, "ir", "issuer_ir", issuer="0000000123")
    assert _validate([doc], source_policies=_policy("ir", "investor.example.com", "issuer_ir"))["corroborated"] == 0
    assert _event(doc)["status"] == "version_unverified_at_cutoff"


def test_sec_immutable_accession_permits_late_capture_but_not_false_issuer_or_accession():
    doc = _doc(host="www.sec.gov")
    doc.raw = replace(doc.raw, url="https://www.sec.gov/Archives/edgar/data/123/000000012326000001/file.htm",
                      fetched_at=CUTOFF.timestamp() + 120)
    doc.event_records[0]["claim_kind"] = "issuer_disclosure"
    _authority(doc, "sec", "sec_filing", issuer="0000000123", proof_type="sec_submission_acceptance",
               accession="0000000123-26-000001", accepted_at="2026-11-02T14:30:00Z")
    policy = _policy("sec", "www.sec.gov", "sec_filing")
    assert _validate([doc], source_policies=policy)["corroborated"] == 1
    assert _event(doc)["allowed_uses"] == ["background", "analysis", "scoring", "action"]
    doc.raw.meta["authority"]["proof_ref"]["accession"] = "0000000123-26-000002"
    assert _validate([doc], source_policies=policy)["corroborated"] == 0
    doc.raw.meta["authority"]["proof_ref"]["accession"] = "0000000123-26-000001"
    doc.raw.meta["authority"]["issuer_id"] = "0000000999"
    assert _validate([doc], source_policies=policy)["corroborated"] == 0


@pytest.mark.parametrize("issuer,expected", [("0000000123", "listed:alpha"), ("0000000999", "listed:beta")])
@pytest.mark.parametrize("reverse_subjects", [False, True])
def test_multi_issuer_filing_only_grants_action_to_its_registered_filer(issuer, expected, reverse_subjects):
    doc = _doc(host="www.sec.gov")
    registry = copy.deepcopy(REGISTRY)
    registry["securities"]["BBB"] = "listed:beta"
    registry["entities"]["issuer:beta"] = {
        "type": "issuer", "name": "Beta Company", "aliases": [],
        "identifiers": {"ticker": ["BBB"], "cik": ["0000000999"]},
        "security_ids": ["listed:beta"],
    }
    quote = "Beta Company (BBB) is a separately listed counterparty."
    doc.raw = replace(doc.raw, content=doc.raw.content + "\n" + quote,
        url=f"https://www.sec.gov/Archives/edgar/data/{int(issuer)}/{issuer}26000001/filing.htm")
    doc.evidence_spans.append({"id": "beta", "start": None, "end": None, "quote": quote})
    doc.subjects.append({"id": "other", "type": "issuer", "name": "Beta Company",
        "identifiers": [{"scheme": "ticker", "value": "BBB", "span_ids": ["beta"]}], "span_ids": ["beta"]})
    doc.event_records[0].update(subject_ids=["other", "subject"] if reverse_subjects else ["subject", "other"],
                               claim_kind="issuer_disclosure")
    doc.event_records[0]["source_span_ids"].append("beta")
    _authority(doc, "sec", "sec_filing", issuer=issuer, proof_type="sec_submission_acceptance",
               accession=issuer + "-26-000001", accepted_at="2026-11-02T14:30:00Z")
    result = validate_structured_events([doc], CUTOFF, entity_registry=registry,
                                        source_policies=_policy("sec", "www.sec.gov", "sec_filing"))
    assert result["corroborated"] == 1
    assert _event(doc)["tradable_security_ids"] == ["listed:alpha", "listed:beta"]
    assert _event(doc)["action_security_ids"] == [expected]
    assert doc.eligibility["claims"][0]["action_security_ids"] == [expected]
    assert "action" in doc.eligibility["claims"][0]["allowed_uses"]


def test_literal_model_cik_and_ticker_do_not_create_unregistered_action_owner():
    doc = _doc(host="www.sec.gov")
    quote = "Central Index Key 0000000123."
    doc.raw = replace(doc.raw, content=doc.raw.content + "\n" + quote,
                      url="https://www.sec.gov/Archives/edgar/data/123/000000012326000001/filing.htm")
    doc.evidence_spans.append({"id": "cik", "start": None, "end": None, "quote": quote})
    doc.subjects[0]["identifiers"].append({"scheme": "cik", "value": "0000000123", "span_ids": ["cik"]})
    doc.event_records[0]["source_span_ids"].append("cik")
    doc.event_records[0]["claim_kind"] = "issuer_disclosure"
    _authority(doc, "sec", "sec_filing", issuer="0000000123", proof_type="sec_submission_acceptance",
               accession="0000000123-26-000001", accepted_at="2026-11-02T14:30:00Z")
    registry = {"securities": {"AAA": "listed:alpha"}, "entities": {}}
    result = validate_structured_events([doc], CUTOFF, entity_registry=registry,
                                        source_policies=_policy("sec", "www.sec.gov", "sec_filing"))
    assert result["corroborated"] == 1
    assert _event(doc)["tradable_security_ids"] == ["listed:alpha"]
    assert _event(doc)["action_security_ids"] == []
    assert "action" not in _event(doc)["allowed_uses"]
    assert doc.eligibility["claims"][0]["action_security_ids"] == []


def test_saved_original_qualifies_as_explicit_historical_baseline_not_current_catalyst():
    doc = _doc(host="investor.example.com")
    doc.raw.meta["source_times"]["published"]["value"] = "2025-11-02T14:30:00Z"
    doc.event_records[0]["claim_kind"] = "issuer_disclosure"
    _authority(doc, "ir", "issuer_ir", issuer="0000000123")
    assert _validate([doc], source_policies=_policy("ir", "investor.example.com", "issuer_ir"))["corroborated"] == 0
    assert _event(doc)["status"] == "historical_baseline"
    assert target_evidence_sources([doc], ["memory"], use="analysis") == {"memory": ["a"]}
    assert target_evidence_sources([doc], ["memory"]) == {"memory": []}


@pytest.mark.parametrize("field", ["url", "body", "annotation", "authority"])
def test_source_projection_cannot_reuse_eligibility_after_bound_facts_change(field):
    docs = _pair()
    _validate(docs)
    doc = docs[0]
    if field == "url":
        doc.raw = replace(doc.raw, url="https://attacker.test/")
    elif field == "body":
        doc.raw = replace(doc.raw, content=doc.raw.content + " changed")
    elif field == "annotation":
        doc.event_records[0]["target_ids"] = ["energy"]
    else:
        doc.raw.meta["authority"] = {"scope": "forged"}
    assert target_evidence_sources([doc], ["memory", "energy"]) == {"memory": [], "energy": []}


def _sentiment(chain="memory", *, status="assessed", refs=None):
    return {"chain_id": chain, "status": status,
            "heat": 5 if status == "assessed" else None,
            "sentiment_score": .3 if status == "assessed" else None,
            "narrative_change": "stable" if status == "assessed" else None,
            "key_drivers": ["已核原文显示订单变化。" if status == "assessed" else "尚缺本链原文。"],
            "evidence_ids": refs if refs is not None else (["a"] if status == "assessed" else [])}


def test_target_scoring_is_nullable_without_borrowing_another_targets_sources():
    sources = {"memory": ["a"], "logic": []}
    schema = sentiment_schema(["memory", "logic"], ["a"], target_sources=sources)
    result = {"chains": [_sentiment(), _sentiment("logic", status="insufficient_evidence")]}
    validate_schema(result, schema)
    validate_target_assessments("tech.sentiment", result, ["memory", "logic"], sources)
    result["chains"][1] = _sentiment("logic", refs=["a"])
    with pytest.raises(LLMUnavailable):
        validate_schema(result, schema)
    with pytest.raises(ValueError):
        validate_target_assessments("tech.sentiment", result, ["memory", "logic"], sources)


@pytest.mark.parametrize("case", ["non_null_unknown", "assessed_null", "duplicate_target", "foreign_id"])
def test_target_semantic_failures_are_not_repaired_or_defaulted(case):
    sources = {"memory": ["a"], "logic": ["b"]}
    result = {"chains": [_sentiment(), _sentiment("logic", status="insufficient_evidence")]}
    if case == "non_null_unknown":
        result["chains"][1]["heat"] = 5
    elif case == "assessed_null":
        result["chains"][0]["heat"] = None
    elif case == "duplicate_target":
        result["chains"][1] = copy.deepcopy(result["chains"][0])
    else:
        result["chains"][0]["evidence_ids"] = ["b"]
    with pytest.raises(ValueError):
        validate_target_assessments("tech.sentiment", result, ["memory", "logic"], sources)


def test_sector_conflict_requires_unknown_interpretations_and_null_score():
    sources = {"XLK": ["a", "b"], "XLE": []}
    rows = [{"etf": target, "status": status, "narrative_score": None,
             "eps_revision_bias": "unknown", "guidance_tone": "unknown",
             "evidence": ["来源存在冲突。"], "evidence_ids": refs}
            for target, status, refs in [("XLK", "conflicted", ["a", "b"]),
                                         ("XLE", "insufficient_evidence", [])]]
    schema = narrative_schema(list(sources), ["a", "b"], target_sources=sources)
    validate_schema({"sectors": rows}, schema)
    rows[0]["narrative_score"] = 5
    with pytest.raises(LLMUnavailable):
        validate_schema({"sectors": rows}, schema)


def _risk_output(*, status="assessed", level=0, severity=None):
    return {"assessment": "逐链记录资料覆盖与已知预警。",
            "chain_assessments": [
                {"chain_id": "memory", "status": status, "risk_level": level, "evidence_ids": ["a"]},
                {"chain_id": "logic", "status": "insufficient_evidence", "risk_level": None, "evidence_ids": []}],
            "alerts": ([] if severity is None else [{"severity": severity, "chain_id": "memory",
                "link": "upstream", "type": "policy", "headline_zh": "原文披露重大政策风险。",
                "transmission": ["可能影响供应。"], "evidence_ids": ["a"]}])}


def test_known_alert_survives_incomplete_overall_assessment_but_does_not_create_score():
    docs = _pair()
    _validate(docs)
    sources = target_evidence_sources(docs, ["memory", "logic"])
    value = _risk_output(status="insufficient_evidence", level=None, severity=9)
    schema = risk_schema(["memory", "logic"], ["a", "b"], target_sources=sources)
    validate_schema(value, schema)
    validate_target_assessments("tech.risk", value, ["memory", "logic"], sources)
    coverage = risk_coverage(docs, chain_ids=["memory", "logic"],
                             assessments=value["chain_assessments"], alerts=value["alerts"])
    assert coverage["contract"] == TARGET_CONTRACT
    assert coverage["targets"]["memory"]["risk_level"] is None
    assert coverage["targets"]["memory"]["known_alert_level"] == 9
    alerts = [RiskAlert(**row) for row in value["alerts"]]
    signals = TechChainFusionAgent().execute([ChainMonitorRow("memory", chain_mom20=.1)], [], {}, alerts,
                                             risk_coverage_by_chain=coverage["targets"])
    memory = next(row for row in signals if row.chain_id == "memory")
    assert isinstance(memory, StructuredTechChainSignal)
    assert memory.risk_level is None and memory.known_alert_level == 9
    assert "tech.risk" in memory.degraded_components and memory.bonus_hint <= 1
    assert memory.alerts[0]["evidence_ids"] == ["a"]


def test_one_target_no_alert_is_not_six_complete_clean_risk_scores():
    docs = _pair()
    _validate(docs)
    value = _risk_output()
    coverage = risk_coverage(docs, chain_ids=["memory", "logic"],
                             assessments=value["chain_assessments"], alerts=[])
    signals = TechChainFusionAgent().execute([], [], {}, [], risk_coverage_by_chain=coverage["targets"])
    memory = next(row for row in signals if row.chain_id == "memory")
    assert memory.risk_level == 0 and memory.prosperity == 10
    assert all(row.risk_level is None and row.prosperity is None and row.bonus_hint == 1
               for row in signals if row.chain_id != "memory")
    availability_only = risk_coverage(docs, chain_ids=["memory", "logic"])
    unassessed = TechChainFusionAgent().execute([], [], {}, [], risk_coverage_by_chain=availability_only["targets"])
    assert all(row.prosperity is None and row.risk_level is None for row in unassessed)


def test_wrong_target_alert_and_complete_risk_score_inconsistency_are_rejected():
    docs = _pair()
    _validate(docs)
    sources = target_evidence_sources(docs, ["memory", "logic"])
    value = _risk_output(level=0, severity=9)
    with pytest.raises(ValueError, match="DIFFERS"):
        validate_target_assessments("tech.risk", value, ["memory", "logic"], sources)
    with pytest.raises(ValueError, match="differs"):
        risk_coverage(docs, chain_ids=["memory", "logic"], assessments=value["chain_assessments"], alerts=value["alerts"])
    value["alerts"][0]["chain_id"] = "logic"
    with pytest.raises(ValueError, match="SOURCE"):
        validate_target_assessments("tech.risk", value, ["memory", "logic"], sources)


def test_severe_subchain_warning_cannot_be_overwritten_when_folding_main_chain_bonus():
    serious = StructuredTechChainSignal("memory", "semis", 8, None, bonus_hint=1,
                                        known_alert_level=9, risk_assessment_status="insufficient_evidence")
    strong = StructuredTechChainSignal("logic", "semis", 10, 0, bonus_hint=1.15,
                                       known_alert_level=0, risk_assessment_status="assessed")
    assert signals_to_bonus_map([serious, strong])["semis"] == 1
    assert signals_to_bonus_map([strong, serious])["semis"] == 1


def test_structured_sentiment_keeps_citations_and_missing_state_without_changing_v1_class():
    row = StructuredSentimentRow("memory", heat=None, sentiment_score=None,
                                 evidence_ids=[], status="insufficient_evidence")
    assert asdict(row)["evidence_ids"] == [] and row.degraded


def test_empty_target_evidence_schemas_allow_honest_complete_unknown_answers():
    sources = {"memory": [], "logic": []}
    value = {"chains": [_sentiment(chain, status="insufficient_evidence") for chain in sources]}
    validate_schema(value, sentiment_schema(list(sources), [], allow_empty=True, target_sources=sources))
    risk = _risk_output(status="insufficient_evidence", level=None)
    risk["chain_assessments"][0]["evidence_ids"] = []
    validate_schema(risk, risk_schema(list(sources), [], allow_empty=True, target_sources=sources))


def test_structured_clean_schema_has_closed_nested_evidence_shapes():
    doc = _doc()
    row = {"id": "a", "tickers": ["AAA"], "sentiment": "neutral", "sentiment_score": 0,
           "events": ["mna"], "summary_zh": "并购尚待核实。", "relevance": .8,
           "subjects": doc.subjects, "evidence_spans": doc.evidence_spans,
           "event_records": doc.event_records, "claims": doc.claims}
    schema = clean_schema(["a"], structured=True, target_ids=["memory"])
    validate_schema({"documents": [row]}, schema)
    row["event_records"][0]["target_ids"] = ["not-approved"]
    with pytest.raises(LLMUnavailable):
        validate_schema({"documents": [row]}, schema)


def test_empty_structured_clean_still_has_a_complete_valid_model_answer():
    validate_schema({"documents": []}, clean_schema([], structured=True))
    with pytest.raises(ValueError):
        clean_schema([])


def test_one_unverified_span_is_reported_on_its_event_without_hiding_valid_sibling():
    docs = _pair()
    _add_period_event(docs[0], "2026-06-30")
    docs[0].evidence_spans[-1]["quote"] = "Not present in captured original."
    assert _validate(docs)["corroborated"] == 2
    primary, comparison = docs[0].eligibility["events"]
    assert primary["status"] == "corroborated"
    assert comparison["status"] == "unverified_event" and comparison["reason"]
    assert comparison["allowed_uses"] == ["background"]


def _number_fact(doc, value):
    old = doc.evidence_spans[2]["quote"]
    quote = f"Net income was USD {value} million in this reporting period. " + old
    doc.raw = replace(doc.raw, content=doc.raw.content.replace(old, quote))
    doc.evidence_spans[2]["quote"] = quote
    doc.claims[0].update(predicate="net_income", value=value, unit="million", currency="USD")


@pytest.mark.parametrize("field,value", [("value", "212.3"), ("unit", "billion"), ("currency", "EUR")])
def test_claim_cannot_invent_number_unit_or_currency_not_literal_in_its_span(field, value):
    docs = _pair()
    for doc in docs:
        _number_fact(doc, "12.3")
    docs[0].claims[0][field] = value
    assert _validate(docs)["corroborated"] == 0
    claim = docs[0].eligibility["claims"][0]
    assert claim["status"] == "unverified_claim" and claim["reason"] == "claim_literal_not_in_span"
    assert _event(docs[0])["status"] == "no_verified_claim"


def test_event_heading_and_period_without_a_verified_fact_do_not_establish_scoring_evidence():
    docs = _pair()
    docs[0].claims = []
    assert _validate(docs)["corroborated"] == 0
    assert _event(docs[0])["status"] == "no_verified_claim"
    for doc in docs:
        doc.claims = [{"id": "mention", "event_id": "event", "predicate": "name_mention",
                       "value": None, "unit": None, "currency": None, "source_span_ids": ["s1"],
                       "relation": "mentions", "conflict_group": None, "supersedes_claim_id": None}]
    assert _validate(docs)["corroborated"] == 0


def test_conflicting_verified_numbers_force_null_target_score_without_hiding_risk_sources():
    docs = _pair()
    _number_fact(docs[0], "12.3")
    _number_fact(docs[1], "11.3")
    assert _validate(docs)["corroborated"] == 2
    sources = target_evidence_sources(docs, ["memory", "logic"])
    coverage = target_evidence_coverage(docs, ["memory", "logic"])
    assert coverage["memory"]["status"] == "conflicted" and coverage["memory"]["conflict_ids"]
    assert sources["memory"] == ["a", "b"]
    wrong = _risk_output()
    schema = risk_schema(["memory", "logic"], ["a", "b"],
                         target_sources=sources, target_coverage=coverage)
    with pytest.raises(LLMUnavailable):
        validate_schema(wrong, schema)
    with pytest.raises(ValueError, match="CONFLICT"):
        validate_target_assessments("tech.risk", wrong, ["memory", "logic"], sources,
                                    target_coverage=coverage)
    known_risk = _risk_output(status="conflicted", level=None, severity=9)
    validate_schema(known_risk, schema)
    risk = risk_coverage(docs, chain_ids=["memory", "logic"],
                         assessments=known_risk["chain_assessments"], alerts=known_risk["alerts"])
    assert risk["targets"]["memory"]["known_alert_level"] == 9
    assert risk["targets"]["memory"]["risk_level"] is None
    assert risk["targets"]["memory"]["conflict_ids"]


def test_model_declared_supersession_does_not_erase_original_evidence_without_source_proof():
    docs = _pair()
    _number_fact(docs[0], "12.3")
    _number_fact(docs[1], "11.3")
    docs[1].claims[0]["supersedes_claim_id"] = "a:claim"
    _validate(docs)
    assert docs[1].eligibility["claims"][0]["supersession_status"] == "unverified"
    assert docs[0].eligibility["claims"][0]["status"] == "conflicted"
    assert "scoring" in docs[0].eligibility["claims"][0]["allowed_uses"]


def test_alias_suffix_requires_reviewed_alias_instead_of_silent_ticker_stripping():
    doc = _doc(name="AAA.US", symbol="")
    _validate([doc])
    assert _event(doc)["tradable_security_ids"] == []
    registry = copy.deepcopy(REGISTRY)
    registry["entities"]["issuer:alpha"]["aliases"].append("AAA.US")
    validate_structured_events([doc], CUTOFF, entity_registry=registry)
    assert _event(doc)["tradable_security_ids"] == ["listed:alpha"]


def test_future_sec_acceptance_is_not_made_public_by_earlier_capture_label():
    doc = _doc(host="www.sec.gov")
    doc.raw = replace(doc.raw, url="https://www.sec.gov/Archives/edgar/data/123/000000012326000001/file.htm")
    doc.event_records[0]["claim_kind"] = "issuer_disclosure"
    _authority(doc, "sec", "sec_filing", issuer="issuer:0000000123", proof_type="sec_submission_acceptance",
               accession="0000000123-26-000001", accepted_at="2026-11-04T14:00:01Z")
    assert _validate([doc], source_policies=_policy("sec", "www.sec.gov", "sec_filing"))["corroborated"] == 0
    assert _event(doc)["status"] == "version_unverified_at_cutoff"


def test_same_issuer_official_versions_do_not_fake_independence_or_hide_correction_conflicts():
    docs = [_doc(host="investor.example.com"),
            _doc("b", "investor.example.com", words="second")]
    for doc, value, issuer in zip(docs, ["12.3", "11.3"], ["issuer:0000000123", "0000000123"]):
        _number_fact(doc, value)
        doc.event_records[0]["claim_kind"] = "issuer_disclosure"
        _authority(doc, "ir", "issuer_ir", issuer=issuer)
    result = _validate(docs, source_policies=_policy("ir", "investor.example.com", "issuer_ir"))
    assert result["corroborated"] == 2
    assert {_event(doc)["origin"] for doc in docs} == {"issuer:cik:0000000123"}
    assert all(_event(doc)["independent_origin_count"] == 1 for doc in docs)
    assert target_evidence_coverage(docs, ["memory"])["memory"]["status"] == "conflicted"


def test_v2_schemas_keep_all_1100_document_ids_while_v1_limit_stays_frozen():
    docs = ["document-" + str(index) for index in range(1100)]
    clean = clean_schema(docs, structured=True, target_ids=["memory"])
    assert clean["properties"]["documents"]["minItems"] == 1100
    assert clean["properties"]["documents"]["items"]["properties"]["id"]["enum"] == docs
    sources = {"memory": docs}
    value = {"chains": [_sentiment(refs=[docs[-1]])]}
    validate_schema(value, sentiment_schema(["memory"], docs, target_sources=sources))
    validate_schema({"assessment": "All source references remain addressable.",
        "chain_assessments": [{"chain_id": "memory", "status": "assessed", "risk_level": 0,
                                "evidence_ids": [docs[-1]]}], "alerts": []},
        risk_schema(["memory"], docs, target_sources=sources))
    validate_schema({"sectors": [{"etf": "XLK", "status": "assessed", "narrative_score": 5,
        "eps_revision_bias": "unknown", "guidance_tone": "unknown", "evidence": ["Primary source reviewed."],
        "evidence_ids": [docs[-1]]}]}, narrative_schema(["XLK"], docs, target_sources={"XLK": docs}))
    for factory, targets in [(sentiment_schema, ["memory"]), (risk_schema, ["memory"]),
                             (narrative_schema, ["XLK"])]:
        with pytest.raises(ValueError, match="ID_SET_REQUIRED"):
            factory(targets, docs)
    with pytest.raises(ValueError, match="ID_SET_REQUIRED"):
        clean_schema(docs)


def test_v2_schema_limit_is_explicit_and_never_silently_truncates():
    from trading_system.llm.required_schemas import V2_MAX_DOCUMENTS, _ids
    docs = ["d" + str(index) for index in range(V2_MAX_DOCUMENTS)]
    assert _ids(docs, maximum=V2_MAX_DOCUMENTS) == docs
    with pytest.raises(ValueError, match="ID_SET_REQUIRED"):
        clean_schema([*docs, "overflow"], structured=True)
    with pytest.raises(ValueError, match="INVALID_ID_LIMIT"):
        _ids(["a"], maximum=V2_MAX_DOCUMENTS + 1)
