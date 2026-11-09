"""The new research contract owns identities and preserves complete evidence."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from trading_system.search.models import RawDocument


def raw(identity="source-1", content="Example revenue was 120 USD in fiscal year 2025."):
    return RawDocument(identity, "public", "Annual result", "https://example.org/report",
                       content, "2026-11-01T12:00:00+00:00", 1793534400,
                       {"completeness": "complete", "text_kind": "html_main", "audit_only": "retained"})


def annotation():
    return {"tickers": [], "sentiment": "neutral", "sentiment_score": 0,
            "events": ["earnings"], "summary_zh": "收入120 USD，低于目标；普通比较120 < 130。",
            "relevance": 1, "subjects": [{"id": "s1", "type": "issuer", "name": "Example",
                "identifiers": [], "span_ids": ["p1"]}],
            "evidence_spans": [{"id": "p1", "start": None, "end": None,
                "quote": "Example revenue was 120 USD in fiscal year 2025."}],
            "event_records": [{"id": "e1", "event_type": "earnings", "subject_ids": ["s1"],
                "is_primary": True, "event_identifiers": [],
                "report_period": {"kind": "fiscal_year", "value": "2025", "span_ids": ["p1"]},
                "event_time": {"value": None, "precision": "unknown", "span_ids": []},
                "effective_time": {"value": None, "precision": "unknown", "span_ids": []},
                "source_span_ids": ["p1"], "claim_kind": "reported_fact", "target_ids": ["XLK"]}],
            "claims": [{"id": "c1", "event_id": "e1", "predicate": "revenue", "value": "120",
                "unit": None, "currency": "USD", "source_span_ids": ["p1"], "relation": "supports",
                "conflict_group": None, "supersedes_claim_id": None}]}


def test_program_owns_identity_and_preserves_distinct_originals():
    from trading_system.llm.work_semantics import validate_raw_documents, extraction_schema
    docs = [raw(), raw("source-2")]
    assert validate_raw_documents(docs) == docs
    assert "id" not in extraction_schema(["XLK"])["properties"]["annotation"]["properties"]
    with pytest.raises(ValueError, match="DUPLICATE_DOCUMENT_ID"):
        validate_raw_documents([raw(), raw()])


def test_new_document_contract_keeps_multi_issuer_roundups_but_legacy_stays_bounded():
    from trading_system.llm.required_schemas import clean_schema
    from trading_system.llm.work_semantics import extraction_schema

    new_schema = extraction_schema(["XLK"])
    assert new_schema["properties"]["annotation"]["properties"]["tickers"]["maxItems"] == 64
    legacy = clean_schema(["program-owned"], structured=True, target_ids=["XLK"])
    assert legacy["properties"]["documents"]["items"]["properties"]["tickers"]["maxItems"] == 8


def test_extract_normalize_validate_keeps_original_and_comparisons():
    from trading_system.llm.work_semantics import normalize_annotation, validate_annotation
    original, native = raw(), annotation()
    untouched = deepcopy(native)
    result = normalize_annotation(original, native, ["XLK"])
    assert native == untouched
    assert result.raw is original and result.raw.content == original.content
    assert result.evidence_spans[0]["start"] == 0
    assert "<" in result.summary_zh
    check = validate_annotation(result, datetime(2026, 11, 8, tzinfo=timezone.utc))
    assert check["valid"] and check["document_id"] == "source-1"
    assert "scoring" not in result.eligibility["claims"][0]["allowed_uses"]


@pytest.mark.parametrize("mutation,code", [
    (lambda x: x.update(id="wrong"), "OUTPUT_SCHEMA"),
    (lambda x: x["evidence_spans"][0].update(quote="absent"), "SPAN"),
    (lambda x: x["claims"][0].update(value="999"), "ANNOTATION_INVALID"),
    (lambda x: x["claims"][0].update(currency="EUR"), "ANNOTATION_INVALID"),
    (lambda x: x["event_records"][0]["report_period"].update(value="2024"), "ANNOTATION_INVALID"),
    (lambda x: x["subjects"][0].update(name="Different issuer"), "ANNOTATION_INVALID"),
])
def test_wrong_identity_and_unbound_financial_facts_fail(mutation, code):
    from trading_system.llm.work_semantics import normalize_annotation, validate_annotation
    value = annotation()
    mutation(value)
    with pytest.raises(ValueError, match=code):
        document = normalize_annotation(raw(), value, ["XLK"])
        validate_annotation(document, datetime(2026, 11, 8, tzinfo=timezone.utc))


def test_ambiguous_quote_cannot_bind_arbitrary_occurrence():
    from trading_system.llm.work_semantics import normalize_annotation
    with pytest.raises(ValueError, match="SPAN"):
        normalize_annotation(raw(content=raw().content * 2), annotation(), ["XLK"])


def test_model_offsets_are_ignored_and_program_binds_the_unique_quote():
    from trading_system.llm.work_semantics import normalize_annotation
    native = annotation()
    native["evidence_spans"][0].update(start=1, end=6)
    original = deepcopy(native)
    result = normalize_annotation(raw(), native, ["XLK"])
    assert native == original
    assert result.evidence_spans[0]["start"] == 0
    assert result.evidence_spans[0]["end"] == len(raw().content)


def test_review_binds_extract_and_omissions_block_completion():
    from trading_system.llm.work_semantics import normalize_annotation, validate_document_review
    document = normalize_annotation(raw(), annotation(), ["XLK"])
    accepted = {"complete": True, "checks": {key: True for key in
        ("subjects", "time_and_period", "amounts_units_currency", "counterevidence", "citations", "material_facts")},
        "omissions": [], "contradictions": [], "notes": "已逐项核对完整正文。"}
    receipt = validate_document_review(document, accepted)
    assert receipt["complete"] and receipt["annotation_sha256"]
    with pytest.raises(ValueError, match="DOCUMENT_REVIEW_FAILED"):
        validate_document_review(document, {**accepted, "omissions": ["遗漏负面指引"]})


def test_task_view_keeps_all_facts_counterevidence_and_omission_index():
    from trading_system.llm.work_semantics import normalize_annotation, task_evidence_view
    document = normalize_annotation(raw(), annotation(), ["XLK"])
    full = task_evidence_view([document], compact=False)
    assert full["documents"][0]["content"] == document.raw.content
    assert "audit_only" not in full["documents"][0]
    compact = task_evidence_view([document], compact=True)
    assert compact["documents"][0]["claims"] == document.claims
    assert compact["selection_manifest"][0]["omitted_ranges"] == []
    document.raw = raw(content="Header\n" + raw().content + "\nFooter")
    document.evidence_spans[0].update(start=7, end=7 + len(raw().content))
    compact = task_evidence_view([document], compact=True)
    assert compact["selection_manifest"][0]["omitted_ranges"] == [[0, 7], [55, 62]]


def test_admitted_contract_is_explicit_and_closed():
    from trading_system.research_work import research_work_policy, validate_research_work_policy
    policy = research_work_policy()
    assert validate_research_work_policy(policy) == policy
    legacy = research_work_policy(legacy=True)
    assert "segment_contract" not in legacy
    assert validate_research_work_policy(legacy) == legacy
    with pytest.raises(ValueError, match="RESEARCH_WORK_POLICY"):
        validate_research_work_policy({**policy, "format_retry_limit": 2})


def test_extract_prompt_keeps_unmapped_subjects_out_of_typed_events():
    from trading_system.llm.work_required import document_arguments
    arguments = document_arguments("extract", raw(), ["XLK"], {
        "run_date": "2026-11-08", "market_timezone": "America/New_York"})
    assert "人物或无法映射到这些类型的主体不得生成event_records或claims" in arguments["user"]
    assert "start/end固定填null" in arguments["user"]
    assert "正文只出现公司名而未出现ticker/CIK/LEI时，identifiers必须为空" in arguments["user"]
    assert "宁可少出typed facts" in arguments["user"]


def test_review_schema_completion_is_scoped_to_provided_source_text():
    from trading_system.llm.work_required import document_arguments
    from trading_system.llm.work_semantics import annotation_payload, normalize_annotation

    document = normalize_annotation(raw(), annotation(), ["XLK"])
    arguments = document_arguments(
        "document_review", raw(), ["XLK"],
        {"run_date": "2026-11-08", "market_timezone": "America/New_York"},
        annotation=annotation_payload(document))
    assert "不判断外部世界或来源全集是否完整" in arguments["user"]
    assert "partial来源即使只能看到摘要/片段" in arguments["user"]


def test_structural_segment_plan_covers_every_original_character_without_truncation():
    from trading_system.llm.work_required import plan_document_segments
    from trading_system.research_work import research_work_policy

    class Planner:
        def plan_chat(self, **arguments):
            return {}, len(arguments["public_payload"]["source"]["content"]), 1

    text = ("Section one. Revenue was 120 USD.\n\n"
            "Section two. Risk was 80 USD.\n")
    policy = research_work_policy()
    policy["segment_input_token_limit"] = 24
    manifest, segments = plan_document_segments(
        Planner(), raw(content=text), ["XLK"], {}, policy)
    assert manifest["complete"] is True
    assert manifest["segments"][0]["start"] == 0
    assert manifest["segments"][-1]["end"] == len(text)
    assert "".join(segment.content for segment in segments) == text
    assert all(row["end"] - row["start"] <= 24 for row in manifest["segments"])
    policy["segment_max_count"] = 1
    with pytest.raises(ValueError) as caught:
        plan_document_segments(Planner(), raw(content=text), ["XLK"], {}, policy)
    assert caught.value.code == "SEGMENT_LIMIT_EXCEEDED"


def test_frozen_source_index_keeps_every_version_without_invented_publication():
    from trading_system.research_snapshot import research_source_index
    from dataclasses import replace
    first = raw()
    second = replace(raw('version-2'), published='', meta={})
    result = research_source_index([first, second])
    assert [row['id'] for row in result] == ['source-1', 'version-2']
    assert result[0]['completeness'] == 'complete'
    assert result[1]['published_at'] is None and result[1]['completeness'] == 'unknown'
    assert result[0]['content_sha256'] == result[1]['content_sha256']
    with pytest.raises(ValueError, match='DUPLICATE'):
        research_source_index([first, first])
