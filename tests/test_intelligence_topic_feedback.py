"""Targeted validation feedback uses actual local HTTP and original receipts.

All sources and model answers are TEST_FIXTURE data. These tests establish
contract/ledger/replay behavior, not production-model repair success rates.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from trading_system.intelligence import topic as module
from trading_system.intelligence.contracts import IntelligenceError, digest
from trading_system.llm.model_budget import CONTEXT_TOKENS, MAX_OUTPUT_TOKENS, count_json_tokens
from test_intelligence_topic_safety_integration import (
    SAFE_QUOTE, _analysis, _claim, _payload, _review, _service, local_topic,
)


OTHER_QUOTE = "TEST_FIXTURE NVDA reports a different original event with a different reporting period."
TOPIC = {"material_policy": {"news_events": "required"}, "entity_ids": ["us:NVDA"]}
DOCUMENTS = [
    {"id": "docver_TEST_FIXTURE_first", "text": SAFE_QUOTE, "completeness": "complete"},
    {"id": "docver_TEST_FIXTURE_second", "text": OTHER_QUOTE, "completeness": "complete"},
]


def feedback(analysis, documents, code, *, resolution=True):
    return module.analysis_validation_feedback(analysis, documents, TOPIC["material_policy"], TOPIC["entity_ids"],
        code, resolution_enabled=resolution)


def test_wrong_document_feedback_identifies_only_the_rejected_pair_without_reassignment():
    claim = _claim(DOCUMENTS[0], SAFE_QUOTE, claim_id="claim-target", kind="fact")
    claim["supports"].append({"document_id": DOCUMENTS[0]["id"], "quote": OTHER_QUOTE})
    analysis = _analysis([claim])
    before = deepcopy((analysis, DOCUMENTS))
    diagnostic = feedback(analysis, DOCUMENTS, "CITATION_INVALID")
    assert diagnostic == feedback(analysis, DOCUMENTS, "CITATION_INVALID")
    bad = diagnostic["failure"]
    assert diagnostic["schema"] == module.ANALYSIS_FEEDBACK_CONTRACT
    assert bad["issue"] == "QUOTE_NOT_IN_CITED_DOCUMENT" and bad["claim_index"] == 0
    assert bad["claim_id"] == "claim-target" and bad["relation"] == "supports" and bad["reference_index"] == 1
    assert bad["document_id"] == DOCUMENTS[0]["id"]
    assert bad["quote_sha256"] == hashlib.sha256(OTHER_QUOTE.encode()).hexdigest()
    assert DOCUMENTS[1]["id"] not in json.dumps(diagnostic)
    assert (analysis, DOCUMENTS) == before
    with pytest.raises(IntelligenceError) as error:
        module.validate_analysis(analysis, DOCUMENTS, TOPIC["material_policy"], TOPIC["entity_ids"])
    assert error.value.code == "CITATION_INVALID"


@pytest.mark.parametrize("relation", ["supports", "refutes"])
def test_partial_fact_feedback_does_not_relabel_or_upgrade_a_source(relation):
    documents = deepcopy(DOCUMENTS); documents[1]["completeness"] = "partial"
    claim = _claim(documents[0], SAFE_QUOTE, kind="fact")
    claim[relation].append({"document_id": documents[1]["id"], "quote": OTHER_QUOTE})
    analysis = _analysis([claim]); before = deepcopy((analysis, documents))
    diagnostic = feedback(analysis, documents, "INCOMPLETE_FACT_SOURCE")
    bad = diagnostic["failure"]
    assert bad["issue"] == "FACT_SOURCE_NOT_COMPLETE" and bad["relation"] == relation
    assert bad["document_id"] == documents[1]["id"] and bad["cited_document_completeness"] == "partial"
    assert bad["claim_kind"] == "fact" and (analysis, documents) == before
    with pytest.raises(IntelligenceError) as error:
        module.validate_analysis(analysis, documents, TOPIC["material_policy"], TOPIC["entity_ids"])
    assert error.value.code == "INCOMPLETE_FACT_SOURCE"


def test_feedback_observes_the_existing_nbsp_contract_without_expanding_it():
    quote = "TEST_FIXTURE NVDA result is 10\u00a0million."
    documents = [{"id": DOCUMENTS[0]["id"], "text": quote, "completeness": "partial"}]
    analysis = _analysis([_claim(documents[0], quote.replace("\u00a0", " "), kind="fact")])
    assert feedback(analysis, documents, "INCOMPLETE_FACT_SOURCE")["failure"]["issue"] == "FACT_SOURCE_NOT_COMPLETE"
    exact = feedback(analysis, documents, "CITATION_INVALID", resolution=False)
    assert exact["failure"]["issue"] == "QUOTE_NOT_IN_CITED_DOCUMENT"
    assert analysis["conclusions"][0]["supports"][0]["quote"] != quote


def test_feedback_is_bounded_and_never_copies_long_source_text_or_claims():
    huge = "TEST_FIXTURE_DO_NOT_COPY_" * 10000
    analysis = _analysis([_claim(DOCUMENTS[0], huge, claim_id="c" * 1000)])
    diagnostic = feedback(analysis, DOCUMENTS, "CITATION_INVALID")
    assert len(json.dumps(diagnostic)) < 1200 and huge not in json.dumps(diagnostic)
    assert len(diagnostic["failure"]["claim_id"]) == 128 and diagnostic["failure"]["claim_id_truncated"]
    assert diagnostic["failure"]["quote_characters"] == len(huge)


def test_legacy_policy_and_prompt_bytes_do_not_gain_new_feedback():
    legacy = {key: value for key, value in module.DEFAULT_POLICY.items()
        if key not in {"analysis_feedback_contract", "model_snapshot_view_contract", "model_transport_contract"}}
    legacy["version"] = "2026-11-07.1"
    parsed = module.trusted_policy_value(legacy)
    assert "analysis_feedback_contract" not in parsed
    assert digest(parsed) == "6b1e43162f6447c82f1bf51f117c91de6455691a56d2636d5b3fe60ed5188187"
    assert module.TopicResearch._analysis_system({"policy": parsed}) == module.LEGACY_ANALYSIS_SYSTEM
    assert hashlib.sha256(module.LEGACY_ANALYSIS_SYSTEM.encode()).hexdigest() == (
        "d0a6e1807f1d61c97570f69253ba63fc20c25d0d13092508db4b0709df9f05a7")
    assert module.trusted_policy()["analysis_feedback_contract"] == module.ANALYSIS_FEEDBACK_CONTRACT
    assert "EVERY supports and refutes document must have completeness=complete" in module.ANALYSIS_SYSTEM
    assert "Never relabel a factual assertion" in module.ANALYSIS_SYSTEM


@pytest.mark.parametrize("contract", ["unknown", "", 1, [], {}])
def test_unknown_feedback_contract_is_rejected_before_any_job(contract):
    with pytest.raises(IntelligenceError) as error:
        module.trusted_policy_value({**module.DEFAULT_POLICY, "analysis_feedback_contract": contract})
    assert error.value.code == "INVALID_STARTUP"


def _wrong_document_service(h, *, correct=True):
    service, _, job = _service(h, [
        ("first_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"]),
        ("second_original", OTHER_QUOTE, ["news_events"], ["topic_analysis"]),
    ])
    def reply(wire, payload):
        if wire["reasoning_effort"] != "high":
            return _review()
        documents = {document["source_id"]: document for document in payload["documents"]}
        first, second = documents["first_original"], documents["second_original"]
        corrected = False
        if "previous_analysis" in payload:
            bad = payload["validation_diagnostic"]["failure"]
            assert bad["issue"] == "QUOTE_NOT_IN_CITED_DOCUMENT" and bad["document_id"] == first["id"]
            assert bad["claim_id"] == "claim-wrong-source" and bad["reference_index"] == 0
            corrected = correct
        return _analysis([_claim(second if corrected else first, OTHER_QUOTE,
            claim_id="claim-wrong-source", kind="fact")])
    h.state["reply"] = reply
    return service, job


def test_actual_reanswer_receives_bad_reference_and_report_reconstructs_feedback(local_topic):
    h = local_topic
    service, job = _wrong_document_service(h)
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    requests = h.state["model_requests"]
    assert [wire["reasoning_effort"] for wire in requests] == ["high", "high", "max"]
    first, second = _payload(requests[0]), _payload(requests[1])
    assert first["documents"] == second["documents"] and first["snapshot"] == second["snapshot"]
    assert "previous_analysis" not in first and second["previous_analysis"]["conclusions"][0]["kind"] == "fact"
    directory = service.store.directory(job["job_id"])
    marker = json.loads((directory / "citation-repair.json").read_text())
    assert marker["schema"] == "tiger.topic-citation-repair/v2"
    assert marker["validation_diagnostic"] == second["validation_diagnostic"]
    attempts = service.store.attempts(job["job_id"])
    assert marker["original_request_sha256"] == attempts[0]["request_sha"]
    assert marker["original_response_id"] == attempts[0]["receipt"]["response_id"]
    original = json.loads((directory / attempts[0]["receipt"]["response_ref"]).read_text())
    raw = json.loads(original["response"]["choices"][0]["message"]["content"])
    assert raw == second["previous_analysis"] and raw["conclusions"][0]["supports"][0]["document_id"] != report["analysis"]["conclusions"][0]["supports"][0]["document_id"]
    assert all(attempt["state"] == "settled" for attempt in attempts)
    assert sum(attempt["receipt"]["usage"]["total_tokens"] for attempt in attempts) == 150
    for wire in requests:
        assert wire["max_tokens"] == min(MAX_OUTPUT_TOKENS, CONTEXT_TOKENS - count_json_tokens(wire))
        assert wire["max_tokens"] > 300000
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]
    original_marker = (directory / "citation-repair.json").read_bytes()
    for fault in ("document", "response", "removed"):
        changed = deepcopy(marker)
        if fault == "document":
            changed["validation_diagnostic"]["failure"]["document_id"] = report["analysis"]["conclusions"][0]["supports"][0]["document_id"]
        elif fault == "response":
            changed["original_response_id"] = attempts[1]["receipt"]["response_id"]
        if fault == "removed":
            (directory / "citation-repair.json").unlink()
        else:
            changed["binding_sha256"] = digest({key: value for key, value in changed.items() if key != "binding_sha256"})
            (directory / "citation-repair.json").write_text(json.dumps(changed))
        with pytest.raises(IntelligenceError) as error:
            service.report(job["job_id"])
        assert error.value.code == "HASH_MISMATCH"
        (directory / "citation-repair.json").write_bytes(original_marker)
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]
    assert len(h.state["model_requests"]) == 3


@pytest.mark.parametrize("correct", [True, False])
def test_actual_partial_fact_reanswer_keeps_gaps_or_fails_without_relabeling(local_topic, correct):
    h = local_topic
    service, _, job = _service(h, [("partial_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"])])
    # Explicit preview markup makes actual builtin extraction partial while
    # retaining its visible quote. No source contract or parser flag is faked.
    h.state["pages"]["/partial_original"] = (
        '<html><head><title>TEST_FIXTURE partial</title><script type="application/ld+json">'
        '{"isAccessibleForFree":false}</script></head><body><article><p>' + SAFE_QUOTE + '</p></article></body></html>')
    def reply(wire, payload):
        if wire["reasoning_effort"] != "high":
            return _review()
        document = payload["documents"][0]
        assert document["completeness"] == "partial" and SAFE_QUOTE in document["text"]
        if "previous_analysis" in payload:
            bad = payload["validation_diagnostic"]["failure"]
            assert bad["issue"] == "FACT_SOURCE_NOT_COMPLETE" and bad["cited_document_completeness"] == "partial"
            if correct:
                return _analysis([], unknowns=["TEST_FIXTURE complete original evidence is unavailable; the asserted fact remains unknown."])
        return _analysis([_claim(document, SAFE_QUOTE, kind="fact")])
    h.state["reply"] = reply
    result = service.run(job["job_id"])
    attempts = service.store.attempts(job["job_id"])
    assert all(attempt["state"] == "settled" for attempt in attempts)
    if correct:
        assert result["state"] == "partial", result
        assert result["analysis"]["conclusions"] == [] and result["analysis"]["unknowns"]
        assert service.report(job["job_id"])["receipt_sha256"] == result["receipt_sha256"]
        assert len(h.state["model_requests"]) == 3
    else:
        assert result["state"] == "failed" and result["error"]["code"] == "INCOMPLETE_FACT_SOURCE", result
        assert len(h.state["model_requests"]) == 2
        assert not (service.store.directory(job["job_id"]) / "report.json").exists()
    original = json.loads((service.store.directory(job["job_id"]) / attempts[0]["receipt"]["response_ref"]).read_text())
    assert json.loads(original["response"]["choices"][0]["message"]["content"])["conclusions"][0]["kind"] == "fact"


def test_detailed_feedback_resume_reuses_both_paid_shape_responses(local_topic, monkeypatch):
    h = local_topic
    service, job = _wrong_document_service(h)
    atomic = module.atomic_json
    def interrupted(root, name, value, **kwargs):
        if name.startswith("io-") and "previous_analysis" in value["inputs"]["request"]["messages"][1]["content"]:
            raise KeyboardInterrupt
        return atomic(root, name, value, **kwargs)
    monkeypatch.setattr(module, "atomic_json", interrupted)
    with pytest.raises(KeyboardInterrupt): service.run(job["job_id"])
    attempts = service.store.attempts(job["job_id"])
    original_ids = [attempt["attempt_id"] for attempt in attempts]
    assert len(h.state["model_requests"]) == len(attempts) == 2
    monkeypatch.setattr(module, "atomic_json", atomic)
    result = service.run(job["job_id"])
    assert result["state"] == "completed", result
    assert len(h.state["model_requests"]) == 3 and h.state["model_requests"][-1]["reasoning_effort"] == "max"
    assert [attempt["attempt_id"] for attempt in service.store.attempts(job["job_id"])[:2]] == original_ids
    assert service.report(job["job_id"])["receipt_sha256"] == result["receipt_sha256"]


@pytest.mark.parametrize("legacy_contract", ["missing", None])
def test_actual_legacy_job_keeps_code_only_v1_repair_and_original_prompt(local_topic, monkeypatch, legacy_contract):
    h = local_topic
    with monkeypatch.context() as legacy:
        if legacy_contract == "missing":
            legacy.delitem(module.DEFAULT_POLICY, "analysis_feedback_contract")
        else:
            legacy.setitem(module.DEFAULT_POLICY, "analysis_feedback_contract", None)
        service, _, job = _service(h, [
            ("first_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"]),
            ("second_original", OTHER_QUOTE, ["news_events"], ["topic_analysis"]),
        ])
    def reply(wire, payload):
        if wire["reasoning_effort"] != "high":
            return _review()
        documents = {document["source_id"]: document for document in payload["documents"]}
        if "previous_analysis" in payload:
            assert payload["validation_diagnostic"] == {"code": "CITATION_INVALID"}
            selected = documents["second_original"]
        else:
            selected = documents["first_original"]
        assert wire["messages"][0]["content"] == module.LEGACY_ANALYSIS_SYSTEM + "\nReturn exactly one JSON object. JSON only."
        return _analysis([_claim(selected, OTHER_QUOTE)])
    h.state["reply"] = reply
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    marker = json.loads((service.store.directory(job["job_id"]) / "citation-repair.json").read_text())
    assert marker["schema"] == "tiger.topic-citation-repair/v1"
    assert marker["validation_diagnostic"] == {"code": "CITATION_INVALID"}
    assert "original_request_sha256" not in marker and "original_response_id" not in marker
    assert len(h.state["model_requests"]) == 3
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]


def test_actual_feedback_skips_legal_nbsp_and_points_to_the_later_wrong_document(local_topic):
    h = local_topic
    original_quote = "TEST_FIXTURE NVDA conference call begins at 2 p.m.\u00a0PT."
    service, _, job = _service(h, [
        ("first_original", original_quote, ["news_events"], ["topic_analysis"]),
        ("second_original", OTHER_QUOTE, ["news_events"], ["topic_analysis"]),
    ])
    def reply(wire, payload):
        if wire["reasoning_effort"] != "high":
            return _review()
        documents = {document["source_id"]: document for document in payload["documents"]}
        first, second = documents["first_original"], documents["second_original"]
        claim = _claim(first, original_quote.replace("\u00a0", " "), claim_id="claim-combined")
        selected = first
        if "previous_analysis" in payload:
            bad = payload["validation_diagnostic"]["failure"]
            assert bad["issue"] == "QUOTE_NOT_IN_CITED_DOCUMENT" and bad["reference_index"] == 1
            assert bad["claim_id"] == "claim-combined" and bad["document_id"] == first["id"]
            assert bad["quote_sha256"] == hashlib.sha256(OTHER_QUOTE.encode()).hexdigest()
            assert payload["previous_analysis"]["conclusions"][0]["supports"][0]["quote"] == original_quote.replace("\u00a0", " ")
            selected = second
        claim["supports"].append({"document_id": selected["id"], "quote": OTHER_QUOTE})
        return _analysis([claim])
    h.state["reply"] = reply
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    references = report["analysis"]["conclusions"][0]["supports"]
    assert references[0]["quote"] == original_quote
    assert references[0]["document_id"] != references[1]["document_id"]
    assert report["citation_resolutions"] and len(h.state["model_requests"]) == 3
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]
