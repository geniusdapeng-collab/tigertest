"""Candidate ID binding: actual local HTTP, no paid provider or invented usage.

Every response and source in these tests is explicitly TEST_FIXTURE. The actual
topic serializer, schema repair, usage ledger and report verifier run unchanged.
The assertions do not claim that a production model will always repair itself.
"""
from copy import deepcopy
import json

import pytest

from test_intelligence_topic_safety_integration import (
    SAFE_QUOTE, _analysis, _claim, _payload, _review, _service, local_topic,
)
from trading_system.intelligence import topic as module
from trading_system.intelligence.contracts import IntelligenceError, digest
from trading_system.llm.deepseek_required import validate_schema
from trading_system.llm.model_budget import (
    CONTEXT_TOKENS, MAX_OUTPUT_TOKENS, TokenBudgetError, count_json_tokens,
)
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


DOCUMENTS = [
    {"id": "docver_TEST_FIXTURE_a", "doc_id": "intel_TEST_FIXTURE_a", "version": "TEST_FIXTURE-version-a",
        "text": SAFE_QUOTE, "completeness": "complete"},
    {"id": "docver_TEST_FIXTURE_b", "doc_id": "intel_TEST_FIXTURE_b", "version": "TEST_FIXTURE-version-b",
        "text": SAFE_QUOTE, "completeness": "complete"},
]
TOPIC = {"entity_ids": ["us:NVDA"], "material_policy": {
    "news_events": {"status": "required", "reason": "TEST_FIXTURE exact source question"},
    "formal_research": {"status": "not_applicable", "reason": "TEST_FIXTURE does not ask for this material"}}}


def _wire_schema(wire):
    raw = wire["messages"][1]["content"].split("\nJSON schema:\n", 1)[1]
    return json.loads(raw.split("\nJSON field guidance:\n", 1)[0])


def _citation_enum(schema, relation="supports"):
    return schema["properties"]["conclusions"]["items"]["properties"][relation]["items"]["properties"]["document_id"]["enum"]


def test_dynamic_schema_binds_only_immutable_version_ids_and_active_scope_without_mutation():
    original = deepcopy(module.ANALYSIS_SCHEMA)
    documents, topic = deepcopy(DOCUMENTS), deepcopy(TOPIC)
    schema = module.analysis_schema(documents, topic)
    properties = schema["properties"]["conclusions"]["items"]["properties"]
    expected = [document["id"] for document in documents]
    assert _citation_enum(schema) == _citation_enum(schema, "refutes") == expected
    assert not set(expected).intersection(document["doc_id"] for document in documents)
    assert properties["entity_id"]["enum"] == ["", "us:NVDA"]
    assert properties["dimension_id"]["enum"] == ["news_events"]
    assert module.ANALYSIS_SCHEMA == original and documents == DOCUMENTS and topic == TOPIC
    assert "documents[].id (docver_*)" in module.ANALYSIS_SYSTEM
    assert "logical doc_id" in module.ANALYSIS_SYSTEM


@pytest.mark.parametrize("damage", ["logical_id", "other_document", "outside_entity", "inactive_material"])
def test_dynamic_schema_rejects_logical_foreign_and_out_of_scope_values(damage):
    schema = module.analysis_schema(DOCUMENTS, TOPIC)
    analysis = _analysis([_claim(DOCUMENTS[0], SAFE_QUOTE)])
    if damage == "logical_id":
        analysis["conclusions"][0]["supports"][0]["document_id"] = DOCUMENTS[0]["doc_id"]
    elif damage == "other_document":
        analysis["conclusions"][0]["supports"][0]["document_id"] = "docver_TEST_FIXTURE_foreign"
    elif damage == "outside_entity":
        analysis["conclusions"][0]["entity_id"] = "us:OUTSIDE"
    else:
        analysis["conclusions"][0]["dimension_id"] = "formal_research"
    with pytest.raises(RequiredModelUnavailable) as error:
        validate_schema(analysis, schema)
    assert error.value.diagnostic["code"] == "OUTPUT_SCHEMA_MISMATCH"
    assert error.value.diagnostic["schema_keyword"] == "enum"


def test_empty_evidence_schema_allows_honest_unknowns_but_no_invented_conclusion():
    schema = module.analysis_schema([], TOPIC)
    assert schema["properties"]["conclusions"]["maxItems"] == 0
    honest = _analysis([], unknowns=["TEST_FIXTURE no eligible originals were observed."])
    validate_schema(honest, schema)
    with pytest.raises(RequiredModelUnavailable) as error:
        validate_schema(_analysis([_claim(DOCUMENTS[0], SAFE_QUOTE)]), schema)
    assert error.value.diagnostic["code"] == "OUTPUT_SCHEMA_MISMATCH"
    assert error.value.diagnostic["schema_keyword"] == "maxItems"


def test_strict_citation_validator_never_rewrites_logical_ids():
    analysis = _analysis([_claim(DOCUMENTS[0], SAFE_QUOTE)])
    analysis["conclusions"][0]["supports"][0]["document_id"] = DOCUMENTS[0]["doc_id"]
    original = deepcopy(analysis)
    with pytest.raises(IntelligenceError) as error:
        module.validate_analysis(analysis, DOCUMENTS, TOPIC["material_policy"], TOPIC["entity_ids"])
    assert error.value.code == "CITATION_INVALID" and analysis == original


def test_real_http_logical_id_schema_repair_preserves_original_responses_and_full_window(local_topic):
    h = local_topic
    service, _, job = _service(h, [("public_original", SAFE_QUOTE,
        ["news_events"], ["topic_analysis"])])
    generation = []
    def reply(wire, payload):
        if wire["reasoning_effort"] != "high":
            return _review()
        generation.append(payload)
        document = payload["documents"][0]
        claim = _claim(document, SAFE_QUOTE)
        if len(generation) == 1:
            claim["supports"][0]["document_id"] = document["doc_id"]
        return _analysis([claim])
    h.state["reply"] = reply
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    requests = h.state["model_requests"]
    assert len(requests) == 3 and [wire["reasoning_effort"] for wire in requests] == ["high", "high", "max"]
    assert requests[0] == requests[1]  # Existing bounded schema repair, no evidence trimming or output-cap reduction.
    document = generation[0]["documents"][0]
    assert document["id"].startswith("docver_") and document["doc_id"].startswith("intel_")
    assert _citation_enum(_wire_schema(requests[0])) == [document["id"]]
    for wire in requests:
        assert wire["max_tokens"] == min(MAX_OUTPUT_TOKENS, CONTEXT_TOKENS - count_json_tokens(wire))
        assert wire["max_tokens"] > 300000
    attempts = service.store.attempts(job["job_id"])
    assert len(attempts) == 3 and all(attempt["state"] == "settled" for attempt in attempts)
    assert sum(attempt["receipt"]["usage"]["total_tokens"] for attempt in attempts) == 150
    first_saved = json.loads((service.store.directory(job["job_id"]) / attempts[0]["receipt"]["response_ref"]).read_text())
    first_content = json.loads(first_saved["response"]["choices"][0]["message"]["content"])
    assert first_content["conclusions"][0]["supports"][0]["document_id"] == document["doc_id"]
    assert report["analysis"]["conclusions"][0]["supports"][0]["document_id"] == document["id"]
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]


def test_real_http_repeated_wrong_logical_id_fails_and_never_becomes_a_report(local_topic):
    h = local_topic
    service, _, job = _service(h, [("public_original", SAFE_QUOTE,
        ["news_events"], ["topic_analysis"])])
    def reply(_wire, payload):
        document = payload["documents"][0]
        claim = _claim(document, SAFE_QUOTE)
        claim["supports"][0]["document_id"] = document["doc_id"]
        return _analysis([claim])
    h.state["reply"] = reply
    result = service.run(job["job_id"])
    assert result["state"] == "failed" and result["error"]["code"] == "MODEL_REQUIRED_FAILED"
    assert len(h.state["model_requests"]) == 2
    assert all(attempt["state"] == "settled" for attempt in service.store.attempts(job["job_id"]))
    assert not (service.store.directory(job["job_id"]) / "analysis-0.json").exists()
    assert not (service.store.directory(job["job_id"]) / "report.json").exists()
    assert service.run(job["job_id"])["state"] == "failed"
    assert len(h.state["model_requests"]) == 2  # Same original failed identity does not resend paid responses.


def test_source_boundary_batches_and_synthesis_use_their_exact_own_id_enums(local_topic, monkeypatch):
    h = local_topic
    second_quote = "TEST_FIXTURE NVDA also disclosed a documented contrary observation."
    service, _, job = _service(h, [
        ("public_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"]),
        ("second_original", second_quote, ["news_events"], ["topic_analysis"]),
    ])
    original_plan = module.TopicModelClient.plan_chat
    def window(self, **kwargs):
        if len(kwargs.get("public_payload", {}).get("documents", [])) > 1:
            raise TokenBudgetError("INPUT_CONTEXT_EXCEEDED")
        return original_plan(self, **kwargs)
    monkeypatch.setattr(module.TopicModelClient, "plan_chat", window)
    def reply(wire, payload):
        if wire["reasoning_effort"] != "high":
            return _review()
        if "source_analyses" in payload:
            return _analysis([claim for part in payload["source_analyses"] for claim in part["conclusions"]])
        return _analysis([_claim(document, SAFE_QUOTE if document["source_id"] == "public_original" else second_quote,
            claim_id="claim-" + document["source_id"]) for document in payload["documents"]])
    h.state["reply"] = reply
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    assert len(h.state["model_requests"]) == 6
    generation = [wire for wire in h.state["model_requests"] if wire["reasoning_effort"] == "high"]
    assert len(generation) == 3
    for wire in generation:
        payload = _payload(wire)
        supplied = payload.get("documents", payload.get("document_refs"))
        expected_ids = {document["id"] for document in supplied}
        schema = _wire_schema(wire)
        assert set(_citation_enum(schema)) == set(_citation_enum(schema, "refutes")) == expected_ids
        properties = schema["properties"]["conclusions"]["items"]["properties"]
        assert properties["entity_id"]["enum"] == ["", "us:NVDA"]
        assert properties["dimension_id"]["enum"] == ["news_events"]
    assert [len(_citation_enum(_wire_schema(wire))) for wire in generation] == [1, 1, 2]
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]


NBSP_QUOTE = "TEST_FIXTURE NVDA conference call begins at 2 p.m.\u00a0PT."


def _nbsp_reply(wire, payload, *, correct_reanswer):
    if wire["reasoning_effort"] != "high":
        return _review()
    document = payload["documents"][0]
    assert NBSP_QUOTE in document["text"]
    quote = NBSP_QUOTE if correct_reanswer and "previous_analysis" in payload else NBSP_QUOTE.replace("\u00a0", " ")
    return _analysis([_claim(document, quote)])


def _legacy_nbsp_service(harness, monkeypatch):
    # Explicitly admitted legacy exact behavior never gains derivatives from a
    # mutable report field or a new process default.
    with monkeypatch.context() as legacy:
        legacy.setitem(module.DEFAULT_POLICY, "citation_resolution_contract", None)
        legacy.setitem(module.DEFAULT_POLICY, "analysis_feedback_contract", None)
        return _service(harness, [("public_original", NBSP_QUOTE,
            ["news_events"], ["topic_analysis"])])


@pytest.mark.parametrize("correct_reanswer", [True, False])
def test_real_http_nbsp_requires_actual_model_reanswer_or_fails_without_normalizing(local_topic, monkeypatch, correct_reanswer):
    h = local_topic
    service, _, job = _legacy_nbsp_service(h, monkeypatch)
    h.state["reply"] = lambda wire, payload: _nbsp_reply(wire, payload, correct_reanswer=correct_reanswer)
    result = service.run(job["job_id"])
    requests = h.state["model_requests"]
    assert len(requests) == (3 if correct_reanswer else 2)
    assert [wire["reasoning_effort"] for wire in requests[:2]] == ["high", "high"]
    original_payload, repair_payload = _payload(requests[0]), _payload(requests[1])
    assert repair_payload["documents"] == original_payload["documents"]
    assert repair_payload["snapshot"] == original_payload["snapshot"]
    assert repair_payload["validation_diagnostic"] == {"code": "CITATION_INVALID"}
    assert repair_payload["previous_analysis"]["conclusions"][0]["supports"][0]["quote"] == NBSP_QUOTE.replace("\u00a0", " ")
    assert NBSP_QUOTE in repair_payload["documents"][0]["text"]
    for wire in requests:
        assert wire["max_tokens"] == min(MAX_OUTPUT_TOKENS, CONTEXT_TOKENS - count_json_tokens(wire))
        assert wire["max_tokens"] > 300000
    marker = json.loads((service.store.directory(job["job_id"]) / "citation-repair.json").read_text())
    assert marker["original_analysis_sha256"] == digest(repair_payload["previous_analysis"])
    assert marker["validation_diagnostic"] == {"code": "CITATION_INVALID"}
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM topic_events WHERE job_id=? AND kind='citation_repair_requested'", (job["job_id"],)).fetchone()[0] == 1
    attempts = service.store.attempts(job["job_id"])
    assert len(attempts) == len(requests) and all(attempt["state"] == "settled" for attempt in attempts)
    original = json.loads((service.store.directory(job["job_id"]) / attempts[0]["receipt"]["response_ref"]).read_text())
    original_quote = json.loads(original["response"]["choices"][0]["message"]["content"])["conclusions"][0]["supports"][0]["quote"]
    assert original_quote == NBSP_QUOTE.replace("\u00a0", " ")  # First invalid response is preserved, never rewritten.
    if correct_reanswer:
        assert result["state"] == "completed", result
        assert result["analysis"]["conclusions"][0]["supports"][0]["quote"] == NBSP_QUOTE
        assert service.report(job["job_id"])["receipt_sha256"] == result["receipt_sha256"]
    else:
        assert result["state"] == "failed" and result["error"]["code"] == "CITATION_INVALID"
        assert not (service.store.directory(job["job_id"]) / "report.json").exists()
    posts_before_replay = len(requests)
    assert service.run(job["job_id"])["state"] == result["state"]
    assert len(h.state["model_requests"]) == posts_before_replay


def test_citation_repair_resume_reuses_original_response_and_sends_only_new_review(local_topic, monkeypatch):
    h = local_topic
    service, _, job = _legacy_nbsp_service(h, monkeypatch)
    h.state["reply"] = lambda wire, payload: _nbsp_reply(wire, payload, correct_reanswer=True)
    original_atomic = module.atomic_json
    def interrupted_io(root, name, value, **kwargs):
        if name.startswith("io-") and "previous_analysis" in value["inputs"]["request"]["messages"][1]["content"]:
            raise KeyboardInterrupt
        return original_atomic(root, name, value, **kwargs)
    monkeypatch.setattr(module, "atomic_json", interrupted_io)
    with pytest.raises(KeyboardInterrupt):
        service.run(job["job_id"])
    assert len(h.state["model_requests"]) == 2
    attempts = service.store.attempts(job["job_id"])
    assert len(attempts) == 2 and all(attempt["state"] == "settled" for attempt in attempts)
    original_attempt_ids = [attempt["attempt_id"] for attempt in attempts]
    monkeypatch.setattr(module, "atomic_json", original_atomic)
    result = service.run(job["job_id"])
    assert result["state"] == "completed", result
    assert result["analysis"]["conclusions"][0]["supports"][0]["quote"] == NBSP_QUOTE
    assert len(h.state["model_requests"]) == 3
    assert h.state["model_requests"][-1]["reasoning_effort"] == "max"
    assert [attempt["attempt_id"] for attempt in service.store.attempts(job["job_id"])[:2]] == original_attempt_ids
    assert service.report(job["job_id"])["receipt_sha256"] == result["receipt_sha256"]


def test_only_one_citation_reanswer_identity_is_available_across_all_source_batches(local_topic, monkeypatch):
    h = local_topic
    service, _, job = _service(h, [
        ("public_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"]),
        ("second_original", "TEST_FIXTURE NVDA documented a contrary event.", ["news_events"], ["topic_analysis"]),
    ])
    original_plan = module.TopicModelClient.plan_chat
    def window(self, **kwargs):
        if len(kwargs.get("public_payload", {}).get("documents", [])) > 1:
            raise TokenBudgetError("INPUT_CONTEXT_EXCEEDED")
        return original_plan(self, **kwargs)
    monkeypatch.setattr(module.TopicModelClient, "plan_chat", window)
    def reply(_wire, payload):
        document = payload["documents"][0]
        quote = document["text"] if "previous_analysis" in payload else "TEST_FIXTURE fabricated quote absent from the original"
        return _analysis([_claim(document, quote, claim_id="claim-" + document["source_id"])])
    h.state["reply"] = reply
    result = service.run(job["job_id"])
    assert result["state"] == "failed" and result["error"]["code"] == "CITATION_INVALID"
    assert len(h.state["model_requests"]) == 3  # First source, its repair, second source; no second repair/review.
    assert all(wire["reasoning_effort"] == "high" for wire in h.state["model_requests"])
    assert len(service.store.attempts(job["job_id"])) == 3
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM topic_events WHERE job_id=? AND kind='citation_repair_requested'", (job["job_id"],)).fetchone()[0] == 1
