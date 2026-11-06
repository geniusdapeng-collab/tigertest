"""Admitted NBSP derivatives preserve actual replies and exact evidence gates.

Integration uses owned loopback HTTP with explicit synthetic provider replies.
Two optional preserved real-response inputs are offline provenance controls,
not new model calls or claims that those original failed jobs became successful.
"""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from test_intelligence_topic_safety_integration import (
    SAFE_QUOTE, _analysis, _claim, _payload, _review, _service, local_topic,
)
from trading_system.intelligence import topic as module
from trading_system.intelligence.citation_resolution import CONTRACT, derive_analysis, derivative_artifact
from trading_system.intelligence.contracts import IntelligenceError, canonical, digest
from trading_system.llm.model_budget import TokenBudgetError


ORIGINAL = "TEST_FIXTURE NVDA conference call begins at 2 p.m.\u00a0PT."
MODEL_QUOTE = ORIGINAL.replace("\u00a0", " ")


def _documents(text=ORIGINAL):
    return [{"id": "docver_TEST_FIXTURE_original", "text": text, "completeness": "complete"}]


@pytest.mark.parametrize("text,quote", [
    ("Revenue\u00a0was 10 million.", "Revenue was 12 million."),
    ("Revenue\u00a0was 10 million.", "Revenue was 10 million!"),
    ("Revenue\u00a0was 10 million.", "revenue was 10 million."),
    ("Revenue\nwas 10 million.", "Revenue was 10 million."),
    ("Revenue\twas 10 million.", "Revenue was 10 million."),
    ("Revenue\u202fwas 10 million.", "Revenue was 10 million."),
    ("Revenue\u00a0 was 10 million.", "Revenue was 10 million."),
    ("Revenue\u00a0was 10 million.", "Revenuewas 10 million."),
    ("Revenue\u200bwas 10 million.", "Revenue was 10 million."),
    ("Caf\u00e9\u00a0revenue", "Cafe\u0301 revenue"),
    ("Revenue\u00a0was 10 million. Revenue\u00a0was 10 million.", "Revenue was 10 million."),
    ("Revenue was 10 million.", "Revenue\u00a0was 10 million."),
    ("A\u00a0B C", "A B\u00a0C"),
])
def test_only_unique_one_way_same_length_nbsp_substitution_can_materialize(text, quote):
    documents = _documents(text)
    analysis = _analysis([_claim(documents[0], quote)])
    before = deepcopy(analysis)
    with pytest.raises(IntelligenceError) as error:
        derive_analysis(analysis, documents)
    assert error.value.code == "CITATION_INVALID" and analysis == before


def test_derivative_preserves_raw_and_all_other_analysis_fields_with_explicit_offsets():
    documents = _documents()
    raw = _analysis([_claim(documents[0], MODEL_QUOTE)])
    raw["conclusions"][0]["refutes"] = deepcopy(raw["conclusions"][0]["supports"])
    raw["unknowns"] = ["TEST_FIXTURE retain this unknown and contrary evidence"]
    before = deepcopy(raw)
    derived, mappings = derive_analysis(raw, documents)
    assert raw == before and len(mappings) == 2
    assert {mapping["relation"] for mapping in mappings} == {"supports", "refutes"}
    for mapping in mappings:
        assert mapping["model_quote"] == MODEL_QUOTE and mapping["resolved_quote"] == ORIGINAL
        assert documents[0]["text"][mapping["start_codepoint"]:mapping["end_codepoint"]] == ORIGINAL
        assert documents[0]["text"].encode()[mapping["start_byte"]:mapping["end_byte"]].decode() == ORIGINAL
        assert mapping["changes"] == [{"quote_codepoint_offset": ORIGINAL.index("\u00a0"),
            "model_codepoint": "U+0020", "original_codepoint": "U+00A0"}]
    control = deepcopy(derived)
    for relation in ("supports", "refutes"):
        control["conclusions"][0][relation][0]["quote"] = MODEL_QUOTE
    assert control == raw
    module.validate_analysis(derived, documents, {"news_events": "required"}, ["us:NVDA"])
    with pytest.raises(IntelligenceError):
        module.validate_analysis(raw, documents, {"news_events": "required"}, ["us:NVDA"])


def test_exact_existing_quote_keeps_old_semantics_even_when_repeated():
    documents = _documents(ORIGINAL + " " + ORIGINAL)
    raw = _analysis([_claim(documents[0], ORIGINAL)])
    derived, mappings = derive_analysis(raw, documents)
    assert derived == raw and mappings == []


def test_mapping_never_uses_other_document_and_never_overrides_fact_completeness():
    documents = _documents("TEST_FIXTURE unrelated original") + [
        {"id": "docver_TEST_FIXTURE_other", "text": ORIGINAL, "completeness": "complete"}]
    raw = _analysis([_claim(documents[0], MODEL_QUOTE)])
    with pytest.raises(IntelligenceError):
        derive_analysis(raw, documents)
    documents = _documents()
    documents[0]["completeness"] = "partial"
    raw = _analysis([_claim(documents[0], MODEL_QUOTE, kind="fact")])
    derived, _ = derive_analysis(raw, documents)
    with pytest.raises(IntelligenceError) as error:
        module.validate_analysis(derived, documents, {"news_events": "required"}, ["us:NVDA"])
    assert error.value.code == "INCOMPLETE_FACT_SOURCE"


def _mapped(local_topic, monkeypatch=None, *, legacy=False, formal_required=False):
    if legacy:
        with monkeypatch.context() as old:
            old.setitem(module.DEFAULT_POLICY, "citation_resolution_contract", None)
            return _service(local_topic, [("public_original", ORIGINAL,
                ["news_events"], ["topic_analysis"])], formal_required=formal_required)
    return _service(local_topic, [("public_original", ORIGINAL,
        ["news_events"], ["topic_analysis"])], formal_required=formal_required)


def _answer(wire, payload):
    if wire["reasoning_effort"] != "high":
        assert payload["citation_resolutions"][0]["mappings"][0]["model_quote"] == MODEL_QUOTE
        assert payload["report"]["conclusions"][0]["supports"][0]["quote"] == ORIGINAL
        return _review()
    return _analysis([_claim(payload["documents"][0], MODEL_QUOTE)])


def test_actual_local_http_derivative_uses_no_repair_and_preserves_original_paid_shape(local_topic):
    service, _, job = _mapped(local_topic)
    local_topic.state["reply"] = _answer
    sealed = service.run(job["job_id"])
    assert sealed["state"] == "completed", sealed
    assert len(local_topic.state["model_requests"]) == 2
    assert [wire["reasoning_effort"] for wire in local_topic.state["model_requests"]] == ["high", "max"]
    directory = service.store.directory(job["job_id"])
    assert not (directory / "citation-repair.json").exists()
    artifacts = list(directory.glob("analysis-resolution-*.json"))
    assert len(artifacts) == 1
    artifact = json.loads(artifacts[0].read_text())
    assert artifact["analysis"] == sealed["analysis"]
    assert sealed["citation_resolutions"] == [artifact["resolution"]]
    assert job["policy"]["citation_resolution_contract"] == CONTRACT
    assert "引用校对：" in sealed["readable_report"]
    attempts = service.store.attempts(job["job_id"])
    original_bytes = {path.name: path.read_bytes() for path in directory.glob("response-*.json")}
    checkpoint = json.loads((directory / attempts[0]["receipt"]["response_ref"]).read_text())
    raw = json.loads(checkpoint["response"]["choices"][0]["message"]["content"])
    assert raw["conclusions"][0]["supports"][0]["quote"] == MODEL_QUOTE
    assert sealed["analysis"]["conclusions"][0]["supports"][0]["quote"] == ORIGINAL
    assert all(attempt["state"] == "settled" for attempt in attempts)
    assert service.report(job["job_id"]) == sealed
    assert all((directory / name).read_bytes() == data for name, data in original_bytes.items())


def _reseal(service, job, report, artifacts):
    directory = service.store.directory(job["job_id"])
    for old in directory.glob("analysis-resolution-*.json"):
        old.unlink()
    for artifact in artifacts:
        artifact["resolution"]["sha256"] = digest({k: v for k, v in artifact["resolution"].items() if k != "sha256"})
        artifact["sha256"] = digest({k: v for k, v in artifact.items() if k != "sha256"})
        (directory / service._derivative_filename(artifact)).write_bytes(canonical(artifact) + b"\n")
    report["citation_resolutions"] = [a["resolution"] for a in sorted(artifacts, key=lambda a: a["sha256"])]
    documents = service._service(readonly=True).archive.snapshot_documents(report["snapshot_ids"][-1])
    report["readable_report"] = module.render_report(report, documents)
    report["receipt_sha256"] = digest({k: v for k, v in report.items() if k != "receipt_sha256"})
    (directory / "report.json").write_bytes(canonical(report) + b"\n")
    service.store.update(job["job_id"], report_sha256=report["receipt_sha256"])


@pytest.mark.parametrize("fault", ["offset", "document", "response", "raw_hash", "resolved_hash", "policy",
                                  "missing_mapping", "extra_mapping", "relation", "model_quote", "extra_flag",
                                  "derived_claim", "omit_artifact", "extra_artifact"])
def test_coherent_trace_and_report_rehash_cannot_replace_independent_recomputation(local_topic, fault):
    service, _, job = _mapped(local_topic)
    local_topic.state["reply"] = _answer
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    directory = service.store.directory(job["job_id"])
    artifact = json.loads(next(directory.glob("analysis-resolution-*.json")).read_text())
    resolution = artifact["resolution"]
    mapping = resolution["mappings"][0]
    artifacts = [artifact]
    if fault == "offset": mapping["start_codepoint"] += 1
    elif fault == "document": mapping["document_id"] = "docver_TEST_FIXTURE_foreign"
    elif fault == "response": resolution["response_id"] = "TEST_FIXTURE-unrelated-provider-reply"
    elif fault == "raw_hash": resolution["raw_analysis_sha256"] = "0" * 64
    elif fault == "resolved_hash": mapping["resolved_quote_sha256"] = "0" * 64
    elif fault == "policy": resolution["schema"] = "TEST_FIXTURE-always-accepted/v1"
    elif fault == "missing_mapping": resolution["mappings"] = []
    elif fault == "extra_mapping": resolution["mappings"].append(deepcopy(mapping))
    elif fault == "relation": mapping["relation"] = "refutes"
    elif fault == "model_quote": mapping["model_quote"] = ORIGINAL
    elif fault == "extra_flag": mapping["verified"] = True
    elif fault == "derived_claim":
        artifact["analysis"]["conclusions"][0]["text"] = "TEST_FIXTURE falsely upgraded definitive claim"
        report["analysis"] = deepcopy(artifact["analysis"])
        resolution["derived_analysis_sha256"] = digest(artifact["analysis"])
    elif fault == "omit_artifact": artifacts = []
    else:
        extra = deepcopy(artifact)
        extra["resolution"]["response_id"] = "TEST_FIXTURE-extra"
        artifacts.append(extra)
    _reseal(service, job, report, artifacts)
    with pytest.raises(IntelligenceError) as denied:
        service.report(job["job_id"])
    assert denied.value.code in {"HASH_MISMATCH", "MODEL_RECEIPT_REQUIRED"}
    assert len(local_topic.state["model_requests"]) == 2


def test_mapping_does_not_upgrade_material_permission_or_override_current_revocation(local_topic, monkeypatch):
    from trading_system.intelligence.topic import TopicResearch
    service, _, job = _mapped(local_topic, formal_required=True)
    def answer(wire, payload):
        if wire["reasoning_effort"] != "high": return _review()
        return _analysis([_claim(payload["documents"][0], MODEL_QUOTE),
            _claim(payload["documents"][0], MODEL_QUOTE, claim_id="TEST_FIXTURE-forged-formal", dimension="formal_research")])
    local_topic.state["reply"] = answer
    report = service.run(job["job_id"])
    assert report["state"] == "partial", report
    assert next(row for row in report["coverage"] if row["dimension_id"] == "formal_research")["status"] == "unknown"
    registry = service.registry
    from trading_system.intelligence.registry import SourceRegistry
    from dataclasses import replace
    current = TopicResearch(service.root, service.archive_root, mode="readonly", policy=service.policy,
        registry=SourceRegistry([replace(registry.get("public_original"), enabled=False)]))
    with pytest.raises(IntelligenceError) as denied:
        current.report(job["job_id"])
    assert denied.value.code == "POLICY_DENIED"


def test_explicit_legacy_admission_still_requires_model_exact_reply_and_cannot_gain_trace(local_topic, monkeypatch):
    service, _, job = _mapped(local_topic, monkeypatch, legacy=True)
    def answer(wire, payload):
        if wire["reasoning_effort"] != "high": return _review()
        return _analysis([_claim(payload["documents"][0], ORIGINAL)])
    local_topic.state["reply"] = answer
    report = service.run(job["job_id"])
    assert report["state"] == "completed" and "citation_resolutions" not in report
    directory = service.store.directory(job["job_id"])
    before = (directory / "report.json").read_bytes()
    assert service.report(job["job_id"]) == report and (directory / "report.json").read_bytes() == before
    report["citation_resolutions"] = [{"schema": CONTRACT, "verified": True}]
    report["receipt_sha256"] = digest({k: v for k, v in report.items() if k != "receipt_sha256"})
    (directory / "report.json").write_bytes(canonical(report) + b"\n")
    service.store.update(job["job_id"], report_sha256=report["receipt_sha256"])
    with pytest.raises(IntelligenceError) as denied:
        service.report(job["job_id"])
    assert denied.value.code == "HASH_MISMATCH"
    changed = deepcopy(job["policy"])
    changed["citation_resolution_contract"] = CONTRACT
    service.store.update(job["job_id"], policy=changed, policy_sha256=digest(changed))
    with pytest.raises(IntelligenceError) as denied:
        service.store.job(job["job_id"])
    assert denied.value.code == "HASH_MISMATCH"


def test_missing_legacy_contract_keeps_exact_report_bytes_and_refuses_derivative_api(local_topic, monkeypatch):
    service, topic, _ = _mapped(local_topic, monkeypatch, legacy=True)
    service.policy.pop("citation_resolution_contract")
    job = service.submit({"topic_id": topic["topic_id"], "version": topic["version"],
        "idempotency_key": "TEST_FIXTURE-missing-legacy-contract"})
    assert "citation_resolution_contract" not in job["policy"]
    local_topic.state["reply"] = lambda wire, payload: (_analysis([
        _claim(payload["documents"][0], ORIGINAL)]) if wire["reasoning_effort"] == "high" else _review())
    report = service.run(job["job_id"])
    assert report["state"] == "completed" and "citation_resolutions" not in report
    path = service.store.directory(job["job_id"]) / "report.json"
    original = path.read_bytes()
    assert service.report(job["job_id"]) == report and path.read_bytes() == original
    with pytest.raises(IntelligenceError) as denied:
        derivative_artifact(job, {}, _documents(), _analysis([_claim(_documents()[0], MODEL_QUOTE)]), {})
    assert denied.value.code == "POLICY_DENIED"


def test_matching_mutable_legacy_policy_and_admission_edits_cannot_override_original_checkpoint(local_topic, monkeypatch):
    service, _, job = _mapped(local_topic, monkeypatch, legacy=True)
    local_topic.state["reply"] = lambda wire, payload: (_analysis([
        _claim(payload["documents"][0], ORIGINAL)]) if wire["reasoning_effort"] == "high" else _review())
    original = service.run(job["job_id"])
    assert original["state"] == "completed"
    directory = service.store.directory(job["job_id"])
    original_responses = {p.name: p.read_bytes() for p in directory.glob("response-*.json")}
    changed = service.store.job(job["job_id"])
    changed["policy"]["citation_resolution_contract"] = CONTRACT
    changed["policy_sha256"] = digest(changed["policy"])
    (directory / "admission.json").write_bytes(canonical(changed) + b"\n")
    with service.store.connection() as connection:
        connection.execute("UPDATE research_jobs SET record=? WHERE job_id=?",
            (canonical(changed).decode(), job["job_id"]))
    with pytest.raises(IntelligenceError) as denied:
        service.report(job["job_id"])
    assert denied.value.code == "HASH_MISMATCH"
    assert all((directory / name).read_bytes() == body for name, body in original_responses.items())
    assert len(local_topic.state["model_requests"]) == 2


@pytest.mark.parametrize("contract", ["TEST_FIXTURE-permissive-normalization", [CONTRACT],
                                      {"verified": True}, True, 123])
def test_unknown_trusted_contract_is_rejected_without_collecting_or_calling_model(local_topic, contract):
    with pytest.raises(IntelligenceError) as denied:
        module.trusted_policy_value({**module.DEFAULT_POLICY,
            "citation_resolution_contract": contract})
    assert denied.value.code == "INVALID_STARTUP"
    assert not local_topic.state["source_calls"] and not local_topic.state["model_requests"]


def test_derivative_resume_reuses_original_response_and_exact_artifact(local_topic, monkeypatch):
    service, _, job = _mapped(local_topic)
    local_topic.state["reply"] = _answer
    atomic = module.atomic_json
    def interrupt(root, name, value, **kwargs):
        if name == "analysis-0.json": raise KeyboardInterrupt
        return atomic(root, name, value, **kwargs)
    monkeypatch.setattr(module, "atomic_json", interrupt)
    with pytest.raises(KeyboardInterrupt): service.run(job["job_id"])
    directory = service.store.directory(job["job_id"])
    artifacts = {p.name: p.read_bytes() for p in directory.glob("analysis-resolution-*.json")}
    assert len(artifacts) == 1 and len(local_topic.state["model_requests"]) == 1
    original_attempt = service.store.attempts(job["job_id"])[0]
    monkeypatch.setattr(module, "atomic_json", atomic)
    result = service.run(job["job_id"])
    assert result["state"] == "completed", result
    assert len(local_topic.state["model_requests"]) == 2
    assert service.store.attempts(job["job_id"])[0] == original_attempt
    assert all((directory / name).read_bytes() == data for name, data in artifacts.items())


def test_review_must_receive_the_derived_report_and_its_resolution_provenance(local_topic, monkeypatch):
    service, _, job = _mapped(local_topic)
    semantic = service._semantic
    def hidden_provenance(client, execution, **kwargs):
        if kwargs["stage"] == "decision.debate":
            kwargs["payload"] = {k: v for k, v in kwargs["payload"].items() if k != "citation_resolutions"}
        return semantic(client, execution, **kwargs)
    monkeypatch.setattr(service, "_semantic", hidden_provenance)
    local_topic.state["reply"] = lambda wire, payload: (_analysis([
        _claim(payload["documents"][0], MODEL_QUOTE)]) if wire["reasoning_effort"] == "high" else _review())
    result = service.run(job["job_id"])
    assert result["state"] == "failed" and result["error"]["code"] == "MODEL_RECEIPT_REQUIRED"
    assert len(local_topic.state["model_requests"]) == 2
    with pytest.raises(IntelligenceError) as denied:
        service.report(job["job_id"])
    assert denied.value.code == "RESEARCH_INCOMPLETE"


def test_source_batches_and_synthesis_each_bind_their_own_raw_derivative(local_topic, monkeypatch):
    service, _, job = _service(local_topic, [
        ("public_original", ORIGINAL, ["news_events"], ["topic_analysis"]),
        ("second_original", SAFE_QUOTE, ["news_events"], ["topic_analysis"])])
    plan = module.TopicModelClient.plan_chat
    def bounded(self, **kwargs):
        if len(kwargs.get("public_payload", {}).get("documents", [])) > 1:
            raise TokenBudgetError("INPUT_CONTEXT_EXCEEDED")
        return plan(self, **kwargs)
    monkeypatch.setattr(module.TopicModelClient, "plan_chat", bounded)
    def answer(wire, payload):
        if wire["reasoning_effort"] != "high": return _review()
        if "source_analyses" in payload:
            claims = [deepcopy(c) for part in payload["source_analyses"] for c in part["conclusions"]]
            for claim in claims:
                for ref in claim["supports"]: ref["quote"] = ref["quote"].replace("\u00a0", " ")
            return _analysis(claims)
        document = payload["documents"][0]
        return _analysis([_claim(document, MODEL_QUOTE if document["source_id"] == "public_original" else SAFE_QUOTE,
            claim_id="claim-" + document["source_id"])])
    local_topic.state["reply"] = answer
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    assert len(report["citation_resolutions"]) == 2
    assert sorted(len(record["document_scope"]) for record in report["citation_resolutions"]) == [1, 2]
    assert len(local_topic.state["model_requests"]) == 6
    global_reviews = [_payload(wire) for wire in local_topic.state["model_requests"]
        if wire["reasoning_effort"] == "max" and "source_reviews" in _payload(wire)]
    assert len(global_reviews) == 1
    assert global_reviews[0]["citation_resolutions"] == report["citation_resolutions"]
    assert service.report(job["job_id"]) == report


@pytest.mark.parametrize("is_repair", [False, True])
def test_preserved_actual_ir_replies_replay_without_network_or_mutating_historical_evidence(is_repair):
    configured = os.environ.get("TIGER_TEST_IR_REPLAY")
    if not configured:
        pytest.skip("Optional preserved real-response directory was not supplied")
    directory = Path(configured)
    before = {p: p.read_bytes() for p in directory.glob("*.json")}
    for path in directory.glob("io-*.json"):
        saved = json.loads(path.read_text())
        payload = _payload(saved["inputs"]["request"])
        if ("previous_analysis" in payload) != is_repair: continue
        raw, documents = saved["result"]["parsed"], payload["documents"]
        with pytest.raises(IntelligenceError) as denied:
            module.validate_analysis(raw, documents, payload["topic"]["material_policy"], payload["topic"]["entity_ids"])
        assert denied.value.code == "CITATION_INVALID"
        derived, mappings = derive_analysis(raw, documents)
        assert {m["claim_id"] for m in mappings} == {"C01", "C02", "C05"}
        assert sum(len(m["changes"]) for m in mappings) == 4
        module.validate_analysis(derived, documents, payload["topic"]["material_policy"], payload["topic"]["entity_ids"])
        assert all(path.read_bytes() == data for path, data in before.items())
        return
    pytest.fail("Matching actual generation/repair response was not found")
