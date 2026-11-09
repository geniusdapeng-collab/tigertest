"""Actual client and durable child joins; local synthetic HTTP, no paid calls."""
from copy import deepcopy
import json
from threading import Lock
from types import SimpleNamespace

import pytest
import requests

from test_deepseek_json_observation import observed, native
from test_deepseek_required import Response
from test_research_work_semantics import raw, annotation
from test_task_work_units import BASE, manifest
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.llm.work_required import clean_round
from trading_system.research_work import research_work_policy
from trading_system.tasks import WorkStore


def review():
    return {"complete": True, "checks": {key: True for key in
        ("subjects", "time_and_period", "amounts_units_currency", "counterevidence", "citations", "material_facts")},
        "omissions": [], "contradictions": [], "notes": "TEST_FIXTURE逐项核对"}


def prepare(context):
    context.research_work_policy = research_work_policy()
    context.research_work_store = WorkStore.create(context.store.root / "tasks", manifest(), clock=lambda: BASE)
    acquisition = SimpleNamespace(frontier={"security_ids": {}}, entities={},
        registry=SimpleNamespace(public_manifest=lambda: {"sources": {}}))
    return context.research_work_store, acquisition, DeepSeekRequiredClient(context)


def transport(monkeypatch, *, extract_mutation=None, review_mutation=None):
    calls, lock = [], Lock()
    def post(_url, **kwargs):
        wire = json.loads(kwargs["data"])
        prompt = wire["messages"][-1]["content"]
        phase = "extract" if '"phase":"extract"' in prompt else "document_review"
        value = {"annotation": annotation()} if phase == "extract" else review()
        mutation = extract_mutation if phase == "extract" else review_mutation
        if mutation:
            mutation(value)
        with lock:
            calls.append({"phase": phase, "wire": wire})
            identifier = "work-test-" + str(len(calls))
        return Response(native(json.dumps(value, ensure_ascii=False), identifier))
    monkeypatch.setattr(requests, "post", post)
    return calls


def schema_retry_transport(monkeypatch):
    calls, counts, lock = [], {}, Lock()
    def post(_url, **kwargs):
        wire = json.loads(kwargs["data"])
        prompt = wire["messages"][-1]["content"]
        phase = "extract" if '"phase":"extract"' in prompt else "document_review"
        payload_text = prompt.split("\n", 1)[1].split("\nJSON schema:\n", 1)[0]
        payload = json.loads(payload_text)
        source_id = payload["source"]["id"]
        with lock:
            counts[source_id] = counts.get(source_id, 0) + 1
            ordinal = counts[source_id]
            calls.append({"phase": phase, "source_id": source_id})
            identifier = "schema-retry-" + str(len(calls))
        if phase == "extract" and ordinal == 1:
            value = {"annotation": {"tickers": []}}
        else:
            value = {"annotation": annotation()} if phase == "extract" else review()
        return Response(native(json.dumps(value, ensure_ascii=False), identifier))
    monkeypatch.setattr(requests, "post", post)
    return calls


def review_schema_transport(monkeypatch):
    calls, lock = [], Lock()
    def post(_url, **kwargs):
        wire = json.loads(kwargs["data"])
        prompt = wire["messages"][-1]["content"]
        phase = "extract" if '"phase":"extract"' in prompt else "document_review"
        value = ({"annotation": annotation()} if phase == "extract"
                 else {"complete": "not-a-boolean"})
        with lock:
            calls.append(phase)
            identifier = "review-schema-" + str(len(calls))
        return Response(native(json.dumps(value, ensure_ascii=False), identifier))
    monkeypatch.setattr(requests, "post", post)
    return calls


@pytest.mark.parametrize("count", [1, 2])
def test_originals_have_isolated_extract_review_and_verified_parent_joins(observed, monkeypatch, count):
    store, acquisition, client = prepare(observed)
    calls = transport(monkeypatch)
    originals = [raw(), raw("source-2")][:count]
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": originals}) as scope:
        result = clean_round(client, originals, acquisition=acquisition)
        scope.output(result)
        assert [call["method"] for call in scope.io_calls] == ["tasks.child.join"] * (2 * count)
    assert [doc.raw.doc_id for doc in result] == [doc.doc_id for doc in originals]
    assert len(calls) == len(client.receipts) == 2 * count
    assert store.status()["graph_state"] == "completed"
    assert store.document_dispositions()["terminal"] == count
    assert store.verify()["unit_count"] == 2 * count
    assert len({row["response_id"] for row in client.receipts}) == 2 * count


def test_wrong_literal_becomes_a_dependency_bound_unverifiable_terminal(observed, monkeypatch):
    store, acquisition, client = prepare(observed)
    calls = transport(monkeypatch, extract_mutation=lambda value: value["annotation"]["claims"][0].update(value="999"))
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "wrong amount"}) as scope:
        result = clean_round(client, [raw()], acquisition=acquisition)
        scope.output({"documents": len(result)})
    assert result == []
    assert len(calls) == 1
    assert [call["method"] for call in scope.io_calls] == ["tasks.child.join"]
    dispositions = store.document_dispositions()
    assert dispositions["terminal"] == 1 and dispositions["unverifiable"] == 1
    terminal = dispositions["documents"][raw().doc_id]
    assert terminal["reason"] == "upstream_document_semantics_unverifiable"
    assert store.status()["graph_state"] == "completed"
    assert store.status()["settled_attempts"] == 1
    assert store.status()["format_reanswers"] == 0


def test_schema_rejection_without_retry_slot_becomes_a_verified_terminal(observed, monkeypatch):
    from trading_system.research_work import _POLICY
    monkeypatch.setitem(_POLICY, "document_workers", 1)
    store, acquisition, client = prepare(observed)
    calls = schema_retry_transport(monkeypatch)
    originals = [raw(), raw("source-2")]
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "schema retry exhausted"}) as scope:
        result = clean_round(client, originals, acquisition=acquisition)
        scope.output({"documents": [doc.raw.doc_id for doc in result]})
    assert [doc.raw.doc_id for doc in result] == ["source-1"]
    assert len(calls) == 4
    assert [call["method"] for call in scope.io_calls] == ["tasks.child.join"] * 3
    dispositions = store.document_dispositions()
    assert dispositions["eligible"] == 1 and dispositions["unverifiable"] == 1
    assert dispositions["documents"]["source-2"] == {
        "state": "unverifiable",
        "reason": "upstream_document_semantics_unverifiable",
    }
    assert store.status()["graph_state"] == "completed"
    assert store.status()["settled_attempts"] == 4
    assert store.status()["format_reanswers"] == 1


def test_review_schema_failure_uses_the_original_child_as_an_unverifiable_terminal(observed, monkeypatch):
    store, acquisition, client = prepare(observed)
    calls = review_schema_transport(monkeypatch)
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "review schema failure"}) as scope:
        result = clean_round(client, [raw()], acquisition=acquisition)
        scope.output({"documents": len(result)})
    assert result == [] and calls == ["extract", "document_review", "document_review"]
    assert [call["method"] for call in scope.io_calls] == ["tasks.child.join"] * 2
    disposition = store.document_dispositions()["documents"]["source-1"]
    assert disposition == {
        "state": "unverifiable",
        "reason": "upstream_document_semantics_unverifiable",
    }
    review = store.read_result(
        next(row["unit_id"] for row in store.status()["units"]
             if row["spec"]["kind"] == "document.document_review"))["value"]["output"]
    assert review["error_code"] == "WORK_OUTPUT_SCHEMA_MISMATCH"
    assert store.status()["graph_state"] == "completed"
    assert store.status()["settled_attempts"] == 3


def test_semantic_review_failure_is_an_unverifiable_terminal_without_format_retry(observed, monkeypatch):
    store, acquisition, client = prepare(observed)
    calls = transport(monkeypatch, review_mutation=lambda value: value.update(omissions=["遗漏负面指引"]))
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "review failure"}) as scope:
        result = clean_round(client, [raw()], acquisition=acquisition)
        scope.output({"documents": len(result)})
    assert result == []
    assert len(calls) == 2
    dispositions = store.document_dispositions()
    assert dispositions["terminal"] == 1 and dispositions["unverifiable"] == 1
    assert store.status()["graph_state"] == "completed"
    assert store.status()["settled_attempts"] == 2
    assert store.status()["format_reanswers"] == 0


def test_whole_document_segments_rejoin_every_fact_after_independent_reviews(observed, monkeypatch):
    from dataclasses import replace
    from trading_system.llm import work_required
    from trading_system.tasks.recovery import make_document_segments

    store, acquisition, client = prepare(observed)
    original = raw(content="Section one revenue was 120 USD.\n\nSection two risk was 80 USD.")
    split = original.content.index("\n\n") + 2
    manifest = make_document_segments(original.doc_id, original.content, [(0, split), (split, len(original.content))])
    segments = [replace(original, doc_id=f"{original.doc_id}:segment-{number}",
                        content=original.content[row["start"]:row["end"]])
                for number, row in enumerate(manifest["segments"], 1)]
    monkeypatch.setattr(work_required, "plan_document_segments",
                        lambda *_args, **_kwargs: (manifest, segments))

    calls, lock = [], Lock()
    def post(_url, **kwargs):
        wire = json.loads(kwargs["data"])
        prompt = wire["messages"][-1]["content"]
        with lock:
            calls.append(prompt)
            identifier = "segment-test-" + str(len(calls))
        if '"phase":"metadata_synthesis"' in prompt:
            value = {"summary_zh": "两段都由原文覆盖：收入120 USD，风险80 USD。"}
        elif '"phase":"extract"' in prompt:
            if "Section one" in prompt:
                quote = "Section one revenue was 120 USD."
            else:
                quote = "Section two risk was 80 USD."
            value = {"annotation": {
                "tickers": [], "sentiment": "neutral", "sentiment_score": 0,
                "events": [], "summary_zh": quote, "relevance": 1,
                "subjects": [], "evidence_spans": [{"id": "p1", "start": None, "end": None, "quote": quote}],
                "event_records": [], "claims": []}}
        else:
            value = review()
        return Response(native(json.dumps(value, ensure_ascii=False), identifier))
    monkeypatch.setattr(requests, "post", post)

    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "segmented original"}) as scope:
        result = clean_round(client, [original], acquisition=acquisition)
        scope.output({"documents": len(result)})
    assert len(result) == 1
    assert result[0].raw is original and result[0].summary_zh.startswith("两段")
    assert [row["quote"] for row in result[0].evidence_spans] == [
        "Section one revenue was 120 USD.", "Section two risk was 80 USD."]
    assert store.document_dispositions()["terminal"] == 1
    assert store.verify()["unit_count"] == 5
    assert len(calls) == len(client.receipts) == 5
