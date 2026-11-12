"""New research origin/revision rules over original typed evidence, no provider."""
from copy import deepcopy
from dataclasses import replace
import hashlib

import pytest

from test_structured_evidence import _doc, _pair, _number_fact, _authority, _policy, REGISTRY, CUTOFF
from trading_system.search.event_identity import validate_structured_events, target_evidence_coverage
from trading_system.research_relations import validate_research_events


def test_late_attribution_cannot_count_as_a_second_independent_origin():
    docs = _pair()
    # Attribution occurs beyond the historical v2 20K scan, in an otherwise
    # different article; it cannot become an independent corroborating source.
    docs[1].raw = replace(docs[1].raw, content=docs[1].raw.content + "\n" + "Editorial context. " * 1600
                         + "\nOriginally published by Reuters.")
    historical = deepcopy(docs)
    assert validate_structured_events(historical, CUTOFF, entity_registry=REGISTRY)["corroborated"] == 2
    originals = [deepcopy(doc.raw) for doc in docs]
    stats = validate_research_events(docs, CUTOFF, entity_registry=REGISTRY)
    assert stats["corroborated"] == 0
    graph = stats["research_relations"]
    assert len(graph["origin_groups"]) == 1
    attributed = graph["documents"]["b"]["attributions"][0]
    assert attributed["start"] > 20_000
    assert docs[1].raw.content[attributed["start"]:attributed["end"]] == attributed["quote"]
    assert [doc.raw for doc in docs] == originals
    assert all(doc.eligibility["events"][0]["independent_origin_count"] == 1 for doc in docs)


def test_ten_reprints_remain_ten_originals_and_one_origin_group():
    docs = [_doc(str(index), "mirror" + str(index) + ".example", words="second") for index in range(10)]
    for doc in docs:
        doc.raw = replace(doc.raw, content=doc.raw.content + "\nSyndicated from Reuters.")
    stats = validate_research_events(docs, CUTOFF, entity_registry=REGISTRY)
    assert stats["total"] == 10 and stats["corroborated"] == 0
    group = stats["research_relations"]["origin_groups"]
    assert len(group) == 1 and len(group[0]["document_ids"]) == 10
    assert all(doc.eligibility["claims"] for doc in docs)


def test_independent_conflicting_numbers_keep_both_periods_sources_and_conflict():
    docs = _pair()
    _number_fact(docs[0], "12.3")
    _number_fact(docs[1], "11.3")
    stats = validate_research_events(docs, CUTOFF, entity_registry=REGISTRY)
    assert stats["corroborated"] == 2
    assert len(stats["research_relations"]["origin_groups"]) == 2
    conflict = stats["research_relations"]["conflicts"][0]
    assert conflict["claim_ids"] == ["a:claim", "b:claim"]
    assert conflict["report_period"] == "2026-09-30"
    assert conflict["unit"] == "million" and conflict["currency"] == "USD"
    assert target_evidence_coverage(docs, ["memory"])["memory"]["status"] == "conflicted"


def test_reported_revision_is_not_permission_to_delete_or_replace_a_fact():
    docs = [_doc(host="investor.example.com"), _doc("b", "investor.example.com", words="second")]
    for doc, amount in zip(docs, ["12.3", "11.3"]):
        _number_fact(doc, amount)
        doc.event_records[0]["claim_kind"] = "issuer_disclosure"
        _authority(doc, "ir", "issuer_ir", issuer="0000000123")
    docs[1].claims[0]["supersedes_claim_id"] = "a:claim"
    stats = validate_research_events(docs, CUTOFF, entity_registry=REGISTRY,
                                     source_policies=_policy("ir", "investor.example.com", "issuer_ir"))
    assert len(stats["research_relations"]["origin_groups"]) == 1
    relation = stats["research_relations"]["revisions"][0]
    assert relation["status"] == "pending_verification"
    assert relation["prior_claim_id"] == "a:claim" and relation["new_claim_id"] == "b:claim"
    assert relation["original_preserved"] is True
    assert all(doc.eligibility["claims"][0]["status"] == "conflicted" for doc in docs)


def test_future_revision_does_not_change_pre_cutoff_qualification():
    docs = _pair()
    future = _doc("future", "reuters.com", words="second")
    future.raw = replace(future.raw, published="2026-11-05T14:30:00Z")
    future.raw.meta["source_times"]["published"]["value"] = future.raw.published
    future.claims[0]["supersedes_claim_id"] = "a:claim"
    stats = validate_research_events([*docs, future], CUTOFF, entity_registry=REGISTRY)
    assert all(doc.corroborated for doc in docs) and not future.corroborated
    relation = stats["research_relations"]["revisions"][0]
    assert relation["status"] == "not_available_at_cutoff" and relation["affects_current_qualification"] is False


def test_source_time_axes_are_recorded_without_inventing_missing_times():
    doc = _doc()
    stats = validate_research_events([doc], CUTOFF, entity_registry=REGISTRY)
    row = stats["research_relations"]["documents"]["a"]
    assert row["times"]["decision_as_of"] == CUTOFF.isoformat()
    assert row["times"]["published"] == doc.raw.published
    assert row["times"]["first_available"] is None
    assert row["times"]["fetched_at"] == doc.raw.fetched_at
    assert row["times"]["revised_at"] is None
    assert row["body_sha256"] == hashlib.sha256(doc.raw.content.encode()).hexdigest()


def test_changed_or_unverified_declared_revision_cannot_grant_eligibility():
    docs = _pair()
    docs[0].claims[0]["supersedes_claim_id"] = "missing:claim"
    stats = validate_research_events(docs, CUTOFF, entity_registry=REGISTRY)
    row = stats["research_relations"]["revisions"][0]
    assert row["status"] == "pending_verification" and row["prior_claim_present"] is False
    assert not row["original_removed"]
