"""Unqualified adverse signals remain leads and trigger bounded verification."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from test_research_work_semantics import annotation, raw
from trading_system.llm.work_semantics import normalize_annotation, validate_annotation
from trading_system.research_risk import pending_risk_leads, assessment_rows


CUTOFF = datetime(2026, 11, 8, tzinfo=timezone.utc)


def plan():
    return {"as_of": CUTOFF.isoformat(), "limits": {"deadline_seconds": 1800},
        "coverage_cells": [{"cell_id": "risk-xlk", "target_id": "sector:XLK", "required": True,
            "material_type": "counterevidence", "window_days": 30},
            {"cell_id": "other-xlf", "target_id": "sector:XLF", "required": True,
            "material_type": "news", "window_days": 30}]}


def document(*, negative=True, relation="supports", published=None):
    original = raw()
    original.meta["intelligence_document_id"] = "original-fixture"
    if published is not None:
        original = replace(original, published=published)
    value = annotation()
    if negative:
        value.update(sentiment="bearish", sentiment_score=-0.8)
    value["claims"][0]["relation"] = relation
    result = normalize_annotation(original, value, ["XLK"])
    validate_annotation(result, CUTOFF)
    return result


def test_unqualified_negative_fact_stays_unverified_and_blocks_only_owned_cell():
    item = document()
    before = deepcopy(item)
    leads = pending_risk_leads([item], plan())
    assert leads["status"] == "pending_verification" and len(leads["leads"]) == 1
    lead = leads["leads"][0]
    assert lead["state"] == "unverified" and lead["severity"] is None
    assert lead["scoring_contribution"] == 0 and lead["verified_fact"] is False
    assert lead["blocked_cell_ids"] == ["risk-xlk"]
    assert lead["source_spans"][0]["quote"] in item.raw.content
    assessed = assessment_rows([item], plan())
    assert assessed["coverage"][0]["reason"] == "pending_source_bound_risk_lead"
    assert assessed["coverage"][0]["pending_lead_ids"] == [lead["lead_id"]]
    assert "pending_lead_ids" not in assessed["coverage"][1]
    assert item == before  # No event qualification, text or financial literal is rewritten.


def test_refutation_is_retained_even_when_overall_sentiment_is_neutral():
    lead = pending_risk_leads([document(negative=False, relation="refutes")], plan())["leads"][0]
    assert lead["trigger"] == "source_bound_refutation"
    assert lead["claim"]["relation"] == "refutes" and lead["state"] == "unverified"


def test_no_adverse_signal_is_not_claimed_as_no_risk():
    result = pending_risk_leads([document(negative=False)], plan())
    assert result["leads"] == [] and result["status"] == "no_pending_leads_observed"
    assert result["risk_absence_verified"] is False


def test_future_publication_cannot_trigger_a_pre_cutoff_risk_claim():
    result = pending_risk_leads([document(published="2026-11-09T00:00:00+00:00")], plan())
    assert result["leads"] == []
    assert result["excluded_future_claims"] == 1


def test_changed_annotation_cannot_reuse_old_qualification():
    item = document()
    item.claims[0]["value"] = "999"
    with pytest.raises(ValueError, match="RISK_DOCUMENT_BINDING"):
        pending_risk_leads([item], plan())


def test_unsupported_or_malformed_literal_is_never_a_pending_fact():
    item = document()
    item.eligibility["claims"][0]["status"] = "unverified_claim"
    assert pending_risk_leads([item], plan())["leads"] == []
