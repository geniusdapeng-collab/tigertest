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
from trading_system.tasks import WorkStore, WorkUnitError


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


def test_wrong_literal_never_becomes_reviewed_document(observed, monkeypatch):
    store, acquisition, client = prepare(observed)
    calls = transport(monkeypatch, extract_mutation=lambda value: value["annotation"]["claims"][0].update(value="999"))
    with pytest.raises(WorkUnitError):
        with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "wrong amount"}):
            clean_round(client, [raw()], acquisition=acquisition)
    assert len(calls) == 1
    assert store.document_dispositions()["terminal"] == 0
    assert store.status()["settled_attempts"] == 1
    assert store.status()["format_reanswers"] == 0


def test_semantic_review_failure_does_not_spend_format_retry(observed, monkeypatch):
    store, acquisition, client = prepare(observed)
    calls = transport(monkeypatch, review_mutation=lambda value: value.update(omissions=["遗漏负面指引"]))
    with pytest.raises(WorkUnitError):
        with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "review failure"}):
            clean_round(client, [raw()], acquisition=acquisition)
    assert len(calls) == 2
    assert store.document_dispositions()["terminal"] == 0
    assert store.status()["settled_attempts"] == 2
    assert store.status()["format_reanswers"] == 0
