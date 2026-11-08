"""Real admitted client code, isolated SQLite/evidence, local fake transport only."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from threading import get_ident
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from test_deepseek_json_observation import observed, native, invoke
from test_deepseek_required import Response
from test_task_work_units import BASE, USAGE, accept, create, manifest, observed as work_observed, unit
from trading_system.agent_evidence import digest, json_bytes
from trading_system.redline.execution import ExecutionContractError
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from trading_system.tasks import (Coordinator, UnitExecutionContext, WorkStore, WorkUnitError,
                                  make_document_segments, verify_child_seal)


def child_fixture(tmp_path, parent, specs=None, clock=None):
    store = create(tmp_path, specs or [unit("extract")], clock=clock)
    lease = store.claim_ready("worker")
    child = UnitExecutionContext(parent, work_store=store, lease=lease, stage="clean.llm_semantic")
    return store, lease, child


def test_admitted_child_has_own_evidence_same_parent_run_and_complete_native_seal(observed, tmp_path, monkeypatch):
    store, lease, child = child_fixture(tmp_path, observed)
    calls = []
    def post(*args, **kwargs):
        status = store.status()
        assert status["unknown_attempts"] == 1 and status["held_reservations"]
        calls.append(kwargs["data"])
        return Response(native('{"score":7,"reason":"ok"}', "child-native"))
    monkeypatch.setattr(requests, "post", post)
    before = list(observed.summary())
    with child.step("clean.llm_semantic", {"TEST_FIXTURE": "whole original"}) as scope:
        scope.output(invoke(child))
    ref = child.seal()
    proof = verify_child_seal(store, ref)
    assert child.store.run_id == observed.run_id and child.store.root != observed.store.root
    assert proof["child_path"] == "children/extract/epoch-1"
    assert proof["event_count"] == 2 and len(calls) == 1
    assert observed.summary() == before
    assert store.status()["known_usage"]["total_tokens"] == 50
    store.publish_result(lease, {"child_seal_ref": ref}, validator=lambda value: verify_and_accept(store, value))
    reopened = WorkStore.open(store.root, manifest(), clock=lambda: BASE)
    assert reopened.resume_plan()["reusable"] == ["extract"]
    assert verify_child_seal(reopened, reopened.read_result("extract")["value"]["child_seal_ref"]) == proof


def verify_and_accept(store, value):
    verify_child_seal(store, value["child_seal_ref"])


def test_child_same_wire_reanswer_is_globally_durable_before_second_send(observed, tmp_path, monkeypatch):
    store, lease, child = child_fixture(tmp_path, observed)
    replies = iter([native('{"score":', "bad-child"), native('{"score":7,"reason":"ok"}', "good-child")])
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs["data"])
        if len(calls) == 2:
            assert store.status()["format_reanswers"] == 1
            assert store.status()["settled_attempts"] == 1
            assert store.status()["unknown_attempts"] == 1
        return Response(next(replies))
    monkeypatch.setattr(requests, "post", post)
    with child.step("clean.llm_semantic", {"TEST_FIXTURE": "whole original"}) as scope:
        scope.output(invoke(child))
    proof = verify_child_seal(store, child.seal())
    assert proof["event_count"] == 2 and len(calls) == 2 and calls[0] == calls[1]
    assert store.status()["known_usage"]["total_tokens"] == 100
    assert store.status()["format_reanswers"] == 1


def test_child_unknown_http_envelope_preserves_native_and_holds_reservation(observed, tmp_path, monkeypatch):
    store, lease, child = child_fixture(tmp_path, observed)
    calls = []
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: calls.append(1) or Response(body=b"{"))
    with pytest.raises(RequiredModelUnavailable):
        with child.step("clean.llm_semantic", {"TEST_FIXTURE": "whole original"}):
            invoke(child)
    assert len(calls) == 1 and store.status()["known_usage"] is None
    assert store.status()["unknown_attempts"] == 1 and store.status()["held_reservations"]
    with store._tx() as connection:
        attempts = store._rows(connection, "attempts")
    assert store.read_attempt_response(attempts[0]["attempt_id"])["response"] is None
    with pytest.raises(ExecutionContractError):
        child.seal()


def test_child_late_native_is_observed_without_reviving_expired_lease(observed, tmp_path, monkeypatch):
    clock = [BASE]
    store, lease, child = child_fixture(tmp_path, observed, clock=lambda: clock[0])
    def post(*args, **kwargs):
        clock[0] += 61
        return Response(native('{"score":7,"reason":"late"}', "late-child"))
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(WorkUnitError, match="lease"):
        with child.step("clean.llm_semantic", {"TEST_FIXTURE": "whole original"}):
            invoke(child)
    assert store.status()["unknown_attempts"] == 1
    with store._tx() as connection:
        observations = store._rows(connection, "observations")
    assert len(observations) == 1 and observations[0]["late"] == 1
    assert observations[0]["usage"]["total_tokens"] == 50


def test_child_does_not_relax_historical_parent_thread_owner(observed, tmp_path):
    store, lease, child = child_fixture(tmp_path, observed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ExecutionContractError, match="owner thread"):
            pool.submit(lambda: observed._check_thread()).result(timeout=2)
    assert observed._failed
    assert child._owner_thread == get_ident()


def test_child_seal_rejects_other_valid_stage_output_reference(observed, tmp_path, monkeypatch):
    store, lease, child = child_fixture(tmp_path, observed)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response(native('{"score":7,"reason":"ok"}', "child-native")))
    with child.step("clean.llm_semantic", {"TEST_FIXTURE": "whole original"}) as scope:
        scope.output(invoke(child))
    ref = child.seal()
    proof = store.read_artifact(ref, schema=ref["schema"])
    other = child.store.snapshot(stage="clean.llm_semantic", role="output", value={"TEST_FIXTURE": "unrelated"})
    proof["output_ref"] = other
    forged = store._blob(proof, ref["schema"])
    with pytest.raises(WorkUnitError, match="references"):
        verify_child_seal(store, forged)


def test_writable_projection_tamper_is_caught_by_event_replay(tmp_path):
    store = create(tmp_path)
    connection = sqlite3.connect(store.database)
    try:
        connection.execute("UPDATE units SET state='validated'")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(WorkUnitError) as caught:
        store.verify()
    assert caught.value.code == "PROJECTION_INVALID"


def test_immutable_event_observation_and_retry_rows_have_sql_guards(tmp_path):
    store = create(tmp_path)
    lease = store.claim_ready("worker")
    attempt, receipt = work_observed(store, lease)
    store.claim_format_retry(lease, attempt.attempt_id, eligibility_verifier=lambda *args: True)
    connection = sqlite3.connect(store.database)
    try:
        for table in ("events", "observations", "retry_claims"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM " + table)
            connection.rollback()
    finally:
        connection.close()
    assert store.verify()["unit_count"] == 1


@pytest.mark.parametrize("target", ["work-units.sqlite3", "work-units.sqlite3-wal", "work-units.sqlite3-shm"])
def test_hardlinked_database_or_sidecar_is_rejected_without_touching_target(tmp_path, target):
    store = create(tmp_path)
    outside = tmp_path / "protected"
    path = store.root / target
    # A clean closed WAL may be absent. The next connection must reject a
    # precreated hardlinked sidecar before SQLite can overwrite it.
    if path.exists():
        path.link_to(outside) if hasattr(path, "link_to") else outside.hardlink_to(path)
        before = outside.read_bytes()
    else:
        outside.write_bytes(b"TEST_FIXTURE protected")
        path.hardlink_to(outside)
        before = outside.read_bytes()
    with pytest.raises(WorkUnitError):
        store.status()
    assert outside.read_bytes() == before


def test_caller_planned_segments_preserve_whole_unicode_original_and_context():
    text = "完整原文第一段🙂第二段。第三段与反证。"
    result = make_document_segments("doc", text, [(0, 8), (8, len(text))], context_characters=4)
    assert result["complete"] and result["source_sha256"] == digest(text.encode())
    assert result["segments"][0]["context_end"] > result["segments"][0]["end"]
    with pytest.raises(WorkUnitError):
        make_document_segments("doc", text, [(0, 8)])
