"""AT39/AT42 contract checks; all evidence and model replies are TEST_FIXTURE.

These assertions prove scope, receipt and rejection mechanics, not a production
model's ability to classify every natural-language temporal statement.
"""
from copy import deepcopy
import json

import pytest

from test_intelligence_topic_safety_integration import _analysis, _claim, _payload, _service, local_topic
from trading_system.intelligence import topic as module
from trading_system.intelligence.contracts import IntelligenceError, canonical, digest
from trading_system.intelligence.monitoring import MonitoringStore
from trading_system.intelligence.topic_cli import validate_request
from trading_system.llm.deepseek_required import validate_schema
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


BEFORE = "TEST_FIXTURE NVDA will hold its conference call on November 20 at 2 p.m.\u00a0PT."
AFTER = "TEST_FIXTURE NVDA completed its conference call and published the recording."
DOCUMENTS = [{"id": "docver_TEST_FIXTURE_calendar", "text": BEFORE, "completeness": "complete"}]
TOPIC = {"entity_ids": ["us:NVDA"], "material_policy": {"news_events": "required"}, "max_conclusions": 3}


def checked_review(analysis, documents, *, phase="before_event", evidence_phase=None):
    ids = {doc["id"] for doc in documents}
    return {"accepted": True, "issues": [], "summary": "TEST_FIXTURE independent temporal checks.",
        "summary_supported": True, "counterevidence_preserved": True,
        "claim_checks": [{"claim_id": claim["id"], "assertion_kind": claim["kind"],
            "support_status": "supported", "asserted_phase": phase,
            "temporally_consistent": True, "rationale": "TEST_FIXTURE scope-bound review.",
            "evidence": [{"relation": relation, "reference_index": index, **ref,
                "source_phase": evidence_phase or phase,
                "rationale": "TEST_FIXTURE exact source wording retains event timing."}
                for relation in ("supports", "refutes") for index, ref in enumerate(claim[relation])
                if ref["document_id"] in ids]} for claim in analysis["conclusions"]]}


def test_max_three_is_frozen_in_create_identity_and_cannot_be_changed_on_replay(tmp_path):
    store = MonitoringStore(tmp_path / "state")
    request = {"question": "TEST_FIXTURE 最多三个结论", "idempotency_key": "three", "max_conclusions": 3}
    assert validate_request("create", request) == request
    topic = store.create_topic(**request)
    assert topic["max_conclusions"] == 3
    assert store.topic(topic["topic_id"], topic["version"]) == topic
    assert store.create_topic(**request) == topic
    with pytest.raises(IntelligenceError) as error:
        store.create_topic(**{**request, "max_conclusions": 4})
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    different = store.create_topic(**{**request, "idempotency_key": "four", "max_conclusions": 4})
    assert different["topic_id"] != topic["topic_id"]


@pytest.mark.parametrize("value", [True, 0, -1, 101, 3.0, "3", None])
def test_conclusion_limit_rejects_ambiguous_and_unbounded_values(tmp_path, value):
    with pytest.raises(IntelligenceError):
        validate_request("create", {"question": "TEST_FIXTURE", "idempotency_key": "k", "max_conclusions": value})
    with pytest.raises(IntelligenceError):
        MonitoringStore(tmp_path / "state").create_topic("TEST_FIXTURE", max_conclusions=value, idempotency_key="k")


def test_conclusion_limit_is_enforced_by_schema_and_deterministic_resume_gate():
    claims = [_claim(DOCUMENTS[0], BEFORE, claim_id=f"claim-{index}") for index in range(4)]
    schema = module.analysis_schema(DOCUMENTS, TOPIC)
    assert schema["properties"]["conclusions"]["maxItems"] == 3
    with pytest.raises(RequiredModelUnavailable):
        validate_schema(_analysis(claims), schema)
    with pytest.raises(IntelligenceError) as error:
        module.validate_analysis(_analysis(claims), DOCUMENTS, TOPIC["material_policy"], TOPIC["entity_ids"], max_conclusions=3)
    assert error.value.code == "CONCLUSION_LIMIT_EXCEEDED"


def test_accepted_flag_cannot_override_before_after_temporal_contradiction():
    from trading_system.intelligence.topic_semantics import validate_review
    analysis = _analysis([_claim(DOCUMENTS[0], BEFORE, kind="fact", text=AFTER)])
    review = checked_review(analysis, DOCUMENTS, phase="after_event", evidence_phase="before_event")
    with pytest.raises(IntelligenceError) as error:
        validate_review(review, analysis, DOCUMENTS)
    assert error.value.code == "TEMPORAL_CONTRADICTION"


@pytest.mark.parametrize("damage", ["omit", "duplicate", "wrong_doc", "wrong_quote", "wrong_index", "relabel", "unknown", "summary", "counterevidence"])
def test_review_requires_one_scope_bound_check_per_claim_and_original_reference(damage):
    from trading_system.intelligence.topic_semantics import validate_review
    analysis = _analysis([_claim(DOCUMENTS[0], BEFORE, kind="fact")])
    review = checked_review(analysis, DOCUMENTS)
    check = review["claim_checks"][0]
    if damage == "omit": review["claim_checks"] = []
    elif damage == "duplicate": review["claim_checks"].append(deepcopy(check))
    elif damage == "wrong_doc": check["evidence"][0]["document_id"] = "docver_TEST_FIXTURE_other"
    elif damage == "wrong_quote": check["evidence"][0]["quote"] = AFTER
    elif damage == "wrong_index": check["evidence"][0]["reference_index"] = 1
    elif damage == "relabel": check["assertion_kind"] = "opinion"
    elif damage == "unknown": check["support_status"] = "unknown"
    elif damage == "summary": review["summary_supported"] = False
    elif damage == "counterevidence": review["counterevidence_preserved"] = False
    with pytest.raises(IntelligenceError):
        validate_review(review, analysis, DOCUMENTS)


def test_partial_rss_stays_ineligible_and_same_document_nbsp_keeps_identity():
    from trading_system.intelligence.citation_resolution import derive_analysis
    raw = _analysis([_claim(DOCUMENTS[0], BEFORE.replace("\u00a0", " "), kind="fact")])
    original = deepcopy(raw)
    derived, mappings = derive_analysis(raw, DOCUMENTS)
    assert raw == original and len(mappings) == 1
    assert derived["conclusions"][0]["supports"][0] == {"document_id": DOCUMENTS[0]["id"], "quote": BEFORE}
    with pytest.raises(IntelligenceError) as error:
        module.validate_analysis(derived, [{**DOCUMENTS[0], "completeness": "partial"}], TOPIC["material_policy"], TOPIC["entity_ids"])
    assert error.value.code == "INCOMPLETE_FACT_SOURCE"
    wrong = [{**DOCUMENTS[0], "text": AFTER}, {"id": "docver_TEST_FIXTURE_other", "text": BEFORE, "completeness": "complete"}]
    with pytest.raises(IntelligenceError):
        derive_analysis(raw, wrong)


def strict_service(harness, *, pages=None, max_conclusions=3):
    return _service(harness, pages or [("calendar", BEFORE, ["news_events"], ["topic_analysis"])],
        strict_semantics=True, max_conclusions=max_conclusions)


def positive_reply(wire, payload):
    if wire["reasoning_effort"] == "high":
        document = payload["documents"][0]
        return _analysis([_claim(document, BEFORE.replace("\u00a0", " "), kind="fact", text=BEFORE)])
    return checked_review(payload["report"], payload["documents"])


def test_new_default_contract_max_three_nbsp_and_offline_report_are_bound_end_to_end(local_topic, monkeypatch):
    from trading_system.intelligence.topic_cli import execute
    h = local_topic
    service, topic, job = strict_service(h)
    assert job["policy"]["semantic_review_contract"] == module.SEMANTIC_REVIEW_CONTRACT
    h.state["reply"] = positive_reply
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    assert report["topic"]["max_conclusions"] == 3 and len(report["analysis"]["conclusions"]) == 1
    assert "最多 3 条" in report["readable_report"] and "事件前公告或预期" in report["readable_report"]
    assert len(h.state["model_requests"]) == 2
    for wire in h.state["model_requests"]:
        payload = _payload(wire)
        assert payload["topic"]["max_conclusions"] == 3
        if wire["reasoning_effort"] == "high":
            assert '"maxItems":3' in wire["messages"][1]["content"]
        else:
            assert payload["review_contract"] == module.SEMANTIC_REVIEW_CONTRACT
            assert payload["review_scope"] == "global"
            assert payload["citation_resolutions"][0]["mappings"][0]["document_id"] == payload["documents"][0]["id"]
    attempts = service.store.attempts(job["job_id"])
    directory = service.store.directory(job["job_id"])
    raw = json.loads((directory / attempts[0]["receipt"]["response_ref"]).read_text())
    assert BEFORE.replace("\u00a0", " ") in raw["response"]["choices"][0]["message"]["content"]
    monkeypatch.setattr(module.TopicModelClient, "_post_json", lambda *_args, **_kwargs: pytest.fail("Offline report sent model request"))
    result = execute("report", {"job_id": job["job_id"]}, state_root=service.root,
        archive_root=service.archive_root, service_factory=lambda *_args, **_kwargs: service)
    assert result["operation_ok"] and result["data"]["report"] == report
    assert service.submit(job["request"])["job_id"] == job["job_id"]
    assert not list(service.root.glob("**/*paper-ledger*"))


@pytest.mark.parametrize("damage,code", [("temporal", "TEMPORAL_CONTRADICTION"),
    ("unknown_timing", "TEMPORAL_UNVERIFIED"), ("relabel", "REVIEW_REJECTED"),
    ("contradicted", "REVIEW_REJECTED"), ("false_summary", "REVIEW_REJECTED")])
def test_actual_review_rejects_pre_post_conflict_even_with_acceptance_flag(local_topic, damage, code):
    h = local_topic
    service, _, job = strict_service(h)
    def reply(wire, payload):
        if wire["reasoning_effort"] == "high":
            return _analysis([_claim(payload["documents"][0], BEFORE, kind="fact", text=AFTER)])
        review = checked_review(payload["report"], payload["documents"])
        check = review["claim_checks"][0]
        if damage == "temporal": check["asserted_phase"] = "after_event"
        elif damage == "unknown_timing": check["asserted_phase"] = "unknown"
        elif damage == "relabel": check["assertion_kind"] = "forecast"
        elif damage == "contradicted": check["support_status"] = "contradicted"
        else: review["summary_supported"] = False
        return review
    h.state["reply"] = reply
    result = service.run(job["job_id"])
    assert result["state"] == "failed" and result["error"]["code"] == code, result
    assert len(h.state["model_requests"]) == 2
    assert all(attempt["state"] == "settled" for attempt in service.store.attempts(job["job_id"]))
    assert not (service.store.directory(job["job_id"]) / "report.json").exists()
    with pytest.raises(IntelligenceError):
        service.report(job["job_id"])


@pytest.mark.parametrize("repair", [True, False])
def test_max_three_uses_actual_same_request_schema_reanswer_without_slicing(local_topic, repair):
    h = local_topic
    service, _, job = strict_service(h)
    generated = []
    def reply(wire, payload):
        if wire["reasoning_effort"] != "high":
            return checked_review(payload["report"], payload["documents"])
        generated.append(wire)
        count = 3 if repair and len(generated) > 1 else 4
        return _analysis([_claim(payload["documents"][0], BEFORE, claim_id=f"event-{index}", kind="fact", text=BEFORE)
            for index in range(count)])
    h.state["reply"] = reply
    result = service.run(job["job_id"])
    assert len(generated) == 2 and generated[0] == generated[1]
    if repair:
        assert result["state"] == "completed" and len(result["analysis"]["conclusions"]) == 3
        assert service.report(job["job_id"]) == result
    else:
        assert result["state"] == "failed" and result["error"]["code"] == "MODEL_REQUIRED_FAILED"
        assert not (service.store.directory(job["job_id"]) / "analysis-0.json").exists()
    directory = service.store.directory(job["job_id"])
    original = json.loads((directory / service.store.attempts(job["job_id"])[0]["receipt"]["response_ref"]).read_text())
    assert len(json.loads(original["response"]["choices"][0]["message"]["content"])["conclusions"]) == 4


@pytest.mark.parametrize("fault", ["review_phase", "review_omitted", "review_reference", "limit", "extra_claim"])
def test_coherently_rehashed_report_cannot_change_review_or_user_constraint(local_topic, fault):
    h = local_topic
    service, _, job = strict_service(h)
    h.state["reply"] = positive_reply
    report = deepcopy(service.run(job["job_id"]))
    assert report["state"] == "completed", report
    if fault == "review_phase": report["review"]["claim_checks"][0]["asserted_phase"] = "after_event"
    elif fault == "review_omitted": report["review"].pop("claim_checks")
    elif fault == "review_reference": report["review"]["claim_checks"][0]["evidence"][0]["quote"] = AFTER
    elif fault == "limit": report["topic"]["max_conclusions"] = 4
    else:
        original = report["analysis"]["conclusions"][0]
        report["analysis"]["conclusions"] = [{**original, "id": f"extra-{index}"} for index in range(4)]
    if fault != "extra_claim":
        report["readable_report"] = module.render_report(report, service._service().archive.snapshot_documents(report["snapshot_ids"][-1]))
    report["receipt_sha256"] = digest({k: v for k, v in report.items() if k != "receipt_sha256"})
    (service.store.directory(job["job_id"]) / "report.json").write_bytes(canonical(report) + b"\n")
    service.store.update(job["job_id"], report_sha256=report["receipt_sha256"])
    with pytest.raises(IntelligenceError):
        service.report(job["job_id"])
    assert len(h.state["model_requests"]) == 2


def test_source_boundary_synthesis_and_every_review_keep_max_three(local_topic, monkeypatch):
    from trading_system.llm.model_budget import TokenBudgetError
    h = local_topic
    service, _, job = strict_service(h, pages=[(name, BEFORE, ["news_events"], ["topic_analysis"])
        for name in ("calendar_a", "calendar_b")])
    original = module.TopicModelClient.plan_chat
    def split(self, **kwargs):
        if len(kwargs.get("public_payload", {}).get("documents", [])) > 1:
            raise TokenBudgetError("INPUT_CONTEXT_EXCEEDED")
        return original(self, **kwargs)
    monkeypatch.setattr(module.TopicModelClient, "plan_chat", split)
    def reply(wire, payload):
        assert payload["topic"]["max_conclusions"] == 3
        if wire["reasoning_effort"] == "high":
            if "source_analyses" in payload:
                return _analysis([claim for part in payload["source_analyses"] for claim in part["conclusions"]])
            doc = payload["documents"][0]
            return _analysis([_claim(doc, BEFORE, claim_id=doc["id"], kind="fact", text=BEFORE)])
        docs = payload.get("documents", [{"id": ref["document_id"]} for claim in payload["report"]["conclusions"]
            for ref in claim["supports"] + claim["refutes"]])
        return checked_review(payload["report"], docs)
    h.state["reply"] = reply
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    assert len(h.state["model_requests"]) == 6
    assert [ _payload(wire)["review_scope"] for wire in h.state["model_requests"] if wire["reasoning_effort"] == "max"] == ["source_batch", "source_batch", "global"]
    assert service.report(job["job_id"]) == report


@pytest.mark.parametrize("fault", [None, "promoted_scope", "source_review", "document_reference", "duplicate_part", "quotation"])
def test_nested_source_reviews_preserve_local_scope_for_cross_branch_claim(local_topic, monkeypatch, fault):
    from trading_system.llm.model_budget import TokenBudgetError
    h = local_topic
    service, _, job = strict_service(h, pages=[(name, BEFORE, ["news_events"], ["topic_analysis"])
        for name in ("calendar_a", "calendar_b", "calendar_c", "calendar_d")])
    # Four source batches plus their three synthesis calls in each model stage.
    # This test-only trusted policy admits all 14 genuine loopback responses.
    with service.store.connection() as db:
        frozen = service.store._get(db, job["job_id"])
    assert frozen["policy"]["model_attempts"] == 12
    # Re-admit a separate fixture identity rather than mutate the frozen job.
    service.policy = {**service.policy, "model_attempts": 20}
    job = service.submit({**job["request"], "idempotency_key": "nested-cross-branch"})
    original = module.TopicModelClient.plan_chat
    def split(self, **kwargs):
        if len(kwargs.get("public_payload", {}).get("documents", [])) > 1:
            raise TokenBudgetError("INPUT_CONTEXT_EXCEEDED")
        return original(self, **kwargs)
    monkeypatch.setattr(module.TopicModelClient, "plan_chat", split)
    def reply(wire, payload):
        if wire["reasoning_effort"] == "high":
            if "source_analyses" in payload:
                claim = deepcopy(payload["source_analyses"][0]["conclusions"][0])
                claim["supports"] = [ref for part in payload["source_analyses"]
                    for item in part["conclusions"] for ref in item["supports"]]
                return _analysis([claim])
            return _analysis([_claim(payload["documents"][0], BEFORE, claim_id="cross-branch",
                kind="fact", text=BEFORE)])
        docs = payload.get("documents", payload.get("document_refs", [{"id": identity}
            for identity in dict.fromkeys(item["document_id"] for review in payload.get("source_reviews", [])
                for check in review["claim_checks"] for item in check["evidence"])]))
        return checked_review(payload["report"], docs)
    h.state["reply"] = reply
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report.get("error")
    assert len(report["analysis"]["conclusions"]) == 1
    assert len(report["analysis"]["conclusions"][0]["supports"]) == 4
    review_payloads = [_payload(wire) for wire in h.state["model_requests"] if wire["reasoning_effort"] == "max"]
    assert [payload["review_scope"] for payload in review_payloads] == [
        "source_batch", "source_batch", "source_batch", "source_batch", "source_batch", "source_batch", "global"]
    syntheses = [payload for payload in review_payloads if "source_reviews" in payload]
    assert [len(payload["document_refs"]) for payload in syntheses] == [2, 2, 4]
    for payload in syntheses:
        local_ids = {ref["id"] for ref in payload["document_refs"]}
        actual = {item["document_id"] for review in payload["source_reviews"]
            for check in review["claim_checks"] for item in check["evidence"]}
        assert actual == local_ids
    assert len(h.state["model_requests"]) == 14
    assert service.report(job["job_id"]) == report
    if fault is not None:
        from test_intelligence_topic_model_view import _rewrite_mutable_request_chain
        wire = next(wire for wire in h.state["model_requests"] if wire["reasoning_effort"] == "max"
            and "source_reviews" in _payload(wire) and _payload(wire)["review_scope"] == "source_batch")
        def mutate(payload):
            if fault == "promoted_scope": payload["review_scope"] = "global"
            elif fault == "source_review": payload["source_reviews"][0]["summary"] = "TEST_FIXTURE no actual native supports this summary"
            elif fault == "document_reference": payload["document_refs"][0]["text_sha256"] = "0" * 64
            elif fault == "duplicate_part": payload["source_reviews"][1] = deepcopy(payload["source_reviews"][0])
            else: payload["verified_quotations"][0]["supports"] = []
        _rewrite_mutable_request_chain(service, job, wire, mutate)
        with pytest.raises(IntelligenceError) as denied:
            service.report(job["job_id"])
        assert denied.value.code in {"HASH_MISMATCH", "MODEL_RECEIPT_REQUIRED", "REVIEW_CONTRACT_INVALID"}
        assert len(h.state["model_requests"]) == 14


def test_missing_review_contract_is_legacy_only_not_backfilled():
    legacy = {key: value for key, value in module.DEFAULT_POLICY.items() if key != "semantic_review_contract"}
    frozen = module.trusted_policy_value(legacy)
    assert "semantic_review_contract" not in frozen
    assert module.TopicResearch._review_system({"policy": frozen}) == module.REVIEW_SYSTEM
    assert module.TopicResearch._analysis_system({"policy": frozen}) == module.ANALYSIS_SYSTEM
    assert module.trusted_policy()["semantic_review_contract"] == module.SEMANTIC_REVIEW_CONTRACT
    with pytest.raises(IntelligenceError):
        module.trusted_policy_value({**module.DEFAULT_POLICY, "semantic_review_contract": "not-a-reviewed-contract"})


@pytest.mark.parametrize("omit_unsupported_fact", [True, False])
def test_real_ir_calendar_rss_with_missing_original_never_certifies_a_fact(local_topic, omit_unsupported_fact):
    from email.utils import format_datetime
    from html import escape
    from trading_system.intelligence.catalog import DIMENSIONS
    from trading_system.intelligence.contracts import utc_now
    from trading_system.intelligence.registry import SourceRegistry, SourceSpec
    from trading_system.intelligence.service import IntelligenceService
    from trading_system.intelligence.transport import HttpTransport
    h = local_topic
    h.state["pages"]["/rss"] = (200, "application/xml", '<rss><channel><item><title>TEST_FIXTURE NVDA calendar</title>'
        '<link>' + h.base + '/missing-original</link><description>' + escape(BEFORE) + '</description><pubDate>'
        + format_datetime(utc_now()) + '</pubDate></item></channel></rss>')
    registry = SourceRegistry([SourceSpec("fixture_ir_calendar", "issuer_rss", h.base + "/rss",
        allowed_hosts=("127.0.0.1",), minimum_interval_seconds=0, max_retries=0,
        options={"issuer_id": "us:NVDA", "material_types": ["news_events"], "allowed_uses": ["topic_analysis"],
            "extractor": {"html_engine": "builtin"}})])
    service = module.TopicResearch(h.root / "state", h.root / "archive", mode="research", registry=registry,
        policy={**module.DEFAULT_POLICY, "rounds": 1, "parser_policy": {"html_engine": "builtin"}},
        service_factory=lambda root, r: IntelligenceService(root, r,
            r.build_adapters(transport=HttpTransport(allow_local=True), environment={})))
    materials = {key: {"status": "not_applicable", "reason": "TEST_FIXTURE calendar scope"} for key in DIMENSIONS}
    materials["news_events"] = "required"
    topic = service.create({"question": "TEST_FIXTURE NVDA IR meeting calendar facts", "entity_ids": ["us:NVDA"],
        "material_policy": materials, "max_conclusions": 3, "idempotency_key": "rss-topic"})
    job = service.submit({"topic_id": topic["topic_id"], "version": topic["version"], "idempotency_key": "rss-research"})
    def reply(wire, payload):
        if wire["reasoning_effort"] != "high":
            return checked_review(payload["report"], payload["documents"])
        doc = payload["documents"][0]
        assert doc["completeness"] == "partial" and BEFORE in doc["text"]
        if "previous_analysis" in payload and omit_unsupported_fact:
            return _analysis([], unknowns=["TEST_FIXTURE full original unavailable; RSS cannot establish a fact."])
        return _analysis([_claim(doc, BEFORE, kind="fact", text=BEFORE)])
    h.state["reply"] = reply
    result = service.run(job["job_id"])
    if omit_unsupported_fact:
        assert result["state"] == "partial" and result["analysis"]["conclusions"] == [], result
        assert result["analysis"]["unknowns"] and service.report(job["job_id"]) == result
    else:
        assert result["state"] == "failed" and result["error"]["code"] == "INCOMPLETE_FACT_SOURCE", result
    paths = {item["path"] for item in h.state["source_calls"]}
    assert "/rss" in paths and "/missing-original" in paths
    assert all(item["state"] == "settled" for item in service.store.attempts(job["job_id"]))


def test_new_contract_review_checkpoint_resume_reuses_original_native(local_topic, monkeypatch):
    h = local_topic
    service, _, job = strict_service(h)
    h.state["reply"] = positive_reply
    original_write = module.atomic_json
    def crash(root, name, value, **kwargs):
        if name.startswith("io-") and value["result"].get("stage") == "decision.debate":
            raise KeyboardInterrupt
        return original_write(root, name, value, **kwargs)
    monkeypatch.setattr(module, "atomic_json", crash)
    with pytest.raises(KeyboardInterrupt):
        service.run(job["job_id"])
    assert len(h.state["model_requests"]) == 2
    directory = service.store.directory(job["job_id"])
    raw = {path.name: path.read_bytes() for path in directory.glob("response-*.json")}
    monkeypatch.setattr(module, "atomic_json", original_write)
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    assert len(h.state["model_requests"]) == 2
    assert raw == {path.name: path.read_bytes() for path in directory.glob("response-*.json")}
    assert service.report(job["job_id"]) == report
