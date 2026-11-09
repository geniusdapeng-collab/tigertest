"""Independent joins and snapshot replay over real local child evidence."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest
import requests

from test_deepseek_json_observation import observed, native
from test_deepseek_required import Response
from test_research_work_execution import review
from test_research_work_semantics import annotation, raw
from trading_system.agent_evidence import (EvidenceInvalid, atomic_bytes, atomic_json,
    digest, json_bytes, read_json, read_snapshot, strict_json)
from trading_system.agent_runtime import INTELLIGENCE_PROFILE, policy_manifest
from trading_system.llm import model_budget
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.llm.intelligence_proof import _Replay
from trading_system.llm.json_observation import TOOL_CONTRACT
from trading_system.llm.work_proof import ResearchWorkProof, partial_work_accounting
from trading_system.llm.work_required import clean_round, document_arguments
from trading_system.research_snapshot import snapshot_manifest
from trading_system.research_work import research_work_policy, work_manifest
from trading_system.redline.execution import REQUIRED_MODEL_STAGES
from trading_system.tasks import WorkStore, UnitSpec, WorkUnitError


class FixtureReplay(_Replay):
    """Use the production native verifier, with only this fixture's prefix."""
    def __init__(self, context, admission, policy):
        self.run, self.admission, self.policy = context.store.root, admission, policy
        self.scene_policy_path = None
        self.budget_contract = model_budget.budget_contract()
        self.assessment = {key: admission["clock"][key] for key in ("run_date", "market_timezone")}
        self.cutoff = context.admitted_at
        self.observation_contract = TOOL_CONTRACT
        self.response_ids, self.call_ids, self.identities = [], set(), set()
        self.counts = {key: 0 for key in REQUIRED_MODEL_STAGES}
        self.chat_schema_retries = 0
        self.observation_refs, self.chat_observations = set(), []
        events = [strict_json(line) for line in (self.run / "events.jsonl").read_bytes().splitlines()]
        self.ends = {row["stage"]: row for row in events if row["kind"] == "end"}
        self.outputs = {stage: self.snapshot(row, "output") for stage, row in self.ends.items()}
        self.inputs = {stage: self.snapshot(row, "input") for stage, row in self.ends.items()}


def prepare_proof_context(context):
    destination = context.store.root.with_name(context.run_id)
    context.store.root.rename(destination)
    context.store.root = destination
    context.response_observation_contract = TOOL_CONTRACT
    research = research_work_policy()
    policy = strict_json(json_bytes(policy_manifest(INTELLIGENCE_PROFILE, authoritative_paper=False,
        response_observation_contract=TOOL_CONTRACT, research_work=research)))
    clock = {"admitted_at_utc": context.admitted_at.isoformat(),
        "deadline_at_utc": (context.admitted_at + timedelta(hours=2)).isoformat(),
        "run_date": context.run_date.isoformat(), "market_timezone": "America/New_York"}
    admission = {"run_id": context.run_id, "request_sha256": "1" * 64,
        "runtime_input_sha256": "2" * 64, "source_sha256": "3" * 64,
        "policy_sha256": digest(json_bytes(policy)), "parent": {}, "clock": clock}
    context.research_work_policy = research
    context.research_work_store = WorkStore.create(destination / "tasks", work_manifest(admission),
        clock=lambda: context.admitted_at.timestamp())
    atomic_json(destination, "policy.json", policy, exclusive=True)
    atomic_bytes(destination, "source/trading_system/llm/model_budget.py",
        Path(model_budget.__file__).read_bytes(), exclusive=True)
    registry = {"sources": {}, "sha256": "4" * 64}
    acquisition = SimpleNamespace(frontier={"security_ids": {}}, entities={},
        registry=SimpleNamespace(public_manifest=lambda: registry))
    return admission, policy, acquisition, DeepSeekRequiredClient(context)


def synthetic_transport(monkeypatch, *, review_value=None, extract_mutation=None):
    wires, lock = [], Lock()
    def post(url, **kwargs):
        wire = json.loads(kwargs["data"])
        user = wire["messages"][-1]["content"].split("\nJSON schema:\n", 1)[0]
        payload = json.loads(user.split("\n", 1)[1])
        if payload["phase"] == "extract":
            value = {"annotation": annotation()}
            if extract_mutation is not None:
                extract_mutation(value)
        elif payload["phase"] == "empty":
            value = {"documents": []}
        else:
            value = review() if review_value is None else deepcopy(review_value)
        with lock:
            wires.append(wire)
            identifier = "proof-fixture-" + str(len(wires))
        response = Response(native(json.dumps(value, ensure_ascii=False), identifier))
        response.url, response.history = url, []
        return response
    monkeypatch.setattr(requests, "post", post)
    return wires


def segmented_transport(monkeypatch, *, extract_mutation=None):
    wires, lock = [], Lock()
    def post(url, **kwargs):
        wire = json.loads(kwargs["data"])
        user = wire["messages"][-1]["content"].split("\nJSON schema:\n", 1)[0]
        payload = json.loads(user.split("\n", 1)[1])
        phase = payload["phase"]
        if phase == "extract":
            content = payload["source"]["content"]
            quote = min((line.strip() for line in content.splitlines() if line.strip()),
                        key=len, default=content[:1])
            value = {"annotation": {
                "tickers": [], "sentiment": "neutral", "sentiment_score": 0,
                "events": [], "summary_zh": quote[:600], "relevance": 1,
                "subjects": [], "evidence_spans": [{
                    "id": "p1", "start": None, "end": None, "quote": quote}],
                "event_records": [], "claims": []}}
            if extract_mutation is not None:
                extract_mutation(value, payload)
        elif phase == "metadata_synthesis":
            value = {"summary_zh": "分段摘要已按完整原文覆盖并逐段复核。"}
        else:
            value = review()
        with lock:
            wires.append(wire)
            identifier = "segment-proof-" + str(len(wires))
        response = Response(native(json.dumps(value, ensure_ascii=False), identifier))
        response.url, response.history = url, []
        return response
    monkeypatch.setattr(requests, "post", post)
    return wires


@pytest.fixture
def completed(observed, monkeypatch):
    admission, policy, acquisition, client = prepare_proof_context(observed)
    wires = synthetic_transport(monkeypatch)
    originals = [raw(), raw("source-2")]
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "new work proof"}) as scope:
        documents = clean_round(client, originals, acquisition=acquisition)
        scope.output(documents)
        joins = deepcopy(scope.io_calls)
    replay = FixtureReplay(observed, admission, policy)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("Proof opened a network connection"))
    return SimpleNamespace(context=observed, admission=admission, policy=policy,
        acquisition=acquisition, store=observed.research_work_store, documents=documents,
        originals=originals, joins=joins, replay=replay, wires=wires)


@pytest.fixture
def segmented_completed(observed, monkeypatch):
    from trading_system.research_work import _POLICY
    monkeypatch.setitem(_POLICY, "segment_input_token_limit", 5000)
    admission, policy, acquisition, client = prepare_proof_context(observed)
    wires = segmented_transport(monkeypatch)
    content = "\n\n".join(
        f"Section {number}. Complete issuer disclosure paragraph with revenue "
        f"and risk evidence number {number}." for number in range(1, 401))
    original = raw(content=content)
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "segmented proof"}) as scope:
        documents = clean_round(client, [original], acquisition=acquisition)
        scope.output(documents)
        joins = deepcopy(scope.io_calls)
    replay = FixtureReplay(observed, admission, policy)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("Proof opened a network connection"))
    return SimpleNamespace(context=observed, admission=admission, policy=policy,
        acquisition=acquisition, store=observed.research_work_store, documents=documents,
        originals=[original], joins=joins, replay=replay, wires=wires)


@pytest.fixture
def segmented_semantic_failed(observed, monkeypatch):
    from trading_system.research_work import _POLICY
    monkeypatch.setitem(_POLICY, "segment_input_token_limit", 5000)
    admission, policy, acquisition, client = prepare_proof_context(observed)
    mutation = lambda value, payload: (
        value["annotation"]["evidence_spans"][0].update(quote="NOT IN THE SEGMENT SOURCE")
        if "Section 1." in payload["source"]["content"] else None)
    wires = segmented_transport(monkeypatch, extract_mutation=mutation)
    content = "\n\n".join(
        f"Section {number}. Complete issuer disclosure paragraph with revenue "
        f"and risk evidence number {number}." for number in range(1, 401))
    original = raw(content=content)
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "segmented semantic terminal"}) as scope:
        documents = clean_round(client, [original], acquisition=acquisition)
        scope.output(documents)
        joins = deepcopy(scope.io_calls)
    replay = FixtureReplay(observed, admission, policy)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("Proof opened a network connection"))
    return SimpleNamespace(context=observed, admission=admission, policy=policy,
        acquisition=acquisition, store=observed.research_work_store, documents=documents,
        originals=[original], joins=joins, replay=replay, wires=wires)


@pytest.fixture
def unreviewable(observed, monkeypatch):
    admission, policy, acquisition, client = prepare_proof_context(observed)
    rejected = review()
    rejected.update(complete=False, omissions=["TEST_FIXTURE source limitation"])
    wires = synthetic_transport(monkeypatch, review_value=rejected)
    original = raw()
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "incomplete proof review"}) as scope:
        documents = clean_round(client, [original], acquisition=acquisition)
        scope.output(documents)
        joins = deepcopy(scope.io_calls)
    replay = FixtureReplay(observed, admission, policy)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("Proof opened a network connection"))
    return SimpleNamespace(context=observed, admission=admission, policy=policy,
        acquisition=acquisition, store=observed.research_work_store, documents=documents,
        originals=[original], joins=joins, replay=replay, wires=wires)


@pytest.fixture
def semantic_failed(observed, monkeypatch):
    admission, policy, acquisition, client = prepare_proof_context(observed)
    wires = synthetic_transport(
        monkeypatch,
        extract_mutation=lambda value: value["annotation"]["claims"][0].update(value="999"))
    original = raw()
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "semantic terminal proof"}) as scope:
        documents = clean_round(client, [original], acquisition=acquisition)
        scope.output(documents)
        joins = deepcopy(scope.io_calls)
    replay = FixtureReplay(observed, admission, policy)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("Proof opened a network connection"))
    return SimpleNamespace(context=observed, admission=admission, policy=policy,
        acquisition=acquisition, store=observed.research_work_store, documents=documents,
        originals=[original], joins=joins, replay=replay, wires=wires)


def validation_context(fixture):
    return {"security_ids": fixture.acquisition.frontier["security_ids"],
        "entity_registry": fixture.acquisition.entities,
        "source_policies": fixture.acquisition.registry.public_manifest()}


def verify_documents(fixture, verifier=None):
    verifier = verifier or ResearchWorkProof(fixture.replay)
    documents = [verifier.verify_document(original, fixture.joins[2 * index],
        fixture.joins[2 * index + 1], validation_context=validation_context(fixture))
        for index, original in enumerate(fixture.originals)]
    return verifier, documents


def test_native_child_joins_rebuild_distinct_originals_and_accounting_without_io(completed):
    verifier, documents = verify_documents(completed)
    assert [asdict(document) for document in documents] == [asdict(document) for document in completed.documents]
    summary = verifier.assert_complete()
    assert summary["verified_units"] == 4 and summary["document_count"] == 2
    assert completed.replay.counts["clean.llm_semantic"] == 4
    assert len(completed.replay.response_ids) == len(completed.wires) == 4
    assert completed.replay.chat_accounting(other_model_io_count=0)["known_usage"]["total_tokens"] == 200


def test_segmented_document_proof_rebuilds_every_core_range_and_review(segmented_completed):
    fixture = segmented_completed
    assert len(fixture.documents) == 1
    verifier = ResearchWorkProof(fixture.replay)
    document, consumed = verifier.verify_document(
        fixture.originals[0], fixture.joins,
        validation_context=validation_context(fixture), return_count=True)
    assert asdict(document) == asdict(fixture.documents[0])
    assert consumed == len(fixture.joins) > 2
    summary = verifier.assert_complete()
    assert summary["document_count"] == 1 and summary["verified_units"] == consumed
    assert verifier.store.document_dispositions()["documents"][fixture.originals[0].doc_id]["state"] == "eligible"


def test_negative_review_is_verified_as_an_unverifiable_terminal(unreviewable):
    fixture = unreviewable
    assert fixture.documents == []
    verifier = ResearchWorkProof(fixture.replay)
    assert verifier.verify_document(
        fixture.originals[0], fixture.joins[0], fixture.joins[1],
        validation_context=validation_context(fixture)) is None
    disposition = verifier.store.document_dispositions()["documents"][fixture.originals[0].doc_id]
    assert disposition == {
        "state": "unverifiable",
        "reason": "independent_review_or_source_qualification_incomplete",
    }
    summary = verifier.assert_complete()
    assert summary["verified_units"] == 2 and summary["document_count"] == 1


def test_semantic_extract_failure_is_replayed_and_dependency_terminalized(semantic_failed):
    fixture = semantic_failed
    assert fixture.documents == []
    assert len(fixture.joins) == len(fixture.wires) == 1
    verifier = ResearchWorkProof(fixture.replay)
    document, consumed = verifier.verify_document(
        fixture.originals[0], fixture.joins,
        validation_context=validation_context(fixture), return_count=True)
    assert document is None and consumed == 1
    disposition = verifier.store.document_dispositions()["documents"][fixture.originals[0].doc_id]
    assert disposition == {
        "state": "unverifiable",
        "reason": "upstream_document_semantics_unverifiable",
    }
    summary = verifier.assert_complete()
    assert summary["verified_units"] == 2 and summary["document_count"] == 1
    assert summary["native_count"] == 1
    review_result = verifier.store.read_result(
        next(row["unit_id"] for row in verifier.store.status()["units"]
             if row["spec"]["kind"] == "document.document_review"))["value"]
    assert review_result["schema"] == "tiger.document-terminal-result/v1"
    assert review_result["upstreams"][0]["error_code"] == "WORK_ANNOTATION_INVALID"


def test_segment_failure_terminalizes_once_after_every_segment_is_accounted(segmented_semantic_failed):
    fixture = segmented_semantic_failed
    assert fixture.documents == []
    verifier = ResearchWorkProof(fixture.replay)
    document, consumed = verifier.verify_document(
        fixture.originals[0], fixture.joins,
        validation_context=validation_context(fixture), return_count=True)
    assert document is None and consumed == len(fixture.joins)
    units = verifier.store.status()["units"]
    summary = next(row for row in units if row["spec"]["kind"] == "document.metadata_synthesis")
    terminal = verifier.store.read_result(summary["unit_id"])["value"]
    assert terminal["schema"] == "tiger.document-terminal-result/v1"
    assert [row["error_code"] for row in terminal["upstreams"]] == ["WORK_SPAN_BINDING_INVALID"]
    disposition = verifier.store.document_dispositions()["documents"][fixture.originals[0].doc_id]
    assert disposition["state"] == "unverifiable"
    assert verifier.assert_complete()["document_count"] == 1


@pytest.mark.parametrize("mutation", ["wrong_join", "changed_value", "missing_parent_copy", "different_original"])
def test_bound_join_rejects_transplant_rehash_missing_copy_and_changed_original(completed, mutation):
    joins, original = deepcopy(completed.joins), completed.originals[0]
    if mutation == "wrong_join":
        joins[0]["output_ref"] = joins[2]["output_ref"]
    elif mutation == "changed_value":
        joined = read_snapshot(completed.context.store.root, joins[0]["output_ref"],
            run_id=completed.context.run_id, stage="clean.llm_semantic", role="output")
        joined["value"]["annotation"]["claims"][0]["value"] = "999"
        joins[0]["output_ref"] = completed.context.store.snapshot(
            stage="clean.llm_semantic", role="output", value=joined)
    elif mutation == "missing_parent_copy":
        joined = read_snapshot(completed.context.store.root, joins[0]["output_ref"],
            run_id=completed.context.run_id, stage="clean.llm_semantic", role="output")
        (completed.context.store.root / joined["io_calls"][0]["output_ref"]["path"]).unlink()
    else:
        original = raw(content=raw().content + " Changed original.")
    with pytest.raises(EvidenceInvalid):
        ResearchWorkProof(completed.replay).verify_document(original, joins[0], joins[1],
            validation_context=validation_context(completed))


def test_same_verified_child_cannot_be_joined_or_billed_twice(completed):
    verifier = ResearchWorkProof(completed.replay)
    verifier.verify_document(completed.originals[0], *completed.joins[:2],
        validation_context=validation_context(completed))
    with pytest.raises(EvidenceInvalid):
        verifier.verify_document(completed.originals[0], *completed.joins[:2],
            validation_context=validation_context(completed))


@pytest.mark.parametrize("field", ["source_sha256", "input_sha256", "deadline_at", "tenant_id"])
def test_work_manifest_must_match_frozen_parent_identity(completed, field):
    manifest = deepcopy(completed.store.manifest)
    manifest[field] = ("5" * 64 if field.endswith("sha256") else
                       "2026-11-09T00:00:00+00:00" if field == "deadline_at" else "other-tenant")
    completed.replay.admission = deepcopy(completed.admission)
    if field == "source_sha256":
        completed.replay.admission[field] = manifest[field]
    elif field == "deadline_at":
        completed.replay.admission["clock"]["deadline_at_utc"] = manifest[field]
    elif field == "input_sha256":
        completed.replay.admission["request_sha256"] = "6" * 64
    else:
        (completed.store.root / "work-manifest.json").write_bytes(json_bytes(manifest))
    with pytest.raises(EvidenceInvalid):
        ResearchWorkProof(completed.replay)


def test_only_explicit_new_policy_can_activate_child_proof(completed):
    completed.replay.policy = deepcopy(completed.policy)
    completed.replay.policy.pop("research_work")
    with pytest.raises(EvidenceInvalid):
        ResearchWorkProof(completed.replay)


def test_boolean_cannot_impersonate_numeric_research_policy_limit(completed):
    policy = deepcopy(completed.policy)
    policy["research_work"]["format_retry_limit"] = True
    completed.replay.policy = policy
    completed.replay.admission["policy_sha256"] = digest(json_bytes(policy))
    atomic_json(completed.context.store.root, "policy.json", policy)
    with pytest.raises(EvidenceInvalid):
        ResearchWorkProof(completed.replay)


def test_empty_original_scope_still_requires_one_native_child_and_exact_empty_result(observed, monkeypatch):
    admission, policy, acquisition, client = prepare_proof_context(observed)
    wires = synthetic_transport(monkeypatch)
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "empty scope"}) as scope:
        documents = clean_round(client, [], acquisition=acquisition)
        scope.output(documents)
        call = scope.io_calls[0]
    replay = FixtureReplay(observed, admission, policy)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("Proof attempted network"))
    verifier = ResearchWorkProof(replay)
    verifier.verify_empty(call)
    summary = verifier.assert_complete()
    assert documents == [] and len(wires) == 1
    assert summary["document_count"] == 0 and summary["verified_units"] == 1


def test_unjoined_or_unrecognized_graph_scope_cannot_claim_completion(completed):
    verifier, _ = verify_documents(completed)
    extra = UnitSpec("unexpected-unit", "unexpected", {}, {"schema": "fixture/v1", "validator": "fixture/v1"})
    completed.store.extend_plan([extra], originals=[])
    lease = completed.store.claim(extra.unit_id, "fixture")
    completed.store.publish_result(lease, {"unexpected": True}, validator=lambda value: None)
    with pytest.raises(EvidenceInvalid):
        verifier.assert_complete()


def snapshot_reference(fixture, documents):
    bundle = {"bundle_id": "fixture-bundle", "manifest_sha256": "7" * 64,
              "gaps": [{"cell_id": "fixture-gap", "status": "unknown"}]}
    registry = fixture.acquisition.registry.public_manifest()
    frontier = fixture.acquisition.frontier
    value = snapshot_manifest(work_manifest=fixture.store.manifest,
        policy=research_work_policy(), docs=documents, frontier=frontier,
        bundle=bundle, source_registry=registry)
    ref = fixture.store._blob(value, value["schema"])
    reference = {"schema": value["schema"], "manifest_sha256": digest(json_bytes(value)), "ref": ref}
    return reference, value, bundle, registry, frontier


@pytest.mark.parametrize("mutation", [None, "source", "annotation", "eligibility", "coverage", "as_of", "bundle", "extra"])
def test_snapshot_is_reconstructed_from_source_facts_scope_and_coverage(completed, mutation):
    verifier, documents = verify_documents(completed)
    reference, value, bundle, registry, frontier = snapshot_reference(completed, documents)
    if mutation:
        if mutation in {"source", "annotation", "eligibility"}:
            field = {"source": "content_sha256", "annotation": "annotation_sha256",
                     "eligibility": "eligibility_sha256"}[mutation]
            value["documents"][0][field] = "9" * 64
        elif mutation == "coverage":
            value["coverage"][0]["status"] = "present"
        elif mutation == "as_of":
            value["as_of"] = "2026-11-08T00:00:00+00:00"
        elif mutation == "bundle":
            value["bundle_sha256"] = "9" * 64
        else:
            value["claimed_complete"] = True
        reference["ref"] = completed.store._blob(value, value["schema"])
        reference["manifest_sha256"] = digest(json_bytes(value))
        with pytest.raises(EvidenceInvalid):
            verifier.verify_snapshot(reference, docs=documents, frontier=frontier,
                bundle=bundle, source_registry=registry)
    else:
        assert verifier.verify_snapshot(reference, docs=documents, frontier=frontier,
            bundle=bundle, source_registry=registry) == value


def test_wire_reconstruction_rejects_plausible_but_different_annotation_prompt(completed):
    verifier = ResearchWorkProof(completed.replay)
    joined = read_snapshot(completed.context.store.root, completed.joins[0]["output_ref"],
        run_id=completed.context.run_id, stage="clean.llm_semantic", role="output")
    call = joined["io_calls"][0]
    arguments = document_arguments("extract", completed.originals[0],
        __import__("trading_system.llm.work_required", fromlist=["TARGET_IDS"]).TARGET_IDS,
        completed.replay.assessment)
    arguments["public_payload"]["source"]["content"] += " Coherent but unadmitted extra fact."
    with pytest.raises(EvidenceInvalid):
        verifier.verify_chat("clean.llm_semantic", call, arguments)


def full_research_fixture(tmp_path, monkeypatch):
    from test_intelligence_workflow_v2 import answer, intelligence_fixture, semantic

    real_post = DeepSeekRequiredClient._post_json
    context, root, args, _, denied, _ = intelligence_fixture(tmp_path, monkeypatch,
        clean_format_contract=semantic.CLEAN_FORMAT_CONTRACT)
    context.response_observation_contract = TOOL_CONTRACT
    research = research_work_policy()
    packet = read_json(root, "intelligence-input.json")
    policy = strict_json(json_bytes(policy_manifest(INTELLIGENCE_PROFILE, authoritative_paper=False,
        intelligence_input=packet, research_work=research, response_observation_contract=TOOL_CONTRACT)))
    admission = {"run_id": context.run_id, "request_sha256": "1" * 64,
        "runtime_input_sha256": "2" * 64, "source_sha256": digest((root / "source-manifest.json").read_bytes()),
        "policy_sha256": digest(json_bytes(policy)), "parent": None,
        "clock": {"admitted_at_utc": context.admitted_at.isoformat(),
            "deadline_at_utc": (context.admitted_at + timedelta(hours=2)).isoformat(),
            "run_date": context.run_date.isoformat(), "market_timezone": "America/New_York"}}
    atomic_json(root, "policy.json", policy)
    context.research_work_policy = research
    context.research_work_store = WorkStore.create(root / "tasks", work_manifest(admission),
        clock=lambda: context.admitted_at.timestamp())
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", real_post)
    calls, lock = [], Lock()

    def post(url, **kwargs):
        import hashlib

        wire = json.loads(kwargs["data"])
        if url.endswith("/v1/tools/search"):
            key = hashlib.sha256(wire["query"].encode()).hexdigest()
            response = {"results": [{"url": "https://www.reuters.com/fixture/" + key,
                "title": "TEST_FIXTURE research work " + key, "content": "Discovery text " + key,
                "date": "2026-10-19T14:00:00Z"}]}
            stage = "search.collect"
        else:
            content = wire["messages"][1]["content"]
            schema = json.loads(content.split("\nJSON schema:\n", 1)[1].split("\nJSON field guidance:\n", 1)[0])
            payload = json.loads(content.split("\n", 1)[1].split("\nJSON schema:\n", 1)[0])
            if payload.get("phase") == "extract":
                extracted = annotation()
                extracted.update(events=[], subjects=[], evidence_spans=[], event_records=[], claims=[],
                    summary_zh="TEST_FIXTURE：原件没有可核验的产业链事实。")
                value, stage = {"annotation": extracted}, "clean.llm_semantic"
            elif payload.get("phase") == "document_review":
                value, stage = review(), "clean.llm_semantic"
            else:
                properties = schema["properties"]
                stage = ("tech.sentiment" if "chains" in properties else
                    "tech.risk" if "chain_assessments" in properties else
                    "sector.narrative" if "sectors" in properties else "decision.debate")
                value = answer(stage, schema, payload)
            with lock:
                identifier = "full-research-fixture-" + str(len(calls) + 1)
                calls.append((stage, url))
            response = native(json.dumps(value, ensure_ascii=False), identifier)
        value = Response(response)
        value.url, value.history = url, []
        return value

    monkeypatch.setattr(requests, "post", post)
    return context, root, args, admission, denied, calls


def test_all_five_model_stages_and_source_children_pass_independent_full_replay(tmp_path, monkeypatch):
    import main
    from trading_system.llm.intelligence_proof import verify_intelligence_model

    context, root, args, admission, denied, calls = full_research_fixture(tmp_path, monkeypatch)
    actual = main._daily(args, "yahoo", execution=context)
    context.assert_complete()
    atomic_json(root, "TEST_FIXTURE-result.json", asdict(actual.result), exclusive=True)
    atomic_json(root, "TEST_FIXTURE-admission.json", admission, exclusive=True)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("Proof attempted network"))
    summary = verify_intelligence_model(root, asdict(actual.result), admission)
    assert summary["all_required_completed"] and all(summary["stage_counts"].values())
    accounting = summary["chat_accounting"]
    assert accounting["unknown_attempts"] == 0
    assert accounting["known_native_attempts"] == summary["call_count"]
    assert accounting["known_usage"]["total_tokens"] == summary["call_count"] * 50
    assert len(calls) == summary["call_count"]
    assert denied == [] and actual.result.picks == []


def failed_work_fixture(context, monkeypatch, *, second_unknown=False):
    admission, policy, acquisition, client = prepare_proof_context(context)
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs["data"])
        response = (Response(body=b"{bad HTTP JSON") if second_unknown and len(calls) == 2 else
            Response(native("{bad assistant JSON", "partial-work-" + str(len(calls)))))
        response.url, response.history = url, []
        return response
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(WorkUnitError):
        with context.step("clean.llm_semantic", {"TEST_FIXTURE": "failed work accounting"}):
            clean_round(client, [raw()], acquisition=acquisition)
    assert len(calls) == 2 and calls[0] == calls[1]
    replay = FixtureReplay(context, admission, policy)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("Accounting opened network"))
    return replay


@pytest.mark.parametrize("second_unknown", [False, True])
def test_partial_work_keeps_rejected_known_usage_and_unknown_reservations(observed, monkeypatch, second_unknown):
    replay = failed_work_fixture(observed, monkeypatch, second_unknown=second_unknown)
    result = partial_work_accounting(replay)
    assert result["schema"] == "tiger.work-native-accounting/v1"
    assert result["semantics_state"] == "unverified"
    assert result["business_completed"] is False and result["all_required_completed"] is False
    assert result["format_reanswers"] == 1 and result["attempt_count"] == 2
    assert result["known_native_attempts"] == (1 if second_unknown else 2)
    assert result["known_usage"]["total_tokens"] == (50 if second_unknown else 100)
    assert result["unknown_attempts"] == int(second_unknown)
    assert result["requires_reconciliation"] is second_unknown
    assert len(result["unknown_reservations"]) == int(second_unknown)
    assert all(row["native_ref"] is not None for row in result["attempts"])
    if second_unknown:
        reservation = result["unknown_reservations"][0]
        assert reservation["ordinal"] == 2 and reservation["reservation"]["output_tokens"] > 0
        assert result["attempts"][1]["usage_known"] is False


def test_parent_counted_native_ids_are_checked_and_excluded_from_additional_totals(observed, monkeypatch):
    replay = failed_work_fixture(observed, monkeypatch)
    original = partial_work_accounting(replay)
    rows = [{"stage": item["stage"], "call_id": item["call_id"], "ordinal": item["ordinal"],
             "response_id": item["provider_response_id"], "usage": item["usage"]}
            for item in original["attempts"]]
    result = partial_work_accounting(replay, parent_accounting={"observed_attempts": rows + deepcopy(rows)})
    assert result["already_parent_counted_native_attempts"] == 2
    assert result["known_usage"]["total_tokens"] == 100
    assert result["additional_known_native_attempts"] == 0
    assert result["additional_known_usage"]["total_tokens"] == 0
    rows[0]["usage"]["completion_tokens"] += 1
    with pytest.raises(EvidenceInvalid):
        partial_work_accounting(replay, parent_accounting={"observed_attempts": rows})


def test_failed_parent_report_uses_v4_and_does_not_hide_unjoined_child_usage(observed, monkeypatch):
    from trading_system.llm.intelligence_proof import verify_partial_intelligence_model

    replay = failed_work_fixture(observed, monkeypatch, second_unknown=True)
    result = verify_partial_intelligence_model(replay.run, replay.admission)
    assert result["schema"] == "tiger.failed-model-accounting/v4"
    assert result["known_usage"]["total_tokens"] == 50 and result["unknown_attempts"] == 1
    assert result["native_count"] == 2 and result["format_reanswers"] == 1
    assert result["requires_reconciliation"] and result["business_completed"] is False
    assert result["parent_observed_attempts"] == [] and "observed_attempts" not in result
    assert len(result["research_work"]["attempts"]) == 2


def test_late_known_native_is_reported_without_clearing_original_unknown_hold(observed, monkeypatch):
    from trading_system.tasks.contracts import UnitLease
    from trading_system.llm.work_semantics import digest as semantic_hash

    replay = failed_work_fixture(observed, monkeypatch, second_unknown=True)
    store = observed.research_work_store
    attempts = sorted(store.attempts_for_unit(store.status()["units"][0]["unit_id"].replace(
        ":document_review", ":extract")), key=lambda row: row["ordinal"])
    first, second = attempts
    saved = deepcopy(store.read_attempt_response(first["attempt_id"]))
    saved["ordinal"] = 2
    saved["response"]["id"] = "late-known-native"
    saved["response_sha256"] = semantic_hash(saved["response"])
    lease = UnitLease(store.manifest["tenant_id"], store.manifest["run_id"], store.manifest_sha256,
        second["unit_id"], second["owner"], second["epoch"], observed.admitted_at.timestamp() + 120)
    amount = {key: saved["response"]["usage"][key] for key in
              ("prompt_tokens", "completion_tokens", "total_tokens")}
    store.record_response(lease, second["attempt_id"], saved, usage=amount)
    result = partial_work_accounting(replay)
    assert result["known_usage"]["total_tokens"] == 100
    assert result["known_usage_settled"]["total_tokens"] == 50
    assert result["unknown_attempts"] == 1 and result["requires_reconciliation"] is True
    assert len(result["unknown_reservations"]) == 1
    assert len(result["attempts"][1]["native_observations"]) == 2


def test_prepared_intent_is_not_sent_and_does_not_invent_unknown_provider_usage(observed, monkeypatch):
    from trading_system.llm.work_proof import _document_unit
    from trading_system.llm.work_semantics import digest as semantic_hash, text_digest

    replay = failed_work_fixture(observed, monkeypatch)
    store = observed.research_work_store
    original = raw("prepared-source")
    context = {"security_ids": {}, "entity_registry": {}, "source_policies": {"sources": {}, "sha256": "4" * 64}}
    specs = [_document_unit(original, phase, assessment=replay.assessment, validation_context=context)
             for phase in ("extract", "document_review")]
    store.extend_plan(specs, originals=[{"id": original.doc_id, "sha256": text_digest(original.content)}])
    lease = store.claim(specs[0].unit_id, "prepared-fixture")
    request = deepcopy(store.read_attempt_request(store.attempts_for_unit(
        next(row["unit_id"] for row in store.status()["units"] if row["spec"]["kind"] == "document.extract"
             and row["unit_id"] != specs[0].unit_id))[0]["attempt_id"]))
    arguments = document_arguments("extract", original,
        __import__("trading_system.llm.work_required", fromlist=["TARGET_IDS"]).TARGET_IDS, replay.assessment)
    body, count, ceiling = ResearchWorkProof(replay).plan("clean.llm_semantic", arguments)
    request.update(call_id="f" * 32, request=body, request_sha256=semantic_hash(body),
                   input_token_estimate=count, output_ceiling=ceiling)
    store.begin_attempt(lease, request, operation="model.deepseek.chat", reservation={"output_tokens": ceiling})
    result = partial_work_accounting(replay)
    prepared = next(row for row in result["attempts"] if row["unit_id"] == specs[0].unit_id)
    assert prepared["ledger_state"] == "prepared" and prepared["dispatch_state"] == "not_sent"
    assert prepared["native_ref"] is None and prepared["usage_known"] is False
    assert result["unknown_attempts"] == 0 and result["known_usage"]["total_tokens"] == 100
    assert len(result["unknown_reservations"]) == 1 and not result["requires_reconciliation"]


def test_archived_parent_attempt_can_account_against_unchanged_root_evidence(observed, monkeypatch):
    from trading_system.llm.intelligence_proof import verify_partial_intelligence_model, verify_intelligence_model

    replay = failed_work_fixture(observed, monkeypatch, second_unknown=True)
    relative = "parent-attempts/1/events.jsonl"
    atomic_bytes(replay.run, relative, (replay.run / "events.jsonl").read_bytes(), exclusive=True)
    (replay.run / "events.jsonl").unlink()
    result = verify_partial_intelligence_model(replay.run, replay.admission, event_relative=relative)
    assert result["parent_event_relative"] == relative
    assert result["schema"] == "tiger.failed-model-accounting/v4"
    assert result["known_usage"]["total_tokens"] == 50 and result["unknown_attempts"] == 1
    with pytest.raises(EvidenceInvalid):
        verify_partial_intelligence_model(replay.run, replay.admission)
    with pytest.raises(EvidenceInvalid):
        verify_intelligence_model(replay.run, {"raw": {}}, replay.admission)


@pytest.mark.parametrize("relative", ["../events.jsonl", "/tmp/events.jsonl",
    "parent-attempts/one/events.jsonl", "parent-attempts/1/../events.jsonl",
    "parent-attempts/1/events.jsonl/extra", None])
def test_parent_attempt_event_path_is_closed_and_never_an_arbitrary_file(observed, monkeypatch, relative):
    from trading_system.llm.intelligence_proof import verify_partial_intelligence_model

    replay = failed_work_fixture(observed, monkeypatch)
    with pytest.raises(EvidenceInvalid):
        verify_partial_intelligence_model(replay.run, replay.admission, event_relative=relative)


def test_old_policy_cannot_opt_into_archived_parent_event_paths(observed, monkeypatch):
    from trading_system.llm.intelligence_proof import verify_partial_intelligence_model

    replay = failed_work_fixture(observed, monkeypatch)
    relative = "parent-attempts/1/events.jsonl"
    atomic_bytes(replay.run, relative, (replay.run / "events.jsonl").read_bytes(), exclusive=True)
    policy = deepcopy(replay.policy)
    policy.pop("research_work")
    atomic_json(replay.run, "policy.json", policy)
    with pytest.raises(EvidenceInvalid):
        verify_partial_intelligence_model(replay.run, replay.admission, event_relative=relative)


@pytest.mark.parametrize("trusted", [None, {"html_engine": "scrapling"}])
def test_only_explicit_research_policy_selects_v1_parser_manifest_and_same_limits(trusted):
    from dataclasses import replace
    from trading_system.intelligence.registry import SourceSpec
    from trading_system.intelligence.parse_limits import ParsePolicy
    from trading_system.llm.intelligence_proof import _source_parameters

    spec = SourceSpec("reviewed", "page", "https://example.com/original",
        options={} if trusted is None else {"extractor": trusted})
    parser = replace(ParsePolicy.from_value(trusted), manifest_version="v1").public()
    parameters = {"url": spec.endpoint, "_parse_policy": parser}
    assert _source_parameters(spec, "fetch", parameters, research_work=True) == {"url": spec.endpoint}
    with pytest.raises(EvidenceInvalid):
        _source_parameters(spec, "fetch", parameters)
    changed = deepcopy(parameters)
    changed["_parse_policy"]["memory_bytes"] += 1
    with pytest.raises(EvidenceInvalid):
        _source_parameters(spec, "fetch", changed, research_work=True)


def test_source_reservations_remain_held_until_original_attempt_is_settled():
    from trading_system.llm.work_proof import source_usage

    attempts = [
        {"operation": "fetch", "status": "unknown", "request": {"reserved_bytes": 100, "reserved_requests": 3},
         "result": {"request_count": 2, "bytes_received": 40, "usage": {"requires_reconciliation": True}}},
        {"operation": "search", "status": "completed", "request": {"reserved_bytes": 200, "reserved_requests": 4},
         "result": {"request_count": 1, "bytes_received": 10, "usage": {"requires_reconciliation": False}}}]
    result = source_usage(attempts)
    assert result == {"requests": 3, "fetches": 1, "bytes": 50, "unreconciled_attempts": 1,
        "reserved_bytes": 100, "reserved_requests": 3, "committed_bytes": 10, "committed_requests": 1}
    attempts[0]["status"] = "failed"
    attempts[0]["result"]["usage"]["requires_reconciliation"] = False
    result = source_usage(attempts)
    assert result["reserved_bytes"] == result["reserved_requests"] == 0
    assert result["committed_bytes"] == 50 and result["committed_requests"] == 3


@pytest.mark.parametrize("mutation", [None, "boolean", "oversized", "unfinished", "no_start", "configured", "extra"])
def test_source_operational_counter_is_bound_to_closed_start_finish_receipts(mutation):
    from trading_system.llm.work_proof import source_runtime_from_events

    start = {"schema": "tiger.source-runtime/v1", "configured_workers": 4, "admitted_workers": 3,
        "peak_source_operations": 0, "active_source_operations": 0,
        "paid_source_policy": "serial_with_reconciliation_barrier", "host_policy": "durable_per_http_host"}
    finish = {**start, "peak_source_operations": 2}
    events = [{"kind": "source_runtime_started", "payload": start},
              {"kind": "source_runtime_finished", "payload": finish}]
    if mutation == "boolean":
        finish["peak_source_operations"] = True
    elif mutation == "oversized":
        finish["peak_source_operations"] = 4
    elif mutation == "unfinished":
        events.pop()
    elif mutation == "no_start":
        events.pop(0)
    elif mutation == "configured":
        finish["configured_workers"] = 2
    elif mutation == "extra":
        finish["unproved_overlap"] = True
    if mutation:
        with pytest.raises(EvidenceInvalid):
            source_runtime_from_events(events, configured_workers=4, concurrency_limit=3)
    else:
        assert source_runtime_from_events(events, configured_workers=4, concurrency_limit=3) == finish


@pytest.mark.parametrize("kind,code", [("length", "MODEL_OUTPUT_TRUNCATED"), ("choices", "INVALID_CHOICES")])
def test_explicit_rejected_native_keeps_known_usage_without_success_or_format_retry(observed, monkeypatch, kind, code):
    admission, policy, acquisition, client = prepare_proof_context(observed)
    sent = []
    def post(url, **kwargs):
        sent.append(kwargs["data"])
        value = native("{truncated assistant content", "known-rejected-" + kind)
        if kind == "length":
            value["choices"][0]["finish_reason"] = "length"
        else:
            value["choices"] = []
        response = Response(value)
        response.url, response.history = url, []
        return response
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(WorkUnitError):
        with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "known rejected envelope"}):
            clean_round(client, [raw()], acquisition=acquisition)
    replay = FixtureReplay(observed, admission, policy)
    result = partial_work_accounting(replay)
    assert len(sent) == 1 and result["known_usage"]["total_tokens"] == 50
    assert result["unknown_attempts"] == 0 and not result["unknown_reservations"]
    assert result["format_reanswers"] == 0 and result["semantics_state"] == "unverified"
    row = result["attempts"][0]
    assert row["provider_response_id"] == "known-rejected-" + kind
    assert row["native_observations"][0]["diagnostic"]["code"] == code
    store = observed.research_work_store
    native_value = store.read_attempt_response(row["attempt_id"])
    assert native_value["response"] is None and "rejected_native" in native_value
    if kind == "length":
        assert native_value["rejected_native"]["finish_reason"] == "length"


def test_native_rejection_marker_cannot_be_injected_into_old_policy_or_dropped_from_new(completed):
    from trading_system.llm.intelligence_proof import _partial_chat_request

    joined = read_snapshot(completed.context.store.root, completed.joins[0]["output_ref"],
        run_id=completed.context.run_id, stage="clean.llm_semantic", role="output")
    request = read_snapshot(completed.context.store.root, joined["io_calls"][0]["input_ref"],
        run_id=completed.context.run_id, stage="clean.llm_semantic", role="input")
    with pytest.raises(EvidenceInvalid):
        _partial_chat_request(request, "clean.llm_semantic", None,
            counter_contract=model_budget.budget_contract())
    request.pop("rejected_native_contract")
    with pytest.raises(EvidenceInvalid):
        _partial_chat_request(request, "clean.llm_semantic", None,
            counter_contract=model_budget.budget_contract(),
            rejected_native_contract=research_work_policy()["rejected_native_contract"])


@pytest.mark.parametrize("mutation", [None, "omitted_version", "id", "title", "url", "published_at",
                                     "source_id", "content_sha256", "completeness"])
def test_coherently_changed_public_source_indexes_still_bind_every_original(completed, mutation):
    import hashlib
    from trading_system.llm.work_proof import verify_source_index

    rows = [{"id": doc.raw.doc_id, "title": doc.raw.title, "url": doc.raw.url,
        "published_at": doc.raw.published or None, "source_id": doc.raw.source,
        "content_sha256": hashlib.sha256(doc.raw.content.encode()).hexdigest(),
        "completeness": doc.raw.meta.get("completeness", "unknown")} for doc in completed.documents]
    if mutation == "omitted_version":
        rows.pop()
    elif mutation:
        rows[0][mutation] = "TEST_FIXTURE coherent but false public index"
    actual, reported = {"source_index": deepcopy(rows)}, {"source_index": deepcopy(rows)}
    if mutation:
        with pytest.raises(EvidenceInvalid):
            verify_source_index(actual, reported, completed.documents)
    else:
        assert verify_source_index(actual, reported, completed.documents) == rows
        assert len(rows) == 2 and rows[0]["content_sha256"] == rows[1]["content_sha256"]


def test_coherently_changed_report_run_id_cannot_relabel_its_admission():
    from trading_system.llm.work_proof import verify_report_identity

    admission = {"run_id": "original-run"}
    verify_report_identity({"run_id": "original-run"}, {"run_id": "original-run"}, admission)
    with pytest.raises(EvidenceInvalid):
        verify_report_identity({"run_id": "another-run"}, {"run_id": "another-run"}, admission)
